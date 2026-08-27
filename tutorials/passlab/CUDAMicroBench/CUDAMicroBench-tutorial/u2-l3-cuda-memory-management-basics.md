# CUDA 显存管理基础：cudaMalloc、cudaMemcpy 与同步

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立写出一条完整的显存数据生命周期链：`cudaMalloc` 分配 → `cudaMemcpy` 主机到设备（H2D）→ kernel 计算 → `cudaMemcpy` 设备到主机（D2H）→ `cudaFree` 释放。
2. 准确区分 `cudaMemcpyHostToDevice` 与 `cudaMemcpyDeviceToHost`，并能判断一段代码里每次拷贝的方向是否正确。
3. 理解 kernel 启动是**异步**的，以及 `cudaDeviceSynchronize` 到底在等什么、什么时候必须等、什么时候只是为了让测量边界清晰。
4. 掌握用 `cudaGetLastError` + `cudaGetErrorString` 为每一步 CUDA API 调用加上错误检查，并理解「静默失败」的危险。

上一讲（u2-l1）我们把注意力放在 `<<<grid, block>>>` 这一行启动语法上；本讲正好补齐它的「左邻右舍」——`axpy_cuda` 包装函数里**其余的所有行**。这些行在每个微基准里都反复出现，是全项目共用度最高的一块代码。

## 2. 前置知识

### 2.1 两个世界：host 内存与 device 显存

传统（离散）GPU 架构下，CPU 和 GPU 是两颗独立芯片，各自挂在各自的内存上：

- **host（主机）**：CPU 及其内存（DRAM）。`main` 里 `malloc` 出来的 `x`、`y` 指针指向这里。
- **device（设备）**：GPU 及其显存。kernel 里读写的数据必须住在这里。

CPU 不能直接解引用显存地址，GPU 也不能直接解引用主机内存地址（在不开统一内存等机制的前提下）。所以「把数据送进 GPU 算、再把结果取回来」必须显式地写出来——这正是本讲的主角。后续第 u5-l4 讲（UniMem）会介绍 `cudaMallocManaged` 如何模糊这条边界，但先理解「显式搬运」的模型，才能体会统一内存解决了什么。

### 2.2 双重指针：为什么 cudaMalloc 的参数是 `void**`

`cudaMalloc` 的函数签名是：

```c
cudaError_t cudaMalloc(void** devPtr, size_t size);
```

C 语言的函数参数是值传递。如果我们传 `d_x`（一个 `REAL*`）进去，函数内部修改的只是副本，调用方的 `d_x` 不会被赋值。所以要传 `&d_x`（`REAL**`），让 CUDA 运行时把分配到的显存地址**写回**我们的变量。这与 `scanf("%d", &i)` 的道理完全相同。

另外注意返回值：`cudaMalloc` 不像 `malloc` 那样返回指针，而是把错误码作为返回值——这是所有 CUDA Runtime API 的统一风格，也是本讲实践的检查对象。

### 2.3 阻塞与异步：两种调用行为

- **阻塞（同步）调用**：函数返回时，事情已经做完。本讲的 `cudaMemcpy`（不带 `Async` 后缀）对可分页主机内存就是这种语义。
- **异步调用**：函数立即返回，活儿在后台排队干。kernel 启动（`<<<>>>`）就是这种语义——host 把任务提交给 GPU 后撒手不管，直到某个同步点。

「谁在等谁、从什么时候等到什么时候」是本讲反复出现的思维练习。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [CoMem_AXPY/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu) | kernel 定义 + 主机端包装函数 `axpy_cuda` | 全部内存管理代码都在 `axpy_cuda`（L52–L73）里 |
| [CoMem_AXPY/axpy_cuda.c](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c) | host 主程序 | `main` 如何用 `malloc`/`free` 管理主机内存、如何循环调用 `axpy_cuda` |
| [CoMem_AXPY/axpy.h](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy.h) | 接口契约 | `axpy_cuda` 的声明，C/C++ 混编的 `extern "C"` |
| [MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu) | 另一个基准的同名包装函数 | 对照组：生命周期骨架完全同构，证明这是项目级模式 |
| [CoMem_AXPY/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/Makefile) | 编译脚本 | 实践环节重新编译时使用 |

## 4. 核心概念与源码讲解

### 4.1 显存分配与拷贝：cudaMalloc 与 cudaMemcpy

#### 4.1.1 概念说明

