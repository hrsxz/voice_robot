# Spike：OpenAI 实时转写（任务 1.1）

- 日期：2026-11-12
- 脚本：`python -m scripts.spike_realtime_transcription`
- 录音：`python -m scripts.make_test_audio` 生成的合成语音（OpenAI TTS，24 kHz），取 5 段多分句录音；分句之间有 0.6 s 停顿，首尾各 0.3 s 静音。
- 方法：按实时速度推送音频（每块 40 ms），推完后立即提交（相当于松开空格）。时间都相对"音频结束"计算，负数表示在说完之前就收到了。

## 结果

| 配置 | 说完前拿到分段结果 | 松开 → 最后一段文字 | 识别质量 |
|---|---|---|---|
| A1 `gpt-4o-transcribe` + 服务端 VAD 400 ms | 5/5（第一段约 −2.1 至 −2.6 s） | 中位数 +0.61 s，最大 +0.66 s | 混入繁体字（後、進、釐、個），"做四次" → "坐四次" |
| **A1b** `gpt-4o-transcribe` + 简体中文 prompt + 服务端 VAD 300 ms | **5/5**（第一段约 −2.0 至 −2.9 s） | 中位数 **+0.57 s**，最大 +0.72 s | 全部简体，同音错误消失 |
| A2 `gpt-realtime-whisper`（delay=low）+ 服务端 VAD | — | — | 报错：这个模型不支持服务端轮次检测 |
| A2b `gpt-realtime-whisper`（delay=low），松开时手动提交 | 0/5（只有逐字 delta，没有分段） | 中位数 +0.74 s，最大 +1.05 s | 明显变差："我夹子"、"往后的退"、"三十里米"、"在左转"、"坐四次" |

## 结论

**选定方案 A：OpenAI 实时转写，`gpt-4o-transcribe`，加简体中文 prompt，服务端 VAD 静音阈值 300 ms。** 松开空格时如果缓冲区里还有没提交的音频，就立即手动提交。

- 按住期间按停顿分段的能力满足 `speech-input`"说话中途得到分句"的要求（5/5）。
- 松开后的延迟中位数是 0.57 s，比 0.5 s 的目标略高。原因是测试录音末尾带了 0.3 s 静音，而且服务端 VAD 要等 300 ms 才提交。实际使用时松开空格会立即手动提交，预计会更短，在实车实验中复测（见 `docs/experiments.md`）。如果仍然偏高，可以把 VAD 阈值降到 200 ms。
- 不采用 `gpt-realtime-whisper`：虽然能逐字输出，但中文识别质量明显不够，而且没有分段。
- 不需要方案 B（本地 VAD 分段 + 普通转写接口）。

## 对实现的影响

1. 实时接口**只支持 24 kHz PCM16**，所以流式模式要按 24 kHz 采集；回退时直接把同一段 24 kHz 音频交给整句识别。
2. **整句识别也要加同样的 prompt**，否则回退路径同样会混入繁体字（快速通道认不出繁体）。
3. 需要 `websockets` 依赖（已经用 `uv add` 加入）。连接时要使用系统证书库（公司网络有 TLS 拦截）。
4. 转写会话的连接方式：`client.realtime.connect(extra_query={"intent": "transcription"})`，然后用 `session.update` 设置 `type: "transcription"`。
5. 服务端已经自动提交了的话，松开时再手动提交会收到"buffer too small"错误，可以忽略；更好的做法是只在缓冲区里还有足够音频时才手动提交。
