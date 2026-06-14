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
    name:
      type: string
  required:
    - name
output_schema:
  type: object
  properties:
    status:
      type: string
    value: {}
runtime: tools.sensor_tool:execute
examples:
  - nl: "读取距离传感器"
    command:
      action: sensor
      params:
        name: distance
version: "1.0"
---

使用说明：

调用 `tools.sensor_tool.execute` 并返回传感器值。
