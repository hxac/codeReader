# Shmem：共享内存分块矩阵乘

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚为什么朴素矩阵乘 kernel 在 GPU 上是"访存受限"的：数据被反复从全局内存读取，读取量是计算量的瓶颈。
2. 读懂并亲手写出 tiled（分块）矩阵乘的共享内存版本 `shared_block`：用 `__shared__` 声明块内共享缓存、用 `__syncthreads()` 做块内同步。
3. 用"流量公式"量化分块的收益：全局内存读取量从 \(2N^3\) 降到 \(2N^3/B\)（\(B\) 为块边长），并能解释为什么实测收益远小于理论值。
4. 独立完成 BLOCK_SIZE（8/16/32）× 矩阵规模 N 的扫描实验，并制作对比表。

本讲依赖 u2-l3（显存管理骨架：cudaMalloc → cudaMemcpy → kernel → cudaMemcpy → cudaFree），请先确认你已经熟悉这条流水线。

## 2. 前置知识

### 2.1 矩阵乘法与行主序一维存储

方阵乘法 \(C = A \times B\) 的定义：

\[
C_{ij} = \sum_{k=0}^{N-1} A_{ik} \cdot B_{kj}
\]

每个元素 \(C_{ij}\) 是 A 的第 i 行与 B 的第 j 列的点积。总浮点运算量为 \(2N^3\)（每个乘加贡献 2 个 FLOP），这是后面算 MFLOPS 的依据。

本项目中矩阵用**行主序一维数组**存储：`A[i*N + k]` 表示第 i 行第 k 列。这继承了 u2-l1 的索引思维，只是从一维公式 `i = blockDim.x*blockIdx.x + threadIdx.x` 扩展到二维：

```text
row = blockIdx.y * blockDim.y + threadIdx.y   // 我负责第几行
col = blockIdx.x * blockDim.x + threadIdx.x   // 我负责第几列
```

### 2.2 GPU 的存储层次与共享内存的位置

回顾 u1-l1 提到的"GPU 内部深存储层次"，从慢到快大致是：

| 存储 | 位置 | 典型访问延迟（数量级） | 作用域 |
|---|---|---|---|
| 全局内存（显存） | 片外 DRAM | 数百周期 | 所有线程可见 |
| L2 / L1 缓存 | 片上 | 数十周期 | 硬件自动管理 |
| **共享内存** | 片上（SM 内） | 约 20~30 周期 | **同一个 block 内所有线程可见** |
| 寄存器 | SM 内 | 最快 | 单个线程私有 |

**共享内存（shared memory）**是本讲主角，它有三个关键性质：

1. **片上、低延迟**：物理上是 SM 内的 SRAM，比走 DRAM 的全局内存快一个数量级。
2. **块级作用域**：一个 block 内所有线程看到同一份共享内存变量——这正是"线程间交换/复用数据"的通道；block 结束即释放。
3. **程序员显式管理**：不像 L1/L2 缓存由硬件自动管理，共享内存里放什么、何时写入、何时读取，全部由你用代码决定。可以把它理解为"一块由程序员手工管理的缓存"。

在 kernel 里用 `__shared__` 修饰符声明，例如 `__shared__ float tile[16][16];`。它是静态分配的：**无论声明写在循环内还是循环外，都是 block 启动时一次性分配、整个 kernel 生命周期共用同一块存储**（这一点初学者容易误解，见 4.2.3）。

### 2.3 块内同步：为什么需要 `__syncthreads()`

CUDA 不保证同一 block 内线程的执行进度一致。如果线程 0 要读线程 31 刚写进共享内存的数据，必须有一个"栅栏"保证**所有线程都写完了**才能往下走。`__syncthreads()` 就是这个栅栏：block 内所有线程都到达这一行之前，谁都不能越过。它只能同步**同一个 block 内**的线程，跨 block 没有对应的栅栏原语。

### 2.4 README 对 Shmem 的定位

README 的汇总表中，Shmem 属于"有效利用 GPU 内部深存储层次"一类：反模式是"数据被多次重复访问"，优化技术是"把需要反复访问的数据放进共享内存"，见 [README.md:L46-L49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L46-L49)（原文拼写 "serveral/repeatly" 未修正）。

## 3. 本讲源码地图

Shmem 目录共 5 个文件，完全符合 u1-l3 总结的"三件套 + Makefile"骨架，只是用 `testResults.txt` 替代了其他基准常见的 `test.sh`：

