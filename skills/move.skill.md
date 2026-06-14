---
id: move
name: Move
description: "让机器人按厘米数移动"
triggers:
  - move
  - 前进
  - 向前
permissions: low
input_schema:
  type: object
  properties:
    distance_cm:
      type: number
      minimum: 0
      maximum: 5
    speed:
      type: number
      minimum: 0
      maximum: 100
  required:
    - distance_cm
output_schema:
  type: object
  properties:
    status:
      type: string
    detail:
      type: string
runtime: tools.move_tools:execute
examples:
  - nl: "向前走 1 米"
    command:
      action: move
      params:
        distance_cm: 1
        speed: 50
version: "1.0"
---

# 使用说明

目的: 让机器人移动指定的米数，适用于短距离位置调整和避障后微调。

NL 示例:

- "向前走 1 厘米" → {"action":"move","params":{"distance_cm":1}}
 - "后退 0.5 厘米，慢速" → {"action":"move","params":{"distance_cm":0.5,"speed":20,"direction":"backward"}}

直接调用示例:

```python
from tools import move_tools  # 或根据项目结构使用 from pc.tools import move_tools
# 同步调用示例（工具实现可能是 sync 或 async）：
res = move_tools.execute({"distance_cm": 1, "speed": "normal"})
# 返回示例: {"status": "ok", "detail": "moved 1.0m"}
# 错误示例: {"status": "error", "detail": "obstacle detected"}
```

输入说明:

- `distance_cm` (number): 要移动的距离，单位为米，范围由 schema 限制（0–5m）。
- `speed` (number): 可选，范围 0-100，默认 50。
- 可选 `direction` 字段指定 `forward|backward|left|right`，默认向前。
- 可选 `dry_run` 布尔值：`true` 表示仅模拟执行并返回预期结果。

输出说明:

- 成功: `{ "status": "ok", "detail": "moved X m" }`。
- 失败: `{ "status": "error", "detail": "原因描述" }`。

权限与确认:

- 标记为 `permissions: low`，但当 `distance_cm` 超过 1.0m 或存在环境未知时，建议 agent 层进行二次确认。

错误与重试策略:

- 常见错误: 障碍物检测、执行器忙、超时。
- 建议: 对 `obstacle detected` 返回进行 1 次降速重试，或请求用户确认后重试。

测试:

- 若无硬件，可实现一个模拟 `tools.move_tools.execute`，例如返回 `{ "status": "ok", "detail": "<simulated>" }`，用于单元测试与 CI。
