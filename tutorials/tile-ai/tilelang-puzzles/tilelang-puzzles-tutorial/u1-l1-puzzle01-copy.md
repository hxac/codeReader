# Puzzle 01 Copy：第一个 Kernel 与并行

## 1. 本讲目标

本讲带你亲手跑通第一个 TileLang kernel。我们用最简单的「把一个张量复制一份」这件事，串起 GPU 编程里最关键的三个并行层级：

- 单线程串行 copy —— 先让程序「能跑」；
- 多线程 copy —— 在一个 block 里用很多线程并行；
- 多 block 分块 copy —— 用很多 block 占满整张 GPU。

学完后你应当能够：

1. 读懂 `puzzles/01-copy.py` 里已经写好的串行版，并说清它每一段在做什么；
2. 理解 `T.copy` 作为一个 TileOp 所具备的「自动并行 + 自动向量化」能力；
3. 独立补全两个 TODO：`tl_copy_1d_multi_threads` 和 `tl_copy_1d_parallel`，并用 `test_puzzle` 验证、用 `bench_puzzle` 对比三种实现的耗时。

## 2. 前置知识

本讲假定你已经读过 [u1-l1（项目总览）](u1-l1-project-overview.md) 和 [u1-l2（Kernel 骨架与测试框架）](u1-l2-kernel-anatomy-and-test-framework.md)，知道：

- 一个 TileLang kernel 由 `@tilelang.jit` 装饰，函数体分「host 声明」和「device 计算」两段，最后 `return` 输出张量；
- host 段用 `T.const("N")` 声明符号维度（在 `compile` 时绑定）、用 `T.Tensor` 标注输入、用 `T.empty` 分配输出；
- `T.Kernel(各维 block 数, threads=N) as (块索引...)` 决定启动配置；
- `test_puzzle` 会按「输出张量放最后」的约定自动造随机输入、跑 torch 参考实现、用 `torch.allclose` 比对。

这里只补两个本讲要用到的 GPU 基础概念（更深的内存层级留到 [u2-l2](u2-l2-memory-hierarchy-fragments.md)）：

- **线程（thread）**：GPU 上最小的执行单位。一个 kernel 里的线程数量级通常是成千上万。
- **线程块（block）**：若干线程组成一个 block。同一个 block 内的线程可以共享「shared memory」、可以用屏障（`__syncthreads`）同步；**不同 block 之间不能直接同步**。
- **网格（grid）**：一个 kernel 启动的所有 block 的集合。GPU 由多个「流式多处理器（SM）」组成，block 会被调度到不同 SM 上，所以「用更多 block」≈「用满更多 SM」。

一句话直觉：**线程让一个 block 内部变快，block 让整张 GPU 都被用起来。** 本讲就是把这句直觉写进代码。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py) | **题目**。已给出串行版 `tl_copy_1d_serial`，多线程版与并行版的函数体是 `# TODO`，需要你补全。 |
| [ans/01-copy.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py) | **参考答案**，与题目一一对应，可用来对照。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | 提供 `test_puzzle`（验证正确性）与 `bench_puzzle`（CUDA Event 计时）。本讲主要用它的计时逻辑做对比。 |

> 约定：本讲引用行号对应当前 HEAD `de37efb`。如果后续代码变动导致行号对不上，请以函数名为准定位。

## 4. 核心概念与源码讲解

我们要实现的数学定义非常简单：

\[
\text{for } i \in [0, N):\quad B[i] = A[i]
\]

难点不在算法，而在「如何把这个循环映射到 GPU 的成千上万个线程上」。下面按三个最小模块逐步演进。

### 4.1 单线程串行 copy

#### 4.1.1 概念说明

这是起点：先不管并行，让一个线程把整件事做完。它的价值有两个：

1. **建立骨架**：确认 `@tilelang.jit`、`T.const`、`T.Tensor`、`T.empty`、`T.Kernel`、`return` 这一套声明能正常工作。
2. **观察 `T.copy` 的「自动向量化」**：哪怕只有 1 个线程，TileLang 编译器也不会真的一条一条搬，而是把它降低成一个带位宽向量化的串行循环（例如每条指令搬 128 位 = 8 个 float16）。这正是 `T.copy` 作为「TileOp」的威力——你只描述「把 A 搬到 B」，编译器决定「怎么搬」。

