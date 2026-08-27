# 一维数据的多种并行化策略：每线程一个元素、块分布与循环分布

## 1. 本讲目标

上一讲（u2-l1）我们读懂了第一个 kernel：`axpy_cudakernel_1perThread`，它把串行 for 循环的每一次迭代直接变成一个线程，「每线程一个元素」。这是最朴素也最常用的并行化方式，但它不是唯一的方式。

本讲结束后，你应该能够：

1. 说清楚同一个 AXPY 循环的三种任务划分方式——`1perThread`、`block`（连续块分布）、`cyclic`（循环分布）——在索引计算上的本质区别。
2. 手推任意一个线程在任何一种分布下「负责哪些下标」。
3. 读懂并写出 grid-stride loop（网格跨步循环），理解它为什么对任意 `n` 都天然正确。
4. 亲手修复 `axpy_cudakernel_block` 中源码注释留下的 TODO：当 `n` 不能被总线程数整除时，这个 kernel 会**静默漏算一部分元素**——你将复现这个 bug、解释它、修好它，并用 `check` 函数验证。

## 2. 前置知识

本讲只需要上一讲（u2-l1）的概念，这里快速复习并补充两个新名词：

- **kernel 与线程层级**：`__global__` 函数在 GPU 上执行；线程按 grid → block → thread 两级组织。每个线程用内建变量回答「我是谁」：

  \( i = \text{blockDim.x} \times \text{blockIdx.x} + \text{threadIdx.x} \)

  这个 `i` 是全局线程编号，从 0 开始分段连续、无缝无重叠。
- **总线程数**：一个 grid 里所有线程的个数，记作

  \( T = \text{gridDim.x} \times \text{blockDim.x} \)

  即块数乘以每块线程数。本讲的两个新 kernel 都会用到 `T`。
- **任务划分（work distribution）**：把「要做 `n` 个元素的活」分给 `T` 个线程的规则。划分方式不同，每个线程拿到的下标集合、程序需要的线程数、以及内存访问模式都会不同——这正是本讲的主题。
- **向下取整的陷阱**：C 语言里两个整数相除 `n / T` 是**向下取整**，余数被直接丢弃。`7 / 2 == 3`，而不是 3.5。这个看似无害的规则，正是本讲要修复的 bug 的根源。

另外 recall 两点已建立的事实（分别在 u1-l3 和 u1-l2 讲过）：

- `axpy_cuda` 中四个 kernel（warmingup、1perThread、block、cyclic）**共享同一对 `d_x`/`d_y`，顺序叠加执行**，所以程序打印的 checksum 通常远大于 0，它只是差值探针，不是「 correctness = 0」的判据——本讲第 4.4 节与综合实践会专门处理这一点。
- 编译只需 `make`（Makefile 里是一行 `nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu`），运行需要真实 GPU。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | kernel 文件 + host 侧包装函数 | 第 24–50 行的两个新 kernel（block、cyclic）以及第 61–68 行的启动配置 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序（实验控制器） | 第 42–48 行串行基线、第 51–60 行 `check` 校验、第 62–99 行 `main` |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口头文件（`axpy_cuda` 的声明与 `REAL` 定义） | 本讲不改动它，但修复实验前要先确认 `REAL` 的定义 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 一行式 nvcc 编译 | 实践时重新编译用 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 四个规模的 nvprof 运行脚本 | 借用它的规模选择 |

## 4. 核心概念与源码讲解

### 4.1 任务划分总览：同一个循环的三种切法

#### 4.1.1 概念说明

串行版本的 AXPY 是一个一重 for 循环：

```c
for (i = 0; i < n; ++i)
    y[i] += a * x[i];
```

见 [CoMem_AXPY/axpy_cuda.c:42-48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L42-L48)：串行基线 `axpy`，每元素一次乘加。

并行化的第一个问题不是「怎么写 kernel」，而是「**怎么把 `n` 次迭代分给 `T` 个线程**」。经典答案有三种：

