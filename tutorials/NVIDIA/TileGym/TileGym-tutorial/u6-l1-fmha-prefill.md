# Flash 多头注意力 FMHA（prefill）

## 1. 本讲目标

本讲是「注意力内核族」的第一讲，以 cuTile 后端的 Flash 多头注意力（Fused Multi-Head Attention, FMHA）prefill 内核为样本，讲清这一族内核共同的两大思想基石：**分块计算**与**在线 softmax（online softmax）**。学完后你应当能够：

- 说清楚「为什么不能直接算一次完整的 QKᵀ」——理解 \(O(N^2)\) 注意力矩阵的显存代价，以及 Flash Attention 如何用分块把显存降到 \(O(N)\)。
- 读懂 cuTile 版 FMHA 内核 `fmha_kernel_impl` 的每一步，尤其是**分块 QKᵀ**、**在线 softmax 的 m/l 在线更新**、**causal 掩码与 scaling**三段逻辑。
- 理解 `fmha_interface` 这个高层封装如何把一次调用转发到统一分发的 `tilegym.ops.fmha`，最终路由到 `tile_fmha`。

本讲不展开自动调优（已在 u5-l3 讲过 `exhaustive_search` 与 tune-once/cache/launch 模式），也不讲解码（u6-l2）、MLA（u6-l3）与各类变体（u6-l4）。

## 2. 前置知识

本讲建立在前面几讲之上，开始前请确认你已经理解：

- **cuTile 内核骨架与数据搬运原语（u3-l1、u3-l2）**：`@ct.kernel`、`ConstInt`、`ct.load/ct.store` 的 `index+shape` 语义、`ct.arange`、`ct.astype`。本讲会大量出现 `ct.load` 的 `index=(...)` + `shape=(...)`、`order=` 转置、`padding_mode`、`latency=` 提示。
- **分块矩阵乘 matmul（u5-l1）**：输出瓦片、累加器（accumulator）、`ct.mma` 张量核心乘加、K 方向 tile 循环。FMHA 内核里的 `QKᵀ` 就是一次分块 GEMM。
- **统一接口与分发（u2-l1、u2-l2）**：`ops.py` 里带 `@dispatch` 的 stub 默认抛 `NotImplementedError`，真正实现由后端用 `@register_impl("算子名", backend=...)` 注册进全局 `_REGISTRY`，运行时按当前后端查表路由。

如果你对注意力计算本身还不熟，下面用一个最小回顾铺垫。

### 2.1 什么是注意力（最小回顾）

缩放点积注意力（scaled dot-product attention）对一个「头」的计算是：

\[ S = QK^{T} \in \mathbb{R}^{S \times S},\qquad P = \mathrm{softmax}(S / \sqrt{d}),\qquad O = PV \in \mathbb{R}^{S \times d} \]

其中 \(S\) 是序列长度、\(d\) 是每个头的维度（head_dim）、\(Q/K/V\in\mathbb{R}^{S\times d}\)。难点在 \(S\) 矩阵：它随序列长度的平方增长。当 \(S=31072\)（Llama 的长序列）时，单头单 batch 的 \(S\) 矩阵就有近 10 亿个元素，物化它意味着既要写一遍显存又要读一遍，且占满显存——这就是「注意力显存墙」。

**因果掩码（causal mask）** 是自回归语言模型的关键约束：第 \(i\) 个 query 只能看第 \(0..i\) 个 key，即要求 \(i \geq j\)（query 下标 ≥ key 下标），矩阵上三角被屏蔽。

### 2.2 Flash Attention 的核心直觉

Flash Attention 的破解之法是：**永远不在显存里物化完整的 \(S\) 矩阵**。它把 \(S\) 沿 K 维切成块，一次只算一小块 \(S_{ij}\in\mathbb{R}^{\text{TILE\_M}\times\text{TILE\_N}}\)，用一块算一块，并维持一份「到目前为止」的 softmax 统计量（行最大值 \(m\) 与行求和 \(l\)）。每处理一个新的 K 块，就用一次「合并」更新 \(m,l\) 和输出累加器，最终除以 \(l\) 得到结果。这样显存占用从 \(O(N^2)\) 降到 \(O(N)\)，而且分块数据能留在片上寄存器/共享内存里反复复用，对带宽极友好。下面要讲的 cuTile 内核，就是这套算法的直接落地。

## 3. 本讲源码地图

本讲涉及两个关键文件，外加一个分发 stub：

| 文件 | 作用 |
| --- | --- |
| [src/tilegym/ops/cutile/attention.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py) | FMHA 的核心：设备函数 `fmha_kernel_impl`、前向内核 `_fmha_kernel`、自动调优 `_cutile_autotune_fmha`、主机封装 `_tile_prefill_fmha`、注册实现 `tile_fmha`。本讲前 3 个模块都在这里。 |
| [src/tilegym/ops/attn_interface.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py) | 高层封装 `fmha_interface` 与工厂 `get_fmha_interface`，负责把 HuggingFace 风格的签名翻译成 `tilegym.ops.fmha` 调用。第 4 个模块在这里。 |
| [src/tilegym/ops/ops.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py) | `fmha` 的统一分发 stub（L313–L343），是 `fmha_interface` 转发的终点、`tile_fmha` 注册的键。 |

