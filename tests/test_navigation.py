"""
导航 + 抓取 (改动 navigation-grasp): 接口和适配器、模拟器、世界状态、路径规划、go_to、
失败处理、停止、抓取和放下。全部在模拟器里运行, 不需要摄像头和小车。
"""

import asyncio
import dataclasses
import unittest
from types import SimpleNamespace

from pc.field.geometry import Pose, Shape, dist, heading_diff
from pc.field.layout import load_layout, parse_layout
from pc.field.model import load_field
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.controller import NavConfig, Navigator
from pc.navigation.planner import plan_route
from pc.navigation.ports import (
    HubResult,
    ObjectReading,
    PoseReading,
    SpikeHubPort,
    VisionObjectSource,
    VisionPoseSource,
)
from pc.sim.mission import build
from pc.sim.simulator import SimConfig, Simulator
from pc.world.state import WorldState

FIELD = load_field()
GEOM = RobotGeometry.load()
NOISY = dict(distance_error=0.05, turn_error=0.05, pose_noise_cm=0.3, heading_noise_deg=1.0,
             object_noise_cm=0.3)


def empty_layout():
    return parse_layout({"objects": {}}, FIELD)


def quiet(_msg):
    pass


# ---------------------------------------------------------------- 1.1 接口和适配器

class FakeReply(SimpleNamespace):
    @property
    def gripper(self):
        return (self.gl, self.gr)


class FakeSpikeHub:
    def __init__(self, reply=None, fail=False):
        self.sent = []
        self.reply = reply
        self.fail = fail
        self.stops = 0

    async def send(self, cmd):
        if self.fail:
            raise RuntimeError("BLE 断开")
        self.sent.append(cmd)

    async def wait_done(self):
        return self.reply

    async def send_stop(self):
        self.stops += 1
        return True


class AdapterTest(unittest.IsolatedAsyncioTestCase):
    async def test_spikehub_port_done(self):
        hub = FakeSpikeHub(FakeReply(kind="DONE", gl=90, gr=90, reason_text=""))
        r = await SpikeHubPort(hub).run("forward 30")
        self.assertEqual(hub.sent, ["forward 30"])
        self.assertTrue(r.ok)
        self.assertEqual(r.gripper, (90, 90))

    async def test_spikehub_port_stopped_timeout_and_error(self):
        r = await SpikeHubPort(FakeSpikeHub(FakeReply(kind="STOPPED", gl=0, gr=0,
                                                      reason_text=""))).run("forward 30")
        self.assertEqual(r.kind, "STOPPED")
        r = await SpikeHubPort(FakeSpikeHub(None)).run("forward 30")
        self.assertEqual(r.kind, "TIMEOUT")
        r = await SpikeHubPort(FakeSpikeHub(fail=True)).run("forward 30")
        self.assertEqual(r.kind, "TIMEOUT")
        self.assertIn("BLE 断开", r.reason)

    async def test_spikehub_port_stop(self):
        hub = FakeSpikeHub()
        await SpikeHubPort(hub).stop()
        self.assertEqual(hub.stops, 1)

    async def test_vision_pose_source(self):
        frames = [SimpleNamespace(found=True, x=10, y=20, heading=370, timestamp=1.0),
                  SimpleNamespace(found=False, timestamp=2.0)]
        loc = SimpleNamespace(latest=lambda: frames.pop(0))
        src = VisionPoseSource(loc, clock=lambda: 3.0)
        r1 = await src.read_pose()
        self.assertEqual(r1.pose, Pose(10, 20, 10))
        r2 = await src.read_pose()
        self.assertFalse(r2.visible)
        self.assertEqual(r2.last_pose, Pose(10, 20, 10))
        self.assertEqual(r2.timestamp, 2.0)

    async def test_vision_object_source(self):
        tracker = SimpleNamespace(latest_objects=lambda: [
            SimpleNamespace(id="red_robot", x=1, y=2, visible=True, timestamp=5.0)])
        out = await VisionObjectSource(tracker).read_objects()
        self.assertEqual(out["red_robot"], ObjectReading("red_robot", (1.0, 2.0), True, 5.0))


