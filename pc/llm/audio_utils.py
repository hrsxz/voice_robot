import asyncio
import os
import tempfile
import time
from typing import Optional

from openai import OpenAI

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

    def __init__(self, provider: str = 'whisper', api_key: Optional[str] = None):
        self.provider = provider
        self.api_key = api_key or os.getenv('OPENAI_API_KEY')

    async def record(self, duration: float = 3.0, out_path: Optional[str] = None) -> str:
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

        fs = 16000
        print(f'Recording {duration}s to {tmp}...')
        data = sd.rec(int(duration * fs), samplerate=fs, channels=1)
        sd.wait()
        sf.write(tmp, data, fs)
        return tmp

    async def transcribe_whisper(self, wav_path: str) -> str:
        if not (_HAS_WHISPER and hasattr(_whisper, "transcribe")):
            raise RuntimeError("Local whisper unavailable")
        res = _whisper.transcribe(wav_path)
        if asyncio.iscoroutine(res):
            return await res
        return res

    async def transcribe_openai(self, wav_path: str) -> str:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        client = OpenAI(api_key=self.api_key)
        with open(wav_path, "rb") as f:
            resp = client.audio.transcriptions.create(model="gpt-4o-transcribe", file=f)
        if hasattr(resp, "text"):
            return resp.text
        if getattr(resp, "data", None):
            return resp.data[0].get("text", "")
        return str(resp)

    def speak_text(self, text: str, voice: Optional[str] = None) -> None:
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
            except Exception:
                raise RuntimeError(
                    "edge-tts synthesized audio, but no playback backend found. "
                    f"Audio saved at: {out_mp3}"
                )

        except Exception:
            pass

        raise RuntimeError("No TTS backend available. Install pyttsx3 or edge-tts (+ pygame optional).")

    async def record_push_to_talk(self, out_path: str | None = None) -> str:
        import time

        import sounddevice as sd
        import soundfile as sf
        from pynput import keyboard

        fs = 16000
        channels = 1
        frames = []
        is_recording = False
        stop_recording = False
        space_down = False

        tmp = out_path or os.path.join(
            tempfile.gettempdir(), f"vr_ptt_{int(time.time())}.wav"
        )

        def on_press(key):
            nonlocal is_recording, space_down
            if key == keyboard.Key.space and not space_down:
                space_down = True
                is_recording = True
                print("开始录音...（松开空格结束）")

        def on_release(key):
            nonlocal stop_recording, space_down
            if key == keyboard.Key.space and space_down:
                space_down = False
                stop_recording = True
                return False  # 停止 listener

        print("请按住空格说话，松开结束。")
        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.start()

        # 等待真正开始
        while not is_recording:
            await asyncio.sleep(0.01)

        with sd.InputStream(samplerate=fs, channels=channels, dtype="float32") as stream:
            while not stop_recording:
                data, _ = stream.read(1024)
                frames.append(data.copy())
                await asyncio.sleep(0)

        listener.join()

        if not frames:
            raise RuntimeError("没有录到音频，请重试。")

        import numpy as np
        audio = np.concatenate(frames, axis=0)
        sf.write(tmp, audio, fs)
        print(f"录音保存: {tmp}")
        return tmp