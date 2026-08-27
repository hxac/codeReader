# 单个微基准的解剖：host 主程序 + kernel + 头文件三件套

## 1. 本讲目标

学完本讲，你应该能够：

1. 拿到 CUDAMicroBench 中任何一个基准目录，一眼识别出它的「三件套」骨架：host 主程序（`.c`）、kernel 文件（`.cu`）、接口头文件（`.h`）。
2. 说出骨架中五个固定阶段的职责：**数据初始化 → 串行基线 → warmup → 计时循环 → check 校验**，并指出每个阶段发生在哪个文件、哪段代码。
3. 解释 `read_timer_ms` 计时、`num_runs` 多轮平均、warmingup kernel 各自解决什么测量问题。
4. 画出一次调用 `main → axpy_cuda → 各 kernel` 的控制流与数据流图。
5. 说清 `axpy.h` 里 `extern "C"` 存在的原因，以及 `Common/` 目录（源自 CUDA Samples 的 helper 头文件）到底服务哪几个基准。

本讲只读代码、不写代码，**没有 GPU 也能完成全部内容**；涉及运行的步骤都标注了「待本地验证」。

## 2. 前置知识

在开始之前，用最通俗的语言扫清几个术语（已学过 u1-l1、u1-l2 的读者可以快速略读）：

- **host 与 device**：host 指 CPU 及其内存（主机内存），device 指 GPU 及其显存。CUDA 程序总是「host 上的普通 C/C++ 程序 + device 上运行的函数」两部分拼起来的。
- **kernel**：在 GPU 上运行的函数，用 `__global__` 修饰，由 host 端用 `<<<grid, block>>>` 语法启动。host 不会「调用」它并等返回值，而是「提交」给 GPU 去并行执行。
- **串行基线（serial baseline）**：同一个计算用普通 C 循环在 CPU 上写一遍。它有两个用途：作为正确性的参照答案、作为性能的对照锚点。微基准的核心方法论就是「串行版 vs 优化版」成对出现。
- **checksum / check**：衡量两个数组差多少的归一化数值，本项目中由 `check()` 函数计算（数值越接近 0 越一致）。
- **warmup（预热）**：第一次在 GPU 上运行 kernel 时，驱动初始化、模块加载、指令缓存填充等一次性开销会污染计时，所以先跑一个「不计入成绩」的 kernel 把这些开销消化掉。
- **微基准（microbenchmark）**：只测量一个性能因素的极小程序。它不是完整应用，而是「控制变量实验」：反模式版本与优化版本只差一行或一个参数，其余全部相同。

另外一个工程背景（u1-l2 已讲）：`nvcc` 把 `.c` 文件当 C 编译、把 `.cu` 文件当 C++ 编译，最后链接成一个可执行文件。**C 和 C++ 的函数符号命名规则不同（name mangling）**，这就是本讲会反复出现的 `extern "C"` 的由来。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口头文件（三件套之三） | `REAL` 宏、`extern "C"` 包裹的函数声明 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序（三件套之一） | `main`、`init`、串行 `axpy`、`check`、`read_timer_ms` |
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | kernel + GPU 入口（三件套之二） | 4 个 `__global__` kernel、`axpy_cuda` 主机包装函数 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 一行式构建脚本 | 两个源文件如何变成一个可执行文件 |
| [CoMem_AXPY/test.sh](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh) | 实验脚本 | 多规模 + nvprof 采集 |
| [MemAlign/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c)、[MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu)、[MemAlign/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy.h) | 对照样本 | 验证骨架的同构性（第 5 节综合实践用） |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 项目总说明 | 「Common folder」一节 |
| [Common/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md) | Common 目录自述 | helper 头文件的服务对象 |

## 4. 核心概念与源码讲解

本讲的最小模块有四个：**4.1 host 主程序骨架**、**4.2 kernel 文件**、**4.3 公共头文件**、**4.4 Common 目录**。前三个合起来就是「三件套」。

### 4.1 host 主程序骨架：`axpy_cuda.c`

#### 4.1.1 概念说明

host 主程序是基准的「实验控制器」。它自己不做任何 GPU 操作，只负责安排实验流程。CUDAMicroBench 绝大多数基准的 host 程序都遵循同一个五阶段模板：

```text
阶段① 数据初始化   分配 host 数组、填入随机数（每次运行可复现）
阶段② 串行基线     用普通 C 循环算出参照答案 y
阶段③ warmup      【在 .cu 文件的 axpy_cuda 内部】先跑一个不计时的 kernel
阶段④ 计时循环     调用 CUDA 入口 num_runs 次，取平均耗时
阶段⑤ check 校验   比较 y_cuda 与 y 的归一化差值，打印 checksum 与时间
```

为什么要把这些阶段固定下来？因为微基准的生命线是**控制变量**：被对比的两个 kernel 之间只允许差一个因素，数据生成方式、规模、计时口径、校验方法必须完全一致。把流程写死在一个模板里，是防止实验者在不知不觉中改变了变量。

#### 4.1.2 核心流程

