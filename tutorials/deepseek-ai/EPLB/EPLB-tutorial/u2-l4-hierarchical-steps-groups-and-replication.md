# 层级策略前两步：组打包到节点与节点内复制

## 1. 本讲目标

本讲精读 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) 中 `rebalance_experts_hierarchical` 的 **Step 1（组打包到节点）** 与 **Step 2（节点内复制）**。学完后你应当能够：

1. 解释 `tokens_per_group` 的 `unflatten + sum` 计算链路：如何从每专家负载得到每专家组负载。
2. 推导 `log2mlog` 编码公式：乘法与 `arange` 偏移如何把「节点号 + 节点内组序号 + 组内偏移」三元组压缩成一个新逻辑编号（mlog 编号）。
3. 说明 `view` 成 `[num_layers * num_nodes, ...]` 如何让所有「层 × 节点」组合的复制在**一次** `replicate_experts` 调用中并行完成。
4. 亲手打印 Step 1、Step 2 的全部中间张量，并验证组划分后每个节点的总负载是否平衡。

Step 3（物理专家打包到 GPU 与多层映射链合成）留给下一讲 u2-l5。

## 2. 前置知识

本讲是 u2 单元的汇合点，默认你已掌握前三讲内容。这里只做一句话回顾，细节请回看对应讲义：

| 前置概念 | 一句话回顾 | 来源讲义 |
|---|---|---|
| 三步主流程 | 层级策略 = 组打包到节点 → 节点内复制 → 物理专家打包到 GPU | u1-l4 |
| `balanced_packing` | 物品按权重降序放入「未满且最轻」的包，每包恰好装 \(n/m\) 个（基数约束），输出 `pack_index` 与 `rank_in_pack` | u2-l1 |
| `replicate_experts` | 每轮复制 `weight / logcnt` 最大的逻辑专家，输出 `phy2log`、`rank`、`logcnt`，循环只跑 `num_phy - num_log` 轮且所有行向量化并行 | u2-l2 |
| A2B 映射表 | `A2B[i]` 读作「A 编号 i 对应的 B 编号」；`gather` 的 index 必须取「目的地 → 来源」方向 | u2-l3 |
| `inverse` | 用 `scatter_` 求置换的逆，`inv[perm[i]] = i`，等价于 argsort | u2-l3 |
| 复合索引编码 | 把多个编号按混合进制进位展平成一维，还原用带步长 `arange` 加基地址 | u2-l3 |
| 组约束 | DeepSeek-V3 组受限路由下，同组专家（含副本）必须放在同一节点 | u1-l2 |

**符号约定**（本讲全程使用，与源码变量对应）：

| 符号 | 源码变量 | 含义 |
|---|---|---|
| \(L\) | `num_layers` | MoE 层数 |
| \(E\) | `num_logical_experts` | 逻辑专家数 |
| \(M\) | `num_physical_experts` | 物理专家总数（复制后） |
| \(G\) | `num_groups` | 专家组数 |
| \(N\) | `num_nodes` | 节点数 |
| \(P\) | `num_gpus` | GPU 总数 |
| \(s = E/G\) | `group_size` | 每组专家数 |
| \(G/N\) | `groups_per_node` | 每节点组数 |
| \(M/P\) | `phy_experts_per_gpu` | 每 GPU 物理专家槽位数 |

## 3. 本讲源码地图

本讲只涉及一个源码文件 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py)（全仓库核心也只有它），但按行号分成几段，各有分工：

| 行范围 | 内容 | 本讲角色 |
|---|---|---|
| [eplb.py:74-88](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L88) | 函数签名与文档 | 入口 |
| [eplb.py:89-96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96) | 形状解析、四条整除断言、派生量 | 模块 4.1 精读 |
| [eplb.py:98-101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101) | 内嵌 `inverse` 函数 | u2-l3 已精读，本讲复用 |
| [eplb.py:103-108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L108) | **Step 1：组打包到节点** | 模块 4.2、4.3 精读 |
| [eplb.py:110-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113) | **Step 2：节点内复制** | 模块 4.4 精读 |
| [eplb.py:115-129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L129) | Step 3：GPU 打包与映射链 | 下一讲 u2-l5 |
| [eplb.py:5-41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L41) | `balanced_packing` | u2-l1 已精读，本讲看它如何被复用 |
| [eplb.py:44-71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71) | `replicate_experts` | u2-l2 已精读，本讲看它如何被复用 |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 两层 12 专家示例 | 本讲全部手算示例的数据来源 |

## 4. 核心概念与源码讲解

### 4.1 模块一：层级策略主干——参数约束与三步流水线总览

#### 4.1.1 概念说明

`rebalance_experts_hierarchical` 是层级策略的唯一实现。它要解决的问题：在**组不可拆散**的前提下，把 \(E\) 个逻辑专家复制成 \(M\) 个物理专家并放到 \(N\) 个节点、\(P\) 个 GPU 上，使各 GPU 负载尽量均衡。

为什么分三步而不是一步到位？因为约束是分层的：

- **节点级约束是硬的**：组受限路由要求同组专家（连同副本）必须在同一节点，否则跨节点 all-to-all 流量爆炸。所以先把**组**当作不可分的原子，打包到节点。
- **节点级负载是复制救不回来的**：一旦组被钉死在节点上，节点总负载就定了。节点内复制只能在节点内部摊薄，无法跨节点搬运负载。所以必须**先**在组粒度上把节点负载配平（Step 1），**再**做复制（Step 2），最后才在节点内做 GPU 级装箱（Step 3）。

这个顺序不可交换——如果先复制再分组，副本可能落到「错误的」节点上破坏组约束。

#### 4.1.2 核心流程

