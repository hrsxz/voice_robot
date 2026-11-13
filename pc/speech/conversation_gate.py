"""
唤醒词模式的对话窗口和本地文字判断。

- 唤醒词和急停只根据**本地识别**出的文字判断 (唤醒之前不上传任何音频);
- 唤醒后打开连续对话窗口: 窗口内说命令不需要唤醒词, 每条命令后重新计时,
  超时或说"结束对话 / 休眠 / 再见"就关闭。

本地 tiny 模型经常把"小蓝"写成"小藍 / 小欄 / 小男", 把"急停"写成"既停 / 鸡庭",
所以先做同音字归一, 再做模糊匹配 (见 openspec/changes/voice-input-modes/measurements.md)。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from rapidfuzz import fuzz

WAKE_WORDS: tuple[str, ...] = ("你好小蓝", "小蓝小蓝")
EXIT_PHRASES: tuple[str, ...] = ("结束对话", "休眠", "再见")
WAKE_THRESHOLD = 75  # 4 个字里错 1 个字是 75 分
WINDOW_SECONDS = 20.0

# 同音或近音字 -> 唤醒词里的字 (含繁体; 很多人 n/l 不分, "男"也算)
_WAKE_HOMOPHONES = {
    **dict.fromkeys("藍蓝欄栏兰蘭岚嵐澜瀾篮籃男难", "蓝"),
    **dict.fromkeys("晓曉筱", "小"),
}
# 唤醒词里名字的那个字: 匹配到的片段必须包含它, 否则"你好小明"也会有 75 分
_NAME_CHAR = "蓝"

# 急停: 整个片段只有停止词时才算 (本地识别的同音字先归一)
_STOP_HOMOPHONES = {
    **dict.fromkeys("急既即及鸡集吉极级疾", "急"),
    **dict.fromkeys("停庭亭婷廷挺", "停"),
    **dict.fromkeys("止指址只纸", "止"),
}
_STOP_PHRASES = {"急停", "停止", "停", "停下", "停下来", "停车", "stop", "停停", "急停急停"}

_TRAILING_PARTICLES = re.compile(r"[吧了啊呀哦啦]+$")


def _is_content(ch: str) -> bool:
    return ch.isalnum()


def _content(text: str) -> tuple[str, list[int]]:
    """去掉标点和空格后的文字, 以及每个字在原文中的位置。"""
    chars: list[str] = []
    positions: list[int] = []
    for i, ch in enumerate(text or ""):
        if _is_content(ch):
            chars.append(ch.lower())
            positions.append(i)
    return "".join(chars), positions


def _map(text: str, table: dict[str, str]) -> str:
    return "".join(table.get(ch, ch) for ch in text)


@dataclass(frozen=True)
class WakeMatch:
    word: str
    score: float
    start: int  # 在原文中的位置 [start, end)
    end: int


def find_wake_word(text: str) -> WakeMatch | None:
    """在文字中找唤醒词 (可以在任意位置)。找不到返回 None。"""
    content, positions = _content(text)
    mapped = _map(content, _WAKE_HOMOPHONES)
    best: tuple[float, int, int, str] | None = None
    for word in WAKE_WORDS:
        n = len(word)
        for width in (n - 1, n, n + 1):
            if width <= 0 or width > len(mapped):
                continue
            for start in range(0, len(mapped) - width + 1):
                window = mapped[start:start + width]
                if _NAME_CHAR not in window:
                    continue
                score = fuzz.ratio(window, word)
                if best is None or score > best[0]:
                    best = (score, start, start + width, word)
    if best is None or best[0] < WAKE_THRESHOLD:
        return None
    score, start, end, word = best
    return WakeMatch(word=word, score=score, start=positions[start], end=positions[end - 1] + 1)


def strip_wake_word(text: str) -> str | None:
    """
    去掉唤醒词 (以及它前面的内容), 返回后面的部分 (保留原来的标点, 分句要用)。
    文字中没有唤醒词时返回 None。
    """
    found = find_wake_word(text)
    if found is None:
        return None
    return text[found.end:].lstrip(" ，,。.!！?？、；;：:")


def is_stop_phrase(text: str) -> bool:
    """整个片段只有停止词 (急停 / 停止 / stop …)。"""
    content, _ = _content(text)
    return _map(content, _STOP_HOMOPHONES) in _STOP_PHRASES


def is_exit_phrase(text: str) -> bool:
    content, _ = _content(text)
    return _TRAILING_PARTICLES.sub("", content) in EXIT_PHRASES


class ConversationGate:
    """连续对话窗口。云端识别只在窗口打开时才允许调用 (见 UploadGuard)。"""

    def __init__(
        self,
        timeout_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self.active_until = 0.0

    def is_active(self) -> bool:
        return self._clock() < self.active_until

    def remaining_seconds(self) -> float:
        return max(0.0, self.active_until - self._clock())

    def wake(self) -> None:
        self.renew()

    def renew(self) -> None:
        self.active_until = self._clock() + self.timeout_seconds

    def deactivate(self) -> None:
        self.active_until = 0.0

    @staticmethod
    def classify_local(text: str) -> str | None:
        """本地识别的一个片段: "wake" / "stop" / None (普通聊天, 忽略)。"""
        if find_wake_word(text) is not None:
            return "wake"
        if is_stop_phrase(text):
            return "stop"
        return None
