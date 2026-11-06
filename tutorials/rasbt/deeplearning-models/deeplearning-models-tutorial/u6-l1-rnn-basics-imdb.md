# 第一个 RNN：IMDb 情感分类

## 1. 本讲目标

本讲是序列建模单元的第一讲。读完本讲，你应该能够：

1. 说清「文本 → 词元 → 词表 → 整数索引 → Embedding 向量」这条数值化流水线，理解 `<unk>` 与 `<pad>` 两个特殊词元的来历。
2. 看懂 `nn.RNN` 的输入输出形状约定（为什么是 `[句长, 批大小, 特征维]` 而不是批优先），以及 `output` 与 `hidden` 各自的含义。
3. 理解 many-to-one 分类：为什么只取最后一个时间步的隐状态就能代表整句话的情感，以及 `hidden.squeeze(0)` 在做什么。
4. 诚实面对本 notebook 的实验结果——简单 RNN 在长影评上卡在 50% 附近——并说清它失败的两个结构性原因（padding 稀释与梯度消失），这正是下一讲 LSTM 与 packed sequences 的出场理由。

## 2. 前置知识

本讲用到的概念大多在前几讲出现过，这里快速回顾，新概念重点展开。

**从图像转向文本。** 此前我们处理的输入是固定大小的图像（如 28×28=784 个像素），展平成一个向量就能喂给全连接网络。文本不同：句子的长度是变化的，而且词与词之间有顺序——「not good」和「good」词相同、情感相反。这要求新的输入表示和新的网络结构。

**词元（token）与词表（vocabulary）。** 把句子切成一个个最小单元（英语里通常按空格和标点切词）的过程叫分词（tokenization），每个单元叫一个 token。把训练集中出现过的 token 收集起来，按出现频率排序，取前 N 个构成词表，每个词表成员分配一个整数编号。两个特殊编号必须预留：

- `<unk>`（unknown）：测试时遇到训练集没见过的词，一律映射到它；
- `<pad>`（padding）：同一个批里的句子长短不一，神经网络要求批内张量形状一致，短句末尾补 `<pad>` 对齐到批内最长句。

**Embedding（词嵌入）。** one-hot 向量维度等于词表大小（本例 20002 维），且任意两个词的正交性使「语义相似」无法表达。Embedding 层本质上是一张可学习的查表：`nn.Embedding(词表大小, 嵌入维度)`，内部保存一个 `[20002, 64]` 的权重矩阵，输入整数编号、输出对应行的 64 维向量。它等价于「one-hot 乘权重矩阵」，但用查表实现，免去构造稀疏向量。这个矩阵和 `nn.Linear` 的权重一样，靠反向传播学习。

**RNN（循环神经网络）的直觉。** 全连接网络对 784 个像素一视同仁地一次性处理；RNN 则像人读句子一样从左到右逐词扫描，维护一个不断更新的「记忆」——隐状态（hidden state）\( h_t \)。每读一个词 \( x_t \)，就用当前词和上一刻的记忆 \( h_{t-1} \) 更新记忆：

\[ h_t = \tanh(W_{xh}\, x_t + W_{hh}\, h_{t-1} + b) \]

整条句子读完后，\( h_L \)（最后一步的隐状态）理论上浓缩了全句信息，把它接一个普通的 `nn.Linear` 就能做分类。整条流水线是：

```
句子 → 分词 → 查词表得整数序列 → Embedding 查表得向量序列
     → RNN 逐步扫描 → 取最终隐状态 → Linear → sigmoid → 正/负情感
```

**复用本手册已建立的两件工具。** 其一是 u2-l2 确立的五步训练范式 `forward → loss → zero_grad → backward → step`，本讲的训练循环一字不差地沿用；其二是 u2-l3 的 `nn.Module` 写法（`__init__` 里登记层、`forward` 里描述计算），本讲的 RNN 类仍是同一模板。另外 u4-l1 讲过 Dataset/Sampler/DataLoader 的分工——本讲的数据管线由旧版 torchtext 的 `BucketIterator` 承担，角色与 DataLoader 相同（产出 `(特征, 标签)` 的批），值得对照理解。