| 策略 | 直觉比喻 | 线程 `tid` 负责的下标 | 需要的线程数 |
| --- | --- | --- | --- |
| 1perThread（每线程一个元素） | 一人一个，各拿各的 | \( \{\,tid\,\} \) | \( \approx n \)（向上取整到 block 的倍数） |
| block（连续块分布） | 切香肠，每人承包一段 | \( [tid \cdot B,\ (tid+1)\cdot B) \)，\( B = n/T \) | \( T \)（固定，如 262144） |
| cyclic（循环分布） | 发扑克牌，轮流拿 | \( \{\,tid,\ tid+T,\ tid+2T,\ \dots\,\} \) | \( T \)（固定，如 262144） |

三者的共同前提：**每个元素的计算彼此独立**（AXPY 的第 `i` 个元素只读写 `x[i]`、`y[i]`），所以怎么分都不会引入数据竞争，划分是「自由的」——这正是 AXPY 被选作教学微基准的原因之一。今后遇到有依赖的计算（比如归约、矩阵乘）就没有这种自由度了。

#### 4.1.2 核心流程

先看 host 侧是怎么启动这三个 kernel 的（这一段是三种策略的「控制面板」）：

- `1perThread`：`<<<(n+255)/256, 256>>>`，块数随 `n` 增长；
- `block` 与 `cyclic`：`<<<1024, 256>>>`，**固定** 1024 个 block、每 block 256 个线程，即 \( T = 1024 \times 256 = 262144 \) 个线程，与 `n` 无关；
- 每个都有 `if (i < n)` 一类的保护和 `cudaDeviceSynchronize()` 逐个同步。

#### 4.1.3 源码精读

kernel 的启动配置集中在包装函数里，见 [CoMem_AXPY/axpy_cudakernel.cu:52-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73)：`axpy_cuda` 依次完成 cudaMalloc、两次 H2D 拷贝、四个 kernel 的启动与同步、D2H 拷贝和释放——这是 u1-l3 讲过的五段式包装。

其中启动四连发在 [CoMem_AXPY/axpy_cudakernel.cu:61-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68)：注意前两个 kernel 用 `(n+255)/256` 个块，后两个固定用 `1024` 个块——同一个 `n`、同一个数组，三种策略使用**数量级不同的线程数**（`n=1024000` 时约 102 万 vs 26 万），这就是任务划分差异的最直观体现。

对比 `1perThread` 的函数体（[CoMem_AXPY/axpy_cudakernel.cu:16-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22)，上一讲已精读）：它每个线程只做一次 `y[i] += a*x[i]`，没有循环。本讲的两个新 kernel 则各自带一个 for 循环——**每线程一个元素**变成了**每线程一段元素**。

#### 4.1.4 代码实践（热身：算一算线程账）

1. **实践目标**：建立「策略 → 线程数 → 每线程工作量」的量化直觉。
2. **操作步骤**：对 `n = 1024000`，手算三行表格：每种策略的 grid 配置、总线程数 `T`、每个线程平均处理几个元素。答案：1perThread 是 4000 块 × 256 = 1024000 线程、人均 1 个；block/cyclic 是 1024 块 × 256 = 262144 线程、人均 \( 1024000 / 262144 \approx 3.9 \) 个。
3. **需要观察的现象**：注意 3.9 不是整数——block 分布必须想办法处理这个「除不尽」，这正是第 4.4 节的伏笔。
4. **预期结果**：能不看源码写出三种策略下线程 0 处理的下标集合（1perThread：{0}；block：[0,3)；cyclic：{0, 262144, 524288, 786432}）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `1perThread` 的块数是 `(n+255)/256` 而不是 `n/256`？
**答案**：这是整数向上取整技巧（u2-l1 讲过）\( \lceil n/256 \rceil = \lfloor (n+255)/256 \rfloor \)。若用 `n/256` 向下取整，总线程数可能小于 `n`，尾部元素无人处理；取整到 256 的倍数只会多出若干「多余线程」，由 kernel 内 `if (i < n)` 挡住，安全无害。注意这个「宁可多启线程再裁掉」的思路，与 block 分布「宁可少做（漏元素）」的向下取整形成了鲜明对比。

