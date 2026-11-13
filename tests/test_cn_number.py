import unittest

from pc.llm.cn_number import parse_number, round_half_up


class ParseNumberTest(unittest.TestCase):
    def test_chinese_integers(self) -> None:
        cases = {
            "三十": 30, "十五": 15, "两百": 200, "一百二十": 120, "九十": 90,
            "十": 10, "零": 0, "三": 3, "一百零二": 102, "两百五": 250,
            "三千五": 3500, "一千零五十": 1050, "九千九百九十九": 9999,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_number(text), expected)

    def test_arabic_and_decimals(self) -> None:
        self.assertEqual(parse_number("30"), 30)
        self.assertEqual(parse_number("1.5"), 1.5)
        self.assertEqual(parse_number("二十五点五"), 25.5)
        self.assertEqual(parse_number("一点五"), 1.5)

    def test_invalid_returns_none(self) -> None:
        for text in ("", "一二三", "十百", "三十厘米", "abc", "点五"):
            with self.subTest(text=text):
                self.assertIsNone(parse_number(text))

    def test_round_half_up(self) -> None:
        self.assertEqual(round_half_up(25.5), 26)
        self.assertEqual(round_half_up(24.5), 25)  # Python 的 round(24.5) 是 24
        self.assertEqual(round_half_up(24.4), 24)


if __name__ == "__main__":
    unittest.main()
