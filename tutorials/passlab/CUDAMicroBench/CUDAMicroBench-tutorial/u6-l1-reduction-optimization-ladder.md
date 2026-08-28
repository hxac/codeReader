# 综合案例：reduce0 到 reduce6 的七步归约优化阶梯

## 1. 本讲目标

`Shuffle/cuda_shuffle` 目录下的 `reduction_kernel.cu` 中并排放着 7 个完成同一任务的 kernel——把一个长数组求和成一个数。它们不是 7 种并列的写法，而是一条**逐步演进的优化阶梯**：每一级都精确针对上一级暴露的一个瓶颈。这是整个 CUDAMicroBench 中最适合精读的单一文件。

学完本讲，你应当能够：

1. **逐版本说出每步优化针对的具体瓶颈**：从模运算开销、intra-warp 分支发散、bank 冲突、整 warp 闲置、块栅栏过多，到循环开销，最后到「每线程元素数」这个算法级变量。
2. **区分两类优化**：实现级优化（寻址方式、循环展开、同步削减——不改变工作量分布）与算法级优化（改变每线程处理的元素数，即改变并行形状）。
3. **建立可迁移的 CUDA kernel 优化检查清单**：把这条阶梯泛化成一套可以套用到任意 kernel 上的排查顺序。
4. 熟练使用 `reduction` 程序的命令行（`--kernel=N`、`--n`、`--maxblocks`、`--shmoo`）与测量框架（warmup、100 轮平均、多级归约级联），并能识别仓库自带 `result.txt` 的测量口径。

本讲是 **u4-l5（bank 冲突）** 与 **u4-l6（warp shuffle）** 的综合承接：那两讲建立的概念在这里被串成一条完整的决策链。

## 2. 前置知识

本讲默认你已掌握前置讲义的内容，这里只做最简回顾与补充：

- **归约（reduction）**：用二元结合运算（这里是加法）把数组折叠成一个值。块内归约的经典结构是折半树：每一步活跃线程数减半，需要 \( \log_2 \text{blockDim} \) 步（256 线程为 8 步）。
- **warp 与发散**（u3-l1）：warp 是 32 个线程的调度单位。同一 warp 内线程在分支处意见不一（intra-warp divergence）时硬件串行执行两个臂；而「整 warp 同走一条臂」几乎无代价。
- **bank 冲突**（u4-l5）：共享内存按 4 字节字交错分布到 32 个 bank，float 的 bank 号即下标 mod 32。同一 warp 同一指令访问同 bank 的不同地址会被串行化。
- **warp shuffle**（u4-l6）：`__shfl_down_sync(mask, val, offset)` 让 lane \( i \) 直接读 lane \( i+\text{offset} \) 的寄存器，数据不落共享内存，因此**不需要块栅栏**——warp 内同步由指令自身完成。
- **cooperative groups**：本文件用 `cg::sync(cta)` 代替传统的 `__syncthreads()`，用 `cg::thread_block_tile<32>` 表示一个 warp 大小的组，二者语义等价但可组合。
- **模运算在 GPU 上很贵**：GPU 没有整数除法/取模的专用电路，`a % b` 会被编译成一长串指令（ reciprocal 乘法 + 修正），开销可达移位/比较的数十倍。源码注释原话："This operator is very expensive on GPUs"（[reduction_kernel.cu:L77-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L77-L80)）。
- **Brent 定理的直觉**：对归约这类树形计算，若串行工作量是 \( n \)，用 \( p \) 个处理器，最优可做到大约
  \[ T(p) \approx \frac{n}{p} + \log_2 p \]
  即先让每个线程串行累积 \( n/p \) 个元素，再花 \( \log_2 p \) 步树形合并。「每线程多扛几个元素」不是偷懒，而是理论保证的更优映射。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Shuffle/cuda_shuffle/reduction_kernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L1-L671) | **本讲主角**。7 个归约 kernel 模板（reduce0~reduce6）+ 主机侧 `reduce<T>` 分发包装函数，共 671 行 |
