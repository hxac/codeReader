# u6-l3 双向多层 RNN、GloVe 与自有 CSV 数据

## 1. 本讲目标

上一讲（u6-l2）我们用 packed sequences 修复了简单 RNN 的失败，并确认 LSTM/GRU 在 IMDb 上能到 85% 左右。本讲沿着三个方向把这套管线「升级到能用在自己的项目里」：

1. **双向与多层 RNN**：理解 `bidirectional=True` 与 `num_layers=2` 之后，`hidden` 张量的形状与内部布局如何变化，为什么分类头要接 `torch.cat((hidden[-2], hidden[-1]), dim=1)`。
2. **GloVe 预训练词向量**：理解「下载预训练向量」和「真正用上预训练向量」是两件事——本讲会用源码证明仓库里名为 GloVe 的 notebook 实际只做了前者，并给出补全后者的写法。
3. **自有 CSV 数据管线**：不再依赖 `datasets.IMDB` 一行式加载，而是用 pandas 预检 + `TabularDataset` 把任意两列（文本列、标签列）的 CSV 接入同一套「Field → build_vocab → BucketIterator → LSTM」管线，最终在 AG News 四分类任务上完整落地一个双向两层 LSTM。

读完本讲，你应该能独立回答三个问题：双向多层的 `hidden` 里下标 `-2` 和 `-1` 分别是什么？预训练向量要满足什么条件才能装进 `nn.Embedding`？换用自己的 CSV 后，哪几个坑（比例顺序、标签编码方向、`sort_key`）必须亲自验证？

## 2. 前置知识

### 2.1 承接前两讲的「地基」

本讲不重复推导，直接复用 u6-l1 与 u6-l2 的结论，不熟悉的读者请先回看：

- **文本数值化**：分词 → 仅用训练集按词频建词表（`<unk>`/`<pad>` 两个特殊词元，所以词表大小是 `max_size + 2`）→ `Embedding` 查表。
- **many-to-one 分类**：句长优先 `[L, B, E]` 输入，LSTM 返回 `(hidden, cell)`，分类头只吃最终隐状态。
- **打包序列**：`include_lengths=True` + `sort_within_batch=True` + `pack_padded_sequence`，让循环核跳过 padding。
- **五步训练循环**：forward → loss → `zero_grad` → `backward` → `step`。

### 2.2 双向与多层到底在做什么

- **单向 RNN 的盲区**：读句子时只用「上文」。但情感判断常需要「下文」——「this movie is not good」里决定性信息出现在 good 之后。**双向 RNN（bidirectional RNN）**用两条独立的循环核各读一遍：正向从第 1 词读到第 T 词，反向从第 T 词读到第 1 词，每个时间步把两个方向的隐状态拼接起来用。
- **多层（stacked / deep RNN）**：把第 1 层每个时间步的输出序列当作第 2 层的输入序列，层层抽象，类似 CNN 里堆卷积块。`nn.LSTM(dropout=...)` 会在**层与层之间**（最后一层除外）插入 Dropout。
- **代价**：参数量随「方向数 × 层数」增长。单个方向、单层的 LSTM 参数量是

  \[ 4(H_{in}H + H^2 + 2H) \]

  其中 \(H_{in}\) 是输入维（第一层是嵌入维，双向堆叠时第二层起是 \(2H\)，因为两个方向的输出要拼起来喂给下一层）、\(H\) 是隐藏维，系数 4 来自 LSTM 的四道门（遗忘、输入、输出、候选记忆），\(2H\) 是两个偏置向量。

### 2.3 GloVe 是什么

`Embedding` 层默认随机初始化，相当于每个词的向量都从零学起。**GloVe**（Global Vectors，Pennington et al., 2014）是在几十亿词的语料上、基于全局共现统计预先训练好的词向量表：语义相近的词（cat/kitten）向量距离近，还能做 `king - man + woman ≈ queen` 这类线性类比。把这种向量当作自己任务嵌入层的**初始值**，相当于站在大规模语料的肩膀上——词汇多的任务尤其受益（自己任务里只出现几次的词，也能带上它在通用语料里学到的语义）。

使用预训练向量要检查三件事：

1. **维度匹配**：`glove.6B.100d` 是 100 维向量，`nn.Embedding(input_dim, embedding_dim)` 的 `embedding_dim` 必须等于 100，否则装不进去；
2. **索引对齐**：词表里第 i 个词的向量要拷贝到 `embedding.weight` 的第 i 行（`TEXT.vocab.vectors` 已经按词表排好）；
3. **未登录词**：词表里有、GloVe 里没有的词（包括 `<unk>`、`<pad>`）需要初始化策略，`unk_init=torch.Tensor.normal_` 表示用正态分布随机向量填充。

### 2.4 版本注意事项

本讲的 notebook 是 torch 1.0–1.4 年代的产物：`from torchtext import data` 这套 API 在新版 torchtext 中先移入 `torchtext.legacy`（0.9–0.11），0.12 之后彻底移除（torchtext 项目本身也已停止维护）。复现有两条路：装旧环境（如 `torchtext==0.4.0`，AG News notebook 末尾的 [`%watermark -iv`](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L2095) 记录了作者环境），或者把 `Field/TabularDataset/BucketIterator` 换成自建 `Dataset` + `DataLoader`（u4-l2 的三要素模板完全够用）。本讲按仓库原代码讲解，因为**数据管线的思想（字段映射、长度排序、比例划分）与框架无关**。

