# GPU fp32 legacy 版：CUDA 入门

## 1. 本讲目标

在前几讲里，我们已经把 GPT-2 的每一层在纯 C 的 CPU 参考 `train_gpt2.c` 里读懂了：encoder、LayerNorm、matmul、attention、GELU、残差、softmax/交叉熵，以及前向/反向/AdamW 的组装。本讲要做一次「跨语言搬家」——看看同样的算法如何改写成能在 NVIDIA GPU 上跑的 CUDA 代码。

我们把切入点选在 `train_gpt2_fp32.cu`，也就是仓库里的 **fp32 legacy 版**。它在 u1-l1 里被定位为「更简单、冻结的 CUDA 学习入口」：它只做 fp32、不支持混合精度与多卡，因此代码直白，是理解「CPU 算子 → CUDA kernel」最干净的一份参照。

学完本讲，你应当能够：

1. 说清 CUDA 编程的基本模型：host/device 分工、`__global__` 核函数、grid/block/thread 的层级，以及 kernel 启动语法 `<<<grid, block>>>`。
2. 掌握把一个 \((B,T,C)\) 张量上「逐元素」或「逐行」的计算并行化的标准套路：`int idx = blockIdx.x * blockDim.x + threadIdx.x; if (idx < N) {...}`，并能算出给定 \(N\) 时的 grid/block 配置。
3. 理解 GPU 训练必须面对的「显存管理与数据搬运」：`cudaMalloc`/`cudaMemset`/`cudaMemcpy`/`cudaFree`，以及 H2D（host→device）与 D2H（device→host）各自的用途与时机。
4. 把 CPU 参考里的每一层，对应到 fp32 legacy 版里的某一种 CUDA 实现策略（手写元素级 kernel、手写分块/归约 kernel、或调用 cuBLAS），并理解为什么有的层要 `atomicAdd`、有的层要复用 scratch 缓冲。

## 2. 前置知识

在进入 CUDA 之前，先用三句话建立直觉，本讲不会重复它们：

- **GPU 是一个超大规模的「并行计算协处理器」。** CPU 的强项是少量复杂任务、低延迟；GPU 的强项是把成千上万个简单任务同时丢给成千上万个轻量线程去做。一个 \((B,T,C)\) 张量上有几百万个元素，恰好是 GPU 喜欢的「embarrassingly parallel」（天然并行）场景。
- **CUDA 是 NVIDIA 提供的 C/C++ 扩展**，让你写一段叫 kernel（核函数）的代码，然后告诉 GPU「启动 N 个线程去并行执行它」。本讲的 `train_gpt2_fp32.cu` 就是把 `train_gpt2.c` 里那些 `for` 循环，逐个翻译成 kernel。
- **host 与 device 的内存是分离的。** CPU（host）不能直接读写 GPU 显存（device），反之亦然。因此 GPU 训练比 CPU 训练多出一类工作：在两边之间搬运数据。这是本讲的核心痛点之一。

另外，本讲默认你已经读过：

- **u2-l5（GELU 与残差连接）**：我们会反复用 `residual_forward`、`gelu_forward`、`gelu_backward` 作为「最简单的对照层」。
- **u3-l1（反向组装 gpt2_backward）**：我们会对比 CPU 与 CUDA 两版的反向组装，需要你理解反向是前向的严格逆序、残差梯度靠 `+=` 累加。

几个本讲反复用到的术语：

| 术语 | 含义 |
|------|------|
| **kernel（核函数）** | 用 `__global__` 标记、在 GPU 上被大量线程并行执行的函数 |
| **thread（线程）** | 执行 kernel 的最小单位，拥有自己的 `threadIdx` |
| **block（线程块）** | 一组线程，拥有共同的 `blockIdx` 与共享内存 |
| **grid（网格）** | 一次 kernel 启动的全部 block 的集合 |
| **H2D / D2H** | `cudaMemcpy` 的方向：host→device / device→host |
| **scratch（暂存缓冲）** | 不需要长期保存、用完即弃的临时显存 |

## 3. 本讲源码地图

本讲只围绕两个文件展开：

| 文件 | 角色 | 本讲如何使用 |
|------|------|-------------|
| [train_gpt2_fp32.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu) | **CUDA fp32 legacy 版**，本讲主角 | 全部 kernel、launcher、显存管理、训练主循环都在这里 |
| [train_gpt2.c](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c) | **纯 C/CPU 参考版** | 作为「搬家前的原点」，逐层对照 |

`train_gpt2_fp32.cu` 自上而下大致分四段，本讲都会碰到：

1. **CUDA 工具层（L36–L65）**：`CEIL_DIV` 宏、`cudaCheck`/`cublasCheck` 错误检查。
2. **所有 kernel（L67–L687）**：从 `encoder_forward_kernel3` 到 `matmul_forward_kernel4`，每一层的前向/反向核函数。
3. **kernel launcher（L690–L886）**：每个 kernel 的 host 端封装，负责算 grid/block 并启动。这是「CPU 风格的函数签名」与「GPU kernel」之间的桥。
4. **模型定义与主循环（L888–L1754）**：`GPT2Config`/`ParameterTensors`/`ActivationTensors`、`gpt2_build_from_checkpoint`、`gpt2_forward`/`gpt2_backward`/`gpt2_update`、`main`。

