# 提案：唤醒词 + 启动选择（voice-input-modes）

## Why

现在的唤醒模式是把每一句话都发给云端 STT 转成文字，再判断有没有唤醒词。这意味着屋里所有人说的话（包括小朋友）都会被上传，既涉及隐私又费钱。另外，说完话后要等满 2 秒静音才结束录音，结束提示音还会阻塞识别。我们已经决定按空格和唤醒词两种方式都要支持，并且和两种语音链路组合，在启动时选择。

## What Changes

- 启动时选择输入方式（`ptt` / `wake` / `cli`）和语音链路（`stream` / `realtime`）；不支持的组合给出明确提示。在 `voice-realtime-pipeline` 完成之前，选 `realtime` 会提示暂不可用。
- **BREAKING**：唤醒词改为在本地检测（本地小模型 + VAD），唤醒之前不上传任何音频。原来"云端转写 + 模糊匹配"的唤醒方式不再使用。
- 唤醒后使用流式识别，用静音判断一句话结束，并保留 20 秒连续对话窗口。
- 支持唤醒词后面紧接着说命令（"小蓝小蓝，前进30厘米"）。
- 没有唤醒时，"停止 / 急停"也能在本地识别并触发停止。
- 句末静音时长可配置，默认约 0.7 秒；提示音不阻塞处理。

## Capabilities

### New Capabilities

- 无

### Modified Capabilities

- `speech-input`（由 `voice-streaming` 引入）：新增启动选择、本地唤醒、唤醒前不上传、连续对话、未唤醒时急停、可配置句末静音、提示音不阻塞。

## Impact

- **代码**：`pc/voice_to_robot.py`（启动参数、主循环）、`pc/speech/conversation_gate.py`、`pc/speech/whisper_stt.py`（本地识别）、`pc/llm/audio_utils.py`（VAD 和提示音）、流式识别客户端（加上按静音结束一句话）。
- **依赖**：沿用已有的 faster-whisper、webrtcvad、rapidfuzz，不新增依赖；第一次运行时需要下载本地 tiny 模型。
- **性能**：本地识别只在检测到人声时运行，CPU 占用可以接受。
- **前置条件**：`voice-streaming` 先完成。
- **数据保护**：唤醒前音频只在本机处理；唤醒后才上传。日志会记录每次上传，方便核查。
