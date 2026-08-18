# 项目概览：EPLB 是什么、解决什么问题

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出**专家并行（Expert Parallelism, EP）下 GPU 负载不均衡的成因与危害**：为什么把不同专家放到不同 GPU 后，某些 GPU 会成为拖慢整体训练的"短板"。
2. 描述 EPLB 的整体解决思路：**冗余专家（redundant experts）+ 启发式打包（heuristic packing）**——先复制重载专家摊薄其负载，再把所有物理专家尽可能均匀地装到各个 GPU 上。
3. 区分 **hierarchical（层级）** 与 **global（全局）** 两种负载均衡策略，并说出它们各自适合的部署场景（prefill 小 EP 规模 vs decode 大 EP 规模）。

本讲是整本手册的第一讲，**不要求读懂任何算法代码**。我们把重心放在 README 上，先把"EPLB 要解决什么问题、用什么思路解决"想清楚，再去看它给出的示例输出，学会"读懂一张专家放置方案表"。后续讲义才会逐行精读 `eplb.py`。

## 2. 前置知识

本讲需要的背景概念不多，用通俗语言逐一解释：

- **MoE（Mixture of Experts，混合专家模型）**：一种层结构。普通 Transformer 层里每个 token 都经过同一个 FFN；MoE 层里则有很多个并列的"专家"网络（本质也是 FFN），外加一个**路由器（router）**。每个 token 到来时，路由器只把它交给其中少数几个专家计算，再把结果汇总。这样模型总参数量很大，但每个 token 的实际计算量很小。
- **专家（expert）**：MoE 层里的一个子网络。本仓库把一个 MoE 层里原本的专家称为**逻辑专家（logical expert）**，把复制之后实际部署的专家实例称为**物理专家（physical expert / replica）**。
- **专家并行（Expert Parallelism, EP）**：一种分布式部署方式——不同的专家放到不同的 GPU 上，token 需要哪个专家就把数据发给哪个 GPU。与之相对的是数据并行（每个 GPU 放完整模型、处理不同数据）、张量并行（把单个层切开）等。
- **节点（node）与 GPU**：一台服务器叫一个节点。节点内的 GPU 通过 NVLink 等高速互联通信，跨节点网络则慢且容易成为瓶颈。`eplb.py` 的参数注释原话就是 "the intra-node network (e.g, NVLink) is faster"（[eplb.py:L81](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L81)）。
- **负载（load）**：一个专家的负载指一段时间内路由到它的 token 数量（或计算量）。它由数据分布决定，会随训练/推理内容变化。EPLB 需要外部给它"每个专家的估计负载"作为输入；README 明确说**负载预测方法不在本仓库范围内**，常用做法是历史统计的滑动平均（[README.md:L10-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)）。
- **木桶效应（负载不均的危害）**：分布式同步计算中，每一步所有 GPU 都要互相等齐（通信同步）。GPU 再快也得等最慢的那个，所以整体速度由**负载最重的 GPU** 决定。

> 提示：以上是理解 EPLB 所需的领域背景。其中关于 DeepSeek-V3 路由细节的内容（如组受限路由）属于论文背景知识，本仓库代码本身并不实现路由，阅读时注意区分。

## 3. 本讲源码地图

EPLB 是一个极小的算法仓库，全部内容如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 项目唯一文档：问题背景、两种策略、接口与示例。**本讲的主线**。 |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | 全部算法实现，约 165 行、四个函数，只依赖 PyTorch（[eplb.py:L1-L4](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L1-L4)）。本讲只看入口 `rebalance_experts`，逐行精读留给第二单元。 |
| [example.png](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/example.png) | README 示例对应的专家复制与放置方案示意图（[README.md:L59-L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L59-L62)）。 |
| [LICENSE](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/LICENSE) | MIT 许可证（[README.md:L65-L67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L65-L67)）。 |

两点结构观察，帮你建立预期：

- 仓库**没有**打包文件（`pyproject.toml`、`setup.py`）、没有测试目录、没有 CI 配置。`eplb.py` 作为一个纯 Python 模块被直接 `import eplb` 使用（示例见 [README.md:L39-L42](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L39-L42)）。
- 模块对外只导出一个函数：`__all__ = ['rebalance_experts']`（[eplb.py:L162-L164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L162-L164)）。这就是整个库的公开接口，本讲 4.4 节会用到它的输出。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 问题：专家并行下的 GPU 负载不均（成因与危害）
2. 思路：冗余专家 + 启发式打包
3. 两种策略：hierarchical 与 global
4. 从示例输出读懂放置方案（`phy2log` → 节点 / GPU / 复制关系表）

### 4.1 问题：专家并行下的 GPU 负载不均

