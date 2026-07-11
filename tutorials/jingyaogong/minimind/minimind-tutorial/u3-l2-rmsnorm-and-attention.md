# RMSNorm 与 GQA 注意力（含 KV Cache）

## 1. 本讲目标

本讲深入 `MiniMindBlock` 中最核心的两个子模块：归一化层 **RMSNorm** 和自注意力层 **Attention**。读完本讲你应该能够：

- 说清 RMSNorm 与传统 LayerNorm 的差别，并能对照源码写出归一化公式。
- 看懂 `Attention.forward` 的完整数据流：`q/k/v` 投影 → `q_norm/k_norm` → RoPE → KV Cache 拼接 → 注意力打分 → 输出投影。
- 理解 **GQA**（Grouped-Query Attention）中 Q 头多于 KV 头的设计，以及 `repeat_kv` 如何把 KV 头「广播」对齐到 Q 头。
- 区分「Flash Attention 分支」与「手动 causal mask 分支」各自的触发条件。
- 动手构造一个张量跑通 `Attention`，验证 KV Cache 累积后的长度变化。

本讲承接 [u3-l1](u3-l1-config-and-model-skeleton.md)：在那里我们只看了模型的「骨架」（Config、Model、ForCausalLM 的组装），这一讲我们钻进 `MiniMindBlock` 的内部，把第 0 层之后真正算数的地方讲透。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**归一化是干什么的？** 神经网络深层叠加后，隐藏状态的数值幅度会越走越大或越走越偏，导致训练不稳定（梯度爆炸/消失）。归一化层的作用是「在每一层把数值重新拉回到一个稳定的范围」，再让模型用一个可学习的缩放系数（weight）去重新调整幅度。MiniMind 用的是 **RMSNorm**，它是 LayerNorm 的简化版，计算更便宜。

**注意力是干什么的？** 自注意力（Self-Attention）让序列中每个位置都能「看见」其它所有位置，并按相关程度加权聚合信息。核心公式是「查询 Q 去查询键 K，得到相关度分数，再用分数对值 V 加权求和」。为了让模型能并行多个「视角」，我们用多个「头」（head），每个头关注不同的子空间。

**GQA 和 KV Cache 是干什么的？**
- **GQA**（Grouped-Query Attention）：标准多头注意力里，Q/K/V 的头数一样多。但推理时只有 Q 在变、K/V 重复度高，于是让多个 Q 头**共享**同一组 K/V 头，能省显存又几乎不掉精度。MiniMind 默认 8 个 Q 头、4 个 KV 头（每组 2 个 Q 头共享 1 个 KV 头）。
- **KV Cache**：自回归生成时，每生成一个新 token，前面 token 的 K/V 是不变的。把它们缓存起来，下一步只算新 token 的 K/V 再拼上去，避免重复计算整个序列。

还需要一点张量形状记号。本讲大量出现 4 维张量，约定形状为 `(batch, seq_len, num_heads, head_dim)`，有时转置成 `(batch, num_heads, seq_len, head_dim)` 用于矩阵乘法。

## 3. 本讲源码地图

本讲只涉及一个文件，但聚焦其中的三段：

| 代码位置 | 作用 |
| --- | --- |
| [model/model_minimind.py:50-60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L50-L60) | `RMSNorm`：归一化层定义与 `forward`。 |
| [model/model_minimind.py:86-89](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L86-L89) | `repeat_kv`：把 KV 头广播对齐到 Q 头的辅助函数。 |
| [model/model_minimind.py:91-134](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L91-L134) | `Attention`：自注意力层，含投影、q/k_norm、RoPE、KV Cache、Flash/手动两个分支。 |
| [model/model_minimind.py:178-194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L178-L194) | `MiniMindBlock`：把 RMSNorm + Attention + FeedForward 串成一个 Transformer 层，方便理解归一化与注意力在层中的位置。 |

配置层面会用到（详见 [u3-l1](u3-l1-config-and-model-skeleton.md)）：
- [model/model_minimind.py:22-24](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L22-L24)：`num_attention_heads=8`、`num_key_value_heads=4`、`head_dim` 三个决定头数与维度的字段。

---

## 4. 核心概念与源码讲解

### 4.1 RMSNorm：更省的归一化

#### 4.1.1 概念说明

LayerNorm 是早期 Transformer 的标配，它对一个样本的某个向量 \(x \in \mathbb{R}^d\) 同时做两件事：先减去均值 \( \mu \)、再除以标准差 \( \sigma \)，最后用可学习的缩放 \( \gamma \) 和偏移 \( \beta \) 还原幅度：

