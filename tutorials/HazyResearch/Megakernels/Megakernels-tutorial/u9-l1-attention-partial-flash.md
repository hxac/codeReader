# attention_partial：单 warp flash attention

## 1. 本讲目标

本讲带读者逐行精读 `attention_partial.cu`，理解 Megakernels 如何用**单个 warp**完成一次「分块 flash attention」计算。学完后你应该能够：

1. 说清 `attention_partial` 这个 op 在整条推理流水线中的位置（它是注意力三段式 op 的中间一环），以及它产出什么、谁来消费它的输出。
2. 看懂 Q/K/V 的 tile 类型定义，理解为什么大量 tile 都标注「only 4 rows/values are used」，以及 `NUM_STAGES = 3` 的三级 KV 双缓冲是如何组织的。
3. **手推在线 softmax（online softmax）的每一步**：在新一块 KV 到来时，running max、`O_reg`（分子部分和）与 `norm_vec`（分母部分和）分别被如何 rescale，以及最终 LSE（log-sum-exp）是如何拼出来的。
4. 理解 `store_4_rows` 这个手写函数如何把 16 行寄存器 tile 中「真正用到的 4 行」抽取并写回共享内存，以及 LSE 如何跨 4 个 lane 写到全局。

本讲是整个手册里最「数值密集」的一讲。我们会先用直觉和数学把在线 softmax 讲透，再回到源码逐行对照。

---

## 2. 前置知识

### 2.1 为什么要 flash attention / 在线 softmax

朴素 attention 的公式是：

\[
O = \operatorname{softmax}\!\left(\frac{QK^{\top}}{\sqrt{D}}\right)V
\]

如果先把整个 \(N\times N\) 的注意力矩阵 \(P=\operatorname{softmax}(QK^\top/\sqrt D)\) 算出来再乘 \(V\)，会占用 \(O(N^2)\) 显存。flash attention 的核心技巧是：**分块（tiling）+ 在线 softmax**，让我们在只持有当前一块 KV 的情况下，逐步累积分子的部分和 \(O\) 与分母的部分和 \(\ell\)，最终一次性归一化得到正确结果，全程不实例化完整的注意力矩阵。

> 在本讲的低延迟推理场景里，\(N\)（序列长度）其实不大，分块是为了**让 K/V 从 HBM 流式进入共享内存并隐藏访存延迟**，而不是为了省显存。但数学是一模一样的。

### 2.2 base-2 softmax：为什么是 exp2 / log2

GPU 的 `exp2`/`log2`（以 2 为底）比 `expf`/`logf`（以 e 为底）快得多，硬件上有专门通路。本讲把 softmax 整体改写到 base-2：

由 \(e^x = 2^{x/\ln 2} = 2^{x\cdot \log_2 e}\)，令 \(\tau = \frac{1}{\sqrt{D}\cdot\ln 2}=\frac{\log_2 e}{\sqrt D}\)。先把原始得分 \(s=QK^\top\) 预缩放为 \(\tilde s = s\cdot \tau\)，那么 \(e^{s/\sqrt D}=2^{\tilde s}\)，之后所有指数运算都可用 `exp2` 完成。源码里这个 \(\tau\) 就是 `softmax_temp`（见下文第 4.2 节）。

最终存出去的 LSE 也是以 2 为底的：

\[
\mathrm{LSE}=\log_2\!\Big(\sum_j e^{s_j/\sqrt D}\Big)=\log_2\!\Big(\sum_j 2^{\tilde s_j}\Big)
\]

下游的 `attention_reduction` op 用同一个 base-2 约定来合并多个 partial，天然自洽。

### 2.3 本讲需要的 kittens 概念速查

| 类型前缀 | 含义 | 本讲用法 |
|---|---|---|
| `rt_fl<R,C>` / `rt_bf<R,C>` | **寄存器 tile**（float / bf16），由单个 warp 的 32 个线程共同持有，标准尺寸 16×16 | Q/K/V/O/注意力矩阵都在寄存器里 |
| `st_bf<R,C>` | **共享内存 tile**（bf16） | K/V 在 SMEM 里做三级缓冲 |
| `sv_fl<N>` / `sv_bf<N>` | **共享内存向量** | O（每头一行）、LSE（每头一个 float） |
| `col_vec<rt>` | 寄存器 tile 的**列向量视图**（每行一个值） | running max、norm、L 的逐头标量 |

寄存器 tile 的尺寸约束很关键：一个 warp 天然能装下「16 行 × 16 的倍数列」的 tile。本架构 `HEAD_DIM=64`、`KV_BLOCK_SIZE=16`，所以 `rt_fl<16,64>`、`rt_fl<16,16>` 都正好是「一个 warp 一个 tile/几个 tile」的工整尺寸。

> 还需要知道 Megakernels 的 op 五子结构：`controller / loader / launcher / consumer / storer`。本讲聚焦 `consumer`（真正算 attention 的单 warp）和 `storer`（把结果写回），`launcher` 负责 TMA 拉 K/V。如果你对五子结构还不熟，请先看 **u8-l1（op 接口）** 与 **u8-l4（rms_qkv_rope_append 实战 op）**。动态信号量与 phase bit 的双缓冲同步机制请复习 **u7-l2**。

