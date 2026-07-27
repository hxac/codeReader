# Puzzle 06 Softmax：数值稳定与 Online Softmax

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 softmax 为什么必须「减最大值」才数值稳定，并把这条技巧落到 `T.reduce_max` 与 `T.fill` 上。
- 解释为什么 GPU 上更推荐 `T.exp2` / `T.log2`，并用恒等式 \(\exp(x)=2^{\log_2(e)\,x}\) 把 `exp` 改写成 `exp2`。
- 推导 Online Softmax / log-sum-exp（LSE）的分块递推公式，理解它为何能把朴素「三遍」算法压成「两遍」。
- 独立补全 `tl_softmax` 的两遍实现，并建立从 softmax 到下一讲 FlashAttention 的直觉。

本讲是整个手册里第一个「真正的神经网络算子」，也是第一次把归约（reduce）与逐元素运算（exp）融合进同一个 kernel。

## 2. 前置知识

在进入 softmax 之前，请确认你已经掌握下面这些（来自前几讲）：

- **TileLang kernel 骨架**：`@tilelang.jit`、`T.const`、`T.Tensor`、`T.empty`、`with T.Kernel(...) as 块索引`（见 u1-l2）。
- **元素级并行**：`for i in T.Parallel(N)` 表达逐元素并行（见 u2-l1）。
- **寄存器片段**：`T.alloc_fragment` 把「一个 block 内所有线程的寄存器」抽象成一块可操作的 buffer，`T.copy` 在 global 与 fragment 之间批量搬运（见 u2-l2）。
- **归约 TileOp**：`T.reduce_sum(..., dim=, clear=)` 必须在 fragment 上执行，`T.Serial` 用于「分块串行累加」（见 u3-l2）。

本讲会把上面的零件组装起来。softmax 在数学上就是「先归约出每行的最大值和指数和，再做一次逐元素归一化」，所以它天然同时用到「归约」和「逐元素」两类操作。

补充一点数学直觉。给定一行数 \(x_1,\dots,x_M\)，softmax 把它变成一组「和为 1」的概率：

\[
\mathrm{softmax}(x_i)=\frac{\exp(x_i)}{\sum_{j=1}^{M}\exp(x_j)}
\]

它的关键性质是「对常数平移不变」：给所有 \(x\) 加同一个常数 \(c\)，softmax 结果不变，因为分子分母的 \(\exp(c)\) 会约掉。这条性质正是数值稳定技巧的根。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [puzzles/06-softmax.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/06-softmax.py) | 题目骨架，含 softmax 的数学定义与提示，`tl_softmax` 留有 TODO 待你补全。 |
| [ans/06-softmax.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py) | 参考答案，用 Online Softmax 的两遍算法实现。 |
| [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) | `test_puzzle`（正确性比对）与 `bench_puzzle`（CUDA Event 计时）框架。 |

阅读建议：先读题目文件顶部的 docstring（它把朴素「三遍」定义写得非常清楚），再带着「能不能合成两遍」的问题去看答案。

## 4. 核心概念与源码讲解

本讲的三个最小模块逐层递进：先把 softmax 写稳，再把 `exp` 换成更快的 `exp2`，最后把「三遍」折叠成 Online Softmax 的「两遍」。

### 4.1 数值稳定 softmax 与 reduce_max / fill

#### 4.1.1 概念说明

如果直接按定义 \(\exp(x_i)/\sum_j\exp(x_j)\) 实现 softmax，当某个 \(x_i\) 较大（比如 1000）时，\(\exp(1000)\) 会远远超出 float32 的表示范围，结果变成 `inf`，整个归一化就崩了。

利用「平移不变」性质，我们可以先把整行减去该行的最大值 \(m\)，再做指数：

\[
\mathrm{softmax}(x_i)=\frac{\exp(x_i-m)}{\sum_{j}\exp(x_j-m)},\qquad m=\max_j x_j
\]

因为 \(x_i-m\le 0\)，所以每个 \(\exp\) 都落在 \((0,1]\)，永远不会上溢。这就是**数值稳定 softmax**（numerically stable softmax）。

