# Transformer 与 BERT

## 1. 本讲目标

本讲是 NLP 单元的关键转折点。前面几讲我们用词袋、词嵌入、RNN 来处理文本，它们都有一个共同软肋：**逐词串行处理，难以并行，长句子还记不住开头**。Transformer 正是为打破这个软肋而生的架构，它催生了 BERT、GPT 等现代大模型。

学完本讲，你应该能够：

1. 说清楚 **自注意力（self-attention）** 到底在算什么，为什么它让模型「看得见上下文」又能「并行计算」。
2. 理解一个 **Transformer 块** 的四件套：位置编码、多头注意力、残差 + 层归一化、前馈网络，以及编码器-解码器的分工。
3. 理解 **BERT** 的「先大规模预训练、再小数据微调」范式，并能动手用 HuggingFace 的预训练 BERT 微调一个文本分类任务并报告指标。

---

## 2. 前置知识

本讲承接讲义 u4-l4（RNN）。开始前请确认你理解以下几个概念：

- **序列到序列任务（sequence-to-sequence / sentence transduction）**：输入一串 token、输出另一串 token，机器翻译是典型代表。RNN 用「编码器（encoder）把输入压成一个隐状态、解码器（decoder）再把隐状态展开成输出」来实现。
- **隐状态（hidden state）**：RNN 每一步的「记忆」。讲义 u4-l4 已说明它随时间步传递。
- **迁移学习 / 微调（transfer learning / fine-tuning）**：讲义 u3-l3 讲过——先冻结预训练特征、只训练新接的「头」，再视情况用更小学习率解冻继续训练。本讲 BERT 的微调套路与此同源。
- **交叉熵损失**：多分类任务的标配，讲义 u2-l5 提到 softmax + 交叉熵成对出现。

一句话回顾 RNN 的痛点（这正是本讲的出发点）：编码器把**整句**压进**一个**最终隐状态，长句开头容易丢；且句中每个词对结果的影响被「一视同仁」。Transformer 用**注意力机制**同时解决了这两点。

---

## 3. 本讲源码地图

本讲只涉及一个课程目录 `lessons/5-NLP/18-Transformers/`，但它含两份互为补充的 Notebook + 一份文字讲义：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md) | 文字讲义：注意力动机、Transformer 两大思想、位置编码、多头自注意力、编码器-解码器注意力、BERT |
| [TransformersTF.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb) | **从零搭**：手写 `TokenAndPositionEmbedding` 与 `TransformerBlock` 做分类，再用 TFHub 的 BERT 做冻结/解冻对比。本讲的「架构」和「注意力实现」细节主要看这里 |
| [TransformersPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb) | **直接用**：用 HuggingFace 预训练 `bert-base-uncased` 在 AG News 上微调分类，跑出约 90% 准确率。本讲的「BERT 微调」实践主要看这里 |
| [torchnlp.py](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/torchnlp.py) | 辅助模块：`load_dataset()` 下载 AG News、`device` 选择 GPU/CPU、`padify` 等。本讲 PyTorch Notebook 用它的数据加载 |
| [assignment.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/assignment.md) | 作业：到 HuggingFace 上玩各类 Transformer 脚本、试官方数据集再换成自己的数据 |

阅读建议：先读 README 建立直觉 → 看 TF Notebook 的 `TransformerBlock` 理解架构 → 看 PyTorch Notebook 跑通 BERT 微调。理解 PyTorch 版即可，两份 Notebook 思想一致。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块层层递进：**自注意力**是 Transformer 的心脏 → **Transformer 架构**把自注意力包装成可堆叠的块 → **BERT** 把一堆 Transformer 编码器块堆成大模型，靠预训练-微调范式落地到具体任务。

### 4.1 自注意力（Self-Attention）

#### 4.1.1 概念说明

先回到动机。课程 README 用机器翻译这个 seq2seq 任务点出 RNN 编码器-解码器的两个毛病：

> ① 编码器的最终状态难以记住句子开头，导致长句质量差；② 序列里所有词对结果的影响被一视同仁，而真实情况是某些词往往更关键。

[README.md:L5-L13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L5-L13) —— 课程用机器翻译引出注意力：RNN 编码器把整句压进一个最终隐状态（长句会忘开头），且所有词影响相同；注意力机制给出解决办法。

**注意力（attention）** 的核心思想：在生成第 \(t\) 个输出词时，不再只依赖编码器那一个最终隐状态，而是回头「看一眼」**所有**输入隐状态 \(h_i\)，并给每个配上不同的权重 \(\alpha_{t,i}\)。这相当于让模型自己决定「此刻该重点关注输入里的哪些词」。

> [README.md:L12-L13](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L12-L13) —— 注意力机制的定义：为每个输入向量对每个输出预测的「上下文影响」加权，靠在输入/输出 RNN 的中间状态之间搭「捷径」实现。

