# 讲义标题：GPT 模型组装与配置

## 1. 本讲目标

在前两讲里，我们已经攒齐了组装 GPT 的所有「零件」：注意力（第 3 章 `MultiHeadAttention`）、`LayerNorm`、`GELU`、`FeedForward` 以及把它们串起来的 `TransformerBlock`（含 pre-LayerNorm 与残差连接）。本讲要做的，就是把这些零件装进一个完整的 **`GPTModel`**。

学完本讲，你应当能够：

- 读懂 `GPTModel` 从 token ID 到 logits 的**完整前向数据流**，并说出每一层张量形状的变化。
- 解释 `GPT_CONFIG_124M` 这张「配方卡」里 7 个超参各自的含义与取值理由。
- 理解 `tok_emb`（token 嵌入）、`pos_emb`（位置嵌入）、`trf_blocks`（12 个 Transformer 块）、`final_norm`、`out_head`（输出头）各自的职责。
- 解释为什么模型打印出来是 **163M** 而非 124M，以及什么是 **weight tying（权重共享）**，它在加载 OpenAI 预训练权重时为何重要。

## 2. 前置知识

- **配置字典（config dict）**：用一个 Python 字典集中存放模型超参（如词表大小、层数、头数），整个模型都从这个字典读取参数，方便复用与切换规格。
- **`nn.Sequential`**：PyTorch 的「串行容器」，按顺序把多个子层依次执行，等价于把它们用管道连起来。
- **`nn.Embedding` / `nn.Linear`**：前者是「查表」（输入整数索引，返回一行权重），后者是「线性变换」 \(y = xW^{\top} + b\)。本讲会再次用到（详见 u2-l4）。
- **logits**：模型输出层未经 softmax 归一化的原始分数，每个词对应一个分数，分数越高模型越倾向预测该词。
- **weight tying（权重共享）**：让输出层的权重矩阵和 token 嵌入矩阵**共用同一份参数**，从而既省参数又把「编码」和「解码」绑定在一起。
- **残差连接 / pre-LayerNorm / TransformerBlock**：这些是上一讲（u4-l2）的内容，本讲直接把 `TransformerBlock` 当作一个「黑盒子」堆叠使用。

## 3. 本讲源码地图

本讲聚焦第 4 章，涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| `ch04/01_main-chapter-code/gpt.py` | 自包含的汇总脚本，把第 2~4 章所有相关代码聚到单文件里，含 `GPTModel`、`GPT_CONFIG_124M`、`main()`，可直接 `python gpt.py` 运行。 |
| `ch04/01_main-chapter-code/ch04.ipynb` | 正文 notebook，逐节演进讲解；其中 4.1 节定义配置、4.6 节组装 `GPTModel`，并讨论了参数量与 weight tying。 |

补充：`TransformerBlock`（u4-l2 已讲）和 `MultiHeadAttention`、`LayerNorm`、`FeedForward` 也都定义在同一个 `gpt.py` 里，本讲会把它们当作已知零件引用。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：配置卡 → 整体数据流 → 嵌入层与 Transformer 块堆叠 → 输出头与权重共享。

### 4.1 GPT_CONFIG_124M：模型的「配方卡」

#### 4.1.1 概念说明

盖一栋楼需要一张图纸，搭一个 GPT 同样需要一张「配方卡」。`GPT_CONFIG_124M` 就是这张卡：它用一个字典把**最小号 GPT-2（124M 参数）**的全部超参写在一起。整个模型、每一层都从这张卡里读参数。

这样做有两个好处：

1. **集中管理**：要换成更大的 GPT-2（medium / large / XL），只改字典里的几个数字即可，模型代码一行都不用动。
2. **规格可追溯**：「124M」这个名字本身就来自这张卡里的 `emb_dim`、`n_layers`、`n_heads` 等组合，方便核对。

#### 4.1.2 核心流程

`GPT_CONFIG_124M` 在脚本里只在 `main()` 内部定义一次，随后被传给 `GPTModel(cfg)`，模型再逐层把 `cfg` 往下传给 `TransformerBlock`、`MultiHeadAttention`、`FeedForward` 等子模块：

