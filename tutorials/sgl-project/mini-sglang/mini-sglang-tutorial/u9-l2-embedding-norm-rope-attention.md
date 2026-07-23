# Embedding / Norm / RoPE 与 AttentionLayer

## 1. 本讲目标

本讲承接 u8-l1（BaseOP 体系与 Llama 模型骨架）与 u9-l1（张量并行 Linear 与分布式通信），把一个 decoder layer 里**除 MLP 与 qkv 投影权重之外**的几个关键层一次性讲透。

读完本讲，你应当能够：

- 说清 `VocabParallelEmbedding`（输入侧）与 `ParallelLMHead`（输出侧）如何把词表沿 TP 切分，以及它们为何一个用 `all_reduce`、一个用 `all_gather`。
- 解释 `RMSNormFused` 如何把「残差相加 + RMSNorm」融合进一个 kernel，以及残差如何在层间传递。
- 写出 RoPE 的频率公式与 cos/sin 缓存的构造方式，理解为何 RoPE 只作用于 q/k、且要在 meta 建图时特殊处理。
- 跟踪 `AttentionLayer.forward` 的完整链路：split qkv → 可选 q/k norm → RoPE → 调用 `ctx.attn_backend.forward`，并能说明 q/k/v 的切分维度在 TP 下如何由 `num_qo_heads` / `num_kv_heads` 决定。

## 2. 前置知识

在进入源码前，先用三段话补齐背景。

**词表（vocabulary）与 embedding。** 语言模型把每个 token id（一个整数）映射成一个固定长度的向量，这个映射表就是 embedding 矩阵，形状为 `(vocab_size, hidden_size)`。`vocab_size` 是词表大小（动辄十几万），`hidden_size` 是每个 token 的向量维度。模型最后一层要把向量再「投影」回词表维度，得到每个 token 的得分（logits），这一步叫 LM Head，本质是 embedding 矩阵的转置乘法。

**RMSNorm 与残差。** Transformer 每个 sub-layer（注意力、MLP）的输出通常要和一个「残差」（residual）相加，再进入下一个 sub-layer，这是为了梯度流通畅。RMSNorm 是 LayerNorm 的简化版：它不减均值，只用均方根（Root Mean Square）做归一化。把「加残差」和「归一化」合到一个 CUDA kernel 里做，就叫 fused rmsnorm，能省一次显存读写。

**旋转位置编码 RoPE。** 自注意力本身是「无序」的——打乱 token 顺序结果不变。为了让模型知道 token 的先后位置，RoPE 在算注意力前，根据每个 token 的**绝对位置**对 q（query）和 k（key）做一个旋转，使得两个 token 的点积自然只依赖它们的**相对距离**。RoPE 不引入可学习参数，只需预算一张 cos/sin 表。

> 本讲假设你已读过 u8-l1（BaseOP 不继承 `nn.Module`、靠 `__dict__` 遍历管理权重）与 u9-l1（列并行/行并行、`all_reduce`/`all_gather`、`div_even` 与 GQA 头复制）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `python/minisgl/layers/` 下，外加一处模型组装入口：

| 文件 | 作用 | 本讲用到的核心符号 |
| --- | --- | --- |
| [embedding.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py) | 词表并行 embedding 与 LM Head | `VocabParallelEmbedding`、`ParallelLMHead` |
| [norm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py) | RMSNorm 与融合残差 RMSNorm | `RMSNorm`、`RMSNormFused` |
| [rotary.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py) | 旋转位置编码 | `RotaryEmbedding`、`get_rope`、`set_rope_device` |
| [attention.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py) | 注意力层：split qkv、norm、RoPE、调 backend | `AttentionLayer` |
| [activation.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/activation.py) | 门控激活（MLP 用，本讲略提） | `silu_and_mul`、`gelu_and_mul` |
| [models/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py) | 把上述层组装成 `RopeAttn` | `RopeAttn`（`qkv_proj`→`AttentionLayer`→`o_proj`） |

数据流串起来是：`input_ids → VocabParallelEmbedding → [每层：RMSNormFused → AttentionLayer(含 RoPE) → RMSNormFused → MLP] → RMSNormFused → ParallelLMHead → logits`。

## 4. 核心概念与源码讲解

### 4.1 VocabParallelEmbedding / ParallelLMHead

#### 4.1.1 概念说明

词表维度 `vocab_size` 通常很大（如 Qwen3 约 15 万）。在张量并行（TP）下，把词表**沿行（token 维）切分**到各卡，每卡只持有词表的一段行，能显著降低每卡显存。于是产生两个对称的层：

