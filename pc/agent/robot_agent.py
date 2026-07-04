from typing import Any

from pc.tools import camera_tools, move_tools, sensor_tool


class RobotAgent:
    """
    单链路执行 Agent:
    输入 -> LLM -> intent_parser -> intent_mapper ->
    RobotAgent.execute_sequence    -> hub
    """
    ACTION_RULES = {
        "stop": {"route": "move", "value_type": "none"},
        "forward": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "backward": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "straightforward": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "straightbackward": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "left": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "right": {"route": "move", "value_type": "int", "min": 0, "max": 10000},
        "face_to": {"route": "move", "value_type": "int", "min": 0, "max": 360},
        "gripper_up": {"route": "move", "value_type": "none"},
        "gripper_down": {"route": "move", "value_type": "none"},
        "gripper_pos": {"route": "move", "value_type": "int", "min": 0, "max": 360},
        "camera": {"route": "camera", "value_type": "str", "allowed": {"photo", "video"}},
        "sensor": {"route": "sensor", "value_type": "str",
                   "allowed":{"distance", "color", "gyro"}},
    }

    def __init__(self, hub: Any):
        self.hub = hub

    async def connect(self) -> None:
        if hasattr(self.hub, "connect"):
            await self.hub.connect()

    async def disconnect(self) -> None:
        if hasattr(self.hub, "disconnect"):
            await self.hub.disconnect()

    async def execute_sequence(self, seq_spec: dict) -> dict:
        """
        统一执行入口，直接消费 intent_mapper 输出:
        {
        "sequence": [
            {"cmd": "forward 30"},
            {"cmd": "left 90"},
            {"cmd": "stop"}
        ]
        }

        统一返回结构:
        {
        "status": "ok|partial|error",
        "executed": [...],
        "skipped": [{"index": i, "cmd": "...", "reason": "..."}],
        "errors": [{"index": i, "cmd": "...", "error": "..."}]
        }
        """
        result = {
            "status": "ok",
            "executed": [],
            "skipped": [],
            "errors": [],
        }

        seq = seq_spec.get("sequence")
        if not isinstance(seq, list):
            result["status"] = "error"
            result["errors"].append({"index": -1, "cmd": None, "error": "sequence is not list"})
            return result

        for index, cmd in enumerate(seq):
            ok, normalized_cmd, reason = self._normalize_sequence_item(cmd)
            if not ok:
                raw_cmd = cmd.get("cmd", None)
                print(f"Skipped command: {raw_cmd}, reason: {reason}")
                result["skipped"].append(
                    {"index": index, "cmd": raw_cmd, "reason": reason}
                )
                continue

            # normalized_cmd = {'action': 'forward', 'value': 30}
            action = normalized_cmd["action"]
            value = normalized_cmd["value"]
            route = self.ACTION_RULES[action]["route"]

            try:
                exec_out = await self._dispatch(route=route, action=action, value=value)
                if exec_out.get("status") == "ok":
                    result["executed"].append(
                        exec_out.get("detail") or self._display_cmd(action, value)
                    )
                else:
                    result["errors"].append(
                        {"index": index,
                         "cmd": self._display_cmd(action, value),
                         "error": exec_out.get("detail", "tool error")}
                    )
            except Exception as exc:
                result["errors"].append(
                    {"index": index,
                     "cmd": self._display_cmd(action, value),
                     "error": str(exc)}
                )

        if result["errors"]:
            if result["executed"]:
                result["status"] = "partial"
            else:
                result["status"] = "error"
        elif result["skipped"]:
            if result["executed"]:
                result["status"] = "partial"
            else:
                result["status"] = "error"
        else:
            result["status"] = "ok"

        return result

    async def _dispatch(self, route: str, action: str, value: Any) -> dict:
        if route == "move":
            return await move_tools.execute({"hub": self.hub, "action": action, "value": value})
        if route == "camera":
            return await camera_tools.execute({"mode": value or "photo", "dry_run": True})
        if route == "sensor":
            return await sensor_tool.execute({"name": value or "distance", "dry_run": True})
        return {"status": "error", "detail": f"unknown route: {route}"}

    def _normalize_sequence_item(self, item: Any) -> tuple[bool, dict, str]:
        if not isinstance(item, dict):
            return False, {}, "item is not dict"

        raw_cmd = item.get("cmd")
        if not isinstance(raw_cmd, str):
            return False, {}, "cmd is not string"

        cmd = raw_cmd.strip()
        if not cmd:
            return False, {}, "cmd is empty"

        parts = cmd.split(maxsplit=1)
        action = parts[0].lower()
        raw_value = parts[1].strip() if len(parts) == 2 else None

        if action not in self.ACTION_RULES:
            return False, {}, "unknown action"

        rule = self.ACTION_RULES[action]
        value_type = rule["value_type"]

        if value_type == "none":
            if raw_value is not None:
                return False, {}, "action does not accept value"
            return True, {"action": action, "value": None}, ""

        if raw_value is None:
            return False, {}, "missing value"

        if value_type == "int":
            value = self._parse_int(raw_value)
            if value is None:
                return False, {}, "value is not integer"
            if "min" in rule and value < rule["min"]:
                return False, {}, "value below min"
            if "max" in rule and value > rule["max"]:
                return False, {}, "value above max"
            return True, {"action": action, "value": value}, ""

        if value_type == "str":
            value = raw_value.lower()
            allowed = rule.get("allowed")
            if allowed and value not in allowed:
                return False, {}, f"value not allowed: {value}"
            return True, {"action": action, "value": value}, ""

        return False, {}, "unknown value_type"

    @staticmethod
    def _parse_int(text: str) -> int | None:
        try:
            return int(float(text))
        except Exception:
            return None

    @staticmethod
    def _display_cmd(action: str, value: Any) -> str:
        return action if value is None else f"{action} {value}"