```text
输入 weight: [L, E]（每层每个逻辑专家的负载统计）
│
├─ Step 1  组打包到节点（本讲模块 4.2、4.3）
│    ├─ tokens_per_group = weight.unflatten(-1,(G,s)).sum(-1)   → [L, G]
│    ├─ balanced_packing(tokens_per_group, N)
│    │      → group_pack_index [L,G]、group_rank_in_pack [L,G]
│    ├─ log2mlog  = 复合编码(节点号, 组序号, 组内偏移)            → [L, E]
│    └─ mlog2log  = inverse(log2mlog)                            → [L, E]
│
├─ Step 2  节点内复制（本讲模块 4.4）
│    ├─ tokens_per_mlog = weight.gather(-1, mlog2log).view(L·N, E/N)
│    └─ replicate_experts(tokens_per_mlog, M/N)
│           → phy2mlog [L·N, M/N]、phyrank [L·N, M/N]、mlogcnt [L·N, E/N]
│
└─ Step 3  物理专家打包到 GPU + 映射链合成（下一讲 u2-l5）
       → pphy2log [L, M]、pphyrank [L, M]、logcnt [L, E]
```

张量形状演变主线（承接 u1-l4 的总结，本讲负责前半段）：

\[ [L, E] \xrightarrow{\text{Step 1}} [L, G] \xrightarrow{\text{编码}} [L, E] \xrightarrow{\text{Step 2}} [L \cdot N,\ E/N] \rightarrow [L \cdot N,\ M/N] \]

#### 4.1.3 源码精读

**入口与断言**。[eplb.py:89-96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96)：从 `weight` 形状解析出 \(L\) 和 \(E\)，随后是四条整除断言和派生量计算：

```python
num_layers, num_logical_experts = weight.shape
assert num_logical_experts % num_groups == 0
group_size = num_logical_experts // num_groups 
assert num_groups % num_nodes == 0
groups_per_node = num_groups // num_nodes
assert num_gpus % num_nodes == 0
assert num_physical_experts % num_gpus == 0
phy_experts_per_gpu = num_physical_experts // num_gpus
```

四条断言各自守护一层「均分」假设（u1-l2 曾从组约束推导过它们）：

| 断言 | 含义 | 为哪一步服务 |
|---|---|---|
| \(E \bmod G = 0\) | 每组恰好 \(s\) 个专家 | Step 1 的 `unflatten` |
| \(G \bmod N = 0\) | 每节点恰好 \(G/N\) 个组 | Step 1 的装箱基数约束 |
| \(P \bmod N = 0\) | 每节点 GPU 数相同 | Step 3 的 GPU 装箱 |
| \(M \bmod P = 0\) | 每 GPU 槽位相同（显存均等） | Step 3 的 `phy2pphy` 编码 |

注意 \(M \geq E\) 并**不**在这里断言——它由 `replicate_experts` 内部的 `assert num_redundant >= 0`（[eplb.py:60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L60)）兜底。

还有一个容易忽略的事实：入口函数 `rebalance_experts`（[eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)）只在 `num_groups % num_nodes == 0` 时才把原始参数传进来，否则改用退化参数 `(1, 1, num_gpus)` 走全局策略。所以**从入口调用永远不会触发第二条断言**，这些断言保护的是直接调用 `rebalance_experts_hierarchical` 的使用者。

**内嵌 `inverse`**。[eplb.py:98-101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101)：用 `scatter_` 求逆置换，机制在 u2-l3 已拆解（`scatter_` 写侧重排：`inv[perm[i]] = i`），本讲直接把它当作「求逆」黑盒使用，Step 1 用它从 `log2mlog` 得到 `mlog2log`。

#### 4.1.4 代码实践

**实践目标**：亲手触发四条断言，建立「哪些参数组合根本进不了算法」的直觉。

**操作步骤**：新建 `assert_probe.py`（放在仓库外或 tutorial 目录均可），直接调用层级函数：

```python
# 示例代码：断言探针
import torch
import eplb

weight = torch.rand(2, 12)          # [L=2, E=12]

# ① E % G != 0：13 个专家分不成 4 组
try:
    eplb.rebalance_experts_hierarchical(torch.rand(2, 13), 16, 4, 2, 8)
except AssertionError as e:
    print("①", e)

# ② G % N != 0：4 个组分不到 3 个节点（注意：走入口 rebalance_experts
#    不会报错，因为入口会改走全局策略——必须直接调用层级函数）
try:
    eplb.rebalance_experts_hierarchical(weight, 16, 4, 3, 8)
except AssertionError as e:
    print("②", e)

# ③ P % N != 0：8 块 GPU 分不到 3 个节点
try:
    eplb.rebalance_experts_hierarchical(weight, 16, 4, 2, 8 + 1)
except AssertionError as e:
    print("③", e)

# ④ M % P != 0：18 个物理专家平分不到 8 块 GPU
try:
    eplb.rebalance_experts_hierarchical(weight, 18, 4, 2, 8)
except AssertionError as e:
    print("④", e)

# ⑤ M < E：四条整除断言全过，死在 replicate_experts 内部
try:
    eplb.rebalance_experts_hierarchical(weight, 8, 4, 2, 8)
except AssertionError as e:
    print("⑤", e)
```

**需要观察的现象**：①—④ 是空的 `AssertionError`（`assert cond` 不带消息），⑤ 同样来自 `assert num_redundant >= 0`。

**预期结果**：五个分支全部打印 `AssertionError`（消息为空字符串）。`num_groups=4, num_nodes=3` 这组参数若改走入口 `eplb.rebalance_experts(weight, 16, 4, 3, 8)` 则**不会**报错——它被分派到全局策略。以上为源码逻辑推断，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要 \(G \bmod N = 0\)，而不仅仅是 \(P \bmod N = 0\)？

**答案**：组是不可拆分的放置原子。若某节点分到「半个组」，这半个组的专家就必须跨节点，违反组受限路由的约束。\(G \bmod N = 0\) 保证每个节点分到整数个组；\(P \bmod N = 0\) 只保证硬件（GPU 数）均分，不保证放置约束。

**练习 2**：`phy_experts_per_gpu`（\(M/P\)）在 Step 1、Step 2 中用到过吗？

**答案**：没有。前两步使用的物理预算是**每节点**的 \(M/N\)（见 [eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113)）。\(M/P\) 直到 Step 3 的 `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`（[eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)）才首次出现。这印证了三步流水线各自持有不同的粒度：组 → 节点 → GPU。

