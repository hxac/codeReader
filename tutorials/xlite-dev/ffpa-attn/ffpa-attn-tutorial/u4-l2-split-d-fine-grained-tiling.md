# Split-D：大 head_dim 的 MMA 级精细分块

## 1. 本讲目标

本讲是 Triton 前向 kernel 的「Split-D 专题」，承接 [u4-l1](u4-l1-triton-fwd-online-softmax.md) 讲过的通用路径四阶段（Phase 1 QK → Phase 2 softmax → Phase 3 PV → Phase 4 收尾）。u4-l1 关注的是「沿序列长度 N 的 KV 主循环 + online softmax」，本讲则放大看 **head_dim（D）这一维在 kernel 内部是如何被切分处理的**——这正是 FFPA 区别于标准 FlashAttention 的核心。

学完本讲你应当能够：

1. 说清 D 在两次矩阵乘里扮演的**两种不同身份**：在 QK^T 里是「归约维」，在 PV 里是「输出维」——这一区别决定了 Split-D 的两种切法。
2. 解释 `NUM_V_GROUPS = cdiv(HEADDIM, BLOCK_HEADDIM_V)` 的含义，以及为什么 QK 用「累加进同一块 score」、而 PV 用「每个 V-group 一个独立累加器」。
3. 读懂 [`_ffpa_fwd.py`](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py) 中 **Phase 1（D 片段循环算 score）** 与 **Phase 3（按 V-group 累加 P@V）** 的真实代码。
4. 用伪代码画出 Split-D 下的两个嵌套循环，并标注每个张量驻留在 SRAM 还是寄存器，从而直观看到「SRAM 与 D 无关、寄存器随 D 线性增长」这一复杂度结论。

## 2. 前置知识

### 2.1 回顾：标准 FlashAttention 的分块与它的瓶颈

[u1-l1](u1-l1-what-is-ffpa-split-d.md) 已经讲过：标准 FlashAttention-2 沿序列长度 N 把 Q（行）和 K/V（列）切成 `BLOCK_M × BLOCK_N` 的块，但**每个块沿 D 维是完整加载**的。于是单个 program 在 SRAM 里要同时驻留：

- Q 块 `[BLOCK_M, D]`
- K 块 `[BLOCK_N, D]`
- V 块 `[BLOCK_N, D]`
- score 块 `[BLOCK_M, BLOCK_N]`

前三个张量的大小都正比于 D。当 D ≤ 256 时还能塞进单 SM 的 SRAM 预算；D 一旦到 320/512/1024 就会撑爆，所以经典 FA-2 的上限止于 256。

### 2.2 Split-D 的直觉：别在 SRAM 里放整个 D

Split-D 的核心思想（详见 [u1-l1](u1-l1-what-is-ffpa-split-d.md)）是：**在矩阵乘指令（MMA）这一层就把 D 维再切一刀**，让 SRAM 任意时刻只驻留 D 的一个窄片段，而不是整个 D。这样 SRAM 工作集就与 D 无关（O(1) in D），代价是 D 维的压力被转移到了**寄存器**（O(D) 个累加器）。

> **关于「宽 16」的说明**：[u1-l1](u1-l1-what-is-ffpa-split-d.md) 用「切成宽 16 的片段」描述 Split-D，这里的 16 指的是 **CUDA PTX `mma` 指令 fragment 的基本粒度**（手写 CUDA 后端在该粒度操作，对应 O(d/4) 寄存器复杂度中的 1/4 折扣）。本讲的 Triton 实现把这个粒度封装成了更大的可调 tile `BLOCK_HEADDIM_QK` / `BLOCK_HEADDIM_V`，候选值是 64 / 128（详见 4.1.3）。每个 tile 由若干个 16 宽的 MMA fragment 组成，因此「NUM_V_GROUPS 数的是 tile 个数，不是 16-fragment 个数」。这一点务必记住，否则会把概念层的 16 和实现层的 tile 混淆。

### 2.3 关键直觉：D 在两次矩阵乘里身份不同

注意力是 \(\mathrm{O}=\mathrm{softmax}(s\cdot QK^{\!T})V\)，含两次矩阵乘。把任意矩阵乘 \(C=A\cdot B\) 的三个维度记为 M（行）、N（列）、K（归约/收缩维），那么：

