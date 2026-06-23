# 命名实体识别 NER

## 1. 本讲目标

本讲是 NLP 单元的第 7 课，从「给整句话打一个标签」的分类任务，跨入「给句子里的每个词各打一个标签」的**序列标注**任务。学完本讲，你应当能够：

- 说清楚 NER（命名实体识别）要解决什么问题，以及它为什么本质上是「逐 token 分类」。
- 掌握 BIO（也叫 IOB）标注体系，能手动给一句话标注实体边界与类型，并算出一共有多少个标签类。
- 看懂课程 Notebook 里「Embedding + 双向 LSTM + TimeDistributed」这套 token 分类网络，并理解它的多对多拓扑。
- 知道为什么「准确率」对 NER 是个会骗人的指标，以及实战中为什么改用实体级别的 precision/recall/F1。

本讲承接 [u4-l4 RNN](u4-l4-rnn.md)（循环网络的多对多拓扑）与 [u4-l6 Transformer 与 BERT](u4-l6-transformers-bert.md)（预训练语言模型），是连接「分类范式」与「预训练模型微调」的关键一环。

## 2. 前置知识

在进入源码前，先用大白话把几个关键术语讲清楚。

- **实体（entity）**：文本里指代某个具体事物的片段，比如人名「John Smith」、地名「Paris」、机构「cancer development institute」、日期、化学物质等。NER 的任务就是把它们从一句话里「挑出来并归类」。
- **token（词元）**：这里可粗略理解为「分词后的一个词」。课程 Notebook 直接用 `split()` 按空格切词，所以一个 token 就是一个空格分隔的单词。
- **分类 vs 标注**：之前几课（如文本分类、情感分析）是**句子级**任务——一句话对应一个标签；NER 是**词元级**任务——一句话有 N 个词，就要输出 N 个标签。后者在 RNN 的术语里属于**多对多（many-to-many）**结构。
- **意图（intent）与槽位（slot）**：这是 NER 最经典的应用场景。一个智能助手先用分类判断「用户想干嘛」（意图，例如「查天气」），再用 NER 抽取「在哪儿」「哪一天」（槽位参数）。本讲开头的 README 正是用这个例子引出 NER 的。
- **BIO / IOB**：一种给序列打标签的编码方式，下文 4.2 会详讲。它把「实体的开头」「实体的内部」「非实体」分别用 `B-`、`I-`、`O` 三类前缀表示。

如果你对 RNN/LSTM 的隐状态、对多对多拓扑还比较陌生，建议先回顾 [u4-l4 RNN](u4-l4-rnn.md) 和 [u4-l5 生成式循环网络](u4-l5-generative-rnn.md)，本讲会直接复用其中的拓扑直觉。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `lessons/5-NLP/19-NER/` 下：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/README.md) | 第 19 课讲义，讲清 NER 是 token 分类、为何要用 BIO、用 RNN 做标注的思路。 |
| [NER-TF.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb) | TensorFlow 版可执行 Notebook：读数据→建词表与标签表→向量化与 padding→搭双向 LSTM→训练→推理。 |
| [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md) | 实验/作业说明：在医学语料 BC5CDR 上训练 NER，并用 PubMedBERT 做进阶版。 |

> 说明：Notebook 的永久链接行号指向 `.ipynb` 原始 JSON 文件（GitHub 上点击行号即跳到对应源码行），下文统一以这种方式引用。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**序列标注任务**（NER 是什么、怎么建模）、**BIO 标注体系**（怎么造标签）、**NER 的评估**（怎么衡量好坏）。模型实现放在第一个模块里讲，因为它就是「序列标注」这件事的工程落地。

### 4.1 序列标注任务：从句子分类到逐词分类

#### 4.1.1 概念说明

前面几课我们一直在做分类：一句话 → 一个类别（比如新闻属于「体育」还是「科技」）。这种任务只需要网络在读完整个句子后，**在最后输出一个标签**。

NER 不一样。给定一句话：

> John Smith went to Paris to attend a conference in cancer development institute

我们想要的不是一个总标签，而是**逐词**的判断：「John」是人名开头，「Smith」是人名内部，「Paris」是地名，「cancer development institute」是机构……也就是说，输入 N 个 token，就要输出 N 个标签。这种「输入序列和输出序列等长、每个位置都给一个标签」的任务，就是**序列标注（sequence labeling）**，也叫 **token classification**（词元分类）。

