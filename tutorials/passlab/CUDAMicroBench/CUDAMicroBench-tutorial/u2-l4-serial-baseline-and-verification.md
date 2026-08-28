# 串行基线与正确性验证：check、checksum 与计时方法

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 `check()` 函数用「差值和 / 绝对值和」衡量的**归一化相对误差**，以及它背后"验证正确性"的设计意图。
2. 理解**串行基线（serial baseline）**在微基准中的角色：它是正确性的"标准答案"，而不是用来比速度的对手。
3. 掌握项目通用的计时三件套：`read_timer_ms()` 墙钟计时、`num_runs = 10` 多轮取平均、`warmingup` kernel 预热，并说清**计时区间到底量了什么**。
4. **定量解释**程序打印的 checksum 为什么是几十（CoMem_AXPY 实测 36.386）或约等于 2（MemAlign 实测 1.99838），而不是接近 0——这是本讲最重要的一课。
5. 知道 `REAL` 宏在 `.h` 与 `.c` 两处定义、切换 `float`/`double` 时必须同步修改，以及切换的精度与性能后果。

本讲是单元二的收尾：u2-l1 至 u2-l3 讲了"怎么算"，本讲讲"怎么证明算得对、怎么把时间量准"。性能优化有一条铁律——**先正确，再快速**；本讲就是这条铁律的代码实现。

## 2. 前置知识

### 2.1 浮点运算天生"不精确"

你需要两个直观认识（不需要数值分析基础）：

- **浮点数是离散的**。`float` 用 32 位、`double` 用 64 位表示实数，绝大多数实数只能被近似存储。两个浮点数相乘再相加，结果通常带 \(10^{-7}\)（float）或 \(10^{-16}\)（double）量级的相对误差。
- **浮点加法不满足结合律**。\((a+b)+c \neq a+(b+c)\) 在浮点世界里是常态。这意味着：即使数学上等价，**不同的计算顺序（串行 for 循环 vs GPU 上千个线程并行）也会得到略有差别的结果**。所以"GPU 和 CPU 结果不是逐位相等"不等于"GPU 算错了"——我们需要一个能容忍微小数值噪声、又能抓住真错误的判定标准，这就是 `check()` 存在的原因。

### 2.2 两类时间：墙钟时间与 kernel 时间

- **墙钟时间（wall time）**：主机程序从时刻 A 到时刻 B 实际经过的时间，包含 `cudaMalloc`、数据拷贝、kernel 启动、同步等一切开销。`read_timer_ms()` 量的就是它。
- **kernel 时间**：GPU 上单纯执行 kernel 的时间，需要 nvprof 之类的工具才能拆出来。

u1-l4 已经用 BankRedux 证明过：本项目的 `time` 输出是**十次完整调用的平均墙钟**，远大于纯 kernel 时间。本讲 4.3 节会用 CoMem_AXPY 的 Carina 实测数据把这笔账算清楚。

### 2.3 承接前几讲

- u2-l1：`axpy_cudakernel_warmingup` 与被测 kernel 函数体相同，用于消化首次启动的一次性开销。
- u2-l3：`axpy_cuda()` 包装函数的五段式（`cudaMalloc` → H2D → kernel 循环 → D2H → `cudaFree`）。
- u1-l3：三件套骨架（host `.c` + kernel `.cu` + 接口 `.h`），以及 `REAL` 宏双处定义的事实；本讲展开它的切换后果。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `CoMem_AXPY/axpy_cuda.c` | host 主程序：`read_timer_ms`、串行 `axpy`、`check`、`main` 的计时与打印，全部在本讲视野内 |
| `CoMem_AXPY/axpy_cudakernel.cu` | 设备侧：`warmingup` kernel 与 `axpy_cuda()` 中四次 kernel 启动的顺序（决定 checksum 的"叠加次数"） |
| `CoMem_AXPY/axpy.h` | 接口契约：`REAL` 的第一处定义、`extern "C"` 声明 |
| `MemAlign/axpy_cuda.c` | 同构对照版：串行从 `i=1` 起、串行跑 10 次——两处差异直接影响 checksum 数值 |
| `MemAlign/axpy_cudakernel.cu` | 三个 kernel 的边界守卫（`i>0`、偏移 +1、`i>1`），解释串行基线为何从 1 开始 |
| `CoMem_AXPY/axpy_cuda.output.carina.txt` | Carina 集群实测转录：checksum 与 time 的真实数值、nvprof 拆解 |
| `MemAlign/axpy_cuda.output.carina.txt` | MemAlign 的实测转录：checksum 恒为 1.99838 |

