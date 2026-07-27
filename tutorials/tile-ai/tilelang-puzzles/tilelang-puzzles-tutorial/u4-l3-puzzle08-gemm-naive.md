# Puzzle 08 GEMM Naive：T.gemm 与 Tensor Core

## 1. 本讲目标

本讲承接上一讲的 GEMV（矩阵-向量乘），把输出从「向量」升级为「矩阵」，实现完整的矩阵-矩阵乘法（GEMM）。学完后你应该能够：

- 说清楚 **`T.gemm` 这个 TileOp 是什么**，以及它如何把矩阵乘加封装成一条 Tensor Core 的 MMA 指令。
- 用 **二维分块（BLOCK_M × BLOCK_N × BLOCK_K）** 描述一个 GEMM kernel 的数据流：哪些维度并行、哪个维度串行累加。
- 解释 **混合精度累加器**（float16 输入 + float32 累加）在 GEMM 里是如何体现的，以及它和上一讲 GEMV 手写 `.astype` 的区别。
- 独立补全 `puzzles/08-matrix.py` 中的 `tl_matmul_naive`，并用 `test_puzzle` / `bench_puzzle` 验证正确性与性能。

> 本讲只讲「朴素 GEMM」。共享内存与软件流水线这两项优化留给下一讲（`tl_matmul_opt`）。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（它们在前置讲义中已建立，这里只做一句话回顾）：

- **GPU 三级内存**：global（显存，最大最慢）→ shared（block 内共享）→ registers / fragment（线程私有，最快最小）。本讲朴素版只用 fragment，下一讲才引入 shared。
- **`T.Kernel` 与块索引**：`T.Kernel(各维 block 数, threads=N) as (块索引...)`，位置参数个数即 grid 维数。二维版本 `T.Kernel(N 块, M 块) as (pid_n, pid_m)` 已在 Puzzle 03 / 04 用过。
- **`T.Serial` 与累加语义**：当多块结果要累加进同一个缓冲区时，必须用 `T.Serial` 串行循环（存在写依赖），循环前用 `T.clear` 清零。这是 Puzzle 05 reduce-sum 与上一讲 GEMV 的核心套路。
- **累加器与 `accum_dtype`**：上一讲 GEMV 已经引入了 `dtype=float16`、`accum_dtype=float32` 的分离，并强调 `.astype(accum_dtype)` 必须放在「相乘之前」。本讲会看到 `T.gemm` 把这件事自动化了。
- **GEMV 的本质**：GEMV = 「逐元素乘（带广播）+ `T.reduce_sum`」，全程跑在 **CUDA Core** 上。

如果你对上面任何一条感到陌生，建议先回到对应讲义复习。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| `puzzles/08-matrix.py` | 题目本体。包含 GEMV、朴素 GEMM、优化 GEMM 三个带 TODO 的 kernel，以及 `ref_matmul` 参考实现和 `run_matmul_naive` 运行配置。本讲聚焦其中的 `tl_matmul_naive`。 |
| `ans/08-matrix.py` | 参考答案，与题目一一对应。本讲会逐行精读其中的 `tl_matmul_naive`，并对照 `tl_gemv` 看新旧写法的差别。 |
| `common/utils.py` | 提供 `test_puzzle`（正确性）与 `bench_puzzle`（CUDA Event 计时）框架，已在 u1-l2 讲过，本讲直接复用。 |

GEMM 的题目说明（含 Tensor Core 与 `T.gemm` 的引子）写在 `puzzles/08-matrix.py` 的 docstring 里，本讲会直接引用。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先认识 `T.gemm` 与 Tensor Core（它替代了 GEMV 里手写的「乘 + 归约」），再讲二维分块与 K 维串行累加的数据流，最后看混合精度累加器在 GEMM 中的体现。

### 4.1 T.gemm 与 Tensor Core

#### 4.1.1 概念说明

