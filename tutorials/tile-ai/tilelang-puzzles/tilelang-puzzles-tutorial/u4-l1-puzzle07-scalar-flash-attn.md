# Puzzle 07 标量 FlashAttention

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「标量 FlashAttention」到底简化了什么：为什么本讲里的 \(Q*K\) 是逐元素乘而不是矩阵乘。
- 把上一讲（Puzzle 06 Softmax）里已经写熟的 online LSE（log-sum-exp）两遍算法，原封不动地搬过来，只在第一遍多算一步 `Q*K`、在第二遍多乘一个 `V`，就拼出一个完整的 attention kernel。
- 看懂参考答案里的两遍 kernel：第一遍在线累积每行的 LSE，第二遍用 `softmax(QK) * V` 还原输出。
- 指出这套实现「不高效」的地方（第二遍重算 `Q*K`），并说清楚真正的单遍 FlashAttention 是怎么把 `V` 的累加也融进在线循环的——这是通向完整 FlashAttention 的扩展点。

## 2. 前置知识

本讲是手册第一个「组合型」算子，它不引入新的 TileLang 原语，而是把已学过的积木拼起来。开始前请确认你已经掌握：

- **注意力机制（Attention）的基本形式**。标准 attention 写作 \(\text{Attention}(Q,K,V)=\text{softmax}\!\left(\tfrac{QK^\top}{\sqrt{d_k}}\right)V\)。本讲会大幅简化它，但你要知道这个原始长相。
- **Puzzle 06 的 online softmax / LSE 两遍算法**（u3-l3）。本讲的 LSE 递推公式与它逐字相同，只是输入从单个张量 `A` 变成了 `Q*K`。如果你对 `T.reduce_max`、`T.fill(lse, -inf)`、`T.exp2`、`log2_e` 恒等式、以及 `lse[i] = cur_max * log2_e + log2(...)` 这条递推式还陌生，请先回看上一讲。
- **二维 fragment 与 `T.Parallel(i, j)`、`T.Serial`、`T.copy`、`T.alloc_fragment`**（u2-l2、u2-l3、u3-l2）。本讲每个 block 处理一个 `BLOCK_B × BLOCK_S` 的二维 tile。
- **`T.exp2` / `T.log2` 与 `log2_e` 恒等式**：\(\exp(x)=2^{x\cdot \log_2 e}\)，\(\log_2 e \approx 1.44269504\)。GPU 上 `exp2` 比 `exp` 快，所以我们把所有 `exp` 改写成 `exp2`。

一句话回顾关键结论：上一讲我们用「分块流式 + 重缩放」把朴素三遍 softmax 压成了「在线两遍」，并维护了一个行级压缩量 LSE，满足 \(\text{softmax}(x_i)=\exp(x_i-\text{lse})\)。本讲就是在这个 LSE 上「加两层」——前面加一步 `Q*K`，后面加一步 `*V`。

## 3. 本讲源码地图

本讲只涉及两个源码文件，互为题目与答案：

| 文件 | 作用 |
|------|------|
| [puzzles/07-scalar-flash-attn.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py) | 题目：含问题描述、PyTorch 参考实现 `ref_scalar_flash_attn`、待补全的 kernel 骨架 `tl_scalar_flash_attn`、以及运行入口 `run_scalar_flash_attn`。 |
| [ans/07-scalar-flash-attn.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py) | 参考答案：完整的 `tl_scalar_flash_attn` 两遍实现，是本讲逐行精读的对象。 |

另外会引用 [common/utils.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/common/utils.py) 中的 `test_puzzle` / `bench_puzzle`（已在 u1-l2 详述，本讲直接使用），并对照 [ans/06-softmax.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py) 说明本讲如何复用 softmax 的结构。

## 4. 核心概念与源码讲解

### 4.1 标量 attention 计算流程

#### 4.1.1 概念说明

完整的注意力机制需要：

- \(Q, K, V \in \mathbb{R}^{B\times S\times d}\)（batch、序列长度、每个 token 的特征维）；
- 计算 \(QK^\top\) 得到 \(S\times S\) 的注意力矩阵，复杂度与显存都是 \(O(S^2)\)；
- 除以 \(\sqrt{d_k}\)、softmax、再乘 \(V\)。

这套结构里有两块硬骨头：\(S\times S\) 矩阵乘（GEMM）和 \(O(S^2)\) 的中间矩阵。本讲作为「从 softmax 到 FlashAttention 的过渡」，刻意把这两块都拿掉，做一个**标量版本**：