---

## 3. 本讲源码地图

唯一的核心源码文件是 [demos/low-latency-llama/attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu)。它内部按 op 五子结构组织，下表给出每个区块在本讲中的角色：

| 行范围 | 区块 | 作用 |
|---|---|---|
| 6–37 | 模板参数与 tile 类型别名 | 定义 `NUM_STAGES`、`GQA_RATIO`、Q/K/V/O/attn/max/norm/L 的 tile 类型 |
| 39–53 | `parsed_instruction` | 从 32 个 int 指令里解析 `layer_idx / kv_head_idx / num_partials / partial_idx` |
| 56–78 | 信号量 getter | 为 Q/O/L 与每级的 K/V 各分配 arrived/finished 信号量 |
| 94–120 | SMEM getter | Q/O/L 共用 QOL_PAGE，K/V 三级缓冲共用 KV_PAGE |
| 122–209 | `store_4_rows` | 把 16 行 O 寄存器 tile 里某一组 4 行写进共享内存向量 |
| 248–274 | `load_Q_async` | 单 warp 异步加载 4 行 Q（GQA 专用） |
| 283–295 | `controller::init_semaphores` | 初始化全部动态信号量，返回占用槽位数 |
| 307–386 | `launcher` | 用 TMA 把 K/V 三级流水地拉进 SMEM |
| 387–548 | `consumer` | **单 warp 在线 softmax 主循环**（本讲重点） |
| 549–674 | `storer` | 把 O（bf16）与 LSE 写回全局，更新 `Bar` 同步计数 |

架构常量来自 [demos/low-latency-llama/llama.cuh:18-21](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L18-L21)：`HEAD_DIM=64`、`NUM_ATTENTION_HEADS=32`、`NUM_KV_HEADS=8`、`KV_BLOCK_SIZE=16`。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：

- **4.1 Q/K/V tile 类型与 NUM_STAGES 双缓冲**
- **4.2 在线 softmax 循环（本讲核心）**
- **4.3 store_4_rows 与 LSE 写回**

### 4.1 Q/K/V tile 类型与 NUM_STAGES 双缓冲

#### 4.1.1 概念说明

`attention_partial` 处理**一个 KV head 对应的 4 个 query head**（GQA group-query attention）。Llama-1B 有 32 个 query head、8 个 KV head，所以每个 KV head 服务 \(32/8=4\) 个 query head，这正是代码里的 `GQA_RATIO`。

这就解释了源码里反复出现的「only 4 rows/values are used」注释：kittens 的寄存器 tile 最小行数是 16，但这里逻辑上只需要 4 行（4 个 query head）。代码用一个 16 行的 tile，只填/只读其中某一组 4 行，其余行是「空着不用」的 padding。这样既复用了工整的 16×N mma（张量核心）指令，又不用为 4 行单独写一套 tile。

KV 则按 `KV_BLOCK_SIZE=16` 分块，用 `NUM_STAGES=3` 级缓冲做软件流水：consumer 在算第 \(i\) 块时，launcher 可以并行地把第 \(i+1\)、\(i+2\) 块通过 TMA 拉进 SMEM。

#### 4.1.2 核心流程

```
QOL_PAGE (page 0):  [ Q_smem(16x64) | O_smem[4](每条64) | L_smem(16) ]
KV_PAGE  (page 1):  [ K0 | V0 | K1 | V1 | K2 | V2 ]   ← NUM_STAGES=3 级
```

- Q、O、L 都属于「每 query head 一行/一个标量」，逻辑上只有 4 份，但放在 16 宽的向量里。
- K、V 各 3 份，按 `stage = block_idx % 3` 轮转复用。每级配两个信号量（arrived + finished）做生产者-消费者同步。

#### 4.1.3 源码精读

模板常量与 GQA 断言（说明整个 op 被写死在 GQA_RATIO=4 的 Llama-1B 上）：

[demos/low-latency-llama/attention_partial.cu:8-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L8-L15) — 定义 `NUM_STAGES=3`、`GQA_RATIO`，并断言 `GQA_RATIO==4`、`NUM_STAGES<=4`。后者的含义见下文「小练习」。

Q/K/V/attn/max/norm/L/O 的 tile 类型（注意大量「only 4 ... used」）：

[demos/low-latency-llama/attention_partial.cu:17-37](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L17-L37) — 逐条说明：

- `q_rt/q_st`：16×64 的 Q，只用 4 行（4 个 query head）。
- `k_rt/v_rt`：`KV_BLOCK_SIZE(16)×HEAD_DIM(64)`，K 用默认布局、V 用 `col_l`（列主序，便于 `mma_AB`）。
- `attn_fl_rt/attn_bf_rt`：16×16 的注意力矩阵（4 query × 16 key），float 版用于 softmax 数学，bf16 版喂给 `mma_AB`。
- `max_vec_rv` / `norm_vec_rv` / `l_rv`：都是 `col_vec<rt_fl<16,64>>`，即 16 行的逐头标量（running max / 分母 / LSE），只用 4 个。
- `o_rt`：16×64 的输出累加器（float），只用 4 行；`o_sv`/`o_sv_bf` 是写回 SMEM/全局用的共享向量。

