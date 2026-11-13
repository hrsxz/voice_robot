"""
模拟器俯视图 (改动 navigation-grasp 任务 1.3): 场地、小车 (朝向和笼区)、物体、路线、走过的轨迹。

    img = render(sim, route=[...])        # numpy 图像 (BGR)
    save(sim, "logs/sim.png")
    python -m scripts.sim_demo --show       # 运行一局并实时显示
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

from pc.field.geometry import Point, Pose
from pc.sim.simulator import Simulator

BGR = {
    "yellow": (40, 200, 245), "green": (70, 160, 40), "blue": (190, 90, 20), "red": (40, 40, 220),
    "black": (20, 20, 20), "gray": (170, 170, 170), "white": (255, 255, 255),
    "road": (110, 110, 110), "route": (0, 140, 255), "trace": (200, 120, 200),
    "tree": (40, 100, 30), "barrier": (160, 60, 20), "robot": (60, 60, 60),
}
MARGIN = 20


def _pt(p: Point, s: float) -> tuple[int, int]:
    return (round(MARGIN + p[0] * s), round(MARGIN + p[1] * s))


def _poly(img, pts: Sequence[Point], s: float, color, thickness: int = -1) -> None:
    arr = np.array([_pt(p, s) for p in pts], np.int32)
    if thickness < 0:
        cv2.fillPoly(img, [arr], color)
    else:
        cv2.polylines(img, [arr], True, color, thickness, cv2.LINE_AA)


def render(sim: Simulator, route: Sequence[Point] | None = None, scale: float = 5.0,
           title: str = "") -> np.ndarray:
    f = sim.field
    s = scale
    w, h = int(f.width * s) + 2 * MARGIN, int(f.height * s) + 2 * MARGIN + 24
    img = np.full((h, w, 3), 235, np.uint8)
    _poly(img, [(0, 0), (f.width, 0), (f.width, f.height), (0, f.height)], s, BGR["white"])

    for z in f.zones.values():
        _poly(img, z.polygon, s, BGR[z.color])
        cx = sum(p[0] for p in z.polygon) / len(z.polygon)
        cy = sum(p[1] for p in z.polygon) / len(z.polygon)
        cv2.putText(img, z.id, _pt((cx - 2, cy + 2), s), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    BGR["black"], 1, cv2.LINE_AA)
    _poly(img, f.start_polygon, s, BGR["black"], 2)
    for c in f.circles.values():
        cv2.circle(img, _pt(c.center, s), int(c.radius * s), BGR[c.color], -1, cv2.LINE_AA)
        cv2.circle(img, _pt(c.center, s), int(c.radius * s), BGR["black"], 2, cv2.LINE_AA)
    for curve in f.curves.values():
        cv2.polylines(img, [np.array([_pt(p, s) for p in curve], np.int32)], False,
                      BGR["black"], max(1, int(s * 0.8)), cv2.LINE_AA)
    for a, b, _ in f.road_edges:
        cv2.line(img, _pt(f.road_nodes[a], s), _pt(f.road_nodes[b], s), BGR["black"],
                 max(1, int(s * 0.8)), cv2.LINE_AA)
    for p in f.positions.values():
        x0, y0 = _pt((p.x - 2, p.y - 2), s)
        x1, y1 = _pt((p.x + 2, p.y + 2), s)
        cv2.rectangle(img, (x0, y0), (x1, y1), BGR["gray"], 1)
    for k in f.enabled_keepouts():
        if k.polygon is not None:
            _poly(img, k.polygon, s, BGR["red"], 1)
        elif k.center is not None:
            cv2.circle(img, _pt(k.center, s), int(k.radius * s), BGR["red"], 1)

    if len(sim.trace) > 1:
        cv2.polylines(img, [np.array([_pt(p.xy, s) for p in sim.trace], np.int32)], False,
                      BGR["trace"], 1, cv2.LINE_AA)
    if route:
        cv2.polylines(img, [np.array([_pt(p, s) for p in route], np.int32)], False,
                      BGR["route"], 2, cv2.LINE_AA)

    for o in sim.objects.values():
        if o.kind == "barrier" and o.polygon is not None:
            _poly(img, o.polygon, s, BGR["barrier"])
            if o.state != "normal":
                _poly(img, o.polygon, s, BGR["red"], 2)
            continue
        color = BGR["tree"] if o.kind == "tree" else BGR.get(
            sim.field.objects[o.id].color if o.id in sim.field.objects else "gray", BGR["gray"])
        center = _pt(o.xy, s)
        cv2.circle(img, center, max(2, int(o.radius * s)), color, -1, cv2.LINE_AA)
        outline = {"captured": BGR["white"], "moved": BGR["black"], "knocked": BGR["red"]}
        if o.state in outline:
            cv2.circle(img, center, max(2, int(o.radius * s)), outline[o.state], 2, cv2.LINE_AA)
        if o.kind == "object":
            cv2.putText(img, o.id.replace("_person", "P").replace("_robot", "R")
                        .replace("package_", "pkg"),
                        (center[0] + int(o.radius * s) + 2, center[1] + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, BGR["black"], 1, cv2.LINE_AA)

    robot = sim.robot
    assert robot is not None
    _poly(img, sim.geom.body_polygon(robot), s, BGR["robot"])
    if sim.gripper_down:
        _poly(img, sim.geom.footprint_polygon(robot, True), s, BGR["robot"], 2)
        _poly(img, sim.geom.cage_polygon(robot), s, BGR["white"], 1)
    else:
        _poly(img, sim.geom.footprint_polygon(robot, False), s, BGR["robot"], 1)
    tip = Pose(robot.x, robot.y, robot.heading).to_world(sim.geom.body_front, 0)
    cv2.arrowedLine(img, _pt(robot.xy, s), _pt(tip, s), BGR["white"], 2, cv2.LINE_AA,
                    tipLength=0.4)

    caption = title or (f"t={sim.time:5.1f}s  pose=({robot.x:.1f}, {robot.y:.1f}, "
                        f"{robot.heading:.0f}°)  gripper={'down' if sim.gripper_down else 'up'}")
    cv2.putText(img, caption.replace("°", " deg"), (MARGIN, h - 8), cv2.FONT_HERSHEY_SIMPLEX,
                0.5, BGR["black"], 1, cv2.LINE_AA)
    return img


def save(sim: Simulator, path: str | Path, route: Sequence[Point] | None = None,
         scale: float = 5.0, title: str = "") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(p), render(sim, route, scale, title))
    return p


class LiveView:
    """运行时实时显示 (OpenCV 窗口)。按 q 关闭窗口, 程序继续运行。"""

    def __init__(self, sim: Simulator, scale: float = 5.0, every_s: float = 0.2) -> None:
        self.sim = sim
        self.scale = scale
        self.every_s = every_s
        self._last = -1.0
        self.route: Sequence[Point] | None = None
        self.enabled = True

    def update(self, force: bool = False) -> None:
        if not self.enabled or (not force and self.sim.time - self._last < self.every_s):
            return
        self._last = self.sim.time
        cv2.imshow("voice_robot simulator", render(self.sim, self.route, self.scale))
        if cv2.waitKey(1) & 0xFF == ord("q"):
            self.enabled = False
            cv2.destroyAllWindows()


__all__ = ["LiveView", "render", "save"]