另外，测试范例 [tests/ops/test_attention.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_attention.py) 用 `torch.nn.functional.scaled_dot_product_attention` 做参考、给出了容差，是本讲实践的权威依据。

## 4. 核心概念与源码讲解

cuTile 版 FMHA 的算法实现集中在设备函数 `fmha_kernel_impl`（本讲主线），它由前向内核 `_fmha_kernel` 包一层后经 `ct.launch` 启动。我们先看整体结构，再按四个最小模块拆解。

### 4.1 分块 QKᵀ 计算

#### 4.1.1 概念说明

标准注意力要先算出完整的 \(S=QK^{T}\)。Flash Attention 的第一刀，是把这个大 GEMM **按 K 维（N 维）切成块**：每个 CTA（线程块）负责「若干 query 行 × 全部 key 列」的结果，但 key 列不是一次性全算，而是在循环里一块一块地累加。

这正好复用了 u5-l1 的分块 GEMM 思路——把输出切成瓦片、用累加器在 K 方向循环累加。区别在于：GEMM 的累加器只做线性乘加，而这里每个 K 块算完后还要插一段「在线 softmax 合并」（见 4.2）。

#### 4.1.2 核心流程

主机侧的网格设置（见 4.4 的 `_cutile_autotune_fmha`）是：

\[ \text{grid} = (\lceil q\_len / \text{TILE\_M}\rceil,\ \text{batch}\times\text{num\_heads},\ 1) \]

也就是说，`bid(0)`（记作 `bid_x`）索引**一个 query 行块**，`bid(1)`（记作 `bid_y`）索引「batch × 头」。每个 CTA 固定负责某 (batch, head) 的一组 query 行，然后循环遍历所有 key 块。流程：

```
bid_y → 拆出 batch_idx, head_idx, off_kv_h（GQA：query 头映射到 KV 头）
加载本块 query：q ∈ (TILE_M, TILE_D)          # 每个 CTA 只加载一次 Q
for j in 0 .. Tc:                              # 沿 K/N 维遍历
    加载第 j 个 key 块（转置后）k ∈ (TILE_D, TILE_N)
    qk = mma(q, k)   ∈ (TILE_M, TILE_N)        # 分块 QKᵀ
    （见 4.3 的 causal 掩码）
    （见 4.2 的在线 softmax 合并 + 加载 v、mma 到 acc）
acc /= l_i   # 最终归一化
写回 Out
```

#### 4.1.3 源码精读

**① 把 `bid_y` 映射到 batch 与 head**（含 GQA 支持）：

[attention.py:L64-L67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L64-L67) — `batch_idx = bid_y // H`，`head_idx = bid_y % H`；GQA（分组查询注意力）下多个 query 头共享一个 KV 头，所以 `off_kv_h = head_idx // QUERY_GROUP_SIZE`，后续用它去索引 K/V 的头维度。

**② query 偏移与初始化在线 softmax 累加器**：

[attention.py:L73-L84](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L73-L84) — `offs_m` 是本块 query 的行下标基底（加上 `input_pos` 支持 chunked prefill）；`m_i=-inf`、`l_i=0`、`acc=0` 三个 float32 累加器是整个在线 softmax 的状态，每行一组。

**③ 加载 query 瓦片**：

[attention.py:L90-L95](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L90-L95) — 用 `ct.load` 的 `index=(batch_idx, head_idx, bid_x, 0)` + `shape=(1,1,TILE_M,TILE_D)` 取出一个二维瓦片再 `reshape` 成 `(TILE_M, TILE_D)`；`padding_mode=ct.PaddingMode.ZERO` 保证 `TILE_M` 超过真实行数时越界读零，避免复用显存里的脏数据导致 softmax 产生 NaN。

**④ 循环内分块 QKᵀ**：

[attention.py:L112-L121](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L112-L121) — 这一段就是本模块的核心。注意 K 的加载：

```python
k = ct.load(K, index=(batch_idx, off_kv_h, 0, j),
            shape=(1, 1, TILE_D, TILE_N), order=(0, 1, 3, 2), latency=2)
k = k.reshape((TILE_D, TILE_N))
qk = ct.full((TILE_M, TILE_N), 0.0, dtype=ct.float32)
qk = ct.mma(q, k, qk)  # [TILE_M, TILE_N]
```

- `order=(0, 1, 3, 2)` 在加载时就把 K 的第 2、3 维对调，等价于「在搬运时做转置」，让取回的瓦片天然是 `(TILE_D, TILE_N)`——这正是算 \(QK^{T}\) 需要的 K 形状，省一次显式的 transpose。
- `latency=2` 是给 TMA（张量内存加速器）的搬运延迟提示，让编译器更好地重叠访存与计算（u5-l1 讲过 cuTile 常用 `latency` 这类 hint）。
- `ct.mma(q, k, qk)` 是张量核心矩阵乘加：\(qk \leftarrow q \cdot k + qk\)，与 u5-l1 的 matmul 累加器写法一致。累加器恒为 fp32，保证长 K 循环里累加不丢精度。