#### 4.1.2 核心流程

串行版的执行流程：

1. `compile(N=1024)` 把符号维度 `N` 绑定为 1024；
2. 启动 **1 个 block、每 block 1 个线程**，共 1 个线程；
3. 线程内执行 `T.copy(A, B)`：编译器生成一个串行循环，循环里用向量化指令把 A 的内容搬到 B；
4. kernel 结束，`return B` 把结果交回 host。

启动配置的对应关系：

| 配置 | 含义 | 串行版的取值 |
| --- | --- | --- |
| 第一个位置参数 | grid 里 block 的数量 | `1` |
| `threads=` | 每个 block 的线程数 | `1` |
| 总线程数 | blocks × threads | 1 |

#### 4.1.3 源码精读

串行版完整代码见 [puzzles/01-copy.py:62-79](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L62-L79)：

```python
@tilelang.jit
def tl_copy_1d_serial(A):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B = T.empty((N,), T.float16)

    with T.Kernel(1, threads=1) as _:
        T.copy(A, B)

    return B
```

几个要点：

- [puzzles/01-copy.py:71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L71) 的 `T.Kernel(1, threads=1) as _`：第一个参数 `1` 表示只启动 1 个 block；`threads=1` 表示这个 block 里只有 1 个线程。`as _` 把唯一的 block 索引绑定到一个我们用不到的名字 `_`（因为只有 1 个 block，索引永远是 0，不需要用）。
- [puzzles/01-copy.py:77](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L77) 的 `T.copy(A, B)`：这是 TileLang 内置的 **TileOp**。它的注释（[puzzles/01-copy.py:72-76](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L72-L76)）明确说：它会自动利用 block 内可用线程做并行拷贝（含自动并行与向量化）；当只有 1 个线程时，它被降低成「带一定位宽向量化的串行拷贝（如每次 128 位）」。

`N` 是怎么拿到具体值的？看 `run_copy_1d_serial`（[puzzles/01-copy.py:82-85](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L82-L85)）：它调用 `test_puzzle(tl_copy_1d_serial, ref_copy_1d, {"N": N})`，而 `test_puzzle` 内部执行 `puzzle_tl.compile(N=1024)`（见 [common/utils.py:76](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L76)），于是符号维度 `N` 在编译期被绑定为 1024。

#### 4.1.4 代码实践

**目标**：先确认串行版能正确运行，并建立「单线程也能跑完」的直觉。

**步骤**：

1. 在仓库根目录运行：
   ```bash
   python3 puzzles/01-copy.py
   ```
   或只跑串行部分：
   ```bash
   python3 -c "from puzzles import __init__" 2>/dev/null; python3 -c "
   import sys; sys.path.insert(0, '.')
   import importlib.util, types
   spec = importlib.util.spec_from_file_location('p01', 'puzzles/01-copy.py')
   m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
   m.run_copy_1d_serial()
   "
   ```
2. 观察输出里的 `✅ Results match: True`。

**需要观察的现象**：

- 即使 `threads=1`，结果依然与 `A.clone()` 完全一致——说明单线程版本「正确但不快」。
- 终端会打印一行 `✅ Results match: True`。

**预期结果**：验证通过（✅）。

> 计时数字本讲不预设：`run_copy_1d_serial` 里没有 bench，所以这一步不会打印耗时。性能对比放到 4.3 与综合实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `T.copy` 在只有 1 个线程时仍能搬完整个张量？

**参考答案**：因为 TileLang 编译器把 `T.copy` 降低成一个串行循环，并在循环里使用位宽向量化（如每次 128 位 = 8 个 float16），所以单个线程靠「向量化」也能逐批搬完所有元素，只是没有用到多线程并行，速度慢。

**练习 2**：`T.const("N")` 中的 `N` 在什么时候获得具体数值？