三级 KV 缓冲在 SMEM 里的布局：

[demos/low-latency-llama/attention_partial.cu:109-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L109-L120) — K 与 V 在 KV_PAGE 里交错排列：`K_stage = base + sizeof(kv_st)*(stage*2)`，`V_stage = base + sizeof(kv_st)*(1 + stage*2)`。3 级共 6 个 16×64 的 bf16 tile。

O 与 L 在 QOL_PAGE 里的布局：

[demos/low-latency-llama/attention_partial.cu:98-108](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L98-L108) — `O_smem[4]` 是 4 条 `sv_fl<64>`（每个 query head 一条长度 64 的向量），紧跟在 `q_st` 后面；`L_smem` 是 `sv_fl<16>`，紧跟在 4 条 O 后面。

三级流水同步靠的信号量布局：

[demos/low-latency-llama/attention_partial.cu:56-78](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L56-L78) — `Q_arrived/O_arrived/L_arrived` 占前 3 个槽；每级 K/V 各占 `arrived/finished` 两个槽。`K_arrived(stage)=slot[3+stage*2]`、`V_arrived(stage)=slot[3+stage*2+1]`、`K_finished/V_finished` 排在更后面（`3 + NUM_STAGES*2` 起）。共 \(3+4\cdot\text{NUM\_STAGES}=15\) 个信号量，远小于框架上限 32。

信号量初始化与计数：

[demos/low-latency-llama/attention_partial.cu:283-295](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L283-L295) — `init_semaphores` 对每个信号量调用 `init_semaphore(..., 0, 1)`（初值 0、上限 1 的二值信号量），返回 `3 + 4*NUM_STAGES`。

launcher 的三级 TMA 流水（生产者侧）：

[demos/low-latency-llama/attention_partial.cu:357-383](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L357-L383) — 按 `stage = cur_blk_idx % NUM_STAGES` 轮转，先 `wait(K_finished/V_finished)` 等 consumer 释放缓冲，再用 `tma::load_async` 把 K/V 拉进对应 stage 的 SMEM，并以 `K_arrived/V_arrived` 通知 consumer。`EVICT_FIRST` 提示 L2 不要缓存这些一次性的 KV（避免污染缓存）。

#### 4.1.4 代码实践

**实践目标**：亲手算清「KV_PAGE 要装得下 NUM_STAGES 级缓冲」这个约束，理解 `static_assert(NUM_STAGES<=4)` 的来源。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 [attention_partial.cu:109-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L109-L120)，确认 KV_PAGE 内放了 `2*NUM_STAGES` 个 `kv_st`。
2. `kv_st = st_bf<16,64>`，一个占 \(16\times64\times2=2048\) 字节。
3. 一个 page 的容量由框架的 page 大小决定（见 u5-l3 / u5-l1）。`NUM_STAGES=3` 时需要 \(6\times2048=12288\) 字节；若 `NUM_STAGES=4` 则需 \(8\times2048=16384\) 字节。
4. 查框架的 page 大小常量，确认 4 级是 page 能装的上限——这就是 `static_assert(NUM_STAGES<=4, "Modify page allocation for KVs.")` 的来历。

**预期结果**：能说清「再想加第 5 级缓冲就必须改 page 分配策略，否则会越界」。

> 待本地验证：page 的确切字节数需结合 `config`（u5-l1）的 `PAGE_*` 常量确认；本仓库未在 `attention_partial.cu` 里直接写出，建议读者在 `include/` 下定位 page 大小定义后核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Q tile 用 16 行却只填 4 行？

**参考答案**：因为 kittens 的寄存器 tile 最小粒度是「一个 warp = 16 行 × 16 列」，4 行无法单独成一个 tile；用 16 行 tile 复用标准 mma 指令，只在逻辑上关心某一组 4 行（对应本 KV head 的 4 个 query head），其余行是 padding。

**练习 2**：`NUM_STAGES=3` 意味着 launcher 最多能领先 consumer 多少块？

**参考答案**：3 级缓冲意味着 launcher 最多领先 consumer 3 块——consumer 在算 stage 0 时，launcher 可在 stage 1、2 上灌数据。注意 stage 是 `block_idx % 3` 轮转的，所以领先量受缓冲级数约束。

---

### 4.2 在线 softmax 循环

#### 4.2.1 概念说明

这是本讲的核心。我们要在**单 warp**里，对每一块 KV 维护三个逐 query head 的「累加状态」：

