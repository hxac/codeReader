# u3-l3 专家级分配与 top-B 预取专家选择

## 1. 本讲目标

上一讲（u3-l2）我们看完了规划器的 Phase A：它产出一张**群组级**迁移矩阵 `z[h, d]`——"home group `h` 总共要给目的 rank `d` 迁移多少个 token"。但 `z` 只是一个总量，还没回答两个更具体的问题：

1. 这些要迁走的 token 具体来自哪些**专家**？（专家级分配）
2. 每个 rank 收到 token 后，它们在接收缓冲区里**按什么顺序、怎么对齐地**摆下来？（padded 段布局）

本讲沿着规划内核的 Phase B 与 Phase C 回答这两个问题。读完本讲，你应该能够：

- 说清 `alloc[e, d]`（专家 `e` 有多少 token 落到目的 rank `d`）是如何由 `z` 矩阵经贪心循环展开的，以及 `alloc_cumsum` 为何要按目的 rank 维做前缀和。
- 掌握 top-B 远程专家选择规则：排除本地专家、按 token 数取最大、平局取大下标、空槽写 `-1`，并理解 `remote_stats` 两个分量的含义。
- 理解 `cu_seqlens`（[E+B] 段偏移）与 `zero_fill_ranges`（padding 清零区间）如何按 `token_padding` 对齐生成，以及 NvS 容量公式 \(NvS = S \cdot K + (tp-1) \cdot 2 \cdot \frac{E}{R}\) 的推导。
- 能用纯 PyTorch（CPU 即可）独立复现 Phase C 的布局决策，并用含空段、满段的小用例自测。

## 2. 前置知识

本讲假设你已读过 u3-l1（`MoonEPCommPlan` 数据结构）与 u3-l2（Phase A 的 surplus/deficit 平衡算法）。快速回顾关键概念：

- **符号**：`S` 每 rank token 数，`K` top-k，`E` 专家总数，`R` EP rank 数，`epn = E/R` 每个 rank 拥有的 home 专家数，`B` 预取槽数，`tp = token_padding` 段对齐粒度。
- **`tpe[src_rank, e]`**：源 rank 上专家 `e` 的 token 直方图；`tpe_cumsum` 沿源 rank 维做前缀和，`tpe_cumsum[R-1, e]` 即专家 `e` 的全局总数 `expert_count[e]`（u3-l2 已讲）。
- **`z[h, d]`**：Phase A 产出的群组级迁移矩阵。它有一条关键不变量：**每列至多一个非零元**——每个目的 rank 至多从一个远程 home group 接收 token。这条不变量是本讲推导 NvS 容量的基石。
- **CAP**：每个 rank 的接收容量，`NvS_capacity = S*K`。Phase A 保证均衡后每个 rank 恰好接收 `S*K` 个真实 token。
- **`cu_seqlens`**：形状 `[E+B]` 的 int32 张量，是 [E+B] 个"段"的**累积结束偏移**——组 GEMM 靠它切分每个专家段（回顾 u1-l4：dispatch 返回它，专家 FFN 用它做变长输入）。本讲讲清楚它是怎么算出来的。
- **预取槽**：权重张量 `[E+B, H, H']` 的最后 `B` 行（u1-l4、u5-l1 会展开）。规划器可以把一个远程专家的 token"挂"到某个预取槽对应的段上，配合 `prefetch_weight` 把该专家的权重搬到本地，让组 GEMM 全程读本地显存。

一个值得先建立的直觉：**Phase B 与 Phase C 仍然只在 rank 0 上运行**（都在 `if rank == 0:` 分支里），算完写入 rank 0 的 meta_buf PLAN 区，再通过组播/暂存发布给所有 rank——发布机制属于下一讲 u3-l6，本讲最后只做衔接。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 规划内核本体。本讲精读 Phase B（L717-L798）与 Phase C（L799-L960） |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | PyTorch 参考实现，与内核逐张量对拍；本讲的实践脚本以它为蓝本 |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | `_create_context` 中的 NvS 容量公式与 `alloc`/`z` 等 scratch 的分配 |
| [tests/test_planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py) | 规划测试：参考实现对拍 + 布局不变量检查 |
| [tests/kernel_test_utils.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py) | `planning_invariant_errors` 等测试工具 |

Phase B/C 的全部输出都写进 meta_buf 的 **PLAN 区**。内核里定义了它的子区偏移（int32 元素为单位）：

[moonep/planning.py:546-553](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L546-L553)

```python
ALLOC_SUB = 0
TPE_SUB = E * R
EOFF_SUB = 2 * E * R
CU_SUB = 3 * E * R
ZFR_SUB = CU_SUB + R * (E + B)
ETC_SUB = ZFR_SUB + 2 * R * (E + B)
STATS_SUB = ETC_SUB + R * B
```

