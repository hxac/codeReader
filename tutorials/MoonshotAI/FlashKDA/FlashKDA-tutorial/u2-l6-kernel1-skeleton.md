# u2-l6 Kernel 1 骨架：tile 映射、varlen 前缀和与单发 TMA 加载

## 1. 本讲目标

本讲进入 Kernel 1（`_flash_kda_fwd_prepare`，下文简称 K1）的**骨架部分**——即 `csrc/smxx/fwd_kernel1.cuh` 中从 tile 前缀和辅助 kernel 到 TMA 数据就绪为止的代码（约第 86–263 行）。计算主体（L2 归一化、门控 cumsum、decay_apply 等）留给下一讲 u2-l7。

学完本讲你应当能够：

1. 说清 K1 的 grid 到工作的映射：`blockIdx` 如何变成 `(global_tile_idx, head_idx)`，varlen 与 batched 两种模式各走哪条路径。
2. 手工模拟 varlen 模式下 `tile_prefix` 的构建，以及 CTA 内 O(log N) 二分查找的全过程。
3. 解释 `total_tiles` 上界公式为什么会让一部分 CTA 直接 early return，以及为什么 batched 模式永远不会触发 early return。
4. 解释「只有线程 0 发起全部 TMA 拷贝、事务字节数一次声明」的单发（single-shot）加载模式，并与 K2 的多级流水线对比。
5. 说出 `fence_barrier_init` / `fence_view_async_shared` 两道代理围栏各自解决什么可见性问题。

## 2. 前置知识

本讲假设你已读过 u2-l5（TMA 描述符、gmem 布局与 workspace 切分）。在此基础上补充三个新概念。

**① tile（块）与 CHUNK。** FlashKDA 把时间轴切成 `CHUNK = 16` 个 token 一块。K1 的每个 CTA 负责一个 `(tile, head)` 组合的全部准备工作。一个长度为 \(\text{len}\) 的序列占

\[
\text{tiles}(\text{len}) = \left\lceil \frac{\text{len}}{16} \right\rceil
\]

个 tile。

**② mbarrier 与事务屏障（Transaction Barrier）。** SM90 的 `mbarrier` 是共享内存中的一个 8 字节同步原语，同时支持两种"完成"条件：** arrive 计数**（若干线程宣布到达）和**事务字节计数**（异步拷贝宣布落了多少字节）。`cutlass::arch::ClusterTransactionBarrier` 是 CUTLASS 对它的封装。TMA 拷贝可以和一个 mbarrier 关联：TMA 单元（异步代理）搬完数据后，会自动把字节数累加到该屏障上——这比"发起拷贝的线程再发一次 `__threadfence_block` + 旗标"高效且精确。

**③ 代理（proxy）与代理围栏。** Hopper 上有两套访问共享内存的"主体"：

- **generic proxy**：普通线程发出的 `ld.shared` / `st.shared` 指令；
- **async proxy**：TMA 单元发起的共享内存读写，以及 mbarrier 的事务完成事件。

两个代理的写入**互相默认不可见**。因此有两个方向的围栏：

- generic 写 → async 读（例如线程初始化 mbarrier，之后 TMA 要引用它）：需要 `fence.mbarrier_init`，对应 `cutlass::arch::fence_barrier_init()`；
- async 写 → generic 读（例如 TMA 把数据搬进 smem，之后普通线程要读）：需要 `fence.proxy.async.shared::cta`，对应 `cutlass::arch::fence_view_async_shared()`。

这两个方向在本讲的代码里各出现一次，缺一不可——这是被 git 历史验证过的：提交 `5fdc7b2`（"fix missing proxy fences around TMA accesses"）专门补上了 K1 中缺失的 `fence_barrier_init()`。

另外回顾两个旧知识点（u1-l4 / u2-l5）：`__syncthreads()` 让块内 256 线程互等；K1 的 grid 是 `(total_tiles, H)`、256 线程、`__launch_bounds__(256, 8)`。

## 3. 本讲源码地图

| 文件 | 本讲涉及范围 | 作用 |
| --- | --- | --- |
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L86-L263) | L86–L263 | tile 前缀和辅助 kernel + K1 主 kernel 的映射、early exit、单发 TMA 加载与等待 |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L146-L181) | L146–L181 | K1 的启动块：前缀和 kernel 的发射、grid/block 维度、动态 smem 属性 |
| [csrc/flash_kda.cpp](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181) | L176–L181 | `total_tiles` 的两种口径：varlen 上界 vs batched 精确值 |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L347-L351) | L202–L237、L347–L351 | 仅作对比：K2 的多级流水线加载（LOAD warp + `producer_acquire`） |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L86-L116) | L86–L116 | 仅作对比：`make_load_pipeline` 封装的 `PipelineTmaAsync` |

## 4. 核心概念与源码讲解

### 4.1 tile 映射与 early exit

#### 4.1.1 概念说明

K1 的并行策略是 **tile × head 全并行**：grid 的 x 轴是"全局 tile 编号"，y 轴是 head 编号。每个 CTA 拿到 `blockIdx` 后的第一件事，是回答三个问题：

