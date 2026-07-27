# Puzzle 05 Reduce Sum：归约 TileOp

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚什么是**归约（reduction）**，以及它为什么和前几讲的元素级操作（element-wise）本质不同。
- 掌握 TileLang 的归约 TileOp `T.reduce_sum`，理解它的接口（输入 fragment、输出 fragment、`dim`、`clear`）以及「必须在 fragment 上执行」的约束。
- 理解为什么大维度要做**分块串行累加**，掌握 `T.Serial` 的语义，并能解释为什么这里不能用 `T.Parallel`。
- 理解 `T.clear` 与 `T.reduce_sum(..., clear=False)` 共同构成的**累加语义**：先清零、再多次累加。
- 独立补全 `puzzles/05-reduce-sum.py` 中的 `tl_reduce_sum`。

## 2. 前置知识

本讲默认你已经掌握前三单元（u1、u2）和 u3-l1 的内容。回顾几个会反复用到的概念：

- **TileOp**：TileLang 提供的内建「块级」操作，如 `T.copy`。它一次性操作一整块数据，框架自动帮你做并行、向量化、线程映射。本讲将认识第二类 TileOp——归约算子。
- **fragment**：`T.alloc_fragment` 把「一个 block 内所有线程的寄存器」抽象成一块可按下标操作的 buffer。归约算子要求在 fragment 上执行，因为它依赖线程间的快速通信。
- **「搬入—计算—搬出」三段式**：从 global memory 用 `T.copy` 把一块数据搬进 fragment，在 fragment 上算，再用 `T.copy` 搬回 global memory。
- **块索引与分块**：`with T.Kernel(N // BLOCK_N, ...) as pid_n` 中，每个 block 用 `pid_n` 定位自己负责的行区间 `[pid_n*BLOCK_N : (pid_n+1)*BLOCK_N]`。
- **元素级 vs 归约**：u2–u3 讲的 `T.Parallel` 都是「输入输出形状不变」的一一映射；归约是**第一次出现形状变小、需要把多个元素汇总**的操作。

一个直观的例子：把一个 \(3\times4\) 的矩阵按行求和：

\[
A=\begin{bmatrix}1&2&3&4\\5&6&7&8\\9&10&11&12\end{bmatrix}
\quad\Longrightarrow\quad
B=\begin{bmatrix}10\\26\\42\end{bmatrix}
\]

