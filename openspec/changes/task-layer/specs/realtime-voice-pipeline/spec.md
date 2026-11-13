# Spec Delta

## ADDED Requirements

### Requirement: 目标级请求交给任务层
在 realtime 链路下，实时模型 SHALL 能通过一个专门的工具，把目标级请求（涉及物体、区域、圆圈或 WRO 任务）交给任务层；直接的移动动作 SHALL 继续通过原有的动作工具执行。

#### Scenario: realtime 下的目标请求
- **WHEN** 在 realtime 链路下说"完成任务2"
- **THEN** 模型调用交接工具，任务层开始把蓝色大人送到 B 区