**练习 3**：若 `weight` 是 `[L, E]` 的浮点张量，函数内为什么没有任何针对 `weight` 的预处理？

**答案**：预处理发生在入口 `rebalance_experts` 的 `weight = weight.float().cpu()`（[eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149)）。层级函数假定自己收到的已是 CPU float 张量；直接调用它时传 GPU 张量会在 `balanced_packing` 内部被 `.cpu()`（[eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27)）拉回 CPU，但 Step 2 之后的张量设备一致性需要调用者自己负责（这是 u3-l3 的主题）。

---

### 4.2 模块二：Step 1（上）——`unflatten + sum` 统计组负载与组到节点装箱

#### 4.2.1 概念说明

要决定「哪些组放哪个节点」，先把决策粒度从专家升到组：需要每个组的总负载。组是按逻辑专家编号**连续切块**定义的（u1-l2）：组 \(g\) 恰好包含编号 \([g \cdot s,\ (g+1) \cdot s)\) 的 \(s\) 个专家。

于是组负载就是块内求和：

\[ \text{tokens\_per\_group}[l, g] = \sum_{o=0}^{s-1} \text{weight}[l,\ g \cdot s + o] \]

为什么用求和而不是最大值或平均值？因为节点负载是**可加的**：一个节点分到若干组后，它的总负载就是这些组负载之和。求和是与「装箱时包重累加」语义一致的统计量。

拿到 `[L, G]` 的组负载后，「组 → 节点」的分配问题就是一个现成的 `balanced_packing` 实例：物品 = 组（\(G\) 个），包 = 节点（\(N\) 个），物品权重 = 组负载。这正是 u2-l1 精读过的那个函数被**第一次**调用的场景（第二次在 Step 3 打包物理专家到 GPU）。

#### 4.2.2 核心流程

`unflatten` 把最后一维 \(E\) 零拷贝地视为 \((G, s)\) 两维，`sum(-1)` 消掉组内维度：

```text
weight:               [L, E]
    .unflatten(-1, (G, s))    → [L, G, s]     # 最后一切成 (组, 组内偏移)
    .sum(-1)                  → [L, G]        # 组内求和
balanced_packing([L,G], N)    → group_pack_index      [L, G]   # 组 → 节点号
                              → group_rank_in_pack    [L, G]   # 组在节点内的序号
```

装箱内部的贪心过程（u2-l1）：每层独立，组按负载降序处理，放入「未满（已装组数 < \(G/N\)）且当前累计负载最轻」的节点，并列时取编号最小的节点。

#### 4.2.3 源码精读

**Step 1 的前两行**。[eplb.py:103-105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L105)：

```python
# Step 1: pack groups to nodes
tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes) 
```

第一行把 `[L, E]` 重排成 `[L, G, s]` 再沿最后一维求和，得到每层每组的 token 数；第二行把组装箱到节点。`balanced_packing` 的关键行为（[eplb.py:27-41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L41)）在组粒度上的具体化：第 27 行对组负载降序排序，第 34-35 行的 `min((i for i in ...), key=pack_weights.__getitem__)` 选出「还有容量且累计负载最小」的节点，第 39-40 行累加节点负载与已装组数。基数约束（每节点恰好 \(G/N\) 个组）由第 34 行的 `pack_items[i] < groups_per_pack` 过滤保证——这个约束不仅为了均衡，也是下一模块复合编码可逆的前提。

一个值得注意的细节：`balanced_packing` 返回的张量在 CPU 上（它内部第 27 行做了 `.cpu()`，第 28 行显式 `device='cpu'`）。因此 Step 1 第 107 行的 `arange` 特意写了 `device=group_pack_index.device` 来对齐设备——这是全函数设备一致性的第一处伏笔。

**用 README 示例手算**。取 [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) 的两层 12 专家权重（\(s=3,\ G=4,\ N=2\)，故每节点 2 个组）：

第 0 层 `weight = [90, 132, 40, 61, 104, 165, 39, 4, 73, 56, 183, 86]`，按 3 个一组求和：

| 组 | 成员专家（编号） | 组负载 |
|---|---|---|
| g0 | 0, 1, 2 | \(90+132+40=262\) |
| g1 | 3, 4, 5 | \(61+104+165=330\) |
| g2 | 6, 7, 8 | \(39+4+73=116\) |
| g3 | 9, 10, 11 | \(56+183+86=325\) |

降序处理顺序：g1(330) → g3(325) → g0(262) → g2(116)。装箱过程：

| 处理 | 选择 | 理由 | 节点负载变化 |
|---|---|---|---|
| g1 | 节点 0 | 两节点皆空，并列取小 | [330, 0] |
| g3 | 节点 1 | 节点 1 更轻（0 < 330） | [330, 325] |
| g0 | 节点 1 | 都有容量，325 < 330 | [330, 587] |
| g2 | 节点 0 | 节点 1 已满 2 个组 | [446, 587] |

第 0 层结果：`group_pack_index = [1, 0, 0, 1]`，`group_rank_in_pack = [1, 0, 1, 0]`，节点负载 **446 : 587**（总计 1033）。

第 1 层组负载为 231, 280, 516, 129（g0—g3），同法手算得 `group_pack_index = [1, 1, 0, 0]`，`group_rank_in_pack = [1, 0, 0, 1]`，节点负载 **645 : 511**（总计 1156）。注意两层选择了**不同的**组→节点方案——每个 MoE 层独立决策，这是 `weight` 第一维存在的意义。

#### 4.2.4 代码实践

**实践目标**：用代码复现上面的手算，确认你对 `unflatten + sum` 与装箱过程的理解。

**操作步骤**（交互式 Python 即可，不必复制函数）：

