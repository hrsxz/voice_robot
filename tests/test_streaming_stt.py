import asyncio
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pc import constants
from pc.speech.streaming_stt import StreamingSTTError, StreamingTranscriber, wait_background_closes


def ev(etype: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=etype, **kwargs)


class FakeConn:
    """按脚本回放服务端事件的假连接。commit 时会触发 on_commit 里预设的事件。"""

    def __init__(self, on_append=None, on_commit=None) -> None:
        self.events: asyncio.Queue = asyncio.Queue()
        self.appended = 0
        self.commits = 0
        self.session_config = None
        self._on_append = on_append or {}
        self._on_commit = on_commit or []
        outer = self

        class Session:
            async def update(self, session):
                outer.session_config = session

        class Buffer:
            async def append(self, audio):
                outer.appended += 1
                for e in outer._on_append.get(outer.appended, []):
                    outer.events.put_nowait(e)

            async def commit(self):
                outer.commits += 1
                for e in outer._on_commit:
                    outer.events.put_nowait(e)

        self.session = Session()
        self.input_audio_buffer = Buffer()

    async def recv(self):
        item = await self.events.get()
        if isinstance(item, Exception):
            raise item
        return item


class FakeManager:
    def __init__(self, conn: FakeConn, fail: Exception | None = None) -> None:
        self.conn = conn
        self.fail = fail
        self.closed = False

    async def __aenter__(self):
        if self.fail:
            raise self.fail
        return self.conn

    async def __aexit__(self, *args):
        self.closed = True


CHUNK = b"\x00\x00" * 960  # 40 ms @ 24 kHz
SPEAKING = {1: [ev("input_audio_buffer.speech_started")]}


class TranscriberTest(unittest.IsolatedAsyncioTestCase):
    def make(self, conn: FakeConn, fail: Exception | None = None, timeout: float = 1.0):
        self.partials: list[str] = []
        self.segments: list[str] = []
        self.manager = FakeManager(conn, fail)

        async def on_partial(text):
            self.partials.append(text)

        async def on_segment(text):
            self.segments.append(text)

        return StreamingTranscriber(
            on_partial, on_segment, connect=lambda key: self.manager, finish_timeout=timeout
        )

    async def test_segments_arrive_while_speaking_and_in_order(self) -> None:
        conn = FakeConn(
            on_append={
                # 说第一句时服务端按停顿切出语段 1
                10: [ev("input_audio_buffer.committed", item_id="a")],
                12: [ev("conversation.item.input_audio_transcription.delta", item_id="a",
                        delta="向前走"),
                     ev("conversation.item.input_audio_transcription.completed", item_id="a",
                        transcript="向前走五十厘米。")],
                # 松开空格时还在说第二句
                14: [ev("input_audio_buffer.speech_started")],
            },
            on_commit=[
                ev("input_audio_buffer.committed", item_id="b"),
                ev("conversation.item.input_audio_transcription.completed", item_id="b",
                   transcript="然后向右转九十度。"),
            ],
        )
        stt = self.make(conn)
        await stt.start()
        for i in range(20):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
            if i == 15:
                # 还没松开空格, 第一个语段已经回调了
                self.assertEqual(self.segments, ["向前走五十厘米。"])
        result = await stt.finish()

        self.assertEqual(result, ["向前走五十厘米。", "然后向右转九十度。"])
        self.assertEqual(self.partials, ["向前走"])
        self.assertEqual(conn.commits, 1)
        await wait_background_closes()  # 连接在后台关闭
        self.assertTrue(self.manager.closed)
        cfg = conn.session_config["audio"]["input"]
        self.assertEqual(cfg["format"]["rate"], 24000)
        self.assertEqual(cfg["transcription"]["model"], "gpt-4o-transcribe")
        self.assertIn("简体中文", cfg["transcription"]["prompt"])

    async def test_out_of_order_completion_is_emitted_in_commit_order(self) -> None:
        conn = FakeConn(on_append=SPEAKING, on_commit=[
            ev("input_audio_buffer.committed", item_id="x"),
            ev("input_audio_buffer.committed", item_id="y"),
            ev("conversation.item.input_audio_transcription.completed", item_id="y",
               transcript="第二句"),
            ev("conversation.item.input_audio_transcription.completed", item_id="x",
               transcript="第一句"),
        ])
        stt = self.make(conn)
        await stt.start()
        for _ in range(5):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
        await stt.finish()
        self.assertEqual(self.segments, ["第一句", "第二句"])

    async def test_buffer_too_small_on_release_is_ignored(self) -> None:
        conn = FakeConn(on_append=SPEAKING, on_commit=[ev("error", error=SimpleNamespace(
            message="buffer too small. Expected at least 100ms",
            code="input_audio_buffer_commit_empty",
        ))])
        stt = self.make(conn)
        await stt.start()
        for _ in range(5):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
        self.assertEqual(await stt.finish(), [])

    async def test_short_tail_is_not_committed(self) -> None:
        conn = FakeConn(on_append=SPEAKING)
        stt = self.make(conn)
        await stt.start()
        await stt.send(CHUNK)  # 只有 40 ms, 少于 100 ms 不补交
        await stt.finish()
        self.assertEqual(conn.commits, 0)

    async def test_connect_failure_raises(self) -> None:
        stt = self.make(FakeConn(), fail=ConnectionError("no network"))
        with self.assertRaises(StreamingSTTError):
            await stt.start()

    async def test_server_error_raises_on_finish(self) -> None:
        conn = FakeConn(on_append={1: [ev("error", error=SimpleNamespace(
            message="internal error", code="server_error"))]})
        stt = self.make(conn)
        await stt.start()
        await stt.send(CHUNK)
        await asyncio.sleep(0)
        with self.assertRaises(StreamingSTTError):
            for _ in range(5):
                await stt.send(CHUNK)
            await stt.finish()

    async def test_no_commit_when_server_already_split_the_last_segment(self) -> None:
        # 服务端已经切完最后一个语段, 剩下的只是静音: 松开时不补交
        conn = FakeConn(on_append={
            1: [ev("input_audio_buffer.speech_started")],
            3: [ev("input_audio_buffer.speech_stopped"),
                ev("input_audio_buffer.committed", item_id="a"),
                ev("conversation.item.input_audio_transcription.completed", item_id="a",
                   transcript="左转90度。")],
        })
        stt = self.make(conn)
        await stt.start()
        for _ in range(10):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
        self.assertEqual(await stt.finish(), ["左转90度。"])
        self.assertEqual(conn.commits, 0)

    async def test_segment_committed_after_release_is_not_lost(self) -> None:
        # 竞态: 松开时服务端已经发了"停止说话", 但"已提交"和识别结果在松开之后才到
        conn = FakeConn(on_append={
            1: [ev("input_audio_buffer.speech_started")],
            4: [ev("input_audio_buffer.speech_stopped")],
        })
        stt = self.make(conn)
        await stt.start()
        for _ in range(6):
            await stt.send(CHUNK)
            await asyncio.sleep(0)

        async def late_events():
            await asyncio.sleep(0.05)
            conn.events.put_nowait(ev("input_audio_buffer.committed", item_id="late"))
            conn.events.put_nowait(ev("conversation.item.input_audio_transcription.completed",
                                      item_id="late", transcript="右夹子放下来。"))

        late = asyncio.create_task(late_events())
        self.assertEqual(await stt.finish(), ["右夹子放下来。"])
        self.assertEqual(conn.commits, 0)  # 已经停止说话, 不补交
        await late

    async def test_quick_release_before_any_vad_event(self) -> None:
        # 很短的一句"停", 说完立刻松开: 服务端还没来得及报告"开始说话", 也必须补交并拿到结果
        conn = FakeConn(on_commit=[
            ev("input_audio_buffer.committed", item_id="s"),
            ev("conversation.item.input_audio_transcription.completed", item_id="s",
               transcript="停！"),
        ])
        stt = self.make(conn)
        await stt.start()
        for _ in range(5):  # 200 ms
            await stt.send(CHUNK)
        self.assertEqual(await stt.finish(), ["停！"])
        self.assertEqual(conn.commits, 1)

    async def test_missing_result_times_out(self) -> None:
        conn = FakeConn(on_append=SPEAKING,
                        on_commit=[ev("input_audio_buffer.committed", item_id="z")])
        stt = self.make(conn, timeout=0.2)
        await stt.start()
        for _ in range(5):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
        with self.assertRaises(StreamingSTTError):
            await stt.finish()


