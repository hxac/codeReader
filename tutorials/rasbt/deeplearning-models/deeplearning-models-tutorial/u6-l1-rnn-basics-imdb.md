# 第一个 RNN：IMDb 情感分类

## 1. 本讲目标

从本讲起，我们离开图像，进入**文本序列**的世界。前面所有模型（MLP、CNN）吃的输入都是**固定大小**的张量——MNIST 永远是 28×28。但一句影评可能有 10 个词，也可能有 800 个词，长度天然不固定；而且词序携带信息：「not good」和「good」词袋相同、情感相反。

本讲以 `pytorch_ipynb/rnn/rnn_simple_imdb.ipynb` 为唯一主线，在 IMDb 影评数据集上搭一个最简单的循环神经网络（RNN）做二分类。学完后你应该能够：

1. **掌握词表构建与 Embedding 层**：把「一句话」变成「一个整数索引序列」，再变成「一个稠密向量序列」，并理解 `<unk>`/`<pad>` 两个特殊 token 的来历。
2. **理解 `nn.RNN` 的输入输出形状**：搞清 `[句长, 批大小, 嵌入维]` → `output`/`hidden` 的完整形状链，尤其是「批在第 1 维」这个与 CNN 不同的约定。
3. **会取最后时间步做 many-to-one 分类**：整条评论压缩成一个隐状态向量，接单个 logit + sigmoid 完成情感判断，并复用你在 u2-l2 学到的五步训练循环。
4. **诚实面对一个「训练失败」的 notebook**：作者保存的输出显示模型准确率停在 50%（等于抛硬币）。我们要解释它为什么失败——这恰好是通往下一讲（LSTM/GRU 与 packed sequences）的门票。

前置依赖：u2-l3（`nn.Module` 写法）。本讲还会复用 u2-l1 的逻辑回归 + BCE 损失、u4-l1 的验证集划分思想、u5-l5 的 `binary_cross_entropy_with_logits` 用法。

## 2. 前置知识

### 2.1 从「固定大小图像」到「变长文本」需要哪些新零件

你已经熟悉的图像管线是：`datasets.MNIST` → `ToTensor()` → `DataLoader` 批拼装，每批形状恒为 `[128, 1, 28, 28]`。文本管线要多解决三个新问题，对应三个新零件：

| 新问题 | 新零件 | 一句话解释 |
| --- | --- | --- |
| 计算机只认数字，不认单词 | **分词器 + 词表（vocabulary）** | 把句子切成 token（词），给每个词发一个整数编号 |
| 编号 5 和编号 8000 没有大小语义 | **Embedding 层** | 一张可学习的查表矩阵，第 i 行就是第 i 个词的向量 |
| 评论长短不一，没法拼成一个矩阵 | **填充（padding）+ 循环网络** | 短句补 `<pad>` 对齐；RNN 沿时间步逐词读入，天然吃变长序列 |

### 2.2 Embedding：one-hot 的替代品

表示词的最笨办法是 one-hot：词表有 2 万个词，每个词就是 2 万维的 0/1 向量。问题有两个：维度灾难（一个词 2 万个数），且任意两词的内积都是 0，「movie」和「film」在数学上毫无关系。

`nn.Embedding(num_embeddings, embedding_dim)` 的做法是直接存一张 `[词表大小 × 嵌入维]` 的可学习矩阵 \( E \)，前向就是一次查表：

\[ \text{embedded}_t = E[x_t], \qquad x_t \in \{0, 1, \dots, V-1\} \]

它等价于「one-hot 向量乘权重矩阵」，但省去了真的构造 one-hot。矩阵的每一行就是一个词向量，训练中与其他参数一起被梯度更新——词的语义是**学出来的**，不是规则给的。

### 2.3 RNN：沿时间共享权重的小机器

循环神经网络的核心只有一个不断重复的更新式。设 \( x_t \) 是第 \( t \) 个词的嵌入向量，\( h_t \) 是读完前 \( t \) 个词后的「记忆」（隐状态）：

\[ h_t = \tanh(W_{xh}\, x_t + W_{hh}\, h_{t-1} + b) \]

直觉：每读一个新词，就把「旧记忆 \( h_{t-1} \)」和「新词 \( x_t \)」加权混合后压进 tanh。\( W_{xh}, W_{hh}, b \) 在所有时间步**共享**——读第 1 个词和第 500 个词用的是同一台机器。这是它与「把 500 个词拼接成一个大向量喂 MLP」的本质区别：参数量与句子长度无关。

### 2.4 你需要带着走的旧知识

