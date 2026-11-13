"""
道路网路径规划 (改动 navigation-grasp 设计 D3, 任务 4.1)。

道路网 = 场地上的黑线 (节点 + 直线连接, 权重是长度), 规模很小, 用 Dijkstra。
起点用一条直线连到最近的几个可达节点, 最后一个节点再用直线连到目标点;
所有直线段 (包括道路本身) 都要和启用的禁行区、其他物体保持车身半宽以上的距离。
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass

from pc.field.geometry import Point, Shape, dist, segment_point_distance
from pc.field.model import FieldModel

CONNECT_NEAREST = 3  # 起点 / 终点连到最近的几个可达节点
DIRECT_MAX = 45.0  # 起点和终点离得很近 (而且直线可达) 时直接走直线
# 离得很近的两个道路节点之间加一条直线捷径 (例如起终点区旁边的台阶形黑线贴着墙,
# 夹着物体时车很长, 在那里转不了弯)
SHORTCUT_MAX = 20.0
# 场地中间没有黑线 (例如圆圈周围): 用一个粗网格补充 "虚拟节点", 只放在空旷、能原地转向的地方;
# 走网格比走黑线多算 20% 的长度, 所以有黑线时优先沿黑线走
GRID_STEP = 15.0
GRID_PENALTY = 1.2
_START, _GOAL = "__start__", "__goal__"


@dataclass(frozen=True)
class Route:
    ok: bool
    points: tuple[Point, ...] = ()  # 包括起点和终点
    nodes: tuple[str, ...] = ()  # 经过的道路节点
    reason: str = ""

    @property
    def length(self) -> float:
        return sum(dist(a, b) for a, b in zip(self.points, self.points[1:], strict=False))


def segment_clear(a: Point, b: Point, obstacles: Sequence[Shape], clearance: float) -> bool:
    return all(ob.distance_to_segment(a, b) >= clearance for ob in obstacles)


def plan_route(
    field: FieldModel, start: Point, goal: Point,
    obstacles: Sequence[Shape] = (), clearance: float = 8.0, turn_room: float = 0.0,
    connect_nearest: int | None = CONNECT_NEAREST,
) -> Route:
    """
    规划从 start 到 goal 的路线。obstacles: 启用的禁行区和不能碰的物体;
    clearance: 路线离它们至少多远 (车身半宽 + 余量)。
    turn_room: 原地转向需要的空间 (车扫过的半径)。离障碍不到这么远的道路节点只能直行通过,
    不能在那里拐弯, 也不能作为起点 / 终点的连接点 (例如底部横线上 D 区方格旁边的路口)。
    """
    for ob in obstacles:
        if ob.distance_to_point(goal) < 1.0:
            return Route(False, reason=f"找不到路线：目标点在 {ob.id} 上")

    nodes = dict(field.road_nodes)
    adj: dict[str, list[tuple[str, float]]] = {n: [] for n in nodes}
    for a, b, length in field.road_edges:
        if segment_clear(nodes[a], nodes[b], obstacles, clearance):
            adj[a].append((b, length))
            adj[b].append((a, length))
    linked = {(a, b) for a, b, _ in field.road_edges} | {(b, a) for a, b, _ in field.road_edges}
    names = list(field.road_nodes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            d = dist(nodes[a], nodes[b])
            if (a, b) not in linked and d <= SHORTCUT_MAX \
                    and segment_clear(nodes[a], nodes[b], obstacles, clearance):
                adj[a].append((b, d))
                adj[b].append((a, d))

    def wall_room(p: Point) -> float:
        return min(p[0], field.width - p[0], p[1], field.height - p[1])

    # 网格节点
    margin = clearance + 0.5
    gx = [margin + i * GRID_STEP for i in range(int((field.width - 2 * margin) // GRID_STEP) + 1)]
    gy = [margin + j * GRID_STEP for j in range(int((field.height - 2 * margin) // GRID_STEP) + 1)]
    grid: dict[tuple[int, int], str] = {}
    for i, x in enumerate(gx):
        for j, y in enumerate(gy):
            far = max(clearance, turn_room)
            if wall_room((x, y)) >= turn_room and \
                    all(ob.distance_to_point((x, y)) >= far for ob in obstacles):
                name = f"__g{i}_{j}"
                grid[(i, j)] = name
                nodes[name] = (x, y)
                adj[name] = []
    for (i, j), name in grid.items():
        for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
            other = grid.get((i + di, j + dj))
            if other and segment_clear(nodes[name], nodes[other], obstacles, clearance):
                w = dist(nodes[name], nodes[other]) * GRID_PENALTY
                adj[name].append((other, w))
                adj[other].append((name, w))
        for rn, rp in field.road_nodes.items():
            d = dist(nodes[name], rp)
            if d <= GRID_STEP * 1.5 and segment_clear(nodes[name], rp, obstacles, clearance):
                adj[name].append((rn, d * GRID_PENALTY))
                adj[rn].append((name, d * GRID_PENALTY))

    adj[_START] = []
    adj[_GOAL] = []
    nodes[_START] = start
    nodes[_GOAL] = goal

    # 起点 / 终点本身就离某个障碍很近 (例如刚抓完物体): 从这里出发的线段只要不更靠近它就行
    def local_obstacles(p: Point) -> list[Shape]:
        return [ob for ob in obstacles if ob.distance_to_point(p) >= clearance]

    def tight(p: Point) -> list[tuple[Shape, float]]:
        return [(ob, ob.distance_to_point(p) - 0.2) for ob in obstacles
                if ob.distance_to_point(p) < clearance]

    def endpoint_clear(p: Point, q: Point) -> bool:
        if not segment_clear(p, q, local_obstacles(p), clearance):
            return False
        return all(ob.distance_to_segment(p, q) >= limit for ob, limit in tight(p))

    def room(n: str) -> float:
        p = nodes[n]
        return min([wall_room(p), *(ob.distance_to_point(p) for ob in obstacles)])

    cramped = {n for n in field.road_nodes if room(n) < turn_room}
    candidates = {**field.road_nodes, **{n: nodes[n] for n in grid.values()}}

    def connect(name: str, p: Point) -> None:
        reachable = sorted(
            (dist(p, q) * (GRID_PENALTY if n.startswith("__g") else 1.0), n)
            for n, q in candidates.items()
            if endpoint_clear(p, q)  # 狭窄的节点也可以连: 搜索时只允许直行通过它
        )[:connect_nearest]
        for d, n in reachable:
            adj[name].append((n, d))
            adj[n].append((name, d))

    connect(_START, start)
    connect(_GOAL, goal)
    if dist(start, goal) <= DIRECT_MAX and endpoint_clear(start, goal) \
            and endpoint_clear(goal, start):
        adj[_START].append((_GOAL, dist(start, goal)))

    def straight(a: str, b: str, c: str) -> bool:
        pa, pb, pc = nodes[a], nodes[b], nodes[c]
        h1 = math.atan2(pb[1] - pa[1], pb[0] - pa[0])
        h2 = math.atan2(pc[1] - pb[1], pc[0] - pb[0])
        diff = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        return math.degrees(diff) <= 10.0

    # Dijkstra; 状态 = (当前节点, 上一个节点), 这样才能检查 "不能在狭窄的路口拐弯"
    start_state = (_START, "")
    best: dict[tuple[str, str], float] = {start_state: 0.0}
    prev: dict[tuple[str, str], tuple[str, str]] = {}
    heap = [(0.0, _START, "")]
    found: tuple[str, str] | None = None
    while heap:
        d, n, came = heapq.heappop(heap)
        state = (n, came)
        if n == _GOAL:
            found = state
            break
        if d > best.get(state, float("inf")):
            continue
        for m, w in adj[n]:
            if m == came:
                continue
            if n in cramped and came and not straight(came, n, m):
                continue
            nxt = (m, n)
            nd = d + w
            if nd < best.get(nxt, float("inf")):
                best[nxt] = nd
                prev[nxt] = state
                heapq.heappush(heap, (nd, m, n))
    if found is None:
        if connect_nearest is not None:
            # 最近的几个节点都走不通 (例如夹着物体、旁边都是障碍): 允许连到所有可达的节点再试
            return plan_route(field, start, goal, obstacles, clearance, turn_room, None)
        return Route(False, reason="找不到路线：目标被禁行区或障碍围住")

    states = [found]
    while states[-1] != start_state:
        states.append(prev[states[-1]])
    path = [n for n, _ in reversed(states)]
    points = simplify([nodes[n] for n in path])
    road_nodes = tuple(n for n in path if n not in (_START, _GOAL) and not n.startswith("__g"))
    return Route(True, tuple(points), road_nodes)


def simplify(points: list[Point], tolerance: float = 0.5) -> list[Point]:
    """去掉直线上多余的中间点 (少转几次弯), 并去掉重合的点。"""
    out: list[Point] = []
    for p in points:
        if out and dist(out[-1], p) < 0.5:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        if segment_point_distance(out[i - 1], out[i + 1], out[i]) <= tolerance:
            del out[i]
        else:
            i += 1
    return out


__all__ = ["Route", "plan_route", "segment_clear", "simplify"]
