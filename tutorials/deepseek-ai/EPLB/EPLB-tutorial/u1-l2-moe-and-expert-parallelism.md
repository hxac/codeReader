# 领域背景：MoE、专家并行与 DeepSeek-V3 路由

> 承接上一讲（u1-l1）：我们已经知道 EPLB 要解决的问题是「专家并行下各 GPU 负载不均，用冗余专家 + 启发式打包来缓解」。本讲暂不进入算法细节，而是补齐读懂算法所需的领域知识：MoE 层长什么样、逻辑专家与物理专家是什么、专家并行如何通信、DeepSeek-V3 的组受限路由为什么会导致「同组专家要放同一节点」这条放置约束，以及 prefill/decode 两阶段为何对应两种均衡策略。

## 1. 本讲目标

学完本讲，你应该能够：

1. 描述 MoE 层的结构（路由器 + 专家）与稀疏激活的工作方式。
2. 区分**逻辑专家**（logical expert）与**物理专家**（physical expert，即副本），并能用 `phy2log`、`logcnt` 的语言描述复制关系。
3. 解释**专家组**（expert group）约束从哪来、它如何限制「哪些专家必须放在同一个节点」，以及这条约束在代码里的四条整除断言。
4. 说明 prefill 阶段用较小 EP（hierarchical 策略）、decode 阶段用较大 EP（global 策略）的原因，并在源码中找到分派逻辑。
5. 完成主线实践：用纸笔为一个 12 专家、4 组、2 节点 8 GPU 的小型 MoE 设计放置草图，亲身体会组约束与负载均衡之间的矛盾。

## 2. 前置知识

### 2.1 从 Transformer 的 FFN 说起

标准 Transformer block = 注意力层 + 前馈网络（FFN）。FFN 通常就是两三层线性变换加激活函数，参数量占整个模型的大头。

MoE（Mixture of Experts，混合专家）的思路：把一个 FFN 替换成 \(N\) 个并列的「专家」——每个专家就是一个独立的小 FFN——再配一个**路由器**（router / gate）给 token 打分，每个 token 只经过其中 \(k\) 个专家（\(k \ll N\)）。这叫**稀疏激活**：模型参数可以做很大，但单个 token 的计算量只随 \(k\) 增长。

### 2.2 GPU、节点与互连

- **GPU（卡）**：一块加速卡，有自己的显存。
- **节点（node）**：一台多卡服务器。节点内的 GPU 通过高速互连（如 NVLink）相连，带宽高、延迟低。
- **节点间**：多台服务器经机房网络（如 InfiniBand / 以太网）互联，带宽与延迟通常都显著差于节点内。

EPLB 的参数 `num_nodes`、`num_gpus` 就对应这套层级，源码注释明确写了「节点内网络（例如 NVLink）更快」（见 4.2 的源码精读）。

### 2.3 本讲使用的记号

- \(w_e\)：逻辑专家 \(e\) 的**负载统计**（`eplb.py` 中变量名叫 `weight`，但语义是 load statistics，即一段时间内该专家被路由到的 token 量，不是模型权重）。
- \(k_e\)：专家 \(e\) 的副本数（代码里叫 `logcnt`）。
- \(L_g\)：某块 GPU（或某个节点、某个组）的负载，等于它所容纳专家的负载之和（副本按均摊计）。

### 2.4 需要的一点算术

只需两个概念：理想情况下每 GPU 平均负载

\[ \bar{L} = \frac{\sum_e w_e}{\text{num\_gpus}} \]

它是一切放置方案中「最重 GPU 负载」的理论下界；以及「复制摊薄」：一个被复制 \(k_e\) 份的专家，每个副本的期望负载约为

\[ \frac{w_e}{k_e} \]

这两条公式贯穿整个 EPLB 的设计直觉。

## 3. 本讲源码地图

| 文件 | 本讲用到的部分 | 作用 |
|---|---|---|
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 问题描述（L3-L13）、两种策略（L19-L31）、示例（L37-L57） | 领域背景的第一手材料 |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | `replicate_experts` 的 docstring 与初始化（L44-L64）、`rebalance_experts_hierarchical` 的签名与断言（L74-L96）、`rebalance_experts` 的签名与分派（L131-L156） | 本讲只读「接口契约 + 约束 + 分派」，**不**精读算法循环体 |

