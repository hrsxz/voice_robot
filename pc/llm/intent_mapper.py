"""
把结构化意图映射为命令序列。

输入契约:
- 输入应为 {"steps": [...]}。
- steps 中每个 step 应为 {"action": str, "args": dict}。
- 参数只从 step.args 读取；若 args 不是 dict，则按空参数处理。
- 无效输入（不是 dict、缺少/错误的 steps）返回 {"sequence": []}。

动作来源与支持范围:
- 动作规则动态来自 skills 目录各 *.skill.md 的 action_rules（经 load_skill_registry 加载并缓存）。
- 常见动作包括:
  - 移动/转向/夹爪: stop, forward, backward, straightforward, straightbackward,
    left, right, face_to, gripper_up, gripper_down, gripper_pos
  - 工具: camera(mode=photo|video), sensor(name=distance|color|gyro)
- 若 action 不在规则表中，保留原 action 作为命令输出。

映射规则:
- value_type == "none": 输出 "action"
- value_type == "int": 读取 rule.arg_key 对应参数，可转为整数则输出 "action <int>"，否则仅输出 "action"
- value_type == "str": 读取 rule.arg_key 对应参数，若不在 allowed 中则回退到 allowed 的第一个值；最终输出 "action <str>"；
  若无法确定字符串参数则仅输出 "action"

输出契约:
{
  "sequence": [
    {"cmd": "forward 30"},
    {"cmd": "camera photo"},
    {"cmd": "sensor distance"}
  ]
}
"""

from functools import lru_cache

from skills import ActionRule, load_skill_registry


@lru_cache(maxsize=1)
def _action_rules() -> dict[str, ActionRule]:
    return load_skill_registry().actions


def _step_to_cmd(step: dict) -> str | None:
    """将单个 step 映射为命令字符串。无法映射时返回 None。"""
    action = (step.get("action") or "").lower()
    payload = step.get("args")
    params = payload if isinstance(payload, dict) else {}

    if not action:
        return None

    rule = _action_rules().get(action)
    if rule is None:
        return action

    if rule.value_type == "none":
        return action

    if rule.value_type == "int":
        value = params.get(rule.arg_key or "")
        if value in (None, ""):
            return action
        parsed_value = _to_int_or_none(value)
        if parsed_value is None:
            return action
        return f"{action} {parsed_value}"

    if rule.value_type == "str":
        raw_value = params.get(rule.arg_key or "")
        value = _normalize_str_value(raw_value, rule)
        if value is None:
            return action
        return f"{action} {value}"

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


def _normalize_str_value(value: object, rule: ActionRule) -> str | None:
    if value not in (None, ""):
        normalized = str(value).lower()
        if not rule.allowed or normalized in rule.allowed:
            return normalized

    if rule.allowed:
        return str(rule.allowed[0]).lower()
    return None
