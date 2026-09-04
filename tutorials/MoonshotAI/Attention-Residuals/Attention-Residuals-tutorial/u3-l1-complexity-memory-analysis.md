# 复杂度分析：从 O(Ld) 到 O(Nd) 的内存优化

## 1. 本讲目标

前两个单元我们把 README 里的伪代码变成了可运行的代码，并在最小实验台上看到了 Block AttnRes 的训练收益。本讲换一个视角：**它到底要付出多少显存和计算？为什么 README 说 Full AttnRes 在规模化时遇到 O(Ld) 的内存瓶颈，而分块能降到 O(Nd)？**

学完本讲，你应该能够：

1. 从 `block_attn_res` 的每一行代码出发，亲手推导 Full 与 Block 两种变体的位点显存、训练期累积显存与打分 FLOPs，说清楚 O(Ld) 与 O(Nd) 里每一个符号对应代码里的哪个张量。
2. 理解块数 \( N \) 这个「旋钮」拧动时，显存、计算、精度收益各自如何变化，为什么论文选择约 8 块。
3. 掌握一套规范的显存/耗时基准测试方法：`max_memory_allocated` 峰值测量、CUDA 同步计时、warmup、控制变量，以及用双对数图的斜率验证增长阶。

本讲依旧没有工程源码可读——全部「源码」仍是 [README.md:L52-L91](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L52-L91) 的伪代码，以及 [README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47) 那段只有三句话、却蕴含本讲全部结论的 Block AttnRes 定义。复杂度分析不是背结论，而是把这几行代码在脑子里「跑」成字节数。

## 2. 前置知识

### 2.1 训练显存从哪里来：自动微分要「留底」

训练时的峰值显存 ≈ 参数 + 优化器状态 + **激活值（activations）**。本讲只关心最后一项：

- 前向传播中，凡是**反向传播时还要用到的中间张量**，都会被自动微分引擎留住，直到 `loss.backward()` 结束才释放。
- 两条与本讲直接相关的规则：
  1. `torch.stack(list)` 会把列表里的所有张量**复制**进一块新的连续显存；这份复制出来的堆叠体若参与后续 `einsum`，反向传播就离不开它，于是一直活着。
  2. `einsum` 的反向需要保存它的两个输入。对 `block_attn_res` 而言，这意味着 **V 堆叠体与 softmax 权重都会被留底**。

所以「残差机制占多少显存」这个问题，本质是在问：**为了算反向梯度，网络里一共留了多少份候选表示？**

### 2.2 怎么测：峰值显存与同步计时

- **峰值显存**：`torch.cuda.max_memory_allocated()` 返回自上次 `torch.cuda.reset_peak_memory_stats()` 重置以来，分配器持有过的最大字节数。它比 `nvidia-smi` 干净（不含 CUDA 上下文与碎片）。
- **GPU 计时**：CUDA 内核是异步发射的，`time.perf_counter()` 返回时内核可能还没跑完，必须先 `torch.cuda.synchronize()` 再读表。
- **warmup**：最初几次调用含内核 JIT、内存池冷启动等一次性开销，测稳态前先空跑几轮。

### 2.3 用双对数斜率验证「阶」

如果峰值显存 \( M \) 随深度 \( L \) 按 \( M \approx c \cdot L^{p} \) 增长，那么在 log-log 图上是一条直线，斜率恰为指数 \( p \)。让 \( L \) 逐次翻倍，相邻测值之比趋于 \( 2^{p} \)：比值 ≈ 2 是线性（\( p=1 \)），≈ 4 是二次（\( p=2 \)）。**基准测试验证的是斜率与相对差距，不是绝对字节数**——绝对值里混着框架底座与常数因子。

### 2.4 符号表

| 符号 | 含义 | 出处 |
|:---:|:---|:---|
| L | transformer 层数 | 本讲自定 |
| 2L | 子层位点总数（每层 attn 前、MLP 前各一处） | [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71)、[README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) |
| k | 每块层数 = `block_size // 2` | [README.md:L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74) |
| block_size | 每块子层数（ATTN+MLP 计，每层 2 个） | [README.md:L74](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74) |
| N | 已完成块数（含嵌入块），\( N = L/k \) | [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) |
| s | 子层位点序号，1 到 2L | 本讲自定 |
| c(s) | 位点 s 的注意力候选份数 | 由 V 的形状 `[N+1, B, T, D]`（[README.md:L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61)）读出 |
| B / T / d | batch / 序列长 / 隐藏维 | — |

注意 README 的 O(Ld)、O(Nd) 是「每 token」口径（\( L \) 份 × 每 token 的 \( d \) 维）；换成完整张量要乘上 \( B \cdot T \)，本讲一律写全 \( O(L \cdot B \cdot T \cdot d) \)。

## 3. 本讲源码地图