每行 4 个数「合并」成 1 个数，形状从 \(3\times4\) 降维到 \(3\)。这就是归约。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [puzzles/05-reduce-sum.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/05-reduce-sum.py) | 题目：`tl_reduce_sum` 只留了声明骨架，`# TODO` 处需要你补全归约实现。 |
| [ans/05-reduce-sum.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py) | 参考答案：用 `T.Serial` 分块 + `T.reduce_sum(..., clear=False)` 累加。本讲精读它。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle` 验证正确性（`torch.allclose`，默认 `atol=rtol=1e-2`），`bench_puzzle` 用 CUDA Event 计时。 |

> 提示：本仓库的 `bx` 与答案里的 `pid_n` 是同一个块索引（CUDA 的 `blockIdx`），只是命名不同（见 u1-l3）。

## 4. 核心概念与源码讲解

本讲的三个最小模块紧密咬合：`T.reduce_sum` 负责「把一块 fragment 归约成一维」，`T.Serial` 负责「沿过大的 M 维分块、串行地反复调用归约」，`T.clear` 与 `clear` 参数则负责「保证累加从 0 开始、且不覆盖之前的累加值」。建议按 4.1 → 4.3 → 4.2 的因果顺序理解，再回头看整体。

### 4.1 T.reduce_sum 归约 TileOp

#### 4.1.1 概念说明

前四讲我们见过的操作（`T.copy`、加减乘除、`T.Parallel`）有一个共同点：**输入和输出形状一一对应**，每个输出元素只依赖一个或几个确定位置的输入元素，线程之间互不通信。

归约打破了这个假设。以本题「按行求和」为例：

\[
B[i] = \sum_{j=0}^{M-1} A[i, j]
\]

输出 \(B[i]\) 依赖整行共 \(M\) 个输入元素，需要把多个元素的值**汇总**到一个结果里。这就天然要求跨元素（进而是跨线程）的通信与合并。

手动实现 GPU 归约（树形归约、warp shuffle）很繁琐。TileLang 把它封装成一类 TileOp，和 `T.copy` 同属「框架自动帮你做线程映射」的高层算子：

- `T.reduce_sum`：求和归约
- `T.reduce_max` / `T.reduce_min`：最值归约（下一讲 softmax 会用到 `reduce_max`）

题目提示里有一句关键约束：

> for efficiency, we need to perform these TileOps in the fragment buffers instead of global memory.

也就是说，归约算子必须在 **fragment（寄存器）** 上执行，不能直接对 global memory 做。原因有二：

1. **性能**：归约需要线程间反复交换中间值，寄存器/shared memory 的带宽远高于显存。
2. **抽象**：fragment 是 TileLang 对「block 内线程寄存器集合」的统一抽象，框架知道每个元素落在哪个线程上，才能插入正确的 warp-level 归约指令。global memory 没有这层映射信息。

#### 4.1.2 核心流程

对一个 `A_local: fragment[BLOCK_N, BLOCK_M]` 沿 `dim=1` 做求和归约，输出 `B_local: fragment[BLOCK_N]`：

\[
\text{B\_local}[i] \;=\; \sum_{j=0}^{\text{BLOCK\_M}-1} \text{A\_local}[i, j],\qquad i\in[0,\text{BLOCK\_N})
\]

即每一行（固定 \(i\)）把该行的 `BLOCK_M` 个列元素压成一个标量。`dim=1` 表示「消去第 1 维（列）」，结果保留第 0 维（行）的 `BLOCK_N` 个元素。

一次 `T.reduce_sum` 只能归约**当前已加载到 fragment 里的一小块**（`BLOCK_M` 列）。但整行有 \(M=16384\) 列，远超 `BLOCK_M`。所以单次归约只是「局部和」，完整答案需要 4.2 的串行分块把所有局部和累加起来。

伪代码：

```
A_local = alloc_fragment(BLOCK_N, BLOCK_M)   # 输入 tile
B_local = alloc_fragment(BLOCK_N)            # 归约结果（每行一个标量）
# ...把一块数据搬进 A_local...
T.reduce_sum(A_local, B_local, dim=1, clear=...)   # B_local[i] = Σ_j A_local[i,j]
```

#### 4.1.3 源码精读

答案中 fragment 的分配与归约调用：

[ans/05-reduce-sum.py:68-69](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L68-L69) 分配两个 fragment：二维 `A_local[BLOCK_N, BLOCK_M]` 作为每次加载的输入 tile，一维 `B_local[BLOCK_N]` 作为每行累加和的「累加器」。

[ans/05-reduce-sum.py:74](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L74) `T.reduce_sum(A_local, B_local, dim=1, clear=False)`：把当前 `A_local` 沿列维归约，结果累加进 `B_local`。`dim=1` 消去列，留下 `BLOCK_N` 个行和；`clear=False` 表示不清零（4.3 详述）。

注意输入 `A_local` 和输出 `B_local` 都是 fragment，满足「归约必须在 fragment 上」的约束。

#### 4.1.4 代码实践

**目标**：确认答案可跑通，并验证对 `dim` 维度的直觉。

1. 直接运行参考答案，确认正确性：

   ```bash
   python3 ans/05-reduce-sum.py
   ```

2. 预期看到 `✅ Results match: True`，随后 `bench_puzzle` 输出 TileLang 与 Torch 的耗时。

3. **思考实验**：把 [ans/05-reduce-sum.py:74](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L74) 的 `dim=1` 改成 `dim=0`。先不运行，自己预测会发生什么：
   - `dim=0` 会消去第 0 维（行），把 `BLOCK_N` 行压成 `BLOCK_M` 个列和，输出形状应当是长度 `BLOCK_M`。
   - 但 `B_local` 的形状是 `(BLOCK_N,)`，对不上 → 预期编译期报形状不匹配的错误。
4. 实际改一下并运行，对照你的预测（确切的报错文案**待本地验证**）。

> 不要把这个改动留在文件里；验证完请改回 `dim=1`，因为后续 4.3、第 5 节都要用它。

#### 4.1.5 小练习与答案

**练习 1**：如果只想求「整行的最大值」而不是和，把哪一处改掉即可？
**答案**：把 `T.reduce_sum` 换成 `T.reduce_max`，其余（fragment、`dim=1`、`clear`、串行循环结构）保持不变。这正是下一讲 softmax 求 `reduce_max` 的写法。

**练习 2**：题目要求「在 fragment 上归约」。如果硬要把 `A_local` 换成 global memory 张量直接喂给 `T.reduce_sum`，预期会怎样？
**答案**：违反了归约算子的使用约束，预期会编译失败或得到错误/低效的结果。归约依赖 fragment 提供的线程映射信息与快速通信路径，不能跨过 `T.copy` 直接对显存操作。

### 4.3 T.clear / clear 累加语义

（先讲 4.3 再讲 4.2，是因为「累加语义」解释了为什么需要串行分块。）

#### 4.3.1 概念说明

归约答案要累加很多块，于是 `B_local` 扮演**累加器（accumulator）**的角色。这里有两个必须弄清的语义点：

1. **fragment 未初始化时值不确定**。新分配的 `B_local` 里可能是任意垃圾值，直接往里累加会得到错误结果。所以累加开始前必须显式清零。
2. **`T.reduce_sum` 的 `clear` 参数控制是否在归约前清零输出**：
   - `clear=True`：先把输出 fragment 清零，再写入归约结果（覆盖语义）。
   - `clear=False`：不清零，把归约结果**加到**输出已有值上（累加语义）。

这两点配合出一种经典模式：

- 进入 block 时，用 `T.clear(B_local)` 把累加器初始化为 0；
- 串行循环里，每次 `T.reduce_sum(..., clear=False)` 把这一块的局部和**累加**进去。

如果把循环里的 `clear` 设成 `True`，每次都先把 `B_local` 清零，前面累加的成果就全丢了——这正是初学者最容易踩的坑。

#### 4.3.2 核心流程

设整行被切成 \(K = M/\text{BLOCK\_M}\) 块，第 \(k\) 块的局部和记作 \(s_k[i]=\sum_{j} A[i, k\cdot\text{BLOCK\_M}+j]\)。正确的累加语义是：

\[
B[i] \;=\; \sum_{k=0}^{K-1} s_k[i]
\]

用「先清零、再多次 `clear=False`」实现：

\[
\text{B\_local} \xleftarrow{\;T.clear\;} 0 \;\xrightarrow{\;k=0,\;\text{clear=False}\;}\; s_0 \;\xrightarrow{\;k=1,\;\text{clear=False}\;}\; s_0+s_1 \;\to\;\cdots\;\to\; \sum_{k=0}^{K-1}s_k
\]

而错误的 `clear=True` 会变成：

\[
\text{B\_local} \;\xrightarrow{\;k=0,\;\text{clear=True}\;}\; s_0 \;\xrightarrow{\;k=1,\;\text{clear=True}\;}\; s_1 \;\to\;\cdots\;\xrightarrow{\;k=K-1\;}\; s_{K-1}
\]

最终只剩最后一块的局部和，前面的全部丢失。

#### 4.3.3 源码精读

[ans/05-reduce-sum.py:70](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L70) `T.clear(B_local)`：在循环之前把累加器清零。`T.clear` 等价于 `for i in T.Parallel(N): buf[i] = 0`，但写法更简洁、且框架会选高效的清零指令。

[ans/05-reduce-sum.py:74](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L74) `clear=False`：循环内每次都累加，不清零。与上一行的 `T.clear` 配合成「先初始化、后累加」的组合拳。

> 对照：在第四单元的 GEMM 里你会看到 `T.gemm(..., clear_accum=True/False)`，那是同一种累加器清零语义在矩阵乘里的体现。

#### 4.3.4 代码实践

**目标**：亲手制造并观察「clear 用错」的典型 bug。

1. 把 [ans/05-reduce-sum.py:74](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L74) 的 `clear=False` 改成 `clear=True`。
2. 运行 `python3 ans/05-reduce-sum.py`。
3. **观察**：`test_puzzle` 应输出 `❌ Results match: False`，并打印 `Yours`（你的结果）与 `Spec`（torch 参考值）。
4. **预期结果**：你的 `B[i]` 会明显偏小，约等于 `sum(A[i, M-BLOCK_M : M])`——即只有最后一块 `BLOCK_M` 列的局部和，其余 \(M-\text{BLOCK\_M}\) 列的贡献全被反复清零抹掉了。本例 \(M=16384,\text{BLOCK\_M}=128\)，正确值大约是错误值的 \(M/\text{BLOCK\_M}=128\) 倍。
5. 验证完务必改回 `clear=False`。

#### 4.3.5 小练习与答案

**练习 1**：如果把循环前的 `T.clear(B_local)` 删掉，但循环内保持 `clear=False`，结果会怎样？
**答案**：`B_local` 初始值不确定（垃圾值），最终结果 = 垃圾初值 + 正确累加和，`test_puzzle` 大概率失败。这就是「累加器必须先清零」的原因。

**练习 2**：是否可以把循环前的 `T.clear(B_local)` 删掉、改用循环内第一次 `clear=True`、之后 `clear=False` 来达到同样效果？
**答案**：理论上可以——第一次迭代清零并写入 \(s_0\)，之后累加。但写法更繁琐（要区分第一次和后续），不如「循环外统一 `T.clear` + 循环内统一 `clear=False`」清晰，所以答案采用了后者。

### 4.2 T.Serial 分块累加

#### 4.2.1 概念说明

现在回到那个绕不开的问题：\(M=16384\) 太大，一次塞不进 fragment（`BLOCK_M=128`）。所以必须把整行切成 \(M/\text{BLOCK\_M}=128\) 段，**逐段加载、逐段归约、累加**到一个累加器里。

这就需要一个**串行循环** `T.Serial`。先把它和已学的 `T.Parallel` 对照：

| | `T.Parallel(N)` | `T.Serial(N)` |
|---|---|---|
| 语义 | 各次迭代互相独立、可并行 | 各次迭代**按顺序**执行 |
| 何时用 | 元素级一一映射（无依赖） | 迭代间有数据依赖（累加、状态更新） |
| 本题角色 | （本题未直接用，块内归约由 TileOp 包办） | 沿 M 维分块，逐块累加 |

**为什么 M 维循环必须是串行？** 因为每一次迭代都对**同一个** `B_local` 做 read-modify-write 累加（`clear=False` 的语义就是「读旧值、加新值、写回」）。若并行执行，多个迭代会同时读写 `B_local` 的同一位置，产生竞态（race condition），结果不确定且通常偏小。

串行循环保证「第 \(k\) 次累加一定建立在第 \(k-1\) 次的结果之上」，顺序确定、结果正确。注意：**串行只发生在「块间 M 维」这一层**；块内的归约本身（`T.reduce_sum`）和不同行（不同 `pid_n`）之间仍然并行，所以整体依然高效。

#### 4.2.2 核心流程

每个 block（由 `pid_n` 标识）负责 `BLOCK_N` 行，把这 `BLOCK_N` 行的每一行都求和。完整流程：

```
with T.Kernel(N // BLOCK_N) as pid_n:        # 每个 block 处理 BLOCK_N 行
    A_local = alloc_fragment(BLOCK_N, BLOCK_M)
    B_local = alloc_fragment(BLOCK_N)
    T.clear(B_local)                          # 累加器清零

    for m_blk_id in T.Serial(M // BLOCK_M):   # 串行遍历 M 维的所有块
        T.copy(A[pid_n*BLOCK_N, m_blk_id*BLOCK_M], A_local)  # 搬入第 m_blk_id 块
        T.reduce_sum(A_local, B_local, dim=1, clear=False)   # 局部和累加进 B_local

    T.copy(B_local, B[pid_n*BLOCK_N])         # 搬回 global memory
```

数据流（与「搬入—计算—搬出」三段式一致，只是中间多了一层「串行重复」）：

```
global A[N, M]
   │  T.copy（每次搬 BLOCK_N × BLOCK_M）
   ▼
fragment A_local[BLOCK_N, BLOCK_M]  ──┐ 重复 M//BLOCK_M 次
   │  T.reduce_sum(dim=1, clear=False)│
   ▼                                  │
fragment B_local[BLOCK_N]  ◄──────────┘ 累加器
   │  T.copy（搬回）
   ▼
global B[N]
```

数学上，整行求和被分解为分块求和的累加：

\[
B[i] \;=\; \sum_{j=0}^{M-1} A[i,j]
\;=\; \sum_{k=0}^{M/\text{BLOCK\_M}-1}\;\sum_{j=0}^{\text{BLOCK\_M}-1} A\bigl[i,\;k\cdot\text{BLOCK\_M}+j\bigr]
\]

外层 \(\sum_k\) 由 `T.Serial` 循环 + `clear=False` 累加实现，内层 \(\sum_j\) 由单次 `T.reduce_sum` 实现。

#### 4.2.3 源码精读

[ans/05-reduce-sum.py:72-74](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L72-L74) 串行分块的主体：`T.Serial(M // BLOCK_M)` 枚举 M 维的每个块编号 `m_blk_id`；每次先用 `T.copy` 把对应 tile 搬进 `A_local`（起点偏移 `m_blk_id*BLOCK_M`），再用 `T.reduce_sum(..., clear=False)` 累加。

[ans/05-reduce-sum.py:67](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L67) `with T.Kernel(N // BLOCK_N, threads=256) as pid_n`：只在 N 维分块（沿 N 切成 `N//BLOCK_N` 个 block），M 维不分块到 grid、而是交给 block 内的 `T.Serial` 串行处理。`threads=256` 控制每个 block 的线程数，块内归约与搬运由这 256 个线程协作完成。

[ans/05-reduce-sum.py:76](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/05-reduce-sum.py#L76) 所有块累加完毕后，用一次 `T.copy` 把 `B_local` 搬回 global memory 的 `B[pid_n*BLOCK_N]` 区间。

#### 4.2.4 代码实践

**目标**：体会 `BLOCK_M`（分块大小）如何影响串行迭代次数与性能。

1. 在 `run_reduce_sum` 里，`N=4096, M=16384, BLOCK_N=16, BLOCK_M=128`（见 [puzzles/05-reduce-sum.py:73-76](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/05-reduce-sum.py#L73-L76)）。当前 `T.Serial` 迭代次数 \(= M/\text{BLOCK\_M}=16384/128=128\) 次。
2. 把 `BLOCK_M` 改成 `256`，预测迭代次数变为多少（应为 64），再运行 `python3 ans/05-reduce-sum.py`，对比 `bench_puzzle` 的 TileLang 耗时。
3. **预期结果**：正确性仍为 ✅（分块大小不改变最终和）；迭代次数减半，但单次搬运量翻倍，性能可能略升或略降——具体取决于硬件，**待本地验证**真实数值。
4. **思考实验（不必真改）**：如果把 `T.Serial` 换成 `T.Parallel`，多次迭代并行地累加进同一个 `B_local`，会发生什么？预期出现数据竞态，结果偏小且每次运行可能不同（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 M 维用 `T.Serial`，而 N 维却可以（通过多 block）并行？
**答案**：N 维的各行彼此独立，一个 block 算自己的 `BLOCK_N` 行、互不干扰，所以用多个 block 并行；M 维的各块都写到**同一组累加器** `B_local`，有写冲突依赖，必须串行才能正确累加。

**练习 2**：题目提示说「为了数值稳定，本题用 float32」。如果强行把 `dtype` 改成 float16 再做 16384 个数的累加，预期会有什么风险？
**答案**：float16 的有效位数和表示范围都小，累加大量数值容易精度损失甚至溢出，结果可能超出 `test_puzzle` 的 `atol=rtol=1e-2` 容差。这正是后续 GEMM 里「float16 输入 + float32 累加器」混合精度思路的动机（见 u4）。

## 5. 综合实践

把三个模块串起来，补全你自己的 kernel。

**任务**：编辑 [puzzles/05-reduce-sum.py:66](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/05-reduce-sum.py#L66) 处的 `# TODO`，实现 `tl_reduce_sum`，使其与 `torch.sum(A, dim=1)` 一致。

**操作步骤**：

1. 在 `with T.Kernel(N // BLOCK_N, threads=256) as pid_n:` 内：
   - 用 `T.alloc_fragment((BLOCK_N, BLOCK_M), dtype)` 分配输入 tile `A_local`；
   - 用 `T.alloc_fragment((BLOCK_N,), dtype)` 分配累加器 `B_local`；
   - `T.clear(B_local)` 初始化为 0；
   - `for m_blk_id in T.Serial(M // BLOCK_M):` 串行遍历 M 维分块，循环体里 `T.copy(A[pid_n * BLOCK_N, m_blk_id * BLOCK_M], A_local)` 搬入，再 `T.reduce_sum(A_local, B_local, dim=1, clear=False)` 累加；
   - 循环外 `T.copy(B_local, B[pid_n * BLOCK_N])` 搬回。
2. 运行验证：

   ```bash
   python3 puzzles/05-reduce-sum.py
   ```

3. **需要观察的现象**：`test_puzzle` 打印 `✅ Results match: True`；`bench_puzzle` 打印 TileLang 与 Torch 两次耗时。
4. **预期结果**：正确性通过（float32 下应远好于 `1e-2` 容差）；TileLang 单遍归约通常显著快于 `torch.sum`（待本地验证具体数值）。
5. **进阶对比**：仿照 `docs/zh/5.reduce-sum/2.implementation-guide.md` 里的「基础版本」（用 `T.alloc_var` + 嵌套循环手动累加），再写一个 `tl_reduce_sum_basic`，用 `bench_puzzle` 比较手写循环版与 `T.reduce_sum` 版的耗时，体会归约 TileOp 的性能价值。

## 6. 本讲小结

- **归约**是把多个元素合并成更小维度结果的操作，输入输出形状不再一一对应，需要跨元素/跨线程汇总。
- **`T.reduce_sum`** 是与 `T.copy` 同级的归约 TileOp，沿 `dim` 把一个 fragment 归约成一维，框架自动处理线程间归约通信；它**必须在 fragment 上执行**。
- **分块串行累加**：当待归约维度（M）远大于单块容量（BLOCK_M）时，用 `T.Serial(M // BLOCK_M)` 逐块加载、逐块归约；必须串行，因为各块累加进同一个累加器，有写依赖。
- **累加语义**：循环前 `T.clear(B_local)` 清零，循环内 `T.reduce_sum(..., clear=False)` 累加；`clear=True` 会反复清零、丢失累加结果，是常见坑。
- 本题只在 N 维分块到 grid，M 维交给 block 内的 `T.Serial`，是「grid 并行 + block 内串行」的典型结构。
- 数值上用 float32 累加以保证精度，为第四单元的混合精度累加器埋下伏笔。

## 7. 下一步学习建议

下一讲 **u3-l3 Puzzle 06 Softmax** 会把本讲的归约直接用上：softmax 需要 `T.reduce_max`（减最大值做数值稳定）、`T.reduce_sum`（求归一化分母）以及 `T.exp2`/`T.log2` 与 \(\log_2 e\) 恒等式，并升级到 Online Softmax / log-sum-exp 两遍算法。建议你：

1. 复习本讲的 `T.reduce_sum(dim=..., clear=...)` 与 `T.Serial` 分块结构，下一讲会原样复用。
2. 尝试把本讲的 `T.reduce_sum` 换成 `T.reduce_max`，提前感受「求每行最大值」的写法。
3. 思考：如果归约操作本身也要分两遍（先求 max 再求 sum），两遍之间该如何组织 `T.Serial`——这正是 softmax 要解决的问题。