**练习 2**：如果 `n = 100`（远小于 262144），`1perThread` 会启动多少个块？block 和 cyclic 呢？
**答案**：1perThread 启动 `(100+255)/256 = 1` 个块、256 个线程，其中 156 个线程被 `if (i < n)` 挡住；block 和 cyclic 仍然启动 1024 个块（262144 个线程），绝大部分线程闲着——block 分布在这种情况下甚至**什么都不做**（第 4.4 节练习 3 会让你解释为什么）。

---

### 4.2 `axpy_cudakernel_block`：每线程承包一段连续区间

#### 4.2.1 概念说明

**块分布（block distribution）**把数组想象成一根香肠，切成 `T` 段等长的连续区间，第 `tid` 根线程承包第 `tid` 段：

\[ B = \lfloor n / T \rfloor, \quad \text{thread } tid \text{ 处理 } [\,tid \cdot B,\ (tid+1)\cdot B\,) \]

它是 MPI 集合通信教程里最经典的「连续划分」：每线程的工作在数组上是**连续**的，好处是索引计算一次成型（`start` 到 `stop` 一个 for 循环走完），而且如果后续要做相邻元素的通信（比如有限差分模板），连续划分的邻居交换边界最小。代价是：它假设 `n` 能被 `T` 整除，否则余数部分无人认领——这个假设就写在源码注释的 TODO 里。

#### 4.2.2 核心流程

```
输入: n, 线程编号 tid ∈ [0, T), T = gridDim.x * blockDim.x

1. B      ← n / T            (整数除法, 向下取整!)
2. start  ← tid * B
3. stop   ← start + B
4. for i in [start, stop):
5.     if i < n:  y[i] += a * x[i]
```

关键在第 1 步：\( n / T \) 丢掉余数 \( r = n \bmod T \)，于是全体线程实际只覆盖 \( [0,\ T \cdot B) \)，尾部 \( [T \cdot B,\ n) \) 共 \( r \) 个元素被**静默跳过**——不报错、不崩溃，只是结果悄悄错了。

#### 4.2.3 源码精读

完整 kernel 见 [CoMem_AXPY/axpy_cudakernel.cu:24-38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L24-L38)：注释 `/* block distribution of loop iteration */` 标明这是连续块分布版本。

逐行拆开看：

- [CoMem_AXPY/axpy_cudakernel.cu:27-28](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L27-L28)：先算全局线程编号 `thread_num` 和总线程数 `total_threads`——与 1perThread 的 `i` 公式相同，只是这里 `i` 不再直接当下标用，而是当「线程工牌」用。
- [CoMem_AXPY/axpy_cudakernel.cu:30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L30)：`int block_size = n / total_threads; //dividable, TODO handle non-dividiable later`——**本讲的靶心**。注释直白承认：假设可整除，不可整除的情形「以后再处理」。仓库作者把修复留给了读者，这正是我们第 4.4 节的实践任务。
- [CoMem_AXPY/axpy_cudakernel.cu:32-37](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L32-L37)：算出自己的连续区间 `[start_index, stop_index)` 并循环处理。注意循环里的 `if (i < n)`——在「向下取整」的前提下 `stop_index ≤ T·B ≤ n` **恒成立**，所以这个判断目前是永远为真的死代码；修好 bug 之后它才会真正派上用场（第 4.4 节会看到这一反转）。

**访问模式提示**（为 u4-l2 埋伏笔，这里只给结论）：同一时刻，同一 warp 里 32 个相邻线程各自站在自己区间的同一相对位置上，访问的地址彼此相隔 `B` 个元素——从 warp 的视角看地址是**分散**的；但每个线程自己前后两次迭代访问的地址是**连续**的。cyclic 分布则相反。这个差异会直接影响内存事务的合并性，本讲先记住「划分方式决定访问模式」即可。