两个目录共用一套骨架，差异集中在"被测变量"上（CoMem 测合并访问、MemAlign 测对齐），这本身就是 u1-l3 讲过的控制变量方法论——本讲要利用这对"同构双胞胎"做对比阅读。

## 4. 核心概念与源码讲解

### 4.1 串行 axpy 基线：给 GPU 一份"标准答案"

#### 4.1.1 概念说明

**串行基线**是用最朴素、最不容易写错的方式实现的一份参考答案：单线程、按序 for 循环。它的价值不在速度，而在**可信**——如果我们对串行版的正确性有信心，就可以拿它当"尺子"去量 GPU 版的输出。

在 CoMem_AXPY 和 MemAlign 里，串行基线**只承担正确性参照**，完全不参与计时（它甚至没被计时过一次）。这与后面 Shmem 等基准里"串行/OpenMP vs CUDA 双方都计时比速度"的做法不同——那是对性能基线的用法。**正确性基线**与**性能基线**是两个不同的角色，本讲的两个目录只用了前者。

要让这把"尺子"有效，还有一个隐含前提：**两条路径必须从同一份输入、同一个初值出发**。代码里用固定种子随机初始化加 `memcpy` 复制来保证这一点。

#### 4.1.2 核心流程

`main` 里数据准备与基线生成的流水线（CoMem_AXPY 版）：

1. `srand48(1<<12)`：固定随机种子 4096，保证每次运行数据一致（同一台机器、同一个 libc 上可复现）。
2. `init(x, n)`：x 取到接下来 n 个 `drand48()` 值。
3. `init(y_cuda, n)`：y_cuda 取到再后面 n 个值（x 与 y_cuda 内容不同）。
4. `memcpy(y, y_cuda, ...)`：**参考数组 y 与 GPU 数组 y_cuda 强制同起点**——这一步是"公平比较"的关键。
5. 串行 `axpy(x, y, n, a)` 执行 1 次：`y = y₀ + a·x`。
6. `axpy_cuda(x, y_cuda, n, a)` 执行 10 次：每次内部还会启动 4 个 kernel（见 4.2）。
7. `check(y_cuda, y, n)` 对账。

#### 4.1.3 源码精读

随机初始化与同起点复制（注意 `(double)drand48()` 的显式转换）：

- [CoMem_AXPY/axpy_cuda.c:33-39](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L33-L39)：`init` 用 `drand48()` 填充 `[0,1)` 的随机浮点数，每元素独立、无规律，避免编译器或硬件走"特殊值捷径"。
- [CoMem_AXPY/axpy_cuda.c:77-80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L77-L80)：固定种子 + 初始化 x、y_cuda，再 `memcpy(y, y_cuda, ...)` 复制出参考数组——没有这行，两条路径起点不同，后面的比较毫无意义。

串行基线本体：

- [CoMem_AXPY/axpy_cuda.c:41-48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L41-L48)：朴素 for 循环 `y[i] += a * x[i]`，从 `i = 0` 覆盖到 `n-1`，一行不多、一行不少。
- [CoMem_AXPY/axpy_cuda.c:82](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L82)：串行版只在计时区**之前**调用 1 次，之后不再参与任何计时。

MemAlign 的同构版本有两处刻意差异：

- [MemAlign/axpy_cuda.c:44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L44)：循环从 `i = 1` 开始，跳过 0 号元素。原因在设备侧：MemAlign 的被测 kernel 都刻意避开 0 号元素——[MemAlign/axpy_cudakernel.cu:13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L13) 守卫 `i > 0 && i < n`，[MemAlign/axpy_cudakernel.cu:20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L20) 的错位 kernel 从全局编号 +1 起步，[MemAlign/axpy_cudakernel.cu:29](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L29) 的 warmup 守卫 `i > 1`。串行基线从 1 开始是为了与被测对象的**覆盖范围**对得上。
- [MemAlign/axpy_cuda.c:84](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L84)：串行版在计时区外循环跑了 `num_runs = 10` 次，而 CoMem_AXPY 只跑 1 次。这一处"调用次数不对称（CoMem 1:10、MemAlign 10:30）"正是 4.2 节 checksum 数值差异的直接来源。