**自注意力（self-attention）** 是注意力的一个特例：输入序列和输出序列是**同一条**。也就是让句子里的**每个词都去和句子里的所有词（含自己）算一遍相关性**，从而得到一个「带着上下文」的新表示。它解决的是「同一个句子里词与词的关系」，最经典的例子是**共指消解（coreference resolution）**：句子里那个 *it* 到底指代谁？自注意力让 *it* 能去「询问」其它词并加权汇总，自然就抓住了指代对象。

> [README.md:L56-L62](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L56-L62) —— 自注意力 = 注意力作用于同一条序列，用于捕捉句内模式、判断词与词（如代词 *it* 的指代）的关联。

自注意力相比 RNN 最大的工程优势是**可并行**。RNN 里第 \(t\) 步必须等第 \(t-1\) 步算完才能开始，只能串行；而自注意力里所有位置可以**同时**计算（本质是一组矩阵乘法），训练时能在 GPU 上大规模并行——这正是 Transformer 能被放大到几十亿参数的前提。

> [README.md:L24](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L24) —— 放大 RNN 的关键障碍：其「循环」本性使训练难以批量化、并行化，序列必须按顺序逐元素处理。

#### 4.1.2 核心流程

自注意力的标准实现叫**缩放点积注意力（scaled dot-product attention）**。它的数学定义（来自《Attention is all you need》论文，也就是课程标题所引的那篇）是：

\[
\text{Attention}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
\]

其中 \(Q\)（查询 Query）、\(K\)（键 Key）、\(V\)（值 Value）都是输入序列经过三个不同线性变换得到的矩阵。把它拆成「三步直觉」最好理解：

1. **打分（\(QK^\top\)）**：查询与键做点积，衡量「token \(i\) 对 token \(j\) 有多相关」。点积越大越相关——这和讲义 u4-l2 里词向量用内积衡量相似度是一回事。
2. **归一化（\(\mathrm{softmax}\)）**：把打分变成一组加起来为 1 的权重 \(\alpha_{i,j}\)，表示 token \(i\) 该把多少注意力分配给每个 token \(j\)。除以 \(\sqrt{d_k}\) 是为了**缩放**：维度高时点积数值会变大，softmax 会进入梯度近乎为 0 的饱和区，缩放能稳定训练。
3. **取值（\(\times V\)）**：用权重对「值」做加权求和，得到 token \(i\) 的新表示——即「融合了相关上下文」的表示。

伪代码：

```
对序列 X（n 个 token，每个 d 维）：
  Q = X @ Wq      # 每个词变成「我在找什么」
  K = X @ Wk      # 每个词变成「我能提供什么」
  V = X @ Wv      # 每个词变成「我携带的内容」
  打分 = Q @ K.T / sqrt(d_k)        # n×n 相关性矩阵
  权重 = softmax(打分, 每行)         # 每行加起来=1
  输出 = 权重 @ V                    # n×d，每个词的新表示
```

**关键直觉**：自注意力里查询、键、值都来自同一条输入序列，所以叫「自」。整个计算都是矩阵乘法，\(n\) 个位置一次性算完，完全并行。

**多头注意力（multi-head attention）** 则是把上面这件事**并行做 \(h\) 遍**：用 \(h\) 组不同的 \((W_q,W_k,W_v)\) 投影，得到 \(h\) 个「头」，每个头能学到一种不同的词间关系（比如长距离依赖 vs 短距离搭配、句法 vs 语义、共指 vs 其它），最后把各头结果拼接后再做一次线性变换。

> [README.md:L64](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L64) —— 用多头注意力让网络有能力捕捉多种不同类型的依赖关系。

#### 4.1.3 源码精读

本课的 Notebook **没有**从零实现 \(Q/K/V\)，而是直接调用框架内置的多头注意力层。我们以 TF Notebook 里自建的 `TransformerBlock` 为例，看自注意力在代码里到底「长什么样」：

[TransformersTF.ipynb:L106-L124](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L106-L124) —— 手写的 `TransformerBlock`。其中 `self.att = keras.layers.MultiHeadAttention(num_heads=num_heads, key_dim=embed_dim, name='attn')`（[L109](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L109)）创建一个多头注意力层；调用时写的是 `attn_output = self.att(inputs, inputs)`（[L119](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L119)）——**两个参数都传 `inputs`**，正是「自」注意力的标志：查询和键/值都来自同一条输入，框架内部据此完成上面伪代码里的 \(QK^\top\)、softmax、\(\times V\) 全套运算。

注意「自注意力」体现在**调用方式**上而非新算子：同一个 `MultiHeadAttention` 层，传同一个张量做查询和键值就是自注意力；若传「解码器的查询 + 编码器的键值」就成了下文要讲的编码器-解码器注意力。

#### 4.1.4 代码实践

这是一个**手算型实践**，帮你把抽象公式变直观。下面的代码是**示例代码**（非仓库原有），用一个 3 词的玩具句子演示缩放点积注意力。

