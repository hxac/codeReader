# 第一个 CUDA kernel：AXPY 与线程索引模型

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 kernel、grid、block、thread 四个概念之间的层级关系，以及 `__global__` 修饰的函数意味着什么。
2. 对任意 `blockIdx.x`、`threadIdx.x`，手推出 `blockDim.x * blockIdx.x + threadIdx.x` 得到的全局线程编号，并判断这个线程负责处理向量的第几个元素。
3. 解释 `<<<(n+255)/256, 256>>>` 这行启动配置里两个参数各自的含义，以及为什么网格大小要用 `(n+255)/256` 这种"加 255 再整除"的写法。
4. 理解 kernel 里 `if (i < n)` 边界检查为什么不能省略。

本讲是单元二的第一讲。前置讲义 u1-l3 已经解剖过微基准的三件套骨架（host 主程序 `.c` + kernel 文件 `.cu` + 头文件 `.h`），本讲把镜头推进到骨架的最核心处：**kernel 本身，以及它被启动时那行 `<<<...>>>` 配置**。

## 2. 前置知识

### 2.1 AXPY 是什么

AXPY 是 BLAS（Basic Linear Algebra Subprograms，基础线性代数程序集）里最基础的一个操作，名字是 "**A** times **X** **P**lus **Y**" 的缩写，即：

\[ y[i] \leftarrow a \times x[i] + y[i], \quad i = 0, 1, \dots, n-1 \]

其中 \( a \) 是标量，\( x \)、\( y \) 是长度为 \( n \) 的向量。按精度不同，它有 `saxpy`（单精度 float）和 `daxpy`（双精度 double）两个变种——这就是源码中 `REAL` 宏存在的意义（见 4.1.3）。

AXPY 最大的特点是：**每个元素的计算彼此完全独立**，第 \( i \) 个元素的结果不依赖第 \( j \) 个元素。这种"各干各的"结构正是 GPU 最喜欢的 workload：可以让 \( n \) 个线程同时各算一个元素，而不是让 CPU 一个循环从 0 算到 \( n-1 \)。

### 2.2 host 与 device：回顾

承接 u1-l3 的结论：这个项目的每个微基准都分成两半——

- **host 侧**（`.c` 文件）：运行在 CPU 上，负责准备数据、计时、校验，是"实验控制器"；
- **device 侧**（`.cu` 文件里的 `__global__` 函数）：运行在 GPU 上，负责真正的并行计算。

`.cu` 文件其实是个"混血儿"：它里面既有跑在 GPU 上的 kernel，也有跑在 CPU 上的普通 C 函数（比如负责分配显存、启动 kernel 的包装函数 `axpy_cuda`）。

### 2.3 思维转换：从"循环"到"我是谁"

写 CPU 串行代码时，我们的思维是"**一个工人按顺序干完所有活**"。写 CUDA kernel 时，思维必须换成："**几百万个工人同时被叫来，每人干一小份活，每个工人首先要搞清楚'我是谁、我该干哪一份'**"。

本讲的主角——线程索引计算——就是 GPU 上每个线程回答"我是谁"的方式。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | kernel 文件：4 个 `__global__` kernel + host 包装函数 `axpy_cuda` | warmingup 与 1perThread 两个 kernel 的函数体；`<<<...>>>` 启动配置 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序：初始化、串行基线、计时循环、check 校验 | 串行版 `axpy` 作为对照；`REAL`/`VEC_LEN` 宏 |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口头文件：`REAL` 宏与 `axpy_cuda` 的 `extern "C"` 声明 | `.c` 与 `.cu` 共享的类型契约 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 一行式编译规则 | 实践时重新编译用 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 四种规模的 nvprof 实验脚本 | 实践时的数据规模选择 |

说明：`axpy_cudakernel.cu` 里还有 `axpy_cudakernel_block` 和 `axpy_cudakernel_cyclic` 两个 kernel，它们代表另外两种并行化策略，留给下一讲 u2-l2，本讲只在 4.4.3 顺带看一眼它们的启动配置。

## 4. 核心概念与源码讲解

### 4.1 AXPY 的并行性：从串行循环到"每线程一个元素"

#### 4.1.1 概念说明

并行化的第一步是找到工作里**互不依赖的部分**。对 AXPY 来说，\( n \) 个元素的计算互相独立，所以最朴素的并行方案是：

> 启动 \( n \) 个线程，第 \( i \) 个线程只执行一次 `y[i] += a * x[i]`。

这就是本讲主角 `axpy_cudakernel_1perThread` 名字的由来——**1 per thread，每线程（恰好）一个元素**。它没有任何循环：串行版的循环被"展开"成了线程的维度，原本由循环变量 \( i \) 索引的工作，现在由线程编号索引。