# ---------------------------------------------------------------- 1.2 模拟器

class SimulatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_motion_without_error(self):
        sim = Simulator(FIELD, empty_layout(), robot=Pose(100, 60, 0))
        await sim.hub.run("forward 30")
        self.assertAlmostEqual(sim.robot.x, 130.0)
        await sim.hub.run("right 90")
        self.assertAlmostEqual(sim.robot.heading, 90.0)
        await sim.hub.run("backward 10")
        self.assertAlmostEqual(sim.robot.y, 50.0)

    async def test_distance_error_range(self):
        for seed in range(20):
            sim = Simulator(FIELD, empty_layout(), config=SimConfig(distance_error=0.05, seed=seed),
                            robot=Pose(100, 60, 0))
            await sim.hub.run("forward 30")
            self.assertTrue(128.5 <= sim.robot.x <= 131.5, sim.robot.x)

    async def test_object_in_cage_moves_with_robot(self):
        layout = parse_layout({"objects": {"red_robot": [135, 60]}}, FIELD)
        sim = Simulator(FIELD, layout, robot=Pose(120, 60, 0))
        await sim.hub.run("gripper_down")
        self.assertEqual(sim.captured, ["red_robot"])
        await sim.hub.run("forward 20")
        self.assertAlmostEqual(sim.objects["red_robot"].x, 155.0)
        await sim.hub.run("gripper_up")
        self.assertEqual(sim.captured, [])

    async def test_object_on_cage_edge_is_knocked(self):
        # 物体一半在笼区外 (左右偏 6.5 cm): 放下夹爪时被碰倒, 位置改变
        layout = parse_layout({"objects": {"red_robot": [135, 66.5]}}, FIELD)
        sim = Simulator(FIELD, layout, robot=Pose(120, 60, 0))
        await sim.hub.run("gripper_down")
        o = sim.objects["red_robot"]
        self.assertEqual(o.state, "moved")
        self.assertNotEqual((o.x, o.y), (135, 66.5))
        self.assertEqual(sim.captured, [])

    async def test_same_seed_same_result(self):
        async def run(seed):
            sim = Simulator(FIELD, load_layout("starter2022", FIELD),
                            config=SimConfig(seed=seed, **NOISY))
            for cmd in ("forward 20", "left 45", "forward 30", "gripper_down", "right 30"):
                await sim.hub.run(cmd)
            readings = await sim.detector.read_objects()
            return sim.trace, {k: v.position for k, v in readings.items()}
        a, b, c = await run(3), await run(3), await run(4)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    async def test_stop_interrupts_motion(self):
        sim = Simulator(FIELD, empty_layout(), config=SimConfig(time_scale=0.01),
                        robot=Pose(100, 60, 0))
        task = asyncio.ensure_future(sim.hub.run("forward 50"))
        await asyncio.sleep(0.02)
        await sim.hub.stop()
        r = await task
        self.assertEqual(r.kind, "STOPPED")
        self.assertLess(sim.robot.x, 150)

    async def test_wall_blocks_motion(self):
        sim = Simulator(FIELD, empty_layout(), robot=Pose(200, 60, 0))
        await sim.hub.run("forward 50")
        self.assertLess(sim.robot.x, 236.2 - GEOM.raised_rect[1] + 0.5)

    async def test_unknown_command(self):
        sim = Simulator(FIELD, empty_layout())
        self.assertEqual((await sim.hub.run("dance")).kind, "ERR")


# ---------------------------------------------------------------- 2.x 世界状态

