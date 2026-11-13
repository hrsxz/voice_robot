"""
运动学模拟器 (改动 navigation-grasp 设计 D7, 任务 1.2)。

二维运动学: 小车是带朝向的刚体, 物体是圆盘, 树是圆、障碍杆是多边形, 笼区是车身坐标系里的矩形。
- 直行和转向可以有比例误差、直行时有朝向漂移, 位姿和物体位置有测量噪声, 都可以配置, 有随机种子;
- 夹爪抬起时只有车身会碰到东西 (笼区是空的, 物体可以进去);
  夹爪放下时, 完全在笼区里的物体被夹住、随车移动; 压在笼壁上的物体被碰倒 (位置改变);
- 车身碰到物体会把它推开 (状态变成"被移动"); 碰到树或障碍杆记为"被碰倒";
- 时间是模拟时间: 每条命令按 motion_timing 的速度推进模拟时钟。time_scale > 0 时同时真实等待
  (time_scale = 1 是实时), 这样"运动中途停止"也能模拟。

对外提供和真实系统一样的三个接口: SimPoseSource / SimObjectSource / SimHubPort (见 ports.py)。
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from typing import Any

from pc.field.geometry import (
    Point,
    Pose,
    Shape,
    point_in_polygon,
    polygon_outside_rect,
    polygon_point_distance,
    polygons_distance,
)
from pc.field.layout import Layout
from pc.field.model import FieldModel
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.ports import HubResult, ObjectReading, PoseReading
from pc.spike_communication import motion_timing

MOVE_SUBSTEP = 1.0  # 直行每 1 cm 检查一次碰撞
TURN_SUBSTEP = 5.0  # 转向每 5° 检查一次
PUSH_GAP = 0.3


@dataclass
class SimConfig:
    distance_error: float = 0.0  # 直行距离的比例误差上限 (0.05 = ±5%)
    turn_error: float = 0.0  # 转向角度的比例误差上限
    drift_deg_per_m: float = 0.0  # 直行时朝向漂移 (每米的标准差, 度)
    pose_noise_cm: float = 0.0  # 位姿测量噪声 (标准差)
    heading_noise_deg: float = 0.0
    object_noise_cm: float = 0.0  # 物体位置测量噪声
    time_scale: float = 0.0  # 真实等待 = 模拟时长 × time_scale
    seed: int = 0
    occlude_under_body: bool = True  # 被车身挡住的物体看不见


@dataclass
class SimObject:
    id: str
    x: float
    y: float
    radius: float
    kind: str = "object"  # object / tree / barrier
    polygon: tuple[Point, ...] | None = None  # 障碍杆
    state: str = "normal"  # normal / captured / moved / knocked
    hidden: bool = False  # 模拟开局就没被检测到
    local: Point | None = None  # 被夹住时在车身坐标系里的位置

    @property
    def xy(self) -> Point:
        return (self.x, self.y)

    def distance_to_polygon(self, poly) -> float:
        if self.polygon is not None:
            return polygons_distance(self.polygon, poly)
        return max(0.0, polygon_point_distance(poly, self.xy) - self.radius)


@dataclass
class Simulator:
    field: FieldModel
    layout: Layout
    geom: RobotGeometry = field(default_factory=RobotGeometry.load)
    config: SimConfig = field(default_factory=SimConfig)
    robot: Pose | None = None

    def __post_init__(self) -> None:
        self.rng = random.Random(self.config.seed)
        if self.robot is None:
            self.robot = self.field.park
        self.time = 0.0
        self.gripper_down = False
        self.blocked = False  # 模拟小车被手挡住 (命令照常完成, 但不动)
        self.slippery: set[str] = set()  # 注入抓取失败: 这些物体夹不住 (放下夹爪也不跟着走)
        self.occlusions: list[tuple[float, float]] = []  # 位姿看不见的时间段 (模拟时间)
        self.objects: dict[str, SimObject] = {}
        for oid, (x, y) in self.layout.objects.items():
            r = self.field.objects[oid].radius_cm
            self.objects[oid] = SimObject(oid, x, y, r)
        for ob in self.layout.obstacles.values():
            s = ob.shape
            if s.polygon is not None:
                cx = sum(p[0] for p in s.polygon) / len(s.polygon)
                cy = sum(p[1] for p in s.polygon) / len(s.polygon)
                self.objects[ob.id] = SimObject(ob.id, cx, cy, 0.0, ob.type, s.polygon)
            else:
                assert s.center is not None
                self.objects[ob.id] = SimObject(ob.id, s.center[0], s.center[1], s.radius, ob.type)
        self.trace: list[Pose] = [self.robot]
        self.commands: list[str] = []
        self._running = False
        self._stop_requested = False
        self.hub = SimHubPort(self)
        self.poses = SimPoseSource(self)
        self.detector = SimObjectSource(self)

    # ---------------- 时间 ----------------

    def clock(self) -> float:
        return self.time

    async def sleep(self, seconds: float) -> None:
        """等待 (推进模拟时钟; time_scale > 0 时同时真实等待)。"""
        self.time += max(0.0, seconds)
        if self.config.time_scale > 0:
            await asyncio.sleep(seconds * self.config.time_scale)
        else:
            await asyncio.sleep(0)

    def pose_visible(self) -> bool:
        return not any(a <= self.time < b for a, b in self.occlusions)

    # ---------------- 查询 (模拟器的"真值") ----------------

    @property
    def task_objects(self) -> dict[str, SimObject]:
        return {k: v for k, v in self.objects.items() if v.kind == "object"}

    @property
    def captured(self) -> list[str]:
        return [o.id for o in self.objects.values() if o.state == "captured"]

    def knocked(self) -> list[str]:
        """被碰倒或被推动过的树和障碍杆。"""
        return [o.id for o in self.objects.values() if o.kind != "object" and o.state != "normal"]

    def gripper_angles(self) -> tuple[int, int]:
        return (0, 0) if self.gripper_down else (90, 90)

    # ---------------- 执行命令 ----------------

    async def execute(self, cmd: str) -> HubResult:
        self.commands.append(cmd)
        parts = cmd.strip().lower().split()
        action = parts[0] if parts else ""
        try:
            value = float(parts[1]) if len(parts) > 1 else None
        except ValueError:
            return HubResult("ERR", cmd, self.gripper_angles(), "参数不是数字")
        self._stop_requested = False
        self._running = True
        try:
            if action in ("forward", "backward") and value is not None:
                sign = 1 if action == "forward" else -1
                return await self._drive(cmd, sign * value)
            if action in ("left", "right") and value is not None:
                sign = -1 if action == "left" else 1  # 右转 = 顺时针 = 朝向增大
                return await self._turn(cmd, sign * value)
            if action in ("gripper_up", "gripper_down") or action.startswith("gripper_pos"):
                down = action == "gripper_down" or (action == "gripper_pos" and (value or 0) < 45)
                await self.sleep(motion_timing.estimate_duration_s(cmd))
                self._set_gripper(down)
                return HubResult("DONE", cmd, self.gripper_angles())
            if action == "stop":
                return HubResult("DONE", cmd, self.gripper_angles())
            return HubResult("ERR", cmd, self.gripper_angles(), "未知命令")
        finally:
            self._running = False

    def request_stop(self) -> None:
        if self._running:
            self._stop_requested = True

    async def _drive(self, cmd: str, distance: float) -> HubResult:
        cfg = self.config
        actual = distance * (1 + self.rng.uniform(-cfg.distance_error, cfg.distance_error))
        drift = 0.0
        if cfg.drift_deg_per_m:
            drift = self.rng.gauss(0, cfg.drift_deg_per_m) * abs(actual) / 100
        n = max(1, math.ceil(abs(actual) / MOVE_SUBSTEP))
        dt = motion_timing.estimate_duration_s(cmd) / n
        for _ in range(n):
            if self._stop_requested:
                return HubResult("STOPPED", cmd, self.gripper_angles(), "stop")
            if not self.blocked:
                assert self.robot is not None
                new = self.robot.moved(actual / n).turned(drift / n)
                self._move_robot(new, direction=1 if actual >= 0 else -1)
            await self.sleep(dt)
        return HubResult("DONE", cmd, self.gripper_angles())

    async def _turn(self, cmd: str, degrees: float) -> HubResult:
        cfg = self.config
        actual = degrees * (1 + self.rng.uniform(-cfg.turn_error, cfg.turn_error))
        n = max(1, math.ceil(abs(actual) / TURN_SUBSTEP))
        dt = motion_timing.estimate_duration_s(cmd) / n
        for _ in range(n):
            if self._stop_requested:
                return HubResult("STOPPED", cmd, self.gripper_angles(), "stop")
            if not self.blocked:
                assert self.robot is not None
                self._move_robot(self.robot.turned(actual / n), direction=0)
            await self.sleep(dt)
        return HubResult("DONE", cmd, self.gripper_angles())

    # ---------------- 物理 ----------------

    def _move_robot(self, new: Pose, direction: int) -> None:
        old = self.robot
        assert old is not None
        # 场地边界 (墙): 车身越出场地就停在墙前
        if polygon_outside_rect(self.geom.footprint_polygon(new, self.gripper_down),
                                self.field.width, self.field.height) > 0.2:
            return
        self.robot = new
        self.trace.append(new)
        for o in self.objects.values():
            if o.state == "captured":
                assert o.local is not None
                o.x, o.y = new.to_world(*o.local)
        body = self.geom.body_polygon(new)
        cage = self.geom.footprint_polygon(new) if self.gripper_down else None
        for o in self.objects.values():
            if o.state == "captured":
                continue
            hit = o.distance_to_polygon(body) <= 0
            if not hit and cage is not None:
                hit = o.distance_to_polygon(cage) <= 0
            if hit:
                self._push(o, old, new, direction)

    def _push(self, o: SimObject, old: Pose, new: Pose, direction: int) -> None:
        if o.kind != "object":
            o.state = "knocked"
            if o.polygon is not None:
                return  # 障碍杆被撞歪了, 位置变化不模拟
        else:
            o.state = "moved"
        # 沿运动方向推开 (转向时从车中心往外推), 直到不再接触
        if direction != 0:
            hx, hy = math.cos(math.radians(new.heading)), math.sin(math.radians(new.heading))
            ux, uy = hx * direction, hy * direction
        else:
            dx, dy = o.x - new.x, o.y - new.y
            d = math.hypot(dx, dy) or 1.0
            ux, uy = dx / d, dy / d
        polys = [self.geom.body_polygon(new)]
        if self.gripper_down:
            polys.append(self.geom.footprint_polygon(new))
        for _ in range(60):
            if all(polygon_point_distance(p, o.xy) > o.radius + PUSH_GAP for p in polys):
                break
            o.x += ux * 0.5
            o.y += uy * 0.5
        o.x = min(max(o.x, o.radius), self.field.width - o.radius)
        o.y = min(max(o.y, o.radius), self.field.height - o.radius)

    def _set_gripper(self, down: bool) -> None:
        robot = self.robot
        assert robot is not None
        if down and not self.gripper_down:
            inner = self.geom.cage_polygon(robot)
            outer = self.geom.footprint_polygon(robot)
            body = self.geom.body_polygon(robot)
            for o in self.objects.values():
                if o.state == "captured" or o.kind != "object":
                    continue
                if o.id in self.slippery:
                    continue
                if (point_in_polygon(o.xy, inner)
                        and polygon_point_distance(inner, o.xy) == 0
                        and _inside_margin(inner, o.xy, o.radius)):
                    o.state = "captured"
                    o.local = robot.to_local(o.xy)
                elif o.distance_to_polygon(outer) <= 0 and o.distance_to_polygon(body) > 0:
                    # 夹爪落在物体上: 碰倒, 位置改变
                    o.state = "moved"
                    angle = self.rng.uniform(0, 2 * math.pi)
                    shift = self.rng.uniform(2.0, 4.0)
                    o.x += math.cos(angle) * shift
                    o.y += math.sin(angle) * shift
            for o in self.objects.values():
                if o.kind != "object" and o.distance_to_polygon(outer) <= 0:
                    o.state = "knocked"
        elif not down and self.gripper_down:
            for o in self.objects.values():
                if o.state == "captured":
                    o.state = "moved"
                    o.local = None
        self.gripper_down = down

    # ---------------- 测量 ----------------

    def measure_pose(self) -> Pose:
        assert self.robot is not None
        c = self.config
        return Pose(self.robot.x + self.rng.gauss(0, c.pose_noise_cm),
                    self.robot.y + self.rng.gauss(0, c.pose_noise_cm),
                    (self.robot.heading + self.rng.gauss(0, c.heading_noise_deg)) % 360)

    def object_visible(self, o: SimObject) -> bool:
        if o.hidden:
            return False
        if self.config.occlude_under_body and o.state != "captured":
            assert self.robot is not None
            if polygon_point_distance(self.geom.body_polygon(self.robot), o.xy) <= 0:
                return False
        return True


def _inside_margin(poly, p: Point, margin: float) -> bool:
    from pc.field.geometry import polygon_edges, segment_point_distance

    return all(segment_point_distance(a, b, p) >= margin for a, b in polygon_edges(poly))


class SimHubPort:
    def __init__(self, sim: Simulator) -> None:
        self.sim = sim

    async def run(self, cmd: str) -> HubResult:
        return await self.sim.execute(cmd)

    async def stop(self) -> None:
        self.sim.request_stop()


class SimPoseSource:
    def __init__(self, sim: Simulator) -> None:
        self.sim = sim
        self._last: Pose | None = None

    def clock(self) -> float:
        return self.sim.clock()

    async def sleep(self, seconds: float) -> None:
        await self.sim.sleep(seconds)

    async def read_pose(self) -> PoseReading:
        if not self.sim.pose_visible():
            return PoseReading(None, self.sim.time, self._last)
        pose = self.sim.measure_pose()
        self._last = pose
        return PoseReading(pose, self.sim.time, pose)


class SimObjectSource:
    def __init__(self, sim: Simulator) -> None:
        self.sim = sim
        self._last: dict[str, Point] = {}

    async def read_objects(self) -> dict[str, ObjectReading]:
        out: dict[str, ObjectReading] = {}
        noise = self.sim.config.object_noise_cm
        for o in self.sim.task_objects.values():
            if o.hidden:
                continue
            if self.sim.object_visible(o):
                p = (o.x + self.sim.rng.gauss(0, noise), o.y + self.sim.rng.gauss(0, noise))
                self._last[o.id] = p
                out[o.id] = ObjectReading(o.id, p, True, self.sim.time)
            elif o.id in self._last:
                out[o.id] = ObjectReading(o.id, self._last[o.id], False, self.sim.time)
        return out


def obstacle_shapes(sim_or_layout: Any) -> list[Shape]:
    """布局里的树和障碍杆 (规划时当作障碍)。"""
    layout = sim_or_layout.layout if isinstance(sim_or_layout, Simulator) else sim_or_layout
    return layout.obstacle_shapes()


__all__ = ["SimConfig", "SimHubPort", "SimObject", "SimObjectSource", "SimPoseSource",
           "Simulator", "obstacle_shapes"]