@unittest.skipUnless(
    os.getenv("VOICE_ROBOT_NETWORK_TESTS") == "1",
    "联网测试: 设置 VOICE_ROBOT_NETWORK_TESTS=1 并准备好 tests/fixtures/audio/ 后运行",
)
class RealStreamingReplayTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_segment_before_audio_ends(self) -> None:
        from pc.speech.audio_sources import WavFileSource

        path = constants.project_root_path / "tests" / "fixtures" / "audio" / "l2_forward_right.wav"
        self.assertTrue(Path(path).exists(), "先运行 python -m scripts.make_test_audio")
        segment_times: list[float] = []

        async def on_partial(_text):
            pass

        async def on_segment(_text):
            segment_times.append(time.perf_counter())

        stt = StreamingTranscriber(on_partial, on_segment)
        await stt.start()
        async for chunk in WavFileSource(str(path)):
            await stt.send(chunk)
        audio_end = time.perf_counter()
        segments = await stt.finish()

        self.assertGreaterEqual(len(segments), 2)
        self.assertLess(segment_times[0], audio_end)


if __name__ == "__main__":
    unittest.main()


class PromptEchoStreamTest(TranscriberTest):
    async def test_echo_segment_and_partials_are_dropped(self) -> None:
        echo = "向前走，往后退，向左转，向右转，停止，左夹子抬起来，左夹子放下来，右夹子抬起来。"
        conn = FakeConn(on_append={
            2: [ev("input_audio_buffer.committed", item_id="a"),
                ev("conversation.item.input_audio_transcription.delta", item_id="a",
                   delta="向前走，往后退，向左转，向右转，停止"),
                ev("conversation.item.input_audio_transcription.completed", item_id="a",
                   transcript=echo),
                ev("input_audio_buffer.committed", item_id="b"),
                ev("conversation.item.input_audio_transcription.completed", item_id="b",
                   transcript="左转90度")],
        })
        stt = self.make(conn)
        await stt.start()
        for _ in range(4):
            await stt.send(CHUNK)
            await asyncio.sleep(0)
        await stt.finish()
        self.assertEqual(self.partials, [])  # 含"停止"的中间文字不能交给急停检测
        self.assertEqual(self.segments, ["左转90度"])
