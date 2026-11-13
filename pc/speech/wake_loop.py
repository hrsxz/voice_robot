"""
唤醒词模式的主循环。

麦克风音频 (24 kHz, 每帧 30 ms) 先经过 VAD 切成一句一句 (Segmenter):
- 未唤醒: 每句话按不超过 2.5 秒切片, 交给**本地**识别, 只判断唤醒词和急停;
  音频不上传。检测到唤醒词后, 从唤醒词所在的片段到这句话结束的音频交给云端识别,
  去掉唤醒词后剩下的部分当作命令 ("小蓝小蓝，前进30厘米")。
- 已唤醒 (对话窗口内): 一开口就把音频交给流式识别, 静音 end_silence_ms 后这句话结束。
  每处理完一句话窗口重新计时, 超时或说"结束对话"就回到未唤醒状态。

realtime 链路 (handlers.window_streaming() 为 True) 的对话窗口不按句处理:
从唤醒词开始, 窗口打开期间的**所有**音频按顺序连续交给 handlers (on_window_start /
on_window_audio), 由服务端 VAD 划分轮次; 本地切句只用来给窗口续时, 以及本地检测"急停"。
窗口超时或结束对话时调用 on_window_end, 之后音频不再交出去。

提示音在后台播放, 不等待; 播放期间麦克风输入换成静音, 避免提示音被当成一句话。
实际的识别、执行由 handlers (VoiceController) 提供, 这里只负责状态和调度,
所以可以用 WAV 文件和假的识别器做测试。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from pc.speech.conversation_gate import ConversationGate
from pc.speech.vad import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, to_16k

END_SILENCE_MS = 700
PROBE_MS = 2500  # 本地识别一个片段的最长时长
MAX_UTTERANCE_S = 15.0
PRE_ROLL_FRAMES = 10  # 开口前 300 ms 一起带上, 避免吞掉第一个字
START_WINDOW = 5
START_VOICED = 3  # 最近 5 帧里有 3 帧是人声才算开口
MIN_PROBE_VOICED = 5  # 片段里人声少于 150 ms 不做本地识别 (几乎全是静音的片段会被模型"脑补"出文字)
MUTE_MARGIN_S = 0.1
SILENCE_FRAME = bytes(FRAME_BYTES)


# ---------- 切句 ----------

@dataclass
class SpeechStart:
    frames: list[bytes]  # 这句话到目前为止的音频 (和 Segmenter 共用同一个列表, 会继续增长)


@dataclass
class Probe:
    offset: int  # 片段在这句话中的起始帧
    frames: list[bytes]
    final: bool  # 这句话的最后一个片段


@dataclass
class SpeechEnd:
    frames: list[bytes]
    reason: str  # "silence" / "max"


Event = SpeechStart | Probe | SpeechEnd


class Segmenter:
    """按 VAD 结果把连续的帧切成一句一句, 并在每句话内每 probe_ms 切出一个片段。"""

    def __init__(
        self,
        end_silence_ms: int = END_SILENCE_MS,
        probe_ms: int = PROBE_MS,
        max_utterance_s: float = MAX_UTTERANCE_S,
    ) -> None:
        if end_silence_ms <= 0:
            raise ValueError("end_silence_ms 必须大于 0")
        self.end_silence_frames = max(1, -(-end_silence_ms // FRAME_MS))  # 向上取整
        self.probe_frames = max(1, probe_ms // FRAME_MS)
        self.max_frames = int(max_utterance_s * 1000 / FRAME_MS)
        self._pre: deque[tuple[bytes, bool]] = deque(maxlen=PRE_ROLL_FRAMES)
        self._window: deque[bool] = deque(maxlen=START_WINDOW)
        self._reset()

    def _reset(self) -> None:
        self.frames: list[bytes] | None = None
        self._voiced: list[bool] = []
        self._piece_start = 0
        self._silence = 0

    @property
    def in_utterance(self) -> bool:
        return self.frames is not None

    def feed(self, frame: bytes, is_speech: bool) -> list[Event]:
        if self.frames is None:
            self._pre.append((frame, is_speech))
            self._window.append(is_speech)
            if sum(self._window) < START_VOICED:
                return []
            self.frames = [f for f, _ in self._pre]
            self._voiced = [v for _, v in self._pre]
            self._pre.clear()
            self._window.clear()
            self._silence = 0
            return [SpeechStart(self.frames)]

        self.frames.append(frame)
        self._voiced.append(is_speech)
        self._silence = 0 if is_speech else self._silence + 1
        events: list[Event] = []

        if self._silence >= self.end_silence_frames:
            return self._end("silence")
        if len(self.frames) >= self.max_frames:
            return self._end("max")
        if len(self.frames) - self._piece_start >= self.probe_frames:
            probe = self._probe(final=False)
            if probe is not None:
                events.append(probe)
        return events

    def flush(self) -> list[Event]:
        """音频结束: 正在说的这句话立即结束。"""
        return self._end("flush") if self.frames is not None else []

    def _probe(self, final: bool) -> Probe | None:
        start, end = self._piece_start, len(self.frames or [])
        self._piece_start = end
        voiced = self._voiced[start:end]
        if sum(voiced) < MIN_PROBE_VOICED:
            return None
        while end > start and not self._voiced[end - 1]:  # 去掉片段末尾的静音
            end -= 1
        return Probe(offset=start, frames=list(self.frames[start:end]), final=final)

    def _end(self, reason: str) -> list[Event]:
        events: list[Event] = []
        probe = self._probe(final=True)
        if probe is not None:
            events.append(probe)
        events.append(SpeechEnd(frames=self.frames or [], reason=reason))
        self._reset()
        return events


# ---------- 一句话的音频流 ----------

class UtteranceSource:
    """
    把一句话的音频一块一块交给流式识别 (和 PushToTalkSource 接口相同),
    结束后 .audio 是完整录音。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._frames: list[bytes] = []
        self.closed = False
        self.audio: np.ndarray = np.zeros(0, dtype=np.int16)

    def push(self, frame: bytes) -> None:
        if self.closed:
            return
        self._frames.append(frame)
        self._queue.put_nowait(frame)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.audio = frames_to_array(self._frames)
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while (item := await self._queue.get()) is not None:
            yield item