| [Shuffle/cuda_shuffle/reduction.cpp](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L1-L563) | 测量框架：命令行解析、规模计算 `getNumBlocksAndThreads`、100 轮计时 `benchmarkReduce`、多规模扫描 `shmoo`、Kahn…（Kahan）CPU 参照 |
| [Shuffle/cuda_shuffle/reduction.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.h#L29-L36) | 声明模板函数 `reduce<T>`，连接 .cpp 与 .cu |
| [Shuffle/cuda_shuffle/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/test.sh#L1-L4) | 4 条运行命令：n = 2^24 ~ 2^27，**均未传 `kernel=`**（即用默认 kernel） |
| [Shuffle/cuda_shuffle/result.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L1-L72) | V100 (Tesla V100-PCIE-32GB) 上 test.sh 的输出转录，4 个规模全部 `Test passed` |
| [Shuffle/cuda_shuffle/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L1-L305) | CUDA Samples 风格 Makefile；`-I../../Common` 引入 helper 头（L243），产物名为 `reduction.out`（L295-L296） |

三个容易踩的坑，先记下来：

1. **默认 kernel 是 3 不是 6**。文件头注释宣称 `--kernel=<N>` "default 6"（[reduction.cpp:L53](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L53)），但代码实际初始化 `int whichKernel = 3;`（[reduction.cpp:L424](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L424)）。`result.txt` 里的 4 次运行都没传 `kernel=`，所以**归档数据全部是 kernel 3（shuffle 版）的测量**，不是七版本对比——这正是本讲综合实践要你亲手补齐的实验。
2. **可执行文件名是 `reduction.out`**。Makefile 的 `run:` 目标写的是 `./reduction`（[Makefile:L298-L299](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L298-L299)），是样本原始名残留；`test.sh` 用的 `./reduction.out` 才是对的。
3. **打印的 `Time` 不是单个 kernel 的时间**。它是一次「全量 kernel + 多级归约级联（含设备间拷贝）」完整流水线的墙钟，100 轮取平均（详见 4.4）。比较 7 个 kernel 的纯 kernel 时间必须用 nvprof/ncu。

## 4. 核心概念与源码讲解

七个 kernel 共享同一个三段式骨架：**装载**（全局内存 → 共享内存/寄存器）→ **块内树形归约** → **写出**（`g_odata[blockIdx.x]`，每块一个部分和）。跨块合成最终结果由主机侧级联完成（4.4 讲）。下面按阶梯顺序拆成四个模块。

### 4.1 阶梯第 1~2 级：reduce0 与 reduce1——模运算发散与 bank 冲突

#### 4.1.1 概念说明

`reduce0` 是最朴素的正确写法：每线程装载 1 个元素，然后 `s = 1, 2, 4, ...` 逐倍增大步长，凡 `tid % (2s) == 0` 的线程执行 `sdata[tid] += sdata[tid + s]`。它有**三重**代价：

1. **模运算本身很贵**（每次循环迭代、每个线程都要算一次 `tid % (2s)`）；
2. **活跃线程交错分布**：第 \( s \) 步的活跃线程是 \( 0, 2s, 4s, \dots \)，散布在块内**所有** warp 里。直到最后一步，每个 warp 都至少剩 1 个活跃线程，没有任何 warp 能整块退休——所有 8 个 warp（256 线程）都要陪着空转每一步。源码注释："the interleaved inactivity means that no whole warps are active"（[reduction_kernel.cu:L77-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L77-L80)）。
3. 交错下标同时还带来 **intra-warp 分支发散**：同一 warp 内一半线程进 `if`、一半不进，硬件串行走两个臂。

`reduce1` 的改法：把「哪些线程活跃」从交错改为**连续**——令 `index = 2 * s * tid`，线程 \( 0,1,2,\dots \) 依次负责归约对 \( (0,s), (2s, 3s), \dots \)。这一步同时消灭了模运算（一次乘法代替取模）和发散的交错性：活跃线程连续排在块头部，闲置的是**整个整个的 warp**，整 warp 统一走 else 路径几乎免费。

但 reduce1 引入了新的、也是它命名的瓶颈：**跨步访问共享内存产生 bank 冲突**。这正是 u4-l5 在 BankRedux 中见过的同一棵树——`sum_cudakernel_bc` 与 `reduce1` 是同构的。

#### 4.1.2 核心流程

reduce0 的循环（blockDim = 256 为例）：

```text
s = 1:   活跃 tid = 0,2,4,...,254   （每 warp 16 个活跃，8 个 warp 全员在场）
s = 2:   活跃 tid = 0,4,8,...,252
...
s = 128: 活跃 tid = 0               （但 8 个 warp 仍都要执行这一步）
```

reduce1 的循环：

```text
s = 1:   index = 2·tid < 256 → tid 0..127 活跃，访问 sdata[0,2,...,254]（步长 2）
s = 2:   index = 4·tid < 256 → tid 0..63，访问 sdata[0,4,...,252]（步长 4）
...
s = 16:  tid 0..7，访问 sdata[0,32,...,224]（步长 32，全落 bank 0）
```

bank 冲突的度数可以手推：warp 内地址步长为 \( 2s \) 个字，bank 号 = 地址 mod 32，同 bank 相撞的线程数为

\[ \text{冲突路数} = \gcd(2s,\ 32) \quad(\text{且以活跃线程数为上限}) \]

对 256 线程的块：\( s=1,2,4 \) 依次是 2 路、4 路、8 路；\( s=8,16 \) 理论上更高，但活跃线程数（16、8）先耗尽，封顶在 **8 路**。这与 u4-l5 的结论完全一致。

#### 4.1.3 源码精读

reduce0 全文（关注 L98 的模判断与 L99 的跨步累加）：

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L81-L107](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L81-L107) —— 朴素交错归约：L91 越界守卫装载（`(i < n) ? g_idata[i] : 0`）；L96-L103 主循环，**L98 `if ((tid % (2 * s)) == 0)` 是本版两大病灶之一**；L106 块首线程写出部分和。

reduce1 全文：

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L112-L139](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L112-L139) —— L127-L135 主循环：**L128 `int index = 2 * s * tid;` 用一次乘法替换了取模，并把活跃线程改为连续**；L130 `if (index < blockDim.x)` 取代了模判断；L131 `sdata[index] += sdata[index + s]` 则是新的病灶——跨步寻址引发 bank 冲突。

另外注意两个版本共用的基础设施：

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L42-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L42-L68) —— `SharedMemory<T>` 工具类：用 `extern __shared__` 声明**大小在启动时才指定**的动态共享内存（对应启动配置的第三个参数 `smemSize`，见 4.4），并为 `double` 写特化版本以避免未对齐访问的编译错误。7 个 kernel 都通过 `T *sdata = SharedMemory<T>();` 取得这块内存。

#### 4.1.4 代码实践

**实践目标**：用纸面推演确认「交错 vs 连续」活跃线程分布与 bank 冲突度数，再用 nvprof 佐证。

1. **纸面推演**：设 `blockDim.x = 256`，画一张 8 行（warp 0~7）× 8 列（s = 1~128）的表格，逐格填写 reduce0 与 reduce1 在该步该 warp 的活跃线程数。验证：reduce0 的表格没有任何一格是 0（没有 warp 能退休），而 reduce1 从 `s = 4` 起开始出现整行 0。
2. **运行对比**（需 GPU）：

   ```bash
   cd Shuffle/cuda_shuffle
   make
   ./reduction.out n=16777216 threads=256 kernel=0
   ./reduction.out n=16777216 threads=256 kernel=1
   ```