这个模块解决的问题是：**数据怎么从 CPU 的世界进入 GPU 的世界，又怎么回来**。

三个 API 各司其职：

1. `cudaMalloc(&d_x, bytes)`：在显存里申请 `bytes` 字节，把显存地址写进 `d_x`。注意它只保证「分配」，**不初始化**，刚分配的显存内容是未定义的垃圾值。
2. `cudaMemcpy(dst, src, bytes, kind)`：在两个世界之间搬运 `bytes` 字节。第四个参数 `kind`（类型为 `cudaMemcpyKind`）说明方向：

   | 常量 | 含义 | 本项目中的用途 |
   |---|---|---|
   | `cudaMemcpyHostToDevice`（H2D） | 主机内存 → 显存 | 把 `x`、`y` 的初值送进 GPU |
   | `cudaMemcpyDeviceToHost`（D2H） | 显存 → 主机内存 | 把计算结果 `d_y` 取回来 |
   | `cudaMemcpyDeviceToDevice` | 显存 → 显存 | 本项目未使用 |
   | `cudaMemcpyHostToHost` | 主机 → 主机（等价 `memcpy`） | 本项目未使用 |

3. `cudaFree(d_x)`：释放显存。与 `malloc`/`free` 配对一样，每个 `cudaMalloc` 都应该有对应的 `cudaFree`。

一个容易忽略的事实：**`cudaMemcpy` 的 `kind` 是你告诉它的，不是它猜的**。运行时只会做有限的指针合法性检查——如果你把方向写反，错误可能被检测到（返回错误码），也可能带着错误的指针组合直接访问非法地址。而本项目的代码**从不检查返回值**，所以任何拷贝失败都是静默的。这是本讲实践要亲手加固的地方。

#### 4.1.2 核心流程

`axpy_cuda` 的内存管理流程可以画成一条单向流水线：

```text
主机内存                          显存
--------                          --------
x[0..n-1]  ──cudaMalloc(d_x)──►  (分配 d_x)
y[0..n-1]  ──cudaMalloc(d_y)──►  (分配 d_y)
           ──memcpy H2D────────►  d_x ← x
           ──memcpy H2D────────►  d_y ← y
                                  [kernel 在 d_x、d_y 上计算]
y[0..n-1]  ◄──memcpy D2H────────  d_y → y
           ──cudaFree(d_x)─────►  (释放)
           ──cudaFree(d_y)─────►  (释放)
```

配合 u2-l1 学过的线程模型，一次调用的数据流量可以估算。设 `n = 1024000`、`REAL = double`（8 字节）：

\[ \text{H2D 流量} = 2 \times n \times \mathtt{sizeof(REAL)} = 2 \times 1024000 \times 8\,\text{B} \approx 15.6\,\text{MiB} \]

再乘上 `main` 里 `num_runs = 10` 次循环，整个程序经 PCIe 送进 GPU 的数据约 156 MiB——而每次 kernel 本身只做 \( n \) 次乘加。这就是 u2-l1 已经指出的结论（此 kernel 访存受限）在**传输侧**的对应版本：搬运本身就要占用可观的墙钟时间。

#### 4.1.3 源码精读

先看分配与 H2D 拷贝：

[CoMem_AXPY/axpy_cudakernel.cu:52-58](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L58)

```c
void axpy_cuda(REAL* x, REAL* y, int n, REAL a) {
  REAL *d_x, *d_y;
  cudaMalloc(&d_x, n*sizeof(REAL));
  cudaMalloc(&d_y, n*sizeof(REAL));

  cudaMemcpy(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice);
  cudaMemcpy(d_y, y, n*sizeof(REAL), cudaMemcpyHostToDevice);
```

- L53：`d_x`、`d_y` 是**设备指针**。它们指向的地址只对 GPU 有意义，host 代码里绝不能出现 `d_x[i]` 这样的解引用。
- L54–L55：两次 `cudaMalloc`，大小都是 `n*sizeof(REAL)`。注意 `sizeof(REAL)` 中的 `REAL` 来自头文件里的 `#define REAL double`——主机侧和设备侧共用同一个宏定义，所以两边算出的字节数必然一致。如果 `.h` 和 `.c` 里的 `REAL` 不一致（u1-l3 提醒过它被定义了两处），这里就会发生主机按 8 字节写、GPU 按 4 字节读的错位。
- L57：第一个参数是**目的**地址 `d_x`（设备），第二个是**源** `x`（主机），方向 H2D——「目的在前，源在后」，与 `memcpy(dst, src, n)` 一致。
- L58：注意这里每轮都把 `d_y` **重新覆盖**为主机侧的 `y` 值，这个细节在 u2-l2 讨论四个 kernel 叠加效应时已经分析过，本讲不再展开。

