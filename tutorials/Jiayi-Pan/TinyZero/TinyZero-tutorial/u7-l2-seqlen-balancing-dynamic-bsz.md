# 序列长度均衡与动态 batching

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚为什么在「数据并行（DP）」下，**各 rank 的有效 token 总数不均衡**会拖慢训练、浪费显存。
- 读懂 `_balance_batch` 如何在 driver 进程上对一整个 batch 做一次重排，让随后被切分到各 rank 的子集 token 总量尽可能接近。
- 理解 Karmarkar-Karp「最大差分法（Largest Differencing Method）」把一堆数分成 k 组、使各组之和尽量相等的分组思想。
- 区分两种 batching 策略：固定条数（`ppo_micro_batch_size`）与按总 token 数动态切（`use_dynamic_bsz=True` + `rearrange_micro_batches`），并理解后者为何能提升 GPU 利用率。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（对应前置讲义）：

- **DataProto 与 chunk/concat**（u3-l1）：driver 进程握有完整 batch，DP_COMPUTE_PROTO 会把它 `chunk(world_size)` 竖切成若干等分子集分发到各 rank。本讲讨论的「均衡」正是为了让这次竖切更均匀。
- **fit() 主循环**（u4-l3）：`_balance_batch` 在 generate 之后、ref/critic 前向之前被调用，是主循环里一个不起眼但影响负载均衡的关键步骤。
- **micro batch 与梯度累积**（u6-l2）：Actor/Critic 的 `update_policy`/`update_critic` 把一个 mini batch 再切成若干 micro batch 逐个前向，梯度累积后一起反传。本讲的「动态 batching」就是在这一层把「固定条数切」换成「按 token 总量切」。

一个直觉问题：RL 训练里，每条样本是「prompt + 模型生成的 response」，response 长度随训练变长（R1 Zero 现象），于是同一批样本的有效长度差异极大——有的几百 token，有的几千。如果把一条几千 token 的长样本和一堆短样本分到同一张卡、又把另一张卡全分到短样本，长样本那张卡就会成为瓶颈：它算得慢、还可能先 OOM。本讲要解决的就是这个「长短不齐」的问题。

## 3. 本讲源码地图

本讲涉及两个文件，分工如下：

| 文件 | 作用 |
| --- | --- |
| `verl/utils/seqlen_balancing.py` | 纯算法工具箱：Karmarkar-Karp 分组 `karmarkar_karp`、对外接口 `get_seqlen_balanced_partitions`、动态切 micro batch 的 `rearrange_micro_batches`、还原顺序的 `get_reverse_idx`，以及均衡度统计 `log_seqlen_unbalance`。 |
| `verl/trainer/ppo/ray_trainer.py` | 训练主循环所在地。`_balance_batch` 调用上面的工具，在 driver 侧对整批数据重排；`fit()` 里还写入了 `global_token_num` 这个 meta_info，供 worker 侧动态切 batch 使用。 |

补充：动态 batching 的真正消费方在 worker 侧的 `verl/workers/actor/dp_actor.py` 与 `verl/workers/critic/dp_critic.py`（调用 `rearrange_micro_batches`），以及配置入口 `verl/trainer/config/ppo_trainer.yaml`（`use_dynamic_bsz` 等开关）。本讲会引用它们，但不展开其主流程（那是 u6-l2/u6-l3 的内容）。

## 4. 核心概念与源码讲解

### 4.1 `_balance_batch`：driver 侧的全局重排

#### 4.1.1 概念说明

`_balance_batch` 解决的是**跨 DP rank 的负载均衡**问题。

在 veRL 的单控制器架构里，driver 进程握有完整 batch，调用 worker group 方法时，`DP_COMPUTE_PROTO` 会把 batch `chunk(world_size)` 成 `world_size` 份**连续的等分子集**，每份发给一个 rank（见 u3-l1、u3-l3）。如果不做任何处理，哪些样本落到哪张卡完全取决于它们在 batch 里的原始顺序——而原始顺序是「按 prompt 顺序」的，与长度无关。结果就是：某张卡可能分到一堆超长 response，另一张卡分到一堆短的，长短卡之间出现严重的等待与显存浪费。

