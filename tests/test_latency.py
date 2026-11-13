import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pc.utils.latency import LATENCY_LOG_ENV, CommandTimer, LatencyLogger
from pc.voice_to_robot import VoiceController


class CommandTimerTest(unittest.TestCase):
    def test_durations_and_summary_for_fast_path(self) -> None:
        timer = CommandTimer(input_mode="mic", route="fast_path")
        timer.mark("recording_end", at=10.0)
        timer.mark("stt_done", at=10.8)
        timer.mark("route_done", at=10.802)
        timer.mark("ble_sent", at=10.83)
        timer.mark("hub_done", at=12.83)
        timer.commands = ["forward 30"]

        self.assertEqual(
            timer.durations_ms(), {"stt": 800, "route": 2, "ble": 28, "hub": 2000}
        )
        self.assertEqual(timer.total_ms(), 2830)
        line = timer.summary_line()
        self.assertIn("路由=快速通道", line)
        self.assertIn("STT 800ms", line)
        self.assertIn("快速通道 2ms", line)
        self.assertNotIn("LLM", line)
        self.assertIn("forward 30", line)

    def test_llm_route_shows_llm_segment(self) -> None:
        timer = CommandTimer(route="llm")
        timer.mark("recording_end", at=0.0)
        timer.mark("stt_done", at=1.0)
        timer.mark("route_done", at=2.5)

        line = timer.summary_line()
        self.assertIn("路由=LLM", line)
        self.assertIn("LLM 1500ms", line)

    def test_same_stage_is_only_marked_once(self) -> None:
        timer = CommandTimer()
        timer.mark("ble_sent", at=1.0)
        timer.mark("ble_sent", at=2.0)
        self.assertEqual(timer.marks["ble_sent"], 1.0)

    def test_failed_stage_is_shown(self) -> None:
        timer = CommandTimer()
        timer.mark("recording_end", at=0.0)
        timer.fail("stt", "network down")
        timer.fail("llm", "later failure is ignored")

        line = timer.summary_line()
        self.assertIn("失败环节=STT (network down)", line)
        self.assertEqual(timer.failed_stage, "stt")


class SttFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_stt_failure_is_reported_in_summary(self) -> None:
        vc = VoiceController(spike_simulation=True)
        vc.audio_client.transcribe_openai = AsyncMock(side_effect=RuntimeError("offline"))
        vc.audio_client.transcribe_whisper = AsyncMock(side_effect=RuntimeError("no model"))

        timer = CommandTimer(input_mode="mic")
        timer.mark("recording_end")
        text = await vc.transcribe_audio("dummy.wav", timer)

        self.assertEqual(text, "")
        self.assertEqual(timer.failed_stage, "stt")
        line = timer.summary_line()
        self.assertIn("失败环节=STT", line)
        self.assertIn("offline", line)


class LatencyLogTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_commands_write_three_records_without_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "logs" / "latency.jsonl"
            vc = VoiceController(spike_simulation=True, latency_log_path=str(log_path))
            await vc.robot_agent.connect()

            for text in ("前进30厘米", "左转90度", "把夹子抬起来"):
                timer = CommandTimer(input_mode="cli")
                timer.mark("recording_end")
                await vc.handle_text(text, timer)
            await vc._drain_reports()

            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            records = [json.loads(line) for line in lines]
            self.assertEqual(
                [r["commands"] for r in records],
                [["forward 30"], ["left 90"], ["gripper_up"]],
            )
            for record in records:
                self.assertEqual(record["route"], "fast_path")
                self.assertIn("durations_ms", record)
                serialized = json.dumps(record, ensure_ascii=False).lower()
                self.assertNotIn(".wav", serialized)
                self.assertNotIn("audio", serialized)

    def test_logger_uses_env_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env.jsonl"
            with patch.dict(os.environ, {LATENCY_LOG_ENV: str(path)}):
                logger = LatencyLogger()
            self.assertTrue(logger.enabled)
            logger.write(CommandTimer(route="llm"))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_logger_disabled_without_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            logger = LatencyLogger()
        self.assertFalse(logger.enabled)
        logger.write(CommandTimer())  # 不应报错


if __name__ == "__main__":
    unittest.main()
