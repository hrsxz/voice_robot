# 设计：Hub 协议升级

## Context

- Hub 程序（`spike_hub/control_by_llm.py`）是一个单线程循环：发 `rdy` → 等 stdin 一行 → 执行（`robot.straight` / `turn` / `run_target` 都是阻塞的）→ 发 `OK`。
- PC（`SpikeHub`）通过 Pybricks 服务特征值收发：写入时前缀 `0x06`（写 stdin），接收时前缀 `0x01`（stdout）。现在只识别 `rdy`，其他内容直接打印。
- `voice-streaming` 之后，RobotAgent 有了执行队列，停止时会清空队列；`voice-quick-wins` 之后，就绪等待的超时按动作时长计算。
- 需求见 specs/，动机见 proposal.md。

## Goals / Non-Goals

**Goals:**
- 演示时任何时候说"停"都能可靠地停下来，而且有两层保险（软停止 + 硬急停），再加上 Hub 按钮这道物理保险。
- PC 能拿到真实的执行状态（开始、完成、被停止、出错、夹爪角度）。

**Non-Goals:**
- 动作执行中接受除 stop 和心跳以外的新命令（仍然一次只执行一条）。
- 基于 IMU 的绝对朝向转向（`face_to` 留给导航模块用视觉实现）。

## Decisions

### D1 Hub 主循环改成"空闲 / 忙碌"两种状态
- **空闲**：发 `rdy`，等待一行命令，解析后开始执行（`straight` / `turn` / `run_target` 都用 `wait=False`），回复 `ACK`，进入忙碌状态。
- **忙碌**：每 10 ms 检查一次：动作是否完成（`robot.done()` / `motor.done()`）、有没有收到新的一行。收到 `stop` → `robot.stop()`，回复 `STOPPED`；收到 `hb` → 只刷新心跳时间；收到其他命令 → 回复 `ERR busy`。动作完成 → 回复 `DONE`，回到空闲。
- 巡线本来就是循环，改成在忙碌状态里按步执行。
- 备选：Pybricks 的多任务（`multitask`、协程）。否决：现在这种轮询写法更直观，课堂上讲解也容易。

### D2 回复格式
每条回复占一行，以换行结束，后面照常跟 `rdy`：

```
ACK <cmd>
DONE <cmd> gl=<0-90> gr=<0-90>
STOPPED <cmd> gl=<0-90> gr=<0-90>
ERR <cmd> <reason>          reason: unknown | missing_value | bad_value | busy
VERSION 2
```

夹爪角度统一换算成"0 = 放下，90 = 抬起"：右夹爪的电机角度是负的，在 Hub 端取反后再上报。

### D3 版本握手
PC 连上后先发 `version`，2 秒内必须收到 `VERSION 2`。旧程序会对 `version` 回 `OK`，或者没有任何有效回复，这时 PC 提示"Hub 程序需要更新"，并且拒绝发送运动命令。

### D4 PC 端接收和发送
- 接收：把 `0x01` 的数据按行拼接（处理分包），解析成事件（ACK / DONE / STOPPED / ERR / VERSION / rdy），交给正在等待的那条命令。
- 发送：所有 GATT 写入都经过一把异步锁，保证不会两次写入交叠；普通命令照常等 `rdy`，`stop` 和 `hb` 不等 `rdy`，拿到锁就立刻写（写一次只要几十毫秒，排队时间可以忽略）。
- 一条命令的完成以收到 DONE / STOPPED / ERR 为准；就绪等待超时沿用 `voice-quick-wins` 的按时长计算。

### D5 硬急停
`stop` 发出后 300 ms 内没收到 `STOPPED`（或者空闲时的 `DONE stop`），就写入 Pybricks 命令 `0x00`（停止用户程序），电机随程序结束而停止。之后把夹爪状态设为"未知"，并提示用户：确认后可以发送 `0x01`（启动用户程序）重新运行。程序重启时会执行夹爪初始化（转到放下位置），所以必须先确认，不能自动重启。

### D6 看门狗
Hub 记录最后一次收到任何一行的时间。在持续运动模式（不带数值的 `dc`）下，超过 1000 ms 没收到就停车，并回复 `STOPPED <cmd> watchdog`。PC 在持续运动期间每 300 ms 发一次 `hb`。带数值的动作本身有终点，不受看门狗影响。

### D7 技能和 Hub 能力对齐
- Hub 新增实现 `gripper_pos <角度>`（两个夹爪一起转到指定角度）。
- 从 `skills/move.skill.md` 和 `move_tools` 中删除 `straightforward`、`straightbackward`、`face_to`。LLM 的提示词是从技能自动生成的，会跟着一起变。
- 新增一个一致性测试：simulate Hub 维护一份支持的动作列表，测试用它和技能声明对比，同时检查 Hub 程序源码里每个动作都有对应的处理分支。

### D8 simulate 模式
simulate Hub 实现完整的 v2 协议：按 `voice-quick-wins` 的预计时长模拟执行时间，支持 stop 打断、ERR、版本查询，而且可以设置成"无响应"，用来测试硬急停。

## Risks / Trade-offs

- [Pybricks 的 stdin 缓冲区有限] → 忙碌期间 PC 只发 stop 和 hb，都只有几个字节。
- [BLE 延迟偶尔变大，导致软停止超过 300 ms 而误触发硬急停] → 硬急停之后需要确认才重启，最坏情况只是多一次重启；阈值做成可配置项。
- [硬急停后重启，夹爪初始化会突然动一下] → 重启前提示并要求确认。
- [PC 事件循环卡住导致心跳中断，看门狗误停] → 1 秒的余量足够；心跳由独立的异步任务发送。
- [BREAKING：删除了三个技能动作] → 这几个动作 Hub 本来就没实现，现在只是让声明和事实一致。

## Migration Plan

1. 先更新 PC 端（带版本握手），再用 Pybricks 把新的 Hub 程序下载到 Hub 上。
2. 版本握手可以防止新 PC 端配旧 Hub 程序的情况。
3. 回滚：把旧版 Hub 程序重新下载到 Hub，并 git revert PC 端改动。

## Open Questions

- 这台 Hub 上的 Pybricks 固件版本是否支持 `0x01`（启动用户程序）：在任务 2.4 里确认。如果不支持，硬急停后改为提示用户按 Hub 按钮重新启动，方案不变。
