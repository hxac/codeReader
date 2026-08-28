# WarpDivRedux：warp 分支发散与无分支改写

## 1. 本讲目标

本讲是单元三「充分利用 GPU 的大规模并行能力」的第一讲。学完后你应该能够：

1. 理解 warp 是 GPU 调度的基本单位，掌握 SIMT（单指令多线程）执行方式。
2. 分析 `tid % 2` 这类分支条件为什么会让同一个 warp 内的线程交替进入两个分支（分支发散），以及它带来多大的性能损失。
3. 读懂 WarpDivRedux 中三个 kernel 的差异：`warmingup`、`warpDivergence`（反模式）、`noWarpDivergence`（无分支改写）。
4. 会用 `nvprof --metrics branch_efficiency` 量化分支优化效果，并能解释仓库归档结果中的数据。
5. 动手实现 README 中描述但 GPU 端尚未实现的「以 warp 大小为步长」的分支改写 kernel。

## 2. 前置知识

### 2.1 从线程到 warp：SIMT 执行模型

在 u2-l1 中我们知道，kernel 以 `<<<grid, block>>>` 启动，每个线程用 `blockIdx.x * blockDim.x + threadIdx.x` 算出自己的编号。但 GPU 并不是逐个线程调度的——**warp（线程束）才是调度与执行的基本单位**：连续 32 个线程组成一个 warp，硬件一次取出一条指令，让这 32 个线程（下文称 32 个 lane）同时执行同一条指令。这就是 SIMT（Single Instruction, Multiple Threads）。

一个直观的类比：warp 像 32 个人排成一排划船，教练喊一次口令，所有人同时做同一个动作。如果有人想划、有人想休息，教练没法只喊半次口令——只能先喊「划船」（想休息的人陪着坐一轮），再喊「休息」（划过的人再坐一轮）。

对一维 block（如本项目用的 256 线程），warp 的划分规则很简单：block 内线性序每 32 个线程为一个 warp。全局线程编号 `tid` 与「所在 warp、warp 内 lane 号」的对应关系是：

\[
\text{lane} = \lfloor tid \rfloor \bmod 32, \qquad \text{warp 编号} = \lfloor tid / 32 \rfloor
\]

（当 `blockDim.x` 是 32 的倍数时，如 256，一个 warp 的 32 个线程的 `tid` 恰好连续，上式严格成立。）

### 2.2 分支发散是怎么回事

当 warp 内所有线程对同一个 `if` 条件得出相同结论时，整条 warp 一次走完，没有任何浪费。当结论不一致时——比如条件是 `tid % 2`，32 个 lane 中 16 个偶、16 个奇——就发生**分支发散（warp divergence）**：硬件只能把两个分支臂**串行**执行，每个臂执行时另一半线程处于关闭状态（inactive），执行完在汇合点重新合体。

发散的代价可以写成：若 warp 分裂成 \( k \) 条路径，各臂指令耗时 \( T_1,\dots,T_k \)，则总耗时近似为：

\[
T_{\text{diverged}} \approx \sum_{k=1}^{K} T_k \;\ge\; \max_k T_k \approx T_{\text{unified}}
\]

两臂等长时，warp 在发散区段的利用率只有：

\[
\text{利用率} = \frac{\max_k T_k}{\sum_k T_k} = \frac{1}{2}
\]

注意两个要点：

- **发散损害的是发散区段本身**，不是整个 kernel。如果两臂各只有一两条寄存器赋值，串行化的绝对代价很小。
- **只有「同一 warp 内」的分歧才叫发散**。不同 warp 本来就在不同时间执行，它们走不同分支毫无损失。这是本讲综合实践的核心思路。

### 2.3 branch_efficiency 指标

`branch_efficiency` 是 nvprof 提供的硬件计数器指标，定义：

\[
\text{branch\_efficiency} = \frac{\text{非发散的分支执行次数}}{\text{分支执行总次数}} \times 100\%
\]