| 文件 | 角色 | 关键内容 |
|---|---|---|
| [Shmem/mm_omp_cuda.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.h#L1-L16) | 接口头文件 | `REAL` 宏、`extern "C"` 声明 `mm_kernel` 与 `mm_kernel_shmem` |
| [Shmem/mm_omp_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L1-L125) | host 主程序 | 初始化、串行基线、warmup、计时、弱校验、打印 |
| [Shmem/mm_kernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L1-L187) | kernel 文件 | 3 个 kernel（`global_element`/`global_block`/`shared_block`）+ 2 个包装函数 |
| [Shmem/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/Makefile#L1-L8) | 构建 | 单行 nvcc 混编 .c 与 .cu |
| [Shmem/testResults.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L1-L85) | 归档实验结果 | Carina 集群上 N=512/1024/2048 的 nvprof 完整转录 |

注意：`BLOCK_SIZE` 只在 [Shmem/mm_kernel.cu:L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L8) 定义一处（值为 16），三个 kernel 和两个包装函数共用它——本讲综合实践要改的就是这一行。

## 4. 核心概念与源码讲解

### 4.1 global_element / global_block：不分块与分块的全局内存基线

#### 4.1.1 概念说明

矩阵乘最自然的并行化（对照 u2-l1 的"每线程一个元素"）：每个线程负责**一个** \(C_{ij}\)，把 A 的第 i 行和 B 的第 j 列各读 N 个元素、做 N 次乘加。这是 `global_element`。

问题在**数据复用**：计算 \(C_{ij}\) 用到的 A 的第 i 行，会被同一行所有 N 个线程（计算 \(C_{i0},C_{i1},\dots,C_{i,N-1}\)）各读一遍；B 的列同理。从 DRAM 视角（忽略缓存），总读取元素数为

\[
\underbrace{N^2}_{\text{线程数}} \times \underbrace{2N}_{\text{每线程读 A 行 + B 列}} = 2N^3
\]

而总计算量只有 \(2N^3\) FLOP——**平均每读 1 个元素（double 占 8 字节）只做 1 次 FLOP**。N=512 时全局读取约 \(2.68\times10^8\) 个元素 ≈ 2.15 GB，算力再强也被内存流量拖住，这就是典型的**访存受限（memory-bound）**。

`global_block` 是改进的"半步"：把 k 维度切成 \(N/B\) 段（B=BLOCK_SIZE），block 一段一段地推进——但每一段的数据仍直接从全局内存读。它的循环结构与后面的 `shared_block` **完全同构**，唯一区别是数据落点，因此是完美的对照实验组。

#### 4.1.2 核心流程

`global_element`（每线程一个 C 元素）：

```text
row, col ← 由 blockIdx/threadIdx 二维编号算出
C_value ← 0
for k in 0..n-1:
    C_value += A[row*n + k] * B[k*n + col]     # 每次都直接读全局内存
C[row*n + col] ← C_value
```

`global_block`（分块推进、仍读全局内存）：

```text
把 C 划分成 (n/B)×(n/B) 个 B×B 子块，每个 block 负责一个子块
aBegin ← A 中该子块所在行带的首地址；bBegin ← B 中该子块所在列带的首地址
Csub ← 0
for a = aBegin; a <= aEnd; a += B:            # 沿 k 方向推进 n/B 步
    for k in 0..B-1:
        Csub += A[a + n*ty + k] * B[b + n*k + tx]   # 仍直接读全局内存
C[子块首 + n*ty + tx] ← Csub
```

#### 4.1.3 源码精读

**global_element** 在 [Shmem/mm_kernel.cu:L10-L23](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L10-L23)：L14-L15 用二维内建变量算出 `(row, col)`——这是 u2-l1 一维公式 `blockIdx.x*blockDim.x+threadIdx.x` 的二维版；L17-L19 的 k 循环里每次迭代都从全局内存读 `A[row*n+k]` 与 `B[n*k+col]`，没有任何缓存手段；L22 写回一个元素。

**global_block** 在 [Shmem/mm_kernel.cu:L25-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L25-L72)，这是从 CUDA Samples 移植的经典写法：

- [L39-L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L39-L51) 计算四个游标：`aBegin/aEnd/aStep` 描述该 block 要扫过 A 的哪一段行带（步长 `BLOCK_SIZE`，即每次前进一个 B×B 子块），`bBegin/bStep` 同理描述 B 的列带（步长 `BLOCK_SIZE*wB`，一次跨 B 整行）。
- [L59-L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L59-L66) 主循环：`for (int a = aBegin, b = bBegin; a <= aEnd; a += aStep, b += bStep)` 沿 k 维推进 \(N/B\) 步，内层 L63-L65 对当前子块做 B 次乘加，**乘数直接来自全局内存** `A[a + wA*ty + k]`、`B[b + wB*k + tx]`。注意访存模式并不差：A 的下标与 tx 无关（同 ty 的线程读同一地址，硬件广播），B 的下标随 tx 连续（合并访问，这正是 u4-l2 要展开的主题）——所以它输给 `shared_block` 的原因不是"不合并"，而是**流量**。
- [L70-L71](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L70-L71) 把累加结果写回 C 的对应子块。

启动配置在包装函数里（见 4.3.3）：`dim3 dimBlock(BLOCK_SIZE, BLOCK_SIZE)` 即 16×16=256 线程的二维 block，`dim3 dimGrid(n/16, n/16)`。N=512 时是 32×32=1024 个 block。**整除假设**：`n/dimBlock.x` 向下取整，若 N 不是 BLOCK_SIZE 的整数倍，右/下边缘的 C 永远没人算（练习 3 会让你推演后果）。

#### 4.1.4 代码实践（纸笔推演型）

1. **实践目标**：建立"流量直觉"，亲眼算出朴素矩阵乘有多浪费。
2. **操作步骤**：
   - 对 N=512：计算一个线程要读多少个元素；乘以线程数得全局读取总量；换算成字节（`REAL` 是 double，见 [Shmem/mm_omp_cuda.h:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.h#L6)）。
   - 数一数 [Shmem/mm_kernel.cu:L25-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L25-L72) 的 `global_block` 与 [L10-L23](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L10-L23) 的 `global_element` 有几处实质差异（提示：循环结构变了，但每次乘加仍读 2 次全局内存）。
3. **需要观察的现象**：两个 kernel 的总读取元素数完全相同，都是 \(2N^3\)。
4. **预期结果**：N=512 时每线程读 \(2\times512=1024\) 个元素；总量 \(2\times512^3=268{,}435{,}456\approx 2.68\times10^8\) 个元素，约 2.15 GB。`global_block` 只是让访问更有局部性（更利于 L1/L2 兜底），DRAM 视角的流量没变——真正降流量的是下一节的 `shared_block`。

#### 4.1.5 小练习与答案

**练习 1**：`global_element` 中同一份 A 的第 i 行最多会被读多少遍？
**答案**：N 遍——计算第 i 行的 N 个输出元素 \(C_{i0},\dots,C_{i,N-1}\) 的 N 个线程各读一遍。B 的每个元素同理被读 N 遍。这就是"数据被多次重复访问"的反模式。

**练习 2**：`global_block` 与 `global_element` 总读取量相同，那它存在的意义是什么？
**答案**：它是 `shared_block` 的**同构对照组**——tile 循环、游标、写回完全一致，唯一差别是数据落点（全局 vs 共享）。对比两者就把"共享内存分块"这一个变量的效果干净地隔离出来，这正是微基准的控制变量方法论（u1-l3）。附带收益是它的地址序列局部性更好，L1/L2 更容易命中。

**练习 3**：把 N 设为 1000（BLOCK_SIZE=16），程序会发生什么？
**答案**：[L155](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L155) 的 `n/dimBlock.x` 向下取整得 62，grid 只覆盖 62×16=992 行/列，最右 8 列与最下 8 行的 C 没有任何线程写入；而 `C_device` 来自未初始化的 `cudaMalloc`（u2-l3 讲过它不清零），拷回的是垃圾值，校验大概率打印差异。程序**不会报错**——这提醒我们改 BLOCK_SIZE 时必须保证 N 整除它。

### 4.2 shared_block：`__shared__` 分块缓存 + `__syncthreads` 同步

#### 4.2.1 概念说明

分块（tiling）的核心思想：**把要被反复使用的数据搬进片上共享内存，让复用发生在片内而不是 DRAM**。

以 B×B 的 tile 为单位看：一个 B×B 的 block 计算 C 的一个 B×B 子块，需要 A 的 B 行带 × N 列、B 的 N 行 × B 列带。把这些数据按 B×B 的子块逐段搬进共享内存，每搬进一对子块 `As`、`Bs`：

- 搬入成本：\(2B^2\) 次全局读取；
- 换来计算：block 内 \(B^2\) 个线程各做 B 次乘加，共 \(B^3\) 次乘加，每个 `As[ty][k]` 被 B 个不同 tx 的线程复用，每个 `Bs[k][tx]` 被 B 个不同 ty 的线程复用。

于是全局内存读取总量降为

\[
\underbrace{(N/B)^2}_{\text{block 数}} \times \underbrace{(N/B)}_{\text{tile 步数}} \times \underbrace{2B^2}_{\text{每步读入}} = \frac{2N^3}{B}
\]

即**理论上流量降低 B 倍**（B=16 即 16 倍），算术强度从每元素 1 FLOP 升到 B FLOP。N=512、B=16 时全局读取从 \(2.68\times10^8\) 降到 \(1.68\times10^7\) 个元素（约 134 MB）。

但请注意"理论上"三个字：L1/L2 缓存在 `global_block` 里已经兜住了一部分复用，加上延迟隐藏等因素，实测 kernel 提速约 18%~22%（见 4.3.3 的 Carina 数据），远小于 16 倍。共享内存的价值在于把"指望缓存命中的复用"变成"保证命中片上的复用"，规模越大、缓存越兜不住时收益越明显。

同步是这套逻辑的安全带，两处 `__syncthreads()` 缺一不可：

- **栅栏 1（写入后）**：确保 block 内**所有**线程都把各自元素写进 `As/Bs` 之后，才允许任何人开读——否则可能读到别人还没写的脏数据。
- **栅栏 2（读完后）**：确保所有线程都用完当前 `As/Bs` 之后，才允许任何人进入下一 tile 迭代覆写它们——否则快线程会冲掉慢线程还在读的数据。

#### 4.2.2 核心流程

```text
每个 block 负责 C 的一个 B×B 子块，block 内 (ty,tx) 对应子块内 (ty,tx)
Csub ← 0
for 每一对 tile (As, Bs) along k 方向:            # 共 n/B 步
    As[ty][tx] ← A[当前 A 子块 + n*ty + tx]        # 每线程搬 1 个元素，共 B² 个
    Bs[ty][tx] ← B[当前 B 子块 + n*ty + tx]
    __syncthreads()                               # 栅栏 1：等全员搬完
    for k in 0..B-1:
        Csub += As[ty][k] * Bs[k][tx]             # 只碰片上共享内存
    __syncthreads()                               # 栅栏 2：等全员用完
C[子块首 + n*ty + tx] ← Csub
```

与 4.1.2 的 `global_block` 伪代码逐行对照：循环骨架一模一样，只是"内层乘加的数据来源"从全局内存换成了共享内存，外加两道栅栏。

#### 4.2.3 源码精读

`shared_block` 在 [Shmem/mm_kernel.cu:L74-L143](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L74-L143)。它与 `global_block`（L25-L72）的 L77-L107 逐行相同——游标计算、`Csub` 累加器、tile 主循环全部保留，这正是刻意设计的对照。差异从循环体内部开始：

- [L111-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L111-L115) 在循环体内声明两个共享内存数组 `__shared__ REAL As[BLOCK_SIZE][BLOCK_SIZE]` 与 `Bs`。注意：虽然声明写在了 for 循环里，**静态共享内存是 block 启动时一次性分配的**，每轮迭代只是复用（覆写）同一块存储，不存在"每轮新分配"。B=16、REAL=double 时每个 block 占用 \(2\times16^2\times8=4096\) 字节 = 4 KB。
- [L120-L121](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L120-L121) 协作装载：block 内 256 个线程每人搬 1 个 A 元素 + 1 个 B 元素，凑齐两个 16×16 tile。`A[a + wA*ty + tx]` 的下标随 tx 连续——装载访问是合并的。
- [L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L124) 栅栏 1。
- [L129-L131](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L129-L131) 内层乘加只读 `As[ty][k]`、`Bs[k][tx]`——片上命中，且 `As[ty][k]` 对同一 warp 内固定 k 的所有线程是同地址（广播）、`Bs[k][tx]` 随 tx 连续（无 bank 冲突的组织方式，详见 u4-l5）。
- [L136](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L136) 栅栏 2，然后回到 L108 进入下一对 tile。
- [L141-L142](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L141-L142) 写回 C 子块，与 `global_block` 的 L70-L71 相同。

一个值得自己动手验证的细节：装载进 `Bs` 的下标是 `B[b + wB*ty + tx]`（按 [ty][tx] 行主序），而乘加使用 `Bs[k][tx]`。展开全局下标可知 `b + wB*ty + tx` 在第 m 轮迭代对应全局元素 \(B_g[mB+ty][bx\cdot B+tx]\)，因此 `Bs[k][tx]` 恰好就是乘法需要的 \(B_g[mB+k][bx\cdot B+tx]\)——**装载方式与使用方式是自洽的**，不是笔误。建议在纸上对 m=0、B=4 的小例子推一遍。

#### 4.2.4 代码实践（修改观察型）

1. **实践目标**：亲手制造一次"缺栅栏"的错误，理解两道 `__syncthreads()` 各自拦住什么。
2. **操作步骤**（在自己的副本上做，勿改仓库源码）：
   - 编译运行基线：`cd Shmem && make && ./mm_omp_cuda.out 512`，记下输出。
   - 注释掉 [L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L124) 的第一道栅栏，重新编译运行，观察校验打印。
   - 恢复 L124，改注释 [L136](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L136) 的第二道栅栏，再编译运行。
3. **需要观察的现象**：缺任一栅栏后，程序仍正常跑完并打印性能表（校验是弱校验，见 4.3.3），但 `C[%d][%d]: ...` 的差异打印可能出现，且**多次运行结果不稳定**（竞态是否发作取决于线程调度）。
4. **预期结果**：两处都可能引发错误结果，但机理不同——缺栅栏 1 是"读到没写完的 tile"，缺栅栏 2 是"下一轮装载覆写了还在被读的 tile"。若你的机器上没有观察到差异打印，多跑几次或换 N=1024（tile 轮数更多，竞态窗口更大）。**待本地验证**（需要 GPU；现象与调度相关，不保证必现）。

#### 4.2.5 小练习与答案

**练习 1**：理论上 B=16 的分块能让全局流量降多少？B=32 呢？
**答案**：流量从 \(2N^3\) 降到 \(2N^3/B\)，即理论上降 16 倍 / 32 倍。但共享内存占用按 \(B^2\) 增长（B=32 时 \(2\times32^2\times8=16\) KB/block），block 能否驻留足够多以填满 SM 会反过来影响性能——B 不是越大越好。

**练习 2**：B=16、B=32 时每个 block 各占多少共享内存？block 内各有多少线程？
**答案**：B=16 → 4 KB 共享内存、16×16=256 线程；B=32 → 16 KB 共享内存、32×32=1024 线程。1024 恰是 CUDA 每 block 线程数上限，所以 **BLOCK_SIZE 不能取 64**（4096 线程会得到非法启动配置）。又因包装函数不检查错误（u2-l3 讲过的静默失败），取 64 时 kernel 根本没执行、程序却照常打印性能表——一个隐蔽的坑。

**练习 3**：为什么实测提速只有约 20%，而不是理论的 16 倍？
**答案**：三个原因叠加。(1) `global_block` 的 tile 化访问有很好的局部性，L1/L2 缓存已经拦截了大量重复读，DRAM 流量差异本来就没 16 倍；(2) 共享内存版本多了装载与同步的开销；(3) 两版 kernel 在这些规模下都未把 DRAM 带宽打满，瓶颈并不纯在流量。"理论公式给上限，硬件缓存与调度决定你能拿到几成"——这是解读一切分块优化时必备的谨慎。

### 4.3 mm_kernel / mm_kernel_shmem：包装函数、计时与结果解读

#### 4.3.1 概念说明

kernel 文件里的两个包装函数把"选哪个 kernel"封装成两个可从 C 调用的入口：`mm_kernel`（内部启动 `global_block`）与 `mm_kernel_shmem`（启动 `shared_block`）。host 主程序 `main` 则是实验控制器：初始化 → 串行基线 → warmup → 两段各 10 次的平均计时 → 弱校验 → 打印。这套五阶段模板在 u1-l3 已总结过，本讲关注它的**计时口径**：`read_timer` 测的是**整个包装函数**的墙钟——包含 `cudaMalloc`、H2D/D2H 拷贝与 `cudaFree`，远大于纯 kernel 时间。要看清 kernel 层的差异，必须借助 nvprof（或新一代 nsys/ncu），这正是 `testResults.txt` 存在的意义。

#### 4.3.2 核心流程

`main`（[Shmem/mm_omp_cuda.c:L61-L122](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L61-L122)）的执行顺序：

```text
N ← argv[1]
分配 A, B, C, C_shmem, C_serial（主机内存）
srand48(1<<12) 后 init 随机填充 A、B          # 固定种子，可复现
matmul_serial(N, A, B, C_serial)              # 串行参考，不计时
mm_kernel_shmem(A,B,C_shmem,N)                # warmup：消化首次 CUDA 上下文等一次性开销
计时 10 次 mm_kernel     → elapsed_cuda       # global_block
计时 10 次 mm_kernel_shmem → elapsed_shmem     # shared_block
逐元素对比 C 与 C_serial（阈值 ALLOWED_DIFF=1e-4）
打印两种实现的 ms 与 MFLOPS = 2N³/(10⁶ × 秒)
```

包装函数内部（以 `mm_kernel_shmem` 为例）就是 u2-l3 的五段式：cudaMalloc×3 → H2D×2 → `shared_block<<<n/B, n/B>, <B,B>>>` → D2H → cudaFree×3。

#### 4.3.3 源码精读

**包装函数** [Shmem/mm_kernel.cu:L145-L164](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L145-L164)（`mm_kernel`，启动 `global_block`，`global_element` 在 [L156](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L156) 被注释掉）与 [L166-L185](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L166-L185)（`mm_kernel_shmem`，启动 `shared_block`）。两者除 L157/L158 与 L178/L179 三行注释互换外**逐行相同**：L154-L155 用 `dim3` 构造 16×16 的二维 block 与 `(n/16)×(n/16)` 的二维 grid——分块粒度由此进入启动配置。

**主程序计时**在 [Shmem/mm_omp_cuda.c:L87-L98](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L87-L98)：L87 串行基线不计时；L88 以一次 `mm_kernel_shmem` 兼作 warmup；L90-L93 与 L95-L98 分别对两种实现跑 10 次取平均，用的是秒级 `read_timer`（[L21-L25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L21-L25)；L28-L32 的 `read_timer_ms` 定义了但没被使用）。

**弱校验**在 [L103-L110](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L103-L110)：只把 `C`（global_block 的结果）与 `C_serial` 逐元素比、超阈值就打印，`break` 只跳出内层循环，既不设置错误标志也不改变退出码——**且从未检查 `C_shmem`**。所以"没有打印"不等于"两个结果都对"。

**结果打印**在 [L112-L119](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_omp_cuda.c#L112-L119)，MFLOPS 按 \(2N^3/(10^6\cdot t)\) 计算。

**归档结果** [Shmem/testResults.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L1-L85) 是 Carina 集群上 BLOCK_SIZE=16 的三组 nvprof 转录，整理如下（kernel 均值取自 "GPU activities" 表）：

| N | matmul_cuda 墙钟 | matmul_shmem 墙钟 | global_block | shared_block | kernel 级提速 |
|---|---|---|---|---|---|
| 512 | 2.60 ms | 2.50 ms | 189.36 µs ([L16](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L16)) | 155.83 µs ([L17](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L17)) | 17.7% |
| 1024 | 11.10 ms | 10.20 ms | 1.594 ms ([L44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L44)) | 1.289 ms ([L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L45)) | 19.2% |
| 2048 | 47.40 ms | 39.80 ms | 11.56 ms ([L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L72)) | 9.05 ms ([L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L73)) | 21.6% |

两个结论值得反复咀嚼：

1. **kernel 级提速（18%~22%）远大于墙钟级差异（4%~16%）**，因为墙钟里混入了 HtoD/DtoH 拷贝与 cudaMalloc——N=512 时 shared_block 每次仅 155.83 µs，只占墙钟 2.5 ms 的约 6%。这与 u1-l4 在 BankRedux 上的观察一致：**比较 kernel 优劣要用 nvprof 的 kernel 时间，不要用程序打印的 time**。
2. **规模越大，共享内存优势越明显**（17.7% → 21.6%）：N 越大，`global_block` 的工作集越超出缓存兜底能力，流量差距越接近理论值；同时 kernel 占墙钟的比重也在上升。

顺带用 "Calls" 列反向核对程序结构（u1-l4 的方法）：`shared_block` 11 次 = warmup 1 次 + 计时 10 次；`global_block` 10 次 = 只在计时循环里（main 没给它 warmup）；`cudaLaunchKernel` 21 = 11+10；HtoD 拷贝 42 = 21 次调用 × 每次 2 个矩阵；`cudaMalloc` 63 = 21 × 3。数字全部对得上。

#### 4.3.4 代码实践（云数据解读型，无需 GPU）

1. **实践目标**：学会从 nvprof 转录中分离 memcpy / kernel / API 三类时间，不跑代码也能得出结论。
2. **操作步骤**：打开 [Shmem/testResults.txt:L57-L85](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/testResults.txt#L57-L85)（N=2048 段），把 HtoD、DtoH、两个 kernel 的时间各乘以调用次数得到总量，与墙钟 47.4/39.8 ms 对账。
3. **需要观察的现象**：GPU activities 合计（370.95+263.60+115.55+99.60 ≈ 849.7 ms）远大于单次墙钟——因为它是 21 次调用的累计；而 API calls 里 `cudaMalloc`（374.11 ms）与 `cudaMemcpy`（877.44 ms）的大头同样来自 63 次/42 次的累计与首次上下文创建摊销。
4. **预期结果**：分摊到每次调用后，kernel 占墙钟约四分之一，其余是拷贝与分配开销。由此回答："Shmem 优化的对象是 kernel 执行阶段，它不减少 H2D/D2H 的搬运量"——减少搬运量是单元五（HDOverlap/MiniTransfer）的主题。

#### 4.3.5 小练习与答案

**练习 1**：N=512 时墙钟只差 0.1 ms，kernel 却差约 34 µs，两者为什么不成比例？
**答案**：墙钟 2.5/2.6 ms 里 kernel 只占约 6%，其余是每次包装调用里的 cudaMalloc、两次 H2D、一次 D2H 和 cudaFree。kernel 差 33.5 µs 摊进墙钟后只表现为 0.1 ms 级别的差异（且计时分辨率与噪声也在这个量级）。

**练习 2**：为什么 main 只给 `mm_kernel_shmem` 安排了 warmup（L88），这对 `mm_kernel` 的计时有何影响？
**答案**：L88 的一次 `mm_kernel_shmem` 消化了首次 CUDA 调用的一次性开销（上下文创建、驱动初始化等），这些开销被挡在两段计时循环之外，对两个被测对象是共同的。但注意 `global_block` 本身首次启动的残余开销（如模块加载）仍落在计时循环的第 1 轮里，靠 10 次平均稀释——这是本骨架计时不严谨处之一，结论解读时应予保留（呼应 u6-l4 的方法论）。

**练习 3**：如果只看程序打印的 MFLOPS 排名，可能会对"共享内存的收益"得出什么误导性结论？
**答案**：N=512 时两版 MFLOPS 只差约 4%（103 vs 107 GFLOPS），容易误判"分块没用"；kernel 级数据却是 17.7%。口径不同结论不同——墙钟口径把拷贝/分配的固定开销掺进来稀释了优化效果。做性能归因必须用 profiler 的 kernel 时间。

## 5. 综合实践

**任务：BLOCK_SIZE × N 扫描实验，绘制"分块收益"对比表**（对应大纲指定的实践任务）。

### 5.1 实践目标

量化两个变量：分块（global_block vs shared_block）与块边长 B（8/16/32）对矩阵乘性能的影响，产出一张可放进实验报告的对比表。

### 5.2 操作步骤

1. 基线编译运行：
   ```bash
   cd Shmem
   make
   ./mm_omp_cuda.out 256    # 再依次试 512、1024
   ```
2. 确认 `BLOCK_SIZE=16`（[Shmem/mm_kernel.cu:L8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L8)）下 N=256/512/1024 都能整除（256/16=16，512/16=32，1024/16=64 ✓），记录 `matmul_cuda` 与 `matmul_shmem` 两行输出的 ms。
3. 把 L8 改为 `#define BLOCK_SIZE 8`，`make` 重新编译，重复三个 N。再改为 32，重复。**每次只改这一个变量**；注意 B=32 时每 block 已达 1024 线程上限，B=64 会静默启动失败（见 4.2.5 练习 2）。
4. 选一组最有代表性的配置（如 B=16、N=1024）补采 kernel 级数据（沿用 testResults.txt 的方式）：
   ```bash
   nvprof ./mm_omp_cuda.out 1024
   ```
   新工具链上可用 `nsys profile` / `ncu` 替代（u1-l4）。
5. 把数据填进下表（墙钟 ms 来自程序输出，kernel µs 来自 profiler）：

   | BLOCK_SIZE | N | matmul_cuda (ms) | matmul_shmem (ms) | global_block (µs) | shared_block (µs) | kernel 提速 |
   |---|---|---|---|---|---|---|
   | 16（Carina 参考） | 512 | 2.60 | 2.50 | 189.4 | 155.8 | 17.7% |
   | 16（Carina 参考） | 1024 | 11.10 | 10.20 | 1594.2 | 1288.5 | 19.2% |
   | 16（Carina 参考） | 2048 | 47.40 | 39.80 | 11555 | 9054.9 | 21.6% |
   | 8 | … | 待本地验证 | 待本地验证 | 待本地验证 | 待本地验证 | |
   | 16 | … | 待本地验证 | 待本地验证 | 待本地验证 | 待本地验证 | |
   | 32 | … | 待本地验证 | 待本地验证 | 待本地验证 | 待本地验证 | |

### 5.3 需要观察的现象

1. 同一 B 下，kernel 级提速是否随 N 增大而扩大（对照 Carina 的 17.7%→21.6% 趋势）。
2. 同一 N 下，B=8/16/32 三档的 shared_block 时间如何变化：流量理论值是 \(2N^3/B\)，但 B 增大会减少 block 数量（\(N/B\) 的平方递减）、改变 SM 驻留情况，两条曲线会"打架"，通常存在一个中间最优值。
3. `global_block` 也使用 BLOCK_SIZE 划分 tile（[L45](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L45)、[L51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shmem/mm_kernel.cu#L51)），因此它的性能也会随 B 摆动——对照组并非恒定。

### 5.4 预期结果与报告要求

预期 shared_block 在所有配置下都不慢于 global_block，且优势随 N 扩大；B 的最优值依 GPU 而定（Carina 上 16 是出厂默认）。报告需包含：对比表、一句"瓶颈→改法→收益"总结（例：全局内存重复读 \(2N^3\) → tile 搬入共享内存降到 \(2N^3/B\) → kernel 提速约 20%，墙钟收益受拷贝摊薄）、以及你观察到的 B 的最优值和一句解释。若无 GPU 环境，第 2 步起均标注"待本地验证"，可先用表中 Carina 参考数据完成"解读型"报告（4.3.4 的方法）。

## 6. 本讲小结

- 朴素矩阵乘每线程读 A 一整行 + B 一整列，全局读取总量 \(2N^3\)，平均每读 1 个元素只做 1 次 FLOP，是典型的访存受限反模式。
- **分块（tiling）+ 共享内存**把要复用的 B×B 子块搬进片上 `__shared__` 数组，流量降到 \(2N^3/B\)（理论降 B 倍）；`global_block` 与 `shared_block` 除数据落点外逐行同构，构成干净的单变量对照。
- 两道 `__syncthreads()` 是正确性的安全带：写入后等全员搬完、用完后才许覆写，缺任何一道都是竞态。
- 实测收益（Carina：kernel 级 18%~22%）远小于理论值，因为 L1/L2 已兜住部分复用且存在装载/同步开销；规模越大收益越明显。
- 墙钟口径含 cudaMalloc/拷贝/free，N=512 时 kernel 只占约 6%——比较 kernel 优劣必须用 nvprof 的 kernel 时间；nvprof 的 Calls 列可反向核对程序结构（10 vs 11 次）。
- BLOCK_SIZE 是全局单点配置（mm_kernel.cu:L8）：B=32 触及每 block 1024 线程上限，B=64 会静默启动失败；N 必须整除 B，否则边缘元素无人计算。

## 7. 下一步学习建议

本讲展示了共享内存的"流量复用"价值，沿以下方向继续：

1. **u4-l2（CoMem·AXPY 篇）**：分块的装载阶段之所以高效，靠的是 warp 内连续地址的**合并访问**；下一讲以最简单的 AXPY 把 coalescing 讲透，再在 u4-l3（CoMem·SpMM 篇）见到稀疏场景下合并性如何变差。
2. **u4-l5（BankRedux）**：共享内存内部按 32 bank 组织，本讲的 `Bs[k][tx]` 访问恰好无冲突，但归约树稍一变形就会产生 2 路 bank 冲突——那是共享内存的"第二宗罪"。
3. **u4-l7（GSOverlap）**：把"全局→共享"的装载从同步拷贝升级为 `__pipeline_memcpy_async` 多级流水，让搬运与计算重叠，是本讲 tile 循环的进阶形态。
4. 动手方向：把本讲的 `shared_block` 改造成"每线程算 2×2 个元素"的版本（装载量不变、算术强度翻倍），与 u6-l1 的归约优化阶梯中"每线程处理多个元素"的思想相互印证。
