# 生成式循环网络

## 1. 本讲目标

上一讲（u4-l4）我们用 RNN/LSTM 做了**文本分类**：吃进一段文本，吐出一个类别。那是 RNN 的「读」用法——把序列压成一个向量。本讲要反过来，用 RNN 的「写」用法：给它一个开头，让它**一个字一个字地把文本续写下去**。

学完本讲你应该能够：

- 说清楚**自回归生成**（autoregressive generation）的原理：每一步用上一步的输出当下一步的输入。
- 看懂本课 Notebook 如何构造训练数据（目标序列 = 输入序列左移一位），并理解这种「老师强制」（teacher forcing）训练法。
- 区分两种采样策略：贪心采样（argmax）和随机采样（multinomial），并理解它们各自产生的文本形态。
- 用**温度**（temperature）这个旋钮在「稳定重复」和「多样发散」之间调节生成结果。
- 理解生成长文本时状态如何跨步传递、字符级与词级生成的取舍，并完成词级生成的 lab。

## 2. 前置知识

在进入源码之前，先用三段话建立直觉。如果你已学过本单元的前几讲，可以快速跳过。

**(1) 语言模型就是「预测下一个字」。** 第三讲（u4-l3）我们讲过语言模型（language model）：给一段文本打分、预测被遮住的或下一个词。本讲正是把「预测下一个字」这件事**反过来用**——既然模型会预测下一个字，那我把预测出来的字再喂回去当输入，不就能连续不断地写出新文本吗？这就是生成。回顾一下单步预测的形式：

\[ P(c_t \mid c_1, c_2, \dots, c_{t-1}) \]

模型在每一步给出一个「在已读到的上文条件下，下一个字符是各候选字符的概率分布」。

**(2) RNN 之所以适合生成，是因为它有「记忆」。** RNN/LSTM/GRU 在读完一个字符后，会把迄今为止的信息压缩进一个**隐状态**（hidden state）\(s\)，传给下一步。所以下一步只需要「新字符 + 上一步的状态」就能继续。这个「状态可以一直传下去」的性质，正是无限续写的物理基础。

**(3) 从「分类网络」到「生成网络」只差一处。** 上一讲做分类时，RNN 只在**最后一步**接一个线性层输出类别（many-to-one）。做生成时，我们在**每一步**都接一个线性层输出「下一个字符的概率分布」（many-to-many）。结构几乎一样，只是输出从「一个类别」变成了「和输入等长的序列」。

> 关键术语：**自回归**（autoregressive，用自己过去的输出当作未来的输入）、**老师强制**（teacher forcing，训练时用真实的下一个字而非模型预测的字做输入）、**温度**（temperature，控制概率分布尖锐程度的旋钮）、**贪心采样 / 随机采样**（greedy / stochastic sampling）。

## 3. 本讲源码地图

本讲只围绕 17-GenerativeNetworks 这一课的两个文件展开，它们一讲概念、一讲实现：

| 文件 | 作用 |
| --- | --- |
| `lessons/5-NLP/17-GenerativeNetworks/README.md` | 课程讲义：讲四种 RNN 拓扑（一对一/一对多/多对一/多对多）、训练与推理的思路、以及「软生成与温度」的概念。 |
| `lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb` | PyTorch 实现：字符级词表、`get_batch` 构造训练对、`LSTMGenerator` 网络、`generate`（贪心）与 `generate_soft`（温度）两个推理函数、训练循环。 |
| `lessons/5-NLP/17-GenerativeNetworks/torchnlp.py` | Notebook 用到的辅助函数：`load_dataset`（加载 AG News）、`encode`（文本转编号）、`device`（自动选 CPU/GPU）等。 |
| `lessons/5-NLP/17-GenerativeNetworks/lab/README.md` | Lab 任务：从「字符级」升级到「词级」文本生成，自选一本书做数据集。 |

口诀：**README 看四种拓扑和温度概念，Notebook 看训练对怎么造、网络怎么搭、采样怎么做，lab 把字符级换成词级。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 自回归生成**（怎么训练一个会预测下一个字的网络）、**4.2 采样策略**（拿到概率分布后，怎么挑出下一个字）、**4.3 生成长文本**（推理时状态怎么传、字符级和词级有什么差别）。

### 4.1 自回归生成

#### 4.1.1 概念说明

