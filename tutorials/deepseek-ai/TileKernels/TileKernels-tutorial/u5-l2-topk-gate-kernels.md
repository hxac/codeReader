# Top-k 门控 kernel：reducer 与 warp shuffle

> 前置：本讲承接 **u5-l1（MoE 路由概览与打分函数）**。在 u5-l1 里我们已经把路由问题拆成两步——先打分（sigmoid/softmax/sqrtsoftplus），再选取（top-k）。本讲专门讲「选取」这一步如何在 GPU 上高效且稳定地实现。

## 1. 本讲目标

学完本讲，你应该能够：

- 画出 `topk_gate` kernel「反复找最大」的主流程，并解释它为何用 `T.reduce_max` + `alloc_reducer('min')` 两段配合。
- 说清「并列最大时取更小下标」这一稳定语义是如何被 `min` reducer 实现的，以及它为何与 PyTorch 的 `stable_topk` 位一致。
- 掌握 warp（线程束）原语 `T.shfl_sync`（按 lane 广播）、`T.shfl_xor`（蝶形交换）、`T.sync_warp`（束内同步）的语义与用途。
- 读懂 `get_topk_group_idx`（`moe/common.py`）用「计数排序 + shfl_sync」做分组 top-k 的逻辑。
- 把 `top2_sum_gate` 这条完整路由链路（打分 → 分组粗筛 → 细选 top-k → 归一化 → EP/TP 掩码）串成一条线。

## 2. 前置知识

在进入源码前，先建立三个心智模型。

### 2.1 top-k 是「按值排序后取前 k 个下标」

给定一行分数 `scores = [s_0, s_1, ..., s_{E-1}]`（每个 `s_i` 是某 token 对专家 `i` 的偏好分），top-k 要输出「分数最大的 k 个专家的**下标**」。注意输出是**下标**而不是分数本身。当多个专家分数并列时，需要一个确定性的打破并列规则（tie-break）——TileKernels 选择的规则是 **并列时取下标更小者**，这与 PyTorch 的稳定排序一致。

### 2.2 一个 warp = 32 个线程，是 GPU 调度的最小单位

GPU 的线程按 32 个一组组成一个 **warp（线程束）**，warp 内的线程锁步执行同一条指令。warp 原语（shuffle）让 warp 内任意两个线程直接交换寄存器值，**不经过共享内存**，速度极快。本讲里所有 kernel 都用「一个 warp 处理一个 token」的设计，于是选 top-k 的协作天然落在 warp 内。

### 2.3 两种「跨线程归约」风格

- **fragment + 内置 reduce**：把数据放进协作布局寄存器 `T.alloc_fragment`，调用 `T.reduce_max` 让编译器自动做跨线程归约（`topk_gate` 用这种）。
- **local + 手写 shuffle**：每个线程持有自己的私有寄存器 `T.alloc_local`，用 `T.shfl_xor` 蝶形归约手动合并（`top2_sum_gate` 用这种）。

这两条路线都将在本讲出现，理解它们的等价性是本讲的重点之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tile_kernels/moe/topk_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py) | 最简形式的 top-k 选取 kernel：每 token 一个 warp，反复 `reduce_max` 选最大，用 `min` reducer 稳定打破并列。含 Python wrapper `topk_gate`。 |
| [tile_kernels/moe/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py) | `get_topk_group_idx` 宏：在 warp 内用「计数排序 + `shfl_sync`」选出 top-k 个组。 |
| [tile_kernels/moe/top2_sum_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py) | 完整路由 kernel：打分、分组粗筛、细选 top-k、权重归一化、logical→physical 映射、EP/TP 掩码一条龙。展示了 `shfl_xor` 蝶形归约与 `warp_reduce_sum` 宏。 |
| [tile_kernels/torch/topk.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py) | 纯 PyTorch 参考实现，其中的 `stable_topk` 是 `topk_gate` 的对拍基准。 |
| [tests/moe/test_topk_gate.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py) | `topk_gate` 的正确性与基准测试，演示对拍范式。 |

> 调用入口：用户调用 `tile_kernels.moe.topk_gate(...)`（由 [tile_kernels/moe/__init__.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/__init__.py) 从 `topk_gate_kernel.py` 再导出）。真正干活的不是 TileLang kernel 对象，而是该文件里的 wrapper 函数——这是 u1-l3 讲过的「用户入口 = wrapper」约定。

---

## 4. 核心概念与源码讲解

### 4.1 topk_gate_kernel：反复选最大的主流程

#### 4.1.1 概念说明

`topk_gate` 是 top-k 选取的**最简形态**：输入只有打完分的 `scores`（`num_tokens × num_experts`），输出每行的 top-k 下标。它不涉及打分、不涉及 bias、不涉及并行掩码——把「选取」这件事单独抽出来，便于我们先理解算法骨架。

核心算法是**反复找最大**：要选 k 个，就找 k 次。每轮：

