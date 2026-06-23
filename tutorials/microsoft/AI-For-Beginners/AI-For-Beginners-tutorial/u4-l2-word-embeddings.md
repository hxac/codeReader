# 词嵌入：Word2Vec 与 GloVe

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「词嵌入（word embedding）」相比上一讲词袋（BoW）/TF-IDF 的独热向量解决了哪两个痛点。
- 理解 `nn.Embedding` 为什么可以看成「省去独热向量、直接按词编号查表」的线性层，并能用它搭一个文本分类网络。
- 说出 Word2Vec 的两种训练模式（CBoW 与 Skip-gram）各自在做什么、谁快谁更擅长低频词。
- 看懂课程 Notebook 里 `king - man + woman ≈ queen` 这类「语义向量运算」背后的代码，并能动手复现。
- 知道 GloVe 与 Word2Vec 的核心差别（局部预测 vs 全局共现矩阵分解），以及把预训练向量塞进自己网络时要处理「词表不匹配」的问题。

本讲承接 [u4-l1 文本表示：BoW 与 TF-IDF](./u4-l1-text-representation.md)。上一讲把文本变成了又长又稀疏、且不含语义的词频向量；本讲要让每个词变成一段**短而稠密、且带有语义**的小向量。

## 2. 前置知识

- **独热编码（one-hot）**：用一个长度等于词表大小的向量表示一个词，只有该词对应的位置是 1，其余全是 0。上一讲的词袋向量就是一堆独热向量相加的结果。
- **词表（vocabulary）与 `stoi`/`itos`**：`stoi`（string-to-int）把单词映射成编号，`itos` 反过来。编号是嵌入层查表的索引。
- **线性层（`nn.Linear`）与查表等价**：一个独热向量乘以权重矩阵 \(W\)，结果正好是 \(W\) 的某一列（某一行，取决于约定）。这个「乘法等价于查表」的直觉是理解嵌入层的关键。
- **矩阵乘法复习**：\(\mathbf{e}_i^\top W\) 取出 \(W\) 的第 \(i\) 行，其中 \(\mathbf{e}_i\) 是第 \(i\) 位为 1 的独热向量。
- **余弦相似度**：衡量两个向量方向是否一致，常用于比较词向量的语义相近程度。

如果你对 PyTorch 训练五件套（`zero_grad → 前向 → loss → backward → step`）还不熟，建议先复习 [u2-l5 引入 PyTorch/Keras 框架与过拟合](./u2-l5-frameworks-overfitting.md)。

## 3. 本讲源码地图

本讲只涉及一课目录 `lessons/5-NLP/14-Embeddings/`，关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md) | 课程讲义正文，讲清「为什么需要嵌入」「Word2Vec 两种架构」「上下文嵌入的局限」 |
| [EmbeddingsPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb) | 可执行笔记本：从 `nn.Embedding` 起步，到加载 Word2Vec / GloVe 预训练向量做分类与类比推理 |
| [torchnlp.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/torchnlp.py) | 笔记本依赖的工具函数：`load_dataset` 加载 AG News、`encode` 把文本转编号、`train_epoch_emb` 训练循环 |

> 说明：笔记本在 GitHub 上按单元格（cell）渲染。下文给出的永久链接里，行号是 `.ipynb` 原始文件的行号；我会在链接旁注明对应单元格编号（如「cell-17」），方便你在渲染页对照。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 分布式表示**、**4.2 Word2Vec**、**4.3 GloVe 共现矩阵**。三者层层递进：先用嵌入层把词压成稠密向量 → 再用 Word2Vec 给这些向量注入语义 → 最后用 GloVe 引入全局统计信息。

### 4.1 分布式表示

#### 4.1.1 概念说明

上一讲我们用词袋和 TF-IDF 表示文本，每个词都被表示成一个**长度等于词表大小**的独热向量。笔记本一开头点出了这种做法的两个痛点（cell-0 开篇）：