- **五步训练循环**（u2-l2）：`forward → loss → zero_grad → backward → step`，本讲原样复用。
- **BCE with logits**（u2-l1、u5-l5）：二分类输出单个 logit，`F.binary_cross_entropy_with_logits` 内部自带数值稳定的 sigmoid，喂 logits 而不是概率。随机初始化的网络对一切都输出 \( p=0.5 \) 时，损失恰为 \( \ln 2 \approx 0.693 \)——这个数在本讲会反复出现，它是「模型什么都没学会」的指纹。
- **验证集**（u4-l1）：从训练数据里切出一块供调参用，测试集只在最后碰一次。
- **`nn.Module` 骨架**（u2-l3）：`__init__` 建层、`forward` 写计算，本讲的 `RNN` 类照抄这个模板。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb) | 本讲唯一主角：torchtext 旧式 API 加载 IMDb → 建词表 → Embedding + 单层 `nn.RNN` + 线性头 → many-to-one 二分类。作者保存的输出显示它训练失败（测试准确率 46.38%），这个失败本身就是教材 |

`pytorch_ipynb/rnn/` 目录下还有 7 个 notebook，本讲不展开，只在第 7 节预告：

- `rnn_simple_packed_imdb.ipynb`：同一模型改用 packed sequences，作者注明比本讲版本**快约 4 倍**（u6-l2 的主角）。
- `rnn_lstm_packed_imdb.ipynb` / `rnn_gru_packed_imdb.ipynb`：把简单 RNN 换成 LSTM/GRU（u6-l2）。
- `rnn_lstm_bi_imdb.ipynb`、`rnn_lstm_packed_imdb-glove.ipynb`、`rnn_lstm_packed_own_csv_imdb.ipynb`、`rnn_bi_multilayer_lstm_own_csv_agnews.ipynb`：双向、GloVe 预训练词向量、自有 CSV 数据（u6-l3）。
- `char_rnn-charlesdickens.ipynb`：字符级文本生成（u6-l4）。

> **两点阅读须知**
>
> 1. **行号说明**：notebook 在磁盘上是 JSON 文件，本文引用的行号是该 JSON 文件的真实行号，与 GitHub 的 `#L` 锚点一致。
> 2. **环境说明**：本 notebook 使用 **torchtext 旧式 API**（`data.Field`、`datasets.IMDB`、`BucketIterator`），作者保存输出对应 Python 3.7.1 + torch 1.0.1 时代。新版 torchtext（≥0.12）已彻底移除这套 API（0.9–0.11 期间曾迁入 `torchtext.legacy`），所以文中凡涉及 torchtext 的实践均标注「待本地验证」；而第 4.2 节的形状实验只用纯 `torch`，现代环境可直接运行。

## 4. 核心概念与源码讲解

先看全局超参数，心里有张底片：

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L90-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L90-L101) 定义随机种子 123、词表上限 `VOCABULARY_SIZE = 20000`、学习率 `1e-4`、批大小 128、训练 15 个 epoch，以及 `EMBEDDING_DIM = 128`、`HIDDEN_DIM = 256` 等模型超参——**注意后三个在建模 cell 里会被覆盖**（见 4.2.3），实际生效的是 64/128/1，这是「以变量最终来源为准」的又一案例（呼应 u3-l5）。

### 4.1 词表与 Embedding：把句子变成数字矩阵

#### 4.1.1 概念说明

这个模块解决「文本如何进网络」的前半段：**分词 → 编号 → 查表**。

- **Field**：torchtext 对「一类文本字段如何处理」的描述——用什么分词器、要不要小写化、数值化成什么 dtype。本讲用 spacy 分词器（比朴素按空格切更能正确处理标点，如把 `great!` 切成 `great` + `!`）。
- **词表（vocab）**：从**训练集**统计词频，只保留最常见的 `max_size=20000` 个词，其余一律映射为 `<unk>`（unknown）。只用训练集建词表，与 u4-l3「标准化统计量只取自训练集」同理——验证/测试阶段见到的新词本来就该按未知处理，这是避免数据泄漏的纪律。
- **两个特殊 token**：`<unk>`（词表外的词）和 `<pad>`（填充符）。torchtext 默认把它们放在词表开头，所以最终词表大小是 20000 + 2 = **20002**。
- **Embedding 层**：`nn.Embedding(20002, 64)`，一张 `20002 × 64` 的可学习查表矩阵，前向接收整数索引、返回对应行的向量。它占了整个模型约 98% 的参数。

#### 4.1.2 核心流程

从原始影评到模型可吃的整数矩阵：

```text
影评文本（字符串）
   │  spacy 分词
   ▼
token 序列 ["this", "movie", "is", ...]
   │  查 vocab.stoi（不在词表 → <unk> 的索引）
   ▼
整数索引序列 [13, 208, 7, ...]
   │  BucketIterator 组批：长度相近的样本尽量放同一批
   │  批内按最长句子补 <pad> 对齐
   ▼
批张量 batch.text，形状 [句长, 批大小]     ← 注意：句长在第 0 维！
批张量 batch.label，形状 [批大小]（0/1 浮点）
```

关键点有两个：

1. **批在第 1 维**。torchtext 的批是 `[sentence_len, batch_size]`，与 CNN 管线的 `[batch, channels, H, W]`（批在第 0 维）正好相反。`nn.RNN` 默认也采用这个约定（`batch_first=False`），所以两边「恰好对上」——但读形状时必须时刻清醒。
2. **句长随批变化**。批内每行长度等于该批最长文档的长度，短文档尾部全是 `<pad>`。作者在 notebook 开头的说明里点明了后果：句子越短，补的 `<pad>` 越多（这对取「最后时间步」的本模型是实打实的伤害，4.3 节展开）。