3. **profiler 佐证**：

   ```bash
   nvprof ./reduction.out n=16777216 kernel=0
   nvprof --metrics branch_efficiency ./reduction.out n=16777216 kernel=0
   ```

4. **需要观察的现象**：kernel 0 与 kernel 1 完成同样工作，kernel 1 应当更快；`branch_efficiency`（未发散分支占比）在 kernel 1 上应更高（其分支是 warp 对齐的）。
5. **预期结果**：kernel 0 ≈ kernel 1 或 kernel 1 略优——因为 reduce1 消除了模运算和发散，却背上 bank 冲突，两项收益部分抵消。具体倍数关系**待本地验证**；共享内存冲突指标可用 `nvprof --metrics shared_load_transactions_per_request` 观察（每次请求的事务数 > 1 即有冲突；新工具链请改用 `ncu` 的 bank conflict 计数器，名称以 `ncu --query-metrics` 输出为准）。

#### 4.1.5 小练习与答案

**练习 1**：reduce0 的循环为什么直到最后一步都不能让任何 warp 退休？

**答案**：第 \( s \) 步活跃线程是 \( 2s \) 的倍数。当 \( 2s \le 256 \)（即整个循环范围）时，任意连续 32 个 tid 的区间内必然含有至少一个 \( 2s \) 的倍数，所以 8 个 warp 每一步都至少各剩 1 个活跃线程，全部要参与循环体与栅栏。

**练习 2**：blockDim = 256 时，reduce1 在 `s = 16` 这一步的冲突是几路？为什么不是理论值 32 路？

**答案**：该步活跃线程为 tid 0~7（`index = 32·tid < 256`），访问 `sdata[0], sdata[32], ..., sdata[224]`，下标 mod 32 全为 0，8 个线程全落 bank 0，是 **8 路**冲突。理论公式 \( \gcd(32,32)=32 \) 要求 warp 内 32 个线程都活跃，但此处整个 warp 只有 8 个活跃线程，冲突度被活跃线程数封顶。

**练习 3**：reduce0 与 reduce1 的共享内存访问步长其实相同（都是 \( 2s \)），为什么教材叙事只把 bank 冲突算在 reduce1 头上？

**答案**：因为优化叙事按「主导瓶颈」分级：reduce0 同时患有模运算、交错发散、bank 冲突三病，先治前两病得到 reduce1，此时 bank 冲突成为**剩余的、最显眼的**瓶颈，成为下一级（reduce2）的靶子。这是优化阶梯的一般方法论：每一步只改一个变量，使收益可归因。

### 4.2 阶梯第 3~4 级：reduce2 与 reduce3——顺序寻址消除冲突，shuffle 消除同步

#### 4.2.1 概念说明

`reduce2` 反转循环方向：步长 \( s \) 从 `blockDim/2` 开始**折半**，活跃线程恒为 `tid < s`（连续的前缀），访问 `sdata[tid] += sdata[tid + s]`——两个操作数都是**连续下标**。这一个改动同时治好两种病：

- **零 bank 冲突**：warp 内地址连续，32 个线程落在 32 个不同 bank；
- **warp 对齐的闲置**：闲置线程仍然是整 warp 整 warp 地退场。

`reduce3` 则换了一个维度的武器。它做两件事：

1. **装载即归约**：每线程从全局内存读 **2** 个元素（`i` 与 `i + blockDim.x`）并先行相加——总线程数需求减半，块数减半，全局内存读完全合并且树高降 1；
2. **用 shuffle 取代共享内存树**：warp 内归约交给 `warpReduceSum`（`__shfl_down_sync`），每 warp 只留下 1 个部分和写入共享内存；随后**仅一次** `cg::sync`，再由 warp 0 对 `blockDim/32` 个部分和做第二次 shuffle 归约。块栅栏从 reduce2 的 9 次（装载 1 次 + 循环 8 次）降到 **1 次**。

这是 u4-l6 已建立的结论在此处的兑现：归约的瓶颈是线程间交换介质，代价阶梯为 寄存器+shuffle < 共享内存 < 全局内存。

#### 4.2.2 核心流程

reduce2（blockDim = 256）：

```text
s = 128: tid<128 活跃，sdata[0..127] += sdata[128..255]
s = 64:  tid<64，  sdata[0..63]  += sdata[64..127]
...
s = 1:   tid<1，   sdata[0]      += sdata[1]
（每步一次 cg::sync，共 8 次 + 装载后 1 次 = 9 次块栅栏）
```

reduce3 的两段式：

```text
段1 装载+首层归约:  mySum = g_idata[i] + g_idata[i + blockDim.x]   （每线程 2 元素）
段2 warp 内归约:    mySum = warpReduceSum(mySum)                   （5 步 shuffle，无栅栏）
段3 warp 间归约:    lane==0 写 sdata[wid]；cg::sync 一次；
                    warp 0 读回 blockDim/32 个部分和，再一次 warpReduceSum
```

#### 4.2.3 源码精读

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L141-L169](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L141-L169) —— reduce2：L159 `for (unsigned int s = blockDim.x / 2; s > 0; s >>= 1)` 步长折半；L160 `if (tid < s)` 活跃集为连续前缀；L161 两个操作数下标 `tid` 与 `tid + s` 均连续。文件头注释 L141-L143 明言 "sequential addressing -- no divergence or bank conflicts"。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L171-L176](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L171-L176) —— `warpReduceSum<T>`：偏移从 `warpSize/2 = 16` 逐倍减半到 1，共 5 步，把 32 个寄存器值归约到 lane 0；掩码 `(unsigned int)-1`（全 32 位为 1）表示全 warp 参与，掩码兼作 warp 内同步。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L182-L208](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L182-L208) —— reduce3 全文：L191 每块负责 `blockDim*2` 个元素（块基数变为 `blockIdx.x * (blockDim.x * 2)`）；L193-L195 首层归约在寄存器里完成（L195 的越界守卫使非 2 幂规模也安全）；L198-L201 warp 归约后由 lane 0 写 `sdata[wid]`；**L202 是全 kernel 唯一一次块栅栏**；L203 只有 warp 0 的前 `blockDim/32` 个线程装载部分和（其余补 0——0 不影响加法，所以 L204 对整个 warp 0 直接 shuffle 仍是正确的）；L207 写出。