def frames_to_array(frames: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(frames), dtype=np.int16).copy()


# ---------- 主循环 ----------

class WakeHandlers(Protocol):
    async def on_local_stop(self) -> None: ...

    async def on_wake_utterance(self, audio: np.ndarray) -> bool:
        """唤醒片段 (24 kHz int16) 交给云端识别并执行命令。返回 True 表示执行了命令。"""
        ...

    async def on_window_utterance(self, source: UtteranceSource) -> bool:
        """对话窗口内的一句话。返回 False 表示用户结束了对话。"""
        ...

    def play_tone(self, kind: str) -> float:
        """在后台开始播放提示音 ("wake" / "end"), 立即返回提示音时长 (秒)。"""
        ...

    # 以下是 realtime 链路用的可选方法 (没有 window_streaming 或它返回 False 时按句处理):
    # window_streaming() -> bool: 对话窗口是否把音频连续交给 handlers
    # on_window_start(frames: list[bytes]): 窗口打开, frames 从唤醒词所在的片段开始
    # on_window_audio(frame: bytes): 窗口内的每一帧 (同步调用, 不能阻塞)
    # on_window_end(): 窗口关闭, 之后不再有音频


@dataclass
class _Utterance:
    frames: list[bytes]
    mode: str = "sleep"  # sleep: 本地检测唤醒词; window: 对话窗口内, 交给流式识别
    source: UtteranceSource | None = None
    ended: bool = False
    woke: bool = False
    wake_offset: int = 0
    tone_pending: bool = False
    stopped: bool = False
    streamed: bool = False  # realtime: 音频已经连续交给 handlers, 不再按句处理
    local_texts: list[str] = field(default_factory=list)