课程 README 一句话点明了本质：

> NER models are essentially **token classification models**, because for each of the input tokens we need to decide whether it belongs to an entity or not, and if it does - to which entity class.

之所以需要专门讲 NER，是因为它有广泛而实用的落地场景。最典型的是聊天机器人：先分类出**意图**（用户想做什么），再用 NER 抽取**槽位参数**（地点、时间等），把参数填进动作里再执行。

#### 4.1.2 核心流程

序列标注的网络结构，正是 RNN 那张经典示意图里**最右侧的「多对多」拓扑**：每个时间步都有一个输出。其前向流程可概括为：

1. 把每个 token 转成 id，再经 Embedding 层变成稠密向量。
2. 用循环层（这里是双向 LSTM）逐词读取，每个时间步产出一个隐状态向量 \(h_t\)，它编码了「当前位置及上下文」的信息。
3. 对**每个**时间步的 \(h_t\)，用一个共享的密集层 + softmax 算出它在各标签类上的概率分布。
4. 取概率最大的类作为该 token 的预测标签。

关键在于第 3 步「对每个时间步都做一次分类」。数学上，设第 \(t\) 步的隐状态为 \(h_t\)，标签类别集合大小为 \(C\)，则该步预测标签 \(c\) 的概率为：

\[
P(\text{tag}_t = c \mid x) = \operatorname{softmax}(W h_t + b)_c
\]

整个序列的损失是各时间步交叉熵之和（或平均）：

\[
\mathcal{L} = -\frac{1}{T}\sum_{t=1}^{T} \log P(\text{tag}_t = y_t \mid x)
\]

注意：这里每个位置都独立地算一次 softmax，但循环层让 \(h_t\) 依赖上下文，所以**判断不是孤立的**——这正是用 RNN 而非「每个词单独分类」的好处。

#### 4.1.3 源码精读

**（1）把原始 CSV 整理成「词序列 + 标签序列」**

Notebook 先用 pandas 读入 `ner_dataset.csv`，数据是「逐词一行」的扁平表，靠 `Sentence #` 列区分句子边界。下面的循环把它重新组装成 `X`（每句的词列表）和 `Y`（每句的标签列表）：

[NER-TF.ipynb（组装句子）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb) 对应 cell-10：遇到 `NaN`（同一句的延续词）就追加，遇到新的 `Sentence #` 就把上一句存入 `X/Y` 并另起新句。这一步把「表格」变成了「序列的序列」，是进入循环网络前必须做的整理。

**（2）建立词表与标签表的互查字典**

标签和词都需要转成整数 id。Notebook 用 `df.Tag.unique()` 取到全部标签种类，再建正反映射：

[NER-TF.ipynb:L172-L173](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L172-L173) 这两行建立 `id2tag` / `tag2id`，把 17 种标签字符串与 0~16 的编号一一对应；词表同理（cell-8），并把编号 0 留给 `<UNK>`（未登录词）。这正是 [u4-l1 文本表示](u4-l1-text-representation.md) 里讲过的「分词→建词表→编码」三步。

**（3）向量化 + 统一长度（padding）**

[NER-TF.ipynb:L274-L280](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L274-L280) 定义 `vectorize`（词→id）和 `tagify`（标签→id）两个小函数，把 `X/Y` 全部数值化。随后因为不同句子长度不一、而网络要定长输入，所以用 Keras 工具补 0：

[NER-TF.ipynb:L299-L300](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L299-L300) 用 `pad_sequences(..., padding='post')` 把所有句子在**末尾**补 0 到与最长句等长（本数据集为 104）。补 0 的位置在标签里也是 0，正好对应 `O`，可视为「无实体」，训练时会被一并学习。

**（4）核心：token 分类网络**

[NER-TF.ipynb:L348-L354](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L348-L354) 是全讲最关键的一段，逐层解读：