\[
\mu = \frac{1}{d}\sum_{i=1}^{d} x_i,\quad \sigma^2 = \frac{1}{d}\sum_{i=1}^{d}(x_i - \mu)^2
\]

\[
y_{LN} = \gamma \cdot \frac{x - \mu}{\sqrt{\sigma^2 + \epsilon}} + \beta
\]

**RMSNorm**（Root Mean Square Normalization）观察到：减均值这一步对最终效果贡献很小，但计算和开销都不小。于是它**只做方差归一化、不做去均值**，并且**不要偏置 \( \beta \)**，只用一个可学习的缩放 \( \gamma \)：

\[
\text{RMS}(x) = \sqrt{\frac{1}{d}\sum_{i=1}^{d} x_i^2 + \epsilon}
\]

\[
y = \gamma \cdot \frac{x}{\text{RMS}(x)}
\]

好处有三：

1. **少算一次均值**：省掉 \( \mu \) 的计算，约省 7%~64% 的归一化算力（视实现而定）。
2. **少一组参数**：没有 \( \beta \)，参数量减半。
3. **数值更稳**：经验上在大模型训练中更稳定，因此 Llama/Qwen 系（包括 MiniMind 对标的 Qwen3）都采用 RMSNorm。

#### 4.1.2 核心流程

RMSNorm 的前向只有三步：

1. 计算每个位置沿特征维度的均方根 RMS。
2. 用 \( x / \text{RMS} \) 把数值缩到单位量级附近。
3. 乘以可学习权重 \( \gamma \)（初始化为全 1）。

伪代码：

```text
def RMSNorm_forward(x):            # x: (..., d)
    ms = mean(x * x, dim=-1)       # 均方，(..., 1)
    rms = sqrt(ms + eps)           # (..., 1)
    x_norm = x / rms               # 归一化
    return weight * x_norm         # 可学习缩放
```

#### 4.1.3 源码精读

RMSNorm 的实现非常短：

[model/model_minimind.py:50-60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L50-L60) 定义了 `RMSNorm`。这段代码做的事情：

- `__init__`：`eps` 防止除零；`weight` 是可学习参数 \( \gamma \)，初始化为全 1（`torch.ones(dim)`）。
- `norm(x)`：核心计算。`x.pow(2).mean(-1, keepdim=True)` 算均方，`torch.rsqrt(... + eps)` 一次性算出 \( 1/\sqrt{\text{ms}+\epsilon} \)，再乘回 `x`，等价于 \( x / \text{RMS} \)。用 `rsqrt` 而非 `sqrt` 再除，是为了少一次逐元素除法。
- `forward(x)`：先在 `float()` 上做归一化（**数值稳定性**：半精度 fp16 下 `mean` 容易溢出/丢精度，强制升到 fp32 计算），再乘 `weight`，最后 `.type_as(x)` 转回输入精度。

> 注意 [model/model_minimind.py:60](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L60) 这一行：`(self.weight * self.norm(x.float())).type_as(x)`。这是 RMSNorm 在混合精度训练（fp16/bf16）下能稳定训练的关键细节——归一化在 fp32 做，输出再回落到原精度。下一讲 [u3-l3](u3-l3-rope-and-yarn.md) 不涉及归一化，但 [u3-l4](u3-l4-swiglu-and-moe.md) 的 Block 里 `input_layernorm`/`post_attention_layernorm` 都是这个 RMSNorm。

#### 4.1.4 代码实践

我们手写一段验证 RMSNorm 的行为，重点观察「归一化后量级接近 1」和「weight 的缩放作用」。这段是**示例代码**，不是项目原有代码：

```python
# 示例代码：验证 RMSNorm 的归一化效果
import torch
from model.model_minimind import RMSNorm

torch.manual_seed(0)
x = torch.randn(2, 5, 16) * 100          # 故意放大，模拟不稳定的隐藏状态
print("输入 RMS（每个位置）:", x.pow(2).mean(-1).sqrt()[0])  # 量级 ~100

norm = RMSNorm(dim=16, eps=1e-6)
# 临时把 weight 设为全 1，纯看归一化效果
with torch.no_grad():
    norm.weight.fill_(1.0)
y = norm(x)
print("输出 RMS（每个位置）:", y.pow(2).mean(-1).sqrt()[0])  # 量级应接近 1
print("weight.shape:", norm.weight.shape)                    # (16,)
```

