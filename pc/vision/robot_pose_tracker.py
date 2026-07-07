from __future__ import annotations

import math

import cv2
import numpy as np

from pc.vision.models import Point, RobotPose


class ArucoPoseTracker:
    def __init__(
        self,
        marker_id: int = 0,
        dictionary_name: int = cv2.aruco.DICT_4X4_50,
    ) -> None:
        self.marker_id = marker_id
        self.dictionary = cv2.aruco.getPredefinedDictionary(dictionary_name)

        if hasattr(cv2.aruco, "DetectorParameters"):
            self.parameters = cv2.aruco.DetectorParameters()
        else:
            self.parameters = cv2.aruco.DetectorParameters_create()

        if hasattr(cv2.aruco, "ArucoDetector"):
            self.detector = cv2.aruco.ArucoDetector(self.dictionary, self.parameters)
        else:
            self.detector = None

    def detect(self, frame) -> RobotPose:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self.detector is not None:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(
                gray,
                self.dictionary,
                parameters=self.parameters,
            )

        if ids is None or len(ids) == 0:
            return RobotPose(found=False)

        flat_ids = ids.flatten()
        for index, detected_id in enumerate(flat_ids):
            if int(detected_id) != self.marker_id:
                continue

            marker_corners = corners[index][0].astype(float)
            center_x = float(np.mean(marker_corners[:, 0]))
            center_y = float(np.mean(marker_corners[:, 1]))

            top_left = marker_corners[0]
            top_right = marker_corners[1]
            dx = float(top_right[0] - top_left[0])
            dy = float(top_right[1] - top_left[1])

            heading_deg = math.degrees(math.atan2(dy, dx))

            return RobotPose(
                found=True,
                marker_id=int(detected_id),
                center=Point(center_x, center_y),
                heading_deg=heading_deg,
                corners=[(float(x), float(y)) for x, y in marker_corners],
            )

        return RobotPose(found=False)

    def draw(self, frame, pose: RobotPose) -> None:
        if not pose.found or not pose.center or not pose.corners:
            return

        points = np.array(pose.corners, dtype=np.int32)
        cv2.polylines(frame, [points], isClosed=True, color=(0, 255, 0), thickness=2)

        center = (int(pose.center.x), int(pose.center.y))
        cv2.circle(frame, center, 5, (0, 255, 0), -1)

        if pose.heading_deg is not None:
            length = 60
            angle_rad = math.radians(pose.heading_deg)
            end = (
                int(pose.center.x + math.cos(angle_rad) * length),
                int(pose.center.y + math.sin(angle_rad) * length),
            )
            cv2.arrowedLine(frame, center, end, (0, 255, 0), 2)

        label = f"robot id={pose.marker_id} heading={pose.heading_deg:.1f}"
        cv2.putText(
            frame,
            label,
            (center[0] + 8, center[1] - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
        )