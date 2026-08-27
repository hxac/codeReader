# TaskGraph：CUDA Graph 与共轭梯度求解器

## 1. 本讲目标

上一讲（u3-l3）我们用 Conkernels 学了 CUDA stream：**多条流让多个 kernel 并发执行**，解决的是「GPU 并行能力没被喂饱」的问题。本讲换一个视角：当一条流上的工作又**多又碎**时，即使 GPU 空着，CPU 也可能忙不过来——每提交一个 kernel 都要付出一次 API 调用的开销。CUDA Graph 就是解决「CPU 提交开销」的武器。

学完本讲，你应该能够：

1. 说出 CUDA Graph 的三步工作流：**stream capture → instantiate → launch**，并在源码中定位每一步。
2. 解释为什么「重复提交同一串任务序列」时 graph 能显著减少 CPU 侧的 API 开销，并会用公式估算节省量。
3. 把共轭梯度（CG）求解器的一次迭代拆解成一串 GPU 操作（SpMV、点积、SAXPY、单线程标量 kernel），看懂它们如何被捕获成一张图。
4. 理解本例最精妙的设计：**让所有随迭代变化的标量都留在 GPU 上生产**（pointer mode + `<<<1,1>>>` kernel），从而使整张图可以不加修改地反复重放。

## 2. 前置知识

### 2.1 kernel 启动开销：为什么「碎任务」会累垮 CPU

在 [u2-l1](u2-l1-first-cuda-kernel-axpy.md) 里我们学过 kernel 用 `<<<grid, block>>>` 语法启动。每一次这样的启动（以及每一次 `cudaMemcpyAsync`、`cudaMemsetAsync`）都对应一次**主机端 API 调用**：CPU 要走运行时库 → 驱动 → 命令队列这一整条路，典型开销在微秒量级。

这对单个大 kernel 无所谓——kernel 本身跑几百微秒，几微秒的提交开销完全被覆盖。但如果一个「迭代」由十几个**小操作**组成，而整个程序要迭代成百上千次，CPU 提交就会成为瓶颈：GPU 干完活了还在等 CPU 把下一条命令排进队列。这种程序称为 **launch-bound**（提交受限）。

CUDA Graph 的思路很直接：既然每次迭代提交的都是**同一串操作**，那就把这一串操作录制下来，之后每次迭代只需提交「重放这张图」一条命令。

### 2.2 共轭梯度法（CG）一分钟速览

本讲的载体是一个共轭梯度求解器，用来解线性方程组 \( A\mathbf{x} = \mathbf{b} \)，其中 \( A \) 是对称正定矩阵。CG 是迭代法：从一个初始猜测出发，每轮更新一次 \( \mathbf{x} \)，残差 \( \mathbf{r} = \mathbf{b} - A\mathbf{x} \) 逐步缩小。每轮迭代核心是这几步：

\[
\begin{aligned}
\beta_k &= \frac{\mathbf{r}_{k+1}\cdot\mathbf{r}_{k+1}}{\mathbf{r}_k\cdot\mathbf{r}_k}, &
\mathbf{p}_{k+1} &= \beta_k \mathbf{p}_k + \mathbf{r}_{k+1} \\
\alpha_k &= \frac{\mathbf{r}_{k+1}\cdot\mathbf{r}_{k+1}}{\mathbf{p}_k\cdot A\mathbf{p}_k}, &
\mathbf{x}_{k+1} &= \mathbf{x}_k + \alpha_k \mathbf{p}_k \\
\mathbf{r}_{k+2} &= \mathbf{r}_{k+1} - \alpha_k A\mathbf{p}_k
\end{aligned}
\]

看不懂推导没关系，本讲只需要记住两个特征：

- 每轮迭代由**少量向量和标量运算**组成：一次稀疏矩阵-向量乘（SpMV）、三次 SAXPY（\( \mathbf{y} = a\mathbf{x} + \mathbf{y} \)）、几次点积（dot）、几次标量除法/取负。
- 迭代次数事先未知，由残差是否小于容差决定——这正是「同一串操作重复若干次」的典型场景。

### 2.3 cuBLAS 与 cuSPARSE

源码里大量调用 `cublasXxx` 和 `cusparseXxx`，它们是 NVIDIA 的两个 GPU 数学库：**cuBLAS** 提供稠密向量/矩阵运算（本讲用到 `cublasSaxpy`、`cublasSdot`、`cublasSscal`、`cublasScopy`），**cuSPARSE** 提供稀疏矩阵运算（本讲用到 `cusparseSpMV` 做稀疏矩阵-向量乘）。库调用在底层同样会启动 kernel，所以「捕获一条流」时，库调用产生的 kernel 会一并被捕获进图。

另外，TaskGraph 这个基准来自 CUDA Samples（文件头是 NVIDIA 2019 年版权），目录里没有 test.sh，也没有仓库自带的 `.output.txt` 归档——实验需要我们自己在有 GPU 的机器上做。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [TaskGraph/conjugateGradientCudaGraphs.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu) | 唯一的源码文件：CG 求解器 + CUDA Graph 捕获与重放，host 与 device 代码都在其中（该基准不再是「三件套」结构，而是 CUDA Samples 风格的单文件） |
| [TaskGraph/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/README.md) | 样本说明：用到的 CUDA Graph API 清单、支持的架构、CUDA 11.1 依赖 |
| [TaskGraph/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile) | CUDA Samples 官方模板 Makefile：多架构 GENCODE、静态链接 cublas/cusparse、`-I../Common` 引入 helper 头 |
| [Common/helper_cuda.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_cuda.h) / [Common/helper_functions.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/helper_functions.h) | 提供 `checkCudaErrors` 错误检查宏与 `findCudaDevice` 设备选择（见 u6-l3 的 Common 依赖专题） |