**参考答案**：在编译期。当 `test_puzzle` 调用 `kernel.compile(N=1024)` 时，符号维度 `N` 被绑定为 1024；编译后的 kernel 就「知道」自己要处理 1024 个元素。

---

### 4.2 多线程 copy（threads）

#### 4.2.1 概念说明

串行版只用 1 个线程，等于把 GPU 上其余成百上千个计算单元晾在一边。最直接的提速办法：**在同一个 block 里开更多线程**。

这里有一个关键认知：`T.copy` 是一个 TileOp，它会**自动把工作分给 block 内的所有线程**。所以从串行版切到多线程版，函数体 `T.copy(A, B)` 一行都不用改——只要把 `threads=1` 改大，编译器自然会把这些元素分配给更多线程并行搬运。这正是「声明式」DSL 的好处：你说 *做什么*（搬 A 到 B），编译器决定 *怎么做*（分多少线程、是否向量化）。

#### 4.2.2 核心流程

多线程版的执行流程：

1. `compile(N=...)` 绑定 `N`（本例 N 较大，`1024 * 256`）；
2. 启动 **1 个 block、每 block 256 个线程**，共 256 个线程；
3. `T.copy(A, B)` 把 N 个元素**自动划分**给这 256 个线程（叠加向量化），每个线程负责一段连续元素；
4. kernel 结束，返回 B。

总线程数对照：

| 配置 | 串行版 | 多线程版 |
| --- | --- | --- |
| blocks | 1 | 1 |
| threads | 1 | 256 |
| 总线程 | 1 | 256 |

注意：这里**仍然只有 1 个 block**。也就是说所有线程都跑在**同一个 SM** 上，受限于单个 block / 单个 SM 的资源上限（线程数上限、寄存器、shared memory 等）。要突破这个上限，需要 4.3 的多 block。

#### 4.2.3 源码精读

题目里的多线程版函数体是空的（[puzzles/01-copy.py:100-109](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L100-L109)）：

```python
@tilelang.jit
def tl_copy_1d_multi_threads(A):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B = T.empty((N,), T.float16)

    # TODO: Implement this function

    return B
```

参考答案（[ans/01-copy.py:100-111](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L100-L111)）只加了两行：

```python
    with T.Kernel(1, threads=256) as _:
        T.copy(A, B)
```

- [ans/01-copy.py:108](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L108)：`threads=256` 是唯一与串行版的实质差别——给 block 配了 256 个线程。
- [ans/01-copy.py:109](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L109)：`T.copy(A, B)` 与串行版**完全相同**，但行为变了——现在它会把工作摊到 256 个线程上。

题目的提示（[puzzles/01-copy.py:94-96](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L94-L96)）也鼓励你试试 128 或 256，比较加速比。

#### 4.2.4 代码实践

**目标**：补全 `tl_copy_1d_multi_threads`，验证正确，并把它和串行版放在一起 bench。

**步骤**：

1. 把上面那两行 `with T.Kernel(...)` / `T.copy(A, B)` 填进 `puzzles/01-copy.py` 的 `tl_copy_1d_multi_threads`；
2. 运行 `run_copy_1d_multi_threads`（[puzzles/01-copy.py:112-132](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L112-L132)），它会先 `test_puzzle` 验证，再分别 bench 串行版（含 torch 基线）和多线程版。

**需要观察的现象**：

- `✅ Results match: True`——多线程版结果与 `A.clone()` 一致；
- 三行耗时：`Torch time`、`TL Serial time`、`TL Multi-threads time`。

**预期结果**：

- 正确性通过；
- 耗时排序（定性，具体数字**待本地验证**）：`TL Multi-threads` 应明显快于 `TL Serial`（1 个线程 vs 256 个线程）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `threads=256` 改回 `threads=1`，多线程版会退化成什么？

**参考答案**：退化成和串行版几乎一样的单线程拷贝——`T.copy` 会因为只有 1 个可用线程而回到串行向量化模式，性能与 `tl_copy_1d_serial` 相当。