class WorldStateTest(unittest.IsolatedAsyncioTestCase):
    def make(self):
        t = {"now": 10.0}
        return WorldState(FIELD, lambda: t["now"]), t

    async def test_snapshot_and_summary(self):
        world, _t = self.make()
        world.update_pose(PoseReading(Pose(50.4, 60.6, 90.2), 9.5))
        world.update_objects({
            "red_robot": ObjectReading("red_robot", (112.2, 98.5), True, 9.5),
            "blue_person": ObjectReading("blue_person", (225.0, 96.0), True, 9.5),
        })
        world.update_hub(HubResult("DONE", "gripper_up", (90, 90)))
        snap = world.snapshot()
        self.assertEqual(snap.robot, Pose(50.4, 60.6, 90.2))
        self.assertAlmostEqual(snap.robot_age_s, 0.5)
        self.assertEqual(snap.gripper, "up")
        self.assertIsNone(snap.holding)
        self.assertEqual(snap.object("red_robot").state, "at_start")
        s = snap.summary()
        self.assertEqual(s["robot"], {"visible": True, "x": 50, "y": 61, "heading": 90})
        self.assertEqual(s["gripper"], "抬起")
        red = next(o for o in s["objects"] if o["id"] == "red_robot")
        self.assertEqual((red["x"], red["y"], red["state"], red["target"]),
                         (112, 98, "在起点", "circle:red"))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.robot = None  # 快照不可变

    async def test_object_states(self):
        world, _ = self.make()
        world.update_objects({
            "red_person": ObjectReading("red_person", (60, 108), True, 1),
            "yellow_person": ObjectReading("yellow_person", (43.5, 8.5), True, 1),
            "blue_person": ObjectReading("blue_person", (225, 96), True, 1),
        })
        # 开局没检测到 package_2: 丢失
        self.assertEqual(world.object_state("package_2"), "missing")
        # 部分接触 B 区 (B 区下边界 y=14, 物体半径 2.5, 圆心在边界外 2 cm): 已送达
        world.update_objects({
            "red_person": ObjectReading("red_person", (170, 16.0), True, 2),
            "yellow_person": ObjectReading("yellow_person", (120, 50), True, 2),
            "blue_person": ObjectReading("blue_person", (225, 96), False, 2),
        })
        self.assertEqual(world.object_state("red_person"), "delivered")
        # 完全在外面: 被移动
        self.assertEqual(world.object_state("yellow_person"), "moved")
        self.assertEqual(world.object_state("blue_person"), "occluded")
        # 完全在 B 区外、但离得很近 (不接触)
        world.update_objects({"red_person": ObjectReading("red_person", (170, 17.0), True, 3)})
        self.assertEqual(world.object_state("red_person"), "moved")
        world.mark_captured("yellow_person")
        self.assertEqual(world.object_state("yellow_person"), "captured")

    async def test_gripper_only_from_hub(self):
        world, _ = self.make()
        self.assertEqual(world.snapshot().gripper, "unknown")
        world.update_hub(HubResult("DONE", "forward 10", None))
        self.assertEqual(world.snapshot().gripper, "unknown")
        world.update_hub(HubResult("DONE", "gripper_down", (0, 0)))
        self.assertEqual(world.snapshot().gripper, "down")
        world.update_hub(HubResult("DONE", "gripper_left_up", (90, 0)))
        self.assertEqual(world.snapshot().gripper, "mixed")


# ---------------------------------------------------------------- 4.1 规划

class PlannerTest(unittest.TestCase):
    def test_route_along_roads(self):
        start = FIELD.park.xy
        goal = FIELD.target("zone:B").entries[0].xy
        r = plan_route(FIELD, start, goal, [], 9.5, 12.8)
        self.assertTrue(r.ok)
        self.assertEqual(r.points[0], start)
        self.assertEqual(r.points[-1], goal)
        self.assertTrue(r.nodes)  # 经过道路节点 (沿黑线)

    def test_keepout_is_avoided(self):
        field = FIELD.with_keepouts({"barrier": True})
        start, goal = (25, 48), (70, 48)  # 最短路线会穿过障碍杆
        free = plan_route(FIELD, start, goal, [], 9.5)
        self.assertTrue(free.ok)
        keepouts = [Shape(k.id, center=k.center, radius=k.radius, polygon=k.polygon)
                    for k in field.enabled_keepouts()]
        r = plan_route(field, start, goal, keepouts, 9.5)
        self.assertTrue(r.ok)
        for a, b in zip(r.points, r.points[1:], strict=False):
            self.assertGreaterEqual(keepouts[0].distance_to_segment(a, b), 9.5 - 0.3)

    def test_no_route(self):
        goal = (120, 55)
        ring = [Shape(f"k{i}", polygon=poly) for i, poly in enumerate([
            [(95, 30), (145, 30), (145, 33), (95, 33)], [(95, 77), (145, 77), (145, 80), (95, 80)],
            [(95, 30), (98, 30), (98, 80), (95, 80)], [(142, 30), (145, 30), (145, 80), (142, 80)],
        ])]
        r = plan_route(FIELD, FIELD.park.xy, goal, ring, 9.5)
        self.assertFalse(r.ok)
        self.assertIn("找不到路线", r.reason)


