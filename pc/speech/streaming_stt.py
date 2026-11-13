"""
OpenAI 实时转写 (边说边识别)。

用法:
    stt = StreamingTranscriber(on_partial=..., on_segment=...)
    await stt.start()
    for chunk in 麦克风:            # 24 kHz 单声道 PCM16
        await stt.send(chunk)
    await stt.finish()              # 松开空格: 补交最后一段, 等所有语段识别完

服务端 VAD 按停顿把音频切成语段, 每个语段识别完就回调 on_segment (按说话顺序);
识别过程中的中间文字回调 on_partial (用于尽早发现"停")。
选型见 openspec/changes/voice-streaming/spike.md。
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

import truststore
from dotenv import load_dotenv

from pc import constants
from pc.llm.audio_utils import STT_MODEL, STT_PROMPT, is_prompt_echo

load_dotenv(constants.project_root_path / ".env")

SAMPLE_RATE = 24000  # 实时接口只支持 24 kHz
SILENCE_MS = 300
MIN_COMMIT_MS = 100  # 服务端要求一次提交至少 100 ms 音频
LONG_TAIL_MS = 600  # 松开时还没提交的尾音超过这个长度, 可能含有还没检测到的话, 要补交

TextCallback = Callable[[str], Awaitable[None]]

# 后台关闭连接的任务 (保留引用, 避免被垃圾回收)
_BACKGROUND_CLOSES: set[asyncio.Task] = set()


async def wait_background_closes() -> None:
    """等后台的连接关闭完成 (程序退出前调用)。"""
    if _BACKGROUND_CLOSES:
        await asyncio.gather(*_BACKGROUND_CLOSES, return_exceptions=True)


class StreamingSTTError(RuntimeError):
    """流式识别失败 (连接失败、服务端报错、等待结果超时)。"""


def _default_connect(api_key: str | None) -> Any:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
    # 公司网络有 TLS 拦截: 用系统证书库 (保留证书校验)
    ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return client.realtime.connect(
        extra_query={"intent": "transcription"},
        websocket_connection_options={"ssl": ssl_ctx},
    )


class StreamingTranscriber:
    def __init__(
        self,
        on_partial: TextCallback,
        on_segment: TextCallback,
        api_key: str | None = None,
        model: str = STT_MODEL,
        prompt: str = STT_PROMPT,
        silence_ms: int = SILENCE_MS,
        finish_timeout: float = 5.0,
        connect: Callable[[str | None], Any] | None = None,
    ) -> None:
        self._on_partial = on_partial
        self._on_segment = on_segment
        self._api_key = api_key
        self._model = model
        self._prompt = prompt
        self._silence_ms = silence_ms
        self._finish_timeout = finish_timeout
        self._connect = connect or _default_connect

        self._manager: Any = None
        self._conn: Any = None
        self._receiver: asyncio.Task | None = None
        self._changed = asyncio.Event()

        self._order: list[str] = []  # 已提交语段的顺序
        self._partials: dict[str, str] = {}
        self._finished: dict[str, str] = {}
        self._emitted = 0
        self._samples_since_commit = 0
        self._speaking = False  # 服务端 VAD 认为用户正在说话 (还没切出语段)
        # 服务端检测到"开始说话"的次数; 每一次最终都会产生一个已提交的语段。
        # "停止说话"和"已提交"之间有一点间隔, 松开空格恰好落在中间时也必须等这个语段
        self._speech_count = 0
        self._release_committed = False
        self._commit_pending = False
        self._error: str | None = None
        self.segments: list[str] = []

    async def start(self) -> None:
        try:
            self._manager = self._connect(self._api_key)
            self._conn = await self._manager.__aenter__()
            await self._conn.session.update(session={
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                    "transcription": {
                        "model": self._model, "language": "zh", "prompt": self._prompt,
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": self._silence_ms,
                        "prefix_padding_ms": 300,
                    },
                }},
            })
        except Exception as exc:
            await self.close()
            raise StreamingSTTError(f"无法建立流式识别连接：{exc}") from exc
        self._receiver = asyncio.create_task(self._receive())

    async def send(self, pcm16: bytes) -> None:
        self._raise_if_failed()
        try:
            await self._conn.input_audio_buffer.append(audio=base64.b64encode(pcm16).decode())
        except Exception as exc:
            raise StreamingSTTError(f"发送音频失败：{exc}") from exc
        self._samples_since_commit += len(pcm16) // 2

    async def finish(self) -> list[str]:
        """
        松开空格: 把还没提交的音频提交掉, 等所有语段识别完就返回。
        连接在后台关闭 —— WebSocket 关闭握手要等约 2 秒, 不能让用户等。
        """
        try:
            self._raise_if_failed()
            await asyncio.wait_for(self._wait_all_done(), self._finish_timeout)
            self._raise_if_failed()
            return list(self.segments)
        except TimeoutError as exc:
            raise StreamingSTTError(f"等待识别结果超时（{self._finish_timeout}s）") from exc
        finally:
            task = asyncio.create_task(self.close())
            _BACKGROUND_CLOSES.add(task)
            task.add_done_callback(_BACKGROUND_CLOSES.discard)

    async def close(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            try:
                await self._receiver
            except (asyncio.CancelledError, Exception):
                pass
            self._receiver = None
        if self._manager is not None:
            try:
                await self._manager.__aexit__(None, None, None)
            except Exception:
                pass
            self._manager = None
            self._conn = None

    # ---------- 内部 ----------

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise StreamingSTTError(self._error)

    async def _commit_if_still_speaking(self) -> None:
        """
        已经松开空格: 判断要不要补交剩下的音频, 让服务端立即切出最后一个语段。补交最多一次。

        需要补交:
        - 服务端认为用户还在说话;
        - 服务端还没检测到过任何语音 (很短的一句话说完立刻松开, "开始说话"事件可能还没到);
        - 自上次提交以来的尾音较长 (里面可能有还没检测到的话)。
        否则剩下的只是服务端切完语段后的一小段静音, 不补交, 免得多等一次识别。
        每次收到事件都会重新判断 ("开始说话"事件可能在松开之后才到)。
        """
        if self._commit_pending or self._release_committed:
            return
        tail_ms = self._samples_since_commit * 1000 / SAMPLE_RATE
        if not (self._speaking or self._speech_count == 0 or tail_ms >= LONG_TAIL_MS):
            return
        if tail_ms >= MIN_COMMIT_MS:
            self._commit_pending = True
            self._release_committed = True
            await self._conn.input_audio_buffer.commit()
        else:
            # 不到 100 ms 的尾音无法提交 (服务端会拒绝), 放弃它, 不再等这个语段
            self._speaking = False
            self._speech_count = len(self._order)

    async def _wait_all_done(self) -> None:
        while True:
            self._raise_if_failed()
            await self._commit_if_still_speaking()
            if (
                not self._commit_pending
                and len(self._order) >= self._speech_count
                and self._emitted == len(self._order)
            ):
                return
            self._changed.clear()
            await self._changed.wait()

    async def _receive(self) -> None:
        try:
            while True:
                event = await self._conn.recv()
                await self._handle(event)
                self._changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"流式识别连接中断：{exc}"
            self._changed.set()

    async def _handle(self, event: Any) -> None:
        etype = getattr(event, "type", "")
        item_id = getattr(event, "item_id", None)

        if etype == "input_audio_buffer.speech_started":
            self._speaking = True
            self._speech_count += 1
        elif etype == "input_audio_buffer.speech_stopped":
            self._speaking = False
        elif etype == "input_audio_buffer.committed":
            self._commit_pending = False
            self._speaking = False
            self._samples_since_commit = 0
            if item_id and item_id not in self._order:
                self._order.append(item_id)
        elif etype.endswith("input_audio_transcription.delta"):
            text = self._partials.get(item_id, "") + (getattr(event, "delta", "") or "")
            self._partials[item_id] = text
            if not is_prompt_echo(text, partial=True):
                await self._on_partial(text)
        elif etype.endswith("input_audio_transcription.completed"):
            if item_id not in self._order:
                self._order.append(item_id)
            self._finished[item_id] = getattr(event, "transcript", "") or ""
            await self._emit_ready()
        elif etype.endswith("input_audio_transcription.failed"):
            self._error = f"识别失败：{getattr(event, 'error', event)}"
        elif etype == "error":
            error = getattr(event, "error", None)
            message = str(getattr(error, "message", error))
            if "buffer too small" in message or "commit_empty" in str(getattr(error, "code", "")):
                # 服务端已经自动提交过了 (事件按顺序到达, 那个"已提交"事件已经处理过),
                # 松开时的补交可以忽略, 也不用再等别的语段
                self._commit_pending = False
                self._speaking = False
                self._speech_count = len(self._order)
            else:
                self._error = f"流式识别报错：{message}"

    async def _emit_ready(self) -> None:
        # 按提交顺序回调, 即使识别结果到达的顺序不同
        while self._emitted < len(self._order) and self._order[self._emitted] in self._finished:
            text = self._finished[self._order[self._emitted]]
            self._emitted += 1
            if is_prompt_echo(text):
                print("[STT] 丢弃一个提示词回显的语段（音频太短或不清楚）")
                continue
            if text.strip():
                self.segments.append(text)
                await self._on_segment(text)
