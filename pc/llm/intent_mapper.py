"""
把结构化意图映射为命令序列。

输入契约:
- 读取 steps 列表。
- 每个 step 至少包含 action。
- 仅从 step.args 读取参数。

当前支持动作:
- 移动: forward/backward/straightforward/straightbackward
- 转向: left/right/face_to
- 夹爪: gripper_up/gripper_down/gripper_pos
- 工具: camera(mode=photo|video), sensor(name=distance|color|gyro)

输出契约:
{
    "sequence": [
        {"cmd": "forward 30"},
        {"cmd": "camera photo"},
        {"cmd": "sensor distance"}
    ]
}
"""


def _step_to_cmd(step: dict) -> str | None:
    """将单个 step 映射为命令字符串。无法映射时返回 None。"""
    action = (step.get("action") or "").lower()
    payload = step.get("args")
    params = payload if isinstance(payload, dict) else {}

    if not action:
        return None

    if action == "stop":
        return "stop"

    if action in ("forward", "backward", "straightforward", "straightbackward"):
        distance = params.get("distance_cm")
        if distance in (None, ""):
            return action
        parsed_distance = _to_int_or_none(distance)
        if parsed_distance is None:
            return action
        return f"{action} {parsed_distance}"

    if action in ("left", "right", "face_to"):
        angle = params.get("angle_deg")
        if angle in (None, ""):
            return action
        parsed_angle = _to_int_or_none(angle)
        if parsed_angle is None:
            return action
        return f"{action} {parsed_angle}"

    if action in ("gripper_up", "gripper_down", "gripper_pos"):
        if action in ("gripper_up", "gripper_down"):
            return action
        if action == "gripper_pos":
            angle = params.get("angle_deg")
            if angle in (None, ""):
                return action
            parsed_angle = _to_int_or_none(angle)
            if parsed_angle is None:
                return action
            return f"{action} {parsed_angle}"

    if action == "camera":
        mode = str(params.get("mode") or "photo").lower()
        if mode not in ("photo", "video"):
            mode = "photo"
        return f"camera {mode}"

    if action == "sensor":
        name = str(params.get("name") or "distance").lower()
        if name not in ("distance", "color", "gyro"):
            name = "distance"
        return f"sensor {name}"

    # 兜底：直接把 action 当命令
    return action


def intent_to_sequence(intent: dict) -> dict:
    """将意图对象映射为 sequence 结构，异常或无效输入返回空序列。
    parsed intent: {
      'steps': [
        {'action': 'forward', 'args': {'distance_cm': 30}}
        {'action': 'left', 'args': {'angle_deg': 60}}
        {'action': 'gripper_up', 'args': {}}
    ]}
    """
    if not isinstance(intent, dict):
        return {"sequence": []}

    seq: list[dict] = []

    steps = intent.get("steps")

    if not isinstance(steps, list):
        return {"sequence": []}

    for step in steps:
        if not isinstance(step, dict):
            continue
        cmd = _step_to_cmd(step)
        if cmd:
            seq.append({"cmd": cmd})
    return {"sequence": seq}


def _to_int_or_none(value: object) -> int | None:
    """将输入转换为 int；无法转换返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None