#### 4.1.4 代码实践

**实践目标**：验证「分块 QKᵀ = 完整 QKᵀ」这件事，体会 Flash 的分块不改变数学结果。

**操作步骤**（源码阅读型 + 小型验证）：

1. 在纯 PyTorch 里模拟「分块 QKᵀ」：随机 `q ∈ (1, S, d)`、`k ∈ (1, S, d)`，设 `TILE_N`，用 `for j in range(0, S, TILE_N)` 循环取 `k_j = k[:,:,j:j+TILE_N]`，逐块 `qk_j = q @ k_j.transpose(-1,-2)` 拼接，与一次性 `q @ k.transpose(-1,-2)` 比较。
2. 进阶：把循环里的 `qk_j` 先存进一个 `qk_full` 张量（**物化完整 S 矩阵**），体会当 `S` 取 `2**12`、`2**13` 时显存占用与拼接耗时的增长。

**预期结果**：分块拼接的结果与一次性计算在数值上完全一致（误差为浮点累加顺序导致的 1e-6 量级），证明 Flash 切块只是改了计算顺序、不改变数学含义。物化完整 `qk_full` 在 `S=2**13` 时约占 `2**26` 个元素，可直观感受 \(O(N^2)\) 的代价。

> 待本地验证：第 2 步的显存/耗时随 `S` 增长的曲线，建议在带 GPU 的机器上跑。

#### 4.1.5 小练习与答案

**练习 1**：为什么 K 用 `order=(0,1,3,2)` 加载，而 Q 不用？
**答案**：算 \(QK^{T}\) 时 Q 保持 `(TILE_M, TILE_D)`，K 需要转置成 `(TILE_D, TILE_N)` 才能做 `mma(q, k)`。`order` 在加载时重排维度，等价于「搬运时转置」，免去额外的 `permute`。

**练习 2**：如果 `TILE_M` 大于真实 query 行数，`padding_mode=ZERO` 为什么能避免 NaN？
**答案**：越界行读零 → 这些行的 `qk` 全零 → 经在线 softmax 后它们对结果无贡献；若读的是脏数据（含极大值），则 `exp` 后可能溢出产生 NaN/Inf 并污染同块其它行的归一化。补零把越界行「安全隔离」。

---

### 4.2 online softmax（m/l 在线更新）

#### 4.2.1 概念说明

这是 Flash Attention 的灵魂。普通数值稳定 softmax 需要先扫一遍整行求最大值 \(m\)，再扫一遍算 \(\sum_j e^{s_j - m}\)。但在分块场景下，**我们还没看到整行**——每次只有一小块 \(S_{ij}\)。在线 softmax 的思路是：维护一份「目前为止」的行最大值 \(m\) 与行求和 \(l\)，每来一个新块就「合并」一次，最终结果与扫两遍完全等价。

关键技巧是**用 \(e^2\)（exp2）代替 \(e\)**：`exp2` 映射到一条快速的硬件指令。为此把缩放因子预先乘以 \(1/\ln 2\)，使得 \(e^{s} = 2^{s/\ln 2}\)，整个过程都在「以 2 为底」的世界里运算。

#### 4.2.2 核心流程

设当前已累积的状态为 \((m, l, acc)\)（`acc` 是输出累加器 \(\sum p_j v_j\)），新来一块分数 \(s\) 与对应 \(v\)。合并步骤（对应内核 4.1.3 ④ 之后的代码）：

\[ m_{\text{new}} = \max\bigl(m,\ \max(s)\bigr) \tag{新行最大值} \]
\[ \alpha = 2^{\,m - m_{\text{new}}} \tag{旧累加器的修正系数} \]
\[ p_j = 2^{\,s_j - m_{\text{new}}} \tag{本块的未归一权重} \]
\[ l_{\text{new}} = l \cdot \alpha + \sum_j p_j \tag{合并行求和} \]
\[ acc_{\text{new}} = acc \cdot \alpha + \sum_j p_j\, v_j \tag{合并输出} \]
\[ m \leftarrow m_{\text{new}} \]

理解 \(\alpha = 2^{m - m_{\text{new}}}\)：之前的 \(l\) 和 \(acc\) 都是用「旧最大值 \(m\)」减去的，现在最大值涨到了 \(m_{\text{new}}\)，所以旧贡献要统一乘以 \(2^{m-m_{\text{new}}}\) 才能与新块在同一基准下相加。循环结束后再做一次最终归一化：

\[ o = acc / l \]

这个公式就是内核里 `m_ij / alpha / l_i / acc` 那几行的数学含义。注意全程**没有任何 \(S\times S\) 的矩阵被物化**——\(m,l,acc\) 都只是「每行一组」的标量/向量，因此显存占用是 \(O(N)\) 而非 \(O(N^2)\)。

#### 4.2.3 源码精读

**① 把 scale 折进 exp2 世界**：

[attention.py:L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L70) — `qk_scale = qk_scale * INV_LOG_2`，其中 `INV_LOG_2 = 1.0 / math.log(2)`（定义在 [L28](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L28)）。这样后面 `ct.exp2(qk * qk_scale)` 在数学上等价于 \(e^{qk \cdot \text{原scale}}\)，但走的是快的 base-2 路径。