#### 4.1.1 概念说明

专家并行把不同专家放到不同 GPU 上。看起来只要专家数是 GPU 数的整数倍，每个 GPU 分到的专家数一样多，就"公平"了——但真正决定耗时的是**每个 GPU 分到的总 token 数**，而不是专家个数。

路由器按 token 的内容选专家，而数据分布不均匀：某些专家（比如擅长处理常见模式的专家）被选中的次数远多于 others。于是"每 GPU 专家数相同"的均匀划分，实际负载可能相差数倍。更麻烦的是，负载不是静态的——数据分布随训练进程、随请求内容变化，今天均匀的方案明天可能严重倾斜。

危害就是木桶效应：同步训练/推理中每一步所有 GPU 必须等齐，整体速度被最重的 GPU 锁死。一个 GPU 闲着、另一个 GPU 排长队，等于花钱买了算力却用不上。

#### 4.1.2 核心流程

把问题形式化。设某层有 \( n \) 个逻辑专家，第 \( e \) 个专家的估计负载（token 数）为 \( w_e \)；\( m \) 个 GPU 各持有若干专家，GPU \( g \) 的负载为：

\[ L_g = \sum_{e \,\in\, \text{GPU } g} w_e \]

整体步时近似由 \( \max_g L_g \) 决定。定义**不均衡度**：

\[ \rho = \frac{\max_g L_g}{\bar{L}}, \qquad \bar{L} = \frac{1}{m}\sum_g L_g \]

\( \rho = 1 \) 是理想状态（完全均衡，实际达不到）；\( \rho \) 越大，浪费的算力越多。EPLB 的目标就是给定 \( w_e \) 的估计，产出一个让 \( \rho \) 尽量接近 1 的专家放置方案。

数据的流向可以概括为：

```text
历史统计（每个专家的 token 数）
        │  滑动平均等预测方法（不在本仓库范围）
        ▼
专家负载估计 weight [层数 × 逻辑专家数]
        │  eplb.rebalance_experts(...)
        ▼
放置方案（哪个物理专家放在哪个 GPU、对应哪个逻辑专家）
        │  训练/推理框架据此重排权重与路由表（也不在本仓库范围）
        ▼
重新均衡后的集群
```

EPLB 只负责中间一步：**从负载估计到放置方案**。

#### 4.1.3 源码精读

README 开头两句话完整陈述了这个问题：

> When using expert parallelism (EP), different experts are assigned to different GPUs. Because the load of different experts may vary depending on the current workload, it is important to keep the load of different GPUs balanced.

见 [README.md:L3-L4](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L3-L4)。这段话点出三个要素：EP 的划分方式（专家→GPU）、负载差异的来源（depends on the current workload）、以及目标（不同 GPU 负载均衡）。

紧接着 README 划定了仓库边界——算法只消费"估计负载"，负载怎么预测是别人的事：

> The algorithm computes a balanced expert replication and placement plan based on the estimated expert loads. Note that the exact method to predict the loads of experts is out of this repo's scope. A common method is to use moving average of historical statistics.

见 [README.md:L10-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)。对应到代码，入口函数 `rebalance_experts` 的第一个参数 `weight` 的文档就写作 "the load statistics for all logical experts"（[eplb.py:L136-L137](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L136-L137)），形状是 `[层数, 逻辑专家数]`。

#### 4.1.4 代码实践

**实践目标**：建立对"不均衡度 \( \rho \)"的手感，理解为什么必须看总负载而不是专家数。

**操作步骤**（纸笔即可，无需运行代码）：

1. 设想 4 个 GPU、8 个专家，每 GPU 放 2 个专家（数量上完全公平）。
2. 给定两组负载：
   - 均匀情形：\( w = [50, 50, 50, 50, 50, 50, 50, 50] \)；
   - 长尾情形：\( w = [400, 300, 50, 50, 50, 50, 25, 25] \)。
3. 对长尾情形，无论怎么两两分组，算出各 GPU 负载与 \( \rho \)（提示：最重的两个专家若不在同一 GPU，最优分组是 \(\{400,25\},\{300,50\},\{50,50\},\{50,50\}\)）。
4. 思考：只靠"挪动专家位置"（不复制），\( \rho \) 最低能到多少？

**需要观察的现象**：均匀情形下随便放都是 \( \rho = 1 \)；长尾情形下无论怎么摆放，总有 GPU 负载 425（\(\{400,25\}\)），\( \bar{L} = 269 \)，\( \rho \approx 1.58 \)。

**预期结果**：当单个专家的负载就超过平均值时，**单纯重新排列无法解决不均衡**——这就是下一节"复制重载专家"的动机。

