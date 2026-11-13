import argparse
import asyncio
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from pc.agent import robot_agent
from pc.agent.robot_agent import CommandBatch, CommandRecord
from pc.llm import audio_utils, command_matcher, intent_mapper, llm_client
from pc.llm.realtime_pipeline import RealtimePipeline
from pc.llm.streaming_router import LLM, STOPPED, StreamingRouter
from pc.speech.audio_sources import SAMPLE_RATE as STREAM_SAMPLE_RATE
from pc.speech.audio_sources import MicrophoneSource, PushToTalkSource
from pc.speech.conversation_gate import ConversationGate, is_exit_phrase, strip_wake_word
from pc.speech.realtime_session import DEFAULT_MODEL as REALTIME_MODEL
from pc.speech.realtime_session import RealtimeSessionError
from pc.speech.streaming_stt import StreamingTranscriber, wait_background_closes
from pc.speech.upload_audit import UploadGuard
from pc.speech.wake_loop import END_SILENCE_MS, UtteranceSource, WakeLoop
from pc.spike_communication.spikehub import SpikeHub
from pc.utils import utils
from pc.utils.latency import CommandTimer, LatencyLogger

INPUT_MODES = ("ptt", "wake", "cli")
PIPELINES = ("stream", "realtime")
HUBS = ("sim", "ble")
FIELDS = ("sim", "none")
DEFAULT_TASK_MODEL = "gpt-5.5"  # 和 pc.task.agent.DEFAULT_TASK_MODEL 一致 (这里不导入, 启动更快)
_INPUT_ALIASES = {"mic": "ptt", "microphone": "ptt"}
# realtime 链路: 语音直接交给实时模型, 由模型给出函数调用 (改动 voice-realtime-pipeline)
REALTIME_AVAILABLE = True


class LaunchConfigError(ValueError):
    """启动参数组合不合法或当前不可用。"""


def validate_launch(input_mode: str, pipeline: str, has_api_key: bool = True) -> str:
    """检查输入方式和语音链路的组合, 返回规范化的输入方式; 不合法时抛出 LaunchConfigError。"""
    input_mode = _INPUT_ALIASES.get(input_mode, input_mode)
    if input_mode not in INPUT_MODES:
        raise LaunchConfigError(f"未知的输入方式：{input_mode}（可选：{' / '.join(INPUT_MODES)}）")
    if pipeline not in PIPELINES:
        raise LaunchConfigError(f"未知的语音链路：{pipeline}（可选：{' / '.join(PIPELINES)}）")
    if pipeline == "realtime":
        if input_mode == "cli":
            raise LaunchConfigError("键盘输入不能使用 realtime 链路，请使用 --pipeline stream")
        if not REALTIME_AVAILABLE:
            raise LaunchConfigError(
                "realtime 链路还没有实现（改动 voice-realtime-pipeline），请使用 --pipeline stream"
            )
        if not has_api_key:
            raise LaunchConfigError("realtime 链路需要 OPENAI_API_KEY")
    return input_mode


