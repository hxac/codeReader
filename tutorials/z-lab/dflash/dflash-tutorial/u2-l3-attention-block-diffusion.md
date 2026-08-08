# DFlash 注意力与块扩散机制

## 1. 本讲目标

本讲打开 DFlash 草稿模型最核心的一个组件——`Qwen3DFlashAttention`。学完本讲，你应该能够：

1. 说清 DFlash 注意力为什么有「两路输入」：来自 target 的上下文（context）与本块待去噪的噪声（noise）。
2. 解释 `k_ctx / k_noise`、`v_ctx / v_noise` 拼接后 key/value 序列长度的含义，并理解 query 只来自噪声。
3. 说清为什么这里 `is_causal=False`，以及「块内双向注意力」如何实现块扩散的并行起草。
4. 看懂 `extract_context_feature` 如何把 target 多层隐藏状态沿特征维拼起来，以及 `offset=1` 的含义。
5. 理解 `apply_rotary_pos_emb` 如何让上下文 key 和噪声 key「各就其位」地获得正确的旋转位置编码。

本讲承接 [u2-l2](u2-l2-draft-model-architecture.md) 已经建立的心智模型：草稿模型是「残缺」的，它通过 `fc + hidden_norm` 把 target 多层特征投影成一路 `target_hidden`，再向 target 借 `embed_tokens` 与 `lm_head`。本讲要回答的是：这路投影好的上下文特征，到底是怎么进入注意力、和噪声块一起被并行去噪的。

## 2. 前置知识

阅读本讲前，建议先掌握以下概念：

- **标准自注意力**：给定 query/key/value，计算 \(\mathrm{softmax}(QK^\top/\sqrt{d})V\)。常规因果语言模型会给注意力加一个下三角掩码（causal mask），让每个位置只能看到自己和之前的 token。
- **分组查询注意力（GQA）**：query 头数 \(H_q\) 多于 key/value 头数 \(H_{kv}\)，若干个 query 头共享一组 kv，节省显存。Qwen3 默认采用 GQA。
- **旋转位置编码（RoPE）**：通过对 q、k 做旋转注入位置信息，核心运算是 `rotate_half` 后与 `cos/sin` 相乘。位置由 `position_ids` 决定。
- **块扩散（block diffusion）**：DFlash 的起草不是「一个 token 一个 token 往右走」，而是一次拿出一整块（`block_size` 个）全是 `<mask>` 的位置，让模型对整块**并行去噪**还原出 token。这正是它能并行起草的根本原因。
- **两个「借」与上下文特征**（来自 [u2-l2](u2-l2-draft-model-architecture.md)）：草稿模型没有自己的 `embed_tokens` 和 `lm_head`；它还通过 `fc` 投影把 target 的多层隐藏状态「翻译」成自己的上下文表示 `target_hidden`。

一句话复习：在 `dflash_generate` 的 decode 循环里，每轮草稿模型都会拿到「投影好的上下文 `target_hidden`」和「本块 mask token 的嵌入 `noise_embedding`」两路输入，对整块并行去噪，产出候选 token（见 [u2-l1](u2-l1-spec-decoding-control-flow.md)）。本讲就钻进这次去噪的注意力内部。

## 3. 本讲源码地图

本讲只涉及一个文件，但会反复在它的几个区段之间跳转：

| 区段 | 行号 | 作用 |
| --- | --- | --- |
| `extract_context_feature` | `dflash/model.py:39-45` | 从 target 的多层隐藏状态里挑出 `target_layer_ids` 指定的层，沿特征维拼接 |
| `apply_rotary_pos_emb` | `dflash/model.py:176-182` | DFlash 自定义的 RoPE 应用，处理 q（仅噪声）与 k（上下文+噪声）长度不等的情况 |
| `Qwen3DFlashAttention.__init__` | `dflash/model.py:186-209` | 注意力的投影层定义，并把 `is_causal` 写死为 `False` |
| `Qwen3DFlashAttention.forward` | `dflash/model.py:211-255` | 本讲主角：拼接 context 与 noise 的 key/value 并做注意力 |
| `Qwen3DFlashDecoderLayer.forward` | `dflash/model.py:267-299` | 把两路输入（target_hidden、noise）接到注意力上，外加残差与 MLP |
| `DFlashDraftModel.forward` | `dflash/model.py:323-347` | 对 `target_hidden` 做 `fc + hidden_norm` 投影后，逐层下发给每个 decoder layer |
| `dflash_generate` 调用点 | `dflash/model.py:99`、`115`、`143` | 上下文特征的抽取、传给草稿的 `position_ids`、按接受长度切片 |

