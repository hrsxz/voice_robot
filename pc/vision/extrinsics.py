"""
相机位姿 (外参, 改动 vision-field-model 设计 D4, 任务 2.2–2.3)。

启动时: 4 个场外 anchor 的 16 个角点 + solvePnP (场地垫厚度忽略不计, anchor 高度为 0)。
少于 4 个 anchor 时报告缺少哪些, 不给出相机位姿 (之后也就不输出任何场地坐标)。
运行中: 每 5 秒复查一次 anchor, 偏差超过 1 cm (例如三脚架被碰了) 就重新计算并警告。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from pc.field.geometry import Point
from pc.field.model import FieldModel
from pc.vision.camera_model import CameraModel, Intrinsics, _to3d
from pc.vision.config import AnchorConfig
from pc.vision.markers import ANCHOR_UP_DEG, marker_corners


@dataclass(frozen=True)
class AnchorSpec:
    id: int
    center: Point
    size: float
    rotation: float = 0.0

    def corners(self) -> np.ndarray:
        return marker_corners(self.center, ANCHOR_UP_DEG + self.rotation, self.size)


def anchor_specs(fm: FieldModel, cfg: AnchorConfig | None = None) -> dict[int, AnchorSpec]:
    cfg = cfg or AnchorConfig()
    return {i: AnchorSpec(i, p, cfg.size_cm, cfg.rotation_deg) for i, p in fm.anchors.items()}


@dataclass
class PoseEstimate:
    model: CameraModel | None
    found: list[int]
    missing: list[int]
    reprojection_px: float | None = None
    error_cm: float | None = None  # anchor 中心反投影到地面后和配置位置的平均偏差
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.model is not None


def estimate_camera_pose(detections: dict[int, np.ndarray], anchors: dict[int, AnchorSpec],
                         intr: Intrinsics) -> PoseEstimate:
    """用全部 4 个 anchor 求相机位姿; 缺 anchor 时不求 (spec: MUST NOT 输出场地坐标)。"""
    found = sorted(i for i in anchors if i in detections)
    missing = sorted(i for i in anchors if i not in detections)
    if missing:
        names = "、".join(str(i) for i in missing)
        return PoseEstimate(None, found, missing,
                            message=f"缺少 anchor {names}（检测到 {len(found)} 个，需要 "
                                    f"{len(anchors)} 个），不输出场地坐标")
    obj = np.vstack([_to3d(anchors[i].corners()) for i in found])
    img = np.vstack([detections[i] for i in found]).astype(np.float64)
    ok, rvec, tvec = cv2.solvePnP(obj, img, intr.K, intr.dist, flags=cv2.SOLVEPNP_IPPE)
    if not ok:
        ok, rvec, tvec = cv2.solvePnP(obj, img, intr.K, intr.dist,
                                      flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return PoseEstimate(None, found, missing, message="solvePnP 失败")
    rvec, tvec = cv2.solvePnPRefineLM(obj, img, intr.K, intr.dist, rvec, tvec)
    model = CameraModel(intr, rvec, tvec)
    proj = model.project(np.c_[obj[:, :2], -obj[:, 2]])
    rep = float(np.mean(np.linalg.norm(proj - img, axis=1)))
    err = anchor_deviation(model, detections, anchors)
    mean_cm = float(np.mean(list(err.values())))
    return PoseEstimate(model, found, missing, rep, mean_cm,
                        f"相机位姿已计算：anchor 平均误差 {mean_cm:.2f} cm"
                        f"（重投影 {rep:.2f} 像素），"
                        f"相机在 ({model.center[0]:.0f}, {model.center[1]:.0f})、"
                        f"高 {model.center[2]:.0f} cm")


def anchor_deviation(model: CameraModel, detections: dict[int, np.ndarray],
                     anchors: dict[int, AnchorSpec]) -> dict[int, float]:
    """每个检测到的 anchor: 角点反投影到地面后的中心, 和配置位置相差多少厘米。"""
    out: dict[int, float] = {}
    for i, spec in anchors.items():
        if i not in detections:
            continue
        pts = model.pixel_to_plane(detections[i], 0.0)
        if not np.all(np.isfinite(pts)):
            continue
        c = pts.mean(axis=0)
        out[i] = float(np.hypot(c[0] - spec.center[0], c[1] - spec.center[1]))
    return out


@dataclass
class DriftCheck:
    checked: bool  # 这次有没有复查 (没到时间、anchor 不全时不复查)
    max_cm: float | None = None
    recalibrated: bool = False
    warning: str | None = None


@dataclass
class CameraPoseMonitor:
    """保存当前相机位姿, 定期复查 anchor (设计 D4: 每 5 秒, 偏差 > 1 cm 就重新计算)。"""

    anchors: dict[int, AnchorSpec]
    intr: Intrinsics
    recheck_s: float = 5.0
    drift_cm: float = 1.0
    clock: Callable[[], float] | None = None
    estimate: PoseEstimate | None = None
    last_check: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def model(self) -> CameraModel | None:
        return self.estimate.model if self.estimate is not None else None

    def start(self, detections: dict[int, np.ndarray], now: float = 0.0) -> PoseEstimate:
        self.estimate = estimate_camera_pose(detections, self.anchors, self.intr)
        self.last_check = now
        return self.estimate

    def maybe_check(self, detections: dict[int, np.ndarray], now: float) -> DriftCheck:
        if self.model is None:
            return DriftCheck(False)
        if self.last_check is not None and now - self.last_check < self.recheck_s:
            return DriftCheck(False)
        self.last_check = now
        if any(i not in detections for i in self.anchors):
            return DriftCheck(False)  # anchor 被挡住时不判断 (保留当前位姿)
        dev = anchor_deviation(self.model, detections, self.anchors)
        worst = max(dev.values()) if dev else 0.0
        if worst <= self.drift_cm:
            return DriftCheck(True, worst)
        new = estimate_camera_pose(detections, self.anchors, self.intr)
        if new.ok:
            self.estimate = new
        msg = (f"相机位置发生了变化（anchor 偏差 {worst:.1f} cm），已重新计算相机位姿"
               if new.ok else f"相机位置发生了变化（anchor 偏差 {worst:.1f} cm），重新计算失败")
        self.warnings.append(msg)
        return DriftCheck(True, worst, new.ok, msg)


__all__ = ["AnchorSpec", "CameraPoseMonitor", "DriftCheck", "PoseEstimate", "anchor_deviation",
           "anchor_specs", "estimate_camera_pose"]