# ---------------------------------------------------------------- 4.2–4.4 go_to

def navigator(sim, config=None, **kw):
    return Navigator(FIELD, sim.hub, sim.poses, GEOM, config or NavConfig(), log=quiet, **kw)


class GoToTest(unittest.IsolatedAsyncioTestCase):
    async def test_reaches_zone_entries_with_5_percent_error(self):
        for zone in "ABCD":
            for seed in range(3):
                with self.subTest(zone=zone, seed=seed):
                    sim = Simulator(FIELD, empty_layout(), config=SimConfig(seed=seed, **NOISY))
                    nav = navigator(sim)
                    entry = FIELD.target(f"zone:{zone}").entries[0]
                    r = await nav.go_to(entry.xy, entry.heading)
                    self.assertTrue(r.ok, r)
                    self.assertLessEqual(dist(sim.robot.xy, entry.xy), 2.0)
                    self.assertLessEqual(abs(heading_diff(entry.heading, sim.robot.heading)), 5.5)

    async def test_no_progress_when_blocked(self):
        sim = Simulator(FIELD, empty_layout())
        sim.blocked = True  # 被手挡住
        r = await navigator(sim).go_to(FIELD.target("zone:B").entries[0].xy)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.reason, "没有进展")

    async def test_pause_while_pose_invisible_then_resume(self):
        sim = Simulator(FIELD, empty_layout(), robot=Pose(60, 60, 0))
        sim.occlusions.append((2.0, 5.0))  # 第 2 到 5 秒 (模拟时间) 看不到 marker
        nav = navigator(sim)
        sent_during = []
        orig = sim.execute

        async def execute(cmd):
            if 2.0 <= sim.time < 5.0:
                sent_during.append(cmd)
            return await orig(cmd)
        sim.execute = execute
        r = await nav.go_to((150, 60), direct=True)
        self.assertTrue(r.ok, r)
        self.assertEqual(sent_during, [])
        self.assertEqual(nav.pauses, 1)

    async def test_pose_invisible_too_long_fails(self):
        sim = Simulator(FIELD, empty_layout(), robot=Pose(60, 60, 0))
        sim.occlusions.append((1.0, 100.0))
        r = await navigator(sim).go_to((150, 60), direct=True)
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.reason, "位姿不可见")
        self.assertLess(sim.time, 20)

    async def test_never_leaves_field(self):
        sim = Simulator(FIELD, empty_layout(), robot=Pose(200, 60, 0))
        nav = navigator(sim)
        r = await nav.go_to((236, 60), direct=True)  # 目标点贴着墙: 车头会越界
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.reason, "会越出场地")
        foot = GEOM.footprint_polygon(sim.robot, False)
        self.assertTrue(all(p[0] <= FIELD.width for p in foot))

    async def test_stop_aborts_go_to(self):
        sim = Simulator(FIELD, empty_layout(), config=SimConfig(time_scale=0.005))
        nav = navigator(sim)
        task = asyncio.ensure_future(nav.go_to(FIELD.target("zone:B").entries[0].xy))
        await asyncio.sleep(0.05)
        await nav.request_stop()
        r = await task
        self.assertEqual(r.status, "stopped")
        moved = dist(sim.robot.xy, FIELD.park.xy)
        await asyncio.sleep(0.05)
        self.assertEqual(dist(sim.robot.xy, FIELD.park.xy), moved)  # 停下后不再动


# ---------------------------------------------------------------- 5.x 抓取和放下

