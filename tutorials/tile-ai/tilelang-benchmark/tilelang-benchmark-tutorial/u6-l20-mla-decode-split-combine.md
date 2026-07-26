# MLA decode：split-KV 并行与 combine

## 1. 本讲目标

本讲聚焦 AMD MI300x（CDNA 架构）上的 **FlashMLA decode** 内核，目标是把「把一个长 KV 序列拆成多段并行算、最后再合并」这件事讲透。学完后你应当能够：

- 说清 **MLA（Multi-head Latent Attention）** 的低秩 KV 与解耦 RoPE 结构，理解为何 attention 打分要拆成 `Q@KV^T` 与 `Q_pe@K_pe^T` 两路 gemm；
- 理解 `flash_attn_split` 如何沿 KV 维切分（split-KV），把单 block 串行遍历整条序列改成多 block 并行各算一段；
- 看懂 `combine` 内核如何用「lse_max → exp2 → log2」的在线 log-sum-exp 把各 split 的部分输出合并成全局 softmax 结果；
- 理解 `T.annotate_layout` 配合 `T.Fragment(forward_thread_fn=...)` 如何为局部寄存器缓冲标注线程布局。

本讲承接 u5-l16（FlashAttention 在线 softmax 与 macro 结构）。MLA decode 的主体计算与 FlashAttention 几乎一致，所以本讲**不重复在线 softmax 的推导**，只讲 MLA 特有的两路 gemm、split-KV 并行与 combine 合并这三处增量。

## 2. 前置知识

### 2.1 什么是 MLA（Multi-head Latent Attention）

MLA 是 DeepSeek-V2/V3 使用的注意力变体。普通 MHA/GQA 里每个（或每组）注意力头各自持有一份完整的 K、V；MLA 的核心是**低秩压缩 KV**：把所有头共享的 K、V 压缩成一个低秩的「潜在向量」，查询时再由各自的权重解压。好处是 KV-cache 显存大幅下降，代价是计算上多了一步投影。

本项目把这套结构简化为两个张量族：

- `KV`：压缩后的潜在 KV，形状 `[batch, seqlen_kv, kv_head_num, dim]`，其中 `dim=512` 是潜在维度。注意代码强制 `kv_head_num==1`，即**全 batch 共享一份 KV 潜在向量**（类似 MQA）。
- `K_pe`：解耦的位置编码（decoupled RoPE），形状 `[batch, seqlen_kv, kv_head_num, pe_dim]`，`pe_dim=64`。

为什么要「解耦」？因为旋转位置编码 RoPE 是逐元素乘在 K/Q 上的，无法被吸收进低秩压缩里。DeepSeek 的做法是把「带 RoPE 的那一小段维度」单独拆出来，作为 `Q_pe / K_pe` 另走一路，最终 attention 打分 = 潜在部分得分 + 位置部分得分。

查询端对应有 `Q [batch, heads, dim]` 与 `Q_pe [batch, heads, pe_dim]`，`heads=128`。每个头都拿同一份共享 KV 去算（`kv_group_num = heads // kv_head_num = heads`）。

### 2.2 为什么 decode 需要拆分 KV

**Decode（解码）阶段** 的特征是「查询极短」——通常每个 batch 只有 1 个待生成的 token，即查询序列长度 = 1（本内核用 `block_H` 维度铺查询头，而非铺查询序列）。这带来一个问题：

- 在 **prefill** 阶段，查询序列很长，矩阵乘的 M 维很大，GPU 自然有大量并行度；
- 在 **decode** 阶段，M 维≈0，并行度全部来自 N（KV 长度）。如果让一个 thread block 串行遍历整条 KV 序列，KV 越长这个 block 越慢，而 GPU 上又「空着」大量 block 没活干。

**split-KV（也叫 split-K / split-KV parallelism）** 的解法是：把 KV 序列沿长度方向切成 `num_split` 段，每段交给一组独立的 thread block 并行计算，各自得到一份「局部的、未做全局归一化的部分输出」；最后再用一个小内核（**combine**）把这些部分输出按各自的统计量合并成正确的全局 softmax 结果。这与经典 split-K GEMM、FlashAttention-2/3 的并行 KV 思路同源。

### 2.3 在线 softmax 与 log-sum-exp（承接 u5-l16）

u5-l16 已讲过：FlashAttention 不显式物化完整的 $n^2$ 分数矩阵，而是分块迭代 KV，用「在线 softmax」维护每个查询块的运行最大值 $m$ 与未归一化累加输出 $O$，迭代结束后再除以 $\text{logsum}$ 完成归一。本讲的 `flash_attn_split` 用的就是同一套四段 macro（MMA0→Softmax→Rescale→MMA1），只是它在迭代结束后**不写最终输出**，而是把「本段的最终权重和」与「已归一化的部分输出」分别写出，供 combine 使用。

