# balanced_packing：带容量约束的贪心装箱

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `balanced_packing` 解决的是哪一类问题：**每包物品数固定为 n/m、且各包总重量尽量接近**的均衡装箱问题，以及这个"基数约束"为什么在 EPLB 中是硬性要求。
2. 逐行读懂贪心主循环：为什么先按权重**降序排序**，再把每个物品放进"**仍有空位且当前最轻**"的包，并能手工模拟整个过程。
3. 掌握两个代码级细节：`min(生成器, key=...)` 这一 Python 选择惯用法（含变量遮蔽陷阱），以及 `groups_per_pack == 1` 平凡分支的向量化构造与它在全局策略中的触发时机。

本讲只聚焦 `balanced_packing` 这一个函数。它在 `eplb.py` 中被调用两次（组打包到节点、物理专家打包到 GPU），是层级策略三步流程中每一步的"承重墙"，但函数本身完全自洽，不依赖 MoE 领域知识，可以当成一个纯算法问题来读。

## 2. 前置知识

### 2.1 装箱与划分问题：为什么"均衡"是难的

把 n 个带权重的物品分给 m 个包，让各包总重量尽量接近——这个问题家族在计算机科学里研究了几十年：

| 问题 | 目标 | 约束 |
|---|---|---|
| 经典装箱（bin packing） | 用最少的箱子装下所有物品 | 每箱有容量上限 |
| 数字划分（number partitioning） | 各堆重量差尽量小 | 物品数任意 |
| **balanced_packing** | 各包重量尽量均衡（最小化最重包） | **每包恰好 n/m 个物品** |

这三类问题本质上都是 NP-难的：想要全局最优，原则上要枚举指数级多的分配方案。所以实践中都用**贪心启发式**：按某种规则逐个放物品，每一步只做局部最优选择，用多项式时间换"足够好"的解。

### 2.2 LPT（最长处理时间优先）规则

LPT（Longest Processing Time first）是均衡调度最经典的贪心规则：**把任务按耗时从大到小排序，逐个把任务交给"当前最闲"的机器**。对于最小化最长机器完工时间（makespan，即"木桶里最长的板"）的调度问题，经典理论给出了 LPT 的近似比保证（无基数约束情形）：

\[ \text{LPT} \le \left(\frac{4}{3} - \frac{1}{3m}\right)\cdot \text{OPT} \]

`balanced_packing` 是 LPT 的**变体**：它额外带"每包恰好 n/m 个物品"的基数约束，所以上述理论界不能直接照搬，但核心直觉完全一致，可以用一句话概括：

> **大物品先放，此时所有包都还空、选择余地最大；小物品最后放，正好用来填缝隙。反过来先放小物品，小物品会把"坑位"均匀占掉，最后几个大物体无处可去，只能叠在同一包里，造成严重倾斜。**

### 2.3 需要用到的 PyTorch / Python 语法

- `torch.sort(t, dim, descending=...)`：沿某个维度排序，`.indices` 取排序后的**下标序列**（即"按权重从大到小，物品的原始编号依次是谁"）。
- `torch.full_like(t, fill_value)`：创建与 `t` 同形状、同设备的张量并填充哨兵值。
- `torch.arange(n).expand(shape)`：把一维序列广播成目标形状（`expand` 返回共享内存的视图，不复制数据，适合只读广播）。
- 生成器表达式 `(x for x in ... if ...)` 与 `min(iterable, key=func)`：Python 的"按条件选最优"惯用法，2.2 节的贪心核心就是一行这个。

如果不熟悉这些，本讲 4.2.3 节会结合源码逐个讲到，不必先去补文档。

## 3. 本讲源码地图

整个仓库的核心实现只有一个文件 `eplb.py`（约 165 行、四个函数）。本讲涉及的代码点如下：

| 位置 | 作用 |
|---|---|
| [eplb.py:L5-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L41) | `balanced_packing` 函数本体，本讲主角 |
| [eplb.py:L18-L20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L18-L20) | 形状解包与"物品数能被包数整除"断言 |
| [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25) | `groups_per_pack == 1` 的平凡分支 |
| [eplb.py:L27-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L41) | 降序排序 + 贪心主循环 |
| [eplb.py:L103-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L103-L107) | 调用点 1：层级策略 Step 1，把专家组打包到节点 |
| [eplb.py:L115-L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L115-L119) | 调用点 2：层级策略 Step 3，把物理专家打包到 GPU |
| [eplb.py:L149-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149-L156) | 入口函数：weight 搬到 CPU、按整除性分派策略（全局策略以退化参数复用层级实现） |

一句话定位：`balanced_packing` 是一个**纯函数式的算法积木**——输入权重矩阵，输出"每个物品去哪个包、在包里排第几"，不持有任何状态，被上层两次调用，分别完成"组→节点"和"物理专家→GPU"两级装箱。

## 4. 核心概念与源码讲解

### 4.1 问题定义：带基数约束的均衡装箱

#### 4.1.1 概念说明

先看 docstring 对问题的完整陈述：

```python
def balanced_packing(weight: torch.Tensor, num_packs: int) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Pack n weighted objects to m packs, such that each bin contains exactly n/m objects and the weights of all packs
    are as balanced as possible.

    Parameters:
        weight: [X, n], the weight of each item
        num_packs: number of packs

    Returns:
        pack_index: [X, n], the pack index of each item
        rank_in_pack: [X, n], the rank of the item in the pack
    """
```