- **去掉多头**：只有一个头。
- **去掉矩阵乘**：\(Q,K,V\) 都是 \(\mathbb{R}^{B\times S}\)，所谓「打分」\(QK\) 变成**逐元素（Hadamard）乘** \(Q\odot K\)，而不是 \(QK^\top\)。题目注释里强调的「two dimensions: batch size B and sequence length S」就是这个意思。
- **去掉缩放**：不除以 \(\sqrt{d_k}\)。

于是 attention 退化成：

\[
O = \text{softmax}(Q\odot K)\odot V
\]

其中 softmax 沿**每一行**（`dim=1`，即 S 方向）归一化，\(\odot\) 是逐元素乘。这样每个行 \(i\) 之间互不依赖——这正是后面 kernel 只用一维 grid 的原因。

> 「标量」二字的含义：每个注意力「打分」退化成一个标量 \(Q[i,j]\cdot K[i,j]\)，不再是一对 token 之间的内积。它保住了 attention 的**三段式骨架**（打分 → softmax → 加权 V），又把矩阵乘推迟到后面几讲（u4-l2 GEMV、u4-l3 GEMM），让本讲可以专心练「softmax 的复用与算子融合」。

#### 4.1.2 核心流程

对每一行 \(i\)（共 B 行，行间独立）：

1. **打分**：\(QK[i,j] = Q[i,j]\cdot K[i,j]\)，对 \(j=0\dots S-1\)。
2. **softmax**：取该行最大值 \(MAX\)，算 \(P[i,j]=\exp(QK[i,j]-MAX)\)，累加 \(SUM=\sum_j P[i,j]\)，归一化得到 \(\text{softmax}(QK)[i,j]=P[i,j]/SUM\)。
3. **加权**：\(O[i,j]=\text{softmax}(QK)[i,j]\cdot V[i,j]\)。

用伪代码表示（题目注释里给出的定义）：

```
for i in range(B):
    SUM = 0; MAX = -inf
    for j in range(S):                       # 打分 + 找最大
        QK[i,j] = Q[i,j] * K[i,j]
        MAX = max(QK[i,j], MAX)
    for j in range(S):                       # exp + 求和
        P[i,j] = exp(QK[i,j] - MAX)
        SUM += P[i,j]
    for j in range(S):                       # 归一化并乘 V
        O[i,j] = P[i,j] / SUM * V[i,j]
```

