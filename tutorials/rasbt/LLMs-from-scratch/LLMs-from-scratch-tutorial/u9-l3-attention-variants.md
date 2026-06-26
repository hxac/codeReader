# 注意力变体：GQA / MLA / SWA

## 1. 本讲目标

在 [u9-l1](u9-l1-kv-cache.md) 里，我们给注意力装上了 **KV Cache**：把历史 token 的 Key/Value 缓存下来，避免每生成一个新词就把整段历史重算一遍。它把推理从 \(O(n^2)\) 的累积开销降到接近 \(O(n)\)，是自回归解码的「标配」。

但 KV Cache 并不免费——**它本身要占显存**，而且随序列长度**线性增长**。当上下文拉到 32k、128k 甚至更长，或者在服务端同时跑很多请求（大 batch）时，KV Cache 占用的显存往往比模型权重本身还大，成为长上下文 LLM 的头号内存瓶颈。

本讲讲解三种主流的「省 KV 内存」注意力变体，它们从三个完全不同的角度攻击同一个问题：

| 变体 | 全称 | 省内存的思路 | 代表模型 |
|------|------|--------------|----------|
| **GQA** | Grouped-Query Attention（分组查询注意力） | **结构**上：让多个 query 头共享同一组 K/V 头，减少 K/V 头的数量 | Llama 2/3、Gemma 3、Qwen3 |
| **MLA** | Multi-Head Latent Attention（多头潜注意力） | **维度**上：把 K/V 压缩进低维「潜空间」再缓存 | DeepSeek V2/V3/R1 |
| **SWA** | Sliding Window Attention（滑动窗口注意力） | **时间**上：每个 query 只看局部窗口内的 token，限制缓存回溯长度 | Gemma 2/3、Longformer |

学完本讲，你应当能够：

1. 说清 MHA 的 KV Cache 内存公式，理解为什么长上下文下它成为瓶颈。
2. 读懂三种变体的源码实现，理解它们各自「在哪一刀」省下了内存。
3. 用仓库自带的 `memory_estimator` 脚本定量估算不同变体的 KV 内存，并画出「内存随上下文长度」的曲线。
4. 理解三者是**正交**的（可组合，Gemma 3 就同时用了 GQA + SWA），并能根据场景做取舍。

---

## 2. 前置知识

本讲是 advanced 阶段的内容，假设你已经掌握：

- **多头注意力（MHA）**（[u3-l3](u3-l3-multihead-attention.md)）：query/key/value 各有 `num_heads` 个头，每个头维度 `head_dim = emb_dim / num_heads`，`view` + `transpose` 做无拷贝切头。
- **KV Cache**（[u9-l1](u9-l1-kv-cache.md)）：缓存历史 K/V、`use_cache` 开关、`current_pos` 位置指针、`reset_kv_cache`、prefill/decode 两阶段。

如果你对「为什么要缓存 K/V」还不清楚，强烈建议先读 u9-l1，因为本讲三种变体**全部建立在 KV Cache 之上**——它们省的就是这个 cache 的大小。

### 2.1 先把「内存瓶颈」量化

MHA 在单层、单个样本下，要为序列里每个 token 缓存它的 Key 和 Value。每个头贡献 `head_dim` 个元素，共 `num_heads` 个头，K 和 V 各一份（所以乘 2）：

\[
\text{KV cache (一层)} \approx \text{batch} \times L \times \text{head\_dim} \times n_{\text{heads}} \times 2 \times b
\]

其中 \(L\) 是序列长度，\(b\) 是每个元素的字节数（bf16/fp16 为 2）。由于 \(n_{\text{heads}} \times \text{head\_dim} = \text{emb\_dim}\)，可化简为：

\[
\text{KV cache (一层)} \approx \text{batch} \times L \times \text{emb\_dim} \times 2 \times b
\]

再乘层数 \(n_{\text{layers}}\) 就是全模型总量。直觉记法：**KV cache 大小正比于 batch × 序列长 × 模型宽 × 层数**。

举个真实数字（仓库 README 里的例子）：`emb_dim=4096, n_heads=32, n_layers=32, context_length=32768, bf16, batch=1`，纯 MHA 的 KV cache 高达 **17.18 GB**——这还只是「记得住历史」要花的钱，模型权重另算。本讲三种变体，就是来砍这 17 GB 的。

---

## 3. 本讲源码地图

三个变体各自放在 `ch04/` 下的独立 bonus 目录里，结构高度对称：每个目录都有一份「变体实现」、一份「MHA 基线对照」、一个内存估算器、一个绘图脚本、一个 README。

