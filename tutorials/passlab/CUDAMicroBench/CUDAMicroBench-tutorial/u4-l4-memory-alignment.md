# MemAlign：内存访问对齐对性能的影响

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出**地址对齐（alignment）**的准确定义：一个地址是 32 字节扇区（或 128 字节缓存行）的整数倍，才算对齐；对齐与否讨论的仍然是 u4-l2 强调的「warp 在同一时刻发出的 32 个地址」这个整体。
2. 解释 `MemAlign` 基准的核心实验设计：两个 kernel **只差一个元素的下标偏移**——对齐版 `i = blockDim.x*blockIdx.x + threadIdx.x`，非对齐版 `i = … + 1`——却让每个 warp 的访存窗口整体错开 8 字节，从而跨出额外的缓存行与扇区。
3. 复现并量化「1 个 `double` 偏移」的代价：理论上每个 warp 请求触碰的扇区数从 8 变 9（多 12.5%），而仓库自带的 Carina 实测显示 kernel 只慢约 3%——你要能解释这 12.5% 与 3% 之间的差距去哪了。
4. 区分源码中的 **warmup kernel** 与**被测 kernel**：谁参与对比、谁只负责预热、它们的守卫条件为什么各不相同。

本讲的载体是 `MemAlign` 目录。README 对它的定义是：反模式为「分配的内存起始地址未对齐（Mallocation has unaliged adress at the begninning，原文拼写如此）」，优化技术为「使用对齐的分配（Use aligned malloc）」，见 [README.md:56-59](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L56-L59)。我们会看到：代码并没有真的去分配一个「不对齐的指针」，而是用一个下标偏移制造出**完全相同的硬件效果**——这是本讲最值得学的一处实验设计。

## 2. 前置知识

### 2.1 什么是对齐

一个字节地址 \(p\) 相对于粒度 \(g\)（32B 或 128B）**对齐**，当且仅当：

\[
p \bmod g = 0
\]

「对齐」永远是相对于某个粒度单位说的。GPU 全局内存世界里有两个粒度（u4-l2 已介绍，这里直接复用）：

- **扇区（sector）**：32 字节，现代 NVIDIA GPU 全局内存的最小传输粒度；
- **缓存行（cache line）**：128 字节 = 4 个扇区，L2 的行大小。

注意两个容易混淆的说法：

- 「`double` 自然对齐」指地址是 8 的倍数——这**不够**，8B 对齐的地址完全可能落在扇区中间（例如 32B 扇区内的偏移 8、16、24 处）。
- `cudaMalloc` 返回的设备指针由 CUDA 保证**至少 256 字节对齐**。所以本基准里数组起点 `x[0]`、`y[0]` 的地址一定是对齐的——错位只能来自**下标偏移**，这正是源码的做法。

### 2.2 与合并访问的关系：u4-l2 的延续

u4-l2 讲的是**合并（coalescing）**：warp 内 32 个线程访问**连续**地址时，硬件能把 32 个请求合并成 8 个扇区事务（`double`、8B 场景）。本讲的两个 kernel **都是**合并访问——每个 warp 的 32 个地址都连续。那还差在哪？

差在**这一串连续地址的起点**。合并回答「32 个地址是否连续」，对齐回答「这串连续地址是否从粒度边界开始」。u4-l2 的 stride 实验改变的是**步长**（地址间隔），本讲固定步长为 1、只平移**起点**——这是比 stride 更精细的一档控制变量。

### 2.3 本基准的精度与规模设定

`MemAlign` 的 `REAL` 是 `double`（8 字节），在 [MemAlign/axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy.h#L6) 与 [MemAlign/axpy_cuda.c:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L21) 双处定义（u2-l4 讲过两处必须同步改）。默认规模 `VEC_LEN = 1024000` 在 [MemAlign/axpy_cuda.c:22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L22)，也可用命令行参数覆盖。8B 这个前提贯穿本讲所有计算：32 个 `double` = 256B = 8 个扇区 = 2 个缓存行。

### 2.4 与 CoMem_AXPY 的同构关系

u2-l4 已经用 `MemAlign` 与 `CoMem_AXPY` 这对目录演示过「骨架同构、差异集中于被测变量」的微基准方法论：两者共享同一个 AXPY 三件套骨架。本讲聚焦它们唯一的实质差异——kernel 里那个 `+1`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu) | 本讲精读对象：3 个 kernel（`1perThread` / `1perThread_misaligned` / `1perThread_warmup`）与主机包装函数 `axpy_cuda` |
| [MemAlign/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c) | host 主程序：`REAL`/`VEC_LEN` 定义、串行基线（注意它从 `i=1` 起循环）、`check`、10 轮平均计时 |
| [MemAlign/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy.h) | 接口契约：`REAL` 与 `axpy_cuda` 的 `extern "C"` 声明 |
| [MemAlign/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt) | 仓库自带的 Carina 集群 nvprof 转录，开头一行 `Offset = 1` 说明当时测的就是这个偏移，含 6 个规模的完整数据，是本讲的「云实验数据」 |
| [MemAlign/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/test.sh) | 实验设计：6 个 `n` 值下分别用 nvprof 运行 |
| [MemAlign/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile) | 调试式单行编译：`nvcc -g -G -arch=sm_30 …` |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 性能模式总表中 MemAlign 的条目（L56-L59） |