#### 4.1.4 代码实践：数一数每条路径叠加了多少次 `a·x`

1. **实践目标**：亲手确认"y 与 y_cuda 各自被更新了多少次"，为 4.2 节的定量推导做准备。
2. **操作步骤**：
   - 在 [CoMem_AXPY/axpy_cudakernel.cu:52-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73) 的 `axpy_cuda()` 里数出每次调用启动了几个会对 `d_y` 做加法的 kernel（4 个：warmingup、1perThread、block、cyclic）。
   - 回到 [CoMem_AXPY/axpy_cuda.c:88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L88) 确认 `axpy_cuda` 共被调用 10 次。
   - 对 MemAlign 重复以上两步（3 个 kernel × 10 次，串行侧 10 次）。
3. **需要观察的现象**：纸面推导出两条路径的终值表达式：CoMem_AXPY 中 `y = y₀ + 1·a·x`，`y_cuda = y₀ + 40·a·x`。
4. **预期结果**：CoMem_AXPY 的差距是 `39·a·x`；MemAlign 中 `y = y₀ + 10·a·x`（覆盖 `[1,n)`），`y_cuda` 在 `[2,n)` 上多出 `20·a·x`、在 1 号元素上多出 `10·a·x`。这些数字将在 4.2 节直接预言实测 checksum。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `memcpy(y, y_cuda, ...)` 这一行删掉，会发生什么？

**答案**：y 和 y_cuda 会被 `init` 先后填充成**不同**的随机序列（`drand48()` 是顺序消费的流），两条路径起点不同，`check` 量出来的"误差"从第一步起就被污染，基线彻底失效。这行复制是公平比较的前提。

**练习 2**：为什么 MemAlign 的串行基线从 `i = 1` 开始，而 CoMem_AXPY 从 `i = 0` 开始？

**答案**：串行基线的覆盖范围要跟**被测 kernel 的有效范围**对齐。MemAlign 的三个 kernel 因为错位设计全部避开 0 号元素（守卫分别是 `i>0`、`+1` 偏移、`i>1`），串行版从 1 开始才能比出"多算了几次"而不是"覆盖范围不同"。CoMem_AXPY 的四个 kernel 都覆盖 `[0,n)`，所以从 0 开始。

**练习 3**：这两个基准里，串行基线被计时了吗？

**答案**：没有。CoMem_AXPY 调用 1 次、MemAlign 调用 10 次，全部位于计时区之外。它们是**正确性基线**（oracle），不是性能基线；这两个基准不比较 CPU 与 GPU 的速度。

### 4.2 check 函数：归一化相对误差与 checksum 的真实含义

#### 4.2.1 概念说明

`check(A, B, n)` 计算的是**相对 L1 误差**：把两个数组逐元素差值的绝对值加起来，再除以参考数组绝对值之和。它回答的问题是"平均而言，每个元素的偏差占元素本身的几分之几"：

\[
\mathrm{check}(A, B) \;=\; \frac{\displaystyle\sum_{i=0}^{n-1} \lvert A_i - B_i \rvert}{\displaystyle\sum_{i=0}^{n-1} \lvert B_i \rvert}
\]

其中 A 是待检验数组（`y_cuda`），B 是参考数组（`y`）。用比值而不是差值本身，是为了让判定标准**与数据规模 n 和数值大小无关**——n 翻倍，分子分母同比例增长，比值不变。

设计意图在源码里留有痕迹：`main` 中有一行**被注释掉的断言** `assert(checkresult < 1.0e-10)`。也就是说，作者原本打算"相对误差小于 \(10^{-10}\) 即通过"，后来放弃了硬性判定，只打印数值让人自己看。为什么放弃？读完下面的定量分析你就明白：在这两个基准的实验结构下，这个比值根本降不到 \(10^{-10}\)。

#### 4.2.2 核心流程

`check` 的执行过程：

1. 两个累加器 `diffsum`、`sum` 清零。
2. 遍历每个元素：`diffsum += |A[i] - B[i]|`，`sum += |B[i]|`。
3. 返回 `diffsum / sum`。
4. `main` 把返回值按 `%g` 格式打印成 `checksum`。

