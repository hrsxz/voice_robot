"""
闭环导航 (改动 navigation-grasp 设计 D4、D8, 任务 4.2–4.4)。

go_to 对每个路线点: 朝向误差超过 5° 就转向 → 前进有限的一段 (远处 30 cm, 离目标 10 cm 以内 5 cm)
→ Hub 回 DONE 后稍等, 重新测量位姿 → 直到误差在容差以内。最后一个点容差 2 cm; 指定了朝向时,
朝向误差不超过 5°。

失败处理 (都返回结构化结果 NavResult, 不抛异常):
- 没有进展: 连续 3 步位置几乎没变 (例如被手挡住) -> 停车, "没有进展"
- 位姿不可见: 不发新的动作 (暂停), 重新可见后继续; 超过 10 秒 -> "位姿不可见"
- 越界: 每一步发出之前预测终点, 车身会越出场地就缩短步长, 实在不行 -> 失败
- 停止: request_stop() 立即打断正在执行的动作, 结果为 "stopped"
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pc.field.geometry import (
    Point,
    Pose,
    Shape,
    dist,
    heading_diff,
    heading_to,
    polygon_outside_rect,
)
from pc.field.model import FieldModel
from pc.field.robot_geometry import RobotGeometry
from pc.navigation.planner import plan_route
from pc.navigation.ports import HubPort, HubResult, PoseSource

OK, FAILED, STOPPED = "ok", "failed", "stopped"
SKIP_DISTANCE = 10.0  # 中间路线点离得这么近但对不准时, 跳过它
LOOKAHEAD = 30.0  # 沿路线段走时, 瞄准投影点前方多远
TIGHT_MARGIN = 0.5  # 正常余量走不通时, 路线离障碍至少 车身半宽 + 0.5


@dataclass(frozen=True)
class NavResult:
    status: str  # ok / failed / stopped
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass
class NavConfig:
    step_far: float = 30.0
    step_near: float = 5.0
    near_distance: float = 10.0  # 离目标多近改用小步长
    heading_threshold: float = 5.0  # 朝向误差超过多少才转向
    final_tolerance: float = 1.5  # spec 要求 ≤ 2 cm; 留 0.5 cm 给测量噪声
    waypoint_tolerance: float = 2.0  # 中间路线点的容差 (底部横线离 D 区方格只有约 8 cm, 不能放太松)
    heading_tolerance: float = 5.0
    max_iterations: int = 8  # 每个路线点最多迭代几次
    no_progress_steps: int = 3
    progress_epsilon: float = 0.5  # 一步移动不到 0.5 cm 算没有进展
    settle_s: float = 0.1  # Hub 回 DONE 之后等多久再取位姿 (避免拿到旧画面)
    pose_pause_s: float = 1.0  # 位姿不可见超过多久算暂停 (只影响提示)
    pose_fail_s: float = 10.0  # 位姿不可见超过多久算失败
    poll_s: float = 0.1
    time_limit_s: float = 180.0
    edge_margin: float = 0.2  # 执行时车身离场地边界至少多远 (规划时用 0.5, 给测量噪声留余量)
    path_margin: float = 2.0  # 路线离障碍至少 车身半宽 + path_margin (给运动误差留余量)


class NavigationStopped(Exception):
    pass


class _Fail(Exception):
    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class Navigator:
    def __init__(
        self,
        field: FieldModel,
        hub: HubPort,
        poses: PoseSource,
        geom: RobotGeometry | None = None,
        config: NavConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        static_obstacles: Sequence[Shape] = (),
        on_hub_result: Callable[[HubResult], None] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.field = field
        self.hub = hub
        self.poses = poses
        self.geom = geom or RobotGeometry.load()
        self.config = config or NavConfig()
        # 模拟器提供自己的 sleep (推进模拟时钟); 实车用 asyncio.sleep
        self.sleep = sleep or getattr(poses, "sleep", None) or asyncio.sleep
        self.static_obstacles = list(static_obstacles)
        self.on_hub_result = on_hub_result
        self.log = log
        self._stop = False
        self.pose: Pose | None = None  # 最近一次测得的位姿
        # 夹爪状态决定车的外形 (抬起时笼区不往前伸); 不知道时按放下 (更长) 处理
        self.gripper_down: bool | None = None
        self.pauses = 0
        self.commands: list[str] = []
        self._obstacles: list[Shape] = list(self.static_obstacles)  # 转向时要避开的东西

    # ---------------- 停止 ----------------

    def request_stop(self) -> asyncio.Future:
        """立即中止: 打断正在执行的动作; 之后的 go_to 都返回 "被停止"。"""
        self._stop = True
        return asyncio.ensure_future(self.hub.stop())

    def clear_stop(self) -> None:
        self._stop = False

    @property
    def stopped(self) -> bool:
        return self._stop

    # ---------------- 公开动作 ----------------

    async def go_to(
        self, goal: Point, heading: float | None = None, tolerance: float | None = None,
        obstacles: Sequence[Shape] = (), direct: bool = False, reverse: bool = False,
        lead_in: float = 0.0,
    ) -> NavResult:
        """
        到达 goal (转弯中心), 可选朝向。direct=True 时不走道路网, 直线过去 (抓取和放下的最后一段)。
        obstacles: 除了静态障碍 (禁行区、树) 之外这次要避开的东西 (例如其他物体)。
        reverse=True: 倒车过去 (停进起终点区)。
        lead_in > 0 (需要 heading): 先到朝向线上离目标 lead_in 的一点 (目标后方或前方),
        在那里对准朝向, 再直线开进去 (前进或倒车)。目标旁边地方很小时用 (预抓取点)。
        """
        cfg = self.config
        tol = cfg.final_tolerance if tolerance is None else tolerance
        start_time = self.poses.clock()
        try:
            pose = await self._pose()
            all_obstacles = [*self.static_obstacles, *obstacles]
            self._obstacles = all_obstacles
            clearance = self.geom.half_width + cfg.path_margin
            lead = None
            if lead_in > 0 and heading is not None and not direct:
                lead = self._lead_point(pose, goal, heading, lead_in, all_obstacles, clearance)
            if direct:
                waypoints = [goal]
            else:
                route = plan_route(self.field, pose.xy, lead or goal, all_obstacles, clearance,
                                   turn_room=self.turn_room())
                if not route.ok:
                    # 留了余量就走不通 (物体很多): 用最小余量再试一次
                    route = plan_route(self.field, pose.xy, lead or goal, all_obstacles,
                                       self.geom.half_width + TIGHT_MARGIN,
                                       turn_room=self.turn_room())
                if not route.ok:
                    return NavResult(FAILED, route.reason)
                waypoints = list(route.points[1:])
                if lead is not None:
                    waypoints.append(goal)
            prev = pose.xy
            for i, wp in enumerate(waypoints):
                last = i == len(waypoints) - 1
                back = reverse
                if last and heading is not None and dist(prev, wp) > cfg.near_distance:
                    # 最后一段按最终朝向到达: 最终朝向和行进方向相反时倒车过去,
                    # 这样车头不会先伸到目标后面的障碍上
                    back = back or abs(heading_diff(heading, heading_to(prev, wp))) > 100
                if lead is not None and wp == lead:
                    # 在引导点对准朝向 (车头或车尾对着目标), 后面直线开进去
                    assert heading is not None
                    toward = heading_to(wp, goal)
                    same = abs(heading_diff(heading, toward)) < 90
                    want = toward if same else (toward + 180) % 360
                    for _ in range(3):
                        await self._reach(wp, cfg.waypoint_tolerance, start_time, True, back)
                        await self._face(want)
                        # 转向时可能先挪了一下 (贴墙时): 离开引导点太远就回去重新对准
                        if dist((await self._pose()).xy, wp) <= cfg.waypoint_tolerance + 1:
                            break
                    prev = wp
                    continue
                await self._reach(wp, tol if last else cfg.waypoint_tolerance, start_time, last,
                                  back, seg_start=prev if i > 0 else None)
                prev = wp
            if heading is not None:
                await self._face(heading)
            pose = await self._pose()
            return NavResult(OK, details={"error_cm": round(dist(pose.xy, goal), 2),
                                          "pose": pose, "pauses": self.pauses})
        except NavigationStopped:
            return NavResult(STOPPED, "被停止")
        except _Fail as f:
            await self._halt()
            return NavResult(FAILED, f.reason, f.details)

    async def turn_to(self, heading: float) -> NavResult:
        try:
            await self._face(heading)
            return NavResult(OK)
        except NavigationStopped:
            return NavResult(STOPPED, "被停止")
        except _Fail as f:
            return NavResult(FAILED, f.reason, f.details)

    async def drive(self, distance: float) -> NavResult:
        """直行 (负数后退) 一段, 不做闭环; 发出之前检查会不会越界。"""
        try:
            await self._check_stop()
            pose = await self._pose()
            d = self._limit_step(pose, distance)
            if abs(d) < 1:
                return NavResult(FAILED, "会越出场地", {"distance": distance})
            await self._run(("forward" if d > 0 else "backward") + f" {round(abs(d))}")
            return NavResult(OK, details={"distance": round(d)})
        except NavigationStopped:
            return NavResult(STOPPED, "被停止")
        except _Fail as f:
            return NavResult(FAILED, f.reason, f.details)

    async def command(self, cmd: str) -> NavResult:
        """发送一条非运动命令 (夹爪)。"""
        try:
            await self._check_stop()
            await self._run(cmd)
            return NavResult(OK)
        except NavigationStopped:
            return NavResult(STOPPED, "被停止")
        except _Fail as f:
            return NavResult(FAILED, f.reason, f.details)

    async def current_pose(self) -> Pose:
        return await self._pose()

    # ---------------- 内部 ----------------

    async def _check_stop(self) -> None:
        if self._stop:
            raise NavigationStopped()

    async def _run(self, cmd: str) -> HubResult:
        await self._check_stop()
        self.commands.append(cmd)
        result = await self.hub.run(cmd)
        if result.gripper is not None:
            self.gripper_down = min(result.gripper) < 45
        if self.on_hub_result is not None:
            self.on_hub_result(result)
        if result.kind == "STOPPED" or self._stop:
            raise NavigationStopped()
        if result.kind != "DONE":
            raise _Fail(f"Hub 没有完成 {cmd}：{result.reason or result.kind}")
        if self.config.settle_s > 0:
            await self.sleep(self.config.settle_s)
        return result

    async def _halt(self) -> None:
        try:
            await self.hub.stop()
        except Exception:
            pass

    async def _pose(self) -> Pose:
        """取一帧新的位姿; 看不见时暂停 (不发动作), 超过 pose_fail_s 失败。"""
        cfg = self.config
        reading = await self.poses.read_pose()
        if reading.visible:
            assert reading.pose is not None
            self.pose = reading.pose
            return reading.pose
        lost_at = self.poses.clock()
        paused = False
        while True:
            await self._check_stop()
            waited = self.poses.clock() - lost_at
            if waited >= cfg.pose_fail_s:
                raise _Fail("位姿不可见", seconds=round(waited, 1))
            if waited >= cfg.pose_pause_s and not paused:
                paused = True
                self.pauses += 1
                self.log("看不到小车的 marker，暂停导航")
            await self.sleep(cfg.poll_s)
            reading = await self.poses.read_pose()
            if reading.visible:
                assert reading.pose is not None
                if paused:
                    self.log("重新看到小车，继续导航")
                self.pose = reading.pose
                return reading.pose

    def footprint_ok(self, pose: Pose) -> bool:
        return self._footprint_ok(pose)

    def _footprint_ok(self, pose: Pose) -> bool:
        foot = self.geom.footprint_polygon(pose, self.gripper_down is not False)
        return polygon_outside_rect(foot, self.field.width, self.field.height,
                                    self.config.edge_margin) <= 1e-6

    def _limit_step(self, pose: Pose, distance: float) -> float:
        """越界检查: 预测终点, 会越出场地就缩短步长 (每次 1 cm)。"""
        d = distance
        sign = 1 if d >= 0 else -1
        while abs(d) >= 1:
            if self._footprint_ok(pose.moved(d)):
                return d
            d -= sign
        return 0.0

    def _lead_point(self, pose: Pose, goal: Point, heading: float, lead_in: float,
                    obstacles: Sequence[Shape], clearance: float) -> Point | None:
        """朝向线上的引导点: 优先目标后方 (前进开进去), 不行再用前方 (倒车开进去)。"""
        goal_pose = Pose(goal[0], goal[1], heading)
        for d in (-lead_in, lead_in):
            p = goal_pose.moved(d)
            if not self.field.inside(p.xy, tolerance=0.0):
                continue
            if any(ob.distance_to_point(p.xy) < clearance for ob in obstacles):
                continue
            if not self._footprint_ok(p) or not all(
                    ob.distance_to_polygon(self.geom.footprint_polygon(p, False)) > 0.5
                    for ob in obstacles):
                continue
            return p.xy
        return None

    def turn_room(self) -> float:
        """原地转向需要多大的空间 (夹爪放下时车更长)。"""
        return self.geom.sweep_radius(self.gripper_down is not False) + 0.3

    def set_obstacles(self, obstacles: Sequence[Shape]) -> None:
        """抓取 / 放下过程中转向要避开的东西 (其他物体); go_to 会自动设置。"""
        self._obstacles = [*self.static_obstacles, *obstacles]

    def _path_clear(self, pose: Pose, distance: float) -> bool:
        """直行这一段不会碰到障碍 (本来就贴着的障碍除外)。"""
        ignore = self._touching(pose)
        n = max(1, int(abs(distance) // 2))
        return all(self._clear_of_obstacles(pose.moved(distance * i / n), ignore)
                   for i in range(n + 1))

    def _touching(self, pose: Pose) -> set[str]:
        foot = self.geom.footprint_polygon(pose, self.gripper_down is not False)
        return {ob.id for ob in self._obstacles if ob.distance_to_polygon(foot) <= 0.2}

    def _clear_of_obstacles(self, pose: Pose, ignore: set[str] = frozenset()) -> bool:
        foot = self.geom.footprint_polygon(pose, self.gripper_down is not False)
        return all(ob.distance_to_polygon(foot) > 0.2
                   for ob in self._obstacles if ob.id not in ignore)

    def _turn_ok(self, pose: Pose, delta: float) -> bool:
        steps = max(1, int(abs(delta) // 10) + 1)
        # 本来就贴着的障碍 (测量误差) 不算, 其余的都要避开
        ignore = self._touching(pose)
        for i in range(steps + 1):
            p = pose.turned(delta * i / steps)
            if not self._footprint_ok(p):
                return False
            if i > 0 and not self._clear_of_obstacles(p, ignore):
                return False
        return True

    async def _turn(self, pose: Pose, delta: float) -> None:
        if round(abs(delta)) < 1:
            return
        if not self._turn_ok(pose, delta):
            # 原地转向会碰到墙: 换个方向转; 还不行就先直行 (前进或后退) 到能转的地方
            other = delta - 360 if delta > 0 else delta + 360
            if self._turn_ok(pose, other):
                delta = other
            else:
                pose, delta = await self._escape_for_turn(pose, delta)
        await self._run(("right" if delta > 0 else "left") + f" {round(abs(delta))}")

    async def _escape_for_turn(self, pose: Pose, delta: float) -> tuple[Pose, float]:
        """
        原地转不过来 (离墙或障碍太近): 像倒车入库一样, 用 "转 15° / 直行 4 cm" 的组合
        (广度优先搜索, 每一步都检查碰撞) 挪到一个能转过来的位置, 再转。
        """
        target = (pose.heading + delta) % 360
        plan = self._search_turn_escape(pose, target)
        if plan is None:
            raise _Fail("转向会越出场地或碰到障碍", pose=pose)
        for cmd in plan:
            await self._run(cmd)
        now = await self._pose()
        return now, heading_diff(target, now.heading)

    def _search_turn_escape(self, pose: Pose, target: float,
                            max_states: int = 6000) -> list[str] | None:
        from collections import deque

        actions = (("left 15", 0.0, -15.0), ("right 15", 0.0, 15.0),
                   ("forward 4", 4.0, 0.0), ("backward 4", -4.0, 0.0))

        def key(p: Pose) -> tuple[int, int, int]:
            return (round(p.x), round(p.y), round(p.heading / 5) % 72)

        queue = deque([(pose, [])])
        seen = {key(pose)}
        while queue and len(seen) < max_states:
            current, cmds = queue.popleft()
            if cmds and self._turn_ok(current, heading_diff(target, current.heading)):
                return cmds
            if len(cmds) >= 14:
                continue
            for cmd, move, turn in actions:
                if turn:
                    if not self._turn_ok(current, turn):
                        continue
                    nxt = current.turned(turn)
                else:
                    nxt = current.moved(move)
                    if not self._footprint_ok(nxt) or not self._path_clear(current, move):
                        continue
                k = key(nxt)
                if k in seen:
                    continue
                seen.add(k)
                queue.append((nxt, [*cmds, cmd]))
        return None

    async def _face(self, heading: float) -> None:
        for _ in range(4):
            pose = await self._pose()
            err = heading_diff(heading, pose.heading)
            if abs(err) <= self.config.heading_tolerance:
                return
            if abs(err) <= 2 * self.config.heading_tolerance and not self._turn_ok(pose, err):
                return  # 贴着墙或物体转不了, 差得不多就算对准 (后面的接近会再修正)
            await self._turn(pose, err)
        pose = await self._pose()
        if abs(heading_diff(heading, pose.heading)) > self.config.heading_tolerance:
            raise _Fail("朝向没有对准", heading=round(pose.heading, 1))

    async def _orient_for(
        self, pose: Pose, wp: Point, step: float, skippable: bool = False, reverse: bool = False,
    ) -> tuple[Pose, int] | None:
        """
        朝向路线点: 车头对着它前进, 或者车尾对着它后退。
        优先前进; 近处 (小步长) 的修正选转得少的那个; 前进不可行 (会撞墙) 时改用后退。
        返回转向后的预测位姿和方向 (1 前进 / -1 后退); skippable 并且两种都不可行时返回 None。
        """
        cfg = self.config
        options = []
        for direction in (1, -1):
            want = heading_to(pose.xy, wp) if direction == 1 else heading_to(wp, pose.xy)
            err = heading_diff(want, pose.heading)
            if abs(err) <= cfg.heading_threshold:
                err = 0.0
            turned = pose.turned(err)
            feasible = (err == 0.0 or self._turn_ok(pose, err)) and \
                abs(self._limit_step(turned, direction * step)) >= 1
            options.append((direction, err, turned, feasible))
        fwd, back = options
        near = step <= cfg.step_near
        if reverse and back[3]:
            choice = back
        elif fwd[3] and (not near or not back[3] or abs(fwd[1]) <= abs(back[1])):
            choice = fwd
        elif back[3]:
            choice = back
        elif skippable:
            return None
        else:
            choice = fwd  # 都不可行: 按前进处理 (_turn 会尝试绕出来)
        direction, err, turned, _ = choice
        if err != 0.0:
            await self._turn(pose, err)
            # 预测 (不重新测量); _turn 可能先绕了一下, 这时用新测的位置
            now = self.pose if self.pose is not None else pose
            turned = Pose(now.x, now.y, (pose.heading + err) % 360) if now is not pose else turned
        return turned, direction

    async def _reach(
        self, wp: Point, tol: float, start_time: float, final: bool = True, reverse: bool = False,
        seg_start: Point | None = None,
    ) -> None:
        """
        到达路线点 wp。给了 seg_start 时沿路线段 seg_start → wp 走: 每一步瞄准路线段上
        (机器人投影点) 前方 LOOKAHEAD 的一点, 偏离路线 (例如绕了一下才转过弯) 后很快回到线上,
        不会斜着贴近路边的物体。
        """
        cfg = self.config
        stuck = 0
        length = dist(seg_start, wp) if seg_start is not None else 0.0
        iterations = cfg.max_iterations + int(length // cfg.step_far)
        for _ in range(iterations):
            if self.poses.clock() - start_time > cfg.time_limit_s:
                raise _Fail("超时")
            pose = await self._pose()
            d = dist(pose.xy, wp)
            if d <= tol:
                return
            aim = wp
            if seg_start is not None and d > LOOKAHEAD:
                aim = _carrot(seg_start, wp, pose.xy, LOOKAHEAD)
            da = dist(pose.xy, aim)
            step = min(da, cfg.step_far) if d > cfg.near_distance else min(da, cfg.step_near)
            oriented = await self._orient_for(pose, aim, step,
                                              skippable=not final and d <= SKIP_DISTANCE,
                                              reverse=reverse)
            if oriented is None:
                return  # 中间路线点已经很近, 但贴着墙对不准: 直接去下一个点
            pose, direction = oriented
            step = self._limit_step(pose, direction * step)
            if abs(step) < 1:
                raise _Fail("会越出场地", target=wp)
            # 这一步会碰到障碍 (不包括本来就贴着的): 缩短, 实在不行就失败
            while abs(step) >= 1 and not self._path_clear(pose, step):
                step -= 1 if step > 0 else -1
            if abs(step) < 1:
                raise _Fail("前方有障碍", target=wp)
            before = pose.xy
            await self._run(("forward" if step > 0 else "backward") + f" {round(abs(step))}")
            after = await self._pose()
            if dist(before, after.xy) < cfg.progress_epsilon:
                stuck += 1
                if stuck >= cfg.no_progress_steps:
                    raise _Fail("没有进展", target=wp)
            else:
                stuck = 0
        pose = await self._pose()
        if dist(pose.xy, wp) > tol:
            raise _Fail("没有到达（迭代次数用完）", target=wp,
                        error_cm=round(dist(pose.xy, wp), 1))


def _carrot(a: Point, b: Point, p: Point, lookahead: float) -> Point:
    """路线段 a→b 上, p 的投影点往前 lookahead 的点 (不超过 b)。"""
    ab = dist(a, b)
    if ab < 1e-6:
        return b
    ux, uy = (b[0] - a[0]) / ab, (b[1] - a[1]) / ab
    t = (p[0] - a[0]) * ux + (p[1] - a[1]) * uy
    t = min(max(t, 0.0) + lookahead, ab)
    return (a[0] + ux * t, a[1] + uy * t)


__all__ = ["FAILED", "OK", "STOPPED", "NavConfig", "NavResult", "Navigator"]
