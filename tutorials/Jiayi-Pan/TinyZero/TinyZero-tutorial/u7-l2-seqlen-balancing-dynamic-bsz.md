# 序列长度均衡与动态 batching

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚在 veRL 的数据并行（DP）训练里，**为什么样本长度不均会让某些 GPU 成为拖慢全队的「木桶短板」**。
- 读懂 `seqlen_balancing.py` 中的 **Karmarkar-Karp 最大差分法**（`karmarkar_karp`），并手算一个小例子。
- 看懂公开入口 `get_seqlen_balanced_partitions` 如何调用上面的算法并做校验。
- 读懂 `RayPPOTrainer._balance_batch` 如何在 driver 进程上重排 batch，让每个 dp rank 分到的有效 token 数尽量相等（`equal_size=True`）。
- 读懂 `rearrange_micro_batches` 如何按 **token 预算** 而非固定条数切分 micro batch（`use_dynamic_bsz`），并解释它为什么能提升 GPU 利用率。

本讲承接 u4-l3（`fit()` 训练主循环）。在那里你已经看到一个 step 里会先 `_balance_batch` 再设 `global_token_num`；本讲就把这两个动作背后的算法拆开。

## 2. 前置知识

- **数据并行（Data Parallel, DP）**：把一个大 batch 平均切成 `world_size` 份，每张 GPU（每个 dp rank）各算一份，最后再合并梯度。veRL 里这一刀切由 dispatch 的 `DP_COMPUTE_PROTO` 完成（见 u3-l3），它内部调用 `DataProto.chunk(world_size)`，**要求每份样本数相等**。
- **同步训练**：PPO 一个 step 里所有 rank 算完才进入下一阶段（all-reduce 梯度、合并结果）。这意味着**最慢的那个 rank 决定了整步耗时**——这就是「木桶效应」。
- **有效长度（valid seqlen）**：一条样本的 `attention_mask` 沿序列维求和，就是去掉左右填充后的真实 token 数（见 u2-l3）。同一个 padded batch 里，不同样本的有效长度可能差很多（countdown 任务的回答有长有短）。
- **计算量与长度的关系**：Transformer 自注意力是 \(O(n^2)\)，所以一条长样本比一条短样本贵得多。**按样本数平均 ≠ 按计算量平均**。
- **本讲要解决的问题**：能否在切分之前**重排样本顺序**，让每个 rank / 每个 micro batch 分到的「总有效 token 数」尽量接近，从而消除木桶短板？

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [verl/utils/seqlen_balancing.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py) | 全部均衡算法的实现：`karmarkar_karp`、`get_seqlen_balanced_partitions`、`rearrange_micro_batches`，以及辅助的 `greedy_partition`、`log_seqlen_unbalance`、`ceildiv`、`get_reverse_idx`。 |
| [verl/trainer/ppo/ray_trainer.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py) | `RayPPOTrainer._balance_batch` 在 driver 进程上做 dp rank 级均衡；`fit()` 里调用它的位置。 |
| [verl/workers/actor/dp_actor.py](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py) | `compute_log_prob` / `update_policy` 中 `use_dynamic_bsz` 分支，调用 `rearrange_micro_batches`。 |
| [verl/trainer/config/ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml) | `use_dynamic_bsz`、`ppo_max_token_len_per_gpu`、`ppo_micro_batch_size` 等开关。 |

## 4. 核心概念与源码讲解

### 4.1 问题背景：序列长度的「木桶效应」

假设一个 batch 有 8 条样本，有效长度分别是

```
[10, 2, 9, 3, 8, 4, 7, 5]   总和 = 48
```

用 2 个 dp rank，最朴素的「顺序切分」是前 4 条给 rank0、后 4 条给 rank1：

| | rank0 | rank1 |
| --- | --- | --- |
| 样本 | 10,2,9,3 | 8,4,7,5 |
| 总 token | 24 | 24 |

这里恰好平衡。但换一组长度 `[10, 9, 8, 7, 5, 4, 3, 2]`，顺序切就变成 rank0=34、rank1=14，rank0 算 2.4 倍的活，整步被它拖死。

**核心观察**：这是一个经典的**多路数字分区问题（multiway number partitioning）**——给定一组数，把它们分成 k 组，使各组的「和」尽量相等。该问题是 NP-hard，但有很好的启发式近似算法。veRL 选用的是 **Karmarkar-Karp 最大差分法（Largest Differencing Method）**。