## 4. 核心概念与源码讲解

### 4.1 对齐参照系：`axpy_cudakernel_1perThread`

#### 4.1.1 概念说明

任何性能实验都需要一个「正常情况」作参照。本基准的参照系就是 u2-l1 逐行讲过的**每线程一个元素**kernel：线程 `t` 处理元素 `t`。它在这个语境下的关键性质是：

> warp `w` 的 32 个线程访问的元素下标是 \(32w, 32w+1, \dots, 32w+31\)，对应字节区间 \([256w,\; 256w+256)\)。

由于 `cudaMalloc` 保证基址至少 256B 对齐，这个区间**恰好从缓存行边界开始、又恰好在缓存行边界结束**：正好铺满 2 个 128B 缓存行、8 个 32B 扇区，没有一个字节被「顺带搬进来」。这就是对齐的参照态：warp 请求触碰的粒度单元数达到理论最小值。

#### 4.1.2 核心流程

一次对齐版 warp 读请求在地址空间的落点：

```text
字节地址：  0        32        64        96       128       160       192       224       256
            │ sector0 │ sector1 │ sector2 │ sector3 │ sector4 │ sector5 │ sector6 │ sector7 │
warp 0 读   ├────────────────── x[0..15] ───────────────┤├────────────── x[16..31] ──────────┤
（256B）    ├── cache line 0（128B）─────────────────────┤├── cache line 1（128B）────────────┤
            ↑ 起点在边界上：8 扇区 / 2 行，不多不少
```

kernel 侧的执行流程（与 u2-l1 相同，这里只列本讲用到的部分）：

1. 启动配置 `<<<（n+255)/256, 256>>>` 产生 \(T = 256 \times \lceil n/256 \rceil\) 个线程；
2. 每个线程计算自己的全局编号 \(i\)；
3. 守卫条件过滤越界与刻意跳过的元素；
4. 执行 `y[i] += a*x[i]`：一次读 `x`、一次读 `y`、一次写 `y`——三条访存流都落在同一个下标 `i` 上。

#### 4.1.3 源码精读

对齐版 kernel 全文只有 7 行，见 [MemAlign/axpy_cudakernel.cu:L8-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L8-L14)：

```cuda
__global__
void
axpy_cudakernel_1perThread(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i > 0 &&i < n) y[i] += a*x[i];
}
```

三处值得停下来看的细节：

- **`i = blockDim.x * blockIdx.x + threadIdx.x`**：下标就是全局线程编号，无任何附加偏移——这是「对齐」的全部来源。地址由硬件按 `base + i*sizeof(REAL)` 计算，`base` 对齐、`i` 从 0 起，warp 窗口自然从边界开始。
- **守卫是 `i > 0 && i < n` 而不是常见的 `i < n`**：它刻意跳过元素 0。为什么？看串行基线 [MemAlign/axpy_cuda.c:L42-L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L42-L48)，串行版循环写的是 `for (i = 1; i < n; ++i)`——从 1 开始。作者把串行基线、对齐版、非对齐版三者的**覆盖范围统一成同一个区间 \([1, n-1]\)**，这样三者的工作量完全相同，比较才干净（非对齐版因 `+1` 天然从元素 1 开始，对齐版就用 `i > 0` 补齐）。
- 少处理 1 个元素对「对齐」本身没有影响：warp 0 只是 lane 0 闲置，其余 31 个 lane 的地址模式不变。

它在包装函数里的启动点见 [MemAlign/axpy_cudakernel.cu:L47-L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L47-L48)，网格配置为 `(n+255)/256` 个块、每块 256 线程；而 `base` 的对齐保证来自这两行分配，见 [MemAlign/axpy_cudakernel.cu:L35-L36](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L35-L36)：`cudaMalloc(&d_x, n*sizeof(REAL))` 与 `cudaMalloc(&d_y, …)`。

#### 4.1.4 代码实践：纸上推演偏移—扇区表（无需 GPU）

