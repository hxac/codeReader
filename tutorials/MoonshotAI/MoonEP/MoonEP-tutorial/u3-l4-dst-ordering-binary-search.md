# u3-l4 dst 槽位计算：排序、二分查找与 src_info 溯源

## 1. 本讲目标

前两讲（u3-l2、u3-l3）已经产出了三张「表」：群组迁移矩阵 `z`、专家级分配 `alloc` 与接收缓冲段布局 `expert_off`。本讲解决规划器的最后一公里——**把这些表应用到每一个 token 条目上**，即对源 rank \(r\) 上的每个平铺 top-k 条目，回答两个问题：

1. 它应该被发送到哪个目的 rank、落在该 rank 接收缓冲的哪个槽位（`dst`）？
2. 目的 rank 事后如何知道「这个槽位里的数据来自哪个源 rank 的哪个条目」（`src_info` 溯源）？

学完本讲你应该能够：

- 理解 `run_c1` 的**多块计数排序**：vblock 直方图 → vblock 前缀和 → warp `match_any` 散射，多级偏移如何拼出每个条目的最终位置。
- 掌握 passB 在 `alloc_cumsum` 行上的**固定步数（LOG2_R）二分查找**，以及它为什么不产生 warp 分歧。
- 理解 `src_info` 的 rank-stride 编码（`src_rank * NvS + offv`）、`-1` 哨兵的含义，以及「先清零、后发布」的屏障顺序。
- 能用纯 PyTorch 复现「全局 token 序号 → (目的 rank, loff)」的映射，并把二分版本与向量化版本对拍。

## 2. 前置知识

本讲只依赖几个经典算法原语，先用一段话讲清直觉，再对照 GPU 语境。

**计数排序（counting sort）与稳定性。** 当排序键是小范围整数（本讲中是专家号 \(e \in [0, E)\)）时，无需比较排序：先统计每个键的出现次数（直方图），再做前缀和得到每个键的区间起点，最后把每个元素散射到「键的起点 + 区间内序号」。若同键元素保持原有先后顺序，则排序是**稳定**的——本讲的排序必须稳定，因为「同专家条目的先后」直接决定全局序号。

**排他/包含前缀和（exclusive / inclusive scan）。** 包含前缀和 `cumsum[i]` 是前 \(i+1\) 项之和；排他前缀和 `scan[i]` 是前 \(i\) 项之和（不含自己）。本讲两种都会出现：`tpe_cumsum` 是沿源 rank 维的**包含**前缀和，而 `expoff` 是沿专家维的**排他**前缀和。

**单调数组上的二分查找。** 给定单调不减数组 \(a[0..R)\) 和值 \(g\)，「第一个满足 \(a[d] > g\) 的下标 \(d\)」正是 `torch.searchsorted(a, g, right=True)` 的语义。本讲的二分查找找的就是它。

**GPU warp 协作原语。** warp 是 32 个线程的执行单元，同一 warp 内可用专用指令做全 warp 通信。本讲用到两个（PTX 封装细节见 u4-l1）：

- `match.any.sync.b32`：返回一个 32 位掩码，标出 warp 内「持有的值与我相同」的 lane 集合；
- `popc`（population count）：数一个 32 位整数里 1 的个数。

两者配合可一次算出「warp 内排在我前面、且与我同值的 lane 数」。

**回顾前几讲的产物**（本讲的输入）：

| 表 | 形状 | 含义 | 产出讲义 |
|---|---|---|---|
| `tpe_cumsum` | \([R, E]\) | 沿源 rank 维的包含前缀和：前 \(r{+}1\) 个 rank 上专家 \(e\) 的条目总数 | u3-l2 |
| `alloc` / `alloc_cumsum` | \([R,E]\) / \([E,R]\) | 专家 \(e\) 落到各目的 rank 的条目数 / 沿目的 rank 维的包含前缀和 | u3-l3 |
| `expert_off` | \([R, E]\) | 专家 \(e\) 的段在 rank \(d\) 接收缓冲中的起始 loff（padded 布局） | u3-l3 |
| `meta_buf` 对称内存 | 每 rank 一段 chunk | 任一 rank 可直写其他 rank 的 chunk，是 `src_info` 远程发布的物理基础 | u2-l2 / u2-l4 |

## 3. 本讲源码地图

| 文件 | 与本讲相关的内容 |
|---|---|
| [moonep/planning.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py) | 主战场：`run_c1`（L398–L517）、各 rank 的排序调度（L961–L972）、`src_info` 清零（L973–L979）、passB 主循环（L1048–L1073）、dst 重复规范化（L1079–L1113）、编码边界断言（L1267–L1291） |
| [moonep/api.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py) | 宿主侧尺寸与 meta 布局：`num_vblocks`（L300）、`SRC_INFO_OFF` 与 `meta_chunk_logical`（L328–L333） |
| [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) | 规划内核的 PyTorch 参考实现，其 Part 2（L207–L237）就是本讲映射公式的对拍口径 |
| [moonep/constants.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/constants.py) | 去重编码位宽常量（`KIDX_BITS` 等），是启动断言里 `K ≤ 127` 等上限的来源 |

一个快速索引：本讲引用的代码集中在 `planning.py` 的三处——`run_c1` 函数体（排序）、`kernel` 中 `if rank == 0` 块之后的部分（清零 + passB + 规范化）、以及启动前的 `_check_dedup_encoding_bounds`。

## 4. 核心概念与源码讲解

### 4.1 通往 dst 的最后一步：全局序号与槽位映射问题

#### 4.1.1 概念说明

u3-l3 结束时我们只知道「每个专家往每个目的 rank 发多少个条目」（`alloc`），但还不知道**具体哪几个条目**去哪个 rank。完成这步映射需要一个统一的「排队顺序」。

定义**全局 token 序号**：把所有源 rank 上选了专家 \(e\) 的条目，按「源 rank 优先、源内平铺偏移 \(offv = s \cdot K + k\) 次之」的字典序拼接成一条全局队列，\(g\) 就是条目在这条队列里的位置。选这个字典序不是因为它是某种「自然」顺序，而是因为它恰好能用两张现成的表算出来：本 rank 内的次序来自排序（4.2 的 `run_c1`），跨 rank 的基数来自 `tpe_cumsum`。

`alloc` 的语义则是把每个专家的这条全局队列**按目的 rank 切成连续区间**：全局序号落在 \([\text{alloc\_cumsum}[e, d{-}1],\ \text{alloc\_cumsum}[e, d])\) 内的条目全部去 rank \(d\)。于是映射变成纯查表：