操作步骤：

1. 在项目根目录新建一个临时脚本（不要放进 `model/`，写完即可删）。
2. 运行 `python <你的脚本>.py`。

需要观察的现象与预期结果：

- 输入的 RMS 量级在 ~100 左右（因为我们乘了 100）。
- 经过 RMSNorm 后，每个位置的 RMS 应接近 1（因为 weight 全 1）。
- `weight.shape` 为 `(16,)`，证实 RMSNorm **每个特征维度只有一个可学习缩放参数、没有偏置**。
- 如果把 `norm.weight` 改成 `torch.linspace(0.5, 1.5, 16)`，输出 RMS 会随 weight 的幅度变化，说明 weight 负责「还原幅度」。

> 待本地验证：具体数值随 `torch.manual_seed` 不同会浮动，但「输出 RMS ≈ 1」这一规律应稳定成立。

#### 4.1.5 小练习与答案

**练习 1**：把 RMSNorm 的 `eps` 从 `1e-6` 改成 `1e-1`，输出量级会变大还是变小？为什么？

> 参考答案：变大。`eps` 加在均方 `ms` 上再开方，`eps` 越大，分母 `RMS` 越大，于是 \( x / \text{RMS} \) 越小——等等，分母变大反而让结果变小。重新理一遍：`y = x / sqrt(ms + eps)`，`eps` 变大 → 分母变大 → `y` 变小。所以输出量级**变小**。`eps` 本意只是防除零，正常取极小值（如 `1e-6`），不应调大。

**练习 2**：为什么 RMSNorm 在 `forward` 里要先 `.float()` 再做归一化？

> 参考答案：在 fp16/bf16 混合精度下，`x.pow(2)` 容易把数值推到接近 fp16 上溢边界（~65504），且低精度求和会丢失精度。先升到 fp32 计算均方和 rsqrt 更稳定，最后 `.type_as(x)` 再降回输入精度，兼顾速度与数值稳定。

---

### 4.2 GQA 与 repeat_kv：让 Q 头共享 KV 头

#### 4.2.1 概念说明

标准多头注意力（MHA）里，Q、K、V 的头数相同，都是 `num_attention_heads`。每一对 Q 头和 KV 头单独做注意力，互不共享。

**MQA**（Multi-Query Attention）走极端：所有 Q 头共享**1 组** K/V 头。显存省到极致，但精度损失明显。

**GQA**（Grouped-Query Attention）是折中：Q 头数 > KV 头数，把 Q 头分成若干组，每组共享一个 KV 头。MiniMind 默认 `num_attention_heads=8`、`num_key_value_heads=4`，即 2 个 Q 头共享 1 个 KV 头（`n_rep = 8 // 4 = 2`）。

GQA 的收益主要体现在推理：KV Cache 占用的显存与 KV 头数成正比，把 KV 头砍半，长序列生成的显存压力直接减半，而质量几乎不掉。代价是计算时需要把 KV 头「复制」对齐到 Q 头的数量，这就是 `repeat_kv` 干的事。

#### 4.2.2 核心流程

`repeat_kv` 接收形状 `(batch, seq_len, num_kv_heads, head_dim)` 的张量，把它扩展成 `(batch, seq_len, num_kv_heads * n_rep, head_dim)`，让每个 KV 头紧接着复制 `n_rep` 份。

以 MiniMind 默认配置（`n_rep=2`）为例，假设 `num_kv_heads=4`，输入 4 个 KV 头 `[H0, H1, H2, H3]`，输出为 8 个头：

```text
[H0, H0, H1, H1, H2, H2, H3, H3]
```

这样就能和 8 个 Q 头 `[Q0..Q7]` 一一配对：`Q0,Q1 ↔ H0`，`Q2,Q3 ↔ H1`，依此类推。

实现上不真正复制数据（太费显存），而是用 `expand` 制造一个「视图」再加 `reshape`：

```text
x[:, :, :, None, :]                        # (b, s, kv, 1, d)  插一个长度1的轴
   .expand(b, s, kv, n_rep, d)             # (b, s, kv, n_rep, d)  广播
   .reshape(b, s, kv * n_rep, d)           # (b, s, kv*n_rep, d)   摊平
```

`expand` 返回的是共享存储的视图，不占额外显存；真正「物化」复制要等后续 `@` 矩阵乘或 `.contiguous()` 时才发生。