#### 4.1.3 源码精读

**第一步：定义字段并切分数据集。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L162-L171](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L162-L171) 定义 `TEXT = data.Field(tokenize='spacy')`（文本字段，spacy 分词）和 `LABEL = data.LabelField(dtype=torch.float)`（标签字段，注意 dtype 是 **float**——后面 BCE 损失要求浮点目标，这与 u2-l2 里 cross_entropy 要 int64 标签不同）；随后 `datasets.IMDB.splits` 下载并切出训练/测试集，再从 25000 条训练数据中以 `split_ratio=0.8` 切出 20000 训练 + 5000 验证——这正是 u4-l1 讲过的「验证集从训练数据里来」的又一次实践。

保存的输出显示：`Num Train: 20000`、`Num Valid: 5000`、`Num Test: 25000`。

**第二步：构建词表。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L206-L210](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L206-L210) 只用 `train_data` 调 `TEXT.build_vocab(..., max_size=VOCABULARY_SIZE)` 与 `LABEL.build_vocab(...)`。输出 `Vocabulary size: 20002`（20000 词 + `<unk>` + `<pad>`）、`Number of classes: 2`（neg/pos）。

**第三步：组批。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L243-L246](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L243-L246) 用 `data.BucketIterator.splits` 一次生成三个 loader，`batch_size=128`、`device=DEVICE`。BucketIterator 的职责是「按长度分桶组批」，让同一批里的句子长度尽量接近，从而少补 `<pad>`。

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L291-L307](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L291-L307) 从每个 loader 取第一个 batch 打印形状，验证管线。保存的输出（[L277-L285](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L277-L285)）：训练批 `Text matrix size: torch.Size([880, 128])`，验证批 `[61, 128]`，测试批 `[42, 128]`——第一维（句长）逐批不同，第二维（批大小）恒为 128。

**第四步：Embedding 层的定义。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L337](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L337) 在 `RNN.__init__` 中创建 `self.embedding = nn.Embedding(input_dim, embedding_dim)`，其中 `input_dim = len(TEXT.vocab) = 20002`。输入 `[句长, 批]` 的整数索引，输出 `[句长, 批, 64]` 的稠密向量。

#### 4.1.4 代码实践

**实践：亲手摸一摸词表（需旧版 torchtext 环境，待本地验证）。**

1. **实践目标**：确认词表内容与 `<unk>`/`<pad>` 的索引位置，直观理解「20002」的构成。
2. **操作步骤**：运行 notebook 到 `build_vocab` 之后，追加一个 cell（以下为**示例代码**）：

   ```python
   print(TEXT.vocab.itos[:6])                     # 词表前 6 个 token
   print(TEXT.vocab.stoi['<unk>'], TEXT.vocab.stoi['<pad>'])
   print(TEXT.vocab.freqs.most_common(10))        # 训练集最高频的 10 个词
   # 任意挑一句训练评论，数一数有多少词落在词表之外
   sample = train_data[0].text
   oov = [t for t in sample if t not in TEXT.vocab.stoi]
   print(f'句子长度 {len(sample)}，OOV 词数 {len(oov)}')
   ```

3. **需要观察的现象**：`itos[:6]` 的开头应是 `<unk>`、`<pad>`（torchtext 默认排在索引 0、1）；`most_common` 里多半是 the/a/and 等停用词；OOV 词数量不多但非零。
4. **预期结果**：`stoi['<unk>'] == 0`、`stoi['<pad>'] == 1`，词表总长 20002。具体数值**待本地验证**（需要 Python 3.7 + torch 1.0 + 旧版 torchtext 与 spacy `en` 模型的环境）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `max_size` 从 20000 降到 5000，`nn.Embedding` 的参数量会变成多少？这会带来什么代价？

<details><summary>参考答案</summary>

Embedding 参数量 = `词表大小 × embedding_dim`。词表大小变为 5000 + 2 = 5002，参数量从 20002 × 64 = 1,280,128 降到 5002 × 64 = 320,128，缩小为原来的约 1/4。代价是更多词被归入 `<unk>`，模型「不认识」的词变多，信息损失可能拉低准确率——词表大小是「参数量 vs 覆盖率」的权衡。
</details>

**练习 2**：为什么 `build_vocab` 只传 `train_data`，不把 `valid_data`/`test_data` 也传进去？

<details><summary>参考答案</summary>

词表（哪些词入围、每个词什么编号）是模型设计的一部分。若用验证/测试数据参与建词表，等于让模型在评估前「见过」测试集的词分布——这是数据泄漏，与 u4-l3「标准化统计量只能取自训练集」同一纪律。正确做法：词表冻结在训练集上，评估阶段遇到的新词一律按 `<unk>` 处理。
</details>

### 4.2 nn.RNN：循环层的一条时间线

