# Spec Delta

## ADDED Requirements

### Requirement: 停止时打断正在执行的命令
触发停止时，除了取消队列中还没发出的命令，正在执行的命令也 SHALL 被打断，它的结果标为"被停止中断"。

#### Scenario: 执行中停止
- **WHEN** `forward 100` 正在执行，队列中还有 `left 90`，此时触发停止
- **THEN** `forward 100` 标为"被停止中断"，`left 90` 标为"被停止取消"

### Requirement: 结果带夹爪状态
每条已执行或被中断的命令，结果中 SHALL 带上 Hub 报告的两个夹爪角度；系统 SHALL 保存最新的夹爪状态，供其他模块读取。

#### Scenario: 抬起夹爪后读取状态
- **WHEN** `gripper_up` 执行完成
- **THEN** 结果中两个夹爪的角度都是 90，此时读取最新夹爪状态得到的也是"都抬起"

#### Scenario: 夹爪状态未知
- **WHEN** Hub 程序刚刚重新启动，还没有回报过任何夹爪角度
- **THEN** 读取最新夹爪状态得到"未知"

### Requirement: Hub 报错的命令记为被拒绝
Hub 对某条命令回复错误时，这条命令 SHALL 标为"被拒绝"，并附上 Hub 给出的原因；队列中后面的命令照常执行。

#### Scenario: Hub 拒绝一条命令
- **WHEN** 序列 `forward 30`、`dance`、`left 90` 中，Hub 对 `dance` 回复"未知命令"
- **THEN** `dance` 标为"被拒绝（Hub：未知命令）"，`forward 30` 和 `left 90` 照常执行