1. 我属于哪条序列（`seq_idx`）？
2. 我在这条序列内的第几个 tile（`local_t`）？
3. 这个 tile 在全局 token 轴上的起点（`bos`，begin of sequence）在哪里？

batched 模式下每条序列等长，答案是纯算术；varlen 模式下序列长度藏在 `cu_seqlens` 里，需要一个查找过程（见 4.2）。此外，varlen 模式下 grid 大小按**上界**发射，多余的 CTA 必须尽早退出——这就是 early exit。

#### 4.1.2 核心流程

```text
grid = (total_tiles, H)                # fwd_launch.cu:169
for each CTA:
    global_tile_idx = blockIdx.x
    head_idx        = blockIdx.y

    if varlen:   # 需要 tile_prefix（见 4.2）
        seq_idx      = 二分查找(global_tile_idx)
        tiles_before = tile_prefix[seq_idx]
        local_t      = global_tile_idx - tiles_before
        bos, eos     = cu_seqlens[seq_idx], cu_seqlens[seq_idx+1]
    else:        # 每条序列等长 T_seq = T_total / N
        tiles_per_seq = ceil(T_seq / 16)
        seq_idx       = global_tile_idx / tiles_per_seq
        local_t       = global_tile_idx % tiles_per_seq  （减法实现）
        bos           = seq_idx * T_seq

    seq_len          = eos - bos
    t_tiles_this_seq = ceil(seq_len / 16)

    if local_t >= t_tiles_this_seq:  return   # 多余 CTA，尽早退出
```

`total_tiles` 的两种口径（host 侧计算）：

- **varlen**：上界 \(\left\lceil T_{\text{total}}/16 \right\rceil + N\)。因为每条序列的取整浪费最多凑出一个整 tile，N 条序列的总浪费严格小于 N 个 tile：

\[
\sum_{i=1}^{N}\left\lceil \frac{\text{len}_i}{16} \right\rceil \;\le\; \sum_{i=1}^{N}\left(\frac{\text{len}_i}{16}+1\right) \;=\; \frac{T_{\text{total}}}{16}+N \;\le\; \left\lceil \frac{T_{\text{total}}}{16} \right\rceil + N
\]

- **batched**：精确值 \(N \cdot \left\lceil T_{\text{seq}}/16 \right\rceil\)，此时 `local_t < tiles_per_seq` 恒成立，**early return 永远不会触发**——它是 varlen 专用的机制。

注意 workspace 分配（u2-l2 的 `get_workspace_size`）永远按上界公式预留空间，batched 模式下只是浪费一点尾部，无害。

#### 4.1.3 源码精读

先看 host 侧的口径选择：varlen 用上界，batched 用精确值。