#### 4.2.1 概念说明

`nn.RNN(embedding_dim, hidden_dim)` 是 PyTorch 内置的最朴素循环层：单层、单向、tanh 激活。它沿时间步逐个消化输入序列，每步执行 2.3 节的更新式，最终返回**两样东西**：

- `output`：每个时间步的隐状态，形状 `[句长, 批, hidden_dim]`——「逐字的记忆流水账」；
- `hidden`：最后一个时间步的隐状态，形状 `[1, 批, hidden_dim]`（三层含义：层数 × 方向数 = 1）——「读完一整句后的总结陈词」。

理解本模块要抓两个形状细节和一个梯度隐患：

1. **批在中间维**。输入 `[T, B, E]`（T 句长、B 批、E 嵌入维），`nn.RNN` 默认 `batch_first=False`，即时间步在第 0 维。这与 4.1 的 torchtext 批形状无缝衔接。
2. **单层单向时 `output[-1]` 与 `hidden.squeeze(0)` 是同一个张量**：都是「最后一个时间步的隐状态」。所以本模型拿哪个都行；但在多层/双向时二者含义不同（u6-l3 展开）。
3. **梯度沿时间指数衰减（消失）或爆炸**。反向传播要把梯度从第 T 步一路传回第 1 步，每过一步就乘一次 \( W_{hh}^\top \) 与 tanh 的局部导数。连乘 T 次：IMDb 评论动辄几百词，梯度要么缩成 0（学不动早期词），要么炸掉。这正是 u2-l5 里「sigmoid 饱和区梯度消失」在时间维上的放大版，也是作者在 notebook 开头警告「simple RNN usually doesn't work very well due to vanishing and exploding gradient problems」的原因。

#### 4.2.2 核心流程

`forward` 里 RNN 部分的形状流转（作者在源码注释里亲自标注了每一步）：

```text
text:            [句长 T, 批 B]              ← 整数索引
   │ self.embedding(text)                     ← 查表
embedded:        [T, B, 嵌入维 E=64]
   │ self.rnn(embedded)                       ← 沿 T 个时间步递推 h_t
   ├─ output:     [T, B, 隐维 H=128]          ← 每步的 h_t（本模型未使用）
   └─ hidden:     [1, B, H=128]               ← 最后一步的 h_T
```

时间维上展开看，就是同一台机器被调用 T 次：

\[ h_1 = \tanh(W_{xh} e_1 + b),\quad h_2 = \tanh(W_{xh} e_2 + W_{hh} h_1 + b),\quad \dots,\quad h_T = \tanh(W_{xh} e_T + W_{hh} h_{T-1} + b) \]

参数量的手工账（后面实践会验证）：

\[ \underbrace{H \times E}_{W_{xh}=128\times64} + \underbrace{H \times H}_{W_{hh}=128\times128} + \underbrace{2H}_{b} = 8192 + 16384 + 256 = 24832 \]

注意 RNN 的参数量**只取决于 E 和 H，与句长 T 无关**——权重跨时间共享，这是循环网络相对「拼接长向量喂 MLP」的根本优势。

#### 4.2.3 源码精读

**模型定义（本讲的核心 22 行）。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L330-L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L330-L351) 定义 `RNN` 类，完全遵守 u2-l3 的模板：`__init__` 里建三件套——`nn.Embedding(input_dim, embedding_dim)` 查表、`nn.RNN(embedding_dim, hidden_dim)` 循环层、`nn.Linear(hidden_dim, output_dim)` 输出头；`forward` 里两行计算——`embedded = self.embedding(text)` 把 `[T, B]` 变 `[T, B, E]`，`output, hidden = self.rnn(embedded)` 得到逐时间步输出与最终隐状态。作者用注释逐行标注形状变化（如 `#  hidden: [1, batch size, hidden size]`），是仓库里形状注释最细致的模型定义之一。

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L364-L372](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L364-L372) 实例化模型：`INPUT_DIM = len(TEXT.vocab)`（=20002）、**`EMBEDDING_DIM = 64`、`HIDDEN_DIM = 128`、`OUTPUT_DIM = 1`**——注意这里把 L99-L100「General Settings」里的 128/256 **覆盖**掉了，实际生效值是 64/128/1。改超参时若只改了 General Settings 而没改这里，等于白改（u3-l5「以变量来源为准」教训重演）。此外建模型前重播了一次 `torch.manual_seed(RANDOM_SEED)`——因为建词表、切数据消耗了随机数，重新播种保证权重初始化可复现（呼应 u4-l4：种子要在建模前打）。优化器是 `torch.optim.Adam`，学习率沿用 `LEARNING_RATE = 1e-4`。

按实际生效的超参（E=64、H=128）手工算总参数量：

| 层 | 计算 | 参数量 | 占比 |
| --- | --- | --- | --- |
| Embedding | 20002 × 64 | 1,280,128 | ≈98% |
| nn.RNN | 128×64 + 128×128 + 2×128 | 24,832 | ≈1.9% |
| Linear 输出头 | 128×1 + 1 | 129 | ≈0.01% |
| **合计** | | **≈1,305,089** | |

