# UniMem：统一内存与低访问密度工作负载

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `cudaMallocManaged` 分配统一内存（Unified Memory，UM），并说清它与 `cudaMalloc + cudaMemcpy` 这条离散内存路径的本质区别。
2. 解释「按需缺页迁移」：数据不整体搬运，而是 GPU 首次访问某页时由驱动把该页从主机内存搬进显存。
3. 用**访问密度**（access density）这个单一变量解释：为什么数据用得越稀疏，统一内存越占优、整体 `cudaMemcpy` 越亏。
4. 读懂 `nvprof` 的 Unified Memory 迁移表（迁移次数、平均粒度、总量），并用它核实「只搬必要页」到底搬了多少。
5. 指出这个基准计时口径上的三处不对称（哪些开销被排除在统一路径计时之外），养成读基准先看口径的习惯。

本讲对应 README 中第三类性能挑战（CPU–GPU 数据搬运）中的第三个反模式：

> **UniMem：低访问密度（Low memory access density）→ 把数据放进统一内存，只拷贝必要的页**
> （[README.md:88-92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L88-L92)）

---

## 2. 前置知识

阅读本讲前，你应当已学过 [u2-l3]（`cudaMalloc`/`cudaMemcpy`/同步的基础生命周期）。在此之上，本讲只需要四个新概念，都用操作系统课里的老朋友来类比：

**（1）两个内存世界与「搬运」问题。**
主机（CPU）内存和设备（GPU）显存是两个独立的物理地址空间，PCIe 是它们之间唯一的桥。u2-l3 学过的离散内存（discrete memory）模式是「程序员全权负责」：`cudaMalloc` 在设备侧分配、`cudaMemcpy` 显式搬运、方向自己写对。它的隐含假设是：**数据迟早会被整体用上，所以整体搬一次是划算的**。

**（2）页（page）与按需缺页（demand paging）。**
操作系统把虚拟内存切成固定大小的页（典型 4KB）；程序访问的地址不在物理内存里时，硬件触发「缺页异常」，由系统把那一页从磁盘调进来。统一内存把这套机制搬到了 CPU–GPU 之间：`cudaMallocManaged` 分配的缓冲区**主机和设备用同一个指针都能访问**，页当前住在哪一侧由 CUDA 运行时跟踪；kernel 首次访问一个不在显存的页时，GPU 触发缺页，驱动把该页（通常一次一批）从主机内存经 PCIe 搬进显存，然后访问继续。这就是「只拷贝必要的页」的硬件机制。

**（3）访问密度（access density）。**
设数组有 \( n \) 个元素，kernel 实际只碰了其中 \( m \) 个，则访问密度

\[
\rho = \frac{m}{n}
\]

本讲的 kernel 按 stride 抽样：只读 \( x[0], x[s], x[2s], \dots \)，于是 \( m = n/s \)、\( \rho = 1/s \)。密度越低，「为了 \( m \) 个有用元素而搬运整个 \( n \)」的浪费倍数就是 \( s \)。很多真实负载天生就是低密度的：稀疏矩阵的零元素、大表中命中少量记录的索引查找、只取样少数字节的可视化/统计。

**（4）迁移粒度。**
「只搬必要的页」并不等于「只搬 4 个字节」。缺页迁移的最小单位是页（4KB），驱动还常常一次迁移页周围的一小批（后文会看到实测约 32KB 一批）。所以统一内存搬运量的下界是「触碰元素数 × 页大小」，而不是「触碰字节数」。