数学上，合并多个 softmax 分块依赖 **log-sum-exp（LSE）** 的可加性。设第 $s$ 个分块覆盖一组 key，其局部最大（缩放后）logit 为 $m_s$、局部权重和为 $Z_s=\sum_{j\in s}\exp(\ell_j-m_s)$（其中 $\ell_j$ 是缩放 logit），则该分块的 LSE 定义为：

\[
\mathrm{lse}_s \;=\; m_s + \log Z_s
\]

合并全部 $S$ 个分块时，全局 softmax 权重和为 $Z=\sum_s \exp(\mathrm{lse}_s)$，为了数值稳定先取 $M=\max_s \mathrm{lse}_s$：

\[
\log Z \;=\; M + \log\!\Big(\sum_{s} \exp(\mathrm{lse}_s - M)\Big)
\]

本内核用 $\log_2/\exp_2$（base-2）实现，所以公式里的 $\log/\exp$ 都替换成 $\log_2/\exp_2$，并把 $\sqrt{1/(\dim+\mathrm{pe\_dim})}$ 预先乘上 $\log_2 e \approx 1.4427$ 折进 `scale`——这与 u5-l16 的 `exp2` 指令优化完全一致。

## 3. 本讲源码地图

本讲只读两个文件（加一个 shell 与一个画图脚本作辅助）：

| 文件 | 作用 |
| --- | --- |
| [benchmark_mla_decode_amd_tilelang.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py) | TileLang 版 FlashMLA decode 内核主体。内含三个 `@T.macro`（`flash_attn` / `flash_attn_split` / `combine`）、两个 `@T.prim_func`（`main_split` / `main_no_split`）以及参考实现 `ref_program` 与 `__main__` 驱动。 |
| [plot_figure.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/plot_figure.py) | 把 TileLang / Triton / PyTorch / Aiter-ASM 四家在 MI300x 上的 TFlops 画成柱状对比图。 |
| [benchmark_tilelang.sh](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_tilelang.sh) | 编排脚本：对 `batch∈{64,128}`、`kv_ctx∈{1024…16384}` 逐形状跑 `--auto_tune`。 |

> ⚠️ 提醒（承接前几讲的「以代码为准」）：本文件 `__name__=="__main__"` 里默认 `num_split=4`，但 `--auto_tune` 调优空间里 `num_split` 取值 `[1,2,4,8,16,32]`，即实际最优切分数由 AutoTuner 决定，不一定是 4。

---

## 4. 核心概念与源码讲解

### 4.1 MLA 的两路 gemm：潜在得分 + 解耦 RoPE 得分

#### 4.1.1 概念说明

普通 attention 的打分是单个矩阵乘 $S = QK^\top$。MLA 因为把 K 拆成了「潜在 KV」和「解耦位置 K_pe」两段，查询 Q 也对应拆成 $Q$（潜在）与 $Q_{pe}$（位置），所以打分变成两项相加：

\[
S_{i,j} \;=\; Q_i \cdot KV_j^\top \;+\; Q_{pe,i}\cdot K_{pe,j}^\top
\]

两项各自是一个矩阵乘，但因为它们共享同一批 query 块、要累加到同一个分数块 `acc_s`，TileLang 用**两次连续的 `T.gemm` 写同一个目的 fragment** 实现，省去中间显存。

注意「值」$V$ 只来自潜在 `KV`（不含 `K_pe`），所以后面 $PV$ 那一次 gemm 只用 `dim`、不用 `pe_dim`。这一点会直接反映在 FLOPS 计算上（见 4.1.4）。

#### 4.1.2 核心流程

`flash_attn` 与 `flash_attn_split` 的主体结构完全相同（这点很关键，4.2 会讲它们的唯一差异），主体流程为：

1. 把当前 query 头块的 `Q`、`Q_pe` 拷进片上缓冲；
2. 沿 KV 分块循环，每块：
   - `T.clear(acc_s)` 清零分数累加器；
   - **MMA0**：`T.gemm(Q, KV, acc_s, transpose_B=True)` 累加潜在得分；
   - **MMA0 续**：`T.gemm(Q_pe, K_pe, acc_s, transpose_B=True)` 把位置得分累加进同一个 `acc_s`；
   - **Softmax**：更新 `scores_max`、`scores_scale`、`scores_sum`、`logsum`；
   - **Rescale**：用 `scores_scale` 缩放旧 `acc_o`；
   - **MMA1**：`T.gemm(acc_s_cast, KV, acc_o)` 累加 $P\cdot V$；
3. 循环结束，`acc_o /= logsum` 归一化。

#### 4.1.3 源码精读

先看函数签名与几个关键的形状/缩放常量设定：

[benchmark_mla_decode_amd_tilelang.py:15-30](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L15-L30) —— `flashmla_decode` 的入参定义。注意第 25 行 `scale = (1.0/(dim+pe_dim))**0.5 * 1.44269504`：有效头维 $=\dim+\mathrm{pe\_dim}=512+64=576$，缩放 $1/\sqrt{576}$，再乘 $\log_2 e\approx 1.4427$，把后面要用 `exp2` 的 base-2 折算提前融进 scale。第 30 行 `assert kv_head_num == 1` 强制全 batch 共享单份 KV。

