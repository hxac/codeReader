# Puzzle 08 GEMM 优化：共享内存与软件流水线

## 1. 本讲目标

本讲在上一讲（u4-l3）写出的朴素 GEMM 基础上，做两项让它「真正跑得快」的关键优化。学完本讲，你应当能够：

- 说清楚为什么把 A、B 的 tile 从寄存器片段（fragment）挪到共享内存（shared memory），而把累加器 C 留在寄存器。
- 用 `T.alloc_shared` 把 A、B 装进共享内存，并理解 `T.gemm` 天然以共享内存为高效输入来源。
- 用 `T.Pipelined(..., num_stages=3)` 把 `T.Serial` 改造成软件流水线，重叠「访存」与「计算」。
- 用 `print_source_code()` 和 `bench_puzzle` 把朴素版与优化版做「生成代码 + 耗时」的双重对比，体会这两项改动带来的加速。

本讲**不引入新的算子语义**——`T.gemm`、二维分块、K 维串行累加在 u4-l3 已讲透。本讲只回答一个问题：**同样的数据流，为什么换两行代码就能快很多？**

## 2. 前置知识

本讲假设你已经掌握以下概念（均在前面讲义建立）：

- **GPU 三级内存层级**（u2-l2）：global（显存，最大最慢）、shared（block 内共享、片上、中等）、registers（线程私有、最快最小）。直觉是「越快越小」。`T.alloc_fragment` 是「block 内所有线程寄存器」的统一抽象，`T.alloc_shared` 则分配 block 内共享的片上存储。
- **T.gemm 与 Tensor Core**（u4-l3）：`T.gemm(A, B, C)` 语义为 `C += A @ B`，一条调用封装了 Tensor Core 的 MMA 指令，完成「乘 + 归约 + 累加」。
- **混合精度累加**（u4-l3）：输入输出 `float16`，累加器 `accum_dtype=float32`。累加精度由输出 buffer 的 dtype 决定，降精度只在写回显存时发生一次。
- **朴素 GEMM 数据流**（u4-l3）：输出在 M、N 两维并行（块索引 `pid_m`、`pid_n`），K 维用 `T.Serial` 串行累加（循环前 `T.clear`，每段一次 `T.gemm`）。
- **测试/基准框架**（u1-l2）：`test_puzzle` 验证正确性、`bench_puzzle` 用 CUDA Event + warmup + repeats 公平计时。

下面用到的两个度量单位：现代 GPU（如 NVIDIA Ampere / Hopper）每个 SM 大约有 **65536 个 32 位寄存器**（约 256 KB 寄存器堆）和 **上百 KB 的共享内存**（Hopper 可达 228 KB），单线程最多可使用约 **255 个寄存器**。这些数字只是量级，用来建立直觉，不必精确记忆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/08-matrix.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py) | 题目文件。其中 [L188-L208](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L188-L208) 是讲解两项优化的文档字符串，[L211-L222](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L211-L222) 是 `tl_matmul_opt` 的 TODO 桩函数，[L225-L252](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L225-L252) 是对比脚本 `run_matmul_opt`。 |
| [ans/08-matrix.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py) | 参考答案。其中 [L155-L181](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L155-L181) 是朴素版 `tl_matmul_naive`（A/B/C 全用 fragment），[L244-L270](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L244-L270) 是优化版 `tl_matmul_opt`（A/B 用 shared），[L273-L300](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L273-L300) 是 `run_matmul_opt`。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | 提供 [bench_puzzle（L109-L155）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L155) 做公平计时。 |

> 阅读提示：优化版与朴素版的函数体几乎逐字相同，差别只有两处（缓冲类型、循环类型）。这正是题目文档里所说的「just a few lines of code changes」。本讲的全部工作就是把这两处差别讲清楚。

## 4. 核心概念与源码讲解

### 4.1 共享内存优化（T.alloc_shared）

#### 4.1.1 概念说明