**实践目标**：亲眼看到「相关性矩阵 + softmax 加权」如何把每个词变成上下文表示。

**操作步骤**（在自己的 `ai4beg` 环境里新建一个 cell 运行）：

```python
# 示例代码：手动实现缩放点积注意力，观察权重矩阵
import torch
import torch.nn.functional as F

torch.manual_seed(0)
n, d = 3, 8                      # 3 个词，每个 8 维
X = torch.randn(n, d)            # 假装这是 3 个词的嵌入

Wq = torch.randn(d, d)
Wk = torch.randn(d, d)
Wv = torch.randn(d, d)

Q, K, V = X @ Wq, X @ Wk, X @ Wv
scores = Q @ K.T / (d ** 0.5)    # 缩放点积
weights = F.softmax(scores, dim=1)
out = weights @ V

print("注意力权重矩阵（每行加起来=1）:\n", weights)
print("每行之和:", weights.sum(dim=1))
```

**需要观察的现象**：

1. `weights` 是一个 \(3\times3\) 矩阵，`weights[i]` 表示第 \(i\) 个词分配给三个词的注意力比例，每行加起来恰为 1。
2. 对角线上的值通常较大——因为每个词和自己最相关。
3. 改一改 `Wq/Wk` 的随机种子，权重分布会变，说明 \(Q/K\) 投影矩阵是**要学的参数**，决定「该关注谁」。

**预期结果**：能打印出 \(3\times3\) 的权重矩阵、且每行之和为 1；若去掉 `/ (d**0.5)` 缩放，某些行的权重会更接近 one-hot（softmax 更「尖」），这就是缩放防止饱和的意义。

> 说明：本课 Notebook 用框架内置层而非手写注意力，所以这条实践需要你自行粘贴示例代码运行；运行结果（具体数值）**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：自注意力和讲义 u4-l4 的 RNN 相比，最大的工程优势是什么？为什么？

**参考答案**：可并行。RNN 第 \(t\) 步依赖第 \(t-1\) 步的隐状态，只能串行计算；自注意力里所有位置的 \(QK^\top\) 是一次矩阵乘法，可同时算出，因此能在 GPU 上大规模并行，这也是 Transformer 能放大到超大模型的前提。

**练习 2**：公式里为什么要除以 \(\sqrt{d_k}\)？

**参考答案**：当维度 \(d_k\) 较大时，点积 \(QK^\top\) 的数值会变大，把 softmax 推进到饱和区（输出接近 one-hot、梯度趋近于 0），训练会变慢甚至停滞。除以 \(\sqrt{d_k}\) 把方差缩回合理范围，使 softmax 保持适度平滑、梯度健康。

**练习 3**：`MultiHeadAttention` 层调用时传 `self.att(inputs, inputs)`，为什么说这就是「自」注意力？

**参考答案**：自注意力的定义是查询、键、值来自同一条序列。这里查询（第一个参数）和键/值（第二个参数）都传同一个 `inputs`，相当于序列自己和自己算相关性，故为自注意力。若换成「解码器查询 + 编码器键值」，则变成跨序列的编码器-解码器注意力。

---

### 4.2 Transformer 架构

#### 4.2.1 概念说明

自注意力虽好，但有两个缺口必须补上，才能拼成一个真正能用的网络：

1. **没有顺序信息**。自注意力本质是个「集合」操作：它只看词与词的相关性，**完全不在乎词的先后**。你把句子打乱顺序喂进去，每个词得到的新表示只是跟着位置换了个位——但「猫追老鼠」和「老鼠追猫」对模型来说关系结构一样，这显然不行。所以必须显式注入**位置信息**。
2. **缺非线性与稳定性结构**。光有一层注意力不够，需要堆叠很多层，而深层网络需要残差连接、归一化、前馈网络等组件才训得动。

Transformer（《Attention is all you need》）用两个核心思想补齐了这些缺口，课程把它们概括得很清楚：

> 一是**位置编码**，二是**用自注意力替代 RNN/CNN 来捕捉模式**。

[README.md:L32-L37](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L32-L37) —— Transformer 的核心思想：避免 RNN 的串行性、做成可并行训练的模型；靠两件事实现——位置编码 + 自注意力。

#### 4.2.2 核心流程

一个完整的 Transformer 由 **编码器（Encoder）** 和 **解码器（Decoder）** 两部分组成，最初为机器翻译设计。理解其结构只需抓住四块积木：

**(1) 位置编码 / 位置嵌入（Positional Encoding/Embedding）**

思路是：给每个 token 再配一个「它是第几个词」的位置编号，然后把**词嵌入**和**位置嵌入**叠加，得到既含「是什么」又含「在哪里」的表示。课程给出两种做法：

- **可训练的位置嵌入**（本课 TF Notebook 采用）：另开一个 `Embedding` 层，输入是位置序号 \(0,1,\dots,\text{maxlen}-1\)，输出和词嵌入同维度的向量，两者**相加**。
- **固定的位置编码函数**（原论文采用）：用不同频率的正余弦函数生成位置向量。