| 文件 | 作用 |
|------|------|
| [ch04/04_gqa/gpt_with_kv_gqa.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/04_gqa/gpt_with_kv_gqa.py) | GQA 版 GPT：`GroupedQueryAttention` + 带 KV cache 的 `GPTModel` |
| [ch04/04_gqa/memory_estimator_gqa.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/04_gqa/memory_estimator_gqa.py) | 估算 MHA vs GQA 的 KV cache 字节数 |
| [ch04/05_mla/gpt_with_kv_mla.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/05_mla/gpt_with_kv_mla.py) | MLA 版 GPT：`MultiHeadLatentAttention`（灵感来自 DeepSeek） |
| [ch04/05_mla/memory_estimator_mla.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/05_mla/memory_estimator_mla.py) | 估算 MHA vs GQA vs MLA 的 KV cache 字节数 |
| [ch04/06_swa/gpt_with_kv_swa.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/gpt_with_kv_swa.py) | SWA 版 GPT：`MultiHeadAttentionWithSWA` + K:1 混合层调度 |
| [ch04/06_swa/memory_estimator_swa.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/memory_estimator_swa.py) | 估算 MHA/GQA 叠加 SWA（按层比例）的 KV cache 字节数 |
| [ch04/06_swa/tests.py](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/tests.py) | SWA 正确性测试：验证「窗口=上下文长」时 SWA 等价于普通 MHA |

**重要约定**：三个目录里的 `GPTModel`、`TransformerBlock`、`LayerNorm`、`GELU`、`FeedForward`、`generate_text_simple_cached` 几乎一模一样，**唯一的差异就在那个注意力类**。所以本讲聚焦在三个注意力类的差异上，其余骨架视为 u9-l1 的复用。

---

## 4. 核心概念与源码讲解

### 4.1 GQA：分组查询注意力（共享 K/V 头）

#### 4.1.1 概念说明

标准 MHA 里，每个 query 头都配「专属」的一对 Key/Value 头。GQA 的想法很直接：**让多个 query 头共享同一组 K/V 头**，从而减少 K/V 头的总数。

用一个超参 `num_kv_groups`（K/V 组数）控制共享程度：

- `num_kv_groups == num_heads`：每个头独占一组 → 退化为标准 **MHA**。
- `num_kv_groups == 1`：所有头共享同一组 K/V → 极端情况叫 **MQA（Multi-Query Attention，多查询注意力）**，最省内存但质量可能掉。
- 介于两者之间（如 Llama 系列常用的 4~8 组）：**GQA 的甜点区**，内存省得多、质量几乎不掉。

为什么省内存？因为 KV cache 的大小正比于 K/V 头的数量 \(n_{\text{kv\_heads}}\)。GQA 把它从 \(n_{\text{heads}}\) 降到 \(n_{\text{heads}} / n_{\text{kv\_groups}}\)，理论上直接省 \(n_{\text{kv\_groups}}\) 倍。

#### 4.1.2 核心流程

1. **Query 走全量**：`W_query` 仍投影出全部 `num_heads × head_dim = emb_dim` 维（query 头数不变）。
2. **K/V 走「缩水」**：`W_key`/`W_value` 只投影出 `num_kv_groups × head_dim` 维（比 `emb_dim` 小）。
3. **缓存缩水版**：KV cache 只存这少量的 K/V 头 → 内存省下来。
4. **计算前「吹胀」**：做注意力前，用 `repeat_interleave` 把每组 K/V 复制 `group_size` 份，让 K/V 头数重新对齐 query 头数，之后照常算缩放点积注意力。

「先存小的、用到再复制」是关键——存的是省内存的小版本，复制只发生在前向计算里（且 GPU 上 `repeat_interleave` 很便宜）。

#### 4.1.3 源码精读

`GroupedQueryAttention.__init__` 里，K/V 投影的输出维度比 query 小，这是 GQA 的本质：