先记住整个程序的骨架（后面按行号逐段精读）：

```text
main
 ├─ 生成三对角对称矩阵（CSR），N = 1048576          L145-159
 ├─ 创建 cublas/cusparse handle、分配显存            L162-211
 ├─ 初始残差 r = b - Ax，r1 = dot(r,r)               L223-246
 ├─ 第 1 次迭代（k=1，逐个提交，不进图）              L248-275
 ├─ 捕获「一次迭代」为图 + 实例化      ← 本讲核心    L277-320
 ├─ while 循环：cudaGraphLaunch 整图重放             L325-361
 ├─ 结果拷回 + 主机端误差校验                         L363-387
 └─ 销毁 graphExec/graph/流/handle，退出              L389-417
```

## 4. 核心概念与源码讲解

### 4.1 共轭梯度迭代 kernel：把一次迭代拆成 GPU 操作序列

#### 4.1.1 概念说明

要用 graph 优化「每次迭代」，第一步是搞清楚每次迭代到底由哪些操作组成。CG 每轮迭代是纯数据流：向量进、向量出，中间夹着几个标量。本例中这些操作分三类：

1. **库调用**（大头）：SpMV、点积、SAXPY，交给 cuSPARSE/cuBLAS；
2. **手写小 kernel**：两个只跑 1 个线程的「标量 kernel」，在 GPU 上算除法和取负；
3. **runtime 调用**：两次 `cudaMemsetAsync`、两次 `cudaMemcpyAsync`（一次设备到设备、一次设备到主机）。

#### 4.1.2 核心流程

矩阵与迭代参数在 main 一开始就定死了：

- 矩阵规模 `N = 1048576`（即 \( 2^{20} \)），非零元 `nz = (N-2)*3 + 4 = 3145726`；
- 收敛容差 `tol = 1e-5f`，最大迭代 `max_iter = 10000`。

矩阵由 `genTridiag` 生成：随机三对角**对称**正定矩阵，对角元约 10~11、非对角元 0~1，对角占优。对角占优意味着条件数不大，CG 收敛很快（预计几十次迭代就达标，具体次数待本地验证）。

一次迭代的操作序列（即后面被捕获进图的内容）与 CG 公式的对应关系：

| CG 公式 | 代码落点（捕获块内行号） | 实现方式 |
| --- | --- | --- |
| \( \beta_k = r_1 / r_0 \) | L284 `r1_div_x(d_r1, d_r0, d_b)` | 手写 `<<<1,1>>>` kernel |
| \( \mathbf{p} \leftarrow \beta_k \mathbf{p} \) | L286 `cublasSscal` | cuBLAS |
| \( \mathbf{p} \leftarrow \mathbf{p} + \mathbf{r} \) | L288 `cublasSaxpy`（α=1） | cuBLAS |
| \( A\mathbf{p} \) | L293 `cusparseSpMV` | cuSPARSE |
| \( \mathbf{p}\cdot A\mathbf{p} \) | L298 `cublasSdot` | cuBLAS |
| \( \alpha_k = r_1 / \text{dot} \) | L300 `r1_div_x(d_r1, d_dot, d_a)` | 手写 `<<<1,1>>>` kernel |
| \( \mathbf{x} \leftarrow \mathbf{x} + \alpha_k\mathbf{p} \) | L302 `cublasSaxpy` | cuBLAS |
| \( -\alpha_k \) | L304 `a_minus` | 手写 `<<<1,1>>>` kernel |
| \( \mathbf{r} \leftarrow \mathbf{r} - \alpha_k A\mathbf{p} \) | L306 `cublasSaxpy` | cuBLAS |
| \( r_0 \leftarrow r_1 \)（旧残差平方） | L308 `cudaMemcpyAsync` D2D | runtime |
| \( r_1 \leftarrow \mathbf{r}\cdot\mathbf{r} \) | L312 `cublasSdot` | cuBLAS |

注意「点积结果除以另一个点积结果」这类**标量间的运算**：两个操作数都在显存里（`d_r1`、`d_dot`），如果搬回主机算，CPU 就得插进迭代的数据流——这正是 graph 要消灭的东西。所以本例写了两个单线程 kernel 把除法和取负放在 GPU 上做。

#### 4.1.3 源码精读

矩阵生成——对角占优的来源是 `+ 10.0f`：[TaskGraph/conjugateGradientCudaGraphs.cu:57-87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L57-L87) 中的 `genTridiag` 用 `rand()` 填充 CSR 三数组（`I` 行指针、`J` 列索引、`val` 值），对角元是 `(float)rand()/RAND_MAX + 10.0f`，次对角通过 `val[start] = val[start-1]` 复制成对称。综合实践里我们会把这两处 `10.0f` 改小来人为增加迭代次数。

两个标量 kernel——函数体只有一条语句，靠 `if (gid == 0)` 保证只有一个线程写：[TaskGraph/conjugateGradientCudaGraphs.cu:98-110](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L98-L110) 定义了 `r1_div_x`（除法）和 `a_minus`（取负）。它们每次迭代处理的数据只有一个 float，是「碎任务」的极端形态。

问题规模与迭代控制：[TaskGraph/conjugateGradientCudaGraphs.cu:115-116](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L115-L116) 定义 `tol = 1e-5f`、`max_iter = 10000`；[TaskGraph/conjugateGradientCudaGraphs.cu:146-147](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L146-L147) 设定 `N = 1048576`、`nz = (N-2)*3+4`。

