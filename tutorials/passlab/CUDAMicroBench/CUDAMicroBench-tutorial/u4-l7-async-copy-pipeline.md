# GSOverlap：memcpy_async 异步拷贝与多级流水线

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚分块矩阵乘中「全局内存 → 共享内存」的搬运与计算为什么是天然串行的，以及这个串行关系如何限制 kernel 的执行效率。
2. 掌握 CUDA 11 引入的设备端异步拷贝原语三件套：`__pipeline_memcpy_async`、`__pipeline_commit`、`__pipeline_wait_prior`，并能区分它们与主机端 `cudaMemcpyAsync` 的本质差别（一个在 kernel 内部、一个在 kernel 之外）。
3. 读懂 `GSOverlap/globalToShmemAsyncCopy.cu` 中的 7 个 kernel 变体，理解「拷贝粒度（float vs float4）」与「流水深度（单级 vs 4 级）」是两个互相独立的优化维度。
4. 用程序自带的 cudaEvent 计时（100 轮平均、GFlop/s 输出）和 nvprof，设计一个近似 2×2 的对照实验，回答：对本案例而言，拷贝粒度与流水深度哪个因素收益更大。

本讲是「GPU 内部深存储层次」单元的最后一站：u4-l1 用共享内存分块解决了**流量**问题（少读几遍全局内存），本讲进一步解决**时序**问题（搬运与计算不要互相干等）。

## 2. 前置知识

### 2.1 分块矩阵乘的回顾（承接 u4-l1）

u4-l1 中我们已经见过分块（tiling）矩阵乘：每个 block 负责 C 的一个 16×16（或 32×32）子块，沿 K 维逐 tile 推进；每个 tile 先把 A、B 的子块搬进 `__shared__` 数组，块内所有线程复用这份数据做完乘加，再搬下一个 tile。它把全局内存流量从 \(2N^3\) 降到 \(2N^3/B\)。

但 u4-l1 的 kernel 有一个没有展开的问题：**每个 tile 内部，「搬」和「算」是先后关系**。伪代码是：

```text
for 每个 tile:
    As[..] = A[..]; Bs[..] = B[..]   # 搬运：全局内存 → 寄存器 → 共享内存
    __syncthreads()                   # 等全块搬完
    Csub += As[ty][k] * Bs[k][tx]     # 计算：读共享内存做乘加
    __syncthreads()                   # 等全块算完（防止下一轮覆写）
```

时间线上，搬运期间乘加单元在等待，计算期间搬运通路在空闲。这就是项目 README 给 GSOverlap 标注的反模式。

### 2.2 「经寄存器中转」是什么意思

普通的赋值语句 `As[ty][tx] = A[a + wA*ty + tx]` 在 GPU 上实际是两条访存指令：一条全局加载（LDG）把数据从显存读进**寄存器**，一条共享内存写（STS）把寄存器写进共享内存。数据必须「路过」寄存器，而寄存器是线程的执行资源——加载指令未完成前，这个线程的后续指令只能等。

CUDA 11 在 Ampere（SM 80）上提供了**异步拷贝指令**（PTX 中的 `cp.async`，硬件通路常称 LDGSTS）：数据从全局内存**直达共享内存**，不经过寄存器，而且指令发射后立刻返回，搬运在后台进行，线程可以继续干别的（比如先算上一个 tile）。编译器把它包装成本讲的主角——`__pipeline_*` 原语。在低于 SM 80 的架构上，这些原语仍然可用，只是编译器会把它们展开成等价的「LDG + STS」序列：**语义正确，但没有真正的异步效果**。这一点直接决定了本讲实验的硬件门槛（见 4.2.4）。

### 2.3 需要区分的两个「异步」

| 名称 | 发生位置 | 数据通路 | 讲义 |
| --- | --- | --- | --- |
| `cudaMemcpyAsync` | 主机端 API，kernel 之外 | 主机内存 ↔ 显存 | u3-l3、u5-l1 |
| `__pipeline_memcpy_async` | 设备端，kernel 内部 | 显存 → 共享内存 | **本讲** |

两者名字里都有 Async，但完全不是一个东西。本讲只讨论后者；u5-l1 会专门讲前者。

### 2.4 本程序与前几个基准的两点不同