## 3. 本讲源码地图

本讲四个 notebook 全部位于 `pytorch_ipynb/rnn/`，都是 u6-l2 那套模板的变体——五段式结构、手写训练循环、不使用共享 helper 模块：

| Notebook | 角色 | 与 u6-l2 基线的差异 |
|---|---|---|
| [rnn_lstm_bi_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb) | **双向多层最小版**（模块 4.1） | `num_layers=2`、`bidirectional=True`，拼接两方向隐状态 |
| [rnn_lstm_packed_imdb-glove.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb) | **GloVe 下载版**（模块 4.2） | 仅在 `build_vocab` 多了 `vectors='glove.6B.100d'`，模型一字未改 |
| [rnn_lstm_packed_own_csv_imdb.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb) | **自有 CSV 模板**（模块 4.3） | 数据改从 CSV 文件加载（`TabularDataset`），模型与 u6-l2 相同 |
| [rnn_bi_multilayer_lstm_own_csv_agnews.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb) | **三者合体的完整版**（4.1 + 4.2 + 4.3） | 自有 CSV + 双向两层 + Dropout + `padding_idx` + 四分类，AG News 数据 |

一句话导览：前三个是「只改一处」的受控对比，最后一个是把三项改动全部合起来、再加工程细节（Dropout、`padding_idx`、两层分类头）的完整工程实现。链接中的行号指向 .ipynb 的原始 JSON 行，可点击定位。

## 4. 核心概念与源码讲解

### 4.1 双向多层 RNN：hidden 张量的布局与拼接

#### 4.1.1 概念说明

`nn.LSTM` 的 `hidden`（即文档里的 `h_n`）形状是

\[ [\,\text{num\_layers} \times \text{num\_directions},\; B,\; H\,] \]

布局规则是**「逐层、每层先正向后反向」**。以 `num_layers=2`、`bidirectional=True` 为例，4 行依次是：

```
hidden[0]  ← 第 1 层 正向 在每个样本真实末词处的隐状态
hidden[1]  ← 第 1 层 反向 在序列起点处的隐状态
hidden[2]  ← 第 2 层 正向 最终隐状态   ← hidden[-2]
hidden[3]  ← 第 2 层 反向 最终隐状态   ← hidden[-1]
```

做句子分类时，我们只需要**最顶层的两个方向**：正向读到句尾、反向也读到「句尾」（反向的最后一个时间步是原句的第 1 词），把两者拼成 \(2H\) 维特征喂给分类头。这就是 `torch.cat((hidden[-2,:,:], hidden[-1,:,:]), dim=1)` 的含义——它同时回答了两个问题：

- 为什么不直接 `hidden.squeeze(0)`？单向单层时可以（u6-l2 就是这么写的，因为那时 `hidden` 只有 1 行）；双向多层时 squeeze 会把 4 行压成 \([4B, H]\)，完全错位。
- 为什么不用全部 4 行？可以（把所有层的隐状态都拼起来），但更常见的是只用顶层，本讲两份 notebook 都是后者。

还要指出一个容易忽略的细节：**打包序列与双向是兼容的**。打包后每条序列的「最终隐状态」恰好产生于它真实最后一个词处（正向）或真实第一个词处（反向），不会读 padding——u6-l2 讲过的打包收益在双向设置下原样成立。

#### 4.1.2 核心流程

`rnn_lstm_bi_imdb.ipynb` 的前向流程：

```
输入 text [L, B]（已按长度降序排好）
  │ embedding                    → [L, B, 128]
  │ pack_padded_sequence(+长度)   → PackedSequence
  │ nn.LSTM(H=128, num_layers=2, bidirectional=True)
  │     ├── packed_output        （本讲不用）
  │     └── (hidden [4, B, 128], cell [4, B, 128])
  │ cat(hidden[-2], hidden[-1])  → [B, 256]
  └─ Linear(256 → 1) → logit [B] →  sigmoid → 正类概率
```

对照 u6-l2（1 层单向、H=256）：`hidden [1, B, 256]` → `Linear(256 → 1)`。可见**分类头输入维必须跟着方向数走**：单向是 \(H\)，双向是 \(2H\)。

#### 4.1.3 源码精读

**（1）开关与超参**。[rnn_lstm_bi_imdb.ipynb:L88-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L88-L101) 在通用设置之外新增两个开关：[L96](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L96) 的 `BIDIRECTIONAL = True` 与 [L99](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L99) 的 `NUM_LAYERS = 2`；同时注意 [L100](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L100) 把 `HIDDEN_DIM` 定成了 128——而 u6-l2 的 LSTM 版是 256。数据管线部分与 u6-l2 完全一致：[L148-L152](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L148-L152) 声明 `include_lengths=True` 的 Field，[L230-L234](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L230-L234) 打开 `sort_within_batch=True`，词表同样是 20002。

**（2）模型类的三处改动**。[rnn_lstm_bi_imdb.ipynb:L318-L344](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L318-L344) 定义 `RNN` 类，与 u6-l2 的版本相比恰好三处不同：

```python
self.rnn = nn.LSTM(embedding_dim, hidden_dim,
                   num_layers=NUM_LAYERS,        # 2 层
                   bidirectional=BIDIRECTIONAL)  # 双向
self.fc = nn.Linear(hidden_dim*2, output_dim)    # 输入维翻倍
...
combined = torch.cat((hidden[-2,:,:], hidden[-1,:,:]), dim=1)
return self.fc(combined.squeeze(0)).view(-1)
```

