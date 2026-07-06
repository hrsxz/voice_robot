from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pc import constants


@dataclass(frozen=True)  # 实例不可变
class ActionRule:
    action: str  # 动作名，比如 forward、left
    route: str
    value_type: str  # 参数类型约束（由 skill 配置定义）
    skill_id: str  # 该规则来自哪个 skill
    runtime: str | None = None
    arg_key: str | None = None
    allowed: tuple[Any, ...] = ()
    min_value: int | float | None = None
    max_value: int | float | None = None

    @classmethod
    def from_mapping(
        cls,
        action: str,
        skill_id: str,
        runtime: str | None,
        data: dict[str, Any],
    ) -> "ActionRule":
        # 把 frontmatter 里某个 action 的配置字典转换成 ActionRule 实例。
        return cls(
            action=action,
            route=str(data["route"]),
            value_type=str(data["value_type"]),
            skill_id=skill_id,
            runtime=runtime,
            arg_key=data.get("arg_key"),
            allowed=tuple(data.get("allowed") or ()),
            min_value=data.get("min"),
            max_value=data.get("max"),
        )


@dataclass(frozen=True)
class SkillRegistry:
    actions: dict[str, ActionRule]

    def get_action_rule(self, action: str) -> ActionRule | None:
        return self.actions.get(action)

    def list_actions(self) -> list[str]:
        return sorted(self.actions)


def load_skill_registry(skills_dir: Path | None = None) -> SkillRegistry:
    skills_path = skills_dir or constants.project_root_path / "skills"
    actions: dict[str, ActionRule] = {}

    for skill_file in sorted(skills_path.glob("*.skill.md")):
        frontmatter = _load_frontmatter(skill_file)
        skill_id = str(frontmatter.get("id"))
        runtime = frontmatter.get("runtime")
        action_rules = frontmatter.get("action_rules") or {}

        if not isinstance(action_rules, dict):
            raise ValueError(f"{skill_file} action_rules must be a mapping")

        for action, rule_data in action_rules.items():
            if not isinstance(rule_data, dict):
                raise ValueError(f"{skill_file} action_rules.{action} must be a mapping")
            if action in actions:
                raise ValueError(f"duplicate action rule: {action}")
            actions[str(action)] = ActionRule.from_mapping(
                action=str(action),
                skill_id=skill_id,
                runtime=str(runtime) if runtime else None,
                data=rule_data,
            )

    return SkillRegistry(actions=actions)


def get_action_rule(action: str, skills_dir: Path | None = None) -> ActionRule | None:
    return load_skill_registry(skills_dir).get_action_rule(action)


def list_actions(skills_dir: Path | None = None) -> list[str]:
    return load_skill_registry(skills_dir).list_actions()


def _load_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    frontmatter_text = _extract_frontmatter(text)
    if not frontmatter_text:
        return {}

    data = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} frontmatter must be a mapping")
    return data


def _extract_frontmatter(markdown_text: str) -> str:
    """
        用 split("---", 2) 最多切三段:
        前导段（通常为空）
        中间段（frontmatter）
        后面的正文
    """
    text = (markdown_text or "").lstrip()
    if not text.startswith("---"):
        return ""

    parts = text.split("---", 2)
    if len(parts) < 3:
        return ""
    # 返回中间段，即 frontmatter
    return parts[1].strip()