1. **实践目标**：在动手改代码之前，先能**预测**结果。对齐分析是纯算术，纸面即可完成，这一步是后面所有实测的「假设」。
2. **操作步骤**：设数组基址 256B 对齐，warp 的读窗口为 32 个连续 `double`（256B），整体偏移 \(k\) 个元素（字节偏移 \(b = 8k\)）。窗口是半开区间 \([b,\; b+256)\)。用下面两个公式对 \(k = 0, 1, 2, 3, 4, 8, 16\) 分别求触碰数：

   \[
   N_{\text{sector}}(b) = \left\lceil \frac{(b \bmod 32) + 256}{32} \right\rceil,
   \qquad
   N_{\text{line}}(b) = \left\lceil \frac{(b \bmod 128) + 256}{128} \right\rceil
   \]

   （原理：窗口左端落在某个粒度单元内部时，第一个单元被「劈开」，右端就会多越过一个单元。）
3. **需要观察的现象**：把 7 个 \(k\) 值的 \(N_{\text{sector}}\)、\(N_{\text{line}}\)、带宽利用率 \(256 / (N_{\text{sector}} \times 32)\) 填进一张表。
4. **预期结果**：应当得到下表（这就是 4.2.4 实测要检验的假设）：

   | 偏移 \(k\)（元素） | 字节偏移 \(b\) | \(N_{\text{sector}}\) | \(N_{\text{line}}\) | 利用率 |
   | ---: | ---: | ---: | ---: | ---: |
   | 0 | 0B | 8 | 2 | 100% |
   | 1 | 8B | 9 | 3 | 88.9% |
   | 2 | 16B | 9 | 3 | 88.9% |
   | 3 | 24B | 9 | 3 | 88.9% |
   | 4 | 32B | 8 | 3 | 100% |
   | 8 | 64B | 8 | 3 | 100% |
   | 16 | 128B | 8 | 2 | 100% |

   关键规律：\(b \bmod 32 = 0\)（即 \(k\) 是 4 的倍数）时扇区数回到最小值 8；\(b \bmod 128 = 0\)（\(k\) 是 16 的倍数）时缓存行数才回到 2。**本实践为纸面推导，表中数值不依赖运行环境。**

#### 4.1.5 小练习与答案

**练习 1**：`CoMem_AXPY` 的 `REAL` 若为 `float`（4B），偏移多少个元素才不产生额外扇区？

**答案**：扇区 32B / 4B = 8，所以偏移必须是 8 的倍数个元素（32 字节）才满足 \(b \bmod 32 = 0\)；对齐到缓存行则需要 32 个元素（128 字节）。可见「多大的偏移算对齐」取决于元素大小，写 kernel 时要按**字节**而不是按元素思考。

**练习 2**：既然 `cudaMalloc` 保证返回的指针已对齐，为什么 README 还说反模式是「分配的地址未对齐」？

**答案**：README 描述的是**通用问题**（现实中确实存在未对齐的起始地址，例如主机侧 `malloc` 只保证 16B 对齐、或者结构体里数组不从偏移 0 开始）；而设备侧代码用**下标偏移**制造了与「基址未对齐 8 字节」完全相同的地址模式——两者只差一个常数平移，硬件看到的 warp 地址窗口一模一样。这是「用最容易改的一行代码复现同一个硬件效应」的实验技巧。

### 4.2 反模式：`axpy_cudakernel_1perThread_misaligned`

#### 4.2.1 概念说明

非对齐版 kernel 与对齐版**只差一个 `+ 1`**：线程 `t` 不再处理元素 `t`，而是处理元素 `t + 1`。于是 warp `w` 的 32 个地址变成 \([256w + 8,\; 256w + 264)\)——整串连续地址**向右平移了 8 字节**。

后果是窗口的两端都不在边界上：

- 左端 \(256w + 8\) 落在某个扇区/行的**内部**，第一个扇区只装了 24B 有效数据；
- 右端 \(256w + 264\) 越过了下一个边界，多碰一个扇区/行，而那个扇区里只有开头的 8B 有效。

这就是非对齐惩罚的本质：**每个 warp 请求多触碰一个粒度单元，有效带宽利用率从 100% 掉到 88.9%**。它和 u4-l2 的 stride 反模式不同——那里是地址不连续导致扇区数翻倍、翻四倍；这里地址完全连续，只是整体错位，代价温和得多。所以 MemAlign 演示的是存储层次问题里「最轻微」的一档。

#### 4.2.2 核心流程

一条 warp 读指令（读 `x`）在非对齐情形下的落点：

```text
字节地址：  0        32        64        96       128       160       192       224       256      288
            │ sector0 │ sector1 │ sector2 │ sector3 │ sector4 │ sector5 │ sector6 │ sector7 │ sector8 │
warp 0 读   │  ┢──── x[1..15] ────────────────────────────────┛├──── x[16..31] ──────────┢│
（偏移 8B） │  ┢ sector0 只用后 24B ─────────────────────────────────────────── sector8 只用前 8B ─┥
             ↑ 起点错开 8B：9 个扇区 / 3 个缓存行，两端各浪费一截
```