- running max \(m\)（以及它的缩放版 \(m_s = m\cdot\tau\)），
- 分子部分和 \(O=\sum_j 2^{\tilde s_j - m_s} V_j\)（一个 4×64 的寄存器 tile `O_reg`），
- 分母部分和 \(\ell=\sum_j 2^{\tilde s_j - m_s}\)（一个逐头标量向量 `norm_vec_reg`）。

每来一块 KV，先算这一块的得分、更新全局 max，再把旧 \(O\) 和旧 \(\ell\) **按新/旧 max 的差 rescale**，最后把这一块的贡献累加进去。所有块跑完后，\(O/\ell\) 就是归一化的注意力输出，\(m_s+\log_2\ell\) 就是这一 partial 的 LSE。

#### 4.2.2 核心流程：rescale 的数学

设当前已累积的状态用的是旧 max \(m_{s,\text{old}}\)，新块到来后合并出的新 max 是 \(m_{s,\text{new}}\)（取旧 max 与本块 max 的更大者）。定义 rescale 因子：

\[
r = 2^{\,m_{s,\text{old}} - m_{s,\text{new}}}
\]

因为新 max 一定不小于旧 max，所以 \(r\le 1\)。更新规则为：

\[
O \leftarrow r\cdot O + \sum_{j\in\text{new}} 2^{\tilde s_j - m_{s,\text{new}}} V_j,
\qquad
\ell \leftarrow r\cdot \ell + \sum_{j\in\text{new}} 2^{\tilde s_j - m_{s,\text{new}}}
\]

跑完所有块后：

\[
O_{\text{out}} = \frac{O}{\ell},\qquad
\mathrm{LSE} = m_{s,\text{last}} + \log_2 \ell
\]

为什么 LSE 这么算？因为 \(2^{m_s}\ell=\sum_j 2^{\tilde s_j}=\sum_j e^{s_j/\sqrt D}\)，所以 \(\log_2(2^{m_s}\ell)=m_s+\log_2\ell=\log_2\sum_j e^{s_j/\sqrt D}\)，正是 base-2 的 LSE。

#### 4.2.3 源码精读

先看 consumer 主循环之前的初始化（设置缩放常数与各累加器初值）：

[demos/low-latency-llama/attention_partial.cu:421-440](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L421-L440) — `softmax_temp = g.attn_scale * 1.44269504089f`，即 \(\tau=\frac{1}{\sqrt D\ln 2}\)（注释 `1 / (sqrt(D_h) * ln(2))`）。`max_vec_reg` 初始化为 \(-\infty\)、`norm_vec_reg`/`O_reg` 初始化为 0、`last_scaled_max_vec_reg` 初始化为 0（「just not +-inf」，保证第一块的 rescale 因子 \(2^{0-m_s}\) 不出 NaN）。

主循环逐块处理（这里把 16 个关键步骤串起来）：

[demos/low-latency-llama/attention_partial.cu:463-520](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L463-L520) — 下面把这几十行对应到数学：

1. **算得分** [469-475](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L469-L475)：`attn_fl_reg = Q_reg @ K_reg^T`（`mma_ABt`），得到这一块 4×16 的原始得分。`wait(K_arrived)` 等 launcher 把这一级 K 灌好；算完 `arrive(K_finished)` 让 launcher 可复用该缓冲。

2. **掩码越界位置** [478-482](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L478-L482)：最后一块可能不满 16 个 key，用 `right_fill` 把无效列填成 `-999999999999.f`（实质 \(-\infty\)），保证它们 softmax 后权重为 0。

3. **更新 running max** [485-486](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L485-L486)：`row_max(max_vec_reg, attn, max_vec_reg)` 把本块 max 与历史 max 合并，得到**未缩放**的新 max \(m\)。注意此时 `attn_fl_reg` 还没乘 `softmax_temp`。

4. **整体缩放到 base-2** [489-490](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L489-L490)：`attn_fl_reg *= softmax_temp`（\(\tilde s = s\tau\)），`scaled_max_vec_reg = max_vec_reg * softmax_temp`（\(m_s = m\tau\)）。

5. **算本块的 softmax 权重（分子）** [493-494](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L493-L494)：`attn_fl_reg = exp2(attn_fl_reg - scaled_max_vec_reg)`，得到 \(2^{\tilde s_j - m_s}\)，即 \(P\)。

6. **算 rescale 因子** [497-500](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L497-L500)：`diff_scaled_max_vec_reg = last_scaled_max_vec_reg - scaled_max_vec_reg`，再 `exp2` 得到 \(r=2^{m_{s,\text{old}}-m_{s,\text{new}}}\)。

7. **rescale 旧分子并累加新块（A@V）** [503-511](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L503-L511)：`O_reg *= r`；`wait(V_arrived)` 拿到 V；把 `attn_fl_reg` 转成 bf16 后 `mma_AB(O_reg, attn_bf_reg, V_reg, O_reg)` 完成 \(O \mathrel{+}= P@V\)；`arrive(V_finished)`。

8. **rescale 旧分母并累加新块** [514-516](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L514-L516)：`norm_vec_reg *= r`，再 `row_sum(norm_vec_reg, attn_fl_reg, norm_vec_reg)` 把本块权重逐行求和加进去，得到新的 \(\ell\)。