```python
# 示例代码
import torch
weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                       [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
G, N = 4, 2
s = weight.shape[1] // G
tokens_per_group = weight.unflatten(-1, (G, s)).sum(-1)
print(tokens_per_group)                 # 预期 [[262,330,116,325],[231,280,516,129]]

import eplb
pack, rank = eplb.balanced_packing(tokens_per_group, N)
print(pack)                             # 预期 [[1,0,0,1],[1,1,0,0]]
print(rank)                             # 预期 [[1,0,1,0],[1,0,0,1]]
print(tokens_per_group.gather(-1, (pack * N + rank).argsort(-1)))  # 排序后可读出节点负载
```

**需要观察的现象**：`tokens_per_group` 是否等于手算的组负载；`pack`/`rank` 是否与手算一致；最后一行按「节点号 × 2 + 组内序号」编码排序后，前 2 个值之和、后 2 个值之和是否分别是两个节点的总负载。

**预期结果**：第 0 层节点负载 446 与 587，第 1 层 645 与 511。以上为手算推演值，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `sum(-1)` 换成 `mean(-1)`，装箱结果会变吗？

**答案**：不会。每组大小同为 \(s\)，均值 = 和 ÷ 常数，所有组负载等比例缩放，降序顺序与「最轻包」比较结果都不变。但 `sum` 才是语义正确的量——后续节点负载累加、与每节点物理预算比较时都依赖可加性，用 `mean` 会让数值失去「token 数」含义。

**练习 2**：装箱的物品为什么是组而不是单个专家？

**答案**：两个原因。其一（约束）：组受限路由要求组不可拆散，专家级装箱会产生「同组专家分散在多节点」的解，不可行。其二（目标层级）：Step 1 的目标是节点级均衡，组是节点能持有的最小单位；专家级与 GPU 级均衡分别交给 Step 2 的复制和 Step 3 的装箱。

**练习 3**：若 \(G = N\)（每节点恰好 1 个组），这一步会发生什么？

**答案**：`balanced_packing` 中 `groups_per_pack = 1`，命中 [eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) 的平凡分支提前返回：每个组就是自己所在包的第 0 个物品，`pack_index = [0..N-1]`，`rank_in_pack` 全 0。装箱退化为恒等映射，但仍输出统一接口供下游编码使用。

---

### 4.3 模块三：Step 1（下）——`log2mlog` 复合编码与 `mlog2log` 逆置换

#### 4.3.1 概念说明

装箱之后我们知道了每个组的 `(节点号 pack_index, 节点内组序号 rank_in_pack)`，但后续 Step 2 需要的是**一个统一的编号系统**，满足：

1. **同节点的专家占据连续编号块**——这样才能用一次 `view` 把所有节点的专家切成分离的行；
2. **同组的专家仍连续**——组块结构不破坏；
3. **是 \([0, E)\) 上的双射**——不丢专家、不重号。

这个新编号就是 **mlog**（按节点分块重编号的逻辑专家，命名来自 u1-l4 的术语表）。`log2mlog` 是「旧逻辑编号 → mlog 编号」的翻译表，`mlog2log` 是它的逆。改编号 = 改布局：这正是 u2-l3 的核心结论「布局即编号方案」的又一次应用——Step 1 表面上只是「算了个新编号」，实际上已经完成了组到节点的**放置**。

#### 4.3.2 核心流程

对逻辑专家 \(j\)，记其所在组 \(g = \lfloor j / s \rfloor\)、组内偏移 \(o = j \bmod s\)，则：

\[ \text{log2mlog}[j] \;=\; \big(\,p_g \cdot \tfrac{G}{N} \;+\; r_g\,\big) \cdot s \;+\; o \]

其中 \(p_g\)、\(r_g\) 分别是该组的节点号与节点内组序号。这是一个**混合进制编码**：把三元组 \((p_g,\ r_g,\ o)\) 当作三位数字，从低位到高位的进位基数分别是 \(s\)、\(G/N\)、\(N\)。展开成「基地址 + 偏移」形式更直观：

\[ \underbrace{p_g \cdot \frac{E}{N}}_{\text{节点基地址}} \;+\; \underbrace{r_g \cdot s}_{\text{组块基地址}} \;+\; \underbrace{o}_{\text{组内偏移}} \]

（因为 \(p_g \cdot \frac{G}{N} \cdot s = p_g \cdot \frac{E}{N}\)。）三位数字的取值范围 \(N \times \frac{G}{N} \times s = G \cdot s = E\)，恰好铺满 \([0, E)\)，所以是双射——**前提是每个节点内 \(r_g\) 不重复**，这由 `balanced_packing` 的基数约束保证（练习 2 会用到）。

编码的关键性质：

