import asyncio
import os
import re
import ssl
import tempfile
import time
from typing import Any

import httpx
import truststore
from openai import AsyncOpenAI
from rapidfuzz import fuzz

# 按空格录音的采样率: 16 kHz 单声道 int16 对语音识别足够, 上传的数据量是 44.1 kHz float 的约 1/5
PTT_SAMPLE_RATE = 16000
STT_MODEL = "gpt-4o-transcribe"
# 不加提示时模型会混入繁体字 (後、進、釐) 和同音错字 ("做四次" -> "坐四次"), 快速通道就认不出来了
# (见 openspec/changes/voice-streaming/spike.md)
# 左右夹子要写完整说法: 只写"把夹子放下来"时, 模型会把"左夹子放下来"也听成"把夹子放下来"
STT_PROMPT = (
    "请用简体中文输出。机器人控制命令：向前走，往后退，向左转，向右转，停止，"
    "左夹子抬起来，左夹子放下来，右夹子抬起来，右夹子放下来，"
    "把夹子抬起来，把夹子放下来，厘米，度，然后，再，一共做四次。"
)

# 音频很短或不清楚时, 模型有时会把提示词原样输出 ("向前走，往后退，……停止，……"),
# 里面的"停止"会被当成急停 (见 openspec/changes/voice-input-modes/measurements.md)
_PROMPT_CONTENT = re.sub(r"\W", "", STT_PROMPT)
_ECHO_ANCHORS = (_PROMPT_CONTENT, _PROMPT_CONTENT[_PROMPT_CONTENT.index("向前走"):])


def is_prompt_echo(text: str, partial: bool = False) -> bool:
    """
    识别结果是不是提示词的回显。
    partial=True 用于识别中的中间文字: 只要开头 9 个字以上和提示词开头一致就算
    (尽早拦截, 避免误急停)。
    """
    content = re.sub(r"\W", "", text or "")
    min_len = 9 if partial else 12
    if len(content) >= min_len and any(
        fuzz.ratio(content, anchor[:len(content)]) >= 90 for anchor in _ECHO_ANCHORS
    ):
        return True
    return not partial and len(content) >= 20 and any(
        fuzz.ratio(content, anchor) >= 75 for anchor in _ECHO_ANCHORS
    )


# 提示音: (频率 Hz, 时长 ms)
TONES = {"wake": (900, 250), "end": (500, 300)}

try:
    # 项目内可能已有的 STT 实现
    from pc.speech import whisper_stt as _whisper
    _HAS_WHISPER = True
except Exception:
    _whisper = None
    _HAS_WHISPER = False