上一讲实现 GEMV 时，我们是这样算点积的：先在 CUDA Core 上做逐元素乘 `A[i,k]*B[k]`，再用 `T.reduce_sum` 沿 K 维归约。CUDA Core 擅长标量/向量运算，灵活但吞吐有限。

现代 GPU（NVIDIA Ampere/Hopper 等）还内置了一类**专为矩阵乘加设计的硬件单元——Tensor Core**。一条 MMA（Matrix Multiply Accumulate）指令可以一次性完成一个小矩阵块的乘加，例如 `16×16×16` 的 FP16 矩阵乘并累加，吞吐量是 CUDA Core 的数倍。

但直接写 MMA 指令非常痛苦：要处理线程到矩阵元素的映射、特定的内存布局（layout）、碎片化的寄存器分配等。TileLang 把这一切封装成一个和 `T.copy` 同级的 TileOp——**`T.gemm`**：

```python
T.gemm(A_buf, B_buf, C_buf)   # 语义：C_buf += A_buf @ B_buf
```

- 输入两个 buffer（A、B），输出一个 buffer（C），和其他 TileOp 风格一致。
- 它**默认是累加**（`C += A @ B`），而非覆盖——这正是 GEMM 在 K 维分段累加所需要的语义。
- 编译时，TileLang 会根据 buffer 的形状/dtype 自动 lower 成合适的 MMA 指令，你不用手写线程映射。

教学上，可以先把 `T.gemm` 理解成**「矩阵版的 FMA（乘加）」**：一条语句完成一个 tile 的乘法和累加，既封装了乘法、也封装了归约，还顺手把数据交给 Tensor Core。

#### 4.1.2 核心流程

`T.gemm` 在一个 kernel 里扮演的角色：

```
输入 tile A_frag[BLOCK_M, BLOCK_K]   (来自 A 的一个行块 × K 段)
输入 tile B_frag[BLOCK_K, BLOCK_N]   (来自 B 的一个 K 段 × 列块)
      │
      ▼  T.gemm(A_frag, B_frag, C_frag)   ← 一条语句 = 一组 MMA 指令
输出/累加 C_frag[BLOCK_M, BLOCK_N]   (C_frag += A_frag @ B_frag)
```

对比上一讲 GEMV 的等价手写流程：

```
GEMV（CUDA Core，手写两步）:
   AB_temp[i,j]  = A[i,j].astype(f32) * B[j].astype(f32)   # 逐元素乘
   reduce_sum(AB_temp, C, dim=1, clear=False)               # 归约累加

GEMM（Tensor Core，一步）:
   T.gemm(A_frag, B_frag, C_frag)                           # 乘 + 归约 + 累加，全打包
```

核心结论：**`T.gemm` 把 GEMV 里「手写乘法 + `T.reduce_sum`」两步合并成一步，并从 CUDA Core 切换到 Tensor Core。**

#### 4.1.3 源码精读

题目 docstring 里专门用一段话引出 `T.gemm` 与 Tensor Core：

> TileLang wraps these complex instructions and memory loading patterns into a simple `T.gemm` operator ... `T.gemm` takes two Buffers as input and one Buffer as output, just like other TileOp we have seen before.

[puzzles/08-matrix.py:99-102](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L99-L102) ——这段 docstring 说明了三件事：Tensor Core 适合矩阵运算、`T.gemm` 封装了复杂的 MMA 指令与加载模式、它的接口和其它 TileOp 一样是「两入一出」。

再看上一讲 GEMV 的「手写乘 + 归约」，用来感受 `T.gemm` 替代了什么：

[ans/08-matrix.py:78-81](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L78-L81) ——GEMV 里要先 `astype(accum_dtype)` 提升精度做逐元素乘（写在 `AB_temp`），再 `T.reduce_sum(..., dim=1, clear=False)` 归约累加到 `C_local`。这一整套在 GEMM 里被一行 `T.gemm` 取代。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲眼看到 `T.gemm` 确实 lower 成了 Tensor Core 的 MMA 指令，而不是退化成 CUDA Core 的标量循环。

**步骤**：