#### 4.2.3 源码精读

[model/model_minimind.py:86-89](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L86-L89) 是 `repeat_kv` 的全部实现：

- 解包出 `bs, slen, num_key_value_heads, head_dim` 四个维度。
- **短路优化**：`if n_rep == 1: return x`。当 Q 头数等于 KV 头数（即标准 MHA）时直接返回，不做任何复制。这是一个重要的快路径，说明同一份代码兼容 MHA/GQA/MQA：`num_key_value_heads == num_attention_heads` 时退化成 MHA，`==1` 时退化成 MQA。
- 否则用 `[:, :, :, None, :]` 在第 3 个位置插一个长度为 1 的轴，`expand` 广播到 `n_rep`，再 `reshape` 摊平。最终 KV 头数从 `num_key_value_heads` 变成 `num_key_value_heads * n_rep`，正好等于 Q 头数。

> `n_rep` 是在哪里算出来的？在 `Attention.__init__` 里：[model/model_minimind.py:97](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L97) `self.n_rep = self.n_local_heads // self.n_local_kv_heads`。默认 `8 // 4 = 2`。

#### 4.2.4 代码实践

用一段最小代码观察 `repeat_kv` 对形状的改变，这是**示例代码**：

```python
# 示例代码：观察 repeat_kv 的形状变化
import torch
from model.model_minimind import repeat_kv

# 模拟 4 个 KV 头，每个头维度 96，序列长度 5，batch 1
x = torch.randn(1, 5, 4, 96)
y = repeat_kv(x, n_rep=2)
print("输入 shape:", tuple(x.shape))   # (1, 5, 4, 96)
print("输出 shape:", tuple(y.shape))   # (1, 5, 8, 96)
# 验证复制关系：输出第 0、1 个头应与输入第 0 个头相同
print("H0 复制一致:", torch.equal(y[:, :, 0, :], y[:, :, 1, :]))  # True
print("H0==H2 ?:   ", torch.equal(y[:, :, 0, :], y[:, :, 2, :]))  # False（H2 是另一个头）
```

操作步骤：

1. 在项目根目录运行该脚本。
2. 改 `n_rep=1` 再跑一次。

需要观察的现象与预期结果：

- `n_rep=2` 时输出形状 `(1, 5, 8, 96)`，KV 头数翻倍。
- 输出的第 0、1 个头完全相同（都来自输入第 0 个头），证实「每组 Q 头共享同一 KV 头」。
- `n_rep=1` 时直接返回原张量，形状不变（走的是 `if n_rep == 1` 短路）。

> 待本地验证：`torch.equal` 比较结果应为 True/False 如注释所示。

#### 4.2.5 小练习与答案

**练习 1**：如果想让 MiniMind 退化成标准 MHA（Q/KV 头数相同），应如何设置配置？

> 参考答案：令 `num_key_value_heads` 等于 `num_attention_heads`（即都设为 8）。此时 `n_rep = 8 // 8 = 1`，`repeat_kv` 走 `n_rep == 1` 短路直接返回，不做任何复制，等价于标准 MHA。

**练习 2**：`repeat_kv` 用 `expand` 而不是 `repeat`/`cat`，主要好处是什么？

> 参考答案：`expand` 返回的是**共享底层存储**的视图，不真正复制数据，几乎不占额外显存；而 `repeat`/`cat` 会立即物化一份新数据。在长序列、多头的场景下，省下的 KV 复制显存很可观。代价是后续某些算子可能要求连续存储，会触发隐式拷贝，但在注意力计算里这一般可接受。

---

### 4.3 Attention.forward：投影、归一化、RoPE、KV Cache 与两个注意力分支

#### 4.3.1 概念说明

`Attention` 是 Transformer 的「信息聚合」部件。对输入序列的每个位置，它产出一个新的表示，编码了「这个位置应该关注序列里哪些其它位置」。完整流程把上两节（RMSNorm、GQA）和后续要讲的 RoPE（[u3-l3](u3-l3-rope-and-yarn.md)）串到了一起。

这一节有几个关键设计点要先点出来：

