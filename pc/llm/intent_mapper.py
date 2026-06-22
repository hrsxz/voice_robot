"""
把结构化意图映射为 hub 命令序列。

输出格式：
{
  "sequence": [
    {"cmd":"forward", "duration_s": 1.2},   # duration_s 可选，调用端负责等待后发送 stop
    {"cmd":"stop"}
  ]
}
如果意图简单（如 stop），sequence 只包含一项。
"""


def intent_to_sequence(intent: dict) -> dict:
    action = (intent.get("action") or "").lower()
    params = intent.get("params") or {}

    if action is None or action == "":
        return {"sequence": []}

    if action == "stop":
        return {"sequence": [{"cmd": "stop"}]}

    if action in ("forward", "backward"):
        distance = params.get("distance_cm")
        if distance in (None, ""):
            seq = [{"cmd": action}]
        else:
            seq = [{"cmd": f"{action} {int(distance)}"}]

        return {"sequence": seq}

    if action in ("left", "right"):
        angle = params.get("angle_deg")
        if angle in (None, ""):
            seq = [{"cmd": action}]
        else:
            seq = [{"cmd": f"{action} {int(angle)}"}]

        return {"sequence": seq}

    # 兜底：直接把 action 当作命令发出
    return {"sequence": [{"cmd": action}]}


def sequence_to_hub_commands(seq_spec: dict) -> list[str]:
    """
    把 sequence 转换为要发送到 hub 的文本命令列表。
    当前策略：
            - 对于带 duration 的项：发送 cmd，然后上位机睡眠 duration，再发送 stop（由 voice_to_command 执行）。
            - 返回仅为命令 name 列表（voice_to_command 可据此并结合 duration 执行）。
    返回示例（仅命令名）: ["forward", "stop"]
    """
    seq = seq_spec.get("sequence", []) if seq_spec else []
    cmds = []
    for item in seq:
        cmd = item.get("cmd")
        if not cmd:
            continue
        cmds.append(cmd)
        # stop 直接包含为命令
    return cmds