1. 先把 `tl_matmul_naive` 按 4.2 的方法补全（或直接用 `ans/08-matrix.py` 里的版本）。
2. 在仓库根目录运行一段最小脚本，编译并打印生成的 CUDA：

   ```python
   # 示例代码：仅供观察生成代码，不是项目原有文件
   from ans import puzzle08_matrix  # 若 ans 模块可导入；否则直接 from ans.08_matrix import ... 需用 importlib
   ```
   实操时更简单的做法是参考 `run_matmul_opt` 里已有的写法：

   ```python
   kern = tl_matmul_naive.compile(M=4096, N=4096, K=4096,
                                  BLOCK_M=128, BLOCK_N=128, BLOCK_K=64)
   kern.print_source_code()
   ```

3. 在打印出的 CUDA 源码里搜索 `mma.`（例如 `mma.m16n8k16`）。

**需要观察的现象**：生成代码中应出现 `mma.sync` 这类 Tensor Core 内联汇编；这正是 `T.gemm` 与 GEMV 手写循环的本质区别。

**预期结果**：能找到 `mma` 指令即说明 `T.gemm` 走了 Tensor Core。具体指令名称与寄存器分配随 TileLang 版本变化——**待本地验证**你机器上实际生成的指令。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「`T.gemm` 既是乘法也是归约」？

**参考答案**：因为 `C += A @ B` 这一步本身就包含了「对应位置相乘后在公共维度 K 上求和」。GEMV 里需要显式 `T.reduce_sum` 完成的归约，在 `T.gemm` 里被 MMA 指令在硬件级一次完成。

**练习 2**：`T.gemm(A, B, C)` 是覆盖 C 还是累加到 C？这一点对 K 维循环有什么影响？

**参考答案**：默认累加（`C += A @ B`）。因此 K 维循环里每次调用 `T.gemm` 都会把新的 K 段部分积**加**到 C 上，循环前只需 `T.clear(C)` 一次即可，循环内不需要反复清零。

---

### 4.2 二维分块 K 维串行累加

#### 4.2.1 概念说明

GEMM 的数学定义是把每个输出元素算成一个长度为 K 的点积：

\[
C[i, j] = \sum_{k=0}^{K-1} A[i, k] \cdot B[k, j], \quad i\in[0,M),\ j\in[0,N)
\]

GEMV（上一讲）的输出是长度为 M 的向量，只需在 M 维并行；GEMM 的输出是 M×N 的**矩阵**，因此要在 **M 和 N 两个维度**上并行——这就是「二维分块」。

我们把输出 C 切成若干 `BLOCK_M × BLOCK_N` 的子矩阵，每个线程块负责算其中一个子矩阵；K 维因为要把多段部分积累加进同一块 C，必须**串行**遍历。于是得到经典的三段分块：

- **M 维并行**：`M / BLOCK_M` 个行块，由 `pid_m` 索引。
- **N 维并行**：`N / BLOCK_N` 个列块，由 `pid_n` 索引。
- **K 维串行累加**：`K / BLOCK_K` 段，用 `T.Serial` 遍历，每段做一次 `T.gemm` 累加。

这和 Puzzle 05 reduce-sum、上一讲 GEMV 的结构同构：**「需要并行的维度交给 grid，需要累加的维度用 `T.Serial`」**。区别只是 GEMM 多了 N 维并行、且把「乘 + 归约」换成了 `T.gemm`。

#### 4.2.2 核心流程

对一个负责输出 tile `(pid_m, pid_n)` 的线程块：

```
1. 分配三个 fragment:
     A_local[BLOCK_M, BLOCK_K]   # A 的行块 × K 段
     B_local[BLOCK_K, BLOCK_N]   # B 的 K 段 × 列块
     C_local[BLOCK_M, BLOCK_N]   # 输出 tile，float32 累加器

2. T.clear(C_local)                       # 累加前必须清零

3. for k in T.Serial(K // BLOCK_K):       # K 维串行
       T.copy(A[pid_m*BLOCK_M, k*BLOCK_K],  A_local)   # 搬入 A 的一个 tile
       T.copy(B[k*BLOCK_K,  pid_n*BLOCK_N], B_local)   # 搬入 B 的一个 tile
       T.gemm(A_local, B_local, C_local)               # C_local += A_local @ B_local

4. T.copy(C_local, C[pid_m*BLOCK_M, pid_n*BLOCK_N])     # 搬回显存
```