- `VocabParallelEmbedding`：**输入侧**。给一批 token id，每卡只对自己负责的那段词表做查表（命中的行取出、不命中的置零），再 `all_reduce` 求和，还原出完整 embedding。每卡输入相同、输出「部分相加」。
- `ParallelLMHead`：**输出侧**。给 hidden 向量，每卡算出自己那段词表的 logits，再 `all_gather` 拼接出完整 logits。它是 `VocabParallelEmbedding` 的子类，复用了「按 rank 切词表」的逻辑，只是前向方向反过来。

两者一对，一个用 `all_reduce`（求和还原），一个用 `all_gather`（拼接还原），区别源于 embedding 是「多卡部分和 → 合一」、LM Head 是「多卡部分列 → 拼全」。

#### 4.1.2 核心流程

`VocabParallelEmbedding` 构造期确定本 rank 的词表区间，前向做一次带区间的查表加一次归约：

```text
num_embeddings_tp = ceil(vocab_size / tp_size)        # 每卡分到的行数（向上取整）
start_idx  = num_embeddings_tp * rank                 # 本卡负责的起始 token id
vocab_range = (start_idx, 本卡实际行数)                # (偏移, 长度)，喂给 indexing kernel
weight 形状 = (num_embeddings_tp, hidden_size)         # 只存本卡的行

forward(x):                                           # x 是 token id 数组
    y = indexing(weight, x, vocab_range)              # 命中本卡区间→取行；否则→0
    return all_reduce(y)  if tp_size > 1 else y       # 各卡部分和相加还原
```

`ParallelLMHead` 前向多一步「prefill 只取每条请求最后一个 token」，再用 `F.linear` 做投影，最后 `all_gather` 拼接：

```text
forward(x):                                           # x 是 hidden 向量
    if 是 prefill: x = x[每条请求最后 token 的下标]     # 只需预测下一个 token，省算力
    logits = F.linear(x, weight, bias)                # 每卡算自己那段词表得分
    if tp_size == 1: return logits
    out = all_gather(logits)                          # 沿 dim0 拼接各卡
    reshape + permute 重组 token/rank 两维             # all_gather 把 token 维打散了
    裁剪到真实 vocab_size（去掉 div_ceil 多出来的 padding）
```

#### 4.1.3 源码精读

构造期切词表区间——用 `div_ceil` 把词表向上取整均分，第 `rank` 张卡负责 `[start_idx, finish_idx)`：

[python/minisgl/layers/embedding.py:24-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L24-L30) 用 `div_ceil(num_embeddings, tp_size)` 算每卡行数，`vocab_range = (start_idx, finish_idx - start_idx)` 是 (偏移, 长度) 二元组，`weight` 只开 `num_embeddings_tp` 行。

前向查表与归约：

