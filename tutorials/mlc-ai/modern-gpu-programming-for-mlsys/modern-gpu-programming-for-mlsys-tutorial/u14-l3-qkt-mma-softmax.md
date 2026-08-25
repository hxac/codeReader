# QKᵀ MMA 与夹在中间的 softmax

## 1. 本讲目标

本讲是 Flash Attention 4 系列第三讲。u14-l1 建立了算法骨架（在线 softmax、tile 数据流），u14-l2 落实了 warp 角色与屏障协议；本讲沿着「QKᵀ MMA → softmax → PV MMA」这条计算链，深入其中间的**数值内容**。学完后你应该能够：

1. 说出 QKᵀ MMA 这条 tile 操作的 scope/layout/dispatch/交接屏障，以及 score tile `S` 的形状与去向。
2. 独立推导在线 softmax 的核心递推：\(\delta\)、`acc_scale`、`P`、`row_sum`、`O` 的更新公式，并判断何时触发对旧 `O` 的 rescale。
3. 解释为什么把自然指数改写成 base-2 的 `exp2` 只是代数变形，以及「硬件 exp2 + FMA 三次多项式」双路径如何改变执行单元利用率、哪些公式保持不变。

## 2. 前置知识

- **在线 softmax 三状态**（u14-l1）：每个 query 行跨 K/V 块携带 `row_max`（指数参考 \(r_i\)）、`row_sum`（\(\ell_i\)）、`O`（加权和 \(o_i\)）；完整 score 矩阵不落盘。
- **FA4 的角色与屏障**（u14-l2）：WG3 warp 0 发起两类 MMA（elect 单线程提交、Tensor Core 执行），WG0/WG1 各跑一个 Q stage 的 softmax，WG2 负责校正；`s_ready` 是靠 `tcgen05.commit` 追踪 Tensor Core 完成的 `TCGen05Bar`，「屏障类型由完成者决定」。
- **TMEM 中的 S/P/O**：S 是 fp32 score tile，P 是 softmax 写回的 fp16 权重 tile，O 是 PV MMA 的 fp32 累加器（三者共享同一 TMEM 分配，细节在 u14-l4）。
- **两个浮点执行概念**：`exp2` 是以 2 为底的指数运算，GPU 上由数量较少的特殊函数执行单元负责；FMA（fused multiply-add）指 \(a\times b+c\) 单指令，由通用 FP32 流水线执行、吞吐高。三次多项式恰好可以用一条 Horner 形式的 FMA 链求值。

## 3. 本讲源码地图

| 文件 / 区段 | 作用 |
|---|---|
| [chapter_flash_attention/index.md:L58-L102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L58-L102) | base-2 改写、\(\delta\)、`acc_scale` 与阈值三种情形的数学推导 |
| [chapter_flash_attention/index.md:L104-L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L104-L161) | 核心算法伪代码、`row_max_safe` 的 `-inf` 处理、指数双路径动机 |
| [chapter_flash_attention/index.md:L329-L355](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L329-L355) | QKᵀ MMA 小节：公式、代码与 tile primitive 四要素 |
| [chapter_flash_attention/index.md:L357-L476](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L357-L476) | Softmax Between MMAs：行到线程映射、参考值选择、exp2 双路径、P 写回与 `row_sum` 更新 |
| [chapter_flash_attention/index.md:L712-L760](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L712-L760) | Rescaling and Writeback：触发判据 `should_rescale`/`any_sync` 与两级过滤（数据路径细节归 u14-l5） |
| [chapter_flash_attention/index.md:L908-L917](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L908-L917) | 章末练习，本讲的实践任务取自练习 1 与练习 6 |
| `zh/chapter_flash_attention/index.md` | 上述内容的中文镜像（行号与英文版略有偏移，引用以英文版行号为准） |

## 4. 核心概念与源码讲解

### 4.1 QKᵀ MMA：score tile 的产生

#### 4.1.1 概念说明

Attention 的第一步是算 query 与 key 的相似度：\(O=\text{softmax}(QK^{\top}/\sqrt{d})V\)（见 [chapter_flash_attention/index.md:L12-L16](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L12-L16)）。FA4 把它切成两段矩阵乘，第一段就是 QKᵀ MMA：对当前 Q stage 和当前 K 块计算

\[ S = Q_{\text{block}}K_{\text{block}}^{\top} \]

`Q_block`、`K_block` 形状均为 `128 × HEAD_DIM`（HEAD_DIM=128），转置 K 后每个 Q 行与全部 128 个 K 行做点积，得到 `128 × 128` 的 score tile：**行对应 query，列对应当前 K 块里的 key**。`MMA_N=128` 就是这个 score tile 的宽度。注意此时**不做** \(/\sqrt{d}\) 缩放——源码从未缩放的 \(QK^{\top}\) 中选参考值，只在求指数时才乘 `scale_log2`（后文 4.2 会看到）。

这一步在硬件上就是 GEMM 章节反复精读的 `tcgen05` 路径，FA4 没有新增机制；新的是它的产物 `S` 落在 TMEM，紧接着要被 softmax 消费。

#### 4.1.2 核心流程