`main` 的执行流程（伪代码）：

```text
main:
  n = 命令行参数 或 默认 VEC_LEN(1024000)
  malloc 三个 host 数组: x, y_cuda, y          # 同一份初始数据喂两条路径
  srand48(1<<12); init(x); init(y_cuda)
  memcpy(y, y_cuda)                            # y 与 y_cuda 起点完全相同
  axpy(x, y, n, a)                             # 阶段②：串行基线（不在计时内）
  t0 = read_timer_ms()
  循环 10 次: axpy_cuda(x, y_cuda, n, a)       # 阶段③④：内部含 warmup + 被测 kernel
  elapsed = (read_timer_ms() - t0) / 10
  checksum = check(y_cuda, y, n)               # 阶段⑤
  printf("axpy(%d): checksum: %g, time: %0.2fms", ...)
  free 三个数组
```

计时口径是一个需要留心的点：这里测量的是 **host 侧墙钟时间**，平均值按下式计算：

\[ \bar{t} \;=\; \frac{t_{\text{end}} - t_{\text{start}}}{\text{num\_runs}} \]

由于 `axpy_cuda` 内部包含 `cudaMalloc`、两次 H2D 拷贝、若干 kernel、一次 D2H 拷贝和 `cudaFree`，这个时间**大于纯 kernel 执行时间**——这正是 u1-l2 强调过的结论：要看纯 kernel 时间须用 nvprof（见 u1-l4）。

#### 4.1.3 源码精读

**计时器：`read_timer_ms` 用 `ftime` 取毫秒级墙钟时间。**

[CoMem_AXPY/axpy_cuda.c:L14-L18](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L14-L18) 定义了整个项目通用的计时函数：把 `struct timeb` 的秒和毫秒拼成一个 double 毫秒数返回。精度只有 1 毫秒，所以必须配合多轮平均来降低读数误差。

**精度开关与默认规模。**

[CoMem_AXPY/axpy_cuda.c:L20-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L20-L22) 中 `#define REAL double` 决定整个基准用双精度（daxpy）；改成 `float` 就是单精度（saxpy）。`VEC_LEN 1024000` 是不带命令行参数时的默认向量长度。

**数据初始化：`init` 填随机数。**

[CoMem_AXPY/axpy_cuda.c:L33-L39](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L33-L39) 用 `drand48()` 给数组填入 `[0,1)` 的随机 double。注意 [CoMem_AXPY/axpy_cuda.c:L77](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L77) 处 `srand48(1<<12)` 固定了随机种子——这是骨架的可复现性设计：同一台机器上每次运行得到完全相同的输入数据。（同文件里还有一个 `zero` 函数，[L24-L30](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L24-L30)，本基准的主流程并未用到它，属于骨架的「常备工具」。）

**串行基线：`axpy`。**

[CoMem_AXPY/axpy_cuda.c:L41-L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L41-L48) 是教科书式的 AXPY 循环：`y[i] += a * x[i]`。AXPY（A·X Plus Y）是 BLAS 里最基础的线性代数内核，被选作教学载体正是因为它简单到「一眼看懂」、又真实地包含读两个数组、写一个数组的内存行为。

**校验：`check` 返回归一化差值。**

[CoMem_AXPY/axpy_cuda.c:L50-L60](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L50-L60) 定义的 `check` 计算：

\[ \text{checksum} \;=\; \frac{\displaystyle\sum_{i=0}^{n-1} \lvert A_i - B_i \rvert}{\displaystyle\sum_{i=0}^{n-1} \lvert B_i \rvert} \]

即「差多少」除以「本体多大」，得到一个与规模无关的相对量。A 是 CUDA 结果 `y_cuda`，B 是串行结果 `y`。

**主流程：五个阶段在 `main` 里的落点。**

[CoMem_AXPY/axpy_cuda.c:L62-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L62-L80) 完成阶段①：解析 `argv[1]` 得到 n（[L68-L72](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L68-L72)），`malloc` 出 `y_cuda`、`y`、`x` 三个数组（[L73-L75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L73-L75)），初始化 `x` 和 `y_cuda` 后用 `memcpy` 把 `y_cuda` 复制成 `y`（[L77-L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L77-L80)）——这一步保证两条路径的**起点逐比特相同**。

[CoMem_AXPY/axpy_cuda.c:L82](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L82) 是阶段②：串行 `axpy` 只跑一次，且**不在计时范围内**（计时器在 L87 才启动）。

[CoMem_AXPY/axpy_cuda.c:L84-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L84-L89) 是阶段④：`num_runs = 10`，围绕循环两次读取 `read_timer_ms()` 相减再除以 10。阶段③（warmup）藏在每次 `axpy_cuda` 调用的内部（见 4.2.3）。

[CoMem_AXPY/axpy_cuda.c:L90-L93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L90-L93) 是阶段⑤：计算 checksum 并按统一格式打印（规模、checksum、平均时间）。注意 [L93](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L93) 的 `assert` 被**注释掉了**——这说明在本基准里 `check` 更多是「差值探针」而非严格的通过/失败判据，原因见下面 4.2.4 的分析。