### 4.2 模块一：Karmarkar-Karp 最大差分法（`karmarkar_karp`）

#### 4.2.1 概念说明

`karmarkar_karp` 把「让 k 组的和尽量相等」转化成「尽量消除组间差值」。它的核心直觉来自最简单的 k=2 情形：

> 两个最大的数，与其放在同一组（拉开差距），不如**分到不同组**——用它们的**差值**代替这两个数继续参与后续配对。

推广到 k 组时，veRL 用一个**优先队列（堆）**来管理「部分分配方案（State）」，每次取出**当前差距最大的两个方案**合并，合并时让「大组配小组」以抵消差距，直到只剩一个方案。

函数签名（[seqlen_balancing.py:L25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L25)）：

```python
def karmarkar_karp(seqlen_list: List[int], k_partitions: int, equal_size: bool):
```

两个关键参数：

- `equal_size=True`：要求**每组样本数必须相等**（用于 dp rank 切分，因为 dispatch 的 `chunk` 要求等分）。
- `equal_size=False`：只关心「和」是否均衡，**每组样本数可变**（用于 micro batch 动态打包）。

#### 4.2.2 核心流程

算法由两个内部类 `Set`（一组样本，记录成员与总和）和 `State`（k 个 `Set` 组成的一个完整分配方案）协作。流程如下（伪代码）：

```
1. sorted_seqlen_list = 把 (seqlen, 原始idx) 按长度升序排序
2. 初始化优先队列 states_pq：
     if equal_size:
         assert len % k == 0
         每 k 个连续样本打包成一个初始 State（样本 i 进 set_i）
     else:
         每个样本各自成一个初始 State（只占 set_0，其余空）
3. while 队列里 State 数 > 1:
       弹出差距(spread)最大的两个 state0, state1
       state0.merge(state1):  把 state1 的「最大组」并进 state0 的「最小组」，
                              反之亦然（大配小，抵消差距）
       把合并后的 state0 重新入队
4. return 最终 State 的分组（每组一个原始 idx 列表）
```

其中：

- **spread（差距）** = 最大组的和 − 最小组的和（[seqlen_balancing.py:L77-L79](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L77-L79)）。
- **堆序**：用 Python 最小堆 + 反向 `__lt__` 实现「差距最大的先弹出」（[seqlen_balancing.py:L81-L87](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L81-L87)）。
- **merge 的「大配小」**（[seqlen_balancing.py:L72-L75](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L72-L75)）：`sets` 始终按和降序排列，合并时把 `self.sets[i]` 与 `other.sets[k-1-i]` 配对，即自己的第 i 大配对方的第 i 小。

数学上，KK 算法对 k=2 的分区能给出比贪心更好的近似（最终差值更小）；veRL 用它来做 RL 训练里的负载均衡，是一次「用经典算法解决工程问题」的好例子。

#### 4.2.3 源码精读

**初始化与堆循环**（[seqlen_balancing.py:L103-L130](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L103-L130)）：

```python
sorted_seqlen_list = sorted([(seqlen, i) for i, seqlen in enumerate(seqlen_list)])
states_pq = []
if equal_size:
    assert len(seqlen_list) % k_partitions == 0
    for offset in range(0, len(sorted_seqlen_list), k_partitions):
        items = []
        for i in range(k_partitions):
            seqlen, idx = sorted_seqlen_list[offset + i]
            items.append((idx, seqlen))
        heapq.heappush(states_pq, State(items=items, k=k_partitions))
else:
    for seqlen, idx in sorted_seqlen_list:
        heapq.heappush(states_pq, State(items=[(idx, seqlen)], k=k_partitions))

while len(states_pq) > 1:
    state0 = heapq.heappop(states_pq)
    state1 = heapq.heappop(states_pq)
    state0.merge(state1)
    heapq.heappush(states_pq, state0)

final_state = states_pq[0]
partitions = final_state.get_partitions()
```

中文说明：`equal_size=True` 时先按长度排序、每 k 个打包成一个初始 `State`（这样保证最终每组样本数相等）；`equal_size=False` 时每个样本独立成一个 `State`。然后反复「弹两个、合并、入队」直到只剩一个方案。

注意：文件里还有一个 [`greedy_partition`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L133-L149)（贪心：每次把当前数放进「和最小的组」），但**默认未被使用**——公开入口 `get_seqlen_balanced_partitions` 调的是 `karmarkar_karp`。读源码时不要被它误导。

