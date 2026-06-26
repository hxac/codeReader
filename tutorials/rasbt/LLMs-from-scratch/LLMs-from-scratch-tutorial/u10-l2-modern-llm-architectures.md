# 现代 LLM 架构概览：Qwen3 / Gemma3

## 1. 本讲目标

本讲承接 [u10-l1（GPT 转 Llama：RoPE 与 RMSNorm）](./u10-l1-gpt-to-llama.md)，把视野从「单个 Llama 架构」扩展到「真实工业界在用的现代 LLM」。学完后你应当能够：

- 读懂 **Qwen3** 的从零实现（稠密版），并说清它相对 Llama / GPT 多了哪些零件（`qk_norm`、解耦的 `head_dim`、SwiGLU、推理 `<think>` 标签等）。
- 读懂 **Gemma3** 的从零实现，并说清它最具辨识度的四件事：**滑动窗口 + 全局注意力的 5:1 混合调度**、**双套 RoPE**、**零中心 RMSNorm**、**MQA 与嵌入缩放**。
- 拿一张表对比「GPT → Llama → Qwen3 / Gemma3」在**归一化、位置编码、注意力、前馈、数据精度**上的逐项差异，建立「现代 LLM = 在解码器骨架上做零件替换」的统一认知。

> 本讲是「概览 + 对比」性质，重在**读结构、做对比**；RoPE、RMSNorm、GQA、MoE、KV cache 等单点机理已在 u10-l1、u9 系列讲透，这里只把它们当作已知零件拼装，不重复推导。

## 2. 前置知识

阅读本讲前，请确认你已掌握（否则建议先读对应讲义）：

- **GPT 解码器骨架**（u4 全系列）：嵌入 → 若干 TransformerBlock → final_norm → out_head，因果自回归生成。
- **RMSNorm 与 RoPE**（u10-l1）：为什么现代模型用 RMSNorm 取代 LayerNorm、用旋转位置编码取代可学习绝对位置嵌入。
- **GQA 与 KV 内存**（u9-l1、u9-l3）：分组查询注意力如何减少 KV cache、滑动窗口注意力（SWA）如何只看局部窗口。
- **MoE**（u9-l4）：把稠密 FFN 换成「多专家 + top-k 门控」的稀疏激活思想（Qwen3 的大模型变体正是 MoE）。

两个本项目贯穿全书的结论会反复用到：

- **head_dim 过去是算出来的，现代模型是配置出来的。** GPT 里 `head_dim = emb_dim / n_heads`；而 Qwen3 / Gemma3 都把 `head_dim` 写进配置字典，使其成为**独立超参**，于是 `W_query` 的输出维 `num_heads * head_dim` 可以**大于** `emb_dim`。
- **现代模型几乎都 weight tying**：输出头 `out_head` 与词嵌入 `tok_emb` 共享权重（u4-l3、u5-l4 已解释），因此「总参数量 − 去重后参数量 ≈ 一张 `emb_dim × vocab_size` 的大矩阵」。

## 3. 本讲源码地图

本讲只读两个「自包含」notebook，它们各自把整条模型流水线写在一个文件里，下载预训练权重后即可生成文本：

| 文件 | 作用 | 模型规模 |
| --- | --- | --- |
| [ch05/11_qwen3/standalone-qwen3.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb) | Qwen3 **稠密版**从零实现：`FeedForward` / `RMSNorm` / `GroupedQueryAttention` / `TransformerBlock` / `Qwen3Model` + 配置 + 权重加载 + 生成 | 0.6B / 1.7B / 4B / 8B / 14B / 32B |
| [ch05/12_gemma3/standalone-gemma3.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb) | Gemma3 从零实现：同样的五大组件，但注意力和归一化策略不同 | 270M |

