"""
感知参数 (pc/config/vision.yaml) 的读取。所有感知模块都从这里拿参数, 换摄像头或小车只改 YAML。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pc import constants

CONFIG_PATH = constants.project_root_path / "pc" / "config" / "vision.yaml"


@dataclass(frozen=True)
class CameraConfig:
    index: int = 0
    width: int = 1920
    height: int = 1080
    fourcc: str = "MJPG"
    backend: str = "dshow"
    autofocus: bool = False
    auto_exposure: bool = False
    auto_white_balance: bool = False
    focus: float | None = None
    exposure: float | None = None


@dataclass(frozen=True)
class AnchorConfig:
    size_cm: float = 8.0
    rotation_deg: float = 0.0
    recheck_s: float = 5.0
    drift_cm: float = 1.0


@dataclass(frozen=True)
class MarkerConfig:
    id: int = 20
    size_cm: float = 9.0
    height_cm: float = 10.0
    offset_cm: tuple[float, float] = (0.0, 0.0)  # (前, 右), 相对转弯中心
    rotation_deg: float = 0.0


@dataclass(frozen=True)
class DetectionConfig:
    diff_threshold: float = 22.0
    min_area_cm2: float = 8.0
    max_area_cm2: float = 400.0
    min_saturation: int = 70
    robot_margin_cm: float = 3.0


@dataclass(frozen=True)
class VisionConfig:
    camera: CameraConfig = field(default_factory=CameraConfig)
    local_dir: Path = constants.project_root_path / "pc" / "config" / "local"
    intrinsics_file: str = "camera_intrinsics.yaml"
    reference_file: str = "reference_topview.png"
    aruco_dictionary: str = "DICT_4X4_50"
    anchors: AnchorConfig = field(default_factory=AnchorConfig)
    marker: MarkerConfig = field(default_factory=MarkerConfig)
    px_per_cm: float = 4.0
    margin_cm: float = 0.0
    lighting_max_change: float = 0.25
    detection: DetectionConfig = field(default_factory=DetectionConfig)
    start_gate_cm: float = 12.0
    track_gate_cm: float = 30.0

    @property
    def intrinsics_path(self) -> Path:
        return self.local_dir / self.intrinsics_file

    @property
    def reference_path(self) -> Path:
        return self.local_dir / self.reference_file

    @classmethod
    def load(cls, path: str | Path | None = None) -> VisionConfig:
        p = Path(path) if path else CONFIG_PATH
        data: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cam = data.get("camera") or {}
        lock = cam.get("lock") or {}
        anchors = data.get("anchors") or {}
        marker = data.get("robot_marker") or {}
        top = data.get("topview") or {}
        det = data.get("detection") or {}
        trk = data.get("tracking") or {}
        local = data.get("local_dir")
        return cls(
            camera=CameraConfig(
                index=int(cam.get("index", 0)), width=int(cam.get("width", 1920)),
                height=int(cam.get("height", 1080)), fourcc=str(cam.get("fourcc", "MJPG")),
                backend=str(cam.get("backend", "dshow")),
                autofocus=bool(lock.get("autofocus", False)),
                auto_exposure=bool(lock.get("auto_exposure", False)),
                auto_white_balance=bool(lock.get("auto_white_balance", False)),
                focus=cam.get("focus"), exposure=cam.get("exposure"),
            ),
            local_dir=(constants.project_root_path / local) if local else
            constants.project_root_path / "pc" / "config" / "local",
            intrinsics_file=str(data.get("intrinsics_file", "camera_intrinsics.yaml")),
            reference_file=str(data.get("reference_file", "reference_topview.png")),
            aruco_dictionary=str((data.get("aruco") or {}).get("dictionary", "DICT_4X4_50")),
            anchors=AnchorConfig(
                size_cm=float(anchors.get("size_cm", 8.0)),
                rotation_deg=float(anchors.get("rotation_deg", 0.0)),
                recheck_s=float(anchors.get("recheck_s", 5.0)),
                drift_cm=float(anchors.get("drift_cm", 1.0)),
            ),
            marker=MarkerConfig(
                id=int(marker.get("id", 20)), size_cm=float(marker.get("size_cm", 9.0)),
                height_cm=float(marker.get("height_cm", 10.0)),
                offset_cm=tuple(float(v) for v in marker.get("offset_cm", (0.0, 0.0))),
                rotation_deg=float(marker.get("rotation_deg", 0.0)),
            ),
            px_per_cm=float(top.get("px_per_cm", 4.0)),
            margin_cm=float(top.get("margin_cm", 0.0)),
            lighting_max_change=float((data.get("lighting") or {}).get("max_change", 0.25)),
            detection=DetectionConfig(
                diff_threshold=float(det.get("diff_threshold", 22.0)),
                min_area_cm2=float(det.get("min_area_cm2", 8.0)),
                max_area_cm2=float(det.get("max_area_cm2", 400.0)),
                min_saturation=int(det.get("min_saturation", 70)),
                robot_margin_cm=float(det.get("robot_margin_cm", 3.0)),
            ),
            start_gate_cm=float(trk.get("start_gate_cm", 12.0)),
            track_gate_cm=float(trk.get("track_gate_cm", 30.0)),
        )


__all__ = ["CONFIG_PATH", "AnchorConfig", "CameraConfig", "DetectionConfig", "MarkerConfig",
           "VisionConfig"]
