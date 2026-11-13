"""
唤醒词模式和启动选择 (改动 voice-input-modes)。

音频用合成的帧: 非零字节的帧算"人声" (假 VAD), 全零算静音;
本地识别和云端识别都用假的, 按顺序返回预先写好的文字。
"""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np

from pc.agent.robot_agent import RobotAgent
from pc.speech import vad as vad_mod
from pc.speech.conversation_gate import ConversationGate
from pc.speech.upload_audit import UploadBlocked, UploadGuard
from pc.speech.wake_loop import Probe, Segmenter, SpeechEnd, SpeechStart, UtteranceSource, WakeLoop
from pc.voice_to_robot import LaunchConfigError, VoiceController, validate_launch

FRAME = vad_mod.FRAME_BYTES
SPEECH = np.full(vad_mod.FRAME_SAMPLES, 1000, dtype=np.int16).tobytes()
SILENCE = bytes(FRAME)
FIXTURES = Path(__file__).parent / "fixtures" / "audio"


def fake_vad(frame: bytes) -> bool:
    return any(frame)


def speech(seconds: float) -> list[bytes]:
    return [SPEECH] * round(seconds * 1000 / vad_mod.FRAME_MS)


def silence(seconds: float) -> list[bytes]:
    return [SILENCE] * round(seconds * 1000 / vad_mod.FRAME_MS)


async def play(frames: list[bytes]):
    for f in frames:
        yield f
        # 让 worker 和处理任务有机会运行 (相当于真实麦克风两帧之间的时间)
        for _ in range(3):
            await asyncio.sleep(0)


class LaunchConfigTest(unittest.TestCase):
    def test_valid_combinations(self):
        for input_mode in ("ptt", "wake", "cli"):
            with self.subTest(input_mode=input_mode):
                self.assertEqual(validate_launch(input_mode, "stream"), input_mode)

    def test_old_name_mic_means_ptt(self):
        self.assertEqual(validate_launch("mic", "stream"), "ptt")

    def test_cli_with_realtime_is_rejected(self):
        with self.assertRaisesRegex(LaunchConfigError, "键盘输入不能使用 realtime"):
            validate_launch("cli", "realtime")

    def test_realtime_available_for_microphone_modes(self):
        for input_mode in ("ptt", "wake"):
            with self.subTest(input_mode=input_mode):
                self.assertEqual(validate_launch(input_mode, "realtime"), input_mode)

    def test_unknown_values(self):
        with self.assertRaises(LaunchConfigError):
            validate_launch("keyboard", "stream")
        with self.assertRaises(LaunchConfigError):
            validate_launch("ptt", "fast")


class SegmenterTest(unittest.TestCase):
    def feed(self, seg: Segmenter, frames: list[bytes]) -> list[tuple[int, object]]:
        events = []
        for i, f in enumerate(frames):
            for ev in seg.feed(f, fake_vad(f)):
                events.append((i, ev))
        return events

    def test_utterance_ends_after_configured_silence(self):
        for silence_ms, expected_frames in ((700, 24), (300, 10), (1000, 34)):
            with self.subTest(silence_ms=silence_ms):
                seg = Segmenter(end_silence_ms=silence_ms)
                frames = silence(0.3) + speech(1.0) + silence(2.0)
                events = self.feed(seg, frames)
                last_speech = len(silence(0.3)) + len(speech(1.0)) - 1
                end_at = [i for i, ev in events if isinstance(ev, SpeechEnd)]
                self.assertEqual(end_at, [last_speech + expected_frames])

    def test_default_end_silence_is_700ms(self):
        # 24 帧, 第一个 ≥ 700 ms 的整帧
        self.assertEqual(Segmenter().end_silence_frames * vad_mod.FRAME_MS, 720)

    def test_short_pause_does_not_end_utterance(self):
        seg = Segmenter(end_silence_ms=700)
        events = self.feed(seg, speech(1.0) + silence(0.5) + speech(1.0) + silence(1.0))
        self.assertEqual(sum(isinstance(ev, SpeechEnd) for _, ev in events), 1)

    def test_probes_are_at_most_2_5_seconds(self):
        seg = Segmenter()
        events = self.feed(seg, speech(6.0) + silence(1.0))
        probes = [ev for _, ev in events if isinstance(ev, Probe)]
        self.assertEqual(len(probes), 3)
        self.assertTrue(all(len(p.frames) * vad_mod.FRAME_MS <= 2500 for p in probes))
        self.assertTrue(probes[-1].final)
        self.assertFalse(probes[0].final)

    def test_piece_with_almost_no_speech_is_not_probed(self):
        seg = Segmenter()
        # 2.5 秒整好切在说完的地方, 剩下的片段只有静音: 不交给本地识别
        events = self.feed(seg, speech(2.5) + silence(1.0))
        probes = [ev for _, ev in events if isinstance(ev, Probe)]
        self.assertEqual(len(probes), 1)

    def test_start_includes_pre_roll(self):
        seg = Segmenter()
        events = self.feed(seg, silence(1.0) + speech(0.5) + silence(1.0))
        start = next(ev for _, ev in events if isinstance(ev, SpeechStart))
        self.assertGreaterEqual(len(start.frames), 3)

    def test_invalid_silence(self):
        with self.assertRaises(ValueError):
            Segmenter(end_silence_ms=0)


