# Spec Delta

## ADDED Requirements

### Requirement: 运动中可以被停止打断
Hub SHALL 在执行直行、转向、巡线和夹爪动作期间持续检查新收到的命令；收到 `stop` 时 SHALL 立即停止当前动作。从 PC 发出 `stop` 到电机停止，目标不超过 0.3 秒。

#### Scenario: 直行中停止
- **WHEN** Hub 正在执行 `forward 100`，PC 发出 `stop`
- **THEN** 小车在 0.3 秒内停下，Hub 回复这个动作已被停止

#### Scenario: 巡线中停止
- **WHEN** Hub 正在执行 `line_follow_left 80`，PC 发出 `stop`
- **THEN** 小车在 0.3 秒内停下

### Requirement: stop 不等待就绪
PC 发送 `stop` 时 SHALL 立即发出，不等待 Hub 的就绪信号。

#### Scenario: Hub 正忙
- **WHEN** Hub 正在执行一个需要 3 秒的动作，系统触发停止
- **THEN** `stop` 立即发出，不会等到这个动作结束

### Requirement: 结构化回复
Hub SHALL 对每条命令给出结构化回复：开始执行时回复开始确认；结束时回复"完成"或"被停止"，并带上两个夹爪的当前角度（0 表示放下，90 表示抬起）。

#### Scenario: 正常完成
- **WHEN** PC 发送 `forward 30` 且动作正常完成
- **THEN** Hub 先回复 `forward 30` 的开始确认，结束时回复完成，并带上两个夹爪的角度

#### Scenario: 被停止
- **WHEN** `forward 100` 执行中被 `stop` 打断
- **THEN** Hub 回复 `forward 100` 被停止，并带上两个夹爪的角度

### Requirement: 未知命令和非法参数回复错误
Hub 对未知命令、缺少参数或参数非法的命令 MUST NOT 回复成功，SHALL 回复错误并说明原因。

#### Scenario: 未知命令
- **WHEN** PC 发送 Hub 不认识的命令 `dance`
- **THEN** Hub 回复错误，原因是"未知命令"

#### Scenario: 缺少参数
- **WHEN** PC 发送 `gripper_left_pos`，但没带角度
- **THEN** Hub 回复错误，原因是"缺少参数"

### Requirement: 技能声明和 Hub 能力一致
技能文件中声明的每个动作，Hub 程序 SHALL 都能执行。

#### Scenario: 遍历所有技能动作
- **WHEN** 把技能文件里声明的每个动作都用合法参数发送给 Hub
- **THEN** 没有一个动作收到"未知命令"错误

### Requirement: 连接时进行版本握手
PC 连上 Hub 后 SHALL 先查询 Hub 程序的协议版本；版本不符或查询无响应时，PC SHALL 明确提示需要更新 Hub 程序，并 MUST NOT 发送任何运动命令。

#### Scenario: Hub 运行的是旧程序
- **WHEN** Hub 上运行的是不支持版本查询的旧程序
- **THEN** PC 提示"Hub 程序需要更新"，不发送任何运动命令

### Requirement: 持续运动看门狗
在不带数值的持续运动期间，PC SHALL 定时发送心跳；Hub 超过 1 秒没有收到 PC 的任何消息时 SHALL 自动停车。

#### Scenario: 持续前进时蓝牙断开
- **WHEN** 小车正在执行不带数值的 `forward`，蓝牙连接突然断开
- **THEN** 小车在 1 秒内自动停下

### Requirement: 硬急停兜底
软停止发出后 300 ms 内没有收到 Hub 的停止确认时，PC SHALL 发送 Pybricks 的"停止程序"命令让电机停止，然后 SHALL 提示 Hub 程序已停止，并提供重新启动程序的操作（需要用户确认）。

#### Scenario: Hub 程序没有响应
- **WHEN** PC 发出 `stop` 后 300 ms 内没有收到停止确认
- **THEN** PC 发送停止程序命令，电机停止，终端提示可以确认后重新启动 Hub 程序