[python/minisgl/layers/embedding.py:32-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L32-L42) 调用 `minisgl.kernel.indexing`，传入 `vocab_range`（仅多卡时），让 kernel 把落入本卡区间的 id 映射到本地行、其余置零；随后 `all_reduce` 求和。单卡时直接返回，省一次通信。`indexing` kernel 内部会按 `element_size` 选 `num_splits`（见 [kernel/index.py:41-48](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/kernel/index.py#L41-L48)），是个为词表并行 embedding 特化的 split-k gather。

`ParallelLMHead` 的 prefill 只取最后 token：

[python/minisgl/layers/embedding.py:87-95](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L87-L95) prefill 阶段一条请求可能算了几千个 token，但只有**最后一个** token 需要预测下一个词，所以用 `batch.attn_metadata.get_last_indices(bs)` 取出各请求末尾下标做切片（这个方法由注意力后端实现，见 u7）。decode 阶段每条请求本来就只产 1 个 token，无需切片。

`all_gather` 后的维度重组——这是本模块最绕的一处：

[python/minisgl/layers/embedding.py:101-110](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L101-L110) `all_gather`（见 [distributed/impl.py:33-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L33-L41)）底层是 `all_gather_into_tensor`，它**沿 dim0 把各卡张量首尾拼接**，于是 `(num_tokens, local_vocab)` 被拼成 `(num_tokens × tp_size, local_vocab)`——token 维与 rank 维被压在一起了。要还原必须先 `view(tp_size, num_tokens, local_vocab)`，再 `permute(1,0,2)` 把 token 提到第 0 维，最后 `reshape(num_tokens, tp_size×local_vocab)`。重组后词表恰按 rank 顺序排列（rank0 的小段在前），与切分时的 `start_idx = num_embeddings_tp * rank` 一致，所以拼接结果就是完整词表。末尾 `[:, :num_embeddings]` 裁掉 `div_ceil` 多估的 padding 行。

> 单 token（decode，`bs==1`）走快捷路径 [embedding.py:104-105](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L104-L105)：只有 1 个 token 时 token/rank 不会串味，直接 `view(1,-1)` 即可，省掉 permute。

**权重绑定（tied embeddings）。** 有些模型（如部分 Qwen）让输入 embedding 与输出 LM Head 共享同一张权重表。`ParallelLMHead` 用 `tied_embedding` 支持这点：

[python/minisgl/layers/embedding.py:59-85](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L59-L85) 若启用 tying，`state_dict` 返回空、`load_state_dict` 把可能存在的 `lm_head.weight/bias` 从字典里 `pop` 掉，避免重复加载；前向 [embedding.py:97-98](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L97-L98) 用 `self.tied_embedding or self` 决定用谁的 weight。

#### 4.1.4 代码实践

**实践目标**：验证 `all_gather` 把 token 维与 rank 维压在一起这一现象，理解 permute 的必要性。

**操作步骤**（源码阅读型，无需 GPU）：

1. 读 [embedding.py:101-110](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L101-L110) 与 [distributed/impl.py:33-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/distributed/impl.py#L33-L41)。
2. 在纸上模拟：`tp_size=2`、`num_tokens=3`、`local_vocab=4`。rank0 的 `logits` 是 `(3,4)`，rank1 也是 `(3,4)`。
3. 写出 `all_gather` 后的 `(6, 4)` 张量，标出哪些行属于 rank0、哪些属于 rank1。
4. 套用 `view(2,3,4) → permute(1,0,2) → reshape(3,8)`，验证结果第 t 行恰为「token t 的完整 8 维词表」。

**需要观察的现象**：如果不做 permute、直接 `view(3, 8)`，第 0 行会混入 rank1 的 token0 数据——这正是 permute 存在的原因。

**预期结果**：`all_gather` 输出形如 `[r0t0, r0t1, r0t2, r1t0, r1t1, r1t2]`（按 rank 分块），permute 后变成按 token 分组 `[t0:(r0,r1), t1:(r0,r1), t2:(r0,r1)]`。（本结论由源码静态推得，完整端到端数值待本地在多卡环境验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 embedding 用 `all_reduce` 而 LM Head 用 `all_gather`？

**答案**：embedding 是「每卡对自己那段词表查表、不命中的置零」，各卡结果相加（`all_reduce SUM`）才能还原完整向量；LM Head 是「每卡算自己那段词表的 logits」，各卡结果是**不同词表列**，需要拼接（`all_gather`）而非求和。

**练习 2**：`bs==1` 时为何可以跳过 permute？

**答案**：`all_gather` 把 `(1, local_vocab)` 拼成 `(tp_size, local_vocab)`，只有一个 token，rank 维与 token 维不会交错污染，`view(1, -1)` 直接得到按 rank 排列的完整词表。

---

### 4.2 RMSNormFused 残差融合

#### 4.2.1 概念说明

标准 RMSNorm 对输入 `x` 做：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\frac{1}{d}\sum_{i=1}^{d} x_i^2 + \varepsilon}} \odot \gamma
\]

其中 \(\gamma\) 是可学习缩放（即 `weight`），\(\varepsilon\) 防止除零。Transformer 里每个 sub-layer 后都要「先加残差再归一化」：

\[
\text{residual}_{\text{new}} = x + \text{residual}_{\text{old}}, \quad x_{\text{new}} = \text{RMSNorm}(\text{residual}_{\text{new}})
\]

如果分两步做，要读写两次显存。flashinfer 提供的 `fused_add_rmsnorm` 把这两步合到一个 kernel，原地完成「加残差 + 归一化」，省一半显存带宽——在逐层、逐 token 的 decode 里这是可观的节省。本模块 `RMSNormFused` 就是这层封装。

#### 4.2.2 核心流程

`RMSNormFused.forward` 按是否传入残差分两条路：

```text
forward(x, residual=None):
    if residual is None:                    # 首次：还没有残差可加
        return rmsnorm(x), x                #   仅归一化；把原 x 当作残差往后传
    else:                                   # 后续：有残差
        fused_add_rmsnorm(x, residual, ...) #   原地：residual = x + residual；x = norm(residual)
        return x, residual                  #   返回归一化结果与新残差
```