**关键推演：这个程序打印的 checksum 到底是什么？** 把 4.1 节数出来的叠加次数代进去。

CoMem_AXPY：`y = y₀ + 1·a·x`，`y_cuda = y₀ + 40·a·x`，于是每个元素差 `= 39·a·x[i]`。取 \(a = 123.456\)、\(x_i, y_{0,i} \sim U(0,1)\)（均值约 0.5）：

\[
\mathrm{check} \;\approx\; \frac{39 \cdot a \cdot \bar{x}}{\bar{y_0} + a \cdot \bar{x}} \;\approx\; \frac{39 \times 123.456 \times 0.5}{0.5 + 123.456 \times 0.5} \;\approx\; 38.7
\]

MemAlign：`y` 与 `y_cuda` 在 `[2,n)` 上相差 `20·a·x`，分母约为 `y₀ + 10·a·x`：

\[
\mathrm{check} \;\approx\; \frac{20 \cdot a \cdot \bar{x}}{\bar{y_0} + 10 \cdot a \cdot \bar{x}} \;\approx\; \frac{20 \times 123.456 \times 0.5}{0.5 + 10 \times 123.456 \times 0.5} \;\approx\; 2.0
\]

也就是说：**打印出来的 checksum 不是浮点误差，而是实验结构的产物**——多轮计时让 `y_cuda` 被反复叠加、`axpy_cuda()` 内部一次启动多个 kernel、串行侧与 CUDA 侧调用次数不对称。浮点噪声（\(10^{-16}\) 量级）淹没在这些结构性偏差里。

#### 4.2.3 源码精读