**② 在线 softmax 合并的核心几行**：

[attention.py:L136-L146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L136-L146) — 这正是上面流程公式的逐行落地：

```python
m_ij = max(m_i, ct.max(qk, axis=-1, keepdims=True) * qk_scale)   # m_new
qk = qk * qk_scale - m_ij                                          # s - m_new
p = ct.exp2(qk, flush_to_zero=True)                               # 2^(s-m_new)
l_ij = ct.sum(p, axis=-1, keepdims=True)                          # Σ p_j
alpha = ct.exp2(m_i - m_ij, flush_to_zero=True)                   # 修正系数 α
l_i = l_i * alpha + l_ij                                          # 合并 l
acc = acc * alpha                                                 # 旧 acc 乘 α
```

要点：
- `ct.max(qk, axis=-1)` 沿 key（N）维归约，给每个 query 行算最大值，`keepdims=True` 保持 `(TILE_M, 1)` 形状以便广播。
- 注释「Moving qk_scale multiplication after reduce_max is to improve performance」点出一个性能细节：先对未缩放的 `qk` 取 `max`、再把缩放挪到后面，能让指令调度更优。
- `flush_to_zero=True` 把亚正规数（denormal）清零，避免慢的硬件处理路径——这与 u4-l1 的 silu 近似是同一套手法。

**③ 紧接着加载 v 并累加进 acc，再更新 m_i**：

[attention.py:L148-L156](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L148-L156) — `v` 以 `(TILE_N, TILE_D)` 加载（`latency=4` 提示），权重 `p` 先 `astype(Q.dtype)` 降回低精度喂张量核心，`acc = ct.mma(p, v, acc)` 完成 \(\sum p_j v_j\)，最后 `m_i = m_ij` 把行最大值推进到新值，本块合并结束。

**④ 循环外的最终归一化与写回**：

[attention.py:L158-L160](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L158-L160) — `acc = ct.truediv(acc, l_i, ...)` 做最后的除以 \(l\)；`astype(Out.dtype)` 降回存储精度；`ct.store` 按 `index=(batch_idx, head_idx, bid_x, 0)` 写回。注意输出张量 `Out` 是主机侧 `torch.empty_like(q)` 预分配的（见 4.4），内核只负责填进去。

#### 4.2.4 代码实践

**实践目标**：用一个最小 Python 脚本复现「在线 softmax」，亲眼看到它逐块合并后与一次性 softmax 结果一致。

**操作步骤**（纯 Python/NumPy，无需 GPU）：

```python
# 示例代码：在线 softmax 数值演示（非项目源码）
import numpy as np
np.random.seed(0)
S = 1024; d = 4; TILE_N = 128
Q = np.random.randn(S, d)
K = np.random.randn(S, d)
scale = 1.0 / np.sqrt(d)

m = -np.inf * np.ones(S); l = np.zeros(S); acc = np.zeros((S, d))
for j in range(0, S, TILE_N):
    s = (Q @ K[j:j+TILE_N].T) * scale          # 本块分数
    m_new = np.maximum(m, s.max(axis=1))
    alpha = 2 ** ((m - m_new) / np.log(2))      # 用 e 时 α = e^(m-m_new)
    p = np.exp(s - m_new[:, None])
    l = l * alpha + p.sum(axis=1)
    acc = acc * alpha[:, None] + p @ np.random.randn(TILE_N, d)  # v 随机
    m = m_new
out = acc / l[:, None]
```

**需要观察的现象**：把 `TILE_N` 从 `S`（退化为一次性）逐步改小到 `16`，最终 `out` 数值几乎不变——这验证了「块越细、显存越省、但结果不变」。

**预期结果**：不同 `TILE_N` 下 `out` 之间的最大相对误差在 1e-6 量级（浮点累加顺序差异）。

> 待本地验证：建议改 `TILE_N` 跑 4 个值，记录误差，确认在线合并的数值稳定性。

#### 4.2.5 小练习与答案

**练习 1**：为什么内核用 `exp2` 而不是 `exp`？又为什么要先 `qk_scale *= 1/ln2`？
**答案**：`exp2` 对应一条快速硬件指令，比 `exp` 快。但数学上注意力用的是自然指数 \(e^{s\cdot\text{scale}}\)，而 \(e^x = 2^{x/\ln 2}\)，所以把 scale 预乘 \(1/\ln 2\) 后，`exp2(qk * qk_scale)` 就等价于原来的自然指数形式，既快又正确。

**练习 2**：如果删掉 `alpha`（即不把旧的 `l_i`、`acc` 乘以修正系数），会发生什么？
**答案**：旧块用的是过时的最大值 \(m\)，新块用的是更大的 \(m_{\text{new}}\)，两者基准不一致，直接相加会让旧块的指数权重相对偏大，softmax 结果失真、数值错误。`alpha` 正是把旧贡献「平移」到新基准上。

---

### 4.3 causal 掩码与 scaling

#### 4.3.1 概念说明