文本模型的典型形态：Embedding 独占绝大部分参数，循环层和输出头反而是零头。

#### 4.2.4 代码实践

**实践：用纯 torch 走一遍形状链，验证 `output[-1] == hidden[0]`（现代环境可直接运行）。**

1. **实践目标**：不看 notebook 也能独立写出 RNN 的形状流转；亲手验证「单层单向时最后时间步输出与 hidden 相同」。
2. **操作步骤**：在任意装有 PyTorch 的环境运行以下**示例代码**（不依赖 torchtext）：

   ```python
   import torch
   import torch.nn as nn

   torch.manual_seed(123)
   T, B, V, E, H = 7, 3, 50, 64, 128   # 句长7、批3、词表50、嵌入64、隐维128

   emb = nn.Embedding(V, E)
   rnn = nn.RNN(E, H)

   x = torch.randint(0, V, (T, B))      # 模拟一个 [句长, 批] 的整数批
   embedded = emb(x)
   output, hidden = rnn(embedded)

   print('x       :', x.shape)          # [7, 3]
   print('embedded:', embedded.shape)   # [7, 3, 64]
   print('output  :', output.shape)     # [7, 3, 128]
   print('hidden  :', hidden.shape)     # [1, 3, 128]
   print(torch.allclose(output[-1], hidden.squeeze(0)))   # 期望 True
   print('RNN 参数量:', sum(p.numel() for p in rnn.parameters()))  # 期望 24832
   ```

3. **需要观察的现象**：四个形状逐步变化；`allclose` 打印 `True`；参数量恰为 24832。
4. **预期结果**：与上面注释一致。此实验只用 `torch` 与 `torch.nn`，可在新版 PyTorch 上直接验证；若你的环境打印结果不同，优先检查是否误传了 `batch_first=True`。

#### 4.2.5 小练习与答案

**练习 1**：如果把这个模型改成 `nn.RNN(64, 128, batch_first=True)`，输入还需要做什么调整？改了有什么好处？

<details><summary>参考答案</summary>

`batch_first=True` 要求输入形状为 `[批, 句长, 嵌入维]`，而 torchtext 的 `batch.text` 是 `[句长, 批]`，因此需要先转置（如 `text.t()` 再嵌入，或在嵌入后 `embedded.permute(1, 0, 2)`）。好处是批在第 0 维、与 CNN 管线约定一致，代码风格统一；代价是本模型源码里所有形状注释都要跟着改。仓库采用默认（批在第 1 维）是为了与 torchtext 输出零转换衔接。
</details>

**练习 2**：`nn.RNN` 的参数量是 24832，为什么和句长 T（比如某批的 880）毫无关系？

<details><summary>参考答案</summary>

因为循环层在全部 T 个时间步上**共享同一组**权重 \( W_{xh}, W_{hh}, b \)：第 1 个词和第 880 个词用的是同一套参数。反向传播时各时间步的梯度会**累加**到这同一组参数上，而不是各自持有独立参数。这正是循环网络「参数量与序列长度解耦」的核心设计，也是它能处理变长输入的原因。
</details>

**练习 3**：把 `HIDDEN_DIM` 从 128 增大到 256，Embedding 之外的参数量大约变为多少？

<details><summary>参考答案</summary>

RNN 层：256×64 + 256×256 + 2×256 = 16384 + 65536 + 512 = 82,432；输出头：256 + 1 = 257；合计约 82,689（原来是 24,961）。注意参数随 H 是**平方级**增长的（\( H^2 \) 项来自 \( W_{hh} \)），而随 E 只是线性增长。
</details>

### 4.3 many-to-one 分类：取最后时间步做情感判断

#### 4.3.1 概念说明

序列模型按「输入序列 → 输出形态」分几类：many-to-many（每个时间步都有输出，如词性标注）、one-to-many（如文本生成）、以及本讲的 **many-to-one**——整条序列读完后输出**一个**判断。影评分类正属此类：读完全文，答一句「正面/负面」。

实现上朴素得惊人：拿 `hidden`（读完最后一个时间步的隐状态）当作整条评论的「摘要向量」，过一个 `nn.Linear(128, 1)` 得到单个 logit，再套 sigmoid 得到正类概率——**这其实就是 u2-l1 逻辑回归的序列版**：线性打分 + sigmoid + BCE 损失，只是「特征」从原始输入 x 换成了 RNN 读完整句后的 h_T。

这里埋着本讲最重要的隐患：**`h_T` 是「读完最后一个 token 之后」的记忆，而批内短句的尾部全是 `<pad>`**。对一句实际 20 个词、被填充到 880 步的评论，RNN 要再空转 860 步 `<pad>` 才停下——有用的记忆被几百步无意义的重复输入反复冲刷。作者在 notebook 开头明确写了这一点：

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L29-L31](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L29-L31) 作者自述：简单 RNN 因梯度消失/爆炸通常表现不佳；本实现用 padding 处理变长输入，句子越短补的 `<pad>` 越多；并注明这个版本比 packed sequences 版本训练慢约 4 倍。