先回顾 u4-l3 写出的朴素 GEMM：A、B、C 三个 tile **全部**用 `T.alloc_fragment` 分配。fragment 的本质（u2-l2 已讲）是「block 内所有线程寄存器」的统一抽象——也就是说，这三个 tile 的数据**都落在寄存器里**。

问题在于寄存器是**最小也最稀缺**的资源。以题目默认规模 `BLOCK_M=128, BLOCK_N=128, BLOCK_K=64`、`threads=128` 为例，估算累加器 `C_local` 的寄存器占用：

\[
\text{C\_local 大小} = \text{BLOCK\_M} \times \text{BLOCK\_N} \times 4\,\text{字节} = 128 \times 128 \times 4 = 64\,\text{KB}
\]

把 64 KB 的 float32 均摊到 128 个线程，每个线程要承担约 **128 个 fp32 寄存器**，已经接近单线程 255 寄存器上限的一半。再加上 A、B 两个 fragment，以及循环变量、地址计算等临时量，编译器很容易**寄存器溢出（register spilling）**——把放不下的寄存器临时转存到 local memory，而 local memory 物理上落在慢速显存里，性能随之崩塌。

因此优化思路是：**让最该待在寄存器里的量留下，把可以「降级」的量挪到共享内存**。

- **C_local（累加器）必须留在寄存器**：它在整个 K 循环里被反复读写、参与每一次 `T.gemm`，访问频率最高，必须用最快的存储。
- **A、B（输入 tile）挪到共享内存**：每个 tile 只被加载一次、参与一次 `T.gemm` 后即可丢弃，是「一次性输入」。共享内存是片上、block 内共享的中等速度存储，正好够用且不占寄存器预算。

更进一步，Tensor Core 的 MMA 指令**天然以共享内存为高效输入来源**（Ampere 用 `ldmatrix` 从 shared 载入 MMA 操作数，Hopper 的 `wgmma` 甚至直接读 shared）。所以把 A、B 放进 shared memory 不仅是「省寄存器」，更是「对齐了 `T.gemm` 偏好的数据落点」。这与题目文档的说法一致：「`T.gemm` will efficiently help us load data from shared memory」。

#### 4.1.2 核心流程

朴素版 vs 共享内存版的数据落点对比：

```
朴素版（全 fragment）           共享内存优化版
─────────────────────         ─────────────────────
A_local  → registers           A_shared → shared memory
B_local  → registers           B_shared → shared memory
C_local  → registers           C_local  → registers   ← 保留！
```

改动后的执行流程（每个 block）：

1. 在 shared memory 中开两块缓冲 `A_shared`、`B_shared`；在寄存器开一块累加器 `C_local`。
2. `T.clear(C_local)` 把累加器清零（语义不变）。
3. 进入 K 维循环：每一段用 `T.copy` 把 A、B 的 tile 从 global 搬进 shared，再调用 `T.gemm(A_shared, B_shared, C_local)` 累加。
4. 循环结束后把 `C_local` 写回 global 的 C。

注意第 3 步里 `T.copy` 的目标从「fragment」变成了「shared」，`T.gemm` 的源从「fragment」变成了「shared」——这两处的类型变化就是全部改动，**计算语义一字未改**。

#### 4.1.3 源码精读

先看朴素版（复习 u4-l3），三个 buffer 全是 fragment：

[A、B、C 全用 fragment，K 维用 T.Serial 串行累加（ans/08-matrix.py:L165-L179）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L165-L179)：

```python
with T.Kernel(T.ceildiv(N, BLOCK_N), T.ceildiv(M, BLOCK_M), threads=128) as (
    pid_n, pid_m,
):
    A_local = T.alloc_fragment((BLOCK_M, BLOCK_K), dtype)
    B_local = T.alloc_fragment((BLOCK_K, BLOCK_N), dtype)
    C_local = T.alloc_fragment((BLOCK_M, BLOCK_N), accum_dtype)

    T.clear(C_local)
    for k in T.Serial(K // BLOCK_K):
        T.copy(A[pid_m * BLOCK_M, k * BLOCK_K], A_local)
        T.copy(B[k * BLOCK_K, pid_n * BLOCK_N], B_local)
        T.gemm(A_local, B_local, C_local)

    T.copy(C_local, C[pid_m * BLOCK_M, pid_n * BLOCK_N])
```