#### 4.1.2 核心流程

串行版与 1perThread 版的对照：

```text
串行（CPU，1 个工人）              并行（GPU，n 个工人）
for i = 0 .. n-1:                  线程 0:  y[0]  += a * x[0]
    y[i] += a * x[i]               线程 1:  y[1]  += a * x[1]
                                   线程 2:  y[2]  += a * x[2]
                                   ...
                                   线程 n-1: y[n-1] += a * x[n-1]
```

- 串行版执行 \( n \) 次"取指令-计算-写回"，时间复杂度 \( O(n) \) 的每一步都由同一个核心顺序完成。
- 并行版每个线程只执行**一次**乘加，理论上 \( n \) 个线程可以在硬件允许的范围内同时进行。

#### 4.1.3 源码精读

先看串行参考实现，它跑在 CPU 上，用作正确性基线：

[CoMem_AXPY/axpy_cuda.c:42-48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L42-L48) —— 串行版 `axpy`：一个标准的 C 循环，对每个元素执行 `y[i] += a * x[i]`，这就是 GPU 版要对照的"标准答案"。

```c
void axpy(REAL* x, REAL* y, long n, REAL a) {
  int i;
  for (i = 0; i < n; ++i)
  {
    y[i] += a * x[i];
  }
}
```

再看 GPU 版 kernel（完整精读放到 4.2 和 4.3，这里先看形状）：

[CoMem_AXPY/axpy_cudakernel.cu:16-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22) —— `axpy_cudakernel_1perThread`：函数体只有两行——算索引、做一次乘加。注意它**没有循环、没有返回值**，`__global__` 修饰表明它是跑在 GPU 上、由 CPU 端调用的 kernel。

```cuda
__global__ 
void
axpy_cudakernel_1perThread(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a*x[i];
}
```

两段代码的对应关系：

| 串行版 | GPU 版 |
| --- | --- |
| `for (i = 0; i < n; ++i)` | `int i = blockDim.x * blockIdx.x + threadIdx.x;`（循环变量变成线程编号） |
| 循环体执行 \( n \) 次 | 函数体在每个线程里执行 1 次，共 \( n \) 份 |
| `i` 永远在 `[0, n)` 内 | `i` 可能超出 `n`，需要 `if (i < n)` 保护 |

还有一个容易被忽略的细节：两处 `REAL` 的定义。

[CoMem_AXPY/axpy.h:6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6) —— 头文件里 `#define REAL double`，被 `.cu` 侧 include，决定了 kernel 的操作精度。

[CoMem_AXPY/axpy_cuda.c:20-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L20-L22) —— host 主程序里**又**定义了一次 `#define REAL double`，并定义默认规模 `VEC_LEN 1024000`；注释明确写着"change this to do saxpy or daxpy"。`REAL` 在两处重复定义（u1-l3 提过的隐患），改动精度时两处都要同步。

#### 4.1.4 代码实践

**实践目标**：确认自己对"循环 → 线程"映射的理解，并核对数据规模。

**操作步骤**：

1. 打开 [CoMem_AXPY/axpy_cuda.c:62-72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L62-L72)，找到 `main` 里 `n` 的来源：默认取 `VEC_LEN`（= 1024000），若命令行给了参数则覆盖。
2. 打开 [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh)，记下实验用的 4 个规模：1024000、4096000、10240000、20480000。
3. 用纸笔或计算器验证：这 4 个数除以 256 是否都整除？（例如 \( 1024000 = 4000 \times 256 \)，\( 10240000 = 40000 \times 256 \)。）

**需要观察的现象**：test.sh 选的 4 个规模全都是 256 的整数倍。

**预期结果**：全部整除。这意味着在这些实验规模下，线程总数恰好等于 \( n \)，`if (i < n)` 永远为真——边界检查从未真正"挡住"过线程（但依然必须有它，原因见 4.3）。一旦你换一个任意的 \( n \)（比如 1000000），检查就会开始发挥作用。

#### 4.1.5 小练习与答案

**练习 1**：AXPY 每个线程要做几次浮点运算、搬运多少字节（按 `REAL = double` 算）？

**答案**：2 次浮点运算（1 乘 1 加）；搬运 24 字节——读 `x[i]` 8 字节、读 `y[i]` 8 字节、写 `y[i]` 8 字节。算术强度约 \( 2 / 24 \approx 0.083 \) flop/字节，非常低，是典型的**访存受限（memory-bound）** kernel，这个结论在 4.4.4 的实践中会用到。

**练习 2**：如果把 `a` 换成 `a + 1` 再传给 kernel，串行版和 GPU 版的结果还一致吗？