**一个必须先说的环境事实。** 本 notebook 使用的是 torchtext 0.x 时代的旧 API（`data.Field`、`datasets.IMDB.splits`），作者保存的运行环境是 CPython 3.7.1 + torch 1.0.1.post2（见 notebook 顶部 watermark 输出）。新版 torchtext 已移除 `data.Field`，所以**直接重跑本 notebook 需要搭建旧版环境**；跑不起来也没关系，本讲的实践任务同时提供「源码阅读型」替代路线。

## 3. 本讲源码地图

本讲只围绕一个 notebook 展开，它位于 RNN 目录下，是整个序列建模单元的起点：

| 文件 | 作用 |
|---|---|
| [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb) | 主角：用单层 `nn.RNN` 做 IMDb 影评二分类，含词表构建、BucketIterator、模型定义、训练循环与单句预测 |

同目录下与本讲形成对照、将在后续讲义精读的文件（本讲只引用不展开）：

| 文件 | 与本讲的关系 |
|---|---|
| `pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb` | 同款 RNN 改用 packed sequences，作者注明比本讲的 padding 版快约 4 倍（u6-l2 精读） |
| `pytorch_ipynb/rnn/rnn_lstm_packed_imdb.ipynb`、`rnn_gru_packed_imdb.ipynb` | 把 `nn.RNN` 换成 LSTM/GRU 解决梯度问题（u6-l2） |

notebook 内部按仓库通用的五段式组织（u1-l2 介绍过）：Imports → General Settings → Dataset → Model → Training，最后附一个单句预测函数。下文的源码精读按这个顺序推进。

## 4. 核心概念与源码讲解

### 4.1 词表与 Embedding：把句子变成矩阵

#### 4.1.1 概念说明

这个模块解决的问题是：神经网络只认数字，不认单词。我们需要一条确定性的流水线把任意一句英语变成一个整数序列，再变成浮点向量序列。旧版 torchtext 用 `data.Field` 对象声明「文本字段如何处理」，它接管了三件事：

1. **分词规则**——`tokenize='spacy'` 表示用 spaCy 分词器（比 `str.split` 能正确处理标点和缩写，如把 "don't" 切成 do + n't）；
2. **词表构建**——`build_vocab` 扫描训练集、统计词频、取最高频的 `max_size` 个词建立「词 → 编号」映射（`stoi`）与反向映射（`itos`）；
3. **数值化与批组装**——把每条样本转成整数序列，批内按最长句补 `<pad>`。

标签字段用 `data.LabelField(dtype=torch.float)` 声明，`dtype=torch.float` 是为了让标签能直接和 sigmoid 概率一起算二元交叉熵。

#### 4.1.2 核心流程

```
声明 Field(tokenize='spacy') 和 LabelField(dtype=float)
        │
IMDB.splits(TEXT, LABEL)      下载并切分数据：20000 训练 / 25000 测试
        │
train.split(split_ratio=0.8)  从训练集再切出 20%：5000 条验证集
        │
TEXT.build_vocab(train_data, max_size=20000)
        │  按词频取前 20000 词 + <unk> + <pad> ⇒ 词表大小 20002
        │
BucketIterator.splits(...)    产出批：batch.text 形状 [句长, 批大小]
```

注意一个本手册反复强调的纪律（u4-l3 讲过数据泄漏）：`build_vocab` **只喂 `train_data`**，词频统计只来自训练集；验证/测试样本数值化时查同一张词表，查不到的词落到 `<unk>`。

#### 4.1.3 源码精读

**超参数声明。** notebook 在 General Settings 段集中声明词表大小、批大小等超参：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L90-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L90-L101)

这段代码定义 `RANDOM_SEED=123`、`VOCABULARY_SIZE=20000`（词表最多收 2 万个词）、`LEARNING_RATE=1e-4`、`BATCH_SIZE=128`、`NUM_EPOCHS=15`，以及设备自动选择写法（u1-l2 讲过的三元表达式）。注意它也声明了 `EMBEDDING_DIM=128`、`HIDDEN_DIM=256`——但这两个值稍后会被覆盖，见 4.2.3 的提醒。

**数据加载与划分。** 声明两个 Field 并下载 IMDb：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L162-L170](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L162-L170)

