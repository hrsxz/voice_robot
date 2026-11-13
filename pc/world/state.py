"""
世界状态 (改动 navigation-grasp 设计 D2, 任务 2.1–2.3)。

把三方数据汇总成一份: 感知 (小车位姿、物体位置)、Hub 回报 (夹爪角度)、任务进度 (被夹住的物体)。
快照是不可变的数据类, summary() 是给任务层 LLM 用的简洁结构 (厘米取整)。

物体状态 (有且只有一个):
    at_start 在起点 / moved 被移动 / captured 被夹住 / delivered 已送达 /
    occluded 被遮挡 / missing 丢失 (开局就没检测到)
"已送达": 物体底面 (按目录里的半径画圆) 和它的目标区域或圆圈有任何重叠 (WRO 规则: 部分接触也算)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pc.field.geometry import Point, Pose, dist
from pc.field.model import FieldModel
from pc.navigation.ports import HubResult, ObjectReading, PoseReading

STATES = ("at_start", "moved", "captured", "delivered", "occluded", "missing")
STATE_LABELS = {
    "at_start": "在起点", "moved": "被移动", "captured": "被夹住", "delivered": "已送达",
    "occluded": "被遮挡", "missing": "丢失",
}
MOVED_THRESHOLD = 2.0  # 离起点超过多少厘米算"被移动"
GRIPPER_LABELS = {"up": "抬起", "down": "放下", "mixed": "一个抬起一个放下", "unknown": "未知"}


@dataclass(frozen=True)
class ObjectState:
    id: str
    type: str
    color: str
    state: str
    position: Point | None  # 最后一次看到的位置 (丢失时为 None)
    start: Point | None
    target: str  # 目标, 例如 "zone:B"
    seen_age_s: float | None = None  # 最后一次看到距今多久


@dataclass(frozen=True)
class WorldSnapshot:
    time: float
    robot: Pose | None  # 最后一次看到的位姿
    robot_visible: bool
    robot_age_s: float | None  # 位姿数据距今多久
    gripper: str  # up / down / mixed / unknown
    gripper_angles: tuple[int, int] | None
    holding: str | None  # 被夹住的物体
    objects: tuple[ObjectState, ...]

    def object(self, oid: str) -> ObjectState:
        for o in self.objects:
            if o.id == oid:
                return o
        raise KeyError(oid)

    def summary(self) -> dict[str, Any]:
        """给任务层 LLM 的结构化摘要 (坐标精确到厘米)。"""
        robot: dict[str, Any] = {"visible": self.robot_visible}
        if self.robot is not None:
            robot.update(x=round(self.robot.x), y=round(self.robot.y),
                         heading=round(self.robot.heading) % 360)
        return {
            "robot": robot,
            "gripper": GRIPPER_LABELS[self.gripper],
            "holding": self.holding,
            "objects": [
                {
                    "id": o.id, "type": o.type, "color": o.color,
                    "state": STATE_LABELS[o.state],
                    **({"x": round(o.position[0]), "y": round(o.position[1])}
                       if o.position is not None else {}),
                    "target": o.target,
                }
                for o in self.objects
            ],
        }


@dataclass
class _Track:
    start: Point | None = None
    position: Point | None = None
    visible: bool = False
    seen_at: float | None = None


@dataclass
class WorldState:
    field: FieldModel
    clock: Callable[[], float]
    targets: dict[str, str] = field(default_factory=dict)  # 物体 -> 目标 (默认用目录里的)

    def __post_init__(self) -> None:
        self._robot: Pose | None = None
        self._robot_visible = False
        self._robot_at: float | None = None
        self._gripper: tuple[int, int] | None = None
        self._holding: str | None = None
        self._tracks: dict[str, _Track] = {oid: _Track() for oid in self.field.objects}
        self._started = False
        for oid, spec in self.field.objects.items():
            self.targets.setdefault(oid, spec.target)

    # ---------------- 更新 ----------------

    def update_pose(self, reading: PoseReading) -> None:
        if reading.visible:
            self._robot = reading.pose
            self._robot_at = reading.timestamp
            self._robot_visible = True
        else:
            self._robot_visible = False
            if self._robot is None:
                self._robot = reading.last_pose

    def update_objects(self, readings: dict[str, ObjectReading]) -> None:
        """第一次调用时记下开局位置; 开局没检测到的物体就是"丢失"。"""
        for oid, r in readings.items():
            tr = self._tracks.get(oid)
            if tr is None:
                continue
            if not self._started:
                tr.start = r.position
            elif tr.start is None:
                tr.start = r.position  # 开局丢失、后来又出现的物体: 从现在算起点
            tr.position = r.position
            tr.visible = r.visible
            if r.visible:
                tr.seen_at = r.timestamp
        for oid, tr in self._tracks.items():
            if oid not in readings:
                tr.visible = False
        self._started = True

    def update_hub(self, result: HubResult) -> None:
        """夹爪状态只来自 Hub 回报的角度。"""
        if result.gripper is not None:
            self._gripper = result.gripper

    def reset_gripper(self) -> None:
        """Hub 程序重新启动: 夹爪角度未知。"""
        self._gripper = None

    async def refresh(self, poses: Any, objects: Any) -> WorldSnapshot:
        self.update_pose(await poses.read_pose())
        self.update_objects(await objects.read_objects())
        return self.snapshot()

    # ---------------- 任务进度 (由抓取 / 放下设置) ----------------

    def mark_captured(self, oid: str) -> None:
        if self._holding is not None and self._holding != oid:
            raise ValueError(f"已经夹着 {self._holding}")
        self._holding = oid

    def mark_released(self, oid: str) -> None:
        if self._holding == oid:
            self._holding = None

    @property
    def holding(self) -> str | None:
        return self._holding

    def set_target(self, oid: str, target_id: str) -> None:
        self.field.target(target_id)  # 不存在时抛 KeyError
        self.targets[oid] = target_id

    # ---------------- 快照 ----------------

    def gripper_state(self) -> str:
        if self._gripper is None:
            return "unknown"
        left, right = self._gripper
        if left >= 45 and right >= 45:
            return "up"
        if left < 45 and right < 45:
            return "down"
        return "mixed"

    def object_state(self, oid: str) -> str:
        tr = self._tracks[oid]
        if self._holding == oid:
            return "captured"
        if tr.position is None:
            return "missing"
        if not tr.visible:
            return "occluded"
        spec = self.field.objects[oid]
        target = self.field.target(self.targets[oid])
        if target.touches(tr.position, spec.radius_cm):
            return "delivered"
        if tr.start is not None and dist(tr.start, tr.position) > MOVED_THRESHOLD:
            return "moved"
        return "at_start"

    def snapshot(self) -> WorldSnapshot:
        now = self.clock()
        objects = []
        for oid, spec in self.field.objects.items():
            tr = self._tracks[oid]
            objects.append(ObjectState(
                id=oid, type=spec.type, color=spec.color, state=self.object_state(oid),
                position=tr.position, start=tr.start, target=self.targets[oid],
                seen_age_s=None if tr.seen_at is None else max(0.0, now - tr.seen_at),
            ))
        return WorldSnapshot(
            time=now,
            robot=self._robot,
            robot_visible=self._robot_visible,
            robot_age_s=None if self._robot_at is None else max(0.0, now - self._robot_at),
            gripper=self.gripper_state(),
            gripper_angles=self._gripper,
            holding=self._holding,
            objects=tuple(objects),
        )


__all__ = [
    "GRIPPER_LABELS",
    "STATES",
    "STATE_LABELS",
    "ObjectState",
    "WorldSnapshot",
    "WorldState",
]
