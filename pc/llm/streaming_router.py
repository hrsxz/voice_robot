"""
流式模式下一句话的分句路由。

流式识别每确定一个语段 (用户停顿一下), 就调用 on_segment; 说话过程中的中间文字调用 on_partial;
松开空格后调用 finish。

状态:
- fast (初始): 每确定一个分句就用本地匹配器判断, 命中就立即放进执行队列;
- llm: 遇到第一个匹配不上的分句后进入, 后面的内容都收集起来, finish 时连同"已执行的动作"交给 LLM;
- stopped: 中间文字里出现停止词, 立即急停, 这句话剩下的内容全部丢弃。

分句什么时候算"确定":
- 语段里除最后一个之外的分句: 立即确定;
- 语段的最后一个分句: 如果它本身已经能完整匹配 (比如"向前走五十厘米"), 立即确定;
  否则先挂起, 等下一个语段来了拼在一起再判断 (应对"向前走 [停顿] 三十厘米"), 或者松开空格时确定。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pc.llm import command_matcher
from pc.utils.utils import normalize_text

FAST = "fast"
LLM = "llm"
STOPPED = "stopped"


class StreamingRouter:
    def __init__(
        self,
        on_command: Callable[[str], None],
        on_stop: Callable[[], Awaitable[None]],
        run_llm: Callable[[str, list[str]], Awaitable[None]],
    ) -> None:
        self._on_command = on_command
        self._on_stop = on_stop
        self._run_llm = run_llm
        self.state = FAST
        self.executed: list[str] = []  # 本地已经放进队列的命令 (按顺序)
        self.segments: list[str] = []  # 识别出的全部语段文字
        self._pending: str | None = None  # 挂起的、还没匹配上的分句
        self._llm_clauses: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.segments)

    async def on_partial(self, text: str) -> None:
        if self.state != STOPPED and command_matcher.contains_stop_word(normalize_text(text)):
            await self._stop()

    async def on_segment(self, text: str) -> None:
        self.segments.append(text)
        if self.state == STOPPED:
            return
        if command_matcher.contains_stop_word(normalize_text(text)):
            await self._stop()
            return

        # 先按原文的标点拆分句 (normalize_text 会把标点去掉), 再逐句规范化
        clauses = [
            c for c in (normalize_text(raw).replace(" ", "")
                        for raw in command_matcher.split_clauses(text)) if c
        ]
        if self._pending is not None:
            if clauses:
                clauses[0] = self._pending + clauses[0]
            else:
                clauses = [self._pending]
            self._pending = None
        if not clauses:
            return

        for clause in clauses[:-1]:
            self._confirm(clause)

        last = clauses[-1]
        if self.state == FAST and not self._is_complete(last):
            self._pending = last
        else:
            self._confirm(last)

    async def finish(self) -> None:
        """松开空格: 确定挂起的分句; 如果进入了 LLM 状态, 把剩下的内容交给 LLM。"""
        if self.state == STOPPED:
            return
        if self._pending is not None:
            self._confirm(self._pending)
            self._pending = None
        if self.state == LLM and self._llm_clauses:
            await self._run_llm("，".join(self._llm_clauses), list(self.executed))

    def _is_complete(self, clause: str) -> bool:
        return (
            command_matcher.match(clause) is not None
            or command_matcher.match_repeat(clause) is not None
        )

    def _confirm(self, clause: str) -> None:
        if self.state == LLM:
            self._llm_clauses.append(clause)
            return

        times = command_matcher.match_repeat(clause)
        if times is not None and self.executed:
            # "一共做四次": 把这句话前面的动作再做 (次数 - 1) 遍
            for cmd in list(self.executed) * (times - 1):
                self._emit(cmd)
            return

        result = command_matcher.match(clause)
        if result is None:
            self.state = LLM
            self._llm_clauses.append(clause)
            return
        for cmd in result.commands:
            self._emit(cmd)

    def _emit(self, cmd: str) -> None:
        self.executed.append(cmd)
        self._on_command(cmd)

    async def _stop(self) -> None:
        self.state = STOPPED
        self._pending = None
        self._llm_clauses.clear()
        await self._on_stop()