`TEXT` 用 spaCy 分词；`LABEL` 声明为 float 类型；`datasets.IMDB.splits` 下载 84MB 的 aclImdb 数据并给出训练/测试两个划分；随后 `train_data.split(..., split_ratio=0.8)` 把 2.5 万条训练数据按 8:2 切成 2 万条训练与 5 千条验证。notebook 保存的输出显示 `Num Train: 20000 / Num Valid: 5000 / Num Test: 25000`。

**构建词表。** 这是本模块的核心调用：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L206-L210](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L206-L210)

`TEXT.build_vocab(train_data, max_size=VOCABULARY_SIZE)` 只用训练集统计词频并取前 20000 个词；notebook 输出 `Vocabulary size: 20002`——多出的 2 正是 `<unk>`（默认编号 0）与 `<pad>`（默认编号 1）。`LABEL.build_vocab` 建立 {neg, pos} → {0, 1} 的映射，输出 `Number of classes: 2`。

**构造批迭代器。** torchtext 的 `BucketIterator` 扮演 DataLoader 的角色：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L243-L246](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L243-L246)

`data.BucketIterator.splits` 一次为三个数据集建迭代器。它的「桶」策略是：把长度相近的样本尽量分进同一个批，使批内只需少量 padding。notebook 随后打印了各迭代器第一个批的形状（[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L291-L305](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L291-L305) 的代码及其上方保存的输出）：训练批 `Text matrix size: torch.Size([880, 128])`——880 是该批最长影评的 token 数，128 是批大小。**第一维是句长、第二维才是批大小**，这个「句长优先」的形状与 `nn.RNN` 的默认约定（`batch_first=False`）天然对齐，是理解下一模块的关键。

#### 4.1.4 代码实践

**实践目标**：亲手确认「词表 + 两个特殊词元」的结构，把抽象的 20002 变成看得见的东西。

**操作步骤**（示例代码，在能运行旧版 torchtext 的环境中、执行完 `build_vocab` 之后运行）：

```python
# 示例代码：检视 TEXT.vocab
vocab = TEXT.vocab
print(f"词表大小: {len(vocab)}")           # 期望 20002
print(f"<unk> 编号: {vocab.stoi['<unk>']}")  # 期望 0
print(f"<pad> 编号: {vocab.stoi['<pad>']}")  # 期望 1
print("最常见的前 10 个词:", vocab.itos[:2 + 10])  # 前两位是特殊词元
print("'movie' 的编号:", vocab.stoi['movie'])
```

**需要观察的现象**：`itos` 列表前几名是高频词（the、a、and 之类，注意它们不带语义但也没被过滤——本 notebook 未用停用词表）；`'movie'` 这类影评高频实词编号很小。

**预期结果**：词表大小恰为 20002，两个特殊词元编号为 0 和 1；`stoi['movie']` 是一个远小于 20000 的整数。若无旧版环境，本实践可降级为阅读型：对照 `Vocabulary size: 20002` 的 notebook 输出，口算 `20000 + 2` 的两个特殊词元各是什么。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `build_vocab` 传的是 `train_data` 而不是 `(train_data, valid_data, test_data)`？

**答案**：词表是「从数据里学到的统计量」。若用验证/测试集的词建词表，等于把评估时才能看的信息泄漏进训练期的决策，违反 u4-l3 确立的原则——一切统计量只取自训练集。测试时遇到词表外的词落到 `<unk>`，这正是对未知数据的诚实模拟。

**练习 2**：某批的 `batch.text` 形状是 `[880, 128]`，批内最短的影评只有 61 个 token，这条短影评的整数序列有多少个位置是 `<pad>`？

**答案**：819 个。批内所有样本都要对齐到最长者的 880 步，短句前 61 步是真实词元、后 819 步全是编号 1（`<pad>`）。这也预告了 4.3 要讨论的问题：RNN 会老老实实把这 819 步 padding 也扫描一遍。

**练习 3**：把 `VOCABULARY_SIZE` 从 20000 改成 5000，`nn.Embedding` 的参数量会怎么变？这对总参数量意味着什么？

