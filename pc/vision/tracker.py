"""
物体身份和跟踪 (改动 vision-field-model 设计 D9, 任务 5.3–5.4)。

开局: 按颜色约束, 用匈牙利算法把检测结果和布局里的起始位置一一配对 (离起点超过 12 cm 的不配),
      配不上的报告为"缺少"(布局里有, 场上没检测到) 或"多出"(检测到了, 布局里没有)。
运行中: 每个已知物体在 30 cm 以内找同色的检测结果 (同色的几个物体一起做最优配对);
      找不到就保留原位置并标为"被遮挡"; 被夹住的物体 (由导航层告诉跟踪器) 位置直接取笼区中心。

latest_objects() 的结果可以直接交给 pc.navigation.ports.VisionObjectSource。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from pc.field.geometry import Point
from pc.field.model import FieldModel
from pc.vision.objects import ObjectDetection

INF = 1e9


def hungarian(cost: np.ndarray) -> list[tuple[int, int]]:
    """最小代价的一一配对 (矩形矩阵, Kuhn-Munkres, O(n³))。返回 (行, 列) 列表。"""
    c = np.asarray(cost, float)
    n_rows, n_cols = c.shape
    n = max(n_rows, n_cols)
    if n == 0:
        return []
    a = np.full((n, n), INF)
    a[:n_rows, :n_cols] = c
    u = np.zeros(n + 1)
    v = np.zeros(n + 1)
    p = np.zeros(n + 1, int)  # p[j] = 配给第 j 列的行 (1-based)
    way = np.zeros(n + 1, int)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = np.full(n + 1, np.inf)
        used = np.zeros(n + 1, bool)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = np.inf
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = a[i0 - 1, j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    pairs = []
    for j in range(1, n + 1):
        i = p[j] - 1
        if i < n_rows and j - 1 < n_cols and a[i, j - 1] < INF:
            pairs.append((i, j - 1))
    return sorted(pairs)


@dataclass
class TrackedObject:
    id: str
    color: str
    height: float
    x: float
    y: float
    visible: bool
    timestamp: float
    captured: bool = False

    @property
    def xy(self) -> Point:
        return (self.x, self.y)


@dataclass
class InitResult:
    assigned: dict[str, Point] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    extra: list[ObjectDetection] = field(default_factory=list)

    @property
    def message(self) -> str:
        parts = [f"认出 {len(self.assigned)} 个物体"]
        if self.missing:
            parts.append("缺少 " + "、".join(self.missing))
        if self.extra:
            parts.append("多出 " + "、".join(
                f"{d.color}({d.ground[0]:.0f}, {d.ground[1]:.0f})" for d in self.extra))
        return "，".join(parts)


def _match(tracks: list[tuple[str, str, float, Point]], detections: list[ObjectDetection],
           gate: float) -> tuple[dict[str, tuple[int, Point]], set[int]]:
    """
    按颜色分组配对。tracks: (编号, 颜色, 高度, 参考位置)。
    返回 {编号: (检测序号, 修正后的位置)} 和用掉的检测序号。
    """
    matched: dict[str, tuple[int, Point]] = {}
    used: set[int] = set()
    colors = {t[1] for t in tracks}
    for color in colors:
        ts = [t for t in tracks if t[1] == color]
        ds = [i for i, d in enumerate(detections) if d.color == color]
        if not ds:
            continue
        cost = np.full((len(ts), len(ds)), INF)
        pos: dict[tuple[int, int], Point] = {}
        for a, (_oid, _c, h, ref) in enumerate(ts):
            for b, di in enumerate(ds):
                p = detections[di].position(h)
                dd = math.hypot(p[0] - ref[0], p[1] - ref[1])
                if dd <= gate:
                    cost[a, b] = dd
                    pos[(a, b)] = p
        for a, b in hungarian(cost):
            matched[ts[a][0]] = (ds[b], pos[(a, b)])
            used.add(ds[b])
    return matched, used


class ObjectTracker:
    def __init__(self, fm: FieldModel, starts: dict[str, Point], start_gate: float = 12.0,
                 track_gate: float = 30.0) -> None:
        self.field = fm
        self.starts = dict(starts)  # 布局里的起始位置 (这一局场上应该有的物体)
        self.start_gate = start_gate
        self.track_gate = track_gate
        self.objects: dict[str, TrackedObject] = {}
        self.extra: list[ObjectDetection] = []

    def initialize(self, detections: list[ObjectDetection], timestamp: float) -> InitResult:
        tracks = [(oid, self.field.objects[oid].color, self.field.objects[oid].height_cm, p)
                  for oid, p in self.starts.items()]
        matched, used = _match(tracks, detections, self.start_gate)
        result = InitResult()
        self.objects.clear()
        for oid, _color, h, _p in tracks:
            if oid in matched:
                p = matched[oid][1]
                result.assigned[oid] = p
                self.objects[oid] = TrackedObject(oid, self.field.objects[oid].color, h,
                                                  p[0], p[1], True, timestamp)
            else:
                result.missing.append(oid)
        result.extra = [d for i, d in enumerate(detections) if i not in used]
        self.extra = result.extra
        return result

    def update(self, detections: list[ObjectDetection], timestamp: float,
               captured: str | None = None, cage_center: Point | None = None) -> None:
        """
        captured: 当前被夹住的物体 (导航层标记); cage_center: 笼区中心 (小车看不见时为 None,
        被夹住的物体保持最后位置)。
        """
        for o in self.objects.values():
            o.captured = o.id == captured
        if captured in self.objects and cage_center is not None:
            o = self.objects[captured]
            o.x, o.y = cage_center
            o.visible = True
            o.timestamp = timestamp
        tracks = [(o.id, o.color, o.height, o.xy) for o in self.objects.values()
                  if not o.captured]
        matched, _used = _match(tracks, detections, self.track_gate)
        for oid, _c, _h, _p in tracks:
            o = self.objects[oid]
            if oid in matched:
                o.x, o.y = matched[oid][1]
                o.visible = True
                o.timestamp = timestamp
            else:
                o.visible = False  # 被遮挡: 保留最后的位置

    def latest_objects(self) -> list[TrackedObject]:
        return list(self.objects.values())


__all__ = ["InitResult", "ObjectTracker", "TrackedObject", "hungarian"]
