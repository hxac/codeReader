# Triton 前向 kernel 与 online softmax 主循环

> 本讲是「Triton 后端 — 前向」单元的第一讲，对应讲义 `u4-l1`，依赖 `u3-l4`（`_FFPAAttnFunc` 的前向/反向分发）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清默认 **Triton 前向 kernel** `_ffpa_fwd_kernel_impl` 的**网格（grid）到数据**的映射：为什么「一个 program 拥有一个 Q 行块 × 一个 (batch, head)」。
- 写出 **online softmax** 在每个 KV 块上的 `m_i / l_i / alpha` 更新公式，并解释为什么最后要 `o_accs / (l_i + eps)` 归一、LSE 写成 `m_i + log(l_i)`。
- 跟着宿主端启动器 `_ffpa_attn_forward_generic_impl` 走一遍：grid 函数怎么算、`autotune` / 持久化配置 / 默认 config 三条路径如何选。
- 把 kernel 内部「Q 块 → K/V 块 → score → P → O 累加」的单 program 数据流画出来。

本讲**只讲通用前向路径（generic path）**，即 `num_splits == 1`、一个 program 跑完整 KV 序列的那条路。解码（decode）的两阶段 split-KV 路径留给 `u4-l3`，Split-D 的更深入拆分留给 `u4-l2`。

## 2. 前置知识

本讲默认你已经掌握以下概念（在 `u1`/`u2`/`u3` 已建立）：

- **head_dim(D)、SDPA、FlashAttention-2、Split-D、SRAM/寄存器**（`u1-l1`）：FFPA 主攻大 D（320~1024），Split-D 在 MMA 层把 D 切成宽 16 的片段，使 SRAM 复杂度降到 \(O(1)\)。
- **`ffpa_attn_func` 的 `[B, Nh, N, D]` 布局与 GQA/group_size**（`u2-l1`/`u2-l4`）：`group_size = Nh_q / Nh_kv`，`key/value` 共享相同的 `Nh_kv` 与 `Nkv`。
- **`_FFPAAttnFunc.forward` 的分发**（`u3-l4`）：前向先用 `head_dim` 判大小 D（D≤256 走 aten），大 D 再按 `forward_meta` 在 cuda/triton/cutedsl 三路里选；选到 triton 后会进入本讲分析的 kernel。
- **torch.library 注册的 `torch.ops.ffpa_attn._fwd_triton`**（`u3-l5`）：Python 侧 `_ffpa_attn_forward_triton` 通过这个自定义算子把调用送进真实现 `_ffpa_attn_forward_impl`。

还需要一点**在线 softmax（online softmax）**的直觉。朴素 softmax 是 \(\mathrm{softmax}(x)_i = e^{x_i}/\sum_j e^{x_j}\)，需要先遍历一遍求分母 \(\sum_j e^{x_j}\)，再遍历一遍做归一。但注意力里 KV 是按块从全局内存**流式**读进来的，我们希望读一个块、算一个块、累加一个块，不想把整行 score 都存下来。online softmax 的关键技巧是：**边读边维护当前已见 score 的行最大值 \(m\) 和加权求和 \(l\)**，每来一个新块就用新的 \(m\) 把旧累加「重缩放（rescale）」到同一个基准下，从而在不回头的情况下得到与一次性 softmax 完全一致的结果。它的额外好处是**数值稳定**（永远减去行最大值再取 exp，不会溢出）。

## 3. 本讲源码地图

本讲几乎全部内容集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| `src/ffpa_attn/triton/_ffpa_fwd.py` | Triton 前向 kernel 的全部实现：通用 kernel、解码两阶段 kernel、autotune 配置生成器、宿主端启动器。 |

需要重点认识的几个符号（都在该文件内）：

- `_ffpa_fwd_kernel_impl`：**通用前向 kernel 本体**，本讲主角。
- `_ffpa_attn_forward_generic_impl`：**宿主端启动器**，负责算 grid、选 config、把张量与步长喂给 kernel。
- `_ffpa_attn_forward_impl`：更上层的**路径选择器**，根据 split-KV 占用启发式决定走 generic 还是 decode，generic 路径就调用本讲的启动器。
- `_ffpa_attn_forward_triton`：注册算子的 Python 入口，经 `torch.ops.ffpa_attn._fwd_triton` 调到 `_ffpa_attn_forward_impl`。

调用链（从 `u3-l5` 承接）：

