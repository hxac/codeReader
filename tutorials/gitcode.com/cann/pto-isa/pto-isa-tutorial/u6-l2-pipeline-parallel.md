# u6-l2 流水线并行：事件驱动的 double buffer

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 PTO kernel 中「软件可见的流水线阶段」有哪些，以及它们与硬件流水线（MTE2/MTE1/M/FIX/V）的对应关系。
2. 掌握 double buffer（乒乓缓冲）模式的构成要素：双份缓冲、槽位编号、按槽翻转、按槽同步。
3. 读懂 `gemm_performance` 中由 `set_flag`/`wait_flag` 编排的四段流水，并能解释每一条事件保护的是哪条依赖。
4. 识别流水线气泡的常见来源：warm-up/drain、粗粒度等待、缓冲深度不足、单缓冲串行化。
5. 在 CPU 仿真的 gemm 基线上手工构造一个两级流水（load 与 compute 重叠一轮），并对比改造前后的指令序列。

## 2. 前置知识

本讲默认你已掌握前几讲的内容，特别是：

- **u2-l3 事件与同步**：一个事件 flag 由 `(srcPipe, dstPipe, eventId)` 三元组唯一确定；`set_flag` 由源流水线执行（生产者挂牌），`wait_flag` 由目标流水线执行（消费者等牌）；同一对流水线只有 8 个事件编号可用。
- **u3-l1 TLOAD/TSTORE 与 u5-l1 TMatmul**：TLOAD 把 GM 数据搬入片上（挂 MTE2 流水线），TSTORE 把结果写回 GM（挂 FIX 流水线），TMATMUL 在 Cube 单元上做矩阵乘（挂 M 流水线）。
- **u5-l2 GEMM 基线**：四级数据通路 TLOAD(MTE2)→TMOV/TEXTRACT(MTE1)→TMATMUL(M)→TSTORE(FIX)，以及基线版已使用的最简单乒乓。
- **u6-l1 多核编程**：核间切分（本讲的流水线是**核内**话题，与核间切分正交）。

两个本讲反复使用、值得先记住的事实：

1. **程序书写顺序 ≠ 硬件完成顺序**。昇腾 AICORE 上 MTE2（搬入）、MTE1（片上搬移）、M（Cube 计算）、FIX（写回）、V（向量）是并行执行的硬件队列，一条指令发射后立刻返回，不等待完成。跨流水线的"生产-消费"依赖必须用事件显式表达；同一条流水线内部则天然按序。
2. **CPU 仿真把同步做成空桩**。`include/pto/common/cpu_stub.hpp:118-119` 把 `set_flag`/`wait_flag` 定义为空函数，CPU 仿真单线程按程序顺序执行指令。因此 CPU 仿真只能验证**功能正确性**，流水线时序是否真正重叠、事件是否配对正确，必须上真机（或 CostModel）才能检验。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/coding/pipeline-parallel.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md) | 流水线并行的官方方法论文档：阶段划分、缓冲角色、warm-up/steady/drain 三阶段、调优流程 |
| [kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp) | 本讲的主标本：四级流水 + 两级 double buffer + stepK 批量搬运的完整事件编排 |
| [kernels/manual/a2a3/gemm_performance/README.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md) | 优化说明与实测利用率表（TLOAD/TEXTRACT/TMATMUL Ratio），气泡判读的依据 |
| [demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp) | 单级乒乓的最简参照实现，用于和 gemm_performance 对照 |
| [demos/cpu/gemm_demo/gemm_demo.cpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp) | CPU 仿真下的"无流水"gemm 基线，是本讲综合实践的改造对象 |
| [docs/coding/Event.md](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md) | 事件模型文档：`Event<SrcOp,DstOp>` 对象风格与裸 flag 风格的对照 |
| [include/pto/common/cpu_stub.hpp](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp) | CPU 仿真桩：证明 `set_flag`/`wait_flag` 在仿真下为空操作 |

## 4. 核心概念与源码讲解

### 4.1 流水线模型

#### 4.1.1 概念说明

流水线并行（pipeline parallelism）指的是：把 kernel 的工作拆成若干**阶段**（stage），让不同 tile 在不同阶段上同时推进——当第 k 块数据在计算时，第 k+1 块可以正在搬运。它与多核并行正交：多核是"把不同 tile 分给不同核"，流水线是"同一个核内让搬入、片上搬移、计算、写回四条硬件队列同时忙起来"。

[docs/coding/pipeline-parallel.md:13-15](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L13-L15) 给出典型高层视图：

```text
TLOAD -> layout / staging transform -> compute -> TSTORE
```

中间阶段可能是 `TEXTRACT`、`TMOV`、`TTRANS`、向量指令或 `TMATMUL`（[docs/coding/pipeline-parallel.md:17-23](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L17-L23)）。

对 GEMM 这类 Cube 算子，四个软件阶段与硬件流水线的对应关系是：

| 软件阶段 | PTO 指令 | 硬件流水线 | 数据通路 |
| --- | --- | --- | --- |
| 搬入 | `TLOAD` | `PIPE_MTE2` | GM → L1 |
| 片上搬移/切片 | `TEXTRACT` / `TMOV` | `PIPE_MTE1` | L1 → L0A/L0B |
| 计算 | `TMATMUL` / `TMATMUL_ACC` | `PIPE_M` | L0A/L0B → L0C |
| 写回 | `TSTORE` | `PIPE_FIX` | L0C → GM |