再看结果取回与释放：

[CoMem_AXPY/axpy_cudakernel.cu:70-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L70-L73)

```c
  cudaMemcpy(y, d_y, n*sizeof(REAL), cudaMemcpyDeviceToHost);
  cudaFree(d_x);
  cudaFree(d_y);
}
```

- L70：方向反过来了——`y`（主机）是目的，`d_y`（设备）是源，D2H。对照 L57，可以总结一个读代码口诀：**看前两个参数谁是主机指针、谁是设备指针，方向参数必须与之匹配**。
- L71–L72：与 L54–L55 的两次分配一一配对释放。

调用方的主机内存管理在另一个文件里，形成对照：

[CoMem_AXPY/axpy_cuda.c:73-75](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L73-L75)

```c
  y_cuda = (REAL *) malloc(n * sizeof(REAL));
  y  = (REAL *) malloc(n * sizeof(REAL));
  x = (REAL *) malloc(n * sizeof(REAL));
```

这里用 `malloc`/`free`（[L95-L97](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L95-L97)）管理主机内存，`axpy_cuda` 内部用 `cudaMalloc`/`cudaFree` 管理显存——两套分配器、两个世界，泾渭分明。

最后验证「这是项目级模式」而不是孤例：

[MemAlign/axpy_cudakernel.cu:33-39](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L33-L39)

```c
void axpy_cuda(REAL* x, REAL* y, int n, REAL a) {
  REAL *d_x, *d_y;
  cudaMalloc(&d_x, n*sizeof(REAL));
  cudaMalloc(&d_y, n*sizeof(REAL));

  cudaMemcpy(d_x, x, n*sizeof(REAL), cudaMemcpyHostToDevice);
```

以及 [MemAlign/axpy_cudakernel.cu:51-53](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L51-L53) 的 D2H 与释放，与 CoMem_AXPY 逐行同构。两个基准唯一本质区别在被测 kernel 的访存方式，显存管理这一层是复制粘贴级别的相同——读懂一处，全项目通用。

还有一处值得指出的工程细节：`main` 的计时循环（[CoMem_AXPY/axpy_cuda.c:87-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L87-L89)）每轮都完整执行一遍分配/拷贝/释放：

```c
  double elapsed = read_timer_ms();
  for (i=0; i<num_runs; i++) axpy_cuda(x, y_cuda, n, a);
  elapsed = (read_timer_ms() - elapsed)/num_runs;
```

性能敏感的真实代码通常会把 `cudaMalloc`/`cudaFree` 挪到循环外复用显存；微基准这样写是为了让每次调用自包含、代码简单，代价是计时里混入了分配与传输开销——这是 u1-l4 已经建立的「墙钟时间 > 纯 kernel 时间」结论的根源之一。

#### 4.1.4 代码实践：给分配与拷贝加上错误检查

**实践目标**：亲手体验 CUDA API 的错误码机制，并制造一次显存分配失败。

**操作步骤**（在你自己检出的工作副本上操作，本讲义不修改仓库源码）：

1. 进入 `CoMem_AXPY` 目录，编辑 `axpy_cudakernel.cu`，在 `#include "axpy.h"` 之后加入一个检查宏（**示例代码**，非项目原有）：

   ```c
   #define CUDA_CHECK(call) do {                                  \
       cudaError_t err_ = (call);                                 \
       if (err_ != cudaSuccess) {                                 \
           fprintf(stderr, "CUDA error at %s:%d: %s\n",           \
                   __FILE__, __LINE__, cudaGetErrorString(err_)); \
           exit(EXIT_FAILURE);                                    \
       }                                                          \
   } while (0)
   ```

2. 把 L54、L55 改成 `CUDA_CHECK(cudaMalloc(&d_x, n*sizeof(REAL)));` 的形式；L57、L58、L70 的 `cudaMemcpy` 同样包裹。

3. 重新编译运行，确认一切正常：

   ```bash
   make
   ./axpy_cuda 1024000
   ```