（本实践为纸笔推演，结论可自行验算；无需本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：既然某个专家特别重，为什么不干脆把它单独放到一个 GPU 上？
**答案**：GPU 的专家容量是固定整数——物理专家总数必须被 GPU 数整除，每 GPU 恰好 `num_replicas / num_gpus` 个槽位（示例中 16/8=2，见 [README.md:L37](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37)）。"单独占一个 GPU"会浪费槽位，且其余 GPU 也得各放整数个专家，问题并没有消失。

**练习 2**：EPLB 的输入 `weight` 从哪里来？仓库自己会统计吗？
**答案**：不会。README 明确说负载预测不在范围内，常用做法是历史统计的滑动平均（[README.md:L11-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L11-L13)）。EPLB 只接收一个形如 `[层数, 逻辑专家数]` 的负载统计张量。

**练习 3**：如果所有专家负载完全相同，还需要 EPLB 吗？
**答案**：这一次不需要——任意均匀划分都有 \( \rho = 1 \)。但真实负载随 workload 漂移，"现在均匀"不等于"一直均匀"，所以实际系统中 EPLB 要被周期性调用、跟随负载变化重排。这也解释了为什么它的输入是"统计值"而不是常量。

### 4.2 思路：冗余专家 + 启发式打包

#### 4.2.1 概念说明

既然瓶颈是"个别专家太重"，最直接的办法就是**把重载专家复制多份**：两份副本分摊同一个逻辑专家的 token，每份的期望负载减半。这就是 DeepSeek-V3 论文中的**冗余专家（redundant experts）**策略。复制出来的实例叫**物理专家（replica）**，与原始的**逻辑专家**区分开。

复制带来两个新问题，正好对应 EPLB 的两个动作：

1. **复制谁？** 冗余名额有限（物理专家总数固定，例如 12 逻辑 → 16 物理，冗余名额 4 个），要挑"复制收益最大"的专家。
2. **怎么放？** 复制后有 16 个物理专家，要装到 8 个 GPU 上（每 GPU 恰好 2 个），使各 GPU 总负载尽量相等。这是一个装箱问题（bin packing），EPLB 用**启发式（heuristic）**求解——不追求理论最优，追求快且足够好。

注意代价：复制会增加参数量与显存占用（16 物理专家比 12 逻辑专家多存 4 份完整专家权重），换取的是瓶颈负载下降。这是一个明确的资源换时间的权衡。

#### 4.2.2 核心流程

EPLB 整体流程：

```text
输入：weight [层数 × num_logical_experts]（估计负载）
        │
        ├─ ① 决定复制：挑出重载逻辑专家，复制成多个物理副本
        │      每个副本的期望负载 ≈ w_e / 副本数 c_e
        │
        ├─ ② 决定放置：把所有物理专家装箱到 GPU
        │      约束：每 GPU 恰好 num_replicas / num_gpus 个专家
        │      目标：各 GPU 负载之和尽量接近
        ▼
输出：放置方案（物理专家 → GPU 槽位 → 逻辑专家 的映射）
```

复制之后，GPU \( g \) 的负载公式从 \( \sum_{e \in g} w_e \) 变为：

\[ L_g = \sum_{\text{副本 } r \,\in\, g} \frac{w_{e(r)}}{c_{e(r)}} \]

其中 \( e(r) \) 是副本 \( r \) 对应的逻辑专家，\( c_{e(r)} \) 是该逻辑专家的副本总数。直觉：复制谁最划算？——复制"当前每份期望负载最高"的专家，拉平所有副本的期望负载。这个直觉在第二单元精读 `replicate_experts` 时会被逐行验证（[eplb.py:L44-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71)），本讲只需记住结论。

#### 4.2.3 源码精读

README 用一句话概括了整个思路：

> As described in the DeepSeek-V3 paper, we adopt a **redundant experts** strategy that duplicates heavy-loaded experts. Then, we heuristically pack the duplicated experts to GPUs to ensure load balancing across different GPUs.

见 [README.md:L5-L6](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L5-L6)。两个关键词对应该模块的两个动作：`duplicates heavy-loaded experts`（复制重载专家）与 `heuristically pack`（启发式打包）。

README 的示例给了具体数字：两层 MoE、每层 12 个专家，引入 4 个冗余专家，共 16 个副本放到 2 节点 × 4 GPU 上（[README.md:L37](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37)）。也就是说：**12 逻辑专家 + 4 冗余 = 16 物理专家 = 8 GPU × 每 GPU 2 个**。`num_replicas` 参数指物理专家总数（不是"额外副本数"），且必须能被 `num_gpus` 整除——入口函数文档写明 "num_replicas: number of physical experts, must be a multiple of num_gpus"（[eplb.py:L137-L138](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L137-L138)）。

