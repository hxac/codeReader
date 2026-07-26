# 块级 GEMM 内核解剖

## 1. 本讲目标

上一讲（u3-l8）我们看懂了 TileLang 算子的「四件套」骨架——`get_configs` 造搜索空间、`@autotune` 遍历调优、`@jit` 编译、`best_result` 取结果。我们知道了 `best_result.kernel` 是编译产物，但**它编译出来的内核体到底写了什么、数据是怎么在 GPU 上搬运的**还没展开。

本讲就只做一件事：**逐行解剖那个 `@T.prim_func` 内核体**，把它拆成五个最小模块——网格映射、显存/寄存器分配、数据搬运、TensorCore 矩阵乘、K 维软件流水线。

学完后你应该能够：

- 说清 `T.Kernel` 如何把输出矩阵切成块、如何把线程块索引绑定到矩阵维度。
- 区分 `alloc_shared`（共享内存）和 `alloc_fragment`（寄存器 fragment）各自的用途。
- 列出内核里每一次 `T.copy` 的「源 → 目的」与方向。
- 解释 `T.gemm` 如何对应一次 TensorCore MMA 累加。
- 说明 `T.Pipelined` 的软流水如何隐藏访存延迟。
- **画出整张数据搬运路径图**，并解释为何写回要走 `C_local → C_shared → C` 两段。

## 2. 前置知识

在进入源码前，先用通俗语言把几个 GPU 概念讲清楚。

### 2.1 GPU 的三级存储层级

| 层级 | 位置 | 容量 | 速度 | 可见范围 |
|---|---|---|---|---|
| **global memory**（全局/显存） | DRAM（如 HBM） | 几十 GB | 最慢（最远） | 所有线程 |
| **shared memory**（共享内存） | 片上 SRAM | 每个 SM 几十 KB | 很快 | 同一个线程块内的所有线程 |
| **register / fragment**（寄存器） | 每个线程私有 | 每线程几百个 | 最快 | 仅当前线程 |

矩阵乘的典型打法就是「**global 太慢、register 太小，所以用 shared 做中转**」：先把数据从 global 成块搬到 shared，再让所有线程协作地从 shared 取数、在 register 里累加。

### 2.2 线程、线程块、网格

- **grid（网格）**：一次 kernel 启动的所有线程块的集合，可以是一维/二维/三维。
- **block / threadblock（线程块）**：grid 里的一个单元，内含若干线程，共享一段 shared memory。
- 本讲的内核把输出矩阵 C 切成很多 (block_M, block_N) 的小块，**每个线程块负责算其中一块**。

### 2.3 Tensor Core 与分块矩阵乘

普通 CUDA Core 一次算一个标量乘加；**Tensor Core** 一次算一个小矩阵乘（例如 int8 输入的 MMA 指令），是现代 GPU 算矩阵乘的主力单元。要把大矩阵乘喂给 Tensor Core，就得把它切成「块」：每个线程块加载 A、B 的子块到 shared，再反复调用 Tensor Core 做小矩阵乘并累加。

### 2.4 本讲用到的数学记号

设输入 \(A\in\mathbb{Z}^{M\times K}\)、\(B\in\mathbb{Z}^{N\times K}\)，本内核计算

\[
C = A\,B^{\top}\in\mathbb{Z}^{M\times N}.
\]

注意 \(B\) 被声明成 \((N,K)\)（K 是最后一维），转置在 `T.gemm` 内部完成。整次乘法的浮点/整型运算量为 \(2MNK\)（承接 u2-l4）。

> **承接 u3-l8 的两个提醒（以代码为准）**：
> 1. 本文件注释写「half-precision」，但代码里 `dtype = "int8"`、`accum_dtype = "int32"`，且驱动脚本 `benchmark_tilelang_matmul.sh` 实际只跑 `int8 int8 int32 int32` 一组——**真实路径是 int8**。本讲按 int8 讲解。
> 2. `print(f"Best latency (s): ...")` 标的是 `(s)`，但下游 `extract_benchmark_results.py` 把它打印成 `ms`——**真实单位是毫秒**。

## 3. 本讲源码地图

本讲只读一个文件，但会从三个角度反复看它：