关键设计：残差不在层内累加完毕，而是**贯穿整个 decoder block 甚至跨层**传递。每经过一个 `RMSNormFused`，就把它对应的 sub-layer（attention 或 mlp）的输出「融」进残差流。

#### 4.2.3 源码精读

两个类都把 flashinfer 的函数在构造期绑定成实例属性，避免每次前向重复 import：

[python/minisgl/layers/norm.py:23-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L23-L30) `RMSNormFused` 在 `__init__` 里 `from flashinfer import fused_add_rmsnorm, rmsnorm`，存为 `self.fused_add_rmsnorm` 与 `self.rmsnorm`。`weight` 用 `torch.empty(size)` 开占位，权重由 Engine 的 `load_state_dict` 后填（见 u5-l1 的 meta 建图）。

残差融合的两分支：

[python/minisgl/layers/norm.py:32-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L32-L38) `residual is None` 时返回 `(rmsnorm(x), x)`——注意第二个返回值是**未归一化的原 x**，它将成为后续的残差基线；否则调用 `fused_add_rmsnorm(x, residual, weight, eps)` 原地改写 `x` 与 `residual`。

> 同文件还有一个不带残差的 `RMSNorm`（[norm.py:8-20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L8-L20)），它额外提供 `forward_inplace`——这个方法正是给下文 `AttentionLayer` 的 q_norm/k_norm 用的（在 RoPE 前对每个 head 的 q/k 做归一化，那里不需要残差）。

**残差如何跨层流动。** 看 `LlamaDecoderLayer.forward`：

[python/minisgl/models/llama.py:34-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L34-L43) 一层的流程是：`input_layernorm(x, residual)` → `self_attn` → `post_attention_layernorm(x, residual)` → `mlp` → 返回 `(x, residual)`。`LlamaModel.forward` 把 `residual` 初始化为 `None`（[llama.py:62](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L62)），逐层传递，最后一层结束后由模型尾部的 `self.norm.forward(x, residual)`（[llama.py:65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L65)）把最后一层 mlp 的输出融进残差并归一化。

这样，注意力残差在 `post_attention_layernorm` 处融合、mlp 残差在**下一层的 `input_layernorm`**（或尾部 norm）处融合——每个 `RMSNormFused` 恰好承担一次「加 + 归一化」。

#### 4.2.4 代码实践

**实践目标**：用一段最小 NumPy 代码验证 `fused_add_rmsnorm` 的语义（加残差 + 归一化），从而看懂源码里的两返回值。

**操作步骤**：

1. 写一个纯 Python/NumPy 的 `rmsnorm(x, gamma, eps)`（按 4.2.1 公式）。
2. 手算：给定 `x=[1,2,3,4]`、`residual=[0,0,0,0]`、`gamma=[1,1,1,1]`、`eps=1e-6`，分别算出 `residual_new = x + residual` 与 `x_new = rmsnorm(residual_new)`。
3. 对照源码 [norm.py:37](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L37) 的 `fused_add_rmsnorm(x, residual, ...)` 原地语义，确认 `residual` 被改写成 `x+residual`、`x` 被改写成归一化结果。

**需要观察的现象**：调用后 `x` 不再是原值，而是归一化后的值；`residual` 变成了「相加和」。

**预期结果**：你的手算 `x_new` 应等于对 `x+residual` 做 RMSNorm 的结果。若在装有 flashinfer 的 GPU 环境运行，可直接对照 `from flashinfer import fused_add_rmsnorm` 的输出；无 GPU 环境则标注「待本地验证」数值相等。

#### 4.2.5 小练习与答案

**练习 1**：`RMSNormFused.forward` 在 `residual is None` 时第二个返回值为什么是 `x` 而不是 `rmsnorm(x)`？

**答案**：因为残差流里存的是**未归一化**的原始 hidden（残差主干始终是未归一化的累加和）。归一化只用于喂给下一个 sub-layer 的输入，不应进入残差。所以把原 `x` 作为残差基线往后传。

**练习 2**：为什么 `RMSNormFused` 把 `fused_add_rmsnorm` 在 `__init__` 里 import 并存为实例属性，而不是在 `forward` 里 import？

**答案**：避免每次前向都执行 import 查找（有开销）；且实例属性是普通属性（非下划线开头会被 `state_dict` 遍历到，但它不是 `Tensor` 也不是 `BaseOP`，会被跳过，不影响权重序列化）。

---

### 4.3 RoPE 旋转位置编码

#### 4.3.1 概念说明

