"""
任务技能执行器 (改动 task-layer 设计 D2, 任务 1.2)。

在 navigation-grasp (Manipulator / Navigator / WorldState) 之上实现任务代理的技能:
get_world_state、move_object、go_to、pick、place、park。ask_user 由任务代理自己处理。

每个技能返回一个字典 (给 LLM 当工具结果):
    {"status": "ok" | "failed" | "stopped", "reason": "...", "details": {...},
     "holding": 夹着的物体 | None, "changed": [状态有变化的物体]}
前置条件不满足 (没夹东西就放下、夹着东西去抓别的、夹着东西去停车) 时不动, 返回 failed,
details.rejected = True。

规则目标: 开局按任务组配置 (missions.yaml) 设置每个物体的目标;
move_object / place 不给目标时用规则目标, 给了就用给的 (设计 D6: 用户指定优先)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pc.field.geometry import dist
from pc.navigation.controller import FAILED, OK, STOPPED, NavResult
from pc.navigation.grasp import Manipulator
from pc.task.missions import MissionRules
from pc.world.state import STATE_LABELS, WorldSnapshot

TYPE_LABELS = {"small_person": "小人", "large_person": "大人", "package": "包裹",
               "service_robot": "服务机器人"}
COLOR_LABELS = {"yellow": "黄色", "green": "绿色", "red": "红色", "blue": "蓝色"}
ZONE_COLORS = {"A": "黄色", "B": "绿色", "C": "蓝色", "D": "红色"}
CHANGE_DISTANCE = 2.0  # 物体位置变化超过多少厘米算"有变化"


def object_label(oid: str, field: Any) -> str:
    spec = field.objects.get(oid)
    if spec is None:
        return oid
    label = COLOR_LABELS.get(spec.color, spec.color) + TYPE_LABELS.get(spec.type, spec.type)
    same = [o for o, s in field.objects.items() if s.type == spec.type and s.color == spec.color]
    if len(same) > 1:
        label += f" {same.index(oid) + 1}"
    return label


def target_label(target_id: str) -> str:
    if target_id.startswith("zone:"):
        return f"{target_id[5:]}区"
    if target_id.startswith("circle:"):
        color = target_id[7:]
        return f"{COLOR_LABELS.get(color, color)}圆圈"
    if target_id == "start":
        return "起终点区"
    return target_id


class TaskSkills:
    def __init__(
        self,
        manip: Manipulator,
        rules: MissionRules,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.manip = manip
        self.nav = manip.nav
        self.world = manip.world
        self.field = manip.field
        self.rules = rules
        self.rule_targets = rules.rule_targets(self.field)
        for oid, target in self.rule_targets.items():
            self.world.set_target(oid, target)
        self.on_progress = on_progress or (lambda _m: None)

    # ---------------- 入口 ----------------

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """执行一个 (已经校验过参数的) 技能, 返回给 LLM 的结果。"""
        if name == "get_world_state":
            snap = await self.manip.refresh()
            return {"status": OK, **snap.summary()}
        before = self.world.snapshot()
        handler = getattr(self, f"_skill_{name}", None)
        if handler is None:
            return self._result(NavResult(FAILED, f"没有这个技能：{name}", {"rejected": True}),
                                before)
        r = await handler(**args)
        snap = await self.manip.refresh()
        return self._result(r, before, snap)

    def label(self, oid: str) -> str:
        return object_label(oid, self.field)

    # ---------------- 技能 ----------------

    async def _skill_move_object(self, object: str, target: str | None = None) -> NavResult:
        target_id = target or self.rule_targets[object]
        holding = self.world.holding
        if holding is not None and holding != object:
            return NavResult(FAILED, f"已经夹着 {holding}，先 place 再处理别的物体",
                             {"rejected": True})
        self.world.set_target(object, target_id)
        if holding is None:
            await self.manip.refresh()
            if self.world.object_state(object) == "delivered":
                self.on_progress(f"{self.label(object)}已经在{target_label(target_id)}")
                return NavResult(OK, "已经在目标上，不用移动", {"target": target_id,
                                                              "already": True})
            r = await self._pick(object)
            if not r.ok:
                return r
        return await self._place(target_id)

    async def _skill_go_to(self, target: str) -> NavResult:
        t = self.field.target(target)
        await self.manip.refresh()
        robot = self.world.snapshot().robot
        others = self.manip._others(self.world.holding)
        entries = sorted(t.entries, key=lambda e: dist(robot.xy, e.xy) if robot else 0.0)
        self.on_progress(f"正在前往{target_label(target)}")
        r = NavResult(FAILED, "没有进入点")
        for e in entries:
            r = await self.nav.go_to(e.xy, e.heading, obstacles=others)
            if r.ok or r.status == STOPPED:
                break
        self._report(r, f"已到达{target_label(target)}", f"前往{target_label(target)}")
        return r

    async def _skill_pick(self, object: str) -> NavResult:
        return await self._pick(object)

    async def _skill_place(self, target: str | None = None) -> NavResult:
        holding = self.world.holding
        if holding is None:
            return NavResult(FAILED, "没有夹着物体", {"rejected": True})
        return await self._place(target or self.rule_targets[holding])

    async def _skill_park(self) -> NavResult:
        holding = self.world.holding
        if holding is not None:
            return NavResult(FAILED, f"还夹着 {holding}，先 place", {"rejected": True})
        self.on_progress("正在回起终点区")
        r = await self.manip.park()
        self._report(r, "已停进起终点区", "回起终点区")
        return r

    # ---------------- 内部 ----------------

    async def _pick(self, oid: str) -> NavResult:
        holding = self.world.holding
        if holding is not None:
            return NavResult(FAILED, f"已经夹着 {holding}", {"rejected": True})
        self.on_progress(f"正在抓取{self.label(oid)}")
        r = await self.manip.pick(oid)
        self._report(r, f"已夹住{self.label(oid)}", f"抓取{self.label(oid)}")
        return r

    async def _place(self, target_id: str) -> NavResult:
        oid = self.world.holding
        assert oid is not None
        self.on_progress(f"正在把{self.label(oid)}送到{target_label(target_id)}")
        r = await self.manip.place(target_id)
        self._report(r, f"{self.label(oid)}已送到{target_label(target_id)}",
                     f"把{self.label(oid)}送到{target_label(target_id)}")
        return r

    def _report(self, r: NavResult, ok_text: str, what: str) -> None:
        if r.ok:
            self.on_progress(ok_text)
        elif r.status == STOPPED:
            self.on_progress(f"{what}：已停止")
        else:
            self.on_progress(f"{what}失败：{r.reason}")

    def _result(self, r: NavResult, before: WorldSnapshot,
                after: WorldSnapshot | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"status": r.status, "reason": r.reason}
        details = {k: v for k, v in r.details.items() if _jsonable(v)}
        if details:
            out["details"] = details
        after = after or self.world.snapshot()
        out["holding"] = after.holding
        out["changed"] = changed_objects(before, after)
        return out


def changed_objects(before: WorldSnapshot, after: WorldSnapshot) -> list[dict[str, Any]]:
    """状态、目标或位置 (超过 2 cm) 有变化的物体。"""
    old = {o.id: o for o in before.objects}
    out = []
    for o in after.objects:
        b = old.get(o.id)
        moved = (b is not None and b.position is not None and o.position is not None
                 and dist(b.position, o.position) > CHANGE_DISTANCE)
        if b is None or b.state != o.state or b.target != o.target or moved:
            item: dict[str, Any] = {"id": o.id, "state": STATE_LABELS[o.state],
                                    "target": o.target}
            if o.position is not None:
                item.update(x=round(o.position[0]), y=round(o.position[1]))
            out.append(item)
    return out


def _jsonable(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool, type(None), list, dict))


__all__ = ["COLOR_LABELS", "TYPE_LABELS", "ZONE_COLORS", "TaskSkills", "changed_objects",
           "object_label", "target_label"]
