# 综合实战：FlashAttention / elementwise 精读

## 1. 本讲目标

本讲是 U8「性能、调优与实战」单元的收口篇，也是整本手册从「读样板」走向「写真实算子」的转折点。前面你已经分别学过了内存层级（u2-l4）、循环与控制流（u2-l3）、`T.gemm` 分派（u4-l2）、张量核发射器（u6-1/u6-2）和完整 GEMM 链路（u6-3）。这些知识点此前都长在 GEMM 这一棵树上，本讲要把它们**迁移到两种新算子**上：

- **elementwise（逐元素加法）**：最简单的「搬进来—算—搬出去」样板，几乎没有 reduction，是理解 TileLang kernel 结构的最小骨架。
- **FlashAttention（注意力前向）**：一个由两次矩阵乘（\(QK^\top\) 与 \(PV\)）、一次 reduction、一次**在线 softmax**交织而成的真实算子，是检验你是否真正掌握「搬进来—算—搬出去 + 分块规约」范式的试金石。

读完本讲你应当能够：

- 逐行读懂 `examples/elementwise/example_elementwise_add.py`，并能用它当模板写出新的 elementwise 算子。
- 理解 `T.reduce`（`T.reduce_max` / `T.reduce_sum`）这一族**分块规约**原语的作用域分派机制与底层 intrinsic。
- 推导**在线 softmax（online softmax）**的递推公式，理解为什么它能在「沿 K 维分块」时不重算整个 softmax。
- 精读 `examples/flash_attention/example_mha_fwd_bshd.py`，画出其分块与在线 softmax 的数据流图。
- 把以上能力迁移到新算子（如 layer normalization）。

## 2. 前置知识

本讲假设你已掌握以下概念（若不熟悉请先回看对应讲义）：

- **四级显存层级**：global / shared / fragment / local，以及 `T.copy`、`T.alloc_shared`、`T.alloc_fragment`、`T.fill` 的作用（u2-l4）。
- **循环原语**：`T.Parallel`（elementwise 并行）、`T.Pipelined(num_stages=...)`（软件流水线，u2-l3、u4-l4）。
- **T.gemm 与分派**：同一个 `T.gemm` 在不同 target 下自动映射到 mma / wgmma / mfma 指令，并支持 `transpose_B`、`policy=T.GemmWarpPolicy.FullRow`（u4-l2、u6-l1）。
- **JITKernel 对象**：`@tilelang.jit` 装饰的函数返回可调用 kernel，提供 `get_profiler()`、`assert_allclose()`、`do_bench()`（u3-l2、u8-l3）。
- **autotuner**：`@autotune(configs=...)` 叠在 `@tilelang.jit` 之上做参数搜索（u8-l1）。

一句话回顾：TileLang 写的是「算什么」（规格），「怎么搬数据、用哪条指令」由编译器按 target 自动决定。本讲的两个算子都不需要你手写任何线程级指令。

## 3. 本讲源码地图

本讲围绕以下文件展开：

| 文件 | 作用 |
| --- | --- |
| `examples/elementwise/example_elementwise_add.py` | 最简单的 elementwise 加法 kernel，是「搬进来—算—搬出去」的最小骨架 |
| `tilelang/language/reduce_op.py` | `T.reduce` / `T.reduce_max` / `T.reduce_sum` / `warp_reduce_*` 的 Python 定义，按作用域分派 |
| `examples/online_softmax/online_softmax.py` | 在线 softmax 的最小范例，展示 max-subtract 与 LSE 递推 |
| `examples/flash_attention/example_mha_fwd_bshd.py` | FlashAttention 前向（BSHD 布局），把两次 gemm、reduction、在线 softmax 串成一个 kernel |
| `examples/norm/layernorm.py` | layer normalization 前向/反向，综合实践的对照参考 |
| `docs/deeplearning_operators/matmul.md` | 官方对 Level 1/2/3 抽象与各原语的说明 |

## 4. 核心概念与源码讲解

### 4.1 elementwise：最简样板 kernel

#### 4.1.1 概念说明

**elementwise（逐元素）算子**指输出每个元素只依赖输入相同位置的元素，元素之间彼此独立——加法、ReLU、激活函数都属此类。它没有 reduction（归约）、没有跨元素依赖，是所有 GPU kernel 里最简单的一类，因此也是学习 kernel 结构的**最佳骨架**。

`example_elementwise_add.py` 计算的是 \(C = A + B\)，但它并没有「偷懒」直接在 global 上加，而是完整演示了 TileLang 的标准四步范式：

1. 把输入 tile 从 global 搬到 shared（`T.copy`）。
2. 在 fragment（寄存器 tile）上做逐元素计算（`T.Parallel` 循环）。
3. 把 fragment 结果搬回 shared。
4. 把 shared 结果搬回 global 的对应位置。

为什么中间要绕一圈 shared/fragment？因为这是真实高性能 kernel 的通用结构——一旦算子变复杂（比如 GEMM、FlashAttention），shared 就是搬运中转站、fragment 就是张量核累加器。elementwise 把这套骨架用最少的代码摆出来，方便你照着改。

#### 4.1.2 核心流程