**答案**：Embedding 权重是 `词表大小 × 嵌入维度`，20002→5002 后参数从 20002×64≈128 万降到约 32 万，缩小为约 1/4。由于 Embedding 占本模型总参数的绝大头（见 4.2.4 的计算），词表大小是本模型最有效的「参数旋钮」；代价是更多词落入 `<unk>`，语义信息受损。

### 4.2 nn.RNN：形状、隐状态与一个易被坑的超参覆盖

#### 4.2.1 概念说明

`nn.RNN` 是 PyTorch 内置的最朴素循环单元，逐时间步执行 4.1 节的公式 \( h_t = \tanh(W_{xh} x_t + W_{hh} h_{t-1} + b) \)。理解它只需抓两件事：

**形状约定（句长优先）。** `nn.RNN(input_size, hidden_size)` 默认 `batch_first=False`，即输入张量形状为 `[句长 L, 批大小 B, 特征维 E]`——时间维在最前。它返回两个张量：

- `output`：`[L, B, H]`，**每个时间步**的隐状态 \( h_1, h_2, \ldots, h_L \) 全history；
- `hidden`：`[num_layers × num_directions, B, H]`，**最后时间步**的隐状态。本模型是单层单向，所以形状就是 `[1, B, H]`，且 `hidden[-1] == output[-1]`。

**隐状态的「记忆」角色。** \( h_t \) 由两项构成：当前词的贡献 \( W_{xh} x_t \) 与历史记忆的贡献 \( W_{hh} h_{t-1} \)。梯度要传到句子开头的词，必须沿 \( W_{hh} \) 连乘 L 次；当 L 达到数百（影评动辄几百词），连乘项趋零（或爆炸），这就是 4.3 将观测到的「学不动」的理论根源——梯度消失/爆炸。

#### 4.2.2 核心流程

模型 `forward` 的三步（对应下方源码）：

```
text [L, B]（整数索引）
   │ self.embedding(text)         查表
   ▼
embedded [L, B, 64]
   │ self.rnn(embedded)           沿 L 逐步扫描
   ▼
output [L, B, 128]、hidden [1, B, 128]
   │ hidden.squeeze(0) → [B, 128]；self.fc → [B, 1]；view(-1) → [B]
   ▼
logits（每条影评一个标量分数）
```

#### 4.2.3 源码精读

**RNN 类定义**（本讲最核心的一段代码）：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L332-L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L332-L351)

`__init__` 登记三层：`nn.Embedding(input_dim, embedding_dim)` 是 4.1 讲的查表；`nn.RNN(embedding_dim, hidden_dim)` 是循环主体；`nn.Linear(hidden_dim, output_dim)` 把最终记忆映射为 1 维打分。`forward` 里作者用注释逐行标注了形状演化——`text` 是 `[句长, 批大小]` 的索引矩阵，`embedding` 查表后多出嵌入维变成 `[L, B, 64]`；`nn.RNN` 返回的 `output` 是全部时间步、`hidden` 是 `[1, B, 128]`；最后一行 `self.fc(hidden.squeeze(0)).view(-1)` 把 `[1, B, 128]` 压掉层数维、过全连接、再展平成 `[B]` 的 logits。整段代码没有一个显式循环——`nn.RNN` 内部完成了沿时间步的展开。

**模型实例化——注意覆盖陷阱**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L364-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L364-L372)

`INPUT_DIM = len(TEXT.vocab)`（=20002）让 Embedding 行数与词表精确对齐；优化器用 `torch.optim.Adam`，学习率取 General Settings 的 1e-4。**关键细节**：这里重新赋值 `EMBEDDING_DIM = 64`、`HIDDEN_DIM = 128`，覆盖了 L99-L100 声明的 128/256。也就是说，notebook 实际训练的模型是 Embedding 64 维、隐状态 128 维，而不是设置段写的那个。u3-l5 已总结过「读 notebook 要以变量最终来源为准」，这里又是一个现成例子：**同名变量的最后一次赋值才作数**。

**复现性设置**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L57-L67](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L57-L67)

imports 单元里 `torch.backends.cudnn.deterministic = True` 强制 cuDNN 选确定性卷积/循环内核，配合两处 `torch.manual_seed(RANDOM_SEED)`（设置段与建模前各一次），呼应 u4-l4 讲过的可复现性分层。

