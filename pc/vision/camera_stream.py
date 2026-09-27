from __future__ import annotations

import cv2


class CameraStream:
    def __init__(self, camera_index: int = 0, width: int = 1280, height: int = 720) -> None:
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.capture: cv2.VideoCapture | None = None

    def open(self) -> None:
        if self.capture is not None and self.capture.isOpened():
            return

        capture = cv2.VideoCapture(self.camera_index)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        if not capture.isOpened():
            raise RuntimeError(f"camera {self.camera_index} is not available")

        self.capture = capture

    def read(self):
        if self.capture is None or not self.capture.isOpened():
            self.open()

        assert self.capture is not None
        ok, frame = self.capture.read()
        if not ok or frame is None:
            raise RuntimeError("failed to read camera frame")

        return frame

    def release(self) -> None:
        if self.capture is not None:
            self.capture.release()
            self.capture = None
