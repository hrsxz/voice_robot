import asyncio
import os
import tempfile
import time
from typing import Optional

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

    async def transcribe(self, wav_path: str) -> str:
        """Transcribe WAV to text. Tries (in order):
        1. project `pc.llm.whisper_stt.transcribe(wav_path)` if present
        2. OpenAI Transcriptions via new `openai` client (if configured)
        If none available, raises RuntimeError with guidance.
        """
        if _HAS_WHISPER and hasattr(_whisper, 'transcribe'):
            res = _whisper.transcribe(wav_path)
            if asyncio.iscoroutine(res):
                return await res
            return res

        if self.api_key:
            try:
                from openai import OpenAI

                client = OpenAI(api_key=self.api_key)
                try:
                    with open(wav_path, 'rb') as f:
                        resp = client.audio.transcriptions.create(model='gpt-4o-transcribe', file=f)
                    if hasattr(resp, 'text'):
                        return resp.text
                    if getattr(resp, 'data', None):
                        return resp.data[0].get('text', '')
                    return str(resp)
                except Exception:
                    raise
            except Exception as e:
                raise RuntimeError(
                    'OpenAI transcription failed or not supported in this '
                    'environment: ' + str(e)
                ) from e

        raise RuntimeError(
            'No STT backend available. Provide '
            'pc.llm.whisper_stt.transcribe or set OPENAI_API_KEY'
        )

    def speak_text(self, text: str, voice: Optional[str] = None) -> None:
        """Speak text using local TTS. Tries pyttsx3 first, then edge-tts if installed.
        This is synchronous and blocks until speech finishes.
        """
        # pyttsx3 (offline)
        try:
            import pyttsx3
            engine = pyttsx3.init()
            if voice:
                try:
                    engine.setProperty('voice', voice)
                except Exception:
                    pass
            engine.say(text)
            engine.runAndWait()
            return
        except Exception:
            pass

        # edge-tts (async) - run synchronously
        try:
            import asyncio as _asyncio

            import edge_tts

            async def _speak():
                communicate = edge_tts.Communicate(text, voice or 'en-US-AriaNeural')
                await communicate.save('tmp_tts.mp3')
            _asyncio.run(_speak())
            # try to play the mp3 with simple player
            try:
                from playsound import playsound
                playsound('tmp_tts.mp3')
            except Exception:
                pass
            return
        except Exception:
            pass

        raise RuntimeError('No TTS backend available. Install pyttsx3 or edge-tts')
