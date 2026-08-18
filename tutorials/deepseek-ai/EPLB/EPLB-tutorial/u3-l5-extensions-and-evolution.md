# 二次开发实战：策略变体与"退化参数复用"设计模式

## 1. 本讲目标

这是本手册的收官之讲。前面你已经读懂了 EPLB 的全部算法（u2）、给它写过正确性测试（u3-l1）、做过均衡度评测（u3-l2）、分析过性能与设备问题（u3-l3）、也理解了它在工程闭环中的位置（u3-l4）。本讲把这些能力合起来，完成一次真正的**二次开发**。

学完后你应该能够：

1. 在**不破坏原有接口**的前提下，实现并接入一个变体复制策略（最大副本数受限的 `replicate_experts`）。
2. 用 u3-l2 的均衡度指标（IB = max/mean）量化变体相对原版的**收益与代价**，而不是凭感觉说"更好"。
3. 讲清楚"用退化参数复用 `rebalance_experts_hierarchical` 实现全局策略"这一设计模式的**优缺点**——它在一个真实 commit（e1100fe）中刚刚被上演过，且紧随其后的一次 bug 修复（d52c72d）恰好暴露了它的风险面。

## 2. 前置知识

本讲默认你已完成 u2 与 u3 前四讲。快速唤醒关键记忆：

- **三个内部函数一个入口**：`balanced_packing`（带容量约束的降序贪心装箱，回答"放在哪"）、`replicate_experts`（每轮复制 `weight/logcnt` 最大的专家，回答"复制谁"）、`rebalance_experts_hierarchical`（把前两者串成三步主流程）、`rebalance_experts`（唯一公开入口，见 `__all__`）。
- **三步主流程**：组打包到节点 → 节点内复制 → 物理专家打包到 GPU。每步产出一张 A2B 映射表，最后逐段复合。
- **INV 不变量清单**（u3-l1）：形状、值域、守恒（每层 `logcnt` 之和恒等于物理专家数 M）、覆盖计数、互逆一致、布局契约、组-节点对齐。任何变体过了这七条才算"结构正确"。
- **IB 指标**（u3-l2）：单副本负载 = `weight/logcnt`，每 GPU 负载按槽位连续切块求和，IB = max/mean ≥ 1，越接近 1 越均衡。长尾分布下冗余专家收益最大且边际递减——这条结论正是本讲变体的动机。
- **设备一致性**（u3-l3）：库内约定伴生张量从上游继承设备；d52c72d 修复过 Step 3 节点基地址 `arange` 缺 `device` 的问题。
- **本讲新术语**：
  - **变体（variant）**：保持接口契约不变、只改变内部决策策略的实现。
  - **退化参数复用（reduce to degenerate case）**：不写独立的特例实现，而是给通用实现传入退化参数，让它自动退化为特例。
  - **掩码（mask）**：用 `masked_fill` 把张量中某些位置改成极小值（`-inf`），使其在 `argmax` 中永远不会被选中。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | 全部核心：三个内部函数的"扩展缝"、入口分派与 `log2phy` 组装 |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | 全局策略的文档承诺（L29-31），用于对照 e1100fe 前后的实现差距 |
| `EPLB-tutorial/` 中你自己的产物 | u3-l1 的 `test_eplb.py`（INV 回归）、u3-l2 的评测脚本（IB 计算），本讲直接复用 |

注意：EPLB 仓库没有 tests 目录、没有打包文件，`eplb.py` 以纯 Python 模块直接 import。这对二次开发反而是好事——你可以在同目录放一个自己的实验模块，`import eplb` 复用其中的函数，一行都不改原文件。

## 4. 核心概念与源码讲解

### 4.1 二次开发的地图：eplb.py 的三条扩展缝

#### 4.1.1 概念说明

"不破坏原有接口"在 EPLB 里有明确的判据。公开契约只有一条：

[eplb.py:164](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L164)

```python
__all__ = ['rebalance_experts']
```

也就是说，外部世界只认识 `rebalance_experts(weight, num_replicas, num_groups, num_nodes, num_gpus)` 及其返回的三元组 `phy2log / log2phy / logcnt`。只要这五个参数和三个返回值的语义不变（u3-l1 的七条 INV 继续成立），内部怎么换实现都算"不破坏接口"。

在这个前提下，`eplb.py` 内部有三条天然的"扩展缝"，分别对应三个决策维度：

| 缝 | 落点 | 决定的决策 | 变体方向举例 |
| --- | --- | --- | --- |
| **缝 A** | `replicate_experts` | **复制谁**（副本分配） | 最大副本数受限、按显存加权 |
| **缝 B** | `balanced_packing` | **放在哪**（装箱判据） | 带宽加权装箱、异构容量 |
| **缝 C** | `rebalance_experts_hierarchical` | **整体流程**（三步如何串联） | 层间平滑、增删步骤 |

三条缝的共同结构：函数都是"纯函数"（输入张量进、输出映射表出，无内部状态），所以替换它们不需要 mock、不需要继承，复制一份改掉即可。这是小仓库的天然优势。

#### 4.1.2 核心流程

变体开发五步法（本讲和 4.3、4.4 节都会沿着它走）：

```text
1. 定缝   —— 我的变体改变的是"复制谁 / 放在哪 / 整体流程"中的哪一个？
2. 复制   —— 把目标函数复制到实验模块（eplb_variant.py），原文件一行不动
3. 保契约 —— 保持返回三元组的形状与语义（必要时整链复刻到入口）
4. 回归   —— 跑 u3-l1 的 INV 清单，结构正确性过关
5. 评测   —— 用 u3-l2 的 IB 指标 + 副本分布 + 耗时，量化收益与代价
```

其中第 3 步最容易被轻视：`replicate_experts` 的返回三元组 `phy2log / rank / logcnt` 不是随便定的——`rank`（副本序号）使 `(逻辑编号, rank)` 编码成唯一地址，入口正是靠它构造逆映射 `log2phy`（u2-l2、u2-l6）。变体如果忘了维护 `rank` 与 `logcnt` 的一致性，下游组装会立刻崩掉，而且往往崩在离出错很远的地方。

#### 4.1.3 源码精读

三条缝在源码中的精确位置。先看缝 A 被上层调用的地方——层级函数的 Step 2：

[eplb.py:110-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L110-L113)

```python
    # Step 2: construct redundant experts within nodes
    # [num_layers * num_nodes, num_logical_experts // num_nodes]
    tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
    phy2mlog, phyrank, mlogcnt = replicate_experts(tokens_per_mlog, num_physical_experts // num_nodes)
```

