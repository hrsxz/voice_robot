"""
布局: 这一局的物体放在哪里 (改动 vision-field-model 设计 D6 的实现调整)。

物体可以随意摆: 布局文件里每个物体引用一个命名位置点, 或者直接写坐标;
模拟器还可以用随机种子生成合法的随机布局 (navigation-grasp 的 motion-simulator 要求)。
摄像头到位后, 起点直接来自开局检测, 不需要布局文件。

    layout = load_layout("starter2022", field)
    layout = random_layout(field, seed=1)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pc.field.approach import choose_approach, keepout_shapes
from pc.field.geometry import Point, Shape, dist, polygon_point_distance
from pc.field.model import DATA_DIR, FieldModel, FieldModelError, read_yaml
from pc.field.robot_geometry import RobotGeometry

LAYOUT_DIR = DATA_DIR / "layouts"
OBJECT_GAP = 1.0  # 两个物体之间至少留多少空隙
RANDOM_GAP = 6.0  # 随机布局里物体之间、物体和障碍之间至少留多少空隙
RANDOM_EDGE = 6.0  # 随机布局里物体离场地边界至少多远
START_CLEAR = 25.0  # 随机布局里物体离起终点区出口 (停车位置和进入点) 至少多远


@dataclass(frozen=True)
class Obstacle:
    """不能碰的东西 (树、障碍杆)。"""

    id: str
    type: str  # tree / barrier
    shape: Shape


@dataclass(frozen=True)
class Layout:
    name: str
    objects: dict[str, Point]  # 物体 -> 起点坐标
    positions: dict[str, str | None]  # 物体 -> 引用的位置点 (直接给坐标时为 None)
    obstacles: dict[str, Obstacle]

    def obstacle_shapes(self) -> list[Shape]:
        return [o.shape for o in self.obstacles.values()]


def load_layout(name_or_path: str | Path, field: FieldModel) -> Layout:
    p = Path(name_or_path)
    if not p.suffix:
        p = LAYOUT_DIR / f"{name_or_path}.yaml"
    data = read_yaml(p)
    return parse_layout(data, field, source=p.name)


def parse_layout(data: dict[str, Any], field: FieldModel, source: str = "") -> Layout:
    errors: list[str] = []
    objects: dict[str, Point] = {}
    positions: dict[str, str | None] = {}
    for oid_key, ref in (data.get("objects") or {}).items():
        oid = str(oid_key)
        if oid not in field.objects:
            errors.append(f"物体 {oid} 不在物体目录里")
            continue
        point = _resolve(ref, field, f"物体 {oid}", errors)
        if point is None:
            continue
        objects[oid] = point
        positions[oid] = ref if isinstance(ref, str) else None

    obstacles: dict[str, Obstacle] = {}
    for bid_key, b in (data.get("obstacles") or {}).items():
        bid = str(bid_key)
        btype = str(b.get("type", "tree"))
        if "polygon" in b:
            poly = tuple((float(x), float(y)) for x, y in b["polygon"])
            obstacles[bid] = Obstacle(bid, btype, Shape(bid, polygon=poly))
            continue
        point = _resolve(b.get("at"), field, f"障碍 {bid}", errors)
        if point is not None:
            radius = float(b.get("radius_cm", 3.5))
            obstacles[bid] = Obstacle(bid, btype, Shape(bid, center=point, radius=radius))

    ids = sorted(objects)
    for i, a in enumerate(ids):
        ra = field.objects[a].radius_cm
        for b in ids[i + 1:]:
            if dist(objects[a], objects[b]) < ra + field.objects[b].radius_cm + OBJECT_GAP:
                errors.append(f"物体 {a} 和 {b} 重叠")
        for ob in obstacles.values():
            if ob.shape.distance_to_point(objects[a]) < ra:
                errors.append(f"物体 {a} 和障碍 {ob.id} 重叠")
    if errors:
        raise FieldModelError(errors, source)
    return Layout(str(data.get("name") or source), objects, positions, obstacles)


def _resolve(ref: Any, field: FieldModel, what: str, errors: list[str]) -> Point | None:
    if isinstance(ref, str):
        if ref not in field.positions:
            errors.append(f"{what} 引用了不存在的位置 {ref}")
            return None
        return field.positions[ref].xy
    try:
        point = (float(ref[0]), float(ref[1]))
    except (TypeError, ValueError, IndexError):
        errors.append(f"{what} 的位置必须是位置点名称或 [x, y]")
        return None
    if not field.inside(point, tolerance=0.0):
        errors.append(f"{what} 在场地外：({point[0]:g}, {point[1]:g})")
        return None
    return point


def random_layout(
    field: FieldModel, seed: int, object_ids: list[str] | None = None,
    obstacles_from: str | None = "starter2022", geom: RobotGeometry | None = None,
    max_tries: int = 200,
) -> Layout:
    """
    随机摆放物体: 都在场地里, 互不重叠, 不压在区域、圆圈和起终点区上, 并且每个物体都能接近。
    同一个种子得到同一个布局。树和障碍杆默认按 starter2022 的标准位置摆。
    """
    geom = geom or RobotGeometry.load()
    rng = random.Random(seed)
    obstacles = load_layout(obstacles_from, field).obstacles if obstacles_from else {}
    ids = object_ids or sorted(field.objects)
    no_go = [t for t in field.targets.values()]
    fixed = [o.shape for o in obstacles.values()] + keepout_shapes(field)

    for _ in range(max_tries):
        placed: dict[str, Point] = {}
        ok = True
        for oid in ids:
            r = field.objects[oid].radius_cm
            for _attempt in range(300):
                p = (rng.uniform(RANDOM_EDGE, field.width - RANDOM_EDGE),
                     rng.uniform(RANDOM_EDGE, field.height - RANDOM_EDGE))
                if any(_touches_target(t, p, r + RANDOM_GAP) for t in no_go):
                    continue
                if any(s.distance_to_point(p) < r + RANDOM_GAP for s in fixed):
                    continue
                if any(dist(p, q.xy) < START_CLEAR + r
                       for q in (field.park, *field.start_entries)):
                    continue
                if any(dist(p, q) < r + field.objects[o].radius_cm + RANDOM_GAP
                       for o, q in placed.items()):
                    continue
                placed[oid] = p
                break
            else:
                ok = False
                break
        if not ok:
            continue
        # 每个物体都要能接近 (其他物体和障碍都算障碍)
        if all(_approachable(oid, placed, field, geom, fixed) for oid in placed):
            return Layout(f"random-{seed}", placed, dict.fromkeys(placed), obstacles)
    raise FieldModelError([f"随机种子 {seed} 生成不了合法的布局"], "random_layout")


def _touches_target(target, p: Point, r: float) -> bool:
    if target.polygon is not None:
        return polygon_point_distance(target.polygon, p) < r
    return dist(target.center, p) < target.radius + r


def _approachable(oid: str, placed: dict[str, Point], field: FieldModel,
                  geom: RobotGeometry, fixed: list[Shape]) -> bool:
    others = [Shape(o, center=q, radius=field.objects[o].radius_cm)
              for o, q in placed.items() if o != oid]
    plan = choose_approach(placed[oid], field.objects[oid].radius_cm, geom,
                           field.width, field.height, others + fixed)
    return plan is not None


__all__ = ["LAYOUT_DIR", "Layout", "Obstacle", "load_layout", "parse_layout", "random_layout"]
