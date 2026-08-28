# CoMem（AXPY 篇）：全局内存合并访问

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释**合并访问（coalescing）**：warp 内 32 个线程在同一时刻访问连续地址时，硬件可以把 32 次访问合并成极少数内存事务。
2. 对任意一维访问模式 `下标 = f(线程编号)`，用「全局线程编号 × 步长」**在纸上推演**出它是否合并、会触碰多少个内存粒度单元。
3. 用 `nvprof` 观察不同访问模式下 kernel 时间的差异，理解「事务数被放大」如何转化为「耗时被放大」。
4. 意识到微基准实验中的**混杂变量**：仓库自带的 `block` 分布同时改变了访问模式与并行度两个因素，而你自己动手写的 stride kernel 才是干净的单变量实验。

本讲的载体是 `CoMem_AXPY` 目录。README 对 CoMem 基准的一句话定义是：反模式为「线程以跨步或随机方式访问数组（uncoalesced memory access，原文拼写如此）」，优化技术为「线程间连续内存访问」，见 [README.md:51-54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L51-L54)。

## 2. 前置知识

### 2.1 内存事务：显存传输的最小粒度

GPU 与显存（DRAM）之间的数据传输不是按「CPU 要几个字节就搬几个字节」进行的，而是有固定的粒度单位：

- **扇区（sector）**：32 字节，现代 NVIDIA GPU 全局内存访问的最小传输粒度。
- **缓存行（cache line）**：128 字节（即 4 个扇区），L2 缓存的行大小，也是早期架构计量事务的粒度。

即使一个线程只读 1 个 `double`（8 字节），硬件也要把它所在的整个 32 字节扇区搬进来。这就是**事务（transaction）**：一次实际发生的最小粒度传输。性能问题的关键不是「线程要多少字节」，而是「这些请求总共落在了多少个扇区上」。

### 2.2 warp：访存请求的发出单位

回顾 u3-l1：warp 是 32 个线程组成的调度与执行基本单位，SIMT 模型下同一 warp 的 32 个线程在**同一时刻执行同一条指令**。当这条指令是访存指令时，32 个线程各自算出一个地址、同时发出请求——硬件把这一批 32 个地址按扇区分组，落在同一个扇区里的请求被**合并（coalesce）**成一次事务。

> **一句话定义**：合并访问讨论的永远是「warp 在同一时刻的 32 个地址」，而不是「单个线程先后访问了什么」。这是本讲最重要、也最容易被误解的一点。

### 2.3 本项目的精度设定：double，8 字节

本基准 `REAL` 为 `double`（8 字节），在 [CoMem_AXPY/axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6) 与 [CoMem_AXPY/axpy_cuda.c:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21) 双处定义（u2-l4 讲过两处必须同步修改）。因此一个 warp 的 32 个请求，数据量共 \(32 \times 8\,\text{B} = 256\,\text{B}\)，即 8 个扇区。8 字节这个前提会贯穿本讲的所有计算。

### 2.4 回顾：这是访存受限的 kernel

u2-l1 分析过：AXPY 每元素只有 2 次浮点运算，却要搬运 24 字节（读 `x[i]`、读 `y[i]`、写 `y[i]`），是典型的**访存受限（memory-bound）** kernel。对它来说，内存事务的数量几乎直接决定耗时——这正是用它来演示合并访问的原因：访问模式的影响不会被计算掩盖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | 4 个 kernel（warmingup / 1perThread / block / cyclic）与主机包装函数 `axpy_cuda`，是本讲的精读对象 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 侧主程序：初始化、串行基线、10 轮平均计时、`check` 校验 |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口契约：`REAL` 定义与 `axpy_cuda` 的 `extern "C"` 声明 |
| [CoMem_AXPY/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt) | 仓库自带的 Carina 集群 nvprof 终端转录，4 个数据规模的完整测量，是本讲的「云实验数据」 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 实验设计：在 4 个 n 值下分别用 nvprof 运行 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 单行式编译：`nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu` |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 性能模式总表中 CoMem 的条目（L51-L54） |

## 4. 核心概念与源码讲解

### 4.1 合并访问的原理：warp 集体地址与事务数

#### 4.1.1 概念说明

设想 warp 内 32 个线程（lane 0~31）同时执行一条读指令。设 lane \(k\) 访问的字节地址为：