9. **保存当前 max 给下轮** [519](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L519)：`copy(last_scaled_max_vec_reg, scaled_max_vec_reg)`，下一轮的「旧 max」就是本轮的「新 max」。

收尾：归一化输出并算 LSE：

[demos/low-latency-llama/attention_partial.cu:525-536](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L525-L536) — `O_reg /= norm_vec_reg`（\(O/\ell\)）；`L_reg = log2(norm_vec_reg)`；`L_reg += last_scaled_max_vec_reg`，得到 LSE \(=m_s+\log_2\ell\)。边界情况（该 partial 一块都没处理，`start_blk_idx>=end_blk_idx`）则把 L 设成 \(-\infty\)（[535](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L535)），保证下游 reduction 时这个空 partial 不贡献权重。

> 注意顺序细节：先 rescale 旧 \(O\)（步骤 7 的乘法）**再**做 `A@V` 累加；分母同理先 rescale 再 `row_sum`。这个顺序对应 4.2.2 节的公式——旧的累加值用旧 max 归一，必须先乘 \(r\) 调到新 max 基准下，才能与本块（已经在新 max 基准下）相加。

#### 4.2.4 代码实践

**实践目标**：手推一个小例子，验证在线 softmax 每一步对 `O_reg` 与 `norm_vec_reg` 的 rescale，吃透步骤 6–8。

**操作步骤**（纸笔推演型，建议两人对照）：

1. 假设某个 query head 只看 2 个 key，分成 2 个 partial 块，每块 1 个 key。设缩放后得分 \(\tilde s_1=1\)、\(\tilde s_2=3\)（即第 2 个 key 更重要），对应 \(V_1, V_2\) 是两个 64 维向量。
2. **块 1**：\(m_s=1\)；\(P_1=2^{1-1}=1\)；\(O=1\cdot V_1=V_1\)；\(\ell=1\)；`last_scaled_max` \(=1\)。
3. **块 2**：新得分 3，\(m_{s,\text{new}}=\max(1,3)=3\)；rescale 因子 \(r=2^{1-3}=2^{-2}=0.25\)。
   - rescale 旧值：\(O\leftarrow 0.25 V_1\)，\(\ell\leftarrow 0.25\)。
   - 本块 \(P_2=2^{3-3}=1\)，累加：\(O\leftarrow 0.25V_1+1\cdot V_2\)，\(\ell\leftarrow 0.25+1=1.25\)。
4. 归一化：\(O_{\text{out}}=(0.25V_1+V_2)/1.25=0.2V_1+0.8V_2\)。

**需要观察的现象**：验证这个结果与「一次性 softmax」一致。一次性算：\(\text{softmax}([1,3])=[2^1,2^3]/(2^1+2^3)=[2,8]/10=[0.2,0.8]\)，权重正是 \(0.2 V_1+0.8 V_2\)。两者完全相同 → 在线 softmax 正确。

**预期结果**：能说清「rescale 因子 \(r=2^{m_{s,\text{old}}-m_{s,\text{new}}}\) 把旧累加值从旧 max 基准搬到新 max 基准，所以新旧可以相加」，并能指出代码里对应步骤 6（[497-500](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L497-L500)）、步骤 7（[503](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L503)）、步骤 8（[514](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L514)）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `row_max`（步骤 3）在乘 `softmax_temp` **之前**做，而不是之后？

**参考答案**：max 对正缩放因子 \(\tau>0\) 是可交换的：\(\max(s)\cdot\tau=\max(s\cdot\tau)\)。所以先用未缩放得分更新 running max，再统一乘 \(\tau\) 得到 \(m_s\)，数学等价但少一次缩放。注意 \(\tau\) 必须恒正，否则会破坏 max 的单调性——这里 \(\tau=\frac{1}{\sqrt D\ln 2}>0\)，满足。

**练习 2**：若把步骤 7 的 `mul_row(O_reg, O_reg, diff_scaled_max_vec_reg)`（rescale 旧分子）删掉，只留 `mma_AB` 累加，结果会怎样？

**参考答案**：旧 \(O\) 仍停留在旧 max 基准（\(2^{m_{s,\text{old}}}\) 量级），而本块 \(P@V\) 在新 max 基准（\(2^{m_{s,\text{new}}}\) 量级）。两者基准不一致直接相加，等价于给旧块权重多乘了 \(2^{m_{s,\text{new}}-m_{s,\text{old}}}\) 倍，输出会偏向旧块、且最终 \(O/\ell\) 不再等于正确 softmax。分母同理会失配。

**练习 3**：第一块（\(i=0\)）时 `last_scaled_max_vec_reg` 是 0，步骤 6 算出的 \(r=2^{0-m_s}\)。这个 \(r\) 会污染第一块的 `O_reg`/`norm_vec_reg` 吗？

