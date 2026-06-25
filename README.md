## Plan: RobotAgent 从零搭建方案

目标是把执行职责完全迁入 RobotAgent：主程序只做输入采集、LLM 调用、意图解析与映射，RobotAgent 成为唯一执行入口。你已明确不保留旧兼容链路，因此本方案默认移除 handle_user_text。

**Steps**
1. Phase A：定义单链路边界  
主链路固定为 输入 -> LLM -> intent_parser -> intent_mapper -> RobotAgent.execute_sequence -> hub，下游执行只允许 RobotAgent 入口。
2. 在 robot_agent.py 增加 execute_sequence(seq_spec) 作为唯一执行入口。  
输入契约直接消费 intent_mapper 产出的 sequence。
3. 在 robot_agent.py 定义统一执行结果结构。  
至少包含 status、executed、skipped、errors，便于主程序打印与后续日志。
4. Phase B：在 Agent 层统一动作词典与参数规则  
在 robot_agent.py 维护 action registry，集中定义 action 名、hub 方法名、参数键、参数范围、是否必填。
5. 在 robot_agent.py 增加命令归一化与校验流程。  
对每个 sequence item 做 cmd 解析、action 白名单校验、参数类型和范围校验。
6. 在 robot_agent.py 增加统一分发执行器。  
通过动作词典调用 hub 对应方法；非法动作或参数不抛崩溃，记录 skipped/errors。
7. Phase C：主程序接入 Agent  
在 voice_to_command.py 初始化 RobotAgent(hub, llm_client)。
8. 在 voice_to_command.py 的 run_once 中，用 robot_agent.execute_sequence(seq) 替换本地执行。
9. 在 voice_to_command.py 删除当前本地 execute_sequence 方法。
10. Phase D：删除旧链路并统一 skills 方向  
在 robot_agent.py 删除 handle_user_text 分支及 parse_skill/generate 兼容路径，避免双入口长期并存。
11. 修正 skills runtime 路径，和当前项目结构对齐。  
把 move.skill.md、camera.skill.md、sensor.skill.md 的 runtime 从 tools.xxx 调整为 pc.tools.xxx。
12. 补齐工具层统一入口规范。  
move_tools.py 补齐 execute(args)；camera_tools.py 与 sensor_tool.py 当前为空，按同样契约实现。
13. Phase E：验证与回归  
先在 simulate 模式验证流程和错误路径，再接真实 hub 验证动作安全边界。

**Relevant files**
- robot_agent.py — 新增统一执行入口、动作词典、参数规则、分发与错误回传；移除旧 handle_user_text。
- voice_to_command.py — 删除本地执行函数，改为委托 RobotAgent。
- intent_mapper.py — 保持输出契约稳定，确保 sequence 格式一致。
- move.skill.md — runtime 路径修正。
- camera.skill.md — runtime 路径修正。
- sensor.skill.md — runtime 路径修正。
- move_tools.py — 补齐 execute(args) 统一入口。
- camera_tools.py — 从空文件补齐工具实现。
- sensor_tool.py — 从空文件补齐工具实现。

**Verification**
1. CLI 单步动作：确认由 RobotAgent.execute_sequence 执行，主程序不再直接 hub.send。
2. 多步动作：确认按序执行，executed/skipped 统计正确。
3. 非法动作与越界参数：确认被拦截并记录，不导致主程序中断。
4. simulate 模式全链路：确认无硬件下可跑通。
5. 真实 hub 冒烟：forward、left、right、stop 与既有行为一致。

**Decisions**
- Included：RobotAgent 成为唯一执行入口。
- Included：动作词典和参数规则统一在 Agent 层。
- Included：删除 handle_user_text 旧链路，从单链路重新搭建。
- Excluded：本轮不重做 parser/mapper 算法，只保证契约稳定。

**Further Considerations**
1. 接入节奏建议  
Option A：先只接 move 动作打通主链路，再接 camera/sensor。  
Option B：三类技能一次接齐，但要先实现两个空工具文件。
2. 参数策略建议  
Option A：越界即拒绝并 skip。  
Option B：自动裁剪到边界后执行，并记录裁剪日志。
3. 失败策略建议  
Option A：单步失败继续后续步骤。  
Option B：任一步失败即停止序列，适合高风险动作。