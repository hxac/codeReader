# 高性能 GEMM 优化

## 1. 本讲目标

矩阵乘（GEMM）是几乎所有深度学习算子的性能地基，也是衡量一个 DSL「能压到多极致」的标尺。本讲以昇腾上的高性能 GEMM 为综合案例，把前面散落在各讲的原语「拼」成一份接近极限性能的实现。学完本讲，你应当能够：

- 说清把一个朴素 GEMM 推到极致需要叠加**哪些正交优化手段**，以及它们各自解决什么瓶颈。
- 读懂 `examples/gemm/example_gemm_intrinsic.py` 这份「手写流水线」版 GEMM：`T.mma`、L0 乒乓双缓冲、多队列 `flag` 流水、`T.use_swizzle` 核间重排。
- 理解 **`kL0Size`、`block_M`、`block_N` 三者的存储预算权衡**，会用 `static_assert` 推导 L0A/L0B 是否放得下。
- 掌握 **双缓冲（`S1`/`S2`）、`flag` 流水、`num_stages` 三者的协同关系**，知道它们是如何用「事件编号配对」实现环形复用的。
- 会用 `msprof` 采集性能数据，按调优方法论迭代出一组更优配置。

## 2. 前置知识

本讲是「拼装型」讲义，需要你已经掌握下列认知（均来自前置讲义）：

- **昇腾存储层级与 Cube/Vector 分工**（u1-l1、u4-l1）：Cube 核（AIC）做矩阵乘，数据落在 L1 → L0A/L0B → L0C；GEMM 是「纯 Cube」算子，本讲全程在 `T.Scope("C")` 内。
- **`T.gemm_v0` 与 `T.mma` 的区别**（u3-l3）：`gemm_v0` 是块级接口，模板内部全包 L1→L0 搬运、K 分段累加与乒乓同步；`mma`（即 `npu_gemm`）是指令级接口，只发一条 `Mmad`，L1→L0 搬运与同步全靠用户手写。**高性能 GEMM 选后者**，换取对搬运与缓冲的完全控制。
- **`T.copy` 的 scope 派发**（u3-l2）：`T.copy(src, dst)` 走哪条 DMA 指令由 `src.scope()` 与 `dst.scope()` 决定，覆盖 GM↔L1、L1→L0A/L0B、L0C→GM 等路径。
- **核内 `set_flag`/`wait_flag` 同步**（u4-l2）：Ascend AI Core 内部 MTE2/MTE1/M/Fix 等多条流水线**并行**推进，必须用事件标志（HardEvent）显式约束先后；三元组（置位方/等待方/事件编号）必须配对且最终计数配平。
- **`T.Pipelined` 软件流水**（u3-l6）：`num_stages` 控制重叠度，等于同时在流水的迭代数与缓冲副本数。
- **`T.Persistent` 数据块调度**（u3-l7）：让相邻 tile 归同一核处理，提升 L2 命中并允许 L1 缓冲跨 tile 复用。

如果你对其中某项还陌生，建议先回看对应讲义再继续。此外，本讲的调优方法论参考自 [examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md)，虽然以 FA 为例，但「核内双缓冲 / 多队列重叠 / 优化到单一 bound」的思路对 GEMM 完全适用。

## 3. 本讲源码地图

本讲围绕三份实现同一算子（\(C=A\times B\)，\(A\in\mathbb{R}^{M\times K}\)、\(B\in\mathbb{R}^{K\times N}\)）的脚本，外加两个文档：

| 文件 | 模式 | 定位 |
|------|------|------|
| [examples/gemm/example_gemm_pto_developer.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py) | Developer（PTO） | **朴素基线**：用 `T.gemm_v0`，模板内部处理搬运与同步，仅暴露 `K_L1` 一个调参旋钮。 |
| [examples/gemm/example_gemm_intrinsic.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py) | Expert（Ascend C） | **高性能主线**：`T.mma` + 手写 L0 乒乓双缓冲 + 多队列 `flag` 流水 + `use_swizzle`，本讲精读对象。 |
| [examples/gemm/example_gemm_intrinsic_persistent.py](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py) | Expert + Persistent | 在主线基础上把外层调度换成 `T.Persistent`，让 L1 缓冲跨 tile 复用。 |
| [docs/deeplearning_operators/matmul.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/deeplearning_operators/matmul.md) | 文档 | tile-lang 的三层抽象（Level 1/2/3）与 GPU 版 GEMM 示例，提供通用 pipeline 概念背景。 |
| [examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md) | 文档 | 昇腾性能调优方法论：双缓冲、多队列重叠、单一 bound、msprof 用法。 |

涉及的核心前端原语（仅给签名与定位，细节见前置讲义）：

- [tilelang/language/customize.py:115-132](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L115-L132) —— `T.mma`（即 `npu_gemm`），指令级 L0A×L0B→L0C。
- [tilelang/language/ascend.py:343-373](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L343-L373) —— `T.gemm_v0`，含 `kL0Size` 参数说明。
- [tilelang/language/__init__.py:202-214](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L202-L214) —— `T.use_swizzle`（昇腾版 `npu_use_swizzle`）。
- [src/tl_templates/ascend/common.h:1144-1165](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1144-L1165) —— `gemm_v0` 模板里 L0A/L0B 的存储预算 `static_assert`，是 `kL0Size` 调参的物理依据。

---

## 4. 核心概念与源码讲解

### 4.1 从朴素 GEMM 到高性能 GEMM：优化全景图

#### 4.1.1 概念说明