**参考答案**：不会。因为第一块进入循环前 `O_reg=0`、`norm_vec_reg=0`，乘以任何 \(r\) 仍是 0，步骤 7/8 的 rescale 对零无影响，之后才真正累加 \(P@V\) 与 \(P\)。这正是初始化把这两个累加器清零、并把 `last_scaled_max_vec_reg` 初始化为「一个有限值（0）而非 ±inf」的原因（避免 \(2^{\pm\infty-\cdot}\) 产生 NaN/inf）。

---

### 4.3 store_4_rows 与 LSE 写回

#### 4.3.1 概念说明

主循环算完之后，`O_reg` 是一个 16×64 的 float 寄存器 tile，但只有「本 KV head 对应的那 4 行」是有意义的。`store_4_rows` 这个手写函数负责把这 4 行抽出来、转换精度，写进共享内存里 4 条 `o_sv<64>` 向量（每个 query head 一条）。LSE 则是 4 个 float 标量，由 storer 跨 4 个 lane 写到全局。

`store_4_rows` 看起来很「黑魔法」，本质是因为 kittens 的 16 行寄存器 tile 在 32 个线程里的排布是**固定的 swizzle 模式**：lane `0~15` 持有第 0~7 行的某些片段、lane `16~31` 持有第 8~15 行的某些片段，且每 4 个 lane 一组对应一行。函数就是按这个 swizzle 规律，让每个 lane 直接用 `sts`（shared store）指令把自己的寄存器碎片写到目标 SMEM 地址。

#### 4.3.2 核心流程

```
consumer 结束:
  store_4_rows(O_smem, O_reg, q_head_local_idx)   # 抽取第 q_head_local_idx 组 4 行 → O_smem[0..3]
  arrive(O_arrived)                                # 通知 storer：O 就绪
  store(L_smem, L_reg)                             # LSE(16个float,只4个有效) → L_smem
  arrive(L_arrived)                                # 通知 storer：L 就绪

storer:
  wait(O_arrived) → 把 O_smem[0..3] (float→bf16) TMA store 到全局 attn_out(_intermediates)
  wait(L_arrived) → lane0..3 各取一个 LSE float → 全局 attn_lse_intermediates
```

`q_head_local_idx` 决定写 16 行 tile 的哪一组 4 行：`q_head_start_idx % 16 / 4`（见 [410-411](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L410-L411)），取值 0/1/2/3，分别对应第 0~3、4~7、8~11、12~15 行。

#### 4.3.3 源码精读

`store_4_rows` 的签名与 swizzle 映射：

[demos/low-latency-llama/attention_partial.cu:122-146](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L122-L146) — 关键三行：

- `dst_ptr[k]` = 第 k 条目标 SMEM 向量的首地址（用 `__cvta_generic_to_shared` 转成 SMEM 地址）。
- `local_row_idx = (laneid % 16) / 4`：每 4 个 lane 一组，对应组内第 0~3 行——这正是「4 行」的来源。
- `local_col_idx = laneid % 4`：组内列索引。

`store_4_rows` 的分支选择（哪 4 行 / 哪一半 warp）：

[demos/low-latency-llama/attention_partial.cu:148-208](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L148-L208) — `row4idx % 2` 与 `laneid<16` 组合，从 16 行 tile 里挑出目标那 4 行（0~3 / 4~7 / 8~11 / 12~15），并从该行的寄存器碎片（`src.tiles[0][j].data[0/1/2/3]`）逐列写回。每写一个片段，先 `convertor<U2,T2>::convert` 把 float 降精度到目标类型（bf16），再用 `move<U2>::sts` 做 16 字节共享内存存储。注意 `data[2]` 而非 `data[1]`（[155](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L155) 等处的注释 `// note 2, not 1`）正是 kittens swizzle 布局的体现。

consumer 末尾调用 `store_4_rows` 与存 LSE：

[demos/low-latency-llama/attention_partial.cu:538-545](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L538-L545) — `store_4_rows(O_smem, O_reg, q_head_local_idx)` 写 O；`arrive(O_arrived)`；`store(L_smem, L_reg)` 写 LSE；`arrive(L_arrived)`。

storer 把 O 从 float 转 bf16 再 TMA 写全局（`skip_attn_reduction` 分支，单 partial 直接写最终输出）：

[demos/low-latency-llama/attention_partial.cu:551-581](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L551-L581) — `store_o_skip`：先 `wait(O_arrived)`，把每条 `O_smem`（float）load 成 `rv_bf`、再 store 回同址（bf16，原地精度转换），最后 `tma::store_async` 写到 `g.attn_out`。

`store_o_no_skip`（多 partial 分支，写中间结果供 reduction 合并）：

[demos/low-latency-llama/attention_partial.cu:583-600](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L583-L600) — 把 4 条 float O 直接 TMA 写到 `g.attn_out_intermediates[0, q_head_start_idx+head_offset, partial_idx, 0]`。

LSE 跨 4 lane 写全局：