1. **前置等待**（u14-l2 已建立）：`q_load.full` 证明当前 Q stage 已在 SMEM，`kv_load.full` 证明当前 K stage 已在 SMEM。
2. **发起**：WG3 warp 0 执行 warp 级 tile 操作 `Tx.warp.gemm_async(..., dispatch="tcgen05")`，结果写入 `S_region[q_stage, :, :]`。
3. **挂接完成信号**：`elect_sync` 选出的一个 lane 执行 `s_ready.arrive(q_stage)`，即 `tcgen05.commit`，把此前发出的 QKᵀ MMA 与该 stage 的屏障关联。
4. **交接**：Tensor Core 硬件完成后更新 `s_ready`；对应 stage 的 softmax warpgroup（WG0 或 WG1）wait 通过后才能读 `S_region`。

#### 4.1.3 源码精读

QKᵀ MMA 的完整调用与说明在 [chapter_flash_attention/index.md:L331-L347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L331-L347)：

```python
Tx.warp.gemm_async(
    S_region[q_stage, :, :],
    Q_smem[q_stage, 0:BLK_M, 0:HEAD_DIM],
    K_smem[kv_stage, 0:BLK_N, 0:HEAD_DIM],
    dispatch="tcgen05",
    cta_group=CTA_GROUP,
)
if T.ptx.elect_sync():
    s_ready.arrive(q_stage)
```

- 目的操作数是 `S_region[q_stage, :, :]`——TMEM 上按 Q stage 切出的 fp32 score 区域；两个源操作数分别是 Q、K 的 SMEM tile 切片。
- `elect_sync` 保证同一 warp 内**恰好一个线程**发出 commit；这是 u9-l3 以来反复强调的「单线程发起」纪律。

章节随后给出这条 tile 操作的四要素标注（[chapter_flash_attention/index.md:L349-L355](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L349-L355)）：

> - Scope: WG3 warp 0 执行 warp 级 tile 操作；一个 elected lane 提交完成通知。
> - Layout: Q、K 在 SMEM → `S` 在 TMEM。
> - Dispatch: `tcgen05`。
> - Handoff: `s_ready`（→ softmax）。

并解释了 `s_ready` 的语义：它是 `TCGen05Bar`，`arrive` 实际发出的是 `tcgen05.commit`，硬件只在 Tensor Core **写完 S 之后**才报告完成——所以 softmax 等 `s_ready` 就等到了「S 可读」。

按 GEMM 章节确立的展开规则（一条 `Tx.gemm_async` 按 \(\lceil K/16\rceil\) 展开为 k16 的 `tcgen05.mma` 指令，u9-l1 中 K=64 展开为 4 条），可推演：QKᵀ MMA 的归约维是 HEAD_DIM=128，预计展开为 \(\lceil 128/16\rceil=8\) 条 `tcgen05.mma`。书中此节未直接给出该数字，属推演结论，可在有 Blackwell GPU 的环境用 `ex.mod.imports[0].inspect_source()` 搜索 `tcgen05.mma` 计数验证（待本地验证）。

#### 4.1.4 代码实践

**实践目标**：把 QKᵀ MMA 这条调用读成「四要素 + 展开预测」，为后续 softmax 分析锁定输入。

**操作步骤**：

1. 打开 [chapter_flash_attention/index.md:L337-L347](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L337-L347)，在纸上抄下这条 `Tx.warp.gemm_async`。
2. 逐参数标注：目的 buffer 与切片、两个源 buffer 与切片、dispatch、cta_group。
3. 回答：这条操作执行前 MMA warp 必须已通过哪两道屏障？（提示见 [chapter_flash_attention/index.md:L628-L638](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L628-L638)）
4. 推演展开条数：按 \(\lceil K/16\rceil\) 规则写出 HEAD_DIM=128 对应的 `tcgen05.mma` 条数。

**需要观察的现象**：本实践为源码阅读型，产出是一张四要素表加一个展开预测。

**预期结果**：scope=WG3 warp 0（单 lane commit）；layout=SMEM 的 Q/K → TMEM 的 `S_region[q_stage]`；dispatch=`tcgen05`；handoff=`s_ready`；两道前置屏障是 `q_load.full` 与 `kv_load.full`；展开预测为 8 条 k16 MMA（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`S` tile 的行和列各对应什么？宽度由哪个常量决定？

答案：行对应 query（当前 Q stage 的 128 行），列对应当前 K 块里的 128 个 key；宽度由 `MMA_N=128` 决定（见 [chapter_flash_attention/index.md:L335](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L335) 与 L276 的 `MMA_N` 条目）。

**练习 2**：`s_ready` 为什么是 `TCGen05Bar` 而不是 `TMABar` 或普通 `MBarrier`？

答案：屏障类型由**谁报告完成**决定。QKᵀ MMA 的完成者是 Tensor Core（经 `tcgen05.commit` 通知），既不是 TMA 引擎（`TMABar` 的字节计数），也不是普通线程 arrive（`MBarrier`），所以用 `TCGen05Bar`（[chapter_flash_attention/index.md:L355](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L355)，规则本身在 u14-l2 与 GEMM Step 7 已建立）。

**练习 3**：QKᵀ MMA 的结果里含 \(/\sqrt{d}\) 缩放吗？缩放发生在哪里？

答案：不含。源码从未缩放的 \(QK^{\top}\) 选参考值，`scale_log2` 只在求指数时乘入（[chapter_flash_attention/index.md:L774](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L774)；下一节 4.2 的 `P` 公式里可见）。

### 4.2 在线 softmax 数值递推：delta、acc_scale 与条件 rescale 的触发

#### 4.2.1 概念说明

