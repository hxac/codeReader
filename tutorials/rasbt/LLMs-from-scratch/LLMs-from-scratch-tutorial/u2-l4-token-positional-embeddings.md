# Token 嵌入与位置嵌入

## 1. 本讲目标

上一讲（u2-l3）我们用滑动窗口把文本切成了形状为 `(batch, max_length)` 的 token ID 张量。但神经网络只会算**连续的浮点张量**，整数 ID 不能直接喂进去。本讲解决「把整数 ID 变成模型能吃的向量」这最后一步。

学完本讲你应该能够：

1. 理解 `nn.Embedding` 本质上是一个**可学习的查表（lookup）操作**，并能说清它为什么等价于 one-hot + 矩阵乘法。
2. 自己用 `nn.Embedding` 生成 **token 嵌入**（编码「这是什么词」）和**位置嵌入**（编码「它在第几位」），并把二者**逐元素相加**得到最终的输入嵌入。
3. 说清**为什么自注意力需要位置信息**——注意力本身是无序的，位置信息必须从外部注入。

本讲是第 2 章「文本数据处理流水线」的收尾：它的产出 `input_embeddings`（形状 `batch × num_tokens × emb_dim`）正是下一单元第 3 章注意力机制的直接输入。

## 2. 前置知识

阅读本讲前，请确认你已经理解以下概念（它们在前几讲已建立）：

- **token 与 token ID**：文本先被分词器切成 token，再通过词表映射成整数 ID。GPT-2 用 BPE（tiktoken），词表大小为 50,257。
- **张量（tensor）**：PyTorch 里的多维数组，神经网络的全部计算都在张量上进行。
- **可学习参数 / 反向传播**：模型里那些会被梯度更新、随训练变好的数值。
- **滑动窗口与 DataLoader**：u2-l3 中 `create_dataloader_v1` 产出的 `inputs` 张量形状是 `(batch_size, max_length)`，里面装的是整数 token ID。

一个关键直觉：**整数 ID 对神经网络没有意义**。token ID 5 和 token ID 1 之间没有「5 是 1 的 5 倍」这种关系，它们只是词表里的行号。我们需要一种办法把每个整数 ID 映射成一个**稠密的连续向量**，让模型自己学会「哪些词在含义上相近」——这就是嵌入（embedding）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `ch02/01_main-chapter-code/ch02.ipynb` | 第 2 章正文 notebook。其中 **§2.7 Creating token embeddings** 和 **§2.8 Encoding word positions** 是本讲的核心，用最小示例从零演示两层嵌入如何构造与相加。 |
| `ch04/01_main-chapter-code/previous_chapters.py` | 第 4 章的「成品汇总器」，提供 `create_dataloader_v1`，产出 `(batch, max_length)` 的 token ID 张量——这正是嵌入层的输入接口。 |
| `ch04/01_main-chapter-code/gpt.py` | 第 4 章汇总脚本（自包含）。其中的 `GPTModel` 把 `tok_emb` / `pos_emb` 真正装配进完整模型，是本讲「两层嵌入相加」在真实 GPT 代码里的落点。 |

> 说明：本讲的「教学演示」来自 `ch02.ipynb`，而「真实模型里怎么用」来自 `ch04/gpt.py`。两者讲的是同一件事，前者更直观，后者更工程化。

## 4. 核心概念与源码讲解

### 4.1 token 嵌入层（token embedding）

#### 4.1.1 概念说明

token 嵌入层解决的问题是：**把离散的整数 token ID 变成连续向量**。

`torch.nn.Embedding(num_embeddings, embedding_dim)` 内部维护一个**可学习的权重矩阵** \(W\)，形状为 `(num_embeddings, embedding_dim)`：

- `num_embeddings` = 词表大小（有多少个可能的 token）。
- `embedding_dim` = 每个token 用多长的向量表示（GPT-2 small 是 768）。

当你把一个整数 ID `i` 喂给嵌入层，它做的全部事情就是**返回权重矩阵的第 `i` 行**。所以嵌入层本质上是一张**查表（lookup table）**：

\[
\text{embed}(i) = W[i,\ :]
\]

这件事可以等价地写成 one-hot 向量乘以权重矩阵：

