# SplitKV 与 Combine Kernel

## 1. 本讲目标

本讲解决一个具体的工程问题：**当一条序列很长、KV 序列动辄上万时，单块 GPU 上的 thread block 不够多，注意力算子喂不饱 GPU，怎么办？**

FA4 的答案是 **SplitKV（KV 切分）**：把一条序列的 KV 维切成 `num_splits` 段，让多个 thread block 各算一段，每个 block 产出一份**部分输出 \(O_s\) 与部分 log-sum-exp \(\text{LSE}_s\)**，再用一个独立的 **Combine kernel** 把它们合并成最终结果。

学完本讲，你应当能够：

- 说清楚 SplitKV 为什么能提升长序列/解码场景的吞吐，以及它受哪些约束（只在 Blackwell 上支持、与 2CTA 互斥等）。
- 推导出「用 LSE 把多份部分结果合并」的 log-sum-exp 数学，并理解为何它是**精确合并**而非近似。
- 读懂 `flash_fwd_combine.py` 的七步流水，把数学公式逐行对到源码上。
- 知道 `num_splits` 是怎么自动选的（`num_splits_heuristic`），以及 `log_max_splits` 如何决定 combine kernel 的编译特化。

## 2. 前置知识

本讲默认你已经掌握以下内容（均为前置讲义的核心结论）：

- **在线 Softmax 与 LSE（u4-l1）**：前向 kernel 在每个 Q tile 上维护行最大值 \(m\)、行和 \(\ell\)，结束时把 \(\ell\) 改写成 \(\text{LSE}=\ln(\ell)+m\cdot\text{softmax\_scale}\)，它正是本讲合并需要的「部分统计量」。
- **BlockInfo 的 n_block 范围（u3-l2）**：前向主循环遍历的是半开区间 \([n\_block\_min, n\_block\_max)\)；SplitKV 做的就是把这个区间等分给若干 split。
- **Ampere 前向主循环（u6-l1）**：Q 常驻、K/V 流水，主循环跑完后再 `finalize` 把输出归一化、写出 LSE。SplitKV 下，**每个 split 各跑一遍这条完整流水**，只是它看到的 n_block 范围被裁成了其中一段。
- **编译特化与 compile_key（u2-l1/u11-l1）**：`is_split_kv`、`num_splits`、`log_max_splits` 都会进入缓存键，改变它们会触发重编译。

