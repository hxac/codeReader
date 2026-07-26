# 分组路由与 group counting

## 1. 本讲目标

本讲承接 u5-l2，把 MoE 路由链路中的「分组粗筛」环节彻底拆透。u5-l2 给出了 `get_topk_group_idx` 的定位与高层直觉，本讲则逐行精读它的实现，并补上 u5-l2 没有展开的两个独立 kernel。

学完后你应该能够：

1. 说清「分组路由（group-limited expert routing）」要解决什么问题，以及为什么先按组粗筛、再在组内细选。
2. 逐行解释 `get_topk_group_idx` 宏：每个 lane 如何算出「自己这组的 top-k 之和」，又如何用 `T.shfl_sync` 的计数排序算出每组的稳定排名。
3. 用数学语言说明 `count_var` 与 `i < lane_idx` 这两个条件如何共同构成一个严格全序，从而保证「降序、并列取小下标」的稳定排序。
4. 读懂独立 kernel `topk_sum_and_topk_group_idx_kernel` 如何把这个宏单独封装成一个可调用算子，并与 `torch.topk(...).values.sum(-1)` 参考对拍。
5. 读懂 `group_count_kernel` 如何用「每块局部直方图 + 全局归约」统计每个专家收到的 token 数，并理解它在派发（dispatch）中的作用。

---

## 2. 前置知识

### 2.1 MoE 路由链路回顾

在前两讲（u5-l1、u5-l2）里，我们已经把路由链路的两步走通了：

- **打分**：把模型给出的原始分 `logits` 经 sigmoid / softmax / sqrtsoftplus 变成非负可比的 `scores`。
- **选取**：从 `scores` 里选出每个 token 的 top-k 专家（`topk_gate` 用反复 `reduce_max` + `alloc_reducer('min')`；`top2_sum_gate` 用 `shfl_xor` 蝶形归约）。

本讲聚焦的是 DeepSeek MoE 在「打分」与「组内细选」之间插入的一道**组级粗筛**。

### 2.2 为什么要分组粗筛

DeepSeek-MoE 提出 **group-limited expert routing**：把全部 `num_routed_experts` 个专家均分成 `num_groups` 组（每组 `num_experts_per_group = num_routed_experts // num_groups` 个专家），先按「组内 top-N 之和」选出最强的 `num_topk_groups` 个组，**再只在这几个组里**做最终的 top-k 细选。

这样做的好处是把细选的搜索空间从「全部专家」缩小到「少数组」，既控制了跨专家的负载，也让一个 warp 能装下整个组的候选。本讲要讲的 `get_topk_group_idx` 就是这道粗筛的核心。

### 2.3 你需要带的几块认知

- **warp 与 lane**：GPU 以 32 线程为一个 warp 协作；`thread_idx % 32` 是 lane 编号。本讲里「一个 lane 负责一个组」是关键设计。
- **`T.shfl_sync(var, i)`**：warp shuffle 原语，把 lane `i` 的 `var` 广播给当前 lane。这是「打听别的组分数」的唯一通信手段（见 u5-l2）。
- **稳定排序**：并列时按某个固定 tie-break（这里是下标小者优先）决定先后，保证输出可复现、可对拍。
- **共享内存 bank conflict**：32 个 bank，按行连续访问时容易撞 bank；用偏移（shift）可错开（见 u3-l1 转置 kernel 的 padding/swizzle）。

---

## 3. 本讲源码地图

本讲涉及三个文件，正好对应三个最小模块：

| 文件 | 作用 | 角色 |
| --- | --- | --- |
| [tile_kernels/moe/common.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py) | 定义 `@T.macro get_topk_group_idx` | 被复用的「组级粗筛」宏，是本讲主角 |
| [tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py) | 把上面的宏独立封装成算子 + wrapper | 单独做「只选组」的算子，便于测试与复用 |
| [tile_kernels/moe/group_count_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/group_count_kernel.py) | 统计每个专家/组收到的 token 数 | 路由完成后的派发计数 kernel |

配套的参考实现与测试：