\[
\text{addr}(k) = \text{base} + k \times s \times 8\,\text{B}
\]

其中 \(s\) 是**元素步长**（相邻 lane 访问的下标差）。硬件要做的分组是：把这 32 个地址映射到扇区编号 \(\lfloor \text{addr}/32 \rfloor\)，相同扇区的请求合并为一次事务。于是：

- \(s=1\)：32 个地址连续覆盖 256B，恰好 8 个扇区，**每个字节的传输都是有用数据**。
- \(s=2\)：相邻地址差 16B，一个 32B 扇区里装得下 2 个请求，共 16 个扇区。
- \(s \ge 4\)：相邻地址差 \(\ge 32\)B，**每个请求独占一个扇区**，无论 \(s\) 是 4 还是 32，都要 32 个扇区。

我们用**带宽利用率**量化访问模式的优劣：

\[
\text{利用率} = \frac{\text{线程真正需要的字节数}}{\text{硬件实际传输的字节数}} = \frac{256}{\text{触碰扇区数} \times 32}
\]

利用率越低，意味着同样的有效数据要占用越多的显存带宽和缓存空间——事务被放大了。

#### 4.1.2 核心流程

一次 warp 全局读请求的处理流程（简化模型）：

```text
32 个线程各自算出地址
        │
        ▼
按 32B 扇区分组（同一扇区的请求合并）
        │
        ▼
L1 命中检查 ──命中──► 直接返回数据
        │未命中
        ▼
向 L2 / DRAM 发出 N 个扇区读事务（N = 触碰的扇区数）
        │
        ▼
数据沿原路返回，线程继续
```

对这个 AXPY 场景（double，8B），不同步长下的理论值：

| 步长 s | 相邻地址间隔 | 触碰扇区数 | 传输字节 | 利用率 |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 8B | 8 | 256B | 100% |
| 2 | 16B | 16 | 512B | 50% |
| 4 | 32B | 32 | 1024B | 25% |
| 8 | 64B | 32 | 1024B | 25% |
| 32 | 256B | 32 | 1024B | 25% |

注意 4、8、32 三行的扇区数与利用率相同——但这**不等于**性能相同：步长越大，地址跨度越大（\(s=32\) 时一个 warp 的请求横跨约 8KB），DRAM 页命中、L2 分布、TLB 局部性都会更差。理论表给出下界，实测往往随跨度继续恶化，这正是第 5 节综合实践要验证的。

#### 4.1.3 源码精读：先看现象

先不写代码，直接看仓库自带的 Carina 集群实测（n=4096000 那一轮），四个 kernel 的 GPU 时间在这里：[CoMem_AXPY/axpy_cuda.output.carina.txt:37-42](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L37-L42)

```text
GPU activities:  67.36%  167.92ms  20  8.3958ms  ...  [CUDA memcpy HtoD]
                 26.83%   66.870ms  10  6.6870ms  ...  [CUDA memcpy DtoH]
                  4.30%   10.716ms  10  1.0716ms  ...  axpy_cudakernel_block
                  0.52%    1.3001ms  10  130.01us  ...  axpy_cudakernel_cyclic
                  0.50%    1.2539ms  10  125.39us  ...  axpy_cudakernel_warmingup
                  0.49%    1.2162ms  10  121.62us  ...  axpy_cudakernel_1perThread
```

`Calls` 列都是 10（对应 host 侧 10 轮平均，见 [CoMem_AXPY/axpy_cuda.c:84-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L89)）。换算成每轮平均耗时：

| kernel | 每轮平均耗时 | 相对 1perThread |
| --- | ---: | ---: |
| axpy_cudakernel_1perThread | 121.6 µs | 1.00× |
| axpy_cudakernel_warmingup | 125.4 µs | 1.03× |
| axpy_cudakernel_cyclic | 130.0 µs | 1.07× |
| axpy_cudakernel_block | 1071.6 µs | **8.8×** |

三个观察，构成本讲的主线：

1. `warmingup` 与 `1perThread` 函数体完全相同，时间也几乎相同（差 3%，正常波动）；
2. `cyclic` 每个线程跨大步长跳跃访问，时间却只差 7%——它其实是合并的（4.3 节解释）；
3. `block` 每个线程访问**连续**的一段元素，反而是最慢的，慢近 9 倍——warp 集体地址发散了（4.4 节解释）。