> 小提醒：fp32 legacy 版对应的 Makefile 构建目标是 `train_gpt2fp32cu`（见 [Makefile:276](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L276)，规则是 `train_gpt2fp32cu: train_gpt2_fp32.cu`）。注意目标名里没有下划线，而源文件名里有。

---

## 4. 核心概念与源码讲解

### 4.1 kernel 与 grid/block 划分

#### 4.1.1 概念说明

CUDA 的并行模型是一个三层结构：一次 kernel 启动会创建一个 **grid（网格）**，grid 由若干 **block（线程块）** 组成，每个 block 又由若干 **thread（线程）** 组成。每个线程都能通过两个内建变量知道自己是谁：

- `threadIdx.x`：自己在所在 block 内的编号。
- `blockIdx.x`：自己所在 block 在整个 grid 内的编号。
- `blockDim.x`：每个 block 有多少线程。
- `gridDim.x`：grid 里有多少 block。

于是「全局线程号」几乎总是这样算：

\[
\text{idx} = \text{blockIdx.x} \times \text{blockDim.x} + \text{threadIdx.x}
\]

这就是把一个长度为 \(N\) 的任务并行化的标准做法：开足够多的线程，让第 `idx` 号线程负责第 `idx` 号元素。当 \(N\) 不能被 `block_size` 整除时，最后一个 block 里会有「多余」的线程，它们的 `idx >= N`，需要一个 **线程守卫（thread guard）** `if (idx < N)` 把它们挡住，否则会越界写显存。

`block_size`（每块线程数）通常取 128/256/512，是 GPU 硬件调度（warp，每 32 线程一组）的好倍数。`grid_size`（块数）则由总元素数 \(N\) 决定：

\[
\text{grid\_size} = \text{CEIL\_DIV}(N,\ \text{block\_size}) = \left\lceil \frac{N}{\text{block\_size}} \right\rceil
\]

#### 4.1.2 核心流程

把一个 CPU 的逐元素 `for` 循环改写成 CUDA kernel，几乎是一个机械的「四步模板」：

1. **写 kernel**：用 `__global__` 声明，函数体第一行算 `idx`，第二行 `if (idx < N)` 守卫，第三行把原来 `out[i] = ...` 里的 `i` 换成 `idx`。
2. **选 block_size**：取一个常数（如 256），保证是 32 的倍数。
3. **算 grid_size**：`CEIL_DIV(N, block_size)`。
4. **启动并查错**：`kernel<<<grid_size, block_size>>>(args...)`，紧跟一句 `cudaCheck(cudaGetLastError())`。

这个模板对**元素级（elementwise）**算子——每个输出元素只依赖同位置的一两个输入元素——几乎零改动就能套用。`residual`、`gelu`、`encoder` 前向都属于这一类。

#### 4.1.3 源码精读

**`CEIL_DIV` 与错误检查宏**。整个文件的 grid/block 计算都依赖一个向上取整宏：

[train_gpt2_fp32.cu:39-40](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L39-L40) —— 定义 `CEIL_DIV(M, N) = (M + N - 1) / N`，即整数向上取整。

[train_gpt2_fp32.cu:43-50](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L43-L50) —— `cudaCheck`：一旦 CUDA API 返回非成功值就打印并 `exit`，宏版自动带上 `__FILE__`/`__LINE__`。每个 launcher 启动 kernel 后都会调一次 `cudaCheck(cudaGetLastError())`，捕捉启动期的错误。

**最干净的例子：残差前向**。先看 CPU 版（u2-l5 已讲过），一个再普通不过的 `for`：

[train_gpt2.c:436-440](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L436-L440) —— CPU 版 `residual_forward`：单层 `for (i=0; i<N; i++) out[i]=inp1[i]+inp2[i]`，串行。

再看 CUDA 版的 kernel 与 launcher：

[train_gpt2_fp32.cu:302-307](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L302-L307) —— `residual_forward_kernel`：第 303 行算 `idx`，第 304 行守卫 `if (idx < N)`，第 305 行把 CPU 的 `out[i]=inp1[i]+inp2[i]` 原样写出（`__ldcs` 是「流式读」的 cache 提示，不影响语义）。**除了 `i` 换成 `idx`、多了守卫，计算公式一字不差。**

[train_gpt2_fp32.cu:785-790](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L785-L790) —— `residual_forward` launcher：`block_size=256`，`grid_size=CEIL_DIV(N, 256)`，`<<<grid_size, block_size>>>` 启动，再查错。这就是「四步模板」的完整现身。

**GELU：同样的模板，公式更长一点**。

[train_gpt2.cu:408-415](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L408-L415) —— CPU 版 `gelu_forward`：tanh 近似 GELU，逐元素。

[train_gpt2_fp32.cu:310-317](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L310-L317) —— CUDA 版 `gelu_forward_kernel`：第 311 行 `i = blockIdx.x*blockDim.x + threadIdx.x`，第 312 行守卫，第 313–315 行公式与 CPU 版逐字相同。

[train_gpt2_fp32.cu:792-797](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L792-L797) —— `gelu_forward` launcher：`block_size=128`，`grid_size=CEIL_DIV(N,128)`。注意 GELU 在 MLP 里作用于 \(B \times T \times 4C\) 个元素（见 4.3），这里的 `N` 就是这个大数。