#### 4.2.4 代码实践（复现漏算 bug 的数字账）

1. **实践目标**：用具体数字算出 block 分布漏掉多少元素，确认 bug 的存在与规模。
2. **操作步骤**（纸笔即可，无需 GPU）：
   - 取 `n = 1024000`（仓库默认 `VEC_LEN`），`T = 262144`：\( B = \lfloor 1024000/262144 \rfloor = 3 \)，全体线程覆盖 \( [0, 786432) \)，漏掉 \( 1024000 - 786432 = 237568 \) 个元素，即下标 \( [786432,\ 1024000) \)。
   - 再取任务规格里的 `n = 1000000`：同样 \( B = 3 \)，漏掉 \( 1000000 - 786432 = 213568 \) 个元素。
   - 找一个能整除的规模：`n = 1048576 = 262144 × 4`，此时 \( B = 4 \) 无遗漏。
3. **需要观察的现象**：默认参数下**每次运行都在漏算约 23% 的元素**，但程序照常打印、不报任何错误。
4. **预期结果**：得到结论「block kernel 在仓库默认的 `n=1024000` 下就是错的」。至于它在最终 checksum 里如何现形，需要第 4.4 节的隔离验证方法（因为四个 kernel 叠加执行，直接看程序输出分不清是谁的锅）。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`n = 1000000`、`T = 262144` 时，线程 100000 处理哪些下标？最后一个线程（编号 262143）呢？
**答案**：\( B = 3 \)。线程 100000 处 `[300000, 300003)`；线程 262143 处 `[786429, 786432)`。注意最后一个线程的 stop 恰好等于 `T·B = 786432`，此后到 `1000000` 之间的 213568 个元素不属于任何线程。

**练习 2**：循环体里的 `if (i < n)` 为什么在当前（向下取整）版本里是死代码？
**答案**：任意线程的 `stop_index = (tid+1)·B ≤ T·B = T·⌊n/T⌋ ≤ n`，所以循环变量 `i` 永远小于 `n`，判断恒真。它是作者为「将来处理非整除」预留的保护，当前版本形同虚设——这也暗示了作者心目中的修复方向。

---

### 4.3 `axpy_cudakernel_cyclic`：跨步循环与 grid-stride loop

#### 4.3.1 概念说明

**循环分布（cyclic distribution）**像发扑克牌：第 0 张给线程 0、第 1 张给线程 1……发完一轮再从线程 0 开始。线程 `tid` 负责下标

\[ \{\, tid,\ tid + T,\ tid + 2T,\ \dots \,\} \cap [0, n) \]

它的 for 循环从 `tid` 出发、步长为 `T`，正好穿过整个网格再折返——这种写法在 CUDA 世界有个通用名字：**grid-stride loop（网格跨步循环）**。它是 CUDA 官方博客推广的标准模式，核心动机有两个：

1. **对任意 `n` 天然正确**：循环条件 `i < n` 就是全部的边界保护，不需要整除假设，也不需要预先算段长——对比 block 分布,这是压倒性的正确性优势。
2. **线程数与 `n` 解耦**：无论 `n` 是一百万还是一亿，grid 都是 1024×256；`n` 增大时只是每个线程多转几圈循环。`1perThread` 做不到这一点（块数随 `n` 线性增长，`n` 很大时要么触及网格规模上限，要么让海量线程每人只做 2 次运算，启动开销摊不平）。

#### 4.3.2 核心流程

```
输入: n, 线程编号 tid ∈ [0, T)

for (i = tid; i < n; i += T):
    y[i] += a * x[i]          # 无需额外判断, 循环条件已是全部边界
```

每个线程的处理个数是 \( \lceil (n - tid) / T \rceil \)：`n=1024000, T=262144` 时，前 237568 个线程处理 4 个元素、其余处理 3 个——负载在 3 与 4 之间**交错分布**，不均衡但差距至多 1 个元素。

