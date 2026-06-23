# 语言模型与训练自己的嵌入

## 1. 本讲目标

上一讲 [u4-l2](./u4-l2-word-embeddings.md) 我们「拿来就用」了别人预训练好的 Word2Vec / GloVe 词向量，并验证它们能做 `king − man + woman ≈ queen` 这类语义运算。但那些向量是怎么「凭空」训出来的？没有人工标注的语义标签，机器凭什么学会「microsoft 和 ibm 语义相近」？

本讲就回答这个问题。我们把视角从「加载嵌入」升级为「训练嵌入」，学完后你应当能够：

1. 说清**语言模型（language model）**的核心目标——给文本打分、预测被遮盖的词，并理解为什么这是用海量无标注文本做「自监督学习」的关键。
2. 说清 **n-gram 语言模型**的马尔可夫假设，以及它数据稀疏、只向后看、无词相似度等局限。
3. 看懂 `CBoW-PyTorch.ipynb` 如何用「上下文预测中心词」这一自监督任务，从零训出自己的 Word2Vec，并理解 `Embedding + Linear` 这个极简结构为什么能产生语义向量。
4. 会用「最近邻（L2 距离）」和「PCA 降维可视化」两种方式，**检验自己训出来的嵌入**是否真的带上了语义。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **词嵌入（word embedding）**：把每个词表示成一段短而稠密、带语义的向量。详见上一讲 [u4-l2](./u4-l2-word-embeddings.md)。
- **`nn.Embedding` 的本质**：它就是一张「按词编号查表」的矩阵，输入词编号、输出该词的向量。这是本讲模型的唯一可学习部件之一。
- **PyTorch 训练五件套**：`zero_grad → 前向 → loss → backward → step`。本讲的训练循环和讲义 u2-l5、u4-l1、u4-l2 完全同构，区别只在于「任务」从分类换成了「预测词」。建议先回顾讲义 [u2-l5](./u2-l5-frameworks-overfitting.md)。
- **交叉熵损失**：分类任务的标准损失，输入是原始 logits、目标是类别编号（无需独热）。详见 [u2-l4](./u2-l4-own-framework.md)。

几个本讲会反复用到的新术语，先在这里点一下：

| 术语 | 一句话解释 |
|------|-----------|
| 语言模型（language model） | 给一段文本打概率，或等价地预测「下一个 / 被遮盖的词」的模型 |
| 自监督学习（self-supervised） | 没有人工标签，靠「遮掉一个词、用上下文猜它」从文本自身造标签 |
| n-gram | 连续 N 个词构成的片段；n-gram 语言模型只看前 N−1 个词预测下一个 |
| 马尔可夫假设 | 假设「下一个词」只取决于最近的有限几个词，而非整段历史 |
| CBoW（Continuous Bag-of-Words） | 用上下文（一袋邻居词）预测中心词 |
| Skip-gram | 反过来，用中心词预测上下文邻居 |
| 最近邻 / L2 距离 | 用向量差的范数衡量两个词向量的远近，越近语义越相似 |

## 3. 本讲源码地图

本讲只涉及 `lessons/5-NLP/15-LanguageModeling/` 一个课程目录，两个文件：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/README.md) | 课程讲义：点明「语义嵌入是迈向语言建模的第一步」，并列出三种训练嵌入的思路（N-Gram / CBoW / Skip-gram） |
| [CBoW-PyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb) | 可执行笔记本：用 AG News 语料、PyTorch 从零训练一个 CBoW 模型，并演示如何提取词向量、查最近邻 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/lab/README.md) | 官方 lab：把 CBoW 改成 Skip-gram，在自选书籍语料上训练，并建议用 PCA 可视化 |

> 说明：笔记本在 GitHub 上按单元格（cell）渲染。下文永久链接的行号是 `.ipynb` 原始 JSON 文件的行号；我会在链接旁注明对应单元格编号（如「cell-4」），方便你在渲染页对照。

---

## 4. 核心概念与源码讲解

### 4.1 n-gram 语言模型