数学上，每个输出 tile 是 K 维上若干小矩阵乘的和：

\[
C_{\text{tile}} = \sum_{k_b=0}^{K/\text{BLOCK\_K}-1} A_{\text{tile}}(pid_m, k_b)\cdot B_{\text{tile}}(k_b, pid_n)
\]

**索引的换算**（关键易错点）：块索引 `pid_m` 对应 M 维（A 的行），`pid_n` 对应 N 维（B 的列）。`T.Kernel` 的位置参数顺序决定 grid 维度顺序，第一个参数 → 第一个块索引（`pid_n`），第二个参数 → 第二个块索引（`pid_m`）。所以代码写成 `T.Kernel(ceildiv(N,BLOCK_N), ceildiv(M,BLOCK_M)) as (pid_n, pid_m)`，**N 在前、M 在后**。

#### 4.2.3 源码精读

参考答案 `tl_matmul_naive` 的核心 15 行：

[ans/08-matrix.py:155-181](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L155-L181) ——这是本讲要补全的 kernel 的完整答案。

逐段看：

[ans/08-matrix.py:165-168](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L165-L168) ——二维 `T.Kernel`：第一个参数 `T.ceildiv(N, BLOCK_N)` 给 `pid_n`，第二个 `T.ceildiv(M, BLOCK_M)` 给 `pid_m`，`threads=128` 是 block 内线程数。

[ans/08-matrix.py:169-173](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L169-L173) ——分配三个 fragment 并用 `T.clear(C_local)` 清零累加器。注意三个 buffer 的形状严格满足 `A_local @ B_local` 可乘、且与 `C_local` 同形：`(BLOCK_M,BLOCK_K) × (BLOCK_K,BLOCK_N) → (BLOCK_M,BLOCK_N)`。

[ans/08-matrix.py:174-177](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L174-L177) ——K 维串行循环：搬入 A、B 的一个 tile，`T.gemm` 累加到 `C_local`。注意 A 用 `pid_m`（行）定位、`k`（K 段）定位；B 用 `k`（K 段）定位、`pid_n`（列）定位——刚好对上「行 × K 段」与「K 段 × 列」。

[ans/08-matrix.py:179](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L179) ——把累加完的 `C_local` 搬回显存中对应的输出 tile。

对照题目里待补全的骨架（你即将动手的地方）：

[puzzles/08-matrix.py:137-148](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L137-L148) ——骨架已经把 host 声明部分（`T.const`、`T.Tensor`、`T.empty`、返回 C）写好，你只需在 `# TODO` 处补上 `with T.Kernel(...)` 整段 device 计算。

#### 4.2.4 代码实践（跟踪型）

**目标**：用一组具体数字验证索引换算，避免「`pid_m`/`pid_n` 谁是谁」搞混。

**步骤**：

1. 设 `M=N=K=4096`、`BLOCK_M=BLOCK_N=128`、`BLOCK_K=64`（即 `run_matmul_naive` 的配置，见 [puzzles/08-matrix.py:154-159](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L154-L159)）。
2. 在纸上回答：
   - 一共启动多少个线程块？(`ceildiv(4096,128) × ceildiv(4096,128) = 32 × 32 = 1024`)
   - 块 `(pid_m=1, pid_n=2)` 负责输出 C 的哪个子矩阵？(`C[128:256, 256:384]`)
   - 该块在 `k=0` 这一轮加载的 A、B 各是哪一段？(`A[128:256, 0:64]`、`B[0:64, 256:384]`)
