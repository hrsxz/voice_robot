"""
感知系统 (改动 vision-field-model): 把相机位姿、小车定位、俯视图、物体检测和跟踪串起来。

    vs = VisionSystem(field, intrinsics, starts=layout.objects)
    r = vs.start(frame)                    # 4 个 anchor -> 相机位姿 (缺 anchor 时 r.ok = False)
    vs.set_reference(empty_frame)          # 空场地参考图 (或者 vs.load_reference(path))
    init = vs.begin_tracking(frame)        # 开局: 按起始布局确定每个物体的身份
    out = vs.process(frame, holding="yellow_person")   # 之后每一帧
    VisionPoseSource(vs.localizer); VisionObjectSource(vs.tracker)   # 交给导航层
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np

from pc.field.geometry import Point
from pc.field.model import FieldModel
from pc.field.robot_geometry import RobotGeometry
from pc.vision.camera_model import Intrinsics
from pc.vision.config import VisionConfig
from pc.vision.extrinsics import CameraPoseMonitor, PoseEstimate, anchor_specs
from pc.vision.localizer import LocalizerReading, RobotLocalizer
from pc.vision.markers import MarkerDetector
from pc.vision.objects import ObjectDetection, ObjectDetector, robot_mask
from pc.vision.topview import Reference, TopView, check_lighting
from pc.vision.tracker import InitResult, ObjectTracker


@dataclass
class FrameResult:
    timestamp: float
    robot: LocalizerReading | None
    detections: list[ObjectDetection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    markers: dict[int, np.ndarray] = field(default_factory=dict)


class VisionNotReady(RuntimeError):
    pass


class VisionSystem:
    def __init__(self, fm: FieldModel, intr: Intrinsics, config: VisionConfig | None = None,
                 geom: RobotGeometry | None = None, starts: dict[str, Point] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.field = fm
        self.intr = intr
        self.config = config or VisionConfig.load()
        self.geom = geom or RobotGeometry.load()
        self.clock = clock
        self.markers = MarkerDetector(self.config.aruco_dictionary)
        a = self.config.anchors
        self.monitor = CameraPoseMonitor(anchor_specs(fm, a), intr, a.recheck_s, a.drift_cm)
        self.starts = dict(starts or {})
        self.view: TopView | None = None
        self.localizer: RobotLocalizer | None = None
        self.detector: ObjectDetector | None = None
        self.reference: Reference | None = None
        self.tracker = ObjectTracker(fm, self.starts, self.config.start_gate_cm,
                                     self.config.track_gate_cm)
        self._lighting_warned = False

    # ---------------- 启动 ----------------

    def start(self, frame: np.ndarray, now: float | None = None) -> PoseEstimate:
        """用 4 个 anchor 计算相机位姿。失败时 (例如缺 anchor) 不输出任何场地坐标。"""
        h, w = frame.shape[:2]
        self.intr.check_size(w, h)
        est = self.monitor.start(self.markers.detect(frame), self._now(now))
        if est.ok:
            self._apply_model()
        return est

    def _apply_model(self) -> None:
        model = self.monitor.model
        assert model is not None
        self.view = TopView(model, self.field, self.config.px_per_cm, self.config.margin_cm)
        if self.localizer is None:
            self.localizer = RobotLocalizer(model, self.config.marker, self.geom, self.clock)
        else:
            self.localizer.set_model(model)
        if self.reference is not None:
            self._make_detector()

    @property
    def ready(self) -> bool:
        return self.monitor.model is not None

    def set_reference(self, empty_frame: np.ndarray) -> Reference:
        """场地上没有小车和物体时调用: 拍参考图。"""
        view = self._view()
        self.reference = Reference.capture(view.rectify(empty_frame), view)
        self._make_detector()
        return self.reference

    def load_reference(self, path) -> str | None:
        """读取保存的参考图; 不能用时返回原因 (分辨率不同、相机移动过)。"""
        ref = Reference.load(path)
        problem = ref.check_compatible(self._view())
        if problem is None:
            self.reference = ref
            self._make_detector()
        return problem

    def _make_detector(self) -> None:
        assert self.reference is not None and self.view is not None
        self.detector = ObjectDetector(self.view, self.reference.image, self.config.detection,
                                       self.geom, self.config.marker.height_cm)

    def begin_tracking(self, frame: np.ndarray, now: float | None = None) -> InitResult:
        """开局: 检测物体, 按起始布局确定身份, 报告缺少和多出的物体。"""
        out = self.process(frame, now, track=False)
        return self.tracker.initialize(out.detections, out.timestamp)

    # ---------------- 每一帧 ----------------

    def process(self, frame: np.ndarray, now: float | None = None, holding: str | None = None,
                track: bool = True) -> FrameResult:
        if not self.ready:
            raise VisionNotReady("相机位姿还没有计算（先调用 start，4 个 anchor 都要在画面里）")
        ts = self._now(now)
        markers = self.markers.detect(frame)
        warnings: list[str] = []
        drift = self.monitor.maybe_check(markers, ts)
        if drift.warning:
            warnings.append(drift.warning)
            if drift.recalibrated:
                self._apply_model()
                if self.reference is not None:
                    problem = self.reference.check_compatible(self._view())
                    if problem:
                        warnings.append(problem)
        assert self.localizer is not None
        reading = self.localizer.update(markers, ts)
        result = FrameResult(ts, reading, warnings=warnings, markers=markers)
        if self.detector is None:
            return result
        top = self._view().rectify(frame)
        pose = reading.pose
        result.detections = self.detector.detect(top, pose)
        exclude = self._busy_mask(pose, result.detections)
        light = check_lighting(self.detector.reference, top, exclude,
                               self.config.lighting_max_change)
        if light.warn and not self._lighting_warned:
            warnings.append(light.message)
        self._lighting_warned = light.warn
        if track and self.tracker.objects:
            self.tracker.update(result.detections, ts, holding, reading.cage_center)
        return result

    def _busy_mask(self, pose, detections: list[ObjectDetection]) -> np.ndarray:
        """光照检查时不参与比较的区域: 小车和物体附近。"""
        view = self._view()
        mask = np.zeros((view.height, view.width), np.uint8)
        if pose is not None:
            mask |= robot_mask(view, pose, self.geom, self.config.marker.height_cm, 8.0)
        for d in detections:
            c = np.round(view.to_px([d.ground])[0]).astype(int)
            cv2.circle(mask, (int(c[0]), int(c[1])), int(12 * view.ppc), 255, -1)
        return mask

    def _view(self) -> TopView:
        if self.view is None:
            raise VisionNotReady("相机位姿还没有计算")
        return self.view

    def _now(self, now: float | None) -> float:
        return self.clock() if now is None else now


__all__ = ["FrameResult", "VisionNotReady", "VisionSystem"]