**一个「向量化」变体：encoder 前向**。当元素级计算同时又是访存密集型时，可以让每个线程一次处理 4 个 float（用 `float4`，触发 128 位宽的显存读写指令）：

[train_gpt2_fp32.cu:76-90](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L76-L90) —— `encoder_forward_kernel3`：`C4 = C/4`，把 \(B \times T \times C\) 的输出看成 \(B \times T \times C4\) 个 `float4`，每个线程取一组、把 `wte[ix]+wpe[t]` 相加。它本质仍是「逐元素相加」，只是元素粒度从 1 个 float 变成 4 个。

[train_gpt2_fp32.cu:693-702](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L693-L702) —— `encoder_forward` launcher：注意 grid 是 `CEIL_DIV(N/4, block_size)`，因为任务被压缩成了 \(N/4\) 份。`assert(C % 4 == 0)`（C=768 满足）保证可整除。

#### 4.1.4 代码实践

**实践目标**：亲手验证「元素级 kernel 的并行映射」，把 CPU 串行循环与 CUDA 网格对上号。

**操作步骤**：

1. 打开 [train_gpt2_fp32.cu:785-790](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L785-L790)（`residual_forward` launcher）和 [train_gpt2_fp32.cu:302-307](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L302-L307)（kernel）。
2. 在 `gpt2_forward` 里，残差前向以 `N = B*T*C` 调用（见 [train_gpt2_fp32.cu:1278](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1278) 与 [:1283](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1283)）。
3. 取默认 `B=4, T=1024, C=768`，手算 `N = 4*1024*768 = 3,145,728`。
4. 用 `block_size=256` 算 `grid_size = CEIL_DIV(3145728, 256) = 12,288` 个 block，总线程数 `= 12288*256 = 3,145,728 = N`，恰好一人一个元素。

**需要观察的现象**：CPU 版（[train_gpt2.c:436-440](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L436-L440)）是一个 `i` 从 0 走到 \(N-1\) 的单线程串行循环；CUDA 版是 \(N\) 个线程各算一个 `idx`，二者输出的每个元素在数学上完全相同，区别只在于「串行」与「大规模并行」。

**预期结果**：数值一致；CUDA 版只要 GPU 空闲资源足够，会比 CPU 版快几个数量级。

**若无法在本机运行 GPU**：本实践的 grid/block 算术是纯整数计算，可直接在纸上验证；「CUDA 版确实更快」这一步需要 GPU，标注为**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`gelu_forward` launcher 用 `block_size=128`，若 `N = B*T*4C = 4*1024*3072 = 12,582,912`，grid_size 是多少？最后一个 block 是否满员？

> **答案**：`grid_size = CEIL_DIV(12582912, 128) = 98,304`。由于 \(12582912\) 能被 \(128\) 整除，最后一个 block 恰好满 128 线程，没有「多余线程」，守卫 `if (i < N)` 不会被触发。

**练习 2**：为什么 kernel 体内几乎总要写 `if (idx < N)`，而 launcher 里已经用 `CEIL_DIV` 算了 grid_size？

> **答案**：`CEIL_DIV` 只保证 grid 的线程总数「不少于」\(N\)，无法保证恰好等于 \(N\)。当 \(N\) 不被 `block_size` 整除时，最后那个 block 会多出 `block_size - (N % block_size)` 个线程，它们的 `idx >= N`；没有守卫就会越界访问显存。所以守卫是「向上取整」必然带来的安全网。

**练习 3**：encoder 前向为什么用 `CEIL_DIV(N/4, block_size)` 而不是 `CEIL_DIV(N, block_size)`？

> **答案**：因为 `encoder_forward_kernel3` 用 `float4` 让每个线程一次处理 4 个 float，任务粒度从 \(N\) 变成 \(N/4\)。grid 必须按实际任务份数 \(N/4\) 来算，否则会多启动 4 倍的线程。

---

### 4.2 显存管理与数据搬运

#### 4.2.1 概念说明

GPU 训练和 CPU 训练最显眼的区别，不是「算法变了」，而是「数据要搬家」。CPU 的 `malloc`/`free`/`memset` 直接操作内存；GPU 则需要一套平行的 API：

| CPU | CUDA | 作用 |
|-----|------|------|
| `malloc` | `cudaMalloc` | 在 device（显存）上分配 |
| `free` | `cudaFree` | 释放显存 |
| `memset` | `cudaMemset` | 把显存清零 |
| （指针直接读写） | `cudaMemcpy` | 在 host 与 device 之间拷贝，需指定方向 |

`cudaMemcpy` 的第四个参数指定方向，最常用的两种：

- `cudaMemcpyHostToDevice`（**H2D**）：把 CPU 上的数据送进显存，例如模型参数、输入 token。
- `cudaMemcpyDeviceToHost`（**D2H**）：把显存里的结果取回 CPU，例如损失值、待采样的 logits。

还有一类是 **pinned memory（页锁定内存）**，用 `cudaMallocHost` 分配，DMA 传输更快，本文件用它存 `cpu_losses`。

核心约束：**kernel 只能读写显存里的指针**。所以每次训练步开始前，必须先把本批的 `inputs/targets` 从 CPU 搬到 GPU；每次想「在 CPU 上看到结果」（打印 loss、采样 token）时，又必须把结果从 GPU 搬回 CPU。这种来回搬运是 GPU 训练的基本开销。