\[
\text{embed}(i) = \text{onehot}(i)\, W
\]

其中 one-hot 向量只有第 `i` 位是 1、其余为 0。乘出来正好选中 \(W\) 的第 `i` 行。书里专门提示了这一点——`nn.Embedding` 只是「one-hot + 全连接层矩阵乘法」的**更高效实现**，所以它和普通神经网络层一样，权重可以通过反向传播被训练。

为什么要训练它？因为初始时这些向量是随机的，没有语义。训练过程中，模型会让**含义相近的词落在向量空间中相近的位置**（这是嵌入「学到语义」的来源）。

#### 4.1.2 核心流程

1. 创建一个 `nn.Embedding(vocab_size, output_dim)`，权重矩阵形状 `(vocab_size, output_dim)`，随机初始化、`requires_grad=True`（可训练）。
2. 给定一批 token ID，形状 `(batch, num_tokens)`。
3. 嵌入层对每个整数查表，输出形状 `(batch, num_tokens, output_dim)`——每个 token 变成一个 `output_dim` 维向量。
4. 这些向量随训练被梯度更新。

#### 4.1.3 源码精读

先看 `ch02.ipynb` §2.7 的**最小示例**：词表只有 6 个词，嵌入维度为 3，用 4 个 ID `[2, 3, 5, 1]` 演示查表。

构造一个极小的输入序列（[ch02/01_main-chapter-code/ch02.ipynb:L1573](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1573)）：

```python
input_ids = torch.tensor([2, 3, 5, 1])
```

创建嵌入层，权重是 6×3 的矩阵（[ch02/01_main-chapter-code/ch02.ipynb:L1591-L1595](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1591-L1595)）：

```python
vocab_size = 6
output_dim = 3
torch.manual_seed(123)
embedding_layer = torch.nn.Embedding(vocab_size, output_dim)
```

运行后 `embedding_layer.weight` 就是一个 `6 × 3` 的 `requires_grad=True` 矩阵。查单个 ID `3` 返回的是权重矩阵的**第 4 行**（下标从 0 算）：

```python
embedding_layer(torch.tensor([3]))   # == embedding_layer.weight[3]
```

这正是「查表」的直接证据——输入 `3`，输出第 3 行。

接着 notebook 把规模放大到 GPT-2 的真实量级：词表 50,257、嵌入维度 256（[ch02/01_main-chapter-code/ch02.ipynb:L1776-L1779](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1776-L1779)）：

```python
vocab_size = 50257
output_dim = 256
token_embedding_layer = torch.nn.Embedding(vocab_size, output_dim)
```

这里 `vocab_size=50257` 正是 GPT-2 BPE 词表的大小。把上一讲 DataLoader 产出的 `inputs`（形状 `(8, 4)`，即 batch=8、每条 4 个 token）送进去，得到（[ch02/01_main-chapter-code/ch02.ipynb:L1852-L1853](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1852-L1853)）：

```python
token_embeddings = token_embedding_layer(inputs)
print(token_embeddings.shape)   # torch.Size([8, 4, 256])
```

可见每个 token 被替换成了一个 256 维向量，整批变成了 `8 × 4 × 256`。

#### 4.1.4 代码实践

**目标**：亲手验证「嵌入层 = 查权重矩阵的某一行」。

**步骤**：

```python
import torch
torch.manual_seed(123)
emb = torch.nn.Embedding(6, 3)          # 6×3 权重矩阵
print(emb.weight.shape)                 # torch.Size([6, 3])

# 查 ID=3，应等于权重第 3 行
out = emb(torch.tensor([3]))
print(torch.equal(out, emb.weight[3]))  # True
```

**应观察到的现象**：
- `emb.weight.shape` 是 `(6, 3)`，且打印 `emb.weight` 时末尾标 `requires_grad=True`，说明它可被训练。
- 查 `ID=3` 的结果与 `emb.weight[3]` 完全相等，证明「嵌入 = 查表」。

**预期结果**：`torch.equal(...)` 输出 `True`。若想确认可学习性，可打印 `emb.weight.requires_grad`，应为 `True`。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能直接把整数 token ID 当作输入喂给神经网络？