一个需要提前打好的预防针：统一内存**不是免费的性能开关**。密度高时，缺页驱动的一片一片搬运比一次流水线化的 `cudaMemcpy` 更慢；密度低时它才反超。本讲的全部内容就是在量化这条交叉曲线。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [UniMem/LowAccessDensityTest_cuda.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L1-L165) | 单文件基准：kernel + 两条主机路径 + `main` | 全部核心逻辑都在这一个文件 |
| [UniMem/LowAccessDensityTest.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest.h#L1-L15) | 接口头文件 | 声明与实现**已经脱节**（见 4.4 的较真时刻） |
| [UniMem/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L28) | 实验一：固定 n=134217728，stride 从 1 倍增到 n | 密度扫描 |
| [UniMem/test2.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test2.sh#L1-L21) | 实验二：n 与 stride 同步倍增，触碰元素数恒为 1024 | 固定「有用工作量」、放大「无谓搬运」 |
| [UniMem/LowAccessDensityTest_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt) | Carina（Tesla V100-PCIE-32GB）上的归档结果 | 无 GPU 时的「云实验数据」 |
| [UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt) | `test2.sh` 对应的归档结果 | 交叉验证实验二 |
| [UniMem/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/Makefile#L1-L2) | 一行式编译 | 无特殊架构/链接选项，UM 由 CUDA 6+ 默认支持 |

`LowAccessDensityTest_omp.c` 是一份 OpenMP/OpenMP-offload 的亲戚实现，**不被 Makefile 编译**，且语义与 CUDA 版不同（它按 `i += stride` 写 `y[i] += a*x[i]`，精度还是 double），本讲只作对照提及，不做依据。

---

## 4. 核心概念与源码讲解

### 4.1 模块一：低访问密度工作负载——stride 抽样 kernel

#### 4.1.1 概念说明

要比较两种数据摆放策略，先要有一个「搬运全量、使用少量」的工作负载。本基准的构造极其直白：一个长度为 \( n \) 的 `float` 数组 \( x \)，kernel 只读取下标为 stride 整数倍的那批元素，每个被读的元素乘以标量 \( a \) 后写入输出数组 \( y \) 的对应位置：

\[
y[i] = a \cdot x[i \times s], \quad 0 \le i < \lfloor n/s \rfloor
\]

- 计算量：\( \lfloor n/s \rfloor \) 次乘加——随 stride 增大而**缩小**；
- 若走离散内存，搬运量：永远是全量 \( 4n \) 字节——**与 stride 无关**；
- 有用字节（真正被读的）：\( 4\lfloor n/s \rfloor \) 字节。

「搬运量恒定、有用量随 stride 缩小」——浪费倍数恰好是 \( s \)。这就是反模式：**低访问密度负载 + 整体搬运**。

#### 4.1.2 核心流程

```text
main
 ├── init(x, n)                       # drand48 随机数填充整个 x（一次性，不计入成绩）
 ├── serial(x, y, n, a, stride)       # CPU 参考实现（算完就扔，见 4.1.3）
 ├── LowAccessDensityTest_cuda_discrete_memory(...)   # 预热 1 次
 ├── 计时 100 次离散路径 → elapsed
 └── 累加 100 次统一路径的内部计时 → elapsed_unified
```

kernel 本体只有两行，执行流程：

1. 计算全局线程编号 `i = blockDim.x * blockIdx.x + threadIdx.x`（u2-l1 学过的标准公式）；
2. 守卫条件 `i < (n/stride)`：只有前 \( \lfloor n/s \rfloor \) 个线程干活；
3. 干活的线程读 `x[i*stride]`（稀疏读）、写 `y[i]`（连续写）。

#### 4.1.3 源码精读

先看常量与 kernel 本体（[UniMem/LowAccessDensityTest_cuda.cu:23-26](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L23-L26) 定义 `REAL float`、默认 `VEC_LEN 102400000`、默认 `STRIDE 1024`，均可被命令行覆盖）：

[UniMem/LowAccessDensityTest_cuda.cu:46-52](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L46-L52) —— 整个基准唯一的 kernel：下标 `i` 的线程读第 `i*stride` 个元素、写到 `y[i]`。守卫条件里的 `n/stride` 是整数除法（向下取整）。

```cuda
__global__
void
LowAccessDensityTest_cudakernel(REAL* x, REAL* y, int n, REAL a, int stride)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < (n/stride)) y[i] = a*x[i*stride];
}
```

三个值得停下来说的细节：

- **网格按全量 n 配置，而不是按 n/stride。** 两条路径都用 `<<<(n+255)/256, 256>>>` 启动（[L60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L60)、[L83](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L83)）。n=134217728 时是 524288 个 block、1.34 亿线程；当 stride=33554432 时其中只有 4 个线程真正访存。由于两条路径的启动配置完全相同，这一浪费在对照中是对称的、不影响结论，但它解释了为什么空转路径的 kernel 也有约 0.82ms 的固定耗时。
- **`i*stride` 不会溢出。** `i` 与 `stride` 都是 `int`，乘积上界 \( (n/s-1)\cdot s < n = 134217728 < 2^{31} \)，安全。
- **串行参考与 check 都是「摆设」。** [L96-102](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L96-L102) 的 `serial` 在 [main L139](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L139) 被调用一次，但 [L105-114](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L105-L114) 定义的 `check` **从未被调用**，`y_cuda`/`y_cuda_unified` 也从未被回填（两条路径的 D2H 拷贝都被注释掉了，见 [L62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L62)、[L86](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L86)）。这个基准只测搬运与访问，不做正确性验证——对照 u2-l4 的方法论，这是骨架上的一个缺口（kernel 算术极简，风险不大，但读代码时要心里有数）。

#### 4.1.4 代码实践

**实践目标**：在跑任何命令之前，先用纸笔算出本讲综合实践要用的五个配置的「账本」，并做出预测。

**操作步骤**：

1. 取 n = 134217728（即 \( 2^{27} \)，float 数组共 512MB），stride 依次为 1、64、1024、65536、33554432；
2. 对每个 stride 计算：触碰元素数 \( \lfloor n/s \rfloor \)、有用字节、访问密度 \( \rho \)、以及「离散搬运量 ÷ 有用字节」的浪费倍数；
3. 先写下你的预测：两种路径的时间随 stride 各自怎么变？交叉点大概在哪？

**需要观察的现象**：浪费倍数一列应该恰好等于 stride；有用字节从 512MB 一路跌到 16 字节。

**预期结果**（可对照下表核验）：

| stride | 触碰元素数 | 有用字节 | 密度 \( \rho \) | 离散搬运/有用 |
| --- | --- | --- | --- | --- |
| 1 | 134217728 | 512 MB | 100% | 1× |
| 64 | 2097152 | 8 MB | 1.56% | 64× |
| 1024 | 131072 | 512 KB | 0.098% | 1024× |
| 65536 | 2048 | 8 KB | 0.0015% | 65536× |
| 33554432 | 4 | 16 B | \( 3\times10^{-8} \) | 33554432× |

注意最后一行：为了读 4 个 `float`（16 字节），离散路径要把 512MB 搬过 PCIe。这就是「低访问密度」的极端形态。

#### 4.1.5 小练习与答案

**练习 1**：若把 stride 取到 134217728（等于 n），kernel 实际做多少工作？test.sh 里有没有这一档？

答案：`n/stride = 1`，只有线程 0 读 `x[0]` 写 `y[0]`。有——[test.sh:28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L28) 的最后一行就是 `./LowAccessDensityTest_cuda 134217728 134217728`，此时为读 4 个字节搬运 512MB，浪费倍数达到 \( 1.34\times10^8 \)。

**练习 2**：为什么 kernel 写 `y[i]`（连续）而读 `x[i*stride]`（稀疏）？如果把两者反过来，本讲要测的东西会变吗？

答案：`y` 是输出、只在设备侧产生和消耗（D2H 被注释掉），它的访问模式不影响搬运量；被研究的对象只有输入数组 `x` 的摆放策略。若反过来（稀疏写、连续读），被稀疏触碰的变成输出数组，而输出恰恰不适合放统一内存——每次写缺失同样触发迁移，还要考虑写回（设备→主机）方向的脏页问题，实验就不再能干净地隔离「只读输入的按需拉入」这一个变量。

---

### 4.2 模块二：离散内存路径——整体 cudaMemcpy 的「全额搬运」

#### 4.2.1 概念说明

`LowAccessDensityTest_cuda_discrete_memory` 是 u2-l3 学过的标准五段式包装：分配 → H2D → kernel → 同步 → 释放。它的关键特征是**搬运量写死为全量**：`cudaMemcpy(d_x, x, n*sizeof(REAL), ...)`，与 stride 完全无关。哪怕 kernel 只读 4 个字节，也要先把 512MB 搬完。同时拷贝与计算是**串行**的：`cudaMemcpy` 返回后 kernel 才启动，两者不能重叠。

这就是本基准的反模式侧。它的成本模型几乎是一条水平线：

\[
T_{\text{discrete}} \approx \underbrace{\frac{4n}{B_{\text{pcie}}}}_{\text{全量拷贝}} + \; T_{\text{alloc}} + \; T_{\text{kernel}}(\lfloor n/s \rfloor) + \; T_{\text{free}}
\]

唯一随 stride 变化的项 \( T_{\text{kernel}} \) 在稀疏时趋近一个约 0.82ms 的固定值（见 4.1.3 的网格空转开销），所以整条曲线应当近似平坦。

#### 4.2.2 核心流程

```text
LowAccessDensityTest_cuda_discrete_memory(x, y, n, a, stride)
 ├── cudaMalloc(d_x, n*4)            # 全量输入
 ├── cudaMalloc(d_y, (n/stride)*4)   # 输出只按触碰量分配
 ├── cudaMemcpy(d_x ← x, n*4, H2D)   # ★ 全量搬运，与 stride 无关
 ├── kernel<<<(n+255)/256, 256>>>    # 稀疏读 + 连续写
 ├── cudaDeviceSynchronize()         # 计时栅栏
 ├── // cudaMemcpy(y ← d_y, D2H)     # 被注释：结果不回填
 └── cudaFree × 2
```

#### 4.2.3 源码精读

[UniMem/LowAccessDensityTest_cuda.cu:54-66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L54-L66) —— 离散路径全貌：分配输入/输出、**全量** H2D 拷贝、启动 kernel、同步、释放。

```c
void LowAccessDensityTest_cuda_discrete_memory(REAL* x, REAL* y, long int n, REAL a, int stride) {
  REAL *d_x, *d_y;
  cudaMalloc(&d_x, n*sizeof(REAL));
  cudaMalloc(&d_y, (n/stride)*sizeof(REAL));

  cudaMemcpy(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice);
  LowAccessDensityTest_cudakernel<<<(n+255)/256, 256>>>(d_x, d_y, n, a, stride);
  cudaDeviceSynchronize();
  //cudaMemcpy(y, d_y, (n/stride)*sizeof(REAL), cudaMemcpyDeviceToHost);

  cudaFree(d_x);
  cudaFree(d_y);
}
```

注意对照**归档数据**这条水平线长什么样（Carina，普通运行，[LowAccessDensityTest_cuda.output.carina.txt:5-87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5-L87)）：stride 从 1 扫到 134217728，离散路径时间始终在 146–154ms 区间抖动——有用字节从 512MB 掉到 4 字节，时间纹丝不动。由 `512MB / 147ms ≈ 3.6 GB/s` 可估算这条拷贝通路的有效带宽（远低于 PCIe 理论峰值，因为 `x` 是 pageable 内存、且每轮还含分配/释放开销；这与 u5-l1 的结论一致）。

`nvprof` 概览（[同文件:99-108](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L99-L108)）也印证了结构：每次进程运行 `[CUDA memcpy HtoD]` 恰好 11 次 = 1 次预热 + 10 次计时的离散路径（统一路径的迁移不走 `cudaMemcpy`），每次约 136ms，占 GPU 时间的大头。

#### 4.2.4 代码实践

**实践目标**：亲手验证「离散路径时间与 stride 无关」。

**操作步骤**（需要 NVIDIA GPU；没有 GPU 则直接用归档数据完成第 3 步）：

1. `cd UniMem && make`（即 `nvcc -o LowAccessDensityTest_cuda LowAccessDensityTest_cuda.cu`，[UniMem/Makefile:1-2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/Makefile#L1-L2)，无任何特殊选项）；
2. 依次运行 `./LowAccessDensityTest_cuda 1 134217728`、`./LowAccessDensityTest_cuda 1024 134217728`、`./LowAccessDensityTest_cuda 134217728 134217728`；
3. 只看每行输出里的 `(Discrete Memory)` 时间，与上一步预测的水平线对比。

**需要观察的现象**：三个相差 8 个数量级的有用工作量的配置，离散时间几乎相同；每次运行开头都会打出一行 `Usage: Low Access Test <n>`（它写在 [L124](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L124)，且**无条件打印**、走 stderr——这就是归档文件里 stdout/stderr 交错的来源，u1-l4 已见过）。

**预期结果**：Carina 上为 121–154ms 的平台；你的机器上具体数值会不同（取决于 PCIe 代数与宽度），但「平台」形状应当复现。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`d_y` 为什么按 `(n/stride)` 分配而不是 `n`？

答案：kernel 只写 `y[0..n/stride)`，输出规模由触碰量决定；按 `n` 分配会在 stride 大时浪费显存（stride=134217728 时是 512MB vs 4 字节）。注意这是**输出**的按需伸缩，与输入的搬运策略是两回事。

**练习 2**：离散路径每轮计时包含 `cudaMalloc × 2` 和 `cudaFree × 2`。这对后面的公平比较有何影响？

答案：nvprof 显示本进程中 `cudaMalloc` 32 次共 341ms、`cudaFree` 42 次共 334ms（[同文件:106-107](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L106-L107)），均值约 8–11ms/次（被首次调用的上下文创建拉高）。离散路径的计时把分配/释放**全部计入**，而 4.3 将看到统一路径的计时**排除了** `cudaFree` 和主机侧填充——这是读结论前必须知道的口径差。

---

### 4.3 模块三：统一内存路径——cudaMallocManaged 与按需缺页迁移

#### 4.3.1 概念说明

`LowAccessDensityTest_cuda_unified_memory` 是本讲的优化侧。三个新角色：

- **`cudaMallocManaged(&p, bytes)`**：分配一段统一内存。返回的指针主机和设备**都能直接解引用**，不需要为它写 `cudaMemcpy`，也没有「host 指针 / device 指针」之分——u2-l3 那套「两个世界、两个指针」的纪律在这里被刻意放松了。
- **按需迁移**：分配完成时数据并不在显存。kernel 访问 `x2` 中不在显存的页时触发 GPU 缺页，驱动把一批页 H2D 搬入。搬运量 ∝ 触碰的页数，而不是数组大小——这正是 README 说的「只拷贝必要的页」。
- **迁移可与计算重叠**：缺页是 kernel 执行期间逐批发生的，先到的页先被算，后面的页在飞；而离散路径是「拷完→同步→再算」的纯串行。这是统一内存在中等密度下就能反超的重要原因。

它的成本模型是另一条曲线：

\[
T_{\text{um}} \approx \underbrace{T_{\text{mallocManaged}}}_{\approx 2.1\text{ms}} + \; T_{\text{fault}}\Big(\big\lfloor \tfrac{n}{s} \big\rfloor\Big) + \; T_{\text{kernel}} + \; T_{\text{alloc}}(d_y)
\]

密度高时 \( T_{\text{fault}} \) 要把几乎整个数组一页页搬过去，比一次大块 `cudaMemcpy` 更贵（还有反复缺页的调度开销），所以 **stride=1 时统一内存反而更慢**；密度低时 \( T_{\text{fault}} \) 趋近于零，只剩分配和 kernel 的固定开销。

#### 4.3.2 核心流程

```text
LowAccessDensityTest_cuda_unified_memory(x, y, n, a, stride)   →  返回本轮耗时
 ├── t0: 计时开始(仅分配)
 ├── cudaMallocManaged(&x2, n*4)     # 统一内存分配，本身只建页表，约 2.1ms
 ├── t1: 计时结束 → elapsed1
 ├── memcpy(x2, x, n*4)              # ★ 主机侧填充：数据先落在主机内存，不计时（作者注释：should not count）
 ├── t2: 计时开始
 ├── cudaMalloc(&d_y, (n/stride)*4)  # 输出仍是离散设备内存
 ├── kernel<<<(n+255)/256, 256>>>(x2, d_y, ...)   # GPU 首次触碰 x2 的页 → 缺页 → 按批 H2D 迁移
 ├── cudaDeviceSynchronize()         # 等全部缺页迁移 + 计算完成
 ├── t3: 计时结束 → elapsed2
 ├── cudaFree(x2); cudaFree(d_y)     # ★ 不计时
 └── return elapsed1 + elapsed2
```

#### 4.3.3 源码精读

[UniMem/LowAccessDensityTest_cuda.cu:69-92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L69-L92) —— 统一路径全貌。它自带计时器并返回耗时（`main` 只负责把 100 轮的返回值累加取平均）。

```c
double LowAccessDensityTest_cuda_unified_memory(REAL* x, REAL* y, long int n, REAL a, int stride) {
  double elapsed1 = read_timer_ms();
  REAL *x2;
  cudaMallocManaged(&x2, n*sizeof(REAL));          // 计时：只测分配
  elapsed1 = (read_timer_ms() - elapsed1);

  //initial unified memory, should not count time here
  memcpy(x2, x, n*sizeof(REAL));                   // ★ 主机侧填充，刻意排除在计时外

  double elapsed2 = read_timer_ms();
  REAL *d_y;
  cudaMalloc(&d_y, (n/stride)*sizeof(REAL));       // 输出仍是普通设备内存
  LowAccessDensityTest_cudakernel<<<(n+255)/256, 256>>>(x2, d_y, n, a, stride);
  cudaDeviceSynchronize();                          // 缺页迁移发生在这段窗口内
  elapsed2 = (read_timer_ms() - elapsed2);
  ...
  return elapsed1 + elapsed2;
}
```

读这段代码要抓三个要点，其中两个是「口径」：

1. **`x2` 用 `cudaMallocManaged` 分配，kernel 直接拿主机侧填充过的指针访问**——没有 H2D `cudaMemcpy`。迁移由 kernel 执行中的缺页驱动，全部落在 `elapsed2` 的窗口内，所以「按需搬运」的成本确实被测到了。
2. **主机侧 512MB 的 `memcpy(x2, x, ...)` 被刻意不计入**（注释原话 `should not count time here`）。作者的意图是建模「数据已经在统一内存里」这一状态；但要清楚，这一步真实发生了——它把 `x2` 的各页落到**主机内存**（并产生约 15355 次 CPU 缺页，见归档数据），随后的 kernel 再把这些页拉回设备。若把这笔主机侧填充也算进统一路径的总成本，UM 的优势会缩小（它是一次 DRAM→DRAM 拷贝，约几十毫秒量级，不走 PCIe，但仍非零）。
3. **每轮都重新分配、重新填充、重新释放 `x2`**。`main` 以 `num_runs = 100` 反复调用（[L142-154](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L142-L154)），于是**每一轮都重演一遍完整的「主机落页 → GPU 缺页拉入」**，缺页成本没有被缓存掉——这正好让按需迁移的代价充分暴露在平均值里。

再看 `nvprof` 专属的 **Unified Memory 迁移表**，这是本基准最宝贵的测量（普通时间打印之外的「搬运账本」）。以 stride=1 为例（[LowAccessDensityTest_cuda.output.carina.txt:118-123](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L118-L123)）：10 轮统一路径共发生 63050 次 H2D 迁移、平均 45KB/次、总量 2.706GB、伴随 6136 个 GPU 缺页组和 15355 次 CPU 缺页。

而稀疏端是另一幅图景（stride=134217728，触碰 1 个元素，[同文件:901-906](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L901-L906)）：全程只有 **20 次迁移、每次 32KB、总量 640KB**——对比离散路径同样的 512MB。把几个稀疏档位排成表（总量 ÷ 10 轮 = 每轮迁移量），会看到一条漂亮的规律：

| stride | 触碰元素/轮 | 迁移次数/轮 | 平均粒度 | 每轮迁移量 | 有用字节 |
| --- | --- | --- | --- | --- | --- |
| 1 | 134217728 | ~6305 | 45.0KB | ~270MB | 512MB |
| 65536（[L582-587](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L582-L587)） | 2048 | ~4096 | 32.0KB | ~128MB | 8KB |
| 33554432（[L843-848](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L843-L848)） | 4 | 8 | 32.0KB | ~256KB | 16B |
| 134217728（[L901-906](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L901-L906)） | 1 | 2 | 32.0KB | ~64KB | 4B |

**规律**：在足够稀疏的档位（stride ≥ 4194304，即元素间距 ≥ 16MB），迁移次数/轮 ≈ 2 × 触碰元素数，每次恰好 32KB——**每个被触碰的元素平均拉来约 64KB**。这说明：① 迁移粒度远大于 4KB 的页（驱动按批搬运，本例约 32KB/批，原因属驱动与架构实现细节，待确认）；② 即便如此，每轮迁移量仍比全量 512MB 小三到四个数量级，且随触碰数线性收缩。「只拷贝必要的页」中的「必要」是以批为粒度近似的，不是以字节为粒度。

最后补一个工程常识（**示例代码**，非项目内容）：生产代码里若已知数据即将被设备整段使用，通常先用 `cudaMemPrefetchAsync(x2, bytes, deviceId, stream)` 把迁移一次性做好、避免运行期缺页，或用 `cudaMemAdvise` 给驱动提示。本基准刻意**不用**预取——它要测的就是「不预取、纯按需」这条路的上限与下限。

#### 4.3.4 代码实践

**实践目标**：从归档的 `nvprof` 迁移表里亲手算出「每元素迁移量」，验证 64KB 规律。

**操作步骤**：

1. 打开 [LowAccessDensityTest_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt)，定位 stride=2097152、4194304、8388608、16777216 四档的 `Unified Memory profiling result` 小节（[L727-732](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L727-L732)、[L756-761](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L756-L761)、[L785-790](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L785-L790)、[L814-819](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L814-L819)）；
2. 对每档记录 `Count` 与 `Avg Size`，除以 10（统一路径共 10 轮）得到每轮迁移次数与迁移量；
3. 用 \( n/s \) 算出每轮触碰元素数，再算「迁移量 ÷ 触碰元素数」。

**需要观察的现象**：四档的迁移 Count 分别约为 1280、640、320、160——每档减半，与触碰元素数严格同步；`Avg Size` 恒为 32.000KB。

**预期结果**：迁移次数/轮 ≈ 2×触碰元素数，每元素迁移量 ≈ 64KB，与 4.3.3 表格一致。全程只需读文件与算术，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么统一路径里 `d_y` 仍用 `cudaMalloc` 而不一起 `cudaMallocManaged`？

答案：本基准研究的是「只读输入的按需拉入」。`d_y` 是纯设备侧的输出（写完即弃、D2H 被注释），放进统一内存只会引入写缺失与脏页写回的干扰，把它留在设备内存使变量隔离更干净。这也提示我们：统一内存可以和离散内存**混用**，不是非此即彼的全局选择。

**练习 2**：`main` 里统一路径没有像离散路径那样先做一次预热调用（对照 [L145](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L145) 与 [L152-154](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L152-L154)）。这对结果有什么影响？

答案：统一路径的第一轮要承担 `cudaMallocManaged` 的首次调用、驱动内部数据结构初始化等一次性开销，并被平均进 100 轮里。缓解因素是离散路径的预热已经把 CUDA 上下文建好了（两路径共享同一上下文），所以剩余的首次开销主要是 UM 子系统的，量级约毫秒级（nvprof 中 `cudaMallocManaged` 均值 2.1ms 已含此效应）。严格做报告时应给统一路径也加一次预热再计时。

**练习 3**：如果给 `x2` 加上 `cudaMemPrefetchAsync`（迁移全部 512MB 到设备后再启动 kernel），stride 扫描的结论会怎么变？

答案：统一路径退化成「显式全量预取」，时间曲线会变得和离散路径差不多平坦——按需缺页带来的「随密度自动缩放搬运量」的好处被取消，只剩「拷贝与计算无法重叠程度不同」的差别。UM 的价值恰恰在于**不写任何搬运代码**时由密度自动决定搬运量；一旦访问模式已知，显式预取/显式 `cudaMemcpy` 往往更快。

---

### 4.4 模块四：stride 扫描——实验设计与 crossover 解读

#### 4.4.1 概念说明

前两个模块给出了两条成本曲线，本模块把它们放到同一张图上，找**交叉点（crossover）**：密度低于某个 \( \rho^* \) 时统一内存占优。两个脚本代表两种实验设计：

- **test.sh（扫描密度）**：固定 n=134217728，stride 从 1 倍增到 n。回答「在多大 stride 下 UM 开始划算」。
- **test2.sh（固定有用工作量）**：n 与 stride 同步倍增，使 \( \lfloor n/s \rfloor \equiv 1024 \) 恒定、有用字节恒为 4KB，而数组足迹从 4KB 涨到 4GB。回答「计算量不变时，无谓搬运随足迹如何放大」。这是把「浪费倍数」单独拉出来放大的对照实验。

#### 4.4.2 核心流程

理想化的字节模型（把两条路径都折算成「过 PCIe 的字节」）：

\[
B_{\text{discrete}} = 4n, \qquad B_{\text{um}} \approx \min\!\big(4n,\; g \cdot \lfloor n/s \rfloor\big)
\]

其中 \( g \) 是每触碰元素的迁移量（4.3.3 实测约 64KB，密度高时因批间复用而更小）。两者字节相等的理论分界在 \( g\lfloor n/s\rfloor = 4n \)，即 \( s \approx g/4 = 16384 \)。但**时间**的交叉点会比字节的交叉点更早出现，原因有二：缺页迁移与 kernel 执行天然重叠（离散路径的拷贝与 kernel 串行）；以及 4.2/4.3 指出的口径差（统一路径不计时 `cudaFree` 与主机侧填充）。

#### 4.4.3 源码精读

[UniMem/test.sh:1-28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L1-L28) —— 实验一：28 行每行一个 stride，n 固定 134217728。命令行格式是 `argv[1]=stride`、`argv[2]=n`（[LowAccessDensityTest_cuda.cu:125-130](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L125-L130)）。

[UniMem/test2.sh:1-21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test2.sh#L1-L21) —— 实验二：n 与 stride 同步翻倍，`n/stride` 恒等于 1024。

归档结果（Carina / Tesla V100-PCIE-32GB，普通运行）节选（[LowAccessDensityTest_cuda.output.carina.txt:5-87](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5-L87)）：

| stride | 密度 | Discrete (ms) | Unified (ms) | 谁快 |
| --- | --- | --- | --- | --- |
| 1 | 100% | 121.54 | 172.70 | 离散 +42% |
| 2 | 50% | 153.58 | 157.30 | 基本打平 |
| **4** | **25%** | **151.22** | **137.52** | **交叉点落在 2→4 之间** |
| 64 | 1/64 | 149.64 | 96.46 | 统一 |
| 1024 | ~0.1% | 147.40 | 90.82 | 统一 |
| 65536 | 1/65536 | 150.26 | 42.12 | 统一 |
| 33554432 | 4 个元素 | 148.78 | 1.46 | 统一快 ~100× |
| 134217728 | 1 个元素 | 147.24 | 1.08 | 统一快 ~136× |

三个层次的解读：

- **交叉点在 stride 2→4 之间（密度 25%–50%）**，远早于字节模型的预测（stride≈16384）。差值来自「迁移与计算重叠」和计时口径（统一路径不计时 `cudaFree`、主机填充）。这提醒我们：**字节账本解释趋势，时间账本才决定胜负**，两者都要看。
- **两条端点**：stride=1 时 UM 慢 42%（缺页式搬运跑不过流水线大块拷贝，页还在两侧来回折腾）；stride=134217728 时为读 4 个字节，离散仍坚持搬运 512MB，慢 ~136 倍。
- **profiler 会挪动交叉点**。归档文件里第二段是套着 `nvprof` 跑的同一扫描（[L94 起](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L94)）：stride=1 时 140.70 vs 236.20，stride=4 时 155.90 vs 249.70，直到 stride=8→16（189.60 vs 158.20）统一才反超。原因：profiler 对每次缺页迁移都要记账，统一路径按迁移次数付费（stride=1 有 6 万多次迁移），被惩罚得更重。**工具改变了结论的数值（交叉点从 ~4 挪到 ~16），但没有翻转结论的形状**——这正是 u1-l4「测量口径决定你能看到什么」的又一例。

实验二的归档结果（[LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt:5-66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda_fixed_access_time.output.carina.txt#L5-L66)）同样干净：有用工作量恒定（每轮触碰 1024 个元素），足迹从 4KB 涨到 4GB，离散时间从 0.40ms 线性涨到 1199.85ms（约 3000 倍），统一时间只从 0.35ms 涨到 104.25ms（约 300 倍）——且中后段差距越拉越大（最后一档 1199.85 vs 104.25）。「无谓搬运」被单独放大时，统一内存的优势随数组规模单调扩大。

两个「较真时刻」（读脚本与归档时值得养成的小习惯）：

- [test.sh:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/test.sh#L21) 写的是 `1048567`，而上下文全是 2 的幂——它比 \( 2^{20}=1048576 \) 少 9。后果轻微（\( \lfloor n/s \rfloor \) 仍是 128，只是元素间距略变），但说明扫描表是手写的，抄数据时要核验。
- 归档文件两段的输出格式不同：第一段有 `stride:1:` 字段（[L5](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L5)），`nvprof` 段没有（[L97](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L97)）。对照 [LowAccessDensityTest_cuda.cu:156-157](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.cu#L156-L157) 的 `printf`（含 `stride:%d`）可知：**`nvprof` 段来自一个更早版本的可执行文件**（其 prompt 目录也换成了 `LowAccessDensity2`）。两段数值只能分别作参考，不能逐行相减。这与 u5-l3 的教训一致。

另外一个骨架层面的发现：[UniMem/LowAccessDensityTest.h:10-11](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest.h#L10-L11) 声明的 `LowAccessDensityTest_cuda` 与 `LowAccessDensityTest_cuda_unified` 在 `.cu` 里**根本不存在**（实际函数名是 `..._discrete_memory` 与 `..._unified_memory`）。由于声明从未被调用、也不与定义冲突，链接器不报错——头文件是过时的残留。教训：**头文件与实现脱节时编译器未必救你**，接口契约要靠人核对（本文件是单文件编译，`.h` 形同虚设）。

#### 4.4.4 代码实践

**实践目标**：把两个脚本的实验设计翻译成两张「控制变量表」，并从归档数据里读出交叉点。

**操作步骤**：

1. 对 test.sh 的任意 3 行，算出 (n, stride, 触碰元素, 有用字节, 离散搬运字节)，填成表；
2. 对 test2.sh 的任意 3 行做同样的事，指出这条设计里**什么被固定、什么被放大**；
3. 只看归档第一段（普通运行）的 28 组数字，标出统一时间首次低于离散时间的行号。

**需要观察的现象**：test.sh 里「离散搬运字节」一列全是 512MB；test2.sh 里「有用字节」一列全是 4KB。

**预期结果**：test.sh 固定足迹、扫描密度；test2.sh 固定有用工作量、放大足迹。归档第一段的交叉发生在 stride=4 那一行（[L11-12](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L11-L12) 的上一对 [L8-9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_cuda.output.carina.txt#L8-L9) 是统一最后一次落后）。全程无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么本基准的 stride 扫描要用 2 的幂逐档翻倍，而不是线性地 1、2、3、4……？

答案：翻倍让横轴覆盖 7 个数量级（1 到 1.34 亿）而只需 28 个数据点，且每档浪费倍数恰好翻番，曲线在对数坐标下近似等距、趋势一目了然。线性扫描要么点数爆炸、要么只覆盖低密度区间。

**练习 2**：你在自己的机器上测出的交叉 stride 是 32，比 Carina 的 4 大。给出至少两个可能的原因。

答案：（任答两个即可）① PCIe 带宽更宽（如 gen4/x16），全量 `cudaMemcpy` 更快，离散曲线整体下压，UM 需要更稀疏才能反超；② GPU 架构代际不同，新架构（Pascal 之后页迁移引擎、更大的迁移批、HMM 支持）改变 \( g \) 与缺页开销，使两条曲线斜率都变；③ 驱动/运行时版本影响迁移粒度（本例 32KB/批）；④ 计时口径与系统状态（是否 pageable、是否开着 profiler）直接影响统一路径的相对惩罚。

---

## 5. 综合实践

把整讲串起来做一次完整的密度扫描实验（即本讲规格里的实践任务）。

**任务**：固定 n=134217728，遍历 stride ∈ {1, 64, 1024, 65536, 33554432}，记录两种内存模式的时间，找出统一内存开始占优的 stride 区间，并用「实际触碰的字节数」解释这个 crossover。

**步骤**：

1. **编译**：`cd UniMem && make`（产物 `LowAccessDensityTest_cuda`，无需任何 UM 相关编译选项）。
2. **预计算账本**：先把 4.1.4 那张表（触碰元素、有用字节、密度、浪费倍数）算好，并写下两条曲线的预测形状。
3. **跑扫描**：依次执行
   ```bash
   ./LowAccessDensityTest_cuda 1 134217728
   ./LowAccessDensityTest_cuda 64 134217728
   ./LowAccessDensityTest_cuda 1024 134217728
   ./LowAccessDensityTest_cuda 65536 134217728
   ./LowAccessDensityTest_cuda 33554432 134217728
   ```
   每次运行输出两行：`(Discrete Memory)` 与 `(Unified Memory)` 各一行时间（本基准不打印 checksum，也不做校验，见 4.1.3）；开头的 `Usage:` 提示行是无条件打印的，忽略即可。
4. **补一轮 profiler 数据**：任选一个稀疏档位跑 `nvprof ./LowAccessDensityTest_cuda 65536 134217728`（新工具链用 `nsys profile` / `ncu`，参考 u1-l4），找到末尾的 `Unified Memory profiling result` 小节，记录迁移 Count / Avg Size / Total。
5. **成表并回答三个问题**：
   - 交叉点（统一首次反超离散）落在哪两个 stride 之间？对应密度是多少？
   - 用 4.3.3 的 64KB/元素规律估算该档 UM 每轮迁移量，与 profiler 的 Total÷10 对得上吗？
   - 你的交叉点比 Carina 的（stride 2→4 之间）早还是晚？用练习 2 的清单解释。

**Carina 参考答案**（归档普通运行值）：stride=1 时离散 121.54 vs 统一 172.70（统一落后）；stride=64 起 149.64 vs 96.46、1024 时 147.40 vs 90.82、65536 时 150.26 vs 42.12、33554432 时 148.78 vs 1.46（统一领先，且差距随密度下降单调拉大）；交叉点在 stride 2→4 之间，即密度降到约 25%–50% 时统一内存开始占优。解释链：离散时间 ∝ 全量 512MB（与 stride 无关的水平线）；统一时间 ∝ 触碰元素数 × 每元素迁移成本（约 64KB/元素 + 缺页调度），随 stride 稀疏化线性下降，加上迁移与计算的重叠，使交叉点比纯字节模型的预测（stride≈16384）更早出现。

若无 GPU：把上述步骤 1–5 全部换成对两份归档文件的读取与计算（普通运行段 + `nvprof` 段），并额外完成「profiler 把交叉点从 ~4 挪到 ~16」的解释，同样算完成实践。待本地验证（指在你自己机器上的具体数值）。

---

## 6. 本讲小结

- **统一内存的本质是把「何时搬、搬多少」从程序员手里交给页错误机制**：`cudaMallocManaged` 分配的缓冲区两侧同指针可用，数据按页、按批（本例实测约 32KB/批、约 64KB/触碰元素）在 kernel 运行中迁入显存。
- **离散路径的成本是全量拷贝的水平线**（本例 512MB、约 147ms，与 stride 无关）；**统一路径的成本随触碰元素数线性下降**（172.70ms → 1.08ms）。交叉点在 Carina 上位于 stride 2→4 之间（密度约 25%–50%），比纯字节模型预测的 stride≈16384 早得多——迁移与计算的重叠是关键加成。
- **密度高时统一内存是负优化**（stride=1 慢 42%）：逐批缺页搬完整个数组跑不过一次流水线化的大块 `cudaMemcpy`。UM 是「访问密度决定收益」的条件命题，不是性能开关。
- **迁移账本要看 profiler 的 UM 表**：次数、平均粒度、总量直接给出「只搬必要页」的真实粒度；profiler 本身会按迁移次数惩罚 UM、把交叉点后移（本例 4→16），报告里必须注明是否在 profiling 下测得。
- **读这个基准先读口径**：统一路径不计时 `cudaFree` 与 512MB 主机侧填充（`should not count time here`）、无独立预热、`check` 与串行参考均未被使用——结论方向可靠，数值不可直接外推。
- 顺带捡到三个工程习惯：头文件声明与实现脱节不必然报错（`LowAccessDensityTest.h:10-11`）；手写扫描表会有 `1048567` 这类笔误；同一归档文件里可能混着两个版本的二进制（输出格式不同即证据）。

## 7. 下一步学习建议

- **下一讲 u5-l5（MiniTransfer/SpMV）**同属第三类挑战，但把焦点从「搬运的时机与粒度」转到「数据布局本身决定搬运量」：dense、CSR、只传非零的 unified 三种布局对同一个 SpMV 的传输量对比，恰好与本讲的「按需拉页」形成互补——一个靠运行时机制省、一个靠数据结构省。建议先做完本讲综合实践再读。
- 若想深挖统一内存本身：阅读 `LowAccessDensityTest_cuda.cu` 后，可以尝试（在自己的分支里）给 `x2` 增加 `cudaMemPrefetchAsync` 与 `cudaMemAdvise(..., cudaMemAdviseSetReadMostly, ...)` 两个变体，复用本讲的扫描脚本验证 4.3.5 练习 3 的预测。
- 回顾性阅读：把 u2-l3（显存生命周期）、u5-l1（异步传输与 pinned 内存）与本讲并排，凑齐 CPU–GPU 数据搬运的三种武器——显式同步拷贝、显式异步拷贝、隐式按需迁移，并写出各自的适用条件。
