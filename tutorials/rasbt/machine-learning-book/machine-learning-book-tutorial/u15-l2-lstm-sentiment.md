# u15-l2 LSTM 情感分类：IMDb 数据管线与训练

## 1. 本讲目标

上一讲（u15-l1）我们用 `torch.matmul` 手算了 RNN 的隐状态递推，理解了 \(h_t = \tanh(W_{xh} x_t + W_{hh} h_{t-1} + b_h)\) 这条「记忆」传递链。本讲进入真实工程：用 PyTorch 完整搭建一条 **IMDb 影评情感分类** 流水线，把「一段长短不一的英文文本」变成「正面/负面」的判断。

学完本讲，你应该能够：

1. 独立完成 **分词 → 词频统计 → 词表 → 整数编码** 的文本预处理四步；
2. 讲清楚 `collate_batch` 如何用「pad + 长度记录」把变长评论拼成一个规整的批张量；
3. 读懂 `Embedding + LSTM + 全连接 + Sigmoid` 的模型前向，以及双向 LSTM 的改动点；
4. 手写 `train` / `evaluate` 两个函数，理解 `model.train()` 与 `model.eval()` + `torch.no_grad()` 的差别；
5. 理解 LSTM 的三个门控如何缓解朴素 RNN 的梯度消失。

> **一个重要的澄清**：本仓库 `ch15/ch15_part2.py` 中**没有** `nn.EmbeddingBag`，也没有 offset（偏移量）拼接，也没有对评论做截断。网上一些资料（以及 torchtext 官方教程）用「变长序列拼接成一维 + offset 标记每条起点 + EmbeddingBag 直接求均值」的路线处理变长文本，但**本书采用的是另一条路线**：`pad_sequence` 补齐 + `pack_padded_sequence` 跳过补齐位。本讲以仓库真实代码为准，第 4.3 节会对比这两条路线。

## 2. 前置知识

- **词元（token）与分词（tokenization）**：把字符串切成一个个最小语义单位（英语中通常按空格切分成单词）。第 8 章（u8-l1）的 `CountVectorizer` 内部也做分词，本讲我们手写分词器。
- **词表（vocabulary）与整数编码**：神经网络吃数字不吃字符串。词表就是「单词 → 整数 id」的字典。这与第 11 讲 `int_to_onehot` 之前的「整数标签」是同一思想：先建立离散符号到整数的映射。
- **嵌入层（Embedding）**：一个可查找的查找表，把整数 id 映射成一个稠密向量（本讲是 20 维）。它是「one-hot × 权重矩阵」的高效等价形式。第 4.3 节会结合代码复习。
- **padding（补齐）**：一个批里 32 条评论长度各异，张量必须是规则矩形，所以短评论要在末尾补占位符（id 0，即 `<pad>`）。第 12 讲（u12-l2）DataLoader 自动堆批的前提是样本同形，文本做不到，所以要自定义 `collate_fn`。
- **BCE 损失（二元交叉熵）**：u3-l1 讲过 log loss，u12-l3 讲过 sigmoid 配 `BCELoss`。本讲的模型输出经过 Sigmoid 得到概率 \(p\in(0,1)\)，用 BCE 训练。
- **LSTM（长短期记忆网络）**：u15-l1 结尾指出，朴素 RNN 的 \(\tanh\) 饱和导致长程梯度消失。LSTM 加入门控与细胞状态，让梯度有一条「高速公路」。概念细节见 4.3.1。
- **`model.train()` / `model.eval()`**：u13-l1（NoisyLinear）与 u14-l2 已见过——切换层的训练/评估行为。本讲中 LSTM 本身没有 dropout，但这个习惯贯穿所有训练代码。

## 3. 本讲源码地图

本讲几乎全部锚定在同一个文件上：

| 文件 | 作用 |
| --- | --- |
| [ch15/ch15_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py) | 第 15 章第 2 部分：IMDb 情感分类全流程——数据加载、分词、词表、collate_batch、三种 RNN 模型、训练/评估循环 |
| [ch15/ch15_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part1.py) | 上一讲已精读：RNN 手算示例与五种序列架构，本讲不再展开 |
| [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py) | u1-l2 讲过的版本体检脚本，`ch15_part2.py` 开头声明依赖 torch≥1.8.0、torchtext≥0.10.0 |

**环境提示（重要）**：本讲依赖 `torchtext`，代码按 0.10.0 编写（见 [ch15/ch15_part2.py:L33-L37](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L33-L37)，此处声明最低版本并交给 `check_packages` 检查）。torchtext 已于 2024 年发布最终版 0.18 后停止维护，新版中 `IMDB` 数据集的下载地址与 API 均有变动。强烈建议按 u1-l2 的方式用书中版本组合（Python 3.9 + torch 1.x + torchtext 0.10）建环境，或使用仓库提供的 Colab 指南运行 `ch15_part2.ipynb`。另外代码默认 `device = "cuda:0"`，无 GPU 时需要改成 `'cpu'`（文件中该行下方留有注释掉的 cpu 选项）。

