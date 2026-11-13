---
id: move
name: Move
description: "移动与底盘/夹爪控制动作"
version: "1.0"
triggers:
  - 前进
  - 后退
  - 左转
  - 右转
  - 停止
  - 夹爪
permissions: low
input_schema:
  type: object
  properties:
    action:
      type: string
      enum:
        - stop
        - forward
        - backward
        - left
        - right
        - gripper_up
        - gripper_down
        - gripper_pos
        - gripper_left_up
        - gripper_left_down
        - gripper_left_pos
        - gripper_right_up
        - gripper_right_down
        - gripper_right_pos
        - line_follow_left
        - line_follow_right
    args:
      type: object
      properties:
        distance_cm:
          type: integer
          minimum: 0
          maximum: 10000
        angle_deg:
          type: integer
          minimum: 0
          maximum: 360
      additionalProperties: false
  required:
    - action
    - args
  allOf:
    - if:
        properties:
          action:
            enum: [forward, backward, line_follow_left, line_follow_right]
      then:
        properties:
          args:
            required: [distance_cm]
    - if:
        properties:
          action:
            enum: [left, right, gripper_pos,
                   gripper_left_pos, gripper_right_pos]
      then:
        properties:
          args:
            required: [angle_deg]
    - if:
        properties:
          action:
            enum: [stop, gripper_up, gripper_down,
                   gripper_left_up, gripper_left_down,
                   gripper_right_up, gripper_right_down]
      then:
        properties:
          args:
            maxProperties: 0
output_schema:
  type: object
  properties:
    status:
      type: string
    detail:
      type: string
action_rules:
  stop:
    route: move
    description: "立即停止所有运动"
    value_type: none
  forward:
    route: move
    description: "向前直行，distance_cm 是距离（厘米）"
    value_type: int
    arg_key: distance_cm
    min: 0
    max: 10000
  backward:
    route: move
    description: "向后直行，distance_cm 是距离（厘米）"
    value_type: int
    arg_key: distance_cm
    min: 0
    max: 10000
  left:
    route: move
    description: "原地左转，angle_deg 是角度（度）"
    value_type: int
    arg_key: angle_deg
    min: 0
    max: 360
  right:
    route: move
    description: "原地右转，angle_deg 是角度（度）"
    value_type: int
    arg_key: angle_deg
    min: 0
    max: 360
  gripper_up:
    route: move
    description: "两个夹爪（夹子）一起抬起；只说了左边或右边时不要用这个"
    value_type: none
  gripper_down:
    route: move
    description: "两个夹爪（夹子）一起放下；只说了左边或右边时不要用这个"
    value_type: none
  gripper_pos:
    route: move
    description: "两个夹爪（夹子）一起转到指定角度，0 是放下，90 是抬起"
    value_type: int
    arg_key: angle_deg
    min: 0
    max: 90
  gripper_left_up:
    route: move
    description: "只抬起左边的夹爪（左夹子）"
    value_type: none
  gripper_left_down:
    route: move
    description: "只放下左边的夹爪（左夹子）"
    value_type: none
  gripper_left_pos:
    route: move
    description: "左边的夹爪（左夹子）转到指定角度，0 是放下，90 是抬起"
    value_type: int
    arg_key: angle_deg
    min: 0
    max: 90

  gripper_right_up:
    route: move
    description: "只抬起右边的夹爪（右夹子）"
    value_type: none
  gripper_right_down:
    route: move
    description: "只放下右边的夹爪（右夹子）"
    value_type: none
  gripper_right_pos:
    route: move
    description: "右边的夹爪（右夹子）转到指定角度，0 是放下，90 是抬起"
    value_type: int
    arg_key: angle_deg
    min: 0
    max: 90

  line_follow_left:
    route: move
    description: "沿黑线巡线前进（用左侧边缘），distance_cm 是距离（厘米）"
    value_type: int
    arg_key: distance_cm
    min: 1
    max: 10000
  line_follow_right:
    route: move
    description: "沿黑线巡线前进（用右侧边缘），distance_cm 是距离（厘米）"
    value_type: int
    arg_key: distance_cm
    min: 1
    max: 10000

runtime: pc.tools.move_tools:execute
examples:
  - nl: "向前走 30 厘米"
    command:
      action: forward
      args:
        distance_cm: 30
    output_json: |
      {
        "steps": [
          { "action": "forward", "args": { "distance_cm": 30 } }
        ]
      }
  - nl: "左转 90 度"
    command:
      action: left
      args:
        angle_deg: 90
    output_json: |
      {
        "steps": [
          { "action": "left", "args": { "angle_deg": 90 } }
        ]
      }
  - nl: "把夹爪抬起来"
    command:
      action: gripper_up
      args: {}
    output_json: |
      {
        "steps": [
          { "action": "gripper_up", "args": {} }
        ]
      }
---

# 使用说明

目的: 统一描述移动类动作的生成格式，并与当前程序链路对齐。

说明: LLM/intent 层使用 `action + args`，运行时 `move_tools.execute` 使用 `action + value`。
在 RobotAgent 分发前会做参数映射：

- `args.distance_cm` -> `value`（前进/后退/直行相关动作）
- `args.angle_deg` -> `value`（转向/角度相关动作）
- 无参动作（stop/gripper_up/gripper_down）-> `value=None`

NL 示例:

- "向前走 30 厘米" -> {"action":"forward","args":{"distance_cm":30}}
- "后退 20 厘米" -> {"action":"backward","args":{"distance_cm":20}}
- "右转 45 度" -> {"action":"right","args":{"angle_deg":45}}
- "夹爪到 60 度" -> {"action":"gripper_pos","args":{"angle_deg":60}}
- "停止" -> {"action":"stop","args":{}}

直接调用示例:

```python
from pc.tools import move_tools

# move_tools.execute 是 async，运行时要求传 hub/action/value
res = await move_tools.execute({"hub": hub, "action": "forward", "value": 30})
# 成功示例: {"status": "ok", "detail": "forward 30"}
# 失败示例: {"status": "error", "detail": "forward requires value"}
```

输入说明:

- `action`: 见 schema 枚举。
- `args.distance_cm`: 用于 `forward/backward/line_follow_left/line_follow_right`。
- `args.angle_deg`: 用于 `left/right`（0–360），以及 `gripper_pos/gripper_left_pos/gripper_right_pos`（0–90）。
- `stop/gripper_up/gripper_down` 等不需要参数，使用空对象 `args: {}`。
- 这里声明的每个动作，Hub 程序（`spike_hub/control_by_llm.py`）都必须能执行，由一致性测试保证。
  按绝对朝向转向（原来的 `face_to`）以后由导航模块用视觉朝向实现。

输出说明:

- 成功: `{ "status": "ok", "detail": "<sent cmd>" }`。
- 失败: `{ "status": "error", "detail": "原因描述" }`。