说明：`balanced_packing` 的贪心循环、`replicate_experts` 的逐副本分配、hierarchical 的三步张量变换，都属于第二单元（u2）的精读内容。本讲把源码当「术语表与合同」来读。

## 4. 核心概念与源码讲解

### 4.1 MoE 层与专家路由：逻辑专家与物理专家

#### 4.1.1 概念说明

- **MoE 层** = 路由器 + \(N\) 个专家。token 经过路由器打分，激活得分最高的 \(k\) 个专家，输出按路由得分加权求和。
- **逻辑专家**：模型定义里的第 \(e\) 号专家，拥有唯一一份权重。数量由模型结构决定，就是输入张量 `weight` 的第二维 `num_logical_experts`。
- **物理专家（副本）**：部署时可以把一个逻辑专家的权重复制 \(k_e\) 份、放到不同 GPU 上，每个副本都是一个物理专家。物理专家总数即 `num_replicas`（docstring 里也叫 `num_physical_experts`）。
- **为什么要复制**：路由器对每个 token 独立打分，某些「热」专家会被长期频繁选中。复制后，同一逻辑专家的多个副本共同接客，单个副本的期望负载降为 \(w_e / k_e\)。
- **关键约束**：一次重平衡中 `num_replicas` 是**固定预算**：

  \[ \sum_e k_e = \text{num\_replicas} \]

  复制是零和的——给热专家多一个副本，就要从别处扣掉一个名额。
- **复制的代价**：显存与参数量增加（每个副本一份完整专家权重），但**总计算量不变**（请求被分摊，而不是变少）。复制改善的是「瓶颈 GPU 的负载」，不是总 FLOPs。

#### 4.1.2 核心流程

单个 MoE 层前向（单个 token 视角）：

```text
token x
  │
  ▼
router(x) ──► 对 N 个逻辑专家打分 s_1 .. s_N
  │
  ▼
选 top-k 个专家 {e_1 .. e_k}（DeepSeek-V3 还先经过「组」的限制，见 4.3）
  │
  ▼
x 发给 e_1 .. e_k 的某个物理副本 ──► 各副本计算 y_i = Expert_{e_i}(x)
  │
  ▼
输出 = Σ s_i · y_i
```

EPLB 在这张图里的位置：它**不参与前向计算**，只在部署/重平衡时决定两件事——每个逻辑专家复制几份（\(k_e\)），以及每个物理副本放在哪块 GPU（`phy2log` 表）。

评价一个方案的标尺是「最重 GPU 的负载」\(L_{\max}\)，它不会低于平均 \(\bar{L}\)。EPLB 的目标是让 \(L_{\max}\) 尽量贴近下界——注意它是启发式，不保证最优（上一讲的伏笔，u3-l2 会定量评估）。

#### 4.1.3 源码精读

入口函数的 docstring 是本讲最重要的「术语表」：

[README.md:L35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L35) 指出主函数是 `eplb.rebalance_experts`；[README.md:L37](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37) 说明示例是「两层 MoE、每层 12 个专家，引入 4 个冗余专家，共 16 个副本放到 2 个节点、每节点 4 GPU」。

[eplb.py:L131-L146](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L146)：`rebalance_experts` 的签名与 docstring。注释原文写明：`weight: [layers, num_logical_experts], the load statistics for all logical experts`（输入是每个**逻辑**专家的负载统计）；`num_replicas: number of physical experts`（**物理**专家总数）；返回三张表——`physical_to_logical_map: [layers, num_replicas]`（每个物理专家对应哪个逻辑专家）、`logical_to_physical_map`、`expert_count: [layers, num_logical_experts]`（每个逻辑专家的副本数）。「逻辑/物理」这对词的权威定义就在这里。

[eplb.py:L44-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L57)：`replicate_experts` 的 docstring——「把 `num_log` 个专家复制成 `num_phy` 个副本」，返回 `phy2log: logical expert id of each physical expert`（每个物理专家的逻辑编号）、`rank: the replica rank`（同一逻辑专家的第几个副本）、`logcnt: number of replicas for each logical expert`（每个逻辑专家的副本数）。三个输出正是描述「复制关系」的全部信息。