| 文件 | 位置 | 作用 |
|:---|:---|:---|
| `README.md` | L45–L47 | **本讲第一主锚点**：Full 需 O(Ld) 内存、Block 降到块级、约 8 块恢复大部分收益的三句话定义 |
| `README.md` | L28 | 概览图 (c) 题注：分块把显存从 O(Ld) 降到 O(Nd)——复杂度声明的图示出处 |
| `README.md` | L53–L65 | `block_attn_res`：L61 的 `torch.stack` 是显存开销的直接来源，L62–L64 是 FLOPs 的直接来源 |
| `README.md` | L67–L90 | `forward` 调度：L74–L75 的边界条件决定了候选数按什么节奏增长（u2-l2 已精读，本讲只取结论） |
| `README.md` | L41 | 总公式 \( \mathbf{h}_l = \sum_{i=0}^{l-1} \alpha_{i \to l} \mathbf{v}_i \)：Full 变体候选数 = \( l \) 的公式依据 |
| `README.md` | L97–L99 | Scaling law 结论（1.25 倍计算等效）：收益侧背景，下一讲 u3-l2 的主角 |
| [assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) | — | (b)/(c) 两个子图直观对比 Full 的全历史候选与 Block 的块级候选 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**复杂度分析**（纸上推导）、**显存基准测试**（把推导变成实测数字）、**块数 N 取舍**（把开销与收益放到同一张权衡图上）。

### 4.1 复杂度分析：候选集大小决定一切

#### 4.1.1 概念说明

回到 u1-l3 建立的直觉：AttnRes 把标准残差的全 1 累加换成对「历史表示候选集」的 softmax 加权聚合。**候选集有多大，机制就要保存多少份 \( [B, T, d] \) 张量**——这就是复杂度的全部来源：

- **Full AttnRes**：第 \( l \) 层要「看见」之前**所有**子层的输出 \( \mathbf{v}_0, \dots, \mathbf{v}_{l-1} \)（\( \mathbf{v}_0 \) 是词嵌入，见 [README.md:L41](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41) 的求和上标）。深度越深候选越多，残差状态随 \( L \) 线性膨胀——这就是 [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47) 说的 "requires O(Ld) memory at scale"。
- **Block AttnRes**：块内用标准残差把 \( k \) 层压缩成一份部分和，注意力只在 **N 份块表示 + 1 份当前部分和** 上打分。候选数封顶在 \( N+1 \)，与深度无关——这就是 [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28) 说的 "reducing memory from O(Ld) to O(Nd)"。