> **答案**：整数 ID 是离散的、无序无尺度的（ID 5 并不比 ID 1「大 5 倍」的含义），而神经网络需要连续、可微的浮点输入才能计算梯度和反向传播；直接用大整数还会带来数值不稳定问题。嵌入层把整数映射成稠密向量，既满足「连续可微」，又允许模型自己学到词与词的语义关系。

**练习 2**：`torch.nn.Embedding(50257, 768)` 的权重矩阵形状是什么？有多少个可学习参数？

> **答案**：形状 `(50257, 768)`，参数量为 \(50257 \times 768 = 38{,}597{,}376\) 个（约 3860 万）。这正是 GPT-2 small 中 token 嵌入层的规模。

---

### 4.2 位置嵌入（positional embedding）

#### 4.2.1 概念说明

光有 token 嵌入还不够。**同一个 token ID 无论出现在序列的哪个位置，嵌入层都返回完全相同的向量**。但语言里顺序至关重要：「狗咬人」和「人咬狗」含义截然相反。

更要命的是，下一章要学的**自注意力机制本质上是无序的**——它对输入向量做加权求和，把输入当成一个「无序集合」来处理。如果不额外告诉模型「每个词在第几位」，模型根本无法区分词序。

解决办法是**位置嵌入**：再开一个嵌入层，专门为「位置」编码。GPT-2 用的是**绝对位置嵌入（absolute positional embedding）**——即位置 `0, 1, 2, …` 各对应一个可学习向量，和 token 嵌入一样是查表、一样会被训练。

> 术语区分：GPT-2 用的是**可学习的绝对位置嵌入**；原始 Transformer 论文用的是**固定的正弦位置编码**（不可学习）。二者目的相同——注入顺序信息，只是实现不同。本仓库随 GPT-2 走，用可学习绝对嵌入。

#### 4.2.2 核心流程

1. 创建第二个 `nn.Embedding(context_length, output_dim)`：
   - `context_length` = 模型能处理的最大序列长度（位置数）。
   - `output_dim` 必须**和 token 嵌入相同**（因为后面要相加）。
2. 用 `torch.arange(seq_len)` 生成位置下标 `[0, 1, …, seq_len-1]`。
3. 查表得到位置嵌入，形状 `(seq_len, output_dim)`。
4. 它独立于 batch——同一条序列里第 `p` 个位置永远用同一个位置向量。

注意一个直接推论：因为位置嵌入的权重矩阵只有 `context_length` 行，所以**模型能处理的最大长度被 `context_length` 钉死**。GPT-2 small 是 1024，超过就没有对应的位置向量可查了。

#### 4.2.3 源码精读

`ch02.ipynb` §2.8 明确写道「GPT-2 uses absolute position embeddings, so we just create another embedding layer」。位置嵌入层的行数是 `context_length`（[ch02/01_main-chapter-code/ch02.ipynb:L1874-L1875](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1874-L1875)）：

```python
context_length = max_length          # 这里 max_length = 4
pos_embedding_layer = torch.nn.Embedding(context_length, output_dim)
```

用 `torch.arange` 生成位置下标并查表（[ch02/01_main-chapter-code/ch02.ipynb:L1896-L1897](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1896-L1897)）：

```python
pos_embeddings = pos_embedding_layer(torch.arange(max_length))
print(pos_embeddings.shape)          # torch.Size([4, 256])
```

`torch.arange(4)` 得到 `[0, 1, 2, 3]`，对应序列的 4 个位置；查表后形状是 `(4, 256)`——**每个位置一个 256 维向量，且与 batch 无关**。

#### 4.2.4 代码实践

**目标**：观察「同一个位置在不同 batch 里用的是同一个向量」。

**步骤**：

```python
import torch
pos_layer = torch.nn.Embedding(context_length=4, embedding_dim=256)
p0 = pos_layer(torch.tensor([0]))                       # 位置 0
p0_again = pos_layer(torch.tensor([0]))                 # 再查一次位置 0
print(torch.equal(p0, p0_again))                        # True
print(torch.equal(pos_layer(torch.tensor([0])),
                  pos_layer(torch.tensor([1]))))        # False，不同位置不同向量
```