[eplb.py:L62-L64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L64)：初始化——`phy2log` 先取 `arange(num_phy)`（前 `num_log` 个物理专家一一对应逻辑专家）、`rank` 全 0、`logcnt` 全 1，即「默认每个逻辑专家恰好 1 个副本」；随后 [eplb.py:L66-L70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L70) 的循环把 `num_phy - num_log` 个「冗余槽位」逐个分配给热专家（怎么选热专家，u2-l2 精读）。

[eplb.py:L95-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95-L96)：断言 `num_physical_experts % num_gpus == 0`，并由此算出 `phy_experts_per_gpu`——每块 GPU 的物理专家**槽位数必须相同且为整数**。

#### 4.1.4 代码实践

**实践目标**：不运行任何代码，仅凭 README 给出的输出，读出「谁被复制了」。

操作步骤：

1. 打开 [README.md:L54-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L54-L57) 的输出，取第 0 层：`[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]`。
2. 统计 0~11 每个逻辑专家编号在这 16 个槽位中出现的次数。
3. 对第 1 层 `[7, 10, 6, 8, 6, 11, 8, 9, 2, 4, 5, 1, 5, 0, 3, 1]` 做同样统计。
4. 自查：每层出现次数总和应等于 `num_replicas = 16`。

**需要观察的现象**：大部分专家出现 1 次，少数出现 2 次；且**两层被复制的专家并不相同**（`weight` 第一维是 layer，每层独立计算放置方案——这解释了输出为什么是 `[layers, ...]` 形状）。

**预期结果**：第 0 层出现 2 次的是 4、5、10、1；第 1 层出现 2 次的是 1、5、6、8。各自 4 个专家 ×2 + 8 个专家 ×1 = 16。

#### 4.1.5 小练习与答案

**练习 1**：`num_logical_experts = 12`、`num_replicas = 16` 时，`logcnt` 全部元素之和是多少？平均每个专家几个副本？

答案：和恒等于 `num_replicas = 16`（每个物理专家在且仅在一个槽位被计数一次）；平均 16/12 ≈ 1.33，实际取值是整数组合，本例为「4 个专家取 2、其余 8 个取 1」。

**练习 2**：把示例的 `num_replicas` 从 16 改成 12（即完全不复制），合法吗？

答案：逻辑上合法（`phy2log` 退化为 12 个逻辑专家的某个排列、`logcnt` 全 1），但 12 % 8 ≠ 0 违反 [eplb.py:L95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95) 的断言——物理专家无法均分到 8 块 GPU。示例选 16，一半原因正是「12 个专家本来就放不均匀到 8 卡」。

**练习 3**：复制一个专家会减少它的总计算量吗？

答案：不会。该专家的总请求量 \(w_e\) 不变，只是摊到 \(k_e\) 个副本上，单副本期望负载降为 \(w_e/k_e\)；代价是多占一份显存。所以复制换来的是「瓶颈 GPU 更轻」，不是「总 FLOPs 更少」。

### 4.2 专家并行（EP）与 all-to-all 通信：节点内 vs 节点间

#### 4.2.1 概念说明

- **专家并行（Expert Parallelism, EP）**：把 \(N\) 个专家分布到 \(G\) 块 GPU 上，每块 GPU 只存放并计算其中一部分专家。与数据并行（每卡一份完整模型、处理不同数据）、张量并行（把单个矩阵切到多卡）相对，EP 切的是「专家」这个维度。
- **路由的必然结果**：一个 token 要去的 \(k\) 个专家可能落在任意 GPU 上。于是每个 MoE 层的前后各需要一次 **all-to-all** 通信：第一次（dispatch）把 token 发到「持有其专家」的 GPU；第二次（combine）把计算结果送回出发地加权合并。
- **通信成本与物理距离强相关**：节点内 NVLink 快、节点间网络慢。同一个 token 的 \(k\) 个专家若分散在多个节点，就要付多次跨节点传输；若都落在同一节点，通信基本被节点内高带宽消化。
- EPLB 能左右的只有「专家放在哪」，从而间接决定每个 token 平均要跨几次节点边界——这正是 hierarchical 策略优化通信的抓手。

#### 4.2.2 核心流程

一个 MoE 层在 EP 下的执行（G 块 GPU）：