`gemm_performance` 源码开头的注释就是这份对照表的落地版——[gemm_performance_kernel.cpp:18-25](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L18-L25) 写明：代码用 `PIPE_MTE*` 等硬件 pipe 表达同步，注释则按高层 PTO 指令称呼各阶段，便于调优时对照。

#### 4.1.2 核心流程

一个流水线化的 kernel 按三段理解最容易（[docs/coding/pipeline-parallel.md:76-84](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L76-L84)）：

```text
1. warm-up（预热）  : 用最早的 tile 填充流水线，此时计算阶段还闲着
2. steady state    : 各阶段在不同 tile 上重叠推进，吞吐最高
3. drain（排空）   : 搬运已结束，计算/写回把在途工作收尾
```

文档特别提醒：**很多流水线错误源于把首迭代和末迭代当成 steady-state 迭代对待**——它们的依赖结构确实不同（首轮没有"上一轮"可等，末轮 set 出的事件没有下一轮来 wait）。4.3 节会看到 `gemm_performance` 如何用 `InitSyncFlags`/`WaitSyncFlags` 专门处理首尾。

理想 steady state 下，三段工作的时序重叠示意：

```text
时间 ─────────────────────────────────────────────►
MTE2: │ LOAD tile0 │ LOAD tile1 │ LOAD tile2 │ LOAD tile3 │
MTE1:             │ EXT tile0  │ EXT tile1  │ EXT tile2  │ ...
M  :                         │ MM tile0   │ MM tile1   │ MM tile2 │ ...
FIX :                                     │ STORE t0   │ STORE t1 │ ...
```

每个阶段在时间上错开一拍，之后四条队列同时满载。流水线有效的条件（[docs/coding/pipeline-parallel.md:86-100](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L86-L100)）：搬运和计算都占可观时间、中间结果可以用有界缓冲暂存、同步图足够精细以保住重叠。若某一阶段占了几乎全部时间，或者缓冲压力太大，流水线收益会趋近于零——**先确认瓶颈再谈重叠**。

#### 4.1.3 源码精读

先看 CPU 仿真下的**反例**——`demos/cpu/gemm_demo` 是一个完全没有流水线的基线：

- [gemm_demo.cpp:109-121](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L109-L121)：两次 `TLOAD` 一次性搬入整个 A、B，两次 `TMOV` 降入 L0，然后循环 50 次 `TMATMUL`，最后一次 `TSTORE`。整个 kernel 没有任何 `set_flag`/`wait_flag`，也没有 K 维分块——搬运与计算是**全序串行**的两段。

它的价值恰恰是"干净"：没有分块就没有 warm-up/drain 问题，是我们综合实践中改造成两级流水的起点。

再看正例的骨架。`gemm_performance` 把 GEMM 组织成三层循环（i 行块 × j 列块 × kIter K 块），每个 kIter 调用一次 `ProcessKIteration`：

- [gemm_performance_kernel.cpp:220-231](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L220-L231)：三重循环体，K 循环内做"搬入+切片+计算"，(i,j) 块收尾时调用 `StoreResult` 写回。launch 常数在 [gemm_performance_kernel.cpp:254-267](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L254-L267)：24 核、base block \([128, 256, 64]\)、`stepKa=stepKb=4`。

对照最简版：基线 [gemm_basic_custom.cpp:47-72](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47-L72) 的 `ProcessKIteration` 也是同样的"搬入→片上搬移→计算"三段，只是它用 `TMOV` 整块搬、无 stepK 批量、L1 与 L0 共用同一个槽位编号 `cur = kIter % 2`。两版对照是理解"流水线模型相同、缓冲策略升级"的最好材料。

#### 4.1.4 代码实践

**实践目标**：确认 CPU gemm 基线是全序串行结构，建立"无流水"与"有流水"的直觉对比。

**操作步骤**：

1. 阅读 [gemm_demo.cpp:109-121](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L109-L121)，数一数有几条 TLOAD/TMOV/TMATMUL/TSTORE。
2. 在本地运行 CPU demo：`python3 tests/run_cpu.py --demo gemm --verbose`（详见 u1-l3）。

**需要观察的现象**：demo 输出 `max_abs_diff` 与 `perf: avg_ms=... gflops=...` 行；整个执行序列中搬运只发生一次。