- [tile_kernels/torch/topk.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py) 中的 `topk_sum_and_topk_group_idx` 与 `stable_topk`，是 `get_topk_group_idx` 的 PyTorch 对拍参考。
- [tile_kernels/torch/moe.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/moe.py) 中的 `group_count`，是 `group_count_kernel` 的 PyTorch 对拍参考。
- [tests/moe/test_topk_sum_and_topk_idx.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_topk_sum_and_topk_idx.py) 与 [tests/moe/test_group_count.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_group_count.py)，分别是两个算子的正确性 + benchmark 测试。

---

## 4. 核心概念与源码讲解

### 4.1 组级粗筛宏 `get_topk_group_idx`（moe/common）

#### 4.1.1 概念说明

`get_topk_group_idx` 是一个 `@T.macro`（见 u2-l1：宏是一段可被多个 kernel 内联复用的 TileLang 代码片段）。它解决一个很具体的问题：

> 给定一个 token 在所有专家上的分数 `scores`，把专家按 `num_groups` 均分，对每组计算「组内最大的 `num_topk_sum` 个分数之和」，再按这个和**稳定降序**选出最强的 `num_topk_groups` 个组的下标。

`num_topk_sum` 只支持 1 或 2：组分数要么是组内最大值（top-1），要么是组内最大两值之和（top-2 sum，DeepSeek 默认）。组分数记作 \( s_g \)（组 \(g\) 的分数）。

整个宏的精妙之处在于：**一个 warp 处理一个 token，一个 lane 负责一个组**。组数 `num_groups` 被约束为不超过 32（见下文 kernel 的 `assert num_groups <= 32`），所以一个 warp 恰好能并行算完全部组。

#### 4.1.2 核心流程

宏分三步，全程只用 warp shuffle，**不用 atomics、不做多趟共享内存排序**：

1. **算组分数**：lane \(g\)（若 \(g < \text{num\_groups}\)）只读自己那组 `num_experts_per_group` 个专家的分数，维护一个组内 top-1/top-2，得到 \(s_g\) 存进 `topk_sum_var`。
2. **计数排序求排名**：lane \(g\) 用 `T.shfl_sync` 依次广播每个 lane 的组分数，数一数「比我强的组有几个」，结果 `count_var` 就是自己的名次（0 = 最强）。
3. **按名次写入**：若名次 < `num_topk_groups`，把自己的组号 `lane_idx` 写进 `topk_group_idx_shared[token_idx, count_var]`。

因为每个组的名次唯一，写入位置不冲突，最终 `topk_group_idx_shared` 的前 `num_topk_groups` 格自然就是稳定降序的组号序列。下面用伪代码概括：

```
# 每个 lane g 代表组 g
if g < num_groups:
    扫描本组 num_experts_per_group 个分数 → 维护 (top1, top2)
    s_g = top1                        # num_topk_sum == 1
        = top1 + top2                 # num_topk_sum == 2

count_var = 0
for i in [0, num_groups):             # 计数排序
    s_i = shfl_sync(s_g', i)          # 取 lane i 的组分数
    if s_i > s_g  or  (s_i == s_g and i < g):
        count_var += 1                # lane i 排在我前面

if count_var < num_topk_groups:
    out[count_var] = g                # 把组号写进自己的名次槽
sync_warp()
```

#### 4.1.3 源码精读

先看宏签名与变量声明。`token_idx` 指出当前 warp 处理第几个 token，`lane_idx` 即组号；四个 `alloc_var` 分别维护组内 top-1、top-2、组分数、以及排名计数：

[tile_kernels/moe/common.py:L4-L22](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L4-L22) — 宏定义：声明 `scores_vec_local`（线程私有寄存器）与 `top1_var / top2_var / topk_sum_var / count_var`，初值分别是 \(-\infty,-\infty,-\infty,0\)。

**第一步：算组分数。** 只有 `lane_idx < num_groups` 的 lane 干活。它把本组的 `num_experts_per_group` 个分数分成 `num_vectorize_for_grouped_expert` 一批的小段，向量化读进 `scores_vec_local`，并维护 top-1/top-2：

