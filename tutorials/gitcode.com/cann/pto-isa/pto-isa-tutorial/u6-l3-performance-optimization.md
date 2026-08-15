# 性能分析与优化方法论：瓶颈分类、tile 参数调优与内存优化

## 1. 本讲目标

学完本讲，你应该能够：

1. 用「分阶段流水」的视角拆解任意一个 PTO kernel，说出 TLOAD / 变换 / 计算 / TSTORE 四个阶段各自占用的时间比例意味着什么。
2. 掌握三种瓶颈（CUBE Bound / MTE·Memory Bound / Vector·变换 Bound）的判定方法：Ratio 阈值判读法、算术强度理论法、时间增量法。
3. 掌握三个正交的调优维度：tile 大小、tile 形状、指令排序（同步粒度）。
4. 了解内存优化的三板斧：数据复用、布局对齐、减少 GM 流量。
5. 完成一个 tile 形状扫描实验：至少三组形状，推导每组的片上占用与 GM 流量，并判定该 kernel 属于 CUBE Bound 还是 MTE Bound。

本讲是方法论总结讲：前三小节把 `docs/coding/` 下三份性能文档（`opt.md`、`performance-best-practices.md`、`memory-optimization.md`）与 `gemm_performance` 算子的真实测量数据串成一条完整的「判定 → 调优 → 验证」闭环。

## 2. 前置知识

### 2.1 复习：四级流水（来自 u5-l2 / u6-l2）

一个典型的 GEMM 类 PTO kernel 是四段流水：

```
TLOAD (MTE2: GM→L1) → TEXTRACT/TMOV (MTE1: L1→L0) → TMATMUL (M: L0→L0C) → TSTORE (FIXPIPE: L0C→GM)
```

硬件上这些流水线队列**并行执行**，程序书写顺序 ≠ 完成顺序；跨流水线的依赖必须用 `(srcPipe, dstPipe, eventId)` 事件显式表达，double buffer（乒乓缓冲）用来让「下一块的搬运」与「当前块的计算」重叠。这些概念在 u6-l2 已详细展开，本讲直接使用。

### 2.2 什么是 Bound（受限）

一个 kernel 的端到端时间由最慢的那条流水线决定，就像木桶由最短的板决定。我们把「时间主要耗在哪条流水线」称为该 kernel 的瓶颈：

| 瓶颈类型 | 又称 | 含义 |
| --- | --- | --- |
| CUBE Bound | Compute Bound | Cube 矩阵单元是短板，搬运喂得饱，计算来不及做 |
| MTE Bound | Memory / Memory-feed Bound | 搬运通路（MTE2/MTE3 或 MTE1）是短板，Cube 在等米下锅 |
| Vector Bound / 变换 Bound | Conversion Bound | 向量单元或片上布局变换（TEXTRACT/TMOV）是短板 |

判定瓶颈类型是优化的**第一步**：对着错误的瓶颈优化（比如 Cube 已经饿了还在压缩计算指令数）只会白费功夫。

### 2.3 为什么 CPU 仿真不能直接测性能

u1-l3 与 u3-l4 已经确立过：CPU 仿真后端只保证**功能正确**，不模拟硬件流水线、repeat 粒度与带宽。因此：

- CPU 仿真可以验证「tile 改了之后结果还对不对」；
- CPU 仿真下的运行时间**不能**当作 NPU 性能依据。

没有真机时，本讲给出的替代手段有两个：**理论计算**（算术强度法，见 4.1.2）和 **CostModel 性能模拟**（`tests/run_costmodel.py`，其内部原理在 u10-l3 展开）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/coding/opt.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md) | 优化总纲：分阶段性能模型、可重复的调优工作流、按 Ratio 判读瓶颈 |
| [docs/coding/performance-best-practices.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md) | 实践手册：六步优化流程、瓶颈阈值定义、各瓶颈对应的解法清单、平台参数 |
| [docs/coding/memory-optimization.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md) | 内存专讲：片上占用估算、数据复用、布局对齐、GM 流量削减、double buffer 模板 |
| kernels/manual/a2a3/gemm_performance/README.md | 真实案例：A3 实测的各阶段 Ratio 表与逐条调优指南 |
| demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp | 实践对象：可修改 tile 常量的流水线 GEMM 基线 |
| demos/cpu/gemm_demo/gemm_demo.cpp | CPU 侧唯一的计时样例：输出 `perf:` 行与 GFLOPS |
| tests/run_costmodel.py | 无真机时的性能模拟入口 |

## 4. 核心概念与源码讲解

### 4.1 瓶颈分类：CUBE/MTE/Vector Bound 的判定

#### 4.1.1 概念说明

瓶颈判定要回答一个问题：**时间都去哪了？**

仓库给出的标准答案是看各阶段的「占比」（Ratio）——即某条流水线忙碌时间占端到端时间的比例。`opt.md` 把大部分 kernel 抽象成四个阶段，并给出三句最有用的判读口诀：

> - TLOAD 接近 100% → 流水线是「喂不饱」型（feed-limited），要减少流量或改善复用/重叠；
> - 变换（TEXTRACT/TMOV）占主导 → 要降低每个 FLOP 的布局工作量，或用更多计算摊薄每次变换；
> - TMATMUL 低而 TLOAD 高 → Cube 在挨饿，重叠已断裂或带宽已饱和。