访问模式上，第 `k` 轮迭代中同一 warp 的 32 个线程访问 \( \{kT + \text{lane}\} \)（lane 为线程在 warp 内的编号）——**32 个连续地址**，与 `1perThread` 的合并性相同；这是 cyclic 相对 block 分布的一个性能优点（u4-l2 展开）。

#### 4.3.3 源码精读

完整 kernel 见 [CoMem_AXPY/axpy_cudakernel.cu:40-50](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L40-L50)：注释 `/* cyclic distribution of loop distribution */`。

- [CoMem_AXPY/axpy_cudakernel.cu:43-44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L43-L44)：与 block kernel 完全相同的两行——先拿到线程工牌与总线程数。两个 kernel 的差异全部体现在循环的写法上，输入输出、签名一模一样，这是微基准「控制变量」风格的又一次体现。
- [CoMem_AXPY/axpy_cudakernel.cu:47-49](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L47-L49)：`for (i = thread_num; i < n; i += total_threads)` 一行完成了任务划分（起点 `thread_num`）、边界保护（`i < n`）与步进（`+= total_threads`）。循环体内的 `if (i < n)` 是**冗余的**：能进入循环体就说明 `i < n` 成立，这个判断永远为真。它与 block kernel 里那个「暂时无用」的判断遥相呼应——一个是没有保护到真正的边界，一个是保护了不存在的边界。

#### 4.3.4 代码实践（玩具规模手推）

1. **实践目标**：在不碰 GPU 的情况下，完全掌握 cyclic 的索引规则。
2. **操作步骤**：取玩具规模 `n = 10, T = 4`，在纸上写出 4 个线程各自处理的下标序列，再按轮次（round）重排：第 0 轮各线程处理什么？第 1 轮呢？
3. **需要观察的现象**：按线程看是 `{0,4,8} {1,5,9} {2,6} {3,7}`；按轮次看是第 0 轮 `{0,1,2,3}`、第 1 轮 `{4,5,6,7}`、第 2 轮 `{8,9}`（后两个线程缺席）——**轮次视角下相邻线程拿相邻下标**，这正是它合并访问的来源。
4. **预期结果**：两种视角都写对，并能把 `n=1024000, T=262144` 的情形类推为「3 到 4 轮、最后一轮约 23.8 万个线程出席」。

#### 4.3.5 小练习与答案

**练习 1**：`n = 1024000, T = 262144` 时，线程 5 处理哪些下标？线程 262140 呢？
**答案**：线程 5 处理 `{5, 262149, 524293, 786437}`（4 个，因为 5 < 237568）；线程 262140 处理 `{262140, 524284, 786428}`（3 个）。

**练习 2**：把 cyclic kernel 循环体内的 `if (i < n)` 删掉安全吗？为什么作者会写出这个冗余判断？
**答案**：安全——循环条件 `i < n` 已经保证进入体的 `i` 合法。可能的解释：与同文件其他 kernel 保持形式一致（`1perThread` 里这个判断是**必需**的，因为它没有循环条件兜底），或是防御式编程习惯。对比之下 block kernel 的同名判断「写了但没用对地方」、cyclic 的「写了但没必要」、`1perThread` 的「写了且必需」——三处同一个 if，三种命运，很适合作为「边界检查放哪里」的教学素材。

**练习 3**：同一个 `n`，把 cyclic 的 grid 从 `<<<1024,256>>>` 改成 `<<<2048,256>>>`（T 翻倍），哪些东西会变、哪些不变？
**答案**：变的是总线程数、每线程的循环轮数（约减半）与每轮的步长；不变的是**计算结果**（每个元素恰好被处理一次，划分方式对独立元素的计算是透明的），数组每个元素也仍然只被一个线程碰一次。这也是 grid-stride loop 的「可伸缩」性质：网格大小是性能旋钮，不是正确性参数。（注意：block kernel 改 grid 就没有这种安全性——`T` 变了，`B = n/T` 与余数 `n mod T` 都跟着变，漏算数量随之改变。）