softmax 夹在两个 MMA 之间，把 score tile `S` 变成未归一化权重 tile `P`。它的**数值核心**是一套跨块递推：每行维护 `row_max`（指数参考 \(r_i\)）、`row_sum`（\(\ell_i\)）、`O`（\(o_i\)），每来一个 K/V 块更新一次。

基础在线 softmax 每遇到更大的行最大值就换参考；FA4 的优化是**先看差距再决定**。推导从 base-2 改写开始（[chapter_flash_attention/index.md:L58-L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L58-L76)）：定义

\[ \alpha=\frac{\log_2(e)}{\sqrt d}\quad(\text{代码名 } \texttt{scale\_log2}),\qquad \exp\!\Big(\frac{s-m}{\sqrt d}\Big)=2^{(s-m)\alpha} \]

设旧参考为 \(r_{\mathrm{old}}\)（即保存的 `row_max`），当前块行最大值为 \(m_{\mathrm{block}}\)，候选参考与二者的指数域差距为

\[ r_c=\max(r_{\mathrm{old}},m_{\mathrm{block}}),\qquad \delta=(r_{\mathrm{old}}-r_c)\,\alpha\le 0 \]

\(\delta\) 是「旧参考比候选参考低多少个二进制指数位」。FA4 取阈值 \(\tau=\log_2(256)=8\)：

- \(\delta\ge -8\)：差距不超过 256 倍，**保留旧参考**，旧状态无需换算；
- \(\delta< -8\)：差距超过 256 倍，**采用候选参考**，旧状态必须乘换算因子

\[ a_{\mathrm{scale}}=e^{(r_{\mathrm{old}}-r_c)/\sqrt d}=2^{\delta}\quad(\text{代码名 } \texttt{acc\_scale}) \]

推导见 [chapter_flash_attention/index.md:L78-L90](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L78-L90)。于是每块三种情形（[chapter_flash_attention/index.md:L98-L102](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L98-L102)）：

| 情形 | new_ref | acc_scale | 旧 O 是否 rescale |
|---|---|---|---|
| 首个 K/V 块 | candidate_max | 1 | 无旧状态，直接初始化 |
| \(\delta\ge -8\) | row_max（保留旧值） | 1 | 否 |
| \(\delta< -8\) | candidate_max | \(2^{\delta}\) | 是（TMEM→寄存器→乘→TMEM） |

选定参考后，本块的权重、分母、输出为：

\[ P=\exp 2\big((S-r_{\mathrm{safe}})\cdot\alpha\big),\qquad \texttt{row\_sum} \leftarrow \texttt{row\_sum}\cdot a_{\mathrm{scale}}+\sum P,\qquad O \leftarrow O\cdot a_{\mathrm{scale}}+P@V \]

其中 \(r_{\mathrm{safe}}\) 是把 \(-\infty\) 参考替换成 0 的安全版本：若某行至今没有有效 score（如 causal 掩码下的全遮蔽块），直接算 \(S-(-\infty)\) 会得到 \(-\infty-(-\infty)\) 这样的未定义值，改用 0 让被遮蔽位置的指数为 0、`P`/`row_sum`/`O` 保持 0（[chapter_flash_attention/index.md:L157](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L157)）。

「条件 rescale 的触发」因此是一个**纯数值判据**：只有 \(\delta< -8\) 的行才会得到 \(a_{\mathrm{scale}}\ne 1\)。这个判据随后驱动两个执行层优化（4.2.3 最后一小节）：`row_sum` 在 softmax 自己的寄存器里就地乘；`O` 在 TMEM 里，要走 WG2 的校正数据路径——而 `acc_scale==1` 的行可以整段跳过。

#### 4.2.2 核心流程

一章一块的 softmax 递推（对应书中伪代码 [chapter_flash_attention/index.md:L106-L153](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L106-L153)）：

```text
等 s_ready → 分 4 段把 S 的本行读入寄存器 s_chunk_buf（每段 32 列）
→ 求本块行最大值，与 row_max 合成 candidate_max
→ 按 is_first / delta>=-8 / delta<-8 三分支定 new_ref 与 acc_scale
→ P = exp2((S - row_max_safe) * scale_log2)     # 双路径，见 4.3
→ 把 P 按 fp16 写回 TMEM（分段，先 p_o_rescale 后 p_ready_2）
→ 等 WG2 归还邮箱后：row_sum = row_sum * acc_scale + rowsum(P)
```

关键点：

- **每行一个线程**。score tile 128 行、softmax warpgroup 128 线程，逻辑行 `r` 归线程 `r`，每线程在 `s_chunk_buf` 里持有整行 128 个 fp32 score——这正是 u14-l2 中 softmax 组把寄存器上限提到 200 的主要原因。
- **分块加载、整行计算**。加载按 32 列一段做（`SOFTMAX_LD_CHUNK=32`），但 softmax 本身总是处理完整行：行最大值、行求和都必须看到全部 128 个 score。
- **\(\delta\) 与 \(a_{\mathrm{scale}}\) 是逐行的量**。不同行可以独立落在「保留」或「换参考」分支，所以 `acc_scale` 是 per-row 数组而非标量。

#### 4.2.3 源码精读

**(a) 行到线程的映射与整行缓冲**。[chapter_flash_attention/index.md:L367-L384](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L367-L384) 说明：`wg_local_layout` 把逻辑行 `r` 编到线程 `r`；等 `s_ready` 后用四次 32 列的 `Tx.wg.copy_async` 填充 `s_chunk_buf`：