**答案**：一致。串行版和 GPU 版用同一个 `a`（[axpy_cuda.c:66](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L66) 中 `REAL a = 123.456;`，同一份值分别传给两条路径），两边执行的是同样的乘加，元素间又无依赖，所以逐元素完全相同（浮点顺序差异在本讲的单元素映射中不存在，因为每个元素只算一次）。

### 4.2 最小 kernel 解剖：`__global__` 函数与 warmingup

#### 4.2.1 概念说明

`__global__` 是 CUDA 对 C/C++ 的扩展关键字（由 nvcc 编译器识别），它声明一个 **kernel**：一个"在设备（GPU）上执行、从主机（CPU）端调用"的函数。它有几个硬性特点：

1. **返回值必须是 `void`**——想传回结果只能通过指针参数写显存（这里的 `y`）。
2. **调用它的不是普通函数调用语法**，而是带三尖括号的 `kernel<<<grid, block>>>(参数...)`（见 4.4）。
3. **一次调用会产生成千上万份并发执行**：GPU 上的每个线程都运行这个函数的一份副本，所有副本共享同一份参数。

`axpy_cudakernel.cu` 里有 4 个 kernel，本讲看前两个。第一个是 `warmingup`——它的函数体和被测的 `1perThread` **一字不差**。为什么要写两遍？承接 u1-l3 讲过的骨架约定：首次启动 kernel 有一次性的初始化开销（建立 GPU 执行上下文、加载代码等），先跑一个"热身"kernel 把这些开销消化掉，正式的计时循环才开始测被测 kernel。这两个函数体相同但**名字不同**，所以是两个独立的 kernel，`nvprof` 里会显示成两条独立的 GPU activity 记录（u1-l4 的 BankRedux 输出里就看到过 `warmingup` 单独列一行）。

#### 4.2.2 核心流程

一次 `axpy_cuda(x, y, n, a)` 调用中，与 kernel 执行相关的流程：

```text
host (CPU)                                device (GPU)
──────────                                ────────────
cudaMalloc 分配 d_x, d_y
cudaMemcpy 把 x, y 拷入显存
启动 axpy_cudakernel_warmingup   ──────▶  每个线程跑一遍函数体
cudaDeviceSynchronize（等 GPU 干完）
启动 axpy_cudakernel_1perThread  ──────▶  每个线程跑一遍函数体
cudaDeviceSynchronize
（block、cyclic 两个 kernel，下一讲）
cudaMemcpy 把 d_y 拷回内存 y
```

要点：kernel 启动是**异步**的——CPU 把"启动命令"提交给 GPU 后可以继续往下跑，所以每次启动后都要跟一句 `cudaDeviceSynchronize()`（让 CPU 在这里等 GPU 全部做完），否则后面的计时和拷贝会乱套。这个同步语义在 u2-l3 会展开。

#### 4.2.3 源码精读

[CoMem_AXPY/axpy_cudakernel.cu:8-14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L8-L14) —— `axpy_cudakernel_warmingup`：热身 kernel，函数体与被测 kernel 完全相同，作用是消化首次启动的一次性开销，让后面的计时更稳定。

```cuda
__global__ 
void
axpy_cudakernel_warmingup(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a*x[i];
}
```

逐行拆解这个函数体：

| 代码 | 含义 |
| --- | --- |
| `__global__` | 声明这是 kernel：设备上执行、主机端调用 |
| `REAL* x, REAL* y` | 参数是显存地址（由 `cudaMalloc` 得来的 `d_x`、`d_y`），kernel 内直接读写显存 |
| `int i = blockDim.x * blockIdx.x + threadIdx.x;` | 计算全局线程编号——"我是谁"（4.3 专门讲） |
| `if (i < n) y[i] += a*x[i];` | 边界检查 + 每线程恰好处理一个元素 |

[CoMem_AXPY/axpy_cudakernel.cu:16-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22) —— `axpy_cudakernel_1perThread`：被测 kernel，函数体与 warmingup 完全一致。两者是名字不同的两个独立 kernel，`nvprof` 会分别列出。

```cuda
__global__ 
void
axpy_cudakernel_1perThread(REAL* x, REAL* y, int n, REAL a)
{
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) y[i] += a*x[i];
}
```

注意一个容易困惑的点：warmingup 和 1perThread 都会**真的修改 `y`**（`y[i] += ...` 不是摆设）。所以 [axpy_cuda.c:88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L88) 的计时循环里，每轮 `axpy_cuda` 都对同一个 `y_cuda` 累加了 4 次（warmingup、1perThread、block、cyclic 各一次），跑 10 轮就累加了 40 次。这就是 u1-l3 说过的"checksum 远大于 0"的直接原因——`check` 比较的是"GPU 反复叠加后的 y"与"串行只加一次的 y"，只能当差值探针用，不能当严格的正确性断言。

#### 4.2.4 代码实践