#### 4.1.4 代码实践：纸面推演事务数

1. **实践目标**：不依赖任何机器，用 4.1.1 的公式手工推演事务数，建立「看代码就能预判合并性」的能力。
2. **操作步骤**：
   - 对 `REAL=float`（4 字节）重算 4.1.2 的表格（此时一个 warp 请求总量为 \(32 \times 4\,\text{B} = 128\,\text{B}\)）。
   - 再补算 \(s=16\)、\(s=31\) 两行。
3. **需要观察的现象**：float 情况下，步长为多少时相邻请求开始「独占扇区」？
4. **预期结果**：float 时相邻地址差为 \(4s\) 字节，\(s \ge 8\)（差 \(\ge 32\)B）起每个请求独占一个扇区，触碰扇区数封顶在 32；\(s \le 7\) 时利用率高于 25%。待本地验证（可与第 5 节实测互相印证）。

#### 4.1.5 小练习与答案

**练习 1**：`REAL=float`、`s=1` 时，一个 warp 的读请求触碰几个 32B 扇区？利用率是多少？

答案：\(32 \times 4\,\text{B} = 128\,\text{B}\)，连续分布，触碰 \(128/32 = 4\) 个扇区，传输 128B 全部有用，利用率 100%。

**练习 2**：`REAL=double`、`s=2` 时利用率是 50%。既然利用率减半，耗时一定翻倍吗？

答案：不一定。事务数翻倍意味着**带宽需求**翻倍；只有当 kernel 已经跑在带宽饱和点上时，耗时才近似翻倍。若 kernel 时间被延迟或占用率主导（数据集小、缓存命中高），差异会被稀释。Carina 数据里 `block` 在 n=1024000 时只慢 12% 而不是理论上的 3 倍，就是这个原因（见 4.4.3）。

**练习 3**：为什么合并的单位是 warp 而不是 block？

答案：因为 SIMT 执行模型中，同一 warp 的 32 个线程在同一时刻执行同一条访存指令，硬件才有机会把这一批地址一次性分组合并。不同 warp 的访存指令发生在不同时刻，彼此之间没有合并关系（但可能在缓存层面互相受益）。

### 4.2 模块一：`axpy_cudakernel_1perThread`——每线程一个元素的天然合并

#### 4.2.1 概念说明

「每线程一个元素」（1perThread）是最朴素的并行化：全局线程编号 \(i\) 与数组下标 \(i\) 一一对应。它同时天然满足合并访问：相邻线程访问相邻元素，lane \(k\) 的地址为 \(\text{base} + k \times 8\)B——正是 4.1.1 中 \(s=1\) 的理想情形。理解它为什么快，就建立了评判其它访问模式的参照系。

#### 4.2.2 核心流程

```text
线程 i = blockDim.x * blockIdx.x + threadIdx.x
        │
        ▼
if (i < n):  读 x[i]（8B）、读 y[i]（8B）、写 y[i]（8B）
        │
        ▼
同一 warp 内：lane 0..31 的 x 地址连续覆盖 256B → 8 个扇区
             y 的读、写同理 → 每条访存指令都是 100% 利用率
```

warp 内编号分段连续、无缝无重叠（u2-l1 详细推导过），所以**每一个** warp 的每一次访问都是合并的，没有任何例外块。

#### 4.2.3 源码精读

kernel 本体在 [CoMem_AXPY/axpy_cudakernel.cu:16-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22)：

```cuda
__global__ 
void
axpy_cudakernel_1perThread(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a*x[i];
}
```

两个关键点：

- 第 20 行把线程层级坐标折叠成一维下标，`i` 同时就是数组下标——访问模式由这一行唯一决定；
- 第 21 行的 `if (i < n)` 是边界保护（u2-l1 讲过 `(n+255)/256` 向上取整保证线程数不少于 n）。

它的启动配置在包装函数里：[CoMem_AXPY/axpy_cudakernel.cu:63-64](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L63-L64)

```cuda
axpy_cudakernel_1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
cudaDeviceSynchronize();
```

线程总数恰好覆盖整个数组（n=1024000 时为 4000 个块、1024000 个线程，n 是 256 的整数倍，`if (i<n)` 实际不裁剪任何线程）。