#### 4.2.2 核心流程

fp32 legacy 版里，显存与数据的生命周期如下：

1. **加载检查点**（`gpt2_build_from_checkpoint`）：先在 CPU 上 `malloc` 一块缓冲读入权重，再 `cudaMemcpy` H2D 把整块权重灌进显存。
2. **首次前向懒分配激活**（`gpt2_forward`）：因为激活张量大小依赖运行时的 \(B,T\)，所以延迟到第一次前向才 `cudaMalloc`，并 `cudaMalloc` 出 `inputs/targets` 显存。
3. **每步前向**：`cudaMemcpy` 把本批 `inputs/targets` H2D。
4. **前向结束后取损失**：`cudaMemcpy` 把 `losses` D2H，在 CPU 上累加求 `mean_loss`。
5. **采样生成**：`cudaMemcpy` 把当前位置的 logits D2H，在 CPU 上跑多项采样。
6. **每步反向前**：`cudaMemset` 把梯度缓冲清零（`gpt2_zero_grad`）。
7. **首次更新懒分配优化器状态**（`gpt2_update`）：`cudaMalloc` 出 m/v，`cudaMemset` 清零。
8. **退出**（`gpt2_free`）：逐个 `cudaFree`。

可以看到，「懒分配」的工程技巧（u1-l3 讲过）从 CPU 版完整继承到了 CUDA 版——只不过把 `malloc`/`calloc` 换成了 `cudaMalloc`/`cudaMemset`。

#### 4.2.3 源码精读

**统一的显存分配函数**。所有激活与梯度缓冲都走同一个工具函数，它把 CPU 版的 `malloc` 换成了 `cudaMalloc`：

[train_gpt2_fp32.cu:1053-1066](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1053-L1066) —— `malloc_and_point`：先累加所有张量大小得到 `num_activations`，再 `cudaMalloc` 一整块显存，最后用指针排布把各张量「钉」进不同偏移（与 CPU 版「一次性 malloc + 指针排布」同一套技巧，见 u1-l3）。

参数内存则多了一个 `on_device` 开关：

[train_gpt2_fp32.cu:945-971](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L945-L971) —— `malloc_and_point_parameters`：`on_device=1` 时 `cudaMalloc`（参数在显存），`=0` 时 `mallocCheck`（CPU 上）。这层抽象让同一套排布逻辑既能服务 GPU 参数，也能服务 CPU 侧的临时读入缓冲。

**检查点加载：典型的「读到 CPU，再 H2D」**。

[train_gpt2_fp32.cu:1152-1155](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1152-L1155) —— `gpt2_build_from_checkpoint` 的关键三行：先 `mallocCheck` 一块 CPU 缓冲 `params_memory_cpu`，`freadCheck` 把权重从文件读进来，`cudaMemcpy(..., cudaMemcpyHostToDevice)` 整块灌进显存，最后 `free` 掉 CPU 缓冲。**注意：权重只搬一次，之后整个训练都常驻显存。**

**每步前向：搬运本批数据**。

[train_gpt2_fp32.cu:1224-1227](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1224-L1227) —— `gpt2_forward` 把 `inputs`（以及非空时的 `targets`）从 host 拷到 device。dataloader 产出的 `inputs/targets` 在 CPU 上（见 u1-l4），kernel 不能直接用，必须每步 H2D。

**前向后取损失：典型的 D2H**。

[train_gpt2_fp32.cu:1291-1301](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1291-L1301) —— 前向用融合分类器算出每个位置的 `losses`（在显存），随后 `cudaMemcpy` 把这 \(B \times T\) 个 loss D2H 到 `cpu_losses`，再用一个 CPU `for` 循环累加、除以 \(B*T\) 得到标量 `mean_loss`。**为什么要在 CPU 上求平均？因为 `mean_loss` 要拿来 `printf`、判定是否 `-1.0f`（哨兵）、写日志，这些都是 CPU 侧的行为。**

**采样时：再把 logits D2H**。

[train_gpt2_fp32.cu:1700-1704](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1700-L1704) —— `main` 的采样循环里，取 `probs[0, t-1, :]` 对应的 logits 指针，`cudaMemcpy` 把 \(V\) 个 logits D2H 到 `cpu_logits`，然后在 CPU 上跑 `sample_softmax` 多项采样。**采样放 CPU 是因为 RNG 和采样逻辑很简单，没必要再写一个 kernel。**

**清零梯度与优化器状态**。

[train_gpt2_fp32.cu:1309-1312](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1309-L1312) —— `gpt2_zero_grad`：两条 `cudaMemset(..., 0, ...)` 分别清零激活梯度与参数梯度。这是 u3-l1 讲过的 `+=` 累加约定的配套：不清零则梯度跨步累积、训练发散。

[train_gpt2_fp32.cu:1442-1448](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1442-L1448) —— `gpt2_update` 首次调用时懒分配 AdamW 的 m/v 状态：`cudaMalloc` 后紧跟 `cudaMemset` 清零（对应 CPU 版的 `calloc`，见 u3-l2）。

**退出释放**。

[train_gpt2_fp32.cu:1461-1471](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1461-L1471) —— `gpt2_free`：逐个 `cudaFree`，pinned 内存用 `cudaFreeHost`。

#### 4.2.4 代码实践