#### 4.2.4 代码实践

**实践目标**：用一个最小例子手动跑一遍堆循环，建立对「大配小」的直觉。

**操作步骤**：取 `seqlen_list = [8, 7, 5, 4]`，`k_partitions = 2`，`equal_size = False`。

1. 排序得 `[(4,idx3),(5,idx2),(7,idx1),(8,idx0)]`，各成一个 `State`，spread 分别为 4、5、7、8。
2. 堆弹出 spread 最大的两个：(idx0,8) 和 (idx1,7)。合并「大配小」→ `{8}` 与 `{7}` 分到两组，新 spread = 1。
3. 再弹出 (idx2,5) 和 (idx3,4)，合并 → `{5}` 与 `{4}` 分到两组，新 spread = 1。
4. 最后两个 State 合并：`{8}` 配 `{4}`、`{7}` 配 `{5}` → 两组和均为 12，spread = 0。

**需要观察的现象**：最终分组为 `{idx0, idx3}` 与 `{idx1, idx2}`，即样本长度 `{8,4}` 一组、`{7,5}` 一组。

**预期结果**：两组总和均为 12（理想值 = 48/2... 实际总和 24/2 = 12），达到完美均衡。而顺序切分 `{8,7}` vs `{5,4}` = 15 vs 9，差距 6。这正是 KK 的价值。

> 如果无法本地运行，可对照上面的手算步骤验证理解（本例为「待本地验证」的纯手算练习）。

#### 4.2.5 小练习与答案

**练习 1**：为什么堆要用「差距最大的先弹出」，而不是「差距最小的先弹出」？

**参考答案**：KK 的策略是**优先处理最不平衡的部分**，把两个最不平衡的方案合并（大配小）能最大化抵消差距；若先合并已平衡的方案，剩下的极端值会集中到一起，最终差距反而更大。

**练习 2**：`equal_size=True` 时，为什么要先 `assert len(seqlen_list) % k_partitions == 0`？

**参考答案**：因为每 k 个样本打包成一个初始 `State`、最终每组恰好分到一个，要求样本总数能被 k 整除；这也对应 dispatch `chunk(world_size)` 要求等分的约束。

### 4.3 模块二：分区入口与校验（`get_seqlen_balanced_partitions`）

#### 4.3.1 概念说明

`get_seqlen_balanced_partitions` 是对外公开的入口（[seqlen_balancing.py:L152-L183](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L152-L183)）。它本身不做算法，只做两件事：调用 `karmarkar_karp`，再对结果做**完整性校验 + 组内排序**。所有上层（`_balance_batch`、`rearrange_micro_batches`）都只认这个入口。

#### 4.3.2 核心流程

```
get_seqlen_balanced_partitions(seqlen_list, k_partitions, equal_size):
    1. assert len(seqlen_list) >= k_partitions        # 至少够分
    2. partitions = karmarkar_karp(...)                # 算法主体
    3. _check_and_sort_partitions(partitions):         # 校验
         - 分组数 == k_partitions
         - 每组非空
         - 所有原始 idx 恰好出现一次（无遗漏无重复）
         - 每组内部 idx 升序排列
    4. return 排好序的 partitions
```

#### 4.3.3 源码精读

**入口与校验**（[seqlen_balancing.py:L152-L183](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L152-L183)）：

```python
def get_seqlen_balanced_partitions(seqlen_list, k_partitions, equal_size):
    assert len(seqlen_list) >= k_partitions

    def _check_and_sort_partitions(partitions):
        assert len(partitions) == k_partitions
        seen_idx = set()
        sorted_partitions = [None] * k_partitions
        for i, partition in enumerate(partitions):
            assert len(partition) > 0
            for idx in partition:
                seen_idx.add(idx)
            sorted_partitions[i] = sorted(partition)
        assert seen_idx == set(range(len(seqlen_list)))   # 每个 idx 恰好一次
        return sorted_partitions

    partitions = karmarkar_karp(seqlen_list=seqlen_list,
                                k_partitions=k_partitions, equal_size=equal_size)
    return _check_and_sort_partitions(partitions)
```

中文说明：`seen_idx == set(range(len(seqlen_list)))` 这一行最关键——它保证**每条样本都被分到且只被分到一个组**，不会丢样本、不会重复。这是后续「按 idx 重排 batch」正确性的前提。

