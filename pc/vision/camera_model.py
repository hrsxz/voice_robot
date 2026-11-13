"""
相机模型 (改动 vision-field-model 设计 D1、D3、D5、D7): 内参 + 相机相对场地的位姿。

场地坐标系: x 向右, y 向下 (从上往下看), 单位厘米; 高度向上为正。
内部的三维坐标用 (x, y, -高度): 这样 x 右、y 下、z 向下 (指向地面以下), 是右手系,
可以直接交给 OpenCV。对外的接口都用"高度"(正数)。

    model = CameraModel(intrinsics, rvec, tvec)     # 场地 -> 相机: X_cam = R · X + t
    px = model.project([(x, y, height), ...])       # 场地点 -> 像素 (含畸变)
    xy = model.pixel_to_plane(px, height)           # 像素 -> 视线和"高度 = height"平面的交点
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml


class IntrinsicsError(ValueError):
    """内参文件不能用 (分辨率不一致、误差过大、格式不对)。"""


@dataclass
class Intrinsics:
    K: np.ndarray  # 3×3
    dist: np.ndarray  # 畸变系数 (k1, k2, p1, p2, k3, ...)
    width: int
    height: int
    rms: float | None = None  # 标定的重投影误差 (像素)
    _rays: np.ndarray | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_fov(cls, width: int, height: int, hfov_deg: float,
                 dist: Any = None) -> Intrinsics:
        """按水平视场角构造 (合成画面和没有标定文件时的近似)。"""
        f = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
        K = np.array([[f, 0, (width - 1) / 2], [0, f, (height - 1) / 2], [0, 0, 1]], float)
        d = np.zeros(5) if dist is None else np.asarray(dist, float)
        return cls(K, d, width, height)

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def check_size(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height):
            raise IntrinsicsError(
                f"内参是在 {self.width}×{self.height} 下标定的，当前采集分辨率是 "
                f"{width}×{height}：请在当前分辨率下重新标定")

    def normalized(self, pixels: np.ndarray) -> np.ndarray:
        """像素 -> 去畸变后的归一化坐标 (x/z, y/z)。"""
        pts = np.asarray(pixels, np.float64).reshape(-1, 1, 2)
        return cv2.undistortPoints(pts, self.K, self.dist,
                                   criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS,
                                             30, 1e-9)).reshape(-1, 2)

    def ray_grid(self) -> np.ndarray:
        """每个像素的归一化坐标 (H×W×2), 第一次调用时计算并缓存 (合成画面和俯视图用)。"""
        if self._rays is None:
            u, v = np.meshgrid(np.arange(self.width, dtype=np.float64),
                               np.arange(self.height, dtype=np.float64))
            pts = np.stack([u.ravel(), v.ravel()], axis=1)
            self._rays = self.normalized(pts).reshape(self.height, self.width, 2).astype(
                np.float32)
        return self._rays

    # ---------------- 文件 ----------------

    def save(self, path: str | Path, max_rms: float = 1.0) -> Path:
        """保存到 YAML。重投影误差超过 max_rms 像素时拒绝保存。"""
        if self.rms is not None and self.rms > max_rms:
            raise IntrinsicsError(
                f"重投影误差 {self.rms:.2f} 像素超过 {max_rms:.1f} 像素：请重新拍摄标定照片，"
                "这次结果不保存")
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "width": self.width, "height": self.height,
            "rms_px": None if self.rms is None else round(float(self.rms), 4),
            "camera_matrix": [[float(v) for v in row] for row in self.K],
            "dist_coeffs": [float(v) for v in np.ravel(self.dist)],
        }
        p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path, expected_size: tuple[int, int] | None = None) -> Intrinsics:
        p = Path(path)
        if not p.exists():
            raise IntrinsicsError(f"没有内参文件 {p}：请先标定（README 第 15 节）")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        try:
            intr = cls(np.array(data["camera_matrix"], float),
                       np.array(data["dist_coeffs"], float),
                       int(data["width"]), int(data["height"]), data.get("rms_px"))
        except (KeyError, TypeError, ValueError) as exc:
            raise IntrinsicsError(f"内参文件 {p} 格式不对：{exc}") from exc
        if intr.K.shape != (3, 3):
            raise IntrinsicsError(f"内参文件 {p}：camera_matrix 不是 3×3")
        if expected_size is not None:
            intr.check_size(*expected_size)
        return intr


def _to3d(points: Any) -> np.ndarray:
    """(x, y) 或 (x, y, 高度) -> 内部三维坐标 (x, y, -高度)。"""
    arr = np.asarray(points, np.float64)
    if arr.ndim == 1:
        arr = arr[None, :]
    if arr.shape[1] == 2:
        arr = np.hstack([arr, np.zeros((len(arr), 1))])
    out = arr.copy()
    out[:, 2] = -arr[:, 2]
    return out


class CameraModel:
    def __init__(self, intrinsics: Intrinsics, rvec: Any, tvec: Any) -> None:
        self.intr = intrinsics
        self.rvec = np.asarray(rvec, np.float64).reshape(3, 1)
        self.tvec = np.asarray(tvec, np.float64).reshape(3, 1)
        self.R, _ = cv2.Rodrigues(self.rvec)
        center = (-self.R.T @ self.tvec).ravel()
        self.center = (float(center[0]), float(center[1]), float(-center[2]))  # (x, y, 高度)

    @classmethod
    def look_at(cls, intrinsics: Intrinsics, eye: tuple[float, float, float],
                target: tuple[float, float, float], roll_deg: float = 0.0) -> CameraModel:
        """相机在 eye (x, y, 高度), 光轴对准 target。合成画面和测试用。"""
        e, t = _to3d(eye)[0], _to3d(target)[0]
        z = t - e
        z /= np.linalg.norm(z)
        up = np.array([0.0, 0.0, -1.0])  # 场地的"上方" (内部坐标 z 向下)
        x = np.cross(z, up)  # 画面向右 (相机 y 轴朝下, 所以 x = z × (-y_cam))
        if np.linalg.norm(x) < 1e-9:
            x = np.array([1.0, 0.0, 0.0])
        x /= np.linalg.norm(x)
        y = np.cross(z, x)
        R = np.stack([x, y, z])
        if roll_deg:
            c, s = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
            R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ R
        rvec, _ = cv2.Rodrigues(R)
        return cls(intrinsics, rvec, -R @ e)

    # ---------------- 投影 ----------------

    def project(self, points: Any) -> np.ndarray:
        """场地点 (x, y[, 高度]) -> 像素 (N×2), 包含镜头畸变。"""
        obj = _to3d(points)
        px, _ = cv2.projectPoints(obj, self.rvec, self.tvec, self.intr.K, self.intr.dist)
        return px.reshape(-1, 2)

    def in_front(self, points: Any) -> np.ndarray:
        cam = (self.R @ _to3d(points).T + self.tvec).T
        return cam[:, 2] > 1e-6

    def pixel_to_plane(self, pixels: Any, height: float = 0.0) -> np.ndarray:
        """像素 -> 视线和"离地 height 厘米的水平面"的交点 (N×2, 场地坐标)。看不到平面的为 NaN。"""
        n = self.intr.normalized(np.asarray(pixels, np.float64).reshape(-1, 2))
        return self._normalized_to_plane(n, height)

    def _normalized_to_plane(self, n: np.ndarray, height: float) -> np.ndarray:
        d_cam = np.concatenate([n, np.ones((len(n), 1))], axis=1)
        d = d_cam @ self.R  # 每行 = R^T · d_cam (视线在场地坐标系里的方向)
        c = np.array([self.center[0], self.center[1], -self.center[2]])
        s = (-height - c[2]) / d[:, 2]
        out = c[None, :2] + s[:, None] * d[:, :2]
        out[~(s > 0)] = np.nan
        return out

    def plane_grid(self, height: float = 0.0) -> np.ndarray:
        """每个像素的视线和平面的交点 (H×W×2, 场地坐标); 看不到平面的为 NaN。"""
        rays = self.intr.ray_grid().reshape(-1, 2).astype(np.float64)
        return self._normalized_to_plane(rays, height).reshape(
            self.intr.height, self.intr.width, 2)

    def ground_shift(self, xy: Any, height: float) -> np.ndarray:
        """
        高度为 height 的点, 在地面 (俯视图) 上看到的位置: 从相机正下方的点往外"拉长"。
        反过来 (unshift) 用于视差修正 (设计 D8)。
        """
        cx, cy, hc = self.center
        p = np.asarray(xy, np.float64).reshape(-1, 2)
        k = hc / (hc - height)
        return np.stack([cx + (p[:, 0] - cx) * k, cy + (p[:, 1] - cy) * k], axis=1)

    def ground_unshift(self, xy: Any, height: float) -> np.ndarray:
        cx, cy, hc = self.center
        p = np.asarray(xy, np.float64).reshape(-1, 2)
        k = (hc - height) / hc
        return np.stack([cx + (p[:, 0] - cx) * k, cy + (p[:, 1] - cy) * k], axis=1)


__all__ = ["CameraModel", "Intrinsics", "IntrinsicsError"]
