---
id: task
name: Task
description: "任务层技能：把场地上的物体送到区域或圆圈（由任务代理调用）"
version: "1.0"
route: task
permissions: medium
# 任务代理 (pc/task/agent.py) 可用的工具。参数是 JSON Schema;
# enum_from: objects / targets 表示枚举值由程序按场地模型填入 (物体编号 / 目标编号),
# 这样模型不能编造不存在的编号。工具定义由 pc/task/tools.py 生成。
task_tools:
  get_world_state:
    description: 读取完整的世界状态：小车位姿、夹爪、每个物体的位置、状态和当前目标。
    parameters: {}
  move_object:
    description: >-
      把一个物体送到目标（组合技能：前往、抓取、送到目标、放下）。
      target 只有用户明确说了目标时才填；不填时用这个物体的规则目标（"送回家"、按任务编号执行）。
    parameters:
      object: {type: string, enum_from: objects, description: 物体编号}
      target: {type: string, enum_from: targets, description: 目标编号（可选）}
    required: [object]
  go_to:
    description: 开到目标（区域、圆圈或起终点区）的进入点，不抓也不放。
    parameters:
      target: {type: string, enum_from: targets, description: 目标编号}
    required: [target]
  pick:
    description: 抓取一个物体（必须没有夹着别的物体）。
    parameters:
      object: {type: string, enum_from: objects, description: 物体编号}
    required: [object]
  place:
    description: 把夹着的物体放到目标；不填 target 时放到它的规则目标。
    parameters:
      target: {type: string, enum_from: targets, description: 目标编号（可选）}
    required: []
  park:
    description: 回起终点区停车（WRO 任务 5）。
    parameters: {}
  ask_user:
    description: >-
      向用户提问并等待回答（语音或键盘，30 秒内）。请求有歧义（可能指多个物体或多个目标）、
      或者失败后要不要继续时使用。返回用户的回答文字。
    parameters:
      question: {type: string, description: 问题（简短的中文）}
      options: {type: array, items: {type: string}, description: 候选项（给用户读出来）}
    required: [question, options]

# 语音层到任务层的交接 (设计 D1): stream 链路的意图 LLM 输出这一行, realtime 链路调用同名工具
handoff:
  name: run_task
  description: >-
    目标级请求交给任务层：涉及场地上的物体（小人、大人、包裹、服务机器人）、区域（A–D 区）、
    圆圈、起终点区或 WRO 任务（任务1–5）的请求。goal 填用户的原话。直接的移动动作不要用它。
  arg_key: goal
examples:
  - nl: "把黄色小人送回家"
    output: |
      {"action": "run_task", "args": {"goal": "把黄色小人送回家"}}
  - nl: "完成任务1"
    output: |
      {"action": "run_task", "args": {"goal": "完成任务1"}}
---

# 任务技能

任务代理（`pc/task/agent.py`）用这些技能把目标级请求（"把红色服务机器人送到红色圆圈""完成任务 1"）
拆成导航、抓取、放下，执行到底。技能由 `pc/task/skills.py` 在 `navigation-grasp` 之上实现，
返回 `{status: ok | failed | stopped, reason, details, changed}`，`changed` 是状态有变化的物体。

- `move_object` 优先：组合技能，一次调用送一个物体。
- `go_to` / `pick` / `place` 用于失败恢复，或者用户的特殊要求（"带到 B 区附近先别放下"）。
- 规则目标来自 `pc/task/data/missions.yaml`（任务组配置）。
- `handoff`（`run_task`）是语音层到任务层的交接，不是任务代理的工具。