一个值得咀嚼的细节：L203 中 `threadIdx.x < blockDim.x / warpSize` 对 256 线程就是 `threadIdx.x < 8`——只有 8 个线程真的载入了部分和，但 32 线程的 warp 0 全体参与 L204 的 shuffle，多出来的 24 个 lanes 握着 0。这是 shuffle 归约的常见套路：**对齐到 warp 边界，用零填充**。

#### 4.2.4 代码实践

**实践目标**：验证「栅栏次数」这一变量的收益，并确认 reduce3 的默认地位。

1. 运行三个版本，规模固定：

   ```bash
   ./reduction.out n=16777216 threads=256 kernel=2
   ./reduction.out n=16777216 threads=256 kernel=3
   nvprof ./reduction.out n=16777216 kernel=2
   nvprof ./reduction.out n=16777216 kernel=3
   ```

2. 记录程序打印的 `Throughput` / `Time`，并从 nvprof 的 GPU activities 表中摘出**单次 kernel 时间**。
3. **需要观察的现象**：打印的 `Time` 上 kernel 2 与 kernel 3 差距不大（因为它被级联、拷贝、启动开销稀释），而 nvprof 里的 kernel 行差距更明显。
4. **预期结果**：kernel 3 的纯 kernel 时间低于 kernel 2（块数减半 + 栅栏 9→1）。差距的具体倍数**待本地验证**；对照 `result.txt` 中 kernel 3 在 V100 上 2^24 规模约 0.00091 s（含级联的整条流水线均值，[result.txt:L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L13)）。
5. 顺手核对：分别给两个 kernel 传 `n=1000`（非 2 幂），`Test passed` 是否仍成立？（reduce3 的 L195 守卫在起作用；reduce0~2 依赖装载守卫补 0。）

#### 4.2.5 小练习与答案

**练习 1**：数一数 reduce2（256 线程）与 reduce3 各执行多少次块级栅栏。

**答案**：reduce2：装载后 1 次 + 循环 8 次 × 1 次 = **9 次**。reduce3：**1 次**（L202）；warp 内的同步由 `__shfl_down_sync` 自带，不需要块栅栏。

**练习 2**：reduce3 把「每线程 1 元素」改成「每线程 2 元素」，这是实现级还是算法级优化？

**答案**：**算法级**。它改变了工作量的分布形状（总线程数从 \( n \) 降到 \( n/2 \)，块数减半，树高降 1），而不是在固定形状下做指令层面的改良。与之对照，reduce1→reduce2（改寻址）是典型的实现级优化。这条分界线在 reduce6 会再次出现。

**练习 3**：为什么 reduce3 的 L203 只让前 8 个线程读共享内存，却不影响结果正确性？

**答案**：块内只有 `blockDim/32 = 8` 个 warp 部分和（`sdata[0..7]`），warp 0 的 lane 8~31 没有对应数据可读，于是被赋 0；后续 shuffle 归约是加法，加 0 不改变和。填充值恰好是加法单位元，这是该技巧成立的前提（若做 min 归约则应填充 +∞）。

### 4.3 阶梯第 5~7 级：reduce4、reduce5、reduce6——展开、最后 warp 特权与每线程多元素

#### 4.3.1 概念说明

reduce3 之后剩余的瓶颈是：块内还剩一段「共享内存树 + 每步栅栏」的循环。三级优化依次收割它：

- **reduce4（展开最后一级 warp）**：观察到一个不对称性——当 \( s \le 32 \) 时，参与归约的线程全部落在 **warp 0** 内，而 warp 内同步根本不需要块栅栏！于是共享内存循环只在 `s > 32` 时运行（256 线程时仅 2 步），最后 32 路归约交给一个 warp 用 shuffle 完成，栅栏从 9 次降到 3 次。这正是 reduce3 思想的彻底化：**能降到 warp 内的操作绝不用块级机制**。
- **reduce5（完全展开）**：reduce4 的循环边界与比较仍在运行时计算。reduce5 把块大小变成**编译期模板参数** `blockSize`，把整个归约树手工展开成平铺的三段 if（`tid<256`、`tid<128`、`tid<64`），每段的谓词在编译期即被求值或简化为单次比较。代价是主机端必须为每种块大小实例化一个版本——4.4 的巨型 switch 就是为此存在的。
- **reduce6（每线程多元素，Brent 定理）**：前六个版本都固定「总线程数 = n 或 n/2」——n 很大时块数暴涨（2^24 时 32768 块），启动、调度、每块一次的部分和写出的开销都在放大。reduce6 反其道而行：**把块数钉死在 `maxBlocks`（默认 64）**，每个线程用 grid-stride 循环串行累积多个元素，最后只做一次块内归约。工作复杂度仍是 \( O(n) \)，步复杂度仍是 \( O(\log \text{blockSize}) \)，但总线程数不再随 n 膨胀——这就是 Brent 定理的 \( n/p + \log_2 p \) 形状。模板参数 `nIsPow2` 在规模为 2 幂时把越界守卫整个编译掉。

#### 4.3.2 核心流程

reduce4/5/6 共享的「最后 warp 特权」收尾（blockSize = 256，块内归约到 64 个部分和后）：

```text
共享内存树只走到 s > 32：     s = 128, 64 两步 → sdata[0..63] 存 64 个部分和
warp 0 接管收尾（无块栅栏）：
  thread_rank < 32 的线程：   mySum += sdata[tid + 32]   // 取来 warp 1 的 32 个部分和
  tile32.shfl_down 五步：     64 个寄存器值归约到 rank 0
写出：                        g_odata[blockIdx.x] = mySum
```