- `Embedding(vocab_size, 300, input_length=maxlen)`：把词 id 映射成 300 维稠密向量（见 [u4-l2 词嵌入](u4-l2-word-embeddings.md)）。
- 两层 `Bidirectional(LSTM(units=100, return_sequences=True))`：**双向** LSTM 让每个位置同时看到左和右的上下文；`return_sequences=True` 是关键开关——它要求 LSTM 在**每个**时间步都吐出隐状态，而不是只在最后吐一个，这正是多对多拓扑的体现。
- `TimeDistributed(Dense(num_tags, activation='softmax'))`：把同一个 Dense 层「复制」到每个时间步上，于是每个位置都得到一个长度为 `num_tags`（=17）的概率分布。`TimeDistributed` 就是 4.1.2 里「对每个 \(h_t\) 各做一次分类」的工程实现。
- `compile(loss='sparse_categorical_crossentropy', optimizer='adam', metrics=['acc'])`：标签是整数编号（而非 one-hot），所以用 `sparse_` 版交叉熵；优化器用 adam。

从 `model.summary()` 可看到输出形状 `(None, 104, 17)`——即「每句 104 个位置，每位置 17 类」，这行形状就是序列标注最直观的写照。

**（5）训练与推理**

[NER-TF.ipynb:L391](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L391) 一句 `model.fit(X_data, Y_data)` 开始训练（Notebook 为省时只跑 1 个 epoch）。推理时同样要把测试句补到 `maxlen` 长，再用 `argmax` 取每个位置概率最大的标签：

[NER-TF.ipynb:L441](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L441) 沿类别维 `argmax`，把每步的概率分布压成一个标签 id，再用 `id2tag` 翻回字符串。Notebook 给出的样例结果是：`john→B-per, smith→I-per, paris→B-geo, cancer→B-org ...`，可以看到模型确实学会了逐词标注。

#### 4.1.4 代码实践

**实践目标**：亲手验证「多对多拓扑」与 `return_sequences` 的关系，理解为什么少了它网络就退化为句子分类。

**操作步骤**（源码阅读 + 小改实验）：

1. 打开 Notebook，定位到 cell-16 的模型定义。
2. 先不改，跑通整个 Notebook（需先按其说明从 Kaggle 下载 `ner_dataset.csv` 放到同目录）。
3. 把**第二层** LSTM 的 `return_sequences=True` 改成 `False`，再执行 `model.summary()`。

**需要观察的现象**：

- 原版里该层输出形状是 `(None, 104, 200)`；改成 `False` 后会变成 `(None, 200)`——时间维 104 消失，序列被「压扁」成一个向量。
- 此时后面的 `TimeDistributed` 会报维度不匹配错误，因为它期望每个时间步都有输入。

**预期结果**：你会直观地看到，`return_sequences=True` 是「逐 token 输出」的开关；关掉它，网络就只能做整句级的分类，不再是 NER。若你不在本地运行，可标注「待本地验证」。

> 这一节没有现成可跑的「假命令」。若暂无数据集，可只做第 3 步的 `summary()` 形状推理（不需要训练），它足以说明问题。

#### 4.1.5 小练习与答案

**练习 1**：Notebook 的模型里，为什么用**双向** LSTM 而不是单向？

> 参考答案：判断一个词是不是实体，往往要看它左右两边的信息（如「New York」中「York」是不是地名，要看前面的「New」）。单向 LSTM 只能看到左上文，双向 LSTM 同时聚合右上下文，标注更准。

**练习 2**：`TimeDistributed(Dense(17, softmax))` 和直接写 `Dense(17, softmax)`（不包 `TimeDistributed`）有什么区别？

> 参考答案：在 Keras 的函数式/序列模型里，对形如 `(batch, timesteps, features)` 的输入，`Dense` 默认只作用在最后一维并对所有时间步共享权重，效果其实与 `TimeDistributed(Dense(...))` 等价；`TimeDistributed` 的价值在于把「对每个时间步套同一个层」这件事写得更明确，也能套用在更复杂的子模型上。两者权重共享、参数数量相同。

**练习 3**：如果把 `loss` 从 `sparse_categorical_crossentropy` 改成普通的 `categorical_crossentropy`，会出什么问题？

> 参考答案：`Y_data` 里存的是整数标签编号（如 0、1、2…），而 `categorical_crossentropy` 要求 one-hot 编码的目标。直接换会因形状/语义不匹配而报错或学到错误目标；要么用 `sparse_` 版，要么先把标签 one-hot 化。

### 4.2 BIO 标注体系：用三类标签描述任意实体

#### 4.2.1 概念说明