```
对每个输出 tile C[i, j]（每个线程块负责一个）：
    分配 A_shared、B_shared、C_shared（block_M×block_N）
    分配 C_local（fragment，block_M×block_N）
    把 A[by*block_M, bx*block_N] 搬到 A_shared
    把 B[by*block_M, bx*block_N] 搬到 B_shared
    并行地：C_local[y, x] = A_shared[y, x] + B_shared[y, x]   # T.Parallel
    把 C_local 搬到 C_shared
    把 C_shared 搬到 C[by*block_M, bx*block_N]
```

每个线程块只负责输出的一块，块与块之间互不通信，天然适合并行。

#### 4.1.3 源码精读

整个 kernel 只有 20 行：

[examples/elementwise/example_elementwise_add.py:11-32](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L11-L32) —— 逐点说明：

1. `@tilelang.jit`：把这个 Python 函数包成可调用 kernel（见 u3-l2）。注意它用了 **eager JIT 风格**：`M, N = T.const("M, N")` 声明运行时回填的符号维，函数参数 `A, B` 是调用时传入的真实 torch 张量（见 u2-l1）。
2. `in_dtype` / `out_dtype` 分开：支持输入输出类型不同（如 fp16 输入、fp32 输出），这是 elementwise 的常见需求。
3. `with T.Kernel(T.ceildiv(N, block_N), T.ceildiv(M, block_M), threads=threads) as (bx, by)`：grid 维度是 `(块数_N, 块数_M)`，`bx` 索引列方向、`by` 索引行方向（顺序与 GEMM 例子里一致，见 u6-l3）。

kernel 主体就是上面四步：

[examples/elementwise/example_elementwise_add.py:20-30](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L20-L30) —— 关键点：

- `T.copy(A[by * block_M, bx * block_N], A_shared)`：源是 global 张量的一个 region（左上角坐标），目标是整块 shared。具体走 cp.async / 普通循环由 C++ 侧按 target 选（见 u2-l4）。
- `for local_y, local_x in T.Parallel(block_M, block_N)`：这是本算子唯一的「计算」。`T.Parallel` 表示这 `block_M×block_N` 次迭代由块内线程**并行**分担（每次迭代相互独立，见 u2-l3）。
- `C_local[local_y, local_x] = A_shared[local_y, local_x] + B_shared[local_y, local_x]`：在 fragment 上做加法。fragment 是寄存器 tile，其逻辑下标到物理寄存器的映射由 layout 推断自动决定（u4-l3）。
- 最后两次 `T.copy` 把结果从 fragment → shared → global。

驱动与对拍代码：