## 4. 核心概念与源码讲解

### 4.1 tokenizer 与词表构建：把单词变成整数 id

#### 4.1.1 概念说明

神经网络的第一道门是把符号数字化。对文本来说分三步：

1. **分词**：字符串 → 词元列表；
2. **统计词频**：用 `Counter` 数出训练集中每个词出现的次数；
3. **建词表**：按词频从高到低排序编号（高频词 id 小），再补两个特殊记号 `<pad>`（id 0，补齐占位）和 `<unk>`（id 1，未登录词）。

为什么按词频排序？因为 id 本身没有语义，但让高频词排前面是个好习惯；更重要的是**词表只在训练集上统计**——与 u5-l1「主成分只能在训练集估计」同理，测试集里的生词一律按 `<unk>` 处理，防止信息泄漏的心态贯穿全书。

#### 4.1.2 核心流程

```text
IMDB(split='train')  ──遍历 20000 条训练评论──▶  Counter 统计词频
        │                                                │
        ▼                                                ▼
random_split 切出 20000 训练 / 5000 验证        按频次降序排成 OrderedDict
                                                         │
                                                         ▼
                                          vocab(ordered_dict) 建词表
                                          insert_token("<pad>", 0)
                                          insert_token("<unk>", 1)
                                          set_default_index(1)   ← 生词一律映射到 1
```

#### 4.1.3 源码精读

**第一步：加载 IMDb 并切分训练/验证集**。[ch15/ch15_part2.py:L76-L83](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L76-L83) 用 torchtext 的 `IMDB` 类自动下载 50000 条影评（正负面各半），每条样本是 `(标签字符串, 评论文本)` 二元组，`random_split` 把 25000 条训练数据切成 20000 训练 + 5000 验证（测试集 25000 条单独加载）。

**第二步：手写分词器**。[ch15/ch15_part2.py:L92-L97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L92-L97) 定义 `tokenizer`：先删 HTML 标签（`<[^>]*>`），再用正则 `(?::|;|=)(?:-)?(?:\)|\(|D|P)` 抓出表情符号（如 `:)` `;(` `=-D`），接着把其余非字母数字字符全替换成空格并转小写，最后把表情符号追加到字符串末尾（去掉连字符）。这个「表情符号单独抢救出来」的写法与第 8 章（u8-l1）的情感分析预处理完全同源——表情符号对情感判断极有信息量，不能当标点删掉。

**第三步：统计词频**。[ch15/ch15_part2.py:L100-L105](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L100-L105) 遍历训练集逐条分词并喂给 `Counter`。注意循环变量写作 `for label, line in train_dataset`——IMDb 每个样本是 `(label, text)`，**标签在前**，后面 `collate_batch` 同样按此解包。

**第四步：构建词表**。[ch15/ch15_part2.py:L112-L121](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L112-L121) 先按词频降序排成 `OrderedDict`，用 `torchtext.vocab` 的 `vocab()` 工厂建成词表；`insert_token` 把 `<pad>` 与 `<unk>` 插到 0、1 两个位置（原有词的 id 整体后移 2，所以 id 0/1 被特殊记号**独占**）；`set_default_index(1)` 让查不到的生词静默返回 1 而不是抛异常。最后一行 `print([vocab[token] for token in ['this', 'is', 'an', 'example']])` 演示正向查找：`vocab[token]` 就是「词 → id」。torchtext 的 Vocab 对象还提供反查接口 `vocab.look_up_token(id)`（id → 词），书中主代码未用到，下面实践里我们会拿它做检查。

#### 4.1.4 代码实践

**实践目标**：验证分词器行为与词表的两个查找方向。

1. 按第 2 节的环境提示启动 Python，进入 `ch15/` 目录；
2. 运行下面的示例代码（标注为**示例代码**，改编自 `ch15_part2.py` 的 Step 2–3，首次运行会自动下载 IMDb，约 80MB，需联网）：

```python
# 示例代码：验证 tokenizer 与 vocab（需先跑通 ch15_part2.py 的 Step 1-3）
sentence = "This movie is <b>REALLY</b> good :)"
print(tokenizer(sentence))
# 期望：['this', 'movie', 'is', 'really', 'good', ':)']

ids = [vocab[t] for t in ['this', 'movie', 'is']]
print(ids)                       # 词 → id
print(vocab.look_up_token(ids[0]))  # id → 词 反查
print(vocab["一个不在词表里的词"])   # 生词 → 应返回 1 (<unk>)
```

