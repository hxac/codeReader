# Puzzle 09 Conv im2col：卷积转 GEMM

## 1. 本讲目标

上一讲（u5-l1）我们用朴素滑动窗口实现了单通道 Conv1D：手写乘加、用 `T.Serial(KL)` 串行累加、用 shared memory 装 halo 区域。它是**正确但非最优**的——计算发生在 CUDA Core 上，无法借用 Tensor Core。

本讲把卷积**升级成矩阵乘法（GEMM）**，从而打开 Tensor Core 这扇性能大门。具体学会三件事：

1. 把单通道卷积**扩展到多输出通道**（输出从 `[N, L]` 变 `[N, L, F]`），作为朴素基线（`tl_conv1d_multi_outchannel`）。
2. 理解 **im2col 变换**：把滑动窗口卷积重写成一次矩阵乘，并用 `T.if_then_else` 做零填充。
3. 用 `T.reshape` 把 3D tile 改造成 GEMM 的 2D 操作数，再用 `T.gemm(..., clear_accum=True)` 一步完成乘加（`tl_conv1d_im2col`）。

学完后，你应该能解释「为什么卷积可以被当成矩阵乘」「im2col 矩阵的每一行代表什么」「`clear_accum=True` 在这里为什么必要」，并能动手对比朴素版与 im2col 版的正确性及耗时。

## 2. 前置知识

本讲是 advanced 阶段，默认你已经掌握以下前置（对应前置讲义摘要）：

- **二维分块与块索引**（u2-l3 / u5-l1）：`T.Kernel(...) as (pid_n, pid_l)`、偏移下标 `X[pid_n*BLOCK_N, pid_l*BLOCK_L]` 作为 tile 锚点。
- **`T.alloc_shared` 与 `T.alloc_fragment`**（u2-l2 / u4-l4）：shared memory 是 block 内共享的片上高速存储；fragment 是寄存器抽象，常用于累加器。
- **`T.Parallel` 与 `T.Serial` 的取舍**（u5-l1）：输出位置互相独立用 `T.Parallel`，存在写依赖的累加用 `T.Serial`。
- **`T.gemm` 与 Tensor Core**（u4-l3 / u4-l4）：`T.gemm(A, B, C)` 语义为 `C += A @ B`，内部映射到一条 Tensor Core MMA 指令；输入常用 shared memory，累加器用 fragment 的 float32。
- **`T.if_then_else` 作为运行时条件表达式**（u3-l1）：用于逐元素运行时分支，是做边界零填充的标准工具。

两个补充概念：