## 4. 核心概念与源码讲解

### 4.1 块扩散注意力的两路输入：上下文与噪声

#### 4.1.1 概念说明

普通 Qwen3 decoder layer 只有一路输入：上一层的 `hidden_states`。而 DFlash 的每一层同时拿到**两路**输入：

1. **上下文（context）`target_hidden`**：来自目标模型，代表「已经确定、干净、可信」的前文表示。它是被 `fc` 投影压回 `hidden_size` 后的产物（见 [u2-l2](u2-l2-draft-model-architecture.md)），在整次草稿前向里只算一次，所有层共享同一份。
2. **噪声（noise）`hidden_states`**：本块 `block_size` 个 `<mask>` token 经过 target 的 `embed_tokens` 得到的嵌入，代表「待去噪、待并行还原」的本块。

你可以把这次注意力理解为一种**「以干净上下文为条件、对噪声块做联合去噪」**的操作：噪声块里的每个位置都要在干净上下文的指引下，互相参照着一起决定自己该填什么 token。这正是块扩散并行起草在注意力层面的体现。

#### 4.1.2 核心流程

`Qwen3DFlashDecoderLayer.forward` 的数据流（去掉 norm/残差细节）：

```text
输入: noise(hidden_states), context(target_hidden)
  │
  ├─ hidden_states = input_layernorm(noise)
  ├─ attn_out = Qwen3DFlashAttention(
  │       hidden_states = hidden_states,      # 噪声
  │       target_hidden  = target_hidden)     # 上下文
  ├─ hidden_states = noise_residual + attn_out
  ├─ hidden_states = post_attention_layernorm(hidden_states)
  ├─ hidden_states = MLP(hidden_states)
  └─ return hidden_states_residual + hidden_states
```

注意：`target_hidden`（上下文）**只参与注意力**，不进 MLP、不进残差；它纯粹作为注意力的 key/value 来源。MLP 和残差都只作用在噪声流 `hidden_states` 上。这条「上下文不污染残差流」的边界很关键——草稿要输出的是本块的去噪结果，上下文只是参照物。

#### 4.1.3 源码精读

先看 `DFlashDraftModel.forward` 如何把上下文准备好并逐层下发。[dflash/model.py:333-346](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L333-L346)：`hidden_states = noise_embedding`，`target_hidden = self.hidden_norm(self.fc(target_hidden))`（投影一次），然后把**同一份** `target_hidden` 传给每一层。

再看 decoder layer 怎么接线，[dflash/model.py:280-294](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L280-L294)：

- 第 281 行 `hidden_states = self.input_layernorm(hidden_states)`：对**噪声**做归一化。
- 第 282-293 行把归一化后的噪声（作为 `hidden_states`）和原始上下文（`target_hidden`）一起送进 `self.self_attn`。
- 第 294 行 `hidden_states = residual + hidden_states`：残差加的是**噪声流**，上下文不参与残差。

#### 4.1.4 代码实践

**实践目标**：确认「上下文只进注意力、不进残差/MLP」这条边界。

**操作步骤**：

1. 打开 [dflash/model.py:267-299](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L267-L299)。
2. 用两个不同颜色的高亮分别标记 `target_hidden` 和 `hidden_states` 在 `Qwen3DFlashDecoderLayer.forward` 中的每一次出现。
3. 数一数：`target_hidden` 出现在哪些行？`hidden_states`（噪声流）出现在残差与 MLP 中吗？

**需要观察的现象**：`target_hidden` 只在第 282-284 行（传入注意力）出现一次；残差 `residual` 与 `MLP` 的输入都来自噪声流。

**预期结果**：上下文是一条「只读」支路，只贡献注意力的 key/value，不进入主残差流。这与普通 cross-attention 的残差结构类似，但这里的「编码器输出」是 target 的隐藏状态。