#### 4.2.4 代码实践

**实践目标**：亲手体会"复制 + 打包"能把不均衡度压低多少。

**操作步骤**（纸笔推演）：

1. 设 4 个逻辑专家、负载 \( w = [10, 9, 8, 1] \)，2 个 GPU，物理专家总数扩到 6 个（每 GPU 恰好 3 个）。
2. **先不复制**：把 4 个专家分成两组（各组数量不必等），找出最好的分法与对应的 \( \rho \)。
3. **再复制**：用"复制当前每份期望负载最高的专家"的直觉，决定复制哪两个专家（各得 2 份副本），写出 6 个物理副本的期望负载，再手工把它们装到 2 个 GPU（每 GPU 3 个，重的先进轻的包）。
4. 比较两种情形的 \( \rho \)。

**需要观察的现象**：不复制时，最好的分法是 \(\{10, 8\}\) vs \(\{9, 1\}\)，负载 18 与 10，\( \rho = 18/14 \approx 1.29 \)。

**预期结果**：复制 10 和 9（各拆成两份 5、5 与 4.5、4.5）后，6 个副本负载为 \([8, 5, 5, 4.5, 4.5, 1]\)。一种手工装箱结果：GPU0 = {8, 4.5, 1} = 13.5，GPU1 = {5, 5, 4.5} = 14.5，\( \rho = 14.5/14 \approx 1.04 \)。注意启发式答案不唯一（比如 13.5 与 14.5 对调同样成立），这正是"启发式"的含义。

（本实践为纸笔推演，数字可自行验算；无需本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：复制专家会增加显存与参数量，为什么仍然划算？
**答案**：瓶颈 GPU 决定整体步时。冗余副本把最重专家的负载摊薄，直接降低 \( \max_g L_g \)；代价是 \((num\_replicas - num\_logical)\) 份额外专家权重。只要瓶颈收益大于显存代价（例如示例中多存 4/12 ≈ 33% 的专家权重），就值得。

**练习 2**：逻辑专家和物理专家的区别是什么？
**答案**：逻辑专家是模型结构里的第 \( e \) 个专家（编号 0..11）；物理专家是部署时实际存在的副本实例（编号 0..15）。一个逻辑专家可以对应多个物理专家。两者的映射关系正是 EPLB 输出的核心内容（见 4.4 节）。

**练习 3**：示例中冗余名额有几个？如果 `num_replicas` 设成 12 会怎样？
**答案**：每层 16 − 12 = 4 个冗余名额（[README.md:L37](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37)）。`num_replicas = 12` 等于逻辑专家数，即"关闭复制、只做装箱"的基线情形（代码中 `replicate_experts` 的循环 `for i in range(num_log, num_phy)` 一次都不执行，见 [eplb.py:L66](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66)）。

### 4.3 两种策略：hierarchical 与 global

#### 4.3.1 概念说明

只看 GPU 负载还不够——**跨节点通信**是另一项成本。背景：DeepSeek-V3 使用**组受限专家路由（group-limited expert routing）**：专家被划分为若干**组（group）**，路由时限制每个 token 从单个组里最多选中若干个专家（论文背景知识，本仓库不实现路由）。推论：一个 token 需要的专家往往集中在少数组里。于是，**如果把同一组的专家放在同一个节点内**，token 的跨节点传输就大为减少。

由此产生两种策略（README 原文 "two policies used for different cases"，[README.md:L17](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L17)）：

| | **Hierarchical（层级）** | **Global（全局）** |
| --- | --- | --- |
| 使用条件 | 专家组数能被节点数整除（`num_groups % num_nodes == 0`） | 其余情况 |
| 是否考虑分组 | 考虑：同组专家尽量放同一节点 | 不考虑：无视分组，全局复制 |
| 流程 | ① 组打包到节点 → ② 节点内复制专家 → ③ 物理专家打包到 GPU | 全局复制后直接打包到 GPU |
| 典型场景 | prefill 阶段、较小的 EP 规模 | decode 阶段、较大的 EP 规模 |

为什么两个阶段对应不同策略？prefill 处理整段 prompt，EP 规模较小（GPU 少、节点少），组数与节点数的整除关系容易满足，可以享受组对齐的通信红利；decode 逐 token 生成，通常用更大的 EP 规模（更多节点），整除关系难以满足，只能退而求其次做全局均衡。README 对两种策略的完整描述见 [README.md:L19-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L19-L25)（hierarchical）与 [README.md:L27-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L27-L31)（global）。

#### 4.3.2 核心流程

策略分派的判定流程：

