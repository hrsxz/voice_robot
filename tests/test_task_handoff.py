"""
语音链路到任务层的交接 (改动 task-layer 任务 3.1、3.2, 以及 2.3 的语音回答、2.5 的急停)。

stream 链路: 意图 LLM 输出 run_task 一行 -> VoiceController.start_task; 直接动作仍走快速通道。
realtime 链路: 模型调用 run_task 工具 -> start_task (用录制的事件夹具 l4_task2 测试)。
"""

import asyncio
import contextlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pc.agent.robot_agent import RobotAgent
from pc.llm import intent_parser
from pc.llm.llm_client import build_intent_instructions
from pc.llm.realtime_tools import INSTRUCTIONS, build_session_tools, handoff_goal
from pc.speech.realtime_session import RealtimeSession
from pc.utils.latency import CommandTimer
from pc.utils.utils import normalize_text
from pc.voice_to_robot import VoiceController
from tests.test_stream_pipeline import FakeSource, FakeTranscriber

FIXTURES = Path(__file__).parent / "fixtures" / "realtime"


class DummyHub:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class FakeLLM:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[str] = []

    async def generate_stream(self, text, executed=None, model=None):
        self.calls.append(text)
        for step in intent_parser.parse_intent(self.output)["steps"]:
            yield step


class FakeRuntime:
    def __init__(self) -> None:
        self.goals: list[str] = []
        self.stops = 0
        self.task: asyncio.Task | None = None
        self.ask = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, goal, ask=None, on_done=None):
        self.goals.append(goal)
        self.ask = ask

        async def run():
            await asyncio.sleep(10)
        self.task = asyncio.ensure_future(run())
        return self.task

    async def stop(self):
        self.stops += 1
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        return True


def make_controller(llm_output: str = "", runtime=True, **fake_kwargs):
    vc = VoiceController(spike_simulation=True)
    hub = DummyHub()
    vc.spike = hub
    vc.robot_agent = RobotAgent(hub)
    llm = FakeLLM(llm_output)
    vc.llm_client.generate_stream = llm.generate_stream
    vc.task_runtime = FakeRuntime() if runtime else None
    if fake_kwargs:
        vc.streaming_transcriber_factory = (
            lambda on_partial, on_segment: FakeTranscriber(on_partial, on_segment, **fake_kwargs))
    return vc, hub, llm


async def say(vc: VoiceController, text: str) -> tuple[CommandTimer, str]:
    timer = CommandTimer(input_mode="cli")
    timer.mark("recording_end")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        await vc.handle_text(normalize_text(text), timer)
        await vc._drain_reports()
    return timer, out.getvalue()


RUN_TASK = '{"action":"run_task","args":{"goal":"把黄色小人送回家"}}'


class StreamHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await asyncio.sleep(0)

    def test_intent_prompt_mentions_run_task(self):
        text = build_intent_instructions()
        self.assertIn('- run_task: {"goal": 用户的原话}', text)
        self.assertIn('{"action":"run_task","args":{"goal":"完成任务1"}}', text)

    async def test_goal_request_goes_to_task_layer(self):
        vc, hub, llm = make_controller(RUN_TASK)
        timer, out = await say(vc, "把黄色小人送回家")
        self.assertEqual(llm.calls, [normalize_text("把黄色小人送回家")])
        self.assertEqual(vc.task_runtime.goals, ["把黄色小人送回家"])
        self.assertEqual(hub.commands, [])
        self.assertEqual(timer.route, "task")
        self.assertIn("路由=任务层", out)
        await vc.task_runtime.stop()

    async def test_direct_action_stays_on_fast_path(self):
        vc, hub, llm = make_controller(RUN_TASK)
        timer, _ = await say(vc, "前进30厘米")
        self.assertEqual(llm.calls, [])
        self.assertEqual(vc.task_runtime.goals, [])
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertEqual(timer.route, "fast_path")

    async def test_lines_after_handoff_are_ignored(self):
        forward = '{"action":"forward","args":{"distance_cm":10}}'
        vc, hub, _llm = make_controller(RUN_TASK + "\n" + forward)
        await say(vc, "把黄色小人送回家")
        self.assertEqual(hub.commands, [])
        await vc.task_runtime.stop()

    async def test_goal_falls_back_to_text(self):
        vc, _hub, _llm = make_controller('{"action":"run_task","args":{}}')
        await say(vc, "完成任务1")
        self.assertEqual(vc.task_runtime.goals, [normalize_text("完成任务1")])
        await vc.task_runtime.stop()

    async def test_without_task_layer(self):
        vc, hub, _llm = make_controller(RUN_TASK, runtime=False)
        _timer, out = await say(vc, "把黄色小人送回家")
        self.assertIn("任务层没有启用", out)
        self.assertEqual(hub.commands, [])

    async def test_busy_task_rejected(self):
        vc, _hub, _llm = make_controller(RUN_TASK)
        from pc.task.runtime import TaskBusyError

        def busy_start(goal, ask=None, on_done=None):
            raise TaskBusyError("已经有任务在执行，先说\"停\"")
        vc.task_runtime.start = busy_start
        _t, out = await say(vc, "把黄色小人送回家")
        self.assertIn("已经有任务在执行", out)

    async def test_answer_goes_to_pending_question(self):
        vc, hub, llm = make_controller(RUN_TASK)
        asking = asyncio.ensure_future(
            vc.ask_user("红色的指哪一个？", ["红色小人", "红色服务机器人"]))
        await asyncio.sleep(0)
        self.assertTrue(vc.answer_pending())
        timer, _ = await say(vc, "红色服务机器人")
        self.assertEqual(await asking, normalize_text("红色服务机器人"))
        self.assertEqual(llm.calls, [])  # 回答不当作命令
        self.assertEqual(hub.commands, [])
        self.assertEqual(timer.route, "answer")
        self.assertFalse(vc.answer_pending())

    async def test_stop_while_question_pending_stops_task(self):
        vc, hub, _llm = make_controller(RUN_TASK)
        await say(vc, "把黄色小人送回家")
        asking = asyncio.ensure_future(vc.ask_user("哪个？", ["a", "b"]))
        await asyncio.sleep(0)
        await say(vc, "停")
        self.assertEqual(vc.task_runtime.stops, 1)
        self.assertIn("stop", hub.commands)
        self.assertFalse(asking.done())  # 问题仍挂着, 由任务代理的取消结束
        asking.cancel()

    async def test_emergency_stop_cancels_task(self):
        vc, _hub, _llm = make_controller(RUN_TASK)
        await say(vc, "把黄色小人送回家")
        self.assertTrue(vc.task_runtime.busy)
        await say(vc, "停")
        self.assertEqual(vc.task_runtime.stops, 1)
        self.assertFalse(vc.task_runtime.busy)

    async def test_stream_answer(self):
        vc, hub, llm = make_controller(RUN_TASK, script={5: ["红色服务"]}, tail=["机器人。"])
        asking = asyncio.ensure_future(vc.ask_user("哪个？", ["红色小人", "红色服务机器人"]))
        await asyncio.sleep(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await vc.process_stream(FakeSource(10))
            await vc._drain_reports()
        self.assertEqual(await asking, "红色服务机器人")
        self.assertEqual(llm.calls, [])
        self.assertEqual(hub.commands, [])

    async def test_stream_goal_request(self):
        vc, hub, _llm = make_controller(RUN_TASK, script={5: ["把黄色小人送回家。"]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await vc.process_stream(FakeSource(10))
            await vc._drain_reports()
        self.assertEqual(vc.task_runtime.goals, ["把黄色小人送回家"])
        self.assertEqual(hub.commands, [])
        await vc.task_runtime.stop()


# ---------------------------------------------------------------- realtime


def load_fixture(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class RealtimeHandoffTest(unittest.IsolatedAsyncioTestCase):
    def test_session_tools_include_run_task(self):
        tools = {t["name"]: t for t in build_session_tools()}
        self.assertIn("run_task", tools)
        self.assertIn("forward", tools)
        self.assertEqual(tools["run_task"]["parameters"]["required"], ["goal"])
        self.assertIn("run_task", INSTRUCTIONS)

    def test_handoff_goal(self):
        self.assertEqual(handoff_goal('{"goal": " 完成任务2 "}'), "完成任务2")
        self.assertIsNone(handoff_goal("{}"))
        self.assertIsNone(handoff_goal("oops"))

    async def test_recorded_task2_calls_run_task(self):
        # 录制的真实接口事件: 说"完成任务2", 模型调用 run_task
        events: list[tuple] = []

        class Rec:
            def __getattr__(self, name):
                async def record(*args):
                    events.append((name, *args))
                return record

        session = RealtimeSession(Rec(), "x", [])
        for e in load_fixture("l4_task2"):
            await session._handle(SimpleNamespace(**e))
        calls = [(e[3], json.loads(e[4])) for e in events if e[0] == "on_function_call"]
        self.assertEqual(len(calls), 1, calls)
        name, args = calls[0]
        self.assertEqual(name, "run_task")
        self.assertIn("任务", args["goal"])
        self.assertIn("2", normalize_text(args["goal"]).replace("二", "2"))

    async def test_pipeline_hands_off_and_finishes_turn(self):
        from pc.llm.realtime_pipeline import RealtimePipeline, Turn

        vc, hub, _llm = make_controller()
        outputs: list[tuple[str, str]] = []
        rp = RealtimePipeline(vc, log=lambda _m: None)

        async def send_output(call_id, output):
            outputs.append((call_id, output))

        async def create_response():
            raise AssertionError("交接后不应再请求回复")

        rp.session.send_function_output = send_output
        rp.session.create_response = create_response
        timer = CommandTimer(input_mode="ptt-realtime")
        turn = Turn(timer=timer, batch=vc.new_batch(timer))
        rp._by_response["r1"] = turn
        rp._current = turn
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await rp.on_function_call("r1", "c1", "run_task", '{"goal": "完成任务2"}')
            await rp.on_response_done("r1", "completed", [], 1)
            await vc._drain_reports()
        self.assertEqual(vc.task_runtime.goals, ["完成任务2"])
        self.assertEqual(outputs, [("c1", "已交给任务层")])
        self.assertTrue(turn.done.is_set())
        self.assertEqual(timer.route, "task")
        self.assertNotIn("无法识别", out.getvalue())
        self.assertEqual(hub.commands, [])
        await vc.task_runtime.stop()

    async def test_pipeline_answer_turn_skips_model(self):
        from pc.llm.realtime_pipeline import RealtimePipeline, Turn

        vc, _hub, _llm = make_controller()
        rp = RealtimePipeline(vc, log=lambda _m: None)

        async def create_response():
            raise AssertionError("回答不应请求模型回复")

        rp.session.create_response = create_response
        asking = asyncio.ensure_future(vc.ask_user("哪个？", ["a", "b"]))
        await asyncio.sleep(0)
        timer = CommandTimer(input_mode="ptt-realtime")
        rp._pending_commit.append(Turn(timer=timer, batch=vc.new_batch(timer)))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await rp.on_committed("item1")
            await rp.on_transcript_done("item1", "红色服务机器人")
        self.assertEqual(await asking, "红色服务机器人")


if __name__ == "__main__":
    unittest.main()