**实践目标**：用 `nvprof` 确认"两个函数体相同的 kernel 在 GPU 侧是两条独立记录"。

**操作步骤**：

1. 进入 `CoMem_AXPY` 目录，执行 `make` 编译（命令见 [CoMem_AXPY/Makefile:1-2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2)，即 `nvcc -o axpy_cuda axpy_cuda.c axpy_cudakernel.cu`，u1-l2 已走通流程）。
2. 运行 `nvprof ./axpy_cuda 1024000`。
3. 在输出的 GPU activities 表里找 kernel 名字。

**需要观察的现象**：GPU activities 一节会列出多个 kernel，`axpy_cudakernel_warmingup` 和 `axpy_cudakernel_1perThread` 各占一行；结合计时循环（10 轮）与 4 个 kernel，每个 kernel 的 Calls 列应为 10。

**预期结果**：4 个 kernel 各被调用 10 次，共 40 次启动；warmingup 与 1perThread 时间接近（函数体相同）。Calls 列的数字还可以反过来印证 [axpy_cuda.c:85-88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L85-L88) 中 `num_runs = 10` 的结构（u1-l4 讲过这种"用 Calls 反推程序结构"的技巧）。若本机无 GPU，此步待本地验证；也可对照 [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) 在有 GPU 的机器上执行。

#### 4.2.5 小练习与答案

**练习 1**：`__global__` 函数能不能写成 `REAL axpy_kernel(...)` 返回一个值？

**答案**：不能。CUDA 规定 kernel 返回值必须是 `void`。因为一次启动会产生成千上万份并发执行，每份都有一个返回值，硬件没法把它们汇集回一个调用点；想传出结果只能通过指针参数写显存（像本例的 `y`），再由 host 侧 `cudaMemcpy` 拷回。

**练习 2**：既然 warmingup 和 1perThread 函数体完全一样，为什么不直接连着启动两次 1perThread 来热身？

**答案**：可以那样做，效果类似（第二次启动时首次开销已消化）。分开命名的好处是**可观测性**：`nvprof` 按 kernel 名字分别统计，实验者能一眼区分"热身那一次"和"被测的那十次"，避免把热身开销混进被测数据。这也符合微基准"控制变量、单独计量"的惯例。

### 4.3 线程索引计算：`blockDim.x * blockIdx.x + threadIdx.x`

#### 4.3.1 概念说明

GPU 不会凭空让 \( n \) 个线程各自"知道"自己该处理哪个元素。CUDA 把一次 kernel 启动产生的所有线程组织成两级层级：

```text
Grid（网格）＝ 本次启动的全部线程
 ├─ Block 0（块）          ← blockIdx.x = 0
 │   ├─ Thread 0           ← threadIdx.x = 0
 │   ├─ Thread 1           ← threadIdx.x = 1
 │   └─ …共 blockDim.x 个线程
 ├─ Block 1                ← blockIdx.x = 1
 │   └─ …
 └─ …共 gridDim.x 个块
```

每个线程体内可以读到 4 个**内建变量**（不需要声明、直接可用）：

| 变量 | 含义 | 本例中的值 |
| --- | --- | --- |
| `threadIdx.x` | 我在**我的块**里的编号 | \( 0 \sim 255 \) |
| `blockDim.x` | 每个块有多少**线程**（启动配置的第二个参数） | 256 |
| `blockIdx.x` | 我的块在整个**网格**里的编号 | \( 0 \sim (\text{blocks}-1) \) |
| `gridDim.x` | 网格里有多少个**块**（启动配置的第一个参数） | \( (n+255)/256 \) |

于是"我是全局第几号线程"就是一个简单的进制换算——把块编号当成"高位"、块内编号当成"低位"：

\[ i \;=\; \underbrace{b \cdot g}_{\text{前面所有块攒下的线程}} + \underbrace{t}_{\text{我在本块内的偏移} }，\quad b = \text{blockIdx.x},\; g = \text{blockDim.x},\; t = \text{threadIdx.x} \]

这和"第 3 排（每排 30 人）的第 7 个座位是全教室第 97 个座位"是同一种算法：\( 3 \times 30 + 7 = 97 \)。

顺带认识一个重要单位：GPU 实际以 **warp**（32 个连续线程）为调度单位执行，`blockDim.x = 256` 恰好是 8 个 warp。warp 的行为（包括分支发散）是单元三 u3-l1 的主题，这里只需记住"块大小最好是 32 的倍数"这条经验法则。

#### 4.3.2 核心流程

手推一个 \( n = 130 \)、`blockDim.x = 128` 的小例子（规模故意取得小，便于人工验算）：

