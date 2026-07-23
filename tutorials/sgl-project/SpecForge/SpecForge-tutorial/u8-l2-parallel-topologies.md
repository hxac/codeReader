# 并行拓扑 USP 与 ring attention

## 1. 本讲目标

上一讲（u8-l1）我们讲清了「分布式初始化怎么把进程组、设备网格建起来」。本讲接着回答：**建好的这些进程组，在训练时到底怎么用？** 也就是说，当一条训练样本比单卡显存能装下的更长、或我们想用更多卡同时算一条序列时，SpecForge 用哪种并行拓扑、又是怎么实现的。

读完本讲，你应该能够：

- 说清 SpecForge 在 online / offline / EAGLE3 USP 三种场景下分别支持哪种并行组合，以及为什么 online 不允许 SP。
- 理解 **USP = Ulysses（头维并行）× Ring（序列维并行）** 这条复合序列并行的分组方式与「SP peer 共享一条序列」的语义。
- 读懂 `layers/ring/` 里 ring attention 的核心实现：环形 K/V 旋转 + online softmax 分块合并。
- 为一台 8 卡机器设计一份合法的 EAGLE3 离线 USP 配置，并解释每条约束的来源。

## 2. 前置知识

本讲默认你已经掌握 u8-l1 的术语：进程组（process group）、设备网格（device mesh）、TP/DP/SP、Ulysses 与 Ring 两个 SP 子组、`init_distributed`。下面只补三个本讲要用到的新概念。

- **注意力头维度（head dimension）与序列维度（sequence dimension）。** 一份注意力输入的张量形状通常是 `[batch, seq_len, num_heads, head_dim]`。我们可以沿 `num_heads` 这条轴切（每张卡算一部分头），也可以沿 `seq_len` 这条轴切（每张卡算一段 token）。**Ulysses 切头，Ring 切序列**——这是本讲的核心口诀。
- **Flash Attention 与 log-sum-exp（lse）。** Flash Attention 把 softmax 拆成分块计算，每块只保留输出 `out` 和一个标量 `lse = log(Σ exp(score))`。多块的 softmax 可以用 `out` 与 `lse` 做**数学上完全等价**的在线合并——这正是 ring attention 能把「每卡算一段」拼回「全序列注意力」的关键。
- **通信原语。** `all-to-all`（每张卡同时给其它卡发不同数据、也收不同数据）和**点对点 send/recv**（两张卡之间直接传一个张量）是本讲看到的两种通信。Ulysses 用 all-to-all，ring attention 用点对点的环形传递。

一句话直觉：**DP 是「多卡算多条不同的序列」，SP 是「多卡合力算同一条长序列」**。USP 就是 SpecForge 选用的 SP 方案。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `specforge/config/schema.py` | 定义 SP 相关配置字段，并在 `validate_world_size` 等处校验拓扑约束（哪些组合合法）。 |
| `specforge/distributed.py` | `init_distributed` 建出 Ulysses/Ring 两个 SP 子组并提供访问器；本讲引用其 SP 相关部分。 |
| `specforge/data/preprocessing.py` | `process_data_usp`：USP 模式下在数据加载阶段就把序列切成 SP 分片并算好 position ids。 |
| `specforge/modeling/draft/llama3_eagle.py` | `LlamaUSPFlashAttention`：USP 注意力的实际落点，把 Ulysses all-to-all 与 ring attention 拼起来。 |
| `specforge/layers/ring/ring_flash_attn.py` | ring attention 的前向/反向实现（环形旋转 K/V + 分块合并）。 |
| `specforge/layers/ring/utils.py` | `update_out_and_lse`（分块 softmax 合并）与 `RingComm`（环形点对点通信）。 |
| `specforge/core/eagle3_adapters.py` | `UspAdapter`：在线 EAGLE3 模型视角下的 SP 视图与跨卡归约（理解「SP 对 metric/loss 的影响」）。 |
| `specforge/training/assembly.py` | USP 下把 `accumulation_steps` 乘以 `sp_size`，保持优化器窗口语义不变。 |
| `docs/basic_usage/training.md` | 「Parallel topologies」一节是约束的权威文字说明。 |

## 4. 核心概念与源码讲解

### 4.1 并行拓扑总览与约束表

#### 4.1.1 概念说明

SpecForge 的训练并行度由 **两条正交的轴线** 决定，本讲关心的是第二条：