> 待本地验证：若你在本地用 Transformers 后端跑通生成，可在 `Qwen3DFlashDecoderLayer.forward` 里对 `target_hidden` 和 `hidden_states` 各打一行 `print(tuple(x.shape))`，确认二者的序列维不同（上下文长度 ≠ 噪声块长度）。

#### 4.1.5 小练习与答案

**练习 1**：如果误把 `target_hidden` 也加进 `residual`（即 `hidden_states = residual + attn_out + target_hidden`），会破坏什么？

**参考答案**：会让上下文的表示泄漏进噪声残差流，使草稿输出不再只表示「本块去噪结果」，后续 `lm_head` 算出的 logits 会失真，破坏与 target 的对齐，导致验证接受率骤降。上下文必须保持「只读支路」。

**练习 2**：为什么所有 decoder layer 共享同一份投影后的 `target_hidden`，而不是每层各自重新投影？

**参考答案**：投影 `fc + hidden_norm` 是为了把 target 多层特征翻译到草稿的表示空间，这个「翻译」对所有层都成立，且只算一次能省算力；每层真正「个性化」的是各自的 `q/k/v/o_proj`，它们会从这份共享上下文里按需抽取信息。

---

### 4.2 Qwen3DFlashAttention：key/value 拼接与并行起草

#### 4.2.1 概念说明

这是 DFlash 最核心的一段代码。它的特点可以浓缩成三句话：

1. **query 只来自噪声**：`q = q_proj(hidden_states)`，长度 = `block_size`。
2. **key/value 由「上下文 + 噪声」拼接而成**：先分别对 `target_hidden`（上下文）和 `hidden_states`（噪声）各做一次 `k_proj/v_proj`，再沿序列维 `cat` 在一起。
3. **`is_causal=False`**：注意力不加任何因果掩码，整块噪声之间是**双向**可见的。

合起来就是：本块的每一个 query 位置，都能同时看到「全部干净上下文」和「本块所有噪声位置」，从而在单次前向里对整块并行给出每个位置的预测。这就是块扩散「一次起草一整块」在注意力里的实现方式。

#### 4.2.2 核心流程

记 `bsz=1`，噪声块长 `q_len = block_size`，上下文长 `ctx_len`，query 头数 `H_q`，kv 头数 `H_kv`，每头维度 `head_dim`。注意力前向的关键步骤：

```text
q       = q_proj(noise)              # (1, q_len,  H_q*head_dim)
k_ctx   = k_proj(target_hidden)      # (1, ctx_len, H_kv*head_dim)   ← 上下文
k_noise = k_proj(noise)              # (1, q_len,   H_kv*head_dim)   ← 噪声
v_ctx   = v_proj(target_hidden)
v_noise = v_proj(noise)

k = cat([k_ctx, k_noise], dim=1)     # (1, ctx_len+q_len, H_kv*head_dim)
v = cat([v_ctx, v_noise], dim=1)

# 重排头维度、应用 RoPE、写入 draft 缓存、调用 attn_fn（无因果掩码）
attn = softmax(q·kᵀ / √head_dim) · v  # 双向注意力，q_len 个 query 对 ctx_len+q_len 个 key
out = o_proj(attn)                    # (1, q_len, hidden_size)
```

注意 `cat` 的顺序是**上下文在前、噪声在后**。所以拼接后 key 序列长度 `ctx_len + q_len` 的含义是：**前 `ctx_len` 个 key 来自干净上下文，后 `q_len` 个 key 来自本块噪声**。

注意力分数矩阵的形状是 `(q_len, ctx_len + q_len)`：每个噪声 query 既 attend 到全部上下文（前 `ctx_len` 列），也 attend 到本块全部噪声（后 `q_len` 列）。由于 `is_causal=False`，后 `q_len` 列内部没有下三角遮蔽，整块噪声彼此可见——这是双向并行去噪的关键。

标准缩放点积注意力写作：

\[
\mathrm{Attn}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right)V
\]

DFlash 在此基础上做两点改变：K、V 由两路拼接而来；且**不施加**因果掩码。

#### 4.2.3 源码精读