网格大小 \( = (130 + 127) / 128 = 257 / 128 = 2 \)（C 整数除法；验证一下确实需要 2 块：\( 2 \times 128 = 256 \geq 130 \)，\( 1 \times 128 = 128 < 130 \)）。逐块列出每个线程的行为：

| 块 `blockIdx.x` | `threadIdx.x` 范围 | 全局编号 \( i = 128 \cdot \text{blockIdx.x} + \text{threadIdx.x} \) | 干活的线程 | 被 `if (i < n)` 挡住的线程 |
| --- | --- | --- | --- | --- |
| 0 | 0 … 127 | 0 … 127 | 全部（\( i < 130 \)） | 无 |
| 1 | 0 … 127 | 128 … 255 | 只有 \( i = 128, 129 \) 两个 | \( i = 130 \dots 255 \)，共 126 个 |

可以看到：

- 全局编号随块编号**分段连续**，段与段之间无缝隙、无重叠——这正是这个公式的关键性质，保证每个元素恰好被一个线程处理（不多不少）。
- 线程总数 \( = \text{gridDim.x} \times \text{blockDim.x} = 2 \times 128 = 256 \)，多于 \( n = 130 \)，多出来的 126 个线程靠 `if (i < n)` 空转返回。

#### 4.3.3 源码精读

[CoMem_AXPY/axpy_cudakernel.cu:20](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L20) —— 1perThread 的索引行：用"块号 × 块大小 + 块内号"把两级层级压平成一维全局编号，是 CUDA 一维 kernel 最经典的一行代码。

```cuda
int i = blockDim.x * blockIdx.x + threadIdx.x;
```

[CoMem_AXPY/axpy_cudakernel.cu:21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L21) —— 边界检查：因为网格大小是向上取整出来的，线程总数可能超过 \( n \)，越界线程如果不加检查就会读写 `y[i]`、`x[i]` 中不存在的元素，可能造成非法显存访问。

```cuda
if (i < n) y[i] += a*x[i];
```

这条 `if` 为什么不能省？再算一笔账：\( n = 1000000 \)、`blockDim.x = 256` 时，网格大小 \( = (1000000+255)/256 = 3907 \)（\( 3907 \times 256 = 1000192 \geq 1000000 \)，\( 3906 \times 256 = 999936 < 1000000 \)），线程总数 1000192，比 \( n \) 多出 192 个——这 192 个线程的 \( i \) 落在 \( [1000000, 1000192) \)，若去掉检查就是**越界读写显存**。test.sh 的 4 个规模恰好都是 256 的倍数（4.1.4 验证过），所以仓库自带的实验从没触发过这条检查，但它是健壮性所必需的。

另外注意 `axpy_cudakernel_warmingup` 的 [第 12 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L12) 用的是写法 `blockDim.x * blockIdx.x + threadIdx.x`（乘法在前），而 block/cyclic kernel 的 [第 27 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L27) 与 [第 43 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L43) 写成 `threadIdx.x + blockIdx.x * blockDim.x`——**运算顺序不同、数学上完全等价**，读别人的 CUDA 代码时要能一眼认出这同一个公式的多种排版。

#### 4.3.4 代码实践

**实践目标**：亲手验证手推的线程编号公式，而不是只相信书上的表格。

**操作步骤**：

1. 把 `CoMem_AXPY` 目录复制一份作为你的练习副本（例如 `cp -r CoMem_AXPY ../my_axpy_playground`，**不要**直接改仓库源码）。
2. 在练习副本的 `axpy_cudakernel.cu` 里**新增**一个下面这样的调试 kernel（注意：这是**示例代码**，不是仓库原有内容；仓库的 4 个 kernel 不打印任何东西）：

```cuda
// 示例代码：打印前几个线程的自我认知，仅用于教学观察
__global__
void debug_whoami(int n) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < 8)  // 只让最前面的 8 个线程说话，避免刷屏
        printf("block %d thread %d -> global i = %d\n",
               blockIdx.x, threadIdx.x, i);
}
```

3. 在 `axpy_cuda` 包装函数里临时调用它（同样加在你的练习副本里）：`debug_whoami<<<(n+127)/128, 128>>>(n); cudaDeviceSynchronize();`
4. 用 `make` 重新编译，先跑 `./axpy_cuda 130`，再跑 `./axpy_cuda 1024000`，对比输出。

**需要观察的现象**：`n=130` 时应看到 block 0 的线程 0…7 和 block 1 的线程 0、1 打印出全局编号 0…7 与 128、129 附近的结果；`n=1024000` 时块编号会大得多。

**预期结果**：打印出的 `global i` 恰好等于 `blockIdx.x * 128 + threadIdx.x`，与 4.3.2 的手推表一致。若本机无 GPU，待本地验证；无 GPU 时也可以只在纸面上完成 4.3.2 的表格推演。