两个坑（梯度消失 + padding 稀释）叠加，注定了这个 notebook 的结局——它是一个**刻意保留失败输出的教学样本**。

#### 4.3.2 核心流程

训练与推理的完整链路（训练循环仍是 u2-l2 的五步范式，一字未改）：

```text
每个 epoch:
  model.train()
  遍历 train_loader 的每个 batch (共 157 批 = 20000/128 向上取整):
    ① logits = model(batch.text)                  # [批] 个 logit
    ② cost  = F.binary_cross_entropy_with_logits(logits, batch.label)
    ③ optimizer.zero_grad()                       # 清梯度
    ④ cost.backward()                             # 反传（沿时间展开 T 步）
    ⑤ optimizer.step()                            # Adam 更新
  epoch 末: 对 train/valid 两个 loader 计算准确率
全部 epoch 结束: 对 test_loader 计算最终准确率

评估: predicted = (sigmoid(logits) > 0.5)      # logit → 概率 → 0/1
推理新句子: spacy 分词 → vocab.stoi 编号 → [句长, 1] 张量 → sigmoid
```

二分类交叉熵（对数以 e 为底）：

\[ \mathcal{L} = -\frac{1}{N}\sum_{i=1}^{N}\Big[\, y_i \ln p_i + (1-y_i)\ln(1-p_i) \,\Big] \]

当模型对所有样本都输出 \( p_i = 0.5 \)（什么都不知道）时，\( \mathcal{L} = \ln 2 \approx 0.693 \)。看训练日志时请带着这个标尺：**损失贴着 0.69 不动 = 模型在抛硬币**。

#### 4.3.3 源码精读

**取最后时间步的一行。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L351](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L351) `return self.fc(hidden.squeeze(0)).view(-1)`：`hidden` 形状 `[1, B, H]`，`squeeze(0)` 去掉层数维得 `[B, H]`（即最后时间步的隐状态，每个样本一行），过全连接得 `[B, 1]`，`view(-1)` 压平成 `[B]`——每条评论一个 logit。这就是 many-to-one 的全部实现：**不取 `output`，只取 `hidden`**。

**评估函数。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L395-L404](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L395-L404) `compute_binary_accuracy` 与 u2-l4 讲过的模式一致：`model.eval()` 管层行为 + `torch.no_grad()` 管梯度；每批 `torch.sigmoid(logits) > 0.5` 把 logit 变 0/1 预测，与 `.long()` 后的标签比对计数，最后返回百分比。它可套用于任意 binary logits 模型。

**训练循环。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L535-L566](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L535-L566) 标准的「epoch × batch」双层循环：`model.train()` 开头、五步训练范式居中（`logits = model(...)` → `binary_cross_entropy_with_logits` → `zero_grad` → `backward` → `step`）、每 50 批打印一次损失、每个 epoch 末在 train/valid 上评估并报告耗时，循环结束后在 test 上出最终成绩。注意标签直接用 `batch_data.label`——`LabelField(dtype=torch.float)` 在 4.1 就为这一步铺好了 float 类型。

**然后，看作者保存的真实输出——训练失败了。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L424-L431](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L424-L431) 第 1 个 epoch 的日志：首批损失 0.7111，随后各批在 0.68–0.70 之间徘徊；训练/验证准确率 49.94% / 49.96%。

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L524-L530](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L524-L530) 第 15 个 epoch 结束时：训练准确率 50.16%、验证准确率 50.68%、总耗时 6.35 分钟、**测试准确率 46.38%**。15 个 epoch 里损失始终贴着 \( \ln 2 \approx 0.693 \)、准确率始终贴着 50%——模型自始至终在抛硬币。

怎么解读这个失败？三个原因按重要性排列：

1. **梯度无法流过长序列**（主因）。评论数百词，tanh 循环的梯度沿时间连乘数百次，早期词的信号到达参数更新时已趋近于零（作者在 L29 明说了 vanishing/exploding gradient）。模型连训练集都拟不到 50% 以上，谈不上泛化。
2. **padding 稀释了「最后时间步」**。取 `hidden` 意味着读的是「补完 `<pad>` 之后」的状态；短评论的有用记忆被数百步填充符冲刷，many-to-one 的摘要质量极差。
3. **训练配方保守**。Adam 学习率 1e-4 + 15 个 epoch，对 130 万参数的模型偏少；但即便调大，前两条结构性问题依然存在。

顺带一提 46.38% 这个「低于 50%」的测试准确率：它并不神秘——模型输出并非精确的 0.5，验证/测试分布上的微小系统性偏移可以让「略差于随机」的固定决策出现，小样本批上的方差也会造成这种现象。