先看 `__init__` 里两处关键设置，[dflash/model.py:194](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L194)：

```python
self.is_causal = False
```

注意力从一开始就被声明为非因果。再看投影层（195-208 行）：`q_proj` 输出 `H_q*head_dim`，`k_proj/v_proj` 输出 `H_kv*head_dim`（GQA），与普通 Qwen3 注意力一致；区别全在 `forward`。

核心拼接逻辑在 [dflash/model.py:221-235](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L221-L235)：

- 第 223-225 行：`q` 只由噪声 `hidden_states` 投影而来，重排为 `(bsz, H_q, q_len, head_dim)`。
- 第 226-229 行：`k_ctx`、`v_ctx` 由**上下文** `target_hidden` 投影；`k_noise`、`v_noise` 由**噪声** `hidden_states` 投影。
- 第 230-231 行：沿序列维 `dim=1` 拼接，得到长度 `ctx_len + q_len` 的 k、v，再 `view` 出头维度。
- 第 234-235 行：对 q、k 应用旋转位置编码（见 4.4）。

随后 [dflash/model.py:236-238](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L236-L238) 把拼接后的 k、v 写入 draft 的 `DynamicCache`（该缓存的 `crop`/重启机制留待 [u2-l4](u2-l4-verify-loop-sampling.md) 详解）。最后 [dflash/model.py:239-252](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L239-L252) 选择注意力实现（`eager` / `sdpa` / `flash_attention_2`）并调用，注意它把 `is_causal=False`、`attention_mask=None` 的语义透传下去。

关于滑动窗口：第 209 行 `self.sliding_window` 仅在该层被配置为 `sliding_attention` 时才生效，普通 DFlash 层为 `None`。滑动窗口的完整用法出现在 MLX 版（见 [u3-l1](u3-l1-mlx-draft-model.md)）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 q/k/v 拼接前后的形状，并据此解释 key 序列长度与 `is_causal=False`。

**操作步骤**：

1. 在 [dflash/model.py:230](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L230) 拼接 `k` 之前插入临时打印（示例代码，**不是项目原有代码**）：

   ```python
   # 示例代码：调试用，验证后请删除
   print("k_ctx  :", tuple(k_ctx.shape))    # (1, ctx_len, H_kv*head_dim)
   print("k_noise:", tuple(k_noise.shape))  # (1, q_len,  H_kv*head_dim)
   print("k cat  :", tuple(k.shape))        # (1, ctx_len+q_len, H_kv*head_dim)
   print("q      :", tuple(q.shape))        # (1, H_q, q_len, head_dim)（transpose 前）
   ```

2. 用 Transformers 后端跑一次 `block_size>1`（如 `b16`，即 block_size=16）的小生成。
3. 取首轮 decode：此时 `ctx_len` 等于 prompt 长度（上下文是完整 prefill）。

**需要观察的现象**：

- `k_ctx` 的序列维 = `ctx_len`（上下文长度，首轮即 prompt 长度）。
- `k_noise` 的序列维 = `block_size`（如 16）。
- 拼接后 `k` 的序列维 = `ctx_len + block_size`。
- `q` 的序列维 = `block_size`。

**预期结果 / 待解释**：

- **拼接后 key 序列长度的含义**：前 `ctx_len` 个 key 是干净上下文，后 `block_size` 个 key 是本块噪声；query（也长 `block_size`）只对应噪声位置，但对全部 `ctx_len + block_size` 个 key 计算注意力。
- **为何 `is_causal=False`**：本块所有位置都是待去噪的 `<mask>`，需要彼此双向参照才能联合还原；若加因果掩码，后面的噪声位置就看不到前面的噪声位置，块扩散就退化回逐 token 起草，失去并行性。

> 待本地验证：实际数值依赖具体模型 config（头数、head_dim）与 prompt 长度，请以本地打印为准；调试结束后务必删除示例打印，不要修改源码提交。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `cat([k_ctx, k_noise], dim=1)` 改成 `cat([k_noise, k_ctx], dim=1)`（噪声在前），注意力结果会变吗？

