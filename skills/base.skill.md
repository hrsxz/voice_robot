---
id: intent_base
name: Intent Base
description: "全局意图输出规范（适用于所有动作技能）"
permissions: low
output_contract:
  type: object
  required:
    - steps
  properties:
    steps:
      type: array
      items:
        type: object
        required:
          - action
          - args
        properties:
          action:
            type: string
          args:
            type: object
json_output_example: |
  {
    "steps": [
      { "action": "forward", "args": { "distance_cm": 30 } },
      { "action": "camera", "args": { "mode": "photo" } },
      { "action": "sensor", "args": { "name": "distance" } }
    ]
  }
hard_rules:
  - 只输出 JSON，不要代码块，不要解释文本。
  - 顶层必须有 steps，且 steps 是数组。
  - 每个 step 只能有两个键：action 和 args。
  - action 必须来自各技能 input_schema 的 action 枚举。
  - args 必须符合对应技能 input_schema 的字段与枚举约束。
  - 若用户说“先A再B”，必须拆成多个 step，按顺序输出。
  - 无法识别时返回 {"steps":[]}。
examples:
  - nl: "向前走30cm，然后左转90度，抓手转到45度"
    output: |
      {
        "steps": [
          { "action": "forward", "args": { "distance_cm": 30 } },
          { "action": "left", "args": { "angle_deg": 90 } },
          { "action": "gripper_pos", "args": { "angle_deg": 45 } }
        ]
      }
  - nl: "拍一张照片，然后读取距离传感器"
    output: |
      {
        "steps": [
          { "action": "camera", "args": { "mode": "photo" } },
          { "action": "sensor", "args": { "name": "distance" } }
        ]
      }
version: "1.0"
---

# 使用说明

这个文件定义全局 JSON 输出约束，不绑定具体动作类别。

- move/camera/sensor 等动作细节应继续在对应 skill 的 `input_schema` 中维护。
- LLM 提示词构建阶段会把本文件 frontmatter 与其他 skills 一起注入。