| 文件 | 作用 | 本讲关注 |
|---|---|---|
| [benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | TileLang 块级 GEMM 内核 + autotune 驱动 | 内核体 `main`（L191–L246） |
| [benchmark_tilelang_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) | 驱动 shell：枚举 shape × dtype 跑内核 | 确认真实跑的是 int8/int32（L24–L28） |
| [extract_benchmark_results.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/extract_benchmark_results.py) | 从日志正则提取 latency | 确认 `Best latency` 单位是 ms（L29–L31） |

内核体被包在 `kernel()` 函数内部的 `@T.prim_func def main(...)` 里（[L191–L196](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L191-L196)）。`@jit` 装饰器（上一讲讲过）负责把这段声明式代码编译成 GPU 代码，编译产物就是上一讲的 `best_result.kernel`。本讲只看这段 `main` 的内部。

---

## 4. 核心概念与源码讲解

### 4.1 T.Kernel：网格映射

#### 4.1.1 概念说明

`T.Kernel` 是 TileLang 用来声明「**启动一个多大的网格、线程块索引怎么取**」的构造。它回答两个问题：

1. **网格有多大**：输出矩阵要被切成多少个块，每个块由一个线程块负责。
2. **索引怎么绑**：`with T.Kernel(...) as (bx, by):` 里的 `bx`、`by` 就是当前线程块在各网格维度上的下标，后面用它们定位「我这一块要算 C 的哪一部分、要从 A/B 取哪一片」。

#### 4.1.2 核心流程

把输出 \(C\in\mathbb{Z}^{M\times N}\) 切成 \((block_M, block_N)\) 的子块，则两个方向的块数为：

\[
\text{grid}_N = \lceil N / block_N\rceil,\qquad \text{grid}_M = \lceil M / block_M\rceil.
\]

本内核把网格写成 `(grid_N, grid_M)`，并把第一个下标 `bx` 绑定到 N 方向（C 的**列块**），第二个下标 `by` 绑定到 M 方向（C 的**行块**）。于是线程块 `(by, bx)` 负责的输出子块是：

\[
C\,[\,by\cdot block_M:(by{+}1)\cdot block_M,\;\; bx\cdot block_N:(bx{+}1)\cdot block_N\,].
\]

#### 4.1.3 源码精读

[benchmark_tilelang_matmul.py:L209-L210](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L209-L210) —— 声明网格并绑定索引：

```python
with T.Kernel(
        T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=thread_num) as (bx, by):
```

- 第一个网格维度 `T.ceildiv(N, block_N)` 绑定给 `bx` → `bx` 是 N 方向块号。
- 第二个网格维度 `T.ceildiv(M, block_M)` 绑定给 `by` → `by` 是 M 方向块号。
- `threads=thread_num` 指定每个线程块的线程数（来自 config，如 128 或 256，见上一讲的搜索空间）。

> **命名小坑**：直觉上 `bx` 像是 x 轴、`by` 像是 y 轴。这里 `bx` 对应 N（横向/列），`by` 对应 M（纵向/行）。后面 `T.copy(A[by*block_M, ...])` 用 `by` 取 A 的行块、`C[by*block_M, bx*block_N]` 用 `(by,bx)` 定位 C 子块——**全靠这套绑定保持一致**。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认网格维度与索引绑定的对应关系。
2. **步骤**：在内核里搜索 `bx`、`by` 出现的全部位置（共 4 处：`T.Kernel` 绑定、`A[by*block_M,...]`、`B[bx*block_N,...]`、`C[by*block_M, bx*block_N]`）。
3. **观察**：每一次出现都用「M 维用 by、N 维用 bx」。
4. **预期**：你能口述「by 永远乘 block_M、bx 永远乘 block_N」，且没有任何一处混用。
5. 待本地验证（可选）：若有 GPU，把 `block_M` 调小，用 `print` 或 `get_kernel_source()` 观察生成的网格维度。

#### 4.1.5 小练习与答案

**练习 1**：`bx` 绑定到哪个矩阵维度？为什么 `T.copy(A[by*block_M, ...])` 用的是 `by` 而不是 `bx`？

> **答**：`bx` 绑定 N 维（列块），`by` 绑定 M 维（行块）。A 的形状是 \((M,K)\)，要取 A 的「行块」必须沿 M 维偏移，所以用 `by`。

**练习 2**：当 \(M=N=8192\)、\(block_M=block_N=128\) 时，整个 grid 有多少个线程块？

> **答**：\(\lceil8192/128\rceil\times\lceil8192/128\rceil = 64\times64 = 4096\) 个。

---

### 4.2 alloc_shared / alloc_fragment：显存与寄存器分配

#### 4.2.1 概念说明

进入 `T.Kernel` 之后，线程块要先**申请好工作中要用的缓冲区**。TileLang 提供两种分配：

- `T.alloc_shared(shape, dtype)`：在 **shared memory** 里开一块，**块内所有线程都能访问**，用来暂存从 global 搬来的子块，也作为 `T.gemm` 的输入和最终写回的中转。
- `T.alloc_fragment(shape, dtype)`：在 **每个线程的寄存器**里开一块，是**线程私有**的，主要用来放累加器 `C_local`。

此外，累加器在进入 K 循环累加之前必须先清零——这就是 `T.clear(C_local)` 的作用。

#### 4.2.2 核心流程

每个线程块申请四个缓冲：

| 缓冲 | 分配方式 | 形状 | dtype | 用途 |
|---|---|---|---|---|
| `A_shared` | shared | \((block_M, block_K)\) | int8 | A 的当前 K 子块 |
| `B_shared` | shared | \((block_N, block_K)\) | int8 | B 的当前 K 子块 |
| `C_local` | fragment（寄存器） | \((block_M, block_N)\) | **int32** | 累加器（私有分布） |
| `C_shared` | shared | \((block_M, block_N)\) | int32 | 写回前的中转 |

关键设计：**累加器 `C_local` 用 `accum_dtype`（int32）而不是 `dtype`（int8）**。因为两个 int8 相乘再累加很多次，结果会远超 int8 的表示范围，必须用高精度整数承载，否则溢出导致结果错误。

#### 4.2.3 源码精读

[benchmark_tilelang_matmul.py:L213-L219](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L213-L219) —— 四块缓冲分配：

```python
A_shared = T.alloc_shared((block_M, block_K), dtype)          # A 子块
B_shared = T.alloc_shared((block_N, block_K), dtype)          # B 子块
C_local  = T.alloc_fragment((block_M, block_N), accum_dtype)  # 累加器(寄存器)
C_shared = T.alloc_shared((block_M, block_N), accum_dtype)    # 写回中转
```

[benchmark_tilelang_matmul.py:L225](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L225) —— 清零累加器：

```python
T.clear(C_local)
```

> 中间夹了一行 [L222](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L222) 的 `T.use_swizzle(...)`（栅格化优化开关），它和 `policy` 一起属于「swizzle 与 warp 策略」，是 u3-l11 的主题，本讲先跳过其细节。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：弄清四个缓冲的存储层级与精度选择。
2. **步骤**：把 L213–L219 四行的「分配方式 / 形状 / dtype」抄成一张表，对照 [L188-L189](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L188-L189) 的 `dtype="int8"`、`accum_dtype="int32"`。
3. **观察**：只有 `C_local` 用 `alloc_fragment` 且用 `accum_dtype`；其余三个都在 shared。
4. **预期**：你能解释「为什么只有累加器是寄存器、且是 int32」。

#### 4.2.5 小练习与答案

**练习 1**：四个缓冲里哪些在 shared memory？哪些在 fragment？`C_local` 为什么用 `accum_dtype`？

> **答**：`A_shared`、`B_shared`、`C_shared` 在 shared；`C_local` 在 fragment。`C_local` 是累加器，要把很多对 int8 的乘积加起来，结果会很大，必须用 int32 才不溢出，所以用 `accum_dtype`。

**练习 2**：如果删掉 `T.clear(C_local)`，结果会怎样？

> **答**：`C_local` 未初始化，里面是垃圾值，累加会从一个未定义初值开始，最终 `C` 完全错误。所以累加前必须 `T.clear`。

---

### 4.3 T.copy：全局 ↔ 共享 ↔ 寄存器的搬运

#### 4.3.1 概念说明

`T.copy(src, dst)` 是 TileLang 的「**成块搬运**」原语：把一块数据从 `src` 拷到 `dst`，形状由 `dst`（或 `src` 的切片）决定。它能跨存储层级工作，方向靠「源」和「目的」的层级自动判断：

- **global → shared**：进 K 循环时把 A、B 的子块从显存搬到共享内存（协作加载，合并访存）。
- **fragment → shared**：算完之后把寄存器里的累加结果汇总到共享内存。
- **shared → global**：最后把结果合并写回显存。

#### 4.3.2 核心流程

本内核一共有 **4 次** `T.copy`，按时间顺序：

1. K 循环内：`A[global] → A_shared`
2. K 循环内：`B[global] → B_shared`
3. K 循环外（写回）：`C_local[fragment] → C_shared`
4. 写回：`C_shared → C[global]`

K 循环内的两次（1、2）每轮 k 重复一次；写回的两次（3、4）只在循环结束后做一次。

**为何写回要两段（`C_local → C_shared → C`），而不是直接 `C_local → C`？**

- `C_local` 是 fragment（寄存器），其数据按 **MMA 指令的输出片段布局**分布在各线程上，并不是一段连续的二维排列。
- 想往 global 写得快，需要**合并写（coalesced store）**：相邻线程写相邻地址，凑成一个完整的事务。
- 先把各线程的片段汇总到 `C_shared`（一块连续二维 shared），就能再做**一次合并的** `shared → global` 拷贝，吞吐最高。
- 这是 CUTLASS/TileLang 风格「shared memory epilogue」的通用做法。（**注意**：这是社区通用的设计层解释；本文件的注释只说「copy them back to global memory C」，并未直接给出原因。）

#### 4.3.3 源码精读

循环内的两次加载（[L230](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L230)、[L232](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L232)）：

```python
T.copy(A[by * block_M, k * block_K], A_shared)   # global -> shared
T.copy(B[bx * block_N, k * block_K], B_shared)   # global -> shared
```

`A[by*block_M, k*block_K]` 是一个二维偏移，表示「从 A 的第 `by*block_M` 行、第 `k*block_K` 列开始」取一片，片大小由目的 `A_shared` 的形状 \((block_M, block_K)\) 决定。

写回的两段（[L243-L244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L243-L244)）：

```python
T.copy(C_local, C_shared)                          # fragment -> shared
T.copy(C_shared, C[by * block_M, bx * block_N])    # shared -> global
```

> 注意 `C_local → C_shared` 这一段没有写偏移，因为整个 `C_local`（\((block_M,block_N)\)）就是要整块搬；而 `C_shared → C` 要用 `(by, bx)` 定位写到 C 的哪一块。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：把内核里所有 `T.copy` 整理成「源 → 目的 + 方向」表。
2. **步骤**：在文件中数出全部 `T.copy(`，逐一标注源缓冲层级、目的缓冲层级、方向。
3. **观察**：你会得到 4 行表（见 4.3.2）。
4. **预期**：能说出哪两段在循环内、哪两段在循环外。
5. 待本地验证（可选）：跑一次内核，用 `best_result.kernel.get_kernel_source()`（见 [L275](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L275)）查看生成的 CUDA 代码里对应的 global/shared store 指令。

#### 4.3.5 小练习与答案

**练习 1**：列出内核里所有 `T.copy` 的 (源, 目的) 及方向。

> **答**：
> - `A[global] → A_shared`（global→shared）
> - `B[global] → B_shared`（global→shared）
> - `C_local[fragment] → C_shared`（fragment→shared）
> - `C_shared → C[global]`（shared→global）

**练习 2**：为什么写回走 `C_local → C_shared → C` 两段，而不是直接 `C_local → C`？

> **答**：`C_local` 是寄存器片段，数据按 MMA 输出布局分散在各线程，直接写 global 难以合并。先汇总到 `C_shared`（连续二维），再一次合并写回 global，吞吐更高。这是设计层原因，文件注释未直接说明。

---

### 4.4 T.gemm：TensorCore MMA 累加

#### 4.4.1 概念说明

`T.gemm(A, B, C, transpose_B=..., policy=...)` 是 TileLang 的「**块级矩阵乘累加**」原语，它把一次小矩阵乘映射到 **Tensor Core 的 MMA 指令**（int8 输入、int32 累加）。它做的是**累加**而非赋值：

\[
C \leftarrow C + A\,B^{\top}.
\]

这是「块级」抽象：你只需声明三个缓冲和是否转置，TileLang 自动把它 lowering 成具体的 MMA 指令序列，不用手写 warp 切分和片段布局。

#### 4.4.2 核心流程

本内核里：

\[
\texttt{C\_local} \;\leftarrow\; \texttt{C\_local} \;+\; \texttt{A\_shared} \cdot \texttt{B\_shared}^{\top}.
\]

形状核对（注意 `transpose_B=True`）：

\[
\underbrace{\texttt{A\_shared}}_{(block_M,\,block_K)}
\cdot
\underbrace{\texttt{B\_shared}^{\top}}_{(block_K,\,block_N)}
\;=\;
\underbrace{(block_M,\,block_N)}_{\texttt{C\_local}}.
\]

每次 `T.gemm` 贡献的运算量为 \(2\cdot block_M\cdot block_N\cdot block_K\)，K 维一共 \(\lceil K/block_K\rceil\) 次，加起来正好 \(2MNK\)（每块的 M、N 维）。

**为什么 B 声明成 \((N,K)\) 而不是 \((K,N)\)？** 因为这样 A、B 的 K 都是连续（最后一）维，K 维规约时两矩阵的 global 加载都能合并访存；转置交给 MMA 指令在内部完成，不增加访存代价。

#### 4.4.3 源码精读

[benchmark_tilelang_matmul.py:L235-L241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L235-L241)：

```python
T.gemm(
    A_shared,
    B_shared,
    C_local,
    transpose_B=True,   # C_local += A_shared @ B_shared^T
    policy=policy,       # warp 切分策略（详见 u3-l11）
)
```

- `transpose_B=True`：把第二个矩阵转置后再乘，对应 \(A\,B^{\top}\)。
- `policy=policy`：决定 warp 如何切分这个块（如 `GemmWarpPolicy.Square`），属于 u3-l11 的内容。
- 这一行会被 lowering 成 int8 Tensor Core 的 MMA 指令，结果累加进 `C_local`。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：核对 `T.gemm` 的形状与累加语义。
2. **步骤**：取一个 config（如 `block_M=block_N=block_K=128`），写出一次 `T.gemm` 的三个张量形状与贡献的 FLOPs。
3. **观察**：`A_shared(128,128) @ B_shared(128,128)^T → C_local(128,128)`，每次贡献 \(2\cdot128^3\approx 4.19\times10^6\) 次运算。
4. **预期**：当 \(K=8192\)、`block_K=128` 时，K 循环跑 64 次 `T.gemm`，累加得到该块的完整结果。
5. 待本地验证（可选）：把 `transpose_B` 改成 `False`（并把 B 声明改成 \((K,N)\)）观察结果是否仍正确——这会帮你理解转置在数学上的等价关系（**示例代码，非项目原有**）。

#### 4.4.5 小练习与答案

**练习 1**：`transpose_B=True` 在数学上对应什么？为什么 B 要声明成 \((N,K)\)？

> **答**：对应 \(C \leftarrow C + A\,B^{\top}\)。B 声明成 \((N,K)\) 使 K 为连续维，与 A 一致，K 维规约时两矩阵加载都能合并访存；转置在 MMA 内部完成。

**练习 2**：一次 `T.gemm` 算的是多大的矩阵乘？贡献多少运算量？

> **答**：\((block_M,block_K)\@(block_K,block_N)=(block_M,block_N)\)，贡献 \(2\cdot block_M\cdot block_N\cdot block_K\) 次乘加运算。

---

### 4.5 T.Pipelined：K 维软件流水线

#### 4.5.1 概念说明

朴素的做法是「加载第 k 块 → 算第 k 块 → 加载第 k+1 块 → 算第 k+1 块」，加载和计算**串行**，访存延迟白白浪费。**软件流水线（software pipelining）**用多份共享内存缓冲，让「算第 k 块」和「预取第 k+1 块」**重叠**起来，把访存延迟藏在计算里。

TileLang 用 `for k in T.Pipelined(range, num_stages=N)` 表达这件事：`num_stages` 就是流水线级数（也对应多缓冲的份数）。

#### 4.5.2 核心流程

把 K 维切成 \(\lceil K/block_K\rceil\) 段，对每段重复「取 A、取 B、gemm 累加」：

```
for k in [0 .. ⌈K/block_K⌉):
    if num_stages >= 1: 预取下一块的 A/B 到额外 shared 缓冲   # 与下面计算重叠
    C_local += A_shared_k @ B_shared_k^T                      # TensorCore 计算
```

`num_stages` 的含义：

- `num_stages=0`：不做软流水，单缓冲，加载与计算串行。
- `num_stages=1`：双缓冲，加载第 k+1 块与计算第 k 块重叠。
- `num_stages=2/3`：更深流水，更多缓冲、更彻底地隐藏延迟。

代价：每多一级，就要多一份 `A_shared`/`B_shared`，共享内存占用随级数线性增长。所以搜索空间里 `num_stages ∈ {0,1,2,3}`（见上一讲 `get_configs`）。

#### 4.5.3 源码精读

[benchmark_tilelang_matmul.py:L228-L241](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228-L241) —— K 维流水线循环，包住两次 `T.copy` 加载和一次 `T.gemm`：

```python
for k in T.Pipelined(T.ceildiv(K, block_K), num_stages=num_stages):
    T.copy(A[by * block_M, k * block_K], A_shared)   # 取 A 的第 k 子块
    T.copy(B[bx * block_N, k * block_K], B_shared)   # 取 B 的第 k 子块
    T.gemm(A_shared, B_shared, C_local,
           transpose_B=True, policy=policy)          # C_local += A_k @ B_k^T
```

- `T.ceildiv(K, block_K)`：K 维块数，即循环总轮数。
- `num_stages=num_stages`：流水线级数，来自 config。
- 循环体被 `T.Pipelined` 包住后，TileLang 自动把「加载」和「gemm」重排成可重叠的多缓冲流水。

#### 4.5.4 代码实践（源码阅读型）

1. **目标**：理解 `num_stages` 对结构与共享内存占用的影响。
2. **步骤**：在上一讲 `get_configs` 的 `else` 分支里找到 `num_stages = [0, 1, 2, 3]`（[L78](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L78)），对照本节的循环结构。
3. **观察**：`num_stages` 越大，`A_shared`/`B_shared` 实际占用的 shared memory 越多。
4. **预期**：能口述「`num_stages=0` 时加载与计算串行；`num_stages=2` 时有三块缓冲在轮转」。
5. 待本地验证（可选）：跑 `--with_roller` 与不带 roller 两种模式，比较最终选出的 `num_stages`（见 [L280](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L280) 打印的 `Best config`）。

#### 4.5.5 小练习与答案

**练习 1**：`num_stages=0` 与 `num_stages=2` 的区别是什么？

> **答**：`num_stages=0` 不做软流水，单缓冲，加载与计算串行；`num_stages=2` 用多份 shared 缓冲，加载第 k+1 块与计算第 k 块重叠，隐藏访存延迟，但占用更多 shared memory。

**练习 2**：为什么 `num_stages` 不能无限大？

> **答**：每多一级就要多一份 `A_shared`/`B_shared` 缓冲，shared memory 占用线性增长，受 SM 的 shared memory 容量限制；所以搜索空间只取 `{0,1,2,3}`。

---

## 5. 综合实践：画出块级 GEMM 的完整数据搬运路径

**任务**：把本讲五个模块串起来，整理出整张「缓冲分配 + 搬运 + 计算」表，并画出数据流。

### 步骤 1：缓冲分配表

根据 [L213-L219](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L213-L219)，填写：

| 缓冲 | 层级 | 形状 | dtype | 谁负责写 | 谁负责读 |
|---|---|---|---|---|---|
| `A_shared` | shared | (block_M, block_K) | int8 | `T.copy(A→A_shared)` | `T.gemm` |
| `B_shared` | shared | (block_N, block_K) | int8 | `T.copy(B→B_shared)` | `T.gemm` |
| `C_local` | fragment | (block_M, block_N) | int32 | `T.gemm` 累加 / `T.clear` | `T.copy(C_local→C_shared)` |
| `C_shared` | shared | (block_M, block_N) | int32 | `T.copy(C_local→C_shared)` | `T.copy(C_shared→C)` |

### 步骤 2：搬运与计算顺序表

根据 [L228-L244](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L228-L244)：

| 顺序 | 语句 | (源 → 目的) | 方向 | 何时执行 |
|---|---|---|---|---|
| 0 | `T.clear(C_local)` | — | 清零 | 循环前一次 |
| 1 | `T.copy(A[...], A_shared)` | global→shared | 加载 | 循环内每轮 |
| 2 | `T.copy(B[...], B_shared)` | global→shared | 加载 | 循环内每轮 |
| 3 | `T.gemm(A_shared,B_shared,C_local)` | shared→fragment | 计算（MMA 累加） | 循环内每轮 |
| 4 | `T.copy(C_local, C_shared)` | fragment→shared | 写回中转 | 循环后一次 |
| 5 | `T.copy(C_shared, C[...])` | shared→global | 最终写回 | 循环后一次 |

### 步骤 3：画出数据流图

用文字箭头画出（global 在最外、register 在最内）：

```
global A ──copy──► A_shared (shared) ┐
                                      ├──gemm(MMA)──► C_local (fragment, 累加)
global B ──copy──► B_shared (shared) ┘                    │
   ↑ 循环 K/block_K 次，受 T.Pipelined 软流水重叠          │
                                                          ▼
                                          C_local ──copy──► C_shared (shared) ──copy──► global C
```

### 步骤 4：回答关键问题

**为何写回要走 `C_local → C_shared → C` 两段？**

> `C_local` 是寄存器片段，数据按 MMA 输出布局分散在各线程，直接写 global 难以合并。先汇总到 `C_shared`（连续二维布局），再一次合并写回 global，得到最高的写吞吐。这是 CUTLASS/TileLang 风格「shared memory epilogue」的通用做法（文件注释未直接说明原因，此为设计层解释）。

### 验收标准

- 能凭表与图，向别人讲清「数据从哪进、在哪算、从哪出」。
- 能指出循环内的 3 步（2 次加载 + 1 次 gemm）和循环外的 2 步写回。
- 待本地验证（可选）：用 [benchmark_tilelang_matmul.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.sh) 跑一个 shape（如 `1024 1024 8192`，int8/int32），观察 `Best config` 里 `block_M/block_N/block_K/num_stages` 的实际取值，并对照本讲的表格理解它选了什么样的搬运路径。

---

## 6. 本讲小结

- **网格映射**：`T.Kernel(⌈N/block_N⌉, ⌈M/block_M⌉) as (bx, by)`，`bx` 绑定 N（列块）、`by` 绑定 M（行块），后续所有索引都遵循「M 用 by、N 用 bx」。
- **缓冲分配**：`A_shared`/`B_shared`/`C_shared` 在 shared memory，`C_local` 在 fragment（寄存器）；累加器 `C_local` 用 `accum_dtype`（int32）避免溢出，且进入循环前必须 `T.clear`。
- **数据搬运**：内核共有 4 次 `T.copy`——循环内 2 次 global→shared 加载，循环外 2 次写回（fragment→shared→global）。
- **块级矩阵乘**：`T.gemm(..., transpose_B=True, policy=...)` 做 \(C \leftarrow C + A\,B^{\top}\)，lowering 成 int8 Tensor Core 的 MMA 指令；B 声明成 \((N,K)\) 使 K 连续、访存合并。
- **软件流水线**：`for k in T.Pipelined(...)` 用 `num_stages` 份多缓冲，把加载与 gemm 重叠，隐藏访存延迟，代价是 shared memory 占用随级数增长。
- **以代码为准**：文件注释与代码在精度上有出入（注释说 half-precision、代码是 int8，且 shell 只跑 int8/int32），`Best latency (s)` 实为 ms——读源码时始终以代码与下游脚本为准。

## 7. 下一步学习建议

本讲把内核体拆成了「五要素」，但有两个细节被刻意留到了后面：

- **`T.use_swizzle` 与 `policy=GemmWarpPolicy`**：栅格化（rasterization）如何提升 L2 命中、warp 如何切分块——见 **u3-l11（swizzle、warp 策略与调优旋钮）**。
- **`tilelang.carver` 与 Roller 自动调优**：`with_roller=True` 时搜索空间怎么由 Roller 推导、`num_stages` 等字段如何换算——见 **u3-l10（tilelang.carver 与 Roller 自动调优）**。

读完 u3-l10、u3-l11 后，建议回到本讲的「综合实践」表格，把 `Best config` 里每个字段（`block_M/N/K`、`num_stages`、`thread_num`、`policy`、`enable_rasteration`）逐一对应回内核里的某个决策，完成对块级 GEMM 的闭环理解。之后进入第 4 单元，看多精度与量化矩阵乘如何复用这套骨架。