```text
每块 GPU 各持有一批 token（按数据并行划分）
   │
   ├─ 本地 router 打分，得知每个 token 的 top-k 专家编号
   ▼
all-to-all #1（dispatch）：按「专家所在 GPU」重组 token
   │
   ├─ 各 GPU 对本地专家做批量前向
   ▼
all-to-all #2（combine）：结果送回原 GPU
   │
   └─ 按路由得分加权求和，得到该层输出
```

用 \(d(\text{token})\) 表示一个 token 的激活专家所跨越的**节点数**，则跨节点流量近似与之成正比：

\[ \text{跨节点流量} \;\propto\; \sum_{\text{token}} d(\text{token}) \]

无任何放置约束时 \(d\) 最坏可达 \(\min(k, \text{节点数})\)；4.3 节的「组对齐节点」能把它压到最多 2。

#### 4.2.3 源码精读

[README.md:L3-L4](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L3-L4)：EP 的定义与本仓库存在的理由——「使用专家并行时，不同专家被分配到不同 GPU；由于专家负载随 workload 变化，保持 GPU 间负载均衡很重要」。

[eplb.py:L74-L82](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L82)：`rebalance_experts_hierarchical` 的 docstring，其中 `num_nodes: number of server nodes, where the intra-node network (e.g, NVLink) is faster`（第 81 行）。这句注释是整个 hierarchical 策略成立的前提：**节点内带宽 ≫ 节点间带宽**，所以「把整组专家钉在一个节点」才有价值。

[eplb.py:L94](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L94)：断言 `num_gpus % num_nodes == 0`——节点是同构的（每节点 GPU 数相同），这样「GPU k 属于节点 k // (num_gpus // num_nodes)」的映射才成立，hierarchical 第三步的节点内 GPU 打包才能按统一规格进行。

#### 4.2.4 代码实践

**实践目标**：确认「节点 / 互连」这组概念在代码中的地位。

操作步骤：

1. 在 `eplb.py` 中搜索 `num_nodes`，逐处记录它参与了哪些断言、哪些计算（本讲只看 [eplb.py:L89-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96) 的断言区即可）。
2. 若手边有 GPU 机器，运行 `nvidia-smi topo -m`，观察卡间互连类型（NVLink / PCIe）与节点边界。

**需要观察的现象**：断言区里 `num_nodes` 同时约束了组数（L92）和 GPU 数（L94）；节点数是 hierarchical 策略里「组的目的地数量」（[eplb.py:L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L105) 把组打包到 `num_nodes` 个节点）。

**预期结果**：源码阅读部分可直接完成；`nvidia-smi topo -m` 的输出取决于硬件，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 EP 下「每 GPU 专家数相同」不等于负载均衡？

答案：因为专家热度不同：路由器可能长期偏爱某些专家，且热度随 workload 漂移。GPU 的计算耗时由「它持有的专家被选中的总负载」决定，专家个数相同只保证显存占用相同。木桶效应下，整体速度被最重的 GPU 锁死。

**练习 2**：如果节点间带宽与节点内一样快，「组对齐节点」还有意义吗？

答案：意义大幅下降。组对齐的全部收益来自「把流量从慢链路挪到快链路」；带宽拉平后组放在哪都一样，不如直接全局均衡（global 策略），更简单也更均衡。

**练习 3**：all-to-all 在每个 MoE 层都会发生，这意味着什么？

答案：大模型有几十层 MoE，每一层都要 dispatch / combine 各一次 all-to-all。通信代价被层数放大，因此「放置方案减少跨节点流量」的收益同样被层数放大——这是一开局就值得优化的原因。

### 4.3 DeepSeek-V3 的组受限路由与「同组放同节点」

#### 4.3.1 概念说明

- **组受限路由**（group-limited expert routing）是 DeepSeek-V3 采用的机制：把路由专家分成若干**组**；对每个 token，先按组汇总组内专家得分、选取得分最高的少数组，再只在选中的组内挑选最终激活的专家。效果是：**一个 token 的所有激活专家集中在极少数组里**。（DeepSeek-V3 论文中为 256 个路由专家分 8 组、每 token 激活 8 个路由专家且限制在前 2 个组内；具体超参数以论文与官方配置为准。）
- **EPLB 的核心洞察**：若把「同一个组的所有专家」放进同一个节点，那么任何 token 的跨节点通信至多发生在它选中的那 2 个组之间——token 的跨节点次数从「最多 \(k\)」压到「最多 2」。
- **组约束的代价**：组是模型路由层面的划分、不可拆分。一旦要求组对齐节点，放置问题就从「逐专家自由摆放」退化为「以整组为单位的装箱」，自由度下降、均衡能力受限。同时组内还可能包含被复制的专家——**副本也必须留在该组所在节点**，否则token跨节点访问副本会让复制反而增加跨节点流量。这是下一节综合实践中你会亲手遇到的矛盾。