3. **观察现象**：`tokenizer` 输出全小写、无 HTML、表情符号 `:)` 被挪到末尾；生词查询返回 1 而不报错。
4. **预期结果**：反查结果应与原词一致；生词返回 1。若 `look_up_token` 报 `AttributeError`，说明 torchtext 版本过旧（该接口在 0.9+ 提供）——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`tokenizer("I saw it yesterday ;-) ... great!")` 的返回值里，`;` 和 `-` 会去哪里？
**答案**：`;-)` 匹配表情正则被抓出（`;-` 后必须有 `)`/`(`/`D`/`P`），追加到末尾时 `replace('-', '')` 去掉连字符，得到 `;)`；句中的 `...` 和 `!` 属于 `\W` 被替换成空格消失。所以表情记号以「去连字符」形态出现在 token 列表末尾。

**练习 2**：为什么必须让 `<pad>` 的 id 是 0，且在 `nn.Embedding` 里指定 `padding_idx=0`？
**答案**：`pad_sequence` 补齐时默认填 0，补齐位的 id 就是 0；`Embedding(..., padding_idx=0)` 让 id 0 对应的嵌入向量在训练中不更新（梯度恒为 0），这样「补出来的占位符」不会参与学习，也不会影响句子语义。两者靠「都等于 0」这一约定对齐。

**练习 3**：词表是在训练集、验证集还是全部数据上统计的？为什么？
**答案**：只在 20000 条训练集上统计（`for label, line in train_dataset`）。验证/测试集的职责是模拟「未来没见过的数据」，其生词经 `set_default_index(1)` 落到 `<unk>`；若把测试集词汇也算进词表，就泄漏了评估信息。

### 4.2 collate_batch：变长文本的批处理三件套

#### 4.2.1 概念说明

第 12 讲（u12-l2）说过，`DataLoader` 默认的堆批逻辑要求样本同形。评论长度从几十词到上千词不等，直接堆批会失败。解法是自定义 `collate_fn`（collate 意为「整理、归档」），本讲的 `collate_batch` 对每个批做三件事：

1. **清洗与编码**：每条评论过 `text_pipeline`（分词 + 查词表）变成整数 id 张量，标签过 `label_pipeline` 变成 0.0/1.0 浮点数；
2. **pad 补齐**：`pad_sequence` 把批内最短评论末尾补 0，对齐到批内最大长度，得到规整的 `(批大小, 最大长度)` 矩阵；
3. **记录真实长度**：把每条评论补齐前的长度存进 `lengths` 张量——这是关键第三件套，因为补齐后光看张量已分不清哪些 0 是补的、哪些位置是真实词（更本质的原因见下：`pack_padded_sequence` 需要它）。

再次强调：仓库这一版**没有**截断（truncation，即把超长评论裁到固定长度如 200）与 offset 两种操作；若想控制序列长度，需要读者自行在 `text_pipeline` 里加切片（见本节练习 3）。

#### 4.2.2 核心流程

```text
DataLoader 取出一个 batch = [(label1, text1), (label2, text2), ...]
        │
        ▼ 逐条处理
text_pipeline(text) ──▶ torch.tensor([id, id, id, ...], dtype=int64)
label_pipeline(label) ──▶ 1.0 ('pos') 或 0.0 ('neg')
lengths.append(该条 token 数)
        │
        ▼ 批级整理
pad_sequence(text_list, batch_first=True)
        │
        ▼ 输出三元组（都搬到 device）
(padded_text [B, L_max], label [B], lengths [B])
```

#### 4.2.3 源码精读

