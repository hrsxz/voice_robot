"""
小车定位 (改动 vision-field-model 设计 D5, 任务 4.1–4.2)。

车顶 marker 的 4 个角点像素 -> 去畸变 -> 视线和"离地 marker 高度"的平面求交 -> 4 个角点的场地坐标
-> marker 中心和"上边"朝向 -> 按配置的偏移得到转弯中心和朝向 -> 笼区中心。
朝向在场地平面里计算 (不是在画面像素里)。

看不到 marker 时如实报告: found = False, 附带最后一次的位姿和距今多久;
不会把旧位姿当作当前测量结果 (spec: MUST NOT)。

latest() 的结果可以直接交给 pc.navigation.ports.VisionPoseSource。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from pc.field.geometry import Point, Pose
from pc.field.robot_geometry import RobotGeometry
from pc.vision.camera_model import CameraModel
from pc.vision.config import MarkerConfig
from pc.vision.markers import heading_from_corners

SIZE_TOLERANCE = 0.25  # 算出来的 marker 边长和配置相差超过 25% -> 认为是误检


@dataclass(frozen=True)
class LocalizerReading:
    found: bool
    timestamp: float
    x: float | None = None  # 转弯中心
    y: float | None = None
    heading: float | None = None
    cage_center: Point | None = None
    marker_center: Point | None = None
    last_pose: Pose | None = None  # 看不见时: 最后一次看到的位姿
    age_s: float | None = None  # 看不见时: 最后一次看到距今多久
    reason: str = ""

    @property
    def pose(self) -> Pose | None:
        if not self.found or self.x is None or self.y is None or self.heading is None:
            return None
        return Pose(self.x, self.y, self.heading)


class RobotLocalizer:
    def __init__(self, model: CameraModel, marker: MarkerConfig | None = None,
                 geom: RobotGeometry | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.model = model
        self.marker = marker or MarkerConfig()
        self.geom = geom or RobotGeometry.load()
        self.clock = clock
        self._latest: LocalizerReading | None = None
        self._last_pose: Pose | None = None
        self._last_seen: float | None = None

    def set_model(self, model: CameraModel) -> None:
        """相机位姿重新计算之后调用。"""
        self.model = model

    def locate(self, corners_px: np.ndarray) -> tuple[Pose, Point, Point] | None:
        """marker 角点像素 -> (转弯中心位姿, 笼区中心, marker 中心); 算不出或像误检时为 None。"""
        m = self.marker
        pts = self.model.pixel_to_plane(np.asarray(corners_px, float).reshape(4, 2),
                                        m.height_cm)
        if not np.all(np.isfinite(pts)):
            return None
        sides = np.linalg.norm(pts - np.roll(pts, -1, axis=0), axis=1)
        if abs(float(sides.mean()) - m.size_cm) > SIZE_TOLERANCE * m.size_cm:
            return None
        center, up = heading_from_corners(pts)
        heading = (up - m.rotation_deg) % 360
        fwd, right = m.offset_cm
        c, s = math.cos(math.radians(heading)), math.sin(math.radians(heading))
        # 转弯中心 = marker 中心 - 偏移 (偏移在车身坐标系里: 前, 右)
        x = center[0] - (fwd * c - right * s)
        y = center[1] - (fwd * s + right * c)
        pose = Pose(x, y, heading)
        return pose, self.geom.cage_center(pose), center

    def update(self, detections: dict[int, np.ndarray],
               timestamp: float | None = None) -> LocalizerReading:
        ts = self.clock() if timestamp is None else timestamp
        corners = detections.get(self.marker.id)
        result = self.locate(corners) if corners is not None else None
        if result is None:
            reason = ("看不到车顶 marker" if corners is None
                      else "车顶 marker 的尺寸不对（可能是误检）")
            reading = LocalizerReading(
                False, ts, last_pose=self._last_pose,
                age_s=None if self._last_seen is None else max(0.0, ts - self._last_seen),
                reason=reason)
        else:
            pose, cage, mc = result
            self._last_pose = pose
            self._last_seen = ts
            reading = LocalizerReading(True, ts, pose.x, pose.y, pose.heading, cage, mc)
        self._latest = reading
        return reading

    def latest(self) -> LocalizerReading | None:
        return self._latest


__all__ = ["LocalizerReading", "RobotLocalizer"]