注意：「组」与 GPU 无关，是路由层面的抽象；EPLB 的输入里分组信息只体现为 `num_groups` 这一个数字，具体哪个专家属于哪个组由编号规则决定（见下面源码精读）。

#### 4.3.2 核心流程

从「组受限路由」推导出「放置约束」的完整链条：

```text
路由侧：每个 token 只碰 ≤ 少数几个组（论文中为 2）
   │  （由此希望）
   ▼
放置侧：同组的所有专家（含全部副本）放在同一个节点
   │  （等价于一个装箱问题）
   ▼
把 num_groups 个组（每组负载 = 组内专家负载之和）装进 num_nodes 个节点
   │  （可行性条件）
   ▼
num_logical_experts % num_groups == 0   （每组专家数相同）
num_groups % num_nodes == 0             （每节点分到整数个组）
```

EPLB hierarchical 策略的第一步（Step 1）做的就是中间那步「组 → 节点」装箱；第二步（Step 2）在节点内复制、第三步（Step 3）把物理专家装进 GPU。三步的算法细节都在 u2 精读，本讲只需记住这个骨架。

#### 4.3.3 源码精读

[README.md:L5-L8](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L5-L8)：策略动机的原文——采用冗余专家复制重载专家、启发式打包到 GPU；「得益于 DeepSeek-V3 使用的 group-limited expert routing，我们还**尽可能**把同组专家放到同一节点以减少节点间数据流量」。「尽可能」（whenever possible）指的正是整除条件满足时。

[eplb.py:L89-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96)：四条整除断言构成「组约束在代码里的形态」：

| 断言（原文） | 含义 |
|---|---|
| `num_logical_experts % num_groups == 0`（L90） | 每组专家数相同，`group_size = num_logical_experts // num_groups`（L91） |
| `num_groups % num_nodes == 0`（L92） | 每节点分到**整数个组**，`groups_per_node`（L93） |
| `num_gpus % num_nodes == 0`（L94） | 每节点 GPU 数相同 |
| `num_physical_experts % num_gpus == 0`（L95） | 每块 GPU 分到整数个物理专家槽位（L96） |

