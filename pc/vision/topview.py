"""
矫正俯视图、空场地参考图、光照检查 (改动 vision-field-model 设计 D7、D10, 任务 5.1)。

俯视图: 去畸变后的画面按相机位姿投影到地面 (高度 0), 默认 4 像素/厘米 (约 945 × 457)。
地面上的东西在俯视图里位置准确; 有高度的东西会朝远离相机的方向"拉长" (物体检测里修正)。

参考图: 场地上没有小车和物体时拍一张俯视图, 存成 PNG + 同名 .yaml (分辨率、亮度、相机位置),
之后重复使用。相机位置变了 (和参考图记录的相差超过 2 cm) 时提示重新拍。
光照检查: 比较参考图和当前俯视图里"静态区域"(排除小车和物体附近) 的平均亮度。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from pc.field.geometry import Point
from pc.field.model import FieldModel
from pc.vision.camera_model import CameraModel

CAMERA_MOVED_CM = 2.0


class TopView:
    def __init__(self, model: CameraModel, fm: FieldModel, px_per_cm: float = 4.0,
                 margin_cm: float = 0.0) -> None:
        self.model = model
        self.field = fm
        self.ppc = px_per_cm
        self.margin = margin_cm
        self.width = round((fm.width + 2 * margin_cm) * px_per_cm)
        self.height = round((fm.height + 2 * margin_cm) * px_per_cm)
        u, v = np.meshgrid(np.arange(self.width), np.arange(self.height))
        xy = np.stack([(u.ravel() + 0.5) / px_per_cm - margin_cm,
                       (v.ravel() + 0.5) / px_per_cm - margin_cm], axis=1)
        px = model.project(xy)
        self.map_x = px[:, 0].reshape(self.height, self.width).astype(np.float32)
        self.map_y = px[:, 1].reshape(self.height, self.width).astype(np.float32)

    def rectify(self, frame: np.ndarray) -> np.ndarray:
        return cv2.remap(frame, self.map_x, self.map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))

    # 俯视图像素 <-> 场地坐标
    def to_px(self, p: Any) -> np.ndarray:
        a = np.asarray(p, float).reshape(-1, 2)
        return (a + self.margin) * self.ppc - 0.5

    def to_field(self, px: Any) -> np.ndarray:
        a = np.asarray(px, float).reshape(-1, 2)
        return (a + 0.5) / self.ppc - self.margin

    def polygon_mask(self, polygons: list[list[Point]], dilate_cm: float = 0.0) -> np.ndarray:
        mask = np.zeros((self.height, self.width), np.uint8)
        for poly in polygons:
            pts = np.round(self.to_px(poly) * 16).astype(np.int32)
            cv2.fillPoly(mask, [pts], 255, cv2.LINE_8, 4)
        if dilate_cm > 0:
            k = max(1, round(dilate_cm * self.ppc))
            mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                                              (2 * k + 1, 2 * k + 1)))
        return mask


# ---------------------------------------------------------------- 参考图


@dataclass
class Reference:
    image: np.ndarray
    px_per_cm: float
    camera_center: tuple[float, float, float]
    brightness: float

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), self.image)
        meta = {"px_per_cm": self.px_per_cm,
                "camera_center": [round(float(v), 2) for v in self.camera_center],
                "brightness": round(float(self.brightness), 2)}
        p.with_suffix(".yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: str | Path) -> Reference:
        p = Path(path)
        img = cv2.imread(str(p))
        if img is None:
            raise FileNotFoundError(f"没有参考图 {p}：场地清空后先拍一张")
        meta = yaml.safe_load(p.with_suffix(".yaml").read_text(encoding="utf-8")) or {}
        return cls(img, float(meta["px_per_cm"]), tuple(meta["camera_center"]),
                   float(meta["brightness"]))

    @classmethod
    def capture(cls, topview_image: np.ndarray, view: TopView) -> Reference:
        return cls(topview_image, view.ppc, view.model.center, mean_brightness(topview_image))

    def check_compatible(self, view: TopView) -> str | None:
        """参考图能不能用于当前的俯视图; 不能时返回原因。"""
        if self.image.shape[:2] != (view.height, view.width) or self.px_per_cm != view.ppc:
            return "参考图的分辨率和当前俯视图不同，请重新拍摄参考图"
        d = np.linalg.norm(np.subtract(self.camera_center, view.model.center))
        if d > CAMERA_MOVED_CM:
            return f"相机位置和拍参考图时相差 {d:.1f} cm，请重新拍摄参考图"
        return None


def mean_brightness(img: np.ndarray, mask: np.ndarray | None = None) -> float:
    L = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)  # 灰度和光照强度大致成正比
    valid = np.any(img > 0, axis=2)  # 俯视图里相机看不到的地方是黑的, 不算
    if mask is not None:
        valid &= mask > 0
    return float(L[valid].mean()) if valid.any() else 0.0


@dataclass
class LightingCheck:
    ratio: float  # 当前亮度 / 参考图亮度
    warn: bool
    message: str = ""


def check_lighting(reference: np.ndarray, current: np.ndarray,
                   exclude: np.ndarray | None = None, max_change: float = 0.25) -> LightingCheck:
    """exclude: 不参与比较的区域 (小车和物体附近, 255 = 排除)。"""
    static = None if exclude is None else (exclude == 0).astype(np.uint8)
    ref = mean_brightness(reference, static)
    cur = mean_brightness(current, static)
    ratio = cur / ref if ref > 0 else 1.0
    warn = abs(ratio - 1.0) > max_change
    msg = (f"光照和参考图差别过大（亮度 {ratio:.0%}），建议重新拍摄参考图" if warn else "")
    return LightingCheck(ratio, warn, msg)


__all__ = ["LightingCheck", "Reference", "TopView", "check_lighting", "mean_brightness"]
