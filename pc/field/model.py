"""
场地模型: 读取 pc/field/data/wro_field.yaml 并校验 (改动 vision-field-model 任务 3.1,
navigation-grasp 任务 3.1)。

    field = load_field()                     # 校验失败时抛出 FieldModelError, 列出所有错误
    field.zone("B").polygon
    field.target("circle:red")               # -> Target (区域 / 圆圈 / 起终点区)
    field.objects["blue_person"].target      # 默认目标 "zone:B"

物体放在哪里不在这里, 见 pc/field/layout.py。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from pc import constants
from pc.field.geometry import Point, Polygon, Pose, dist, point_in_polygon, polygon_circle_overlap

DATA_DIR = constants.project_root_path / "pc" / "field" / "data"
DEFAULT_FIELD = DATA_DIR / "wro_field.yaml"
OBJECT_TYPES = ("small_person", "large_person", "package", "service_robot")
COLORS = ("yellow", "green", "blue", "red")
EDGE_TOLERANCE = 0.5  # 场地元素允许超出场地边界的误差 (测量误差)


class FieldModelError(ValueError):
    """场地模型 (或布局) 校验失败。errors 是全部错误, 每条一句话。"""

    def __init__(self, errors: list[str], source: str = "") -> None:
        self.errors = list(errors)
        head = f"{source}: " if source else ""
        super().__init__(head + "；".join(self.errors))


@dataclass(frozen=True)
class Zone:
    id: str
    color: str
    polygon: tuple[Point, ...]
    entries: tuple[Pose, ...]


@dataclass(frozen=True)
class Circle:
    id: str  # 颜色名
    center: Point
    radius: float
    entries: tuple[Pose, ...]

    @property
    def color(self) -> str:
        return self.id


@dataclass(frozen=True)
class Position:
    id: str
    x: float
    y: float
    approach: float | None = None  # 抓取这里的物体时小车的朝向; None = 自动选择

    @property
    def xy(self) -> Point:
        return (self.x, self.y)


@dataclass(frozen=True)
class ObjectSpec:
    id: str
    type: str
    color: str
    height_cm: float
    radius_cm: float  # 底面按圆处理
    target: str  # 默认目标, 例如 "zone:B"


@dataclass(frozen=True)
class Keepout:
    id: str
    enabled: bool
    polygon: tuple[Point, ...] | None = None
    center: Point | None = None
    radius: float = 0.0

    def distance_to(self, p: Point) -> float:
        from pc.field.geometry import polygon_point_distance

        if self.polygon is not None:
            return polygon_point_distance(self.polygon, p)
        assert self.center is not None
        return max(0.0, dist(self.center, p) - self.radius)


@dataclass(frozen=True)
class Target:
    """物体可以送达的地方: 区域、圆圈或起终点区。"""

    id: str  # "zone:B" / "circle:red" / "start"
    kind: str  # zone / circle / start
    entries: tuple[Pose, ...]
    polygon: tuple[Point, ...] | None = None
    center: Point | None = None
    radius: float = 0.0

    @property
    def label(self) -> str:
        if self.kind == "zone":
            return f"{self.id.split(':', 1)[1]} 区"
        if self.kind == "circle":
            return f"{self.id.split(':', 1)[1]} 圆圈"
        return "起终点区"

    def touches(self, center: Point, radius: float) -> bool:
        """物体底面 (圆) 和目标有没有重叠: 部分接触也算 (WRO 规则)。"""
        if self.polygon is not None:
            return polygon_circle_overlap(self.polygon, center, radius)
        assert self.center is not None
        return dist(self.center, center) <= self.radius + radius

    def contains(self, p: Point, margin: float = 0.0) -> bool:
        """点在目标里, 并且离边界至少 margin。"""
        if self.polygon is not None:
            from pc.field.geometry import polygon_edges, segment_point_distance

            if not point_in_polygon(p, self.polygon):
                return False
            return all(segment_point_distance(a, b, p) >= margin
                       for a, b in polygon_edges(self.polygon))
        assert self.center is not None
        return dist(self.center, p) <= self.radius - margin

    @property
    def anchor(self) -> Point:
        """目标的中心点 (放下物体时朝它走)。"""
        if self.center is not None:
            return self.center
        assert self.polygon is not None
        xs = [p[0] for p in self.polygon]
        ys = [p[1] for p in self.polygon]
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


@dataclass(frozen=True)
class FieldModel:
    width: float
    height: float
    anchors: dict[int, Point]
    zones: dict[str, Zone]
    start_polygon: tuple[Point, ...]
    start_entries: tuple[Pose, ...]
    park: Pose
    circles: dict[str, Circle]
    positions: dict[str, Position]
    objects: dict[str, ObjectSpec]
    road_nodes: dict[str, Point]
    road_edges: tuple[tuple[str, str, float], ...]
    curves: dict[str, tuple[Point, ...]]
    keepouts: dict[str, Keepout]
    source: str = ""
    _targets: dict[str, Target] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        targets: dict[str, Target] = {}
        for z in self.zones.values():
            targets[f"zone:{z.id}"] = Target(f"zone:{z.id}", "zone", z.entries, polygon=z.polygon)
        for c in self.circles.values():
            targets[f"circle:{c.id}"] = Target(f"circle:{c.id}", "circle", c.entries,
                                               center=c.center, radius=c.radius)
        targets["start"] = Target("start", "start", self.start_entries,
                                  polygon=self.start_polygon)
        object.__setattr__(self, "_targets", targets)

    # --- 查询 ---

    def zone(self, zone_id: str) -> Zone:
        return self.zones[zone_id]

    def circle(self, color: str) -> Circle:
        return self.circles[color]

    @property
    def targets(self) -> dict[str, Target]:
        return dict(self._targets)

    def target(self, target_id: str) -> Target:
        try:
            return self._targets[target_id]
        except KeyError:
            raise KeyError(f"没有这个目标：{target_id}") from None

    def resolve_point(self, ref: Any) -> Point:
        """位置点名称或 [x, y] -> 坐标。"""
        if isinstance(ref, str):
            return self.positions[ref].xy
        return (float(ref[0]), float(ref[1]))

    def inside(self, p: Point, tolerance: float = EDGE_TOLERANCE) -> bool:
        return (-tolerance <= p[0] <= self.width + tolerance
                and -tolerance <= p[1] <= self.height + tolerance)

    def enabled_keepouts(self) -> list[Keepout]:
        return [k for k in self.keepouts.values() if k.enabled]

    def with_keepouts(self, enabled: dict[str, bool]) -> FieldModel:
        """返回改了禁行区开关的副本。"""
        from dataclasses import replace

        unknown = set(enabled) - set(self.keepouts)
        if unknown:
            raise KeyError(f"没有这些禁行区：{sorted(unknown)}")
        keepouts = {k: replace(v, enabled=enabled.get(k, v.enabled))
                    for k, v in self.keepouts.items()}
        return replace(self, keepouts=keepouts)

    def road_connected(self) -> bool:
        if not self.road_nodes:
            return False
        adj: dict[str, set[str]] = {n: set() for n in self.road_nodes}
        for a, b, _ in self.road_edges:
            adj[a].add(b)
            adj[b].add(a)
        start = next(iter(adj))
        seen = {start}
        stack = [start]
        while stack:
            for nxt in adj[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return len(seen) == len(adj)


# ---------------------------------------------------------------- 读取


class _UniqueKeyLoader(yaml.SafeLoader):
    """重复的键 (编号) 直接报错, 而不是像 PyYAML 默认那样悄悄覆盖。"""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"重复的编号：{key}", key_node.start_mark)
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    try:
        data = yaml.load(p.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise FieldModelError([str(exc).replace("\n", " ")], p.name) from exc
    if not isinstance(data, dict):
        raise FieldModelError(["文件内容必须是一个映射"], p.name)
    return data


def load_field(path: str | Path | None = None, validate_approach: bool = True) -> FieldModel:
    p = Path(path) if path else DEFAULT_FIELD
    return parse_field(read_yaml(p), source=p.name, validate_approach=validate_approach)


def _pose(item: Any) -> Pose:
    return Pose(float(item["x"]), float(item["y"]), float(item["heading"]) % 360)


def _points(raw: Any) -> tuple[Point, ...]:
    return tuple((float(x), float(y)) for x, y in raw)


def parse_field(data: dict[str, Any], source: str = "",
                validate_approach: bool = True) -> FieldModel:
    errors: list[str] = []

    def section(name: str) -> dict[str, Any]:
        value = data.get(name)
        if not isinstance(value, dict):
            errors.append(f"缺少 {name}")
            return {}
        return value

    try:
        fd = section("field")
        width, height = float(fd["width_cm"]), float(fd["height_cm"])
    except (KeyError, TypeError, ValueError):
        raise FieldModelError(["field 必须有 width_cm 和 height_cm"], source) from None

    def check_point(p: Point, what: str) -> None:
        if not (-EDGE_TOLERANCE <= p[0] <= width + EDGE_TOLERANCE
                and -EDGE_TOLERANCE <= p[1] <= height + EDGE_TOLERANCE):
            errors.append(f"{what} 超出场地范围：({p[0]:g}, {p[1]:g})")

    def entries(raw: Any, what: str) -> tuple[Pose, ...]:
        if not raw:
            errors.append(f"目标 {what} 缺少进入点")
            return ()
        out = []
        for i, item in enumerate(raw):
            try:
                pose = _pose(item)
            except (KeyError, TypeError, ValueError):
                errors.append(f"目标 {what} 的第 {i + 1} 个进入点格式不对（需要 x、y、heading）")
                continue
            check_point(pose.xy, f"目标 {what} 的进入点")
            out.append(pose)
        return tuple(out)

    anchors = {}
    for k, v in (data.get("anchors") or {}).items():  # anchor 可以在场地垫外, 不检查范围
        try:
            anchors[int(k)] = (float(v["x_cm"]), float(v["y_cm"]))
        except (KeyError, TypeError, ValueError):
            errors.append(f"anchor {k} 格式不对")

    zones: dict[str, Zone] = {}
    for zid_key, z in section("zones").items():
        zid = str(zid_key)
        try:
            poly = _points(z["polygon"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"区域 {zid} 缺少多边形")
            continue
        if len(poly) < 3:
            errors.append(f"区域 {zid} 的多边形至少要 3 个点")
        for pt in poly:
            check_point(pt, f"区域 {zid}")
        if z.get("color") not in COLORS:
            errors.append(f"区域 {zid} 的颜色不对：{z.get('color')}")
        zones[zid] = Zone(zid, str(z.get("color")), poly, entries(z.get("entries"), f"zone:{zid}"))

    sa = section("start_area")
    start_polygon = _points(sa.get("polygon") or [])
    if len(start_polygon) < 3:
        errors.append("起终点区缺少多边形")
    start_entries = entries(sa.get("entries"), "start")
    try:
        park = _pose(sa["park"])
        if not point_in_polygon(park.xy, start_polygon):
            errors.append("起终点区的停车位置不在起终点区里")
    except (KeyError, TypeError, ValueError):
        errors.append("起终点区缺少停车位置 park")
        park = Pose(0, 0, 0)

    circles: dict[str, Circle] = {}
    for cid_key, c in section("circles").items():
        cid = str(cid_key)
        if cid not in COLORS:
            errors.append(f"圆圈 {cid} 的名字必须是颜色：{', '.join(COLORS)}")
        try:
            center = (float(c["center"][0]), float(c["center"][1]))
            radius = float(c["radius"])
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append(f"圆圈 {cid} 需要 center 和 radius")
            continue
        check_point(center, f"圆圈 {cid}")
        if radius <= 0:
            errors.append(f"圆圈 {cid} 的半径必须大于 0")
        circles[cid] = Circle(cid, center, radius, entries(c.get("entries"), f"circle:{cid}"))

    positions: dict[str, Position] = {}
    for pid_key, pdata in section("positions").items():
        pid = str(pid_key)
        try:
            pos = Position(pid, float(pdata["x"]), float(pdata["y"]),
                           None if pdata.get("approach") is None
                           else float(pdata["approach"]) % 360)
        except (KeyError, TypeError, ValueError):
            errors.append(f"位置点 {pid} 需要 x 和 y")
            continue
        check_point(pos.xy, f"位置点 {pid}")
        positions[pid] = pos

    targets_ok = {f"zone:{z}" for z in zones} | {f"circle:{c}" for c in circles} | {"start"}
    objects: dict[str, ObjectSpec] = {}
    for oid_key, o in section("objects").items():
        oid = str(oid_key)
        try:
            spec = ObjectSpec(oid, str(o["type"]), str(o["color"]), float(o["height_cm"]),
                              float(o["radius_cm"]), str(o["target"]))
        except (KeyError, TypeError, ValueError):
            errors.append(f"物体 {oid} 需要 type、color、height_cm、radius_cm、target")
            continue
        if spec.type not in OBJECT_TYPES:
            errors.append(f"物体 {oid} 的类型不对：{spec.type}")
        if spec.color not in COLORS:
            errors.append(f"物体 {oid} 的颜色不对：{spec.color}")
        if spec.target not in targets_ok:
            errors.append(f"物体 {oid} 的目标不存在：{spec.target}")
        if spec.radius_cm <= 0 or spec.height_cm <= 0:
            errors.append(f"物体 {oid} 的尺寸必须大于 0")
        objects[oid] = spec

    roads = section("roads")
    nodes: dict[str, Point] = {}
    for nid, xy in (roads.get("nodes") or {}).items():
        try:
            nodes[str(nid)] = (float(xy[0]), float(xy[1]))
        except (TypeError, ValueError, IndexError):
            errors.append(f"路线点 {nid} 格式不对")
            continue
        check_point(nodes[str(nid)], f"路线点 {nid}")
    edges: list[tuple[str, str, float]] = []
    for e in roads.get("edges") or []:
        try:
            a, b = str(e[0]), str(e[1])
        except (TypeError, IndexError):
            errors.append(f"道路连接格式不对：{e}")
            continue
        missing = [n for n in (a, b) if n not in nodes]
        if missing:
            errors.append(f"道路连接 {a}–{b} 引用了不存在的路线点 {', '.join(missing)}")
            continue
        edges.append((a, b, dist(nodes[a], nodes[b])))

    curves = {str(k): _points(v) for k, v in (data.get("curves") or {}).items()}

    keepouts: dict[str, Keepout] = {}
    for kid_key, k in (data.get("keepouts") or {}).items():
        kid = str(kid_key)
        enabled = bool(k.get("enabled", False))
        if "polygon" in k:
            poly = _points(k["polygon"])
            for pt in poly:
                check_point(pt, f"禁行区 {kid}")
            keepouts[kid] = Keepout(kid, enabled, polygon=poly)
        elif "circle" in k:
            c = k["circle"]
            at = c.get("at")
            if isinstance(at, str):
                if at not in positions:
                    errors.append(f"禁行区 {kid} 引用了不存在的位置 {at}")
                    continue
                center = positions[at].xy
            else:
                try:
                    center = (float(at[0]), float(at[1]))
                except (TypeError, ValueError, IndexError):
                    errors.append(f"禁行区 {kid} 的位置格式不对")
                    continue
            keepouts[kid] = Keepout(kid, enabled, center=center, radius=float(c.get("radius", 0)))
        else:
            errors.append(f"禁行区 {kid} 需要 polygon 或 circle")

    if errors:
        raise FieldModelError(errors, source)

    model = FieldModel(
        width=width, height=height, anchors=anchors, zones=zones,
        start_polygon=start_polygon, start_entries=start_entries, park=park,
        circles=circles, positions=positions, objects=objects,
        road_nodes=nodes, road_edges=tuple(edges), curves=curves, keepouts=keepouts,
        source=source,
    )
    if not model.road_connected():
        raise FieldModelError(["道路网不连通"], source)
    if validate_approach:
        from pc.field.approach import approach_errors

        errs = approach_errors(model)
        if errs:
            raise FieldModelError(errs, source)
    return model


def heading_of_vector(dx: float, dy: float) -> float:
    return math.degrees(math.atan2(dy, dx)) % 360


__all__ = [
    "Circle", "FieldModel", "FieldModelError", "Keepout", "ObjectSpec", "Polygon", "Position",
    "Target", "Zone", "load_field", "parse_field", "read_yaml",
]