[demos/low-latency-llama/attention_partial.cu:618-640](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L618-L640) — `laneid < GQA_RATIO`（0~3）时，每个 lane 用内联 PTX `ld.shared.f32` 从 `L_smem.data[q_head_vec_start_idx + laneid]` 读一个 LSE，再用 `st.global.f32` 写到 `attn_lse_intermediates[(q_head_start_idx+laneid)*cols + partial_idx]`。注释解释了「没法用花哨的向量写法写 4 个分散的值，所以逐 lane 标量写」。

收尾：等 TMA 完成、释放 QOL_PAGE、更新 `Bar` 同步计数：

[demos/low-latency-llama/attention_partial.cu:641-672](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L641-L672) — `tma::store_async_wait()` 确保 O 落盘；`finish_QOL_page` 释放页；最后对 `g.Bar[{layer, opcode-1, q_head_start_idx+laneid}]` 做 `atomicAdd(...,1)`，通知下游 `attention_reduction`：本 KV head 的 4 个 head 都已产出。

#### 4.3.4 代码实践

**实践目标**：弄清 `q_head_local_idx` / `q_head_vec_start_idx` 这两个「4 行选择器」如何随 `kv_head_idx` 变化，从而理解 `store_4_rows` 与 LSE 写回的「分散寻址」。

**操作步骤**（源码阅读 + 手算）：

1. 读 [attention_partial.cu:605-607](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L605-L607)：`q_head_start_idx = kv_head_idx * 4`（取值 0,4,8,…,28），`q_head_vec_start_idx = q_head_start_idx % 16`。
2. 读 [attention_partial.cu:410-411](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L410-L411)：`q_head_local_idx = (q_head_start_idx % 16) / 4`。
3. 列表填入 8 个 KV head：

| kv_head_idx | q_head_start_idx | q_head_vec_start_idx | q_head_local_idx（写 O 的哪组 4 行） |
|---|---|---|---|
| 0 | 0 | 0 | 0（行 0~3） |
| 1 | 4 | 4 | 1（行 4~7） |
| 2 | 8 | 8 | 2（行 8~11） |
| 3 | 12 | 12 | 3（行 12~15） |
| 4 | 16 | 0 | 0（行 0~3，下一轮 tile） |
| … | … | … | … |

**需要观察的现象**：`q_head_local_idx` 只有 0~3 四个值，对应 16 行 tile 的四组；而 `q_head_vec_start_idx` 在 LSE 写回时给出 SMEM/全局里的起始偏移。

**预期结果**：能解释为什么 8 个 KV head 可以复用同一个 16 行 tile（因为一次只算一个 KV head 的 4 行），以及 LSE 的 4 个 float 为何落在 `L_smem.data` 的 `q_head_vec_start_idx` 位置、又如何被 lane 0~3 散写到全局对应 head。

#### 4.3.5 小练习与答案

**练习 1**：`store_4_rows` 为什么用 `__cvta_generic_to_shared` + `move<U2>::sts`，而不用 kittens 的 `warp::store`？

**参考答案**：因为要写的目标不是规则 tile，而是「4 条分散的、按行排布的共享向量」，且要从 16 行 tile 里精确挑出某 4 行的寄存器碎片。通用 `warp::store` 是按整 tile 布局写的，无法表达这种「挑 4 行 + 跨向量」的非规则写法；直接用 PTX 级 `sts`（16 字节共享存储）按 swizzle 手动寻址，才能零拷贝、无中间缓冲地把碎片落到正确地址。

**练习 2**：`store_4_rows` 里 `static_assert(SV::length == src.cols)`（[126-127](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L126-L127)）保证了什么？

**参考答案**：保证目标向量长度（如 `o_sv<64>` 的 64）等于源 tile 的列数（`o_rt` 的 `HEAD_DIM=64`）。也就是说，把「一行 64 维」整条写进一条长度 64 的向量，没有截断或越界。

**练习 3**：为什么 LSE 写回用 lane 0~3 标量写（[618-640](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L618-L640)），而不是 4 个 lane 一起做一次向量 store？

**参考答案**：4 个 query head 的 LSE 在全局里落在 `attn_lse_intermediates` 的不同行（`(q_head_start_idx+laneid)*cols + partial_idx`），地址步长是 `cols()`（一整行），不连续，无法用一次连续向量 store 完成。注释 [622-624](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L622-L624) 也说明「4 个分散值没法花哨地写」，所以每个 lane 独立 `st.global.f32`。

---

## 5. 综合实践

**任务：从指令到 LSE，完整跟踪一次 `attention_partial` 的执行轨迹。**

设当前 `seq_len=40`，即 `total_attn_blocks = ceil(40/16) = 3` 个 KV 块；设 `num_partials=1`、`partial_idx=0`（单 partial，会走 `skip_attn_reduction` 分支）。请完成：