#### 4.2.4 代码实践

**实践目标**：用参数量证明「Embedding 是本模型的参数霸主」，并验证你对各层形状的理解。

**操作步骤**（示例代码，接在模型实例化之后运行；本实践不依赖旧版 torchtext——只要手工按 20002/64/128 构建同款模型即可在任何 PyTorch 环境复现）：

```python
# 示例代码：分层统计参数量
model = RNN(input_dim=20002, embedding_dim=64, hidden_dim=128, output_dim=1)
for name, param in model.named_parameters():
    print(f"{name:20s} {str(tuple(param.shape)):18s} {param.numel():>9,d}")
print(f"{'合计':20s} {'':18s} {sum(p.numel() for p in model.parameters()):>9,d}")
```

**需要观察的现象**：逐层打印出的形状——`embedding.weight` 为 `(20002, 64)`；`rnn.weight_ih_l0` 为 `(128, 64)`、`rnn.weight_hh_l0` 为 `(128, 128)`、两个偏置各 `(128,)`；`fc.weight/fc.bias` 为 `(1, 128)/(1,)`。

**预期结果**（手算即得，待本地验证具体打印格式）：

| 层 | 计算 | 参数量 |
|---|---|---|
| Embedding | 20002 × 64 | 1,280,128 |
| nn.RNN | 128×64 + 128×128 + 128 + 128 | 24,832 |
| Linear | 128 + 1 | 129 |
| **合计** | | **1,305,089** |

Embedding 一层占约 98%。这正是 NLP 模型的普遍形态，也是后续讲义（GloVe 预训练词向量）要优化这部分参数的动机。

#### 4.2.5 小练习与答案

**练习 1**：为什么本模型不需要 `batch_first=True`？

**答案**：`batch_first=True` 是让输入形状变成 `[B, L, E]` 的开关。torchtext 的 `BucketIterator` 吐出的 `batch.text` 本来就是 `[L, B]`（句长优先），与 `nn.RNN` 的默认约定一致，直接喂入即可；若强行开 `batch_first`，反而要先转置。形状约定没有优劣，只有「数据管线与模型约定要对齐」这一条纪律。

**练习 2**：单层单向 RNN 中 `output[-1]` 与 `hidden` 是什么关系？写成 `self.fc(output[-1])` 行不行？

**答案**：完全相等，所以行得通。`output` 收集了所有时间步的隐状态，其最后一步就是 `hidden` 挤掉层数维后的内容。但当模型变成多层（`num_layers>1`）或双向（`bidirectional=True`）时，`hidden` 的第一维会变成 `层数×方向数`，`squeeze(0)` 将不再适用而需要拼接/索引——u6-l3 会遇到这个情形。写 `hidden` 的好处是接口对未来扩展更稳。

**练习 3**：把 `HIDDEN_DIM` 从 128 改成 256，`nn.RNN` 那一项参数量怎么变？

**答案**：变为 256×64 + 256×256 + 256 + 256 = 82,176，约增至 3.3 倍。注意增长主要来自 \( W_{hh} \) 的 \( H^2 \) 项——隐状态维度对循环层参数是平方级影响，而 Embedding 只受 `EMBEDDING_DIM` 线性影响。

### 4.3 many-to-one 分类：从最终隐状态到情感概率

#### 4.3.1 概念说明

序列模型按「输入序列 → 输出形态」可分几类：many-to-many（每步都有输出，如 char-RNN 语言模型，u6-l4 会见）、seq2seq（序列到序列），以及本讲的 **many-to-one**：整条序列读完后输出一个判断。情感分类正是 many-to-one——不管影评有 40 个词还是 800 个词，输出始终是一个「正面概率」。

网络尾部沿用 u2-l1 逻辑回归的套路：`nn.Linear(hidden_dim, 1)` 输出标量 logit，配 `F.binary_cross_entropy_with_logits` 损失（吃 logits、内部做数值稳定的 log-sigmoid，这是 u5 系列反复强调的纪律：logits 配 `with_logits` 版损失）。推断时 `sigmoid(logit) > 0.5` 即判正面。

#### 4.3.2 核心流程

训练循环仍是 u2-l2 的五步范式，只是套在 torchtext 迭代器上：