- \( \lfloor \text{mlog} / (E/N) \rfloor = p_g\)：mlog 编号的**节点块**连续，块边界是 \(E/N\) 的整数倍；
- 同一个组的 \(s\) 个专家映射到连续 mlog 区间 \([r' \cdot s,\ r' \cdot s + s)\)（\(r'\) 为全局展平后的组槽位）；
- 不同组、不同节点的区间互不重叠。

`mlog2log = inverse(log2mlog)` 用 u2-l3 的 `scatter_` 逆置换求得，满足 `mlog2log[log2mlog[j]] = j`。

#### 4.3.3 源码精读

**Step 1 的后两行**。[eplb.py:106-108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L108)：

```python
log2mlog = (((group_pack_index * groups_per_node + group_rank_in_pack) * group_size).unsqueeze(-1) + 
            torch.arange(group_size, dtype=torch.int64, device=group_pack_index.device)).flatten(-2)
mlog2log = inverse(log2mlog)
```

分三步读：

1. **算组块基地址**：`(pack_index * groups_per_node + rank_in_pack) * group_size`。内层括号把「节点号 × 每节点组数 + 节点内组序号」合成节点内的组槽位号 \(p_g \cdot \frac{G}{N} + r_g \in [0, E/N)\)，再乘 \(s\) 得到该组 mlog 块的首地址。形状 `[L, G]`。
2. **加组内偏移**：`unsqueeze(-1)` 变 `[L, G, 1]`，加 `arange(group_size)` 广播出每个组内专家的偏移 \(0..s-1\)，得 `[L, G, s]`。注意 `device=group_pack_index.device`——因为 `balanced_packing` 的输出在 CPU 上（模块 4.2 提到的细节），这里必须对齐。
3. **压平**：`flatten(-2)` 把最后两维合并，`[L, G, s] → [L, E]`。因为 `unflatten` 本来就是按 `(G, s)` 行主序切的，压平后第 \(g \cdot s + o\) 列恰好对应「组 \(g\) 的第 \(o\) 个专家」，与 `weight` 的列语义对齐。

最后 `inverse(log2mlog)`（[eplb.py:108](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L108)，函数体在 [eplb.py:98-101](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L98-L101)）得到逆表。

**接 README 手算**。第 0 层的装箱结果是 `pack=[1,0,0,1]`、`rank=[1,0,1,0]`（按组 g0—g3 排列），代入公式：

| 组 | \(p_g\) | \(r_g\) | 组块首地址 \((p_g \cdot 2 + r_g) \cdot 3\) | 专家映射 |
|---|---|---|---|---|
| g0 | 1 | 1 | \((1\cdot2+1)\cdot3 = 9\) | 0,1,2 → 9,10,11 |
| g1 | 0 | 0 | \(0\) | 3,4,5 → 0,1,2 |
| g2 | 0 | 1 | \(3\) | 6,7,8 → 3,4,5 |
| g3 | 1 | 0 | \(6\) | 9,10,11 → 6,7,8 |

于是第 0 层 `log2mlog = [9,10,11, 0,1,2, 3,4,5, 6,7,8]`，`mlog2log = [3,4,5, 6,7,8, 9,10,11, 0,1,2]`。验证节点块：mlog \(0..5\)（节点 0）对应逻辑专家 \(\{3..8\}\) = 组 g1+g2，负载 \(330+116=446\)；mlog \(6..11\)（节点 1）对应 \(\{9,10,11,0,1,2\}\) = 组 g3+g0，负载 \(325+262=587\)——与模块 4.2 的装箱结果严格一致。第 1 层同法得 `log2mlog = [9,10,11, 6,7,8, 0,1,2, 3,4,5]`。

#### 4.3.4 代码实践

**实践目标**：独立实现编码公式并验证三条性质（双射、节点块连续、组块连续）。

**操作步骤**：

```python
# 示例代码：从上一实践得到的 pack/rank 出发
import torch
E, G, N, s, L = 12, 4, 2, 3, 2
pack = torch.tensor([[1,0,0,1],[1,1,0,0]])
rank = torch.tensor([[1,0,1,0],[1,0,0,1]])

log2mlog = (((pack * (G//N) + rank) * s).unsqueeze(-1) +
            torch.arange(s)).flatten(-2)
inv = torch.empty_like(log2mlog)
inv.scatter_(1, log2mlog, torch.arange(E).expand(L, E))   # 手写 inverse

# ① 双射：逆置换复合回恒等
assert torch.equal(inv.gather(-1, log2mlog), torch.arange(E).expand(L, E))
# ② 节点块对齐：每个逻辑专家的所在节点号 == 其 mlog 编号所在的节点块
assert torch.equal(pack.repeat_interleave(s, dim=-1), log2mlog // (E // N))
# ③ 组块连续：同组专家的 mlog 编号除以 s 后得到同一个「组槽位号」
#    注意 log2mlog // s 是新编号下的组槽位 (pack*(G//N)+rank)，不是原始组编号 g
assert torch.equal(log2mlog // s, (pack * (G//N) + rank).repeat_interleave(s, dim=-1))
print(log2mlog)   # 预期 [[9,10,11,0,1,2,3,4,5,6,7,8],[9,10,11,6,7,8,0,1,2,3,4,5]]
print(inv)        # 预期 [[3,4,5,6,7,8,9,10,11,0,1,2],[6,7,8,9,10,11,0,1,2,3,4,5]]
```

**需要观察的现象**：打印的 `log2mlog` / `inv` 是否与手算表一致；三条断言是否全部通过（①验证可逆，②验证节点块边界恰为 \(E/N\) 的整数倍，③验证组块边界恰为 \(s\) 的整数倍）。

**预期结果**：两个张量与 4.3.3 的手算值完全一致，断言全部通过。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：公式里乘的是 `group_size`，为什么不是 `groups_per_node`？

**答案**：混合进制中每一位的权重等于**所有更低位的基数之积**。偏移 \(o\) 是最低位（基数 \(s\)），所以组序号位的权重是 \(s\)；节点位的权重是 \(\frac{G}{N} \cdot s = \frac{E}{N}\)。若乘错基数，不同组的区间会重叠或错位，编码不再是双射。

**练习 2**：如果允许两个组在**同一节点**内拿到相同的 `rank_in_pack`，会发生什么？

**答案**：两组的 mlog 区间完全重叠，`log2mlog` 出现重复值，不再是置换；`inverse` 用 `scatter_` 求逆时重复位置互相覆盖，专家丢失。`balanced_packing` 的基数约束（`pack_items[i] < groups_per_pack` 过滤，[eplb.py:34](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34)）保证包内序号 \(0..\frac{G}{N}-1\) 不重复，是编码可逆的隐含前提——这是「装箱基数约束」与「编号方案」之间一条容易被忽略的依赖链。

**练习 3**：不写代码，说出 `log2mlog` 每一行作为多重集合的特点。

**答案**：每一行都是 \([0, E)\) 的一个置换（值集合恰为 \(0..E-1\)），且按 \(s\) 个一组切块后，块的整体顺序由装箱决定、块内顺序保持组内原有相对顺序（稳定：偏移 \(o\) 直接加在块首地址上）。

---

### 4.4 模块四：Step 2——`gather` 重排、`view` 合批与节点内复制

#### 4.4.1 概念说明

Step 1 完成了放置决策，但数据（`weight`）还躺在旧逻辑编号里。Step 2 做两件事：

1. **重排**：按 `mlog2log` 把每层负载重排成 mlog 顺序。u2-l3 的结论：`gather` 的 index 取「目的地 → 来源」方向——`tokens_per_mlog[l, k] = weight[l, mlog2log[l, k]]`，即 mlog 编号 \(k\) 的专家去读它对应的旧编号负载。
2. **复制**：在**每个节点内部**独立运行 u2-l2 的 `replicate_experts` 贪心，把 \(E/N\) 个节点内逻辑专家复制成 \(M/N\) 个物理专家。

为什么复制必须限制在节点内？因为组（连同其副本）不可跨节点（u1-l2 的组约束）。mlog 编号恰好把节点边界固化成了编号块边界，于是「在块内复制」天然保证「副本不跨节点」——编号系统替我们守住了约束。

物理预算也按节点均分：总共 \(M\) 个物理槽位，每节点 \(M/N\) 个。这并非随意选择，而是 \(P \bmod N = 0\)、\(M \bmod P = 0\) 两条断言的推论：每节点 GPU 数相同、每 GPU 槽位相同，故每节点槽位 \(= \frac{P}{N} \cdot \frac{M}{P} = \frac{M}{N}\)。

#### 4.4.2 核心流程

```text
weight.gather(-1, mlog2log)        # [L, E]  按 mlog 顺序重排（层内：节点0块 | 节点1块 | ...）
       .view(-1, E/N)              # [L·N, E/N]  行 r = l·N + n，第 r 行 = (第 l 层, 第 n 节点)
replicate_experts(·, M/N)          # 每行独立贪心，循环 M/N − E/N 轮（与行数无关）
       → phy2mlog [L·N, M/N]       # 节点内物理槽位 → 节点内逻辑(mlog)编号
       → phyrank   [L·N, M/N]      # 副本序号
       → mlogcnt   [L·N, E/N]      # 每个 mlog 专家的副本数
```

`view` 能工作的原因藏在 mlog 编码里：重排后**每一层内部**，前 \(E/N\) 列恰好是节点 0 的专家，接下来 \(E/N\) 列是节点 1，以此类推。所以 `[L, E]` 可以无歧义地视为隐式的 `[L, N, E/N]`，行主序压平前两维就得 `[L·N, E/N]`，行号 \(r = l \cdot N + n\) 精确对应「第 \(l\) 层第 \(n\) 个节点」。`gather` 的输出是连续内存，`view` 合法且零拷贝（u2-l3）。

合批的收益（本讲学习目标 3）：`replicate_experts` 的贪心循环只执行 \(M/N - E/N\) 轮，**每一轮对所有行同时做向量化 `max`**（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)，u2-l2 已精读）。行数 \(L \cdot N\) 完全不进入循环次数——不管多少层、多少节点，复制决策都由**一次**函数调用完成。若没有 `view` 合批，就得写 `for l in range(L): for n in range(N):` 的双层 Python 循环，规模上去后开销可观（u3-l3 会回到这个话题）。