```
ffpa_attn_func ──► _FFPAAttnFunc.forward ──► (Triton 后端)
   torch.ops.ffpa_attn._fwd_triton
      └─► _ffpa_attn_forward_triton          # 本文件 L1447
            └─► _ffpa_attn_forward_impl       # 本文件 L1319，选 generic / decode
                  ├─ num_splits==1 ─► _ffpa_attn_forward_generic_impl  # 本讲 L857
                  │                  └─► _ffpa_fwd_kernel_impl          # 本讲 L304
                  └─ 否则          ─► _ffpa_attn_forward_decode_impl   # u4-l3 再讲
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**(4.1)** kernel 的网格映射与状态初始化；**(4.2)** KV 主循环里的 online softmax 重缩放与 PV 累加（本讲心脏）；**(4.3)** 宿主端启动器与 config 选择。

### 4.1 _ffpa_fwd_kernel_impl：网格映射与单 program 状态

#### 4.1.1 概念说明

FlashAttention v2 的并行策略是「**一个 program 负责一个 Q 行块**」，FFPA 的通用前向 kernel 完全继承这一策略，并把它扩展到二维网格：

- **网格第 0 维 `program_id(0)`**：Q 的行块编号 `start_m`。每个 program 拿走连续 `BLOCK_M` 行 query。
- **网格第 1 维 `program_id(1)`**：把 batch 和 query 头「拍扁」成一个联合索引 `off_hb`，再拆回 `off_b = off_hb // nheads_q`（batch 号）和 `off_hq = off_hb % nheads_q`（query 头号）。

这样做的好处是：**不同 (batch, head)、不同 Q 行块之间互不通信**，天然可大规模并行；而单个 program 内部沿 KV 序列方向串行流式累加，用 online softmax 维护中间状态。GQA/MQA 不需要真的把 K/V 复制成 `Nh_q` 份，而是用 `off_hkv = off_hq // group_size` 直接换算出「这个 query 头实际该读哪个 KV 头」（承接 `u2-l4` 的连续分组约定）。

因为每个 program 要独立算完自己那段 query 的注意力并写回结果，它在循环开始前要准备好三组**逐行状态**：

- `m_i`：当前已见 score 的**行最大值**，初值 \(-\infty\)。
- `l_i`：当前已见 score 的（去最大值后的）**行求和**，初值 0。
- `o_accs`：**每个 V 头维度片段一个 fp32 累加器**，初值全 0；片段数 `NUM_V_GROUPS = cdiv(HEADDIM, BLOCK_HEADDIM_V)`。

#### 4.1.2 核心流程

单个 program 在进入 KV 主循环前的初始化流程：

```text
start_m = program_id(0)                 # Q 行块号
off_hb  = program_id(1)                 # batch×heads 联合索引
off_b   = off_hb // nheads_q            # 拆出 batch
off_hq  = off_hb %  nheads_q            # 拆出 query 头
off_hkv = off_hq // group_size          # GQA: 映射到 KV 头

把 Q/K/V/O/LSE/AttnBias 的基地址各自偏移到本 program 负责的 (b, h) 切片
初始化 m_i=-inf, l_i=0, o_accs=全0
按 IS_CAUSAL 计算本 program 的 KV 终点 end_n（跳过全被掩蔽的块）
```

其中的 `NUM_V_GROUPS`、`EVEN_M`、`EVEN_N` 不是手填的，而是由 `@triton.heuristics` 装饰器在编译期根据运行时参数推导出的编译期常量（`tl.constexpr`），这样循环次数和边界分支都能被编译器静态优化。

#### 4.1.3 源码精读

网格映射与 GQA 头映射——`program_id` 拆解与 `off_hkv` 换算：

[src/ffpa_attn/triton/_ffpa_fwd.py:L363-L370](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L363-L370) — 把二维 `program_id` 解析成 `start_m`（Q 行块）、`off_hb`（batch×头），再拆出 `off_b`/`off_hq`，并用 `off_hq // group_size` 得到 KV 头号 `off_hkv`。

基地址偏移——把每个张量的指针前进到本 (batch, head) 切片：

[src/ffpa_attn/triton/_ffpa_fwd.py:L372-L378](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L372-L378) — Q/O 按 query 头 `off_hq` 偏移，K/V 按 KV 头 `off_hkv` 偏移，LSE 按联合索引 `off_hb` 偏移；`AttnBias` 仅在启用时偏移。

行/列/D 偏移向量与 QK 的 D 分片数：

[src/ffpa_attn/triton/_ffpa_fwd.py:L380-L386](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L380-L386) — 准备 `offs_m`/`offs_n`/`offs_d_*` 索引，算出 `num_qk_d_chunks = cdiv(HEADDIM, BLOCK_HEADDIM_QK)`（Split-D 的 QK 分片数）和因果对齐量 `kv_offset = seqlen_k - seqlen_q`。

累加器初始化——`m_i`、`l_i` 与「每个 V 片段一个 O 累加器」：

[src/ffpa_attn/triton/_ffpa_fwd.py:L388-L396](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L388-L396) — `m_i` 初始化为 \(-\infty\)、`l_i` 为 0；`o_accs = (zero_acc,) * NUM_V_GROUPS` 是一个长度为 `NUM_V_GROUPS` 的元组，每个元素是独立的 `[BLOCK_M, BLOCK_HEADDIM_V]` fp32 累加器（注释说它对应 CUDA fwd 的 `R_D` 寄存器组）。

因果路径下提前收紧 KV 终点，避免空跑全掩蔽的块：

[src/ffpa_attn/triton/_ffpa_fwd.py:L398-L401](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L398-L401) — `IS_CAUSAL` 时令 `end_n = min(seqlen_k, (start_m+1)*BLOCK_M + kv_offset)`，让靠前的 Q 行块提前结束 KV 循环。