```python
for chunk_idx in T.unroll(BLK_N // SOFTMAX_LD_CHUNK):
    Tx.wg.copy_async(
        s_chunk[:, chunk_idx * SOFTMAX_LD_CHUNK : (chunk_idx + 1) * SOFTMAX_LD_CHUNK],
        S_region[wg_id, :, chunk_idx * SOFTMAX_LD_CHUNK : ...],
    )
```

注意 `S_region[wg_id, ...]`——在 WG0/WG1 的 softmax 分支里，`wg_id` 恰好就是 Q stage 编号（见 L274 的命名表），两个 softmax 组各读自己 stage 的 score tile。

**(b) 参考值选择与阈值分支**。这段代码（[chapter_flash_attention/index.md:L390-L413](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L390-L413)）就是 4.2.1 公式的逐行落地：

```python
if is_first:
    Tx.max(tile_max, s_chunk_buf)          # 首块：行最大值即候选
else:
    row_max_old = row_max[0]
    tile_max[0] = row_max_old
    Tx.max(tile_max, s_chunk_buf, accum=True)   # max(row_max_old, rowmax(S))

row_max_new = tile_max[0]
row_max_safe = T.if_then_else(tile_max[0] == -float("inf"), 0.0, tile_max[0])
if is_first:
    acc_scale = T.float32(1.0)
else:
    acc_scale_ = (row_max_old - row_max_safe) * scale_log2   # 即 delta
    if acc_scale_ >= -rescale_threshold:                     # delta >= -8
        row_max_new = row_max_old                            # 保留旧参考
        row_max_safe = row_max_old
        acc_scale = T.float32(1.0)                           # 显式置精确的 1.0
    else:
        acc_scale = T.ptx.exp2(acc_scale_)                   # 2^delta
row_max[0] = row_max_new
```

对照公式：`acc_scale_` 就是 \(\delta=(r_{\mathrm{old}}-r_c)\cdot\alpha\)（代码用 `row_max_safe` 代替候选值，差别只在 \(-\infty\) 边界），`rescale_threshold=8.0`（L280）。**一个重要细节**：保留分支里 `acc_scale` 被显式赋成浮点常量 `1.0`，而不是 `exp2(delta)`——这使后面 WG2 用 `acc_scale < 1.0` 判断是否需要校正时是**精确比较**，不会因 `exp2(-3)` 的舍入误差误触发。

**(c) `row_sum` 的更新**（[chapter_flash_attention/index.md:L462-L472](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L462-L472)）：

```python
softmax_corr.empty.wait(wg_id, phase_q)
phase_q ^= 1
if is_first:
    Tx.sum(row_sum, s_chunk_buf)
else:
    row_sum[0] = row_sum[0] * acc_scale
    Tx.sum(row_sum, s_chunk_buf, accum=True)
```

即 \(\ell_i \leftarrow \ell_i\cdot a_{\mathrm{scale}}+\sum_j p_{ij}\)。这一步在 softmax 自己的寄存器里完成，代价只是一次乘法；等待 `softmax_corr.empty` 是为了按两 stage 交替协议复用与 WG2 的 SMEM 邮箱（u14-l2 已讲，u14-l5 展开）。`P` 的 fp16 写回与 `p_o_rescale`/`p_ready_2` 分段交接见 [chapter_flash_attention/index.md:L439-L460](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L439-L460)，其屏障语义归 u14-l2/u14-l4，此处不重复。

**(d) rescale 的触发端：两级过滤**。数学上只有 \(\delta<-8\) 的行需要换算；执行上这个判据在 WG2 侧变成两层筛查（[chapter_flash_attention/index.md:L737-L752](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L737-L752)）：

```python
should_rescale = T.Select(acc_scale < T.float32(1.0), 1, 0)
any_needs_rescale = T.ptx.any_sync(0xFFFFFFFF, should_rescale)

if any_needs_rescale != 0:
    # 本 warp：TMEM -> 寄存器 -> 乘 -> TMEM
    ...
p_o_rescale.arrive(i_q)
softmax_corr.empty.arrive(1 - i_q)
```

第一层是阈值测试（让多数行 `acc_scale=1`）；第二层是 `any_sync` 把 warp 内 32 行的标志按位或——**32 行全是 1 才能整 warp 跳过** TMEM→寄存器→TMEM 的数据路径。即便跳过数据路径，屏障 arrive 一次不少（L750），否则 PV MMA 会永远等不齐 256 次到达。校正数据路径本身（`RESCALE_TILE=16` 的分块读写）在 [chapter_flash_attention/index.md:L718-L731](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L718-L731)，归 u14-l5 精读。

#### 4.2.4 代码实践：章末练习 1 全程手算

**实践目标**：用两组输入把 4.2.1 的递推完整走一遍，弄清「只有第二组触发 rescale」的原因。

**题目**（[chapter_flash_attention/index.md:L910](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L910)）：一行 query，`scale_log2=1`、`rescale_threshold=8`、已有状态 `row_max=2`、`row_sum=3`、`O=[4,6]`；新块 `S=[5,4]`（再换 `S=[11,10]`），`V=[[1,0],[0,1]]`（单位阵，于是 \(P@V=P\)）。

**操作步骤**：对每组 S 依次计算 `rowmax(S)` → `candidate_max` → `delta` → 分支判定 → `new_ref`/`acc_scale` → `P` → 新 `row_sum` → `block_O` → 新 `O`。