完全没有发散时为 100%；`tid % 2` 这种每个 warp 必然对半分裂的写法会把它显著拉低（具体数值与编译产物中分支指令的形态有关，需实测）。采集命令是 `nvprof --metrics branch_efficiency`（u1-l4 已介绍过 nvprof 的基本用法；新工具链中 nvprof 已被 Nsight Systems / Nsight Compute 取代，等价指标可用 `ncu --query-metrics` 查询，指标名待确认）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [WarpDivRedux/warpDivergenceTest.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest.h) | 接口头文件：`REAL` 宏 + `extern "C"` 声明包装函数（三件套契约） |
| [WarpDivRedux/warpDivergenceTest_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c) | host 主程序：初始化、两个串行参考实现、10 次平均计时、check 校验 |
| [WarpDivRedux/warpDivergenceTest_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu) | 本讲主角：`warmingup` / `warpDivergence` / `noWarpDivergence` 三个 kernel + 包装函数 |
| [WarpDivRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile) | 单行 nvcc 编译，带 `-g -G -arch=sm_30` |
| [WarpDivRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh) | 实验设计：5 个规模 × （nvprof 概览 + branch_efficiency 采集） |
| [WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt) | Carina 集群上的归档输出（有时间数据、指标采集失败） |
| [WarpDivRedux/warpDivergenceTest_cudakernel.ptx](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx) | nvcc 生成的 PTX 中间代码存档，无 GPU 也能「看见」分支 |

整个基准的调用链（承接近 u1-l3 的三件套骨架）：

```text
main (warpDivergenceTest_cuda.c)
 ├─ warpDivergenceSerial(...)      串行参考 1：按 i%2 分支
 ├─ NoWarpDivergenceSerial(...)    串行参考 2：按 (i/32)%2 分支
 ├─ 10 × warpDivergenceTest_cuda(...)        (.cu 中的包装函数)
 │    ├─ cudaMalloc ×4 → cudaMemcpy H2D ×2
 │    ├─ warmingup<<<(n+255)/256, 256>>>     → 写入 d_warp_divergence
 │    ├─ warpDivergence<<<...>>>             → 写入 d_warp_divergence（覆盖上者）
 │    ├─ noWarpDivergence<<<...>>>           → 写入 d_no_warp_divergence
 │    └─ cudaMemcpy D2H ×2 → cudaFree ×4
 ├─ check1 = check(warp_divergence, warp_divergence_serial)
 └─ check2 = check(no_warp_divergence, no_warp_divergence_serial)
```

一个与 CoMem_AXPY 不同的细节：本基准的 `main` 计算了平均耗时 `elapsed`，但**从不打印它**（[WarpDivRedux/warpDivergenceTest_cuda.c:L103-L110](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L103-L110) 只打印两行 check）。所以程序自身输出里没有 time 行，所有时间数据都必须来自 nvprof。

## 4. 核心概念与源码讲解

### 4.1 warmingup：预热 kernel——「两臂各算一遍」的完整形态

#### 4.1.1 概念说明

`warmingup` 首先是 u2-l4 讲过的**预热 kernel**：每次调用 `warpDivergenceTest_cuda` 时它第一个启动，用来消化首次 kernel 启动的一次性开销（模块加载、上下文初始化等），让后面的测量更干净。

但它同时是本基准里**第三种分支形态**：它的 `if/else` 两个分支臂里各自放了一条**完整的计算语句**（包括全局内存读和写）。对比后面 4.2 的 `warpDivergence`（分支里只有寄存器赋值），`warmingup` 展示了「把访存和计算整体放进两个分支」时发散更痛的样子。

#### 4.1.2 核心流程

`warmingup` 对每个 tid 做的事：

```text
tid 为偶数: z[tid] = 2*x[tid] + 3*y[tid]
tid 为奇数: z[tid] = 3*x[tid] + 2*y[tid]
```

执行到 `if` 时，一个 warp 内 16 个偶 lane 与 16 个奇 lane 意见不一，硬件串行执行两个臂：

```text
pass 1: 偶 lane 活跃、奇 lane 关闭 → 执行 ld x, ld y, 2*x+3*y, st z
pass 2: 奇 lane 活跃、偶 lane 关闭 → 再执行一遍 ld x, ld y, 3*x+2*y, st z
汇合 → 结束
```

也就是说，发散区段内的加载、乘加、存储指令 warp 都要发两遍（每遍只有一半 lane 干活），指令发射量接近翻倍。

#### 4.1.3 源码精读