```text
输入 num_groups（专家组数）、num_nodes（节点数）
        │
        ├── num_groups % num_nodes == 0 ?
        │        │
        │        ├─ 是 → hierarchical 策略
        │        │        Step 1: 组打包到节点（节点间负载均衡）
        │        │        Step 2: 节点内冗余复制
        │        │        Step 3: 物理专家打包到 GPU（节点内负载均衡）
        │        │
        │        └─ 否 → global 策略
        │                 无视分组，全局复制 + 直接打包到 GPU
        ▼
输出放置方案（两条路径的返回值格式完全相同）
```

值得注意的设计：global 策略在代码里**没有独立实现**，而是把 `rebalance_experts_hierarchical` 的参数退化为 `(num_groups=1, num_nodes=1, num_gpus=num_gpus)` 来复用——"1 个组、1 个节点"自然没有组约束和节点约束，层级流程就退化成了全局复制 + GPU 打包。这个精巧的复用是 [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156) 的内容，第二单元 u2-l6 会专门分析，本讲先留个印象。

#### 4.3.3 源码精读

README 中关于组对齐动机的原话：

> Moreover, thanks to the **group-limited expert routing** used in DeepSeek-V3, we also attempt to place the experts of the same group to the same node to reduce inter-node data traffic, whenever possible.

见 [README.md:L7-L8](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L7-L8)。三个信息点：组受限路由是前提（thanks to）、动作是同组专家放同节点、目的是减少跨节点流量。

hierarchical 策略的三步流程，README 描述为：

> We first pack the expert groups to nodes evenly, ensuring the loads of different nodes are balanced. Then, we replicate the experts within each node. Finally, we pack the replicated experts to individual GPUs to ensure different GPUs are load-balanced.

见 [README.md:L21-L24](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L21-L24)。global 策略则"复制时不看组"：

> In other cases, we use the global load balancing policy that replicates the experts globally regardless of expert groups, and pack the replicated experts to individual GPUs.

见 [README.md:L29-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L29-L31)。

代码中的分派逻辑与 README 描述一一对应（这段是预告，第二单元才逐行精读）：

```python
if num_groups % num_nodes == 0:
    # use hierarchical load-balance policy
    phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas,
                                                              num_groups, num_nodes, num_gpus)
else:
    # use global load-balance policy
    phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

见 [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)：整除则走层级策略；否则以退化参数 `(1, 1, num_gpus)` 调用同一个函数实现全局策略。

#### 4.3.4 代码实践

**实践目标**：能根据部署参数判断该走哪条策略分支。

**操作步骤**（判断题，只需心算整除关系）：

对下列三种部署，判断 `rebalance_experts` 会走 hierarchical 还是 global，并说明理由：

1. prefill：`num_groups=8`，`num_nodes=2`，`num_gpus=8`（每节点 4 GPU）。
2. decode：`num_groups=8`，`num_nodes=3`，`num_gpus=24`。
3. 单机：`num_groups=4`，`num_nodes=1`，`num_gpus=8`。

**需要观察的现象**：判定只依赖 `num_groups % num_nodes` 是否为 0，与 `num_gpus` 大小无关。

**预期结果**：

1. `8 % 2 == 0` → hierarchical（典型 prefill 场景，与 [README.md:L24-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L24-L25) 的描述一致）。
2. `8 % 3 != 0` → global（典型 decode 大 EP 场景，[README.md:L29-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L29-L31)）。
3. `4 % 1 == 0` → hierarchical（单节点下层级策略自动退化为"节点内复制 + GPU 打包"，没有跨节点问题，组对齐自然满足）。

（本实践为判断推演；若想用代码确认，可在安装 PyTorch 后运行入口函数观察输出——待本地验证，见 u1-l3。）

#### 4.3.5 小练习与答案

**练习 1**：README 说 hierarchical 可用于 prefill、global 可用于 decode，这是"硬性绑定"吗？
**答案**：不是。代码里唯一的硬性条件是 `num_groups % num_nodes == 0`（[eplb.py:L150](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150)）。prefill/decode 只是两种典型的拓扑差异：prefill 的 EP 规模小、整除关系常满足；decode 的 EP 规模大、常不满足。README 用的是 "can be used / can be adopted"（[README.md:L24-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L24-L25)、[README.md:L30-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L30-L31)），是经验适配而非强制。

**练习 2**：为什么"同组专家放同一节点"能减少跨节点流量？
**答案**：组受限路由限制每个 token 从单组选取的专家数上限，使得一个 token 访问的专家集中在少数组。若整组专家都在同一节点内，这些 token 的专家计算就只需一次跨节点传输到该节点，其余通信走节点内高速互联（NVLink 等，见 [eplb.py:L81](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L81) 的参数注释）。

**练习 3**：global 策略为什么要"无视分组"？
**答案**：当组数不被节点数整除时，无法把完整的组对齐到节点（总有组被拆开或节点间组数不均）。强行对齐反而制造约束，于是 global 干脆放弃组信息，以"所有 GPU 全局负载均衡"为唯一目标（[README.md:L29-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L29-L31)）。

### 4.4 从示例输出读懂放置方案

#### 4.4.1 概念说明

EPLB 的一切结论最终浓缩在入口函数的输出里。`rebalance_experts` 返回三个张量（[README.md:L51](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L51)，完整接口说明见 [README.md:L33-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L33-L57)）。本讲只需要看懂第一个：`phy2log`。

`phy2log`（physical to logical）是一张"**物理槽位 → 逻辑专家编号**"对照表，形状 `[层数, num_replicas]`：

- **行**：MoE 层编号（示例 2 层）。注意**每一层有自己独立的放置方案**——同一逻辑专家在不同层可能被复制到不同位置。
- **列/元素**：第 \( j \) 个物理槽位上放的是哪个逻辑专家。若某个逻辑专家编号出现 2 次，说明它被复制成 2 份。

槽位到 GPU 的对应关系：示例中 16 个物理专家、8 个 GPU，每 GPU 恰好 `num_replicas / num_gpus = 2` 个槽位（代码依据 `phy_experts_per_gpu = num_physical_experts // num_gpus`，[eplb.py:L94-L96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L94-L96)），槽位按"GPU 序号 × 每 GPU 槽位数 + 槽内序号"连续编码（代码依据 [eplb.py:L118-L120](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118-L120)，细节在 u2-l5 精读）。因此本例的解读规则是：

