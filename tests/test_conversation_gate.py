"""唤醒词 / 急停 / 退出用语的本地文字判断, 以及对话窗口计时。

测试输入大多是本地 tiny 模型对合成录音的真实输出 (见 voice-input-modes/measurements.md)。
"""

import unittest

from pc.speech.conversation_gate import (
    ConversationGate,
    find_wake_word,
    is_exit_phrase,
    is_stop_phrase,
    strip_wake_word,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class WakeWordTest(unittest.TestCase):
    def test_wake_words(self):
        for text in ("小蓝小蓝", "你好小蓝", "小蓝小蓝。", "小蓝，小蓝"):
            with self.subTest(text=text):
                self.assertEqual(ConversationGate.classify_local(text), "wake")

    def test_variants_from_local_model(self):
        # tiny 不加 prompt 时的真实输出: 繁体、近音字、n/l 不分
        for text in ("小欄小欄", "小男 小男", "小藍小藍", "你好小男", "小兰小兰", "晓岚晓岚"):
            with self.subTest(text=text):
                self.assertEqual(ConversationGate.classify_local(text), "wake")

    def test_wake_word_in_the_middle(self):
        self.assertEqual(ConversationGate.classify_local("我想想小蓝小蓝向左转"), "wake")

    def test_emergency_stop(self):
        for text in ("急停", "既停。", "鸡庭", "停止", "停！", "stop"):
            with self.subTest(text=text):
                self.assertEqual(ConversationGate.classify_local(text), "stop")

    def test_chat_is_ignored(self):
        # 包括 tiny 对聊天录音的真实输出
        for text in (
            "小明你好", "小蓝你好。", "把藍色的機幕放到", "到脱子上", "今天天起真不错",
            "我们中午去吃面条吧", "你的作業,血完了", "昨天我們去公園玩了", "小男孩在放风筝",
            "我叫小蓝", "蓝色", "", "停车场在哪里", "听不清",
        ):
            with self.subTest(text=text):
                self.assertIsNone(ConversationGate.classify_local(text))

    def test_short_text_does_not_match_partially(self):
        self.assertIsNone(find_wake_word("小蓝"))

    def test_strip_wake_word_keeps_punctuation(self):
        self.assertEqual(strip_wake_word("小蓝小蓝，前进30厘米，然后左转90度"),
                         "前进30厘米，然后左转90度")
        self.assertEqual(strip_wake_word("嗯，你好小蓝。向左转九十度"), "向左转九十度")
        self.assertEqual(strip_wake_word("小蓝小蓝"), "")
        self.assertIsNone(strip_wake_word("前进30厘米"))

    def test_stop_phrase_must_be_whole_piece(self):
        self.assertTrue(is_stop_phrase("急停！"))
        self.assertFalse(is_stop_phrase("前进30厘米然后停"))

    def test_exit_phrases(self):
        for text in ("结束对话", "结束对话吧。", "再见", "休眠"):
            with self.subTest(text=text):
                self.assertTrue(is_exit_phrase(text))
        self.assertFalse(is_exit_phrase("前进再见"))


class WindowTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.gate = ConversationGate(timeout_seconds=20, clock=self.clock)

    def test_inactive_until_woken(self):
        self.assertFalse(self.gate.is_active())
        self.gate.wake()
        self.assertTrue(self.gate.is_active())

    def test_timeout_and_renew(self):
        self.gate.wake()
        self.clock.now += 15
        self.gate.renew()  # 执行了一条命令
        self.clock.now += 15
        self.assertTrue(self.gate.is_active())
        self.clock.now += 6
        self.assertFalse(self.gate.is_active())

    def test_deactivate(self):
        self.gate.wake()
        self.gate.deactivate()
        self.assertFalse(self.gate.is_active())


if __name__ == "__main__":
    unittest.main()
