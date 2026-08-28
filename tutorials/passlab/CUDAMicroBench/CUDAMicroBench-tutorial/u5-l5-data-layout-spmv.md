# MiniTransfer（SpMV 篇）：数据布局如何决定无谓传输量

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 MiniTransfer_SpMV 要演示的反模式——**数据布局不当导致 CPU–GPU 之间搬运大量从未被用到的数据**——以及它对应的优化思路。
2. 对同一个稀疏矩阵-向量乘（SpMV），分别从**传输量**（搬多少字节）和**计算量**（做多少次运算）两个轴，定量比较四种布局：全矩阵（dense）、CSR 压缩、统一内存＋显式索引（unified）、带行计数的统一内存（unified_count）。
3. 手推「CSR 显式传输字节数 < dense」的稀疏度阈值，理解**稀疏格式只在真稀疏时才省**。
4. 读懂本基准的源码，并能指出几处真实的实现缺陷（未接线的 kernel、越界写、计时口径不对称）——这也是微基准阅读的必修课：**源码的意图与实现是两回事**。

## 2. 前置知识

**SpMV（稀疏矩阵-向量乘）**。给定 \( N \times N \) 矩阵 \( A \) 与向量 \( x \)，计算 \( y = A x \)，即 \( y_i = \sum_j A_{ij} x_j \)。它是科学计算中最常见的内核之一。当 \( A \) 的非零元素只有 \( Z \) 个（\( Z \ll N^2 \)）时称 \( A \) 稀疏，**密度**定义为：

\[
\rho = \frac{Z}{N^2}
\]

**三种存储布局**（u4-l3 在 SpMM 中详细讲过 CSR/CSC，这里只复习要点）：

| 布局 | 存储内容 | 大小 | 特点 |
|---|---|---|---|
| dense（行主序） | 全部 \( N^2 \) 个元素（含零） | \( 4N^2 \) 字节 | 零也搬、零也算（或至少也要读一遍判断） |
| CSR（按行压缩） | 每行非零的**值**＋**列号**，外加每行起始偏移 | \( 8Z + 4(N+1) \) 字节 | 只存非零，但每个非零要附带 4 字节索引 |
| COO 索引表（本讲 unified 用） | 每个非零的**行号**＋**列号**，值仍留在原矩阵里 | \( 8Z \) 字节索引 | 只把「在哪里有非零」显式搬给 GPU，值不显式搬 |

**统一内存（UM）与按页迁移**。u5-l4 已经讲过：`cudaMallocManaged` 分配的缓冲区 CPU、GPU 都能直接访问，数据并不在 `cudaMallocManaged` 时搬运，而是在 kernel **触碰到的页**上按需缺页迁移，迁移粒度是页（典型 4KB = 1024 个 float）。本讲的 unified 路径正是把「值」放在 UM 里、只显式拷贝索引表，让值「留在原地、用到哪页搬哪页」。

**传输量 ≠ 时间**。u1-l4 与 u2-l4 反复强调：这些基准的 wall time 包含每次调用的 `cudaMalloc`/`cudaFree`、多条小拷贝与主机开销。传输量给出的是**流量下界**，不是端到端时间的预言。本讲会同时给出两条轴，并明确区分。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [MiniTransfer_SpMV/SpMV_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c) | host 主程序：构造测试矩阵、生成 CSR/索引表、串行基线、驱动四条实验路径并打印 CSV | `init_matrix` 如何精确控制非零个数；参数顺序 |
| [MiniTransfer_SpMV/SpMV_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu) | 5 个 kernel ＋ 4 个主机包装函数（分配、拷贝、启动、释放、计时） | 四条路径各自搬了什么；哪个 kernel 真正被启动 |
| [MiniTransfer_SpMV/SpMV.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV.h) | 接口契约：`REAL=float`、extern "C" 声明 | 精度与混编 |
| [MiniTransfer_SpMV/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/test.sh) | 实验设计：固定 `num_rows=10240`，nnz 从 \( 2^{26} \) 到 8 逐点减半 | 稀疏度扫描 |
| [MiniTransfer_SpMV/SpMV_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.output.carina.txt) | Carina 集群上的归档终端转录 | 跨版本解读归档数据 |
| [MiniTransfer_SpMV/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/Makefile) | 单行式：`nvcc -o SpMV_cuda SpMV_cuda.c SpMV_cudakernel.cu` | 无特殊架构参数，`make` 即可 |

## 4. 核心概念与源码讲解

### 4.1 问题设定：可控稀疏度的测试矩阵与四条实验路径

#### 4.1.1 概念说明

README 把 MiniTransfer 的反模式概括为「错误的数据布局导致 CPU 与 GPU 之间大量无谓传输」，优化技术是「换成正确的布局避免无谓传输」。本基准把这个抽象命题做成一个可测的实验：**同一个矩阵、同一个 SpMV，用四种数据布局各做一遍**，每条路径都完整走「host 格式准备 → cudaMalloc → H2D → kernel → D2H → free」的流程并计 wall time，最后只改一个变量——**布局**。

关键在于实验矩阵的构造方式：非零元素的**个数**由命令行精确指定、位置均匀随机散布。这样命令行参数就直接控制了密度 \( \rho \)，`test.sh` 的逐点减半扫描就是对 \( \rho \) 的扫描。

#### 4.1.2 核心流程

`main` 的执行流程：

