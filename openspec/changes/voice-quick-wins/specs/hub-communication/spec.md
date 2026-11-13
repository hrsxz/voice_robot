# Spec Delta

## Purpose

规定 PC 和 Spike Hub 之间怎样可靠地收发命令：命令按顺序送达，长动作不会被误判为超时，通信出错时不会继续盲目发送。

## ADDED Requirements

### Requirement: 等 Hub 就绪后再按顺序发送
PC SHALL 在 Hub 报告就绪之后才发送下一条普通命令，保证命令按顺序逐条执行。

#### Scenario: 两步序列
- **WHEN** 执行序列 `forward 30`、`left 90`
- **THEN** `left 90` 在 Hub 完成 `forward 30` 并报告就绪之后才发送

### Requirement: 就绪等待超时按动作时长计算
等待 Hub 就绪的超时时间 SHALL 按上一条命令的预计执行时长加上固定余量来计算，MUST NOT 使用与动作无关的固定短超时。

#### Scenario: 长距离前进后接转向
- **WHEN** 执行序列 `forward 100`、`left 90`，且前进需要 3 秒以上
- **THEN** `left 90` 正常发送，不报超时

#### Scenario: Hub 真的没有响应
- **WHEN** Hub 在预计时长加余量的时间内一直没有报告就绪
- **THEN** 系统报告超时错误，并指出是在等待哪条命令完成

### Requirement: 通信失败后停止发送剩余命令
同一个序列中，一旦某条命令因为 Hub 通信失败（超时、断开、写入失败）而没能执行，系统 SHALL 跳过这个序列里剩下的命令，并在结果中逐条标明被跳过的原因。

#### Scenario: 中途超时
- **WHEN** 序列 `forward 30`、`left 90`、`forward 20` 在等待 `forward 30` 完成时超时
- **THEN** `left 90` 和 `forward 20` 都不发送，结果中标为"因通信失败跳过"