> [README.md:L45-L54](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L45-L54) —— 位置编码的做法：把 token 序列配上位置序列（0,1,2…），再用「可训练嵌入」或「原论文的固定函数」把位置变成向量，与词嵌入相加，使表示同时编码了 token 本身和它在句中的位置。

**(2) Transformer 块（Transformer Block）**

这是 Transformer 的标准层，输入输出形状一致，可以无限堆叠。一个块内含两个子层，每个子层都套着「残差连接 + 层归一化」：

```
输入 x
 ├─ 子层1：多头自注意力  →  attn
 │    out1 = LayerNorm(x + attn)          # 残差 + 归一化
 └─ 子层2：前馈网络 FFN（两层全连接，中间 ReLU）→  ffn
      out2 = LayerNorm(out1 + ffn)        # 再次残差 + 归一化
返回 out2
```

- **残差连接（\(x + \text{子层}(x)\)）**：把输入直接加到子层输出上，让梯度能「抄近路」流回浅层，缓解深层网络的梯度消失——和讲义 u3-l2 讲的 ResNet 残差块同理。
- **层归一化（LayerNorm）**：对**单个样本**的特征维做归一化（拉到均值 0、方差 1 附近）。注意它和讲义 u3-l3 的**批归一化（BatchNorm）**不同：BatchNorm 跨 batch 样本归一化、依赖 batch 大小；LayerNorm 每个样本独立归一、不依赖 batch，更适合变长序列。
- **前馈网络（FFN）**：两层全连接 + ReLU，给每个位置独立地做一次非线性变换，补充注意力所没有的表达能力。

**(3) 编码器-解码器分工**

- **编码器**：堆叠多个 Transformer 块，每块用**自注意力**读入整条输入，输出一组「带满上下文」的表示。
- **解码器**：也堆叠多个块，但每块有两个注意力子层——先是**带掩码的自注意力**（生成第 \(t\) 个词时只能看到 \(<t\) 的词，不能「偷看」未来），再是**编码器-解码器注意力**：解码器的词当**查询**，编码器的输出当**键/值**，从而「对照原文做翻译」。

课程强调注意力在 Transformer 里出现在**两处**：

> 一是用自注意力捕捉输入文本内部的模式；二是夹在编码器与解码器之间做序列翻译。

[README.md:L68-L79](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L68-L79) —— Transformer 里注意力用在两处：输入内部的**自注意力**，以及编码器-解码器之间的**跨注意力**；由于每个输入位置独立映射到每个输出位置，Transformer 比 RNN 更易并行，从而支持更大、更有表达力的语言模型。

**(4) 一个重要区分（为下一模块铺垫）**

- **BERT = 编码器栈**：自注意力是**双向**的，每个词同时看左右文，适合「理解」类任务（分类、问答、NER）。
- **GPT = 解码器栈**：自注意力带掩码、**从左到右**，适合「生成」类任务。讲义 u4-l8 会展开。

#### 4.2.3 源码精读

TF Notebook 手写了一个迷你 Transformer 用于 AG News 分类，正好把这四块积木完整展示出来。先看位置嵌入层：

[TransformersTF.ipynb:L74-L86](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L74-L86) —— `TokenAndPositionEmbedding`：内部有两个 `Embedding` 层，`token_emb`（[L77](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L77)）编码词、`pos_emb`（[L78](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L78)）编码位置。`call` 里先用 `tf.range(0, maxlen)` 生成位置序号（[L83](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L83)）并过 `pos_emb`，再把词嵌入与位置嵌入**相加**返回（`return x+positions`，[L86](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L86)）——这就是「可训练位置嵌入」做法的落地。

接着是 Transformer 块本身：

[TransformersTF.ipynb:L106-L124](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L106-L124) —— `TransformerBlock`：`MultiHeadAttention`（[L109](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L109)）做自注意力，前馈网络是两层 `Dense`（ReLU 激活，[L110-L111](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L110-L111)）。`call` 里 `out1 = self.layernorm1(inputs + attn_output)`（[L121](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L121)）是「自注意力 + 残差 + 层归一化」，`return self.layernorm2(out1 + ffn_output)`（[L124](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L124)）是「前馈 + 残差 + 层归一化」——正是上面伪代码描述的两子层结构。

最后把它们拼成完整模型（这段属于分类用的「编码器风格」Transformer，没有解码器）：

[TransformersTF.ipynb:L185-L185](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L185) —— 用 `keras.Sequential` 把「文本向量化 → 位置嵌入 → Transformer 块 → 全局平均池化 → Dropout → 全连接 → 4 类 softmax」串起来，在 AG News 上训练约 0.81 训练准确率、0.91 验证准确率（见 Notebook 输出）。这证明了「位置编码 + 自注意力 + 残差/归一化/FFN」这套积木在分类任务上确实有效。