1. 找当前所有分数里的最大值。
2. 在「等于该最大值的专家」中，挑出**下标最小**的那个（稳定并列处理）。
3. 把选中的专家分数置为 `-inf`，下一轮它就不会再被选中。

这种朴素方法的代价是 \(O(k \cdot E)\)（E 是专家数），但 MoE 场景里 \(k\) 和 \(E\) 都很小（典型 \(k=6, E\le 256\)），而且完全在一个 warp 内完成，无需共享内存间多次往返，反而高效。

#### 4.1.2 核心流程

```
对每个 token（一个 block，32 线程 = 一个 warp）：
    1. 把 num_experts 个分数载入 fragment，不足 32 倍数的补 -inf
       （用 align 凑成 num_aligned_experts = 32 的倍数）
    2. 准备一个下标数组 idx_fragment[i] = i
    3. for k in 0..num_topk:                 # unroll 展开
         a. reduce_max(scores_fragment) → amax   # 当前最大分
         b. idx_reducer ← +∞（min 归约器初始化）
         c. 对每个 i：若 scores[i] == amax，
                       idx_reducer = min(idx_reducer, idx[i])
         d. finalize_reducer(idx_reducer)        # 跨线程合并 → 最小下标
         e. topk_idx[k] = idx_reducer            # 记录本轮流选中的下标
         f. 把该下标的分数置 -inf                 # 下一轮剔除
    4. 写回 topk_idx
```

注意第 (b)~(d) 步：`amax` 告诉我们「最大分是多少」，但它**不告诉我们这个最大分出现在哪些下标**。第 (c) 步用 `min` 归约器在「分数等于最大值的候选」里挑出下标最小者——这正是稳定并列处理的关键，下一小节专门讲。

#### 4.1.3 源码精读

**JIT 构造器与运行时符号。** `num_experts` 与 `num_topk` 是编译期参数（被烤进编译产物），`num_tokens` 用 `T.dynamic` 声明为运行时符号。`align` 把专家数补到 32 的倍数，使 fragment 能被 32 线程整除：