**预期结果**：`max_abs_diff` 小于 `1e-3` 阈值（demo 用 `gemm_naive` 三重循环做 golden 比对，[gemm_demo.cpp:128-142](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L128-L142)），退出码 0。具体输出数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gemm_demo` 不需要任何 `set_flag`/`wait_flag` 也能在真机上算对？

**答案**：它没有 K 维分块——A、B 各只搬一次、整块进 L0、算完再写回，阶段之间天然全序；且 CPU 仿真本身单线程按序执行，事件是空桩。但正因为它全序串行，搬运期间 Cube 空转、计算期间 MTE2 空转，这是它作为"基线"的含义。

**练习 2**：软件阶段"四个"是固定的吗？

**答案**：不是。[docs/coding/pipeline-parallel.md:17-23](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L17-L23) 说明中间阶段可以是 TEXTRACT/TMOV/TTRANS/向量指令等，取决于 kernel：向量算子可能只有"搬入→V 计算→写回"三段，`gemm_performance` 则是四段（多出 TEXTRACT 做 L1→L0 切片）。

### 4.2 double buffer 模式

#### 4.2.1 概念说明

流水线重叠需要缓冲支撑。核心思想一句话（[docs/coding/pipeline-parallel.md:50-58](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L50-L58)）：**当后一阶段正在消费某块 tile 时，前一阶段可以同时为下一块 tile 准备数据**。

最简单的实现就是 double buffer（乒乓缓冲）：

1. 在同一级存储上开两份等大的槽（ping 槽 0 / pang 槽 1）。
2. 用一个 0/1 翻转的槽位变量（slot flag）指示"本轮写哪个槽、读哪个槽"。
3. 写入方在覆写槽 \(s\) 前必须先等读出方用完槽 \(s\)；读出方在读槽 \(s\) 前必须等写入方写完槽 \(s\)。两个方向的保护各用一对事件。

为什么是"两"份？因为依赖链是"上一轮的读"与"这一轮的写"竞争同一地址：深度为 2 时，第 k 轮读槽 \(k \bmod 2\)、写槽 \((k+1) \bmod 2\)，恰好错开一轮，实现"load 与 compute 重叠一轮"。深度更深（多缓冲）可容忍更长的搬运延迟，代价是片上容量。

`gemm_performance` 里有**两级**独立翻转的 double buffer：

- **L1 级**（`aMatTile[2]`/`bMatTile[2]`）：TLOAD 的落点，槽位变量 `mte2DBFlag`。特殊之处在于配合 `stepKa=4` 批量搬运——一次 TLOAD 搬进 4 个 K 块的 panel，所以 L1 槽的持有期横跨 4 轮 kIter，每 4 轮才翻转一次。
- **L0 级**（`aTile[2]`/`bTile[2]`，L0A/L0B）：TEXTRACT 的落点，槽位变量 `mte1DBFlag`，每轮 kIter 都翻转，是标准的一轮一换乒乓。

两级缓冲深度不必相同——L1 层的目的是摊薄 DMA 启动开销（一次搬 4 片），L0 层的目的是让切片与计算错开一轮。

#### 4.2.2 核心流程

稳态下（一个 stepK 组内 4 轮 kIter）的推进逻辑：

```text
组 g 首轮 (kModstepKa == 0):
    wait (MTE1→MTE2, 槽 mte2DBFlag)      # 等 MTE1 把组 g-2 读过的 L1 槽还回来
    TLOAD aMatTile[mte2DBFlag], bMatTile[mte2DBFlag]   # 异步 DMA，立刻返回
    mte2DBFlag 翻转                         # 指向下一组要填的槽

每轮 kIter:
    wait (M→MTE1, 槽 mte1DBFlag)          # 等 TMATMUL 用完这个 L0 槽
    TEXTRACT aTile[mte1DBFlag] ← aMatTile[当前 L1 槽]   # 切出本轮 baseK
    TEXTRACT bTile[mte1DBFlag] ← bMatTile[当前 L1 槽]
    TMATMUL(_ACC) cTile × aTile[mte1DBFlag] × bTile[mte1DBFlag]
    mte1DBFlag 翻转                         # 下一轮用另一个 L0 槽

组 g 末轮 ((kIter+1) % stepKa == 0):
    set (MTE1→MTE2, 当前 L1 槽)            # 释放 L1 槽，允许下一组 TLOAD 覆写
```

三队列时间线（稳态、一组 4 轮）：

```text
MTE2: │═ TLOAD 组 g+1 panel（64+128 KiB，异步）═════════════│
MTE1: │ EXT k0 │ EXT k1 │ EXT k2 │ EXT k3 │
M  :            │ MM k0  │ MM k1  │ MM k2  │ MM k3 │
       （FIX 仅在每个 (i,j) 块的 StoreResult 阶段出现）