#### 4.4.3 源码精读

**Step 2 全部两行**。[eplb.py:110-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113)：

```python
# Step 2: construct redundant experts within nodes
# [num_layers * num_nodes, num_logical_experts // num_nodes]
tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
phy2mlog, phyrank, mlogcnt = replicate_experts(tokens_per_mlog, num_physical_experts // num_nodes)    
```

第一行：`gather(-1, mlog2log)` 按「mlog → log」方向读出负载（重申方向约定：`gather` 的 index 是目的地到来源的映射），再 `view` 成 `[L·N, E/N]`，源码注释直接标明了这个形状。第二行：以 `num_phy = M/N` 调用 `replicate_experts`。

`replicate_experts` 内部（[eplb.py:62-70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L70)）：初始化 `phy2mlog = arange(M/N)`、`logcnt = 1`；随后循环 \(i\) 从 \(E/N\) 到 \(M/N\)，每轮第 67 行对**所有行**同时取 `(weight / logcnt).max(-1)`，第 68-70 行写入新副本并更新 `logcnt`。输出的编号全部是**节点内局部**的 mlog 编号——它们要到 Step 3 末尾经过 `pphy2mlog` 加节点基地址、`mlog2log` 翻译，才变回全局逻辑编号（下一讲的主线）。

**接 README 手算**。\(E/N = 6\)，\(M/N = 8\)，故每行复制 \(8-6=2\) 个专家，`[L·N, E/N]` 共 4 行（第 0、1 行 = 第 0 层节点 0/1，第 2、3 行 = 第 1 层节点 0/1）：

| 行 | (层, 节点) | `tokens_per_mlog`（按 mlog2log 读出） | 两轮复制选择 | `mlogcnt` |
|---|---|---|---|---|
| 0 | (0, 0) | [61, 104, 165, 39, 4, 73] | mlog 2（165），再 mlog 1（104/1 > 165/2=82.5） | [1,2,2,1,1,1] |
| 1 | (0, 1) | [56, 183, 86, 90, 132, 40] | mlog 1（183），再 mlog 4（132） | [1,2,1,1,2,1] |
| 2 | (1, 0) | [187, 157, 172, 86, 16, 27] | mlog 0（187），再 mlog 2（172） | [2,1,2,1,1,1] |
| 3 | (1, 1) | [20, 107, 104, 64, 19, 197] | mlog 5（197），再 mlog 1（107） | [1,2,1,1,1,2] |

以第 0 行为例走一遍贪心：初始 `logcnt` 全 1，`weight/logcnt = weight`，最大值 165 在 mlog 2 → 复制它，`logcnt[2]=2`；第二轮 `weight/logcnt = [61, 104, 82.5, 39, 4, 73]`，最大 104 在 mlog 1 → 复制它。注意第二轮**不会**再选 mlog 2：\(165/2 = 82.5 < 104\)，副本摊薄后它已不是「水位」最高的——这就是 u2-l2 讲的「拉平单副本期望负载」贪心。

