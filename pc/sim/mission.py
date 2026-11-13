"""
在模拟器里跑一整局: 把布局里的物体依次送到各自的目标, 最后回起终点区 (navigation-grasp 任务 6.1)。

    result = await run_mission(field, layout, SimConfig(distance_error=0.05, ...))
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from pc.field.layout import Layout
from pc.field.model import FieldModel
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.controller import NavConfig, Navigator
from pc.navigation.grasp import Manipulator
from pc.sim.simulator import SimConfig, Simulator
from pc.world.state import WorldState

# 送物体的顺序: 先把 D 区上方方格里的服务机器人送走 (送进 D 区的包裹会挡住抓它们时的笼区),
# 再送人 (红色小人在 D 区左边), 最后送包裹
DEFAULT_ORDER = ("green_robot", "red_robot", "blue_robot", "yellow_robot",
                 "yellow_person", "red_person", "green_person", "blue_person",
                 "package_1", "package_2")


@dataclass
class ObjectOutcome:
    id: str
    status: str
    reason: str
    target: str
    sim_time_s: float  # 这个物体花的模拟时间


@dataclass
class MissionResult:
    layout: str
    outcomes: list[ObjectOutcome] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)  # 结束时真的在目标上的物体
    parked: bool = False
    total_s: float = 0.0
    knocked: list[str] = field(default_factory=list)  # 被碰到的树 / 障碍杆
    commands: int = 0
    sim: Simulator | None = None

    @property
    def success_rate(self) -> float:
        return len(self.delivered) / len(self.outcomes) if self.outcomes else 0.0


def build(field: FieldModel, layout: Layout, config: SimConfig,
          geom: RobotGeometry | None = None, nav_config: NavConfig | None = None,
          log: Callable[[str], None] = lambda _m: None):
    geom = geom or RobotGeometry.load()
    sim = Simulator(field, layout, geom, config)
    world = WorldState(field, sim.clock)
    nav = Navigator(field, sim.hub, sim.poses, geom, nav_config or NavConfig(),
                    static_obstacles=layout.obstacle_shapes(), on_hub_result=world.update_hub,
                    log=log)
    manip = Manipulator(field, nav, world, sim.detector, geom,
                        static_obstacles=layout.obstacle_shapes(), log=log)
    return sim, world, nav, manip


async def run_mission(
    field: FieldModel, layout: Layout, config: SimConfig,
    order: tuple[str, ...] = DEFAULT_ORDER, log: Callable[[str], None] = lambda _m: None,
    on_step: Callable[[Simulator], None] | None = None,
) -> MissionResult:
    sim, world, _nav, manip = build(field, layout, config, log=log)
    await manip.refresh()
    result = MissionResult(layout.name, sim=sim)
    ids = [o for o in order if o in layout.objects] + \
          [o for o in layout.objects if o not in order]
    for oid in ids:
        t0 = sim.time
        target = world.targets[oid]
        r = await manip.move_object(oid)
        result.outcomes.append(ObjectOutcome(oid, r.status, r.reason, target, sim.time - t0))
        log(f"{oid} → {target}: {r.status} {r.reason}")
        if world.holding is not None:
            # 没放下就失败了: 放回原地附近 (抬起夹爪, 后退), 继续下一个
            await manip.nav.command("gripper_up")
            world.mark_released(world.holding)
            await manip.nav.drive(-10)
        if on_step is not None:
            on_step(sim)
    r = await manip.park()
    result.parked = r.ok
    for oid in ids:
        o = sim.objects[oid]
        if field.target(world.targets[oid]).touches(o.xy, o.radius) and o.state != "captured":
            result.delivered.append(oid)
    result.total_s = sim.time
    result.knocked = sim.knocked()
    result.commands = len(sim.commands)
    return result


__all__ = ["DEFAULT_ORDER", "MissionResult", "ObjectOutcome", "build", "run_mission"]