#### 4.1.1 概念说明

上一讲我们看到词嵌入「像魔法一样」管用，README 开篇点破：**语义嵌入其实是迈向「语言建模」的第一步**——语言模型试图用某种方式去*理解*或*表示*语言的本质。

语言模型的核心目标，是给一段文本 \(w_1, w_2, \dots, w_T\) 打一个概率：

\[
P(w_1, w_2, \dots, w_T) = \prod_{t=1}^{T} P(w_t \mid w_1, \dots, w_{t-1})
\]

也就是说，它要能回答「给定前面这些词，下一个词最可能是谁」。等价地，这也是「**预测被遮盖的词**」。

为什么这件事如此重要？README 给出关键动机：

> [lessons/5-NLP/15-LanguageModeling/README.md:7](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/README.md#L7) —— 我们能在**无标注数据集**上**无监督**地训练语言模型。这很重要，因为互联网上有海量无标注文本，而有标注文本永远受限于人工标注的力气。最常用的技巧是「**预测文本中缺失的词**」：随机遮掉一个词，这一条就成了一个训练样本。

这是一种**自监督学习（self-supervised learning）**——没有人手工标注「microsoft 的语义」，但「遮住一个词、用上下文猜它」这个任务天然能从任意文本里自动造出无穷无尽的训练样本。本讲及后续的 RNN、Transformer、GPT，本质都在做这件事，只是网络结构越来越强。

最朴素的实现就是 **n-gram 语言模型**。它对历史做**马尔可夫假设**：下一个词只取决于最近的 \(N-1\) 个词，而非整段历史：

\[
P(w_t \mid w_1, \dots, w_{t-1}) \approx P(w_t \mid w_{t-N+1}, \dots, w_{t-1})
\]

这个条件概率用「数频次」就能估出来（最大似然估计）：

\[
P(w_t \mid w_{t-N+1}, \dots, w_{t-1}) \approx \frac{\text{count}(w_{t-N+1}, \dots, w_{t-1}, w_t)}{\text{count}(w_{t-N+1}, \dots, w_{t-1})}
\]

README 把它列为三种「训练嵌入」思路之一：

> [lessons/5-NLP/15-LanguageModeling/README.md:13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/README.md#L13) —— **N-Gram 语言建模**：看前 N 个词来预测当前词。

#### 4.1.2 核心流程

n-gram 语言模型的工作流程：

1. **切分**：把语料切成一个个 N 连词（n-gram）。例如对 `I like deep learning`，2-gram 为 `(I like) (like deep) (deep learning)`。
2. **数频次**：统计每个 n-gram 和 (N−1)-gram（前缀）在语料里出现的次数。
3. **估概率**：用上面的比值估计条件概率。
4. **预测 / 打分**：给定前缀，取概率最高的词作为预测；或对整句连乘得到句子概率。

伪代码：

```
counts_n     = Counter()   # 完整 N 连词频次
counts_n_minus_1 = Counter()  # 前 N-1 连词频次
for ngram in corpus_ngrams(text, N):
    counts_n[ngram] += 1
    counts_n_minus_1[ngram[:-1]] += 1

def predict(prefix):            # prefix 长度为 N-1
    return argmax_w  counts_n[prefix + (w,)] / counts_n_minus_1[prefix]
```

#### 4.1.3 源码精读

本课程**没有**单独实现一个统计型 n-gram 语言模型，但 `CBoW-PyTorch.ipynb` 的词表构建函数里，`ngrams` 参数正是 n-gram 思想的直接体现——它决定「把连续 N 个词当成一个词表项」：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:56-70](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L56-L70) —— `load_dataset`（cell-4）。`ngrams_iterator(tokenizer(line), ngrams=ngrams)` 把分词后的句子再切成 n-gram 片段喂进 `Counter`，从而用频次构建词表。默认 `ngrams=1` 即「一个词就是一个词表项」；调大 `ngrams`，词表里就会出现 `new york`、`deep learning` 这种多词单元。这就是 n-gram 在工程里最朴素的用法：**用频次统计，从无标注文本里得到语言的基本单元**。

```python
def load_dataset(ngrams=1, min_freq=1, vocab_size=5000, lines_cnt=500):
    tokenizer = torchtext.data.utils.get_tokenizer('basic_english')
    ...
    counter = collections.Counter()
    for i, (_, line) in enumerate(train_dataset):
        counter.update(torchtext.data.utils.ngrams_iterator(tokenizer(line), ngrams=ngrams))
        if i == lines_cnt:
            break
    vocab = torchtext.vocab.Vocab(collections.Counter(dict(counter.most_common(vocab_size))), min_freq=min_freq)
    return train_dataset, test_dataset, classes, vocab, tokenizer
```

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手感受 n-gram 的「数据稀疏」局限。
2. **操作步骤**：在 `ai4beg` 环境下打开 `CBoW-PyTorch.ipynb`，把 `load_dataset`（cell-4）的 `ngrams` 参数从 `1` 改成 `3`，重新运行 cell-5，观察打印结果。
3. **需要观察的现象**：词表里会出现 `tech stocks fall` 这类 3 词单元；同时，绝大多数 3-gram 在 500 条新闻里只出现 1 次。
4. **预期结果**：你会直观体会到 README 没明说、但 n-gram 模型的致命伤——**数据稀疏（data sparsity）**。具体词组合很难恰好重复出现，`count(前缀+词)` 常为 0，概率估不出来。
5. **结论**：「待本地验证」具体词表内容，但稀疏现象是确定的。

#### 4.1.5 小练习与答案

**练习 1**：n-gram 模型有哪些固有局限？请至少列出 3 条。

**参考答案**：
1. **数据稀疏**：具体词组合很少恰好出现，分母或分子常为 0。
2. **只向后看**：只能用「左边的词」预测，用不到右边的上下文。
3. **无词相似度**：`the cat` 与 `a dog` 在统计上是完全无关的两条记录，模型不知道 cat 和 dog 相似。
4. **N 取值的两难**：N 大则组合数指数爆炸、稀疏更严重；N 小则语义太浅。

**练习 2**：用自监督学习的观点解释，为什么「遮住一个词、用上下文猜它」能让模型从无标注文本中学到东西？

**参考答案**：标签（被遮住的那个词）直接来自文本本身，无需人工标注；于是任意一段文本都能自动转换成训练样本。只要语料够多，模型为了把「猜词」这个任务做好，就必须把「出现在相似上下文里的词」学到相近的表示里——语义就这样被逼出来了。这正是 CBoW / Skip-gram / BERT 的共同根基。

---

### 4.2 CBoW 训练目标

#### 4.2.1 概念说明

n-gram 太朴素、太稀疏。现代训练词嵌入用的是**神经网络化的语言模型**，其中最经典的就是 CBoW。它的理论基础是上一讲提到的**分布式假设（distributional hypothesis）**：**上下文相似的词，语义相似**。

CBoW（Continuous Bag-of-Words）把这个假设变成一个自监督任务：**给定一段词序列 \(W_{-N}, \dots, W_{-1}, W_0, W_1, \dots, W_N\)，用周围 \(2N\) 个邻居词（一「袋」上下文）去预测中心词 \(W_0\)**。

> [lessons/5-NLP/15-LanguageModeling/README.md:14](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/README.md#L14) —— **CBoW**：在词序列 \(W_{-N}, \dots, W_N\) 中预测中间词 \(W_0\)。

它和 n-gram 的关键区别有二：① **双向**——同时用左右邻居；② **连续**——用稠密向量表示词（continuous），并把整个任务塞进一个可微分的小神经网络里端到端训练，从而**绕开了 n-gram 的稀疏问题**（向量之间天然有相似度，`cat` 和 `dog` 不再互不相干）。

> README 还列出了第三种思路 **Skip-gram**：方向反过来，用中心词 \(W_0\) 预测周围邻居，见 [README.md:15](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/README.md#L15)。本讲的官方 lab 就是把笔记本从 CBoW 改成 Skip-gram。

#### 4.2.2 核心流程

笔记本实现的 CBoW（一种成对简化版）流程如下：

1. **造配对**：对句子中每个位置 \(i\)（中心词），在窗口 \([i-N, i+N]\) 内取每个邻居 \(j\)，生成一条 `(邻居词 → 中心词)` 的训练样本。窗口半径 \(N\) 由 `window_size` 控制。
2. **前向**：邻居词编号 → `Embedding` 查表得到 30 维向量 → `Linear` 映射到 `vocab_size` 维 logits。
3. **算损失**：`CrossEntropyLoss(logits, 中心词编号)`。
4. **反向 + 更新**：标准 PyTorch 五件套。
5. **循环**：遍历所有新闻、跑若干 epoch。
6. **取出嵌入**：训练好后，`Embedding` 层的权重矩阵就是我们要的 Word2Vec。

前向打分的直觉（关键！）：因为 `Embedding` 和 `Linear` 之间**没有非线性激活**，输入词 \(w_i\) 对候选中心词 \(c\) 的打分近似为两个向量的内积：

\[
\text{score}(w_i \to c) \approx \text{embed}(w_i) \cdot W_c
\]

其中 \(W_c\) 是 `Linear` 层对应词 \(c\) 的那列权重。最小化交叉熵，就是让 `embed(w_i)` 与「\(w_i\) 经常作为上下文出现的那些中心词」的权重列 \(W_c\) 对齐。于是——**出现在相似上下文里的词，embedding 会被拉到相近的方向**。这就是 CBoW 能凭空产生语义向量的根本原因。

#### 4.2.3 源码精读

**① 造 CBoW 配对**。`to_cbow` 把「用一袋邻居预测中心」拆成大量 `(邻居, 中心)` 配对。笔记本专门强调它能同时处理词列表和编号列表：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:198-207](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L198-L207) —— `to_cbow`（cell-10）。对每个位置 \(i\)，遍历窗口内所有 \(j\neq i\)，追加 `[sent[j], x]`，即 `[邻居, 中心]`。

```python
def to_cbow(sent, window_size=2):
    res = []
    for i, x in enumerate(sent):
        for j in range(max(0, i - window_size), min(i + window_size + 1, len(sent))):
            if i != j:
                res.append([sent[j], x])   # 邻居词 → 中心词
    return res
```

笔记本给的例子（cell-10 输出）非常值得细读：句子 `I like to train networks` 在 \(N=1\) 时生成 `(like,I), (I,like), (to,like), (like,to), (train,to), (to,train), (networks,train), (train,networks)` 等配对。注意**第一项是输入（邻居），第二项是要预测的词（中心）**。

**② 模型结构**。整个 CBoW 网络只有两层，`embedder` 被单独拎出来，因为它就是我们最终要的 Word2Vec：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:135-141](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L135-L141) —— `embedder = nn.Embedding(vocab_size, 30)` 是嵌入层（=Word2Vec）；其后接 `Linear(30, vocab_size)` 把 30 维向量打回整个词表大小的 logits。两层之间**无激活函数**。

```python
vocab_size = len(vocab)
embedder = torch.nn.Embedding(num_embeddings=vocab_size, embedding_dim=30)
model = torch.nn.Sequential(
    embedder,
    torch.nn.Linear(in_features=30, out_features=vocab_size),
)
```

打印结果为 `Embedding(5002, 30)` + `Linear(30, 5002)`。注意 `vocab_size` 是 5002 而非 5000，因为 `torchtext.vocab` 会额外加 `<unk>`（未登录词）和 `<pad>` 两个特殊词。维度 30 是为了跑得快；真正的 Word2Vec 用 300 维（笔记本 markdown cell-7 特意提醒你可以调大）。

**③ 构建训练集**。遍历前 1 万条新闻，对每条用 `window_size=5` 造配对，分别塞进 `X`（邻居）和 `Y`（中心）：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:227-234](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L227-L234) —— `for w1, w2 in to_cbow(encode(x[1], vocab), window_size=5)`，逐条新闻展开成 `(邻居编号, 中心编号)` 配对，最后转成 `torch.tensor`。