1. **目标模型的并行** 属于外部的 SGLang 推理服务，不归 trainer 管。trainer 这边 `training.tp_size` 始终为 1（离线时 schema 强制）。
2. **草稿模型的并行** 才是 trainer 自己的事：在线/离线 disaggregated 用 **纯 DP**（每张 trainer 卡吃一条不相交的特征流），只有 EAGLE3 离线 colocated 可以额外开 **USP 序列并行**。

也就是说，「谁和谁算同一条序列」是受严格约束的：online 和非 EAGLE3 的离线都不允许 SP。

#### 4.1.2 核心流程

把支持矩阵浓缩成一张表（与 `docs/basic_usage/training.md` 的「Supported combinations」「Parallel topologies」一致）：

| 场景 | tp_size | SP | 说明 |
| --- | --- | --- | --- |
| 在线 disaggregated consumer | 1 | 1 | 每个 trainer rank 都是一个 DP rank，吃不相交特征流；目标 TP 在外部 SGLang 上配。 |
| 离线（非 EAGLE3）consumer | 1 | 1 | 每个 trainer rank 吃不相交的离线特征引用分片，作 DP。 |
| EAGLE3 离线 USP | 1 | `sp_ulysses × sp_ring > 1` | 多卡合力算同一条长序列；强制 `batch_size=1`。 |

这些「不允许」的约束不是软提示，而是在**配置校验阶段 fail-fast**。关键是三处校验：

- USP 只允许离线：[specforge/config/schema.py:834-836](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L834-L836) 说明 `attention_backend == "usp"` 时若 `mode != "offline"` 直接报错。
- 离线不允许 trainer TP：[specforge/config/schema.py:837-842](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L837-L842) 说明离线特征 consumer 不实现 trainer 张量并行，强制 `tp_size=1`，让每个非 SP rank 各拿一份数据分片。
- 在线 disaggregated consumer 把 TP/SP 全锁成 1：[specforge/config/schema.py:843-856](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L843-L856) 说明在线 consumer 把每个 trainer rank 都用于 DP，目标 TP 必须配在外部 server 上。

而 USP 自身的字段约束在另一处：[specforge/config/schema.py:560-574](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L560-L574) 说明：选了 `usp` 就必须 `batch_size=1`、且 `sp_ulysses_size * sp_ring_size > 1`；反过来，若设了 SP size 却没选 `usp`，也报错。这意味着「SP size」与「attention_backend=usp」是绑定的，不能单设其一。

#### 4.1.3 源码精读：world size 整除约束

整除关系是本讲最重要的硬约束。它由 `init_distributed` 的断言与 schema 的 `validate_world_size` **双重把关**：

[specforge/distributed.py:166-170](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L166-L170) 说明在 `init_distributed` 内部断言 world size 必须能被 SP 总规模整除，并据此推导 `draft_dp_size = world_size // (sp_ulysses_size * sp_ring_size)`。

[specforge/config/schema.py:863-878](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L863-L878) 说明 `validate_world_size` 在装配前再校验一次：world size 必须同时被 `tp_size` 和 `sp_ulysses_size * sp_ring_size` 整除。这把「非法卡数」拦在拉起进程之前。

合起来，一张 8 卡机器开 USP 时，`sp_ulysses × sp_ring` 只能取 2/4/8（必须整除 8 且大于 1），对应的 `draft_dp_size` 则是 4/2/1。

#### 4.1.4 代码实践

1. **实践目标。** 体会「哪些组合会被 schema 当场拒绝」。
2. **操作步骤。** 复制离线样例 `examples/configs/qwen3-8b-eagle3-offline.yaml`，把它改成 `deployment.mode: disaggregated`（即 online），同时保留 `training.attention_backend: usp`，然后跑 `specforge train --plan -c 你的副本.yaml`。
3. **需要观察的现象。** 命令应当**立刻报错退出**，错误信息形如 `USP attention currently requires offline features`。
4. **预期结果。** 因为 USP 与 online 在 [schema.py:834-836](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L834-L836) 处互斥。`--plan` 不会占 GPU，适合反复试错。若本地未装 SpecForge，此为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1。** 一份 online disaggregated 配置里把 `training.sp_ring_size=2`，会发生什么？

> **答案。** 被 [schema.py:843-856](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L843-L856) 拒绝：在线 consumer 必须把 `tp_size` 与两个 SP size 全锁成 1。