量化一下代价。设偏移字节 \(b = 8k\)，则每个 warp 请求触碰的扇区数为

\[
N_{\text{sector}}(b) =
\begin{cases}
8, & b \bmod 32 = 0 \\
9, & \text{其他}
\end{cases}
\]

偏移 1 个元素时，扇区请求量放大 \(9/8 = 1.125\) 倍（+12.5%），缓存行从 2 条变 3 条（+50%）。

**但要注意总流量并没有放大 12.5%**。相邻 warp 的窗口是**首尾相接**的：warp `w` 多碰的那个右端扇区，正是 warp `w+1` 需要的第一个扇区。对「顺序扫过整个数组」的访问来说，若这个扇区还留在 L1/L2 里，就会被邻居直接复用——**DRAM 层面几乎不多搬一个字节**（整个数组两端各多至 1 个扇区而已）。所以非对齐的真实代价主要是 **L1 层面每条访存指令的扇区请求数变多、地址跨行导致的一次请求被拆成更多次处理**，而不是带宽被放大大 12.5%。这解释了为什么实测惩罚远小于 12.5%（4.3.3 给出数据：约 3%）。反过来说，若访问本身没有这种「邻居复用」（例如每个 block 各自处理一段错位的行），多搬的行不会被任何人复用，惩罚会明显得多——这是把本讲结论迁移到真实代码时必须带上的前提。

#### 4.2.3 源码精读

非对齐版 kernel 见 [MemAlign/axpy_cudakernel.cu:L16-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L16-L22)：

```cuda
__global__
void
axpy_cudakernel_1perThread_misaligned(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x + 1;
    if (i < n) y[i] += a*x[i];
}
```

逐点拆解这个「反模式」有多克制：

- **`+ 1` 是唯一的改动**：地址计算仍是 `base + i*8`，warp 内 32 个地址仍然连续（合并访问保持成立），只是整体右移 8B。这个 kernel 与 4.1 的对齐版构成一对**严格的单变量对照**。
- **覆盖范围与对齐版完全一致**：\(i = t + 1\)，线程总数 \(T \ge n\)，所以 \(i\) 的取值范围是 \([1, T]\)，守卫 `i < n` 砍掉上端后恰好处理 \([1, n-1]\)——与对齐版（`i > 0` 跳过 0）逐元素相同。工作量、访存次数、写回地址全部一样，唯一不同的是线程到元素的映射错开一格。
- **守卫只需 `i < n`**：因为 `i = t + 1 ≥ 1` 恒成立，不会出现负下标，不需要再挡下界。
- 它的启动点见 [MemAlign/axpy_cudakernel.cu:L45-L46](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L45-L46)，网格配置与对齐版**完全相同**（`<<<(n+255)/256, 256>>>`）——并行度也不变。

一句话总结这对 kernel 的实验设计：**同样的元素、同样的网格、同样的访存次数，只有 warp 地址窗口的起点错开 8 字节**。观测到的任何差异都可以归因于对齐。

#### 4.2.4 代码实践：偏移量扫描（本讲核心实践）

这就是本讲指定的实践任务：把偏移量从 1 改成 2、4、8（分别对应 `double` 的 16/32/64 字节错位），回答**多大的错位才不会引起额外事务**。

1. **实践目标**：用实测检验 4.1.4 的理论表——检验「扇区对齐」的门槛到底在 32B（4 个元素）还是 128B（16 个元素），并体会时间差异与事务数差异的量级差距。