另外注意 [CoMem_AXPY/axpy_cudakernel.cu:8-14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L8-L14) 的 `axpy_cudakernel_warmingup` 与 1perThread **逐字相同**——它是预热 kernel（u2-l1、u2-l4 讲过其作用），也为 4.1.3 的对照提供了一个「同代码同耗时」的基准点。

#### 4.2.4 代码实践：估算有效带宽

1. **实践目标**：用 Carina 实测数据反推 1perThread 的有效带宽，验证它确实接近带宽受限。
2. **操作步骤**：
   - 从 [CoMem_AXPY/axpy_cuda.output.carina.txt:66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L66) 取 n=10240000 时 1perThread 的总时间 3.0278ms（10 轮），每轮 302.8µs；
   - 按「每元素 3 次访问（读 x、读 y、写 y）」计算搬运量：\(3 \times n \times 8\,\text{B} = 245.76\,\text{MB}\)；
   - 计算有效带宽并与其余三个 n 值（[L15-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L15-L18)、[L42](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L42)、[L90](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L90)）重复。
3. **需要观察的现象**：四个规模算出的有效带宽是否大体恒定？
4. **预期结果**：n=10240000 时有效带宽 \(\approx 245.76\,\text{MB} / 302.8\,\mu s \approx 811\,\text{GB/s}\)。若各规模算出的值接近常数，说明 kernel 已跑在稳定的带宽工作点上，访问模式的任何恶化都会直接反映为耗时上升。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：n=4096000 时 1perThread 启动多少个块、多少个线程？

答案：\((4096000+255)/256 = 16000\) 个块（向上取整后仍为精确值，因为 4096000 是 256 的倍数），共 \(16000 \times 256 = 4096000\) 个线程，恰好一线程一元素。

**练习 2**：把 block size 从 256 改成 128（启动改为 `<<<(n+127)/128, 128>>>`），合并性会被破坏吗？

答案：不会。合并只取决于「同一 warp 内 32 个线程的地址是否连续」，而 warp 内线程编号依然是连续的 `blockDim.x*blockIdx.x + threadIdx.x`。block size 只改变 warp 的分组边界，每个 warp 覆盖的仍是 32 个连续元素。u2-l1 的实践（改 block size 影响很小）已经从实验侧面印证了这一点。

**练习 3**：如果线程编号计算写成 `i = threadIdx.x * gridDim.x + blockIdx.x`（把块号加在个位），访问还合并吗？

答案：不合并。这样相邻线程（threadIdx.x 差 1）的地址差为 `gridDim.x` 个元素（间隔数千字节），每个请求独占扇区，一个 warp 触碰约 32 个扇区、利用率跌到 25%。编号公式的「写法」直接决定访问模式。

### 4.3 模块二：`axpy_cudakernel_cyclic`——跨步循环为何依然合并

#### 4.3.1 概念说明

cyclic 分布（即 grid-stride loop，u2-l2 讲过它的任务划分性质）表面上很「不连续」：每个线程在循环里以总线程数为步长跳跃。初学者常据此以为它访问模式差。事实恰恰相反——**合并的单位是 warp 在同一迭代内的集体地址**：第 \(j\) 次迭代时，warp 内 lane \(k\) 访问下标 \(j \cdot T + w + k\)（\(T\) 为总线程数，\(w\) 为该 warp 的起始线程编号），32 个下标**连续**。单个线程的轨迹跨大步，但每次集体访问都落在连续的 256B 里。「形散而神不散」。

#### 4.3.2 核心流程

```text
total_threads T = gridDim.x * blockDim.x   （本基准固定 1024×256 = 262144）
线程编号 t = threadIdx.x + blockIdx.x * blockDim.x

for (i = t; i < n; i += T):
        warp w 的 32 个线程在同一迭代访问 [j*T + w, j*T + w + 32)
        → 连续 32 个 double = 256B = 8 个扇区 → 完全合并
```

相邻迭代之间，同一个 warp 的窗口整体向前跳 \(T\) 个元素（2MB），但这无所谓——事务只看单次访存指令的地址分组。

#### 4.3.3 源码精读

kernel 在 [CoMem_AXPY/axpy_cudakernel.cu:41-50](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L41-L50)：

