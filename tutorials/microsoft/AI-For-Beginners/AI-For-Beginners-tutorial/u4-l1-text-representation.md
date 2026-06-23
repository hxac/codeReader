# 文本表示：BoW 与 TF-IDF

## 1. 本讲目标

本讲是「自然语言处理（NLP）」单元的第一课。前面几个单元我们喂给神经网络的都是**定长**输入：表格的一行、一张 (H,W,3) 的图像。而文本是**变长序列**，长度未知、且词与词之间还有顺序和上下文关系。神经网络只能吃数字，所以在进入任何「文本神经网络」之前，必须先解决一个前置问题：**怎么把一句话变成一个固定长度的数字向量？**

学完本讲你应该能够：

- 说出 NLP 这一单元整体要解决哪些任务、学习路线是怎样的；
- 把一段文本「分词（tokenize）→ 建词表（vocabulary）→ 编码成数字」；
- 手写一个词袋（Bag-of-Words, BoW）向量，并理解它的「只记词频、丢了顺序」的代价；
- 用 TF-IDF 公式给词频加权，压低 `the / and / is` 这类「到处都出现」的无信息词的权重；
- 在 Notebook 里用 `TfidfVectorizer` 把短文本向量化，并计算两条文本的余弦相似度。

本讲的训练循环本身并不新——它和上一阶段（讲义 u2-l5）里讲过的 PyTorch「五件套」训练循环、`LogSoftmax + NLLLoss` 分类是同一套东西。**新东西只有一个：输入从图像/表格换成了文本向量。** 所以本讲的重心是「文本→向量」这一步。

## 2. 前置知识

在进入正文前，先确认几个概念你已经熟悉（不熟悉的可以回头翻对应讲义）：

- **张量（tensor）与神经网络分类**：本讲会复用讲义 u2-l5 里学过的 PyTorch 训练循环（`zero_grad → forward → loss → backward → step`）和多分类输出激活与损失的配对（`LogSoftmax` 配 `NLLLoss`）。
- **词（word）与字符（character）**：一段文本既可以按「字」拆，也可以按「词」拆。英文按空格分词比较自然；中文则需要专门的分词工具。
- **独热编码（one-hot）**：把一个离散类别（比如一个词）表示成一个「只有一个位置是 1、其余全是 0」的长向量。本讲的 BoW 可以理解为「句子里所有词的独热向量加起来」。
- **词频（frequency）**：一个词在一段文本里出现的次数。

如果上面这些你都大致有印象，就可以继续了。

## 3. 本讲源码地图

本讲涉及三个核心文件：

| 文件 | 作用 |
| --- | --- |
| [lessons/5-NLP/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md) | NLP 单元总览：列出本单元要解决哪些 NLP 任务、为什么文本需要新网络结构、本单元 6 课的学习路线。 |
| [lessons/5-NLP/13-TextRep/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md) | 第 13 课讲义：讲「字符级 vs 词级」表示、N-Grams、BoW 与 TF-IDF 的基本思想。 |
| [lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb) | 可执行 Notebook：在 AG News 数据集上完成分词、建词表、BoW、训练一个线性分类器，最后演示 TF-IDF。 |

记住一个口诀：**README 讲「为什么」，Notebook 讲「怎么敲」。** 看不懂概念先翻 README，想跑代码就打开 Notebook。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**NLP 任务总览**、**词袋模型（BoW）**、**TF-IDF**。

### 4.1 NLP 任务总览

#### 4.1.1 概念说明

自然语言处理（Natural Language Processing, NLP）是让计算机处理人类语言（文本/语音）的技术。本单元 README 开篇就列举了它要解决的一大类任务，其中与本讲后面几课直接相关的有：

- **文本分类（text classification）**：把一段文本归到某个类别，比如新闻是「体育 / 科技 / 财经 / 世界」哪一类；聊天机器人里判断用户「想干嘛」的**意图分类**也是它。
- **情感分析（sentiment analysis）**：给句子打一个「正面/负面」的分数，本质是回归问题。
- **命名实体识别（NER）**：从句子里抠出人名、地名、日期等实体。
- **文本生成 / 机器翻译 / 摘要 / 问答**：这些更高级的任务本单元后面会逐步接触到。