1. **q_norm / k_norm**：在 Q、K 投影之后、RoPE 之前，对每个头内部再做一次 RMSNorm。这是 Qwen3 系（以及 Llama 后期变体）引入的改进，能让注意力打分前的 Q/K 更稳定。V 不做归一化。
2. **RoPE**：旋转位置编码，把位置信息注入 Q/K（详见 [u3-l3](u3-l3-rope-and-yarn.md)，本讲只把它当成「对 Q/K 的一次变换」）。
3. **KV Cache 拼接**：推理时把历史的 K/V 和当前步的 K/V 在序列维 `cat` 起来。
4. **两个注意力分支**：满足条件时走 PyTorch 内置的 `scaled_dot_product_attention`（会自动调用 Flash Attention 内核，快且省显存）；否则走手动实现的 `Q @ K^T → softmax → @ V` 并自己加 causal mask。

#### 4.3.2 核心流程

`Attention.forward` 的数据流（形状以默认配置 `hidden_size=768, num_heads=8, kv_heads=4, head_dim=96` 为例，假设 `seq_len=S`）：

```text
x: (B, S, 768)
 │
 ├── q_proj/k_proj/v_proj  →  xq:(B,S,8,96)  xk,xv:(B,S,4,96)
 ├── q_norm/k_norm         →  对每个头做 RMSNorm（仍为上面形状）
 ├── apply_rotary_pos_emb  →  对 xq,xk 应用 RoPE
 ├── [可选] cat 历史 KV     →  xk,xv 的序列维变长 (B, S+L_cache, 4, 96)
 ├── repeat_kv(xk,2)/repeat_kv(xv,2) → (B, S+L_cache, 8, 96)
 ├── transpose(1,2)        →  (B, 8, S+L_cache, 96)
 │
 ├── 分支A (flash): scaled_dot_product_attention(xq,xk,xv, is_causal=True)
 ├── 分支B (手动): scores = xq @ xk^T / sqrt(96)
 │                  + causal_mask（上三角 -inf）
 │                  + attention_mask（可选 padding 屏蔽）
 │                  output = softmax(scores) @ xv
 │
 ├── transpose(1,2).reshape → (B, S, 768)
 └── o_proj → (B, S, 768)   返回 output 和 past_kv
```

注意力打分的数学核心：

\[
\text{scores} = \frac{Q K^\top}{\sqrt{d_k}},\qquad
\text{Attention}(Q,K,V) = \text{softmax}(\text{scores})\, V
\]

其中 \( d_k \) 是 `head_dim`（默认 96），除以 \( \sqrt{d_k} \) 是为了让点积的方差稳定在 O(1)，避免 softmax 饱和（梯度消失）。

**因果掩码（causal mask）**：语言模型生成第 `i` 个 token 时只能看到位置 `0..i`，不能「偷看」未来。用一个上三角为 `-inf` 的矩阵加到 scores 上，softmax 后未来位置的权重就变成 0。

#### 4.3.3 源码精读

**初始化（投影层与头数）**：[model/model_minimind.py:92-109](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L92-L109)

- `n_local_heads`（Q 头数）= `num_attention_heads`；`n_local_kv_heads` = `num_key_value_heads`；`n_rep` = 二者之商。
- `q_proj`：`hidden_size → num_heads * head_dim`（768 → 768）。
- `k_proj`/`v_proj`：`hidden_size → num_kv_heads * head_dim`（768 → 384），**比 q_proj 窄一半**，这就是 GQA 省参数与显存的直接体现。
- `o_proj`：`num_heads * head_dim → hidden_size`，把多头结果投影回 hidden 维。
- `q_norm`/`k_norm`：`RMSNorm(head_dim)`，注意作用在 `head_dim` 而不是 `hidden_size` 上。
- [model/model_minimind.py:109](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L109)：`self.flash` 判定——只有当 `torch.nn.functional` 有 `scaled_dot_product_attention` 且配置 `flash_attn=True`（默认 True，见 [model/model_minimind.py:21](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L21)）时才启用。

**前向（核心）**：[model/model_minimind.py:111-134](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L111-L134)

逐段说明：

