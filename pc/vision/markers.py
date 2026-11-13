"""
ArUco marker: 检测, 以及 marker 四个角在场地上的位置 (设计 D4、D5)。

角点顺序和 OpenCV 一致: 左上、右上、右下、左下 (以 marker 图案的"上边"为上)。
marker 的"上边"朝向用 up_deg 表示 (场地朝向: 0° = +x, 顺时针为正):
- anchor: rotation_deg = 0 时"上边"朝场地上方 (-y), 即 up_deg = 270°;
- 车顶 marker: rotation_deg = 0 时"上边"朝车头, 即 up_deg = 小车朝向。
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

from pc.field.geometry import Point

ANCHOR_UP_DEG = 270.0


def marker_axes(up_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """marker 的"上"方向和"右"方向 (场地坐标的单位向量)。"""
    a = math.radians(up_deg)
    up = np.array([math.cos(a), math.sin(a)])
    right = np.array([-math.sin(a), math.cos(a)])
    return up, right


def marker_corners(center: Point, up_deg: float, size: float) -> np.ndarray:
    """四个角的场地坐标 (4×2), 顺序: 左上、右上、右下、左下。"""
    up, right = marker_axes(up_deg)
    c = np.asarray(center, float)
    h = size / 2
    return np.array([c + up * h - right * h, c + up * h + right * h,
                     c - up * h + right * h, c - up * h - right * h])


def heading_from_corners(corners: Any) -> tuple[Point, float]:
    """场地坐标里的四个角 -> (中心, "上边"朝向 up_deg)。"""
    c = np.asarray(corners, float).reshape(4, 2)
    center = c.mean(axis=0)
    top = (c[0] + c[1]) / 2
    bottom = (c[3] + c[2]) / 2
    v = top - bottom
    return (float(center[0]), float(center[1])), math.degrees(math.atan2(v[1], v[0])) % 360


def get_dictionary(name: str = "DICT_4X4_50"):
    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))


def marker_image(marker_id: int, pixels: int = 120, dictionary: str = "DICT_4X4_50",
                 border: float = 0.25) -> np.ndarray:
    """marker 图案, 外面加一圈白边 (border = 白边宽度 / marker 边长, 两边各一圈)。"""
    inner = cv2.aruco.generateImageMarker(get_dictionary(dictionary), marker_id, pixels)
    pad = round(pixels * border)
    return cv2.copyMakeBorder(inner, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)


class MarkerDetector:
    def __init__(self, dictionary: str = "DICT_4X4_50") -> None:
        params = cv2.aruco.DetectorParameters()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.detector = cv2.aruco.ArucoDetector(get_dictionary(dictionary), params)

    def detect(self, frame: np.ndarray) -> dict[int, np.ndarray]:
        """画面里的 marker: {id: 4×2 像素角点}。同一个 id 出现多次时取面积最大的。"""
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = self.detector.detectMarkers(gray)
        out: dict[int, np.ndarray] = {}
        if ids is None:
            return out
        for c, i in zip(corners, ids.ravel(), strict=True):
            pts = c.reshape(4, 2).astype(np.float64)
            mid = int(i)
            if mid in out and cv2.contourArea(out[mid].astype(np.float32)) >= \
                    cv2.contourArea(pts.astype(np.float32)):
                continue
            out[mid] = pts
        return out


__all__ = ["ANCHOR_UP_DEG", "MarkerDetector", "get_dictionary", "heading_from_corners",
           "marker_axes", "marker_corners", "marker_image"]