- [CoMem_AXPY/axpy_cuda.c:51-60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L51-L60)：`check` 的完整实现——分子是逐元素绝对差之和，分母是参考数组 `B` 的绝对值之和。注意 `n` 参数是 `int`，而串行 `axpy` 的 `n` 是 `long`，类型不完全一致（对本基准的规模无影响，但读代码时要留意）。
- [CoMem_AXPY/axpy_cuda.c:91-92](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L91-L92)：`check(y_cuda, y, n)` 的调用与输出格式 `axpy(n): checksum: %g, time: %0.2fms`——A 是 GPU 结果、B 是串行参考，checksum 与 time 同行打印。
- [CoMem_AXPY/axpy_cuda.c:93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L93)：被注释掉的 `//assert (checkresult < 1.0e-10);`，`check` 原本的设计意图是硬阈值判定，现在退化为"差值探针"。
- [CoMem_AXPY/axpy_cuda.output.carina.txt:9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L9)：Carina 实测 `checksum: 36.386`（n=1024000；n=4096000 时 38.289、n=10240000 时 38.6708，见同文件 33、57 行）——与上面 38.7 的理论预言吻合，小规模时的偏离来自随机数据均值的抽样波动。
- [MemAlign/axpy_cuda.output.carina.txt:10](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.output.carina.txt#L10)：MemAlign 实测 `checksum: 1.99838`，且在 n=4096000、10240000、20480000 下恒为 1.99838（33、56、79 行）——相对误差是归一化比值，所以**不随规模变化**，这本身就是 4.2.1"与规模无关"性质的实证。

再看 `REAL` 宏的双处定义——它是 `check` 精度语义的开关：

- [CoMem_AXPY/axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6)：`#define REAL double`——设备侧（`.cu` 只 include 这个头）看到的类型。
- [CoMem_AXPY/axpy_cuda.c:20-21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L20-L21)：host 侧再次定义 `REAL`，注释写明 "change this to do saxpy or daxpy"。当前 HEAD 两个目录都是 `double`；要切换精度，**两处必须同时改**。只改一处会导致 host 按 `float`（4 字节/元素）分配和搬运，而设备侧包装函数按 `double`（8 字节/元素）执行 `cudaMemcpy n*sizeof(double)`——越界读取、结果错乱甚至崩溃，而且由于 `extern "C"` 符号名不含类型信息，**链接阶段不会报任何错**（这是 u1-l3 讲过的 C/C++ 混编符号机制的"反面"：它掩盖类型不匹配）。

#### 4.2.4 代码实践：切换 REAL 精度并给 check 加上 max diff 输出

1. **实践目标**：亲手验证 (a) 结构性偏差主导着当前 checksum，切精度几乎不改变它；(b) `check` 扩展出最大绝对误差后，能提供逐元素视角。
2. **操作步骤**：
   - 复制一份 CoMem_AXPY 目录到临时位置（不动仓库源码）。
   - 把 [CoMem_AXPY/axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6) 与 [CoMem_AXPY/axpy_cuda.c:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21) 两处的 `REAL` 都从 `double` 改成 `float`，`make` 重新编译，`./axpy_cuda 1024000` 运行。
   - 再做一个反例：只改 `.h` 一处、不改 `.c`，重新编译运行，观察会发生什么。
   - 为 `check` 增加最大绝对误差输出（示例代码，非项目原有代码）：

     ```c
     /* 示例代码：扩展 check，同时返回最大绝对误差 */
     REAL check_verbose(REAL *A, REAL *B, int n, REAL *max_diff_out)
     {
         int i;
         REAL diffsum = 0.0, sum = 0.0, max_diff = 0.0;
         for (i = 0; i < n; i++) {
             REAL d = fabs(A[i] - B[i]);
             diffsum += d;
             sum += fabs(B[i]);
             if (d > max_diff) max_diff = d;
         }
         *max_diff_out = max_diff;
         return diffsum / sum;
     }
     ```

     在 `main` 中改用它：`REAL maxdiff; REAL checkresult = check_verbose(y_cuda, y, n, &maxdiff);`，并在 printf 里追加 `max diff: %g`。
3. **需要观察的现象**：
   - float 版的 checksum 与 double 版（Carina 基准约 36.4~38.7）量级相同、只在末几位数字变化；`time` 则明显下降（传输与计算的字符节数减半）。
   - max diff 约为 `39 · a · max|x[i]|`，即 4815 上下——比 checksum 更直观地暴露"最坏元素差了多少"。
   - 只改一处 `REAL` 的反例：程序行为异常（checksum 变成离谱的值、结果错乱，甚至段错误）。
4. **预期结果**：结构性偏差（39 次多余叠加）远大于 float 的 \(10^{-7}\) 级舍入噪声，所以切精度不动 checksum、只动时间；反例证明类型不一致是静默失败。以上现象**待本地验证**（需要 NVIDIA GPU 与 nvcc 环境）。

#### 4.2.5 小练习与答案

**练习 1**：`check` 的分母为什么用 \(\sum |B_i|\) 而不是 `n` 或 `1`？

**答案**：除以 \(\sum|B_i|\) 得到的是**相对**误差，与数据规模 n、数值整体大小都无关——MemAlign 的 checksum 在四个不同 n 下恒为 1.99838 就是证据。若除以 n，得到的是平均绝对差，数值大小会随 a 的大小漂移；若不归一化，n 每翻一倍结果就翻一倍，无法设定统一阈值。

**练习 2**：假设把 CoMem_AXPY 改造到"理想对账"状态——`axpy_cuda()` 内只启动 `1perThread` 一个 kernel（去掉 warmingup），串行与 CUDA 各调用 1 次——checksum 会是多少？

**答案**：AXPY 是逐元素独立计算，每元素做的都是同一个表达式 `y[i] + a*x[i]`，串行与 GPU 的计算顺序完全一致，double 下应得 0 或极小值；float 下同样是 0 或极小值（若一边被编译成融合乘加 FMA、另一边没有，会出现 1~2 ULP 的末位差异，量级约 \(10^{-7}\)）。具体数值**待本地验证**。这也说明：**AXPY 型逐元素基准的精度切换主要影响带宽与时间，而不是可见的数值差异**——真正对精度敏感的是 BankRedux、Shuffle 那类跨元素归约（加法顺序不同）。

**练习 3**：为什么作者把 `assert(checkresult < 1.0e-10)` 注释掉，而不是修好它？

**答案**：因为按现在的实验结构（计时循环反复调用 + 一次启动 4 个 kernel + 两侧调用次数不对称），checksum 的结构性取值就是几十或 2，断言必然失败。要让它通过，必须先做练习 2 那样的隔离改造。这提醒我们：**验证框架失效时，问题可能不在"算错了"，而在"实验设计与验证目标不匹配"**。

### 4.3 read_timer_ms：多轮平均与 warmup 的计时方法

#### 4.3.1 概念说明

`read_timer_ms()` 用 POSIX 的 `ftime` 读取当前墙钟时间，返回毫秒精度的浮点数。单独一次测量毫无意义——进程调度、缓存冷热、CUDA 上下文创建都会污染单次数据——所以项目通用的计时方法是三件套：

1. **多轮取平均**：跑 `num_runs = 10` 次取平均，抑制偶发抖动；
2. **warmup kernel**：每次调用内先跑一个 `warmingup` kernel，把 kernel 首次启动的模块加载等一次性开销挡在"正式测量"之前（虽然它也被计了时，见下面的分析）；
3. **计时区只包住被测函数**：串行基线、数据初始化、`check`、`printf` 都在区外。

但要清醒：这套方法挡得住一部分噪声，**挡不住最大的一笔**——首次 `cudaMalloc` 触发的 CUDA 上下文创建（百毫秒级），它发生在计时区内部第一轮，被 10 次平均稀释后仍然显著抬高均值。warmup 有两个层级：**kernel 级**（warmingup kernel 负责的模块加载/首次启动）与**上下文级**（首次 CUDA API 调用），前者管不了后者。

#### 4.3.2 核心流程

计时代码的骨架（伪代码）：

```
num_runs = 10
t0 = read_timer_ms()
for i in 0 .. num_runs-1:
    axpy_cuda(x, y_cuda, n, a)     # 内部: malloc→H2D→warmingup→kernel×3→D2H→free
elapsed = (read_timer_ms() - t0) / num_runs
```

被一次 `axpy_cuda()` 调用计入的时间可以分解为：

\[
\bar{t} \;=\; t_{\text{malloc}} + t_{\text{H2D}} + \sum_k t_{\text{kernel}_k} + t_{\text{sync}} + t_{\text{D2H}} + t_{\text{free}}
\]

用 Carina 上 n=1024000 的实测数据（[CoMem_AXPY/axpy_cuda.output.carina.txt:9](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L9) 打印 `time: 69.20ms`）逐项对账：

| 组成部分 | Carina 实测（10 次总计） | 折算每次调用 | 占比（对 69.20ms） |
| --- | --- | --- | --- |
| 4 个 kernel 合计 | 362.43+361.21+321.31+316.45 µs ≈ 1.36 ms | ≈ 136 µs | **约 0.2%** |
| cudaMemcpy（20 次 HtoD + 10 次 DtoH） | 69.98 ms | ≈ 7.0 ms | 约 10% |
| cudaMalloc（20 次） | 356.88 ms，其中**单次最大 351.66 ms** | 名义 17.8 ms | 约 50% |
| 其余（同步、释放、启动、主机开销） | 余项 | — | 余下部分 |

结论清晰可见：**"GPU 算"只占 0.2%，时间几乎全花在搬运与分配上**；而且首次 `cudaMalloc` 的 351.66 ms（上下文创建）集中砸在第一轮，摊进 10 次平均后仍贡献约 35 ms/次的偏置——如果丢弃首轮再平均，`time` 会大幅下降。这与 u1-l4 在 BankRedux 上看到的模式（56.80ms 中三个 kernel 合计仅约 9.55µs）完全一致，是整个项目的共性现象。

#### 4.3.3 源码精读

- [CoMem_AXPY/axpy_cuda.c:14-18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18)：`read_timer_ms` 的实现——`ftime` 返回秒 + 毫秒两个字段，拼成毫秒计的 double。分辨率 1 ms（`millitm` 字段），对本基准 ms 级的量程够用；要测 µs 级 kernel 得用 nvprof 或 CUDA Event（u1-l4 的方法）。
- [CoMem_AXPY/axpy_cuda.c:84-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L89)：计时三件套的现场——`num_runs = 10`、计时区只包住 `axpy_cuda` 循环、总耗时除以轮数得均值；串行基线（82 行）与 `check`（91 行）都在区外。
- [CoMem_AXPY/axpy_cudakernel.cu:61-62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L62)：`warmingup` kernel 在每次 `axpy_cuda()` 内**第一个**启动并同步——kernel 级预热的位置。注意它也在计时区内、且同样修改 `d_y`（这就是 4.2 节"40 次叠加"里的 1/4）。
- [MemAlign/axpy_cudakernel.cu:42-43](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L42-L43)：MemAlign 的对应位置，warmup 同样排在被测 kernel 之前。
- [CoMem_AXPY/axpy_cuda.output.carina.txt:19](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.output.carina.txt#L19)：nvprof API 表的 `cudaMalloc` 行——`Avg 17.844ms` 但 `Max 351.66ms`，平均值被首次调用严重拉高，这就是"上下文级 warmup 缺失"的直接证据。
- [CoMem_AXPY/axpy_cuda.c:69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L69)：顺带一提，`fprintf(stderr, "Usage: axpy <n>\n")` 无条件打印，这就是输出转录里每段之前都有一行 "Usage" 的原因（承接 u1-l4 提到的 stdout/stderr 错位现象）。

#### 4.3.4 代码实践：改变 num_runs，观察平均值的偏置

1. **实践目标**：体会"首轮上下文创建开销被摊进平均"的量级，理解为什么严谨的基准要丢弃前几轮。
2. **操作步骤**：
   - 在临时副本中把 [CoMem_AXPY/axpy_cuda.c:85](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L85) 的 `num_runs` 分别改为 1、2、10、50，各编译运行一次（同 n，如 1024000），记录 `time`。
   - 进一步：在计时循环之前**额外**先行调用一次 `axpy_cuda(x, y_cuda, n, a)`（不计入时间），再按 `num_runs = 10` 测量，与原版对比。
3. **需要观察的现象**：`num_runs = 1` 时 `time` 最大（首轮开销全额计入）；轮数增加，均值逐渐下降但收敛慢（首轮 352 ms 摊到 n 轮就是 352/n ms 的固定偏置）；先行调用一次后，均值显著下降。
4. **预期结果**：以 Carina 数据推算，`num_runs=1` 约数百 ms、`num_runs=10` 约 69 ms、先行 warmup 一次后应降到十几 ms 量级；具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`warmingup` kernel 能消除首次 `cudaMalloc` 的 351.66 ms 吗？

**答案**：不能。上下文创建发生在**首次 CUDA API 调用**（这里是计时区内第一轮的第一次 `cudaMalloc`），属于"上下文级"的一次性成本；`warmingup` kernel 只吸收 kernel 模块加载与首次启动这类"kernel 级"成本，而且它本身排在 `cudaMalloc`、`cudaMemcpy` 之后才执行，根本来不及。正确做法是在计时区外先做一次完整调用（见 4.3.4 实践的第二步）。

**练习 2**：`ftime` 的分辨率是多少？为什么这套代码不用它测单个 kernel 的时间？

**答案**：1 ms（`millitm` 字段）。单个 kernel 只有几十 µs（Carina 实测每个约 31~36 µs），低于分辨率一个数量级，`ftime` 根本量不出来；测 kernel 时间要用 nvprof（u1-l4）或 CUDA Event。

**练习 3**：打印的 `time: 69.20ms` 是在 nvprof 干预下测得的（test.sh 用 `nvprof ./axpy_cuda ...` 运行）。这会让你对这个数字的解读发生什么变化？

**答案**：nvprof 会给每个 CUDA API 调用插入拦截与记录开销，API 时间被系统性放大，因此 69.20 ms 高于裸运行的墙钟。跨数据比较时应使用**同一测量条件**下的相对结论（不同 n 之间、不同 kernel 之间），而不是把 nvprof 下的绝对值当作真实性能——这也是 u1-l4 强调的"跨机器/跨条件只能比相对结论"的一个具体来源。

## 5. 综合实践：让 checksum 说真话

本讲三个模块（串行基线、`check`、计时方法）共同服务一个目标：**可信的验证**。综合实践就把 CoMem_AXPY 改造成一个"验证框架真正生效"的基准，并用它做一次双精度对比实验。在临时目录中操作，不要改动仓库源码。

任务步骤：

1. **隔离被测对象**：修改 `axpy_cudakernel.cu` 的 `axpy_cuda()`，注释掉 `warmingup`、`block`、`cyclic` 三个 kernel 的启动（参考 [CoMem_AXPY/axpy_cudakernel.cu:61-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68) 的启动区），只保留 `1perThread`。
2. **对称化调用次数**：修改 `main`，让串行 `axpy` 与 `axpy_cuda` 的调用次数一致（都 1 次，或都 10 次）。
3. **加强 check**：按 4.2.4 的示例代码给 `check` 加上 max diff 输出。
4. **双精度对比**：在 `double` 与 `float` 两种 `REAL`（记得两处同步改：[axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6) 与 [axpy_cuda.c:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21)）下分别编译运行，记录 checksum、max diff、time 三个指标。
5. **写一份简短实验记录**，包含下表并回答两个问题：

| 配置 | checksum | max diff | time (ms) | 备注 |
| --- | --- | --- | --- | --- |
| 原版（4 kernel × 10 次，double） | 36~39（Carina 实测） | ~4815（推算） | 69.20（Carina 实测） | 结构性偏差主导 |
| 隔离 + 对称（double） | 待本地验证 | 待本地验证 | 待本地验证 | 预期 checksum ≈ 0 |
| 隔离 + 对称（float） | 待本地验证 | 待本地验证 | 待本地验证 | 预期 time 下降 |

问题 A：隔离 + 对称之后，double 与 float 的 checksum 是否如 4.2.5 练习 2 所预言接近 0？若出现非零值，量级是多少、来自哪里（FMA 差异？舍入顺序？）。问题 B：float 版 time 下降的幅度与"传输字节数减半"的预期是否吻合？若远超减半，还能从 `axpy_cuda` 的五段式里找到什么别的解释？

完成这个实践，你就把本讲的三个模块串成了一条完整链路：**基线提供标准答案 → check 提供量化判据 → 计时方法保证测的是你想测的东西**。

## 6. 本讲小结

- **串行基线是正确性的尺子，不是速度的对手**：CoMem_AXPY 与 MemAlign 的串行版都在计时区外运行（1 次与 10 次），只为给 `check` 提供参考数组；MemAlign 的串行从 `i=1` 起步是为了对齐被测 kernel 的覆盖范围。
- **`check` 是相对 L1 误差**：\(\sum|A_i - B_i| / \sum|B_i|\)，与规模无关（MemAlign 四个规模下恒为 1.99838）；原本配有 `assert < 1e-10` 的硬判定，已被注释掉。
- **打印出的 checksum 是实验结构的产物**：计时循环 10 次调用 × 每次内部多个 kernel 叠加 × 两侧调用次数不对称，给出可精确预言的取值（CoMem ≈ 38.7 实测 36.4~38.7；MemAlign ≈ 2.0 实测 1.99838），浮点噪声被彻底淹没。
- **计时三件套的口径**：`read_timer_ms`（1 ms 分辨率墙钟）+ `num_runs=10` 平均 + kernel 级 warmingup；但首次 `cudaMalloc` 的上下文创建（Carina 实测 351.66 ms）在计时区内第一轮，平均后仍抬高均值——kernel 级 warmup 管不了上下文级成本。
- **Carina 数据拆账**：69.20 ms 的均值里 4 个 kernel 合计只占约 0.2%，时间花在 `cudaMalloc` 与 `cudaMemcpy` 上；"GPU 算得快"与"程序跑得快"是两件事。
- **`REAL` 宏双处定义**（`axpy.h:6` 与 `axpy_cuda.c:21`）：切换 float/double 必须同步修改，只改一处会被 `extern "C"` 的符号机制静默掩盖，造成 host/device 类型不一致的越界访问。

## 7. 下一步学习建议

到这里，单元二（CUDA 基础与微基准骨架）全部完成：你已经能读懂一个微基准从数据准备、串行基线、kernel 启动到验证计时的完整链路。接下来两条路：

1. **主线**：进入单元三 u3-l1（WarpDivRedux：warp 分支发散与无分支改写），开始逐个攻克 README 列出的性能挑战；它会用到 u1-l4 的 nvprof `--metrics branch_efficiency` 知识，建议先回顾该讲。
2. **支线（若对本讲的"归约顺序影响精度"意犹未尽）**：预读 BankRedux（u4-l5 的主角）的 `sum_cudakernel.cu`，观察归约树如何改变加法顺序——那里 `check` 的误差才是真正的浮点非结合性噪声，与 AXPY 的"结构性 checksum"形成鲜明对照。

无论走哪条路，记住本讲建立的习惯：**看到一个性能数字，先问三件事——它量的是什么区间？平均了几轮？首轮开销摊进去了多少？**