```text
解析命令行：argv[1] → nnz（非零个数），argv[2] → num_rows（矩阵阶数 N）
构造 N×N 稠密矩阵 matrix，其中恰好 nnz 个位置非零（随机散布），值 = drand48()+1
构造 CSR 三数组（ptr / indices / data）
串行基线 spmv_csr_serial 计算 y（不计时，本版 check 输出已被注释）
四条实验路径，每条先 warmingup 一次、再计时 num_runs=5 次取平均：
  1) spmv_cuda_dense_discrete        → y_dense
  2) spmv_cuda_csr_discrete          → y_csr
  3) spmv_cuda_unified               → y_unified
  4) spmv_cuda_unified_count         → y_unified_count
打印一行 CSV：nnz, dense时间, csr时间, unified时间, unified_count时间
```

#### 4.1.3 源码精读

参数解析——注意第一个参数是 nnz、第二个才是矩阵阶数，与多数基准「先规模后其他」的习惯相反，读 `test.sh` 时容易搞反：

[MiniTransfer_SpMV/SpMV_cuda.c:L152-L157](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L152-L157) —— `argv[1]` 送入 `nnz`，`argv[2]` 送入 `num_rows`。所以 `test.sh` 里的 `./SpMV_cuda 67108864 10240` 表示：10240×10240 的矩阵、恰好 67,108,864 个非零（密度 64%）。

矩阵构造是本模块最精巧的 15 行：

[MiniTransfer_SpMV/SpMV_cuda.c:L41-L61](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L41-L61) —— 先造一个 \( 0..n-1 \)（\( n=N^2 \)）的随机排列 `d`（Fisher–Yates 洗牌），再令每个元素非零当且仅当 `d[位置] < nnz`。因为 `d` 是排列，值小于 `nnz` 的位置**恰好有 nnz 个**——这就是「精确控制非零个数」的技巧；非零值取 `drand48()+1`，加 1 保证标记为非零的元素永远不会真的等于 0。

两个可复现性细节值得注意（对照 u1-l3 讲过的「固定种子可复现」原则，这里只做对了一半）：

- [MiniTransfer_SpMV/SpMV_cuda.c:L171](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L171) 用 `srand48(1<<12)` 固定了**数值**序列；
- [MiniTransfer_SpMV/SpMV_cuda.c:L47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L47) 却用 `srand(time(NULL))` 播种**位置**洗牌——非零位置每次运行都不同。

CSR 与索引表的构造（COO 格式的生成器，供 unified 路径使用）：

[MiniTransfer_SpMV/SpMV_cuda.c:L63-L85](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L63-L85) —— `init_csr` 按行扫描稠密矩阵，填出 `data/indices`，并用每行非零个数的累加和得到 `ptr`；注意 [L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L66) 显式写了哨兵 `ptr[num_rows] = nnz`，所以 `ptr` 必须有 \( N+1 \) 个int——这个细节在 4.5 会形成对照。

[MiniTransfer_SpMV/SpMV_cuda.c:L106-L118](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L106-L118) —— `init_index` 生成两个长度为 nnz 的数组：非零元素的行号 `row[]` 与列号 `column[]`。这就是「索引表」：它告诉 GPU 哪里有非零，但不带值。

四条路径的驱动与输出：

[MiniTransfer_SpMV/SpMV_cuda.c:L190-L211](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L190-L211) —— 每条路径先以 `y_warmingup` 为输出跑一次预热（消化首次 `cudaMalloc` 的上下文创建等一次性开销，见 u1-l3/u2-l4），再对 `num_runs = 5`（[L179](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L179)，注意本基准是 5 次平均，不是其他基准常用的 10 次）累计时间；最后 [L211](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L211) 打印 5 列 CSV：`nnz, dense, csr, unified, unified_count`。

顺带两个诚实提醒（不影响计时，但影响「正确性验证」）：

- [MiniTransfer_SpMV/SpMV_cuda.c:L215-L218](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L215-L218) —— 所有 `check()` 输出都被注释掉了，当前版本**没有任何正确性报告**；
- [MiniTransfer_SpMV/SpMV_cuda.c:L120-L130](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L120-L130) —— 串行基线用 `y[row] += dot`，而 `y` 从 `malloc`（[L163](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L163)）之后从未清零（文件头定义的 `zero()` 没有任何调用点）。基线累加的是未初始化内存；好在结果只喂给已注释的 check，缺陷处于休眠状态。

#### 4.1.4 代码实践

1. **实践目标**：跑通一个实验点，确认输出格式与参数语义。
2. **操作步骤**：进入 `MiniTransfer_SpMV` 目录，`make` 编译；执行 `./SpMV_cuda 8192 10240`（密度约 0.0078%），再执行 `./SpMV_cuda 33554432 10240`（密度 32%）。
3. **需要观察的现象**：每次运行只打印一行 5 列 CSV；第一列是 nnz，后四列依次是 dense / csr / unified / unified_count 的平均毫秒数。
4. **预期结果**：两个点的 dense 时间应基本不随 nnz 变化（它搬的总是整个 10240×10240 矩阵，约 419.5 MB）；csr 时间随 nnz 明显变化。若本机无 GPU，改用下面 4.1.5 练习 3 的源码推算方式，并阅读归档输出。
5. **待本地验证**：具体毫秒数依赖机器，本文不预设数值。

#### 4.1.5 小练习与答案

**练习 1**：`test.sh` 固定 `num_rows=10240`、nnz 从 67,108,864 逐点减半到 8。请算出扫描的密度范围，并指出哪个点密度超过 50%。

答案：\( N^2 = 10240^2 = 104{,}857{,}600 \)。最大密度 \( 67{,}108{,}864 / 104{,}857{,}600 = 64\% \)，最小密度 \( 8/104{,}857{,}600 \approx 7.6\times10^{-8} \)。只有第一个点（nnz=67,108,864）密度超过 50%，其余全部低于 50%——`test.sh` 的设计意图正是让绝大多数点落在「真稀疏」区。