一句话：**标准残差把历史「压缩」进一个累加器（状态 \( O(d) \)；Full AttnRes 把历史「完整陈列」出来（状态 \( O(Ld) \）；Block AttnRes 用块折中（状态 \( O(Nd) \)）**。

#### 4.1.2 核心流程

把网络看成 \( 2L \) 个顺序执行的**子层位点**（每层 attn 前、MLP 前各一，对应 [README.md:L71](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L71) 与 [README.md:L84](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L84) 的两次调用）。位点 \( s \) 的候选数记为 \( c(s) \)：

\[ c_{\text{full}}(s) = s \qquad\qquad c_{\text{block}}(s) = \lfloor (s-1)/(2k) \rfloor \; \text{附近，随块阶梯式 +1，最大值 } N+1 \]

（块内候选数的精确阶梯推导见 4.1.4 的计数器函数；不影响阶。）

由此推出三个量：

**① 位点显存（残差状态本身，README 的 O(Ld)/O(Nd) 口径）**——一次 `torch.stack` 的大小：

\[ M_{\text{site}} = c(s) \cdot B \cdot T \cdot d \;\;\Rightarrow\;\; \text{Full}: O(LBTD), \quad \text{Block}: O(NBTD) \]

**② 打分 FLOPs（每个位点）**——两次 einsum 各扫一遍候选（u2-l3 已得每位点约 \( 6(N{+}1)BTd \)，Full 即 \( 6s \cdot BTd \)）：

\[ \text{FLOPs}_{\text{site}} \propto c(s) \cdot B \cdot T \cdot d \]

**③ 训练期累积显存（峰值实测的主体）**——每个位点的 V 堆叠体都要为反向留底，全部同时存活到 `backward()`：

\[ M_{\text{train}}^{\text{full}} = \sum_{s=1}^{2L} s \cdot BTD = (2L)(2L+1)/2 \cdot BTD = O(L^2 BTD) \]

\[ M_{\text{train}}^{\text{block}} = \sum_{s=1}^{2L} c_{\text{block}}(s) \cdot BTD \approx \frac{L \cdot N}{2} \cdot BTD = O(LNBTD) \]

三个结论浓缩成一张表：

| 量 | Standard | Full AttnRes | Block AttnRes |
|:---|:---:|:---:|:---:|
| 残差状态（每 token） | \( O(d) \) | \( O(Ld) \) | \( O(Nd) \) |
| 训练累积候选显存 | 0（无堆叠） | \( O(L^2 BTD) \) | \( O(LNBTD) \) |
| 打分总 FLOPs | 0 | \( O(L^2 BTd) \) | \( O(LNBTd) \) |
| 额外参数（u2-l3） | 0 | 每层 \( 4d \) | 每层 \( 4d \)，**与 N 无关** |

两个值得单独点出的推论：

- **Full 的二次项**：不只是「保留 L 份」，训练时每个位点的堆叠复制都要留底，总量是 \( \sum s = O(L^2) \)；即使推理时只看状态 \( O(LBTD) \)，训练时的累积才是真正的痛点。
- **Block 的线性项斜率由 N 定**：\( O(LNBTD) \) 对 L 是线性的，N 固定时（比如 8）就是「斜率很小的直线」——深度翻倍，机制开销只翻倍；而 Full 要翻四倍。

#### 4.1.3 源码精读

**开销的第一来源：L61 的堆叠。**

> [README.md:L61-L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61-L61)：把 N 份块表示与当前部分和堆叠成候选张量 V，形状注释 `[N+1, B, T, D]`。

```python
V = torch.stack(blocks + [partial_block])  # [N+1, B, T, D]
```

这一行分配 \( (N{+}1) \cdot B \cdot T \cdot D \) 个元素的连续显存并**复制**进来。它的形状注释就是 O(Nd) 的代码化身：候选份数出现在张量第一维，Full 变体里这个第一维是 \( s \)（随深度增长），Block 里封顶 \( N{+}1 \)。

**开销的第二来源：L62 的归一化再复制一份。**

> [README.md:L62-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62-L64)：打分用归一化后的 K，聚合用原始 V，两次 einsum 完成跨深度注意力。

```python
K = norm(V)                                                        # 又一份同尺寸张量
logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
h = torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)
```

`K = norm(V)` 让候选显存常数翻倍（V、K 各一份）；两次 einsum 的计算量都与第一维（候选数）成正比——FLOPs 公式里 \( c(s) \) 的来源。反向传播需要 V 与 softmax 权重，所以它们都活到 `backward()`，这就是 4.1.2 中 ③ 的机制依据。

**复杂度声明的原文出处。**

> [README.md:L45-L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L45-L47)：Block AttnRes 的定义段——Full 直接但规模化时需 O(Ld) 内存；Block 把层划分成 N 块、块内标准残差、只对块级表示做注意力；约 8 块恢复 Full 的大部分收益，是开销边际的 drop-in 替换。

> [README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28)：概览图 (c) 的题注，O(Ld) → O(Nd) 的图示出处；[assets/overview.png](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/assets/overview.png) 的 (b) 子图里每个子层都要拖着自己的输出供后人查询，(c) 子图里只有块边界产出候选。

**块数 N 如何被代码控制。**

> [README.md:L74-L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L75)：`block_size` 以 ATTN+MLP 子层计数（每层 2 个），边界条件 `layer_number % (block_size // 2) == 0`。

由 u2-l2 的结论：\( k = \text{block\_size}/2 \) 是每块层数，\( N = L/k \)。**N 不是独立的超参，而是由 `block_size` 与 L 共同决定**——这是 4.3 节取舍分析的代码接口。

#### 4.1.4 代码实践：候选计数器与预测表

**实践目标**：在跑任何真实基准之前，先用一个纯算术的计数器把「理论候选元素总数」算出来，写下对斜率的预言——基准实验只是来验证这张表的。

**操作步骤**：

1. 新建 `theory_counts.py`（示例代码），粘贴运行：

```python
# 示例代码：每个位点的注意力候选数计数器
def site_candidates(L, mode, n_blocks=None):
    """返回 2L 个位点的候选数列表。理论候选元素总数 = sum(counts) * B * T * D。"""
    if mode == 'standard':
        return [0] * (2 * L)                      # 标准残差不产生候选堆叠
    if mode == 'full':
        return list(range(1, 2 * L + 1))          # 位点 s 的候选 = 嵌入 + 前 s-1 个子层输出
    k, counts, sealed = L // n_blocks, [], 0      # k = 每块层数
    for l in range(L):
        counts.append(sealed + 1)                 # attn 前位点：先算 h……
        if l % k == 0:
            sealed += 1                           # ……再封存（README 的语句顺序）
        counts.append(sealed + 1)                 # mlp 前位点：含新封存的块
    return counts

for L in [4, 8, 16, 32]:
    f = sum(site_candidates(L, 'full'))
    b = sum(site_candidates(L, 'block', 4))
    print(f"L={L:>2}  full Σc={f:>5}  block(N=4) Σc={b:>4}  比值={f/b:.1f}")
```

2. 对着输出填写预测表，并写下两条斜率预言。

**需要观察的现象**：full 的 Σc 每逢 L 翻倍大约乘 4（二次），block(N 固定) 大约乘 2（线性）；两者的比值随 L 拉大。

**预期结果**（纯算术，可直接验证）：

| L | full Σc | block(N=4) Σc | full : block |
|:---:|:---:|:---:|:---:|
| 4 | 36 | 24 | 1.5 |
| 8 | 136 | 51 | 2.7 |
| 16 | 528 | 108 | 4.9 |
| 32 | 2080 | 220 | 9.5 |

预言：基准实验（4.2）里 full 的峰值显存随 L 的 log-log 斜率应接近 2，block(N 固定) 接近 1；比值 9.5 意味着 L=32 时 Full 的机制开销约是 Block(N=4) 的 9 倍以上，且差距随深度继续拉大——这正是 README "at scale" 一词的分量。

#### 4.1.5 小练习与答案

**练习 1**：一个 48 层、\( d = 4096 \) 的模型，\( B=8 \)、\( T=2048 \)、fp16（2 字节）。Full AttnRes 在**最后一个位点**的 V 堆叠需要多少显存？Block(N=8) 呢？

**答案**：最后位点 \( s = 2L = 96 \)，full 堆叠 = \( 96 \times 8 \times 2048 \times 4096 \approx 6.44 \times 10^9 \) 个元素，fp16 约 **12 GiB**——还只是 96 份留底中的最后一份。Block(N=8) 的堆叠是 9 份，约 \( 9/96 \approx 9.4\% \)，即 **约 1.1 GiB**。深度越大，这个比值越悬殊。

**练习 2**：标准残差流为什么不需要 O(Ld) 的状态？

**答案**：标准残差把每个子层输出**即时加进**同一个累加器 \( \mathbf{h} \)，历史被压缩进部分和，当前状态永远只是一份 \( [B,T,d] \) 张量（机制口径 \( O(d) \)）。Full AttnRes 为了让后续层能「查询」任意历史输出，必须把它们完整陈列，状态变成 \( O(Ld) \)。代价换来的是选择性——这正是 u1-l3 讲的权衡。

**练习 3**：为什么说 Full 的打分总 FLOPs 是 \( O(L^2 BTd) \) 而不是 \( O(LBTd) \)？

**答案**：位点 \( s \) 的两次 einsum 都要扫过 \( s \) 份候选，单点成本 \( \propto s \)；全网络求和 \( \sum_{s=1}^{2L} s = (2L)(2L+1)/2 = O(L^2) \)。只有当候选数被封顶（Block 的 \( N{+}1 \)）时，求和才回落为 \( O(LN) \)、对 L 线性。

### 4.2 显存基准测试：把 O(·) 变成实测数字

#### 4.2.1 概念说明

纸上推导可能漏因子、可能高估或低估框架行为，工程师的答案是**测**。但测得可信需要三件事：

1. **隔离变量**：注意力子层和 MLP 本身的激活也是 \( O(LBTD) \)，会淹没残差机制的信号。本讲的基准把子层换成**占位模块**（`nn.Identity` 语义，仅保留 norm），让「候选堆叠」成为唯一随模式变化的显存来源。要测真实模型时，把占位换回 u2-l4 实验台的子层即可，对比双方必须同构。
2. **测峰值而非瞬时**：训练显存的瓶颈是整段时间里的最大值，用 `reset_peak_memory_stats` + `max_memory_allocated`。
3. **同步与预热**：计时前 synchronize、先 warmup，否则测到的是内核队列深度和 JIT，不是模型。

#### 4.2.2 核心流程

```text
对每个配置 (mode, L, N)：
    构造 TinyModel（占位子层 + 对应残差机制）        ─┐
    warmup 3 轮 fwd+bwd（内核 JIT / 内存池热身）       │ 控制变量：
    reset_peak_memory_stats + synchronize             │ 同 B,T,d、同种子、
    计时 10 轮 fwd+bwd，取每轮平均                     │ 同 ITERS、同损失形式
    读 max_memory_allocated → 峰值 MiB                │
    del 模型 + empty_cache（防跨配置碎片）            ─┘
输出：表格 + （可选）log-log 拟合斜率
```

#### 4.2.3 源码精读

基准脚本的三个层类与 README 伪代码一一对应，差异只有「子层换成占位」：

> [README.md:L67-L90](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L67-L90)：`forward` 的完整调度——`BlockLayer` 逐行保留 L68（`partial_block = hidden_states`）、L71 与 L84（两处 attn_res 位点）、L75–L77（边界判断与封存）、L81（`None` 哨兵起算）、L88（MLP 后累加），只把 L80 的 `self.attn(self.attn_norm(h))` 与 L87 的 `self.mlp(self.mlp_norm(h))` 换成 `self.n(h)`（占位）。

> [README.md:L53-L65](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L53-L65)：`attn_res` 函数原样照搬，不改一字——被测对象必须是 README 的机制本身，而不是我们的再实现。

> [README.md:L41](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L41)：Full 变体没有现成伪代码，`FullLayer` 维护一个 `outs` 列表（初始为 `[x]`），每个位点对**当前列表全部元素**做 attn_res，再把子层输出 append 进去——这正是求和公式的直接实现，也是 u1-l3 实践中 `full_attn_res` 的层化版本。

#### 4.2.4 代码实践：完整基准脚本

**实践目标**：实测 Standard / Full / Block 三种残差机制的峰值显存与前后向耗时，验证 4.1 的斜率预言。

**操作步骤**：

1. 保存以下脚本为 `bench_attnres.py`（示例代码，放在你自己的工作目录，不改动仓库）：

```python
# 示例代码：Full / Block / Standard 残差机制的显存与耗时基准
# 用法：python bench_attnres.py   （有 GPU 测显存+耗时；仅 CPU 时跳过显存、保留耗时与理论计数）
import time
import torch
import torch.nn as nn

B, T, D = 2, 32, 256           # 刻意取小：聚焦残差机制本身的候选堆叠开销
DEPTHS = [4, 8, 16, 32]        # 实验 1：显存/耗时 - 深度（block 固定 N=4）
NS = [2, 4, 8, 16, 32]         # 实验 2：耗时/显存 - 块数（固定 L=32）
ITERS = 10
DEV = 'cuda' if torch.cuda.is_available() else 'cpu'

class RMSNorm(nn.Module):
    def __init__(self, d, eps=1e-6):
        super().__init__()
        self.weight, self.eps = nn.Parameter(torch.ones(d)), eps
    def forward(self, x):
        return x * x.pow(2).mean(-1, keepdim=True).add(self.eps).rsqrt() * self.weight

def attn_res(blocks, partial_block, proj, norm):
    """对应 README.md:L53-L65 的 block_attn_res，原样照搬。"""
    V = torch.stack(blocks + [partial_block])                       # [N+1, B, T, D]
    K = norm(V)
    logits = torch.einsum('d, n b t d -> n b t', proj.weight.squeeze(), K)
    return torch.einsum('n b t, n b t d -> b t d', logits.softmax(0), V)

class StdLayer(nn.Module):       # 标准残差基线：一条流；子层为占位（只剩 norm）
    def __init__(self, d):
        super().__init__()
        self.n1, self.n2 = RMSNorm(d), RMSNorm(d)
    def forward(self, h):
        h = h + self.n1(h)
        return h + self.n2(h)

class FullLayer(nn.Module):      # Full：候选 = 全部历史输出（README.md:L41 公式）
    def __init__(self, d):
        super().__init__()
        self.p1, self.g1 = nn.Linear(d, 1), RMSNorm(d)
        self.p2, self.g2 = nn.Linear(d, 1), RMSNorm(d)
        self.n1, self.n2 = RMSNorm(d), RMSNorm(d)
    def forward(self, outs):
        h = attn_res(outs[:-1], outs[-1], self.p1, self.g1)
        outs.append(self.n1(h))                  # 占位子层输出成为新候选
        h = attn_res(outs[:-1], outs[-1], self.p2, self.g2)
        outs.append(self.n2(h))
        return outs

class BlockLayer(nn.Module):     # README.md:L67-L90 逐行对应（子层换成占位）
    def __init__(self, d, layer_number, k):      # k = 每块层数 = block_size // 2
        super().__init__()
        self.p1, self.g1 = nn.Linear(d, 1), RMSNorm(d)
        self.p2, self.g2 = nn.Linear(d, 1), RMSNorm(d)
        self.n1, self.n2 = RMSNorm(d), RMSNorm(d)
        self.layer_number, self.k = layer_number, k
    def forward(self, blocks, hidden_states):
        partial_block = hidden_states                                   # README:L68
        h = attn_res(blocks, partial_block, self.p1, self.g1)           # 位点 1
        if self.layer_number % self.k == 0:                             # README:L75
            blocks.append(partial_block)
            partial_block = None
        attn_out = self.n1(h)                                           # 占位子层
        partial_block = partial_block + attn_out if partial_block is not None else attn_out
        h = attn_res(blocks, partial_block, self.p2, self.g2)           # 位点 2
        return blocks, partial_block + self.n2(h)

class TinyModel(nn.Module):
    def __init__(self, d, L, mode, n_blocks=None):
        super().__init__()
        self.mode = mode
        if mode == 'standard':
            self.layers = nn.ModuleList(StdLayer(d) for _ in range(L))
        elif mode == 'full':
            self.layers = nn.ModuleList(FullLayer(d) for _ in range(L))
        else:
            assert n_blocks <= L and L % n_blocks == 0
            self.layers = nn.ModuleList(BlockLayer(d, i, L // n_blocks) for i in range(L))
        self.final_norm = RMSNorm(d)
    def forward(self, x):
        if self.mode == 'standard':
            h = x
            for ly in self.layers: h = ly(h)
            return self.final_norm(h)
        if self.mode == 'full':
            outs = [x]
            for ly in self.layers: outs = ly(outs)
            return self.final_norm(outs[-1])
        blocks, partial = [], x          # 层 0 的边界会把嵌入封存为 blocks[0]（u2-l2）
        for ly in self.layers: blocks, partial = ly(blocks, partial)
        return self.final_norm(partial)

def bench(mode, L, n_blocks=None):
    torch.manual_seed(0)
    model, x = TinyModel(D, L, mode, n_blocks).to(DEV), torch.randn(B, T, D, device=DEV)
    def step():
        model(x).pow(2).mean().backward()
        model.zero_grad(set_to_none=True)
    for _ in range(3): step()                                   # warmup
    if DEV == 'cuda':
        torch.cuda.synchronize(); torch.cuda.reset_peak_memory_stats()
    t0 = time.perf_counter()
    for _ in range(ITERS): step()
    if DEV == 'cuda': torch.cuda.synchronize()
    dt = (time.perf_counter() - t0) / ITERS * 1e3
    peak = torch.cuda.max_memory_allocated() / 2**20 if DEV == 'cuda' else float('nan')
    cand = sum(site_candidates(L, mode, n_blocks))              # 理论候选元素数（×B·T·D 前）
    del model, x
    if DEV == 'cuda': torch.cuda.empty_cache()
    return peak, dt, cand

def site_candidates(L, mode, n_blocks=None):                    # 与 4.1.4 相同的计数器
    if mode == 'standard': return [0] * (2 * L)
    if mode == 'full':     return list(range(1, 2 * L + 1))
    k, counts, sealed = L // n_blocks, [], 0
    for l in range(L):
        counts.append(sealed + 1)
        if l % k == 0: sealed += 1
        counts.append(sealed + 1)
    return counts

print(f'{"mode":<9}{"L":>3}{"N":>4}{"peak(MiB)":>11}{"ms/iter":>9}{"Σcand":>7}')
for L in DEPTHS:
    for mode, nb in [('standard', None), ('full', None), ('block', 4)]:
        p, t, c = bench(mode, L, nb)
        print(f'{mode:<9}{L:>3}{str(nb or "-"):>4}{p:>11.1f}{t:>9.2f}{c:>7}')
print()
for nb in NS:
    p, t, c = bench('block', 32, nb)
    print(f'{"block":<9}{32:>3}{nb:>4}{p:>11.1f}{t:>9.2f}{c:>7}')
```

2. 运行 `python bench_attnres.py`，把两张表的结果抄录下来。
3. （可选）用 numpy 拟合斜率：

```python
# 示例代码：双对数斜率拟合（把 [...] 换成你实测的峰值）
import numpy as np
Ls   = np.array([4, 8, 16, 32])
peak_full    = np.array([...])
peak_block4  = np.array([...])
print('full  斜率:', np.polyfit(np.log2(Ls), np.log2(peak_full),   1)[0])   # 预言 ≈ 2
print('block 斜率:', np.polyfit(np.log2(Ls), np.log2(peak_block4), 1)[0])   # 预言 ≈ 1
```

**需要观察的现象**：

1. 实验 1 中，full 的峰值随 L 翻倍约乘 4（L 较小时因公共底座存在会偏低，L 越大越接近 4）；block(N=4) 约乘 2；standard 最平。
2. full 与 block 的峰值比值随 L 拉大（L=32 时应为好几倍，方向与 4.1.4 预测表的 9.5 一致）。
3. 实验 2 中，耗时随 N 近似线性增长；N=32（= L，`block_size=2`，每层一块）的耗时与峰值都逼近 full——因为此时块粒度已细到逐层。

**预期结果**：三条 log-log 斜率分别接近 1（standard，底座）、2（full）、1（block，斜率小、截距随 N 抬升）。**具体数值待本地验证**——绝对 MiB 取决于 PyTorch 版本、分配器行为与 GPU 型号，但斜率与相对关系是机制决定的，应稳定复现。

#### 4.2.5 小练习与答案

**练习 1**：为什么计时循环结束后必须 `torch.cuda.synchronize()` 才能读 `perf_counter`？

**答案**：CUDA 内核异步执行，Python 端发起后立即返回。不同步就读表，测到的是「内核发射完成」而非「内核执行完成」，多轮计时会被队列深度掩盖，结果系统性偏小。

**练习 2**：为什么基准要用占位子层而不是真实的注意力/MLP？

**答案**：控制变量。真实子层自身的激活也是 \( O(LBTD) \)，与 Block 的机制开销同阶，会把信号淹没在底座里；换成占位后，三种模式之间唯一的差异就是残差机制的候选堆叠。代价是绝对数值不能外推到真实模型——真实模型里机制开销要叠加在更大的底座上，但**阶与斜率**结论不变。

**练习 3**：脚本里每个配置都 `torch.manual_seed(0)` 后重建模型、配置之间 `del + empty_cache`，为什么？

**答案**：同种子保证同规模参数的初始化一致，减少无关波动；`del` 与 `empty_cache` 防止上一个配置的显存残留与碎片影响下一个配置的峰值读数（`max_memory_allocated` 是累计峰值语义，必须每配置重置并从干净分配器状态起步）。

### 4.3 块数 N 取舍：精度收益与开销的平衡点

#### 4.3.1 概念说明

N 是 Block AttnRes 最核心的工程旋钮，它同时拨动三根指针：

| 指针 | 随 N 的变化 | 公式依据 |
|:---|:---|:---|
| 位点显存（状态） | 线性 ↑：\( (N{+}1)BTD \) | 4.1.2 ① |
| 训练累积显存 / 打分 FLOPs / 耗时 | 线性 ↑：\( O(LNBTD) \)、\( O(LNBTd) \) | 4.1.2 ②③ |
| 额外参数量 | **不变**：每层 \( 4d \)，与 N 无关 | u2-l3 的结论 |
| 精度收益 | 凹型饱和：候选粒度变细、块内稀释变轻，但边际收益递减 | [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)：约 8 块恢复 Full 的大部分收益 |

直觉上，N 是在「标准残差」与「Full AttnRes」之间滑动：

- **N = 1**（`block_size = 2L`）：全场只有嵌入块 + 当前部分和两个候选，块内就是一条长长的标准残差流，注意力只在「嵌入 vs 当前累加」之间选择——开销最小，选择性最弱。
- **N = L**（`block_size = 2`）：每层封存一块，候选粒度细到逐层，行为与开销都逼近 Full（4.2 实验 2 的 N=32 一行可直接验证）。
- **论文选择约 8**：开销曲线是线性上升的直线，收益曲线是先陡后平的凹曲线，两者的交点附近就是性价比拐点——README 用 "recovers most of Full AttnRes's gains with marginal overhead" 一句话概括。

注意方向性：**显存开销与 N 的关系在「固定 L」下讨论**（N 由 `block_size` 决定，见 4.1.3）；而「Block vs Full 的节省比」在「固定 N、增大 L」下讨论（4.1.4 的比值列随 L 增大）。两个视角不要混。

#### 4.3.2 核心流程

给一个新模型选 N 的决策流程：

```text
输入：层数 L、显存预算、算力预算
1. 先按开销上限反解：由 4.1.2 的 O(LN·BTD) 与每位点 6(N+1)BTd FLOPs，
   算出 N 的最大允许值 N_max
2. 取 N = min(8, N_max) 起步          ← 论文的经验甜点（README.md:L47）
3. 换算 block_size = 2 * (L / N)，要求整除（k = L/N 为整数）
4. 小规模短训对比 N 与 N/2 的验证损失（多种子），若差距小于种子方差，
   取更小的 N（省开销）；若显著，考虑加大
5. 上线前跑一次 4.2 的基准确认峰值显存与吞吐在预算内
```

定性权衡图（横轴 N，纵轴归一化）：

```text
收益(相对Full) │      ·················●●●● Full 水平
              │    ●●
              │  ●●          ← 约 N=8 处已接近饱和（论文结论）
              │●
开销(显存/FLOPs)│●●●●●●●●●●●●●●●●●●●●  ← 随 N 线性上升
              └─────────────────────────→ N (对数轴)
               1    2 4  8 16 ... L
```

#### 4.3.3 源码精读

> [README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)："With ~8 blocks, it recovers most of Full AttnRes's gains while serving as a practical drop-in replacement with marginal overhead."——本讲的定量分析最终都汇到这一句：8 这个数字不是理论上推出的闭式解，而是论文实验测得的性价比拐点（具体实验设置见论文，仓库未提供，待确认）。

> [README.md:L74-L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L75)：`block_size` 以子层计数、边界按 `block_size // 2` 取模——代码里没有 "N" 这个变量，N 完全由 `block_size = 2L/N` 间接设定，移植时最容易踩的坑就是忘了「除以 2」。

> [README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99)：收益侧的总账——Block AttnRes 匹配 1.25 倍计算量的基线。把这条与开销侧「每层仅 4d 参数、O(Nd) 状态」放在一起看，才是完整的取舍：花不到 3% 量级的参数与边际显存，换来等效 25% 的算力（下一讲 u3-l2 展开）。

#### 4.3.4 代码实践：N 扫描与开销-收益对照

**实践目标**：用 4.2 的基准定量测出「N 每翻一倍，开销涨多少」，并在小规模上感受「收益饱和」为何在小实验台上难以直接观测。

**操作步骤**：

1. 运行 `bench_attnres.py` 的实验 2（固定 L=32，N ∈ {2,4,8,16,32}），记录每档的 `ms/iter` 与 `peak(MiB)`。
2. 画「耗时-N」图（N 用对数轴），检验线性：相邻档位的耗时增量应大致相等。
3. （可选，接 u2-l4 实验台）在字符级语言模型上对 N ∈ {2,4,8} 各短训 3 个种子，记录验证损失的均值 ± 标准差。

**需要观察的现象**：

1. 耗时与 Σcand 随 N 近似等比例增长；N=32（=L）的峰值与耗时逼近同深度的 full 行。
2. （可选部分）三档 N 的验证损失差距大概率**小于种子间方差**——这是预期内的，不是失败。

**预期结果**：开销侧结论清晰可复现；收益侧的「约 8 块饱和」来自论文的大规模实验（README 未附小规模数据），在小实验台上通常分不出 2/4/8 块的差异。**具体数值待本地验证**。这个「小规模看不见收益、只看得见开销」的不对称，正是 u2-l4 强调的「小实验不能证实或证伪 48B 规模结论」在开销维度的镜像。

#### 4.3.5 小练习与答案

**练习 1**：64 层的模型想用约 8 块，`block_size` 应设多少？

**答案**：\( k = L/N = 64/8 = 8 \) 层每块，`block_size` 以 ATTN+MLP 子层计数 = \( 2k = 16 \)。若误设 8，实际是 16 块，显存与 FLOPs 开销直接翻倍。

**练习 2**：N 增大时，哪些量线性增大、哪些不变、哪个凹型饱和？

**答案**：线性增大——位点状态 \( (N{+}1)BTD \)、训练累积 \( O(LNBTD) \)、打分 FLOPs \( O(LNBTd) \)、耗时；不变——额外参数量（每层 \( 4d \)，u2-l3）；凹型饱和——精度收益（约 8 块恢复大部分，README.md:L47）。

**练习 3**：`block_size=2`（即 N=L）时 Block AttnRes 退化成什么？显存复杂度回到多少？

**答案**：每层都是块边界、每层封存一份「层内部分和」，候选粒度细到逐层，机制行为与开销都逼近 Full（子层粒度上略有差别：块内 attn 位点与 mlp 位点共享同一层封存节奏）。显存回到 \( O(LBTD) \) 量级——4.2 实验 2 的 N=32 行可直接看到这一点。这说明 N 是连续旋钮，两端分别渐近「标准残差 + 读出」与「Full AttnRes」。

## 5. 综合实践

把本讲三个模块串成一个完整的「预测 → 实测 → 结论」闭环：

1. **预测**：运行 4.1.4 的 `site_candidates` 计数器，抄下 4.1.4 的预测表；用 4.1.2 的公式手算练习 1 的 48 层场景（full 最后位点 ≈ 12 GiB vs block(N=8) ≈ 1.1 GiB），写下三条斜率预言（full≈2、block≈1、standard≈1）。
2. **实测**：跑 `bench_attnres.py` 两个实验，得到「显存-深度」与「耗时-块数」两张表（有 matplotlib 的话各画一张图，前者用 log-log 轴）。
3. **拟合**：用 4.2.4 的 `polyfit` 片段拟合三条 log-log 斜率，与预言对照。
4. **结论报告**（一小节 Markdown，建议存进你自己的实验笔记）需回答：
   - full 与 block 的实测斜率分别是多少？与 2 和 1 差多远？偏差来自哪里（公共底座、V/K 双份常数、分配器粒度）？
   - L=32 时 full/block(N=4) 的实测峰值比是多少，与预测表的 9.5 相比呢？
   - 耗时随 N 的增长是否线性？N=32 是否逼近 full？
   - 若你要训一个 L=64、d=1024 的模型，`block_size` 选多少、预期机制开销占比多少？
5. **反思题**（写进报告结尾）：实测峰值里哪些部分是 4.1.2 公式**没有**覆盖的？这提示复杂度分析的「阶」与「常数」分别在什么场合重要。

预期整体结论：Block 用一个与深度无关的候选上限（N+1）把残差机制从二次累积压回线性，代价随 N 线性、参数不增——这就是 README 敢称其 "marginal overhead" 的定量基础。所有具体数字**待本地验证**。

## 6. 本讲小结

- 残差机制的显存与计算全部来自**候选集大小** \( c(s) \)：`torch.stack` 复制候选（[README.md:L61](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L61)），einsum 逐候选打分（[README.md:L62-L64](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L62-L64)）。
- Full 的候选随深度线性增长（\( c=s \)）：残差状态 \( O(LBTD) \)、训练累积 \( O(L^2BTD) \)、打分 \( O(L^2BTd) \)；Block 把候选封顶在 \( N{+}1 \)：状态 \( O(NBTD) \)、累积与打分 \( O(LN\cdot) \)，深度解耦（[README.md:L28](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L28)、[README.md:L47](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L47)）。
- 基准方法三件套：占位子层隔离变量、`max_memory_allocated` 测峰值、synchronize + warmup 后计时；用 log-log 斜率验证「阶」而非绝对值。
- N 是唯一同时拨动显存/FLOPs（线性↑）与候选粒度（收益凹饱和）的旋钮，参数量不随 N 变化；论文甜点约 8 块，代码里通过 `block_size = 2L/N` 间接设定（[README.md:L74-L75](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L74-L75)）。
- 小实验台能清晰复现**开销侧**结论（斜率、比值、线性），但通常分辨不出 N 的**收益侧**差异——两种不对称都要诚实对待。

## 7. 下一步学习建议

- **下一讲 u3-l2（Scaling Law 实验解读）**：本讲算的是「花多少」，下一讲算「赚多少」——Block AttnRes 如何在所有计算预算下超越基线、匹配 1.25 倍计算的曲线（[README.md:L97-L99](https://github.com/MoonshotAI/Attention-Residuals/blob/85e22310fe5ee860b4a023de312d791de8a5a5e6/README.md#L97-L99)），并学习损失-计算量幂律的拟合方法（与本讲的 log-log 斜率法同源）。
- **u3-l5（移植到你自己的模型）**：把本讲的 `block_size = 2L/N` 旋钮与上线前基准检查清单带进真实模型改造。
- **回到论文**：打开仓库根目录的 `Attention_Residuals.pdf`，找 Block 数量 N 的消融实验与「约 8 块」结论的原始图表（仓库 README 只有一句话，细节以论文为准，待确认），对照本讲 4.3 的定性权衡图。