**应观察到的现象**：
- 重复查位置 0 得到完全相同的向量（位置嵌入与 batch 无关）。
- 位置 0 和位置 1 的向量不同。

**预期结果**：第一个 `torch.equal` 为 `True`，第二个为 `False`。

#### 4.2.5 小练习与答案

**练习 1**：为什么自注意力机制需要额外注入位置信息？

> **答案**：自注意力对输入做加权求和，本质上是位置无关的——它把输入视作无序集合，打乱顺序只要权重跟着打乱，输出也跟着打乱、相对关系不变。因此顺序信息无法从注意力内部产生，必须靠位置嵌入从外部注入，模型才能区分「谁在前、谁在后」。

**练习 2**：为什么 GPT-2 的绝对位置嵌入会限制模型能处理的最大上下文长度？

> **答案**：绝对位置嵌入是一个 `nn.Embedding(context_length, emb_dim)`，权重矩阵只有 `context_length` 行。序列长度超过它就没有对应的位置向量可查（下标越界）。GPT-2 small 的 `context_length=1024`，故最多处理 1024 个 token。

---

### 4.3 嵌入相加（token + position）

#### 4.3.1 概念说明

现在我们有两套向量：

- token 嵌入：编码「**这是什么词**」，形状 `(batch, num_tokens, output_dim)`。
- 位置嵌入：编码「**它在第几位**」，形状 `(num_tokens, output_dim)`。

最终送入 Transformer 的输入嵌入，就是二者**逐元素相加**：

\[
\text{input\_embed} = \text{token\_embed} + \text{pos\_embed}
\]

这里靠的是 PyTorch 的**广播（broadcasting）**：位置嵌入没有 batch 维，相加时会在 batch 维上被自动「复制」`batch_size` 份，与每条序列相加。这正是要求两种嵌入 `output_dim` 必须一致的原因——只有维度对齐才能逐元素相加。

相加之后，每个位置上的向量既携带了「我是哪个词」，又携带了「我在第几位」，两种信息叠加在一个向量里交给后续的注意力层去处理。

#### 4.3.2 核心流程

1. token 嵌入：`(batch, num_tokens, emb_dim)`。
2. 位置嵌入：`(num_tokens, emb_dim)`（由 `torch.arange(num_tokens)` 查表得到）。
3. 逐元素相加，广播补齐 batch 维 → `(batch, num_tokens, emb_dim)`。
4. （在真实模型里）紧接着过一个 Dropout，再送入 Transformer block。

#### 4.3.3 源码精读