> 说明：Qwen3 还有 **MoE 变体**（30B-A3B，含 Thinking / Instruct / Coder），写在同目录的 `standalone-qwen3-moe.ipynb`，详见 [README.md:8](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/README.md#L8) 与 [README.md:19](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/README.md#L19)。其门控路由机理已在 u9-l4 讲透，本讲只在 4.1 末尾点出它的存在，不重复展开。两个 notebook 各自还有 `*-plus-kvcache.ipynb` 版本，是叠加了 KV cache 的推理加速版（机理见 u9-l1），不影响架构理解。

## 4. 核心概念与源码讲解

### 4.1 Qwen3 架构

#### 4.1.1 概念说明

Qwen3（阿里通义千问第三代）的架构被作者在 notebook 开头一句话点破：**「Many architectural components in Qwen3 are similar to Llama 3」**。也就是说，把 u10-l1 学到的 Llama 架构拿来，再做几处针对性升级，就是 Qwen3。这些升级点正是本节要抓的「最小模块」：

1. **QK-Norm（Q/K 归一化）**：在注意力内部，对投影后的 query 和 key 各做一次 RMSNorm，稳定大上下文下注意力分数的尺度。这是 Qwen3 / Gemma3 共有、而原始 Llama 没有的零件。
2. **解耦的 `head_dim`**：`head_dim` 写死为 128，与 `emb_dim`、`n_heads` 无关，于是每个注意力头更「宽」，`W_query` 的输出维度 `num_heads*head_dim` 可以超过 `emb_dim`。
3. **GQA（`n_kv_groups=8`）**：16 个 query 头共享 8 组 KV，介于标准 MHA 与 MQA 之间（u9-l3 已讲机理）。
4. **SwiGLU 前馈**：`silu(fc1(x)) * fc2(x)` 再经 `fc3`，三个矩阵的门控式 FFN（u10-l1 已讲）。
5. **长上下文 RoPE（`rope_base=1_000_000`）**：用比 GPT（`10_000`）大 100 倍的基频，把上下文撑到 40 960。
6. **推理 / 指令双形态 + `<think>` 标签**：同一权重既是「会推理」的 reasoning 模型，也能靠 tokenizer 注入空 `<think></think>` 退化为「直接回答」的 instruct 模型。

Qwen3 覆盖 0.6B ~ 32B 的稠密模型，以及 30B-A3B 的 MoE 模型，所有规模共用同一套架构代码，只是配置字典不同。

#### 4.1.2 核心流程

Qwen3 的前向数据流与 GPTModel / Llama3Model 同构，只是零件换了：

```
token IDs (b, T)
   │  tok_emb(查表)
   ▼
embeddings (b, T, emb_dim)            # 注意：Qwen3 不对嵌入做缩放
   │  for block in trf_blocks (28层):
   │     x = x + att( norm1(x),  causal_mask, cos, sin )   # pre-norm + 残差
   │             └ Q/K 投影 → qk_norm → RoPE → GQA → softmax(QKᵀ/√d)V → out_proj
   │     x = x + ff( norm2(x) )                              # pre-norm + 残差(SwiGLU)
   ▼
final_norm → out_head → logits (b, T, vocab_size)
```

两个值得记的细节：① **没有独立的位置嵌入层**——顺序信息全部由注意力内部的 RoPE 注入（`cos`/`sin` 作为非持久化 buffer 存在模型上）；② 因果掩码是标准的「上三角置 -inf」，每层都一样、没有 SWA 这种花样（这是它与 Gemma3 最显眼的区别之一）。

#### 4.1.3 源码精读

**(a) SwiGLU 前馈网络** — 三矩阵门控 FFN，`fc1` 当门、`fc2` 当上投影、`fc3` 当下投影：

```python
x = nn.functional.silu(x_fc1) * x_fc2   # SiLU 门控：silu(gate) * up
return self.fc3(x)                        # down 投影回 emb_dim
```

见 [standalone-qwen3.ipynb:156-167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L156-L167)。对比 u4-l1 的 GPT 用「GELU + 两层」非门控 FFN，现代模型普遍改成门控式以提升参数效率。

**(b) RMSNorm（带 `qwen3_compatible` 开关）** — `scale` 初始化为 1，方差用 float32 计算以保数值稳定：

```python
self.scale = nn.Parameter(torch.ones(emb_dim))   # 标准参数化：增益=1
...
variance = x.pow(2).mean(dim=-1, keepdim=True)
norm_x = x * torch.rsqrt(variance + self.eps)
norm_x = norm_x * self.scale
```

见 [standalone-qwen3.ipynb:177-198](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L177-L198)。这正是 u10-l1 讲的标准 RMSNorm，注意它与下面 Gemma3 的「零中心」版本只有一行差别。

**(c) GQA + QK-Norm 注意力** — 本节核心。`qk_norm=True` 时为 Q、K 各挂一个 `RMSNorm(head_dim)`；缩放用 `head_dim**0.5` 作除数：

```python
if qk_norm:
    self.q_norm = RMSNorm(head_dim, eps=1e-6)
    self.k_norm = RMSNorm(head_dim, eps=1e-6)
...
# forward:
queries = self.q_norm(queries)   # Q/K 各归一化一次（V 不动）
keys   = self.k_norm(keys)
queries = apply_rope(queries, cos, sin)   # RoPE 在 qk_norm 之后
keys    = apply_rope(keys, cos, sin)
keys   = keys.repeat_interleave(self.group_size, dim=1)   # GQA：把 KV 组吹胀到 query 头数
attn_weights = torch.softmax((queries @ keys.transpose(2,3)).masked_fill(mask, -torch.inf)
                             / self.head_dim**0.5, dim=-1)
```

见 [standalone-qwen3.ipynb:262-325](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L262-L325)。两个顺序要点：**先 qk_norm 再 RoPE**；GQA 的 `repeat_interleave` 机理见 u9-l3。

**(d) 配置字典（0.6B）** — 注意 `head_dim=128` 与 `emb_dim=1024`、`n_heads=16` 都解耦：

```python
QWEN3_CONFIG = {
    "vocab_size": 151_936, "context_length": 40_960, "emb_dim": 1024,
    "n_heads": 16, "n_layers": 28, "hidden_dim": 3072,
    "head_dim": 128,          # 独立超参，≠ emb_dim/n_heads(=64)
    "qk_norm": True, "n_kv_groups": 8, "rope_base": 1_000_000.0,
    "dtype": torch.bfloat16,
}
```

见 [standalone-qwen3.ipynb:442-458](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L442-L458)。后果是 `W_query: Linear(1024→2048)`，输出维 2048 > emb_dim 1024——这正是「解耦 head_dim」的直接证据（见 notebook 中打印的模型结构 `(W_query): Linear(in_features=1024, out_features=2048, bias=False)`）。

**(e) 模型组装与 RoPE buffer** — RoPE 的 `cos/sin` 预计算后用 `register_buffer(..., persistent=False)` 挂在模型上，所有层共享同一份：

```python
self.trf_blocks = nn.ModuleList([TransformerBlock(cfg) for _ in range(cfg["n_layers"])])
...
cos, sin = compute_rope_params(head_dim=cfg["head_dim"], theta_base=cfg["rope_base"],
                               context_length=cfg["context_length"])
self.register_buffer("cos", cos, persistent=False)
self.register_buffer("sin", sin, persistent=False)
```

见 [standalone-qwen3.ipynb:377-418](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L377-L418)。`persistent=False` 意味着它不进 `state_dict`（加载 OpenAI/HF 权重时不会冲突），机理见 u3-l2 的 `register_buffer` 讲解。

**(f) weight tying** — 加载权重时若无独立 `lm_head`，就让输出头复用词嵌入：

```python
if "lm_head.weight" in params:
    model.out_head.weight = assign(model.out_head.weight, params["lm_head.weight"], ...)
else:
    model.out_head.weight = model.tok_emb.weight
    print("Model uses weight tying.")
```

见 [standalone-qwen3.ipynb:835-838](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L835-L838)（在 `load_weights_into_qwen` 末尾）。这解释了为什么 0.6B 的 `total_params=751,632,384` 而去重后 `unique=596,049,920`——差额正是一张被重复计数的 `1024×151936` 嵌入矩阵（u4-l3、u5-l4 同款口径）。

**(g) MoE 变体（点到为止）** — Qwen3 的 30B-A3B 把上面的 `FeedForward` 换成 `MoEFeedForward`（多专家 + top-k 门控），其余骨架完全不变。门控路由、`index_add_` 加权聚合等机理已在 u9-l4 详细讲过；本讲不再展开 MoE notebook 的逐行代码，只强调一句：**MoE 只替换 FFN 一个零件，注意力与残差结构原样保留**。

#### 4.1.4 代码实践

**实践目标**：在不下载任何权重的前提下，亲手建一个 Qwen3 0.6B，验证「解耦 head_dim」与「weight tying 去重」两个结论。

**操作步骤**（把 notebook 里的架构类与配置抄进一个 `.py` 或新 cell）：

1. 复制 [standalone-qwen3.ipynb:156-418](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L156-L418) 的全部类与 `compute_rope_params`/`apply_rope`，以及 0.6B 配置。
2. 运行：
   ```python
   torch.manual_seed(123)
   model = Qwen3Model(QWEN3_CONFIG)
   print(model.att if False else model.trf_blocks[0].att.W_query)  # 看 W_query 形状
   total = sum(p.numel() for p in model.parameters())
   unique = total - model.tok_emb.weight.numel()
   print(total, unique)
   ```

**需要观察的现象**：
- `W_query` 打印为 `Linear(in_features=1024, out_features=2048, bias=False)`，证明 `num_heads*head_dim = 16*128 = 2048 > emb_dim=1024`。
- `total` 应为 `751,632,384`，`unique` 应为 `596,049,920`，与 notebook 输出一致。

**预期结果**：两个数字与 notebook 完全吻合即说明架构实现无误。若 `W_query` 出现 `out_features=1024`，多半是你漏配了 `head_dim`、退回了 `head_dim=emb_dim//n_heads` 的旧 GPT 口径。

**运行结果**：待本地验证（依赖 `torch`，CPU 即可，0.6B 约需 1.5GB 内存）。

#### 4.1.5 小练习与答案

**练习 1**：Qwen3 0.6B 的 `head_dim=128`，而 `emb_dim/n_heads = 1024/16 = 64`。如果把它改回「经典口径」`head_dim=64`，`W_query` 的输出维度会变成多少？参数量会变多还是变少？

> **答案**：变成 `16*64 = 1024`（等于 emb_dim）。`W_query`、`out_proj` 等矩阵变小，模型总参数量会**减少**。这说明 Qwen3 故意放大 head_dim 是为了给注意力「更宽的每个头」，代价是更多参数。

**练习 2**：`qk_norm` 对 V（value）做归一化了吗？为什么？

> **答案**：没有，只对 Q 和 K 归一化。因为 QKᵀ 的点积对 Q、K 的尺度敏感（容易随上下文变长而爆炸），归一化它们能稳定 softmax 分母；而 V 是直接被注意力权重加权求和后输出的内容，缩放它没有同样的稳定收益。

**练习 3**：Qwen3 的 reasoning 模型和 instruct 模型用的是同一份权重吗？

> **答案**：是同一份权重。notebook 注释说明：instruct 形态只是 tokenizer 在提示里注入一个**空的** `<think>\n\n</think>`，从而抑制长推理链；模型本身不变。这是「靠数据格式而非模型结构切换行为」的典型设计。

---

### 4.2 Gemma3 架构

#### 4.2.1 概念说明

Gemma3（Google）是另一个「Llama 系骨架 + 自己一套零件」的现代模型。它的 270M 从零实现写在 [standalone-gemma3.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb)，README 还附了一张 [Gemma3 与 Qwen3 的并排对比图](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/README.md#L1)。相对 Qwen3 / Llama，Gemma3 最有辨识度的四个最小模块是：

1. **5:1 滑动窗口 + 全局注意力混合调度**：18 层里，每 5 层 SWA（只看最近 `sliding_window=512` 个 token）接 1 层 full attention（看全部历史）。这是 u9-l3 讲过的「K:1 混合调度」真实落地——局部窗口省 KV 内存、隔几层来一次全局注意力保长程能力。
2. **双套 RoPE**：SWA 层用「局部 RoPE」（`rope_local_base=10_000`，短基频适合局部），full 层用「全局 RoPE」（`rope_base=1_000_000`，长基频适合长程）。两种 cos/sin 各预计算一份挂在模型上。
3. **零中心 RMSNorm**：`scale` 初始化为 **0**，前向用 `(1 + scale)` 而不是 `scale`。表达力与标准 RMSNorm 等价，但参数以 0 为中心，是 Gemma 全家族的标志性写法。
4. **MQA（`n_kv_groups=1`）+ 嵌入缩放 + 每块 4 个 Norm**：单 KV 头（比 Qwen3 的 8 组更激进）；嵌入乘 `\sqrt{emb_dim}` 放大幅度；每个 block 有 4 个 RMSNorm（比 Qwen3 的 2 个多一倍，多出来的两个用于归一化子层**输出**）。

#### 4.2.2 核心流程

Gemma3 前向的特别之处都在「进入 block 之前」和「block 内部派发」：

```
token IDs (b, T)
   │  x = tok_emb(ids) * sqrt(emb_dim)        # ← Gemma 独有：嵌入缩放
   │  mask_global, mask_local = _create_masks(T)   # 预算两套掩码
   ▼
for block in blocks (18层, 类型由 layer_types 决定):
   │  根据 block.attn_type 选 (mask_local, cos_local, sin_local) 或 (mask_global, cos_global, sin_global)
   │  shortcut=x; x=input_layernorm(x); x=att(...); x=post_attention_layernorm(x); x=shortcut+x
   │  shortcut=x; x=pre_feedforward_layernorm(x); x=ff(x); x=post_feedforward_layernorm(x); x=shortcut+x
   ▼
final_norm → out_head → logits
```

两套掩码是关键：`mask_global` 是标准因果上三角；`mask_local` 在它基础上**再屏蔽掉窗口以外的远古位置**（`mask_global | far_past`）。`_create_masks` 里用 ASCII 矩阵把这两种掩码画得清清楚楚，是理解 SWA 的最好教材。

#### 4.2.3 源码精读

**(a) 零中心 RMSNorm** — 注意 `scale` 初始化为 0、前向乘 `(1.0 + self.scale)`：

```python
# Gemma3 stores zero-centered weights and uses (1 + weight) during forward
self.scale = nn.Parameter(torch.zeros(emb_dim))      # ← 初始化为 0（不是 1）
...
var = x_f.pow(2).mean(dim=-1, keepdim=True)
x_norm = x_f * torch.rsqrt(var + self.eps)
out = x_norm * (1.0 + self.scale.float())            # ← (1 + scale)，初始化即恒等
```

见 [standalone-gemma3.ipynb:161-180](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L161-L180)。两种 RMSNorm 的统一写法：

\[ \text{RMSNorm}(x) = \frac{x}{\sqrt{\tfrac{1}{d}\textstyle\sum_{i} x_i^2 + \varepsilon}} \cdot g,\qquad g_{\text{Qwen}}=\text{scale}_q\ (\text{init }1),\quad g_{\text{Gemma}}=1+\text{scale}_g\ (\text{init }0) \]

二者表达力完全等价（都让初始增益为 1），只是参数中心不同。

**(b) GeGLU 前馈（注意是 GELU 不是 SiLU）** — 仍是三矩阵门控 FFN，但门控激活用 tanh 近似 GELU：

```python
x = nn.functional.gelu(x_fc1, approximate="tanh") * x_fc2
return self.fc3(x)
```

见 [standalone-gemma3.ipynb:140-151](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L140-L151)。即 **Gemma3 = GeGLU（GELU 门控）**，**Qwen3 = SwiGLU（SiLU 门控）**——同为门控、激活函数不同。

**(c) MQA + query_pre_attn_scalar 缩放** — `n_kv_groups=1` 即单 KV 头（MQA）；缩放因子来自 `query_pre_attn_scalar` 而非 `head_dim`，并且是**预先乘到 query 上**：

```python
if query_pre_attn_scalar is not None:
    self.scaling = (query_pre_attn_scalar) ** -0.5     # 256^-0.5 = 1/16
else:
    self.scaling = (head_dim) ** -0.5
...
queries = queries * self.scaling                        # 预缩放 query（而非除 scores）
attn_scores = queries @ keys.transpose(2, 3)
attn_weights = torch.softmax(attn_scores.masked_fill(mask, -torch.inf), dim=-1)
```

见 [standalone-gemma3.ipynb:275-278](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L275-L278) 与 [:309](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L309)。数学上 \(QK^\top/\sqrt{d}=(Q/\sqrt{d})K^\top\)，预缩放与后除等价，但预缩放让点积前的数值更小、对低精度更友好。配置里 `query_pre_attn_scalar=256` 恰好等于 `head_dim=256`，但 Gemma 把它单列为字段，便于二者解耦（见 [:550](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L550)）。

**(d) 5:1 混合调度 + 4 个 Norm 的 TransformerBlock** — block 按自身 `attn_type` 选 mask 与 RoPE，子层输出也各过一次 Norm：

```python
def __init__(self, cfg, attn_type):
    self.attn_type = attn_type
    self.input_layernorm          = RMSNorm(...)   # pre-attn
    self.post_attention_layernorm = RMSNorm(...)   # 归一化注意力输出
    self.pre_feedforward_layernorm  = RMSNorm(...) # pre-ffn
    self.post_feedforward_layernorm = RMSNorm(...) # 归一化 FFN 输出

def forward(self, x, mask_global, mask_local, cos_global, sin_global, cos_local, sin_local):
    if self.attn_type == "sliding_attention":
        attn_mask, cos, sin = mask_local, cos_local, sin_local
    else:
        attn_mask, cos, sin = mask_global, cos_global, sin_global
    x = shortcut + self.post_attention_layernorm(self.att(self.input_layernorm(x), ...))
    x = shortcut + self.post_feedforward_layernorm(self.ff(self.pre_feedforward_layernorm(x)))
```

见 [standalone-gemma3.ipynb:329-393](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L329-L393)。对比 Qwen3 每个 block 只有 `norm1/norm2` 两个 Norm（[:337-375](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L337-L375)），Gemma3 多出的 `post_*` 两个 Norm 会约束进入残差主路的子层输出尺度。

**(e) 双套掩码与双套 RoPE** — `_create_masks` 用布尔矩阵显式画出 global / local 两种掩码；模型在 `__init__` 里预算 local 与 global 两份 cos/sin：

```python
mask_global = torch.triu(ones, diagonal=1)                       # 标准因果
far_past    = torch.triu(ones, diagonal=self.cfg["sliding_window"]).T  # 太早的也屏蔽
mask_local  = mask_global | far_past                             # local = 未来 OR 远古
```

见 [standalone-gemma3.ipynb:429-472](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L429-L472)。双 RoPE 见 [standalone-gemma3.ipynb:412-427](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L412-L427)（`rope_local_base=10_000` 与 `rope_base=1_000_000`）。

**(f) 嵌入缩放（Gemma 全家族特征）** — 进入 block 之前先把嵌入放大：

```python
x = self.tok_emb(input_ids) * (self.cfg["emb_dim"] ** 0.5)
```

见 [standalone-gemma3.ipynb:477](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L477)。Qwen3 没有这一步（[standalone-qwen3.ipynb:408-409](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L408-L409) 直接 `x = tok_embeds`）。

**(g) 配置字典** — `layer_types` 显式列出 18 层里哪些是 SWA、哪些是 full：

```python
GEMMA3_CONFIG_270M = {
    "vocab_size": 262_144, "context_length": 32_768, "emb_dim": 640,
    "n_heads": 4, "n_layers": 18, "hidden_dim": 2048, "head_dim": 256,
    "qk_norm": True, "n_kv_groups": 1,                      # ← MQA
    "rope_local_base": 10_000.0, "rope_base": 1_000_000.0,  # ← 双 RoPE
    "sliding_window": 512,
    "layer_types": ["sliding_attention"*5, "full_attention", ...],  # 5:1 重复 3 次
    "query_pre_attn_scalar": 256,
}
```

见 [standalone-gemma3.ipynb:516-551](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L516-L551)（`layer_types` 完整为 5 个 sliding 接 1 个 full，循环 3 轮共 18 层）。注意 `head_dim=256` 同样与 `emb_dim/n_heads=160` 解耦，`W_query: 640→1024`。

#### 4.2.4 代码实践

**实践目标**：用「最小上下文」亲眼看到 SWA 的掩码长什么样，建立对 `mask_local` 的直觉。

**操作步骤**：

1. 抄入 [standalone-gemma3.ipynb:395-493](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L395-L493) 的 `Gemma3Model`（含 `_create_masks`），并定义一个临时配置：`sliding_window=4`，其余字段照抄 270M。
2. 直接调用掩码生成，打印出来：
   ```python
   # 借用一个已实例化的 model，或把 _create_masks 抽成独立函数
   m = Gemma3Model(GEMMA3_CONFIG_270M)          # 任意合法 cfg 即可
   mg, ml = m._create_masks(seq_len=8, device="cpu")
   print("global\n", mg.int()); print("local\n", ml.int())
   ```

**需要观察的现象**：
- `global` 是严格上三角（未来位置为 1）。
- `local` 在 `sliding_window=4` 时，除了未来，还会把「`i-j >= 4` 的远古位置」置 1——即每个 query 只剩 `[i-3, i]` 这 4 个可见位置（含自身）。

**预期结果**：`local` 矩阵应与源码注释里画的 8×8 示例完全一致（见 [:459-L471](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L459-L471) 的注释图）。这一步不下载权重、不训练，纯结构验证。

**运行结果**：待本地验证（仅需 `torch`，CPU 秒级完成）。

#### 4.2.5 小练习与答案

**练习 1**：Gemma3 270M 的 `n_kv_groups=1`、`n_heads=4`，这是 GQA 还是 MQA？相比 Qwen3 的 `n_kv_groups=8`，谁的 KV cache 更小？

> **答案**：`n_kv_groups=1` 即 MQA（所有 query 头共享唯一一组 KV，u9-l3）。KV 组数越少缓存越小，故 Gemma3(1) 的 KV cache 比 Qwen3(8) 更小——这是 Gemma3 为 262 144 大词表 + 长上下文省内存的配套设计。

**练习 2**：为什么 Gemma3 要给 SWA 层配一套「短基频」`rope_local_base=10_000`，而 full 层用「长基频」`1_000_000`？

> **答案**：SWA 层只看最近 512 个 token，位置范围小，短基频（角度变化更快）能在这个局部窗口内给出区分度足够的位置编码；full 层要看整条 32 768 长度，需要长基频让角度变化更平缓、避免高位维度周期过快导致位置混淆。两种尺度各司其职。

**练习 3**：Gemma3 每个 block 有 4 个 RMSNorm，多出来的两个 `post_*` 作用在哪？

> **答案**：`post_attention_layernorm` 归一化注意力子层的**输出**、`post_feedforward_layernorm` 归一化 FFN 子层的**输出**，然后再加回残差。也就是说 Gemma3 对「进入残差主路之前的子层结果」额外做了一次归一化，而 Qwen3 只在子层**输入**端做 pre-norm、输出直接进残差。

---

### 4.3 架构差异对比

#### 4.3.1 概念说明

把 u4 的 GPT、u10-l1 的 Llama、本讲的 Qwen3 / Gemma3 并排放，会得到一个清晰的结论：**所谓「现代 LLM」，就是在一个解码器骨架（嵌入 → N×TransformerBlock → norm → out_head）上，对归一化 / 位置编码 / 注意力 / 前馈 / 精度这五类零件做「替换与叠加」**。零件的种类是有限的、可枚举的；不同模型的差异，本质上是「选了哪几样零件、怎么组合」。本模块就是要把这种「组合关系」用一张表固定下来。

#### 4.3.2 核心流程

对比的固定套路：沿**五个维度**逐项过——① 归一化（LayerNorm? RMSNorm? 零中心? 每块几个? 是否有 qk_norm?）；② 位置编码（可学习绝对? RoPE? 单套还是双套? 基频多大?）；③ 注意力（标准 MHA? GQA? MQA? 是否 SWA 混合? 缩放怎么算?）；④ 前馈（非门控 GELU? SwiGLU? GeGLU? MoE?）；⑤ 精度与杂项（float32? bfloat16? 有无 bias? 是否 weight tying? 是否缩放嵌入?）。每个维度都能在两个 notebook 里找到对应的配置字段或代码行作为证据。

#### 4.3.3 源码精读（对比表）

下表把「GPT（u4）→ Llama（u10-l1）→ Qwen3 → Gemma3」四个架构并排。证据列指向两个 notebook 的具体行号。

| 维度 | GPT-2（ch04） | Llama（u10-l1） | **Qwen3** | **Gemma3** | 证据（Qwen3 / Gemma3） |
| --- | --- | --- | --- | --- | --- |
| 归一化 | LayerNorm（带 mean） | RMSNorm，`scale` init=1 | RMSNorm，`scale` init=1，**有 qk_norm** | RMSNorm，`scale` init=**0**，`(1+scale)`，**有 qk_norm** | [Qwen :177-198](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L177-L198) / [Gemma :161-180](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L161-L180) |
| 每块 Norm 数 | 2 | 2 | 2（+qk_norm 在注意力内） | **4**（pre/post 各一对） | [Qwen :337-375](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L337-L375) / [Gemma :329-393](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L329-L393) |
| 位置编码 | 可学习绝对位置嵌入 | RoPE（单套，theta=500k） | RoPE（单套，**theta=1e6**） | RoPE（**双套**：local 1e4 + global 1e6） | [Qwen :442-458](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L442-L458) / [Gemma :412-427](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L412-L427) |
| 注意力头组织 | 标准 MHA | GQA | GQA（**8 组**） | **MQA（1 组）** | [Qwen :262-325](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L262-L325) / [Gemma :244-317](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L244-L317) |
| 注意力掩码 | 纯因果 | 纯因果 | 纯因果（每层一致） | **5:1 SWA/full 混合** | [Gemma :429-472](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L429-L472) |
| 缩放方式 | `/√head_dim` | `/√head_dim` | `/√head_dim`（后除） | `×query_pre_attn_scalar^-0.5`（**预乘**） | [Qwen :322](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L322) / [Gemma :275-309](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L275-L309) |
| 前馈 | GELU，两层（非门控） | SwiGLU（SiLU 门控） | SwiGLU（SiLU 门控） | **GeGLU（GELU 门控）** | [Qwen :156-167](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L156-L167) / [Gemma :140-151](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L140-L151) |
| head_dim | = emb_dim/n_heads（算出） | = emb_dim/n_heads | **解耦**（128，独立配置） | **解耦**（256，独立配置） | [Qwen :451](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L451) / [Gemma :523](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L523) |
| Linear 偏置 | 有 | 无 | 无 | 无 | 见各 `nn.Linear(..., bias=False)` |
| 嵌入缩放 | 无 | 无 | 无 | **×√emb_dim** | [Gemma :477](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L477) |
| 数据精度 | float32 | bfloat16 | bfloat16 | bfloat16 | [Qwen :455](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L455) / [Gemma :549](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L549) |
| weight tying | 否（独立 out_head） | 是 | 是 | 是 | [Qwen :835-838](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L835-L838) / [Gemma :879-881](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L879-L881) |

读这张表的三条主线：

1. **归一化**：从「带均值的 LayerNorm」一路收敛到「RMSNorm」，并在 Qwen3/Gemma3 上叠加了 `qk_norm`；Gemma3 进一步把参数零中心化、并给每个 block 翻倍到 4 个 Norm。
2. **位置编码**：从「可学习绝对嵌入」统一换成 RoPE；Gemma3 为了配合 SWA 又裂变出「双套基频」。
3. **注意力**：从标准 MHA 演化出 GQA（Qwen3）→ MQA（Gemma3）的 KV 压缩谱系；Gemma3 还把「纯因果」升级成「SWA/full 混合」。

#### 4.3.4 代码实践

**实践目标**：亲手填充上表中 Qwen3 与 Gemma3 的差异，并用代码核验关键字段。

**操作步骤**：

1. 打开两个配置字典：[standalone-qwen3.ipynb:442-458](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L442-L458) 与 [standalone-gemma3.ipynb:516-551](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L516-L551)。
2. 分别 `Qwen3Model(cfg)`、`Gemma3Model(cfg)` 实例化（不需下载权重），打印 `print(model)` 对比模块结构。
3. 用下面的核对清单逐项填表（归一化 / 位置编码 / 注意力三列各找 ≥1 处差异）。

**需要观察的现象 / 预期结果**（即本讲正式实践任务的答案骨架）：

| 差异点 | Qwen3 0.6B | Gemma3 270M |
| --- | --- | --- |
| **归一化①** 每块 Norm 数 | 2（`norm1/norm2`，外加注意力内 qk_norm） | **4**（`input/post_attention/pre_feedforward/post_feedforward`） |
| **归一化②** 参数中心 | `scale` init=1，乘 `scale` | `scale` init=**0**，乘 `(1+scale)` |
| **位置编码** | 单套 RoPE，`rope_base=1e6` | **双套** RoPE，local=1e4 / global=1e6 |
| **注意力①** KV 组织 | GQA，`n_kv_groups=8` | **MQA**，`n_kv_groups=1` |
| **注意力②** 掩码 | 每层纯因果 | **5 层 SWA + 1 层 full** 循环（`sliding_window=512`） |
| **注意力③** 缩放 | 后除 `/√head_dim` | 预乘 `query_pre_attn_scalar^-0.5` |
| （附加）前馈 | SwiGLU（SiLU） | GeGLU（GELU） |
| （附加）嵌入缩放 | 无 | `×√emb_dim` |

**预期结果**：填出的表与 4.3.3 一致。若想再严谨，可对同一句提示分别跑两个模型的 `generate_text_basic_stream`（[Qwen :1109](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L1109) / [Gemma :1142](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L1142)），二者生成循环结构完全相同——再次印证「骨架不变、零件不同」。

**运行结果**：结构对比部分待本地验证（CPU 即可）；权重下载与文本生成部分需 Hugging Face 访问权限（Gemma3 还需先接受 Google 许可），属可选项。

#### 4.3.5 小练习与答案

**练习 1**：如果要把 Qwen3 的某个 TransformerBlock 改造成「Gemma3 风格」，至少要动哪几处？

> **答案**：① 把 `norm1/norm2` 扩成 4 个 Norm 并在子层输出处加 `post_*`；② 给 block 加 `attn_type` 并能接收 `mask_local/cos_local/sin_local`；③ 注意力的 `n_kv_groups` 从 8 改成 1、缩放改成预乘 `query_pre_attn_scalar`；④ FFN 的 SiLU 换成 GELU。注意力本身的 Q/K 投影、RoPE、softmax 结构都不用改。

**练习 2**：Qwen3 和 Gemma3 都「解耦了 head_dim」。这件事会让哪两个权重矩阵的形状发生变化？为什么和 GPT 不一样？

> **答案**：`W_query`（出 `num_heads*head_dim`）和 `W_key`/`W_value`（出 `num_kv_groups*head_dim`）。GPT 里 `head_dim=emb_dim//n_heads` 是被算出来的，所以这些矩阵的输出维被 `emb_dim` 间接锁定；现代模型让 `head_dim` 自由配置，输出维就能脱离 `emb_dim` 独立伸缩（如 Qwen3 的 `W_query: 1024→2048`）。

**练习 3**：两个模型都把 RoPE 的 `cos/sin` 用 `register_buffer(..., persistent=False)` 挂在模型级而不是每层各算一份。这样做的好处是什么？

> **答案**：① 所有层共享同一份预计算结果，省去每层重复计算的开销；② `persistent=False` 使其不进入 `state_dict`，加载 HuggingFace / OpenAI 预训练权重时不会与外部 checkpoint 冲突（u3-l2 已讲 buffer 机制）；③ 随 `model.to(device)` 自动迁移到 GPU，不会触发设备不一致错误。

---

## 5. 综合实践

**任务：把 Qwen3 与 Gemma3 的「注意力前向」抽象成一个统一对照实验，量化 KV 压缩差异。**

要求：

1. 从两个 notebook 抄入各自的 `GroupedQueryAttention`、`compute_rope_params`、`apply_rope`（[Qwen :210-325](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/11_qwen3/standalone-qwen3.ipynb#L210-L325)、[Gemma :192-317](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/standalone-gemma3.ipynb#L192-L317)）。
2. 各实例化一层注意力（用各自配置的 `head_dim`、`n_heads`、`n_kv_groups`），喂同一段随机输入 `(1, 512, emb_dim)`。
3. 用 u9-l3 的 KV 内存估算口径，比较「Qwen3 GQA(8 组)」与「Gemma3 MQA(1 组)」单层的 K/V 缓存量比值。
4. 把你的发现写成一句话结论，附在实验脚本后面。

**预期结论**：在相同 `head_dim` 与序列长度下，KV 缓存量正比于 `num_kv_groups`，故 Gemma3 的 MQA 单层 KV 缓存约为 Qwen3 GQA 的 \(1/8\)；代价是共享程度更高、理论上表达力略降——这正是 Gemma3 为 262 144 大词表 + 32k 上下文腾出内存的取舍。

> 这个任务把本讲三个模块（Qwen3 的 GQA、Gemma3 的 MQA、二者的差异对比）串到一条可量化的实验线上，并自然衔接到 u9-l3 的 KV 内存公式。

**运行结果**：待本地验证（仅需 `torch`，CPU 可跑）。

## 6. 本讲小结

- **Qwen3 ≈ Llama 3 + qk_norm + 解耦 head_dim + 长上下文 RoPE**：架构与 Llama 高度同构，亮点是在注意力内对 Q/K 各做一次 RMSNorm，并把 `head_dim` 设为独立超参（0.6B 的 `W_query: 1024→2048`）。
- **Gemma3 的四张名片**：5:1 的 SWA/full 混合调度、双套 RoPE（local 1e4 / global 1e6）、零中心 RMSNorm（`(1+scale)`）、MQA（`n_kv_groups=1`）+ 嵌入缩放 + 每块 4 个 Norm。
- **共性趋势**：两者都用 RMSNorm、RoPE、门控 FFN（SwiGLU/GeGLU）、GQA/MQA、bfloat16、weight tying、解耦 head_dim——这就是 2024–2025 年现代 LLM 的「标配零件箱」。
- **差异本质**：Qwen3 偏「保守改良」（贴近 Llama、靠 qk_norm 稳定注意力）；Gemma3 偏「激进省内存」（MQA + SWA + 双 RoPE，为超大词表与长上下文腾空间）。
- **阅读方法**：所有差异都能在两个 notebook 的**配置字典**与**注意力类**里直接读出来——配置字段是「声明」，`forward` 里的算子调用是「实现」，二者对照即可验证任何一条架构结论。
- **MoE 是正交扩展**：Qwen3 的 30B-A3B 只把 FFN 换成 MoE，骨架与注意力完全不变，机理见 u9-l4。

## 7. 下一步学习建议

- **走向推理加速**：本讲两个模型都有 `*-plus-kvcache.ipynb` 版本。建议接着读它们，对照 u9-l1，亲手验证「KV cache 版与朴素版逐位相同但更快」。
- **走向权重工程**：u10-l3 讲「内存高效权重加载 / 扩展 tokenizer / 训练加速」，正好承接本讲末尾的「下载并加载 HuggingFace 权重」环节，可顺带解决大 state_dict 的内存峰值问题。
- **走向对齐与评估**：u11 系列讲 LoRA、DPO、LLM-as-a-judge。你可以把本讲的 Qwen3 / Gemma3 当作「可替换骨架」，在它们之上做指令微调或偏好对齐实验，体会「架构不变、只换训练阶段」的全流程。
- **横向扩展阅读**：作者在 Gemma3 README 里指向了文章 *The Big LLM Architecture Comparison*（[README.md:33](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch05/12_gemma3/README.md#L33)），把本讲的对比表扩展到 DeepSeek-V3、Kimi K2 等更多模型，推荐作为本讲的「广度延伸」读物。