**手算结果**：

| 量 | 情形一 `S=[5,4]` | 情形二 `S=[11,10]` |
|---|---|---|
| `rowmax(S)` | 5 | 11 |
| `candidate_max = max(2, rowmax)` | 5 | 11 |
| `delta = (2 − candidate)×1` | **−3** | **−9** |
| 判定 | −3 ≥ −8 → 保留旧参考 | −9 < −8 → 采用候选 |
| `new_ref` | 2 | 11 |
| `acc_scale` | 1 | \(2^{-9}=1/512\approx 0.001953\) |
| `P = exp2(S − new_ref)` | \([2^3, 2^2]=[8, 4]\) | \([2^0, 2^{-1}]=[1, 0.5]\) |
| 新 `row_sum = 3·acc + ΣP` | \(3+12=15\) | \(3/512+1.5=1.505859375\) |
| `block_O = P@V` | \([8,4]\) | \([1, 0.5]\) |
| 新 `O = O·acc + block_O` | \([4,6]+[8,4]=[12,10]\) | \([4/512+1,\;6/512+0.5]=[1.0078125,\;0.51171875]\) |

**为什么只有情形二 rescale 旧状态**：阈值 \(\tau=8\) 允许「候选超出旧参考」最多 \(2^8=256\) 倍。情形一的指数域差距只有 3 位（\(2^3=8\le 256\)），保留旧参考后本块最大权重也只有 8，完全在可表示范围内，旧状态无需换算，`acc_scale=1` → WG2 的 `should_rescale=0` → 整段跳过 O 的 TMEM 往返。情形二若仍保留旧参考，本块最大权重将达到 \(2^9=512>256\)，超出阈值允许的界（并持续侵占 fp16 `P` 的表示余量），因此必须把参考换成 11，同时把旧的 `row_sum`/`O` 乘 \(2^{-9}\) 拉到新尺度。这正是书中「用阈值换算次数换有界的指数增长」的权衡（[chapter_flash_attention/index.md:L76](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L76)）。

**预期结果**：上表数值即预期结果；第 5 节的综合实践脚本会自动复算这两组输入作为单元检查（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：同题设但 `S=[6,4]`，求新状态。

答案：`candidate_max=6`，\(\delta=(2-6)\times1=-4\ge -8\) → 保留参考 2，`acc_scale=1`；`P=[16,4]`；`row_sum=3+20=23`；`block_O=[16,4]`；`O=[20,10]`。不触发 rescale。

**练习 2**：把阈值改成 \(\tau=4\)，`S=[5,4]` 与 `S=[11,10]` 各走向哪个分支？说明阈值大小与 rescale 频率的关系。

答案：`S=[5,4]` 时 \(\delta=-3\ge -4\) 仍保留；`S=[11,10]` 时 \(\delta=-9<-4\) 仍换参考。阈值越小，「保留旧参考」的窗口越窄，rescale 触发越频繁——O 的 TMEM→寄存器→TMEM 往返越多，但指数增长上界越紧。书中取 8 是「更少 rescale」与「有界指数增长」之间的折中（L76）。

**练习 3**：WG2 侧判据为什么写 `acc_scale < 1.0` 而不是重新比较 `delta < -8`？

答案：因为 \(\delta\) 不跨线程组传递——softmax 只把每行的 `acc_scale` 写进 SMEM 邮箱交给 WG2（u14-l2 的 statistics named barrier）；且保留分支把 `acc_scale` 显式置为精确常量 `1.0`（L409），使 `< 1.0` 成为可靠判据。另外 \(\delta\le 0\) 恒成立，故 `acc_scale\le 1`，不会出现大于 1 的值。

### 4.3 exp2 双路径：硬件指数单元与 FMA 多项式分担

#### 4.3.1 概念说明

把 \(\exp\) 改写成 \(\exp 2\) 只是代数变形：\(\exp\big((s-m)/\sqrt d\big)=2^{(s-m)\alpha}\)。它**并没有**解除 softmax 的吞吐瓶颈——如果每个元素仍走硬件 `exp2` 单元，这类专用单元的数量和吞吐仍然决定 softmax 的上限（[chapter_flash_attention/index.md:L159-L161](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L159-L161)）。

FA4 的办法是把 128 个指数求值**拆到两条执行路径**：一部分元素用硬件 `exp2`（`T.ptx.exp2`），其余用 FP32 FMA 指令评估的三次多项式近似（当前 TIRx 实现中名为 `ex2_emulation_2`）。这样两类执行单元——指数单元与 FMA 流水线——可以**并发工作**，softmax 不再被单一执行路径限流。这就是章末练习 6 所说的「改变执行单元利用率」：原来一条流水线满载、另一条闲置；现在两条流水线同时有活干，同样的指数总量分摊到两条通路上。

关键在于：这一改变**只作用于「每个 \(2^x\) 怎么算」**，4.2 的全部递推公式（candidate_max、\(\delta\)、acc_scale、`P`、`row_sum`、`O`）一个都不变——多项式路径算出的仍是同一个数学函数的近似值。

#### 4.3.2 核心流程

softmax 中指数求值的组织方式：

```text
先用一条 tile 级 FMA 把整行变成指数参数：x = S·scale_log2 − row_max_safe·scale_log2
→ 把 128 列分成 4 个 32 列 fragment
→ 对每个 16 元素小组按 EMU 配置决定：哪些"对"走 ex2_emulation_2，其余走硬件 exp2
→ 头部/尾部 fragment 与被掩码位置强制走硬件 exp2
→ 结果经 Tx.wg.cast 转成 fp16 形成 P
```