**④ 训练循环**。和讲义 u2-l5、u4-l1 的五件套一模一样，区别只在于损失目标变成了「词编号」：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:300-319](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L300-L319) —— `train_epoch`（cell-18）。`for labels, features in dataloader:` 里依次 `zero_grad → out=net(features) → loss_fn(out, labels) → backward → step`。`CrossEntropyLoss` 接收原始 logits 与词编号，无需独热编码。

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:330](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L330) —— 实际调用：`SGD` 优化器、`lr=0.1`、`CrossEntropyLoss`、10 个 epoch（cell-19）。

```python
def train_epoch(net, dataloader, lr=0.01, optimizer=None,
                loss_fn=torch.nn.CrossEntropyLoss(), epochs=None, report_freq=1):
    optimizer = optimizer or torch.optim.Adam(net.parameters(), lr=lr)
    loss_fn = loss_fn.to(device); net.train()
    for i in range(epochs):
        total_loss, j = 0, 0
        for labels, features in dataloader:
            optimizer.zero_grad()
            features, labels = features.to(device), labels.to(device)
            out = net(features)
            loss = loss_fn(out, labels)
            loss.backward(); optimizer.step()
            total_loss += loss; j += 1
        if i % report_freq == 0:
            print(f"Epoch: {i+1}: loss={total_loss.item()/j}")
    return total_loss.item()/j
```