**练习 2**：为什么说 `init_matrix` 保证非零个数**恰好**等于 nnz？

答案：`d` 是 \( 0..n-1 \) 的一个排列，其中值小于 nnz 的数恰好有 nnz 个；矩阵每个位置对应唯一的 `d` 值，非零条件又是 `d[位置] < nnz`，所以非零位置恰好 nnz 个，与随机性无关。

**练习 3**（源码推算，无需 GPU）：从 [SpMV_cuda.c:L159-L169](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L159-L169) 看，host 侧共分配了几个 O(N²) 或 O(nnz) 的大数组？N=10240 时各多大？

答案：O(N²) 的有 `matrix`（以及 `init_matrix` 内部临时的 `d`、`A`，见 [L44-L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L44-L45)），各 \( 4N^2 \) = 约 400 MB；O(nnz) 的有 `indices`、`data`。仅 `init_matrix` 一趟就要吃掉约 1.2 GB 主存——实践时如果机器内存紧张，可先减小 N。

### 4.2 spmv_dense：全矩阵布局——传输量被 N² 锁死

#### 4.2.1 概念说明

反模式登场。dense 路径把**整个矩阵**（包括所有零）从主机搬到设备，再在 GPU 上做标准稠密矩阵-向量乘。它的问题有两个：

1. **传输量与 \( N^2 \) 绑定**，与实际非零个数 Z 无关。哪怕矩阵只有 8 个非零，也要搬 \( 4N^2 \) 字节——这就是「无谓传输」（useless transfer）的字面来源。
2. **计算量同样被 \( N^2 \) 绑定**：即使加了「跳过零」的分支，每个零元素也至少要读出来判断一次。

它也是其余三条路径的**对照组**：换了布局之后传输量降多少、时间降多少，都以它为基线。

#### 4.2.2 核心流程

`spmv_cuda_dense_discrete` 包装函数（计时单位：整个调用的 wall time）：

```text
计时开始
cudaMalloc d_x / d_matrix / d_y
H2D: x（4N 字节）＋ matrix（4N² 字节）        ← 流量大头
启动 spmv_dense_check_and_compute<<<256,256>>>
cudaDeviceSynchronize
D2H: y（4N 字节）
cudaFree × 3
计时结束
```

显式传输字节数：

\[
B_{dense} = \underbrace{4N^2 + 4N}_{H2D} + \underbrace{4N}_{D2H} = 4N^2 + 8N
\]

N=10240 时约 **419.5 MB**，且**在整个 nnz 扫描上是一个常数**——这是解读 `test.sh` 结果的钥匙：dense 那一列应当近似水平线。

#### 4.2.3 源码精读

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L81-L108](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L81-L108) —— dense 路径包装函数。两个 `cudaMemcpy`（[L90-L91](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L90-L91)）搬走 \( 4N^2+4N \) 字节；源码里甚至在拷贝前后留了 `//timer start/end for H2D` 注释（[L89-L92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L89-L92)），提示作者原本想分段计时，最终实现的是整段 wall time。

真正被启动的 kernel 是「边检查边计算」版：

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L39-L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L39-L51) —— `spmv_dense_check_and_compute`：每线程负责一行，内层循环扫全行，`if (matrix[...] != 0.0)` 跳过零。注意它**仍然读满 \( N^2 \) 个元素**——分支只省了乘加，不省读、更不省传输。一行内 `matrix[i*num_rows+j]` 与 `vector[j]` 都是连续访问，这是 u4-l2 讲过的合并访问形态。

而「朴素不带分支」的版本其实是一段**死代码**：

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L27-L37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L27-L37) —— `spmv_dense` 全仓库无任何启动点（4.5 会看到它不是唯一一个）。读微基准源码时要养成习惯：**以 `<<<...>>>` 启动行为准，不以函数存在为准**。

启动配置方面，[L95](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L95) 固定 `<<<256,256>>>` = 65,536 线程，配合 kernel 内 `if (i < num_rows)` 守卫（[L30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L30)）在 N=10240 时只有 10,240 个线程有活干。

#### 4.2.4 代码实践

1. **实践目标**：用 nvprof 验证「dense 路径每次调用搬运约 419.5 MB、且与 nnz 无关」。
2. **操作步骤**：分别执行 `nvprof ./SpMV_cuda 8192 10240` 与 `nvprof ./SpMV_cuda 33554432 10240`，在 GPU activities 的 memcpy 行里找出每次调用约 419 MB 的 `HtoD` 拷贝（每轮 5 次计时＋1 次预热， Calls 列可反向核对，方法见 u1-l4）。
3. **需要观察的现象**：两次实验中该 memcpy 的单次大小与耗时几乎相同。
4. **预期结果**：由 \( 4N^2+4N \) 计算得 419,512,320 字节 ≈ 419.5 MB；用「大小 ÷ 耗时」估算达到的拷贝带宽（PCIe 典型每秒几 GB 到二十几 GB）。
5. **待本地验证**：无 GPU 环境可改为阅读归档 [SpMV_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.output.carina.txt)：其中 dense 一列在所有 nnz 点上确实近似恒定（约 1.6 ms）——但注意该归档来自旧版二进制（打印格式与现源码 [L211](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L211) 的 CSV 不一致，且所用 N 未知），只能取定性结论。

#### 4.2.5 小练习与答案

**练习 1**：`spmv_dense_check_and_compute` 的分支能省传输量吗？能省什么？

答案：不能省传输——矩阵仍是整体搬运的；也不能省全局内存读——每个元素仍要读出来与 0 比较。它只省了零元素上的乘加运算（以及 `x[j]` 的部分读取）。传输量由布局决定，布局没变，传输不变。