向量初始化 kernel 用了 grid-stride loop（u2-l2 讲过的 cyclic 分布），block 大小由 occupancy API 自动推荐：[TaskGraph/conjugateGradientCudaGraphs.cu:89-96](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L89-L96)，配套的 `cudaOccupancyMaxPotentialBlockSize` 调用在 [TaskGraph/conjugateGradientCudaGraphs.cu:219-221](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L219-L221)。

进入循环前的准备（初始化残差）：[TaskGraph/conjugateGradientCudaGraphs.cu:232-246](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L232-L246) 依次执行 SpMV（\( A\mathbf{x}_0 \)）、`cublasSaxpy`（\( \mathbf{r} = \mathbf{b} - A\mathbf{x}_0 \)）、`cublasSdot`（\( r_1 = \mathbf{r}\cdot\mathbf{r} \)），全部排在 `stream1` 上。

第 1 次迭代（k=1）走的是「逐个提交」路径：[TaskGraph/conjugateGradientCudaGraphs.cu:248-275](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L248-L275)。注意它与后续迭代有一个算法差异：CG 的第一次方向直接取 \( \mathbf{p}_1 = \mathbf{r}_0 \)（没有 \( \beta \) 项），所以这里用 `cublasScopy` 而不是 `Sscal + Saxpy` 组合。这段代码也因此成为「非 graph 路径长什么样」的天然参照。

#### 4.1.4 代码实践

**实践目标**：数出「一次迭代」包含的主机端 API 调用数，为后面估算 graph 的节省量建立事实基础。

**操作步骤**（需要 GPU，待本地验证）：

1. 进入 `TaskGraph` 目录，`make` 编译，运行 `./conjugateGradientCudaGraphs`。
2. 观察输出：每行 `iteration = k, residual = ...`，最后一行 `Test Summary: Error amount = ...`。记下总共迭代了多少次（即最后一次打印的 k）。
3. 运行 `nvprof ./conjugateGradientCudaGraphs`，看 **GPU activities** 表的 `Calls` 列：`r1_div_x`、`a_minus` 各出现「迭代次数」次；`dot_kernel`/reduce 类、SpMV 类 kernel 的调用次数也随迭代次数线性增长（库内部 kernel 名以实机输出为准）。

**需要观察的现象**：每次迭代贡献约 3 个手写/标量 kernel 调用 + 一批库内部 kernel 调用；总调用次数 = 单次迭代操作数 × 迭代次数。

**预期结果**：以 [TaskGraph/conjugateGradientCudaGraphs.cu:284-315](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L284-L315) 中被捕获的调用计，一次迭代含 **14 个主机端 API 调用**（3 个手写 kernel、4 个 cuBLAS 向量运算、1 个 cuSPARSE SpMV、2 个 memset、2 个 memcpy；其中 cuBLAS/cuSPARSE 内部还可能再拆出多个 GPU kernel，实际节点数以 nvprof 为准）。若程序迭代 K 次，则非 graph 版本约需 14K 次提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `r1_div_x` 和 `a_minus` 只用 `<<<1,1>>>`（1 个 block、1 个 thread）就够了？用更大的 block 会更快吗？

**答案**：它们处理的输入输出各只有一个 float（如 `d_a[0] = d_r1[0]/d_dot[0]`），单个线程即可完成；多开的线程只会空转（还有 `if (gid==0)` 挡着）。这两个 kernel 的价值不在算力，而在于**把标量运算留在 GPU 上**，让数据流不经过主机。

**练习 2**：`genTridiag` 生成的矩阵为什么对称？指出体现对称性的那一行代码。

**答案**：下三角元素直接复用上三角已生成的值——[TaskGraph/conjugateGradientCudaGraphs.cu:78](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L78) 的 `val[start] = val[start - 1]` 把第 i 行的次对角元（连接 i-1）设成第 i-1 行超对角元（连接 i）的值，即 \( A_{i,i-1} = A_{i-1,i} \)。

**练习 3**：第 1 次迭代（L248-275）为什么用 `cublasScopy` 而后续迭代用 `cublasSscal + cublasSaxpy` 组合？

**答案**：CG 首轮没有历史方向向量，\( \mathbf{p}_1 = \mathbf{r}_0 \) 直接整体拷贝；从第二轮起要做 \( \mathbf{p}_{k+1} = \beta_k\mathbf{p}_k + \mathbf{r}_{k+1} \)，是「缩放旧向量再累加」，对应 `Sscal`（乘 β）加 `Saxpy`（加 r）两步。首轮被单独放在捕获区间之外，捕获的模板从第二轮的形态开始。

### 4.2 流捕获：把 stream 当记录仪

#### 4.2.1 概念说明

stream capture（流捕获）是构建 CUDA Graph 最常用的方式。回忆 u3-l3：stream 是按入队顺序执行的任务队列。捕获模式复用了这个「顺序记录」语义——在 `cudaStreamBeginCapture` 与 `cudaStreamEndCapture` 之间，向这条流提交的操作**不会真正执行**，而是被记录成一张有向无环图（DAG）：节点是 kernel/memcpy/memset，边是流顺序隐含的依赖关系。捕获结束后，这条流恢复正常工作模式。

这解释了一个初学者常见的困惑：**捕获区间内的代码一行都不会跑**。k=2 那次迭代并没有在捕获时执行，它是在图中第一次被 `cudaGraphLaunch` 时才真正执行的。