> 课程提示：Keras 没有内置 Transformer 层，所以这里手写；Transformer 在更难的 NLP 任务上才最能体现优势（见 Notebook cell-1 末尾说明）。README 也指出实现细节主要在 [TF Notebook](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L66)（[README.md:L66](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L66)）。

#### 4.2.4 代码实践

这是一个**调参观察型实践**，帮你理解多头注意力里 `num_heads`（头数）和 `embed_dim`（嵌入维度）如何影响参数量。

**实践目标**：通过改 `num_heads`，观察 `TransformerBlock` 的参数量变化，直观感受「多头」的开销。

**操作步骤**：

1. 打开 [TransformersTF.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb)，运行到 `model.summary()`（[L185](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L185) 附近），记下默认 `num_heads=2, embed_dim=32, ff_dim=32` 时 `transformer_block` 这一层的参数量（Notebook 显示为 10656）。
2. 把定义 `num_heads` 的 cell 改成 `num_heads = 4`，再次运行 `model.summary()`。
3. 再改成 `num_heads = 8`，对比三组参数量。

**需要观察的现象**：

- 头数翻倍，`transformer_block` 的参数量会**上升**（多头意味着更多组 \(W_q/W_k/W_v\) 投影矩阵）。
- 但因为每个头的维度相应变小（总维度 `embed_dim` 不变），参数量并非线性翻倍。

**预期结果**：`num_heads=2 → 4 → 8` 时，`transformer_block` 参数量单调上升；若观察到几乎不变，说明该实现里投影矩阵总参数与头数关系被设计成近似守恒（具体数值**待本地验证**，因 Keras 版本略有差异）。

**思考延伸**：头数是不是越多越好？不是——头太多会让每个头维度太小、表达力下降，且更难训练。`embed_dim` 必须能被 `num_heads` 整除，否则会报错。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Transformer 必须加位置编码，而 RNN 不需要？

**参考答案**：RNN 按时间步**串行**处理，相对位置天然隐含在「第几步」里，无需额外表示。而自注意力是「集合」操作，对输入顺序不敏感（打乱输入只等价于打乱输出），所以必须显式把位置信息注入到嵌入里，模型才能区分词序。

**练习 2**：Transformer 块里的 `LayerNorm` 和计算机视觉里讲过的 `BatchNorm` 有何不同？这里为什么选 `LayerNorm`？

**参考答案**：BatchNorm 跨一个 batch 内的多个样本、对同一特征维做归一化，依赖 batch 统计量、受 batch 大小影响；LayerNorm 对**单个样本**的所有特征维做归一化，与 batch 无关。NLP 序列长度可变、batch 可能很小，LayerNorm 更稳定，所以 Transformer 用它。

**练习 3**：编码器-解码器注意力和自注意力的查询/键/值分别来自哪里？

**参考答案**：自注意力中查询、键、值都来自同一条序列；编码器-解码器注意力中，**查询**来自解码器（当前要生成的位置），**键和值**来自编码器的输出（整条输入的上下文表示），从而实现「对照输入做翻译」。

---

### 4.3 BERT 预训练-微调

#### 4.3.1 概念说明

**BERT**（Bidirectional Encoder Representations from Transformers，基于 Transformer 的双向编码器表示）就是把一堆 Transformer **编码器**块堆成的大模型：`BERT-base` 有 12 层，`BERT-large` 有 24 层。它的核心卖点是「**双向**」——因为编码器的自注意力每个词能同时看左文和右文（区别于 GPT 的从左到右），所以 BERT 擅长**理解**而非生成。

BERT 的精髓是**预训练-微调（pre-train then fine-tune）** 范式，分两个阶段：

1. **预训练（无监督、海量语料）**：在维基百科 + 书籍等大规模**无标注**文本上，用**掩码语言模型（Masked Language Model, MLM）** 训练——随机盖住句子里约 15% 的词，让模型根据**上下文（左右双向）**去猜被盖住的词。因为「猜词」这件事不需要人工标注（盖住的词本身就是答案），这就是讲义 u4-l3 讲过的**自监督学习**。预训练后，模型「吸收了大量语言知识」。
2. **微调（有监督、少量标注）**：在预训练好的 BERT 上面接一个小小的任务头（如分类器），用你**自己**的有标注数据，用很小的学习率稍加训练，就能把语言知识迁移到具体任务上——这叫**迁移学习**，和讲义 u3-l3 的套路同源。

> [README.md:L81-L87](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L81-L87) —— BERT 是多层 Transformer（base 12 层、large 24 层），先在维基+书籍语料上用「预测被掩码词」做无监督预训练、吸收语言理解，再通过微调迁移到其它数据集，即迁移学习。

为什么这套范式威力巨大？因为预训练只需做**一次**（且由大厂做好了），下游每个人只需要用很少的标注数据微调，就能得到接近 SOTA 的效果。课程点明：BERT 之外还有 DistilBERT、BigBird、GPT 等多种变体，都可微调，HuggingFace 库统一提供了这些架构。