kernel 本体（注意两臂各自包含完整的 `z[tid] = ...` 赋值）：

- [WarpDivRedux/warpDivergenceTest_cudakernel.cu:L9-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L9-L17) —— `warmingup` kernel：以 `tid % 2 == 0` 为分支条件，偶数线程走 `2*x+3*y`、奇数线程走 `3*x+2*y`，两臂都内含全局内存访问。

它在包装函数中的启动位置（每次调用都执行，且写入的正是 `d_warp_divergence` 缓冲，随后被 `warpDivergence` 覆盖）：

- [WarpDivRedux/warpDivergenceTest_cudakernel.cu:L46-L52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L46-L52) —— 先 `cudaDeviceSynchronize`，再依次启动 `warmingup` 与 `warpDivergence`，各自跟一次同步；网格都是 `(n+255)/256` 个 block、每 block 256 线程（u2-l1 讲过的向上取整技巧）。

Carina 集群归档结果中三个 kernel 的平均耗时（n=1,024,000）：

- [WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt:L15-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L15-L17) —— `warmingup` 36.33µs、`warpDivergence` 31.29µs、`noWarpDivergence` 29.76µs：两臂含完整计算+访存的 `warmingup` 最慢。

需要诚实标注：`warmingup` 的耗时里混有「第一次触碰数据、冷缓存」等预热效应，不能把 36.33µs 与 31.29µs 的差距全部归因于分支形态差异——它本来就是用来被「污染」的。

#### 4.1.4 代码实践：用 Calls 列反向核对程序结构

这是一个**无 GPU 也能做**的实践，直接阅读归档输出即可。

1. **实践目标**：用 nvprof 输出的调用次数反推程序结构，确认你理解的调用链与真实执行一致。
2. **操作步骤**：打开 [WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt:L12-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L12-L25)，把 `warmingup`/`warpDivergence`/`noWarpDivergence`、`[CUDA memcpy HtoD]`、`[CUDA memcpy DtoH]`、`cudaMalloc`、`cudaLaunchKernel` 各行的 Calls 值抄下来，逐一解释它们的来源。
3. **需要观察的现象**：三个 kernel 各 10 次；HtoD 与 DtoH 各 20 次；`cudaMalloc` 与 `cudaFree` 各 40 次；`cudaLaunchKernel` 共 30 次。
4. **预期结果**：`main` 的计时循环跑 `num_runs = 10` 次（[WarpDivRedux/warpDivergenceTest_cuda.c:L98-L104](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L98-L104)），每次调用包装函数启动 3 个 kernel（3×10=30）、做 2 次 H2D 与 2 次 D2H（2×10=20 各）、分配并释放 4 块显存（4×10=40 各）。数字应严丝合缝——这就是 u1-l4 说的「Calls 列可反向核对程序结构」。

#### 4.1.5 小练习与答案

**练习 1**：`warmingup` 是预热 kernel，为什么 nvprof 表格里它也有完整的 10 次统计？

答案：预热的作用是消化**首轮**的一次性开销，但包装函数每次被调用都会启动它（[warpDivergenceTest_cudakernel.cu:L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L48)），nvprof 统计的是全部执行，所以 10 次调用就有 10 次记录；「预热」指的是第 1 次为后面 9 次铺路，不是「只跑一次」。

**练习 2**：`warmingup` 与 4.2 的 `warpDivergence` 分支条件相同（都是 tid 的奇偶性），为什么归档结果里前者更慢？

答案：`warmingup` 把全局内存读、乘加、写整体放进两个分支臂，发散后 warp 要把这套指令串行发射两遍；`warpDivergence` 的分支臂里只有两条寄存器赋值，发散代价小得多。另外 `warmingup` 还承受首次触碰数据的冷缓存代价，两部分叠加（因此不能精确归因）。

### 4.2 warpDivergence：tid%2 ——教科书级的发散反模式

#### 4.2.1 概念说明

这是本基准的**反模式（anti-pattern））主角**。README 对它的描述是：

- [README.md:L23-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L23-L25) —— WarpDivRedux 行：低效模式为「线程遇到控制流语句时进入不同分支」，优化思路为「改变算法：以 warp 大小为步长」。

