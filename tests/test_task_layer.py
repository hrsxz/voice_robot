"""
任务层 (改动 task-layer): 任务技能和工具定义、技能执行器、任务组配置、任务代理
(工具调用循环、上限、目标优先级、反问、失败策略、停止、进度)。

任务代理用脚本化的假 LLM (ScriptedLLM) 测试; 技能在模拟器里真实执行 (没有运动误差, 跑得快)。
接真实 LLM 的集成验证见 scripts/sim_task.py 和 openspec/changes/task-layer/measurements.md。
"""

import asyncio
import itertools
import json
import unittest
from typing import Any

from pc.field.model import FieldModelError, load_field
from pc.task.agent import (
    CANCELLED,
    DONE,
    LIMIT,
    TASK_STOPPED,
    LLMReply,
    TaskAgent,
    ToolCall,
    build_system_prompt,
)
from pc.task.missions import load_missions, parse_missions
from pc.task.runtime import TaskRuntime
from pc.task.tools import (
    build_task_tools,
    check_call,
    handoff_realtime_tool,
    load_handoff,
    load_task_tools,
)
from skills import load_skill_frontmatters, load_skill_registry

FIELD = load_field()
TASK_TOOLS = ("get_world_state", "move_object", "go_to", "pick", "place", "park", "ask_user")