```

缓冲容量是硬约束。L0A/L0B 乒乓各半区 32 KiB（[gemm_performance_kernel.cpp:16](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L16)），所以每个 L0 tile 的字节数不得超过：

\[ \text{L0A tile 字节} = baseM \times baseK \times 2 = 128 \times 64 \times 2 = 16\,\text{KiB} \le 32\,\text{KiB} \]

\[ \text{L0B tile 字节} = baseK \times baseN \times 2 = 64 \times 256 \times 2 = 32\,\text{KiB} \le 32\,\text{KiB} \]

B tile 恰好填满预算——这正是 README 所说"prefer tile sizes that fully utilize the 32 KiB budget"的含义（[README.md:127-144](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L127-L144)）。若想再加大 baseN，就放不进乒乓半区，只能降低 stepK 或缩小其他维——**缓冲容量反过来约束分块形状**，这是流水线设计的核心权衡。

#### 4.2.3 源码精读

缓冲的声明与摆放全部在 `RunGemmE2E` 里：

- [gemm_performance_kernel.cpp:15-16](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L15-L16)：`BUFFER_NUM = 2` 与 `L0_PINGPONG_BYTES = 32 * 1024`（L0A/L0B 乒乓按 32 KiB 一分为二）。
- [gemm_performance_kernel.cpp:182-196](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L182-L196)：Tile 类型定义与数组声明。注意 L1 级 `TileMatA` 的容量形状是 `[baseM, baseK * stepKa]`——一个槽装 4 个 K 块；L0 级 `LeftTile`/`RightTile` 只有 `[baseM, baseK]`/`[baseK, baseN]`——一个槽装 1 个 K 块。
- [gemm_performance_kernel.cpp:198-210](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L198-L210)：Manual 模式下的显式摆放。L1 四个槽在 L1 地址空间首尾相接（偏移逐段累加）；L0A/L0B 的乒乓则分别用 `0x0` 和 `0x0 + L0_PINGPONG_BYTES`——这正是 32 KiB 半区的出处。回顾 u3-l2：TASSIGN 只做摆放不查重叠，排布不重叠不越界是开发者的责任。
- [gemm_performance_kernel.cpp:212-215](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L212-L215)：`mLoop/nLoop/kLoop`（12/4/96）与两个槽位变量的初始化。

槽位翻转的代码散布在 `ProcessKIteration`：

- [gemm_performance_kernel.cpp:103](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L103)：L1 槽每 4 轮翻转一次（仅在 `kModstepKa == 0` 的分支内）。
- [gemm_performance_kernel.cpp:106](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L106)：`currMte2Idx = (mte2DBFlag == 0) ? 1 : 0`——本轮 TEXTRACT 要读的 L1 槽是**刚填好的那个**（flag 已翻转，取反即当前数据槽）。
- [gemm_performance_kernel.cpp:129-130](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L129-L130)：L0 槽每轮翻转。

与基线对照：[gemm_basic_custom.cpp:47](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47) 用 `int cur = kIter % 2` 一个变量同时充当 L1 与 L0 的槽位编号，因为基线一步 TLOAD 只装一轮的数据，两级缓冲同步翻转。gemm_performance 把两级解耦后，才获得"每 4 轮一次大 DMA"的带宽收益。

#### 4.2.4 代码实践

**实践目标**：核算 gemm_performance 的全部缓冲占用，验证 32 KiB 约束与 L1 槽容量。

**操作步骤**：

1. 读 [gemm_performance_kernel.cpp:198-210](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L198-L210) 的四个 L1 偏移与四个 L0 偏移。
2. 手工计算（fp16 按 2 字节、fp32 按 4 字节）：
   - `aMatTile` 单槽 = \(128 \times (64 \times 4) \times 2\) 字节 = 64 KiB，两槽共 128 KiB；
   - `bMatTile` 单槽 = \((64 \times 4) \times 256 \times 2\) 字节 = 128 KiB，两槽共 256 KiB；
   - L1 staging 合计 384 KiB；
   - `aTile` 单槽 16 KiB、`bTile` 单槽 32 KiB，各自两槽恰好落在两个 32 KiB 半区内；
   - `cTile`（TileAcc，fp32）= \(128 \times 256 \times 4\) = 128 KiB，位于 L0C。

**需要观察的现象**：L0B 的单槽恰好等于半区大小，没有任何余量。

**预期结果**：所有 L0 tile ≤ 32 KiB；结论与 [README.md:130-144](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L130-L144) 的算例一致。若把 `baseN` 从 256 提到 512，`bTile` 单槽变为 64 KiB 超出半区——TASSIGN 静态检查（SA-0351～0354，见 u3-l2）会在 NPU 编译期报错，而 CPU 仿真因跳过检查不会发现，需要等真机验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `stepKa` 从 4 改成 8（假设 L1 容量足够），L1 槽的翻转周期和 TLOAD 的触发频率如何变化？

**答案**：L1 单槽容量翻倍（A 槽变为 \(128 \times 512 \times 2 = 128\) KiB），翻转周期从每 4 轮变为每 8 轮；TLOAD 触发频率减半、单次 DMA 字节数翻倍，DMA 启动开销被摊得更薄，但 L1 占用也翻倍，且下一组数据要等更久才发出，流水线对搬运延迟的容忍度下降。

**练习 2**：为什么 L0 级缓冲深度选 2 而不是 3？

**答案**：L0A/L0B 容量固定且很小（每核 64 KiB 量级，乒乓各 32 KiB）。深度 2 已实现"本轮计算与下轮切片重叠"；深度 3 需要三个半区，会把单槽压到约 21 KiB 以下，逼着缩小 base block，反而降低 Cube 效率。缓冲深度与分块大小是一对必须一起调的参数。

### 4.3 事件驱动调度

#### 4.3.1 概念说明

double buffer 给了"错开一轮"的**空间**（两份缓冲），事件同步给的是**纪律**：谁在什么条件下可以写/读哪个槽。PTO 有两种等价写法：

1. **裸 flag 风格**：`set_flag(srcPipe, dstPipe, id)` / `wait_flag(srcPipe, dstPipe, id)`。`gemm_performance` 与 `gemm_basic` 用这种。kernel 里封装了模板助手（[gemm_performance_kernel.cpp:37-46](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L37-L46)）：
   ```cpp
   template <pipe_t srcPipe, pipe_t dstPipe>
   AICORE inline void SetFlag(uint32_t id) { set_flag(srcPipe, dstPipe, static_cast<event_t>(id)); }
   ```
2. **Event 对象风格**：`Event<SrcOp, DstOp>` + `RecordEvent`（赋值即记录、传参即等待），见 [docs/coding/Event.md:77-95](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md#L77-L95) 的最小示例。注意 `Event` 类型只在设备构建（`__CCE_AICORE__`）下存在（[docs/coding/Event.md:7](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md#L7)），CPU 仿真路径只能用裸 flag 或干脆依赖程序顺序。

**初学者最大的困惑**：源码里经常出现 `SetFlag` 紧跟 `WaitFlag`（同一个 id），看起来像"自己通知自己、白做"。关键在于：`set_flag` 由**源流水线**的队列执行，`wait_flag` 由**目标流水线**的队列执行。两者在程序文本上相邻，却进入两条不同的硬件队列；程序顺序只在同一条队列内有意义，跨队列的先后只能靠这对 flag 传递。所以这对指令表达的是"当 MTE1 队列推进到此处（切片完成）时，才放行 M 队列中排在这条 wait 之后的计算"。

事件调度的三条纪律（承接 u2-l3）：

- 一个 `(srcPipe, dstPipe, eventId)` 三元组在一次在途窗口内**记录一次、等待一次**；每对流水线只有 8 个编号。深度为 2 的乒乓天然用编号 0/1 轮转，同一编号在下一个窗口到来前必须已完成配对。
- 只对**真实数据依赖**挂牌，不要用宽泛的屏障淹没并行（[docs/coding/pipeline-parallel.md:61-74](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L61-L74)）。
- 循环的首尾要单独处理：首轮的 `wait` 等的是尚未存在的事件，末轮的 `set` 没有后续消费者。

**流水线气泡**（某条队列空闲等待）的常见来源，对照本 kernel 一一有解：

| 气泡来源 | 表现 | gemm_performance 的对策 |
| --- | --- | --- |
| 无双缓冲 | 每轮"搬完才算、算完才搬"全序串行 | L1/L0 两级乒乓（4.2） |
| 搬运延迟 > 计算耗时 | Cube 等 MTE2，TMATMUL Ratio 低 | `stepKa=4` 批量搬运摊薄 DMA，且 TLOAD 提前一组发出 |
| 同步过粗 | 一轮内所有指令互相等待 | 事件按"槽位 + 操作数 A/B"细分（A、B 各占一个事件编号） |
| warm-up/drain 串行化 | 首末轮拖慢整循环 | `InitSyncFlags`/`WaitSyncFlags` 首尾补同步，主循环保持稳态形态 |
| 分块太小 | 每轮固定开销占比高 | base block \([128,256,64]\) 尽量填满 L0 |

#### 4.3.2 核心流程

`ProcessKIteration` 稳态一轮的完整事件编排（指令顺序即源码顺序）：

```text
 1  [仅组首] Wait  (MTE1→MTE2, mte2DBFlag)   ← 等 TEXTRACT 用完将被覆写的 L1 槽
 2  [仅组首] TLOAD aMatTile[mte2DBFlag]      ← 异步发出，MTE2 队列
 3  [仅组首] Set   (MTE2→MTE1, 0)            ← A panel 搬完挂牌
 4  [仅组首] TLOAD bMatTile[mte2DBFlag]
 5  [仅组首] Set   (MTE2→MTE1, 1)            ← B panel 搬完挂牌
 6  Wait  (M→MTE1, mte1DBFlag)               ← 等 TMATMUL 用完将被覆写的 L0 槽
 7  [仅组首] Wait  (MTE2→MTE1, 0)            ← 等 A 的 TLOAD 完成
 8  TEXTRACT aTile[mte1DBFlag] ← aMatTile[currMte2Idx]     (MTE1)
 9  [仅组首] Wait  (MTE2→MTE1, 1)            ← 等 B 的 TLOAD 完成