#### 4.1.4 代码实践

**实践：先「纸面预测」checksum，再运行核对。**

1. 实践目标：不运行程序，仅凭源码推理出 `checksum` 应该出现在什么量级，理解 `check` 在这个骨架里扮演的真正角色。
2. 操作步骤：
   - 读 [CoMem_AXPY/axpy_cudakernel.cu:L52-L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73)，数一数一次 `axpy_cuda` 调用中有**几个** kernel 都对同一个 `d_y` 执行了 `+= a*x`（答案是 4 个：warmingup、1perThread、block、cyclic）。
   - 推理：每次调用使 `y_cuda` 净增约 \(4 \cdot a \cdot \bar{x}\)，计时循环共调用 10 次，而串行基线只加 \(1 \cdot a \cdot \bar{x}\)。代入 \(a = 123.456\)、\(\bar{x} \approx 0.5\)、\(\bar{y}_0 \approx 0.5\)，估算 checksum 量级。
   - 有 GPU 的话运行 `make && ./axpy_cuda 1024000` 核对打印值（注意 `block` kernel 只覆盖数组前约 76.8% 的元素——n=1024000、262144 个线程、每人 3 个元素——所以差值并非处处相同）。
3. 需要观察的现象：打印出的 `checksum` 是一个**远大于 0** 的数（几十的量级），而不是浮点误差级别的小数。
4. 预期结果：checksum ≈ 量级为「(施加次数差) × a × x̄ ÷ (y₀ + a·x̄)」的数值。**待本地验证**（具体数值依赖 GPU 与运行环境，但「远大于 0」这一结论只由源码决定）。
5. 这个实践教我们的道理：**读基准输出之前先读数据流**。CoMem_AXPY 的四个 kernel 共享同一个 `d_y`、反复叠加，check 自然不会归零；这不是 bug，而是该基准以时间对比为主、以 check 为差值探针的设计选择（所以 assert 被注释掉了）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main` 要分配 `y` 和 `y_cuda` 两个数组，还要 `memcpy(y, y_cuda, ...)`，而不是让两条路径共用一个数组？
**答案**：串行基线和 CUDA 路径必须从**完全相同的初始状态**出发，结果才能逐元素比较；同时两条路径都会就地修改各自的输出数组，共用一个数组会让先跑的一方污染后跑一方的输入。`memcpy` 保证了「同源、分流、互不干扰」。

**练习 2**：`read_timer_ms` 精度只有 1 毫秒，骨架用什么手段让毫秒级误差不影响结论？
**答案**：两件事配合：一是把循环跑 `num_runs = 10` 次取平均（[L87-L89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L87-L89)），把单次读数误差稀释到 1/10；二是默认 `VEC_LEN = 1024000` 让单次执行时间足够长（毫秒级以上），使相对误差可忽略。想看更细的 kernel 级时间则交给 nvprof（u1-l4）。

**练习 3**：如果去掉 warmup 阶段，测出的时间会偏向哪边？为什么？
**答案**：偏大。第一次把 kernel 提交给 GPU 时，驱动要完成上下文初始化、module 加载、指令缓存填充等一次性工作，这些开销会计入第一次循环的墙钟时间；warmup kernel 先「消化」掉这些开销，使被测轮次尽量只包含稳定状态的执行成本。

### 4.2 kernel 文件：`axpy_cudakernel.cu`

#### 4.2.1 概念说明

`.cu` 文件是「device 侧的家」：所有 `__global__` kernel 都定义在这里。但注意，它**不只包含 kernel**——CUDAMicroBench 的约定是把 `axpy_cuda` 这个「主机包装函数」也放在 `.cu` 里，作为 host 程序与 kernel 之间唯一的桥。

为什么 host 主程序用 `.c`、桥和 kernel 放 `.cu`？因为 `<<<>>>` 启动语法和 `__global__` 修饰符只有 nvcc 的 CUDA C++ 方言才认识，普通 C 编译器（把 `.c` 当 C 编译）无法处理。于是职责切分为：

- `.c`（纯 C）：实验流程——数据、基线、计时、校验。不含任何 CUDA 语法。
- `.cu`（CUDA C++）：GPU 资源管理——显存分配、数据搬运、kernel 启动、同步、释放。
- `.h`：两者之间的契约（见 4.3）。

这个切分让「实验设计」与「GPU 实现」解耦：想把 CUDA 版换成 OpenMP 版，只需另写一个同名函数，`.c` 几乎不动（UniMem 目录正是这么做的，它带有 `LowAccessDensityTest_omp.c` 串行/OpenMP 对照版本）。

#### 4.2.2 核心流程

`axpy_cuda` 包装函数的五段式（这是**所有**基准共享的模式）：

```text
axpy_cuda(x, y, n, a):
  ① cudaMalloc d_x, d_y                      # 在显存里开空间
  ② cudaMemcpy H2D: x → d_x, y → d_y         # 主机内存 → 显存
  ③ 依次启动各 kernel，每个后面跟 cudaDeviceSynchronize
       warmingup   <<< (n+255)/256, 256 >>>   # 预热，不计入对比
       1perThread <<< (n+255)/256, 256 >>>    # 被测 kernel A
       block      <<< 1024, 256        >>>    # 被测 kernel B
       cyclic     <<< 1024, 256        >>>    # 被测 kernel C
  ④ cudaMemcpy D2H: d_y → y                  # 显存 → 主机内存
  ⑤ cudaFree d_x, d_y                        # 释放显存