**实践目标**：说清「前向之后那两次 `cudaMemcpy` 分别在做什么、为什么要搬」，理解 GPU 训练里 D2H 的必要性。

**操作步骤**：

1. 打开 [train_gpt2_fp32.cu:1291-1301](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1291-L1301)（前向后取 loss）和 [train_gpt2_fp32.cu:1700-1704](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1700-L1704)（采样取 logits）。
2. 回答两个问题：
   - 训练步里，`mean_loss` 是怎么得到的？为什么必须 D2H？
   - 采样步里，为什么只搬 \(V\) 个 logits 而不是 \(V_p\) 个？

**需要观察的现象**：两次都是 `cudaMemcpyDeviceToHost`，且紧接着的 `for`/采样逻辑都跑在 CPU 上。

**预期结果**：

- **取 loss**：融合分类器把每个位置的标量 loss 写进显存的 `acts.losses`；要在 CPU 上求和取平均得到 `mean_loss`（用于打印、日志、哨兵判断），就必须把这 \(B \times T\) 个 float D2H。
- **取 logits**：采样只关心真实词表 \(V=50257\)（u2-l6 讲过填充区 `[V,Vp)` 不采样），所以只搬 `vocab_size` 个 logits，省下搬运填充区的带宽。
- 反观参数与每步的 `inputs/targets`，方向是 H2D：参数在检查点加载时一次性搬入并常驻，`inputs/targets` 每步搬一次。

**若无法在本机运行**：结论可直接从源码读出，无需运行；若想实测 D2H 耗时占比，标注为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不把 `mean_loss` 的求和也写成一个 reduction kernel 在 GPU 上算，省掉一次 D2H？

> **答案**：完全可以，而且主线 `train_gpt2.cu` 里就有更激进的融合。fp32 legacy 版为了「简单可读」选择在 CPU 上求和：\(B \times T\) 个 float 的 D2H 量很小（默认 \(4 \times 1024 \times 4\) 字节 = 16 KiB），开销可忽略，却省得再写一个归约 kernel。这是「教学版」与「性能版」的取舍。

**练习 2**：`gpt2_build_from_checkpoint` 里 `params_memory_cpu` 在 `cudaMemcpy` 之后立刻被 `free`，这样安全吗？

> **答案**：安全。`cudaMemcpy(..., HostToDevice)` 是同步拷贝（默认流上），调用返回时数据已写入显存，CPU 缓冲的使命完成，即可释放。权重此后常驻显存的 `model->params_memory`，不再依赖 CPU 副本。

**练习 3**：`gpt2_zero_grad` 用 `cudaMemset` 而不是写一个「置零 kernel」，为什么？

> **答案**：`cudaMemset` 是 runtime 提供的高度优化、专门的显存填充 API，比手写 kernel 更简洁也通常更快。清零是「无脑填 0」的操作，没有任何计算逻辑，正好用它。

---

### 4.3 各层前向/反向的 CUDA 化

#### 4.3.1 概念说明

把 CPU 的每一层搬到 GPU，并不是「一个模板套到底」。fp32 legacy 版实际用了**三种策略**，按「该层计算的特点」择优：

1. **手写元素级 kernel**：每个输出只依赖同位置输入，直接套 4.1 的模板。代表：`residual`、`gelu`、`encoder`（前向）、`adamw`。
2. **手写分块 / 归约 kernel**：计算里含跨维度的归约（求均值方差）或矩阵分块累加，需要共享内存与 warp 协作。代表：`layernorm`（warp reduce）、`matmul` 前向（tile）、`matmul` 的 bias 归约。
3. **直接调 cuBLAS 库**：成熟的高性能线性代数，自己写很难超越。代表：`matmul` 反向（`cublasSgemm`）、`attention` 里的批量矩阵乘（`cublasSgemmStridedBatched`）。

此外还有两个 CUDA 特有的工程要点：

- **`atomicAdd`（原子加）**：当多个线程可能往**同一个地址**写累加时（scatter-add 模式），必须用原子操作避免数据竞争。代表：`encoder_backward`（同一 token id / 同一位置会被多个 `(b,t)` 命中）、`layernorm_backward` 对 `dweight/dbias` 的跨位置归约。
- **scratch 缓冲复用**：GPU 显存金贵，fp32 legacy 版会把「反向不再需要的激活」就地当临时缓冲用，从而少分配显存。代表：`attention_forward` 把 `inp`（QKV）当 `preatt` 的 scratch。

理解了这三策略加两要点，你就能把 CPU 版的每个 `*_forward`/`*_backward` 对应到 CUDA 版的具体实现。

#### 4.3.2 核心流程

下表把 fp32 legacy 版里每一层、每种策略一一对齐（CPU 函数 → CUDA 策略 → 关键位置）：

