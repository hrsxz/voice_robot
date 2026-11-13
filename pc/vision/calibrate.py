"""
镜头内参标定 (改动 vision-field-model 设计 D3, 任务 2.1)。

用打印的棋盘格 (默认 9×6 个内角点、格子 25 mm) 拍 15–20 张不同角度的照片 -> 内参和畸变系数,
连同分辨率保存成 YAML (默认 pc/config/local/camera_intrinsics.yaml)。
重投影误差超过 1 像素时提示重新拍摄, 不保存。

用法:
    python -m pc.vision.calibrate          # 打开摄像头: 空格拍一张 (认出棋盘格才算), c 计算, q 退出
    python -m pc.vision.calibrate --images 照片目录    # 用已经拍好的照片
    python -m pc.vision.calibrate --pattern 9x6 --square 25
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from pc.vision.camera_model import Intrinsics, IntrinsicsError
from pc.vision.config import VisionConfig

MIN_IMAGES = 10
MAX_RMS = 1.0


@dataclass
class CalibrationResult:
    intrinsics: Intrinsics | None
    rms: float | None
    used: int
    rejected: list[int] = field(default_factory=list)  # 没认出棋盘格的照片序号
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.intrinsics is not None and self.rms is not None and self.rms <= MAX_RMS


def find_corners(image: np.ndarray, pattern: tuple[int, int]) -> np.ndarray | None:
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    ok, corners = cv2.findChessboardCornersSB(gray, pattern, flags=cv2.CALIB_CB_EXHAUSTIVE |
                                              cv2.CALIB_CB_ACCURACY)
    if not ok:
        ok, corners = cv2.findChessboardCorners(gray, pattern)
        if not ok:
            return None
        corners = cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
                                   (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3))
    return corners.reshape(-1, 2).astype(np.float32)


def board_points(pattern: tuple[int, int], square: float) -> np.ndarray:
    cols, rows = pattern
    grid = np.zeros((rows * cols, 3), np.float32)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * square
    return grid


def calibrate_images(images: Iterable[np.ndarray], pattern: tuple[int, int] = (9, 6),
                     square: float = 25.0) -> CalibrationResult:
    obj_pts, img_pts, rejected = [], [], []
    size: tuple[int, int] | None = None
    template = board_points(pattern, square)
    for i, img in enumerate(images):
        h, w = img.shape[:2]
        if size is None:
            size = (w, h)
        elif size != (w, h):
            rejected.append(i)
            continue
        corners = find_corners(img, pattern)
        if corners is None:
            rejected.append(i)
            continue
        obj_pts.append(template)
        img_pts.append(corners)
    if size is None or len(img_pts) < MIN_IMAGES:
        return CalibrationResult(None, None, len(img_pts), rejected,
                                 f"认出棋盘格的照片只有 {len(img_pts)} 张，至少要 {MIN_IMAGES} 张"
                                 "（建议 15–20 张，覆盖画面四角和不同倾斜角度）")
    rms, K, dist, _r, _t = cv2.calibrateCamera(obj_pts, img_pts, size, None, None)
    intr = Intrinsics(K, dist.ravel(), size[0], size[1], float(rms))
    msg = f"重投影误差 {rms:.2f} 像素（{len(img_pts)} 张照片）"
    if rms > MAX_RMS:
        msg += f"，超过 {MAX_RMS:.0f} 像素：请重新拍摄标定照片，这次结果不保存"
    return CalibrationResult(intr, float(rms), len(img_pts), rejected, msg)


def save_result(result: CalibrationResult, path: str | Path) -> Path:
    if result.intrinsics is None:
        raise IntrinsicsError(result.message)
    return result.intrinsics.save(path, MAX_RMS)


# ---------------------------------------------------------------- 命令行


def _parse_pattern(text: str) -> tuple[int, int]:
    a, b = text.lower().split("x")
    return int(a), int(b)


def _capture(cfg: VisionConfig, pattern: tuple[int, int]) -> list[np.ndarray]:
    from pc.vision.camera_stream import CameraStream

    cam = CameraStream.from_config(cfg.camera)
    for w in cam.open():
        print(f"警告：{w}")
    shots: list[np.ndarray] = []
    print("对准棋盘格按空格拍一张（认出棋盘格才保存），拍够 15–20 张后按 c 计算，q 退出")
    try:
        while True:
            frame = cam.read()
            view = frame.copy()
            corners = find_corners(frame, pattern)
            if corners is not None:
                cv2.drawChessboardCorners(view, pattern, corners.reshape(-1, 1, 2), True)
            cv2.putText(view, f"{len(shots)} shots", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                        (0, 0, 255), 2)
            cv2.imshow("calibrate", cv2.resize(view, None, fx=0.5, fy=0.5))
            key = cv2.waitKey(1) & 0xFF
            if key == ord(" ") and corners is not None:
                shots.append(frame)
                print(f"已拍 {len(shots)} 张")
            elif key == ord("c"):
                break
            elif key == ord("q"):
                return []
    finally:
        cam.release()
        cv2.destroyAllWindows()
    return shots


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pc.vision.calibrate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--images", help="已经拍好的照片目录（jpg / png）")
    p.add_argument("--pattern", default="9x6", help="内角点数（列x行），默认 9x6")
    p.add_argument("--square", type=float, default=25.0, help="格子边长（毫米）")
    p.add_argument("--out", default=None,
                   help="输出文件（默认 pc/config/local/camera_intrinsics.yaml）")
    args = p.parse_args(argv)
    cfg = VisionConfig.load()
    pattern = _parse_pattern(args.pattern)
    if args.images:
        files = sorted(f for f in Path(args.images).iterdir()
                       if f.suffix.lower() in (".jpg", ".jpeg", ".png"))
        images = [cv2.imread(str(f)) for f in files]
    else:
        images = _capture(cfg, pattern)
    if not images:
        print("没有照片")
        return 2
    result = calibrate_images(images, pattern, args.square)
    print(result.message)
    if result.rejected:
        nums = ", ".join(str(i + 1) for i in result.rejected)
        print(f"没认出棋盘格（或分辨率不同）的照片：第 {nums} 张")
    if not result.ok:
        return 1
    out = save_result(result, args.out or cfg.intrinsics_path)
    print(f"已保存 {out}（{result.intrinsics.width}×{result.intrinsics.height}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
