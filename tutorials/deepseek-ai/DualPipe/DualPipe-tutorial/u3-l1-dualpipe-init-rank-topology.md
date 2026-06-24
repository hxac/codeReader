# DualPipe 初始化与 rank 拓扑

## 1. 本讲目标

本讲进入第 3 单元「DualPipe 引擎剖析」的第一站：**初始化**。

`DualPipe.__init__` 看起来只是一堆赋值，但它实际完成了三件事：让每个进程持有**两个**镜像模块、把「物理进程编号」翻译成「逻辑流水线编号」、并预先算好一组**拓扑标志位**。这些标志位会驱动后面 8 步调度的每一个分支。

学完本讲，你应该能够：

- 说清楚为什么每个进程要持有两个模块，以及它们分别对应完整模型的哪一部分。
- 在给定 `rank_mapping` 的情况下，手算出 `rank`、`first_rank`、`prev_rank`、`next_rank`、`last_rank` 这些**组内 rank** 的取值。
- 解释 `rank_inverse_mapping` 多出来的「+1」那一格为什么是 `None`，以及它如何优雅地处理首末进程的「没有邻居」边界。
- 判断任意一个进程是否属于 `is_first_rank / is_last_rank / is_middle_rank / is_in_second_half`，并知道这些标志在调度里起什么作用。

---

## 2. 前置知识

本讲默认你已经掌握了下面这些概念（它们在前置讲义中建立）：

- **流水线并行（PP）与微批次（micro-batch / chunk）**：把模型沿深度切成若干 stage 放在多张 GPU 上，再把一个 batch 切成多个微批次依次流过（见 u1-l1）。
- **双向流水线与每进程两模块**：数据从流水线两端相向喂入，形成 forward、reverse 两条对称数据流；每个 rank 因此持有一对镜像 stage——rank \(r\) 持有 stage \(r\) 与 stage \(\text{pp\_size}-1-r\)，代价是参数量 \(2\times\)（见 u2-l1）。
- **进程组、rank、world_size**：`dist.init_process_group` 建立进程组，`group.rank()` 是当前进程在组内的编号，`group.size()` 是组内进程总数（见 u1-l2）。
- **相邻 GPU 的 P2P 通信**：`comm.append_irecv / append_isend` 向相邻 rank 收发张量，内部用 `get_global_rank` 把组内 rank 翻译成全局 rank（见 u2-l2）。

> 一个关键的术语区分贯穿全讲：DualPipe 里同时存在三种「rank」。
>
> - **物理/组内 rank**：`self.group.rank()`，进程在当前进程组里的编号，范围 \(0 \sim \text{num\_ranks}-1\)。
> - **逻辑/pp rank**：进程在**逻辑流水线**里的位置编号，记作 `self.rank`，范围同样是 \(0 \sim \text{num\_ranks}-1\)。
> - **全局 rank**：进程在整个分布式世界里的编号，只在底层通信 `get_global_rank` 时用到。
>
> 本讲的核心就是搞清楚「物理 rank」与「逻辑 pp rank」之间的互译。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
|------|------|----------|
| [dualpipe/dualpipe.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py) | `DualPipe` 引擎本体 | `__init__` 的全部拓扑计算（L11–L45） |
| [examples/example_dualpipe.py](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py) | 可运行示例 | 如何把两个模块传给 `DualPipe`（L138–L142） |

本讲只读 `dualpipe/dualpipe.py` 的 `__init__`，以及示例中与之对应的实例化片段，**不展开** `_reset_states`、`step` 等调度细节（它们属于后续 u3-l2、u3-l5）。

---

## 4. 核心概念与源码讲解

### 4.1 每个进程持有两个模块的设计

#### 4.1.1 概念说明

回顾 u2-l1：双向流水线有 forward、reverse 两个方向（代码里称为两个 **phase**：phase 0 与 phase 1）。一个进程要同时服务这两个方向，所以它手里必须有**两个模块**：

- `self.module[0]`：服务其中一个方向；
- `self.module[1]`：服务另一个方向。