注意第一步本身就是一次 FMA：\(x=s\cdot\alpha+(-r_{\mathrm{safe}}\cdot\alpha)\)，把「乘 scale、减参考」合并成一条指令——FMA 路径从这里就开始帮忙了。

#### 4.3.3 源码精读

双路径的实现见 [chapter_flash_attention/index.md:L415-L437](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L415-L437)：

```python
Tx.wg.fma(s_chunk, s_chunk, scale_log2, -row_max_safe * scale_log2)
for frag_idx in T.unroll(4):
    s_chunk_local = s_chunk_buf.local(BLK_N)
    for i in T.unroll(BLK_N // 4 // 2):
        idx = T.meta_var(frag_idx * BLK_N // 4 + 2 * i)
        emu_pairs = T.meta_var(EMU_PAIRS_CAUSAL if is_causal else EMU_PAIRS_NC)
        emu_start = T.meta_var(EMU_START_CAUSAL if is_causal else EMU_START_NC)
        if (i * 2 % 16 < 16 - 2 * emu_pairs or frag_idx >= 3
                or frag_idx < emu_start or apply_mask):
            s_chunk_local[idx] = T.ptx.exp2(s_chunk_local[idx])
            s_chunk_local[idx + 1] = T.ptx.exp2(s_chunk_local[idx + 1])
        else:
            ex2_emulation_2(
                s_chunk_local, idx, s_chunk_local[idx], s_chunk_local[idx + 1]
            )
    Tx.wg.cast(
        p_chunk[:, frag_idx * BLK_N // 4 : (frag_idx + 1) * BLK_N // 4],
        s_chunk[:, frag_idx * BLK_N // 4 : (frag_idx + 1) * BLK_N // 4],
    )
```

逐段读：

- **L418**：`Tx.wg.fma` 一次算出全部指数参数 \((S-r_{\mathrm{safe}})\cdot\alpha\)。
- **L419-L424**：外层 4 个 fragment（每个 32 列），内层按「成对」枚举（`idx = ... + 2*i`）——`ex2_emulation_2` 一次吃相邻两个元素，与硬件路径每次算两个相对应。
- **L425-L426 的选择条件**，满足任一者走**硬件** `exp2`：
  1. `i * 2 % 16 < 16 - 2 * emu_pairs`——每个 16 元素小组里，前段留给硬件；
  2. `frag_idx >= 3`——最后一个 fragment 全走硬件；
  3. `frag_idx < emu_start`——起始的若干 fragment 全走硬件；
  4. `apply_mask`——被 causal 掩码的位置走硬件。
  其余的成对元素走 `ex2_emulation_2`。
- **L433-L436**：fp32 结果经 `Tx.wg.cast` 转成 fp16 装进 `p_chunk`，供写回 TMEM 成为 PV MMA 的操作数（布局细节归 u14-l4）。

`EMU_PAIRS_*`、`EMU_START_*` 的具体数值定义在 tirx-kernels 的内核源文件里，书中未给出（待确认：若需精确比例，可查 `flash_attention4.py` 顶部定义）。双路径的精度代价被明确列进验证一节的误差来源清单：「hardware `exp2` 与三次多项式近似的有限精度」连同 fp16 舍入、分块累加顺序等，一起被 `rtol=1e-2, atol=1e-2` 的容差覆盖（[chapter_flash_attention/index.md:L898-L904](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L898-L904)）。

#### 4.3.4 代码实践：章末练习 6 + 多项式精度小实验

**实践目标**：回答练习 6（[chapter_flash_attention/index.md:L915](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L915)），并用一个本地小实验建立对「三次多项式近似够准」的直觉。

**第一部分（分析，练习 6 答案）**：

1. **为什么改写成 `exp2` 后硬件指数路径仍可能成为瓶颈**：base-2 改写只是把 \(\exp\) 换成等价的 \(\exp 2\)，每个元素仍然要求一次指数运算；若全部走硬件 `exp2`，该类单元的固定吞吐就是 softmax 的上限，FMA 流水线此时大多闲置。
2. **双路径如何改变执行单元利用率**：把一部分元素（按 16 元素小组配对、跳过首尾 fragment 与掩码位置）改由 FMA 三次多项式求值后，指数单元与 FMA 流水线**同时**有工作量，指数总量被分摊到两条通路，softmax 对单一执行路径的依赖下降。
3. **哪些公式不变**：全部在线 softmax 递推——`candidate_max`、\(\delta\)、`acc_scale`、`P = exp2((S − row_max_safe)·scale_log2)`、`row_sum = row_sum·acc_scale + ΣP`、`O = O·acc_scale + P@V`。改变的只是每个 \(2^x\) 的**数值求值方式**（L161 明说「这项调整只改变指数的实现方式，不改变上面的 online-softmax 更新公式」）。

**第二部分（本地实验，示例代码——不是内核的 `ex2_emulation_2`，仅教学演示「三次多项式 + FMA 链」能达到的精度量级）**：