顺带看一眼「编译期常量是怎么算出来的」——heuristic 表：

[src/ffpa_attn/triton/_ffpa_fwd.py:L287-L294](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L287-L294) — `EVEN_M`/`EVEN_N` 表示序列长度是否被块大小整除，`NUM_V_GROUPS` 是 V 片段数；它们由 `@triton.heuristics(_FFPA_FWD_HEURISTICS)` 在编译期从运行时参数推导（见 [src/ffpa_attn/triton/_ffpa_fwd.py:L302-L303](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L302-L303)）。

#### 4.1.4 代码实践

**实践目标**：用一个具体形状，手工算出 grid 大小和某个 program 的指针偏移，验证「网格→数据」映射。

**操作步骤**（源码阅读 + 手算）：

1. 取 `docs/index.md` 的 self-attn 示例形状：`B=1, H=32, Nq=Nkv=8192, D=512`，假设 autotune 选了 `BLOCK_M=128`。
2. 读 [src/ffpa_attn/triton/_ffpa_fwd.py:L363-L370](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L363-L370) 与启动器 grid [src/ffpa_attn/triton/_ffpa_fwd.py:L913-L914](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L913-L914)。
3. 手算：`grid(0) = cdiv(8192, 128) = 64`，`grid(1) = 1 * 32 = 32`，总 program 数 = `64 × 32 = 2048`。
4. 取 `start_m=0, off_hb=0`：算出 `off_b=0, off_hq=0, off_hkv=0`；再取 `start_m=0, off_hb=5`：`off_b=0, off_hq=5`，GQA 下（此处 `group_size=1`）`off_hkv=5`。

**需要观察的现象**：grid 的第 1 维确实是「batch × query 头」的拍扁索引，`off_hq` 与 `off_hkv` 在 MHA（`group_size=1`）时相等，在 GQA 时才分开。

**预期结果**：总 program 数 2048；`off_hb=5` 对应 `(batch=0, head=5)`，MHA 下 K/V 读第 5 个头。

**待本地验证**：如果你把 `H` 换成 GQA（如 `Nh_q=32, Nh_kv=8`），用同样的手算确认 `off_hb=5 → off_hkv = 5 // 4 = 1`，即第 5 个 query 头共用第 1 个 KV 头。

#### 4.1.5 小练习与答案

**练习 1**：为什么网格第 1 维要把 batch 和头「拍扁」成 `off_hb`，而不是用三维网格 `(start_m, off_b, off_hq)`？

**参考答案**：拍扁后 grid 是二维 `(cdiv(Nq,BLOCK_M), B*Nh_q)`，每个 program 完全独立、互不通信；Triton 对二维网格的 launch 与 L2 命中更直接，也方便 tile scheduler / 持久化调度（见 `u6-l3`）。拆成三维在语义上等价，但会多一层索引换算且无额外并行收益。

**练习 2**：`m_i` 初值为什么必须是 \(-\infty\)，而 `l_i` 初值是 0？

**参考答案**：`m_i` 是「已见 score 的行最大值」，还没见到任何 score 时应是恒等元 \(-\infty\)（任何数都大于它），这样第一个块进来时 `m_new = max(-inf, rowmax(scores)) = rowmax(scores)`、`alpha = exp(-inf - m_new) = 0`，旧累加被清零，正确接管。`l_i` 是「求和」，恒等元是 0。

---

### 4.2 online softmax 更新段：KV 主循环里的重缩放与 PV 累加

#### 4.2.1 概念说明

这是整个前向 kernel 的心脏。KV 序列被切成若干 `BLOCK_N` 宽的块，program 顺序处理每一块。对每一块要做三件事，恰好对应源码里注释的 **Phase 1 / 2 / 3**：

- **Phase 1（Split-D 算 QKᵀ）**：当前 KV 块的 score 不是一次性 `Q @ Kᵀ` 算出来的，而是沿 head_dim 方向再切成 `BLOCK_HEADDIM_QK` 宽的片段，逐片段用 `tl.dot(q, kᵀ, acc=scores)` **累加**成完整的 `[BLOCK_M, BLOCK_N]` score。这就是 Split-D 在 QKᵀ 上的体现——SRAM 任意时刻只驻留 `BLOCK_HEADDIM_QK` 宽的 Q/K 切片，而不是整 D。
- **Phase 2（online softmax）**：用本块的 score 更新行最大值 `m_i`、行求和 `l_i`，并算出本块的注意力权重 `p = exp(scores - m_new)`；dropout 只作用在 `p` 上，LSE 保留**未 dropout** 的归一因子。
- **Phase 3（Split-D 算 PV）**：把同一个 `p` 复用到**每一个 V 头维度片段**上，分别累加进各自的 `o_accs[g]`，避免为每个输出 D 片段重算一遍 QK/softmax。

关键洞察：**QKᵀ 的 D 维是「先全部累加完再 softmax」，而 PV 的 D 维是「同一个 softmax 片段复用到多个 V 切片」**。前者保证 score 完整，后者复用 softmax 结果省算力——这正是 FFPA 相对朴素 Split-D 实现的省算子设计（源码注释点明它对齐 CUDA fwd 的 `R_D[j] = alpha*R_D[j] + P@V_j`）。

