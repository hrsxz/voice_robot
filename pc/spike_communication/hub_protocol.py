"""
PC 和 Spike Hub 之间的协议 v2 (Hub 端程序: spike_hub/control_by_llm.py)。

PC -> Hub: 每条命令一行, 例如 "forward 30\\n"; 另有 "version"、"hb" (心跳)、"bye"。
Hub -> PC: 每条回复一行:

    rdy                                    空闲, 可以接收下一条普通命令
    ACK <cmd>                              开始执行
    DONE <cmd> gl=<0-90> gr=<0-90>         执行完成, 附两个夹爪角度 (0=放下, 90=抬起)
    STOPPED <cmd> gl=.. gr=.. [reason=..]  被 stop 打断 (reason=watchdog 表示心跳超时)
    ERR <cmd> <reason>                     无法执行: unknown | missing_value | bad_value | busy
    VERSION <n>                            对 "version" 的回答

执行动作期间 Hub 仍然读取新命令: 只接受 stop 和 hb, 其他命令回 ERR busy。
"""

from __future__ import annotations

from dataclasses import dataclass

PROTOCOL_VERSION = 2

# Hub 支持的动作 -> 参数要求
#   none:      不带参数
#   distance:  必须带距离 (厘米, >= 0)
#   angle90:   必须带角度 0-90
#   optional_distance / optional_angle: 带数值是定量动作, 不带是持续运动 (需要心跳)
HUB_ACTIONS: dict[str, str] = {
    "forward": "optional_distance",
    "backward": "optional_distance",
    "left": "optional_angle",
    "right": "optional_angle",
    "stop": "none",
    "gripper_up": "none",
    "gripper_down": "none",
    "gripper_pos": "angle90",
    "gripper_left_up": "none",
    "gripper_left_down": "none",
    "gripper_left_pos": "angle90",
    "gripper_right_up": "none",
    "gripper_right_down": "none",
    "gripper_right_pos": "angle90",
    "line_follow_left": "distance",
    "line_follow_right": "distance",
}

CONTROL_COMMANDS = {"version", "hb", "bye"}

ERR_REASONS = {
    "unknown": "未知命令",
    "missing_value": "缺少参数",
    "bad_value": "参数非法",
    "busy": "正在执行其他动作",
}


@dataclass(frozen=True)
class HubReply:
    kind: str  # RDY | ACK | DONE | STOPPED | ERR | VERSION | OTHER
    cmd: str = ""
    gl: int | None = None
    gr: int | None = None
    reason: str | None = None
    raw: str = ""

    @property
    def gripper(self) -> tuple[int, int] | None:
        if self.gl is None or self.gr is None:
            return None
        return (self.gl, self.gr)

    @property
    def reason_text(self) -> str:
        return ERR_REASONS.get(self.reason or "", self.reason or "")


def parse_reply(line: str) -> HubReply:
    text = (line or "").strip()
    tokens = text.split()
    if not tokens:
        return HubReply("OTHER", raw=text)

    head = tokens[0]
    if head == "rdy" and len(tokens) == 1:
        return HubReply("RDY", raw=text)
    if head == "VERSION" and len(tokens) == 2:
        return HubReply("VERSION", cmd=tokens[1], raw=text)
    if head == "ACK" and len(tokens) >= 2:
        return HubReply("ACK", cmd=" ".join(tokens[1:]), raw=text)
    if head == "ERR" and len(tokens) >= 3:
        return HubReply("ERR", cmd=" ".join(tokens[1:-1]), reason=tokens[-1], raw=text)
    if head in ("DONE", "STOPPED") and len(tokens) >= 2:
        cmd_tokens: list[str] = []
        fields: dict[str, str] = {}
        for tok in tokens[1:]:
            if "=" in tok:
                key, _, value = tok.partition("=")
                fields[key] = value
            elif not fields:
                cmd_tokens.append(tok)
        return HubReply(
            head,
            cmd=" ".join(cmd_tokens),
            gl=_to_int(fields.get("gl")),
            gr=_to_int(fields.get("gr")),
            reason=fields.get("reason"),
            raw=text,
        )
    return HubReply("OTHER", raw=text)


def validate_command(cmd: str) -> str | None:
    """按 Hub 的规则检查一条命令, 合法返回 None, 否则返回 ERR 原因 (和 Hub 程序一致)。"""
    parts = (cmd or "").split()
    if not parts or parts[0] not in HUB_ACTIONS:
        return "unknown"
    kind = HUB_ACTIONS[parts[0]]
    value = parts[1] if len(parts) > 1 else None
    if len(parts) > 2:
        return "bad_value"
    if kind == "none":
        return None if value is None else "bad_value"
    if value is None:
        return None if kind.startswith("optional") else "missing_value"
    number = _to_int(value)
    if number is None or number < 0:
        return "bad_value"
    if kind == "angle90" and number > 90:
        return "bad_value"
    return None


def is_continuous(cmd: str) -> bool:
    """不带数值的前进/后退/转向: 持续运动, 需要心跳。"""
    parts = (cmd or "").split()
    return len(parts) == 1 and HUB_ACTIONS.get(parts[0], "").startswith("optional")


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
