import time

from rapidfuzz import fuzz

from pc.utils.utils import normalize_text


class ConversationGate:
    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.wake_words = (
            "你好小蓝",
            "小蓝小蓝",
            "小兰小兰",
        )
        self.timeout_seconds = timeout_seconds
        self.active_until = 0.0

    def is_active(self) -> bool:
        return time.monotonic() < self.active_until

    def remaining_seconds(self) -> float:
        return max(0.0, self.active_until - time.monotonic())

    def renew(self) -> None:
        self.active_until = time.monotonic() + self.timeout_seconds

    def deactivate(self) -> None:
        self.active_until = 0.0

    def consume(self, raw_text: str) -> tuple[str | None, bool]:
        text = normalize_text(raw_text)
        now = time.monotonic()

        if not text:
            return None, False

        if text in ("停止", "急停", "stop"):
            return "stop", False

        if text in ("结束对话", "休眠", "再见"):
            self.deactivate()
            return None, False

        best_word = ""
        best_score = 0.0

        for wake_word in self.wake_words:
            candidate = text[:len(wake_word)]
            score = fuzz.ratio(candidate, wake_word)
            if score > best_score:
                best_word = wake_word
                best_score = score

        if best_score >= 75:
            self.renew()
            command = text[len(best_word):].strip()
            return command or None, True

        if now < self.active_until:
            self.renew()
            return text, False

        return None, False