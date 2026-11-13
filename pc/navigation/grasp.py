"""
笼式抓取和放下 (改动 navigation-grasp 设计 D5、D6, 任务 5.1–5.4)。

抓取 pick(物体):
  前往预抓取点 (接近方向上、物体前方约 15 cm) → 抬起夹爪、车头对准物体 → 分步接近
  (每步最多 5 cm, 同时修正左右偏差) → 物体在笼区容差内才放下夹爪 (否则后退、重新对准) →
  后退 3 cm 验证物体有没有跟着走 → 成功标记"被夹住"; 失败就抬起夹爪、后退、重试, 最多 3 次。
  最后几厘米物体可能被车身挡住: 用最后一次看到的位置 (物体自己不会动) 加上小车当前位姿推算。
放下 place(目标):
  前往目标的进入点 → 直线前进到笼区中心进入目标 3 cm 以上 → 抬起夹爪 → 后退 10 cm →
  确认物体"已送达" (和目标部分重叠也算)。
一次只夹一个物体。所有动作都返回 NavResult (ok / failed / stopped + 原因)。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from pc.field.approach import GraspPlan, choose_approach, keepout_shapes
from pc.field.geometry import Point, Pose, Shape, dist, heading_diff, heading_to
from pc.field.model import FieldModel, Target
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.controller import FAILED, OK, Navigator, NavResult
from pc.navigation.planner import plan_route
from pc.navigation.ports import ObjectSource
from pc.world.state import WorldState

MAX_ATTEMPTS = 3
MAX_REALIGN = 2
APPROACH_STEP = 5.0
VERIFY_BACK = 3.0
VERIFY_MORE = 5.0
RETREAT = 10.0
PLACE_DEPTH = 3.0  # 笼区中心进入目标至少多深 (设计 D6)
PLACE_EXTRA = 2.0  # 再往里多走一点 (区域够大时)
LEAD_IN = 12.0  # 预抓取点前的引导距离: 在那里对准方向, 再直线开到预抓取点
PLACE_CLEARANCE = 2.0  # 放下时车 (夹爪放下) 离旁边的物体至少多远
NAMED_POSITION_RADIUS = 2.0  # 物体离命名位置点这么近, 就用那个位置点的接近方向


class Manipulator:
    def __init__(
        self,
        field: FieldModel,
        nav: Navigator,
        world: WorldState,
        objects: ObjectSource,
        geom: RobotGeometry | None = None,
        static_obstacles: Sequence[Shape] = (),
        log: Callable[[str], None] = print,
    ) -> None:
        self.field = field
        self.nav = nav
        self.world = world
        self.objects = objects
        self.geom = geom or nav.geom
        self.static_obstacles = list(static_obstacles) + keepout_shapes(field)
        self.log = log
        self.last_plan: GraspPlan | None = None

    # ---------------- 公开动作 ----------------

    async def refresh(self):
        return await self.world.refresh(self.nav.poses, self.objects)

    async def pick(self, oid: str) -> NavResult:
        if oid not in self.field.objects:
            return NavResult(FAILED, f"没有这个物体：{oid}")
        holding = self.world.holding
        if holding is not None:
            return NavResult(FAILED, f"已经夹着 {holding}", {"rejected": True})
        snap = await self.refresh()
        if snap.object(oid).position is None:
            return NavResult(FAILED, f"看不到 {oid}")
        radius = self.field.objects[oid].radius_cm
        # 先抬起夹爪再走: 车更短, 转弯需要的地方小 (导航也就知道了夹爪状态)
        r = await self.nav.command("gripper_up")
        if not r.ok:
            return r

        for attempt in range(1, MAX_ATTEMPTS + 1):
            snap = await self.refresh()
            pos = snap.object(oid).position
            assert pos is not None
            plan = self._plan(oid, pos, radius)
            if plan is None:
                return NavResult(FAILED, f"物体 {oid} 无法接近")
            self.last_plan = plan
            self.nav.set_obstacles(self._others(oid))
            self.log(f"[抓取] {oid} 第 {attempt} 次：从 {plan.heading:.0f}° 方向接近")
            # 去预抓取点的路上, 要抓的物体也是障碍 (转弯时不能扫到它)
            target_shape = Shape(oid, center=pos, radius=radius)
            r = await self.nav.go_to(plan.pregrasp.xy, plan.heading,
                                     obstacles=[*self._others(oid), target_shape],
                                     lead_in=LEAD_IN)
            if not r.ok:
                return _phase(r, "前往预抓取点")
            r = await self.nav.command("gripper_up")
            if not r.ok:
                return r

            self.nav.set_obstacles(self._others(oid))
            outcome = await self._approach(oid, pos, plan, radius)
            if isinstance(outcome, NavResult):
                return _phase(outcome, "接近物体")
            if outcome == "misaligned":
                self.log(f"[抓取] {oid} 不在笼区容差内，不放夹爪，后退重新对准")
                r = await self.nav.drive(-RETREAT)
                if not r.ok and r.status != FAILED:
                    return r
                continue

            r = await self.nav.command("gripper_down")
            if not r.ok:
                return r
            verdict = await self._verify(oid, pos)
            if isinstance(verdict, NavResult):
                return verdict
            if verdict:
                self.world.mark_captured(oid)
                self.log(f"[抓取] {oid} 已夹住")
                return NavResult(OK, details={"attempts": attempt})
            self.log(f"[抓取] {oid} 没有跟着小车移动，抬起夹爪重试")
            for cmd_result in (await self.nav.command("gripper_up"),
                               await self.nav.drive(-RETREAT)):
                if cmd_result.status == "stopped":
                    return cmd_result

        return NavResult(FAILED, "抓取失败", {"attempts": MAX_ATTEMPTS})

    async def place(self, target_id: str | None = None) -> NavResult:
        oid = self.world.holding
        if oid is None:
            return NavResult(FAILED, "没有夹着物体")
        target = self.field.target(target_id or self.world.targets[oid])
        if target_id is not None:
            self.world.set_target(oid, target_id)
        snap = await self.refresh()
        robot = snap.robot
        assert robot is not None
        others = self._others(oid)

        entries = sorted(target.entries, key=lambda e: self._route_length(robot.xy, e.xy, others))
        last_reason = "没有可用的进入点"
        for entry0 in entries:
            entry, final = self._clear_place(entry0, target, others)
            if final is None:
                continue
            self.log(f"[放下] {oid} → {target.label}")
            r = await self.nav.go_to(entry.xy, entry.heading, obstacles=others)
            if not r.ok:
                if r.status == "stopped":
                    return r
                last_reason = r.reason
                continue
            r = await self.nav.go_to(final.xy, final.heading, direct=True)
            if not r.ok:
                return _phase(r, "进入目标")
            r = await self.nav.command("gripper_up")
            if not r.ok:
                return r
            self.world.mark_released(oid)
            r = await self.nav.drive(-RETREAT)
            if r.status == "stopped":
                return r
            await self.refresh()
            if self.world.object_state(oid) == "occluded":
                await self.nav.drive(-VERIFY_MORE)
                await self.refresh()
            state = self.world.object_state(oid)
            if state == "delivered":
                self.log(f"[放下] {oid} 已送达 {target.label}")
                return NavResult(OK, details={"target": target.id})
            return NavResult(FAILED, "没有送达", {"state": state, "target": target.id})
        return NavResult(FAILED, f"到不了 {target.label}（{last_reason}）")


    async def move_object(self, oid: str, target_id: str | None = None) -> NavResult:
        """组合动作: 抓取, 再放到目标 (默认用目录里的目标)。"""
        r = await self.pick(oid)
        if not r.ok:
            return r
        return await self.place(target_id)

    async def park(self) -> NavResult:
        """回起终点区: 先到进入点 (车头朝外), 再倒车停进去。"""
        await self.refresh()
        entry = self.field.start_entries[0]
        r = await self.nav.go_to(entry.xy, entry.heading, obstacles=self._others(None))
        if not r.ok:
            return _phase(r, "前往起终点区")
        park = self.field.park
        return await self.nav.go_to(park.xy, park.heading, direct=True, reverse=True,
                                    tolerance=3.0)

    # ---------------- 内部 ----------------

    def _others(self, oid: str | None) -> list[Shape]:
        """规划时要避开的东西: 其他物体 (不含被夹住的和要抓的) + 静态障碍。"""
        snap = self.world.snapshot()
        shapes = [
            Shape(o.id, center=o.position, radius=self.field.objects[o.id].radius_cm)
            for o in snap.objects
            if o.id != oid and o.position is not None and o.state != "captured"
        ]
        return shapes + self.static_obstacles

    def _plan(self, oid: str, pos: Point, radius: float) -> GraspPlan | None:
        hint = None
        for p in self.field.positions.values():
            if p.approach is not None and dist(p.xy, pos) <= NAMED_POSITION_RADIUS:
                hint = p.approach
                break
        others = self._others(oid)
        plan = None
        if hint is not None:
            plan = choose_approach(pos, radius, self.geom, self.field.width, self.field.height,
                                   others, hint)
        if plan is None:
            plan = choose_approach(pos, radius, self.geom, self.field.width, self.field.height,
                                   others)
        return plan

    async def _object_estimate(self, oid: str, fallback: Point) -> Point:
        readings = await self.objects.read_objects()
        r = readings.get(oid)
        if r is not None:
            return r.position  # 看不见时是最后一次看到的位置
        return fallback

    async def _approach(self, oid: str, start_pos: Point, plan: GraspPlan, radius: float):
        """分步接近。返回 "ready" / "misaligned" / NavResult (被停止或失败)。"""
        tol = self.geom.cage_tolerance(radius)
        x0, x1, _y0, _y1 = self.geom.cage_inner_rect
        est = start_pos
        realigned = 0
        for _ in range(16):
            pose = await self.nav.current_pose()
            est = await self._object_estimate(oid, est)
            lx, ly = pose.to_local(est)
            remaining = lx - plan.depth
            if abs(ly) > tol or lx < x0 + radius:
                if remaining > 6 and realigned < MAX_REALIGN:
                    realigned += 1
                    r = await self.nav.turn_to(heading_to(pose.xy, est))
                    if not r.ok:
                        return r
                    continue
                return "misaligned"
            if remaining <= 0.7:
                return "ready" if lx <= x1 - radius else "misaligned"
            # 走偏一点也修正 (不超过容差时只修大于 3° 的)
            err = heading_diff(heading_to(pose.xy, est), pose.heading)
            if abs(err) > 3 and remaining > 3:
                r = await self.nav.turn_to(heading_to(pose.xy, est))
                if not r.ok:
                    return r
            r = await self.nav.drive(min(remaining, APPROACH_STEP))
            if not r.ok:
                if r.status == FAILED and x0 + radius <= lx <= x1 - radius and abs(ly) <= tol:
                    return "ready"  # 贴着墙再往前会越界, 物体已经在笼区里了
                return r
        return "misaligned"

    async def _verify(self, oid: str, before: Point):
        """后退一点, 看物体有没有跟着走。返回 True / False / NavResult (被停止)。"""
        r = await self.nav.drive(-VERIFY_BACK)
        if r.status == "stopped":
            return r
        for extra in (0.0, VERIFY_MORE):
            if extra:
                r = await self.nav.drive(-extra)
                if r.status == "stopped":
                    return r
            readings = await self.objects.read_objects()
            reading = readings.get(oid)
            if reading is None or not reading.visible:
                continue
            pose = await self.nav.current_pose()
            if dist(reading.position, before) < 1.5:
                return False  # 留在原地
            lx, ly = pose.to_local(reading.position)
            x0, x1, y0, y1 = self.geom.cage_inner_rect
            return x0 - 1 <= lx <= x1 + 1 and y0 - 1 <= ly <= y1 + 1
        return False

    def _clear_place(self, entry: Pose, target: Target,
                     others: Sequence[Shape]) -> tuple[Pose, Pose | None]:
        """进入点和放下位姿; 放下位姿 (夹爪放下) 压到其他物体时, 把进入点左右挪一点再试。"""
        if target.kind == "circle":
            shifts = [0.0, 1.5, -1.5, 3.0, -3.0, 4.5, -4.5]
        else:
            # 区域里可能要放好几个物体: 从区域的一端开始依次排, 不要第一个就放在正中间
            shifts = [-21.0 + 1.5 * i for i in range(29)]
        for shift in shifts:
            moved = Pose(*entry.to_world(0.0, shift), entry.heading)
            final = self._place_pose(moved, target)
            if final is None:
                continue
            path = [moved.moved(d) for d in range(0, int(dist(moved.xy, final.xy)) + 1, 2)]
            path.append(final)
            if all(ob.distance_to_polygon(self.geom.footprint_polygon(p, True)) > PLACE_CLEARANCE
                   for p in path for ob in others):
                return moved, final
        return entry, None

    def _place_pose(self, entry: Pose, target: Target) -> Pose | None:
        """从进入点沿朝向往前, 笼区中心进入目标 PLACE_DEPTH 以上的位置。"""
        margin = PLACE_DEPTH if target.kind != "circle" else min(PLACE_DEPTH, target.radius - 1)
        s = 0.0
        first: float | None = None
        while s <= 80:
            pose = entry.moved(s)
            if target.contains(self.geom.cage_center(pose), margin):
                first = s
                break
            s += 0.5
        if first is None:
            return None
        best = entry.moved(first)
        if target.kind == "circle":
            # 圆圈: 尽量让笼区中心对准圆心
            cx, cy = target.anchor
            hx, hy = entry.moved(1).x - entry.x, entry.moved(1).y - entry.y
            ccx, ccy = self.geom.cage_center(entry)
            along = (cx - ccx) * hx + (cy - ccy) * hy
            return entry.moved(max(first, along))
        deeper = entry.moved(first + PLACE_EXTRA)
        if self.nav.footprint_ok(deeper) and target.contains(self.geom.cage_center(deeper), margin):
            return deeper
        return best if self.nav.footprint_ok(best) else None

    def _route_length(self, start: Point, goal: Point, obstacles: Sequence[Shape]) -> float:
        route = plan_route(self.field, start, goal, obstacles,
                           self.geom.half_width + self.nav.config.path_margin,
                           turn_room=self.nav.turn_room())
        return route.length if route.ok else float("inf")


def _phase(r: NavResult, phase: str) -> NavResult:
    """失败原因前面加上是哪一步失败的 (被停止的不加)。"""
    if r.status != FAILED:
        return r
    return NavResult(r.status, f"{phase}：{r.reason}", r.details)


__all__ = ["MAX_ATTEMPTS", "Manipulator"]