**练习 2**：同一个 block 内的线程能做哪件「跨 block 的线程做不到」的事？

**参考答案**：同 block 线程可以访问同一块 shared memory、可以通过屏障（`__syncthreads`）彼此同步与通信；不同 block 的线程彼此独立，无法直接同步，也不能访问对方的 shared memory。这也是为什么「需要协作」的计算通常放在一个 block 内。

---

### 4.3 多 block 分块 copy（pid_n + BLOCK_N）

#### 4.3.1 概念说明

多线程版已经很快，但它**只有 1 个 block**，只能占用 1 个 SM。真实 GPU 有几十个 SM，要让它们都忙起来，就必须启动**很多个 block**。

新的核心概念是 **块索引（block index）**。当我们启动 `G` 个 block 时，每个 block 都需要一个「我是第几号」的标识，才能算出自己该处理数据的哪一段。这个标识就是 `T.Kernel(...)` 里 `as (名字)` 绑定的变量。

> ⚠️ 命名小坑：题目 `puzzles/01-copy.py` 的注释里把这个变量写成 `bx`（[puzzles/01-copy.py:142](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L142)），而参考答案 `ans/01-copy.py` 写成 `pid_n`（[ans/01-copy.py:144](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L144)）。这只是变量名不同，本质都是同一个东西（GPU 里的 `blockIdx`）。本讲统一用 `pid_n`，与参考答案一致。`pid` 是 "program id" 的缩写，沿用自 Triton/TVM 的命名习惯。

#### 4.3.2 核心流程

设每个 block 负责 `BLOCK_N` 个元素，则一共需要 `N // BLOCK_N` 个 block。第 `k` 号 block 负责的区间是：

\[
\text{start}_k = k \cdot \text{BLOCK\_N},\qquad
\text{区间}_k = [\,\text{start}_k,\ \text{start}_k + \text{BLOCK\_N}\,)
\]

| block 编号 pid_n | 负责的数据区间 |
| --- | --- |
| 0 | `[0, BLOCK_N)` |
| 1 | `[BLOCK_N, 2·BLOCK_N)` |
| k | `[k·BLOCK_N, (k+1)·BLOCK_N)` |

执行流程：

1. `compile(N=..., BLOCK_N=...)` 同时绑定符号维度 `N` 和 Python 形参 `BLOCK_N`（都是编译期常量）；
2. 启动 `N // BLOCK_N` 个 block，每个 block 256 个线程；
3. 每个 block 用自己的 `pid_n` 算出区间，对 `A[该区间]` 与 `B[该区间]` 这两个**子张量（tile）**执行 `T.copy`；
4. 所有 block 并发执行（分布到多个 SM），结束后返回 B。

这里出现了一个贯穿后续所有讲义的核心动作——**分块（tiling）**：把整个大问题切成若干等大的小 tile，每个 block 处理一个 tile。所有 GEMM、卷积、注意力都是这个套路。

总线程数对照（N = 1024×256，BLOCK_N = 1024 时）：

| 配置 | 串行版 | 多线程版 | 并行版 |
| --- | --- | --- | --- |
| blocks | 1 | 1 | `N // BLOCK_N` = 256 |
| threads | 1 | 256 | 256 |
| 总线程 | 1 | 256 | 256 × 256 = 65536 |

#### 4.3.3 源码精读

题目里的并行版同样是空函数体（[puzzles/01-copy.py:147-156](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L147-L156)）：

```python
@tilelang.jit
def tl_copy_1d_parallel(A, BLOCK_N: int):
    N = T.const("N")
    A: T.Tensor((N,), T.float16)
    B = T.empty((N,), T.float16)

    # TODO: Implement this function

    return B
```

参考答案（[ans/01-copy.py:149-163](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L149-L163)）：

```python
    with T.Kernel(N // BLOCK_N, threads=256) as pid_n:
        T.copy(
            A[pid_n * BLOCK_N : (pid_n + 1) * BLOCK_N],
            B[pid_n * BLOCK_N : (pid_n + 1) * BLOCK_N],
        )
```

逐行解读：