**练习 2。** 为什么 `tp_size` 和 SP size 是两条独立的整除约束，而不是合并成一条？

> **答案。** TP 切的是草稿模型权重（这里离线被锁成 1），SP 切的是同一条序列。它们对应**不同的设备网格维度**（u8-l1 里主网格 `(dp, tp)` 与草稿网格 `(draft_dp, sp)` 是分开建的），所以要分别校验整除，才能各自正确切出进程组。

---

### 4.2 USP 分组：Ulysses × Ring

#### 4.2.1 概念说明

**USP（Ulysses–Ring 序列并行）** 把一个大小为 `sp_size = sp_ulysses_size × sp_ring_size` 的 SP 组拆成两个正交的子组：

- **Ulysses 子组**（`sp_ulysses_size` 个 rank）：沿**注意力头维度**并行。靠一次 `all-to-all` 在头维与序列维之间做转置，让每个 rank 用「完整的序列、一部分头」跑一次 flash attention 内核。
- **Ring 子组**（`sp_ring_size` 个 rank）：沿**序列维度**并行。每个 rank 只持有一段序列的 K/V，通过**环形点对点通信**把 K/V 在 ring 内轮转，使每段的 Q 都能注意到全序列。

合起来，`sp_size` 个 rank 协作算完**同一条逻辑长序列**的注意力，而没有任何一张卡需要把整条序列装进显存。这就是「SP peer 共享一条序列」：**一个 SP 组内的所有 rank 处理的是同一条序列的不同部分；不同的 SP 组（draft-DP 组）才处理不同的、互不相交的序列**。

#### 4.2.2 核心流程

USP 在 SpecForge 里的端到端流程是：

1. **建组。** `init_distributed` 调用 yunchang 的 `set_seq_parallel_pg(sp_ulysses_size, sp_ring_size, rank, world_size)` 建出 `ULYSSES_PG` 与 `RING_PG` 两个子组，存进模块级全局。
2. **数据分片。** 数据加载阶段，每条序列被切成 `sp_size` 段，每个 SP rank 取一段，并加上 `ttt_length` 的重叠，同时算好 position ids。
3. **前向。** 注意力层里先做 Ulysses all-to-all，再做 ring attention（若 `sp_ring_size > 1`），最后再做一次 Ulysses all-to-all 还原。
4. **梯度。** 反向时 Ulysses 的 all-to-all 与 ring 的 K/V 轮转都带自动求导，梯度尺度按 `sp_world_size` 缩放，使 SP 梯度与 DP 梯度尺度等价。

建组这一步在 u8-l1 已讲过，这里只贴关键一行确认两个子组的来源：[specforge/distributed.py:176](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L176) 说明 `set_seq_parallel_pg` 按两个 SP 维度注册序列并行进程组，随后 [specforge/distributed.py:182-183](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L182-L183) 说明从 yunchang 的 `PROCESS_GROUP` 取出 `ULYSSES_PG` 与 `RING_PG` 两个句柄。

#### 4.2.3 源码精读：数据分片与 position ids

「共享一条序列」的物理基础是数据加载阶段就把序列切了。看 [specforge/data/preprocessing.py:445-449](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L445-L449)：`chunk_size = ceil(global_len / sp_size)`，每个 SP rank 取 `start = sp_rank * chunk_size` 开始、长度为 `chunk_size + ttt_length` 的一段。即整条序列被 SP 组的 `sp_size` 个 rank 瓜分，每段再多取 `ttt_length` 个 token 作为重叠（训练时测试 TTT 的步进区，见 u1-l4）。

position ids 必须对齐 Ulysses all-to-all 后的序列布局，[specforge/data/preprocessing.py:486-496](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L486-L496) 给出推导：在一个 ring 组内有 `sp_ulysses_size` 个 Ulysses peer，每个持有一段 `usp_chunk_size` 切片，所以 position id 按 `ulysses_rank` 偏移；不同 ring 组之间按 `ring_chunk = usp_chunk_size * sp_ulysses_size` 错开。这正是「同一 ring 组内的 Ulysses peer 共享同一段大区间、彼此相邻」的体现。

#### 4.2.4 源码精读：Ulysses all-to-all