1. **块划分**：用 [attention_partial.cu:340-348](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L340-L348) 的公式算出 `blocks_per_partial`、`start_blk_idx`、`end_blk_idx`，确认本 partial 处理块 0、1、2。
2. **掩码定位**：最后一块（块 2）只有 `40 % 16 = 8` 个有效 key，定位 [478-482](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L478-L482) 的 `right_fill` 把哪些列（列 8~15）填成 \(-\infty\)。
3. **三级缓冲轮转**：列表给出块 0/1/2 各落在哪个 `stage`（0/1/2），指出 consumer 在算块 0 时 launcher 能在哪几个 stage 上灌数据。
4. **softmax 推演**：取一个 query head，假设三块缩放后得分 row-max 分别给出 \(m_s\) 序列 \(m_0, m_1, m_2\)（自己设数，例如 1→2→3），按 4.2.4 的方法逐块写出 `O_reg`、`norm_vec_reg` 的 rescale 与累加过程，最后算出 \(O/\ell\) 与 LSE。
5. **写回路径**：说明本例因 `skip_attn_reduction=true`，O 走 [551-581](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L551-L581) 的 `store_o_skip` 直接写到 `g.attn_out`（而非 intermediates），且 storer 末尾 `atomicAdd` 的 `Bar` 键是 `{layer, OPCODE_AttentionReduction-1, 0}`（[658-660](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L658-L660)）。

**验收标准**：能画出「块 i → stage i%3 → wait(K_arrived) → Q@K.T → 更新 max → rescale O/ℓ → A@V → arrive(K/V_finished)」的时序图，并能解释为什么最后一块的掩码不会污染前两块的结果（因为前两块根本没有触发 `right_fill` 分支）。

> 待本地验证：第 4 步的具体数值可写成一个小 CUDA 或 Python 参考脚本对照（非本仓库自带 demo，属示例代码），重点是流程正确而非数值精度。

---

## 6. 本讲小结

- `attention_partial` 是注意力三段式 op（QKV 投影 → **partial attention** → attention reduction）的中间一环，由**单个 warp**（`warpid()==0`）跑分块 flash attention，把序列切成若干 KV 块流式处理。
- tile 类型大量标注「only 4 rows/values are used」，根因是 GQA：每个 KV head 服务 `GQA_RATIO=32/8=4` 个 query head，复用 16 行标准 tile、只用其中一组 4 行。`static_assert(GQA_RATIO==4)` 把整个 op 写死在 Llama-1B 的 4:1 GQA 上。
- KV 用 `NUM_STAGES=3` 级 SMEM 缓冲做软件流水，`stage = block_idx % 3` 轮转；每级配 `arrived/finished` 两个二值信号量协调 launcher 与 consumer（共 \(3+4\times3=15\) 个动态信号量）。
- 在线 softmax 的精髓是 rescale 因子 \(r=2^{m_{s,\text{old}}-m_{s,\text{new}}}\)：每来一块，先把旧 \(O\) 和旧 \(\ell\) 乘 \(r\) 搬到新 max 基准，再累加本块贡献。全程用 `exp2/log2`（base-2），`softmax_temp=1/(\sqrt D\ln2)` 把 e 底 softmax 改写成 2 底。
- 最终 \(O_{\text{out}}=O/\ell\)、\(\mathrm{LSE}=m_s+\log_2\ell\)（base-2 LSE）；空 partial 把 LSE 置 \(-\infty\) 使其在 reduction 中零权重。
- `store_4_rows` 是为「从 16 行 tile 抽 4 行写分散向量」量身手写的 PTX 级存储，依赖 kittens 的固定 swizzle lane 映射；LSE 则由 storer 的 lane 0~3 逐标量散写到全局 `attn_lse_intermediates`。

---

## 7. 下一步学习建议

1. **`attention_reduction`**：本讲产出的多个 partial 的 O 与 LSE 如何合并？关键就是用 base-2 LSE 做加权：\(\mathrm{merge}(O_a,O_b)=\frac{2^{\mathrm{LSE}_a}O_a+2^{\mathrm{LSE}_b}O_b}{2^{\mathrm{LSE}_a}+2^{\mathrm{LSE}_b}}\)。建议在 `demos/low-latency-llama/` 找到 reduction op 源码，验证它确实用 `exp2(LSE - LSE_max)` 做 rescale，与本讲的 base-2 约定自洽。
2. **`rms_qkv_rope_append`（u8-l4）回顾**：本讲消费者开头 `wait` 的 `g.Bar[{layer, OPCODE_RMS_QKV_MatVecRopeAppend-1, ...}]` 计数（[398-405](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L398-L405)）正是上一步 QKV op 产出的「Q/K/V 就绪」信号；读 u8-l4 把这条依赖链补全。
3. **kittens 原语**：若对 `mma_ABt / mma_AB / row_max / row_sum / col_vec` 还想更深入，可去 `kittens` 库的 register-tile 与 warp 命名空间实现里看这些原语如何映射到 `mma`/`redux` 等 PTX 指令。
4. **`skip_attn_reduction` 两条路径**：当序列短到只需 1 个 partial 时走 `store_o_skip` 直接出最终结果，省掉一次 reduction；建议对照 `globals_t::skip_attn_reduction` 的设置时机（生成脚本 / host 侧），理解这条「短序列快速路径」的触发条件。