4. 先用 `nvidia-smi` 查看显存总量，然后把 `n` 设为一个两倍于显存的规模再运行，例如 16 GB 显存的机器上取 `n = 1200000000`（每个向量 \( 1.2\times10^9 \times 8\,\text{B} \approx 8.9\,\text{GiB} \)，两个共约 17.8 GiB）：

   ```bash
   ./axpy_cuda 1200000000
   ```

**需要观察的现象**：

- 步骤 3 中程序照常输出 `checksum: ... time: ...`，没有任何错误打印。
- 步骤 4 中程序在到达出错的那一行时立刻打印类似 `CUDA error at axpy_cudakernel.cu:54: out of memory` 的信息并退出。
- 把 `CUDA_CHECK` 撤掉再跑一次步骤 4：程序**不报错**，但输出一个巨大的 checksum（因为 `d_x`/`d_y` 未被有效赋值，kernel 在读未定义数据）。

**预期结果**：你会直观看到「检查与不检查」的差别——CUDA 运行时从不会主动替你打印错误，不检查就是静默失败。

**待本地验证**：以上运行现象需要在有 NVIDIA GPU 的机器上确认；具体错误文本以你本机的 CUDA 版本输出为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L57 的方向参数误写成 `cudaMemcpyDeviceToHost`，会发生什么？

**参考答案**：此时目的 `d_x` 被声明为设备指针、源 `x` 是主机指针，与 D2H 方向的语义（目的应为主机、源应为设备）矛盾。运行时通常会检测到指针与方向不匹配并返回 `cudaErrorInvalidValue`，拷贝不发生；但原代码不检查返回值，所以失败是静默的——`d_x` 保持未初始化状态，后续 kernel 读到垃圾数据，最终 checksum 巨大。具体行为待本地验证，不同驱动版本的检测严格程度可能不同。

**练习 2**：为什么本项目每个基准里 `x` 向量只需要 H2D 拷贝（L57），从来不需要 D2H？

**参考答案**：查看任一 kernel（如 [CoMem_AXPY/axpy_cudakernel.cu:18-22](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L18-L22)）可知 `x` 只出现在赋值号右边（只读），GPU 对它的更新为零，没有结果需要取回；`y` 既被读又被写（`y[i] += a*x[i]`），所以 L58 送初值进去、L70 把结果取回来。传输方向由数据的读写角色决定。

**练习 3**：如果删掉 L71–L72 的两个 `cudaFree`，程序会怎样？

**参考答案**：程序仍能输出正确结果——显存泄漏不会立刻报错。但 `main` 的计时循环调用 `axpy_cuda` 十次，就泄漏十份 `2×n×8` 字节的显存；泄漏积累到超出显存容量时，后续 `cudaMalloc` 开始失败（又因为没有检查而静默化）。短命进程退出时 CUDA 会回收显存，所以这类问题在小实验里容易被掩盖。

### 4.2 kernel 异步启动与设备同步

#### 4.2.1 概念说明

这个模块解决的问题是：**host 把 kernel「发射」出去之后，凭什么知道它算完了**。

关键事实：`<<<grid, block>>>` 形式的 kernel 启动是**异步**的。这一行执行时，host 只是把「请执行这个 kernel」的命令提交给 GPU 的命令队列，然后**立即执行下一行 host 代码**——此时 kernel 可能还没开始跑。于是产生两个必须回答的问题：

1. **正确性**：L70 的 D2H 拷贝如果跑到 kernel 前面去了，取回的就是旧数据。谁来保证顺序？
2. **测量**：host 的计时秒表什么时候停下来才算数？

答案分两层：

- **默认流（default stream）内部的隐式排序**：本项目所有 kernel 和同步版 `cudaMemcpy` 都提交在默认流里，同一流内的操作按提交顺序依次执行——第二个 kernel 不会插到第一个前面，D2H 拷贝也不会抢在 kernel 之前。所以单看正确性，本项目的代码即使删掉所有 `cudaDeviceSynchronize` 也不会算错。
- **`cudaDeviceSynchronize()` 的显式栅栏**：这个调用阻塞 host，直到设备上**所有先前提交的工作**全部完成才返回。它回答的是「测量」问题：把异步的 GPU 执行变成 host 可感知的时间点。