RoPE（Rotary Position Embedding）通过「旋转」q 和 k 来注入位置信息。对位置 \(t\) 的向量，按相邻两维一组配对，施加角度 \(\theta_i \cdot t\) 的旋转，其中频率 \(\theta_i\) 为：

\[
\theta_i = \text{base}^{-2i/d}, \quad i = 0, 1, \dots, d/2-1
\]

\(d\) 是 `head_dim`（`rotary_dim`），`base` 通常是 10000（Llama）或 1000000（Qwen3）。两个位置 \(t_m, t_n\) 的 q、k 点积最终只依赖 \(t_m - t_n\)，即相对位置，这正是 RoPE 的妙处。

RoPE **只作用于 q 和 k**（不作用 v），且在注意力分数计算**之前**施加。它没有可学习参数，只需预先算好一张 `(max_position, head_dim)` 的 cos/sin 表。本模块 `RotaryEmbedding` 负责建表，实际旋转调用 flashinfer 的 `apply_rope_with_cos_sin_cache_inplace`（原地）。

#### 4.3.2 核心流程

```text
__init__:
    inv_freq[i] = 1 / base^(2i/rotary_dim)                 # 频率向量
    t[pos]      = pos                                       # 位置向量 0..max_position-1
    freqs       = outer(t, inv_freq)                        # (max_position, rotary_dim/2)
    cos, sin    = cos(freqs), sin(freqs)
    _cos_sin_cache = concat([cos, sin], dim=-1)             # (max_position, rotary_dim)

forward(positions, q, k):
    apply_rope_with_cos_sin_cache_inplace(positions, q, k, head_size, _cos_sin_cache)
    return q, k                                              # 原地修改
```

注意 `concat` 把 cos 和 sin **沿最后一维拼在一起**而非交错，这是 flashinfer kernel 约定的布局。`assert rotary_dim == head_size` 表示当前只支持「整头旋转」。

#### 4.3.3 源码精读

建表——`inv_freq` 与 cos/sin 外积：

[python/minisgl/layers/rotary.py:13-32](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L13-L32) 第 23 行 `assert rotary_dim == head_size` 锁定整头旋转；第 24 行算 `inv_freq`；第 28 行用 `einsum("i,j -> ij", t, inv_freq)` 算外积得 `freqs`；第 32 行 `torch.cat((cos, sin), dim=-1)` 拼成 `_cos_sin_cache`。属性名带下划线前缀，故 BaseOP 的 `state_dict` 会跳过它（[base.py:23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L23)），它不是权重、每次构造重算。第 33 行 `assert head_size in [64,128,256,512]` 约束 flashinfer kernel 支持的头维。

前向——原地旋转：

[python/minisgl/layers/rotary.py:39-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L39-L52) 直接把 `positions`、`q`、`k` 与缓存交给 flashinfer 的 `apply_rope_with_cos_sin_cache_inplace`，函数原地改写 q/k，再返回它们。

**rope_scaling：扩展上下文长度。** 原始 RoPE 在超出 `max_position` 后效果变差。Llama3、YaRN 等 scaling 方案通过改写 `inv_freq` 来支持更长上下文：

[python/minisgl/layers/rotary.py:55-114](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L55-L114) `_get_rope` 按 `rope_scaling["rope_type"]` 分支：`default` 不变；`llama3`（第 69-91 行）按波长对高低频做不同缩放并平滑过渡；`yarn`（第 93-112 行）用 ramp 函数对频率做分段插值。二者都通过 `post_process` 回调改写 `inv_freq`，复用同一段建表逻辑。

**meta 设备的特殊处理。** Engine 用 `torch.device("meta")` 零显存搭模型骨架（见 u5-l1），但 RoPE 的 cos/sin 缓存必须在**真实设备**上构造：

[python/minisgl/layers/rotary.py:125-143](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L125-L143) `get_rope` 被 `@functools.cache` 装饰（按参数缓存、跨层共享同一 RoPE 实例）。第 135 行检测到当前是 meta 设备时，要求先调 `set_rope_device`（[rotary.py:120-122](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L120-L122)）登记真实设备，再用 `with torch.device(_ROPE_DEVICE)` 在真实设备上建表。这是 RoPE 与 meta 建图流程协作的关键。

#### 4.3.4 代码实践

**实践目标**：手算 `inv_freq` 与 cos/sin 表的一小段，确认与源码一致。

**操作步骤**：