**编码管道**。[ch15/ch15_part2.py:L126-L132](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch15/ch15_part2.py#L126-L132) 定义两条 lambda 管道：`text_pipeline` 把文本变成「id 列表」（`vocab[token]` 遇生词自动落 `<unk>`）；`label_pipeline` 把字符串标签 `'pos'`/`'neg'` 映成 `1.`/`0.` 浮点——是浮点而不是整数，因为后面要与 Sigmoid 概率一起喂给 `BCELoss`。这两行还选定 `device`（默认 `cuda:0`，无 GPU 改用注释掉的 `'cpu'` 行）。

**collate_batch 本体**。[ch15/ch15_part2.py:L136-L148](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L136-L148) 中，循环逐条做「标签编码 → 文本转 int64 张量 → 记录长度」；循环外把三个列表分别堆成张量，其中 [L146-L147](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L146-L147) 调用 `nn.utils.rnn.pad_sequence(text_list, batch_first=True)` 把「长度不一的 id 张量列表」补 0 对齐成 `(B, L_max)` 矩阵（`batch_first=True` 让批维在前，与 u15-l1 讲的 `(batch, seq_len, feature)` 约定一致）；最后 [L148](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L148) 把三个张量统一 `.to(device)`。

**先拿 4 条样本试批**。[ch15/ch15_part2.py:L153-L160](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L153-L160) 用 `batch_size=4` 建一个临时 DataLoader，`next(iter(dataloader))` 取出第一个批，打印补齐矩阵、标签、长度与形状——这是作者刻意安排的「小批冒烟测试」，值得效仿：上模型之前先确认数据形状。

**正式三个 DataLoader**。[ch15/ch15_part2.py:L165-L174](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L165-L174) 以 `batch_size=32` 建训练/验证/测试三个加载器，全部传 `collate_fn=collate_batch`；只有训练集 `shuffle=True`（理由同 u12-l2：打破样本顺序相关性，验证测试无需打乱）。

#### 4.2.4 代码实践

**实践目标**：亲眼看一个批的形状与 pad 的位置。

1. 跑通 Step 1–3 后，在 `ch15` 目录运行下面的**示例代码**（不需要 GPU）：

```python
# 示例代码：观察一个批的形状与补齐（改编自 ch15_part2.py L153-L160）
device = 'cpu'   # 便于无 GPU 验证；正式训练用原文件的 cuda:0
dl = DataLoader(train_dataset, batch_size=4, shuffle=False, collate_fn=collate_batch)
text_batch, label_batch, length_batch = next(iter(dl))
print(text_batch.shape)      # (4, 批内最长评论的 token 数)
print(length_batch)          # 4 条评论各自的真实长度
print(label_batch)           # 0./1. 浮点标签

short = length_batch.argmin()           # 最短那条的下标
real_len = length_batch[short]
print(text_batch[short, :real_len][-5:])   # 真实词 id（末 5 个）
print(text_batch[short, real_len:][:10])   # 补齐区：应全为 0
```

2. **观察现象**：补齐区（真实长度之后的位置）全是 0；`text_batch.shape[1]` 等于 `length_batch.max()`。
3. **预期结果**：打印出的补齐区元素全为 0，与 4.1 练习 2 的 `<pad>`=0 约定吻合。批内长短差越大，补齐浪费的算力越多——这正是 4.3 节 `pack_padded_sequence` 存在的原因。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：批内两条评论分别有 5 和 9 个 token，`pad_sequence(batch_first=True)` 输出形状是什么？`lengths` 是什么？
**答案**：形状 `(2, 9)`——第 1 条末尾补 4 个 0；`lengths = [5, 9]`。

**练习 2**：既然 id 0 被特殊记号 `<pad>` 独占（4.1.3），理论上扫描矩阵就能恢复每条的真实长度，为什么还要单独传 `lengths`？
**答案**：两个原因：其一，模型前向里的 `pack_padded_sequence` 明确需要长度参数才能知道每条序列在哪一位结束；其二，显式传长度比逐条扫描更直接、也更能表达意图——「长度」是序列建模的一等公民信息，而不是可以从数据反推的冗余量。

**练习 3**：如果想让超长评论最多保留 200 个 token（截断），应该改哪里？
**答案**：改 `text_pipeline`，例如 `lambda x: [vocab[token] for token in tokenizer(x)][:200]`（`collate_batch` 无需改动，`lengths` 会自动跟着变短）。注意：这是**示例代码**，仓库当前版本没有做截断，IMDb 评论长短悬殊时训练会比较慢。

### 4.3 Embedding + LSTM 模型：三种变体与 EmbeddingBag 路线对比

#### 4.3.1 概念说明

**嵌入层**：`nn.Embedding(num_embeddings, embedding_dim)` 是一张 `(词表大小, 嵌入维度)` 的可学习查找表。输入整数 id（或 id 矩阵），输出对应行向量。它等价于「one-hot 向量乘权重矩阵」，但查一行远快于做一次高维矩阵乘法。文件中 [ch15/ch15_part2.py:L199-L205](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L199-L205) 用一个 10 词 × 3 维的小表演示了：输入 `(2, 4)` 的 id 矩阵，输出 `(2, 4, 3)` 的张量——每个 id 被替换成一个 3 维向量（`padding_idx=0` 对应的行恒为初始值）。

**LSTM 与朴素 RNN 的门控差别**：u15-l1 的朴素 RNN 只有一条通道 \(h_t = \tanh(W_{xh}x_t + W_{hh}h_{t-1} + b_h)\)，\(h\) 既当记忆又当输出，\(\tanh\) 一饱和梯度就消失。LSTM 引入**细胞状态** \(C_t\) 这条加法通道和三个门：

\[
\begin{aligned}
i_t &= \sigma(W_{xi}x_t + W_{hi}h_{t-1} + b_i) &&\text{输入门：放多少新信息进记忆}\\
f_t &= \sigma(W_{xf}x_t + W_{hf}h_{t-1} + b_f) &&\text{遗忘门：旧记忆保留多少}\\
o_t &= \sigma(W_{xo}x_t + W_{ho}h_{t-1} + b_o) &&\text{输出门：记忆输出多少}\\
\tilde{C}_t &= \tanh(W_{xC}x_t + W_{hC}h_{t-1} + b_C) &&\text{候选记忆}\\
C_t &= f_t \odot C_{t-1} + i_t \odot \tilde{C}_t &&\text{记忆更新：逐元素加权}\\
h_t &= o_t \odot \tanh(C_t) &&\text{隐状态}
\end{aligned}
\]

关键在 \(C_t = f_t \odot C_{t-1} + \dots\) 这一步**加法**：梯度沿加法通道几乎无衰减地流回很早的时间步（u15-l1 讲的链式法则里，加法节点的局部导数为 1），这就是「梯度高速公路」。\(\sigma\) 是 u3-l1 讲过的 sigmoid，输出 (0,1) 当作「阀门开度」，逐元素（\(\odot\)）地调制信息流。

**pack_padded_sequence 的意义**：补齐位的 0 送进 LSTM 也会被计算，还会稀释隐状态。`pack_padded_sequence` 把补齐矩阵压缩成「紧凑打包」格式，RNN 家族层（`nn.LSTM` 等）遇到它就**只计算真实长度内的位置**；算完可用 `pad_packed_sequence` 再展开。它就是 4.2 节 `lengths` 的消费者。

**两条路线对比（EmbeddingBag 路线在仓库中不存在）**：处理变长文本的另一条流行路线是——不补齐，把整批序列**首尾拼接成一维长序列**，再配一个 offset 数组记录每条序列的起点，最后用 `nn.EmbeddingBag` 直接输出每条序列嵌入的（加权）平均。torchtext 官方教程采用它。**本书没有采用**：作者选择了「pad + pack」路线，模型里逐位置跑 LSTM。两者对比：

| | pad + pack 路线（本书） | 拼接 + offset + EmbeddingBag 路线（torchtext 教程） |
| --- | --- | --- |
| 批张量形状 | `(B, L_max)` 规整矩阵 | 一维 `(总 token 数,)` + offset 数组 |
| 序列建模 | LSTM 逐步递推，保留词序 | 通常退化为词袋平均（词序丢失） |
| 补齐开销 | 有（短序列补 0） | 无 |
| 仓库中是否存在 | 是（`collate_batch` + `pack_padded_sequence`） | **否** |

#### 4.3.2 核心流程

模型前向（以单向 LSTM 版为例）：

```text
text [B, L] 整数 id 矩阵, lengths [B]
  ──▶ Embedding(vocab_size, 20, padding_idx=0)   → [B, L, 20]     词 id → 稠密向量
  ──▶ pack_padded_sequence(..., enforce_sorted=False)  打包，跳过补齐位
  ──▶ nn.LSTM(20, 64, batch_first=True)          → output, (hidden, cell)
  ──▶ hidden[-1]                                 → [B, 64]        取最后一层最终隐状态
  ──▶ Linear(64, 64) → ReLU → Linear(64, 1)      → [B, 1]         分类头
  ──▶ Sigmoid                                     → [B, 1] ∈ (0,1) 正面概率
```

这是 u15-l1 五种架构中的**多对一**：整条序列进，一个 sentiment 概率出。

#### 4.3.3 源码精读

**变体一：演示用通用 RNN**。[ch15/ch15_part2.py:L224-L246](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L224-L246) 先给一个与任务无关的最小骨架：两层 `nn.RNN`（注释里标明 `nn.GRU`/`nn.LSTM` 换一行即可），`forward` 中 `_, hidden = self.rnn(x)` 丢弃逐步输出 `output`、只留 `hidden`，`hidden[-1, :, :]` 取**最后一层**的最终隐状态送入 `nn.Linear(hidden_size, 1)`。[L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L245) 用 `model(torch.randn(5, 3, 64))` 验证形状：(5 批, 3 步, 64 维特征) 的输入可以顺畅通过。`hidden` 的形状是 `(num_layers, B, H)`，所以 `[-1]` 是「层」维上的最后一个——这个下标习惯在变体三里会升级成 `[-2]` 与 `[-1]` 配对。

**变体二：情感分类 LSTM（主角）**。[ch15/ch15_part2.py:L252-L274](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L252-L274) 定义与任务同名的 `RNN` 类（覆盖了前一个类）。`__init__` 四件套：`Embedding(vocab_size, embed_dim, padding_idx=0)`（pad 行不学习）；`nn.LSTM(embed_dim, rnn_hidden_size, batch_first=True)`；`fc1 + ReLU + fc2` 两层全连接头；末端 `Sigmoid` 压成概率。`forward` 的两处细节最值得精读：

- [L267](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L267) `pack_padded_sequence(out, lengths.cpu().numpy(), enforce_sorted=False, batch_first=True)`：`lengths` 必须转回 CPU numpy（CUDA 张量不能 `.numpy()`，这是常见报错点）；`enforce_sorted=False` 允许批内序列乱序（DataLoader 是随机取样的，不保证按长度降序），函数内部会自行处理排序。
- [L268-L269](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L268-L269) `out, (hidden, cell) = self.rnn(out)` 取出 `hidden`（形状 `(1, B, 64)`，1 = 单层×单向），`hidden[-1, :, :]` 即最终隐状态——LSTM 读完整个词序列后留在「记忆」里的摘要，用它代表整条评论。

[L276-L283](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L276-L283) 设定超参数：`vocab_size = len(vocab)`、`embed_dim = 20`、`rnn_hidden_size = 64`、`fc_hidden_size = 64`，固定 `torch.manual_seed(1)` 后实例化并 `.to(device)`。实践任务将扫描 `rnn_hidden_size`。

**变体三：双向 LSTM**。[ch15/ch15_part2.py:L341-L367](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L341-L367) 与变体二的差别只有三处：`nn.LSTM(..., bidirectional=True)`；`fc1` 输入维度改为 `rnn_hidden_size*2`；[L358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L358) 用 `torch.cat((hidden[-2, :, :], hidden[-1, :, :]), dim=1)` 把**前向最后一层的终点隐状态**（`hidden[-2]`，读完全文）与**后向最后一层的终点隐状态**（`hidden[-1]`，从最后一个词倒读回开头）拼成 `2H` 维特征。情感判断天然双向有益——句首的否定词（never、not）会翻转句尾形容词的极性，只读一半容易误判。注意双向 RNN 依赖「完整序列」，不适合语言模型那种逐词生成任务（u15-l3 会回到单向）。

#### 4.3.4 代码实践

**实践目标**：不训练，只验证三种前向的形状链路。

运行下面的**示例代码**（变量 `vocab_size`、`embed_dim` 沿用文件定义；`device` 用 `'cpu'` 即可）：

```python
# 示例代码：验证 LSTM 前向的形状（构造一个 3×7 的小批，真实长度 7、5、3）
import torch
from ch15_part2 import RNN, vocab            # 或在同一进程里已执行过该文件定义
model_cpu = RNN(len(vocab), 20, 64, 64).eval()  # 换成 CPU 副本，避免无 GPU 报错
text = torch.randint(2, len(vocab), (3, 7))     # id 从 2 起，避开 pad/unk
lengths = torch.tensor([7, 5, 3])
with torch.no_grad():
    prob = model_cpu(text, lengths)
print(prob.shape)   # 期望 (3, 1)
print(prob)         # 3 个 (0,1) 之间的概率
```

1. **操作步骤**：也可以更简单——直接把上面三行张量构造接到 `ch15_part2.py` 的 L283 之后打印 `model(text.to('cpu'), lengths)`。
2. **观察现象**：输出 `(3, 1)`，每个值在 0 与 1 之间（未训练，值可能都接近 0.5 附近的任意位置）。
3. **预期结果**：形状正确即通过；若报 `lengths` 相关错误，检查是否忘了 `.cpu().numpy()`（L267 的既有写法已处理）。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`nn.Embedding(vocab_size, 20, padding_idx=0)` 这一层的参数量是多少？其中多少个在训练中保持不变？
**答案**：`vocab_size × 20`（每词一行 20 维）。id 0（`<pad>`）那一行的 20 个参数因 `padding_idx=0` 不接收梯度更新，其余照常学习。

**练习 2**：变体三中 `hidden` 的形状是 `(2, B, 64)`，为什么取 `[-2]` 和 `[-1]` 两个拼起来，而不是像单向那样只取 `[-1]`？
**答案**：双向单层时「层×方向」维大小为 2：`hidden[-2]` 是**前向**读完整个序列后的终点隐状态，`hidden[-1]` 是**后向**从句尾倒读回句首的终点隐状态。二者各代表一个阅读方向对全文的概括，拼接成 128 维才能同时利用双向信息；只取 `[-1]` 会丢掉整个前向方向，且维度（64）也对不上 `fc1` 的输入（128）。

**练习 3**：把变体二的 `nn.LSTM` 换成 `nn.GRU`，`forward` 里哪一行会编译报错？为什么？
**答案**：`out, (hidden, cell) = self.rnn(out)` 一行。GRU 没有独立细胞状态，返回值是 `(output, hidden)` 二元组，解包成三元组会直接报错；应改为 `_, hidden = self.rnn(out)`（后续 `hidden[-1]` 用法不变）。LSTM 返回 `(output, (h, c))` 是 RNN 家族里的特例。

### 4.4 train / evaluate：BCE 训练循环与 no_grad 评估

#### 4.4.1 概念说明

`train` 与 `evaluate` 是一对镜像函数，共同构成 u12-l3 总结的「五步训练骨架」与评估骨架。三个要点：

1. **模式切换**：`model.train()` 与 `model.eval()` 切换层的训练/评估行为（本模型没有 dropout/batchnorm，差别不明显，但 u14-l2 的 CNN 里这个开关直接控制 dropout 生效与否）；
2. **五步循环**：`zero_grad → 前向 → 算损失 → backward → step`，与 XOR、MNIST、CNN 各章完全同构；
3. **`torch.no_grad()`**：评估不需要梯度，包住循环可以不建计算图，省显存、提速度；训练循环里**绝不能**加它，否则 `loss.backward()` 无梯度可反传。

损失用 `nn.BCELoss()`：输入必须先过 Sigmoid（这正是模型末端挂 `self.sigmoid` 的原因）。准确率以 0.5 为阈值 `(pred >= 0.5)` 把概率二值化后与浮点标签比对。

#### 4.4.2 核心流程

```text
train(dataloader):                    evaluate(dataloader):
  model.train()                         model.eval()
  for 每个批:                            with torch.no_grad():
    optimizer.zero_grad()                 for 每个批:
    pred = model(text, lengths)[:, 0]       pred = model(text, lengths)[:, 0]
    loss = BCELoss(pred, label)             loss = BCELoss(pred, label)
    loss.backward()                         （无 backward / step）
    optimizer.step()                        累计准确数与损失
    累计准确数与损失                       返回 acc, loss
  返回 acc, loss
主循环: for epoch: train(train_dl) → evaluate(valid_dl) → 打印
最后: evaluate(test_dl) 一次性报告测试准确率
```

#### 4.4.3 源码精读

**train 函数**。[ch15/ch15_part2.py:L288-L299](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L288-L299)。[L293](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L293) `pred = model(text_batch, lengths)[:, 0]` 把 `(B, 1)` 输出压成 `(B,)` 与标签同形；随后 [L292-L296](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L292-L296) 是标准五步：清梯度、前向、损失、反传、更新。[L297](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L297) `(pred>=0.5).float() == label_batch` 三步链：阈值化 → 转浮点 → 与 0./1. 标签逐元素比对，`.sum().item()` 得批内正确数。[L298](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L298) `total_loss += loss.item()*label_batch.size(0)`：`BCELoss` 默认 `reduction='mean'` 返回**批平均**，乘回批大小还原成「逐样本损失之和」，这样最后除以 `len(dataloader.dataset)`（数据集总样本数）得到的才是**逐样本平均损失**，最后一个不完整批也不会带来偏差。

**evaluate 函数**。[ch15/ch15_part2.py:L301-L310](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L301-L310) 与 train 逐行对照：多了 [L304](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch15/ch15_part2.py#L304) 的 `with torch.no_grad():`，少了 `zero_grad/backward/step` 三行——评估只前向不学习。累计逻辑与 train 完全一致，两个函数可直接复用给任何 DataLoader。

**损失、优化器与主循环**。[ch15/ch15_part2.py:L315-L325](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L315-L325) 选 `nn.BCELoss()` + `Adam(lr=0.001)`，跑 10 个 epoch，每轮打印训练/验证准确率；[L331-L332](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L331-L332) 训练结束后在 `test_dl` 上一次性报告测试准确率——沿袭 u6-l1 的纪律：验证集用于训练中观察，测试集只在最后碰一次。双向版的重跑在 [L365-L395](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L365-L395)，注意 [L373](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L373) 把学习率提到 0.002、[L387-L389](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L387-L389) 重新加载了一份测试集与 DataLoader。

#### 4.4.4 代码实践

**实践目标**：验证「评估不需要梯度」与 `model.train()/eval()` 的存在感。

1. 阅读式实践：对照 [L288-L299](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L288-L299) 与 [L301-L310](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part2.py#L301-L310)，列出差异行（答案应有且仅有：`train()`/`eval()` 切换、`no_grad` 上下文、三行梯度操作）。
2. 小实验（**示例代码**）：在 evaluate 循环里临时打印 `pred.requires_grad`，会发现它在 `no_grad` 下为 `False`；把 `with torch.no_grad():` 注释掉再打印则为 `True`——后者会白白为每个评估批构建计算图。改完记得还原。
3. **预期结果**：`no_grad` 版评估更快、显存占用更低；两版准确率数值相同（前向数值不受是否建图影响）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `evaluate` 里没有 `optimizer.zero_grad()`？删掉 train 里的这一行会发生什么？
**答案**：评估不更新参数，无需清梯度。train 里删掉它的话，`loss.backward()` 会把梯度**累加**到上一批的旧梯度上（autograd 的累加语义，u12-l3 讲过），实际更新方向被历史梯度污染，训练立即异常。

**练习 2**：`total_loss += loss.item()*label_batch.size(0)` 中 `label_batch.size(0)` 是什么？为什么乘它？
**答案**：当前批的样本数（batch_size，最后一个批可能不足 32）。`BCELoss` 默认返回批内平均损失，乘以样本数还原为「这批样本的损失总和」；各批之和再除以整个数据集样本数，得到逐样本平均——这等价于把全部样本放一起算的均值，且对不完整的尾批同样成立。

**练习 3**：模型为什么把 `Sigmoid` 写进 `forward`，而不是像有些代码那样配 `BCEWithLogitsLoss`？
**答案**：`BCELoss` 的输入要求是概率（0 到 1 之间），所以 Sigmoid 必须在模型内先做；`BCEWithLogitsLoss` 则把 sigmoid 与 BCE 数值稳定地合并，要求模型吐出未过激活的 logits。两种搭配二选一，配错（比如 sigmoid 输出再喂 `BCEWithLogitsLoss`）会造成双重压缩、训练变差。

## 5. 综合实践

**任务：扫描 LSTM 隐层宽度，并量化 collate_batch 的批形状。**

这个任务把本讲四个模块串起来：词表与 collate（数据侧）、Embedding+LSTM 前向（模型侧）、train/evaluate 循环（训练侧）。

**操作步骤**（在跑通 `ch15_part2.py` Step 1–3B 之后进行；GPU 训练快，纯 CPU 建议把 `train_dataset` 进一步切小，如 `random_split(list(train_dataset), [5000, 20000])`——**示例代码**）：

```python
# 示例代码：hidden_dim 扫描 + 批长度统计（改编自 ch15_part2.py L276-L332）
import copy, torch

results = {}
for hidden_dim in [8, 16, 64]:
    torch.manual_seed(1)
    model = RNN(len(vocab), 20, hidden_dim, 64).to(device)
    loss_fn = nn.BCELoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    for epoch in range(3):                      # 省时用 3 轮，原书 10 轮
        acc_train, loss_train = train(train_dl)
        acc_valid, loss_valid = evaluate(valid_dl)
        print(f'hidden={hidden_dim} epoch={epoch} '
              f'acc: {acc_train:.4f} val_acc: {acc_valid:.4f}')
    acc_test, _ = evaluate(test_dl)
    results[hidden_dim] = (acc_valid, acc_test)

print(results)

# 第二部分：统计一个批经 collate_batch 后的实际长度
text_batch, label_batch, length_batch = next(iter(DataLoader(
    train_dataset, batch_size=32, shuffle=True, collate_fn=collate_batch)))
print('批形状:', text_batch.shape)            # (32, 批内最长 token 数)
print('长度分布: min/mean/max =',
      length_batch.min().item(),
      length_batch.float().mean().item(),
      length_batch.max().item())
print('补齐占比: {:.1%}'.format(
    1 - length_batch.sum().item() / text_batch.numel()))
```

**需要观察的现象**：

1. 三个宽度的验证/测试准确率排序——预期 64 ≥ 16 > 8：隐层太窄（8）时「记忆容量」不足，前几轮验证准确率明显偏低；16 与 64 在 3 轮内可能接近（Embedding 才 20 维，信息瓶颈不全在 LSTM）。
2. `length_batch` 的 min/mean/max 差异巨大（IMDb 评论从几十词到上千词），`text_batch.shape[1]` 被**批内最长一条**撑大，补齐占比可能高达百分之几十——这正是 `pack_padded_sequence` 与（概念上的）截断/分桶等技巧存在的理由。

**预期结果**：训练日志逐轮上升，3 轮内验证准确率大致落在 0.7~0.85 区间（原书 10 轮、hidden=64 的量级在 0.85 上下，双向版更高）；`text_batch.shape[0]` 恒为 32（尾批除外），`shape[1]` 因批而异。所有具体数字**待本地验证**（依赖硬件、torchtext 版本与随机种子）。

## 6. 本讲小结

- 文本预处理四步——`tokenizer` 分词、`Counter` 词频、`vocab()` 词表、`text_pipeline` 编码——把字符串变成整数 id；`<pad>`=0 / `<unk>`=1 两个特殊记号靠 `insert_token` 与 `set_default_index(1)` 兜底。
- `collate_batch` 三件套：逐条「清洗→id 化→记长度」，批级 `pad_sequence` 补齐，输出 `(padded_text, labels, lengths)` 三元组；仓库版本**没有** offset 与截断。
- 模型前向链路：`Embedding(padding_idx=0)` → `pack_padded_sequence`（跳过补齐位，`lengths` 在此被消费）→ `LSTM` → `hidden[-1]` → 两层全连接 → `Sigmoid`；双向版改 `bidirectional=True` 并拼接 `hidden[-2]` 与 `hidden[-1]`。
- LSTM 用输入/遗忘/输出三门与细胞状态 \(C_t = f_t \odot C_{t-1} + i_t \odot \tilde{C}_t\) 的加法通道缓解朴素 RNN 的长程梯度消失。
- `train` 是五步梯度循环，`evaluate` 是 `model.eval()` + `torch.no_grad()` 的纯前向；损失配对是 Sigmoid（模型内）+ `BCELoss`（循环内），准确率以 0.5 阈值化统计。

## 7. 下一步学习建议

本讲解决了「序列进、一个值出」的多对一分类；下一讲 **u15-l3（LSTM 语言模型与文本采样生成）** 转向「文本进、文本出」的同步多对多任务：阅读 `ch15/ch15_part3.py` 中 `TextDataset` 按 `step=1` 滑窗构造 `(x, y)` 错位样本、训练字符级 LSTM、以及 `sample()` 从起始文本自回归生成新段落。届时会重新用上 u15-l1 的隐状态递推直觉，并理解为什么语言模型必须**单向**。建议先自己重读一遍 [ch15/ch15_part3.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch15/ch15_part3.py) 的数据集类，带着「它和本讲 `collate_batch` 解决的是不是同一个问题」去读。
