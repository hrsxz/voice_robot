"""
流式识别的音频来源。都按 24 kHz 单声道 PCM16 一块一块地产出 (bytes),
结束后 .audio 是完整的录音 (numpy int16), 流式识别失败时用它回退到整句识别。

- PushToTalkSource: 按住空格时从麦克风采集, 松开 (加一点尾音) 结束
- MicrophoneSource: 持续采集 (唤醒词模式), 由 VAD 决定一句话的开始和结束
- WavFileSource: 回放 WAV 文件, 默认按实时速度推送 (用于回放测试, 不需要真人说话)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import numpy as np

SAMPLE_RATE = 24000
CHUNK_S = 0.04


class WavFileSource:
    def __init__(self, path: str, realtime: bool = True, chunk_s: float = CHUNK_S) -> None:
        import soundfile as sf

        data, sr = sf.read(path, dtype="int16")
        if sr != SAMPLE_RATE:
            raise ValueError(f"{path}: 需要 {SAMPLE_RATE} Hz 的录音, 实际是 {sr} Hz")
        if data.ndim > 1:
            data = data[:, 0]
        self.path = path
        self.audio: np.ndarray = data
        self.realtime = realtime
        self.chunk = int(chunk_s * SAMPLE_RATE)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        start = time.perf_counter()
        for i in range(0, len(self.audio), self.chunk):
            yield self.audio[i:i + self.chunk].tobytes()
            if self.realtime:
                target = start + (i + self.chunk) / SAMPLE_RATE
                await asyncio.sleep(max(0.0, target - time.perf_counter()))
            else:
                await asyncio.sleep(0)


class MicrophoneSource:
    """持续采集麦克风 (唤醒词模式用), 每块 30 ms; 一直运行到任务被取消 (Ctrl+C)。"""

    def __init__(self, chunk_ms: int = 30) -> None:
        self.blocksize = SAMPLE_RATE * chunk_ms // 1000

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import sounddevice as sd

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def callback(indata, frame_count, time_info, status):
            del frame_count, time_info
            if status:
                print(f"录音状态: {status}")
            loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                               blocksize=self.blocksize, callback=callback):
            while True:
                yield await queue.get()


class PushToTalkSource:
    """按住空格说话, 松开结束。按下之前 0.5 s 的声音也会带上 (避免吞掉第一个字)。"""

    def __init__(
        self,
        pre_roll_s: float = 0.5,
        post_roll_s: float = 0.25,
        max_record_s: float = 60.0,
    ) -> None:
        self.pre_roll_s = pre_roll_s
        self.post_roll_s = post_roll_s
        self.max_record_s = max_record_s
        self.audio: np.ndarray = np.zeros(0, dtype=np.int16)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import threading
        from collections import deque

        import sounddevice as sd
        from pynput import keyboard

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        blocksize = int(CHUNK_S * SAMPLE_RATE)
        pre_buffer: deque[np.ndarray] = deque(maxlen=max(1, int(self.pre_roll_s / CHUNK_S)))
        post_chunks_target = max(1, int(self.post_roll_s / CHUNK_S))
        state = {"post_seen": 0}
        frames: list[np.ndarray] = []

        started = threading.Event()
        released = threading.Event()
        done = threading.Event()

        def push(chunk: np.ndarray | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, None if chunk is None else chunk.tobytes())

        def callback(indata, frame_count, time_info, status):
            del frame_count, time_info
            if status:
                print(f"录音状态: {status}")
            chunk = indata[:, 0].copy()
            if done.is_set():
                return
            if not started.is_set():
                pre_buffer.append(chunk)
                return
            frames.append(chunk)
            push(chunk)
            if released.is_set():
                state["post_seen"] += 1
                if state["post_seen"] >= post_chunks_target:
                    done.set()
                    push(None)

        def on_press(key):
            if key == keyboard.Key.space and not started.is_set():
                for chunk in list(pre_buffer):
                    frames.append(chunk)
                    push(chunk)
                started.set()
                print("开始录音...（松开空格结束）")

        def on_release(key):
            if key == keyboard.Key.space and started.is_set():
                released.set()
                return False
            return None

        print("请按住空格说话，松开结束。")
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=blocksize, callback=callback):
                record_start: float | None = None
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except TimeoutError:
                        item = b""
                    if started.is_set() and record_start is None:
                        record_start = time.perf_counter()
                    if item is None:
                        break
                    if item:
                        yield item
                    if record_start and time.perf_counter() - record_start > self.max_record_s:
                        print("录音超过上限，自动停止。")
                        done.set()
                        break
        finally:
            listener.stop()
            self.audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.int16)
