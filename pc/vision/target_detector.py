import cv2
import numpy as np

from pc.vision.data_types import Detection, Point


class RedBlockDetector:
    def __init__(self, min_area: int = 500) -> None:
        self.min_area = min_area

    def detect(self, frame) -> Detection:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

        lower_red_1 = np.array([0, 80, 60])
        upper_red_1 = np.array([10, 255, 255])
        lower_red_2 = np.array([170, 80, 60])
        upper_red_2 = np.array([180, 255, 255])

        mask_1 = cv2.inRange(hsv, lower_red_1, upper_red_1)
        mask_2 = cv2.inRange(hsv, lower_red_2, upper_red_2)
        mask = cv2.bitwise_or(mask_1, mask_2)

        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return Detection(found=False)

        contour = max(contours, key=cv2.contourArea)
        area = float(cv2.contourArea(contour))
        if area < self.min_area:
            return Detection(found=False, area=area)

        x, y, width, height = cv2.boundingRect(contour)
        center = Point(x=x + width / 2, y=y + height / 2)

        return Detection(
            found=True,
            center=center,
            area=area,
            bbox=(x, y, width, height),
        )

    def draw(self, frame, detection: Detection) -> None:
        if not detection.found or detection.center is None or detection.bbox is None:
            return

        x, y, width, height = detection.bbox
        cv2.rectangle(frame, (x, y), (x + width, y + height), (0, 0, 255), 2)
        cv2.circle(frame, (int(detection.center.x), int(detection.center.y)), 5, (0, 0, 255), -1)
        cv2.putText(
            frame,
            "red_block",
            (x, max(20, y - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 255),
            2,
        )