"""
调试画面 (改动 vision-field-model 任务 6.1): 在摄像头画面上叠加场地元素、anchor、
小车位姿和朝向、笼区中心、每个物体的编号和状态。

    img = draw_debug(frame, model, field, reading, objects)
    python -m pc.vision.vision_demo --camera --show     # 实时调试画面 (README 第 15 节)
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence

import cv2
import numpy as np

from pc.field.geometry import Point
from pc.field.model import FieldModel
from pc.vision.camera_model import CameraModel
from pc.vision.localizer import LocalizerReading
from pc.vision.tracker import TrackedObject

ZONE_BGR = {"yellow": (0, 220, 255), "green": (0, 200, 0), "blue": (255, 120, 0),
            "red": (0, 0, 255)}


def _poly(model: CameraModel, pts: Sequence[Point], closed: bool = True,
          height: float = 0.0, step: float = 2.0) -> np.ndarray:
    """沿多边形每 step 厘米取一个点再投影 (画面有畸变, 直线会弯)。"""
    pts = list(pts) + ([pts[0]] if closed else [])
    dense: list[Point] = []
    for a, b in itertools.pairwise(pts):
        n = max(1, int(np.hypot(b[0] - a[0], b[1] - a[1]) / step))
        dense += [(a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n) for t in range(n)]
    dense.append(pts[-1])
    arr = np.array(dense)
    px = model.project(np.c_[arr, np.full(len(arr), height)])
    return np.round(px).astype(np.int32)


def _circle(c: Point, r: float, n: int = 36) -> list[Point]:
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return [(c[0] + r * np.cos(t), c[1] + r * np.sin(t)) for t in a]


def draw_debug(frame: np.ndarray, model: CameraModel, fm: FieldModel,
               reading: LocalizerReading | None = None,
               objects: Iterable[TrackedObject] = (), marker_height: float = 10.0,
               anchors: dict[int, np.ndarray] | None = None, lines: Sequence[str] = ()
               ) -> np.ndarray:
    img = frame.copy()
    border = [(0, 0), (fm.width, 0), (fm.width, fm.height), (0, fm.height)]
    cv2.polylines(img, [_poly(model, border)], False, (255, 255, 255), 2, cv2.LINE_AA)
    for z in fm.zones.values():
        cv2.polylines(img, [_poly(model, z.polygon)], False, ZONE_BGR[z.color], 2, cv2.LINE_AA)
        c = np.mean(np.array(z.polygon), axis=0)
        p = model.project([tuple(c)])[0]
        cv2.putText(img, z.id, (int(p[0]) - 8, int(p[1]) + 8), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (0, 0, 0), 2, cv2.LINE_AA)
    for c in fm.circles.values():
        cv2.polylines(img, [_poly(model, _circle(c.center, c.radius))], False,
                      ZONE_BGR[c.color], 2, cv2.LINE_AA)
    for mid, corners in (anchors or {}).items():
        pts = np.round(corners).astype(np.int32)
        cv2.polylines(img, [pts], True, (255, 0, 255), 2, cv2.LINE_AA)
        org = (int(pts[0][0]), int(pts[0][1]) - 8)
        label = f"anchor {mid}" if mid in fm.anchors else f"marker {mid}"
        cv2.putText(img, label, org, cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (255, 0, 255), 2, cv2.LINE_AA)

    if reading is not None and reading.found and reading.pose is not None:
        pose = reading.pose
        base = model.project([(pose.x, pose.y, marker_height)])[0]
        tip = model.project([(*pose.to_world(15.0, 0.0), marker_height)])[0]
        cv2.arrowedLine(img, tuple(np.round(base).astype(int)), tuple(np.round(tip).astype(int)),
                        (0, 255, 0), 3, cv2.LINE_AA, tipLength=0.3)
        if reading.cage_center is not None:
            cc = model.project([reading.cage_center])[0]
            cv2.drawMarker(img, tuple(np.round(cc).astype(int)), (0, 255, 255),
                           cv2.MARKER_CROSS, 16, 2)
        org = (round(base[0]) + 12, round(base[1]) - 12)
        cv2.putText(img, f"robot ({pose.x:.1f}, {pose.y:.1f}) {pose.heading:.0f} deg",
                    org, cv2.FONT_HERSHEY_SIMPLEX,
                    0.7, (0, 255, 0), 2, cv2.LINE_AA)
    elif reading is not None:
        age = f"{reading.age_s:.1f}s ago" if reading.age_s is not None else "never"
        lines = [*lines, f"robot NOT VISIBLE (last seen {age})"]

    for o in objects:
        p = model.project([(o.x, o.y, o.height)])[0]
        state = "captured" if o.captured else ("" if o.visible else "occluded")
        color = (0, 255, 0) if o.visible else (0, 165, 255)
        q = tuple(np.round(p).astype(int))
        cv2.circle(img, q, 6, color, 2, cv2.LINE_AA)
        cv2.putText(img, f"{o.id} {state}".strip(), (q[0] + 8, q[1] - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
    for i, text in enumerate(lines):
        cv2.putText(img, text, (20, 40 + 32 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2,
                    cv2.LINE_AA)
    return img


__all__ = ["draw_debug"]