再看优化版，**只有缓冲类型和（下一节的）循环类型不同**：

[A、B 改用 T.alloc_shared，C_local 保留为 fragment（ans/08-matrix.py:L254-L268）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L254-L268)：

```python
with T.Kernel(T.ceildiv(N, BLOCK_N), T.ceildiv(M, BLOCK_M), threads=128) as (
    pid_n, pid_m,
):
    A_shared = T.alloc_shared((BLOCK_M, BLOCK_K), dtype)
    B_shared = T.alloc_shared((BLOCK_K, BLOCK_N), dtype)
    C_local = T.alloc_fragment((BLOCK_M, BLOCK_N), accum_dtype)   # 仍是 fragment

    T.clear(C_local)
    for k in T.Pipelined(K // BLOCK_K, num_stages=3):             # 见 4.2 节
        T.copy(A[pid_m * BLOCK_M, k * BLOCK_K], A_shared)
        T.copy(B[k * BLOCK_K, pid_n * BLOCK_N], B_shared)
        T.gemm(A_shared, B_shared, C_local)

    T.copy(C_local, C[pid_m * BLOCK_M, pid_n * BLOCK_N])
```

两段代码逐行对比，区别只有三处：`alloc_fragment` → `alloc_shared`（A、B）、`T.Serial` → `T.Pipelined`（4.2 节）、以及循环体里引用的变量名 `A_local/B_local` → `A_shared/B_shared`。题目文档所说的「just a few lines of code changes」即指于此（[puzzles/08-matrix.py:L188-L208](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L188-L208)）。

#### 4.1.4 代码实践

> 类型：源码阅读型实践。

1. **实践目标**：亲眼看到「A、B 从寄存器搬到了共享内存」这件事确实发生在生成的 CUDA 代码里。
2. **操作步骤**：
   - 在能编译运行的机器上，执行 `run_matmul_opt`（见 [ans/08-matrix.py:L273-L300](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L273-L300)），它会把朴素版与优化版的 CUDA 源码都打印出来。
   - 在优化版的输出里搜索关键字 `__shared__`（CUDA 中声明共享内存的语法）。
   - 在朴素版的输出里同样搜索 `__shared__`，对比出现次数与用途。
3. **需要观察的现象**：优化版应当能搜到用于 A、B tile 的 `__shared__` 缓冲；朴素版要么没有、要么只有少量（与 A/B tile 无关的用途）。
4. **预期结果**：A、B 两个 tile 在优化版里以 `__shared__` 形式存在；C 的累加器仍落在寄存器中。
5. **待本地验证**：不同 TileLang 版本生成的 CUDA 写法略有差异，`__shared__` 的具体数量请以你本机输出为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 C_local 留在寄存器、却把 A_shared/B_shared 挪到共享内存，而不是反过来？

> **答**：C_local 是累加器，在整个 K 循环里被反复读写，访问频率最高，必须用最快的寄存器；A、B 的每个 tile 只参与一次 `T.gemm` 后即丢弃，是低频一次性输入，放进稍慢但仍片上的共享内存即可，挪走它们能腾出宝贵的寄存器预算给累加器，避免溢出。

**练习 2**：把 A、B 全放进共享内存有没有代价？

> **答**：有。共享内存每个 SM 容量有限（百余 KB 量级），A、B tile 越大、流水线级数（见 4.2）越多，shared memory 占用越高；超过上限会编译失败或被迫缩小 tile / 降低流水线级数。共享内存优化是用「片上存储容量」换「寄存器预算与访存效率」。

### 4.2 软件流水线（T.Pipelined, num_stages）

#### 4.2.1 概念说明