**参考答案**：会变。因为注意力是按位置计算分数的，cat 的顺序决定了上下文与噪声在 key 序列中的排列；除非同步调整位置编码与（若有）掩码，否则 query 与「上下文/噪声」的对应关系会被打乱，结果不再正确。这也说明 4.4 要讲的 RoPE 位置必须与 cat 顺序自洽。

**练习 2**：query 头数 `H_q` 与 kv 头数 `H_kv` 在 DFlash 注意力里是否相等？为什么？

**参考答案**：通常不等（GQA），`H_q > H_kv`，多个 query 头共享一组 kv。这与普通 Qwen3 注意力一致，DFlash 没有改变 GQA 结构，只改变了 k/v 的来源与拼接方式。

---

### 4.3 extract_context_feature：从 target 多层抽取上下文

#### 4.3.1 概念说明

上下文 `target_hidden` 并非凭空而来。它来自**目标模型**在前向时产出的「各层隐藏状态」。Transformers 在 `output_hidden_states=True` 时会返回一个元组，其中：

- 第 0 个元素是**嵌入层输出**（embedding output），不是任何一层 transformer 层的输出；
- 第 `i+1` 个元素才是**第 `i` 层** transformer 层的输出。

`extract_context_feature` 要做的事，就是从 target 的这个多层隐藏状态元组里，按 `target_layer_ids`（[u2-l2](u2-l2-draft-model-architecture.md) 讲过的等距采样层号）挑出若干层，沿特征维拼成一个大张量，作为后续 `fc` 投影的输入。`offset=1` 就是为了「跳过元组开头的嵌入层」，把层号对齐到正确的元组下标。

#### 4.3.2 核心流程

```text
target 返回的 hidden_states 元组: [embed_out, layer0_out, layer1_out, ..., layer(L-1)_out]
                                        ↑ idx 0   ↑ idx 1                ↑ idx L

对每个 layer_id in target_layer_ids:
    取 hidden_states[layer_id + 1]      # offset=1，跳过 embed_out

沿特征维 cat → (bsz, seq_len, len(target_layer_ids) * hidden_size)
```

输出形状的特征维 = `len(target_layer_ids) * hidden_size`，这正是 `DFlashDraftModel.__init__` 里 `fc` 输入维度的依据（见 [u2-l2](u2-l2-draft-model-architecture.md)）。

#### 4.3.3 源码精读

[dflash/model.py:39-45](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L39-L45)：

```python
def extract_context_feature(hidden_states, layer_ids):
    offset = 1
    selected_states = [hidden_states[layer_id + offset] for layer_id in layer_ids]
    return torch.cat(selected_states, dim=-1)
```

- `offset = 1`：跳过元组第 0 个（嵌入层输出）。
- `dim=-1`：沿特征维（`hidden_size`）拼接，序列维 `seq_len` 不变。

调用点有两处，都在 `dflash_generate` 中：

- [dflash/model.py:99](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L99)：prefill 阶段抽取完整上下文（序列维 = prompt 长度）。
- [dflash/model.py:143](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L143)：每轮验证后，按接受长度切片 `[:, :acceptance_length+1, :]`，只保留被接受部分的上下文，作为下一轮草稿的 `target_hidden`。

注意第 143 行的切片：`target_hidden` 的序列维被裁到 `acceptance_length+1`，于是下一轮注意力里 `ctx_len = acceptance_length+1`（与 4.4 讲的位置自洽直接相关）。

#### 4.3.4 代码实践

**实践目标**：验证 `offset=1` 与 `target_layer_ids` 的对应关系。

**操作步骤**：

1. 在 Hugging Face 上打开任一 DFlash 草稿模型的 `config.json`（例如 `z-lab/Qwen3-8B-DFlash-b16`），记录 `num_target_layers`、`num_hidden_layers`（target 层数）、`block_size`，以及 `dflash_config` 里的 `target_layer_ids`。
2. 在本地执行（示例代码）：

   ```python
   # 示例代码
   from dflash.model import build_target_layer_ids
   print(build_target_layer_ids(num_target_layers=<target层数>, num_draft_layers=<draft层数>))
   ```

3. 把第 2 步的输出与第 1 步从 config 读到的 `target_layer_ids` 比对。