- [L326-L329](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L326-L329)：把层数与方向作为参数传给 `nn.LSTM`。注意这里 `NUM_LAYERS`、`BIDIRECTIONAL` 是直接读模块级全局变量，而不是像 `hidden_dim` 那样从构造参数传入——功能正确，但类的复用性差（后面的 AG News 版本修正了这一点）。
- [L330](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L330)：`nn.Linear(hidden_dim*2, output_dim)`，对应拼接后的 \(2H = 256\) 维输入。
- [L337-L339](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L337-L339)：照旧打包并解包 `(hidden, cell)`；[L341-L342](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L341-L342) 拼接顶层两个方向（注释原文 `# combine both directions`），[L344](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L344) 过全连接输出 logit。训练循环与 `compute_binary_accuracy`（[L385 起](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L385)）与 u6-l2 逐字相同，五步范式不变。

**（3）保存结果与一个方法论警示**。notebook 保存输出显示 15 个 epoch 共 5.06 min、**测试准确率 83.21%**（[L520-L521](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L520-L521)）。对比 u6-l2 的 1 层单向 LSTM（H=256，85.08%），**双向两层反而低了约 2 个百分点**。能不能得出「双向无用」的结论？不能——这次对比同时改了三处：方向、层数、隐藏维（256→128），是混淆变量的对比，不是 u2-l4 以来我们强调的「只改一处」。它真正教给我们的是：

| 版本 | 层数/方向 | HIDDEN_DIM | 测试准确率 | 依据 |
|---|---|---|---|---|
| u6-l2 LSTM 打包版 | 1 / 单向 | 256 | 85.08% | u6-l2 notebook 保存输出 |
| 本讲双向版 | 2 / 双向 | 128 | 83.21% | [L521](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L521) |
| 本讲 GloVe 版 | 1 / 单向 | 256 | 84.60% | 4.2 节 |

（RNN 训练方差大，u6-l2 已见过单 epoch 验证准确率暴跌 9 个点的波动；几个百分点的差距须做多种子实验再下结论。）另外，推断函数 [L575-L588](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L575-L588) 对任意单句做「分词 → 查词表 → 补 batch 维 → sigmoid」，对 "I really love this movie..." 给出 0.939 的正类概率——双向模型确实学到了可用的语义。

**（4）工程完整版：AG News 的模型**。[rnn_bi_multilayer_lstm_own_csv_agnews.ipynb:L754-L782](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L754-L782) 是同一思想的完整工程版，值得精读的细节有五个：

```python
self.embedding = nn.Embedding(input_dim, embedding_dim, padding_idx=pad_idx)
self.rnn = nn.LSTM(embedding_dim, hidden_dim,
                   num_layers=num_layers, bidirectional=bidirectional,
                   dropout=dropout)
self.fc1 = nn.Linear(hidden_dim * num_layers, 64)
self.fc2 = nn.Linear(64, output_dim)
```

- [L762](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L762) `padding_idx=pad_idx`：`<pad>` 对应的嵌入向量固定为零向量且不接收梯度更新（[L797](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L797) 用 `PAD_IDX = TEXT.vocab.stoi[TEXT.pad_token]` 取它的行号传入）。
- [L763-L767](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L763-L767)：层数、方向、层间 Dropout 都改成构造参数（修掉了 bi 版读全局变量的问题）；`dropout=0.5` 只在 `num_layers > 1` 时生效。
- [L768-L769](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L768-L769)：两层分类头 `512 → 64 → 4`。注意 `hidden_dim * num_layers = 256*2 = 512` 与拼接维 \(2H\) 相等**纯属 num_layers=2 恰好等于方向数 2**——若把 `num_layers` 改成 3（保持双向），拼接仍是 512 而 `Linear` 期望 768，前向传播立即形状报错。更稳的写法是 `hidden_dim * 2`（双向时）。这类「公式恰好成立」的脆弱性，读别人代码时要专门检查。
- [L774-L776](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L774-L776)：嵌入后、循环前先 Dropout；[L778-L781](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L778-L781)：拼接后的隐状态与两层全连接之间也各有一次 Dropout——`NUM_EPOCHS=50`（[L115](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L115)）的长训练配强正则。

#### 4.1.4 代码实践：亲手摸一次 hidden 的形状