引用自 [eplb.py:L5-L17](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L5-L17)：把 n 个带权物品装进 m 个包，要求**每个包恰好装 n/m 个物品**（"exactly"是硬约束），同时让各包总重量尽可能均衡。

用数学语言写清楚：给定权重 \( w_1, \dots, w_n \) 和 m 个包，求分配 \( \sigma: \{1,\dots,n\} \to \{1,\dots,m\} \)，满足基数约束

\[ \forall j:\ \left| \sigma^{-1}(j) \right| = \frac{n}{m} \]

并最小化最重包的负载（makespan）

\[ \min\ \max_{j=1}^{m}\ \sum_{i:\,\sigma(i)=j} w_i \]

**为什么基数约束是硬的？** 这是 EPLB 的物理约束决定的，不是算法偏好：

- 在调用点 2（物理专家 → GPU）中，每个专家的参数量相同、占用显存相同，每个 GPU 的显存和"专家槽位"数也相同，所以**每个 GPU 必须恰好放 num_replicas / num_gpus 个物理专家**，多一个放不下、少一个浪费。
- 在调用点 1（专家组 → 节点）中同理，每个节点要放恰好 num_groups / num_nodes 个组。

也就是说，"只均衡总重量、不管每包个数"的解在工程上是不可实施的。这个约束也解释了函数开头的断言（见 4.1.3）。

**注意两个输出都是"按物品组织"的**：`pack_index[i, g]` 回答"第 i 层的第 g 个物品被放进了哪个包"，`rank_in_pack[i, g]` 回答"它是那个包里的第几个（0 起）"。它们都不是按包组织的清单，而是与输入逐列对齐的两张"去向表"。第 0 维 X 是批维度（层数），**每一层独立求解一次装箱**，层与层互不影响。

#### 4.1.2 核心流程

```
输入 weight [X, n], num_packs = m
前提：n % m == 0，否则断言失败
groups_per_pack = n // m          # 每包物品数

对每一层 i（独立地）：
    把 n 个物品按权重降序排序，得到处理顺序
    初始化每个包的：当前重量 pack_weights = [0]*m
                    当前物品数 pack_items  = [0]*m
    依序取出每个物品 g：
        在所有"未满"（pack_items < groups_per_pack）的包里
        挑当前重量最小的包 p（并列取编号最小者）
        记录 pack_index[i, g] = p
        记录 rank_in_pack[i, g] = pack_items[p]   # 放进去之前的人数 = 它的名次
        pack_weights[p] += weight[i, g]
        pack_items[p] += 1

输出 pack_index [X, n], rank_in_pack [X, n]
```

整个函数只有两个分支：`groups_per_pack == 1` 时走 4.3 节的平凡分支直接向量化构造；否则走上面的贪心主循环（4.2 节逐行精读）。

#### 4.1.3 源码精读

先看函数体的前三行：

```python
    num_layers, num_groups = weight.shape
    assert num_groups % num_packs == 0
    groups_per_pack = num_groups // num_packs
```

引用自 [eplb.py:L18-L20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L18-L20)。这三行做了三件事：

1. 解包形状：第二维在这里叫 `num_groups`（"组数"），因为函数作者预期的主要调用场景是把**专家组**打包到节点（调用点 1）。但在调用点 2 它装的是物理专家——**变量名沿用了第一个调用场景的词汇，读代码时不要被名字限制住**，它就是"物品数 n"。
2. 断言物品数能被包数整除：这是"每包恰好 n/m 个物品"约束的前提。不满足时（例如 6 个物品装 4 个包）直接 `AssertionError`，绝不静默给出非法方案。这对应 u1-l2 讲过的四条整除断言家族。
3. 算出每包容量 `groups_per_pack`，它是后续容量判断的基准。

再对照两个真实调用点，看参数如何落到这个通用函数上：

```python
    tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
    group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes)
```

引用自 [eplb.py:L104-L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L105)。Step 1：先把每个专家的负载按组求和得到每组 token 数（形状 [层数, 组数]），再把 **n = num_groups 个"物品"（组）** 装进 **m = num_nodes 个"包"（节点）**，每节点恰好 `num_groups / num_nodes` 个组。

```python
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
    pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
```

引用自 [eplb.py:L117-L118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117-L118)。Step 3：把节点内 **n = num_physical_experts / num_nodes 个"物品"（物理专家）** 装进 **m = num_gpus / num_nodes 个"包"（该节点内的 GPU）**，每 GPU 恰好 `num_physical_experts / num_gpus` 个物理专家。

两次调用都是"同一算法、不同粒度"：先粗粒度（组）装箱定节点，再细粒度（副本）装箱定 GPU。这正是"hierarchical（分层）"名字的由来之一。这两处的上下文细节属于 u2-l4 / u2-l5 的内容，本讲只需记住：**任何"n 个东西均衡地分到 m 个槽位组、每组恰好 n/m 个"的需求，都归这个函数处理**。

#### 4.1.4 代码实践

**实践目标**：跑通最小调用，验证输出形状与断言行为，建立对接口的手感。

**操作步骤**（示例代码，保存为 `EPLB-tutorial/` 外任意目录的脚本或直接在解释器里执行均可）：

