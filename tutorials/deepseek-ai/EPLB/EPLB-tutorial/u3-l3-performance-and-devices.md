# 性能、数值与设备一致性：结合两次真实修复演进

## 1. 本讲目标

前两讲（u3-l1、u3-l2）解决了「方案对不对」和「方案好不好」，本讲换成工程师的第三问：**「跑得快不快、稳不稳」**。学完后你应该能：

1. 对数百专家 × 数十层的真实规模（如 60 层 × 256 专家）做复杂度剖析，说出 EPLB 的耗时瓶颈在 `balanced_packing` 的 Python 双层循环而非任何张量操作，并会用 `timeit` / `cProfile` 实测验证。
2. 亲手把 `replicate_experts` 的循环改造成「水位维护」版向量化实现，理解哪些循环是**语义串行**（改不动）、哪些开销只是**实现冗余**（可以省），并用 u3-l1 的不变量测试验证等价性。
3. 解释入口处 [eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 那行 `weight.float().cpu()` 在数值稳定与设备一致性上的三重动机。
4. 做一次完整的**设备一致性审计**：对照 commit d52c72d（修复 `pphy2mlog` 的设备缺失）盘点当前代码中每一处张量构造的 device 来源，并推演出「绕过入口直接用 GPU 张量调 `rebalance_experts_hierarchical`」在哪些参数组合下能跑通、哪些组合会崩。
5. 从 git 历史读懂 commit e1100fe：全局策略为什么从「只复制不装箱」改为以退化参数 `(num_replicas, 1, 1, num_gpus)` 复用层级实现，这一改动在性能与均衡质量上各付出了什么、换来了什么。

一句话：本讲把 u2 精读过的三步算法重新放回工程现场——尺度、精度、设备、演化——四个维度各审一遍。

## 2. 前置知识

- **两处 Python 循环**（承接 u2-l1、u2-l2）：`balanced_packing` 的双层 for（外层遍历层、内层逐物品贪心装箱）与 `replicate_experts` 的冗余轮循环（每轮 argmax 一次、索引写两次）。本讲的性能分析全部围绕这两处展开。
- **张量级向量化**（承接 u2-l4）：`replicate_experts` 内部 `phy2log[:, i] = ...` 这类写法本身是张量级操作，一次处理所有「层 × 节点」行——层维度已被作者向量化，剩下的轮数维度是否还能压，是 4.2 的主题。
- **副本均分负载模型**（承接 u2-l2、u3-l2）：单副本负载 \( \hat w = w / c \)。这个除法同时是性能分析的热点（循环内反复计算）和数值分析的焦点（整数截断风险）。
- **IB 指标与 `gpu_loads`**（承接 u3-l2）：不均衡度 IB = max/mean，以及用 `view + sum` 复算每 GPU 负载的函数。本讲 4.5 的演化对比实验直接复用它们。
- **不变量测试套件**（承接 u3-l1）：`test_eplb.py` 的 INV-1~7 断言。任何性能优化后的第一件事就是跑它——**先保正确，再谈快**。
- **退化复用**（承接 u2-l6）：全局策略以 `(num_replicas, 1, 1, num_gpus)` 调用层级函数，三步自动退化为「恒等置换 → 全局复制 → GPU 级装箱」。本讲 4.5 从 git 历史还原这次重构的动机。
- **PyTorch 设备规则**：二元运算要求两个操作数在同一设备，CUDA 与 CPU 混算会直接 `RuntimeError`；`torch.arange` / `torch.full` / `torch.zeros` 若不显式传 `device=`，一律创建在 CPU 上——**这是设备 bug 的头号来源**，也是 d52c72d 的病灶。
- **时间测量**：`timeit.timeit`（多次取总时）适合微基准；`cProfile` 适合定位热点函数。本讲的实践会用到两者。

## 3. 本讲源码地图

| 文件 / commit | 本讲关注点 |
| --- | --- |
| [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) | 唯一算法文件。本讲重点引用：L27-L41（装箱循环）、L61-L71（复制循环）、L149（`weight.float().cpu()`）、L150-L156（策略分派）、L104-L128（层级三步中的全部张量构造与 device 参数） |
| [README.md](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/README.md) | L37-L57 两层 12 专家金标准示例，是本讲所有基准脚本的起点输入 |
| commit [e1100fe](https://github.com/deepseek-ai/EPLB/commit/e1100fe) | 全局策略从 `replicate_experts(weight, num_replicas)` 改为 `rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)`，1 增 1 删 |
| commit [d52c72d](https://github.com/deepseek-ai/EPLB/commit/d52c72d) | 给 Step 3 的 `torch.arange` 补上 `device=group_pack_index.device`，2 增 1 删 |

两个 commit 都只改了几行——本讲的立场恰恰是：**小 diff 背后有完整的因果链**，读懂它比读懂大段代码更能训练工程判断力。

## 4. 核心概念与源码讲解

### 4.1 性能解剖：EPLB 的耗时都花在哪

#### 4.1.1 概念说明

EPLB 的算法部分几乎全是张量操作（排序、gather、scatter、除法），这些在 C++/CUDA 内核里执行，对 60 层 × 256 专家这种规模来说是微秒级的小事。真正的耗时在两处**显式 Python 循环**：

1. `balanced_packing` 的双层循环——每一步都要经过 Python 解释器，做一次 `min` 线性扫描、两次对张量的**单元素写**；
2. `replicate_experts` 的冗余轮循环——每轮启动若干个小内核并分配一个 `[n, num_log]` 的临时浮点张量。

「Python 循环慢」的直觉要量化成公式才能指导优化。下面把两条循环的迭代次数写成层数 \(L\)、逻辑专家数 \(E\)、组数 \(G\)、节点数 \(N\)、GPU 数 \(P\)、物理专家数 \(M\) 的函数。

#### 4.1.2 核心流程

层级策略对两个贪心函数各有一次调用，把两次调用的循环规模分开算（记 \(R\) 为冗余轮数）：

| 循环 | 位置 | 外层迭代 | 内层迭代 | 每个内层步的开销 |
| --- | --- | --- | --- | --- |
| 装箱调用 1（组→节点） | L30-L40 | \(L\) | \(G\) | `min` 扫描 \(N\) 个包 + 2 次张量单元素写 |
| 装箱调用 2（物理→GPU） | L30-L40 | \(L \cdot N\)（view 后的行数） | \(M/N\) | `min` 扫描 \(P/N\) 个包 + 2 次张量单元素写 |
| 复制循环（层级下） | L66-L70 | \(R = M/N - E/N\) | ——（每轮全行向量化） | 整表除法 \(O(L \cdot E)\) + argmax + 2 次列写 |

三条循环的 Python 级总步数：

\[
T_{\text{pack1}} \sim L \cdot G \cdot N, \qquad
T_{\text{pack2}} \sim (L \cdot N) \cdot \frac{M}{N} \cdot \frac{P}{N} = L \cdot M \cdot \frac{P}{N}, \qquad
T_{\text{repl}} \sim \frac{M - E}{N} \cdot L \cdot E \;(\text{向量化})
\]

代入一个 DeepSeek 量级的假想配置（\(L{=}60,\ E{=}256,\ G{=}8,\ N{=}4,\ P{=}32,\ M{=}288\)，满足全部整除断言）：

- 装箱调用 1：\(60 \times 8 = 480\) 个内层步，每步扫 4 个包 → 约 2 千次基本操作；
- 装箱调用 2：\(60 \times 4\) 行 × 每行 72 项 = 17 280 个内层步，每步扫 8 个包 → **约 14 万次基本操作**；
- 复制循环：每节点冗余 \(72 - 64 = 8\) 轮，每轮一次 15 360 元素的向量化除法 + argmax → 8 轮内核调用。

结论：**装箱调用 2 是主导热点**（步数比调用 1 高两个数量级），且它逐物品做张量单元素读写——这正是 Python 解释器最不擅长的工作负载。复制循环的轮数少、每轮已向量化，通常不是瓶颈。

#### 4.1.3 源码精读

**热点一：装箱的双层循环。** [eplb.py:30-L40](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L30-L40)：

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

三个慢的细节：

1. `for group in indices[i]` 迭代张量，每次产出**一个 0 维张量对象**（而非 Python int），对象构造本身有开销；
2. `pack_index[i, group] = pack` 与 `weight[i, group]` 是对张量的单元素读/写，每次都走一遍 `__getitem__` / `__setitem__` 的完整派发路径；
3. `min(..., key=...)` 里的生成器每次重建、每步线性扫描——这部分是算法本身（选当前最轻的未满包，u2-l1 讲过的 LPT 式贪心），语义上无法省。

**热点二：复制循环。** [eplb.py:66-L70](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L66-L70)：

```python
for i in range(num_log, num_phy):
    redundant_indices = (weight / logcnt).max(dim=-1).indices
    phy2log[:, i] = redundant_indices
    rank[:, i] = logcnt[arangen, redundant_indices]
    logcnt[arangen, redundant_indices] += 1
```

注意 `weight / logcnt` 在**每一轮**都对整个 `[n, num_log]` 表做一次除法并分配一个新浮点张量——但循环实际只改变了被选中那一列的 `logcnt`，其余列的商根本没变。这是典型的**可消除的重复计算**，4.2 会动手消除它。

**对照：真正快的部分。** [eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 的排序 `weight.float().sort(-1, descending=True)` 是整个算法里最大的单次张量操作，复杂度 \(O(L \cdot E \log E)\)，但在 C++ 内核中完成——这行末尾的 `.indices.cpu()` 把索引搬回 CPU，正是「批量部分留在原设备、逐元素部分搬到 CPU」的布局策略，4.3 与 4.4 都会回到这个主题。

#### 4.1.4 代码实践

实践目标：用 `cProfile` 在真实规模下验证「装箱调用 2 是热点」的推断，并测出耗时随层数的伸缩性。

操作步骤（示例代码，非项目原有）：

```python
# bench_profile.py
import cProfile, pstats, torch, eplb

L, E, G, N, P, M = 60, 256, 8, 4, 32, 288     # 满足全部整除断言
torch.manual_seed(0)
weight = torch.rand(L, E) * 1000               # 模拟 token 计数统计

cProfile.run("eplb.rebalance_experts(weight, M, G, N, P)", "eplb.prof")
pstats.Stats("eplb.prof").sort_stats("cumtime").print_stats(12)
```

再用 `timeit` 测层数伸缩性（示例代码）：

```python
import timeit, torch, eplb

for L in (1, 10, 60):
    w = torch.rand(L, 256) * 1000
    t = timeit.timeit(lambda: eplb.rebalance_experts(w.clone(), 288, 8, 4, 32), number=5) / 5
    print(f"L={L:3d}  {t*1000:8.2f} ms")
```

需要观察的现象与预期结果：

1. cProfile 输出里 `balanced_packing` 的 `tottime` 应显著大于 `replicate_experts`，且调用栈中出现两次（对应两次装箱调用）；
2. 耗时应近似随 \(L\) 线性增长（三条循环的步数都正比于 \(L\)）；
3. 把 `G` 从 8 改成 4（仍满足 `8 % 4 == 0` 与 `256 % 4 == 0`）观察变化——装箱调用 1 的步数不变、每步扫描数减半，总耗时应变化不大，佐证调用 1 不是主导。

具体毫秒数依赖机器与 torch 版本，**待本地验证**；本实践要验证的是**相对结论**（谁热点、如何伸缩），不是绝对数值。

#### 4.1.5 小练习与答案

**练习 1**：保持 \(L{=}60, E{=}256, M{=}288\) 不变，把 GPU 数 \(P\) 从 32 加倍到 64（\(N\) 仍为 4），装箱调用 2 的步数怎么变？

**答案**：\(T_{\text{pack2}} \sim L \cdot M \cdot P/N\)，\(P\) 翻倍使步数翻倍（120 → 240 步/行的扫描宽度从 8 变 16，同时每行物品数从 \(M/N = 72\) 不变、包数变 16）；同时 `phy_experts_per_gpu` 从 9 变 4.5——但 \(288 \% 64 \ne 0\)，断言 [eplb.py:95](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L95) 会先失败。要让参数合法需同时调 \(M\)（如 320），此时步数 \(\sim 60 \times 320 \times 16 = 30.7\) 万，比原来翻倍还多。这说明**参数矩阵必须先过整除断言再谈性能**。

**练习 2**：只做一件事就能大幅降低装箱循环的常数开销——把张量读写换成 Python 列表操作。怎么做？

**答案**：循环前一次性 `indices_list = indices.tolist()`、`w = weight.tolist()`，循环内全部用 Python int/list 计算，最后把 `pack_index` / `rank_in_pack` 用 `torch.tensor(...)` 一次构造。这消除了练习 4.1.3 指出的「0 维张量对象 + 单元素读写派发」两项开销，通常有一个数量级左右的常数收益（具体倍数待本地验证）。算法语义完全不变——这是纯实现层优化，与 4.2 的水位维护同属一类。

### 4.2 向量化实战：replicate_experts 的水位维护改造

#### 4.2.1 概念说明

优化循环前先分类：这个循环是**语义串行**还是**实现冗余**？

- 语义串行：第 \(i\) 轮选择谁，依赖第 \(i-1\) 轮更新后的 `logcnt`（复制会摊薄水位，下一轮的最优选择随之改变）。这是贪心的内在依赖，**不改变算法语义就无法消除**——想「一次 topk 选出全部冗余」会得到另一个算法（练习 1 会正面撞上这一点）。
- 实现冗余：`weight / logcnt` 每轮对**全表**重算，但循环只改了一列。商表（下称**水位表** \(\text{score} = w/c\)）只在被选中列处过期，增量维护即可。

于是优化方案是：初始化一次水位表，每轮 argmax 后只更新被选中列。每轮成本从「整表除法 + 整表临时张量分配 + argmax」降到「argmax + 单列更新」。

#### 4.2.2 核心流程

原版与改造版的每轮开销对比：

\[
\text{原版每轮：} \underbrace{O(nE)\,\text{除法}}_{\text{weight/logcnt}} + \underbrace{O(nE)\,\text{临时分配}}_{\text{新浮点张量}} + O(nE)\,\text{argmax}
\quad\Longrightarrow\quad
\text{改造版每轮：} O(nE)\,\text{argmax} + O(n)\,\text{单列更新}
\]

渐进复杂度不变（argmax 仍是每轮 \(O(nE)\)，且它源于贪心语义），但省掉了每轮的除法内核与整表临时分配。当轮数 \(R\) 较大（全局策略下 \(R = M - E\)，不分节点摊薄）时收益最明显。

#### 4.2.3 源码精读

改造对象是 [eplb.py:61-L71](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L61-L71)。原版循环体第一行 `(weight / logcnt).max(dim=-1).indices` 就是冗余所在：除法对全表重算，而上一轮只递增了一列的 `logcnt`。

改造版（示例代码，非项目原有）：

```python
def replicate_experts_fast(weight: torch.Tensor, num_phy: int):
    n, num_log = weight.shape
    num_redundant = num_phy - num_log
    assert num_redundant >= 0
    device = weight.device
    phy2log = torch.arange(num_phy, dtype=torch.int64, device=device).repeat(n, 1)
    rank = torch.zeros(n, num_phy, dtype=torch.int64, device=device)
    logcnt = torch.ones(n, num_log, dtype=torch.int64, device=device)
    arangen = torch.arange(n, dtype=torch.int64, device=device)

    score = weight.float() / logcnt            # 水位表：只在初始化时全表除法一次
    for i in range(num_log, num_phy):
        redundant_indices = score.max(dim=-1).indices      # 归约算子与原版一致
        phy2log[:, i] = redundant_indices
        rank[:, i] = logcnt[arangen, redundant_indices]
        logcnt[arangen, redundant_indices] += 1
        # 只刷新被选中列的水位，其余列的商没有变
        score[arangen, redundant_indices] = (
            weight[arangen, redundant_indices].float() / logcnt[arangen, redundant_indices])
    return phy2log, rank, logcnt
```

三个等价性要点：

1. **数值位级一致**：原版每轮重算的 \(w/c\) 与改造版增量更新的 \(w/c\) 是同一对操作数、同一个除法运算，浮点结果逐位相同（前提是 `weight` 先 `.float()` 统一类型，与入口 [eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 的规范化一致）；
2. **归约算子一致**：两版都用 `max(dim=-1).indices` 选列，并列打破规则相同，因此连并列场景的输出也应一致；
3. **循环轮数不变**：仍是 `num_phy - num_log` 轮，每轮的三个张量写一字不差。

#### 4.2.4 代码实践

实践目标：实现 `replicate_experts_fast`，验证与原版**严格等价**，再测加速比。

操作步骤：

1. 把 4.2.3 的函数存为 `eplb_fast.py`；
2. 等价性验证分两层（示例代码）：

```python
import torch, eplb
from eplb_fast import replicate_experts_fast

torch.manual_seed(0)
for trial in range(20):
    n, num_log, num_phy = 64, 256, 288          # 层×节点行数、每节点逻辑/物理专家数
    w = torch.rand(n, num_log) * 1000
    a = eplb.replicate_experts(w, num_phy)
    b = replicate_experts_fast(w, num_phy)
    assert all(torch.equal(x, y) for x, y in zip(a, b))   # 逐元素严格相等
print("20 组随机输入全部逐元素一致")
```

3. 把 `replicate_experts_fast` 接入完整的 `rebalance_experts` 流水线（复制一份 `rebalance_experts_hierarchical` 为实验版，仅替换 [eplb.py:113](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L113) 的调用），跑 u3-l1 的 `test_eplb.py` 全部不变量断言；
4. 用 4.1.4 的 `timeit` 方法对比两版在 \(L{=}60, E{=}256\) 输入上的耗时（注意让两版都跑全局策略分支——`num_groups=3` 触发——此时 \(R = M-E\) 最大，收益最可见）。

需要观察的现象与预期结果：逐元素相等断言应全过；不变量断言应全过；全局策略分支下改造版在 `replicate_experts` 上的耗时应有可测的下降（下降幅度取决于 torch 版本与除法内核成本，**待本地验证**）。若第 2 步出现不一致，优先检查是否忘了先 `weight.float()`——int64 输入下两版的类型提升路径可能不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能用 `torch.topk(weight / logcnt, num_phy - num_log)` 一次性选出所有冗余专家？

**答案**：`logcnt` 是**初始**副本数，topk 忽略了「每复制一次、水位减半」的反馈：某专家被选中一次后，它的 \(w/c\) 应更新为 \(w/(c{+}1)\)，可能仍高于其他专家、值得再复制；反之初始水位高但复制一次后就不再最优的专家，topk 会重复占用名额。贪心的每步依赖前步（4.2.1 的语义串行），批量 topk 是另一个算法，输出不再等价——它甚至可能把全部冗余名额给同一个专家。

**练习 2**：改造版的加速在什么参数下最不明显？为什么？

**答案**：层级策略、且每节点冗余数 \(M/N - E/N\) 很小时（如 README 示例只有 2 轮）。收益正比于省掉的「整表除法次数 = 轮数」，轮数少则省得少；此时瓶颈回到 `balanced_packing` 的 Python 循环（4.1 的结论），优化它（练习 4.1.5-2）才是正道。这也演示了性能工作的通用节奏：**先 profile 定位，再对症下药**。

**练习 3**：`score` 为什么用 `weight.float()` 初始化，而不是直接 `weight / logcnt`？

**答案**：与原版保持同一类型提升路径。原版里被除数 `weight` 在入口处已是 float32，除法结果位级确定；若调用方绕过入口直接传 int64 或 bfloat16 统计，`weight / logcnt` 的提升结果因 dtype 组合而异（详见 4.3），先 `.float()` 把行为钉死在 float32，等价性断言才可靠。

### 4.3 数值与类型：weight.float().cpu() 的三重动机

#### 4.3.1 概念说明

入口函数 [eplb.py:149](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L149) 只有短短一行：

```python
weight = weight.float().cpu()
```

这行没有注释，但它是全仓库最重要的一行「防御性代码」，同时解决三个问题：

1. **类型规范化（数值稳定）**：调用方传来的统计可能是 int64（token 计数）、float16 或 bfloat16（框架侧省内存的统计表）。下游的核心运算 `weight / logcnt`（[eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)、[eplb.py:117](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L117)）对整数输入的行为依赖 torch 版本的除法语义，对低精度浮点输入则有明显的精度损失（bfloat16 尾数仅 8 位，水位比较会大量并列、排序失真）。统一转到 float32（24 位尾数，计数在 \(2^{24} \approx 1670\) 万以内精确表示），把所有下游行为钉死在同一条数值路径上。
2. **设备规范化（搬到 CPU）**：两个贪心函数的核心是逐元素 Python 循环（4.1），这类负载在 CPU 上反而快——若张量在 GPU 上，`pack_index[i, group] = pack` 这种单元素写每步都是一次内核启动加潜在的设备同步，比 CPU 慢几个数量级。入口**一次性**把数据搬到 CPU，胜过让内部几十处操作各自隐式同步。
3. **设备一致性防线**：`.cpu()` 之后，4.4 将要盘点的所有设备陷阱在公开 API 路径上**全部不可达**——这是 d52c72d 只修一处 arange、而公开接口从未出过设备 bug 的根本原因。

值得注意的对照：[eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 在 `balanced_packing` 内部又写了一次 `weight.float()`——因为该函数可能被绕过入口直接调用，这是同一防御在函数级的重复，幂等而无害（dtype 已是 float32 时 `.float()` 近似零开销）。

#### 4.3.2 核心流程

「批量在设备、逐元素在 CPU」的分工可以用执行流描述：

1. 入口：`weight.float().cpu()` —— 一次类型转换 + 一次设备搬运，此后全流水线在 CPU float32 上运行；
2. `balanced_packing` 内部：`weight.float().sort(-1, descending=True)` 批量排序在张量库内核完成，`.indices.cpu()` 把逐元素循环需要的索引搬回 CPU（若输入本就在 CPU 则为空操作）；
3. 全部 Python 循环、单元素读写都在 CPU 张量上进行；
4. 出口：结果张量天然在 CPU，调用方需要 GPU 时自行 `.to(device)`——一次搬运，边界清晰。

#### 4.3.3 源码精读

**除法发生的位置决定了 float() 的价值。** 复制循环的水位 [eplb.py:67](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L67)：

```python
redundant_indices = (weight / logcnt).max(dim=-1).indices
```

`logcnt` 始终是 int64（[eplb.py:64](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L64) 用 `torch.ones(..., dtype=torch.int64)` 构造）。整数张量除法在 torch 历史上经历过语义变更（早期版本的 `torch.div` 对整数输入做整除，后续版本改为真除法并提升为浮点），若 `weight` 也是整数且行为落在整除上，\(w/c\) 会被截断成 0 或 1，水位比较彻底失真。`.float()` 让被除数先行成为浮点，除法语义与版本无关地确定为真除。（若你使用的 torch 版本对整数除法有不同处理，以本地文档为准——这正是防御性写法存在的原因。）

**装箱权重的同一处理。** [eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 对排序键同样先 `.float()`；而 [eplb.py:39](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L39) 的 `pack_weights[pack] += weight[i, group]` 是 Python 标量累加，float32 张量读出的元素天然是 Python float，无截断风险。

**float32 的边界。** 24 位尾数意味着 token 计数超过 \(2^{24}\) 后出现相对误差约 \(10^{-7}\) 的舍入。对排序与水位比较这类用途可忽略；但若你的统计是长期累积的原始计数且专家间负载差异极小，可以在入口改为 `.double()`（示例变体），代价是内存与带宽翻倍——数值精度与性能在此互换。

#### 4.3.4 代码实践

实践目标：直观看到「不转 float 会发生什么」，理解防御的必要性。

操作步骤（示例代码，非项目原有；以现代 torch 的真除法行为为前提，**具体表现待本地验证**）：

```python
import torch

w_int = torch.tensor([[90, 132, 40, 61, 104, 165, 39, 4, 73, 56, 183, 86]])
c = torch.ones(1, 12, dtype=torch.int64)
print(w_int / c)          # 观察整数输入下的除法结果类型与数值
print(w_int.float() / c)  # 对照：入口规范化后的路径

w_bf16 = w_int.bfloat16()
print((w_bf16 / c).dtype) # 低精度输入直接进算法时的水位精度
```

需要观察的现象与预期结果：第一行在现代 torch 下应提升为 float32 并得到真商；若你的环境输出整数（整除行为），就演示了 `.float()` 防御的旧世界；第三行显示 bfloat16 统计进入水位比较时的精度上限。把三种输入分别喂给 `eplb.rebalance_experts`，对比输出 `logcnt` 是否出现差异（int 与 float32 路径应一致，bfloat16 路径可能因并列而不同）。

#### 4.3.5 小练习与答案

**练习 1**：`.float()` 和 `.cpu()` 的链式调用顺序重要吗？

**答案**：对最终结果不重要——两次转换可交换，最终都得到 CPU float32。对性能略有差别：若输入在 GPU 上，`.float()` 先在 GPU 完成类型转换（内核并行）、再搬 CPU，比先把 float64/int64 大张量搬回 CPU 再转换通常更快；但这属于微优化，写成一行的可读性优先。

**练习 2**：`.cpu()` 之后，4.4 里讨论的「CUDA 张量直接调用层级函数」的崩溃路径为什么不会发生在公开 API 上？

**答案**：`.cpu()` 让 `weight` 及其**全部派生物**（gather、除法、装箱输出的张量）都在 CPU 上生成——设备沿数据流传播，源头在 CPU 则全链在 CPU。后面所有 `device=` 参数（无论指向何处）与 CPU 数据相遇时都恰好处处一致。这就是「在入口收口设备」的价值：一处规范化，胜过在内部每处运算前检查。

### 4.4 设备一致性审计：d52c72d 修了什么、还剩什么

#### 4.4.1 概念说明

commit [d52c72d](https://github.com/deepseek-ai/EPLB/commit/d52c72d)（2025-03-24，标题 *Fix missing device for pphy2mlog tensor*）的全部改动是给 Step 3 的一个 `torch.arange` 补上 `device=` 参数。要看懂这个两行 diff，需要先建立**设备审计**的方法论：

1. **找无主构造**：扫出所有未显式传 `device=` 的张量构造（`arange` / `full` / `zeros`）——它们默认落在 CPU，与谁相加谁遭殃；
2. **查运算双边**：每个二元运算的两个操作数设备是否同源；
3. **查分支一致性**：同一函数的不同分支是否返回同一设备（最容易埋雷）；
4. **锚定数据源**：修复时用 `某张量.device` 跟随数据所在设备，而不是硬编码 `'cuda'`。

#### 4.4.2 核心流程

先盘点当前 HEAD 上 `rebalance_experts_hierarchical` 数据流中每一处张量构造的设备来源：

| 位置 | 构造 / 运算 | 设备来源 | 状态 |
| --- | --- | --- | --- |
| [eplb.py:23-24](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L23-L24) | 装箱平凡分支输出 | `weight.device`（跟随输入） | 一致 ✓ |
| [eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) | 排序索引 | 原设备排序后 `.cpu()` | 刻意搬运 |
| [eplb.py:28-29](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28-L29) | 装箱循环分支输出 | **硬编码 `device='cpu'`** | 与平凡分支不一致 ✗ |
| [eplb.py:62-65](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L62-L65) | 复制循环全部张量 | `weight.device` | 一致 ✓ |
| [eplb.py:100](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L100) | `inverse` 内 arange | `perm.device` | 一致 ✓ |
| [eplb.py:107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L107) | Step 1 组内偏移 | `group_pack_index.device` | 一致 ✓ |
| [eplb.py:124](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L124) | Step 3 节点基地址 | `group_pack_index.device`（**d52c72d 补上**） | 修复 ✓ |
| [eplb.py:158-161](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L158-L161) | 入口 log2phy 及其 arange | `logcnt.device` / `log2phy.device` | 一致 ✓ |

再看 d52c72d 修复前后这一行的差别（修复前即 [eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125) 处去掉 `device=` 参数）：

```python
# 修复前：arange 落在默认 CPU，与 GPU 上的 pphy2mlog 相加直接 RuntimeError
pphy2mlog = (pphy2mlog.view(num_layers, num_nodes, -1) + 
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes).view(1, -1, 1)).flatten(-2)
# 修复后：跟随 group_pack_index 所在设备
             torch.arange(0, num_logical_experts, num_logical_experts // num_nodes,
                          device=group_pack_index.device).view(1, -1, 1)
```

#### 4.4.3 源码精读：修复是否彻底？做一次可达性推演

关键问题是：**绕过入口、直接用 CUDA 张量调用 `rebalance_experts_hierarchical`，现在能跑通吗？** 沿数据流推演（依据 PyTorch「跨设备二元运算报错、gather 要求 index 与 input 同设备」的公开语义；具体报错文案待本地验证）：

- **情形 A：\(G > N\)（Step 1 走装箱循环分支）**——[eplb.py:28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28) 的硬编码使 `group_pack_index` 落在 CPU，`log2mlog` / `mlog2log` 随之在 CPU；到 [eplb.py:112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112) `weight.gather(-1, mlog2log)` 时 CUDA 输入配 CPU 索引 → **崩溃**。d52c72d 没有触及这条路径。
- **情形 B：\(G = N\)（Step 1 平凡分支，输出跟随输入在 CUDA）且 \(M/P > 1\)（Step 3 走装箱循环分支）**——Step 2 全链 CUDA 正常，但 Step 3 的 `pack_index` 落在 CPU，`pphy2phy` 随之 CPU；到 [eplb.py:122](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L122) `phy2mlog.gather(-1, pphy2phy)` 时设备错配 → **崩溃**。d52c72d 同样没触及。
- **情形 C：\(G = N\) 且 \(M = P\)（两次装箱都命中平凡分支，例如全局退化的极端配置或「每 GPU 恰一个物理专家」）**——两条装箱输出都跟随输入在 CUDA，一路 gather/scatter 全部同设备，直到修复前的 [eplb.py:123-125](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L123-L125)：CUDA 的 `pphy2mlog` 加上无 `device=` 的 CPU `arange` → **崩溃。这正是 d52c72d 修复的场景**；修复后此情形可完整跑通。

推演结论：d52c72d 是**针对触发路径的定点修复**，而非全面的 GPU 适配——`balanced_packing` 循环分支硬编码 CPU（[eplb.py:28](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28)）造成的情形 A / B 崩溃在当前 HEAD 依然存在。这不是苛责：公开 API 被入口的 `.cpu()` 完整保护（4.3），下游受影响的只是「直接 import 内部函数并在 GPU 上调用」的使用方式。工程上真正的教训是两条：

1. **函数分支间设备契约不一致**（`balanced_packing` 平凡分支返回输入设备、循环分支返回 CPU）是最隐蔽的设备 bug 形态——同一个函数，参数组合不同，行为在「能跑」与「崩」之间切换；
2. **入口收口**（`.cpu()`）比逐处修补更可靠——d52c72d 三天前的 e1100fe 刚把全局分支改为直接调用层级函数，随后就有外部贡献者提交设备修复，时间线暗示内部函数确有被直接使用的场景（此为合理推断，仓库内无直接证据）。

最后看修复的手法：补的参数是 `device=group_pack_index.device` 而非硬编码 `'cuda'`——跟随锚张量，CPU 输入下依然正确。与 [eplb.py:107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L107) 的既有写法一致，这是仓库内的惯用法。

#### 4.4.4 代码实践

实践目标：在有 GPU 的环境上验证 4.4.3 的可达性推演；无 GPU 则改为纯代码审计。

操作步骤（示例代码，非项目原有）：

```python
import torch, eplb

w = torch.rand(60, 256, device="cuda") * 1000

# 情形 C（修复目标场景）：G == N 且 M == P，两次装箱均走平凡分支
try:
    out = eplb.rebalance_experts_hierarchical(w, 256, 8, 8, 256)
    print("情形 C 跑通，输出设备:", out[0].device)
except RuntimeError as e:
    print("情形 C 崩溃:", e)

# 情形 A：G > N，Step 1 循环分支输出 CPU
try:
    out = eplb.rebalance_experts_hierarchical(w, 288, 8, 4, 32)
    print("情形 A 跑通")
except RuntimeError as e:
    print("情形 A 崩溃于:", str(e).splitlines()[0])
```

需要观察的现象与预期结果：情形 C 在当前 HEAD 应跑通（若回退到 `git checkout d52c72d^` 则应崩在 arange 处）；情形 A 应崩在 [eplb.py:112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112) 的 gather，报错信息含「same device」字样。同时用 `git stash` / `git checkout d52c72d^ -- eplb.py` 前后对照，亲眼看两行 diff 的作用范围。（CUDA 行为基于 PyTorch 公开语义推演，**待本地验证**；无 GPU 环境可跳过运行，仅完成上表审计并核对每行源码。）

#### 4.4.5 小练习与答案

**练习 1**：修复为什么选 `group_pack_index.device`，而不是写 `device="cuda"`？

**答案**：跟随锚张量让修复在 CPU 输入下依然正确（此时 `group_pack_index` 在 CPU，arange 也应在 CPU）；硬编码 `'cuda'` 会反过来弄坏 CPU 路径。更严格的说法是：被加数 `pphy2mlog` 的设备才是这里的语义目标，`group_pack_index.device` 是在可达路径上与之一致的锚（见练习 2）。

**练习 2**：如果让你把这一行改得更语义化，锚哪个张量最合适？

**答案**：锚 `pphy2mlog.device`——设备一致性约束直接存在于「相加的两个操作数」之间，锚定左操作数让正确性不再依赖「上游某张量恰好同设备」的隐式链路。当前写法能工作是因为在所有可达路径上 `group_pack_index` 与 `pphy2mlog` 同源，但那是推演出来的不变量而非局部可见的事实。这是防御性设备编程的一般原则：**在运算发生的现场锚定设备**。

**练习 3**：情形 A 的崩溃点在 [eplb.py:112](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L112)，如果要修复它，最小改动是什么？

**答案**：把 [eplb.py:28-29](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L28-L29) 的 `device='cpu'` 改为 `device=weight.device`，并考虑把 [eplb.py:27](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L27) 的 `.indices.cpu()` 与循环内的逐元素访问策略一起调整（例如练习 4.1.5-2 的 `tolist()` 方案：在原设备完成排序、把索引与权重转成 Python 列表计算、最后在 `weight.device` 上构造输出张量）——既修设备又提速，一举两得。改完必须重跑 u3-l1 测试与 4.4.4 的三个情形。

### 4.5 演化案例 e1100fe：全局策略如何补上 GPU 级均衡

#### 4.5.1 概念说明

commit [e1100fe](https://github.com/deepseek-ai/EPLB/commit/e1100fe)（2025-03-21，标题 *add gpu-level load balance for global policy*，关闭 issue #14）的 diff 只有一处实质变更——全局策略分支的调用替换：

```python
# 修复前（e1100fe^）：
phy2log, phyrank, logcnt = replicate_experts(weight, num_replicas)
# 修复后（当前 HEAD，即 eplb.py:156）：
phy2log, phyrank, logcnt = rebalance_experts_hierarchical(weight, num_replicas, 1, 1, num_gpus)
```

要理解这行为什么是一个 **bug 修复**而不只是重构，需要看清旧版输出的物理含义：`replicate_experts(weight, num_replicas)` 返回的 `phy2log` 是「前 \(E\) 个槽位按逻辑编号原序排列 + \(M-E\) 个冗余副本追加在尾部」的列表。而 EPLB 的下游契约是**物理槽位按连续分块切给 GPU**（u1-l3、u2-l5 讲过的槽位编码）。两者叠加意味着：哪个 GPU 分到哪些专家，完全由「编号的自然顺序」决定，与负载无关——重载专家的副本可能挤在同一张 GPU 上。**旧全局策略只回答了「复制谁」，从未回答「放在哪」**，装箱这半截工程是缺失的。

#### 4.5.2 核心流程

新版用退化参数 \((M, G{=}1, N{=}1, P)\) 调用层级函数，三步自动退化（u2-l6 已建立结论，此处从断言与开销角度补全论证）：

1. **四条整除断言全部通过**：\(E \bmod 1 = 0\)、\(1 \bmod 1 = 0\)、\(P \bmod 1 = 0\)、\(M \bmod P = 0\)（最后一条本就是入口契约）。退化参数落在合法域内，不是侥幸；
2. **Step 1 退化为恒等置换**：`balanced_packing([L,1], 1)` 命中 `groups_per_pack == 1` 平凡分支，`pack_index`、`rank_in_pack` 全零，代入 [eplb.py:106-107](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L106-L107) 得 \((0 \cdot 1 + 0) \cdot E + \mathrm{arange}(E) = \mathrm{arange}(E)\)，即 `log2mlog` 为恒等；
3. **Step 2 退化为全局复制**：`view(-1, E)` 原样（\(N{=}1\)），`replicate_experts` 直接对整层做全局贪心复制——与旧版行为完全一致的部分；
4. **Step 3 是新增的价值**：`balanced_packing(tokens_per_phy, P)` 以每副本负载为权重，把 \(M\) 个物理专家装箱到全部 \(P\) 张 GPU（每卡恰 \(M/P\) 个，基数约束成立），随后走完整的映射链合成（[eplb.py:119-L128](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py#L119-L128)，在退化参数下 pphy 编码退化为「GPU → 槽内」两级）。

#### 4.5.3 源码精读

**代价侧：性能开销增加了多少。** 对比新旧两条路径的循环与张量操作：

| 项目 | 旧版（仅复制） | 新版（退化层级） |
| --- | --- | --- |
| 复制循环轮数 | \(M - E\) | \(M - E\)（相同，Step 2 不变） |
| 装箱调用 | 0 | 1 次：\(L\) 行 × \(M\) 项 × 扫描 \(P\) |
| 额外张量链 | 无 | 恒等置换构造、两次 `inverse`、四次以上 gather |

新增的装箱调用正是 4.1 分析过的热点模式（\(T \sim L \cdot M \cdot P\) Python 步）——但注意退化路径下 Step 1 的装箱走的是**平凡分支**（无循环、纯张量构造），只有 Step 3 一次真实装箱。总体：全局策略变慢了（多一次装箱与若干 gather），但仍与层级策略同数量级——**用一次装箱的计算成本，换回 GPU 级负载均衡**，这笔交易在重排以分钟/小时为周期触发的场景下（u3-l4 将讨论）几乎免费。

**收益侧：均衡质量提升多少。** u3-l2 已经用实验给出结论：全局策略（装箱后）在长尾负载下的 IB 显著低于「仅复制 + 自然分块」的旧语义；且旧版的 IB 几乎不随冗余数改善——因为瓶颈在「放在哪」而不在「复制谁」，加副本只会让自然分块的错配更复杂。4.5.4 的实践将用 git 历史版本亲手复现这个对比。

**设计模式侧：退化复用 vs 双实现。** 这次重构没有为全局策略新写一行装箱代码，而是发现「层级实现在 \((1, 1, P)\) 参数下恰好就是想要的全局算法」。收益是一份代码维护两条策略、语义升级自动同步；代价是全局策略的正确性从此**隐式依赖**层级实现的退化行为——四条断言、平凡分支、映射链在退化参数下的表现都必须持续成立（4.5.2 第 1、2 条就是这条依赖的检查清单）。u3-l5 将把这个模式作为二次开发的模板。

#### 4.5.4 代码实践

实践目标：从 git 历史取出旧版实现，量化 e1100fe 前后全局策略的均衡差异与耗时差异。

操作步骤：

1. 导出两个版本（只读操作，不改工作区源码；示例命令）：

```bash
git show e1100fe^:eplb.py > /tmp/eplb_old.py    # 旧版：全局策略只复制不装箱
cp eplb.py /tmp/eplb_new.py                      # 当前 HEAD
```

2. 对比脚本（示例代码，非项目原有）：

```python
import sys, timeit, torch
sys.path.insert(0, "/tmp")
import eplb_old, eplb_new

def gpu_loads(weight, phy2log, logcnt, num_gpus):        # 承接 u3-l2 的评估函数
    load = (weight.float() / logcnt).gather(-1, phy2log)
    return load.view(weight.size(0), num_gpus, -1).sum(-1)

torch.manual_seed(0)
L, E, M, P = 60, 256, 288, 32
w = torch.rand(L, E) ** 3 * 1000        # 长尾分布：多数专家轻、少数极重

for name, mod in (("旧版", eplb_old), ("新版", eplb_new)):
    phy2log, log2phy, logcnt = mod.rebalance_experts(w, M, 3, 2, P)   # 3%2≠0 → 全局策略分支
    g = gpu_loads(w, phy2log, logcnt, P)
    ib = (g / g.mean(-1, keepdim=True)).max().item()                   # 承接 u3-l2 的 IB
    t = timeit.timeit(lambda: mod.rebalance_experts(w, M, 3, 1, P), number=5) / 5
    print(f"{name}: IB={ib:.3f}  耗时={t*1000:.1f}ms")
```

需要观察的现象与预期结果：旧版 IB 明显高于新版（自然分块 vs 贪心装箱的差距），新版耗时略高（多一次装箱）。两版输出的 `logcnt` 应**完全相同**——新版退化路径 Step 2 的输入恰是原序 `weight`（恒等置换 + `view` 后不变），复制决策与旧版逐位一致，差异只来自其后的装箱重排。具体数值**待本地验证**；若想进一步定位差异来源，可打印两版 `phy2log[0]` 的前几个槽位——旧版应呈「0,1,2,…」的原序开头，新版则是装箱后的乱序。

#### 4.5.5 小练习与答案

**练习 1**：旧版全局分支返回的 `phy2log` 形状与新版相同（\([L, M]\)，`log2phy` 组装逻辑也相同），为什么说它是「不完整的方案」？

**答案**：形状合法不等于语义正确。EPLB 的输出契约不只是「一张 [L, M] 的表」，而是「槽位号按 节点→GPU→槽内 编码、可被下游连续分块消费的放置方案」（u2-l5、u3-l2 的布局契约）。旧版表的内容是复制后的自然序，隐含的放置是「按编号顺序切块」，负载不均衡；u3-l1 的不变量 INV-1~7 大多仍能通过（每卡数量、守恒、互逆都成立），只有均衡质量类检查能暴露问题——再次印证 u3-l1 的结论：**结构正确性与均衡质量必须分开验收**。

**练习 2**：如果未来要给层级策略增加「节点级带宽权重」这样的新特性，退化复用的全局策略会自动受益吗？

**答案**：会——只要特性实现在层级函数内部且在退化参数下行为合理（例如带宽权重在 \(N{=}1\) 时退化为常数）。这正是退化复用的核心红利：单点改动、两条策略同步受益。但反过来也要警惕：若新特性依赖「组」或「节点」的真实语义（如组间亲和），在 \(G{=}1, N{=}1\) 的退化世界里这些概念是缩并的，需要显式检查退化行为是否仍是想要的全局策略——依赖清单见 4.5.2。u3-l5 的变体实战会正面处理这个问题。

## 5. 综合实践

**任务：产出一份《EPLB 微基准与优化报告》**，把本讲四个维度（尺度、优化、数值、设备）串成一条工作流。

1. **基准矩阵**：对参数组合 \(\{L{=}1, 60\} \times \{G{=}8（层级）, G{=}3（全局）\}\)（固定 \(E{=}256, M{=}288, N{=}4, P{=}32\)）用 `timeit`（number=5，取均值）测量 `rebalance_experts` 耗时，填一张 2×2 表，验证耗时近似线性于 \(L\) 且两种策略同数量级（4.1、4.5.3 的预测）。
2. **热点定位**：对 \(L{=}60\) 的层级分支跑 `cProfile`，确认 `balanced_packing` 的 `tottime` 主导；截图/摘录前 12 行统计（4.1.4）。
3. **优化实验**：实现 4.2.3 的 `replicate_experts_fast`，先跑 20 组随机输入的逐元素等价断言，再接入实验版层级函数跑 u3-l1 的 `test_eplb.py`，最后在全局分支（\(R = M-E = 32\) 最大）上测加速比。
4. **（可选，需 GPU）设备审计**：运行 4.4.4 的三个情形，记录当前 HEAD 的通过/崩溃情况，与 4.4.3 的推演表逐条对照。
5. **结论**：用三到五句话回答——EPLB 在真实规模下的瓶颈是什么？值不值得进一步向量化？入口的 `.float().cpu()` 各自在防御什么？

验收标准：等价性断言与不变量测试全绿后再引用任何性能数字；报告中每个数字都注明测量方法（number、机器、torch 版本）。

## 6. 本讲小结

- **瓶颈定位**：EPLB 的耗时不在张量操作，而在 `balanced_packing` 的 Python 双层循环（\(T_{\text{pack2}} \sim L \cdot M \cdot P/N\) 步逐元素张量读写），装箱调用 2（物理专家→GPU）比调用 1 高约两个数量级；`replicate_experts` 的循环轮数少且每轮已按行向量化，通常不是热点。
- **向量化方法论**：先区分**语义串行**（贪心的轮间依赖，改了就不是同一算法）与**实现冗余**（每轮全表重算 `weight/logcnt`）；水位维护版消除了后者，与原版位级等价，但渐进复杂度不变——argmax 本身就是贪心语义的成本。
- **数值与类型**：入口 `weight.float().cpu()` 一行三防——把任意输入统计钉死在 float32 数值路径（防整数除法语义漂移与低精度并列）、把逐元素循环搬到 CPU（防 GPU 单元素写的内核启动开销）、并让全部设备陷阱在公开 API 上不可达。
- **设备审计**：d52c72d 修复了 Step 3 `arange` 缺 `device=` 的崩溃，但那是定点修复——`balanced_packing` 循环分支硬编码 CPU（L28-29）与平凡分支设备不一致的深层问题仍在，直接以 CUDA 张量调用层级函数在 \(G>N\) 或 \(M/P>1\) 的常见组合下依然会崩；可靠的防线是入口收口。
- **演化案例 e1100fe**：旧全局策略只复制不装箱，「放在哪」这半截工程缺失导致负载不均；改为以退化参数 \((M,1,1,P)\) 复用层级实现后，用一次装箱的成本换回 GPU 级均衡，并确立了「退化参数复用」这一单实现服务双策略的架构模式，其正确性依赖四条断言与平凡分支在退化参数下持续成立。
- **工程节奏**：先 profile 定位、再语义分类、优化后先跑不变量测试、性能数字必须附测量方法——这条流水线比任何单个结论更值得带走。

## 7. 下一步学习建议

- **u3-l4（工程集成）**：本讲的耗时分析直接决定重排的触发节奏——重排一次花多少毫秒、参数迁移花多少秒，两者共同决定「多长时间重排一次」的权衡。读完本讲再去设计「统计 → rebalance_experts → 权重重排」流水线，性能预算就有了着落。
- **u3-l5（二次开发实战）**：把本讲的两个产出——水位维护版复制函数、退化复用的依赖检查清单——作为变体实验的起点，实现「最大副本数受限」等变体并用 u3-l2 的指标体系评估。
- **源码再读**：带着审计清单重读 [eplb.py](https://github.com/deepseek-ai/EPLB/blob/d52c72d5b2f2fb4c41afbf8eb21366820239913d/eplb.py) 一遍，试着自己发现 4.4 表格之外的可疑点（提示：`torch.full_like` 与 `torch.zeros_like` 的 device 继承规则略有差别）；再对照 [e1100fe](https://github.com/deepseek-ai/EPLB/commit/e1100fe) 与 [d52c72d](https://github.com/deepseek-ai/EPLB/commit/d52c72d) 的父提交，体会「小 diff、大因果」的阅读方式。