**练习 2**：N=10240 时 dense 路径每轮（含预热共 6 次调用）搬多少字节？如果换到 N=2048 呢？

答案：单次显式传输 \( 4N^2+8N \)。N=10240：419,512,320 字节 ≈ 419.5 MB；6 次约 2.5 GB。N=2048：\( 4\times 2048^2 + 8\times 2048 \) = 16,781,312 字节 ≈ 16.8 MB——矩阵减为 1/25，传输也近似减为 1/25（\( N^2 \) 主导）。

### 4.3 spmv_csr：CSR 压缩布局——只搬非零元素及其索引

#### 4.3.1 概念说明

优化路线一：**在主机端把矩阵压缩成 CSR，只把非零值和它们的列号搬上 GPU**。传输量从 \( N^2 \) 的函数变成 nnz 的函数：

\[
B_{csr} = \underbrace{4(N+1)}_{ptr} + \underbrace{4Z}_{indices} + \underbrace{4Z}_{data} + \underbrace{4N}_{x\,(H2D)} + \underbrace{4N}_{y\,(D2H)} = 8Z + 8N + 4
\]

注意每个非零元素花 8 字节（4 字节值＋4 字节列号）——**索引是压缩的代价**。把它与 dense 比较：

\[
B_{csr} < B_{dense} \iff 8Z + 8N < 4N^2 + 8N \iff Z < \frac{N^2}{2} \iff \rho < 50\%
\]

这就是「**稀疏格式只在真稀疏时才省**」的定量表达：密度高于 50% 时，「压缩」格式反而比原矩阵更大（N=10240、nnz=67,108,864 的第一个实验点正是这种情况：CSR 要搬约 536.9 MB，dense 只搬 419.5 MB）。本讲综合实践会带你完整推导并实测这个阈值。

计算量同样受益：kernel 每线程只遍历自己那一行的非零段，总乘加次数是 Z 而不是 \( N^2 \)。

#### 4.3.2 核心流程

`spmv_cuda_csr_discrete` 包装函数：

```text
（计时前！）主机端 init_csr：把稠密矩阵转成 ptr/indices/data
计时开始
cudaMalloc d_ptr / d_indices / d_data / d_x / d_y
H2D: ptr（4(N+1)）＋ indices（4Z）＋ data（4Z）＋ x（4N）
启动 spmv_csr<<<256,256>>>（每线程一行，行内有守卫）
cudaDeviceSynchronize
D2H: y（4N）
free 主机临时数组；cudaFree 全部设备数组
计时结束
```

与 dense 的单条大拷贝不同，这里是**四条小拷贝**——这个差别在解释实测时间时很重要。

#### 4.3.3 源码精读

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L110-L125](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L110-L125) —— 包装函数开头：先在主机上 `malloc` 并调用 `init_csr` 生成 CSR（[L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L119)），**然后**才启动计时器（[L120](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L120)）。也就是说格式转换的 \( O(N^2) \) 主机开销被排除在 CSR 的时间之外——记住这一点，4.4 会看到 unified 路径的相反做法，两者口径不对称。

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L133-L141](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L133-L141) —— 四条 H2D 拷贝正好对应公式里的 \( 4(N+1) + 4Z + 4Z + 4N \) 字节；kernel 启动后同步、回拷 y。

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L11-L24](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L11-L24) —— `spmv_csr` kernel：全局线程号即行号，守卫 `if (row < num_rows)`，用 `ptr[row]` 与 `ptr[row+1]` 框出该行非零段，做点积后 `y[row] = dot`。它就是串行基线 `spmv_csr_serial`（[SpMV_cuda.c:L120-L130](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L120-L130)）的逐行并行化，算法结构完全一致。访存方面 `data[n]`、`indices[n]` 连续（合并），但 `x[indices[n]]` 是随机 gather——正是 u4-l2 讲过的模式。

#### 4.3.4 代码实践

1. **实践目标**：验证 csr 的传输量与时间随 nnz 线性增长、且密度越低相对 dense 优势越大。
2. **操作步骤**：取 `test.sh` 中的三个点 `./SpMV_cuda 33554432 10240`、`./SpMV_cuda 1048576 10240`、`./SpMV_cuda 8192 10240`，记录 CSV 第 2 列（csr）与第 1 列（dense）；用公式 \( 8Z + 8N + 4 \) 先算出三者的理论字节数（约 268.5 MB、8.4 MB、0.15 MB）再对照实测趋势。
3. **需要观察的现象**：csr 时间随 nnz 单调下降（nnz 减半时间约减半）；dense 时间几乎不动。
4. **预期结果**：nnz 足够小之后 csr 的显式传输量比 dense 低两三个数量级；但 csr 时间**不会**同比低两三个数量级——因为 wall time 里还有每次调用的 5 次 `cudaMalloc`、4 条小拷贝的固定开销（u2-l3/u1-l4 的老结论：小数据量下分配与启动开销主导）。
5. **待本地验证**：无 GPU 时用归档数据做趋势对照——Carina 归档中 csr 从 14.88 ms（nnz=524288）单调降到 2.64 ms（nnz=8），而 dense 恒在 1.6 ms 附近；归档里 csr 反而比 dense 慢，正是「流量下界 ≠ 时间」的实例（归档所用 N 未知，只能定性解读）。

#### 4.3.5 小练习与答案

**练习 1**：密度分别是 64% 与 0.01% 时，CSR 相对 dense 的显式传输量比值是多少（N=10240）？