| 层 | CPU 版函数 | CUDA 版策略 | 关键源码位置 |
|----|-----------|------------|-------------|
| encoder 前向 | `encoder_forward` | 手写元素级（float4 向量化） | kernel [:76-90](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L76-L90) / launcher [:693-702](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L693-L702) |
| encoder 反向 | `encoder_backward` | 手写 + `atomicAdd`（scatter-add） | kernel [:93-114](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L93-L114) |
| LayerNorm 前向 | `layernorm_forward` | 手写归约（warp reduce 求 mean/var） | kernel [:116-161](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L116-L161) |
| matmul 前向 | `matmul_forward` | 手写分块（128×128 tile，共享内存） | kernel [:617-687](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L617-L687) |
| matmul 反向 | `matmul_backward` | **调 cuBLAS**（`cublasSgemm`）+ bias 归约 kernel | [:806-822](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L806-L822) |
| attention 前向 | `attention_forward` | permute kernel + **cuBLAS 批量 gemm** + online-softmax kernel + unpermute kernel | [:738-783](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L738-L783) |
| GELU 前/反向 | `gelu_forward/backward` | 手写元素级 | [:310-331](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L310-L331) |
| 残差前向 | `residual_forward` | 手写元素级 | [:302-307](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L302-L307) |
| 分类器（softmax+CE+反向融合） | CPU 三步分立 | 手写融合 kernel（`fused_classifier3`） | [:878-886](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L878-L886) |
| AdamW 更新 | `gpt2_update` | 手写元素级（`adamw_kernel2`） | [:497-513](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L497-L513) |

一个值得记住的不对称：**matmul 前向是手写分块 kernel，而 matmul 反向却调 cuBLAS**。这说明作者并非「一刀切」，而是哪条路在这个教学版里更合适就走哪条。

#### 4.3.3 源码精读

**策略 1 的极致：GELU/残差反向也是同一模板**。前向已在 4.1 看过，反向同理——CPU 的 `gelu_backward`（[train_gpt2.c:422-433](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L422-L433)）是个 `for`，CUDA 的 `gelu_backward_kernel`（[train_gpt2_fp32.cu:319-331](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L319-L331)）把它套进 `idx + 守卫` 模板，公式逐字一致。唯一的差别是 CUDA 版的 `dinp[i] = local_grad * dout[i]` 用 `=` 而非 `+=`——这正是文件顶部注释（[train_gpt2_fp32.cu:5-12](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L5-L12)）所说的优化：非残差流的激活梯度只写不累加，更快。

**`atomicAdd` 为什么不可或缺：encoder 反向**。CPU 版（[train_gpt2.c:60-76](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L60-L76)）按 `(b,t,i)` 三重循环用 `+=` 把梯度累加回 `dwte[ix]/dwpe[t]`。在 GPU 上，如果不同线程的 `(b,t)` 恰好指向**同一个 token id** 或**同一个位置 t**，它们就会并发写同一个 `dwte`/`dwpe` 地址，产生数据竞争。解决办法是 `atomicAdd`：

[train_gpt2_fp32.cu:93-114](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L93-L114) —— `encoder_backward_kernel`：每个线程负责一个 `(b,t,c)`，算出目标地址后用 `atomicAdd(dwte_ix, *dout_btc)` 与 `atomicAdd(dwpe_tc, *dout_btc)` 原子累加。注释自嘲是「really bad naive kernel」，因为原子操作在高冲突时会串行化，但作为教学版它最直观地对应 CPU 的 `+=`。生产版会用更聪明的归约（见主线 `llmc/encoder.cuh`，u5 会讲）。

**策略 3 的代表：matmul 反向调 cuBLAS**。CPU 的 `matmul_backward`（见 u2-l3）是三重循环求三路梯度；CUDA 版直接把矩阵乘交给 cuBLAS：

[train_gpt2_fp32.cu:806-822](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L806-L822) —— `matmul_backward`：对输入的梯度用 `cublasSgemm(..., beta=&zero, dinp)`（`beta=0` 即 `=` 覆盖写，对应文件顶部「激活梯度用 `=`」的约定）；对权重的梯度用 `beta=&one`（`+=` 累加，对应「参数梯度用 `+=`」）；bias 梯度另起一个列归约 kernel（[:338-371](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L338-L371)）。**`beta` 参数 0/1 的选择，精确编码了 `=` vs `+=` 的语义。**

**scratch 复用：attention 前向**。CPU 版 attention 用单独的 `preatt` 缓冲存原始打分（u2-l4）；CUDA 版为了省显存，把反向不再需要的 QKV 输入就地当 `preatt` 用：

[train_gpt2_fp32.cu:738-783](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L738-L783) —— `attention_forward`：第 741–742 行注释明说 `inp` 反向不需要、拿来当 scratch 覆盖写。流程是：`permute_kernel`（拆 Q/K/V）→ cuBLAS 批量 gemm 算 \(QK^\top\)（结果写进复用的 `preatt`）→ `softmax_forward_kernel5`（online softmax，[:241-300](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L241-L300)）→ cuBLAS 批量 gemm 算 `att @ V` → `unpermute_kernel`。一个函数里同时用了「手写 kernel」和「cuBLAS」，是三种策略混合的典型。

**融合算子：分类器**。CPU 版的 softmax + crossentropy + 融合反向（u2-l6）是三步；CUDA 版把前向 softmax 与「反向第一步」融进一个 kernel：

[train_gpt2_fp32.cu:878-886](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L878-L886) —— `fused_classifier3`：每个 block 处理一个 `(b,t)` 位置，块内归约求 softmax，算出 loss，并直接把 logits 原地改写成 logit 梯度 \(\text{probs} - \text{onehot}\)（u2-l6 推导过的招牌结论）。这种「前向里偷偷干了反向第一刀」的融合，是 GPU 训练省带宽的常见手段。