#### 4.2.2 核心流程

设当前 program 拥有 `BLOCK_M` 行 query，处理到第 j 个 KV 块。对每一行（下标 m）维护 \(m_i, l_i\) 与（每个 V 片段 g 的）\(O_{i,g}\)。第 j 块的 score 行向量记作 \(s_j \in \mathbb{R}^{\text{BLOCK\_N}}\)（已乘 `softmax_scale` 并施加 mask/bias）。online softmax 的块合并公式为：

\[
m_i^{(j)} = \max\!\bigl(m_i^{(j-1)},\ \max_n s_{j,n}\bigr)
\]

\[
\alpha = \exp\!\bigl(m_i^{(j-1)} - m_i^{(j)}\bigr)
\]

\[
p_n = \exp\!\bigl(s_{j,n} - m_i^{(j)}\bigr)
\]

\[
l_i^{(j)} = l_i^{(j-1)}\,\alpha + \sum_n p_n
\]

每个 V 片段 g 的输出累加（\(P\in\mathbb{R}^{\text{BLOCK\_M}\times\text{BLOCK\_N}}\)，\(V_g\in\mathbb{R}^{\text{BLOCK\_N}\times\text{BLOCK\_HEADDIM\_V}}\)）：

\[
O_{i,g}^{(j)} = O_{i,g}^{(j-1)}\,\alpha + P\,V_g
\]

处理完所有 KV 块后（Phase 4 收尾）：

\[
O_{i,g}^{\text{final}} = O_{i,g}^{(J)} \,/\, \bigl(l_i^{(J)} + \varepsilon\bigr), \qquad \varepsilon = 10^{-10}
\]

\[
\mathrm{LSE} = m_i^{(J)} + \log\!\bigl(l_i^{(J)}\bigr)
\]

**为什么最后除以 \(l_i\) 就等价于 \(\mathrm{softmax}(s)V\)？** 把所有块展开，\(l_i^{(J)}\) 恰好等于 \(\sum_n \exp(s_n - m_i^{(J)})\)（每一块的 \(p_n\) 都被重缩放到同一个最大值 \(m_i^{(J)}\) 下），而 \(O_{i,g}^{(J)}\) 等于 \(\sum_n \exp(s_n - m_i^{(J)})\,v_n\)。两者相除，分子分母的 \(\exp(s_n - m_i^{(J)})\) 抵消，得到 \(\sum_n \mathrm{softmax}(s)_n\,v_n\)。

**为什么 LSE 写成 \(m_i + \log(l_i)\)？** 因为 \(\sum_n \exp(s_n) = \exp(m_i)\sum_n \exp(s_n - m_i) = \exp(m_i)\,l_i\)，取对数即 \(m_i + \log(l_i)\)。源码 docstring 也明确声明保存的是自然对数约定 `lse = log(sum(exp(score)))`（[src/ffpa_attn/triton/_ffpa_fwd.py:L24-L26](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L24-L26)），这是反向 kernel 对齐粒度的前提。

#### 4.2.3 源码精读

**Phase 1：Split-D 算 QKᵀ**——沿 D 分片累加出完整 score：

[src/ffpa_attn/triton/_ffpa_fwd.py:L403-L424](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L403-L424) — KV 主循环 `for start_n in range(0, end_n, BLOCK_N)`；内层 `for qk_d_chunk` 逐片段加载 Q/K 并用 `tl.dot(q, tl.trans(k), acc=scores)` 把 D 维 Reduction 累加进 `scores`；循环结束后 `scores = scores * softmax_scale`。

施加 attn_bias / 边界 / 因果掩码（承接 `u2-l3` 的尾对齐因果）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L425-L440](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L425-L440) — 可加偏置 `bias` 用零 stride 广播加载（紧凑掩码不物化）；`EVEN_N` 为假时把越界列置 \(-\infty\)；`IS_CAUSAL` 时令 `offs_kv <= offs_m + kv_offset` 之外的单元置 \(-\infty\)。

**Phase 2：online softmax**——本讲的核心四行：

[src/ffpa_attn/triton/_ffpa_fwd.py:L442-L462](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L442-L462) — 依次算 `m_new = maximum(m_i, rowmax(scores))`、`alpha = exp(m_i - m_new)`、`p = exp(scores - m_new)`、`l_new = l_i*alpha + rowsum(p)`；随后 `_apply_dropout_to_p` 只改 `p`，`l_new` 保留未 dropout 的归一因子；最后 `p = p.to(DTYPE)` 降到 fp16/bf16 以匹配 V 的 dtype 做 MMA。

**Phase 3：Split-D 算 PV**——同一 `p` 复用到每个 V 片段：

[src/ffpa_attn/triton/_ffpa_fwd.py:L464-L478](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L464-L478) — `for v_group in tl.static_range(0, NUM_V_GROUPS)`：加载第 g 个 V 片段，`o_acc = o_accs[g] * alpha[:, None] + tl.dot(p, v)`，再用 `_update_o_accs` 把新累加器塞回元组；最后把 `m_i, l_i` 推进到 `m_new, l_new`，进入下一块。