#### 4.2.4 代码实践

1. **实践目标**：跑通笔记本，亲眼看到「无标注文本 + 极简网络」训出能用的词向量。
2. **操作步骤**：在 `ai4beg` 内核下，从 cell-0 到 cell-19 依次运行；重点观察 cell-19 打印的 10 个 epoch 的 loss。
3. **需要观察的现象**：loss 从约 5.66 缓慢降到约 5.55。
4. **预期结果 / 解读**：loss 看着「很高」，但这正常。5002 个词均匀猜测的交叉熵基线是 \(\log(5002) \approx 8.52\)；训练到 5.55 已显著低于基线，说明模型确实学到了「给定邻居词，中心词分布偏向某些词」。注意 loss 不会接近 0——**仅凭单个邻居词精确预测中心词本来就极难**，而我们的真正目标不是预测准，而是把 `embedder` 训成有语义的向量空间（见 4.3）。
5. 想要更低 loss 可把 epoch 调大、改用 `Adam`、或放开 1 万条新闻的限制（笔记本 cell-17 的 markdown 明确提示「重跑这个 cell 以获得更低 loss」）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Embedding` 和 `Linear` 之间**不加**激活函数（如 ReLU）？加了会怎样？

**参考答案**：不加激活，两层复合仍是「词向量与某权重列的线性内积」，最大化这个内积会把语义相近的词向量拉到同一方向，这正是我们想要的嵌入几何性质。如果插入非线性，前向打分不再是干净的线性内积，词向量之间的关系会被扭曲，最近邻/类比推理这类几何性质就不再成立。换句话说，**CBoW 刻意保留线性结构，是为了让 `embedder` 的权重矩阵成为可直接做向量运算的语义空间**。

**练习 2**：把 `window_size`（造配对时）从 5 改成 1，训练同样的 epoch，预测 `close_words('microsoft')` 结果会如何变化？

**参考答案**：窗口越小，每个词只能看到紧邻的 1 个词，上下文信息变少，嵌入能捕捉的语义更局部、更弱；窗口越大，每个词能看到更广的语境，语义聚类更明显，但配对数量也更多、训练更慢。直觉上 window=1 的最近邻会更嘈杂、语义相关性更差。具体结果「待本地验证」。

**练习 3**：笔记本里 `train_epoch` 同时支持 `optimizer` 参数和 `lr` 参数，二者如何协作？

**参考答案**：`optimizer = optimizer or torch.optim.Adam(...)`——若调用方显式传了 `optimizer`（cell-19 传了 `SGD(lr=0.1)`）就用它，`lr` 参数被忽略；否则用默认的 `Adam`，`lr` 才生效。这是一种「允许调用方覆盖默认优化器」的常见写法。

---

### 4.3 自训练嵌入评估

#### 4.3.1 概念说明

训完 CBoW，我们关心的不是「预测中心词有多准」，而是 **`embedder` 这一层学到了什么**。问题是：词向量是 30 个浮点数，人眼看不出好坏，怎么验证它「真的带语义」？

常见的验证手段有三种，笔记本演示了前两种：

1. **最近邻（nearest neighbors）**：取一个词的向量 \(v\)，在词表里找 L2 距离 \(\lVert w_i - v \rVert\) 最小的若干词。如果 microsoft 的近邻里有 ibm、google、intel 这类科技公司，就说明语义被学进去了。
2. **PCA / t-SNE 降维可视化**：把 30 维向量压到 2 维画散点图，看同类词是否聚成一簇（官方 lab 明确建议）。
3. **类比推理**：上一讲用过的 `king − man + woman ≈ queen`，看线性结构是否成立。

这套评估思路也是上一讲 [u4-l2](./u4-l2-word-embeddings.md) 用 `gensim` 的 `most_similar` 做的事——只不过那时向量是别人训的，本讲是我们自己训的。

#### 4.3.2 核心流程

笔记本的评估流程：

1. **抽取向量**：遍历词表 `vocab.itos`，对每个词编号过一遍 `embedder`，`torch.stack` 拼成 `(词表大小, 30)` 的矩阵 `vectors`。
2. **算距离**：对查询词 \(x\)，算它的向量与所有词向量的 L2 距离。
3. **排序取前 n**：`argsort` 升序、取前 `n` 个，再用 `itos` 把编号转回单词。
4. **人工解读**：看返回的近邻是否语义合理。

距离公式（L2 / 欧氏距离）：

\[
d_i = \lVert w_i - v \rVert = \sqrt{\sum_{k=1}^{30}(w_{i,k} - v_k)^2}
\]

#### 4.3.3 源码精读

**① 抽取全词表向量**：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:388](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L388) —— `vectors = torch.stack([embedder(torch.tensor(vocab[s])) for s in vocab.itos], 0)`（cell-21）。注意是 `embedder`（嵌入层）而不是 `model`（整个网络）——我们要的是查表后的 30 维向量，不是 `vocab_size` 维 logits。

**② 最近邻函数**：

> [lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb:460-465](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L460-L465) —— `close_words`（cell-25）。`np.linalg.norm(vectors - vec, axis=1)` 一次算出查询向量与所有词向量的 L2 距离，`.argsort()[:n]` 取最小 n 个的索引，`itos` 转回单词。

```python
def close_words(x, n=5):
    vec = embedder(torch.tensor(vocab[x]))
    top5 = np.linalg.norm(vectors.detach().numpy() - vec.detach().numpy(),
                          axis=1).argsort()[:n]
    return [vocab.itos[x] for x in top5]