reduce6 的装载（每线程串行累积，gridSize = blockSize·2·gridDim）：

```text
mySum = 0
while (i < n):
    mySum += g_idata[i]                      // 相邻两路，warp 集体仍合并访问
    if nIsPow2 或越界检查通过: mySum += g_idata[i + blockSize]
    i += gridSize
```

以默认配置（threads=256、maxBlocks=64）跑 n = 2^24：gridSize = 32768，每线程循环 512 圈、每圈 2 元素，即**每线程累积 1024 个元素**；全 GPU 只有 64 个块、16384 个线程。

#### 4.3.3 源码精读

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L224-L264](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L224-L264) —— reduce4：L243 `for (unsigned int s = blockDim.x / 2; s > 32; s >>= 1)` 共享内存树止步于 32；L251 `cg::thread_block_tile<32> tile32 = cg::tiled_partition<32>(cta)` 取得 warp 粒度的组；L253-L260 **只有 `thread_rank() < 32` 的 warp 0 执行收尾**——L255 `mySum += sdata[tid + 32]` 取回 warp 1 的部分和（注意这解释了 4.4 将提到的 `threads<=32` 时共享内存要加倍分配的伏笔），L257-L259 用 `tile32.shfl_down` 完成最后 6 步（64→1），全程无需块栅栏。文件头注释 L210-L223 还给出了共享内存下限：blockSize ≤ 32 时也要分配 64 个元素。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L278-L328](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L278-L328) —— reduce5：模板参数 `unsigned int blockSize`（L278）；L297-L313 三段平铺展开 `if ((blockSize >= 512) && (tid < 256)) ... (blockSize >= 256) && (tid < 128) ... (blockSize >= 128) && (tid < 64)`——当 blockSize 在编译期已知，`blockSize >= 512` 等条件直接蒸发，循环变量与比较指令全部消失；L315-L324 与 reduce4 相同的最后 warp shuffle 收尾。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L330-L402](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L330-L402) —— reduce6：模板参数多了 `bool nIsPow2`（L339）；L348-L349 基地址与跨距按 `blockSize * 2 * gridDim.x` 计算；L351-L364 **grid-stride while 循环**——每线程串行累积多个元素，L361 的守卫 `if (nIsPow2 || i + blockSize < n)` 在 2 幂规模时整条被编译器消除；L366-L397 落共享内存后走与 reduce5 完全相同的展开收尾。头注释 L330-L337 点名 Brent's Theorem。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L339-L340](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L339-L340) —— reduce6 签名：`template <class T, unsigned int blockSize, bool nIsPow2>`，三个模板参数对应「类型 × 块大小 × 是否 2 幂」的编译期特化空间。

#### 4.3.4 代码实践

**实践目标**：感受「每线程元素数」这一算法级杠杆的力量，并验证展开的收益。

1. 固定规模对比 4/5/6 三级：

   ```bash
   ./reduction.out n=16777216 threads=256 kernel=4
   ./reduction.out n=16777216 threads=256 kernel=5
   ./reduction.out n=16777216 threads=256 kernel=6
   nvprof ./reduction.out n=16777216 kernel=4   # 逐个版本记录 kernel 行
   ```

2. 扫描 reduce6 的块数杠杆（注意 `maxblocks` 只对 kernel 6 生效）：

   ```bash
   for mb in 32 64 128 256 1024; do
       ./reduction.out n=16777216 threads=256 kernel=6 maxblocks=$mb
   done
   ```

3. **需要观察的现象**：kernel 6 的输出行 `blocks` 恒等于 `min(maxblocks, 32768)`；随 maxblocks 增大，吞吐先升后降或趋平——块太少则每线程元素过多、并行度不足（占用不满 SM），块太多则部分和写出的中间开销回升。
4. **预期结果**：kernel 6 显著快于 kernel 4/5（差距来自算法级变量），kernel 5 略快于或接近 kernel 4。V100 上该方向的具体数值**待本地验证**；可参考 u4-l6 已建立的旁证——同款 shuffle 归约比 cuda_global 的全局内存归约吞吐高约 28%。
5. 若无 GPU，可做源码侧实践：统计三个 kernel 在 blockSize=256 下的块栅栏条数（数 `cg::sync` 的可达次数），并解释为什么 reduce5 的三条展开段后的栅栏是无条件的、即使该段被编译为空。

#### 4.3.5 小练习与答案

**练习 1**：reduce4 的共享内存循环为什么停在 `s > 32` 而不是 `s > 0`？

**答案**：当 \( s \le 32 \) 时，活跃线程 `tid < s ≤ 32` 全部属于 warp 0。warp 内的同步可由 `tile32.shfl_down`（底层即 `__shfl_down_sync`）完成，不需要也不应该再付出块栅栏。把树截在 32，剩下的一整段降级为 warp 内寄存器归约——「能不用块级同步就不用」。

**练习 2**：reduce5 用模板参数换展开，代价是什么？这个代价由谁承担？

**答案**：`blockSize` 成为编译期常量后，每种块大小都要单独实例化一份代码，主机端必须用 switch 把运行时的 `threads` 分发到对应实例（见 4.4 的 switch）。代价由**代码体积与主机端分发代码**承担，kernel 本身零开销。reduce6 又叠加 `nIsPow2`，使特化空间再翻倍。

**练习 3**：n = 2^24、threads=256、maxblocks=64 时，reduce6 每个线程处理多少个元素？reduce3 呢？

**答案**：reduce6：gridSize = 256×2×64 = 32768，每圈 2 个元素，共 2^24/32768 = 512 圈，即 **1024 个元素/线程**（全 GPU 仅 64 块）。reduce3：固定 **2 个元素/线程**（块数 32768）。二者相差 512 倍——这就是「并行形状」的差异。