辅助函数——元组式累加器更新（对应 CUDA 的逐寄存器组更新）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L75-L77](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L75-L77) — `_update_o_accs` 用元组拼接替换第 `v_group` 个累加器，保持 `NUM_V_GROUPS` 个独立 O 累加器互不干扰。

**Phase 4：收尾**——最终归一与 LSE 写回：

[src/ffpa_attn/triton/_ffpa_fwd.py:L480-L489](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L480-L489) — 对每个 V 片段 `out = o_accs[g] / (l_i[:, None] + 1e-10)`，降到 DTYPE 存入 O；并把 `m_i + log(l_i)` 存入 LSE（带越界 mask）。`1e-10` 防止全掩蔽行除零。

#### 4.2.4 代码实践

**实践目标**（本讲指定的代码实践）：在 `_ffpa_fwd_kernel_impl` 中定位 KV 块循环与 `rowmax`/`rowsum` 更新的代码行，画出单 program 的数据流。

**操作步骤**：

1. 打开 [src/ffpa_attn/triton/_ffpa_fwd.py:L403-L489](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L403-L489)。
2. 标出四段：Phase 1（L407~L424）、Phase 2（L442~L462）、Phase 3（L464~L478）、Phase 4（L480~L489）。
3. 在 Phase 2 里精确圈出三行：`m_new`（L446，含 `rowmax`）、`alpha`（L447）、`l_new`（L449，含 `rowsum`）。
4. 用下面的数据流图把单 program 的张量流转画出来。

**单 program 数据流（一个 KV 块的迭代）**：

```text
  ┌──────────── Phase 1: Split-D 算 QKᵀ（D 维分片累加）─────────────┐
  │ for qk_d_chunk:                                                │
  │   Q_c [BLOCK_M, BD_QK] ─┐                                      │
  │   K_c [BLOCK_N, BD_QK] ─┤─► tl.dot(Q_c, K_cᵀ, acc= ─► scores  │
  │                         │      [BLOCK_M, BLOCK_N]              │
  │  （跨所有 D 片段累加完整 score）                                 │
  └────────────────────────────┬───────────────────────────────────┘
                               ▼
            scores *= softmax_scale; (+attn_bias / 边界 / causal 掩码)
                               │
  ┌──────────────── Phase 2: online softmax（逐行重缩放）──────────┐
  │ m_new = max(m_i, rowmax(scores))        ← L446               │
  │ alpha = exp(m_i - m_new)                ← L447               │
  │ p     = exp(scores - m_new)             ← L448               │
  │ l_new = l_i * alpha + rowsum(p)         ← L449               │
  │ (dropout 只作用在 p；LSE 保留未 dropout 归一因子)               │
  └────────────────────────────┬───────────────────────────────────┘
                               ▼
  ┌──────────────── Phase 3: Split-D 算 PV（复用 p）──────────────┐
  │ for v_group:                                                  │
  │   V_g [BLOCK_N, BD_V] ──► o_acc[g] = o_acc[g]*alpha + p@V_g  │
  │                         （每个 V 片段一个累加器，L475）         │
  │ m_i, l_i ← m_new, l_new   （进入下一个 KV 块）                │
  └────────────────────────────┬───────────────────────────────────┘
                               ▼  （所有 KV 块结束后）
  ┌──────────────── Phase 4: 收尾 ───────────────────────────────┐
  │ out[g] = o_acc[g] / (l_i + 1e-10)  → 存 O      （L483~L488）  │
  │ LSE    = m_i + log(l_i)            → 存 LSE     （L489）      │
  └──────────────────────────────────────────────────────────────┘
```

**需要观察的现象**：

- `scores` 在 Phase 1 内是**被累加的对象**（`acc=scores`），D 维 Reduction 发生在此；进入 Phase 2 后 score 已是完整的 `[BLOCK_M, BLOCK_N]`。
- `p` 在 Phase 2 算**一次**，在 Phase 3 被**复用 NUM_V_GROUPS 次**——这就是「PV 复用 softmax」的省算力来源。
- `alpha` 同时用于 `l_new`（标量乘）和 `o_acc`（按列广播 `alpha[:, None]`），二者必须用**同一个**重缩放因子，否则输出与分母不在同一基准下。

**预期结果**：你能用一句话说清「QKᵀ 的 D 维先累加完、PV 的 D 维靠复用 p 分组累加」这一不对称设计。

**待本地验证**：把 `BLOCK_HEADDIM_QK`/`BLOCK_HEADDIM_V` 都设为 `D`（即 `NUM_V_GROUPS=1`、`num_qk_d_chunks=1`）时，本结构退化为标准 FlashAttention 的单片段 tiling——可在 autotune 日志里观察这种 full-D config 是否出现（仅高 SMEM 设备，见 `u8-l1`）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 Phase 2 里的 `alpha = exp(m_i - m_new)` 误写成 `alpha = exp(m_new - m_i)`，会发生什么？

