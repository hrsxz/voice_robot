import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from pc.vision.data_types import Point


@dataclass(frozen=True)
class Anchor:
    marker_id: int
    x_cm: float
    y_cm: float


class MapCalibrator:
    def __init__(
        self,
        config_path: str | Path,
        dictionary_name: int = cv2.aruco.DICT_4X4_50,
        min_anchors: int = 4,
    ) -> None:
        self.config_path = Path(config_path)
        self.min_anchors = min_anchors
        self.anchors = self._load_anchors(self.config_path)
        self.homography: np.ndarray | None = None
        self.inverse_homography: np.ndarray | None = None

        self.dictionary = cv2.aruco.getPredefinedDictionary(dictionary_name)
        if hasattr(cv2.aruco, "DetectorParameters"):
            self.parameters = cv2.aruco.DetectorParameters()
        else:
            self.parameters = cv2.aruco.DetectorParameters_create()

        if hasattr(cv2.aruco, "ArucoDetector"):
            self.detector = cv2.aruco.ArucoDetector(self.dictionary, self.parameters)
        else:
            self.detector = None

    def calibrate(self, frame) -> dict:
        detected = self._detect_anchor_pixels(frame)

        image_points: list[list[float]] = []
        map_points: list[list[float]] = []

        for marker_id, pixel_center in detected.items():
            anchor = self.anchors.get(marker_id)
            if anchor is None:
                continue

            image_points.append([pixel_center.x, pixel_center.y])
            map_points.append([anchor.x_cm, anchor.y_cm])

        if len(image_points) < self.min_anchors:
            self.homography = None
            self.inverse_homography = None
            return {
                "status": "error",
                "detail": f"need at least {self.min_anchors} anchors, got {len(image_points)}",
                "detected_anchor_ids": sorted(detected),
            }

        image_array = np.array(image_points, dtype=np.float32)
        map_array = np.array(map_points, dtype=np.float32)

        homography, mask = cv2.findHomography(image_array, map_array, cv2.RANSAC)
        if homography is None:
            self.homography = None
            self.inverse_homography = None
            return {
                "status": "error",
                "detail": "failed to compute homography",
                "detected_anchor_ids": sorted(detected),
            }

        self.homography = homography
        self.inverse_homography = np.linalg.inv(homography)

        error_cm = self._mean_reprojection_error(image_array, map_array)

        return {
            "status": "ok",
            "detected_anchor_ids": sorted(detected),
            "mean_reprojection_error_cm": error_cm,
        }

    def is_ready(self) -> bool:
        return self.homography is not None

    def pixel_to_map(self, point: Point) -> Point:
        if self.homography is None:
            raise RuntimeError("map calibrator is not ready")

        src = np.array([[[point.x, point.y]]], dtype=np.float32)
        dst = cv2.perspectiveTransform(src, self.homography)
        x_cm, y_cm = dst[0][0]
        return Point(float(x_cm), float(y_cm))

    def map_to_pixel(self, point: Point) -> Point:
        if self.inverse_homography is None:
            raise RuntimeError("map calibrator is not ready")

        src = np.array([[[point.x, point.y]]], dtype=np.float32)
        dst = cv2.perspectiveTransform(src, self.inverse_homography)
        x_px, y_px = dst[0][0]
        return Point(float(x_px), float(y_px))

    def transform_corners_to_map(
        self,
        corners: list[tuple[float, float]],
    ) -> list[Point]:
        if self.homography is None:
            raise RuntimeError("map calibrator is not ready")

        src = np.array([[corners]], dtype=np.float32)
        dst = cv2.perspectiveTransform(src, self.homography)
        return [Point(float(x), float(y)) for x, y in dst[0]]

    @staticmethod
    def heading_from_map_corners(corners: list[Point]) -> float:
        if len(corners) < 2:
            raise ValueError("at least two corners are required")

        top_left = corners[0]
        top_right = corners[1]
        dx = top_right.x - top_left.x
        dy = top_right.y - top_left.y
        return math.degrees(math.atan2(dy, dx))

    def draw_anchors(self, frame) -> None:
        detected = self._detect_anchor_pixels(frame)
        for marker_id, center in detected.items():
            cv2.circle(frame, (int(center.x), int(center.y)), 6, (255, 0, 255), -1)
            cv2.putText(
                frame,
                f"anchor {marker_id}",
                (int(center.x) + 8, int(center.y) - 8),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 0, 255),
                2,
            )

    def _detect_anchor_pixels(self, frame) -> dict[int, Point]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        if self.detector is not None:
            corners, ids, _ = self.detector.detectMarkers(gray)
        else:
            corners, ids, _ = cv2.aruco.detectMarkers(
                gray,
                self.dictionary,
                parameters=self.parameters,
            )

        if ids is None:
            return {}

        detected: dict[int, Point] = {}
        for index, marker_id in enumerate(ids.flatten()):
            marker_corners = corners[index][0].astype(float)
            center_x = float(np.mean(marker_corners[:, 0]))
            center_y = float(np.mean(marker_corners[:, 1]))
            detected[int(marker_id)] = Point(center_x, center_y)

        return detected

    def _mean_reprojection_error(
        self,
        image_points: np.ndarray,
        expected_map_points: np.ndarray,
    ) -> float:
        if self.homography is None:
            return float("inf")

        src = image_points.reshape(-1, 1, 2)
        projected = cv2.perspectiveTransform(src, self.homography).reshape(-1, 2)
        errors = np.linalg.norm(projected - expected_map_points, axis=1)
        return float(np.mean(errors))

    @staticmethod
    def _load_anchors(config_path: Path) -> dict[int, Anchor]:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        raw_anchors = data.get("anchors") or {}

        anchors: dict[int, Anchor] = {}
        for marker_id, item in raw_anchors.items():
            marker_id_int = int(marker_id)
            anchors[marker_id_int] = Anchor(
                marker_id=marker_id_int,
                x_cm=float(item["x_cm"]),
                y_cm=float(item["y_cm"]),
            )

        return anchors