具体到示例，rank \(r\) 的两个模块分别复制自完整模型的 stage \(r\) 与 stage \(\text{pp\_size}-1-r\)。这样一来，两个方向的数据流都能在本地找到对应的计算单元，而无需在进程间搬运权重。

#### 4.1.2 核心流程

`__init__` 对模块的处理可以概括为「接收 → 校验 → 装入 ModuleList → 检测重叠钩子」：

1. 接收一个长度为 2 的模块序列 `modules`。
2. 校验第一个模块的参数确实在当前 CUDA 设备上（防止把模块放错卡）。
3. 用 `nn.ModuleList(modules)` 包起来，得到可用 `self.module[phase]` 索引的两元素列表。
4. 检测这两个模块是否同型、且该类型是否提供了 `overlapped_forward_backward` 钩子（决定能否做前反向重叠，详见 u3-l3）。

#### 4.1.3 源码精读

构造函数签名，可见 `modules` 是一个二元组，外加 `batch_dim`、`process_group`、`rank_mapping` 四个参数：

[dualpipe/dualpipe.py:12-18](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L12-L18) —— 定义 `__init__` 的四个参数，其中 `modules` 是两个镜像 stage。

接收并校验模块、装入 `ModuleList`、检测重叠钩子：

[dualpipe/dualpipe.py:21-23](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L21-L23) —— 校验设备、用 `nn.ModuleList(modules)` 持有两个模块，并检测是否支持 `overlapped_forward_backward`。

示例里如何准备这两个模块并喂给 `DualPipe`：