把这条数学翻译到 TileLang，需要两个新工具：

- `T.reduce_max`：和 `T.reduce_sum` 同族的**归约 TileOp**，沿某个 `dim` 把一个 fragment 压成更小维度，同样**必须在 fragment 上执行**。这里用来求每行的最大值。
- `T.fill`：把一个 fragment 整体填成指定值。求最大值的累加器必须初始化成 \(-\infty\)（而不是 0），否则一行全是负数时「最大值」会被错误地记成 0。题目的提示明确强调了这一点：

> Use `T.fill` to set the initial value of the buffer. `T.clear` sets all elements to zero by default, which may not be what you want.

#### 4.1.2 核心流程

最直白的实现是题目 docstring 里写的「三遍」算法（每行独立处理）：

```text
for i in range(N):                # 每行
    MAX = -inf
    for j in range(M):            # 第 1 遍：求行最大值
        MAX = max(A[i,j], MAX)
    SUM = 0
    for j in range(M):            # 第 2 遍：求 exp(x-MAX) 及其和
        B[i,j] = exp(A[i,j] - MAX)
        SUM += B[i,j]
    for j in range(M):            # 第 3 遍：除以 SUM
        B[i,j] /= SUM
```

三遍意味着要把 `A` 从显存读三遍。第 4.3 节我们会把它压成两遍；这里先把「每遍要做什么」记牢：归约 max、归约 sum、逐元素除法。

#### 4.1.3 源码精读

题目 docstring 把上面的三遍定义完整写在了注释里，是本讲最权威的「规格说明」：

