# 提案：Hub 协议升级（hub-protocol-v2）

## Why

现在 Hub 执行动作时是阻塞的，喊"停"要等动作做完才生效，不安全。Hub 对不认识的命令也回 "OK"，PC 会误以为执行成功。Hub 不上报夹爪状态，PC 不知道夹爪是抬起还是放下。不带数值的持续运动在蓝牙断开后会一直开下去。小朋友使用的演示机器人必须能随时停下来。

## What Changes

- Hub 程序改为非阻塞执行：直行、转向、巡线、夹爪动作执行期间持续检查新命令，收到 `stop` 立即停下。
- **BREAKING**：新的回复格式。开始执行时回 `ACK`，结束时回 `DONE` 或 `STOPPED`（带两个夹爪的角度），无法执行时回 `ERR`（带原因）。PC 和 Hub 程序必须一起更新。
- 新增版本握手：PC 连上后先查询 Hub 程序的协议版本，版本不对就提示更新，并拒绝发送运动命令。
- 未知命令和非法参数一律回 `ERR`，不再回成功。
- **BREAKING**：技能声明和 Hub 能力对齐。Hub 新增实现 `gripper_pos`；从技能里删除没有实现的 `straightforward`、`straightbackward`（和 forward/backward 重复）以及 `face_to`（以后由导航模块用视觉朝向实现）。
- PC 端新增 stop 插队通道：不等 Hub 就绪，直接发出。
- PC 端新增硬急停：软停止发出后 300 ms 内没有收到确认，就发送 Pybricks 的"停止程序"命令，并提供重新启动程序的操作。
- 新增持续运动看门狗：PC 在持续运动期间定时发送心跳，Hub 超过 1 秒没收到就自动停车。
- 执行结果里带上夹爪状态；Hub 回 `ERR` 的命令记为"被拒绝"。
- simulate 模式同步支持新协议。

## Capabilities

### New Capabilities

- 无

### Modified Capabilities

- `hub-communication`（由 `voice-quick-wins` 引入）：新增运动中可打断、stop 插队、结构化回复、未知命令报错、版本握手、看门狗、硬急停、技能与 Hub 能力一致。
- `command-execution`（由 `voice-streaming` 引入）：新增停止时打断正在执行的命令、结果带夹爪状态、Hub 错误的处理方式。

## Impact

- **Hub 程序**：`spike_hub/control_by_llm.py` 的主循环基本重写，需要用 Pybricks 重新下载到 Hub 上。
- **PC 代码**：`pc/spike_communication/spikehub.py`（解析回复、插队、急停、心跳、版本握手）、`pc/agent/robot_agent.py`、`pc/tools/move_tools.py`、`skills/move.skill.md`。
- **前置条件**：`voice-quick-wins` 和 `voice-streaming` 先完成（复用执行队列）。
- **兼容性**：旧的 Hub 程序和新的 PC 端不兼容，版本握手可以避免混用。
- **依赖**：不新增 Python 依赖；硬急停要求 Pybricks 固件支持"停止程序"和"启动程序"这两个蓝牙命令。