#### 4.3.4 代码实践

**实践目标**：用一个稍大的例子跑通入口函数，观察 `equal_size` 的差异。

**操作步骤**（示例代码，可在本地 Python 里运行）：

```python
# 示例代码：非项目原有代码，仅供理解
from verl.utils.seqlen_balancing import get_seqlen_balanced_partitions

seqlen_list = [10, 9, 8, 7, 5, 4, 3, 2]   # 8 条样本

# (a) dp rank 用：equal_size=True，4 组每组 2 条
p_eq = get_seqlen_balanced_partitions(seqlen_list, k_partitions=4, equal_size=True)
print("equal_size=True :", p_eq)
print("各组总和:", [sum(seqlen_list[i] for i in g) for g in p_eq])

# (b) micro batch 用：equal_size=False，2 组不限条数
p_var = get_seqlen_balanced_partitions(seqlen_list, k_partitions=2, equal_size=False)
print("equal_size=False:", p_var)
print("各组总和:", [sum(seqlen_list[i] for i in g) for g in p_var])
```

**需要观察的现象**：两种模式下各组总和都接近理想值（总和 48，(a) 每组理想 12，(b) 每组理想 24）；但 `(a)` 每组样本数相同、`(b)` 每组样本数可能不同。

**预期结果**：`(a)` 每组恰好 2 条、和接近 12；`(b)` 每组和接近 24、条数可不等。**待本地验证**具体 idx 分布。

#### 4.3.5 小练习与答案

**练习**：如果 `seen_idx == set(range(len(seqlen_list)))` 这个断言失败，最可能说明 `karmarkar_karp` 出了什么问题？

**参考答案**：说明某个样本被分配到了多个组（重复）或没有任何组接收（遗漏），即分区不构成原集合的一个划分。这会直接导致后续按 idx 重排 batch 时样本错位，必须当作 bug 处理。

### 4.4 模块三：driver 侧的 dp rank 均衡（`RayPPOTrainer._balance_batch`）

#### 4.4.1 概念说明

`_balance_batch` 在 **driver 进程**上运行（不是 worker），目的是：在 batch 被 dispatch 切分到各 dp rank **之前**，重排样本顺序，让每个 rank 分到的**总有效 token 数尽量相等**。它是 u4-l3 里 `fit()` 主循环的一环，紧接在 `generate_sequences` 和 `repeat/union` 之后。

它用的是 `equal_size=True`，因为下游 dispatch 的 `DP_COMPUTE_PROTO` 会做 `chunk(world_size)`——**只能等分**。

#### 4.4.2 核心流程

```
_balance_batch(batch, metrics):
    1. 用 attention_mask 求和得到每条样本的有效长度 global_seqlen_lst  # 长度 = train_batch_size
    2. world_size = actor_rollout_wg.world_size                          # dp rank 数
    3. partitions = get_seqlen_balanced_partitions(
           global_seqlen_lst, k_partitions=world_size, equal_size=True)
    4. global_idx = 把 partitions 展平成一个重排索引
    5. batch.reorder(global_idx)            # 原地重排（见 u3-l1 的 reorder）
    6. log_seqlen_unbalance(...)            # 记录均衡前后的统计指标
```

重排后，dispatch 的 `chunk(world_size)` 会**顺序地**把前 `1/world_size` 条给 rank0、次 `1/world_size` 给 rank1……由于我们是按 KK 分组后排好的，每个连续段的「总 token 数」就近似相等了。

#### 4.4.3 源码精读

**`_balance_batch` 全貌**（[ray_trainer.py:L530-L545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L530-L545)）：

```python
def _balance_batch(self, batch: DataProto, metrics, logging_prefix='global_seqlen'):
    """Reorder the data on single controller such that each dp rank gets similar total tokens"""
    attention_mask = batch.batch['attention_mask']
    batch_size = attention_mask.shape[0]
    global_seqlen_lst = batch.batch['attention_mask'].view(batch_size, -1).sum(-1).tolist()
    world_size = self.actor_rollout_wg.world_size
    global_partition_lst = get_seqlen_balanced_partitions(global_seqlen_lst,
                                                          k_partitions=world_size,
                                                          equal_size=True)
    # reorder based on index. The data will be automatically equally partitioned by dispatch function
    global_idx = torch.tensor([j for partition in global_partition_lst for j in partition])
    batch.reorder(global_idx)
    global_balance_stats = log_seqlen_unbalance(seqlen_list=global_seqlen_lst,
                                                partitions=global_partition_lst,
                                                prefix=logging_prefix)
    metrics.update(global_balance_stats)
```