---

### 4.4 整除假设与边界修复：让 block 分布对任意 n 正确

#### 4.4.1 概念说明

现在正式处理源码里的 TODO。bug 的本质：\( B = \lfloor n/T \rfloor \) 丢弃了余数 \( r = n \bmod T \)，尾部 \( r \) 个元素无主。修复思路有两类：

- **方案 A（向上取整 + 边界判断）**：取 \( B' = \lceil n/T \rceil = \lfloor (n + T - 1)/T \rfloor \)，区间照旧 \( [\,tid \cdot B',\ (tid+1)\cdot B'\,) \)。总覆盖达到 \( T \cdot B' \ge n \)，不再漏元素；代价是最后一个线程的区间可能**越过** `n`，此时循环里原有的 `if (i < n)` 从死代码变成真正的边界保护。改动只有一行，且激活了作者预留的判断——与代码原有意图严丝合缝。
- **方案 B（余数前摊）**：取 \( B = \lfloor n/T \rfloor \)、\( r = n \bmod T \)，让前 `r` 个线程多处理一个元素：`tid < r` 的线程区间长 `B+1`、其余长 `B`。好处是负载完全均衡（每线程 `B` 或 `B+1` 个，与 cyclic 的均衡度相同），代价是 `start` 的计算要分情况，代码稍长。

两个方案处理后的正确性判据相同：**每个下标恰好被一个线程处理一次**——不重不漏。

#### 4.4.2 核心流程

方案 A 的验证逻辑（也是实践的核对清单）：

```
修复后对任意 tid:
  区间 [tid*B', (tid+1)*B') 与其他线程的区间不相交        (不重)
  所有区间之并 ⊇ [0, n)                                    (不漏)
  循环内 if (i < n) 裁掉越界部分                            (安全)
```

验证的度量手段是 host 侧的 `check` 函数：它对两个数组逐元素求差值绝对值之和，再除以参考解绝对值之和（[CoMem_AXPY/axpy_cuda.c:51-60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L51-L60)）。若 kernel 与串行基线对每个元素做**完全相同的单次乘加**，浮点结果逐位一致，checksum 应为 0（或由 FMA 收缩差异导致的、接近机器精度的小量，量级 \(10^{-17}\) 左右——u1-l4 讲过浮点噪声，主程序第 93 行注释掉的 assert 用的阈值是 `1e-10`）；漏算 21 万个元素的坏版本则会产生**个数量级**的 checksum（`a = 123.456`，每个漏掉的元素差约 `a·x[i]`）。

#### 4.4.3 源码精读（修复补丁 + 验证环境）

被修复的代码是 [CoMem_AXPY/axpy_cudakernel.cu:26-38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L26-L38)。下面是**示例代码**（修复方案 A 的 diff 形式，替换第 30 行的 `block_size` 计算，其余行不动）：

```c
// 示例代码: 方案 A, 仅改一行
int block_size = (n + total_threads - 1) / total_threads; // 向上取整, 任何 n 都覆盖到
```

验证环境的准备依赖对 `axpy_cuda` 调用序列的理解，见 [CoMem_AXPY/axpy_cudakernel.cu:61-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68)：四个 kernel 对同一 `d_y` 顺序叠加。**直接跑完整程序无法判断 block kernel 单独的正确性**——即便它完全不算，另外三个 kernel 也会把 `d_y` 加满，checksum 依然是「错的大数」。要隔离验证，需临时注释掉第 61–64 行（warmingup 与 1perThread）和第 67–68 行（cyclic），只保留第 65–66 行的 block 启动与同步；验证完再恢复。这是做实验的临时改动，不动仓库逻辑（在自己的工作副本上操作）。

计时口径的参照物在 [CoMem_AXPY/axpy_cuda.c:84-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L89)：`num_runs = 10` 次取平均的 host 墙钟（u1-l3、u2-l4 已讲）。

#### 4.4.4 代码实践（本讲主实践：修复 TODO 并验证）