@unittest.skipUnless((FIXTURES / "l1_forward_30.wav").exists(),
                     "需要合成测试录音: python -m scripts.make_test_audio")
class SegmenterRealAudioTest(unittest.TestCase):
    """合成语音 + 真实 webrtcvad: 说完后静音达到设定时长, 这句话结束。"""

    def test_end_after_silence_on_synthetic_speech(self):
        import soundfile as sf

        audio, sr = sf.read(FIXTURES / "l1_forward_30.wav", dtype="int16")
        self.assertEqual(sr, vad_mod.SAMPLE_RATE)
        audio = np.concatenate([audio, np.zeros(vad_mod.SAMPLE_RATE * 2, dtype=np.int16)])
        frames = [audio[i:i + vad_mod.FRAME_SAMPLES].tobytes()
                  for i in range(0, len(audio) - vad_mod.FRAME_SAMPLES + 1, vad_mod.FRAME_SAMPLES)]
        vad = vad_mod.WebRtcVad()
        for silence_ms in (700, 400):
            with self.subTest(silence_ms=silence_ms):
                seg = Segmenter(end_silence_ms=silence_ms)
                voiced = [vad(f) for f in frames]
                ends = []
                for i, f in enumerate(frames):
                    for ev in seg.feed(f, voiced[i]):
                        if isinstance(ev, SpeechEnd):
                            ends.append(i)
                last_voiced = max(i for i, v in enumerate(voiced) if v)
                self.assertEqual(len(ends), 1)
                self.assertEqual(ends[0] - last_voiced, seg.end_silence_frames)


class FakeHandlers:
    def __init__(self, gate: ConversationGate, window_results=None) -> None:
        self.gate = gate
        self.stops = 0
        self.wake_utterances: list[np.ndarray] = []
        self.window_utterances: list[UtteranceSource] = []
        self.window_results = list(window_results or [])
        self.tones: list[tuple[str, float]] = []
        self.loop: WakeLoop | None = None

    async def on_local_stop(self) -> None:
        self.stops += 1

    async def on_wake_utterance(self, audio) -> bool:
        self.wake_utterances.append(audio)
        return True

    async def on_window_utterance(self, source: UtteranceSource) -> bool:
        self.window_utterances.append(source)
        async for _ in source:
            pass
        return self.window_results.pop(0) if self.window_results else True

    def play_tone(self, kind: str) -> float:
        self.tones.append((kind, self.loop.audio_time if self.loop else 0.0))
        return 0.3


class ScriptedLocalSTT:
    def __init__(self, texts: list[str]) -> None:
        self.texts = list(texts)
        self.calls = 0

    async def __call__(self, _audio16k) -> str:
        self.calls += 1
        return self.texts.pop(0) if self.texts else ""