**需要观察的现象**：二者应当一致（若 config 显式给了 `target_layer_ids`，则以 config 为准；否则就是 `build_target_layer_ids` 的结果）。

**预期结果**：层号落在 `[1, num_target_layers-3]` 区间（[u2-l2](u2-l2-draft-model-architecture.md) 讲过的等距采样）。再结合 `offset=1`，确认 `extract_context_feature` 取的是 `hidden_states[层号 + 1]`，即元组里「跳过嵌入层之后」的正确层。

> 待本地验证：具体层号取决于模型 config；若网络无法下载模型，可改为阅读 `build_target_layer_ids`（[dflash/model.py:27-36](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L27-L36)）手算。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `offset` 改成 0，会发生什么？

**参考答案**：`hidden_states[0]` 是嵌入层输出，不是 transformer 层输出；`offset=0` 会让采到的「第 `layer_id` 层」实际指向元组里的 `layer_id-1` 层（甚至把嵌入层当第一层混进来），上下文特征错位，`fc` 投影后的表示质量下降。

**练习 2**：为什么用多层拼接（`cat` 沿特征维），而不是只取一层或求平均？

**参考答案**：不同深度的 target 层携带不同抽象级别的信息（浅层偏局部/词法，深层偏语义/推理）。拼接后交给 `fc` 让模型自己学习如何加权融合这些层级信息，比单层或平均表达力更强；这也是 `fc` 输入维 = `len(target_layer_ids)*hidden_size` 的原因。

---

### 4.4 apply_rotary_pos_emb：上下文与噪声各就其位

#### 4.4.1 概念说明

RoPE（旋转位置编码）靠 `cos/sin` 给 q、k 注入位置信息，而 `cos/sin` 是由 `position_ids` 算出来的。在 DFlash 注意力里有个「不对齐」的难题：

- `q` 只有 `block_size` 个位置（噪声块）；
- `k` 有 `ctx_len + block_size` 个位置（上下文 + 噪声）。

如果直接套用通用 RoPE，q 与 k 长度不等就会出错。DFlash 的处理方式是：**让 `cos/sin` 的长度等于 k 的长度**（即覆盖「上下文 + 噪声」全部位置），然后对 q 只取 `cos/sin` 的**末尾 `q_len` 个位置**——也就是噪声块对应的那些位置。这样：

- 上下文 key 落在它们真实的前缀位置；
- 噪声 key（以及 q）落在噪声块的真实位置。

拼好之后，整条 key 序列在位置轴上是连续且自洽的，RoPE 就能正确编码「上下文在前、噪声块在后」的绝对位置关系。

#### 4.4.2 核心流程

```text
cos, sin = rotary_emb(noise, position_ids)   # 长度 = len(position_ids) = ctx_len + q_len
cos = cos.unsqueeze(1); sin = sin.unsqueeze(1)

q_len = q.size(-2)                            # = block_size
q_embed = q * cos[..., -q_len:, :] + rotate_half(q) * sin[..., -q_len:, :]   # q 取末尾 q_len 个位置
k_embed = k * cos                + rotate_half(k) * sin                        # k 取全部位置
```

关键点：`cos[..., -q_len:, :]` 这一片切片，让只覆盖噪声块的 q 拿到与噪声 key **相同**的位置编码，保证 query 与「自己对应的那些 noise key」位置一致；而上下文 key 用的是 `cos` 的前缀部分，对应它们的前缀位置。

#### 4.4.3 源码精读

DFlash 自定义了 RoPE 应用函数（而非沿用从 `modeling_qwen3` 导入的版本，导入列表里只有 `rotate_half`，没有 `apply_rotary_pos_emb`），见 [dflash/model.py:176-182](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L176-L182)：

- 第 180 行 `q_embed` 用 `cos[..., -q_len:, :]`，确保 q（噪声段）取末尾 `q_len` 个位置；
- 第 181 行 `k_embed` 用完整的 `cos`，覆盖上下文+噪声全部位置。

`cos/sin` 来自哪里？在 [dflash/model.py:335](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L335)：`position_embeddings = self.rotary_emb(hidden_states, position_ids)`。`position_ids` 由 `dflash_generate` 传入，见 [dflash/model.py:115](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L115)：