```

**③ 实际结果解读**。笔记本跑了三个查询（cell-25/26/27），结果值得客观看待：

| 查询词 | 返回的 5 个最近邻（含自身） |
|--------|-----------------------------|
| microsoft | microsoft, quoted, lp, rate, top |
| basketball | basketball, lot, sinai, states, healthdaynews |
| funds | funds, travel, sydney, japan, business |

可以看到：**结果明显带上了 AG News 的领域味**（`funds` → `travel/sydney/japan/business` 多为财经新闻高频词），证明嵌入确实学到了「同现上下文」的信息；但近邻里也有不少噪声（如 `sinai`、`healthdaynews`），**质量远不如上一讲加载的谷歌 300 维预训练向量**。

这完全在意料之中——笔记本用了「5000 词表 + 30 维 + 1 万条新闻 + 10 epoch」这个最小配置，目的是让你看清原理，而不是产出工业级向量。要提升质量：扩大语料、提高维度（如 300）、加长训练、调大窗口。官方 lab 的建议正是如此：换一本完整的书做语料，并用 PCA 可视化检验。

> [lessons/5-NLP/15-LanguageModeling/lab/README.md:26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/lab/README.md#L26) —— lab 鼓励探索：嵌入维度如何影响结果？不同文本风格如何影响？取若干词及同义词，做 PCA 降到 2 维画散点，观察是否有规律。

#### 4.3.4 代码实践

1. **实践目标**：用最近邻 + PCA 两种方式检验自训嵌入，建立「好嵌入 vs 坏嵌入」的直觉。
2. **操作步骤**：
   - 运行到 cell-27 后，自己追加查询：`close_words('computer')`、`close_words('game')`、`close_words('company')`，记录近邻。
   - 新增一个 cell，对一组词做 PCA 降到 2 维并画散点（示例代码如下，标注「示例代码」）：

     ```python
     # 示例代码：对自训向量做 PCA 可视化
     from sklearn.decomposition import PCA
     import matplotlib.pyplot as plt

     words = ['computer','game','company','microsoft','sony','ibm','baseball','basketball',
              'stock','market','oil','price']
     idx = [vocab[w] for w in words if w in vocab.itos]
     mat = torch.stack([embedder(torch.tensor(i)) for i in idx]).detach().numpy()
     pts = PCA(n_components=2).fit_transform(mat)
     for w, (x, y) in zip([vocab.itos[i] for i in idx], pts):
         plt.scatter(x, y); plt.text(x, y, w)
     plt.show()
     ```
3. **需要观察的现象**：同类词（如科技公司、球类、财经词）是否在 2 维图上聚得更近。
4. **预期结果**：部分聚类可辨，但边界模糊——这正是「最小配置」应有的表现。
5. **结论**：若聚类不明显，可调大 `embedding_dim`、增加新闻条数、增加 epoch 后重画对比。具体图样「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`close_words` 用 L2 距离，而上一讲 gensim 的 `most_similar` 常用余弦相似度。两者在「找近邻」上等价吗？

**参考答案**：当所有向量都归一化到单位长度时，L2 距离最小等价于余弦相似度最大（可由 \(\lVert a-b\rVert^2 = 2 - 2\cos(a,b)\) 推出）。但 CBoW 训出的向量**没有归一化**，模长各不相同，此时 L2 距离会把「模长差异」也当成语义距离的一部分，可能引入偏差。工业实现普遍用余弦相似度，正是为了只比方向、不比长度。

**练习 2**：为什么评估时必须取 `embedder` 的输出，而不能取整个 `model` 的输出？

**参考答案**：`model` 的末层是 `Linear(30 → vocab_size)`，输出是「对每个词的预测打分（logits）」，维度等于词表大小且含义是「这个输入词后面/附近更可能出现哪些词」，不是词本身的语义表示。`embedder` 输出的 30 维向量才是我们定义的「词嵌入」。两者用途完全不同。

**练习 3**：笔记本结果质量一般，请列出至少 3 条改进方向。

**参考答案**：① 扩大语料（放开 1 万条限制，或换整本书）；② 提高嵌入维度（30 → 300）；③ 增加训练 epoch；④ 增大上下文窗口；⑤ 换用余弦相似度评估；⑥ 升级为 Skip-gram（对低频词更友好，正是 lab 的任务）。

---

## 5. 综合实践

**任务：在自选小语料上，用 CBoW 训出一份属于你自己的词嵌入，并用最近邻 + PCA 检验。**

把本讲三块知识串起来：① 语言模型「遮词预测」的自监督思想 → ② CBoW「上下文预测中心」的任务定义 → ③ 最近邻评估。

步骤：

1. **准备语料**：从 [Project Gutenberg](https://www.gutenberg.org/) 下载一本英文小说（笔记本 lab 建议用 *Alice's Adventures in Wonderland* 或莎士比亚剧本），读成一个大字符串 `text`。
2. **复用笔记本的 `load_dataset` 思路**：用 `basic_english` 分词、`collections.Counter` 建词表（`vocab_size` 可设 2000~5000）。注意你的语料不再是 AG News，需要自己改数据加载。
3. **造 CBoW 配对**：直接复用 [cell-10 的 `to_cbow`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L198-L207)，对全文按句/行切分后逐段调用，`window_size=2~5`。
4. **搭模型并训练**：复用 [cell-8 的两层结构](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L135-L141) 和 [cell-18 的 `train_epoch`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L300-L319)，训练 10~30 个 epoch。
5. **评估**：用 [cell-25 的 `close_words`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/CBoW-PyTorch.ipynb#L460-L465) 查这本书里的角色名（如 `alice`、`queen`）或主题词的近邻；再用 4.3.4 的 PCA 散点看聚类。

**验收标准**：能说出至少 2 个「近邻结果符合这本书语境」的例子（例如《爱丽丝》里 `alice` 的近邻出现其他角色名），并用一句话解释「为什么没有人工语义标签，嵌入也能学到这些关系」。

> 进阶：照 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/15-LanguageModeling/lab/README.md) 把任务方向反过来——用中心词预测邻居（Skip-gram），对比两者在同一语料上的近邻质量。

## 6. 本讲小结

- **语言模型**的核心是给文本打概率 / 预测被遮盖的词；它能用「遮词自造标签」的方式在**海量无标注文本**上自监督训练，这是现代 NLP（含 GPT）的根基。
- **n-gram 模型**用马尔可夫假设（只看前 N−1 个词）数频次估概率，简单但有**数据稀疏、只向后看、无词相似度**三大局限。
- **CBoW** 把分布式假设（上下文相似→语义相似）变成「用上下文预测中心词」的自监督任务，笔记本用 `to_cbow` 把它拆成大量 `(邻居→中心)` 配对。
- 模型极简：`Embedding(vocab_size, 30)` + `Linear(30, vocab_size)`，**两层之间无激活**，刻意保留线性内积结构，好让 `embedder` 权重成为可做向量运算的语义空间——**`embedder` 就是 Word2Vec**。
- 训练循环仍是标准 PyTorch 五件套，损失用 `CrossEntropyLoss(logits, 词编号)`；loss 看着高（≈5.55）但低于均匀基线 \(\log(5002)\approx8.52\) 即说明学到了东西。
- **评估自训嵌入**用最近邻（L2 距离 `close_words`）与 PCA 降维可视化；笔记本的最小配置产出质量一般，要靠扩语料、提维度、加长训练来改进。

## 7. 下一步学习建议

- **纵向深入 NLP**：下一讲 [u4-l4 循环神经网络 RNN](./u4-l4-rnn.md) 会把「语言模型」从「静态词向量」推进到「**逐词处理序列、带隐状态**」的 RNN/LSTM，本讲的 CBoW 任务（预测中心词）会升级为「逐词预测下一个词」的真正序列语言模型。
- **横向补全嵌入三件套**：本讲实现了 CBoW，建议做官方 lab 补上 **Skip-gram**（用中心词预测邻居，对低频词更友好），并对比同一语料下两者的最近邻质量。
- **官方延伸阅读**（README 的 Self-List）：[PyTorch 词嵌入教程](https://pytorch.org/tutorials/beginner/nlp/word_embeddings_tutorial.html)、[TensorFlow Word2Vec 教程](https://www.tensorflow.org/tutorials/text/word2vec)，以及用 `gensim` 几行代码训练常用嵌入的文档。
- **回头印证**：学完本讲后，再翻上一讲 [u4-l2](./u4-l2-word-embeddings.md) 里「加载谷歌 300 维 Word2Vec」那段，你会更清楚那 300 维向量当初是怎么用「海量语料 + CBoW/Skip-gram」训出来的——「加载别人预训练向量」与「自己训出语义向量」就此闭环。