这行调用就是缝 A 的接入点：换成你自己的变体函数（签名兼容即可），Step 1 和 Step 3 完全无感。注意输入被 `view` 成 `[num_layers * num_nodes, E/N]`——所有层 × 节点在一次调用里按行独立并行（u2-l4 讲过的"行维批量化"）。

缝 B 的接入点在 Step 3：

[eplb.py:117-119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117-L119)

```python
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
    pack_index, rank_in_pack = balanced_packing(tokens_per_phy, num_gpus // num_nodes)
    phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack
```

装箱输入是"单副本负载" `tokens_per_phy`（复制后流量由副本均分，u2-l5），输出经 L119 的槽位编码变成置换。改装箱判据不影响这行编码——但改"容量"会影响，4.5 节细说。

缝 C 的调用点在入口的分派段：

[eplb.py:148-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L148-L156)

```python
    num_layers, num_logical_experts = weight.shape
    weight = weight.float().cpu()
    if num_groups % num_nodes == 0:
        # use hierarchical load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas,
                                                                  num_groups, num_nodes, num_gpus)
    else:
        # use global load-balance policy
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

这一段是整个仓库最近一次"官方二次开发"动刀的地方——else 分支的那行调用在 e1100fe 之前是另一个样子。这正是下一节的案例。

#### 4.1.4 代码实践

**实践目标**：在动手写代码之前，先画出你自己的扩展缝地图，确认变体插在哪条缝、会影响哪些下游。

**操作步骤**（阅读型实践）：

1. 打开 `eplb.py`，从 L164 的 `__all__` 出发，标出唯一公开入口。
2. 沿调用链向下：入口 → 层级函数 → `balanced_packing` / `replicate_experts`，在纸上画出三条缝的位置。
3. 对每条缝回答两个问题：① 换掉它，输入输出契约是什么？② 它的输出被下游哪几行消费？（提示：缝 A 的输出被 L117 和 L122-127 消费；缝 B 的输出只被 L119 消费。）
4. 特意检查一件事：层级函数内嵌套定义的 `inverse` 函数（L98-101）**不是模块级函数**——你复制层级函数做实验版时，必须把它一起复制，不能 `from eplb import inverse`。

**需要观察的现象**：你会发现自己画的地图上，缝 B（装箱）的影响半径比缝 A（复制）小得多——`balanced_packing` 的输出只在紧邻的一行被消费，而 `replicate_experts` 的三个输出贯穿 Step 3 全部六行。影响半径越小，变体越安全。

**预期结果**：一张三行两列的表（缝 × {契约, 下游消费点}），以及"缝 B 改判据最安全、缝 C 改流程风险最大"的结论。待本地验证（手工绘图，无运行结果）。

#### 4.1.5 小练习与答案

**练习 1**：我想让"每层复制的总预算"随负载动态变化（重载层多复制、轻载层少复制），应该动哪条缝？

> **参考答案**：缝 C。当前 `num_physical_experts` 对所有层是同一个常数（每层守恒 `Σlogcnt = M`），且层级函数 Step 2 把所有层 `view` 进一次调用。动态预算意味着每行 `num_phy` 不同，需要拆开调用或做掩码对齐，还要同步修改 L95 的整除断言与 L96 的 `phy_experts_per_gpu`（它假设全局统一槽位数）。这是三件套耦合最深的改动，也正是它属于缝 C 的原因。

**练习 2**：为什么 `__all__` 只导出一个函数，却又把三个内部函数放在模块顶层、不加下划线前缀？

> **参考答案**：`__all__` 只约束 `from eplb import *` 的行为，声明"对外承诺的稳定接口只有一个"；不加前缀则允许显式 `import eplb` 后调用内部函数——方便测试（u3-l1 直接测过内部函数）和本讲这类二次开发复用（实验模块可以 `import eplb` 复用 `balanced_packing`）。这是 Python 里常见的"窄承诺、宽可用"惯例。

**练习 3**：判断题：给 `replicate_experts` 增加一个**必选**位置参数 `max_replicas`，算不算破坏接口？

> **参考答案**：对公开契约（`__all__`）不破坏——入口签名没变；但对"可替换性"破坏了——Step 2 的调用点必须同步改。由于我们的方案是复制整个层级函数到实验模块，这并无不可；但如果想以最小 diff 融入上游源码，应改成带默认值的关键字参数 `max_replicas: int | None = None`（默认 `None` 表示不限制、行为与原版逐位一致）。"变体 = 默认参数下与原版等价"是很有价值的工程性质，练习 3 of 4.3 会再用到它。

### 4.2 案例研究：e1100fe 与"退化参数复用"设计模式

#### 4.2.1 概念说明

**退化参数复用**：不写独立的特例实现，而是给通用实现传入退化参数，让它自动退化为特例。在 EPLB 里，这个模式的长相是：

```python
# 全局策略 = 层级策略在 (num_groups=1, num_nodes=1) 下的退化形态
rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

看 e1100fe 的真实 diff（2025-03-21，commit 标题 "add gpu-level load balance for global policy"，close #14）：

```diff
     else:
         # use global load-balance policy
-        phy2log, phyrank, logcnt = replicate_experts(weight, num_replicas)
+        phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

旧实现为什么是错的？回看 `replicate_experts` 的初始化（[eplb.py:62](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62)）：`phy2log` 的前 `num_log` 列是恒等排列 `0..E-1`，冗余副本全部**追加在尾部**。而物理槽位到 GPU 的划分是连续切块——GPU g 占槽位 `[g·M/P, (g+1)·M/P)`。于是：

- 尾部 GPU 分到一堆"重载专家的副本"（每份是 `weight/cnt`，仍然不小）；
- 头部 GPU 分到的可能是一堆轻载专家；
- **没有任何装箱步骤来平衡**。GPU 间负载差异可以非常大，这正是 issue #14 报告的问题。

有趣的是时间线：README 自 2025-02-26 初始提交起就写着全局策略要 "pack the replicated experts to individual GPUs"（[README.md:29-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L29-L31)）——**文档先承诺，实现迟到了近一个月**。e1100fe 不是"文档追赶代码"，而是"实现兑现文档"。

#### 4.2.2 核心流程

新实现为什么对？把退化参数 `(num_groups=1, num_nodes=1, num_gpus=P)` 逐条代入层级函数，手工推导三步的退化行为：

| 层级函数步骤 | 正常参数下 | 退化参数 (1, 1, P) 下 |
| --- | --- | --- |
 | 前置断言（L90-95） | 四条整除检查 | `E % 1 == 0`、`1 % 1 == 0`、`P % 1 == 0`、`M % P == 0` 全部自动满足 |
| Step 1 组→节点 | 组装箱到节点 | `tokens_per_group` 形状 `[L, 1]`（整层总负载）；`balanced_packing` 命中 `groups_per_pack == 1` 平凡分支，`pack_index = 0`；`log2mlog` 退化为**恒等置换** |
| Step 2 节点内复制 | 每节点独立复制 | 恒等置换下 `tokens_per_mlog` 就是原始 `weight`；`num_nodes=1` → **全局复制** |
| Step 3 物理专家→GPU | 节点内 GPU 间装箱 | `num_gpus // num_nodes = P` → **全部 P 张 GPU 间装箱**；节点基地址 `arange(0, E, E) = [0]`，偏移为零 |