[benchmark_mla_decode_amd_tilelang.py:67-75](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L67-L75) —— 这是本模块最核心的两路 gemm。两次 `T.gemm` 都写 `acc_s`、都用 `transpose_B=True`（即 $Q\cdot KV^\top$、$Q_{pe}\cdot K_{pe}^\top$），第一次把潜在得分算进 `acc_s`，第二次在同一个 `acc_s` 上**累加**位置得分。两次都用 `policy=T.GemmWarpPolicy.FullRow`。

[benchmark_mla_decode_amd_tilelang.py:87](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L87) —— 在线 softmax 的 `logsum` 更新（承接 u5-l16）：`logsum = logsum * scores_scale + scores_sum`，把旧分母迁到新参考最大值上、再加新块的权重和。

[benchmark_mla_decode_amd_tilelang.py:288-290](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L288-L290) —— FLOPS 计算佐证「V 只有潜在部分」：`qk_flops` 用 `(dim+pe_dim)`、`pv_flops` 只用 `dim`。换算成 TFlops 仍用前几讲统一的 `total_flops/latency*1e-9`（latency 以 ms 计）。

#### 4.1.4 代码实践

**实践目标**：用 FLOPS 公式验证「两路 gemm 在算量上确实是潜在维 + 位置维，而 PV 只算潜在维」。

**操作步骤（源码阅读型，无需 GPU）**：

1. 读 [benchmark_mla_decode_amd_tilelang.py:288-290](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L288-L290)，记下 `qk_flops` 与 `pv_flops` 的表达式。
2. 取默认参数 `batch=1, heads=128, kv_ctx=1024, dim=512, pe_dim=64`，手算三者数值。
3. 回到第 68-75 行的两路 gemm，确认它们对应 `qk_flops` 的 `(dim+pe_dim)`；回到第 90 行的 `T.gemm(acc_s_cast, KV_shared, ...)`，确认它只用到 `KV`（潜在），对应 `pv_flops` 的 `dim`。

**需要观察的现象**：

- `qk_flops` 比 `pv_flops` 大，多出来的部分正比于 `pe_dim=64`。
- 若有人误把 `pv_flops` 也写成 `(dim+pe_dim)`，TFlops 会被**高估**（同一 latency 算出更高 TFlops），对比图失真。

**预期结果（batch=1, heads=128, kv_ctx=1024, dim=512, pe_dim=64）**：

- $qk\_flops = 2\cdot1\cdot128\cdot1024\cdot(512+64) = 2\cdot128\cdot1024\cdot576$；
- $pv\_flops = 2\cdot1\cdot128\cdot1024\cdot512$；
- 二者比值 $qk:pv = 576:512 = 9:8$。

> 待本地验证：上述手算数值在你本机代入后是否与日志里 `total_flops / latency * 1e-9` 给出的 TFlops 口径一致（注意单位陷阱——本内核 `latency` 以 ms 计）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `pe_dim` 改成 0（退化为无解耦 RoPE 的普通 MLA/MQA），打分公式和 FLOPS 会怎么变？
**答案**：`scale` 变成 $1/\sqrt{\dim}$；第 70-75 行的第二路 `T.gemm(Q_pe, K_pe, acc_s)` 因 `pe_dim=0` 实际不产生工作量；`qk_flops` 退化成 $2\cdot batch\cdot heads\cdot kv\_ctx\cdot dim$，与 `pv_flops` 同阶。

**练习 2**：两路 gemm 是否可以交换顺序（先算 `Q_pe@K_pe` 再算 `Q@KV`）？结果会变吗？
**答案**：数学上浮点加法可交换但不可结合，顺序改变会有微小数值差异；逻辑上结果不变，因为都累加到同一 `acc_s`。但先算哪一路可能影响 `transpose_B` 与 fragment 布局下的访存合并效率，属调优问题。

---

### 4.2 flash_attn 与 flash_attn_split：split-KV 并行的差异

#### 4.2.1 概念说明

`flash_attn` 是「不切分」版本：一个 thread block 负责某个 (batch, 头组)，串行遍历**整条** KV 序列，算完直接写最终 `Output`。它在 `num_split==1` 时使用。

`flash_attn_split` 是「切分」版本：沿 KV 序列多出一个并行维度 `bz`（split 编号），每个 block 只负责 `seqlen_kv/num_split` 长度的一段 KV，算完后**不写最终输出**，而是写两样东西到全局显存：

- `glse[batch, heads, num_split]`：本段的 log-sum-exp（合并用的统计量）；
- `Output_partial[batch, heads, num_split, dim]`：本段**已按本段权重归一化**的部分输出。

这两样东西随后由 `combine` 内核读回，合并成全局 `Output`。

注意一个精妙之处：`flash_attn_split` 在段内最后做了 `acc_o /= logsum`（见 [第 157-158 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L157-L158)），即段内已经归一化，所以 `Output_partial` 是「该段内的条件 softmax 加权和」；要把它升级成全局 softmax，combine 必须再按各段的总权重重新加权——这正是 4.3 要讲的合并数学。