答案：密度 64%（\( Z = 67{,}108{,}864 \)）：\( (8Z+8N+4)/(4N^2+8N) \approx 536.9/419.5 \approx 1.28 \)——CSR 反而多搬 28%。密度 0.01% 意味着 \( Z = 0.0001 N^2 = 10{,}486 \)：\( 8Z \approx 83{,}889 \) 字节，比值约 \( 0.15\text{MB}/419.5\text{MB} \approx 1/2800 \)。

**练习 2**：为什么 CSR 里每个非零元素要付 8 字节，而 dense 每个槽位只付 4 字节？由此推出平衡密度。

答案：CSR 必须同时携带值（float，4 字节）与列号（int，4 字节），因为压缩后元素的列位置信息丢失了、必须显式存；dense 的位置隐含在下标里，零与非零同价 4 字节。每非零 8 字节对阵每槽位 4 字节，平衡点在非零占比 50%。

**练习 3**：`spmv_csr` 与串行基线 `spmv_csr_serial` 在算法上是什么关系？

答案：完全相同的行点积算法，只是把「for 每行」分派到线程维度：一个线程负责一行，行边界由同一个 `ptr` 数组给出。这是典型的「串行参考实现 → kernel 化」对应（u1-l3 讲过的 oracle 模式）。

### 4.4 spmv_unified：索引显式搬运＋数值按页迁移

#### 4.4.1 概念说明

优化路线二：结合 u5-l4 的统一内存。设计意图分三部分：

1. **矩阵本体放进统一内存**（`cudaMallocManaged`），不显式 `cudaMemcpy`——数值「留在原地」，GPU 用到哪些页，迁移引擎按 4KB 页把哪些页搬过去；
2. **只显式搬运索引表**（`row[]`、`column[]` 各 \( 4Z \) 字节）——告诉 kernel 非零在哪；
3. kernel 按索引表取 `matrix_unified[row*N + column[n]]` 参与计算。

显式传输量降到：

\[
B_{unified}^{explicit} = 8Z + 4N\,(x) + 4N\,(y\,D2H) = 8Z + 8N
\]

再加上**隐式页迁移**：矩阵共 \( P = N^2/1024 \) 页（每页 1024 个 float），Z 个均匀随机非零触碰的期望页数为

\[
P_{touch} = P\left(1-\left(1-\frac{1}{P}\right)^{Z}\right) \approx P\,(1-e^{-Z/P}), \qquad B_{unified}^{migrate} \approx 4096 \cdot P_{touch}
\]

代入 N=10240（P=102,400 页）：nnz=33,554,432 时 \( Z/P \approx 328 \)，几乎所有页都被触碰，迁移 ≈ 整个矩阵 419.4 MB；nnz=8192 时约 7,900 页 ≈ 32 MB；nnz=8 时约 8 页 = 32 KB。**迁移按页不按元素**——32 KB 的有效数据可能换来 32 MB 的迁移流量，页粒度就是税（u5-l4 的「每触碰元素代价」结论在这里换个形状再现）。

所以 unified 布局的适用区间与 u5-l4 的交叉点结论同构：**非零少且散布导致触碰页远小于总页数时**，unified 才在流量上占优；密度一高，它既要付 8Z 的索引，又要付接近整矩阵的迁移，两头吃亏（64% 密度点：536.9 MB ＋ 419.4 MB ≈ 956 MB，全场最差）。

#### 4.4.2 核心流程

`spmv_cuda_unified` 包装函数：

```text
计时开始                                    ← 注意：比 csr 早得多
cudaMallocManaged 分配 matrix_unified（4N²）
主机 memcpy 把整个矩阵写进托管缓冲（4N² 字节，在 CPU 侧填页）
（计时内！）init_index 在主机生成 row[]/column[] 索引表
cudaMalloc d_row / d_column / d_x / d_y
H2D: row（4Z）＋ column（4Z）＋ x（4N）     ← 显式搬运只有这些
启动 spmv_unified<<<256,256>>>              ← kernel 触碰托管页 → 按需缺页迁移
cudaDeviceSynchronize；D2H y；释放
计时结束
```

三个必须记住的口径问题：

- 计时从 `cudaMallocManaged` 之前就开始，**包含 4N² 字节的主机 memcpy**（N=10240 时约 400 MB 的内存拷贝！）和 `init_index` 的 \( O(N^2) \) 扫描——而 csr 路径的 `init_csr` 在计时外。两条路径的主机准备工作一个算时间、一个不算，**口径不对称**；
- 真正的 CPU→GPU 迁移发生在 kernel 执行期间（缺页），被 wall time「打包」计入，无法从输出中单独分离，必须靠 nvprof；
- 与其他所有基准一样，每次调用都重新分配/释放，`cudaMallocManaged` 的建立开销也计入平均。

#### 4.4.3 源码精读

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L162-L168](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L162-L168) —— 计时开始后立刻 `cudaMallocManaged` 并用**普通主机 memcpy** 把整个矩阵填入托管缓冲（[L165](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L165)），随后 `init_index`（[L168](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L168)）也在计时区内。此刻所有页驻留 CPU，GPU 侧尚无任何矩阵数据。

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L178-L182](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L178-L182) —— 显式 H2D 只有索引表与 x：\( 4Z + 4Z + 4N \) 字节；然后启动 `spmv_unified`。

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L54-L64](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L54-L64) —— `spmv_unified` kernel。仔细读循环：**每个线程从 0 扫到 nnz**，只处理 `rowNum[n] == row` 的那些索引。总比较次数是 \( 65536 \times Z \)（网格固定 256×256），其中 N=10240 时约 84% 的线程行号越界、整个扫描全程空转。两个后果：