```cuda
/* cyclic distribution of loop distribution */
__global__
void axpy_cudakernel_cyclic(REAL* x, REAL* y, int n, REAL a) {
	int thread_num = threadIdx.x + blockIdx.x * blockDim.x;
	int total_threads = gridDim.x * blockDim.x;
	
	int i;
	for (i=thread_num; i<n; i+=total_threads) { 
		if (i < n) y[i] += a*x[i];
	}
}
```

- 第 43-44 行算出线程编号与总线程数 \(T\)；
- 第 47 行的循环步长是 \(T\)：**同一条访存指令执行时**，warp 内 32 个线程的 `i` 值依次相差 1，这就是合并的全部条件。

启动配置在 [CoMem_AXPY/axpy_cudakernel.cu:67-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L67-L68)，固定 `<<<1024, 256>>>`（即 262144 个线程，输出文件 [第 3 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L3) 也专门记录了这个配置）。对 n=4096000，每个线程循环约 15~16 次，每次迭代 warp 集体访问一个连续 256B 窗口。

Carina 实测印证了分析：n=4096000 时 cyclic 每轮 130.0µs，仅比 1perThread 的 121.6µs 慢 7%（[output.carina.txt:40 与 L42](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L40)），这 7% 主要来自循环控制开销与固定线程数带来的占用差异，而不是事务数。

#### 4.3.4 代码实践：无 GPU 的云验证 + 有 GPU 的实测

1. **实践目标**：验证「cyclic 跨步但合并」的论断在各个数据规模下都成立。
2. **操作步骤**：
   - 无 GPU 环境：从 [CoMem_AXPY/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt) 的四段输出（L15-L18、L39-L42、L63-L66、L87-L90）提取 cyclic 与 1perThread 的 10 轮总时间，除以 10 得每轮平均；
   - 有 GPU 环境：进入 `CoMem_AXPY` 目录 `make` 编译，按 [CoMem_AXPY/test.sh:1-4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1-L4) 的方式依次 `nvprof ./axpy_cuda 1024000`、`4096000`、`10240000`、`20480000`。
3. **需要观察的现象**：cyclic 与 1perThread 的每轮耗时比值。
4. **预期结果**：比值应在 1.0~1.1 之间（Carina 上四个规模分别为 0.98、1.07、1.11、1.13）。若你测出 2 倍以上的差距，优先怀疑环境差异（占用率、驱动）而非访问模式。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`<<<1024, 256>>>` 配置下，线程 1280~1311（某 warp）第一次迭代访问哪些下标？

答案：下标 1280~1311，共 32 个连续元素。该 warp 的起始编号 \(w=1280\)，第一次迭代 \(j=0\)，访问 \([j \cdot 262144 + 1280,\ j \cdot 262144 + 1312)\)。

**练习 2**：cyclic 的线程数（262144）远少于 1perThread（等于 n），为什么对 n=20480000 仍能保持接近的速度？

答案：一是每次迭代都完全合并，带宽利用率不打折；二是 262144 个线程已足以填满 GPU 的并发执行资源，且每个线程的多轮迭代还提供了指令级并行；两者相加，使它落在与 1perThread 相同的带宽工作点上。

**练习 3**：既然两者速度接近，grid-stride loop 相对 1perThread 有什么工程优势？

答案：线程数与数据规模 n 解耦——n 很大或编译期未知时不必启动超大规模 grid；便于多 kernel 共享 GPU 或控制占用率。CUDA 官方范式正是推荐这种写法。

### 4.4 对照与陷阱：`axpy_cudakernel_block` 的非合并访问与混杂变量

#### 4.4.1 概念说明

block 分布是 cyclic 的镜像：每个线程承包一段**连续**区间，线程编号 \(t\) 处理 \([t \cdot B,\ (t+1) \cdot B)\)，\(B = n / T\)。单线程轨迹完全连续，看起来「缓存友好」；但**同一迭代内**，warp 内相邻线程的地址相差 \(B\) 个元素——集体地址发散，正是 4.1.1 中步长 \(s = B\) 的情形。它是本仓库「跨线程非连续访问」反模式的主要演示载体。同时（重要！）它还悄悄改变了第二个变量：线程数固定 262144，随 n 增长每线程串行工作量剧增。**这个 kernel 的慢是两个因素的叠加**，读实验数据时必须保持警惕。

#### 4.4.2 核心流程

