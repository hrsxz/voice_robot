import asyncio
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pc.agent.robot_agent import EXECUTED, REJECTED, RobotAgent
from pc.llm.llm_client import LLMClient
from pc.utils.latency import CommandTimer
from pc.voice_to_robot import VoiceController


def chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])


class GenerateStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_steps_are_yielded_line_by_line(self) -> None:
        log: list[str] = []

        async def fake_stream():
            # 第 2 行被拆在两个数据块里
            parts = [
                '{"action":"forward","args":{"distance_cm":30}}\n{"act',
                'ion":"left","args":{"angle_deg":90}}\n',
                '{"action":"right","args":{"angle_deg":45}}',
            ]
            for i, part in enumerate(parts, start=1):
                log.append(f"chunk{i}")
                yield chunk(part)

        client = LLMClient()
        client.openai_api_key = "test"
        fake = MagicMock()
        fake.chat.completions.create = AsyncMock(return_value=fake_stream())
        client._openai_client = fake

        steps = []
        async for step in client.generate_stream("跳个舞", executed=["forward 10"]):
            steps.append(step)
            log.append(f"step:{step['action']}")

        self.assertEqual([s["action"] for s in steps], ["forward", "left", "right"])
        # 第 1 个动作在第 3 个数据块到达之前就产出了
        self.assertLess(log.index("step:forward"), log.index("chunk3"))
        kwargs = fake.chat.completions.create.await_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertIn("已执行：forward 10", kwargs["messages"][1]["content"])


class GatedLLM:
    """输出第 1 个动作后暂停, 等测试放行后再输出后面的。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.first_yielded = asyncio.Event()

    async def generate_stream(self, text, executed=None, model=None):
        yield {"action": "forward", "args": {"distance_cm": 30}}
        self.first_yielded.set()
        await self.gate.wait()
        yield {"action": "forward", "args": {"distance_cm": 20000}}  # 超出范围
        yield {"action": "left", "args": {"angle_deg": 90}}


class DummyHub:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class StreamedExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_action_runs_while_llm_still_generating(self) -> None:
        vc = VoiceController(spike_simulation=True)
        hub = DummyHub()
        vc.spike = hub
        vc.robot_agent = RobotAgent(hub)
        llm = GatedLLM()
        vc.llm_client.generate_stream = llm.generate_stream

        timer = CommandTimer(input_mode="cli")
        timer.mark("recording_end")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            task = asyncio.create_task(vc.handle_text("跳个舞", timer))
            await asyncio.wait_for(llm.first_yielded.wait(), 1)
            for _ in range(5):
                await asyncio.sleep(0)
            # LLM 还没输出完, 第 1 个动作已经发给了 Hub
            self.assertEqual(hub.commands, ["forward 30"])
            self.assertFalse(task.done())

            llm.gate.set()
            batch = await asyncio.wait_for(task, 1)
            await vc._drain_reports()

        assert batch is not None
        self.assertEqual([r.status for r in batch.records], [EXECUTED, REJECTED, EXECUTED])
        self.assertEqual(batch.records[1].detail, "value above max")
        self.assertEqual(hub.commands, ["forward 30", "left 90"])
        self.assertIn("first_enqueued", timer.marks)
        self.assertLessEqual(timer.marks["first_enqueued"], timer.marks["route_done"])


if __name__ == "__main__":
    unittest.main()
