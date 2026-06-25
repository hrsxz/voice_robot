from typing import Any


class RobotAgent:
    """
    单链路执行 Agent:
    输入 -> LLM -> intent_parser -> intent_mapper ->
    RobotAgent.execute_sequence    -> hub
    """
    ACTION_RULES = {
        "stop": {"param": None, "min": None, "max": None, "allow_value": False},
        "forward": {"param": "distance_cm", "min": 0, "max": 10000, "allow_value": True},
        "backward": {"param": "distance_cm", "min": 0, "max": 10000, "allow_value": True},
        "straightforward": {"param": "distance_cm", "min": 0, "max": 10000, "allow_value": True},
        "straightbackward": {"param": "distance_cm", "min": 0, "max": 10000, "allow_value": True},
        "left": {"param": "angle_deg", "min": 0, "max": 10000, "allow_value": True},
        "right": {"param": "angle_deg", "min": 0, "max": 10000, "allow_value": True},
        "face_to": {"param": "angle_deg", "min": 0, "max": 360, "allow_value": True},    
        "gripper_up": {"param": None, "min": None, "max": None, "allow_value": False},
        "gripper_down": {"param": None, "min": None, "max": None, "allow_value": False},
        "gripper_pos": {"param": "angle_deg", "min": 0, "max": 360, "allow_value": True},
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

        for index, item in enumerate(seq):
            ok, normalized_cmd, reason = self._normalize_sequence_item(item)
            if not ok:
                raw_cmd = item.get("cmd") if isinstance(item, dict) else None
                print(f"Skipped command: {raw_cmd}, reason: {reason}")
                result["skipped"].append(
                    {"index": index, "cmd": raw_cmd, "reason": reason}
                )
                continue

            try:
                await self.hub.send(normalized_cmd)
                print(f"Executed command: {normalized_cmd}")
                result["executed"].append(normalized_cmd)
            except Exception as exc:
                print(f"Error executing command: {normalized_cmd}, error: {exc}")
                result["errors"].append(
                    {"index": index, "cmd": normalized_cmd, "error": str(exc)}
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

    def _normalize_sequence_item(self, item: Any) -> tuple[bool, str, str]:
        if not isinstance(item, dict):
            return False, "", "item is not dict"

        raw_cmd = item.get("cmd")
        if not isinstance(raw_cmd, str):
            return False, "", "cmd is not string"

        cmd = raw_cmd.strip()
        if not cmd:
            return False, "", "cmd is empty"

        parts = cmd.split()
        action = parts[0].lower()

        if action not in self.ACTION_RULES:
            return False, "", "unknown action"

        rule = self.ACTION_RULES[action]

        if len(parts) == 1:
            if rule["allow_value"] is False:
                return True, action, ""
            return True, action, ""

        if len(parts) != 2:
            return False, "", "invalid cmd format"

        if rule["allow_value"] is False:
            return False, "", "action does not accept value"

        value = self._parse_int(parts[1])
        if value is None:
            return False, "", "value is not integer"

        min_v = rule["min"]
        max_v = rule["max"]

        if min_v is not None and value < min_v:
            return False, "", "value below min"
        if max_v is not None and value > max_v:
            return False, "", "value above max"

        return True, f"{action} {value}", ""

    @staticmethod
    def _parse_int(text: str) -> int | None:
        try:
            return int(float(text))
        except Exception:
            return None