1. **不省内存**：AG News 的词表大小是 `95812`，每个词的独热向量就有近 10 万维，绝大多数位是 0。
2. **不含语义**：每个词被当成彼此独立的符号，`cat` 和 `dog` 的独热向量互相正交（点积为 0），模型看不出它们语义相近。

**嵌入（embedding）** 的思想是：用一个**低维稠密向量**（比如 32 维或 300 维）来表示一个词，并希望这个向量能「某种程度上反映词的语义」。先不管语义怎么来，本模块只把嵌入理解成一种**降维**手段——把高维独热向量压成低维稠密向量。

一个关键直觉：嵌入层其实就是一个**被查表等价的线性层**。普通线性层接收独热向量 \(\mathbf{e}_i\)，输出 \(\mathbf{e}_i^\top W\)，也就是权重矩阵 \(W\) 的第 \(i\) 行；既然如此，干脆**直接拿词的编号 \(i\) 去查 \(W\) 的第 \(i\) 行**，省去构造巨大的独热向量。这就是 `nn.Embedding` 的来历。

#### 4.1.2 核心流程

把「文本 → 分类」串起来：

```text
原始文本
  │  tokenize（分词）+ stoi（查词表编号）
  ▼
一串词编号 [i1, i2, ..., in]            ← 长度可变
  │  nn.Embedding 查表
  ▼
一串低维词向量 [e1, e2, ..., en]        ← 每个维度如 32/300
  │  聚合（mean / sum / max）成一个文本向量
  ▼
单个文本向量
  │  nn.Linear 分类头
  ▼
类别打分（logits）
```

注意这里文本长度可变，而上一讲的词袋向量长度恒为 `vocab_size`。为了把「多个词的嵌入」聚合成「一个文本向量」，课程用了两种做法：

- **朴素版**：所有序列补零（padding）到等长，再用 `torch.mean(x, dim=1)` 对词维求平均。
- **进阶版（EmbeddingBag）**：把所有样本拼成一条长向量，再用一个「偏移量（offset）」数组标记每条样本的起点，`EmbeddingBag` 一次性完成「查表 + 求平均」。

#### 4.1.3 源码精读

**① README 用一句话点出嵌入层与线性层的血缘关系**：

> [lessons/5-NLP/14-Embeddings/README.md:5-11](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L5-L11) —— 说明独热表示既不省内存也不含语义，引出「用低维稠密向量表示词」的嵌入思想。

具体到嵌入层接受什么输入：

> [lessons/5-NLP/14-Embeddings/README.md:9](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L9) —— 嵌入层很像 `Linear` 层，但它直接接收**词的编号**，从而避免构造巨大的独热向量。这正是上一节「查表等价于乘权重矩阵」的工程化表达。

**② 笔记本里最朴素的嵌入分类网络（cell-3）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:77-86](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L77-L86) —— `EmbedClassifier`：`Embedding(vocab_size, embed_dim)` 查表 → `torch.mean(x, dim=1)` 对一条文本里所有词向量求平均 → `Linear` 分类。

核心三行（摘自上述单元格，为示例呈现）：

```python
class EmbedClassifier(torch.nn.Module):
    def __init__(self, vocab_size, embed_dim, num_class):
        self.embedding = torch.nn.Embedding(vocab_size, embed_dim)
        self.fc = torch.nn.Linear(embed_dim, num_class)
    def forward(self, x):
        x = self.embedding(x)      # 查表：(批量, 词数) -> (批量, 词数, embed_dim)
        x = torch.mean(x, dim=1)   # 聚合：对词维求平均 -> (批量, embed_dim)
        return self.fc(x)          # 分类头 -> (批量, num_class)
```

**③ 进阶版用 `EmbeddingBag` 同时做「查表 + 平均」（cell-10）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:195-201](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L195-L201) —— 把 `nn.Embedding` 换成 `nn.EmbeddingBag`，`forward` 多接收一个偏移量 `off`，省去手动 padding。