```
每个 epoch:
    model.train()
    遍历 train_loader 的每个批:
        logits = model(batch.text)                        # ① forward
        cost  = BCE_with_logits(logits, batch.label)      # ② loss
        optimizer.zero_grad()                             # ③ 清梯度
        cost.backward()                                   # ④ 反向传播
        optimizer.step()                                  # ⑤ 更新参数
    在 train/valid 上各算一次准确率（model.eval + no_grad）
```

评估准确率的函数与 u2-l2 的 `compute_accuracy` 同构，仅阈值判断不同。单句预测函数则演示了「分词 → `stoi` 查编号 → 变张量 → 补一个伪批维 → sigmoid」的完整推断管线。

#### 4.3.3 源码精读

**最终隐状态到 logits 的一行**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L351)

`return self.fc(hidden.squeeze(0)).view(-1)` 三步连做：`squeeze(0)` 把 `[1, B, 128]` 压成 `[B, 128]`（去掉「层数×方向数」维），全连接映射成 `[B, 1]`，`view(-1)` 展平成 `[B]`——与标签向量形状一致，可直接算损失。

**训练循环五步**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L537-L549](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L537-L549)

`for epoch` / `for batch_idx` 双层循环内，`logits = model(batch_data.text)`、`cost = F.binary_cross_entropy_with_logits(logits, batch_data.label)`、`zero_grad`、`backward`、`step` 五步与 MNIST notebook 逐字对应——序列模型换的只是网络结构，训练范式不变。每 50 个批打印一次成本。

**二元准确率评估**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L395-L404](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L395-L404)

`compute_binary_accuracy` 内部先 `model.eval()` 再 `torch.no_grad()` 遍历数据集（u2-l4 讲过这两件事分别管层行为与梯度）；`predicted_labels = (torch.sigmoid(logits) > 0.5).long()` 把概率阈值化成 0/1，与 `batch_data.label.long()` 比对求正确数。每个 epoch 结束，主循环在 [L552-L561](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L552-L561) 打印训练/验证准确率。

**单句预测管线**：

[pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L582-L592](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L582-L592)

`predict_sentiment` 展示了部署视角的完整链路：spaCy 分词得 `tokenized`，`TEXT.vocab.stoi[t]` 查每个词的编号，`torch.LongTensor(indexed)` 转张量后 **`tensor.unsqueeze(1)` 补一个伪批维**（模型期望 `[L, B]`，单句就是 `[L, 1]`——和 u2-l2 的 ModelWrapper 单样本适配是同一个道理），前向取 sigmoid 得正面概率。notebook 保存的输出里，句子 "I really love this movie. This movie is so great!" 得到 0.5701——只比瞎猜的 0.5 高一点，这引出下面的诚实讨论。

**必须直面的失败结果。** notebook 开头的说明 markdown（[L28-L32](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L28-L32)）预先声明了两件事：简单 RNN 受梯度消失/爆炸困扰、padding 实现比 packed 版慢约 4 倍。保存的训练输出印证了前者：15 个 epoch 的 cost 始终徘徊在 0.69 左右（\( \ln 2 \approx 0.693 \)，正是「输出恒为 50/50 概率」时的二元交叉熵），最终训练准确率 50.16%、验证 50.68%、**测试 46.38%**——模型根本没学到东西。原因即 4.2.1 的分析：梯度沿 \( W_{hh} \) 连乘数百步而湮灭，开头携带的情感信号传不到参数更新；再叠加批内短句尾部的成百上千个 `<pad>` 也会被逐一扫过，进一步稀释最终隐状态。这不是 bug，而是简单 RNN 在长序列上的真实局限，notebook 把这个负结果原样保存，本身就是很好的教材。

#### 4.3.4 代码实践

**实践目标**：验证「50% 平台期」是模型结构问题而非训练代码问题——用同一个训练循环训练一个非循环基线。

**操作步骤**（示例代码，思路演示；完整可跑版本见第 5 节综合实践的环境说明）：

