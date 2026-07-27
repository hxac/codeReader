# Puzzle 03 Outer Vector Add：进入二维

## 1. 本讲目标

前两讲我们都在一维张量上打转：Puzzle 01 拷贝、Puzzle 02 元素级加减乘除与 ReLU。本讲正式进入**二维世界**。学完后你应当能够：

- 写出一个**二维** `T.Kernel`，并用两个块索引（`pid_n`、`pid_m`）让每个 block 定位自己负责的二维子矩阵。
- 用**多维** `T.Parallel(i, j)` 循环表达二维元素级计算。
- 理解为什么这里要分配一个二维的寄存器片段 `C_local` 作为中间结果，并用 `T.copy` 把它整块回写到二维输出张量。
- 把 Puzzle 02 学到的「用 fragment 缓存数据、减少显存访问」思路，迁移到二维的「外积式」运算上，并理解其中的数据复用。

## 2. 前置知识

本讲默认你已经学过 **u1-l3（Puzzle 01 Copy）** 和 **u2-l1 / u2-l2（Puzzle 02 与 GPU 内存层级）**。下面几条术语会直接用到，先做个 30 秒回顾：

- **block / grid / 块索引**：GPU 上一个 kernel 由很多个 block 组成 grid；每个 block 用块索引（CUDA 里的 `blockIdx`）来定位自己要处理的数据区间。TileLang 里块索引由 `with T.Kernel(...) as (块索引...)` 提供，位置参数的个数 = grid 的维数 = 块索引变量的个数。
- **`T.Parallel`**：声明「这些迭代之间互不依赖、可以并行」的循环抽象，框架自动把迭代分摊给 block 内的线程。
- **`T.alloc_fragment`**：把「一个 block 内所有线程的寄存器」抽象成一块可像普通 buffer 一样读写的 **fragment（寄存器片段）**；配合 `T.copy` 在 global memory 与 fragment 之间批量搬运（如 `ldg128`），是「把数据尽量留在快的存储里」的关键手段。
- **偏移下标约定**：在 `T.copy` 里写 `A[offset]` 时，TileLang 会从另一端推断这段 tile 有多长，等价于一个切片。这个写法在 Puzzle 02 的内存优化版里已经出现过。

还需要一点线性代数直觉：所谓 **outer（外积式）加法**，就是把一个长度 N 的向量 A 和一个长度 M 的向量 B「张成」一张 N×M 的矩阵，其中第 (i, j) 个元素就是 `A[i] + B[j]`。PyTorch 里用广播（broadcast）一句话就能写出来，本讲我们要把它写成一个二维 GPU kernel。

## 3. 本讲源码地图

本讲只涉及两个对照文件，外加前面讲过的测试框架：

| 文件 | 作用 |
| --- | --- |
| [puzzles/03-outer-vec-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py) | 题目：带 `TODO` 的 `tl_outer_add` 骨架，以及 PyTorch 参考实现 `ref_outer_add`。 |
| [ans/03-outer-vec-add.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py) | 参考答案：完整的二维 kernel 实现。 |
| common/utils.py | `test_puzzle` / `bench_puzzle` 框架（前面讲过，本讲沿用）。 |

题目骨架与参考答案的函数签名、host 声明部分完全一致，唯一区别就是 `# TODO` 处是否填了 `with T.Kernel` 体内的 DSL 代码。

## 4. 核心概念与源码讲解

先看题目本身要我们算什么。题目文档里写得很清楚：输入是一维向量 `A: [N]` 和 `B: [M]`，输出是二维矩阵 `C: [N, M]`，定义是：

[puzzles/03-outer-vec-add.py:38-41](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L38-L41) —— 这是本讲要实现的数学定义：两层循环，`C[i, j] = A[i] + B[j]`。

参考实现用 PyTorch 的广播一行搞定：

[puzzles/03-outer-vec-add.py:45-49](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L45-L49) —— `A[:, None]` 把 A 变成 `(N, 1)`、`B[None, :]` 把 B 变成 `(1, M)`，`torch.add` 广播相加得到 `(N, M)`。**广播的方向就是本讲 kernel 里数据复用的方向**，记住这一点后面会豁然开朗。

而我们要补全的 kernel 骨架长这样：

[puzzles/03-outer-vec-add.py:52-62](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L52-L62) —— 注意三点：①输出 `C` 是二维 `(N, M)`；②有两个编译期超参 `BLOCK_N`、`BLOCK_M`（分块大小）；③`N, M = T.const("N, M")` 一次声明了两个符号维度。