- **函数签名多了一个 `BLOCK_N: int`**（[puzzles/01-copy.py:148](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L148)）：它是**编译期超参数**（Python 整数形参），在 `compile(BLOCK_N=1024)` 时确定。让编译器在编译时就知道 tile 大小，有利于向量化与展开。
- **`T.Kernel(N // BLOCK_N, ...)`**（[ans/01-copy.py:157](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L157)）：第一个位置参数是 block 数量。因为 `N` 与 `BLOCK_N` 都是编译期已知，`N // BLOCK_N` 在编译期就被算成具体整数（这里是 256），作为 grid 大小。
- **`as pid_n`**：把当前 block 的编号绑定到 `pid_n`。GPU 启动 256 个 block 时，每个 block 拿到一个唯一的 `pid_n ∈ [0, 255)`，这就是它的「身份证」。
- **`A[pid_n * BLOCK_N : (pid_n + 1) * BLOCK_N]`**（[ans/01-copy.py:159](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L159)）：用 Python 切片语法表达「我这一块要读的子张量」。`T.copy` 接收的是两个**形状相同的 tile**，于是 block 内的 256 个线程再把这个 tile 内部并行摊开。

调用方在 `run_copy_1d_parallel`（[puzzles/01-copy.py:159-170](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L159-L170)）里同时传了 `{"N": N, "BLOCK_N": BLOCK_N}`，二者都在 `test_puzzle`/`bench_puzzle` 内部的 `compile(**params)` 被绑定。

#### 4.3.4 代码实践

**目标**：补全 `tl_copy_1d_parallel`，让每个 block 拷贝 `BLOCK_N` 个元素；验证正确并与 torch 比性能。

**步骤**：

1. 把上面那段 `with T.Kernel(N // BLOCK_N, threads=256) as pid_n: T.copy(A[...], B[...])` 填进 `puzzles/01-copy.py` 的 `tl_copy_1d_parallel`；
2. 运行 `run_copy_1d_parallel`，它会先 `test_puzzle`（`{"N": N, "BLOCK_N": BLOCK_N}`）再 `bench_puzzle`（`bench_torch=True`，即同时打印 torch 基线）。

**需要观察的现象**：

- `✅ Results match: True`；
- 两行耗时：`Torch time` 与 `TL Parallel time`。

**预期结果**：

- 正确性通过；
- 由于并行版同时用了「多 block + 多线程」，耗时应优于 4.2 的多线程版（具体数字**待本地验证**）。
- **可选拓展**：把 `BLOCK_N` 从 1024 改成 512 或 2048（保持 `N` 能被整除），观察 `TL Parallel time` 如何变化——这是后续 [u5-l4（性能工程）](u5-l4-performance-and-codegen.md) 的入门体验。

> 排错提示：如果出现结果错位或报错，先检查两点——(1) `N` 是否能被 `BLOCK_N` 整除（题目假设可以，见 [puzzles/01-copy.py:139](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L139)）；(2) 读和写两个切片的区间是否完全一致。

#### 4.3.5 小练习与答案

**练习 1**：如果 `N` 不能被 `BLOCK_N` 整除（例如 N=1000, BLOCK_N=1024），`N // BLOCK_N` 会带来什么问题？

**参考答案**：`1000 // 1024 = 0`，于是 grid 大小为 0，根本没有 block 被启动，B 不会被写入，结果是错的（即便 N≥BLOCK_N 也可能漏搬尾部）。正确做法是用 `T.ceildiv(N, BLOCK_N)` 向上取整来决定 block 数，并在 kernel 内对「超出 N 的部分」做边界判断（这一点会在后续卷积/二维分块讲义里正式用到）。

**练习 2**：block 索引变量的名字是固定的吗？写成 `bx` 还是 `pid_n` 有区别吗？

**参考答案**：没有区别。`T.Kernel(...) as 名字` 里的「名字」只是一个绑定，叫 `pid_n`、`bx` 或 `k` 都行，它都指向同一个运行时量（CUDA 里的 `blockIdx.x`）。本项目参考答案统一用 `pid_n`，遵循 Triton/TVM 的 "program id" 命名习惯。