### 4.4 reduce 模板入口与测量框架：分发、规模计算与多级归约级联

#### 4.4.1 概念说明

kernel 之外的 `.cu`/`.cpp` 代码同样值得精读，因为它决定了「7 个 kernel 如何被公平地比较」：

- **`reduce<T>` 分发包装**：kernel 0~3 是普通模板，直接按 `whichKernel` 分发；kernel 4~6 带编译期块大小参数，必须嵌套 switch 按 `threads` 选择实例；kernel 6 还要先按 `isPow2(size)` 分流。它还处理一个共享内存陷阱：块 ≤ 32 时也要分配 64 个元素的共享内存，因为 reduce4~6 的收尾会读 `sdata[tid + 32]`。
- **`getNumBlocksAndThreads`**：阶梯的「每线程元素数」差别直接体现在启动规模上——kernel < 3 每线程 1 元素，kernel ≥ 3 每线程 2 元素，kernel 6 再把块数钳制到 `maxBlocks`。
- **`benchmarkReduce` 的多级归约级联**：所有 kernel 每块只产出一个部分和，跨块合成由主机驱动——反复把部分和数组再喂给同一个 kernel，直到只剩 1 个数（或交给 CPU，`--cpufinal`）。**本讲主题中「最后一个块做最终归约」这一环，在这份 2019 版代码里就是由这条级联（或 CPU 路径）承担的**，而不是设备端的「最后一个块原子计数」技巧。计时器罩住整条流水线，100 轮取平均。
- **`shmoo`**：内置的多规模扫描模式，一次吐出 7 kernel × 规模 1~2^25 的 CSV 表——现成的「阶梯表发生器」。

#### 4.4.2 核心流程

一次 `./reduction.out n=16777216 kernel=3` 的完整执行：

```text
main → 选类型(int/float/double) → runTest<T>
  ├─ 解析命令行（默认 size=2^24, threads=256, whichKernel=3, maxBlocks=64）
  ├─ 生成随机数据（int: rand()&0xFF，数值小以避免截断误差）
  ├─ getNumBlocksAndThreads → (blocks=32768, threads=256)
  ├─ cudaMalloc + H2D 拷贝
  ├─ warmup：reduce 一次（不计入）
  ├─ benchmarkReduce × 100 轮：
  │     [timer 起] 全量 kernel（32768 块 → 32768 个部分和）
  │     级联：s=32768 → 64 块再归约 → s=64 → 1 块再归约 → s=1
  │           （每级含一次 D2D cudaMemcpy）
  │     [timer 止 cudaDeviceSynchronize]
  ├─ 平均 Time = 总和/100；Throughput = 4n 字节 / Time
  └─ 对照 CPU Kahan 求和 → Test passed/failed
```

#### 4.4.3 源码精读

- [Shuffle/cuda_shuffle/reduction_kernel.cu:L409-L658](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L409-L658) —— `reduce<T>` 分发包装：L415-L418 `smemSize = (threads <= 32) ? 2 * threads * sizeof(T) : threads * sizeof(T)`，注释言明是为避免块内仅 1 个 warp 时 `sdata[tid+32]` 越界；L421-L437 kernel 0~3 直接分发；L439-L546 kernel 4/5 按 `threads` 嵌套 switch（512/256/128/64/32/16/8/4/2/1 十档）；L548-L656 kernel 6 先 `isPow2(size)` 分流再十档 switch——`default` 落在 case 6 上，所以非法 whichKernel 也会跑 kernel 6。
- [Shuffle/cuda_shuffle/reduction_kernel.cu:L660-L668](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L660-L668) —— 显式实例化 `reduce<int/float/double>` 三份：模板定义在 .cu 中，必须显式实例化才能被 reduction.cpp 链接到（.cpp 侧只有 reduction.h 的声明）。
- [Shuffle/cuda_shuffle/reduction.cpp:L201-L235](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L201-L235) —— `getNumBlocksAndThreads`：L209-L211 kernel<3 时 `blocks = (n + threads - 1) / threads`（每线程 1 元素）；L212-L215 kernel≥3 时 `blocks = (n + threads*2 - 1) / (threads*2)`（每线程 2 元素）；L232-L234 `if (whichKernel == 6) blocks = MIN(maxBlocks, blocks)`——**reduce6 独有的块数钳制**；L217-L230 还防御了 grid 超限（块数减半、线程数加倍）。
- [Shuffle/cuda_shuffle/reduction.cpp:L241-L321](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L241-L321) —— `benchmarkReduce`：L254-L312 每轮先 `cudaDeviceSynchronize` 再起表（L257-L258）；L261 全量 kernel；L266-L276 `cpuFinalReduction` 分支（`--cpufinal` 时拷回块部分和在 CPU 上求和）；**L278-L295 GPU 级联**——`while (s > cpuFinalThreshold)` 反复「D2D 拷贝部分和 → 再调 reduce」（L286-L288），`s` 按 kernel 是否 ≥3 以不同步长收缩（L290-L294）；L310-L311 同步后停表。L314-L318：级联收敛到 s=1 时，最终值在计时循环之外统一拷回。
- [Shuffle/cuda_shuffle/reduction.cpp:L329-L415](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L329-L415) —— `shmoo`：L363-L366 对 7 个 kernel 各 warmup 一次；L374-L405 按规模从 1 翻倍到 33554432，逐 kernel 打印 CSV（逗号分隔），直接可用作阶梯表。
- [Shuffle/cuda_shuffle/reduction.cpp:L165-L178](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L165-L178) —— `reduceCPU` 用 **Kahan 补偿求和**做参照：树形/分块求和的浮点误差与朴素串行不同，Kahan 算法抑制舍入累积，是更可信的 oracle；int 类型则直接逐位精确（`result.txt` 中两行 GPU/CPU 结果连负数回绕都一致，L15-L16、L33-L34）。
- [Shuffle/cuda_shuffle/reduction.cpp:L422-L427](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L422-L427) —— 默认参数：`size = 1<<24`、`maxThreads = 256`、`whichKernel = 3`、`maxBlocks = 64`。**注意与文件头注释 L53 "default 6" 的矛盾——以代码为准。**

