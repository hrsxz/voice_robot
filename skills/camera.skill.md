---
id: camera
name: Camera
description: "拍照或录制视频"
triggers:
  - camera
  - 拍照
  - 照相
  - 录制
permissions: low
input_schema:
  type: object
  properties:
    action:
      type: string
      enum:
        - camera
    args:
      type: object
      properties:
        mode:
          type: string
          enum:
            - photo
            - video
      required:
        - mode
      additionalProperties: false
  required:
    - action
    - args
output_schema:
  type: object
  properties:
    status:
      type: string
    path:
      type: string
runtime: pc.tools.camera_tools:execute
examples:
  - nl: "拍一张照片"
    command:
      action: camera
      args:
        mode: photo
  - nl: "录制视频"
    command:
      action: camera
      args:
        mode: video
version: "1.0"
---

# 使用说明

目的: 拍照或录制视频，返回文件路径或错误信息。

NL 示例:

- "拍一张照片" -> {"action":"camera","args":{"mode":"photo"}}
- "录制视频" -> {"action":"camera","args":{"mode":"video"}}

直接调用示例:

```python
from pc.tools import camera_tools

res = await camera_tools.execute({"mode": "photo", "dry_run": True})
# 可能返回: {"status":"ok","path":"captures/photo_*.jpg","detail":"simulated photo"}
```

注意:

- `mode` 支持 `photo` 或 `video`。
- 当前实现中 `video` 会返回未实现错误。
- `dry_run` 默认是 `true`，便于无硬件调试。
- 错误返回格式为 `{"status":"error","detail":"..."}`。

测试:

- 当前 `dry_run=true` 已提供模拟返回，可直接用于联调。