即便把 A、B 挪进了共享内存，朴素版用 `T.Serial` 写的 K 循环仍有一个低效点：**访存与计算是串行的**。每一段迭代「先 `T.copy` 把 A、B 从显存搬进 shared，再 `T.gemm` 计算」，GPU 必须**等搬完才算**、**算完才搬下一段**。搬运（访存）期间 Tensor Core 闲置，计算期间显存带宽闲置，两者互不重叠。

「软件流水线（software pipeline）」正是用来重叠它们的。直觉是：在算第 `k` 段 `T.gemm` 的同时，**提前把第 `k+1`、`k+2` 段的 A、B 也搬起来**，让显存加载与 Tensor Core 计算像两条并行的传送带那样同时运转，从而**用计算时间掩盖访存延迟**。题目文档把它描述为「overlap the loading of A and B tiles with the computation of the GEMM operation」（[puzzles/08-matrix.py:L200-L207](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L200-L207)）。

在 TileLang 里，把 `T.Serial` 换成 `T.Pipelined` 并指定 `num_stages`（流水线级数）即可让编译器自动生成这种调度：

```python
for k in T.Pipelined(K // BLOCK_K, num_stages=3):
    ...
```

`num_stages=3` 表示流水线深度为 3——同时维持约 3 段在途的 tile，访存和计算错开推进。

> **命名小贴士**：题目文档字符串里把它写成 `T.Pipeline`（[puzzles/08-matrix.py:L203](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L203)），但参考答案与实际 API 用的是 `T.Pipelined`（带 `-ed` 后缀）。以 `T.Pipelined` 为准，文档里是笔误。

#### 4.2.2 核心流程

朴素 `T.Serial`：每段迭代严格「搬完才算」，访存与计算不重叠：

```
段 k:   [搬A][搬B]              [gemm]
段 k+1:                        [搬A][搬B]              [gemm]
时间 →  ────────────────────────────────────────────────
        ↑ gemm 期间显存空闲      ↑ 搬运期间 Tensor Core 空闲
```

`T.Pipelined(num_stages=3)`：把搬运提前，访存与计算重叠（稳态）：

```
段 k:            [搬A][搬B]──────[gemm]
段 k+1:        [搬A][搬B]──────[gemm]
段 k+2:      [搬A][搬B]──────[gemm]
时间 →  ─────────────────────────────────────
        ↑ 计算第 k 段时，第 k+1、k+2 段已在搬运 ↑
```

编译器会把循环改造成三段：
1. **prologue（预热）**：先把前 `num_stages` 段的 A、B 预取进 shared，填满流水线。
2. **steady state（稳态）**：每一步「算第 k 段」与「搬第 k+num_stages 段」同时进行——这就是性能提升的主要来源。
3. **epilogue（排空）**：最后几段只剩计算，把仍在流水线里的数据算完。

代价：流水线要为「在途」的多段 tile 准备缓冲，因此 `num_stages` 越大，shared memory 占用越高（与 4.1 的共享内存预算互相制约）；过大可能编译失败，需要权衡。

#### 4.2.3 源码精读

[优化版用 T.Pipelined(num_stages=3) 替换 T.Serial（ans/08-matrix.py:L262-L266）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L262-L266)：

```python
T.clear(C_local)
for k in T.Pipelined(K // BLOCK_K, num_stages=3):
    T.copy(A[pid_m * BLOCK_M, k * BLOCK_K], A_shared)
    T.copy(B[k * BLOCK_K, pid_n * BLOCK_N], B_shared)
    T.gemm(A_shared, B_shared, C_local)
```

对比朴素版的 [ans/08-matrix.py:L173-L177](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L173-L177)：

```python
T.clear(C_local)
for k in T.Serial(K // BLOCK_K):
    T.copy(A[pid_m * BLOCK_M, k * BLOCK_K], A_local)
    T.copy(B[k * BLOCK_K, pid_n * BLOCK_N], B_local)
    T.gemm(A_local, B_local, C_local)
```

**循环体完全一样**（两段 `T.copy` + 一段 `T.gemm`），唯一变化是 `T.Serial(K // BLOCK_K)` → `T.Pipelined(K // BLOCK_K, num_stages=3)`。所有 prologue / 稳态 / epilogue 的调度都由编译器根据 `num_stages` 自动生成，开发者只声明「我想把这段循环流水化、深度为 3」。