其中 Step 1 的退化依赖一个我们早在 u2-l1 就讲过的"看似无用的分支"——`balanced_packing` 的 `groups_per_pack == 1` 提前返回：

[eplb.py:22-25](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L22-L25)

```python
    if groups_per_pack == 1:
        pack_index = torch.arange(weight.size(-1), dtype=torch.int64, device=weight.device).expand(weight.shape)
        rank_in_pack = torch.zeros_like(weight, dtype=torch.int64)
        return pack_index, rank_in_pack
```

如果没有这个分支，`[L, 1]` 的输入也能被下面的双层循环正确处理（每层 1 个物品放进唯一未满的包），只是徒增 Python 循环开销。所以全局策略对它的依赖是**性能依赖而非正确性依赖**——但请注意，退化复用正是靠这些"边界上的小分支"拼出来的，删掉任何一个都可能让退化路径悄悄变慢或变形。这就是维护退化复用时需要的"隐式契约意识"。

**这个模式的优缺点**（本讲第三个学习目标，务必自己能推导一遍）：

优点：

1. **单一事实源**：算法只维护一份。层级函数的任何修复和优化自动惠及两条策略分支。
2. **行为可静态推导**：如上表，退化行为不靠运行、靠代入参数就能从代码推出来——特例的正确性不需要单独论证，它"继承"通用实现的正确性。
3. **测试面收缩**：u3-l1 的一份 INV 清单 + 参数矩阵（含退化组合 `(1, 1, P)`）就覆盖了两种策略。

缺点：

1. **缺陷同样被复用**：复用传播特性，也传播缺陷。d52c72d（2025-03-24，e1100fe 三天后）修复的正是层级函数 Step 3 的设备 bug——
   [eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)
   ```python
       pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) +
                    torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                                 device=group_pack_index.device).view(1, -1, 1)).flatten(-2)
   ```
   这个 bug 在 e1100fe 之前只影响层级策略；复用之后，全局策略同样暴露在这条路径的风险面下。**重构之后必须回归 INV 测试**——u3-l1 的资产在这里兑现价值。
2. **可读性成本**：`hierarchical(weight, M, 1, 1, P)` 不如一个叫 `rebalance_experts_global` 的函数直白。源码用注释（L155 `# use global load-balance policy`）补偿，但没有类型系统强制这个注释与实现同步。
3. **隐式契约绑定**：将来任何人给层级函数加新断言或新步骤，全局策略的行为都可能悄悄变化（例如在 Step 1 引入节点级新约束，会破坏"恒等置换"这个退化前提）。退化参数 `(1, 1, P)` 必须永远满足层级函数的全部前置假设。

#### 4.2.3 源码精读

入口分派与退化调用（已在 4.1.3 引用，[eplb.py:150-156](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L150-L156)）是模式的"使用端"。再看"定义端"——层级函数签名与前置断言，体会退化参数如何被兼容：

[eplb.py:74-75](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L74-L75) 与 [eplb.py:89-96](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L89-L96)

```python
def rebalance_experts_hierarchical(weight: torch.Tensor, num_physical_experts: int,
                      num_groups: int, num_nodes: int, num_gpus: int):
```

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

四条断言在 `(1, 1, P)` 下全部退化为平凡真（`E % 1`、`1 % 1`、`P % 1`），唯一保持实质约束的是 `M % P == 0`——这恰好是全局策略本来就需要的约束（物理专家均分到 GPU）。断言族与退化参数的这种"自动兼容"不是巧合，而是层级抽象本来就以"组数、节点数是自由参数"为前提设计。

最后，分派之后的组装段对两条分支完全无差别：

[eplb.py:157-162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L162)

```python
    maxlogcnt = logcnt.max().item()
    log2phy: torch.Tensor = torch.full((num_layers, num_logical_experts, maxlogcnt),
                                       -1, dtype=torch.int64, device=logcnt.device)
    log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank,
            torch.arange(num_replicas, dtype=torch.int64, device=log2phy.device).expand(num_layers, -1))
    return phy2log, log2phy, logcnt
```

无论哪个分支，只要返回的三元组满足"phy2log 为每层 M 个槽位的放置表、phyrank 为副本序号、logcnt 为副本计数"这一契约，`log2phy` 的组装就是同一段代码。**契约一致，组装无感**——这正是退化复用能只改一行的深层原因。

#### 4.2.4 代码实践

**实践目标**：用运行结果验证"全局策略 = 层级函数的退化形态"，把 4.2.2 的推导表变成眼见为实。

**操作步骤**：

1. 构造一组负载（可直接用 README 示例的 `weight`，`[2, 12]`）。
2. 写一个小脚本（示例代码）：

```python
import torch
import eplb

weight = torch.tensor([[ 90, 132,  40,  61, 104, 165,  39,   4,  73,  56, 183,  86],
                       [ 20, 107, 104,  64,  19, 197, 187, 157, 172,  86,  16,  27]])
M, P = 16, 8

# 路径 1：走入口的全局分支（num_groups=3 不能被 num_nodes=2 整除）
p2l_a, l2p_a, cnt_a = eplb.rebalance_experts(weight, M, num_groups=3, num_nodes=2, num_gpus=P)

# 路径 2：直接以退化参数调用层级函数，再手工组装（组装段复刻 eplb.py L157-161）
p2l_b, rank_b, cnt_b = eplb.rebalance_experts_hierarchical(weight.float(), M, 1, 1, P)
maxcnt = cnt_b.max().item()
l2p_b = torch.full((2, 12, maxcnt), -1, dtype=torch.int64)
l2p_b.view(2, -1).scatter_(-1, p2l_b * maxcnt + rank_b,
                           torch.arange(M).expand(2, -1))

print(torch.equal(p2l_a, p2l_b), torch.equal(l2p_a, l2p_b), torch.equal(cnt_a, cnt_b))
```