1. **实践目标**：让 `axpy_cudakernel_block` 在 `n = 1000000`、总线程数 262144（不可整除）时仍然正确，并用 `check` 验证。
2. **操作步骤**：
   - **第一步（复现）**：在自己副本上按第 4.4.3 节隔离 block kernel（注释另外三个的启动行），`make` 重新编译，分别运行 `./axpy_cuda 1048576`（整除）与 `./axpy_cuda 1000000`（不整除），记下两个 checksum。预期：前者接近 0，后者是个大数（漏了 213568 个元素）。
   - **第二步（修复）**：把 [axpy_cudakernel.cu:30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L30) 改为方案 A 的向上取整（或实现方案 B），重新 `make`。
   - **第三步（验证）**：再次运行 `./axpy_cuda 1000000` 与 `./axpy_cuda 1048576`，两个规模的 checksum 都应降到 0 附近（≤ `1e-10` 量级即可视为通过，与被注释的 assert 同阈值）。
   - **第四步（回归）**：恢复被注释的四个 kernel 启动，确认程序回到原始行为。
3. **需要观察的现象**：修复前，`n=1000000` 的 checksum 比 `n=1048576` 大若干个数量级；修复后两者持平于 0 附近。若想再看到 bug 变体，试 `./axpy_cuda 100`（此时 `B=0`，所有线程的循环体一次都不执行，block kernel 整体空转）。
4. **预期结果**：手算与实测对上——修复前 `n=1000000` 漏 213568 个元素、checksum 显著非零；修复后 checksum 通过 `1e-10` 判据。**具体数值待本地验证**（需要真实 GPU 环境；无 GPU 时至少完成纸笔推导与代码修改，编译可用 `make` 验证语法）。

#### 4.4.5 小练习与答案

