# replicate_experts：冗余副本的贪心分配

## 1. 本讲目标

上一讲我们精读了 `balanced_packing`，它回答的是"**放在哪**"——把物品装进哪个包。本讲精读它的搭档 `replicate_experts`，回答的是"**复制谁**"——在固定的物理专家预算下，把冗余副本分配给哪些逻辑专家。

学完本讲你应该能够：

1. 解释为什么 `weight / logcnt` 近似"每个副本的期望负载"，以及贪心每轮复制它的最大者等价于"尽量拉平所有副本的水位"。
2. 推导循环 `for i in range(num_log, num_phy)` 恰好执行 `num_phy - num_log` 次、恰好生成 `num_phy - num_log` 个冗余专家。
3. 说出 `phy2log`、`rank`、`logcnt` 三个输出分别在整个放置方案中扮演的角色，以及它们在 `rebalance_experts_hierarchical` 和入口函数中的下游去向。
4. 亲手实现一个"每轮直接复制当前最重专家"的朴素对照版本，并用实验验证 `weight / logcnt` 贪心的优势。

## 2. 前置知识

### 2.1 回顾：复制是为了摊薄，不是为了省算力

- **逻辑专家 / 物理专家**：MoE 层里路由器眼里的专家编号是逻辑专家；实际部署时一个逻辑专家可以有多个物理副本。复制重载专家后，发给它的 token 会被分摊到各个副本上，单副本负载下降。
- **`weight` 的语义**：`[层数, 逻辑专家数]` 的负载统计（比如一段时间内路由到各专家的 token 数）。README 明确说负载预测方法（如历史统计的滑动平均）不在本仓库范围内，EPLB 拿到的是已经估计好的 `weight`。
- **副本间流量均分假设**：本函数的全部数学都建立在"发给逻辑专家 \( j \) 的流量在其 \( c_j \) 个副本间均匀分摊"这一假设上。此时单副本期望负载就是 \( w_j / c_j \)。
- **预算约束**：物理专家总数 `num_phy` 是固定预算（显存决定的槽位数），所以"复制谁"是一个零和分配问题：给了这个专家，别的专家就少一个名额。

### 2.2 本讲要用到的 PyTorch 操作

| 操作 | 作用 |
| --- | --- |
| `t.max(dim=-1).indices` | 按最后一个维度取最大值并返回** argmax 索引**，形状比 `t` 少一维 |
| `t[k_rows, k_cols]`（高级索引） | 两个一维整数张量配对索引，`out[i] = t[k_rows[i], k_cols[i]]` |
| `t[k_rows, k_cols] += 1` | 对上述配对位置做原地自增 |
| `t.repeat(n, 1)` | 把张量在第 0 维重复 `n` 次 |
| 张量除法 `weight / logcnt` | PyTorch 的 `/` 恒为真除法，整数张量相除也会提升为浮点 |

高级索引配对写法 `logcnt[arangen, redundant_indices]` 如果读着别扭，4.3 节会给出等价的 `gather` 写法。

## 3. 本讲源码地图

本仓库的核心实现只有一个文件，本讲聚焦其中一个函数及其上下游：