1. **计算量爆炸**：算法复杂度是 \( O(\text{threads} \times Z) \) 而不是 \( O(Z) \)。以 test.sh 最大点算，\( 65536 \times 67{,}108{,}864 \approx 4.4\times10^{12} \) 次比较——「省了传输、毁在算法」的活教材；
2. **越界写**：kernel 没有 `if (row < num_rows)` 守卫，`y[row] = dot`（[L63](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L63)）对 row ∈ [num_rows, 65536) 全部执行，而 `d_y` 只分配了 `num_rows` 个 float（[L176](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L176)）。N=10240 时约 5.5 万次 4 字界外写。对照 csr（[L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L14)）与 dense（[L41](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L41)）都有守卫，唯独这个 kernel 没有。实际是否触发非法地址错误取决于驱动与显存布局，**待本地验证**（可用 `compute-sanitizer --tool memcheck` 复核，结论填入你的实验记录）。

#### 4.4.4 代码实践

1. **实践目标**：把 unified 路径的「显式拷贝」与「隐式迁移」分开测量，验证页粒度税。
2. **操作步骤**：执行 `nvprof --print-summary per-kernel ./SpMV_cuda 8192 10240`，在 memcpy 行确认显式 H2D 只有约 65.5 KB＋40.9 KB（row/column/x），没有矩阵大拷贝；再按 u5-l4 介绍的方法观察统一内存迁移相关计数器（如 `cudaMallocManaged` 数据的 HtoD 迁移流量）。分别对 nnz=8192 与 nnz=33554432 各跑一遍。
3. **需要观察的现象**：nnz=8192 时迁移量为几十 MB 量级（≈ 4096×触碰页数），远大于 65.5 KB 的显式拷贝；nnz=33554432 时迁移量逼近整个矩阵约 419 MB。
4. **预期结果**：与 4.4.1 的 \( P_{touch} \) 估算同数量级。若迁移计数器取不到（工具版本差异），退而求其次：对比 CSV 第 3 列在两个 nnz 点的差值，它主要反映迁移流量之差。
5. **待本地验证**：wall time 里混有 400 MB 主机 memcpy（N=10240 时），解读时务必扣除或改用小 N（如 2048）重跑以放大迁移占比的信号。

#### 4.4.5 小练习与答案

**练习 1**：unified 路径的显式传输量公式与 csr 几乎一样（都是 \( 8Z + 8N \) 量级），那它的意义在哪里？

答案：差别在**矩阵数值怎么过去**：csr 把值打进 `data`（显式、精确到元素）；unified 不显式搬值，靠 kernel 触碰时的按页迁移。当非零散布只触碰极少数页时，迁移量远小于任何显式搬运；当非零多到触碰所有页时，迁移量收敛到整矩阵——所以 unified 的优势区间是「低密度」，与 u5-l4 的访问密度交叉点结论一致。

**练习 2**：N=10240、nnz=8192 时，估算 \( B^{migrate} \) 与 \( B^{explicit} \) 的比值，并解释这个比值的含义。

答案：\( P = 102{,}400 \)，\( P_{touch} \approx P(1-e^{-8192/102400}) \approx 102{,}400 \times 0.0769 \approx 7{,}874 \) 页 → \( B^{migrate} \approx 32.3 \) MB；\( B^{explicit} = 8Z + 8N \approx 0.15 \) MB。比值约 215：即每搬 1 字节「有用索引」，还要为数值付 215 字节的页迁移——**页粒度税**的直观大小（比 u5-l4 每 4KB 页装 1 个被触碰元素的极端情形温和，因为这里每页平均还有 8 个非零落位）。

**练习 3**：`spmv_unified` kernel 的复杂度是多少？如果给你改，最省力的修法是什么？

答案：\( O(\text{threads}\times Z) \)，threads 固定 65,536。最省力的修法是给每个线程只扫自己行的索引段——这正是仓库里已经写好的 `spmv_unified_count` kernel（4.5）；退而求其次也至少应补上 `if (row < num_rows)` 守卫消除越界写。

### 4.5 spmv_unified_count：写对了算法、却没接上线的计数变体

#### 4.5.1 概念说明

第四条路径的设计意图：在 unified 的基础上**再加一个每行起始偏移数组 `count`**（即 CSR 的 `ptr`），让每个线程只扫自己那一行的索引段，把 \( O(\text{threads}\times Z) \) 的全表扫描修成 \( O(Z) \)。「count」指每行非零个数的计数，其前缀和就是行起始偏移。这本来应该是四条路径里算法最合理的一条——**但源码里它没有被接线**：包装函数启动的仍是 `spmv_unified`。本模块因此有两层内容：一是读懂「本应发生什么」（算法层面的修正），二是读懂「实际发生了什么」（工程层面的缺陷）。后者对微基准读者同样重要：**归档数据里 unified 与 unified_count 两列为何几乎相同？答案就藏在源码的一行启动语句里。**

#### 4.5.2 核心流程

**设计上**的执行流程：

```text
（同 unified：托管矩阵＋主机 memcpy＋索引表）
init_index_count 生成 row[]/column[] ＋ count[]（每行起始偏移的前缀和）
H2D: row ＋ column ＋ count ＋ x
启动 spmv_unified_count<<<256,256>>>
  每线程：row_start = count[row]; row_end = count[row+1]
  只扫 [row_start, row_end) 的索引段 → O(Z) 总工作量
```

**实际上**的执行流程：与 unified 完全相同（同一个 kernel），外加一次白搬的 `count` 数组拷贝。

#### 4.5.3 源码精读