中文说明：

- `global_seqlen_lst`：每条样本的有效 token 数（prompt+response 的真实长度）。
- `equal_size=True`：对应 dispatch 等分约束。
- `global_idx = [j for partition in ... for j in partition]`：把 k 个分组的 idx **首尾相接**展平，作为新的样本顺序。这样 `chunk(world_size)` 顺序切分时，每段正好是一个 KK 分组。

**`fit()` 里的调用位置**（[ray_trainer.py:L597-L603](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L597-L603)）：

```python
# balance the number of valid tokens on each dp rank.
# Note that this breaks the order of data inside the batch.
# Please take care when you implement group based adv computation such as GRPO and rloo
self._balance_batch(batch, metrics=metrics)

# compute global valid tokens
batch.meta_info['global_token_num'] = torch.sum(batch.batch['attention_mask'], dim=-1).tolist()
```

中文说明：注释点出一个**重要副作用**——`_balance_batch` **打乱了 batch 内的样本顺序**。这对 GRPO/RLOO 这类「同 prompt 多采样分组」的算法是个坑：分组必须靠 `uid`（在 repeat 之前赋值，见 u5-l5）来重新定位，而不能依赖样本相邻。紧接着设置的 `global_token_num` 把每条样本的有效长度记进 `meta_info`，供后续 worker 与指标使用。

**指标**：[`log_seqlen_unbalance`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L186-L217) 会输出均衡前（顺序切）与均衡后（KK 切）的 min/max/minmax_diff/mean，是诊断负载是否均衡的关键日志。

#### 4.4.4 代码实践

**实践目标**：理解 `equal_size=True` 为何是 `_balance_batch` 的硬性选择。

**操作步骤**（源码阅读型实践）：

1. 打开 [ray_trainer.py:L530-L545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L530-L545)，确认 `k_partitions=world_size`、`equal_size=True`。
2. 回顾 u3-3：`update_actor` / `generate_sequences` 的 dispatch 模式是 `DP_COMPUTE_PROTO`，其 `dispatch_fn` 会调用 `DataProto.chunk(world_size)`。

**需要观察的现象**：`chunk` 只能等分（见 u3-l1 的 `chunk` 说明）。

**预期结果**：因此 `_balance_batch` 必须保证每个分组样本数相等——若改用 `equal_size=False`，某些 rank 会分到不同条数，`chunk` 直接报错或数据错位。结论：**`equal_size=True` 不是可选项，而是被 dispatch 机制强制的**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_balance_batch` 要在 driver 上做，而不是在每个 worker 上各做各的？

**参考答案**：因为只有 driver 握有**完整**的 batch（worker 只拿到切分后的片段）。均衡的前提是看到全体样本的长度分布，必须在切分之前、在 driver 上一次性完成重排。

**练习 2**：`_balance_batch` 之后，GRPO 靠什么把「同一个 prompt 的 n 条回答」重新归组？

**参考答案**：靠 `uid`。`uid` 在 `repeat` 之前赋值（同 prompt 的 n 条共享一个 uid），即使 `_balance_batch` 打乱了样本顺序，GRPO 的 `compute_grpo_outcome_advantage` 仍能按 uid 重新分组（见 u5-l5）。

### 4.5 模块四：动态 batching（`rearrange_micro_batches`）

#### 4.5.1 概念说明

到这里为止，我们只解决了「rank 之间」的均衡。但在**单个 rank 内部**，一次更新还要把数据切成多个 micro batch（控制单次显存，见 u6-l2）。veRL 提供两种切法：

- **固定 batching（默认）**：`batch.split(ppo_micro_batch_size)`，每个 micro batch **固定条数**。
- **动态 batching（`use_dynamic_bsz=True`）**：`rearrange_micro_batches`，每个 micro batch **按 token 预算** `max_token_len` 装填，条数可变。

后者就是本模块主角。它复用同一个 `get_seqlen_balanced_partitions`（这次 `equal_size=False`），把样本「装箱」成若干 token 负载均衡的 micro batch。

#### 4.5.2 核心流程

```
rearrange_micro_batches(batch, max_token_len, dp_group=None):
    1. max_seq_len = batch 的 padded 序列长度
       assert max_token_len >= max_seq_len            # 至少装得下一条最长样本
    2. seq_len_effective = 每条样本有效 token 数        # attention_mask.sum(dim=1)
       total_seqlen = 全部有效 token 总和
    3. num_micro_batches = ceildiv(total_seqlen, max_token_len)
    4. 若在分布式环境：all_reduce(MAX, group=dp_group)
       → 让同一 dp 组内所有 rank 切出「相同数量」的 micro batch（同步）
    5. micro_bsz_idx = get_seqlen_balanced_partitions(
           seq_len_effective, num_micro_batches, equal_size=False)
    6. 按 micro_bsz_idx 把 batch 切成若干 micro batch（条数可变）返回