既然每个 token 都要打一个标签，那标签该怎么设计？最朴素的想法是「每种实体类型一个标签」——人名标 `PER`、地名标 `GEO`、非实体标 `O`。但这有个致命问题：**无法表达实体的边界**。

看 README 的医学论文标题例子：

> **Tricuspid valve regurgitation** and **lithium carbonate** **toxicity** in a newborn infant.

这里「Tricuspid valve regurgitation」是一个实体（疾病 DIS），「lithium carbonate」是另一个实体（化学物质 CHEM），两者紧挨着。如果只用 `DIS/CHEM/O`，那么连续多个 `CHEM` 词到底属于一个实体还是相邻两个实体，根本分不清。

BIO（也叫 IOB）就是为了解决边界问题而生的编码方案，它给每个实体类型拆成两个标签：

- `B-X`（**B**eginning）：类型为 X 的实体的**第一个**词。
- `I-X`（**I**nside）：类型为 X 的实体的**后续**词。
- `O`（**O**utside）：不属于任何实体的词。

这样，相邻同类实体就能靠一个新的 `B-` 区分开（`I-CHEM` 之后若再出现 `B-CHEM`，就说明是另一个化学实体开始了）。

#### 4.2.2 核心流程

README 把上面的医学标题用 BIO 标注后，得到这张一一对应表（[README.md:L35-L48](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/README.md#L35-L48)）：

| Token | Tag |
|-------|-----|
| Tricuspid | B-DIS |
| valve | I-DIS |
| regurgitation | I-DIS |
| and | O |
| lithium | B-CHEM |
| carbonate | I-CHEM |
| toxicity | B-DIS |
| in / a / newborn / infant / . | O |

从这张表可以总结出 BIO 标注的三条手工规则：

1. 一个实体的**首词**永远标 `B-类型`。
2. 该实体的**其余词**标 `I-类型`。
3. 凡是非实体词，一律标 `O`。

**标签总数怎么算**？设实体类型有 \(E\) 种，则 BIO 标签数为：

\[
C = 2E + 1
\]

即每种类型拆成 `B-` / `I-` 两个，再加一个公共的 `O`。课程 Notebook 的数据集有 8 种类型（geo、gpe、per、org、tim、art、nat、eve），所以 \(C = 2\times 8 + 1 = 17\)，正好对应 `num_tags = 17`。lab 的 README 也用同一公式提示分类数（[lab/README.md:L38](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md#L38)：`# number of classes: 2*entities+1`）。

> 进阶提示：BIO 还有个变体 **BIOES / BILOU**，额外用 `E-`（End）或 `L-`（Last）标记实体末词、`S-`（Single）标记单字实体，能更显式地编码边界。BIO 是本课使用的最简版本。

#### 4.2.3 源码精读

README 对 BIO 的完整说明在此：

[README.md:L33](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/README.md#L33) 这段话讲清了为何要为每类实体用两个标签（`B-` 表开头、`I-` 表内部）以及为何用 `O` 表其余词，并点出这叫 BIO（IOB）标注。它正是 4.2.1「概念说明」的原始出处。

在 Notebook 里，BIO 标签并不是「现成的」，而是数据集已经标好的——`ner_dataset.csv` 的 `Tag` 列直接就是 `O / B-geo / I-per ...` 这种字符串。Notebook 只是：

[NER-TF.ipynb:L172](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L172) 用 `df.Tag.unique()` 把这 17 种标签收集起来，再映射成 0~16 的编号。换句话说，本课的 Notebook **消费** BIO 标签，而真正「把原始文本转成 BIO」的工作交给作业（见 [lab/README.md:L26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md#L26)：「You will need to write some Python code to convert this into BIO encoding」）——因为 BC5CDR 数据集给的是字符区间，需要你自己写代码转成 BIO。

#### 4.2.4 代码实践

**实践目标**：手动给一句话做 BIO 标注，并与 Notebook 的标签集合对照，检验对边界编码的理解。

**操作步骤**：

1. 自选一句话，例如：「Microsoft was founded by Bill Gates in Seattle.」
2. 按 4.2.2 的三条规则，逐词写下 BIO 标签，类型用 Notebook 里的 `org / per / geo`。
3. 把你写的标签与下面「预期结果」对照。

**预期结果**：

| Token | Tag |
|-------|-----|
| Microsoft | B-org |
| was / founded / by | O |
| Bill | B-per |
| Gates | I-per |
| in | O |
| Seattle | B-geo |
| . | O |

**需要观察的现象**：注意「Bill Gates」是**一个**人名实体，所以首词 `B-per`、次词 `I-per`；而「Microsoft」「Seattle」各只占一词，所以都是 `B-` 且后面没有 `I-`。如果你把 Gates 误标成 `B-per`，就等于把一个人名拆成了两个实体——这正是 BIO 要避免的错误。

#### 4.2.5 小练习与答案

**练习 1**：如果两个**同类型**实体紧挨着，比如「Washington Street」（假设两者都是地名），该怎么标？

> 参考答案：`Washington → B-geo`，`Street → B-geo`。第二个实体虽然是地名的延续词位置，但因为它是**新实体**的开头，必须用 `B-` 而不是 `I-`，否则会被误判成同一个实体。这正是 BIO 相比「单标签法」的核心价值。

**练习 2**：某数据集有 5 种实体类型，用 BIO 方案一共需要多少个输出类？若改用 BIOES 呢？

> 参考答案：BIO：\(2\times5+1=11\) 类。BIOES：每类拆成 B/E/I/S 四个加一个 O，共 \(4\times5+1=21\) 类。

**练习 3**：为什么说「只用一种标签 `E` 表示实体、其余用 `O`」的方案不适合 NER？

> 参考答案：因为它既丢失了实体**类型**，又无法表达**边界**——连续的 `E` 无法区分是一个长实体还是多个相邻实体，也就无法在评估时正确「合并」出实体。

### 4.3 NER 的评估：为什么准确率会骗人

#### 4.3.1 概念说明

Notebook 训练完打印出 `acc: 0.9841`——98.4% 的准确率，看起来非常好。但对 NER 来说，**token 级准确率是个会骗人的指标**。

原因在于标签分布极不均衡：自然语言里绝大多数词都不是实体，标签几乎全是 `O`。一个「永远预测 `O`」的废模型，也能蒙对 80%~90% 的 token。所以 98% 的准确率里，有相当一部分是「把非实体猜成非实体」白送的，并不代表模型真的能找出实体。

正确的评估应该到**实体级别**：把连续的 `B-X I-X ...` 合并成一个完整实体（类型 + 跨度），再去和标准答案比对。一个实体**只有当类型和边界都完全正确**才算对。然后用三个指标：

- **Precision（精确率）**：模型抽出的实体里，有多少是对的。
- **Recall（召回率）**：标准答案里的实体，有多少被模型找出来了。
- **F1**：两者的调和平均，综合衡量。

#### 4.3.2 核心流程

实体级评估的计算流程：

1. **解码**：把模型输出的标签序列，按 `B-` 开头、`I-` 续接的规则，合并成 `(类型, 起始, 结束)` 的实体列表。注意处理非法转移（如 `O` 后直接跟 `I-`，通常把这种 `I-` 当成新实体开头或忽略）。
2. **比对**：预测实体集合 \(P\) 与标准实体集合 \(G\)，严格匹配（类型 + 跨度完全相同）才算「正确」集合 \(TP = P \cap G\)。
3. **算指标**：

\[
\text{Precision} = \frac{|TP|}{|P|}, \quad
\text{Recall} = \frac{|TP|}{|G|}, \quad
F_1 = \frac{2 \cdot P \cdot R}{P + R}
\]

举个小例子说明 token 准确率为何失真：假设一句 10 个词，其中只有 1 个是真实体（1 个 `B-per`），其余 9 个是 `O`。模型把那 1 个实体**完全漏掉**（预测成 `O`），但其余 9 个 `O` 全对。则：

- token 准确率 = \(9/10 = 90\%\)（看起来很高）
- 实体级 recall = \(0/1 = 0\)（一个实体都没找着）

两者落差极大，这就是为什么 NER 不能只看准确率。

> 课程 Notebook 本身**只**报 `acc`，并未实现实体级评估，是有意把「正确评估」留给读者思考（见 README 末尾的 Challenge 与 lab）。下文实践会带你手算一遍这个差距。

#### 4.3.3 源码精读

Notebook 训练时确实只挂了准确率这一个指标：

[NER-TF.ipynb:L354](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L354) `metrics=['acc']` 让 `fit` 打印 token 级准确率，训练 1 个 epoch 后达到 `acc: 0.9841`。注意这个数字的「水分」来源：标签里 `O` 占绝对多数。

推理样例也暴露了同样的「假象」。看 Notebook 对测试句的输出：

[NER-TF.ipynb（推理输出，cell-21 之后）](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/NER-TF.ipynb#L441) 模型把「cancer development institute」标成了 `B-org / I-org / I-org`——但真实世界里这更像一个机构名被「合理地」识别出来了。仅凭 `acc` 你无法知道这种「整段实体」找得准不准，必须做实体级比对。

README 在结尾也承认简单 LSTM 只是「reasonable results」，并把更强的方案指向 BERT（[README.md:L58](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/README.md#L58) 提到可用熟悉的 RNN 做 token 分类；Notebook 的 Takeaway 进一步指向 HuggingFace 的 BERT 微调教程）。这与本模块主题一致：评估要做对，模型也要更强。

#### 4.3.4 代码实践

**实践目标**：用纸笔算出「token 准确率 vs 实体级 F1」的差距，直观体会为什么不能只看 `acc`。

**操作步骤**：

1. 取下面这个小例子（标准答案 vs 某模型预测）：

   | Token | 标准答案 | 模型预测 |
   |-------|----------|----------|
   | John | B-per | B-per |
   | Smith | I-per | O |
   | went | O | O |
   | to | O | O |
   | Paris | B-geo | B-geo |
   | yesterday | O | O |

2. 计算 token 级准确率：6 个词里预测对了几个。
3. 按 4.3.2 的规则，把标签序列合并成实体（类型 + 跨度），分别列出标准实体集合 \(G\) 和预测实体集合 \(P\)，再算实体级 precision / recall / F1。

**需要观察的现象与预期结果**：

- token 准确率：6 个里对了 5 个（仅 `Smith` 错），\(5/6 \approx 83\%\)。
- 标准实体 \(G = \{\)「John Smith」(per, 0-1),「Paris」(geo, 4)\(\}\)，共 2 个。
- 预测实体 \(P = \{\)「John」(per, 0),「Paris」(geo, 4)\(\}\)，共 2 个（`Smith` 被标成 `O`，所以「John」单独成一个实体）。
- 严格匹配下：「John Smith」≠「John」（跨度不同）→ 不算对；只有「Paris」算对，\(TP=1\)。
- 于是 Precision \(=1/2=50\%\)，Recall \(=1/2=50\%\)，F1 \(=50\%\)。

对比之下：token 准确率 83% 看着不错，实体级 F1 只有 50%——半数实体没找对。这就说明了 NER 必须用实体级指标。

> 若你想在代码里实现这个评估，可参考业界常用的 `seqeval` 库（输入 BIO 标签序列即可输出实体级 P/R/F1）。本课 Notebook 未内置，属「待本地验证/自行扩展」的内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NER 的 token 准确率通常「虚高」？用一句话解释。

> 参考答案：因为绝大多数 token 的标签是 `O`，模型只要把非实体都猜成 `O` 就能拿很高分，掩盖了它在「真正找实体」上的真实能力。

**练习 2**：实体级「严格匹配」要求哪两方面都正确？

> 参考答案：实体的**类型**和**边界（起始-结束位置/跨度）**都必须与标准答案完全一致，才算一个正确实体（true positive）。

**练习 3**：Precision 和 Recall 哪个更重要，取决于应用。给一个「宁可多报也别漏」（偏重 Recall）的 NER 场景。

> 参考答案：例如医学文献中抽取疾病/药物实体用于辅助诊断——漏掉一个关键药物名可能造成严重后果，因此宁可多报一些再人工筛选（高 recall），也不想漏。反之，做新闻人物索引可能更看重 precision，避免把错误人物塞进数据库。

## 5. 综合实践

本讲的综合实践直接对应课程作业 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md)：在一个**真实医学语料**上完成「数据转 BIO → 训练 NER → 评估」的全流程，把本讲三个模块串起来。

**任务**：在 BC5CDR 数据集上训练一个识别「疾病（Disease）」与「化学物质（Chemical）」的 NER 模型，并在自选文本上抽取实体、人工校验。

**步骤**：

1. **拿数据**：按 [lab/README.md:L11](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md#L11) 的指引，从 BioCreative V 注册下载 BC5CDR。它的格式是「标题行 + 摘要行 + 若干实体行（带字符起止位置、类型、本体 ID）」。

2. **转 BIO**（对应模块 4.2）：数据给的是字符区间（如 `6794356 0 29 Tricuspid valve regurgitation Disease`），你需要写 Python 代码：先按区间切词，再给区间内的首词标 `B-DIS`、其余词标 `I-DIS`，区间外的词标 `O`。[lab/README.md:L26](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md#L26) 明确要求你完成这一转换。这里实体类型有 2 种，所以标签共 \(2\times2+1=5\) 类：`O / B-DIS / I-DIS / B-CHEM / I-CHEM`。

3. **训练模型**（对应模块 4.1）：
   - 入门版：复用本课 Notebook 的「Embedding + 双向 LSTM + TimeDistributed」结构，把 `num_tags` 改成 5，在 BC5CDR 上 `fit`。
   - 进阶版：按 [lab/README.md:L37-L41](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md#L37-L41) 用 HuggingFace 加载医学预训练模型 PubMedBERT（`BertForTokenClassification`）做微调——这就是 [u4-l6 BERT](u4-l6-transformers-bert.md) 的落地。注意 PubMedBERT 用子词分词，一个原词可能被切成多个子词，BIO 标签要对齐到子词（通常只给首子词标实体标签、其余子词标 `O` 或忽略）。

4. **评估与校验**（对应模块 4.3）：划分训练/验证集，**不要只看 `acc`**。用实体级 P/R/F1 评估（可用 `seqeval`）。然后挑 5~10 段自选医学摘要（比如从 PubMed 复制），让模型抽取实体，人工对照判断找得对不对、边界准不准。

**需要观察的现象**：

- LSTM 版的实体级 F1 与 token 准确率的差距（前者通常明显低于后者）。
- PubMedBERT 微调版在很少数据下就能显著超过从零训的 LSTM——这正是预训练模型的威力。

**预期结果**：你将得到一个能在任意医学文本上标出疾病与化学物质的小模型，并亲手体会到「转 BIO、搭模型、做实体级评估」三件事的完整闭环。若数据或算力受限，至少完成「转 BIO 脚本」与「在小样本上手算 P/R/F1」两部分，并对其余部分标注「待本地验证」。

## 6. 本讲小结

- NER 本质是**序列标注 / token 分类**：输入一句话，对每个词各输出一个标签，对应 RNN 的**多对多**拓扑。
- **BIO（IOB）标注**用 `B-X / I-X / O` 三类前缀，既编码实体类型又编码边界；\(E\) 种实体类型对应 \(2E+1\) 个标签类（本课为 17）。
- 课程 Notebook 用 `Embedding + 双向 LSTM(return_sequences=True) + TimeDistributed(Dense+softmax)` 实现 token 分类，`TimeDistributed` 负责「对每个时间步各做一次分类」。
- 训练用 `sparse_categorical_crossentropy`（标签是整数编号），推理用 `argmax` 在每个位置取最大概率标签。
- **token 级准确率对 NER 会虚高**（`O` 占多数），实战必须用**实体级** precision / recall / F1，且实体要严格匹配（类型 + 边界）才算对。
- 简单 LSTM 只是「reasonable」，更强的做法是用预训练模型（如 PubMedBERT）微调，衔接 [u4-l6 BERT](u4-l6-transformers-bert.md)。

## 7. 下一步学习建议

- **补全评估**：给本课 Notebook 加上 `seqeval` 的实体级 P/R/F1，对比它和 `acc` 的差距，这是把本讲学透的最快路径。
- **完成作业**：做完 [lab/README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/19-NER/lab/README.md) 的 BC5CDR + PubMedBERT 实验，体会预训练模型在小数据上的优势。
- **延伸阅读**：README 推荐的 Karpathy 博客《The Unreasonable Effectiveness of Recurrent Neural Networks》能加深对多对多拓扑的理解；HuggingFace 官方「Token classification」教程（Notebook Takeaway 给出链接）是工业级 NER 的标准入门。
- **下一讲**：进入 [u4-l8 大语言模型与提示编程](u4-l8-llm-prompting.md)，看 GPT 类自回归大模型如何把「逐 token 预测」推向极致，以及提示工程如何改变任务范式。