[eplb.py:L104](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104)：`weight.unflatten(-1, (num_groups, group_size)).sum(-1)`——把专家维 reshape 成 `(组数, 每组大小)` 再对组内求和得到每组负载 `tokens_per_group`。这行代码同时告诉我们**分组方式：按专家编号连续切块**，即专家 \(e\) 属于组 \(e \,//\, \text{group\_size}\)。README 示例（12 专家、4 组）就是 {0,1,2}、{3,4,5}、{6,7,8}、{9,10,11} 四个组。

[eplb.py:L103-L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L105)：Step 1 注释「pack groups to nodes」——先算组负载，再调用 `balanced_packing(tokens_per_group, num_nodes)` 把组打包到节点。

[eplb.py:L110-L113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113)：Step 2「construct redundant experts within nodes」——先把专家按节点重排并 `view` 成 `[num_layers * num_nodes, ...]`，再对**每个节点各自**的专家做复制。副本因此被钉死在该组所在节点上，印证了 4.3.1 的约束。

#### 4.3.4 代码实践

**实践目标**：验证「组 = 按编号连续切块」这一分组方式。

操作步骤（示例代码，需本地已安装 PyTorch，待本地验证）：

```python
# 示例代码：验证 eplb.py 的专家分组方式（对应 eplb.py:104 的 unflatten）
import torch

num_logical_experts, num_groups = 12, 4
group_size = num_logical_experts // num_groups

ids = torch.arange(num_logical_experts)                 # 专家编号 0..11
groups = ids.unflatten(-1, (num_groups, group_size))    # reshape 成 (4, 3)
for g, members in enumerate(groups.tolist()):
    print(f"组 {g}: 专家 {members}")
```

**需要观察的现象**：`unflatten` 只是 reshape、不改变元素顺序，所以每个组的成员编号必然连续。

**预期结果**：

```text
组 0: 专家 [0, 1, 2]
组 1: 专家 [3, 4, 5]
组 2: 专家 [6, 7, 8]
组 3: 专家 [9, 10, 11]
```

记住「连续切块」这一点：u2-l4 讲 `log2mlog` 编号变换时，所有推导都建立在它之上。

#### 4.3.5 小练习与答案

**练习 1**：12 个专家、`num_groups = 5`，会发生什么？

答案：`12 % 5 ≠ 0`，违反 [eplb.py:L90](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90) 的断言，直接抛异常。组数必须整除逻辑专家数。

**练习 2**：一个 token 激活 8 个专家、分属 2 个组。组对齐前最坏跨几个节点？对齐后呢？

答案：对齐前，8 个专家最多落在 8 个不同节点（若 EP 跨 8 个节点）；对齐后，激活专家集中在 2 个组内、每组整组在同一节点，至多跨 2 个节点。

**练习 3**：为什么断言方向是 `num_groups % num_nodes == 0`（组数是节点数的倍数），而不是节点数整除组数？

答案：因为装箱单位是「组」、目的地是「节点」：要让每个节点分到**相同个数**的组（每组专家数固定，这样各节点的专家槽位才均匀），组数就必须是节点数的整数倍。README 里「服务器节点数整除专家组数」（nodes divide groups）说的正是这同一个关系。

### 4.4 prefill 与 decode：EP 规模差异如何决定策略选择

#### 4.4.1 概念说明

- 推理的两个阶段：**prefill（预填充）** 一次性处理整段输入提示，token 批量大、矩阵大，**计算密集**；**decode（解码）** 逐 token 自回归生成，每步每个请求只算 1 个 token，**访存密集**、单步计算量小。
- 为什么两个阶段的 EP 规模不同（以下是通行部署考量的一般性解释；README 只给了 smaller / larger 的结论）：
  - **prefill**：每步已有海量 token，少量 GPU 上的专家就能被「喂饱」，继续扩大 EP 只会增加通信；多出的 GPU 更适合做数据并行 / 流水线并行。于是 EP 组内 GPU 数少、常落在少数节点内 → 组数容易被节点数整除 → **hierarchical**。
  - **decode**：每步计算量极小，瓶颈在「专家权重放不放得下 + 读得多快」，必须把专家摊到更多 GPU 上（同时缓解显存压力）才能提高吞吐 → EP 更大、跨越更多节点 → 组数通常不再是节点数的整数倍 → 组对齐不可行 → **global**：放弃组约束，全局复制并直接打包到 GPU。
- 两个策略的取舍：**hierarchical 保通信（组对齐节点）但牺牲均衡自由度；global 保均衡但接受更多跨节点流量**。decode 阶段每 token 的 all-to-all 消息很小，跨节点代价相对可承受，这就是「大 EP 的 decode 用 global」说得通的原因。

#### 4.4.2 核心流程

策略分派的决策流：

```text
rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
   │
   ├─ num_groups % num_nodes == 0 ?
   │        │
   │        ├─ 是 ──► hierarchical：
   │        │      组打包到节点 → 节点内复制 → 物理专家打包到 GPU
   │        │      （典型：prefill，较小 EP）
   │        │
   │        └─ 否 ──► global：
   │               退化为 rebalance_experts_hierarchical(weight,
   │                                num_replicas, 1, 1, num_gpus)
   │               「1 个组、1 个节点、全部 GPU」——组约束自动消失，
   │               等价于「全局复制 + GPU 级打包」
   │               （典型：decode，较大 EP）
```

注意 global 的实现技巧：**不是另写一套算法，而是用退化参数复用 hierarchical**（组数 = 1、节点数 = 1、GPU 数 = EP 总数）。「1 个组、1 个节点」的世界里不存在组对齐问题。这个设计模式的优缺点在 u3-l5 再评价。

#### 4.4.3 源码精读

[README.md:L19-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L25)：hierarchical 策略原文——当服务器节点数整除专家组数时使用；先把组均衡地打包到节点，再在节点内复制专家，最后把复制后的专家打包到 GPU；「可用于 expert-parallel size 较小的 prefill 阶段」。

[README.md:L27-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L27-L31)：global 策略原文——其余情况使用；无视分组、全局复制专家并打包到 GPU；「可用于 expert-parallel size 较大的 decode 阶段」。

[eplb.py:L148-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L156)：代码中的分派。`weight = weight.float().cpu()` 之后，`if num_groups % num_nodes == 0:` 走 hierarchical 原参数；`else:` 分支调用 `rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)`——`1, 1, num_gpus` 正是 4.4.2 说的退化参数。注意一个细节：**global 分支里原始的 `num_nodes` 被完全忽略**（传给 hierarchical 的是 1），连「num_gpus 是否整除原始 num_nodes」都不再检查。

（`weight.float().cpu()` 把整型统计转成浮点并搬回 CPU 的原因，留到 u3-l3「性能与设备一致性」一讲展开，本讲只需记住入口处有这一步。）

#### 4.4.4 代码实践

**实践目标**：不运行代码，纯靠阅读判断两组参数各走哪条策略分支。

操作步骤：

1. **组合 A**（README 示例）：`num_groups=4, num_nodes=2, num_gpus=8, num_replicas=16`。
2. **组合 B**：`num_groups=4, num_nodes=3, num_gpus=8, num_replicas=16`（3 节点共 8 卡的集群）。
3. 对每个组合写出：`num_groups % num_nodes` 的值、命中的分支、实际传给 `rebalance_experts_hierarchical` 的参数、复制发生在什么范围。

**需要观察的现象**：组合 B 中 4 % 3 = 1 ≠ 0，进入 global 分支；此时断言 `num_gpus % num_nodes == 0`（8 % 3 ≠ 0）**并不会报错**，因为 global 分支里 `num_nodes` 已经被替换成 1（8 % 1 == 0）。原始的 3 节点拓扑信息被丢弃，8 块卡被当作一台「单节点大机器」。

**预期结果**（纯推演，结论可在 u1-l3 首次运行时顺手验证，待本地验证）：

- 组合 A：hierarchical，实际参数 (16, 4, 2, 8)，复制发生在每个节点内部（每节点 6 个逻辑专家 → 8 个物理专家）。
- 组合 B：global，实际参数 (16, 1, 1, 8)，12 → 16 的复制在全局进行，4 个组的划分不参与任何计算。

#### 4.4.5 小练习与答案

**练习 1**：global 分支传入 `num_nodes=1` 意味着什么？

答案：把整个 EP 集群看作一台「单节点机器」：没有节点边界、没有组对齐问题，唯一要做的就是把 `num_replicas` 个物理专家均衡地放到 `num_gpus` 块 GPU 上。

**练习 2**：hierarchical 和 global 各牺牲了什么？

答案：hierarchical 牺牲逐专家自由放置（整组钉在节点、副本不跨节点），换取更少的节点间流量；global 牺牲组对齐（跨节点流量上升），换取全局的复制与均衡自由度。

**练习 3**：为什么「组数是否整除节点数」会随部署形态改变，而组数本身不变？

答案：组数由模型结构决定（路由分组写死在模型里）；节点数由当前 EP 世界大小（world size）的部署拓扑决定。prefill 与 decode 使用不同 EP 规模，所以同一个模型可能一个阶段走 hierarchical、另一个阶段走 global。

## 5. 综合实践：纸笔设计一份小型 MoE 放置方案

这是本讲的主线实践，参数与 README 示例完全一致：**12 逻辑专家、4 组、2 节点 8 GPU（每节点 4 卡）、物理专家总数 16**。使用第 0 层的负载统计：

[README.md:L43](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L43)：`weight[0] = [90, 132, 40, 61, 104, 165, 39, 4, 73, 56, 183, 86]`。

### 步骤

1. **算组负载**：按 4.3 的分组 {0,1,2} / {3,4,5} / {6,7,8} / {9,10,11} 求每组负载和。
   （自查点：四组之和应为 1033；理想每 GPU 负载 \(\bar{L} = 1033/8 \approx 129.1\)。）
2. **组分到节点**：每节点恰 2 个组，目标是两节点负载尽量接近。写下你的方案与两节点各自的负载。
3. **分配冗余副本预算**：全网新增 16 − 12 = 4 个冗余副本。由于副本不能跨节点（4.3 的约束），你要先决定「两节点各分到几个冗余」，再在每个节点内挑选复制对象（提示：直觉是复制后单副本期望负载 \(w_e / k_e\) 最大的专家；算法的做法是把每节点的物理专家数固定为 `num_physical_experts // num_nodes = 8`，即每节点固定 2 个冗余，见 [eplb.py:L113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113)——你可以先自由分配，再对比这个约束）。
4. **装进 GPU**：每节点把分到的 8 个物理专家放到 4 块 GPU，每卡恰 2 个（为什么必须每卡 2 个？见 [eplb.py:L95-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95-L96)）。
5. **算均衡度**：每块 GPU 的负载 = 槽位负载和（被复制的专家按 \(w_e/2\) 计），计算 max/min、max/\(\bar{L}\)。
6. **记录矛盾**：写下至少一处「如果允许破坏组约束（把某个专家或副本挪去另一个节点）会更均衡」的时刻，并估计它换来多少均衡、付出多少跨节点流量。

### 需要观察的现象

- 12 个逻辑专家若不复制，根本无法均分到 8 卡（12 % 8 ≠ 0）——这是 `num_replicas=16` 的必要性之一。
- 两节点负载很难完全相等：组负载是「凑数」的，你只能在 {组负载的两两组合} 里挑，自由度有限。
- 你很可能想把某个热专家的第二个副本放到另一个节点来填平差距——但 hierarchical 算法不会这么做（副本被钉在组所在节点，见 4.3 源码精读）。

### 预期结果

- 一张 8 卡 × 2 槽位的草图，每个槽位标注所放逻辑专家；被复制者出现两次，且两个副本在同一节点。
- 一份「组约束代价」记录：两节点负载差、max GPU 负载相对 \(\bar{L} \approx 129.1\) 的超出比例。
- **保留你的方案**。u1-l3 首次运行 `rebalance_experts` 后，把算法输出的 `phy2log[0]` 与草图逐槽位对照（前 8 列对应节点 0 的 4 块卡 × 2 槽位，后 8 列对应节点 1；可先检查「前 8 列的专家编号是否全部来自同两个组」）。本讲不运行代码，对照环节待本地验证。

## 6. 本讲小结

- MoE 层 = 路由器 + \(N\) 个专家，token 只激活 top-\(k\) 个；**逻辑专家**是模型里的第 \(e\) 号专家，**物理专家**是它的副本；物理专家总数 `num_replicas` 是固定预算（\(\sum_e k_e = \text{num\_replicas}\)），复制摊薄单副本负载但不减少总计算量，代价是显存。
- 专家并行把不同专家放到不同 GPU，每个 MoE 层需要 dispatch / combine 两次 all-to-all；节点内互连（NVLink）远快于节点间，因此「专家放得近」直接省通信，且收益被 MoE 层数放大。
- DeepSeek-V3 的组受限路由使每个 token 的激活专家集中在少数组内；把同组专家（含全部副本）放进同一节点，可将 token 的跨节点次数从最多 \(k\) 压到最多 2——代价是放置自由度下降，并派生出 `eplb.py:L89-L96` 的四条整除断言。
- prefill 计算密集、用较小 EP → 组数常能被节点数整除 → hierarchical；decode 访存密集、用较大 EP → 组对齐通常不可行 → global（代码里用 `(1, 1, num_gpus)` 退化参数复用 hierarchical 实现，原始节点数被忽略）。
- EPLB 的输入是逻辑专家的负载统计 `weight`（如何统计与预测不在本仓库范围），输出是 `phy2log` / `log2phy` / `logcnt` 三张表；本讲只建立了这些术语的语义，尚未进入算法。

## 7. 下一步学习建议

- 下一讲 **u1-l3《环境搭建与首次运行》**：安装 PyTorch、运行 README 示例，把你在本讲综合实践里的纸笔方案与算法真实输出逐槽位对照，并解释返回值 `log2phy` 中 `-1` 的含义。
- 对照材料：README 示例（12 专家 → 16 副本、2 节点 8 卡）与本讲综合实践参数完全相同，是天然的「人机对拍」用例。
- 想追溯路由机制的原始出处，可阅读 DeepSeek-V3 技术报告中关于 group-limited expert routing 与负载均衡的章节（超参数以其原文为准）。
- 之后再进入 **u1-l4** 建立「四函数调用地图」，从下一单元（u2）开始逐函数精读源码。