10  TEXTRACT bTile[mte1DBFlag] ← bMatTile[currMte2Idx]     (MTE1)
11  [仅组末] Set   (MTE1→MTE2, currMte2Idx)  ← 释放 L1 槽
12  Set   (MTE1→M, mte1DBFlag)               ← 切片完成（进入 MTE1 队列尾部）
13  Wait  (MTE1→M, mte1DBFlag)               ← 计算前等切片（进入 M 队列）
14  TMATMUL / TMATMUL_ACC                     (M)
15  Set   (M→MTE1, mte1DBFlag)               ← 计算完成，下一轮可覆写该 L0 槽
```

五个事件对各自守护一条依赖：

| 事件对 | 方向 | 守护的依赖 |
| --- | --- | --- |
| (MTE1→MTE2, 槽号) | TEXTRACT 完成 → 允许 TLOAD 覆写 | 防 L1 槽**写覆盖未读完的数据** |
| (MTE2→MTE1, 0/1) | TLOAD 完成 → 允许 TEXTRACT 读 | 防 L1 槽**读未就绪的数据**（A、B 各一个编号） |
| (M→MTE1, 槽号) | TMATMUL 完成 → 允许 TEXTRACT 覆写 | 防 L0 槽写覆盖 |
| (MTE1→M, 槽号) | 切片完成 → 允许 TMATMUL 读 | 防 L0 槽读未就绪 |
| (M↔FIX, 0) | 写回握手 | StoreResult 内 L0C → GM |

写回路径在 `StoreResult`：[gemm_performance_kernel.cpp:136-152](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L136-L152) 先 `Set/Wait (M→FIX, 0)` 确保 cTile 的累加全部完成，再 `TSTORE`，最后 `Set/Wait (FIX→M, 0)` 确保 FIX 写回完成——因为该 cTile 会被下一个 (i,j) 块的 `TMATMUL(cTile, ...)` 重新覆写，必须等写回真的结束。

#### 4.3.3 源码精读

逐段精读 [gemm_performance_kernel.cpp:88-131](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L88-L131) 的 `ProcessKIteration`：

- **TLOAD 段**（[L88-104](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L88-L104)）：`kModstepKa = kIter % stepKa`，仅组首执行。先 `WaitFlag<PIPE_MTE1, PIPE_MTE2>(mte2DBFlag)`——注意它**在 TLOAD 之前**：必须等两个 stepK 组之前的 TEXTRACT 把这个 L1 槽消费完。两个 GlobalTensor 视图 `gmA/gmB` 以 `kIter * baseK` 平移到本组起点，一次各搬 `[baseM, baseK*stepKa]` 与 `[baseK*stepKb, baseN]` 的大 panel。A、B 搬完分别 `SetFlag<PIPE_MTE2, PIPE_MTE1>(0/1)`——两个编号分开挂牌，使 B 的切片不必等 A 的 TLOAD。
- **TEXTRACT 段**（[L106-122](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L106-L122)）：`WaitFlag<PIPE_M, PIPE_MTE1>(mte1DBFlag)` 在覆写 L0 槽前等上一轮同槽的 TMATMUL 完成；随后（组首轮）分别等 A、B 的 TLOAD 事件，再做两次 `TEXTRACT`——从 L1 大 panel 中按 `(kIter % stepKa) * baseK` 偏移切出本轮的 64 列/行。组末 `SetFlag<PIPE_MTE1, PIPE_MTE2>(currMte2Idx)` 归还 L1 槽。
- **TMATMUL 段**（[L124-130](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L124-L130)）：`MatmulAcc`（[L27-35](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L27-L35)）在 `kIter==0` 用 `TMATMUL`（首轮清零），其余轮用 `TMATMUL_ACC` 累加——K 维 96 轮的累加语义靠它维系。计算后 `SetFlag<PIPE_M, PIPE_MTE1>` 释放 L0 槽、翻转 `mte1DBFlag`。

首尾补同步是本 kernel 最值得学习的工程细节：

- [gemm_performance_kernel.cpp:154-160](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L154-L160) `InitSyncFlags`：循环开始前把 (MTE1→MTE2, 0/1) 与 (M→MTE1, 0/1) 四个事件**预先挂牌**。原因：主循环首轮的 `WaitFlag` 等的事件还没有生产者（没有"上一组 TLOAD"、没有"上一轮 TMATMUL"），不预置就会在真机上永远等待。注释 [L217-218](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L217-L218) 称之为 "supplement first sync instr"。
- [gemm_performance_kernel.cpp:162-168](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L162-L168) `WaitSyncFlags`：循环结束后把末轮 `Set` 出的四个事件**补等待**，否则事件未配对（泄漏），既违反"记录一次等待一次"的纪律，也会在 CPU/CostModel 后端触发断言（见 u2-l3）。

基线 [gemm_basic_custom.cpp:118-129](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L118-L129) 有完全同构的首尾处理——四个 `set_flag` 预置在循环前、四个 `wait_flag` 收尾在循环后。**这个模式是通用的**：凡"循环体内先 wait 后 set"的写法，首尾都必须补齐。

CPU 仿真行为必须牢记：[cpu_stub.hpp:118-119](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/cpu_stub.hpp#L118-L119) 中

```cpp
inline void set_flag(pipe_t, pipe_t, int) {}
inline void wait_flag(pipe_t, pipe_t, int) {}
```

两个函数体为空。所以 CPU 仿真下上面 15 条指令按程序顺序逐条执行，事件全部"瞬间通过"——**功能正确性可以验证，事件配对错误在 CPU 下不可见**（漏写 wait 不会死等、漏写 set 不会卡住）。这就是本讲反复强调"CPU 验功能、真机验时序"的原因。

#### 4.3.4 代码实践

**实践目标**：把 `ProcessKIteration` 的事件序列抄写成表格，并论证 `InitSyncFlags` 的必要性——这是"读懂别人流水线"的基本功。

**操作步骤**：

1. 打开 [gemm_performance_kernel.cpp:88-131](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L88-L131)，按 4.3.2 的 15 行格式抄写 `kIter=0`（组首+组末同轮）这一轮实际执行的指令，删去 `[仅组首]`/`[仅组末]` 标注后应为 13 条。
2. 做删除实验（纸面推演，不改源码）：假设删掉 [L154-160](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L154-L160) 的 `InitSyncFlags`，追踪 `kIter=0` 时第一条 `WaitFlag<PIPE_MTE1, PIPE_MTE2>(0)` 会发生什么。
3. 对照基线 [gemm_basic_custom.cpp:51](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L51) 首轮的 `wait_flag(PIPE_MTE1, PIPE_MTE2, (event_t)cur)`，确认它依赖的也是循环前的 `set_flag`（[L118-119](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L118-L119)）。

**需要观察的现象**：首轮的每个 wait 都能对应到"循环前的预置 set"或"本轮更早的 set"；末轮的每个 set 都能对应到"循环后的补 wait"。

**预期结果**：删除 `InitSyncFlags` 后，`kIter=0` 的 `WaitFlag<PIPE_MTE1, PIPE_MTE2>(0)` 没有任何生产者，真机上 MTE2 队列会永久阻塞（死等）；而 CPU 仿真下 `wait_flag` 是空桩，程序照常跑通且结果正确——**恰好说明 CPU 仿真无法暴露这类错误**。此推演为源码分析结论，真机行为待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 A、B 两个 TLOAD 用事件编号 0/1 分开挂牌，而不是共用一个编号？

**答案**：分开挂牌后，TEXTRACT(A) 只需等 (MTE2→MTE1, 0)，不必等 B 的 TLOAD 也完成——两条搬运流水可以错峰供给两个操作数，减少无谓等待。若共用编号，先完成的一方也要陪着等另一方，制造人为气泡。

**练习 2**：`StoreResult` 里 `Set/Wait (FIX→M, 0)`（[gemm_performance_kernel.cpp:150-151](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/gemm_performance_kernel.cpp#L150-L151)）守护的是什么依赖？删掉它安全吗？

**答案**：守护"FIX 写回 cTile 完成 → M 才能开始下一个 (i,j) 块对 cTile 的 TMATMUL 覆写"。删掉后，下一个块的 `TMATMUL(cTile, ...)`（首轮形式会清零 cTile）可能与仍在 FIX 队列中的 TSTORE 竞争同一块 L0C，产生写回数据被覆写或读到半新半旧数据的竞态。CPU 仿真下单线程顺序执行不会暴露，真机上则是数据损坏。

**练习 3**：如何利用 README 的利用率表判断气泡在哪一段？

**答案**：看各段 Ratio 的相对大小（[README.md:81-98](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L81-L98)）：`m=7680` 时 TLOAD Ratio 98.4% 而 TMATMUL Ratio 降到 80.6%，说明 Cube 在等数据——搬运段是瓶颈（memory-feed limited），优化的方向是减少每 FLOP 的搬运字节（加大 stepK/baseN、提高复用），而不是优化 TMATMUL 本身；反之若 TMATMUL 接近 100% 而 TLOAD 低，则是 CUBE Bound，应加大计算量占比。

## 5. 综合实践

**任务**：把 CPU 仿真的 gemm 基线（`demos/cpu/gemm_demo`）改造成"K 分块 + 两级流水（load 与 compute 重叠一轮）"的版本，并对比改造前后的指令序列。这正是本讲规格中代码实践任务的展开。

**为什么选它做基线**：`gemm_demo` 目前是全序串行（4.1.3），改造空间干净；它自带 naive golden 比对与 `perf:` 输出，功能验证零成本；编译只需 `__CPU_SIM __PTO_AUTO__` 两个宏和 `include/` 一个头文件路径（[CMakeLists.txt:26-29](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/CMakeLists.txt#L26-L29)）。

**步骤一：搭一个独立的实验目录**（不要改动仓库源码）：

```bash
mkdir -p /tmp/gemm_pipe_practice && cd /tmp/gemm_pipe_practice
cp <仓库路径>/demos/cpu/gemm_demo/gemm_demo.cpp ./gemm_pipe.cpp
```

**步骤二：写出"串行分块版"**（示例代码，供对照的第一版）。把 `kK=16` 拆成两段 `baseK=8`，循环体内"搬一块、算一块"，不加任何事件：

```cpp
// 示例代码：串行分块（每轮全序，无重叠）
constexpr int kBaseK = 8;
constexpr int kKLoop = kK / kBaseK;
// aMatSlot/bMatSlot 类型改为 Tile<TileType::Mat, float, kM, kBaseK, ...>
for (int kIter = 0; kIter < kKLoop; ++kIter) {
    GlobalA gmASlice(A.data() + kIter * kBaseK);          // 视图平移，O(1)（见 u2-l1）
    GlobalB gmBSlice(B.data() + kIter * kBaseK * kN);
    TLOAD(aMatSlot, gmASlice);
    TLOAD(bMatSlot, gmBSlice);
    TMOV(aTile, aMatSlot);
    TMOV(bTile, bMatSlot);
    if (kIter == 0) { TMATMUL(cTile, aTile, bTile); }
    else            { TMATMUL_ACC(cTile, cTile, aTile, bTile); }
}
TSTORE(cGlobal, cTile);
```

**步骤三：升级为"两级流水版"**（示例代码）。双份 L1 槽 + 双份 L0 槽，按 4.2/4.3 的模式插入事件与槽位翻转：

```cpp
// 示例代码：两级流水（load 与 compute 重叠一轮）
TileMatA aMatSlot[2];  TileMatB bMatSlot[2];
LeftTile aSlot[2];     RightTile bSlot[2];
uint8_t dbFlag = 0;