#### 4.2.2 核心流程

`flash_attn_split` 相对 `flash_attn` 的差异只有四处：

| 项 | `flash_attn` | `flash_attn_split` |
| --- | --- | --- |
| 网格维度 | 2D `(bx=batch, by=头组)` | 3D `(bx=batch, by=头组, bz=split)` |
| KV 循环范围 | `ceildiv(seqlen_kv, block_N)` 遍历全序 | `ceildiv(seqlen_kv/num_split, block_N)` 只遍历本段 |
| KV 偏移 | `k*block_N` | `(seqlen_kv/num_split)*bz + k*block_N` |
| 输出 | `acc_o/logsum` → `Output` | `log2(logsum)+scores_max*scale` → `glse[...,bz]`；`acc_o/logsum` → `Output_partial[...,bz,:]` |

其余四段 macro 完全一致。这种「主体复用、只换并行与输出」的写法是 split-KV 类内核的通用范式。

#### 4.2.3 源码精读

[benchmark_mla_decode_amd_tilelang.py:104-106](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L104-L106) —— `flash_attn_split` 的 `T.Kernel` 多出第三个网格维度 `num_split`，绑定到 `bz`。对照 `flash_attn` 的 [第 40 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L40) 只有 `(bx, by)`，这是切分并行的根。

[benchmark_mla_decode_amd_tilelang.py:128-133](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L128-L133) —— 段内 KV 切片。`loop_range = ceildiv(seqlen_kv/num_split, block_N)` 只算本段需要多少 block；`kv_start/kv_end = (seqlen_kv/num_split)*bz + k*block_N` 用 `bz` 把全局偏移算出来。这就是「同一份 KV，不同 split 看不同段」。

[benchmark_mla_decode_amd_tilelang.py:159-162](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L159-L162) —— 段末输出。第 158 行先把 `acc_o` 按段内 `logsum` 归一化（段内条件 softmax 结果）；第 160 行 `logsum = log2(logsum) + scores_max*scale` 把「段内最大缩放 logit」与「段内权重和」合成一个 LSE 标量写进 `glse[...,bz]`；第 161-162 行分别把 LSE 与归一化部分输出写回全局显存。

[benchmark_mla_decode_amd_tilelang.py:91-93](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L91-L93) —— 对照 `flash_attn`：段内循环结束后直接 `acc_o /= logsum` 写最终 `Output`，**没有** `log2(logsum)+scores_max*scale` 这一步，也**不**写 `glse/Output_partial`。这就是「不切分」路径。

> 关于 `scores_max` 的含义：它在该 K 块循环里是「**当前块**的 acc_s 最大值」（每轮 `T.fill(scores_max, -inf)` 后 `reduce_max(acc_s,...,clear=False)`），循环结束后保留的是**最后一个块**的最大值，而非整段最大值。这在 4.3 解释合并时是关键细节：`glse` 里写入的 `scores_max*scale` 不是严格的段内全局最大，但因为同一段内的 `Output_partial` 也是用同一套（最后一轮的）`scores_max` 体系归一化的，combine 端只关心各 split 的 **LSE 相对值**，绝对偏移会在 `lse_max` 与差值 `exp2(lse-lse_max)` 中被消掉。因此这不影响最终 softmax 的正确性。

#### 4.2.4 代码实践

**实践目标**：理解 `num_split` 如何改变网格规模与每 block 的工作量。

**操作步骤（源码阅读型）**：

1. 读 [第 104-106 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L104-L106) 与 [第 128-133 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L128-L133)。
2. 取 `batch=128, heads=128, block_H=64, kv_ctx=16384, block_N=32`，分别算 `num_split=1`（走 `flash_attn`）与 `num_split=8`（走 `flash_attn_split`）时：
   - 总 thread block 数（网格三轴之积）；
   - 每个 block 要遍历的 KV block 数 `loop_range`。

**需要观察的现象**：

- `num_split=1`：网格 `128 × (128//64) = 128×2 = 256` 个 block，每个遍历 `16384/32 = 512` 个 KV block（很重的串行）。
- `num_split=8`：网格 `128 × 2 × 8 = 2048` 个 block，每个只遍历 `(16384/8)/32 = 64` 个 KV block（并行度 ×8、每块工作量 ÷8）。

**预期结果**：split-KV 用 8 倍的 block 数换取每块 1/8 的工作量，把串行长 KV 变成宽并行；代价是多了一次 combine 内核启动与多写 `num_split` 份 `Output_partial` 的显存。**待本地验证**：在 MI300x 上对短 `kv_ctx`（如 1024）`num_split=1` 是否反而更快（切分/合并的额外开销可能不划算）。

#### 4.2.5 小练习与答案