补充一个直观的并行性概念：GPU 由若干个 **SM（Streaming Multiprocessor）** 组成，每个 SM 同时跑一个或多个 **thread block（CTA）**。当一个算子的「工作块」总数少于 SM 数时，部分 SM 会闲置，算子就**喂不饱 GPU**（occupancy 不足）。长序列解码（seqlen_q 很小、seqlen_k 很大）正是这种典型场景——Q 行太少导致工作块不够，这时就需要把 KV 切开来「制造」更多工作块。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_attn/cute/flash_fwd_combine.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py) | **Combine kernel 主体**：`FlashAttentionForwardCombine` 类，把多份 \(O_s/\text{LSE}_s\) 用 log-sum-exp 合并成最终 \(O/\text{LSE}\)。 |
| [flash_attn/cute/interface.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py) | **分发与配置**：`num_splits_heuristic` 自动选切分数；`_flash_attn_fwd` 里决定是否切分、分配 fp32 部分缓冲、调用前向 kernel、再调用 `_flash_attn_fwd_combine`；公开的 `flash_attn_combine` 供用户手动合并。 |
| [flash_attn/cute/block_info.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py) | `get_n_block_min_max` 在 `is_split_kv` 下把 n_block 区间等分给每个 split。 |
| [flash_attn/cute/flash_fwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel，FA4 中**唯一**支持 SplitKV 的前向实现：把 `split_idx` 编进 work tile，每个 split 写出自己的 \(O_s/\text{LSE}_s\)。 |

> 本讲的 SplitKV 特性在 FA4 中**仅 Blackwell（SM100/SM110）前向 kernel 支持**，Ampere（SM80）、Hopper（SM90）、SM120 均会断言失败，详见 4.1.3。

## 4. 核心概念与源码讲解

### 4.1 KV 切分与多 block 并行

#### 4.1.1 概念说明

回顾前向 kernel 的工作划分：一个「work tile」由 `(batch, head, m_block)` 唯一确定，负责算 Q 的一个行块对**整条** K/V 序列的注意力。当序列很长、尤其是**解码场景**（seqlen_q 很小，比如 =1）时：

- m_block 的数量 ≈ seqlen_q / tile_m 很少；
- 于是 `total_mblocks = batch × num_head_kv × num_m_blocks` 远小于 SM 数；
- 大量 SM 闲置，而每个 block 又要串行扫完整条几万长的 KV，**单 block 工作量巨大却并行不起来**。

SplitKV 的思路：**既然 Q 行不够分，就把 K/V 维也切开分**。把整条 KV 的 n_block 区间 \([0, n\_block\_max)\) 等分成 `num_splits` 段，第 \(s\) 个 split 只负责第 \(s\) 段 n_block。这样：

- 工作块数量从 `total_mblocks` 放大到 `total_mblocks × num_splits`，能把空闲的 SM 用起来；
- 每个 block 只扫 KV 的 \(1/\text{num\_splits}\)，单块耗时下降；
- 代价是每个 split 只能看到「部分 KV」，算出的不是最终注意力，而是一份**部分结果**，需要第二轮合并。

打个比方：原来 1 个工人搬完整条传送带上的货；现在雇 `num_splits` 个工人各搬一段，最后由一个「统计员」（Combine kernel）把每个人的账本汇总。

#### 4.1.2 核心流程

SplitKV 的端到端流程是「**两轮 kernel**」：

```text
                      前向 kernel（每个 split 一份工作块）
   Q, K, V  ──────────────────────────────────────────────────►  每个split各跑一遍
                                                                  完整的前向流水（只是 n_block
                                                                  范围被裁成第 s 段）
                                                                       │
                                                                       ▼
                                              out_partial[num_splits, ..., head_dim_v]  (fp32)
                                              lse_partial[num_splits, ..., 1]          (fp32)
                                                                       │
                                                                       ▼
                      Combine kernel（FlashAttentionForwardCombine）
   out_partial, lse_partial  ──────────────────────────────────►  最终 out[..., head_dim_v]
                                                                   最终 lse[..., 1]
```

关键设计点：

1. **每个 split 跑完整流水**：prologue → 主循环（只遍历自己的 n_block 段）→ finalize。finalize 照常把输出按本 split 的行和 \(\ell_s\) 归一化、写出本 split 的 \(\text{LSE}_s\)。所以 \(O_s\) 是「在 split \(s\) 内部已经 softmax 归一化」的输出。
2. **部分输出用 fp32**：合并时要跨 split 累加，fp32 用于避免精度损失。
3. **空 split 安全**：当 n_block 数不足以整除 num_splits，某些 split 段为空（`n_block_min >= n_block_max`），前向主循环直接跳过，combine 也会跳过。

#### 4.1.3 源码精读

**(1) 公共 API 暴露 `num_splits`。** `flash_attn_func` 把它作为公开参数透传下去，默认 `1`（不切分）：

[interface.py:L2709-L2721](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2709-L2721) —— `flash_attn_func` 签名，`num_splits: int = 1`。

**(2) 自动选择切分数。** 当用户传 `num_splits < 1`（即「让我自动选」）时，调用启发式：

[interface.py:L567-L568](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L567-L568) —— 触发自动选择。

[interface.py:L260-L272](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L260-L272) —— `num_splits_heuristic`，逻辑很直白：

- KV 块太少（`num_n_blocks <= 4`）直接返回 1，不切（注释举了 hdim=128、seqlen_k=512 的例子，切了反而增加 combine 开销）；
- 否则取 `min(num_SMs // total_mblocks, max_splits=128, num_n_blocks)`，即「把 SM 填满为止，但不超过 KV 总块数，也不超过 128」。

**(3) 决定切分、分配 fp32 部分缓冲。**

[interface.py:L580-L583](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L580-L583) —— `is_split_kv = num_splits > 1`，并分配两个 fp32 缓冲：`out_partial` 形状 `(num_splits, ..., head_dim_v)`、`lse_partial` 形状 `(num_splits, ...)`。注意 line 570 的注释明确写了「SplitKV uses float32 partial output, which doubles the O buffer size」。

**(4) SplitKV 与其它高级特性互斥。**

[interface.py:L585-L599](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L585-L599) —— `use_2cta_instrs` 的判定条件里包含 `and not is_split_kv`（line 590），即**开了 SplitKV 就不能用 2CTA 指令**。这是当前实现的一项约束。

**(5) 仅 Blackwell 支持。** 分发处对其它架构显式断言：

- [interface.py:L825](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L825) —— `assert not is_split_kv, "SplitKV not supported on SM 8.0"`
- [interface.py:L844](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L844) —— `assert not is_split_kv, "SplitKV not supported on SM 9.0"`
- [interface.py:L944](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L944) —— `assert not is_split_kv, "SplitKV not supported on SM 12.0 in this PR"`

**(6) 前向 kernel 把 `is_split_kv` 传给 kernel 类。** Blackwell kernel 构造时把它作为编译期 `Constexpr` 注入：

[interface.py:L915-L930](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L915-L930) —— 构造 `FlashAttentionForwardSm100` 时传入 `is_split_kv=is_split_kv`（line 921）；同时 line 930 还可看出 `is_split_kv` 也会关闭 persistent 调度。

**(7) 每个 split 写到 out_partial 的第 s 维。** 在 kernel 内部，输出布局按 split 维转置，`num_splits` 直接取自输出张量的第 0 维：

[flash_fwd_sm100.py:L425-L432](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L425-L432) —— `is_split_kv` 时把 `num_splits = mO.shape[0]`，否则 `num_splits = Int32(1)`。

**(8) work tile 解包出 split_idx，并用它计算本 split 的 n_block 范围。**

[flash_fwd_sm100.py:L1363](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L1363) —— `m_block, head_idx, batch_idx, split_idx = work_tile.tile_idx`，即 split_idx 被编进了 tile 调度坐标。

[flash_fwd_sm100.py:L1473-L1476](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_sm100.py#L1473-L1476) —— 调 `block_info.get_n_block_min_max(seqlen, m_block, split_idx, num_splits)` 拿到本 split 的范围；随后 `if n_block_min < n_block_max` 才进主循环，空 split 直接跳过。

**(9) 区间等分的具体算法** 在 BlockInfo 里：

[block_info.py:L47-L55](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/block_info.py#L47-L55) —— 在因果/滑窗裁剪出完整范围 \([n\_block\_min, n\_block\_max)\) 之后，按向上取整等分：

```text
num_n_blocks_per_split = ceil(n_block_max - n_block_min, num_splits)
n_block_min = n_block_min + split_idx * num_n_blocks_per_split
n_block_max = min(n_block_min + num_n_blocks_per_split, n_block_max)  # 末段不越界
```

注意这是**向上取整**，所以末尾几个 split 可能为空（由上面的 `n_block_min < n_block_max` 判掉）。

**(10) 前向算完后调用 combine。**

[interface.py:L1060-L1061](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L1060-L1061) —— 前向 kernel 的输出指针：不切分时写 `out`，切分时写 `out_partial`；LSE 同理写 `lse_partial`。

[interface.py:L1090-L1098](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L1090-L1098) —— `is_split_kv` 时紧接着调用 `_flash_attn_fwd_combine(out_partial, lse_partial.transpose(-1,-2), out, ...)` 完成合并。

#### 4.1.4 代码实践

**实践目标**：直观感受「Q 行少时切分能放大并行度」，并验证切分前后数学等价。

> ⚠️ 这一步**需要 Blackwell（SM100/SM110）GPU**。在没有 Blackwell 的机器上，FA4 会在分发时 `assert not is_split_kv` 报错；此时请改做 4.2.4 的纯 PyTorch 模拟实践。

操作步骤（示例代码，**未在本机运行过**）：

```python
# 示例代码：需在 Blackwell GPU 上运行
import torch
from flash_attn.cute import flash_attn_func

torch.manual_seed(0)
batch, seqlen_q, seqlen_k = 1, 16, 8192      # 解码式：Q 很短，KV 很长
nheads, nheads_kv, d = 8, 2, 128
q = torch.randn(batch, seqlen_q, nheads, d, dtype=torch.bfloat16, device="cuda")
k = torch.randn(batch, seqlen_k, nheads_kv, d, dtype=torch.bfloat16, device="cuda")
v = torch.randn_like(k)

# num_splits=1：不切分
out1, lse1 = flash_attn_func(q, k, v, causal=True, num_splits=1, return_lse=True)
# num_splits=4：把 KV 切成 4 段，4 倍工作块，再 combine
out4, lse4 = flash_attn_func(q, k, v, causal=True, num_splits=4, return_lse=True)

print(out1.shape, lse1.shape)            # lse 形状恒为 (batch, nheads, seqlen_q)
print((out1 - out4).abs().max().item())  # 期望：bf16 舍入量级
print((lse1 - lse4).abs().max().item())  # 期望：接近 0
```

**需要观察的现象 / 预期结果**（待本地验证）：

1. 两次输出的形状一致，`lse` 形状都是 `(batch, nheads, seqlen_q)`、dtype 为 `float32`。
2. `out1` 与 `out4` 最大误差在 bf16 的舍入量级（\(10^{-2}\sim10^{-3}\)），`lse1` 与 `lse4` 几乎相等——证明切分**只改并行实现、不改数学结果**。
3. 用 `torch.cuda.synchronize()` + 计时对比两次耗时：在 seqlen_q 很小、seqlen_k 很大时，`num_splits=4` 通常更快（更充分利用 SM）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_splits_heuristic` 在 `num_n_blocks <= 4` 时直接返回 1？

**参考答案**：KV 总共就 ≤4 个块，切分能放大的并行度有限，却要额外付出一份 fp32 部分缓冲 + 一次 combine kernel 的开销，得不偿失；尤其是 hdim=128、seqlen_k=512 这种本来块就很少的场景。

**练习 2**：在因果掩码 + seqlen_q = seqlen_k 的情形下，不同 m_block 对应的 n_block 数本来就不一样（靠下的 Q 行要扫更多 KV）。SplitKV 把每个 m_block 的 n_block 区间各自等分，这会带来什么负载不均？结合 u8-l2 的 CLC 动态调度思考缓解办法。

**参考答案**：靠下的 Q 行 n_block 多，等分成 num_splits 段后每段更重；靠上的 Q 行 n_block 少，可能根本凑不满 num_splits 个非空 split。于是不同 work tile 的工作量差异被进一步放大，出现「尾延迟」。静态调度下某些 SM 会先空等；CLC 动态调度（见 u8-l2）让 SM 完成一个 tile 后主动领新 tile，能削平这种尾延迟——这也是 Blackwell persistent kernel + CLC 的动机之一。

---

### 4.2 部分 O+LSE 的 log-sum-exp 合并

#### 4.2.1 概念说明

每个 split \(s\) 跑完前向后给出两样东西：

- \(O_s\)：在 split \(s\) 内部已经 softmax 归一化的输出，\(O_s=\sum_{j\in s}\frac{\exp(S_j-m_s)}{\ell_s}V_j\)，其中 \(m_s,\ell_s\) 是本 split 内的行最大值与行和。
- \(\text{LSE}_s\)：本 split 的 log-sum-exp，\(\text{LSE}_s=\ln(\ell_s)+m_s\cdot\text{softmax\_scale}\)（由前向 `finalize` 写出，定义见 u4-l1）。

Combine kernel 的任务：把这些 \((O_s,\text{LSE}_s)\) 合成「把所有 KV 一起算」的最终结果 \((O,\text{LSE})\)。这件事**必须用 log-sum-exp 做，不能直接平均**——因为每个 \(O_s\) 是用各自不同的 \(m_s\) 归一化的，基准不一致。

#### 4.2.2 核心流程（数学推导）

先推合并公式。定义全局行最大值用 LSE 表达：记 \(\text{LSE}_\text{final}=\ln\sum_s \exp(\text{LSE}_s)\)（log-sum-exp over splits）。

对每个 split 取重缩放因子

\[
\alpha_s=\exp(\text{LSE}_s-\text{LSE}_\text{final}),
\]

则最终输出与最终 LSE 为

\[
\boxed{\,O=\sum_s \alpha_s\,O_s,\qquad \text{LSE}=\ln\!\Big(\sum_s \exp(\text{LSE}_s)\Big)\,}
\]

**为什么这是精确合并？** 展开验证（以下用「打分已含 softmax_scale」理解，不影响结论）：

1. \(\exp(\text{LSE}_\text{final})=\sum_s\exp(\text{LSE}_s)\) 是把各 split 的 \(\exp(\ell_s)\) 类统计在「公共基准」下求和，等价于全序列的 \(\sum_j\exp(S_j)\)。
2. 把 \(\alpha_s O_s=\exp(\text{LSE}_s-\text{LSE}_\text{final})\cdot O_s\) 展开，分子里 \(O_s\) 自带的 \(\ell_s\)（来自归一化）与 \(\exp(\text{LSE}_s)\) 里的 \(\ell_s\) 抵消，剩下 \(\exp(m_s-\text{LSE}_\text{final})\sum_{j\in s}\exp(S_j-m_s)V_j=\exp(-\text{LSE}_\text{final})\sum_{j\in s}\exp(S_j)V_j\)。
3. 对 \(s\) 求和：\(O=\exp(-\text{LSE}_\text{final})\sum_j\exp(S_j)V_j=\frac{\sum_j\exp(S_j)V_j}{\sum_j\exp(S_j)}\)，正是全序列 softmax 注意力。

这正是 FlashAttention 号称「exact attention」的延续——合并是**精确**的，误差仅来自 fp 累加舍入（所以部分输出才用 fp32）。

Combine kernel 的实现步骤对应这组公式：

```text
Step 1  把 LSE_partial[num_splits, tile_m] 从 gmem 搬到 smem（带边界 -inf 填充）
Step 2  预取前 stages-1 份 O_partial 到 smem 环形缓冲
Step 3  LSE 从 smem 读到寄存器
Step 4  对每行求 lse_max、各 split 的 scale=exp2(LSE_s - lse_max)*log2(e)、
        再求 lse_sum，写出 LSE_final = log(lse_sum) + lse_max；
        并把 scale 归一化（除以 lse_sum）写回 smem
Step 5  把 LSE_final 写回 gmem
Step 6  流水读 O_partial，按 scale[s] 加权累加到 O
Step 7  把 O 写回 gmem
```

#### 4.2.3 源码精读

Combine kernel 的主体是 `FlashAttentionForwardCombine.kernel`，七步清晰分注。注意它全程用 **`exp2`**（配合 \(\log_2 e\) 做换底）而不是 `exp`，以利用硬件 `ex2` 指令——这和在线 softmax（u4-l1）是同一套数值技巧。

**(1) Step 4：算最终 LSE 与每个 split 的归一化权重**——本讲最核心的一段，直接对应上面的公式。

[flash_fwd_combine.py:L512-L570](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L512-L570) 逐行解读：

- L525-L530：跨 split 求 `lse_max = max_s LSE_s`（用 warp 归约，`init_val=-inf`）。
- L534-L540：顺便求「最大有效 split 下标」`max_valid_split`，用于后面短路掉全是 \(-\infty\) 的尾部分支。
- L548-L552：核心换底——`scale = exp2(LSE_s * LOG2_E - lse_max * LOG2_E)`。因为 `exp2(x*log2(e)) = e^x`，这等价于 `scale = exp(LSE_s - lse_max)`，正是 \(\exp(\text{LSE}_s-\text{lse\_max})\)。同时把 `lse_sum_cur += scale`。
- L553-L556：`lse_sum` 跨线程归约后，`lse_sum[m] = log(lse_sum_cur) + lse_max`，即 \(\text{LSE}_\text{final}=\ln\sum_s\exp(\text{LSE}_s-\text{lse\_max})+\text{lse\_max}=\ln\sum_s\exp(\text{LSE}_s)\)。
- L558-L561：`inv_sum = 1/lse_sum_cur`，把 `scale` 归一化（乘 `inv_sum`）写回寄存器，得到 \(\alpha_s=\exp(\text{LSE}_s-\text{LSE}_\text{final})\)。

**(2) Step 5：把最终 LSE 写回 gmem**（只在 `k_block==0` 时由负责的线程写一次，避免 head_dim 维分块重复写）。

[flash_fwd_combine.py:L576-L592](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L576-L592)。

**(3) Step 6：按 \(\alpha_s\) 加权累加 \(O\)。**

[flash_fwd_combine.py:L612-L639](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L612-L639) —— 主累加循环。关键三处：

- L617：`scale[m] = sLSE[s, ...]`，把 Step 4 写回 smem 的归一化权重 \(\alpha_s\) 读出来。
- L620-L624：流水预取下一份 `O_partial`（`split_to_load = s + stages - 1`）。
- L634-L639：`tOrO += scale[m] * tOrO_partial.to(Float32)`——正是 \(O=\sum_s\alpha_s O_s\)。累加用 fp32，最后在 Step 7 才转回 `dtype`。

**(4) Step 7：转回输出 dtype 并写回 gmem。**

[flash_fwd_combine.py:L641-L665](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L641-L665) —— L645-L646 把 fp32 累加器转成 `dtype`，再按 universal copy 写回 `mO`。

**(5) Step 1-3：数据搬运与边界处理。**

- [flash_fwd_combine.py:L404-L439](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L404-L439) —— Step 1：把 LSE_partial 搬到 smem；L437 把越界 split 填 `-inf`（这样 Step 4 的 `exp(LSE_s - lse_max)` 自然得 0，不贡献）。
- [flash_fwd_combine.py:L441-L492](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L441-L492) —— Step 2：用 cp.async 预取 `stages-1` 份 O_partial，建立环形流水。
- [flash_fwd_combine.py:L494-L510](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L494-L510) —— Step 3：把 LSE 从 smem 读进寄存器，供 Step 4 归约。

**(6) 早退优化：单 split 不走 combine 主体。**

[flash_fwd_combine.py:L398-L402](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L398-L402) —— 当 `num_splits_dynamic_ptr` 表明实际只有 1 个 split 时，跳过整个合并主体。注意：这是为「逐 batch 切分数不同」的动态场景准备的；静态 `num_splits==1` 时 `_flash_attn_fwd` 根本不会调用 combine。

**(7) 手动合并的公开入口。** 用户也可以自己拿一批 \(O_s/\text{LSE}_s\) 调 combine：

[interface.py:L2998-L3041](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2998-L3041) —— 公开函数 `flash_attn_combine(out_partial, lse_partial, ...)`，是 `_flash_attn_fwd_combine` 的用户友好包装，接受 `(num_splits, ...)` 形状的张量并返回合并后的 `(out, lse)`。

#### 4.2.4 代码实践

**实践目标**：用纯 PyTorch 复现「SplitKV 切分 + log-sum-exp 合并」的数学，证明它与一次性 softmax 注意力数值一致。这一步**不需要 GPU**，用来把 4.2.2 的公式吃透。

操作步骤（示例代码，**未在本机运行过**）：

```python
# 示例代码：纯 CPU/PyTorch，验证 combine 的 log-sum-exp 数学
import torch
import math

torch.manual_seed(0)
Nq, Nk, d = 4, 4096, 64
q = torch.randn(Nq, d, dtype=torch.float64)
k = torch.randn(Nk, d, dtype=torch.float64)
v = torch.randn(Nk, d, dtype=torch.float64)
scale = 1.0 / math.sqrt(d)

def full_attention():
    S = (q @ k.T) * scale
    return torch.softmax(S, dim=-1) @ v, torch.logsumexp(S, dim=-1)  # O, LSE

def split_and_combine(num_splits, tile_n):
    # 1) 每个 split 算自己 n_block 段的部分 O_s 与 LSE_s
    Os, LSEs = [], []
    nb = (Nk + tile_n - 1) // tile_n
    per = (nb + num_splits - 1) // num_splits          # ceil 等分，对照 block_info.py
    for s in range(num_splits):
        lo, hi = s * per, min((s + 1) * per, nb)
        ks = k[lo*tile_n:hi*tile_n] if lo < hi else torch.zeros(0, d, dtype=k.dtype)
        vs = v[lo*tile_n:hi*tile_n] if lo < hi else torch.zeros(0, d, dtype=v.dtype)
        if ks.shape[0] == 0:                            # 空 split
            Os.append(torch.zeros(Nq, d, dtype=torch.float64))
            LSEs.append(torch.full((Nq,), -math.inf))
            continue
        Ss = (q @ ks.T) * scale
        Os.append(torch.softmax(Ss, dim=-1) @ vs)      # O_s（已按本 split 归一化）
        LSEs.append(torch.logsumexp(Ss, dim=-1))       # LSE_s
    Os, LSEs = torch.stack(Os), torch.stack(LSEs)      # (num_splits, ...)
    # 2) combine：α_s = exp(LSE_s - LSE_final)，O = Σ_s α_s * O_s
    LSE_final = torch.logsumexp(LSEs, dim=0)           # (Nq,)
    alpha = torch.exp(LSEs - LSE_final[None, :])       # (num_splits, Nq)
    O = (alpha[..., None] * Os).sum(0)
    return O, LSE_final

O_ref, LSE_ref = full_attention()
O_cb, LSE_cb = split_and_combine(num_splits=4, tile_n=128)

print("O max abs err :", (O_ref - O_cb).abs().max().item())
print("LSE max abs err:", (LSE_ref - LSE_cb).abs().max().item())
```

**需要观察的现象 / 预期结果**：在 fp64 下，两者误差应在 \(10^{-12}\) 量级（机器精度），从而验证：

1. 即便 `num_splits` 增大、出现空 split，合并结果与一次性注意力**数学一致**。
2. 改 `num_splits` 为 1/2/4/8 分别跑，误差量级不变——切分数不影响正确性。
3. 把任一 \(\text{LSE}_s\) 换大/换小，观察 \(\alpha_s\) 如何随之变化，体会「log-sum-exp 自动按各 split 的统计量加权」。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 \(O_s\) 平均（`mean_s O_s`）？给一个会算错的反例。

**参考答案**：每个 \(O_s\) 是用各自 \(m_s\) 归一化的，基准不同。极端反例：两个 split，split0 的打分整体远大于 split1，则 \(O_0\) 才是真正主导，平均却给了相同权重。正确做法是按 \(\alpha_s=\exp(\text{LSE}_s-\text{LSE}_\text{final})\) 加权——\(\text{LSE}\) 大的 split（打分整体更高）自动拿到更大权重。

**练习 2**：源码 Step 4 里 `lse_max_cur = 0.0 if lse_max == -inf else lse_max`（L542-L544）这一句在防什么？

**参考答案**：当某一行在所有 split 上都是被完全掩掉的（例如全 -inf 行），`lse_max` 会是 \(-\infty\)。此时直接 `exp2(0 * LOG2_E - (-inf)*LOG2_E)` 会得到 `inf - inf = NaN`。把基准替换成 0.0 后，`lse_sum_cur` 保持 0，`inv_sum` 也被 L559 的 `lse_sum_cur == 0` 判定为 0，输出该行为 0，避免 NaN 污染。这与在线 softmax 里对全 \(-\infty\) 行的安全化处理（u4-l1）思路一致。

---

### 4.3 num_splits 配置

#### 4.3.1 概念说明

`num_splits` 有三层含义，初学者容易混淆：

1. **运行期切分数**：前向 kernel 真正把 KV 切成多少段。由 `num_splits_heuristic`（或用户显式传入）决定，受 `max_splits=128` 与「KV 总块数」夹逼。
2. **缓冲区第 0 维**：`out_partial` / `lse_partial` 的第 0 维就是这个数，combine kernel 从这里读回。
3. **combine kernel 的编译特化上限 `max_splits = 1 << log_max_splits`**：combine 要为「最坏情况下多少个 split」分配 smem（LSE 共享内存布局尺寸 = `max_splits × tile_m`，见 [flash_fwd_combine.py:L181-L183](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L181-L183)）。这个上限取**大于等于 num_splits 的最小 2 的幂**，且最小 16（`log_max_splits>=4`），从而让同一份编译产物能复用于一批相近的 num_splits。

`log_max_splits` 的选择规则（见下方源码）：`max(ceil(log2(num_splits)), 4)`；当 `tile_m==8` 时还要 `max(..., 5)`（即至少 32），注释解释这是为了让 256 个线程每线程读 4 个 float 时能覆盖足够的 split。

#### 4.3.2 核心流程

```text
num_splits（运行期）
   │
   ├── interface.py: num_splits_heuristic(...) 自动选；或用户显式传
   │        └── 受 max_splits=128 与 num_n_blocks 夹逼
   │
   ├── 分配 out_partial/lse_partial 的第 0 维
   │
   └── _flash_attn_fwd_combine:
            log_max_splits = max(ceil(log2(num_splits)), 4)   # tile_m==8 时 >=5
            max_splits     = 1 << log_max_splits              # combine 的编译上限
            compile_key    = (dtype, dtype_partial, head_dim,
                              tile_m, k_block_size, log_max_splits,
                              has_cu_seqlens, has_seqused, has_lse, has_varlen_batch_idx)
```

注意 `compile_key` 里是 **`log_max_splits`（2 的幂档位）而不是确切的 `num_splits`**。所以 num_splits=5 和 num_splits=8 会命中**同一份**编译产物（都落到 `log_max_splits=3→实际 max(3,4)=4` 档，即 max_splits=16），减少重编译次数。

#### 4.3.3 源码精读

**(1) `log_max_splits` 与 `tile_m`/`k_block_size` 的选择。**

[interface.py:L2956-L2966](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2956-L2966) —— combine 的 tile 配置：

- L2958：`k_block_size = 64 if head_dim <= 64 else 128`（head_dim 维分块）。
- L2961：`tile_m` 在 `8/16/32` 中取小，目标是「tile_m 尽量小以最大化并行度」（注释举例：hdim=64 时取 16，配 256 线程、每线程 4 个 float）。
- L2962-L2966：`log_max_splits = max(ceil(log2(num_splits)), 4)`；`tile_m==8` 时再 `max(..., 5)`。

**(2) compile_key 的构成。**

[interface.py:L2971-L2986](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2971-L2986) —— 注意键里是 `log_max_splits` 而非 `num_splits`；键命中失败才调 `_compile_fwd_combine` 用 FakeTensor 编译。

**(3) combine 能否实现的硬约束。**

[flash_fwd_combine.py:L56-L82](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L56-L82) —— `can_implement` 的护栏：`max_splits <= 256`（L77-L79）、`(tile_m * max_splits) % num_threads == 0`（L80，保证 LSE 部分能被线程整除）。结合 interface.py L2955 的 `assert num_splits <= 256`，num_splits 的绝对上限就是 256。

**(4) 逐 batch 动态切分数。**

[flash_fwd_combine.py:L378-L382](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L378-L382) —— combine 支持用 `num_splits_dynamic_ptr[batch]` 读取「每个 batch 实际切了几段」。这是为 varlen/动态图准备的：不同序列长度可以切不同份数，超出部分按 \(-\infty\) 处理。静态场景下这个指针为 `None`，直接用 `mLSE_partial.shape[1]`。

#### 4.3.4 代码实践

**实践目标**：体会「num_splits 落到同一 2 的幂档位 → 复用同一份 combine kernel」。

操作步骤（**源码阅读型实践**，无需运行）：

1. 打开 [interface.py:L2962-L2966](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2962-L2966)，按下表手算 `log_max_splits` 与 `max_splits`（设 `tile_m=16`，即 `k_block_size=128`）：

   | 运行期 num_splits | ceil(log2) | log_max_splits（max(·,4)） | max_splits（1<<） |
   |---|---|---|---|
   | 1 | 0 | 4 | 16 |
   | 4 | 2 | 4 | 16 |
   | 5 | 3 | 4 | 16 |
   | 9 | 4 | 4 | 16 |
   | 17 | 5 | 5 | 32 |
   | 33 | 6 | 6 | 64 |
   | 200 | 8 | 8 | 256 |

2. 预期结论：num_splits ∈ {1,2,…,16} 全部落到同一档（`log_max_splits=4`），共享同一份编译产物；只有跨过 2 的幂边界（17、33、65、129）才会触发新的 combine 编译。
3. 进阶：在 Blackwell 机器上，用 `FLASH_ATTENTION_CUTE_DSL_CACHE_ENABLED=1`（参考 u11-l1）依次跑 num_splits=4 与 num_splits=8，观察第二次是否明显快于第一次——若两次落在同一档位，第二次应命中磁盘缓存、几乎无编译开销。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果用户传入 `num_splits=300`，会怎样？

**参考答案**：`_flash_attn_fwd_combine` 在 [interface.py:L2955](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L2955) 的 `assert num_splits <= 256` 会直接抛错；即便没有这道断言，`can_implement`（L77-L79）也会因 `max_splits > 256` 返回 False 而拒绝编译。所以 num_splits 的硬上限是 256。

**练习 2**：为什么 `tile_m==8` 时要把 `log_max_splits` 抬到至少 5（max_splits≥32）？结合线程数 256 推。

**参考答案**：combine 用 256 个线程按列读 LSE。当 `tile_m==8` 时每行的「列数」就是 split 数；若 max_splits 太小（如 16），线程-数据映射会出现 `tile_m * max_splits = 8*16 = 128` 个元素被 256 线程分，每线程不足一个元素，存在整除与利用率问题（源码注释 `If kBlockM == 8 then the minimum number of splits is 32`）。把档位抬到 32 让 `8*32=256` 被 256 线程整除，满足 `can_implement` 的 `(tile_m*max_splits) % num_threads == 0` 约束，也提高了读 LSE 阶段的利用率。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个小型「SplitKV + Combine 调查报告」：

1. **选场景**：构造一个解码式输入（`seqlen_q` 很小，例如 16；`seqlen_k` 很大，例如 32768），这正是 SplitKV 的主场。
2. **算并行度**：手算 `total_mblocks = batch × num_head_kv × num_m_blocks`，再算 SM 数（B200 约 132，见 [interface.py:L566](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/interface.py#L566)），用 `num_splits_heuristic` 推断自动会选几个 split。
3. **跑对比**（Blackwell）：对 `num_splits ∈ {1, 自动(-1), 4, 16}` 分别计时，记录前向耗时，画出「num_splits vs 耗时」曲线，找到甜点。注意 num_splits 过大时 combine 开销与 fp32 缓冲会反噬。
4. **验正确性**：把每个 num_splits 的 `(out, lse)` 与 `num_splits=1` 的基准对比最大误差，确认都在 fp 舍入量级。
5. **读源码对账**：在 combine kernel 的 [Step 4](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L512-L570) 与 [Step 6](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py#L612-L639) 上各标一行注释，写明哪条指令对应 \(\text{LSE}_\text{final}=\ln\sum_s\exp(\text{LSE}_s)\)、哪条对应 \(O=\sum_s\alpha_s O_s\)。

若没有 Blackwell GPU，用 4.2.4 的纯 PyTorch 模拟替换第 3-4 步，重点完成第 5 步的源码对账。

## 6. 本讲小结

- SplitKV 把一条序列的 KV 维切成 `num_splits` 段、分给多个 thread block 并行算，每个 split 产出一份**已归一化的部分输出 \(O_s\)** 与**部分 log-sum-exp \(\text{LSE}_s\)**（均为 fp32），用来解决长 KV、尤其是解码场景下 SM 喂不饱的问题。
- 合并必须用 log-sum-exp：\(\text{LSE}=\ln\sum_s\exp(\text{LSE}_s)\)、\(\alpha_s=\exp(\text{LSE}_s-\text{LSE})\)、\(O=\sum_s\alpha_s O_s\)；这是**精确合并**，误差仅来自 fp 舍入。
- `FlashAttentionForwardCombine` 是一个独立的七步 kernel：搬 LSE → 预取 O → 求 lse_max/scale/lse_sum → 写 LSE → 按 scale 加权累加 O → 写 O，全程用 `exp2` 换底加速。
- `num_splits` 由 `num_splits_heuristic` 自动选（`min(num_SMs//total_mblocks, 128, num_n_blocks)`，KV 块太少不切）；combine 按 `log_max_splits`（≥4 的 2 的幂档位）编译特化，同档位复用产物，硬上限 256。
- FA4 中 SplitKV **仅 Blackwell（SM100/SM110）前向支持**，且与 2CTA 指令、persistent 调度互斥；空 split 通过 `n_block_min<n_block_max` 与 LSE 填 \(-\infty\) 双重保证安全。

## 7. 下一步学习建议

- **u7-l3（Paged KV Cache）**：分页 KV 是另一种「KV 不连续」场景， Combine 也会出现在跨页累积中，可与本讲对照阅读 `paged_kv.py` 与 combine 的 varlen 路径。
- **u8-l2（Tile Scheduler 与 CLC 动态调度）**：本讲练习 4.1.5 提到的「SplitKV 带来的尾延迟」正是 CLC 动态调度的用武之地，建议接着学。
- **u9-x（反向传播）**：反向同样有 SplitKV 与 combine，且 dQ 的跨 split 累加比前向更复杂（涉及原子加 / 2CTA reduce），是本讲思路在反向的延伸。
- **直接读 [flash_fwd_combine.py](https://github.com/Dao-AILab/flash-attention/blob/1f7ce2f7cb503473559f3d44d575ae05b1ed8557/flash_attn/cute/flash_fwd_combine.py) 全文**：它是 FA3 同名 C++ kernel 的 CuTeDSL 移植（见文件头注释），对照 hopper 版的 C++ 实现阅读能加深对combine 数值技巧的理解。