#### 4.2.2 核心流程

```text
cudaStreamBeginCapture(stream1, cudaStreamCaptureModeGlobal)
        │  此后 stream1 上的操作只被记录、不执行
        ▼
    （14 个操作按序入队：kernel / cublas / cusparse / memset / memcpy）
        │  流顺序 → 图的依赖边，形成一条 14 节点的链
        ▼
cudaStreamEndCapture(stream1, &initGraph)   →  得到 cudaGraph_t
```

捕获模式参数 `cudaStreamCaptureModeGlobal` 表示「全局捕获模式」：捕获期间**任何线程**发起的可能不安全的 CUDA API 调用都会被驱动检查并报错，是最保守安全的选项（另有 `ThreadLocal`、`Relaxed` 两种放宽模式）。

#### 4.2.3 源码精读

捕获的开启与结束：[TaskGraph/conjugateGradientCudaGraphs.cu:282](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L282) 调用 `cudaStreamBeginCapture(stream1, cudaStreamCaptureModeGlobal)`，[TaskGraph/conjugateGradientCudaGraphs.cu:317](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L317) 调用 `cudaStreamEndCapture(stream1, &initGraph)` 把记录收进 `cudaGraph_t initGraph`（L278 声明）。两行之间的 L284-315 就是「一次迭代」的全部 14 个操作（见 4.1.2 的映射表）。

捕获区间内值得注意的三个细节：

1. 库调用照常写：[TaskGraph/conjugateGradientCudaGraphs.cu:286](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L286) 的 `cublasSscal`、[TaskGraph/conjugateGradientCudaGraphs.cu:293-295](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L293-L295) 的 `cusparseSpMV` 等，因为 handle 已经通过 `cublasSetStream`/`cusparseSetStream` 绑到 `stream1`（L280-281），它们排入的 kernel 全部被捕获——graph 不区分「你写的 kernel」和「库发的 kernel」。
2. 两次 `cudaMemsetAsync`（[L297](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L297) 清零 `d_dot`、[L310](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L310) 清零 `d_r1`）是给设备指针模式下的点积输出做初始化，属于「满足库实现约定的操作」，同样必须进图，否则重放时点积会累加上一轮的旧值。
3. 捕获区间内也出现了纯主机配置调用 `cublasSetPointerMode`（[L285、L287、L289](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L285-L289)）——它不产生 GPU 工作、不成为图节点，只影响紧随其后的库调用如何解释标量参数（详见 4.4）。

一条 D2H 拷贝也被画进了图：[TaskGraph/conjugateGradientCudaGraphs.cu:314-315](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L314-L315) 把 `d_r1` 拷回主机变量 `r1`。图记录的是**主机地址 `&r1`**，于是每次重放都会把最新残差写到同一个地址——主机循环条件因此能拿到每轮的 \( \mathbf{r}\cdot\mathbf{r} \)（代价是每次 launch 后要同步一次，见 4.3）。

#### 4.2.4 代码实践

**实践目标**：把捕获区间画成一张节点依赖图，并核对「14 个操作」的账。

**操作步骤**（纯源码阅读，无需 GPU）：

1. 打开 [TaskGraph/conjugateGradientCudaGraphs.cu:277-320](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L277-L320)，从 L284 到 L315 逐行给每个「产生 GPU 工作的调用」编号（跳过 `cublasSetPointerMode`/`cusparseSetPointerMode` 这类纯配置调用）。
2. 在纸上画 14 个节点排成一条链，箭头方向即流顺序。
3. 对照 4.1.2 的 CG 公式表，在每个节点旁标注它实现的数学含义（β → p → Ap → dot → α → x → r → r1）。
4. 思考题式检查：如果 L297 的 `cudaMemsetAsync(d_dot, 0, ...)` 被删掉，图重放到第 3 次迭代时 `d_dot` 里是什么？（答案见练习 2。）

**需要观察的现象**：14 个节点首尾相接构成一条链，没有任何分支——因为全部操作排在同一条流上，依赖关系是纯线性的。

**预期结果**：得到一张 14 节点线性 DAG；这条链正是 graph 相比逐个提交「省掉 13 次 API」的实体。多流交叉形成的真分支 DAG 属于进阶话题（u3-l3 的 join 模式若被捕获就会产生分叉），本例没有出现。

#### 4.2.5 小练习与答案

**练习 1**：捕获期间往 `stream1` 提交的 kernel 会执行吗？`stream1` 上排在 `BeginCapture` **之前**的还没执行完的工作会怎样？

**答案**：捕获期间的提交只记录、不执行。捕获开始前已入队的工作不受影响，仍会正常执行——`BeginCapture` 只改变之后提交物的去向；不过本例在捕获前有 `cudaStreamSynchronize`（L272）把流排干了，所以捕获时流是空的。

**练习 2**：删掉 L310 的 `cudaMemsetAsync(d_r1, 0, ...)` 后，重放到第二轮会发生什么？

**答案**：`cublasSdot` 在设备指针模式下把点积结果写进/累加到 `d_r1` 所指的 4 字节。不先清零，`d_r1` 里残留上一轮的旧 \( \mathbf{r}\cdot\mathbf{r} \)，新一轮结果会被污染（具体行为取决于库实现是「写入」还是「累加」，但依赖未初始化缓冲本身就是未定义行为）。这就是为什么满足库约定的 memset 必须一起进图。

**练习 3**：README 列出的 API 清单里有 `cudaGraphCreate`，源码里用到了吗？