[examples/example_dualpipe.py:138-142](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/examples/example_dualpipe.py#L138-L142) —— rank 取 `stage[rank]` 与 `stage[pp_size-1-rank]`，复制进两个全新的 `PipelineStage`，作为一个 `Sequential` 传给 `DualPipe`。

可以看到：`local_full_modules` 取的是 `full_modules[rank]` 和 `full_modules[pp_size - 1 - rank]`，恰好印证了「rank \(r\) 持有 stage \(r\) 与 stage \(\text{pp\_size}-1-r\)」。

#### 4.1.4 代码实践

**实践目标**：确认「两模块 = 两个方向的 stage」这一设计。

**操作步骤**：

1. 打开 `examples/example_dualpipe.py`，定位 L138 的 `local_full_modules = nn.Sequential(full_modules[rank], full_modules[pp_size - 1 - rank])`。
2. 假设 `pp_size = 8`，分别对 `rank = 0`、`rank = 3`、`rank = 7` 写出 `full_modules[rank]` 和 `full_modules[pp_size - 1 - rank]` 取到的是第几个 stage。

**需要观察的现象 / 预期结果**：

| rank | module[0] 对应 stage | module[1] 对应 stage |
|------|----------------------|----------------------|
| 0 | stage 0 | stage 7 |
| 3 | stage 3 | stage 4 |
| 7 | stage 7 | stage 0 |

注意首末两个进程（rank 0 与 rank 7）持有的两个 stage 互为对调镜像，这正是双向流水线两端相向喂入的基础。

#### 4.1.5 小练习与答案

**练习 1**：如果 `pp_size = 8`，那么 `stage[3]` 会出现在哪些进程的哪个模块位置上？

**参考答案**：`stage[3]` 会出现在 rank 3 的 `module[0]`，以及 rank 4 的 `module[1]`（因为 `pp_size - 1 - 4 = 3`）。两个进程分别从 forward 和 reverse 两个方向服务同一个 stage。

---

### 4.2 rank_mapping 与 rank_inverse_mapping 的互逆计算

#### 4.2.1 概念说明

在默认情况下，「物理进程排第几」就等于「逻辑流水线排第几」——组内 rank 0 就是 pp rank 0。但 DualPipe 允许你用一个 `rank_mapping` 把这层关系**解耦**：物理进程的排布（受限于机架、网络拓扑）不必和理想流水线顺序一致，由 `rank_mapping` 指明每个物理进程应扮演哪个逻辑 pp rank。

为此引擎需要两张互逆的表：

- **`rank_mapping`**：物理（组内）rank → 逻辑 pp rank。`rank_mapping[i]` 表示组内第 `i` 号进程扮演的 pp rank。
- **`rank_inverse_mapping`**：逻辑 pp rank → 物理（组内）rank。`rank_inverse_mapping[p]` 表示扮演 pp rank `p` 的是组内第几号进程。

#### 4.2.2 核心流程

构造这两张表的逻辑很简单：

1. 若调用方没给 `rank_mapping`，则默认恒等映射 `rank_mapping = [0, 1, 2, ..., num_ranks-1]`。
2. 申请一个长度为 `num_ranks + 1` 的数组 `rank_inverse_mapping`，全部初始化为 `None`（多出的那一格的用处见 4.3）。
3. 对每个组内 rank `i`，执行 `rank_inverse_mapping[rank_mapping[i]] = i`——把「正向表」原地翻转成「逆向表」。

因为 `rank_mapping` 是 \(0 \sim \text{num\_ranks}-1\) 的一个排列，上面的原地翻转能正好填满前 `num_ranks` 格，而第 `num_ranks` 格保持 `None`。

> 用数学语言说：设 \(M\) 为 `rank_mapping`，则逆向表 \(M^{-1}\) 满足 \(M^{-1}[M[i]] = i\)，二者互为排列意义下的逆。引擎只用了一行 `rank_inverse_mapping[rank_mapping[i]] = i` 就求出了这个逆排列。

#### 4.2.3 源码精读

注释和默认映射：

[dualpipe/dualpipe.py:28-31](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L28-L31) —— 注释说明两张表的方向；未提供时默认恒等映射。

构造逆映射的那行：

[dualpipe/dualpipe.py:32-34](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L32-L34) —— 申请 `num_ranks + 1` 格、用原地翻转求出逆排列。注意数组比 `num_ranks` 多一格。

#### 4.2.4 代码实践

**实践目标**：亲手把一张正向表翻成逆向表。

**操作步骤**：

1. 取 `num_ranks = 8`，给定 `rank_mapping = [0, 4, 1, 5, 2, 6, 3, 7]`（物理进程被交错排进流水线）。
2. 仿照 L32–L34，写出长度为 9、初值全 `None` 的 `rank_inverse_mapping`，然后逐个执行 `rank_inverse_mapping[rank_mapping[i]] = i`。

**预期结果**：

| 组内 rank `i` | `rank_mapping[i]`（pp rank） | 写入操作 |
|---------------|------------------------------|----------|
| 0 | 0 | `rank_inverse_mapping[0] = 0` |
| 1 | 4 | `rank_inverse_mapping[4] = 1` |
| 2 | 1 | `rank_inverse_mapping[1] = 2` |
| 3 | 5 | `rank_inverse_mapping[5] = 3` |
| 4 | 2 | `rank_inverse_mapping[2] = 4` |
| 5 | 6 | `rank_inverse_mapping[6] = 5` |
| 6 | 3 | `rank_inverse_mapping[3] = 6` |
| 7 | 7 | `rank_inverse_mapping[7] = 7` |

最终 `rank_inverse_mapping = [0, 2, 4, 6, 1, 3, 5, 7, None]`，最后一格保持 `None`。

#### 4.2.5 小练习与答案

**练习 1**：用上面的 `rank_inverse_mapping` 验证「逆映射的逆映射等于原映射」是否成立。

**参考答案**：任取 `i`，`rank_inverse_mapping[rank_mapping[i]]` 都等于 `i`，例如 `rank_mapping[3]=5`，而 `rank_inverse_mapping[5]=3`，确实回到 `3`。说明二者互逆。

**练习 2**：如果调用方传了一个**不是排列**的 `rank_mapping`（比如同一个 pp rank 出现两次），会发生什么？

**参考答案**：原地翻转时会有两格被写、某些 pp rank 对应的格保持 `None`。后续 `prev_rank = rank_inverse_mapping[...]` 就可能取到 `None` 而非边界进程，导致通信对端错误。所以 `rank_mapping` 必须是 \(0 \sim \text{num\_ranks}-1\) 的合法排列——代码没有显式断言这一点，使用时需自行保证。

---

### 4.3 first/prev/next/last 与 None 哨兵技巧

#### 4.3.1 概念说明

有了逆映射表，引擎就能回答四类拓扑问题：

- **我是谁**：`self.rank` = 我扮演的 pp rank。
- **谁是最左 / 最右 stage**：`first_rank` / `last_rank` 是扮演 pp rank 0 / pp rank `num_ranks-1` 的**组内 rank**。
- **我的左右邻居是谁**：`prev_rank` / `next_rank` 是逻辑上比我小一档 / 大一档的 stage 所在的**组内 rank**，P2P 通信就发往这里。

棘手的是**边界**：首进程没有「左邻居」，末进程没有「右邻居」。DualPipe 用一个极其简洁的技巧处理它——让 `rank_inverse_mapping` 多一格 `None`，于是首进程查 `prev_rank`、末进程查 `next_rank` 时会自然得到 `None`，通信函数再据此短路。

#### 4.3.2 核心流程

计算自身 pp rank 与四个邻居：

```
self.rank      = rank_mapping[group.rank()]            # 物理 rank → 逻辑 pp rank
self.first_rank = rank_inverse_mapping[0]              # 谁扮演 pp rank 0
self.prev_rank  = rank_inverse_mapping[self.rank - 1]  # 左邻居（组内 rank）
self.next_rank  = rank_inverse_mapping[self.rank + 1]  # 右邻居（组内 rank）
self.last_rank  = rank_inverse_mapping[num_ranks - 1]  # 谁扮演最后一个 pp rank
```

关键边界行为：

- 首进程 `self.rank = 0`：`prev_rank = rank_inverse_mapping[-1]`。
- 末进程 `self.rank = num_ranks - 1`：`next_rank = rank_inverse_mapping[num_ranks]`。

而 `rank_inverse_mapping` 长度为 `num_ranks + 1`，于是 Python 的负索引 `[-1]` 与正索引 `[num_ranks]` **指向同一格**——也就是那格从未被写入的 `None`。这就是「+1」的真正用途。

#### 4.3.3 源码精读

四行邻居计算：

[dualpipe/dualpipe.py:36-40](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L36-L40) —— `self.rank` 是逻辑 pp rank；`first/prev/next/last_rank` 都是**组内 rank**，直接供 `comm.append_irecv/append_isend` 使用。

> 提醒：`first_rank` 是一个**整数**（首 stage 的组内 rank），而下一节的 `is_first_rank` 是一个**布尔值**（「我是否就是那个首 stage」），不要混淆。

#### 4.3.4 代码实践

**实践目标**：亲手验证「+1」那一格的 `None` 边界行为。

**操作步骤**：用 4.2.4 得到的 `rank_inverse_mapping = [0, 2, 4, 6, 1, 3, 5, 7, None]`，分别对首进程（pp rank 0）和末进程（pp rank 7）计算 `prev_rank` / `next_rank`。

**预期结果**：

- 首进程 pp rank 0：`prev_rank = rank_inverse_mapping[-1] = rank_inverse_mapping[8] = None`；`next_rank = rank_inverse_mapping[1] = 2`。
- 末进程 pp rank 7：`prev_rank = rank_inverse_mapping[6] = 5`；`next_rank = rank_inverse_mapping[8] = None`。

首进程的「左邻居」和末进程的「右邻居」都是 `None`——这正是 `_recv_forward` / `_send_backward` 等函数里 `is_first_stage` / `is_last_stage` 短路判定的依据（详见 u3-l4）。

#### 4.3.5 小练习与答案

**练习 1**：假如 `rank_inverse_mapping` 的长度是 `num_ranks`（而不是 `num_ranks + 1`），首进程的 `prev_rank` 会算成什么？为什么这是错的？

**参考答案**：会变成 `rank_inverse_mapping[-1] = rank_inverse_mapping[num_ranks - 1]`，即末 stage 的组内 rank。这意味着首进程会把自己的「左邻居」误认成最右端的进程，发出错误的 P2P 请求。多出的那一格正是为了避免这个 off-by-one，让负索引落在一个 `None` 哨兵上。

---

### 4.4 first/last/middle/second-half 拓扑判定

#### 4.4.1 概念说明

除了邻居，`__init__` 还算出四个布尔标志位。它们是 8 步调度里的「开关」，决定了本进程在每一步该走哪个分支：

| 标志 | 为真的条件 | 在调度中的意义 |
|------|-----------|----------------|
| `is_first_rank` | `rank == 0` | 我是逻辑最左 stage，负责喂数据 / 收 loss |
| `is_last_rank` | `rank == num_ranks - 1` | 我是逻辑最右 stage，职责同上但对端 |
| `is_in_second_half` | `rank >= num_ranks // 2` | 我在流水线后半段，phase 方向定义会**翻转** |
| `is_middle_rank` | `rank == num_ranks//2 - 1` 或 `rank == num_ranks//2` | 我正落在「对折点」上，调度有特殊处理 |

两个最需要理解的点：

1. **`is_middle_rank` 是「对折点」**：双向流水线在流水线正中央折返。`num_ranks` 为偶数时，正中央有两个相邻 pp rank（如 `num_ranks=8` 时是 pp rank 3 和 4）。它们是对折点，主调度步骤里会单独处理（见 u3-l5 的 Step 4）。
2. **`is_in_second_half` 翻转方向**：u2-l1 讲过，前半段进程里 phase 0 是 forward、phase 1 是 reverse；后半段进程里这两个定义互换。代码里用一行 `phase ^= self.is_in_second_half`（布尔转 0/1 异或）实现翻转。本节先记住「后半段要翻转」，翻转的细节在 u3-l2/u3-l4 展开。

#### 4.4.2 核心流程

```
is_first_rank       = (self.rank == 0)
is_last_rank        = (self.rank == num_ranks - 1)
is_in_second_half   = (self.rank >= num_ranks // 2)
is_middle_rank      = (self.rank == num_ranks // 2 - 1) or (self.rank == num_ranks // 2)
```

以 `num_ranks = 8` 为例（`num_ranks // 2 = 4`）：

- `is_first_rank`：仅 pp rank 0 为真。
- `is_last_rank`：仅 pp rank 7 为真。
- `is_in_second_half`：pp rank 4、5、6、7 为真。
- `is_middle_rank`：pp rank 3 和 4 为真（对折点）。

> 衔接提示：`step()` 里还会用到 `half_rank = min(rank, num_ranks - 1 - rank)`，它把任意 pp rank 折叠成「到最近端点的距离」，用来计算每步循环次数。它不属于 `__init__`，将在 u3-l5 详讲；这里只需知道对折点两侧对称、`is_middle_rank` 正是 `half_rank` 取到最大值的地方。

#### 4.4.3 源码精读

四个布尔标志：

[dualpipe/dualpipe.py:42-45](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L42-L45) —— 用 `self.rank`（逻辑 pp rank）一次性算出首末、中段、后半段四个标志位。

`is_in_second_half` 在计算原语里翻转方向的一个例子（仅供感知，详见 u3-l2）：

[dualpipe/dualpipe.py:67-68](https://github.com/deepseek-ai/DualPipe/blob/030ce4325f4ebeb437da4ebc6d00a70469dd58ae/dualpipe/dualpipe.py#L67-L68) —— `_forward_compute_chunk` 开头 `phase ^= self.is_in_second_half`，让后半段进程把 phase 0/1 的方向定义互换。

#### 4.4.4 代码实践

**实践目标**：判断给定进程落在哪一半、是否为对折点。

**操作步骤**：取 `num_ranks = 8`，对 pp rank `1`、`3`、`4`、`6` 分别判断 `is_in_second_half` 和 `is_middle_rank`。

**预期结果**：

| pp rank | `is_in_second_half`（rank ≥ 4） | `is_middle_rank`（rank ∈ {3,4}） |
|---------|----------------------------------|----------------------------------|
| 1 | False | False |
| 3 | False | True |
| 4 | True | True |
| 6 | True | False |

注意 pp rank 3 和 4 虽然一个在前半、一个在后半，但**都是对折点** `is_middle_rank`。

#### 4.4.5 小练习与答案

**练习 1**：`num_ranks = 8` 时，`is_middle_rank` 为什么是**两个**进程而不是一个？

**参考答案**：`num_ranks` 是偶数，正中央没有「单独一个」中间进程，而是相邻的 pp rank `num_ranks//2 - 1 = 3` 与 `num_ranks//2 = 4`。双向流水线在这两个进程之间折返，所以二者都算「中段」。`step()` 里它们走与其他进程不同的主步骤分支（u3-l5）。

**练习 2**：一个 `is_in_second_half == True` 的进程，`phase 0` 实际代表 forward 还是 reverse？

**参考答案**：后半段进程方向定义翻转，所以 `phase 0` 代表 **reverse**，`phase 1` 代表 forward。前半段进程则相反（phase 0 = forward）。这正是 L68 `phase ^= self.is_in_second_half` 的作用：让同一套以 phase 写的调度代码对前后半段都正确。

---

## 5. 综合实践

**任务**：取 `num_ranks = 8`，分别用「默认恒等映射」和「一个交错排列」手算每个进程的全部拓扑量，并标注哪些进程落在 second half。这是把 4.1–4.4 串起来的完整练习。

### 场景 A：默认恒等映射

`rank_mapping = [0, 1, 2, 3, 4, 5, 6, 7]`，`rank_inverse_mapping = [0, 1, 2, 3, 4, 5, 6, 7, None]`。

此时「物理 rank == 逻辑 pp rank」，邻居就是简单的 ±1：

| pp rank | `first_rank` | `prev_rank` | `next_rank` | `last_rank` | first? | last? | second half? | middle? |
|---------|--------------|-------------|-------------|-------------|--------|-------|--------------|---------|
| 0 | 0 | None | 1 | 7 | ✓ | | | |
| 1 | 0 | 0 | 2 | 7 | | | | |
| 2 | 0 | 1 | 3 | 7 | | | | |
| 3 | 0 | 2 | 4 | 7 | | | | ✓ |
| 4 | 0 | 3 | 5 | 7 | | | ✓ | ✓ |
| 5 | 0 | 4 | 6 | 7 | | | ✓ | |
| 6 | 0 | 5 | 7 | 7 | | | ✓ | |
| 7 | 0 | 6 | None | 7 | | ✓ | ✓ | |

`first_rank` 与 `last_rank` 是全局常量（所有进程都算得 0 和 7）；首进程 `prev_rank = None`、末进程 `next_rank = None`。

### 场景 B：交错排列 `rank_mapping = [0, 4, 1, 5, 2, 6, 3, 7]`

由 4.2.4，`rank_inverse_mapping = [0, 2, 4, 6, 1, 3, 5, 7, None]`。注意此时「物理 rank ≠ 逻辑 pp rank」，邻居不再是 ±1：

| 组内 rank | pp rank (`self.rank`) | `first_rank` | `prev_rank` | `next_rank` | `last_rank` | first? | last? | second half? | middle? |
|-----------|-----------------------|--------------|-------------|-------------|-------------|--------|-------|--------------|---------|
| 0 | 0 | 0 | None | 2 | 7 | ✓ | | | |
| 1 | 4 | 0 | 6 | 3 | 7 | | | ✓ | ✓ |
| 2 | 1 | 0 | 0 | 4 | 7 | | | | |
| 3 | 5 | 0 | 1 | 5 | 7 | | | ✓ | |
| 4 | 2 | 0 | 2 | 6 | 7 | | | | |
| 5 | 6 | 0 | 3 | 7 | 7 | | | ✓ | |
| 6 | 3 | 0 | 4 | 1 | 7 | | | | ✓ |
| 7 | 7 | 0 | 5 | None | 7 | | ✓ | ✓ | |

**几个值得对照的点**：

1. `first_rank = rank_inverse_mapping[0] = 0`、`last_rank = rank_inverse_mapping[7] = 7`，与场景 A 一致——首末 stage 仍由组内 rank 0 和 7 扮演，只是中间进程的 pp 位置被打乱了。
2. `prev_rank` / `next_rank` 不再是相邻整数，例如组内 rank 1（pp rank 4）的左右邻居是组内 rank 6 和 3——这正是 `rank_mapping` 解耦物理与逻辑顺序后，P2P 通信对端要靠逆映射查出来的原因。
3. `is_first_rank`（pp rank 0 → 组内 rank 0）、`is_last_rank`（pp rank 7 → 组内 rank 7）只与 **pp rank** 有关，不受映射打乱影响。
4. 落在 **second half** 的是 pp rank ≥ 4，即组内 rank 1、3、5、7；**middle** 是 pp rank 3 和 4，即组内 rank 6 和 1。
5. 首进程（pp rank 0）的 `prev_rank`、末进程（pp rank 7）的 `next_rank` 仍是 `None`——`None` 哨兵技巧与映射方式无关。

> **待本地验证**：如果你有 8 张 GPU，可以把场景 B 的 `rank_mapping` 传给 `DualPipe(local_modules, rank_mapping=[0,4,1,5,2,6,3,7])`，运行示例并观察 P2P 通信是否仍正确（示例默认走恒等映射，需自行改造构造调用）。在没有 GPU 的环境下，上面的手算表就是可核对的「正确答案」。

---

## 6. 本讲小结

- `DualPipe.__init__` 让每个进程通过 `nn.ModuleList(modules)` 持有**两个镜像模块**，分别服务 forward / reverse 两个 phase；示例里它们复制自 stage `rank` 与 stage `pp_size-1-rank`。
- `rank_mapping`（物理 rank → pp rank）与 `rank_inverse_mapping`（pp rank → 物理 rank）互为排列意义下的逆，后者用一行原地翻转 `rank_inverse_mapping[rank_mapping[i]] = i` 求出。
- `self.rank` 是**逻辑 pp rank**；`first/prev/next/last_rank` 都是**组内 rank**，直接供底层 P2P 通信使用。
- `rank_inverse_mapping` 多出的「+1」格始终是 `None`，让首进程的 `prev_rank`、末进程的 `next_rank` 经负索引 / 越界索引自然落到 `None`，优雅处理边界。
- `is_first_rank / is_last_rank / is_middle_rank / is_in_second_half` 四个标志由 pp rank 决定，分别标记端点、对折点、后半段（方向翻转）。
- `is_in_second_half` 在后续计算原语里用 `phase ^= self.is_in_second_half` 翻转前后半段的方向定义，使同一套调度代码对所有进程成立。

---

## 7. 下一步学习建议

本讲把 `__init__` 的拓扑算清了，但这些 rank / 标志位如何被**使用**，要看后面的讲义：

- **u3-l2 状态管理与计算原语**：进入 `_reset_states`，看四类 `[phase][chunk_id]` 缓冲与 `current_*_chunk_id` 计数器如何组织，以及 `_forward_compute_chunk` / `_backward_compute_chunk` 如何在两 phase 之间切换——那里会真正用到 `phase ^= self.is_in_second_half`。
- **u3-l4 通信原语与组合操作**：看 `_recv_forward` / `_send_backward` 等如何用本讲算出的 `prev_rank` / `next_rank` 收发张量，并用 `is_first_stage` / `is_last_stage` 在 `None` 边界处短路。
- **u3-l5 DualPipe 八步调度 `step()`**：看 `half_rank = min(rank, num_ranks-1-rank)` 与本讲的 `is_middle_rank` 如何共同决定 8 步循环的次数与分支。

建议在进入 u3-l2 前，先确认你能闭着眼写出 4.3 的「None 哨兵」推导和 4.4 的四个标志判定——它们是后面所有调度的地基。
