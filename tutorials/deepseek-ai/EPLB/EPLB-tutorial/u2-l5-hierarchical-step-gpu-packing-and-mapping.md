# 层级策略第三步：GPU 打包与多层映射链合成

## 1. 本讲目标

上一讲（u2-l4）我们读完了 `rebalance_experts_hierarchical` 的 Step 1（组打包到节点）与 Step 2（节点内复制冗余专家）。本讲读完整个函数的最后一段——Step 3。学完本讲，你应该能够：

1. **追踪一个物理专家的完整旅程**：从 Step 2 产出的节点内编号（phy / mlog），一步步翻译，直到它最终落在「某个节点的某张 GPU 的某个槽位」上，并且能反查出它对应的原始逻辑专家编号（log）。
2. **解释槽位编码公式** `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`：它是混合进制编码，同时回答「放在哪张 GPU、第几个槽」两个问题。
3. **说明末尾按 `num_logical_experts // num_nodes` 为步长的 `arange` 偏移**如何把节点内局部编号「加上基地址」还原为全局编号。
4. 掌握一个可复用的源码阅读心法：**映射链的合成 = 函数复合 = 逐段 gather**。

本讲引用的代码集中在 [eplb.py:115-129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L129)，只有 15 行，但每行都在做编号系统之间的翻译，是整个文件里信息密度最高的一段。

## 2. 前置知识

### 2.1 承接上一讲：进入 Step 3 时手上有哪些资产

先把前两步的产出列出来（详细推导见 u2-l4，这里只回顾结论）。记：

- \( E \) = `num_logical_experts`（逻辑专家数）
- \( M \) = `num_physical_experts`（物理专家总数）
- \( N \) = `num_nodes`（节点数）
- \( P \) = `num_gpus`（GPU 总数）
- \( L \) = `num_layers`（MoE 层数）

README 示例中 \( L=2, E=12, M=16, N=2, P=8 \)。以下手工推演均以该示例**第 0 层**为例（第 0 层负载为 `[90, 132, 40, 61, 104, 165, 39, 4, 73, 56, 183, 86]`）。

Step 1 把 4 个组打包到 2 个节点后（节点 0 得组 1、组 2，负载 446；节点 1 得组 3、组 0，负载 587），得到两张互逆置换表：

- `log2mlog = [9, 10, 11, 0, 1, 2, 3, 4, 5, 6, 7, 8]`（逻辑编号 → 按节点分块重编号）
- `mlog2log = [3, 4, 5, 6, 7, 8, 9, 10, 11, 0, 1, 2]`（逆映射）

Step 2 在每个节点内独立调用 `replicate_experts`（每节点 6 个逻辑专家复制成 8 个物理专家），得到（**行 = 层×节点**，本讲用第 0 层的两个节点行）：

| 量 | 形状 | 节点 0（第 0 层） | 节点 1（第 0 层） |
|---|---|---|---|
| `tokens_per_mlog` | `[L·N, E/N]` | `[61, 104, 165, 39, 4, 73]` | `[56, 183, 86, 90, 132, 40]` |
| `phy2mlog` | `[L·N, M/N]` | `[0, 1, 2, 3, 4, 5, 2, 1]` | `[0, 1, 2, 3, 4, 5, 1, 4]` |
| `phyrank` | `[L·N, M/N]` | `[0, 0, 0, 0, 0, 0, 1, 1]` | `[0, 0, 0, 0, 0, 0, 1, 1]` |
| `mlogcnt` | `[L·N, E/N]` | `[1, 2, 2, 1, 1, 1]` | `[1, 2, 1, 1, 2, 1]` |

以节点 0 为例读一下 `phy2mlog = [0, 1, 2, 3, 4, 5, 2, 1]`：前 6 个槽位是「初始副本」，第 6、7 个槽位是复制出来的冗余专家，分别复制了节点内 2 号（专家 5）和 1 号（专家 4）。`mlogcnt = [1, 2, 2, 1, 1, 1]` 说明节点 0 内专家 4、专家 5 各有 2 个副本。

### 2.2 为什么 Step 3 还要做一次装箱

Step 2 结束时，我们只知道每个节点内有 8 个物理专家，但**还没有决定它们分别放在节点的 4 张 GPU 上的哪个槽位**。层级策略的三步是「组→节点、节点内复制、专家→GPU」，Step 3 补上最后一环：把每个节点的 \( M/N = 8 \) 个物理专家装箱到该节点的 \( P/N = 4 \) 张 GPU 上，每张 GPU 恰好 \( M/P = 2 \) 个——这个每 GPU 专属数在源码里叫 `phy_experts_per_gpu`（[eplb.py:96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L96)）。

### 2.3 四套编号系统（本讲的地图）