3. 再补一个对比：把 `p2l_b` 每行按 `view(2, P, M // P)` 切块求和，复算每 GPU 负载（单副本负载 = `weight/cnt` 按 `p2l` 对齐后 gather），观察 8 张 GPU 的负载是否明显比"简单按 `replicate_experts` 原始输出切块"更均衡。

**需要观察的现象**：三个 `torch.equal` 应全部为 `True`（逐位一致）；每 GPU 负载的 max/min 比值应明显小于按原始 `replicate_experts` 输出直接切块的比值。

**预期结果**：逐位一致成立——因为两条路径执行的实为同一行代码（L156），只是在入口内部多走了一次 `weight.float().cpu()`。若不一致，先检查你是否漏了 `.float()`（入口 L149 的规范化会改变数值路径）。GPU 负载对比的具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：不运行代码，推导 `rebalance_experts_hierarchical(w, M, 1, 1, P)` 中 `log2mlog` 的值。

> **参考答案**：`tokens_per_group` 形状 `[L, 1]`，调 `balanced_packing` 时 `num_packs = 1`，`groups_per_pack = 1 // 1 = 1`，命中平凡分支返回 `pack_index = 0`、`rank_in_pack = 0`。代入 [eplb.py:106-107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107)：`((0 * 1 + 0) * E).unsqueeze(-1) + arange(E)`，即恒等置换 `0, 1, ..., E-1`。

**练习 2**：为什么说 e1100fe 之前"README 与实现不一致"？给出两段原文证据。

> **参考答案**：证据一，[README.md:29-31](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L29-L31)（自 2025-02-26 初始提交即存在）：全局策略 "replicates the experts globally regardless of expert groups, **and pack the replicated experts to individual GPUs**"。证据二，e1100fe 的 diff 删除行 `phy2log, phyrank, logcnt = replicate_experts(weight, num_replicas)`——旧实现只复制、不装箱，`phy2log` 的尾部 GPU 会集中分到冗余副本，与文档承诺的 "pack" 不符。

**练习 3**：e1100fe 之后，全局策略输出的 `phy2log` 每行仍然是 `0..M-1` 的一个置换吗？为什么？

> **参考答案**：是。Step 3 中 `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack`（[eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119)）——由于装箱的基数约束（每包恰好 `M/P` 个物品），`pack_index ∈ [0, P)` 与 `rank_in_pack ∈ [0, M/P)` 构成混合进制编码，`phy2pphy` 必为置换；`pphy2phy` 是其逆；其后每段 `gather` 复合的都是置换或"加常数偏移"，整条映射链保持双射。这正是 u3-l1 INV-2（值域覆盖）在全局策略下依然成立的原因。

### 4.3 变体实战：最大副本数受限的 replicate_experts

#### 4.3.1 概念说明

现在轮到你自己动手。原版 `replicate_experts` 的贪心没有任何副本数上限——极端长尾负载下，一个超重专家理论上可以吃掉全部 `R = M - E` 个冗余槽位。为什么要限制单专家最大副本数 `c`？至少四个工程理由：

1. **路由表体积**：`log2phy` 第三维等于 `maxlogcnt`（[eplb.py:157-158](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L158)）。一个专家 8 个副本，整张表就撑到 8——而其他专家可能只需要 2。
2. **显存**：每个副本是一份完整的专家权重。副本向单一专家集中，意味着"花在同一处的显存"快速增加，而 u3-l2 已证明冗余的均衡收益边际递减。
3. **路由复杂度**：框架侧要为每个逻辑专家维护副本列表并做流量均分（u3-l4），列表越长越笨重。
4. **均衡质量的再分配**：限制头部专家的副本数，等于把冗余预算让给第二梯队的专家——长尾场景下这可能反而是划算的（本讲 4.4 用实验检验这一点）。

变体的语义：**在"每轮复制 `weight/logcnt` 最大者"的原有贪心之上，把已达到 `max_replicas` 份的专家从候选中剔除**。

#### 4.3.2 核心流程

原版贪心的核心一行（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)）：

```python
redundant_indices = (weight / logcnt).max(dim=-1).indices
```

变体只改一处——在 `argmax` 之前用掩码屏蔽已达上限的专家：

\[ \text{priority}_e = \begin{cases} w_e / \text{cnt}_e, & \text{cnt}_e < c \\ -\infty, & \text{cnt}_e \ge c \end{cases} \]

每轮复制 \( \arg\max_e \text{priority}_e \)。可行性与正确性有两个必须推导的条件：

- **可行性（鸽笼条件）**：循环恰好执行 \( R = M - E \) 轮（u2-l2），每轮必须存在 `cnt < c` 的候选。至多可容纳的物理槽位为 \( E \cdot c \)，故需要
  \[ E \cdot c \;\ge\; M \quad\Longleftrightarrow\quad E(c-1) \ge R \]
  不满足时应在入口断言报错，否则 `max` 会在全 `-inf` 行上返回无意义的 0。
- **守恒不变量不破坏**：循环轮数、每轮恰好一个副本、`logcnt` 自增语义全部保持，所以 INV-4（每层 `logcnt` 之和 = M）自动成立。掩码只改变"给谁"，不改变"给多少"。

#### 4.3.3 源码精读

原版全文（[eplb.py:44-71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71)）中，与变体相关的骨架：

```python
def replicate_experts(weight: torch.Tensor, num_phy: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    n, num_log = weight.shape
    num_redundant = num_phy - num_log
    assert num_redundant >= 0
    ...
    for i in range(num_log, num_phy):
        redundant_indices = (weight / logcnt).max(dim=-1).indices      # ← 变体唯一要改的判据
        phy2log[:, i] = redundant_indices
        rank[:, i] = logcnt[arangen, redundant_indices]                # ← 契约:副本序号
        logcnt[arangen, redundant_indices] += 1                        # ← 契约:计数自增
    return phy2log, rank, logcnt
```

注意三点：① `rank` 取的是自增**前**的 `logcnt`，即第 0 个副本 rank=0、第 1 个 rank=1，掩码版必须原样保留这个时序；② 循环体对 `[n, num_log]` 的所有行一次性向量化（行维批量化，u3-l3），变体不引入任何按行循环；③ `weight / logcnt` 的 true division 结果必为浮点张量，所以对它 `masked_fill(-inf)` 永远类型安全——即使调用方传入整数 `weight`。

#### 4.3.4 代码实践

**实践目标**：实现 `replicate_experts_capped`，并验证它满足三个性质：契约一致、大上限下与原版逐位一致、小上限下分布被削平。

**操作步骤**：

1. 新建 `eplb_variant.py`（与 `eplb.py` 同目录；示例代码）：

