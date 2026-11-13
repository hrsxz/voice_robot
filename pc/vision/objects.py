"""
物体检测 (改动 vision-field-model 设计 D8, 任务 5.2)。

当前俯视图和空场地参考图在 Lab 色彩空间里做差分 -> 阈值 -> 形态学处理 -> 连通区域
-> 去掉小车所在的区域 (由位姿和车身轮廓算出, 包括车身高度造成的"拉长")
-> 按区域里饱和度够高的像素的色相判断颜色 (红、绿、蓝、黄)。
场地上印的同色区域和圆圈在参考图里也有, 差分时就抵消了, 所以不会被当成物体。

位置: 区域的质心是物体底面和"拉长"的顶面的中间, 按物体高度的一半朝相机正下方的点缩回
(设计 D8 的视差修正)。物体高度要等确定身份以后才知道, 所以检测结果保留原始质心,
用 position(高度) 计算修正后的位置。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from pc.field.geometry import Point, Pose
from pc.field.robot_geometry import RobotGeometry
from pc.vision.config import DetectionConfig
from pc.vision.topview import TopView

# OpenCV HSV 色相 (0–180)
HUES = {"red": [(0, 10), (165, 180)], "yellow": [(18, 38)], "green": [(40, 90)],
        "blue": [(95, 135)]}
MIN_COLOR_FRACTION = 0.3
WEAK_FRACTION = 0.4  # 弱阈值 = 阈值 × 0.4


@dataclass(frozen=True)
class ObjectDetection:
    color: str  # red / green / blue / yellow / unknown
    ground: Point  # 俯视图里的质心 (没有视差修正)
    area_cm2: float
    view: TopView | None = None

    def position(self, height: float) -> Point:
        """按物体高度做视差修正后的底面中心。"""
        if self.view is None or height <= 0:
            return self.ground
        p = self.view.model.ground_unshift([self.ground], height / 2)[0]
        return (float(p[0]), float(p[1]))


def classify_color(hsv_pixels: np.ndarray, min_saturation: int) -> str:
    if len(hsv_pixels) == 0:
        return "unknown"
    sat = hsv_pixels[(hsv_pixels[:, 1] >= min_saturation) & (hsv_pixels[:, 2] >= 40)]
    if len(sat) < max(5, 0.15 * len(hsv_pixels)):
        return "unknown"
    h = sat[:, 0].astype(int)
    counts = {c: int(sum(((h >= lo) & (h < hi)).sum() for lo, hi in ranges))
              for c, ranges in HUES.items()}
    color, n = max(counts.items(), key=lambda kv: kv[1])
    return color if n >= MIN_COLOR_FRACTION * len(sat) else "unknown"


def robot_mask(view: TopView, pose: Pose, geom: RobotGeometry, body_height: float,
               margin_cm: float) -> np.ndarray:
    """小车在俯视图里占的区域: 车身 + 放下的夹爪, 以及车身高度造成的"拉长"部分。"""
    base = np.array(geom.footprint_polygon(pose, True))
    top = view.model.ground_shift(base, body_height)
    hull = cv2.convexHull(np.vstack([base, top]).astype(np.float32)).reshape(-1, 2)
    return view.polygon_mask([list(map(tuple, hull))], margin_cm)


class ObjectDetector:
    def __init__(self, view: TopView, reference: np.ndarray,
                 config: DetectionConfig | None = None, geom: RobotGeometry | None = None,
                 robot_height: float = 10.0) -> None:
        self.view = view
        self.config = config or DetectionConfig()
        self.geom = geom or RobotGeometry.load()
        self.robot_height = robot_height
        self.set_reference(reference)

    def set_reference(self, reference: np.ndarray) -> None:
        self.reference = reference
        self._ref_lab = cv2.cvtColor(reference, cv2.COLOR_BGR2LAB).astype(np.float32)
        self._valid = np.any(reference > 0, axis=2)

    def diff_mask(self, top: np.ndarray, robot: Pose | None = None) -> np.ndarray:
        c = self.config
        lab = cv2.cvtColor(top, cv2.COLOR_BGR2LAB).astype(np.float32)
        d = lab - self._ref_lab
        d[..., 0] *= 0.5  # 亮度差 (阴影) 权重减半
        de = np.sqrt((d ** 2).sum(axis=2))
        strong = ((de > c.diff_threshold) & self._valid).astype(np.uint8) * 255
        k = max(1, round(0.4 * self.view.ppc))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
        strong = cv2.morphologyEx(strong, cv2.MORPH_OPEN, kernel)
        # 双阈值: 和强差异区域相连的弱差异像素也算 (物体和下面印的颜色相近时, 例如蓝色大人在 C 区里)
        weak = ((de > c.diff_threshold * WEAK_FRACTION) & self._valid).astype(np.uint8)
        n, labels = cv2.connectedComponents(weak, connectivity=8)
        keep = np.zeros(n, bool)
        keep[np.unique(labels[strong > 0])] = True
        keep[0] = False
        mask = keep[labels].astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        if robot is not None:
            mask[robot_mask(self.view, robot, self.geom, self.robot_height,
                            c.robot_margin_cm) > 0] = 0
        return mask

    def detect(self, top: np.ndarray, robot: Pose | None = None) -> list[ObjectDetection]:
        c = self.config
        mask = self.diff_mask(top, robot)
        n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
        hsv = cv2.cvtColor(top, cv2.COLOR_BGR2HSV)
        area_px = self.view.ppc ** 2
        out: list[ObjectDetection] = []
        for i in range(1, n):
            area = stats[i, cv2.CC_STAT_AREA] / area_px
            if not c.min_area_cm2 <= area <= c.max_area_cm2:
                continue
            pix = hsv[labels == i]
            color = classify_color(pix, c.min_saturation)
            g = self.view.to_field(centroids[i])[0]
            out.append(ObjectDetection(color, (float(g[0]), float(g[1])), float(area),
                                       self.view))
        return out


__all__ = ["HUES", "ObjectDetection", "ObjectDetector", "classify_color", "robot_mask"]