- **im2col（image to column）**：一种经典变换，把卷积的滑动窗口「展开」成矩阵的一行行，使卷积等价于一次矩阵乘。名字来自图像处理，1D 时也叫 im2row，本质相同。
- **零填充（zero padding）**：当滑动窗口超出输入边界时，把「虚」位置当成 0 参与计算。本 puzzle 的 torch 参考实现就是先给 `X` 末尾补 `KL-1` 个 0 再做 `torch.conv1d`，所以我们也要在边界补 0 才能对齐。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py) | 题目文件。含 `tl_conv1d_multi_outchannel` 与 `tl_conv1d_im2col` 两个待补全 TODO，以及多通道卷积的数学定义和 `run_conv1d_im2col` 测试/基准入口。 |
| [ans/09-conv.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py) | 参考答案。两个函数的完整实现，是本讲源码精读的主要对象。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle`（正确性，`torch.allclose` 容差 1e-2）与 `bench_puzzle`（CUDA Event 计时，warmup=10、repeats=100）。 |

> 题目里多通道卷积的数学定义见 [puzzles/09-conv.py#L127-L152](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L127-L152)（注：文件内编号写作 `08-2`，与 Puzzle 09 同文件，是历史编号）。

---

## 4. 核心概念与源码讲解

### 4.1 多输出通道卷积（朴素基线）

#### 4.1.1 概念说明

上一讲的单通道卷积核 `K` 形状是 `[KL]`，输出 `O` 是 `[N, L]`：每个输出位置由一个标量卷积核扫过窗口得到。

真实的神经网络卷积通常有**多个输出通道（filter）**：卷积核 `K` 变成 `[KL, F]`，每个 `f ∈ [0, F)` 是一个独立的 filter；输出 `O` 变成 `[N, L, F]`，即每个空间位置产出 `F` 个通道的响应。数学定义（互相关，不翻转核）：

\[
O[i, j, f] = \sum_{k=0}^{KL-1} \mathbb{1}[\,j+k<L\,]\, X[i, j+k]\, K[k, f]
\]

与单通道相比，唯一的结构变化是**多了一个 `f` 维度**：`K[k]` 变 `K[k, f]`，`O[i,j]` 变 `O[i,j,f]`。这是把 im2col「撑成真正的矩阵乘」而不是退化成 GEMV 的关键（题目注释里明确说：*To present GEMM from degenerating to GEMV, we need to introduce an output channel dimension F*）。当 `F=1` 时它就退回单通道；当 `F` 较大（32~128）时，矩阵乘的「瘦长」形状变「方正」，Tensor Core 才有施展空间。

#### 4.1.2 核心流程

朴素多通道实现就是把 u5-l1 的朴素 Conv1D 多套一层 `f`：

```
grid = (N // BLOCK_N, L // BLOCK_L)          # 每个 block 处理一个 (N, L) 输出 tile
每个 block (pid_n, pid_l):
  1. X_local  = shared[BLOCK_N, BLOCK_L+KL]   # 带 halo 的输入 tile（含窗口溢出列）
  2. K_local  = shared[KL, F]                 # 整个卷积核
  3. O_local  = fragment[BLOCK_N, BLOCK_L, F] # float32 累加器
  4. T.copy 输入 tile / 卷积核 进片上存储
  5. T.clear(O_local)
  6. for (i, j, f) in Parallel(BLOCK_N, BLOCK_L, F):   # 输出位置 + 通道，互相独立
       for k in Serial(KL):                            # 窗口累加，写依赖必须串行
         if j + k < L:                                 # 边界零填充
           O_local[i,j,f] += X_local[i,j+k] * K_local[k,f]   # 每项各自 .astype(float32)
  7. T.copy(O_local -> O 的对应 tile)
```

要点对照单通道版本：`K_local` 形状 `[KL] → [KL, F]`、`O_local` 形状 `[BLOCK_N, BLOCK_L] → [BLOCK_N, BLOCK_L, F]`、外层并行循环 `(i,j) → (i,j,f)`、乘法 `K_local[k] → K_local[k,f]`。计算仍在 CUDA Core 上做标量 FMA，所以它只是「正确的多通道基线」，不是高性能实现。

#### 4.1.3 源码精读

完整答案见 [ans/09-conv.py#L212-L236](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L212-L236)。关键片段：

声明部分——`K` 与 `O` 都多出 `F` 维（[ans/09-conv.py#L216-L218](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L216-L218)）：

```python
X: T.Tensor((N, L), dtype)
K: T.Tensor((KL, F), dtype)          # 多了 F
O = T.empty((N, L, F), dtype)        # 多了 F
```

片上存储分配——`K_local` 用 shared（全 `[KL, F]` 被 block 内所有线程反复读），`O_local` 用 fragment 做高精度累加（[ans/09-conv.py#L222-L224](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L222-L224)）：

```python
X_local = T.alloc_shared((BLOCK_N, BLOCK_L + KL), dtype)   # 带 KL 列 halo
K_local = T.alloc_shared((KL, F), dtype)
O_local = T.alloc_fragment((BLOCK_N, BLOCK_L, F), accum_dtype)
```

> 注意 `X_local` 的列数是 `BLOCK_L + KL`（朴素版用了 `+ KL` 而非更省的 `+ KL - 1`，留一列冗余但不影响正确性，因为多余列在 `if j+k<L` 守卫下不参与累加）。这是上一讲 halo 思想的直接沿用。

数据搬运与累加——`T.copy` 用偏移下标定位 tile 锚点，三维 `T.Parallel(i, j, f)` 外层并行、`T.Serial(KL)` 内层串行累加（[ans/09-conv.py#L226-L234](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L226-L234)）：

```python
T.copy(X[pid_n*BLOCK_N, pid_l*BLOCK_L], X_local)
T.copy(K, K_local)
T.clear(O_local)
for i, j, f in T.Parallel(BLOCK_N, BLOCK_L, F):
    for k in T.Serial(KL):
        if j + k < L:
            O_local[i, j, f] += X_local[i, j + k].astype(accum_dtype) * \
                K_local[k, f].astype(accum_dtype)
T.copy(O_local, O[pid_n*BLOCK_N, pid_l*BLOCK_L, 0])
```

回写时 `O[pid_n*BLOCK_N, pid_l*BLOCK_L, 0]` 是 `(N, L, F)` 张量里的一个三维 tile 锚点，区域大小 `(BLOCK_N, BLOCK_L, F)` 由源 `O_local` 推断。`.astype(accum_dtype)` 放在乘法**之前**，让 float16 相乘升到 float32（与 u4-l2 GEMV 的精度纪律一致）。

#### 4.1.4 代码实践

**目标**：把单通道朴素卷积「机械扩展」到多通道，跑通正确性。

**步骤**：

1. 打开 `puzzles/09-conv.py`，定位 [tl_conv1d_multi_outchannel 的 TODO](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L196-L206)（puzzle 文件第 196–206 行）。
2. 参照 4.1.3 的结构补全：声明 `X_local / K_local / O_local`，搬运、`T.clear`、三维 `T.Parallel` + `T.Serial(KL)` 累加、回写。
3. 运行 `python3 puzzles/09-conv.py`（会先后执行 `run_conv1d_naive` 与 `run_conv1d_im2col`，后者会顺带测你的 `tl_conv1d_multi_outchannel`）。

**需要观察的现象**：`run_conv1d_im2col` 里对 `tl_conv1d_multi_outchannel` 的 `test_puzzle` 打印 `✅ Results match: True`。

**预期结果**：与 `ref_conv1d_multi_outchannel`（torch 版）在 `atol=rtol=1e-2` 下一致。若出现 `❌`，最常见原因是漏了 `.astype(accum_dtype)` 或 `T.clear(O_local)`。

#### 4.1.5 小练习与答案

**练习 1**：把 `tl_conv1d_multi_outchannel` 里 `O_local` 的 dtype 从 `accum_dtype`（float32）改成 `dtype`（float16），运行 `test_puzzle` 会怎样？为什么？

> **答案**：大概率 `❌`。累加在 float16 上做 `KL=32` 次相加，有效位不足以支撑长求和，误差超出 1e-2 容差。这正是累加器必须用 float32 的原因（参见 u4-l2）。

**练习 2**：为什么 `K_local` 选 `T.alloc_shared` 而不是像朴素单通道那样用 `T.alloc_fragment`？

> **答案**：`K_local` 形状是 `[KL, F]`，被 block 内所有 `(i, j, f)` 工作项反复读取，是典型的「广播只读」数据；放进 shared memory 让全 block 共享一份拷贝，既省存储又利于 `T.copy(K, K_local)` 一次性批量载入。fragment 适合「每个线程私有」或作为高频更新的累加器（如 `O_local`）。shared vs fragment 的取舍详见 u2-l2 / u4-l4。

---

### 4.2 im2col 变换与 T.if_then_else 填充

#### 4.2.1 概念说明

朴素卷积的性能瓶颈在于：计算是标量 FMA，吃不到 Tensor Core；访存模式是滑窗重叠读取，不如矩阵乘规则。**im2col** 的核心洞察是——卷积的定义本身就是一个矩阵乘，只要把数据换个摆法：

定义一个新张量 \(\tilde{X}\)（读作「im2col 后的 X」），它把「第 `(i,j)` 个输出位置、第 `k` 个窗口抽头」对应的输入值摆在一起：

\[
\tilde{X}[i, j, k] \;=\; \mathbb{1}[\,j+k<L\,]\,\cdot\, X[i,\, j+k]
\]

即第 `(i,j)` 行、第 `k` 列放的就是「窗口里第 `k` 个输入值」（越界补 0）。于是卷积定义式立刻变成：

\[
O[i, j, f] \;=\; \sum_{k=0}^{KL-1} \tilde{X}[i, j, k]\, K[k, f]
\]

这正是矩阵乘 \(O_{mat} = \tilde{X}_{mat}\, K\)，其中把 \(\tilde{X}\) 看作形状 \((N\!\cdot\!L,\; KL)\) 的矩阵（行由 \((i,j)\) 编号、列由 \(k\) 编号），\(K\) 是 \((KL, F)\)：

\[
\underbrace{O_{mat}}_{(NL)\times F} \;=\; \underbrace{\tilde{X}_{mat}}_{(NL)\times KL}\;\cdot\;\underbrace{K}_{KL\times F}
\]

直观记忆：**im2col 矩阵的每一行 = 一个输出位置要「凑齐」的一组窗口抽头；每一列 = 卷积核的一个 tap**。这样卷积的 `k` 求和就成了矩阵乘的规约维（K 维），可以被 `T.gemm` 一步算完，并落到 Tensor Core 上。

> 为什么必须引入 `F`？若 `F=1`，`K` 退化为向量 `[KL]`，`O` 退化为 `[N,L]`，矩阵乘退化成 GEMV；GEMV 的 GEMM 形状「瘦长」，Tensor Core 利用率极低。引入 `F`（题目给 32~128）后形状变方正，才值得走 im2col+GEMM。

#### 4.2.2 核心流程

在 kernel 里，我们**每个 block 现场构建**一个 im2col tile（而不是事先在显存里展开整个矩阵，那样太费显存）：

```
每个 block (pid_n, pid_l):  负责 X 的一个 (BLOCK_N, BLOCK_L) 空间 tile
  1. X_shared = shared[BLOCK_N, BLOCK_L, KL]   # im2col 张量（仍是 3D 视图）
  2. 用 T.Parallel(i, j, k) 填充 X_shared:
       global 列 = pid_l*BLOCK_L + j + k
       X_shared[i, j, k] = if (global 列 < L) then X[行, global 列] else 0   # T.if_then_else 零填充
  —— 此时 X_shared[i,j,k] == \tilde{X}[对应全局 (i,j), k]
```

`T.if_then_else(cond, a, b)` 在这里扮演两个角色：

1. **运行时分支**：`cond = pid_l*BLOCK_L + j + k < L` 逐元素判断（编译期 `if` 做不到，因为它依赖运行时的 `pid_l, j, k`）。
2. **带守卫的零填充读取**：当抽头落在补零区，返回 `0`，与 torch 参考的 `pad(..., KL-1)` 行为对齐。TileLang 会把这种「条件 + 全局读取」编译成带谓词守卫的 load，避免越界访存。

> 与 4.1 朴素版的边界处理对比：朴素版在**累加内层**用 `if j+k<L` 跳过越界抽头；im2col 版则在**构建矩阵时**就把越界位置填成 0。后者让后续 `T.gemm` 拿到的是「干净的稠密矩阵」，无需在乘加里再判边界——这是把卷积完全交给 GEMM 的前提。

#### 4.2.3 源码精读

im2col 矩阵的分配与填充见 [ans/09-conv.py#L260-L267](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L260-L267)：

```python
X_shared = T.alloc_shared((BLOCK_N, BLOCK_L, KL), dtype)     # im2col 张量，第 3 维 = 抽头 k
K_shared = T.alloc_shared((KL, F), dtype)
O_local  = T.alloc_fragment((BLOCK_N * BLOCK_L, F), accum_dtype)   # 注意：已是 2D！

for i, j, k in T.Parallel(BLOCK_N, BLOCK_L, KL):
    X_shared[i, j, k] = T.if_then_else(
        pid_l * BLOCK_L + j + k < L, X[pid_n * BLOCK_N + i, pid_l * BLOCK_L + j + k], 0
    )
```

逐行解读：

- `X_shared` 第三维大小是 `KL`：每个空间位置 `(i, j)` 携带完整的 `KL` 个抽头，正是 im2col 矩阵的一行。
- `O_local` 直接声明成 **2D** `(BLOCK_N*BLOCK_L, F)`——因为它要作为 `T.gemm` 的输出（矩阵乘结果是 2D），行号 = 空间位置展平。
- 填充循环用**全局列坐标** `pid_l * BLOCK_L + j + k` 判边界（不像朴素版只判局部 `j+k`），这对最后一个 block 也正确。
- `T.if_then_else(cond, X[...], 0)`：真分支取真实输入，假分支补 0，实现零填充。

注意此处**只判了 `L` 方向边界**（`pid_l*BLOCK_L + j + k < L`），没判 `N` 方向。因为测试规模 `N` 是 `BLOCK_N` 的整数倍、block 数正好 `N//BLOCK_N`，行方向不会越界；若要支持任意 `N`（如 docs 实现指南里的写法），还需加上 `pid_n*BLOCK_N + i < N` 守卫并改用 `T.ceildiv`（见本讲 4.3.5 的扩展讨论）。

#### 4.2.4 代码实践

**目标**：亲手把输入 tile 变成 im2col 矩阵，并验证「边界处确实是 0」。

**步骤**：

1. 在 [tl_conv1d_im2col 的 TODO](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L220-L230)（puzzle 文件第 220–230 行）里，先只写分配 `X_shared / K_shared / O_local` 与上面的填充循环（暂时不写 `T.gemm`）。
2. 为观察填充结果，可临时加一句 `print` 钩子：在 `compile()` 后调用 `kernel.get_kernel_source()` 或对极小规模（`N=1, L=4, KL=2`）单步推算 `X_shared` 的每个元素。
3. 手算验证：对 `L=4, KL=2`，最后一个输出位置 `j=3` 的窗口抽头应取 `X[i,3]`（k=0）和 `0`（k=1，因 `j+k=4 ≥ L=4`）。

**需要观察的现象**：im2col 矩阵右下角（最后一个 block 的末尾行、`k` 较大列）出现 0；非越界位置等于对应 `X[i, j+k]`。

**预期结果**：填充后的 `X_shared[i,j,k]` 满足 \(\tilde{X}[i,j,k] = X[i, j+k]\)（越界处为 0）。具体 GPU 上的逐元素值「待本地验证」（可用 `print_source_code` 配合小规模手算确认逻辑）。

#### 4.2.5 小练习与答案

**练习 1**：im2col 矩阵 `X_shared` 的形状是 `(BLOCK_N, BLOCK_L, KL)`。如果 `BLOCK_N=16, BLOCK_L=32, KL=32`，一个 block 的 im2col 矩阵有多少元素？相比朴素版只存 `(BLOCK_N, BLOCK_L+KL)` 的输入 tile，多用了多少倍存储？

> **答案**：im2col tile 有 \(16\times32\times32 = 16384\) 个元素；朴素版输入 tile 有 \(16\times(32+32)=1024\) 个元素。im2col 多用了约 16 倍存储（因为每个空间位置都重复存了 `KL` 个抽头）。这就是 im2col「以空间换 Tensor Core 算力」的代价，也是为何我们只在 block 内做局部 im2col、而不是全局展开。

**练习 2**：把 `T.if_then_else(cond, X[...], 0)` 换成朴素版的写法「先 `T.copy` 整段、再在内层 `if` 跳过」，im2col 还能直接喂给 `T.gemm` 吗？

> **答案**：不能（会很别扭）。`T.gemm` 要求输入是稠密矩阵、规约维完整出现在列里。朴素写法的「跳过」是**计算时**的判断，矩阵里没有显式的 0，结构上不再是干净的 \(\tilde{X}_{mat}\)。im2col 的精髓就是**在数据准备阶段**把越界处显式填 0，让后续乘法完全无需判边界。

---

### 4.3 T.reshape + T.gemm(clear_accum)

#### 4.3.1 概念说明

4.2 构建出的 `X_shared` 形状是 `(BLOCK_N, BLOCK_L, KL)`——这是 im2col 张量的**三维视图**，但矩阵乘需要**二维**操作数 `(M, K) @ (K, N)`。`T.reshape` 就是把三维视图「无拷贝地」 reinterpret 成二维矩阵。

关键在于行主序（C-contiguous）下的 reshape 是「免费的视图」：三维 `(BLOCK_N, BLOCK_L, KL)` 在内存里按行主序排布，元素 `X_shared[i, j, k]` 的线性偏移是：

\[
\text{offset}(i,j,k) = \big((i\cdot \text{BLOCK\_L}) + j\big)\cdot \text{KL} + k
\]

reshape 成 `(BLOCK_N*BLOCK_L, KL)` 后，元素 `X_reshaped[r, c]` 的偏移是 \(r\cdot \text{KL} + c\)。两者相等当且仅当：

\[
r = i\cdot \text{BLOCK\_L} + j, \qquad c = k
\]

也就是说 **reshape 把空间位置 `(i, j)` 展平成行号 `r`、把抽头 `k` 对齐到列 `c`**——这恰好就是 im2col 矩阵想要的布局！reshape 因此无需搬运任何数据，纯粹改变下标解释，`T.gemm` 拿到的正是正确的 \(\tilde{X}_{mat}\)。

`T.gemm(X_reshaped, K_shared, O_local, clear_accum=True)` 随后完成整个卷积：

- 语义 `O_local = X_reshaped @ K_shared`，其中 `X_reshaped` 是 `(BLOCK_N*BLOCK_L, KL)`、`K_shared` 是 `(KL, F)`、`O_local` 是 `(BLOCK_N*BLOCK_L, F)`。
- **整个 `KL` 规约被一次 `T.gemm` 吃掉**——不再需要 4.1 朴素版那种 `T.Serial(KL)` 循环，因为矩阵乘的 K 维就是 `KL`。
- `clear_accum=True` 表示在累加**之前**自动清零 `O_local`。这里只调用一次 `T.gemm`，没有跨段累加，所以用 `clear_accum=True` 等价于「先 `T.clear` 再累加」，比朴素版的「`T.clear` + `T.Serial` 内层 `+=`」更紧凑。
- 输入在 shared memory、累加器在 fragment（float32），`T.gemm` 自动完成 fp16×fp16→fp32 的混合精度乘加，并落到 Tensor Core MMA。

最后 `O_local` 是 `(BLOCK_N*BLOCK_L, F)` 的 2D 结果，再用一次 `T.reshape` 还原成 `(BLOCK_N, BLOCK_L, F)`（同样的行主序逆映射），回写到输出 `O` 的对应 tile。

#### 4.3.2 核心流程

```
每个 block (pid_n, pid_l):
  1. X_shared = shared[BLOCK_N, BLOCK_L, KL]    # im2col 张量（4.2 已填好）
  2. K_shared = shared[KL, F]
  3. O_local  = fragment[BLOCK_N*BLOCK_L, F]     # 2D GEMM 输出

  4. X_reshaped = T.reshape(X_shared, (BLOCK_N*BLOCK_L, KL))   # 3D -> 2D 视图（免费）
  5. T.copy(K -> K_shared)
  6. T.gemm(X_reshaped, K_shared, O_local, clear_accum=True)   # 一步矩阵乘，落 Tensor Core
  7. O_reshaped = T.reshape(O_local, (BLOCK_N, BLOCK_L, F))    # 2D -> 3D 还原
  8. T.copy(O_reshaped -> O tile)
```

与 4.1 朴素版的**结构对照**：

| 环节 | 朴素多通道 (4.1) | im2col (4.3) |
| --- | --- | --- |
| KL 规约方式 | `T.Serial(KL)` 手写 `+=` | 一次 `T.gemm`，K 维 = KL |
| 计算单元 | CUDA Core 标量 FMA | Tensor Core MMA |
| 累加器清零 | 显式 `T.clear(O_local)` | `clear_accum=True` 隐式清零 |
| 边界处理 | 累加内层 `if j+k<L` | 构建 im2col 时 `T.if_then_else` 填 0 |
| 输出 tile 形状 | `(BLOCK_N, BLOCK_L, F)` 3D | `(BLOCK_N*BLOCK_L, F)` 2D，再 reshape |

#### 4.3.3 源码精读

`T.gemm` 调用及其前后的 reshape，见 [ans/09-conv.py#L268-L272](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L268-L272)：

```python
X_reshaped = T.reshape(X_shared, (BLOCK_N * BLOCK_L, KL))   # im2col 矩阵 2D 视图
T.copy(K, K_shared)
T.gemm(X_reshaped, K_shared, O_local, clear_accum=True)     # O_local = X_reshaped @ K_shared
O_reshaped = T.reshape(O_local, (BLOCK_N, BLOCK_L, F))      # 还原成 3D
T.copy(O_reshaped, O[pid_n * BLOCK_N, pid_l * BLOCK_L, 0])
```

几个要点：

- `T.reshape(X_shared, ...)` 作用于 **shared buffer**，返回一个可喂给 `T.gemm` 的 2D 视图——它不分配新存储、不搬运数据，只改下标语义。
- `T.gemm` 的三个操作数分别来自 shared（`X_reshaped, K_shared`）与 fragment（`O_local`），正是 u4-l4 强调的「shared 作输入、fragment 作累加器」布局，对齐 Tensor Core MMA 的偏好。
- `clear_accum=True`：因 `O_local` 只参与这一次 `T.gemm`，用 `clear_accum=True` 代替 `T.clear(O_local)` 更简洁；若像 GEMM K 维分块那样多次调用 `T.gemm` 累加，则首段用 `clear_accum=True`、后续段用 `clear_accum=False`（参见 u4-l3）。
- 回写 `O[pid_n*BLOCK_N, pid_l*BLOCK_L, 0]` 与 4.1 一致，是 `(N, L, F)` 输出里的三维 tile 锚点。

`threads=256`：注意 im2col 版的 `T.Kernel` 显式带了 `threads=256`（[ans/09-conv.py#L259](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L259)）。这是因为 `T.gemm`/Tensor Core 对线程布局有要求，显式指定 threads 让编译器生成正确的 warp 划分；朴素版没写 `threads`，用了默认值。

完整 kernel 可读 [ans/09-conv.py#L250-L274](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L250-L274)。

#### 4.3.4 代码实践

**目标**：补全 im2col 版 `T.gemm` 调用，并与朴素多通道版对比正确性与耗时。

**步骤**：

1. 在 [tl_conv1d_im2col 的 TODO](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L220-L230) 里，按 4.2 + 4.3 完整补全：填充 `X_shared`（`T.if_then_else` 零填充）→ `T.reshape` → `T.copy(K)` → `T.gemm(..., clear_accum=True)` → `T.reshape` 还原 → 回写。
2. 运行测试与基准。题目自带的入口见 [puzzles/09-conv.py#L233-L264](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/09-conv.py#L233-L264)（puzzle 文件用 `N=L=128` 做快速正确性；答案文件 [ans/09-conv.py#L277-L308](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/09-conv.py#L277-L308) 用 `N=L=1024` 做更有意义的性能对比）。
   ```bash
   python3 puzzles/09-conv.py      # 用你补全的 puzzle 文件
   # 或直接看答案行为：
   python3 ans/09-conv.py
   ```
3. （可选）用「正确性 + 生成代码 + 性能」三件套（u2-l2）诊断：
   ```python
   kernel = tl_conv1d_im2col.compile(N=1024, L=1024, KL=32, F=32, BLOCK_N=16, BLOCK_L=32)
   print(kernel.get_kernel_source())   # 在 im2col 版里应能看到 Tensor Core 的 mma 指令
   ```

**需要观察的现象**：

- `test_puzzle` 对 `tl_conv1d_multi_outchannel` 与 `tl_conv1d_im2col` 都打印 `✅ Results match: True`（两者数值一致，因为都是同一个数学定义）。
- `bench_puzzle` 打印两者耗时：im2col 版（`Conv1D im2col`）应明显快于朴素多通道版（`Conv1D Multi OutChannel Naive`）。
- im2col 版的生成代码里能找到 Tensor Core 的 `mma`/`wmma` 指令，朴素版里只有标量 `fma`。

**预期结果**：两者正确性一致；im2col 版在大规模（`N=L=1024, F=32`）下更快。具体加速比「待本地验证」（依赖 GPU 型号与 `BLOCK`/`F` 取值）。

#### 4.3.5 小练习与答案

**练习 1**：本 puzzle 的 block 数用 `N // BLOCK_N`（整除）。如果 `N` 不能被 `BLOCK_N` 整除（例如 `N=130, BLOCK_N=16`），会发生什么？怎么修？

> **答案**：`N // BLOCK_N = 8`，只启动 8 个 block、处理 128 行，**丢失最后 2 行**输出，结果错误。修法是把 `N // BLOCK_N` 改成 `T.ceildiv(N, BLOCK_N)`（启动 9 个 block），并在 im2col 填充和回写时加上 `pid_n*BLOCK_N + i < N` 的守卫（参考 docs 实现指南 `docs/zh/9.conv/2.implementation-guide.md` 的写法）。本讲答案之所以用整除，是因为测试规模保证整除；生产代码应始终用 `ceildiv`。

**练习 2**：为什么 im2col 版的 `T.gemm` 只调用一次、不需要像 u4-l3 朴素 GEMM 那样套 `T.Serial(K // BLOCK_K)` 循环？

> **答案**：因为整个 `KL` 维度已经被完整地「物化」成 im2col 矩阵的列（`X_shared` 第三维就是 `KL`，reshape 后是矩阵的 K 维）。`T.gemm` 内部一次性完成对 `KL` 的规约，没有「分块累加」的必要。u4-l3 的朴素 GEMM 之所以要 `T.Serial`，是因为 K 维太大、一次装不下，必须分段加载累加；这里 `KL ≤ 32` 很小，整段放进一个 tile 即可。

**练习 3**：把 `clear_accum=True` 改成 `clear_accum=False`（且不在前面加 `T.clear(O_local)`），`test_puzzle` 会怎样？

> **答案**：`O_local` 是 fragment 累加器，初始内容未定义（垃圾值）。`clear_accum=False` 不清零就直接累加，输出会叠加上随机垃圾，`test_puzzle` 几乎必然 `❌`。这等价于在 GEMM 里忘了初始化累加器（u4-l3 强调过的坑）。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「朴素 → im2col」的改造与对比。

**任务**：在 `puzzles/09-conv.py` 里补全 `tl_conv1d_multi_outchannel`（4.1）与 `tl_conv1d_im2col`（4.2+4.3），然后做下面三件事：

1. **正确性对照**：运行 `run_conv1d_im2col`，确认两个实现都通过 `test_puzzle`（都应 `✅`）。它们产出相同结果，因为都是同一个数学定义的两种实现。
2. **性能对照**：把 `run_conv1d_im2col` 的规模调到答案文件的 `N=L=1024, F=32`，记录 `bench_puzzle` 打印的 `Conv1D Multi OutChannel Naive` 与 `Conv1D im2col` 两个耗时，算出加速比。
3. **生成代码对照**：用 `compile().get_kernel_source()`（或 `print_source_code()`）分别打印两个 kernel 的 CUDA，定位：
   - 朴素版里的标量 `fma`/循环；
   - im2col 版里的 Tensor Core `mma` 指令、`__shared__` 缓冲、以及 reshape 带来的下标计算。

**进阶（可选）**：把 `F` 从 32 调到 128，重新测耗时，观察「输出通道越多，im2col+GEMM 相对朴素版的优势越大还是越小？」并解释（提示：`F` 越大，矩阵乘的 `N` 维越大，Tensor Core 利用率越高；而朴素版的 `f` 维只是多一层并行标量 FMA，吃不到 Tensor Core）。把你的观察写成 3–5 行结论。具体数值「待本地验证」。

> 这个综合实践复用了本讲全部三个最小模块：多通道扩展（4.1）建立基线，im2col 填充（4.2）重构数据布局，`T.gemm`+`clear_accum`（4.3）完成矩阵乘，最后用「正确性 + 生成代码 + 性能」三件套（u2-l2）做评估。

## 6. 本讲小结

- **多输出通道**只是给单通道卷积多套一层 `f`：`K: [KL]→[KL, F]`、`O: [N,L]→[N,L,F]`；它让卷积有「方正」的矩阵形状，是 im2col 值得做的前提（否则退化成 GEMV）。
- **im2col 的本质**是换数据摆法：\(\tilde{X}[i,j,k] = \mathbb{1}[j+k<L]\cdot X[i,j+k]\)，于是卷积 \(O[i,j,f]=\sum_k \tilde{X}[i,j,k]K[k,f]\) 就是矩阵乘 \(O_{mat}=\tilde{X}_{mat}K\)。
- **`T.if_then_else` 做零填充**：构建 im2col 时用全局列坐标判边界，越界抽头填 0，让后续乘法无需再判边界——与朴素版「累加时判 `if j+k<L`」形成对照。
- **`T.reshape` 是免费的视图**：行主序下 `(BLOCK_N, BLOCK_L, KL) → (BLOCK_N*BLOCK_L, KL)` 恰好把空间位置 `(i,j)` 展平成行、抽头 `k` 对齐成列，正是 im2col 矩阵布局，无数据搬运。
- **`T.gemm(..., clear_accum=True)`** 一步吃掉整个 `KL` 规约（无需 `T.Serial` 循环），落到 Tensor Core MMA；`clear_accum=True` 等价于「先清零再累加」，对应朴素版的 `T.clear`。
- **代价是存储**：im2col 把每个空间位置重复存 `KL` 个抽头，局部 tile 存储约放大 `KL/BLOCK_L` 量级，是「以空间换 Tensor Core 算力」。

## 7. 下一步学习建议

- **Puzzle 10 W4A16 量化矩阵乘**（下一讲 u5-l3）：把 im2col+`T.gemm` 的套路再推进一步——在 `T.Pipelined` 流水线里把 INT4 权重现场解量化，再与 `T.gemm` 融合。你会再次用到 `T.alloc_shared` + `T.gemm` + fragment 累加器这套组合。
- **性能工程讲义**（u5-l4）：用 `bench_puzzle` 系统对比本讲的朴素版与 im2col 版，学习调 `BLOCK`/`F`/`num_stages` 的直觉。
- **源码延伸**：阅读 `docs/zh/9.conv/2.implementation-guide.md` 看 `T.ceildiv` + 全边界守卫的「生产级」写法；并把本讲的 1D im2col 思路推广到 2D 卷积（`X: [N,H,W] → X_col: [N·H·W, KH·KW]`），体会 cuDNN 等库为何长期以 im2col/`MMA` 为卷积主力实现。