```python
# 示例代码：balanced_packing 最小调用实验
import torch
from eplb import balanced_packing   # __all__ 只约束 import *，显式导入没问题

# 1) 基本调用：2 层、8 个物品、2 个包
weight = torch.tensor([[8., 3., 7., 2., 5., 1., 6., 4.],
                       [1., 1., 1., 1., 1., 1., 1., 1.]])
pack_index, rank_in_pack = balanced_packing(weight, num_packs=2)
print("pack_index  =", pack_index)
print("rank_in_pack =", rank_in_pack)
print(pack_index.shape, rank_in_pack.shape)   # torch.Size([2, 8]) torch.Size([2, 8])

# 2) 验证槽位编码是 0..n-1 的置换：
#    (包号, 包内名次) 的二维坐标压成一维后应不重不漏
slot = pack_index * (8 // 2) + rank_in_pack
assert torch.equal(slot.sort(-1).values,
                   torch.arange(8).expand(2, 8)), "槽位编码不是置换"
print("slot =", slot)

# 3) 断言行为：6 个物品装 4 个包，无法均分
try:
    balanced_packing(torch.rand(1, 6), num_packs=4)
except AssertionError as e:
    print("按预期触发断言:", e)
```

**需要观察的现象**：

- 两个输出与输入同形状 `[2, 8]`，第一层的值与第二层无关（第二层权重全 1，结果是完全对称的平局分配）。
- `slot` 每一行都是 0~7 的某个排列——因为每包恰好 4 个物品、名次取遍 0~3，(包号, 名次) 二元组不重不漏。
- 第 3 步抛出 `AssertionError`，证明非法参数被显式拒绝而非静默处理。

**预期结果**：第一层的 `pack_index` / `rank_in_pack` 的具体数值在 4.2.4 手算后再对照（剧透：包负载为 18 对 18）。以上输出为笔算推导，**请以本地实际运行结果为准（待本地验证）**。

#### 4.1.5 小练习与答案

**练习 1**：调用 `balanced_packing(torch.rand(3, 12), num_packs=8)` 会发生什么？为什么？

答案：抛出 `AssertionError`。12 % 8 = 4 ≠ 0，无法把 12 个物品均分到 8 个包使每包物品数相同，[eplb.py:L19](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19) 的断言拦截。这也提醒我们：在 EPLB 里想用 8 GPU 放 12 个逻辑专家是不合法的，必须先通过冗余复制把物理专家数凑成 GPU 数的倍数。

**练习 2**：为什么"只均衡总重量、允许每包物品数不同"的解在这里不可接受？

答案：因为调用场景里每个物品（物理专家/专家组）占用的资源量相同（显存、槽数），每个目标（GPU/节点）的容量也相同且必须装满。若某 GPU 分到 5 个专家而另一块只分到 3 个，前者显存直接不够。基数约束是物理约束，不是优化目标的一部分。

**练习 3**：输入 `weight` 形状为 `[4, 10]`、`num_packs=5`，输出形状是什么？`groups_per_pack` 是多少？

答案：两个输出均为 `[4, 10]`；`groups_per_pack = 10 // 5 = 2`。层数 4 只表示同样的装箱独立做 4 次，不改变输出形状。

### 4.2 贪心主循环：降序处理 + "最轻且未满"的包

#### 4.2.1 概念说明

主循环是 LPT 式贪心在基数约束下的变体，规则只有两条：

1. **降序处理**：先按权重从大到小排序，重物品先安置；
2. **最轻未满包**：每个物品放进"还有空位（未满）的包里当前总重量最小的那个"。

两条规则分别攻击失败模式的两个方向：

- **不排序会怎样？** 小物品先到，均匀铺开占坑；大物品最后到时选择余地最小，甚至被迫与另一个大物品同包。用一组具体权重看（降序排列后为 8,7,6,5,4,3,2,1）：
  - 按原顺序 8,3,7,2,5,1,6,4 逐个贪心：最终两包负载 **19 对 17**（比值 1.12）；
  - 按降序处理同样的物品：最终两包负载 **18 对 18**（比值 1.00，且总重 36 的完美对半）。
  
  同一组物品、同一个"选最轻包"规则，仅仅改变处理顺序，均衡度就不同——这就是降序的价值。推导过程见 4.2.4 实践，此处先记住结论。

- **"未满"限定会怎样？** 若只看重量不看空位，物品可能被分进已满的包，基数约束被破坏，每包恰好 n/m 个的硬要求失效。"未满"过滤保证每次写入都落在合法槽位里。

还有一个容易被忽略的确定性细节：`min` 在并列（多个包重量相同）时返回**迭代顺序中最先出现**的那个，也就是编号最小的包。这让整个算法在权重无并列时完全确定、可复现。

#### 4.2.2 核心流程

```
indices = weight 每层按权重降序排序后的物品编号序列
pack_index, rank_in_pack 先全部填 -1（哨兵值）

for 每层 i:
    pack_weights = [0] * m        # 各包当前重量，每层重置
    pack_items   = [0] * m        # 各包当前物品数，每层重置
    for group in indices[i]:      # 按重量从大到小逐个处理
        # 关键一步：在未满的包中选当前最轻者
        pack = min(所有满足 pack_items[p] < groups_per_pack 的 p,
                   按 pack_weights[p] 比较)
        pack_index[i, group]   = pack
        rank_in_pack[i, group] = pack_items[pack]     # 写入前的人数即名次
        pack_weights[pack] += weight[i, group]
        pack_items[pack]   += 1
```

每层复杂度：排序 \( O(n \log n) \)，贪心循环每步扫描 m 个包共 \( O(nm) \)，合计 \( O(n(\log n + m)) \) 每层。对 EPLB 的规模（n 最多几百个专家、m 最多几十个 GPU/节点）这完全不是瓶颈——真正的常数开销来自 Python 循环逐元素访问张量，这一点留到 u3-l3 的性能专题讨论。