这段代码把 PLAN 区切成 6 个子区：`alloc_cumsum`（[E,R]）、`tpe_cumsum`（[R,E]，Phase A 已写入）、`expert_offsets`（[R,E]）、`all_cu_seqlens`（[R,E+B]）、`zero_fill`（[R,E+B,2]）、`experts_to_copy`（[R,B]）与 `remote_stats`（[R,2]）。总大小 `3ER + R(E+B) + 2R(E+B) + RB + 2R` 恰好等于测试工具里校验的 `planning_out_elems`（见 [tests/kernel_test_utils.py:326-340](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/kernel_test_utils.py#L326-L340)）——布局若有差错，测试第一步就会报偏移不一致。

## 4. 核心概念与源码讲解

### 4.1 模块一：Phase B——把 z 矩阵展开成逐专家的 alloc

#### 4.1.1 概念说明

Phase A 的 `z[h, d]` 说"group `h` 给 rank `d` 迁 `z` 个 token"，但 group 是 `epn` 个专家的集合——迁哪些专家的 token？这就是 `alloc` 要回答的问题：

\[
alloc[e, d] = \text{专家 } e \text { 落到目的 rank } d \text{ 的 token 数}
\]

它满足两条硬约束（参考实现里有显式断言，内核靠构造保证）：

- **逐专家守恒**：\(\sum_d alloc[e, d] = expert\_count[e]\)，一个 token 不能多也不能少。
- **配额一致**：对每个 home group \(h\)，\(\sum_{e \in h} alloc[e, d] = z[h, d]\)，Phase A 定下的迁移量必须分毫不差地落实。

展开策略是又一个贪心，而且和 Phase A 的平衡循环形神兼备：Phase A 每轮挑"最过剩 group ↔ 最大缺口 group"配对，Phase B 每轮挑"最大配额 rank ↔ 剩余 token 最多的本地专家"配对。直觉是：**优先用最热的专家去填最大的坑**，一轮 `take = min(剩余, 配额)`，直到配额清零。

还要注意一个布局细节：内核工作缓冲 `ctx['alloc']` 用**转置的 [R, E] 布局**（扁平为 `alloc[d*E + e]`），因为 Phase C 按目的 rank 读它是合并访问；而 PLAN 区里的 `alloc_cumsum` 用 **[E, R] 布局**（`alloc_cumsum[e, d]`），因为 Phase C2 的二分查找按专家行访问。参考实现的注释明确说明了这一点：[tests/planning_reference.py:73-77](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L73-L77)。

#### 4.1.2 核心流程

对每个 owner rank `h`（各 CTA 并行认领 `for owner_rank in range(pid, R, num_sms)`）：

```text
# 第一步：初始态 = 全部 token 留在 home
s_alloc[h, e] = expert_count[e]      # 对 e ∈ h 的 epn 个本地专家
s_alloc[d≠h, e] = 0

# 第二步：单 warp 贪心，把 z[h, ·] 的配额摊到专家上
quotas[d]         = z[h, d]          # 装进寄存器
owner_remaining[e] = s_alloc[h, e]   # 装进寄存器
while True:
    (max_quota, target_rank)      = argmax(quotas)          # 平局取小 rank
    if max_quota <= 0: break
    (max_remaining, selected_e)   = argmax(owner_remaining) # 平局取小专家号
    if max_remaining <= 0: break
    take = min(max_remaining, max_quota)
    s_alloc[target_rank, selected_e] += take
    s_alloc[h,           selected_e]  = max_remaining - take
    quotas[target_rank] -= take; owner_remaining[selected_e] -= take

# 第三步：写回全局 alloc（[R,E]），再沿目的 rank 维做包含式前缀和得 alloc_cumsum（[E,R]）
```

一个专家的 token 可能被**拆开**：若配额小于该专家的剩余量（`take = max_quota`），剩余部分留在 home；下一轮换别的专家继续填。极端情况下同一专家的 token 会分居 home 与一个远程 rank。

`alloc_cumsum[e, d] = Σ_{d' ≤ d} alloc[e, d']` 是留给 Phase C2（passB，u3-l4）的查找表：给定某 token 在专家 `e` 全局序列里的名次 `g`，在 `alloc_cumsum[e, ·]` 上二分找到第一个大于 `g` 的位置即目的 rank。这也是它必须按 [E, R]（rank 维连续）存放的原因。

#### 4.1.3 源码精读

**初始化——全部 token 先留在 home：**

[moonep/planning.py:717-727](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L717-L727)

```python
for owner_rank in cutlass.range(pid, R, num_sms):
    expert_base = owner_rank * epn
    for idx in cutlass.range(tid, epn * R, num_threads):
        ...
        s_alloc[rank_idx, local_expert_id] = (
            tpe_cumsum[R - 1, global_expert] if rank_idx == owner_rank else 0
        )
```

这段代码把 owner group 的 `epn` 个专家铺进共享内存 `s_alloc`（[R, epn]），只有 `rank_idx == owner_rank` 那一行拿到 `tpe_cumsum[R-1, e]`（即 `expert_count[e]`），其余行清零——"初始态：全部留 home"。对照参考实现 [tests/planning_reference.py:78-80](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L78-L80) 的 `alloc[e, e // epn] = expert_count[e]`。

**寄存器化的配额贪心循环：**

[moonep/planning.py:730-768](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L730-L768)

```python
if tid < 32:                       # 单 warp 完成整个贪心
    quotas[j] = z_tensor[owner_rank, rank_idx]        # 目的 rank 配额
    owner_remaining[j] = s_alloc[owner_rank, local_expert_id]
    keep_balancing = cutlass.Boolean(True)
    while keep_balancing:
        max_quota, target_rank = reg_scan_argmax_min_idx(quotas, R, lane)
        if max_quota <= 0: break
        max_remaining, selected_expert_id = reg_scan_argmax_min_idx(
            owner_remaining, epn, lane)
        if max_remaining <= 0: break
        take = cutlass.min(max_remaining, max_quota)
        ...
        if tid == 0:
            s_alloc[target_rank, selected_expert_id] += take
            s_alloc[owner_rank, selected_expert_id] = max_remaining - take
```

要点：

- `quotas` 与 `owner_remaining` 都是 `make_rmem_tensor`（warp 寄存器数组，见 [moonep/planning.py:257-263](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L257-L263) 的注释），全程不碰共享内存，每轮 argmax 是一次 warp 归约。
- 平局规则：`reg_scan_argmax_min_idx` 里 `if x > bv`（严格大于）使先出现的（小下标）胜出——与参考实现 `torch.argmax` 取首个最大值的语义一致，这是内核与参考能逐元素对拍相等的前提之一。
- `s_alloc[owner_rank, ·] = max_remaining - take` 是赋值而非减法，但 `max_remaining` 是本轮读到的旧值，语义等同 `-= take`。

对照参考实现的同款循环：[tests/planning_reference.py:101-122](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L101-L122)，`d = quotas.argmax()` / `local_e = remaining.argmax()` / `take = torch.minimum(rem, quota)` 一一对应。

**写回与前缀和：**

[moonep/planning.py:771-795](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L771-L795)

```python
alloc_tensor[rank_idx, global_expert] = s_alloc[rank_idx, local_expert_id]  # [R,E]
...
for local_expert_id in cutlass.range(tid, epn, num_threads):
    cum = 0
    for rank_idx in cutlass.range_constexpr(R):
        cum += s_alloc[rank_idx, local_expert_id]
        s_alloc[rank_idx, local_expert_id] = cum        # 包含式前缀和
...
alloc_cumsum[global_expert, rank_idx] = s_alloc[rank_idx, local_expert_id]  # [E,R]
```

这段代码先把 `s_alloc` 覆写为沿目的 rank 维的**包含式**前缀和，再转置写入 PLAN 区的 ALLOC 子区。参考实现的等价物是 [tests/planning_reference.py:135](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L135) 的 `alloc_cumsum = alloc.cumsum(dim=1)`，随后 L124-127 有两条断言兜底：逐专家守恒与"任何 rank 的总接收不超过 CAP"。

#### 4.1.4 代码实践

**实践目标**：手工追踪 Phase B 的贪心循环，确认你理解 `take` 的语义与"专家被拆分"的情形。

**操作步骤**：

1. 取小配置：`R=2, E=4, epn=2`，owner group `h=0` 拥有专家 0、1，`expert_count = [9, 3]`（专家 2、3 属于 group 1，本例不管）。设 Phase A 给出 `z[0,1] = 10`。
2. 在纸上按轮次填表：每轮记录 `(max_quota, target_rank)`、`(max_remaining, selected_e)`、`take`、更新后的 `s_alloc[1, e]` 与 home 行余量。
3. 写成脚本（示例代码，非项目源码）：

```python
# phase_b_trace.py — 示例代码：手工复现 Phase B 的配额贪心
expert_count = [9, 3]   # owner group 0 的两个专家
z_row = {1: 10}         # z[0, 1] = 10，发给 rank 1
alloc = {0: expert_count[:], 1: [0, 0]}   # alloc[d][local_e]
quotas, remaining = dict(z_row), expert_count[:]
while True:
    d = max(quotas, key=lambda r: quotas[r])          # 平局取小 rank
    if quotas[d] <= 0: break
    e = min(range(len(remaining)), key=lambda i: (-remaining[i], i))
    if remaining[e] <= 0: break
    take = min(remaining[e], quotas[d])
    alloc[1][e] += take; alloc[0][e] = remaining[e] - take
    remaining[e] -= take; quotas[d] -= take
print(alloc)   # 期望: rank0=[0, 2], rank1=[9, 1]
```

**需要观察的现象**：第 1 轮 `take = min(9, 10) = 9`，配额只剩 1；第 2 轮选中专家 1（剩余 3），`take = min(3, 1) = 1`——专家 1 被拆成"1 个去 rank 1、2 个留 home"。

**预期结果**：`alloc[·, 专家0] = [0, 9]`（home 清零、全部迁走），`alloc[·, 专家1] = [2, 1]`；逐专家守恒 `9 = 0+9`、`3 = 2+1`，且第 1 列之和 `10 = z[0,1]`。脚本含固定期望输出，本地运行即可自检（本讲在无 GPU 环境下编写，输出为手工推导，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `quotas` 的对角元 `z[h, h]` 一定是 0，因而 `target_rank` 永远不会等于 `owner_rank`？

**答案**：Phase A 的平衡循环只在 `surplus_rank`（balance > 0）与 `deficit_rank`（balance < 0）之间写 `z`，两者不可能相等，所以 `z` 对角线恒为 0；于是 `quotas[owner_rank] = 0`，而循环要求 `max_quota > 0`，`target_rank == owner_rank` 不可能胜出。

**练习 2**：贪心循环最多执行多少轮？复杂度由什么决定？

**答案**：每轮要么清空一个配额（`take = max_quota`），要么清空一个专家的剩余量（`take = max_remaining`）。配额至多 `R-1` 个非零，专家至多 `epn` 个，所以轮数上界 \(O(R + epn)\)；每轮两次 warp argmax 归约，单 warp 串行、无共享内存往返。

**练习 3**：内核 `alloc` 缓冲用 [R, E] 而 `alloc_cumsum` 用 [E, R]，为什么不统一？

**答案**：两者服务于不同读者。Phase C 按"目的 rank"扫全部专家（`alloc_tensor[dest_rank, expert_idx]`），[R, E] 让一次循环内的访问在专家维连续；Phase C2/passB 按"专家"在 rank 维二分（`alloc_cumsum[expert_idx, mid]`），[E, R] 让二分路径缓存友好。转置发生在 [moonep/planning.py:789-795](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L789-L795) 写回 PLAN 区时，一次搬运转置一次，之后两个消费者各自读到合并访问的布局。

### 4.2 模块二：Phase C（上）——top-B 远程专家选择与 experts_to_copy

#### 4.2.1 概念说明

对每个目的 rank `d` 来说，`alloc[·, d]` 里非零的**远程**专家（不属于 `[d*epn, (d+1)*epn)` 本地段）就是它必须经 NVLink 读远端权重才能计算的专家。MoonEP 的核心卖点"动态冗余专家"在这里落地：挑出**最热的 B 个**远程专家，把它们写进 `experts_to_copy[d]`，让 `prefetch_weight`（u5-l1）把权重搬进本地预取槽，之后每层前向都读本地显存。

选择规则（内核与参考实现严格一致）：

1. 候选 = `alloc[e, d] > 0` 且 `e` 不在 `d` 的本地段内的专家。注意 `remote_stats[d, 0]` 统计的是**全部**候选数——它可以大于 B，意味着超出 B 的远程专家仍要在全局组上算（权重继续走 NVLink），预取只覆盖最热的前 B 个。
2. 按 `(token 数, 专家号)` 降序取前 B；**平局取大专家号**。
3. 候选不足 B 时，空槽写 `-1`（`prefetch_weight` 跳过 `-1` 槽）。
4. 每选中一个专家 `e`，就给它的 owner 计数 `remote_stats[e // epn, 1]` 加一——这个分量统计"我的专家被各目的 rank 选中预取的总次数"（按 `(expert, dest)` 选择计数；同一专家被两个 rank 选中会计两次）。

于是 `remote_stats` 的两行分别是：本 rank 作为**接收方**关心的"非零远程专家数"，与本 rank 作为**权重提供方**关心的"我的专家被预取次数"。

#### 4.2.2 核心流程

```text
对每个 dest_rank d（各 CTA 并行认领）:
    s_expert_counts[e] = alloc[d, e]        # 本 rank 视角的专家计数（[R,E] 布局）
    单 warp:
        remote_cnt[e] = 0 若 e 本地, 否则 alloc[d, e]
        stats[d][0] = #{e: remote_cnt[e] > 0}
        重复 B 轮:
            best = argmax(remote_cnt)        # 平局取大下标
            slot = best if cnt>0 else -1
            experts_to_copy[d][slot轮次] = slot
            若 slot >= 0: stats[slot//epn][1] += 1（原子加）; 标记 mask[slot]=1
            remote_cnt[best] = 0             # 挖掉已选，下一轮取次大
```

#### 4.2.3 源码精读

**候选构建与计数：**

[moonep/planning.py:846-865](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L846-L865)

```python
if tid < 32:
    remote_expert_counts[j] = 0
    if expert_idx < E:
        cnt = s_expert_counts[expert_idx]
        is_local = (expert_idx >= local_start) & (expert_idx < local_end)
        remote_expert_counts[j] = 0 if is_local else cnt
    ...
    all_remote_stats[dest_rank, 0] = remote_expert_count   # 非零远程专家数
```

这段代码把本地专家清零后放进 warp 寄存器数组，先做一次"非零项计数"写入 `remote_stats[d, 0]`。

**B 轮挖最大：**

[moonep/planning.py:866-884](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L866-L884)

```python
for slot in cutlass.range_constexpr(B):
    best_cnt, best_idx = reg_scan_argmax_max_idx(remote_expert_counts, E, lane)
    for j ...:
        if expert_idx == best_idx: remote_expert_counts[j] = 0   # 挖掉
    if tid == 0:
        expert_idx = best_idx if best_cnt > 0 else -1
        s_selected_experts[slot] = expert_idx
        all_experts_to_copy[dest_rank, slot] = expert_idx
        if expert_idx >= 0:
            owner_rank = expert_idx // epn
            cute.arch.atomic_add(elem_ptr(all_remote_stats, (owner_rank, 1)), 1, scope="gpu")
            s_selected_mask[expert_idx] = 1
```

三个细节：

- **平局规则**换了方向：这里用 `reg_scan_argmax_max_idx`，其扫描条件是 `if x >= bv`（[moonep/planning.py:236-242](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L236-L242)），平局取**大**下标。参考实现对应 [tests/planning_reference.py:160](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L160) 的 `remote_experts.sort(key=lambda e: (alloc[e, d].item(), e), reverse=True)`——按 (计数, 专家号) 降序，同计数时大专家号在前。两处规则必须同向，否则对拍必挂。
- `all_remote_stats` 的第二分量在多个 CTA（不同 dest_rank）同时原子累加到同一 owner 行上，所以必须 `atomic_add` 且 `scope="gpu"`。
- `s_selected_mask` 是给下一段布局用的：被选中的专家在**全局组**上的段将被置空，token 改挂在预取槽组。

参考实现的对应片段（列表推导 + 排序 + 填槽 + owner 计数）：

[tests/planning_reference.py:153-166](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L153-L166)

```python
remote_experts = [e for e in range(E)
                  if alloc[e, d].item() > 0 and not (local_start <= e < local_end)]
remote_experts.sort(key=lambda e: (alloc[e, d].item(), e), reverse=True)
remote_stats_all[d, 0] = len(remote_experts)
for b, e in enumerate(remote_experts[:B]):
    experts_to_copy[d, b] = e
    remote_stats_all[e // epn, 1] += 1
```

#### 4.2.4 代码实践

**实践目标**：验证平局规则理解正确，并确认"候选不足 B 时补 `-1`"的路径。

**操作步骤**：

1. 阅读上面对照的两段代码，先写下你的预测：`B=1`，某 dest rank 的远程专家计数为 `e2=5, e3=5`，哪个专家进槽？
2. 写 6 行脚本（示例代码，非项目源码）模拟参考实现的排序：

```python
alloc_col = {2: 5, 3: 5}          # 只有 e2、e3 有远程 token
B = 1
remote = sorted(alloc_col, key=lambda e: (alloc_col[e], e), reverse=True)
print(remote[:B])                  # 期望 [3]
```

3. 再构造 `alloc_col = {7: 4}`、`B=3`，观察 `experts_to_copy` 应为 `[7, -1, -1]`。

**需要观察的现象**：同计数时排序键 `(cnt, e)` 的第二维生效，大专家号在前；候选只有 1 个而 B=3 时，第 2、3 轮内核走 `best_cnt == 0` 分支写 `-1`。

**预期结果**：第一个实验输出 `[3]`（内核侧 `reg_scan_argmax_max_idx` 的 `>=` 平局规则与此一致）；第二个实验得到 `[7, -1, -1]`。纯 CPU 可运行，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`remote_stats[d, 0] = 10` 而 `B = 4`，说明什么？

**答案**：目的 rank `d` 收到了来自 10 个不同远程专家的 token，但只有最热的 4 个会被预取权重；其余 6 个专家的 token 仍在全局组上计算，每次前向都要经 NVLink 读 owner rank 的权重——预取是"部分冗余"，不是全量复制。

**练习 2**：为什么 top-B 用单 warp 的 B 轮 argmax，而不是像 Phase A 的 group_tokens 那样块级并行？

**答案**：top-B 是**多轮选择**问题（选完要挖掉再选次大），天然串行；数据量 E 可整段装进 warp 寄存器（`E_CHUNK = ceil(E/32)` 个寄存器/线程），B 轮 warp 归约的成本远低于设计一个并行 top-K 的复杂度。而 Phase A 的统计是一次性归约，块级并行才划算。

**练习 3**：`experts_to_copy` 里出现 `-1` 时，下游 `prefetch_weight` 与段布局分别怎么表现？

**答案**：`prefetch_weight` 跳过 `-1` 槽（不发起远程拷贝）；段布局侧，预取槽组 `g = E+b` 读到 `selected_expert = -1` 后 `token_count` 保持 0，该组是空段，`cu_seqlens` 不增长（见下一模块 [moonep/planning.py:900-904](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L900-L904)）。

### 4.3 模块三：Phase C（下）——padded 段布局：cu_seqlens、expert_offsets 与 zero_fill_ranges

#### 4.3.1 概念说明

段布局回答"rank `d` 收到的 `S*K` 个 token 在它的 `[NvS, H]` 接收缓冲里怎么排"。规则：

- 缓冲区被切成 **E+B 个段**：段 `g < E` 是全局专家 `g` 的"本名段"；段 `g >= E` 是预取槽 `b = g - E`，挂着被选中预取的那个专家的 token。参考实现注释原话：[tests/planning_reference.py:169-171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L169-L171)——"0..E-1 是全局专家组，E..E+B-1 是被选远程专家的预取槽"。
- **每个专家只占一个段**：被选中预取的专家 `e`，其本名段 `g=e` 为空，token 全部挂到对应预取槽段；未选中的专家占本名段。这保证了 dispatch 对每个 token 恰好写一次、组 GEMM 对每段恰好用一套权重（本地或预取槽）。
- 每个非空段从 0 起顺序紧密排布，但段长向上取整到 `token_padding` 的倍数：

\[
padded(cnt) = \left\lceil \frac{cnt}{tp} \right\rceil \cdot tp
\]

- `cu_seqlens[g]` = 前 `g+1` 段 padded 长度的累积和（段的**结束**偏移）；`expert_offsets[d, e]` = 专家 `e` 实际所在段的**起始**偏移（供 passB 拼 `dst`，u3-l4）；`zero_fill_ranges[g] = (pad_start, n_pad)` 描述该段尾部 padding 行的清零区间——这些行没有真实数据，dispatch 的零填充 warp（u4-l3）必须把 hidden 和权重槽一起清零，否则组 GEMM 会把残影算进去。
- `cnt == 0` 的段占 0 行，`cu_seqlens` 不增长；`n_pad == 0` 时 `pad_start` 保持 0（下游从不读它）；最后一个非空段的 padded 结束点到 `NvS` 之间的尾部**故意不覆盖**（参考实现 [tests/planning_reference.py:142-146](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L142-L146) 的注释明确说明这是 master 行为：消费方一律经 `cu_seqlens` 读段，尾部未定义）。

**NvS 容量为什么够用**？这是本讲最重要的数学结论。设目的 rank `d` 上非空段数为 \(m\)：

\[
\sum_g padded(cnt_g) = \underbrace{\sum_g cnt_g}_{=S\cdot K\ \text{（完美均衡）}} + \sum_g \big(padded(cnt_g) - cnt_g\big) \le S\cdot K + m\,(tp-1)
\]

由 Phase A 的不变量"z 每列至多一个非零"，`d` 的远程 token 全部来自**同一个** home group，故远程专家至多 `epn` 个；本地专家也至多 `epn` 个；于是 \(m \le 2 \cdot \frac{E}{R}\)，从而

\[
NvS = S \cdot K + (tp-1)\cdot 2\cdot \frac{E}{R}
\]

这正是 [moonep/api.py:277-288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L277-L288) 的分配公式，注释逐句给出了同一论证（"each destination rank receives tokens from at most one remote home group … the extra logical NvS slot bound is (token_padding - 1) * 2 * E/R"）。`token_padding` 越大、段对齐越粗，缓冲冗余越多——这是性能（组 GEMM 段对齐）与显存的直接权衡。

#### 4.3.2 核心流程

```text
对每个 dest_rank d:
    # 第一遍：为每个段 g 算三个值
    for g in 0..E+B-1 (块内并行, 每线程负责 IPT_EB 个段):
        if g < E:  未被预取选中 → (cnt, expert_id) = (alloc[d][g], g)
                   被选中       → (0, -1)            # 本名段置空
        else:      etc[g-E] >= 0 → (cnt, expert_id) = (alloc[d][etc[g-E]], etc[g-E])
                   etc[g-E] == -1 → (0, -1)          # 空预取槽
        padded = ⌈cnt/tp⌉·tp  (cnt>0 时)
    # 第二遍：块级 exclusive scan 求 base（每段起始偏移）
    # 第三遍：写回
    for g:
        if cnt > 0: expert_offsets[d][expert_id] = base
        cu_seqlens[d][g] = base + padded
        n_pad = padded - cnt
        zero_fill[d][g] = (base+cnt, n_pad) if n_pad>0 else (0, 0)
        base += padded
```

#### 4.3.3 源码精读

**第一遍——每个线程为 `IPT_EB` 个段构建 `(cnt, expert_id, padded)` 三元组：**

[moonep/planning.py:887-913](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L887-L913)

```python
for i in cutlass.range_constexpr(IPT_EB):
    group_idx = tid * IPT_EB + i
    token_count = 0; expert_id = -1
    if group_idx < E + B:
        if group_idx < E:
            is_selected = s_selected_mask[group_idx] != 0
            if ~is_selected:
                token_count = s_expert_counts[group_idx]
                expert_id = group_idx
        else:
            selected_expert = s_selected_experts[group_idx - E]
            if selected_expert >= 0:
                token_count = s_expert_counts[selected_expert]
                expert_id = selected_expert
    padded_count = 0
    if token_count > 0:
        padded_count = cute.round_up(token_count, tp)   # tp==1 时原样
```

注意 `g < E` 分支里 `is_selected` 时三元组保持 `(0, -1)`——被预取专家的本名段就此"消失"，其 token 由预取槽分支（`expert_id = selected_expert`）接管。`padded_count` 由 `cute.round_up(token_count, tp)` 完成（tp>1 时），对照参考实现 [tests/planning_reference.py:186-191](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L186-L191) 的 `padded = ((cnt + token_padding - 1) // token_padding) * token_padding`。

**第二遍——块级 exclusive scan：**

[moonep/planning.py:915-933](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L915-L933)

```python
total_padded = 0
for i in cutlass.range_constexpr(IPT_EB):
    total_padded += padded_values[i]
# cub BlockScan 等价物：warp 内 shfl 扫描 + warp 前缀合并
inclusive = warp_inclusive_scan(total_padded, lane)
if lane == 31: s_scan_warp_prefix[warp_id] = inclusive
...                       # tid 0 把 warp 前缀转成 exclusive 基址
base = s_scan_warp_prefix[warp_id] + inclusive - total_padded
```

每个线程先把自己名下 `IPT_EB` 个段的 padded 长度求和，再做块级排他扫描得到 `base`——该线程第一段的起始偏移。这是标准 cub BlockScan 的手写版。

**第三遍——写回四个输出：**

[moonep/planning.py:934-953](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L934-L953)

```python
if group_idx < E + B:
    padded_end = base + padded_values[i]
    if token_count > 0:
        expert_offsets[dest_rank, expert_id] = base
    all_cu_seqlens[dest_rank, group_idx] = padded_end
    pad_start = 0; pad_count = 0
    if token_count > 0:
        pad_extra = padded_values[i] - token_count
        if pad_extra > 0:
            pad_start = base + token_count     # 真实 token 的结束处
            pad_count = pad_extra
    zero_fill_start[dest_rank, group_idx] = pad_start
    zero_fill_count[dest_rank, group_idx] = pad_count
    base += padded_values[i]
```

对照参考实现 [tests/planning_reference.py:172-205](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L172-L205)：`cu_seqlens[d, g] = aligned_end`、`expert_off[d, expert_id] = start`、`zero_fill_by_rank[d, g] = (end, n_pad)` 逐项对应；L204-205 还有一道保险 `cu_seqlens[d].max() > NvS 则 raise`——正是 4.3.1 容量推导的程序化断言。

**发布（衔接 u3-l6）**：布局完成后，rank 0 把 PLAN 区**前 `3ER` 个元素**（ALLOC/TPE/EOFF 三张查找表，C2 阶段每个 rank 的 passB 都要用）经组播对象一次性广播到所有 rank 的本地 PLAN 区；CU/ZFR/ETC/STATS 则由各 rank 在 Phase D 用 G2S bulk 拷贝从 rank 0 的 chunk 暂存自己的切片：

[moonep/planning.py:955-960](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L955-L960)

```python
nb = 3 * E * R; nvec = nb // 4
for i in cutlass.range(pid * num_threads + tid, nvec, num_sms * num_threads):
    ...
    multimem_st_v4(addr.ir_value(), a0, a1, a2, a3)   # 一次写全 rank
```

**一个完整的手算例子**（后文实践脚本会复用它）：

设 `R=2, E=4 (epn=2), B=2, tp=8`，目的 rank `d=0`（本地专家段 `[0,2)`），`S*K = 24`，`alloc[·, 0] = [9, 0, 7, 8]`（专家 0 本地 9 个；专家 1 本地 0 个；专家 2、3 是 rank 1 的远程专家）。

top-B：远程候选 = `{e2:7, e3:8}`，按 (cnt, e) 降序 → `experts_to_copy[0] = [3, 2]`（e3 更热，占槽 0）；`remote_stats[0,0] = 2`，`remote_stats[1,1] += 2`。

| 段 g | 含义 | cnt | padded | base | cu_seqlens[g] | zero_fill[g] | expert_off |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 本地 e0 | 9 | 16 | 0 | 16 | (9, 7) | e0 → 0 |
| 1 | 本地 e1（空段） | 0 | 0 | 16 | 16 | (0, 0) | — |
| 2 | e2 被选中，本名段置空 | 0 | 0 | 16 | 16 | (0, 0) | — |
| 3 | e3 被选中，本名段置空 | 0 | 0 | 16 | 16 | (0, 0) | — |
| 4 | 预取槽 0 → e3 | 8 | 8 | 16 | 24 | (0, 0)（满段无 padding） | e3 → 16 |
| 5 | 预取槽 1 → e2 | 7 | 8 | 24 | 32 | (31, 1) | e2 → 24 |

校验：真实 token `9+7+8 = 24 = S*K`；padded 总长 `32 ≤ NvS = 24 + 7×2×2 = 52`。这个例子同时覆盖了空段（g1/g2/g3）、满段（g4，`8 % 8 == 0` 无 padding）与普通 padding 段（g0 补 7 行、g5 补 1 行）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：不看参考实现的循环体，仅凭上面的规则用 PyTorch 写出"给定 `alloc[E, R]` 的某一列 → top-B 选择 + padded 段布局"的参考实现，用三个小用例（含空段、满段）自测，再与项目参考实现逐行对照。

**操作步骤**：

1. 新建 `my_phase_c_ref.py`（示例代码，非项目源码），实现两个函数：

```python
import torch

def pick_top_b(alloc_col, d, R, B, epn):
    """top-B 远程专家选择，返回 (experts_to_copy[d], remote_stats 增量)。"""
    E = alloc_col.numel()
    local_start, local_end = d * epn, (d + 1) * epn
    remote = [e for e in range(E)
              if alloc_col[e] > 0 and not (local_start <= e < local_end)]
    remote.sort(key=lambda e: (int(alloc_col[e]), e), reverse=True)  # 平局取大 e
    etc = torch.full((B,), -1, dtype=torch.int32)
    for b, e in enumerate(remote[:B]):
        etc[b] = e
    return etc, len(remote)

def build_layout(alloc_col, d, B, tp, prefetch_set, etc):
    """padded 段布局，返回 (cu_seqlens[E+B], zero_fill[E+B,2], expert_off[E])。"""
    E = alloc_col.numel()
    cu = torch.zeros(E + B, dtype=torch.int32)
    zfr = torch.zeros(E + B, 2, dtype=torch.int32)
    off = torch.full((E,), -1, dtype=torch.int32)
    start = 0
    for g in range(E + B):
        cnt, expert_id = 0, -1
        if g < E:
            if g not in prefetch_set:                 # 被选中 → 本名段置空
                cnt, expert_id = int(alloc_col[g]), g
        else:
            sel = int(etc[g - E])
            if sel >= 0:
                cnt, expert_id = int(alloc_col[sel]), sel
        padded = ((cnt + tp - 1) // tp) * tp if cnt > 0 else 0
        cu[g] = start + padded
        if cnt > 0:
            off[expert_id] = start
            if padded - cnt > 0:
                zfr[g, 0], zfr[g, 1] = start + cnt, padded - cnt
        start += padded
    return cu, zfr, off
```

2. 用例一（4.3.3 的手算例子）：`alloc_col = [9,0,7,8], d=0, R=2, B=2, tp=8`，断言 `etc = [3,2]`、`cu = [16,16,16,16,24,32]`、`zfr[0] = (9,7)`、`zfr[4] = (0,0)`、`zfr[5] = (31,1)`、`off = [0,-1,24,16]`。
3. 用例二（空段 + 候选不足 B）：`alloc_col = [0,0,0,5], B=2, tp=8`，断言 `etc = [3,-1]`、`cu = [0,0,0,0,8,8]`——槽 1 为 `-1`，第 5 段是空段。
4. 用例三（tp=1 无 padding + 满段）：`alloc_col = [4,0,3,0], B=1, tp=1`，断言 `etc = [2]`、`cu = [4,4,4,7]`、`zfr` 全零。
5. 三个用例通过后，打开 [tests/planning_reference.py:149-205](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L149-L205) 逐行对照，确认你的实现与官方参考在排序键、空段、`n_pad==0` 时 `pad_start=0` 等边界上语义一致。

**需要观察的现象**：空段不推进 `start`（用例一中 g1/g2/g3 三段连续压在偏移 16 上）；满段 `padded == cnt` 时 `zero_fill` 保持 `(0,0)`；被预取专家的 `expert_off` 指向**预取槽段**的起始（e3 → 16、e2 → 24），而不是它的本名段位置。

**预期结果**：三个用例的断言全部通过；`cu` 单调不减且最终值 `32 ≤ NvS = 52`。本实践只需 CPU 版 PyTorch，断言值来自 4.3.3 的手工推导（待本地验证）。有多卡环境时，可进一步把它嵌入 `tests/test_planning.py` 的 `KernelCase` 与真实内核对拍（对照 [tests/test_planning.py:287-299](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L287-L299) 的 `assert_tensor_equal_all_ranks` 用法）。

#### 4.3.5 小练习与答案

**练习 1**：若把本讲的例子中 `tp` 从 8 改成 16，`cu_seqlens` 与 `zero_fill` 如何变化？NvS 上界呢？

**答案**：g0 段 `padded = ⌈9/16⌉·16 = 16`（凑巧不变）、g4 段 `padded = 16`（原 8）、g5 段 `padded = 16`（原 8），`cu = [16,16,16,16,32,48]`；`zero_fill[4] = (24, 8)`、`zero_fill[5] = (39, 9)`。NvS 上界变为 `24 + 15×2×2 = 84`——padding 越粗，逻辑冗余越大。

**练习 2**：为什么 `zero_fill_ranges` 不覆盖"最后一个非空段结束到 NvS"的尾部区域？

**答案**：所有消费方（组 GEMM、combine）都通过 `cu_seqlens` 切段，尾部区域在任何段之外，永远不会被读；为它发起清零是纯浪费带宽。参考实现 [tests/planning_reference.py:142-146](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L142-L146) 的注释明确把这一点记为 master 行为。

**练习 3**：段布局为什么必须先做 top-B 选择、再做 padded 排布，两步能合并吗？

**答案**：段 `g < E` 的内容取决于该专家是否被预取选中（选中则置空、token 移到槽段），所以布局的输入里包含 `s_selected_mask`/`s_selected_experts`，两步有严格的因果依赖。理论上可以边选边排，但选择是单 warp 串行的 B 轮 argmax、排布是块级并行扫描，合并会把并行部分拖成串行；分开写各用最优并行形态，代价只是一次 `cute.arch.barrier()`（[moonep/planning.py:841](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L841) 与 L885）。

## 5. 综合实践

把 Phase B 与 Phase C 串成一个端到端的小型"规划器沙盘"（纯 CPU，示例代码，非项目源码）：

1. 复用 4.1.4 的 `phase_b_trace.py`，扩展为支持 `R=2` 的完整版本：手工给定 `tpe`（[R, E] 直方图）与 `z`（可以直接用 [tests/planning_reference.py:83-97](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L83-L97) 的 balance 循环生成），跑两份 owner group 的贪心，拼出完整 `alloc[E, R]`，并断言两条硬约束：逐专家守恒 `alloc.sum(dim=1) == expert_count`、每列之和不超过 CAP（对照参考实现 L124-127 的两条断言）。
2. 把 `alloc` 喂给 4.3.4 的 `pick_top_b` + `build_layout`，对 `d=0` 与 `d=1` 各产出一份 `cu_seqlens / zero_fill_ranges / experts_to_copy`。
3. 自检不变量：`cu_seqlens` 单调不减、末值 ≤ `S*K + (tp-1)*2*epn`、每个 `expert_off ≥ 0` 的专家恰有一个非空段、`experts_to_copy` 中非 `-1` 项互不相同且都落在本地段之外。
4. 观察一组偏置 `tpe`（例如某 home group 两倍过载）：确认 `z` 只把 token 迁给一个远程 rank（每列至多一个非零），且超载越重，`experts_to_copy` 里该 group 的专家越靠前——这正是"动态冗余专家跟着负载走"的直接体现。

预期：全部断言通过；沙盘输出与 `launch_planning_torch_reference` 在相同输入下逐张量一致（如本地有多卡环境，可用 `torchrun` 起 2 卡对照 [tests/test_planning.py:260-308](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_planning.py#L260-L308) 的口径验证）。本沙盘的期望行为为手工推导，待本地验证。

## 6. 本讲小结

- **Phase B** 把群组级 `z[h, d]` 展开为专家级 `alloc[e, d]`：初始态全部 token 留 home，再由单 warp 寄存器化的贪心循环反复"最大配额 rank ↔ 最热本地专家"配对，`take = min(剩余, 配额)`，一个专家可能被拆分到 home 与远程两处。
- `alloc` 内核缓冲用 [R, E]（Phase C 按目的 rank 合并读），PLAN 区的 `alloc_cumsum` 用 [E, R]（passB 按专家在 rank 维二分），写回时一次转置。
- **top-B 选择**：候选是"有 token 的远程专家"，按 (token 数, 专家号) 降序取 B 个写入 `experts_to_copy`，平局取大专家号（内核 `>=` 扫描与参考排序键严格同向）；不足补 `-1`；`remote_stats[0]` 是非零远程专家数（可大于 B），`remote_stats[1]` 是本 rank 专家被各 rank 选中预选的总次数。
- **padded 段布局**：E+B 个段紧密排布，被预取专家的本名段置空、token 挂预取槽段；段长取整到 `token_padding`；`cu_seqlens` 记累积结束偏移，`zero_fill_ranges` 记每段尾部 padding 的清零区间，`expert_offsets` 记每个专家实际所在段的起点。
- **NvS 容量** \(= S\cdot K + (tp-1)\cdot 2\cdot\frac{E}{R}\) 的根基是 Phase A 的"每列至多一个非零"：每个 rank 至多 `2·epn` 个非空段，每段至多浪费 `tp-1` 行。
- 布局结果只写在 rank 0 的 PLAN 区；前 `3ER` 个查找表经组播广播，其余由各 rank 的 Phase D 暂存——这是下一讲的入口。

## 7. 下一步学习建议

本讲产出的 `alloc_cumsum` 与 `expert_offsets` 是两张查找表，它们的第一消费者就是下一讲 **u3-l4（dst 槽位计算：排序、二分查找与 src_info 溯源）**：passB 把每个 token 的全局名次 `g` 在 `alloc_cumsum[e, ·]` 上做固定步数二分定位目的 rank，再用 `expert_offsets` 拼出 `dst = rank*NvS + off`。建议按顺序：

1. 先读 [moonep/planning.py:1048-1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1048-L1073)（C2/passB），带着本讲的 `alloc_cumsum` 语义验证二分的正确性。
2. 之后进入 **u3-l6**，看 PLAN 区如何经组播与 G2S 暂存发布到每个 rank（本讲 4.3.3 结尾留下的悬念）。
3. 若想验证理解，回头跑 `torchrun --nproc_per_node=8 -m pytest tests/test_planning.py`，重点观察 `no_padding`（tp=1）与 `tiny_biased_with_prefetch`（B=1）两个用例——它们分别钉死了本讲的 padding 语义与预取槽语义。