1. **实践目标**：用最小实验验证 4.1.1 的布局论断——「`hidden` 是 `[4, B, 128]`，`hidden[-2]`/`hidden[-1]` 是顶层正/反向」。
2. **操作步骤**（示例代码，待本地验证；需要在装好 torch 1.x + torchtext 0.4 的环境中运行 rnn_lstm_bi_imdb 的数据单元之后执行）：

   ```python
   # 示例代码：在 notebook 里模型定义之后追加
   import torch.nn as nn
   probe = nn.LSTM(128, 128, num_layers=2, bidirectional=True)
   x = torch.randn(10, 4, 128)                 # [L=10, B=4, E=128]
   out, (h, c) = probe(x)
   print(h.shape)   # 预期 torch.Size([4, 4, 128]) = [层数×方向数, B, H]
   print(out.shape) # 预期 torch.Size([10, 4, 256])，双向输出按方向维拼接
   ```

   再把 `rnn_lstm_bi_imdb.ipynb` [L88-L101](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_bi_imdb.ipynb#L88-L101) 里的 `BIDIRECTIONAL` 改成 `False` 后从头重跑模型单元。
3. **需要观察的现象**：`h.shape` 的第一维从 4 变 2；且不改 `self.fc` 的情况下，训练第一个 batch 就会抛出 `RuntimeError: mat1 and mat2 shapes cannot be multiplied`（`Linear(256,1)` 收到 `[B,128]` 输入）。
4. **预期结果**：形状变化与报错都印证「分类头输入维 = \(H \times\) 方向数」。把 `self.fc` 同步改为 `nn.Linear(hidden_dim, output_dim)` 并把 forward 的拼接改为 `hidden[-1]`（单向时顶层唯一），即可恢复正常训练。
5. 以上运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`num_layers=3`、`bidirectional=True` 时，`hidden` 有几行？`hidden[-2]` 是什么？
答案：6 行（3 层 × 2 方向，布局为「第1层正、第1层反、第2层正、第2层反、第3层正、第3层反」）。`hidden[-2]` 是第 3 层（顶层）正向的最终隐状态，`hidden[-1]` 是顶层反向的——与层数无关，顶层永远是最后两行。

**练习 2**：用 4.1.1 的参数公式估算 `rnn_lstm_bi_imdb` 的 LSTM 参数量（E=128、H=128、2 层双向），并说明它相对 u6-l2 单层单向版（H=256）增长了多少。
答案：第 1 层每方向 \(4(128\cdot128+128^2+2\cdot128)=132{,}096\)，两方向 264,192；第 2 层每方向输入是 \(2H=256\)，每方向 \(4(256\cdot128+128^2+2\cdot128)=197{,}632\)，两方向 395,264；合计约 659K。u6-l2 单层单向为 \(4(128\cdot256+256^2+2\cdot256)\approx395\text{K}\)，本讲约 1.67 倍。嵌入层（20002×128≈2.56M）仍占大头。

**练习 3**：双向 RNN 能不能用于「预测下一个词」的语言模型（如 u6-l4 的 char-RNN）？
答案：不能。预测第 t 个词只能用前 t−1 个词的信息，而反向通道读了「未来」，训练时等于作弊、推理时未来根本不存在。双向只适合**整句可见**的任务（分类、匹配、序列标注），这是选型时第一道判断。

### 4.2 GloVe 预训练向量：下载了 ≠ 用上了

#### 4.2.1 概念说明

用预训练向量初始化嵌入层，标准动作分两步：

1. `build_vocab(..., vectors='glove.6B.100d', unk_init=...)`：torchtext 建词表的同时下载/读取 GloVe，把词表里每个词对应的 100 维向量对齐排好，存进 `TEXT.vocab.vectors`（形状 `[词表大小, 100]`）；词表里有而 GloVe 里没有的词按 `unk_init` 给出的方式初始化（本 notebook 用标准正态随机向量）。
2. 把 `TEXT.vocab.vectors` **拷贝进模型**：`model.embedding.weight.data.copy_(TEXT.vocab.vectors)`，通常再把 `embedding.weight.requires_grad = False`（冻结）或保持可训练（微调），二选一做对照。

本节的源码侦查会证明：仓库里这份名为 GloVe 的 notebook 只做了第 1 步，第 2 步从未发生——嵌入层仍是随机初始化、从零训练。这不是「错误结论」的素材，而是本手册反复强调的方法论（u3-l2「读源码应以代码与变量来源为准」）的又一实例：**notebook 的标题和描述文字承诺的东西，代码不一定兑现**。

#### 4.2.2 核心流程

```
build_vocab(vectors='glove.6B.100d', unk_init=normal_)
  ├── 词表照常构建（20002 项）
  ├── 下载 GloVe 到 .vector_cache/（首次运行，文件较大）
  └── TEXT.vocab.vectors ← [20002, 100] 的对齐矩阵
模型侧（本 notebook 的实际行为）
  └── nn.Embedding(20002, 128) 随机初始化 → 全程自己学   ← 断点在此
模型侧（应有的补全，示例代码）
  ├── EMBEDDING_DIM 改为 100
  ├── embedding.weight.data.copy_(TEXT.vocab.vectors)
  └── 可选：requires_grad = False 冻结
```

#### 4.2.3 源码精读

**（1）唯一的新增行**。[rnn_lstm_packed_imdb-glove.ipynb:L196-L199](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb#L196-L199) 的 `build_vocab` 比 u6-l2 版本多了两个参数：`vectors='glove.6B.100d'`（60 亿词元语料、100 维）与 `unk_init=torch.Tensor.normal_`。输出显示词表仍是 20002——预训练向量不改变词表本身，只是给每个词附带一行向量。

**（2）证据链：向量从未进入模型**。三条相互印证的证据：

- **全文检索**：整份 notebook 中 `TEXT.vocab.vectors`、`copy_`、`requires_grad` 一次都没有出现；`model = RNN(...)` 之后（[L360-L365](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb#L360-L365)）到训练循环开始之间没有任何对嵌入层权重的操作。
- **维度对不上**：[L102-L103](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb#L102-L103) 设 `EMBEDDING_DIM = 128`，而 GloVe 6B.100d 是 100 维——即使想 `copy_` 也装不进去，说明作者压根没打算装。
- **模型类逐字相同**：[L324-L347](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb#L324-L347) 的 `RNN` 与 u6-l2 的 `rnn_lstm_packed_imdb.ipynb` 完全一样（1 层单向 LSTM、`fc(hidden.squeeze(0))`），没有为向量做任何适配。

**（3）实验结果的旁证**。保存输出显示 2.45 min、测试准确率 **84.60%**（[L523-L524](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_imdb-glove.ipynb#L523-L524)），与 u6-l2 那份**没有下载任何向量**的 LSTM 版（85.08%）在 RNN 训练噪声范围内基本一致——如果预训练向量真的参与了训练，在 2 万条训练数据的小语料上通常能看到更明显的收益或至少明显不同的训练曲线。这与「向量没被用上」的代码证据互相印证。AG News 的 [L604-L607](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L604-L607) 同样只在 `build_vocab` 下载了 `glove.6B.100d`（`EMBEDDING_DIM=128`，模型里同样没有 `copy_`）——这是仓库的普遍模式，不是孤例。

**（4）补全第 2 步（示例代码）**。若要真正用上 GloVe，改法如下（**示例代码**，仓库中不存在；待本地验证）：

```python
# 1) 嵌入维必须与预训练向量一致
EMBEDDING_DIM = 100

# 2) 建模后、训练前，把向量拷进嵌入层
model.embedding.weight.data.copy_(TEXT.vocab.vectors)

# 3) 可选一：冻结（嵌入层不更新，省显存、防小数据过拟合）
model.embedding.weight.requires_grad = False
# 可选二：不冻结（微调，通常配更小的学习率）

# 4) 常见补充：pad 向量清零（若未用 padding_idx）
model.embedding.weight.data[PAD_IDX] = torch.zeros(EMBEDDING_DIM)
```

#### 4.2.4 代码实践：三臂对照实验

1. **实践目标**：量化「GloVe 到底带来什么」，同时补上仓库缺失的第 2 步。
2. **操作步骤**：以 `rnn_lstm_packed_imdb-glove.ipynb` 为基线做三个臂——(a) 原样跑（随机嵌入，等价于复现 84.60%）；(b) 按 4.2.3(4) 的示例代码装载 GloVe 并**冻结**；(c) 装载但**不冻结**（微调）。三臂其余超参完全一致，种子固定为 123。
3. **需要观察的现象**：训练前 1～2 个 epoch 的验证准确率——冻结/微调臂应该**起步明显更快**（初始嵌入已含语义），这正是预训练的价值窗口；15 个 epoch 后三臂差距可能收窄（2 万条影评足够把高频词学出来）。
4. **预期结果**：记录三臂的「第 1 epoch 验证准确率」与「最终测试准确率」两张表。若 (b)(c) 起步不快于 (a)，先检查 `copy_` 是否真的执行（打印 `model.embedding.weight[3]` 与 `TEXT.vocab.vectors[3]` 是否一致）。
5. 运行结果**待本地验证**。首次运行会向 `.vector_cache/` 下载较大的 GloVe 压缩包。

#### 4.2.5 小练习与答案

**练习 1**：`unk_init=torch.Tensor.normal_` 会被用在哪些词上？
答案：词表中出现、但 GloVe 向量文件里查不到的词——包括 `<unk>` 和 `<pad>` 两个特殊词元（GloVe 没有它们），以及拼写生僻、在 GloVe 的 40 万词表之外的词。这就是为什么装载后通常还要手动把 `<pad>` 的向量清零。

**练习 2**：词表大小 20002、GloVe 100 维、隐藏维 256 的单层 LSTM。冻结嵌入层能省掉多少可训练参数？
答案：嵌入层有 20002×100 = 2,000,200 个参数，冻结后可训练参数只剩 LSTM（约 4(100·256+256²+2·256)≈385K）加分类头（257）≈ 385K 左右——可训练量降到约 1/6，小数据集上还能显著缓解嵌入层过拟合。

**练习 3**：为什么 `model.embedding.weight.requires_grad = False` 之后，模型照样能「使用」这些向量？
答案：`requires_grad` 只控制**梯度是否回传/参数是否更新**，不影响前向传播查表。冻结的是「学习」，不是「使用」；被冻结的张量也不再有梯度存储开销（除非输入需要对其求梯度）。

### 4.3 自有 CSV 数据管线：TabularDataset 与字段改名

#### 4.3.1 概念说明

`datasets.IMDB.splits(TEXT, LABEL)` 一行搞定数据，但真实项目的数据往往是「一个 CSV、两三列」。接管自有数据需要把管线的**源头**换掉，中下游（`build_vocab`、`BucketIterator`、模型、训练循环）原样复用。关键概念：

- **TabularDataset + fields 映射**：`fields = [('review', TEXT), ('sentiment', LABEL)]` 把 CSV 的列名与 Field 一一对应；torchtext 会把列名变成**样本对象的属性名**——于是 batch 里的字段从 `batch.text`/`batch.label` 变成 `batch.review`/`batch.sentiment`（或 AG News 的 `batch.content`/`batch.classlabel`）。列名 → 属性名，这是本节最重要的机械规则。
- **`sort_key` 必须手动给**：u6-l1 讲过 `BucketIterator` 靠「例子的长度」把相近长度的句子装进同一 batch；`datasets.IMDB` 自带默认的 `sort_key`，而 `TabularDataset` 不知道你的文本在哪一列，必须显式传 `sort_key=lambda x: len(x.review)`（AG News 版是 `len(x.content)`）。
- **`skip_header=True`**：CSV 第一行是表头时要跳过；用 pandas 预处理后写出的文件带表头，所以两个 notebook 都打开了它。
- **pandas 预检的意义**：接入前先 `read_csv` + `head()` + 检查类别分布（`np.bincount`），与 u4-l2「先判断数据是否按标签排序」是同一习惯——AG News 原始标签是 1～4，`df['classlabel']-1` 改成 0～3，正是交叉熵对类别索引的要求。
- **两个「必须打印验证」的坑**：
  1. **比例顺序**：`dataset.split(split_ratio=[0.75, 0.05, 0.2])` 的三个数在本版 torchtext 中并不按「训练/验证/测试」的直觉顺序生效（保存输出显示 0.05 落到了测试集、0.2 落到了验证集），务必打印各划分规模；
  2. **标签编码方向**：`LabelField` 会给标签重新编号，编号与 CSV 里的语义标签（如 0=负面）**不保证同序**，推断时要用一两条已知情感的句子校准方向（下面源码里正好有一个现成的例子）。

#### 4.3.2 核心流程

```
movie_data.csv（50000 行：review, sentiment）
  │ wget 下载 + gunzip 解压                          （网络与解压）
  │ pd.read_csv → df.head() 预检 → del df            （确认列名与内容）
  │ TEXT/LabelField 声明                              （同 u6-l2）
  │ fields=[('review',TEXT),('sentiment',LABEL)]
  │ data.TabularDataset(path, 'csv', skip_header=True, fields)
  │ dataset.split(split_ratio=[...]) → train/valid/test   ← 打印规模验证
  │ TEXT.build_vocab(train_data, max_size=...)            ← 只用训练集
  │ BucketIterator(sort_within_batch=True, sort_key=len(x.review))
  └→ batch.review / batch.sentiment → 模型/训练循环（与 u6-l2 相同）
```

AG News 版多了「下载官方 tar 包 → pandas 清洗 → 写出两个预处理 CSV」的离线步骤，且训练/测试各自一个 `TabularDataset`，再从训练集中切验证集。

#### 4.3.3 源码精读

**（1） IMDb-CSV 模板**。[rnn_lstm_packed_own_csv_imdb.ipynb:L149](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L149) 用 `wget` 下载作者另一本书仓库里的 `movie_data.csv.gz`（25MB），[L158](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L158) 解压；[L243-L244](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L243-L244) `pd.read_csv` + `df.head()` 预检：两列 `review` / `sentiment`（0/1），检查完 `del df` 释放内存。

- [L272-L275](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L272-L275)：Field 声明与 u6-l2 相同（`tokenize='spacy'`、`include_lengths=True`），只是显式写了 `sequential=True`（默认即 True）。
- [L292-L297](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L292-L297)：**本讲最核心的六行**——`fields` 列表声明「列名 ↔ Field」映射，`data.TabularDataset(path="movie_data.csv", format='csv', skip_header=True, fields=fields)` 由此把 CSV 变成 torchtext 数据集。
- [L330-L336](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L330-L336)：`split_ratio=[0.75, 0.05, 0.2]` 三分。**看保存输出**（[L323-L325](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L323-L325)）：训练 37500、**验证 10000、测试 2500**——中间那个 0.05 实际成了测试集、末尾的 0.2 成了验证集。教训写在输出里：**换数据/换版本后，划分规模必须打印确认**。（顺带一提，测试集只有 2500 条，这也是它 86.88% 的准确率与 25000 条测试集的 u6-l2 不宜直接比较的原因之一。）
- [L429-L434](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L429-L434)：`BucketIterator.splits` 多了 `sort_key=lambda x: len(x.review)`——注意属性名用的是**列名** `review`。
- [L583-L592](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L583-L592) 与 [L759-L763](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L759-L763)：评估与训练循环里所有取数都变成 `batch_data.review` / `batch_data.sentiment`，其余五步范式与 u6-l2 逐字相同。模型（[L518-L542](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L518-L542)）也是 1 层单向 LSTM，一字未改——**换数据源不换模型与训练循环**，正是模板化的价值。
- 保存输出：15 epoch、4.34 min、**测试准确率 86.88%**（[L748](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L748)）。

**（2）标签方向的现成案例**。推断单元里，正面例句用的是 `1-predict_sentiment(...)`（[L850](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L850)，输出 0.826），负面例句直接用 `predict_sentiment(...)`（[L877](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L877)，输出 0.846）——同一个函数，方向却相反。原因即 4.3.1 所说：CSV 的 0/1 标签经 `LabelField` 重新编号后，sigmoid 输出的「正类」与 CSV 语义里的「正面」恰好反序（`LABEL.vocab.freqs` 显示两个类的计数几乎对半），作者用 `1-` 手工校准。相比之下，`datasets.IMDB` 版（4.1 节）直接 `predict_sentiment` 就是正面概率。**换数据源后，标签编码方向必须用已知样本校准**。

**（3） AG News 完整落地**。[rnn_bi_multilayer_lstm_own_csv_agnews.ipynb:L274-L277](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L274-L277) 的 pandas 预处理读入无表头的 `train.csv`，手工命名三列，`df['classlabel']-1` 把 1～4 的类别改为 0～3，`np.unique`/`np.bincount` 确认类别完整且均衡（训练集每类 30000、测试集每类 1900）；[L326](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L326) 只保留 `[classlabel, content]` 两列写出 `train_prepocessed.csv`（`title` 列被丢弃，新闻分类用正文即可），测试集同样处理（L417-L469 一带）。之后：

- [L524-L531](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L524-L531)：训练/测试**两个** `TabularDataset`，`fields = [('classlabel', LABEL), ('content', TEXT)]`——注意标签列在前，与 IMDb 模板顺序不同，`fields` 按列名而非位置对齐。
- [L562-L568](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L562-L568)：官方已有 train/test，只需二分 `split_ratio=[0.95, 0.05]` 切出 114000/6000 的验证集（二分时不存在 4.3.1 的顺序歧义）。
- [L665-L670](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L665-L670)：`sort_key=lambda x: len(x.content)`。
- [L825-L836](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L825-L836)：四分类的 `compute_accuracy` 用 `torch.max(logits, 1)` 取 argmax（不再是 `sigmoid > 0.5`）；[L1915-L1921](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L1915-L1921) 的训练循环把损失换成 `F.cross_entropy`（多分类标准损失，吃 logits），标签转 `.long()`——这是从二分类模板迁移到多分类时**必须改的三处**（损失、预测规则、类别数 `OUTPUT_DIM=4`，后者在 [L124](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L124)）。
- 推断侧 [L1975](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L1975) 定义 `{0: World, 1: Sports, 2: Business, 3: Sci/Tech}` 的映射字典，[L1983](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L1983) 的 `predict_class` 里 `min_len=4` 会把过短的句子补 `<pad>` 词元——这是 LSTM 无需但无害的保险（沿袭自 CNN 版模板）。
- 保存输出：50 个 epoch、26.11 min、验证 91.88%、**测试准确率 90.87%**（[L1906](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L1906)）。注意此处标签映射靠的是预处理时 `classlabel-1` + 写出文件的列顺序，编号 0～3 与四个主题的对应关系**完全由 pandas 预处理决定**——又一处「方向要自己锁死」的例证。

#### 4.3.4 代码实践：三行验证你的 CSV 能否接入

1. **实践目标**：在训练任何模型之前，用最小成本确认「列名、标签取值、类别分布」三件事，把 4.3.1 的坑消灭在接入前。
2. **操作步骤**（示例代码，可对任意自有 CSV 执行）：

   ```python
   # 示例代码：CSV 预检三件套（对应 agnews notebook 的 pandas 单元）
   import pandas as pd, numpy as np
   df = pd.read_csv('your_text.csv')
   print(df.columns.tolist())              # 1) 确认文本列/标签列名
   print(df['label'].unique())             # 2) 标签取值是否 0..K-1 且无 NaN
   print(np.bincount(df['label'].values))  # 3) 类别分布是否失衡
   df[['label', 'text']].head()            # 抽查内容与标签是否对得上
   ```

   若标签不是 0..K-1（如 AG News 的 1~4 或字符串），先仿照 [L276](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L276) 用 pandas 转换并另存新 CSV。
3. **需要观察的现象**：第 2 步输出是否恰好是 `[0 1 2 3]` 这类连续整数；第 3 步各类计数是否同量级。
4. **预期结果**：三项全部通过后，`fields` 映射 + `TabularDataset` 就能一步接入 4.3.2 的管线；任何一项异常（NaN、字符串标签、严重失衡）都应在 pandas 阶段解决，而不是丢给 torchtext。
5. 具体输出取决于你的数据，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 IMDb-CSV 版必须写 `sort_key=lambda x: len(x.review)`，而 u6-l2 的 `datasets.IMDB` 版不用写？
答案：`BucketIterator` 需要「例子的长度」来分桶，torchtext 的官方数据集类自带默认 `sort_key`（按文本长度），而 `TabularDataset` 是通用容器、不知道哪一列是文本，必须由用户显式告诉它。lambda 里的 `x.review` 就是 `fields` 映射出的样本属性（CSV 列名）。

**练习 2**：把 IMDb-CSV 模板迁移到 AG News（或任意 4 类数据），除数据预处理外必须修改哪几处？
答案：① `OUTPUT_DIM` 由 1 改 4；② 损失由 `binary_cross_entropy_with_logits` 改 `F.cross_entropy`（标签 `.long()`）；③ 预测由 `sigmoid>0.5` 改 `torch.max(logits, 1)`；④ `fields`/`sort_key`/batch 取数处的属性名换成自己的列名；⑤（若用本讲双层双向模型）`fc` 输入维按方向数调整。数据管线（TabularDataset → build_vocab → BucketIterator）与训练循环骨架不变。

**练习 3**：`dataset.split(split_ratio=[0.75, 0.05, 0.2])` 保存输出为 37500/10000/2500。若你希望「训练 37500、验证 2500、测试 10000」，应该怎么写、怎么确认？
答案：把三个数按「train, (这一版中落入测试的槽位), (落入验证的槽位)」调整为 `[0.75, 0.2, 0.05]`，并**务必**用 notebook 同款的三个 `print(f'Num ...: {len(...)}')`（[L334-L336](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_lstm_packed_own_csv_imdb.ipynb#L334-L336)）验证规模——比例语义随 torchtext 版本变化，打印结果才是唯一可靠的事实来源。

## 5. 综合实践

**任务：把双向两层 LSTM 完整迁移到你自己的 CSV 数据（或 AG News 子集）上。**这是对本讲三个模块的串联检验。

1. **准备数据**：任选一条路线——(a) 下载 AG News 的 `ag_news_csv.tar.gz` 并按 [L274-L326](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L274-L326) 的 pandas 流程预处理（标签 −1 变 0～3、只留两列）；(b) 自备一个两列 CSV（`text`, `label`），先跑 4.3.4 的预检三件套。数据量大时可抽样（如每类 5000 条）以控制训练时间。
2. **搭管线**：以 `rnn_lstm_packed_own_csv_imdb.ipynb` 为模板——`fields` 映射、`TabularDataset`、`split`（打印三个规模！）、`build_vocab`、`BucketIterator`（`sort_within_batch=True` + `sort_key=len(你的文本列)`）。
3. **换模型**：把模型类换成 [L754-L782](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb#L754-L782) 的双向两层版（含 `padding_idx` 与 Dropout），并把 `fc1` 的输入维改稳健：`nn.Linear(hidden_dim * 2, 64)`（想一想为什么不用 `hidden_dim * num_layers`，见 4.1.3(4)）。二分类沿用 `binary_cross_entropy_with_logits`，多分类改 `cross_entropy` + `torch.max`。
4. **训练与报告**：固定种子，训练若干 epoch（AG News 全量可对照 90.87%；子集版自选 epoch 数），每 epoch 打印训练/验证准确率；报告**测试准确率**、参数量统计（`sum(p.numel() for p in model.parameters())`，可分别统计嵌入层与 LSTM）、以及一条手写样例的预测结果（多分类配 `map_dictionary`，二分类先用已知情感句子校准标签方向）。
5. **加分项**：做 (a) 单向 vs 双向、(b) 1 层 vs 2 层 两组「只改一处」的受控对比，各跑 2 个种子，验证本讲 4.1.3 关于「混淆变量对比不可信」的告诫。

运行结果**待本地验证**；如遇 torchtext API 缺失，按 2.4 的建议换旧环境或以自建 Dataset 替代。

## 6. 本讲小结

- **双向多层 RNN**：`hidden` 形状为 `[num_layers × num_directions, B, H]`，布局逐层、每层先正后反；句子分类取顶层两方向拼接 `cat(hidden[-2], hidden[-1])`，分类头输入维由 \(H\) 变 \(2H\)。打包序列与双向完全兼容，最终隐状态仍落在真实词尾。仓库实证：双向两层（H=128，83.21%）不敌单层单向（H=256，85.08%），且该对比混淆了三个变量——「更花哨的结构 ≠ 更好的结果」，受控实验才是判断依据。
- **GloVe**：`build_vocab(vectors=...)` 只是**下载并缓存**向量（存于 `TEXT.vocab.vectors`）；真正使用还需维度一致（100d ↔ EMBEDDING_DIM=100）+ `copy_` 进嵌入层 + 冻结/微调二选一。仓库两份 notebook 都止步于第一步（维度 128 对不上、无 `copy_`、成绩≈随机嵌入基线）——「标题写了 GloVe」不等于「模型用了 GloVe」，以代码为准。
- **自有 CSV 管线**：pandas 预检 → `fields` 列名映射 → `TabularDataset(skip_header=True)` → `split` → `build_vocab` → `BucketIterator(sort_key=...)`。列名变成 batch 的属性名；`sort_key` 必须手写；`split_ratio` 的顺序语义与标签重编号方向都不能想当然，一律打印规模、用已知样本校准。
- **迁移到多分类**只需改三处：损失换 `cross_entropy`、预测换 `torch.max`、`OUTPUT_DIM` 设类别数；AG News 上双向两层 LSTM + Dropout + `padding_idx` 的完整配方拿到 90.87% 测试准确率。
- **工程细节**：`padding_idx` 让 `<pad>` 嵌入恒为零且不更新；`nn.LSTM(dropout=...)` 只作用于层间且需 `num_layers>1`；`fc1 = Linear(hidden_dim*num_layers, 64)` 的维度恰好正确依赖 `num_layers=2` 的巧合，通用写法应为 `hidden_dim*2`。

## 7. 下一步学习建议

本讲之后，RNN 部分还剩两条线：

1. **下一讲 u6-l4（字符级 RNN 文本生成）**：从 many-to-one 分类切换到 many-to-many 语言模型——每个时间步都要出预测，且按步截断计算图以处理长序列；届时你会看到本讲的「双向不可用」结论（练习 3）在生成任务中的直接体现。
2. **更远的 u6-l5（TextCNN）与 u6-l6（DistilBERT）**：TextCNN 用卷积替代循环，训练快一个量级；DistilBERT 则是「预训练 + 微调」路线的现代形态——对照本讲手工 `copy_` GloVe 的做法，你会看到 Hugging Face 时代如何把「下载预训练权重并真正装载」做成一行 `from_pretrained()`。

源码层面建议二刷 [rnn_bi_multilayer_lstm_own_csv_agnews.ipynb](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/rnn/rnn_bi_multilayer_lstm_own_csv_agnews.ipynb)：它一份 notebook 覆盖了本讲全部三个模块，且是 `pytorch_ipynb/rnn/` 目录里工程完成度最高的一份，适合作为你自己项目的起始模板。