- **投影 + reshape**（L113-116）：三个 `Linear` 把 `x` 投成 Q/K/V，再 `view` 成 `(B, S, num_heads, head_dim)`。
- **q_norm/k_norm**（L117）：对 Q 和 K 各自按头做 RMSNorm。注意只对 Q/K 做，V 不做。
- **RoPE**（L118-119）：从 `position_embeddings` 取出 `cos, sin`，调用 `apply_rotary_pos_emb` 给 Q/K 注入位置信息。
- **KV Cache 拼接**（L120-122）：若传入了 `past_key_value`（即历史的 `(k, v)`），在**序列维 `dim=1`** 上把历史 K/V 和当前 K/V `cat` 起来。这就是增量推理的关键：当前步只算新 token 的 K/V，再拼到缓存后面。
- **返回缓存**（L123）：`use_cache=True` 时返回当前完整 `(xk, xv)` 作为新的缓存，供下一步使用。
- **repeat_kv + transpose**（L124）：对 K/V 调用 `repeat_kv` 广播到 Q 头数，再 `transpose(1,2)` 把头维提到前面，变成 `(B, heads, seq, head_dim)` 以便做矩阵乘。Q 也一起 transpose。
- **Flash 分支判定**（L125）：这是全讲义最值得细看的一行。条件是：
  ```text
  self.flash
  且 seq_len > 1
  且 (not is_causal 或 past_key_value is None)   # is_causal 恒为 True，故等价于 past_key_value is None
  且 (attention_mask is None 或 attention_mask 全为 1)
  ```
  即：**只有在「整段前向（prefill）、无 padding、不带历史缓存」时才走 Flash 分支**。生成阶段的逐 token 解码（`seq_len=1`）、带 KV Cache 的增量前向、以及带真实 padding mask 的训练，都会落到手动分支。这是一个精度/兼容性优先的保守选择——Flash Attention 的 `is_causal` 与自定义 `attention_mask` 同时作用时语义易混，作者选择在复杂场景退回显式实现。
- **Flash 分支**（L126）：直接调用 `F.scaled_dot_product_attention(xq, xk, xv, is_causal=True)`，PyTorch 会自动选择 Flash Attention / memory-efficient 后端，省去显式 `Q@K^T` 的 O(S²) 显存中间矩阵。
- **手动分支**（L128-131）：
  - `scores = xq @ xk.transpose(-2,-1) / sqrt(head_dim)`：标准缩放点积。
  - **causal mask**（L129）：取 scores 最后 `seq_len` 列，加上一个上三角（`triu(1)`）为 `-inf` 的矩阵，屏蔽未来。注意只 mask 当前 `seq_len` 段，不影响已缓存的历史 K/V（历史部分本来就该被全部看到）。
  - **padding mask**（L130）：若有 `attention_mask`，把 padding 位置（mask=0）加上 `-1e9` 屏蔽。
  - `softmax` 后乘 `xv` 得到输出。softmax 在 fp32 做（`.float()`）再 `type_as(xq)`，同样是数值稳定考虑。
- **合并多头 + 输出投影**（L132-133）：`transpose` 回 `(B, S, heads*head_dim)`，`reshape` 摊平，过 `o_proj` 与 `resid_dropout`。
- **返回**（L134）：`output, past_kv`。

> 关于 `MiniMindBlock` 如何调用它：见 [model/model_minimind.py:186-194](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L186-L194)。`input_layernorm` 是一个 RMSNorm，先归一化再进 `self_attn`，结果加上残差 `residual`，体现「Pre-Norm」结构（归一化在残差路径内部，而非外部）。

#### 4.3.4 代码实践

构造一个 `(1, 8, 768)` 的输入，手动实例化 `Attention` 跑一遍，重点观察两件事：(a) 启用/不启用 flash 分支的输出形状一致；(b) 带入 KV Cache 后，返回的 `past_kv` 序列长度是否累加。这是**示例代码**：

```python
# 示例代码：手动调用 Attention，观察形状与 KV Cache 累积
import torch
from model.model_minimind import MiniMindConfig, Attention, precompute_freqs_cis

torch.manual_seed(0)
cfg = MiniMindConfig(hidden_size=768, num_hidden_layers=1)  # 默认 8 头 / 4 KV 头 / head_dim 96
attn = Attention(cfg).eval()

# 准备一段位置编码（取前 8 个位置），shape: (S, 2*head_dim)
freqs_cos, freqs_sin = precompute_freqs_cis(cfg.head_dim, end=8, rope_base=cfg.rope_theta)
pos_emb = (freqs_cos, freqs_sin)

x = torch.randn(1, 8, 768)   # batch=1, seq_len=8, hidden=768

# 1) 不带 KV Cache：观察 flash 开/关的输出形状
out_flash, _ = attn(x, pos_emb, past_key_value=None, use_cache=False)
print("无 cache 输出 shape:", tuple(out_flash.shape))   # (1, 8, 768)

attn.flash = False            # 强制走手动分支
out_manual, _ = attn(x, pos_emb, past_key_value=None, use_cache=False)
print("手动分支输出 shape:", tuple(out_manual.shape))   # (1, 8, 768)
print("两分支形状一致:", out_flash.shape == out_manual.shape)

# 2) 模拟两步增量推理，验证 KV Cache 累积
attn.flash = cfg.flash_attn   # 恢复默认
x_prompt = torch.randn(1, 5, 768)   # 第一步：5 个 token
pos5 = (freqs_cos[:5], freqs_sin[:5])
out1, kv1 = attn(x_prompt, pos5, past_key_value=None, use_cache=True)
print("第1步后 k_cache 长度:", kv1[0].shape[1])          # 5

x_new = torch.randn(1, 1, 768)      # 第二步：1 个新 token
pos1 = (freqs_cos[5:6], freqs_sin[5:6])
out2, kv2 = attn(x_new, pos1, past_key_value=kv1, use_cache=True)
print("第2步后 k_cache 长度:", kv2[0].shape[1])          # 6（5 + 1）
print("v_cache 长度:", kv2[1].shape[1])                  # 6
```