| 矩阵乘 | A | B | 结果 | D 是什么维？ | 切 D 等于切什么？ | 各片段如何合并？ |
|---|---|---|---|---|---|---|
| \(S=QK^{\!T}\) | Q `[M,D]` | K `[N,D]` | S `[M,N]` | **K（归约维）** | 切归约维 | **相加**（reduce） |
| \(\mathrm{O}=PV\) | P `[M,N]` | V `[N,D]` | O `[M,D]` | **N（输出列维）** | 切输出列 | **各写各的**（不同列） |

这张表是本讲的「题眼」：

- **算 score 时**，D 是要被消掉的归约维。把 D 切成片段 \(d_0,d_1,\dots\) 后，\(S=\sum_c Q_{[:,d_c]}K_{[:,d_c]}^{\!T}\)，各片段乘积**相加**进同一块 `[M,N]` 的 score。SRAM 里只需要一个 D 片段的 Q 和 K。
- **算 PV 时**，D 是输出 O 的列维。把 D 切成片段后，每个片段对应 O 的一组不同列，**不能相加**，必须给每个片段（V-group）配一个独立的累加器。

Split-D 正是利用了「归约维可拆分求和、输出维只能各存各的」这条线性代数事实，把两次矩阵乘都改成对 D 的精细分块。

### 2.4 与标准 FA tiling 的本质区别

| 维度 | 标准 FA-2 tiling | FFPA Split-D |
|---|---|---|
| 分块方向 | 仅沿 N（序列长度） | 沿 N **且** 沿 D |
| SRAM 工作集 | \(\propto (B_M+2B_N)\cdot D\)，随 D 线性增长 | \(\propto (B_M+2B_N)\cdot B_{D}\)，与 D **无关** |
| 寄存器压力 | O(1) in D | O(D)（每个 V-group 一个累加器） |
| 支持的 head_dim | ≤ 256 | 320 / 512 / 1024 |

一句话：**Split-D 用「D 维切分 + 寄存器多放几个累加器」换来了「SRAM 不再随 D 爆炸」，从而支持大 head_dim。**

## 3. 本讲源码地图

本讲只涉及一个文件，但聚焦其中三段：

| 代码区域 | 行号 | 作用 |
|---|---|---|
| `_update_o_accs` | L75–L77 | 更新 V-group 累加器元组的辅助函数 |
| `_gen_fwd_autotune_configs` | L126–L169 | 生成候选 tile（决定 `BLOCK_HEADDIM_QK/V`） |
| `_FFPA_FWD_HEURISTICS`（含 `NUM_V_GROUPS`） | L287–L294 | 编译期启发式，算出 V-group 个数 |
| `_ffpa_fwd_kernel_impl` 签名 | L302–L351 | 通用前向 kernel 入参 |
| `num_qk_d_chunks` / `o_accs` 初始化 | L385 / L392–L396 | 两种 D 分块计数与累加器元组 |
| **Phase 1：QK 跨 D 片段归约** | L407–L422 | 本讲核心模块一 |
| **Phase 3：按 V-group 累加 P@V** | L464–L476 | 本讲核心模块二 |
| Phase 4 收尾 | L480–L489 | 用 `l_i` 归一、写 O/LSE |
| 默认 launch config | L976–L983 | 无 autotune 时的 tile 默认值 |

文件：[src/ffpa_attn/triton/_ffpa_fwd.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py)

## 4. 核心概念与源码讲解

### 4.1 NUM_V_GROUPS：head_dim 被切成多少个 V-group

#### 4.1.1 概念说明

`NUM_V_GROUPS` 是「V 在 head_dim 方向被切成多少片」。它由两个编译期常量决定：

\[ \mathrm{NUM\_V\_GROUPS}=\left\lceil \frac{\mathrm{HEADDIM}}{\mathrm{BLOCK\_HEADDIM\_V}} \right\rceil \]

类似地，QK 那边也有一个同构的计数：

\[ \mathrm{num\_qk\_d\_chunks}=\left\lceil \frac{\mathrm{HEADDIM}}{\mathrm{BLOCK\_HEADDIM\_QK}} \right\rceil \]