```python
from typing import Tuple
import torch


def replicate_experts_capped(weight: torch.Tensor, num_phy: int,
                             max_replicas: int = 2**31) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """replicate_experts 的变体:限制单个逻辑专家的最大副本数。

    与原版唯一的行为差异:已达 max_replicas 份的专家不再进入复制候选。
    max_replicas 取极大值时,行为与原版 replicate_experts 逐位一致。
    """
    n, num_log = weight.shape
    num_redundant = num_phy - num_log
    assert num_redundant >= 0
    assert num_log * (max_replicas - 1) >= num_redundant, (
        f"max_replicas={max_replicas} 过小: {num_log} 个专家至多容纳 "
        f"{num_log * (max_replicas - 1)} 个冗余槽位,需要 {num_redundant} 个")
    device = weight.device
    phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
    rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
    logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
    arangen = torch.arange(n, dtype=torch.int64, device=device)
    for i in range(num_log, num_phy):
        priority = (weight / logcnt).masked_fill(logcnt >= max_replicas, float("-inf"))
        redundant_indices = priority.max(dim=-1).indices
        phy2log[:, i] = redundant_indices
        rank[:, i] = logcnt[arangen, redundant_indices]
        logcnt[arangen, redundant_indices] += 1
    return phy2log, rank, logcnt
```

2. 与原版做逐行 diff（肉眼即可）：唯一新增的是断言和 `priority` 一行的 `masked_fill`。
3. 写性质测试（示例代码）：

```python
import torch
from eplb import replicate_experts
from eplb_variant import replicate_experts_capped

w = torch.rand(4, 32) * 100

# 性质 1:契约一致 —— 三个输出形状与类型不变
p2l, r, c = replicate_experts_capped(w, 48, max_replicas=3)
assert p2l.shape == (4, 48) and r.shape == (4, 48) and c.shape == (4, 32)
assert c.sum(-1).eq(48).all()                      # INV-4 守恒
assert c.le(3).all(), "存在超过上限的副本数"          # 变体特有不变量

# 性质 2:大上限下与原版逐位一致(掩码永不触发)
p2l0, r0, c0 = replicate_experts(w, 48)
p2l1, r1, c1 = replicate_experts_capped(w, 48, max_replicas=48)
assert torch.equal(p2l0, p2l1) and torch.equal(r0, r1) and torch.equal(c0, c1)

# 性质 3:不可行参数应断言报错
try:
    replicate_experts_capped(w, 48, max_replicas=1)   # E(c-1)=0 < R=16
    raise RuntimeError("应当触发断言")
except AssertionError:
    pass
```

4. 用 README 示例的 `weight` 手工模拟 `max_replicas=2` 的前几轮：第 1 层权重降序前几名为 183、165、132、104…，每轮复制 `weight/logcnt` 最大者，观察最重专家拿到第 2 份后即被掩码，后续轮次转向次重专家。

**需要观察的现象**：性质 1-3 的断言全部通过；手工模拟中，被"封顶"的专家在后续轮次的 `priority` 中变为 `-inf`，副本预算流向第二梯队。

**预期结果**：`max_replicas=2` 时每行 `logcnt ∈ {1, 2}`，且恰好有 16 个（每层）专家持有 2 份副本（`R=4` 时为 4 个，依参数而定）；`max_replicas` 足够大时输出与原版逐位相同。手工模拟与程序输出一致。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`E=12, M=16`。`c=2` 是否可行？原版隐含的 `c` 上界是多少？

> **参考答案**：可行性条件 \( E(c-1) \ge R \)：\( 12 \times (2-1) = 12 \ge 4 \)，可行。原版无上限，极端情况下单一专家可独占全部 4 个冗余槽位，因此隐含上界 \( c_{\max} = R + 1 = 5 \)。

**练习 2**：为什么 `masked_fill` 要作用在 `weight / logcnt` 的结果上，而不是对 `weight` 本身掩码？

> **参考答案**：两个原因。① 类型安全：`masked_fill` 的填充值 `-inf` 只对浮点张量合法，`weight` 可能是整数（绕过入口直接调用时），而 true division 的结果必为浮点。② 语义正确：我们要屏蔽的是"优先级"（`argmax` 的输入），不是"权重"；对 `weight` 置 `-inf` 会同时污染除法的分子，且若未来有人把判据改成别的函数，掩码就失效了。掩码应紧跟决策点。

**练习 3**：变体在 `c` 极大时与原版"逐位一致"这个性质，对二次开发有什么工程价值？

> **参考答案**：它把变体变成了原版的**保守扩展**——上线时可以默认 `c=∞`（行为与旧版逐位相同，零回归风险），再按需收紧。同时"大上限等价"是最强的正确性测试之一：任何非掩码引入的差异（初始化、`rank` 时序、循环边界）都会破坏逐位一致而被立刻捕获。4.2 讲的"默认参数下与原版等价"原则在这里落到了实处。

### 4.4 接入与评测：收益、代价与耗时

#### 4.4.1 概念说明

变体写完只完成了一半。一个变体必须回答三个问题，每个问题对应一类指标：

| 维度 | 问题 | 指标 | 来自 |
| --- | --- | --- | --- |
| 结构正确性 | 契约破坏了吗？ | INV-1~7 + 变体特有不变量（`logcnt ≤ c`） | u3-l1 |
| 均衡质量 | GPU 负载变好还是变差？ | IB = max/mean（每 GPU） | u3-l2 |
| 资源与开销 | 路由表、副本分布、耗时呢？ | `maxlogcnt`、`logcnt` 直方图、`timeit` | 本讲 |

预期中的**代价与收益**（用实验验证，不要背结论）：

- 代价：`c` 越小，头部专家摊薄越不充分，IB 单调或不单调地恶化；`c` 小到接近可行性边界时恶化加速。
- 收益：`maxlogcnt` 被压到 `c`，`log2phy` 第三维变小（路由表更小）；副本分布从"头部集中"变为"梯队摊开"；耗时几乎不变（同样 \( R \) 轮循环，每轮只多一次逐元素的 `masked_fill`）。

还有一个**必须先想清楚的语义坑**：层级模式下，`replicate_experts` 是按节点切分调用的（[eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113) 传入的是 `num_physical_experts // num_nodes`，输入行数是 `num_layers * num_nodes`）。所以 `max_replicas` 在层级模式下是**每层每节点内**的上限——一个专家在两个节点各放 2 份，全局就是 4 份。只有全局策略（`num_nodes=1`）下它才是全局上限。这不是 bug，是层级架构的自然推论，但写进文档时必须说清。

#### 4.4.2 核心流程

接入五步（对应 4.1.2 五步法的 2-3 步展开）：

