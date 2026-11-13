"""
WRO 任务组 (改动 task-layer 设计 D5, 任务 1.3)。

    rules = load_missions()               # 默认规则 (missions.yaml 的 default, 现在是 wro2025)
    rules = load_missions("starter2022")
    rules.mission(1).items                # {"yellow_person": "zone:A", ...}
    rules.rule_targets(field)             # 每个物体的规则目标 ("送回家"用它)

规则目标只来自这里; 场地模型物体目录里的 target 是没有出现在任何任务里时的后备值。
加载时校验: 物体和目标必须在场地模型里存在, 同一个物体不能出现在两个任务里。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pc.field.model import FieldModel, FieldModelError, read_yaml

MISSIONS_PATH = Path(__file__).parent / "data" / "missions.yaml"


@dataclass(frozen=True)
class Mission:
    number: int
    names_zh: tuple[str, ...]
    names_de: tuple[str, ...]
    items: dict[str, str]  # 物体 -> 规则目标
    park: bool = False

    def describe(self) -> str:
        names = "、".join(self.names_zh + self.names_de)
        if self.park:
            body = "回起终点区停车（park）"
        else:
            body = "，".join(f"{oid} → {target}" for oid, target in self.items.items())
        return f"任务 {self.number}（{names}）：{body}"


@dataclass(frozen=True)
class MissionRules:
    name: str
    title: str
    missions: dict[int, Mission]

    def mission(self, number: int) -> Mission:
        try:
            return self.missions[number]
        except KeyError:
            raise KeyError(f"没有任务 {number}") from None

    def rule_targets(self, field: FieldModel) -> dict[str, str]:
        """每个物体的规则目标: 任务里写的优先, 其余用场地模型目录里的默认目标。"""
        targets = {oid: spec.target for oid, spec in field.objects.items()}
        for m in self.missions.values():
            targets.update(m.items)
        return targets

    def describe(self) -> str:
        return "\n".join(m.describe() for m in self.missions.values())


def load_missions(ruleset: str | None = None, field: FieldModel | None = None,
                  path: str | Path = MISSIONS_PATH) -> MissionRules:
    data = read_yaml(path)
    return parse_missions(data, ruleset, field, source=Path(path).name)


def parse_missions(data: dict[str, Any], ruleset: str | None = None,
                   field: FieldModel | None = None, source: str = "") -> MissionRules:
    rulesets = data.get("rulesets") or {}
    name = ruleset or data.get("default")
    if not name or name not in rulesets:
        raise FieldModelError([f"没有这份规则：{name}（可选：{', '.join(rulesets)}）"], source)
    raw = rulesets[name] or {}
    errors: list[str] = []
    missions: dict[int, Mission] = {}
    seen: dict[str, int] = {}
    for key, m in (raw.get("missions") or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            errors.append(f"任务编号必须是整数：{key}")
            continue
        entry = m or {}
        names = entry.get("names") or {}
        items = {str(k): str(v) for k, v in (entry.get("items") or {}).items()}
        park = bool(entry.get("park", False))
        if not items and not park:
            errors.append(f"任务 {number} 既没有物体也不是停车")
        for oid, target in items.items():
            if oid in seen:
                errors.append(f"{oid} 同时出现在任务 {seen[oid]} 和任务 {number} 里")
            seen[oid] = number
            if field is not None:
                if oid not in field.objects:
                    errors.append(f"任务 {number}：场地模型里没有物体 {oid}")
                if target not in field.targets:
                    errors.append(f"任务 {number}：场地模型里没有目标 {target}")
        missions[number] = Mission(
            number=number,
            names_zh=tuple(str(n) for n in names.get("zh") or ()),
            names_de=tuple(str(n) for n in names.get("de") or ()),
            items=items,
            park=park,
        )
    if not missions:
        errors.append(f"规则 {name} 里没有任务")
    if errors:
        raise FieldModelError(errors, source)
    return MissionRules(name=name, title=str(raw.get("title") or name),
                        missions=dict(sorted(missions.items())))


__all__ = ["MISSIONS_PATH", "Mission", "MissionRules", "load_missions", "parse_missions"]