因为 `_gen_fwd_autotune_configs` 里 `BLOCK_HEADDIM_QK` 和 `BLOCK_HEADDIM_V` **永远取相同值（lockstep）**，所以这两个计数在数值上相等，但用途完全不同：

- `num_qk_d_chunks`：Phase 1 里把 **归约维 D** 切成多少段，各段结果**相加**进同一块 score。
- `NUM_V_GROUPS`：Phase 3 里把 **输出维 D** 切成多少段，每段配一个**独立**的 O 累加器。

#### 4.1.2 核心流程

1. autotune 选定一个 `block_headdim`（64 或 128）。
2. Triton 的 `@triton.heuristics` 在编译期把 `NUM_V_GROUPS` 算成 `tl.constexpr`。
3. kernel 内部把 `NUM_V_GROUPS` 个 fp32 零张量打包成一个**元组** `o_accs`，每个元素形状 `[BLOCK_M, BLOCK_HEADDIM_V]`，对应 O 输出的一个 D 片段。
4. kernel 内部另外用 `tl.cdiv` 在运行期算 `num_qk_d_chunks`，控制 Phase 1 的归约循环次数。

#### 4.1.3 源码精读

**启发式定义**（编译期算 `NUM_V_GROUPS`）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L287-L294](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L287-L294) —— `_FFPA_FWD_HEURISTICS` 把 `NUM_V_GROUPS` 注册为 `cdiv(HEADDIM, BLOCK_HEADDIM_V)`，Triton 会在 JIT 编译时求值并作为常量注入 kernel。

**QK 那边的对偶计数**（运行期）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L385](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L385) —— `num_qk_d_chunks = tl.cdiv(HEADDIM, BLOCK_HEADDIM_QK)`，控制 Phase 1 的归约循环。

**V-group 累加器元组初始化**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L392-L396](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L392-L396) —— 这段注释「Mirrors CUDA fwd's R_D registers: one O accumulator per V head-dim slice」直指 [u1-l1](u1-l1-what-is-ffpa-split-d.md) 讲过的 CUDA `R_D` 寄存器：每个 V head-dim 片段配一个 O 累加器。`o_accs = (zero_acc,) * NUM_V_GROUPS` 在 Triton 里造出一个长度为 `NUM_V_GROUPS` 的张量元组（Triton 用元组表达「展开的数组」），全程驻留寄存器、随 D 线性增长。

**tile 候选值与 lockstep**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L148-L163](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L148-L163) —— `headdim_candidates = [64, 128]`，仅当目标 `headdim == 256` 时才追加 256；并且 `BLOCK_HEADDIM_QK` 与 `BLOCK_HEADDIM_V` 被赋成同一个 `block_headdim`。也就是说，对 D=512 的真实大 head_dim 用例，tile 只会是 64 或 128，`NUM_V_GROUPS` 分别是 8 或 4。

**无 autotune 时的默认 tile**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L976-L983](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L976-L983) —— 默认 `BLOCK_HEADDIM_QK=64, BLOCK_HEADDIM_V=64`，所以 D=512 时默认有 8 个 V-group。

#### 4.1.4 代码实践

**目标**：建立「D → tile → V-group 个数」的直觉。

**操作步骤**（纯算术，源码阅读型）：

1. 固定 D = 512。
2. 对照 [L148-L150](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L148-L150) 列出 tile 候选 {64, 128}。
3. 用公式 \(\lceil 512 / B_V \rceil\) 算 `NUM_V_GROUPS`。

**需要观察的现象 / 预期结果**：

| BLOCK_HEADDIM_V | NUM_V_GROUPS (D=512) | 寄存器里 o_accs 的总 fp32 元素数（BLOCK_M=128） |
|---|---|---|
| 64 | 8 | 8 × 128 × 64 = 65536 |
| 128 | 4 | 4 × 128 × 128 = 65536 |