[puzzles/06-softmax.py:53-64](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/06-softmax.py#L53-L64) —— 给出了 `MAX`、`SUM` 两个中间量与三重循环的定义。注意 docstring 标注输出是 float16，但骨架里 `dtype = T.float32`、`B = T.empty((N, M), dtype)`，实际编译与比对的输出 dtype 是 **float32**（以真实代码为准，`test_puzzle` 按实际 KernelParam 决定）。

答案里求行最大值用的是 `T.reduce_max`，且每处理完一个分块就用 `clear=True` 重新计算该块的块内最大值：

[ans/06-softmax.py:106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L106)

> 说明：`T.reduce_max(A_local, cur_max_A, dim=1, clear=True)` 把 `BLOCK_N × BLOCK_M` 的 fragment 沿 `dim=1`（列方向）归约，得到每行的块内最大值 `cur_max_A`（长度 `BLOCK_N`）。`clear=True` 表示每次调用都先把输出清零再归约（因为我们要的是「当前块」的 max，而不是跨块累加）。

而把累加器初始化成 \(-\infty\) 用的是 `T.fill`：

[ans/06-softmax.py:101](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L101)

> 说明：`T.fill(lse, -T.infinity(dtype))` 把保存「行级 LSE」的一维 fragment 填成负无穷。`lse` 是 4.3 节的主角，这里只需记住：「求 max 类累加必须用 `T.fill(-inf)`，不能用 `T.clear`」。

小结这两个关键点：

- 求块内行最大值：`T.reduce_max(..., dim=1, clear=True)`（[ans/06-softmax.py:106](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L106)）
- 用 `T.fill` 把 LSE 初始化为 \(-\infty\)（[ans/06-softmax.py:101](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L101)）

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认你理解了 `T.fill` 与 `T.clear` 的差别，以及 `reduce_max` 的归约方向。

1. 打开 [ans/06-softmax.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py)，定位第 101 行 `T.fill(lse, -T.infinity(dtype))`。
2. 思考实验：如果把它改成 `T.clear(lse)`（即初始化为 0），对于一行全为负数（例如 `A[i,:]` 全部是 `-5`）的输入，第一遍循环里会发生什么？
3. 写下你的预测，再去 4.3 节看完整体后再回头核对。

**预期观察**：把累加器初始化为 0 时，第一次遇到全负行，max 累加器的「基准线」会被钉在 0（而非 \(-\infty\)），导致后续 \(\exp(x_i-0)=\exp(x_i)\) 虽然不溢出但**基准错误**，LSE 的递推会从第一块起就偏离真值，最终 softmax 结果出错。结论：**求 max 的累加器必须用 `T.fill(-inf)` 初始化**。

#### 4.1.5 小练习与答案

**练习 1**：`T.reduce_max(A_local, cur_max_A, dim=1, ...)` 中的 `dim=1` 对应「行最大值」还是「列最大值」？为什么？

**参考答案**：`A_local` 形状是 `(BLOCK_N, BLOCK_M)`，`dim=1` 是「列方向」，沿它归约就是「把每一行的所有列压成一个值」，即**行最大值**，结果 `cur_max_A` 长度为 `BLOCK_N`（每行一个 max）。这与 softmax「按行归一化」的需求一致。

**练习 2**：题目里说输出 `B` 是 float16，但骨架代码实际用的 `dtype` 是什么？这会影响 `test_puzzle` 的比对吗？

**参考答案**：骨架里 `dtype = T.float32`，`B = T.empty((N, M), dtype)`，所以实际输出是 **float32**。`test_puzzle` 会按 `KernelParam` 推断出的真实 dtype（float32）构造张量并与 `torch.softmax` 比对，docstring 里的 float16 只是文字描述，不影响实际行为。

---

### 4.2 exp2 / log2 与 log2_e 恒等式

#### 4.2.1 概念说明

题目提示明确建议「不要用 `T.exp`，改用 `T.exp2`」：

> We recommend not using `T.exp` but instead using `T.exp2`.

原因在于 GPU 硬件指令：计算 \(2^x\) 的 `exp2` 指令通常比计算 \(e^x\) 的 `exp` 指令更快、精度更可控（在很多架构上 `exp` 内部就是先乘 \(\log_2(e)\) 再调 `exp2`）。既然如此，不如自己用一条恒等式显式地走 `exp2`：

\[
\exp(x)=2^{\log_2(e)\,x},\qquad \log_2(e)\approx 1.44269504
\]

于是 `exp(x)` 就改写成 `exp2(x * log2_e)`。同理，求对数时用 `T.log2`。整个答案里**一次 `T.exp` 都没用**，全部走 `exp2`/`log2` + 常数 `log2_e`。

#### 4.2.2 核心流程

把恒等式代入 softmax 的指数部分（先不写减 max，下同）：

\[
\exp(x_i) = 2^{\log_2(e)\,x_i}
\]

归一化时分母 \(\sum_j \exp(x_j)\) 也按同样方式换成以 2 为底。题目骨架里直接提供了常数：

```python
log2_e = 1.44269504
```

[puzzles/06-softmax.py:80](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/06-softmax.py#L80) —— 在 kernel 函数体顶部定义 `log2_e`，供后续 `exp2` 调用使用。

#### 4.2.3 源码精读

第二遍循环里，最终的逐元素归一化就是用 `exp2` 写的：

[ans/06-softmax.py:122-125](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L122-L125)

> 说明：`B_local[i,j] = T.exp2(A_local[i,j] * log2_e - lse[i])`。这里 `lse[i]` 是第 4.3 节求出的「以 2 为底的 log-sum-exp」，满足 \(2^{\mathrm{lse}[i]}=\sum_j \exp(A[i,j])\)。于是：
>
> \[
> \mathrm{exp2}(A\cdot\log_2 e-\mathrm{lse}) = \frac{2^{A\log_2 e}}{2^{\mathrm{lse}}}=\frac{\exp(A)}{\sum_j \exp(A[j])}=\mathrm{softmax}(A)
> \]
>
> 随后 `T.copy(B_local, B[...])` 把结果写回显存。

注意第一遍循环里计算「块内指数」也用了同一个恒等式（带减块内 max）：

[ans/06-softmax.py:108-109](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L108-L109)

> 说明：`cur_exp_A[i,j] = T.exp2(A_local[i,j]*log2_e - cur_max_A[i]*log2_e)`，即 \(\exp(A_{ij}-\mathrm{cur\_max}_i)\)（把减法提进指数后正好就是 exp2 的两个项）。这就是「减最大值」技巧在本讲的落地形式。

#### 4.2.4 代码实践（可本地运行）

**目标**：用 Python/Torch 验证 `exp2` 与 `log2_e` 恒等式确实等价于 `exp`。

1. 在仓库根目录运行：
   ```bash
   python3 -c "
   import torch, math
   x = torch.randn(8, dtype=torch.float32)
   log2_e = math.log2(math.e)
   a = torch.exp(x)
   b = torch.exp2(x * log2_e)          # torch.exp2 是 2^x
   print('max abs diff:', (a-b).abs().max().item())
   print('log2_e =', log2_e)
   "
   ```
2. 观察最大差值。

**预期结果**：最大差值应在 \(10^{-7}\) 量级（float32 的舍入误差），`log2_e ≈ 1.442695`。这证明 `exp(x)` 与 `exp2(x*log2_e)` 数值上等价，所以用 `exp2` 不损失精度。**待本地验证**（具体差值以你的机器为准）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接写 `T.exp(A_local[i,j] - cur_max_A[i])`，而要写成 `T.exp2(A_local[i,j]*log2_e - cur_max_A[i]*log2_e)`？

**参考答案**：两者数学等价，但 `T.exp2` 直接映射到 GPU 的 `ex2`（\(2^x\)）硬件指令，比 `T.exp`（\(e^x\)）更快、更稳。把减法 `A - max` 拆成两项分别乘 `log2_e` 再喂给 `exp2`，等价于 \(\exp(A-\mathrm{max})\)，却完全绕开了 `exp` 指令。

**练习 2**：题目里 `log2_e = 1.44269504`，它精确等于 \(\log_2 e\) 吗？这点误差会带来问题吗？

**参考答案**：\(\log_2 e \approx 1.4426950408889634\)，题目取了 8 位有效数字，存在约 \(9\times10^{-9}\) 的截断误差。但 softmax 最后要除以总和做归一化，且 `test_puzzle` 用 `atol=rtol=1e-2` 比对，这点常数误差被完全吸收，不影响正确性。

---

### 4.3 Online Softmax / LSE 两遍算法

#### 4.3.1 概念说明

4.1 节的朴素算法要读三遍 `A`。能不能少读一遍？关键量是 **log-sum-exp（LSE）**：

\[
\mathrm{lse}(x)=\log\sum_{j}\exp(x_j)
\]

知道了 LSE，softmax 就是 \(\mathrm{softmax}(x_i)=\exp(x_i-\mathrm{lse}(x))\)（因为 \(\exp(x_i)/\sum_j\exp(x_j)=\exp(x_i-\mathrm{lse})\)）。

**Online Softmax**（在线 softmax）的核心观察是：LSE 可以**分块流式地递推**，不必先扫一遍算全局 max、再扫一遍算 sum。每读进来一个块，就用一条递推公式把「旧的 LSE」更新成「并入新块后的 LSE」。这样第一遍循环就能同时搞定 max 和 sum，第二遍只需再读一次 `A` 做归一化——从三遍压成两遍。

这条「分块流式 + 重缩放」的思路正是 **FlashAttention** 算法的核心：FlashAttention 之所以能不落地完整的 \(N\times N\) 注意力矩阵，就是因为它在 K/V 上滑动分块，用 online softmax 维护一个「当前 LSE」并随时重缩放累计输出。本讲把它练熟，下一讲（u4-l1 标量 FlashAttention）就是顺水推舟。

#### 4.3.2 核心流程

先在自然底数下推导递推式（更直观），再说明答案里如何用 `exp2/log2` 实例化。

设已经处理过若干列，得到旧 LSE 记 \(\mathrm{lse}_{\text{old}}\)，满足 \(\exp(\mathrm{lse}_{\text{old}})=\sum_{\text{已处理}}\exp(x_j)\)。现在读入一个新块，其**块内最大值**为 \(b\)，块内「减 max 后的指数和」为 \(S=\sum_{\text{块}}\exp(x_j-b)\)。

新块的完整指数和是 \(\sum_{\text{块}}\exp(x_j)=\exp(b)\cdot S\)。于是并入新块后的总指数和为：

\[
\exp(\mathrm{lse}_{\text{new}})=\exp(\mathrm{lse}_{\text{old}})+\exp(b)\cdot S
\]

把右边提取公因子 \(\exp(b)\)：

\[
\mathrm{lse}_{\text{new}}=b+\log\!\Big(\exp(\mathrm{lse}_{\text{old}}-b)+S\Big)
\]

这就是 online softmax 的递推式。其中 \(\exp(\mathrm{lse}_{\text{old}}-b)\) 就是「重缩放因子」——当新块的 \(b\) 比之前隐含的最大值更大时，它自动把旧累加值缩小，无需单独维护一个全局 max。

> 数值稳定性：因为 \(\mathrm{lse}_{\text{old}}\ge 0\) 量级有界、\(b\) 取自真实数据，括号内不会出现「极大正数相减」的灾难性抵消，实测稳定。初始化时 \(\mathrm{lse}=-\infty\)，于是 \(\exp(-\infty-b)=0\)，第一块直接退化成 \(\mathrm{lse}_{\text{new}}=b+\log S\)，正是该块的 LSE。

答案把上述公式整体改写到**以 2 为底**：令 \(\mathrm{LSE}_2=\log_2\sum_j\exp(x_j)\)（满足 \(2^{\mathrm{LSE}_2}=\sum_j\exp(x_j)\)），用 \(\log_2(e)\) 把 \(b\) 折算进底数 2，递推式变成代码里的：

\[
\mathrm{LSE}_2 \leftarrow b\cdot\log_2 e+\log_2\!\Big(\mathrm{exp2}(\mathrm{LSE}_2-b\cdot\log_2 e)+S\Big)
\]

最终输出用 \(\mathrm{softmax}(x_i)=\mathrm{exp2}(x_i\cdot\log_2 e-\mathrm{LSE}_2)\) 一次算出（见 4.2.3）。

整体两遍结构：

```text
# 每个 block 负责一条 BLOCK_N 行的水平条带，沿 M 串行分块
T.fill(lse, -inf)                       # 行级 LSE 累加器
# 第 1 遍：流式递推 LSE
for m_blk in T.Serial(M // BLOCK_M):
    读入 A_local
    b      = reduce_max(A_local, dim=1)          # 块内行最大值
    exp_A  = exp2((A_local - b) * log2_e)        # 块内 exp(x-b)
    S      = reduce_sum(exp_A, dim=1)            # 块内指数和
    lse    = b*log2_e + log2(exp2(lse - b*log2_e) + S)   # online 递推
# 第 2 遍：用 LSE 归一化并写回
for m_blk in T.Serial(M // BLOCK_M):
    读入 A_local
    B_local = exp2(A_local * log2_e - lse)
    写回 B_local
```

#### 4.3.3 源码精读

每个 block 处理一条 `BLOCK_N` 行的条带，沿 M 方向分块串行，所以 kernel 只有「行方向」一个 grid 维度：

[ans/06-softmax.py:87](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L87)

> 说明：`with T.Kernel(N // BLOCK_N, threads=256) as pid_n:` 启动 `N//BLOCK_N` 个 block，`pid_n` 标识当前 block 负责第 `pid_n*BLOCK_N` 行起的一条条带。归约发生在 M（列）方向，而 M 很大（默认 16384），所以在 block 内用 `T.Serial` 流式处理。

答案先分配若干 fragment，注意「逐块更新」的量用 `cur_` 前缀、行级累计量单独命名 `lse`：

[ans/06-softmax.py:88-101](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L88-L101)

> 说明：`A_local`/`B_local` 是 `BLOCK_N×BLOCK_M` 的输入/输出 tile；`cur_exp_A`、`cur_max_A`、`cur_sum_exp_A` 每个分块都会被重算（所以用 `cur_` 前缀）；`lse` 是长度 `BLOCK_N` 的**行级**累计量，不随块清零，最后用 `T.fill(lse, -T.infinity(dtype))` 初始化为 \(-\infty\)。

第一遍循环：online 递推 LSE。

[ans/06-softmax.py:104-116](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L104-L116)

> 说明：依次完成「读块（105）→ 块内 max（106）→ 块内 exp（108-109）→ 块内 sum（111）→ online 递推 lse（113-116）」。第 113-116 行就是上一节推导的递推式的逐行落地：
>
> ```python
> lse[i] = cur_max_A[i] * log2_e + T.log2(
>     T.exp2(lse[i] - cur_max_A[i] * log2_e) + cur_sum_exp_A[i]
> )
> ```

第二遍循环：读一次 `A`，用 LSE 一次性归一化。

[ans/06-softmax.py:119-125](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L119-L125)

> 说明：`B_local[i,j] = T.exp2(A_local[i,j]*log2_e - lse[i])` 直接得到 softmax 输出，再 `T.copy` 写回。整个 kernel 一共只读 `A` **两遍**（两个 `T.Serial` 各一遍），相比朴素三遍省了一次完整显存读。

#### 4.3.4 代码实践（主任务）

**目标**：在 [puzzles/06-softmax.py:86](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/06-softmax.py#L86) 的 `# TODO` 处补全 `tl_softmax`，实现 Online Softmax 的两遍算法。

**操作步骤**：

1. 在 `with T.Kernel(N // BLOCK_N, threads=256) as pid_n:` 内，按 4.3.3 的结构声明 fragment：`A_local`、`B_local`、`cur_exp_A`、`cur_max_A`、`cur_sum_exp_A`、`lse`。
2. 用 `T.fill(lse, -T.infinity(dtype))` 初始化 LSE。
3. 第一遍 `for m_blk_id in T.Serial(M // BLOCK_M)`：`T.copy` 读入 `A_local`，`T.reduce_max` 求块内行 max，`T.Parallel` + `T.exp2` 算块内 exp，`T.reduce_sum` 求块内和，最后用递推式更新 `lse`。
4. 第二遍 `for m_blk_id in T.Serial(M // BLOCK_M)`：再次 `T.copy` 读入 `A_local`，用 `T.exp2(A_local*log2_e - lse)` 算 `B_local`，`T.copy` 写回 `B`。
5. 运行验证与基准：
   ```bash
   python3 puzzles/06-softmax.py
   ```
6. 想核对时参考 [ans/06-softmax.py:87-127](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L87-L127)。

**需要观察的现象**：

- `test_puzzle` 打印 `✅ Results match: True`（`atol=rtol=1e-2`，与 `torch.softmax` 对齐）。
- `bench_puzzle` 会打印 TileLang 与 torch 两次单次耗时（`Torch time` 与 `Tilelang time`）。
- 两个循环各调用一次 `T.copy` 读 `A`，总共读两遍。

**预期结果**：正确性通过；TileLang 版与 torch 版同为单次 softmax，量级接近。具体耗时与你的 GPU 有关，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么第一遍循环里 `reduce_max` 和 `reduce_sum` 都用 `clear=True`，而 `lse` 却用 `T.fill(-inf)` 只初始化一次、循环里不重置？

**参考答案**：`cur_max_A`/`cur_sum_exp_A` 描述的是「当前块」的统计量，每个块都要重算，所以每次都 `clear=True` 清零后再归约。而 `lse` 是「跨所有块的累计量」，online 递推正是靠它把块间信息传递下去，若每块都清零就丢失了历史，所以只在循环外初始化一次（且必须为 \(-\infty\)），循环内只做递推更新。

**练习 2**：把递推式 `lse = b*log2_e + log2(exp2(lse - b*log2_e) + S)` 改回自然底数，会写成什么？两者等价吗？

**参考答案**：自然底数下为 \(\mathrm{lse}\leftarrow b+\log\big(\exp(\mathrm{lse}-b)+S\big)\)。两者完全等价，只是答案统一用 `exp2/log2 + log2_e` 来贴合 GPU 的 \(2^x\) 指令。可以代入 \(S=\sum_{\text{块}}\exp(x-b)\) 自行验证两边都等于 \(\log\sum\exp(x)\)。

**练习 3**：如果 `M` 不能被 `BLOCK_M` 整除，这个实现会怎样？

**参考答案**：骨架用 `T.Serial(M // BLOCK_M)`，块数按整除计算，末尾不足一个 `BLOCK_M` 的「尾巴」会被漏掉，结果不正确。本讲沿用题目骨架的简化假设（`M` 可被 `BLOCK_M` 整除，默认 `M=16384, BLOCK_M=256` 满足）；工程化处理尾巴需要边界判断或用 `T.ceildiv` + 越界掩码，超出本讲范围，留作进阶思考。

## 5. 综合实践

把三个模块串起来，做一次「朴素 vs Online」的对照实验。

1. **实现两版 softmax**：在同一个 kernel 骨架里，先按 4.1 的「三遍」思路写一个 `tl_softmax_naive`（第一遍 `reduce_max` 求全局行 max、第二遍 `reduce_sum` 求指数和、第三遍逐元素除），再按 4.3 写一个 `tl_softmax_online`（两遍）。提示：朴素版需要像 [ans/06-softmax.py:101](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L101) 那样用 `T.fill`/`T.clear` 正确初始化各累加器。
2. **正确性对照**：两版都用 `test_puzzle` 跑同一组 `{N, M, BLOCK_N, BLOCK_M}`，确认都打印 `✅`。
3. **性能对照**：用 `bench_puzzle(..., bench_torch=True)` 分别计时，记录 naive / online / torch 三者的单次耗时。
4. **生成代码检视**：对两版分别调用 `tl_softmax.compile(...).print_source_code()`（参考 u2-l2 介绍的方法），数一数生成的 CUDA 里访问全局显存（`A`）的循环各出现几次，验证「三遍 vs 两遍」。
5. 写一份简短观察：online 版相比 naive 版省了什么、快了多少（**待本地验证**），以及它和「读 `A` 的次数」之间的关系。

这个任务同时检验了：数值稳定初始化（`T.fill(-inf)`）、`exp2/log2_e` 恒等式、以及 online 递推，是本讲三块内容的合练。

## 6. 本讲小结

- softmax 必须「减行最大值」才数值稳定；求 max 的累加器要用 `T.fill(-inf)` 初始化，不能用 `T.clear`（会钉死在 0）。
- `T.reduce_max` 与 `T.reduce_sum` 同族，都是「必须在 fragment 上执行」的归约 TileOp，`dim` 决定归约方向、`clear` 决定是否每调用一次就清零。
- GPU 上优先用 `T.exp2`/`T.log2`，借助恒等式 \(\exp(x)=2^{\log_2(e)\,x}\)（`log2_e≈1.44269504`）把 `exp` 全部改写。
- LSE（log-sum-exp）满足 \(\mathrm{softmax}(x_i)=\exp(x_i-\mathrm{lse})\)，是 softmax 的「压缩表示」。
- Online Softmax 用递推式 \(\mathrm{lse}_{\text{new}}=b+\log(\exp(\mathrm{lse}_{\text{old}}-b)+S)\) 把朴素三遍压成两遍，重缩放因子自动处理「新块 max 更大」的情形。
- 这条「分块流式 + 重缩放」正是 FlashAttention 的核心，本讲是下一讲标量 FlashAttention 的直接前置。

## 7. 下一步学习建议

- **下一讲 u4-l1 标量 FlashAttention**：把本讲的 online softmax 从「先 QK 再 softmax 再乘 V」拆开，融合进一个多遍 kernel，attention 的归一化用的就是这里的 LSE 递推。建议先自己用一句话复述本讲的递推式，再去看那一讲。
- **横向巩固 GEMV/GEMM（u4-l2、u4-l3）**：本讲的 `reduce_sum`/累加器思路会直接复用到矩阵乘；尤其注意 float32 累加器（`accum_dtype`）在 softmax 里已经以 `dtype=T.float32` 的形式出现过。
- **源码延伸阅读**：如果想看「工程级」的 online softmax/attention，可在 TileLang 上游示例中搜索 `lse`、`logsumexp` 关键字，对照本讲理解生产实现如何处理尾巴边界与 shared memory。