先看「本应被启动」的 kernel：

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L68-L79](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L68-L79) —— `spmv_unified_count`：用 `count[row]`/`count[row+1]` 框定本行索引段，循环体与 `spmv_csr` 同构（区别只是值从托管矩阵按 `row*num_rows+columnNum[n]` 取）。这就是把 4.4 的算法缺陷修掉的版本。

再看关键的一行——包装函数实际启动了谁：

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L225-L229](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L225-L229) —— `d_count` 被认真拷上 GPU（[L225](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L225)），但 [L229](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L229) 启动的是 **`spmv_unified`**。全仓库搜索可以确认 `spmv_unified_count` 没有任何启动点：数据传了、kernel 写了、启动语句没换。这直接解释了为什么（无论你的机器还是归档里）unified 与 unified_count 两列总是几乎相等——它们跑的是同一个 kernel，只差 4N 字节的多余拷贝。

`count` 的生成与一个潜在 off-by-one：

[MiniTransfer_SpMV/SpMV_cuda.c:L87-L104](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L87-L104) —— `init_index_count` 填 `row_nnz_start[0..num_rows-1]`，**没有**写第 \( N+1 \) 个哨兵（对照 `init_csr` 的 [L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L66)：`ptr[num_rows] = nnz`）。而包装函数里 `count` 只 `malloc` 了 `num_rows` 个 int（[L202](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L202)）。若真启动 `spmv_unified_count`，kernel 会读 `count[num_rows]`（[L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L72)）——越界读未初始化值，最后一行的行终点错误。目前因 kernel 未启动而休眠。

释放段的错误：

[MiniTransfer_SpMV/SpMV_cudakernel.cu:L233-L241](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L233-L241) —— [L235](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L235) 已用 `free(count)` 释放**主机**指针，[L238](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L238) 又把同一个主机指针交给 `cudaFree`（对非设备指针的非法调用），而真正的设备缓冲 `d_count`（[L218](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L218)）从未释放。由于项目从不检查 API 返回值（u2-l3 的老问题），这些错误全部静默。

最后是归档证据：