```

micro batch 数量的核心公式（向上取整除法）：

\[
\text{num\_micro\_batches} = \left\lceil \frac{\sum_i \text{seqlen}_i}{\text{max\_token\_len}} \right\rceil
\]

其中 [`ceildiv`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L220-L221) 用 `-(a // -b)` 实现正整数向上取整。

#### 4.5.3 源码精读

**`rearrange_micro_batches` 主体**（[seqlen_balancing.py:L224-L256](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L224-L256)）：

```python
def rearrange_micro_batches(batch: TensorDict, max_token_len, dp_group=None):
    max_seq_len = batch['attention_mask'].shape[-1]
    assert max_token_len >= max_seq_len, \
        f'max_token_len must be greater than the sequence length. Got {max_token_len=} and {max_seq_len=}'

    seq_len_effective: torch.Tensor = batch['attention_mask'].sum(dim=1)
    total_seqlen = seq_len_effective.sum().item()
    num_micro_batches = ceildiv(total_seqlen, max_token_len)
    if dist.is_initialized():
        num_micro_batches = torch.tensor([num_micro_batches], device='cuda')
        dist.all_reduce(num_micro_batches, op=dist.ReduceOp.MAX, group=dp_group)
        num_micro_batches = num_micro_batches.cpu().item()

    seq_len_effective = seq_len_effective.tolist()
    assert num_micro_batches <= len(seq_len_effective)

    micro_bsz_idx = get_seqlen_balanced_partitions(seq_len_effective, num_micro_batches, equal_size=False)

    micro_batches = []
    for partition in micro_bsz_idx:
        curr_micro_batch = []
        for idx in partition:
            curr_micro_batch.append(batch[idx:idx + 1])
        curr_micro_batch = torch.cat(curr_micro_batch)
        micro_batches.append(curr_micro_batch)
    return micro_batches, micro_bsz_idx
```

中文说明：

- `assert max_token_len >= max_seq_len`：一个 micro batch 至少要装得下**单条最长样本**，否则无解。
- `all_reduce(MAX)`：同一 dp 组内不同 rank 拿到的数据不同、算出的 `num_micro_batches` 可能不同；取 MAX 保证大家迭代**相同次数**，否则会在集合通信处死锁。
- 返回 `micro_bsz_idx` 是为了调用方能**还原顺序**（见下）。

**调用方 1：actor 的 `compute_log_prob`**（[dp_actor.py:L181-L199](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L181-L199)）：

```python
if use_dynamic_bsz:
    max_token_len = data.meta_info['max_token_len'] * self.ulysses_sequence_parallel_size
    micro_batches, indices = rearrange_micro_batches(batch=batch, max_token_len=max_token_len)
else:
    micro_batches = batch.split(micro_batch_size)
log_probs_lst = []
for micro_batch in micro_batches:
    with torch.no_grad():
        _, log_probs = self._forward_micro_batch(micro_batch, temperature=temperature)
    log_probs_lst.append(log_probs)
log_probs = torch.concat(log_probs_lst, dim=0)

if use_dynamic_bsz:
    indices = list(itertools.chain.from_iterable(indices))
    revert_indices = torch.tensor(get_reverse_idx(indices), dtype=torch.long)
    log_probs = log_probs[revert_indices]      # 还原成原顺序
```

中文说明：动态 batching 会**打乱样本顺序**来装箱，所以算完 log_prob 后必须用 [`get_reverse_idx`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L259-L265) 把结果按原 idx 顺序还原；固定 batching 用 `split` 不打乱，所以无需还原。

**调用方 2：actor 的 `update_policy`**（[dp_actor.py:L224-L229](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L224-L229)）：同样在 `use_dynamic_bsz` 分支用 `ppo_max_token_len_per_gpu` 调用 `rearrange_micro_batches`。

**配置项**（[ppo_trainer.yaml:L23-L26](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L23-L26)）：

```yaml
actor:
  ppo_micro_batch_size: 64                 # 固定 batching 时的条数
  use_dynamic_bsz: False                   # 默认关闭
  ppo_max_token_len_per_gpu: 16384         # 动态 batching 时的 token 预算
```

注意：yaml 里 `critic`、`rollout`、`ref` 的 `*_use_dynamic_bsz` 与 `*_max_token_len_per_gpu` 都通过变量插值 `${actor_rollout_ref.actor.use_dynamic_bsz}` 跟随 actor 的设置（见 u1-l4 的变量插值），保证各角色一致。

#### 4.5.4 代码实践

**实践目标**：解释 `use_dynamic_bsz=True` 为什么能提升 GPU 利用率。

**操作步骤**（推理型实践）：

1. 设想一个 mini batch 有 4 条样本，有效长度 `[100, 100, 100, 1000]`，`max_token_len = 1200`，固定 `micro_batch_size = 2`。
2. **固定 batching**：切成 2 个 micro batch，每个 2 条。分组可能是 `{100,100}` 和 `{100,1000}`——后者因为含 1000 长样本，padded 到 1000，两个 micro batch 计算量严重不均，且短样本被大量 padding 浪费算力。
3. **动态 batching**：`total_seqlen = 1300`，`num_micro_batches = ceil(1300/1200) = 2`，KK 均衡后尽量让两个 micro batch 的总 token 接近 650，且每个 micro batch 都尽量贴近 `max_token_len` 预算。
4. 关键：动态 batching 在样本普遍较短时，会在一个 micro batch 里**塞进更多条**（直到接近 `max_token_len`），让 GPU 每次都「吃饱」。

**需要观察的现象 / 预期结果**：

- 固定 batching 的计算量由「该 micro batch 里最长样本」主导，短样本带来 padding 浪费，且各 micro batch 负载不均；
- 动态 batching 让**每个 micro batch 的总 token 数都接近预算 `max_token_len`**，从而：
  1. **token 吞吐稳定在高位**（GPU 持续饱和），而不是固定条数下「短样本多时欠载、长样本多时可能 OOM」；
  2. **减少 padding 浪费**（短样本成组、长样本成组，组内长度更接近）；
  3. **天然适配长度方差大的 RL 数据**（countdown 回答长短悬殊）。

所以一句话：**动态 batching 把「按条数切」换成「按 token 预算切」，让每张 GPU 每个 micro batch 都处理接近满载的 token 数，从而提升利用率。**

#### 4.5.5 小练习与答案

**练习 1**：为什么 `rearrange_micro_batches` 里要先 `all_reduce(MAX)` 统一 `num_micro_batches`？

**参考答案**：同一 dp 组内各 rank 数据不同，算出的 micro batch 数可能不同；而后续前向/反向会有集合通信，所有 rank 必须**迭代相同次数**才能同步，否则死锁。取 MAX 是为了保证至少有那个数个 micro batch（不够的会被 KK 分到空……实际上配合 assert 与数据量约束保证可行）。

**练习 2**：动态 batching 打乱了样本顺序，调用方是如何把结果还原的？

**参考答案**：`rearrange_micro_batches` 返回了 `micro_bsz_idx`（每组内的原始 idx）；调用方用 `get_reverse_idx(indices)` 构造反向映射，对输出做 `log_probs[revert_indices]` 即可恢复原顺序（见 [dp_actor.py:L195-L199](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L195-L199)）。

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端」的均衡实验。

**任务**：模拟一个 8 条样本、2 个 dp rank 的场景，对比「不均衡」「仅 rank 均衡」「rank 均衡 + 动态 micro batch」三种策略。

**示例代码**（非项目原有代码，仅供理解，可在本地 Python 运行）：

```python
# 示例代码
from verl.utils.seqlen_balancing import (
    get_seqlen_balanced_partitions, rearrange_micro_batches, log_seqlen_unbalance
)
from tensordict import TensorDict
import torch

seqlen = [10, 9, 8, 7, 5, 4, 3, 2]   # 8 条样本，总 48

# (1) 不做任何均衡：顺序切成 2 rank
order_idx = list(range(8))
rank0_naive = sum(seqlen[i] for i in order_idx[:4])
rank1_naive = sum(seqlen[i] for i in order_idx[4:])
print("朴素顺序切: rank0=%d, rank1=%d, 差=%d" % (rank0_naive, rank1_naive, abs(rank0_naive-rank1_naive)))

# (2) _balance_batch 的核心：KK 均衡切 2 组，equal_size=True
parts = get_seqlen_balanced_partitions(seqlen, k_partitions=2, equal_size=True)
reorder_idx = [j for p in parts for j in p]
rank0_bal = sum(seqlen[reorder_idx[i]] for i in range(4))
rank1_bal = sum(seqlen[reorder_idx[i]] for i in range(4, 8))
print("KK 均衡切: rank0=%d, rank1=%d, 差=%d" % (rank0_bal, rank1_bal, abs(rank0_bal-rank1_bal)))
print("  均衡指标:", log_seqlen_unbalance(seqlen, parts, prefix="global_seqlen"))

# (3) 在 rank0 的 4 条样本上做动态 micro batch（max_token_len=18）
attn = torch.tensor([[1]*L + [0]*(10-L) for L in [seqlen[i] for i in reorder_idx[:4]]])
td = TensorDict({"attention_mask": attn}, batch_size=[4])
mbs, idx = rearrange_micro_batches(td, max_token_len=18)
print("动态 micro batch 数=%d, 各 batch 有效 token=%s" %
      (len(mbs), [int(m["attention_mask"].sum()) for m in mbs]))
```

**需要观察的现象与预期结果**：

1. 朴素顺序切：rank0=34、rank1=14，差 20（严重不均）。
2. KK 均衡切：两 rank 和接近 24、24，差大幅缩小。
3. 动态 micro batch：每个 micro batch 的有效 token 都接近 18 的预算，且数量由 `ceildiv` 决定。

**待本地验证**：具体数值取决于 KK 的 tie-break，但「均衡后差距显著缩小」「micro batch token 负载接近预算」两个结论稳定成立。

## 6. 本讲小结

- veRL 把「让各 dp rank / 各 micro batch 的有效 token 数均衡」建模为**多路数字分区问题**，用 **Karmarkar-Karp 最大差分法**近似求解（[seqlen_balancing.py:L25-L130](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L25-L130)）。
- 公开入口 `get_seqlen_balanced_partitions` = `karmarkar_karp` + 完整性校验 + 组内排序；`equal_size` 区分「等条数（rank 切分）」与「可变条数（micro batch 打包）」两种用途（[seqlen_balancing.py:L152-L183](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L152-L183)）。
- `_balance_batch` 在 **driver** 上、用 `equal_size=True` 重排 batch，让 dispatch 的等分 `chunk(world_size)` 给每个 rank 分到相近的总 token；它会**打乱样本顺序**，GRPO 靠 `uid` 重新归组（[ray_trainer.py:L530-L545](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L530-L545)）。
- `rearrange_micro_batches` 是 **worker 内**、用 `equal_size=False` 按 token 预算 `max_token_len` 装箱；它让每个 micro batch 的 token 负载接近满载，从而提升 GPU 利用率（[seqlen_balancing.py:L224-L256](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L224-L256)）。
- 两个均衡是**同一算法在两个粒度**上的应用：rank 级（消除木桶短板）+ micro batch 级（动态 packing）。
- 动态 batching 打乱顺序后，调用方靠 `get_reverse_idx` 还原结果；`use_dynamic_bsz` 默认关闭，开启需要同时设 `ppo_max_token_len_per_gpu`。

## 7. 下一步学习建议

- 回到 u6-l2（Actor 更新）与 u6-l3（Critic 更新），对照看 `_forward_micro_batch` 如何消费这里切出的 micro batch，理解「mini → micro → 梯度累积」三层结构。
- 结合 u5-l5（GRPO），体会 `_balance_batch` 打乱顺序后，`uid` 分组机制为何是 GRPO 正确性的关键。
- 进阶：阅读 `seqlen_balancing.py` 中未被默认使用的 `greedy_partition`，思考它为何不如 KK；并尝试评估在长度方差极大时（如很长的 countdown 回答），`use_dynamic_bsz=True` 对吞吐与显存的实际影响。