操作步骤：

1. 在项目根目录把上面脚本存成临时文件并运行（`model` 目录需可 import，故在根目录执行）。
2. 关注两组打印：无 cache 时两种分支的输出形状；带 cache 时两步累积后的 K/V 长度。

需要观察的现象与预期结果：

- 无 KV Cache 时，flash 分支与手动分支输出形状都是 `(1, 8, 768)`，**完全一致**（数值会因随机初始化和分支实现差异略有不同，但形状必然相同）。
- 第一步处理 5 个 token 后，`kv1[0]`（K 缓存）序列长度为 5。
- 第二步再喂 1 个新 token 并传入 `kv1`，新的 `kv2[0]` 长度变为 **6**（`cat` 了历史 5 + 新 1）。这验证了 [model/model_minimind.py:120-122](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L120-L122) 的缓存拼接逻辑。
- 注意：第二步 `seq_len=1`，根据 [model/model_minimind.py:125](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L125) 的条件，`seq_len > 1` 不满足，必然走**手动分支**。

> 待本地验证：第二步是否真的落到手动分支，可在 [model/model_minimind.py:127](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L127) 处临时加一行 `print("manual branch")` 验证（验证完请删掉，不要修改源码提交）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 K/V 的投影矩阵（`k_proj`/`v_proj`）输出维度只有 Q 投影（`q_proj`）的一半？这会带来什么好处？

> 参考答案：因为 MiniMind 用了 GQA，KV 头数（4）是 Q 头数（8）的一半，所以 `k_proj`/`v_proj` 的输出维度 = `num_kv_heads * head_dim = 4*96 = 384`，正好是 `q_proj`（`8*96=768`）的一半。好处是：(1) 投影层参数量与算力减少；(2) 更重要的是推理时 KV Cache 的显存占用与 KV 头数成正比，长序列生成时显存压力减半。

**练习 2**：[model/model_minimind.py:125](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L125) 的 flash 分支条件里有 `seq_len > 1`。请解释为什么单 token 解码（`seq_len == 1`）时作者选择不走 flash 分支？

> 参考答案：单 token 解码属于「逐 token 增量生成」阶段，此时序列长度为 1、且通常带 KV Cache。`scaled_dot_product_attention` 的 `is_causal=True` 在 `seq_len=1` 时没有意义（一个 token 谈不上屏蔽未来），而带缓存时 Q 的长度是 1、K/V 的长度是历史长度，这种非对称形状与 `is_causal`/自定义 mask 同时作用语义容易出错。作者选择在这种场景退回显式的手动实现，用 [model/model_minimind.py:129](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L129) 的方式只 mask 当前 `seq_len` 段，行为更可控。`seq_len=1` 时计算量本来就小，不走 flash 的性能损失也可忽略。

**练习 3**：q_norm/k_norm 为什么作用在 `head_dim` 上而不是 `hidden_size` 上？为什么没有 `v_norm`？

> 参考答案：投影后的 Q/K 形状是 `(B, S, num_heads, head_dim)`，每个头是独立的子空间，按 `head_dim` 做归一化相当于「在每个头的子空间内部」稳定 Q/K 的量级，从而稳定注意力打分 `Q@K^T` 的尺度。这是 Qwen3 系的 QK-Norm 设计。V 不参与打分（只参与最后的加权求和 `softmax(scores) @ V`），其量级对 softmax 分布没有直接影响，因此不需要归一化。