**练习 1**：`flash_attn_split` 里 `cur_kv_head = by // (kv_group_num // block_H)`，在 `heads=128, kv_head_num=1, block_H=64` 时，`by∈{0,1}` 各算到哪个 `cur_kv_head`？为什么都是同一个？
**答案**：`kv_group_num = 128`，`kv_group_num//block_H = 128//64 = 2`，故 `by=0 → 0//2=0`、`by=1 → 1//2=0`，都指向唯一的共享 KV head 0。这正是 MLA「全头共享单份 KV」的体现。

**练习 2**：为什么 `kv_ctx` 必须能被 `num_split` 整除？（提示：看 [第 128、130 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L128-L131) 的整除写法。）
**答案**：代码用 `seqlen_kv // num_split` 直接切分、用 `*bz` 算偏移，没有处理余数。若不整除，尾段长度对不齐会漏算或越界 KV。本项目脚本里 `kv_ctx∈{1024,2048,4096,8192,16384}` 都是 2 的幂，且 `num_split∈{1,2,4,8,16,32}` 都能整除，故安全。

---

### 4.3 combine 与 glse 合并数学

#### 4.3.1 概念说明

`combine` 是 split-KV 的「第二阶段」。它读入所有 split 的 `glse`（每段 LSE）与 `Output_partial`（每段已段内归一化的部分输出），用在线 log-sum-exp 把它们合并成全局 softmax 的最终 `Output`。

合并的关键观察是：`Output_partial[s]` 是「段内条件 softmax 加权和」$\sum_{j\in s} p_{j|s} V_j$，其中 $p_{j|s}=\exp_2(\ell_j-m_s)/Z_s$。要把它升级成全局权重，需要乘上「该段所有 key 的全局权重之和」$\sum_{j\in s}\exp_2(\ell_j-M)/Z = \exp_2(\mathrm{lse}_s-M)$（其中 $M$ 是全局最大缩放 logit）。于是合并公式变成：把每个 `Output_partial[s]` 按系数 $\exp_2(\mathrm{lse}_s - \mathrm{LSE}_{global})$ 加权求和。

#### 4.3.2 核心流程

combine 对每个 (head, batch) 配一个 thread block，做三步：

1. **求最大 LSE**：扫一遍 `num_split` 个 `glse` 取最大，记 `lse_max`（数值稳定锚点）。
2. **算全局 LSE**：
   \[
   \mathrm{LSE}_{global} \;=\; \mathrm{lse\_max} + \log_2\!\Big(\sum_s \exp_2(\mathrm{lse}_s - \mathrm{lse\_max})\Big)
   \]
3. **加权合并输出**：对每个 split，
   \[
   O \;\mathrel{+}=\; \mathrm{Output\_partial}[s] \cdot \exp_2(\mathrm{lse}_s - \mathrm{LSE}_{global})
   \]

这三步在源码里就是 `lse_max` 循环 → `lse_logsum` 累加 + `log2` → 输出加权累加。

#### 4.3.3 源码精读

[benchmark_mla_decode_amd_tilelang.py:164-199](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L164-L199) —— 整个 `combine` 宏。第 170 行 `T.Kernel(heads, batch, threads=128) as (by, bz)`：注意命名——第一轴是 `heads` 却叫 `by`、第二轴是 `batch` 却叫 `bz`，所以 `glse[bz,by,k]` 即 `glse[batch,head,split]`，与张量形状 `[batch,heads,num_split]` 对齐。

[benchmark_mla_decode_amd_tilelang.py:185-186](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L185-L186) —— **第一步**：`T.serial(num_split)` 串行扫一遍，`lse_max_local[0] = max(lse_max_local, glse[bz,by,k])`。初始为 $-\infty$。

[benchmark_mla_decode_amd_tilelang.py:187-190](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L187-L190) —— **第二步**：`T.Pipelined(num_split, num_stages=1)` 软流水累加 `lse_logsum_local += exp2(glse[k] - lse_max)`，再 `lse_logsum_local = log2(lse_logsum_local) + lse_max`，得到全局 LSE。注意这里用 `exp2/log2`（base-2），与 `flash_attn_split` 写出 LSE 时用的 base 一致。

[benchmark_mla_decode_amd_tilelang.py:191-199](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L191-L199) —— **第三步**：对每个 split，取 `po_local[i] = Output_partial[bz,by,k,i]`，算 `scale_local = exp2(glse[k] - lse_logsum)`（即 $\exp_2(\mathrm{lse}_s-\mathrm{LSE}_{global})$），再 `o_accum_local[i] += po_local[i]*scale_local`。最后第 198-199 行把累加结果写回 `Output[bz,by,i]`。

把第二步、第三步的 `exp2` 项对比一下就能验证正确性：

- 第二步里被减的是 `lse_max`（局部锚点）；
- 第三步里被减的是 `lse_logsum`（全局 LSE）；
- 二者差 $\mathrm{lse\_max}-\mathrm{LSE}_{global}=-\log_2(\sum_s\exp_2(\mathrm{lse}_s-\mathrm{lse\_max}))$，正是把「以 lse_max 为参考」换算成「以全局 LSE 为参考」的归一因子，保证最终结果是严格的全局 softmax。

