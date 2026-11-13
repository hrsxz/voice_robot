import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import soundfile as sf

from pc.llm import audio_utils


class SttClientReuseTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_transcriptions_create_one_client(self) -> None:
        fake_client = MagicMock()
        fake_client.audio.transcriptions.create = AsyncMock(
            return_value=SimpleNamespace(text="前进30厘米")
        )
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            wav.write_bytes(b"RIFF")
            with patch.object(audio_utils, "AsyncOpenAI", return_value=fake_client) as ctor:
                client = audio_utils.AudioClient(api_key="test-key")
                first = await client.transcribe_openai(str(wav))
                second = await client.transcribe_openai(str(wav))

        self.assertEqual(first, "前进30厘米")
        self.assertEqual(second, "前进30厘米")
        self.assertEqual(ctor.call_count, 1)
        self.assertEqual(fake_client.audio.transcriptions.create.await_count, 2)


class SpeechWavTest(unittest.TestCase):
    def test_push_to_talk_wav_is_16khz_pcm16(self) -> None:
        self.assertEqual(audio_utils.PTT_SAMPLE_RATE, 16000)
        audio = np.zeros((1600, 1), dtype=np.int16)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ptt.wav")
            audio_utils.write_speech_wav(path, audio)
            info = sf.info(path)
        self.assertEqual(info.samplerate, 16000)
        self.assertEqual(info.channels, 1)
        self.assertEqual(info.subtype, "PCM_16")


if __name__ == "__main__":
    unittest.main()


class PromptEchoTest(unittest.TestCase):
    """云端识别把提示词原样输出时要丢弃, 否则里面的"停止"会触发急停。"""

    # 回放 wake_in_middle.wav 时 gpt-4o-transcribe 的真实输出
    ECHO = ("向前走，往后退，向左转，向右转，停止，左夹子抬起来，左夹子放下来，右夹子抬起来，"
            "右夹子放下来，把夹子抬起来，把夹子放下来，厘米，度，然后，再，一共做四次。")

    def test_full_echo_is_detected(self):
        self.assertTrue(audio_utils.is_prompt_echo(self.ECHO))
        self.assertTrue(audio_utils.is_prompt_echo(audio_utils.STT_PROMPT))

    def test_partial_echo_is_detected_early(self):
        self.assertTrue(audio_utils.is_prompt_echo("向前走，往后退，向左转，", partial=True))
        self.assertTrue(audio_utils.is_prompt_echo("请用简体中文输出。机器人", partial=True))

    def test_real_commands_are_not_echo(self):
        for text in (
            "左夹子抬起来，左夹子放下来，右夹子抬起来，右夹子放下来",  # 正好是提示词的一部分
            "向前走三十厘米再左转九十度，一共做四次",
            "把夹子放下来，向前走二十厘米，把夹子抬起来，再往后退二十厘米",
            "向前走五十厘米", "停止", "向前走，", "把红色的块搬到B区",
        ):
            with self.subTest(text=text):
                self.assertFalse(audio_utils.is_prompt_echo(text))
                self.assertFalse(audio_utils.is_prompt_echo(text, partial=True))


class PromptEchoTranscribeTest(unittest.IsolatedAsyncioTestCase):
    async def test_echo_result_returns_empty_text(self):
        fake_client = MagicMock()
        fake_client.audio.transcriptions.create = AsyncMock(
            return_value=SimpleNamespace(text=PromptEchoTest.ECHO)
        )
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "a.wav"
            wav.write_bytes(b"RIFF")
            with patch.object(audio_utils, "AsyncOpenAI", return_value=fake_client):
                text = await audio_utils.AudioClient(api_key="k").transcribe_openai(str(wav))
        self.assertEqual(text, "")