这 15 行代码里出现了 4 个「编号宇宙」，理解本讲的关键就是随时知道自己在哪个宇宙里：

| 编号 | 名称 | 范围 | 含义 |
|---|---|---|---|
| `log` | 原始逻辑编号 | \([0, E)\) 全局 | 使用者认识的专家编号，最终输出必须回到它 |
| `mlog` | 节点分块编号 | \([0, E)\) 全局 | 按节点重排后的逻辑编号：节点 \( n \) 占区间 \([n \cdot E/N,\ (n+1) \cdot E/N)\) |
| `phy` | 节点内物理专家序 | \([0, M/N)\) 节点内 | Step 2 的输出行内顺序（前 \( E/N \) 个是初始副本，后面是复制的） |
| `pphy` | 节点内物理槽位 | \([0, M/N)\) 节点内 | 最终放置编号 = 节点内 GPU 号 × 每 GPU 槽数 + 槽内序号 |

它们之间的翻译关系（箭头方向即「X2Y 表把 X 编号翻译成 Y 编号」）：

```
              log2mlog                 (Step 2 复制)              phy2pphy (本讲)
   log  ─────────────────►  mlog  ──────────────────────►  phy  ─────────────────►  pphy
              mlog2log                 phy2mlog                    pphy2phy
   ◄─────────────────            ◄─────────────────────      ◄─────────────────
```

Step 3 的全部工作可以概括为一句话：**算出 `phy2pphy`（放到哪张 GPU），然后把 `pphy → phy → mlog → log` 这条链逐段复合，得到最终输出**。

### 2.4 两个必备直觉

**函数复合**：若映射 \( f \) 把 pphy 翻译成 phy，\( g \) 把 phy 翻译成 mlog，那么「先 \( f \) 后 \( g \)」的复合映射 \( g \circ f \) 就把 pphy 翻译成 mlog。本讲末尾的 `pphy2log` 就是四个映射的复合。

**gather 的方向规则**（u2-l3 讲过，这里给出合成形式）：`A2B` 类映射张量按 gather 合成的一般规律是

\[ \texttt{Z2Y} = \texttt{X2Y}.\mathrm{gather}(-1,\ \texttt{Z2X}) \]

即：想要「Z → Y」的新表，就拿旧的「X → Y」表，用「Z → X」表作为 index 去 gather。记住这一条，本讲每一行 gather 你都能自己推出来。

## 3. 本讲源码地图

整个仓库核心只有一个文件，本讲聚焦其中 Step 3 与它复用的 `balanced_packing`：

| 文件 | 行号 | 作用 |
|---|---|---|
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) L74-L96 | 层级函数签名与 4 条整除断言、`phy_experts_per_gpu` 的定义 |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) L98-L101 | `inverse`：用 `scatter_` 求逆置换的内部工具函数 |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) L103-L113 | Step 1 + Step 2（上一讲已精读，本讲作为输入回顾） |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) L115-L129 | **本讲主体**：Step 3，副本负载 → GPU 装箱 → 映射链合成 |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) L5-L41 | `balanced_packing`：被 Step 3 第二次调用的贪心装箱 |
| README.md L39-L57 | 两层 12 专家示例与官方输出，本讲所有手算的对照基准 |

## 4. 核心概念与源码讲解

### 4.1 模块一：副本负载计算与 GPU 装箱（`rebalance_experts_hierarchical` 前半 + `balanced_packing` 复用）

#### 4.1.1 概念说明

Step 2 复制出冗余专家后，产生了一个计量问题：**装箱时，每个物理槽位的「重量」该算多少？**

逻辑专家的原始负载 `tokens_per_mlog` 是按「专家」计的，但一个有 2 个副本的专家，它的 token 会被路由层分摊到两个副本上（副本间流量均分，这正是 u2-l2 讲过的 `replicate_experts` 贪心的前提假设）。所以打包装箱时，单个副本的期望负载是

\[ \text{单副本负载} = \frac{\text{该逻辑专家的总负载}}{\text{副本数}} = \frac{\texttt{tokens\_per\_mlog}}{\texttt{mlogcnt}} \]

如果不做这个除法，被复制的专家会按双倍重量参与装箱，装箱器会错误地「躲开」它们，反而把放放置方案弄歪。

#### 4.1.2 核心流程

```text
对每一行（层 × 节点）：
1. tokens_per_mlog / mlogcnt     → 每个节点内 mlog 的单副本期望负载  [L·N, E/N]
2. .gather(-1, phy2mlog)         → 按物理专家顺序重排               [L·N, M/N]
3. balanced_packing(..., P/N)    → 每节点的物理专家装箱到该节点的 GPU
   每包（GPU）恰好 phy_experts_per_gpu = M/P 个
```