教学版的两层相加在 `ch02.ipynb` 一行搞定（[ch02/01_main-chapter-code/ch02.ipynb:L1926-L1927](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb#L1926-L1927)）：

```python
input_embeddings = token_embeddings + pos_embeddings
print(input_embeddings.shape)         # torch.Size([8, 4, 256])
```

`token_embeddings` 是 `(8, 4, 256)`，`pos_embeddings` 是 `(4, 256)`，相加后广播成 `(8, 4, 256)`——这就是送入 LLM 的最终输入嵌入。

再看真实模型里的写法。`ch04/gpt.py` 的 `GPTModel` 在 `__init__` 里同时创建两层嵌入（[ch04/01_main-chapter-code/gpt.py:L188-L190](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L188-L190)）：

```python
self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"])
self.pos_emb = nn.Embedding(cfg["context_length"], cfg["emb_dim"])
self.drop_emb = nn.Dropout(cfg["drop_rate"])
```

注意 `tok_emb` 用 `vocab_size` 行、`pos_emb` 用 `context_length` 行，但二者第二个维度都是 `emb_dim`——这正是为了能相加。

前向传播里把两层相加，再过 Dropout（[ch04/01_main-chapter-code/gpt.py:L198-L203](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L198-L203)）：

```python
def forward(self, in_idx):
    batch_size, seq_len = in_idx.shape
    tok_embeds = self.tok_emb(in_idx)
    pos_embeds = self.pos_emb(torch.arange(seq_len, device=in_idx.device))
    x = tok_embeds + pos_embeds   # Shape [batch_size, num_tokens, emb_size]
    x = self.drop_emb(x)
    ...
```

可以看到：`seq_len` 直接从输入形状取，位置下标用 `torch.arange(seq_len)` 生成（并显式放到输入所在的 `device` 上），与教学版逻辑完全一致，只是多了 `device` 处理和 Dropout。配套的配置字典 `GPT_CONFIG_124M` 给出真实量级（[ch04/01_main-chapter-code/gpt.py:L237-L245](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L237-L245)）：

```python
GPT_CONFIG_124M = {
    "vocab_size": 50257,     # 词表大小 → tok_emb 行数
    "context_length": 1024,  # 上下文长度 → pos_emb 行数
    "emb_dim": 768,          # 嵌入维度 → 两层共用
    ...
}
```

即 GPT-2 small 实际是 `nn.Embedding(50257, 768)` 与 `nn.Embedding(1024, 768)` 相加。

#### 4.3.4 代码实践

**目标**：复现「两层相加 + 广播」，并检查形状与数值范围。

**步骤**：

```python
import torch
torch.manual_seed(123)

batch_size, num_tokens, emb_dim = 2, 4, 8
# 模拟一批 token ID（取值要在词表范围内，这里用小词表演示）
inputs = torch.randint(0, 100, (batch_size, num_tokens))

tok_layer = torch.nn.Embedding(100, emb_dim)
pos_layer = torch.nn.Embedding(num_tokens, emb_dim)

tok_emb = tok_layer(inputs)                          # (2, 4, 8)
pos_emb = pos_layer(torch.arange(num_tokens))        # (4, 8)

input_emb = tok_emb + pos_emb                        # 广播 → (2, 4, 8)
print(input_emb.shape)                               # torch.Size([2, 4, 8])
print(input_emb.min().item(), input_emb.max().item())
print(input_emb.mean().item(), input_emb.std().item())
```

**应观察到的现象**：
- `input_emb.shape` 为 `(2, 4, 8)`，广播成功。
- 因为 `tok_layer`、`pos_layer` 默认按标准正态初始化，相加后数值大致集中在 0 附近，标准差约在 1.4 左右（两个独立 \(N(0,1)\) 相加，方差约为 2）。

**预期结果**：形状 `torch.Size([2, 4, 8])`；精确的 min/max/mean/std 随机种子而变，**待本地验证**，但 mean 应接近 0、std 约 1.4。

#### 4.3.5 小练习与答案

**练习 1**：`token_embeddings` 形状 `(8, 4, 256)`，`pos_embeddings` 形状 `(4, 256)`，直接用 `+` 相加会发生什么？

> **答案**：PyTorch 广播机制会把 `pos_embeddings` 在缺失的 batch 维（第 0 维）上复制 8 份，相当于每条序列都加上同一组位置向量，结果形状 `(8, 4, 256)`。无需手动扩展 batch 维。

**练习 2**：如果把 `pos_emb` 的 `embedding_dim` 设成和 `tok_emb` 不同的值，相加会怎样？

> **答案**：会报错（形状不匹配、无法广播/逐元素相加）。因此 token 嵌入与位置嵌入的维度必须严格相同，GPT 里统一用 `emb_dim`（如 768）。

---

## 5. 综合实践

把三个最小模块串起来，完整走一遍「文本 → token ID → 输入嵌入」的最后一公里。这个任务直接对应本讲的核心实践要求。

**目标**：给定一句话，用 tiktoken（GPT-2 BPE）分词，分别生成 token 嵌入和位置嵌入并相加，最终打印出送入 Transformer 前的 `input_embeddings` 的形状与数值范围。

**操作步骤**（示例代码，需本机安装 `torch` 与 `tiktoken`）：

```python
import torch
import tiktoken

# 1) 文本 → token ID（沿用第 2 章的 GPT-2 BPE 分词器）
tokenizer = tiktoken.get_encoding("gpt2")
text = "Hello, I am"
token_ids = torch.tensor(tokenizer.encode(text)).unsqueeze(0)   # (1, num_tokens)
print("token_ids:", token_ids, "shape:", token_ids.shape)

# 2) 配置两层嵌入（用与 GPT-2 一致的词表大小，嵌入维度自选 256 演示）
vocab_size, emb_dim = 50257, 256
torch.manual_seed(123)
token_embedding_layer = torch.nn.Embedding(vocab_size, emb_dim)

context_length = 1024                       # GPT-2 的最大上下文长度
pos_embedding_layer = torch.nn.Embedding(context_length, emb_dim)

# 3) token 嵌入 + 位置嵌入（用 torch.arange 生成位置下标）
seq_len = token_ids.shape[1]
token_embeddings = token_embedding_layer(token_ids)            # (1, num_tokens, 256)
pos_embeddings     = pos_embedding_layer(torch.arange(seq_len))# (num_tokens, 256)
input_embeddings   = token_embeddings + pos_embeddings         # 广播 → (1, num_tokens, 256)

# 4) 打印形状与数值范围
print("input_embeddings.shape:", input_embeddings.shape)
print("min/max:", input_embeddings.min().item(), input_embeddings.max().item())
print("mean/std:", input_embeddings.mean().item(), input_embeddings.std().item())
```

**需要观察的现象**：
1. `token_ids` 是一串整数（例如 `[15496, 11, 40, 716]`），`unsqueeze(0)` 后形状 `(1, 4)`。
2. `token_embeddings` 形状 `(1, 4, 256)`，`pos_embeddings` 形状 `(4, 256)`。
3. 相加后 `input_embeddings` 形状 `(1, 4, 256)`，即「batch × 序列长度 × 嵌入维度」。

**预期结果**：最终形状一定是 `torch.Size([1, <实际token数>, 256])`。精确的数值范围因随机初始化而异，**待本地验证**，但均值应接近 0。把这段输出和张量形状记下来——它正是下一单元第 3 章自注意力层的输入。

> 进阶思考：试着把 `emb_dim` 从 256 改成 768，并把 `vocab_size` 保持 50257，对比 `token_embedding_layer.weight.numel()`——你会看到这层单独就有约 3860 万参数，是 GPT-2 small 里参数量的大头之一。

## 6. 本讲小结

- 神经网络只能吃连续浮点张量，整数 token ID 必须先经**嵌入层**变成向量。
- `nn.Embedding(vocab_size, emb_dim)` 本质是**可学习的查表**：权重矩阵 `(vocab_size, emb_dim)`，输入 ID `i` 返回第 `i` 行；它等价于 one-hot + 矩阵乘法，因而可被反向传播训练。
- 同一个 token 在任意位置都映射成**同一个向量**，而语言顺序至关重要，且自注意力本身无序——因此需要**位置嵌入**额外注入顺序信息。
- GPT-2 用**可学习的绝对位置嵌入** `nn.Embedding(context_length, emb_dim)`，行数 `context_length` 决定了模型能处理的最大长度（GPT-2 small 为 1024）。
- 最终输入嵌入 = **token 嵌入 + 位置嵌入**（逐元素相加，靠广播补齐 batch 维），所以两层 `emb_dim` 必须一致；真实模型里相加后还会过一层 Dropout。
- 至此第 2 章文本数据流水线完成：文本 → 分词 → token ID → 滑动窗口采样 → **输入嵌入**，产出形状 `batch × num_tokens × emb_dim`，正是下一章注意力的输入。

## 7. 下一步学习建议

本讲产出的 `input_embeddings`（`batch × num_tokens × emb_dim`）就是下一单元的直接入口：

- **下一讲 u3-l1 自注意力原理**：你将看到注意力层如何吃下这批嵌入向量，并用「加权求和」让每个 token 看到上下文中的其他 token。届时你会真切体会到「为什么必须注入位置信息」——注意力本身会把输入当无序集合处理。
- 建议先回头确认：你能脱口说出 `ch04/gpt.py` 里 `tok_emb`、`pos_emb` 两个 `nn.Embedding` 各自两个参数的含义，以及为什么它们的第二个维度必须相等。
- 进阶预告：本讲的位置嵌入是「绝对、可学习」的；到 u10-l1「GPT 转 Llama」你会学到 **RoPE（旋转位置编码）**，它用旋转把相对位置编码进查询/键向量，是现代 LLM 的主流做法。先掌握本讲的绝对嵌入，才能理解 RoPE 要解决什么问题。