```text
GPT_CONFIG_124M (dict)
      │  作为 cfg 传入
      ▼
GPTModel(cfg) ──► tok_emb, pos_emb, out_head 读 cfg["vocab_size"]/["emb_dim"]/["context_length"]
      │           trf_blocks 读 cfg["n_layers"]
      ▼
TransformerBlock(cfg) ──► MultiHeadAttention 读 cfg["n_heads"]/["qkv_bias"]
                          FeedForward 读 cfg["emb_dim"]
```

#### 4.1.3 源码精读

字典本身定义在 `gpt.py` 的 `main()` 里，每个字段都带注释说明用途：

```python
GPT_CONFIG_124M = {
    "vocab_size": 50257,     # 词表大小（BPE 分词器）
    "context_length": 1024,  # 最大上下文长度
    "emb_dim": 768,          # 嵌入维度
    "n_heads": 12,           # 注意力头数
    "n_layers": 12,          # Transformer 块层数
    "drop_rate": 0.1,        # dropout 比例
    "qkv_bias": False        # Q/K/V 是否带偏置
}
```

详见 [ch04/01_main-chapter-code/gpt.py:237-245](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L237-L245)：这段是 124M 模型的配置定义，每行注释说明了字段含义。

逐字段含义（与 notebook 4.1 节的解释一致，见 [ch04/01_main-chapter-code/ch04.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/ch04.ipynb#L125) 配置单元及其后的字段说明）：

| 字段 | 值 | 含义 | 出现位置 / 备注 |
| --- | --- | --- | --- |
| `vocab_size` | 50257 | 词表大小 | 由第 2 章 BPE 分词器决定；`tok_emb` 和 `out_head` 的行数都等于它 |
| `context_length` | 1024 | 模型一次最多能读多少个 token | `pos_emb` 的行数等于它；超出要裁剪 |
| `emb_dim` | 768 | 每个 token 的嵌入向量维度 | 整个模型内部的「主干宽度」 |
| `n_heads` | 12 | 多头注意力的头数 | 768/12=64，每头 64 维（须整除） |
| `n_layers` | 12 | Transformer 块的层数 | 决定模型「有多深」 |
| `drop_rate` | 0.1 | dropout 强度 | 训练时随机置零 10% 单元，防过拟合；推理时自动关闭 |
| `qkv_bias` | False | Q/K/V 线性层是否带偏置 | 现代 LLM 通常关闭；第 5 章加载 OpenAI 权重时会重新启用 |

> 小贴士：`qkv_bias` 这里设为 `False`，但第 5 章加载 OpenAI 预训练权重时必须改回 `True`，否则权重对不上。这是一个典型的「配置随任务变化」的例子。

#### 4.1.4 代码实践

**实践目标**：直观感受配置卡如何控制模型规模。

1. 操作步骤：复制 `GPT_CONFIG_124M`，分别把 `emb_dim` 改成 1024、把 `n_layers` 改成 24（即 GPT-2 medium），构建模型并打印参数量。
2. 需要观察的现象：参数量随 `emb_dim` 平方级增长（因为 `Linear` 参数量是 `in×out`），随 `n_layers` 线性增长。
3. 预期结果：medium 配置下参数量会显著超过 124M（粗略在 ~350M 量级）。
4. 由于依赖具体环境，精确数字「待本地验证」，但增长趋势是确定的。

（关于如何统计参数量，见 4.4 节的实践。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `context_length` 从 1024 调到 2048，模型能处理的输入变长了吗？参数量会明显增加吗？

> **参考答案**：能处理的输入变长了一倍（`pos_emb` 行数翻倍）。但参数量增加得很少——只多了 1024×768 ≈ 78 万个位置嵌入参数，相比 163M 几乎可以忽略。

**练习 2**：为什么 `n_heads=12`、`emb_dim=768` 是合法配置，而 `n_heads=11`、`emb_dim=768` 会报错？

> **参考答案**：多头注意力要求 `emb_dim` 能被 `n_heads` 整除，768÷11 除不尽；768÷12=64 整除，所以每头 64 维。代码里的断言 `assert d_out % num_heads == 0` 会拦截非法配置。

---

### 4.2 GPTModel 整体数据流与组装

#### 4.2.1 概念说明

`GPTModel` 是一个标准的 `nn.Module`，它把全部零件按顺序拼成一条流水线：**输入 token ID → 嵌入 → 12 个 Transformer 块 → 最终归一化 → 输出头 → logits**。这条流水线的入口接收一个 `(batch, num_tokens)` 的整数张量，出口吐出一个 `(batch, num_tokens, vocab_size)` 的浮点张量。

一个关键直觉：模型对序列里**每个位置**都独立输出一份 logits，因此输出形状的第 0、1 维与输入一一对应，只是最后一维从「token 数」变成了「词表大小」。

#### 4.2.2 核心流程

`forward` 的前向数据流（形状记号：`b`=batch，`T`=num_tokens，`C`=emb_dim=768，`V`=vocab_size=50257）：

```text
输入 idx : (b, T)                      # 整数 token ID
   │ tok_emb(idx)                       # 查表 → (b, T, C)
   │ pos_emb(arange(T))                 # 查表 → (T, C)，广播成 (b, T, C)
   │ 相加 + drop_emb                     # (b, T, C)
   ▼
trf_blocks (×12)                        # 逐块处理，形状保持 (b, T, C)
   ▼
final_norm (LayerNorm)                  # (b, T, C)
   ▼
out_head (Linear C→V, bias=False)       # (b, T, V)  ← 这就是 logits
```

整条流水线的形状变化可以浓缩成：

\[
(b,\, T)_{\text{整数}} \;\longrightarrow\; (b,\, T,\, C) \;\longrightarrow\; (b,\, T,\, C) \;\longrightarrow\; (b,\, T,\, V)_{\text{logits}}
\]

中间所有 Transformer 块都**保持** `(b, T, C)` 不变（这是 u4-l2 强调的「形状保持」），只有首尾各做一次维度变换。

#### 4.2.3 源码精读

`GPTModel` 的 `__init__` 负责把所有零件实例化并作为子模块挂到 `self` 上（PyTorch 靠此自动注册参数）：

详见 [ch04/01_main-chapter-code/gpt.py:186-196](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L186-L196)：这段实例化了 token 嵌入、位置嵌入、dropout、12 个 TransformerBlock 的 `Sequential`、最终 LayerNorm 和输出头。

```python
def __init__(self, cfg):
    super().__init__()
    self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"])
    self.pos_emb = nn.Embedding(cfg["context_length"], cfg["emb_dim"])
    self.drop_emb = nn.Dropout(cfg["drop_rate"])

    self.trf_blocks = nn.Sequential(
        *[TransformerBlock(cfg) for _ in range(cfg["n_layers"])])

    self.final_norm = LayerNorm(cfg["emb_dim"])
    self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=False)
```

其中 `*[TransformerBlock(cfg) for _ in range(cfg["n_layers"])]` 是「列表展开」写法：用列表推导生成 12 个独立的块，再用 `*` 把它们解包进 `nn.Sequential`。

`forward` 把数据流一步步写出来：

详见 [ch04/01_main-chapter-code/gpt.py:198-207](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L198-L207)：这段就是上文数据流的逐行实现，注意 `pos_embeds` 用 `torch.arange(seq_len)` 生成 0,1,2,… 的位置索引。

```python
def forward(self, in_idx):
    batch_size, seq_len = in_idx.shape
    tok_embeds = self.tok_emb(in_idx)
    pos_embeds = self.pos_emb(torch.arange(seq_len, device=in_idx.device))
    x = tok_embeds + pos_embeds  # 广播相加
    x = self.drop_emb(x)
    x = self.trf_blocks(x)
    x = self.final_norm(x)
    logits = self.out_head(x)
    return logits
```

> 两个细节：① `torch.arange(seq_len, device=in_idx.device)` 显式把位置索引放到和输入**同一设备**上，避免 GPU 训练时设备不一致；② `tok_embeds + pos_embeds` 靠广播把 `(T, C)` 的位置嵌入扩展到 `(b, T, C)`。

#### 4.2.4 代码实践

**实践目标**：构建模型，喂一个 batch，验证输出形状符合预期。

操作步骤（示例代码，可直接在 `ch04/01_main-chapter-code/` 目录运行）：

```python
import torch
import tiktoken
from gpt import GPTModel, GPT_CONFIG_124M   # 从 gpt.py 导入（见 4.1.3 的配置位置）

torch.manual_seed(123)
model = GPTModel(GPT_CONFIG_124M)

tokenizer = tiktoken.get_encoding("gpt2")
batch = torch.stack([
    torch.tensor(tokenizer.encode("Every effort moves you")),
    torch.tensor(tokenizer.encode("Every day holds a")),
])

out = model(batch)
print("输入形状:", batch.shape)   # 预期 torch.Size([2, 4])
print("输出形状:", out.shape)     # 预期 torch.Size([2, 4, 50257])
```

需要观察的现象与预期结果：

- 输入是 2 条、每条 4 个 token 的整数张量 `(2, 4)`。
- 输出是 `(2, 4, 50257)`——每个 token 位置都给出一份覆盖整个词表的 logits。
- 输出是随机数（未训练），数值本身没有意义，只验证「流水线跑通 + 形状正确」。

> 说明：`from gpt import GPT_CONFIG_124M` 时需注意 `gpt.py` 里 `GPT_CONFIG_124M` 定义在 `main()` 内部，直接 import 拿不到。实际跑本例时请在脚本里**自己重新定义一份配置字典**（照抄 4.1.3 的内容即可），这一点「待本地确认」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `batch` 换成 `(2, 10)` 的输入，输出形状会是什么？

> **参考答案**：`(2, 10, 50257)`。模型对每个位置都输出一份 logits，所以中间维随输入 token 数变化。

**练习 2**：为什么 `forward` 里要先算 `tok_embeds` 再加 `pos_embeds`，而不是反过来或者用乘法？

> **参考答案**：相加是 GPT-2 的设计（token 嵌入与位置嵌入维度相同、直接叠加注入位置信息）；乘法没有同样的几何含义，且会改变向量尺度。顺序在加法下不影响结果，但代码按「内容 + 位置」的语义先 token 后 pos，便于阅读。

---

### 4.3 嵌入层与 TransformerBlock 堆叠

#### 4.3.1 概念说明

`GPTModel` 中部由三大块组成：

- **`tok_emb`（token 嵌入）**：把整数 token ID 映射成 768 维向量（详见 u2-l4）。
- **`pos_emb`（位置嵌入）**：给每个位置（0~1023）一个可学习的 768 维向量，注入顺序信息。
- **`trf_blocks`（12 个 TransformerBlock）**：模型的「大脑」，每一块做一次「自注意力 + 前馈」，形状保持 `(b, T, 768)` 不变。

这部分的关键认知是：**嵌入层负责「进入」模型主干，Transformer 块负责在主干里反复加工信息**。所有主干计算都在 768 维空间里进行，这就是 `emb_dim` 的「主干宽度」含义。

#### 4.3.2 核心流程

从 ID 到「主干张量」的加工链：

```text
idx[b, T] ──tok_emb──► (b, T, 768)
                       + pos_emb[0..T-1]   # 注入位置
                       drop_emb            # 训练时随机丢弃，推理时透传
                       ──────────────  进入主干 (b, T, 768)
trf_blocks:  block_1 → block_2 → … → block_12   # 每块都 (b,T,768) 进、(b,T,768) 出
```

每个 `TransformerBlock` 内部（u4-l2 已详述）：先 pre-LayerNorm，再多头注意力 + 残差，再 pre-LayerNorm，再 FeedForward + 残差。12 块串联，相当于把同样的加工重复 12 次，每次都让信息更深地融合。

#### 4.3.3 源码精读

嵌入与堆叠在 `__init__` 中定义（见 [ch04/01_main-chapter-code/gpt.py:188-193](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L188-L193)）：

```python
self.tok_emb = nn.Embedding(cfg["vocab_size"], cfg["emb_dim"])      # (50257, 768)
self.pos_emb = nn.Embedding(cfg["context_length"], cfg["emb_dim"])  # (1024, 768)
self.drop_emb = nn.Dropout(cfg["drop_rate"])

self.trf_blocks = nn.Sequential(
    *[TransformerBlock(cfg) for _ in range(cfg["n_layers"])])
```

`TransformerBlock` 的定义在 [ch04/01_main-chapter-code/gpt.py:152-182](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L152-L182)：它把 `MultiHeadAttention`、`FeedForward`、两个 `LayerNorm` 和 `drop_shortcut` 组装成「注意力残差子路 + 前馈残差子路」。

forward 中相加并进入主干：

详见 [ch04/01_main-chapter-code/gpt.py:200-204](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L200-L204)：`tok_embeds + pos_embeds` 完成嵌入相加，`drop_emb` 与 `trf_blocks` 依次处理进入主干。

```python
tok_embeds = self.tok_emb(in_idx)
pos_embeds = self.pos_emb(torch.arange(seq_len, device=in_idx.device))
x = tok_embeds + pos_embeds  # (b, T, 768)
x = self.drop_emb(x)
x = self.trf_blocks(x)       # 形状仍为 (b, T, 768)
```

> 维度核对：`tok_emb` 权重 `(50257, 768)`，`pos_emb` 权重 `(1024, 768)`。两者 `emb_dim=768` 必须一致才能相加；`pos_emb` 的行数 `1024` 决定了模型最大上下文长度。

#### 4.3.4 代码实践

**实践目标**：亲手核对嵌入层与主干堆叠的形状与层数。

操作步骤（示例代码）：

```python
import torch
# 假设已构建 model = GPTModel(cfg)
print("tok_emb 权重形状:", model.tok_emb.weight.shape)   # (50257, 768)
print("pos_emb 权重形状:", model.pos_emb.weight.shape)   # (1024, 768)
print("TransformerBlock 个数:", len(model.trf_blocks))   # 12

# 单块前向形状保持
x = torch.rand(2, 4, 768)
y = model.trf_blocks[0](x)
print("单块输入/输出:", x.shape, y.shape)                 # 都是 (2, 4, 768)
```

需要观察的现象与预期结果：

- `tok_emb.weight.shape == torch.Size([50257, 768])`。
- `pos_emb.weight.shape == torch.Size([1024, 768])`。
- `len(model.trf_blocks) == 12`。
- 单个块输入输出形状一致，印证「形状保持」。

#### 4.3.5 小练习与答案

**练习 1**：`pos_emb` 为什么行数是 1024 而不是 `vocab_size` 50257？

> **参考答案**：位置嵌入索引的是「位置 0,1,2,…」而不是「词」，行数等于 `context_length`（模型能处理的最大 token 数），与词表大小无关。

**练习 2**：如果把 `n_layers` 从 12 改成 6，模型是变深了还是变浅了？参数量大概变化多少？

> **参考答案**：变浅了。参数量大约减半（少了 6 个块），但 124M 这个名字是和「12 层」绑定的，改了就不再是标准 small 配置。

---

### 4.4 out_head 输出头与权重共享（weight tying）

#### 4.4.1 概念说明

`out_head` 是模型最后一层，把 768 维主干向量**映射回词表空间**（50257 维），产生每个词的 logits——本质就是「在 5 万个候选词里打分」。它是一个**不带偏置**的线性层 `nn.Linear(768, 50257, bias=False)`。

有意思的是：`out_head` 的权重矩阵 `(50257, 768)` 和 `tok_emb` 的权重矩阵 `(50257, 768)` **形状完全一样**。于是产生一个问题——能不能让它们共用同一份参数？这就是 **weight tying（权重共享）**：输入端用嵌入矩阵把「词」编码成向量，输出端用**同一矩阵的转置**把向量「解码」回词表分数。

原始 GPT-2 论文正是这么做的。这既省下约 3860 万参数（从 163M 降到 124M），也在数学上更自洽：编码和解码是对称的逆过程。但本项目为了「更容易训练」，**没有实现** weight tying——所以直接 `sum(p.numel())` 数出来是 163M 而非 124M。第 5 章加载 OpenAI 预训练权重时会把这层共享关系对齐。

#### 4.4.2 核心流程

参数量的两种口径：

\[
\text{直接统计} = \sum_{p} \text{numel}(p) = 163{,}009{,}536 \approx 163\text{M}
\]

\[
\text{考虑 weight tying} = \text{直接统计} - \underbrace{|\text{out\_head}|}_{38{,}597{,}376} = 124{,}412{,}160 \approx 124\text{M}
\]

124M 参数的来源拆解（本讲已核对，与 notebook 输出一致）：

| 部件 | 参数量 | 占比 |
| --- | --- | --- |
| 12 × `TransformerBlock` | 85,026,816 | ~52% |
| `out_head` | 38,597,376 | ~23.7% |
| `tok_emb` | 38,597,376 | ~23.7% |
| `pos_emb` | 786,432 | ~0.5% |
| `final_norm` | 1,536 | ~0% |
| **合计** | **163,009,536** | 100% |

结论：参数量主要来自**两大块**——12 个 Transformer 块（约 85M）和两个超大嵌入/输出矩阵 `tok_emb` + `out_head`（合计约 77M）。weight tying 正是把其中 `out_head` 那 38.6M「合并」掉，从而回到 124M。

#### 4.4.3 源码精读

`out_head` 定义在 [ch04/01_main-chapter-code/gpt.py:195-196](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/gpt.py#L195-L196)：`final_norm` 做最后归一化，`out_head` 是不带偏置的线性层，把 768 维映射到 50257 维词表。

```python
self.final_norm = LayerNorm(cfg["emb_dim"])
self.out_head = nn.Linear(cfg["emb_dim"], cfg["vocab_size"], bias=False)
```

参数量统计与 weight tying 讨论来自 notebook 4.6 节：

- 总参数量 [ch04/01_main-chapter-code/ch04.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/ch04.ipynb#L1217) 的 `total_params = sum(p.numel() for p in model.parameters())`，输出 **163,009,536**。
- weight tying 解释见 [ch04/01_main-chapter-code/ch04.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/ch04.ipynb#L1226)：说明原始 GPT-2 让 `out_head` 复用 `tok_emb`。
- 扣除输出层后 [ch04/01_main-chapter-code/ch04.ipynb](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch04/01_main-chapter-code/ch04.ipynb#L1278) 得到 **124,412,160**。

> 关于 `bias=False`：注意 `out_head` 显式禁用偏置，这样它就是纯粹的 \(xW^{\top}\)，与 `tok_emb`（也是纯查表、无偏置）在结构上对称，为 weight tying 提供了前提。

#### 4.4.4 代码实践

**实践目标**：亲手数出 124M，验证「主要参数来源」。

操作步骤（示例代码）：

```python
import torch
# 假设已构建 model = GPTModel(GPT_CONFIG_124M)

# 1) 直接统计总参数量
total_params = sum(p.numel() for p in model.parameters())
print(f"总参数量: {total_params:,}")   # 预期 163,009,536

# 2) 扣除输出层，得到「考虑 weight tying」的口径
tied = total_params - sum(p.numel() for p in model.out_head.parameters())
print(f"weight tying 口径: {tied:,}")  # 预期 124,412,160 ≈ 124M

# 3) 模型占显存（float32，每个参数 4 字节）
print(f"模型大小: {total_params * 4 / (1024*1024):.2f} MB")  # 预期 ~621.83 MB

# 4) 逐部件拆解
for name, p in model.named_parameters():
    print(f"{name:40s} {tuple(p.shape)}  {p.numel():>12,}")
```

需要观察的现象与预期结果：

- 总参数量 = **163,009,536**（163M）。
- weight tying 口径 = **124,412,160**（≈124M，与「124M 模型」的名字吻合）。
- 模型权重大小 ≈ **621.83 MB**。
- 拆解表中，`tok_emb`、`out_head` 各约 3860 万，12 个块的参数占大头，`pos_emb`/`final_norm` 很小。

> 如果环境内存/算力受限，可只构建模型并做前向，不必训练；以上数字由模型结构决定，与是否训练无关。

#### 4.4.5 小练习与答案

**练习 1**：为什么直接 `sum(p.numel())` 是 163M，而模型却叫「124M」？

> **参考答案**：因为本项目没实现 weight tying，`out_head`（约 3860 万）是独立参数，被统计进去了；原始 GPT-2 让 `out_head` 共享 `tok_emb`，扣掉这部分后正好是 124M。

**练习 2**：weight tying 在数学上意味着 `out_head` 的权重等于 `tok_emb` 的权重。那么输出 logits 实际上等价于哪个运算？

> **参考答案**：等价于用 `tok_emb.weight`（即嵌入矩阵）去乘主干向量，即 logits = \(x \cdot E^{\top}\)，其中 \(E\) 是嵌入矩阵。这正是「用同一张表既编码又解码」。

**练习 3**：把 `bias=False` 改成 `bias=True`，参数量会增加多少？

> **参考答案**：增加 50257 个（输出层偏置的维度等于词表大小）。相比 163M 微乎其微，但会让 `out_head` 不再与 `tok_emb` 结构对称，weight tying 也就不那么自然。

## 5. 综合实践

**任务**：用 `GPT_CONFIG_124M` 从零搭建一个 GPTModel，完整跑通「构建 → 前向 → 数参数 → 拆来源」，并把 124M 这个数字亲手验证出来。

操作步骤：

1. 进入 `ch04/01_main-chapter-code/` 目录，准备好一份 `GPT_CONFIG_124M` 字典（照抄 4.1.3）。
2. `torch.manual_seed(123); model = GPTModel(GPT_CONFIG_124M); model.eval()`。
3. 用 tiktoken 把 `"Every effort moves you"` 编码成 tensor（加 batch 维），跑 `model(...)`，确认输出形状为 `(1, 4, 50257)`。
4. 用 4.4.4 的代码统计总参数量（应得 163,009,536），再算 weight tying 口径（应得 124,412,160）。
5. 逐部件拆解参数量，找出「参数量主要来源」（预期是 12 个 Transformer 块 + `tok_emb` + `out_head` 三大块）。
6. 进阶：把配置改成 GPT-2 medium（`emb_dim=1024, n_layers=24, n_heads=16`），重复统计，记录参数量增长。

需要观察的现象与预期结果：

- 前向输出形状正确、未训练 logits 是随机数。
- 124M 口径数字可复现。
- 拆解表与 4.4.2 的表格一致。
- medium 配置参数量明显增大（量级与精确值「待本地验证」）。

> 这是把「配置 → 组装 → 数据流 → 参数预算」串起来的练习，做完后你应当能在不看代码的情况下画出 `GPTModel` 的完整结构图。

## 6. 本讲小结

- `GPT_CONFIG_124M` 是模型的「配方卡」，7 个超参（词表、上下文、嵌入维、头数、层数、dropout、qkv 偏置）定义了 124M GPT-2 的全部规格。
- `GPTModel` 的数据流是：token ID `(b,T)` → 嵌入相加 `(b,T,768)` → 12 个 Transformer 块（形状不变）→ `final_norm` → `out_head` → logits `(b,T,50257)`。
- `tok_emb` 与 `pos_emb` 负责「进入主干」，`trf_blocks`（12 层）在 768 维主干里反复加工信息，`out_head` 负责「回到词表打分」。
- 直接统计得到 **163M** 参数，扣除独立 `out_head`（weight tying 口径）后才是 **124M**——这正是「124M」名称的由来。
- 参数量主要来自三大块：12 个 Transformer 块（~85M）、`tok_emb`（~38.6M）、`out_head`（~38.6M）；位置嵌入与归一化几乎可忽略。

## 7. 下一步学习建议

- 模型已搭好但还没法生成有意义的文字。下一讲 **u4-l4：简单自回归文本生成** 会讲解 `generate_text_simple` 如何用贪心解码，把 logits 一步步变成新 token。
- 想理解 weight tying 为何在第 5 章才「真正落地」，可预习 **u5-l4：权重保存/加载与加载 OpenAI GPT-2 权重**，那里会把 OpenAI 的 `out_head` 与 `tok_emb` 共享关系对齐。
- 想深入每个 Transformer 块的内部，回顾 **u4-l2：TransformerBlock 与残差连接**；想换更大模型，参考 notebook 4.6 节末给出的 GPT-2 medium/large/XL 配置尝试改写配置卡。