`tid % 2` 是对分支发散伤害最大的写法：warp 内 32 个 lane 的 tid 连续，奇偶必然逐个交替，于是**每一个 warp、每一次执行**这个 `if` 都必然分裂成 16/16 两半——没有任何一个 warp 能逃过发散。用 active mask 表示，两个执行 pass 的活跃掩码分别是 `0x5555…`（奇 lane）与 `0xAAAA…`（偶 lane）。

值得注意的细节：这个 kernel 的分支臂里只有对寄存器 `a`、`b` 的赋值，真正的访存与计算 `z[tid] = a*x[tid] + b*y[tid]` 放在分支**外面**。这是一种「轻度发散」——发散区段极短，所以消除它的收益是有限的（归档数据显示约 5%，见 4.2.3）。理解「发散代价 ≈ 两臂指令串行执行的浪费」就能预判这个量级。

#### 4.2.2 核心流程

```text
每个线程:
  tid = blockIdx.x * blockDim.x + threadIdx.x
  a = 2, b = 3                 ← 默认（偶数路径）
  if (tid % 2 != 0):
      a = 3; b = 2             ← 奇数路径（分支臂只有两条寄存器赋值）
  z[tid] = a * x[tid] + b * y[tid]   ← 分支外：每个线程恰好读 2 个、写 1 个 float
```

发散过程：

```text
warp（lane 0..31，tid 连续）
  │  if (tid % 2 != 0)：16 偶 vs 16 奇 → 分裂
  ├─ pass 1: 奇 lane 活跃  → a=3, b=2   （偶 lane 闲置）
  ├─ pass 2: （汇合）
  └─ 全 warp 执行 ld x / ld y / mul / mul / add / st
```

每个线程搬运 12 字节、只做 3 次浮点运算，这个 kernel 与 u2-l1 的 AXPY 一样是**访存受限**的，分支开销只是零头。

#### 4.2.3 源码精读

kernel 本体：

- [WarpDivRedux/warpDivergenceTest_cudakernel.cu:L19-L27](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L19-L27) —— `warpDivergence` kernel：分支条件 `tid % 2 != 0`，两臂只给 `a`、`b` 赋不同值，计算在分支外完成。这就是反模式的标本。

配套的串行参考实现：

- [WarpDivRedux/warpDivergenceTest_cuda.c:L42-L49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L42-L49) —— `warpDivergenceSerial`：CPU 单线程版，同样的 `i%2` 分支。串行 CPU 上分支没有 warp 发散问题，它只作正确性参照（u2-l4 的 oracle 概念）。

编译产物里的分支证据（`-G` 关闭优化后分支原样保留）：