K/V 投影到 `num_kv_groups * head_dim`，而 query 投影到全量 `d_out`，并算出每组覆盖几个 query 头 —— [ch04/04_gqa/gpt_with_kv_gqa.py:32-37](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/04_gqa/gpt_with_kv_gqa.py#L32-L37)

```python
self.W_key = nn.Linear(d_in, num_kv_groups * self.head_dim, ...)
self.W_value = nn.Linear(d_in, num_kv_groups * self.head_dim, ...)
self.num_kv_groups = num_kv_groups
self.group_size = num_heads // num_kv_groups
self.W_query = nn.Linear(d_in, d_out, ...)
```

两个 `assert` 守住可整除性（`d_out % num_heads == 0`、`num_heads % num_kv_groups == 0`），否则切头会除不尽。`group_size` 就是「每个 K/V 组要喂给几个 query 头」，后面靠它决定复制几份。

前向里，reshape 后 K/V 的头维是 `num_kv_groups`，缓存也只存这么多头；做点积前用 `repeat_interleave` 沿头维（dim=1）把每组复制 `group_size` 份 —— [ch04/04_gqa/gpt_with_kv_gqa.py:71-80](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/04_gqa/gpt_with_kv_gqa.py#L71-L80)

```python
# Expand keys and values to match the number of heads
keys = keys_base.repeat_interleave(self.group_size, dim=1)
values = values_base.repeat_interleave(self.group_size, dim=1)
# 例如 group_size=2 时，[K1, K2] 经 repeat_interleave 变成 [K1, K1, K2, K2]
# 若用普通 repeat 则是 [K1, K2, K1, K2]（错误的配对）
```

这段注释点破了一个易错点：必须用 `repeat_interleave`（每组连续重复）而不是 `repeat`（整体重复），否则头 1/2 和 K1/K2 的对应关系就乱了。吹胀之后，`queries @ keys.transpose(2,3)` 与标准 MHA 在数学上完全一致，缩放因子仍是 `head_dim**0.5`，因果掩码照常。换句话说，**GQA 没有改变注意力算式，只改变了 K/V 从哪来、cache 存多少**。

> 💡 与 u9-l1 的关系：KV cache 部分（`cache_k`/`cache_v` 两个 buffer、`use_cache` 开关、`reset_cache`、位置指针 `ptr_current_pos`）与 u9-l1 完全同构，只是现在缓存的是「缩水版」K/V，所以更省内存。

#### 4.1.4 代码实践

**目标**：亲眼看到 GQA 的 K/V 投影比 query 小、且吹胀后头数对齐。

**步骤**（在仓库根目录用 Python 交互）：

```python
# 示例代码
import torch
from ch04_04_gqa.gpt_with_kv_gqa import GroupedQueryAttention  # 视你的 sys.path 而定
# 或直接把文件放进同目录: from gpt_with_kv_gqa import GroupedQueryAttention

torch.manual_seed(0)
att = GroupedQueryAttention(d_in=768, d_out=768, dropout=0.0,
                            num_heads=12, num_kv_groups=4)
print("W_query 输出维:", att.W_query.out_features)   # 期望 768（全量）
print("W_key   输出维:", att.W_key.out_features)     # 期望 num_kv_groups*head_dim = 4*64 = 256（缩水）
print("group_size:", att.group_size)                  # 期望 3（12头/4组）

x = torch.randn(1, 5, 768)
out = att(x, use_cache=False)
print("输出形状:", tuple(out.shape))                  # 期望 (1, 5, 768)
print("cache 头数:", att.cache_k.shape[1])            # use_cache=False 时为 None
```

**观察**：`W_key` 的输出维度是 `num_kv_groups × head_dim = 4 × 64 = 192`，远小于 `W_query` 的 768。这就是 GQA 省内存的物理来源。

**预期结果**：`W_query=768`、`W_key=256`、`group_size=3`、输出形状 `(1, 5, 768)`。若 `use_cache=False`，`cache_k` 为 `None`。

#### 4.1.5 小练习与答案

**练习 1**：把 `num_kv_groups` 设成 `12`（等于 `num_heads`），此时 GQA 等价于什么？

> **答案**：等价于标准 MHA——每个 query 头都有专属 K/V 头，`group_size=1`，`repeat_interleave` 复制 1 份（即不复制）。这也是 `memory_estimator` 里 MHA 的取法：`n_kv_heads_mha = n_heads`。

**练习 2**：把 `num_kv_groups` 设成 `1`，会变成哪种注意力？有什么代价？

> **答案**：变成 **MQA（多查询注意力）**，所有 12 个头共享同一组 K/V，最省内存。代价是建模质量可能下降（README 提到「极端情况下内存骤降但性能可能受损」），所以实践中常折中选 4~8 组。

---

### 4.2 MLA：多头潜注意力（压缩 K/V 进潜空间）

#### 4.2.1 概念说明

GQA 是「把 K/V 头变少」，MLA 换了个完全不同的刀法：**K/V 头数不变，但把它们压缩进一个低维的「潜空间」（latent space）再缓存**。这是 DeepSeek V2/V3/R1 用的招。

类比：GQA 像「几个人合用一份笔记」，MLA 像「把整份笔记压缩成摘要存档，要用时再解压还原」。还原需要一次额外的矩阵乘法（多花一点算力），但存的只是摘要，所以 cache 大幅变小。

引入 `latent_dim`（潜空间维度，通常 ≪ `emb_dim`）：

- 缓存里只存每个 token 的 `latent_dim` 维潜向量 \(c\)（**一份**，不再分 K 和 V，所以不乘 2）。
- 推理时用两个「上投影」矩阵 \(W_{UK}, W_{UV}\) 把 \(c\) 分别还原成完整的 K 和 V，再正常做多头注意力。

> 📌 教学简化说明：真实 DeepSeek 的 MLA 还包含 query 的压缩、解耦 RoPE 等细节。本仓库是教学版（README 注明灵感来自 [deepseek-mla](https://huggingface.co/bird-of-paradise/deepseek-mla)），query 这里**不压缩**，只压缩 K/V 路径，足以讲清「压缩再缓存」的核心思想。

#### 4.2.2 核心流程

1. **下投影**：`W_DKV` 把输入 \(x\)（`emb_dim` 维）压成潜向量 \(c\)（`latent_dim` 维）。
2. **只缓存潜向量**：`cache_c_kv` 存的是低维 \(c\)，而非完整 K/V。
3. **上投影**：`W_UK`/`W_UV` 把累积的潜序列还原成完整 K/V（`emb_dim` 维）。
4. **切头 + 注意力**：照常 reshape 成多头、做缩放点积注意力。

内存公式（注意**没有 ×2**，因为 K/V 共享同一份潜向量）：

\[
\text{KV cache (MLA, 全模型)} \approx \text{batch} \times L \times n_{\text{layers}} \times \text{latent\_dim} \times b
\]

对比 MHA 的 `batch × L × n_layers × emb_dim × 2 × b`，MLA 用 `latent_dim` 替换了 `emb_dim × 2`。

#### 4.2.3 源码精读

`__init__` 里能看到「下投影 + 两个上投影」的三件套，以及**只缓存潜向量**的 `cache_c_kv` —— [ch04/05_mla/gpt_with_kv_mla.py:33-48](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/05_mla/gpt_with_kv_mla.py#L33-L48)

```python
self.latent_dim = latent_dim if latent_dim is not None else max(16, d_out // 8)
self.W_query  = nn.Linear(d_in, d_out, bias=qkv_bias)            # query 走全量
self.W_DKV    = nn.Linear(d_in, self.latent_dim, bias=qkv_bias)  # 下投影: emb -> latent
self.W_UK     = nn.Linear(self.latent_dim, d_out, bias=qkv_bias) # 上投影: latent -> K
self.W_UV     = nn.Linear(self.latent_dim, d_out, bias=qkv_bias) # 上投影: latent -> V
...
self.register_buffer("cache_c_kv", None, persistent=False)       # 只缓存潜向量
```

`latent_dim` 默认 `max(16, d_out // 8)`——比如 `emb_dim=768` 时默认 `latent_dim=96`，相比 MHA 要存的 `emb_dim × 2 = 1536`，压缩比约 16×。

前向里最关键的是「先更新潜缓存、再上投影」的顺序 —— [ch04/05_mla/gpt_with_kv_mla.py:65-86](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/05_mla/gpt_with_kv_mla.py#L65-L86)

```python
queries_all = self.W_query(x)        # (b, T, d_out)
latent_new  = self.W_DKV(x)          # (b, T, latent_dim)  ← 下投影
if use_cache:
    if self.cache_c_kv is None:
        latent_total = latent_new
    else:
        latent_total = torch.cat([self.cache_c_kv, latent_new], dim=1)  # 只拼潜向量
    self.cache_c_kv = latent_total
keys_all   = self.W_UK(latent_total) # (b, T_k, d_out)  ← 上投影还原 K
values_all = self.W_UV(latent_total) # (b, T_k, d_out)  ← 上投影还原 V
```

注意三点：① 缓存拼接的是 `latent_new`（`latent_dim` 维），不是完整 K/V；② 上投影 `W_UK`/`W_UV` 作用在**整个累积潜序列**上，每步 decode 都要把历史潜向量一起还原（这就是 README 说的「额外矩阵乘法」代价）；③ K 和 V 从同一份潜向量 \(c\) 还原，故 cache 只存一份。之后的切头与注意力与 MHA 完全相同。

> 🔁 与 GQA 对比：GQA 让 K/V 头「变少」，MLA 让 K/V「变扁」（低维）。两者都保留完整 query 路径，都不改变注意力算式本身。

#### 4.2.4 代码实践

**目标**：验证 MLA 的缓存维度是 `latent_dim`，而不是 `emb_dim`。

**步骤**：

```python
# 示例代码
import torch
from gpt_with_kv_mla import MultiHeadLatentAttention  # 文件置于同目录

torch.manual_seed(0)
att = MultiHeadLatentAttention(d_in=768, d_out=768, dropout=0.0,
                               num_heads=12, latent_dim=96)
x = torch.randn(1, 1, 768)        # 模拟 decode 阶段单 token
_ = att(x, use_cache=True)        # 第 1 步: 建潜缓存
print("潜缓存形状:", tuple(att.cache_c_kv.shape))  # 期望 (1, 1, 96)
_ = att(x, use_cache=True)        # 第 2 步: 拼接
print("潜缓存形状:", tuple(att.cache_c_kv.shape))  # 期望 (1, 2, 96)
```

**观察**：缓存的最后一维是 `96`（`latent_dim`），而非 `768`（`emb_dim`）。如果这是 MHA，缓存的是 K/V，最后一维应是 `768` 且要存两份。

**预期结果**：两次前向后 `cache_c_kv` 形状依次为 `(1, 1, 96)`、`(1, 2, 96)`。

#### 4.2.5 小练习与答案

**练习 1**：MLA 的 cache 公式里为什么**不乘 2**（不像 MHA 那样区分 K 和 V）？

> **答案**：因为 K 和 V 是从**同一份**潜向量 \(c\) 经 `W_UK`、`W_UV` 还原出来的，缓存里只存这一份 \(c\)。MHA 则要把 K 和 V 各存一份，所以乘 2。

**练习 2**：把 `latent_dim` 设得极小（比如 8），会发生什么？

> **答案**：内存更省，但潜向量承载的信息太少，K/V 还原后表达能力下降，建模质量会受损。README 明确指出 `latent_dim` 是需要仔细调的超参——太小会负面影响性能，类似 GQA 里 `n_kv_groups` 选太大的问题。

---

### 4.3 SWA：滑动窗口注意力（只看局部窗口）

#### 4.3.1 概念说明

GQA 和 MLA 都在「每个 query 仍看全部历史」的前提下省内存，SWA 更激进：**限制每个 query 只能看它前后一个固定大小 \(W\) 的窗口**，把「全局注意力」变成「局部注意力」。

直觉：远处的历史对预测下一个词通常没那么重要，把它从注意力里剔掉，KV cache 也只需保留最近 \(W\) 个 token，不必无限堆积。

但纯局部注意力会丢失长程信息，所以现代模型（如 Gemma 3）用**混合策略**：大部分层用 SWA（省内存），少量层用全局注意力（保长程能力）。Gemma 3 用 **5:1** 比例——每 5 层 SWA 接 1 层全局；Gemma 2 用 **1:1**。

SWA 的内存收益：把公式里的序列长 \(L\) 换成窗口 \(W\)，每个 SWA 层省 \(W/L\) 倍。

#### 4.3.2 核心流程

1. **窗口掩码**：query \(i\) 只能 attend 到 key \(j\)，当 \(0 \le i-j < W\)（在因果约束下，往后看 \(W\) 步以内）。用位置差 `diff` 一次性生成「因果 + 窗口」布尔掩码。
2. **截断缓存**：KV cache 只保留最近 \(W\) 个 token 的 K/V，超出窗口的历史直接丢弃。
3. **K:1 混合调度**：在 `GPTModel` 里按 `sliding_window_stride`（记作 K）安排：K 层 SWA 接 1 层全局，循环填满 `n_layers`。

一个关键不变量：**当窗口 \(W\) 等于上下文长度时，SWA 退化为普通 MHA**——因为窗口大到能看见全部历史。仓库的 `tests.py` 正是用来验证这个等价性的安全网。

#### 4.3.3 源码精读

掩码是 SWA 的灵魂。用 query/key 的绝对位置差，一把生成「因果（`diff<0` 屏蔽未来）+ 窗口（`diff>=W` 屏蔽太远的过去）」合并掩码 —— [ch04/06_swa/gpt_with_kv_swa.py:112-121](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/gpt_with_kv_swa.py#L112-L121)

```python
W = num_tokens_K + 1 if self.sliding_window_size is None else int(self.sliding_window_size)
diff = q_positions.unsqueeze(-1) - k_positions.unsqueeze(0)
mask_bool = (diff < 0) | (diff >= W)     # 未来(diff<0)或太远(diff>=W)都屏蔽
...
attn_scores.masked_fill_(mask_bool, -torch.inf)
```

注意 `sliding_window_size is None` 时 \(W\) 取 `num_tokens_K + 1`（一个大到不触发窗口裁剪的值），即该层退化为普通全局因果注意力——这正是 K:1 调度里「全局层」的取法。

缓存截断逻辑保证 KV cache 不超过窗口大小，prefill 分块时还要多留 \(W-1\) 个旧 key 给块内最早的 query —— [ch04/06_swa/gpt_with_kv_swa.py:68-82](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/gpt_with_kv_swa.py#L68-L82)

```python
if self.sliding_window_size is not None:
    # prefill 分块时最多保留 W-1 个旧 key + 整个当前块
    attn_keep = min(keys.size(1), self.sliding_window_size + num_tokens - 1)
    keys = keys[:, -attn_keep:, :, :]
    values = values[:, -attn_keep:, :, :]
    cache_keep = min(combined_k.size(1), self.sliding_window_size)
    self.cache_k = combined_k[:, -cache_keep:, :, :]   # 缓存只留最近 W 个
    self.cache_v = combined_v[:, -cache_keep:, :, :]
```

这段处理了 SWA 配合 KV cache 的微妙处：计算当前块注意力时要多借 \(W-1\) 个历史 key（`attn_keep`），但缓存本身只存 \(W\) 个（`cache_keep`）。注释解释了为什么——「让块内最早的 query 仍有完整窗口上下文」。

K:1 混合层调度写在 `GPTModel.__init__` 里，决定哪些层用 SWA、哪些用全局 —— [ch04/06_swa/gpt_with_kv_swa.py:232-247](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/gpt_with_kv_swa.py#L232-L247)

```python
K = int(window_stride)          # sliding_window_stride
if K <= 0:
    use_swa = False if K == 0 else True   # K=0 全局; K<0 全 SWA
else:
    group = K + 1
    use_swa = (i % group) < K             # 每 K+1 层里前 K 层是 SWA
blk.att.sliding_window_size = window_size if use_swa else None
```

所以命令行 `--sliding_window_stride 5`（README 的 Gemma 3 例子）就是 5:1——每 6 层里 5 层 SWA、1 层全局。设成 `None` 的层即退化为普通 MHA 层。

> 🧪 正确性兜底：[tests.py:37-71](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/tests.py#L37-L71) 把 SWA 模型（窗口=上下文长、stride=-1 即全 SWA）加载上普通 MHA 的权重，断言两者 logits 和生成 token 完全一致——证明「窗口足够大时 SWA == MHA」，这是 SWA 实现正确性的金标准。

#### 4.3.4 代码实践

**目标**：验证 SWA 的缓存确实被截断到窗口大小，且窗口=上下文长时等价于 MHA。

**步骤**：

```python
# 示例代码
import torch
from gpt_with_kv_swa import MultiHeadAttentionWithSWA

torch.manual_seed(0)
att = MultiHeadAttentionWithSWA(d_in=8, d_out=8, dropout=0.0,
                                num_heads=2, sliding_window_size=4)
att.eval()
x = torch.randn(1, 6, 8)        # 6 个 token，但窗口只有 4
_ = att(x, use_cache=True)
print("cache_k 时序长度:", att.cache_k.size(1))   # 期望 4（被截到窗口大小）
```

再对照 `tests.py` 的思路：当 `sliding_window_size == context_length` 时，运行 `pytest ch04/06_swa/tests.py`（或直接调用 `test_swa_matches_base_model_when_window_equals_context`）。

**观察**：尽管输入 6 个 token，缓存只剩 4 个（窗口大小）。`tests.py` 应全部通过，证明窗口足够大时 SWA 与 MHA 逐位一致。

**预期结果**：`cache_k.size(1) == 4`；`pytest` 两个测试通过。如本地未配置 `llms_from_scratch` 包，`tests.py` 中 `from llms_from_scratch.ch04 import GPTModel` 会报 ImportError，此时可只跑前一个单层测试 `test_cached_prefill_matches_uncached_swa`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：如果所有层都用 SWA（`sliding_window_stride=-1`），长程信息会怎样？

> **答案**：信息只能在窗口 \(W\) 内逐层传递，跨越窗口的依赖要经过很多层才能「接力」传到远方，容易丢失长程上下文。这正是 Gemma 3 用 5:1 混合（留少量全局层）的原因——全局层充当「信息枢纽」把长程信号重新散播开。

**练习 2**：为什么 SWA 的内存收益在大上下文下最明显？

> **答案**：SWA 把 cache 公式里的 \(L\) 换成 \(W\)，省的倍数是 \(W/L\)。\(L\) 越大（上下文越长），\(W/L\) 越小，省得越多。比如 \(W=1024, L=32768\) 时省 32 倍；\(L=1024\) 时则一点都不省（\(W=L\)）。

---

### 4.4 KV 内存权衡：用估算器横向对比

#### 4.4.1 概念说明

前面三个模块分别讲了「怎么省」，本模块把它们放进**同一个公式框架**里定量比较。三个目录各带一个 `memory_estimator_*.py`，本质都是在算同一个东西——KV cache 字节数，只是替换不同的「省内存因子」：

| 变体 | 替换的因子 | 公式（单层，简化） |
|------|-----------|-------------------|
| MHA（基线） | — | \(\text{batch} \times L \times \text{head\_dim} \times n_{\text{heads}} \times 2 \times b\) |
| GQA | \(n_{\text{heads}} \to n_{\text{heads}}/n_{\text{kv\_groups}}\) | 把上面的 \(n_{\text{heads}}\) 换成 \(n_{\text{kv\_heads}}\) |
| MLA | \(\text{emb\_dim}\times 2 \to \text{latent\_dim}\) | \(\text{batch} \times L \times \text{latent\_dim} \times b\)（无 ×2） |
| SWA | \(L \to W\) | 把序列长 \(L\) 换成窗口 \(W\)（仅 SWA 层） |

一句话总结三刀：**GQA 砍头数、MLA 砍维度、SWA 砍长度**。三者正交，可叠加（Gemma 3 = GQA + SWA；README 也提到 MLA 理论上可与 GQA 叠加，但暂无知名模型这么做）。

#### 4.4.2 核心流程

1. 用 `calc_kv_bytes_total` 算 MHA/GQA 的 cache：`batch × L × head_dim × n_kv_heads × 2 × b × n_layers`。
2. 用 `calc_mla_bytes_total` 算 MLA：`batch × L × n_layers × latent_dim × b`。
3. SWA 估算器多了「层比例」逻辑：按 `swa_ratio`（如 `5:1`）把 `n_layers` 拆成 SWA 层和全局层，SWA 层用 \(W\)、全局层用 \(L\)，加权求和。
4. 输出各变体的总字节数、相对 MHA 的压缩比与节省百分比。

#### 4.4.3 源码精读

GQA 估算器的核心公式 —— [ch04/04_gqa/memory_estimator_gqa.py:26-30](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/04_gqa/memory_estimator_gqa.py#L26-L30)

```python
def calc_kv_bytes_total(batch, context_length, emb_dim, n_heads,
                             n_kv_heads, n_layers, bytes_per_elem):
    head_dim = math.ceil(emb_dim / n_heads)
    per_layer = batch * context_length * head_dim * n_kv_heads * 2 * bytes_per_elem
    return per_layer * n_layers
```

MHA 调用时传 `n_kv_heads = n_heads`，GQA 调用时传 `n_kv_heads = n_heads // n_kv_groups`——**同一个函数，靠 `n_kv_heads` 参数区分两种注意力**。这就是 GQA 的全部内存本质：少传几个 KV 头。

MLA 估算器多了一个不含 ×2 的潜向量公式 —— [ch04/05_mla/memory_estimator_mla.py:33-36](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/05_mla/memory_estimator_mla.py#L33-L36)

```python
def calc_mla_bytes_total(batch, context_length, n_layers, latent_dim, bytes_per_elem):
    # 每个 token 只存 latent_dim 维潜向量（K/V 共享，不乘 2）
    return batch * context_length * n_layers * latent_dim * bytes_per_elem
```

SWA 估算器最复杂，要按层比例把 SWA 层（用有效窗口 `eff_W = min(L, W)`）和全局层（用 \(L\)）分开算再求和 —— [ch04/06_swa/memory_estimator_swa.py:63-76](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/06_swa/memory_estimator_swa.py#L63-L76)

```python
eff_W = min(context_length, sliding_window_size)   # 窗口不超过序列长
per_mha_swa = calc_kv_bytes_per_layer(batch_size, eff_W, head_dim, n_kv_heads_mha, bytes_per_elem)
per_mha_full = calc_kv_bytes_per_layer(batch_size, L, head_dim, n_kv_heads_mha, bytes_per_elem)
...
total_mixed_mha = n_swa_layers * per_mha_swa + n_full_layers * per_mha_full
```

`distribute_layers` 按 `a:b` 比例把 `n_layers` 分成 `a` 份 SWA 和 `b` 份全局，于是「GQA + SWA（5:1）」这种组合也能一行算出（README 的例子：MHA 17.18GB → GQA+SWA 仅 0.78GB）。

#### 4.4.4 代码实践

**目标**：用估算器跑一组真实大模型配置，直观看内存差距。

**步骤**（在 `ch04/04_gqa/` 与 `ch04/05_mla/` 目录下分别运行）：

```bash
# 1) GQA: Llama-3 风格大配置
cd ch04/04_gqa
uv run memory_estimator_gqa.py \
  --emb_dim 4096 --n_heads 32 --n_layers 32 \
  --context_length 32768 --n_kv_groups 4 --dtype bf16

# 2) MLA: DeepSeek 风格配置
cd ../05_mla
uv run memory_estimator_mla.py \
  --emb_dim 2048 --n_heads 24 --n_layers 48 \
  --context_length 8192 --n_kv_groups 4 --latent_dim 1024 --dtype bf16
```

**观察**：第 1 个会打印 MHA 17.18GB vs GQA 4.29GB（省 75%）；第 2 个会打印 MHA 3.25GB vs GQA 0.81GB vs MLA 0.81GB。

**预期结果**：与各 README 给出的数字一致。注意 MLA 与 GQA 在这套配置下节省比例接近（README 也指出，这是 `latent_dim` 选得使压缩比相近的结果）。

**待本地验证**：若未装 `uv`，可改用 `python memory_estimator_gqa.py ...`；无网络/无 GPU 也不影响，估算器是纯算术、不碰模型。

#### 4.4.5 小练习与答案

**练习 1**：为什么 README 里 GQA 真实跑 `gpt_with_kv_gqa.py`（生成 32768 token）时，内存只从 1.54GB 降到 0.63GB，远没有估算器的 4× 那么夸张？

> **答案**：README 列了两点：① 用了较小的配置让生成在合理时间完成；② 更重要的是，`torch.cuda.max_memory_allocated()` 测的是**整个模型**的峰值显存，而前馈层（FFN）权重占了大部分——KV cache 只是其中一块。估算器算的是**纯 KV cache**，两者口径不同。

**练习 2**：给定 `emb_dim=4096, n_heads=32, latent_dim=512`，MLA 相对 MHA 的 KV cache 压缩比是多少？

> **答案**：MHA 每 token 存 `emb_dim × 2 = 8192` 个元素，MLA 存 `latent_dim = 512` 个，压缩比 \(8192/512 = 16\times\)。代入公式也一致：`(emb_dim×2) / latent_dim = 16`。

---

## 5. 综合实践

**任务**：画出「MHA / GQA / MLA 的 KV cache 内存随上下文长度增长」的对比曲线，把三个变体放进同一张图，直观比较它们的内存随 \(L\) 的增长速度。

这是本讲规格里指定的实践任务，依据是仓库自带的 `plot_memory_estimates_gqa.py` 思路（它已示范了「循环多个 context_length、调用 `calc_kv_bytes_total`、`plt.plot` 并存 PDF」）。

**步骤**：

1. 在 `ch04/05_mla/` 目录新建一个脚本（**示例代码**，非仓库原有文件）：

```python
# 示例代码: compare_kv_memory.py (放到 ch04/05_mla/ 下)
import matplotlib.pyplot as plt
from memory_estimator_mla import calc_kv_bytes_total, calc_mla_bytes_total, DTYPE_BYTES

# DeepSeek 风格固定配置
emb_dim, n_heads, n_layers, batch = 2048, 24, 48, 1
n_kv_groups, latent_dim = 4, 1024
b = DTYPE_BYTES["bf16"]
n_kv_heads_gqa = n_heads // n_kv_groups

context_lengths = [256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
def to_gb(n): return n / (1000**3)

mha, gqa, mla = [], [], []
for L in context_lengths:
    mha.append(to_gb(calc_kv_bytes_total(batch, L, emb_dim, n_heads, n_heads, n_layers, b)))
    gqa.append(to_gb(calc_kv_bytes_total(batch, L, emb_dim, n_heads, n_kv_heads_gqa, n_layers, b)))
    mla.append(to_gb(calc_mla_bytes_total(batch, L, n_layers, latent_dim, b)))

plt.figure()
plt.plot(context_lengths, mha, "o-", label="MHA")
plt.plot(context_lengths, gqa, "s-", label="GQA (4 groups)")
plt.plot(context_lengths, mla, "^-", label="MLA (latent=1024)")
plt.xscale("log"); plt.yscale("log")
plt.xlabel("context_length (log)"); plt.ylabel("KV cache total (GB, log)")
plt.legend(); plt.grid(True, which="both"); plt.tight_layout()
plt.savefig("compare_kv_memory.pdf")
print("已保存 compare_kv_memory.pdf")
```

2. 运行：`uv run compare_kv_memory.py`（或 `python compare_kv_memory.py`）。
3. （可选）用 SWA 估算器再算一组「GQA + SWA 5:1」的点，叠到图上。

**需要观察的现象**：

- 三条曲线都随 \(L\) **线性**增长（log-log 图上呈 45° 直线），因为公式里 \(L\) 都是一次项。
- **截距不同**：MHA 最高、GQA 与 MLA 明显更低且互相接近（在这套 `latent_dim` 下）。
- 上下文每翻倍，三条线同步上移一倍——印证「KV cache 随长度线性增长」。

**预期结果**：生成 `compare_kv_memory.pdf`，MHA 线在最上方、GQA/MLA 在下方且基本平行。在这套配置（`emb_dim=2048, n_heads=24, n_layers=48, latent_dim=1024, n_kv_groups=4`）下，\(L=32768\) 处 MHA ≈ 13 GB、GQA ≈ 3.25 GB、MLA ≈ 3.2 GB（\(L=8192\) 时三者分别约为 3.25 / 0.81 / 0.81 GB，与 05_mla 的 README 一致）。若你加上了 SWA（\(W=1024\)，5:1），SWA 那条线会在长上下文处进一步下探。

> 这个实践把本讲串起来：公式（4.4）→ 三种变体的差异（4.1~4.3）→ 一张图直观呈现「砍头数 / 砍维度 / 砍长度」各自砍掉了多少。

---

## 6. 本讲小结

- **共同动机**：长上下文 + 大 batch 下，KV cache 随序列长线性增长，常常比模型权重还大，是推理内存的头号瓶颈。GQA/MLA/SWA 都是为省这块内存而生。
- **GQA（砍头数）**：多个 query 头共享一组 K/V 头，`W_key`/`W_value` 投影到 `num_kv_groups × head_dim`，前向用 `repeat_interleave` 吹胀对齐；`num_kv_groups=num_heads` 退化为 MHA，`=1` 为 MQA。
- **MLA（砍维度）**：K/V 先下投影到 `latent_dim` 潜向量再缓存（只存一份、不乘 2），推理时上投影还原；用「额外一次矩阵乘」换「cache 变扁」。
- **SWA（砍长度）**：每个 query 只看窗口 \(W\) 内的 token，cache 只留最近 \(W\) 个；K:1 混合调度（如 Gemma 3 的 5:1）兼顾局部省内存与全局长程能力。
- **统一公式**：KV cache ≈ batch × 序列长 × 每token维度 × 层数 × 字节数；GQA 减头数、MLA 减每 token 维度、SWA 减有效序列长，三者正交可叠加。
- **正确性兜底**：GQA 吹胀后注意力算式不变；SWA 在窗口=上下文长时与 MHA 逐位等价（`tests.py` 守护）。

---

## 7. 下一步学习建议

- **看真实模型怎么用这些变体**：[ch05/07_gpt_to_llama](https://github.com/rasbt/LLMs-from-scratch/tree/main/ch05/07_gpt_to_llama) 的 Llama3、[ch05/11_qwen3](https://github.com/rasbt/LLMs-from-scratch/tree/main/ch05/11_qwen3)、[ch05/12_gemma3](https://github.com/rasbt/LLMs-from-scratch/tree/main/ch05/12_gemma3) 都用了 GQA，Gemma3 还叠了 SWA——这是 [u10-l2](u10-l2-modern-llm-architectures.md) 的内容，建议接着读。
- **回到 KV cache 本身**：若对本讲频繁出现的 `use_cache`、`current_pos`、prefill/decode 还不熟，重读 [u9-l1](u9-l1-kv-cache.md)。
- **深入 MLA 的工程细节**：本讲是教学版 MLA（无解耦 RoPE）。想了解 DeepSeek 真实实现的「吸收」技巧与 RoPE 解耦，可阅读 [DeepSeek-V2 论文](https://arxiv.org/abs/2405.04434)。
- **动手扩展**：尝试在本讲的 SWA 实现里叠加 GQA（让 `MultiHeadAttentionWithSWA` 也支持 `num_kv_groups`），用 `memory_estimator_swa.py` 的「GQA + SWA」一行验证内存，复刻 Gemma 3 的组合。