#### 4.2.4 代码实践

> 类型：改参数观察行为。

1. **实践目标**：感受 `num_stages` 对流水线深度与性能的影响。
2. **操作步骤**：在 [ans/08-matrix.py:L263](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L263) 把 `num_stages=3` 分别改成 `1`、`2`、`3`、`4`，每次用 `bench_puzzle` 记录耗时（`run_matmul_opt` 会自动调用，见 [ans/08-matrix.py:L299-L300](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L299-L300)）。
3. **需要观察的现象**：
   - `num_stages=1` 通常与朴素串行接近（无重叠）。
   - 随级数增大，访存与计算重叠增加，耗时下降。
   - 级数大到一定程度（占用 shared memory 过多），可能出现编译报错或耗时不再改善。
4. **预期结果**：在 `num_stages=2~3` 附近通常能拿到明显加速；继续增大会受 shared memory 容量约束。
5. **待本地验证**：最佳 `num_stages` 随 GPU 型号、问题规模、`BLOCK_K` 而变，请以本机实测为准。

#### 4.2.5 小练习与答案

**练习 1**：把 `T.Pipelined` 换回 `T.Serial`（保留 shared memory 优化），还会快吗？

> **答**：会有一定提升（因为 A、B 落在 shared、`T.gemm` 读得更快、寄存器压力更小），但**访存与计算仍不重叠**——每段还是「搬完才算」，显存延迟无法被计算掩盖。要拿到本讲的最大加速，shared memory 优化与软件流水线通常**配合使用**。

**练习 2**：`num_stages` 是越大越好吗？

> **答**：不是。级数越大，需要为在途 tile 预留的 shared buffer 越多，一旦超过 SM 的 shared memory 上限就编译失败；同时 prologue/epilogue 的开销也会增加。实际要结合 `BLOCK_K` 与 shared memory 预算权衡，典型经验值在 2~4。

### 4.3 朴素 vs 优化的性能对比

#### 4.3.1 概念说明

本节把前两节的改动合起来看效果。两项优化彼此独立又互补：

- **共享内存优化**解决「寄存器不够用、数据落点不对」——属于**容量与布局**问题。
- **软件流水线**解决「访存与计算不重叠」——属于**时序（latency hiding）**问题。

二者叠加后，GEMM 才真正接近手写 CUDA / cuBLAS 的水准。如何客观地「看到」这个提升？项目给出的标准做法是「正确性 + 生成代码 + 耗时」三件套（u2-l2 已建立）：

- `test_puzzle` 确认结果与 torch 一致（正确性不变）。
- `compile().print_source_code()` 打印生成的 CUDA，对比朴素版与优化版的差异（生成代码）。
- `bench_puzzle` 用 CUDA Event 计时（性能）。

`bench_puzzle` 的计时方法学（[common/utils.py:L109-L155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L155)）保证了对比公平：

- **warmups=10**：先空跑 10 次预热，避免首次编译/缓存带来的干扰。
- **repeats=100**：正式计时跑 100 次取平均，降低抖动。
- **CUDA Event + synchronize**：用 GPU 端事件戳计时，并在前后 `torch.cuda.synchronize()` 确保计时窗口覆盖真正的 kernel 执行。
- 最终耗时 = 总时间 / repeats，单位 ms。

题目脚本 `run_matmul_opt` 把这套流程串了起来：它先编译并打印两个版本的源码，再分别 `bench_puzzle` 并以 `bench_torch=True` 附上 torch 参考耗时。

#### 4.3.2 核心流程

`run_matmul_opt` 的执行顺序（[puzzles/08-matrix.py:L225-L252](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L225-L252)）：

