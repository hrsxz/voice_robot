---
id: intent_base
name: Intent Base
description: "全局意图输出规范（适用于所有动作技能）"
version: "2.0"
permissions: low
# 每行一个动作 (NDJSON)，方便边生成边执行：收到完整的一行就可以开始执行这个动作
output_contract:
  format: ndjson
  line:
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
  {"action": "forward", "args": {"distance_cm": 30}}
  {"action": "camera", "args": {"mode": "photo"}}
  {"action": "sensor", "args": {"name": "distance"}}
hard_rules:
  - 每行只输出一个 JSON 对象，表示一个动作，格式为 {"action":"<动作>","args":{...}}。
  - 不要代码块，不要解释文本，不要把多个动作放在同一行。
  - action 必须是"可用动作"里列出的名字，args 必须符合对应的字段和范围。
  - 若用户说“先A再B”，按顺序分成多行输出。
  - 如果给出了"已执行"的动作，只输出剩下还需要执行的动作，不要重复已执行的。
  - 无法识别时不输出任何内容。
examples:
  - nl: "向前走30cm，然后左转90度，抓手转到45度"
    output: |
      {"action": "forward", "args": {"distance_cm": 30}}
      {"action": "left", "args": {"angle_deg": 90}}
      {"action": "gripper_pos", "args": {"angle_deg": 45}}
  - nl: "拍一张照片，然后读取距离传感器"
    output: |
      {"action": "camera", "args": {"mode": "photo"}}
      {"action": "sensor", "args": {"name": "distance"}}
  - nl: "已执行：forward 30。用户：然后原地转一圈"
    output: |
      {"action": "left", "args": {"angle_deg": 360}}
---

# 使用说明

这个文件定义全局输出约束，不绑定具体动作类别。

- 输出格式是每行一个动作（NDJSON），系统每收到完整的一行就校验并放进执行队列，不等全部输出完。
- 解析器仍然兼容旧格式 `{"steps": [...]}`。
- move/camera/sensor 等动作细节应继续在对应 skill 的 `input_schema` 中维护。
- LLM 提示词构建阶段会把本文件的 hard_rules 和 examples 与其他 skills 的动作表一起注入。