```

数据流一句话：**host 内存 → 显存 → kernel 加工 → 显存 → host 内存**。控制流一句话：**每次 kernel 启动后立刻 `cudaDeviceSynchronize`，保证「排队的都已执行完」再走下一步**。

#### 4.2.3 源码精读

**四个 kernel：同一 AXPY 的四种线程划分。**

[CoMem_AXPY/axpy_cudakernel.cu:L8-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L8-L14) 是 warmingup kernel，[L16-L22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L16-L22) 是 `1perThread`——两者的函数体**逐字符相同**（全局线程编号 `blockDim.x * blockIdx.x + threadIdx.x`，越界保护 `if (i < n)`），区别只在被调用的时机：一个用于预热，一个是被测版本。这也解释了为什么它对 `d_y` 的修改会叠加进结果（4.1.4 的分析）。

[L24-L38](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L24-L38) 是 `block` 分布：每线程负责**连续**一段（`block_size = n / total_threads`），源码注释明确留了 `TODO handle non-dividiable later`——u2-l2 会拿这个 TODO 做练习。[L40-L50](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L40-L50) 是 `cyclic` 分布：每线程以 `total_threads` 为步长跨步访问。同一计算、三种任务划分放在一起，本身就是「控制变量对比」的示范（详见 u2-l2）。

**主机包装函数 `axpy_cuda`：五段式的落地。**

[CoMem_AXPY/axpy_cudakernel.cu:L52-L58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L58) 完成段①②：两个 `cudaMalloc` 在显存开出 `d_x`、`d_y`，两条 `cudaMemcpy(..., cudaMemcpyHostToDevice)` 把 host 数据搬进去。方向枚举 `cudaMemcpyHostToDevice` / `cudaMemcpyDeviceToHost` 是 CUDA 里区分搬运方向的标准写法。

[CoMem_AXPY/axpy_cudakernel.cu:L60-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L60-L68) 完成段③：四次启动、四次 `cudaDeviceSynchronize`。注意网格配置的差异——warmingup 和 1perThread 用 `(n+255)/256` 个 block（向上取整保证覆盖 n 个元素），block 和 cyclic 固定用 `<<<1024, 256>>>`（262144 线程，靠 kernel 内部循环覆盖全部元素）。

[CoMem_AXPY/axpy_cudakernel.cu:L70-L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L70-L73) 完成段④⑤：`cudaMemcpyDeviceToHost` 把结果写回 host 的 `y`（也就是 `main` 传入的 `y_cuda`），随后 `cudaFree` 释放两块显存。

**构建与实验脚本把三件套串起来。**

[CoMem_AXPY/Makefile:L1-L2](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile#L1-L2) 的单行规则把 `.c` 与 `.cu` 一起交给 nvcc；[CoMem_AXPY/test.sh:L1-L4](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/test.sh#L1-L4) 用四个递增的 n 值在 nvprof 下运行——同一个骨架、多个规模，这正是微基准的标准实验形态（u1-l2）。

#### 4.2.4 代码实践

**实践：用 nvprof 数 kernel、核对控制流。**

1. 实践目标：验证「一次 `axpy_cuda` 调用 = 4 个 kernel」的控制流推断，并看清 host 墙钟时间里各部分的占比。
2. 操作步骤：进入 `CoMem_AXPY/` 目录，执行 `make`，然后运行 `nvprof ./axpy_cuda 1024000`（若无 nvprof，用 `nsys profile` 或 Nsight Compute 替代，见 u1-l2 的说明）。
3. 需要观察的现象：GPU activities 一节会列出 `axpy_cudakernel_warmingup`、`axpy_cudakernel_1perThread`、`axpy_cudakernel_block`、`axpy_cudakernel_cyclic` 各被调用 10 次（10 轮计时循环 × 每轮 4 个）；此外每轮还有 2 次 H2D、1 次 D2H 的 memcpy。
4. 预期结果：四个 kernel 的实例数都是 10，memcopy 操作数与推理一致。**待本地验证**。
5. 若暂时没有 GPU：这一步可以完全用「纸面追踪」替代——从 [axpy_cuda.c:L88](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L88) 的循环体出发，沿 `axpy_cuda` → 四次 kernel 启动画出完整调用链（即第 5 节综合实践的调用图）。

#### 4.2.5 小练习与答案

**练习 1**：`cudaDeviceSynchronize` 在 [L61-L68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L61-L68) 里出现了四次。如果全部删掉，程序结果会错吗？
**答案**：结果大概率仍正确：[L70](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L70) 的 D2H `cudaMemcpy` 本身就会隐式等待同一流中之前的操作完成，`cudaFree` 也会阻塞到使用该内存的工作结束。但显式同步让「每个 kernel 独立占一段时间」成为事实，保证被对比的 kernel 互不重叠计时（这一点对 Conkernels 那类并发实验尤其重要）；删掉后 kernel 可能背靠背挤在同一队列里，语义与测量口径都变了。

**练习 2**：为什么 warmingup 和 1perThread 的网格是 `(n+255)/256`，而 block 和 cyclic 用固定的 `<<<1024, 256>>>`？
**答案**：前两者是「每线程至多处理 1 个元素」的划分，block 数必须随 n 增长才能覆盖全数组，`(n+255)/256` 即向上取整的 \(\lceil n/256 \rceil\)；后两者是「每线程循环处理多个元素」的划分，线程总数只需固定（262144），由 kernel 内部的 `for` 循环覆盖任意 n。这正是 u2-l2 要展开的两种并行化策略。

**练习 3**：`axpy_cuda` 里没有对 `cudaMalloc`/`cudaMemcpy`/kernel 启动做任何错误检查。这样做有什么风险？
**答案**：任何一步失败（显存不足、启动配置非法）都可能被静默吞掉，后续结果要么是旧数据要么是未定义行为，而 `check` 只能报告「数值不一致」却无法定位原因。为每一步补 `cudaGetLastError()` / 检查返回值是标准的健壮化手段——这正是 u2-l3 的实践任务。

### 4.3 公共头文件：`axpy.h` 与 `extern "C"`

#### 4.3.1 概念说明

头文件是三件套里最小的一个，却是 host `.c` 与 device `.cu` 之间的**契约**：`.c` 只 include 它，就能调用 `axpy_cuda` 而不必知道任何 CUDA 细节。它只做两件事：

1. 用 `#define REAL double` 统一精度类型——`.c` 和 `.cu` 都通过这个宏使用同一类型，改一处即全局切换 float/double。
2. 声明 `axpy_cuda` 函数原型，并用 `extern "C"` 包裹。

