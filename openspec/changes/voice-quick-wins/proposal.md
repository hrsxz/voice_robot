# 提案：语音快速见效（voice-quick-wins）

## Why

现在从说完话到小车开始动大约要 2–3 秒，主要花在云端 STT 和 LLM 上；而"前进 30 厘米"这类简单命令其实根本不需要 LLM。在做流式识别、Realtime 这些大改动之前，先用改动小、风险低的办法拿到明显提速，同时把各环节的耗时测出来，作为后面优化的基准。

## What Changes

- 新增耗时测量：每条语音命令输出一行各环节耗时（录音结束 → STT 完成 → 路由/LLM 完成 → BLE 发出 → Hub 确认）。
- 新增本地快速通道：严格的整句匹配器，支持中文数字、单位换算，以及 docs 里第 1–3 级的各种说法。只有所有分句都命中才在本地执行，否则整句交给 LLM；"停 / 停止 / 急停"优先级最高。
- **BREAKING**：离线兜底改用严格匹配器。原来按关键词包含来猜动作的解析（会把"左夹子抬起来"误判成左转）不再直接产生动作，识别不了的输入会被拒绝，不再"猜"。
- STT 客户端复用，改为异步调用；按空格录音改为 16 kHz。
- LLM 提示词精简，只保留动作表、参数范围和少量示例；限制输出长度，调低推理强度（看模型是否支持）。
- 修复连续发送长动作时报错的问题：等待 Hub 就绪的超时改为按预计动作时长计算，不再固定 3 秒。
- 修正 README 里的启动命令（`voice_to_command.py` → `voice_to_robot.py`）。

## Capabilities

### New Capabilities

- `latency-telemetry`：语音命令各环节耗时的测量和输出。
- `voice-command-routing`：把识别出的文字路由成机器人命令，包括本地快速通道、急停优先、LLM 兜底。
- `hub-communication`：PC 和 Spike Hub 之间的命令收发与就绪等待。

### Modified Capabilities

- 无

## Impact

- **代码**：`pc/voice_to_robot.py`、`pc/llm/audio_utils.py`、`pc/llm/llm_client.py`、`pc/llm/intent_parser.py`、`pc/spike_communication/spikehub.py`、`pc/tools/move_tools.py`；新增严格匹配器模块和对应测试。
- **依赖**：不新增依赖，中文数字转换自己实现。
- **Hub 程序**：不改。
- **数据保护**：耗时日志只记录时间和命令文本，不保存音频。
- **后续改动**：`voice-streaming`、`hub-protocol-v2` 会在本改动的 capability 上继续追加需求。
