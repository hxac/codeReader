# 专用注意力变体

## 1. 本讲目标

本讲是「注意力内核族」的收尾篇。在 [u6-l1](./u6-l1-fmha-prefill.md) 中我们已经搭好了 Flash 多头注意力的骨架——分块 QKᵀ、在线 softmax、causal 掩码、`exp2` 快速路径。本讲不再引入新的「主流程」，而是看 **同一个 FMHA 骨架如何被改造成四种专用变体**，以服务真实大模型里那些「 vanilla FlashAttention 不够用」的场景。

学完本讲，你应当能够：

1. 说清 **attention sink**（注意力汇聚槽）为何能稳定长文本生成，以及它如何被折叠进在线 softmax 的分母。
2. 理解 Gemma 系列的 **soft cap**（`tanh` 软限幅）对注意力 logit 的数学作用，以及它与**对称滑窗**的组合方式。
3. 掌握纯 **sliding window attention（SWA）** 如何把复杂度从 \(O(S^2)\) 降到 \(O(S\cdot W)\)，以及 `exp2`+FTZ 为何走 SFU 硬件快速路径。
4. 理解 **sparse MLA** 如何用一个 `indices` 张量做 top-k 稀疏选取，以及为何稀疏场景下必须在 `exp2` 之后**再次掩码**。

四个变体共同点是：它们只改动 FMHA 骨架里的两个旋钮——**(a) 每个 query 能看到哪些 key（掩码/索引语义）** 与 **(b) logit 在进入 softmax 前如何被变换（soft cap）**——其余在线 softmax、`mma`、`exp2` 机制完全复用 u6-l1。

## 2. 前置知识

### 2.1 在线 softmax 速回顾（来自 u6-l1）

本讲所有内核都基于「行级在线 softmax」。对当前 query 行，维护三个运行态：

- \(m_i\)：到目前为止见到的最大 logit（行最大值）；
- \(l_i\)：到目前为止的 softmax 分母（加权和）；
- \(\text{acc}\)：到目前为止的加权 value 累加器。

每来一个 KV 块，先算出该块 logit 矩阵 `qk`，再做一次「平移—合并」：

\[
m_{ij} = \max(m_i,\ \max(\text{qk})\cdot \text{scale}),\qquad
\alpha = 2^{\,m_i - m_{ij}}
\]

\[
p = 2^{\,\text{qk}\cdot \text{scale} - m_{ij}},\qquad
l_i \leftarrow \alpha\cdot l_i + \sum p,\qquad
\text{acc} \leftarrow \alpha\cdot \text{acc} + p\cdot V
\]

注意 \(\alpha\) 的底数是 2 而非 \(e\)，因为内核用 `ct.exp2`（映射到 GPU 的 SFU 特殊函数单元）代替 `exp`，所以 `scale` 要预先乘上 \(\frac{1}{\ln 2}\)（代码里写作 `INV_LOG_2`）。循环结束后输出 \(o = \text{acc}/l_i\)。这整套流程在四个变体里几乎逐字相同，本讲只标注**它与 u6-l1 的差异点**。

### 2.2 本讲新增的四个概念

| 概念 | 一句话解释 | 解决的问题 |
|---|---|---|
| **attention sink** | 一个可学习的「虚拟 token」，其 logit 永远参与 softmax 分母 | 长文本生成时注意力质量会塌缩到少数近期 token，sink 吸收溢出的注意力质量以稳定生成（StreamingLLM 思想） |
| **soft cap** | 对 logit 做 \(\text{cap}\cdot\tanh(\cdot/\text{cap})\) 限幅 | 把注意力 logit 钳制在 \([-\text{cap},+\text{cap}]\)，防止极端大 logit 让 softmax 变成 hardmax（Gemma2/3 的做法） |
| **sliding window** | 每个 query 只看它前后窗口 \(W\) 内的 key | 把注意力的 \(O(S^2)\) 显存/算力降到 \(O(S\cdot W)\)，支持长上下文 |
| **top-k sparse** | 用一个索引张量显式指定每个 query 只看哪 top-k 个 key | MLA/稀疏注意力里只挑最相关的若干 key，进一步压缩计算量 |

### 2.3 实验内核标记 `@experimental_kernel`