#### 4.3.5 小练习与答案

**练习 1**：`n = 20480000`、`blockDim.x = 512` 时，网格需要多少个块？`gridDim.x` 在 kernel 里读到的就是它吗？

**答案**：\( (20480000 + 511) / 512 = 40000 \) 个块（20480000 恰能被 512 整除，加 511 再整除不改变结果）。是的，`gridDim.x` 就是启动配置的第一个参数，kernel 内读到 40000。

**练习 2**：`n = 1000000`、`blockDim.x = 256` 时，有多少线程的 `if (i < n)` 判定为假？

**答案**：192 个。网格大小 \( (1000000+255)/256 = 3907 \)，线程总数 \( 3907 \times 256 = 1000192 \)，\( 1000192 - 1000000 = 192 \)，它们集中在最后一个块的高编号线程里。

**练习 3**：kernel 里能改 `blockDim.x` 或 `blockIdx.x` 吗？

**答案**：不能（也不该）。它们是只读的内建变量，值由 host 端的启动配置在启动那一刻决定，GPU 上每个线程只能读取。想改变它们，唯一的办法是回到 host 代码修改 `<<<grid, block>>>` 的参数再重新启动——这正是 4.4 的主题。

### 4.4 `<<<grid, block>>>`：启动配置如何决定并行规模

#### 4.4.1 概念说明

host 代码里启动 kernel 用的是三尖括号语法：

```cuda
kernel<<<gridDim, blockDim>>>(参数...);
```

- **第一个参数 `gridDim`**：网格里开多少个**块**；
- **第二个参数 `blockDim`**：每个块里开多少个**线程**。

一次启动的并行规模就是两者的乘积 \( \text{gridDim} \times \text{blockDim} \)。这两个数是 host 端程序员的"调度决策"：块开多少、每块多大，直接决定了 4.3 那个全局编号公式里的 \( g \)（`blockDim.x`）与块的取值范围。

对 1perThread 这种"每线程一个元素"的方案，要求线程总数**不少于** \( n \)。若固定每块 256 线程，块数就得对 \( n / 256 \) **向上取整**。整数运算里向上取整的经典技巧是"加分母减一，再整除"：

\[ \left\lceil \frac{n}{b} \right\rceil \;=\; \left\lfloor \frac{n + b - 1}{b} \right\rfloor \]

代入 \( b = 256 \) 就是源码里的 `(n + 255) / 256`。C 的整数除法自动向下取整，配上"加 255"恰好实现了"向上取整"。

#### 4.4.2 核心流程

以 `n = 1024000`、每块 256 线程为例，走一遍从启动配置到线程归属的完整链条：

```text
host: axpy_cudakernel_1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a)
        │
        ├─ gridDim.x = (1024000+255)/256 = 4000 个块
        ├─ blockDim.x = 256 线程/块
        ├─ 并行规模 = 4000 × 256 = 1024000 个线程  ← 恰好等于 n（此规模整除）
        ▼
device: 每个线程执行同一份函数体
        线程 (blockIdx.x = 3999, threadIdx.x = 255):
            i = 256 × 3999 + 255 = 1023999  ← 全局最后一个元素
            i < n 成立 → y[1023999] += a * x[1023999]
```

这里 \( 1024000 = 4000 \times 256 \) 恰好整除，所以并行规模与 \( n \) 一一对应、一个不多一个不少；换成 \( n = 1000000 \) 就是 3907 块、1000192 线程，多出 192 个（4.3.3 算过）。

#### 4.4.3 源码精读

[CoMem_AXPY/axpy_cudakernel.cu:61-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68) —— `axpy_cuda` 包装函数里的四次 kernel 启动：warmingup 与 1perThread 的网格大小随 \( n \) 自适应（`(n+255)/256` 向上取整），block 与 cyclic 则固定 `<<<1024, 256>>>`，每次启动后都跟一句 `cudaDeviceSynchronize()` 等待 GPU 完成。

```cuda
  // Perform axpy elements
  axpy_cudakernel_warmingup<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
  axpy_cudakernel_1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
  axpy_cudakernel_block<<<1024, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
  axpy_cudakernel_cyclic<<<1024, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
```

四个启动分两组，值得对比：

| kernel | 启动配置 | 网格大小 | 每线程工作量 |
| --- | --- | --- | --- |
| warmingup / 1perThread | `<<<(n+255)/256, 256>>>` | 随 \( n \) 变化，保证线程数 ≥ \( n \) | 恰好 1 个元素 |
| block / cyclic（下一讲） | `<<<1024, 256>>>` | 固定 262144 个线程 | 每线程多个元素（循环） |