```python
# 示例代码：三次多项式逼近 2^x 的精度演示（教学用，非内核实现）
import numpy as np

# 内核未公开多项式；此处演示标准做法：把 x 拆成 n + f（f 属于 [0,1)），
# 多项式只逼近 2^f，整数部分 2^n 由指数域处理（demo 中用精确的 2**n）。
f = np.linspace(0.0, 1.0, 4001, endpoint=False)
c3, c2, c1, c0 = np.polyfit(f, 2.0 ** f, 3)      # 三次拟合

def exp2_poly(x):                                 # Horner 形式 = 一条 FMA 链
    n = np.floor(x)
    frac = x - n
    p = c0 + frac * (c1 + frac * (c2 + frac * c3))
    return p * 2.0 ** n

xs = np.linspace(-30.0, 8.0, 100001)              # softmax 实际参数范围：<= +8
err = np.abs(exp2_poly(xs) / np.exp2(xs) - 1.0)
print(f"max relative error = {err.max():.3e}")    # 与书中 rtol=1e-2 对比
```

**操作步骤**：运行上述脚本，记录最大相对误差；再改 `deg=3` 为 `deg=2` / `deg=4` 观察误差量级变化。

**需要观察的现象 / 预期结果**：三次拟合在 \([-30, 8]\) 上的最大相对误差预期在 \(10^{-3}\) 量级（具体数值待本地验证），比内核验证用的 `rtol=1e-2` 小一个量级以上；降为二次则误差显著增大，升为四次改善有限——这解释了「三次」是精度与 FMA 链长度的折中。

#### 4.3.5 小练习与答案

**练习 1**：双路径拆分后，哪些量必须逐元素一致？哪些允许有近似差？

答案：分段交接的**同步与布局**必须逐元素一致（`P` 的每个位置无论哪条路径算出，都写进同一个 fp16 TMEM 槽位，读写两端布局相等）；**数值**上允许近似差——多项式路径给出的是 \(2^x\) 的三次近似，其误差与硬件 `exp2` 的有限精度同列验证一节的误差来源（L902），由容差覆盖。

