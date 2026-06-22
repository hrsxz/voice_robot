输入采集：实现统一接口支持 CLI / 文件 / 麦克风（STT），并做基础规范化（去噪、语言识别、转小写、去标点等）。
提示与上下文构建：在调用 LLM 前构建 prompt（系统提示、技能示例、当前状态），可放到 llm/prompts.py。
LLM 调用与回退：实现超时、重试、模型选择与降级（本地 Ollama → 远程 OpenAI → 离线调试回退）。
结构化输出约定：规定 LLM 最好返回 JSON（如 {"action":"forward","distance_cm":10,"duration_s":1.2}），并把规范写成小文档。
意图解析与校验：parse_intent 需要做字段校验、范围限制、参数默认值与单位转换（米→cm 等）。
权限与安全策略：白名单动作、最大速度/距离/角度上限、紧急停止入口（物理按键或 stop 命令）。
意图到序列：intent_mapper 将意图转成序列项（每项含 cmd 和可选 duration_s/params），并可把复杂动作拆成多条基础命令。
调度与执行器：控制器按序列执行：发送命令（await spike.send(cmd)），若项含 duration_s 则 await asyncio.sleep(duration_s) 后发送 stop；处理并发/中断（新指令到来时如何中止当前动作）。
Hub 状态/反馈处理：处理 Hub 的异步通知（ready、错误信息、传感器值），并据此更新控制流或重试。
错误处理与重连策略：BLE 写入失败/断开要重试、退到模拟模式或提示用户。
日志与可观测性：记录 prompt、LLM 输出、解析结果、下发命令和 Hub 返回，用于调试与回放。
模拟与单元测试：--simulate 模式、模拟 SpikeHub 和 move_tools，写自动化脚本覆盖常见指令序列。
配置管理：集中配置速度估算、默认单位、超时、模型参数与 API 密钥（.env 或 config 文件）。
用户确认/可选反馈：对风险动作（大角度、大距离）可要求确认；可选添加语音或文本确认反馈（TTS/console）。
部署与运行脚本：提供 pc/controller.py 启动参数（--simulate、--model、--demo）和 README 运行说明。