- [WarpDivRedux/warpDivergenceTest_cudakernel.ptx:L131-L139](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx#L131-L139) —— `rem.s32`（即 tid%2）、`setp.ne.s32`/`not.pred` 生成谓词，随后 `@%p2 bra`（谓词跳转）与 `bra.uni`（无条件跳转）构成两个分支臂。
- [WarpDivRedux/warpDivergenceTest_cudakernel.ptx:L158-L177](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx#L158-L177) —— 汇合点之后的公共代码：两次 `ld.f32` 读 x、y，`mul/mul/add`，一次 `st.f32` 写 z，印证「访存与计算在分支外」。

Carina 归档的跨规模数据（每次调用的平均耗时）：

| n | warmingup | warpDivergence | noWarpDivergence |
| --- | --- | --- | --- |
| 1,024,000 | 36.33µs | 31.29µs | 29.76µs |
| 4,096,000 | 129.68µs | 109.78µs | 103.85µs |
| 10,240,000 | 313.39µs | 266.58µs | 253.89µs |
| 40,960,000 | 1.2061ms | 1.0306ms | 0.9773ms |
| 102,400,000 | 2.8693ms | 2.4359ms | 2.3218ms |

（来源：[warpDivergenceTest_cuda.output.carina.txt:L15-L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L15-L17) 与 [L143-L145](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L143-L145)。）无分支版稳定快约 5%——与「两臂只有两条寄存器赋值、kernel 访存受限」的预判一致。

#### 4.2.4 代码实践：读 PTX，亲眼看见分支

编译生成 PTX 不需要 GPU（u1-l2 结论），仓库也已存档一份。

1. **实践目标**：在编译产物层面确认 `warpDivergence` 确实生成了两条互斥跳转路径，而 `noWarpDivergence` 没有跳转。
2. **操作步骤**：进入 `WarpDivRedux/` 目录，可直接阅读仓库存档 [warpDivergenceTest_cudakernel.ptx](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx)，或自己重新生成：`nvcc -ptx warpDivergenceTest_cudakernel.cu`（会输出同名 .ptx）。在 `_Z14warpDivergencePfS_S_`（`warpDivergence` 的修饰名）一段找到 `rem.s32`、`@%p2 bra`、`bra.uni`；再到 `_Z16noWarpDivergencePfS_S_` 一段数一数 `bra` 的个数。
3. **需要观察的现象**：`warpDivergence` 段有谓词跳转 + 两个标号（`$L__BB1_1`、`$L__BB1_2`）构成的两臂；`noWarpDivergence` 段（[L212-L219](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx#L212-L219)）只有 `rem/setp/selp`——用选择指令代替跳转，全程无 `bra`。
4. **预期结果**：源码里的 `if` 与 PTX 里的 `bra` 一一对应；「无分支」在编译产物层面同样成立。若你在重新生成时去掉 Makefile 里的 `-G`（即开优化），对比 `warpDivergence` 段是否还能找到 `bra`——优化器很可能自动把短 if/else 改写成选择指令，届时反模式就被编译器「悄悄修复」了（此对比结果待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：把条件改成 `tid % 32 == 0`，发散情况如何变化？

答案：每个 warp 内 1 个线程（lane 0）走 then 臂、31 个走 else 臂，仍然是发散的（两臂都要执行），`branch_efficiency` 同样受损；与 `tid%2` 的区别只是两臂活跃线程比例从 16/16 变成 1/31。要完全不发散，必须让**整个 warp 的 32 个线程取同一侧**。

**练习 2**：为什么消除这个 kernel 的分支只能带来约 5% 的提速？

答案：发散代价约为两臂指令串行执行多付的部分；这里两臂各只有两条寄存器赋值指令，串行化浪费极小，而 kernel 本身访存受限（每线程读 8 字节写 4 字节、仅 3 次浮点运算），时间大头在内存搬运上。若像 `warmingup` 那样把访存与计算放进两臂，消除发散的收益会大得多。

**练习 3**：`blockDim.x = 256` 时，`tid / 32` 在一个 warp 内是不是常数？

答案：是。`tid = 256*blockIdx.x + threadIdx.x`，`tid/32 = 8*blockIdx.x + threadIdx.x/32`；同一 warp 内 `threadIdx.x/32` 相同，所以 `tid/32` 对整个 warp 是常数——这正是综合实践中「warp 对齐分支」可行的原因（前提是 blockDim.x 是 32 的倍数）。

### 4.3 noWarpDivergence：无分支算术改写——把 if 变成乘加

#### 4.3.1 概念说明

这是与 4.2 配对的**优化技术**：完全不用 `if`，用算术运算从条件里「选出」系数。技巧在于 C 语言的关系比较结果就是 0 或 1：

```c
int even = tid % 2 == 0;   // 偶数 → 1，奇数 → 0
```

再用这个 0/1 值线性组合出 `a`、`b`。数学上，源码写的

\[
a = \text{even}\times 2 + (1-\text{even})\times 3 = 3 - \text{even}, \qquad
b = \text{even}\times 3 + (1-\text{even})\times 2 = 2 + \text{even}
\]

代入验证：偶数线程（even=1）得 \( a=2, b=3 \)；奇数线程（even=0）得 \( a=3, b=2 \)——与 `warpDivergence` 的两臂完全等价，但全程没有任何分支指令，每个 warp 一次走完，`branch_efficiency` 不受影响。这种手法叫**算术选择 / 手工谓词化**；优化开启时编译器对短 if/else 也会做类似改写（上一节的 PTX 实践可以验证）。

代价是多出几条标量整型运算。分支臂越短，这种改写越划算；分支臂很长时，更合适的思路是让整个 warp 走同一分支（本讲综合实践）。

#### 4.3.2 核心流程

```text
每个线程:
  tid  = blockIdx.x * blockDim.x + threadIdx.x
  even = (tid % 2 == 0)            → 0/1
  a = even*2 + (1-even)*3          → 2 或 3
  b = even*3 + (1-even)*2          → 3 或 2
  z[tid] = a * x[tid] + b * y[tid]
无 if、无跳转 → warp 不分裂
```

正确性口径：输出与 `warpDivergence` 逐元素相同（两臂数学等价、浮点运算顺序也一致），所以 `check` 结果应完全一致。

#### 4.3.3 源码精读

- [WarpDivRedux/warpDivergenceTest_cudakernel.cu:L29-L35](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L29-L35) —— `noWarpDivergence` kernel：用 0/1 布尔值乘加选出 `a`、`b`，消灭了分支；函数签名与启动方式与 `warpDivergence` 完全相同，唯一的差别就是控制流写法——微基准的控制变量方法论（u1-l3）在此的体现。
- [WarpDivRedux/warpDivergenceTest_cudakernel.ptx:L212-L214](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.ptx#L212-L214) —— 编译产物证据：`rem.s32` 算奇偶、`setp.eq.s32` 比较、`selp.u32 %r7, 1, 0, %p1` 用**选择指令**得到 0/1；整个 kernel 找不到一条 `bra`。
- [WarpDivRedux/warpDivergenceTest_cudakernel.cu:L54-L58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L54-L58) —— 它被启动到独立的 `d_no_warp_divergence` 缓冲并单独拷回主机，与 `warpDivergence` 的输出互不覆盖，供 `main` 分别校验。

一个必须诚实指出的**源码观察**：本基准的初始化里 `init(y, n)` 被注释掉，`y` 是 `x` 的逐元素拷贝（[WarpDivRedux/warpDivergenceTest_cuda.c:L91-L94](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L91-L94)）。于是任何分支下都有 \( z = a\,x + b\,y = (a+b)\,x = 5x \)——两个系数互换根本不改变结果。这解释了归档输出里两行 `check:0.000000`（如 [output.carina.txt:L8-L9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L8-L9)）：这里的 check 是一个**弱校验**，它证明「结果与串行参考一致」，但两边其实都等于 5x，分支选择对输出零影响——本基准刻意把「控制流开销」与「计算结果」解耦，check 只能兜底「没算崩」。这与 u2-l4 讲过的「checksum 是结构性产物」一脉相承。

还有一个更微妙的点：串行参考 `NoWarpDivergenceSerial` 用的分支条件是 `(i/32)%2`（[WarpDivRedux/warpDivergenceTest_cuda.c:L51-L58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L51-L58)），而 GPU 端 `noWarpDivergence` 的选择依据是 `tid%2`——两者的**逐元素映射并不相同**，当前 check2 能得 0 纯粹是因为 y==x 让所有写法殊途同归。这个不一致恰好指向 README 描述的那条尚未在 GPU 端实现的优化路径，综合实践将把它补全。

#### 4.3.4 代码实践：让弱校验现出原形

1. **实践目标**：亲手证明「y==x 掩盖了映射差异」，体会 check 通过不等于语义一致。
2. **操作步骤**：复制一份 WarpDivRedux 目录做实验（保持原目录不动），把 [warpDivergenceTest_cuda.c:L93-L94](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L93-L94) 处的 `//init(y, n);` 取消注释、注释掉 `memcpy(y, x, ...)`，使 y 成为与 x 独立的随机向量；重新 `make`（架构选项见综合实践的说明）并运行 `./warpDivergenceTest_cuda 1024000`。
3. **需要观察的现象**：两行 `check:` 的值如何变化。
4. **预期结果**：check1（GPU `warpDivergence` vs 串行 `i%2` 版）仍为 0.000000——两者映射与运算顺序完全一致；check2（GPU `tid%2` 算术选择版 vs 串行 `(i/32)%2` 版）变为明显非零——两版对同一元素可能取不同系数组合（如 tid=1：一版取 3x+2y、另一版取 2x+3y）。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：证明 `even*2 + (1-even)*3 == 3 - even`。

答案：展开左边 \( 2\,\text{even} + 3 - 3\,\text{even} = 3 - \text{even} \)。源码故意写成前一种形式，是为了让「按 even 选 2 或 3」的选择结构一目了然。

**练习 2**：`noWarpDivergence` 多做了几条整型运算，为什么反而更快？

答案：省掉了分支指令的发射与两臂串行执行；新增的只是几条标量整型乘加（编译后是一条 `selp` 加少量算术），远比对分支便宜。在访存受限的整体格局下，归档数据显示约 5% 的稳定优势。

**练习 3**：什么时候「算术选择」不划算？

答案：当两臂各自包含大量互不相同的计算或访存时。算术选择要求两条路径都（以掩码方式）执行，长臂场景会把两臂的工作全付一遍，反而不如让整个 warp 统一走同一分支（按 warp 对齐划分数据），或干脆把两臂拆成两次独立的 kernel 启动。

## 5. 综合实践：实现 README 描述的「warp 对齐分支」kernel

README 给出的优化思路是「**以 warp 大小为步长**」（[README.md:L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L25)）。它已经体现在串行参考 `NoWarpDivergenceSerial` 的 `(i/32)%2` 里，但 GPU 端并没有对应 kernel——GPU 的 `noWarpDivergence` 走的是另一条路（无分支算术选择）。本实践把 README 描述的方案真正落地：**分支条件以 warp 为粒度，让同一 warp 的 32 个线程永远走同一分支，从源头上避免发散**。

1. **实践目标**：新增一个 `warpAlignedDivergence` kernel，与 `warpDivergence`/`noWarpDivergence` 在同一程序中对比 `branch_efficiency` 与耗时。

2. **操作步骤**：

   a. 复制实验目录，避免污染原仓库：
   ```bash
   cp -r WarpDivRedux /tmp/WarpDivRedux-practice && cd /tmp/WarpDivRedux-practice
   ```

   b. 按需调整 [Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/Makefile)：`-arch=sm_30` 在 CUDA 12 及以后已无法编译（u1-l2 结论），用 `nvidia-smi` 查本机计算能力后替换（如 `-arch=sm_86`）或删去该选项；**保留 `-g -G`**——关闭优化才能保证分支原样保留、实验不失真（见 4.2.4）。

   c. 在 .cu 中新增 kernel（示例代码，非项目原有）：
   ```cuda
   __global__ void warpAlignedDivergence(float *x, float *y, float *z) {
       int tid = blockIdx.x * blockDim.x + threadIdx.x;
       float a = 2, b = 3;
       if ((tid / 32) % 2 != 0) {   // 同一 warp 内 tid/32 为常数 → 整 warp 同侧
           a = 3;
           b = 2;
       }
       z[tid] = a * x[tid] + b * y[tid];
   }
   ```
   依据：`blockDim.x = 256` 是 32 的倍数，`tid/32` 对整个 warp 是常数（练习见 4.2.5 第 3 题）。若改用非 32 倍数的 block，请改用 `(threadIdx.x/32)%2`（block 内局部 warp 号对任意 block 大小都恒定于一个 warp 之内）。

   d. 仿照 [warpDivergenceTest_cudakernel.cu:L36-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cudakernel.cu#L36-L68) 的包装函数：新增一个 `cudaMalloc` 缓冲、一次 kernel 启动（后跟 `cudaDeviceSynchronize`）、一次 D2H 拷贝和一次 `cudaFree`，并给函数签名增加一个输出指针参数。随后同步修改两处契约（u1-l3 的三件套规则）：[warpDivergenceTest.h:L11](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest.h#L11) 的 `extern "C"` 声明，以及 [warpDivergenceTest_cuda.c:L104](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L104) 处 `main` 的调用（记得在 L82-L88 一带为新输出数组 `malloc`、free）。

   e. 校验对象就选现成的 `no_warp_divergence_serial`——`NoWarpDivergenceSerial` 的 `(i/32)%2` 与新 kernel 的映射**完全一致**（这正是它存在的意义）：在 main 末尾仿照 [L107-L110](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.c#L107-L110) 加一行 `check` 与打印。

   f. 编译并采集（沿用 [test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/test.sh) 的方式，先用小规模）：
   ```bash
   make
   ./warpDivergenceTest_cuda 1024000
   nvprof ./warpDivergenceTest_cuda 1024000
   nvprof --metrics branch_efficiency ./warpDivergenceTest_cuda 1024000
   ```

3. **需要观察的现象**：三行 check 的值；nvprof 概览中四个 kernel 的耗时排序；`branch_efficiency` 表中各 kernel 的数值。

4. **预期结果**：
   - 三行 check 均为 0.000000——由 4.3.3 的分析，y==x 使所有分支写法都输出 5x（新 kernel 对照的串行版映射一致，更是精确为 0）。
   - 耗时上 `warpAlignedDivergence` 应与 `warpDivergence` 接近（两者分支臂同样只有两条寄存器赋值，kernel 访存受限）。
   - `branch_efficiency`：`warpDivergence` 与 `warmingup` 显著低于 100%（每个 warp 必然对半分裂）；`noWarpDivergence` 接近 100%（无数据依赖分支可计）；`warpAlignedDivergence` **虽然源码里有 if**，也应接近 100%——因为条件在 warp 内是常数，整 warp 统一走一侧。具体数值待本地验证。
   - 权限提醒：采集硬件计数器可能报 `ERR_NVGPUCTRPERM`——Carina 归档中 branch_efficiency 全部因该错误采集成失败（[output.carina.txt:L33-L37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/WarpDivRedux/warpDivergenceTest_cuda.output.carina.txt#L33-L37)），需要在有计数器权限的环境（管理员或性能组）运行，或改用 Nsight Compute 采集等价指标。

5. **思考题（配合结果）**：`warpAlignedDivergence` 与 `noWarpDivergence` 都把 branch_efficiency 拉满，两者各自付出了什么代价？（前者：分支仍在，但按 warp 整体执行、靠数据划分避免分裂；后者：无分支，但每条路径的算术都要做一遍。）

## 6. 本讲小结

- warp 是 GPU 调度与执行的基本单位（32 线程，SIMT）；同一 warp 内线程对分支意见不一才会发散，硬件串行执行各臂，发散区段利用率可低至 \( \max_k T_k / \sum_k T_k \)。
- `tid % 2` 是发散的最坏写法：每个 warp 必然 16/16 分裂；本基准中两臂只有寄存器赋值，加上 kernel 访存受限，消除发散仅换来归档数据中稳定约 5% 的提速——发散的代价取决于两臂长度，而非分支存在与否。
- `noWarpDivergence` 用 0/1 布尔值乘加（手工谓词化/算术选择）消灭分支，PTX 中只有 `selp`、没有 `bra`；`warmingup` 则展示了两臂内含完整访存与计算的更重形态。
- Makefile 的 `-G` 关闭设备端优化，保证 if 原样编译成分支而不是被优化器自动改写成选择指令——对「演示反模式」的基准这是保真手段，但绝对时间不可作性能结论（承接 u1-l2）。
- 本基准的 check 是弱校验：y==x 使所有分支写法都输出 5x，且 GPU `noWarpDivergence`（tid%2）与串行参考（(i/32)%2）映射本不相同却被掩盖——读微基准必须核对初始化与校验逻辑。
- README 记载的「以 warp 大小为步长」优化在串行参考中有、GPU 端缺席；`(tid/32)%2`（blockDim 为 32 的倍数时）让整 warp 同侧，是无分支之外的另一条消除发散的路线。

## 7. 下一步学习建议

- **同单元继续**：u3-l2（DynParallel 动态并行）讲 GPU 自己生成工作的另一种「饱和并行」手段；u3-l3（Conkernels）与 u3-l4（TaskGraph）讲多 kernel 并发与任务图提交。
- **向存储层次深入**：u4-l5（BankRedux）与 u4-l6（Shuffle）同属 warp 级技术——一个讲共享内存 bank 冲突，一个用 `__shfl_down_sync` 在 warp 内寄存器间交换数据，与本讲的 warp 概念直接衔接。
- **进阶阅读**：打开 `nvcc -ptx` 或 `cuobjdump -sass` 对照阅读三个 kernel 的产物，观察 `-G` 开关对分支形态的影响；学完 u4-l6 后再回看 u6-l1 的 reduce0→reduce6 优化阶梯，其中「整 warp 闲置」「按 warp 对齐归约」正是本讲发散概念的综合应用。