3. 把答案与上面源码精读里的偏移公式 `pid_m*BLOCK_M`、`pid_n*BLOCK_N`、`k*BLOCK_K` 对齐核对。

**需要观察的现象**：手算的下标区间与公式完全一致。

**预期结果**：如上括号内的区间。无需运行 GPU 即可完成——这是一道纯索引推导题。

#### 4.2.5 小练习与答案

**练习 1**：为什么 K 维用 `T.Serial` 而不能用 `T.Parallel`？

**参考答案**：K 维的每一段都要把部分积**累加进同一个 `C_local`**，存在写依赖与竞态；`T.Serial` 保证各段顺序累加，`T.Parallel` 会让多个段并发写同一块累加器而出错。这和 Puzzle 05 reduce-sum、上一讲 GEMV 必须串行的理由完全相同。

**练习 2**：把 `T.Kernel` 的两个位置参数写反（先 M 后 N），会发生什么？

**参考答案**：kernel 仍能编译运行，但块索引与维度的对应关系会错位——`pid_n` 实际枚举的是 M 维、`pid_m` 枚举的是 N 维。由于后续 `A[pid_m*BLOCK_M, ...]`、`B[..., pid_n*BLOCK_N]` 仍按原语义写下标，结果会把错误的 tile 写进 C，`test_puzzle` 会判定不匹配（或在边界不整除时越界）。所以位置参数顺序与下标语义必须一致。

---

### 4.3 混合精度累加器

#### 4.3.1 概念说明

从 Puzzle 08 开始，项目固定采用**混合精度**：输入输出用 `float16`（`dtype`），累加用 `float32`（`accum_dtype`）。

为什么这样设计？

- **float16 输入/输出**：现代 AI 工作流的默认精度，Tensor Core 原生支持，且只占 float32 一半的带宽与显存。
- **float32 累加**：float16 的有效位只有约 3–4 位十进制、最大值约 65504；GEMM 在 K=4096 上要做 4096 次乘加，直接用 float16 累加会迅速丢精度甚至溢出。float32 累加器保证长求和的数值稳定性。

这一点上一讲 GEMV 已经讲过。**本讲的新意在「`T.gemm` 把精度提升自动化了」**。

回顾 GEMV：因为乘法和归约是手写的，我们必须**显式** `.astype(accum_dtype)` 把 float16 提升成 float32 再相乘，否则乘法会在 float16 下完成、精度已经丢了。

而在 GEMM 里，整条「乘 + 归约 + 累加」被 `T.gemm` 打包成 MMA 指令。MMA 硬件本身就按「fp16 输入 × fp16 输入 → **fp32 累加**」工作，累加精度由**输出 buffer `C_local` 的 dtype** 决定。所以只要把 `C_local` 声明成 `accum_dtype`（float32），`T.gemm` 就自动用 float32 累加，**不需要手写 `.astype`**。

#### 4.3.2 核心流程

混合精度在三个 buffer 上的分工：

```
A_local : float16   ┐
B_local : float16   ┘  ← 输入，省带宽、Tensor Core 原生
                │
                ▼  T.gemm(A_local, B_local, C_local)
C_local : float32     ← 累加器，由它的 dtype 决定累加精度（MMA 自动 fp16×fp16→fp32）
                │
                ▼  T.copy(C_local, C[...])
C       : float16     ← 输出，写回显存时才降回 float16（唯一一次主动降精度）
```

要点：

1. **输入/输出 dtype 与累加 dtype 解耦**：`dtype` 和 `accum_dtype` 是两个独立变量。
2. **累加 dtype = `C_local` 的 dtype**：`T.gemm` 不读 `accum_dtype` 这个 Python 变量，而是看 `C_local` 被声明成什么类型。
3. **降精度只发生在写回**：`T.copy(C_local, C[...])` 时才从 float32 舍入回 float16。

#### 4.3.3 源码精读

题目骨架里两个 dtype 的声明：

[puzzles/08-matrix.py:140-141](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L140-L141) ——`dtype = T.float16` 与 `accum_dtype = T.float32` 分离，本讲所有 puzzle 都沿用这对组合。

