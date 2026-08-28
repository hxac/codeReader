# BankRedux：共享内存 bank 冲突与归约算法改写

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出共享内存（shared memory）的 **32 bank 组织方式**，并解释 bank 冲突（bank conflict）与广播（broadcast）的判定规则。
2. 手推两个归约 kernel——`sum_cudakernel`（折半步长、连续下标）与 `sum_cudakernel_bc`（跨步下标）——在每一步迭代中，warp 内活跃线程各自触达的 cache 下标与 bank 编号，并标出哪几步发生 2 路、4 路乃至 8 路冲突。
3. 用 nvprof 的 kernel 时间（以及共享内存指标）验证「改寻址方式消除 bank 冲突」的实际收益，并能对照仓库自带的 Carina 归档数据解读结果。
4. 体会 README 对 BankRedux 的一句定性——**「反模式：两个及以上线程访问同一 bank 的不同位置；优化：改算法避免 bank 冲突」**——在源码中具体对应哪两段循环。

## 2. 前置知识

### 2.1 归约（reduction）

「归约」是把 N 个数用一个满足结合律的运算（这里是加法）合成 1 个数的过程。串行写法是一个 for 循环（本项目的串行基线见 [BankRedux/sum_cuda.c:44-51](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L44-L51)，它只作正确性参照，不参与计时）。GPU 上并行归约的标准做法是**树形两两合并**：每一步让一半线程把另一半线程的部分和加进来，步数为 \(\log_2 N\)。一个 block 内 256 个元素只需 8 步。

### 2.2 共享内存回顾（承接 u4-l1）

u4-l1（Shmem 分块矩阵乘）里，共享内存是**复用数据、减少全局内存流量**的片上缓存：`__shared__` 声明、block 内所有线程可见、`__syncthreads()` 作块内栅栏。本讲视角不同：共享内存不再只是优化手段，它本身就是**被研究的对象**——我们要看它的内部结构如何决定访问吞吐。两个内核都声明了同样大小的共享数组：

- [BankRedux/sum.h:6-8](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L6-L8)：`REAL` 是 `float`，`ThreadsPerBlock` 为 256，因此 `cache` 是 256 个 float（1024 字节）。

### 2.3 bank：共享内存的内部组织

共享物理 Bank 模型（CUDA 经典模型，也是本讲实践任务采用的模型）：

- 共享内存被划分为 **32 个 bank**，bank 编号 0～31。
- 每个 bank 宽 4 字节，可以每个周期服务一次访问。
- 地址按 4 字节字（word）**轮流交错**映射到 bank：第 \(j\) 个 4 字节字的 bank 编号为
  \[ \text{bank}(j) = j \bmod 32 \]
  对 `float`（4 字节）数组 `cache` 来说，`cache[j]` 的 bank 就是 \(j \bmod 32\)——这正是本讲实践任务里「bank = (index * 4 / 4) % 32 = index % 32」的由来。若元素换成 8 字节的 `double`，则 bank = \((2j) \bmod 32\)。
- 一个 warp（32 线程）的**同一条**访存指令理想情况下一次完成：32 个地址恰好落在 32 个不同 bank 上，全部并行服务。

### 2.4 冲突与广播：判定规则

对**同一条指令、同一个 warp** 内的 32 个（或活跃的那部分）地址：

| 情形 | 名称 | 后果 |
| --- | --- | --- |
| 不同线程访问**不同 bank** | 无冲突 | 1 次传递完成 |
| 多个线程访问**同一 bank 的不同地址** | **bank 冲突** | 访问被拆成多次串行传递：n 个地址撞在同一 bank = **n 路冲突**，该指令耗时约放大 n 倍 |
| 多个线程访问**完全相同的地址** | 广播（broadcast） | 1 次传递完成，**不算冲突** |

一个直觉类比：32 个 bank 像 32 个收银台，warp 一次派 32 位顾客结账；两位顾客排到**同一个收银台**且买**不同商品**（不同地址）就得排队（冲突）；买**同一件商品**（相同地址）则一次扫码共享（广播）。

两条容易踩坑的细则，本讲源码都会用到：