**推理：对任意一句话打分。**

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L579-L592](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L579-L592) `predict_sentiment` 把训练管线的每个环节手动重演一遍：spacy 分词 → `TEXT.vocab.stoi` 查索引 → `torch.LongTensor(...).unsqueeze(1)` 把 `[句长]` 变 `[句长, 1]`（批大小为 1 的批，与训练形状约定保持一致）→ `torch.sigmoid(model(tensor))` 得到正类概率。这份「推理时重演预处理」的手工流程，就是 u2-l2 里 `ModelWrapper` 思路的函数版。

> [pytorch_ipynb/rnn/rnn_simple_imdb.ipynb:L616-L618](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_simple_imdb.ipynb#L616-L618) 对一句明显正面的评论 `"I really love this movie. This movie is so great!"`，模型输出概率 **0.5701**——方向对了，但只比抛硬币高 7 个百分点，与 50% 的训练结果自洽：模型没有真正学会情感。

#### 4.3.4 代码实践

**实践：给失败做「尸检」，用数字确认模型输出恒近 0.5（可在训练后进行，待本地验证）。**

1. **实践目标**：用亲手统计的证据解释「准确率 50%」的含义，把 `ln 2` 标尺用起来。
2. **操作步骤**：训练（或加载权重）后追加一个 cell（**示例代码**）：

   ```python
   import torch
   model.eval()
   probs = []
   with torch.no_grad():
       for batch in valid_loader:
           probs.append(torch.sigmoid(model(batch.text)))
   probs = torch.cat(probs)
   print(f'均值 {probs.mean():.4f}，标准差 {probs.std():.4f}')
   print(f'概率在 [0.45, 0.55] 区间的比例: {((probs > 0.45) & (probs < 0.55)).float().mean():.2%}')
   ```

3. **需要观察的现象**：均值接近 0.5、标准差很小、绝大多数预测落在 0.45–0.55——即模型对一切输入都「不确定」。
4. **预期结果**：若复现训练，输出应呈现「均值 ≈0.5、标准差 ≈0.02–0.05」的形态；具体数值**待本地验证**。对比 u5-l5 健康的 GAN（双方在 ln2 附近拉锯但样本质量持续提升），这里是「卡死在 ln2」——同为 0.693，含义截然不同。

#### 4.3.5 小练习与答案

**练习 1**：训练日志里损失约 0.69、准确率约 50%。请用 BCE 公式解释这两个数字的来源。

<details><summary>参考答案</summary>

当模型对每个样本都输出 \( p = 0.5 \)：\( \mathcal{L} = -[0.5\ln 0.5 + 0.5\ln 0.5] = \ln 2 \approx 0.693 \)；且无论标签是 0 还是 1，`sigmoid(logit) > 0.5` 的判定只由 logit 的符号微弱倾向决定，约一半样本会判错——准确率 50%。损失贴 0.69 + 准确率贴 50% 是「模型未学到任何东西」的标准指纹。
</details>

**练习 2**：本模型的 many-to-one 用 `hidden` 做句子摘要。假设改成「对 `output` 沿时间维取平均（mean pooling）」，相比取最后时间步，对 padding 的敏感性有何变化？

<details><summary>参考答案</summary>

取 `hidden` 时，`<pad>` 的影响是**劫持**式的：最后几百步全在处理填充符，真实内容被彻底冲刷。取 `output` 的时间平均时，`<pad>` 的影响是**稀释**式的：真实词的隐状态仍在平均值里占有一席，但会按填充长度被不断摊薄。两者都不如「跳过 padding 位置」理想——这正是 packed sequences（u6-l2）要解决的问题：让 RNN 在每个样本的真实最后一个词处停下。若做 mean pooling，更彻底的做法是配合 mask 只对有效位置求平均。
</details>

**练习 3**：`predict_sentiment` 里为什么要 `tensor.unsqueeze(1)` 而不是 `unsqueeze(0)`？

<details><summary>参考答案</summary>

模型前向的输入约定是 `[句长, 批大小]`（批在第 1 维）。`nlp.tokenizer(sentence)` 得到的索引序列是一维 `[句长]`，`unsqueeze(1)` 把它变成 `[句长, 1]`——句长在第 0 维、批大小为 1，与训练时形状约定一致。若 `unsqueeze(0)` 会得到 `[1, 句长]`，第 0 维的 1 被解释为「长度为 1 的句子、批大小 = 句长」，形状虽然能跑通（Embedding/RNN 不校验语义），但语义完全错误。
</details>

## 5. 综合实践

**任务：扫描「序列长度截断」与「Embedding 维度」两个旋钮，量化它们对验证准确率与训练时间的影响，整理成对比表。**

这个实验直接回应本讲两大痛点：截断序列同时缓解「梯度流不过长序列」和「padding 拖慢计算」（作者注明 packed 版本比本版快约 4 倍，截断是更粗暴的同类手段）；Embedding 占了 98% 的参数，它的维度是性价比最高的容量旋钮。

**环境准备（诚实前置条件）**：本 notebook 依赖 torchtext 旧式 API，需要 Python 3.7 + torch 1.0 + 旧版 torchtext（0.4 前后）+ spacy 旧名 `en` 模型（`python -m spacy download en`）。若无法搭建旧环境，可按 u6-l3 `rnn_lstm_packed_own_csv_imdb.ipynb` 的自有 CSV 模板用现代 API 重写数据管线，模型与训练代码本讲已全部讲透；以下所有数值结论**待本地验证**。

操作步骤：

1. **基线复跑**：按原 notebook（无截断，`EMBEDDING_DIM=64`、`HIDDEN_DIM=128`）训练 15 个 epoch，记录总训练时间、最终验证准确率、测试准确率（参照作者输出：6.35 min / 50.68% / 46.38%）。
2. **改截断长度**：把 4.1 节的字段定义改为 `data.Field(tokenize='spacy', fix_length=N)`（**示例代码**；`fix_length` 让每条序列精确补/截到 N 个 token），分别取 `N ∈ {100, 200, 400}`。其余一切不动。
3. **改 Embedding 维度**：在**建模 cell（L364-L372）**里把 `EMBEDDING_DIM` 改为 32 / 128（切记别只改 General Settings——见 4.2.3 的覆盖陷阱），配合表现最好的截断长度各跑一次。
4. **整理对比表**（模板）：

| 截断 N | EMBEDDING_DIM | 每 epoch 耗时 | 总训练时间 | 最终验证准确率 | 测试准确率 |
| --- | --- | --- | --- | --- | --- |
| 无（基线） | 64 | | | | |
| 100 | 64 | | | | |
| 200 | 64 | | | | |
| 400 | 64 | | | | |
| 最优 N | 32 | | | | |
| 最优 N | 128 | | | | |

需要观察的现象与预期方向：

- **训练时间**应随 N 近似线性缩短（RNN 时间步数 = 序列长度）；无截断基线在含 880 词长句的批上最慢。
- **准确率**可能不升反不升——如果梯度消失是主因，截断到 100–200 或许让模型终于学到一点东西（准确率显著偏离 50%）；但也可能因丢掉评论后半段信息而无改善。**无论哪种结果，都比「只跑一组就下结论」有价值**：它能告诉你失败的主因究竟是「序列太长」还是「模型太弱」。
- 所有具体数值均**待本地验证**；若截断后损失开始跌破 0.65 并持续下降，说明实验切中了要害。

## 6. 本讲小结

- **文本管线三步**：分词（spacy）→ 只用训练集建词表（top-20000 词 + `<unk>`/`<pad>` = 20002 项）→ `nn.Embedding(20002, 64)` 查表把 `[句长, 批]` 的索引变成 `[句长, 批, 64]` 的向量；torchtext 的批**在第 1 维**，与 CNN 管线相反。
- **`nn.RNN` 的两件产出**：`output [T, B, H]` 是逐时间步的隐状态流水账，`hidden [1, B, H]` 是读完整句的总结；单层单向时 `output[-1]` 与 `hidden.squeeze(0)` 相同。参数量（24,832）只由嵌入维与隐维决定，与句长无关。
- **many-to-one**：取 `hidden` 过单个 `Linear(128, 1)` 输出 logit，配 sigmoid + `binary_cross_entropy_with_logits`——结构上就是「序列版逻辑回归」；训练循环仍是五步范式，一处未改。
- **读形状要认维度、读变量要认来源**：`[880, 128]` 的批第一维是句长；建模 cell 里的 `EMBEDDING_DIM=64` 覆盖了 General Settings 里的 128，实际生效的是 64/128/1。
- **损失贴 \( \ln 2 \approx 0.693 \) + 准确率贴 50% = 模型在抛硬币**。本 notebook 是仓库里少见的「保留失败输出」的教学样本：简单 tanh RNN 的梯度流不过几百词的长评论，取最后时间步又被批内 `<pad>` 冲刷，15 个 epoch 后测试准确率仅 46.38%。
- 修复路线已经写好：**packed sequences** 消灭 padding 的空转与稀释（且快约 4 倍），**LSTM/GRU** 的门控缓解梯度消失——两者都是下一讲的主角。

## 7. 下一步学习建议

下一讲 **u6-l2（LSTM、GRU 与 packed sequences）**将正面解决本讲留下的两个问题，主读文件是同目录的 `rnn_simple_packed_imdb.ipynb`（注意：notebook 内文的链接写作 `rnn-simple-packed-imdb.ipynb`，磁盘上的实际文件名是下划线版本，以 `git ls-files` 或目录列表为准）。建议先自行对比思考：

1. `pack_padded_sequence` 如何让 RNN「在每个样本的真实最后一个词处停下」，从而同时提速与保住摘要质量？
2. LSTM 的输入门/遗忘门/输出门如何给长程梯度修一条「高速公路」？

之后的路线：u6-l3 讲双向多层 RNN、GloVe 预训练词向量与自有 CSV 数据管线（现代 torchtext 环境的可行替代），u6-l4 讲字符级 char-RNN 文本生成，u6-l5 讲 TextCNN，u6-l6 讲 DistilBERT 微调。若想回头补基础，u4-l1（DataLoader 与采样器）能帮你把 torchtext 的 iterator 与主库的 DataLoader 概念对齐。
