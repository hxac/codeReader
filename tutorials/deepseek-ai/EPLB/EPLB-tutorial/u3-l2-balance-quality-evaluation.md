# 负载均衡质量评估：指标、下界与实验

## 1. 本讲目标

上一讲（u3-l1）我们解决了「方案对不对」：七条不变量断言保证 `rebalance_experts` 的输出在结构上合法。本讲解决「方案好不好」：

1. 会用 `weight` 与 `logcnt` 实现每 GPU 负载的计算函数——这是评估一切均衡质量的地基，而且它的负载模型直接来自源码里的一行除法。
2. 会定义均衡度指标（max/mean、max/min、标准差），并推导理论下界（总负载 / GPU 数），理解层级策略还额外受「节点配平」约束，因此有更紧的条件下界。
3. 能在均匀、长尾、真实感三种负载分布 × hierarchical / global 两种策略 × 多个冗余数量下跑对照实验，并解释：长尾分布下冗余收益最大；层级策略「组打包到节点」的约束付出明显的均衡代价。

一句话：u3-l1 是验收员，本讲让你变成评委。

## 2. 前置知识

- **结构正确性 vs 均衡质量**（承接 u3-l1）：不变量断言只回答「输出是不是一张合法的放置方案」，不回答「这张方案均衡不均衡」。质量必须另建指标、另做实验，两者不可混用——断言全过的方案，均衡度可能很差。
- **三张映射表**（承接 u2-l6）：入口返回 `phy2log`（物理槽位→逻辑专家）、`log2phy`（反向表，第三维长为最大副本数，空位填 -1）、`logcnt`（每个逻辑专家的副本数）。本讲的负载计算只用 `phy2log` 和 `logcnt`。
- **副本均分假设**（承接 u2-l2）：路由器把发往某逻辑专家的 token 流量在其全部副本间均分。因此一个逻辑专家的副本数翻倍，单个副本的期望负载减半；**复制只摊薄单副本负载，不减少总计算量**（承接 u1-l2）。
- **木桶效应**（承接 u1-l1）：一层的前向时间被最重的 GPU 锁死，所以 `max`（而非平均值）才是我们要压的量。
- **槽位编码**（承接 u2-l5 / u1-l3）：`phy2log` 的物理槽位号按「节点 → 节点内 GPU → 槽内位置」混合进制编码，来源是 [eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 的 `pack_index * phy_experts_per_gpu + rank_in_pack`。这决定了负载计算可以用一个 `view` 还原图层。
- **策略分派**（承接 u2-l6）：`num_groups % num_nodes == 0` 走层级策略，否则走全局策略（以退化参数 `(num_replicas, 1, 1, num_gpus)` 复用同一实现）。策略**无法显式选择**，这是本讲实验设计的关键约束。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | 全部算法实现。本讲反复引用五处：L104（组负载聚合）、L117（每副本负载 = 组内负载 ÷ 副本数，负载模型的源码依据）、L119（GPU 槽位编码）、L112-113（节点内复制预算）、L150-156（策略分派） |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 问题陈述（L3-8）、两种策略的定位（L21-31）、两层 12 专家的金标准示例（L37-57）——本讲的手算例与实验配置都从这里出发 |

## 4. 核心概念与源码讲解

### 4.1 每副本负载模型：从源码里的一行除法说起

#### 4.1.1 概念说明

要评估均衡度，第一步是回答「一张放置方案下，每张 GPU 到底背多少负载」。这需要一个负载模型，而模型不必发明——**算法自己在 Step 3 装箱前就算过一遍**：把每个逻辑专家的负载除以副本数得到单副本负载，再装箱。我们在算法外部用同样的公式，就能复算每张 GPU 的负载。

记第 \(l\) 层逻辑专家 \(e\) 的负载统计为 \(w_{l,e}\)（即输入 `weight`），其副本数为 \(c_{l,e}\)（即 `logcnt`）。副本均分下，单副本期望负载为：

\[ \hat{w}_{l,e} = \frac{w_{l,e}}{c_{l,e}} \]

#### 4.1.2 核心流程

1. 对每个物理槽位 \(s\)，查出它承载的逻辑专家 `phy2log[l, s]`，得到该槽位负载 \(\hat w_{l,\,\mathrm{phy2log}[l,s]}\)；
2. 按 GPU 聚合：物理槽位号 = 节点号 × (M/N) + 节点内 GPU 号 × e + 槽内序号（e 为每 GPU 专家数），因此一次 `view(L, num_gpus, e)` 加 `sum(-1)` 即得每 GPU 负载；
3. 需要节点级负载时，再按 `view(L, num_nodes, -1)` 聚合（仅层级策略有节点语义）。

#### 4.1.3 源码精读

**依据一：每副本负载的除法。** [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117) 在把物理专家装箱到 GPU 之前，先把每个 mlog 专家的负载除以副本数 `mlogcnt`，再按 `phy2mlog` 换序到物理槽位：

```python
tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
```

这就是「副本均分流量」假设的源码化——算法用 \(\hat w\) 而非 \(w\) 做装箱权重。我们的评估函数在入口外部做完全一样的事，只是改用对 `logcnt`（已映射回原逻辑编号）取商。

**依据二：槽位编码。** [eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 把「哪张 GPU、第几槽」编码进槽位号：`phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`；随后 [eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125) 再加上节点基地址。两级编码叠加后，`phy2log` 的最后一维天然是「节点 → GPU → 槽内」的行主序，`view` 即可无损还原。

**依据三：复制守恒。** [eplb.py:66-70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L70) 的复制循环只递增 `logcnt`、不改总负载：某专家 \(c\) 个副本的负载之和为 \(c \cdot (w/c) = w\)，**无论复制多少份，一层的总负载 \(W_l=\sum_e w_{l,e}\) 不变**。这是下一节推导下界的基石。

#### 4.1.4 代码实践

实践目标：实现 `gpu_loads`，并在 README 金标准示例的第一层上复算每 GPU 负载。

操作步骤（示例代码，非项目原有）：

```python
import torch, eplb

def per_replica_load(weight, phy2log, logcnt):
    # 与 eplb.py:117 同一负载模型：单副本负载 = 逻辑负载 / 副本数
    return (weight.float() / logcnt).gather(-1, phy2log)      # [L, M]

def gpu_loads(weight, phy2log, logcnt, num_gpus):
    e = phy2log.size(-1) // num_gpus                            # 每 GPU 物理专家数
    return per_replica_load(weight, phy2log, logcnt).view(weight.size(0), num_gpus, e).sum(-1)

weight = torch.tensor([[90,132,40,61,104,165,39,4,73,56,183,86]])
phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, 16, 4, 2, 8)
print(gpu_loads(weight, phy2log, logcnt, 8))
```

需要观察的现象与预期结果：下面是按算法逐步**手算**得到的第 0 层结果（节点 0 分到组 {g1,g2}，负载 446；节点 1 分到组 {g0,g3}，负载 587；两节点各自复制到 8 个物理专家后装箱）：

| GPU（全局编号） | 负载 | 归属节点 |
| --- | --- | --- |
| 0 | 121.5 | 节点 0 |
| 1 | 86.5 | 节点 0 |
| 2 | 125.0 | 节点 0 |
| 3 | 113.0 | 节点 0 |
| 4 | 147.5 | 节点 1 |
| 5 | 131.5 | 节点 1 |
| 6 | 156.0 | 节点 1 |
| 7 | 152.0 | 节点 1 |

例如 GPU0 的两个槽位承载逻辑专家 5、6（见 [README.md:55](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55) 的输出行 `[5, 6, ...]`），负载 = 165/2 + 39/1 = 82.5 + 39 = 121.5。手算的 `phy2log` 已与 README 打印的金标准输出逐槽位核对一致，因此这张表可作为脚本的预期值；请以脚本输出复核（手算过程本身可复算验证，具体打印格式待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`gpu_loads` 里为什么可以直接 `view(L, num_gpus, e)`，不需要先知道 `num_nodes`？

**答案**：槽位编码是两级混合进制：全局槽位 = 节点号 × (M/N) + 节点内 GPU 号 × e + 槽内序号（[eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 与 [eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125) 合成）。节点块大小 (M/N) 恰等于 (P/N)·e，所以「节点 → GPU → 槽内」的行主序与「全局 GPU → 槽内」的行主序完全一致，合并层级行不行都对；只有要按**节点**聚合时才需要 `num_nodes`。

**练习 2**：全局策略（内部以 `(M, 1, 1, P)` 调用层级实现，[eplb.py:156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L156)）的输出还能用 `view(L, num_nodes, -1)` 算节点负载吗？

**答案**：不能给出有物理意义的节点负载。全局分支内部 `num_nodes=1`，槽位只按「GPU → 槽内」编码，专家副本可以落在任意节点上；从入口外部按真实集群的 `num_nodes` 去 view，切出来的块与真实节点边界无关。节点级分析只对层级策略（或已知内部布局）成立。

### 4.2 均衡度指标与理论下界：给「好不好」装上标尺

#### 4.2.1 概念说明

有了每 GPU 负载 \(L_{l,g}\)，均衡质量要压缩成少数几个数字。我们关心三件事：

- **最重 GPU 有多重**（木桶短板，直接决定层延迟）；
- **与理想均分的差距**（浪费率）；
- **分布的离散程度**（是否存在特别闲的 GPU，暗示放置被约束卡死）。

同时，光有指标没有参照系会误判：max/mean = 1.15 好不好？要看它**离理论下界还有多远**。下界告诉我们：给定负载和约束，任何算法都不可能做得更好——与下界的差距才是算法（或约束）的责任。

#### 4.2.2 核心流程

一层内（省略层下标 \(l\)），设 GPU 数为 \(P\)、节点数 \(N\)、层级策略中节点 \(n\) 分到的逻辑专家集合为 \(S_n\)、其总负载 \(W_n=\sum_{e\in S_n} w_e\)、全层总负载 \(W=\sum_e w_e\)：

1. **主指标 max/mean（不均衡度）**：

\[ \mathrm{IB} = \frac{\max_g L_g}{\frac{1}{P}\sum_g L_g} = \frac{P \cdot \max_g L_g}{W} \]

   分母由复制守恒恒等于 \(W/P\)，与放置方案无关，因此 IB 只由 max 决定，且恒 ≥ 1。层前向时间 ∝ \(\max_g L_g\)，故 **1/IB 就是该层的理论硬件利用率上限**。

2. **辅助指标**：max/min（极差比，对「闲 GPU」敏感）、标准差（整体离散度）。

3. **全局策略的下界**：总量守恒给出

\[ \max_g L_g \ \ge\ \frac{W}{P} \quad\Longrightarrow\quad \mathrm{IB} \ge 1 \]

4. **层级策略的条件下界**：节点 \(n\) 的 \(P/N\) 张 GPU 上的全部物理槽位都映射到 \(S_n\)（Step 1 分组决定，Step 2 复制不搬运跨节点负载），且该节点全部副本负载之和 = \(W_n\)（复制守恒），所以

\[ \max_g L_g \ \ge\ \max_n \frac{W_n}{P/N} \quad\Longrightarrow\quad \mathrm{IB}_{\mathrm{hier}} \ \ge\ \max\!\Big(1,\ \frac{\max_n W_n / (P/N)}{W/P}\Big) \]

5. **代价分解**：层级策略的 IB 可拆成两个因子的乘积——

\[ \mathrm{IB}_{\mathrm{hier}} = \underbrace{\frac{\max_g L_g}{\max_n W_n/(P/N)}}_{\text{节点内：复制 + 装箱损失}} \times \underbrace{\frac{\max_n W_n/(P/N)}{W/P}}_{\text{节点配平损失（组粒度约束）}} \]

#### 4.2.3 源码精读

- **节点负载 \(W_n\) 由 Step 1 决定**：[eplb.py:104](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104) 用 `unflatten+sum` 把专家负载聚合成组负载，[eplb.py:105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L105) 用 `balanced_packing` 把组装箱到节点——此后 \(W_n\) 被冻结。
- **复制不跨节点搬运负载**：[eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113) 把每层负载 `gather` 成 `[num_layers*num_nodes, E/N]` 后按节点独立调用 `replicate_experts`，每节点物理预算固定为 `M/N`——重载节点**不能**向轻载节点借副本名额，这是条件下界的根源。
- **为什么必须先配平再复制**（承接 u2-l4）：若 Step 1 就把 587 的负载锁进某节点，后续任何操作都无法弥补——这个 587/4 = 146.75 会成为该节点所有 GPU 的平均水位，压着整层的 max。
- README 对两种策略定位的描述见 [README.md:21-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L21-L31)：层级策略「先组到节点、再节点内复制、再装 GPU」。

#### 4.2.4 代码实践

实践目标：在 README 示例第 0 层上计算全部指标与两级下界，并完成代价分解。

操作步骤（示例代码）：先算层级策略的负载（4.1.4 的表），再手算/复算下界：

- \(W\) = 90+132+40+61+104+165+39+4+73+56+183+86 = 1033，\(W/P\) = 1033/8 = **129.125**；
- 组负载：g0=262（专家 0-2）、g1=330（专家 3-5）、g2=116（专家 6-8）、g3=325（专家 9-11）；Step 1 装箱得节点 0 = {g1,g2} = 446、节点 1 = {g0,g3} = 587；条件下界 = 587/4 = **146.75**。

预期结果（手算，脚本可复核）：

| 量 | 层级策略（G=4） |
| --- | --- |
| 每 GPU 负载 | [121.5, 86.5, 125, 113, 147.5, 131.5, 156, 152] |
| max / min / mean | 156 / 86.5 / 129.125 |
| max/mean | **1.208** |
| max/min | **1.803** |
| 标准差 | ≈ 21.6（总体口径；注意 `torch.std` 默认除以 N-1，得 ≈ 23.1） |
| 代价分解 | 1.208 = 1.063（节点内损失）× 1.137（节点配平损失） |

值得注意的观察：Step 1 的组装箱在这里已达到**约束最优**——4 个组两两划分的三种方案中，最优就是 {330,116}=446 / {325,262}=587（其余为 592/441 与 655/378，max 更大）。也就是说 13.7% 的节点配平损失是**组粒度约束的代价，不是贪心的失败**；贪心（[eplb.py:34-35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L35)）碰巧命中了最优划分。

#### 4.2.5 小练习与答案

**练习 1**：证明层级策略的条件下界 \(\max_g L_g \ge \max_n W_n/(P/N)\)。

**答案**：节点 \(n\) 的 GPU 集合为 \(G_n\)（大小 \(P/N\)）。Step 1 之后，落在这些 GPU 上的物理槽位全部映射到 \(S_n\) 的逻辑专家（组不跨节点，[eplb.py:104-107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L107)）；每个逻辑专家 \(e\) 的全部副本负载和为 \(c_e \cdot (w_e/c_e) = w_e\)（复制守恒）。故 \(\sum_{g \in G_n} L_g = W_n\)，由鸽笼原理 \(\max_{g \in G_n} L_g \ge W_n / (P/N)\)，再对 n 取最大即得。

**练习 2**：为什么说「与下界的差距」比「指标绝对值」更能指导优化方向？

**答案**：差距指认了责任方。贴近条件下界（如上例 1.063 的节点内损失）说明节点内算法已接近极限，继续优化应放宽约束（更细的组粒度、允许跨节点副本）而非改进装箱；远离下界则说明贪心本身有改进空间。只看绝对值无法区分「约束太紧」和「算法太弱」。

### 4.3 对照实验设计：策略无法显式选择，如何公平对比

#### 4.3.1 概念说明

本讲规格要求对比 hierarchical 与 global 两种策略，但入口**没有 policy 参数**——策略由 `num_groups % num_nodes` 的整除性隐式分派。因此对照实验的第一件事是构造参数对：同一份负载、同样的 `num_nodes`/`num_gpus`，只翻转整除性。

同时必须声明**公平性边界**：两种策略优化的目标本来就不完全相同。层级策略额外承担了「同组专家同节点、减少跨节点流量」的义务（[README.md:7-8](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L7-L8)）；全局策略无视分组，等于用跨节点流量换负载均衡。**纯负载指标上 global 占优不代表总体更优**——对比的意义是量化层级策略为流量约束付出了多少负载代价。

#### 4.3.2 核心流程

参数合法性域（全部来自 [eplb.py:90-95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L90-L95) 与 [eplb.py:131-141](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L141) 的断言）：

\[ E \bmod G = 0,\qquad G \bmod N = 0\ (\text{层级}),\qquad P \bmod N = 0,\qquad M \bmod P = 0,\qquad M \ge E \]

主实验网格的构造步骤：

1. 固定集群与规模：`L=4` 层、`E=24` 专家、`N=2` 节点、`P=8` GPU；
2. 策略开关：`G=4`（24%4=0 且 4%2=0 → 层级）vs `G=3`（24%3=0 且 3%2=1 → 全局）；
3. 冗余数量：`M ∈ {24, 32, 40, 48}`，均为 8 的倍数；**M=24=E 即关闭冗余的基线**（E%P=0 使其合法）；
4. 负载分布 × 策略 × M 全组合，同一种子同一份 weight，保证差异只来自策略与冗余；
5. 附加单变量实验：层级策略下扫 `G ∈ {2,4,6,8,12,24}`（组粒度从每组 12 个专家到每组 1 个），观察层级代价随粒度变细的收敛；注意 `G=2` 时 `groups_per_node=1`，Step 1 命中平凡分支（[eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)），节点划分完全由编号硬性决定。

一个现成的小对照（README 示例，E=12、N=2、P=8、M=16）：`G=4` 走层级；改成 `G=3`（12%3=0，3%2=1）即切到全局。同一份第 0 层负载上手算两者：

| 方案 | 每 GPU 负载 | max/mean | max/min |
| --- | --- | --- | --- |
| 层级（G=4） | [121.5, 86.5, 125, 113, 147.5, 131.5, 156, 152] | **1.208** | 1.803 |
| 全局（G=3） | [143.5, 131.5, 142, 142, 121.5, 86.5, 134, 132] | **1.111** | 1.659 |

（全局行同样按 [eplb.py:66-71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L71) 复制：4 个冗余名额依次给专家 10、5、1、4，logcnt 变为 {1:2, 4:2, 5:2, 10:2}，再全局装箱到 8 GPU。手算结果，待脚本复核。）

结论预告：同一负载下，层级策略的最重 GPU 比全局策略重约 9%（156 vs 143.5）——这就是「组打包到节点」约束的可量化代价。

#### 4.3.3 源码精读

**分派逻辑**：[eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)

```python
if num_groups % num_nodes == 0:
    # use hierarchical load-balance policy
    phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas,
                                                              num_groups, num_nodes, num_gpus)
else:
    # use global load-balance policy
    phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

这段代码同时给了实验设计两条铁律：其一，`num_groups` 是唯一的策略开关，但受 `E % G == 0` 限制——E=24、N=2 时能触发全局分支的 G 只有奇因子 3（G=1 退化到无分组也可）；其二，全局分支内部以 `num_nodes=1` 运行，节点信息不会传入，因此实验报告里全局策略只有 GPU 级指标。另注意入口在分派前做了 `weight.float().cpu()` 规范化（[eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149)），评估函数也应对 weight 取 `.float()` 以保持口径一致。

**无冗余基线的合法性**：`num_replicas=E` 要求 `E % P == 0`（[eplb.py:95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95)）。README 的 E=12、P=8 不满足（12%8≠0），最小合法 M 是 16——所以主实验选 E=24，M=24 的零冗余基线才跑得起来。

#### 4.3.4 代码实践

实践目标：写出实验网格的骨架，先小规模跑通并确认策略分派符合预期。

操作步骤（示例代码）：

```python
import torch, eplb

L, E, N, P = 4, 24, 2, 8
weight = torch.rand(L, E, generator=torch.Generator().manual_seed(0)) * 100 + 50

for name, G in [("hier", 4), ("global", 3)]:
    assert (G % N == 0) == (name == "hier")          # 分派条件自检
    for M in (24, 32, 40, 48):
        phy2log, log2phy, logcnt = eplb.rebalance_experts(weight, M, G, N, P)
        # ... 用 4.1 的 gpu_loads 与 4.2 的指标计算并记录
```

需要观察的现象：(1) 两组调用都通过全部断言；(2) 层级方案的副本成对落在同一节点（用 `phy2log.view(L, N, -1)` 检查每个逻辑专家的槽位不跨节点），全局方案无此性质。

预期结果：层级行满足「副本同节点」，全局行不满足；这正是两者语义差异的直接体现（承接 u3-l1 的 INV-7 在全局分支的缺席）。完整网格数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：E=24、N=2、P=8 时，为什么对照实验选 G=4 与 G=3，而不是 G=4 与 G=1？

**答案**：两者都能触发目标分支（4%2=0 层级；3 与 1 对 2 取余均非 0，全局）。但 G=1 意味着没有分组，而 G=3 代表「存在分组但无法与节点对齐」这一真实场景（分组约束存在却放不下），与 G=4 的可比性更强；G=1 更接近「本来就没有组」的另一种工作负载。对照组应只翻转目标变量（整除性），尽量保持其余语义（组是否存在）不变。

**练习 2**：若实验中把两种策略的 M 也设得不同（如层级用 32、全局用 24），结论会出什么问题？

**答案**：冗余预算 M 直接影响均衡度（下节会看到它是主要收益来源），M 不同时策略差异与冗余差异混杂，无法归因。对照实验必须同 weight、同 N、同 P、同 M，唯一变量是 G 的整除性；M 本身应作为第三个实验因子单独扫描。

### 4.4 结果解读：冗余收益、层级代价与基数约束代价

#### 4.4.1 概念说明

实验跑完后，数字要回答三个问题：

- **冗余收益**：M 从 E 增加到更大，均衡度改善多少？收益来自哪里？——来自复制热点专家摊薄其单副本负载（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67) 每轮复制 `weight/logcnt` 最大者，即压最高水位）。
- **层级代价**：层级策略相对全局策略差多少？——来自两层约束：节点配平受组粒度限制（4.2 的条件下界），副本名额不能跨节点调配（[eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113) 每节点预算固定 M/N）。
- **基数约束代价**：即使负载总量能完美均分，「每 GPU 恰好 M/P 个专家」的硬约束（[eplb.py:96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L96)）也会阻止理论最优——一个负载 4 的极轻专家（README 示例中的专家 7）占据一个槽位，只能靠同槽搭配重专家补偿，但它占掉的容量无法用「多放一个专家」弥补。这解释了为什么两种策略的 max/min 都停在 1.66~1.80 而非接近 1。

#### 4.4.2 核心流程

解读实验数据的顺序：

1. 先看零冗余基线（M=E）：此时的 IB 是「纯装箱」水平，也是层级/全局共有的起点；
2. 固定策略，扫 M：画出 IB 随冗余预算的下降曲线，观察收益的边际递减；
3. 固定 M，对比层级 vs 全局：差值即层级代价，可再按 4.2 的公式分解为节点配平损失与节点内损失；
4. 换分布重复：均匀 → 长尾 → 真实感，观察收益对分布形状的敏感度；
5. 每组配置都对照下界：贴近下界说明已到约束极限，远离下界说明算法有改进空间。

#### 4.4.3 源码精读

**冗余收益的来源**：[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67) 的贪心准则 `(weight / logcnt).max(...)` 每轮选出「单副本期望负载最高」的逻辑专家复制一份。直观上这是不断削平最高水位线：均匀分布下所有 `w/c` 几乎相等，复制谁都一样，收益趋近 0；长尾分布下 `w/c` 的最大值远高于均值，复制热点专家立竿见影——所以**长尾是冗余策略的主场**。

**层级代价的两个来源**：

- 组粒度：[eplb.py:104-105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L105) 只能在「组」这个粒度上配平节点。组越少越粗（`groups_per_node` 越小），节点负载的可组合方案越少；极端 `groups_per_node=1` 时装箱完全失效（平凡分支 [eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)），节点负载被编号硬性锁死。
- 副本名额的节点配额：[eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113) 中每节点复制预算固定为 `num_physical_experts // num_nodes`。全局策略可以把第 5 个、第 6 个冗余名额都给同一个跨集群热点；层级策略中该热点所在节点名额用完后就只能复制次热专家。

**README 的实验参考**：[README.md:43-44](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L43-L44) 给的负载本身就是轻度长尾（max 183 vs min 4，max/mean ≈ 1.42），其放置图（[README.md:62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L62) 的 example.png）中专家 5、10 等热点均被复制，可作为「谁被复制」的直观参照。

#### 4.4.4 代码实践

实践目标：在单一配置（E=24、N=2、P=8、长尾分布）下，先做一次「M 扫描 + 策略对照」的迷你实验，为综合实践探路。

操作步骤（示例代码）：

```python
import torch, eplb

# 齐普夫式长尾：负载 ∝ 1/i^0.7，再加每层乘性扰动
base = 1.0 / torch.arange(1, 25).double() ** 0.7
weight = (base.repeat(4, 1) * (0.8 + 0.4 * torch.rand(4, 24, generator=torch.Generator().manual_seed(7)))).float()

for name, G in [("hier", 4), ("global", 3)]:
    for M in (24, 32, 48):
        phy2log, _, logcnt = eplb.rebalance_experts(weight, M, G, 2, 8)
        loads = gpu_loads(weight, phy2log, logcnt, 8)          # 4.1.4 的函数
        print(name, M, (loads.max(-1).values / loads.mean(-1)).mean().item())
```

需要观察的现象：(1) M=24 时两策略 IB 接近（纯装箱，差异只来自层级策略装箱对象是「节点内子问题」）；(2) M 增大时两策略 IB 都下降，但层级下降更慢；(3) 被复制的专家（`logcnt > 1`）集中在负载排序的头部。

预期结果：长尾分布下全局策略各 M 档的 IB 均低于层级策略，且 M=32 相对 M=24 的改善显著大于 M=48 相对 M=32 的改善（边际递减）；具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么均匀分布下增加冗余预算几乎不再改善 max/mean？

**答案**：均匀负载下所有专家的 `weight/logcnt` 几乎相等，复制任何一个专家对最高水位的削减都相同，复制后 `w/c` 立即减半、退出最大值竞争，剩下的仍是「全体同高」的局面；而 max/mean 的下界 1 在零冗余 + 均匀负载时本来就近似达到（装箱粒度即专家粒度）。冗余的收益本质来自负载的**离散度**（热点），分布越平收益越小。

**练习 2**：层级策略中，把 `num_groups` 从 4 提高到 24（E=24，每组 1 个专家），Step 1 会发生什么？层级代价会怎么变？

**答案**：`group_size=1` 后 `tokens_per_group` 就是专家负载本身，Step 1 的「组装箱」退化为「专家级装箱」，节点配平粒度达到最细，节点配平损失（4.2 分解的第二因子）趋近全局装箱水平。但层级代价不会完全消失：每节点复制预算仍固定为 M/N（[eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113)），副本名额依然不能跨节点调配——这是层级策略与全局策略的剩余差异。注意此时分组约束名存实亡（每组 1 个专家，「同组同节点」不再约束任何东西），该配置已偏离层级策略的真实使用场景，只适合作为消融实验。

**练习 3**：max/min 指标中，两种策略的最小值都是 86.5（同一个极轻专家所在 GPU），这说明什么？

**答案**：说明瓶颈在基数约束而非装箱算法：负载 4 的专家必然占据某个槽位，与它同 GPU 的另一个专家无论选谁，该 GPU 总负载都很难填到均值（[eplb.py:96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L96) 的均分槽位约束使「给轻 GPU 多分一个专家」不可行）。max/min 对这类「被约束卡死的闲 GPU」最敏感，是 max/mean 之外有用的补充信号。

## 5. 综合实践

把四个模块串成一个完整的评测脚本 `eval_balance.py`（示例代码，放在 `EPLB-tutorial/` 或任何能 `import eplb` 的目录，不要修改仓库源码）：

1. **负载生成器**：三种分布——`uniform`（100±10 的平坦负载）、`longtail`（齐普夫式 \(w_i \propto 1/i^{0.7}\) 加每层乘性扰动）、`realistic`（平坦基底 + 每层随机 12.5% 的热点专家 ×3.5 倍增载，模拟「热点随 workload 漂移」；README 指出真实负载预测用历史统计的滑动平均，见 [README.md:10-13](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L10-L13)，此处用简化模型代替）；
2. **指标模块**：4.1 的 `per_replica_load`/`gpu_loads` + 4.2 的 `metrics`（max/mean、max/min、std），每层算一次再对层取均值；同时用 `eplb.balanced_packing` 复算层级策略的节点负载 `W_n`（`scatter_add_` 聚合），报告条件下界与实际 IB 的比值；
3. **实验网格**：3 分布 × 2 策略（G=4/G=3）× M ∈ {24, 32, 40, 48}，固定种子，输出 Markdown 表格；
4. **消融实验**：长尾分布下扫层级策略的 G ∈ {2, 4, 6, 8, 12, 24}，验证「组粒度越细、节点配平损失越小」；
5. **正确性护栏**：每行结果先过一遍 u3-l1 的不变量校验器再进表格——保证评的是合法方案。

验收标准（预期趋势，具体数值待本地验证）：

| 对比 | 预期 |
| --- | --- |
| 均匀分布 × 任意策略 × M 递增 | IB 几乎不变，始终接近 1——冗余无热点可摊 |
| 长尾分布 × M: 24→32 | IB 显著下降——冗余收益最大的场景 |
| 长尾分布 × M: 40→48 | 改善明显变缓——边际递减 |
| 任意分布 × 层级 vs 全局（同 M） | 层级 IB 更高；长尾下差值最大——层级代价 |
| 长尾分布 × 层级 × G 递增 | IB 单调向全局策略收敛，但 G=N（平凡分支）时最差 |
| 每行 vs 下界 | 层级策略贴近其条件下界时，说明剩余差距属约束而非算法 |

最后写三句话结论：哪种「分布 × 策略 × M」组合收益最大、层级的均衡代价在哪个分布下最明显、以及如果只允许加一种冗余预算该加多少。

## 6. 本讲小结

- 负载模型直接来自源码：每副本负载 = `weight / logcnt`（[eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117) 的同一假设），复制守恒使一层的 GPU 负载均值恒为 W/P，与放置方案无关。
- 槽位按「节点→GPU→槽内」编码（[eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)），因此 `per_replica_load(...).view(L, P, e).sum(-1)` 一行复算每 GPU 负载。
- 主指标 max/mean ≥ 1 恒成立，1/IB 即理论利用率上限；层级策略还有更紧的条件下界 \(\max_n W_n/(P/N)\)，其 IB 可分解为「节点配平损失 × 节点内损失」。
- README 示例手算：层级 IB=1.208（= 1.137 × 1.063）vs 全局 IB=1.111——层级策略为「同组同节点」的流量约束付出约 9% 的 max 负载代价，且其节点划分已是约束最优。
- 策略无法显式选择，对照实验靠翻转 `num_groups` 的整除性（G=4 vs G=3），必须同 weight、同 N、同 P、同 M；纯负载指标上 global 占优不代表总体更优。
- 冗余收益来自负载离散度：长尾分布收益最大，均匀分布趋近 0 且边际递减；基数约束（每 GPU 恰好 M/P 个专家）是两种策略共同的、装箱无法弥补的剩余不均衡来源。

## 7. 下一步学习建议

- **u3-l3 性能、数值与设备一致性**：本讲的评测脚本在大规模（如 60 层 × 256 专家）上会触到 `balanced_packing`/`replicate_experts` 的 Python 循环瓶颈，下一讲正好分析复杂度与向量化机会；你评测脚本里的耗时数据可以直接带过去讨论。
- **u3-l4 工程集成**：本讲的 `realistic` 分布用静态模拟代替了真实统计；下一讲把「负载统计（滑动平均）→ rebalance_experts → 权重重排」串成随时间演进的流水线，本讲的指标函数可直接复用为每轮重排前后的监控。
- **源码延伸阅读**：重读 [eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)（降序排序）与 [eplb.py:34-35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L35)（最轻未满包），思考：如果把装箱目标从「最小化 max」换成「最小化方差」，贪心准则该怎么改？这可以作为 u3-l5 变体实战的候选题目之一。