> [README.md:L96-L98](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/README.md#L96-L98) —— Transformer 架构有许多变体（BERT、DistilBERT、BigBird、GPT3 等）可被微调；HuggingFace 仓库用 PyTorch 和 TensorFlow 提供了训练这些架构的统一工具。

#### 4.3.2 核心流程

用预训练 BERT 做文本分类，整体流程是「换词表 → 造数据 → 加分类头 → 小学习率微调 → 评估」。下面把 PyTorch Notebook 的步骤梳理成流程：

```
1. 选模型与分词器：bert-base-uncased（大小写不敏感）
   ├─ tokenizer = BertTokenizer.from_pretrained(...)   # 必须用 BERT 自己的 WordPiece 分词器
2. 数据预处理：把每条文本 encode 成 token id 序列，按 batch 内最长做 padding
3. 加载带分类头的模型：BertForSequenceClassification.from_pretrained(num_labels=4)
   └─ BERT 主体已预训练；只有最后的 classifier 是随机初始化（需训练）
4. 微调训练（小学习率 2e-5）：
   for batch:
      loss, out = model(texts, labels=labels)   # 模型直接返回 loss 和 logits
      optimizer.zero_grad(); loss.backward(); optimizer.step()
5. 评估：model.eval() 后在测试集上算准确率
```

有几个**必须理解的关键点**：

- **分词器必须匹配**。BERT 用自己的 **WordPiece** 词表（把词拆成子词），不能用前面几讲用的 `basic_english`。课程强调：必须使用与预训练时**同一个**分词器，否则 token id 对不上、模型直接失效。
- **模型自带分类头**。`BertForSequenceClassification` 已经在 BERT 主体后面接好了线性分类器，加载时会提示「分类器权重是新初始化的、需要训练」——这正是微调的对象，符合预期。
- **小学习率**。微调用 `lr=2e-5`（远小于从头训练的 0.01），目的是「轻微调整」预训练权重、不要把它们冲坏——这呼应讲义 u3-l3「先冻结、再解冻」时用小学习率的铁律。
- **模型直接返回 loss**。给定 `labels`，`BertForSequenceClassification` 内部就用交叉熵算好 loss 一起返回，省去自己写损失函数。

课程还通过 TF Notebook 给出一个**重要对照**：把 BERT **冻结**（只训练分类头，可训练参数从约 1.1 亿骤降到几百），准确率只有约 0.79；而**解冻** BERT 并用 AdamW + warmup 微调，能提升到约 0.82（但训练很慢）。这正对应讲义 u3-l3 的「特征提取 vs 微调」两种迁移策略。

#### 4.3.3 源码精读

**① 加载 BERT 分词器（必须用模型自带的 WordPiece 分词器）。**

[TransformersPyTorch.ipynb:L91-L97](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L91-L97) —— `bert_model = 'bert-base-uncased'`（[L91](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L91)）指定模型名，`tokenizer = transformers.BertTokenizer.from_pretrained(bert_model)`（[L97](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L97)）加载配套分词器。`bert-base-uncased` 表示「基础版、不区分大小写」。`encode` 把句子变成 token id 序列（[L128](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L128) 的示例输出首尾的 `101`/`102` 是 BERT 特有的 `[CLS]`/`[SEP]` 标记）。

**② 用 BERT 分词器做批量 padding。**

[TransformersPyTorch.ipynb:L144-L154](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L144-L154) —— `pad_bert` 是给 `DataLoader` 用的 `collate_fn`：对 batch 内每条文本调用 `tokenizer.encode` 转成 id 序列，算出 batch 内最长长度，再用 `F.pad(..., value=0)` 把短序列补 0 对齐（[L154](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L154)）。这和讲义 u4-l1 的 `padify` 思路一致，区别只是这里用 BERT 的 `encode` 而非 `basic_english`。

**③ 加载带分类头的 BERT（分类头是新初始化的）。**

[TransformersPyTorch.ipynb:L186-L186](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L186) —— `BertForSequenceClassification.from_pretrained(bert_model, num_labels=4)`：BERT 主体加载预训练权重，最后接一个 4 类的分类器（AG News 有 4 类）。运行时会出现「`classifier.weight`/`classifier.bias` 未从 checkpoint 初始化、需训练」的警告——这正是微调要训练的部分，**符合预期**。

**④ 小学习率微调，模型直接返回 loss。**

[TransformersPyTorch.ipynb:L225-L240](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L225-L240) —— 训练循环：`optimizer = torch.optim.Adam(model.parameters(), lr=2e-5)`（[L225](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L225)）用很小的学习率；`loss, out = model(texts, labels=labels)[:2]`（[L239](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L239)）模型同时返回损失和 logits——给了 `labels` 它就内部算好交叉熵；随后标准的 `zero_grad → backward → step`（讲义 u2-l5 的 PyTorch 五件套在此简化为「模型自带 loss」）。注意 `labels = labels.to(device)-1` 把标签从 1~4 平移到 0~3，因为分类器输出索引从 0 开始。

**⑤ 评估并报告准确率。**

[TransformersPyTorch.ipynb:L291-L304](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L291-L304) —— 评估循环：先 `model.eval()`（[L291](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L291)）切到推理模式（关闭 Dropout），再用 `argmax` 比对预测与真实标签累加准确率。Notebook 给出的最终测试准确率约为 **0.9047**（[L286](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb#L286)）——远高于讲义 u4-l1 词袋的约 0.86，体现了预训练-微调的优势。

**对照：TF Notebook 里冻结 vs 解冻 BERT。**

[TransformersTF.ipynb:L722-L722](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L722) —— `model.layers[0].trainable = False` 把 BERT 主体**冻结**（特征提取模式），可训练参数从约 1.1 亿骤降到 516，对应 Notebook 输出验证准确率仅约 0.79；随后 Notebook 再解冻并用 AdamW + warmup 微调，准确率升到约 0.82。这正是讲义 u3-l3「特征提取 vs 微调」两种迁移策略在 BERT 上的复现。

#### 4.3.4 代码实践

本实践对应课程的 [assignment.md](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/assignment.md)：到 HuggingFace 上用预训练 BERT 微调一个文本分类任务并报告指标。分两步走。

**实践目标**：① 先跑通课程自带的 BERT 微调、复现约 90% 准确率；② 再用 HuggingFace 的 `pipeline` 或 `run_glue` 脚本，把 BERT 用到一个**你自己**的小数据集上并报告指标。

**操作步骤**：

第一步——复现课程 Notebook：

1. 在 `ai4beg` 环境里安装依赖：`pip install transformers`（PyTorch 版 Notebook 用 HuggingFace `transformers`）。
2. 打开 [TransformersPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb)，把第 3 个 cell 里 `bert_model = './bert'` 改回 `bert_model = 'bert-base-uncased'`（本地自行运行时从网络拉取模型；课程沙箱里才用预置的 `./bert` 目录）。
3. 从头运行所有 cell。训练 cell 里 `iterations = 500`，建议有 GPU 时调大（如 2000）以观察准确率收敛。

第二步——按作业要求用 HuggingFace 跑自己的数据（**示例代码**，非仓库原有）：

```python
# 示例代码：用 HuggingFace pipeline 做零样本/微调文本分类
from transformers import pipeline

# 零样本分类（无需训练，直接用现成模型体验 BERT 类模型的能力）
clf = pipeline("zero-shot-classification",
               model="facebook/bart-large-mnli")
print(clf("NASA launches a new satellite.",
          candidate_labels=["politics", "science", "sports"]))
```

更贴近作业的做法是参考 HuggingFace 官方脚本：<https://huggingface.co/docs/transformers/run_scripts>，用 `run_glue.py` 在 GLUE 某个子集（或你从课程/ Kaggle 导入的数据）上微调 `bert-base-uncased`。

**需要观察的现象**：

1. 第一步：训练过程中每 50 步打印的 Loss 单调下降、Accuracy 从约 0.58 升到 0.90+；评估 cell 输出 `Final accuracy` 约为 0.90。
2. 第二步：`pipeline` 能直接给出每条文本属于各候选标签的概率；`run_glue` 训练结束后会报告 GLUE 指标（如准确率或 Matthews 相关系数）。

**预期结果**：

- 课程 Notebook：训练 Accuracy 收敛到约 0.90、测试 `Final accuracy ≈ 0.9047`（与 Notebook 输出一致）。
- 你的自定义数据：准确率取决于数据集，但通常只需很少的标注样本就能超过讲义 u4-l1 的词袋基线；具体数值**待本地验证**。

> 注意：完整微调 BERT 很吃算力（GPU 优先，最好多卡）。若本地无 GPU，可把 `iterations` 调小、或先用 `pipeline` 体验，再理解微调代码即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么微调 BERT 要用 `lr=2e-5` 这样小的学习率，而不是从头训练常用的 0.01？

**参考答案**：BERT 主体已在海量语料上预训练好、含丰富的语言知识。微调目的是「轻微调整」它以适配下游任务，用大学习率会用随机梯度的剧烈更新把预训练权重「冲坏」，所以必须用很小的学习率（如 2e-5）做精细调整。这与讲义 u3-l3「解冻后用更小学习率」是同一原则。

**练习 2**：为什么必须用 `BertTokenizer`，而不能用前面几讲的 `basic_english` 分词器？

**参考答案**：BERT 预训练时用的是它自己的 **WordPiece** 词表（把词拆成子词，并有 `[CLS]`/`[SEP]` 等特殊标记）。模型的嵌入层是按这个词表的 id 索引的，喂入别的分词器产生的 id 对不上，模型直接失效。所以分词器必须与预训练时完全一致。

**练习 3**：BERT 和 GPT 都是 Transformer，但一个擅长理解、一个擅长生成，根本区别在哪？

**参考答案**：架构与注意力方向不同。BERT 是**编码器栈**，自注意力**双向**（每个词同时看左右文），适合理解类任务（分类、问答、NER）；GPT 是**解码器栈**，自注意力**带掩码、从左到右**，只能根据上文预测下一个词，适合生成类任务。讲义 u4-l8 会展开 GPT 类大模型。

---

## 5. 综合实践

**任务：从 RNN 到 Transformer 的对照实验，理解注意力与预训练带来的提升。**

把本讲三块知识串起来，做一个最小对照实验（可在 [TransformersPyTorch.ipynb](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersPyTorch.ipynb) 基础上改）：

1. **基线（讲义 u4-l4 的 RNN）**：用 LSTM 在 AG News 上做 4 类文本分类，记录测试准确率与单 epoch 训练时间。
2. **自建 Transformer（本讲 4.2）**：参考 [TransformersTF.ipynb 的 `TransformerBlock`](https://github.com/microsoft/AI-For-Beginners/blob/fa78bc6fb0b30eea0678c27a54b915b79ad16fe8/lessons/5-NLP/18-Transformers/TransformersTF.ipynb#L106-L124)，用 PyTorch 复刻一个「位置嵌入 + 多头自注意力 + FFN」的小 Transformer 做同样的 AG News 分类，记录准确率与训练时间。
3. **预训练 BERT 微调（本讲 4.3）**：直接跑课程 PyTorch Notebook，记录测试准确率（约 0.90）。
4. **分析**：把三者填入下表，回答两个问题——

   | 模型 | 测试准确率 | 单 epoch 训练时间 | 可否高度并行 |
   | --- | --- | --- | --- |
   | LSTM（RNN） |  |  | 否 |
   | 自建小 Transformer |  |  | 是 |
   | 预训练 BERT 微调 | ≈0.90 |  | 是 |

   - 自注意力带来了多少准确率提升？预训练又额外贡献了多少？
   - 同样数据下，Transformer 的并行性如何体现在训练时间上？

**交付物**：一张对照表 + 一段 200 字以内的结论，说明「注意力机制」与「预训练-微调」各自对效果的贡献。具体数值**待本地验证**（取决于硬件与超参）。

---

## 6. 本讲小结

- **自注意力**让序列里每个词都去和所有词算相关性（缩放点积公式 \(\text{Attention}(Q,K,V)=\mathrm{softmax}(QK^\top/\sqrt{d_k})V\)），既捕捉了上下文（如代词指代），又能全位置并行计算——这是 Transformer 取代 RNN 的根本原因。
- **Transformer 架构**靠四块积木拼成：位置编码（补顺序信息）、多头自注意力（多视角看关系）、残差 + 层归一化（稳住深层训练）、前馈网络（补非线性）；编码器负责双向理解、解码器带掩码并靠编码器-解码器注意力做翻译。
- **BERT** 是 Transformer **编码器**栈（base 12 层 / large 24 层），自注意力双向，靠**掩码语言模型**在海量无标注语料上**预训练**，再用很小学习率在下游任务上**微调**——这是现代 NLP 的「一次预训练、人人微调」范式。
- 落地 BERT 微调有三个要点：**必须用配套的 WordPiece 分词器**、**模型自带分类头只需训练它**、**用 `lr=2e-5` 级别的小学习率**；课程在 AG News 上达到约 0.90 测试准确率。
- 工程对照：冻结 BERT（特征提取）参数少但效果一般（约 0.79），解冻微调（用 AdamW + warmup）效果更好（约 0.82）但更慢——这与讲义 u3-l3 的迁移学习策略完全一致。
- BERT（编码器、理解）与 GPT（解码器、生成）的分野，是理解后续大语言模型（讲义 u4-l8）的关键。

---

## 7. 下一步学习建议

- **继续本单元**：下一讲 u4-l7（命名实体识别 NER）会把序列标注任务与 BIO 标注体系落地，BERT 类模型正是 NER 的主力，可立刻把本讲的 `BertForSequenceClassification` 换成 `BertForTokenClassification` 体验。
- **通向大模型**：u4-l8（大语言模型与提示编程）会从 GPT 的自回归解码出发，讲解提示工程与少样本学习——建议把本讲「BERT 双向理解」与 u4-l8「GPT 单向生成」对照学习。
- **深入源码**：想真正看清自注意力的 Q/K/V 实现，可阅读 HuggingFace `transformers` 库里 `models/bert/modeling_bert.py` 的 `BertSelfAttention` 类，对照本讲的缩放点积公式逐行验证。
- **补原理**：本课 README 的「Review & Self Study」给出了《Attention is all you need》原论文的图解博客，强烈推荐读一遍，把本讲的公式和论文里的「缩放点积注意力 + 多头」图示对上号。