**④ 数据与编号从哪来**：笔记本 cell-1 调用的 `load_dataset()` 与 `encode()` 定义在工具文件里。

> [lessons/5-NLP/14-Embeddings/torchnlp.py:12-24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/torchnlp.py#L12-L24) —— `load_dataset` 加载 AG News、用 `collections.Counter` 统计词频、调用 `torchtext.vocab.vocab` 建词表，返回 `vocab`（笔记本里打印出 `Vocab size = 95812`）。

> [lessons/5-NLP/14-Embeddings/torchnlp.py:27-39](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/torchnlp.py#L27-L39) —— `encode` 把一段文本经分词后，逐词查 `stoi` 转成编号；遇到查不到的词返回 `unk=0`。这段代码里还有一个小细节：它同时兼容「新式 `vocab`（`get_stoi()`）」和「GloVe 对象（`.stoi` 属性）」，为 4.3 节加载 GloVe 词表埋下了伏笔。

#### 4.1.4 代码实践

**目标**：直观感受「嵌入把 95812 维压到 32 维」，并验证嵌入分类器能跑通。

**步骤**：

1. 在 `ai4beg` 环境下打开 `EmbeddingsPyTorch.ipynb`，从上到下依次运行到 cell-1，记下打印的 `Vocab size`。
2. 运行 cell-3（定义 `EmbedClassifier`）与 cell-7（用 `embed_dim=32` 训练 25000 条样本）。
3. 在 cell-3 下方**新建一个单元格**，打印可训练参数量（示例代码）：

   ```python
   net = EmbedClassifier(vocab_size, 32, len(classes))
   print(sum(p.numel() for p in net.parameters()))
   ```

**需要观察的现象**：

- 词表大小约为 95812，而嵌入维度只有 32。
- 训练 25000 条后准确率爬升到约 0.75（笔记本输出约 `0.757`），说明仅靠「查表 + 平均 + 线性」就能把 AG News 分到四个类。
- 参数量约等于 `vocab_size × embed_dim + embed_dim × num_class`（约 95812×32 ≈ 306 万），其中绝大部分是嵌入矩阵本身。

**预期结果**：分类准确率随训练稳步上升，最终在 0.75 左右。注意此时这些 32 维向量是**任务驱动学出来的**，尚无强语义——这正是下一节要解决的问题。

> 待本地验证：实际准确率与参数量取决于 torchtext 版本与运行环境；若环境无法联网下载数据集，可改用 README/assignment 建议的自选小语料。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 `nn.Embedding(vocab_size, embed_dim)` 可以用一个线性层 `nn.Linear(vocab_size, embed_dim, bias=False)` 等价替代（在输入是独热向量的前提下）？

**参考答案**：线性层对独热向量 \(\mathbf{e}_i\) 的输出是 \(\mathbf{e}_i^\top W\)，等于取出权重矩阵 \(W\) 的第 \(i\) 行；而 `Embedding(i)` 也正是返回权重矩阵的第 \(i\) 行。两者数学等价，但 `Embedding` 直接以编号 \(i\) 为索引查表，不必真的构造 \(95812\) 维的独热向量，更省内存、更快。

**练习 2**：`EmbedClassifier` 里 `torch.mean(x, dim=1)` 这一步，相当于上一讲词袋模型里的哪一步操作？

**参考答案**：相当于「把一条文本里所有词的向量相加（或求平均）聚合成一个文本向量」。词袋是对独热向量求和得到词频向量；这里是对稠密词向量求平均得到文本向量。两者都丢掉了词序，所以叫「embedding bag」。

---

### 4.2 Word2Vec

#### 4.2.1 概念说明

4.1 节的嵌入向量是**跟着分类任务一起学**的，向量之间并不保证有语义关系——`cat` 和 `dog` 的向量未必靠近。我们希望预先训练出一组向量，让**语义相近的词，其向量在空间里也靠近**（欧氏距离或余弦相似度小）。

**Word2Vec** 就是这类「语义嵌入」的代表作。它建立在一个语言学直觉上——**分布式假设（distributional hypothesis）**：上下文相似的词，语义往往相似。于是可以用「词与上下文的关系」来训练向量：

- **CBoW（Continuous Bag-of-Words，连续词袋）**：给定中心词周围的上下文，**预测中心词**。例如对 5 元组 \((W_{-2},W_{-1},W_0,W_1,W_2)\)，用 \((W_{-2},W_{-1},W_1,W_2)\) 预测 \(W_0\)。
- **Skip-gram（连续跳字）**：方向反过来，用**中心词预测周围上下文**。

两者各有取舍：**CBoW 更快，Skip-gram 更慢但更擅长表示低频词**（因为每个低频词都会作为中心词多次被预测）。

Word2Vec 的迷人之处在于：训练得到的向量空间会自发出现**线性语义结构**，最著名的就是

\[
\mathrm{vec}(\text{king}) - \mathrm{vec}(\text{man}) + \mathrm{vec}(\text{woman}) \approx \mathrm{vec}(\text{queen})
\]

也就是说「国王 − 男人 + 女人 ≈ 女王」，向量运算居然能捕捉「性别」这种语义关系。本模块的代码实践就是复现它。

#### 4.2.2 核心流程

以 **Skip-gram** 为例（CBoW 是镜像）：

```text
对语料中每个中心词 w_t 和它的窗口内上下文 w_{t±k}：
  1. 用中心词 w_t 的向量去预测上下文词
  2. 最大化 log p(w_{上下文} | w_t)
  3. 反向传播更新所有词向量
```

Skip-gram 的目标可写成最大化：

\[
\frac{1}{T}\sum_{t=1}^{T}\sum_{-k\le j\le k,\,j\ne 0} \log p(w_{t+j}\mid w_t)
\]

其中条件概率用两个词向量的点积经 softmax 定义。工程上为加速常用**负采样（negative sampling）** 把多分类简化成二分类，但课程 Notebook 不要求实现训练，只要求**加载预训练向量并使用**。

预训练好以后，使用流程很直接：

```text
加载预训练向量（如 Google News 训练的 300 维 Word2Vec）
  │  对于自己词表里的每个词：
  │    若预训练里有 → 把它的向量填进 Embedding 权重
  │    若没有       → 用随机向量占位
  ▼
得到一个「自带语义初始化」的 Embedding 层，再照常训练分类器
```

#### 4.2.3 源码精读

**① README 给出 Word2Vec 两种架构的正式定义**：

> [lessons/5-NLP/14-Embeddings/README.md:23-38](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L23-L38) —— 说明要在大量文本上预训练才能学到有语义的向量，引出 Word2Vec 的两个架构，并指出 CBoW 与 Skip-gram 的分工。

两条关键定义（摘自 README）：

> [lessons/5-NLP/14-Embeddings/README.md:29-30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L29-L30) —— CBoW 用上下文 \((W_{-2},W_{-1},W_1,W_2)\) 预测中心词 \(W_0\)；Skip-gram 反过来用中心词预测上下文。

> [lessons/5-NLP/14-Embeddings/README.md:32](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L32) —— 一句话总结取舍：CBoW 更快，Skip-gram 更慢但更擅长表示低频词。

**② 用 gensim 加载 Google News 预训练 Word2Vec（cell-16）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:330](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L330) —— `api.load('word2vec-google-news-300')` 下载并加载一个 300 维的预训练模型（首次下载较慢）。

**③ 验证「语义相近」：查 `neural` 最近邻（cell-17）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:356](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L356) —— `w2v.most_similar('neural')` 返回与 `neural` 最相近的词。笔记本输出 `neuronal / neurons / neural_circuits / neuron …`，全是「神经」语义簇，这正是 4.1 节独热向量给不出的语义相似度。