class WakeLoopTest(unittest.IsolatedAsyncioTestCase):
    def make(self, local_texts, window_results=None, end_silence_ms=700):
        holder: dict = {}
        gate = ConversationGate(timeout_seconds=20, clock=lambda: holder["loop"].audio_time)
        handlers = FakeHandlers(gate, window_results)
        stt = ScriptedLocalSTT(local_texts)
        loop = WakeLoop(gate, handlers, stt, fake_vad, end_silence_ms=end_silence_ms,
                        log=lambda _msg: None)
        holder["loop"] = loop
        handlers.loop = loop
        return loop, handlers, stt, gate

    async def test_chat_without_wake_word_does_nothing(self):
        loop, handlers, stt, gate = self.make(["今天天气真不错", "我们中午去吃面条吧"])
        await loop.run(play(speech(1.5) + silence(1.0) + speech(2.0) + silence(1.0)))
        self.assertEqual(stt.calls, 2)
        self.assertFalse(gate.is_active())
        self.assertEqual(handlers.wake_utterances, [])
        self.assertEqual(handlers.window_utterances, [])

    async def test_continuous_commands_in_window(self):
        # 唤醒 -> 1 秒后说第一条命令 -> 10 秒后说第二条命令, 都不需要再说唤醒词
        loop, handlers, stt, gate = self.make(["小蓝小蓝"])
        frames = (speech(1.0) + silence(1.0) + speech(1.0) + silence(10.0)
                  + speech(1.0) + silence(1.0))
        await loop.run(play(frames))
        self.assertEqual(stt.calls, 1)  # 窗口内的话不经过本地识别
        self.assertEqual(len(handlers.wake_utterances), 1)
        self.assertEqual(len(handlers.window_utterances), 2)
        self.assertTrue(all(src.closed for src in handlers.window_utterances))
        self.assertTrue(gate.is_active())

    async def test_window_times_out_after_20s(self):
        loop, handlers, stt, gate = self.make(["小蓝小蓝", "聊天"])
        frames = speech(1.0) + silence(21.0) + speech(1.0) + silence(1.0)
        await loop.run(play(frames))
        self.assertEqual(handlers.window_utterances, [])  # 超时后又回到本地检测
        self.assertEqual(stt.calls, 2)
        self.assertFalse(gate.is_active())

    async def test_exit_phrase_closes_window(self):
        # 第一句窗口内的话是"结束对话" (handler 返回 False), 之后的话要重新唤醒
        loop, handlers, stt, gate = self.make(["小蓝小蓝", "前进"], window_results=[False])
        frames = (speech(1.0) + silence(1.0) + speech(1.0) + silence(1.0)
                  + speech(1.0) + silence(1.0))
        await loop.run(play(frames))
        self.assertEqual(len(handlers.window_utterances), 1)
        self.assertEqual(stt.calls, 2)
        self.assertFalse(gate.is_active())

    async def test_wake_word_followed_by_command_in_one_breath(self):
        # 3 秒的一句话: 第一个 2.5 秒片段里有唤醒词, 整句 (从唤醒片段开始) 交给云端
        loop, handlers, stt, _gate = self.make(["小蓝小蓝前进", "三十厘米"])
        await loop.run(play(speech(3.0) + silence(1.0)))
        self.assertEqual(stt.calls, 1)  # 唤醒后同一句话的后续片段不再本地识别
        self.assertEqual(len(handlers.wake_utterances), 1)
        audio = handlers.wake_utterances[0]
        self.assertGreaterEqual(len(audio), int(3.0 * vad_mod.SAMPLE_RATE))
        self.assertEqual(handlers.window_utterances, [])
        self.assertEqual(handlers.tones[0][0], "wake")  # 说完才响, 不打断用户

    async def test_wake_word_in_later_piece_uploads_from_there(self):
        loop, handlers, _stt, _gate = self.make(["嗯那个我想想", "小蓝小蓝向左转"])
        await loop.run(play(speech(4.0) + silence(1.0)))
        audio = handlers.wake_utterances[0]
        # 只从唤醒词所在的片段开始上传 (剩下 1.5 秒语音 + 句末静音), 前面 2.5 秒的聊天不上传
        self.assertLess(len(audio), int(2.5 * vad_mod.SAMPLE_RATE))

    async def test_local_stop_without_wake(self):
        loop, handlers, _stt, gate = self.make(["急停"])
        await loop.run(play(speech(0.6) + silence(1.0)))
        self.assertEqual(handlers.stops, 1)
        self.assertFalse(gate.is_active())

    async def test_command_right_after_wake_word_utterance(self):
        # 说完唤醒词立刻接着说命令 (本地识别还没出结果): 第二句也要进入对话窗口
        loop, handlers, _stt, _gate = self.make(["小蓝小蓝"])
        await loop.run(play(speech(1.0) + silence(0.75) + speech(1.0) + silence(1.0)))
        self.assertEqual(len(handlers.window_utterances), 1)

    async def test_tone_is_not_heard_as_speech(self):
        # 唤醒提示音播放期间麦克风收到的声音 (这里用人声帧模拟) 被丢弃
        loop, handlers, stt, _gate = self.make(["小蓝小蓝"])
        frames = speech(1.0) + silence(0.75) + silence(0.06) + speech(0.3) + silence(2.0)
        await loop.run(play(frames))
        self.assertEqual([k for k, _ in handlers.tones], ["wake"])
        self.assertEqual(handlers.window_utterances, [])
        self.assertEqual(stt.calls, 1)

    async def test_end_tone_does_not_delay_processing(self):
        loop, handlers, _stt, _gate = self.make(["小蓝小蓝"])
        await loop.run(play(speech(1.0) + silence(1.0) + speech(1.0) + silence(1.0)))
        kinds = [k for k, _ in handlers.tones]
        self.assertEqual(kinds, ["wake", "end"])
        # 一句话结束时 source 立即关闭 (识别处理开始), 提示音同时在后台播放
        self.assertTrue(handlers.window_utterances[0].closed)


class DummyHub:
    simulate = True

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def send(self, cmd: str) -> None:
        self.commands.append(cmd)


class FakeLocalTranscriber:
    def __init__(self, texts) -> None:
        self.texts = list(texts)
        self.calls = 0

    def load(self) -> None:
        pass

    def transcribe(self, _audio16k) -> str:
        self.calls += 1
        return self.texts.pop(0) if self.texts else ""


