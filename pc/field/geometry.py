"""
场地坐标系里的二维几何 (单位厘米)。

坐标系: x 向右, y 向下; 朝向 heading 用度, 0° 指向 +x, 顺时针为正 (90° 指向 +y)。
车身坐标系: x 指向车头, y 指向车的右侧 (把车头方向顺时针转 90°)。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Point = tuple[float, float]
Polygon = Sequence[Point]


@dataclass(frozen=True)
class Pose:
    """小车位姿: 转弯中心 (两个驱动轮中点) 的坐标和车头朝向。"""

    x: float
    y: float
    heading: float  # 度, 0 指向 +x, 顺时针为正

    @property
    def xy(self) -> Point:
        return (self.x, self.y)

    def to_world(self, lx: float, ly: float) -> Point:
        """车身坐标 (前, 右) -> 场地坐标。"""
        c, s = math.cos(math.radians(self.heading)), math.sin(math.radians(self.heading))
        return (self.x + lx * c - ly * s, self.y + lx * s + ly * c)

    def to_local(self, p: Point) -> Point:
        """场地坐标 -> 车身坐标 (前, 右)。"""
        c, s = math.cos(math.radians(self.heading)), math.sin(math.radians(self.heading))
        dx, dy = p[0] - self.x, p[1] - self.y
        return (dx * c + dy * s, -dx * s + dy * c)

    def moved(self, distance: float) -> Pose:
        """沿车头方向移动 distance (负数为后退)。"""
        hx, hy = heading_vector(self.heading)
        return Pose(self.x + hx * distance, self.y + hy * distance, self.heading)

    def turned(self, delta: float) -> Pose:
        return Pose(self.x, self.y, normalize_heading(self.heading + delta))


def heading_vector(heading: float) -> Point:
    rad = math.radians(heading)
    return (math.cos(rad), math.sin(rad))


def normalize_heading(h: float) -> float:
    return h % 360.0


def heading_diff(target: float, current: float) -> float:
    """从 current 转到 target 的最小角度, 范围 (-180, 180]; 正数 = 顺时针 (右转)。"""
    d = (target - current) % 360.0
    return d - 360.0 if d > 180.0 else d


def heading_to(a: Point, b: Point) -> float:
    return normalize_heading(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])))


def dist(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def point_in_polygon(p: Point, poly: Polygon) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def segment_point_distance(a: Point, b: Point, p: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return dist(a, p)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2))
    return dist((ax + t * dx, ay + t * dy), p)


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    eps = 1e-9
    return (
        (abs(d1) < eps and segment_point_distance(c, d, a) < eps)
        or (abs(d2) < eps and segment_point_distance(c, d, b) < eps)
        or (abs(d3) < eps and segment_point_distance(a, b, c) < eps)
        or (abs(d4) < eps and segment_point_distance(a, b, d) < eps)
    )


def segment_segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        segment_point_distance(a, b, c), segment_point_distance(a, b, d),
        segment_point_distance(c, d, a), segment_point_distance(c, d, b),
    )


def polygon_edges(poly: Polygon) -> list[tuple[Point, Point]]:
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def polygon_point_distance(poly: Polygon, p: Point) -> float:
    """点到多边形的距离 (点在多边形内为 0)。"""
    if point_in_polygon(p, poly):
        return 0.0
    return min(segment_point_distance(a, b, p) for a, b in polygon_edges(poly))


def segment_polygon_distance(a: Point, b: Point, poly: Polygon) -> float:
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return 0.0
    return min(segment_segment_distance(a, b, c, d) for c, d in polygon_edges(poly))


def polygon_circle_overlap(poly: Polygon, center: Point, radius: float) -> bool:
    """多边形和圆有没有重叠 (部分接触也算)。"""
    return polygon_point_distance(poly, center) <= radius


def polygons_distance(p: Polygon, q: Polygon) -> float:
    """两个多边形之间的距离 (相交或包含时为 0)。"""
    if any(point_in_polygon(v, q) for v in p) or any(point_in_polygon(v, p) for v in q):
        return 0.0
    return min(
        segment_segment_distance(a, b, c, d)
        for a, b in polygon_edges(p) for c, d in polygon_edges(q)
    )


def polygon_outside_rect(poly: Polygon, width: float, height: float, margin: float = 0.0) -> float:
    """多边形超出矩形 [margin, width-margin] × [margin, height-margin] 的最大距离 (0 = 没超出)。"""
    worst = 0.0
    for x, y in poly:
        worst = max(worst, margin - x, x - (width - margin), margin - y, y - (height - margin))
    return worst


def rect_polygon(pose: Pose, x0: float, x1: float, y0: float, y1: float) -> list[Point]:
    """车身坐标系里的矩形 [x0, x1] × [y0, y1] 在场地坐标系里的四个角。"""
    return [pose.to_world(x0, y0), pose.to_world(x1, y0),
            pose.to_world(x1, y1), pose.to_world(x0, y1)]


@dataclass(frozen=True)
class Shape:
    """障碍物的形状: 圆 (center + radius) 或多边形。"""

    id: str
    center: Point | None = None
    radius: float = 0.0
    polygon: tuple[Point, ...] | None = None

    def distance_to_polygon(self, poly: Polygon) -> float:
        if self.polygon is not None:
            return polygons_distance(self.polygon, poly)
        assert self.center is not None
        return max(0.0, polygon_point_distance(poly, self.center) - self.radius)

    def distance_to_segment(self, a: Point, b: Point) -> float:
        if self.polygon is not None:
            return segment_polygon_distance(a, b, self.polygon)
        assert self.center is not None
        return max(0.0, segment_point_distance(a, b, self.center) - self.radius)

    def distance_to_point(self, p: Point) -> float:
        if self.polygon is not None:
            return polygon_point_distance(self.polygon, p)
        assert self.center is not None
        return max(0.0, dist(self.center, p) - self.radius)


def polygon_centroid(poly: Polygon) -> Point:
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))