[topk_gate_kernel.py:L10-L18](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L10-L18) —— `@tilelang.jit` 装饰、`T.dynamic('num_tokens')`、`num_aligned_experts = align(num_experts, 32)`。其中 `align(x, y)` 定义在 [tile_kernels/utils.py:L5-L6](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/utils.py#L5-L6)，即向上取整到 `y` 的倍数。

**网格与分配。** 一维网格 `T.Kernel(num_tokens, threads=32)`，即一个 block 一个 token、一个 warp。分配中 `idx_reducer` 是本讲的主角：

[topk_gate_kernel.py:L25-L30](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L25-L30) —— 其中 `idx_reducer = T.alloc_reducer((1,), T.int32, 'min', replication='all')` 创建一个「取最小」的跨线程归约器，`replication='all'` 表示每个线程持有一份副本，最终由 `finalize_reducer` 合并。

**载入分数并补 -inf。** 越界的专家填 `-inf`，保证它们永远不会被选为最大：

[topk_gate_kernel.py:L32-L38](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L32-L38) —— 分支：`i < num_experts` 取真实分数，否则填 `-T.infinity(T.float32)`；同时初始化 `idx_fragment[i] = i`。

**反复找最大主循环。** 这是整个算法的心脏：

[topk_gate_kernel.py:L40-L51](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L40-L51) —— 对应流程的 a~f：
- `T.reduce_max(scores_fragment, amax_fragment)`：内置全规约，得到当前最大分。
- `T.fill(idx_reducer, T.max_value(T.int32))`：把每线程的归约器副本初始化为 `int32` 最大值。
- `if scores_fragment[i] == amax_fragment[0]: idx_reducer[0] = T.min(...)`：在等于最大分的候选里取下标最小（线程内局部更新）。
- `T.finalize_reducer(idx_reducer)`：跨所有线程副本做 `min` 合并，得到全局最小的命中下标。
- `scores_fragment[i] = -T.infinity(...)`：剔除本轮流选中的下标。

**wrapper。** 遵循 u2-l1 讲过的固定四步：校验、分配输出、零规模守卫、触发 JIT 并启动：

[topk_gate_kernel.py:L77-L90](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L77-L90) —— 校验 `scores` 为 2 维、连续、float32（第 77 行）；`num_tokens == 0` 时直接返回空张量（第 81-82 行，零规模守卫在 wrapper 做）；构造 kernel 并启动（第 84-89 行）。

#### 4.1.4 代码实践

**实践目标：** 跟踪 `topk_gate` 的一条完整调用链，验证它的输出与 `stable_topk` 逐元素一致，并亲手构造一个「多重并列」的输入观察稳定并列行为。

**操作步骤：**

1. 阅读参考实现 [tile_kernels/torch/topk.py:L8-L10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L8-L10) —— `stable_topk` 用 `torch.sort(..., descending=True, stable=True)`，稳定排序保证等值键保持原序（即下标升序）。
2. 在一张 SM90/SM100 GPU 上运行如下最小对拍脚本（**示例代码**，需 torch+tilelang 环境方能执行）：

   ```python
   # 示例代码：topk_gate 与 stable_topk 对拍
   import torch, tile_kernels
   from tile_kernels.torch import stable_topk

   torch.manual_seed(0)
   num_tokens, num_experts, num_topk = 4, 8, 3
   scores = torch.randn(num_tokens, num_experts, dtype=torch.float32, device='cuda')

   ref = stable_topk(scores, num_topk)
   out = tile_kernels.moe.topk_gate(scores, num_topk)
   assert torch.equal(out, ref), (out, ref)   # 逐元素位精确
   print("随机输入对齐：OK")
   ```

3. 构造一个故意制造并列的输入（同一行多个专家分数完全相等），观察 kernel 是否总取下标较小者：

   ```python
   # 示例代码：并列场景
   scores_tie = torch.full((1, 8), -1.0, dtype=torch.float32, device='cuda')
   scores_tie[0, [1, 3, 5]] = 9.9   # 三个并列最大
   out_tie = tile_kernels.moe.topk_gate(scores_tie, num_topk=2)
   print(out_tie)   # 预期第一行前两个为 [1, 3]（下标升序）
   ```

**需要观察的现象：**
- 随机输入下 `out` 与 `ref` 应**完全相等**（`torch.equal` 返回 True），因为两者都是稳定排序语义。
- 并列场景下，输出应按选中顺序为 `[1, 3]`（下标升序），而不是 `[3, 1]` 或 `[5, 1]`。

**预期结果：** 两段断言都通过。若你在没有 GPU/TileLang 的只读环境，则上述为「**待本地验证**」——可改为先用 `stable_topk` 单独推导手算结果，确认逻辑后再上机。

#### 4.1.5 小练习与答案

**练习 1.** 假设 `scores = [5, 5, 3, 5, 1]`、`num_topk = 3`，手算 `topk_gate` 每轮选中的下标。

> **答案。** 第 1 轮：max=5，命中下标 {0,1,3}，min=0 → 选 0，置 scores[0]=−inf；第 2 轮：max=5，命中 {1,3}，min=1 → 选 1；第 3 轮：max=5，命中 {3} → 选 3。结果 `[0,1,3]`，与 `stable_topk` 一致。

**练习 2.** 为什么把选中专家的分数置为 `-inf` 而不是置为 0？

> **答案。** 置 0 不能保证剔除：如果存在负分专家，0 仍可能成为新的「最大」被错误选中。置 `-inf` 才能确保它在该轮及之后永远不再被 `reduce_max` 选中。

**练习 3.** `num_aligned_experts = align(num_experts, 32)` 中，若 `num_experts = 36`，`num_aligned_experts` 是多少？补的 28 个位置值是多少？

> **答案。** `align(36, 32) = ceil(36/32)*32 = 64`。多出来的 28 个位置填 `-inf`（见源码第 35-36 行的 `else` 分支），保证它们不参与选取。

---

### 4.2 稳定并列处理：min reducer 与 shfl 蝶形归约

#### 4.2.1 概念说明

「并列时取更小下标」听起来简单，但在并行环境下实现却要费心思。本节对比两种实现稳定并列的方式，它们都贯穿本讲：

1. **`min` 归约器（topk_gate 用）**：把「打破并列」和「跨线程合并」一起交给 `alloc_reducer('min', replication='all')`。
2. **显式 `(score, idx)` 比较 + 蝶形 shuffle（top2_sum_gate 用）**：用 `T.shfl_xor` 蝶形归约，在比较函数里写死「等值取更小 idx」。

先回顾 u2-l3 讲过的 reducer 三步曲：`T.fill`（初始化每线程副本）→ 并行更新（每线程改自己的副本）→ `T.finalize_reducer`（跨线程合并）。`replication='all'` 让每线程各持一份，最后合并所有副本。

`min` 归约器为何能做稳定并列处理？关键在于**非命中线程不更新**：在「`if scores[i] == amax`」的保护下，只有分数等于最大值的下标才会去更新 `idx_reducer`，其余线程的副本保持初始值 `+∞`。于是 `min` 归约后，得到的就是「所有命中下标里的最小者」。这正是「并列取小下标」。

#### 4.2.2 核心流程

**min reducer 方式（对应 4.1.3 的循环）：**

```
amax = reduce_max(scores)                 # 最大分
fill(idx_reducer, +∞)                     # 每线程副本 = +∞
for i: if scores[i] == amax:
           idx_reducer = min(idx_reducer, idx[i])   # 只有命中者更新
finalize_reducer(idx_reducer)             # 跨线程 min → 最小命中下标
```

**显式比较方式（top2_sum_gate 的细选，4.3 节详讲）：**

```
# 每线程先在自己持有的 experts 里找局部 (max_score, min_idx)
# 再用 5 轮 shfl_xor 蝶形归约，比较函数为：
better(a, b) = a.score > b.score
            OR (a.score == b.score AND a.idx < b.idx)
```

后者本质上是把 \((-score, idx)\) 当作字典序最小来比——分数大的赢，分数并列时下标小的赢。两种方式**语义等价**，只是归约手段不同。

#### 4.2.3 源码精读

**min 归约器的命中-更新-合并三步：**

[topk_gate_kernel.py:L43-L48](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L43-L48) —— `T.fill(idx_reducer, T.max_value(T.int32))` 初始化；条件 `scores_fragment[i] == amax_fragment[0]` 保护下取 `T.min`；`T.finalize_reducer(idx_reducer)` 合并。注意「非命中线程不更新」是由 `if` 守卫保证的，它们的副本仍是 `+∞`，不影响最终 `min`。

**显式比较的并列规则（4.3 节细讲，这里先看比较式）：**

[top2_sum_gate_kernel.py:L244-L249](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L244-L249) —— `if other_score > topk_scores_local[k] or (other_score == topk_scores_local[k] and other_idx < topk_idx_local[k])`，正是 `better(a,b)` 的字面翻译：等值时取 `other_idx` 更小者。

#### 4.2.4 代码实践

**实践目标：** 论证 `topk_gate` 为何用 `alloc_reducer('min', replication='all')` 来稳定处理并列，并对照 `stable_topk` 在含并列的输入上对拍。

**操作步骤：**

1. 阅读测试 [tests/moe/test_topk_gate.py:L47-L54](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L47-L54) —— 项目本身就用 `torch_stable_topk` 与 `topk_gate` 做 `assert_equal`（位精确）对拍，这正是你要复现的实验。
2. 构造一个整行分数全等的极端输入（所有专家并列），用 `num_topk = E`，验证输出恰为 `[0, 1, 2, ..., E-1]`（升序）：

   ```python
   # 示例代码：全并列
   E = 16
   scores = torch.full((1, E), 2.0, dtype=torch.float32, device='cuda')
   out = tile_kernels.moe.topk_gate(scores, num_topk=E)
   print(out)   # 预期 [[0,1,2,...,15]]
   ```

3. 推理：若把第 46 行的 `T.min` 改成 `T.max`，输出会变成什么？

**需要观察的现象与预期结果：** 全并列输入下输出必须是严格升序 `[0..E-1]`，证明 `min` 归约器确实在并列时选小下标。第 3 步的推理结论：改成 `T.max` 后会在并列里选**最大**下标，与 `stable_topk` 不再一致（测试会失败）——这反过来说明 `min` 是刻意为之。无 GPU 环境记为「**待本地验证**」。

#### 4.2.5 小练习与答案

**练习 1.** 为什么 `idx_reducer` 初始化成 `+∞` 而不是 0？

> **答案。** 因为我们对它做 `min` 归约。若初始化为 0，任何真实下标（都 ≥ 0）都取不到比 0 更小，归约结果恒为 0，丢失了真实命中信息。初始化为 `+∞` 才能让「未命中线程」的副本不干扰最终的 `min`（只有命中线程会把它拉低到真实下标）。

**练习 2.** `replication='all'` 在这里的作用是什么？如果省略会怎样？

> **答案。** `replication='all'` 让每个线程持有一份归约器副本，`finalize_reducer` 再跨所有线程合并。若不复制（每线程无独立副本），并发更新同一存储会有写竞争；这套 fill→局部更新→finalize 的三步正是为 `replication='all'` 设计的安全并行归约模式。

---

### 4.3 moe/common 的 get_topk_group_idx：shfl_sync 与计数排序

#### 4.3.1 概念说明

`top2_sum_gate` 在细选 top-k 之前，会先做一轮**分组粗筛**：把 `num_routed_experts` 个专家分成 `num_groups` 组，先选出「组内 top-2 之和最大」的 `num_topk_groups` 个组，再只在这些组里细选 top-k（这是 DeepSeek-MoE 的 group-limited routing 思想，能提升负载均衡）。`get_topk_group_idx` 就是这个粗筛步骤，它是一个 `@T.macro`，被 `top2_sum_gate_kernel` 内联调用。

它的精妙之处在于用**计数排序**求出每个组的稳定排名，全程只用 warp shuffle，无需额外共享内存往返。两个核心 warp 原语：

- `T.shfl_sync(val, src_lane)`：让本线程读取 `src_lane` 号线程的 `val`（广播式读取，**同步**）。用它可以把「别人的 top-2 和」拿过来比较。
- `T.sync_warp()`：warp 内同步屏障，确保此前所有共享内存写完成。

#### 4.3.2 核心流程

```
每个 lane 代表一个组（lane_idx < num_groups 才有效）
1. 计算本组的 top-2 之和 topk_sum_var（top1 + top2）
2. 算出我的稳定排名 count_var：
       遍历所有组 i，用 shfl_sync 取来 i 的 topk_sum：
         若 i 的和 > 我的和 → count++（比我强的）
         若 i 的和 == 我的和 且 i < 我（lane_idx）→ count++（并列但下标更小）
   于是 count_var 就是「我前面有几个组」，即稳定降序排名。
3. 若 count_var < num_topk_groups：
       我入选，把我的 lane_idx 写到 topk_group_idx_shared[count_var]
   （排名即写入位置，天然升序）
4. sync_warp
```

这里的 `i < lane_idx` 与 4.2 节的「等值取小下标」是**同一个稳定规则**：并列时下标小的组排名靠前。

#### 4.3.3 源码精读

**宏签名与 lane 解析。** 它操作共享内存缓冲 `scores_shared`，输出 `topk_group_idx_shared`：

[common.py:L4-L17](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L4-L17) —— `thread_idx = T.get_thread_binding()` 拿到线程号，`lane_idx = thread_idx % 32` 拆出 warp 内 lane。

**每组的 top-2 之和。** 注意 `vec_idx = (i + lane_idx) % num_vec_experts_per_group` 这一行带「错位」，注释写明是 **「Shift to avoid bank conflict」**——让不同 lane 访问共享内存时错开 bank：

[common.py:L24-L39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L24-L39) —— 维护 `top1_var/top2_var` 两个标量，扫描组内所有元素，更新 top-2；最后 `topk_sum_var = T.Select(num_topk_sum == 1, top1_var, top1_var + top2_var)`。

**计数排序求排名（本节核心）。** 用 `shfl_sync` 把每个 lane 的 `topk_sum_var` 广播出来比较：

[common.py:L41-L49](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L41-L49) —— `other_top2_sum = T.shfl_sync(topk_sum_var, i)` 取 lane `i` 的和；`count_var` 在「严格更大」或「相等且 `i < lane_idx`」时自增；最后 `if count_var < num_topk_groups: topk_group_idx_shared[token_idx, count_var] = lane_idx`，排名即写入下标，结果天然升序稳定。

**收尾同步：**

[common.py:L51-L52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L51-L52) —— `T.sync_warp()` 确保所有组下标都写完后再返回。

#### 4.3.4 代码实践

**实践目标：** 用纯 PyTorch 复现 `get_topk_group_idx` 的「计数排序求稳定排名」逻辑，与 kernel 在一组随机数上比对。

**操作步骤：**

1. 复现参考（**示例代码**）：

   ```python
   # 示例代码：计数排序求 top-k 组下标（升序稳定）
   import torch
   def ref_topk_group(sums: torch.Tensor, num_topk_groups: int):
       # sums: [num_groups]
       g = sums.numel()
       rank = torch.zeros(g, dtype=torch.long)
       for me in range(g):
           cnt = 0
           for i in range(g):
               if sums[i] > sums[me] or (sums[i] == sums[me] and i < me):
                   cnt += 1
           rank[me] = cnt
       out = torch.full((num_topk_groups,), -1, dtype=torch.long)
       for me in range(g):
           if rank[me] < num_topk_groups:
               out[rank[me]] = me
       return out

   sums = torch.tensor([3.0, 5.0, 5.0, 1.0, 4.0])  # 注意 lane1、lane2 并列
   print(ref_topk_group(sums, num_topk_groups=3))  # 预期 [1, 2, 4]
   ```

2. 阅读参考实现 [tile_kernels/torch/topk.py:L13-L19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L13-L19) —— `topk_sum_and_topk_group_idx` 用 `scores.topk(2).sum(-1)` 算每组和、再 `stable_topk` 取组下标，是 kernel 的对拍基准。

**需要观察的现象与预期结果：** 上例输出 `[1, 2, 4]`——并列的两个 5.0 中，lane 1（下标小）排在 lane 2 前；4.0 排第三。这验证了「相等且 `i < lane_idx` 时 count 自增」确实把下标小的组排到更前。注意：真实的 kernel 输入是每组的 top-2 之和而非裸 `sums`，这里为了聚焦计数排序逻辑做了简化，**严格对拍需在端到端 `top2_sum_gate` 上做**（见 4.4.4）。

#### 4.3.5 小练习与答案

**练习 1.** 若把 `common.py:L44` 的条件从 `i < lane_idx` 改成 `i > lane_idx`，排名会怎样变化？

> **答案。** 改成 `i > lane_idx` 后，并列时反而让**下标更大**的组排名靠前（入选），与稳定排序相反。最终 `topk_group_idx` 在并列组上会变成下标降序，与 `stable_topk` 参考不再一致。

**练习 2.** `T.shfl_sync(topk_sum_var, i)` 与直接读 `scores_shared` 比有什么优势？

> **答案。** `topk_sum_var` 是每个 lane 在寄存器里算出的标量，`shfl_sync` 直接从对方寄存器取值，不经过共享内存、无需 `sync_threads`，延迟最低。这正是「把每组的聚合结果留在寄存器、用 shuffle 交换」的设计动机。

---

### 4.4 top2_sum_gate_kernel：完整路由与蝶形归约

#### 4.4.1 概念说明

`top2_sum_gate` 是一条**端到端路由流水线**：从原始 `logits` 出发，一路加工到可直接用于派发的 `(topk_idx, topk_weights)`。相比 `topk_gate` 的「只选取」，它多了打分、bias、分组粗筛、权重归一化、logical→physical 映射、EP/TP 掩码等环节。本节聚焦其中与「选取」和「warp 原语」相关的部分，把前面学到的 `shfl_sync`、`shfl_xor`、`sync_warp` 串起来用。

它用 `local + 手写 shuffle` 风格：每个 lane 持有 `num_routed_experts_per_thread` 个专家，先在本地找局部最优，再用 **5 轮 `shfl_xor` 蝶形归约**把局部最优合并成 warp 内全局最优。

#### 4.4.2 核心流程

```
对每个 token（一个 warp）：
  A. 打分：按 scoring_type 算 scores（sigmoid/sqrtsoftplus/softmax），
     softmax 需要先 warp_reduce_max 求 max、再 warp_reduce_sum 求 sum（归一化分母）
  B. 分组粗筛（若 num_groups != num_topk_groups）：
     调 get_topk_group_idx 选出 num_topk_groups 个组，
     冒泡排序成升序，每 lane 从这些组里加载候选分数
  C. 细选 top-k（反复找最大 + 5 轮 shfl_xor 蝶形归约）：
     - 每 lane 在本地持有的候选里找局部 (max_score, 对应 idx)
     - 5 轮 shfl_xor（offset 1,2,4,8,16）合并，等值取更小 idx
     - 把上一轮选中的 idx 在本地置 -inf
  D. 归一化：topk_sum 用 shfl_sync 汇总各 lane 的 score，weights = score / sum
  E. （可选）logical→physical 映射 + EP/TP 掩码写出
```

**蝶形归约原理。** `shfl_xor(val, mask)` 让 lane `L` 与 lane `L XOR mask` 交换值。5 轮的 offset 取 \(2^0, 2^1, 2^2, 2^3, 2^4 = 1,2,4,8,16\)，恰能覆盖 32 个 lane。每轮把「对方已经聚合的一半」并到自己，5 轮后每个 lane 都拿到全 warp 的归约结果——这是经典的 butterfly reduction。

#### 4.4.3 源码精读

**`warp_reduce_sum` 宏：5 轮 `shfl_xor` 蝶形求和。** 注释「Keep the same with the old implementation」说明它刻意保持与旧实现一致；offset 从 `1<<(4-i)` 即 16,8,4,2,1（与细选的 1,2,4,8,16 顺序相反，但都能覆盖）：

[top2_sum_gate_kernel.py:L12-L16](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L12-L16) —— `x += T.shfl_xor(x, 1 << (4 - i))`，参数 `x: T.Ref` 表示就地累加。

**softmax 分支的 warp 归约。** softmax 需要 \(\sum_j e^{s_j - \max}\)，分两步：先求 max（`warp_reduce_max`），再求 sum（`warp_reduce_sum` + `sync_warp`）：

[top2_sum_gate_kernel.py:L155-L165](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L155-L165) —— `logit_max_var = T.warp_reduce_max(logit_max_var)`（内置 warp 归约求 max），再 `T.exp(x - max)` 累加 `logit_sum_var`，最后 `warp_reduce_sum(logit_sum_var); T.sync_warp()`。

**打分函数派发。** 用整数 `scoring_type`（即 `ScoringFunc` 的枚举值，见 [tile_kernels/moe/scoring.py:L5-L9](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/scoring.py#L5-L9)）特化分支：

[top2_sum_gate_kernel.py:L173-L184](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L173-L184) —— `scoring_type == 0` sigmoid、`1` sqrtsoftplus（调 `softplus` 宏）、`2` softmax（除以 `logit_sum_var`）、`3` IDENTITY（仅 `pass`，故 u5-l1 提到它在 top2_sum_gate 中被禁用）。

**分组粗筛。** 调 `get_topk_group_idx` 选组，再用一段 \(O(k^2)\) 冒泡把组下标排成升序，保证后续加载顺序确定：

[top2_sum_gate_kernel.py:L200-L229](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L200-L229) —— 调宏选组（第 202-210 行）；双层 `unroll` 冒泡排序（第 215-220 行）；每 lane 从选中组加载候选（第 225-229 行）。

**细选 top-k：本地找最大 + 蝶形归约。** 这一段对应 4.4.2 的 C，注意本地比较用严格 `>`（`elif scores_local[i] > topk_scores_local[k]`），等值不更新即天然保小下标：

[top2_sum_gate_kernel.py:L231-L249](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L231-L249) —— 本轮先剔除上一轮选中（第 236-237 行），本地扫描找局部最优（第 239-241 行），5 轮 `shfl_xor` 蝶形合并并在比较函数里实现「等值取小 idx」（第 244-249 行，即 4.2.3 引用的那段）。

**归一化：用 shfl_sync 汇总各 lane 的分数。** `topk_sum_var` 用 `T.shfl_sync(topk_score_var, i)` 把 `lane i` 的分数取来累加，得到选中专家的分数和（`1e-20` 防除零）：

[top2_sum_gate_kernel.py:L262-L274](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L262-L274) —— `topk_sum_var += T.shfl_sync(topk_score_var, i)`；随后 `topk_score_var = topk_score_var / topk_sum_var * routed_scaling_factor` 归一化。注意分数取自 `scores_wo_bias_shared`（第 254 行），呼应 u5-l1 讲的「最终权重用不带 bias 的 scores」。

**EP/TP 掩码写出。** 把不属于本卡的专家置 `-1`（这部分属于分布式语义，u5-l5 会专门讲，这里只点到为止）：

[top2_sum_gate_kernel.py:L288-L301](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py#L288-L301) —— 按 `num_experts_per_rank` 判断目标 EP rank，非本 TP 组置 `-1`，否则重映射到本 rank 局部下标。

#### 4.4.4 代码实践

**实践目标：** 在端到端 `top2_sum_gate` 上做对拍，覆盖「无分组」与「有分组粗筛」两条路径，并对照 PyTorch 参考验证稳定并列。

**操作步骤：**

1. 阅读参考 [tile_kernels/torch/topk.py:L22-L206](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L22-L206) —— `top2_sum_gate` 参考实现按「打分 → 加 bias → 分组粗筛（`masked_fill(-inf)`）→ `stable_topk` 细选 → 归一化 → 映射 → 掩码」的顺序，与 kernel 一一对应。特别注意第 141 行 `selected = stable_topk(sb, num_topk)` 是细选基准。
2. 跑一个无分组（`num_groups == num_topk_groups`，触发 `skip_group_sort=True`）的对拍（**示例代码**）：

   ```python
   # 示例代码：top2_sum_gate 端到端对拍（sigmoid，无分组）
   import torch
   from tile_kernels.moe import top2_sum_gate as tk_top2
   from tile_kernels.torch import top2_sum_gate as ref_top2

   torch.manual_seed(0)
   N, E = 16, 64
   logits = torch.randn(N, E, dtype=torch.float32, device='cuda')
   bias = torch.zeros(E, dtype=torch.float32, device='cuda')
   common = dict(num_topk=6, num_topk_groups=1, num_groups=1,
                 use_shared_as_routed=False, num_shared_experts=0,
                 routed_scaling_factor=1.0, ep_rank=0, num_ep_ranks=1,
                 tp_rank=0, num_tp_ranks=1, scoring_func='sigmoid')
   ref_idx, ref_w = ref_top2(logits, bias, **common)
   out_idx, out_w = tk_top2(logits, bias, **common)
   assert torch.equal(out_idx, ref_idx)
   print("top2_sum_gate (无分组) 对齐：OK")
   ```

3. 改成有分组（如 `num_groups=8, num_topk_groups=4`），重新对拍，观察走 `get_topk_group_idx` 粗筛路径后结果仍一致。
4. 用 `TK_PRINT_KERNEL_SOURCE=1` 跑一次，观察 `shfl_xor`/`shfl_sync` 在生成的 CUDA 源码里如何降级成 `__shfl_xor_sync`/`__shfl_sync` 内建。

**需要观察的现象与预期结果：** 两条路径下 `out_idx` 都应与 `ref_idx` 位精确相等（`torch.equal`）。无 GPU/TileLang 环境记为「**待本地验证**」——可先纯用参考实现 `ref_top2` 验证你对流程的理解（手算一个小例子），确认无误后再上机对 kernel。

#### 4.4.5 小练习与答案

**练习 1.** 细选 top-k 时，本地比较用 `elif scores_local[i] > topk_scores_local[k]`（严格大于）。为什么用 `>` 而不是 `>=`？

> **答案。** `idx_local` 在同一 lane 内是按下标升序排列的（加载时 `idx_local[i] = ...` 递增，注释 `# If j > i, then idx_local[j] > idx_local[i]` 也点明）。用严格 `>` 时，遇到等值不更新，于是保留**先出现的（下标更小）**候选，正是稳定并列规则。若改成 `>=`，等值时会用后出现的较大下标覆盖，破坏稳定性。

**练习 2.** 蝶形归约 5 轮的 offset 是 1,2,4,8,16。为什么恰好 5 轮、为什么这个 offset 序列能覆盖 32 个 lane？

> **答案。** 32 = \(2^5\)，所以需要 \(\lceil\log_2 32\rceil = 5\) 轮。offset \(2^0..2^4\) 对应二进制每一位，每轮让 lane 与「该位翻转」的对方交换并合并，5 轮后每一位都已对齐，于是每个 lane 拿到了全 32 lane 的归约结果。这是标准的蝶形（butterfly）归约。

**练习 3.** `top2_sum_gate` 为什么在归一化时用 `scores_wo_bias`（第 254、260 行）而不是带 bias 的分数？

> **答案。** 呼应 u5-l1 的结论：bias 只用来「决定选谁」，不参与「权重大小」。用带 bias 的分数做归一化会让 bias 渗透进最终权重，与参考实现 `topk_score_local = scores_wo_bias.gather(...)`（[torch/topk.py:L143](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L143)）不一致。

---

## 5. 综合实践

把本讲的三条线索（反复找最大、稳定并列、warp shuffle）合到一个任务里：

**任务：为 `topk_gate` 设计并解释一组「压力对拍」，并手推一个 kernel 改动的后果。**

1. **并列压力矩阵。** 写一个生成器，构造若干行分数，每行刻意设置不同数量的并列最大（0 个、1 对、3 连并列、全并列），用 `topk_gate` 与 `stable_topk` 对拍，断言每行都逐元素相等。把结果整理成一张表：并列情况 → 预期输出 → 实际输出。
2. **极限规模。** 用 `_EXPERT_CONFIGS` 里最大的 `(256, 8)`（见 [tests/moe/test_topk_gate.py:L14-L25](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_gate.py#L14-L25)）跑 benchmark，记录延迟与带宽，判断它是否接近显存带宽极限（提示：top-k 的有效数据量主要是读 `scores`）。
3. **改动推理（不上机）。** 假设你把 [topk_gate_kernel.py:L29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_gate_kernel.py#L29) 的 `'min'` 改成 `'max'`，同时把第 43 行的 `fill(+∞)` 改成 `fill(-∞)`，请推断：输出的「选取结果」是否会变？在什么输入下会变？为什么？

**预期：** 第 1 项所有行都对齐；第 2 项带宽应较高（带宽受限算子）；第 3 项结论是——结果在「无并列」时不变，但在「有并列」时会变成取**最大**下标（与 `stable_topk` 不一致），因为 `max` 归约器在命中候选里取了最大下标。这一题把 4.2 节「min 归约器为何做稳定并列」彻底吃透。

> 说明：第 1、2 项需要 SM90/SM100 GPU + TileLang 环境，若无则记为「**待本地验证**」，可先纯用 `stable_topk` 完成第 1 项的「预期输出」手算与第 3 项的推理。

## 6. 本讲小结

- `topk_gate` 用「反复找最大」做 top-k：每轮 `reduce_max` 求最大分，再用 `min` 归约器在「等于最大分的候选」里取最小下标，选中后置 `-inf` 剔除。
- 「并列取小下标」的稳定语义由 `alloc_reducer('min', replication='all')` 实现：非命中线程不更新（保持 `+∞`），`finalize_reducer` 跨线程 `min` 合并；与 PyTorch `stable_topk`（稳定降序排序）逐元素一致。
- `top2_sum_gate` 走另一条等价路线：每个 lane 本地持候选，用 5 轮 `shfl_xor` 蝶形归约合并，比较函数里显式写「等值取更小 idx」。
- `get_topk_group_idx` 用「计数排序 + `shfl_sync`」求每个组的稳定排名（`count_var`），排名即写入位置，天然升序；`i < lane_idx` 保证并列时下标小的组靠前。
- 关键 warp 原语：`shfl_sync(val, lane)` 按指定 lane 广播读取、`shfl_xor(val, mask)` 蝶形交换（5 轮覆盖 32 lane）、`sync_warp()` 束内同步、`warp_reduce_max/sum` 内置束归约。
- 两套选取风格（fragment + 内置 reduce / local + 手写 shuffle）语义等价，选择取决于是否需要 per-thread 细粒度控制：`topk_gate` 简单用 fragment，`top2_sum_gate` 因要复用 lane 的 `scores_local`、与 bias/打分交织，选了 local + shuffle。

## 7. 下一步学习建议

- **u5-l3（分组路由与 group counting）** 会进一步拆解 `get_topk_group_idx` 在完整路由中的位置，以及 `topk_sum_and_topk_group_idx`、`group_count` 如何配合派发。
- **u5-l4（融合派发 expand/reduce/mapping）** 讲路由结果如何变成 fused 布局喂给后续 GEMM，届时会用到本讲输出的 `(topk_idx, topk_weights)`。
- **u5-l5（权重归一化、TP 掩码、aux 负载与完整路由参考）** 会展开本讲只点到为止的 EP/TP 掩码、`normalize_weight`、`aux_fi`，并对照 `torch/topk.py` 把完整路由算法讲透——建议把它当作本讲的「完整版」回读。
- 若你对 warp shuffle 还想加深理解，可回头看 [tile_kernels/mhc/sinkhorn_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/mhc/sinkhorn_kernel.py)（u7-l3），里面有更多 reducer 与规约的用法。