把 `mlogcnt` 翻译回逻辑编号抽查一下：第 1 行（第 0 层节点 1）的 mlog 1 和 4 对应 `mlog2log` 第 0 层的 log 10（负载 183）和 log 1（负载 132）——正是该节点两个最重的专家。与 README 最终输出 `phy2log` 第 0 层 `[...,10, 9, 10, 2, 0, 1, 11, 1]` 中 10 与 1 各出现两次完全吻合（Step 3 只在节点内重排顺序，不改变副本集合）。

#### 4.4.4 代码实践

**实践目标**：不复制任何函数，只用 `eplb` 模块的两个公开构件，独立复现 Step 1 + Step 2，并验证两个不变量。

**操作步骤**（`balanced_packing` 与 `replicate_experts` 都是模块级函数，可直接调用——`__all__` 只影响 `from eplb import *`）：

```python
# 示例代码：Step 1+2 复现器
import torch, eplb

weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                       [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
E, G, N, M = 12, 4, 2, 16
s = E // G

# ---- Step 1 ----
tokens_per_group = weight.unflatten(-1, (G, s)).sum(-1)
pack, rank = eplb.balanced_packing(tokens_per_group, N)
log2mlog = (((pack * (G // N) + rank) * s).unsqueeze(-1) + torch.arange(s)).flatten(-2)
inv = torch.empty_like(log2mlog)
inv.scatter_(1, log2mlog, torch.arange(E).expand_as(log2mlog))

# ---- Step 2 ----
tokens_per_mlog = weight.gather(-1, inv).view(-1, E // N)
phy2mlog, phyrank, mlogcnt = eplb.replicate_experts(tokens_per_mlog, M // N)

print(tokens_per_mlog)   # 预期见 4.4.3 表格
print(mlogcnt)           # 预期 [[1,2,2,1,1,1],[1,2,1,1,2,1],[2,1,2,1,1,1],[1,2,1,1,1,2]]

# 不变量 A：每行负载之和 == 该层总负载（重排不增不减）
assert torch.equal(tokens_per_mlog.view(2, N, -1).sum(-1).sum(-1), weight.sum(-1))
# 不变量 B：每行 mlogcnt 之和 == M/N（副本预算按节点均分）
assert torch.equal(mlogcnt.sum(-1), torch.full((4,), M // N))
```

**需要观察的现象**：`tokens_per_mlog` 每两行拼起来是否还原该层全部 12 个负载（只是顺序变了）；`mlogcnt` 中值为 2 的位置是否恰好落在该行最重的两个专家上。

**预期结果**：与 4.4.3 手算表格逐项一致；两条断言通过。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`gather` 里为什么放 `mlog2log` 而不是 `log2mlog`？

**答案**：`gather` 是读侧重排，index 必须按「目的地 → 来源」方向组织（u2-l3）。目标张量按 mlog 编号排列，位置 \(k\) 要填的是 mlog \(k\) 对应的旧编号负载，即 `weight[mlog2log[k]]`。若误用 `log2mlog`，得到的是「旧编号 \(j\) 位置放上 mlog \(j\) 的负载」——方向相反，虽然也是置换但后续 `view` 切出的节点块就是错的。

**练习 2**：把 `view(-1, E//N)` 换成 `reshape(-1, E//N)`，行为有区别吗？

**答案**：此处没有区别——`gather` 输出是连续张量，两者都做零拷贝重排。区别在约束强度：`view` 要求内存连续或 stride 兼容，不满足会直接报错；`reshape` 必要时默默拷贝。源码用 `view` 表达「这里只是重新切维度，不应发生拷贝」的意图，兼带自检作用。

**练习 3**：若某一行（某层某节点）的负载本来就完全均匀，`replicate_experts` 还会复制它吗？

**答案**：会。每个节点必须凑满 \(M/N\) 个物理槽位（显存已按此预留），复制轮数是 \(M/N - E/N\)，与负载形态无关。均匀时 `weight/logcnt` 并列最大，`max` 取第一个达到最大值的下标——贪心照常运行，只是「复制谁」的选择退化为并列中编号最小者，均衡性不受损。

---

## 5. 综合实践

**任务**：按本讲规格复制 `rebalance_experts_hierarchical` 为实验版本，在 Step 1、Step 2 之后插入打印，输出 `tokens_per_group`、`log2mlog`、`mlog2log`、`tokens_per_mlog`、`mlogcnt` 等中间张量，并验证「组划分后每个节点的总负载是否平衡」。

**操作步骤**：新建 `hierarchical_debug.py`，内容如下（Step 3 保持原样，这样还能顺手对照最终 `phy2log`）：