注意：**总元素数相同**（都是 D × BLOCK_M），tile 大小只改变「分几份」，不改变寄存器总占用——这正说明寄存器占用是 O(D) 的，与 tile 切法无关。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BLOCK_HEADDIM_QK` 和 `BLOCK_HEADDIM_V` 要 lockstep（取相同值）？

**参考答案**：两者都表示「SRAM 里一次驻留多宽的 D 片段」。让它们相等意味着 QK 阶段和 PV 阶段复用同一套 D 分块边界与同一份指针算术，代码更简单、SRAM 工作集一致；同时 `num_qk_d_chunks == NUM_V_GROUPS`，循环次数对齐，编译器更容易做软件流水线。

**练习 2**：D=1024、tile=128 时 `NUM_V_GROUPS` 是多少？此时单个 program 的 O 累加器一共多少个 fp32 元素（BLOCK_M=128）？

**参考答案**：\(\lceil 1024/128\rceil=8\) 个 V-group；累加器总元素 = 8 × 128 × 128 = 131072 个 fp32（是 D=512 时的两倍，再次印证 O(D)）。

---

### 4.2 Phase 1：QK^T 跨 D 片段归约算完整 score

#### 4.2.1 概念说明

在算 \(S=s\cdot QK^{\!T}\) 时，D 是**归约维**。Split-D 把 D 切成 `num_qk_d_chunks` 段，每段宽度 `BLOCK_HEADDIM_QK`。因为归约维可以任意拆分求和：

\[ S_{[M,N]}=\sum_{c=0}^{C-1} Q_{[M,\,d_c]}\;K_{[N,\,d_c]}^{\!T},\qquad C=\left\lceil \frac{D}{B_{QK}} \right\rceil \]

实现上，先开一块全零的 `scores [BLOCK_M, BLOCK_N]`，然后对每个 D 片段加载窄窄一片 Q 和 K，做一次小矩阵乘并**累加进同一块 scores**。Triton 的 `tl.dot(a, b, acc=scores)` 就是 `scores = scores + a@b`，天然表达「D 片段归约」。

关键收益：任意时刻 SRAM 里只有一片 `[*, B_QK]` 的 Q/K，而不是 `[*, D]`。

#### 4.2.2 核心流程

单个 KV 块（固定 `start_n`）里算 score 的伪代码：

```
scores = zeros[BLOCK_M, BLOCK_N]          # SRAM/寄存器，仅此一块
for c in range(num_qk_d_chunks):          # D 归约循环
    q = load Q[行块, c*B_QK : (c+1)*B_QK]  # SRAM，[BLOCK_M, B_QK]，一次一片
    k = load K[列块, c*B_QK : (c+1)*B_QK]  # SRAM，[BLOCK_N, B_QK]，一次一片
    scores = scores + q @ k.T              # 归约维 → 相加