\[ d = \min\{d' : \text{alloc\_cumsum}[e, d'] > g\}, \qquad \text{loff} = \text{expert\_off}[d, e] + (g - \text{alloc\_cumsum}[e, d{-}1]) \]

最后把 (目的 rank, 槽位) 打包进一个 int32：

\[ \text{dst}[offv] = d \cdot NvS + \text{loff} \]

这就是 u3-l1 介绍过的 `dst` 编码——高若干位是目的 rank，低位是接收缓冲内偏移。同时向目的 rank **发布溯源**：

\[ \text{src\_info}[d \cdot NvS + \text{loff}] = r \cdot NvS + offv \]

`src_info` 与 `dst` 用的是同一种 rank-stride 编码，只是指向相反方向：`dst` 说「我去哪」，`src_info` 说「我从哪来」。dispatch 内核（u4-l3 的 dedup builder）在 fresh-planning 路径上消费目的 rank 本地的 `src_info` 切片来构建去重结构。

#### 4.1.2 核心流程

从 `topk` 到 `dst` 的完整数据流（每一步都驻留 GPU，无宿主同步）：

```text
topk[N]（本 rank 的平铺 top-k，N = S×K）
   │  run_c1：稳定计数排序（4.2）
   ▼
order[N]（专家升序的稳定置换）      本 rank tpe ──scan──▶ expoff[E]（专家基址）
   │                                   │
   ▼                                   ▼
按序位置 idx 遍历：offv = order[idx]，e = topk[offv]
local_rank ℓ = idx − expoff[e]
   │  加上跨 rank 基数 tpe_cumsum[r−1, e]（Phase A 产物，经组播发布）
   ▼
全局序号 g = tpe_cumsum[r−1, e] + ℓ
   │  alloc_cumsum[e, ·] 上 LOG2_R 步二分（Phase B 产物）
   ▼
目的 rank d，段内偏移 seg = g − alloc_cumsum[e, d−1]
   │  段基址 expert_off[d, e]（Phase C 产物）
   ▼
loff = expert_off[d, e] + seg
   ├─▶ dst[offv] = d·NvS + loff            （写回本 rank 的 plan.dst）
   └─▶ src_info[d][loff] = r·NvS + offv     （经对称内存远程写入目的 rank 的 meta 切片）
```

值得停下来体会的一点：`order` 里存的是**位置 → 平铺偏移**方向。passB 按有序位置 `idx` 顺序扫描时，条目的专家内序号不需要任何额外存储——它就是 `idx − expoff[e]`。置换数组本身就「携带」了所有条目的序号信息，这是整个设计里最优雅的一笔。

#### 4.1.3 源码精读

**passB 主循环**先整体看一遍（细节拆到 4.3），位于 [moonep/planning.py:L1048-L1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1048-L1073)：每个 CTA 认领一段有序位置区间，线程按 4 条目一组步进；对每个 `idx` 依次做「取 offv → 查专家 → 算全局序号 → 二分定位 rank → 写 dst → 远程发布 src_info」。这段代码就是 4.1.2 流程图的逐行实现。