本讲的 swa 与 sparse_mla 内核上方都挂着 `@experimental_kernel` 装饰器（attention_sink 与 gemma 没有挂）。它的作用是：在 `tilegym` 导入时把 `ct.launch` 做 monkey-patch，当某个被标记的内核**第一次启动**时，通过 `warn_once` 打印一次性告警「该内核由外部贡献者提交、尚未经核心团队完整验证」，然后清除标记，之后不再打扰。详见 [experimental.py:L25-L73](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/experimental.py#L25-L73)。这是个**不改内核内部代码**就能加告警的优雅技巧，本身也是值得学习的工程模式。

## 3. 本讲源码地图

| 文件 | 角色 | 是否实验内核 | 是否含 autograd |
|---|---|---|---|
| [src/tilegym/ops/cutile/attention_sink.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py) | 带 sink token + 可选滑窗的 FMHA（prefill） | 否 | 否（纯函数） |
| [src/tilegym/ops/cutile/gemma_attention.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py) | Gemma 专用：soft cap + 对称滑窗 + STAGE 分段 | 否 | 是（仅前向，反向抛 `NotImplementedError`） |
| [src/tilegym/ops/cutile/experimental/swa_attention.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py) | 纯滑窗注意力，O(S·W)，fp16，含 HF 集成辅助 | 是 | 否（纯函数） |
| [src/tilegym/ops/cutile/experimental/sparse_mla.py](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py) | top-k 稀疏 MLA，按索引 gather KV | 是 | 是（仅前向，反向抛 `NotImplementedError`） |

四个算子都经 `@register_impl("<算子名>", backend="cutile")` 注册，统一入口在 `src/tilegym/ops/ops.py` 中对应的 `@dispatch` stub（如 [ops.py:L966-L976](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/ops.py#L966-L976) 的 `swa_attention`）。分发机制回顾见 [u2-l2](./u2-l2-backend-dispatcher.md)。

## 4. 核心概念与源码讲解

### 4.1 attention sink + 滑窗（attention_sink.py）

#### 4.1.1 概念说明

**attention sink（注意力汇聚槽）** 来自 StreamingLLM 的观察：在长文本自回归生成中，softmax 分母几乎全部分配给最近几个 token，导致早期信息被「冲掉」、生成质量崩坏。解决办法是引入一个（或几个）**可学习的虚拟 token**，它的 logit（`sinks`，形状 `(H_kv*G,)`，每个头一个标量）始终参与 softmax 分母，充当注意力的「溢出缓冲池」。

在数学上，带 sink 的 softmax 分母变成：

\[
z = \underbrace{\sum_{j} 2^{\,\text{qk}_j - m}}_{\text{普通 key 贡献 } l_i} \;+\; \underbrace{2^{\,\text{sink} - m}}_{\text{sink 贡献}}
\]

输出仍为 \(o = \text{acc}/z\)。关键技巧：为了让 sink 自然地参与「行最大值」比较，内核把在线 softmax 的运行最大值 **\(m_i\) 初始化为 `sink_scaled` 而非 \(-\infty\)**（见 4.1.3）。此外该算子支持可选的**滑窗**（`sliding_window`/`BANDWIDTH`），限制每个 query 只看窗口内的 key。

#### 4.1.2 核心流程

```
读 start_q（张量，避免 .item() 同步）与 sink 标量
m_i ← sink_scaled          # 关键：从 sink 起步，而非 -inf
l_i ← 0, acc ← 0
算窗口下界 lo = max(0, query_pos - BANDWIDTH)   # BANDWIDTH=0 表示无滑窗
for 每个 KV 块 j in [lo, hi):
    qk = mma(q, K_j)                       # 分块 QK^T
    应用 causal 掩码、越界掩码、(可选) too_old 滑窗掩码
    在线 softmax 平移合并：更新 m_i / l_i / acc
循环结束后:
    z = l_i + 2^(sink_scaled - m_i)        # 把 sink 折进分母
    输出 acc / z
```

#### 4.1.3 源码精读

**(1) sink 折进行最大值——\(m_i\) 从 sink 起步**

[attention_sink.py:L92-L96](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L92-L96)：在线 softmax 三个运行态初始化。`m_i` 加上了 `sink_scaled`（`sink * INV_LOG_2`），这样第一个 KV 块的 `max(m_i, ...)` 比较里天然包含了 sink。

```python
m_i = ct.full((TILE_M, 1), 0.0, dtype=ct.float32) + sink_scaled
l_i = ct.full((TILE_M, 1), 0.0, dtype=ct.float32)
acc = ct.full((TILE_M, TILE_D), 0.0, dtype=ct.float32)
```

**(2) 循环上下界带滑窗语义**

[attention_sink.py:L102-L112](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L102-L112)：`BANDWIDTH > 0` 时把窗口下界 `lo` 抬高到 `query_pos - BANDWIDTH`，从而**在块级别直接跳过太老的 KV 块**；`BANDWIDTH == 0` 时退化为全因果。`Tc`、`start_block` 把 `lo/hi` 换算成块索引。

**(3) 三合一掩码：causal + 越界 + too_old**

[attention_sink.py:L134-L144](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L134-L144)：把未来 key（`offs_n > query_pos`）、越界 key（`offs_n >= N_KV_CTX`）、窗口外的旧 key（`offs_n < query_pos - BANDWIDTH + 1`）统一置为 `-1e6`，`exp2` 后贡献近似为 0。

**(4) sink 折进分母**

[attention_sink.py:L171-L175](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L171-L175)：循环结束后才把 sink 的贡献加进分母 `z = l_i + sink_exp`，再做最终归一化。

```python
sink_exp = ct.exp2(sink_scaled - m_i, flush_to_zero=True)
z = l_i + sink_exp
acc = ct.truediv(acc, z, flush_to_zero=True, rounding_mode=RMd.APPROX)
```

**(5) `start_q` 必须是 GPU 张量——禁止 `.item()`**

[attention_sink.py:L283-L289](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L283-L289)：`start_q` 表示 query 在 KV 序列里的起始偏移（KV-cache 场景下 query 对应较后位置）。注释明确「CRITICAL: avoid `.item()` which causes sync」——如果用 `.item()` 取成 Python 标量会强制 CPU/GPU 同步、抹掉异步流水线，所以这里把它保留成 `int32` 张量，在内核里用 `ct.load` 读出。`sliding_window` 映射成 `bandwidth`（[L291-L292](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L291-L292)）。

#### 4.1.4 代码实践

**实践目标**：验证 sink 与滑窗对注意力范围的影响。

1. 阅读 [tests/ops/test_attention_sink.py:L39-L81](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_attention_sink.py#L39-L81) 的 `reference` 实现，它用 PyTorch 物化了完整掩码，是理解「sink 折进分母」的最权威参照。
2. 在参考实现里定位 `sinks_exp = torch.exp(sinks - logits_or_sinks_max)` 与 `normalizer = unnormalized_scores.sum(...) + sinks_exp`（约 L71-L73），与内核 [L171-L172](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L171-L172) 对照，确认两者数学等价。
3. **待本地验证**：在带 Blackwell/Ampere 的机器上跑 `pytest tests/ops/test_attention_sink.py -k "sliding_window" -v`，观察 `sliding_window=None` 与 `sliding_window=128` 两组用例的覆盖范围差异。

**预期结果**：开滑窗时，对位置 `i` 的 query，参与计算的 key 范围从 `[0, i]` 收缩到 `[i-127, i]`；sink 始终在分母里。

#### 4.1.5 小练习与答案

**练习 1**：为何把 `m_i` 初始化为 `sink_scaled`，而不是先初始化为 \(-\infty\) 再在循环外补算 sink？

**参考答案**：在线 softmax 的 `m_i` 必须是「全局最大值」的无偏估计才能保证 `exp2(qk - m_i)` 不溢出。若 \(m_i\) 从 \(-\infty\) 起步、循环里只看 key 的 logit，最终 \(m_i\) 可能小于 sink 的 logit，那么最后补算 `exp2(sink_scaled - m_i)` 就可能 \(>1\) 甚至爆炸。把 sink 一开始就放进 `m_i`，保证整个合并过程都以「key 与 sink 的联合最大值」为基准，数值上自洽。

**练习 2**：`start_q` 为何禁止用 `.item()`？

**参考答案**：`.item()` 会把单个 GPU 标量拷回主机，强制 CPU 等待 GPU 把队列里所有未完成 kernel 跑完（即 device-to-host 同步），破坏 kernel launch 的异步流水线。保留成张量由内核内部 `ct.load` 读取，偏移量全程留在 GPU 上。

---

### 4.2 Gemma 的 soft cap + 对称滑窗（gemma_attention.py）

#### 4.2.1 概念说明

Gemma2/3 在注意力里加了两样东西：

1. **soft cap（attn_logit_softcapping）**：在 softmax 之前，对 logit 做
   \[
   \text{qk}' = \text{cap}\cdot \tanh\!\left(\frac{\text{sm\_scale}\cdot \text{qk}}{\text{cap}}\right)
   \]
   由于 \(\tanh\in(-1,1)\)，logit 被钳制在 \((-\text{cap},+\text{cap})\)。这避免了某个 logit 过大时 softmax 退化成 one-hot（hardmax），保持梯度健康。
2. **对称滑窗**：每个 query 看它前后 `WINDOW_SIZE` 内的 key（结合 causal 后实际是 `[i-W, i]`）。

此外该内核用 **STAGE 位标志** 把 causal 注意力的循环拆成「非对角块」与「对角块」两段，这是 FlashAttention 经典的分段优化（对角块需要掩码、非对角块的全下三角整块有效，可省掉掩码判断）。

#### 4.2.2 核心流程

```
按 STAGE 决定 KV 循环范围：
  STAGE 1/2：causal——非对角块(STAGE=1)整块无掩码，对角块(STAGE=2)带掩码
  STAGE 3：非 causal——整段循环
for curr_n in [lo, hi) step BLOCK_N:
    qk = mma(q, K_n)                       # K 用 order 转置加载
    if HAS_SOFT_CAP:
        qk = tanh(qk*sm_scale/cap) * cap   # soft cap，sm_scale 在 tanh 内
    应用 causal / 边界 / 对称滑窗掩码
    在线 softmax（exp2）平移合并
最后 acc / l_i
```

#### 4.2.3 源码精读

**(1) soft cap 的四步变换**

[gemma_attention.py:L103-L107](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L103-L107)：注意 `sm_scale` 是在 `tanh` **内部**乘进去的：

```python
if HAS_SOFT_CAP:
    qk = qk * sm_scale
    qk = ct.truediv(qk, SOFT_CAP, flush_to_zero=True, rounding_mode=RMd.APPROX)
    qk = ct.tanh(qk, rounding_mode=RMd.APPROX)
    qk = qk * SOFT_CAP
```

参考实现 [test_gemma_attention.py:L178-L181](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_gemma_attention.py#L178-L181) 用同样的 `p/cap → tanh → *cap` 顺序，两者逐字对应。

**(2) soft cap 改变了 exp2 的缩放基准**

[gemma_attention.py:L122-L123](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L122-L123) 与 [L138-L139](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L138-L139)：有无 soft cap 两条路径，`m_ij` 的缩放因子不同。

```python
# 有 soft cap：sm_scale 已在 tanh 内乘过，这里只需 *INV_LOG_2 转到 log2 空间
m_ij = max(m_i, ct.max(qk, axis=-1) * INV_LOG_2)
qk = qk * INV_LOG_2 - m_ij
```

```python
# 无 soft cap：sm_scale 还没乘，用 qk_scale = sm_scale * INV_LOG_2 一步到位
m_ij = max(m_i, ct.max(qk, axis=-1) * qk_scale)
qk = qk * qk_scale - m_ij
```

这是一个容易看漏的细节：**soft cap 路径里 `sm_scale` 进了 tanh，所以外面的缩放只剩 `INV_LOG_2`**；无 cap 路径则用 `qk_scale`（含 `sm_scale`）。

**(3) 对称滑窗掩码**

[gemma_attention.py:L117-L120](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L117-L120)：窗口判断 `qk_offset = key_pos - query_pos` 落在 `[-W, +W]`，是**双向**窗口（再被 causal 截成 `[i-W, i]`）。

```python
qk_offset = curr_n + offs_n[None, :] - offs_m[:, None]
window_mask = (qk_offset >= -WINDOW_SIZE) & (qk_offset <= WINDOW_SIZE)
qk = ct.where(window_mask, qk, -1.0e6)
```

**(4) STAGE 位标志驱动两段循环**

[gemma_attention.py:L227-L278](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L227-L278)：用 `STAGE & 1` 和 `STAGE & 2` 两个位分别触发「非对角块」（`inner_stage = 4 - STAGE`）和「对角块」（固定 stage=2）两次内层循环调用。`stage` 由主机侧按序列长度决定：`S_qo == 1`（解码）→ `stage=1`；否则 causal → `stage=3`，非 causal → `stage=1`（见 [L410-L413](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L410-L413)）。

**(5) autograd 壳：仅前向**

[gemma_attention.py:L381-L394](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L381-L394) 用 `torch.autograd.Function` 包装，但 [L472-L474](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L472-L474) 的 `backward` 直接 `raise NotImplementedError`——所以**仅支持推理**。`gemma_attention = _GemmaAttentionFunction.apply`（[L477](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/gemma_attention.py#L477)）。

#### 4.2.4 代码实践

**实践目标**：理解 soft cap 的限幅效果与对称窗口的掩码形状。

1. 阅读 [test_gemma_attention.py:L69-L96](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_gemma_attention.py#L69-L96) 的 `_generate_window_mask`：它用 `dist = (k_pos - q_pos).abs() > window_size` 生成掩码，与内核的 `[-W, +W]` 双向判断一致。
2. **思考实验**：取 `soft_cap=50.0`，对一个 logit `qk*sm_scale = 1000` 的位置，手算限幅结果。
3. **待本地验证**：跑 `pytest tests/ops/test_gemma_attention.py -v`，观察 `soft_cap=None` 与 `soft_cap=50.0` 两组用例的正确性容差。

**预期结果**：`cap·tanh(1000/50) = 50·tanh(20) ≈ 50`，即任何超过 `cap` 的 logit 都被压到 `cap` 附近，softmax 不会退化成 hardmax。

#### 4.2.5 小练习与答案

**练习 1**：为何 soft cap 路径里 `m_ij` 用 `INV_LOG_2`，而无 cap 路径用 `qk_scale`？

**参考答案**：有 cap 时 `sm_scale` 已经在 `tanh` 内乘进 logit（`qk*sm_scale/cap`），进入在线 softmax 的 `qk` 已是缩放后的值，只需再乘 `INV_LOG_2` 转到 log2 空间喂给 `exp2`；无 cap 时 `sm_scale` 尚未应用，故用 `qk_scale = sm_scale*INV_LOG_2` 一步完成缩放与换底。

**练习 2**：STAGE 机制把 causal 循环拆成两段的好处是什么？

**参考答案**：causal 注意力的非对角块（完全在 query 行下三角内的 KV 块）所有元素都有效，无需逐元素掩码；只有覆盖对角线的那个块需要掩码。拆成两段后，非对角段省掉了 `where(mask, ...)` 的逐元素判断开销，对角段单独处理，整体更快。这是 FlashAttention 的经典优化。

---

### 4.3 纯滑窗 SWA 与 exp2/FTZ（swa_attention.py）

#### 4.3.1 概念说明

[swa_attention.py:L5-L12](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L5-L12) 的模块注释点明三件事：(1) 每个 query 最多看身后 `W` 个 key（加 causal）；(2) 在线 softmax 用 `exp2`+FTZ（flush-to-zero，亚正规数清零）以充分利用 SFU 硬件；(3) 在 B300 上离线调优得到 `M=64, N=128, occ=2`。

与前两个变体最大的区别：**SWA 在块级别直接跳过窗口外的 KV 块**，而不是「加载整块再逐元素掩码」。这让算力/访存从 \(O(S^2)\) 降到 \(O(S\cdot W)\)。代价是该内核只支持 fp16、且只实现了 prefill（解码回退到 PyTorch SDPA）。

#### 4.3.2 核心流程

```
# 主机侧：把 (B,H,S,D) 拍平成 (B*H*S, D) 的二维瓦片网格，按头算 stride 防越界
# 内核侧：
m_i ← -inf, l_i ← 0, acc ← 0
scale_log2 = qk_scale * INV_LOG_2
算 KV 块范围：
  kv_lo = max(0, q_start - W + 1) // TILE_N     # 跳过窗口左外的整块
  kv_hi = min(总块数, 对角块)                    # causal 截断
for kv_block in [kv_lo, kv_hi):                  # 只遍历窗口内块 → O(S·W)
    qk = mma(q_tile, K^T)
    三合一掩码：trailing window + causal + 序列边界
    exp2 + FTZ 在线 softmax 平移合并
l_i ← max(l_i, 1e-6)        # 全掩码行防 NaN
输出 acc / l_i（降回 fp16）
```

#### 4.3.3 源码精读

**(1) 块级跳窗——\(O(S\cdot W)\) 的来源**

[swa_attention.py:L80-L83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L80-L83)：

```python
kv_lo = max(0, q_start - window_size + 1) // TILE_N
kv_hi = _cdiv(seq_k, TILE_N)
if CAUSAL:
    kv_hi = min(kv_hi, (q_start + TILE_M - 1) // TILE_N + 1)
```

注释直言「this is where the O(S*W) complexity comes from -- we skip blocks entirely outside the window」。对比 attention_sink 是「加载后逐元素掩 `too_old`」，SWA 直接不进入这些块的循环。

**(2) 三合一逐元素掩码**

[swa_attention.py:L99-L104](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L99-L104)：即便块级已跳窗，块内边界仍需逐元素处理——尾部窗口（`offs_n > offs_m - window_size`）、causal 上三角（`offs_n <= offs_m`）、序列长度边界（`offs_n < seq_k`）。

**(3) exp2 + FTZ 在线 softmax**

[swa_attention.py:L108-L116](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L108-L116)：

```python
m_new = ct.maximum(m_i, ct.max(qk * scale_log2, axis=1))
alpha = ct.exp2(m_i - m_new, flush_to_zero=True)
p = ct.exp2(qk * scale_log2 - ct.expand_dims(m_new, axis=1), flush_to_zero=True)
l_i = alpha * l_i + ct.sum(p, axis=1)
p_fp16 = ct.astype(p, ct.float16)        # PV 矩阵乘降回 fp16
acc = ct.expand_dims(alpha, axis=1) * acc + ct.mma(p_fp16, v_tile, ...)
```

`flush_to_zero=True` 把亚正规（denormal）浮点强制当 0，避免硬件处理亚正规数的慢路径，换取 SFU 全速。这是 u6-l1 `exp2` 快速路径的强化版。

**(4) 全掩码行防 NaN**

[swa_attention.py:L121-L123](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L121-L123)：若某 query 行的所有 key 都在窗口外（`l_i=0`），直接除会得 NaN，故先把 `l_i` 钳到 `≥ 1e-6`，使该行输出为 0 而非 NaN。

**(5) 主机侧拍平与 GQA 展开**

[swa_attention.py:L221-L296](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L221-L296)：`tile_swa_attention` 把 `(B,H,S,D)` 拍平成 `(B*H*S, D)` 的二维缓冲，按 `stride_q = cdiv(S_Q, TILE_M)`、`stride_kv = cdiv(S_K, TILE_N)` 计算每个头占多少瓦片，避免瓦片加载越过头部边界（[L248-L269](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L248-L269)）。GQA（如 Mistral 32 Q 头 / 8 KV 头）在主机侧用 `repeat_interleave` 扩展 KV（[L236-L243](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L236-L243)），内核内只看等头数。输入强制 fp16（[L224-L225](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L224-L225)）。

**(6) HuggingFace 集成辅助（解码回退 SDPA）**

[swa_attention.py:L127-L185](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L127-L185)：`get_swa_fmha_interface` 返回一个可替换 `ALL_ATTENTION_FUNCTIONS["sdpa"]` 的包装函数。prefill 走 cuTile SWA 内核，**解码（`q.size(-2)==1`）回退到 PyTorch SDPA**（[L143-L163](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L143-L163)），因为该内核不追踪 KV-cache 的绝对位置。`apply_tilegym_swa_to_mistral`（[L188-L218](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L188-L218)）是给 Mistral 做 monkey-patch 的入口，模式同 [u8-l1](./u8-l1-transformer-monkey-patch.md) 将讲的 `apply_tilegym_kernel_to_llama`。

#### 4.3.4 代码实践

**实践目标**：体会块级跳窗带来的复杂度差异。

1. 阅读 [tests/ops/experimental/test_swa_attention.py:L17-L31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/experimental/test_swa_attention.py#L17-L31) 的 `swa_reference`：它物化了完整 `S×S` 掩码（`mask = j > (i - window_size)` 等），是验证基准。
2. 定位内核的 `kv_lo`/`kv_hi` 计算（[L80-L83](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/swa_attention.py#L80-L83)），对一个 `S=4096, W=512, TILE_N=128` 的场景，手算某 query 块实际进入循环的 KV 块数。
3. **待本地验证**：跑 `pytest tests/ops/experimental/test_swa_attention.py -k "long_context_mistral" -v`（标了 `@slow`），观察 8K 上下文、4K 窗口的场景。

**预期结果**：`S=4096, W=512` 时窗口覆盖 `512/128 = 4` 个 KV 块（加对角块），远少于全序列的 `4096/128 = 32` 块，故算力约为密集注意力的 `4/32 = 1/8`。

#### 4.3.5 小练习与答案

**练习 1**：SWA 的「块级跳窗」与 attention_sink 的「逐元素 `too_old` 掩码」都能限制窗口，区别在哪？

**参考答案**：SWA 在算 `kv_lo` 时直接把窗口外的整块排除在循环之外，根本不加载、不计算这些块，复杂度真正降到 \(O(S\cdot W)\)；attention_sink 是加载整块后再用 `too_old` 逐元素置 `-1e6`，仍要遍历（计算）这些块，只是让它们贡献为 0，复杂度仍是 \(O(S^2)\)。前者更快但要求窗口语义能在块级别确定。

**练习 2**：为何解码（单 token）要回退到 SDPA 而不用 SWA 内核？

**参考答案**：该 SWA 内核是 prefill 内核，按 query 块算 KV 块范围，不维护 KV-cache 的绝对位置信息；解码时 query 只有 1 行且 KV 在 cache 里持续增长，内核的拍平索引与窗口下界逻辑无法直接套用，故回退到成熟的 PyTorch SDPA。

---

### 4.4 sparse_mla：top-k 稀疏选取（sparse_mla.py）

#### 4.4.1 概念说明

`sparse_mla` 把 [u6-l3](./u6-l3-mla-decode.md) 的多潜注意力（MLA）做成**稀疏**版：每个 query 不再扫整条 KV，而是由一个索引张量 `indices`（形状 `[B, S, H_kv, topk]`）显式指定它只看哪 `topk` 个 key。MLA 的联合位置编码（内容 `q·k` + 位置 `qpe·kpe`）原样保留，所以分数仍是：

\[
\text{score} = q\cdot k_{\text{idx}} + q_{pe}\cdot k_{pe,\text{idx}}
\]

区别仅在「key 从哪来」：密集 MLA 用 `ct.load` 按连续块搬，稀疏 MLA 用 `ct.gather` 按 `indices` 数组按下标**散列**取（回顾 [u3-l2](./u3-l2-data-movement.md) 的 gather 语义）。

#### 4.4.2 核心流程

```
m_i ← -inf, l_i ← 0, acc ← 0
载入 q, qpe（TILE_H 个连续头，同一 query 位置）
for 每个索引块 i_i in [0, NI):           # NI = topk / TILE_N
    indices_tile = load(Indices, ...)     # TILE_N 个下标
    gathered_k  = gather(K,  (b, kv_h, s_idx, d_idx))    # 按下标散列取
    gathered_v  = gather(V,  ...)
    gathered_kpe= gather(KPE,...)
    qk  = mma(q,   gathered_k^T)          # 内容相似度
    qk  = mma(qpe, gathered_kpe^T, qk)    # 位置相似度，累加到同一 qk
    causal 掩码：indices_tile <= s_i
    在线 softmax（exp2）平移合并
    关键：exp2 之后再次把掩码位置置 0（见 4.4.3）
acc / l_i
```

#### 4.4.3 源码精读

**(1) 按索引 gather KV**

[sparse_mla.py:L150-L169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L150-L169)：先用 `ct.load` 取一块 `TILE_N` 个下标（连续 tile 访问），再用 `ct.gather` 按这些下标从 K/V/KPE 里散列取值。`s_idx` 是 `[TILE_N,1]`、`d_idx` 是 `[1,TILE_D]`，广播成 `[TILE_N, TILE_D]` 的瓦片——正是 u3-l2 所讲的「索引数组广播」语义。

```python
indices_tile = ct.load(Indices, index=(batch_idx, s_i, off_kv_h, i_i), shape=(1,1,1,TILE_N))
indices_tile = ct.reshape(indices_tile, (TILE_N,))
...
gathered_k = ct.gather(K, (batch_idx, off_kv_h, s_idx, d_idx))     # [TILE_N, TILE_D]
gathered_v = ct.gather(V, (batch_idx, off_kv_h, s_idx, d_idx))
gathered_kpe = ct.gather(KPE, (batch_idx, 0, s_idx, dpe_idx))
```

**(2) 联合位置编码——同一 qk 累加两次 mma**

[sparse_mla.py:L174-L179](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L174-L179)：内容项 `q·gathered_k^T` 和位置项 `qpe·gathered_kpe^T` 累加到**同一个** `qk` 张量，这是 MLA 区别于 FMHA 的唯一算术差异（与 u6-l3 一致）。

**(3) 稀疏场景的关键：exp2 之后再次掩码**

[sparse_mla.py:L182-L195](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L182-L195)：先用 `indices_tile <= s_i` 做 causal 掩码（被掩位置 `qk = -1e6`），但**在 `exp2` 之后还要再做一次 `p = where(valid_mask, p, 0.0)`**。注释解释了原因：

```python
# Re-apply mask: zero out masked positions so they contribute nothing
# to l_i or acc. This is critical when an entire index block has no
# causal-valid entries (all indices > s_i), which cannot happen in
# dense attention but is common in sparse attention with shuffled indices.
p = ct.where(valid_mask_2d, p, 0.0)
```

**为何密集注意力不需要、稀疏必须？** 密集场景下每个 KV 块按顺序遍历，必然有有效元素，`max(qk)` 来自真实 logit；但稀疏场景的 `indices` 是**打乱的**，一个索引块可能**整块都是未来位置**（全部 `> s_i`），此时 `max(qk) = -1e6`，会让 `m_ij` 虚低、`alpha = exp2(m_i - m_ij)` 虚高爆炸，进而污染 `l_i` 与 `acc`。把 `p` 强制置 0 后，这些位置对 `l_i`/`acc` 的贡献精确为 0，online softmax 才稳定。这是稀疏注意力相对密集注意力最微妙的一处工程差异。

**(4) TILE_H：一块算多个头（GQA）**

[sparse_mla.py:L137-L143](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L137-L143)：`TILE_H` 个共享同一 KV 头的 query 头放进一个块一起算（GQA 复用 K/V），`off_kv_h = h_start // QUERY_GROUP_SIZE`。`TILE_H` 的取值受 GQA 组大小约束（见下）。

**(5) 三选一的配置选择 + 严格校验**

[sparse_mla.py:L219-L309](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L219-L309)：`_launch_sparse_mla_fwd` 有三条互斥路径——(1) 显式 `kernel_configs` 直接启动；(2) `TILEGYM_DISABLE_AUTOTUNE=1` 取搜索空间首个合法配置；(3) autotune 用 `exhaustive_search` 搜最优。配置合法性由 [_validate_sparse_mla_config](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L33-L72) 严格校验：`TILE_H`/`TILE_N` 必须是 2 的幂、`TILE_H ≤ query_group_size` 且 `query_group_size % TILE_H == 0`（瓦片边界须与 GQA 组边界对齐）、`topk % TILE_N == 0`。

**(6) 仅前向的 autograd 壳**

[sparse_mla.py:L312-L359](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L312-L359)：`_SparseAttentionFunction` 同样 `backward → NotImplementedError`，仅推理。缩放因子 `scaling = 1/sqrt(D + D_PE)`（[L364-L365](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L364-L365)），分母是内容维与位置维之和，与 u6-l3 的 MLA 一致。

#### 4.4.4 代码实践

**实践目标**：理解 `indices` 张量如何决定注意力范围，以及校验规则。

1. 阅读 [tests/ops/experimental/test_sparse_mla.py:L17-L43](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/experimental/test_sparse_mla.py#L17-L43) 的 `_generate_indices`：它先用 `randperm` 取 `min(topk, s+1)` 个因果过去位置，不足的用未来位置填充（会被 causal 掩码丢弃），并打乱顺序——这正是 4.4.3(3) 「整块都是未来位置」场景的来源。
2. 阅读 [test_sparse_mla.py:L48-L105](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/experimental/test_sparse_mla.py#L48-L105) 的 `reference`：它用 `torch.gather` 按索引取 K/V/KPE，是验证基准。
3. **待本地验证**：跑 `pytest "tests/ops/experimental/test_sparse_mla.py::Test_SparseMLA::test_op_invalid_kernel_configs" -v`，观察非法 `kernel_configs`（如 `TILE_H` 非幂2、`TILE_H > query_group_size`）如何被立即拒绝。

**预期结果**：`indices` 里下标值 `> s_i` 的位置被 causal 丢弃；`topk == S_kv` 且索引覆盖全部位置时（`test_op_topk_equals_skv`），稀疏结果应逼近密集 MLA。

#### 4.4.5 小练习与答案

**练习 1**：为何稀疏 MLA 必须在 `exp2` 后再次把掩码位置置 0，而 u6-l1 的密集 FMHA 不用？

**参考答案**：密集 FMHA 按 KV 顺序遍历，每个块必有有效元素，`max(qk)` 始终来自真实 logit，掩码位置经 `-1e6` 后 `exp2 ≈ 0`，对 `l_i`/`acc` 无害。稀疏 MLA 的 `indices` 是打乱的，可能出现**整块全为未来位置**，使 `max(qk) = -1e6` 虚低、`alpha` 虚高爆炸；再次置 0 让这些位置对 `l_i`/`acc` 贡献精确为 0，保证数值稳定。

**练习 2**：`_validate_sparse_mla_config` 为何要求 `query_group_size % TILE_H == 0`？

**参考答案**：`TILE_H` 个连续 query 头共享同一个 KV 头。若瓦片边界（每 `TILE_H` 个头）与 GQA 组边界（每 `query_group_size` 个头共享一个 KV 头）不对齐，一个瓦片就会跨越两个 KV 头，`off_kv_h = h_start // QUERY_GROUP_SIZE` 这个统一映射就会出错，无法用单一 KV 头服务整块。要求取模为 0 保证瓦片边界严整地落在组边界上。

---

## 5. 综合实践

**任务**：选 `sparse_mla` 与 `attention_sink` 两个变体，对比它们「限制注意力范围」的机制差异，并用一张表总结。

### 步骤

1. **读 `attention_sink` 的窗口机制**：定位 [attention_sink.py:L102-L112](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L102-L112)（`BANDWIDTH` 决定块级下界 `lo`）与 [L140-L142](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/attention_sink.py#L140-L142)（`too_old` 逐元素掩码）。它限制的是「key 的**位置**范围」：`query_pos - BANDWIDTH < key_pos ≤ query_pos`。

2. **读 `sparse_mla` 的索引机制**：定位 [sparse_mla.py:L150-L169](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L150-L169)（按 `indices` gather）与 [L182-L184](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/src/tilegym/ops/cutile/experimental/sparse_mla.py#L182-L184)（causal 掩码 `indices_tile <= s_i`）。它限制的是「key 的**身份（下标集合）**」：只看 `indices` 指定的 `topk` 个位置。

3. **填表对比**（示例答案）：

   | 维度 | attention_sink 的 `sliding_window` | sparse_mla 的 `indices` |
   |---|---|---|
   | 限制对象 | key 的**连续位置区间** | key 的**任意下标集合**（top-k） |
   | 范围表达 | 一个标量窗口宽度 `BANDWIDTH` | 一个 `[B,S,H_kv,topk]` 张量 |
   | 选取方式 | 按位置区间**连续**加载（`ct.load`） | 按下标**散列**选取（`ct.gather`） |
   | 因果性 | 由 `query_pos` 与 `offs_n` 比较 | 由 `indices <= s_i` 比较 |
   | 复杂度 | \(O(S^2)\)（逐元素掩，不跳块） | \(O(S\cdot \text{topk})\) |
   | 是否需 exp2 后再掩码 | 否（块内必有有效元素） | **是**（整块可能全无效） |

4. **思考题**：如果想让 `sparse_mla` 的 `indices` 退化为「最近 W 个 key」（即等价于一个滑窗），`indices` 该怎么填？答：对每个 query 位置 `s`，填 `[s-W+1, ..., s]`（不足补未来占位），此时稀疏 MLA 在数值上应与带滑窗的密集注意力一致——这正是 `test_op_topk_equals_skv` 类比验证的思路。

### 预期结果

你能用一句话概括二者本质区别：**`sliding_window` 用「位置区间」描述注意力范围、靠掩码实现；`indices` 用「下标集合」描述、靠 gather 实现——前者是后者的一个连续特例。**

## 6. 本讲小结

- 四个变体共享 u6-l1 的 FMHA 骨架（分块 QKᵀ、在线 softmax、`exp2`、`mma`），只改「**能看到哪些 key**」（掩码/索引）和「**logit 如何变换**」（soft cap）两个旋钮。
- **attention sink** 把可学习虚拟 token 的 logit 折进 softmax 分母：\(m_i\) 从 `sink_scaled` 起步、循环外补 `z = l_i + 2^{sink - m_i}`；`start_q` 必须保持张量以避免 device 同步。
- **gemma_attention** 用 `cap·tanh(·/cap)` 对 logit 软限幅、配对称滑窗，并用 STAGE 位标志拆分 causal 的非对角/对角循环；soft cap 改变了 `exp2` 的缩放基准（用 `INV_LOG_2` 而非 `qk_scale`）。
- **SWA** 在**块级别**跳过窗口外 KV 块，把复杂度真正降到 \(O(S\cdot W)\)，用 `exp2`+FTZ 走 SFU 全速路径；全掩码行靠 `l_i ← max(l_i, 1e-6)` 防 NaN；解码回退 SDPA。
- **sparse_mla** 用 `indices` 张量做 top-k 散列选取（`ct.gather`），保留 MLA 的联合位置编码（同一 qk 两次 mma）；稀疏打乱索引下必须在 `exp2` 后**再次掩码**以防整块无效导致 `alpha` 爆炸。
- 四个内核**均仅前向**（gemma/sparse_mla 虽有 `autograd.Function` 但反向抛 `NotImplementedError`）；swa/sparse_mla 挂 `@experimental_kernel` 走一次性告警；是否走 autotune 各有不同（attention_sink 看 `is_autotune_enabled`、gemma 看 `use_autotune` 参、swa 用离线调优硬编码、sparse_mla 三选一并带严格配置校验）。

## 7. 下一步学习建议

- **回到集成**：本讲四个变体都是「算子层」实现，下一单元 [u8-l1](./u8-l1-transformer-monkey-patch.md) 会讲它们如何经 `monkey_patch.py` 接进 HuggingFace 模型（如 Gemma→`gemma_attention`、Mistral→SWA）。建议带着「这些算子被哪个模型的哪个模块调用」的问题去读 u8。
- **解码侧对照**：本讲只讲了 prefill 变体。`attention_sink_decode.py`、`gemma_attention_decode.py` 是它们的解码版，可对照 [u6-l2](./u6-l2-decode-splitkv.md) 的 Split-KV 思路阅读。
- **稀疏注意力的索引生成**：`sparse_mla` 的 `indices` 通常由一个「路由/检索」网络产生（选 top-k 相关 key），本仓库未实现该路由——可结合外部资料（如 NSA、MoBA 等稀疏注意力方案）理解 `indices` 从何而来。
- **贡献一个新变体**：若想加一种新的注意力变体（如 block-sparse），可参照本讲四个文件的套路——复用在线 softmax 骨架、只改掩码/索引/限幅逻辑——并参考 `skills/tilegym-adding-cutile-kernel/SKILL.md`（[u9-l2](./u9-l2-add-new-op-workflow.md)）走注册流程。
