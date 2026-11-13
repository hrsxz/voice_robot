import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np

from pc.agent.robot_agent import RobotAgent
from pc.speech.streaming_stt import StreamingSTTError
from pc.voice_to_robot import VoiceController

CHUNK = np.zeros(960, dtype=np.int16).tobytes()


class FakeSource:
    """产出 n 个音频块; 每产出一块后调用 on_chunk(i), 方便在"说话过程中"检查状态。"""

    def __init__(self, n: int = 20, on_chunk=None) -> None:
        self.n = n
        self.on_chunk = on_chunk
        self.audio = np.zeros(960 * n, dtype=np.int16)

    async def __aiter__(self):
        for i in range(1, self.n + 1):
            yield CHUNK
            await asyncio.sleep(0)
            if self.on_chunk:
                self.on_chunk(i)


class FakeTranscriber:
    """
    script: {第几块: [语段文字, ...]}, 在收到这一块时回调 on_segment。
    fail_start / fail_at_chunk: 模拟连接失败 / 中途断开。
    """

    def __init__(self, on_partial, on_segment, script=None, tail=None,
                 fail_start=False, fail_at_chunk=None) -> None:
        self.on_partial = on_partial
        self.on_segment = on_segment
        self.script = script or {}
        self.tail = tail or []
        self.fail_start = fail_start
        self.fail_at_chunk = fail_at_chunk
        self.count = 0

    async def start(self):
        if self.fail_start:
            raise StreamingSTTError("无法建立流式识别连接：no network")

    async def send(self, _chunk):
        self.count += 1
        if self.fail_at_chunk and self.count >= self.fail_at_chunk:
            raise StreamingSTTError("流式识别连接中断")
        for text in self.script.get(self.count, []):
            await self.on_segment(text)

    async def finish(self):
        for text in self.tail:
            await self.on_segment(text)

    async def close(self):
        pass


class DummyHub:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class StreamPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.temp_wavs: list[Path] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, **fake_kwargs) -> tuple[VoiceController, DummyHub]:
        vc = VoiceController(spike_simulation=True)
        hub = DummyHub()
        vc.spike = hub
        vc.robot_agent = RobotAgent(hub)
        vc.streaming_transcriber_factory = (
            lambda on_partial, on_segment: FakeTranscriber(on_partial, on_segment, **fake_kwargs)
        )

        def temp_path() -> str:
            path = Path(self.tmp.name) / f"fallback_{len(self.temp_wavs)}.wav"
            self.temp_wavs.append(path)
            return str(path)

        vc._temp_wav_path = temp_path
        return vc, hub

    async def run_stream(self, vc: VoiceController, source: FakeSource) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await vc.process_stream(source)
            await vc._drain_reports()
        return out.getvalue()

    async def test_first_command_runs_before_release(self) -> None:
        vc, hub = self.make(script={5: ["左转90度。"]}, tail=["然后前进20厘米。"])
        seen_at_chunk_10: list[str] = []
        def on_chunk(i: int) -> None:
            if i == 10:
                seen_at_chunk_10.extend(hub.commands)

        source = FakeSource(20, on_chunk=on_chunk)

        out = await self.run_stream(vc, source)

        self.assertEqual(seen_at_chunk_10, ["left 90"])  # 还在说话, 第一个动作已经发出
        self.assertEqual(hub.commands, ["left 90", "forward 20"])
        self.assertEqual(self.temp_wavs, [])  # 流式成功时不写临时录音
        self.assertIn("路由=快速通道", out)
        self.assertIn("说完→首个动作 入队 -", out)  # 入队时间在说完之前, 是负数

    async def test_connect_failure_falls_back_to_whole_sentence(self) -> None:
        vc, hub = self.make(fail_start=True)
        vc.audio_client.transcribe_openai = AsyncMock(return_value="前进30厘米")

        out = await self.run_stream(vc, FakeSource(10))

        self.assertIn("已回退到整句识别", out)
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertEqual(len(self.temp_wavs), 1)
        self.assertFalse(self.temp_wavs[0].exists())  # 临时录音已删除

    async def test_midway_failure_continues_with_remaining_commands(self) -> None:
        vc, hub = self.make(script={3: ["左转90度。"]}, fail_at_chunk=8)
        vc.audio_client.transcribe_openai = AsyncMock(return_value="左转90度，然后前进20厘米")

        await self.run_stream(vc, FakeSource(15))

        # 左转已经执行过, 回退后只补上前进, 不重复左转
        self.assertEqual(hub.commands, ["left 90", "forward 20"])

    async def test_midway_failure_without_match_does_not_guess(self) -> None:
        vc, hub = self.make(script={3: ["左转90度。"]}, fail_at_chunk=8)
        vc.audio_client.transcribe_openai = AsyncMock(return_value="右转45度然后跳个舞")

        out = await self.run_stream(vc, FakeSource(15))

        self.assertEqual(hub.commands, ["left 90"])
        self.assertIn("请重新说一遍", out)

    async def test_temp_audio_deleted_when_fallback_stt_fails(self) -> None:
        vc, hub = self.make(fail_start=True)
        vc.audio_client.transcribe_openai = AsyncMock(side_effect=RuntimeError("offline"))
        vc.audio_client.transcribe_whisper = AsyncMock(side_effect=RuntimeError("no model"))

        out = await self.run_stream(vc, FakeSource(10))

        self.assertEqual(hub.commands, [])
        self.assertIn("失败环节=STT", out)
        self.assertEqual(len(self.temp_wavs), 1)
        self.assertFalse(self.temp_wavs[0].exists())

    async def test_stop_word_cancels_rest_of_sentence(self) -> None:
        vc, hub = self.make(script={3: ["前进100厘米。"], 6: ["停！"]}, tail=["然后左转90度。"])

        await self.run_stream(vc, FakeSource(10))

        self.assertEqual(hub.commands, ["forward 100", "stop"])


if __name__ == "__main__":
    unittest.main()
