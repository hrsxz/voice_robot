# 提案：任务层（task-layer）

## Why

项目的最终目标是：用自然语言说出一个目标（"把红色服务机器人送到红色圆圈""完成任务 1"），小车自己把它拆解成导航、抓取、放下这些步骤，并执行到底。`navigation-grasp` 已经提供了可靠的技能，还缺一层能看懂目标、根据世界状态做规划、并在出问题时重试、重新规划或询问用户的任务层。

## What Changes

- 新增任务技能：`get_world_state`、`move_object(物体, 目标)`、`go_to(目标)`、`pick(物体)`、`place(目标)`、`park()`、`ask_user(问题, 选项)`，在新的 skill 文件中定义，并自动生成工具定义。
- 新增任务代理：基于 LLM 工具调用的多轮循环，读取世界状态 → 调用技能 → 读取结果 → 继续，直到目标完成或明确失败；设有步数和时间上限。
- 新增 WRO 任务组配置：任务 1–5 分别对应哪些物体、目标是什么，可以用"完成任务 1"这样的说法。
- 用户明确指定了目标时，以用户为准；按任务编号执行时，以规则里的目标为准。
- 有歧义时（比如"红色的"可能指 4 个物体）先反问，不去猜。
- 喊"停"会取消整个任务；每一步都有进度反馈（终端显示，可选语音播报）。
- 语音链路新增交接：目标级的请求交给任务层，直接动作（"前进 30 厘米"）照旧走原来的路径。

## Capabilities

### New Capabilities

- `task-planning`：把目标级请求拆解成技能调用并执行到底，包括任务组、歧义反问、失败处理、停止和进度反馈。

### Modified Capabilities

- `voice-command-routing`（由 `voice-quick-wins` 引入）：新增"目标级请求交给任务层"。
- `realtime-voice-pipeline`（由 `voice-realtime-pipeline` 引入）：新增"目标级请求交给任务层"。

## Impact

- **代码**：新增 `skills/task.skill.md`、`pc/task/`（任务代理、技能执行器、任务组配置）；修改意图提示词和路由（stream 链路），以及 realtime 的工具列表。
- **前置条件**：`navigation-grasp`（技能和世界状态）、`voice-streaming`（路由）；realtime 交接依赖 `voice-realtime-pipeline`。开发和测试可以在模拟器里进行。
- **费用**：任务代理是多轮 LLM 调用，用步数上限控制。
- **数据保护**：世界状态摘要里只有场地和物体信息，没有个人数据。