[examples/elementwise/example_elementwise_add.py:35-41](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/elementwise/example_elementwise_add.py#L35-L41) —— `out = elementwise_add(a, b, block_M=32, block_N=32, threads=128, ...)` 直接传 torch 张量调用，再用 `torch.testing.assert_close` 与 `ref_program`（即 `x + y`）对拍。

#### 4.1.4 代码实践

**实践目标**：把 elementwise 加法跑通，并把它改写成另一个 elementwise 算子（ReLU），验证你理解了骨架。

**操作步骤**：

1. 运行 `python examples/elementwise/example_elementwise_add.py`（默认 1024×1024）。
2. 复制该文件为 `relu_kernel.py`，把 `C_local[...] = A_shared[...] + B_shared[...]` 改成 ReLU：`C_local[local_y, local_x] = T.max(A_shared[local_y, local_x], 0.0)`，去掉 `B` 相关代码。
3. 修改 `ref_program` 为 `torch.relu(x)`，重新对拍。

**需要观察的现象**：

- 原始加法 kernel 运行无报错（`assert_close` 不抛异常即正确）。
- 你的 ReLU kernel 与 `torch.relu` 结果一致。

**预期结果**：对拍通过。若 ReLU 结果不对，多半是 `T.max` 的第二参数类型不匹配——可写成 `T.Cast(in_dtype, 0.0)`。**待本地验证**。

> 无 GPU 时，可在 `import tilelang` 后只调用 `get_kernel_source()` 查看生成的设备源码，完成「阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 elementwise kernel 里要先把数据搬到 shared 再到 fragment，而不是直接在 global 上算？

**答案**：global memory 访问延迟高且不规整；先搬到 shared（块内共享、可协同访问）再进 fragment（寄存器，最快），是高性能 kernel 的通用骨架。直接在 global 上算虽功能正确，但访存效率低。此外这套 shared/fragment 结构是为复杂算子（GEMM、注意力）准备的，elementwise 只是借它演示。

**练习 2**：如果把 `T.Parallel` 改成 `T.serial`，结果还对吗？性能会怎样？

**答案**：结果仍正确（`T.serial` 只是按序串行执行同样的计算），但性能会大幅下降——`block_M×block_N`（如 32×32=1024）次迭代串行执行，没有利用块内线程并行。`T.Parallel` 才是把迭代分给各线程的关键标注（见 u2-l3）。

---

### 4.2 reduction：分块规约原语

#### 4.2.1 概念说明

elementwise 没有跨元素的依赖，但很多算子需要**规约（reduction）**——把一个 tile 沿某维压缩，例如求行最大值、行和。softmax 需要 rowmax 和 rowsum；layer norm 需要 row 均值和方差。这些都是 reduction。

TileLang 把 reduction 做成了一族 DSL 原语：`T.reduce_max`、`T.reduce_sum`、`T.reduce_min`、`T.reduce_abssum` 等，它们都是 `T.reduce` 的薄封装。与 GEMM 类似，`T.reduce` 在前端只是一行调用，真正的「怎么在 warp/block 内做树形规约」由 C++ 侧的 `tl.tileop.reduce` intrinsic 与 `LowerTileOp` pass 完成（见 u5-l2）。

理解 `T.reduce` 的两个关键点：

- **按作用域分派**：输入、输出 buffer 可以是 shared 或 fragment，四种组合走四条不同路径（必要时插入隐式的 fragment 中转）。
- **`clear` 参数**：控制是否先把输出清零。对 max，`clear=True` 会初始化为 \(-\infty\)；对 sum，`clear=True` 初始化为 0。`clear=False` 则在输出**现有值**基础上继续规约（用于增量规约，FlashAttention 会用到）。

另有一族 `warp_reduce_*`（如 `warp_reduce_sum`）针对**单个寄存器标量**做 warp 内规约（用 shuffle），与 tile 级的 `T.reduce` 互补。

#### 4.2.2 核心流程

`T.reduce(buffer, out, reduce_type, dim, clear)` 的前端分派逻辑：

```
if buffer 是 shared 且 out 是 shared:
    申请 red_frag_in / red_frag_out（fragment）
    if not clear: 把 out 拷到 red_frag_out        # 保留输出旧值
    把 buffer 拷到 red_frag_in
    发 tl.tileop.reduce intrinsic（读 red_frag_in，写 red_frag_out）
    把 red_frag_out 拷回 out
elif buffer 是 shared 且 out 是 fragment:
    申请 red_frag_in，把 buffer 拷过去；直接规约写进 out
elif buffer 是 fragment 且 out 是 shared:
    申请 red_frag_out；if not clear 拷 out；规约写 red_frag_out；拷回 out
elif buffer 是 fragment 且 out 是 fragment:
    直接发 intrinsic（读 buffer，写 out）
```

无论哪条路径，最终都落到同一个 C++ intrinsic `tl.tileop.reduce`，由 `LowerTileOp` pass 在 layout 推断之后降级为 warp 内的树形 shuffle 规约。

#### 4.2.3 源码精读

intrinsic 名字与 reduce 类型枚举：

[tilelang/language/reduce_op.py:18-20](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/reduce_op.py#L18-L20) —— `_REDUCE_OP_KEY = "tl.tileop.reduce"`，`ReduceKind` 列出支持的 8 种规约：`sum`/`abssum`/`max`/`absmax`/`min`/`bitand`/`bitor`/`bitxor`。

`T.reduce` 的核心是一个 macro（因为要按作用域插若干条语句、无返回值）：

[tilelang/language/reduce_op.py:64-136](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/reduce_op.py#L64-L136) —— 这就是上面流程图里四种作用域组合的实现。注意两个细节：

- `clear=False` 时会先把 `out` 拷进 fragment 中转 buffer（`copy(out, red_frag_out)`），保证输出旧值参与规约——这是「增量规约」的来源。
- intrinsic 调用统一用 `tirx.call_intrin("handle", tirx.op.Op.get(_REDUCE_OP_KEY), 输入region, 输出region, reduce_type, dim, clear, annotations=...)`，把 `clear` 透传给 C++。

三个最常用封装：

[tilelang/language/reduce_op.py:139-165](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/reduce_op.py#L139-L165) —— `reduce_max`：默认 `clear=True`（输出先初始化为 \(-\infty\)）。

[tilelang/language/reduce_op.py:190-213](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/reduce_op.py#L190-L213) —— `reduce_sum`：默认 `clear=True`（先清零）。注意 docstring 里的重要提醒：`clear=True` 时不会直接在输出 buffer 上规约，而是经临时 buffer 中转再累加，避免 warp 规约时同一值被累加多次（线程数倍）。

标量级的 warp 规约：

[tilelang/language/reduce_op.py:331-344](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/tilelang/language/reduce_op.py#L331-L344) —— `warp_reduce_sum(value)` 接收一个**寄存器标量**，用 warp shuffle 在 warp 内求和，返回一个所有线程都相同的标量。这与 tile 级 `T.reduce_sum`（操作整个 fragment）互补，适合你已经有一个标量、想跨线程汇总的场景。

#### 4.2.4 代码实践

**实践目标**：用 `T.reduce` 写一个 rowsum kernel（矩阵每行求和），验证你对 `dim` 与 `clear` 的理解。

**操作步骤**：

1. 以 `example_elementwise_add.py` 为模板：输入 `A: T.Tensor((M, N), dtype)`，输出 `S: T.Tensor((M,), accum_dtype)`。
2. kernel 内 `T.copy` 把 A 的 tile 搬到 `A_shared`，再 `T.copy` 到 `A_local`（fragment）。
3. 调 `T.reduce_sum(A_local, sum_row, dim=1, clear=True)`，其中 `sum_row` 是 `(block_M,)` 的 fragment。
4. 把 `sum_row` 拷回 global 的 `S[by*block_M : (by+1)*block_M]`。
5. 对拍 `ref = x.sum(dim=1)`。

**需要观察的现象**：

- 调用 `T.reduce_sum(..., dim=1)` 后，`sum_row` 的形状是 `(block_M,)`，即沿列维（dim=1）压缩。
- 若误把 `dim` 写成 0，会得到沿行压缩的 `(block_N,)`，形状不匹配会报错。

**预期结果**：与 `x.sum(dim=1)` 数值一致（fp32 下应几乎完全相同）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`T.reduce_max(..., clear=True)` 与 `clear=False` 的区别是什么？

**答案**：`clear=True` 先把输出初始化为该规约的单位元（max 为 \(-\infty\)、sum 为 0）再规约，等价于「只对当前输入规约」。`clear=False` 不清零，输出**保留的旧值**也参与规约——用于把新输入增量合并进已有结果（FlashAttention 用它做跨 key 块的运行最大值）。

**练习 2**：为什么 `reduce_sum` 的 docstring 强调 `clear=True` 时要经临时 buffer 中转，而不是直接在 `out` 上规约？

**答案**：warp 内做树形规约时，若直接在输出 buffer 上累加，同一个累加值会被 warp 里每个线程各算一次，导致结果被放大「warp 线程数」倍。经临时 buffer 中转再累加，可避免这种重复累加。

---

### 4.3 在线 softmax（online softmax）

#### 4.3.1 概念说明

**softmax** 把一组实数 \(x_i\) 归一化为概率分布：

\[
\mathrm{softmax}(x_i) \;=\; \frac{e^{x_i}}{\sum_j e^{x_j}}
\]

直接按公式实现会**数值溢出**：\(e^{x_i}\) 在 \(x_i\) 稍大时就爆炸。标准做法是**减去最大值**（max-subtraction trick），因为：

\[
\frac{e^{x_i}}{\sum_j e^{x_j}} \;=\; \frac{e^{x_i - m}}{\sum_j e^{x_j - m}}, \qquad m = \max_j x_j
\]

减去 \(m\) 后指数参数 \(\le 0\)，\(e^{(\cdot)}\in(0,1]\)，数值稳定。

更进一步，**在线 softmax（online softmax）** 解决的是另一个问题：当输入沿某一维**分块**到达时（比如 FlashAttention 里沿 K 维一块一块算 attention score），如何在不重算整行 softmax 的情况下，把新块的贡献增量合并进已有结果？答案是用 **log-sum-exp（LSE）** 的递推：

设已处理部分的最大值为 \(m\)、LSE 为 \(\ell\)（即 \(\ell=\sum_{\text{已处理}} e^{x_j-m}\)），新来一块的最大值为 \(m_{\text{new}}=\max(m,\ m_{\text{block}})\)，则：

\[
m_{\text{new}} = \max(m,\ m_{\text{block}})
\]
\[
\ell_{\text{new}} = e^{m - m_{\text{new}}}\cdot \ell \;+\; \sum_{j\in\text{block}} e^{x_j - m_{\text{new}}}
\]

每来一块就用这两式更新一次，最后 \( \mathrm{softmax} = e^{x_i - m_{\text{new}}}/\ell_{\text{new}} \)。这就是 FlashAttention 能「沿 K 分块」而不必把整行 score 存下来的数学基础。

> 一个工程小技巧：GPU 上 `exp2`（以 2 为底）通常比 `exp`（以 e 为底）快，且 \(e^x = 2^{x\log_2 e}\)。TileLang 的例子普遍把 scale 预乘 \(\log_2 e \approx 1.44269504\)，然后用 `T.exp2` 代替 `T.exp`。

#### 4.3.2 核心流程

`examples/online_softmax/online_softmax.py` 用 LSE 递推实现了一个**可分块**的 softmax（每块处理 `BLOCK_N` 列）：

```
scale = log2(e)
lse = -inf                              # 行级 log-sum-exp，初值 -inf
对每一块 j（沿 N 维，软件流水）：
    把 X 的这一块搬进 fragment x
    max_x = rowmax(x)                   # 当前块的最大值  (T.reduce_max)
    exp_x = exp2(x * scale - max_x * scale)     # 当前块的归一化指数
    sum_exp_x = rowsum(exp_x)           # 当前块的指数和  (T.reduce_sum)
    # LSE 递推（在 log 空间合并）：
    lse = max_x * scale + log2( exp2(lse - max_x*scale) + sum_exp_x )
最后再扫一遍，用最终 lse 归一化输出：y = exp2(x*scale - lse)
```

这里的 `lse = max_x*scale + log2(...)` 就是上面递推式 \(\ell_{\text{new}}=e^{m-m_{\text{new}}}\ell+\sum e^{x_j-m_{\text{new}}}\) 取对数后的形式，使得数值始终保持在 log 空间，不会爆炸。

#### 4.3.3 源码精读

scale 的设置（`log2(e)` 技巧）：

[examples/online_softmax/online_softmax.py:14](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/online_softmax/online_softmax.py#L14) —— `scale = 1.44269504  # log2(e)`，把 e 底指数转成 2 底。

LSE 递推的代码（核心三行）：

[examples/online_softmax/online_softmax.py:27-35](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/online_softmax/online_softmax.py#L27-L35) —— 逐行对照上面的公式：

- `T.reduce_max(x, max_x, dim=1, clear=True)`：当前块的行最大值 \(m_{\text{block}}\)。
- `exp_x[i,j] = T.exp2(x[i,j]*scale - max_x[i]*scale)`：当前块减最大值后的指数（数值稳定）。
- `T.reduce_sum(exp_x, sum_exp_x, dim=1, clear=True)`：当前块的指数和。
- `lse[i] = max_x[i]*scale + T.log2(T.exp2(lse[i] - max_x[i]*scale) + sum_exp_x[i])`：LSE 递推。`T.exp2(lse - max_x*scale)` 把旧 lse 重新归一到新的最大值基准下，再与新块的指数和相加，取 log2 回到 log 空间。

> 注意 `lse` 初值是 `-inf`（见 [online_softmax.py:23](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/online_softmax/online_softmax.py#L23) `T.fill(lse, -T.infinity(accum_dtype))`）。第一块时 `exp2(-inf - max_x*scale) = 0`，递推自然退化为 `lse = max_x*scale + log2(sum_exp_x)`，即单块的 LSE，正确。

第二趟用最终 `lse` 做归一化：

[examples/online_softmax/online_softmax.py:37-43](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/online_softmax/online_softmax.py#L37-L43) —— `y[i,j] = T.exp2(x[i,j]*scale - lse[i])` 即 \(\mathrm{softmax}=e^{x-m}/\ell\)，搬回 global。

#### 4.3.4 代码实践

**实践目标**：验证在线 softmax 与朴素 softmax 数值一致，并理解「分块」不影响结果。

**操作步骤**：

1. 运行 `python examples/online_softmax/online_softmax.py`（默认 8192×8192，fp16）。
2. 把 `BLOCK_N` 从 8192 改成 4096（即沿 N 分两块），重新运行。

**需要观察的现象**：

- 两次都通过 `torch.testing.assert_close(Y, X.softmax(dim=1), ...)`。
- 改小 `BLOCK_N` 后，第一趟 `for i_n in T.Pipelined(...)` 会循环两次，LSE 递推被触发；结果不变。

**预期结果**：分块前后结果一致（在线 softmax 的「在线」二字正体现在此——分多少块都不影响数学结果）。延迟方面，`BLOCK_N` 变小会多一次循环但每次搬运量更小，受软件流水线影响，**具体数值待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 scale 要乘 `log2(e)`，并用 `T.exp2` 而不是 `T.exp`？

**答案**：GPU 上 `exp2`（2 的幂）通常比 `exp`（e 的幂）快。利用恒等式 \(e^x = 2^{x\log_2 e}\)，把 scale 预乘 \(\log_2 e\approx 1.4427\)，就能用更快的 `exp2` 算出同样的 \(e^x\)。

**练习 2**：LSE 递推里为什么要把旧 `lse` 写成 `exp2(lse - max_x*scale)` 再加新块？

**答案**：旧 `lse` 是在「旧最大值」基准下算的，新块带来了更大的 `max_x`，基准变了。`exp2(lse - max_x*scale)` 把旧 LSE 重新归一到新基准下（相当于乘 \(e^{m_{\text{旧}}-m_{\text{新}}}\)），再与新块的指数和相加，才能得到正确的新 LSE。这正是 max-subtraction trick 在「增量合并」时的推广。

---

### 4.4 FlashAttention：把一切串起来

#### 4.4.1 概念说明

**注意力（attention）** 是 Transformer 的核心运算。给定查询 \(Q\)、键 \(K\)、值 \(V\)，标准注意力为：

\[
\mathrm{Attn}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right) V
\]

朴素实现要先把整个 \(QK^\top\)（\(L\times L\)，\(L\) 为序列长度）算出来存进显存，再 softmax，再乘 \(V\)——对长序列（如 \(L=4096\)）这个中间矩阵极大，访存与显存都是灾难。

**FlashAttention** 的核心思想是：**沿 K 维分块**，用上一节的**在线 softmax** 增量合并每一块的贡献，同时**增量累加**输出 \(O\)，从而不必把整个 \(QK^\top\) 物化。每个线程块只负责一段 query（`block_M` 行），沿 key 维循环 `block_N` 一块块处理：

\[
S = QK^\top \quad(\text{当前 key 块}),\qquad
P = \mathrm{softmax}_{\text{在线}}(S)
\]
\[
O \;\leftarrow\; O\cdot \text{rescale} \;+\; P\,V
\]

这里的 `rescale` 就是在线 softmax 里「基准最大值变了，旧累加器要乘一个修正因子」的那一项。于是 FlashAttention 把**两次 `T.gemm`**（\(QK^\top\) 与 \(PV\)）、**两次 `T.reduce`**（rowmax、rowsum）、**在线 softmax 递推**、**软件流水线**全部揉进一个 kernel，是检验你是否真正掌握本手册前 8 单元的最佳算子。

本例采用 **BSHD 布局**（Batch, Seq, Head, Dim）：`shape = [batch, seq_len, heads, dim]`。

#### 4.4.2 核心流程

```
每个线程块负责 (bz 批, by 头, bx 段 query，block_M 行)
初始化：
    acc_o = 0                         # 输出累加器 O（block_M × dim）
    logsum = 0                        # 行 LSE（block_M）
    scores_max = -inf                 # 行运行最大值 m（block_M）

for k in 沿 key 维分块（软件流水线 num_stages）：
    # ① 第一次 gemm：算当前块的 score S = Q @ K^T / sqrt(d)
    把 K 的第 k 块搬到 K_shared
    （causal 时把未来位置置 -inf 做掩码）
    acc_s = Q_shared @ K_shared^T     # T.gemm(transpose_B=True)

    # ② 在线 softmax 递推（更新 m 与 logsum，并 rescale acc_o）
    scores_max_prev = scores_max      # 保存旧 m
    scores_max = rowmax(acc_s)        # 当前块最大值
    scores_max = max(scores_max, scores_max_prev)   # m_new
    scores_scale = exp2((m_old - m_new) * scale)    # acc_o / logsum 的修正因子
    acc_s = exp2((acc_s - m_new) * scale)           # 当前块归一化指数 P
    scores_sum = rowsum(acc_s)                      # 当前块指数和
    logsum = logsum * scores_scale + scores_sum     # LSE 递推
    acc_o *= scores_scale              # 用修正因子 rescale 旧输出

    # ③ 第二次 gemm：累加 P @ V
    把 V 的第 k 块搬到 V_shared
    acc_o += acc_s_cast @ V_shared     # T.gemm（acc_s_cast 为 fp16）

# ④ 末尾用 logsum 归一化输出
acc_o /= logsum
把 acc_o 写回 Output
```

数学上，这就是把 4.3 的 LSE 递推与一个「同步缩放的输出累加器」耦合在一起：每次基准最大值变大，旧的 \(O\) 与 \(\ell\) 都要乘同一个修正因子 \(e^{m_{\text{旧}}-m_{\text{新}}}\)，保证最终 \(O/\ell\) 不变。

#### 4.4.3 源码精读

装饰器与形状定义：

[examples/flash_attention/example_mha_fwd_bshd.py:16-27](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L16-L27) —— `@autotune(configs=..., warmup=10, rep=10)` 叠在 `@tilelang.jit(out_idx=[3], pass_configs={TL_ENABLE_FAST_MATH: True})` 之上。`out_idx=[3]` 表示第 4 个张量 `Output` 是输出；`scale = (1.0/dim)**0.5 * 1.44269504` 即 \(\frac{1}{\sqrt{d}}\log_2 e\)，把 \(\frac{1}{\sqrt{d}}\) 与 `log2(e)` 合进一个常数（见 4.3 的 exp2 技巧）。

启动上下文与所有 buffer 分配：

[examples/flash_attention/example_mha_fwd_bshd.py:36-48](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L36-L48) —— `with T.Kernel(ceildiv(seq_len, block_M), heads, batch, threads=threads) as (bx, by, bz)`：三维 grid，`bx` 是 query 段、`by` 是头、`bz` 是批。buffer 分工：

- `Q_shared`/`K_shared`/`V_shared`/`O_shared`：shared 中转（块内共享）。
- `acc_s`（`block_M×block_N`，fp32）：当前 key 块的 score 累加器。
- `acc_s_cast`（fp16）：score 转 fp16，供第二次 gemm。
- `acc_o`（`block_M×dim`，fp32）：**输出累加器** \(O\)。
- `scores_max`/`scores_max_prev`/`scores_scale`/`scores_sum`/`logsum`（均为 `(block_M,)`，fp32）：在线 softmax 的标量行状态。

初始化（进入 key 循环前）：

[examples/flash_attention/example_mha_fwd_bshd.py:50-53](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L50-L53) —— `T.fill(acc_o, 0)`、`T.fill(logsum, 0)`、`T.fill(scores_max, -T.infinity(accum_dtype))`。注意 `scores_max` 初值 \(-\infty\)，`logsum` 初值 0（因为 \(e^{0}=1\)，第一块递推自然成立）。

key 循环的边界处理（causal）与第一次 gemm：

[examples/flash_attention/example_mha_fwd_bshd.py:55-67](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L55-L67) —— `loop_range` 在 `is_causal` 时取 `min(总块数, ceildiv((bx+1)*block_M, block_N))`，即因果掩码下只算 query 行之前的 key 块（下三角）。`T.gemm(Q_shared, K_shared, acc_s, transpose_B=True, policy=T.GemmWarpPolicy.FullRow)` 算 \(S = QK^\top\)，`transpose_B=True` 因为 K 存的是 `[block_N, dim]` 而 \(K^\top\) 需要 `[dim, block_N]`。`policy=FullRow` 决定 warp 如何切分输出 tile（见 u4-2，MACA 因 warp_size=64 切分与 CUDA 不同）。

**在线 softmax 递推**（本算子最精妙的一段）：

[examples/flash_attention/example_mha_fwd_bshd.py:69-80](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L69-L80) —— 逐行对照 4.4.2 的流程：

- 69-70：保存旧最大值，把 `scores_max` 重置为 \(-\infty\)。
- 71：`T.reduce_max(acc_s, scores_max, dim=1, clear=False)` 求当前块的行最大值（`clear=False` 配合上面 \(-\infty\) 重置，等价于纯块内 rowmax）。
- 72-73：`scores_max = max(scores_max, scores_max_prev)` 得到 \(m_{\text{new}}\)。
- 74-75：`scores_scale = exp2((m_old - m_new)*scale)`，即旧累加器的修正因子。
- 76-77：`acc_s = exp2((acc_s - m_new)*scale)`，当前块归一化指数 \(P\)。
- 78：`T.reduce_sum(acc_s, scores_sum, dim=1)` 当前块指数和。
- 79-80：`logsum = logsum*scores_scale + scores_sum`，LSE 递推。

rescale 输出累加器并做第二次 gemm：

[examples/flash_attention/example_mha_fwd_bshd.py:81-87](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L81-L87) —— `T.copy(acc_s, acc_s_cast)` 把 fp32 score 降精度到 fp16（供 gemm）；`acc_o *= scores_scale` 用修正因子 rescale 旧输出；`T.gemm(acc_s_cast, V_shared, acc_o, policy=FullRow)` 累加 \(O \mathrel{+}= P\,V\)。注意两次 gemm 的输出累加器都是 `acc_s`/`acc_o`，即 `T.gemm` 默认是**累加**（`+=`），与 GEMM 例子一致。

末尾归一化与写回：

[examples/flash_attention/example_mha_fwd_bshd.py:89-92](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L89-L92) —— 循环结束后 `acc_o /= logsum`（即 \(O/\ell\)），再 fragment → shared → global 三段写回。

驱动与对拍：

[examples/flash_attention/example_mha_fwd_bshd.py:124-135](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/flash_attention/example_mha_fwd_bshd.py#L124-L135) —— `profiler.assert_allclose(ref_program_processed, rtol=0.01, atol=0.01)` 做数值校验，`profiler.do_bench(warmup=500)` 测延迟，TFLOPS 用 `total_flops / latency * 1e-9`（`total_flops = 2 * batch*heads*seq_len*seq_len*dim*2`，两次 gemm；causal 时 ×0.5）。

#### 4.4.4 代码实践

**实践目标**：画出 FlashAttention 的分块与在线 softmax 数据流图，把上面的源码「可视化」。

**操作步骤**：

1. 在纸上（或任何画图工具）画出三类 buffer：`Q_shared`、`K_shared`/`V_shared`（沿 k 循环流动）、状态 buffer（`acc_o`、`logsum`、`scores_max`）。
2. 标出 key 循环每一步的数据流向：①`Q@K^T → acc_s` ②`reduce_max → scores_max` ③`exp2 → acc_s(P)` ④`reduce_sum → scores_sum` ⑤`logsum 递推` ⑥`acc_o *= scores_scale` ⑦`P@V → acc_o`。
3. 用箭头标出 `scores_scale` 同时作用于 `logsum`（步骤⑤）和 `acc_o`（步骤⑥）——这是它俩必须同步缩放的关键。
4. 运行 `python examples/flash_attention/example_mha_fwd_bshd.py`（默认 batch=8, heads=32, seq_len=4096, dim=128），观察 `All checks pass.` 与延迟/TFLOPS。
5. 加 `--is_causal` 跑因果版本，对比 TFLOPS（应约为非因果的 2 倍，因为算一半）。

**需要观察的现象**：

- 非因果版打印 `All checks pass.` 与延迟、TFLOPS。
- 因果版 TFLOPS 约为非因果版的 2 倍（`total_flops *= 0.5` 且计算量减半）。

**预期结果**：数据流图应清楚体现「两次 gemm 夹一次在线 softmax」的结构，且 `scores_scale` 同时驱动 `logsum` 与 `acc_o` 的 rescale。延迟具体数值**待本地验证**（依赖 GPU 型号）。

> 无 GPU 时，把 `main()` 改成只调 `get_kernel_source()` 查看生成的设备源码，重点看两次 gemm 如何被分派成 mma/wgmma/mfma，以及 `T.reduce` 如何变成 warp 内 shuffle 规约。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `logsum` 初值是 0，而 `scores_max` 初值是 \(-\infty\)？

**答案**：`scores_max` 是「行最大值」，初值须为 \(-\infty\) 才能让第一块的 rowmax 通过 `max(-inf, ...)` 正确胜出。`logsum` 是 LSE 的「指数和」部分，初值须是其单位元 0（即 \(e^0=1\)，乘进去等于不改变第一块贡献）。两者是各自规约运算的单位元。

**练习 2**：如果删掉 `acc_o *= scores_scale`（第 84 行），结果会怎样？

**答案**：会出错。`scores_scale = exp2((m_old - m_new)*scale)` 是把旧累加器 \(O\) 重新归一到新最大值基准下的修正因子。当某块的 rowmax 大于之前所有块时，`scores_scale < 1`，旧 \(O\) 必须相应缩小，否则末尾 `O/logsum` 的归一化就不成立——因为 `logsum` 已经按 `scores_scale` 缩放了，`acc_o` 必须同步缩放。

**练习 3**：两次 `T.gemm` 都用了 `policy=T.GemmWarpPolicy.FullRow`，这代表什么？

**答案**：`GemmWarpPolicy` 决定 warp 如何切分输出 tile（见 u4-2）。`FullRow` 表示一个 warp 负责输出 tile 的完整若干行，便于后续的 row 级 reduction（`reduce_max`/`reduce_sum` 沿 dim=1）在同一 warp 内高效完成。这与 GEMM 例子默认的 `Square` 切分不同，是注意力算子为配合 row 规约做的特意选择。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**实现一个 layer normalization（层归一化）前向 kernel**，并对拍、测延迟。

layer norm 对张量 `X (N, D)` 的每一行做：

\[
\mu_i = \frac{1}{D}\sum_j X_{ij}, \qquad
\sigma_i^2 = \frac{1}{D}\sum_j (X_{ij}-\mu_i)^2
\]
\[
Y_{ij} = \gamma_j \cdot \frac{X_{ij}-\mu_i}{\sqrt{\sigma_i^2+\epsilon}} + \beta_j
\]

它正好把本讲的元素都串起来：elementwise 计算（减均值、除标准差、仿射）、两次 reduction（行和、行平方和），且和 softmax 一样需要先规约再逐元素。

要求：

1. 以 `examples/elementwise/example_elementwise_add.py` 的骨架为模板（`@tilelang.jit`、`T.Kernel`、`T.copy`、`T.Parallel`）。
2. 输入 `X (N, D)`、`gamma (D,)`、`beta (D,)`，输出 `Y (N, D)`，用 fp32 累加。
3. kernel 内：把 X tile 搬到 shared，cast 到 fragment；用 `T.reduce_sum` 算行和 `sum_row` 与行平方和 `sumsq_row`（见 4.2）；再算 `mean = sum_row / D`、`rstd = rsqrt(sumsq_row/D - mean^2 + eps)`；最后 elementwise 算 `Y`（见 4.1）并写回。
4. 对拍 `torch.nn.functional.layer_norm(x, (D,), gamma, beta, eps)`。
5. 用 `profiler.do_bench()` 或 `tilelang.profiler.do_bench` 量延迟。

参考实现就在仓库里——`examples/norm/layernorm.py` 的 `_layernorm_fwd`。你可以先自己写，写不出来再对照。重点对照两处：

[examples/norm/layernorm.py:39-40](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/norm/layernorm.py#L39-L40) —— 两次 `T.reduce_sum` 算行和与行平方和（先在 fragment 上算好 `X_sq = X*X`）。

[examples/norm/layernorm.py:43-54](https://github.com/tile-ai/tilelang-metax/blob/60e2199fa6a972a526a3712d929c92ef8f09b9c1/examples/norm/layernorm.py#L43-L54) —— 由行和/平方和推出 `mean`、`rstd`（用 `T.rsqrt`），再做逐元素归一化与仿射，注意 `T.Cast` 在 dtype 间的转换。

> **二选一的替代任务**：如果你更想巩固 FlashAttention，则精读 `example_mha_fwd_bshd.py` 并按 4.4.4 画出完整数据流图，标注两次 gemm、两次 reduce、在线 softmax 递推与 `scores_scale` 的同步 rescale 路径。

验收标准：

- 你的 layer norm 与 `torch.nn.functional.layer_norm` 对拍通过（bfloat16 下 `rtol=1e-2, atol=1e-2`）。
- 能用一句话指出 kernel 里「先规约、后逐元素」的依赖顺序为何不能颠倒（规约要先算出整行的 mean/rstd，逐元素归一化才能用到它们）。
- 量出前向延迟并与 torch 参考实现对比。

完成这个任务，你就把本讲的 elementwise 骨架（4.1）、reduction 原语（4.2）、在线/分块规约思想（4.3）和「真实算子的全链路组织」（4.4）全部用上了，具备从零写一个带规约的真实算子的最小工作流。

## 6. 本讲小结

- **elementwise** 是最简 kernel 骨架：`T.copy` 搬进 shared/fragment → `T.Parallel` 逐元素算 → `T.copy` 搬回 global。它是写任何新算子的起点模板。
- **`T.reduce`** 是 tile 级分块规约原语（`reduce_max`/`reduce_sum`/...），前端按 shared/fragment 四种作用域组合分派，统一落到 C++ `tl.tileop.reduce` intrinsic；`clear` 参数控制是否增量规约。另有标量级 `warp_reduce_*` 做 warp 内 shuffle 规约。
- **在线 softmax** 用 max-subtraction 保证数值稳定，用 **LSE 递推** 实现分块增量合并；TileLang 普遍用 `exp2` + 预乘 \(\log_2 e\) 的 scale 替代 `exp` 换取速度。
- **FlashAttention** 把两次 `T.gemm`（\(QK^\top\)、\(PV\)）、两次 `T.reduce`（rowmax、rowsum）、在线 softmax 递推、输出累加器 rescale、软件流水线揉进一个 kernel，核心是「沿 K 维分块 + 在线合并」，不必物化整个 \(QK^\top\)。
- 两次 gemm 的输出都是**累加**（`+=`），`GemmWarpPolicy.FullRow` 是为配合 row 级 reduction 的特意切分。
- 写带规约的真实算子（如 layer norm）遵循「搬进来 → 规约出统计量 → 逐元素用统计量 → 搬出去」的依赖顺序，不可颠倒。

## 7. 下一步学习建议

- **回看 u6-3（完整 GEMM 例子）**：本讲的两次 `T.gemm` 与 GEMM 例子的「搬进来—算—搬出去」是同一套机制，对照阅读能加深对 `policy`、累加、layout 推断的理解。
- **u8-1（autotuner）**：FlashAttention 例子已用 `@autotune` 包裹，可尝试扩大 `block_M`/`block_N`/`num_stages` 的搜索空间，做一次真正的注意力调优。
- **u8-3（profiling）**：把本讲的 `do_bench` 测量做严谨（warmup、L2 冲刷、backend 选择、tensor 供给），并算出 TFLOPS 与理论峰值对比。
- **阅读更多 examples**：`examples/flash_attention/` 下还有 GQA、varlen、反向（bwd）等变体；`examples/norm/` 下有 rms_norm。它们都在本讲的骨架上扩展，是迁移能力的下一步练习场。
- **尝试 Metax 后端**：把本讲的 kernel 用 `target={"kind":"maca"}` 编译（见 u3-l3、u7-1），观察 `T.gemm` 如何分派到 mfma、`T.reduce` 如何在 warp_size=64 下规约——这是把本手册后半段（U7 MACA 后端）与前半段（DSL 与算子）打通的关键一步。