// 首部补同步：预置首轮要等的事件（对应 InitSyncFlags）
set_flag(PIPE_MTE1, PIPE_MTE2, EVENT_ID0);
set_flag(PIPE_MTE1, PIPE_MTE2, EVENT_ID1);
set_flag(PIPE_M,    PIPE_MTE1, EVENT_ID0);
set_flag(PIPE_M,    PIPE_MTE1, EVENT_ID1);

for (int kIter = 0; kIter < kKLoop; ++kIter) {
    // TLOAD 段：为第 kIter 轮填 dbFlag 槽（在上一轮计算仍在途时发出）
    wait_flag(PIPE_MTE1, PIPE_MTE2, (event_t)dbFlag);     // 等该槽被读干净
    TLOAD(aMatSlot[dbFlag], gmASliceOf(kIter));
    set_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID0);
    TLOAD(bMatSlot[dbFlag], gmBSliceOf(kIter));
    set_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID1);
    const uint8_t curr = (dbFlag == 0) ? 1 : 0;           // 刚填好的数据槽
    dbFlag = curr;

    // MTE1 段：切片/搬运到 L0
    wait_flag(PIPE_M, PIPE_MTE1, (event_t)dbFlag);        // 等上一轮同槽算完
    wait_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID0);
    TMOV(aSlot[dbFlag], aMatSlot[curr]);
    wait_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID1);
    TMOV(bSlot[dbFlag], bMatSlot[curr]);
    set_flag(PIPE_MTE1, PIPE_MTE2, (event_t)curr);        // 归还 L1 槽

    // M 段：计算
    set_flag(PIPE_MTE1, PIPE_M, (event_t)dbFlag);
    wait_flag(PIPE_MTE1, PIPE_M, (event_t)dbFlag);
    if (kIter == 0) { TMATMUL(cTile, aSlot[dbFlag], bSlot[dbFlag]); }
    else            { TMATMUL_ACC(cTile, cTile, aSlot[dbFlag], bSlot[dbFlag]); }
    set_flag(PIPE_M, PIPE_MTE1, (event_t)dbFlag);
    dbFlag = (dbFlag == 0) ? 1 : 0;
}
// 尾部补同步：消化末轮 set 出的事件（对应 WaitSyncFlags）
wait_flag(PIPE_M,    PIPE_MTE1, EVENT_ID0);
wait_flag(PIPE_M,    PIPE_MTE1, EVENT_ID1);
wait_flag(PIPE_MTE1, PIPE_MTE2, EVENT_ID0);
wait_flag(PIPE_MTE1, PIPE_MTE2, EVENT_ID1);
```

注意 `__PTO_AUTO__` 下 TASSIGN/TSYNC 均为空操作（见 u3-l2），CPU 仿真下事件也是空桩，因此这段代码在 CPU 上**功能上等价于串行版**——这正符合预期。

**步骤四：编译运行**（g++ ≥ 13 或 clang++ ≥ 15）：

```bash
g++ -std=c++20 -O2 -D__CPU_SIM -D__PTO_AUTO__ \
    -I<仓库路径>/include gemm_pipe.cpp -o gemm_pipe
