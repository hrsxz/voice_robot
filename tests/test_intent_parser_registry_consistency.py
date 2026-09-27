# pylint: disable=protected-access

import unittest
from typing import ClassVar

from pc.llm import intent_parser
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

    def test_fallback_steps_actions_exist_in_registry(self) -> None:
        text = "向前 30cm，然后左转90度，然后停止，然后 gripper up"

        steps = intent_parser._fallback_steps_from_text(text)

        self.assertEqual(
            steps,
            [
                {"action": "forward", "args": {"distance_cm": 30}},
                {"action": "left", "args": {"angle_deg": 90}},
                {"action": "stop", "args": {}},
                {"action": "gripper_up", "args": {}},
            ],
        )
        for step in steps:
            with self.subTest(step=step):
                self.assertIn(step["action"], self.registry.actions)
                self.assertEqual(step["args"], self._expected_args_for_fallback(step))

    def test_fallback_step_args_match_registry_rules(self) -> None:
        cases = [
            ("forward 2m", {"action": "forward", "args": {"distance_cm": 200}}),
            ("backward 45cm", {"action": "backward", "args": {"distance_cm": 45}}),
            ("left 90度", {"action": "left", "args": {"angle_deg": 90}}),
            ("right 45deg", {"action": "right", "args": {"angle_deg": 45}}),
            ("stop", {"action": "stop", "args": {}}),
            ("gripper down", {"action": "gripper_down", "args": {}}),
            ("gripper pos 30", {"action": "gripper_pos", "args": {"angle_deg": 30}}),
        ]

        for text, expected in cases:
            with self.subTest(text=text):
                steps = intent_parser._fallback_steps_from_text(text)
                self.assertEqual(steps, [expected])
                self.assertEqual(steps[0]["args"], self._expected_args_for_fallback(steps[0]))

    def test_fallback_only_emits_registry_arg_keys(self) -> None:
        text = "forward 30, left 45, stop, gripper down"

        steps = intent_parser._fallback_steps_from_text(text)

        for step in steps:
            with self.subTest(step=step):
                rule = self.registry.get_action_rule(step["action"])
                self.assertIsNotNone(rule)
                assert rule is not None
                if rule.value_type == "none":
                    self.assertEqual(step["args"], {})
                else:
                    self.assertEqual(list(step["args"].keys()), [rule.arg_key])

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

    def _expected_args_for_fallback(self, step: dict) -> dict:
        rule = self.registry.get_action_rule(step["action"])
        self.assertIsNotNone(rule)
        assert rule is not None
        if rule.value_type == "none":
            return {}
        return {rule.arg_key: step["args"].get(rule.arg_key)}


if __name__ == "__main__":
    unittest.main()