`extern "C"` 解决的是 C/C++ 混编的符号问题：C++ 编译器会把函数名「装饰」上参数类型信息（name mangling，例如 `axpy_cuda` 可能变成带后缀的符号），而 C 编译器不会。nvcc 把 `.cu` 当 C++ 编译、把 `.c` 当 C 编译：若不处理，`.c` 里引用的是未装饰的 `axpy_cuda` 符号，链接器在 `.cu` 的目标文件里只能找到装饰过的版本，报「undefined reference」。`extern "C"` 告诉 C++ 侧「这个函数按 C 规则命名符号」，两边就对上了。

#### 4.3.2 核心流程

```text
axpy.h 被两方包含:
  axpy_cuda.c（按 C 编译）  → 看到 void axpy_cuda(double*, double*, int, double);
  axpy_cudakernel.cu（按 C++ 编译）→ __cplusplus 已定义
      → extern "C" { 声明 }  → 该函数在 .cu 里定义时也按 C 符号导出
链接时: .c 引用的符号 == .cu 导出的符号  →  链接成功
```

`#ifdef __cplusplus` 守卫的作用：同一个头文件也能安全地被纯 C 翻译单元包含（C 里没有 `extern "C"` 这个语法，直接写会编译错误）。

#### 4.3.3 源码精读

[CoMem_AXPY/axpy.h:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L6) 定义 `REAL` 宏；注意 `.c` 文件里（[axpy_cuda.c:L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21)）又独立定义了一次同名宏——两处必须保持一致，这是该骨架的一个小陷阱：只改头文件不改 `.c`（或反之）会在链接或运行时暴露类型不一致。