**AdamW：又一个元素级 kernel**。CPU 的 `gpt2_update`（[train_gpt2.c:1007-1033](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L1007-L1033)，u3-l2 讲过）对每个参数独立更新，天然适合 GPU：

[train_gpt2_fp32.cu:497-513](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L497-L513) —— `adamw_kernel2`：每个线程负责一个参数，公式与 CPU 版完全一致（更新 m/v、偏差修正、解耦权重衰减），只是 `lerp` 用了两次 `fma` 把线性插值压成两次浮点运算（[:493-495](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L493-L495)）。

#### 4.3.4 代码实践

**实践目标**：用一个最简单的层（`residual` 或 `gelu`）完成「CPU 公式 → CUDA kernel → grid/block 映射」的完整闭环，并说清 `cudaMemcpy` 在前向后的角色。

**操作步骤**：

1. 选定 **`gelu_forward`**。对照三处：CPU 公式 [train_gpt2.c:408-415](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L408-L415)、CUDA kernel [train_gpt2_fp32.cu:310-317](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L310-L317)、launcher [train_gpt2_fp32.cu:792-797](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L792-L797)。
2. 在 `gpt2_forward` 里找到 GELU 的调用点 [train_gpt2_fp32.cu:1281](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1281)：`gelu_forward(l_fch_gelu, l_fch, B*T*4*C)`，确认 `N = B*T*4*C`。
3. 写下 grid/block：`block_size=128`，`grid_size=CEIL_DIV(B*T*4*C, 128)`；每个线程读一个 `inp[i]`、写一个 `out[i]`。
4. 回答：GELU 前向**之后**，哪一次 `cudaMemcpy` 真正「紧跟着前向」发生？为什么？

**需要观察的现象**：

- kernel 与 CPU 公式逐字对应，区别仅是 `i→idx` + 守卫。
- GELU 作用在 MLP 的 \(4C\) 升维通道上，因此 `N` 比「残差作用在 \(C\) 通道上」大 4 倍。
- 单看 `gelu_forward` 本身，它前后并不直接触发 `cudaMemcpy`——它的输入 `l_fch`、输出 `l_fch_gelu` 都在显存里，由前序/后序 kernel 串起来。真正「前向后」的搬运发生在**整条前向链的尽头**：`fused_classifier3` 之后，把 `losses` D2H（[:1297](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L1297)）。

**预期结果**：

- grid/block：\(B=4,T=1024,C=768\) 时 \(N=4 \times 1024 \times 4 \times 768 = 12{,}582{,}912\)，`grid_size = 98,304`，每线程算一个 GELU。
- `cudaMemcpy` 在前向后的作用：把本步的损失从显存取回 CPU，好让 `main` 打印 `train loss`、写日志、并作为 `gpt2_backward` 是否可执行的哨兵（`mean_loss == -1.0f` 表示「没前向过」）。

**若无法在本机运行 GPU**：grid/block 算术可手算验证；kernel 数值正确性需 GPU（或对照 `make test_gpt2fp32cu` 的回归测试），标注为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`encoder_backward_kernel` 为什么必须用 `atomicAdd`，而 `encoder_forward_kernel3` 不需要？

> **答案**：前向是 gather（查表读）：每个输出位置 `(b,t)` 读自己的 `wte[ix]`，没有写冲突。反向是 scatter-add（散播累加写）：要把梯度写回 `dwte[ix]`/`dwpe[t]`，而不同 `(b,t)` 可能共享同一个 `ix`（同一个词出现多次）或同一个 `t`（同一个位置），于是多个线程会写同一地址，必须 `atomicAdd` 保证累加正确。

**练习 2**：`matmul_backward` 里对 `dinp` 用 `beta=0`、对 `dweight` 用 `beta=1`，这对应什么语义？

> **答案**：`beta=0` 让 cuBLAS 执行 `dinp = 1.0 * (... )`，即**覆盖写**——对应文件顶部「激活梯度用 `=`」的约定（dinp 是激活梯度，每层重新算，不跨层累加）。`beta=1` 让 cuBLAS 执行 `dweight += 1.0 * (...)`，即**累加**——对应「参数梯度用 `+=`」，配合 `gpt2_zero_grad` 清零后跨层累加。同一个 `cublasSgemm`，靠 `beta` 一参数切换两种语义。

**练习 3**：fp32 legacy 版的 `attention_forward` 为什么不像 CPU 版那样为每一层长期保存 `preatt`？

> **答案**：因为 CUDA 版的 `softmax_autoregressive_backward_kernel`（[:446-489](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L446-L489)）反向时**只需要 `att`（归一化后的权重），不需要原始打分 `preatt`**（与 u2-l4 讲的「softmax 反向不读 preatt」一致）。既然 `preatt` 反向用不上，前向就把它写进一个用完即弃的 scratch 缓冲（复用 QKV 的 `inp`），省下为每一层长期存 \((B,NH,T,T)\) 的显存。这也是 fp32 legacy 版激活张量布局（`NUM_ACTIVATION_TENSORS=21`，[:973](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L973)）与 CPU 版（23 个）不同的主要原因之一。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「以 `residual_forward` 为样本的端到端追踪」任务。

**背景**：残差前向是整个模型里最简单的层，但它完整经历了「数据在 GPU 上、由 kernel 并行算、结果又被后续层消费」的全过程，是理想的样本。