#### 4.3.4 代码实践

**实践目标**：亲手把 combine 的三步合并数学跑通一个最小数值例子，验证它等价于直接对全序列做 softmax。

**操作步骤（纸上验证型，无需 GPU）**：

1. 设 `num_split=2`、`dim=2`（每段输出 2 维），并人为给定：
   - 段 0：`glse[0]=10`，`Output_partial[0] = [1.0, 2.0]`
   - 段 1：`glse[1]=12`，`Output_partial[1] = [3.0, 4.0]`
2. 按源码三步算：
   - 第一步 `lse_max = max(10,12) = 12`；
   - 第二步 `lse_logsum = log2(exp2(10-12)+exp2(12-12)) + 12 = log2(0.25+1)+12 ≈ 12.3219`；
   - 第三步 `scale[0]=exp2(10-12.3219)≈0.2`、`scale[1]=exp2(12-12.3219)≈0.8`；
   - 合并 `O = [1.0,2.0]*0.2 + [3.0,4.0]*0.8 = [2.6, 3.6]`。
3. 把这个例子对应回源码 [第 185-199 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L185-L199)，确认每个变量名。

**需要观察的现象**：`scale[0]+scale[1] = 0.2+0.8 = 1.0`——即各 split 的合并系数之和恰为 1。这不是巧合：因为 `Output_partial` 已段内归一，combine 的系数正好把「各段在全局权重中的占比」分摊回去，系数和必为 1。

**预期结果**：手算 `O=[2.6,3.6]`；且对于任意 `dim` 维度，合并系数和恒为 1。这印证「combine = 把段内条件 softmax 按段权重重新加权」的正确性。

> 待本地验证：在 MI300x 上跑 `python benchmark_mla_decode_amd_tilelang.py --auto_tune` 时，第 304 行 `torch.testing.assert_close(tilelang_output, ref_output, rtol=0.01, atol=0.01)` 应通过，说明 split→combine 的合并与 `ref_program`（直接全序列 softmax）数值一致。

#### 4.3.5 小练习与答案

**练习 1**：combine 里第二步用 `T.Pipelined(num_split, num_stages=1)`，而第一步和第三步用 `T.serial(num_split)`。为什么第二步能流水、而一/三步不能？
**答案**：第二步是「累加 `exp2(glse[k]-lse_max)`」，依赖的 `lse_max` 已在第一步算定，每次迭代互不依赖，可以软流水隐藏访存；第一步取 max 虽也可结合，但代码写成串行 reduce（依赖前一次的 `lse_max_local`）；第三步每个 split 还要先取 `Output_partial` 再 `exp2` 再累加，且与第二步共享 `lse_logsum`（必须等第二步完成），整体写成串行更直观。这是实现选择，非唯一正确写法。

**练习 2**：如果某个 split 的 `glse` 极小（该段所有 key 权重几乎为 0），combine 会怎样？
**答案**：`exp2(极小 - lse_max) ≈ 0`，第二步贡献可忽略、第三步 `scale≈0`，该段 `Output_partial` 被乘 0 丢弃。这正是 split-KV 的数值稳定性来源——冷段自动归零，不会污染全局结果。

---

### 4.4 T.annotate_layout 与 T.Fragment 布局标注

#### 4.4.1 概念说明

`combine` 内核里有一处本仓库独有的写法：

```python
T.annotate_layout({
    lse_logsum_local: T.Fragment(lse_logsum_local.shape, forward_thread_fn=lambda i: i),
})
```

`lse_logsum_local` 是一个形状 `[1]` 的 `alloc_local`（每个线程私有的寄存器标量缓冲），用来做第二步的全局 LSE 累加。`T.annotate_layout` 给编译器额外提示：这个局部缓冲的逻辑下标应当如何映射到物理线程。`T.Fragment(shape, forward_thread_fn=...)` 中的 `forward_thread_fn=lambda i: i` 表示「逻辑下标 `i` 映射到线程 `i`」。对 `[1]` 缓冲即「下标 0 的标量落在 0 号线程」。

为什么需要这种标注？`combine` 用 `threads=128` 启动，但 LSE 累加是一份标量状态，需要在一个**确定**的线程上随串行/流水循环连贯地更新；若编译器把这份标量随意分布到不同线程，跨迭代的状态传递会出错。显式 `forward_thread_fn` 就是把这份标量「钉」在确定的线程布局上，保证串行累加的语义连贯。