**练习 1**：方案 A（向上取整）修复后，线程 262143（`n=1000000`）的区间是多少？它实际处理几个元素？
**答案**：\( B' = \lceil 1000000/262144 \rceil = 4 \)，区间 `[1048572, 1048576)`，但 `i ≥ 1000000` 的部分被 `if (i < n)` 裁掉，实际只处理 `{1048572, 1048573, 1048574, 1048575}`——恰好在 `n` 处截住，处理 4 个元素。

**练习 2**：方案 A 有一个理论边界情况：当 `tid·B' ≥ n` 时该线程一个元素都不处理。这种情况会发生吗？发生在哪些线程上？
**答案**：会。`n=100` 时 `B' = 1`，所有 `tid ≥ 100` 的线程区间起点已越过 `n`，循环条件直接不成立，空转。这正是「向上取整 + 边界判断」的代价：少量线程颗粒无收。方案 B 中前 `r` 个线程拿 `B+1` 个、其余拿 `B` 个，没有空转线程（`n ≥ T` 时）；两种方案的负载差至多 1 个元素，对性能影响可忽略。

**练习 3**：为什么说「cyclic kernel 天然不需要这一节的所有修复」？
**答案**：cyclic 的任务划分由循环条件 `i < n` 与步进 `i += T` 直接定义，不经过任何除法与区间算术，因此对任意 `n`（包括 `n < T`、`n` 与 `T` 互质等一切情形）都满足不重不漏。grid-stride loop 用「循环代替切分」换来的这种鲁棒性，是它成为 CUDA 标准范式的重要原因。

---

## 5. 综合实践

把本讲全部内容串成一个完整的小实验（建议在拥有 NVIDIA GPU 的机器上进行，环境准备见 u1-l2）：

**任务：三种并行化策略的正确性与性能对照。**

1. **准备**：进入 `CoMem_AXPY`，`make` 编译。先跑 `./axpy_cuda 1024000` 确认基线输出格式（`checksum` 与 `time`，含义见 u1-l4）。
2. **正确性隔离**：按第 4.4.3 节分别只保留 block、只保留 cyclic 启动，各跑 `n = 1000000 / 1048576` 两个规模。预期：cyclic 两种规模 checksum 均 ≈ 0；block 仅在整除规模 ≈ 0。
3. **修复**：用方案 A 修复 block kernel，重复第 2 步，全绿。
4. **性能对照**：恢复全部四个 kernel，仿照 [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) 用 `nvprof ./axpy_cuda 1024000` 与 `nvprof ./axpy_cuda 10240000` 各跑一次，从 GPU activities 表里抄下 `1perThread`、`block`、`cyclic` 三个 kernel 的时间（参照 u1-l4 讲的 nvprof 概览读法）。
5. **分析**：回答两个问题——(a) 三种划分在这个访存受限的 kernel 上时间差多大？结合 u2-l1 的结论（每线程 2 次运算搬 24 字节）解释；(b) `n` 放大 10 倍后差距如何变化？block 分布「同一轮 warp 内地址相隔 B」与 cyclic「同一轮 warp 内地址连续」的差异有没有显形？
6. **产出**：一张三策略 × 两规模的 kernel 时间表 + 一段「正确性（修复前后 checksum）」记录。若无 GPU，完成 2、3 的代码修改与纸笔推导，性能部分引用仓库归档数据思路（u1-l4 的 carina/fornax 文件）说明如何替代。全部数值**待本地验证**。

## 6. 本讲小结

- 同一个 AXPY 循环有三种经典划分：`1perThread`（线程数 ≈ `n`）、`block`（连续段，\( B = n/T \)）、`cyclic`（跨步，步长 `T`）；划分方式同时决定线程数量、负载均衡与内存访问模式。
- `axpy_cudakernel_block` 用向下取整的 `n / total_threads` 切段，**余数元素被静默漏算**——仓库默认 `n=1024000` 就漏 237568 个（约 23%）；源码注释中的 TODO 承认了这一点。
- 一行修复（方案 A）：`block_size = (n + total_threads - 1) / total_threads` 向上取整，让原本恒真的 `if (i < n)` 变成真正的边界保护；方案 B（前 `r` 个线程多摊一个）则换取消除了空转线程的完全均衡。
- `axpy_cudakernel_cyclic` 就是 grid-stride loop：起点 `tid`、步长 `T`、条件 `i < n`，对任意 `n` 天然不重不漏，且线程数与 `n` 解耦、同轮 warp 内地址连续——正确性与可伸缩性双优，是 CUDA 的标准范式。
- 验证单一 kernel 的正确性必须**隔离运行**：四个 kernel 共享 `d_y` 顺序叠加，完整程序的 checksum 无法归因；隔离后与串行基线逐位对比，checksum 应为 0（或 FMA 带来的机器精度小量）。
- 源码里三处 `if (i < n)` 命运各异——`1perThread` 必需、block 起初是死代码（修复后激活）、cyclic 冗余——「边界检查放在哪里」本身就是划分策略的镜子。

## 7. 下一步学习建议

- **下一讲 u2-l3（CUDA 显存管理基础）**将深入 `axpy_cuda` 的另外几段：cudaMalloc/cudaMemcpy/cudaFree 的生命周期与 `cudaDeviceSynchronize` 的同步语义——本讲我们反复进出这个包装函数，但只盯着 kernel 启动行看。
- **u2-l4（串行基线与正确性验证）**系统展开 `check`、checksum 与计时方法，与本讲第 4.4 节的验证手段互为补充。
- 若你想提前看到「访问模式」的完整故事，可跳到 **u4-l2（CoMem：全局内存合并访问）**——本讲 4.2/4.3 两处伏笔（block 分散 vs cyclic 连续）在那里会用 nvprof 的内存事务指标验证。
- 源码阅读建议：把 `axpy_cudakernel.cu` 的四个 kernel 并排打开，只看「索引如何产生」这一件事，自己写出第四种划分（比如每个线程两段：前半一段 + 后半一段），再用本讲的隔离验证法检验它——能独立完成这一步，说明任务划分真的过关了。