class AudioClient:
    """Audio helper: recording, STT (transcribe) and TTS (speak).

    Usage:
      ac = AudioClient(provider='whisper' or 'openai')
      wav = await ac.record(3.0)
      text = await ac.transcribe(wav)
      ac.speak_text('你好')
    """

    def __init__(self, provider: str = 'whisper', api_key: str | None = None):
        self.provider = provider
        self.api_key = api_key or os.getenv('OPENAI_API_KEY')
        # 复用同一个异步客户端, 避免每次识别都重新建立连接
        self._openai_client: Any = None

    def _get_openai_client(self) -> Any:
        if self._openai_client is None:
            verify: Any
            try:
                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                verify = True  # 回退到默认 CA 验证
            self._openai_client = AsyncOpenAI(
                api_key=self.api_key,
                http_client=httpx.AsyncClient(verify=verify, timeout=30.0),
            )
        return self._openai_client

    def start_tone(self, kind: str) -> float:
        """
        在后台开始播放提示音, 立即返回提示音时长 (秒), 不等播放完。
        kind: "wake" (唤醒成功, 可以说命令) / "end" (一句话结束)。
        """
        frequency, duration_ms = TONES[kind]
        try:
            import numpy as np
            import sounddevice as sd

            sample_rate = 44100
            sample_count = int(sample_rate * duration_ms / 1000)
            timeline = np.arange(sample_count) / sample_rate
            audio = 0.35 * np.sin(2 * np.pi * frequency * timeline)
            sd.play(audio.astype(np.float32), sample_rate)  # 不调用 sd.wait(), 不阻塞
        except Exception as exc:
            print(f"提示音播放失败：{exc}")
            return 0.0
        return duration_ms / 1000

    async def record(self, duration: float = 3.0, out_path: str | None = None) -> str:
        """Record audio from default microphone and save as WAV.
        Returns path to WAV file.
        Requires `sounddevice` and `soundfile` packages.
        """
        tmp = out_path or os.path.join(tempfile.gettempdir(), f'vr_record_{int(time.time())}.wav')
        try:
            import sounddevice as sd
            import soundfile as sf
        except Exception as e:
            raise RuntimeError(
                'Recording requires sounddevice and soundfile: '
                'pip install sounddevice soundfile'
            ) from e

        fs = 44100
        print(f'Recording {duration}s to {tmp}...')
        data = sd.rec(int(duration * fs), samplerate=fs, channels=1)
        sd.wait()
        sf.write(tmp, data, fs)
        return tmp

    async def transcribe_whisper(self, wav_path: str) -> str:
        if not (_HAS_WHISPER and hasattr(_whisper, "transcribe")):
            raise RuntimeError("Local Whisper unavailable")

        return await asyncio.to_thread(_whisper.transcribe, wav_path)

    async def transcribe_openai(self, wav_path: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        client = self._get_openai_client()
        with open(wav_path, "rb") as f:
            resp = await client.audio.transcriptions.create(
                model=STT_MODEL, file=f, language="zh", prompt=STT_PROMPT
            )
        if hasattr(resp, "text"):
            text = resp.text
        elif getattr(resp, "data", None):
            text = resp.data[0].get("text", "")
        else:
            text = str(resp)
        if is_prompt_echo(text):
            print("[STT] 识别结果是提示词的回显（音频太短或不清楚），已丢弃")
            return ""
        return text

    def speak_text(self, text: str, voice: str | None = None) -> None:
        """Speak text using local TTS.
        Priority:
        1) pyttsx3 (offline, sync)
        2) edge-tts + pygame playback (if pygame available)
        3) edge-tts file output only (open by system on Windows)
        """
        # 1) pyttsx3 (offline)
        try:
            import pyttsx3

            engine = pyttsx3.init()
            if voice:
                try:
                    engine.setProperty("voice", voice)
                except Exception:
                    pass
            engine.say(text)
            engine.runAndWait()
            return
        except Exception:
            pass

        # 2) edge-tts synthesis
        try:
            import asyncio as _asyncio
            import os
            import tempfile
            import uuid

            import edge_tts

            out_mp3 = os.path.join(tempfile.gettempdir(), f"vr_tts_{uuid.uuid4().hex}.mp3")

            async def _speak():
                communicate = edge_tts.Communicate(text, voice or "en-US-AriaNeural")
                await communicate.save(out_mp3)

            _asyncio.run(_speak())

            # 2a) try pygame for blocking playback
            try:
                import pygame

                pygame.mixer.init()
                pygame.mixer.music.load(out_mp3)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    _asyncio.run(_asyncio.sleep(0.05))
                pygame.mixer.music.unload()
                pygame.mixer.quit()

                try:
                    os.remove(out_mp3)
                except Exception:
                    pass
                return
            except Exception:
                pass

            # 2b) last fallback: open file with system default player (Windows)
            try:
                os.startfile(out_mp3)  # type: ignore[attr-defined]
                return
            except Exception as exc:
                raise RuntimeError(
                    "edge-tts synthesized audio, but no playback backend found. "
                    f"Audio saved at: {out_mp3}"
                ) from exc

        except Exception:
            pass

        raise RuntimeError(
            "No TTS backend available. Install pyttsx3 or edge-tts (+ pygame optional)."
        )

    async def record_push_to_talk(self, out_path: str | None = None) -> str:
        import threading
        import time
        from collections import deque

        import numpy as np
        import sounddevice as sd
        from pynput import keyboard

        fs = PTT_SAMPLE_RATE
        channels = 1
        blocksize = 512
        wait_press_seconds = 500.0
        max_record_seconds = 60.0
        pre_roll_ms = 500
        post_roll_ms = 250

        frames: list[np.ndarray] = []
        pre_buffer = deque(
            maxlen=max(1, int((pre_roll_ms / 1000.0) * fs / blocksize))
        )

        started = threading.Event()
        released = threading.Event()
        done = threading.Event()

        post_chunks_target = max(1, int((post_roll_ms / 1000.0) * fs / blocksize))
        post_chunks_seen = 0

        tmp = out_path or os.path.join(
            tempfile.gettempdir(), f"vr_ptt_{int(time.time())}.wav"
        )

        def callback(indata, frame_count, time_info, status):
            nonlocal post_chunks_seen
            if status:
                print(f"录音状态: {status}")

            chunk = indata.copy()
            pre_buffer.append(chunk)

            if not started.is_set():
                return

            frames.append(chunk)

            if released.is_set():
                post_chunks_seen += 1
                if post_chunks_seen >= post_chunks_target:
                    done.set()

        def on_press(key):
            if key == keyboard.Key.space and not started.is_set():
                frames.extend(list(pre_buffer))
                started.set()
                print("开始录音...（松开空格结束）")
            elif key == keyboard.Key.esc and started.is_set():
                released.set()
                done.set()
                return False

        def on_release(key):
            if key == keyboard.Key.space and started.is_set():
                released.set()
                return False

        print("请按住空格说话，松开结束。")
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()

        try:
            with sd.InputStream(
                samplerate=fs,
                channels=channels,
                dtype="int16",
                device=None,
                blocksize=blocksize,
                callback=callback,
            ):
                press_deadline = time.time() + wait_press_seconds
                while not started.is_set():
                    if time.time() >= press_deadline:
                        raise RuntimeError("等待按下空格超时，请重试。")
                    await asyncio.sleep(0.01)

                record_start = time.time()
                while not done.is_set():
                    if time.time() - record_start >= max_record_seconds:
                        print("录音超过30秒，自动停止。")
                        done.set()
                        break
                    await asyncio.sleep(0.01)
        finally:
            listener.stop()
            listener.join(timeout=1.0)

        if not frames:
            raise RuntimeError("没有录到音频，请重试。")

        audio = np.concatenate(frames, axis=0)
        write_speech_wav(tmp, audio, fs)
        print(f"录音保存: {tmp}")
        return tmp


def write_speech_wav(path: str, audio: Any, sample_rate: int = PTT_SAMPLE_RATE) -> None:
    """把语音写成单声道 PCM16 WAV（识别用的统一格式）。"""
    import soundfile as sf

    sf.write(path, audio, sample_rate, subtype="PCM_16")