```text
1. 复制 rebalance_experts_hierarchical 全文到 eplb_variant.py,
   重命名为 rebalance_experts_hierarchical_capped,多收一个 max_replicas 参数
   —— 记得连嵌套的 inverse 函数一起复制 (eplb.py L98-101)
2. 把其中 Step 2 的一行换成变体调用:
   phy2mlog, phyrank, mlogcnt = replicate_experts_capped(
       tokens_per_mlog, num_physical_experts // num_nodes, max_replicas)
3. 复制入口三段式为 rebalance_experts_capped:
   规范化 (weight.float().cpu()) → 分派 (整除走层级参数,否则退化参数 1,1,P)
   → 组装 log2phy (复刻 eplb.py L157-161,分派两条分支都要带上 max_replicas)
4. 用 u3-l1 的 check_invariants 对变体入口全参数矩阵回归
5. 跑评测脚本,填 4.4.4 的对比表
```

评测脚本的核心是每 GPU 负载复算（承接 u3-l2 的槽位编码结论——GPU g 占连续槽位块）：

\[ \text{load}[l, g] = \sum_{s = g \cdot M/P}^{(g+1) \cdot M/P - 1} \frac{w[l,\; \text{phy2log}[l, s]]}{\text{logcnt}[l,\; \text{phy2log}[l, s]]}, \qquad \text{IB} = \max_g \frac{\text{load}[l, g]}{\text{mean}_g\, \text{load}[l, g]} \]

#### 4.4.3 源码精读

接入点与组装段的原文。Step 2 调用（替换点，[eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113)）：

```python
    tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
    phy2mlog, phyrank, mlogcnt = replicate_experts(tokens_per_mlog, num_physical_experts // num_nodes)
```

实验入口的组装段必须复刻的原文（[eplb.py:157-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L157-L161)，已在 4.2.3 引用全文）——特别注意 `maxlogcnt = logcnt.max().item()` 这一行：**变体的收益就直接体现在这里**。原版长尾下 `maxlogcnt` 可达 `R+1`，变体把它钳到 `c`，`log2phy` 的第三维随之收缩。

还有一个容易被忽略的细节：入口返回的 `phyrank` 被组装段消费后**不再外泄**（返回的是 `phy2log, log2phy, logcnt` 三元组，[eplb.py:162](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L162)）。所以只要变体正确维护了 `rank`，公开输出一个字节都不会变——接口保持封闭。

#### 4.4.4 代码实践

**实践目标**：完成变体的端到端接入，产出一张"原版 vs 变体"的三维对比表（均衡度、副本分布、耗时）。

**操作步骤**：

1. 按 4.4.2 的五步完成 `eplb_variant.py` 的接入部分（示例代码骨架）：

```python
import torch
import eplb
from eplb import rebalance_experts_hierarchical  # 仅作对照,实验函数为下面的复制版

def rebalance_experts_hierarchical_capped(weight, num_physical_experts,
                                          num_groups, num_nodes, num_gpus, max_replicas):
    # ============ 逐行复制 eplb.rebalance_experts_hierarchical (L89-129) ============
    # 仅两处不同:
    #   (1) 签名多收 max_replicas
    #   (2) Step 2 一行改为:
    #       phy2mlog, phyrank, mlogcnt = replicate_experts_capped(
    #           tokens_per_mlog, num_physical_experts // num_nodes, max_replicas)
    ...  # 含嵌套的 inverse (复制自 L98-101)

def rebalance_experts_capped(weight, num_replicas, num_groups,
                             num_nodes, num_gpus, max_replicas=2**31):
    num_layers, num_logical_experts = weight.shape
    weight = weight.float().cpu()
    if num_groups % num_nodes == 0:
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical_capped(
            weight, num_replicas, num_groups, num_nodes, num_gpus, max_replicas)
    else:  # 退化参数复用:全局策略同样受限
        phy2log, phyrank, logcnt = rebalance_experts_hierarchical_capped(
            weight, num_replicas, 1, 1, num_gpus, max_replicas)
    maxlogcnt = logcnt.max().item()                       # ← 变体收益在此体现
    log2phy = torch.full((num_layers, num_logical_experts, maxlogcnt), -1, dtype=torch.int64)
    log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank,
            torch.arange(num_replicas).expand(num_layers, -1))
    return phy2log, log2phy, logcnt
```

2. 评测脚本（示例代码，长尾负载生成 + 三维指标）：

```python
import time, torch
from eplb import rebalance_experts
from eplb_variant import rebalance_experts_capped

def longtail_weight(L, E, alpha=2.0, seed=0):
    """幂律型长尾负载: alpha 越大,头部越重。归一化到层均值 100。"""
    g = torch.Generator().manual_seed(seed)
    w = 1.0 / torch.rand(L, E, generator=g) ** alpha
    return w / w.sum(-1, keepdim=True) * (E * 100)

def gpu_imbalance(weight, phy2log, logcnt, P):
    per_slot = weight.gather(-1, phy2log) / logcnt.gather(-1, phy2log)   # [L, M]
    load = per_slot.view(weight.size(0), P, -1).sum(-1)                   # [L, P]
    return (load.max(-1).values / load.mean(-1)).max().item()             # 最差层的 IB

L, E, M, P = 4, 64, 80, 8
w = longtail_weight(L, E, alpha=2.0)

for name, fn, kw in [("原版", rebalance_experts, {}),
                     ("c=4", rebalance_experts_capped, {"max_replicas": 4}),
                     ("c=2", rebalance_experts_capped, {"max_replicas": 2})]:
    t0 = time.perf_counter()
    for _ in range(20):
        out = fn(w, M, num_groups=3, num_nodes=2, num_gpus=P, **kw)   # G=3 → 全局策略
    dt = (time.perf_counter() - t0) / 20 * 1000
    p2l, l2p, cnt = out
    hist = cnt.flatten().bincount(minlength=6)[:6].tolist()           # 副本数 0..5 的专家个数
    print(f"{name}: IB={gpu_imbalance(w, p2l, cnt, P):.3f}  "
          f"maxlogcnt={l2p.size(-1)}  耗时={dt:.2f}ms  logcnt直方图={hist}")
```

3. 先跑 `check_invariants`（u3-l1 产物）对 `rebalance_experts_capped` 过一遍参数矩阵，确认 INV 全绿再评测。

**需要观察的现象**：

- `c=4` 的 IB 略差于原版，`c=2` 更差（头部专家摊薄受限）；
- `maxlogcnt` 从原版的较大值（取决于长尾陡峭程度）降到 `c`；
- `logcnt` 直方图：原版头部堆积（直方图右移），变体被削平、向左截断；
- 耗时三列几乎相同（每轮仅多一次 `masked_fill`）。

