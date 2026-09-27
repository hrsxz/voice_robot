from __future__ import annotations

from pc.vision.camera_stream import CameraStream
from pc.vision.color_detector import RedBlockDetector
from pc.vision.models import WorldState
from pc.vision.pose_tracker import ArucoPoseTracker


class VisionSystem:
    def __init__(
        self,
        camera_index: int = 0,
        marker_id: int = 0,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.camera = CameraStream(camera_index=camera_index, width=width, height=height)
        self.red_detector = RedBlockDetector()
        self.pose_tracker = ArucoPoseTracker(marker_id=marker_id)

    def open(self) -> None:
        self.camera.open()

    def close(self) -> None:
        self.camera.release()

    def get_world_state(self) -> dict:
        frame = self.camera.read()
        height, width = frame.shape[:2]

        robot = self.pose_tracker.detect(frame)
        red_block = self.red_detector.detect(frame)

        state = WorldState(
            robot=robot,
            red_block=red_block,
            frame_width=width,
            frame_height=height,
        )

        return state.to_dict()

    def get_debug_frame(self):
        frame = self.camera.read()

        robot = self.pose_tracker.detect(frame)
        red_block = self.red_detector.detect(frame)

        self.pose_tracker.draw(frame, robot)
        self.red_detector.draw(frame, red_block)

        return frame, WorldState(
            robot=robot,
            red_block=red_block,
            frame_width=frame.shape[1],
            frame_height=frame.shape[0],
        ).to_dict()