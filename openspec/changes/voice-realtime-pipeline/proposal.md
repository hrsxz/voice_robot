# 提案：Realtime 语音链路（voice-realtime-pipeline）

## Why

流水线链路（stream）在快速通道没命中时，仍然要经过 STT 和 LLM 两次云端调用。Realtime 链路把语音直接交给实时模型，由模型直接给出动作调用，中间少了一个环节，复杂命令的延迟有机会更低。我们决定两条链路都做，在启动时选择，并用实测数据来比较。

## What Changes

- 新增 `realtime` 链路：语音直接送给实时模型，模型以动作调用（函数调用）的形式给出命令；可用的动作和参数从技能定义自动生成。
- 按空格模式：按住时上传音频，松开时提交这句话。唤醒词模式：本地唤醒后才建立会话、开始上传，连续 20 秒没有说话就关闭会话。
- 动作调用一到达就校验，并放进 `voice-streaming` 的执行队列。
- 停止不依赖模型决策：在转写文字或本地识别中检测到停止词就立即停止。
- 会话失败时给出提示，并自动切换到 stream 链路。
- 耗时记录和 stream 链路格式一致（路由标记为 realtime），并提供用同一批录音对比两条链路的工具。

## Capabilities

### New Capabilities

- `realtime-voice-pipeline`：语音直接交给实时模型、由模型产生动作调用的链路，包括会话的生命周期、动作调用的执行、停止保障和失败回退。

### Modified Capabilities

- 无

## Impact

- **代码**：新增 realtime 会话客户端模块、从技能生成工具定义的模块；`pc/voice_to_robot.py`（接入链路选择）。
- **依赖**：WebSocket 客户端库（如果现有依赖里没有的话）。
- **前置条件**：`voice-streaming`（执行队列）、`voice-input-modes`（启动选择和本地唤醒）、`hub-protocol-v2`（可打断的停止）。
- **费用**：音频 token 比文字贵，只在选择 realtime 时才产生。
- **数据保护**：会话期间音频持续上传到 OpenAI；需要确认 AVV（数据处理协议）和第三国传输的依据。唤醒模式下，唤醒前不上传的原则不变。