下面按三个最小模块拆开讲。

### 4.1 二维 T.Kernel 与块索引

#### 4.1.1 概念说明

Puzzle 01 里 grid 是一维的：`T.Kernel(N // BLOCK_N, threads=256) as pid_n`，一个块索引 `pid_n` 沿 N 方向切。本讲输出是 N×M 的矩阵，自然要把 grid 升到**二维**：沿 N 方向切 `N // BLOCK_N` 份、沿 M 方向切 `M // BLOCK_M` 份，于是每个 block 负责一个 `BLOCK_N × BLOCK_M` 的子矩阵。

`T.Kernel` 的位置参数就是**每个维度的 block 数**，参数个数 = grid 维数 = `as` 后面块索引变量的个数。写两个位置参数、解包出两个索引，就是二维 grid：

```python
with T.Kernel(N // BLOCK_N, M // BLOCK_M, threads=256) as (pid_n, pid_m):
```

这里 `pid_n` 是 N 方向的块号（0 ~ `N//BLOCK_N - 1`），`pid_m` 是 M 方向的块号。`threads=256` 仍是「每个 block 内的线程数」，和 grid 维数无关——它管的是 block 内部的并行，二维只是多了「block 之间怎么排兵布阵」的一个方向。

> 小提示：题目注释里有时把 N 方向的索引写成 `bx`，答案里写成 `pid_n`，它们是同一个东西（都是 `blockIdx`），只是命名不同。本讲沿用答案的 `pid_n / pid_m` 写法。

#### 4.1.2 核心流程

每个 block 拿到自己的 `(pid_n, pid_m)` 后，第一步永远是算出「我负责的这块数据在整张矩阵里的起点」：

```
n_idx = pid_n * BLOCK_N      # 本 block 在 N 方向的起始下标
m_idx = pid_m * BLOCK_M      # 本 block 在 M 方向的起始下标
```

那么本 block 要处理的输出区间就是：

- N 方向：`[n_idx, n_idx + BLOCK_N)`
- M 方向：`[m_idx, m_idx + BLOCK_M)`

整张 `N × M` 矩阵被切成 `(N // BLOCK_N) × (M // BLOCK_M)` 个互不重叠的子矩阵，每个 block 独立算自己那块。这是 Puzzle 01 「多 block 分块」思想在二维上的直接推广。

#### 4.1.3 源码精读

答案里的二维 `T.Kernel` 与起点计算：

[ans/03-outer-vec-add.py:61-63](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L61-L63) —— 两个位置参数 `(N // BLOCK_N, M // BLOCK_M)` 定义二维 grid，`as (pid_n, pid_m)` 解包出两个块索引；随后把块索引换算成数据下标起点 `n_idx`、`m_idx`。

