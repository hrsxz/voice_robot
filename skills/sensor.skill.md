---
id: sensor
name: Sensor
description: "读取传感器数据"
triggers:
  - sensor
  - 传感器
  - 读取
permissions: low
input_schema:
  type: object
  properties:
    action:
      type: string
      enum:
        - sensor
    args:
      type: object
      properties:
        name:
          type: string
          enum:
            - distance
            - color
            - gyro
      required:
        - name
      additionalProperties: false
  required:
    - action
    - args
output_schema:
  type: object
  properties:
    status:
      type: string
    value: {}
runtime: pc.tools.sensor_tool:execute
examples:
  - nl: "读取距离传感器"
    command:
      action: sensor
      args:
        name: distance
  - nl: "读取颜色传感器"
    command:
      action: sensor
      args:
        name: color
version: "1.0"
---

# 使用说明

目的: 读取传感器数据。

NL 示例:

- "读取距离传感器" -> {"action":"sensor","args":{"name":"distance"}}
- "读取陀螺仪" -> {"action":"sensor","args":{"name":"gyro"}}

直接调用示例:

```python
from pc.tools import sensor_tool

res = await sensor_tool.execute({"name": "distance", "dry_run": True})
# 可能返回: {"status":"ok","value":42,"unit":"cm","detail":"simulated distance"}
```

注意:

- 当前仅 `distance` 在 dry_run 下返回模拟值。
- `color` 与 `gyro` 当前返回未实现错误。
- 未知传感器名返回 unsupported 错误。