1. 固定问题规模 `M=N=K=4096`、`BLOCK_M=BLOCK_N=128`、`BLOCK_K=64`，组成 `args_dict`。
2. 编译朴素版并 `print_source_code()`。
3. 编译优化版并 `print_source_code()`。
4. 对朴素版调用 `bench_puzzle(..., bench_torch=True)`，打印 torch 与 TileLang 耗时。
5. 对优化版同样 `bench_puzzle(..., bench_torch=True)`。

最终你会拿到三个数：torch 耗时、TileLang 朴素版耗时、TileLang 优化版耗时，可以直接比较加速比。

#### 4.3.3 源码精读

[题目对比脚本 run_matmul_opt：编译两个版本、打印源码、分别 bench（puzzles/08-matrix.py:L243-L252）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L243-L252)：

```python
naive_matmul_kernel = tl_matmul_naive.compile(**args_dict)
naive_matmul_kernel.print_source_code()

opt_matmul_kernel = tl_matmul_opt.compile(**args_dict)
opt_matmul_kernel.print_source_code()

bench_puzzle(tl_matmul_naive, ref_matmul, args_dict, bench_torch=True)
bench_puzzle(tl_matmul_opt, ref_matmul, args_dict, bench_torch=True)
```

而 `bench_puzzle` 内部的关键计时片段（[common/utils.py:L143-L155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L143-L155)）：

```python
for _ in range(warmups):          # 预热
    tl_kernel(*inputs_in_torch_tensors)

tl_start = torch.cuda.Event(enable_timing=True)
tl_end = torch.cuda.Event(enable_timing=True)
torch.cuda.synchronize()
tl_start.record()
for _ in range(repeats):           # 正式计时 100 次
    tl_kernel(*inputs_in_torch_tensors)
tl_end.record()
torch.cuda.synchronize()
tl_time = tl_start.elapsed_time(tl_end) / repeats
print(f"{bench_name} time: {tl_time:.3f} ms")
```

#### 4.3.4 代码实践

> 类型：运行示例 + 数据记录。

1. **实践目标**：用真实数字体会两项优化带来的加速。
2. **操作步骤**：
   - 先补全 `tl_matmul_opt`（详见第 5 节综合实践），确保 `test_puzzle` 通过。
   - 运行 `python3 ans/08-matrix.py`（或你的 `puzzles/08-matrix.py`），观察 `run_matmul_opt` 的输出。
   - 记录三行耗时：`Torch time`、朴素版的 `Tilelang time`、优化版的 `Tilelang time`。
   - 计算加速比：`优化版 / 朴素版`、`优化版 / torch`。