那么这个微基准为什么在每个 kernel 后面都放一个 `cudaDeviceSynchronize`（[CoMem_AXPY/axpy_cudakernel.cu:62](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L62) 等）？因为四个 kernel 都写同一个 `d_y`，作者要确保它们**严格一前一后**地执行、互不重叠，让每个 kernel 的行为可单独归因——这是微基准「控制变量」思想在同步上的体现。而 u1-l4 讲过，`main` 层面的计时测的是整个 `axpy_cuda` 的墙钟，粒度较粗；kernel 级时间要靠 nvprof 拆分。

最后是错误传播的时序，这是理解下一节实践的前提：

- **启动期错误**（如网格配置非法）：在启动调用返回前后即被记入 host 端的错误状态，紧跟启动调用 `cudaGetLastError()` 就能捕获。
- **执行期错误**（如 kernel 内非法访存）：发生在 GPU 上，host 要等某个同步点（如 `cudaDeviceSynchronize`）之后才能查询到。

`cudaGetLastError()` 的语义是「返回并**清除**自上次查询以来的错误状态」；对应的 `cudaPeekAtLastError()` 只查看不清除。正因为它会清除状态，检查必须紧贴被检查的调用，否则错误可能被别的查询悄悄吞掉。

#### 4.2.2 核心流程

把 `axpy_cuda` 一次调用展开成 host/device 双时间轴：

```text
host 时间轴                                device 时间轴
────────────                              ────────────
cudaMalloc(d_x)   ─┐ 立即返回的 API 调用
cudaMalloc(d_y)   ─┘
cudaMemcpy H2D(x) ───阻塞，直到传输完成──► [PCIe: x → 显存]
cudaMemcpy H2D(y) ───阻塞，直到传输完成──► [PCIe: y → 显存]
launch warmingup  ─┐ 提交即返回            [kernel 1 warmingup 执行…]
cudaDeviceSync    ─┘──阻塞等待───────────►  kernel 1 结束
launch 1perThread ─┐                      [kernel 2 执行…]
cudaDeviceSync    ─┘─────────────────────► kernel 2 结束
launch block      ─┐                      [kernel 3 执行…]
cudaDeviceSync    ─┘─────────────────────► kernel 3 结束
launch cyclic     ─┐                      [kernel 4 执行…]
cudaDeviceSync    ─┘─────────────────────► kernel 4 结束
cudaMemcpy D2H(y) ───阻塞───────────────► [PCIe: 显存 → y]
cudaFree(d_x/d_y) ── 释放
```

用伪代码总结同步语义：

```text
for 每个 kernel k:
    提交 k（异步，立即返回）
    cudaDeviceSynchronize()   # host 在此等待 device 清空
                              # 此后 k 的结果保证可见、k 的执行错误保证可查询
```

#### 4.2.3 源码精读

[CoMem_AXPY/axpy_cudakernel.cu:60-68](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L60-L68)

```c
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

- 四组「启动 + 同步」严格成对出现，节奏完全一致。
- 前两个 kernel 用 `(n+255)/256` 个块（u2-l1 讲过的向上取整技巧），后两个用固定 `1024` 个块（block/cyclic 策略让线程数与 `n` 解耦，u2-l2 已分析）——注意 kernel 的参数 `d_x, d_y` 全部是**设备指针**，这正是 4.1 分配的变量在这里被消费。
- 同一个 `d_y` 被四个 kernel 先后写入，靠同步保证串行叠加顺序。

对照组 MemAlign 同样是三组「启动 + 同步」：

[MemAlign/axpy_cudakernel.cu:41-48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L41-L48)

```c
  //warm up
  axpy_cudakernel_1perThread_warmup<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
  // Perform axpy elements
  axpy_cudakernel_1perThread_misaligned<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
  axpy_cudakernel_1perThread<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
  cudaDeviceSynchronize();
