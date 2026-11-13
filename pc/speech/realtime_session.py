"""
OpenAI 实时接口的会话客户端 (realtime 链路): 连接、配置、推送音频、接收事件、重连。

只负责和接口打交道, 把服务端事件翻译成几种回调 (RealtimeEvents);
一句话怎么变成命令、什么时候停止, 由 pc.llm.realtime_pipeline 决定。
接口的事件格式如果变了, 只需要改这个文件 (见 openspec/changes/voice-realtime-pipeline/design.md)。

用法:
    session = RealtimeSession(events, turn_detection=None)   # 按空格: 手动提交
    await session.start()
    await session.send_audio(pcm16)       # 24 kHz 单声道 PCM16
    await session.commit()                # 松开空格
    await session.create_response()
    await session.send_function_output(call_id, "已入队")
    await session.close()

回复只要文字和函数调用, 不要语音; 同时打开输入转写, 用于停止检测和日志。
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
import time
from collections.abc import Callable
from typing import Any, Protocol

import truststore
from dotenv import load_dotenv

from pc import constants
from pc.llm.audio_utils import STT_MODEL, STT_PROMPT

load_dotenv(constants.project_root_path / ".env")

SAMPLE_RATE = 24000
DEFAULT_MODEL = "gpt-realtime-mini"
# 接口规定一个会话最长 60 分钟; 按空格模式下快到时提前重连, 避免说到一半被断开
MAX_SESSION_AGE_S = 55 * 60
CONTEXT_TOKEN_LIMIT = 8000


class RealtimeSessionError(RuntimeError):
    """会话无法建立, 或者断开后重连失败。"""


class RealtimeEvents(Protocol):
    """会话事件的回调 (都是 async)。"""

    async def on_speech_started(self) -> None: ...

    async def on_speech_stopped(self) -> None: ...

    async def on_committed(self, item_id: str) -> None: ...

    async def on_transcript_delta(self, item_id: str, text: str) -> None:
        """text 是这一句到目前为止的转写文字 (不是增量)。"""

    async def on_transcript_done(self, item_id: str, text: str) -> None: ...

    async def on_response_created(self, response_id: str) -> None: ...

    async def on_function_call(
        self, response_id: str, call_id: str, name: str, arguments: str
    ) -> None: ...

    async def on_response_done(
        self, response_id: str, status: str, texts: list[str], calls: int
    ) -> None: ...

    async def on_error(self, message: str, code: str) -> None:
        """服务端报错 (会话仍然可用)。"""

    async def on_reconnected(self) -> None:
        """断开后重连成功 (之前的对话上下文已经没有了)。"""

    async def on_failed(self, reason: str) -> None:
        """会话断开且重连失败。"""


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _default_connect(api_key: str | None, model: str) -> Any:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
    # 公司网络有 TLS 拦截: 用系统证书库 (保留证书校验)
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return client.realtime.connect(model=model, websocket_connection_options={"ssl": ssl_ctx})


def session_config(
    instructions: str,
    tools: list[dict],
    turn_detection: dict | None,
    transcription_prompt: str = STT_PROMPT,
) -> dict:
    return {
        "type": "realtime",
        "output_modalities": ["text"],
        "instructions": instructions,
        "tools": tools,
        "tool_choice": "auto",
        "max_output_tokens": 1024,
        # 会话保留之前几句话的上下文 (音频 token), 句子越多每次请求越贵:
        # 超过上限就丢掉较早的内容, 只保留一半
        "truncation": {
            "type": "retention_ratio", "retention_ratio": 0.5,
            "token_limits": {"post_instructions": CONTEXT_TOKEN_LIMIT},
        },
        "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
            "transcription": {
                "model": STT_MODEL, "language": "zh", "prompt": transcription_prompt,
            },
            "turn_detection": turn_detection,
        }},
    }


def server_vad(silence_ms: int) -> dict:
    """唤醒模式: 服务端 VAD 划分轮次, 但不自动产生回复 (回复由 pipeline 控制, 保证一次只有一条)。"""
    return {
        "type": "server_vad",
        "silence_duration_ms": silence_ms,
        "prefix_padding_ms": 300,
        "create_response": False,
        "interrupt_response": False,
    }


class RealtimeSession:
    def __init__(
        self,
        events: RealtimeEvents,
        instructions: str,
        tools: list[dict],
        turn_detection: dict | None = None,
        model: str = DEFAULT_MODEL,
        api_key: str | None = None,
        connect: Callable[[str | None, str], Any] | None = None,
        max_reconnects: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.model = model
        self._config = session_config(instructions, tools, turn_detection)
        self._api_key = api_key
        self._connect = connect or _default_connect
        self._max_reconnects = max_reconnects
        self._clock = clock

        self._manager: Any = None
        self._conn: Any = None
        self._receiver: asyncio.Task | None = None
        self._closing = False
        self._opened_at = 0.0
        self._transcripts: dict[str, str] = {}
        self.items: list[str] = []  # 会话 (对话上下文) 里现有的条目, 按加入顺序
        self.failed: str | None = None
        self.reconnects = 0

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self.failed is None

    @property
    def age_s(self) -> float:
        return self._clock() - self._opened_at if self.is_open else 0.0

    async def start(self) -> None:
        """建立连接并配置会话; 失败时抛出 RealtimeSessionError。"""
        self._closing = False
        self.failed = None
        try:
            await self._open()
        except Exception as exc:
            await self._close_connection()
            raise RealtimeSessionError(f"无法建立 realtime 会话：{exc}") from exc
        self._receiver = asyncio.create_task(self._receive())

    async def ensure_fresh(self, max_age_s: float = MAX_SESSION_AGE_S) -> None:
        """会话没打开或快到时长上限时重新建立 (按空格模式每句话开始前调用)。"""
        if self.is_open and self.age_s < max_age_s:
            return
        await self.close()
        await self.start()

    async def send_audio(self, pcm16: bytes) -> None:
        conn = self._require()
        try:
            await conn.input_audio_buffer.append(audio=base64.b64encode(pcm16).decode())
        except Exception as exc:
            raise RealtimeSessionError(f"发送音频失败：{exc}") from exc

    async def commit(self) -> None:
        await self._call("提交音频", lambda c: c.input_audio_buffer.commit())

    async def clear_audio(self) -> None:
        await self._call("清空音频", lambda c: c.input_audio_buffer.clear())

    async def create_response(self) -> None:
        await self._call("请求回复", lambda c: c.response.create())

    async def cancel_response(self) -> None:
        """取消正在进行的回复; 没有回复时服务端会报一个无害的错误 (忽略)。"""
        if not self.is_open:
            return
        try:
            await self._conn.response.cancel()
        except Exception:
            pass

    async def send_function_output(self, call_id: str, output: str) -> None:
        await self._call("回传函数结果", lambda c: c.conversation.item.create(item={
            "type": "function_call_output", "call_id": call_id, "output": output,
        }))

    async def clear_context(self, keep: set[str] | frozenset[str] = frozenset()) -> int:
        """
        删除对话上下文里除 keep 之外的全部条目 (之前几句话的音频、函数调用和回复),
        返回删除的数量。
        """
        conn = self._require()
        removed = 0
        for item_id in [i for i in self.items if i not in keep]:
            self.items.remove(item_id)
            try:
                await conn.conversation.item.delete(item_id=item_id)
                removed += 1
            except Exception as exc:
                raise RealtimeSessionError(f"清理上下文失败：{exc}") from exc
        return removed

    async def close(self) -> None:
        self._closing = True
        if self._receiver is not None:
            self._receiver.cancel()
            try:
                await self._receiver
            except (asyncio.CancelledError, Exception):
                pass
            self._receiver = None
        await self._close_connection()

    # ---------- 内部 ----------

    def _require(self) -> Any:
        if self.failed is not None:
            raise RealtimeSessionError(self.failed)
        if self._conn is None:
            raise RealtimeSessionError("realtime 会话没有打开")
        return self._conn

    async def _call(self, what: str, fn: Callable[[Any], Any]) -> None:
        conn = self._require()
        try:
            await fn(conn)
        except Exception as exc:
            raise RealtimeSessionError(f"{what}失败：{exc}") from exc

    async def _open(self) -> None:
        self._manager = self._connect(self._api_key, self.model)
        self._conn = await self._manager.__aenter__()
        await self._conn.session.update(session=self._config)
        self._opened_at = self._clock()
        self._transcripts.clear()
        self.items.clear()

    async def _close_connection(self) -> None:
        manager, self._manager, self._conn = self._manager, None, None
        if manager is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:
                pass

    async def _receive(self) -> None:
        while True:
            try:
                event = await self._conn.recv()
            except Exception as exc:  # 任务取消 (CancelledError) 不是 Exception, 不会被拦下
                if self._closing:
                    return
                if not await self._reconnect(exc):
                    return
                continue
            try:
                await self._handle(event)
            except Exception as exc:  # 回调出错不能让接收任务停掉
                etype = _get(event, "type")
                print(f"[realtime] 处理事件 {etype} 出错：{type(exc).__name__}: {exc}")

    async def _reconnect(self, exc: Exception) -> bool:
        reason = f"realtime 会话断开：{exc}"
        await self._close_connection()
        for attempt in range(self._max_reconnects):
            await asyncio.sleep(0.2 * (attempt + 1))
            try:
                await self._open()
            except Exception as again:
                reason = f"realtime 会话断开，重连失败：{again}"
                await self._close_connection()
                continue
            self.reconnects += 1
            print("[realtime] 会话断开，已重新连接")
            await self.events.on_reconnected()
            return True
        self.failed = reason
        await self.events.on_failed(reason)
        return False

    async def _handle(self, event: Any) -> None:
        etype = _get(event, "type", "")
        item_id = _get(event, "item_id") or ""
        ev = self.events

        if etype in ("conversation.item.added", "conversation.item.created"):
            item = _get(event, "item")
            iid = _get(item, "id") if item is not None else None
            if iid and iid not in self.items:
                self.items.append(iid)
            return
        if etype == "conversation.item.deleted":
            if item_id in self.items:
                self.items.remove(item_id)
            return

        if etype == "input_audio_buffer.speech_started":
            await ev.on_speech_started()
        elif etype == "input_audio_buffer.speech_stopped":
            await ev.on_speech_stopped()
        elif etype == "input_audio_buffer.committed":
            await ev.on_committed(item_id)
        elif etype == "conversation.item.input_audio_transcription.delta":
            text = self._transcripts.get(item_id, "") + (_get(event, "delta") or "")
            self._transcripts[item_id] = text
            await ev.on_transcript_delta(item_id, text)
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._transcripts.pop(item_id, None)
            await ev.on_transcript_done(item_id, _get(event, "transcript") or "")
        elif etype == "conversation.item.input_audio_transcription.failed":
            await ev.on_error(f"转写失败：{_get(event, 'error')}", "transcription_failed")
        elif etype == "response.created":
            await ev.on_response_created(_get(_get(event, "response"), "id") or "")
        elif etype == "response.function_call_arguments.done":
            await ev.on_function_call(
                _get(event, "response_id") or "", _get(event, "call_id") or "",
                _get(event, "name") or "", _get(event, "arguments") or "",
            )
        elif etype == "response.done":
            resp = _get(event, "response") or {}
            texts: list[str] = []
            calls = 0
            for out in _get(resp, "output") or []:
                otype = _get(out, "type")
                if otype == "function_call":
                    calls += 1
                elif otype == "message":
                    text = _get(out, "text")
                    if text is None:
                        text = "".join(
                            _get(c, "text") or _get(c, "transcript") or ""
                            for c in _get(out, "content") or []
                        )
                    if text.strip():
                        texts.append(text.strip())
            await ev.on_response_done(
                _get(resp, "id") or "", _get(resp, "status") or "", texts, calls
            )
        elif etype == "error":
            error = _get(event, "error") or {}
            await ev.on_error(str(_get(error, "message", error)), str(_get(error, "code") or ""))


__all__ = [
    "DEFAULT_MODEL",
    "MAX_SESSION_AGE_S",
    "SAMPLE_RATE",
    "RealtimeEvents",
    "RealtimeSession",
    "RealtimeSessionError",
    "server_vad",
    "session_config",
]