```python
position_ids=position_ids[:, past_key_values_draft.get_seq_length(): start + block_size]
```

以**首轮 decode** 为例（此时 draft 缓存为空、`start = prompt 长度 N`、上下文是完整 prefill，故 `ctx_len = N`）：该切片长度 = `N + block_size`，恰好等于 k 的长度 `ctx_len + block_size = N + block_size`。

- 切片覆盖绝对位置 `[0 : N + block_size]`；
- 前 `N` 个位置（`[0:N]`）分给上下文 key——正是 prompt 的真实位置；
- 后 `block_size` 个位置（`[N : N+block_size]`）分给噪声 key 与 q——正是本块要起草的真实位置。

后续每轮因 `target_hidden` 被切片到 `acceptance_length+1`、且 `position_ids` 起点随 draft 缓存推进，这一「上下文占前缀位置、噪声占块位置」的自洽性依然成立（精确的缓存推进机制留待 [u2-l4](u2-l4-verify-loop-sampling.md)）。也就是说，cat 的顺序（上下文在前、噪声在后）与 RoPE 的位置分配是**配套设计**的，二者必须同时成立。

#### 4.4.4 代码实践

**实践目标**：验证「`cos` 长度 == k 长度 == `ctx_len + q_len`」与「q 取末尾 `q_len` 个位置」。

**操作步骤**：

1. 在 [dflash/model.py:234](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L234) 应用 RoPE 之前，打印 `cos`、`q`、`k` 的序列维长度（示例代码）：

   ```python
   # 示例代码：调试用，验证后请删除
   print("cos seq len :", cos.shape[1])          # 期望 = ctx_len + q_len
   print("q   seq len :", q.shape[-2])           # = block_size
   print("k   seq len :", k.shape[-2])           # 期望 = ctx_len + q_len
   ```

2. 跑一次首轮 decode，比较三者。

**需要观察的现象**：`cos` 的序列长度应等于 `k` 的序列长度，且都等于 `ctx_len + block_size`；`q` 的序列长度等于 `block_size`。

**预期结果**：`cos.shape[1] == k.shape[-2]`，故 `(k * cos)` 不报维度错；`q` 通过 `cos[..., -q_len:, :]` 取到与噪声 key 相同位置的那段编码。若二者不等，则说明 `position_ids` 切片与 cat 顺序不自洽（可作为排查 bug 的断言点）。

> 待本地验证：`ctx_len` 在首轮等于 prompt 长度，在后续轮等于 `acceptance_length+1`；请以本地打印为准。

#### 4.4.5 小练习与答案

**练习 1**：如果不做 `cos[..., -q_len:, :]` 切片，直接 `q * cos`，会出什么问题？

**参考答案**：q 的序列维（`block_size`）短于 `cos` 的序列维（`ctx_len + block_size`），直接相乘会因广播维度不匹配而报错；即便强行广播，q 也会拿到上下文位置的位置编码，导致 query 位置错乱。

**练习 2**：为什么 cat 顺序必须是「上下文在前、噪声在后」，而不能反过来？

**参考答案**：因为 `position_ids` 给出的绝对位置是「前缀（上下文）→ 块（噪声）」连续递增的，`cos` 的前缀段对应上下文位置、末尾段对应噪声位置。只有 cat 顺序与之同向，上下文 key 与噪声 key 才能各自拿到正确的位置编码；反过来会让上下文拿到块位置、噪声拿到前缀位置，RoPE 注入的位置信息全错。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「块扩散注意力数据流追踪」。

**任务**：画出一次草稿前向（draft forward）中，从「mask 块」到「draft logits」的完整数据流，并填出每一步的形状（用符号表达即可）。

**建议步骤**：

1. 从 `dflash_generate` 的 decode 循环出发（[dflash/model.py:107-121](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L107-L121)），标注两条输入：
   - `noise_embedding = target.model.embed_tokens(block_output_ids)`（mask 块嵌入，长度 `block_size`）；
   - `target_hidden`（由 `extract_context_feature` 抽取并按接受长度切片，长度 `ctx_len`）。
