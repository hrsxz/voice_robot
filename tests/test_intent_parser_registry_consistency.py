
import unittest
from typing import ClassVar

from pc.llm import command_matcher, intent_parser
from pc.utils.utils import normalize_text
from skills import SkillRegistry, load_skill_registry


class IntentParserRegistryConsistencyTest(unittest.TestCase):
    registry: ClassVar[SkillRegistry]

    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = load_skill_registry()

    def test_canonical_actions_are_recognized(self) -> None:
        for action in self.registry.list_actions():
            with self.subTest(action=action):
                self.assertEqual(intent_parser._normalize_action(action), action)

    def test_normalize_step_matches_registry_rules(self) -> None:
        for action, rule in self.registry.actions.items():
            with self.subTest(action=action):
                step = {"action": action, "args": self._sample_args(rule)}
                normalized = intent_parser._normalize_step(step)

                self.assertIsNotNone(normalized)
                assert normalized is not None
                self.assertEqual(normalized["action"], action)
                self.assertEqual(normalized["args"], self._expected_args(rule))

    def test_parse_intent_uses_registry_compatible_shape(self) -> None:
        payload = {
            "steps": [
                {"action": "forward", "args": {"distance_cm": 30}},
                {"action": "camera", "args": {"mode": "photo"}},
                {"action": "sensor", "args": {"name": "distance"}},
            ]
        }

        parsed = intent_parser.parse_intent(str(payload).replace("'", '"'))

        self.assertEqual(
            parsed,
            {
                "steps": [
                    {"action": "forward", "args": {"distance_cm": 30}},
                    {"action": "camera", "args": {"mode": "photo"}},
                    {"action": "sensor", "args": {"name": "distance"}},
                ]
            },
        )

    def test_non_json_output_is_not_guessed(self) -> None:
        # 不再按关键词猜动作 (原 _fallback_steps_from_text), 非 JSON 输出得到空结果
        for text in ("向前 30cm，然后左转90度", "forward 30, left 45", "", "好的"):
            with self.subTest(text=text):
                self.assertEqual(intent_parser.parse_intent(text), {"steps": []})

    def test_json_without_action_or_steps_gives_empty_result(self) -> None:
        self.assertEqual(intent_parser.parse_intent('{"foo": 1}'), {"steps": []})

    def test_ndjson_lines_are_parsed(self) -> None:
        text = "\n".join([
            '{"action":"forward","args":{"distance_cm":30}}',
            "这一行不是 JSON",
            '{"action":"left","args":{"angle_deg":90}}',
        ])
        self.assertEqual(
            intent_parser.parse_intent(text),
            {"steps": [
                {"action": "forward", "args": {"distance_cm": 30}},
                {"action": "left", "args": {"angle_deg": 90}},
            ]},
        )

    def test_unknown_action_line_is_kept_for_rejection(self) -> None:
        # 不认识的动作不静默丢掉, 交给 RobotAgent 拒绝并记录原因
        self.assertEqual(
            intent_parser.parse_line('{"action":"dance","args":{}}'),
            [{"action": "dance", "args": {}}],
        )

    def test_fast_path_commands_match_registry_rules(self) -> None:
        # 快速通道生成的命令必须能通过同一套 registry 规则校验
        cases = [
            "forward 2m", "backward 45cm", "left 90度", "right 45deg", "stop",
            "gripper down", "gripper pos 30", "左夹爪转到60度", "沿左侧巡线50厘米",
        ]
        for text in cases:
            result = command_matcher.match(normalize_text(text))
            self.assertIsNotNone(result, text)
            assert result is not None
            for cmd in result.commands:
                with self.subTest(text=text, cmd=cmd):
                    parts = cmd.split()
                    rule = self.registry.get_action_rule(parts[0])
                    self.assertIsNotNone(rule)
                    assert rule is not None
                    if rule.value_type == "none":
                        self.assertEqual(len(parts), 1)
                    else:
                        value = int(parts[1])
                        self.assertGreaterEqual(value, rule.min_value)
                        self.assertLessEqual(value, rule.max_value)

    @staticmethod
    def _sample_args(rule) -> dict:
        if rule.value_type == "none":
            return {}
        if rule.value_type == "int":
            value = rule.min_value if rule.min_value is not None else 1
            return {rule.arg_key: int(value)}
        if rule.value_type == "str":
            if rule.allowed:
                return {rule.arg_key: rule.allowed[0]}
            return {rule.arg_key: "sample"}
        return {}

    @staticmethod
    def _expected_args(rule) -> dict:
        if rule.value_type == "none":
            return {}
        if rule.value_type == "int":
            value = rule.min_value if rule.min_value is not None else 1
            return {rule.arg_key: int(value)}
        if rule.value_type == "str":
            if rule.allowed:
                return {rule.arg_key: str(rule.allowed[0]).lower()}
            return {rule.arg_key: "sample"}
        return {}


if __name__ == "__main__":
    unittest.main()
