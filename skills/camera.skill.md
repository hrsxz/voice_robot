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
    mode:
      type: string
      enum:
        - photo
        - video
  required:
    - mode
output_schema:
  type: object
  properties:
    status:
      type: string
    path:
      type: string
runtime: tools.camera_tools:execute
examples:
  - nl: "拍一张照片"
    command:
      action: camera
      params:
        mode: photo
version: "1.0"
---

# 使用说明

目的: 拍照或录制视频，返回文件路径或错误信息。

NL 示例:

- "拍一张照片" → {"action":"camera","params":{"mode":"photo"}}

直接调用示例:

```python
from tools import camera_tools  # 或根据项目结构使用 `from pc.tools import camera_tools`
res = camera_tools.execute({"mode":"photo"})
# 可能返回: {"status":"ok","path":"/tmp/photo.jpg"} 或 {"status":"error","detail":"camera busy"}
```

注意:

- `mode` 支持 `photo` 或 `video`。
- 支持可选 `dry_run` 参数用于测试：`{"mode":"photo","dry_run":true}` 会返回模拟结果。
- 摄像头可能需要预热，首次调用延迟较大。
- 错误返回格式为 `{"status":"error","detail":"..."}`，高风险操作应在 agent 层做二次确认。

测试:

- 若无硬件，可实现一个模拟的 `tools.camera_tools.execute` 返回 `{"status":"ok","path":"<simulated>"}` 用于开发与单元测试。