1. 取 `head_dim=4`（为简化，虽不在 `[64,128,256,512]`，仅用于手算）、`base=10000`、`max_position=2`。
2. 按 [rotary.py:24](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/rotary.py#L24) 算 `inv_freq = 1 / base^(arange(0,4,2)/4) = 1 / base^[0, 0.5]`，即 `[1, 1/100]`。
3. 算 `freqs = outer([0,1], inv_freq)`，再算 `cos`、`sin`，最后 `cat` 得到 `(2, 4)` 的缓存。

**需要观察的现象**：位置 0 对应的 cos 全为 1、sin 全为 0（旋转 0 度，向量不变）；位置 1 的值随频率变化。

**预期结果**：位置 0 行 = `[1,1,1,1, 0,0,0,0]`（前半 cos、后半 sin）。本结论由公式静态推得，完整 kernel 行为待本地 GPU 验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_cos_sin_cache` 用下划线前缀？

**答案**：BaseOP 的 `state_dict`/`load_state_dict` 会跳过以下划线开头的属性（[base.py:23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L23)）。cos/sin 表由 `head_dim`/`base`/`max_position` 确定性算出、不是学习到的权重，不应进入 checkpoint，也不应被 `load_state_dict` 校验。

**练习 2**：`get_rope` 为何要 `@functools.cache`，又为何要在 meta 设备时报错？

**答案**：cache 是因为同一模型所有层用同一套 RoPE 参数，缓存后只建一张表、跨层共享；meta 设备报错是因为 cos/sin 表是真实张量、不能在 meta 设备上分配，必须先 `set_rope_device` 指明真实 GPU 再建。

---

### 4.4 AttentionLayer

#### 4.4.1 概念说明

`AttentionLayer` 是「注意力计算」的编排者：它本身**不持有 q/k/v 投影权重**（那在 `RopeAttn.qkv_proj`，见 [models/utils.py:89-95](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L89-L95)），只接收已经投影好的 `qkv` 拼接张量，负责：

1. 把 `qkv` 沿最后一维 split 成 q、k、v 三段；
2. （可选）对 q、k 做 per-head RMSNorm（Qwen3 的 QK-norm）；
3. 对 q、k 施加 RoPE；
4. 调用注意力后端 `ctx.attn_backend.forward` 算出注意力输出 o；
5. 把 o reshape 回扁平向量，交给后续的 `o_proj`。

它是「模型层」与「注意力后端」（u7）之间的桥：模型层提供 q/k/v，后端负责 paged attention 的具体计算。

#### 4.4.2 核心流程

```text
__init__:
    local_num_qo = div_even(num_qo_heads, tp_size)                      # query 头按卡均分
    local_num_kv = div_even(num_kv_heads, tp_size, allow_replicate=True)# KV 头允许复制(GQA)
    qo_attn_dim  = local_num_qo * head_dim
    kv_attn_dim  = local_num_kv * head_dim
    rotary       = get_rope(...)                                        # 取/建 RoPE
    q_norm/k_norm = RMSNorm(head_dim) 或 None                            # Qwen3 才有

forward(qkv):                                                          # qkv 来自 LinearQKVMerged
    q, k, v = qkv.split([qo_attn_dim, kv_attn_dim, kv_attn_dim], dim=-1)
    if q_norm: q_norm.forward_inplace(q.view(-1, local_num_qo, head_dim))
    if k_norm: k_norm.forward_inplace(k.view(-1, local_num_kv, head_dim))
    q, k = rotary.forward(positions, q, k)                              # 只旋 q/k
    q = q.view(-1, local_num_qo, head_dim)
    o = ctx.attn_backend.forward(q, k, v, layer_id, batch)              # 算 paged attention
    return o.view(-1, qo_attn_dim)
```

#### 4.4.3 源码精读

**TP 下头数与切分维度。** 这是本模块（也是本讲核心实践）的关键：

[python/minisgl/layers/attention.py:29-36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L29-L36) 第 29 行 `assert num_qo_heads % num_kv_heads == 0` 强制 GQA 结构（query 头数是 KV 头数的整数倍）。第 33 行 query 头用 `div_even(num_qo_heads, tp_size)` 均分；第 34 行 KV 头用 `div_even(num_kv_heads, tp_size, allow_replicate=True)`——当 TP 卡数多于 KV 头数时（如 8 卡跑只有 8 个 KV 头、或更少），`allow_replicate` 让 KV 头被**复制**而非报错（见 u9-l1 的 `div_even` 定义 [utils/misc.py:20-26](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/utils/misc.py#L20-L26)）。第 35-36 行把头数换算成向量宽度 `qo_attn_dim` / `kv_attn_dim`。

> 这套切分与 `LinearQKVMerged` 的权重切分（[linear.py:71-88](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L71-L88)）必须一致：投影层输出的列数 `(local_qo + 2*local_kv)*head_dim` 恰好等于这里 split 三段的总宽。这是 u8-l2 强调的「形状契约」。

**split qkv。**

[python/minisgl/layers/attention.py:47-49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L49) `qkv.split([qo_attn_dim, kv_attn_dim, kv_attn_dim], dim=-1)` 按列切成 q、k、v 三段。注意 k 和 v 的宽度相同（都是 `kv_attn_dim`），q 的宽度通常更大（因为 query 头更多）。

**可选的 q/k norm（Qwen3）。**

[python/minisgl/layers/attention.py:50-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L50-L53) 仅当 `self.q_norm is not None` 时，把 q reshape 成 `(-1, num_qo_heads, head_dim)` 后调 `forward_inplace` 做 per-head RMSNorm（k 同理用 `num_kv_heads`）。这两个 norm 对象由 `RopeAttn` 在 `has_qk_norm=True` 时传入（见 4.4.4）。norm 在 RoPE **之前**做。

**RoPE 与后端调用。**

[python/minisgl/layers/attention.py:54-57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L54-L57) 第 54 行对 q、k 施加 RoPE（传入 `ctx.batch.positions`，即各 token 的绝对位置）；第 55 行把 q 再 view 成 `(num_tokens, num_qo_heads, head_dim)`（后端要求的三维形状）；第 56 行调 `ctx.attn_backend.forward(q, k, v, layer_id, batch)`——这里 `ctx.attn_backend` 是全局上下文里的注意力后端（u7，可能是 `HybridBackend`，按 prefill/decode 分发）；返回的 o reshape 回 `(num_tokens, qo_attn_dim)` 交给 `o_proj`。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：说清 q/k/v 的切分维度如何由 `num_qo_heads` / `num_kv_heads` 在 TP 下决定，并指出 q_norm/k_norm 何时启用。

**操作步骤**：

1. 读 [attention.py:29-36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L29-L36) 与 [attention.py:47-49](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L49)，确认 split 用的是「TP 后的 local 头数」算出的宽度。
2. 读 [models/utils.py:79-116](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L116)，看 `RopeAttn` 如何把 `q_norm`/`k_norm` 传给 `AttentionLayer`。
3. 对比 Llama 与 Qwen3 的构造：[llama.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L20) 用 `LlamaAttn(config, layer_id)`（默认 `has_qk_norm=False`），而 [qwen3.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py#L20) 用 `Qwen3Attn(config, layer_id, has_qk_norm=True)`。

**需要观察的现象 / 结论**：

- q 段宽度 = `div_even(num_qo_heads, tp_size) × head_dim`；k、v 段宽度都 = `div_even(num_kv_heads, tp_size, allow_replicate=True) × head_dim`。query 头均分，KV 头在不足时复制。
- `q_norm`/`k_norm` **仅当 `has_qk_norm=True` 时启用**，目前只有 Qwen3（[qwen3.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py#L20)）开启，Llama 不开启（`self.q_norm = None`，见 [utils.py:96-102](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L96-L102)）。启用时它们是 per-head 的 `RMSNorm`（非 Fused），在 RoPE 之前对 q、k 各做一次归一化。

**预期结果**：你能不查代码就回答「tp=2、num_qo_heads=32、num_kv_heads=8、head_dim=128 时，q 段宽 = 16×128=2048，k/v 段宽 = 4×128=512」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `assert num_qo_heads % num_kv_heads == 0`？GQA 在这里意味着什么？

**答案**：GQA（Grouped-Query Attention）里多个 query 头共享一组 KV 头，所以 query 头数必须是 KV 头数的整数倍（每组若干 query 头配 1 个 KV 头）。这个整除关系保证了注意力后端能正确地把 KV 头广播给同组的 query 头。

**练习 2**：q_norm/k_norm 为何用 `RMSNorm`（非 Fused）且 `forward_inplace`？

**答案**：这里是对 q/k 的每个 head 单独归一化，**没有残差可加**，所以用不带残差的 `RMSNorm`；`forward_inplace` 直接原地改写 q/k，省一次显存分配（见 [norm.py:19-20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L19-L20)）。

**练习 3**：`AttentionLayer` 是 `StateLessOP`（[attention.py:10,18](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L10)），这意味着什么？

**答案**：`StateLessOP` 的 `state_dict` 返回空、`load_state_dict` 不消费权重（[base.py:56-71](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L56-L71)）。`AttentionLayer` 自身没有权重（qkv_proj/o_proj 在外层 `RopeAttn`，q_norm/k_norm 是独立子对象由外层装配），所以它不需要参与序列化。

## 5. 综合实践

把本讲四个模块串起来，跟踪 **一个 token 在一个 decoder layer 内的完整穿越路径**（源码阅读型，无需 GPU）：

1. **入流**：上一层传来的 hidden 向量 `x` 与残差 `residual` 进入 `LlamaDecoderLayer.forward`（[llama.py:34-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L34-L43)）。
2. **归一化**：`input_layernorm`（`RMSNormFused`）把 x 归一化、把原 x 存入残差流（[norm.py:32-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L32-L38)）。
3. **投影**：`RopeAttn.qkv_proj`（`LinearQKVMerged`）把 x 投影成拼接的 qkv（[utils.py:120](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L120)）。
4. **注意力编排**：`AttentionLayer.forward` split qkv →（Qwen3 才有 q/k norm）→ RoPE → `attn_backend.forward` → 返回 o（[attention.py:47-57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L57)）。请在这一步标注：q/k/v 各自的宽度由 4.4 决定、RoPE 只旋 q/k、q_norm/k_norm 是否启用取决于 `has_qk_norm`。
5. **输出投影**：`o_proj`（`LinearOProj`，行并行）对 o 做投影并 `all_reduce`（u9-l1）。
6. **残差融合**：`post_attention_layernorm`（`RMSNormFused`）把 attn 输出融进残差并归一化（为 MLP 准备输入）。
7. **MLP**：`mlp` 处理后，残差在下一层的 `input_layernorm` 或模型尾部 norm 处再融合。

**产出**：画一张时序图，标出每一步用的是哪个类、是否发生通信（`all_reduce`/`all_gather`）、残差流如何流动。这张图能帮你把 u8（模型骨架）、u9-l1（TP Linear）、本讲（embedding/norm/rope/attention）、u7（注意力后端）四块知识粘合在一起。

## 6. 本讲小结

- `VocabParallelEmbedding` 沿词表行切分，靠带 `vocab_range` 的 `indexing` kernel 查表 + `all_reduce` 还原；`ParallelLMHead` 是其子类，前向反过来用 `F.linear` + `all_gather` 拼全词表，`all_gather` 会把 token/rank 维压平故需 `permute` 重组。
- prefill 阶段 `ParallelLMHead` 只取每条请求最后一个 token（`get_last_indices`）来算 logits，decode 无需此步；它还支持 tied embedding（与输入 embedding 共享权重）。
- `RMSNormFused` 用 flashinfer 的 `fused_add_rmsnorm` 把「加残差 + 归一化」融成一个原地 kernel；残差流贯穿整个模型，attention 残差在 `post_attention_layernorm` 融合、mlp 残差在下一层 `input_layernorm` 或尾部 norm 融合。
- RoPE 用 `inv_freq = base^(-2i/d)` 构造频率，外积成 cos/sin 缓存（下划线前缀、不进 checkpoint），只旋 q/k；`get_rope` 跨层缓存实例，并在 meta 建图时要求 `set_rope_device`。
- `AttentionLayer` 是无状态编排者：split qkv（宽度由 TP 后的 local 头数决定，query 头均分、KV 头可复制）→ 可选 q/k norm（仅 Qwen3）→ RoPE → 调 `ctx.attn_backend.forward`。
- q_norm/k_norm 仅在 `has_qk_norm=True` 时启用，目前只 Qwen3 开（Llama 为 `None`）。

## 7. 下一步学习建议

- **向下游走**：本讲的 `ctx.attn_backend.forward` 是个黑盒，建议接着读 u7-l1（注意力后端抽象与 Hybrid）与 u7-l2（FlashInfer 后端实现），看 q/k/v 进入后端后如何被 store_kv 落池、如何做 paged attention。
- **向上游走**：若想理解 qkv 投影权重怎么从 HF checkpoint 切分成运行时形状，读 u8-l2（模型配置解析与权重加载分片），它解释了 `_MERGE_GROUPS` 如何把 q/k/v 合并成这里 `LinearQKVMerged` 用的 `qkv_proj`。
- **横向对照**：对比 Llama 与 Qwen3 的 decoder layer（[llama.py:18-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L18-L43) vs [qwen3.py:18-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/qwen3.py#L18-L41)），唯一差别就是 `has_qk_norm`，这正好是 u10-l3「接入新模型架构」的最小改动范例。
