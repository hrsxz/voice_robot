# 测量记录：voice-quick-wins

## 提示词精简前后对比（任务 4.3 / 4.4）

- 日期：2026-11-12
- 模型：`gpt-6-luna`
- 工具：`python -m scripts.compare_intent_prompts --model gpt-6-luna`
- 新提示词参数：`max_completion_tokens=800`，不传 `reasoning_effort`；模型接受这些参数，没有报错。

| 指标 | 精简前 | 精简后 |
|---|---|---|
| 提示词长度（字符） | 7518 | 1646 |
| LLM 耗时中位数（22 句） | 2192 ms | 1451 ms |
| 解析结果一致 | — | 21 / 22 |

唯一的差异是"向前走半米然后掉头"：两种提示词在重复运行时，都会在 `left 180`、`right 180`、`face_to 180` 之间随机变化，属于模型本身的不确定性，不是精简造成的。

发现的问题：LLM 会输出 Hub 没有实现的 `face_to`（Hub 会忽略它，同时照样回 "OK"）。由 `hub-protocol-v2` 处理：从技能中删除 `face_to`，并让 Hub 对未知命令回 ERR。

## 语音命令耗时（任务 6.2）

待实车测试后补充。