class VoiceController:
    def __init__(
        self,
        spike_simulation: bool = True,
        fast_path: bool = True,
        latency_log_path: str | None = None,
        streaming: bool = True,
        end_silence_ms: int = END_SILENCE_MS,
        upload_log_path: str | None = None,
        realtime_model: str = REALTIME_MODEL,
        task_runtime: Any = None,
        speak_progress: bool = False,
    ):
        self.audio_client = audio_utils.AudioClient()
        self.conversation_gate = ConversationGate(timeout_seconds=20)
        # 所有向云端发送音频的地方都先经过它: 唤醒词模式下未唤醒时禁止上传, 并记录审计日志
        self.upload_guard = UploadGuard(self.conversation_gate, log_path=upload_log_path)
        self._upload_log_path = upload_log_path
        # 唤醒词模式: 句末静音多久算一句话结束
        self.end_silence_ms = end_silence_ms
        # 唤醒词模式的本地识别 (第一次用到时创建)
        self.local_transcriber: Any = None
        self._llm_model: str | None = None
        self.wake_loop: WakeLoop | None = None
        self.llm_client = llm_client.LLMClient()
        self.spike = SpikeHub(simulate=spike_simulation)
        self.robot_agent = robot_agent.RobotAgent(hub=self.spike)

        # 按空格模式下边说边识别 (需要 OPENAI_API_KEY); False 则说完再整句识别
        self.streaming = streaming
        # 可替换的流式识别客户端 (测试时注入假的)
        self.streaming_transcriber_factory: Any = StreamingTranscriber

        # 本地快速通道: 简单命令不经过 LLM; 设为 False 可退回全部走 LLM (用于对比)
        self.fast_path = fast_path
        # 耗时日志文件: 参数为空时读取环境变量 VOICE_ROBOT_LATENCY_LOG, 都没有就不写文件
        self.latency_logger = LatencyLogger(latency_log_path)
        self._pending_reports: set[asyncio.Task] = set()

        # 当前使用的语音链路; realtime 失败时自动切换到 stream
        self.pipeline = "stream"
        self.realtime_model = realtime_model
        self.realtime: RealtimePipeline | None = None
        # 可替换的 realtime 连接 (测试时注入假的); None = 真实接口
        self.realtime_connect: Any = None
        self._rt_queue: asyncio.Queue | None = None
        self._rt_sender: asyncio.Task | None = None
        self._input_mode = "ptt"

        # 任务层 (改动 task-layer): 目标级请求交给任务代理; None = 没有启用
        self.task_runtime = task_runtime
        self.speak_progress = speak_progress  # 任务进度同时语音播报 (不阻塞)
        self._pending_answer: asyncio.Future | None = None  # 任务代理在等用户回答
        self._speech_tasks: set[asyncio.Future] = set()

    # ---------- 任务层 ----------

    def start_task(self, goal: str, timer: CommandTimer | None = None) -> str:
        """目标级请求交给任务层, 在后台执行; 返回一句说明 (realtime 链路把它回给模型)。"""
        if timer is not None:
            timer.route = "task"
            timer.mark("route_done")
        if self.task_runtime is None:
            msg = "任务层没有启用（用 --field sim 在模拟器里运行；摄像头到货后才能接实车）"
            print(f"[任务] {goal}：{msg}")
            return f"被拒绝：{msg}"
        from pc.task.runtime import TaskBusyError

        try:
            self.task_runtime.start(goal, ask=self.ask_user, on_done=self._on_task_done)
        except TaskBusyError as exc:
            print(f"[任务] {exc}")
            return f"被拒绝：{exc}"
        print(f"[任务] 开始：{goal}")
        return "已交给任务层"

    def task_progress(self, msg: str) -> None:
        print(f"[任务] {msg}")
        if self.speak_progress:
            fut = asyncio.ensure_future(asyncio.to_thread(self.audio_client.speak_text, msg))
            self._speech_tasks.add(fut)
            fut.add_done_callback(self._speech_tasks.discard)

    async def _on_task_done(self, result: Any) -> None:
        from pc.task.agent import STATUS_LABELS

        label = STATUS_LABELS.get(result.status, result.status)
        print(f"[任务] {label}：{result.message}"
              f"（技能调用 {result.skill_calls} 次，LLM {result.llm_rounds} 轮，"
              f"用时 {result.elapsed_s:.0f} s）")

    async def ask_user(self, question: str, options: list[str]) -> str | None:
        """任务代理的反问: 显示 (可选播报) 问题, 等下一句话 (语音或键盘) 作为回答。"""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_answer = fut
        try:
            return await fut
        finally:
            if self._pending_answer is fut:
                self._pending_answer = None

    def answer_pending(self) -> bool:
        return self._pending_answer is not None and not self._pending_answer.done()

    def answer(self, text: str) -> None:
        if self.answer_pending():
            assert self._pending_answer is not None
            print(f"[任务] 回答：{text}")
            self._pending_answer.set_result(text)

    async def transcribe_audio(self, wav_path: str, timer: CommandTimer | None = None) -> str:
        errors: list[str] = []
        try:
            self.upload_guard.check("file")
            text = await self.audio_client.transcribe_openai(wav_path)
            if text.strip():
                print("OpenAI STT:", text)
                return text
        except Exception as exc:
            print(f"OpenAI STT failed, fallback to local: {exc}")
            errors.append(f"OpenAI: {exc}")

        try:
            text = await self.audio_client.transcribe_whisper(wav_path)
            print("Local STT:", text)
            return text
        except Exception as exc:
            print(f"Local STT failed: {exc}")
            errors.append(f"本地: {exc}")

        if timer is not None:
            timer.fail("stt", "; ".join(errors))
        return ""

    async def get_input_text(self, mode: str) -> tuple[str, CommandTimer]:
        if mode == "cli":
            raw = await asyncio.to_thread(input, "> ")
            timer = CommandTimer(input_mode="cli")
            timer.mark("recording_end")
            return utils.normalize_text(raw), timer

        # 按空格录音, 说完再整句识别 (streaming=False 或没有 API 密钥时)
        if mode in ("ptt", "mic", "microphone"):
            wav_path = await self.audio_client.record_push_to_talk()
            timer = CommandTimer(input_mode="mic")
            timer.mark("recording_end")

            try:
                text = await self.transcribe_audio(wav_path, timer)
                if text:
                    timer.mark("stt_done")
                return utils.normalize_text(text), timer
            finally:
                Path(wav_path).unlink(missing_ok=True)

        raise ValueError(f"unknown mode: {mode}")

    def _enqueue(self, batch: CommandBatch, cmd: str) -> CommandRecord:
        if batch.timer is not None:
            batch.timer.mark("first_enqueued")
        return self.robot_agent.enqueue(batch, {"cmd": cmd})

    # ---------- realtime 链路的执行层接口 (RealtimeHost) ----------

    def new_batch(self, timer: CommandTimer) -> CommandBatch:
        return self.robot_agent.new_batch(timer)

    def enqueue_cmd(self, batch: CommandBatch, cmd: str) -> CommandRecord:
        return self._enqueue(batch, cmd)

    def reject_cmd(self, batch: CommandBatch, cmd: str, reason: str) -> CommandRecord:
        return self.robot_agent.reject(batch, cmd, reason)

    def schedule_report(self, timer: CommandTimer, batch: CommandBatch | None) -> None:
        self._schedule_report(timer, batch)

    async def _run_llm_into(
        self,
        batch: CommandBatch,
        text: str,
        executed: list[str],
        llm_model: str | None = None,
    ) -> None:
        """
        把文字交给 LLM, LLM 每输出完整的一行 (一个动作) 就立即校验并放进执行队列。
        executed: 这句话里已经由快速通道执行的动作, 告诉 LLM 不要重复。
        LLM 不可用或调用失败时拒绝执行, 不按关键词猜测。
        """
        timer = batch.timer
        if timer is not None and timer.route is None:
            timer.route = "llm"
        count = 0
        try:
            async for step in self.llm_client.generate_stream(
                text, executed=executed, model=llm_model
            ):
                if step.get("action") == "run_task":
                    # 目标级请求: 交给任务层, 这句话剩下的输出不再执行
                    args = step.get("args") if isinstance(step.get("args"), dict) else {}
                    goal = args.get("goal")
                    self.start_task(goal.strip() if isinstance(goal, str) and goal.strip()
                                    else text, timer)
                    return
                cmd = intent_mapper.step_to_cmd(step)
                if cmd:
                    print("LLM step:", cmd)
                    self._enqueue(batch, cmd)
                    count += 1
        except llm_client.LLMUnavailableError as exc:
            if timer is not None:
                timer.fail("llm", str(exc))
            print(f"无法识别：{text}（{exc}）")
            return
        except Exception as exc:
            if timer is not None:
                timer.fail("llm", f"{type(exc).__name__}: {exc}")
            print(f"无法识别：{text}（LLM 调用失败：{exc}）")
            return
        if timer is not None:
            timer.mark("route_done")
        if count == 0:
            print(f"无法识别：{text}")

    def _report(self, timer: CommandTimer) -> None:
        print(timer.summary_line())
        self.latency_logger.write(timer)

    async def _report_when_done(self, timer: CommandTimer, batch: CommandBatch | None) -> None:
        # 在后台等这一句话的命令都执行完 (执行队列每发一个动作都会等 Hub 完成),
        # 不阻塞下一句话的录音 —— 小车运动时也能继续说话, 包括喊"停"
        if batch is not None:
            await batch.wait()
            timer.commands = batch.executed
            if "ble_sent" in timer.marks and timer.failed_stage is None:
                timer.mark("hub_done")
            if batch.records or timer.route not in ("task", "answer"):
                print(batch.summary_line())  # 交给任务层 / 回答提问的句子没有直接动作
        self._report(timer)

    def _schedule_report(self, timer: CommandTimer, batch: CommandBatch | None = None) -> None:
        task = asyncio.create_task(self._report_when_done(timer, batch))
        self._pending_reports.add(task)
        task.add_done_callback(self._pending_reports.discard)

    async def _drain_reports(self) -> None:
        if self._pending_reports:
            await asyncio.gather(*self._pending_reports, return_exceptions=True)

    async def handle_text(
        self,
        input_text: str,
        timer: CommandTimer,
        llm_model: str | None = None,
    ) -> CommandBatch | None:
        """
        处理一句已经识别好的话 (整句模式):
        1) 快速通道: 整句都能被本地严格匹配时直接放进执行队列, 不调用 LLM;
           整句只有停止词时立即急停。
        2) 否则交给 LLM, 边生成边执行。
        不等命令执行完就返回, 执行结果在后台输出。
        """
        timer.text = input_text
        batch = self.robot_agent.new_batch(timer)
        if await self._route_whole_into(batch, input_text, llm_model):
            return None  # 急停已经单独输出了汇总
        batch.close()
        self._schedule_report(timer, batch)
        return batch

    async def _route_whole_into(
        self, batch: CommandBatch, text: str, llm_model: str | None
    ) -> bool:
        """整句路由, 命令放进 batch。返回 True 表示这句话是急停 (已经执行)。"""
        timer = batch.timer
        matched = command_matcher.match(text) if self.fast_path else None
        if self.answer_pending() and not (matched is not None and matched.is_emergency_stop):
            # 任务代理在等回答: 这句话就是回答, 不当作命令
            timer.route = "answer"
            timer.mark("route_done")
            self.answer(text)
            return False

        if matched is not None and matched.is_emergency_stop:
            timer.route = "stop"
            timer.mark("route_done")
            await self.emergency_stop(timer)
            return True

        if matched is not None:
            timer.route = "fast_path"
            timer.mark("route_done")
            for cmd in matched.commands:
                self._enqueue(batch, cmd)
        else:
            await self._run_llm_into(batch, text, [], llm_model)
        return False

    async def process_stream(
        self,
        source: Any,
        llm_model: str | None = None,
        input_mode: str = "mic-stream",
        segment_filter: Callable[[str], bool] | None = None,
    ) -> CommandBatch:
        """
        流式模式处理一句话: 边说边识别, 每确定一个分句就路由 (见 StreamingRouter),
        命中快速通道的动作在说完之前就开始执行。
        source: 产出 24 kHz PCM16 音频块的异步迭代器 (PushToTalkSource / WavFileSource),
                结束后 source.audio 是完整录音。
        流式识别失败时, 用完整录音回退到整句识别。
        segment_filter: 返回 True 的语段不再路由 (唤醒模式用它拦截"结束对话")。
        """
        timer = CommandTimer(input_mode=input_mode)
        batch = self.robot_agent.new_batch(timer)
        stop_tasks: list[asyncio.Task] = []

        async def stop_now() -> None:
            # route_done = 识别出"停"的时刻, ble_sent = Hub 确认停止的时刻;
            # 汇总里的"BLE发出"这一段就是"识别出停 -> 停下"的时间
            timer.mark("route_done")
            await self.emergency_stop()
            timer.mark("ble_sent")

        async def on_stop() -> None:
            # 不等 Hub (1b 中正在执行的动作要做完才会停), 不阻塞识别结果的接收
            stop_tasks.append(asyncio.create_task(stop_now()))

        router = StreamingRouter(
            on_command=lambda cmd: self._enqueue(batch, cmd),
            on_stop=on_stop,
            run_llm=lambda text, executed: self._run_llm_into(batch, text, executed, llm_model),
        )
        # 任务代理在等回答: 这句话不路由, 识别完整句交给任务层 (停止词照常急停)
        answering = self.answer_pending()
        answer_parts: list[str] = []

        async def on_segment(text: str) -> None:
            print("识别语段:", text)
            if segment_filter is not None and segment_filter(text):
                return
            if answering and not command_matcher.contains_stop_word(utils.normalize_text(text)):
                answer_parts.append(text)
                return
            await router.on_segment(text)

        transcriber = self.streaming_transcriber_factory(router.on_partial, on_segment)

        failure: Exception | None = None
        try:
            self.upload_guard.check("stream")
            await transcriber.start()
        except Exception as exc:
            failure = exc

        async for chunk in source:
            if failure is None:
                try:
                    await transcriber.send(chunk)
                except Exception as exc:
                    failure = exc  # 继续录音, 留给回退用
        timer.mark("recording_end")

        if failure is None:
            try:
                await transcriber.finish()
            except Exception as exc:
                failure = exc
        else:
            await transcriber.close()

        stopped = False
        if failure is None:
            timer.mark("stt_done")
            timer.text = utils.normalize_text(router.text)
            await router.finish()
            stopped = router.state == STOPPED
            if answering and not stopped and answer_parts:
                self.answer(utils.normalize_text("".join(answer_parts)))
            # 流式模式下快速通道的动作在说完之前就发出了, 不打 route_done,
            # 看"说完→首个动作"即可; 交给 LLM 的部分由 _run_llm_into 打 route_done
            timer.route = {
                STOPPED: "stop",
                LLM: "fast_path+llm" if router.executed else "llm",
            }.get(router.state, "answer" if answering else "fast_path")
        else:
            print(f"流式识别失败，已回退到整句识别：{failure}")
            stopped = await self._fallback_whole(source.audio, router, batch, llm_model)

        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        batch.close()
        if not (stopped and failure is not None):
            if stopped:
                timer.commands = ["stop"]
            self._schedule_report(timer, batch)
        return batch

    def _temp_wav_path(self) -> str:
        fd, path = tempfile.mkstemp(prefix="vr_stream_", suffix=".wav")
        os.close(fd)
        return path

    async def _fallback_whole(
        self, audio: Any, router: StreamingRouter, batch: CommandBatch, llm_model: str | None
    ) -> bool:
        """流式识别失败: 用完整录音做整句识别。返回 True 表示最后执行的是急停。"""
        timer = batch.timer
        wav_path = self._temp_wav_path()
        try:
            audio_utils.write_speech_wav(wav_path, audio, STREAM_SAMPLE_RATE)
            text = await self.transcribe_audio(wav_path, timer)
        finally:
            Path(wav_path).unlink(missing_ok=True)

        if not text:
            return False
        timer.mark("stt_done")
        text = utils.normalize_text(text)
        timer.text = text
        print("整句识别:", text)

        if router.state == STOPPED:
            return True
        done = router.executed
        if not done:
            return await self._route_whole_into(batch, text, llm_model)

        # 断开前已经执行了一部分: 只有能确认剩下哪些时才继续, 否则不猜
        matched = command_matcher.match(text)
        if matched is not None and matched.commands[:len(done)] == done:
            timer.route = "fast_path"
            for cmd in matched.commands[len(done):]:
                self._enqueue(batch, cmd)
        else:
            print(f"流式识别中断：已执行 {', '.join(done)}，剩下的部分无法确定，请重新说一遍")
        return False

    # ---------- realtime 链路 ----------

    def _make_realtime(self, input_mode: str) -> RealtimePipeline:
        return RealtimePipeline(
            self, input_mode=input_mode, model=self.realtime_model,
            end_silence_ms=self.end_silence_ms, connect=self.realtime_connect,
            on_failed=self._on_realtime_failed, on_exit=self._on_realtime_exit,
        )

    def _switch_to_stream(self, reason: str) -> None:
        if self.pipeline != "realtime":
            return
        self.pipeline = "stream"
        print(f"realtime 链路不可用：{reason}")
        print("已切换到 stream 链路（流式识别 + 快速通道 + LLM），继续接收命令")

    async def _on_realtime_failed(self, reason: str) -> None:
        self._switch_to_stream(reason)

    async def _on_realtime_exit(self) -> None:
        self.conversation_gate.deactivate()
        print("已结束对话，需要重新唤醒")

    async def process_realtime(self, source: Any) -> Any:
        """
        realtime 链路处理一句话 (按空格): 按住时推送音频, 松开时提交, 模型以函数调用给出动作。
        不等模型回复完就返回 (返回的 turn 可以 await turn.wait())。
        会话失败时切换到 stream 链路, 这句话用完整录音整句识别。
        """
        assert self.realtime is not None
        try:
            self.upload_guard.check("realtime")
            return await self.realtime.ptt_turn(source)
        except RealtimeSessionError as exc:
            self._switch_to_stream(str(exc))
            audio = getattr(source, "audio", None)
            if audio is None or len(audio) == 0:
                return None
            timer = CommandTimer(input_mode="ptt")
            timer.mark("recording_end")
            text = await self._transcribe_array(audio, timer)
            if text.strip():
                timer.mark("stt_done")
                print("整句识别:", text)
                await self.handle_text(utils.normalize_text(text), timer, self._llm_model)
            elif timer.failed_stage:
                self._report(timer)
            return None

    # --- 唤醒词模式: 对话窗口内的音频连续交给 realtime 会话 (WakeLoop 的可选 handlers) ---

    def window_streaming(self) -> bool:
        return self.pipeline == "realtime" and self.realtime is not None

    def on_window_start(self, frames: list[bytes]) -> None:
        """唤醒: 建立会话 (在后台), 从唤醒词开始的音频先排队, 连上后按顺序发出。"""
        queue: asyncio.Queue = asyncio.Queue()
        for f in frames:
            queue.put_nowait(f)
        self._rt_queue = queue
        self._rt_sender = asyncio.ensure_future(self._rt_send_loop(queue))

    def on_window_audio(self, frame: bytes) -> None:
        if self._rt_queue is not None:
            self._rt_queue.put_nowait(frame)

    def on_window_end(self) -> None:
        if self._rt_queue is not None:
            self._rt_queue.put_nowait(None)
            self._rt_queue = None

    async def _rt_send_loop(self, queue: asyncio.Queue) -> None:
        assert self.realtime is not None
        try:
            self.upload_guard.check("realtime")
            await self.realtime.open()
        except Exception as exc:
            self._switch_to_stream(str(exc))
            # 唤醒词那句话 (以及连上之前说的话) 用整句识别处理
            frames = []
            while not queue.empty():
                item = queue.get_nowait()
                if item is None:
                    break
                frames.append(item)
            self._rt_queue = None
            if frames:
                await self.on_wake_utterance(np.frombuffer(b"".join(frames), dtype=np.int16))
            return
        try:
            while (frame := await queue.get()) is not None:
                if not self.conversation_gate.is_active():
                    continue  # 窗口已经关闭: 不再上传 (之后会收到结束标记)
                try:
                    await self.realtime.session.send_audio(frame)
                except RealtimeSessionError:
                    if self.realtime.failed is None:
                        await self.realtime.on_failed("发送音频失败")
                    return
        finally:
            await self.realtime.close()

    async def emergency_stop(self, timer: CommandTimer | None = None) -> None:
        """急停: 不排队, 清空所有还没发出的命令, 立即发送 stop; 正在执行的任务整个取消。"""
        task_stop = None
        if self.task_runtime is not None and self.task_runtime.busy:
            task_stop = asyncio.ensure_future(self.task_runtime.stop())
        cancelled = await self.robot_agent.stop()
        if task_stop is not None:
            await task_stop
        if cancelled:
            print(f"急停：取消了 {len(cancelled)} 条还没执行的命令：",
                  ", ".join(r.display for r in cancelled))
        if timer is not None:
            timer.mark("ble_sent")
            timer.commands = ["stop"]
            self._schedule_report(timer)

    # ---------- 唤醒词模式 (WakeLoop 的 handlers) ----------

    def _use_streaming(self) -> bool:
        return self.streaming and bool(self.audio_client.api_key)

    async def _local_transcribe(self, audio16k: np.ndarray) -> str:
        return await asyncio.to_thread(self.local_transcriber.transcribe, audio16k)

    async def on_local_stop(self) -> None:
        """本地识别出"急停" (未唤醒时, 或 realtime 窗口内)。"""
        if self.window_streaming() and self.realtime is not None:
            await self.realtime.stop(None, "本地识别：急停")
            return
        timer = CommandTimer(input_mode="wake-local")
        timer.text = "急停"
        timer.route = "stop"
        timer.mark("route_done")
        await self.emergency_stop(timer)

    async def _transcribe_array(self, audio: np.ndarray, timer: CommandTimer) -> str:
        wav_path = self._temp_wav_path()
        try:
            audio_utils.write_speech_wav(wav_path, audio, STREAM_SAMPLE_RATE)
            return await self.transcribe_audio(wav_path, timer)
        finally:
            Path(wav_path).unlink(missing_ok=True)

    async def on_wake_utterance(self, audio: np.ndarray) -> bool:
        """
        唤醒片段 (从唤醒词所在的片段到这句话结束): 云端识别, 去掉唤醒词, 剩下的部分当作命令。
        "小蓝小蓝，前进30厘米" -> forward 30。
        """
        timer = CommandTimer(input_mode="wake")
        timer.mark("recording_end")
        text = await self._transcribe_array(audio, timer)
        if not text.strip():
            if timer.failed_stage:
                self._report(timer)
            return False
        timer.mark("stt_done")
        print("唤醒片段识别:", text)
        command = strip_wake_word(text)
        if command is None:
            # 本地误唤醒, 或者云端没听出唤醒词: 不执行, 窗口保持打开
            print("云端识别结果里没有唤醒词，这句话不执行")
            return False
        if not command.strip():
            return False
        if is_exit_phrase(command):
            self.conversation_gate.deactivate()
            print("已结束对话")
            return False
        await self.handle_text(utils.normalize_text(command), timer, self._llm_model)
        return True

    async def on_window_utterance(self, source: UtteranceSource) -> bool:
        """对话窗口内的一句话。返回 False 表示用户说了"结束对话"。"""
        exited = False

        def exit_filter(text: str) -> bool:
            nonlocal exited
            if is_exit_phrase(text):
                exited = True
                print("已结束对话，需要重新唤醒")
                return True
            return False

        if self._use_streaming():
            await self.process_stream(
                source, self._llm_model, input_mode="wake-stream", segment_filter=exit_filter
            )
            return not exited

        # 没有流式识别: 说完再整句识别
        async for _chunk in source:
            pass
        timer = CommandTimer(input_mode="wake")
        timer.mark("recording_end")
        text = await self._transcribe_array(source.audio, timer)
        if not text.strip():
            if timer.failed_stage:
                self._report(timer)
            return True
        timer.mark("stt_done")
        print("识别文本:", text)
        if exit_filter(text):
            return False
        await self.handle_text(utils.normalize_text(text), timer, self._llm_model)
        return True

    def play_tone(self, kind: str) -> float:
        return self.audio_client.start_tone(kind)

    async def run_wake(self, frames: Any = None, vad: Any = None) -> None:
        """
        唤醒词模式主循环。frames: 24 kHz 音频块的异步迭代器, 默认是麦克风;
        vad: 判断一帧是否是人声, 默认 webrtcvad (测试时可以替换)。
        """
        from pc.speech.local_stt import LocalTranscriber
        from pc.speech.vad import WebRtcVad

        if self.local_transcriber is None:
            self.local_transcriber = LocalTranscriber()
        print("正在加载本地唤醒模型（第一次运行会下载约 75 MB）...")
        await asyncio.to_thread(self.local_transcriber.load)
        loop = WakeLoop(
            self.conversation_gate, self, self._local_transcribe, vad or WebRtcVad(),
            end_silence_ms=self.end_silence_ms,
        )
        self.wake_loop = loop
        log = self.upload_guard.log_path
        print("唤醒词模式：说「小蓝小蓝」或「你好小蓝」唤醒，未唤醒时也可以喊「急停」。")
        print(f"未唤醒时音频只在本机处理；上传审计日志：{log if log else '（未写文件）'}")
        if self.pipeline == "realtime":
            idle = self.conversation_gate.timeout_seconds
            print(f"realtime 链路：唤醒后才建立会话，{idle:.0f} 秒没人说话就关闭会话、停止上传")
        try:
            await loop.run(frames if frames is not None else MicrophoneSource())
        finally:
            self.on_window_end()
            if self._rt_sender is not None:
                await asyncio.gather(self._rt_sender, return_exceptions=True)

    # ---------- 启动 ----------

    async def run(
        self,
        input_mode: str = "ptt",
        pipeline: str = "stream",
        hub: str | None = None,
        llm_model: str | None = None,
        stt_device: str = "auto",
        run_once: bool = False,
        mode: str | None = None,
    ) -> None:
        """
        input_mode: ptt (按住空格) / wake (唤醒词) / cli (键盘)
        pipeline:   stream (流式识别 + 快速通道 + LLM) / realtime (语音直接到函数调用)
        hub:        sim / ble; 为空时沿用构造函数的 spike_simulation
        mode:       旧参数名 (mic / cli / wake), 兼容旧代码
        """
        input_mode = validate_launch(
            mode or input_mode, pipeline, has_api_key=bool(self.audio_client.api_key)
        )
        if stt_device not in {"auto", "cpu", "cuda"}:
            raise LaunchConfigError(f"unknown STT device: {stt_device}")
        os.environ["VOICE_ROBOT_STT_DEVICE"] = stt_device

        if hub is not None:
            if hub not in HUBS:
                raise LaunchConfigError(f"未知的 Hub 连接方式：{hub}（可选：{' / '.join(HUBS)}）")
            self.spike = SpikeHub(simulate=(hub == "sim"))
            self.robot_agent = robot_agent.RobotAgent(hub=self.spike)

        self._llm_model = llm_model
        self._input_mode = input_mode
        self.upload_guard.set_mode(input_mode, self._upload_log_path)
        self.pipeline = pipeline
        if pipeline == "realtime":
            self.realtime = self._make_realtime(input_mode)

        use_streaming = input_mode == "ptt" and self._use_streaming()
        if input_mode in ("ptt", "wake") and self.streaming and not self._use_streaming():
            print("没有 OPENAI_API_KEY，改用整句识别（本地 Whisper）")
        print(f"输入方式：{input_mode}，语音链路：{pipeline}"
              + (f"（{self.realtime_model}）" if pipeline == "realtime" else "")
              + f"，Hub：{'模拟' if self.spike.simulate else 'BLE'}")

        # step 0: connect to SpikeHub
        await self.robot_agent.connect()
        try:
            if input_mode == "wake":
                await self.run_wake()
                return

            if self.pipeline == "realtime" and self.realtime is not None:
                # 按空格: 程序启动时建立会话并保持连接, 只在按住空格时发送音频
                try:
                    await self.realtime.open()
                    print("realtime 会话已建立（不按空格时不上传音频）")
                except RealtimeSessionError as exc:
                    self._switch_to_stream(str(exc))

            while True:
                if self.pipeline == "realtime":
                    await self.process_realtime(PushToTalkSource())
                    if run_once:
                        break
                    continue
                if use_streaming:
                    # 按住空格边说边识别, 命中快速通道的分句在说完之前就开始执行
                    await self.process_stream(PushToTalkSource(), llm_model)
                    if run_once:
                        break
                    continue

                # step 1: parse input text from mic or cli
                input_text, timer = await self.get_input_text(input_mode)
                print("input text:", input_text)

                if input_text:
                    # step 2-5: 快速通道或 LLM -> 命令序列 -> RobotAgent -> SpikeHub
                    await self.handle_text(input_text, timer, llm_model)
                elif timer.failed_stage:
                    self._report(timer)

                if run_once:
                    break
        finally:
            if self.realtime is not None:
                await self.realtime.close()
            await self._drain_reports()
            await wait_background_closes()
            await self.robot_agent.disconnect()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m pc.voice_to_robot", description="语音控制 LEGO Spike 小车"
    )
    parser.add_argument("--input", choices=INPUT_MODES, default="ptt",
                        help="输入方式：ptt 按住空格说话（默认）/ wake 唤醒词 / cli 键盘输入")
    parser.add_argument("--pipeline", choices=PIPELINES, default="stream",
                        help="语音链路：stream 流式识别+快速通道+LLM（默认）/ "
                             "realtime 语音直接到函数调用")
    parser.add_argument("--realtime-model", default=REALTIME_MODEL,
                        help=f"realtime 链路使用的模型（默认 {REALTIME_MODEL}，"
                             "也可以用 gpt-realtime / gpt-realtime-2）")
    parser.add_argument("--hub", choices=HUBS, default="sim",
                        help="sim 模拟 Hub（默认，不需要小车）/ ble 通过蓝牙连接真实 Hub")
    parser.add_argument("--model", default="gpt-6-luna", help="LLM 模型")
    parser.add_argument("--once", action="store_true", help="只处理一句话就退出（ptt / cli）")
    parser.add_argument("--no-fast-path", action="store_true",
                        help="所有命令都交给 LLM（用于对比）")
    parser.add_argument("--no-streaming", action="store_true", help="说完再整句识别（用于对比）")
    parser.add_argument("--end-silence-ms", type=int, default=END_SILENCE_MS,
                        help=f"唤醒词模式下句末静音多久算说完（默认 {END_SILENCE_MS}）")
    parser.add_argument("--latency-log", default=None,
                        help="耗时日志 JSONL 文件（也可以用环境变量 VOICE_ROBOT_LATENCY_LOG）")
    parser.add_argument("--upload-log", default=None,
                        help="上传审计日志 JSONL 文件（唤醒词模式默认 logs/uploads.jsonl）")
    parser.add_argument("--stt-device", choices=("auto", "cpu", "cuda"), default="auto",
                        help="整句识别回退到本地 Whisper 时使用的设备")
    # 任务层 (改动 task-layer)
    parser.add_argument("--field", choices=FIELDS, default="sim",
                        help="任务层的场地：sim 运动学模拟器（默认）/ none 不启用任务层"
                             "（摄像头到货后再加实车）")
    parser.add_argument("--layout", default="wro2025_demo",
                        help="模拟器的物体布局（pc/field/data/layouts/ 下的名字，或 random）")
    parser.add_argument("--rules", default=None,
                        help="任务组规则（pc/task/data/missions.yaml，默认 wro2025）")
    parser.add_argument("--task-model", default=DEFAULT_TASK_MODEL,
                        help=f"任务代理使用的模型（默认 {DEFAULT_TASK_MODEL}）")
    parser.add_argument("--show-field", action="store_true",
                        help="显示模拟场地的实时俯视图（小车按 0.3 倍时间运动）")
    parser.add_argument("--speak-progress", action="store_true",
                        help="任务进度同时语音播报")
    return parser.parse_args(argv)