class CountingTranscriber:
    """假的流式识别: 记录被创建的次数 (= 上传次数)。"""
    created = 0

    def __init__(self, on_partial, on_segment, segments=None) -> None:
        CountingTranscriber.created += 1
        self.on_segment = on_segment
        self.segments = segments or []

    async def start(self):
        pass

    async def send(self, _chunk):
        pass

    async def finish(self):
        for text in self.segments:
            await self.on_segment(text)

    async def close(self):
        pass


class WakeControllerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        CountingTranscriber.created = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make(self, local_texts, cloud_text="", stream_segments=None):
        log = Path(self.tmp.name) / "uploads.jsonl"
        vc = VoiceController(spike_simulation=True, upload_log_path=str(log))
        hub = DummyHub()
        vc.spike = hub
        vc.robot_agent = RobotAgent(hub)
        vc.upload_guard.set_mode("wake", str(log))
        vc.local_transcriber = FakeLocalTranscriber(local_texts)
        vc.audio_client.api_key = "test"
        vc.audio_client.transcribe_openai = AsyncMock(return_value=cloud_text)
        vc.audio_client.transcribe_whisper = AsyncMock(return_value="")
        vc.audio_client.start_tone = lambda kind: 0.3
        vc.streaming_transcriber_factory = (
            lambda on_partial, on_segment: CountingTranscriber(
                on_partial, on_segment, stream_segments)
        )
        vc._temp_wav_path = lambda: str(Path(self.tmp.name) / "seg.wav")
        return vc, hub, log

    async def run_wake(self, vc, frames):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await vc.run_wake(play(frames), vad=fake_vad)
            await vc._drain_reports()
        return out.getvalue()

    async def test_chat_recording_uploads_nothing(self):
        vc, hub, log = self.make(["今天天气真不错", "小明你好", "把藍色的機幕放到", "到脱子上"])
        frames = (speech(1.5) + silence(1.0) + speech(1.0) + silence(1.0)
                  + speech(4.0) + silence(1.0))
        await self.run_wake(vc, frames)
        self.assertEqual(vc.audio_client.transcribe_openai.await_count, 0)
        self.assertEqual(CountingTranscriber.created, 0)
        self.assertEqual(vc.upload_guard.records, [])
        self.assertFalse(log.exists())
        self.assertEqual(hub.commands, [])

    async def test_wake_word_then_command_in_one_sentence(self):
        vc, hub, log = self.make(["小蓝小蓝"], cloud_text="小蓝小蓝，前进30厘米。")
        out = await self.run_wake(vc, speech(2.0) + silence(1.0))
        self.assertEqual(hub.commands, ["forward 30"])
        self.assertIn("唤醒片段识别", out)
        self.assertEqual(len(vc.upload_guard.records), 1)
        self.assertTrue(vc.upload_guard.records[0]["window_open"])
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
        # 审计日志不含识别内容
        self.assertNotIn("前进", log.read_text(encoding="utf-8"))

    async def test_local_false_wake_is_not_executed(self):
        # 本地误唤醒, 云端结果里没有唤醒词: 不执行
        vc, hub, _log = self.make(["小蓝小蓝"], cloud_text="把蓝色的积木放到桌子上")
        await self.run_wake(vc, speech(2.0) + silence(1.0))
        self.assertEqual(hub.commands, [])

    async def test_window_uses_streaming_and_exit_phrase(self):
        vc, hub, _log = self.make(["小蓝小蓝"], cloud_text="小蓝小蓝",
                                  stream_segments=["左转90度。"])
        frames = speech(1.0) + silence(1.0) + speech(1.0) + silence(1.0)
        await self.run_wake(vc, frames)
        self.assertEqual(hub.commands, ["left 90"])
        self.assertEqual(CountingTranscriber.created, 1)
        self.assertTrue(vc.conversation_gate.is_active())

        vc2, hub2, _ = self.make(["小蓝小蓝"], cloud_text="小蓝小蓝",
                                 stream_segments=["结束对话。"])
        await self.run_wake(vc2, frames)
        self.assertEqual(hub2.commands, [])
        self.assertFalse(vc2.conversation_gate.is_active())

    async def test_guard_blocks_upload_before_wake(self):
        gate = ConversationGate()
        guard = UploadGuard(gate, input_mode="wake", log_path=Path(self.tmp.name) / "u.jsonl")
        with self.assertRaises(UploadBlocked):
            guard.check("file")
        gate.wake()
        guard.check("file")
        self.assertEqual(len(guard.records), 1)

    async def test_guard_does_not_block_push_to_talk(self):
        guard = UploadGuard(ConversationGate(), input_mode="ptt")
        guard.check("stream")
        self.assertIsNone(guard.log_path)
        self.assertEqual(len(guard.records), 1)


if __name__ == "__main__":
    unittest.main()