---

## 5. 综合实践

把本讲三个模块串起来：手写一个最小脚本，构造一段 `(1, 8, 768)` 的随机序列，**完整模拟一次「prefill + 两步解码」的注意力过程**，并打印每一步的关键形状与缓存长度。要求：

1. 实例化 `MiniMindConfig(hidden_size=768, num_hidden_layers=1)` 与对应的 `Attention`。
2. 用 `precompute_freqs_cis` 准备长度至少为 10 的位置编码（覆盖 prefill 5 + 解码 2 共 7 个位置即可）。
3. **第 1 步（prefill）**：喂入 `(1, 5, 768)`，`use_cache=True`，打印输出形状与 `past_kv` 中 K 的长度（应分别为 `(1,5,768)` 和 5）。说明此时走的是 flash 分支还是手动分支，并说明依据（`seq_len>1` 且无 `past_key_value`）。
4. **第 2、3 步（解码）**：每次喂 `(1, 1, 768)` 并传入上一步的 `past_kv`，每次打印新的 K 缓存长度（应依次为 6、7）。说明此时为什么必然走手动分支（`seq_len==1`）。
5. 把 `attn.flash` 改为 `False` 重跑一遍，确认整条流程的形状与缓存长度**完全不变**，从而验证两个分支在形状语义上等价（差异只在数值精度与显存）。

预期结果：缓存长度序列为 `5 → 6 → 7`；两种分支下输出形状恒为对应输入的 `(B, S, 768)`。若第 5 步发现形状不一致，说明你对 `repeat_kv` 或 `transpose` 的理解有误，回头检查 [model/model_minimind.py:124](https://github.com/jingyaogong/minimind/blob/512eed0b6556e741d80864f054d45d271459772a/model/model_minimind.py#L124)。

> 待本地验证：实际数值随权重随机初始化变化，但形状与缓存长度的规律应严格成立。本实践不依赖任何预训练权重，纯用随机初始化的 `Attention` 即可完成。

## 6. 本讲小结

- **RMSNorm** = 只做方差归一化（不去均值、无偏置）的 LayerNorm，计算更省、参数更少、训练更稳；MiniMind 在 `forward` 里强制升 fp32 计算再回落精度，保证混合精度下的数值稳定。
- **GQA** 让 Q 头（8）多于 KV 头（4），`n_rep = 8//4 = 2`，每个 KV 头被 2 个 Q 头共享，直接砍半了 `k_proj`/`v_proj` 的参数与推理时的 KV Cache 显存。
- **repeat_kv** 用 `expand` + `reshape` 把 KV 头「广播」对齐到 Q 头数，且 `n_rep==1` 时短路返回，使同一份代码兼容 MHA/GQA/MQA。
- **Attention.forward** 的数据流为：投影 → q_norm/k_norm → RoPE → （可选）KV Cache 拼接 → repeat_kv → 注意力打分 → o_proj。
- **两个注意力分支**：仅在「整段前向、无 padding、无缓存」时走 `scaled_dot_product_attention`（Flash）；单 token 解码、带缓存、带 padding mask 时退回手动 `Q@K^T + causal mask` 实现，优先保证语义可控。
- **KV Cache** 在序列维 `dim=1` 上 `cat` 累积，增量推理时缓存长度按 `历史长度 + 当前步长度` 增长。

## 7. 下一步学习建议

- 本讲把 RoPE 当成黑盒使用了（只调了 `apply_rotary_pos_emb` 和 `precompute_freqs_cis`）。下一讲 **[u3-l3 RoPE 旋转位置编码与 YaRN 长度外推](u3-l3-rope-and-yarn.md)** 会打开这两个函数，讲清旋转矩阵如何编码相对位置，以及 YaRN 如何在推理时外推到 4 倍长度。
- 本讲的 `Attention` 只负责「token 之间交换信息」，每个位置独立经过 FeedForward 还做一次「特征维的非线性变换」。这部分见 **[u3-l4 SwiGLU 前馈网络与 MoE 路由](u3-l4-swiglu-and-moe.md)**，那里也会把 `MiniMindBlock` 的 Pre-Norm + 残差结构补全。
- 想看 Attention 如何在真实生成循环里被反复调用、KV Cache 如何跨步传递，可以提前扫一眼 [u3-l6 自定义 generate](u3-l6-custom-generate.md)，但建议先按顺序学完 u3-l3、u3-l4、u3-l5。