> 这一段任务清单在源码里直接列着：[lessons/5-NLP/README.md:5-16](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md#L5-L16) 用一段 bullet 把文本分类、情感分析、NER、关键词抽取、文本聚类、问答、文本生成、摘要、机器翻译逐一列出。

为什么文本要单独开一个单元、而不是继续用前几单元的网络？README 给出的关键原因是：

> 文本是**变长序列**，而图像的输入尺寸是提前已知的；而且文本里的模式更复杂——比如否定词和它修饰的对象之间可以隔任意多个词（`I do not like oranges` vs `I do not like those big colorful tasty oranges`），网络仍要把它们当成同一个模式来理解。因此要引入**循环网络**和 **Transformer** 这类新结构。见 [lessons/5-NLP/README.md:23](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md#L23)。

#### 4.1.2 核心流程：本单元的学习路线

README 末尾给出了本单元 6 课的目录顺序，这就是学习路线：

1. **第 13 课 文本表示**（本讲）：把文本变成向量——BoW、TF-IDF。
2. **第 14 课 词嵌入**：Word2Vec / GloVe，让向量带「语义」。
3. **第 15 课 语言建模**：n-gram、自己训练嵌入。
4. **第 16 课 循环神经网络（RNN）**：处理顺序。
5. **第 17 课 生成式网络**：用 RNN 生成文本。
6. **第 18 课 Transformer**：自注意力。

> 这条路线在 [lessons/5-NLP/README.md:58-66](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md#L58-L66) 的 `## In this Section` 里列着。

可以把它看成一条「表示能力逐步增强」的阶梯：**BoW/TF-IDF（本讲，无语义无顺序）→ 词嵌入（有语义无顺序）→ RNN（有顺序）→ Transformer（有顺序 + 全局注意力）**。本讲是这条阶梯最底层的「笨办法」，但它是后面所有方法的地基。

#### 4.1.3 源码精读

第 13 课 README 一开头就点明：本单元前半部分会围绕**文本分类**任务展开，使用 **AG News** 数据集（把新闻分成 World / Sports / Business / Sci-Tech 四类）。这是贯穿本讲和下一讲的「样本任务」。见 [lessons/5-NLP/13-TextRep/README.md:5-13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L5-L13)。

#### 4.1.4 代码实践

**实践目标**：建立对 NLP 单元全局的认知，不写代码。

**操作步骤**：

1. 打开 [lessons/5-NLP/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md)，通读 L5–L23 的任务清单与「为什么文本需要新网络」的段落。
2. 跳到 L58–L66 的 `## In this Section`，把 6 课的标题抄下来。

**需要观察的现象**：你会看到任务清单里既有「分类」这种我们熟悉的任务，也有「生成 / 翻译 / 摘要」这种前几单元没出现过的新任务类型。

**预期结果**：能用一句话回答「为什么文本不能直接套用前面的 CNN？」

**答案**：因为文本是变长序列，且关键模式（如否定）可能跨任意距离，CNN 的局部卷积不足以捕捉，需要 RNN / Transformer 这类专门处理序列的结构。

#### 4.1.5 小练习与答案

**练习 1**：情感分析和文本分类，哪个是回归问题、哪个是分类问题？

**参考答案**：文本分类是分类问题（输出离散类别）；情感分析是回归问题（输出一个表示正/负程度的连续数值），见 [README.md:7-8](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/README.md#L7-L8)。

**练习 2**：本单元 6 课里，哪一课才开始真正解决「词的顺序」问题？

**参考答案**：第 16 课 RNN。BoW/TF-IDF（13）和词嵌入（14）都不含顺序信息，RNN 才引入对序列顺序的建模。

---

### 4.2 词袋模型（BoW）

#### 4.2.1 概念说明

要让神经网络吃文本，第一步是把文本变成「定长向量」。第 13 课 README 给出两种基本粒度：

- **字符级表示（character-level）**：把每个字符当一个类别，词 `Hello` 表示成 5×C 的独热矩阵（C 是字符表大小）。
- **词级表示（word-level）**：先建一个覆盖所有词的**词表（vocabulary）**，每个词用独热编码表示。比字符级更好，因为「单个字母几乎没含义」，而词是有语义的最小单位；代价是词表很大、向量又长又稀疏。

> 见 [lessons/5-NLP/13-TextRep/README.md:25-30](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L25-L30)。

无论哪种粒度，流程都分三步：**先把文本切成一个个 token（分词），再用词表把 token 映射成数字，最后用独热编码喂给网络。**

但独热编码有个问题：它保留了顺序、且每个词独占一维，整句话就是一个「长度×词表大小」的大矩阵，对分类任务来说太大了。于是有了最朴素的压缩办法——**词袋（Bag-of-Words, BoW）**：把句子里每个词的独热向量**加起来**，得到一个长度等于词表大小的「词频向量」，记录「每个词出现了几次」，而**完全丢掉顺序**。

> README 的定义见 [lessons/5-NLP/13-TextRep/README.md:38-46](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L38-L46)：「BoW 本质上表示哪些词出现在文本里、各出现了几次，这往往能很好地反映文本在讲什么」。

为什么「丢了顺序」还能用？因为很多分类任务里，**关键词本身就足够 indicative**：出现 `stocks / dollar` 大概率是财经新闻，出现 `weather / snow` 大概率是天气预报。词频在很多场景下是文本内容的良好指示。

#### 4.2.2 核心流程：从一句话到一个 BoW 向量

```
原始文本 "I love to play with my words"
   │ ① 分词 tokenizer
   ▼
['i', 'love', 'to', 'play', 'with', 'my', 'words']
   │ ② 查词表 stoi (token → 编号)
   ▼
[599, 3279, 97, 1220, 329, 225, 7368]
   │ ③ 统计每个编号出现次数（词频）
   ▼
长度 = vocab_size 的稀疏向量，res[599]=1, res[3279]=1, ...
```

三个关键函数：

- **tokenizer**：把字符串切成 token 列表。
- **vocab / stoi**：词表，以及「token → 编号」的字典。
- **to_bow**：把一段文本变成词频向量。

BoW 的本质可以用一个求和公式概括——把句子里每个词的独热向量 \(\mathrm{onehot}(w)\) 相加：

\[
\mathrm{BoW}(\text{text}) = \sum_{w \in \text{text}} \mathrm{onehot}(w)
\]

结果向量的第 \(i\) 维就是「编号为 \(i\) 的词在文本里出现的次数」。

#### 4.2.3 源码精读

**① 分词器**：用 torchtext 的 `basic_english` 分词器，它会把文本转小写、按空格和标点切开。

> [TextRepresentationPyTorch.ipynb:134](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L134)：`tokenizer('He said: hello')` 输出 `['he', 'said', 'hello']`，注意大小写被归一化、冒号被去掉。

**② 建词表 + encode**：先用 `collections.Counter` 统计训练集所有 token 的频次，再用 `torchtext.vocab.vocab` 建词表；`encode` 函数把文本里的每个 token 经 `stoi` 字典转成编号。

> [TextRepresentationPyTorch.ipynb:181-189](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L181-L189)：AG News 训练集词表大小为 **95810**，`encode('I love to play with my words')` 得到 `[599, 3279, 97, 1220, 329, 225, 7368]`。

**③ 用 scikit-learn 演示 BoW**：`CountVectorizer` 是工业界做 BoW 的事实标准工具。对三句话构成的小语料 `fit_transform` 后，再 `transform` 一句新话，得到词频向量。

> [TextRepresentationPyTorch.ipynb:226-234](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L226-L234)：对 `'My dog likes hot dogs on a hot day.'` 输出 `[[1, 1, 0, 2, 0, 0, 0, 0, 0]]`——注意 `hot` 出现 2 次，对应位置是 2，这就是 BoW 的「词频」。

**④ 手写 to_bow**：在 AG News 上自己实现一遍 BoW——开一个全零向量，对文本里每个 token 编号把对应位置 `+1`。

> [TextRepresentationPyTorch.ipynb:258-267](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L258-L267)：`to_bow` 先建 `torch.zeros(vocab_size)`，再循环 `res[i] += 1`，对第一条新闻输出 `tensor([2., 1., 2., ..., 0., 0., 0.])`。

**⑤ 把 BoW 接进 PyTorch 训练循环**：这是本讲与讲义 u2-l5 的「衔接点」。`bowify` 作为 `collate_fn` 传给 `DataLoader`，把一个 minibatch 的 `(label, text)` 元组列表转成 `(标签张量, BoW 特征张量)` 这一对。

> [TextRepresentationPyTorch.ipynb:295-305](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L295-L305)：`bowify` 里 `torch.stack([to_bow(t[1]) for t in b])` 把每条文本的 BoW 向量堆成一个 batch；标签做了 `t[0]-1`（把 AG News 的 1~4 类映射成 0~3，配合 `NLLLoss` 的下标从 0 开始）。

随后网络只是一个线性层 + `LogSoftmax`，训练循环 `zero_grad → forward → NLLLoss → backward → step` 与 u2-l5 完全一致，只跑了一个 epoch 的前 15000 条样本就达到约 **86%** 训练准确率。这说明：**哪怕丢了顺序，光靠词频向量 + 一个线性层，文本分类也能达到不错的基线。**

#### 4.2.4 代码实践

**实践目标**：亲手体会 BoW「只记词频、丢了顺序」的特性。

**操作步骤**：

1. 在 `ai4beg` 内核下打开 `lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb`，从上到下依次运行到 `to_bow` 那个 cell（cell-16）。
2. 在 cell-16 下方新增一个 cell（示例代码），验证「丢了顺序」：

```python
# 示例代码：验证 BoW 不区分顺序
a = "stocks dollar weather snow"
b = "snow weather dollar stocks"
print(torch.equal(to_bow(a), to_bow(b)))   # 预期 True
```

3. 再加一个 cell（示例代码），观察「词频」被如实记录：

```python
# 示例代码：观察词频
v = to_bow("dog dog dog cat")
print(v[v.nonzero()])   # 应能看到 3 和 1 两个非零值
```

**需要观察的现象**：调换词序后两个 BoW 向量完全相等；`dog` 出现 3 次，对应维度的值是 3。

**预期结果**：第 2 步输出 `True`，证明 BoW 对顺序无感；第 3 步能看到非零值 `3.` 和 `1.`。

**待本地验证**：`v.nonzero()` 返回的具体下标取决于 `dog`/`cat` 在词表里的编号，因运行环境而异，但非零值大小应是 3 和 1。

#### 4.2.5 小练习与答案

**练习 1**：`'I do not like oranges'` 和 `'oranges like not do I'` 的 BoW 向量是否相同？这说明 BoW 丢失了什么信息？

**参考答案**：完全相同（只要分词结果一致）。这说明 BoW 丢失了**词序**，因此也无法表达「否定」这类靠顺序/结构才成立的语义。

**练习 2**：AG News 词表有 95810 个词，每条新闻的 BoW 向量有多长？其中大部分元素是什么？

**参考答案**：长度就是 95810；因为一条新闻只含几十个词，所以绝大部分元素是 0——这就是 README 说的「高维稀疏（high-dimensional sparse）」向量。

**练习 3**：Notebook 提到可以「降低 `vocab_size` 只保留高频词」。这样做的主要好处和代价分别是什么？

**参考答案**：好处是向量变短、计算和内存更省；代价是丢掉了低频但可能有区分度的词，准确率会略有下降（Notebook 注释说「下降但不剧烈」）。见 [TextRepresentationPyTorch.ipynb:274](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L274)。

---

### 4.3 TF-IDF

#### 4.3.1 概念说明

BoW 有个明显缺点：**所有词一视同仁**。但 `the / and / is` 这类「停用词」几乎每篇文档都出现、词频还最高，会淹没真正有区分度的词（`collider / president`）。README 是这样描述问题的：

> 「BoW 的问题在于 `and`、`is` 等常见词在大多数文本里都出现且频次最高，盖住了真正重要的词。我们可以通过考虑词在整个文档集合里出现的频次来降低它们的重要性——这就是 TF-IDF 的核心思想。」见 [lessons/5-NLP/13-TextRep/README.md:48](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L48)。

**TF-IDF（Term Frequency – Inverse Document Frequency，词频-逆文档频率）** 就是给 BoW 的词频打个「权重折扣」：

- 一个词在**本篇文档**里出现越多（TF 大）→ 越重要；
- 一个词在**所有文档**里都出现（DF 大）→ 越没信息量，要压低权重。

直觉上：`the` 在每篇文档都出现，DF 接近文档总数，权重应趋近 0；`collider` 只在少数科技文档出现，DF 小，权重大。

#### 4.3.2 核心流程：TF-IDF 权重公式

词 \(i\) 在文档 \(j\) 中的权重定义为：

\[
w_{ij} = tf_{ij}\times\log\!\left(\frac{N}{df_i}\right)
\]

其中：

- \(tf_{ij}\)：词 \(i\) 在文档 \(j\) 中出现的次数（就是 BoW 的值）；
- \(N\)：文档集合里的文档总数；
- \(df_i\)：整个集合中「包含词 \(i\)」的文档数。

两个极端便于记忆：

- 若词 \(i\) 出现在**每一篇**文档里，则 \(df_i = N\)，\(\log(N/N)=\log 1 = 0\)，于是 \(w_{ij}=0\)——**到处都有的词被彻底抹掉**。
- 若词 \(i\) 只出现在**极少数**文档里，\(df_i\) 很小，\(\log(N/df_i)\) 很大，权重被放大——**稀有词被抬高**。

这正是 Notebook 里那段公式和说明的含义：见 [TextRepresentationPyTorch.ipynb:490-499](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L490-L499)。

整个流程相对 BoW 多一步「全局统计」：

```
语料所有文档
   │ ① 统计每个词出现在多少篇文档里 → df_i
   │ ② 数文档总数 → N
   ▼
对每篇文档的每个词：
   │ ③ 词频 tf_ij × log(N / df_i) → w_ij
   ▼
TF-IDF 向量（仍是定长、仍丢顺序，但词频被加权）
```

#### 4.3.3 源码精读

Notebook 用 scikit-learn 的 `TfidfVectorizer` 一行完成 TF-IDF：

> [TextRepresentationPyTorch.ipynb:524-527](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L524-L527)：`TfidfVectorizer(ngram_range=(1,2))` 先对三句话小语料 `fit_transform`，再 `transform` 新句子，得到一个浮点向量。注意它和 BoW（整数词频）的区别——这里全是小数，且 `ngram_range=(1,2)` 表示同时把单词和二元词组都当作特征。

把同一句 `'My dog likes hot dogs on a hot day.'` 喂给 BoW 和 TF-IDF，对比一下：

- **BoW**（cell-14）：`[[1, 1, 0, 2, 0, 0, 0, 0, 0]]`，整数词频，`hot`=2。
- **TF-IDF**（cell-31）：`[[0.434, 0, 0.434, 0, 0.660, 0.434, 0, ...]]`，浮点权重，且因为语料小、很多词被压低或归一化。

Notebook 结尾引用语言学家 J. R. Firth 的话点题：**词义的完整理解永远离不开上下文。** BoW 和 TF-IDF 都只加权了词频，**既不能表达语义，也不能表达顺序**——这正引出下一课的词嵌入。见 [TextRepresentationPyTorch.ipynb:490-499](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/TextRepresentationPyTorch.ipynb#L490-L499) 与 README 的 [Conclusion 段](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L59-L61)。

#### 4.3.4 代码实践（本讲的主实践）

**实践目标**：用 TF-IDF 把一组短文本向量化，并计算两条文本的**余弦相似度**，体会「权重压低常见词」的效果。

**操作步骤**：

1. 打开 Notebook，运行到 TF-IDF 那个 cell（cell-31）确认环境正常。
2. 在它下方新增一个 cell，粘贴下面的示例代码并运行：

```python
# 示例代码：TF-IDF 向量化 + 余弦相似度
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

corpus = [
    "the dog likes hot dogs",
    "the cat likes cold milk",
    "stocks and dollar rise on wall street",
]
vec = TfidfVectorizer()
X = vec.fit_transform(corpus)        # 3×词表 的 TF-IDF 矩阵

# 计算两两余弦相似度
sim_01 = cosine_similarity(X[0], X[1])[0,0]   # 狗 vs 猫
sim_02 = cosine_similarity(X[0], X[2])[0,0]   # 狗 vs 股市
print(f"狗 vs 猫   相似度 = {sim_01:.3f}")
print(f"狗 vs 股市 相似度 = {sim_02:.3f}")
```

3. 再加一个对照 cell（示例代码），把 TF-IDF 换回纯 BoW，看相似度有何不同：

```python
# 示例代码：用纯 BoW 做对照
from sklearn.feature_extraction.text import CountVectorizer
bow = CountVectorizer()
B = bow.fit_transform(corpus)
print("BoW  狗 vs 猫   =", round(cosine_similarity(B[0], B[1])[0,0], 3))
print("BoW  狗 vs 股市 =", round(cosine_similarity(B[0], B[2])[0,0], 3))
```

**需要观察的现象**：

- 第 2 步里，「狗 vs 猫」的相似度应明显高于「狗 vs 股市」，因为前两句共享 `the / likes` 这类词。
- 第 3 步用纯 BoW 时，因为 `the` 等高频词被全额计入，「狗 vs 猫」的相似度通常比 TF-IDF 还要高（高频词撑高了重叠）。

**预期结果**：两组相似度都满足「狗 vs 猫 > 狗 vs 股市」；TF-IDF 版本由于压低了 `the` 的权重，相似度数值整体会比 BoW 版本更小、更「克制」。

**待本地验证**：具体数值取决于 scikit-learn 版本与默认归一化（`TfidfVectorizer` 默认 L2 归一化），请以本地运行结果为准；但相对大小关系应稳定成立。

#### 4.3.5 小练习与答案

**练习 1**：某个词出现在语料的**每一篇**文档里，它的 TF-IDF 权重是多少？为什么？

**参考答案**：权重为 0。因为 \(df_i = N\)，\(\log(N/df_i)=\log 1=0\)，整项被乘成 0——这正是 TF-IDF 「抹掉到处都有的词」的设计。

**练习 2**：TF-IDF 相比 BoW 解决了什么问题？又和 BoW 一样保留了什么缺陷？

**参考答案**：解决了「高频停用词淹没关键词」的问题（通过 DF 压低常见词权重）。但和 BoW 一样，它**仍然丢失词序、不表达语义**，只是给词频换了个更合理的权重。

**练习 3**：Notebook 里 `TfidfVectorizer(ngram_range=(1,2))` 的 `(1,2)` 是什么意思？

**参考答案**：表示把「1-gram（单词）」和「2-gram（相邻二元词组）」都当作特征，从而部分缓解 BoW 不识别 `hot dog` 这种多词短语的问题。这是上一节 N-Grams 思想的体现，见 [README.md:32-36](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/13-TextRep/README.md#L32-L36)。

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**用 TF-IDF 给一组短文本做最简单的「相似文档检索」。**

1. 自己准备 5~8 条短文本作为语料（可以是新闻标题、商品描述等），存成 Python 列表。
2. 用 `TfidfVectorizer` 把语料向量化成矩阵 \(X\)。
3. 写一个查询函数：给定一条新文本，先 `transform` 成 TF-IDF 向量，再用 `cosine_similarity` 把它和语料里每一条算相似度，打印相似度最高的那条作为「最相关文档」。
4. 观察并记录：换用 `CountVectorizer`（纯 BoW）时，检索结果会不会因为 `the / a / and` 这类词而变差？

> 提示：完整可运行的参考骨架就是 4.3.4 节的示例代码，把它从「两两相似度」扩展成「一条查询 vs 全语料排序」即可。这一步用到的全部是本讲讲过的 `TfidfVectorizer`、`CountVectorizer` 和 `cosine_similarity`，不需要任何神经网络。

完成后，你应该能直观体会到：**本讲的方法能把文本变成可比对的向量，但完全不知道词的含义**——这正好是下一课「词嵌入」要补上的能力。

## 6. 本讲小结

- NLP 单元围绕**变长文本序列**展开，核心任务有文本分类、情感分析、NER、生成/翻译/摘要等；本单元 6 课是一条「表示能力递增」的阶梯，本讲是它的最底层。
- 把文本变成向量要三步：**分词（tokenize）→ 建词表（vocabulary/stoi）→ 编码成数字**，AG News 训练集词表有 95810 个词。
- **词袋（BoW）** 把句子里所有词的独热向量相加，得到词频向量；它「只记词频、丢了顺序」，但配上一个线性层就能在 AG News 上达到约 86% 的训练准确率。
- **TF-IDF** 用 \(w_{ij}=tf_{ij}\log(N/df_i)\) 给词频加权，压低 `the/and/is` 这类到处都有的词，抬高稀有区分词；权重仍只是频率加权，**依旧不含语义和顺序**。
- 本讲的训练循环和讲义 u2-l5 完全一致（`zero_grad→forward→loss→backward→step`，`LogSoftmax+NLLLoss`），**唯一的新东西是输入变成了文本向量**。

## 7. 下一步学习建议

本讲的 BoW/TF-IDF 有个硬伤：向量又长又稀疏、且词与词之间毫无「语义关系」（`cat` 和 `dog` 在向量空间里毫无瓜葛）。下一课 **第 14 课 词嵌入（Word2Vec / GloVe）** 正是要解决这个问题——用一个**短而稠密**的向量表示每个词，并让「意思相近的词，向量也相近」。

建议继续：

- 读 [lessons/5-NLP/14-Embeddings/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/README.md)，对比「独热/BoW」与「稠密嵌入」的差异。
- 打开 [lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/14-Embeddings/EmbeddingsPyTorch.ipynb)，跑词向量的「类比推理」（king − man + woman ≈ queen）。
- 同时可以回头看本讲 Notebook 里关于 N-Grams（bigram）的 cell（cell-26 / cell-28），它解释了为什么词表会「爆炸」，以及为什么需要嵌入来降维——这是衔接下一课的关键伏笔。