答案里三个 buffer 的 dtype 分配，正好对应上面的流程图：

[ans/08-matrix.py:169-171](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L169-L171) ——`A_local`、`B_local` 用 `dtype`（float16），`C_local` 用 `accum_dtype`（float32）。注意这里**没有**任何 `.astype`——对比 GEMV 的 [ans/08-matrix.py:79](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/08-matrix.py#L79) 那行显式 `.astype(accum_dtype)`，差别一目了然：`T.gemm` 把精度提升内化了。

#### 4.3.4 代码实践（调参观察型）

**目标**：直观感受「float32 累加器」对长求和精度的意义。

**步骤**：

1. 先保证 `tl_matmul_naive`（`C_local` 为 float32）能通过 `test_puzzle`，记录控制台打印的 `Max diff`。
2. **复制一份** kernel（不要改原题），把 `C_local` 的 dtype 从 `accum_dtype` 改成 `dtype`（即 float16），其余不变，重新编译运行 `test_puzzle`。
3. 对比两次的 `Max diff` 与 `Results match`。

**需要观察的现象**：

- float32 累加器版：`Results match: True`，`Max diff` 较小（`test_puzzle` 默认 `atol=rtol=1e-2`，见 [common/utils.py:71](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py#L71)）。
- float16 累加器版：`Max diff` 显著变大，可能 `Results match: False`。

**预期结果**：累加精度下降导致误差放大。注意：把 `T.gemm` 的输出 buffer 设成 float16 在某些 TileLang 版本下可能**无法编译**（MMA 物理上以 fp32 累加），若遇到编译报错，本身就是「累加必须高精度」的佐证——**待本地验证**你机器上的具体行为。

#### 4.3.5 小练习与答案

**练习 1**：GEMM 里为什么看不到 GEMV 那样的 `.astype(accum_dtype)`？

**参考答案**：因为 `T.gemm` 把乘法和累加打包成一条 MMA 指令，而 MMA 硬件按 `fp16 × fp16 → fp32 累加` 工作，累加精度由输出 buffer `C_local` 的 dtype（float32）决定。只要 `C_local` 声明成 float32，精度提升就自动完成，无需手写 `.astype`。

**练习 2**：整条 GEMM 数据流中，唯一一次「主动降精度」发生在哪里？为什么是那里？

**参考答案**：发生在最后 `T.copy(C_local, C[...])` 写回显存时——把 float32 累加结果舍入回 float16 输出。因为输出张量 C 的 dtype 是 float16，且此时所有 K 维累加已完成，精度损失只发生一次、不再累积，是性价比最高的降精度时机。

---

## 5. 综合实践

把三个模块串起来，完成本讲的主任务：**补全 `puzzles/08-matrix.py` 中的 `tl_matmul_naive`**。

**任务目标**：实现一个用二维分块 + K 维串行 `T.gemm` 累加的朴素 GEMM kernel，并通过正确性与性能验证。

**操作步骤**：

1. 打开 `puzzles/08-matrix.py`，定位 [puzzles/08-matrix.py:137-148](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L137-L148) 的 `tl_matmul_naive`。
2. 在 `# TODO` 处补上 `with T.Kernel(...)` 整段，要点回顾：
   - 二维 grid：`T.Kernel(T.ceildiv(N, BLOCK_N), T.ceildiv(M, BLOCK_M), threads=128) as (pid_n, pid_m)`；
   - 三个 fragment：`A_local(BLOCK_M,BLOCK_K)`、`B_local(BLOCK_K,BLOCK_N)` 用 `dtype`，`C_local(BLOCK_M,BLOCK_N)` 用 `accum_dtype`；
   - `T.clear(C_local)`；
   - `for k in T.Serial(K // BLOCK_K)`：搬入 A/B tile，`T.gemm(A_local, B_local, C_local)`；
   - `T.copy(C_local, C[pid_m*BLOCK_M, pid_n*BLOCK_N])`。
3. 运行验证：

   ```bash
   python3 puzzles/08-matrix.py
   ```

   或单独调用 `run_matmul_naive()`（[puzzles/08-matrix.py:151-185](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L151-L185)），它会依次跑 `test_puzzle` 和 `bench_puzzle`。

**需要观察的现象**：

- `test_puzzle` 打印 `✅ Results match: True`（K=4096 下 float16 容差 `1e-2`，可能有极小的 `Max diff`，属正常）。
- `bench_puzzle` 打印 `Tilelang time` 与 `Torch time`。

**预期结果**：朴素版正确性与 torch 对齐；性能通常**慢于** torch 的 cuBLAS（朴素版既没上共享内存也没流水线，这正是下一讲的改进空间）。

**进阶（衔接下一讲）**：参考 `run_matmul_opt`（[puzzles/08-matrix.py:225-252](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/08-matrix.py#L225-L252)）里已有的写法，用 `.compile(...).print_source_code()` 同时打印 naive 与 opt 两版的生成 CUDA，直观感受「朴素版用 fragment + `T.Serial`」与「优化版用 shared + `T.Pipelined`」在生成代码上的差别——这就把本讲和下一讲（u4-l4）连起来了。

> 说明：本实践未替你执行任何命令；上述耗时与 diff 的具体数值需在你本机 GPU 上验证。

## 6. 本讲小结

- **`T.gemm` 是封装 Tensor Core MMA 的 TileOp**：一条 `T.gemm(A, B, C)` 同时完成「乘 + 归约 + 累加」（`C += A @ B`），替代了 GEMV 里手写的逐元素乘 + `T.reduce_sum`，并把计算从 CUDA Core 切到 Tensor Core。
- **二维分块**：输出 C 在 M、N 两维并行（`pid_m`、`pid_n`），K 维串行累加（`T.Serial` + `T.clear` 一次 + 每段 `T.gemm`），遵循「并行维度交 grid、累加维度用 Serial」的统一范式。
- **`T.Kernel` 位置参数顺序即块索引语义**：写成 `(N 块, M 块) as (pid_n, pid_m)`，下标里 `pid_m*BLOCK_M` 对 A 的行、`pid_n*BLOCK_N` 对 B 的列，二者必须一致。
- **混合精度由 `C_local` 的 dtype 决定**：输入输出 float16、累加 float32；`T.gemm` 自动做 `fp16×fp16→fp32` 累加，所以 GEMM 里**不需要** GEMV 那样的显式 `.astype`，降精度只发生在写回显存那一步。
- **朴素版正确但非最优**：A/B/C 全放 fragment 会增大寄存器压力，且 `T.Serial` 让访存与计算串行——这正是下一讲「共享内存 + `T.Pipelined`」要解决的问题。

## 7. 下一步学习建议

本讲完成了「能跑通、能对齐 torch」的朴素 GEMM。建议下一步：

1. **进入 u4-l4（Puzzle 08 GEMM 优化）**：学习 `T.alloc_shared` 如何把 A/B tile 从寄存器搬到共享内存以缓解寄存器压力，以及 `T.Pipelined(num_stages=3)` 如何用软件流水线重叠 K 维的访存与计算，把朴素版性能拉到接近 cuBLAS。
2. **对照阅读** `docs/zh/8.matrix/2.implementation-guide.md` 中「GEMM 朴素实现详解」与「GEMM 优化实现详解」两节，里面有完整的内存层级图与流水线时间线。
3. **代码实验**：在进入下一讲前，先用 `print_source_code()` 把本讲朴素版的生成 CUDA 看一遍（找 `mma` 指令、找 `T.Serial` 展开后的循环），下一讲再对比优化版的生成代码，差异会非常直观。
4. **向后展望**：GEMM 是 FlashAttention、卷积（im2col）、量化矩阵乘的共同基石——本册 u5 系列的三个 hard/medium puzzle 都会复用本讲建立的「二维分块 + `T.gemm` + K 维累加」骨架。