```text
B = n / 262144（整除，向下取整——u2-l2 已指出尾部元素被漏算）
线程 t 的循环：for (i = t*B; i < (t+1)*B; i++)
        │
        ▼
第 j 次迭代：warp 内 lane k 访问 t_start*B + j + k*B
        → 相邻 lane 地址差 = B × 8 字节
        B=3   → 差 24B，warp 跨 768B，触碰约 24 个扇区，利用率 ≈ 33%
        B=15  → 差 120B ≥ 32B，每请求独占扇区 → 32 个扇区，利用率 25%
        B=78  → 差 624B，仍 32 个扇区，但 warp 单次迭代地址跨度 ≈ 19.3KB
```

理论上 B≥4（间隔 ≥32B）后触碰扇区数封顶 32，但跨度随 B 线性增大：一个 warp 的完整工作集达 \(32 \times B \times 8\)B（B=78 时约 19.3KB），远超迭代间 L1 能稳定驻留的量，加上并行度低导致延迟难以隐藏，实测劣化远超「利用率 25%」的字面值。

#### 4.4.3 源码精读

kernel 在 [CoMem_AXPY/axpy_cudakernel.cu:24-38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L24-L38)：

```cuda
/* block distribution of loop iteration */
__global__ 
void axpy_cudakernel_block(REAL* x, REAL* y, int n, REAL a) {
	int thread_num = threadIdx.x + blockIdx.x * blockDim.x;
	int total_threads = gridDim.x * blockDim.x;

	int block_size = n / total_threads; //dividable, TODO handle non-dividiable later
	
	int start_index = thread_num * block_size;
	int stop_index = start_index + block_size;
	int i;
        for (i=start_index; i<stop_index; i++) {
		if (i < n) y[i] += a*x[i];
	}
}
```

- 第 30 行 `block_size = n / total_threads` 向下取整，注释里的 TODO 承认了整除假设（u2-l2 已分析过漏算尾部的缺陷，本讲关注性能而非正确性）；
- 第 35 行循环体内，同一时刻 warp 的 32 个 `i` 相差 `block_size`——非合并访问就写在这两行的组合里。

启动配置固定为 `<<<1024, 256>>>`：[CoMem_AXPY/axpy_cudakernel.cu:65-66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L65-L66)。

对照 Carina 四个规模的完整数据（各文件段落：[L15-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L15-L18)、[L37-L42](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L37-L42)、[L61-L66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L61-L66)、[L85-L90](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L85-L90)），换算为每轮平均耗时：

| n | B = n/262144 | 1perThread | cyclic | block | block / 1perThread |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,024,000 | 3 | 32.1 µs | 31.6 µs | 36.1 µs | 1.12× |
| 4,096,000 | 15 | 121.6 µs | 130.0 µs | 1071.6 µs | 8.8× |
| 10,240,000 | 39 | 302.8 µs | 336.4 µs | 5050.7 µs | 16.7× |
| 20,480,000 | 78 | 604.2 µs | 684.1 µs | 11927 µs | 19.7× |

三个层次的解读：

1. **n=1024000 时只慢 12%**：B=3 间隔 24B，理论利用率约 33%（应慢约 3 倍），但数据集小（约 16MB）缓存命中高、kernel 又短，非合并代价被大幅稀释——理论下界不保证兑现；
2. **n 增大后劣化远超利用率模型的预测**（B≥15 后利用率恒为 25%，劣化却从 8.8× 涨到 19.7×）：说明**并行度**（固定 262144 线程 vs 1perThread 的 n 线程）与每线程串行迭代次数在同步恶化，warp 地址跨度增大也在伤害 L2/DRAM 局部性；
3. **方法论教训**：block kernel 不是干净的单变量实验。要单独量化「访问模式」的影响，需要保持线程数与每线程工作量不变、只改地址分布——这正是下一节综合实践要你亲手做的 stride kernel。

#### 4.4.4 代码实践：计算各规模下的 warp 地址跨度

1. **实践目标**：把 4.4.3 的表格与理论模型逐行对应起来，理解「利用率封顶、劣化不封顶」。
2. **操作步骤**：
   - 对每个 n 计算 \(B\)、相邻 lane 字节间隔（\(B \times 8\)）、warp 单次迭代的地址跨度（\(\approx 31 \times B \times 8 + 8\) 字节）、warp 完整工作集（\(32 \times B \times 8\) 字节）；
   - 将跨度与本机 GPU 的 L1 容量（每 SM 通常几十 KB，可用 `cudaGetDeviceProperties` 的 `l2CacheSize`、`sharedMemPerMultiprocessor` 等字段佐证）比较。
