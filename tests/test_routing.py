import contextlib
import io
import unittest

from pc.agent.robot_agent import RobotAgent
from pc.llm import intent_parser
from pc.llm.llm_client import LLMUnavailableError
from pc.utils.latency import CommandTimer
from pc.utils.utils import normalize_text
from pc.voice_to_robot import VoiceController


class DummyHub:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class FakeLLM:
    """代替 LLMClient.generate_stream: 按输出文本逐行产出 step, 或者抛出指定异常。"""

    def __init__(self, output: str = "", error: Exception | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    async def generate_stream(self, text, executed=None, model=None):
        self.calls.append((text, list(executed or [])))
        if self.error is not None:
            raise self.error
        for step in intent_parser.parse_intent(self.output)["steps"]:
            yield step


class RoutingTest(unittest.IsolatedAsyncioTestCase):
    def make_controller(self, fast_path: bool = True, llm_output: str = "",
                        llm_error: Exception | None = None):
        vc = VoiceController(spike_simulation=True, fast_path=fast_path)
        hub = DummyHub()
        vc.spike = hub
        vc.robot_agent = RobotAgent(hub)
        self.llm = FakeLLM(llm_output, llm_error)
        vc.llm_client.generate_stream = self.llm.generate_stream
        return vc, hub

    async def run_text(self, vc: VoiceController, text: str) -> tuple[CommandTimer, str]:
        timer = CommandTimer(input_mode="cli")
        timer.mark("recording_end")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await vc.handle_text(normalize_text(text), timer)
            await vc._drain_reports()
        return timer, out.getvalue()

    async def test_all_clauses_hit_skips_llm(self) -> None:
        vc, hub = self.make_controller()
        timer, _ = await self.run_text(vc, "向前走50厘米，然后向右转90度")

        self.assertEqual(self.llm.calls, [])
        self.assertEqual(hub.commands, ["forward 50", "right 90"])
        self.assertEqual(timer.route, "fast_path")

    async def test_partial_hit_sends_whole_sentence_to_llm(self) -> None:
        vc, hub = self.make_controller()
        await self.run_text(vc, "前进30厘米然后把红块搬到B区")

        self.assertEqual(len(self.llm.calls), 1)
        sent_text, executed = self.llm.calls[0]
        self.assertEqual(sent_text, normalize_text("前进30厘米然后把红块搬到B区"))
        self.assertEqual(executed, [])
        # LLM 返回空结果, 本地也没有执行前半句
        self.assertEqual(hub.commands, [])

    async def test_llm_result_is_executed(self) -> None:
        llm_output = '{"steps":[{"action":"left","args":{"angle_deg":45}}]}'
        vc, hub = self.make_controller(llm_output=llm_output)
        timer, _ = await self.run_text(vc, "跳个舞")

        self.assertEqual(hub.commands, ["left 45"])
        self.assertEqual(timer.route, "llm")
        self.assertIn("route_done", timer.marks)

    async def test_fast_path_can_be_disabled(self) -> None:
        vc, hub = self.make_controller(fast_path=False)
        timer, _ = await self.run_text(vc, "前进30厘米")

        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(timer.route, "llm")
        self.assertEqual(hub.commands, [])

    async def test_emergency_stop_sends_single_stop(self) -> None:
        vc, hub = self.make_controller()
        timer, _ = await self.run_text(vc, "停！停！")

        self.assertEqual(self.llm.calls, [])
        self.assertEqual(hub.commands, ["stop"])
        self.assertEqual(timer.route, "stop")

    async def test_stop_inside_sequence_is_normal_step(self) -> None:
        vc, hub = self.make_controller()
        timer, _ = await self.run_text(vc, "前进30厘米然后停止")

        self.assertEqual(hub.commands, ["forward 30", "stop"])
        self.assertEqual(timer.route, "fast_path")

    async def test_offline_unknown_phrase_is_rejected(self) -> None:
        vc, hub = self.make_controller(llm_error=LLMUnavailableError("no llm"))
        timer, out = await self.run_text(vc, "左夹子转一转")

        self.assertEqual(hub.commands, [])
        self.assertIn("无法识别", out)
        self.assertEqual(timer.failed_stage, "llm")
        self.assertIn("失败环节=LLM", out)

    async def test_offline_fast_path_still_works(self) -> None:
        vc, hub = self.make_controller(llm_error=LLMUnavailableError("no llm"))
        await self.run_text(vc, "左夹子抬起来")

        self.assertEqual(hub.commands, ["gripper_left_up"])

    async def test_llm_error_is_rejected(self) -> None:
        vc, hub = self.make_controller(llm_error=RuntimeError("timeout"))
        _, out = await self.run_text(vc, "跳个舞")

        self.assertEqual(hub.commands, [])
        self.assertIn("无法识别", out)

    async def test_non_json_llm_output_is_not_guessed(self) -> None:
        vc, hub = self.make_controller(llm_output="好的，我会向左转90度然后前进30cm")
        _, out = await self.run_text(vc, "跳个舞")

        self.assertEqual(hub.commands, [])
        self.assertIn("无法识别", out)


if __name__ == "__main__":
    unittest.main()