- GSOverlap 目录里**没有 test.sh，也没有 .output.*.txt 归档**（可用 `ls GSOverlap/` 核验），所以本讲无法像 u4-l5 那样借用 Carina/Fornax 的云端结果，性能实践需要本地 NVIDIA GPU；无 GPU 时请做各模块给出的「源码阅读型」替代实践。
- 它源自 CUDA Samples（主仓库 [README.md:L113-L115](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md#L113-L115) 说明 GSOverlap、Conkernels、TaskGraph 三个基准依赖 Common 目录的 helper 头文件），因此计时方式不是本手册常见的 `read_timer_ms` 墙钟，而是 **cudaEvent 事件计时**（u3-l3 已见过）——这反而是更接近纯 kernel 时间的口径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [GSOverlap/globalToShmemAsyncCopy.cu](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu) | 全部内容都在这一个文件：7 个矩阵乘 kernel 模板 + host 侧 `MatrixMultiply` 驱动函数 + `main` 命令行解析 |
| [GSOverlap/README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/README.md) | 样本自述：SM 8.0+ 才走真异步拷贝，需要 CUDA 11.1，支持架构列表到 SM 8.6 |
| [GSOverlap/Makefile](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile) | CUDA Samples 风格多架构构建：`-I../Common`、SMS/GENCODE 生成、C++11 |
| [README.md](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/README.md) | 项目总表：GSOverlap 的反模式是「全局-共享内存拷贝耗时」，对策是 CUDA 11 的 memcpy_async |

程序结构总览（运行时可用 `-kernel=N` 选择，编号定义在 [globalToShmemAsyncCopy.cu:L56-L69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L56-L69)）：

| 编号 | kernel 名 | 拷贝方式 | 粒度 | 流水级数 |
| --- | --- | --- | --- | --- |
| 0 | MatrixMulAsyncCopyMultiStageLargeChunk | 异步 | float4 | 4 |
| 1 | MatrixMulAsyncCopyLargeChunk | 异步 | float4 | 1 |
| 2 | MatrixMulAsyncCopyLargeChunkAWBarrier | 异步 | float4 | 1（awbarrier 同步） |
| 3 | MatrixMulAsyncCopyMultiStage | 异步 | float | 4 |
| 4 | MatrixMulAsyncCopySingleStage | 异步 | float | 1 |
| 5 | MatrixMulNaive | 同步赋值 | float | — |
| 6 | MatrixMulNaiveLargeChunk | 同步赋值 | float4 | — |

这张表就是本讲的实验设计图纸：编号 4/1/5/6 恰好构成「是否异步 × 是否 float4」的 2×2 对照，编号 0/3 再叠加「多级流水」维度。

## 4. 核心概念与源码讲解

### 4.1 反模式基线：MatrixMulNaive 与 NaiveLargeChunk

#### 4.1.1 概念说明

任何优化都要有参照系。本基准的反模式就是 u4-l1 学过的经典分块矩阵乘（kernel 5，`MatrixMulNaive`），以及它的 float4 装载变体（kernel 6，`MatrixMulNaiveLargeChunk`）。两者的「搬」与「算」都是彻底串行的：线程发出装载指令后，必须等数据真正落到共享内存才能继续；反过来，下一轮装载也必须等所有线程算完，防止覆写还没读完的 tile。

#### 4.1.2 核心流程

`MatrixMulNaive` 单个 tile 的时序：

```text
── 装载（LDG→寄存器→STS，每线程 1 个 float × 2 矩阵）── 块栅栏 ── 乘加 16 步 ── 块栅栏 ── 装载下一 tile …
```

若一个 tile 的搬运耗时为 \(T_{copy}\)、计算耗时为 \(T_{comp}\)，共 \(N_K\) 个 tile，则总时间近似为：

\[
T_{serial} \approx N_K \times (T_{copy} + T_{comp})
\`

理想情况下的流水线则应达到：

\[
T_{pipelined} \approx T_{fill} + N_K \times \max(T_{copy},\; T_{comp}), \qquad T_{fill} \approx D \times T_{copy}
\]

其中 \(D\) 是流水深度，\(T_{fill}\) 是开头「灌管线」的一次性代价。两条公式的差距就是本讲所有优化的收益上限。

#### 4.1.3 源码精读

装载与两道栅栏（u4-l1 已逐行讲过同样的模式，这里只看关键差异点）：

- [globalToShmemAsyncCopy.cu:L568-L572](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L568-L572)：`As[threadIdx.y][threadIdx.x] = A[...]` 同步赋值装载（每线程 1 个 float），随后 `__syncthreads()` 等全块搬完——这是「搬完才算」的栅栏。
- [globalToShmemAsyncCopy.cu:L577-L585](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L577-L585)：`#pragma unroll` 的 16 步乘加之后又一道 `__syncthreads()`——注释写明这是防止下一迭代装载覆写还在被读的 As/Bs，即「算完才搬」的栅栏。

`MatrixMulNaiveLargeChunk` 只改装载方式，计算部分与 Naive 完全一样：

- [globalToShmemAsyncCopy.cu:L636-L643](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L636-L643)：`t4x = threadIdx.x * 4`，只有 `t4x < BLOCK_SIZE`（即 threadIdx.x < 4，全块 1/4 的线程）参与装载，每人通过 `float4` 一次搬 16 字节（4 个 float）。16×16 的一行是 16 个 float = 64 字节 = 4 个 float4，正好由 x = 0..3 四个线程拼齐。

这样 kernel 5 与 6 之间只有「装载粒度」一个变量；后面 4.4 节你会看到 kernel 1 与 4 之间同样是粒度变量——对照实验的骨架在这里就埋好了。

#### 4.1.4 代码实践（源码阅读型，无需 GPU）

1. **实践目标**：量化「每线程 1 个 float」与「1/4 线程各搬 16 字节」两种装载在指令条数上的差别。
2. **操作步骤**：
   - 阅读 L568-L569（Naive）与 L636-L643（NaiveLargeChunk），分别统计**单个 tile 内、单个 block** 为装满 As 与 Bs 需要发起多少条「装载语句」、多少线程参与。
   - 用 `grep -n "t4x" GSOverlap/globalToShmemAsyncCopy.cu` 找出所有使用 4 元素粒度的 kernel，确认它们都满足 `BLOCK_SIZE % 4 == 0` 的注释要求（BLOCK_SIZE 此处为 16，见 L73）。
3. **需要观察的现象**：Naive 每 tile 每线程 2 条装载语句（A、B 各一），256 线程共 512 次 4 字节搬运；LargeChunk 版只有 64 个线程干活，但每人 16 字节，总字节数相同（2×16×16×4B = 2KB）。
4. **预期结果**：总搬运字节数不变，但装载指令数减少为 1/4，且单条指令从 4 字节变为 16 字节对齐访问——这对 warp 内合并访问（u4-l2）更友好。
5. 本实践为纯阅读推算，结论「待本地验证」的部分是它对实际耗时的贡献（留到综合实践用 `-kernel=5` vs `-kernel=6` 实测）。

#### 4.1.5 小练习与答案

**练习 1**：`MatrixMulNaive` 的乘加循环里为什么必须有两道 `__syncthreads()`，而去掉第二道会怎样？

**答案**：第一道保证「搬完才算」（否则别的线程可能读到旧 tile）；第二道保证「算完才搬」（否则快的线程开始写下一个 tile，会把慢线程还在读的 As/Bs 覆盖掉）。去掉第二道会产生块内数据竞争，结果错误。

**练习 2**：`MatrixMulNaiveLargeChunk` 中，如果 `BLOCK_SIZE` 是 14（不能被 4 整除），`t4x < BLOCK_SIZE` 的守卫会导致什么问题？

**答案**：`threadIdx.x*4` 会越过 `BLOCK_SIZE-1` 后仍可能小于 BLOCK_SIZE 的组合不再整齐：一行 14 个 float 无法用 4 个 float4 严丝合缝地覆盖，最后一个 float4 会越出该行边界（读到下一行）甚至越出共享内存数组范围。所以源码多处注释 `Requires BLOCK_SIZE % 4 == 0`（如 [L80](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L80)、[L180](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L180)、[L272](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L272)）。

### 4.2 `__pipeline` 流水线原语

#### 4.2.1 概念说明

异步拷贝不是一条孤立的指令，而是一套「发起—打包—等待」的协作协议，由头文件 `cuda_pipeline.h` 提供的三个原语组成：

- **`__pipeline_memcpy_async(dst, src, size)`**：发起一次从全局内存（`src`）到共享内存（`dst`）的异步拷贝，`size` 为字节数（本例中是 4 或 16）。调用立即返回，不保证数据已到位。单次拷贝的字节数较小，float 对应 4 字节、float4 对应 16 字节。
- **`__pipeline_commit()`**：把「上次 commit 以来」发出的所有异步拷贝打包成一个**组（stage）**。此后它们作为一个整体被跟踪。
- **`__pipeline_wait_prior(N)`**：等待，直到**最多只剩最近 N 个组**还没完成。`__pipeline_wait_prior(0)` 就是「等全部完成」；`__pipeline_wait_prior(3)` 则允许最新的 3 个组继续在后台飞行，只保证第 4 新的组已就绪——这正是多级流水的钥匙。

「组」的编号是每次 commit 递增的，所以「等除最近 N 个之外的全部」等价于「我需要的那个数据已经到了」。另外还有一个隐含约定：`__pipeline_wait_prior` 只约束**本线程**发起的拷贝；要让**全块**都看到数据，仍需 `__syncthreads()`。

#### 4.2.2 核心流程

单级（拷贝与计算不重叠，只是免寄存器中转）：

```text
memcpy_async(A...) ; memcpy_async(B...)   # 发起
commit()                                  # 打包成 1 个组
wait_prior(0)                             # 等这组完成
__syncthreads()                           # 等全块完成
计算本 tile
```

多级（拷贝与计算重叠，D = 4 级旋转缓冲）：

```text
每轮：
    预取循环：把领先窗口补满（最多领先 D 个 tile），每个 tile commit 一次
    wait_prior(D-1)        # 最新 D-1 个组可以还在飞，我要算的这组必须已完成
    __syncthreads()
    计算第 i 个 tile（用第 i%D 级缓冲）
    # 注意：计算后不再需要第二道 __syncthreads（见 4.5）
```

#### 4.2.3 源码精读

- [globalToShmemAsyncCopy.cu:L44-L48](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L44-L48)：`#include <cuda_pipeline.h>` 引入 `__pipeline_*` 原语；`cuda_awbarrier.h` 只在 `__CUDA_ARCH__ >= 700` 时引入，供 kernel 2 使用。
- [globalToShmemAsyncCopy.cu:L54](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L54)：`namespace nvcuda_namespace = nvcuda::experimental;` 是 C++ 风格 API 的别名。
- [globalToShmemAsyncCopy.cu:L71](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L71)：`#define USE_CPP_API 0` 是一个编译期开关。置 0 用上面的 C 风格内建原语（默认）；置 1 则每个 kernel 改用 `nvcuda::experimental::pipeline` 对象，接口变为 `memcpy_async(dst, src, pipe)` / `pipe.commit()` / `pipe.wait_prior<N>()`，例如多级版本中的 [L132-L151](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L132-L151)（`#if USE_CPP_API` 分支与 `#else` 分支一一对应）。两条路线语义相同，本讲以默认的 C 风格为主。

#### 4.2.4 代码实践（环境验证型）

1. **实践目标**：确认你的工具链与架构组合能否走「真异步」路径。
2. **操作步骤**：
   - `nvcc --version` 确认 CUDA ≥ 11（本样本按 CUDA 11.1 编写，见 [GSOverlap/README.md:L33](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/README.md#L33)）。
   - 阅读 [GSOverlap/Makefile:L313-L332](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/Makefile#L313-L332)：注意默认 `SMS` 列表（x86 上是 `35 37 50 52 60 61 70 75`）**并不包含 80/86**，最高架构只生成 compute_75 的 PTX 用于前向兼容。为 compute_75 编译时，`__pipeline_memcpy_async` 会被展开成等价的同步装载序列；要让编译器生成 Ampere 的原生异步拷贝指令，需显式指定目标架构。
   - 若你的 GPU 是 SM 80+（如 A100），用 `make SMS="80"`（或 `make SMS="80 86"`）重新编译；其他架构直接 `make` 即可，语义正确但异步收益有限（待本地验证）。
3. **需要观察的现象**：构建日志会打印 GENCODE 参数；`make SMS="80"` 后产物只在 SM 80 GPU 上可直接运行。
4. **预期结果**：在非 Ampere GPU 上本基准仍能跑通并 PASS（原语有等价降级实现），但异步/多级版本相对 Naive 的加速会明显缩水——这正是「反模式 vs 优化」的演示价值受硬件代际影响的例子。

#### 4.2.5 小练习与答案

**练习 1**：`__pipeline_wait_prior(0)` 与 `__pipeline_wait_prior(3)` 的区别是什么？后者为什么能带来流水线效果？

**答案**：`wait_prior(0)` 等待本线程发起的全部拷贝组完成，此后没有任何拷贝在飞行，等价于同步屏障；`wait_prior(3)` 允许最近 3 个组继续飞行，只保证更早的组（也就是马上要计算的那个 tile）就绪。这样当前 tile 在做乘加时，后面 3 个 tile 的数据还在后台搬运，搬运延迟被计算掩盖。

**练习 2**：为什么 `__pipeline_wait_prior` 之后还需要 `__syncthreads()`？

**答案**：wait_prior 只跟踪**本线程** commit 的组；计算要读的是**整个 block** 256 个线程共同装满的 As/Bs，必须用块级栅栏等所有线程的拷贝都完成，否则会读到别的线程尚未装好的元素。

### 4.3 MatrixMulAsyncCopySingleStage：单级异步流水

#### 4.3.1 概念说明

kernel 4 是最小改动版：保持 Naive 的「单份 tile 缓冲 + 双栅栏」结构，只把装载语句换成 `__pipeline_memcpy_async` + `commit` + `wait_prior(0)`。它是理解异步原语的最好起点，因为它**没有**流水线效果——每轮发起拷贝后立刻等它完成，搬运与计算依旧串行。它的价值在于隔离出「异步通路本身」（免寄存器中转、指令语义）这一个变量：kernel 4 对 kernel 5 的差异 = 异步原语替换同步赋值；其余一切不变。

#### 4.3.2 核心流程

```text
for 每个 tile:
    每线程发起 2 次 4 字节异步拷贝（As、Bs 各一）
    commit(); wait_prior(0)        # 等自己的拷贝完成
    __syncthreads()                # 等全块
    16 步乘加
    __syncthreads()                # 防覆写（缓冲只有一份，仍需两道栅栏）
写回 C
```

注意它保留了第二道 `__syncthreads()`：因为共享缓冲只有一份，下一轮装载会写同一个 As/Bs，必须等全块读完。

#### 4.3.3 源码精读

- [globalToShmemAsyncCopy.cu:L353-L363](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L353-L363)：kernel 签名与单份共享内存声明 `__shared__ float As[BLOCK_SIZE][BLOCK_SIZE]`（无 stage 维度）。
- [globalToShmemAsyncCopy.cu:L392-L407](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L392-L407)：装载主体。`A_float`/`B_float` 仍是每线程 1 个 float 的地址；`__pipeline_memcpy_async(&As[threadIdx.y][threadIdx.x], A_float, sizeof(float))` 把它交给异步通路，随后 `__pipeline_commit()` 打包、`__pipeline_wait_prior(0)` 等待完成。
- [globalToShmemAsyncCopy.cu:L410-L424](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L410-L424)：乘加与第二道栅栏，与 Naive 的 L411/L424 结构完全对应——确认「除装载外一切不变」。

#### 4.3.4 代码实践

1. **实践目标**：量化「同步赋值 → 异步原语（立即等待）」这一步替换的净收益。
2. **操作步骤**（需 GPU）：
   - `cd GSOverlap && make`
   - `./globalToShmemAsyncCopy -kernel=5 -wA=640 -hA=640 -wB=640 -hB=640`，记录输出的 `Performance= ... GFlop/s, Time= ... msec`。
   - `./globalToShmemAsyncCopy -kernel=4 -wA=640 -hA=640 -wB=640 -hB=640`，同样记录。
   - 可选：`nvprof ./globalToShmemAsyncCopy -kernel=4 ...` 观察 GPU activities 中 kernel 名与耗时，和程序自打印对照。
3. **需要观察的现象**：两组输出的 `Result = PASS` 都应出现；GFlop/s 有差异。
4. **预期结果**：在没有真异步通路的架构上，两者可能接近（都退化为 LDG+STS）；在 SM 80+ 上 kernel 4 应不慢于 kernel 5。具体差距**待本地验证**——本基准目录无历史归档可查。
5. 无 GPU 替代：对比阅读 L392-L407 与 L568-L569，列出两条路线各自的指令构成（异步：发起+打包+等待；同步：加载+存储），并解释为什么「立即 wait_prior(0)」使流水线深度为 1。

#### 4.3.5 小练习与答案

**练习 1**：SingleStage 版本每轮有两道 `__syncthreads()`，4.5 节的 MultiStage 版本计算后却没有第二道。为什么单级版不能省？

**答案**：单级版只有一份 As/Bs，下一轮装载会直接覆写同一块缓冲，必须先等全块读完；多级版有 4 份旋转缓冲，下一轮写的是不同的 stage，同一 stage 要到 4 轮之后才会被重写，期间已经历多次轮首栅栏，安全性由「缓冲距离」保证。

**练习 2**：如果误把 `__pipeline_wait_prior(0)` 写成 `__pipeline_memcpy_async` 之后、`__pipeline_commit()` 之前，会发生什么？

**答案**：wait_prior 等待的对象是「已 commit 的组」。尚未 commit 的拷贝请求不属于任何组，等待行为与未完成的组无关——语义上要么编译不通过要么等待范围错误，正确顺序必须是「发起 → commit → wait」。这也是为什么三个原语总是一起出现。

### 4.4 MatrixMulAsyncCopyLargeChunk：float4 拷贝粒度

#### 4.4.1 概念说明

kernel 1 在 kernel 4 的基础上只改一个变量：**每次异步拷贝的字节数**，从 4 字节（float）提升到 16 字节（float4）。参与装载的线程从「全块 256 人」变为「1/4 线程（threadIdx.x < 4）」，每人负责 4 个连续 float。它与 kernel 4 构成「流水深度同为 1，粒度不同」的对照；与 kernel 6（同步 float4）构成「粒度相同，是否异步」的对照。三对关系拼起来就是 2×2 因子实验。

另外，kernel 2（`AsyncCopyLargeChunkAWBarrier`）是它的孪生版本，只是把同步机制从 `__syncthreads()` 换成 arrive-wait barrier（SM 7.0+ 的实验 API，`main` 里会检查架构并拒绝在低版本上运行），本讲只做简介。

#### 4.4.2 核心流程

```text
for 每个 tile:
    if threadIdx.x < 4:                     # t4x < BLOCK_SIZE
        把 As/Bs 第 threadIdx.y 行重解释为 4 个 float4
        memcpy_async(16B) × 2（A、B 各一）
        commit(); wait_prior(0)
    __syncthreads()
    16 步乘加
    __syncthreads()
写回 C
```

「重解释」是关键技巧：`reinterpret_cast<float4*>(&As[ty][t4x])` 把 `float` 数组的一段地址当作 `float4` 指针使用，前提是这段地址 16 字节对齐且长度是 4 的倍数——这就是各处 `BLOCK_SIZE % 4 == 0` 注释的由来。

#### 4.4.3 源码精读

- [globalToShmemAsyncCopy.cu:L208-L227](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L208-L227)：`t4x = threadIdx.x * 4`；四个 `reinterpret_cast` 分别把**共享内存目的地址**与**全局内存源地址**都转成 `float4*`。注意 L218-L220 的注释保留了「旧写法：每线程一个元素」，方便对照。
- [globalToShmemAsyncCopy.cu:L235-L240](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L235-L240)：`__pipeline_memcpy_async(A4s, A4, sizeof(float4))`——size 参数从 4 变 16，随后仍是 `commit()` + `wait_prior(0)` 的单级协议。
- [globalToShmemAsyncCopy.cu:L243-L257](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L243-L257）：栅栏与乘加结构同 SingleStage。
- AWBarrier 变体：[globalToShmemAsyncCopy.cu:L322-L329](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L322-L329) 用 `pipe.arrive_on(barrier)`（拷贝完成时屏障计数自动 +1）替代 `commit+wait`，再用 `barrier.arrive_and_wait()` 替代 `__syncthreads()`；架构守卫在 [L271](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L271) 与 [L934-L940](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L934-L940)（低于 SM 7.0 直接退出）。

**一个值得批判性阅读的细节**：本 kernel 中 B 的源地址写的是 `B[a + wA * threadIdx.y + t4x]`（[L227](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L227)，用的是 **a/wA**），而 Naive 版本用的是 `B[b + wB * threadIdx.y + threadIdx.x]`（[L569](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L569)，**b/wB**）。`a` 起于 `wA*16*blockIdx.y` 而 `b` 起于 `16*blockIdx.x`，地址并不相同。它之所以不影响校验，是因为 host 侧把 B 全部初始化为常量 `valB`（[L692-L694](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L692-L694)），读 B 的任何位置值都一样。这是「常量初始化 + 常量参考答案」的校验设计会掩盖访存差异的活例子（与 u2-l4 讲过的弱校验一脉相承）。

#### 4.4.4 代码实践

1. **实践目标**：隔离「拷贝粒度 4B → 16B」这一个变量的收益。
2. **操作步骤**（需 GPU）：依次运行
   - `./globalToShmemAsyncCopy -kernel=4 ...`（异步 float）
   - `./globalToShmemAsyncCopy -kernel=1 ...`（异步 float4）
   - `./globalToShmemAsyncCopy -kernel=6 ...`（同步 float4）
   - `./globalToShmemAsyncCopy -kernel=5 ...`（同步 float）
   （`...` 代表同样的 `-wA=640 -hA=640 -wB=640 -hB=640`；也可再加大到 1280 观察规模影响。）把四个 GFlop/s 填进 2×2 表。
3. **需要观察的现象**：全部 `Result = PASS`；四格数值两两不同。
4. **预期结果**：粒度提升（5→6、4→1）通常带来正收益（装载指令更少、16 字节对齐更利于合并）；异步替换（5→4、6→1）的收益依赖架构代际。哪一格最高**待本地验证**。
5. 无 GPU 替代：完成 4.4.5 练习 2 的地址推算，并回答「若把 `ConstantInit(h_B, ...)` 改为随机值，哪些 kernel 的校验会失败？」——纯源码推演即可完成。

#### 4.4.5 小练习与答案

**练习 1**：为什么参与装载的线程恰好是 1/4，而不是让 256 个线程都各搬一个 float4？

**答案**：一个 tile 的 As 总共 16×16 = 256 个 float = 64 个 float4，只需要 64 次搬运；块内有 256 个线程，让每人都搬一个 float4 会搬 4 倍的数据（越界）。`t4x < BLOCK_SIZE`（即 x < 4）正好筛出 16 行 × 4 列 = 64 个线程，每人负责本行 1/4 段（4 个 float）。

**练习 2**：对 640×640 的矩阵、`blockIdx = (2, 3)`、`threadIdx = (1, 5)`，写出 LargeChunk kernel 中 A4 与 B4 各自读取的元素下标区间（按一维展开）。

**答案**：`wA = wB = 640`，`BLOCK_SIZE = 16`，`t4x = 4`。A：基址 `a = 640*16*3 = 30720`，读取 `A[30720 + 640*5 + 4 .. +7]`，即 30744..30747。B：源码用的基址与 A 相同（`a + wA*ty + t4x`，见 L227），即 `B[30744..30747]`；而按 Naive 的语义本应是 `b = 16*2 = 32`，读 `B[32 + 640*5 + 4 .. +7]` = `B[3236..3239]`。两者不一致——再次印证 4.4.3 末尾的观察：只有 B 为常量时结果才碰巧一致。

### 4.5 MatrixMulAsyncCopyMultiStage：多级流水与旋转缓冲

#### 4.5.1 概念说明

单级流水里 `wait_prior(0)` 一票否决了重叠。多级版本（kernel 3，float 粒度；kernel 0，float4 粒度）做两件事：

1. **旋转缓冲（rotating buffer）**：共享内存从 1 份 tile 扩到 `maxPipelineStages = 4` 份，即 `__shared__ float As[4][16][16]`。第 `iStage` 个预取 tile 写入第 `iStage % 4` 级，计算第 `i` 个 tile 时读第 `i % 4` 级。
2. **预取窗口**：装载循环不再与计算循环一一同步，而是始终保持领先最多 4 个 tile；每轮计算前只 `__pipeline_wait_prior(3)`——最新的 3 组拷贝允许继续飞行，要算的这一组已经落地。

效果：搬运延迟被后续 tile 的计算掩盖，总时间从 \(N_K (T_{copy}+T_{comp})\) 向 \(T_{fill} + N_K \max(T_{copy},T_{comp})\) 靠拢。代价是共享内存从每块 2 KB 涨到 8 KB（4 级 × 2 矩阵 × 16×16 × 4B），occupancy 可能下降——典型的存储换时间的取舍。

#### 4.5.2 核心流程

```text
iStage 指针初始与主循环同起点
for i = 0 .. N_K-1:                        # 计算循环
    while aStage <= a + aStep*4:           # 预取循环：补满领先窗口
        if aStage <= aEnd:
            异步拷贝 tile iStage → 缓冲级 iStage % 4
        commit()                            # 每个 tile 一个组
        iStage += 1
    wait_prior(3)                           # 保留最新 3 组在飞
    __syncthreads()
    用缓冲级 i % 4 做 16 步乘加
    # 计算后无第二道栅栏：下一轮写的是别的级
写回 C
```

正确性说明：第 `i` 轮计算读第 `i%4` 级；该级再次被写入发生在预取第 `i+4` 个 tile 时，中间隔着第 i+1、i+2、i+3 轮，每轮开头都有一道 `__syncthreads()`，足以保证全块都读完了第 `i` 轮的数据。源码注释 [L515-L516](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L515-L516) 说的正是这件事。

#### 4.5.3 源码精读

（以 kernel 3 `MatrixMulAsyncCopyMultiStage` 为主；kernel 0 是它的 float4 版，结构相同。）

- [globalToShmemAsyncCopy.cu:L438-L447](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L438-L447)：`constexpr size_t maxPipelineStages = 4;` 与带 stage 维度的共享内存声明——多级版本与单级版本在数据结构上的全部差别。
- [globalToShmemAsyncCopy.cu:L471-L497](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L471-L497)：主循环与嵌套预取循环共用 6 个循环变量（`a, b, i` 给计算，`aStage, bStage, iStage` 给预取）。内层 `for ( ; aStage <= a + aStep*maxPipelineStages; ...)` 没有初值表达式——它从上一轮停下的地方继续，这就是「保持领先窗口」的实现；`if (aStage <= aEnd)` 处理 K 维末尾不足 4 个 tile 的情况；每个 tile 拷完后 `__pipeline_commit()` 立即打包成组。
- [globalToShmemAsyncCopy.cu:L498-L506](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L498-L506)：`__pipeline_wait_prior(maxPipelineStages-1)` 即 `wait_prior(3)`，随后唯一的 `__syncthreads()` 与 `j = i % maxPipelineStages` 取本级缓冲。
- [globalToShmemAsyncCopy.cu:L511-L513](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L511-L513)：乘加读 `As[j][...]`，循环后直接进入下一轮（无第二道栅栏）。
- kernel 0（float4 多级）：[L121-L151](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L121-L151) 的预取循环里多了 `t4x < BLOCK_SIZE` 的线程筛选与 `float4` 重解释（对应 4.4 的装载方式），`wait_prior` 逻辑与 [L150](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L150) 相同；它是 `main` 里 switch 的 `default` 分支（[L736-L738](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L736-L738)），也是命令行不传 `-kernel` 时的默认选项（[L919](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L919)）——样本作者把它当作「最优组合」的示范。

host 侧驱动（所有 kernel 共用，承接 u3-l3 的事件计时）：warmup 一次并 `cudaStreamSynchronize`（[L734-L761](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L734-L761)），再 `cudaEventRecord` 夹住 100 次循环（[L765-L805](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L765-L805)），`cudaEventElapsedTime` 得到平均每次的毫秒与 GFlop/s。这个口径只含 kernel 执行（同一 stream 中 kernel 之间没有其他工作），比 u1-l4 讲的 wall time 干净得多；但程序在 [L857-L858](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L857-L858) 自我提醒：样本并非为精确性能测量设计，GPU Boost 会带来波动。

#### 4.5.4 代码实践

1. **实践目标**：隔离「流水深度 1 → 4」这一个变量的收益，并体会 `maxPipelineStages` 的取舍。
2. **操作步骤**（需 GPU）：
   - 同规模运行 `-kernel=4`（单级 float）与 `-kernel=3`（4 级 float），记录 GFlop/s；
   - 同规模运行 `-kernel=1`（单级 float4）与 `-kernel=0`（4 级 float4），记录 GFlop/s；
   - 把 [L439](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L439) 的 `maxPipelineStages` 分别改为 2 与 8（kernel 0 在 [L83](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L83)），重新编译 `-kernel=3`/`-kernel=0` 各跑一次，观察共享内存占用（每级 2 KB/block）与时间的联动。
3. **需要观察的现象**：多级应快于同级单级；stage 数从 2 → 4 → 8 时收益递增或饱和。
4. **预期结果**：若 \(T_{copy}\) 与 \(T_{comp}\) 接近，4 级已足够掩盖大部分延迟，8 级的边际收益趋零甚至因 occupancy 下降而变慢——具体拐点**待本地验证**。
5. 无 GPU 替代：手工模拟预取循环——设 K 维共 6 个 tile，列表写出外层 `i = 0..5` 各轮进入时 `iStage` 的值与 `wait_prior(3)` 之后「已就绪的最新 tile 编号」，验证第 4 轮起预取循环体不再执行（窗口已顶到 `aEnd`）。

#### 4.5.5 小练习与答案

**练习 1**：多级版本每轮只有一道 `__syncthreads()`，为什么不会发生「计算还没读完，下一轮装载就覆写」的竞争？

**答案**：写读相隔 4 级。第 `i` 轮计算读第 `i%4` 级；该级下一次被写入要等到预取第 `i+4` 个 tile，而中间的第 i+1、i+2、i+3 轮每轮开头都有一道 `__syncthreads()`，三轮栅栏保证了全块对第 `i` 级的读取在它被重写前早已完成。

**练习 2**：把 `maxPipelineStages` 从 4 改成 1，这个 kernel 会退化成什么？改成 5 呢？

**答案**：改成 1 时，`wait_prior(0)` 等价于等全部完成，且每次都写同一级缓冲——退化为 4.3 节的单级流水（但计算后没有第二道栅栏，`i%1` 级马上被重写，将产生竞争！所以 stage=1 不是合法配置，旋转缓冲的正确性依赖「级数 ≥ 2 且由轮首栅栏隔开」）。改成 5 时预取窗口更深、共享内存每 block 增加 2 KB（5×2×16×16×4B = 10 KB），可能压低 occupancy。这提示我们：流水深度是一个需要实验标定的参数，不是越大越好。

**练习 3**：`wait_prior(maxPipelineStages-1)` 中的 `-1` 若误删（变成 `wait_prior(maxPipelineStages)`），程序还正确吗？性能会怎样？

**答案**：`wait_prior(N)` 只要求「最多剩 N 个组未完成」，把 N 从 3 放宽到 4 意味着连将要计算的那个 tile 也不保证就绪，线程可能读到旧数据——正确性被破坏且校验（常量 B 掩盖下仍可能 PASS，见 4.4.3）未必能发现；性能上等待变少但结果是错的。这个例子再次说明弱校验基准里「读对源码」比「跑过校验」更可靠。

## 5. 综合实践

**任务：完成「粒度 × 异步 × 流水深度」的完整对照实验，并回答本讲的核心问题。**

本基准的 7 个 kernel 本身就是一套设计好的因子实验。请按以下步骤完成：

1. **准备**：`cd GSOverlap && make`（A100 等 SM 80+ 显卡建议 `make clean && make SMS="80"`）。确认 `./globalToShmemAsyncCopy -kernel=0` 能输出 `Result = PASS`。先用 `-help` 查看命令行格式（[L871-L882](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L871-L882)）。
2. **采集**：在固定规模（默认 640×640，可加一组 1280×1280）下依次运行 `-kernel=5、6、4、1、3、0`，把每次的 `Performance`（GFlop/s）与 `Time`（msec）填入下表（每格建议跑 3 次取中位数，抵消 GPU Boost 波动）：

   | | 同步赋值 | 异步单级 | 异步 4 级 |
   | --- | --- | --- | --- |
   | float | kernel 5：__ | kernel 4：__ | kernel 3：__ |
   | float4 | kernel 6：__ | kernel 1：__ | kernel 0：__ |

3. **补充指标**：任选两格（如 kernel 1 与 kernel 0）用 `nvprof ./globalToShmemAsyncCopy -kernel=N -wA=... ...` 采集 GPU activities 表，确认你看到的 kernel 名与 [L67-L69](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L67-L69) 的 `kernelNames[]` 一致，并记录 nvprof 口径的 kernel 时间与程序自打印时间的吻合程度。
4. **分析（本实践的交付物）**：写一段 200 字左右的分析，回答——**对本案例而言，拷贝粒度（float→float4）与流水深度（1→4）哪个因素收益更大？** 论证要求：
   - 从表中分别计算「粒度增益」（同列内 float4/float 的 GFlop/s 比值）与「流水增益」（同行内 4 级/单级的比值），比较两个平均比值；
   - 用 4.1.2 的流水线时间模型解释你观察到的模式（例如若 \(T_{comp} \gg T_{copy}\)，流水深度收益应有限）；
   - 至少指出一个可能干扰结论的因素（架构代际、GPU Boost、640 规模下 K 维只有 40 个 tile、`__restrict__` 修饰差异——注意 async 系列 kernel 的指针带 `__restrict__` 而 Naive 系列不带，见 [L353-L355](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L353-L355) 与 [L529-L531](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L529-L531)，这也是一个未控制的变量）。
5. **无 GPU 替代方案**：把第 4 步的分析改为纯推演——基于指令数（4.1.4 的结论）、共享内存占用（2 KB vs 8 KB）与流水线模型做定性排序，并明确标注哪些结论「待本地验证」。

## 6. 本讲小结

- 分块矩阵乘（u4-l1）解决**流量**，本讲解决**时序**：每个 tile 的「搬入共享内存」与「乘加计算」原本串行，异步拷贝让二者重叠。
- 设备端流水线三原语：`__pipeline_memcpy_async` 发起（全局→共享、不经寄存器、立即返回）、`__pipeline_commit` 打包成组、`__pipeline_wait_prior(N)` 等到只剩最近 N 组未完成；`wait_prior(0)` 是「全等」，`wait_prior(3)` 是流水的钥匙。
- 真正的异步硬件通路（`cp.async`/LDGSTS）需要 SM 80+ 且以 compute_80+ 为编译目标；低架构上原语退化为等价同步序列——语义对，收益无。默认 Makefile 的 SMS 不含 80，A100 上要做性能实验须 `make SMS="80"`。
- 7 个 kernel 构成天然因子实验：`{同步, 异步} × {float, float4} × {1 级, 4 级}`；多级流水靠 4 份旋转缓冲 + `wait_prior(D-1)` + 「写读相隔 D 级、轮首栅栏隔开」保证无竞争，还省掉了计算后的第二道 `__syncthreads()`；代价是共享内存每 block 从 2 KB 涨到 8 KB。
- 本基准的校验用常量矩阵（A 全 1.0、B 全 2.10）+ 常量参考答案，掩盖了 LargeChunk 系列对 B 的可疑索引（用 `a/wA` 而非 `b/wB`）——弱校验之下，读对源码比跑过 PASS 更重要。
- 计时口径：cudaEvent 夹 100 次循环取平均并换算 GFlop/s，只含 kernel 时间，比手册其他基准的 wall time 干净，但仍受 GPU Boost 波动影响。

## 7. 下一步学习建议

- **进入单元五（CPU-GPU 数据搬运）**：本讲的 `__pipeline_memcpy_async` 是**设备内部**（全局→共享）的异步；u5-l1 的 HDOverlap 讲**主机↔设备**的 `cudaMemcpyAsync` + stream，两者名字相近、机制完全不同，学完后建议回头画一张「三级异步通路」对照图（主机端 cudaMemcpyAsync、设备端 pipeline、kernel 内 warp 异步原语）。
- **与 u6-l1 互相印证**：Shuffle 的 reduce 优化阶梯里有「每个线程处理多个元素」的算法级优化；本讲的 `maxPipelineStages` 与 tile 尺寸 `blockSize`（[L73](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L73)）同属「参数空间需要实验标定」的问题，可把两处的实验方法统一成一张 checklist。
- **扩展阅读（源码内）**：kernel 2 的 arrive-wait barrier（[L267-L350](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L267-L350)）展示了比 `__syncthreads()` 更细粒度的块内同步；`USE_CPP_API` 开关（[L71](https://github.com/passlab/CUDAMicroBench/blob/59c4ca6b0c7800c4994428db4b829f98f71c9071/GSOverlap/globalToShmemAsyncCopy.cu#L71)）对应的 `nvcuda::experimental::pipeline` C++ 接口则是后来 `cuda::memcpy_async`（`cuda/barrier>`）正式 API 的前身，可作为进阶跳板。
- 若要继续本仓库的源码训练，下一讲 u5-l1（HDOverlap）只需 `axpy_cudakernel.cu` 一个文件，阅读量比本讲小很多，适合用来巩固「计时位置测的究竟是什么」这一贯穿全手册的主题。
