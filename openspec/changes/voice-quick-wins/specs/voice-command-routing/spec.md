# Spec Delta

## Purpose

决定识别出的文字怎样变成机器人命令：能在本地准确识别的简单命令直接执行，其余交给 LLM；急停永远最优先；识别不了的不去猜。

## ADDED Requirements

### Requirement: 严格的整句匹配
本地匹配器 SHALL 只在一个分句整体符合已知句式时才判定为命中。分句里仅仅出现某个关键词（比如"左""前"）MUST NOT 算作命中。

#### Scenario: "夹子"说法
- **WHEN** 输入"左夹子抬起来"
- **THEN** 识别为 `gripper_left_up`，不会识别成左转

#### Scenario: 只有关键词但句式不符
- **WHEN** 输入"把红块搬到B区"
- **THEN** 本地匹配器判定为不命中

### Requirement: 支持中文数字和单位换算
本地匹配器 SHALL 同时支持阿拉伯数字和中文数字（例如 三十、九十、一百二十），并 SHALL 把以米为单位的距离换算成厘米。

#### Scenario: 中文数字距离
- **WHEN** 输入"向前走三十厘米"
- **THEN** 得到命令 `forward 30`

#### Scenario: 米换算
- **WHEN** 输入"前进1米"
- **THEN** 得到命令 `forward 100`

#### Scenario: 中文数字角度
- **WHEN** 输入"左转九十度"
- **THEN** 得到命令 `left 90`

### Requirement: 按顺序拆分分句
本地匹配器 SHALL 按连接词（然后、再、之后、接着、逗号、分号，以及"数值+单位"后面跟的"后"）把一句话拆成多个分句，并保持原来的顺序。

#### Scenario: 用"后"连接
- **WHEN** 输入"前进30厘米后左转90度"
- **THEN** 依次得到 `forward 30`、`left 90`

#### Scenario: 用"然后"连接
- **WHEN** 输入"往后退10厘米，然后把夹子抬起来"
- **THEN** 依次得到 `backward 10`、`gripper_up`

### Requirement: 所有分句都命中才走快速通道
在整句识别模式下（一句话说完才得到完整文字），当这句话的所有分句都被本地匹配器命中时，系统 SHALL 在本地生成命令并执行，不调用 LLM。只要有任意一个分句没有命中，系统 SHALL 把整句交给 LLM，本地 MUST NOT 执行这句话里的任何分句。

#### Scenario: 全部命中
- **WHEN** 输入"向前走50厘米，然后向右转90度"
- **THEN** 本地依次执行 `forward 50`、`right 90`，没有调用 LLM

#### Scenario: 部分命中
- **WHEN** 输入"前进30厘米然后把红块搬到B区"
- **THEN** 本地不执行任何动作，整句交给 LLM

### Requirement: 急停优先
当一句话只由停止词组成（停、停止、急停、stop，可以带语气词或重复）时，系统 SHALL 立即发送 `stop`，不调用 LLM。停止词作为多步命令中的一个普通分句出现时，SHALL 按顺序当作普通的 `stop` 步骤处理。

#### Scenario: 单独喊停
- **WHEN** 输入"停！停！"
- **THEN** 立即发送一次 `stop`，没有调用 LLM

#### Scenario: 序列中的停止
- **WHEN** 输入"前进30厘米然后停止"
- **THEN** 依次得到 `forward 30`、`stop`

### Requirement: 识别不了时不猜测
当本地匹配器没有命中，而且 LLM 不可用时，系统 SHALL 拒绝执行并提示"无法识别"，MUST NOT 根据关键词去猜测动作。

#### Scenario: 离线时的未知说法
- **WHEN** LLM 不可用，输入"左夹子转一转"
- **THEN** 不执行任何动作，并提示无法识别

### Requirement: 所有命令发送前都要校验
无论命令来自本地快速通道还是 LLM，系统在发送给 Hub 之前 SHALL 按技能规则校验动作名和参数范围，不合法的命令 MUST NOT 发送。

#### Scenario: LLM 给出超范围参数
- **WHEN** LLM 输出 `forward` 且距离为 20000 厘米
- **THEN** 这条命令被拒绝，不会发送给 Hub，结果中注明原因