\[ \text{槽位 } j \;\in\; \text{GPU } \lfloor j / 2 \rfloor, \qquad \text{GPU } g \;\in\; \text{节点 } \lfloor g / 4 \rfloor \]

即槽位 0–1 → GPU0，槽位 2–3 → GPU1，……；GPU 0–3 → 节点 0，GPU 4–7 → 节点 1。

#### 4.4.2 核心流程

解读 `phy2log` 输出的固定套路：

```text
拿到 phy2log [2, 16]
        │
        ├─ ① 按每 GPU 槽位数（= num_replicas / num_gpus = 2）切块
        │      得到每层"GPU → 专家集合"表
        │
        ├─ ② 按 GPU 数 / 节点数（= 4）把 GPU 分块
        │      得到"节点 → GPU → 专家集合"表
        │
        ├─ ③ 在一行内统计每个逻辑专家编号出现的次数
        │      出现 2 次的即被复制的专家（其两个副本所在 GPU 一目了然）
        │
        └─ ④ 检查副本位置：同一专家的多个副本应在同一节点（hierarchical 策略下）
```

#### 4.4.3 源码精读

README 示例的输入与输出（真实运行结果，摘自 [README.md:L43-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L43-L57)）：

``` python
weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                       [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])

num_replicas = 16
num_groups = 4
num_nodes = 2
num_gpus = 8

phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)
print(phy2log)

# Output:
# tensor([[ 5,  6,  5,  7,  8,  4,  3,  4, 10,  9, 10,  2,  0,  1, 11,  1],
#         [ 7, 10,  6,  8,  6, 11,  8,  9,  2,  4,  5,  1,  5,  0,  3,  1]])
```

这段代码（[README.md:L39-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L39-L57)）中：`weight` 的第 0 行是第 0 层 12 个专家的负载统计，第 1 行是第 1 层的；`num_groups=4` 说明 12 个专家分成 4 组（每组 3 个：专家 0–2、3–5、6–8、9–11）；`4 % 2 == 0`，所以走 hierarchical 策略。

按 4.4.2 的套路解读第 0 层 `[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]`：

| 节点 | GPU | 槽位 | 专家（负载） | GPU 负载 |
| --- | --- | --- | --- | --- |
| 0 | GPU0 | 0, 1 | 5 (165), 6 (39) | 204 |
| 0 | GPU1 | 2, 3 | 5 (165), 7 (4) | 169 |
| 0 | GPU2 | 4, 5 | 8 (73), 4 (104) | 177 |
| 0 | GPU3 | 6, 7 | 3 (61), 4 (104) | 165 |
| 1 | GPU4 | 8, 9 | 10 (183), 9 (56) | 239 |
| 1 | GPU5 | 10, 11 | 10 (183), 2 (40) | 223 |
| 1 | GPU6 | 12, 13 | 0 (90), 1 (132) | 222 |
| 1 | GPU7 | 14, 15 | 11 (86), 1 (132) | 218 |

（负载取自第 0 层 `weight`，逐项可验算。）可以读出三个关键事实：