class ScriptedLLM:
    """按脚本给出回复: 每一项是一个工具调用 (name, args) 列表, 或者最后的文字。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.messages: list[list[dict]] = []
        self._ids = itertools.count(1)

    async def complete(self, messages, tools):
        self.messages.append([dict(m) for m in messages])
        if not self.script:
            return LLMReply("完成")
        step = self.script.pop(0)
        if callable(step):
            step = step(messages)
        if isinstance(step, str):
            return LLMReply(step)
        if isinstance(step, tuple):
            step = [step]
        return LLMReply(None, [ToolCall(f"call_{next(self._ids)}", name,
                                        json.dumps(args, ensure_ascii=False))
                               for name, args in step])

    def tool_outputs(self) -> list[dict]:
        last = self.messages[-1] if self.messages else []
        return [json.loads(m["content"]) for m in last if m["role"] == "tool"]


def runtime(script, layout="wro2025_demo", **options) -> tuple[TaskRuntime, ScriptedLLM, list]:
    llm = ScriptedLLM(script)
    progress: list[str] = []
    rt = TaskRuntime.simulated(llm, layout=layout, noise=False, field=FIELD,
                               on_progress=progress.append, **options)
    return rt, llm, progress


# ---------------------------------------------------------------- 1.1 工具定义


class TaskToolsTest(unittest.TestCase):
    def test_seven_tools_from_skill_file(self):
        specs = load_task_tools()
        self.assertEqual(tuple(specs), TASK_TOOLS)
        fm = load_skill_frontmatters()["task"]
        self.assertEqual(fm["route"], "task")
        tools = build_task_tools(FIELD, specs)
        self.assertEqual([t["function"]["name"] for t in tools], list(TASK_TOOLS))
        for tool in tools:
            name = tool["function"]["name"]
            params = tool["function"]["parameters"]
            raw = fm["task_tools"][name]
            with self.subTest(tool=name):
                self.assertEqual(set(params["properties"]), set(raw.get("parameters") or {}))
                self.assertEqual(params["required"], list(raw.get("required") or []))
                self.assertFalse(params["additionalProperties"])
                for prop in params["properties"].values():
                    self.assertNotIn("enum_from", prop)

    def test_enums_come_from_field_model(self):
        tools = {t["function"]["name"]: t["function"]["parameters"]
                 for t in build_task_tools(FIELD)}
        self.assertEqual(tools["move_object"]["properties"]["object"]["enum"],
                         sorted(FIELD.objects))
        self.assertEqual(tools["move_object"]["properties"]["target"]["enum"],
                         sorted(FIELD.targets))
        self.assertIn("zone:A", tools["go_to"]["properties"]["target"]["enum"])
        self.assertEqual(tools["move_object"]["required"], ["object"])

    def test_task_skill_does_not_add_low_level_actions(self):
        # 任务技能不是 Hub 动作: 不进入动作 registry (快速通道 / RobotAgent / realtime 动作工具)
        registry = load_skill_registry()
        self.assertFalse(any(r.skill_id == "task" for r in registry.actions.values()))
        for name in (*TASK_TOOLS, "run_task"):
            self.assertIsNone(registry.get_action_rule(name))

    def test_handoff_definition(self):
        h = load_handoff()
        self.assertEqual((h.name, h.arg_key), ("run_task", "goal"))
        tool = handoff_realtime_tool(h)
        self.assertEqual(tool["name"], "run_task")
        self.assertEqual(tool["parameters"]["required"], ["goal"])

    def test_check_call_is_strict(self):
        ok = check_call("move_object", '{"object": "red_robot", "target": "zone:B"}', FIELD)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.args, {"object": "red_robot", "target": "zone:B"})
        # 可选参数为 null 等于没给
        self.assertEqual(check_call("move_object", {"object": "red_robot", "target": None},
                                    FIELD).args, {"object": "red_robot"})
        cases = {
            ("fly", "{}"): "没有这个工具",
            ("move_object", "{}"): "缺少参数",
            ("move_object", '{"object": "purple_dragon"}'): "没有这个物体",
            ("move_object", '{"object": "red_robot", "target": "zone:E"}'): "没有这个目标",
            ("go_to", '{"target": "zone:A", "speed": 3}'): "多余的参数",
            ("park", "not json"): "不是合法的 JSON",
            ("ask_user", '{"question": "哪个？", "options": "红色"}'): "字符串列表",
        }
        for (name, args), error in cases.items():
            with self.subTest(name=name, args=args):
                r = check_call(name, args, FIELD)
                self.assertFalse(r.ok)
                self.assertIn(error, r.error)


# ---------------------------------------------------------------- 1.3 任务组


class MissionsTest(unittest.TestCase):
    def test_default_is_wro2025_mission1_three_persons(self):
        rules = load_missions(field=FIELD)
        self.assertEqual(rules.name, "wro2025")
        self.assertEqual(rules.mission(1).items, {
            "yellow_person": "zone:A", "green_person": "zone:B", "red_person": "zone:D"})
        self.assertEqual(rules.mission(2).items, {"blue_person": "zone:B"})
        self.assertTrue(all(t.startswith("circle:") for t in rules.mission(4).items.values()))
        self.assertTrue(rules.mission(5).park)
        self.assertEqual(sorted(rules.missions), [1, 2, 3, 4, 5])
        for m in rules.missions.values():
            self.assertTrue(m.names_zh and m.names_de, m.number)

    def test_rule_targets(self):
        targets = load_missions(field=FIELD).rule_targets(FIELD)
        self.assertEqual(targets["yellow_person"], "zone:A")
        self.assertEqual(targets["red_robot"], "circle:red")
        self.assertEqual(set(targets), set(FIELD.objects))
        # 2022 年的规则: 所有人都送到 B 区
        old = load_missions("starter2022", FIELD).rule_targets(FIELD)
        self.assertEqual({old[o] for o in ("yellow_person", "green_person", "red_person")},
                         {"zone:B"})

    def test_validation(self):
        data = {"default": "x", "rulesets": {"x": {"missions": {
            1: {"items": {"yellow_person": "zone:Z", "ghost": "zone:A"}},
            2: {"items": {"yellow_person": "zone:A"}},
            3: {},
        }}}}
        with self.assertRaises(FieldModelError) as cm:
            parse_missions(data, field=FIELD)
        text = str(cm.exception)
        for part in ("zone:Z", "ghost", "同时出现", "任务 3"):
            self.assertIn(part, text)
        with self.assertRaises(FieldModelError):
            parse_missions(data, ruleset="nope")


# ---------------------------------------------------------------- 1.2 技能执行器


class TaskSkillsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rt, _llm, self.progress = runtime([])
        self.skills = self.rt.skills
        self.sim = self.rt.sim
        await self.skills.manip.refresh()

    async def test_rule_targets_applied_to_world(self):
        snap = self.skills.world.snapshot()
        self.assertEqual(snap.object("yellow_person").target, "zone:A")
        self.assertEqual(snap.object("red_person").target, "zone:D")

    async def test_move_object_uses_rule_target(self):
        out = await self.skills.run("move_object", {"object": "yellow_person"})
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(self.skills.world.object_state("yellow_person"), "delivered")
        self.assertTrue(FIELD.target("zone:A").touches(self.sim.objects["yellow_person"].xy, 2.5))
        changed = {c["id"]: c for c in out["changed"]}
        self.assertEqual(changed["yellow_person"]["state"], "已送达")
        self.assertIsNone(out["holding"])
        self.assertIn("正在抓取黄色小人", self.progress)
        self.assertIn("黄色小人已送到A区", self.progress)
        # 已经送达的再送一次: 不动
        n = len(self.sim.commands)
        again = await self.skills.run("move_object", {"object": "yellow_person"})
        self.assertEqual(again["status"], "ok")
        self.assertTrue(again["details"]["already"])
        self.assertEqual(len(self.sim.commands), n)

    async def test_pick_go_to_place(self):
        out = await self.skills.run("pick", {"object": "green_robot"})
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["holding"], "green_robot")
        # 夹着东西: 不能抓别的、不能停车 (不动)
        n = len(self.sim.commands)
        for name, args in (("pick", {"object": "red_robot"}), ("park", {}),
                           ("move_object", {"object": "red_robot"})):
            with self.subTest(name=name):
                r = await self.skills.run(name, args)
                self.assertEqual(r["status"], "failed")
                self.assertTrue(r["details"]["rejected"])
        self.assertEqual(len(self.sim.commands), n)
        out = await self.skills.run("go_to", {"target": "circle:blue"})
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["holding"], "green_robot")
        out = await self.skills.run("place", {"target": "circle:blue"})
        self.assertEqual(out["status"], "ok", out)
        self.assertIsNone(out["holding"])
        self.assertEqual(self.skills.world.snapshot().object("green_robot").target,
                         "circle:blue")
        self.assertEqual(self.skills.world.object_state("green_robot"), "delivered")

    async def test_place_without_holding_rejected(self):
        r = await self.skills.run("place", {})
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["reason"], "没有夹着物体")
        self.assertTrue(r["details"]["rejected"])
        self.assertEqual(self.sim.commands, [])

    async def test_park_and_world_state(self):
        r = await self.skills.run("go_to", {"target": "zone:B"})
        self.assertEqual(r["status"], "ok", r)
        r = await self.skills.run("park", {})
        self.assertEqual(r["status"], "ok", r)
        state = await self.skills.run("get_world_state", {})
        self.assertEqual(state["status"], "ok")
        self.assertEqual(len(state["objects"]), len(FIELD.objects))
        self.assertTrue(FIELD.target("start").contains(self.sim.robot.xy) or
                        FIELD.target("start").touches(self.sim.robot.xy, 8.0))


# ---------------------------------------------------------------- 2.x 任务代理


def call(name, **args):
    return (name, args)


class TaskAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_completes_and_reports(self):
        rt, llm, _progress = runtime([
            call("move_object", object="yellow_person"),
            "黄色小人已经送回 A 区。",
        ])
        result = await rt.run("把黄色小人送回家")
        self.assertEqual(result.status, DONE)
        self.assertEqual(result.message, "黄色小人已经送回 A 区。")
        self.assertEqual(result.skill_calls, 1)
        self.assertEqual(result.delivered, ["yellow_person"])
        # 第一轮: 系统提示 + 用户请求和开局世界摘要
        first = llm.messages[0]
        self.assertEqual(first[0]["role"], "system")
        self.assertIn("把黄色小人送回家", first[1]["content"])
        self.assertIn('"objects"', first[1]["content"])
        # 第二轮: 技能结果带着状态有变化的物体
        out = llm.tool_outputs()[0]
        self.assertEqual(out["status"], "ok")
        self.assertIn("yellow_person", [c["id"] for c in out["changed"]])

    async def test_step_limit(self):
        rt, _llm, _progress = runtime([call("get_world_state")] * 40, max_skill_calls=5)
        result = await rt.run("一直看")
        self.assertEqual(result.status, LIMIT)
        self.assertEqual(result.skill_calls, 5)
        self.assertIn("任务停止", result.message)

    async def test_time_limit_reports_delivered_and_pending(self):
        rt, _llm, _p = runtime([
            call("move_object", object="yellow_person"),
            call("move_object", object="green_person"),
            call("move_object", object="red_person"),
        ], time_limit_s=1.0)
        result = await rt.run("完成任务1")
        self.assertEqual(result.status, LIMIT)
        # 第一个物体送完时模拟时间已经超过 1 秒: 第二个不再执行
        self.assertEqual(result.skill_calls, 1)
        self.assertEqual(result.delivered, ["yellow_person"])
        self.assertIn("已送达：黄色小人", result.message)

    async def test_user_target_wins_and_mission_uses_rule_targets(self):
        # 2.2: 用户指定目标 -> 用用户的; 任务编号 -> 不填 target, 用规则目标
        rt, _llm, _p = runtime([
            call("move_object", object="red_robot", target="zone:B"),
            "红色服务机器人已经送到 B 区。",
        ])
        result = await rt.run("把红色服务机器人送到B区")
        self.assertEqual(result.status, DONE)
        world = rt.skills.world
        self.assertEqual(world.snapshot().object("red_robot").target, "zone:B")
        self.assertEqual(world.object_state("red_robot"), "delivered")
        self.assertTrue(FIELD.target("zone:B").touches(rt.sim.objects["red_robot"].xy, 3.0))

        rt, _llm, _p = runtime([call("move_object", object=o) for o in
                                ("green_robot", "red_robot", "blue_robot", "yellow_robot")]
                               + ["任务 4 完成"])
        result = await rt.run("完成任务4")
        self.assertEqual(result.status, DONE)
        for color in ("green", "red", "blue", "yellow"):
            oid = f"{color}_robot"
            with self.subTest(oid=oid):
                self.assertEqual(rt.skills.world.snapshot().object(oid).target,
                                 f"circle:{color}")
                self.assertEqual(rt.skills.world.object_state(oid), "delivered")

    async def test_system_prompt_rules(self):
        rt, _llm, _p = runtime([])
        prompt = build_system_prompt(rt.skills)
        self.assertIn("任务 1", prompt)
        self.assertIn("yellow_person → zone:A", prompt)
        self.assertIn("用户明确说了目标", prompt)
        self.assertIn("ask_user", prompt)
        self.assertIn("red_robot：红色服务机器人", prompt)
        self.assertIn("package_1：红色包裹 1", prompt)

    async def test_ask_user_waits_then_continues(self):
        # 2.3: "把红色的送到D区" -> 先列出 4 个红色候选, 回答之前小车不动
        asked: list[tuple[str, list[str], int]] = []
        candidates = ["红色小人", "红色包裹 1", "红色包裹 2", "红色服务机器人"]
        rt, _llm, _p = runtime([
            call("ask_user", question="红色的指哪一个？", options=candidates),
            lambda msgs: [("move_object", {"object": "red_robot", "target": "zone:D"})]
            if "服务机器人" in msgs[-1]["content"] else "听不懂",
            "红色服务机器人已经送到 D 区。",
        ])

        async def ask(question, options):
            asked.append((question, options, len(rt.sim.commands)))
            return "红色服务机器人"

        result = await rt.run("把红色的送到D区", ask=ask)
        self.assertEqual(result.status, DONE)
        self.assertEqual(asked, [("红色的指哪一个？", candidates, 0)])
        self.assertEqual(result.calls[0].status, "answered")
        self.assertEqual(rt.skills.world.object_state("red_robot"), "delivered")

    async def test_ask_user_timeout_cancels(self):
        rt, llm, _progress = runtime([
            call("ask_user", question="红色的指哪一个？", options=["红色小人", "红色服务机器人"]),
            call("move_object", object="red_robot"),
        ], ask_timeout_s=0.05)

        async def never(_q, _o):
            await asyncio.sleep(10)

        result = await rt.run("把红色的送到D区", ask=never)
        self.assertEqual(result.status, CANCELLED)
        self.assertIn("没有回答", result.message)
        self.assertEqual(rt.sim.commands, [])
        self.assertEqual(len(llm.messages), 1)  # 取消后不再调用 LLM

    async def test_failure_policy(self):
        # 2.4: 注入抓取失败; 同一个物体最多再试 1 次, 之后代码拒绝, 提示报告并询问
        answers: list[str] = []
        rt, _llm, progress = runtime([
            call("move_object", object="package_1"),
            call("move_object", object="package_1"),
            call("move_object", object="package_1"),
            call("ask_user", question="抓取包裹 1 失败，要继续处理其他物体吗？",
                 options=["继续", "停止"]),
            "包裹 1 抓取失败，用户选择停止。",
        ])
        rt.sim.slippery.add("package_1")

        async def ask(question, options):
            answers.append(question)
            return "停止"

        result = await rt.run("把包裹送到D区", ask=ask)
        self.assertEqual(result.status, DONE)
        statuses = [(c.name, c.status) for c in result.calls]
        self.assertEqual(statuses, [("move_object", "failed"), ("move_object", "failed"),
                                    ("move_object", "rejected"), ("ask_user", "answered")])
        self.assertTrue(result.calls[0].reason)  # 第一次"抓取失败"; 物体被碰动后也可能"无法接近"
        self.assertIn("不要再试", result.calls[2].reason)
        self.assertEqual(result.skill_calls, 2)  # 被拒绝的那次没有执行
        self.assertEqual(answers, ["抓取包裹 1 失败，要继续处理其他物体吗？"])
        self.assertTrue(any("抓取红色包裹 1失败" in p for p in progress), progress)
        self.assertEqual(result.pending, ["package_1"])

    async def test_failure_hint_after_first_failure(self):
        rt, llm, _p = runtime([call("move_object", object="package_1"), "好的"])
        rt.sim.slippery.add("package_1")
        await rt.run("把包裹 1 送到 D 区")
        out = llm.tool_outputs()[0]
        self.assertEqual(out["status"], "failed")
        self.assertIn("还可以再试 1 次", out["hint"])

    async def test_stop_cancels_whole_task(self):
        # 2.5: "完成任务1"执行到第二个小人时喊停 -> 小车停下, 第三个小人不再处理
        rt, llm, progress = runtime([
            call("move_object", object="yellow_person"),
            call("move_object", object="green_person"),
            call("move_object", object="red_person"),
            "完成",
        ], time_scale=0.0002)
        stopped = asyncio.Event()
        stop_task: list[asyncio.Task] = []

        def on_progress(msg: str) -> None:
            progress.append(msg)
            if msg == "正在抓取绿色小人" and not stop_task:
                async def later():
                    await asyncio.sleep(0.01)
                    await rt.stop()
                    stopped.set()
                stop_task.append(asyncio.ensure_future(later()))

        rt.on_progress = on_progress
        result = await rt.run("完成任务1")
        await stopped.wait()
        self.assertEqual(result.status, TASK_STOPPED)
        self.assertEqual(rt.skills.world.object_state("yellow_person"), "delivered")
        self.assertNotIn("正在抓取红色小人", progress)
        self.assertNotEqual(rt.skills.world.object_state("red_person"), "delivered")
        self.assertIn("任务已停止", progress)
        rounds = len(llm.messages)
        await asyncio.sleep(0.05)
        self.assertEqual(len(llm.messages), rounds)  # 停止后不再调用 LLM
        self.assertFalse(rt.busy)

    async def test_only_one_task_at_a_time(self):
        rt, _llm, _p = runtime([call("move_object", object="yellow_person"), "完成"],
                               time_scale=0.0002)
        task = rt.start("把黄色小人送回家")
        from pc.task.runtime import TaskBusyError
        with self.assertRaises(TaskBusyError):
            rt.start("完成任务1")
        await task

    async def test_invalid_call_is_reported_back(self):
        rt, llm, _p = runtime([call("move_object", object="purple_dragon"), "没有这个物体"])
        result = await rt.run("把紫色恐龙送到A区")
        self.assertEqual(result.status, DONE)
        self.assertEqual(result.skill_calls, 0)
        out = llm.tool_outputs()[0]
        self.assertEqual(out["status"], "rejected")
        self.assertIn("没有这个物体", out["reason"])

    async def test_agent_without_ask_cancels(self):
        rt, _llm, _p = runtime([call("ask_user", question="哪个？", options=["a", "b"])])
        result = await rt.run("把红色的送到D区")
        self.assertEqual(result.status, CANCELLED)

    async def test_progress_sequence(self):
        # 2.6: 每个技能开始和结束时都有进度
        rt, _llm, progress = runtime([call("move_object", object="green_robot"),
                                      call("park"), "完成"])
        await rt.run("把绿色服务机器人送回圆圈，然后停车")
        self.assertEqual(progress, [
            "正在抓取绿色服务机器人", "已夹住绿色服务机器人",
            "正在把绿色服务机器人送到绿色圆圈", "绿色服务机器人已送到绿色圆圈",
            "正在回起终点区", "已停进起终点区",
        ])


class TaskAgentDirectTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_any_skill(self):
        rt, _llm, _p = runtime([])
        agent = TaskAgent(rt.skills, ScriptedLLM([]))
        await agent.stop()  # 没有任务在执行: 不报错
        self.assertFalse(agent.running)


if __name__ == "__main__":
    unittest.main()