| 位置 | 作用 |
| --- | --- |
| [eplb.py:44-71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L71) | **`replicate_experts` 本体**，本讲主角 |
| [eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113) | 层级策略 Step 2 中的调用点：把 `weight` 重排后 view 成 `[层数×节点数, ...]` 再传入 |
| [eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117) | 下游 1：`phy2mlog` 与 `mlogcnt` 用来算每个副本的负载，供 Step 3 打包到 GPU |
| [eplb.py:127-128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L127-L128) | 下游 2：`phyrank`、`mlogcnt` 被重排回原始逻辑编号顺序 |
| [eplb.py:160-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L160-L161) | 下游 3：入口用 `phy2log * maxlogcnt + phyrank` 散射构造逆映射 `log2phy`，`rank` 在这里不可或缺 |
| [eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) | 入口的 `weight.float().cpu()`：保证本函数拿到的 `weight` 是 CPU 浮点张量 |
| [README.md:37-57](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md#L37-L57) | 两层 12 专家示例，可用于端到端验证 |

## 4. 核心概念与源码讲解

本讲的最小模块是 `replicate_experts`，我们把它拆成三个递进的侧面：问题定义与骨架、贪心准则、输出构造与批量并行。

### 4.1 replicate_experts（上）：问题定义与整体骨架

#### 4.1.1 概念说明

给定每个逻辑专家的负载 `weight`（形状 `[X, num_log]`，`X` 通常是把"层数×节点数"压平后的行数）和物理专家总数 `num_phy`，本函数要决定：`num_phy - num_log` 个冗余名额各给谁，才能让**所有副本中最大的单副本负载**尽量小。

这是一个组合分配问题。穷举所有分配显然不可行（把 `num_phy - num_log` 个名额分给 `num_log` 个专家是组合爆炸的），而且它和装箱问题一样是 NP-难家族的近亲，所以源码选择贪心：一次复制一个，每一步都复制"当前最惨"的那个专家。文档字符串直白地写出了这个目标：

```python
Replicate `num_log` experts to `num_phy` replicas, such that the maximum load of all replicas is minimized.
```

注意与 `balanced_packing` 的分工：那一讲解决"放在哪"（放置），这一讲解决"复制谁"（冗余分配）。层级策略把两者串成流水线：先复制，再把复制出来的物理专家装箱。

#### 4.1.2 核心流程

```text
输入: weight [X, num_log], num_phy (>= num_log)
初始化: 每个逻辑专家恰好 1 个副本（0 号副本）
循环 num_phy - num_log 次:
    1. 对每一行（每一层/每个节点），计算所有专家的 weight / logcnt
    2. 取最大者的下标 redundant_indices（每行一个）
    3. 新物理槽位 i 记录: phy2log[:, i] = 该专家编号
                         rank[:, i]   = 它已有的副本数（成为第 rank 个副本）
                         logcnt[该专家] += 1
输出: phy2log [X, num_phy], rank [X, num_phy], logcnt [X, num_log]
```

关键点：循环变量 `i` 既是轮次，也是**新副本写入的物理槽位编号**，所以循环跑完，`num_log` 到 `num_phy - 1` 的每个槽位都被恰好写入一次。

#### 4.1.3 源码精读

先看签名、形状契约与唯一的断言：

```python
def replicate_experts(weight: torch.Tensor, num_phy: int) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    n, num_log = weight.shape
    num_redundant = num_phy - num_log
    assert num_redundant >= 0
```

[eplb.py:44-60](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L44-L60) 定义函数并分解形状：`n` 是行数（层数，或层级策略下的层数×节点数），`num_log` 是逻辑专家数；`num_redundant` 就是冗余名额数，`assert num_redundant >= 0` 保证物理专家数不少于逻辑专家数（每个专家至少要有一个副本）。文档字符串（L46-57）逐一声明了三个输出的形状，这是本函数对外的"合同"。

循环的边界值得盯着看一眼：

```python
    for i in range(num_log, num_phy):
```

[eplb.py:66](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66) 的循环从 `num_log` 跑到 `num_phy - 1`，共 `num_phy - num_log` 次。前 `num_log` 个物理槽位（编号 `0..num_log-1`）在初始化时就已经是"每个逻辑专家的 0 号副本"，循环只负责**追加**冗余槽位。由此可得本讲的第二个学习目标：

\[ \text{复制次数} = \text{num\_phy} - \text{num\_log} = \text{num\_redundant} \]

每个逻辑专家的最终副本数为 \( c_j = 1 + (\text{它被循环选中的次数}) \)，且所有 \( c_j \) 之和恒等于 `num_phy`——这正是 u1-l3 观察到的"每行 logcnt 之和恒等于 num_replicas"的来源。

#### 4.1.4 代码实践

1. **实践目标**：跑通函数并验证基本合同（形状、取值范围、计数一致）。
2. **操作步骤**（示例代码）：

   ```python
   import torch
   from eplb import replicate_experts   # 模块内函数可直接导入，__all__ 不影响具名导入

   weight = torch.tensor([[ 90., 132.,  40.,  61., 104., 165.,  39.,   4.,  73.,  56., 183.,  86.]])
   phy2log, rank, logcnt = replicate_experts(weight, num_phy=16)

   assert phy2log.shape == (1, 16) and rank.shape == (1, 16) and logcnt.shape == (1, 12)
   assert (phy2log < 12).all()                       # 槽位里只存逻辑编号
   counts = torch.bincount(phy2log[0], minlength=12)
   assert (counts == logcnt[0]).all()                # phy2log 的计数与 logcnt 一致
   assert logcnt.sum() == 16                         # 副本总数 = num_phy
   print("phy2log =", phy2log.tolist())
   print("rank    =", rank.tolist())
   print("logcnt  =", logcnt.tolist())
   ```

3. **需要观察的现象**：`logcnt` 中哪些专家是 2、哪些还是 1；`phy2log` 尾部追加的 4 个编号是什么。
4. **预期结果**：`logcnt.sum() == 16` 与计数一致这两条断言必然成立（由上一节的推导保证）；具体哪些专家被复制取决于负载分布，负载最大的 183、165 对应的专家大概率拿到副本（待本地验证具体数值）。

#### 4.1.5 小练习与答案

**练习 1**：如果调用 `replicate_experts(weight, num_phy=weight.shape[1])`（即 `num_phy == num_log`），函数返回什么？这对应什么部署形态？

**答案**：`range(num_log, num_phy)` 为空，循环体一次都不执行，直接返回初始化值：`phy2log` 每行是 `0..num_log-1` 的恒等映射，`rank` 全 0，`logcnt` 全 1。这就是"关闭冗余"的基线部署（每个逻辑专家恰好一个物理副本），也是 u3-l2 评测实验里的对照组。

**练习 2**：为什么断言只检查 `num_redundant >= 0`，而不检查 `num_phy` 能否被 GPU 数整除？

**答案**：整除约束属于**调用方**的职责。本函数只做"复制谁"，完全不知道 GPU、节点的存在；`num_physical_experts % num_gpus == 0` 的断言在 [eplb.py:95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95) 由 `rebalance_experts_hierarchical` 检查。职责边界清晰是这段代码的一个优点：算法函数只管算法。

### 4.2 replicate_experts（中）：weight/logcnt 贪心准则

#### 4.2.1 概念说明

整个函数的灵魂是第 67 行的一个除法。设逻辑专家 \( j \) 的负载为 \( w_j \)、副本数为 \( c_j \)。在"副本间流量均分"的假设下，它的**单副本期望负载**是：

\[ \ell_j = \frac{w_j}{c_j} \]

多复制一个副本，\( c_j \) 加一，水位 \( \ell_j \) 就降一截。把每个专家想象成一根粗细可调的量杯：负载 \( w_j \) 是水量，副本数 \( c_j \) 是底面积，水位就是 \( w_j / c_j \)。**贪心策略 = 每次给当前水位最高的量杯加宽一格**，反复压低最高水位。

为什么这样能近似最小化"最大单副本负载"？因为任何放置方案的最大水位有一个无法突破的下界。由 \( \sum_j w_j = \sum_j c_j \ell_j \le \text{num\_phy} \cdot \max_j \ell_j \) 可得：

\[ \max_j \ell_j \;\ge\; \frac{\sum_j w_j}{\text{num\_phy}} \]

贪心每一步都直接攻击当前的最高水位，直觉上就是在逼近这条下界。需要诚实说明的是：这是启发式，不是严格最优解的证明——对"最小化最大负载"这个目标它表现非常好（反例很难找，见综合实践），但换成别的指标（比如最小化负载方差、最小化前两大负载之和）它未必最优；而且它完全依赖流量均分假设，若路由器把副本当Distinct 专家对待、流量不均，实际负载会偏离 \( w_j / c_j \)。

还要注意与"朴素思路"的对比：朴素的想法是"谁负载最大就复制谁"，即每轮取 \( \arg\max_j w_j \)。但 \( w_j \) 在循环中从不变化，所以朴素版本每轮都会选中**同一个**专家，把全部冗余预算押在最初最重的那个专家身上，直到它不再是最重。`weight / logcnt` 的分母让选择"与时俱进"——已经被复制过的专家，其有效负载要打折。这是本讲综合实践要量化对比的核心差异。

#### 4.2.2 核心流程

一轮循环的三个动作（对 `n` 行同时做，行与行互不影响）：

```text
每轮 i:
    levels      = weight / logcnt            # [n, num_log]，各行独立的水位
    chosen      = argmax_j levels[k, j]      # 每行 k 各选一个专家
    phy2log[:, i] = chosen                   # 新槽位装这个专家
    rank[:, i]     = logcnt[k, chosen]       # 它成为"第 logcnt 个"副本（自增前的旧值）
    logcnt[k, chosen] += 1                   # 副本数 +1，水位下降
```

手算示例（无并列，结果确定）：`weight = [[8, 7, 6, 3, 1]]`，`num_phy = 8`：

| 轮次 | 槽位 i | 水位 \(w_j/c_j\) | 选中 | 更新后 logcnt |
| --- | --- | --- | --- | --- |
| 初始 | — | 8, 7, 6, 3, 1 | — | [1, 1, 1, 1, 1] |
| 1 | 5 | **8**, 7, 6, 3, 1 | 专家 0 | [2, 1, 1, 1, 1] |
| 2 | 6 | 4, **7**, 6, 3, 1 | 专家 1 | [2, 2, 1, 1, 1] |
| 3 | 7 | 4, 3.5, **6**, 3, 1 | 专家 2 | [2, 2, 2, 1, 1] |

最终最大水位 \( \max_j w_j/c_j = \max(4, 3.5, 3, 3, 1) = 4 \)；对比下界 \( (8+7+6+3+1)/8 = 3.125 \)，贪心结果离下界不远。

#### 4.2.3 源码精读

贪心准则本体只有一行：

```python
        redundant_indices = (weight / logcnt).max(dim=-1).indices
```

[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67) 对整个 `[n, num_log]` 矩阵按最后一维取 argmax：`weight / logcnt` 逐元素相除得到每行水位（`weight` 是浮点、`logcnt` 是整型，PyTorch 的 `/` 是真除法，结果为浮点），`max(dim=-1).indices` 返回形状 `[n]` 的每行最大值下标。**一次调用同时为所有层/所有节点做决策**，这是本函数效率的关键（详见 4.3）。

一个小小的实现细节：当某行存在并列最大值时，`torch.max` 返回哪个下标在官方文档中并未跨设备严格承诺（CPU 实现上通常取最靠前的那个）。并列时选谁不影响"当前最大水位"的值，但会影响最终副本分布——做实验时如果观察到并列情形，值得留意这一点（待本地验证）。

随后三行完成"记录 + 计数"：

```python
        phy2log[:, i] = redundant_indices
        rank[:, i] = logcnt[arangen, redundant_indices]
        logcnt[arangen, redundant_indices] += 1
```

[eplb.py:68-70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L68-L70)：第 68 行把选中的逻辑编号写进新槽位；第 69 行用高级索引 `logcnt[arangen, redundant_indices]` 取出"第 k 行选中专家的当前副本数"（自增**前**的旧值），所以新副本拿到的 `rank` 恰好是 1, 2, 3, ... 无空档；第 70 行原地自增，让下一轮的水位计算生效。顺序很重要：先取旧值做 `rank`，再自增。

#### 4.2.4 代码实践

1. **实践目标**：验证 4.2.2 的手算表与代码行为完全一致。
2. **操作步骤**（示例代码）：

   ```python
   import torch
   from eplb import replicate_experts

   weight = torch.tensor([[8., 7., 6., 3., 1.]])
   phy2log, rank, logcnt = replicate_experts(weight, num_phy=8)

   print(phy2log)   # 预期 [[0, 1, 2, 3, 4, 0, 1, 2]]
   print(rank)      # 预期 [[0, 0, 0, 0, 0, 1, 1, 1]]
   print(logcnt)    # 预期 [[2, 2, 2, 1, 1]]
   print(weight / logcnt)          # 每副本期望负载，预期 [[4, 3.5, 3, 3, 1]]
   print((weight / logcnt).max())  # 最大水位，预期 4
   ```

3. **需要观察的现象**：三个张量是否与手算表一致；`weight / logcnt` 的最大值是否为 4。
4. **预期结果**：本例无并列，每一步 argmax 都唯一，上述输出是确定的（可先遮住答案手算再对照）。若你改动了权重引入并列（例如把 7 改成 6），观察被复制的专家编号是否会变化。

#### 4.2.5 小练习与答案

**练习 1**：手算 `replicate_experts(torch.tensor([[12., 9., 6.]]), num_phy=5)` 的三个输出。

**答案**：第 1 轮水位 `[12, 9, 6]` 选中专家 0；第 2 轮水位 `[6, 9, 6]` 选中专家 1。结果：`phy2log = [[0, 1, 2, 0, 1]]`，`rank = [[0, 0, 0, 1, 1]]`，`logcnt = [[2, 2, 1]]`，最终水位 `[6, 4.5, 6]`，最大 6。

**练习 2**：负载 `[[10, 10, 10, 1]]`、`num_phy = 6`，朴素版（每轮复制 `argmax w`）和贪心版各会怎么分配？

**答案**：贪心版：轮 1 选中专家 0（水位 `[10,10,10,1]` 并列，CPU 上通常取最前者），轮 2 水位 `[5,10,10,1]` 选中专家 1，得 `logcnt = [2,2,1,1]`，最大水位 5。朴素版同样选中专家 0、然后是专家 1——在这个例子里两者一致，因为并列时无论选谁水位都相同。真正拉开差距的是"最重专家被复制后仍然长期独占 argmax(w)"的场景（如综合实践中 `[10, 9, 5, 2]` 的例子）：朴素版会连复制最重专家，把第二重的专家晾在一边。

**练习 3**：为什么说贪心"不保证最优"？请给出一个它可能失误的角度。

**答案**：参考角度一：它只优化"最大单副本负载"这一项指标，若目标改为最小化方差或前两大负载之和，每轮压最高水位的动作未必最优；参考角度二：它假设副本间流量均分，若上层路由对副本的分流不均，真实水位会偏离 \( w_j/c_j \)，贪心的依据本身失真；参考角度三：并列时的选择会影响后续分配，而并列的打破方式并非由目标函数决定。

### 4.3 replicate_experts（下）：三个输出与多层批量并行

#### 4.3.1 概念说明

三个输出是同一份冗余分配方案的三种读法，缺一不可：

| 输出 | 形状 | 语义 | 在放置方案中的角色 |
| --- | --- | --- | --- |
| `phy2log` | `[X, num_phy]` | 每个物理槽位装的是哪个逻辑专家 | 正向表：后续装箱（"放在哪"）的对象就是这些槽位 |
| `rank` | `[X, num_phy]` | 该槽位是此逻辑专家的第几个副本（0, 1, 2, ...） | 给同一逻辑专家的多个副本发"身份证号"，用于无冲突地构造逆映射 |
| `logcnt` | `[X, num_log]` | 每个逻辑专家有几个副本 | 冗余程度的计数表，也用来计算每副本负载 |

`rank` 为什么必须有？想象只有 `phy2log` 和 `logcnt`：想知道"逻辑专家 5 的副本都在哪些物理槽位"需要扫描全表；更糟的是，入口函数要把逆映射 `log2phy` 的槽位地址编码成一个整数（`phy2log * maxlogcnt + phyrank`）。如果同一逻辑专家的两个副本没有可区分的 `rank`，它们会散射到同一个地址互相覆盖。`rank` 保证了每行内 `(逻辑编号, rank)` 二元组**处处唯一且从 0 连续编号**，这正是逆映射能干净构造的前提。

另一个容易被忽视的维度是**批量并行**：`weight` 的第一维可以是任意行数，循环里所有操作（除法、argmax、高级索引、自增）都是按行独立、一次算完所有行的。Python 层的循环长度只等于 `num_phy - num_log`（每节点冗余数，通常个位到几十），与层数完全无关。

#### 4.3.2 核心流程

初始化阶段为三个输出搭好"无冗余"的起点：

```text
phy2log = 每行 0..num_phy-1（前 num_log 列即恒等映射，尾部列是占位符）
rank    = 全 0（初始副本都是第 0 号）
logcnt  = 全 1（每个专家起初一个副本）
arangen = 0..n-1（高级索引的行下标）
循环 num_phy - num_log 轮，逐列覆写尾部槽位
```

随后循环逐列改写，直到尾部所有占位符都被真实编号替换。

#### 4.3.3 源码精读

初始化四行：

```python
    device = weight.device
    phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
    rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
    logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
    arangen = torch.arange(n, dtype=torch.int64, device=device)
```

[eplb.py:61-65](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L61-L65) 有一个初看可疑的细节：`phy2log` 用的是 `arange(num_phy)` 而不是 `arange(num_log)`——尾部 `num_phy - num_log` 列的初始值（`num_log..num_phy-1`）**并不是合法的逻辑专家编号**！这是安全的：循环变量恰好遍历 `i = num_log..num_phy-1`，每个尾部列都会被 [eplb.py:68](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L68) 恰好覆写一次，非法占位值不会泄漏到输出。`repeat(n, 1)` 把同一行铺 `n` 份，各行从同一起点出发，之后按各自负载独立演化。`device = weight.device` 则保证新张量跟随输入设备（入口已把 `weight` 搬到 CPU，所以实践中都在 CPU 上）。

三个输出的下游去向，串起了整个放置方案：

```python
    tokens_per_phy = (tokens_per_mlog / mlogcnt).gather(-1, phy2mlog)
```

[eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)（Step 3）把本函数的两个输出组合成每个**副本**的负载：`tokens_per_mlog / mlogcnt` 算出各逻辑专家的单副本水位，再 `gather(-1, phy2mlog)` 按物理槽位顺序取出——这正是 4.2 节公式 \( w_j/c_j \) 的落地，随后交给 `balanced_packing` 打包到 GPU。

```python
    pphyrank = phyrank.gather(-1, pphy2phy).view(num_layers, -1)
    logcnt = mlogcnt.view(num_layers, -1).gather(-1, log2mlog)
```

[eplb.py:127-128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L127-L128) 把 `rank` 与 `logcnt` 从"节点内编号"重排回原始逻辑编号顺序，作为层级策略的返回值上抛。

```python
    log2phy.view(num_layers, -1).scatter_(-1, phy2log * maxlogcnt + phyrank,
            torch.arange(num_replicas, ...).expand(num_layers, -1))
```

[eplb.py:160-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L160-L161) 是 `rank` 的高光时刻：入口把 `(逻辑编号, 副本序号)` 编码为单一地址 `phy2log * maxlogcnt + phyrank`，把每个物理槽位的编号散射进 `log2phy`。因为 `rank` 在每个逻辑专家内是从 0 连续无重复的，地址不会冲突，未被填满的槽位保持 `-1`（u1-l3 观察到的 padding）。细节留到 u2-l6 精读。

最后看调用点如何"批量"使用本函数：

```python
    tokens_per_mlog = weight.gather(-1, mlog2log).view(-1, num_logical_experts // num_nodes)
    phy2mlog, phyrank, mlogcnt = replicate_experts(tokens_per_mlog, num_physical_experts // num_nodes)
```

[eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113) 把 `[层数, 逻辑专家数]` 的权重重排后 view 成 `[层数×节点数, 逻辑专家数//节点数]`，于是**所有层的所有节点在同一次调用里各自独立地完成冗余分配**。全局策略（入口 L156 以退化参数 `(1, 1, num_gpus)` 复用层级实现）同样最终走到这里，只是"节点"退化为 1、行数恰为层数。

#### 4.3.4 代码实践

1. **实践目标**：验证"各行独立"与"高级索引的 gather 等价写法"两件事。
2. **操作步骤**（示例代码）：

   ```python
   import torch
   from eplb import replicate_experts

   torch.manual_seed(0)
   w = torch.rand(3, 6) * 100
   phy2log, rank, logcnt = replicate_experts(w, num_phy=9)

   # (a) 行独立性：第 k 行的结果应与单独跑第 k 行完全一致
   for k in range(3):
       p, r, c = replicate_experts(w[k:k+1], num_phy=9)
       assert torch.equal(phy2log[k:k+1], p)
       assert torch.equal(rank[k:k+1], r)
       assert torch.equal(logcnt[k:k+1], c)

   # (b) rank 的无冲突性：把 (逻辑编号, rank) 编码成唯一地址，检查无重复
   addr = phy2log * (int(logcnt.max()) + 1) + rank
   for k in range(3):
       assert len(set(addr[k].tolist())) == 9, "地址出现冲突"

   # (c) 高级索引的 gather 等价写法
   arangen = torch.arange(3)
   idx = (w / logcnt).max(dim=-1).indices
   a = logcnt[arangen, idx]
   b = logcnt.gather(-1, idx.unsqueeze(-1)).squeeze(-1)
   assert torch.equal(a, b)
   ```

3. **需要观察的现象**：三条断言是否全部通过；(b) 中地址是否恰好 9 个互不相同。
4. **预期结果**：(a) 与 (c) 是张量语义的直接推论，应当成立；(b) 依赖"每行内 rank 对同一逻辑专家从 0 连续编号"，由 4.3.1 的分析应当成立（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：把 [eplb.py:69](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L69) 的 `rank[:, i] = logcnt[arangen, redundant_indices]` 和第 70 行的自增交换顺序，会出什么问题？

**答案**：`rank` 会比正确值大 1（取到自增后的新值），同一逻辑专家的副本编号变成 2, 3, ... 起步且出现空洞。下游入口的 `phy2log * maxlogcnt + phyrank` 地址会随之整体错位，极端情况下越过 `maxlogcnt` 的边界与下一个逻辑专家的地址重叠，`log2phy` 被写坏。两行的顺序是"先读旧值、再自增"。

**练习 2**：`phy2log` 初始化为什么不能改成 `torch.arange(num_log).repeat(n, 1)` 后再 `torch.cat` 补零？

**答案**：功能上可以构造出等价的初始状态（前 `num_log` 列恒等、尾部填任意占位值），但更啰嗦。现有写法 `arange(num_phy)` 一次性给出正确宽度，尾部占位值虽非法却保证被循环逐列覆写（见 4.3.3 的分析），是更紧凑的惯用法。核心不变量是：**尾部每一列恰好被循环覆写一次**，占位初值是什么无所谓。

**练习 3**：本函数的时间复杂度是多少？瓶颈在哪？

**答案**：Python 层循环 `num_phy - num_log` 次（层级策略下即每节点冗余专家数），每次做全行向量化的除法 + argmax + 高级索引，单轮成本 \( O(n \cdot \text{num\_log}) \)。总复杂度 \( O((\text{num\_phy} - \text{num\_log}) \cdot n \cdot \text{num\_log}) \)。注意循环长度**与层数 n 无关**——把层数压进张量第一维是这条流水线能扩展到几十层的关键设计。向量化改进的空间（比如批量 argmax）在 u3-l3 讨论。

## 5. 综合实践

**任务**：实现"每轮直接复制当前最重专家"的朴素对照版本，在同一组负载上对比两个版本的最大单副本负载，验证 `weight / logcnt` 贪心的优势，并尝试寻找反例。

1. **实践目标**：量化"分母 `logcnt` 让选择与时俱进"带来的收益；体会启发式对比实验的基本方法（控制变量 + 多分布 + 记录反例）。

2. **操作步骤**：

   先写朴素对照版（示例代码，与原版的唯一区别是不除以 `logcnt`）：

   ```python
   # compare_greedy_naive.py（示例代码）
   import torch
   from eplb import replicate_experts   # 原版贪心

   def replicate_naive(weight: torch.Tensor, num_phy: int):
       n, num_log = weight.shape
       phy2log = torch.arange(num_phy, dtype=torch.int64).repeat(n, 1)
       rank = torch.zeros(n, num_phy, dtype=torch.int64)
       logcnt = torch.ones(n, num_log, dtype=torch.int64)
       arangen = torch.arange(n)
       for i in range(num_log, num_phy):
           idx = weight.max(dim=-1).indices          # 唯一区别：argmax(w) 而非 argmax(w/c)
           phy2log[:, i] = idx
           rank[:, i] = logcnt[arangen, idx]
           logcnt[arangen, idx] += 1
       return phy2log, rank, logcnt

   def max_replica_load(weight, logcnt):
       return (weight / logcnt).max(dim=-1).values    # 每行最大单副本负载

   cases = {
       "教科书反例":  torch.tensor([[10., 9., 5., 2.]]),
       "均匀负载":    torch.ones(1, 8),
       "长尾负载":    torch.tensor([[100., 50., 25., 12., 6., 3., 2., 1.]]),
   }
   for name, w in cases.items():
       num_phy = w.shape[1] + 4                        # 每行 4 个冗余名额
       g = replicate_experts(w, num_phy)
       nv = replicate_naive(w, num_phy)
       print(f"{name}: greedy max={max_replica_load(w, g[2]).item():.2f} "
             f"naive max={max_replica_load(w, nv[2]).item():.2f} "
             f"greedy logcnt={g[2].tolist()} naive logcnt={nv[2].tolist()}")
   ```

   再加一组随机压力测试（示例代码）：

   ```python
   torch.manual_seed(0)
   better = equal = worse = 0
   for _ in range(200):
       w = (torch.rand(4, 32) ** 4) * 100              # 幂次制造长尾
       g = replicate_experts(w, num_phy=40)[2]
       nv = replicate_naive(w, num_phy=40)[2]
       for k in range(4):                              # 逐行统计
           d = (max_replica_load(w, nv)[k] - max_replica_load(w, g)[k]).item()
           better, equal, worse = better + (d > 1e-6), equal + (abs(d) <= 1e-6), worse + (d < -1e-6)
   print(f"greedy 更优: {better}, 持平: {equal}, greedy 更差: {worse}")
   ```

3. **需要观察的现象**：
   - "教科书反例"一行：贪心的 `logcnt` 应为 `[[2, 2, 1, 1]]`（最大水位 5），朴素版应把两个名额都给专家 0，`logcnt = [[3, 1, 1, 1]]`（最大水位 9）——手算即可推出，代码应复现。
   - "均匀负载"一行：两者应完全打平（任何分配的最大水位都相同）。
   - "长尾负载"与随机压力测试：贪心的最大水位应小于等于朴素版，且朴素版的 `logcnt` 高度集中在最初最重的那个专家上。
   - 随机测试中是否存在 `greedy 更差 > 0` 的行——这是在找反例。

4. **预期结果**：长尾分布下贪心优势明显；均匀分布下无差异（这是贪心无收益的边界情形）；`greedy 更差` 的行数预期为 0，但若你构造出反例请记录下来——它说明"argmax(w/c) 每步局部最优"不必然导出"全局最优"，这本身就是宝贵的学习材料（随机部分待本地验证）。

## 6. 本讲小结

- `replicate_experts` 回答"复制谁"：在 `num_phy - num_log` 个冗余名额中，每轮复制 `weight / logcnt` 最大的逻辑专家，与回答"放在哪"的 `balanced_packing` 互补。
- \( \ell_j = w_j / c_j \) 是"副本间流量均分"假设下的单副本期望负载；贪心等价于反复给水位最高的量杯加宽一格，逼近下界 \( \sum_j w_j / \text{num\_phy} \)，但它是启发式，且依赖均分假设。
- 循环 `for i in range(num_log, num_phy)` 恰好执行 `num_phy - num_log` 次，每次覆写一个尾部物理槽位；初始化 `arange(num_phy)` 的非法尾部占位值因此不会泄漏。
- `phy2log` 是正向表（装箱对象）、`rank` 是副本身份证（保证 `(逻辑编号, rank)` 地址无冲突，入口构造 `log2phy` 全靠它）、`logcnt` 是副本计数（水位分母、最终输出）。
- 全部行（层数×节点数）在一次调用中按行独立并行处理，Python 循环长度只等于每节点冗余数，与层数无关。

## 7. 下一步学习建议

本讲结束后，你已经掌握层级策略三步中的"复制"环节，但 Step 2 与 Step 3 之间的张量搬运还没有拆开看。建议：

1. 先补齐工具箱：学习 u2-l3（gather、scatter 与逆置换），特别是 `inverse` 函数和 `gather` 按索引重排的语义——Step 3 的映射链全靠它们。
2. 然后进入 u2-l4，看 [eplb.py:112-113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112-L113) 如何把 `weight` 重排、压平后喂给本函数，以及 `tokens_per_mlog / mlogcnt` 如何沿用本讲的 \( w_j / c_j \) 公式。
3. 阅读 [eplb.py:117-128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117-L128) 时，随时回来对照本讲的"三个输出的角色表"——那一大段 gather 链只是在这三种读法之间换坐标系。