1. **每层复制了 4 个逻辑专家**（16 − 12 = 4 个冗余名额全部用上）：层 0 被复制的是专家 5、4（节点 0）与专家 10、1（节点 1）。
2. **副本成对出现在同一节点**：专家 5 的两份在 GPU0/GPU1，专家 4 在 GPU2/GPU3，专家 10 在 GPU4/GPU5，专家 1 在 GPU6/GPU7——正是 hierarchical 策略"节点内复制"的直接体现。
3. **层数据独立**：第 1 层 `[7, 10, 6, 8, 6, 11, 8, 9, 2, 4, 5, 1, 5, 0, 3, 1]` 中被复制的是专家 6、8（节点 0）与专家 5、1（节点 1）——层 0 里专家 5 在节点 0 复制，层 1 里却换到了节点 1，因为两层的负载分布不同。

顺带一提，用这张表还能算出层 0 的不均衡度：\( \bar{L} = 1617/8 \approx 202.1 \)（1617 是 8 个 GPU 负载之和，即复制后的总物理负载），\( \rho = 239/202.1 \approx 1.18 \)。而第 1 层同样方法可算出 \( \rho \approx 1.58 \)（GPU1 负载 359，均值 227.4）——**启发式并不保证最优**，且均衡质量与负载分布有关，这个观察会在 u3-l2 的评估实践中正式展开。

README 最后说，示例输出对应的复制与放置方案画在了 `example.png` 里（[README.md:L59-L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L59-L62)），读者可以拿上面两张表与图核对。

#### 4.4.4 代码实践

**实践目标**：不运行任何代码，直接从 README 打印的输出张量中提取出完整的放置方案。

**操作步骤**：

1. 打开 [README.md:L54-L56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L54-L56)，抄下两行输出。
2. 用"槽位 \( j \) → GPU \( \lfloor j/2 \rfloor \)、GPU \( g \) → 节点 \( \lfloor g/4 \rfloor \)"的规则，像 4.4.3 那样为**第 1 层**整理出"节点 / GPU / 专家"表。
3. 在第 1 层的表里圈出出现两次的逻辑专家编号，记录每对副本所在的 GPU 与节点。
4. （可选）若已安装 PyTorch，把 [README.md:L39-L57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L39-L57) 的代码存成脚本运行，确认 `print(phy2log)` 与 README 一致（待本地验证；完整环境搭建见 u1-l3）。

**需要观察的现象**：第 1 层同样恰好有 4 个编号各出现两次；每对副本落在同一节点的不同 GPU 上。

**预期结果**（第 1 层参考表，可自行核对）：

| 节点 | GPU | 专家 | 被复制的专家及其副本位置 |
| --- | --- | --- | --- |
| 0 | GPU0 | 7, 10 | 专家 6 → GPU1、GPU2 |
| 0 | GPU1 | 6, 8 | 专家 8 → GPU1、GPU3 |
| 0 | GPU2 | 6, 11 |  |
| 0 | GPU3 | 8, 9 |  |
| 1 | GPU4 | 2, 4 | 专家 5 → GPU5、GPU6 |
| 1 | GPU5 | 5, 1 | 专家 1 → GPU5、GPU7 |
| 1 | GPU6 | 5, 0 |  |
| 1 | GPU7 | 3, 1 |  |

#### 4.4.5 小练习与答案

**练习 1**：第 1 层输出中，槽位 13 在哪个 GPU、哪个节点？放的是哪个逻辑专家？
**答案**：GPU \( \lfloor 13/2 \rfloor = 6 \)，节点 \( \lfloor 6/4 \rfloor = 1 \)，对应第 1 层输出的第 13 个元素 `0`，即逻辑专家 0。

**练习 2**：只看第 0 层输出，逻辑专家 11 有几个副本？依据是什么？
**答案**：1 个。第 0 层 16 个元素里 `11` 只出现一次（槽位 14）；而 `5`、`4`、`10`、`1` 各出现两次。每行出现次数总和恒等于 16。

**练习 3**：为什么每行恰好有 4 个"重复"？
**答案**：物理专家数 16 减逻辑专家数 12 = 4 个冗余名额，每个名额让某个逻辑专家多一份副本。由于初始每专家 1 份（共 12 份），加上 4 份冗余共 16 份，恰好填满 16 个槽位。

## 5. 综合实践

**任务**：阅读 README，观察 `example.png` 中的放置方案与示例输出，标出哪些逻辑专家被复制了、它们的副本分别落在哪些节点和 GPU 上，并用自己的话写一段问题陈述（problem statement）。

**步骤**：