[tile_kernels/moe/common.py:L25-L39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L25-L39) — 每个有效 lane 扫描自己那组的分数，用经典的「打擂台」更新 top-1/top-2，再用 `T.Select` 据是 top-1 还是 top-2 求和得到 `topk_sum_var`。注意第 30 行 `vec_idx = (i + lane_idx) % num_vec_experts_per_group`：本组内读取的起始列偏移随 lane 旋转，正是 u3-l1 见过的「shift to avoid bank conflict」技巧——让不同 lane 在同一迭代不撞同一组 bank。

**第二步：计数排序求排名。** 这是本讲的灵魂，也是标题里「比我大的组有几个」的由来：

[tile_kernels/moe/common.py:L41-L45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L41-L45) — 对每个组 `i`，`T.shfl_sync(topk_sum_var, i)` 把 lane `i` 的组分数广播给当前 lane；若它比我强（更大，或相等但下标更小），计数 `count_var += 1`。

**第三步：按名次写入。**

[tile_kernels/moe/common.py:L47-L52](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/common.py#L47-L52) — 若名次落在前 `num_topk_groups`，把组号 `lane_idx` 写进 `topk_group_idx_shared[token_idx, count_var]`，最后 `T.sync_warp()` 保证所有写回完成。

#### 4.1.4 稳定排序的数学保证（重点）

把组 \(g\) 的分数记作 \(s_g\)。第二步代码算出的 `count_var` 等价于：

\[
\text{rank}(g) \;=\; \bigl|\{\, i : s_i > s_g \,\}\bigr| \;+\; \bigl|\{\, i < g : s_i = s_g \,\}\bigr|
\]

也就是说，「排在我前面的组」=「严格比我大的全部组」+「与我并列但下标比我小的组」。我们断言这正是 \(g\) 在**稳定降序排序**中的位置（从 0 开始）。

- **降序**：分数越大，被它「压在下面」的组越少，\(\text{rank}\) 越小，槽位越靠前。
- **并列取小下标**：两个并列组 \(a < b\)（\(s_a=s_b\)）时，对 \(b\) 来说 \(a\) 满足 \(i<b\) 计入它的 rank，而对 \(a\) 来说 \(b\) 不满足 \(i<a\)，所以 \(\text{rank}(a)<\text{rank}(b)\)，即 \(a\) 排在前面。
- **名次唯一（构成排列）**：把每个组看成一个二元组 \((-s_g,\, g)\)，上述判据等价于对 \((-s_g, g)\) 做**字典序升序**比较——这构成一个严格全序。全序下每个元素的 rank（比它小的元素个数）唯一，取遍 \(0,1,\dots,\text{num\_groups}-1\)。因此把组号写进 `out[rank]` 不冲突，且恰好重建出稳定降序的组号序列。

> 小细节：循环里 `i` 取遍含自身的全部组。当 \(i=g\) 时，\(s_i>s_g\) 为假、\(s_i=s_s\) 且 \(i<g\) 也为假，所以自身不会把自己计入 rank，无需特判排除。

对照 PyTorch 参考，`stable_topk` 用的是 `torch.sort(..., descending=True, stable=True)`，其稳定语义正是「同值保持原（升）下标顺序」——与本宏的 `i < lane_idx` tie-break 完全一致，故可位精确对拍：

[tile_kernels/torch/topk.py:L8-L10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L8-L10) — `stable_topk` 用 `stable=True` 的降序排序后取前 `num_topk`，定义了对拍的「标准答案」。

#### 4.1.5 代码实践：手算一遍计数排序

- **实践目标**：用一个手算例子验证「排名 = 槽位」确实重建出稳定降序，并把代码里的 `count_var` 与 `i < lane_idx` 对上。
- **操作步骤**：
  1. 设 `num_groups = 4`，`num_topk_sum = 2`，`num_topk_groups = 2`。
  2. 假设已算出各组 top-2 之和 \(s = [3.0,\ 1.0,\ 3.0,\ 2.0]\)（lane 0、1、2、3）。注意 lane 0 与 lane 2 并列。
  3. 对每个 lane \(g\)，按公式算 `count_var`：
     - \(g=0\)：没有严格更大者，也没有 \(i<0\) 的并列者 → `count_var=0`。
     - \(g=1\)：\(s_0,s_2,s_3\) 都更大 → `count_var=3`。
     - \(g=2\)：\(s_0=3.0>s_2\) 不成立，但 \(s_0=s_2\) 且 \(0<2\) → 计 1；\(s_3=2.0<3.0\) 不计 → `count_var=1`。
     - \(g=3\)：\(s_0,s_2\) 更大 → `count_var=2`。
  4. 写入：`out[0]=0`、`out[1]=2`、`out[2]=3`、`out[3]=1`，取前 `num_topk_groups=2` 得 `[0, 2]`。
- **需要观察的现象**：并列的组 0、2 中，下标小的 0 排前；输出恰好是稳定降序的前两名。
- **预期结果**：`topk_group_idx = [0, 2]`，与 `stable_topk([3,1,3,2], 2)` 一致。
- **若想本地验证**：把上述 \(s\) 喂给 `tile_kernels.moe.topk_sum_and_topk_group_idx`（见 4.2）即可对拍。

#### 4.1.6 小练习与答案

**练习 1**：若把 `i < lane_idx` 改成 `i > lane_idx`，并列组的 tie-break 会变成什么？输出还稳定吗？

> **答案**：会变成「并列取**大**下标优先」。输出仍然确定（仍是某种全序），但不再与 `stable_topk`（升下标优先）一致，故与 PyTorch 参考对拍会失败。

**练习 2**：为什么宏里可以全程不用 `atomic_add`？

> **答案**：因为每个组的名次唯一，写入槽位 `out[count_var]` 天然互不冲突；排名是纯只读的「读别人分数 + 本地累加」，没有跨线程写竞争。

**练习 3**：第 30 行的 `(i + lane_idx) % num_vec_experts_per_group` 旋转若去掉（直接用 `i`），正确性会变吗？性能呢？

> **答案**：正确性不变（每个 lane 仍读遍本组全部专家），但不同 lane 在同一迭代会以相同列模式读共享内存，更易撞 bank conflict，性能下降。这是与 u3-l1 同源的 bank conflict 规避手段。

---

### 4.2 独立算子 `topk_sum_and_topk_group_idx_kernel`

#### 4.2.1 概念说明

`get_topk_group_idx` 是个**宏**，不能单独启动；它被内联进两个地方：一是 `top2_sum_gate_kernel`（完整路由流水线，见 u5-l2），二是本节的独立 kernel `topk_sum_and_topk_group_idx_kernel`。后者把「只选组」这件事单独封装成一个算子，方便单独测试、调参与复用——比如你想单独观察组级粗筛的延迟/带宽，或在没有细选的情况下只拿组号。

#### 4.2.2 核心流程

kernel 同样是「一个 warp 处理一个 token」：

1. 把当前 token 的 `scores` 从全局拷进 `scores_shared`（按 `num_threads=32` 对齐）。
2. 调用 `get_topk_group_idx` 宏，把组号写进 `topk_group_idx_shared`。
3. 由前 `num_topk_groups` 个 lane 把组号从 shared 写回全局 `group_topk_idx`（int64）。

#### 4.2.3 源码精读

构造函数与编译期参数。注意三个硬约束：`num_groups <= 32`（一个 warp 装得下）、`num_experts_per_group` 必须能被向量化宽度整除、`align(num_experts, 32)` 把专家数补到 32 的倍数以对齐共享内存：

[tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py:L18-L35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py#L18-L35) — 声明网格只用 `num_threads=32`、断言组数不超过 warp 大小、把 `num_vectorize_for_grouped_expert` 从 4 逐次减半到能整除 `num_experts_per_group`（向量化宽度自适应，与 u2-l3 的向量化循环呼应）。

prim_func 主体：拷 scores 到 shared，调宏，再回写。grid 是一维的 `num_tokens`，每个 block 恰一个 warp（`num_tokens_per_block = 32//32 = 1`）：

[tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py:L39-L67](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py#L39-L67) — 注意第 52 行 `T.copy(scores[pid*num_tokens_per_block, 0], scores_shared)` 用集体加载（u2-l2），第 55-63 行调用 4.1 的宏，第 65-66 行由 `lane_idx < num_topk_groups` 的 lane 回写 int64 组号。

wrapper 是用户真正入口（u2-l1 的「校验 → 分配输出 → 触发/命中 JIT → 按形参顺序启动」四步）。它要求输入是 **3D 连续 float32** `(num_tokens, num_groups, num_experts_per_group)`，内部 `view(num_tokens, -1)` 摊平后喂给 kernel；零规模守卫 `if num_tokens == 0: return` 也在 wrapper：

[tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py:L71-L102](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/topk_sum_and_topk_group_idx_kernel.py#L71-L102) — 校验 `dim()==3`、连续、float32，且 `num_topk_sum in (1,2)`、`num_topk_groups <= num_groups`；分配 int64 输出并启动。

PyTorch 参考只有两行，正好对应宏的两步：先按组求 top-`k` 之和，再稳定降序取前 `num_topk_groups`：

[tile_kernels/torch/topk.py:L13-L19](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py#L13-L19) — `scores.topk(num_group_sum_topk, dim=-1).values.sum(-1)` 算组分数，`stable_topk` 取组号，与 kernel 位精确对拍。

#### 4.2.4 代码实践：跑测试 + 造并列用例对拍

- **实践目标**：确认 kernel 与 PyTorch 参考在一般情况与并列情况下都位精确一致。
- **操作步骤**：
  1. 跑现成正确性测试（详见 u1-l2 的测试姿势）：
     ```bash
     pytest tests/moe/test_topk_sum_and_topk_idx.py -n 4
     ```
  2. 造一个能产生并列组分数的随机输入，单独对拍。示例代码（**示例代码**，非项目原有）：
     ```python
     import torch, tile_kernels
     from tile_kernels.torch import topk_sum_and_topk_group_idx as ref

     torch.manual_seed(0)
     num_tokens, num_groups, epg = 3, 4, 8   # 4 组，每组 8 专家
     # 故意让多组 top2 之和相等：把每组的两个大值设成同样大小
     scores = torch.zeros(num_tokens, num_groups, epg, dtype=torch.float32, device='cuda')
     scores[:, :, 0] = 5.0   # 每组最大值都 5
     scores[:, :, 1] = 3.0   # 每组次大值都 3 → 各组 top2 和都为 8，全部并列
     scores += 0.1 * torch.randn_like(scores)  # 加一点点噪声，部分组会并列

     got = tile_kernels.moe.topk_sum_and_topk_group_idx(scores, 2, 2)
     exp = ref(scores, 2, 2)
     print(got); print(exp)
     assert torch.equal(got, exp)
     ```
- **需要观察的现象**：当多个组的 top-2 和相等时，输出按组号升序选前 `num_topk_groups` 个（即并列取小下标）。
- **预期结果**：`got` 与 `exp` 完全相等（int64 位精确），断言通过。
- **待本地验证**：本环境若无 GPU，以上代码无法运行，请到带 SM90/SM100 的机器验证（见 u1-l1 的硬件依赖说明）。

#### 4.2.5 小练习与答案

**练习 1**：wrapper 为何要求输入是 3D `(num_tokens, num_groups, num_experts_per_group)`，而 kernel 形参却是 2D `(num_tokens, num_experts)`？

> **答案**：组结构只在 Python 校验与「组内均分」语义里有意义；GPU kernel 只需要线性内存与「每 `num_experts_per_group` 个为一组」的步长，所以 wrapper 用 `view(num_tokens, -1)` 摊平成 2D 喂给 kernel，组号边界由 `lane_idx * num_experts_per_group` 在宏里隐式确定。

**练习 2**：`num_vectorize_for_grouped_expert` 从 4 逐次减半到能整除 `num_experts_per_group`。若 `num_experts_per_group = 6`，最终取多少？

> **答案**：6 不能被 4 整除，减半到 2，6 % 2 == 0，故取 2。

---

### 4.3 派发计数 `group_count_kernel`

#### 4.3.1 概念说明

路由结束后，每个 token 拿到了自己选中的 `num_topk` 个专家下标 `topk_idx`（形状 `(num_tokens, num_topk)`，被 EP/TP 掩成 `-1` 的槽表示「不派发」）。在做真正的专家计算前，调度器需要知道**每个专家（或组）收到了多少 token**——这就是 `group_count`：它对 `topk_idx` 做一次直方图统计，输出长度 `num_groups` 的计数向量。

直方图统计在 GPU 上的经典痛点是**原子写竞争**：所有线程都要往少数几个桶里 `atomic_add`。本 kernel 用「每 block 一份局部直方图 + 末尾一次全局归约」来缓解竞争。

#### 4.3.2 核心流程

1. **网格规模随硬件伸缩**：`num_blocks = num_sms * 2`，每 block 128 线程；`num_sms` 来自 `get_num_sms()`（见 u1-l3、u10-l1）。
2. **每 block 一份局部直方图**：`out_shared`（长度对齐到 128）先 `T.clear` 清零。
3. **跨步扫描输入**：每个线程以 `global_thread_idx, +stride, +stride...` 的跨步方式遍历 `(token, slot)` 对，对有效下标（`>= 0`）做 `atomic_add(out_shared[expert_idx], 1)`——这里只竞争 block 内的 shared，不竞争全局。
4. **末尾归约**：block 内处理完后，由各线程把自己负责的、非零的局部桶用 `atomic_add` 累加进全局 `out`。

第 3 步的跨步循环把海量 (token, slot) 对分摊到 `num_blocks * num_threads` 个线程上；第 4 步把「每 block 一份」的局部结果汇成全局结果。

#### 4.3.3 源码精读

[tile_kernels/moe/group_count_kernel.py:L10-L29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/group_count_kernel.py#L10-L29) — 构造函数：`num_threads=128`、`num_blocks=num_sms*2`，把 `num_topk`（即 `topk_idx` 第二维）、`num_groups`、`num_sms` 都作为编译期参数特化；运行时符号 `num_tokens` 用 `T.dynamic` 声明（u2-l1）。

prim_func 主体——分配局部直方图、跨步扫描、末尾归约：

[tile_kernels/moe/group_count_kernel.py:L20-L45](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/group_count_kernel.py#L20-L45) — 几个关键点：
- 第 29 行 `align(num_groups, num_threads)`：局部直方图长度补到 128 的倍数，避免 bank conflict（与 u2-l2、u3-l1 同源）。
- 第 33 行 `T.serial(global_thread_idx, num_tokens, num_blocks*num_threads)`：TileLang 的跨步循环，起点是线程全局编号、步长是总线程数（见 u2-l3 的 `T.serial` 语义）。
- 第 36-37 行 `T.device_assert(-1 <= expert_idx < num_groups)` + `T.assume(expert_idx < num_groups)`：前者运行期断言（掩码后值域是 `[-1, num_groups)`），后者把上界告诉编译器辅助越界分析与向量化。
- 第 38-39 行：`>= 0` 才计数，跳过被 EP/TP 掩成 `-1` 的槽。
- 第 42-44 行：末尾把非零局部桶累加进全局 `out`，仅非零桶才写，减少无谓的全局原子。

wrapper 比较直接：校验 2D 连续、按 `group_idx.shape[1]`（即 `num_topk`）与 `num_groups`、`get_num_sms()` 特化 kernel，分配 `torch.zeros(num_groups, int32)` 并启动：

[tile_kernels/moe/group_count_kernel.py:L49-L69](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/group_count_kernel.py#L49-L69) — 注意输出用 `torch.zeros` 预置 0，因为 kernel 是「累加进 out」而非「写覆盖」；`TK_PRINT_KERNEL_SOURCE` 同 u1-l2。

PyTorch 参考同样跳过 `< 0` 后用 `scatter_add_` 做直方图，语义与 kernel 一致：

[tile_kernels/torch/moe.py:L27-L40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/moe.py#L27-L40) — `valid_idx = group_idx[group_idx >= 0]` 过滤 padding，`scatter_add_` 把每个有效下标 +1 累加进对应桶。

#### 4.3.4 代码实践：限制 SM 数观察计数正确性

- **实践目标**：验证 `group_count` 在不同 `num_sms` 下都与参考一致（grid 随 `num_sms` 变，但结果不变）。
- **操作步骤**：
  1. 跑现成测试（它内部会用 `generate_num_sms()` 遍历多个 `num_sms` 并 `set_num_sms` 后断言）：
     ```bash
     pytest tests/moe/test_group_count.py -n 4
     ```
  2. 手动限制 SM 数对拍。示例代码（**示例代码**）：
     ```python
     import torch, tile_kernels
     from tile_kernels.config import set_num_sms, get_device_num_sms, get_num_sms
     from tile_kernels.torch import group_count as ref

     topk_idx = torch.randint(0, 8, (1000, 6), dtype=torch.int64, device='cuda')
     # 随机掩掉一些槽为 -1，模拟 EP/TP padding
     topk_idx[torch.rand_like(topk_idx, dtype=torch.float32) < 0.2] = -1

     for n in [1, get_device_num_sms() - 20, get_device_num_sms()]:
         set_num_sms(n)
         assert torch.equal(tile_kernels.moe.group_count(topk_idx, 8), ref(topk_idx, 8))
     print('ok, sms range tried:', get_num_sms())
     ```
- **需要观察的现象**：无论 `num_sms` 是 1 还是满额，计数结果都与 `scatter_add_` 参考一致——因为「局部直方图 + 全局归约」与并行度无关。
- **预期结果**：三次断言全部通过。
- **待本地验证**：无 GPU 环境下不可运行，需到 SM90/SM100 机器执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 42-44 行只对 `out_shared[i] > 0` 的桶做全局 `atomic_add`？

> **答案**：值为 0 的桶对全局和没有贡献，跳过它们能减少无谓的全局原子写，降低全局内存竞争与流量。

**练习 2**：若把第 29 行的 `align(num_groups, num_threads)` 去掉，直接用 `num_groups` 作 `out_shared` 长度，正确性与性能分别会怎样？

> **答案**：正确性不变（只要 `num_groups` 仍够装所有桶），但当 `num_groups` 不是 32/128 的「友好」值时，多线程并发访问 `out_shared` 更易撞 bank conflict，性能下降。这是与 4.1 同源的 bank conflict 规避。

**练习 3**：kernel 用「每 block 局部直方图 + 末尾归约」，相比「所有线程直接对全局 `out` 做原子加」有什么收益？

> **答案**：把高竞争的全局原子限制在每个 block 内的 shared 上（shared 原子更快、竞争被 block 数量分摊），全局内存只剩每 block 至多 `num_groups` 次累加，显著降低全局原子竞争与带宽消耗。

---

## 5. 综合实践

把本讲三块串起来，手工走一遍「从 scores 到组号、再到组内 token 计数」的迷你链路（**示例代码**，重在理解，不强求运行）：

1. **造分**：生成 `scores (num_tokens, num_groups, num_experts_per_group)`，比如 `(5, 4, 8)`。
2. **选组**：调用 `tile_kernels.moe.topk_sum_and_topk_group_idx(scores, 2, 2)`，得到每个 token 选中的 2 个组 `(num_tokens, 2)`。
3. **（模拟细选）**：在本讲里我们不真做组内 top-k，而是直接把「选中的组号」当作要派发的目标，构造一个 `(num_tokens, 2)` 的 `group_idx` 张量。
4. **计数**：调用 `tile_kernels.moe.group_count(group_idx, num_groups)`，得到每个组被多少 token 选中。

```python
import torch, tile_kernels
from tile_kernels.torch import topk_sum_and_topk_group_idx as ref_select

torch.manual_seed(1)
num_tokens, num_groups, epg = 5, 4, 8
scores = torch.randn(num_tokens, num_groups, epg, dtype=torch.float32, device='cuda')

# 第 2 步：组级粗筛
group_idx = tile_kernels.moe.topk_sum_and_topk_group_idx(scores, 2, 2)
assert torch.equal(group_idx, ref_select(scores, 2, 2))   # 对拍

# 第 4 步：统计每个组被选中的次数（派发计数）
counts = tile_kernels.moe.group_count(group_idx, num_groups)
print('每 token 选中的组:\n', group_idx.cpu())
print('每组被选次数:', counts.cpu())
assert int(counts.sum().item()) == num_tokens * 2          # 每个 token 选 2 个组
```

**需要观察与解释的现象**：

- `group_idx` 每行是稳定降序的组号（并列取小下标），可对照 4.1.5 的手算逻辑核对。
- `counts.sum()` 必须等于 `num_tokens * num_topk`（这里 `num_topk=2`），因为每个 token 恰好贡献 `num_topk` 个有效（非 -1）选择。
- 把 4.1 的「计数排序求排名」与 4.3 的「直方图计数」放在一起对比：前者是「每个元素自己算自己在全序里的位置」，后者是「把元素归桶统计频次」——两种截然不同的「计数」思路，分别服务于排序与派发。

**待本地验证**：以上需在带 GPU 的环境运行；无 GPU 时可作为源码阅读练习，逐行把 `group_idx` 的来历对应到 4.1.3 的源码行号。

---

## 6. 本讲小结

- `get_topk_group_idx` 是 DeepSeek 式「组级粗筛」的 `@T.macro`：一个 warp 处理一个 token、一个 lane 负责一个组，组分数 = 组内 top-`num_topk_sum` 之和。
- 排名用**计数排序**完成：lane \(g\) 经 `T.shfl_sync` 广播比较，数「比我强的组」，`count_var` 即名次；槽位 `[0, num_topk_groups)` 装稳定降序的组号。
- 稳定性来自严格全序：判据等价于对 \((-s_g, g)\) 字典序升序，`count_var` 与 `i < lane_idx` 共同保证「降序、并列取小下标」，与 `torch.sort(stable=True)` 位精确对拍。
- `(i + lane_idx) % num_vec_experts_per_group` 的旋转、`align(num_groups, num_threads)` 的补齐，都是与 u3-l1 同源的 bank conflict 规避手段。
- `topk_sum_and_topk_group_idx_kernel` 把宏独立封装成「只选组」的算子（3D 输入 `view` 成 2D 喂 kernel），对应 PyTorch 参考的两行 `topk().values.sum(-1)` + `stable_topk`。
- `group_count_kernel` 用「每 block 局部直方图 + 末尾全局归约」做派发计数，`num_blocks = num_sms*2` 随硬件伸缩，结果与并行度无关；跳过 `>= 0` 的 padding 槽与零桶以减少竞争。

---

## 7. 下一步学习建议

- **横向**：阅读 [tile_kernels/moe/top2_sum_gate_kernel.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/moe/top2_sum_gate_kernel.py) 第 198-230 行，看 `get_topk_group_idx` 选出组号后，如何「把未选中组的专家分数置 \(-\infty\)、再在组内细选 top-k」——这正是粗筛与细选的衔接处，对应 [tile_kernels/torch/topk.py](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/topk.py) 第 131-143 行的 `group_mask` + `masked_fill(-inf)`。
- **纵向（下一讲 u5-l4）**：进入「融合派发」布局，看 `expand_to_fused` / `reduce_fused` / `get_fused_mapping` 如何把路由结果重排成利于连续访存的 fused 布局；`group_count` 算出的计数正是派发缓冲定大小的依据。
- **延伸**：若对计数排序的全序证明意犹未尽，可对照 u2-l3 的 `alloc_reducer('min', replication='all')`——那是「选取」环节的另一种稳定 tie-break 手段，与本讲的计数排序殊途同归。
