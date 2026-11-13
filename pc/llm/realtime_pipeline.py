"""
realtime 链路: 语音直接交给实时模型, 模型以函数调用给出动作 (改动 voice-realtime-pipeline)。

一句话 (一个"轮次") 的处理:
1. 音频提交后请求一次回复。实测 gpt-realtime 系列每次回复只给**一个**函数调用,
   所以每个调用到达时: 严格校验 -> 放进执行队列 -> 立即回函数结果 ("已入队" / "被拒绝：原因"),
   这一次回复结束后再请求下一次回复, 直到模型不再调用工具 (见 measurements.md)。
   不等动作执行完, 所以第一个动作入队后小车就开始动, 后面的调用同时在生成。
2. 同一时间只有一条回复在生成; 前一句还在生成时又说了一句, 新的一句排队。
3. 停止不依赖模型: 输入转写的中间文字里出现停止词 (或本地识别出"急停") 就立即急停、
   取消正在生成的回复, 之后到达的函数调用一律忽略。模型自己调用 stop 也按急停处理。
4. 会话断开且重连失败时, 正在处理的句子记为失败, 并通知上层切换到 stream 链路。
5. 目标级请求 (改动 task-layer): 模型调用 run_task(goal) -> 交给任务层 (host.start_task),
   这句话到此结束。任务代理在反问 (ask_user) 时, 下一句话不请求模型回复,
   而是把它的转写作为回答交给任务层 (host.answer)。

耗时记录和 stream 链路相同的 CommandTimer, 路由标记为 "realtime"。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pc.agent.robot_agent import REJECTED, CommandBatch, CommandRecord
from pc.llm import command_matcher
from pc.llm.audio_utils import is_prompt_echo
from pc.llm.realtime_tools import (
    INSTRUCTIONS,
    build_session_tools,
    handoff_goal,
    tool_call_to_cmd,
)
from pc.speech.conversation_gate import is_exit_phrase
from pc.speech.realtime_session import (
    DEFAULT_MODEL,
    RealtimeSession,
    RealtimeSessionError,
    server_vad,
)
from pc.utils.latency import CommandTimer
from pc.utils.utils import normalize_text
from skills import SkillRegistry, load_skill_registry

ROUTE = "realtime"
MAX_CALLS_PER_TURN = 24  # 防止模型陷入循环 ("走一个正方形"是 8 个调用)
TURN_TIMEOUT_S = 20.0
STOP_DEDUP_S = 1.0  # 一句话从提交到模型不再调用工具的最长时间
_COMMIT_EMPTY_CODES = ("input_audio_buffer_commit_empty",)


class RealtimeHost(Protocol):
    """realtime 链路需要的执行层接口 (由 VoiceController 提供)。"""

    def new_batch(self, timer: CommandTimer) -> CommandBatch: ...
    def enqueue_cmd(self, batch: CommandBatch, cmd: str) -> CommandRecord: ...
    def reject_cmd(self, batch: CommandBatch, cmd: str, reason: str) -> CommandRecord: ...
    async def emergency_stop(self) -> None: ...
    def schedule_report(self, timer: CommandTimer, batch: CommandBatch | None) -> None: ...
    # 任务层 (可选; 没有时 run_task 记为被拒绝)
    # def start_task(self, goal: str, timer: CommandTimer) -> str: ...
    # def answer_pending(self) -> bool: ...
    # def answer(self, text: str) -> None: ...


@dataclass
class Turn:
    timer: CommandTimer
    batch: CommandBatch
    item_id: str | None = None
    calls: int = 0
    stopped: bool = False  # 被急停取消
    is_stop: bool = False  # 这句话本身是"停"
    exited: bool = False  # 这句话是"结束对话"
    handed_off: bool = False  # 交给了任务层
    answer: bool = False  # 这句话是对任务层提问的回答 (不请求模型回复)
    cancel_sent: bool = False
    texts: list[str] = field(default_factory=list)  # 模型的文字回复
    done: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self) -> None:
        await self.done.wait()


class RealtimePipeline:
    def __init__(
        self,
        host: RealtimeHost,
        input_mode: str = "ptt",
        model: str = DEFAULT_MODEL,
        end_silence_ms: int = 700,
        connect: Any = None,
        registry: SkillRegistry | None = None,
        on_failed: Callable[[str], Awaitable[None]] | None = None,
        on_exit: Callable[[], Awaitable[None]] | None = None,
        keep_context: bool = False,
        max_calls: int = MAX_CALLS_PER_TURN,
        turn_timeout_s: float = TURN_TIMEOUT_S,
        log: Callable[[str], None] = print,
    ) -> None:
        self.host = host
        self.input_mode = input_mode
        self.registry = registry or load_skill_registry()
        self._on_failed_cb = on_failed
        self._on_exit_cb = on_exit
        self.max_calls = max_calls
        # False: 每句话独立 (和 stream 链路一致), 开始处理一句话之前删掉之前几句话的上下文。
        # 实测保留上下文时, 同一会话里第二次说"一共做四次"常常少做一遍; 删掉也省音频 token 费用
        self.keep_context = keep_context
        self.turn_timeout_s = turn_timeout_s
        self.log = log
        # 按空格: 关闭服务端轮次检测, 松开时手动提交; 唤醒词: 服务端 VAD 划分轮次
        turn_detection = server_vad(end_silence_ms) if input_mode == "wake" else None
        self.session = RealtimeSession(
            self, INSTRUCTIONS, build_session_tools(self.registry), turn_detection,
            model=model, connect=connect,
        )
        self.failed: str | None = None

        self._turns: dict[str, Turn] = {}  # 输入 item_id -> 这句话
        self._by_response: dict[str, Turn] = {}
        self._ignored: set[str] = set()  # 急停后作废的回复
        self._waiting: deque[Turn] = deque()  # 已提交、等待生成回复
        self._pending_commit: deque[Turn] = deque()  # 按空格: 已发出提交, 还没收到"已提交"
        self._current: Turn | None = None  # 正在生成回复的句子
        self._awaiting_created: Turn | None = None
        self._watchdog: asyncio.Task | None = None
        self._tasks: set[asyncio.Future] = set()
        self._speech_end: float | None = None
        self._last_stop = -1e9  # 上一次急停的时刻 (转写和本地识别可能先后报告同一个"停")

    # ---------- 会话 ----------

    async def open(self) -> None:
        """建立会话; 失败时抛出 RealtimeSessionError。"""
        await self.session.start()

    async def close(self) -> None:
        await self.session.close()
        self._abort_all("会话已关闭")

    @property
    def is_open(self) -> bool:
        return self.session.is_open

    # ---------- 按空格: 一句话 ----------

    async def ptt_turn(self, source: AsyncIterable[bytes]) -> Turn | None:
        """
        按住空格期间把音频推给会话, 松开时提交并请求回复, 不等模型回复完就返回
        (可以用 turn.wait() 等这句话的回复全部结束)。没按空格时不推送任何音频。
        会话出错时抛出 RealtimeSessionError (调用方回退到 stream 链路)。
        """
        timer = CommandTimer(input_mode=f"{self.input_mode}-realtime")
        turn = Turn(timer=timer, batch=self.host.new_batch(timer))
        failure: Exception | None = None
        try:
            await self.session.ensure_fresh()
        except RealtimeSessionError as exc:
            failure = exc
        sent = 0
        async for chunk in source:
            if failure is not None:
                continue  # 继续录完这句话, 留给回退的整句识别用
            try:
                await self.session.send_audio(chunk)
                sent += len(chunk)
            except RealtimeSessionError as exc:
                failure = exc
        timer.mark("recording_end")
        if failure is not None:
            raise failure
        if sent == 0:
            return None
        self._pending_commit.append(turn)
        try:
            await self.session.commit()
        except RealtimeSessionError:
            self._pending_commit.remove(turn)
            raise
        return turn

    # ---------- 停止 ----------

    async def stop(self, turn: Turn | None = None, reason: str = "") -> None:
        """急停: 作废所有正在生成和排队的回复, 立即发出 stop。"""
        now = time.monotonic()
        # 转写和本地识别可能先后报告同一个"停": 1 秒内只真正发一次 stop
        already = now - self._last_stop < STOP_DEDUP_S
        if turn is None and already:
            return
        if turn is not None:
            if turn.is_stop:
                return  # 同一句话里的"停"只处理一次
            turn.is_stop = True
            if turn.done.is_set():
                turn = None  # 这句话已经处理完 (耗时记录已经交出去了), 只执行急停
            else:
                turn.timer.route = "stop"
                turn.timer.mark("route_done")
        if already:
            if turn is not None:  # 这句话记为急停, 它的回复作废, 但 stop 已经发过了
                turn.stopped = True
                for rid, t in self._by_response.items():
                    if t is turn:
                        self._ignored.add(rid)
                if self._current is turn:
                    await self._cancel(turn)
                elif turn in self._waiting:
                    self._waiting.remove(turn)
                    self._finish(turn)
                turn.timer.mark("ble_sent")
                turn.timer.commands = ["stop"]
            return
        self._last_stop = now
        if reason:
            self.log(f"[realtime] 急停（{reason}）")
        for t in [self._current, *self._waiting, *self._pending_commit]:
            if t is not None:
                t.stopped = True
        for rid, t in self._by_response.items():
            if t.stopped:
                self._ignored.add(rid)
        stop_task = asyncio.ensure_future(self.host.emergency_stop())
        if self._current is not None:
            await self._cancel(self._current)
        await stop_task
        if turn is not None:
            turn.timer.mark("ble_sent")
            turn.timer.commands = ["stop"]
        # 排队的句子直接结束; 正在生成的那句等"回复已取消"事件 (或超时) 再结束
        # (服务端按顺序处理: 先收到的"请求回复"一定先于"取消", 所以取消不会落空)
        while self._waiting:
            self._finish(self._waiting.popleft())

    # ---------- RealtimeEvents ----------

    async def on_speech_started(self) -> None:
        pass

    async def on_speech_stopped(self) -> None:
        self._speech_end = time.perf_counter()

    async def on_committed(self, item_id: str) -> None:
        if self._pending_commit:
            turn = self._pending_commit.popleft()
        else:
            # 唤醒模式: 服务端 VAD 切出了一句话
            timer = CommandTimer(input_mode=f"{self.input_mode}-realtime")
            timer.mark("recording_end", self._speech_end)
            self._speech_end = None
            turn = Turn(timer=timer, batch=self.host.new_batch(timer))
        turn.item_id = item_id
        turn.timer.route = turn.timer.route or ROUTE
        self._turns[item_id] = turn
        if turn.stopped:
            self._finish(turn)
            return
        answer_pending = getattr(self.host, "answer_pending", None)
        if answer_pending is not None and answer_pending():
            # 任务层在等回答: 不请求模型回复, 等转写完成后把文字交给任务层
            turn.answer = True
            turn.timer.route = "answer"
            return
        self._waiting.append(turn)
        await self._next_response()

    async def on_transcript_delta(self, item_id: str, text: str) -> None:
        if is_prompt_echo(text, partial=True):
            return
        if command_matcher.contains_stop_word(normalize_text(text)):
            turn = self._turns.get(item_id)
            if turn is None or not turn.is_stop:
                await self.stop(turn, f"识别到：{text}")

    async def on_transcript_done(self, item_id: str, text: str) -> None:
        turn = self._turns.pop(item_id, None)
        if is_prompt_echo(text):
            self.log("[realtime] 丢弃一个提示词回显的转写（音频太短或不清楚）")
            return
        self.log(f"[realtime] 转写: {text}")
        if turn is None:
            return
        if not turn.done.is_set():
            turn.timer.text = normalize_text(text)
        if command_matcher.contains_stop_word(normalize_text(text)) and not turn.is_stop:
            await self.stop(turn, f"识别到：{text}")
            return
        if turn.answer:
            answer = getattr(self.host, "answer", None)
            if answer is not None:
                answer(text)
            self._finish(turn, report=False)
            return
        if self._on_exit_cb is not None and is_exit_phrase(text):
            turn.exited = True
            turn.stopped = True
            for rid, t in self._by_response.items():
                if t is turn:
                    self._ignored.add(rid)
            if self._current is turn:
                await self._cancel(turn)
            if turn in self._waiting:
                self._waiting.remove(turn)
                self._finish(turn)
            await self._on_exit_cb()

    async def on_response_created(self, response_id: str) -> None:
        turn = self._awaiting_created
        self._awaiting_created = None
        if turn is None:
            return
        self._by_response[response_id] = turn
        if turn.stopped:
            self._ignored.add(response_id)
            await self._cancel(turn)

    async def on_function_call(
        self, response_id: str, call_id: str, name: str, arguments: str
    ) -> None:
        turn = self._by_response.get(response_id)
        if turn is None or response_id in self._ignored or turn.stopped:
            return
        turn.calls += 1
        if name == "stop":
            await self.stop(turn, "模型调用 stop")
            return
        if name == "run_task":
            await self._handoff(turn, call_id, arguments)
            return
        result = tool_call_to_cmd(name, arguments, self.registry)
        if result.ok:
            assert result.cmd is not None
            record = self.host.enqueue_cmd(turn.batch, result.cmd)
            if record.status == REJECTED:
                output = f"被拒绝：{record.detail}"
                self.log(f"Realtime 调用: {result.cmd}（{output}）")
            else:
                # 告诉模型这句话已经入队了几个动作, 帮它数"一共做四次"
                queued = sum(r.status != REJECTED for r in turn.batch.records)
                output = f"已入队（这句话第 {queued} 个动作）"
                self.log(f"Realtime 调用: {result.cmd}")
        else:
            display = f"{name} {arguments.strip()}" if arguments.strip() else name
            self.host.reject_cmd(turn.batch, display, result.error or "")
            output = f"被拒绝：{result.error}"
            self.log(f"Realtime 调用被拒绝: {display}（{result.error}）")
        try:
            await self.session.send_function_output(call_id, output)
        except RealtimeSessionError as exc:
            self.log(f"[realtime] {exc}")

    async def on_response_done(
        self, response_id: str, status: str, texts: list[str], calls: int
    ) -> None:
        turn = self._by_response.pop(response_id, None)
        ignored = response_id in self._ignored
        self._ignored.discard(response_id)
        if turn is None:
            return
        if not ignored:
            # 动作做完后模型常常补一句"已完成", 不打印; 没有动作时的说明在 _finish 里输出
            turn.texts.extend(texts)
        if ignored or turn.stopped:
            self._finish(turn)
            return
        if status == "failed":
            turn.timer.fail(ROUTE, "模型回复失败")
            self._finish(turn)
            return
        if turn.handed_off:
            self._finish(turn)  # 交给任务层以后不再请求下一次回复
            return
        if calls > 0 and status == "completed":
            if turn.calls >= self.max_calls:
                self.log(f"[realtime] 这句话已经调用了 {turn.calls} 次工具，不再继续")
                self._finish(turn)
                return
            # 函数结果已经回传: 请求下一次回复, 让模型给出后面的动作
            await self._request_response(turn)
            return
        self._finish(turn)

    async def on_error(self, message: str, code: str) -> None:
        if code in _COMMIT_EMPTY_CODES or "buffer too small" in message:
            if self._pending_commit:
                turn = self._pending_commit.popleft()
                self.log("录音太短，没有听到内容")
                self._finish(turn, report=False)
            return
        if code == "response_cancel_not_active":
            return
        self.log(f"[realtime] 服务端报错：{message}")
        if self._awaiting_created is not None and "response" in code:
            # 请求回复被拒绝 (例如已经有一条回复在生成): 这句话结束
            turn = self._awaiting_created
            self._awaiting_created = None
            turn.timer.fail(ROUTE, message)
            self._finish(turn)

    async def on_reconnected(self) -> None:
        self._abort_all("realtime 会话断开（已重新连接），这句话请重新说一遍")

    async def on_failed(self, reason: str) -> None:
        self.failed = reason
        self._abort_all(reason)
        if self._on_failed_cb is not None:
            await self._on_failed_cb(reason)

    # ---------- 内部 ----------

    async def _handoff(self, turn: Turn, call_id: str, arguments: str) -> None:
        """run_task: 把目标交给任务层, 这句话不再继续调用工具。"""
        goal = handoff_goal(arguments)
        start_task = getattr(self.host, "start_task", None)
        if goal is None:
            output = "被拒绝：缺少 goal"
        elif start_task is None:
            output = "被拒绝：任务层没有启用"
        else:
            turn.handed_off = True
            turn.timer.route = "task"
            output = start_task(goal, turn.timer)
        self.log(f"Realtime 调用: run_task {goal!r}（{output}）")
        try:
            await self.session.send_function_output(call_id, output)
        except RealtimeSessionError as exc:
            self.log(f"[realtime] {exc}")

    async def _next_response(self) -> None:
        while self._current is None and self._waiting:
            turn = self._waiting.popleft()
            if turn.stopped:
                self._finish(turn)
                continue
            self._current = turn
            self._start_watchdog(turn)
            if not self.keep_context:
                keep = {t.item_id for t in (turn, *self._waiting) if t.item_id}
                try:
                    await self.session.clear_context(keep)
                except RealtimeSessionError as exc:
                    self.log(f"[realtime] {exc}")
            await self._request_response(turn)

    async def _request_response(self, turn: Turn) -> None:
        self._awaiting_created = turn
        try:
            await self.session.create_response()
        except RealtimeSessionError as exc:
            self._awaiting_created = None
            turn.timer.fail(ROUTE, str(exc))
            self._finish(turn)

    async def _cancel(self, turn: Turn) -> None:
        if not turn.cancel_sent:
            turn.cancel_sent = True
            await self.session.cancel_response()

    def _start_watchdog(self, turn: Turn) -> None:
        async def watch() -> None:
            await asyncio.sleep(self.turn_timeout_s)
            if not turn.done.is_set():
                self.log(f"[realtime] 等待模型回复超时（{self.turn_timeout_s:.0f}s）")
                turn.timer.fail(ROUTE, "等待模型回复超时")
                for rid, t in list(self._by_response.items()):
                    if t is turn:
                        self._ignored.add(rid)
                await self._cancel(turn)
                self._finish(turn)

        self._watchdog = asyncio.ensure_future(watch())

    def _finish(self, turn: Turn, report: bool = True) -> None:
        if turn.done.is_set():
            return
        turn.done.set()
        if not turn.is_stop and turn.timer.failed_stage is None:
            turn.timer.mark("route_done")
        empty = (not turn.batch.records and not turn.is_stop and not turn.exited
                 and not turn.stopped and not turn.handed_off and not turn.answer
                 and turn.timer.failed_stage is None)
        if empty:
            # 没有动作: 只输出模型的说明 (只说了唤醒词时模型不回复, 什么都不输出), 不出耗时汇总
            if turn.texts:
                self.log(f"无法识别（模型：{turn.texts[-1]}）")
            report = False
        turn.batch.close()
        if report and not turn.exited:
            # 只有"停"的句子没有排队的命令, 汇总里的命令就是 stop (不要被空的批次覆盖)
            pure_stop = turn.is_stop and not turn.batch.records
            only_task = turn.handed_off and not turn.batch.records
            self.host.schedule_report(turn.timer,
                                      None if pure_stop or only_task else turn.batch)
        if self._current is turn:
            self._current = None
            if self._watchdog is not None and asyncio.current_task() is not self._watchdog:
                self._watchdog.cancel()
            self._watchdog = None
            task = asyncio.ensure_future(self._next_response())
            self._tasks.add(task)  # 保留引用, 避免任务被垃圾回收
            task.add_done_callback(self._tasks.discard)

    def _abort_all(self, reason: str) -> None:
        turns = [self._current, *self._waiting, *self._pending_commit]
        self._waiting.clear()
        self._pending_commit.clear()
        self._awaiting_created = None
        self._ignored.update(self._by_response)
        self._by_response.clear()
        for turn in turns:
            if turn is not None and not turn.done.is_set():
                turn.timer.fail(ROUTE, reason)
                self._finish(turn)
        self._current = None


__all__ = ["MAX_CALLS_PER_TURN", "ROUTE", "RealtimeHost", "RealtimePipeline", "Turn"]