1. 冲突只统计**同一条指令内、不同线程之间**。同一线程先后两次访问落在同一 bank，不算冲突。
2. 判断对象是 warp 在**某一时刻集体发出**的地址集合，而不是单个线程自己的访问轨迹（u4-l2 讲合并访问时已确立同一原则）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [BankRedux/sum.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L1-L17) | 契约头文件：`REAL`、`ThreadsPerBlock=256`、`VEC_LEN`，以及 `sum_cuda` 的 `extern "C"` 声明 |
| [BankRedux/sum_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L1-L76) | 本讲主角：三个 kernel（`sum_warmingup`、`sum_cudakernel`、`sum_cudakernel_bc`）+ 主机包装函数 `sum_cuda` |
| [BankRedux/sum_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L65-L101) | host 主程序：初始化、串行基线、10 轮平均计时、checksum 打印 |
| [BankRedux/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/Makefile#L1-L2) | 单行式编译：`nvcc -o sum_cuda sum_cuda.c sum_cudakernel.cu` |
| [BankRedux/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/test.sh#L1-L4) | 实验设计：4 个数据规模 × nvprof |
| [BankRedux/sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L1-L97) | Carina 集群上的归档测量结果（无 GPU 环境的云实验数据） |
| [Shuffle/cuda_shuffle/reduction_kernel.cu:109-169](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L109-L169) | 关键旁证：`reduce1`/`reduce2` 与本讲两个 kernel 逐字同构，注释明说了 bank 冲突 |

README 汇总表中 BankRedux 的定位在 [README.md:70-74](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L70-L74)：反模式是「两个及以上线程访问同一 bank 的不同位置」，优化技术是「改变算法以避免 bank 冲突」。

## 4. 核心概念与源码讲解

### 4.1 sum_cudakernel：折半步长、连续下标的无冲突归约（参照系）

#### 4.1.1 概念说明

`sum_cudakernel` 完成「把一个 block 内 256 个元素归约成 1 个和」的任务，是本讲的**优化版/参照系**。它的归约循环用**折半步长 + 连续活跃线程**的写法：

- 每一步 \(i\) 从 128 开始减半到 1；
- 只有编号**连续**的前 \(i\) 个线程（`cacheIndex < i`）干活，线程 \(t\) 读 `cache[t]` 和 `cache[t+i]`，把结果写回 `cache[t]`；
- 因为活跃线程的下标连续，warp 集体发出的地址是**一整段连续区间**，而连续下标必然落在互不相同的 bank 上（\(j \bmod 32\) 对连续 \(j\) 取遍 0～31）——**全程零冲突**。

它同时暴露另一个（与 bank 无关的）低效：每一步都有超过一半的线程闲置、只参与 `__syncthreads()`。这正是 u6-l1 归约优化阶梯继续改进的起点——**无 bank 冲突 ≠ 最优**。

#### 4.1.2 核心流程

```
装载：cache[threadIdx.x] = x[全局线程编号]        ← 连续写共享内存，零冲突
__syncthreads()
for i = 128, 64, 32, 16, 8, 4, 2, 1:            ← 8 步，log2(256)
    if threadIdx.x < i:
        cache[threadIdx.x] += cache[threadIdx.x + i]
    __syncthreads()
thread 0 把 cache[0] 写回 result[blockIdx.x]
```

以 8 元素的小例子画两棵求值树（256 元素只是深度多几层，形态相同）。折半版：

```
i=4:  c0+=c4   c1+=c5   c2+=c6   c3+=c7
i=2:  c0+=c2   c1+=c3
i=1:  c0+=c1           →  结果在 cache[0]
```

每步的关键性质：**活跃线程 t 与它读的两个下标 {t, t+i} 都是「随 t 连续」的集合**。对 warp 内同一条读指令，32 个地址形如 \(\{t_0, t_0+1, \dots, t_0+31\}\)，bank 编号取遍 0～31 各一次。

#### 4.1.3 源码精读

主体在 [BankRedux/sum_cudakernel.cu:24-38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L24-L38)（下方摘录关键行）：

```cuda
__global__ void sum_cudakernel(const REAL *x, REAL *result) {
  __shared__ REAL cache[ThreadsPerBlock];        // 256 个 float
  ...
  cache[cacheIndex] = x[tid];                    // 装载阶段：连续下标
  __syncthreads();
  for (int i = blockDim.x / 2; i > 0; i /= 2) {  // i = 128..1 折半
    if (cacheIndex < i) {
      cache[cacheIndex] += cache[cacheIndex + i];// 连续活跃线程，地址连续
    }
    __syncthreads();
  }
  if (cacheIndex == 0) result[blockIdx.x] = cache[cacheIndex];
}
```

逐点说明：

- [sum_cudakernel.cu:25](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L25)：`__shared__` 数组大小来自 [sum.h:7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L7) 的 `ThreadsPerBlock=256`，每个 block 各有一份私有的 `cache`。
- [sum_cudakernel.cu:30-35](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L30-L35)：归约主循环。注意 `+=` 一条语句实际产生**两次读**（`cache[cacheIndex]` 与 `cache[cacheIndex + i]`）和**一次写**（写回 `cache[cacheIndex]`），三次访问的 warp 地址集各自都是连续段，因此都不冲突。
- [sum_cudakernel.cu:8-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L8-L22)：`sum_warmingup` 与本 kernel **逐字相同**（预热专用，见 u1-l3）。这个「重复」在本讲有个意外用处：它天然构成**对照组**——代码与寻址完全相同的两个 kernel，耗时应当几乎一样（Carina 数据里两者相差 1.5%～4%），从而支持「`_bc` 版慢是因为寻址不同，而不是因为它第三个被启动」的因果推断。
- [sum_cudakernel.cu:65-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L65-L68)：包装函数 `sum_cuda` 依次启动 warmingup → `sum_cudakernel` → `sum_cudakernel_bc`，每个 kernel 之后 `cudaDeviceSynchronize()`；网格为 `(n+255)/256` 个 block、每 block 256 线程，每个 block 归约出 256 个全局元素的和。

**仓库旁证**：这段代码与 `Shuffle/cuda_shuffle/reduction_kernel.cu` 中的 `reduce2` 完全同构——[reduction_kernel.cu:141-169](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L141-L169) 的注释原话是「This version uses sequential addressing -- no divergence or bank conflicts.」（顺序寻址——无发散、无 bank 冲突）。也就是说，BankRedux 把经典归约优化阶梯中的一级（连续寻址修正）单独抽出来做成了教学微基准。

#### 4.1.4 代码实践（纸上推演 A：验证「无冲突」）

1. **实践目标**：对 `sum_cudakernel` 的前 3 步（\(i=128, 64, 32\)），手推 warp 0 内每条访存指令触达的下标集合与 bank 集合，确认零冲突，并解释第 3 步一个看似可疑的现象。
2. **操作步骤**（纯纸面推导，不需要 GPU）：
   - 取 `ThreadsPerBlock = 256`。对每一步 \(i\)，先写出活跃线程范围（`cacheIndex < i`）。
   - 对 warp 0（线程 \(t=0..31\)）分别列出「读 cache[t]」「读 cache[t+i]」「写 cache[t]」三组下标。
   - 用 \( \text{bank}(j) = j \bmod 32 \) 把每个下标换算成 bank，数一数每个 bank 被几个**不同线程**命中。
3. **需要观察的现象**：第 3 步（\(i=32\)）中，线程 \(t\) 读的两个下标 \(t\) 与 \(t+32\) 的 bank 编号相同（都是 \(t \bmod 32\)）——先判断这是不是冲突。
4. **预期结果**（可对照 4.1.5 练习 1 的答案核验）：

   | 步 | \(i\) | 活跃线程 | 读 1 的下标（warp 0） | 读 2 的下标（warp 0） | 每条指令各 bank 命中数 | 冲突 |
   | --- | --- | --- | --- | --- | --- | --- |
   | 1 | 128 | 0..127 | 0..31 | 128..159 | bank 0..31 各 1 次 | 无 |
   | 2 | 64 | 0..63 | 0..31 | 64..95 | bank 0..31 各 1 次 | 无 |
   | 3 | 32 | 0..31 | 0..31 | 32..63 | bank 0..31 各 1 次 | 无 |

   第 3 步的「同 bank」发生在**同一线程**的两次读之间（两次读是两条独立的访存请求），每条请求内部 warp 的 32 个地址仍恰好覆盖 32 个不同 bank——按 2.4 节规则一，不构成冲突。

#### 4.1.5 小练习与答案

**练习 1**：步 \(i=32\) 时 `bank(t) == bank(t+32)`，为什么不算 bank 冲突？

**答案**：冲突的定义是**同一条指令内、同一 warp 的不同线程**访问同一 bank 的不同地址。这里撞在同一个 bank 上的两次访问来自**同一个线程**（读 `cache[t]` 和读 `cache[t+32]` 是两条先后独立的请求）；分别看每条请求，warp 集体触达的地址是连续段 0..31 或 32..63，bank 各命中一次，均无冲突。

**练习 2**：折半版每步超过一半线程闲置（如 \(i=128\) 时线程 128..255 只等 `__syncthreads()`），这是 bank 冲突问题吗？后续该怎么改进？

**答案**：不是。这是**线程利用率**问题（warp 内活跃通道不足），与 bank 组织无关。改进方向：让每线程先在寄存器里累加多个元素（每线程处理多元素的算法级优化），或用 `__shfl_down_sync` 在 warp 内直接于寄存器间归约、绕开共享内存——分别对应 u6-l1 阶梯的 reduce4~reduce6 与 u4-l6 的 shuffle 专题。

**练习 3**：把 [sum.h:7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L7) 的 `ThreadsPerBlock` 改成 128，`sum_cudakernel` 会出现 bank 冲突吗？

**答案**：不会。任意 block 大小下，活跃线程的下标集合始终连续，连续下标 \(\to\) 连续不同 bank，该性质与 256 还是 128 无关。但要注意一个陷阱：包装函数 `sum_cuda` 里**硬编码**了 256（见 [sum_cudakernel.cu:61-69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L61-L69) 的 `(n+255)/256` 与启动配置 `<<<..., 256>>>`），只改宏不改这些数字，kernel 仍会按 256 线程/block 启动——详见第 5 节综合实践。

### 4.2 sum_cudakernel_bc：跨步下标的归约树与 bank 冲突

#### 4.2.1 概念说明

`sum_cudakernel_bc`（后缀 `_bc` 即 bank conflict）计算**完全相同**的结果，装载阶段也完全相同，唯独归约循环换了寻址方式：步长 \(i\) 从 1 倍增到 128，线程 \(t\) 负责的下标是

\[ \text{index} = 2 \cdot i \cdot t, \qquad \text{执行 } \; \text{cache}[\text{index}] \;{+}{=}\; \text{cache}[\text{index} + i] \]

活跃线程按 `index < blockDim.x` 过滤，即第 \(i\) 步活跃线程数为 \(256/(2i)\)。这写成「交错/跨步寻址」（interleaved addressing）：**活跃线程的编号连续，但它们访问的下标以 \(2i\) 为跨步跳着走**。于是 warp 的 32 个地址不再连续，bank 编号 \((2it) \bmod 32\) 开始互相碰撞：

- 第 1 步（\(i=1\)，跨步 2）：warp 的 32 个偶数下标 \(\{0,2,\dots,62\}\) 只覆盖 16 个偶数 bank \(\{0,2,\dots,30\}\)，每个 bank 被 2 个不同线程命中——**教科书式的 2 路冲突**，该条指令被拆成 2 次串行传递，共享内存吞吐折半；
- 步数越深跨步越大，地址进一步向少数 bank 聚拢：第 2 步 4 路、第 3 步 8 路（TPB=256 时的最坏情况）；
- 冲突路数最终受「该步活跃线程数」封顶（活跃线程不足时想撞也撞不满）。

这就是 README 所说的反模式：「两个及以上线程访问同一 bank 的不同位置」。**算法（两两合并的树）没变，变的只是地址映射，性能就差了一截**——这是本讲最想传递的观念。

#### 4.2.2 核心流程

8 元素小例子上的求值树（与 4.1.2 的折半版对照，结果相同、结合次序不同）：

```
i=1:  c0+=c1   c2+=c3   c4+=c5   c6+=c7
i=2:  c0+=c2   c4+=c6
i=4:  c0+=c4           →  结果在 cache[0]
```

TPB=256 时的完整 8 步推导表（**本讲核心**，建议亲手算一遍再核对）：

| 步 \(i\) | 活跃线程数 | 读/写下标跨步 \(2i\) | 覆盖的 bank 数 \(\frac{32}{\gcd(2i,32)}\) | 每路冲突的线程数（= 冲突度） |
| --- | --- | --- | --- | --- |
| 1 | 128 | 2 | 16（偶数 bank） | **2 路** |
| 2 | 64 | 4 | 8 | **4 路** |
| 4 | 32 | 8 | 4 | **8 路** |
| 8 | 16 | 16 | 2 | 8 路 |
| 16 | 8 | 32 | 1（全部 bank 0 / bank 16） | 8 路 |
| 32 | 4 | 64 | 1 | 4 路 |
| 64 | 2 | 128 | 1 | 2 路 |
| 128 | 1 | 256 | 1 | 1（无冲突） |

（「读 A」= `cache[2it]`，「读 B」= `cache[2it+i]`，两者冲突度相同：B 的 bank 集合只是 A 平移 \(i\) 个位置。）

若想一步算出任意步的冲突度，可用（对活跃线程都在同一 warp 内、即活跃数 ≤ 32 的情形；\(A_i\) 为该步 warp 内活跃线程数，\(A_i = \min(32,\; \text{blockDim}/2i)\)）：

\[ \text{冲突路数}(i) \;=\; \left\lceil \frac{A_i \times \gcd(2i,\,32)}{32} \right\rceil \]

直觉：跨步 \(2i\) 让地址只在 \(32/\gcd(2i,32)\) 个 bank 里打转，32 个线程摊到这些 bank 上，每个 bank 被 摊到 \(A_i \times \gcd(2i,32)/32\) 个线程（向上取整）。代入 \(i=1\)：\(32 \times 2 / 32 = 2\)；代入 \(i=8\)：\(16 \times 16/32 = 8\)。与上表一致。

**为什么冲突度封顶在 8**：一步内被拆成的串行传递次数不可能超过「同 bank 撞车的线程数」，而活跃线程数随步数减半；TPB=256 时最深也就 8 个线程同撞一个 bank。把 block 加大到 512，冲突可以深到 16 路（见 4.2.5 练习 1）。

#### 4.2.3 源码精读

主体在 [BankRedux/sum_cudakernel.cu:40-55](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L40-L55)：

```cuda
__global__ void sum_cudakernel_bc(const REAL *x, REAL *result) {
  __shared__ REAL cache[ThreadsPerBlock];
  ...
  cache[cacheIndex] = x[tid];                   // 装载阶段与折半版完全相同
  __syncthreads();
  for (int i = 1; i < blockDim.x; i *= 2) {     // i = 1,2,...,128 倍增
    int index = 2 * i * cacheIndex;             // ← 唯一的实质差异：跨步下标
    if (index < blockDim.x) {
      cache[index] += cache[index + i];         // warp 地址跨步 2i → 冲突
    }
    __syncthreads();
  }
  if (cacheIndex == 0) result[blockIdx.x] = cache[cacheIndex];
}
```

逐点说明：

- [sum_cudakernel.cu:46-47](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L46-L47)：差异的核心就这两行——循环方向从「折半」换成「倍增」，访问下标从 `cacheIndex` 换成 `2*i*cacheIndex`。装载阶段（L44）、栅栏、写回（L53-54）与折半版一字不差，构成了严格的**单变量对照**：唯一被测变量就是归约循环的寻址模式。
- [sum_cudakernel.cu:48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L48)：守卫条件换成 `index < blockDim.x`，作用与折半版的 `cacheIndex < i` 等价——筛选出该步的活跃线程（第 \(i\) 步恰好 \(256/(2i)\) 个）。
- [sum_cudakernel.cu:69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L69)：包装函数中它最后被启动，随后 D2H 拷回的是**它**的逐 block 结果（[sum_cudakernel.cu:72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L72)），所以 main 打印的 checksum 反映的是带冲突版本的输出。

**仓库旁证**：它与 `Shuffle/cuda_shuffle/reduction_kernel.cu` 的 `reduce1` 逐字同构，而且那里的注释直接点破了本讲主题——[reduction_kernel.cu:109-111](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L109-L111)：「This version uses contiguous threads, but its interleaved addressing results in many shared memory bank conflicts.」（线程连续，但交错寻址导致大量共享内存 bank 冲突）。注意它强调的正是 2.4 节细则二：**线程编号连续不重要，warp 集体发出的地址连续才重要**。

**实测证据（Carina 归档，单次调用平均耗时）**：取自 [sum_cuda.output.carina.txt:12-16](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L12-L16)（n=102400）等四段：

| n | block 数 | `sum_cudakernel`（µs） | `sum_cudakernel_bc`（µs） | 冲突版慢 |
| --- | --- | --- | --- | --- |
| 102400 | 400 | 3.017 | 3.385 | 12.2% |
| 204800 | 800 | 4.431 | 5.254 | 18.6% |
| 409600 | 1600 | 6.959 | 8.467 | 21.7% |
| 1024000 | 4000 | 14.723 | 18.236 | 23.9% |

（同表中的 `sum_warmingup` 与 `sum_cudakernel` 代码相同、耗时几乎相同，是天然的对照组。）两点解读：

1. 差距 12%～24% 且随规模扩大。注意 block 内的归约树与 n 无关（树深恒为 8），差距变大的一种合理解释是：n 越大并发驻留的 block/warp 越多，访存单元越忙，bank 串行化越难被其他指令掩盖——此为待本地验证的假设，也可能混杂 L2、频率提升等因素。
2. 记住 u1-l4 的教训：程序打印的 `time`（约 42～57ms）被 cudaMalloc 等主机开销淹没，三个 kernel 每轮合计只有几微秒到几十微秒；**讨论 bank 冲突必须看 nvprof 的 kernel 时间，不能看 wall time**。

#### 4.2.4 代码实践（纸上推演 B + nvprof 验证）

1. **实践目标**：先在纸面推出 `_bc` 版前 3 步的冲突位置与冲突度，再用 nvprof 的 kernel 时间验证「寻址不同 → 耗时不同」。
2. **操作步骤**：
   - **纸面部分**：对 \(i=1,2,4\) 三步，分别列出 warp 0 内活跃线程的两个读下标集合（\(2it\) 与 \(2it+i\)）、换算 bank、数每个 bank 被几个线程命中，标出冲突路数。
   - **运行部分**：在有 NVIDIA GPU 的机器上
     ```
     cd BankRedux && make
     ./sum_cuda 102400                                # 先看程序输出格式
     nvprof ./sum_cuda 102400                         # 概览三个 kernel 时间
     sh test.sh                                       # 4 个规模全跑（u1-l4 讲过输出格式）
     ```
   - **可选加分**：用共享内存指标直接观测冲突（无冲突的 warp 访问每请求约 1 次事务，k 路冲突 ≈ k 次）：
     ```
     nvprof --metrics shared_load_transactions_per_request,shared_store_transactions_per_request ./sum_cuda 102400
     ```
     指标名随 profiler 版本可能不同：报错时用 `nvprof --query-metrics` 查询，或在新工具链上改用 `nsys profile` / `ncu --section MemoryWorkloadAnalysis`（u1-l2 已交代 nvprof → nsys/ncu 的替代关系）。待本地验证。
3. **需要观察的现象**：
   - nvprof 概览中 `sum_cudakernel_bc` 的 Avg 明显大于 `sum_cudakernel`，且 `sum_warmingup` ≈ `sum_cudakernel`（对照组）；
   - （若指标可用）`_bc` 版的 transactions_per_request 明显高于 1，而无冲突版接近 1。
4. **预期结果**：

   | 步 \(i\) | 活跃线程 | 读 A 下标（warp 0） | A 的 bank 集合 | 冲突 |
   | --- | --- | --- | --- | --- |
   | 1 | 0..127 | 0,2,4,…,62 | {0,2,…,30} 各 2 线程 | **2 路** |
   | 2 | 0..63 | 0,4,8,…,124 | {0,4,…,28} 各 4 线程 | **4 路** |
   | 4 | 0..31 | 0,8,16,…,248 | {0,8,16,24} 各 8 线程 | **8 路** |

   （读 B = A 整体平移 \(i\)，冲突度相同。）运行侧预期：冲突版慢 10%～25%，且随 n 扩大趋势；若你的 GPU 架构较新（共享缓存更大、调度更宽），差距可能小于 Carina——记下实际数字即可。无 GPU 环境时，用上表 Carina 数据完成「云验证」，并标注「未本地复现」。

#### 4.2.5 小练习与答案

**练习 1**：把 block 大小改为 512，`_bc` 版最深会出现几路冲突？出现在哪一步？

**答案**：16 路，出现在 \(i=8\) 这一步。此时活跃线程 \(t < 512/(2\cdot8) = 32\) 个（恰好一个 warp），读 A 的下标为 \(16t\)，bank 只落在 {0, 16} 两个上，每个 bank 被 16 个线程命中 → 16 路。用公式验证：\(A_8 = 32\)，\(\gcd(16,32)=16\)，\(32\times16/32 = 16\)。（同时记得：真正实验必须同步修改 [sum_cudakernel.cu:61-69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L61-L69) 里硬编码的 256，见第 5 节。）

**练习 2**：给 `_bc` 版的共享数组加 padding（声明成 `cache[ThreadsPerBlock + 1]`）能消除冲突吗？

**答案**：不能。bank 由**元素的地址**决定：`cache[j]` 恒在基地址偏移 \(4j\) 字节处，bank 恒为 \(j \bmod 32\)，与数组声明多长无关。padding 有效的经典场景是**访问跨步恰好等于（或相关于）二维数组行宽**的列访问——行宽 +1 把相邻行的同一列错开到不同 bank。本例是线性归约，访问下标本身就是 \(2it\)，要让地址错开只能改下标计算——也就是**改算法**（回到折半连续版），恰好印证 README 的优化建议「Change the algorithm to avoid bank conflicts」。

**练习 3**：两个 kernel 数学上算的是同一个和吗？浮点结果会完全一致吗？

**答案**：数学上相同（同一个两两合并的树深，只是「谁加谁」的配对次序不同）。但浮点加法不满足结合律，两棵树的**结合次序**不同（对照 4.1.2 与 4.2.2 的 8 元素树：折半版是 \((c_0+c_4)+(c_2+c_6)\) 一路、`_bc` 版是 \((c_0+c_1)+(c_2+c_3)\) 一路），逐 block 结果可能有极微小的舍入差异。另外 main 打印的 checksum 还叠加上一轮遗留值与未初始化数据（[sum_cuda.c:92-93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.c#L92-L93) 的归并循环用的是 `VEC_LEN` 而非 `n`，n < VEC_LEN 时会把未写入的项也加进来，u1-l4 已定性过），所以 checksum 只能作定性探针，不能当精度证据。

## 5. 综合实践

**任务：完成一份「BankRedux 冲突分析」一页实验报告**，把纸面推导、nvprof 测量、参数实验串起来。

1. **实践目标**：验证「归约树寻址模式决定 bank 冲突，进而决定共享内存吞吐」，并体验一次完整的「预测 → 测量 → 解释」闭环。
2. **操作步骤**：
   1. **推导**：完成 4.1.4 与 4.2.4 的两张前三步推演表，写出每步的冲突路数与理由。
   2. **测量**：`make` 后依次 `nvprof ./sum_cuda 102400`、`sh test.sh`，把三个 kernel 在 4 个规模下的 Avg 耗时填进 4.2.3 那张对照表。
   3. **对照**：与 [sum_cuda.output.carina.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cuda.output.carina.txt#L1-L97) 归档数据并排比较，计算两台机器上的 slowdown 百分比。
   4. **参数实验（改 block 大小）**：把 BankRedux **复制一份到自己的实验目录**（不要改仓库源码），然后：
      - 改 [sum.h:7](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum.h#L7) 的 `ThreadsPerBlock` 为 512；
      - **关键**：同步修改 [sum_cudakernel.cu:61、65、67、69、72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L61-L72) 里 5 处硬编码的 `256`（`(n+255)/256` 与 `<<<..., 256>>>`），否则 kernel 仍按 256 线程启动，宏只改了共享数组大小，实验会静默失效；
      - 重编译运行，检验 4.2.5 练习 1 预测的「16 路冲突」是否表现为差距进一步拉大。
   5. **报告**：一页纸写清——推导表、测量表、与 Carina 的对照、block=512 的结果、以及一段结论：**改寻址（算法）为什么比任何「保持交错寻址的补救」都更对症**。
3. **需要观察的现象**：`_bc` 版全程慢于折半版；`sum_warmingup` ≈ `sum_cudakernel`；block=512 后差距扩大；程序打印的 `time`（wall time）几乎不反映这些差异。
4. **预期结果**：冲突版慢 10%～25%（Carina 上 12%～24%），且随规模与 block 大小单调扩大；若本机架构较新导致差距缩小，如实记录并在报告中讨论架构差异（呼应 u6-l4 的方法论）。无 GPU 时：推导 + Carina 数据分析照常完成，参数实验标注「待本地验证」。

## 6. 本讲小结

- 共享内存按 4 字节字交错映射到 32 个 bank：\(\text{bank}(j) = j \bmod 32\)（float）；同一指令、同一 warp 内不同线程撞进同一 bank 的不同地址 = **n 路冲突**，该指令串行 n 次；相同地址则是广播，不算冲突。
- `sum_cudakernel`（折半步长 + 连续活跃线程，[sum_cudakernel.cu:24-38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L24-L38)）的 warp 地址始终连续 → 全程零冲突；但它每步闲置过半线程，无冲突不等于最优。
- `sum_cudakernel_bc`（步长倍增 + 跨步下标 `2*i*t`，[sum_cudakernel.cu:40-55](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu#L40-L55)）：第 1 步 2 路、第 2 步 4 路、第 3 步起 8 路冲突（TPB=256 时封顶 8 路），冲突度 = \(\lceil A_i \cdot \gcd(2i,32)/32 \rceil\)。
- 两个 kernel 装载、栅栏、写回完全一致，唯一变量是归约寻址——单变量对照使 Carina 上 12%～24% 的耗时差可以直接归因于 bank 冲突；`sum_warmingup` 是天然的重复代码对照组。
- 交错寻址版与仓库 `Shuffle/cuda_shuffle` 的 `reduce1` 同构（注释明说 bank conflicts），折半版与 `reduce2` 同构（sequential addressing, no bank conflicts）——BankRedux 正是经典归约优化阶梯的一级被抽出来做成的教学切片。
- 结论口径：bank 冲突的代价要看 nvprof 的 kernel 时间（或共享内存事务指标），程序打印的 wall time 被显存分配搬运淹没，看不出来。

## 7. 下一步学习建议

- **下一讲 u4-l6（Shuffle）**：`__shfl_down_sync` 让 warp 内归约直接在寄存器间交换数据，**根本不经由共享内存**，从机制上绕开了 bank 这一整层问题——可对照本讲思考「绕开」与「改写寻址」两条路线的适用场景。
- **u6-l1（reduce0→reduce6 优化阶梯）**：把本讲的两个 kernel 放回完整阶梯——`reduce1`（本讲 `_bc`，交错寻址 + bank 冲突）→ `reduce2`（本讲折半版，顺序寻址）只是前两级，后面还有消除模运算、展开、最后单块归约、每线程多元素等算法级优化，串成一条完整的优化决策链。
- **回看 u4-l1（Shmem 分块矩阵乘）**：用本讲的 bank 模型重新审视按列访问 tile 的场景，理解「padding 一列」在那里为何有效（练习 2 的对偶问题）。
- **延伸阅读方向（待确认具体可用性）**：CUDA C++ Programming Guide 的「Shared Memory」一节关于 bank 组织、64 位访问与广播规则的架构代际差异；`ncu` 的 Memory Workload Analysis 面板可以直接显示每指令共享内存事务数。
