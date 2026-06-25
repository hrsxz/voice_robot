"""
把结构化意图映射为 hub 命令序列。

支持两种输入：
1) 新格式（多步骤）:
{
  "steps": [
    {"action":"forward","params":{"distance_cm":30,"angle_deg":null}},
    {"action":"left","params":{"distance_cm":null,"angle_deg":90}}
  ]
}

2) 旧格式（单步骤，向后兼容）:
{
  "action":"forward",
  "params":{"distance_cm":30,"angle_deg":null}
}

输出格式：
{
  "sequence": [
    {"cmd":"forward 30"},
    {"cmd":"left 90"}
  ]
}
"""


def _step_to_cmd(step: dict) -> str | None:
    action = (step.get("action") or "").lower()
    params = step.get("params") or {}

    if not action:
        return None

    if action == "stop":
        return "stop"

    if action in ("forward", "backward", "straightforward", "straightbackward"):
        distance = params.get("distance_cm")
        if distance in (None, ""):
            return action
        return f"{action} {int(distance)}"

    if action in ("left", "right"):
        angle = params.get("angle_deg")
        if angle in (None, ""):
            return action
        return f"{action} {int(angle)}"

    if action in ("gripper_up", "gripper_down", "gripper_pos"):
        return action

    # 兜底：直接把 action 当命令
    return action


def intent_to_sequence(intent: dict) -> dict:
    if not isinstance(intent, dict):
        return {"sequence": []}

    seq: list[dict] = []

    steps = intent.get("steps")
    for step in steps:
        if not isinstance(step, dict):
            continue
        cmd = _step_to_cmd(step)
        if cmd:
            seq.append({"cmd": cmd})
    return {"sequence": seq}


def sequence_to_hub_commands(seq_spec: dict) -> list[str]:
    seq = seq_spec.get("sequence", []) if seq_spec else []
    cmds: list[str] = []
    for item in seq:
        cmd = item.get("cmd")
        if not cmd:
            continue
        cmds.append(cmd)
    return cmds