---

## 5. 综合实践

把三个版本放在一起做一次完整对比，串起本讲全部内容。

**任务**：

1. 确保 `tl_copy_1d_serial`（已给出）、`tl_copy_1d_multi_threads`、`tl_copy_1d_parallel` 三个函数都已实现（后两个由你补全）；
2. 在仓库根目录运行：
   ```bash
   python3 puzzles/01-copy.py
   ```
   它会依次执行 `run_copy_1d_serial` / `run_copy_1d_multi_threads` / `run_copy_1d_parallel`（见 [puzzles/01-copy.py:173-176](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/01-copy.py#L173-L176)）；
3. 收集三种实现的耗时（串行与多线程在 `run_copy_1d_multi_threads` 里一起 bench，并行版在 `run_copy_1d_parallel` 里 bench）；
4. 填一张表（耗时请填你本机实测值）：

| 实现 | blocks | threads | 是否用满多 SM | 实测算时（ms） |
| --- | --- | --- | --- | --- |
| `tl_copy_1d_serial` | 1 | 1 | 否 | _待本地验证_ |
| `tl_copy_1d_multi_threads` | 1 | 256 | 否（仅 1 个 SM） | _待本地验证_ |
| `tl_copy_1d_parallel` | N//BLOCK_N | 256 | 是 | _待本地验证_ |
| torch 基线 `A.clone()` | — | — | — | _待本地验证_ |

**思考题（可选）**：为什么多线程版（256 线程）已经比串行版快很多，但并行版还能再快？答：因为多线程版受限于**单个 block / 单个 SM**；并行版通过增加 block 数把工作散到多个 SM 上，突破了单 SM 的吞吐与资源上限。

> 说明：`bench_puzzle` 的计时方法学（warmup、CUDA Event、`synchronize`、repeats=100）来自 [common/utils.py:109-155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L155)，更系统的讲解在 [u5-l4](u5-l4-performance-and-codegen.md)。

## 6. 本讲小结

- **串行版**先用 1 个 block、1 个线程跑通骨架，并展示了 `T.copy` 的自动向量化——单线程也能搬完，只是慢。
- **`T.copy` 是 TileOp**：你只声明「把 A 搬到 B」，并行与向量化由编译器决定；因此从串行切到多线程，**函数体一行都不用改**，只改 `threads=`。
- **多线程版**在一个 block 内开 256 个线程提速，但受限于单个 SM；同 block 线程可共享 shared memory 与同步，跨 block 不行。
- **多 block 分块版**用 `N // BLOCK_N` 个 block 占满多个 SM，每个 block 用块索引 `pid_n` 算出自己的数据区间 `[pid_n*BLOCK_N : (pid_n+1)*BLOCK_N]`——这是贯穿全册的 **tiling（分块）** 范式。
- **块索引变量名不固定**：题目注释里的 `bx` 与参考答案里的 `pid_n` 是同一回事，都对应 `blockIdx`。
- **编译期超参数**：`BLOCK_N` 作为 Python 形参在 `compile` 时绑定，让编译器在编译期就知道 tile 大小，利于优化。

## 7. 下一步学习建议

本讲你已经掌握了 `T.copy`、threads、blocks、块索引与一维分块。接下来：

- 进入 [u2-l1（Puzzle 02 Vector Add）](u2-l1-puzzle02-vector-add.md)：学习 `T.Parallel` 循环与元素级算术（加法、乘法、ReLU），理解「元素级运算」与 `T.copy` 这类 TileOp 的区别。
- 之后 [u2-l2（GPU 内存层级）](u2-l2-memory-hierarchy-fragments.md) 会正式讲 GPU 三级内存（global/shared/registers）与 `T.alloc_fragment`，并教你看 TileLang 生成的 CUDA 代码（`print_source_code`），把本讲对「自动向量化」的直觉落到生成代码上。
- 建议同时把 `ans/01-copy.py` 通读一遍，对照自己的实现，确认每个 TODO 的写法你都理解了为什么这么写。