Ulysses 在注意力层的落点是 `LlamaUSPFlashAttention`。先看它在构造期就取好的两个子组与度数：[specforge/modeling/draft/llama3_eagle.py:1352-1359](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1352-L1359) 说明它把 `ring_pg`、`ulysses_pg` 及两者的度数存为成员，并约定 `scatter_idx=2`（头维）、`gather_idx=1`（序列维）。

前向里对 Q/K/V 各做一次 Ulysses all-to-all，以 Q 为例：[specforge/modeling/draft/llama3_eagle.py:1380-1388](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1380-L1388) 说明 `SeqAllToAll4D`（来自 yunchang）在 `ulysses_pg` 上、沿头维（dim=2）切分、沿序列维（dim=1）拼接，做一次 all-to-all 转置。这就是「Ulysses 切头」的代码证据——`scatter_idx=2` 明确作用在 `num_heads` 维。

紧接着模型需要恢复「全局逻辑长度」来正确施加 RoPE：[specforge/modeling/draft/llama3_eagle.py:1414-1415](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1414-L1415) 说明 `global_q_len = q_len * sp_ring_degree * sp_ulysses_degree` 把本卡的局部长度还原成全局长度，供 rotary embedding 使用。

注意力算完后，再用一次反向的 Ulysses all-to-all 把结果还原回原来的头/序列布局：[specforge/modeling/draft/llama3_eagle.py:1466-1470](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1466-L1470) 说明输出沿 `gather_idx=1`、`scatter_idx=2` 反向 all-to-all，与进入时的转置互逆。

> 注意：这一节我们只说「Ulysses 沿头维 all-to-all 转置」，刻意不展开 all-to-all 内部逐卡的张量形状换算——那依赖 yunchang 的具体实现与输入已分片的假设。把握住「scatter 作用在头维、两次 all-to-all 把注意力夹在中间」即可。

#### 4.2.5 源码精读：何时走 ring attention

`sp_ring_size` 可以为 1，此时只有 Ulysses、没有 ring。代码用一个分支区分：[specforge/modeling/draft/llama3_eagle.py:1440-1461](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1440-L1461) 说明当 `sp_ring_degree > 1` 时走 `_USPRingFlashCachedMergeFunc`（含 ring attention），否则走普通 flash。也就是说 `sp_ring_size=1` 给的是「纯 Ulysses」，`sp_ring_size>1` 才引入环形序列并行。

在线 EAGLE3 模型侧另有一个 `UspAdapter`（虽然 schema 当前把在线 USP 拒之门外，但它展示了「SP 对训练统计量的影响」这一通用问题）。看它的 `step_view`：[specforge/core/eagle3_adapters.py:126-134](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L126-L134) 说明在线视角下每个 rank 只看自己那段 `usp_chunk_size = seq_length - ttt_length` 的目标概率；而 `reduce_metrics`：[specforge/core/eagle3_adapters.py:147-156](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L147-L156) 说明正确率/分母这类跨序列统计必须在 `sp_group` 上 all-reduce 求和，才能还原成全序列指标。这印证了一个通用规则：**SP 模式下任何「按 token 聚合」的量都得在 SP 组内归约**。

#### 4.2.6 源码精读：accumulation_steps 的放大