**答案**：没有。[TaskGraph/README.md:25-26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/README.md#L25-L26) 列出的是 CUDA Graph 全家桶（`cudaGraphCreate` 用于手动逐节点建图），而本源码走的是 stream capture 路线，图由 `cudaStreamEndCapture` 生成，不需要 `cudaGraphCreate`。这是「README 是模板化文档、阅读时须回源码核对」的又一例（呼应 u1-l1 的文档核验方法论）。

### 4.3 graph 实例化与启动：一次 API 提交整张图

#### 4.3.1 概念说明

捕获得到的 `cudaGraph_t` 只是「图纸」，还不能直接运行。`cudaGraphInstantiate` 把图纸编译成可执行的**图实例**（`cudaGraphExec_t`）——驱动在实例化时完成节点调度、内存与依赖的整理，这一步有一次性成本，但只付一次。之后每次 `cudaGraphLaunch(graphExec, stream)` 把整张图当作一个任务排进流，一条 API 携带全部 14 个操作。

设一次迭代含 \( n \) 个流操作（本例 \( n = 14 \)），程序迭代 \( K \) 次，单次 API 提交平均开销 \( t_{sub} \)，graph 启动开销 \( t_{gl} \)，则两种方式的 CPU 侧提交成本约为：

\[
T_{\text{逐个}} \approx K \cdot n \cdot t_{sub}, \qquad
T_{\text{graph}} \approx K \cdot t_{gl} + t_{\text{instantiate}}
\]

节省的提交次数是 \( K(n-1) \)。以 K = 25、n = 14 计，约省去 325 次 API 往返；K 越大收益越大（`max_iter` 上限 10000，病态矩阵下收益可达上万次提交）。

但要诚实地提醒（承接 u1-l4 的口径意识）：本例每个向量运算都在百万级元素上执行，单个 kernel 往往几十微秒以上，CPU 提交开销大概率**隐藏在 GPU 执行之下**，所以端到端 wall time 的改善可能有限；graph 带来的差异在 **CPU 侧 API 时间**上最直接可见——这正是实践任务要用 nvprof 的 API calls 表来对比的原因。graph 的收益在 kernel 越小、越碎的场景（如深度学习推理的小算子流）越显著。

#### 4.3.2 核心流程

```text
捕获完成后：
cudaGraphInstantiate(&graphExec, initGraph, ...)   一次性：图纸 → 可执行实例
        ▼
while (r1 > tol*tol && k <= max_iter)      主机判断收敛（需要每轮拿到 r1）
    cudaGraphLaunch(graphExec, streamForGraph)     一条 API 提交 14 个操作
    cudaStreamSynchronize(streamForGraph)          等 D2H 拷贝把 r1 写回主机
        ▼
使用完毕：cudaGraphExecDestroy + cudaGraphDestroy
```

注意收敛判断 `while (r1 > tol*tol ...)` 留在主机——它依赖每轮迭代结束时的 \( \mathbf{r}\cdot\mathbf{r} \)，而图是静态的，无法把「带数据依赖的循环退出条件」画进图里，所以主机必须每轮同步一次读取 `r1`。这是 stream capture 式 graph 应用的典型折中：图管迭代体，主机管控制流。

#### 4.3.3 源码精读

实例化：[TaskGraph/conjugateGradientCudaGraphs.cu:317-319](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L317-L319)——`cudaStreamEndCapture` 产出 `initGraph` 后，`cudaGraphInstantiate(&graphExec, initGraph, NULL, NULL, 0)` 生成 `graphExec`（这是 CUDA 11 时代的五参数旧签名；CUDA 12 起推荐 `cudaGraphInstantiateWithFlags`）。

主循环的 graph 路径：[TaskGraph/conjugateGradientCudaGraphs.cu:325-328](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L325-L328)。`while (r1 > tol * tol && k <= max_iter)` 判断收敛，体内只有两条调用：`cudaGraphLaunch(graphExec, streamForGraph)` 与 `cudaStreamSynchronize(streamForGraph)`。`streamForGraph` 是专门为重放新建的流（[L279](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L279) 创建）——捕获用的 `stream1` 只是「记录介质」，图启动到哪条流都可以。

对照分支——非 graph 的逐个提交路径：[TaskGraph/conjugateGradientCudaGraphs.cu:329-357](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L329-L357)（`#else` 分支）把同样 14 个操作在循环体里逐个重新提交，末尾 `cudaStreamSynchronize(stream1)`。两条路径由编译期宏 `WITH_GRAPH` 切换（[TaskGraph/conjugateGradientCudaGraphs.cu:52-54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L52-L54) 默认定义为 1）——这是微基准「控制变量」思想的又一体现：同一文件，只翻一个开关。

收尾：结果向量经 `streamForGraph` 拷回主机（[TaskGraph/conjugateGradientCudaGraphs.cu:363-366](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L363-L366)），主机串行重算每行内积得到最大误差（[L373-387](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L373-L387)，呼应 u2-l4 的「串行基线校验」），随后按序销毁 `graphExec`、`graph`、流（[L390-392](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L390-L392)），退出码反映是否在 `max_iter` 内收敛（[L417](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L417)）。

编译侧：[TaskGraph/Makefile:302](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L302) 静态链接 `-lcublas_static -lcublasLt_static -lcusparse_static -lculibos`；[TaskGraph/Makefile:274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L274) 以 `-I../Common` 引入 helper 头；[TaskGraph/Makefile:281-284](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L281-L284) 的 `SMS` 默认最多到 75（未含 80/86，但 L296-298 会为最高架构生成 PTX 以兼容新卡；README 明确要求 CUDA Toolkit 11.1，见 [TaskGraph/README.md:33](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/README.md#L33)）。

#### 4.3.4 代码实践

**实践目标**：亲手编译出 graph 版与非 graph 版两个可执行文件（同一源码、只差一个宏），为综合实践的对比实验备好材料。

**操作步骤**（编译无需 GPU，运行需要，待本地验证）：

1. graph 版（默认）：`cd TaskGraph && make`（[TaskGraph/README.md:48-53](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/README.md#L48-L53) 的标准流程）。
2. 非 graph 版：利用 Makefile 的透传变量（[TaskGraph/Makefile:262](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L262) 把 `EXTRA_NVCCFLAGS` 追加进编译 flags）：

   ```bash
   make clean
   make EXTRA_NVCCFLAGS="-DWITH_GRAPH=0"
   mv conjugateGradientCudaGraphs ../cg_nograph   # 改名保存，避免被下次构建覆盖
   make clean && make                             # 再编回 graph 版
   ```

   若不想用 Makefile（它默认还静态链接、多架构编译，较慢），也可以直接：

   ```bash
   nvcc -I../Common -DWITH_GRAPH=0 -O3 -o cg_nograph conjugateGradientCudaGraphs.cu \
        -lcublas -lcusparse
   ```

3. 分别运行两个版本，确认输出的迭代序列与 `Error amount` 一致（控制变量：两条路径算的是同一个数学问题，结果应基本相同，浮点求和顺序差异可能带来末位微小差别）。

**需要观察的现象**：两版打印的 `iteration = k, residual = ...` 序列几乎一致；`Error amount` 为同一个很小的数。

**预期结果**：两个可执行文件行为等价，仅提交方式不同。若 `EXTRA_NVCCFLAGS` 透传不生效（不同版本 Makefile 行为差异），退回手敲 `nvcc` 命令即可——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`cudaGraph_t`（`initGraph`）和 `cudaGraphExec_t`（`graphExec`）是什么关系？可以只保留后者、提前销毁前者吗？

**答案**：前者是图的结构描述（图纸），后者是由它实例化出的可执行对象。实例化完成后图纸仍被 sample 保留到程序末尾才销毁（L391），但从 API 语义上讲，`graphExec` 的生命周期不依赖 `initGraph`，实例化后即可销毁 `initGraph`（保持引用便于调试或再实例化时才需要保留）。

**练习 2**：为什么每次 `cudaGraphLaunch` 之后都要跟一句 `cudaStreamSynchronize(streamForGraph)`？去掉它行不行？

**答案**：不行。收敛条件 `r1 > tol*tol` 在主机上求值，而 `r1` 的值由图末尾的 D2H 拷贝（L314-315）异步写回；不同步就读 `r1`，读到的是上一轮（甚至未定义）的值，循环会错乱。同步是「图管迭代体、主机管控制流」这一分工的必然代价。

**练习 3**：按 K = 25 次迭代、每次迭代 14 个操作估算，graph 版比逐个提交版少多少次主机 API 调用？若 `max_iter` 跑满 10000 呢？

**答案**：节省 \( K(n-1) \)：25 × 13 = 325 次；跑满 10000 次时节省 10000 × 13 = 130000 次。再加上实例化只发生一次，迭代越多，被摊薄的单次成本越低。

### 4.4 让图可以自洽重放：pointer mode 与单线程标量 kernel

#### 4.4.1 概念说明

graph 是**静态的**：节点参数在捕获那一刻就固化了，重放不会重新读主机变量。可 CG 每轮的 \( \alpha_k \)、\( \beta_k \) 都在变——如果这些标量留在主机，图就废了。本例的解法是让**标量数据流在 GPU 上闭环**：

- cuBLAS 默认的 pointer mode 是 `CUBLAS_POINTER_MODE_HOST`：标量参数（如 SAXPY 的 α）按值从主机内存读取，捕获时**数值**被固化进图。对常量（α = 1.0）这没问题；对每轮变化的量就错了。
- 切到 `CUBLAS_POINTER_MODE_DEVICE` 后，标量参数按**设备地址**解释：图记录的是 `d_b`、`d_a`、`d_na` 这些指针。指向的显存内容每轮被 `r1_div_x`、`a_minus` 重新生产，于是重放时库自动拿到新值——数据依赖被画进了图，主机彻底退出迭代体。

配合图末尾那条把 `r1` 写回主机的 D2H 拷贝，整个迭代形成闭环：**输入旧残差 → 图内算出新残差 → 回传主机判断是否继续**。

#### 4.4.2 核心流程

```text
图内标量的生产与消费（每轮重放自动更新）：

  d_r1, d_r0 ──r1_div_x──▶ d_b (β)  ──Sscal(DEVICE 模式)──▶ 缩放 p
  d_r1, d_dot ─r1_div_x──▶ d_a (α)  ──Saxpy(DEVICE 模式)──▶ 更新 x
       d_a ────a_minus───▶ d_na(-α) ──Saxpy(DEVICE 模式)──▶ 更新 r
  （常量 α=1.0 仍走 HOST 模式，数值固化无妨）
```

#### 4.4.3 源码精读

pointer mode 的三次切换：[TaskGraph/conjugateGradientCudaGraphs.cu:285-289](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L285-L289)——`cublasSscal`（吃 `d_b`）与后面的 `cublasSaxpy`（吃 `d_a`）处于 DEVICE 模式；唯独 L288 的 `cublasSaxpy(cublasHandle, N, &alpha, d_r, 1, d_p, 1)` 临时切回 HOST 模式，因为它的 α 恒为 1.0（[L232](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L232) 初始化），常量固化进图完全正确。程序开头计算初始残差时也有一处 DEVICE 模式的 `cublasSdot`（[L244-246](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L244-L246)），让点积结果直接落在 `d_r1` 而不是主机栈上。

消费这些设备标量的调用：[TaskGraph/conjugateGradientCudaGraphs.cu:286](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L286) `cublasSscal(cublasHandle, N, d_b, d_p, 1)`（p ×= β）、[L302](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L302) `cublasSaxpy(..., d_a, d_p, 1, d_x, 1)`（x += α·p）、[L306](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L306) `cublasSaxpy(..., d_na, d_Ax, 1, d_r, 1)`（r −= α·Ap）——标量参数全是设备指针。cuSPARSE 侧同理有 `cusparseSetPointerMode`（[L291-292](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L291-L292)），本例 SpMV 的 α/β 恰是主机常量（1 与 0），故设为 HOST 模式。

7 个专用标量缓冲全部单独 `cudaMalloc`（每个 4 字节）：[TaskGraph/conjugateGradientCudaGraphs.cu:185-191](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L185-L191)（`d_r1、d_r0、d_dot、d_a、d_na、d_b`）。它们就是图的「寄存器」：跨重放持久、图内读写。

#### 4.4.4 代码实践

**实践目标**：体会在 GPU 上做标量运算的成本量级，理解「两个 `<<<1,1>>>` kernel 换来主机完全退出迭代体」这笔交易。

**操作步骤**（需要 GPU，待本地验证）：

1. 运行 `nvprof ./conjugateGradientCudaGraphs`，在 GPU activities 表中找到 `r1_div_x` 与 `a_minus` 两行。
2. 记录它们的调用次数（应各等于迭代次数）与单次耗时（通常 1~3 µs 量级——几乎全是启动开销本身）。
3. 思考核对：这两个 kernel 每次各只算 1 个 float 除法/取负，却每次迭代要付 2 次提交成本；对比把标量搬回主机计算（每轮多 2 次 D2H + 2 次 H2D + 主机介入，且无法进图），权衡是否划算。

**需要观察的现象**：`r1_div_x`、`a_minus` 的单次耗时与最简单的空 kernel 同量级，即提交开销主导，计算时间可忽略。

**预期结果**：确认「用极小的 GPU kernel 换取数据流不落地主机」是 graph 化改造的标准手法；在 graph 版里这两个 kernel 的提交成本已被整图 launch 摊薄。

#### 4.4.5 小练习与答案

**练习 1**：如果把 L288 的 `cublasSaxpy(cublasHandle, N, &alpha, d_r, 1, d_p, 1)` 也放在 DEVICE 模式下，会发生什么？

**答案**：`&alpha` 是主机地址，DEVICE 模式会把它当作设备指针去读——轻则读到错误值（非法地址/垃圾数据），重则内核访问越界报错。HOST/DEVICE 模式必须与实参指针的种类严格匹配，这也是 u2-l3「`cudaMemcpy` 方向参数必须与指针匹配」同类的纪律。

**练习 2**：图末尾的 D2H 拷贝写到主机栈变量 `r1` 的地址。这个地址在图重放期间必须保持有效吗？

**答案**：必须。图记录的是缓冲地址，每次重放都往同一地址写。本例 `r1` 是 main 的局部变量、生命周期覆盖整个循环，所以安全；若捕获后该地址被释放或复用（比如捕获栈帧返回），重放会写坏无关内存。同理，捕获期间引用的任何主机/设备缓冲在图存活期间都不能挪动或释放。

**练习 3**：为什么说 `d_r0 ← d_r1`（L308 的 D2D 拷贝）必须在**图内**完成，而不能在图外的主机循环里做？

**答案**：`d_r0` 保存上一轮的 \( \mathbf{r}\cdot\mathbf{r} \) 供下一轮算 β。若放到图外，主机每轮要额外提交一次 memcpy（削弱 graph 的意义），而且时序上必须插在两次 launch 之间，主机又被卷进迭代体。放进图内则随每次重放自动执行，顺序由依赖边保证。

## 5. 综合实践

**任务**：完成讲义规格中的核心实验——定量测量 CUDA Graph 在本基准上的提交开销节省。整个过程分四步，需要 NVIDIA GPU 与 CUDA 11+ 工具链，本讲义撰写环境无法运行，全部**待本地验证**。

### 第 1 步：数清楚「图里有什么、launch 了多少次」

1. 在 [TaskGraph/conjugateGradientCudaGraphs.cu:325-328](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L325-L328) 确认：`cudaGraphLaunch` 位于 while 循环体内，每迭代执行 1 次；循环次数 = 程序输出中最后一行 `iteration = k` 的 k（k 从 2 起，因为 k=1 在捕获前已完成）。
2. 用 4.1.2 的表核对每次迭代的主机 API 调用数 \( n = 14 \)（3 kernel + 4 cuBLAS 向量运算 + 1 cuSPARSE + 2 memset + 2 memcpy）。
3. 计算节省量：若程序打印的最后迭代号为 K，则逐个提交路径约需 \( 14 \times (K-1) \) 次流操作提交，graph 路径只需 \( K-1 \) 次 `cudaGraphLaunch` + 1 次实例化，节省约 \( 13 \times (K-1) \) 次提交。把这三个数字记入实验记录表。

### 第 2 步：把迭代次数翻倍

收敛条件 `r1 > tol*tol` 依赖矩阵条件数，改 `max_iter` 没用（收敛就退出）。有效做法是削弱对角占优：把 [TaskGraph/conjugateGradientCudaGraphs.cu:59](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L59) 与 [TaskGraph/conjugateGradientCudaGraphs.cu:79](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/conjugateGradientCudaGraphs.cu#L79) 两处的 `+ 10.0f` 同时改为 `+ 2.0f`（注意两处都要改，保持对称结构），重新编译运行，观察 `iteration = k` 的终值是否明显增大。若仍不足翻倍，继续调小（如 `+ 1.5f`）；若想精确控制迭代数，也可以把 while 条件临时改成固定次数的 for 循环（实验后改回）。

### 第 3 步：nvprof 对比 API 时间

按 4.3.4 的方法准备两个可执行文件（`WITH_GRAPH=1` 与 `=0`），分别执行：

```bash
nvprof ./conjugateGradientCudaGraphs 2> graph.prof.txt
nvprof ./cg_nograph                  2> nograph.prof.txt
```

（nvprof 的 profiler 输出走 stderr；CUDA 11 之后若提示 nvprof 已弃用，可用 `nsys profile` + `nsys stats --report cuda_api_sum` 得到等价的 API 汇总。）

对比两份输出的 **API calls** 段：

| 观察项 | graph 版预期 | 非 graph 版预期 |
| --- | --- | --- |
| `cudaGraphLaunch` 调用次数 | = 迭代次数 | 不出现 |
| `cublasSaxpy` 等 API 调用次数 | 仅初始化阶段几次 | ≈ 4 × 迭代次数 |
| `cudaLaunchKernel` 总次数 | 仅初始化阶段 | ≈ 3 × 迭代次数 |
| CPU API 总时间占比 | 低 | 明显更高 |

同时对比两版的 **GPU activities** 总时间——预期基本持平（同样的 kernel 在跑），这正是 4.3.1 所说「wall time 未必大变、API 时间才是 graph 的主战场」的验证点。

### 第 4 步：写一段结论

用第 1 步的节省公式、第 3 步的实测数据，回答三个问题：(1) graph 把多少次提交合并成了多少次？(2) 端到端时间变了吗，为什么？(3) 若把 N 从 \( 2^{20} \) 缩到 \( 2^{12} \)（每个 kernel 变小、碎任务特征放大），你预期两版差距如何变化？——第 (3) 问可作为下一个实验的假设，动手验证它。

## 6. 本讲小结

- **CUDA Graph 三步工作流**：`cudaStreamBeginCapture` 让流从「执行队列」变成「记录仪」→ `cudaStreamEndCapture` 产出图纸（`cudaGraph_t`）→ `cudaGraphInstantiate` 编译为可执行实例（`cudaGraphExec_t`）→ 循环中一条 `cudaGraphLaunch` 整图提交。
- **适用场景**：同一串操作反复提交。本例每次迭代 14 个主机 API 调用合并为 1 次图启动，节省 \( K(n-1) \) 次提交；kernel 越碎、迭代越多，收益越大。
- **捕获语义**：捕获区间内的操作（含 cuBLAS/cuSPARSE 库调用产生的 kernel）不执行、只记录；流顺序变成图的依赖边；本例 14 个节点构成线性链。
- **自洽重放的关键设计**：随迭代变化的标量（α、β）必须留在 GPU 上——`CUBLAS_POINTER_MODE_DEVICE` 让库从 `d_a`/`d_b` 等设备标量缓冲读参数，`r1_div_x`/`a_minus` 两个 `<<<1,1>>>` kernel 在图内生产它们；仅有的回传量（残差平方）由图末 D2H 拷贝写到固定主机地址。
- **分工与代价**：图管迭代体，主机管控制流——每次 launch 后必须 `cudaStreamSynchronize` 才能读到新一轮残差做收敛判断。
- **测量口径**（承接 u1-l4）：本例 kernel 不算太小，graph 的收益主要体现在 nvprof 的 API calls 时间而非端到端 wall time；下结论前先分清测的是哪种时间。

## 7. 下一步学习建议

本讲完成后，单元三（充分利用 GPU 大规模并行）四个基准就齐了：WarpDivRedux（分支发散）、DynParallel（设备端启动）、Conkernels（多流并发）、TaskGraph（图提交）。三条技术线索对比着记忆：**并发**解决「GPU 空闲」，**动态并行**解决「负载未知时谁来生成工作」，**graph** 解决「CPU 提交太慢」。

接下来的学习路线：

1. **进入单元四（GPU 存储层次）**：推荐从 [u4-l1 共享内存分块矩阵乘](u4-l1-shared-memory-tiling-mm.md) 开始——CG 里的 SpMV 是典型的访存受限操作，正好衔接。
2. **横向延伸**：如果对「迭代法 + 库调用」的形态感兴趣，CUDA Samples 里还有非 graph 版的 conjugateGradient 样本（本仓库未包含，可在 NVIDIA 官方 samples 仓库对照），比较两者的循环体即可看出 graph 化改造的最小改动集：pointer mode + 标量 kernel + 捕获三件套。
3. **方法论沉淀**：把本讲的估算公式 \( K(n-1) \) 次提交节省与「API 时间 vs wall time」的口径讨论，记入你自己的 kernel 优化检查清单——到 [u6-l1 归约优化阶梯](u6-l1-reduction-optimization-ladder.md) 和 [u6-l2 设计自己的微基准](u6-l2-design-your-own-microbenchmark.md) 时，你会需要用同样的方法为自己的基准设计对照实验。