**预期结果**：一张形如下面的表（数值待本地验证，以实际输出为准）：

| 版本 | IB (max/mean) | maxlogcnt | 耗时 | logcnt 直方图特征 |
| --- | --- | --- | --- | --- |
| 原版 | 最小 | 可能较大 | 基准 | 头部堆积 |
| c=4 | 略差 | ≤ 4 | ≈ 基准 | 梯队摊开 |
| c=2 | 明显差 | ≤ 2 | ≈ 基准 | 上限截断 |

#### 4.4.5 小练习与答案

**练习 1**：层级模式下（`num_nodes=2`）设 `max_replicas=2`，一个逻辑专家全局最多几份副本？

> **参考答案**：最多 4 份。Step 2 按节点切分调用（[eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113)），掩码作用在节点内的局部 `logcnt` 上，每个节点独立允许 2 份。若业务上需要"全局副本上限"，要么在层级模式外再套一层检查，要么只对全局策略启用该参数——语义必须在文档里写死。

**练习 2**：变体让 `maxlogcnt` 变小、`log2phy` 第三维变小，这为什么是真实收益而不只是形状好看？

> **参考答案**：`log2phy` 是下游路由侧的核心数据结构（u3-l4）：路由器要按 `(逻辑专家, 副本序号)` 从这张表寻址物理专家并把流量均分到各副本。第三维 = `maxlogcnt` 意味着**所有**专家都按最坏情况占用路由表空间（定长张量化，u2-l6）。一个 8 副本的极端专家会让全部专家的路由条目翻数倍。压平 `maxlogcnt` 直接缩小这张表的内存与广播开销。

**练习 3**：如果实验发现 `c=2` 时 IB 恶化 20%，而 `c=4` 只恶化 2%，你如何决策？

> **参考答案**：没有唯一答案，决策框架是把三笔账摆在一起：① IB 恶化带来的算力浪费（u3-l2：IB 的倒数是理论利用率上限，20% 恶化 ≈ 利用率上限降约 17%）；② 路由表与显存的节省量（`maxlogcnt` 从多少降到多少）；③ 与 u3-l4 的迁移开销联动（副本分布更平滑通常也意味着重排时迁移的权重张量更少）。若显存不紧张且路由表本来不大，选原版或 `c=4`；若在 decode 大 EP 场景（全局策略）且路由表是瓶颈，`c=4` 的 2% 代价很可能值得。

### 4.5 更远的变体方向：加权装箱与放置约束

#### 4.5.1 概念说明

缝 B（`balanced_packing`）同样可做变体，而且改起来更小。当前装箱判据是"未满包中最轻者"：

[eplb.py:34-35](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L34-L35)

```python
            pack = min((i for i in range(num_packs) if pack_items[i] < groups_per_pack),
                       key=pack_weights.__getitem__)
```

这隐含一个**同构假设**：所有包（GPU / 节点）容量同质、无先验偏好。现实集群常常不满足：节点间带宽不同、GPU 新旧混布、某些卡上还跑着别的任务。两个自然的变体方向：

- **带宽加权装箱**：节点 \( i \) 的"轻重"不用绝对负载衡量，而用相对其带宽的负载衡量——让快的车拉更多的货。
- **异构容量**：不同包装不同数量的物品。

#### 4.5.2 核心流程

**带宽加权**只需改 `key`（把权重换成"归一化负载"）：

\[ \text{score}_i = \frac{\text{pack\_weights}_i}{\text{bandwidth}_i}, \qquad \text{pack} = \arg\min_{i:\, \text{未满}} \text{score}_i \]

**异构容量**则要动三处联动的地方，这是本讲最后想让你带走的风险意识：

1. `pack_items[i] < groups_per_pack` 的容量上限要变成 per-pack 数组；
2. 排序与断言（[eplb.py:19-20](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L19-L20)）假设 `n % m == 0` 均分——异构下不再成立，要改为"容量之和等于 n"；
3. **最隐蔽的一处**：下游槽位编码 [eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) `phy2pphy = pack_index * phy_experts_per_gpu + rank_in_pack` 用统一的 `phy_experts_per_gpu` 做步长——它假设每包等槽位。异构容量下这个编码会产生冲突（两个不同包的槽位映射到同一编号），`inverse` 随之失效。必须改为 per-pack 偏移表 `offset[pack]`。

也就是说：**改一处判据是安全的，改一处"结构假设"会沿映射链扩散**。判断某个变体落在哪一类，是二次开发前最重要的风险评估。

#### 4.5.3 源码精读

对照两处源码体会"判据改动"与"结构改动"的差别。判据（安全改动点）：

[eplb.py:30-40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L40)

```python
    for i in range(num_layers):
        pack_weights = [0] * num_packs
        pack_items = [0] * num_packs
        for group in indices[i]:
            pack = min((i for i in range(num_packs) if pack_items[i] < groups_per_pack),
                       key=pack_weights.__getitem__)
            assert pack_items[pack] < groups_per_pack
            pack_index[i, group] = pack
            rank_in_pack[i, group] = pack_items[pack]
            pack_weights[pack] += weight[i, group]
            pack_items[pack] += 1
```

`min(..., key=...)` 的 `key` 是唯一需要替换的决策函数；只要仍然保证"每包恰好装满 `groups_per_pack` 个"，`pack_index / rank_in_pack` 的输出契约不变，L119 的编码照常成立。而结构假设（危险改动点）就是 L119 与 L20 的 `groups_per_pack = num_groups // num_packs`——它们与装箱的基数约束互为因果（u2-l1：基数约束正是编码双射的前提）。

#### 4.5.4 代码实践

**实践目标**：设计（不要求完整实现）带宽加权装箱的最小改动方案，并预判其对全链路的影响。

**操作步骤**：

1. 复制 `balanced_packing` 为 `balanced_packing_weighted`，签名多收一个 `pack_capacity_factor: torch.Tensor`（长度 `num_packs`，如带宽比例）。
2. 把 `key=pack_weights.__getitem__` 改为 `key=lambda i: pack_weights[i] / factor[i]`（注意 `factor` 需为正浮点，建议入口断言）。
3. 推演三个问题：① 输出契约（每包物品数仍为 `n/m`）变了吗？② 层级函数 Step 1 与 Step 3 的调用点需要改吗？③ 语义上"带宽高的节点分到更重的组"——这和 u1-l2 讲的"组受限路由要求同组同节点"约束冲突吗？

**需要观察的现象**（若实现了并接入层级函数 Step 1 跑通）：带宽因子差异大时，高带宽节点上的组总负载应显著高于低带宽节点；`phy2log` 仍过全部 INV。