- [csrc/flash_kda.cpp:L176-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/flash_kda.cpp#L176-L181)：`total_tiles` 的计算——varlen 分支 `((T_total + CHUNK - 1) / CHUNK + N_val)` 是上界，batched 分支 `N_val * ((T_seq + CHUNK - 1) / CHUNK)` 是精确值。

再看 kernel 侧的映射与退出：

- [csrc/smxx/fwd_kernel1.cuh:L169-L173](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L169-L173)：`global_tile_idx = blockIdx.x; head_idx = blockIdx.y;`——grid 两根轴的语义在这里定型，随后声明 `seq_idx / tiles_before / local_t / bos / eos` 等变量。

- [csrc/smxx/fwd_kernel1.cuh:L187-L195](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L187-L195)：batched（`else`）分支。`T_seq = T_total / N`、`tiles_per_seq = (T_seq + CHUNK - 1) / CHUNK`，用一次除法和一次减法得到 `seq_idx / local_t / bos / eos`，无任何内存访问。

- [csrc/smxx/fwd_kernel1.cuh:L196-L199](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L196-L199)：算出 `seq_len` 与本序列真实 tile 数 `t_tiles_this_seq`，然后 `if (local_t >= t_tiles_this_seq) return;`——多余 CTA 在触碰任何共享内存、任何 barrier 之前就退出。这个条件只依赖 `blockIdx` 派生的值，块内 256 线程**一致地**一起返回，不存在发散或"有人在等 barrier、有人已退出"的死锁风险。

- [csrc/smxx/fwd_launch.cu:L169-L172](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L169-L172)：`dim3 grid_k1(total_tiles, H)`、256 线程、动态 smem 尺寸启动 K1。`total_tiles` 正是上面 host 侧算出的口径。

- [csrc/smxx/fwd_kernel1.cuh:L120](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L120)：`__launch_bounds__(NumThreads, 8)`——要求编译器把 K1 调度到每 SM 至少 8 个 CTA。K1 的延迟隐藏策略是"用海量小 CTA 填满 GPU"，与 K2 的"少量大 CTA + 块内流水线"形成对照（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：手工模拟 varlen 模式下 grid → 工作的映射，并标出哪些 CTA 会 early return。

**操作步骤**：

1. 设 varlen 输入 `seq_lens = [7, 33, 16, 64]`，则 `cu_seqlens = [0, 7, 40, 56, 120]`，`T_total = 120`，`N = 4`，`CHUNK = 16`。
2. 按公式算每序列 tile 数：⌈7/16⌉=1、⌈33/16⌉=3、⌈16/16⌉=1、⌈64/16⌉=4，得 `tile_prefix = [0, 1, 4, 5, 9]`（真实 tile 总数 9）。
3. 算启动口径：`total_tiles = ⌈120/16⌉ + 4 = 8 + 4 = 12`，即每个 head 发射 12 个 CTA（grid=(12, H)）。
4. 对 `global_tile_idx = 0..11` 逐一确定 `(seq_idx, local_t, 是否退出)`。

**需要观察的现象 / 预期结果**（映射表，建议先自己填再对照）：

| global_tile_idx | seq_idx | local_t | 覆盖的 token（序列内） | 结果 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 0–6（仅 7 个，尾块） | 计算 |
| 1 | 1 | 0 | 0–15 | 计算 |
| 2 | 1 | 1 | 16–31 | 计算 |
| 3 | 1 | 2 | 32（仅 1 个，尾块） | 计算 |
| 4 | 2 | 0 | 0–15 | 计算 |
| 5–8 | 3 | 0–3 | 各 16 个 | 计算 |
| 9、10、11 | 3（二分退化到最后一条） | 4、5、6 | — | **early return** |

tile 9 走二分的推演：`lo=0, hi=4` → `mid=2`，`tile_prefix[2]=4 ≤ 9` → `lo=2` → `mid=3`，`tile_prefix[3]=5 ≤ 9` → `lo=3`，`lo+1=hi` 结束；`local_t = 9 - 5 = 4 ≥ t_tiles(seq 3)=4`，退出。

若无法在真机验证，上表可作为"待本地验证"的手算基准（第 5 节的综合实践会用脚本自动核对它）。

#### 4.1.5 小练习与答案

**练习 1**：batched 模式下 B=4、T_seq=20、H=任意。grid 是多少？CTA 3 映射到什么？

答案：`tiles_per_seq = ⌈20/16⌉ = 2`，`total_tiles = 4×2 = 8`（精确），grid=(8, H)。CTA 3：`seq_idx = 3/2 = 1`，`local_t = 1`，`bos = 20`；该 tile 是序列 1 的尾块，`actual_len = min(16, 20-16) = 4`。

**练习 2**：为什么 batched 模式不会触发 `local_t >= t_tiles_this_seq`？

答案：batched 的 `total_tiles = N × tiles_per_seq` 是精确值，`seq_idx = idx / tiles_per_seq < N`，`local_t = idx % tiles_per_seq < tiles_per_seq`，而 `t_tiles_this_seq` 对等长序列恰为 `tiles_per_seq`，条件永假。上界只在 varlen 口径下引入。

**练习 3**：如果把 early return 从 `if (local_t >= t_tiles_this_seq) return;` 改成放在 TMA 加载之后，会发生什么？

答案：多余 CTA 会为不存在的 tile 发起 TMA 读（读到的是相邻序列或越界数据）并执行全部计算，再把这些垃圾写进 workspace 中本不存在映射的 `ws_idx` 槽位——其中越界 TMA 可能直接非法访问。early return 必须在任何加载之前。（源码把 return 放在 L199，正是加载段 L200 之前。）

### 4.2 tile_prefix + 二分搜索

#### 4.2.1 概念说明

varlen 模式下，"全局 tile 编号 → 所属序列"是一个区间查找问题：给定前缀和数组 `tile_prefix[i]`（前 i 条序列的 tile 总数），找最大的 `lo` 使

\[
\text{tile\_prefix}[lo] \le g < \text{tile\_prefix}[lo+1]
\]

每个 CTA 都要做一次这个查找。早期实现（提交 `1ce47ea` 之前）是每个 CTA 各自对 `cu_seqlens` 做 O(N) 线性扫描——`H × total_tiles` 个 CTA 每个都读 N 个 int64，序列数大时这是映射路径的显著开销。优化方案：先跑一个微小 kernel，把每序列 tile 数的前缀和一次性写进 workspace 尾部的 `tile_prefix` 缓冲（u2-l2 已见过它的空间预留），之后每个 CTA 只需 O(log N) 的二分查找，读的是 int32 前缀数组。

为什么不在 host 上算好前缀和传进来？`cu_seqlens` 是设备端张量，host 要读它就必须做一次 D2H 同步，破坏整条流水线的异步性。用一个 1 CTA 的微小 kernel 在设备端算，代价只有一次微秒级的 kernel 发射。（这一段是设计动机分析，源码注释只说了复杂度这一层。）

#### 4.2.2 核心流程

**阶段一：`_flash_kda_build_tile_prefix`（整个 launch 只跑一次）**

```text
输入: cu_seqlens[0..N]（N 条序列的累计 token 数）, chunk = 16
输出: tile_prefix[0..N]
tile_prefix[0] = 0
acc = 0
for i in 0..N-1:                        # 串行
    slen = cu_seqlens[i+1] - cu_seqlens[i]
    acc += ceil(slen / chunk)
    tile_prefix[i+1] = acc
```

**阶段二：每个 K1 CTA 的二分查找**

```text
lo, hi = 0, N                      # 不变量: tile_prefix[lo] <= g < tile_prefix[hi]
                                    #（对多余 CTA，g >= tile_prefix[N]，不变量右端失效，
                                    # lo 会退化到 N-1，由 4.1 的 early return 兜底）
while lo + 1 < hi:
    mid = (lo + hi) >> 1
    if tile_prefix[mid] <= g: lo = mid
    else:                     hi = mid
seq_idx = lo; local_t = g - tile_prefix[lo]
```

比较用 `<=` 而非 `<` 是刻意的：当 `g` 恰好等于某条序列的起始 tile 编号时，应归属**后面的**序列（`local_t = 0`）。这也自动跳过空序列（长度 0 的序列 tile 数为 0，`tile_prefix[i] == tile_prefix[i+1]`，边界值会落到后面的非空序列上）。空序列在 K2 侧也有对应处理（`5fdc7b2` 提交信息里提到 `t_tiles == 0` 的路径）。

复杂度：阶段一 O(N) 串行但只跑一次；阶段二每个 CTA O(log N)，读 ⌈log₂N⌉ 个 int32。

#### 4.2.3 源码精读

- [csrc/smxx/fwd_kernel1.cuh:L86-L104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L86-L104)：前缀和 kernel 全貌。L86–L88 的注释直接说明了动机——"map global_tile_idx -> seq_idx with an O(log N) binary search instead of an O(N) linear scan per CTA"。注意它**不是**逐线程并行的 scan，而是 `if (threadIdx.x == 0)` 里一个纯串行循环（L95–L103）：N 最多几千，串行写 N+1 个 int32 比组织并行 scan 更简单且足够快。

- [csrc/smxx/fwd_launch.cu:L164-L167](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L164-L167)：`if constexpr (IsVarlen)` 时以 `<<<1, 32, 0, stream>>>` 发射——单 block、32 线程、同一 stream。同 stream 的先后发射保证了 K1 所有 CTA 看到的 `tile_prefix` 已写好，无需额外同步。batched 模式整个跳过。

- [csrc/smxx/fwd_kernel1.cuh:L175-L186](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L175-L186)：K1 内 varlen 分支。L176–L181 是逐行对应上面伪代码的二分；L182–L185 取出 `seq_idx / tiles_before / local_t` 并从 `cu_seqlens` 读出 `bos / eos`。`tile_prefix` 作为最后一个 kernel 参数传入（签名见 L140）。

- [csrc/smxx/fwd_kernel1.cuh:L516](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L516)：顺带记住 K1 末尾写 workspace 用的寻址 `ws_idx = head_idx * total_tiles + global_tile_idx`——注意这里用的是**启动口径**的 `total_tiles`，K2 读取侧（u2-l8）用同一公式，这是 workspace 位一致契约的一半。

#### 4.2.4 代码实践

**实践目标**：用 PyTorch 内置的 `searchsorted` 交叉验证手写二分与"正确答案"一致。

**操作步骤**：运行下面的小脚本（纯 CPU 即可，无需 GPU 与编译）。

```python
# searchsorted_check.py（示例代码）
import torch

CHUNK = 16
seq_lens = [7, 33, 16, 64]
cu = torch.tensor([0] + list(torch.tensor(seq_lens).cumsum(0)), dtype=torch.int64)
tiles = torch.tensor([(l + CHUNK - 1) // CHUNK for l in seq_lens])
prefix = torch.cat([torch.zeros(1, dtype=torch.int64), tiles.cumsum(0)])
N = len(seq_lens)

def kernel_bs(g):                      # 逐行复刻 kernel 的二分
    lo, hi = 0, N
    while lo + 1 < hi:
        mid = (lo + hi) >> 1
        if int(prefix[mid]) <= g: lo = mid
        else:                     hi = mid
    return lo, g - int(prefix[lo])

for g in range(9):                     # 9 个真实 tile
    seq, local = kernel_bs(g)
    ref = int(torch.searchsorted(prefix[1:], g, right=True))  # 答案：最后一个 <= g 的前缀位置
    assert (seq, local) == (ref, g - int(prefix[ref])), (g, seq, local, ref)
print("prefix =", prefix.tolist(), "| 9 个 tile 全部与 searchsorted 一致")
```

**预期结果**：打印 `prefix = [0, 1, 4, 5, 9]` 并通过全部断言。`searchsorted(prefix[1:], g, right=True)` 返回"最后一个 ≤ g 的前缀"的下标（0-based 对应序列号），与 kernel 二分的语义完全一致。

**待本地验证**：具体打印格式以实际运行为准。

#### 4.2.5 小练习与答案

**练习 1**：`seq_lens = [16, 0, 16]` 时 `tile_prefix` 是什么？CTA `g=1` 映射到哪条序列？

答案：tiles = [1, 0, 1]，`tile_prefix = [0, 1, 1, 2]`。`g=1`：`mid=1` 时 `prefix[1]=1 ≤ 1` → `lo=1`；`mid=2` 时 `prefix[2]=1 ≤ 1` → `lo=2`；得 `seq_idx=2, local_t=0`——空序列 1 被正确跳过。

**练习 2**：前缀和 kernel 为什么可以只让线程 0 串行写，而不用担心成为瓶颈？

答案：它只跑一次（单 block），工作量是 N+1 个 int32 写加 N 次除法；典型 N（几千以内）在微秒级。相比之下，若让每个 K1 CTA 都 O(N) 扫描 `cu_seqlens`（int64、可能未缓存），总读取次数是 `H × total_tiles × N`，N 大时远贵于一次串行前缀和。这是"一次 O(N) 换千百次 O(log N)"的典型交易。

**练习 3**：二分查找循环里的不变量是什么？为什么初始化 `hi = N` 而不是 `N - 1`？

答案：不变量是 `tile_prefix[lo] <= g < tile_prefix[hi]`（初始 `lo=0` 成立，因为 `tile_prefix[0]=0<=g`）。`hi` 是"开区间右端"，代表"尚未确认的上界"，取 N 使循环能覆盖第 N-1 条序列；若取 N-1，当 g 属于最后一条序列时查找会提前停止并给出错误结果。

### 4.3 单发 TMA 与事务 barrier

#### 4.3.1 概念说明

K1 每个 CTA 一生只处理一个 tile，输入只需加载一次、算完一次、写出一次。因此它**不需要** K2 那种多级流水线，而是采用最朴素的"单发（single-shot）"模式：

1. **只有一个生产者线程**：`threadIdx.x == 0`（不是 `elect_one_sync`——那是按 warp 选 lane 的，见源码 L201 注释）。它负责初始化 barrier、声明事务字节数、发起全部 5 个 TMA 拷贝。
2. **事务字节数一次声明**：`arrive_and_expect_tx(kTmaTransactionBytes)` 把 5 个拷贝（q、k、beta、g、dt_bias）的总字节数一次性登记到 barrier 上；每个 TMA 完成后由 TMA 单元自动扣减。**barrier 相位翻转 = 5 个拷贝全部落地**。
3. **全块消费者**：其余 255 个线程与线程 0 一起在 `wait(0)` 上等待，等齐后过一道 `fence_view_async_shared()`，再进入计算。

这与 K2 的差别是结构性的：

| 维度 | K1（单发） | K2（多级流水线） |
| --- | --- | --- |
| 生产者 | 线程 0（块内唯一） | 专用 LOAD warp（`elect_one_sync` 选 lane） |
| smem 缓冲 | 单级（每个张量一份） | `input[3] / output[2]` 多级环形缓冲 |
| 同步原语 | 裸 `ClusterTransactionBarrier`，一次 `init/expect/wait` | `PipelineTmaAsync` 封装，按 stage `producer_acquire / consumer_wait` |
| 事务字节 | 启动前一次声明 | 每个 stage 的 barrier 各自带 expect |
| 延迟隐藏 | 靠海量 CTA 并行（grid=total_tiles×H，每 SM 8 CTA） | 靠块内 load 与 MMA 重叠 |
| 次数 | 每 CTA 一次 | 每 tile 一次、循环 N/16 次 |

K1 唯一的"重叠"技巧在 L257–L258：发起 TMA 后、等待之前，全块先去读一个标量 `expf(A_log_ptr[head_idx])`——把一次 gmem 标量读藏在 TMA 飞行时间里。

#### 4.3.2 核心流程

```text
# 常量（编译期）
kTmaTransactionBytes = cosize(QKLayout)*3*2      # q + k + g_bf16: 2048*3*2 = 12288
                      + 32*2                     # beta: 64
                      + 128*4                    # dt_bias: 512
                    = 12864 字节

# 线程 0                                     # 其余 255 线程
tma_load_barrier.init(1)                      # 等待 1 次 arrive
fence_barrier_init()                          # 让 init 对 async proxy 可见
arrive_and_expect_tx(12864)                   # arrive + 声明事务字节
copy(tma_q,   gmem tile (head, bos+local*16, 0..15, 0..127) -> smem q)
copy(tma_k,   同上 -> smem k)
copy(tma_beta, 对齐后的 32 个 bf16 -> smem beta)
copy(tma_g,   同 q 布局 -> smem g_bf16)
copy(tma_dt,  (head, 0..127) fp32 -> smem dt_bias)
                                              a_log_exp = expf(A_log[head])  # 与 TMA 重叠
                                              __syncthreads()
                                              tma_load_barrier.wait(0)       # 等 5 份拷贝全部落地
                                              fence_view_async_shared()      # async 写 -> generic 可见
                                              __syncthreads()
# 此后 256 线程一起进入 L2 归一化（u2-l7）
```

beta 的 1D TMA 对齐技巧：varlen 下 `bos` 任意，`beta_linear = head_idx*T_total + bos + local_t*16` 不保证对齐，而 TMA 的 gmem 起址必须 16 字节对齐（8 个 bf16）。做法是向下对齐到 8 元素边界发起加载（`& ~7`），消费端再用 `& 7` 的余数偏移读（见 4.3.3 最后两条链接）。smem 侧固定加载 32 个 bf16，覆盖最坏情形（余数最大 7 + 需要 16 行 = 23 ≤ 32）。这正是 u2-l2 里"beta 转置成 [H, T_total] 连续布局 + 1D TMA 免除 T 对齐约束"在 kernel 侧的兑现。

#### 4.3.3 源码精读

**事务字节预算**：

- [csrc/smxx/fwd_kernel1.cuh:L158-L161](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L158-L161)：`kTmaTransactionBytes` 的编译期公式。`cosize(QKLayout)` = 16×128 = 2048；三项合计 \(2048 \times 3 \times 2 + 32 \times 2 + 128 \times 4 = 12864\) 字节。注释标明 beta 是 "bf16, sigmoid fused"——beta 传的是激活前 logits，sigmoid 在 kernel 内做（u1-l5 的约定）。

**线程 0 的五步**：

- [csrc/smxx/fwd_kernel1.cuh:L201-L206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L201-L206)：`if (threadIdx.x == 0)` 内依次 `init(1)`（L204，期望 1 次 arrive）、`fence_barrier_init()`（L205，generic 写 → async 可见）、`arrive_and_expect_tx(kTmaTransactionBytes)`（L206，一次性凑满 arrive 计数并登记总字节数）。L201 的注释特意说明用 `threadIdx.x == 0` 而非 `elect_one_sync`（后者是按 warp 选 lane）。`fence_barrier_init` 这一行正是提交 `5fdc7b2` 补上的修复——修复前，TMA 单元可能看不到刚初始化的 barrier。

- [csrc/smxx/fwd_kernel1.cuh:L208-L220](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L208-L220)：用 `get_tma_tensor` 构造与 TMA 描述符同构的坐标张量，再以 `qk_off = g_q.layout()(head_idx, bos + local_t*CHUNK, 0)` 算出本 tile 起点，手工拼一个 `(1, CHUNK, D)` 的 gmem 子张量——u2-l5 讲过的"描述符管全局形状、偏移靠张量算术"模式。

- [csrc/smxx/fwd_kernel1.cuh:L222-L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L222-L225)：beta 的对齐三步——`beta_linear = head_idx*T_total + (bos + local_t*CHUNK)`，`beta_aligned = beta_linear & ~7`（向下对齐到 8 个 bf16 = 16 字节），从对齐地址加载 32 个元素进 smem。

- [csrc/smxx/fwd_kernel1.cuh:L231-L236](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L231-L236)：q、k、beta 三个 `cute::copy`，每个都用 `.with(reinterpret_cast<BarrierType&>(...tma_load_barrier))` 关联到同一个事务 barrier——三份拷贝的字节数共同扣减 `expect_tx` 的预算。

- [csrc/smxx/fwd_kernel1.cuh:L238-L254](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L238-L254)：第 4、5 份拷贝：g 以与 q/k 完全相同的布局载入 `g_bf16`（该 smem 缓冲与 `k_restored` 是 union，u2-l8 详述）；dt_bias 从 `[H, D]` 里切出当前 head 的 128 个 fp32。

**等待与围栏**：

- [csrc/smxx/fwd_kernel1.cuh:L257-L263](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L257-L263)：这是全讲的同步精华。`a_log_exp = expf(...)`（L258）由**全部**线程执行，与 TMA 飞行重叠；随后 L260 `__syncthreads()` 确保线程 0 的发起已完成、全块对齐；L261 `tma_load_barrier.wait(0)` 每个线程各自在 mbarrier 上阻塞等待相位 0（arrive 凑满 1 且 12864 字节全部落地）；L262 `fence_view_async_shared()` 让 TMA（async proxy）写入的 smem 对普通指令（generic proxy）可见；L263 再 `__syncthreads()`，保证没有任何线程在别人还没过围栏时就开始原地改写 q/k（下一讲的 L2 归一化直接在 smem 上原地写）。K1 的 barrier 一生只用一次，所以相位恒为 0，不像 K2 要用 `PipelineState` 翻相位。

**消费端闭环（beta 余数）**：

- [csrc/smxx/fwd_kernel1.cuh:L345](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L345)：`beta_smem_offset = (head_idx*T_total + bos + local_t*CHUNK) & 7`——与加载侧的 `& ~7` 互补，得到对齐损失掉的余数。
- [csrc/smxx/fwd_kernel1.cuh:L502](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L502)：实际消费处 `beta_tile(beta_smem_offset + i)`，从 32 个加载元素中偏移到本 tile 真正的第 i 行 beta。

**与 K2 的对照**（只需读两眼，u3-l2/l3 再展开）：

- [csrc/smxx/fwd_kernel2.cuh:L347-L351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L347-L351)：K2 的 LOAD warp 每个迭代先 `load_pipeline.producer_acquire(load_write)` 拿到一个空闲 stage，再从该 stage 的 barrier 取 `producer_get_barrier`，之后才发 TMA——每个 stage 一把 barrier、一套 expect，循环 `N/16` 次。
- [csrc/smxx/utils.cuh:L86-L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L86-L116)：K2 用的 `make_load_pipeline` 封装——`transaction_bytes / num_consumers / num_producers` 都是流水线参数，与 K1 的裸 barrier 形成繁简两极。

#### 4.3.4 代码实践

**实践目标**：验证事务字节预算公式，并在真实二进制里找到 mbarrier 指令（条件允许时）。

**操作步骤**：

1. 手算/脚本验证字节数：

```python
# tx_bytes.py（示例代码）
CHUNK, D = 16, 128
qk = CHUNK * D                      # cosize(QKLayout)
tx = qk * 3 * 2 + 32 * 2 + D * 4    # q+k+g_bf16, beta, dt_bias
print(tx)                           # 期望 12864
```

2. （需已完成 u1-l3 的编译）在编译产物中确认 mbarrier 与围栏真的存在：

```bash
so=$(python -c "import flash_kda_C as m; print(m.__file__)")
cuobjdump --dump-sass "$so" | grep -n -m 20 -i "mbarrier"
```

3. 阅读对照：打开 [csrc/smxx/fwd_kernel2.cuh:L347-L351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L347-L351)，数一数 K2 每个 stage 关联了多少个 `cute::copy`，与 K1 的 5 个对比。

**需要观察的现象 / 预期结果**：

- 步骤 1 应打印 `12864`，与 `kTmaTransactionBytes` 一致。
- 步骤 2 预期能看到 `MBARRIER.INIT` / `MBARRIER.ARRIVE.EXPECT_TX` / `MBARRIER.TRY_WAIT` 一类 SASS 指令，以及 TMA 相关指令序列（具体助记符因架构而异）。
- 步骤 3 应发现 K2 每 stage 的拷贝数不少于 8（v、beta、6 个 workspace 量）。

**待本地验证**：步骤 2、3 需要 SM90 机器与已编译的 `flash_kda_C`；本环境未运行，无法代跑。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `init(1)` 改成 `init(2)`，会发生什么？

答案：barrier 会等 2 次 arrive，但只有线程 0 调用了一次 `arrive_and_expect_tx`（含 arrive），第二次 arrive 永远不会发生，`wait(0)` 将挂死——除非再让别的线程 arrive。到达计数必须与实际的 arriver 数量严格一致。

**练习 2**：`wait(0)` 之后为什么还需要 `fence_view_async_shared()`？`__syncthreads()` 不能替代吗？

答案：`wait` 只保证 TMA 写入已完成（事务字节落地），但 TMA 写经由 async proxy，普通线程的读经由 generic proxy，两个代理间没有自动的可见性顺序；`fence.proxy.async.shared::cta` 建立"先前的 async 写 → 后续 generic 读"的顺序。`__syncthreads()` 只同步线程、不跨代理排序内存操作，替代不了这道围栏（反之亦然，所以 L262 与 L263 两个都要）。

**练习 3**：K1 为什么不像 K2 那样给输入做多级缓冲（例如 input[3]）？

答案：多级缓冲的意义在于"第 t 步计算与第 t+1 步加载重叠"，前提是同一 CTA 要处理多个时间步。K1 每个 CTA 只处理一个 tile，加载-计算-存储各只发生一次，无重叠可挖；它的吞吐靠 grid 里的海量 CTA（`total_tiles × H` 个，每 SM 最多 8 个）互相填满 SM 实现，单 CTA 内加流水线只会白费 smem。

## 5. 综合实践

**任务**：把 4.1、4.2 的映射逻辑完整落地为可回归的 Python 模拟器 `prefix_check.py`——用 PyTorch 复刻 `_flash_kda_build_tile_prefix` 与 kernel 的二分查找，再对 100 个随机 `global_tile_idx` 验证与暴力法一致（含 early-return 判定）。纯 CPU 可运行，不需要 GPU。

```python
# prefix_check.py（示例代码）
import torch

CHUNK = 16

def build_tile_prefix(cu_seqlens):
    """复刻 _flash_kda_build_tile_prefix：单线程串行前缀和（fwd_kernel1.cuh L95-L103）。"""
    N = cu_seqlens.numel() - 1
    tile_prefix = torch.zeros(N + 1, dtype=torch.int64)
    acc = 0
    for i in range(N):
        slen = int(cu_seqlens[i + 1] - cu_seqlens[i])
        acc += (slen + CHUNK - 1) // CHUNK
        tile_prefix[i + 1] = acc
    return tile_prefix

def kernel_binary_search(tile_prefix, g):
    """复刻 K1 的 O(log N) 二分（fwd_kernel1.cuh L176-L184），返回 (seq_idx, local_t)。"""
    N = tile_prefix.numel() - 1
    lo, hi = 0, N
    while lo + 1 < hi:
        mid = (lo + hi) >> 1
        if int(tile_prefix[mid]) <= g:
            lo = mid
        else:
            hi = mid
    return lo, g - int(tile_prefix[lo])

def brute_force(cu_seqlens, g):
    """暴力法：线性扫描每序列 tile 数。返回 (seq_idx, local_t)；超出真实总数返回 (None, None)。"""
    N = cu_seqlens.numel() - 1
    before = 0
    for i in range(N):
        slen = int(cu_seqlens[i + 1] - cu_seqlens[i])
        n_tiles = (slen + CHUNK - 1) // CHUNK
        if before <= g < before + n_tiles:
            return i, g - before
        before += n_tiles
    return None, None

# ---- 手算部分：seq_lens = [7, 33, 16, 64] ----
seq_lens = [7, 33, 16, 64]
cu_seqlens = torch.tensor([0] + list(torch.tensor(seq_lens).cumsum(0)), dtype=torch.int64)
tile_prefix = build_tile_prefix(cu_seqlens)
N = len(seq_lens)
actual = int(tile_prefix[-1])
T_total = int(cu_seqlens[-1])
upper = (T_total + CHUNK - 1) // CHUNK + N          # host 侧启动口径（flash_kda.cpp L178）

print("cu_seqlens  =", cu_seqlens.tolist())
print("tile_prefix =", tile_prefix.tolist(), "(期望 [0, 1, 4, 5, 9])")
print(f"真实 tile 数 = {actual}, 启动 tile 数(上界) = {upper}")

# ---- 随机交叉验证：100 个 global_tile_idx（含上界内的多余 CTA）----
torch.manual_seed(0)
exits = 0
for g in torch.randint(0, upper, (100,)).tolist():
    seq, local = kernel_binary_search(tile_prefix, g)
    slen = int(cu_seqlens[seq + 1] - cu_seqlens[seq])
    t_tiles = (slen + CHUNK - 1) // CHUNK
    early_exit = local >= t_tiles                    # 复刻 fwd_kernel1.cuh L199
    b_seq, b_local = brute_force(cu_seqlens, g)
    if b_seq is None:                                # 多余 CTA：只验证会退出
        assert early_exit, f"g={g}: 应触发 early return"
        exits += 1
    else:                                            # 真实 tile：逐字段对拍
        assert not early_exit and (seq, local) == (b_seq, b_local), (g, seq, local, b_seq, b_local)
print(f"100/100 个随机 global_tile_idx 验证通过，其中 {exits} 个触发 early return")
```

**预期结果**：

- `tile_prefix = [0, 1, 4, 5, 9]`（注意 ⌈33/16⌉ = 3、⌈64/16⌉ = 4，不是 2 和 3——逐项用公式核算，不要凭直觉）。
- `真实 tile 数 = 9, 启动 tile 数(上界) = 12`。
- 全部断言通过；触发 early return 的个数取决于随机种子，期望约为 `100 × (12-9)/12 = 25` 个（精确值待本地验证）。

**延伸（可选，需 GPU）**：把 `seq_lens` 换成含空序列的 `[16, 0, 16]` 与极端的 `[1]*50`，重跑脚本观察 prefix 与命中分布；再用 `flash_kda.fwd`（按 u1-l5 的 varlen 调用方式，`cu_seqlens` 由上面 cumsum 得到）跑一次真实前向，确认多余 CTA 的存在不影响输出正确性（`tests/test_fwd.py` 已有 exact-match 断言可参照）。

## 6. 本讲小结

- K1 的 grid 是 `(total_tiles, H)`：x 轴全局 tile 编号、y 轴 head；varlen 用 `tile_prefix` 二分 + `cu_seqlens` 定位 `seq_idx/local_t/bos`，batched 用一次除法完成同样的映射。
- varlen 的 `total_tiles` 是上界 \(\lceil T_{\text{total}}/16\rceil + N\)（每序列取整浪费 < 1 个 tile），多余 CTA 在任何加载/同步之前一致 early return；batched 口径是精确值，永不触发该退出。
- `_flash_kda_build_tile_prefix` 以 1 CTA 串行 O(N) 把"每 CTA O(N) 扫描 cu_seqlens"优化成"每 CTA O(log N) 二分 int32 前缀"（提交 `1ce47ea`）；`<=` 比较保证边界 tile 归属后继序列并自动跳过空序列。
- K1 是单发加载模式：仅线程 0 `init(1)` → `fence_barrier_init` → `arrive_and_expect_tx(12864)` 后连发 5 个 TMA 拷贝共用同一事务 barrier；全块 `wait(0)` + `fence_view_async_shared()` + `__syncthreads()` 后进入计算——两道围栏分别打通 generic→async 与 async→generic 的可见性（后者方向的缺失曾由 `5fdc7b2` 修复）。
- beta 用 1D TMA 加载 32 个 bf16：gmem 侧 `& ~7` 向下对齐 16 字节，消费侧 `& 7` 补回余数，免除 varlen 下 T 不对齐的约束。
- K1 用"海量 CTA + `__launch_bounds__(256,8)`"隐藏延迟，K2 用"专用 warp + PipelineTmaAsync 多级缓冲"隐藏延迟——同一份 TMA 机制的两极用法。

## 7. 下一步学习建议

下一讲 **u2-l7（Kernel 1 计算阶段）** 从本讲结束的 L265 继续往下读：L2 归一化的 warp shuffle 树归约、128+128 线程的双任务分支（门控激活 + cumsum 与 k 尾部清零）、`decay_apply` 的寄存器分块，以及 L/Mqk 的构造与 tril 掩码——并对照 `tests/torch_ref.py` 验证每一步的数值等价性。

如果想先巩固本讲的同步机制，建议直接预读 [csrc/smxx/fwd_kernel2.cuh:L347-L351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L347-L351) 与 [csrc/smxx/utils.cuh:L86-L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L86-L116)，体会"裸 ClusterTransactionBarrier → PipelineTmaAsync"正是把本讲的单发模式推广成环形多级流水的结果（u3-l2、u3-l3 会系统展开）。workspace 的写入侧（本讲只点了 L516 的 `ws_idx`）在 u2-l8 与 K2 的读取侧对齐讲解。