有了分块 QKᵀ 和在线 softmax，注意力还差两件事：

- **scaling**：分数要乘 \(1/\sqrt{d}\)，防止点积过大导致 softmax 饱和。`fmha_interface` 默认 `scaling = 1/sqrt(head_dim)`，作为 `sm_scale` 传入。
- **causal 掩码**：自回归模型里 query \(i\) 只能看 key \(j\le i\)。朴素做法是给上三角位置加 \(-\infty\)（`exp` 后变 0）。但逐元素造掩码很贵，cuTile 内核用了两个优化：**循环上界裁剪**（因果性使 query 行块根本不需要遍历到自己之后的 key 块）和**只在靠近对角线的块才造掩码**。

#### 4.3.2 核心流程

内核在进入 K 循环前，根据 `CAUSAL` 与 `EVEN_K` 预算两个量：

- `Tc`：K 循环上界。因果时只需遍历到 `min(m_end, k_seqlen)`（query 行块末端与 KV 长度的较小者），省掉对角线右上方整块整块的无效计算。
- `mask_start`：从第几个 key 块开始需要逐元素造掩码。完全在因果下三角内部的块（`j < mask_start`）整块合法，无需掩码；只有跨越对角线的块才要逐元素判断。

循环内，只有当 `(CAUSAL or not EVEN_K) and j >= mask_start` 时才计算掩码：

```
mask = True                                       # 默认全合法
if not EVEN_K: mask &= (offs_n < k_seqlen)        # KV 长度不是 tile 整数倍：越界屏蔽
if CAUSAL:  mask &= (offs_m >= offs_n)            # 因果：query 行 >= key 列
qk += where(mask, 0.0, -inf)                      # 非法位置加 -inf
```

#### 4.3.3 源码精读

**① 循环上界与 mask_start 的预算**：

[attention.py:L97-L108](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L97-L108) — 因果分支里 `Tc = ct.cdiv(min(m_end, k_seqlen), TILE_N)` 把循环限制到必要的块数；`mask_start = (input_pos + bid_x*TILE_M) // TILE_N` 算出「本 query 块的对角线落在第几个 key 块」，再用 `min(mask_start, k_seqlen//TILE_N)` 防止越界。非因果分支则 `Tc = cdiv(k_seqlen, TILE_N)`、`mask_start = k_seqlen//TILE_N`（无因果掩码时仍有 KV 越界掩码）。

**② 逐元素掩码构造**：

[attention.py:L123-L133](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L123-L133) — 关键是因果判断 `mask = mask & (offs_m >= offs_n)`：`offs_m` 是 `(TILE_M, 1)` 的 query 行下标、`offs_n` 是 `(1, TILE_N)` 的 key 列下标，广播成 `(TILE_M, TILE_N)` 的布尔掩码，合法位置（query ≥ key）保留、其余加 \(-\infty\)。注意这是发生在「在线 softmax 取 max 之前」的，所以 \(-\infty\) 会被 `max` 排除、`exp` 后贡献为 0，干净利落。

**③ scaling 的传递路径**：

scaling 不在内核里硬编码，而是由上层算好 `sm_scale` 经 `ct.launch` 的 args 传入（见 4.4 的 `_cutile_autotune_fmha` 调用），在 [attention.py:L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L70) 折进 exp2 后贯穿整个计算。默认值来自 [attention.py:L891-L892](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L891-L892)（`tile_fmha` 里 `scaling = 1.0 / math.sqrt(q.size(-1))`）。

#### 4.3.4 代码实践

**实践目标**：通过对照测试，确认 causal 掩码的语义与 `torch.nn.functional.scaled_dot_product_attention(is_causal=True)` 一致，并理解 `mask_start` 的省算效果。

**操作步骤**（源码阅读 + 对照测试）：

1. 阅读 [attention.py:L100-L107](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L100-L107)，对一个 `q_len=9, TILE_M=64, TILE_N=128` 的小例子手算 `Tc` 与 `mask_start`，验证「query 块 0 不需要遍历到 key 块 1」。
2. 运行本讲 4.4 给出的对照脚本（或仓库的 `tests/ops/test_attention.py`），把 `is_causal` 在 `True/False` 间切换，观察输出差异：非因果时每个 query 行对所有 key 都有非零权重；因果时上三角为 0。