**预期结果**：契约不变、调用点零改动（缝 B 影响半径小的直接推论）；但第 ③ 问的答案值得写进你的实验记录：加权装箱改变的是"节点间负载的度量衡"，而组-节点对齐约束（INV-7）限制的是"哪些组能去哪些节点"，两者正交、可以叠加。运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：把 `key=pack_weights.__getitem__` 改成 `key=lambda i: pack_weights[i] * factor[i]`（`factor` 为归一化带宽的倒数），与除法版本语义等价吗？

> **参考答案**：等价（`factor` 取倒数时）。两者都是在最小化"相对负载"——除法版本 `w_i / b_i` 与乘法版本 `w_i \cdot (1/b_i)` 给出相同的排序。工程上更推荐乘法（预计算倒数、避免每轮除法与除零），但要在入口断言 `b_i > 0`。

**练习 2**：为什么异构容量不能只改 `pack_items` 的上限？

> **参考答案**：见 4.5.2 的三处联动。特别是 [eplb.py:119](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119) 的槽位编码用统一步长 `phy_experts_per_gpu`，异构下不同包的 `(pack_index, rank_in_pack)` 会编码到相同槽位号，`phy2pphy` 不再是置换，`inverse` 与整条映射链全部失效——而这个失效离改动点有六行之远，极易漏改。

**练习 3**：想引入"相邻重排之间放置方案尽量平滑"（减少 u3-l4 的权重迁移量），应动哪条缝？为什么它比前两个变体都难？

> **参考答案**：缝 C（整体流程）。平滑性是**跨调用、跨层**的全局性质，需要把上一轮的放置方案作为额外输入传进决策——这打破了三个函数"无状态纯函数"的共同前提，也让 u3-l1 的 INV 测试需要新增时间维度的对照。它本质上是在改"决策问题的定义"，而不只是换求解策略。

## 5. 综合实践

把本讲所有内容串成一个完整的二次开发闭环。**任务**：交付一份《EPLB 变体实验报告》，包含代码、测试与数据三部分。

**步骤**：

1. **实现**（4.3）：在 `eplb_variant.py` 中实现 `replicate_experts_capped`，满足"大上限下与原版逐位一致"性质。
2. **接入**（4.4）：复制并改造 `rebalance_experts_hierarchical_capped` 与 `rebalance_experts_capped`，两条分派分支（层级 + 退化参数 `(1,1,P)`）都要支持 `max_replicas`。注意复制嵌套的 `inverse`。
3. **回归**（u3-l1 资产）：用你的 `check_invariants` 对变体入口跑参数矩阵（至少覆盖：层级 `G=4,N=2`；全局 `G=3,N=2`；边界 `groups_per_pack=1`、`num_nodes=1`；`c` 取可行边界值），外加变体特有断言 `logcnt ≤ c`（层级模式下按"每节点 ≤ c"校验）。
4. **评测**（4.4.4）：在 `alpha ∈ {1, 2, 3}` 三档长尾负载 × `{原版, c=4, c=2}` × `{层级, 全局}` 两策略下，输出 IB、`maxlogcnt`、耗时、`logcnt` 直方图的完整表格。
5. **报告**（一页以内）：给出你的 `c` 推荐值及理由（用 4.4.5 练习 3 的决策框架），并写一段"退化参数复用"模式的心得——如果你来评审 e1100fe 这个 PR，你会要求作者补充什么？（提示：INV 回归、退化组合的测试覆盖、注释里写明退化语义。）

**验收标准**：INV 全绿；`c` 极大时与原版逐位一致的断言通过；评测表三列齐全；报告有明确结论而非"各有优劣"。

## 6. 本讲小结

- eplb.py 有三条扩展缝：**缝 A**（`replicate_experts`，复制谁）、**缝 B**（`balanced_packing`，放在哪）、**缝 C**（层级函数，整体流程）；公开契约只有 `rebalance_experts` 的五参数三元组，契约不变即接口不破。
- **e1100fe** 是退化参数复用的真实案例：全局策略从独立调用 `replicate_experts`（只复制、不装箱，尾部 GPU 堆满重载副本，README 的承诺迟到近一个月）重构为 `hierarchical(weight, M, 1, 1, P)`——Step 1 经平凡分支退化为恒等置换、Step 2 变全局复制、Step 3 变 GPU 级装箱。
- 该模式的优点是**单一事实源、行为可静态推导、测试面收缩**；代价是**缺陷同样被复用**（d52c72d 修的设备 bug 在复用后波及两条分支）、可读性下降、与层级函数的前置断言形成隐式契约绑定——重构后必须回归 INV 测试。
- 变体开发五步法：定缝 → 复制 → 保契约 → 回归 → 评测；`replicate_experts_capped` 用 `masked_fill` 屏蔽达上限专家，仅改一行判据，循环轮数与守恒不变量自动保持，可行性由鸽笼条件 \( E(c-1) \ge R \) 保证。
- 变体的收益与代价必须用指标说话：IB 恶化多少、`maxlogcnt`（即 `log2phy` 第三维，路由表体积）缩小多少、耗时变化多少；层级模式下 `max_replicas` 是**每节点**上限而非全局上限。
- 改**判据**安全（如带宽加权装箱只动 `min` 的 `key`），改**结构假设**危险（如异构容量会破坏 L119 的等步长槽位编码，失效点离改动点六行之远）——动手前先判断变体属于哪一类。

## 7. 下一步学习建议

本手册到此收官。沿着本讲打开的方向，推荐三条继续深入的路线：

1. **把变体做扎实**：完成综合实践后，尝试 4.5 的带宽加权装箱，并挑战练习 3 的"层间平滑"变体（它会逼你把 u3-l4 的迁移开销模型和 u3-l1 的 INV 体系扩展到时间维度）。如果结果漂亮，`max_replicas` 以带默认值关键字参数的形式正是一次真实的 upstream PR 素材——e1100fe 只有 1 行改动加注释，你的 PR 也可以很小。
2. **走出仓库**：EPLB 只负责"负载估计 → 放置方案"这一段（README L11-13）。回到 u3-l4 的闭环视角，阅读你所使用的训练/推理框架中消费 `phy2log / log2phy / logcnt` 的代码，观察 `-1` padding 如何被 `logcnt` 掩码、流量如何被均分到副本——你会发现本手册的每个不变量在下游都有一个对应的消费假设。
3. **回到问题域**：重读 DeepSeek-V3 论文中关于冗余专家与组受限路由的章节（u1-l2 的背景），此时你已经能从工程约束反推论文里每一句"we attempt to"背后的取舍——从读懂代码到读懂设计，是这套手册希望你走完的最后一里路。