**任务清单**：

1. **并行映射（对应 4.1）**：给定默认 `B=4, T=1024, C=768`，写出 `residual_forward` 一次调用的 `N`、`block_size`、`grid_size`，并说明每个线程算什么。再问：如果某次调用 `N` 不能被 `block_size` 整除，会发生什么？

2. **数据搬运路径（对应 4.2）**：画一张从「检查点文件」到「`mean_loss` 被打印」的数据流，标出每一次 `cudaMemcpy` 及其方向（H2D/D2H）。至少应包含：权重加载、每步 `inputs/targets`、每步 `losses`。指出残差前向本身是否触发搬运。

3. **逐层对照（对应 4.3）**：把 `residual`、`gelu`、`encoder`、`matmul`（前向/反向）、`attention`、`adamw` 填进一张三列表（CPU 函数 / CUDA 策略 / 是否用 `atomicAdd` 或 cuBLAS），并用自己的话解释「为什么 matmul 前向手写、反向却调 cuBLAS」。

**参考答案要点**：

1. \(N = 4 \times 1024 \times 768 = 3{,}145{,}728\)；`block_size=256`；`grid_size=12,288`；每线程 `out[idx]=inp1[idx]+inp2[idx]`。若 `N` 不整除 `block_size`，最后一块多出的线程会被 `if (idx < N)` 守卫挡住。
2. 路径：`gpt2_124M.bin` →（fread 到 CPU 缓冲）→ H2D 进显存常驻；每步 `inputs/targets` H2D；前向链跑完 → `losses` D2H → CPU 求平均 → `printf`。残差前向本身**不**触发搬运，输入输出都在显存。
3. 见 4.3.2 的对照表；matmul 前向手写分块是为了教学展示 tile/共享内存，反向调 cuBLAS 是因为反向需要的两个大 GEMM 用成熟库更省事——这是「教学性」与「实用性」在同一文件里的混合。

> 说明：以上算术与对照均可从源码直接得出；若你想实测「残差前向在不同 `block_size` 下的耗时」或「D2H 在一步训练里的时间占比」，需要 GPU 环境，标注为**待本地验证**。

## 6. 本讲小结

- CUDA 的并行模型是 grid→block→thread 三层，把长度 \(N\) 的任务并行化的标准写法是 `idx = blockIdx.x*blockDim.x + threadIdx.x; if (idx < N) {...}`，grid 用 `CEIL_DIV(N, block_size)` 计算，多余的线程靠守卫挡掉。
- GPU 训练比 CPU 多出「显存管理与数据搬运」：`cudaMalloc/cudaMemset/cudaFree` 管显存，`cudaMemcpy` 按 H2D/D2H 方向搬运；权重 H2D 一次常驻，每步 `inputs/targets` H2D，损失与待采样 logits D2H 回 CPU。
- 每一层搬到 GPU 有三种策略：手写元素级 kernel（gelu/residual/encoder/adamw）、手写分块/归约 kernel（layernorm/matmul 前向）、调 cuBLAS（matmul 反向/attention 的批量 gemm）。
- scatter-add 型的反向（如 encoder 反向）必须用 `atomicAdd` 防数据竞争；cuBLAS 的 `beta` 参数（0=`=`，1=`+=`）精确编码了「激活梯度覆盖写、参数梯度累加」的约定。
- fp32 legacy 版靠 scratch 复用（attention 把 QKV 当 preatt 暂存）与懒分配来省显存，激活张量布局因此与 CPU 版不同（21 vs 23 个）。
- 算法本身没有任何改变：fp32 legacy 版与 CPU 参考数值等价，正确性由 `test_gpt2.cu`（对照 PyTorch debug state）把关——本讲只换了「执行器」，没换「数学」。

## 7. 下一步学习建议

本讲把「CPU 算子 → CUDA kernel」的搬家讲透了，但 fp32 legacy 版是「教学快照」：它只支持单卡 fp32。接下来有两条路：

- **主线推荐：进入 u5（CUDA 主线 train_gpt2.cu 与 llmc 头文件库）**。`train_gpt2.cu` 把本讲里散在一个文件里的 kernel，拆进了 `llmc/` 下一组头文件（`layernorm.cuh`、`matmul.cuh`、`attention.cuh`、`adamw.cuh`……），并支持混合精度、cuBLASLt、cuDNN Flash Attention。建议先读 [u5-l1](u5-l1-cuda-mainline-architecture.md) 建立主线架构地图，再按层对照本讲——你会发现 fp32 legacy 版里「手写的 matmul 前向 kernel」在主线里被 `llmc/matmul.cuh` 的 cuBLASLt 封装取代。

- **若想先吃透单算子的优化演进**：读 `dev/cuda/` 下的内核教学库（如 `dev/cuda/layernorm_forward.cu` 里 kernel1~kernel6 的逐步优化），这正是 [u7-l1](u7-l1-cuda-kernel-library.md) 的内容。它和本讲互补：本讲是「怎么搬」，`dev/cuda` 是「怎么搬得越来越快」。

继续阅读建议：先重读 [train_gpt2_fp32.cu:67-90](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2_fp32.cu#L67-L90) 的几个最简 kernel 巩固模板，再带着本讲的「三策略 + 两要点」框架进入 u5。