#### 4.1.3 源码精读

第一步，计算每个物理槽位的负载，对应 [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)：

```python
tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
```

- `tokens_per_mlog / mlogcnt`：浮点除法（入口已把 `weight` 转成 `float`），得到每个 mlog 的单副本负载，形状 `[L·N, E/N]`。
- `.gather(-1, phy2mlog)`：这里套用 2.4 节的合成规律——想要「phy → 负载」，已有「mlog → 负载」和「phy → mlog」，所以用 `phy2mlog` 作 index 去 gather。注意 gather 维度上 index 的长度可以超过输入（\( M/N > E/N \)），只要值域落在 \([0, E/N)\) 内即可——多个物理槽位可以取同一个 mlog 的负载，这正是「副本」的含义。

第二步，装箱到 GPU，对应 [eplb.py:118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118)：

```python
pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
```

注意传给 `balanced_packing` 的包数是 `num_gpus // num_nodes`（**节点内 GPU 数**），不是总 GPU 数——因为 `tokens_per_phy` 的每一行只描述**一个节点**的 8 个物理专家，它们只在本节点的 4 张 GPU 之间分配。函数内部会断言 `(M/N) % (P/N) == 0`，它等价于入口处的 `num_physical_experts % num_gpus == 0`（[eplb.py:95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95)），每张 GPU 恰好分到 `phy_experts_per_gpu` 个（[eplb.py:96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L96)）。