./gemm_pipe
```

**步骤五：对比指令序列变化**（本实践的核心交付物）。写出两版每轮 kIter 的指令时间线：

```text
串行分块版（每轮 6 条，全序）:
  MTE2: │LOAD A│LOAD B│          │          │LOAD A│LOAD B│
  MTE1:               │MOV A│MOV B│          │
  M  :                            │MM k0     │      │MM k1 ...

两级流水版（第 k 轮的 LOAD 与第 k-1 轮的 MM 在不同队列上同时存在）:
  MTE2: │LOAD A/B k0│LOAD A/B k1 │LOAD A/B k2 │
  MTE1:             │MOV k0      │MOV k1      │
  M  :                          │MM k0       │MM k1  │
```

**需要观察与预期结果**：

1. `./gemm_pipe` 输出 `max_abs_diff` 仍小于 `1e-3`（naive golden 比对沿用 [gemm_demo.cpp:128-129](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L128-L129) 的写法），退出码 0——分块累加（TMATMUL/TMATMUL_ACC 交替）没有破坏数值语义。具体数值**待本地验证**。
2. CPU 仿真下两版**运行时间不会有流水线收益**（单线程按序执行、事件空桩），`perf:` 行只反映计算本身——这是 CPU 仿真的固有边界，不是你的实现错误。
3. 真正的重叠收益要到真机或 CostModel 上才能看到（u10-l3 会讲如何用 `tests/run_costmodel.py` 估算周期）；真机上若首尾部同步写漏，会表现为死等或数据损坏，**待真机验证**。
4. 代码结构上，流水版比串行版多出的全部内容恰好是：双份缓冲声明、槽位翻转、5 对事件、首尾补同步——这就是"流水线并行"在代码层面的完整成本清单。

## 6. 本讲小结

- PTO kernel 的软件流水线通常为"TLOAD(MTE2) → 片上搬移(MTE1) → 计算(M/V) → TSTORE(FIX)"四段；程序顺序 ≠ 完成顺序，跨流水线依赖必须显式用事件表达。
- double buffer 的三要素：双份等大缓冲、0/1 翻转的槽位变量、按槽配对的双向事件（防写覆盖 + 防读未就绪）；`gemm_performance` 在 L1（每 4 轮翻转、配合 stepK 批量搬运）与 L0（每轮翻转）两级独立实施。
- 缓冲容量反过来约束分块形状：L0A/L0B 每个乒乓半区 32 KiB，base block \([128,256,64]\) 恰好把 L0B 填满。
- `set` 与 `wait` 程序文本相邻并非冗余——它们分别进入源/目标两条硬件队列，是跨队列传递顺序的唯一通道。
- "循环内先 wait 后 set"的写法必须在循环前预置事件（`InitSyncFlags`）、循环后补等待（`WaitSyncFlags`），否则真机首轮死等、末轮事件泄漏。
- CPU 仿真把 `set_flag`/`wait_flag` 做成空桩、单线程按序执行：功能可验、时序与同步错误不可见；瓶颈判读要靠利用率表（TLOAD Ratio 近 100% 即 memory-feed limited）。

## 7. 下一步学习建议

- **u6-l3 性能分析与优化方法论**：把本讲的利用率表判读推广为系统的 CUBE/MTE/Vector Bound 判定方法与 tile 参数扫描流程，并做 tile 形状扫描实验。
- 回头重读 [docs/coding/pipeline-parallel.md:114-124](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/pipeline-parallel.md#L114-L124) 的调优五步流程，对照你综合实践中"串行版 → 流水版"的改造路径，体会"从正确出发、逐步引入重叠、每步验证"的节奏。
- 若想看事件更密集的编排，可通读 [kernels/manual/a2a3/gemm_ar](https://github.com/gitcode.com/cann-pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_ar/README.md)（u7-l5 的计算通信融合算子）——它在四段流水之外再叠加通信队列。
- u10-l3 将讲解如何用 CostModel 在无硬件环境下估算流水线各段周期占比，验证你在本讲画出的时间线。