[CoMem_AXPY/axpy.h:L8-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h#L8-L14) 是完整的契约：`extern "C"` 包裹的 `axpy_cuda` 声明。而 `.cu` 侧（[axpy_cudakernel.cu:L6](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L6)）第一件事就是 `#include "axpy.h"`，因此函数**定义**处也自动带上了 `extern "C"` 属性——声明和定义靠同一个头文件保持一致，这是 C/C++ 混编的标准做法。

对照 [MemAlign/axpy.h:L6-L14](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy.h#L6-L14)，可以看到两个基准的头文件**逐字符相同**（连版权头都一样）——三件套骨架在目录之间是复制后微调的，这是确认「同构性」的最直接证据。

#### 4.3.4 代码实践

**实践：亲手制造一次链接错误（编译期实验，无需 GPU）。**

1. 实践目标：用实验证实 `extern "C"` 的必要性。
2. 操作步骤：把 `CoMem_AXPY/axpy.h` 复制到一个临时目录（不要改源码），去掉 `extern "C"` 三行后，在临时目录里执行 `nvcc -o /tmp/axpy_test axpy_cuda.c axpy_cudakernel.cu -I.`（用 `-I.` 让编译优先找到改过的头文件；编译不需要 GPU，见 u1-l2）。
3. 需要观察的现象：链接阶段报 `undefined reference to 'axpy_cuda'`（或未定义符号）错误。
4. 预期结果：没有 `extern "C"` 时链接失败；恢复后成功。**待本地验证**。
5. 补充观察：错误发生在**链接**而非编译阶段——两个翻译单元各自都编译通过，只是符号名对不上。这能帮你定位今后遇到的同类报错。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `axpy.h` 里的 `REAL` 改成 `float`，只改这一个文件，程序还能得到正确结果吗？
**答案**：不能简单认为能。`.c` 文件里另有一份 `#define REAL double`（[axpy_cuda.c:L21](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L21)），两处不一致时，`.c` 按 double 分配和传递、`.cu` 按 float 解释，函数签名在二进制层面不匹配，结果是未定义行为。正确做法是同步修改两处（或把宏集中到头文件里只定义一次）。

**练习 2**：`#ifdef __cplusplus` 守卫去掉 `extern "C"` 之外还有什么意义？这个头文件能被第三个纯 C 文件包含吗？
**答案**：能。守卫保证当包含者是 C 翻译单元（`__cplusplus` 未定义）时，头文件退化成普通 C 声明；只有 C++ 翻译单元才展开 `extern "C"`。因此同一份契约可以同时服务 `.c` 和 `.cu` 双方，这正是它作为「公共头文件」的价值。

### 4.4 Common 目录：来自 CUDA Samples 的 helper 头文件

#### 4.4.1 概念说明

上面三件套描述的是「自研基准」的骨架（CoMem_AXPY、MemAlign、BankRedux、HDOverlap 等）。项目里还有另一类基准**直接改编自 NVIDIA 官方的 CUDA Samples 示例代码**：GSOverlap、Conkernels、TaskGraph，以及 Shuffle。这些样例原代码依赖一组 helper 头文件（`helper_cuda.h`、`helper_timer.h`、`helper_image.h` 等，提供设备查询、计时、图像 I/O 等便利功能）。为了让这些基准可编译，项目把所需的 helper 头文件集中放在 `Common/` 目录——所以 `Common/` 是「外部依赖的本地副本」，不属于三件套骨架本身。

需要注意 u1-l1 已发现的文档与实地差异：README 说 Common 服务 3 个基准，但实际 grep 所有 Makefile 后发现服务对象更多（见 4.4.3）。

#### 4.4.2 核心流程

```text
某基准的 Makefile:  INCLUDES := -I../Common（或 -I../../Common）
        ↓
源文件里:  #include <helper_cuda.h> 等
        ↓
编译器到 Common/ 找到这些头文件 → 基准可编译
（自研基准的 Makefile 没有这一行 → 完全不依赖 Common）
```

#### 4.4.3 源码精读

[README.md:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L113-L115) 是总 README 的「Common folder」一节：说明 GSOverlap、ConKernels、TaskGraph 三个基准源自 CUDA Samples，所需头文件存放在 common 文件夹。

[Common/README.md:L1](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Common/README.md#L1) 一句话自述：该目录派生自 CUDA Samples，用于支持若干派生自 Samples 的基准。目录下共有 17 个文件（如 `helper_cuda.h`、`helper_timer.h`、`helper_image.h`、`helper_string.h`、`exception.h` 等，可用 `ls Common/` 核对）。

真正确定「谁依赖 Common」的证据在各 Makefile 的 `INCLUDES` 行：

| Makefile | 引用路径 | 说明 |
| --- | --- | --- |
| [GSOverlap/Makefile:L280](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile#L280) | `-I../Common` | GSOverlap 在仓库根下一级，`../Common` 正确 |
| [TaskGraph/Makefile:L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/TaskGraph/Makefile#L274) | `-I../Common` | 同上，正确 |
| [Shuffle/cuda_shuffle/Makefile:L243](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/Makefile#L243)、[Shuffle/cuda_global/Makefile:L243](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_global/Makefile#L243) | `-I../../Common` | Shuffle 在仓库根下两级，`../../Common` 正确 |
| [Conkernels/Makefile:L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile#L274)（及 [Makefile_serialized:L274](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Conkernels/Makefile_serialized#L274)） | `-I../../Common` | Conkernels 在仓库根下**一级**，`../../Common` 指向了仓库外的上级目录，疑为多写了一级（u1-l1 的发现），应为 `-I../Common` |

结论：`Common/` 实际服务的基准是 **GSOverlap、TaskGraph、Shuffle（cuda_global 与 cuda_shuffle 两个子目录）、Conkernels**——比 README 列出的 3 个多出 Shuffle，且 Conkernels 的引用路径存疑。

#### 4.4.4 代码实践

**实践：用一条命令验证 Common 的服务对象。**

1. 实践目标：不依赖任何文档，仅凭仓库事实列出依赖 `Common/` 的全部目录。
2. 操作步骤：在仓库根目录执行 `grep -rn "Common" --include=Makefile* .`（本讲的表格即由此得出）；再用 `ls Common/` 清点 helper 文件；最后对照 `grep -l "helper_" */*.cu */*/*.cu 2>/dev/null` 看哪些源文件真正 `#include` 了 helper 头。
3. 需要观察的现象：除上表 6 个 Makefile 外，CoMem_AXPY、MemAlign、BankRedux、HDOverlap 等自研基准的 Makefile 均不含 `Common`——它们的三件套骨架零外部依赖。
4. 预期结果：依赖 Common 的是且仅是源自 CUDA Samples 的那几个目录；自研基准完全不依赖。
5. 若要验证 Conkernels 路径问题：在 Conkernels 目录执行 `ls ../../Common` 应报「不存在」，而 `ls ../Common` 能列出 helper 文件——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么自研基准不把 `read_timer_ms` 之类也搬进 Common？
**答案**：自研基准刻意保持三件套自包含：每个目录复制一份骨架、只改被测 kernel，副本之间的微小差异（如 MemAlign 的边界处理）不影响其他目录。集中共享公共代码虽好，但会让「每个基准只演示一个因素」的教学隔离性变差，也增加改动一处波及全局的风险。这是有意的工程取舍。

**练习 2**：`Conkernels/Makefile` 里 `-I../../Common` 的写法最可能是怎么产生的？
**答案**：最可能是从 Shuffle（或 CUDA Samples 原始工程，其目录层级更深）复制 Makefile 模板后没有调整相对路径。Conkernels 实际位于仓库根下一级，正确写法是 `-I../Common`。教训：阅读带相对路径的构建脚本时，必须把「脚本所在目录」代入计算，不能想当然。

## 5. 综合实践

**任务：为 CoMem_AXPY 画一张标注五阶段的调用图，并用 MemAlign 验证骨架同构性。** 这是本讲的总实践，完成后你就掌握了「解剖任何微基准」的标准动作。

### 5.1 画出调用图（阶段①—⑤落点）

先自己动手，再对照下面的参考答案。画图时从 [axpy_cuda.c:L62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L62) 的 `main` 出发，沿每一次函数调用走到叶子（kernel），并在每个节点旁标注「文件:行号附近」与所属阶段。参考答案：

```text
main                                    [axpy_cuda.c:L62]
├─ 阶段① 数据初始化
│   ├─ malloc x / y_cuda / y            [axpy_cuda.c:L73-L75]
│   ├─ srand48(1<<12)                   [axpy_cuda.c:L77]      ← 固定种子，可复现
│   ├─ init(x) / init(y_cuda)           [axpy_cuda.c:L78-L79]  ← drand48 随机数
│   └─ memcpy(y ← y_cuda)               [axpy_cuda.c:L80]      ← 两条路径同源
├─ 阶段② 串行基线（不计入时间）
│   └─ axpy(x, y, n, a)                 [axpy_cuda.c:L82] → 定义 [L42-L48]
├─ 阶段③④ 计时循环 ×10                  [axpy_cuda.c:L87-L89]
│   └─ axpy_cuda(x, y_cuda, n, a)       [axpy_cudakernel.cu:L52]
│       ├─ cudaMalloc d_x, d_y          [L54-L55]  ┐
│       ├─ cudaMemcpy H2D ×2            [L57-L58]  ├ 段①②：显存准备
│       ├─ warmingup  <<<⌈n/256⌉,256>>> [L61-L62]  ← 阶段③：warmup（每轮都执行）
│       ├─ 1perThread <<<⌈n/256⌉,256>>> [L63-L64]  ┐
│       ├─ block      <<<1024,256>>>    [L65-L66]  ├ 阶段④：被测 kernel，逐个同步
│       ├─ cyclic     <<<1024,256>>>    [L67-L68]  ┘
│       ├─ cudaMemcpy D2H (y_cuda ← d_y)[L70]      ← 结果回主机
│       └─ cudaFree d_x, d_y            [L71-L72]
└─ 阶段⑤ check 校验与输出
    ├─ check(y_cuda, y, n)              [axpy_cuda.c:L91] → 定义 [L51-L60]
    └─ printf checksum + 平均时间        [axpy_cuda.c:L92]
```

画图的三个检验标准：(a) 每个箭头都能对应一行真实代码；(b) 五个阶段各有落点；(c) 能从图上直接回答「一次程序运行中 `axpy_cudakernel_1perThread` 总共执行几次」（10 次）。

### 5.2 对照 MemAlign 验证同构性

逐文件比对 [MemAlign/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c)、[MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu)、[MemAlign/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy.h) 与 CoMem_AXPY 三件套，填写类似下面的对照表：

| 比对项 | CoMem_AXPY | MemAlign | 是否同构 |
| --- | --- | --- | --- |
| `read_timer_ms` / `init` / `zero` / `check` 定义 | L14-L60 | L14-L60 | ✅ 逐字符相同 |
| `axpy.h` 内容 | L6-L14 | L6-L14 | ✅ 逐字符相同 |
| `main` 五阶段顺序 | L62-L98 | L62-L97 | ✅ 相同 |
| 串行基线调用方式 | 1 次（[L82](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L82)） | 循环 10 次（[L84](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L84)），均在计时外 | ⚠️ 微差 |
| 串行循环起点 | `i = 0`（[L44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L44)） | `i = 1`（[L44](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cuda.c#L44)），跳过首元素 | ⚠️ 微差 |
| kernel 数量与命名 | 4 个（warmingup/1perThread/block/cyclic） | 3 个（1perThread/1perThread_misaligned/1perThread_warmup） | ⚠️ 换了「被测变量」 |
| kernel 越界保护 | `if (i < n)` | `if (i > 0 && i < n)` 等，跳过边界元素（如 [MemAlign/axpy_cudakernel.cu:L13](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L13)） | ⚠️ 微差 |
| `axpy_cuda` 五段式 | cudaMalloc→H2D→kernel×4→D2H→Free | cudaMalloc→H2D→kernel×3→D2H→Free | ✅ 结构相同 |

### 5.3 从对照表中得出结论

把比对结果写成两三句结论，参考表述：

1. **骨架同构成立**：计时器、数据初始化、check、`main` 流程、`axpy_cuda` 五段式在两个基准间完全一致——它们是复制同一模板得到的。
2. **差异全部集中在「被测变量」上**：MemAlign 把 kernel 换成对齐/非对齐一对，并让所有版本（含串行）跳过首元素附近，使错位版本与参照版本可在同一初始数据下公平比较——这正是微基准「只改一个因素」原则的体现（MemAlign 的深入分析见 u4-l4）。
3. **串行基线的调用次数（1 次 vs 10 次）是不影响结论的自由度**：两处都在计时窗口之外，只影响串行版自身是否被「预热」，提示我们读代码时要区分「骨架必需」与「作者习惯」。

**交付物**：一张调用图（5.1）、一张对照表（5.2）、一段结论（5.3）。全程无需 GPU；若想加一步运行验证，可在有 GPU 的机器上分别运行两个基准并核对输出格式一致（`axpy(n): checksum: ..., time: ...ms`）——待本地验证。

## 6. 本讲小结

- CUDAMicroBench 的绝大多数基准共享一个**三件套骨架**：host 主程序 `.c`（实验控制器）、kernel 文件 `.cu`（GPU 实现 + 主机包装函数）、接口头文件 `.h`（两者契约），外加 `Makefile` 与 `test.sh`。
- host 主程序按**五阶段**组织：数据初始化（固定种子可复现）→ 串行基线（计时外）→ warmup（消化首次启动开销）→ 计时循环（10 次取平均，host 墙钟口径）→ check 校验（归一化差值 \(\sum|A_i-B_i| / \sum|B_i|\)）。
- `.cu` 中的 `axpy_cuda` 包装函数是**五段式**：`cudaMalloc` → H2D 拷贝 → 逐个启动 kernel（各跟一次 `cudaDeviceSynchronize`）→ D2H 拷贝 → `cudaFree`。
- `axpy.h` 里的 `extern "C"` 解决 nvcc「`.c` 按 C、`.cu` 按 C++ 编译」带来的符号名不一致；`REAL` 宏是精度开关，但它在 `.h` 与 `.c` 里各定义一次，修改须两处同步。
- `Common/` 是 CUDA Samples helper 头文件的本地副本，实际服务 GSOverlap、TaskGraph、Shuffle（两个子目录）、Conkernels；自研基准零依赖；Conkernels 的 `-I../../Common` 路径疑多一级。
- 读基准的正确姿势：先沿 `main → 包装函数 → kernel` 画出数据流与控制流，再解读输出——CoMem_AXPY 的 checksum 不是浮点误差量级，因为四个 kernel 在同一 `d_y` 上叠加了修改，这只有读代码才能预见。

## 7. 下一步学习建议

- **下一讲（u1-l4）**：学会用 nvprof 把五阶段中的 GPU 部分拆开看——哪些时间属于 memcpy、哪些属于每个 kernel，并读懂仓库自带的 `.output.txt` 结果文件。
- **进入单元二前**：把本讲的调用图再走一遍，确认你能不看书说出「一次 `./axpy_cuda 1024000` 运行中，`init`、串行 `axpy`、warmingup、被测 kernel、`check` 各执行多少次」。
- **单元二（u2-l1 起）**将深入 kernel 内部：`blockIdx/threadIdx/blockDim` 的线程索引模型、`<<<grid, block>>>` 的含义，以及 1perThread/block/cyclic 三种划分的细节；届时回看 [axpy_cudakernel.cu:L24-L50](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L24-L50) 会更有感觉。
- **延伸阅读**：对照 [UniMem/LowAccessDensityTest_omp.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/UniMem/LowAccessDensityTest_omp.c) 观察骨架如何容纳「OpenMP 串行对照版」，体会 `.h` 契约让计算后端可替换的设计；以及 [Shuffle/cuda_shuffle/reduction.cpp](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/Shuffle/cuda_shuffle/reduction.cpp) 这类源自 CUDA Samples 的代码在骨架上与本讲模板的差异（u6-l1 的伏笔）。