为什么 block/cyclic 敢用固定网格？因为它们每个线程用**循环**处理多个元素，线程数不必等于 \( n \)（例如 block 版把 \( n \) 均分给 262144 个线程，cyclic 版让线程跨步跳跃）——这是两种根本不同的任务划分哲学，u2-l2 会专门对比。本讲只需记住：**"每线程一个元素"方案必须让网格随 \( n \) 缩放，而"每线程多个元素"方案可以固定网格**。

顺带一提，`<<<...>>>` 里的两个数在本讲只用了最简形式（int 或整型表达式）；完整语法还支持可选的共享内存字节数与 stream 参数，遇到时再讲。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手修改启动配置，观察块大小（block size）对一个访存受限 kernel 的影响，并解释"为什么影响不大"。

**操作步骤**：

1. 复制 `CoMem_AXPY` 为练习副本（同 4.3.4，不要直接改仓库源码）。
2. 在副本的 `axpy_cudakernel.cu` 中找到 1perThread 的启动行 [第 63 行](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L63)：

   ```cuda
   axpy_cudakernel_1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
   ```

3. **只改这一行**（保持 warmingup、block、cyclic 三处启动不变，维持控制变量），依次做三个版本：
   - 原版：`<<<(n+255)/256, 256>>>`（基准）
   - 版本 A：`<<<(n+127)/128, 128>>>`
   - 版本 B：`<<<(n+511)/512, 512>>>`

   注意分子上的 `+255/+127/+511` 必须跟着块大小同步改成 `块大小-1`，否则向上取整就错了。
4. 每个版本 `make` 重新编译后，分别在 `n = 1024000` 和 `n = 10240000` 下运行：
   - `./axpy_cuda 1024000`
   - `./axpy_cuda 10240000`
   并用 `nvprof ./axpy_cuda 10240000` 记录 `axpy_cudakernel_1perThread` 单个 kernel 的 GPU time（不要只看程序打印的 time——它计的是整个 `axpy_cuda` 十轮平均墙钟，含显存分配与拷贝，口径不同，见 u1-l4）。
5. 把 6 组结果（3 个块大小 × 2 个规模）填进一张表。

**需要观察的现象**：程序打印的 `time` 在三个版本之间差别很小（可能淹没在噪声里）；`nvprof` 里 1perThread 的 kernel time 也差别不大；`checksum` 列在每个版本内基本一致（改块大小不改变计算内容，只改线程组织）。

**预期结果**（待本地验证）：三个块大小下 kernel 时间应基本持平。原因分析：

1. **瓶颈在内存带宽**：4.1.5 算过每线程 24 字节搬运只换 2 次运算，kernel 是访存受限的，性能上限是显存带宽，而 128/256/512 线程每块的配置都足以让 GPU 的流处理器（SM）拿到足够多的 warp 去隐藏访存延迟。
2. **128、256、512 都是 32（warp 大小）的整数倍**：块内没有残缺的 warp，三种配置下硬件按 warp 调度的效率是同一档的。
3. **总线程数不变**：三个版本都保证线程总数 ≈ \( n \)，每线程依然只处理一个元素，访存总量与访问模式（连续、合并——u4-l2 的主题）完全一样。

真正会让这类 kernel 明显变慢的是破坏合并访问或把块大小取成非 32 倍数等做法，而不是 128 与 512 之间的这种"健康区间"选择。若你的测量显示明显差异，优先怀疑计时口径（把 kernel time 和墙钟 time 混了）或机器上同时跑着别的任务。

#### 4.4.5 小练习与答案

**练习 1**：把启动写成 `<<<(n+255)/128, 128>>>`（块大小改成 128，但分子仍加 255），会发生什么？

**答案**：结果仍然正确，只是网格偶尔偏大。\( \lfloor (n+255)/128 \rfloor \geq \lceil n/128 \rceil \)，即开出的线程数只会更多不会不够，多出的线程被 `if (i < n)` 挡住，正确性无虞，但某些 \( n \) 下会浪费一个块的线程。反过来若写成 `<<<(n+127)/256, 256>>>`（加的数比块大小减一还小），当 \( n \) 是 256 的奇数倍附近时就可能**少开一个块**，导致尾部元素没人处理——那才是真 bug。

**练习 2**：`n = 1` 时 `<<<(1+255)/256, 256>>>` 会启动多少线程？浪费多少？

**答案**：1 个块、256 个线程，其中 255 个被 `if (i < n)` 挡住，只有 `i = 0` 的线程干活。GPU 不擅长"小任务"由此可见一斑——启动一个块本身就有开销，并行规模太小根本跑不满硬件。

**练习 3**：为什么不干脆把块大小设成 1（`<<<n, 1>>>`），逻辑上也是每线程一个元素？