3. **需要观察的现象**：优化版应明显快于朴素版；与 torch.matmul 的差距应显著缩小（题目规模下未必能超过 cuBLAS，但应接近一个量级）。
4. **预期结果**：优化版相对朴素版有数倍量级的提速（**待本地验证**，具体倍数取决于 GPU 与环境）。
5. **待本地验证**：本讲不假定任何具体耗时数字，请以你本机 `bench_puzzle` 输出为准并记录下来。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bench_puzzle` 要先 warmup 再计时？

> **答**：首次调用涉及 JIT 编译、kernel 加载、缓存填充、GPU 频率爬升等一次性开销，会让首次执行远慢于稳态。先空跑 10 次预热，让这些开销在计时窗口之外发生，测到的才是 kernel 本身的稳态性能。

**练习 2**：如果优化版相对朴素版「没快多少」，可能的原因有哪些？

> **答**：常见原因包括：(1) 问题规模太小，访存/计算占比不足以体现流水线收益；(2) `num_stages` 设得不当（过小无重叠、过大触发 shared memory 不足）；(3) tile 尺寸选择不当导致 occupancy 低；(4) 朴素版因规模小本就没有严重溢出。可用 `print_source_code` 检查是否真的生成了流水线调度，并调整 `BLOCK_K` / `num_stages` 复测。

## 5. 综合实践

**任务**：补全 [puzzles/08-matrix.py:L211-L222](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L211-L222) 的 `tl_matmul_opt`，并在本机完成一份「优化前后对比报告」。

**步骤**：

1. **写出 `tl_matmul_opt`**：以朴素版 [tl_matmul_naive（ans/08-matrix.py:L155-L181）](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L155-L181) 为模板，做两处改动：
   - 把 `A_local`、`B_local` 的 `T.alloc_fragment` 改为 `T.alloc_shared`（`C_local` 保持 `T.alloc_fragment`）。
   - 把 `for k in T.Serial(K // BLOCK_K)` 改为 `for k in T.Pipelined(K // BLOCK_K, num_stages=3)`。
   - 循环体里的 `A_local`/`B_local` 改名为 `A_shared`/`B_shared`，其余（`T.copy`、`T.gemm`、`T.clear`、写回 C）一字不动。
   - 期望实现与参考答案 [ans/08-matrix.py:L244-L270](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L244-L270) 一致。
2. **验证正确性**：运行 `run_matmul_opt`，确认 `test_puzzle` 打印 `✅ Results match: True`（容差 `atol=rtol=1e-2`，float16）。
3. **对比生成代码**：阅读打印出的两份 CUDA 源码，找出优化版新增的 `__shared__` 缓冲与流水线调度（prologue / 稳态 / epilogue 结构），记录 1~2 处差异。
4. **对比耗时**：记录 torch、朴素版、优化版三者耗时，计算加速比。
5. **写一份简短报告**（半页以内）：包含正确性结论、生成代码差异要点、加速比，以及你对「哪一项优化贡献更大」的判断（可在 4.2.4 的实践中分别关掉某一项来验证）。

**预期结果**：结果与 torch 对齐；优化版明显快于朴素版。具体数字与本机最优 `num_stages` **待本地验证**。

## 6. 本讲小结

- 朴素 GEMM 把 A、B、C 三个 tile 全放进 fragment（寄存器），累加器 `C_local` 在 `BLOCK_M=BLOCK_N=128` 时就占掉约 64 KB / 128 寄存器每线程，叠加 A、B 后易引发**寄存器溢出**。
- 共享内存优化：把一次性的输入 tile A、B 用 `T.alloc_shared` 挪进片上共享内存，把高频读写的累加器 `C_local` 留在寄存器；这既省寄存器，又对齐了 `T.gemm`（Tensor Core MMA）偏好的输入落点。
- 软件流水线：用 `T.Pipelined(K // BLOCK_K, num_stages=3)` 替换 `T.Serial`，让「搬下一段 tile」与「算当前段 gemm」重叠，用计算掩盖显存延迟；编译器自动生成 prologue / 稳态 / epilogue。
- 这两项改动加起来只是「换缓冲类型 + 换循环类型」的几行代码，计算语义完全不变，却带来数倍加速（具体待本地验证）。
- 用 `compile().print_source_code()` 看生成 CUDA、用 `bench_puzzle`（warmup=10、repeats=100、CUDA Event + synchronize）公平计时，是验证「正确性 + 生成代码 + 性能」的标准三件套。
- `num_stages` 受 shared memory 容量约束，并非越大越好，需结合 `BLOCK_K` 权衡；文档字符串里的 `T.Pipeline` 是笔误，实际 API 为 `T.Pipelined`。

## 7. 下一步学习建议

- **横向迁移**：把本讲的两项优化套到下一单元的卷积（Puzzle 09）和量化矩阵乘（Puzzle 10）上——你会发现 `T.alloc_shared` + `T.Pipelined` 是 TileLang 高性能 kernel 的通用骨架。可先读 [puzzles/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py) 与 [puzzles/10-dequant-mm.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/10-dequant-mm.py)。
- **性能工程专题**：直接进入 u5-l4，系统地学习「调参方法论」——固定问题规模，扫描 `BLOCK_K` 与 `num_stages`，结合 `print_source_code` 诊断，建立 shared vs fragment、tile 尺寸、流水线级数的调参直觉。
- **回到本讲做对照实验**：作为热身，在本讲代码上分别「只加 shared」「只加 pipeline」「两者都加」，用 `bench_puzzle` 量化每一项的独立贡献，亲手验证它们互补的判断。