```python
# 示例代码：均值池化基线（替换 RNN，训练循环原封不动）
class MeanPoolBaseline(nn.Module):
    def __init__(self, input_dim, embedding_dim, output_dim):
        super().__init__()
        self.embedding = nn.Embedding(input_dim, embedding_dim)
        self.fc = nn.Linear(embedding_dim, output_dim)
    def forward(self, text):                    # text: [L, B]
        embedded = self.embedding(text)         # [L, B, E]
        return self.fc(embedded.mean(dim=0)).view(-1)  # 沿时间平均 → [B]
```

用它替换 `RNN` 类，其余代码（损失、优化器、训练循环、评估）一字不改，训练同样轮数。

**需要观察的现象**：均值池化基线的 cost 能跌破 0.69、验证准确率明显高于 50%。

**预期结果**：即便如此简单的基线，也应在若干 epoch 后把验证准确率带到 70% 以上（待本地验证：具体数值依赖环境与轮数）。若 RNN 版停在 50% 而基线正常学习，即可排除「训练循环写错」的嫌疑，把责任锁定在 RNN 的长程梯度上。

#### 4.3.5 小练习与答案

**练习 1**：训练输出里 cost 一直贴着 0.69，这个数字为什么恰好是「什么都没学」的标志？

**答案**：二元交叉熵下，若模型对任何输入都输出概率 0.5，则损失为 \( -\ln 0.5 = \ln 2 \approx 0.693 \)。随机初始化的 sigmoid 输出接近 0.5，而梯度消失使它停留在那里，于是 cost 曲线在 0.69 附近躺平——这是「二分类模型未学习」的经典信号。

**练习 2**：`predict_sentiment` 里若漏写 `tensor = tensor.unsqueeze(1)` 会发生什么？

**答案**：输入变成一维张量 `[L]`，而 `nn.Embedding` 查表后得到 `[L, 64]`，`nn.RNN` 会把它解释为「L 个批、每批 64 维特征」而不是「1 条长 L 的序列」，隐状态变成 `[1, 64, 128]` 形状的广播结果，随后 `fc` 与 `view(-1)` 的形状对不上（或得到错误语义的结果）而报错/输出无意义值。单样本推断补伪批维是与训练形状对齐的必要步骤。

**练习 3**：既然 `BucketIterator` 已经按长度分桶，为什么说 padding 仍在伤害这个模型？

**答案**：分桶只保证「批内长度相近」，不保证相等——批内仍以最长者为界补齐。更重要的是即便长度完全一致，句子有长有短这件事跨批存在，且影评本身长达数百词，RNN 必须逐步扫完全部（包括无信息的 `<pad>`）才能得到最终隐状态。packed sequences（下一讲）让 RNN 在每个样本自己的最后一个真实词元处停下，既省算力（快约 4 倍）又让隐状态不被 padding 污染。

## 5. 综合实践

**任务：序列截断 × Embedding 维度的受控对比实验。**

长影评是简单 RNN 的天敌（4.3.3 的 50% 平台期）。本实践把句子截断到固定长度、并扫描 Embedding 维度，量化这两个旋钮对「验证准确率 + 训练时间」的影响。设计沿用 u2-l4 确立的受控对比法：一次只动一个变量。

**步骤**：

1. **搭环境**（二选一）：
   - 旧版路线：Python 3.7 + torch 1.0.1.post2 + 配套 torchtext 0.x + spaCy 英文模型（notebook 保存环境，见 watermark 输出）。安装旧依赖常有兼容性麻烦，卡住就转下一条。
   - 现代路线：用 Hugging Face `datasets` 的 imdb + 任意现代分词器手写数值化管线（u4-l2 的 Dataset 三要素正好用上），把 notebook 的模型与训练循环搬过去。数据管线可换，模型与循环保持原样。
2. **加截断**：不引入新 API，直接在训练循环里切时间步（示例代码）：

   ```python
   # 示例代码：只取每条影评的前 200 个词元
   logits = model(batch_data.text[:200])
   ```

   评估与预测处做同样截断。这样 `<pad>` 与超长影评同时被压制，最长步数封顶。