3. **需要观察的现象**：从哪个 n 开始，单个 warp 的工作集超过典型 L1 容量？
4. **预期结果**：B=15 时工作集约 3.75KB、B=78 时约 19.3KB；一个 SM 同时驻留多个 warp 时总需求成倍放大，n=4096000 起缓存已无法兜住，劣化开始远超利用率模型的预测。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：n=20480000 时 block kernel 的相邻 lane 字节间隔是多少？

答案：\(B = 20480000 / 262144 = 78\)（向下取整），间隔 \(78 \times 8 = 624\) 字节；warp 单次迭代地址跨度约 \(31 \times 624 + 8 \approx 19.3\)KB。

**练习 2**：如果把 block kernel 的启动配置从 `<<<1024, 256>>>` 改成 `<<<(n*B+...)>>>` 让线程数等于 n/B 且 B=1，它的行为会退化成哪一种？

答案：B=1 时每个线程只处理一个元素、相邻线程访问相邻元素——退化成 1perThread，完全合并。这说明 block 分布的非合并性由「每线程承包连续区间」与「相邻线程区间相邻」共同造成，区间长度 B 是放大器。

**练习 3**：为什么说「block kernel 慢 19.7 倍」不能直接当作「非合并访问慢 19.7 倍」的结论引用？

答案：因为该实验同时改变了访问模式与并行度两个变量（262144 线程固定不变，而对照组 1perThread 的线程数随 n 增长）。诚实引用应表述为「在固定 262144 线程、每线程承包连续区间的设定下，非合并访问叠加并行度不足造成 19.7 倍劣化」；隔离出纯访问模式的影响需要 stride kernel 实验。

## 5. 综合实践：写一个带 stride 参数的 AXPY kernel

这是本讲的核心动手任务（对应大纲中本讲的 practice_task）：**新写一个 kernel，thread i 访问 `x[i*stride]`，在 stride=1、2、4、32 下运行并用 nvprof 观察耗时变化，解释为什么 stride=1 时 warp 的 32 次访问可以被合并成最少的事务。**

它同时是 4.4 提出的「干净单变量实验」：每个版本都做相同次数的乘加、每线程恰好处理一个元素、线程组织完全相同，唯一变量是相邻线程访问下标的间隔。

**第 0 步：在副本中工作（不修改仓库源码）。**

```bash
cp -r CoMem_AXPY MyStrideLab
cd MyStrideLab
```

**第 1 步：新增 strided kernel（示例代码，非仓库原有）。**

在 `axpy_cudakernel.cu` 末尾的 `axpy_cuda` 函数体之前加入：

```cuda
/* 示例代码：综合实践新增，非仓库原有。
   m 为本 kernel 要处理的元素个数，实际下标 = i * stride */
__global__
void axpy_cudakernel_strided(REAL* x, REAL* y, int m, REAL a, int stride) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;   /* 第 i 个被处理的元素 */
    if (i < m) {
        int j = i * stride;                            /* 实际访问的下标 */
        y[j] += a * x[j];
    }
}
```

设计要点：`stride=1` 时它与 `axpy_cudakernel_1perThread` 严格等价（\(j = i\)），这是验证实现正确性的锚点——它的耗时应当与 nvprof 里 1perThread 那一行几乎相同。

**第 2 步：接线到 host 侧（示例代码）。**

- 把 `m = n / stride`（向下取整，保证 \(m \times stride \le n\) 不越界）作为元素个数，启动 `axpy_cudakernel_strided<<<(m+255)/256, 256>>>(d_x, d_y, m, a, stride)`，加在 `axpy_cuda` 的 cyclic 之后（对照 [axpy_cudakernel.cu:52-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73) 的包装骨架）；
- 让 `stride` 从命令行进入：在 [axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) 的 `main` 里读 `argv[2]`（`if (argc >= 3) stride = atoi(argv[2]);`），并把 `stride` 一路传入 `axpy_cuda`（同步修改 [axpy.h:11](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L11) 的函数原型）。