[MiniTransfer_SpMV/SpMV_cuda.output.carina.txt:L42-L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.output.carina.txt#L42-L51) —— 任取一段：unified 与 unified_count 两个时间在所有 nnz 点上都只差毫秒级噪声（如 5.44 vs 5.40、68.68 vs 68.44）。归档出自旧版二进制（打印格式与现源码不同、N 未知），不能证明旧版也这样实现；但**当前源码**下两列必然近乎相同，因为跑的是同一个 kernel。

#### 4.5.4 代码实践

1. **实践目标**：完成一次「让 count 变体真正跑起来」的修复实验（在你自己的副本上，不动仓库源码）。
2. **操作步骤**：把仓库复制一份到临时目录；在副本中（a）把 [L229](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L229) 的启动改为 `spmv_unified_count<<<256,256>>>(d_count, matrix_unified, num_rows, d_row, d_column, d_x, d_y, nnz)`；（b）把 [L202](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L202) 的 `count` 分配改为 `(num_rows+1)*sizeof(int)`，并在 `init_index_count` 末尾补 `count[num_rows] = nnz;`（对照 `init_csr` 的写法）；（c）顺手把 [L238](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L238) 改为 `cudaFree(d_count);`；（d）给 kernel 补 `if (row < num_rows)` 守卫（消除 4.4 指出的越界写）。用 `./SpMV_cuda 1048576 10240` 与 `./SpMV_cuda 8192 10240` 对比修复前后的第 4 列。
3. **需要观察的现象**：修复后 unified_count 的时间应显著低于 unified（算法从 \( 65536\times Z \) 次比较降到 \( Z \) 次乘加）。
4. **预期结果**：nnz 越大差距越悬殊；同时正确性仍可用——如需验证，可临时恢复 main 里被注释的 `check_unified_count` 打印（注意 4.1 提到的基线 `y` 未初始化问题，比较前应先 `zero(y, num_rows)` 并把基线的 `+=` 改为 `=` 或先清零）。
5. **待本地验证**：需要 GPU；无 GPU 时完成源码级练习——写出修复后的 diff 并推演每处改动的理由，即为合格完成。

#### 4.5.5 小练习与答案

**练习 1**：Carina 归档里 unified 与 unified_count 两列几乎相同。给出两种可能的解释，并说明哪一种能被当前源码证实。

答案：解释 A：两者启动了同一个 kernel（只差一次小拷贝）；解释 B：两者算法不同但该数据点上耗时恰好接近。当前源码的 [L229](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cudakernel.cu#L229) 证实了解释 A 成立于**现在这份代码**；但归档来自旧版二进制，旧版行为无法从现源码确证——这是 u1-l4 讲过的「归档与现版本不可直接划等号」的又一实例。

**练习 2**：若 `spmv_unified_count` 被正确启动但 `count` 仍只分配 `num_rows` 个 int，会发生什么？

答案：kernel 读 `count[row+1]`，最后一行（row=num_rows−1）读到 `count[num_rows]`——越界且未初始化，该行的循环终点错误：可能提前结束（漏算非零）或大幅超界（把后续内存当索引读），结果错误甚至崩溃。这正是 CSR 实现总用 \( N+1 \) 长度 `ptr` 并写哨兵 `ptr[N]=nnz` 的原因。

**练习 3**：本模块的三处缺陷（kernel 未接线、count 少一个元素、`cudaFree` 释放主机指针）为什么都没被实验发现？

答案：kernel 未接线只影响性能结论（两列一样高），不影响程序跑通；count 少一个元素只在未启动的 kernel 里被读；`cudaFree` 的错误返回值没人检查，静默失败。三者共同说明：**没有正确性输出（check 被注释）＋没有错误检查的微基准，会跑得很顺、错得很深**——这也是 u6-l4 方法论讲义要展开的主题。

## 5. 综合实践

把本讲的传输量/计算量模型完整走一遍。**目标**：对你的机器（或归档数据）回答「稀疏度多少时 CSR 必然优于 dense」，并用实测检验。

**第 1 步：找出实验设定。** 在 [SpMV_cuda.c:L152-L157](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.c#L152-L157) 确认参数语义，在 [test.sh:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/test.sh#L1) 与 [test.sh:L15](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/test.sh#L15) 读出扫描设计：N=10240，nnz 从 \( 2^{26} \) 到 \( 2^3 \)。密度 \( \rho = \text{nnz}/N^2 \)，范围 64% 到约 \( 7.6\times10^{-8} \)。

**第 2 步：填传输量表。** 用本讲公式补全（已给两行示范）：

| nnz | ρ | dense（MB） | csr（MB） | unified 显式（MB） | unified 迁移（MB，估） |
|---|---|---|---|---|---|
| 67,108,864 | 64% | 419.5 | 536.9 | 536.9 | ≈419.4 |
| 33,554,432 | 32% | 419.5 | 268.5 | 268.4 | ≈419.4 |
| 1,048,576 | 1% | ？ | ？ | ？ | ？（\( Z/P\approx10.2 \)，仍近乎全页） |
| 8,192 | 0.0078% | ？ | ？ | ？ | ？ |
| 8 | ~0 | ？ | ？ | ？ | ？ |

（答案：csr 列依次约 8.4 MB、0.15 MB、0.08 MB；unified 显式列再减去 ptr 那 0.04 MB 即可；迁移列用 \( 4096\,P(1-e^{-Z/P}) \) 估算，nnz=8 时约 0.03 MB。）

**第 3 步：实测对照。** 在有 GPU 的机器上按 `test.sh` 的方式跑上述点（无 GPU 则用 [SpMV_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MiniTransfer_SpMV/SpMV_cuda.output.carina.txt) 做趋势对照，并注明版本差异）。检查三件事：dense 列是否近似常数；csr 列是否随 nnz 线性；unified 列是否在所有点上都最高（为什么？把 4.4 的算法复杂度与 400 MB 主机 memcpy 一起算进去）。

**第 4 步：回答阈值问题。** 显式传输量意义上：\( 8Z + 8N < 4N^2 + 8N \iff \rho < 50\% \)——密度低于一半时 CSR 搬得更少，「必然优于」成立于**流量轴**；高于 50% 时 CSR 反而多搬（索引开销超过省下的零）。但要在报告里写清两个限定：（a）端到端时间还受拷贝条数、每次调用的分配开销、kernel 效率调制（归档里 dense 反而最快就是证据）；（b）本讲 unified 路径的 kernel 是 \( O(65536\times Z) \) 的实现，其耗时不能用来评价「统一内存布局」本身的好坏——布局与实现要分开归因（单变量原则，见 u1-l3/u4-l2）。

**第 5 步：写一页实验报告**，包含：参数与公式、上表、实测表、阈值推导、以及至少一条「我发现的源码与文档/直觉不符之处」（候选：死代码 `spmv_dense`、未接线 `spmv_unified_count`、越界写、计时口径不对称、`y` 未清零）。

## 6. 本讲小结

- **布局决定流量**：dense 的传输量 \( 4N^2+8N \) 与非零个数无关；CSR 是 \( 8Z+8N+4 \)；unified 显式只搬索引 \( 8Z+8N \)，数值交给按页迁移 \( \approx 4096\,P(1-e^{-Z/P}) \)。
- **稀疏格式有价**：每个非零要付 8 字节（值＋列号），所以密度超过 50% 时「压缩」格式比 dense 还大——稀疏格式只在真稀疏时才省。
- **迁移按页不按元素**：unified 的隐藏成本是页粒度税（nnz=8192 时约 215 倍于显式索引流量）；密度高时迁移收敛到整矩阵，unified 两头吃亏。
- **传输量是下界不是时间**：wall time 混有每次调用的 cudaMalloc/cudaFree、多条小拷贝、（unified 路径独有的）4N² 主机 memcpy 与计时内的格式转换——归档中 csr 慢于 dense 正是这些固定开销所致。
- **源码要读启动语句**：`spmv_dense` 与 `spmv_unified_count` 两个 kernel 从未被启动，后者意味着 CSV 第 4、5 列跑的其实是同一个 kernel；此外还有 `spmv_unified` 的越界写、`count` 的 off-by-one、`cudaFree(主机指针)`、串行基线累加未初始化 `y` 等休眠缺陷——没有 check 输出与错误检查的程序，跑得顺不等于没错。

## 7. 下一步学习建议

- 顺着单元四/五的剩余线索复习对照：u4-l3（SpMM 的 CSR/CSC 对偶——存储格式如何决定**配对算法**）与本讲（布局如何决定**搬运量**）合起来，就是「先看访问模式，再选数据布局」的完整方法论。
- 若想继续深挖统一内存：回看 u5-l4 的预取提示（`cudaMemPrefetchAsync`/`cudaMemAdvise`），思考若在 kernel 前显式预取整个矩阵，本讲 unified 路径的迁移行为会变成什么样，并设计实验验证。
- 下一讲可进入单元六：u6-l1 的归约优化阶梯（把 u4-l5/u4-l6 串成完整的优化决策链）与 u6-l4 的基准方法论（本讲反复出现的「计时口径、归档版本、单变量归因」问题的系统化总结）。也可先做 u6-l2，用本学到的骨架亲手写一个自己的微基准。