class WakeLoop:
    def __init__(
        self,
        gate: ConversationGate,
        handlers: WakeHandlers,
        local_stt: Callable[[np.ndarray], Awaitable[str]],
        vad: Callable[[bytes], bool],
        end_silence_ms: int = END_SILENCE_MS,
        probe_ms: int = PROBE_MS,
        max_utterance_s: float = MAX_UTTERANCE_S,
        log: Callable[[str], None] = print,
    ) -> None:
        self.gate = gate
        self.handlers = handlers
        self.local_stt = local_stt
        self.vad = vad
        self.log = log
        self.segmenter = Segmenter(end_silence_ms, probe_ms, max_utterance_s)
        self.audio_time = 0.0  # 已经处理的音频时长 (秒)
        self.local_runs = 0
        self.local_ms: list[float] = []  # 每个片段的本地识别耗时
        self._muted_until = 0.0
        self._utt: _Utterance | None = None
        self._jobs: asyncio.Queue = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()
        self._window_open = False
        self._streaming = False  # realtime: 正在把窗口内的音频连续交给 handlers

    async def run(self, frames: AsyncIterable[bytes]) -> None:
        worker = asyncio.create_task(self._worker())
        buf = b""
        try:
            async for chunk in frames:
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    self._step(frame)
        finally:
            self._handle_events(self.segmenter.flush())
            await self._jobs.put(None)
            await worker
            while self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- 每一帧 (同步, 不能阻塞) ---

    def _step(self, frame: bytes) -> None:
        if self.audio_time < self._muted_until:
            frame, is_speech = SILENCE_FRAME, False  # 正在播放提示音
        else:
            is_speech = self.vad(frame)
        self.audio_time += FRAME_MS / 1000

        if self._streaming and not self._streams():
            self._streaming = False  # realtime 失败, 已经切换到按句处理
        if self._utt is None and self._window_open and not self.gate.is_active():
            self._window_open = False
            self.log("连续对话已超时，需要重新唤醒")
            self._end_stream()
        elif self._streaming and self._utt is None and not self.gate.is_active():
            self._end_stream()  # "结束对话"

        utt = self._utt
        events = self.segmenter.feed(frame, is_speech)
        if utt is not None and utt.mode == "window" and utt.source is not None:
            utt.source.push(frame)
        if self._streaming:
            self.handlers.on_window_audio(frame)  # type: ignore[attr-defined]
        self._handle_events(events)

    def _handle_events(self, events: list[Event]) -> None:
        for ev in events:
            if isinstance(ev, SpeechStart):
                utt = _Utterance(frames=ev.frames)
                self._utt = utt
                if self._streaming and self.gate.is_active():
                    utt.mode = "window"
                    utt.streamed = True
                    self.gate.renew()
                elif self.gate.is_active():
                    self._open_window(utt)
                else:
                    # 前一句话可能还在本地识别 (说完唤醒词马上接着说命令), 由 worker 按顺序决定
                    self._jobs.put_nowait(("start", utt, None))
            elif isinstance(ev, Probe):
                if self._utt is not None and self._utt.mode == "sleep":
                    self._jobs.put_nowait(("probe", self._utt, ev))
                elif self._utt is not None and self._utt.streamed:
                    # realtime 窗口内: 本地只检测"急停", 和云端转写谁先到用谁
                    self._jobs.put_nowait(("stop_probe", self._utt, ev))
            elif isinstance(ev, SpeechEnd):
                utt = self._utt
                self._utt = None
                if utt is None:
                    continue
                utt.ended = True
                if utt.mode == "window" and utt.source is not None:
                    utt.source.close()
                    self._tone("end")
                elif utt.mode == "window" and utt.streamed:
                    if self.gate.is_active():
                        self.gate.renew()
                    self._tone("end")
                else:
                    self._jobs.put_nowait(("end", utt, None))

    def _open_window(self, utt: _Utterance) -> None:
        utt.mode = "window"
        utt.source = UtteranceSource()
        for f in utt.frames:
            utt.source.push(f)
        if utt.ended:
            utt.source.close()
        self.gate.renew()
        self._spawn(self._run_window(utt.source))

    def _streams(self) -> bool:
        fn = getattr(self.handlers, "window_streaming", None)
        return bool(fn is not None and fn())

    def _start_stream(self, utt: _Utterance, offset: int) -> None:
        """realtime: 从唤醒词所在的片段开始, 把音频连续交给 handlers。"""
        frames = list(utt.frames[offset:])
        utt.streamed = True
        current = self._utt
        if current is not None and current is not utt and current.mode == "sleep":
            # 唤醒词那句话已经结束, 用户紧接着又开口了 (本地识别还没出结果): 这句也带上
            frames += current.frames
            current.mode = "window"
            current.streamed = True
        self._streaming = True
        self.handlers.on_window_start(frames)  # type: ignore[attr-defined]

    def _end_stream(self) -> None:
        if not self._streaming:
            return
        self._streaming = False
        try:
            self.handlers.on_window_end()  # type: ignore[attr-defined]
        except Exception as exc:
            self.log(f"关闭对话窗口出错：{type(exc).__name__}: {exc}")

    def _tone(self, kind: str) -> None:
        try:
            duration = self.handlers.play_tone(kind)
        except Exception as exc:
            self.log(f"提示音播放失败：{exc}")
            return
        self._muted_until = max(self._muted_until, self.audio_time + duration + MUTE_MARGIN_S)

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- 未唤醒时的处理 (按顺序) ---

    async def _worker(self) -> None:
        while (job := await self._jobs.get()) is not None:
            kind, utt, ev = job
            try:
                if kind == "stop_probe":
                    await self._stop_probe(ev)
                    continue
                if utt.mode != "sleep":
                    continue
                if kind == "start":
                    if self.gate.is_active():
                        self._open_window(utt)
                elif kind == "probe":
                    await self._probe(utt, ev)
                elif kind == "end":
                    await self._end_sleep_utterance(utt)
            except Exception as exc:
                self.log(f"唤醒处理出错：{type(exc).__name__}: {exc}")

    async def _probe(self, utt: _Utterance, ev: Probe) -> None:
        if utt.woke or utt.stopped:
            return
        audio16k = to_16k(frames_to_array(ev.frames))
        self.local_runs += 1
        start = time.perf_counter()
        text = await self.local_stt(audio16k)
        ms = (time.perf_counter() - start) * 1000
        self.local_ms.append(ms)
        utt.local_texts.append(text)
        cls = ConversationGate.classify_local(text)
        if text:
            mark = {"wake": "  → 唤醒", "stop": "  → 急停"}.get(cls or "", "")
            self.log(f"[本地 {ms:.0f} ms] {text}{mark}")
        if cls == "stop":
            utt.stopped = True
            await self.handlers.on_local_stop()
        elif cls == "wake":
            utt.woke = True
            utt.wake_offset = ev.offset
            self.gate.wake()
            self._window_open = True
            self.log(f"已唤醒，{self.gate.timeout_seconds:.0f} 秒内可以连续说命令")
            if self._streams():
                self._start_stream(utt, ev.offset)
            if utt.ended:
                self._tone("wake")
            else:
                utt.tone_pending = True  # 还在说话 (可能紧接着说命令), 说完再响

    async def _stop_probe(self, ev: Probe) -> None:
        text = await self.local_stt(to_16k(frames_to_array(ev.frames)))
        if ConversationGate.classify_local(text) == "stop":
            self.log(f"[本地] {text}  → 急停")
            await self.handlers.on_local_stop()

    async def _end_sleep_utterance(self, utt: _Utterance) -> None:
        if not utt.woke:
            return
        if utt.tone_pending:
            self._tone("wake")
        if utt.streamed:
            if self.gate.is_active():
                self.gate.renew()
            return  # realtime: 音频已经连续交出去了
        audio = frames_to_array(utt.frames[utt.wake_offset:])
        await self.handlers.on_wake_utterance(audio)
        if self.gate.is_active():
            self.gate.renew()

    async def _run_window(self, source: UtteranceSource) -> None:
        try:
            keep = await self.handlers.on_window_utterance(source)
        except Exception as exc:
            self.log(f"处理这句话时出错：{type(exc).__name__}: {exc}")
            keep = True
        if keep:
            self.gate.renew()
            self._window_open = True
        else:
            self.gate.deactivate()
            self._window_open = False


__all__ = [
    "END_SILENCE_MS",
    "PROBE_MS",
    "SAMPLE_RATE",
    "Probe",
    "Segmenter",
    "SpeechEnd",
    "SpeechStart",
    "UtteranceSource",
    "WakeHandlers",
    "WakeLoop",
    "frames_to_array",
]