```python
# 示例代码：hierarchical_debug.py
import torch
from typing import Tuple

def rebalance_experts_hierarchical_debug(weight, num_physical_experts,
                                         num_groups, num_nodes, num_gpus):
    # ===== 以下与 eplb.py:89-101 相同 =====
    num_layers, num_logical_experts = weight.shape
    assert num_logical_experts % num_groups == 0
    group_size = num_logical_experts // num_groups
    assert num_groups % num_nodes == 0
    groups_per_node = num_groups // num_nodes
    assert num_gpus % num_nodes == 0
    assert num_physical_experts % num_gpus == 0
    phy_experts_per_gpu = num_physical_experts // num_gpus

    def inverse(perm):
        inv = torch.empty_like(perm)
        inv.scatter_(1, perm, torch.arange(perm.size(1), dtype=torch.int64,
                                           device=perm.device).expand(perm.shape))
        return inv

    # ===== Step 1: eplb.py:103-108，加打印 =====
    tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
    group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes)
    log2mlog = (((group_pack_index * groups_per_node + group_rank_in_pack) * group_size
                ).unsqueeze(-1) +
                torch.arange(group_size, dtype=torch.int64,
                             device=group_pack_index.device)).flatten(-2)
    mlog2log = inverse(log2mlog)
    print("[Step1] tokens_per_group =\n", tokens_per_group)
    print("[Step1] group_pack_index =\n", group_pack_index)
    print("[Step1] group_rank_in_pack =\n", group_rank_in_pack)
    print("[Step1] log2mlog =\n", log2mlog)
    print("[Step1] mlog2log =\n", mlog2log)

    # ===== Step 2: eplb.py:110-113，加打印 =====
    tokens_per_mlog = weight.gather(-1, mlog2log).view(
        -1, num_logical_experts // num_nodes)
    phy2mlog, phyrank, mlogcnt = replicate_experts(
        tokens_per_mlog, num_physical_experts // num_nodes)
    print("[Step2] tokens_per_mlog =\n", tokens_per_mlog)
    print("[Step2] phy2mlog =\n", phy2mlog)
    print("[Step2] mlogcnt =\n", mlogcnt)

    # ===== 节点负载均衡性检查 =====
    L, N = num_layers, num_nodes
    node_load = tokens_per_mlog.sum(-1).view(L, N)      # 行 r=l*N+n 求和再还原 [L,N]
    layer_total = weight.sum(-1)
    print("[Check] 每层节点负载 =\n", node_load)
    print("[Check] 每层负载比 max/min =",
          (node_load.max(-1).values / node_load.min(-1).values).tolist())
    assert torch.allclose(node_load.sum(-1), layer_total)   # 总量守恒
    assert torch.equal(mlogcnt.sum(-1),
                       torch.full((L * N,), num_physical_experts // num_nodes))

    # ===== Step 3: eplb.py:115-129 原样保留（见下一讲） =====
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
    pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
    phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
    pphy2phy = inverse(phy2pphy)
    pphy2mlog = phy2mlog.gather(-1, pphy2phy)
    pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) +
                 torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                              device=group_pack_index.device).view(1, -1, 1)).flatten(-2)
    pphy2log = mlog2log.gather(-1, pphy2mlog)
    pphyrank = phyrank.gather(-1, pphy2phy).view(num_layers, -1)
    logcnt = mlogcnt.view(num_layers, -1).gather(-1, log2mlog)
    return pphy2log, pphyrank, logcnt

from eplb import balanced_packing, replicate_experts   # 复用真构件，别抄实现

if __name__ == "__main__":
    weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                           [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
    pphy2log, pphyrank, logcnt = rebalance_experts_hierarchical_debug(
        weight, 16, 4, 2, 8)
    print("[Final] pphy2log =\n", pphy2log)   # 应与 README 的 phy2log 输出一致
```

**需要观察的现象**：

1. `[Step1]` 打印是否与讲义 4.2.3、4.3.3 的手算表一致（`tokens_per_group`、`pack/rank`、两张互逆置换表）。
2. `[Step2]` 打印是否与 4.4.3 的表格一致，`mlogcnt` 里 2 的位置是否都是该行最重专家。
3. `[Check]` 每层节点负载：手算预期第 0 层为 `[446, 587]`、第 1 层为 `[645, 511]`，负载比分别约 1.32 与 1.26。可以再算一个对照基线——不做装箱、按原始编号顺序每组顺序切分（g0,g1→节点 0；g2,g3→节点 1），第 0 层负载为 `[592, 441]`（比 ≈ 1.34），体会 Step 1 带来的节点级改善幅度（第 1 层两种方案凑巧相同，均为 `[511, 645]`）。
4. `[Final]` 的 `pphy2log` 是否与 README 记载的 `phy2log` 输出一致——这同时验证了你复制的 Step 3 没抄错，为下一讲做好了数据准备。

**预期结果**：所有打印与手算值一致、两条 `assert` 通过、最终输出与 README 吻合。以上预期值均为本讲义手工推演，待本地验证。

**思考延伸**：把 `num_groups` 从 4 改成 2（`s=6`）重跑，观察 `tokens_per_group` 变粗后节点负载比如何变化；再把 `weight` 换成长尾分布（如 `torch.pareto` 采样取正），观察 `mlogcnt` 的集中程度。为什么组越少，节点级均衡越难做？（提示：装箱物品变大、基数约束变紧。）

## 6. 本讲小结

- **Step 1 = 组装箱 + 换编号**：`unflatten + sum` 把负载升到组粒度，`balanced_packing` 以「每节点恰好 \(G/N\) 个组」的基数约束做降序贪心装箱；`log2mlog` 用混合进制 \((\)节点号 \(\times\, E/N) + (\)组序号 \(\times\, s) +\) 组内偏移，把放置决策固化成一张可逆的编号置换表。
- **编号即放置**：mlog 编号让「同节点专家占据连续块、同组专家保持连续」，`log2mlog`/`mlog2log` 互为 `scatter_` 逆置换；装箱的基数约束是编码双射的隐含前提。
- **Step 2 = 重排 + 合批 + 节点内贪心**：`gather` 按「目的地 → 来源」方向用 `mlog2log` 重排负载，`view` 把 `[L, E]` 折成 `[L·N, E/N]`（行 \(= l \cdot N + n\)），使所有「层 × 节点」的复制在**一次** `replicate_experts` 调用中行向量化完成，循环轮数只依赖 \(M/N - E/N\)。
- **约束由编号守护**：副本只出现在节点块内部（`mlogcnt` 按行计数），物理预算按节点均分 \(M/N\)，组约束与显存均分在进入 Step 3 之前就已经成立。
- **每层独立**：两层示例给出了不同的组→节点方案，`weight` 的第一维贯穿所有中间张量。
- 所有中间量的手算值都已给出，综合实践脚本可逐项核对，并最终对上 README 的 `phy2log`。

## 7. 下一步学习建议

下一讲 **u2-l5《层级策略第三步：GPU 打包与多层映射链合成》** 精读 [eplb.py:115-129](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L129)：`tokens_per_phy = tokens_per_mlog / mlogcnt` 再 `gather` 得副本级负载、`balanced_packing` 的第二次调用（物理专家 → GPU）、以及 `phy2pphy → pphy2phy → pphy2mlog → pphy2log` 四层映射如何复合，把本讲得到的节点内局部编号一路翻译回全局逻辑编号。建议带着本讲综合实践打印出的 `phy2mlog`、`mlogcnt` 数据去读——u2-l5 的追踪练习正好从它们出发。之后再进入 u2-l6 看入口分派与全局策略如何以退化参数复用本讲的整条流水线。