3. **跑网格**：`EMBEDDING_DIM ∈ {32, 64, 128}` × 截断长度 `∈ {100, 200, 不截断}`，共 9 组；其余超参（`HIDDEN_DIM=128`、学习率 1e-4、批 128）与 notebook 保持一致。每组至少跑 5 个 epoch（有条件跑满 15），用 `time.time()` 记录耗时（notebook 的 [L537](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L537) 前已有 `start_time = time.time()` 可仿照）。
4. **填表**：

   | 截断长度 | EMBEDDING_DIM | 每轮耗时(min) | 最终验证准确率 | 最终 cost |
   |---|---|---|---|---|
   | 100 | 32 | | | |
   | 100 | 64 | | | |
   | …（9 行） | | | | |

5. **分析**：训练时间应随截断长度近似等比下降（RNN 计算量 ∝ 步数）；验证准确率未必单调——截太短丢信息，不截则梯度撑不住。找出本设置下的最优组合，并回答：截断能否把简单 RNN 从 50% 平台期里救出来？

**预期结果（待本地验证）**：截断到 100–200 后，cost 应能跌破 0.69、验证准确率应明显高于 50%（时间步数减少直接缓解梯度连乘）；Embedding 维度在截断固定时影响相对温和。若旧环境搭不起来，降级完成：跑通第 5 节的均值池化基线 + 任意一组截断配置即可，并把「读 4.3.3 保存输出得到的三行结论」写进表格备注。

## 6. 本讲小结

- 文本数值化流水线：`Field(tokenize='spacy')` 声明处理规则 → `build_vocab` 只用训练集按频率取前 20000 词 → 词表大小 20002（多出的 2 是编号 0 的 `<unk>` 与编号 1 的 `<pad>`）→ `BucketIterator` 按长度分桶出 `[句长, 批大小]` 的整数批。
- 模型是三件套 `Embedding(20002, 64) → nn.RNN(64, 128) → Linear(128, 1)`；Embedding 以约 128 万参数占总参数 1,305,089 的 98%，是 NLP 模型的参数霸主。注意 notebook 里 `EMBEDDING_DIM/HIDDEN_DIM` 在建模单元被覆盖为 64/128，读源码以最后一次赋值为准。
- `nn.RNN` 句长优先：输入 `[L, B, E]`，返回全时间步 `output [L, B, H]` 与最终步 `hidden [1, B, H]`；many-to-one 分类取 `hidden.squeeze(0)` 过全连接得每句一个 logit，配 `binary_cross_entropy_with_logits`。
- 训练循环与 MNIST 系列逐字同构（forward → loss → zero_grad → backward → step）；推断时单句要 `unsqueeze(1)` 补伪批维，`stoi` 查编号后过 sigmoid 得正面概率。
- 诚实的负结果：简单 RNN 在数百词的影评上 cost 躺平 0.69（\( \ln 2 \)）、测试准确率仅 46.38%——梯度沿 \( W_{hh} \) 连乘数百步而消失，加之 `<pad>` 被逐步扫过稀释最终隐状态。
- 两条出路都已在本仓库就位：packed sequences 让 RNN 跳过 padding（快约 4 倍），LSTM/GRU 用门控缓解梯度消失——两者是下一讲的全部内容。

## 7. 下一步学习建议

下一讲 **u6-l2（LSTM、GRU 与 packed sequences）** 直接接住本讲留下的两个问题：

1. 先读 `pytorch_ipynb/rnn/rnn_simple_packed_imdb.ipynb`——与本讲唯一的数据管线差异是 `pack_padded_sequence/pad_packed_sequence`，对照两份 notebook 的训练时间即可体会「跳过 padding」的收益。
2. 再读 `rnn_lstm_packed_imdb.ipynb` 与 `rnn_gru_packed_imdb.ipynb`，看门控单元（输入门/遗忘门/输出门）如何取代朴素 `tanh` 更新，把验证准确率从 50% 拉到 80% 以上（具体数值以 notebook 保存输出为准）。

延伸阅读：若你想先补 NLP 数值化的现代做法，可跳读 u6-l3（GloVe 预训练词向量如何替换本讲随机初始化的 Embedding 那 98% 的参数）与 u6-l6（Hugging Face tokenizers 取代 `data.Field`）；若对「隐状态如何浓缩序列」的数学感兴趣，4.2.1 的梯度连乘分析在 u2-l5 手工反向传播里有亲手推导过的同款工具。