**参考答案**：符号反了。正确公式里旧累加器要从「旧最大值基准 \(m_i\)」搬到「新最大值基准 \(m_new\)」，因子应是 \(\exp(m_i - m_{\text{new}})\)（因为旧累加里隐含了 \(\exp(\cdot - m_i)\)，要再乘 \(\exp(m_i - m_{\text{new}})\) 才能换到 \(m_{\text{new}}\) 基准）。写反会得到 \(\exp(m_{\text{new}} - m_i)\)，导致旧累加被放大而非缩小，数值爆炸，输出 NaN/Inf。

**练习 2**：为什么 `l_new` 用的是**未 dropout 的** `p`，而 O 累加用的是**dropout 后的** `p`？

**参考答案**：LSE 要保存的是「真注意力的归一因子」\(\sum_n \exp(s_n)\)，供反向重放使用，因此不能含 dropout；而前向输出 O 必须是 dropout 后的期望输出（被保留下来的元素乘 \(1/(1-p_{\text{drop}})\) 补偿），所以 O 累加用 dropout 后的 `p`。源码注释（L442~L445）明确说明了这一约定。

**练习 3**：Phase 4 里 `1e-10` 的作用是什么？去掉它会怎样？

**参考答案**：防止「整行被完全掩蔽」（如因果掩蔽下某些 query 行看不到任何 key）时 `l_i` 为 0 导致除零，产生 NaN。去掉后，全掩蔽行会输出 NaN 并污染反向。这是一个纯防御性的数值兜底。

---

### 4.3 _ffpa_attn_forward_generic_impl：启动器与 config 选择

#### 4.3.1 概念说明

kernel 本体（4.1/4.2）只管「一个 program 怎么算」；真正决定「**启多少个 program、喂什么 config**」的是宿主端启动器 `_ffpa_attn_forward_generic_impl`。它做四件事：

1. **解析形状与默认值**：从 `q/k` 读出 `batch, nheads_q, seqlen_q, headdim`，算默认 `softmax_scale = 1/sqrt(D)`、LSE 存储的「对齐长度」`seqlen_q_rounded`、autotune 用的 seqlen 桶 key。
2. **算 grid**：`grid = (cdiv(seqlen_q, BLOCK_M), batch * nheads_q)`——正是 4.1 讲的二维映射。
3. **选 config**：三条路径二选一——
   - `autotune=True`：用 `_get_fwd_autotune(...)` 拿到一个 `triton.autotune` 包装版，由 Triton 现场跑候选 config 选最优（结果按 `(headdim, mode, dtype)` 缓存）。
   - `autotune=False`：先查持久化配置 `lookup_persistent_config`（见 `u8-l3`），命中就用它的 config；没命中就用硬编码默认 config。
4. **启动 kernel**：把张量、步长、运行时标量、编译期常量一长串传给 `_ffpa_fwd[grid](...)`。

注意 `_ffpa_fwd = _ffpa_fwd_kernel_impl`（[src/ffpa_attn/triton/_ffpa_fwd.py:L1286-L1286](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1286-L1286)）——启动器调用的 `_ffpa_fwd` 就是 4.1 分析的 kernel 本体。

#### 4.3.2 核心流程

```text
q,k,v,o,lse,attn_bias, causal, softmax_scale, autotune, autotune_mode, dropout_* 进
├─ batch,nheads_q,seqlen_q,headdim = q.shape
├─ softmax_scale 默认 1/sqrt(D)
├─ seqlen_q_rounded = lse.shape[-1]              # LSE 对齐存储长度
├─ autotune seqlen 桶 key（fast/max 分桶）
├─ DTYPE = fp16 / bf16
├─ bias_strides = _attn_bias_broadcast_strides(...)  # 紧凑掩码 → 零 stride
├─ grid(meta) = (cdiv(seqlen_q, meta['BLOCK_M']), batch*nheads_q)
├─ if autotune:    _get_fwd_autotune(...)[grid](...)
│  else:
│      cfg = lookup_persistent_config(...) or {BLOCK_M=128, BLOCK_N=64, ...}
│      _ffpa_fwd[grid](..., **cfg)
└─ （o、lse 原地写回，无返回）
```

启动器之上还有一层**路径选择器** `_ffpa_attn_forward_impl`：它先用 `_get_decode_num_splits(...)` 算 split-KV 切分数，`num_splits == 1` 才调用本讲的 generic 启动器，否则走 decode 路径（`u4-l3`）。这一层决定了「什么时候用本讲这条单 kernel 路径」。

#### 4.3.3 源码精读

形状解析、默认 scale、autotune 桶 key：

[src/ffpa_attn/triton/_ffpa_fwd.py:L898-L911](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L898-L911) — 读 `q/k` 形状，`softmax_scale` 缺省 `1/sqrt(headdim)`，`seqlen_q_rounded = lse.shape[-1]`，`autotune_seqlen_q_bucket`/`autotune_seqlen_k_bucket` 由 `autotune_seqlen_key` 分桶（细节见 `u8-l1`），并算出 `bias_strides`。

grid 函数——二维映射的宿主端写法：