1. 通读 [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) 全文（约 60 行），重点是 [L3-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L3-L13)（问题与思路）、[L15-L31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L15-L31)（两种策略）、[L33-L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L33-L62)（示例）。
2. 按 4.4 的解读规则，为两层输出分别整理"被复制专家 → 副本所在节点与 GPU"清单。
3. 打开 [example.png](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/example.png)（README 说明该图展示的就是这份复制与放置方案，[README.md:L59-L62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L59-L62)），对照你整理的清单逐项核对。
4. 写一段 5–8 句的问题陈述，覆盖：输入是什么、输出是什么、约束是什么（每 GPU 槽位数固定、副本尽量同节点）、目标是什么（最小化不均衡度 \( \rho \) 与跨节点流量）。

**参考答案**（基于 README 打印的 `phy2log`，可自行验算；与 `example.png` 图形内容的一致性请在查看图片时核对）：

| 层 | 被复制专家 | 副本所在节点 | 副本所在 GPU |
| --- | --- | --- | --- |
| 0 | 5 | 节点 0 | GPU0、GPU1 |
| 0 | 4 | 节点 0 | GPU2、GPU3 |
| 0 | 10 | 节点 1 | GPU4、GPU5 |
| 0 | 1 | 节点 1 | GPU6、GPU7 |
| 1 | 6 | 节点 0 | GPU1、GPU2 |
| 1 | 8 | 节点 0 | GPU1、GPU3 |
| 1 | 5 | 节点 1 | GPU5、GPU6 |
| 1 | 1 | 节点 1 | GPU5、GPU7 |

**问题陈述示例**：给定一个两层 MoE 模型每层 12 个专家的历史负载统计，以及 2 节点 × 4 GPU、每节点 4 组专家的部署拓扑，求一个"逻辑专家 → 物理副本 → GPU 槽位"的放置方案，使得：物理专家总数为 16（每 GPU 恰好 2 个）；重载专家被复制以摊薄负载（每层 4 个冗余名额）；hierarchical 策略下同一专家的副本不跨节点、同组专家尽量聚在同一节点；目标是各 GPU 负载尽量均衡（层 0 实测 \( \rho \approx 1.18 \)）。

## 6. 本讲小结

- **问题**：专家并行下不同专家的负载随 workload 变化且差异巨大，"每 GPU 专家数相同"不等于负载均衡；同步系统中整体速度被最重 GPU 锁死（木桶效应）。
- **思路**：DeepSeek-V3 的冗余专家策略——复制重载专家摊薄其负载（12 逻辑 + 4 冗余 = 16 物理），再启发式打包到 GPU，使各 GPU 总负载尽量接近。
- **两种策略**：`num_groups % num_nodes == 0` 时走 hierarchical（组打包到节点 → 节点内复制 → GPU 打包，适配 prefill 小 EP）；否则走 global（无视分组全局复制，适配 decode 大 EP），代码里 global 是用退化参数 `(1, 1, num_gpus)` 复用 hierarchical 实现的。
- **读懂输出**：`phy2log[l][j]` 表示第 \( l \) 层第 \( j \) 个物理槽位放哪个逻辑专家；槽位 \( j \) 在 GPU \( \lfloor j/2 \rfloor \)（本例每 GPU 2 槽），同一编号出现两次即被复制。
- **边界**：EPLB 只负责"负载估计 → 放置方案"这一步；负载预测（滑动平均）与权重重排都由外部系统完成（[README.md:L10-L13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)）。
- **一个伏笔**：启发式不保证最优——同一算法下层 0 的 \( \rho \approx 1.18 \)、层 1 的 \( \rho \approx 1.58 \)，均衡质量如何量化与改进是第三单元的话题。

## 7. 下一步学习建议

- **下一讲（u1-l2）**：补齐领域背景——MoE 层与逻辑/物理专家的精确关系、专家并行的通信模式、DeepSeek-V3 组受限路由如何引出"同组专家放同节点"的约束，以及 prefill/decode 的 EP 规模差异。
- **然后（u1-l3）**：动手搭建环境、实际运行 README 示例，并弄清 `rebalance_experts` 返回的另外两个张量 `log2phy`（含 `-1` 填充）和 `logcnt` 的语义——本讲只用了 `phy2log`。
- **再看（u1-l4）**：建立 `eplb.py` 四个函数（`balanced_packing`、`replicate_experts`、`rebalance_experts_hierarchical`、`rebalance_experts`）的代码地图与张量形状速查表，为第二单元的逐行精读做准备。
- 若想先睹算法全貌，可直接浏览 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py)（约 165 行）；配合本讲建立的"复制 + 打包 + 组对齐"三个关键词阅读，入口 `rebalance_experts`（[eplb.py:L131-L162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162)）已经可以看懂大半。
