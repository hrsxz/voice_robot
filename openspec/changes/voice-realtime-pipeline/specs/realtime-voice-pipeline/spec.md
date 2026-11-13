# Spec Delta

## Purpose

提供第二种语音链路：把用户语音直接交给实时模型，由模型直接给出动作调用。这样中间少一个环节，可以和流水线链路用同样的指标对比延迟和准确率。

## ADDED Requirements

### Requirement: 语音直接产生动作调用
在 realtime 链路下，系统 SHALL 把用户语音直接送给实时模型，由模型以动作调用的形式给出命令；模型可以调用的动作和参数范围 SHALL 从技能定义自动生成。

#### Scenario: 简单命令
- **WHEN** 在 realtime 链路下按住空格说"向前走30厘米"
- **THEN** 模型给出前进 30 厘米的动作调用，系统执行 `forward 30`

### Requirement: 动作调用一到达就执行
每个动作调用一到达，系统 SHALL 立即校验，合法的放进执行队列，不等模型这一轮回复全部结束；不合法的调用被拒绝并记录原因。

#### Scenario: 多步命令
- **WHEN** 说"前进30厘米，然后左转90度"，模型依次给出两个动作调用
- **THEN** 第一个调用到达后，`forward 30` 立即入队执行，不等第二个调用

#### Scenario: 参数超范围
- **WHEN** 模型给出前进 20000 厘米的调用
- **THEN** 这个调用被拒绝，原因被记录，不会发送给 Hub

### Requirement: 按空格模式下松开即提交
在 realtime + 按空格模式下，系统 SHALL 只在按住空格期间上传音频，松开空格时提交这句话。

#### Scenario: 没按空格
- **WHEN** 程序运行中，用户没有按空格
- **THEN** 没有任何音频被上传

### Requirement: 唤醒词模式下的会话生命周期
在 realtime + 唤醒词模式下，系统 SHALL 只在本地检测到唤醒词之后才建立会话、开始上传音频；连续 20 秒没有人说话时，系统 SHALL 关闭会话并停止上传。

#### Scenario: 未唤醒
- **WHEN** 处于未唤醒状态
- **THEN** 没有建立会话，也没有音频被上传

#### Scenario: 空闲关闭
- **WHEN** 唤醒后连续 20 秒没有人说话
- **THEN** 会话关闭，停止上传音频

### Requirement: 停止不依赖模型决策
在 realtime 链路下，系统 SHALL 在转写文字或本地识别结果中检测停止词，一旦检测到就立即停止；系统 MUST NOT 只依赖模型去调用停止动作。

#### Scenario: 运动中喊停
- **WHEN** 小车正在运动，用户说"停"
- **THEN** 系统不等模型回复就立即停止

### Requirement: 会话失败时回退到 stream 链路
realtime 会话建立失败或中途断开时，系统 SHALL 提示原因，并在 stream 链路可用时自动切换过去，继续接收命令。

#### Scenario: 连接失败
- **WHEN** 以 realtime 链路启动，但会话无法建立
- **THEN** 终端提示失败原因，并切换到 stream 链路继续工作

### Requirement: 耗时记录与 stream 链路格式一致
realtime 链路 SHALL 产生和 stream 链路格式相同的耗时记录，路由标记为"realtime"，以便两条链路直接对比。

#### Scenario: 对比记录
- **WHEN** 分别用 stream 和 realtime 链路执行同一条命令
- **THEN** 两条耗时记录的字段相同，只是路由标记不同