一个最朴素的昇腾 GEMM 是：每个核负责输出矩阵 \(C\) 的一块 \(\text{block\_M}\times\text{block\_N}\)，沿 K 维分块，每块从 GM 搬到片上、算一次矩阵乘、累加到 L0C，最后写回 GM。它正确，但远未榨干硬件，因为：

- **GM 带宽是瓶颈**：每次 mma 都要等数据从 GM 搬到 L1、再到 L0，搬运期间 Cube 计算单元（M 队列）在干等。
- **L2 cache 命中差**：相邻核若取的是不相邻的 tile，共享 L2 反复被换入换出。
- **L1/L0 利用率低**：缓冲只开一份，上一块没算完下一块搬不进来。

高性能 GEMM 的本质就是**用各种手段把「搬运」藏到「计算」背后**，并让存储利用率与 cache 命中率最大化。本项目里，把这些手段叠在一起就得到 `example_gemm_intrinsic.py`。它们大致是六条**互相正交**的优化：

| 优化手段 | 解决的瓶颈 | 本讲对应模块 |
|---------|-----------|-------------|
| L0 乒乓双缓冲（`S2`） | L1→L0 搬运等 mma | 4.2 |
| 多队列 `flag` 流水 | GM→L1、L1→L0、mma、写回四段串行 | 4.3 |
| `kL0Size` 调参 | L0A/L0B 放不下 / L0C 利用率低 | 4.2 |
| `T.use_swizzle` 核间重排 | L2 cache 命中差 | 4.4 |
| `T.Persistent` 跨块复用 | L1 缓冲每块重分配、L2 命中 | 4.5 |
| L1 常驻（GM→L1 双缓冲 `S1`） | GM→L1 搬运等计算 | 4.2 / 4.3 |