**预期结果**：cuTile FMHA 与 `F.scaled_dot_product_attention` 在 fp16/bf16 下满足 `atol=5e-2, rtol=1e-2`（正是 [test_attention.py:L118-L120](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_attention.py#L118-L120) 的容差）。`mask_start` 在长序列下能跳过约一半的逐元素掩码计算。

> 待本地验证：第 2 步需 Blackwell/Ampere + `cuda-tile` 环境；无 GPU 时可只做第 1 步的手算练习。

#### 4.3.5 小练习与答案

**练习 1**：`mask = ct.where(mask, 0.0, -math.inf)` 之后，`-inf` 会不会污染在线 softmax 的 `max`？
**答案**：不会。合法位置加 0 不变；非法位置加 \(-\infty\)。随后 `m_ij = max(m_i, max(qk*...))` 取行最大值时，\(-\infty\) 永远不会被选为最大（只要该行至少有一个合法的有限分数），而且 `exp2(-inf - m_ij) = 0`，非法位置权重恰好为 0。

**练习 2**：为什么 `mask_start` 用 `(input_pos + bid_x*TILE_M) // TILE_N` 而不是直接 `bid_x`？
**答案**：`mask_start` 要回答「本 query 块的对角线落在第几个 key 块」。query 块的起始绝对位置是 `input_pos + bid_x*TILE_M`（`input_pos` 来自 chunked prefill，KV 比 Q 长时非零），换算成「以 TILE_N 计的块号」要除以 `TILE_N`。当 `TILE_M != TILE_N` 时它和 `bid_x` 不同，直接用 `bid_x` 会算错对角线位置。

---

### 4.4 fmha_interface 封装：从高层调用到内核启动

#### 4.4.1 概念说明

到目前为止我们讲的都是设备函数 `fmha_kernel_impl`。但在调用方（尤其是 HuggingFace 模型）眼里，注意力是一个带 `(module, q, k, v, attention_mask, ...)` 签名的函数。`attn_interface.py` 提供的 `fmha_interface` 与工厂 `get_fmha_interface` 就是这两者之间的「适配层」：它把高层语义（默认 scaling、是否解码、输出转置）翻译成一次对统一分发入口 `tilegym.ops.fmha` 的调用，再由分发器路由到当前后端的 `tile_fmha`。

这条链路把 u2 讲的「接口—分发—实现」三层架构和本讲的内核算法缝在一起，是理解「内核如何被真正用起来」的最后一环。

#### 4.4.2 核心流程

完整调用链（一次 prefill 注意力）：

```
get_fmha_interface(backend=...)(module, q, k, v, ...)     # HF 风格入口
        │  （默认 scaling=1/sqrt(d)；seq_len>1 走 prefill）
        ▼
fmha_interface(q, k, v, is_causal, scaling, backend=...)  # 高层封装
        │  （from tilegym.ops import fmha）
        ▼
tilegym.ops.fmha(q,k,v,scaling,is_causal,...,backend=)    # @dispatch("fmha") stub
        │  （wrapper 查 _REGISTRY，按 backend 选实现）
        ▼
tile_fmha  (register_impl("fmha", backend="cutile"))      # cuTile 实现
        │
        ▼
_tile_prefill_fmha  →  _cutile_autotune_fmha  →  ct.launch(_fmha_kernel)
        │                                                  │
        │  （tune-once/cache/launch，见 u5-l3）            ▼
        └────────────────────────────────────►  fmha_kernel_impl  (4.1–4.3)
```

关键点：`fmha_interface` 自身**不做计算**，只是转发；真正选后端的是分发器 wrapper（通过 `backend=` 参数或当前后端）。

#### 4.4.3 源码精读

**① 分发 stub：只声明签名、抛 NotImplementedError**：

[ops.py:L313-L343](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L313-L343) — `@dispatch("fmha")` 把 `fmha` 登记进 `_REGISTRY`，函数体 `raise NotImplementedError(...)` 仅是占位实现（u2-l1 已讲：stub 声明统一签名与 docstring，各后端用 `@register_impl` 挂实现到同一个 `"fmha"` 键）。

**② 高层封装：转发到分发入口**：

[attn_interface.py:L58-L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L58-L70) — 这是本模块的核心几行：

```python
from tilegym.ops import fmha          # 拿到 @dispatch 装饰后的 wrapper
return fmha(q, k, v, scaling=scaling, is_causal=is_causal,
            has_backward=has_backward, kernel_configs=kernel_configs,
            backend=backend)          # backend= 由 wrapper 拦截，决定走哪个实现
```

注意 `backend=` 是调用级覆盖（u2-l1 讲的最高优先级后端选择），由 wrapper 经 `kwargs.pop` 拦截，不改进程级当前后端。

**③ 工厂函数：HF 风格签名 + 解码分流 + 输出转置**：

[attn_interface.py:L73-L122](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L73-L122) — `get_fmha_interface(backend=..., kernel_configs=...)` 返回一个闭包 `fmha_interface_wrapper`，它的签名 `(module, q, k, v, attention_mask, dropout, scaling, ...)` 正是 HuggingFace `eager_attention_forward` 的形状。它做三件事：
1. 默认 `scaling = 1.0 / math.sqrt(q.size(-1))`（[L97-L98](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L97-L98)）。
2. **解码分流**：当 `q.size(-2) == 1`（单 token）时改走 `fmha_decode`（[L100-L103](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L100-L103)）——那是 u6-l2 的解码内核，本讲不展开。
3. 调 `fmha_interface(...)`，再把输出 `o.transpose(1,2).contiguous()`（[L120](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L120)），把 `[B,H,S,D]` 转成 HF 期望的 `[B,S,H,D]`。

**④ cuTile 实现：从 `tile_fmha` 到 `ct.launch`**：

[attention.py:L882-L895](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L882-L895) — `@register_impl("fmha", backend="cutile")` 把 `tile_fmha` 挂到 `"fmha"` 键的 cutile 槽位；它只是算默认 scaling 后调 `_tile_prefill_fmha`。

[attention.py:L847-L879](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L847-L879) — `_tile_prefill_fmha` 是主机侧准备：`contiguous()` 保证连续、`torch.empty_like(q)` 预分配输出、算 `query_group_size`、`input_pos = k_len - q_len`（支持 chunked prefill）、用 `EVEN_K = (k_len % max_tile_n)==0` 决定是否需要 KV 越界掩码，最后交给 `_cutile_autotune_fmha`。

[attention.py:L743-L844](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L743-L844) — `_cutile_autotune_fmha` 是 u5-l3 讲过的「tune-once/cache/launch」：首次按 `fwd_cache_key` 跑 `exhaustive_search` 选最优 `TILE_M/TILE_N/num_ctas/occupancy`（候选来自 [L182-L200](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L182-L200) 的按架构分流配置），把 `(best_cfg, tuned_kernel)` 缓存，之后直接 `ct.launch`。候选配置按 GPU 架构分流：sm120/pre-SM90 给较小的 `64×64`，Blackwell(sm100) 给更大的 `256×128`。

[attention.py:L684-L723](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L684-L723) — `_fmha_kernel` 只是 `@ct.kernel` 包一层，取出 `bid_x=ct.bid(0)`、`bid_y=ct.bid(1)` 后转交给 `fmha_kernel_impl`。之所以把算法抽成 `fmha_kernel_impl` 而非直接写在 `@ct.kernel` 里，是为了让多个内核（如独立 prefill 与融合 POD attention）复用同一段注意力计算，避免重复（见 [L35-L38](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L35-L38) 的注释）。

#### 4.4.4 代码实践

**实践目标**：跑通「高层封装 → 分发 → cuTile 内核」的最小调用，并与 PyTorch 参考对比；同时验证你对 `fmha_interface` 转发机制的理解。

**操作步骤**：

1. 写一个约 15 行脚本，调用 `fmha_interface` 并与 `torch.nn.functional.scaled_dot_product_attention`（SDPA）对比：

```python
# 示例代码：调用 fmha_interface 并与 PyTorch SDPA 对比
import math, torch
from tilegym.ops import fmha_interface, set_backend   # set_backend 来自 tilegym 顶层

set_backend("cutile")   # 或省略，默认即 cutile
B, H, S, D = 2, 32, 4095, 128
dtype = torch.bfloat16
q = torch.randn(B, H, S, D, device="cuda", dtype=dtype)
k = torch.randn(B, H, S, D, device="cuda", dtype=dtype)
v = torch.randn(B, H, S, D, device="cuda", dtype=dtype)
sm_scale = 1.0 / math.sqrt(D)

out = fmha_interface(q, k, v, is_causal=True, scaling=sm_scale)
ref = torch.nn.functional.scaled_dot_product_attention(
    q, k, v, is_causal=True, scale=sm_scale)
err = (out - ref).abs().max().item()
print("max abs err =", err)        # 期望 <= 5e-2 量级
```

2. **源码追踪**：在 `fmha_interface` 内部的 `return fmha(...)`（[attn_interface.py:L61-L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/attn_interface.py#L61-L70)）处看到它如何把 `backend=` 透传给 `tilegym.ops.fmha` 的 wrapper；再在 [attention.py:L882](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L882) 确认 `tile_fmha` 正是注册到同一 `"fmha"` 键的 cutile 实现，从而回答「转发如何落到这个内核」。

**需要观察的现象**：
- `fmha_interface` 返回的形状是 `[B,H,S,D]`（注意：工厂 `get_fmha_interface` 才会额外 `transpose(1,2)`，直接调 `fmha_interface` 不转置）。
- 最大绝对误差在 bf16 容差内（`atol≈5e-2`），与 [test_attention.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_attention.py) 的断言一致。

**预期结果**：在 Blackwell/Ampere + `cuda-tile` 环境下，输出与 SDPA 数值吻合。

> 待本地验证：本脚本需要支持 `cuda.tile` 的 GPU（Blackwell CUDA 13.1+ 或 Ampere CUDA 13.2+）。无该环境时，可只做第 2 步的源码追踪，并直接阅读 `tests/ops/test_attention.py` 里的参数化用例（含 `(1,32,2047,128,True,fp16)`、`(2,32,4095,128,True,bf16)` 等）作为「断言依据」。

#### 4.4.5 小练习与答案

**练习 1**：`fmha_interface` 为什么要写成「函数内 `from tilegym.ops import fmha`」再调用，而不是在文件顶部 import？
**答案**：这是规避循环导入的常见手法。`attn_interface.py` 顶部已经 `from tilegym.backend import *`，而 `tilegym.ops` 的初始化又可能间接触发后端/算子注册；把对 `tilegym.ops` 的 import 推迟到函数体内，确保只在真正调用时才解析，避免模块加载期的循环依赖。

**练习 2**：`get_fmha_interface` 返回的闭包在 `q.size(-2)==1` 时改调 `fmha_decode`，为什么不直接用 prefill 内核？
**答案**：解码时 query 只有 1 个 token，prefill 内核的网格与分块（按 `TILE_M` 切 query 行）会大量浪费并行度。解码内核（u6-l2）针对「单 query、长 KV」做了 split-KV 等专门优化，所以工厂函数按 query 长度自动分流到更合适的内核。

---

## 5. 综合实践

把本讲四个模块串成一个任务：**「在一个内核里讲清一次 attention」**。

**任务**：给定 `[B=1, H=4, S=512, D=64]` 的小规模注意力，请完成下面三件事，把分块、在线 softmax、因果掩码、封装链路全部走一遍。

1. **算法层（NumPy）**：仿照 4.2.4 的在线 softmax 脚本，写一个带 **causal 掩码** 的分块 attention（`TILE_M=64, TILE_N=64`），输出与一次性 `F.scaled_dot_product_attention(is_causal=True)` 对比，记录最大误差。
2. **源码层（阅读）**：对照你的 NumPy 实现，在 [attention.py:L111-L160](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L111-L160) 的 K 循环体里，逐一标注哪几行对应你脚本中的「分块 QKᵀ」「causal 掩码」「在线 softmax 合并」「mma 到 acc」「最终归一化」。
3. **封装层（追踪）**：画出从 `get_fmha_interface(...)(...)` 到 `ct.launch(_fmha_kernel)` 的完整调用链（参考 4.4.2），并指出「后端选择」发生在哪一步（提示：`@dispatch` 的 wrapper 查 `_REGISTRY`）。

**验收标准**：
- 第 1 步最大误差在 1e-5 量级（NumPy fp64）。
- 第 2 步能正确指出 [L136-L146](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L136-L146) 是「在线 softmax 合并」、[L131](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L131) 是「因果掩码」。
- 第 3 步能说清 `fmha_interface → tilegym.ops.fmha(wrapper) → tile_fmha` 的转发，并指出 `backend=` 参数在 wrapper 处被拦截用于选实现。

> 待本地验证：第 1 步可在任意带 NumPy 的机器完成；若要跑真实 cuTile 内核对照，需 Blackwell/Ampere + `cuda-tile` 环境。

## 6. 本讲小结

- **注意力墙与 Flash 直觉**：完整 \(S=QK^{T}\) 是 \(O(N^2)\) 显存，Flash Attention 用「分块 + 在线 softmax」把它降到 \(O(N)\)，且数据留在片上复用。
- **分块 QKᵀ**：每个 CTA 负责 (batch,head) 的一组 query 行，沿 K/N 维循环加载 key 块做 `ct.mma`——就是 u5-l1 分块 GEMM 的复用；K 在 `ct.load` 时用 `order=(0,1,3,2)` 边搬边转置。
- **在线 softmax 的 m/l 更新**：维护行最大值 `m_i`、行求和 `l_i`、输出累加器 `acc`；每来一块用修正系数 `alpha=exp2(m_i-m_ij)` 把旧贡献平移到新基准后合并，循环结束除以 `l_i`。全程用 `exp2`（scale 预乘 `1/ln2`）走快速硬件路径。
- **causal 掩码与 scaling**：因果性 `offs_m >= offs_n` 加 \(-\infty\)；用 `Tc` 裁剪循环上界、`mask_start` 只在近对角线块造掩码，省掉无效计算。scaling 由上层传入、在 [L70](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L70) 折进 exp2。
- **fmha_interface 封装**：`fmha_interface` 不做计算，只把调用转发给统一分发入口 `tilegym.ops.fmha`；分发 wrapper 按 `backend=`/当前后端查 `_REGISTRY` 路由到 `tile_fmha`，再经 `_tile_prefill_fmha → _cutile_autotune_fmha → ct.launch(_fmha_kernel)` 启动算法内核。
- **复用与扩展**：算法被抽成 `fmha_kernel_impl` 以便多个内核复用；前向另有带 LSE 的版本 `_fmha_fwd_kernel_with_lse` 与完整反向（`_fmha_bwd_*`）支持训练，后者被 `@experimental_kernel` 标记，本讲未展开。

## 7. 下一步学习建议

- **u6-l2 解码注意力与 Split-KV**：本讲的 prefill 内核按 query 行切分；解码时 query 只有 1 个 token，并行度全在 KV 维，下一讲讲 `fmha_decode` 如何用 split-KV 切分长 KV 并用 `splitk_reduce` 归约。建议先复习本讲的在线 softmax，因为解码版要做跨 split 的 LSE 合并。
- **u6-l3 MLA 与 u6-l4 变体**：在掌握 prefill/解码两阶段后，继续看多潜注意力（MLA，qpe/kpe 联合位置编码）和 attention sink / gemma soft-cap / sparse_mla 等变体如何在本讲的框架上「换索引/掩码语义」。
- **源码延伸阅读**：若关心训练路径，可读 [attention.py:L246-L358](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L246-L358)（带 LSE 的前向）与 [L977-L1254](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention.py#L977-L1254)（`_fmha_backward` 的 dK/dV/dQ 三内核），它们复用了本讲的分块与在线 softmax 思想，区别在于反向要用 LSE 重建 softmax 概率。