> 说明：全仓搜索显示 `forward_thread_fn` 与 `T.Fragment(...)`（注意区别于 blocksparse 内核里的 `T.FragmentBuffer`，那是另一种缓冲分配原语）**仅本文件使用**。仓库内未提供其文档，上述「逻辑下标→线程」的语义是据命名、用法与 `T.annotate_layout`（同文件 mha 测试里还出现过 `make_swizzled_layout` 这类布局提示，见 [mha_benchmark/test_tilelang_mha.py:88](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mha_benchmark/test_tilelang_mha.py#L88)）推断。**精确的线程分配行为待确认**——可在本机用 `tilelang.disable_cache()` + `get_kernel_source()` 打印出生成的 HIP 源码核对其落地方式。

#### 4.4.2 核心流程

`T.annotate_layout` 的一般用法：传入一个 `{缓冲: 布局描述}` 字典，对若干缓冲指定布局。本内核只标了 `lse_logsum_local` 一个标量缓冲，其余缓冲（`po_local`、`o_accum_local` 等以及它们在 `T.Parallel(dim)` 里的分布）交给编译器默认分配。

#### 4.4.3 源码精读

[benchmark_mla_decode_amd_tilelang.py:170-199](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L170-L199) —— `combine` 全貌。第 171-176 行声明多个 `alloc_local`/`alloc_fragment` 缓冲；其中 `lse_logsum_local`（`[1]`，accum_dtype）就是要被标注的那个。

[benchmark_mla_decode_amd_tilelang.py:178-180](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L178-L180) —— 本模块焦点：`T.annotate_layout({lse_logsum_local: T.Fragment(..., forward_thread_fn=lambda i: i)})`。其后的第 182-190 行正是依赖这份标量被「钉」在确定线程上，才能让 `lse_logsum_local[0] += ...` 在串行/流水循环里语义连贯。

#### 4.4.4 代码实践

**实践目标**：体会「标量累加缓冲为何需要布局标注」。

**操作步骤（源码阅读 + 思考实验型）**：

1. 读 [第 171-199 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L171-L199)，找到所有 `alloc_local([1], ...)` 的标量缓冲（`lse_local_split`、`lse_logsum_local`、`lse_max_local`、`scale_local`）。
2. 思考：为什么 `o_accum_local`（`[dim]`）和 `po_local`（`[dim]`）在 `T.Parallel(dim)` 下天然线程分布、不需标注，而 `lse_logsum_local`（`[1]`）需要？
3. （可选，需 MI300x）在 `__main__` 里 `kernel = tilelang.compile(...)` 后加一行 `print(kernel.get_kernel_source())`，在生成的 HIP 源码里找 `lse_logsum_local` 对应的寄存器，核对其是否被固定在某个线程上。

**需要观察的现象**：`[dim]` 缓冲与 `T.Parallel(dim)` 配对，编译器知道「dim 个元素分给 dim 个线程」；而 `[1]` 缓冲与 `T.serial`/`T.Pipelined` 配对，没有并行下标可分配，编译器需要一个明确锚点，于是用 `forward_thread_fn` 指定。

**预期结果**：`lse_logsum_local` 被标注、其余 `[1]` 缓冲未标注。一个值得追问的点：`lse_max_local`、`scale_local` 同为 `[1]` 却没标注——这表明要么编译器对未标注的 `[1]` 缓冲有默认「线程 0」策略，要么存在历史/冗余。**该点待本地用 `get_kernel_source()` 验证**。

#### 4.4.5 小练习与答案

**练习 1**：`T.Fragment` 与 blocksparse 内核里的 `T.FragmentBuffer` 是同一个东西吗？
**答案**：不是。`T.FragmentBuffer([shape], dtype)`（见 [blocksparse 内核](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/blocksparse_attention/1.tilelang-benchmark/benchmark_tilelang_bsa.py#L72)）是一种**缓冲分配类型**（fragment 类型的缓冲，与 `alloc_shared`/`alloc_local` 并列）；本讲的 `T.Fragment(shape, forward_thread_fn=...)` 是 `T.annotate_layout` 字典里的**布局描述**，作用于已分配的缓冲。二者只是名字相近。

**练习 2**：如果把 `forward_thread_fn=lambda i: i` 改成 `lambda i: 0`，对 `[1]` 缓冲有区别吗？
**答案**：对 `[1]` 缓冲（只有下标 0），`i` 只能取 0，所以 `lambda i: i` 与 `lambda i: 0` 等价，都把标量放在 0 号线程。差异只有在缓冲长度 >1 时才显现（`lambda i: i` 会把下标 i 散到线程 i，`lambda i: 0` 会全压到线程 0）。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，画一张「一个 (batch, head) 的查询从输入到最终 `Output`」的完整数据流，并解释 split→combine 如何与 `ref_program` 等价。

**建议步骤**：

1. **读驱动入口**：读 [benchmark_mla_decode_amd_tilelang.py:275-307](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L275-L307) 的 `__main__`，理清「`flashmla_decode(...)` 返回一个 `program` → `tilelang.compile(program, out_idx=[6])` 编译 → `get_profiler` 计时 + `ref_program` 校验」的主线。注意 `out_idx=[6]` 指向 `main_split`/`main_no_split` 的第 7 个参数 `Output`。
2. **理清分发**：读 [第 226-229 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L226-L229)：`num_split>1` 返回 `main_split`（[第 201-212 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L201-L212)，内部先 `flash_attn_split` 再 `combine`），否则返回 `main_no_split`（[第 214-224 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L214-L224)，只调 `flash_attn`）。
3. **写出两路 gemm**：在数据流图上标注 `Q@KV^T` 与 `Q_pe@K_pe^T` 都汇入 `acc_s`，而 `P@KV` 只用 `KV`。
4. **画出 split-KV**：在 `flash_attn_split` 这一支画出 KV 被切成 `num_split` 段，每段产出 `(glse[..,bz], Output_partial[..,bz,:])`。
5. **画出 combine**：把所有 split 的 `glse` 经 `lse_max → exp2 → log2` 合并成全局 LSE，再把各 `Output_partial` 按系数 `exp2(glse-LSE)` 加权求和得 `Output`。
6. **对照参考实现**：读 [第 232-272 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L232-L272) `ref_program`：它把 `Q/KV` 与 `Q_pe/K_pe` `concat` 后一次性 `einsum` 算全序列 softmax。说明 `ref_program` 是「直接全序列算」，`main_split` 是「切分并行算再合并」，二者在数学上等价、由第 304 行 `assert_close(rtol=0.01, atol=0.01)` 校验。

**交付物**：一张数据流图 + 一段说明，指出图中哪些边对应「显存写回」（`glse`、`Output_partial`），哪些边是「片上 fragment」；并用一句话解释为什么 `num_split=1` 时不需要 `combine`。

> 提示：可参考 [plot_figure.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/plot_figure.py) 里 TileLang 与 Aiter-ASM（AMD 手调库）在 MI300x 上几乎并驾齐驱（batch=128、kv_ctx=16384 时 TileLang≈138.8 TFlops、aiter≈157.5 TFlops，远超 Triton 的 51.5 与 PyTorch 的 0.55），说明这套 split-KV+combine 的 TileLang 实现已接近 AMD 官方手写汇编库的吞吐。

---

## 6. 本讲小结

- **MLA 的两路 gemm**：attention 打分 = `Q@KV^T`（潜在，`dim=512`）+ `Q_pe@K_pe^T`（解耦 RoPE，`pe_dim=64`），两次 `T.gemm` 累加进同一 `acc_s`；值 $V$ 只用潜在 `KV`，故 `pv_flops` 不含 `pe_dim`。
- **split-KV 并行**：`flash_attn_split` 在网格上多一维 `bz=num_split`，每 block 只算 `seqlen_kv/num_split` 长，把 decode 阶段的串行长 KV 拆成宽并行；段末写出 `glse`（段 LSE）与 `Output_partial`（段内已归一化的部分输出）。
- **combine 合并**：三步 `lse_max → exp2 → log2` 实现在线 log-sum-exp 合并；合并系数 `exp2(glse-LSE_global)` 把段内条件 softmax 升级为全局 softmax，系数和恰为 1。
- **glse 的内容**：`glse[s] = scores_max*scale + log2(logsum)`，是 base-2 的段 LSE；其绝对偏移在 combine 的 `lse_max` 差值中被消掉，故段内 `scores_max` 是否严格段最大不影响正确性。
- **T.annotate_layout / T.Fragment**：本仓独有写法，给 `[1]` 标量累加缓冲 `lse_logsum_local` 标注线程布局（`forward_thread_fn=lambda i: i`），保证串行/流水累加语义连贯；精确线程落地行为待 `get_kernel_source()` 本地确认。
- **两种主路径**：`num_split>1` 走 `main_split`（split→combine 两阶段），`num_split==1` 走 `main_no_split`（直接 `flash_attn` 一阶段），由 [第 226-229 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L226-L229) 分发。

## 7. 下一步学习建议

- **下一讲 u6-l21（tvm.tl 变体与卷积 AMD/HIP）**：本讲的 MLA 内核用的是**独立 `tilelang` 包**（`import tilelang`、`import tilelang.language as T`、`@T.prim_func`），而下一讲会看到 CDNA 上另一批内核走 **TVM 内置 `tvm.tl`**（`from tvm import tl; tvm.tl.language as T`、`target="hip"`）。建议对比两套 API 的 import 与 target 差异，理解本项目「同一架构、两套 TileLang 风格 API 并存」的现状。
- **u6-l22（显式 AutoTuner API 与多内核组合）**：本讲的 `AutoTuner.from_kernel(kernel, configs).set_compile_args(...).run(...)`（[第 336-341 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/1.tilelang_benchmark/benchmark_mla_decode_amd_tilelang.py#L336-L341)）就是「显式 AutoTuner」的典型例子，且 `main_split`/`main_no_split` 两个 `@T.prim_func` 按条件返回正是「多内核组合」。下一讲会系统讲清它与装饰器式 `@autotune` 的关系。
- **延伸阅读**：对照 [0.aiter_benchmark](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/0.aiter_benchmark/) 的 AMD 官方 Aiter-ASM 实现、[2.triton_benchmark/benchmark_mla_decode_amd_triton.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/cdna_benchmark/mla_benchmark/2.triton_benchmark/benchmark_mla_decode_amd_triton.py) 的 Triton 版，理解同一 MLA decode 在三种框架下如何各自实现 split-KV。