#### 4.2.3 源码精读

逐段读 [eplb.py:L27-L41](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L41)。

**第一段：排序与初始化。**

```python
    indices = weight.float().sort(-1, descending=True).indices.cpu()
    pack_index = torch.full_like(weight, fill_value=-1, dtype=torch.int64, device='cpu')
    rank_in_pack = torch.full_like(pack_index, fill_value=-1)
```

引用自 [eplb.py:L27-L29](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27-L29)。四个要点：

- `weight.float()`：排序前统一转 float32。若 weight 是整数张量，某些情况下排序比较语义和后续累加都不如浮点统一、安全（这也是入口函数 [eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 同样做 `weight.float().cpu()` 的原因之一，数值稳定性话题在 u3-l3 展开）。
- `.sort(-1, descending=True).indices`：取降序排序的下标序列——`indices[i]` 就是"第 i 层按权重从大到小排列的物品编号清单"，即处理顺序。
- `.cpu()`：后面要 `for group in indices[i]` 用 Python 循环逐元素取值，索引张量必须在 CPU 上才高效。
- `fill_value=-1` 的**哨兵值**（sentinel）：所有位置最终都会被覆盖（n 个物品恰好填满 n 个槽），初始化为 -1 只是让"如果有 bug 漏放某物品"在输出里一眼可见。注意它与入口函数 `log2phy` 中 -1 的区别——那里 -1 是**真实的 padding 语义**（复制数不足 maxlogcnt 的专家槽位），这里纯粹是防御性初始化。

**第二段：双层循环的骨架。**

```python
    for i in range(num_layers):
        pack_weights = [0] * num_packs
        pack_items = [0] * num_packs
        for group in indices[i]:
```

引用自 [eplb.py:L30-L33](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L33)。外层遍历层，**每层都重新初始化两个 Python 列表**——这保证层与层完全独立（第 3 层的装箱不受第 1 层影响）。内层按降序顺序取出物品编号 `group`。状态只有两个列表：`pack_weights`（各包当前重量）与 `pack_items`（各包当前物品数），一边装一边更新。

**第三段：本函数最精炼的一行——选择哪个包。**

```python
            pack = min((i for i in range(num_packs) if pack_items[i] < groups_per_pack), 
                       key=pack_weights.__getitem__)
            assert pack_items[pack] < groups_per_pack
```

引用自 [eplb.py:L34-L36](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L36)。这行 `min` 值得拆成三层来读：

1. **生成器表达式** `(i for i in range(num_packs) if pack_items[i] < groups_per_pack)`：产出所有"还没装满"的包编号。容量约束就在这个 `if` 里生效——满包根本不进入候选集。
2. **`key=pack_weights.__getitem__`**：以包的当前重量作为比较键。`pack_weights.__getitem__` 等价于 `lambda p: pack_weights[p]`，直接传方法引用少写一个 lambda，是常见的 Python 惯用法。于是 `min` 的语义就是"未满的包里最轻的那个"。
3. **变量遮蔽**：生成器里的 `i` 是生成器表达式**自己的作用域**中的名字，与外层 `for i in range(num_layers)` 的层号 `i` 毫无关系。Python 规定生成器表达式自成作用域，所以这里不会真的干扰外层循环，但两个 `i` 同名确实牺牲了可读性——读的时候请在心里把内层那个改名为 `p`（pack）。

还有一个正确性论证：**为什么生成器永远不会为空、`assert` 永远不会触发？** 放第 k 个物品（k 从 1 数起）之前已放 k-1 个，总容量是 m × groups_per_pack = n，剩余空位 n-(k-1) ≥ 1，所以候选集必然非空，且选中的包必未满。[eplb.py:L36](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L36) 的断言是纯防御性的（对类型检查器和未来维护者声明不变量）。

**第四段：写入与状态更新。**

```python
            pack_index[i, group] = pack
            rank_in_pack[i, group] = pack_items[pack]
            pack_weights[pack] += weight[i, group]
            pack_items[pack] += 1
```

引用自 [eplb.py:L37-L40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L37-L40)。注意 `rank_in_pack[i, group] = pack_items[pack]` 写的是**更新前**的计数——物品放入前包里已有几件，它就排第几（0 起），顺序天然正确。最后两行把重量（注意 `weight[i, group]` 是 0 维张量，`pack_weights` 的元素从 Python int 变成 0 维张量，`min` 对 0 维 CPU 张量比较没有问题）和计数加上。

一个工程细节：`weight` 本身没有被 `.cpu()`，只有排序索引被移到了 CPU。如果 weight 留在 GPU 上，这个循环里每次 `pack_weights[pack] += weight[i, group]` 都会引发一次主机-设备同步，性能急剧下降。当前不出问题是因为入口函数 [eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 已经把 weight 整体搬到 CPU——**`balanced_packing` 隐式依赖调用方的这个约定**。这类设备一致性问题曾真实地咬过这个仓库（commit `d52c72d` 修复了 `pphy2mlog` 的设备缺失），u3-l3 会专门讨论。

#### 4.2.4 代码实践

**实践目标**：用 8 个物品、2 个包的小例子，**手工模拟**算法每一步写出 `pack_index` 和 `rank_in_pack`，再用代码验证手算结果一致；并做一个"降序 vs 原顺序"的对照实验，亲眼看到排序带来的均衡收益。

**操作步骤**：

第 1 步，手算。输入 `weight = [[8, 3, 7, 2, 5, 1, 6, 4]]`（物品编号 0~7），`num_packs = 2`，`groups_per_pack = 4`。先写出降序处理顺序：物品 0(8) → 2(7) → 6(6) → 4(5) → 7(4) → 1(3) → 3(2) → 5(1)。然后填下面这张表（"候选包"列列出所有未满的包，加粗的是被选中的；我们给出前两步作为示范，其余请自己完成）：

| 步 | 物品 | 权重 | 未满的包 | 各包重量（选择前） | 选中包 | rank | 各包重量（选择后） |
|---|---|---|---|---|---|---|---|
| 1 | 0 | 8 | {0, 1} | [0, 0] | **0**（并列取小） | 0 | [8, 0] |
| 2 | 2 | 7 | {0, 1} | [8, 0] | **1** | 0 | [8, 7] |
| 3 | 6 | 6 | {0, 1} | [8, 7] | ？ | ？ | ？ |
| 4 | 4 | 5 | {0, 1} | ？ | ？ | ？ | ？ |
| 5 | 7 | 4 | {0, 1} | ？ | ？ | ？ | ？ |
| 6 | 1 | 3 | {0, 1} | ？ | ？ | ？ | ？ |
| 7 | 3 | 2 | {0, 1} | ？ | ？ | ？ | ？ |
| 8 | 5 | 1 | 只剩未满的包是？ | ？ | ？ | ？ | ？ |

第 2 步，把每步结果整理成按物品编号组织的两张表（与输出张量同构）：`pack_index[0][g]` 和 `rank_in_pack[0][g]`。

第 3 步，运行验证（示例代码）：

```python
# 示例代码：手算结果验证 + 降序/原顺序对照
import torch
from eplb import balanced_packing

w = torch.tensor([[8., 3., 7., 2., 5., 1., 6., 4.]])
pack_index, rank_in_pack = balanced_packing(w, num_packs=2)
print("pack_index   =", pack_index.tolist())    # 与手算对照
print("rank_in_pack =", rank_in_pack.tolist())  # 与手算对照

# 对照实验：同样的物品、同样"选最轻包"规则，但按原顺序处理
def sequential_greedy(weight, num_packs):
    """示例代码：不排序的朴素贪心，行为对照用"""
    n = weight.size(-1)
    gpp = n // num_packs
    pi = torch.full_like(weight, -1, dtype=torch.int64)
    ri = torch.full_like(weight, -1, dtype=torch.int64)
    for layer in range(weight.size(0)):
        pw, pc = [0.] * num_packs, [0] * num_packs
        for g in range(n):                      # 唯一区别：不排序
            p = min((j for j in range(num_packs) if pc[j] < gpp), key=pw.__getitem__)
            pi[layer, g], ri[layer, g] = p, pc[p]
            pw[p] += weight[layer, g].item(); pc[p] += 1
    return pi, ri, pw

pi2, ri2, _ = sequential_greedy(w, 2)
def loads(w, pi):
    return [w[0][pi[0] == j].sum().item() for j in range(pi.max().item() + 1)]
print("降序处理，各包负载:", loads(w, pack_index))
print("原顺序处理，各包负载:", loads(w, pi2))
```

**需要观察的现象**：

- 手算表在第 5 步和第 8 步各有一个值得注意的点：第 5 步出现重量**并列**（13 对 13），观察 `min` 是否选了编号较小的包 0；第 8 步包 1 已经装满 4 个物品，容量约束开始生效，候选集只剩包 0。
- 代码输出与你的手算逐位一致。
- 对照实验中两种顺序给出的每包物品**集合**不同、负载也不同。

**预期结果**（笔算推导，请以本地运行为准，待本地验证）：

```
pack_index   = [[0, 1, 1, 1, 0, 0, 1, 0]]
rank_in_pack = [[0, 2, 0, 3, 1, 3, 1, 2]]
降序处理，各包负载: [18.0, 18.0]
原顺序处理，各包负载: [19.0, 17.0]
```

即：降序贪心把总重 36 完美对半（包 0 装物品 {0,4,7,5}，包 1 装物品 {2,6,1,3}），而不排序得到 19/17。若你的手算与代码不一致，优先检查并列步骤是否记成了"选编号较小的包"，以及第 8 步是否忽略了"包已满"的过滤。

一个与复现性有关的提醒：`torch.sort` 在未指定 `stable=True` 时**不保证相等权重元素间的相对顺序**。本例权重互不相同所以完全确定；若你的输入存在权重并列，不同版本/后端可能给出不同的 `pack_index`（每包物品集合可能与标答互换），这是正常现象，不是你算错。

#### 4.2.5 小练习与答案

**练习 1**：输入 `weight = [[5, 5, 5, 5, 5, 5]]`、`num_packs = 3`，写出输出。

答案：`groups_per_pack = 2`，且所有权重并列。降序排序后处理顺序是 6 个物品的某个排列（并列时顺序取决于 `torch.sort` 实现）；以 0,1,2,3,4,5 的顺序模拟：物品 0→包 0（并列取小），物品 1→包 1，物品 2→包 2，物品 3→包 0（三者并列 5 取最先），物品 4→包 1，物品 5→包 2。得 `pack_index = [[0,1,2,0,1,2]]`，`rank_in_pack = [[0,0,0,1,1,1]]`，三包负载均为 10——绝对均衡。若你运行时 sort 的并列顺序不同，`pack_index` 可能是别的排列，但"每包 2 个、负载 10/10/10"不变。

**练习 2**：把 `key=pack_weights.__getitem__` 改成"选最重的未满包"（例如 `key=lambda p: -pack_weights[p]`），预期均衡度如何变化？为什么？

答案：急剧恶化。该规则会让重物不断叠加到同一个包直到装满（越重越被选），形成"先填满一个再填下一个"的效果，等于把最大的几个物品堆在一起。对比之下，"选最轻包"每一步都在拉平各包差距，这正是贪心目标（最小化最重包）的局部最优动作。

**练习 3**：证明 `assert pack_items[pack] < groups_per_pack`（[eplb.py:L36](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L36)）永远不会触发。

答案：设放入第 k 个物品（k = 1..n）。此前共放入 k-1 个物品，所有包总容量为 m × groups_per_pack = n，剩余空位 n - (k - 1) ≥ 1，故至少存在一个满足 `pack_items[p] < groups_per_pack` 的包，生成器非空；`min` 的候选本身就带这个过滤条件，选出的 `pack` 必然满足断言。该断言是防御式编程，用于向维护者声明这一不变量。

### 4.3 平凡分支与在 EPLB 中的调用场景

#### 4.3.1 概念说明

当 `groups_per_pack == 1`（每包只装 1 个物品）时，装箱问题**退化到没有选择余地**：物品 g 只能进包 g，名次只能是 0。此时再走通用贪心循环纯属浪费，函数直接用两个向量化操作构造答案、提前返回：

```python
    if groups_per_pack == 1:
        pack_index = torch.arange(weight.size(-1), dtype=torch.int64, device=weight.device).expand(weight.shape)
        rank_in_pack = torch.zeros_like(weight, dtype=torch.int64)
        return pack_index, rank_in_pack
```

引用自 [eplb.py:L22-L25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)。

- `torch.arange(n).expand([X, n])`：把 `[0, 1, ..., n-1]` 广播到每一层——"物品 g 进包 g"。`expand` 共享底层内存、不复制数据，适合这种只读广播（对比 `repeat` 会真实复制）。
- `torch.zeros_like(weight, dtype=torch.int64)`：每个包只有 1 个物品，它必然是第 0 个，名次全 0。

这个分支不只是"锦上添花的快路径"，它在 EPLB 中有**必然被触发的真实场景**（见 4.3.3）。理解它还有一层架构意义：**全局策略没有独立实现**。回忆 u1-l4 的结论——当 `num_groups % num_nodes != 0` 时，入口函数以退化参数 `(num_groups=1, num_nodes=1, num_gpus=P)` 复用层级实现；此时"组打包到节点"这步自然退化为这里的平凡分支。一个 4 行的分支，让整个全局策略可以搭层级策略的便车，这是"用退化参数复用实现"设计模式的一个具体落点（u3-l5 会正面讨论这个模式）。

另一个值得注意的细节：平凡分支的张量建在 `weight.device` 上，而主循环路径的输出被显式建在 CPU（[eplb.py:L28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28) 的 `device='cpu'`）。两条路径的输出设备**并不自动一致**——当前不出问题是因为入口函数 [eplb.py:L149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 保证了 weight 一定在 CPU。读代码时能注意到这种"隐式约定"，是培养源码敏感度的好练习。

#### 4.3.2 核心流程

```
groups_per_pack = n // m
若 groups_per_pack == 1：
    pack_index  = [[0, 1, ..., n-1] 逐层广播]   # 物品 g 进包 g
    rank_in_pack = 全 0                          # 每包唯一物品必为第 0 个
    提前返回
否则：
    走 4.2 的降序贪心主循环
```

#### 4.3.3 源码精读

梳理平凡分支在两条策略路径下的触发链条。

**场景 A：全局策略必然触发。** 看入口函数的策略分派：

```python
    if num_groups % num_nodes == 0:
        # use hierarchical load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 
                                                                  num_groups, num_nodes, num_gpus)
    else:
        # use global load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

引用自 [eplb.py:L150-L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)。全局策略传入 `num_groups=1, num_nodes=1`，于是层级策略 Step 1 中：

```python
    tokens_per_group = weight.unflatten(-1, (num_groups, group_size)).sum(-1)
    group_pack_index, group_rank_in_pack = balanced_packing(tokens_per_group, num_nodes)
```

引用自 [eplb.py:L104-L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L105)。此时 `tokens_per_group` 形状为 `[L, 1]`（1 个组），`num_packs = 1`，`groups_per_pack = 1 // 1 = 1`——**命中平凡分支**，返回恒等放置（唯一 的组进唯一的节点），Step 1 整体变成无操作。这正是全局策略"无视分组"的实现方式：不是绕过分组逻辑，而是让分组数坍缩为 1。

**场景 B：层级策略在特定参数下触发 Step 3 的平凡分支。** Step 3 打包节点内物理专家到 GPU：

```python
    pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
```

引用自 [eplb.py:L118](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L118)。此处物品数 = `num_physical_experts // num_nodes`，包数 = `num_gpus // num_nodes`，所以 `groups_per_pack = num_physical_experts // num_gpus`（即每 GPU 物理专家数）。当 `num_replicas == num_gpus`（每个 GPU 恰好放 1 个物理专家，即"最少冗余"配置）时该值为 1，命中平凡分支。

顺带把输出在下游的用法点一下（细节留给 u2-l4/u2-l5）：两个调用点拿到 `pack_index`、`rank_in_pack` 后，都用同一个复合编码把二维坐标压成一维槽位号：

```python
    log2mlog = (((group_pack_index * groups_per_node + group_rank_in_pack) * group_size).unsqueeze(-1) + 
                torch.arange(group_size, dtype=torch.int64, device=group_pack_index.device)).flatten(-2)
```

引用自 [eplb.py:L106-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107)（Step 1 侧，`包号 × 每包组数 + 名次` 得到组的新编号，再展开到组内每个专家）；Step 3 侧同理，见 [eplb.py:L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 的 `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`。因为 (包号, 名次) 是不重不漏的二维坐标，这个一维槽位号恰好构成 0..n-1 的**置换**——这就是 4.1.4 实践里那条断言验证的性质，也是上层能用它做 gather 重排的前提。

#### 4.3.4 代码实践

**实践目标**：直接观察平凡分支的输入输出，并分别在全局策略与"最少冗余"层级策略两个真实场景中确认它被走到。

**操作步骤**（示例代码）：

```python
# 示例代码：平凡分支的观察
import torch
from eplb import balanced_packing, rebalance_experts

# 1) 直接调用平凡分支：1 个物品、1 个包
pi, ri = balanced_packing(torch.tensor([[100.]]), num_packs=1)
print(pi, ri)   # 预期 [[0]] [[0]]

# 2) 再来一个：4 个物品、4 个包（每包 1 个）
pi, ri = balanced_packing(torch.tensor([[9., 1., 5., 5.]]), num_packs=4)
print(pi.tolist(), ri.tolist())   # 预期 [[0, 1, 2, 3]] [[0, 0, 0, 0]]

# 3) 场景 A：不整除触发全局策略（4 组 3 节点），内部 Step 1 走平凡分支
weight = torch.rand(2, 12) * 100
phy2log, log2phy, logcnt = rebalance_experts(weight, 16, num_groups=4,
                                             num_nodes=3, num_gpus=6)
print(phy2log.shape, logcnt.sum(-1))   # [2,16]，每层 logcnt 之和 = 16

# 4) 场景 B：num_replicas == num_gpus，层级策略 Step 3 走平凡分支
phy2log, log2phy, logcnt = rebalance_experts(weight, 8, num_groups=4,
                                             num_nodes=2, num_gpus=8)
print(phy2log.shape, logcnt.sum(-1))   # [2,8]，每层 logcnt 之和 = 8
```

**需要观察的现象**：

- 第 1、2 步：无论权重多么悬殊（9 对 1），每包 1 个物品时输出永远是恒等放置、名次全 0——没有选择余地，权重根本不参与决策。
- 第 3、4 步：两条策略路径都正常返回。可以（建议）在本地复制 `eplb.py` 为自己的实验副本，在平凡分支里加一行 `print("trivial branch hit")`，再跑第 3、4 步，确认它确实被走到（注意：是修改你自己的副本，不要动仓库源码）。
- 第 4 步中每个 GPU 恰好 1 个物理专家，此时 Step 3 的"GPU 级均衡"实际上已经没有自由度——负载差异完全由 Step 2 复制谁、复制多少决定。

**预期结果**：第 1、2 步输出如注释所写（确定性结果）；第 3、4 步形状与计数如注释（`logcnt` 每行求和恒等于物理专家总数，这是 u1-l3 讲过的不变量）。随机权重部分的具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：全局策略（`num_groups % num_nodes != 0`）的调用链中，具体是哪一次 `balanced_packing` 调用命中平凡分支？参数是什么？

答案：入口 [eplb.py:L156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L156) 以 `(1, 1, num_gpus)` 调用层级实现，Step 1（[eplb.py:L105](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L105)）执行 `balanced_packing(tokens_per_group, num_nodes=1)`，`tokens_per_group` 形状 `[L, 1]`，`groups_per_pack = 1 // 1 = 1`，命中平凡分支，Step 1 退化为恒等映射。

**练习 2**：层级策略下，若 `num_replicas == num_gpus`（且整除约束都满足），Step 3 的 `balanced_packing` 是否走平凡分支？此时 GPU 间负载还能被 Step 3 改善吗？

答案：走平凡分支。Step 3 的 `groups_per_pack = (num_replicas/num_nodes) / (num_gpus/num_nodes) = num_replicas / num_gpus = 1`，每 GPU 恰好 1 个物理专家，放置没有自由度，输出恒等。此时 GPU 间负载完全由前两步（组到节点的划分、节点内复制）决定，Step 3 无从改善——这也是为什么 `num_replicas` 显著大于 `num_gpus`（引入冗余）时均衡效果更好，u3-l2 的评估实验会量化这一点。

**练习 3**：`expand` 和 `repeat` 都能把 `[0..n-1]` 变成 `[X, n]`，这里为什么选 `expand`？

答案：`expand` 返回共享内存的广播视图，不复制数据，对于之后只读不写的 `pack_index` 是零拷贝的正确选择；`repeat` 会真实复制 X 份数据，浪费内存且无必要。配套的取舍是：向 expanded 视图原位写入是受限的，而下游对它的用法（参与算术运算、作为 gather 索引）都是只读，因此安全。

## 5. 综合实践

**任务：给 balanced_packing 做一次"与最优解的差距"基准评测。**

本讲三个知识块——问题定义与不变量、贪心行为、边界分支——在这个任务里全部用上。思路：物品数小的时候可以**暴力枚举**所有满足基数约束的划分，求出真正的最优 makespan，再统计 `balanced_packing` 相对最优的差距。

**操作步骤**（示例代码，n=8、m=2 时划分只有 C(8,4)=70 种，暴力完全可行）：

```python
# 示例代码：balanced_packing vs 暴力最优
import itertools, torch
from eplb import balanced_packing

def brute_force_opt(weight_row, num_packs):
    """枚举所有每包 n/m 个物品的划分，返回最优 makespan"""
    n = weight_row.numel()
    gpp = n // num_packs
    best = float("inf")
    items = set(range(n))
    for first in itertools.combinations(range(n), gpp):  # 包 0 的物品集合
        rest = tuple(items.difference(first))
        # 固定包 0 集合后，其余包再递归划分；m=2 时直接算：
        if num_packs == 2:
            w0 = sum(weight_row[list(first)].tolist())
            w1 = weight_row.sum().item() - w0
            best = min(best, max(w0, w1))
    return best

ratios = []
torch.manual_seed(0)
for trial in range(100):
    w = torch.rand(1, 8) * 100            # 随机权重（浮点，几乎不会并列）
    pi, ri = balanced_packing(w, 2)
    loads = [w[0][pi[0] == j].sum().item() for j in range(2)]
    greedy = max(loads)
    optimal = brute_force_opt(w[0], 2)
    ratios.append(greedy / optimal)

print(f"平均比值 {sum(ratios)/len(ratios):.4f}，最大比值 {max(ratios):.4f}，"
      f"达到最优的比例 {sum(r == 1 for r in ratios)}/{len(ratios)}")
# 顺带复查不变量：每包恰好 4 个物品
assert all((pi[0] == j).sum().item() == 4 for j in range(2))
```

**需要观察与思考的现象**：

1. 贪心解与最优解的比值分布：多少比例的实例上贪心恰好达到最优？最差到多少？（n=8、m=2 的世界里贪心非常接近最优；这是小规模的经验观察，不代表大规模理论保证。）
2. 把 `m` 改成 4（n=8，每包 2 个）重跑——注意 `brute_force_opt` 需要相应扩展成对剩余物品递归划分，C(8,2)×C(6,2)×C(4,2)/4! = 35 种本质不同的划分，仍可暴力。贪心比值是否变差？
3. 构造一个长尾权重（如 `[97, 90, 1, 1, 1, 1, 1, 1]` 量级），观察贪心的表现——两个大物品被分进不同包了吗？这对应 EPLB 里"两个重载专家必须落在不同 GPU"的真实诉求。

**预期结果**：随机均匀权重下，绝大多数实例的比值应为 1.0 或非常接近 1（待本地验证，具体数字请以实测为准）。这个小型基准评测的框架，将在 u3-l2（负载均衡质量评估）被推广到"策略级"的对比。

## 6. 本讲小结

- `balanced_packing` 解决的是**带基数约束的均衡装箱**：n 个物品装进 m 个包，每包恰好 n/m 个（EPLB 的显存/槽位硬约束），最小化最重包负载；问题 NP-难，所以用贪心。
- 算法是 **LPT 式贪心变体**：物品按权重**降序**处理（大件先放、小件填缝），每个物品放入"**未满的包中最轻**"的一个；并列时 `min` 取编号最小的包，权重无并列时结果完全确定。
- 核心一行是 [eplb.py:L34-L35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L35) 的 `min((i for i in ... if 未满), key=pack_weights.__getitem__)`：生成器负责容量过滤，`key` 负责选最轻；生成器里的 `i` 遮蔽外层层号 `i` 但互不影响。
- 输出 `pack_index`/`rank_in_pack` 是**按物品组织的去向表**，(包号, 名次) 复合编码 `pack_index * groups_per_pack + rank_in_pack` 恰构成 0..n-1 的置换，这是上层用 gather 做重排的基础。
- `groups_per_pack == 1` 的**平凡分支**用 `arange().expand()` + `zeros_like()` 向量化提前返回；全局策略（退化参数 1,1,P）的 Step 1 和"每 GPU 一个物理专家"（`num_replicas == num_gpus`）的 Step 3 都会真实命中它。
- 两个工程细节埋了伏笔：输出的 -1 是哨兵而非 padding 语义（对照 `log2phy`）；函数隐式依赖入口把 weight 放在 CPU（`.cpu()` 只给了排序索引），设备一致性问题见 u3-l3。

## 7. 下一步学习建议

- **下一讲 u2-l2（replicate_experts）**：学习另一个基础积木——每轮复制 `weight / logcnt` 最大的逻辑专家，用冗余副本摊薄重载。它与 `balanced_packing` 分别回答"复制谁"和"放在哪"，组合起来才是完整的放置算法。
- **u2-l3（张量技巧）**：如果对 `sort().indices`、`gather`/`scatter_`、`expand`、复合索引编码还想加深，可以先补这一讲，为精读主流程做准备。
- **带着问题重读本讲的两个调用点**：[eplb.py:L104-L107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L104-L107) 与 [eplb.py:L117-L119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117-L119)。思考：为什么 Step 1 用"组负载"装箱而 Step 3 用"每个副本的平均负载"装箱？这个差异会在 u2-l4、u2-l5 的主流程精读中得到解答。
- **延伸阅读（可选）**：经典调度理论中 Graham 的 LPT 分析（近似比 \( \frac{4}{3} - \frac{1}{3m} \)）与 number partitioning 的 Karmarkar–Karp 差分法，可以帮你把"降序贪心"放进更大的算法版图里理解。