被复用的 `balanced_packing` 主体是 [eplb.py:27-41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L41) 的 LPT 式贪心：物品按负载降序逐个放入「未满且当前最轻」的包（[eplb.py:34-40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L40)），算法细节在 u2-l1 已逐行讲过。有一个值得重申的分支：当 `groups_per_pack == 1`（即每张 GPU 只放 1 个物理专家，`phy_experts_per_gpu == 1`）时走 [eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 的平凡分支——此时根本没有装箱自由度，`pack_index` 直接是 `arange`、`rank_in_pack` 全 0。

#### 4.1.4 代码实践

**实践目标**：手工算出 README 示例第 0 层两个节点的 `tokens_per_phy`，并用代码验证。

**操作步骤**（示例代码）：

```python
import torch

# 节点 0（第 0 层）：mlog 0..5 对应专家 3,4,5,6,7,8
tokens_per_mlog = torch.tensor([[61., 104., 165., 39., 4., 73.]])
phy2mlog        = torch.tensor([[0, 1, 2, 3, 4, 5, 2, 1]])
mlogcnt         = torch.tensor([[1, 2, 2, 1, 1, 1]])

tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
print(tokens_per_phy)
```

**需要观察的现象**：输出应为 `[61, 52, 82.5, 39, 4, 73, 82.5, 52]`——前 6 个等于原始负载，第 6、7 个槽位（冗余副本）分别取了 `165/2 = 82.5` 和 `104/2 = 52`。

**预期结果**：手工推演值如上表（本讲所有手算的最终输出已与 README 官方输出逐位核对一致，中间张量请运行验证，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果装箱时直接用 `tokens_per_mlog`（不除以 `mlogcnt`）会怎样？
**答案**：被复制专家的两个副本各自按全量负载计入所在 GPU，装箱器会系统性低估「与重载专家副本同卡」的方案、高估其代价，导致 GPU 负载预测失真；最终的每 GPU 实际负载会偏离装箱时的估计，均衡效果变差。

**练习 2**：`phy_experts_per_gpu == 1` 时，`balanced_packing` 走哪条分支？Step 3 结果如何？
**答案**：走 [eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 平凡分支，`pack_index = arange`、`rank_in_pack = 0`，于是 `phy2pphy` 是恒等置换，即「物理专家序 = 槽位序」，无重排发生。

**练习 3**：为什么装箱的包数是 `num_gpus // num_nodes` 而不是 `num_gpus`？
**答案**：`tokens_per_phy` 的形状是 `[L·N, M/N]`，每行只含**单个节点**的物理专家；节点之间的分配已在 Step 1 完成，Step 3 只做节点内分配，所以包数是节点内 GPU 数。

### 4.2 模块二：槽位编码 `phy2pphy` 与逆置换 `pphy2phy`

#### 4.2.1 概念说明

`balanced_packing` 返回的 `pack_index`（放进哪张 GPU）和 `rank_in_pack`（在包内排第几）是两个**按物品组织**的并行数组——第 \( i \) 列描述物品 \( i \) 的去向。但下游需要的「放置方案」是一张**按槽位组织**的表：给定槽位，里面放的是谁。

解决办法是把两个整数压成一个**槽位编号 pphy**：

\[ \texttt{pphy} = \texttt{pack\_index} \times \text{phy\_experts\_per\_gpu} + \texttt{rank\_in\_pack} \]

这就是十进制「十位 × 10 + 个位」的混合进制套路：pack_index 是「高位」，rank_in_pack 是「低位」。因为每个包恰好装 `phy_experts_per_gpu` 个物品（装箱的基数约束），这个编码恰好把 \([0, M/N)\) 的每个槽位编号**不重不漏**地分配出去——`phy2pphy` 是一个置换。

#### 4.2.2 核心流程

```text
pack_index ∈ [0, P/N)      每个物理专家放进节点内哪张 GPU
rank_in_pack ∈ [0, M/P)    在该 GPU 的第几个槽
        │
        ▼  混合进制编码
phy2pphy[i] = pack_index[i] * (M/P) + rank_in_pack[i]     # phy → pphy 置换
        │
        ▼  inverse() 求逆
pphy2phy                                                    # pphy → phy 置换
```

反向解码公式（给定槽位读出位置）：

\[ n = \left\lfloor \frac{c}{M/N} \right\rfloor,\quad
g = \left\lfloor \frac{c \bmod (M/N)}{M/P} \right\rfloor,\quad
r = c \bmod \frac{M}{P} \]

其中 \( c \) 是最终输出的全局槽位编号（节点号 \( n \)、节点内 GPU 号 \( g \)、槽内序号 \( r \)）——这正是 u1-l3 讲过的「物理槽位按节点→GPU→槽内位置编码」的来源。

#### 4.2.3 源码精读

槽位编码与求逆对应 [eplb.py:119-120](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119-L120)：

```python
phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
pphy2phy = inverse(phy2pphy)
```

`inverse` 定义在 [eplb.py:98-101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101)：

```python
def inverse(perm: torch.Tensor) -> torch.Tensor:
    inv = torch.empty_like(perm)
    inv.scatter_(1, perm, torch.arange(perm.size(1), ...).expand(perm.shape))
    return inv
```

其原理（u2-l3 已详解）：`scatter_` 是写侧重排，把「第 \( i \) 个值写到位置 `perm[i]`」，于是 `inv[perm[i]] = i`，恰好是求逆。同一手法在本函数里被用了两次——Step 1 求 `mlog2log`（[eplb.py:108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L108)），Step 3 求 `pphy2phy`。

**为什么马上要求逆？** 因为下一行的映射链合成需要「pphy → phy」方向的表。我们手上有 `phy2mlog`（phy → mlog）和 `phy2pphy`（phy → pphy），要构造「pphy → mlog」只能借助逆 `pphy2phy` 先把 pphy 翻译回 phy。

#### 4.2.4 代码实践

**实践目标**：验证 `phy2pphy` 是每行 `0..M/N-1` 的置换，并练习槽位解码。

**操作步骤**（示例代码）：

```python
import torch

# 节点 0（第 0 层）装箱结果，按 phy 序（i = 0..7）
pack_index   = torch.tensor([[3, 3, 0, 0, 1, 2, 1, 2]])
rank_in_pack = torch.tensor([[0, 1, 0, 1, 1, 0, 0, 1]])

phy2pphy = pack_index * 2 + rank_in_pack      # phy_experts_per_gpu = 2
print(phy2pphy)                                # 期望 [6, 7, 0, 1, 3, 4, 2, 5]
print(torch.sort(phy2pphy).values)             # 排序后应为 0..7 —— 置换验证

# 解码：全局槽位 13 在哪里？（M/N = 8, M/P = 2）
c = 13
n, q = divmod(c, 8)      # 节点号、节点内槽位
g, r = divmod(q, 2)      # 节点内 GPU 号、槽内序号
print(n, g, r)           # 期望 1 2 1：节点 1 的 2 号 GPU（全局 6 号）第 1 槽
```

**需要观察的现象**：`sort` 后得到 `0..7` 有序序列，说明每个槽位恰好被一个物品占据；解码结果 `1 2 1`。

**预期结果**：以上手工推演值（待本地验证）。README 输出的第 0 层 `phy2log` 第 13 列是专家 1——它确实是被复制专家（`logcnt=2`）的 rank-1 副本，放在节点 1 的 2 号 GPU。

#### 4.2.5 小练习与答案

**练习 1**：证明 `pack_index * phy_experts_per_gpu + rank_in_pack` 是 \([0, M/N)\) 上的双射。
**答案**：装箱基数约束保证每包恰有 \( M/P \) 个物品，即每个 `pack_index` 值与每个 `rank_in_pack` 值的组合恰好出现一次；而 \( 0 \le \text{pack} < P/N \)、\( 0 \le \text{rank} < M/P \)，乘积编码覆盖 \([0, (P/N) \cdot (M/P)) = [0, M/N)\) 且无碰撞——这正是混合进制表示的唯一性。

**练习 2**：`phy2pphy` 与 Step 1 的 `log2mlog`（[eplb.py:106-107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107)）在构造手法上有什么共同点？
**答案**：都是「高位乘基数 + 低位偏移」的复合索引编码：`log2mlog` 用 `(pack*groups_per_node + rank) * group_size + arange(group_size)`，`phy2pphy` 用 `pack_index * phy_experts_per_gpu + rank_in_pack`；差别仅在于后者低维不需要再展开组内 `arange`，因为物品本身就是单个物理专家。

**练习 3**：如果不调用 `inverse`，直接拿 `phy2pphy` 去 gather `phy2mlog`，得到的是哪个方向的映射？
**答案**：`phy2mlog.gather(-1, phy2pphy)` 按 2.4 节规律得到的是「pphy 无关的另一张表」——确切说 index 必须是「目的地→来源」方向，`phy2pphy` 是 phy→pphy，用它做 index 得到的是按 pphy 的**值**重排的表，即「错序的 phy → mlog」，不是想要的 `pphy2mlog`。方向弄反是这类代码最典型的 bug。

### 4.3 模块三：多层映射链合成——从 pphy 一路翻译回 log

#### 4.3.1 概念说明

现在手上有了 `pphy2phy`（pphy → phy），而 Step 2 留下了 `phy2mlog`（phy → **节点内局部** mlog），Step 1 留下了全局的 `mlog2log`（mlog → log）。最终目标是一张 `[L, M]` 的表：**每个最终物理槽位里放的是哪个原始逻辑专家**。

这里有一个隐藏的坑：`phy2mlog` 的值域是**节点内局部编号** \([0, E/N)\)，而 `mlog2log` 是**全局** mlog 的映射表（值域 \([0, E)\)）。两段链路中间差一个「节点基地址」。所以合成分三段：

1. **换序**：`pphy → phy → 节点内 mlog`（一次 gather）；
2. **升维到全局**：给第 \( n \) 个节点的编号加上基地址 \( n \cdot E/N \)，变成全局 mlog；
3. **翻译**：全局 `mlog → log`（再一次 gather）。

用复合函数记号，最终输出是：

\[ \texttt{pphy2log} = \texttt{mlog2log} \circ (\text{基地址偏移} \circ \texttt{phy2mlog} \circ \texttt{pphy2phy}) \]

#### 4.3.2 核心流程

```text
(A) pphy2mlog = phy2mlog.gather(-1, pphy2phy)          # pphy → 节点内 mlog   [L·N, M/N]
(B) pphy2mlog.view(L, N, -1) + arange(0, E, E/N)        # 逐节点加基地址      [L, N, M/N]
(C) .flatten(-2)                                        # → 全局 mlog          [L, M]
(D) pphy2log = mlog2log.gather(-1, pphy2mlog)           # 全局 mlog → log      [L, M]

支线 1：pphyrank = phyrank.gather(-1, pphy2phy).view(L, -1)   # 副本序号同步换序 [L, M]
支线 2：logcnt  = mlogcnt.view(L, -1).gather(-1, log2mlog)    # 副本计数映射回 log 序 [L, E]
```

注意 (D) 之前必须先完成 (B)(C)：`mlog2log` 的形状是 `[L, E]`，gather 的 index 第一维必须与它一致（都是 \( L \)），所以 `[L·N, M/N]` 必须先变回 `[L, M]`——这也是 (B) 中 `view` 的第二个作用（第一个作用是让基地址能按节点维度广播）。

#### 4.3.3 源码精读

映射链的主体在 [eplb.py:122-126](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L122-L126)：

```python
pphy2mlog = phy2mlog.gather(-1, pphy2phy) # [num_layers * num_nodes, num_log_per_nodes]
pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) + 
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                          device=group_pack_index.device).view(1, -1, 1)).flatten(-2)
pphy2log = mlog2log.gather(-1, pphy2mlog)
```

**第一行（换序）**：套用规律 \(\texttt{Z2Y} = \texttt{X2Y}.\mathrm{gather}(-1, \texttt{Z2X})\)——想要 pphy→mlog，拿 phy→mlog 的表用 pphy→phy 做 index。**注意变量被连续赋值两次**：第一行结束时 `pphy2mlog` 还是节点内局部编号，第二行结束后才升级为全局编号，变量名没变但语义变了，这是阅读时的坑。顺带一提，行尾注释写的形状 `[num_layers * num_nodes, num_log_per_nodes]` 描述的是第一行结束时的形状，且名字略有出入——该维度实际是每节点**物理**专家数 \( M/N \)（只是值域是逻辑编号），阅读时不要盲信注释，以张量运算为准。

**第二行（加基地址，本讲学习目标之三）**：

```python
torch.arange(0, num_logical_experts, num_logical_experts // num_nodes)
```

生成 \([0,\ E/N,\ 2E/N,\ \ldots,\ (N-1) \cdot E/N)\)，共 \( N \) 个元素——每个节点的 mlog 区间左端点。`view(num_layers, num_nodes, -1)` 把 `[L·N, M/N]` 整形为 `[L, N, M/N]`，使第 \( n \) 片（第 \( n \) 个节点的所有槽位）统一加上第 \( n \) 个基地址；`arange(...).view(1, -1, 1)` 沿节点维广播。由于 mlog 编码保证节点 \( n \) 占全局区间 \([n \cdot E/N, (n+1) \cdot E/N)\)（u2-l4 讲过的分块连续性），「局部编号 + 基地址 = 全局编号」严格成立。最后 `flatten(-2)` 把 `[L, N, M/N]` 压回 `[L, M]`，行内顺序为「节点 0 的 8 个槽位、节点 1 的 8 个槽位……」——这正是最终输出的全局槽位布局。

这一行里的 `device=group_pack_index.device` 参数是 commit `d52c72d`（"Fix missing device for pphy2mlog tensor"）补上的修复：若输入 `weight` 在 GPU 上，这个 `arange` 默认会建在 CPU 上，与 GPU 张量相加直接报错。设备一致性的完整讨论留到 u3-l3。

**第三行（翻译回 log）**：再一次套用 gather 合成规律——想要 pphy→log，拿 mlog→log 的表用 pphy→mlog 做 index。至此主链完成。

两条支线在 [eplb.py:127-128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L127-L128)：

```python
pphyrank = phyrank.gather(-1, pphy2phy).view(num_layers, -1)
logcnt = mlogcnt.view(num_layers, -1).gather(-1, log2mlog)
```

- `pphyrank`：副本序号只需随槽位换序（同样的 `pphy2phy` gather），再 `view` 回 `[L, M]`。入口函数随后用 `(phy2log, phyrank)` 组装 `log2phy`（u2-l6 的内容）。
- `logcnt`：`mlogcnt` 是按 mlog 顺序排的副本计数，`view(L, -1)` 后用 `log2mlog` 作 index gather——把「按 mlog 排的计数」重排回「按原始 log 排的计数」。注意这里用的是 `log2mlog`（log→mlog）而不是 `mlog2log`，方向同样由「index 取目的地→来源」规则决定：目的地是 log，来源是 mlog。

最后 [eplb.py:129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L129) 返回三件套 `pphy2log, pphyrank, logcnt`，即入口 `rebalance_experts` 转发给用户的 `phy2log, phyrank, logcnt`。

**手工验证第 0 层全链**（与 README 官方输出核对）：

| 步骤 | 节点 0 | 节点 1 |
|---|---|---|
| `pphy2phy` | `[2, 3, 6, 4, 5, 7, 0, 1]` | `[1, 0, 6, 5, 3, 7, 2, 4]` |
| `pphy2mlog`（局部，换序后） | `[2, 3, 2, 4, 5, 1, 0, 1]` | `[1, 0, 1, 5, 3, 4, 2, 4]` |
| `pphy2mlog`（+基地址 0 / 6） | `[2, 3, 2, 4, 5, 1, 0, 1]` | `[7, 6, 7, 11, 9, 10, 8, 10]` |
| `pphy2log = mlog2log[...]` | `[5, 6, 5, 7, 8, 4, 3, 4]` | `[10, 9, 10, 2, 0, 1, 11, 1]` |

拼接后第 0 层 `pphy2log = [5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]`，与 README 示例输出（[README.md:55-56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55-L56)）第一行**逐位一致**。

#### 4.3.4 代码实践

**实践目标**：跟踪第 0 层被复制的逻辑专家 5（`logcnt=2`），把它的两个副本从 mlog 编号一路追到最终 GPU 槽位。

**操作步骤**（示例代码，把上面的手工推演交给程序执行）：

```python
import torch

# 第 0 层、节点 0 的 Step 3 输入（来自 4.1/4.2 两节的推演）
phy2mlog = torch.tensor([[0, 1, 2, 3, 4, 5, 2, 1]])
pphy2phy = torch.tensor([[2, 3, 6, 4, 5, 7, 0, 1]])
mlog2log = torch.tensor([[3, 4, 5, 6, 7, 8, 9, 10, 11, 0, 1, 2]])

pphy2mlog_local = phy2mlog.gather(-1, pphy2phy)              # (A) 换序
pphy2mlog_global = pphy2mlog_local + 6 * 0                   # (B) 节点 0 基地址为 0
pphy2log = mlog2log.gather(-1, pphy2mlog_global)             # (D) 翻译回 log

for slot in range(8):
    if pphy2log[0, slot] == 5:                               # 找出专家 5 的所有槽位
        print(f"全局槽位 {slot}: mlog={pphy2mlog_global[0, slot]}, "
              f"GPU={slot // 2}, 槽内={slot % 2}")
```

**需要观察的现象**：程序应打印两个槽位——全局槽位 0（节点 0 的 0 号 GPU 第 0 槽）和全局槽位 2（节点 0 的 1 号 GPU 第 0 槽），两者对应的 mlog 都是 2。

**预期结果**：手工推演得到链路图如下（最终输出已与 README 核对一致，中间量待本地验证）：

```
log 5 ──(log2mlog)──> mlog 2（节点 0）
                        │ Step 2 复制，mlogcnt[2] = 2
            ┌───────────┴───────────┐
        副本 0                   副本 1
      phy 序中的 phy 2         phy 序中的 phy 6（新增槽）
            │ 装箱到包 0 rank 0       │ 装箱到包 1 rank 0
        pphy = 0×2+0 = 0        pphy = 1×2+0 = 2
            │ + 节点基地址 0          │ + 节点基地址 0
        全局槽位 0                全局槽位 2
```

对照 README 输出第一行 `[5, 6, 5, ...]`：第 0、2 列确实是 5。✓

#### 4.3.5 小练习与答案

**练习 1**：`pphy2mlog` 的构造为什么必须分「gather 换序」和「加基地址」两步？直接用 `phy2mlog` 有什么问题？
**答案**：`phy2mlog` 是按 Step 2 的输出顺序（phy 序）组织的，而最终表必须按槽位顺序（pphy 序）组织，所以要先换序；且其值域是节点内局部编号，而 `mlog2log` 只认全局 mlog，必须加基地址 \( n \cdot E/N \) 对齐。两步解决的是两个独立的错位：顺序错位和值域错位。

**练习 2**：`logcnt` 的还原为什么用 `log2mlog` 做 index，而不是 `mlog2log`？
**答案**：gather 的 index 必须取「目的地 → 来源」方向。这里目的地是 log 序、来源是 mlog 序，所以用 log→mlog 的 `log2mlog`：`logcnt[log] = mlogcnt[log2mlog[log]]`。若用 `mlog2log`，得到的是把 logcnt 按 mlog 位置打散的错误排列。

**练习 3**：Step 3 生产的 `pphy2log` 每列含义是「槽位 → 逻辑专家」。如果要反过来问「逻辑专家 5 的副本都在哪些槽位」，仅凭 `pphy2log` 和 `pphyrank` 怎么求？
**答案**：`torch.nonzero(pphy2log == 5)` 即得全部槽位；更一般地，入口函数 `rebalance_experts` 用 `pphy2log * maxlogcnt + pphyrank` 做扁平化 scatter，组装出反向表 `log2phy`（含 -1 padding）——这是下一讲 u2-l6 的主题。

## 5. 综合实践

**任务**：写一个「实验版」层级函数，在 Step 3 的每个关键点插入打印，然后选取一个被复制的专家，输出它在各级编号系统中的取值，画出完整映射链路图。

**操作步骤**（示例代码，基于 [eplb.py:74-129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L129) 复制改造，只动 Step 3）：

```python
import torch
from eplb import balanced_packing, replicate_experts

def hierarchical_debug(weight, num_physical_experts, num_groups, num_nodes, num_gpus):
    num_layers, num_logical_experts = weight.shape
    group_size = num_logical_experts // num_groups
    groups_per_node = num_groups // num_nodes
    phy_experts_per_gpu = num_physical_experts // num_gpus

    def inverse(perm):
        inv = torch.empty_like(perm)
        inv.scatter_(1, perm, torch.arange(perm.size(1)).expand(perm.shape))
        return inv

    # Step 1（与原版相同）
    tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
    group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes)
    log2mlog = (((group_pack_index * groups_per_node + group_rank_in_pack) * group_size)
                .unsqueeze(-1) + torch.arange(group_size)).flatten(-2)
    mlog2log = inverse(log2mlog)

    # Step 2（与原版相同）
    tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
    phy2mlog, phyrank, mlogcnt = replicate_experts(
        tokens_per_mlog, num_physical_experts // num_nodes)

    # Step 3：逐行打印
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
    print("tokens_per_phy:\n", tokens_per_phy)                      # ① 副本负载
    pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
    print("pack_index:\n", pack_index, "\nrank_in_pack:\n", rank_in_pack)
    phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
    pphy2phy = inverse(phy2pphy)
    print("phy2pphy:\n", phy2pphy)                                  # ② 槽位编码
    pphy2mlog = phy2mlog.gather(-1, pphy2phy)                       # ③ 换序
    pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1)
                 + torch.arange(0, num_logical_experts,
                                 num_logical_experts // num_nodes).view(1, -1, 1)).flatten(-2)
    print("pphy2mlog(全局):\n", pphy2mlog)                          # ④ +基地址
    pphy2log = mlog2log.gather(-1, pphy2mlog)
    print("pphy2log:\n", pphy2log)                                  # ⑤ 最终表
    return pphy2log

weight = torch.tensor([[90, 132, 40, 61, 104, 165, 39, 4, 73, 56, 183, 86],
                       [20, 107, 104, 64, 19, 197, 187, 157, 172, 86, 16, 27]])
phy2log = hierarchical_debug(weight, 16, 4, 2, 8)

# 追踪第 0 层所有被复制（logcnt > 1）的专家
counts = torch.bincount(phy2log[0], minlength=12)
for log_id in counts.nonzero().squeeze(1).tolist():
    if counts[log_id] > 1:
        slots = (phy2log[0] == log_id).nonzero().squeeze(1).tolist()
        print(f"专家 {log_id}: {counts[log_id]} 个副本, 全局槽位 {slots}")
```

**需要观察的现象与预期结果**：

1. `pphy2log` 第 0 行应等于 `[5, 6, 5, 7, 8, 4, 3, 4, 10, 9, 10, 2, 0, 1, 11, 1]`，与 README（[README.md:55-56](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L55-L56)）一致；第 1 行也应与 README 第二行一致。
2. 被复制的专家应为 `{1, 4, 5, 10}`（第 0 层），每个恰好 2 个副本；例如专家 5 的槽位是 `[0, 2]`——都在节点 0（槽位 < 8），且分别是节点 0 的 0 号、1 号 GPU 的第 0 槽。
3. 检查不变量：`pphy2mlog`（全局）与 `pphy2log` 的每行各自是「值可以重复、但槽位覆盖完整」的表；同一专家的多个副本槽位除以 `M/N = 8` 的商相同（副本不跨节点）。
4. 把中间量与本讲 4.3.3 的手工推演表逐项对照，确认你对该链路的理解与程序行为一致。

以上第 1 点的预期值直接来自 README 官方输出，第 2-4 点基于本讲手工推演（待本地验证）。完成后，把 4.3.4 的链路图补全成「四个被复制专家」的完整版本，你就拥有了层级策略 Step 3 的全景图。

## 6. 本讲小结

- **副本负载**：Step 3 先用 `tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)` 把「专家负载」换算成「单副本期望负载」——复制后流量被副本均分，装箱必须按副本计量（[eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)）。
- **GPU 装箱**：`balanced_packing(tokens_per_phy, num_gpus // num_nodes)` 在节点内把物理专家装箱到本节点 GPU，每卡恰好 `phy_experts_per_gpu = M/P` 个（[eplb.py:118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118)）。
- **槽位编码**：`phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack` 是混合进制编码，把「哪张 GPU + 第几个槽」压成一个槽位编号，且因基数约束必然构成置换（[eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)）。
- **映射链合成**：最终输出 = `pphy → phy →（+节点基地址）mlog → log` 的逐段复合，每段都是一次 gather，方向规则是「index 取目的地→来源」（[eplb.py:122-126](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L122-L126)）。
- **基地址偏移**：步长为 `num_logical_experts // num_nodes` 的 `arange` 给每个节点的局部编号加上区间左端点，把节点内 mlog 还原为全局 mlog；该行的 `device` 参数来自修复 commit `d52c72d`（[eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)）。
- **同步还原**：`pphyrank` 随槽位换序、`logcnt` 用 `log2mlog` 映射回原始 log 序，三个返回值共同构成完整放置方案（[eplb.py:127-129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L127-L129)）。

## 7. 下一步学习建议

层级策略的三步到此全部读完，但 `rebalance_experts_hierarchical` 仍是内部函数。下一讲 **u2-l6（入口函数与全局策略）** 将精读 [eplb.py:131-162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L131-L162) 的入口 `rebalance_experts`：

- 它如何按 `num_groups % num_nodes` 分派层级/全局策略，以及全局策略如何用退化参数 `(1, 1, num_gpus)` 复用本讲的整条流水线（这也意味着全局策略同样享有本讲的 GPU 级装箱——commit `e1100fe` 的演进）；
- 它如何用本讲输出的 `pphy2log` 和 `pphyrank`，通过 `phy2log * maxlogcnt + phyrank` 的扁平化 scatter 组装出反向表 `log2phy`（含 -1 padding）。

阅读时可以带着一个问题：如果没有 `pphyrank`，`log2phy` 还能构造出来吗？带着它进入下一讲。