2. 进入 `DFlashDraftModel.forward`（[dflash/model.py:333-347](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L333-L347)）：标出 `target_hidden = hidden_norm(fc(target_hidden))` 这一步投影。
3. 进入一个 `Qwen3DFlashDecoderLayer`（[dflash/model.py:267-299](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L267-L299)）：标出「上下文只进注意力、噪声进残差+MLP」。
4. 进入 `Qwen3DFlashAttention.forward`（[dflash/model.py:211-255](https://github.com/z-lab/dflash/blob/94e4abc5e0c31b67bc1a9d30f1cc34ece28a8756/dflash/model.py#L211-L255)）：填一张形状表：

   | 张量 | 形状（符号） | 来源 |
   | --- | --- | --- |
   | `q` | `(1, H_q, block_size, head_dim)` | `q_proj(noise)` |
   | `k_ctx` / `v_ctx` | `(1, ctx_len, H_kv*head_dim)` | `k_proj/v_proj(target_hidden)` |
   | `k_noise` / `v_noise` | `(1, block_size, H_kv*head_dim)` | `k_proj/v_proj(noise)` |
   | `k` / `v`（cat 后） | `(1, H_kv, ctx_len+block_size, head_dim)` | `cat([ctx, noise], dim=1)` |
   | `attn_output` | `(1, block_size, hidden_size)` | 双向注意力 + `o_proj` |

5. 在图上用三种颜色分别标出：**上下文支路**（target_hidden → k_ctx/v_ctx）、**噪声支路**（noise → q、k_noise/v_noise、残差、MLP）、**位置编码**（cos/sin 覆盖 `ctx_len+block_size`，q 取末尾 `block_size`）。
6. 在图旁用一句话回答三个问题：
   - 拼接后 key 序列长度 `ctx_len + block_size` 代表什么？
   - 为什么 `is_causal=False`？
   - 为什么 `q` 要取 `cos` 的末尾 `block_size` 个位置？

> 若本地有 GPU 与已下载的 DFlash 草稿模型，可在 4.2.4、4.4.4 的打印点跑真实数据填表；否则本任务可作为「源码阅读 + 符号推导」型实践完成。所有插入的打印均为示例代码，验证后请删除，勿提交对源码的修改。

## 6. 本讲小结

- DFlash 注意力有**两路输入**：来自 target 的干净上下文 `target_hidden`，和待去噪的噪声块 `hidden_states`；上下文是「只读支路」，只进注意力、不进残差/MLP。
- `Qwen3DFlashAttention` 把 key/value 拼成 `[上下文 | 噪声]`，长度 `ctx_len + block_size`；query 只来自噪声，长度 `block_size`，对全部 key 计算注意力。
- `is_causal=False` 让块内噪声**双向可见**，这是块扩散「一次并行起草整块」的根本机制；若加因果掩码则退化为逐 token 起草。
- `extract_context_feature` 用 `offset=1` 跳过 target 隐藏状态元组开头的嵌入层，按 `target_layer_ids` 取若干层沿特征维拼接，喂给 `fc` 投影。
- `apply_rotary_pos_emb` 通过让 `cos/sin` 覆盖 `ctx_len+block_size`、q 取末尾 `block_size` 个位置，使上下文 key 与噪声 key 在位置轴上「各就其位」，与 cat 顺序配套自洽。

## 7. 下一步学习建议

本讲只看了「单次草稿前向」的注意力内部。要理解这块注意力产出的候选 token 如何被 target 验证、接受长度如何计算、draft/target 两套 KV cache 如何在每轮被裁剪回滚，请继续阅读：

- [u2-l4 验证接受循环与采样](u2-l4-verify-loop-sampling.md)：`dflash_generate` 的 decode 循环、`cumprod` 接受长度、`past_key_values.crop`、`sample()`。
- [u2-l5 Transformers 集成与权重加载](u2-l5-transformers-integration.md)：`from_pretrained` 如何加载这套注意力权重、`_no_split_modules` 的作用、注意力实现的选择与回退。
- 进阶可对比 MLX 实现 [u3-l1](u3-l1-mlx-draft-model.md) 与 [u3-l2](u3-l2-mlx-generation-cache.md)，看滑动窗口、钩子捕获 target 隐藏状态与缓存回滚在另一套代码里如何实现。