「自回归」听起来吓人，其实就是一句话：**模型把自己上一时刻的输出，当作下一时刻的输入**。这样它就能一步接一步地往下写。

RNN 之所以能这么用，是因为它的拓扑灵活。README 引用了 Andrej Karpathy 那篇著名的博客，把循环网络归纳成四种基本拓扑：

- **一对一（one-to-one）**：普通网络，一个输入一个输出。
- **一对多（one-to-many）**：给一个输入（比如一张图），输出一串字符——这就是**图像描述**（image captioning）。
- **多对一（many-to-one）**：吃进一串，输出一个——上一讲的**文本分类**就是它。
- **多对多 / 序列到序列（many-to-many / seq2seq）**：先读完整句压成状态，再展开成另一串——**机器翻译**是典型。

本讲要做的「给定上文、续写下文」，属于**多对多**的简化版：在每一个时间步都输出「下一个字符的概率分布」，并且这个分布在训练时与输入序列**等长**。

见 README 对四种拓扑的描述：

[README.md:L17-L20](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/README.md#L17-L20) —— 用「一对一/一对多/多对一/多对多」四类拓扑说明生成任务属于多对多，并点出图像描述、机器翻译等典型应用。

#### 4.1.2 核心流程

训练一个生成式 RNN，关键在于**如何造训练样本**。它的核心技巧叫**老师强制**（teacher forcing）：

1. 取一段长度为 `nchars` 的字符序列作为**输入**。
2. 把这段序列**整体左移一位**作为**目标**——也就是说，目标是「每个位置上真正的下一个字符」。
3. 网络在每个位置都预测下一个字符，用交叉熵比较预测分布和真实下一个字符。

用伪代码表达训练对的关系（设窗口长度 `nchars`，文本 `s`）：

```
输入 ins[i]  = s[i : i+nchars]      # 第 i 个窗口的 nchars 个字符
目标 outs[i] = s[i+1 : i+nchars+1]  # 同样长度，但整体往后挪一位
```

这样网络学到的就是：**给定当前位置的字符和它之前的隐状态，预测下一个字符。** 这正是生成的最小能力单元。

训练流程与上一讲一致，仍是 PyTorch 五件套（`zero_grad → 前向 → loss → backward → step`），唯一的差别是：损失要在**所有时间步**上算，并且不再关心分类准确率，而是每隔若干步直接打印一段生成文本来看看效果。

#### 4.1.3 源码精读

**① 字符级词表。** 做字符级生成，首先要把文本拆成单个字符而不是词。Notebook 定义了一个极简的字符分词器，并用 `torchtext.vocab` 统计字符频次建词表：

[GenerativePyTorch.ipynb:L62-L73](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L62-L73) —— `char_tokenizer` 直接 `list(words)` 把字符串拆成字符列表，再用 `collections.Counter` 统计得到 `vocab_size`（本数据集为 82）。这就是「字符级」与前面几讲「词级」的唯一差别。

**② 构造训练对（老师强制）。** 这是本模块最关键的代码，值得逐行看：

[GenerativePyTorch.ipynb:L158-L170](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L158-L170) —— `get_batch` 对一段文本 `s` 用滑动窗口枚举所有长度为 `nchars` 的片段：`ins[i]` 取 `s[i:i+nchars]`，`outs[i]` 取 `s[i+1:i+nchars+1]`。注意 `outs` 恰好比 `ins` 整体晚一位——这就是老师强制：网络在每个位置要预测的就是「真实文本里紧跟着的下一个字符」。

**③ 网络结构。** `LSTMGenerator` 非常短，但它体现了「多对多」的本质：

[GenerativePyTorch.ipynb:L187-L195](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L187-L195) —— 输入字符编号先经 `one_hot` 转成 one-hot 向量（因为词表只有 82 个字符，不必用嵌入层），再过 LSTM；`self.rnn` 默认返回**每个时间步**的输出 `x` 和状态 `s`，最后用 `self.fc` 把每个时间步的隐状态映成「词表大小的打分（logits）」。返回 `(self.fc(x), s)`——注意这里返回的是**整条序列**每个位置的 logits，不止最后一个，这正是多对多。

> 为什么不用嵌入层？因为字符级词表很小（82），one-hot 向量本身就很短，直接进 LSTM 即可；词表大时（词级，动辄几万）才会改用 `nn.Embedding` 把稀疏 one-hot 压成稠密向量。

**④ 训练循环。** 损失的计算方式值得专门说明：

[GenerativePyTorch.ipynb:L269-L290](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L269-L290) —— 训练循环遍历 AG News 的每条新闻，调 `get_batch` 造训练对，然后标准五件套。重点是损失行：`cross_entropy(out.view(-1,vocab_size), text_out.flatten())`。`out` 形状是 `(batch, seq, vocab_size)`，`view(-1, vocab_size)` 把它摊平成 `(batch×seq, vocab_size)`，`text_out.flatten()` 摊成对应的目标编号——于是一次前向里**所有时间步的「预测下一个字符」误差被一起算进 loss**。每 1000 步打印一次 loss 和一段 `generate(net)` 的样例文本来观察效果。

训练日志里能看到 loss 从 4.4 降到 1.5 左右，生成的文本从乱码 `sr sr sr...` 逐渐变成「像新闻」的片段（`today and the company and the company ...`）。

#### 4.1.4 代码实践

**实践目标**：亲手跟踪一遍「输入序列 → 目标序列」的对应关系，确认你真的理解了老师强制。

**操作步骤**（在 `ai4beg` 内核下打开 `GenerativePyTorch.ipynb`，从头运行到 `get_batch` 那个 cell）：

1. 在 `get_batch` cell 之后，新增一个 cell，挑一条短文本手动构造一个小 batch 并打印：
   ```python
   s = "hello world"          # 示例代码：仅用于观察对应关系
   nchars = 3
   ins, outs = get_batch(s, nchars=nchars)
   for i in range(min(5, len(ins))):
       print("in =", [vocab.get_itos()[j] for j in ins[i]],
             " out =", [vocab.get_itos()[j] for j in outs[i]])
   ```
   > 上面的 `s`、`nchars` 是示例值；Notebook 原本的 `nchars=100`、文本是 AG News 整条新闻，这里改小只为看得清。

**需要观察的现象**：每一行的 `out` 应当恰好是 `in` 左移一位——即 `out[0]` 等于 `in[1]`，`out[1]` 等于 `in[2]`，以此类推。

**预期结果**：你会清楚地看到「网络在每个位置要预测的就是紧随其后的那一个字符」。如果你看不到这种严格错位关系，说明你对老师强制的理解有偏差，回到 4.1.2 重读。

> 是否能跑通取决于本地 AG News 是否下载成功，下载/编码结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么训练时用「真实的下一个字符」（老师强制）当输入，而不是用「模型上一步预测出来的字符」？

**参考答案**：训练初期模型几乎随机，若用它自己的错误预测当输入，误差会像滚雪球一样累积，训练极不稳定且慢；老师强制让每一步都站在「正确上文」上学习，梯度干净、收敛快。代价是「训练时见到的都是正确上文」，推理时却要用自己的预测，存在轻微的「训练/推理分布不匹配」（exposure bias）。

**练习 2**：`get_batch` 中 `outs[i] = enc(s[i+1:i+nchars+1])`，如果改成 `outs[i] = enc(s[i:i+nchars])`（和 `ins` 完全相同），会发生什么？

**参考答案**：网络会学到「原样复制输入」，loss 也能降到很低，但它学到的不是「预测下一个字符」，于是根本不会生成新文本——`generate` 出来的只会是无限复读 prompt。这反过来说明「左移一位」这个细节是生成能力的来源。

---

### 4.2 采样策略

#### 4.2.1 概念说明

网络在每个位置输出的是一个**概率分布**（经 softmax 后，词表里每个字符都有一个概率）。现在的问题是：**拿到这个分布后，挑哪个字符当下一个字符？**

最直接的想法：永远挑概率最大的那个（argmax）。这叫**贪心采样**（greedy / hard sampling）。但它有个著名毛病——容易**陷入循环**。Notebook 训练日志里反复出现这种文本：

```
today and the company to the company to the company to the company ...
```

为什么会循环？因为一旦走进某个局部最优的字符序列，模型对「下一个字符」的判断几乎确定，于是反复输出同一段话。

README 指出，其实很多时候**前几名的概率差得并不多**。比如序列 `play` 之后，下一个字符是空格（`play `）还是 `e`（`player`）都说得通。如果永远只选第一名，就放弃了这些「同样合理」的备选。

于是更聪明的做法是**随机采样**（stochastic / soft sampling）：**按概率分布抽样**，而不是总取最大值。这样第二名、第三名也有机会被选中，文本就不再死循环。

#### 4.2.2 核心流程

设网络在某一步输出的 logits（未归一化打分）为向量 \(z\)，词表大小为 \(V\)。两种采样方式：

- **贪心**：\(c = \arg\max_i z_i\)。确定、可复现，但易循环。
- **随机（温度 \(\tau\)）**：先算温度 softmax，再按它抽样：

\[ P(i) = \frac{\exp(z_i / \tau)}{\sum_{j=1}^{V} \exp(z_j / \tau)}, \qquad c \sim \mathrm{Multinomial}(P) \]

温度 \(\tau\) 的作用：

- \(\tau = 1\)：标准的「公平」随机采样。
- \(\tau \to 0\)：\(z_i/\tau\) 趋向无穷大，最大项彻底压倒其他项 → 退化为 argmax → 越来越像贪心、越来越重复。
- \(\tau \to \infty\)：\(z_i/\tau \to 0\)，所有 \(\exp(\cdot)=1\)，分布趋向均匀 → 完全随机 → 乱码。

一句话总结温度的旋钮作用：**温度低 = 稳定但啰嗦重复；温度高 = 多样但容易胡言乱语。**

#### 4.2.3 源码精读

**① 贪心采样的推理循环。** `generate` 是整个生成的「主循环」，它和采样策略耦合在一起——这里用的是 `argmax`：

[GenerativePyTorch.ipynb:L212-L222](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L212-L222) —— 先把整个 `start` 提示串喂进网络拿到输出 `out` 和状态 `s`；随后循环 `size` 次，每次取上一步输出序列**最后一个位置** `out[0][-1]` 做 `argmax` 得到字符编号 `nc`，把它追加进结果，再把 `(nc, s)` 喂回网络生成下一步。`argmax` 就是贪心采样，它正是循环复读的根源。

> 注意 `out[0][-1]` 的两层索引：`[0]` 取 batch 第 0 条，`[-1]` 取该条序列的**最后一个时间步**。因为前向返回的是整条序列每个位置的预测，而我们只需要「在已读到所有字符之后，下一个字符」的预测。

**② 软采样 + 温度。** `generate_soft` 和 `generate` 几乎一模一样，唯一区别在「怎么从分布里取字符」这一行：

[GenerativePyTorch.ipynb:L350-L362](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L350-L362) —— 把 `argmax` 换成：`out_dist = out[0][-1].div(temperature).exp()`，再用 `torch.multinomial(out_dist, 1)` 按权重抽样。`z.div(τ).exp()` 等价于算 \(\exp(z_i/\tau)\)，而 `multinomial` 按权重正比抽样，归一化常数被约掉，因此这等价于从 \(\mathrm{softmax}(z/\tau)\) 里采样——正是 4.2.2 的公式。

**③ 概念出处。** README 用一整节解释了为什么需要软采样与温度：

[README.md:L41-L53](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/README.md#L41-L53) —— 说明贪心会循环、概率前几名往往相差不大，从而引出「按分布抽样」与「温度」两个概念。

Notebook 用 `[0.3, 0.8, 1.0, 1.3, 1.8]` 五个温度各生成 300 字，结果完美印证了旋钮效应：温度 0.3 时文本较连贯但重复（`Today and a company and complete an all the land ...`），温度 1.8 时几乎乱码（`Today plary, P.slan chly\401 ...`）。

#### 4.2.4 代码实践

**实践目标**：用一组温度值扫描，亲眼看「温度→文本风格」的对应关系。

**操作步骤**：

1. 运行完整 Notebook 直到训练循环跑完（至少看到 loss 稳定、`generate(net)` 能产出像新闻的文本）。
2. 运行 `generate_soft` 那个 cell，它已经内置了五档温度的对比输出。
3. 新增一个 cell，把温度换成更密的网格再扫一次（示例代码）：
   ```python
   # 示例代码：扫描更多温度档位
   for t in [0.2, 0.5, 0.7, 1.0, 1.5, 2.0]:
       print(f"--- T={t}\n{generate_soft(net, size=200, start='Today ', temperature=t)}\n")
   ```

**需要观察的现象**：随着温度升高，文本应当从「连贯但重复」逐渐变成「多样但破碎」，到很高温度时变成无意义字符流。

**预期结果**：你能找到一个「甜点温度」（本数据集大致在 0.5~0.8 之间），既有一定多样性又不至于乱码。把每一档的生成结果贴到一个对照表里。具体数值**待本地验证**（取决于你训练了多久、loss 降到多少）。

#### 4.2.5 小练习与答案

**练习 1**：`generate_soft` 里用的是 `out[0][-1].div(temperature).exp()`，没有做 `softmax`，为什么采样结果仍然正确？

**参考答案**：`multinomial(w)` 是按权重 `w` **正比**抽样，即概率为 \(w_i / \sum_j w_j\)。softmax 的分母 \(\sum_j \exp(z_j/\tau)\) 对所有候选都一样，是个常数，在「正比抽样」里会被约掉。所以 `exp(z/τ)` 直接喂给 `multinomial` 与先 softmax 再抽样完全等价。（代价是数值上没有减最大值的稳定化处理，logits 很大时理论上可能溢出，但本玩具例子里不会出问题。）

**练习 2**：有人把温度设成 0.01 想得到「最稳定」的文本，结果反而得到无限循环的复读。解释原因。

**参考答案**：温度趋近 0 时，softmax(z/τ) 趋近于 one-hot（只指向 argmax），软采样退化成贪心采样，于是又回到了 4.2.1 说的循环复读问题。「稳定」和「重复」是一体两面。

---

### 4.3 生成长文本

#### 4.3.1 概念说明

前两节我们有了能预测下一个字符的网络、有了采样策略，现在要把它们组装成「能无限写下去」的生成器。这里要回答三个问题。

**第一，状态怎么跨步传递？** 这是长文本生成的核心。`generate` / `generate_soft` 的循环每一步都把上一步的状态 `s` 喂回去：`(out, s) = net(nc.view(1,-1), s)`。也就是说，网络「记得」它从 prompt 开始读到的全部历史——不是显式存下来，而是压缩在那个隐状态里。只要状态在循环里不断传递，理论上就能无限续写。这正是 README 强调的推理流程：先用 prompt「暖场」出状态，再从该状态开始逐字生成。

**第二，字符级 vs 词级，差别在哪？** 本课 Notebook 用的是**字符级**（vocab 只有 82），好处是词表极小、不会有「没见过的词」；坏处是模型要从零学会拼出每个单词，需要更长的上下文窗口、更长的训练才能凑出像样的词。词级（lab 的任务）则相反：词表动辄几万，但每一步直接预测一个词，能更快学到词与词的搭配。

**第三，怎么让生成质量更好？** Notebook 在结尾给出了几条改进方向（更好的 minibatch 构造、**多层 LSTM**、换 GRU、调隐层大小）。多层 LSTM 的直觉很形象：底层学音节、高层学词与词组——只要给 `nn.LSTM` 传一个层数参数即可。

#### 4.3.2 核心流程

长文本生成的推理流程：

```
1. warmup（暖场）：把整个 prompt 串喂进网络，得到输出 out 和状态 s
2. loop（重复 size 次）：
   a. 从 out 的最后一步取出下一个字符分布
   b. 按采样策略（贪心 / 温度随机）挑出字符 nc
   c. 把 (nc, s) 喂回网络，得到新的 out 和 s   ← 状态在此传递
   d. 把 nc 追加进结果
3. 返回拼接好的长字符串
```

注意第 2c 步：**每次只喂一个新字符 + 旧状态**，而不是把整段已生成文本重新喂一遍。这就是 RNN 比 Transformer 更省算力的地方——Transformer 每步要重算整个上文的自注意力，RNN 只更新一个固定大小的状态。

**生成长文本的隐患**：隐状态容量有限，越往后写，越容易「漂」——早期信息被冲淡，生成慢慢跑偏或陷入循环。这也是后来 Transformer（带显式长程注意力）取代 RNN 做生成的原因之一，下一讲（u4-l6）会讲。

#### 4.3.3 源码精读

**① 状态跨步传递。** 再回看 `generate` 的循环，这次重点放在 `s`：

[GenerativePyTorch.ipynb:L212-L222](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L212-L222) —— 第 3 行 `out, s = net(enc(chars).view(1,-1).to(device))` 是 warmup；循环里 `out, s = net(nc.view(1,-1), s)` 每步只喂一个新字符 `nc` 和上一步状态 `s`，状态就这样无限传下去。把它和 `generate_soft` 对比（L350-L362），会发现**两者的状态传递逻辑完全相同，唯一差别只在挑字符那一行**——这也说明「采样策略」和「状态传递」是正交的两件事，可以独立替换。

**② 多层 LSTM 等改进方向。** Notebook 在训练之后用一节 Markdown 列出了改进点：

> （对应 cell 的说明文字）更好的 minibatch 生成、多层 LSTM、换 GRU、调隐层大小。多层 LSTM 可通过给 `nn.LSTM` 构造器传「层数」参数实现，层数过高会过拟合（直接背下原文）、过低则学不出好结果。

对应源码位于训练循环之后的说明 cell（紧接 [L269-L290](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/GenerativePyTorch.ipynb#L269-L290) 训练 cell）。

**③ lab：字符级升级为词级。** Lab 把本课的字符级生成换成词级，并要求自选一本书当语料：

[lab/README.md:L1-L12](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/17-GenerativeNetworks/lab/README.md#L1-L12) —— 任务是任选一本书（建议用 Project Gutenberg，如《爱丽丝梦游仙境》），用它当数据集训一个**词级**文本生成器。这意味着你要把 `char_tokenizer` 换回本单元前面几讲用过的词级 tokenizer，词表会从 82 暴涨到成千上万，相应地通常要引入嵌入层、增大 `nchars` 对应的词数、并注意 minibatch 的等长处理。

#### 4.3.4 代码实践（本讲的 lab）

**实践目标**：把字符级生成器改造成词级生成器，在一本书上训练并生成风格仿写的续写文本。

**操作步骤**：

1. **选数据集**：从 [Project Gutenberg](https://www.gutenberg.org/) 下载一本书的纯文本（如《爱丽丝梦游仙境》[11-0.txt](https://www.gutenberg.org/files/11/11-0.txt)），读成一个长字符串 `text`。
2. **建词级词表**：复用本单元前面讲过的词级 tokenizer（`torchtext` 的 `basic_english` 分词器，见 `torchnlp.py` 第 10 行）和 `torchtext.vocab.vocab`，把文本切成词、统计建表。
3. **改造 `get_batch`**：把 `char_tokenizer` 换成词级 tokenizer；`nchars` 的语义从「字符数」变成「词数」，建议设 20~50。
4. **改造网络**：词表很大时，one-hot 会很稀疏，考虑把 `LSTMGenerator` 的 one-hot 输入换成 `nn.Embedding`；其余结构（LSTM + 线性输出层）不变。
5. **训练**：复用本课的训练循环（五件套 + 交叉熵），每若干步打印 `generate` / `generate_soft` 的样例，观察是否逐步学到书里的风格。
6. **生成**：用书中一句原文当 `start` 提示，用 `generate_soft` 在合适温度（如 0.7）下续写 300~500 词。

**需要观察的现象**：

- 词表大小从 82 变成几千~几万。
- 训练初期生成的应是不成词的乱码，随着 loss 下降逐渐出现书里常见的词、句式甚至人名。
- 不同温度下生成风格的差异（同 4.2）。

**预期结果**：能得到一段「像那本书风格」的续写文本——词大多正确、局部句式模仿原文，但长程逻辑通常不通顺（这是字符/词级 RNN 的固有局限）。具体生成质量**待本地验证**，取决于语料大小、训练时长、隐层维度。

> 如果时间有限，可先用一小段中文/英文语料（几千字）跑通流程，确认能生成出「词」级别的文本，再扩大语料。

#### 4.3.5 小练习与答案

**练习 1**：`generate` 的循环里每步只把 `(nc, s)` 喂回网络，而不是把「到目前为止生成的整段文本」重新喂一遍。为什么这样是正确的？又有什么代价？

**参考答案**：正确性来自 RNN 的马尔可夫式状态压缩——状态 `s` 已经把迄今为止的全部历史浓缩进去了，所以「新字符 + 旧状态」就等价于「整段历史 + 新字符」，不必重喂。代价是状态容量有限，很早的信息可能在长生成中被冲淡甚至遗忘，导致长程一致性差（这正是 LSTM 门控想缓解、而 Transformer 用显式注意力想根本解决的问题）。

**练习 2**：从字符级换到词级，词表从 82 变成几万。这对训练和生成分別带来什么直接影响？

**参考答案**：训练上，one-hot 输入维度暴涨、输出层（`Linear(hidden, vocab_size)`）参数量暴增，显存和计算开销变大，通常需改用嵌入层并配合更大的隐层与更多数据；稀疏词（低频词）样本少，难学好。生成上，每步直接预测一个词而非拼字符，能更快出现像样的词与搭配，但遇到训练时没见过的词（OOV）会出问题，且词表大导致 softmax over 词表成为瓶颈。

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「读—训—采—生」小任务。

**任务**：在 `GenerativePyTorch.ipynb 基础上，做一个「温度对照生成器」**。

1. **读**：从头跑通 Notebook，确认 `LSTMGenerator` 能在 AG News 上学到「英文新闻味」的字符级生成（loss 降到 1.6 左右、`generate` 能产出 `today and the company ...` 这类片段）。
2. **训**：把训练样本数 `samples_to_train` 从 10000 提到 20000（或外层再套一个 epoch 循环，如 Notebook 鼓励的那样），观察 loss 是否进一步下降、生成文本是否更连贯。
3. **采**：写一个小函数，对**同一段 prompt** 分别用贪心 `generate` 和五档温度的 `generate_soft` 各生成 200 字，整齐打印成对照表。
4. **生**：从对照表里挑出你觉得「最像新闻」的一档温度，用它生成一段 500 字的长文本，并指出它在第几个字符附近开始「跑偏」或循环——以此体会 4.3.1 说的「状态漂移」。

**交付物**：一份对照表（贪心 + 5 档温度）+ 一段长文本 + 一句话点评「贪心为何循环、温度为何能破循环、但温度过高又为何乱码」。

> 这是源码阅读 + 本地实验型实践，运行结果**待本地验证**。

## 6. 本讲小结

- **生成 = 反过来用语言模型**：把「预测下一个字符」的能力接成自回归循环——每步用上一步输出当下一步输入，就能无限续写。
- **训练靠老师强制**：`get_batch` 让目标序列等于输入序列左移一位，于是网络在每个位置学的都是「真正的下一个字符」；损失用交叉熵在所有时间步上一起算。
- **`LSTMGenerator` 是多对多**：one-hot 输入 → LSTM → 每个时间步都接线性层输出词表大小的 logits，返回整条序列的预测加状态。
- **采样策略决定文本形态**：贪心（argmax）确定但易循环；随机（multinomial）按概率抽样更自然；温度 \(\tau\) 通过 \(P(i)\propto\exp(z_i/\tau)\) 在「稳定重复」与「多样发散」间调节，\(\tau\to0\) 退化成贪心、\(\tau\to\infty\) 退化成乱码。
- **长文本靠状态跨步传递**：推理时每步只喂「新字符 + 旧状态」，prompt 暖场后即可逐字生成；但隐状态容量有限，长生成会漂移。
- **字符级 vs 词级**：字符级词表小（82）、无 OOV，但要学拼词；词级词表大、生成更顺，但开销大、怕低频词——lab 把本课从字符级升级到词级。

## 7. 下一步学习建议

本讲是 RNN 系列的收尾，把「读」（分类）和「写」（生成）都讲完了。接下来：

- **下一讲 u4-l6（Transformer 与 BERT）**：RNN 靠隐状态传历史，长程一致性差；Transformer 用**自注意力**让每步直接看到全部上文，是当今生成模型的基础。学完你会发现，本讲的「自回归生成 + 温度采样」这套范式在 GPT 里几乎原样保留，只是把 RNN 换成了 Transformer。
- **u4-l7（NER）**：如果想看 RNN/序列模型在**判别式**任务（序列标注）上的另一面，可接着读。
- **延伸阅读**（README 推荐）：Andrej Karpathy 的 [The Unreasonable Effectiveness of Recurrent Neural Networks](http://karpathy.github.io/2015/05/21/rnn-effectiveness/)——本讲四类拓扑图就来自这里，强烈推荐亲手读一遍；以及 [Keras 的字符级文本生成示例](https://keras.io/examples/generative/lstm_character_level_text_generation/)，可对照 PyTorch 版理解。
