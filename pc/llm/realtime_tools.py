"""
realtime 链路的工具 (函数) 定义和函数调用的校验。

每个技能动作生成一个函数 (见 openspec/changes/voice-realtime-pipeline/design.md D2):
- 无参动作 -> 参数为空对象, 例如 gripper_up()
- int 动作 -> 一个整数参数, 范围和技能一致, 例如 forward(distance_cm: 0–10000)
- str 动作 -> 一个枚举参数, 例如 camera(mode: photo | video)

模型给出的函数调用在这里严格校验 (不认识的函数、缺参数、多参数、类型不对、超范围都拒绝,
不像 LLM 文字输出那样宽松纠正), 转换成执行队列使用的命令字符串 ("forward 30")。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from skills import ActionRule, SkillRegistry, load_skill_registry

INSTRUCTIONS = "\n".join([
    "你是一个乐高小车的语音控制器。用户用中文说出动作命令，你只通过调用工具让小车执行。",
    "规则：",
    '- 每次只调用一个工具，表示一个动作；"先A再B"按顺序依次调用。'
    "工具结果会告诉你这句话已经入队了几个动作。",
    '- "一共做N次"：把这句话前面的全部动作按顺序完整做N遍（第一遍也算在内）。'
    '例如"前进10厘米，再左转90度，一共做两次"要依次调用4次：前进、左转、前进、左转。',
    '- 距离统一换算成厘米（1米=100厘米），角度用度；"转一圈"是360度，"掉头"是180度。',
    '- 只说"左转"或"右转"没说角度时用90度。',
    '- "夹子"就是夹爪。说了"左夹子"或"右夹子"时只动那一边'
    "（gripper_left_* / gripper_right_*），没说左右才两个一起动。",
    '- 用户的话里可能带唤醒词"小蓝小蓝"或"你好小蓝"，忽略它，只执行后面的命令；'
    "只有唤醒词、没有命令时，不调用工具，也不要回复。",
    '- 一句话里有做不到的部分（比如"跳个舞"）时，能做的动作照常调用，'
    "最后用一句很短的中文说明哪部分做不了。",
    "- 听不清、不是命令、或者参数超出范围时，不要调用工具，用一句很短的中文说明原因。",
    "- 涉及场地上的物体（小人、大人、包裹、服务机器人）、区域（A–D 区）、圆圈、起终点区"
    '或 WRO 任务（"完成任务1"）的请求，调用一次 run_task，goal 填用户的原话，'
    "不要自己拆成动作；之后不再调用其他工具。直接的移动动作不要用 run_task。",
    "- 动作全部调用完后不需要文字回复。",
])


@dataclass(frozen=True)
class ToolCallResult:
    cmd: str | None  # 合法时的命令字符串, 例如 "forward 30"
    error: str | None = None  # 不合法时的原因

    @property
    def ok(self) -> bool:
        return self.cmd is not None


def tool_for_rule(rule: ActionRule) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if rule.value_type in ("int", "str"):
        if not rule.arg_key:
            raise ValueError(f"action {rule.action} 缺少 arg_key")
        if rule.value_type == "int":
            prop: dict[str, Any] = {"type": "integer"}
            if rule.min_value is not None:
                prop["minimum"] = rule.min_value
            if rule.max_value is not None:
                prop["maximum"] = rule.max_value
        else:
            prop = {"type": "string"}
            if rule.allowed:
                prop["enum"] = [str(a) for a in rule.allowed]
        properties[rule.arg_key] = prop
        required.append(rule.arg_key)
    return {
        "type": "function",
        "name": rule.action,
        "description": rule.description or rule.action,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def build_tools(registry: SkillRegistry | None = None) -> list[dict[str, Any]]:
    """为技能 registry 里的每个动作生成一个函数定义 (按动作名排序, 便于服务端缓存)。"""
    registry = registry or load_skill_registry()
    return [tool_for_rule(registry.actions[name]) for name in registry.list_actions()]


def build_session_tools(registry: SkillRegistry | None = None) -> list[dict[str, Any]]:
    """realtime 会话的全部工具: 每个动作一个函数, 加上交接给任务层的 run_task (改动 task-layer)。"""
    from pc.task.tools import handoff_realtime_tool

    return [*build_tools(registry), handoff_realtime_tool()]


def handoff_goal(arguments: str | dict | None) -> str | None:
    """run_task 调用的 goal 参数; 不合法时返回 None。"""
    from pc.task.tools import load_handoff

    key = load_handoff().arg_key
    if isinstance(arguments, dict):
        args: Any = arguments
    else:
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except json.JSONDecodeError:
            return None
    if not isinstance(args, dict):
        return None
    goal = args.get(key)
    return goal.strip() if isinstance(goal, str) and goal.strip() else None


def tool_call_to_cmd(
    name: str, arguments: str | dict | None, registry: SkillRegistry | None = None
) -> ToolCallResult:
    """校验一个函数调用, 合法时返回命令字符串。"""
    registry = registry or load_skill_registry()
    rule = registry.get_action_rule(name)
    if rule is None:
        return ToolCallResult(None, f"未知的动作：{name}")

    if isinstance(arguments, dict):
        args: Any = arguments
    else:
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except json.JSONDecodeError:
            return ToolCallResult(None, "参数不是合法的 JSON")
    if not isinstance(args, dict):
        return ToolCallResult(None, "参数必须是对象")

    if rule.value_type == "none":
        if args:
            return ToolCallResult(None, f"{name} 不需要参数")
        return ToolCallResult(name)

    key = rule.arg_key or ""
    extra = sorted(set(args) - {key})
    if extra:
        return ToolCallResult(None, f"多余的参数：{', '.join(extra)}")
    if key not in args:
        return ToolCallResult(None, f"缺少参数 {key}")
    value = args[key]

    if rule.value_type == "int":
        if isinstance(value, bool):
            return ToolCallResult(None, f"{key} 必须是整数")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            return ToolCallResult(None, f"{key} 必须是整数")
        if rule.min_value is not None and value < rule.min_value:
            return ToolCallResult(None, f"{key}={value} 小于最小值 {rule.min_value}")
        if rule.max_value is not None and value > rule.max_value:
            return ToolCallResult(None, f"{key}={value} 超过最大值 {rule.max_value}")
        return ToolCallResult(f"{name} {value}")

    if rule.value_type == "str":
        if not isinstance(value, str):
            return ToolCallResult(None, f"{key} 必须是字符串")
        value = value.strip().lower()
        if rule.allowed and value not in rule.allowed:
            allowed = " / ".join(str(a) for a in rule.allowed)
            return ToolCallResult(None, f"{key}={value} 不在可选值内（{allowed}）")
        return ToolCallResult(f"{name} {value}")

    return ToolCallResult(None, f"不支持的参数类型：{rule.value_type}")