见 [docs/coding/opt.md:L15-L36](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md#L15-L36)：这段先定义了 TLOAD → 布局变换 → Cube/Vector 计算 → TSTORE 的四阶段模型（L19-L24），随后给出上述三条 Ratio 判读提示（L32-L36）。

`performance-best-practices.md` 把同样的思想量化成了阈值表：

| 瓶颈类型 | 判定阈值（启发式） |
| --- | --- |
| Memory Bound | TLOAD/TSTORE 占比 > 60% |
| Compute Bound | TMATMUL 占比 > 70% |
| Conversion Bound | TEXTRACT/TMOV 占比 > 20% |

见 [docs/coding/performance-best-practices.md:L44-L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L44-L57)（Step 3 给出一个示例分布：TLOAD 45% / TEXTRACT 10% / TMATMUL 40% / TSTORE 5%，以及三条 Bottleneck Types 阈值）。

配套的健康指标表在 [docs/coding/performance-best-practices.md:L93-L100](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L93-L100)：TMATMUL 占比目标 > 50%、TLOAD < 40%、MTE 带宽利用 > 70%、流水线气泡 < 10%。注意文档开头明确声明：所有数字是分析启发值而非硬件保证值（[L1-L3](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L1-L3)）。

#### 4.1.2 核心流程

判定瓶颈有三条互补的证据链：

**证据链一：Ratio 判读法（有 profiler 数据时）**

用 msprof 采集各阶段时间占比，按 4.1.1 的阈值表归类：

```
msprof --application="./your_app" --output=./profiling_data --ai-core=on --task-time=on
```

**证据链二：算术强度理论法（只有纸和笔时）**

这是无真机时最重要的方法。定义**算术强度**（Arithmetic Intensity）为每搬运 1 字节 GM 数据对应的计算量：

\[
AI = \frac{\text{总 FLOP}}{\text{总 GM 搬运字节}}
\]

再定义「喂饱 Cube 所需的带宽」：

\[
BW_{\text{need}} = \frac{P_{\text{cube}}}{AI}
\]

其中 \(P_{\text{cube}}\) 是 Cube 峰值算力。判定规则：

- 若 \(BW_{\text{need}}\) 明显大于硬件可用 GM 带宽 \(BW_{\text{avail}}\) → **MTE/Memory Bound**（搬运喂不饱计算）；
- 若 \(BW_{\text{need}}\) 明显小于 \(BW_{\text{avail}}\) → **CUBE Bound**（计算是短板，加大 tile 提高算力利用率才有意义）。

这正对应文档 [docs/coding/performance-best-practices.md:L121-L131](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L121-L131) 的推理模式：理论上限 = 峰值 × 估算利用率，实测吞吐 = 工作量 FLOP / 实测时间，两相比较得出 compute-bound 还是 memory-bound；文档同样警告不要把平台数字硬编码进设计结论。

对 GEMM 这类算子，算术强度有一个著名的理论趋势：输出块越大，复用越高。对边长为 \(b\) 的立方体分块（\(b \times b\) 的输出块、\(b\) 长的 K 步），每个输出块需要搬运约 \(2b^2\) 个输入元素、产生 \(b^2\) 个输出、做 \(2b^3\) FLOP，因此 \(AI \propto b\)。**加大 tile 是提高算术强度、逃离 Memory Bound 的第一手段**——这解释了为什么 4.2 节的 tile 维度永远排在调优第一位。

**证据链三：时间增量法（什么 profiler 都没有时）**

在关键路径上插计时（文档示例见 [docs/coding/performance-best-practices.md:L104-L119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L104-L119)），对比 load / compute / store 各段耗时。注意这只在真机上有效——CPU 仿真下插计时得到的是宿主机 C++ 循环的时间，没有意义。

三条证据链的产出汇入同一个六步循环：

```
正确性验证 → 性能基线 → 瓶颈分析 → 定向优化 → 验证效果 → 迭代
```

见 [docs/coding/performance-best-practices.md:L11-L72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L11-L72)。注意第一步永远是正确性（CPU 先跑过、数值误差达标），第二步才是建基线——**不要在没有基线的情况下开始优化**。

#### 4.1.3 源码精读：gemm_performance 的实测 Ratio 表

瓶颈判定最生动的教材是 `kernels/manual/a2a3/gemm_performance/README.md` 里 A3（24 核）上的实测表：

| 形状 | TMATMUL (Cube) Ratio | TEXTRACT Ratio | TLOAD Ratio | TSTORE Ratio | 时间 (ms) |
| --- | --- | --- | --- | --- | --- |
| 1536³ | 54.5% | 42.2% | 72.2% | 7.7% | 0.0388 |
| 3072³ | 79.0% | 62.0% | 90.9% | 5.8% | 0.2067 |
| 6144³ | 86.7% | 68.1% | 95.2% | 3.1% | 1.5060 |
| 7680³ | 80.6% | 63.0% | 98.4% | 2.4% | 3.1680 |

来源：[kernels/manual/a2a3/gemm_performance/README.md:L77-L98](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L77-L98)，这段代码给出了表格及逐行解读。

用 4.1.2 的算术强度法交叉验证 6144³ 这一行：24 核按 4×6 切分，每核负责 `1536×6144×1024` 的输出块，读入 `1536×6144`（A panel）与 `6144×1024`（B panel）个 fp16 元素（2 字节），写出 fp32：

\[
FLOP_{\text{每核}} = 2 \times 1536 \times 6144 \times 1024 \approx 1.93 \times 10^{10}
\]

\[
Bytes_{\text{每核}} \approx (1536 \times 6144 + 6144 \times 1024) \times 2 + 1536 \times 1024 \times 4 \approx 3.21 \times 10^{7}
\]

\[
AI \approx 1.93 \times 10^{10} / 3.21 \times 10^{7} \approx 600 \ \text{FLOP/字节}
\]

按文档 5.1 节的启发值「A2/A3 Cube 峰值约 50 TFLOPS/核」（[docs/coding/performance-best-practices.md:L339-L360](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L339-L360)），喂饱单个 Cube 核需要 \(50 \times 10^{12} / 600 \approx 83\) GB/s，24 核共约 2 TB/s——恰好落在现代 HBM 带宽的量级附近。**理论与实测互相印证**：TLOAD Ratio 95.2% 说明 GM 供数通路已接近打满，kernel 处在「MTE 接近饱和、Cube 刚好喂饱」的临界区；一旦形状再增大（7680³），TLOAD 升到 98.4% 而 TMATMUL 反而掉到 80.6%——搬运开始拖累计算，方向从 CUBE Bound 滑向 MTE Bound。README 的总结原话是：当 TLOAD Ratio 接近 100% 时，你就是 memory-feed limited，后续提速来自「减少每 FLOP 的搬运字节 + 改善重叠」，而不是微调 TMATMUL。

反过来对照 TEXTRACT 那一列：42%~68% 意味着 L1→L0 的切片/布局变换是不可忽略的第三股力量，按 4.1.1 的阈值（> 20% 即 Conversion Bound）它始终超标——这也是 u5-l3 强调过的结论：TEXTRACT 占比高时要「提高每次变换摊到的计算量」，而不是硬抠 TMATMUL。

#### 4.1.4 代码实践：Ratio 判读练习

1. **实践目标**：不看结论，仅凭 Ratio 表训练判定手感。
2. **操作步骤**：
   - 打开 [kernels/manual/a2a3/gemm_performance/README.md:L81-L86](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L81-L86)；
   - 对 1536³ 与 7680³ 两行分别回答：(a) 按 4.1.1 阈值表应归类为什么 Bound？(b) 如果只允许改一个参数，你先改哪个？
3. **需要观察的现象**：1536³ 行 TMATMUL 只有 54.5% 且 TLOAD 72.2%——小形状下流水线没跑稳（warm-up/drain 占比高）；7680³ 行 TLOAD 98.4%——搬运已饱和。
4. **预期结果**：1536³ 判为「重叠不足 + 搬运偏紧」，先调 stepK/重叠；7680³ 判为 MTE Bound，先减流量（加大复用）。与 README 第 6 节 [L189-L194](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L189-L194) 的官方建议一致。
5. 结论可直接从 README 文字核对，无需本地运行；若想复测数据则需 A3 真机（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：某 kernel 的 profiler 输出为 TLOAD 65%、TMATMUL 25%、TEXTRACT 8%、TSTORE 2%，它是什么 Bound？应从哪个方向优化？

> 答案：TLOAD/TSTORE 合计 67% > 60%，Memory Bound。方向是减少 GM 流量：加大 tile 提高复用（见 4.3.1 的 K 维分块）、避免重复搬运、必要时用 TPREFETCH / double buffer 改善重叠。

**练习 2**：算术强度 \(AI = 400\) FLOP/字节，Cube 峰值 50 TFLOPS/核，硬件可用 GM 带宽约 100 GB/s/核。该 kernel 偏向哪种 Bound？

> 答案：\(BW_{\text{need}} = 50 \times 10^{12} / 400 = 125\) GB/s > \(BW_{\text{avail}} = 100\) GB/s，搬运喂不饱计算，偏 MTE Bound。应继续加大输出块（\(AI \propto b\)）或减少写回流量。

**练习 3**：为什么「TLOAD Ratio 95% 但 TMATMUL Ratio 也有 86%」并不矛盾？

> 答案：Ratio 是各流水线忙碌时间占端到端时间的比例，多条流水线并行时可以同时都很高。两者都高说明流水线已充分重叠、处于平衡临界区；此时优化收益最小、风险最大，任何减流量的改动都可能把平衡推向 Cube 空闲。

### 4.2 调优维度：tile 大小、tile 形状与指令排序

#### 4.2.1 概念说明

判定瓶颈后，接下来是「拧旋钮」。PTO 把性能旋钮明确留给开发者（u1-l1 讲过：抬高抽象但不隐藏底层），其中最有效的是三个**正交**维度：

1. **tile 大小**（baseM/baseK/baseN、singleCoreM/N）：决定片上占用、数据复用率与算术强度；
2. **tile 形状**（同样的字节数下 M/K/N 的配比，以及布局）：决定是否装得下、是否对齐、Cube 是否高效；
3. **指令排序**（事件同步的粒度与位置）：决定流水线重叠程度、气泡大小。

`opt.md` 把 tile 称为「性能的一阶旋钮」，因为它同时决定三件事：片上占用是否溢出、加载数据被复用多少次、各阶段能否重叠。见 [docs/coding/opt.md:L74-L91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md#L74-L91)（含检查清单：tile 不超片上容量、形状对齐目标引擎、尽量提高算术强度）。

#### 4.2.2 核心流程

**维度一：tile 大小 —— 先过容量预算这一关**

任何 tile 参数首先要通过「片上容量」的硬约束。估算方法见 [docs/coding/memory-optimization.md:L29-L42](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L29-L42)：double buffer 下 staging 需要 ×2，累加器常驻，三者相加不得超过目标存储上限。文档的例子：`TM=128, TK=64, TN=256` 时 staging（双缓冲）96 KB + 累加器 128 KB = 224 KB，必须装得下。

除了 L1，L0A/L0B 还有更紧的预算。`gemm_performance` 的经验规则（[README:L125-L143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L125-L143)）：L0A/L0B 显式按 32 KiB ping/pang 半区切分，因此**每个缓冲 tile 的占用必须 ≤ 32 KiB**。fp16（2 字节）下：

\[
\text{L0A bytes} \approx baseM \times baseK \times 2, \qquad \text{L0B bytes} \approx baseK \times baseN \times 2
\]

参考选型 `baseM=128, baseK=64` → 16 KiB（宽松）；`baseK=64, baseN=256` → 32 KiB（顶满预算）。指南是「吃满但不超」，且 baseK 对齐 Cube 偏好的 K 粒度（32/64/128）。

各档位 tile 的推荐形状也有平台差异（A2/A3 推荐 Left 128×64 / Right 64×256 / Acc 128×256；A5 片上更大可翻倍），见 [docs/coding/performance-best-practices.md:L209-L216](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L209-L216) 与 4.1 节的多级分块说明（[L229-L238](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L229-L238)）。

**维度二：tile 形状 —— 对齐与整除**

形状不是自由的，两层约束：

- 对齐约束由 `Tile` 的 `static_assert` 编译期强制：行主序 tile 要求 `Cols × sizeof(T)` 是 32 字节倍数，列主序要求 `Rows × sizeof(T)` 是 32 字节倍数，boxed tile（TileLeft/TileRight/TileAcc）形状须匹配分形基础块（fractalABSize=512 / fractalCSize=1024），见 [docs/coding/memory-optimization.md:L44-L57](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L44-L57)；
- 整除约束来自循环结构：`singleCoreM % baseM == 0`、`singleCoreK % baseK == 0`、`singleCoreN % baseN == 0`（[gemm_performance README:L118-L122](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L118-L122)）。若 baseM/baseN 小于 singleCoreM/N，就必须引入 mLoop/nLoop 嵌套——而这会改变 GM 流量分布（见 4.3.4 的推导）。

**维度三：指令排序 —— 只等真依赖**

同步粒度直接决定流水线气泡。规则有两条：

- 只在**真实生产-消费依赖**处 wait，避免稳态循环里「全部排空」式等待；
- 把循环看作 warm-up / steady / drain 三段，优先调稳态，首尾用补同步兜底。

文档对照示例见 [docs/coding/performance-best-practices.md:L301-L333](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L301-L333)：好写法是细粒度 `Event<Op::TLOAD, Op::TADD>` 只等必要的生产者，坏写法是循环体内 `TSYNC()` 全局排空；`opt.md` 的对应规则在 [L103-L118](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md#L103-L118)。

三个维度的完整调优工作流（一次只动一个旋钮、固定问题形状、把结果记进 README 表格以便复盘）见 [docs/coding/opt.md:L38-L56](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md#L38-L56)。

#### 4.2.3 源码精读

以 `demos/baseline/gemm_basic` 的 kernel 为例，看三个维度分别落在哪些代码位置：

```cpp
constexpr uint32_t M = 512;            // 问题形状
constexpr uint32_t K = 2048;
constexpr uint32_t N = 1536;
constexpr uint32_t singleCoreM = 128;  // 核切分（维度一：tile 大小·核级）
constexpr uint32_t singleCoreK = 2048;
constexpr uint32_t singleCoreN = 256;
constexpr uint32_t baseM = 128;        // 基础块（维度一：tile 大小·块级 + 维度二：形状）
constexpr uint32_t baseK = 64;
constexpr uint32_t baseN = 256;
```

出处：[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L137-L150](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L137-L150)。这十几行常量就是 tile 维度的全部旋钮：改动它们即完成一次「tile 大小/形状扫描」，其余代码通过模板参数 `baseM, baseK, baseN` 自动适配（模板声明见 [L31-L37](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L31-L37)）。

tile 大小同时决定片上摆放地址。L1 中两对乒乓 Mat tile 的地址是手工排布的：

```cpp
TASSIGN(aMatTile[0], 0x0);
TASSIGN(aMatTile[1], 0x0 + baseM * baseK * sizeof(U));          // 紧跟着 [0]
TASSIGN(bMatTile[0], 0x0 + baseM * baseK * 2 * sizeof(U));       // A 双缓冲之后
TASSIGN(bMatTile[1], 0x0 + baseM * baseK * 2 * sizeof(U) + baseK * baseN * sizeof(U));
```

出处：[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L96-L114](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L96-L114)。**注意**：这些偏移表达式里出现了 `baseM * baseK`、`baseK * baseN`——改 tile 大小时若忘了同步这些手工地址（Manual 模式第一责任，u3-l2 讲过 TASSIGN 不查重叠），缓冲就会互相踩踏。

维度三（指令排序）落在 K 迭代函数与首尾补同步：

```cpp
wait_flag(PIPE_MTE1, PIPE_MTE2, (event_t)cur);   // 上一轮 MTE1 用完这个 L1 槽
TLOAD(aMatTile[cur], gmA);
set_flag(PIPE_MTE2, PIPE_MTE1, EVENT_ID0);       // 搬完 A，通知 MTE1
...
wait_flag(PIPE_M, PIPE_MTE1, (event_t)cur);      // 上一轮 Cube 用完这个 L0 槽
TMOV(aTile[cur], aMatTile[cur]);
```

出处：[demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp:L47-L72](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47-L72)（K 迭代内的按槽位双向事件），循环外的首尾补同步（预热挂牌 + 收尾等待）在 [L116-L129](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L116-L129)：进循环前先 set 四个 flag（否则真机首轮死等），出循环后 wait 兜底排空——这正是 warm-up/drain 的标准写法。

#### 4.2.4 代码实践：footprint 估算器

1. **实践目标**：为给定 `(baseM, baseK, baseN)` 写一个容量预算表，学会在写 kernel 之前先算账。
2. **操作步骤**：
   - 取 fp16 输入、fp32 累加；用 4.2.2 的公式计算三组候选的 L1 staging（双缓冲）、L0A/L0B 单缓冲占用、Acc 占用；
   - 把结果填进表格，对照「L0 半区 ≤ 32 KiB」与「L1 总量」预算判断可行性；
   - 示例代码（非项目原有，为「示例代码」）：

```cpp
// 示例代码：编译期容量预算，static_assert 让超预算的组合直接编译失败
template <int BM, int BK, int BN>
struct GemmFootprint {
    static constexpr size_t l0a = BM * BK * 2;            // fp16
    static constexpr size_t l0b = BK * BN * 2;
    static constexpr size_t acc = BM * BN * 4;            // fp32
    static constexpr size_t l1  = 2 * (l0a + l0b);        // Mat staging 双缓冲
    static_assert(l0a <= 32 * 1024, "L0A ping half > 32KiB");
    static_assert(l0b <= 32 * 1024, "L0B ping half > 32KiB");
};
```

3. **需要观察的现象**：`GemmFootprint<128,128,256>` 会在编译期报错（L0B = 64 KiB 超预算），而 `<128,64,256>`、`<128,128,128>`（L0A/L0B 均 32 KiB 顶满）通过。
4. **预期结果**：得到一张「候选形状 × 占用 × 是否可行」的表；这正是综合实践（第 5 节）第 2 步的输入。可本地用任意 C++20 编译器验证（**待本地验证**具体报错文本）。

#### 4.2.5 小练习与答案

**练习 1**：fp16 下 `(baseM, baseK, baseN) = (128, 128, 128)` 与 `(128, 64, 256)` 的 L0A+L0B 总占用相同（都是 96 KiB），哪个更好？

> 答案：不能只看总量。(128,64,256) 的输出块 128×256 更大，算术强度更高（\(AI \propto\) 输出块边长），且 baseN=256×2B=512B 写回对齐好；(128,128,128) 两个半区都顶满 32 KiB，无余量。除非 K 粒度有特殊要求，通常选 (128,64,256)——这也是 gemm_performance 的实际选型，理由见 [README:L125-L143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L125-L143)。

**练习 2**：把 gemm_basic 的 baseN 从 256 改成 128，除了改常量还需要做什么？

> 答案：`singleCoreN=256` 不再被 `baseN=128` 整除覆盖，必须引入 nLoop 内层循环（或把 singleCoreN 改成 128 并调整核分组），否则只算一半输出；同时 L1/L0 的 TASSIGN 手工地址都要按新的 `baseK*baseN` 重排。更重要的是 GM 流量结构会变（B panel 复用下降、A panel 可能重复搬），需要按 4.3.4 重新推导。

**练习 3**：为什么「循环体内 TSYNC()」是性能反模式？

> 答案：TSYNC 是全流水线排空，等于每轮迭代都把并行流水线拉回串行，稳态吞吐退化成 \(T_{load}+T_{compute}\) 而不是 \(\max(T_{load}, T_{compute})\)。正确做法是按槽位（ping/pong）配对细粒度事件，只在循环外做一次最终同步，见 [docs/coding/performance-best-practices.md:L318-L333](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L318-L333) 的正反例对照。

### 4.3 内存优化：减少每个 FLOP 搬运的字节

#### 4.3.1 概念说明

前两小节回答「瓶颈在哪、旋钮是什么」；本小节是 MTE Bound 时最核心的解法库：**让每个搬运进片上的字节参与更多计算**。内存优化的前提是理解片上存储模型——每种 TileType 对应一级存储，数据沿固定通路流动：

```
GM --TLOAD--> Mat(L1) --TMOV/TEXTRACT--> Left/Right(L0A/L0B) --TMATMUL--> Acc(L0C) --TSTORE--> GM
```

见 [docs/coding/memory-optimization.md:L7-L26](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L7-L26)（存储类别表 + 通路图；向量指令直接操作 Vec tile，Mat↔Vec 转换走 TMOV/TEXTRACT）。

#### 4.3.2 核心流程

内存优化手段可以归成三板斧：

**第一板斧：数据复用——加载一次，用多次**

- GEMM 的 K 维分块：累加器 `TileAcc` 全程常驻片上，K 循环里只搬 A/B 面板，最后一次 TSTORE。参考实现见 [docs/coding/memory-optimization.md:L60-L84](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L60-L84)（注释点明「每个 TM×TK 块只 1 次 GM 访问」「acc 常驻片上」）。
- Softmax 的行统计缓存：row_max/row_sum 算一次存在窄 Vec tile 里反复用，若存回 GM 再读回来会让统计量的流量大约翻三倍，见 [docs/coding/memory-optimization.md:L86-L101](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L86-L101)。
- 工程版复用就是 gemm_performance 的 **stepK caching**：一次 TLOAD 搬 stepK=4 个 K 微面板进 L1，再逐片 TEXTRACT，用更大的 DMA 摊薄启动开销、提高 burst 效率，见 [kernels/manual/a2a3/gemm_performance/README.md:L145-L158](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L145-L158)（指南：加大 stepK 直到 L1 容量或重叠撑不住为止）。

**第二板斧：布局与对齐——从一开始就选对格式**

布局错配的代价是额外一轮 TTRANS/TEXTRACT。原则是「让布局匹配消费指令」：GM 行主序就用 `BLayout::RowMajor` 装载；GEMM 输入在支持的目标上用 `GlobalTensor` 的 `Layout::NZ` 直接让 TLOAD 边搬边分形、省掉 TMOV 一整段；TTRANS 只在源/目标布局真的不同时使用。见 [docs/coding/memory-optimization.md:L105-L121](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L105-L121)。u3-l3 建立的成本排序在这里直接适用：改视图 < TRESHAPE < TMOV < TTRANS。

**第三板斧：削减 GM 流量**

- 算子融合：连续逐元素操作不落 GM，中间结果留在片上，把 4 次 GM 往返降到 2 次（正反例见 [docs/coding/memory-optimization.md:L127-L141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L127-L141)）；
- 连续访问：优先行主序遍历；不得不列访问时「整块搬入 + 片上 TTRANS」（[L143-L146](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L143-L146)）；
- TPREFETCH 非阻塞预取，让下一块搬运与当前计算重叠（[L148-L155](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L148-L155)）；
- double buffer 模板（[L159-L192](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L159-L192)）：稳态吞吐逼近 \(\max(T_{load}, T_{compute})\) 而非两者之和——这是 u6-l2 的主题，此处作为流量手段引用。

**尾块：用 valid region 而不是多套 tile**

维度不是 tile 整数倍时，用有效区（静态或 DYNAMIC 动态）表达实际范围，容量按对齐补齐，见 [docs/coding/memory-optimization.md:L196-L215](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L196-L215)。这避免了为每个尾块尺寸实例化一套 tile 类型。

全部手段收敛为一张检查清单（容量 / 对齐 / 复用 / 流量 / 同步五组），发布 kernel 前逐项打勾：[docs/coding/memory-optimization.md:L219-L243](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L219-L243)。

#### 4.3.3 源码精读

把三板斧映射回真实 kernel（gemm_basic，L31-L72 的 `ProcessKIteration`）：

- **复用**：`cTile`（TileAcc）在 [L66-L70](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L66-L70) 首轮 `TMATMUL` 初始化、后续轮 `TMATMUL_ACC` 累加——K 循环 32 轮期间累加器全程在 L0C，只有最后一次 TSTORE，正是「Acc 常驻」的实例化；
- **布局**：B 输入在 GM 里用 `Layout::DN` 视图（[L43-L45](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L43-L45)），配合 `SLayout::ColMajor` 的 bMatTile，让搬运路径天然贴合 Cube 的 Right 操作数摆放，省掉额外转置；
- **重叠**：`cur = kIter % 2`（[L47](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp#L47)）+ 按槽位事件，构成 double buffer。

`gemm_performance` 在此之上叠加 stepK=4 的 L1 caching 与三级乒乓（优化叙述见 [README:L50-L58](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/a2a3/gemm_performance/README.md#L50-L58)），这正是 4.1.3 中 TLOAD Ratio 能压在 95% 而非 100% 的原因之一。

#### 4.3.4 代码实践：算术强度计算器

这是综合实践（第 5 节）的理论核心，先单独练一遍。

1. **实践目标**：写出给定形状下「GM 流量与算术强度」的推导，弄清**哪些量由核切分决定、哪些由 base block 决定**。
2. **操作步骤**：
   - 对 gemm_basic（M=512, K=2048, N=1536，24 核 4×6 切分，singleCoreM=128 / singleCoreN=256），推导每核读入与写出：

\[
Bytes_{\text{读/核}} = (singleCoreM \times K + K \times singleCoreN) \times 2, \qquad Bytes_{\text{写/核}} = singleCoreM \times singleCoreN \times 4
\]

\[
FLOP_{\text{核}} = 2 \times singleCoreM \times K \times singleCoreN, \qquad AI = \frac{FLOP_{\text{核}}}{Bytes_{\text{读/核}} + Bytes_{\text{写/核}}}
\]

   - 代入数字：读 \((128 \times 2048 + 2048 \times 256) \times 2 = 1.5\ \text{MiB}\)，写 \(128 \times 256 \times 4 = 128\ \text{KiB}\)，\(FLOP_{\text{核}} = 2 \times 128 \times 2048 \times 256 \approx 1.34 \times 10^8\)，得 \(AI \approx 79\) FLOP/字节；
   - 再验证一个关键不变量：**在 `baseM=singleCoreM`、`baseN=singleCoreN` 的前提下，扫描 baseK 不改变 GM 流量**——因为 `kLoop × baseM × baseK = singleCoreM × K`、`kLoop × baseK × baseN = K × singleCoreN`，与 baseK 无关。baseK 改变的是 DMA 条数、burst 大小与 MTE1（L1→L0）的片上流量，不是 GM 流量。
3. **需要观察的现象**：用三组 baseK（32/64/128）代入公式，AI 均约 79 不变；而 kLoop 分别为 64/32/16，L1 staging 占用分别为 80/96/128 KiB（4.2.4 的结果）。
4. **预期结果**：理解「Bound 判定是 kernel 级问题（由 AI 与硬件比值决定），tile 扫描改变的是搬运效率与片上占用」——这是第 5 节判定结论的理论依据。纯纸面推导，可本地用计算器/脚本复核。

#### 4.3.5 小练习与答案

**练习 1**：gemm_basic 的 \(AI \approx 79\)，gemm_performance 在 6144³ 的 \(AI \approx 600\)。为什么差这么多？

> 答案：AI 由输出块复用决定。gemm_performance 每核输出块 1536×1024，A/B 面板被更多输出元素复用；gemm_basic 每核只有 128×256。两者共同印证 \(AI \propto\) 输出块边长——加大 singleCore 块（前提是片上装得下、核数够切）是提升 AI 最直接的手段。

**练习 2**：一个 kernel 里 `TSTORE(gTmp, b); ... TLOAD(c, gTmp);` 相邻出现，有什么问题？

> 答案：中间结果落 GM 再读回，白白多两次 GM 往返。应融合为片上直通（b 留在 tile 里直接喂下一条指令），参考 [docs/coding/memory-optimization.md:L127-L141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L127-L141) 的未融合/融合对照。

**练习 3**：TPREFETCH 与 double buffer 都能实现「搬运与计算重叠」，区别是什么？

> 答案：double buffer 是**结构性的**——两份等大缓冲 + 按槽位事件，把重叠写进循环骨架，稳态吞吐逼近 \(\max(T_{load}, T_{compute})\)；TPREFETCH 是**提示性的**非阻塞预取，只影响 GM 侧取数时机，不提供同步保证，适合在已有结构上锦上添花。见 [docs/coding/memory-optimization.md:L148-L192](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/memory-optimization.md#L148-L192)。

## 5. 综合实践：tile 形状扫描与 Bound 判定

把本讲三个模块串成一个实验：对 u6-l2 完成的流水线 GEMM（若你保留了自己的 CPU 版本就用它；否则以 `demos/baseline/gemm_basic/csrc/kernel/gemm_basic_custom.cpp` 为纸面对象）做至少 3 组 tile 形状扫描，记录结果并判定该 kernel 是 CUBE Bound 还是 MTE Bound。

**第 1 步：选三组形状并通过约束检查**

固定 `baseM=128`、`baseN=256`（即保持 `baseM=singleCoreM`、`baseN=singleCoreN`，避免引入 mLoop/nLoop 改变流量结构），扫描 baseK：

| 组 | (baseM, baseK, baseN) | kLoop = singleCoreK/baseK | L0A / L0B 占用 | L1 staging（双缓冲） |
| --- | --- | --- | --- | --- |
| A | (128, 32, 256) | 64 | 8 KiB / 32 KiB | 80 KiB |
| B | (128, 64, 256) | 32（原始） | 16 KiB / 32 KiB | 96 KiB |
| C | (128, 128, 256) | 16 | 32 KiB / 32 KiB | 128 KiB |

约束核对：`2048 % baseK == 0` 三组均成立；L0 半区 ≤ 32 KiB 均成立（C 组顶满）；B tile 行主序 `256×2B=512B` 为 32 字节倍数成立；baseK ∈ {32,64,128} 对齐 Cube 的 K 粒度。

**第 2 步：功能验证（CPU 侧）**

若你维护着 u6-l2 的 CPU 流水线版本：修改常量后重新编译，跑通并确认与参考实现的最大误差达标（fp32 < 1e-5 或 fp16 < 1e-3，标准见 [docs/coding/performance-best-practices.md:L18-L31](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L18-L31)）。同时记录可观测的**结构性指标**：kLoop 次数、每轮 TLOAD/TMOV/TMATMUL 条数（可临时在 CPU 版本里加计数打印）。以 gemm_basic 为对象时，此步为纸面推导（**待本地验证**）。注意改 baseK 后 L1 的 TASSIGN 手工地址（`0x0 + baseM*baseK*sizeof(U)` 等）必须同步改，否则乒乓缓冲互相踩踏。

**第 3 步：性能数据（三选一，按可用条件降级）**

- **有 A2/A3 真机**：按 [docs/coding/performance-best-practices.md:L80-L91](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/performance-best-practices.md#L80-L91) 用 msprof 采集各阶段 Ratio，把三组结果填成与 gemm_performance README 同格式的表；
- **无真机、有本仓库**：用 CostModel 模拟：

```bash
python3 tests/run_costmodel.py --demo gemm --verbose
```

（入口参数见 [tests/run_costmodel.py:L469-L503](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_costmodel.py#L469-L503)，`--demo gemm` 会构建并运行 gemm demo；CostModel 的流水线建模原理在 u10-l3 展开。CPU demo 侧还有一个最小的计时样例 [demos/cpu/gemm_demo/gemm_demo.cpp:L113-L141](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/cpu/gemm_demo/gemm_demo.cpp#L113-L141)，它输出 `perf: avg_ms/gflops`，并支持 `PTO_CPU_PEAK_GFLOPS` 环境变量输出 MFU——但注意它只是宿主机参考，不代表 NPU。）具体周期数字**待本地验证**；
- **纯纸面**：用 4.3.4 的推导完成判定（下述第 4 步在此条件下即结论）。

**第 4 步：判定 CUBE Bound 还是 MTE Bound**

用 4.3.4 的结论：三组的 GM 流量与 FLOP 相同，\(AI \approx 79\) FLOP/字节。按文档启发值 Cube 峰值 ~50 TFLOPS/核，喂饱单个 Cube 需要 \(50 \times 10^{12} / 79 \approx 630\) GB/s，24 核合计约 15 TB/s——远超常规 HBM 带宽量级。结论：**该 kernel 在理想流水下是 MTE（memory-feed）Bound**——即使 Ratio 表上 TMATMUL 看着忙碌，限制端到端时间的是供数通路。这也与形状小（512×2048×1536）、warm-up/drain 占比高的现实相符：实测中它还会额外受启动开销拖累。

三组扫描的对比结论则回答「tile 旋钮改变了什么」：baseK 不改变 GM 流量与 AI（Bound 类型不变），改变的是 DMA 条数（64→32→16）、burst 大小与 L1 占用（80→96→128 KiB）——即**搬运效率与容量余量**。因此三组中若在真机/CostModel 上测量，预期 C 组（大 baseK、少 DMA）在 TLOAD Ratio 上更从容，但 C 组 L1 已用 128 KiB、L0 顶满，再无加大 stepK 的空间——这正是 4.2.2「吃满但不超」预算规则的现实意义。

**验收标准**：一张三组形状的记录表（kLoop / 占用 / DMA 条数 / AI）+ 一段判定推理 + （如有）Ratio 数据交叉验证。

## 6. 本讲小结

- **瓶颈判定先行**：用 Ratio 阈值（TLOAD+TSTORE>60% → Memory Bound；TMATMUL>70% → Compute Bound；TEXTRACT/TMOV>20% → Conversion Bound）、算术强度理论（\(BW_{need}=P_{cube}/AI\) 对比可用带宽）、时间增量三条证据链定位瓶颈；`gemm_performance` 的实测表（TLOAD 95%+ 时 memory-feed limited）是最好的判读范例。
- **三个正交调优维度**：tile 大小（容量预算与复用）、tile 形状（32 字节对齐与整除约束）、指令排序（只等真依赖、稳态循环禁止全局排空、首尾补同步）。
- **容量先算后写**：L0A/L0B 乒乓半区 32 KiB、L1 staging 双缓冲 ×2、Acc 常驻，三笔账加起来装得下才谈得上形状选择。
- **内存三板斧**：数据复用（K 分块、Acc/行统计常驻、stepK caching）、布局对齐（一开始就选对格式，成本排序：改视图 < TRESHAPE < TMOV < TTRANS）、削减 GM 流量（融合、连续访问、TPREFETCH、double buffer）。
- **一个关键不变量**：在输出块归属固定的前提下，扫描 baseK 不改变 GM 流量与算术强度——Bound 类型是 kernel 级属性，tile 扫描改善的是搬运效率与占用余量。
- **CPU 仿真的边界**：它能验证 tile 改动后的功能正确性，不能提供性能证据；性能证据来自真机 msprof 或 CostModel。

## 7. 下一步学习建议

- 下一讲进入单元七「通信指令集」：从 [u7-l1 通信 ISA 总览](u7-l1-comm-isa-overview.md) 开始，学习点对点/集合通信指令如何与计算指令共用 tile 抽象——通信带宽与 4.1 节的搬运带宽是同一类资源，本讲的 Bound 判定方法在 u7-l5 的计算通信融合中会再次用到。
- 想深入无硬件的性能评估，预习 [tests/run_costmodel.py](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/tests/run_costmodel.py) 与 `include/pto/costmodel/`，u10-l3 将拆解其轻量 CostModel 与 perf_sim 流水线建模。
- 想看本讲方法论的完整应用，通读 [kernels/manual/common/flash_atten/README.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/kernels/manual/common/flash_atten/README.md)（分阶段调优的真实笔记）与 [docs/coding/opt.md:L120-L131](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/opt.md#L120-L131) 列出的 example-driven 清单，u8-l1 会以 Flash Attention 为对象把规约、指数、流水线组合成完整算子。