scores = scores * softmax_scale
scores += attn_bias                        # 若有
scores = mask(scores); scores = causal(scores)
# 之后交给 Phase 2 的 online softmax
```

注意 `q`/`k` 切片是**循环覆盖**的——下一次迭代加载新片段，旧片段即可丢弃，所以 SRAM 里同一时刻只占一片。

#### 4.2.3 源码精读

**归约循环主体**（本模块最核心的一小段）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L407-L422](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L407-L422) —— 中文逐行说明：

- L407：每个 KV 块开始前，把 `scores` 清零成 `[BLOCK_M, BLOCK_N]` 的 fp32。
- L408：注释点明这是「Phase 1: QK with Split-D reduction structure」。
- L409：`for qk_d_chunk in range(num_qk_d_chunks):` —— D 归约循环，次数 = `cdiv(D, B_QK)`。
- L410–L411：算当前片段的 D 偏移 `qk_d = qk_d_chunk * B_QK + offs_d_qk`。
- L412–L416：加载 Q 的窄片段 `[BLOCK_M, B_QK]`，带越界 mask（`qk_d < HEADDIM`）补零。
- L417–L421：加载 K 的窄片段 `[BLOCK_N, B_QK]`，同样带 mask。
- L422：`scores = tl.dot(q, tl.trans(k), acc=scores)` —— **关键**：`acc=scores` 表示 `scores += q @ k^T`，把这一 D 片段的乘积累加进同一块 score。这正是「归约维可拆分求和」的代码体现。

**score 后处理**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L424-L440](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L424-L440) —— D 归约完成后才统一乘 `softmax_scale`、加 `attn_bias`（用零 stride 广播，见 [u4-l1](u4-l1-triton-fwd-online-softmax.md) 与 4.3）、做 `EVEN_N` 越界掩码与 `IS_CAUSAL` 尾对齐掩码。这些操作都只作用在 `[BLOCK_M, BLOCK_N]` 这一块上，与 D 无关。

#### 4.2.4 代码实践

**目标**：确认「score 是所有 D 片段之和」这一数值事实。

**操作步骤**（源码阅读 + 待本地验证的计算）：

1. 读 [L407-L422](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L407-L422)，确认 `scores` 在循环外清零、循环内用 `acc=scores` 累加。
2. 用 NumPy/PyTorch 写一段对照（**示例代码**，非项目原有）：

```python
# 示例代码：验证 Split-D 的 QK 归约等价于整 D 一次乘
import torch
M, N, D, B = 4, 4, 512, 64           # B = BLOCK_HEADDIM_QK
Q = torch.randn(M, D); K = torch.randn(N, D)
ref = Q @ K.T                         # 整 D 一次乘
scores = torch.zeros(M, N)
for c in range(D // B):               # D 归约循环
    q = Q[:, c*B:(c+1)*B]; k = K[:, c*B:(c+1)*B]
    scores += q @ k.T
print(torch.allclose(scores, ref))    # 预期 True
```

**需要观察的现象 / 预期结果**：上面 `allclose` 应为 `True`，证明「D 切片累加」与「整 D 一次乘」数值等价。

**待本地验证**：上述片段为纯 CPU 张量运算，可在本机直接跑；但若要在真实 kernel 里观察，需 GPU 且 D=512 的 prefill 用例（Nq≥512），属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 L407 的 `scores = tl.zeros(...)` 误删，让它沿用上一 KV 块的值，会发生什么？

**参考答案**：`scores` 会累加上一个 KV 块的残值，相当于把不同 KV 列块的 score 混在一起相加，attention 彻底错乱。所以「每个 KV 块清零、D 片段内累加」的嵌套层次不能搞反：外层（KV 块）清零，内层（D 片段）累加。

**练习 2**：为什么 `tl.dot(q, tl.trans(k), acc=scores)` 用了 `tl.trans(k)`？

**参考答案**：K 在内存里按 `[N, D]` 存（行是 KV 序列、列是 D），而 QK^T 需要的是 K 的转置 `[D, N]`。`tl.trans(k)` 在寄存器里把 `[N, B_QK]` 的窄片段转成 `[B_QK, N]` 参与乘法，避免在全局内存里另存一份转置。

---

### 4.3 Phase 3：按 V-group 累加 P@V_slice

#### 4.3.1 概念说明

在算 \(\mathrm{O}=PV\) 时，D 是**输出维**（O 的列）。Split-D 把 D 切成 `NUM_V_GROUPS` 段，每段对应 O 的一组不同列，因此**不能相加**，而是给每个 V-group \(g\) 配一个独立累加器 \(\mathrm{R}_g\)：

\[ \mathrm{R}_g \leftarrow \alpha\,\mathrm{R}_g + P\,V_{[:,\,d_g]},\qquad g=0,\dots,\mathrm{NUM\_V\_GROUPS}-1 \]

其中 \(\alpha=\exp(m_i-m_{\mathrm{new}})\) 是 online softmax 的逐行重缩放因子（详见 [u4-l1](u4-l1-triton-fwd-online-softmax.md)），`[:, None]` 把它广播到每个 V-group 的所有 D 列上。

**核心技巧（省算力的关键）**：同一块 softmax 概率 `P [BLOCK_M, BLOCK_N]` 被**所有 V-group 复用**。因为 P 只依赖于 score（QK^T），与输出的 D 列无关；所以 QK 和 softmax 只算一次，然后「同一块 P」依次乘上每个 V 片段。源码注释把这条写成 CUDA 那边的同名公式 `R_D[j] = alpha * R_D[j] + P @ V_j`。

如果**不**做 Split-D，而是 naïvely 对每个输出 D 列组重新算一遍 QK+softmax，计算量会翻 `NUM_V_GROUPS` 倍。Split-D 的「P 复用」避免了这种重复。

#### 4.3.2 核心流程

单个 KV 块（固定 `start_n`）、softmax 算完拿到 `p` 之后：

```
# p: [BLOCK_M, BLOCK_N] 已经过 dropout + cast，全 V-group 复用
for g in range(NUM_V_GROUPS):              # D 输出循环
    v = load V[列块, g*B_V : (g+1)*B_V]    # SRAM，[BLOCK_N, B_V]，一次一片
    o_accs[g] = o_accs[g] * alpha[:,None] + p @ v   # 各 g 独立累加
# 收尾（Phase 4）：O[:, d_g] = o_accs[g] / (l_i + 1e-10)
```

对比 4.2.2：Phase 1 是「内层累加进同一块」，Phase 3 是「内层写到不同累加器」。两种循环结构镜像，但语义因 D 的身份不同而相反。

#### 4.3.3 源码精读

**Phase 3 主体**（本模块最核心的一小段）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L464-L476](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L464-L476) —— 中文逐行说明：

- L464–L467：注释点明这是「Phase 3: PV with Split-D V accumulation」，并写出 FFPA CUDA fwd 的同名公式 `R_D[j] = alpha * R_D[j] + P @ V_j`，说明「同一块 P 复用到所有 V 片段，避免按输出 head-dim 组重算 QK/softmax」。
- L468：`for v_group in tl.static_range(0, NUM_V_GROUPS):` —— 用 `static_range` 让编译器**展开**循环（因为 `NUM_V_GROUPS` 是 constexpr），每个 V-group 生成独立的累加代码。
- L469：算当前 V-group 的 D 偏移 `o_d = B_V * v_group + offs_d_v`。
- L470–L474：加载 V 的窄片段 `[BLOCK_N, B_V]`，带越界 mask。
- L475：`o_acc = o_accs[v_group] * alpha[:, None] + tl.dot(p, v)` —— **关键**：先对旧累加器做 online-softmax 重缩放（`* alpha[:, None]` 把逐行因子广播到所有 D 列），再加上本次 `P @ V_g`。
- L476：`o_accs = _update_o_accs(o_accs, v_group, o_acc)` —— 把更新后的累加器写回元组的第 `v_group` 位。

**累加器元组更新辅助函数**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L75-L77](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L75-L77) —— Triton 里元组不可变，所以靠「切片 + 拼接」把第 `v_group` 个元素替换掉，返回新元组。这正是「每个 V-group 独立累加器」在代码层面的落点。

**Phase 4 收尾归一**：

[src/ffpa_attn/triton/_ffpa_fwd.py:L480-L489](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L480-L489) —— KV 主循环结束后，对每个 V-group 把累加器除以 \((l_i+10^{-10})\) 得到最终 O 切片，并写回 `O`；LSE 写成 \(m_i+\log(l_i)\)。注意除法是对**整个 V-group 切片**做的，所以归一化正确地作用到了 O 的所有 D 列。

#### 4.3.4 代码实践

**目标**：验证「同一块 P 复用给所有 V-group」与「整 D 一次 PV」数值等价。

**操作步骤**（**示例代码**，非项目原有，纯 CPU 可跑）：

```python
import torch
M, N, D, B = 4, 4, 512, 64
P = torch.softmax(torch.randn(M, N), dim=-1)   # 模拟 softmax 后的 P
V = torch.randn(N, D)
ref = P @ V                                     # 整 D 一次 PV
G = D // B
o_accs = [torch.zeros(M, B) for _ in range(G)]
alpha = torch.ones(M)                           # 假设无历史，alpha=1
for g in range(G):
    v = V[:, g*B:(g+1)*B]
    o_accs[g] = o_accs[g] * alpha[:, None] + P @ v
out = torch.cat(o_accs, dim=1)
print(torch.allclose(out, ref))                 # 预期 True
```

**需要观察的现象 / 预期结果**：`allclose` 为 `True`，证明「按 V-group 累加 + P 复用」等价于「整 D 一次 PV」。同时可见 `P` 在循环里只算了一次、被所有 `g` 复用。

**待本地验证**：在真实 kernel 中观察需 GPU 与大 D prefill 用例。

#### 4.3.5 小练习与答案

**练习 1**：L475 里为什么是 `alpha[:, None]` 而不是 `alpha`？

**参考答案**：`alpha` 形状是 `[BLOCK_M]`（每行一个重缩放因子），而 `o_accs[v_group]` 形状是 `[BLOCK_M, B_V]`。`alpha[:, None]` 把它变成 `[BLOCK_M, 1]`，在 D 列方向广播，保证「同一行的所有 D 列用同一个 alpha」——这正是 online softmax 按行重缩放的要求。

**练习 2**：如果不做「P 复用」，而是对每个 V-group 都重新算一遍 QK 和 softmax，计算量会增加多少？

**参考答案**：会增加约 `NUM_V_GROUPS` 倍的 QK 与 softmax 工作量。对 D=512、tile=64，`NUM_V_GROUPS=8`，即 QK/softmax 部分会重复 8 次。Split-D 通过把 QK/softmax 提到 V-group 循环之外、让所有 V-group 共享同一块 P，把这部分成本摊平到 1 次。

---

## 5. 综合实践

**任务**：把本讲两个核心循环合并成一份完整伪代码，画出单个 program 的数据流，并标注每个张量驻留在 SRAM 还是寄存器。这是本讲规格指定的代码实践任务。

**操作步骤**：

1. 阅读完整的 KV 主循环 [L403-L478](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L403-L478)，对照下面的伪代码补全。
2. 在伪代码每一行标注张量驻留位置（SRAM / 寄存器）。
3. 用一张表总结「哪些随 D 增长、哪些不随 D 增长」。

**参考伪代码**（标注了驻留位置）：

```
# 一个 program 处理: Q 行块 start_m × (batch, query head) off_hb
# 常量: BLOCK_M(行), BLOCK_N(KV列), B = BLOCK_HEADDIM_QK = BLOCK_HEADDIM_V
#       G = NUM_V_GROUPS = cdiv(D, B),  C = num_qk_d_chunks = cdiv(D, B)

# ── 全程驻留寄存器（随 D 增长的只有 o_accs）──
m_i  = -inf               # [BLOCK_M]  寄存器  (不随 D)
l_i  = 0                  # [BLOCK_M]  寄存器  (不随 D)
o_accs = [zeros[BLOCK_M, B] for _ in range(G)]   # 寄存器, G 个  (随 D 线性增长)

for start_n in range(0, Nkv, BLOCK_N):           # ── KV 主循环 ──

    # ===== Phase 1: QK^T 跨 D 片段归约（D = 归约维 → 相加）=====
    scores = zeros[BLOCK_M, BLOCK_N]             # SRAM/寄存器  (不随 D)
    for c in range(C):
        q = load Q[start_m*M, c*B : (c+1)*B]     # SRAM [M, B]  (不随 D, 一次一片)
        k = load K[start_n*N, c*B : (c+1)*B]     # SRAM [N, B]  (不随 D, 一次一片)
        scores += q @ k.T
    scores = scores*scale; += bias; mask; causal

    # ===== Phase 2: online softmax（详见 u4-l1）=====
    m_new = max(m_i, rowmax(scores)); alpha = exp(m_i - m_new)
    p = exp(scores - m_new)                      # [M, N] SRAM/寄存器 (不随 D)
    l_i = l_i*alpha + rowsum(p)
    p = dropout(p); p = cast(p)                  # 这一块 p 被下面所有 V-group 复用

    # ===== Phase 3: 按 V-group 累加 P@V（D = 输出维 → 各 g 独立）=====
    for g in range(G):
        v = load V[start_n*N, g*B : (g+1)*B]     # SRAM [N, B]  (不随 D, 一次一片)
        o_accs[g] = o_accs[g]*alpha[:,None] + p @ v
    m_i = m_new

# ===== Phase 4: 收尾归一 =====
for g in range(G):
    O[start_m*M, g*B:(g+1)*B] = o_accs[g] / (l_i + 1e-10)
LSE = m_i + log(l_i)
```

**需要观察的现象 / 预期结果**：填写下面这张「驻留与 D 的关系」表：

| 张量 | 形状 | 驻留位置 | 是否随 D 增长 |
|---|---|---|---|
| `scores` | `[BLOCK_M, BLOCK_N]` | SRAM / 寄存器 | **否** |
| `q`、`k` 切片 | `[M, B]` / `[N, B]` | SRAM | **否**（一次一片，循环覆盖） |
| `v` 切片 | `[N, B]` | SRAM | **否**（一次一片，循环覆盖） |
| `p` | `[BLOCK_M, BLOCK_N]` | SRAM / 寄存器 | **否** |
| `m_i`、`l_i` | `[BLOCK_M]` | 寄存器 | **否** |
| `o_accs` | `G × [BLOCK_M, B]` | 寄存器 | **是**（G = D/B） |

**结论**：SRAM 工作集完全由 `BLOCK_M / BLOCK_N / B` 这些固定 tile 决定，**与 D 无关**（O(1) in D）；只有寄存器里的 `o_accs` 随 D 线性增长（O(D) 个 fp32 累加器）。这就是 [u1-l1](u1-l1-what-is-ffpa-split-d.md) 所述「Split-D 把 D 方向压力从 SRAM 转移到寄存器」在 Triton 实现里的具象体现。

**可选的端到端验证（待本地验证，需 GPU）**：构造 `B=1, H=32, Nq=Nkv=8192, D=512` 的 bf16 张量，调用 `ffpa_attn_func`，并与 `F.scaled_dot_product_attention` 对比 `max_abs_err`（参考 [u2-l1](u2-l1-ffpa-attn-func-signature-layout.md) 的示例）。预期二者数值接近，从而间接确认 Split-D 的两个循环正确实现了完整 attention。本步骤在无 GPU 环境下属「待本地验证」。

## 6. 本讲小结

- **D 有两种身份**：在 QK^T 里是归约维（可拆分相加），在 PV 里是输出维（只能各写各的）。Split-D 对两种身份用了两种不同的切法。
- **两个计数**：`num_qk_d_chunks = cdiv(D, B_QK)` 控制 Phase 1 的归约循环；`NUM_V_GROUPS = cdiv(D, B_V)` 控制 Phase 3 的累加器个数；二者因 tile lockstep 而数值相等，但语义相反。
- **Phase 1（[L407-L422](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L407-L422)）**：开一块全零 `scores`，对每个 D 片段加载窄 Q/K，用 `tl.dot(q, trans(k), acc=scores)` **累加进同一块**，SRAM 一次只驻留一片 D。
- **Phase 3（[L464-L476](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L464-L476)）**：每个 V-group 配独立累加器 `o_accs[g]`，用公式 `R_g = α·R_g + P@V_g` 累加；**同一块 P 被所有 V-group 复用**，省下重算 QK/softmax 的开销。
- **复杂度结论**：SRAM 工作集与 D 无关（O(1) in D），寄存器随 D 线性增长（`o_accs` 有 `NUM_V_GROUPS` 个）。这是 Split-D 相对标准 FA tiling 的本质区别，也是 FFPA 能支持 D=320/512/1024 的原因。
- **tile 粒度**：Triton 实现的 tile 候选是 64/128（256 仅当 headdim==256），与 [u1-l1](u1-l1-what-is-ffpa-split-d.md) 概念层的「16 宽 MMA fragment」是「封装 vs 基本粒度」的关系。

## 7. 下一步学习建议

- **[u4-l3](u4-l3-decode-fwd-split-kv.md) Decode 前向：split-KV 两阶段**：当 Nq 很小（如解码）时，沿 KV 再切 chunk 走 stage1/stage2；那里的 stage1 kernel 同样用了 `num_qk_d_chunks` 与 `NUM_V_GROUPS` 两个循环，可以对照本讲确认 Split-D 在 decode 路径里是一致的。
- **[u4-l4](u4-l4-fwd-features-gqa-mask-dropout.md) 前向特性**：GQA 头映射、attn_bias 零 stride 广播、causal 掩码、dropout Philox 重放，都是在 Phase 1/3 这两个循环「周围」生效的特性，读完本讲再看会更有体感。
- **延伸阅读（专家层）**：[u7-l1](u7-l1-cuda-fwd-kernel-architecture.md) 与 [u7-l2](u7-l2-cuda-sram-register-swizzle-persist.md) 讲手写 CUDA 后端，那里的 `R_D` 寄存器正是本讲 `o_accs` 的 CUDA 对应物（注释 [L393-L395](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L393-L395) 已点明），可以对照理解 O(d/4) 寄存器复杂度中 1/4 折扣的由来。
