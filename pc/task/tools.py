"""
任务技能的工具定义和参数校验 (改动 task-layer 设计 D3, 任务 1.1)。

工具定义来自 skills/task.skill.md 的 task_tools; enum_from: objects / targets 由场地模型填入,
所以模型只能选存在的物体和目标编号 (设计的风险"LLM 编造不存在的物体编号")。
校验严格: 不认识的工具、缺参数、多参数、不在枚举里都拒绝, 不做纠正。

交接工具 run_task (语音层 -> 任务层) 也定义在同一个技能文件的 handoff 里。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from pc.field.model import FieldModel
from skills import load_skill_frontmatters

SKILL_ID = "task"


@dataclass(frozen=True)
class TaskToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # 参数名 -> JSON Schema (可能带 enum_from)
    required: tuple[str, ...]


@dataclass(frozen=True)
class Handoff:
    name: str
    description: str
    arg_key: str


def _task_frontmatter() -> dict[str, Any]:
    fm = load_skill_frontmatters().get(SKILL_ID)
    if not fm:
        raise ValueError("缺少 skills/task.skill.md")
    return fm


def load_task_tools() -> dict[str, TaskToolSpec]:
    raw = _task_frontmatter().get("task_tools") or {}
    specs: dict[str, TaskToolSpec] = {}
    for name, entry in raw.items():
        data = entry or {}
        params = dict(data.get("parameters") or {})
        required = tuple(data.get("required") or ())
        for r in required:
            if r not in params:
                raise ValueError(f"task_tools.{name}: required 里的 {r} 不在 parameters 里")
        specs[str(name)] = TaskToolSpec(
            name=str(name),
            description=" ".join(str(data.get("description") or name).split()),
            parameters=params,
            required=required,
        )
    return specs


def load_handoff() -> Handoff:
    data = _task_frontmatter().get("handoff") or {}
    return Handoff(
        name=str(data.get("name") or "run_task"),
        description=" ".join(str(data.get("description") or "").split()),
        arg_key=str(data.get("arg_key") or "goal"),
    )


def enum_values(field: FieldModel) -> dict[str, list[str]]:
    return {
        "objects": sorted(field.objects),
        "targets": sorted(field.targets),
    }


def _schema(spec: TaskToolSpec, field: FieldModel) -> dict[str, Any]:
    enums = enum_values(field)
    props: dict[str, Any] = {}
    for key, raw in spec.parameters.items():
        prop = copy.deepcopy(raw)
        source = prop.pop("enum_from", None)
        if source is not None:
            prop["enum"] = enums[source]
        props[key] = prop
    return {
        "type": "object",
        "properties": props,
        "required": list(spec.required),
        "additionalProperties": False,
    }


def build_task_tools(field: FieldModel,
                     specs: dict[str, TaskToolSpec] | None = None) -> list[dict[str, Any]]:
    """Chat Completions 格式的工具定义 (任务代理用)。"""
    specs = specs or load_task_tools()
    return [
        {"type": "function",
         "function": {"name": s.name, "description": s.description,
                      "parameters": _schema(s, field)}}
        for s in specs.values()
    ]


def handoff_realtime_tool(handoff: Handoff | None = None) -> dict[str, Any]:
    """realtime 链路的交接工具 (Realtime 接口的函数格式)。"""
    h = handoff or load_handoff()
    return {
        "type": "function",
        "name": h.name,
        "description": h.description,
        "parameters": {
            "type": "object",
            "properties": {h.arg_key: {"type": "string", "description": "用户的原话"}},
            "required": [h.arg_key],
            "additionalProperties": False,
        },
    }


@dataclass(frozen=True)
class CheckedCall:
    name: str
    args: dict[str, Any] | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def check_call(name: str, arguments: str | dict | None, field: FieldModel,
               specs: dict[str, TaskToolSpec] | None = None) -> CheckedCall:
    """严格校验一个工具调用。"""
    specs = specs or load_task_tools()
    spec = specs.get(name)
    if spec is None:
        return CheckedCall(name, None, f"没有这个工具：{name}")
    if isinstance(arguments, dict):
        args: Any = arguments
    else:
        try:
            args = json.loads(arguments) if arguments and arguments.strip() else {}
        except json.JSONDecodeError:
            return CheckedCall(name, None, "参数不是合法的 JSON")
    if not isinstance(args, dict):
        return CheckedCall(name, None, "参数必须是对象")
    # 可选参数传 null / 空字符串 = 没传
    args = {k: v for k, v in args.items() if not (v is None or v == "")}
    extra = sorted(set(args) - set(spec.parameters))
    if extra:
        return CheckedCall(name, None, f"多余的参数：{', '.join(extra)}")
    missing = [r for r in spec.required if r not in args]
    if missing:
        return CheckedCall(name, None, f"缺少参数：{', '.join(missing)}")
    enums = enum_values(field)
    for key, value in args.items():
        schema = spec.parameters[key]
        kind = schema.get("type")
        if kind == "string" and not isinstance(value, str):
            return CheckedCall(name, None, f"{key} 必须是字符串")
        if kind == "array" and (not isinstance(value, list)
                                or not all(isinstance(v, str) for v in value)):
                return CheckedCall(name, None, f"{key} 必须是字符串列表")
        source = schema.get("enum_from")
        if source is not None and value not in enums[source]:
            what = "物体" if source == "objects" else "目标"
            return CheckedCall(name, None, f"没有这个{what}：{value}")
    return CheckedCall(name, args)


__all__ = [
    "CheckedCall",
    "Handoff",
    "TaskToolSpec",
    "build_task_tools",
    "check_call",
    "enum_values",
    "handoff_realtime_tool",
    "load_handoff",
    "load_task_tools",
]