USP 把一条逻辑序列摊到了 `sp_size` 张卡上，于是「一个用户语义的累积单元」对应的本地 micro-batch 数也要放大 `sp_size` 倍，否则优化器窗口的语义会变。[specforge/training/assembly.py:519-524](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/assembly.py#L519-L524) 说明当 `attention_backend == "usp"` 时，`accumulation_steps = t.accumulation_steps * sp_ulysses_size * sp_ring_size`。这条注释写得很清楚：「一个用户累积单元代表一条完整的逻辑序列，而不是一条本地序列分片」。这是 USP 与 u6 梯度累积/边界语义（u6-l3、u6-l4）的交汇点。

#### 4.2.7 代码实践

1. **实践目标。** 在纸面上把一条长序列在 4 卡 USP（`sp_ulysses_size=2, sp_ring_size=2`）下的分配画出来。
2. **操作步骤。** 假设 `global_len=1024`、`ttt_length=7`。按 [preprocessing.py:445-449](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L445-L449) 计算：`sp_size=4`、`chunk_size=256`、每个 SP rank 取 `256+7=263` 个 token。
3. **需要观察的现象。** rank 0 取 token `[0:263]`、rank 1 取 `[256:519]`、rank 2 取 `[512:775]`、rank 3 取 `[768:1024]`（末段不足则 padding）。相邻 rank 之间有 7 个 token 的重叠区。
4. **预期结果。** 四张卡合起来覆盖了整条 1024 序列，没有任何一张卡独占全部；这正是「共享一条序列」。

#### 4.2.8 小练习与答案

**练习 1。** `sp_ulysses_size=1, sp_ring_size=4` 与 `sp_ulysses_size=4, sp_ring_size=1` 有何不同？

> **答案。** 前者是「纯 ring」（无 Ulysses all-to-all，全靠环形 K/V 旋转覆盖序列）；后者是「纯 Ulysses」（`sp_ring_degree=1`，代码在 [llama3_eagle.py:1440](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1440) 处走普通 flash 分支）。两者 `sp_size` 都是 4，整除约束相同，但通信模式不同：Ulysses 是 all-to-all，ring 是点对点轮转。

**练习 2。** 为什么 USP 下正确率指标必须额外做 SP 组 all-reduce？

> **答案。** 每张 SP 卡只持有并预测自己那段 `usp_chunk_size` 的 token（见 [eagle3_adapters.py:126-134](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L126-L134)），单卡统计只是「全序列的一段」。必须在 `sp_group` 上把命中数与分母求和（[eagle3_adapters.py:147-156](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/core/eagle3_adapters.py#L147-L156)）才能得到全序列正确率。

---

### 4.3 ring attention 的实现

#### 4.3.1 概念说明

ring attention 解决的问题是：序列沿 ring 维被切成 `sp_ring_size` 段，每段只在本地持有自己的 K/V，但注意力要求每个 Q 都能看到**全序列**的 K/V。做法是把 K/V 沿一个**逻辑环**轮流传递——rank `i` 把自己的 K/V 发给 rank `i+1`、从 rank `i-1` 收一份——经过 `sp_ring_size` 步后，每段 Q 都依次和所有段的 K/V 算过一次分块注意力，再用 online softmax 把这些分块结果**数学等价地**合并成全序列注意力。

#### 4.3.2 核心流程：online softmax 分块合并

设某段 Q 与第 `j` 段 K/V 的分块注意力给出 `out_j` 与 `lse_j`，其中 `lse_j = log Σ_k exp(score_{jk})`。两块合并的在线 softmax 公式为：

\[
\text{lse}_{\text{new}} = \log\!\big(e^{\text{lse}_a} + e^{\text{lse}_b}\big)
\]

\[
\text{out}_{\text{new}} = \frac{e^{\text{lse}_a}\,\text{out}_a + e^{\text{lse}_b}\,\text{out}_b}{e^{\text{lse}_a} + e^{\text{lse}_b}}
\]

为避免大指数溢出，工程上改用 sigmoid 形式（与朴素指数形式**代数等价**）：

\[
\text{out}_{\text{new}} = \text{out}_a - \sigma(\text{lse}_b - \text{lse}_a)\,(\text{out}_a - \text{out}_b)
\]

\[
\text{lse}_{\text{new}} = \text{lse}_a - \log\sigma(\text{lse}_a - \text{lse}_b)
\]

其中 \(\sigma\) 是 sigmoid。这样每来一段 K/V，只需一次「合并」即可把累积结果扩展，全程不物化 \([Q, K_{\text{全序列}]\) 的大矩阵。

#### 4.3.3 源码精读：分块合并

合并公式落在 `update_out_and_lse`，其内核是 TorchScript 编译的 `_update_out_and_lse`：[specforge/layers/ring/utils.py:10-28](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/utils.py#L10-L28) 正是上面两个 sigmoid 形式公式的实现（`out = out - sigmoid(block_lse - lse)*(out - block_out)`、`lse = lse - logsigmoid(lse - block_lse)`）。首块直接初始化 `out/lse`，见 [specforge/layers/ring/utils.py:38-51](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/utils.py#L38-L51)。

#### 4.3.4 源码精读：环形通信 RingComm

环形点对点通信由 `RingComm` 封装：[specforge/layers/ring/utils.py:84-89](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/utils.py#L84-L89) 说明每个 rank 的「发送目标是 `(rank+1) % world_size`、接收来源是 `(rank-1) % world_size`」，正好构成一个环。通信是非阻塞的：[specforge/layers/ring/utils.py:91-119](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/utils.py#L91-L119) 说明 `send_recv` 用 `dist.batch_isend_irecv` 把「发本地 K/V、收邻居 K/V」打包成异步 P2P 操作，`commit` 提交、`wait` 等待完成。这样「发收」可以与本地的 flash 计算重叠，隐藏通信延迟。

#### 4.3.5 源码精读：前向轮转循环

把合并与环形通信拼起来就是前向：[specforge/layers/ring/ring_flash_attn.py:29-59](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L29-L59) 是核心循环。逐步看：

- 循环跑 `world_size`（即 `sp_ring_size`）步。
- 每步：先异步把当前 K/V 发给下一个 rank、从上一个 rank 收一份（`send_recv` + `commit`）。
- 用本地的 Q 对当前 K/V 跑一次 flash attention，得到 `block_out/block_lse`，再用 `update_out_and_lse` 合并。
- `wait` 等收完，把 K/V 换成刚收到的那份，进入下一步。

因果掩码跨 ring 的处理在 [ring_flash_attn.py:35](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L35) 与 [ring_flash_attn.py:45](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L45)：只有 `step <= rank`（即「自己及更靠前的段」）才需要算，且只有第一步（`step == 0`，自己的段）才施加局部因果掩码。这保证了整条序列的因果性不被破坏。

反向同理：[specforge/layers/ring/ring_flash_attn.py:67-149](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L67-L149) 说明反向也用一条环形 K/V 轮转通道，外加一条独立的 `dK/dV` 梯度轮转通道，逐块累加梯度。整套前向/反向被包进 `RingFlashAttnFunc`（[ring_flash_attn.py:152-241](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L152-L241)），对外暴露成可自动求导的 `ring_flash_attn_func` 等入口（[ring_flash_attn.py:305-336](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L305-L336)）。

#### 4.3.6 代码实践：源码阅读型

1. **实践目标。** 验证「合并是数学等价而非近似」。
2. **操作步骤。** 读 [utils.py:21-27](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/utils.py#L21-L27) 上方的注释与链接（指向 ring-flash-attention PR#34 的讨论）。用纸笔取 `lse_a=2, lse_b=1, out_a=0.6, out_b=0.3`，分别用「朴素指数形式」与代码里的「sigmoid 形式」算一次 `out_new`、`lse_new`。
3. **需要观察的现象。** 两种算法给出**完全相同**的数值（在浮点容差内）。
4. **预期结果。** 说明 ring attention 的分块合并不引入近似误差，全序列注意力结果与单卡一致——这正是 SpecForge 敢用它训草稿模型的依据。此为纸笔推导，无需 GPU，无「待本地验证」之忧。

#### 4.3.7 小练习与答案

**练习 1。** 如果 `sp_ring_size=1`，`ring_flash_attn_forward` 的循环还跑得起来吗？

> **答案。** 形式上 `world_size=1`，循环只跑一步、不发生任何 P2P 通信（`step+1 != world_size` 恒为假，跳过 send_recv），等价于一次普通 flash。但 [llama3_eagle.py:1440](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/modeling/draft/llama3_eagle.py#L1440) 在 `sp_ring_degree==1` 时根本不走 ring 分支而走普通 flash，更省事。

**练习 2。** 为什么 `RingComm` 用 `batch_isend_irecv`（异步）而不是同步的 `send/recv`？

> **答案。** 异步 P2P 让「把 K/V 发给邻居」与「本地用当前 K/V 算 flash」可以重叠（见 [ring_flash_attn.py:31-57](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/layers/ring/ring_flash_attn.py#L31-L57) 的 `commit`→计算→`wait` 顺序），从而把环形通信的延迟藏在计算背后。

## 5. 综合实践

**任务。** 为一台 8 卡机器设计一份合法的 EAGLE3 离线 USP 配置，并解释「SP peer 共享一条序列」的含义。

约束回顾（全部来自源码）：

- world size = 8，必须被 `sp_ulysses_size * sp_ring_size` 整除（[schema.py:863-878](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L863-L878)、[distributed.py:166-168](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/distributed.py#L166-L168)）。
- USP 必须离线、`batch_size=1`、`sp_ulysses * sp_ring > 1`（[schema.py:560-574](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L560-L574)、[schema.py:834-836](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L834-L836)）。
- 离线 `tp_size=1`（[schema.py:837-842](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/config/schema.py#L837-L842)）。

**设计。** 取 `sp_ulysses_size=2, sp_ring_size=2`，于是 `sp_size=4`，`draft_dp_size = 8/4 = 2`。关键字段如下（示例代码，仅展示相关段）：

```yaml
# 示例代码：EAGLE3 离线 USP，8 卡
training:
  strategy: eagle3
  attention_backend: usp
  sp_ulysses_size: 2
  sp_ring_size: 2
  batch_size: 1          # USP 强制
  tp_size: 1             # 离线强制
  accumulation_steps: 1  # 装配期会被放大成 1*4=4（见 assembly.py:519-524）
data:
  hidden_states_path: ./cache/hidden_states/train   # 离线特征
deployment:
  mode: local_colocated
  trainer:
    nnodes: 1
    nproc_per_node: 8   # world_size=8
```

**自检。**

- 整除：`8 % (2*2) == 0` ✓；`8 % tp_size(1) == 0` ✓。
- 乘积 > 1：`2*2 = 4 > 1` ✓。
- 离线：`hidden_states_path` 非空 → `mode == "offline"` ✓。
- 拓扑：8 卡被划成 `draft_dp_size=2` 个 draft-DP 组，每组 4 卡（2 Ulysses × 2 Ring）。

**解释「SP peer 共享一条序列」。** 这 4 张卡（一个 SP 组）**合力处理同一条逻辑序列**：数据加载时该序列被切成 4 段、每卡取一段加 `ttt_length` 重叠（[preprocessing.py:445-449](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/data/preprocessing.py#L445-L449)）；前向时 Ulysses 在 2 个头-peer 间 all-to-all，Ring 在 2 个序列-peer 间轮转 K/V，从而拼出全序列注意力。两个 draft-DP 组则各处理**不同的、互不相交**的序列——这才是真正的数据并行。对比纯 DP（不开 USP）时，8 张卡各算各的一条序列；开 USP 后变成「2 组 × 每组 4 卡算同一条」，用更多卡换来了能训练更长序列的能力。

**可选验证（待本地验证）。** 在装好 SpecForge 与 8 卡 GPU 的环境，先 `specforge train --plan -c 你的副本.yaml` 确认 `plan` 渲染出 8 个 worker、且无校验报错；再实际训练若干步，观察日志里 `device mesh`、SP 度数与 `accumulation_steps` 是否如预期（注意 accumulation 已被放大 4 倍）。

## 6. 本讲小结

- SpecForge 的并行拓扑由两条正交轴线决定：目标模型并行归外部 SGLang，草稿模型并行才是 trainer 的事；online 与非 EAGLE3 离线都只支持纯 DP，只有 EAGLE3 离线能开 USP。
- USP = Ulysses（沿**头维** all-to-all）× Ring（沿**序列维**环形轮转 K/V），`sp_size = sp_ulysses_size × sp_ring_size`；一个 SP 组内的 rank 共享同一条序列，不同 SP 组（draft-DP）处理不相交序列。
- 两条硬约束：world size 必须同时被 `tp_size` 和 `sp_ulysses × sp_ring` 整除；USP 必须离线、`batch_size=1`、SP 乘积大于 1——全部在 schema 与 `init_distributed` 双重 fail-fast。
- 数据加载阶段就把序列切成 SP 分片并算好 position ids；装配阶段会把 `accumulation_steps` 放大 `sp_size` 倍，保证「一个累积单元 = 一条完整逻辑序列」。
- ring attention 靠环形异步点对点通信轮转 K/V，用 online softmax（sigmoid 形式）做分块合并，数学上与全序列注意力等价、非近似；前向反向都自带自动求导。

## 7. 下一步学习建议

- 若想看「SP 放大后如何与梯度累积、optimizer 边界、durable ack 协同」，回看 u6-l3（Trainer/Controller 的 `optimizer_stepped` 单一权威边界）与 u6-l4（FSDP `no_sync`），把 USP 的 `accumulation_steps *= sp_size` 嵌进那条边界链里理解。
- 若关心在线分布式如何用纯 DP 跑通，继续 u7 系列（尤其 u7-l4 的 quantum = `dp × batch × accumulation` 握手），与本讲的 SP 分片形成对照。
- 想深入 ring attention 的工程细节，可顺读 `specforge/layers/ring/` 下 `ring_flash_attn.py` 的反向实现，并对照其注释里引用的上游 ring-flash-attention 讨论。