**练习 2**：读 [chapter_flash_attention/index.md:L425-L426](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_flash_attention/index.md#L425-L426)，列出哪些元素一定走硬件 `exp2`。

答案：四类——(1) 每个 16 元素小组中 `i*2 % 16 < 16 - 2*emu_pairs` 的前段元素；(2) 最后一个 fragment（`frag_idx >= 3`）的全部元素；(3) 起始 `emu_start` 个 fragment 的全部元素；(4) `apply_mask` 生效时的被掩码元素。

**练习 3**：如果 `row_max_safe` 已把参数压到 \(\le 0\)，为什么双路径近似对最终归一化输出 \(O/\ell\) 的影响更小？

答案：softmax 最终输出是比值 \(o_i/\ell_i\)，分子分母来自同一组 \(p_{ij}\)；逐元素近似误差在求和与归一化中有相当部分相互抵消，且偏离参考很远的位置 \(p_{ij}\) 本身极小、对和式贡献可忽略。所以多项式近似的净效应被控制在容差内（这也是验证一节把它与其他舍入效应合并处理的原因，L902-L904）。

## 5. 综合实践

把本讲三个模块串成一个可在任何有 Python/numpy 的机器上运行的小实验（示例代码，非项目源码）：用 numpy 复现「QKᵀ MMA → 在线 softmax（含条件 rescale）→ 指数双路径」的单行递推，先复算 4.2.4 的两组输入，再对一段随机 attention 与标准参考对数。

```python
# 示例代码：FA4 softmax 递推 + 指数双路径的最小复现
import numpy as np

LN2 = np.log(2.0)
f = np.linspace(0.0, 1.0, 4001, endpoint=False)
c3, c2, c1, c0 = np.polyfit(f, 2.0 ** f, 3)

def exp2_poly(x):                       # 扮演 ex2_emulation_2（FMA 路径）
    n = np.floor(x); frac = x - n
    return (c0 + frac * (c1 + frac * (c2 + frac * c3))) * 2.0 ** n

def exp2_dual(x):                       # 扮演双路径分流：奇偶位分别走两条路
    x = np.asarray(x, dtype=np.float64).ravel()
    out = np.empty_like(x)
    out[0::2] = np.exp2(x[0::2])        # 硬件 exp2 路径
    out[1::2] = exp2_poly(x[1::2])      # FMA 多项式路径
    return out

def fa4_row(S_blocks, V_blocks, scale_log2, tau=8.0,
            row_max=-np.inf, row_sum=0.0, O=None, first=True):
    """单行 online-softmax 递推，对应书中伪代码 L106-L153"""
    n_rescale = 0
    for S, V in zip(S_blocks, V_blocks):            # S 扮演 QK^T MMA 的产物
        cand = max(row_max, S.max())
        if first:
            new_ref, acc = cand, 1.0
        else:
            delta = (row_max - cand) * scale_log2   # delta <= 0
            if delta >= -tau:
                new_ref, acc = row_max, 1.0         # 保留旧参考
            else:
                new_ref, acc = cand, np.exp2(delta) # 换参考 + 重缩放
                n_rescale += 1
        ref_safe = 0.0 if new_ref == -np.inf else new_ref
        P = exp2_dual((S - ref_safe) * scale_log2)  # P = exp2((S-ref)*alpha)
        row_sum = row_sum * acc + P.sum()
        O = P @ V if O is None else O * acc + P @ V # block_O 累加
        row_max, first = new_ref, False
    O = O / row_sum if row_sum != 0 else np.zeros_like(O)
    return row_max, row_sum, O, n_rescale

if __name__ == "__main__":
    V = np.eye(2)
    # 单元检查：4.2.4 的两组输入（预期见该节表格）
    print(fa4_row([np.array([5., 4.])], [V], 1.0,
                  row_max=2., row_sum=3., O=np.array([4., 6.]), first=False))
    print(fa4_row([np.array([11., 10.])], [V], 1.0,
                  row_max=2., row_sum=3., O=np.array([4., 6.]), first=False))

    # 随机 attention：对第 0 个 query 行跑分块递推，与稳定 softmax 参考对比
    rng = np.random.default_rng(0)
    L, d, BLK = 256, 16, 32
    Q, K, Vm = (rng.standard_normal((L, d)) for _ in range(3))
    alpha = np.log2(np.e) / np.sqrt(d)
    S_blocks = [(Q @ K[i:i+BLK].T)[0:1] for i in range(0, L, BLK)]  # 第 0 行的未缩放 QK^T
    V_blocks = [Vm[i:i+BLK] for i in range(0, L, BLK)]
    *_, O_row0, n_res = fa4_row(S_blocks, V_blocks, alpha)

    logits = (Q @ K.T) / np.sqrt(d)                    # 稳定 softmax 参考实现
    W = np.exp(logits - logits.max(axis=1, keepdims=True))
    W /= W.sum(axis=1, keepdims=True)
    print("max |fa4 - ref| =", np.abs(O_row0 - (W @ Vm)[0]).max(),
          " rescales:", n_res)
```

**操作步骤**：

1. 保存为 `fa4_softmax_sim.py` 并运行（只需 numpy）。
2. 核对前两行输出是否等于 4.2.4 表格中的 `row_sum`/`O`（`row_max` 分别为 2 与 11，`n_rescale` 分别为 0 与 1）。
3. 把 `tau` 从 8 改成 4、再改成 2，观察 `rescales` 次数与最终误差的变化。
4. 把 `exp2_dual` 换成全 `np.exp2`、再换成全 `exp2_poly`，对比最终 `max |fa4 - ref|`。

**需要观察的现象**：三组对照分别对应本讲三个模块——QKᵀ 的产物 `S` 喂给递推（模块一）、`tau` 控制条件 rescale 的触发频率（模块二）、双路径拆分只引入远小于 1e-2 的误差（模块三）。

**预期结果**：单元检查输出与 4.2.4 手算一致；随机 attention 的最大误差在 \(10^{-3}\) 量级（多项式近似 + 奇偶分流所致，待本地验证具体数值）；减小 `tau` 会使 `n_rescale` 上升而结果几乎不变——印证条件 rescale 是性能优化而非数学变化。

## 6. 本讲小结

- QKᵀ MMA 由 WG3 warp 0 以一条 `Tx.warp.gemm_async(dispatch="tcgen05")` 完成：SMEM 里的 Q（128×HEAD_DIM）与 K（128×HEAD_DIM）产出 128×128 的 fp32 score tile `S` 写入 `S_region[q_stage]`，经 `s_ready`（`TCGen05Bar`，靠 `tcgen05.commit`）交给 softmax；按 \(\lceil K/16\rceil\) 规则可推演其展开为 8 条 k16 MMA。
- 在线 softmax 的数值核心是 \(\alpha=\log_2(e)/\sqrt d\)、\(\delta=(r_{\mathrm{old}}-r_c)\alpha\le 0\)、\(a_{\mathrm{scale}}=2^{\delta}\) 三件套；\(\delta\ge -8\) 保留旧参考且 `acc_scale` 被显式置为精确 1.0，\(\delta<-8\) 才换参考并把旧 `row_sum`/`O` 乘 \(2^{\delta}\)。
- rescale 的触发是纯数值判据，执行上是两级过滤：阈值测试让多数行 `acc_scale=1`，`any_sync` 让 32 行全为 1 的 warp 整体跳过 O 的 TMEM→寄存器→TMEM 路径，但屏障 arrive 一次不少。
- base-2 改写只是代数变形；真正的优化是把指数元素拆给硬件 `exp2` 与 `ex2_emulation_2`（FP32 FMA 三次多项式）两条执行路径并发分摊，在线 softmax 的递推公式全部不变。
- softmax 把 fp32 结果 cast 成 fp16 的 `P` 写回 TMEM，才成为 PV MMA 可读的操作数——这段布局与复用是下一讲的主题。

## 7. 下一步学习建议

- 下一讲 **u14-l4（PV MMA 与 TMEM 布局复用）**：`P@V` 两段式 MMA（`K_SPLIT` 64+64 / 96+32）、fp16 视图列到 32-bit 物理列的映射 \(\lfloor c/2\rfloor\)，以及 S0/S1/P0/P1/O0/O1 的物理列区间与重叠防护——本讲 4.3 末尾的 `Tx.wg.cast` 正是它的入口。
- 之后 **u14-l5（条件 rescaling 与 writeback）** 精读 WG2 的校正数据路径（`RESCALE_TILE=16` 分块、交替 stage 协议）与 `O` 从 TMEM 到 GMEM 的完整回写，把本讲 4.2 的触发判据落到执行层。
- 延伸阅读：tirx-kernels 仓库的 `flash_attention4.py` 中 `ex2_emulation_2` 的真实多项式与 `EMU_*` 常量定义（书中未给出数值，需对照源码确认）；[FA4 论文](https://arxiv.org/abs/2603.05451)中关于两条指数路径错峰与阈值 \(\tau\) 选取的讨论。