> 小提醒：题目注释里第三段写的是 `for j in range(M)`，那是遗留笔误，正确含义是沿 S 遍历——参考实现 [puzzles/07-scalar-flash-attn.py:66](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py#L66) 用 `dim=1`（即 S 方向）做 softmax，与本讲所有公式一致。

这个「朴素三遍」流程完全等价于把上一讲的 softmax 套上前后两步。下一节我们就把它重写成「在线两遍」。

#### 4.1.3 源码精读

先看 PyTorch 参考实现，它一句话定义了正确行为：

[ans/07-scalar-flash-attn.py:59-66](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L59-L66) —— `Q*K` 是**逐元素乘**，`softmax(dim=1)` 沿 S 归一化，`.mul_(V)` 再逐元素乘 V。我们的 kernel 必须在 float32 下与它 `torch.allclose`（`test_puzzle` 默认 atol=rtol=1e-2）。

再注意 kernel 的声明部分，它和上一讲 softmax 几乎一模一样，只是多了一个输入张量 `V`：

[ans/07-scalar-flash-attn.py:75-86](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L75-L86) —— `B, S = T.const("B, S")` 用符号维度声明形状；`Q/K/V` 都是 `(B, S)` 的 float32；`O = T.empty((B, S), dtype)` 是输出；`log2_e` 是编译期常量；`BLOCK_B / BLOCK_S` 作为编译期超参由 `compile` 绑定。

最后看启动配置，这是「行间独立」的直接体现：

[ans/07-scalar-flash-attn.py:85](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L85) —— `with T.Kernel(B // BLOCK_B, threads=256) as pid_b:`。grid 只有一维 `B // BLOCK_B`，每个 block 用 `pid_b` 定位自己负责的 `BLOCK_B` 行；一个 block 内的 `threads=256` 分摊 `BLOCK_B × BLOCK_S` 个元素的计算。**不需要任何跨行、跨 block 的同步**，因为每行的 softmax 与加权都只依赖本行的数据。

#### 4.1.4 代码实践

**实践目标**：在动手写 kernel 前，先用 PyTorch 把「逐元素打分」和「行间独立」这两点确认下来，避免后面误把 \(Q*K\) 当成矩阵乘。

**操作步骤**：

1. 打开 Python（装有 torch），构造小输入，验证参考实现：
   ```python
   import torch
   Q = torch.randn(4, 8, dtype=torch.float32, device="cuda")
   K = torch.randn(4, 8, dtype=torch.float32, device="cuda")
   V = torch.randn(4, 8, dtype=torch.float32, device="cuda")
   O = torch.softmax(Q * K, dim=1).mul_(V)   # ref
   ```
2. 手算第 0 行：`qk = Q[0]*K[0]`，`m = qk.max()`，`p = (qk-m).exp()`，`p = p/p.sum()`，`o0 = p*V[0]`，确认与 `O[0]` 一致。
3. 把 `dim=1` 改成 `dim=0`，观察输出形状不变但数值完全不同——确认 softmax 方向必须是 S（行内）。

**需要观察的现象**：`O[0]` 与手算的 `o0` 数值吻合（float32 下应几乎相等）；改 `dim` 后结果大变。

**预期结果**：你应当直观看到「每一行各自做一次 softmax，再各自乘以 V」，行与行之间互不影响。这正是后面 kernel 用一维 grid、每个 block 处理若干整行的依据。

#### 4.1.5 小练习与答案

**练习 1**：如果误把 `Q * K` 写成了矩阵乘 `Q @ K.T`，本题的形状还合法吗？结果会变成什么？

**答案**：本题 `Q` 是 `(B, S)`，`Q @ K.T` 会得到 `(B, B)`，与后续 `V`（`(B, S)`）无法逐元素相乘，既不符合参考实现也无法运行。本讲的「标量」就是指**不做**这个矩阵乘。

**练习 2**：为什么 kernel 的 grid 维度只需要 `B // BLOCK_B` 一维，而不像 Puzzle 03 那样需要 `(pid_n, pid_m)` 两维？

**答案**：因为 softmax 沿 S 归一化，**同一行**的 S 个元素必须由同一个 block 串行扫完（要累加 LSE），不能拆给不同 block；而行与行之间独立，所以只在 B 方向分块。Puzzle 03 是纯元素级外加法，N、M 两个方向都可以独立分块，故用两维 grid。

---

### 4.2 多遍归约 + exp 融合 kernel

#### 4.2.1 概念说明

4.1 的朴素流程要「三遍读同一行」：一遍算 \(QK\) 与 max、一遍算 exp 与 sum、一遍算输出。上一讲我们已经把它压成「在线两遍」：第一遍维护行级 LSE，第二遍用 LSE 一次性还原 softmax。

本节的核心洞察是：**把 softmax 的输入 `A` 换成 `Q*K`，整套两遍算法一行都不用改**。于是标量 FlashAttention 的 kernel = softmax kernel + 两处小补丁：

| 阶段 | softmax（u3-l3） | 标量 FlashAttention（本讲） | 差异 |
|------|------|------|------|
| 第一遍加载 | `T.copy(A[...], A_local)` | `T.copy(Q[...], Q_local); T.copy(K[...], K_local)` | 多加载一个 K |
| 第一遍打分 | 直接用 `A_local` | `cur_QK = Q_local * K_local` | **新增一步逐元素乘** |
| max / exp / sum / 更新 LSE | 同左 | **完全相同** | 逐字复用 |
| 第二遍加载 | `T.copy(A[...], A_local)` | `T.copy(Q,K,V[...], *_local)` | 多加载 V |
| 第二遍输出 | `B_local = exp2(A*log2_e - lse)` | `O_local = exp2(Q*K*log2_e - lse) * V_local` | **多乘一个 V** |

这里的「融合」有两层含义：

- **exp 融合**：把 `exp`、减 LSE、乘 V 揉进同一个 `T.Parallel(i,j)` 循环体，中间的 `softmax(QK)` 不落回显存。
- **归约融合**：max、exp、sum 都在 fragment（寄存器）上用 `T.reduce_max` / `T.reduce_sum` 完成，依赖上一讲讲过的「归约 TileOp 必须在 fragment 上执行」。

#### 4.2.2 核心流程

回顾 LSE 与 softmax 的关系（上一讲已推导，这里以本讲记号重述）。设 \(x_j = QK[i,j]\)，定义

\[
\text{lse} \;=\; \log_2\!\Big(\sum_j \exp(x_j)\Big),
\]

则有恒等式

\[
\text{softmax}(x_j) \;=\; \exp(x_j - \text{lse}) \;=\; 2^{\,x_j\cdot \log_2 e \;-\; \text{lse}}.
\]

（这里 lse 取以 2 为底，因为我们全程用 `exp2`/`log2`，省一次换底。）

**在线两遍流程**（每个 block 处理 `BLOCK_B` 行，沿 S 切 `BLOCK_S` 大小的块）：

- **第一遍（算 LSE）**：对 `s_blk_id` 在 `T.Serial(S // BLOCK_S)` 上串行扫。每个 S-块内：
  1. 加载 `Q_local, K_local`；
  2. `cur_QK = Q_local * K_local`；
  3. `cur_max_QK = max over BLOCK_S`（`T.reduce_max(dim=1)`）；
  4. `cur_exp_QK = exp2(cur_QK*log2_e - cur_max_QK*log2_e)`（即 \(\exp(x - \text{块内max})\)）；
  5. `cur_sum_exp_QK = sum(cur_exp_QK)`（`T.reduce_sum(dim=1)`）；
  6. 用块内 max \(b=\text{cur\_max\_QK}\cdot\log_2 e\) 把本块的贡献流式并入行级 lse：
     \[
     \text{lse} \;\leftarrow\; b + \log_2\!\Big(2^{\,\text{lse}-b} + \text{cur\_sum\_exp\_QK}\Big).
     \]
- **第二遍（算输出）**：再沿 S 串行扫一遍，每个 S-块内：加载 `Q, K, V`，直接
  \[
  O[i,j] \;=\; 2^{\,QK[i,j]\cdot \log_2 e \;-\; \text{lse}[i]}\cdot V[i,j] \;=\; \text{softmax}(QK)[i,j]\cdot V[i,j].
  \]

**关于 LSE 递推式的直觉**：\(2^{\text{lse}-b}\) 是一个「重缩放因子」——把之前累积好的指数和，从旧的基准（旧 max）换算到新基准 \(b\)（本块 max）下，再与本块的 \(\text{cur\_sum\_exp\_QK}\) 相加。这正是「分块流式 + 重缩放」，与上一讲 softmax 一字不差。

**累加语义的两个关键点**（也是上一讲强调过的坑）：

- `cur_max_QK` / `cur_sum_exp_QK` 是**每块重算**的量，所以每次调用都用 `clear=True`。
- `lse` 是**跨块累积**的量，循环开始前用 `T.fill(lse, -T.infinity(dtype))` 初始化为 \(-\infty\)（求 max 的累加器必须初始化为 \(-\infty\)，不能用默认置零的 `T.clear`），且循环内**不重置**。

#### 4.2.3 源码精读

**fragment 分配**。每个 block 要缓存 `BLOCK_B × BLOCK_S` 的输入/输出 tile，加上若干 `BLOCK_B` 长的「行级」累加量：

[ans/07-scalar-flash-attn.py:86-98](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L86-L98) —— `Q_local/K_local/V_local/O_local` 是二维 tile；`cur_QK`、`cur_exp_QK` 是每块的二维中间结果（命名 `cur_` 前缀表示「每块重算」）；`cur_max_QK`、`cur_sum_exp_QK` 是每块的行级归约；`lse` 是跨块累积的行级 LSE，用 `T.fill(lse, -T.infinity(dtype))` 初始化。

**第一遍：在线算 LSE**。

加载并打分（这是相对 softmax 唯一「新增」的逐元素乘）：

[ans/07-scalar-flash-attn.py:101-107](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L101-L107) —— `T.Serial(S // BLOCK_S)` 串行扫；`T.copy` 用偏移下标 `Q[pid_b*BLOCK_B, s_blk_id*BLOCK_S]` 定位 tile（tile 长度由 fragment 形状推断，u2-l3 讲过）；`cur_QK[i,j] = Q_local[i,j]*K_local[i,j]` 就是 4.1.2 的「打分」。

块内 max、exp、sum（与 softmax 逐字相同）：

[ans/07-scalar-flash-attn.py:108-113](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L108-L113) —— `T.reduce_max(cur_QK, cur_max_QK, dim=1, clear=True)` 沿 S 方向归约出每行块内最大值；`cur_exp_QK` 用 `exp2` 一次性表达「减块内 max 再取 exp」（`log2_e` 把自然底换成 2 底）；`T.reduce_sum(..., clear=True)` 得每行块内指数和。

流式更新 LSE（本节的「心脏」，与 softmax 同构）：

[ans/07-scalar-flash-attn.py:115-118](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L115-L118) —— 令 \(b=\)`cur_max_QK[i]*log2_e`，则 `exp2(lse[i] - b)` 是把旧 LSE 换算到新基准 \(b\) 下的重缩放因子，加上本块的 `cur_sum_exp_QK[i]`，再 `log2`、再加 \(b\)，得到新的行级 LSE。这条公式与 4.2.2 的递推式完全一致。

**第二遍：用 LSE 还原 `softmax(QK)*V`**。

[ans/07-scalar-flash-attn.py:122-132](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L122-L132) —— 再次沿 S 串行扫；这次连 `V_local` 一起加载；循环体里把「重算 QK → exp2 减 LSE → 乘 V」三步**融合**进同一个 `T.Parallel(i,j)`：`exp2(Q_local*K_local*log2_e - lse[i])` 恰是 \(\text{softmax}(QK)[i,j]\)，再乘 `V_local[i,j]` 得到输出；最后 `T.copy(O_local, O[...])` 写回显存。注意第二遍没有再调用任何归约 TileOp——LSE 已经在第一遍算好了，第二遍纯逐元素。

对照 softmax 答案 [ans/06-softmax.py:103-125](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/06-softmax.py#L103-L125)，你会发现两者除「打分」与「乘 V」外完全同构——这正是本讲想让你建立的肌肉记忆。

#### 4.2.4 代码实践

**实践目标**：把题目骨架 `tl_scalar_flash_attn` 补成与参考答案等价的两遍 kernel，并用 `test_puzzle` 验证正确性。

**操作步骤**：

1. 打开 [puzzles/07-scalar-flash-attn.py:84](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py#L84) 的 `# TODO` 处，按下面三步填入（先别看答案）：
   - **fragment 分配**：照 4.2.3 抄一遍 7 个 `alloc_fragment` 与 `T.fill(lse, -inf)`。
   - **第一遍**：`T.Serial(S // BLOCK_S)`，加载 Q/K、算 `cur_QK`、`reduce_max`、算 `cur_exp_QK`、`reduce_sum`、更新 `lse`。
   - **第二遍**：`T.Serial(S // BLOCK_S)`，加载 Q/K/V、`O_local[i,j] = exp2(Q_local*K_local*log2_e - lse[i]) * V_local[i,j]`、`T.copy(O_local, O[...])`。
2. 运行验证：
   ```bash
   python3 puzzles/07-scalar-flash-attn.py
   ```
3. 用 `bench_puzzle` 对比 torch（题目 `run_scalar_flash_attn` 已经带 `bench_torch=True`，直接跑即可；详见 [puzzles/07-scalar-flash-attn.py:100-105](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py#L100-L105)）。

**需要观察的现象**：`test_puzzle` 打印 `✅ Results match: True`（atol=rtol=1e-2）；`bench_puzzle` 打印 `Torch time` 与 `Tilelang time` 两行。

**预期结果**：正确性应为 True。具体耗时与 GPU 型号、S 大小相关——**待本地验证**。一个值得记录的观察是：因为第二遍要重读 Q、K（见 4.3），本讲 kernel 的访存量明显高于「理论下界」，所以它不一定比 torch 快，这正是参考答案里那条 `TODO(chaofan): not very efficient` 注释的含义。

> 排错提示：若结果为 `False` 且**每行都偏大或偏小一个倍数**，多半是 `lse` 没用 `-inf` 初始化（被默认置零，导致基准错位）；若**只有个别行错**，检查 `reduce_max`/`reduce_sum` 的 `dim=1` 是否写对（要沿 S，即列方向归约）。

#### 4.2.5 小练习与答案

**练习 1**：如果把第一遍里 `T.fill(lse, -T.infinity(dtype))` 误写成 `T.clear(lse)`（即初始化为 0），输出会怎样？为什么？

**答案**：`lse` 初值变成 0，相当于假定「已经见过一个 max=0 的空块」。第一块的 `exp2(lse - b) = exp2(-b)` 不再是 0，于是把一个虚假的「1」并入分母，最终 `softmax(QK)` 每行都被多除了一个常数、数值偏小。这正是上一讲强调「求 max 的累加器必须初始化为 \(-\infty\)」的原因。

**练习 2**：第二遍为什么**不需要**任何 `reduce_sum`？请用 4.2.2 的恒等式解释。

**答案**：因为第一遍已经把「归一化分母」压缩进了 `lse`。由 \(\text{softmax}(x_j)=2^{x_j\log_2 e-\text{lse}}\)，只要知道每行的 lse，输出就是纯逐元素运算，不再需要任何跨元素归约。

**练习 3**：题目用 `B=256, S=16384, BLOCK_B=16, BLOCK_S=128`（[puzzles/07-scalar-flash-attn.py:91-94](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py#L91-L94)）。算一下每个 block 要串行扫多少个 S-块。

**答案**：`S // BLOCK_S = 16384 // 128 = 128`。即每个 block 在第一遍和第二遍各串行执行 128 次循环体。由于 `BLOCK_B=16`，一个 block 负责 16 行，16 行之间在 `T.Parallel(i, ...)` 里并行、128 个 S-块之间在 `T.Serial` 里串行。

---

### 4.3 从 softmax 到 FlashAttention 的扩展点

#### 4.3.1 概念说明

4.2 的两遍 kernel 已经是「能跑、正确」的标量 attention，但它还不是真正的 FlashAttention。真正的 FlashAttention 有两个目标：**(a) 单遍**（不再读两遍 Q、K）和 **(b) 不物化 \(S\times S\) 注意力矩阵**。本讲由于是「标量」版本，目标 (b) 天然满足（根本没有 \(S\times S\) 矩阵），但目标 (a) 没有满足——我们的第二遍把 `Q*K` 又算了一遍。

参考答案自己点了名：

[ans/07-scalar-flash-attn.py:120-121](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L120-L121) —— `# TODO(chaofan): Now this implementation is not very efficient.`，紧跟着就是那个重算 `Q*K` 的第二遍。

本节就讲两件事：当前实现「不高效」在哪，以及把它升级成单遍 FlashAttention 需要补什么。这两个点合起来，就是「从 softmax 到 FlashAttention 的扩展点」。

#### 4.3.2 核心流程

**先量化「不高效」**。当前两遍 kernel 的显存访问（按项目文档的口径）：

| 阶段 | 读 | 写 |
|------|----|----|
| 第一遍（算 LSE） | \(2\cdot B\cdot S\)（Q、K） | 0 |
| 第二遍（算输出） | \(3\cdot B\cdot S\)（Q、K、V） | \(1\cdot B\cdot S\)（O） |
| 合计 | \(5\cdot B\cdot S\) | \(1\cdot B\cdot S\) |

痛点在第二遍又读了一遍 Q、K，只为重算 `Q*K`。朴素的「不融合」实现是「读 3、写 3」（写 QK、P、O 三个全尺寸中间张量）；本讲是「读 5、写 1」——**写大幅减少（不物化中间结果），但读反而变多**。

**单遍 FlashAttention 的思路**：把 `*V` 的累加也融进第一遍的在线循环，边扫边维护三个行级状态量：

- 行级 max：\(m\)（对应我们的 `lse` 的「max 部分」）；
- 行级指数和：\(s=\sum \exp(x_j)\)；
- 行级输出累加器：\(o=\sum \text{softmax}(x_j)\cdot V_j\)。

每来一个新块，先用块内 max \(b\) 把旧 \(o\)、旧 \(s\) 重缩放（乘 \(\exp(m_{\text{old}}-b)\)），再并入本块的 \(\exp(x-b)\cdot V\)。扫完一遍后，\(o\) 已经是最终输出，**第二遍整个删掉**，读量从 \(5\cdot B\cdot S\) 降到 \(3\cdot B\cdot S\)（Q、K、V 各读一遍）。

用伪代码表示单遍版本（仅示意，**不是项目原有代码**）：

```
# 示意代码：单遍在线 FlashAttention（本仓库未实现，用于说明扩展点）
m = -inf; s = 0; o = 0                 # 每行的三个流式状态
for blk in Serial(S // BLOCK_S):
    x  = Q_blk * K_blk                  # 打分
    b  = reduce_max(x)                  # 块内 max
    p  = exp2(x*log2_e - b*log2_e)      # 块内未归一化 softmax
    sp = reduce_sum(p)                  # 块内指数和
    rescale = exp2((m - b)*log2_e)      # 把旧状态换算到新基准 b 下
    o = o * rescale + p @ V_blk         # 重缩放旧输出，并入本块贡献
    s = s * rescale + sp
    m = max(m, b)
o = o / s                               # 最后归一化
```

> 注意：真正的 FlashAttention 里 `p @ V_blk` 是一次矩阵-向量乘（每个输出 token 对应一行注意力），这正是后面 **u4-l2 GEMV** 与 **u4-l3 GEMM** 要解决的「打分」环节。本讲刻意把 `Q*K` 退化为逐元素乘，正是为了在引入矩阵乘之前，先让你吃透「在线归约 + 重缩放」这条主线。

#### 4.3.3 源码精读

本节没有新的源码要逐行读，重点是**对照阅读**，看清「差异点」所在：

- 当前实现的低效点就在这条注释下方的那段第二遍循环：[ans/07-scalar-flash-attn.py:120-132](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L120-L132)。注意它重新 `T.copy` 了 Q、K，并在循环体里重算 `Q_local[i,j]*K_local[i,j]*log2_e`——这正是 4.3.2 列出的「多出来的 \(2\cdot B\cdot S\) 读」的来源。
- 把它与第一遍 [ans/07-scalar-flash-attn.py:101-118](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L101-L118) 对照：第一遍已经算过 `cur_QK` 和 `cur_exp_QK`，却没存下来，第二遍只好重算。单遍版本的改造点，就是在这里多维护一个输出累加器 `O_acc` 并吸收 `*V`。

至于「扩展到完整 FlashAttention」，需要补的是矩阵乘（`Q @ K^T`）、多头、\(\sqrt{d}\) 缩放、因果掩码等。其中矩阵乘这一环，本仓库在 Puzzle 08 用 `T.gemm`（Tensor Core）实现（见 **u4-l3**）。也就是说：**本讲 + Puzzle 08 GEMM = 一个真实可用的 FlashAttention 骨架**。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读 + 伪代码设计」的方式，亲手定位当前实现的低效点，并草拟单遍改造方案（不要求在本仓库跑通，重点是建立直觉）。

**操作步骤**：

1. 打开 [ans/07-scalar-flash-attn.py:122-130](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/ans/07-scalar-flash-attn.py#L122-L130)，找出第二遍里所有「重新加载/重新计算」的语句，统计它们对应多少倍 \(B\cdot S\) 的显存读与计算。
2. 在纸上（或注释里）画出单遍版本需要新增的 fragment：除了 `lse`（或 `m`、`s`），还需要一个 `O_acc`，形状应为 `(BLOCK_B, BLOCK_S)`，用来累加每行对 V 的加权和——为什么是二维而不是 `BLOCK_B`？（因为输出 O 本身是 `(B, S)`，每个 S 位置都要累加。）
3. 写出把 4.3.2 的伪代码「贴回」TileLang 的草图：第一遍循环体末尾多一步 `O_acc[i,j] = O_acc[i,j]*rescale[i] + cur_exp_QK[i,j] * V_local[i,j]`，循环结束后用 `lse` 把 `O_acc` 归一化再写回。注意 `V_local` 需要提前在第一遍就加载。

**需要观察的现象**：你会清楚地看到，当前实现把「算 exp」做了两遍（第一遍算 `cur_exp_QK` 只为喂给 `reduce_sum`，第二遍又算一遍 `exp2(Q*K*log2_e - lse)`）；单遍版本则只算一遍。

**预期结果**：你能用一句话说清「为什么单遍版本少读一遍 Q、K」，并能指出「单遍版本要求第一遍就加载 V，且需要额外的 `O_acc` 寄存器」这一**时间-空间权衡**（省访存、费寄存器）。是否真的更快——**待本地验证**（取决于寄存器压力与是否值得多占寄存器）。

#### 4.3.5 小练习与答案

**练习 1**：当前两遍实现里，「计算 `exp`（`exp2`）」一共执行了几遍？单遍版本能减少到几遍？

**答案**：当前实现里 `exp2` 出现两次——第一遍算 `cur_exp_QK`、第二遍算 `exp2(Q*K*log2_e - lse)`；单遍版本只在第一遍算一次，第二遍整个删掉。这也是单遍版本更省算力的原因之一。

**练习 2**：单遍版本需要在第一遍就加载 `V`，并多维护一个 `O_acc`。这会带来什么代价？

**答案**：第一遍的寄存器占用从「Q、K 两个 tile + 几个行级量」增加到「Q、K、V 三个 tile + O_acc + 行级量」，寄存器压力变大；若 `BLOCK_B*BLOCK_S` 较大可能挤爆寄存器、被迫 spill 或缩小 tile。这是典型的「用寄存器换显存带宽」的权衡。

**练习 3**：把本讲的标量 attention 升级成「真实」的 FlashAttention，最关键缺失的一块计算是什么？本仓库哪一讲会补上？

**答案**：最关键缺失是把逐元素 `Q*K` 换成矩阵乘 `Q @ K^T`（以及对应的 `P @ V`）。这一块由 **u4-l3 Puzzle 08 GEMM（`T.gemm` / Tensor Core）** 提供。把本讲的在线 LSE/重缩放骨架与 Puzzle 08 的 `T.gemm` 组合，就得到了完整 FlashAttention 的雏形。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端任务：

1. **实现并验证**：补全 [puzzles/07-scalar-flash-attn.py](https://github.com/tile-ai/tilelang-puzzles/blob/de37efb54067cad12d6676f15959e0a201ee601d/puzzles/07-scalar-flash-attn.py) 的 `tl_scalar_flash_attn`（参考 4.2.4），运行 `python3 puzzles/07-scalar-flash-attn.py`，确认 `✅ Results match: True`。
2. **看生成代码**：仿照 u2-l2 的做法，在脚本里临时加一行
   ```python
   tl_scalar_flash_attn.compile(B=256, S=16384, BLOCK_B=16, BLOCK_S=128).print_source_code()
   ```
   （运行后**记得删掉**，不要提交到题目文件），观察 `exp2`、`log2`、以及 reduce 对应的 CUDA，确认两个 `T.Serial` 循环确实生成了两段独立的循环。
3. **对比单遍设想**：把 4.3.4 草拟的单遍改造写在一份单独的笔记里（不要改题目源码），列出它相比两遍版本「省了什么、费了什么」。
4. **调参观察**：把 `BLOCK_S` 在 64 / 128 / 256 之间切换（保持 `BLOCK_B=16`），用 `bench_puzzle` 记录耗时，写下你的观察。**待本地验证**：`BLOCK_S` 增大通常减少串行块数（`S//BLOCK_S` 变小）但增大寄存器占用，存在一个甜点。

完成后，你应当能清晰复述：标量 attention = softmax(QK)·V；它的两遍 kernel 与 softmax kernel 同构，只多了「打分」和「乘 V」两步；而把它压成单遍、并把逐元素乘换成矩阵乘，就离真实 FlashAttention 只差一步。

## 6. 本讲小结

- 标量 FlashAttention 把 attention 简化为 \(O=\text{softmax}(Q\odot K)\odot V\)：去掉多头、去掉矩阵乘（用逐元素乘）、去掉 \(\sqrt{d}\) 缩放，只保留「打分 → softmax → 加权 V」三段骨架。
- 它的 kernel 与 Puzzle 06 softmax **逐字同构**：第一遍多算一步 `Q*K`，第二遍多乘一个 `V`；LSE 递推公式 `lse = b + log2(2^(lse-b) + cur_sum)` 一字不改地复用。
- 每行 softmax 互相独立，所以 grid 只有一维 `B // BLOCK_B`；块内 `BLOCK_B` 行并行、沿 S 用 `T.Serial` 串行分块累加 LSE。
- `cur_*` 是每块重算量（`clear=True`），`lse` 是跨块累积量（`T.fill(-inf)` 初始化、循环内不重置）；第二遍因 LSE 已知，纯逐元素、无需归约。
- 当前两遍实现「不高效」：第二遍重读了 Q、K 并重算 `Q*K`/`exp`；单遍 FlashAttention 通过在第一遍同时累加 `O_acc`、维护行级 max/sum 来消除第二遍，代价是更大的寄存器压力。
- 通向完整 FlashAttention 的下一步，是把逐元素 `Q*K` 换成矩阵乘 `Q@K^T`——这正是紧接着的 Puzzle 08 GEMM（`T.gemm` / Tensor Core）要解决的部分。

## 7. 下一步学习建议

- **下一讲 u4-l2（Puzzle 08 GEMV）**：先看「矩阵-向量乘」如何用 `reduce_sum` + 累加器（`accum_dtype=float32`）表达，它是从「逐元素乘」迈向「矩阵乘」的最小一步。
- **随后 u4-l3（Puzzle 08 GEMM Naive）**：学 `T.gemm` 与 Tensor Core，掌握 \(QK^\top\) 这类二维分块矩阵乘；把本讲的 LSE 骨架与 `T.gemm` 组合，你就握住了真实 FlashAttention 的两块核心积木。
- **u4-l4（Puzzle 08 GEMM 优化）**：学 `T.alloc_shared` 共享内存与 `T.Pipelined` 软件流水线，理解如何把「读 Q/K/V」与「算」重叠，进一步逼近 FlashAttention 的访存效率目标。
- **延伸阅读**：FlashAttention 原论文（Dao et al.）的在线 softmax 推导，与本讲 4.3 的单遍伪代码一一对应，建议对照阅读以巩固「分块流式 + 重缩放」的直觉。
