# Spec Delta

## ADDED Requirements

### Requirement: 目标级请求交给任务层
当请求涉及场地上的物体、区域、圆圈或 WRO 任务，而不是直接的移动动作时，系统 SHALL 把它作为一个目标交给任务层，而不是自己生成一串底层动作；直接的移动动作 SHALL 继续走原来的路径。

#### Scenario: 目标级请求
- **WHEN** 用户说"把黄色小人送回家"
- **THEN** 这句话作为目标交给任务层处理

#### Scenario: 直接动作
- **WHEN** 用户说"前进30厘米"
- **THEN** 仍由快速通道直接执行，不经过任务层