> 提示：tile-lang 的三层抽象（详见 [docs/deeplearning_operators/matmul.md:15-22](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/docs/deeplearning_operators/matmul.md#L15-L22)）里，Developer 模式（`gemm_v0`）≈ Level 2，让编译器/模板处理大部分调度；Expert 模式（`mma` + 手写 flag）≈ Level 3，把每条搬运与同步都握在自己手里。**性能极致通常需要降到 Level 3**，这正是本讲主线。

#### 4.1.2 核心流程

先看「朴素基线」与「高性能主线」在结构上的差别。

**朴素基线**（`example_gemm_pto_developer.py`）的每个核只做三件事循环：

```
对每个输出块 (bx, by):
    for k in 0..ceildiv(K, K_L1):          # GM → L1
        T.copy(A 块, A_L1); T.copy(B 块, B_L1)
        T.gemm_v0(A_L1, B_L1, C_L0, init=(k==0))   # 模板内: L1→L0 + mma + 乒乓
    T.copy(C_L0, C 块)                      # L0C → GM
```

`gemm_v0` 模板内部已经做了一定程度的 L0 乒乓与 flag 同步（见 4.2.3），但 **GM→L1 这一跳是完全串行暴露的**：第 k 块搬完才能算，算完才能搬第 k+1 块。

**高性能主线**（`example_gemm_intrinsic.py`）把这条串行链拆成四条可重叠的硬件队列，用 flag 让它们并行推进：

```
预置初始 flag（prime）
对每个输出块:
    预取第 0 块到 L1[0]                    # MTE2 队列
    for k in 0..loop_k:
        预取第 k+1 块到 L1[(k+1)%S1]        # MTE2 与本块计算重叠（S1=2 双缓冲）
        for kk in 0..loop_kk:
            L1[k%S1] → L0[kk%S2]           # MTE1 队列（S2=2 双缓冲）
            T.mma(A_L0, B_L0, C_L0)         # M 队列
        L0C → GM                            # Fix 队列
配平 flag（drain）
```

关键转变：从「一段算完再下一段」变成「四段流水同时跑，靠 flag 保证正确先后」。

#### 4.1.3 源码精读

先看朴素基线的核心循环（[examples/gemm/example_gemm_pto_developer.py:44-52](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py#L44-L52)）：

```python
with T.Scope("C"):
    loop_k = T.ceildiv(K, K_L1)
    for k in T.serial(loop_k):
        T.copy(A[bx * block_M, k * K_L1], A_L1)
        T.copy(B[k * K_L1, by * block_N], B_L1)
        T.gemm_v0(A_L1, B_L1, C_L0, init=(k == 0))
    T.copy(C_L0, C[bx * block_M, by * block_N])
```

注意它**没有任何手写 `set_flag`/`wait_flag`**——因为顶部开了两个自动 pass（[example_gemm_pto_developer.py:20-23](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py#L20-L23)）：

```python
pass_configs = {
    tilelang.PassConfigKey.TL_ASCEND_AUTO_SYNC: True,       # 自动插同步
    tilelang.PassConfigKey.TL_ASCEND_MEMORY_PLANNING: True, # 自动缓冲复用
}
```

再看高性能主线最外层结构（[examples/gemm/example_gemm_intrinsic.py:50-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L50-L65)）：

```python
with T.Kernel(core_num, is_npu=True) as (cid, _):
    A_L1 = T.alloc_L1((S1, block_M, K_L1), dtype)   # S1 份 L1 双缓冲
    B_L1 = T.alloc_L1((S1, K_L1, block_N), dtype)
    A_L0 = T.alloc_L0A((S2, block_M, block_K), dtype)  # S2 份 L0 双缓冲
    B_L0 = T.alloc_L0B((S2, block_K, block_N), dtype)
    C_L0 = T.alloc_L0C((block_M, block_N), accum_dtype)
    with T.Scope("C"):
        init_flag()                                  # 预置 flag
        for i in T.serial(T.ceildiv(m_num * n_num, core_num)):
            cid = T.use_swizzle(i * core_num + cid, M, N, K, block_M, block_N, off=3)
            if cid < m_num * n_num:
                bx = cid // n_num; by = cid % n_num
                ...                                  # 四段流水主体（4.2/4.3 展开）
        clear_flag()                                 # 配平 flag
```

两个一眼可见的差别：(1) 缓冲维度多了 `S1`/`S2` 前缀，这是双缓冲的「份数」；(2) 没有 `pass_configs`，同步全靠手写 `init_flag`/`clear_flag`。调用处（[example_gemm_intrinsic.py:108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L108)）把 `S1=2`、`S2=2` 显式传进去：`func = matmul(M, N, K, 128, 256, 64, 256, 2, 2)`，即 `block_M=128, block_N=256, block_K=64, K_L1=256, S1=2, S2=2`。

#### 4.1.4 代码实践

1. **实践目标**：建立「朴素 vs 高性能」的直观性能差。
2. **操作步骤**：
   - 先跑朴素基线：`python examples/gemm/example_gemm_pto_developer.py`（默认 1024³）。
   - 再跑高性能主线：`python examples/gemm/example_gemm_intrinsic.py`（默认 8192×1024×8192）。
3. **需要观察的现象**：两份脚本最后都会打印 `tilelang time` 与 `torch time`。
4. **预期结果**：高性能版相对 torch 的加速比应显著优于朴素版（朴素版在 1024³ 这种小规模甚至可能慢于 torch，因为没有规模优势）。**待本地验证**具体数值——因规模、卡型不同而异。
5. 若没有真实 NPU，可改读两份脚本生成的源码：在 `func(a, b)` 之前都有 `print(func.get_kernel_source())`，对比朴素版（`gemm_v0` 模板调用）与高性能版（密集的 `SetFlag/WaitFlag` + `Mmad`）的代码量差异。

#### 4.1.5 小练习与答案

**练习 1**：朴素基线为什么开 `TL_ASCEND_AUTO_SYNC=True`，而高性能主线不开？

**参考答案**：朴素基线用 Developer 抽象（`gemm_v0`），用户不写 flag，靠 `AscendSyncInsert` pass 自动插；高性能主线用 Expert 抽象（`mma`），同步由用户手写精确配对（`init_flag`/`clear_flag`），若再开自动同步会与手写 flag 冲突或产生冗余。详见 u4-l3。

**练习 2**：列出本讲六条优化手段中，哪几条是「核内」优化、哪几条是「核间」优化？

**参考答案**：核内——L0 乒乓双缓冲（`S2`）、多队列 flag 流水、`kL0Size` 调参、L1 常驻/`S1` 双缓冲；核间——`T.use_swizzle`（核间任务重排）、`T.Persistent`（核间数据块调度）。注意 GEMM 是纯 Cube 算子，这里的「核间」指多个 AI Core 之间，而非 Cube↔Vector 之间。

---

### 4.2 T.mma 与 L0 乒乓双缓冲（S1/S2、kL0Size 与 L0 预算）

#### 4.2.1 概念说明

`T.mma`（即 `npu_gemm`，[customize.py:115-132](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/customize.py#L115-L132)）是**指令级**矩阵乘：操作数 A 必须在 L0A、B 必须在 L0B，结果写 L0C，每次调用只发一条硬件 `Mmad` 指令。它**不负责**把数据从 L1 搬到 L0——那是用户的活。

这就引出核心矛盾：**L0A/L0B 各只有 64KB**（A2/A3/A5 一致），一块 `(block_M × kL0Size)` 的 A 和 `(kL0Size × nTile)` 的 B 要塞进去，还得留出空间做**乒乓（ping-pong）双缓冲**——即同时存两份，让「搬第 i+1 块」与「算第 i 块」重叠。于是引入两个调参旋钮：

- **`kL0Size`**（在 `gemm_v0` 里是参数，在 Expert 版里对应 `block_K`）：L1→L0 时 K 维的分段大小，必须是 16 的倍数。
- **份数 `S2`**：L0A/L0B 开几份做乒乓，高性能主线取 `S2=2`（双缓冲）。

同理，GM→L1 这跳也开 `S1` 份（主线 `S1=2`），让「搬第 k+1 块到 L1」与「第 k 块在 L0 里算」重叠。

#### 4.2.2 核心流程

L0 乒乓的核心时序（取 `S2=2`）：

```
kk=0:  搬 L1→L0[0]  →  mma(L0[0])  →  搬 L1→L0[1]
kk=1:  mma(L0[1])   →  搬 L1→L0[0] →  mma(L0[0])   ...   （L0[0]/L0[1] 交替复用）
```

用「事件编号 `kk % S2`」把 buffer 槽位与 flag 配对：第 `kk` 轮用槽 `kk%2`，置位/等待的 flag 编号也用 `kk%2`，于是两份缓冲与两个事件号天然形成两个独立的「通道」，互不干扰。

`kL0Size` 的存储预算则由模板里的 `static_assert` 强制（见 4.2.3）。其权衡是：

- **调大 `kL0Size`**：每次 mma 算更多 K，L1→L0 搬运趟数少；但 L0A/L0B 单份占用变大，可能挤掉双缓冲空间，或迫使 N 维再分片（`nL0split>1`）。
- **调小 `kL0Size`**：L0A/L0B 更省，能容纳更大 `block_M`/`block_N`（提升 L0C 利用率）并保留双缓冲；代价是 L1→L0 搬运趟数变多。

`gemm_v0` 的 docstring（[ascend.py:361-371](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/ascend.py#L361-L371)）给出的经验值是：**fp16、`block_M=128`、`block_N=256` 时，`kL0Size=64` 推荐**。本讲高性能主线正是用 `block_K=64`（即 `kL0Size` 的角色）配 `block_M=128, block_N=256`，与该建议完全一致。

#### 4.2.3 源码精读

L1→L0 的乒乓搬运在主线 kk 循环里（[example_gemm_intrinsic.py:83-96](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L83-L96)）：

```python
for kk in T.serial(loop_kk):
    if kk == 0:
        T.wait_flag("mte2", "mte1", k % S1)          # 等 GM→L1 就绪
    T.wait_flag("m", "mte1", kk % S2)                # 等上一轮该槽 mma 用完
    T.copy(A_L1[k % S1, 0, kk * block_K], A_L0[kk % S2, :, :])  # L1→L0A，用 kk%S2 槽
    T.copy(B_L1[k % S1, kk * block_K, 0], B_L0[kk % S2, :, :])
    ...
    T.mma(A_L0[kk % S2, :, :], B_L0[kk % S2, :, :], C_L0,
          init=T.And(k == 0, kk == 0))
    T.set_flag("m", "mte1", kk % S2)                 # 标记该槽 mma 完成，可供下一轮搬运
```

注意三处 `% S2`（槽位）与 `kk` 的耦合：搬运目标、mma 操作数、flag 编号都用 `kk % S2`，构成环形复用。`init=T.And(k==0, kk==0)` 表示**整个 K 累加的最开始一次**才清零 L0C，其余累加。

存储预算的物理依据在 `gemm_v0` 模板（这条 `static_assert` 对 Expert 版 `mma` 同样适用，因为 `mma` 落到同一个底层模板）。关键三行（[common.h:1144-1165](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1144-L1165)）：

```cpp
constexpr uint32_t nMaxByL0B = (32u * 1024u) / (kL0Size * sizeof(T1));  // 单槽 32KB 上限下的最大 N
constexpr uint32_t nTile = (transpose_B || N <= nMaxByL0B) ? N : nMaxByL0B;
constexpr uint32_t kL0Budget = (64u * 1024u) / (kNumSteps > 1 ? 2u : 1u); // 乒乓时单槽 32KB
static_assert(nTile * kL0Size * sizeof(T1) <= kL0Budget, ...);  // B 放得下
static_assert(M * kL0Size * sizeof(T1) <= kL0Budget, ...);      // A 放得下
```

代入主线的数：fp16（`sizeof=2`）、`block_M=128`、`block_N=256`、`kL0Size=64`：
- A 槽：\(128\times 64\times 2 = 16384\text{B}=16\text{KB} \le 32\text{KB}\) ✓
- B 槽：\(nTile=256\)（因 \(256 \le 32\text{KB}/(64\times2)=256\)），\(256\times 64\times 2 = 32\text{KB} \le 32\text{KB}\) ✓（恰好放满，单 N 片即可，`nL0split=1`）

若改用 `kL0Size=128`：\(nMaxByL0B=32768/(128\times2)=128\)，于是 `block_N=256` 时 `nTile=128`、`nL0split=2`，B 要分两片搬，L1→L0 趟数翻倍——这就是「调大 `kL0Size` 挤掉空间」的代价。

#### 4.2.4 代码实践

1. **实践目标**：亲手验证 `kL0Size`（主线里的 `block_K`）对 L0 预算的影响。
2. **操作步骤**：把 [example_gemm_intrinsic.py:108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L108) 的 `block_K` 从 `64` 改成 `128`（即 `matmul(M, N, K, 128, 256, 128, 256, 2, 2)`），重新运行。
3. **需要观察的现象**：脚本是否仍输出 `Kernel Output Match!`；生成的源码里 `loop_kk`（L1→L0 内层趟数）是否变化。
4. **预期结果**：正确性应仍通过（结果与 `block_K` 无关，只影响搬运粒度）；但 `kL0Size=128` 会让 `block_N=256` 走两片 N（更多搬运），性能大概率变差。**待本地验证**具体时延。
5. 进一步可尝试 `block_K=32`，观察 L0A/L0B 是否空转更多、mma 指令数是否翻倍。

#### 4.2.5 小练习与答案

**练习 1**：为什么主线用 `S2=2` 而不是 `S2=3`？

**参考答案**：`S2` 是 L0 双缓冲份数，`S2=2` 已能让「搬下一块」与「算当前块」完全重叠，再多一份只增加 L0 占用而不进一步提升重叠度（mma 与搬运只有两段需要交替）。L0A/L0B 仅 64KB，`S2>2` 会迅速挤爆预算。`num_stages` 与 `S` 的关系是「份数 = num_stages」，主线等价于 L0 这一级 `num_stages=2`。

**练习 2**：计算 fp16、`block_M=128`、`block_N=128`、`kL0Size=128` 时，A、B 各占 L0 单槽多少 KB，是否还能双缓冲？

**参考答案**：A 槽 \(128\times128\times2=32\text{KB}\)，B 槽 \(nTile=128\)（\(128\le 32768/(128\times2)=128\)），\(128\times128\times2=32\text{KB}\)。两者都恰好等于单槽上限 32KB，`kNumSteps>1` 时刚好放得下双缓冲，是「紧巴巴但合法」的配置。

---

### 4.3 多队列 flag 流水：MTE2/MTE1/M/Fix 的重叠

#### 4.3.1 概念说明

高性能 GEMM 把一条串行链拆给四条硬件队列（详见 FA 调优指南 [flash_attention_performance_optimization_zh.md:84-106](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md#L84-L106)）：

| 队列 | 职责 | 对应搬运 |
|------|------|---------|
| **MTE2** | GM→片上搬运 | GM → L1 |
| **MTE1** | 片上→L0 搬运 | L1 → L0A/L0B |
| **M**（Cube） | 矩阵乘 | `T.mma` |
| **Fix** | L0C→GM 写回 | `T.copy(C_L0, C[...])` |

这四条队列**物理上并行**推进，编译器/硬件不保证它们的先后，必须由 `set_flag(src, dst, eventId)` / `wait_flag(src, dst, eventId)` 显式声明依赖：`src` 队列完成某事件后置位，`dst` 队列在执行依赖该数据的指令前必须等待。详见 u4-l2。

#### 4.3.2 核心流程

一份正确的 flag 流水要做两件「全局」的事：

1. **Prime（预置）**：循环开始前，给那些「循环第一轮要 wait、但还没人 set」的 flag 预先置位，否则第一轮会死锁。
2. **Drain（配平）**：循环结束后，把循环内多 set 的 flag wait 掉，保证整个程序结束时每对 flag 的 set/wait 计数相等（详见 u4-l2「计数配平」）。

主线的 `init_flag`/`clear_flag` 两个 `T.macro` 正是干这两件事（[example_gemm_intrinsic.py:28-43](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L28-L43)）：

```python
@T.macro
def init_flag():                # prime：预置循环第一轮要消费的 flag
    T.set_flag("mte1", "mte2", 0); T.set_flag("mte1", "mte2", 1)
    T.set_flag("m", "mte1", 0);   T.set_flag("m", "mte1", 1)
    T.set_flag("fix", "m", 0)

@T.macro
def clear_flag():               # drain：配平循环内多出来的 set
    T.wait_flag("mte1", "mte2", 0); T.wait_flag("mte1", "mte2", 1)
    T.wait_flag("m", "mte1", 0);   T.wait_flag("m", "mte1", 1)
    T.wait_flag("fix", "m", 0)
```

循环内的 flag 则承担「逐拍同步」：每个 `set/wait` 的事件编号都和缓冲槽位（`k%S1`、`kk%S2`）绑定，形成「事件号 × 槽位」的双通道流水。

> 关键协同：**双缓冲（`S1`/`S2`）与 flag 流水是一体两面**。`S1=2` 决定 L1 开两份，flag 就需要两个事件号（`0` 和 `1`）区分这两份；`num_stages` 在手写版里就等于 `S1`/`S2`。这与 `T.Pipelined(num_stages=N)` 让编译器自动做多版本化（u3-l6）是同一原理，只是这里由人手写。

#### 4.3.3 源码精读

逐段看主线四段流水的 flag 配对（承接 4.1.3 的外层结构）。

**GM→L1 预取 + 第一块**（[example_gemm_intrinsic.py:69-79](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L69-L79)）：

```python
T.wait_flag("mte1", "mte2", 0)                       # 等 L1 槽 0 空闲（prime 时已 set）
T.copy(A[bx * block_M, 0], A_L1[0, :, :])            # MTE2: GM→L1 槽 0
T.copy(B[0, by * block_N], B_L1[0, :, :])
T.set_flag("mte2", "mte1", 0)                        # 告知 MTE1：L1 槽 0 可用
T.wait_flag("fix", "m", 0)                           # 等 L0C 可写
for k in T.serial(loop_k):
    if k < loop_k - 1:
        T.wait_flag("mte1", "mte2", (k + 1) % S1)    # 等 L1 槽 (k+1)%S1 空闲
        T.copy(A[bx*block_M, (k+1)*K_L1], A_L1[(k+1)%S1, :, :])  # 预取下一块
        T.copy(B[(k+1)*K_L1, by*block_N], B_L1[(k+1)%S1, :, :])
        T.set_flag("mte2", "mte1", (k + 1) % S1)     # 告知 MTE1：新块可用
```

这里 `(k+1)%S1` 把「搬第 k+1 块」与「算第 k 块」通过两个事件号（`0`/`1`）解耦——MTE2 在搬新块时，M 队列正在算旧块，互不等待。

**kk==3 处的「提前释放 L1 槽」**（[example_gemm_intrinsic.py:89-90](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L89-L90)）：

```python
if kk == 3:
    T.set_flag("mte1", "mte2", k % S1)   # L1 槽 k%S1 的数据已全部搬入 L0，可被下一 k 覆盖
```

这是一个精妙的**提前归还**：L1 里的一块 `K_L1` 数据要被拆成多次 `block_K` 搬到 L0，等到第 `kk==3` 次（即 L1 这块已被消费到一定程度）就提前告诉 MTE2「这个 L1 槽可以复用了」，让 GM→L1 的下一块更早进来，进一步减小气泡。

**写回**（[example_gemm_intrinsic.py:98-101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L98-L101)）：

```python
T.set_flag("m", "fix", 0)        # M 队列算完，告知 Fix 可写回
T.wait_flag("m", "fix", 0)       # Fix 等 M 完成
T.copy(C_L0, C[bx * block_M, by * block_N])  # Fix: L0C→GM
T.set_flag("fix", "m", 0)        # 告知 M：L0C 已写出，可被下一块复用
```

#### 4.3.4 代码实践

1. **实践目标**：理解 prime/drain 缺一会死锁。
2. **操作步骤**：把 [example_gemm_intrinsic.py:59](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L59) 的 `init_flag()` 注释掉，重新运行。
3. **需要观察的现象**：程序是否卡死或报 flag 计数不平。
4. **预期结果**：循环第一轮的 `T.wait_flag("mte1","mte2",0)` 永远等不到（没人 set），程序挂起或运行时报同步错误。**待本地验证**具体表现（可能表现为 NPU 调用超时）。恢复 `init_flag()` 后应正常。
5. 进阶：把 `clear_flag()` 注释掉，观察结尾是否报 flag 未配平。

#### 4.3.5 小练习与答案

**练习 1**：`init_flag` 里为什么有 `set_flag("fix","m",0)` 而 `clear_flag` 里对应的是 `wait_flag("fix","m",0)`？它们配的是循环里的哪一对？

**参考答案**：循环结尾 `T.copy(C_L0, ...)` 之后有 `T.set_flag("fix","m",0)`（[L101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L101)），它通知 M「L0C 已写出可复用」；而下一块的 `T.wait_flag("fix","m",0)`（[L73](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L73)）要消费它。`init_flag` 预置这一个 set（让第一块的 wait 不死锁），`clear_flag` 在最后 wait 掉它（配平）。这是 prime/drain 的标准套路。

**练习 2**：如果把 `S1` 从 2 改成 3（L1 三缓冲），flag 事件号要怎么改？

**参考答案**：事件号取值要与份数一致，所有 `k%S1`、`(k+1)%S1` 自动变成 `% 3`，事件号集合从 `{0,1}` 扩为 `{0,1,2}`；`init_flag`/`clear_flag` 里对应 `mte1↔mte2` 的 set/wait 也要各加一个事件号 `2`。否则会有事件号未被配平。

---

### 4.4 T.use_swizzle：核间任务重排提升 L2 命中

#### 4.4.1 概念说明

朴素分配里，第 `cid` 号核算第 `cid` 块输出 tile，相邻核算的是相邻但「跨行」的 tile，它们从 GM 取的 A、B 数据几乎没有重叠，共享 L2 cache 形同虚设。`T.use_swizzle` 通过**重排「核号 → tile 号」的映射**，让相邻的若干个核在相近时刻取**同一行 A**（共享 bx），从而大幅提升 L2 命中率。

注意它与 u4-l4 讲的「数据布局 swizzle」是两回事：那个 swizzle 改的是片上数据怎么摆（zN/nZ 布局），这个 swizzle 改的是**核间任务怎么轮流取**。本项目里前端用 `del` + 重定义把 `use_swizzle` 切到昇腾版（见 u4-l4）。

#### 4.4.2 核心流程

昇腾版 `T.use_swizzle`（[__init__.py:202-214](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L202-L214)）发射一个 `tl.ascend_use_swizzle` intrinsic，模板串是 `thread_block_swizzle<m,n,k,block_m,block_n,off,dir>`，输入原始 `cid`，输出重排后的 `cid`。随后用户用重排后的 `cid` 解码出 `(bx, by)`：

```
remapped = T.use_swizzle(原始cid, M, N, K, block_M, block_N, off=3)
bx = remapped // n_num
by = remapped % n_num
```

`off` 是重排的「错开步长」：相邻核的 tile 编号错开 `off`，使它们落在同一 `bx`（同一 A 行）但不同 `by`。`dir` 控制行/列优先。

#### 4.4.3 源码精读

主线里 `use_swizzle` 出现在外层循环顶端（[example_gemm_intrinsic.py:61-65](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L61-L65)）：

```python
for i in T.serial(T.ceildiv(m_num * n_num, core_num)):
    cid = T.use_swizzle(i * core_num + cid, M, N, K, block_M, block_N, off=3)
    if cid < m_num * n_num:
        bx = cid // n_num
        by = cid % n_num
```

注意这里是 **Persistent 式的外层循环**（每核用 `T.serial` 反复领新 tile），`use_swizzle` 作用在每个「逻辑 tile 序号」上。`off=3` 表示相邻 3 个核共享同一 `bx`，于是它们取的 A 行块在 L2 里能复用。前置断言（[__init__.py:207](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/tilelang/language/__init__.py#L207)）要求 `m%block_m==0` 且 `n%block_n==0`，故本例要求 M、N 被 block 整除。

> 对照：朴素基线 `example_gemm_pto_developer.py` **没有** `use_swizzle`（[L35-37](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_pto_developer.py#L35-L37) 直接 `bx=cid//n_num; by=cid%n_num`），相邻核取相邻 tile，L2 命中较差——这是它在大规模下慢于主线的原因之一。

#### 4.4.4 代码实践

1. **实践目标**：观察 `use_swizzle` 对 L2 命中（进而对总耗时）的影响。
2. **操作步骤**：把 [example_gemm_intrinsic.py:62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L62) 的 `cid = T.use_swizzle(...)` 改成 `cid = i * core_num + cid`（即不做重排，退回朴素映射），其余不变，重新运行。
3. **需要观察的现象**：`tilelang time` 是否变化；用 `msprof op` 采集时 L2 命中率指标是否下降。
4. **预期结果**：去掉 swizzle 后，大规模（如默认 8192×1024×8192）下性能应有可测下降，因为相邻核不再共享 A 行的 L2 缓存。**待本地验证**——收益与 N（列数）规模相关，N 越小、core 越多，效果越明显。
5. 可尝试把 `off=3` 改成 `off=1` 或 `off=5`，对比哪個最优（通常与 `core_num` 和 `n_num` 的比例有关）。

#### 4.4.5 小练习与答案

**练习 1**：`use_swizzle` 改变了输出的正确性吗？为什么？

**参考答案**：不改变。每个输出 tile 仍由且仅由一个核计算一次，swizzle 只改「哪个核算哪个 tile」的映射，不改 tile 内容或覆盖关系。`if cid < m_num*n_num` 保证了越界的核不计算。

**练习 2**：为什么 `use_swizzle` 对「N 很大、core 很少」的场景收益小？

**参考答案**：swizzle 收益来自相邻核共享同一 `bx`（A 行块）在 L2 的缓存。若 N 很大，每个核要沿 N 方向走很多步，A 行块的 L2 复用窗口相对变短；core 很少则「相邻核」本就少，错开的重排能共享的组合有限。极端情况下退化为无收益。

---

### 4.5 T.Persistent：L1 跨数据块复用

#### 4.5.1 概念说明

朴素 `T.Kernel(m_num*n_num)` 模式下，每个核算完一个 tile 就「退出」，L1/L0C 缓冲随之释放，下一个 tile 由另一个核重新分配——缓冲无法跨 tile 复用，且核间调度对 L2 不友好。`T.Persistent`（详见 u3-l7）让**只启动 `core_num` 个常驻核**，每核在循环里反复领新 tile，于是 L1/L0C 缓冲可以提到循环外、跨 tile 复用，调度也更确定。

#### 4.5.2 核心流程

`T.Persistent` 把 tile 网格 `[m_num, n_num]` 按 `wave`（波）分配给 `core_num` 个核：

```
启动 core_num 个常驻核
对每核 cid，在 T.Persistent 里循环领 tile:
    (bx, by) = Persistent 重排后的坐标
    用循环外分配的 L1/L0C 缓冲算这一块
```

与 4.4 的 `use_swizzle`+`T.serial` 手写版相比，`T.Persistent` 是把「常驻循环 + 友好调度」做成了内置原语，逻辑全在 `src/ir.cc::PersistentFor`，无需专用 pass。

#### 4.5.3 源码精读

把主线和 persistent 版逐行对比，**循环体几乎完全相同**（同样的 `init_flag`、四段流水、`use_swizzle` 缺席、`T.mma`），唯一差别在外层调度。主线用手写的 `T.serial` + `use_swizzle`（[example_gemm_intrinsic.py:61-62](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L61-L62)），persistent 版换成原语（[example_gemm_intrinsic_persistent.py:48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py#L48)）：

```python
for bx, by in T.Persistent([T.ceildiv(M, block_M), T.ceildiv(N, block_N)], core_num, cid):
    loop_k = T.ceildiv(K, K_L1)
    ...   # 与主线一致的 K 双缓冲 + kk 乒乓 + mma
```

注意两点：(1) `T.Persistent` 直接解包出 `(bx, by)`，省去了 `cid//n_num` 的手写解码；(2) `A_L1`/`B_L1`/`A_L0`/`B_L0`/`C_L0` 仍分配在循环外（[persistent 版 L38-43](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py#L38-L43)），跨 tile 复用——这正是 Persistent 的核心收益。两者调用参数一致（[persistent 版 L101](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py#L101)：`matmul(M,N,K,128,256,64,256,2,2)`）。

> 经验：`T.Persistent` 与 `use_swizzle` 通常**二选一**——两者都在做「核间任务重排以利 L2」。persistent 版用内置重排（`group_size` 控制），主线版用手写 `use_swizzle`（`off` 控制）。可据喜好与可读性选择。

#### 4.5.4 代码实践

1. **实践目标**：对比「朴素一核算一 tile」与「Persistent 常驻」在结构上的差别。
2. **操作步骤**：阅读 [example_gemm_intrinsic_persistent.py:37-48](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic_persistent.py#L37-L48)，对比朴素基线 `example_gemm_pto_developer.py:35-37` 的 `T.Kernel(m_num*n_num, ...)`。
3. **需要观察的现象**：朴素版启动 `m_num*n_num` 个 block（每核一块即退），persistent 版只启动 `core_num=20` 个常驻 block。
4. **预期结果**：两者结果一致（都打印 `Kernel Output Match!`）；persistent 版因 L1/L0C 跨 tile 复用、调度确定，大规模下性能更稳。**待本地验证**时延差异。
5. 若有真实 NPU，可分别对两版跑 `do_bench`，记录时延并解释差异来源。

#### 4.5.5 小练习与答案

**练习 1**：为什么 persistent 版可以把 `C_L0` 分配在 `T.Persistent` 循环**外面**，而朴素版不行？

**参考答案**：persistent 版每核常驻、循环领多个 tile，`C_L0` 在核的生命期内一直有效，可跨 tile 复用同一块 L0C；朴素版每核只算一个 tile 就退出，`C_L0` 随核退出而释放，无法跨 tile 复用，故只能算一块分一次。

**练习 2**：`T.Persistent` 与手写 `use_swizzle`+`T.serial` 都能实现「常驻 + L2 友好」，列出各一个优缺点。

**参考答案**：`T.Persistent` 优点是简洁、`(bx,by)` 自动解包、`group_size` 内置；缺点是重排策略固定、灵活性低。手写 `use_swizzle`+`T.serial` 优点是 `off`/`dir` 可自由调、能与任意外层逻辑组合；缺点是需手写越界保护（`if cid < m_num*n_num`）和坐标解码，更易出错。

---

## 5. 综合实践

**任务**：基于 `example_gemm_intrinsic.py`，做一轮完整的「调参 → 验证」迭代，亲手把一组配置调到更优。

**步骤**：

1. **建立基线**。运行原版（[example_gemm_intrinsic.py:108](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/gemm/example_gemm_intrinsic.py#L108)，`block_M=128, block_N=256, block_K=64, K_L1=256, S1=2, S2=2`），记录 `tilelang time` 作为基线 \(T_0\)。
2. **采集 profile**。按 FA 调优指南（[flash_attention_performance_optimization_zh.md:29-40](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/examples/flash_attention/fa_opt/flash_attention_performance_optimization_zh.md#L29-L40)）用 msprof 采集：

   ```bash
   msprof op --kernel-name="main_kernel" --output=./prof_base python3 examples/gemm/example_gemm_intrinsic.py
   ```
   观察哪条流水（MTE2/MTE1/M/Fix）是 bound（参考指南第 2.3 节「单一 bound」原则）。
3. **调参扫描**。按下面的取舍表逐组尝试，每组改完先确认仍输出 `Kernel Output Match!`，再记 `tilelang time`：

   | 旋钮 | 尝试方向 | 预期影响 |
   |------|---------|---------|
   | `block_K`（=`kL0Size`） | 64 → 32 / 128 | 小则 mma 数翻倍、大则 L0 紧张（见 4.2） |
   | `block_M` × `block_N` | 128×256 → 128×128 / 256×128 | 影响 L0C 利用率与 L0 预算 |
   | `K_L1` | 256 → 128 / 512 | 影响 GM→L1 趟数与 L1 占用 |
   | `S1`（L1 份数） | 2 → 3 | 更多重叠但 L1 占用增大（见 4.3） |

4. **验证最优组**。选出时延最低且正确的配置 \(T_{\text{opt}}\)，再用 msprof 采一次：

   ```bash
   msprof op simulator --soc-version=Ascend910B4 --kernel-name="main_kernel" --output=./prof_opt python3 examples/gemm/example_gemm_intrinsic.py
   ```
   用 `chrome://tracing/` 或 MindStudio Insight 打开流水图，确认气泡是否减小、是否更接近「单一 bound」。
5. **记录结论**。写一份小报告：最优配置、相对基线的加速比、bound 流水类型、以及哪个旋钮贡献最大。

**注意**：若没有真实 NPU，步骤 2/4 的 msprof 无法运行（仿真仅支持 PTO 后端，见 u7-l5），此时退化为「源码阅读型实践」——重点用 4.2 的 `static_assert` 公式**纸面验证**每组配置是否合法（L0A/L0B 是否放得下），并据此预测哪组更优。

## 6. 本讲小结

- 高性能 GEMM = **六条正交优化叠加**：L0 乒乓双缓冲、多队列 flag 流水、`kL0Size` 调参、`T.use_swizzle` 核间重排、`T.Persistent` 跨块复用、L1 常驻/`S1` 双缓冲；本质都是「把搬运藏到计算背后」。
- **`T.mma`（`npu_gemm`）是指令级入口**，只发一条 `Mmad`，L1→L0 搬运与同步全靠手写；`T.gemm_v0` 是块级入口，模板内部包好搬运与乒乓，适合做基线。
- **`kL0Size` 的物理边界是 L0A/L0B 各 64KB**（乒乓时单槽 32KB），由 `common.h` 的 `static_assert` 强制；fp16、`block_M=128`、`block_N=256` 推荐 `kL0Size=64`。
- **双缓冲（`S1`/`S2`）与 flag 流水是一体两面**：份数决定缓冲副本数，flag 事件号与槽位 `%S` 绑定形成环形复用；`num_stages` = 份数。手写版必须 prime（`init_flag`）+ drain（`clear_flag`）保证计数配平。
- **`T.use_swizzle` 改的是核间任务映射**（让相邻核共享同一 A 行，提升 L2 命中），与「数据布局 swizzle」（zN/nZ）是两回事。
- **调优方法论**（参考 FA 指南）：先 `msprof` 采 bound → 针对性调参 → 验证收益，目标是优化到「单一 bound」。

## 7. 下一步学习建议

- **u7-l1（FlashAttention）**：把本讲的 `mma` + flag 流水 + workspace 中转扩展到 Cube↔Vector 协同，是 GEMM 思路在「CV 融合算子」上的延伸，且 FA 调优指南正是本讲方法论的来源。
- **u7-l6（Autotuner）**：本讲的调参扫描是手动的，下一步可学 `tilelang.autotuner` 自动遍历 `block_M`/`block_N`/`kL0Size`/`num_stages` 参数空间，让搜索代替手工。
- **u7-l4（调试与性能分析）**：深入 `msprof op` / `simulator` 与 `T.dump_tensor`，把本讲的「采 bound」做得更精细。
- **源码延伸**：阅读 [src/tl_templates/ascend/common.h](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h) 的 `mma` 与 `gemm_v0` 模板（[L1108-L1230](https://github.com/tile-ai/tilelang-ascend/blob/ee60e122b6a758367cf42f4055d32d199c148bd8/src/tl_templates/ascend/common.h#L1108-L1230)），理解 L0 乒乓与 N-tiling 是如何在 C++ 模板里实现的，这是从「调参」走向「改模板」的必经之路。