**答案**：正确性上没问题，但性能上灾难性的：GPU 以 warp（32 线程）为调度单位，一个块只有 1 个线程意味着每个 warp 里 31 个通道空转，且硬件对"每 SM 能同时驻留多少个块"有上限（块数太多时驻留反而受限于块调度器），实际并行度大幅缩水。这就是为什么经验上块大小取 128～512（32 的倍数）。可在 4.4.4 的练习副本里加一个 `<<<n, 1>>>` 版本亲自验证。

## 5. 综合实践

把本讲的三块知识（索引公式、启动配置、边界检查）串成一个完整的"线程编号侦探"小任务：

**任务**：给你一个未知的启动配置，不看运行结果，仅凭纸面推演预测每个线程的行为，再用 `nvprof` 和打印验证。

1. **纸面推演**。取 \( n = 1000000 \)，对下表三行分别算出：网格大小、线程总数、被 `if (i < n)` 挡住的线程数、最后一个干活的线程的 `(blockIdx.x, threadIdx.x)`：

   | 块大小 | 网格大小 | 线程总数 | 被挡线程数 | 最后干活的线程 |
   | --- | --- | --- | --- | --- |
   | 128 | ？ | ？ | ？ | ？ |
   | 256 | ？ | ？ | ？ | ？ |
   | 512 | ？ | ？ | ？ | ？ |

   参考答案（256 那行）：网格 3907、线程总数 1000192、被挡 192；最后一个干活的线程处理 \( i = n-1 = 999999 \)，由 \( 256 \times 3906 = 999936 \) 可知它是**块 3906 内的线程 63**（\( 999999 - 999936 = 63 \)）——注意它不是"最后一个块的最后一个线程"：最后的块 3906 里只有线程 0…63 在干活，线程 64…255 全被边界检查挡住。这道题的陷阱正是逼你完整走一遍公式，而不是想当然地填"块 3906、线程 255"。128 与 512 两行请独立完成。

2. **程序验证**。在 4.3.4 的练习副本里，把调试 kernel 的打印条件改成 `if (i >= n - 4 && i < n)`（只让"最后几个干活的线程"说话），用 `./axpy_cuda 1000000` 运行，核对打印出的 `(blockIdx.x, threadIdx.x, i)` 是否与你的推演一致。

3. **性能验证**。接着完成 4.4.4 的块大小实验（128/256/512 × 两个规模），把 kernel time 与你的推演表并排整理成一份简短实验记录，写清：改了哪一行、控制了哪些变量、观察口径是 nvprof 的 kernel time 还是程序墙钟、结论及其适用范围（访存受限、每线程一元素、块大小为 32 倍数的场景）。

这个任务做完，你就具备了读懂任意一维 CUDA kernel"线程怎么分活"的能力——它是本项目所有 14 个微基准共同的地基。

## 6. 本讲小结

- AXPY 的 \( n \) 个元素互相独立，最朴素的 GPU 并行化就是"每线程一个元素"（`axpy_cudakernel_1perThread`），串行版的 `for` 循环被折叠进了线程维度。
- `__global__` 声明 kernel：设备上执行、host 端用 `<<<grid, block>>>` 调用、返回值必须是 `void`；一次启动意味着每个线程各执行一份函数体。
- 线程组织成 grid → block → thread 两级层级；"我是谁"由 `i = blockDim.x * blockIdx.x + threadIdx.x` 回答，该公式保证编号分段连续、无缝无重叠。
- 启动配置 `<<<(n+255)/256, 256>>>` 的第一个数是块数、第二个数是每块线程数；`(n+255)/256` 是整数向上取整技巧，保证线程总数 ≥ \( n \)。
- 线程总数可能超过 \( n \)，kernel 里的 `if (i < n)` 边界检查防止越界访问显存，是健壮性所必需（test.sh 的规模恰好全是 256 的倍数，所以仓库自带实验从未触发它）。
- 对这种每线程 24 字节搬运换 2 次运算的访存受限 kernel，块大小在 128～512（32 的倍数）之间变化对性能影响很小，瓶颈在显存带宽而不是线程组织。

## 7. 下一步学习建议

下一讲 **u2-l2《一维数据的多种并行化策略》** 自然衔接本讲 4.4.3 留下的悬念：`axpy_cudakernel_block` 与 `axpy_cudakernel_cyclic` 如何用固定网格 + 每线程循环的方式处理任意 \( n \)，其中 block 版源码里还留有一处 `TODO handle non-dividiable later`（[CoMem_AXPY/axpy_cudakernel.cu:30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L30)）等着你去修复。之后再进入 u2-l3 的显存管理（`cudaMalloc`/`cudaMemcpy`/同步语义）与 u2-l4 的正确性验证。想提前建立 warp 直觉的读者，可以先跳读 u3-l1 的开头部分，再回来按顺序学习。