命令行完整用法在文件头注释：[reduction.cpp:L46-L62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L46-L62)（`--shmoo / --n / --threads / --kernel / --maxblocks / --cpufinal / --cputhresh / --type`）。samples 的参数解析器同时接受带 `--` 与裸前缀两种写法，[test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/test.sh#L1-L4) 用的是裸形式 `n=16777216`。

#### 4.4.4 代码实践

**实践目标**：弄清「程序打印的 Time 到底量了什么」，并复现 result.txt 的一次记录。

1. 裸运行一次（不传 kernel）：

   ```bash
   cd Shuffle/cuda_shuffle && make
   ./reduction.out n=16777216
   ```

2. 对照 [result.txt:L1-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L1-L18)：你的输出应包含 `256 threads (max)`、`32768 blocks`、`Throughput = ... GB/s, Time = ... s`、GPU/CPU 两行结果与 `Test passed`——从而确认这次运行走的是 **kernel 3**（代码默认值）。
3. 用 `--cpufinal` 关掉 GPU 级联再跑一次：

   ```bash
   ./reduction.out n=16777216 cpufinal
   ```

4. **需要观察的现象**：打开 cpufinal 后每轮计时内多了一次 D2H 拷贝与 CPU 求和、少了一串级联 kernel；打印 Time 会变化。
5. **预期结果**：两种模式的 GPU/CPU 结果一致；Time 差异反映「块部分和的最终合成放在哪一侧」的成本。具体数值**待本地验证**。
6. 最后跑一次内置扫描，直接得到 7 kernel × 多规模的 CSV 原始表（运行较久）：

   ```bash
   ./reduction.out shmoo
   ```

#### 4.4.5 小练习与答案

**练习 1**：为什么 `reduce<T>` 包装里 `threads <= 32` 时共享内存要分配 `2 * threads` 个元素？

**答案**：reduce4/5/6 的收尾段中 warp 0 会执行 `mySum += sdata[tid + 32]`（如 [reduction_kernel.cu:L255](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L255)）。当块只有 32 个线程时，`tid+32` 最远到 63，超出 `threads` 个元素的分配——所以至少要给 64 个元素（代码注释 L415-L417 原话："we need to allocate two warps worth of shared memory so that we don't index shared memory out of bounds"）。

**练习 2**：程序打印的 `Time` 包含哪些阶段？它与 nvprof 里单个 kernel 行的时间是什么关系？

**答案**：`Time` 是 100 轮平均的**整条流水线墙钟**：全量 kernel 启动 +（默认路径下的）多级归约级联（每级一次 D2D 拷贝 + 一次 kernel）+ 结尾同步；数据分配、H2D 装载、最终单值 D2H 与 Kahan 参照都在计时窗口之外。它 ≥ nvprof 中被调 kernel 行的时间之和；级联轮数越多（块越多），差距越大。这正是 u1-l4「wall time 与 kernel time 两种口径」的又一实例。

**练习 3**：`--maxblocks` 为什么只对 kernel 6 生效？

**答案**：kernel 0~5 的总线程数由 n 唯一确定（1 或 2 元素/线程），块数是推导量；只有 kernel 6 的 grid-stride 循环允许「线程数与 n 解耦」，块数成为自由参数，`getNumBlocksAndThreads` 中 `blocks = MIN(maxBlocks, blocks)`（[reduction.cpp:L232-L234](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp#L232-L234)）才只对它生效。给其他 kernel 传 `maxblocks` 会被静默忽略（可自行运行验证，**待本地验证**）。

## 5. 综合实践：亲手测出七级阶梯并建立优化检查清单

这是本讲的主实践，对应任务：**对同一 size 依次运行 7 个 kernel，整理成阶梯表，每一级写一句「瓶颈 → 改法 → 收益」，并与 `result.txt` 核对口径。**

### 步骤

1. **准备**（需 NVIDIA GPU 与 CUDA 工具链；无 GPU 则完成第 5 步的源码侧替代方案）：

   ```bash
   cd Shuffle/cuda_shuffle
   make                # 产物为 reduction.out
   ```

2. **逐级测量**（规模固定 2^24，块大小固定 256，避免多变量）：

   ```bash
   for k in 0 1 2 3 4 5 6; do
       echo "== kernel $k =="
       ./reduction.out n=16777216 threads=256 kernel=$k
   done
   ```

3. **profiler 采证**：至少对 kernel 0、1、2、3 各跑一次概览与指标，把「单次 kernel 时间」「branch_efficiency」记入表格：

   ```bash
   nvprof ./reduction.out n=16777216 kernel=0
   nvprof --metrics branch_efficiency ./reduction.out n=16777216 kernel=0
   ```

4. **填表**（示例骨架，数据列待你本地测得）：

   | 级 | 版本 | 瓶颈（本级要杀的） | 改法 | 级别 | 块数(2^24) | kernel 时间 | 相对 reduce0 |
   | -- | ---- | -------------------- | ---- | ---- | ---------- | ----------- | ------------ |
   | 1 | reduce0 | 模运算 + 交错发散（无 warp 可退休） | —（基线） | — | 65536 | 待测 | 1.0× |
   | 2 | reduce1 | 上：模/发散；新：bank 冲突(2→8 路) | 活跃线程改连续 `index=2*s*tid` | 实现 | 65536 | 待测 | 待测 |
   | 3 | reduce2 | 上：bank 冲突 | 折半步长顺序寻址 | 实现 | 65536 | 待测 | 待测 |
   | 4 | reduce3 | 上：9 次块栅栏 + 块数过多 | 装载即归约(2 元素/线程) + shuffle | 算法+实现 | 32768 | 待测 | 待测 |
   | 5 | reduce4 | 上：最后 6 步仍走共享内存+栅栏 | 树止于 32，最后 warp 用 tile shuffle 收尾 | 实现 | 32768 | 待测 | 待测 |
   | 6 | reduce5 | 上：运行时循环开销 | 模板 blockSize 完全展开 | 实现 | 32768 | 待测 | 待测 |
   | 7 | reduce6 | 上：总线程数随 n 膨胀、块数过多 | grid-stride 每线程多元素，块钳到 64 | **算法** | 64 | 待测 | 待测 |

   每级补一句收益归因，例如第 3 级：「bank 冲突 2~8 路归零，代价为零（只是换了下标方向）」。

5. **与 `result.txt` 核对口径**：[result.txt](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/result.txt#L1-L72) 的 4 次运行均未传 `kernel=`，故全是 kernel 3；其 Time（0.00083~0.00091 s）是含级联的流水线均值。把你测得的 kernel 3 一行与之并排——数值不会相等（机器不同），但量级与 `blocks` 数字（32768/65536/131072/262144）应逐个对上，这是「先核对 Calls/blocks 再对比时间」方法论（u6-l4 主题）的一次预演。

6. **沉淀检查清单**：把阶梯泛化成顺序化的排查表，写成你自己的备注（顺序有讲究——先定形状，再抠指令）：

   1. **算法级（并行形状）**：每线程处理几个元素？块数/wave 数是否合理？能否 grid-stride 与规模解耦？（reduce3、reduce6）
   2. **全局访存**：warp 集体地址是否连续（合并）？是否对齐？（u4-l2、u4-l4）
   3. **共享内存访存**：跨步访问是否引发 bank 冲突？能否改顺序寻址或换 shuffle？（reduce1→2→3）
   4. **分支**：发散是否 warp 对齐？能否消除模/除法？能否谓词化？（reduce0→1，u3-l1）
   5. **同步**：栅栏次数能否减少？能否把工作降级到 warp 内（shuffle/tile）？（reduce3、reduce4）
   6. **指令流**：循环能否用编译期常量展开？守卫能否被模板参数蒸发？（reduce5、reduce6 的 nIsPow2）
   7. **测量**：warmup、多轮平均、kernel 时间与 wall time 分口径、正确性对照（Kahan）。（benchmarkReduce）

**预期结果**：阶梯表整体呈单调改善、且第 7 级（kernel 6）改善幅度最大；kernel 0 与 1 可能相差不大（收益与 bank 冲突损失相互抵消）。各级具体倍数**待本地验证**——若你的实测出现非单调（例如 kernel 5 在小规模上不优于 kernel 4），那本身就是有价值的发现，请结合块数与占用率解释它。

## 6. 本讲小结

- 7 个 kernel 是一条**单变量演进的优化阶梯**：reduce0（模运算+交错发散）→ reduce1（连续活跃线程，引入 bank 冲突）→ reduce2（顺序寻址，零冲突）→ reduce3（装载即归约+warp shuffle，栅栏 9→1）→ reduce4（树止于 32，最后 warp 用 tile shuffle 收尾）→ reduce5（模板 blockSize 完全展开）→ reduce6（grid-stride 每线程多元素，块数钳制，Brent 定理）。
- **两类优化要分清**：实现级（寻址、展开、同步削减——reduce1/2/4/5）不改变工作量分布；算法级（改变每线程元素数与并行形状——reduce3 的 2 元素、reduce6 的多元素）才是量级最大的收益来源。
- **能降到 warp 内的操作绝不用块级机制**：shuffle 让数据留在寄存器、同步由指令自带，这是 reduce3~6 共同的灵魂（承接 u4-l6）。
- 分发与测量框架同样重要：`reduce<T>` 的嵌套 switch 是模板特化的必然代价；`smemSize` 对 `threads≤32` 加倍是为 `sdata[tid+32]` 收尾兜底；`benchmarkReduce` 的多级归约级联（或 `--cpufinal`）承担跨块合成，**打印的 Time 是含级联的流水线均值，不是单 kernel 时间**。
- 仓库自带 `result.txt` 全部是**默认 kernel 3** 的运行（代码默认 `whichKernel=3`，与头注释 "default 6" 矛盾，以代码为准），不能直接当七级对比数据用——阶梯表要自己测。
- 优化顺序应当是「先定形状、再抠访存、再削分支与同步、最后展开指令流」，并始终用 profiler 的 kernel 时间与 warmup+多轮平均的口径说话。

## 7. 下一步学习建议

- **u6-l2（设计你自己的微基准）**：把本讲的检查清单用起来——以 CoMem_AXPY 三件套骨架为模板，做一个只含单一变量的对照基准，复现「阶梯」式实验设计。
- **u6-l4（实验方法论与结果解读）**：本讲综合实践第 5 步已经触及「核对 blocks/Calls 再比时间」的跨平台方法，u6-l4 将系统化讨论 warmup、计时口径与跨平台（Carina/Fornax）结果的解读。
- **延伸阅读**：kernel 注释中给出的 NVIDIA 开发者博客文章（关于 Kepler 及以后架构上用 shuffle 做更快归约，[reduction_kernel.cu:L215-L216](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction_kernel.cu#L215-L216)）是这条阶梯的原始出处，值得通读。
- 若你在新架构 GPU 上实验，注意 `nvprof` 已被 `nsys`/`ncu` 取代（u1-l2、u1-l4 均有说明）：`ncu --set full` 能直接给出每个 kernel 的 bank 冲突、发散与占用率计数，把本讲第 5 节的表格填得更满。
