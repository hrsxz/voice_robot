"""
估算一条 Hub 命令的执行时长, 并据此计算等待 Hub 就绪的超时时间。

速度先取保守值 (宁可等久一点, 也不要误报超时),
之后可以用耗时日志里的 "Hub完成" 数据校准。
"""

from __future__ import annotations

# 保守估计值, 之后用实测数据校准
STRAIGHT_SPEED_CM_S = 10.0  # 直行速度
TURN_RATE_DEG_S = 60.0  # 原地转向角速度
LINE_FOLLOW_SPEED_CM_S = 5.0  # 巡线速度, Hub 程序里 LINE_SPEED = 50 mm/s
GRIPPER_SINGLE_S = 1.0  # 单个夹爪转动
GRIPPER_BOTH_S = 2.0  # 两个夹爪依次转动
STOP_S = 0.5

TIMEOUT_FACTOR = 1.5
TIMEOUT_MARGIN_S = 2.0
MIN_TIMEOUT_S = 3.0

_DISTANCE_ACTIONS = {"forward", "backward"}
_TURN_ACTIONS = {"left", "right"}
_LINE_ACTIONS = {"line_follow_left", "line_follow_right"}
_GRIPPER_BOTH = {"gripper_up", "gripper_down", "gripper_pos"}
_GRIPPER_SINGLE = {
    "gripper_left_up", "gripper_left_down", "gripper_left_pos",
    "gripper_right_up", "gripper_right_down", "gripper_right_pos",
}


def estimate_duration_s(cmd: str | None) -> float:
    """估算命令执行时长 (秒)。无法估算的命令返回 0。"""
    if not cmd:
        return 0.0

    parts = cmd.strip().lower().split()
    if not parts:
        return 0.0

    action = parts[0]
    value = _parse_value(parts[1]) if len(parts) > 1 else None

    if action in _DISTANCE_ACTIONS and value is not None:
        return abs(value) / STRAIGHT_SPEED_CM_S
    if action in _TURN_ACTIONS and value is not None:
        return abs(value) / TURN_RATE_DEG_S
    if action in _LINE_ACTIONS and value is not None:
        return abs(value) / LINE_FOLLOW_SPEED_CM_S
    if action in _GRIPPER_BOTH:
        return GRIPPER_BOTH_S
    if action in _GRIPPER_SINGLE:
        return GRIPPER_SINGLE_S
    if action == "stop":
        return STOP_S
    # 不带数值的持续运动 (dc) 会立刻返回就绪
    return 0.0


def ready_timeout_s(previous_cmd: str | None) -> float:
    """等待 Hub 完成上一条命令并报告就绪的超时时间 (秒)。"""
    estimated = estimate_duration_s(previous_cmd)
    return max(MIN_TIMEOUT_S, estimated * TIMEOUT_FACTOR + TIMEOUT_MARGIN_S)


def _parse_value(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None