**参考实现的同一逻辑**在 [tests/planning_reference.py:L207-L237](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py#L207-L237)：先显式循环算 `local_cnt`（L208–L213），再显式循环拼 `global_rank`（L215–L219），最后用 `torch.searchsorted(alloc_cumsum[e], g, right=True)` 定位目的 rank 并组装 `dst = dest * NvS + base_off + seg_pos`（L221–L237）。它与内核逐值对拍（tests/test_planning.py），是理解内核行为的最佳脚注：

```python
# 每个 token 在本 rank 内、同专家中的先后序号（tests/planning_reference.py:L208-L213）
local_cnt = torch.zeros(N, dtype=torch.int32)
counter = torch.zeros(E, dtype=torch.int32)
for i in range(N):
    e = flat_topk_experts[i].item()
    local_cnt[i] = counter[e]
    counter[e] += 1
```

**编码边界的守护**：src_info 的线性编码要求 \(R \cdot NvS\) 不超出 int32、\(N \le NvS\)（保证 \(offv \in [0,N)\) 落进切片语义），启动前由 [moonep/planning.py:L1275-L1281](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1275-L1281) 断言：

```python
assert N <= NvS, (
    f"src_info NvS-stride encoding requires S*K <= NvS, got S*K={N}, NvS={NvS}"
)
assert R * NvS <= int32_max, (
    "src_info linear encoding requires R*NvS <= int32_max: ..."
)
```

#### 4.1.4 代码实践

手工推导一个小例子，建立对映射公式的手感（示例代码，纯 CPU 可运行）。

设 \(E=4, R=2\)（\(epn=2\)），两个源 rank 的 tpe 分别为 `[3,0,2,3]` 与 `[1,2,0,1]`，则每专家总条目数 `counts=[4,2,2,4]`。手工造一个合法的 alloc（每位专家约一半留家、一半送往下一个 rank，`token_padding=1`）：

| 专家 e | home | alloc[e,0] | alloc[e,1] | alloc_cumsum[e,·] |
|---|---|---|---|---|
| 0 | 0 | 2 | 2 | [2, 4] |
| 1 | 0 | 1 | 1 | [1, 2] |
| 2 | 1 | 1 | 1 | [1, 2] |
| 3 | 1 | 2 | 2 | [2, 4] |

padded 段布局（`token_padding=1`，每个 rank 按专家号顺序排非零段）：rank0 依次是 e0、e2、e3 三段，故 `expert_off[0] = [0, –, 2, 3]`（`–` 表示无段）；rank1 依次是 e1、e0、e2、e3 四段，故 `expert_off[1] = [1, 0, 3, 4]`。

实践步骤：

1. **实践目标**：手算源 rank 0 上专家 0 的 3 个条目（\(g = 0, 1, 2\)）的 `dst`，再用脚本核对推导。
2. **操作步骤**：按 4.1.1 公式，\(g=0,1\) 落在区间 \([0,2)\) → \(d=0\)，loff 分别为 0、1（段基址 `expert_off[0][0]=0`）；\(g=2\) 落在 \([2,4)\) → \(d=1\)，loff = `expert_off[1][0]` + (2−2) = 1（rank 1 的布局里专家 1 的段排在专家 0 前面，所以段基址是 1）。用脚本验证：

```python
import torch
alloc_cumsum = torch.tensor([[2, 4], [1, 2], [1, 2], [2, 4]])
expert_off = torch.tensor([[0, -9, 2, 3],   # rank0：e1 无段，-9 表示永不会被查询
                           [1,  0, 3, 4]])
for g in range(3):                       # 源 rank 0 上专家 0 的全局序号 0/1/2
    d = int(torch.searchsorted(alloc_cumsum[0], g, right=True))
    pc = 0 if d == 0 else int(alloc_cumsum[0, d - 1])
    print(f"g={g} -> dest={d}, loff={int(expert_off[d, 0]) + g - pc}")
```

3. **需要观察的现象**：输出 `g=0 -> dest=0, loff=0`、`g=1 -> dest=0, loff=1`、`g=2 -> dest=1, loff=1`。
4. **预期结果**：前两个条目留在 rank 0（专家 0 段从 0 开始），第三个条目迁往 rank 1 的专家 0 段首。请再手算源 rank 1 上专家 0 的那 1 个条目（提示：\(g = \text{tpe\_cumsum}[0,0] + 0 = 3\)，应落在 rank 1 段内偏移 1，即 loff = 2）。
5. 本例可完全纸面核验；脚本输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么全局序号必须按「源 rank 优先、平铺偏移次之」拼接，而不能直接用各 rank 本地的 `offv`？

**答案**：每个 rank 的 `offv` 都取值于 \([0, N)\)，不同 rank 的 `offv` 互相重叠，无法拼出全局唯一序。而「先按源 rank 分段」的前缀基数恰好就是 Phase A 已经算好的 `tpe_cumsum[r-1, e]`——字典序的选择让映射完全复用现成的表，不需要任何新通信。

**练习 2**：若 `alloc[e, d] = 0`（某个目的 rank 分到空区间），二分查找会不会把条目映射到这个 rank？

**答案**：不会。空区间 \([\,c, c\,)\) 不包含任何整数 \(g\)。「第一个 `alloc_cumsum > g`」的查找会跳过所有与左邻相等（即零宽）的区间，直接落到更后面的 rank；`torch.searchsorted(..., right=True)` 的计数语义同样跳过零宽区间。

**练习 3**：`dst` 为什么把目的 rank 乘上 `NvS` 编码进一个 int32，而不是用两个数组分开存？

**答案**：dispatch/combine 内核拿到一个 int32 即可通过除法和取模一次解出 (rank, loff)，无需第二次访存查表；这是 MoonEP「单值携带完整路由信息」设计的一部分。代价是要求 \(R \cdot NvS \le 2^{31}-1\)，由启动断言（L1278–L1281）保证。

### 4.2 run_c1：分块计数排序

#### 4.2.1 概念说明

映射公式的第一个输入是每个条目「在本 rank 内、同专家中的先后序号」。给 \(N = S \cdot K\)（如 4096×8 = 32768）个键为 \([0, E)\) 整数的条目算序号，标准做法是**稳定计数排序**：排序后专家 \(e\) 的条目连续占据一段位置，条目的专家内序号即「排序后位置 − 该段起点」。

问题在于规模：单个 CTA 的共享内存放不下全局直方图 + 全部条目，而规划内核是 `num_sms` 个 CTA 的协作式启动（cooperative launch，见 u3-l6）。跨 CTA 的计数排序因此拆成两级：

1. **分块（vblock）直方图**：把 \([0, N)\) 切成 `num_vblocks` 个 2048 大小的块，每块独立统计直方图；
2. **跨块前缀和**：对每个专家，把各块的计数变成「排在我前面的块里同专家条目数」。

这样「块内序号 + 跨块基址 + 专家基址」相加就是最终位置——全部由 scan/scatter 组成，工作量 \(O(N + E \cdot V)\)（\(V\) 为 vblock 数），远低于比较排序的 \(O(N \log N)\)，且天然适合 GPU。

源码把这个函数命名为 `run_c1`，函数头注释概括了四步：`1a histogram / 1b vblock prefix / expoff / passA scatter`，见 [moonep/planning.py:L396-L400](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L396-L400)。

#### 4.2.2 核心流程

用伪代码描述（省略同步）：

```text
输入: topk_in[N]（平铺专家号）, tpe_counts[E]（本 rank 直方图）
输出: order_out[N]（稳定计数排序的置换：位置 -> 平铺偏移）
常量: BLOCK_SIZE_P2 = 2048, num_vblocks = ceil(N / 2048)

# 1a  分块直方图（每个 CTA 以 grid-stride 认领若干 vblock）
for vb in vblocks_of(pid):
    s_hist = [0] * E                      # 共享内存直方图
    for off in vblock vb: atomic_add(s_hist[topk_in[off]], 1)
    vblocks_histogram[vb] = s_hist        # 写到全局 local_hist[num_vblocks, E]

grid_sync()

# 1b  vblock 前缀和（每个 CTA 认领一段 32 对齐的专家区间）
for e in my_expert_segment:
    cum = 0
    for vb in 0..num_vblocks-1:
        (vblocks_histogram[vb, e], cum) = (cum, cum + vblocks_histogram[vb, e])   # 原地变排他前缀

# expoff  专家基址：本 rank tpe 沿专家维的排他前缀和
expoff[e] = Σ_{e' < e} tpe_counts[e']

# passA  散射（逐 vblock）
for vb in vblocks_of(pid):
    载入 s_block_prefix[e] = vblocks_histogram[vb, e]，清零 s_warp_counts
    每线程持 4 个条目 (p = warp*(32*4) + i*32 + lane)
      warp 内: match_any 算出「本 warp 内同专家、排在我之前的 lane 数」ww
               最低 peer lane 把该 (专家, warp) 计数格累加 popc(peers)
      块内:   对 s_warp_counts[e, w] 做排他前缀 -> warp 基址
    最终位置 sp = expoff[e] + s_block_prefix[e] + warp_base[e, w] + ww
    order_out[sp] = chunk + p
```

条目最终位置是**四级偏移的分解**，每一级只统计「与自己同键的前驱」在某个聚合层次上的数量，各级互不重叠、相加恰为全局排位：

```text
sp = expoff[e]                # 本 rank 上专家号更小的条目总数（专家基址）
   + vpfx[vb, e]              # 更早 vblock 中的同专家条目（跨块基址）
   + warp_base[e, w]          # 本 vblock 更早 warp 中的同专家条目（warp 基址）
   + ww                       # 本 warp 内更早的同专家条目（lane 级）
```

#### 4.2.3 源码精读

**常量**：每块 2048 条目、512 线程、每线程 4 条目，见 [moonep/planning.py:L94-L96](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L94-L96)；`num_vblocks = ceil(N / 2048)` 在宿主侧算好传入（[moonep/api.py:L300](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L300)），`local_hist` 缓冲按 `E * num_vblocks` 分配（[moonep/api.py:L379](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L379)）。

**1a 分块直方图**（[moonep/planning.py:L426-L444](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L426-L444)）：每个 vblock 开始先清零共享内存直方图，线程对块内条目逐个做 CTA 作用域原子累加，最后把直方图写入全局 `vblocks_histogram[vb, :]`（行内连续，写回合并访存）：

```python
chunk = vb * BLOCK_SIZE_P2
for p in cutlass.range(tid, BLOCK_SIZE_P2, num_threads):
    off = chunk + p
    if off < N:
        expert = topk_in[off]
        cute.arch.atomic_add(elem_ptr(s_histogram, expert), 1, scope="cta")
...
for e in cutlass.range(tid, E, num_threads):
    vblocks_histogram[vb, e] = s_histogram[e]
```

**1b vblock 前缀和**（[moonep/planning.py:L446-L457](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L446-L457)）：`grid_sync` 之后，专家维被切成 32 对齐的连续段分给各 CTA；每条线程串行扫描一个专家的所有 vblock，把计数原地改写为排他前缀。此后 `vblocks_histogram[vb, e]` 的语义变为「vblock `vb` **之前**的同专家条目数」：

```python
for e in cutlass.range(e_lo + tid, e_hi, num_threads):
    cumsum = 0
    for vb in cutlass.range_constexpr(num_vblocks):
        v = vblocks_histogram[vb, e]
        vblocks_histogram[vb, e] = cumsum
        cumsum += v
```

**expoff 专家基址**（[moonep/planning.py:L460-L464](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L460-L464)）：把本 rank 的 `tpe` 载入共享内存，用一个 warp 完成沿专家维的排他前缀和（`warp_exclusive_scan_e`，[moonep/planning.py:L172-L189](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L172-L189)，lane 分段驻寄存器 + warp shuffle 的经典 scan）。它与 passB 里的 `s_expoff`（L980–L985）是同一个量、两处各算一遍。

**passA 散射**：每个 vblock 开始时载入跨块基址并清零 warp 计数格（[moonep/planning.py:L478-L486](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L478-L486)），每线程按 `p = warp*(32*IPT) + i*32 + lane` 预取 4 个条目的专家号到寄存器（越界条目填哨兵 `E`，L466–L476）。核心的 warp `match_any` 加速在 [moonep/planning.py:L488-L497](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L488-L497)：

```python
for i in cutlass.range_constexpr(IPT):
    peers = match_any_b32(my_e[i])                    # warp 内同值 lane 掩码
    cell = elem_ptr(s_warp_counts, (my_e[i], warp))   # 该 (专家, warp) 的计数格
    base = cute.arch.load(cell, Int32)
    ww.append(base + Int32(cute.arch.popc(peers & lanes_lt)))  # 我的 warp 内排位
    cute.arch.sync_warp()
    if (peers & lanes_lt) == Uint32(0):               # 只有最低 peer lane 执行写
        cute.arch.store(cell, base + Int32(cute.arch.popc(peers)))
    cute.arch.sync_warp()
```

这实际上是**手工展开的 warp 聚合原子递增**：同值 lane 一次 `match.any` + `popc` 就拿到各自连续的排位（读一次、无原子）；每轮只有「左边没有同值 lane」的那一个线程写回一次计数格，两道 `sync_warp` 分别保证「先读旧值再写」与「写完下轮再读」。相比 1a 中每条目一次 `atomic_add`，这里换掉了原子操作，还免费得到确定性的先后序（lane 序即排位序）——排序稳定性在 warp 级由此实现。`s_warp_counts` 形状为 `(E+1, NUM_WARPS+1)`：`E+1` 行容纳哨兵值 `E`（越界条目也参与 match，但最终散射被 `if ei < E` 过滤）；`WST = NUM_WARPS + 1` 的 `+1` 使行 stride 为奇数 17，让相邻专家行的同列访问错开共享内存 bank（源码未注释，这是常见的 bank-conflict 规避手法，属合理推断）。

**块内 warp 基址与最终散射**（[moonep/planning.py:L500-L515](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L500-L515)）：对每个专家把 `s_warp_counts[e, w]` 改写为跨 warp 排他前缀（warp 基址），随后每线程把 4 个条目一次性散射到位：

```python
for i in cutlass.range_constexpr(IPT):
    ei = my_e[i]
    if ei < E:
        within = s_warp_counts[ei, warp] + ww[i]
        sp = s_histogram[ei] + s_block_prefix[ei] + within
        order_out[sp] = chunk + my_p[i]
```

`s_histogram[ei]` 此刻存的是 expoff（L460–L464 已扫描为排他前缀），`s_block_prefix[ei]` 是当前 vblock 的跨块基址——四级偏移在此汇合。

**谁来排谁的数据**（[moonep/planning.py:L961-L972](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L961-L972)）：rank 0 正串行执行 Phase A/B/C（规划拥有者），无暇排自己的 topk，于是这份工作外包给 rank 1——Phase A 期间 rank 0 已把自己的 `topk`/`tpe` 推进 rank 1 chunk 的 `TOPK0`/`TPE0` 区（L606–L608），rank 1 此刻对这份快照再跑一次 `run_c1`，产出 `order0` 并经对称内存拷回 rank 0 的 `ORDER` 区：

```python
if cutlass.const_expr(R > 1):
    if rank != 0:
        self.run_c1(topk, order, tpe, lh, s_hist, s_bp, scratch, bar_p, num_sms, pid, tid)
        if rank == 1:
            tk0 = cute.make_tensor(meta.iterator + (rank * ms + TOPK0_OFF), ...)
            tp0 = cute.make_tensor(meta.iterator + (rank * ms + TPE_OFF), ...)
            order0 = cute.make_tensor(meta.iterator + (rank * ms + ORDER0_OFF), ...)
            self.run_c1(tk0, order0, tp0, lh, s_hist, s_bp, scratch, bar_p, num_sms, pid, tid)
            copy_v4_remote(meta, ORDER_OFF, order0, N, pid, tid, num_threads, num_sms)  # 写进 rank 0 的 chunk
else:
    self.run_c1(topk, order, tpe, lh, s_hist, s_bp, scratch, bar_p, num_sms, pid, tid)
```

排序与 rank 0 的规划阶段在时间上重叠，是「rank 1 打双份工」换来的负载均衡。

#### 4.2.4 代码实践

用 PyTorch 逐步复现 `run_c1` 的产物 `order`，并与 `torch.argsort(..., stable=True)` 对照（示例代码，纯 CPU 可运行）：

1. **实践目标**：验证「vblock 直方图 + vblock 前缀 + 顺序散射」确实等价于稳定计数排序；核内线程布局（`warp*(32*4) + i*32 + lane`）恰是 `p` 的升序，因此块内简单的顺序计数即可复现块内序。
2. **操作步骤**：

```python
import torch

def run_c1_reference(topk_flat, tpe, E, block_size=2048):
    """复现 run_c1：返回 order（稳定计数排序的置换数组，位置 -> 平铺偏移）。"""
    N = topk_flat.numel()
    num_vblocks = (N + block_size - 1) // block_size
    vhist = torch.zeros(num_vblocks, E, dtype=torch.long)          # 1a
    for vb in range(num_vblocks):
        chunk = topk_flat[vb * block_size:(vb + 1) * block_size]
        vhist[vb] = torch.bincount(chunk, minlength=E)
    vpfx = vhist.cumsum(0) - vhist                                  # 1b：沿 vb 排他前缀
    exp_base = tpe.cumsum(0) - tpe                                  # expoff：沿专家排他前缀
    order = torch.empty(N, dtype=torch.long)                        # passA：散射
    for vb in range(num_vblocks):
        lo = vb * block_size
        cnt = torch.zeros(E, dtype=torch.long)                      # 块内顺序计数
        for p in range(min(block_size, N - lo)):
            e = int(topk_flat[lo + p])
            order[int(exp_base[e]) + int(vpfx[vb, e]) + int(cnt[e])] = lo + p
            cnt[e] += 1
    return order

E, N = 32, 5000                                 # 故意不整除，覆盖最后一个短块
topk = torch.randint(0, E, (N,))
tpe = torch.bincount(topk, minlength=E)
order = run_c1_reference(topk, tpe, E)
print(torch.equal(order, torch.argsort(topk, stable=True)))        # 逐元素对照
```

3. **需要观察的现象**：对照打印 `True`；把 `stable=True` 去掉（非稳定 argsort）在某些种子下应观察到 `False`——同专家条目的相对顺序被打乱。
4. **预期结果**：`True`。核内 warp/lane 布局给出的块内次序就是 `p` 升序，与稳定排序「同键按原顺序」一致。脚本输出「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 1b（vblock 前缀和）整步去掉，散射会发生什么？

**答案**：每个 vblock 都会从「块内序号 0」开始往 `expoff[e] + 0 + 块内序号` 的位置散射，不同 vblock 的同专家条目争夺同一段位置，`order_out` 同一格被多次覆盖且留下空洞，排序结果错误。跨块基址正是多块计数排序区别于单块版的关键增量。

**练习 2**：`match_any` 版散射相对「每条目一次共享内存 atomicAdd」省在哪里？

**答案**：atomicAdd 版每条目一次原子读-改-写，且原子完成顺序不确定，拿不到稳定的先后序；`match_any` 版中 warp 内同值 lane 共享一次读，排位由 `popc(peers & lanes_lt)` 纯位运算得出，每 (专家, warp, 轮) 只有最低 peer lane 写一次计数格——原子完全消失，且排位天然按 lane 序确定，稳定性免费获得。

**练习 3**：为什么 rank 0 不排序自己的 topk？

**答案**：rank 0 是规划拥有者，正串行执行 Phase A/B/C 并发布结果，若再排自己的数据会拉长关键路径。rank 1 在 Phase A 期间相对空闲，就地把 rank 0 推来的 topk/tpe 快照（rank 1 chunk 的 TOPK0/TPE0 区）排好并远程写回 rank 0 的 ORDER 区，两条路径在后续的跨 rank 屏障（L979）处汇合——一次教科书式的任务外包。

### 4.3 passB：固定步数二分查找与 src_info 发布

#### 4.3.1 概念说明

有了 `order`，映射只剩查表。核心一步是在 `alloc_cumsum[e, ·]` 这一行单调不减数组上定位 \(g\)。GPU 上有两种写法：

- **数据依赖的 while 循环**：各线程找到目标所需步数不同，同 warp 线程分裂成不同执行路径（warp 分歧），迭代次数被拖到最坏者；
- **固定步数循环**：步数在编译期定为 `LOG2_R = max(ceil(log2(R+1)), 1)`，循环完全展开成直线代码，所有线程同步进退，零分歧。

MoonEP 选择后者——`R` 至多 128（dst 规范化的位集断言，L1282–L1284），`LOG2_R` 至多 7 步，用编译期常量换掉运行期分歧非常划算。辅助函数见 [moonep/planning.py:L114-L116](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L114-L116)：

```python
def log2_r(R):
    """Return max(ceil(log2(R + 1)), 1), for fixed-trip-count rank binary search."""
    return max(R.bit_length(), 1)
```

**src_info 溯源**是 passB 的第二个产出。目的 rank \(d\) 的 `src_info` 切片长为 `NvS`（[moonep/api.py:L328-L333](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L328-L333)，`meta_chunk_logical = SRC_INFO_OFF + NvS`），槽位与接收缓冲行一一对应。所有源 rank 在 passB 里经对称内存**远程直写**目的 rank 的切片，写入值 \(r \cdot NvS + offv\) 与 `dst` 同构。没有被任何条目占据的槽位（空段、padding 行）保持清零阶段写入的 **`-1` 哨兵**——dispatch 的 dedup builder（u4-l3）扫描本地切片时据此跳过空槽。

**顺序约束**：src_info 必须遵循「全员先清零自己的切片 → 跨 rank 屏障 → 再互相远程写」。若顺序颠倒，后到的清零会抹掉别的 rank 刚发布的溯源。清零与屏障在 [moonep/planning.py:L973-L979](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L973-L979)：

```python
# Clear this rank's src_info slice before all ranks publish fresh slot
# provenance into destination-rank slices below. src_info mirrors dst's
# rank-stride encoding: src_rank * NvS + offv; -1 is the empty-slot
# sentinel. offv is always in [0, N), and NvS >= N.
for idx in cutlass.range(pid * num_threads + tid, NvS, num_sms * num_threads):
    meta[rank * ms + SRC_INFO_OFF + idx] = Int32(-1)
cross_rank_barrier(meta, ms, BARRIER_OFF, rank, R, bar_p, num_sms, num_threads, tid)
```

#### 4.3.2 核心流程

passB 单条目的处理序列：

```text
state: lo = 0, hi = R, pc = 0     # 不变式：答案 d ∈ [lo, hi]，pc = alloc_cumsum[e, lo-1]（lo=0 时取 0）
repeat LOG2_R 次:
    mid = (lo + hi) >> 1
    ac  = alloc_cumsum[e, mid]
    若 ac > g:  hi = mid              # 答案在左半
    否则:       lo = mid + 1; pc = ac # 答案在右半，pc 记住新区间左端
# 循环结束时 lo == d，pc == alloc_cumsum[e, d-1]
loff = expert_off[d, e] + (g - pc)
dst[offv] = d·NvS + loff
src_info[d 的切片 + loff] = r·NvS + offv     # 远程写
```

二分不变式保证它与 `searchsorted(alloc_cumsum[e], g, right=True)` 完全同语义；`pc` 的作用是把「段内偏移」一并带出来，查完 rank 不必再查一次前缀。每个条目只需常数次访存（order、topk、三张表、写 dst、远程写 src_info），整个 passB 是 \(O(N \log R)\) 且完全并行。

passB 之后还有一道跨 rank 屏障（[moonep/planning.py:L1074-L1077](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1074-L1077)），注释点明用意：让所有 rank 的 src_info 远程写在**任何** rank 的 fresh dispatch builder 读取本地切片之前全部可见。最后一小步是 dst 重复规范化——同一 token 的多个 top-k 落到同一目的 rank 时改写为 `-raw_dst - 1`，用两个 int64 位集判重（[moonep/planning.py:L1079-L1113](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1079-L1113)）。负数编码的完整语义留给 u3-l5，本讲只需知道 raw dst 在此诞生。

#### 4.3.3 源码精读

**视图准备**（[moonep/planning.py:L980-L1001](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L980-L1001)）：passB 在**每个** rank 上执行（规划表经组播已在各 rank 的 PLAN 区持有相同副本，见 u3-l6）。`s_expoff` 从本 rank `tpe` 重算专家基址；三张查表以 `plo = rank * ms + PLAN_OFF` 为基址建立视图。注意 `alloc_cumsum` 是 `[E, R]` 行主序（专家为行），与 `tpe_cumsum`、`expert_off` 的 `[R, E]` 布局刻意相反——Phase B 的注释（L703–L704）说明这正是为了让本步按条目连续地读一行：

```python
s_expoff = cute.make_tensor(s_hist.iterator, cute.make_layout((E,)))
for e in cutlass.range(tid, E, num_threads):
    s_expoff[e] = tpe[e]
cute.arch.barrier()
warp_exclusive_scan_e(s_expoff, E, tid)
```

**主循环的工作划分与全局序号**（[moonep/planning.py:L1048-L1059](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1048-L1059)）：`seg = ceil(N / num_sms)`，CTA `pid` 认领有序位置区间 \([sbeg, send)\)，线程每次跨 `num_threads * 4` 个条目、组内 `idx = base + i * BLOCK_DIM_P2`。与 run_c1 不同，**线程拿哪些条目不影响结果**——条目序号来自 `idx` 本身而非执行顺序，这是「先排序、再顺序扫描」带来的解耦：

```python
offv = order_in[idx]                                # 有序位置 -> 平铺偏移
expert_idx = topk_by_off[offv]
prev = 0
if rank > 0:
    prev = tpe_cumsum_view[rank - 1, expert_idx]    # 跨 rank 基数（Phase A 产物）
global_rank = prev + (idx - s_expoff[expert_idx])   # 全局序号 g
```

`idx - s_expoff[expert_idx]` 就是 4.2 排序结果携带的「本 rank 内专家内序号」——排序的收益在此兑现，无需任何每条目计数器。

**固定步数二分**（[moonep/planning.py:L1060-L1065](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1060-L1065)）：

```python
lo = 0; hi = R; pc = 0
for bin_step in cutlass.range_constexpr(LOG2_R):
    mid = (lo + hi) >> 1
    ac = alloc_cumsum_view[expert_idx, mid]
    if ac > global_rank: hi = mid
    else: lo = mid + 1; pc = ac
```

`range_constexpr` 让 `LOG2_R` 在编译期展开为固定指令序列。

**落位与发布**（[moonep/planning.py:L1066-L1073](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/planning.py#L1066-L1073)）：

```python
bo = expert_off_view[lo, expert_idx]                 # 段基址（Phase C 产物）
dst_out[offv] = lo * NvS + bo + (global_rank - pc)   # 编码进单个 int32
# Publish source provenance for the dispatch builder. This
# mirrors dst's rank-stride encoding, but points back to the
# source rank and source flat top-k offset.
src_val = rank * NvS + offv
loff = bo + (global_rank - pc)
meta[lo * ms + SRC_INFO_OFF + loff] = src_val        # 对称内存远程直写目的 rank 切片
```

注意 `dst_out` 按 `offv`（token 侧）索引写回本 rank 的 plan，而 `src_info` 按 `loff`（槽位侧）索引写入目的 rank 的 meta 切片——两侧编码互为镜像。

#### 4.3.4 代码实践

把内核的二分查找逐句翻译成 Python，与向量化「计数法」在随机数据上对拍（示例代码，纯 CPU 可运行）：

1. **实践目标**：验证固定步数二分与 `searchsorted` 语义完全一致（含零宽区间），并复现 `dst`/`src_info` 的成对产出。
2. **操作步骤**：

```python
import torch

def pass_b_kernel_style(topk_flat, tpe_cumsum, alloc_cumsum, expert_off, r, R, NvS, E):
    """逐条目模拟内核 passB：固定步数二分查找版本。返回 (dst, src_info)。"""
    LOG2_R = max(R.bit_length(), 1)
    tpe_own = tpe_cumsum[r] - (tpe_cumsum[r - 1] if r > 0 else 0)
    expoff = torch.cat([torch.zeros(1, dtype=torch.long), tpe_own.cumsum(0)[:-1]])
    order = torch.argsort(topk_flat, stable=True)          # 4.2 的排序产物
    dst = torch.empty(topk_flat.numel(), dtype=torch.long)
    src_info = {}                                          # 槽位 key -> 溯源
    for idx in range(topk_flat.numel()):
        offv = int(order[idx]); e = int(topk_flat[offv])
        g = (int(tpe_cumsum[r - 1, e]) if r > 0 else 0) + (idx - int(expoff[e]))
        lo, hi, pc = 0, R, 0
        for _ in range(LOG2_R):                            # 固定步数，与内核一致
            mid = (lo + hi) >> 1
            ac = int(alloc_cumsum[e, mid])
            if ac > g: hi = mid
            else: lo, pc = mid + 1, ac
        loff = int(expert_off[lo, e]) + (g - pc)
        dst[offv] = lo * NvS + loff
        src_info[lo * NvS + loff] = r * NvS + offv         # 远程发布的本地模拟
    return dst, src_info

def pass_b_direct(topk_flat, tpe_cumsum, alloc_cumsum, expert_off, r, NvS):
    """向量化版本：计数法等价 searchsorted(..., right=True)。"""
    e = topk_flat
    order = torch.argsort(e, stable=True)
    sorted_e = e[order]
    _, counts = torch.unique(sorted_e, return_counts=True)
    starts = counts.cumsum(0) - counts
    pos = torch.arange(e.numel())
    group = torch.repeat_interleave(torch.arange(len(counts)), counts)
    local = torch.empty_like(pos); local[order] = pos - starts[group]   # 专家内序号
    prev = torch.zeros_like(tpe_cumsum[0]) if r == 0 else tpe_cumsum[r - 1]
    g = prev[e] + local                                    # 全局序号
    ac = alloc_cumsum[e]                                   # [条目数, R]
    dest = (ac <= g.unsqueeze(1)).sum(1)                   # 右侧 searchsorted
    pc = torch.where(dest > 0,
                     ac.gather(1, (dest - 1).unsqueeze(1)).squeeze(1),
                     torch.zeros_like(g))
    return dest * NvS + expert_off[dest, e] + (g - pc)

torch.manual_seed(0)
E, R, n = 16, 4, 128
for trial in range(20):
    flats = [torch.randint(0, E, (n,)) for _ in range(R)]  # 每个 rank n 个条目
    tpe = torch.stack([torch.bincount(f, minlength=E) for f in flats])
    counts = tpe.sum(0)
    alloc = torch.zeros(E, R, dtype=torch.long)            # 随机合法 alloc（允许空区间）
    for e_ in range(E):
        if counts[e_] > 0:
            parts = torch.multinomial(torch.ones(R) / R, int(counts[e_]), replacement=True)
            alloc[e_] = torch.bincount(parts, minlength=R)
    alloc_cumsum = alloc.cumsum(1)
    NvS = int(alloc.sum(0).max())                          # 玩具布局只需容量足够
    expert_off = torch.zeros(R, E, dtype=torch.long)       # padding=1：段首 = 沿专家排他前缀
    for d in range(R):
        col = alloc[:, d]
        expert_off[d] = col.cumsum(0) - col
    tpe_cs = tpe.cumsum(0)
    for r in range(R):
        d_bin, _ = pass_b_kernel_style(flats[r], tpe_cs, alloc_cumsum, expert_off, r, R, NvS, E)
        d_vec = pass_b_direct(flats[r], tpe_cs, alloc_cumsum, expert_off, r, NvS)
        assert torch.equal(d_bin, d_vec), f"trial {trial} rank {r} 不一致"
print("20 组随机对拍全部通过")
```

3. **需要观察的现象**：终端打印 `20 组随机对拍全部通过`；若故意把 `LOG2_R` 改成 `R.bit_length() - 1`（少一步），部分 trial 应触发断言——可自行实验体会固定步数下界的必要性。
4. **预期结果**：断言全部通过，说明内核的固定步数二分与参考实现的 `searchsorted` 完全同语义。玩具 `alloc` 随机且不满足完美均衡不变量，只影响「每 rank 收多少」，不影响查表机制本身的正确性；完整均衡版见第 5 节。脚本输出「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`R = 8` 时 `LOG2_R` 是几？为什么这个步数一定够？

**答案**：`log2_r(8) = 8.bit_length() = 4`。初始区间 \([0, 8)\) 长 8，每步减半，4 步后长度 \(8/2^4 < 1\)，即收敛到 \(lo == hi == d\)。一般地 `R.bit_length() = ceil(log2(R+1))` 保证收敛，`max(..., 1)` 兜住 `R = 1` 的边界。

**练习 2**：二分结束时 `pc` 等于什么？`g - pc` 又是什么？

**答案**：`pc = alloc_cumsum[e, d-1]`（`d = 0` 时为 0），即被选中区间的左端点——排在「专家 \(e\) 去往 rank \(d\)」条目之前的全局条目总数。因此 `g - pc = seg_pos`，是该条目在目的 rank 上专家 \(e\) 段内的偏移。

**练习 3**：src_info 的清零为什么放在 passB 之前、且紧跟一道跨 rank 屏障？

**答案**：src_info 切片属于**目的** rank，写入方却是**所有**源 rank。若某 rank 还在清零自己的切片时别的 rank 已开始远程写，清零会覆盖刚发布的溯源。屏障把「全员清零」与「全员发布」排成全序：先各清各的 `-1`，屏障汇合，再互相写入——恰好是「写后不能被抹除」的最小顺序约束。

## 5. 综合实践

把本讲两块内容串成一条完整链路：**随机偏置路由 → Phase A/B 均衡 → Phase C 布局 → passB 映射 → 全面断言**。这个脚本就是「规划器去掉 GPU 并行外壳」后的串行参考，跑通它等于亲手实现了一遍 u3-l2～u3-l4 的算法主干（示例代码，纯 CPU 可运行，复用 4.3.4 的两个映射函数）：

```python
import torch

# ---------- 配置 ----------
S, K, E, R = 512, 4, 32, 4
token_padding, bias_temp, B = 8, 3.0, E // R
epn, N, CAP = E // R, S * K, S * K
NvS = CAP + (token_padding - 1) * 2 * epn        # 与 api.py 的 NvS 公式一致

def make_topk(S, K, E, bias_temp, seed):          # 偏置路由：温度拉偏 + Gumbel top-k
    gen = torch.Generator().manual_seed(seed)
    logits = torch.randn(E, generator=gen) * bias_temp
    noise = -torch.log(-torch.log(torch.rand(S, E, generator=gen)))
    return torch.argsort(logits + noise, dim=1, descending=True)[:, :K]

def phase_ab(tpe):                                # surplus/deficit 平衡 + 专家展开（u3-l2/l3）
    tpe_cumsum = tpe.cumsum(0)
    expert_count = tpe_cumsum[-1]
    group_tokens = torch.stack([expert_count[h * epn:(h + 1) * epn].sum() for h in range(R)])
    balance = group_tokens - CAP
    alloc = torch.zeros(E, R, dtype=torch.long)
    for e in range(E):
        alloc[e, e // epn] = expert_count[e]
    z = torch.zeros(R, R, dtype=torch.long)
    while True:
        h, u = int(balance.argmax()), int(balance.argmin())
        if balance[h] <= 0: break
        move = int(-balance[u]); z[h, u] = move
        balance[h] -= move; balance[u] = 0
    for h in range(R):
        es = h * epn
        remaining = expert_count[es:es + epn].clone(); quotas = z[h].clone()
        while True:
            d = int(quotas.argmax()); quota = int(quotas[d])
            if quota <= 0: break
            le = int(remaining.argmax()); e = es + le; rem = int(remaining[le])
            take = min(rem, quota)
            alloc[e, d] += take; alloc[e, h] -= take
            remaining[le] -= take; quotas[d] -= take
    return tpe_cumsum, alloc

def phase_c(alloc):                               # top-B 预取 + padded 段布局（u3-l3）
    expert_off = torch.zeros(R, E, dtype=torch.long)
    etc = torch.full((R, B), -1, dtype=torch.long)
    for d in range(R):
        ls, le = d * epn, (d + 1) * epn
        remote = [e for e in range(E) if alloc[e, d] > 0 and not ls <= e < le]
        remote.sort(key=lambda e: (int(alloc[e, d]), e), reverse=True)
        for b, e in enumerate(remote[:B]): etc[d, b] = e
        pref = {int(x) for x in etc[d] if x >= 0}
        start = 0
        for g in range(E + B):
            cnt, eid = 0, -1
            if g < E:
                if g not in pref: cnt, eid = int(alloc[g, d]), g
            else:
                sel = int(etc[d, g - E])
                if sel >= 0: cnt, eid = int(alloc[sel, d]), sel
            if cnt > 0:
                expert_off[d, eid] = start
                start += -(-cnt // token_padding) * token_padding
        assert start <= NvS, f"rank {d} 布局 {start} 超出 NvS={NvS}"
    return expert_off

topk_all = [make_topk(S, K, E, bias_temp, seed=100 + r) for r in range(R)]
tpe = torch.stack([torch.bincount(t.reshape(-1), minlength=E) for t in topk_all])
tpe_cumsum, alloc = phase_ab(tpe)
alloc_cumsum, expert_off = alloc.cumsum(1), phase_c(alloc)

assert torch.equal(alloc.sum(1), tpe_cumsum[-1])            # 守恒：每专家条目数不变
assert torch.equal(alloc.sum(0), torch.full((R,), CAP))     # 完美均衡：每 rank 恰收 S*K

dst_all, src_info = [], {}
for r in range(R):
    flat = topk_all[r].reshape(-1)
    d_bin, si = pass_b_kernel_style(flat, tpe_cumsum, alloc_cumsum, expert_off,
                                    r, R, NvS, E)
    assert torch.equal(d_bin, pass_b_direct(flat, tpe_cumsum, alloc_cumsum,
                                            expert_off, r, NvS))
    dst_all.append(d_bin); src_info.update(si)

slots = torch.cat(dst_all)
assert slots.numel() == R * N and torch.unique(slots).numel() == R * N   # 槽位全局无冲突
for d in range(R):
    got = int((slots // NvS == d).sum())
    assert got == CAP, f"rank {d} 收到 {got} != CAP={CAP}"
print(f"全部断言通过：R*N={R * N} 个条目各得其所，每 rank 恰收 {CAP}")
```

操作步骤：把 4.3.4 的 `pass_b_kernel_style` / `pass_b_direct` 与上面的主脚本存为 `passb_tour.py` 运行。需要观察的现象：三条结构性断言（守恒、完美均衡、每 rank 恰收 CAP）与槽位唯一性断言全部通过；把 `bias_temp` 从 3.0 调到 0.0（均匀路由）再跑，断言依然通过——均衡算法对输入分布不敏感。预期结果：打印 `全部断言通过...`（待本地验证）。若把 `token_padding` 改成 1，`NvS` 变小，`phase_c` 的布局断言在某些偏置种子下可能失败——这正好复现 u3-l3 讲过的「padding 余量保证布局放得下」的必要性。

## 6. 本讲小结

- **排序是序号的载体**：`run_c1` 用「vblock 直方图 → vblock 排他前缀 → expoff 专家基址 → warp `match_any` 散射」四级偏移完成多 CTA 稳定计数排序，产出置换数组 `order`；passB 按有序位置扫描时，专家内序号免费由 `idx - expoff[e]` 给出。
- **`match_any` 是免原子的聚合递增**：warp 内同值 lane 一次 match + popc 得到连续排位，每 (专家, warp, 轮) 只有一次读和最低 lane 的一次写，既消灭原子又自带稳定序。
- **rank 0 的排序外包给 rank 1**：Phase A 期间 rank 0 把 topk/tpe 推到 rank 1 的 TOPK0/TPE0 区，rank 1 打双份工排完经对称内存写回 rank 0 的 ORDER 区，两条路径在屏障汇合。
- **映射 = 全局序号 + 两张表**：\(g = \text{tpe\_cumsum}[r{-}1, e] + \ell\) 在 `alloc_cumsum[e, ·]` 上做 `LOG2_R` 步固定二分得目的 rank，`expert_off` 给段基址，`dst = d \cdot NvS + \text{loff}` 单 int32 编码全部路由信息。
- **src_info 与 dst 镜像同构**：`src_rank * NvS + offv` 经对称内存远程写进目的 rank 切片，`-1` 哨兵标空槽；「全员清零 → 屏障 → 互相发布 → 屏障」的两道屏障定出全序，供 dispatch 的 dedup builder 安全消费。
- 全过程驻留 GPU、零宿主同步——这是 MoonEP「静态形状」承诺在规划器内部的落实。

## 7. 下一步学习建议

下一讲 **u3-l5（去重编码：负数 dst 与重复组结构）** 顺着本讲结尾的规范化继续：同一 token 的多个 top-k 落到同一目的 rank 时 `dst` 如何改写为 `-raw_dst - 1`、只传权重不传 payload，以及 `dup_groups/dup_loffs/dup_counts` 三件套的数据契约。之后：

- **u3-l6（计划发布与同步原语）**：补齐本讲只引用未展开的 `multimem_st` 组播发布、`cross_rank_barrier` 自复位屏障与 `grid_sync` 的实现细节。
- **u4-l3（dispatch 的零填充 warp 与去重构建 warp）**：看 src_info 的消费方——builder warps 如何扫描本地切片、用 packed 编码原子构建去重结构。
- 源码方面，建议回到 [tests/planning_reference.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/planning_reference.py) 把 Part 3（L239–L295）读完：它把本讲的 raw dst 与 u3-l5 的负数编码、去重结构串成一条参考链，是两讲之间的最佳桥梁。