**④ 经典类比：king − man + woman ≈ queen（cell-21）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:415](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L415) —— `w2v.most_similar(positive=['king','woman'], negative=['man'])[0]`，即找最接近 `king + woman − man` 的词，结果返回 `('queen', 0.7118…)`。

`most_similar` 的 `positive`/`negative` 参数含义就是「加这些向量、减那些向量，再找最近邻」。

**⑤ 把预训练向量灌进 PyTorch 嵌入层（cell-24）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:462-470](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L462-L470) —— 遍历自己词表 `vocab.get_itos()`：命中则 `net.embedding.weight[i].data = w2v.get_vector(w)`；未命中则用正态随机向量 `torch.normal(0.0,1.0,…)` 占位。笔记本打印 `found 41080 words, 54732 words missing`——超过一半词查不到，这正是「词表不匹配」问题。

> [lessons/5-NLP/14-Embeddings/README.md:38](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md#L38) —— README 明确提醒：预训练词表与自己的语料词表很可能不一致，需要处理这个问题（Notebook 给出了「随机填充」与「直接换用预训练词表」两种思路）。

#### 4.2.4 代码实践（本讲主实践）

**目标**：用预训练 Word2Vec 复现 `king − man + woman ≈ queen`，并体会语义向量的可运算性。

**步骤**：

1. 在 `ai4beg` 环境运行笔记本到 cell-16，加载 `word2vec-google-news-300`（首次需联网下载约 1.6 GB，请耐心等待）。
2. 运行 cell-21，确认输出为 `queen`。
3. 在其后**新建单元格**，自行设计几组类比（示例代码）：

   ```python
   # 示例代码：用 most_similar 做类比推理
   analogies = [
       (['king', 'woman'], ['man']),       # 国王-男人+女人 ≈ ?
       (['paris', 'italy'], ['france']),   # 巴黎-法国+意大利 ≈ ?（首都关系）
       (['big', 'biggest'], ['small']),    # big-biggest+small ≈ ?
   ]
   for pos, neg in analogies:
       print(pos, neg, '->', w2v.most_similar(positive=pos, negative=neg)[0])
   ```

**需要观察的现象**：

- `king − man + woman` 最近邻确实是 `queen`，相似度约 0.71。
- 首都类比往往得到 `rome`，最高级类比往往得到 `smallest`——向量空间「学会」了这些关系。
- 部分类比可能不准，这说明预训练向量并非万能，关系是否被捕捉取决于语料和词频。

**预期结果**：第一组稳定返回 `queen`；其余两组大概率命中，但不保证 100%。把命中和未命中的都记下来。

> 待本地验证：不同 gensim 版本下载源可能失效；若无 GPU/大内存，加载 300 维 Google News 模型可能较慢，可改用更小的 `glove-wiki-gigaword-50` 验证流程。

#### 4.2.5 小练习与答案

**练习 1**：CBoW 和 Skip-gram 都基于「分布式假设」，请用一句话解释什么是分布式假设。

**参考答案**：上下文（周围共现的词）相似的词，语义往往也相似；因此可以靠「词的上下文分布」来推断词义，并把这种分布编码进向量。

**练习 2**：为什么说 Skip-gram 比 CBoW 更适合低频词？

**参考答案**：Skip-gram 把每个词都当成中心词，分别去预测它的多个上下文，于是**每个词（包括低频词）都会作为训练样本被多次更新**；CBoW 则是把多个上下文词「平均」后再预测中心词，低频词容易被高频上下文淹没，得到的有效更新更少。

**练习 3**：把预训练 Word2Vec 灌入自己的 `Embedding` 层后，笔记本的准确率并没有明显提升（cell-26 仍约 0.75）。结合 cell-24 的 `found/missing` 统计，说明原因。

**参考答案**：因为自己的语料词表里**超过一半（54732/95812）的词在预训练词表中查不到**，只能用随机向量占位，这些词对分类几乎没有贡献；同时分类任务的目标和 Word2Vec 的语义目标并不完全一致，所以「自带语义初始化」带来的收益被词表不匹配严重稀释。这正是下一节/后续需要靠「统一词表」或「自训练嵌入」来解决的问题。

---

### 4.3 GloVe 共现矩阵

#### 4.3.1 概念说明

Word2Vec 是**预测式（predictive）**方法：它只在局部窗口里做「预测中心词/上下文」这件事，每次只看一小撮词，**没有利用全局统计信息**。

**GloVe（Global Vectors）** 换了个思路：先在整个语料上统计一张**词共现矩阵（co-occurrence matrix）** \(X\)，其中 \(X_{ij}\) 表示词 \(i\) 与词 \(j\) 在某个窗口内共同出现的次数（或加权次数）；然后用**矩阵分解**得到每个词的低维向量，使得两个词向量的点积能「解释」它们的共现强度。

笔记本 cell-22 对此有一句精炼概括：

> **GloVe**, leverages the idea of co-occurence matrix, uses neural methods to decompose co-occurrence matrix into more expressive and non linear word vectors.

一句话区分：**Word2Vec 用局部窗口做预测，GloVe 用全局共现矩阵做分解**。两者最终得到的向量用法完全一样——都能做 `king − man + woman ≈ queen` 这类语义运算，也都能塞进 `Embedding` 层。

补充一个常被一起提及的 **FastText**：它在 Word2Vec 基础上额外学习**字符 n-gram** 的向量并求平均，从而能编码「子词」信息，对形态丰富或含未登录词的语言更友好（见 [EmbeddingsPyTorch.ipynb:424-428](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L424-L428)）。

#### 4.3.2 核心流程

GloVe 的训练目标可理解为：让词向量的点积去拟合（对数）共现次数。简化形式为

\[
J = \sum_{i,j} f(X_{ij})\left(\mathbf{w}_i^\top \tilde{\mathbf{w}}_j + b_i + \tilde{b}_j - \log X_{ij}\right)^2
\]

其中 \(\mathbf{w}_i\)、\(\tilde{\mathbf{w}}_j\) 是词 \(i\) 与词 \(j\) 的向量（中心词与上下文词各一组），\(b\) 是偏置，\(f(X_{ij})\) 是权重函数（压制极高频共现对的影响）。直观说：**两个词共现越频繁，它们的向量点积就应该越大**。

在课程里我们**不需要自己实现** GloVe，而是直接用 `torchtext` 内置的预训练 GloVe 词表，它把「词表 + 向量矩阵」打包好了：

```text
torchtext.vocab.GloVe(name='6B', dim=50)
  │  返回一个对象：
  │    stoi   —— 词 -> 编号
  │    itos   —— 编号 -> 词
  │    vectors —— (词表大小, 50) 的向量矩阵
  ▼
直接把 vectors 复制进 nn.Embedding 的权重即可
```

相比 4.2 节「逐词 try/except 填充」，这里因为「词表与向量来自同一份 GloVe」，**天然不存在词表不匹配**，加载非常干净。

#### 4.3.3 源码精读

**① 笔记本对 GloVe / FastText 的定位（cell-22）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:424-428](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L424-L428) —— 指出 Word2Vec/CBoW/Skip-gram 都是「预测式、只看局部上下文」；GloVe 借助共现矩阵、用神经网络方法将其分解为更有表达力的非线性词向量；FastText 则加入字符 n-gram 编码子词信息。

