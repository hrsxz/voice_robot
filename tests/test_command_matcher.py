import unittest

from pc.llm.command_matcher import match
from pc.utils.utils import normalize_text
from skills import load_skill_registry

# docs/how_robot_works3.md 第 1–3 级的全部中文例句
DOCS_EXAMPLES: list[tuple[str, list[str]]] = [
    # 第 1 级
    ("向前走 30 厘米", ["forward 30"]),
    ("往后退 20 厘米", ["backward 20"]),
    ("向左转 90 度", ["left 90"]),
    ("向右转 45 度", ["right 45"]),
    ("把夹子抬起来", ["gripper_up"]),
    ("把夹子放下来", ["gripper_down"]),
    ("左夹子抬起来", ["gripper_left_up"]),
    ("右夹子放下来", ["gripper_right_down"]),
    ("停！", ["stop"]),
    # 第 2 级
    ("向前走 50 厘米，然后向右转 90 度", ["forward 50", "right 90"]),
    ("往后退 10 厘米，再把夹子抬起来", ["backward 10", "gripper_up"]),
    ("向左转 180 度，然后向前走 20 厘米", ["left 180", "forward 20"]),
    ("左夹子抬起来，右夹子放下来", ["gripper_left_up", "gripper_right_down"]),
    # 第 3 级
    (
        "把夹子放下来，向前走 20 厘米，把夹子抬起来，再往后退 20 厘米",
        ["gripper_down", "forward 20", "gripper_up", "backward 20"],
    ),
    (
        "左夹子抬起来，左夹子放下来，右夹子抬起来，右夹子放下来",
        ["gripper_left_up", "gripper_left_down", "gripper_right_up", "gripper_right_down"],
    ),
    (
        "向左转 45 度，向右转 90 度，向左转 45 度，左夹子和右夹子都抬起来",
        ["left 45", "right 90", "left 45", "gripper_up"],
    ),
    ("向前走 30 厘米再左转 90 度，一共做四次", ["forward 30", "left 90"] * 4),
]


def commands_of(text: str) -> list[str] | None:
    # 和主链路一样, 先经过 normalize_text
    result = match(normalize_text(text))
    return None if result is None else result.commands


class DocsExamplesTest(unittest.TestCase):
    def test_all_docs_examples_hit_fast_path(self) -> None:
        for text, expected in DOCS_EXAMPLES:
            with self.subTest(text=text):
                self.assertEqual(commands_of(text), expected)

    def test_docs_examples_without_spaces_or_punctuation(self) -> None:
        # STT 的输出经常没有空格, 也可能没有标点
        for text, expected in DOCS_EXAMPLES:
            compact = text.replace(" ", "").replace("，", "")
            with self.subTest(text=compact):
                self.assertEqual(commands_of(compact), expected)

    def test_emitted_commands_are_valid_registry_actions(self) -> None:
        registry = load_skill_registry()
        for text, _ in DOCS_EXAMPLES:
            for cmd in commands_of(text) or []:
                action = cmd.split()[0]
                with self.subTest(cmd=cmd):
                    self.assertIn(action, registry.actions)


class SplittingTest(unittest.TestCase):
    def test_hou_after_number_and_unit_splits(self) -> None:
        self.assertEqual(commands_of("前进30厘米后左转90度"), ["forward 30", "left 90"])

    def test_hou_inside_backward_is_not_split(self) -> None:
        self.assertEqual(commands_of("往后退10厘米"), ["backward 10"])
        self.assertEqual(commands_of("后退10厘米"), ["backward 10"])

    def test_connectors_keep_order(self) -> None:
        cases = {
            "前进10厘米接着右转30度之后后退5厘米": ["forward 10", "right 30", "backward 5"],
            "左转90度;前进20厘米": ["left 90", "forward 20"],
            "往后退10厘米，然后把夹子抬起来": ["backward 10", "gripper_up"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands_of(text), expected)


class PhrasingTest(unittest.TestCase):
    def test_numbers_and_units(self) -> None:
        cases = {
            "向前走三十厘米": ["forward 30"],
            "前进1米": ["forward 100"],
            "前进1.5米": ["forward 150"],
            "向前走一百二十公分": ["forward 120"],
            "前进 30 cm": ["forward 30"],
            "后退50mm": ["backward 5"],
            "左转九十度": ["left 90"],
            "右转45°": ["right 45"],
            "左转90": ["left 90"],
            "前进二十五点五厘米": ["forward 26"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands_of(text), expected)

    def test_grippers(self) -> None:
        cases = {
            "抬起左夹爪": ["gripper_left_up"],
            "放下右边的夹子": ["gripper_right_down"],
            "两个夹子都放下来": ["gripper_down"],
            "左夹爪转到60度": ["gripper_left_pos 60"],
            "右夹子转到三十度": ["gripper_right_pos 30"],
            "夹爪转到45度": ["gripper_pos 45"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands_of(text), expected)

    def test_line_follow(self) -> None:
        self.assertEqual(commands_of("沿左侧巡线50厘米"), ["line_follow_left 50"])
        self.assertEqual(commands_of("沿着黑线右边走1米"), ["line_follow_right 100"])

    def test_polite_fillers(self) -> None:
        self.assertEqual(commands_of("请向前走30厘米吧"), ["forward 30"])
        self.assertEqual(commands_of("小车左转90度一下"), ["left 90"])

    def test_english_command_syntax(self) -> None:
        cases = {
            "forward 30": ["forward 30"],
            "forward 2m": ["forward 200"],
            "backward 45cm": ["backward 45"],
            "left 90度": ["left 90"],
            "right 45deg": ["right 45"],
            "stop": ["stop"],
            "gripper down": ["gripper_down"],
            "gripper pos 30": ["gripper_pos 30"],
            "forward 30, left 45, stop, gripper down": [
                "forward 30", "left 45", "stop", "gripper_down"
            ],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(commands_of(text), expected)


class StopTest(unittest.TestCase):
    def test_only_stop_words_is_emergency(self) -> None:
        for text in ("停！停！", "停", "急停", "停下来", "停止吧"):
            with self.subTest(text=text):
                result = match(normalize_text(text))
                self.assertIsNotNone(result)
                assert result is not None
                self.assertTrue(result.is_emergency_stop)

    def test_stop_in_sequence_is_normal_step(self) -> None:
        result = match(normalize_text("前进30厘米然后停止"))
        assert result is not None
        self.assertEqual(result.commands, ["forward 30", "stop"])
        self.assertFalse(result.is_emergency_stop)


class NegativeTest(unittest.TestCase):
    def test_not_matched(self) -> None:
        for text in (
            "左夹子转一转",
            "把红块搬到B区",
            "前进",  # 缺数值
            "前进30",  # 中文说法缺单位
            "前进30厘米然后把红块搬到B区",  # 部分命中也算不命中
            "不要停",
            "停车场",
            "跳个舞",
            "向前走一米半",
            "前进30厘米，一共做二十次",  # 重复次数超过上限
            "",
        ):
            with self.subTest(text=text):
                self.assertIsNone(commands_of(text))


if __name__ == "__main__":
    unittest.main()