2. **操作步骤**：

   a. **修改偏移**：把 [MemAlign/axpy_cudakernel.cu:L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L20) 的 `+ 1` 依次改为 `+ 2`、`+ 4`、`+ 8`（建议一次只改一处，改完编译、测完再改下一组，避免多版本混在一起）。

   b. **同步修改对齐版的守卫，保持覆盖范围一致**：偏移为 \(k\) 时非对齐版处理 \([k, n-1]\)，因此对齐版的守卫也要从 `i > 0` 改成 `i >= k`（即 `i > k-1`，见 [MemAlign/axpy_cudakernel.cu:L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L13)）。**这一步最容易被忽略**：如果不改，两个 kernel 的工作量不同，对比就掺入了第二个变量，违背本基准的控制变量设计。

   c. **编译**：仓库的 [MemAlign/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/Makefile#L1-L2) 写的是 `-arch=sm_30`，新版 CUDA 工具链已不支持 Kepler（u1-l2 讲过），按你的 GPU 改架构号，例如：

   ```bash
   cd MemAlign
   nvcc -g -G -arch=sm_80 -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu   # 架构号按本机 GPU 填
   ```

   `-G` 会关闭设备端优化（u3-l1 讲过它对分支研究的必要性）；本基准只关心地址模式，保留或去掉 `-G` 都可以，但**两组对比必须用同一组编译选项**。

   d. **采集事务数（关键证据）**：时间差异预计只有几个百分点，光看墙钟不够，必须数扇区。新版工具链用 Nsight Compute：

   ```bash
   ncu --metrics l1tex__t_sectors_pipe_lsu_mem_global_op_ld.sum,l1tex__t_requests_pipe_lsu_mem_global_op_ld.sum,dram__bytes_read.sum ./axpy_cuda 10240000
   ```

   （指标名以本机 `ncu --query-metrics` 的输出为准；旧工具链可尝试 `nvprof --metrics gld_efficiency ./axpy_cuda 10240000`，可用性随架构与版本变化，待本地验证。）**扇区数 ÷ 请求数**就是每条 warp 访存指令触碰的扇区数——对齐时应为 8，偏移 1/2/3 时应为 9，偏移 4/8 时应回到 8。

   e. **记录 kernel 时间**：用 `nvprof ./axpy_cuda 10240000`（或 `ncu` 报告的耗时），把三个 kernel 各自的 Avg 记下来。

3. **需要观察的现象**：

   - **扇区/请求比**随偏移的阶梯变化：偏移 1、2、3 → 9；偏移 4、8 → 8。这是判断「是否产生额外事务」的决定性指标。
   - **kernel 时间**随偏移的变化：预计各组之间差异都在百分之几的噪声量级（仓库自带的 Offset=1 实测也只有约 3%，见 4.3.3），偏移 4/8 不见得比偏移 1 明显更快。
   - **DRAM 读字节**（`dram__bytes_read.sum`）几乎不随偏移变化——印证 4.2.2 的分析：顺序扫描下多碰的边缘扇区会被相邻 warp 复用，总流量没有放大。

4. **预期结果与答案**：**多大错位才不引起额外事务？** 按 32 字节扇区（内存事务的实际粒度）衡量，偏移是 **4 个 `double`（32 字节）的整数倍**即可让每个 warp 请求回到最少 8 个扇区、不产生额外扇区事务；若按 128 字节缓存行衡量，则要 **16 个 `double`（128 字节）的整数倍**才回到 2 条行。两个口径都要报告，并注明你的指标（sectors 还是 lines）支持哪一个。同时应当写明：**在 kernel 时间上，1/2/4/8 各组的差异预计落在噪声范围（约 0–4%），结论必须由扇区计数指标支撑，而不是墙钟时间**。本实践需要 NVIDIA GPU 与性能计数器权限，具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把偏移改成 `+ 5` 和 `+ 6`，预测它们的扇区/请求比，并说明它们应与哪一组归为一档。

**答案**：`+5` → 40B，\(40 \bmod 32 = 8 \ne 0\) → 9 个扇区；`+6` → 48B，\(48 \bmod 32 = 16 \ne 0\) → 9 个扇区。它们与偏移 1、2、3 同档（非扇区对齐）。反过来，偏移 100 个元素 = 800B，\(800 \bmod 32 = 0\)，虽然「错得很远」却是扇区对齐的。可见起作用的判据只有 \(b \bmod 32\)（缓存行口径则是 \(b \bmod 128\)）是否为 0，与错开的绝对距离无关。

**练习 2**：为什么这个基准用「下标偏移」而不是真的用 `cudaMalloc` 之后加一个错位指针（例如 `d_x + 1` 传进 kernel）来实现？

**答案**：两者在硬件层面等价（`base + i*8` 与 `(base+8) + (i-1)*8` 算出的地址完全相同）。用下标偏移的好处：其一，不需要改指针类型或担心越界分配，改动最小（一处 `+1`）；其二，对齐版与非对齐版的**覆盖区间可以精确对齐**（配合 `i > 0` 守卫），保证单变量对照；其三，`cudaMalloc` 本来就返回对齐指针，想在分配层面制造错位反而要额外分配再取内部地址，更绕。

**练习 3**：一个 `float` 数组（4B）想用 `float4` 向量化加载（一次读 16B），对地址有什么要求？这与本讲的门槛是什么关系？

**答案**：`float4` 加载要求地址 16B 对齐（否则结果未定义或性能惩罚，依架构而定）。16B 对齐严于 32B 扇区对齐吗？不——16B 对齐并不能保证 32B 扇区对齐（例如偏移 16B 恰好跨在两个扇区的边界上）。向量化访存的对齐要求是**按向量大小**（16B）算的，而事务粒度的对齐要按**扇区（32B）**算，两套门槛互相独立，优化时要同时满足。本基准仓库中没有 `float4` 用法，此题仅作概念延伸（示例说明，非项目代码）。

### 4.3 warmup kernel 与被测 kernel：区分与实测解读

#### 4.3.1 概念说明

源码里有三个 kernel，角色并不相同，读代码时必须先分清谁在「被测」：

| kernel | 守卫条件 | 实际覆盖的下标区间 | 角色 |
| --- | --- | --- | --- |
| `axpy_cudakernel_1perThread_warmup` | `i > 1 && i < n` | \([2, n-1]\) | **预热**，吸收首次启动的一次性开销 |
| `axpy_cudakernel_1perThread_misaligned` | `i < n` | \([1, n-1]\) | **被测**（反模式） |
| `axpy_cudakernel_1perThread` | `i > 0 && i < n` | \([1, n-1]\) | **被测**（参照系） |

「warmup kernel」的概念 u1-l3 与 u2-l4 都提过：kernel 第一次启动要付出指令装载、上下文建立、冷缓存等一次性成本，若直接计时会污染结果。MemAlign 的做法是**独立写一个 warmup kernel**（而非把被测 kernel 多跑一次）——它的守卫刻意取 `i > 1`，覆盖 \([2, n-1]\)，比两个被测 kernel 少处理开头的元素。这样它在功能上不与任何一个被测 kernel 完全重合，但访存形态（同样的连续 `double` 流、同样的网格）与被测 kernel 一致，足以把存储系统「焐热」。

判断「谁是 warmup」的通用线索：**名字**（`_warmup` 后缀）、**执行位置**（在计时关注的 kernel 之前）、**守卫条件的细微差异**，以及最重要的——**在 nvprof 的 GPU activities 表里它单独成行，你可以检查它的时间是否应当被排除在你的结论之外**。

#### 4.3.2 核心流程

`axpy_cuda` 的一次调用（共三个 kernel 顺序执行，每个之后都同步），见 [MemAlign/axpy_cudakernel.cu:L41-L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L41-L48)：

```text
cudaMalloc d_x, d_y            ── L35-36
cudaMemcpy x,y → 设备           ── L38-39
axpy_cudakernel_1perThread_warmup      ── L42  （预热，不计入对比）
cudaDeviceSynchronize                  ── L43
axpy_cudakernel_1perThread_misaligned  ── L45  （被测：反模式）
cudaDeviceSynchronize                  ── L46
axpy_cudakernel_1perThread             ── L47  （被测：参照系）
cudaDeviceSynchronize                  ── L48
cudaMemcpy y ← 设备, cudaFree          ── L51-53
```

host 侧 main 对 `axpy_cuda` 整体调用 10 次取平均（[MemAlign/axpy_cuda.c:L83-L88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L83-L88)），所以 nvprof 表里**每个 kernel 恰好出现 10 次（Calls=10）**——这个数字本身就是核对你的调用链理解的抓手（u1-l4 讲过用 Calls 列反查程序结构）。注意程序打印的 `time` 是**整个 `axpy_cuda` 的平均墙钟**（含 `cudaMalloc`、三次 `cudaMemcpy` 与三个 kernel），远大于任一 kernel 的 GPU 时间；对齐效应必须看 nvprof 的 per-kernel 行。

#### 4.3.3 源码精读：Carina 实测告诉我们什么

warmup kernel 的源码见 [MemAlign/axpy_cudakernel.cu:L24-L30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L24-L30)。归档实验明确标注了 `Offset = 1`，见 [MemAlign/axpy_cuda.output.carina.txt:L3](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt#L3)。取最小规模那一轮的 kernel 三行（[MemAlign/axpy_cuda.output.carina.txt:L16-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt#L16-L18)）：

```text
0.62%  382.01us  10  38.201us  ...  axpy_cudakernel_1perThread_warmup(double*, double*, int, double)
0.58%  355.67us  10  35.567us  ...  axpy_cudakernel_1perThread_misaligned(double*, double*, int, double)
0.56%  342.49us  10  34.249us  ...  axpy_cudakernel_1perThread(double*, double*, int, double)
```

把 6 个规模的 `Avg` 整理成表（数据取自该文件各轮的 GPU activities 段，例如最大规模在 [L131-L133](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt#L131-L133)）：

| n | warmup (µs) | misaligned (µs) | aligned (µs) | misaligned / aligned |
| ---: | ---: | ---: | ---: | ---: |
| 1 024 000 | 38.20 | 35.57 | 34.25 | 1.039 |
| 4 096 000 | 130.56 | 130.05 | 126.19 | 1.031 |
| 10 240 000 | 313.37 | 317.97 | 309.28 | 1.028 |
| 20 480 000 | 623.37 | 639.36 | 620.01 | 1.031 |
| 40 960 000 | 1238.0 | 1271.6 | 1234.2 | 1.030 |
| 102 400 000 | 3084.9 | 3170.6 | 3080.3 | 1.029 |

三个层次的解读，一层比一层重要：

- **非对齐的代价约 3%，且跨 6 个规模非常稳定**。这比 stride 反模式的「慢 20 倍」（u4-l2 的 `block` 分布）温和得多——错位只是让每个 warp 请求多碰一个扇区，地址仍然连续。
- **3% ≪ 12.5% 的扇区请求放大**，印证 4.2.2 的分析：顺序扫描时相邻 warp 的窗口首尾相接，多碰的边缘扇区会被邻居复用，DRAM 总流量几乎不变；真正的开销在 L1 每条访存指令的扇区分组变多。这也提醒我们：**「扇区数 +12.5%」是请求数口径的下界推断，不是带宽或耗时的等比例放大**。
- **warmup 在小规模下反而最慢**（38.20µs > 35.57µs > 34.25µs），大规模下与被测 kernel 几乎持平（3084.9 vs 3080.3/3170.6µs）。合理的解释是：warmup 是 H2D 拷贝之后第一个启动的 kernel，承揽了冷缓存/冷 TLB/首次启动的摊销——这正是它存在的意义；规模变大后这些固定成本被摊薄，三者趋同。（此为基于数据的推断，可在 4.3.4 实践中用「再跑一遍取第二轮」验证，待本地验证。）

另外对照一行工程事实：README 说反模式是「分配地址未对齐」、对策是「用对齐的分配」（[README.md:L56-L59](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L56-L59)），而实现展示的是**下标偏移**这条等价路径。u1-l1 建立的「文档与实地互核」习惯在这里再次适用：读 README 拿到问题意识，读代码看它实际怎么制造问题。

#### 4.3.4 代码实践：从归档数据提取对齐惩罚（无需 GPU）

1. **实践目标**：不依赖 GPU，从仓库自带的 Carina 转录中独立复现上面那张比值表，练「从 nvprof 表格里取数」的手感。
2. **操作步骤**：打开 [MemAlign/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt)，对 6 个 `n`（1024000 / 4096000 / 10240000 / 20480000 / 40960000 / 102400000，与 [MemAlign/test.sh:L1-L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/test.sh#L1-L6) 一一对应）各找出三个 kernel 行的 `Avg` 列，计算 `misaligned / aligned`。
3. **需要观察的现象**：比值是否落在 1.02–1.04 之间；warmup 与两个被测 kernel 的相对大小随 `n` 如何变化；`Calls` 列是否都是 10/10/10（20 与 10 分别对应 HtoD/DtoH 拷贝次数，想一想为什么 HtoD 是 20：`d_x` 和 `d_y` 各拷一次 × 10 轮）。
4. **预期结果**：得到上表的 6 个比值，均值约 1.031；由此写出一句话结论——「在 Carina 的 V100 上，1 个 `double` 的错位使 kernel 慢约 3%，且随规模稳定」。注意该文件是终端转录，stdout 与 stderr 交错（u1-l4 讲过），取数时以 `==...== Profiling result:` 之后的表格为准。本实践全部数据来自仓库文件，无需运行即可完成。

#### 4.3.5 小练习与答案

**练习 1**：如果作者没写 warmup kernel，而是把非对齐 kernel 先空跑一遍再计时，会不会改变结论？

**答案**：对「misaligned vs aligned 的相对比较」影响很小——两者都在 warmup 之后、条件对等。真正的差别在于工程语义：独立的 warmup kernel 让「预热」这个意图在代码里显式可见（名字、位置、守卫都可读出来），而「先空跑一遍被测 kernel」会让 nvprof 表里同一个 kernel 出现两种 Calls 计数，读者难以分辨哪次算预热。把意图写进代码结构，是微基准可维护性的要点。

**练习 2**：三个 kernel 的守卫分别是 `i > 1`、`i < n`、`i > 0 && i < n`。假设有人把 warmup 的守卫误改成与对齐版相同的 `i > 0 && i < n`，程序输出（checksum）会变吗？性能对比会受影响吗？

**答案**：checksum 会变。u2-l4 推导过 MemAlign 的 checksum ≈ 2.0 是「每轮 warmup/misaligned/aligned 对 y 的叠加次数不同」的结构性产物：当前 warmup 覆盖 \([2, n-1]\)，元素 1 每轮被加 2 次、元素 ≥2 被加 3 次；若 warmup 改为覆盖 \([1, n-1]\)，则所有元素 ≥1 每轮被加 3 次，checksum 的数值随之改变。性能对比**基本不受影响**（多处理 1 个元素可忽略），这正说明 checksum 在这套骨架里是结构性指纹、不是误差度量——改动任何 kernel 的覆盖范围都要预期它变化。

## 5. 综合实践：一次完整的偏移扫描实验

把本讲的三块内容（理论表、对照设计、实测口径）串成一个闭环任务，产出一份一页纸实验报告。

**任务**：测量「warp 访存窗口的字节偏移 → 扇区事务数 → kernel 耗时」三者的关系，找出不再产生额外事务的最小偏移。

1. **准备假设表**（纸面，来自 4.1.4）：对 \(k \in \{1, 2, 3, 4, 8, 16\}\) 写出预测的 \(N_{\text{sector}}\) 与 \(N_{\text{line}}\)。
2. **改造代码**：按 4.2.4 的方法逐组修改 [MemAlign/axpy_cudakernel.cu:L20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L20) 的偏移，并**同步**修改对齐版守卫保持覆盖一致；每组编译运行。
3. **采集两类证据**：`ncu` 的扇区/请求比（决定性证据）与 kernel 平均耗时（次要证据），规模建议固定 `n=10240000`（test.sh 的中间档，Carina 数据显示该规模下三 kernel 区分度好且方差小）。
4. **写报告**，至少包含：
   - 假设表与实测表并排，标出吻合与不吻合的项；
   - 一句话回答「多大的错位才不引起额外事务」（分扇区/缓存行两个口径）；
   - `dram__bytes_read.sum` 是否随偏移变化，用它解释「3% vs 12.5%」；
   - 你结论中误差的来源（单机单卡、`-G` 选项、10 轮平均、nvprof 采集本身的开销）。
5. **无 GPU 的替代方案**：以 [MemAlign/axpy_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt) 的 Offset=1 数据为「实验组」，完成报告的第 4 步中所有不依赖新测量的部分，并把扫描部分标注「待本地验证」。

## 6. 本讲小结

- **对齐是 warp 层面的概念**：一串连续地址若不从 32B 扇区（或 128B 行）边界开始，warp 请求就要多触碰一个粒度单元；`double` 场景下 32 个连续元素（256B）从对齐到错位，是 8 扇区/2 行 → 9 扇区/3 行。
- **MemAlign 的实验设计**：两个 kernel 只差一个 `+1`，覆盖区间用守卫（`i > 0` vs 无需下界）精确对齐到 \([1, n-1]\)，网格、访存次数全部相同——单变量对照的范本；串行基线同样从 `i=1` 起循环与之匹配。
- **错位惩罚是温和的且「只看余数」**：\(b \bmod 32\)（或 \(\bmod 128\)）为 0 才免罚，与错开多远无关；对 `double`，4 个元素（32B）的偏移就回到最少扇区数，16 个元素（128B）才回到最少行数。
- **请求数 ≠ 流量 ≠ 耗时**：扇区请求 +12.5%，但顺序扫描下相邻 warp 复用边缘扇区，DRAM 总流量几乎不变，Carina 六个规模实测 kernel 只慢约 3%——解释这三层口径的差距比记住 3% 这个数字更重要。
- **三个 kernel 三种角色**：warmup（覆盖 \([2,n-1]\)、承揽冷启动摊销、不参与对比）、misaligned（反模式）、aligned（参照系）；用名字、守卫、执行位置和 nvprof 的 Calls 列可以随时把它们认出来。
- **README 与代码的口径差**：README 说「分配未对齐 → 用对齐的分配」，代码用下标偏移制造等价的地址模式——同一个硬件效应，取了最容易实现的一条路径。

## 7. 下一步学习建议

- **u4-l5（BankRedux）**：同样是「多触碰一个粒度单元」的思想，搬到共享内存上——bank 冲突让一次共享内存访问串行化为多次，手工推演 bank 编号的方法与本讲的扇区编号推演如出一辙，建议紧接着学。
- **u4-l7（GSOverlap）**：本讲的错位窗口分析工具（「一个 warp 请求落在哪些粒度单元上」）可以直接迁移到全局→共享内存拷贝的粒度分析上，那里还会遇到 `float4` 等 16B 粒度的拷贝单位。
- **u6-l4（实验方法论）**：本讲已经出现了「扇区数 +12.5% 但耗时只 +3%」这类口径问题，u6-l4 系统讨论跨平台结果、计时口径与报告写作，是本讲综合实践的自然延伸。
- 想继续读源码的话，回到 [MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu) 把三个 kernel 的 PTX 打印出来看一眼（`nvcc -ptx`，示例命令，非项目自带），观察 `ld.global`/`st.global` 指令与偏移是如何体现在地址计算上的。