def make_task_runtime(args: argparse.Namespace, on_progress: Callable[[str], None]) -> Any:
    """按启动参数创建任务层的运行环境; --field none 时返回 None。"""
    if args.field == "none":
        return None
    from pc.task.agent import OpenAITaskLLM
    from pc.task.runtime import TaskRuntime

    return TaskRuntime.simulated(
        OpenAITaskLLM(args.task_model), layout=args.layout, ruleset=args.rules,
        on_progress=on_progress, show=args.show_field,
        time_scale=0.3 if args.show_field else 0.0,
    )


def main(argv: list[str] | None = None) -> int:
    # 输出重定向到不支持中文的编码 (例如 Git Bash 管道的 cp1252) 时不崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    try:
        validate_launch(args.input, args.pipeline,
                        has_api_key=bool(os.getenv("OPENAI_API_KEY")))
        if args.end_silence_ms <= 0:
            raise LaunchConfigError("--end-silence-ms 必须大于 0")
    except LaunchConfigError as exc:
        print(f"启动参数错误：{exc}")
        return 2

    controller = VoiceController(
        spike_simulation=(args.hub == "sim"),
        fast_path=not args.no_fast_path,
        latency_log_path=args.latency_log,
        streaming=not args.no_streaming,
        end_silence_ms=args.end_silence_ms,
        upload_log_path=args.upload_log,
        realtime_model=args.realtime_model,
        speak_progress=args.speak_progress,
    )
    try:
        controller.task_runtime = make_task_runtime(args, controller.task_progress)
    except Exception as exc:
        print(f"任务层启动失败（{type(exc).__name__}: {exc}），只能执行直接动作")
    if controller.task_runtime is not None:
        if args.hub == "ble":
            print("注意：任务层现在只在模拟器里运行（还没有摄像头），目标级请求不会让真实小车移动")
        rules = controller.task_runtime.skills.rules
        print(f"任务层：模拟场地，布局 {args.layout}，规则 {rules.title}，模型 {args.task_model}")
    try:
        asyncio.run(controller.run(
            input_mode=args.input,
            pipeline=args.pipeline,
            llm_model=args.model,
            stt_device=args.stt_device,
            run_once=args.once,
        ))
    except KeyboardInterrupt:
        print("已退出")
    except LaunchConfigError as exc:
        print(f"启动参数错误：{exc}")
        return 2
    except Exception as exc:
        print("Error:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