```

模式一模一样，只是被测 kernel 换成了对齐/错位访问两个变体（那是 u4-l4 的主题）。至此可以给出本讲的通用骨架总结——**任何微基准的包装函数都是这段代码的变体**：

```text
声明设备指针 → cudaMalloc → cudaMemcpy H2D
→ 循环{ kernel<<<...>>> ; cudaDeviceSynchronize }
→ cudaMemcpy D2H → cudaFree
```

#### 4.2.4 代码实践：触发并捕获一次 kernel 启动错误

**实践目标**：验证「kernel 启动是异步的、但配置错误会被立即报告」，并观察没有错误检查时启动失败如何静默吞掉。

**操作步骤**（在你自己检出的工作副本上操作）：

1. 延续 4.1.4 的修改（`CUDA_CHECK` 宏已在文件中）。给**每一个** kernel 启动后面加一行检查（**示例代码**，非项目原有）：

   ```c
   axpy_cudakernel_warmingup<<<(n+255)/256, 256>>>(d_x, d_y, n, a);
   CUDA_CHECK(cudaGetLastError());
   cudaDeviceSynchronize();
   ```

   注意：这里检查的对象是 `cudaGetLastError()` 的返回值，不是启动表达式本身——启动语法没有可赋值的返回值，错误状态要通过查询获得。

2. 编译并正常跑一次，确认无错误输出：`make && ./axpy_cuda 1024000`。

3. **只把第 63 行**（`axpy_cudakernel_1perThread` 的启动）中的 `(n+255)/256` 改成 `0`，其他行不动，重新 `make`。

4. 运行 `./axpy_cuda 1024000`，观察输出。

5. 再做一组对照：把 `CUDA_CHECK` 全部注释掉，重复步骤 3–4。

6. （可选）不改代码，直接运行 `./axpy_cuda 0`：此时 `n=0`，`(n+255)/256 = 0`，同样构成零网格启动。

**需要观察的现象**：

- 步骤 4：程序在改过的那一行打印类似 `CUDA error at axpy_cudakernel.cu:64: invalid configuration argument` 的信息后退出。网格维度为 0 违反了 CUDA 对启动配置的硬性要求（grid 各维必须 ≥ 1），属于**启动期错误**，所以在紧跟的 `cudaGetLastError()` 处即被捕获——不需要等 `cudaDeviceSynchronize`。
- 步骤 5：没有任何错误输出，程序正常走完，但 checksum 明显异常（该 kernel 从未执行，`d_y` 少了一轮应有的更新）。
- 步骤 6：零规模输入从另一个路径自然产生零网格。

**预期结果**：错误报告精确落在你改过的行号上；对照实验证明「不检查 = 无声无息地算错」。这正是本讲强调错误检查的原因：CUDA 的设计哲学是把错误处理权完全交给调用者。

**待本地验证**：以上均为预期行为，具体错误文本随 CUDA 版本可能略有差异，请在有 GPU 的环境确认；无 GPU 环境可通过阅读本节的分析理解时序原理。

#### 4.2.5 小练习与答案

**练习 1**：如果把本节四处 `cudaDeviceSynchronize()` 全部删掉，程序的输出会变吗？

**参考答案**：checksum 不会变。所有 kernel 与同步版 `cudaMemcpy` 都在默认流中按提交顺序执行，D2H 拷贝仍会排在四个 kernel 之后，结果正确。变化的是行为语义而非数值结果：host 与 device 的执行进一步重叠，且 kernel 若发生执行期错误，最早也要等到 D2H 拷贝处才可能暴露。微基准保留这些同步是为了让每个 kernel 的执行边界清晰可归因。

**练习 2**：`cudaDeviceSynchronize` 之后还需要 `CUDA_CHECK(cudaGetLastError())` 吗？

**参考答案**：需要，且两者捕获的错误类别不同。紧跟启动的 `cudaGetLastError` 抓启动期错误（如本节实践的零网格）；`cudaDeviceSynchronize` 后再查一次才能抓到执行期错误（如 kernel 内越界访问导致的 `cudaErrorIllegalAddress`，具体名称随 CUDA 版本可能不同）——这类错误只在该同步点之后才进入 host 可见的状态。严谨的写法是两处都查。

**练习 3**：为什么说「kernel 启动是异步的」这件事解释了 u1-l4 观察到的现象——`main` 里测出的 time 远大于单个 kernel 的执行时间？

**参考答案**：`main` 的秒表（[CoMem_AXPY/axpy_cuda.c:87-89](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cuda.c#L87-L89)）量的是**整个 `axpy_cuda` 的墙钟**：显存分配、四次 H2D/D2H 的 PCIe 传输、四次同步等待 host 与 device 互相等待的空转，全部计入。异步启动意味着 host 时间与 device 时间只是部分重叠，墙钟是两条时间轴的总跨度而非 kernel 时长之和，所以它必然大于纯 kernel 时间，后者要靠 nvprof 之类的工具单独拆出。

## 5. 综合实践

把 4.1.4 与 4.2.4 合并成一次完整的加固实验，这就是本讲规格设定的总任务：

**任务**：为 `CoMem_AXPY/axpy_cuda` 的每一步 `cudaMalloc`/`cudaMemcpy`/kernel 启动加上 `cudaGetLastError()` 错误检查并打印错误信息；然后故意把网格大小改为 0 触发一次启动错误，观察检查代码如何报告它。

**建议流程**：

1. 复制一份 `CoMem_AXPY` 目录（例如 `cp -r CoMem_AXPY ../my_axpy_check`，保持原目录干净），或在版本控制下随意修改后用 `git checkout -- CoMem_AXPY/axpy_cudakernel.cu` 恢复。
2. 加入 4.1.4 的 `CUDA_CHECK` 宏，包裹全部 2 次 `cudaMalloc`、3 次 `cudaMemcpy`，并在 4 次 kernel 启动后各追加一行 `CUDA_CHECK(cudaGetLastError());`。
3. 正常规模运行一遍（`./axpy_cuda 1024000`）：预期无错误输出，checksum 与改造前一致——加固不应改变行为。
4. 触发三类错误并分别记录输出，形成一张「错误注入 → 报告位置 → 错误文本」对照表：
   - 分配失败：`./axpy_cuda <超过显存的 n>`（4.1.4 步骤 4）；
   - 启动失败：网格数改 0（4.2.4 步骤 3）；
   - （可选进阶）拷贝方向调换：把 L57 的 `kind` 改成 `cudaMemcpyDeviceToHost`，看你的检查是否拦截了它（4.1.5 练习 1 的动手版）。
5. 结束后恢复源码，用 `git diff` 确认仓库无残留改动。

**产出**：一张错误对照表 + 一段关于「为什么 CUDA 程序必须显式检查每个 API 调用」的 3–5 句总结。若本机无 GPU，步骤 3–4 标注**待本地验证**，但宏的写法与检查位置仍可在代码审查层面完成。

## 6. 本讲小结

- 显存数据生命周期是一条固定流水线：`cudaMalloc` → `cudaMemcpy`（H2D）→ kernel → `cudaMemcpy`（D2H）→ `cudaFree`；这段骨架在 [CoMem_AXPY/axpy_cudakernel.cu:52-73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/CoMem_AXPY/axpy_cudakernel.cu#L52-L73) 与 [MemAlign/axpy_cudakernel.cu:33-54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu#L33-L54) 中逐行同构，是全项目通用模式。
- `cudaMemcpy` 的方向参数「目的在前、源在后」：H2D 是 `（d_x, x, ..., HostToDevice）`，D2H 是 `（y, d_y, ..., DeviceToHost）`；方向写反时运行时可能返回错误，但原代码不检查返回值，失败是静默的。
- kernel 启动是异步的：`<<<>>>` 提交即返回；默认流保证同流操作按序执行（正确性），`cudaDeviceSynchronize` 提供 host 可感知的完成栅栏（测量与错误可见性）。
- 错误分两类时序：启动期错误（如零网格的 invalid configuration）紧跟启动即可用 `cudaGetLastError` 捕获，执行期错误要等同步点之后；`cudaGetLastError` 会清除错误状态，检查必须紧贴被查调用。
- 微基准把 `cudaMalloc`/`cudaFree` 放在十次计时循环内部、并在每个 kernel 后显式同步，是以简洁性和可归因性换来的实验设计，这让 `main` 测得的墙钟包含大量非 kernel 开销。

## 7. 下一步学习建议

- **下一讲（u2-l4）**：串行基线与正确性验证——`check()` 如何量化 GPU 结果与串行结果的偏差、`REAL` 宏切换精度的后果，以及多轮平均计时的细节。本讲实践中反复出现的「checksum 巨大」现象将在那里得到定量解释。
- **向后衔接（u5-l1，HDOverlap）**：本讲的同步 `cudaMemcpy` 是阻塞的；那篇讲义将展示 `cudaMemcpyAsync` + stream 如何让传输与计算重叠，是本讲同步语义的自然进阶。
- **源码阅读建议**：用本讲的骨架清单（分配→H2D→启动+同步→D2H→释放）去通读 [MemAlign/axpy_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/MemAlign/axpy_cudakernel.cu) 和 [BankRedux/sum_cudakernel.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/BankRedux/sum_cudakernel.cu)，验证「读懂一处、全项目通用」；遇到与本讲骨架的差异（例如 BankRedux 的 D2H 只搬一个标量）思考数据角色如何决定传输量。