**② 用 torchtext 一行加载 GloVe（cell-28）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:542](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L542) —— `vocab = torchtext.vocab.GloVe(name='6B', dim=50)` 加载用 60 亿词训练、50 维的 GloVe。得到的 `vocab` 自带 `stoi`/`itos`/`vectors`。

**③ 手算 king − man + 1.3×woman，再找最近邻（cell-30）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:575](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L575) —— 先算目标向量 `qvec = king − man + 1.3×woman`，再算它与所有词向量的平方距离 `d = sum((vectors − qvec)^2, dim=1)`，取 `argmin` 得到编号，最后 `itos` 转回单词。结果输出 `'queen'`。

这一格相当于**手动实现**了 `most_similar`：用欧氏距离最近邻替代余弦相似度。作者特意提到「不得不微调系数（1.3）才能算出 queen」，说明 GloVe 的向量空间里这个关系并非严格线性，需要一点缩放。

**④ 一行把 GloVe 向量灌进嵌入层（cell-34）**：

> [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb:623](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb#L623) —— `net.embedding.weight.data = vocab.vectors`。因为词表和向量来自同一份 GloVe，直接整块赋值即可，无需 4.2 节的 try/except 逐词填充。

配套地，数据加载要改用 GloVe 的词表来编码（cell-32 的 `offsetify` 把 `encode(t[1], voc=vocab)` 里的 `vocab` 换成 GloVe 词表），这正用到了 4.1.3 节提到的、`encode` 函数对 `get_stoi()`/`.stoi` 的双兼容（[torchnlp.py:27-39](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/torchnlp.py#L27-L39)）。

#### 4.3.4 代码实践

**目标**：用 GloVe 词表复现一次类比推理，并体会「整块赋值」比逐词填充更省事。

**步骤**：

1. 运行笔记本到 cell-28，加载 `GloVe(name='6B', dim=50)`（首次会下载向量文件）。
2. 运行 cell-30，确认输出 `'queen'`。
3. 在其后**新建单元格**，改写 cell-30 的逻辑，把「欧氏距离最近邻」换成「余弦相似度最高」（示例代码）：

   ```python
   # 示例代码：用余弦相似度做类比推理
   import torch.nn.functional as F
   qvec = vocab.vectors[vocab.stoi['king']] - vocab.vectors[vocab.stoi['man']] \
          + vocab.vectors[vocab.stoi['woman']]
   cos = F.cosine_similarity(vocab.vectors, qvec.unsqueeze(0))  # 与所有词算余弦
   idx = torch.argmax(cos).item()
   print('类比结果：', vocab.itos[idx])
   ```

**需要观察的现象**：

- 用欧氏距离（cell-30 原版）需要把 `woman` 系数调到 1.3 才得到 `queen`。
- 换成余弦相似度后，通常**不必调系数**就能直接得到 `queen`——因为余弦只看方向不看长度，对系数缩放不敏感。

**预期结果**：两种度量都能定位到 `queen` 或其近义词；余弦版通常更稳健。这也解释了为什么业界比较词向量相似度时更常用余弦相似度。

> 待本地验证：`torchtext.vocab.GloVe` 的下载源在不同版本/网络环境下可能失败；若无法下载，可改为在 [GloVe 项目页](https://nlp.stanford.edu/projects/glove/) 手动下载 `glove.6B.50d.txt` 并自行解析。

#### 4.3.5 小练习与答案

**练习 1**：用一句话区分 Word2Vec 与 GloVe 在「信息来源」上的差别。

**参考答案**：Word2Vec 是预测式方法，只利用**局部上下文窗口**里的预测信号；GloVe 先统计整个语料的**全局词共现矩阵**，再通过矩阵分解得到词向量，用上了全局统计信息。

**练习 2**：为什么 cell-34 只需一行 `net.embedding.weight.data = vocab.vectors` 就能完成加载，而 cell-24 却要写一个 try/except 循环？

**参考答案**：因为 cell-34 用的词表和向量**来自同一份 GloVe**，词表顺序与 `vectors` 行顺序完全一致，可以直接整块赋值；而 cell-24 是把「Google News 的 Word2Vec 向量」填进「自己语料构建的词表」，两套词表不一致，必须逐词查找，查不到的词只能随机填充，所以需要循环和异常处理。

**练习 3**：cell-30 里作者要把 `woman` 的系数从 1 调到 1.3 才得到 `queen`，这反映了向量空间的什么性质？

**参考答案**：说明 GloVe 向量空间里 `king − man + woman` 的方向虽然指向 `queen`，但**量级/尺度并不完全对齐**，加减运算后的点未必恰好落在 `queen` 的最近邻上；需要微调系数。改用余弦相似度（只比较方向）通常能缓解这个问题，这也解释了上一题实践中余弦版更稳健的现象。

---

## 5. 综合实践

把三个模块串成一个完整小任务：**用一份预训练向量，把「语义嵌入」真正接进一个文本分类流程，并对比三种初始化方式。**

任务步骤：

1. **准备**：在 `ai4beg` 环境运行笔记本到 cell-7（随机初始化的嵌入分类器，准确率约 0.75），记为「基线 A：随机嵌入」。
2. **接入 Word2Vec**：运行 cell-16 到 cell-26（逐词填充 Google News Word2Vec），记为「方案 B：Word2Vec 填充」。观察准确率（仍约 0.75）和 `found/missing` 统计。
3. **接入 GloVe**：运行 cell-28 到 cell-36（直接用 GloVe 词表 + 整块赋值），记为「方案 C：GloVe 整表」。注意此时词表已换成 GloVe，准确率约 0.75。
4. **分析**：写一段 200 字左右的对比，回答：
   - 为什么三种方案准确率都很接近（约 0.75）？（提示：AG News 是主题分类，词袋信号已足够；且都用了 mean 聚合、丢失词序。）
   - 方案 B 和方案 C 在「词表不匹配」上各自怎么处理？哪个更干净？
5. **延伸**（可选）：把聚合函数从 `mean` 改成 `sum` 或 `max`（修改 `EmbeddingBag` 的 `mode` 参数或 `torch.mean`），重新训练方案 C，观察准确率变化。

> 这一任务的关键收获不在「提升准确率」，而在理解：**嵌入层是分类网络的第一层、预训练向量是给它的一个好初始化、而词表不匹配是落地预训练向量时的头号障碍。**

## 6. 本讲小结

- **嵌入 = 低维稠密向量表示词**。`nn.Embedding` 本质是「按词编号查权重矩阵某一行」的线性层，省去了构造巨大独热向量，解决了独热表示「高维、稀疏、无语义」的痛点。
- **聚合把变长文本压成定长向量**。朴素做法是 padding 后 `mean`，进阶做法是 `EmbeddingBag` + 偏移量，都丢失词序（故称 embedding bag）。
- **Word2Vec 让向量带语义**。基于分布式假设，CBoW 用上下文预测中心词（更快），Skip-gram 用中心词预测上下文（更擅长低频词）；训练出的向量支持 `king − man + woman ≈ queen` 这类线性语义运算。
- **GloVe 用全局共现矩阵分解**，弥补 Word2Vec 只看局部窗口的不足；用法与 Word2Vec 一致，且在 `torchtext` 里「词表+向量」打包好，可整块赋值。
- **落地预训练向量的头号障碍是词表不匹配**。课程演示了两种对策：逐词 try/except 填充（4.2）、直接改用预训练词表编码（4.3）。
- **静态嵌入有固有局限**：一词多义（如 `play`）无法区分，需上下文相关嵌入（BERT 等），那是后续语言模型课程的内容（见 README 末尾 Contextual Embeddings 一节）。

## 7. 下一步学习建议

- **横向阅读**：读本课 [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md) 的「Contextual Embeddings」一节与原始论文 [Efficient Estimation of Word Representations in Vector Space](https://arxiv.org/pdf/1301.3781.pdf)，理解 Word2Vec 的设计动机与静态嵌入的一词多义局限。
- **纵向继续**：下一讲 [u4-l3 语言模型与训练自己的嵌入](./u4-l3-language-modeling.md) 会进入 `15-LanguageModeling`，亲手用 **CBoW 从零训练**自己的词嵌入（对应 `CBoW-PyTorch.ipynb`），把本讲「加载别人预训练向量」升级为「自己训出语义向量」。
- **动手挑战**：完成本课 [assignment](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/assignment.md)——用自选语料（如 Kaggle 歌词数据集）重跑笔记本，对比 Word2Vec 与 GloVe 在你的语料上的最近邻与类比结果。
