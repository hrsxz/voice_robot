import unittest
from unittest.mock import AsyncMock

from pc.llm.streaming_router import LLM, STOPPED, StreamingRouter


class Recorder:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.events: list[str] = []
        self.stop = AsyncMock(side_effect=lambda: self.events.append("STOP"))
        self.llm = AsyncMock(side_effect=lambda text, done: self.events.append(f"LLM:{text}"))

    def on_command(self, cmd: str) -> None:
        self.commands.append(cmd)
        self.events.append(cmd)

    def router(self) -> StreamingRouter:
        return StreamingRouter(self.on_command, self.stop, self.llm)


class FastPathStreamingTest(unittest.IsolatedAsyncioTestCase):
    async def test_complete_segment_executes_immediately(self) -> None:
        rec = Recorder()
        router = rec.router()

        await router.on_segment("左转90度。")
        # 不用等下一个语段或松开空格, 第一个动作已经入队
        self.assertEqual(rec.commands, ["left 90"])

        await router.on_segment("然后前进20厘米。")
        await router.finish()
        self.assertEqual(rec.commands, ["left 90", "forward 20"])
        rec.llm.assert_not_awaited()

    async def test_incomplete_clause_waits_for_next_segment(self) -> None:
        rec = Recorder()
        router = rec.router()

        await router.on_segment("向前走")
        self.assertEqual(rec.commands, [])  # 挂起
        await router.on_segment("三十厘米。")
        self.assertEqual(rec.commands, ["forward 30"])
        await router.finish()
        rec.llm.assert_not_awaited()

    async def test_repeat_in_later_segment(self) -> None:
        rec = Recorder()
        router = rec.router()
        await router.on_segment("向前走30厘米，再左转90度。")
        await router.on_segment("一共做四次。")
        await router.finish()
        self.assertEqual(rec.commands, ["forward 30", "left 90"] * 4)


class FastThenLlmTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_clause_runs_before_llm_gets_the_rest(self) -> None:
        rec = Recorder()
        router = rec.router()

        await router.on_segment("前进30厘米。")
        self.assertEqual(rec.commands, ["forward 30"])  # LLM 还没被调用就已经入队

        await router.on_segment("然后跳个舞。")
        # "跳个舞"匹配不上, 先挂起 (下一个语段可能接着说), 这句话还没说完, 不调用 LLM
        rec.llm.assert_not_awaited()

        await router.finish()
        self.assertEqual(router.state, LLM)
        rec.llm.assert_awaited_once_with("跳个舞", ["forward 30"])
        self.assertEqual(rec.events, ["forward 30", "LLM:跳个舞"])

    async def test_everything_after_first_miss_goes_to_llm(self) -> None:
        rec = Recorder()
        router = rec.router()
        await router.on_segment("左转90度，跳个舞，然后前进10厘米。")
        await router.finish()
        self.assertEqual(rec.commands, ["left 90"])
        rec.llm.assert_awaited_once_with("跳个舞，前进10厘米", ["left 90"])


class StreamingStopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_word_in_partial_stops_immediately(self) -> None:
        rec = Recorder()
        router = rec.router()
        await router.on_segment("前进100厘米。")
        await router.on_partial("停")

        rec.stop.assert_awaited_once()
        self.assertEqual(router.state, STOPPED)

        # 这句话剩下的内容被丢弃
        await router.on_segment("停，然后左转90度。")
        await router.finish()
        self.assertEqual(rec.commands, ["forward 100"])
        rec.llm.assert_not_awaited()
        rec.stop.assert_awaited_once()

    async def test_stop_word_in_segment_also_stops(self) -> None:
        rec = Recorder()
        router = rec.router()
        await router.on_segment("急停！")
        rec.stop.assert_awaited_once()
        self.assertEqual(rec.commands, [])


if __name__ == "__main__":
    unittest.main()