`_balance_batch` 的做法是：**在分发之前，在 driver 上对 batch 做一次重排（reorder）**，使得「重排后连续的 `world_size` 等分」每一份的有效 token 总量都接近。重排只改变样本顺序，不改变样本内容，因此对训练正确性无影响（PPO 是无状态地处理每条样本的）。

#### 4.1.2 核心流程

`_balance_batch` 的执行过程可以概括为四步：

1. 统计每条样本的有效长度：`attention_mask` 在序列维上求和，得到长度为 `batch_size` 的列表 `global_seqlen_lst`。
2. 用 `get_seqlen_balanced_partitions` 把这批长度分成 `world_size` 组，**每组样本数相等（`equal_size=True`）** 且各组长度之和尽量接近。
3. 把分组结果「拍平」成一个全局索引 `global_idx`，调用 `batch.reorder(global_idx)` 原地重排。因为各组大小相等，所以重排后连续的 `world_size` 等分恰好等于这 `world_size` 个组——dispatch 随后竖切时，每个 rank 就拿到一个均衡的组。
4. 调用 `log_seqlen_unbalance` 把均衡前后的 min/max/minmax_diff 写入 metrics，便于在日志里观察均衡效果。

> 注意：重排会**打乱 batch 内的原始顺序**。`fit()` 的注释明确提醒：如果你要做「按组计算优势」的算法（如 GRPO，依赖同一 prompt 的多次采样相邻），需要额外小心。GRPO 通过 `uid` 字段而非位置来分组，因此不受影响（见 u5-l5）。

#### 4.1.3 源码精读

[`_balance_batch`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L530-L545) 的核心几行：

```python
global_seqlen_lst = batch.batch['attention_mask'].view(batch_size, -1).sum(-1).tolist()  # (train_batch_size,)
world_size = self.actor_rollout_wg.world_size
global_partition_lst = get_seqlen_balanced_partitions(global_seqlen_lst,
                                                      k_partitions=world_size,
                                                      equal_size=True)
# reorder based on index. The data will be automatically equally partitioned by dispatch function
global_idx = torch.tensor([j for partition in global_partition_lst for j in partition])
batch.reorder(global_idx)
```

- [ray_trainer.py:534](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L534)：`view(batch_size,-1).sum(-1)` 把每条样本的所有 token 位置求和，得到每条的有效 token 数。`attention_mask` 中有效位为 1、padding 位为 0，所以求和就是真实长度。
- [ray_trainer.py:536-538](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L536-L538)：`equal_size=True` 强制每组样本数相等——这是为了让 dispatch 的等分竖切与分组一一对应。
- [ray_trainer.py:540-541](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L540-L541)：拍平分组得到 `global_idx`，`reorder` 按此索引重排。注释「automatically equally partitioned by dispatch function」点明了它与 dispatch 竖切的配合关系。

