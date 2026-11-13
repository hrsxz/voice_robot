"""
摄像头采集 (改动 vision-field-model 设计 D2, 任务 1.2)。

Windows 下用 DirectShow: 设置 1920×1080 和 MJPG (保证帧率), 尝试关闭自动对焦、自动曝光、
自动白平衡, 然后读回属性确认; 设置不成功时 open() 返回警告 (不中断运行)。

    cam = CameraStream.from_config(VisionConfig.load().camera)
    for w in cam.open(): print("警告：", w)
    frame = cam.read()
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import cv2

from pc.vision.config import CameraConfig

# DirectShow 的 CAP_PROP_AUTO_EXPOSURE: 0.25 = 手动, 0.75 = 自动 (V4L2 的约定, 多数驱动接受)
MANUAL_EXPOSURE = 0.25


def _fourcc_str(value: float) -> str:
    v = int(value)
    return "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))


class CameraStream:
    def __init__(self, camera_index: int = 0, width: int = 1920, height: int = 1080,
                 config: CameraConfig | None = None,
                 capture_factory: Callable[..., Any] | None = None) -> None:
        self.config = config or CameraConfig(index=camera_index, width=width, height=height)
        self.capture_factory = capture_factory or cv2.VideoCapture
        self.capture: Any = None
        self.warnings: list[str] = []

    @classmethod
    def from_config(cls, config: CameraConfig, **kwargs: Any) -> CameraStream:
        return cls(config=config, **kwargs)

    @property
    def width(self) -> int:
        return self.config.width

    @property
    def height(self) -> int:
        return self.config.height

    def open(self) -> list[str]:
        """打开摄像头, 返回警告列表 (空 = 全部设置成功)。打不开时抛出 RuntimeError。"""
        if self.capture is not None and self.capture.isOpened():
            return self.warnings
        c = self.config
        if c.backend == "dshow" and sys.platform == "win32":
            cap = self.capture_factory(c.index, cv2.CAP_DSHOW)
        else:
            cap = self.capture_factory(c.index)
        if not cap.isOpened():
            raise RuntimeError(f"打不开摄像头 {c.index}")
        self.capture = cap
        warnings: list[str] = []

        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*c.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, c.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c.height)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if (w, h) != (c.width, c.height):
            warnings.append(f"分辨率设置为 {c.width}×{c.height} 失败，实际 {w}×{h}")
        fourcc = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
        if fourcc.strip("\x00") and fourcc != c.fourcc:
            warnings.append(f"无法使用 {c.fourcc} 格式（实际 {fourcc!r}），帧率可能偏低")

        if not c.autofocus:
            ok = cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            if c.focus is not None:
                ok = cap.set(cv2.CAP_PROP_FOCUS, float(c.focus)) and ok
            if not ok or cap.get(cv2.CAP_PROP_AUTOFOCUS) != 0:
                warnings.append("无法锁定对焦，标定可能失效")
        if not c.auto_exposure:
            ok = cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, MANUAL_EXPOSURE)
            if c.exposure is not None:
                ok = cap.set(cv2.CAP_PROP_EXPOSURE, float(c.exposure)) and ok
            if not ok or cap.get(cv2.CAP_PROP_AUTO_EXPOSURE) not in (MANUAL_EXPOSURE, 0, 1):
                warnings.append("无法关闭自动曝光，光照变化时物体检测可能不稳定")
        if not c.auto_white_balance:
            ok = cap.set(cv2.CAP_PROP_AUTO_WB, 0)
            if not ok or cap.get(cv2.CAP_PROP_AUTO_WB) != 0:
                warnings.append("无法关闭自动白平衡，颜色判断可能不稳定")
        self.warnings = warnings
        return warnings

    def read(self):
        if self.capture is None or not self.capture.isOpened():
            self.open()
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("读取摄像头画面失败")
        return frame

    def release(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None


__all__ = ["CameraStream"]