对照一维的 Puzzle 01 答案 [ans/01-copy.py:157](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/01-copy.py#L157)（`T.Kernel(N // BLOCK_N, threads=256) as pid_n`），可以看出：**多一个维度，就是多给 `T.Kernel` 一个位置参数、多解包一个块索引、多算一个起点下标**，范式完全一致。

#### 4.1.4 代码实践

1. **实践目标**：建立对「二维 grid 一共启动多少个 block」的直觉。
2. **操作步骤**：打开 `puzzles/03-outer-vec-add.py`，找到 `run_outer_add`（[L65-75](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L65-L75)），其中 `N=8192, M=4096, BLOCK_N=1024, BLOCK_M=1024`。手算 grid 维度。
3. **需要观察的现象**：在脑子里画一张 8×4 的 block 网格（N 方向 8 个、M 方向 4 个）。
4. **预期结果**：`N // BLOCK_N = 8`，`M // BLOCK_M = 4`，总共 `8 × 4 = 32` 个 block，每个处理 `1024 × 1024` 个输出元素。
5. 待本地验证：你可以在补全 kernel 后，临时把 `BLOCK_N`/`BLOCK_M` 改小（如 64），重新手算 grid，确认理解无误。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 61 行误写成只有一个位置参数 `with T.Kernel(N // BLOCK_N, threads=256) as (pid_n, pid_m):`，会发生什么？

**答案**：grid 退化为一维（只有一个块索引），但 `as` 却要解包出两个变量，解包数量不匹配会直接报错；即便不报错，也丢失了 M 方向的分块，无法覆盖整张矩阵。

**练习 2**：为什么是 `N // BLOCK_N` 而不是 `N * BLOCK_N`？

**答案**：`N // BLOCK_N` 是「N 维度上能切出多少个 BLOCK_N 大小的块」，即 block 数量；`N * BLOCK_N` 会得到一个远大于 N 的、毫无意义的数。block 数 = 总长 ÷ 每块大小。

---

### 4.2 多维 T.Parallel

#### 4.2.1 概念说明

`T.Parallel` 在 Puzzle 02 里我们已经用过一维形式 `for i in T.Parallel(N)`。它声明「这些迭代互相独立、可以并行」，框架负责把迭代分给 block 内线程，与我们无关。

到了二维，我们要同时沿 i（N 方向）和 j（M 方向）遍历当前 block 的 `BLOCK_N × BLOCK_M` 子矩阵。TileLang 直接支持**多维 `T.Parallel`**：在循环变量和 `T.Parallel(...)` 里各写两个参数即可。

```python
for i, j in T.Parallel(BLOCK_N, BLOCK_M):
    C_local[i, j] = A_local[i] + B_local[j]
```

这等价于两层嵌套循环 `for i in range(BLOCK_N): for j in range(BLOCK_M):`，但写成多维形式后，框架把 `(i, j)` 这个二维迭代空间**作为一个整体**来并行划分，语义上更清晰、也更贴近「这是一块二维 tile」的心智模型。

#### 4.2.2 核心流程

注意循环体里的下标分工——这正是「outer（外积式）」运算的本质：

```
C_local[i, j] = A_local[i] + B_local[j]
                ^^^^^^^^^^   ^^^^^^^^^^
                只依赖 i      只依赖 j
```

- `A_local[i]` 只随 i 变化，与 j 无关 → 对固定的 i，所有 `BLOCK_M` 个 j 复用同一个 `A_local[i]`。
- `B_local[j]` 只随 j 变化，与 i 无关 → 对固定的 j，所有 `BLOCK_N` 个 i 复用同一个 `B_local[j]`。

这恰好对应参考实现里的广播：`A` 沿 M 方向「广播」、`B` 沿 N 方向「广播」。题目文档里那句「we have two different iterators in buffers A and B」（[puzzles/03-outer-vec-add.py:20-22](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L20-L22)）说的就是这个：A 沿 i 迭代、B 沿 j 迭代，两个缓冲区走的是不同方向的下标。

#### 4.2.3 源码精读

答案里的二维 `T.Parallel` 循环体：

[ans/03-outer-vec-add.py:70-71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L70-L71) —— `for i, j in T.Parallel(BLOCK_N, BLOCK_M)` 遍历当前 block 的二维 tile，循环体里 `A_local[i] + B_local[j]` 完成外积式加法，结果写到二维寄存器片段 `C_local[i, j]`。

对照 Puzzle 02 的一维写法 [ans/02-vector-add.py:186-188](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L186-L188)（`for i in T.Parallel(BLOCK_N)`），二维版只是多了一个循环变量和 `T.Parallel` 里多了一个维度，循环体本身依然是普通的元素级算术。

#### 4.2.4 代码实践

1. **实践目标**：理解多维 `T.Parallel` 的迭代空间与下标独立性。
2. **操作步骤**：阅读 [ans/03-outer-vec-add.py:70-71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L70-L71)。假设当前 block 是 `pid_n=1, pid_m=2`，`BLOCK_N=BLOCK_M=1024`。
3. **需要观察的现象**：手算这个 block 处理的真实输出下标范围，以及当 `i=0, j=0` 时，`C_local[0,0]` 对应输出 `C[?, ?]`，用到的 `A_local[0]` 对应输入 `A[?]`。
4. **预期结果**：该 block 的输出区间是 `C[1024:2048, 2048:3072]`；`C_local[0,0]` 对应 `C[1024, 2048] = A[1024] + B[2048]`，即 `A_local[0]` 对应 `A[1024]`、`B_local[0]` 对应 `B[2048]`。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `for i, j in T.Parallel(BLOCK_N, BLOCK_M)` 改成 `for j, i in T.Parallel(BLOCK_M, BLOCK_N)`，同时保持循环体为 `C_local[i, j] = A_local[i] + B_local[j]`，计算结果会变吗？

**答案**：不会变。只要循环变量名与循环体里的下标、`T.Parallel` 的维度顺序三者对应一致，遍历的二维空间和每个元素的计算都相同，只是框架对迭代空间的并行划分方式可能不同。变量名 `i/j` 只是标签，关键是对应关系。

**练习 2**：能不能在 `T.Parallel` 循环体里同时写 `A_local` 和 `C_local`（既有读又有写同一个片段）？

**答案**：不建议。`T.Parallel` 的前提是「各迭代相互独立、无写冲突」。如果不同迭代对同一片段既有依赖的读又有写，会破坏并行正确性。本讲里 `A_local`/`B_local` 只读、`C_local` 只写，且每个 `(i,j)` 写不同位置，是安全的。

---

### 4.3 二维 fragment 中间结果与 T.copy 回写

#### 4.3.1 概念说明

这是本讲最关键的一块，把 Puzzle 02 的「fragment 缓存」思路搬到二维。回顾 u2-l2 的核心动机：**让数据尽量待在快的存储里，减少对 global memory 的反复访问**。

朴素做法（不缓存）是：在 `T.Parallel` 循环体里直接写 `C[n_idx+i, m_idx+j] = A[n_idx+i] + B[m_idx+j]`。这样每算一个 `(i,j)`，都要从显存读一次 `A[n_idx+i]` 和一次 `B[m_idx+j]`。问题在于外积式运算里 `A[i]` 要被同一行的所有 M 个 j 复用、`B[j]` 要被同一列的所有 N 个 i 复用——直接读显存会让同一个 `A[i]`、`B[j]` 被从显存读上成百上千次。

优化做法是：先用 `T.copy` 把本 block 要用的 A 的一段、B 的一段**一次性**搬进寄存器片段，之后循环体里只读寄存器；最后把算好的 `C_local` **一次性**搬回显存。这正是 u2-l2「批量搬入寄存器、块内计算、批量搬出」的三段式，只不过这里中间结果是二维的。

#### 4.3.2 核心流程

每个 block 的工作分三步：

```
① 搬入：T.copy(A[n_idx], A_local)   # A 的一段 BLOCK_N 个 → 1D fragment
        T.copy(B[m_idx], B_local)   # B 的一段 BLOCK_M 个 → 1D fragment
② 计算：C_local = T.alloc_fragment((BLOCK_N, BLOCK_M))  # 2D fragment
        for i, j in T.Parallel(BLOCK_N, BLOCK_M):
            C_local[i, j] = A_local[i] + B_local[j]      # 只读写寄存器
③ 搬出：T.copy(C_local, C[n_idx, m_idx])                # 2D fragment → 显存
```

**关于偏移下标 `A[n_idx]` / `C[n_idx, m_idx]` 的含义**：`T.copy` 要求两端形状一致。当你写 `A[n_idx]`（A 是一维 `(N,)`）时，TileLang 会从另一端 `A_local`（形状 `(BLOCK_N,)`）推断这段 tile 的长度，等价于切片 `A[n_idx : n_idx+BLOCK_N]`。这个写法在 Puzzle 02 内存版 [ans/02-vector-add.py:183-184](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/02-vector-add.py#L183-L184) 已出现过。本讲新增的是**二维偏移下标** `C[n_idx, m_idx]`：C 是 `(N, M)`，配合另一端 `C_local` 的 `(BLOCK_N, BLOCK_M)`，两个维度各自的起点是 `n_idx`、`m_idx`，各自长度由 `C_local` 推断，等价于 `C[n_idx:n_idx+BLOCK_N, m_idx:m_idx+BLOCK_M]`。

**数据复用的收益**（这是本模块要建立的核心直觉）。以一个 block 为单位，比较两种做法对 A、B 的显存读取次数：

| 做法 | 读 A 的次数 | 读 B 的次数 | 合计 |
| --- | --- | --- | --- |
| 不缓存（每个 (i,j) 直接读显存） | `BLOCK_N × BLOCK_M` | `BLOCK_N × BLOCK_M` | `2 · BLOCK_N · BLOCK_M` |
| 用 fragment 缓存 | `BLOCK_N`（搬入一次） | `BLOCK_M`（搬入一次） | `BLOCK_N + BLOCK_M` |

缓存后显存读取量从 \(2 \cdot \text{BLOCK\_N} \cdot \text{BLOCK\_M}\) 降到 \(\text{BLOCK\_N} + \text{BLOCK\_M}\)。代入 `BLOCK_N = BLOCK_M = 1024`：

\[
\frac{2 \times 1024 \times 1024}{1024 + 1024} = \frac{2\,097\,152}{2\,048} = 1024
\]

也就是同一个 block 内，缓存版对 A、B 的显存读取次数大约只有朴素版的 **1/1024**。这正是「让数据待在寄存器里反复复用」的威力，也是后续矩阵乘、卷积等所有「带数据复用」算子的性能基石。

#### 4.3.3 源码精读

三个 fragment 的分配——注意 `A_local`/`B_local` 是一维、`C_local` 是二维：

[ans/03-outer-vec-add.py:64-66](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L64-L66) —— `A_local` 形状 `(BLOCK_N,)`（只装 A 的一段）、`B_local` 形状 `(BLOCK_M,)`（只装 B 的一段）、`C_local` 形状 `(BLOCK_N, BLOCK_M)`（二维寄存器 tile，装本 block 的输出子矩阵）。

搬入 + 计算 + 搬出的完整三段：

[ans/03-outer-vec-add.py:68-72](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L68-L72) —— `T.copy(A[n_idx], A_local)` 和 `T.copy(B[m_idx], B_local)` 把 A、B 的一段搬进一维 fragment（偏移下标，长度由 fragment 推断）；随后在二维 `C_local` 上做并行计算；最后 `T.copy(C_local, C[n_idx, m_idx])` 用二维偏移下标把整块结果写回显存。

> 为什么 `A_local` 是 `(BLOCK_N,)` 而不是 `(BLOCK_N, BLOCK_M)`？因为 A 本身只有 N 一个维度，每个 block 只取它的一段 `BLOCK_N`；A 在 M 方向上是「被复用/广播」的，靠的是循环体里 `A_local[i]` 对所有 j 取同一个值，不需要真的复制成二维。`B_local` 同理。这种「一维片段 + 二维循环复用」正是外积式运算的典型写法。

#### 4.3.4 代码实践

1. **实践目标**：体会 fragment 缓存对显存访问量的影响。
2. **操作步骤**：阅读 [ans/03-outer-vec-add.py:64-72](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/03-outer-vec-add.py#L64-L72)。然后在脑子里把它改写成「朴素版」：删掉三个 `alloc_fragment` 和两个搬入 `T.copy`，把循环体直接改成 `C[n_idx+i, m_idx+j] = A[n_idx+i] + B[m_idx+j]`（注意 `C` 是显存里的输出，不能再用 `C_local`）。
3. **需要观察的现象**：对比两版每个 block 对显存的读写次数（搬入 A/B、搬出 C）。
4. **预期结果**：朴素版每算一个 `(i,j)` 要读 2 次显存（A、B 各一）、写 1 次（C）；缓存版搬入 `BLOCK_N + BLOCK_M` 次读、计算全在寄存器、最后 `BLOCK_N·BLOCK_M` 次写。读显存次数的差距就是上面算出的约 1024 倍。
5. 待本地验证：综合实践里可以用 `bench_puzzle` 实测两版耗时差距。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `C_local` 必须是二维 `(BLOCK_N, BLOCK_M)`，而 `A_local` 只是一维？

**答案**：`C_local` 要装本 block 的输出子矩阵，它确实有 N、M 两个维度各 `BLOCK_N`、`BLOCK_M` 大小；而 A、B 各自只有一个维度的数据，每个 block 只取其中一段，所以是一维。A/B 在另一维上是靠循环体下标复用，而非物理复制。

**练习 2**：把 `T.copy(C_local, C[n_idx, m_idx])` 里的 `C[n_idx, m_idx]` 换成 `C[n_idx]`（少写一个偏移），会发生什么？

**答案**：`C[n_idx]` 只给了一个维度的偏移，与 `C_local` 的二维形状 `(BLOCK_N, BLOCK_M)` 在维度数上不匹配，会报错或语义错误。二维输出必须给两个偏移下标，让 TileLang 在两个维度上都能推断出 tile 的位置与长度。

---

## 5. 综合实践

把三个模块串起来，补全 `puzzles/03-outer-vec-add.py` 里的 `tl_outer_add`。这是本讲的主任务。

**任务**：用 `(BLOCK_N, BLOCK_M)` 分块、缓存 `A_local`/`B_local` 到寄存器片段，并用多维 `T.Parallel(i, j)` 计算 `C[i, j] = A[i] + B[j]`。

**操作步骤**：

1. 打开 `puzzles/03-outer-vec-add.py`，定位到 [L52-62](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/03-outer-vec-add.py#L52-L62) 的 `tl_outer_add`，在 `# TODO` 处填入 kernel 体。
2. 写二维 grid 与起点：
   ```python
   with T.Kernel(N // BLOCK_N, M // BLOCK_M, threads=256) as (pid_n, pid_m):
       n_idx = pid_n * BLOCK_N
       m_idx = pid_m * BLOCK_M
   ```
3. 分配三个 fragment（两个一维输入 + 一个二维输出）：
   ```python
   A_local = T.alloc_fragment((BLOCK_N,), dtype)
   B_local = T.alloc_fragment((BLOCK_M,), dtype)
   C_local = T.alloc_fragment((BLOCK_N, BLOCK_M), dtype)
   ```
4. 搬入 A、B 的一段（偏移下标，长度由 fragment 推断）：
   ```python
   T.copy(A[n_idx], A_local)
   T.copy(B[m_idx], B_local)
   ```
5. 用多维 `T.Parallel` 做外积式加法，结果写进二维 `C_local`：
   ```python
   for i, j in T.Parallel(BLOCK_N, BLOCK_M):
       C_local[i, j] = A_local[i] + B_local[j]
   ```
6. 把 `C_local` 整块回写到显存输出：
   ```python
   T.copy(C_local, C[n_idx, m_idx])
   ```
7. 运行验证：
   ```bash
   python3 puzzles/03-outer-vec-add.py
   ```

**需要观察的现象**：终端打印的比对结果。

**预期结果**：`test_puzzle` 用 `torch.allclose(atol=rtol=1e-2)` 比对你的 kernel 与 `ref_outer_add` 的广播结果，应打印 `✅ Results match: True`。（参考答案见 `ans/03-outer-vec-add.py`，本实践即复刻它。）

**进阶（性能对比，可选）**：在 `run_outer_add` 里追加一次基准测试，把你的 kernel 与 PyTorch 广播版比一比：

```python
from common.utils import bench_puzzle
bench_puzzle(
    tl_outer_add,
    ref_outer_add,
    {"N": N, "M": M, "BLOCK_N": BLOCK_N, "BLOCK_M": BLOCK_M},
    bench_name="TL Outer Add",
    bench_torch=True,
)
```

`bench_puzzle` 会用 CUDA Event + warmup(10) + repeats(100) + synchronize 做公平计时（见 [common/utils.py:109-155](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L109-L155)）。尝试调整 `BLOCK_N`/`BLOCK_M`（例如 512 vs 1024），观察耗时变化——这会为你建立第一份「block size 调参」直觉，为后面 u5-l4 的性能工程做铺垫。待本地验证具体数值。

## 6. 本讲小结

- **二维 `T.Kernel`**：多一个维度就是多给 `T.Kernel` 一个位置参数、多解包一个块索引（`pid_n, pid_m`）、多算一个起点下标（`n_idx, m_idx`）；`threads` 仍只管 block 内部并行，与 grid 维数无关。
- **多维 `T.Parallel(i, j)`**：把二维迭代空间作为整体并行划分，循环体里写下标即可；外积式运算里 `A_local[i]` 只依赖 i、`B_local[j]` 只依赖 j，对应参考实现的广播方向。
- **二维 fragment + `T.copy` 回写**：用一维 `A_local`/`B_local` 装输入的一段，用二维 `C_local` 装输出子矩阵；偏移下标 `A[n_idx]`、`C[n_idx, m_idx]` 配合另一端形状自动推断 tile 长度。
- **数据复用是性能核心**：缓存后每个 block 对 A/B 的显存读取从 \(2 \cdot \text{BLOCK\_N} \cdot \text{BLOCK\_M}\) 降到 \(\text{BLOCK\_N} + \text{BLOCK\_M}\)，BLOCK 取 1024 时约快 1024 倍。
- **沿用既有约定**：验证仍走 `test_puzzle`（「输出放最后」、自动造随机输入、`torch.allclose` 比对），无需手写测试代码。

## 7. 下一步学习建议

本讲把「二维分块 + 多维并行 + fragment 缓存」三件套集齐，你已经具备写出像样二维 kernel 的全部基础。下一讲 **u3-l1（Puzzle 04 Backward Op）** 会把外积式运算升级为「带广播的前向 + 反向梯度」算子，重点学习如何手动处理广播（B 广播到 A 的二维形状）以及用链式法则写反向 kernel。

建议你继续阅读：
- `ans/03-outer-vec-add.py` 全文，对照本讲逐行确认理解；
- 回顾 `ans/02-vector-add.py` 的 `tl_mul_relu_1d_mem`，把一维 fragment 三段式与本讲的二维版本做对照，巩固「搬入—计算—搬出」范式；
- 想提前感受二维上的数据复用在更复杂算子里的形态，可以略读 `ans/08-matrix.py` 里矩阵乘的 `T.gemm`（后续 u4 单元会系统讲）。