它在 `fit()` 主循环中的调用位置（generate/repeat 之后，ref/critic 前向之前）见 [ray_trainer.py:597-603](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/ppo/ray_trainer.py#L597-L603)。紧随其后的一行也很关键：

```python
batch.meta_info['global_token_num'] = torch.sum(batch.batch['attention_mask'], dim=-1).tolist()
```

这行把**重排后**每条样本的长度写进 `meta_info`，随 batch 一起分发到各 rank。它是 driver 视角下「这批数据到底有多长」的全局记录，也用于日志统计。

#### 4.1.4 代码实践

实践目标：观察 `_balance_batch` 对「分组均衡度」的改善。

操作步骤（源码阅读型，无需 GPU）：

1. 打开 [seqlen_balancing.py 的 `log_seqlen_unbalance`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L186-L217)，读懂它返回的六个指标含义。
2. 在一段示例代码里，构造一个长度极不均的 `seqlen_list`（例如 `[100, 950, 120, 880, 200, 600]`，`k_partitions=2`），分别计算「原始连续切两段」和「`get_seqlen_balanced_partitions` 切两段」的各组之和。

需要观察的现象：原始切法两组和分别是 `100+950+120=1170` 与 `880+200+600=1680`，差 510；均衡后两组和应非常接近（差值大幅缩小）。

预期结果：均衡后 `minmax_diff` 显著下降，`balanced_min`/`balanced_max` 接近 `mean`。

> 「待本地验证」：你可以用下面的最小脚本确认（示例代码，非项目原有代码）：
> ```python
> from verl.utils.seqlen_balancing import get_seqlen_balanced_partitions, log_seqlen_unbalance
> lst = [100, 950, 120, 880, 200, 600]
> parts = get_seqlen_balanced_partitions(lst, k_partitions=2, equal_size=True)
> print(log_seqlen_unbalance(lst, parts, prefix="seqlen"))
> ```

#### 4.1.5 小练习与答案

**练习 1**：`_balance_batch` 为什么必须用 `equal_size=True`？如果改成 `equal_size=False` 会怎样？

**参考答案**：因为 dispatch 的 `DP_COMPUTE_PROTO` 是按 `world_size` 做**等分**竖切的（`chunk(world_size)` 要求能整除）。`equal_size=True` 保证每个分组的样本数恰好等于 `batch_size/world_size`，这样重排后「连续等分」与「均衡分组」一一对应，每个 rank 拿到一个均衡组。若改 `False`，各组样本数不等，连续等分就不再等于分组，均衡被打乱，甚至可能因不能整除而报错。

**练习 2**：`_balance_batch` 的重排会破坏 GRPO 的「同 prompt 多采样分组」吗？

**参考答案**：不会。GRPO 不依赖样本在 batch 里的相邻位置来分组，而是给每个 prompt 的 n 次采样打上相同的 `uid`（在 `repeat` 之前赋值），靠 `uid` 字段做组内归一化（见 u5-l5）。重排只改顺序不改 `uid`，所以 GRPO 不受影响——这正是 `fit()` 注释提醒「group based adv 需当心」、而 GRPO 用 `uid` 规避的原因。

---

### 4.2 `karmarkar_karp`：最大差分法分组

#### 4.2.1 概念说明

`get_seqlen_balanced_partitions` 的算法内核是 `karmarkar_karp`，它实现的是 **Karmarkar–Karp 最大差分法（Largest Differencing Method, LDM）**——一种经典的「多路数字分组（multiway partition / number partitioning）」启发式算法。

问题本身是 NP-hard 的：给定一堆数，分成 k 组，使各组之和尽量相等。精确求解不可行，LDM 用一个巧妙的「差分」贪心策略得到很好的近似解。

直觉上，最朴素的贪心是「长蛇阵（greedy/LPT）」：把数从大到小排序，依次扔进当前和最小的那组（本文件里 `greedy_partition` 就是这个基线）。而 LDM 更聪明：它不只看「当前最小」，而是**把两个最不均衡的状态配对、让大的一边对小的一边做差**，从而系统性地抵消差距。代码注释直接指向了维基百科条目。

#### 4.2.2 核心流程

`karmarkar_karp` 用一个小顶堆（`heapq`）维护一组「状态（State）」，每个 State 是一种「把若干样本分配进 k 个集合」的方案。算法骨架：

1. **初始化**：把样本排序。若 `equal_size=True`，每 k 个连续样本组成一个初始 State（每个集合放一个）；若 `equal_size=False`，每个样本单独成一个 State（只有第 0 个集合非空，其余 k-1 个空）。
2. **归并循环**：只要堆里还有多于 1 个 State，就弹出**差度（spread）最大的两个**，把它们 merge 成一个新 State，再压回堆。
3. **配对规则**：merge 时，一个 State 的第 i 大集合去吸收另一个 State 的第 (k-1-i) 大集合——即「大配小」，这正是「差分」名字的由来，目的是让合并后的两组和更接近。
4. 循环结束时只剩一个 State，它的 k 个集合就是最终的 k 个分组。

一个 State 的「差度」定义为：

\[
\text{spread} = \max_i \text{sum}(\text{set}_i) - \min_i \text{sum}(\text{set}_i)
\]

算法每一步都优先合并差度最大的两个 State，从而逐步把全局差度压低。

堆的弹出顺序靠 `State.__lt__` 控制：它让「spread 更大」的 State 被判定为「更小」，于是在小顶堆里最先被弹出。

#### 4.2.3 源码精读

整个函数定义在 [seqlen_balancing.py:25-130](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L25-L130)，内含两个嵌套类 `Set` 与 `State`。

**`Set` 类**（[L27-47](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L27-L47)）：一个集合，记录成员 `(idx, val)` 列表与累加和 `sum`，提供 `add` 与 `merge`。

**`State` 类**关键是三处：

- `spread` 属性（[L77-79](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L77-L79)）：`self.sets[0].sum - self.sets[-1].sum`（集合始终保持降序，故首尾即最大最小）。
- `merge`（[L72-75](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L72-L75)）：「大配小」核心——
  ```python
  for i in range(self.k):
      self.sets[i].merge(other.sets[self.k - 1 - i])
  self.sets = sorted(self.sets, reverse=True)
  ```
  第 i 大的集合吸收对方的第 i 小集合，i 从 0（最大）到 k-1（最小），随后重新降序排列。
- `__lt__`（[L81-87](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L81-L87)）：让 spread 大者「更小」优先弹出——
  ```python
  if self.spread != other.spread:
      return self.spread > other.spread
  return self.sets[0] > other.sets[0]
  ```

**主循环**（[L103-130](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L103-L130)）：

```python
sorted_seqlen_list = sorted([(seqlen, i) for i, seqlen in enumerate(seqlen_list)])
# ... 初始化 states_pq（见下）...
while len(states_pq) > 1:
    state0 = heapq.heappop(states_pq)   # spread 最大
    state1 = heapq.heappop(states_pq)   # spread 次大
    state0.merge(state1)                # 大配小合并
    heapq.heappush(states_pq, state0)
final_state = states_pq[0]
```

初始化分两种（[L105-115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L105-L115)）：
- `equal_size=True`：按 k 个一组打包成 State（[L107-112](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L107-L112)），保证最终每组样本数相等。
- `equal_size=False`：每个样本单独成 State（[L113-115](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L113-L115)），只追求和均衡，组内条数可变。

#### 4.2.4 代码实践

实践目标：手算一个 `k_partitions=2` 的分组，验证 LDM「大配小」的效果。

设 `seqlen_list = [4, 7, 2, 5, 9, 3]`，`k_partitions=2`，`equal_size=True`。

操作步骤：

1. 排序得到（按值升序，保留原下标）：`(2@idx2), (3@idx5), (4@idx0), (5@idx3), (7@idx1), (9@idx4)`。
2. `equal_size=True` 按 2 个一组初始化三个 State：
   - SA：{3(idx5)} / {2(idx2)}，spread=1
   - SB：{5(idx3)} / {4(idx0)}，spread=1
   - SC：{9(idx4)} / {7(idx1)}，spread=2
3. 弹出 spread 最大的两个（SC 与 SB），「大配小」合并：SC 的 9 吸收 SB 的 4 得 13；SC 的 7 吸收 SB 的 5 得 12 → 新 State {13} / {12}，spread=1。
4. 再把这个 State 与 SA 合并：13 吸收 SA 的 2 得 15；12 吸收 SA 的 3 得 15 → {15} / {15}，spread=0。

需要观察的现象：每一步合并后 spread 都不增，最终两组和完全相等。

预期结果：分组为 `{idx4, idx0, idx2}`（值 9+4+2=15）与 `{idx1, idx3, idx5}`（值 7+5+3=15），经 `_check_and_sort_partitions` 排序后返回 `[[0,2,4],[1,3,5]]`。

> 「待本地验证」：用 `get_seqlen_balanced_partitions([4,7,2,5,9,3], 2, True)` 确认返回 `[[0,2,4],[1,3,5]]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `State.__lt__` 要写成「spread 越大越先弹出」？这与 Python `heapq` 是小顶堆有什么关系？

**参考答案**：`heapq` 是小顶堆，`heappop` 弹出「最小」元素。LDM 每一步要优先合并「最不均衡」的两个 State，即 spread 最大的。因此把 `__lt__` 定义为 `self.spread > other.spread`，让 spread 大的 State 在序关系上「更小」，从而被小顶堆最先弹出。这是一种常见的「用小顶堆实现大顶堆」的技巧。

**练习 2**：把同一组数据交给 `greedy_partition`（[L133-149](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L133-L149)）跑一遍，比较它与 LDM 的均衡度。

**参考答案**：`greedy_partition` 是 LPT 长蛇阵：每次把当前数扔进和最小的组。对 `[4,7,2,5,9,3]`（升序处理 2,3,4,5,7,9），结果两组和通常也能接近，但在更极端的长短不齐数据上，LDM 的「大配小」差分策略往往比贪心更优。两者都只是启发式近似，LDM 是项目选用的默认实现。

---

### 4.3 `get_seqlen_balanced_partitions`：对外接口与校验

#### 4.3.1 概念说明

`get_seqlen_balanced_partitions` 是 `seqlen_balancing.py` 对外暴露的**统一分组入口**，是 `_balance_batch` 与 `rearrange_micro_batches` 共同依赖的底层能力。它在 `karmarkar_karp` 之上做了一层薄薄的封装：调用 LDM 算出分组，再做合法性校验和排序。

这一层存在的意义是：调用方（driver 的 rank 均衡、worker 的 micro batch 切分）不需要关心 LDM 内部的堆、State、Set，只要传入「长度列表 + 分几组 + 是否等量」就能拿到干净的分组结果。

#### 4.3.2 核心流程

1. 断言样本数不少于组数（`len(seqlen_list) >= k_partitions`）。
2. 调用 `karmarkar_karp` 得到分组。
3. `_check_and_sort_partitions` 校验：组数正确、无空组、所有原始下标都被恰好覆盖一次（不重不漏），并把每组内部按下标升序排好。

#### 4.3.3 源码精读

接口定义与文档串见 [seqlen_balancing.py:152-183](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L152-L183)：

```python
def get_seqlen_balanced_partitions(seqlen_list, k_partitions, equal_size):
    assert len(seqlen_list) >= k_partitions, ...
    def _check_and_sort_partitions(partitions):
        ...
        assert seen_idx == set(range(len(seqlen_list)))   # 不重不漏
        return sorted_partitions
    partitions = karmarkar_karp(seqlen_list=seqlen_list,
                                k_partitions=k_partitions, equal_size=equal_size)
    return _check_and_sort_partitions(partitions)
```

关键点：

- [L168](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L168)：前置断言，组数不能超过样本数。
- [L170-180](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L170-L180)：`_check_and_sort_partitions` 用一个 `seen_idx` 集合确认每个下标 `0..n-1` 恰好出现一次——这是分组正确性的硬保证，防止 LDM 在边界情况下漏掉或重复某个样本。
- [L182](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L182)：算法本体委托给 `karmarkar_karp`。

注意它**不直接返回「排序后的样本」**，而是返回「索引分组」——每个分组是一个原始下标列表。调用方（`_balance_batch` 或 `rearrange_micro_batches`）再自行用这些索引去重排或抽取数据。

#### 4.3.4 代码实践

实践目标：确认 `equal_size` 两个取值的行为差异。

操作步骤：

1. 用 `seqlen_list = [10, 10, 10, 1000]`、`k_partitions=2` 分别以 `equal_size=True` 和 `equal_size=False` 调用 `get_seqlen_balanced_partitions`。
2. 观察两种结果每组样本数是否相等、每组之和如何。

需要观察的现象：`equal_size=True` 时每组恰好 2 个样本（受 `assert len%k==0` 约束，这里 4%2==0 通过），1000 这个长样本必然让某组和远大于另一组；`equal_size=False` 时样本数可不等，算法可把 1000 单独成一组、其余三个 10 成另一组（和 30），但均衡仍受「单个超长样本」限制。

预期结果：`equal_size` 控制的是「组内条数是否相等」，而非「组和是否相等」；当存在极端长样本时，无论哪种模式都无法做到完美均衡——这正是 `log_seqlen_unbalance` 指标存在的意义（告诉你还差多少）。

> 「待本地验证」：注意 `equal_size=True` 要求 `len(seqlen_list) % k_partitions == 0`，否则 `karmarkar_karp` 内部会触发 [L106](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L106) 的断言报错。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_check_and_sort_partitions` 要断言 `seen_idx == set(range(len(seqlen_list)))`？

**参考答案**：分组必须把每个输入样本恰好分到且仅分到一个组——不重不漏。这个断言是正确性兜底：一旦 LDM 实现出现 bug（比如漏掉某样本或重复分配），这里会立刻抛错，而不是让训练默默用到错误的数据子集。

**练习 2**：`_balance_batch` 用 `equal_size=True`，而 `rearrange_micro_batches` 用 `equal_size=False`，为什么不同？

**参考答案**：`_balance_batch` 的结果要和 dispatch 的「等分竖切」对齐，必须每组样本数相等。而 `rearrange_micro_batches` 是在单卡上切 micro batch，目标是「每个 micro batch 的 token 总量不超过 `max_token_len` 且尽量均衡」，组内放几条样本无所谓（反正都要在这张卡上前向），所以放开「等量」约束，换取更好的 token 均衡度。

---

### 4.4 `rearrange_micro_batches`：动态 micro batch 切分

#### 4.4.1 概念说明

`rearrange_micro_batches` 解决的是**单卡内部**的 batching 策略问题，对应配置开关 `use_dynamic_bsz`。

默认情况下（`use_dynamic_bsz=False`），Actor/Critic 把 mini batch 按**固定条数** `ppo_micro_batch_size` 切成 micro batch——比如每次取 64 条。问题是：64 条里如果混了几条超长样本，这个 micro batch 就会很重、显存吃紧甚至 OOM；而如果都是短样本，64 条又算得太少、GPU 算力闲置。

动态 batching（`use_dynamic_bsz=True`）改为按**总 token 数**切：规定每个 micro batch 的有效 token 总量上限 `max_token_len`，然后让每个 micro batch 在不超过上限的前提下尽量塞满。长短样本会被混合搭配（靠 `get_seqlen_balanced_partitions`），使每个 micro batch 的计算量接近，从而**稳定地吃满 GPU 显存与算力**，减少「忽闲忽忙」的波动。

这就是本讲第二个标题——「动态 batching」——的含义：把「固定条数」换成「固定 token 预算」。

#### 4.4.2 核心流程

`rearrange_micro_batches(batch, max_token_len, dp_group=None)` 的流程：

1. 算每条样本有效长度 `seq_len_effective = attention_mask.sum(dim=1)`，求总长 `total_seqlen`。
2. 用 `ceildiv(total_seqlen, max_token_len)` 算出**最少**需要多少个 micro batch。
3. 若在分布式环境，对这个数量在 `dp_group` 上做 `all_reduce(MAX)`——保证同一 DP 组内所有 rank 用相同数量的 micro batch（否则各 rank 前向次数不同会死锁/等待）。
4. 调 `get_seqlen_balanced_partitions(seq_len_effective, num_micro_batches, equal_size=False)` 把样本均衡地分成这么多组。
5. 按分组逐个拼出 micro batch（`torch.cat`），返回 micro batch 列表与索引分组。

其中 `ceildiv(a,b) = -(a // -b)`（[L220-221](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L220-L221)）是向上取整的常见写法。

#### 4.4.3 源码精读

函数本体在 [seqlen_balancing.py:224-256](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L224-L256)，几个关键点：

```python
max_seq_len = batch['attention_mask'].shape[-1]
assert max_token_len >= max_seq_len, ...          # 单条不能超过预算
seq_len_effective = batch['attention_mask'].sum(dim=1)
total_seqlen = seq_len_effective.sum().item()
num_micro_batches = ceildiv(total_seqlen, max_token_len)
if dist.is_initialized():
    num_micro_batches = torch.tensor([num_micro_batches], device='cuda')
    dist.all_reduce(num_micro_batches, op=dist.ReduceOp.MAX, group=dp_group)
    num_micro_batches = num_micro_batches.cpu().item()
...
micro_bsz_idx = get_seqlen_balanced_partitions(seq_len_effective, num_micro_batches, equal_size=False)
```

- [L230-231](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L230-L231)：硬约束——预算 `max_token_len` 必须 ≥ 单条最大序列长度（含 padding 的 `max_seq_len`），否则连一条都放不下。
- [L236-239](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L236-L239)：`all_reduce(MAX)` 对齐 micro batch 数量。这是 DP 组内同步的关键——不同 rank 数据不同、算出的数量也不同，取最大值保证大家都跑同样多轮前向。
- [L244](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L244)：复用 4.3 的分组接口，`equal_size=False`。

**返回顺序与 `get_reverse_idx`**：函数返回 `(micro_batches, micro_bsz_idx)`。`micro_bsz_idx` 是「每个 micro batch 由哪些原始下标组成」。调用方把各 micro batch 前向结果 `concat` 后，位置 p 对应的是原始下标 `indices[p]`——顺序被打乱了。

这带来一个重要区分（见 worker 侧消费代码）：

- **只读前向且结果要与他人对齐**（`compute_log_prob`/`compute_values`）：必须用 [`get_reverse_idx`](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L259-L265) 把结果还原回原始顺序，再写回 batch（见 [dp_actor.py:195-199](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L195-L199)、[dp_critic.py:138-142](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L138-L142)）。
- **训练反传**（`update_policy`/`update_critic`）：梯度是各 micro batch 求和，与顺序无关，所以直接丢弃索引（`micro_batches, _ = rearrange_micro_batches(...)`，见 [dp_actor.py:224-226](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L224-L226)、[dp_critic.py:160-162](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/critic/dp_critic.py#L160-L162)）。

`get_reverse_idx`（[L259-265](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L259-L265)）做的事：`indices[p] = 原始下标`，构造反向映射 `reverse[原始下标] = p`，于是 `result[reverse]` 就把第 p 个结果放回它原来的位置。

#### 4.4.4 代码实践

实践目标：解释 `use_dynamic_bsz=True` 为什么能提升 GPU 利用率。

操作步骤（源码阅读型）：

1. 打开配置 [ppo_trainer.yaml 的 actor 段](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L21-L35)，找到 `use_dynamic_bsz`（[L25](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L25)）与预算 `ppo_max_token_len_per_gpu`（[L26](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L26)）。
2. 对照 [dp_actor.py:224-229](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/workers/actor/dp_actor.py#L224-L229)，比较动态切（`rearrange_micro_batches`）与固定切（`mini_batch.split(ppo_micro_batch_size)`）两条分支。

需要观察的现象：固定切时，micro batch 的「条数」恒定，但「token 总量」随样本长短大幅波动；动态切时，每个 micro batch 的 token 总量被钳制在预算附近。

预期结果（解释）：固定条数下，遇到长样本 batch 显存吃紧、遇到短样本 batch 算力闲置，GPU 利用率忽高忽低；动态 batching 让每个 micro batch 的计算量稳定接近上限，相当于「按显存容量自动决定塞几条」，从而稳定地吃满 GPU。代价是各 micro batch 条数不等、需要 `get_reverse_idx` 还原顺序（仅只读前向场景）。

> 「待本地验证」：若条件允许，可用 `tests/e2e` 的最小训练分别跑 `use_dynamic_bsz=True/False`，对比 wandb 里的 `actor/pg_loss` 阶段耗时与显存峰值。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rearrange_micro_batches` 里要对 `num_micro_batches` 做 `all_reduce(MAX)`？

**参考答案**：同一个 DP 组内的各 rank 持有不同的数据子集，各自算出的 micro batch 数量不同。但组内 rank 在前向/反传时通常需要同步（如梯度 all-reduce），如果某 rank 跑 3 个 micro batch、另一 rank 跑 5 个，快的 rank 会一直阻塞等待慢的。取 MAX 让所有 rank 都按最大的数量跑（多出来的轮次对数据少的 rank 是空操作或重复处理），保证步调一致。

**练习 2**：`update_policy` 里调用 `rearrange_micro_batches` 时为什么把返回的索引赋给 `_`（丢弃）？

**参考答案**：`update_policy` 做的是梯度累积——把各 micro batch 的梯度累加后统一反传。梯度求和满足交换律，与样本在 micro batch 内、micro batch 之间的顺序无关，所以不需要还原顺序。而 `compute_log_prob`/`compute_values` 是只读前向，其输出（log_prob/values）要按位置写回 batch 与 `old_log_probs`、`advantages` 等对齐，必须用 `get_reverse_idx` 还原。

---

## 5. 综合实践

把本讲的两条主线（跨 rank 均衡 + 单卡动态切）串起来，完成下面这个端到端的「纸面推演」任务。

**任务背景**：假设你用 2 张卡（`world_size=2`）训练，某个 step 的一个 mini batch 经 generate/repeat 后得到 6 条样本，其有效 token 长度为：

```
seqlen = [200, 850, 300, 800, 250, 600]   # 下标 0..5
```

**要求**：

1. **跨 rank 均衡**：手动用 Karmarkar-Karp（`equal_size=True`、`k_partitions=2`）算出 `_balance_batch` 的重排索引 `global_idx`，并验证重排后「前 3 条」与「后 3 条」（即两个 rank 各自分到的子集）的 token 总量是否接近。
2. **单卡动态切**：假设 rank 0 拿到的子集为 `[850, 300, 200]`（仅作示意），`max_token_len=1000`，手动推演 `rearrange_micro_batches` 会把它切成几个 micro batch、每个装了哪些样本。
3. **配置切换**：在 [ppo_trainer.yaml](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L21-L35) 中指出要打开动态 batching 需改哪个键，并说明 critic 段的 `use_dynamic_bsz` 为何不用单独设（提示：变量插值 `${}`）。

**参考推演**：

1. 总和为 `200+850+300+800+250+600=3000`，理想每组 1500。排序后两两打包初始化三个 State，经两次「大配小」合并，最终两组和应分别接近 1500（如 `{850,300,200}=1350` 与 `{800,600,250}=1650`，或更均衡的搭配），两组差值远小于极端随机切法可能出现的差距。`global_idx` 即两组下标按组拼接，`batch.reorder` 后前 3 条/后 3 条分别对应两个 rank 的均衡子集。
2. `[850,300,200]` 总和 1350，`ceildiv(1350,1000)=2` 个 micro batch；LDM 倾向把 850 与较短样本配在一组以均衡 token。注意若组合后某组仍超过 `max_token_len`，说明预算偏小——这正好印证 [L230-231](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/utils/seqlen_balancing.py#L230-L231) 的断言：`max_token_len` 必须 ≥ 单条最大长度，实际预算应留足余量以容纳组合。
3. 改 `actor_rollout_ref.actor.use_dynamic_bsz: True` 即可；critic 段的 `use_dynamic_bsz` 通过 [ppo_trainer.yaml:112](https://github.com/Jiayi-Pan/TinyZero/blob/95df88f2dcb05f33bd18da546531b52d0954c18b/verl/trainer/config/ppo_trainer.yaml#L112) 的 `${actor_rollout_ref.actor.use_dynamic_bsz}` 插值自动跟随，一改全改（见 u1-l4 的变量插值机制）。

> 「待本地验证」：综合实践的精确分组结果建议用脚本实跑确认，纸面推演重在理解「均衡→切分→配置」这条链路。

## 6. 本讲小结

- **两层均衡**：`_balance_batch` 在 driver 上做**跨 rank** 的 token 均衡（`equal_size=True`），`rearrange_micro_batches` 在 worker 上做**单卡 micro batch** 的 token 均衡（`equal_size=False`），两者共用同一个分组算法内核。
- **Karmarkar-Karp（LDM）**：用小顶堆维护「差度（spread）最大的优先合并」，合并时「大配小」做差分，把 NP-hard 的数字分组问题逼近最优解；`State.__lt__` 让 spread 大者先弹出是关键技巧。
- **`get_seqlen_balanced_partitions`** 是统一入口，在 LDM 之上加了「不重不漏」校验与组内排序，返回索引分组而非数据本身。
- **`equal_size` 的语义**：`True` 要求各组样本数相等（对接 dispatch 等分竖切），`False` 只约束 token 和（用于单卡动态切 batch）。
- **动态 batching（`use_dynamic_bsz`）**：把「固定条数切」换成「固定 token 预算切」，让每个 micro batch 计算量稳定、稳定吃满 GPU；只读前向场景需用 `get_reverse_idx` 还原顺序，训练反传场景因梯度可交换而直接丢弃索引。
- **DP 组同步**：`rearrange_micro_batches` 用 `all_reduce(MAX)` 对齐各 rank 的 micro batch 数量，避免快慢卡互相等待。

## 7. 下一步学习建议

- 若想看动态 batching 的结果如何被消费，回到 **u6-l2（Actor 更新）** 与 **u6-l3（Critic 更新）**，重点对照 `update_policy`/`update_critic` 里 `use_dynamic_bsz` 的两条分支。
- 若想理解 `_balance_batch` 之后数据如何被竖切分发，复习 **u3-l1（DataProto 的 chunk/concat）** 与 **u3-l3（DP_COMPUTE_PROTO 装饰器）**。
- 下一讲 **u7-l3（自定义新任务）** 会离开性能优化话题，转向二次开发：如何新增一个任务的数据、奖励与路由。本讲的均衡机制对你新任务透明——只要数据进了 `fit()` 主循环，均衡就会自动生效。