class GraspTest(unittest.IsolatedAsyncioTestCase):
    async def pick(self, oid, layout=None, config=None):
        layout = layout or load_layout("starter2022", FIELD)
        sim, world, _nav, manip = build(FIELD, layout, config or SimConfig(), GEOM)
        await manip.refresh()
        r = await manip.pick(oid)
        return r, sim, world, manip

    async def test_pick_each_object_type(self):
        for oid in ("red_person", "blue_person", "package_2", "red_robot"):
            with self.subTest(object=oid):
                r, sim, world, _ = await self.pick(oid)
                self.assertTrue(r.ok, r)
                self.assertEqual(sim.captured, [oid])
                self.assertEqual(world.holding, oid)
                self.assertEqual(sim.knocked(), [])

    async def test_package_approached_from_inside_along_the_branch(self):
        r, _sim, _world, manip = await self.pick("package_2")
        self.assertTrue(r.ok, r)
        plan = manip.last_plan
        self.assertEqual(plan.heading, 180)
        self.assertGreater(plan.pregrasp.x, plan.grasp.x)

    async def test_offset_object_is_not_grasped_before_realigning(self):
        # 物体比预计的位置偏了 3 cm (感知还没更新): 先不放夹爪, 后退重新对准
        layout = parse_layout({"objects": {"red_robot": [150, 60]}}, FIELD)
        sim, _world, _nav, manip = build(FIELD, layout, SimConfig(), GEOM)
        await manip.refresh()
        sim.objects["red_robot"].y += 3.0  # 真实位置偏了, 但第一次规划用的是旧位置
        downs = []
        orig = sim.execute

        async def execute(cmd):
            if cmd == "gripper_down":
                o = sim.objects["red_robot"]
                downs.append(abs(sim.robot.to_local(o.xy)[1]))
            return await orig(cmd)
        sim.execute = execute
        r = await manip.pick("red_robot")
        self.assertTrue(r.ok, r)
        # 放下夹爪时物体都在笼区容差内
        self.assertTrue(all(d <= GEOM.cage_tolerance(3.0) for d in downs), downs)

    async def test_three_failures(self):
        layout = parse_layout({"objects": {"red_robot": [150, 60]}}, FIELD)
        sim, world, _nav, manip = build(FIELD, layout, SimConfig(), GEOM)
        await manip.refresh()
        orig = sim._set_gripper

        def slippery(down):  # 夹爪放下时物体总是滑出去
            orig(down)
            for o in sim.objects.values():
                if o.state == "captured":
                    o.state, o.local = "moved", None
        sim._set_gripper = slippery
        r = await manip.pick("red_robot")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.reason, "抓取失败")
        self.assertEqual(r.details["attempts"], 3)
        self.assertFalse(sim.gripper_down)
        self.assertIsNone(world.holding)

    async def test_place_into_zone_and_circle(self):
        for oid, target in (("red_person", "zone:B"), ("red_robot", "circle:red")):
            with self.subTest(object=oid):
                r, sim, world, manip = await self.pick(oid)
                self.assertTrue(r.ok, r)
                r = await manip.place()
                self.assertTrue(r.ok, r)
                o = sim.objects[oid]
                self.assertTrue(FIELD.target(target).touches(o.xy, o.radius))
                self.assertEqual(world.object_state(oid), "delivered")
                self.assertIsNone(world.holding)
                # 放下后小车后退离开
                self.assertFalse(sim.gripper_down)
                self.assertGreater(dist(GEOM.cage_center(sim.robot), o.xy), 8)

    async def test_only_one_object_at_a_time(self):
        r, _sim, _world, manip = await self.pick("red_person")
        self.assertTrue(r.ok)
        r = await manip.pick("green_person")
        self.assertEqual(r.status, "failed")
        self.assertEqual(r.reason, "已经夹着 red_person")

    async def test_stop_during_pick(self):
        layout = load_layout("starter2022", FIELD)
        _sim, _world, nav, manip = build(FIELD, layout, SimConfig(time_scale=0.005), GEOM)
        await manip.refresh()
        task = asyncio.ensure_future(manip.pick("red_robot"))
        await asyncio.sleep(0.05)
        await nav.request_stop()
        r = await task
        self.assertEqual(r.status, "stopped")


if __name__ == "__main__":
    unittest.main()