提示：四个原有 kernel 仍在共享 `d_y` 叠加结果（u2-l2 讲过），所以整体 `checksum` 在本实践中没有参照意义；我们只看 nvprof 表中 strided 那一行的 kernel 时间。想更干净也可以在副本里注释掉其它四个启动。

**第 3 步：编译并运行四个步长。**

```bash
make                                        # 或 nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu
nvprof ./axpy_cuda 4096000 1
nvprof ./axpy_cuda 4096000 2
nvprof ./axpy_cuda 4096000 4
nvprof ./axpy_cuda 4096000 32
```

进阶：可加采内存指标帮助量化事务数，如 `nvprof --metrics achieved_occupancy,dram_read_throughput ./axpy_cuda 4096000 32`；不同工具链版本可用指标名不同（Nsight Compute 对应的是 `l1tex__average_t_sectors_per_request_pipe_lsu_mem_global_op_ld.ratio`，即每请求平均扇区数），具体可用 `nvprof --query-metrics | grep -i sector` 查询，待本地验证。

**第 4 步：记录并对照理论。**

| stride | m = n/stride | 触碰扇区数（理论） | 利用率（理论） | strided kernel 每轮耗时（你的测量） |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 4096000 | 8 | 100% | 待填（应与 1perThread 接近） |
| 2 | 2048000 | 16 | 50% | 待填 |
| 4 | 1024000 | 32 | 25% | 待填 |
| 32 | 128000 | 32 | 25% | 待填 |

**第 5 步：写出你的解释。**

回答实践任务的核心问题——为什么 stride=1 时 warp 的 32 次访问能合并成最少的事务：32 个 double 请求恰好连续覆盖 256B，硬件一次只需搬 8 个 32B 扇区，搬来的每个字节都是线程要用的；而 stride≥4 时每个请求各占一个扇区，同样 256B 的有效数据要搬 32 个扇区（1024B），再大的 stride 还会拉大地址跨度、恶化 DRAM/L2 局部性。

**预期结果**：耗时随 stride 单调不降；stride=1 与 nvprof 中 1perThread 行基本一致；stride=4 与 32 尽管理论利用率同为 25%，后者通常更慢（跨度 8KB vs 1KB）。若你的机器上差异不明显，检查 n 是否太小（建议 ≥4096000）以及是否误用了 `-G` 编译。待本地验证。

## 6. 本讲小结

- 合并访问的判断单位是 **warp 在同一时刻发出的 32 个地址**，不是单个线程的访问轨迹；DRAM 按 32B 扇区（L2 行 128B）为粒度传输。
- `1perThread`（下标 = 全局线程编号）天然满足步长 1，每条访存指令 8 个扇区、利用率 100%，是所有一维访问模式的参照系。
- `cyclic` / grid-stride loop 虽然单线程轨迹跨大步，但**同一迭代内 warp 集体地址连续**，同样完全合并——Carina 实测与 1perThread 仅差百分之几。
- `block` 分布（每线程承包连续区间）恰好相反：单线程连续、warp 集体发散；且它同时固定了线程数，实验混杂了并行度因素，大 n 下近 20 倍的劣化是两个变量叠加的结果。
- 理论「利用率」给出劣化下界但不封顶实测：小数据集上缓存会稀释非合并代价（n=1024000 时 block 只慢 12%），大数据集上跨度与并行度会把代价放大到利用率模型之外。
- 干净的单变量实验要自己造：stride kernel 固定线程组织与每线程工作量，只改地址间隔——这正是微基准设计的方法论精髓。

## 7. 下一步学习建议

1. **u4-l3（CoMem_SpMM）**：把本讲的「看下标算步长」方法应用到稀疏矩阵乘（CSR/CSC 格式）上——真实负载里访问模式由数据结构决定，合并性不再由你直接控制，判断难度陡增。
2. **u4-l4（MemAlign）**：本讲假设了起始地址对齐；MemAlign 基准专门演示仅偏移 1 个元素时跨缓存行/扇区边界造成的额外事务，是合并访问的「对齐补篇」。
3. **u4-l5（BankRedux）**：同样的「分散访问被粒度单元放大」思想在共享内存中的版本——32 个 bank 与 bank 冲突。
4. 动手方向：把第 5 节的 stride 实验扩展成 2 的幂次扫描（1、2、4、…、128），画出「耗时–步长」曲线，找出你这块 GPU 上劣化开始饱和的步长，并与 4.1.2 的理论表对照。