[src/ffpa_attn/triton/_ffpa_fwd.py:L913-L914](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L913-L914) — `grid = (cdiv(seqlen_q, BLOCK_M), batch * nheads_q)`，与 4.1 的 `program_id(0/1)` 一一对应。

autotune 分支——现场选 config：

[src/ffpa_attn/triton/_ffpa_fwd.py:L916-L957](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L916-L957) — `autotune=True` 时用 `_get_fwd_autotune(headdim, mode, dtype)[grid](...)`，BLOCK_M/N 等由 autotuner 根据 key（seqlen 桶 + causal + headdim）选定。

持久化配置 / 默认 config 分支：

[src/ffpa_attn/triton/_ffpa_fwd.py:L959-L1025](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L959-L1025) — `autotune=False` 时先 `lookup_persistent_config`（命中即用），否则用默认 `{BLOCK_M:128, BLOCK_N:64, BLOCK_HEADDIM_QK:64, BLOCK_HEADDIM_V:64, num_warps:8, num_stages:3}`，然后 `_ffpa_fwd[grid](..., **launch_config)`。

更上层——路径选择器 `_ffpa_attn_forward_impl` 决定走 generic：

[src/ffpa_attn/triton/_ffpa_fwd.py:L1384-L1427](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1384-L1427) — 算 `num_splits`；`enable_tma` 且支持时优先试 SM90 TMA 路径；`num_splits == 1` 调本讲的 `_ffpa_attn_forward_generic_impl`，否则调 decode。

autotune 包装器与缓存（说明「调优开销每形状最多付一次」）：

[src/ffpa_attn/triton/_ffpa_fwd.py:L1291-L1316](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L1291-L1316) — `_get_fwd_autotune` 按 `(headdim, mode, dtype)` 缓存 `triton.autotune(...)(_ffpa_fwd_kernel_impl)`，`cache_results=True` 让同 key 的结果被缓存。

#### 4.3.4 代码实践

**实践目标**：手算 grid 与默认 config 下的总 program 数，理解 `BLOCK_M` 对并行度的影响。

**操作步骤**：

1. 形状仍取 `B=1, H=32, N=8192, D=512`，读 [src/ffpa_attn/triton/_ffpa_fwd.py:L913-L914](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L913-L914)。
2. 用默认 config `BLOCK_M=128` 算 grid：`grid = (64, 32)`，总 program `= 2048`。
3. 假设改成 `BLOCK_M=64`：`grid = (128, 32)`，总 program `= 4096`，每个 program 工作量减半、并行度翻倍但调度开销也变大。

**需要观察的现象**：`BLOCK_M` 越大，单 program 算的 query 行越多、program 总数越少、online softmax 状态向量越宽；这是 autotune 要在「并行度」与「单 program 工作量/寄存器压力」之间权衡的核心旋钮。

**预期结果**：默认 `BLOCK_M=128` 时总 program 数 2048；`BLOCK_M=64` 时 4096。

**待本地验证**：在 GPU 上分别用两种 `BLOCK_M`（可通过 `TritonBackend(autotune=True, autotune_mode='max')` 让 autotuner 实测）比较 kernel 耗时，观察哪个更快——结论随 SM 数与序列长度而变，需本地实测。

#### 4.3.5 小练习与答案

**练习 1**：`autotune=False` 时启动器为什么要先查 `lookup_persistent_config`，而不是直接用默认 config？

**参考答案**：默认 config 是「保守通用值」，未必对该形状最优。持久化配置是预先在目标硬件上跑过 autotune、按 direction/kernel/headdim/seqlen 等维度就近匹配存下来的最优 config（见 `u8-l3`）。先查它能在「不付运行时 autotune 开销」的前提下拿到接近最优的 config，兼顾性能与启动确定性。

**练习 2**：`grid` 函数为什么要写成 `lambda meta: (...)`、从 `meta['BLOCK_M']` 取值，而不是直接用一个固定的 `BLOCK_M` 常量？

**参考答案**：因为 `BLOCK_M` 是 autotuner/persistent config **选出来的**，启动器在构造 grid 时还不知道它的值。Triton 的 grid 函数接受 `meta` 字典，从中读取「被选中的 config 参数」，使 grid 大小随选中的 config 动态调整——这正是 autotune 能改变并行度的机制。

---

## 5. 综合实践

把本讲三块知识串起来：**用一个真实调用走完「宿主启动器 → grid → kernel 内部四阶段」的对应关系，并用 SDPA 验证数值正确性。**

**实践目标**：

1. 跑通一个 FFPA 大 D 前向，确认它确实走了本讲分析的通用路径（而非回退 SDPA）。
2. 把输出形状、program 总数、online softmax 的归一约定对上号。

**操作步骤**（依据 `docs/index.md` 的 self-attn 示例）：

```python
# 示例代码：复现 docs/index.md 的 self-attention 用例并对照本讲
import torch
import torch.nn.functional as F
from ffpa_attn import ffpa_attn_func

B, H, N, D = 1, 32, 8192, 512  # batch, heads, seqlen, head_dim
q = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
k = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")
v = torch.randn(B, H, N, D, dtype=torch.bfloat16, device="cuda")

out = ffpa_attn_func(q, k, v)          # -> (B, H, N, D) = (1, 32, 8192, 512)
ref = F.scaled_dot_product_attention(q, k, v)
print(out.shape, out.dtype)
print(f"vs SDPA max_abs_err={(out - ref).abs().max().item():.4e}")
```

> 上面是「示例代码」，主体来自 `docs/index.md` 的 self-attn 示例（[docs/index.md:L62-L79](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/index.md#L62-L79)）。

跑完后做三处对照：

1. **确认走 FFPA**：`D=512 > 256` 且 `N=8192 ≥ 512`，根据 `u1-l4`/`u3-l3` 的回退条件不会回退 SDPA，调用进入本讲的 Triton 通用前向。
2. **对上 grid**：默认 `BLOCK_M=128` 时，启动器算出 `grid=(64, 32)`，共 2048 个 program，每个 program 拥有 128 行 query、跑完整 8192 列 KV。
3. **对上 online softmax**：kernel 最终写出的 `out = o_accs / (l_i+eps)`、`LSE = m_i + log(l_i)`，与 SDPA 的 `softmax(scale·QKᵀ)V` 数值一致。

**需要观察的现象**：`out.shape == (1, 32, 8192, 512)`、`out.dtype == torch.bfloat16`；`max_abs_err` 为一个有限的小数。

**预期结果**：形状与 dtype 如上；`max_abs_err` 在 bf16、D=512 下通常落在 `1e-2 ~ 1e-1` 量级。

**待本地验证**：`max_abs_err` 的具体数值随硬件、PyTorch/SDPA 版本而变，需在本地 GPU 上实测确认。

> 本实践需要 NVIDIA GPU（sm≥80）与已安装的 `ffpa_attn`（`u1-l2`）。若无可用的 CUDA 环境，可退化为「源码阅读型实践」：只做 4.2.4 的数据流绘制与 grid 手算，跳过运行部分。

## 6. 本讲小结

- FFPA 的 Triton **通用前向**用 FlashAttention v2 风格的二维网格：`program_id(0)` 是 Q 行块、`program_id(1)` 是「batch×query 头」的拍扁索引，GQA 用 `off_hkv = off_hq // group_size` 直接换算 KV 头，无需复制 K/V。
- KV 主循环分四阶段：**Phase 1** 沿 D 分片累加出完整 score（Split-D 的 QKᵀ）；**Phase 2** 用 `m_new/alpha/p/l_new` 做 online softmax 的逐行重缩放；**Phase 3** 把同一个 `p` 复用到每个 V 片段累加（Split-D 的 PV，复用 softmax 省算力）；**Phase 4** 收尾 `out = o_accs/(l_i+eps)`、`LSE = m_i + log(l_i)`。
- online softmax 的核心是「每来一个 KV 块就用新最大值把旧累加重缩放到同一基准」，最后除以 `l_i` 恰好等价于 \(\mathrm{softmax}(s)V\)，且全程减最大值取 exp 保证数值稳定。
- LSE 保存的是自然对数约定 `log(sum(exp(score))) = m_i + log(l_i)`，dropout 只作用在 O 累加的 `p` 上、LSE 保留未 dropout 的归一因子，二者刻意分离。
- 宿主启动器 `_ffpa_attn_forward_generic_impl` 负责算 grid、在 `autotune` / 持久化配置 / 默认 config 三条路径里选 config，再调用 `_ffpa_fwd = _ffpa_fwd_kernel_impl`；更上层的 `_ffpa_attn_forward_impl` 用 split-KV 占用启发式决定何时走这条单 kernel 路径。

## 7. 下一步学习建议

- **`u4-l2` Split-D：大 head_dim 的 MMA 级精细分块**：把本讲的 `num_qk_d_chunks` 与 `NUM_V_GROUPS` 推到极致，讲清「QKᵀ 的 D 维先全累加、PV 的 D 维靠复用 p 分组累加」的不对称设计与 SRAM/寄存器复杂度的关系。
- **`u4-l3` Decode 前向：split-KV 两阶段**：本讲只覆盖 `num_splits==1` 的通用路径；当 `Nq` 很小、SM 占用不足时，`_ffpa_attn_forward_decode_impl` 会把 KV 切 chunk、stage1 算部分 O+局部 LSE、stage2 做 log-sum-exp 合并，去补回并行度。
- **`u4-l4` 前向特性**：GQA 头映射、`attn_bias` 零 stride 广播、causal 尾对齐、dropout Philox 重放在 kernel 内的实现细节（本讲只点到为止）。
- **`u8-l1` / `u8-l3`**：本讲启动器里的 `autotune_seqlen_key` 分桶、`lookup_persistent_config` 就近匹配的完整机制。
- 建议精读：[src/ffpa_attn/triton/_ffpa_fwd.py:L304-L489](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L304-L489)（kernel 本体）与 [src/ffpa_attn/triton/_ffpa_fwd.py:L857-L1025](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/src/ffpa_attn/triton/_ffpa_fwd.py#L857-L1025)（启动器）。
