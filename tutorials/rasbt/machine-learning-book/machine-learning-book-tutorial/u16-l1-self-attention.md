# 自注意力与多头注意力从零实现

> 对应仓库文件：`ch16/ch16-part1-self-attention.py`（第 16 章 Part 1/3）
> 前置讲义：u15-l1（RNN 原理与手算示例）、u12-l1（张量基础）、u12-l3（softmax 与训练循环）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「查询—键—值」（Query / Key / Value）这个数据库类比在注意力机制里各自的角色。
2. 不借助任何封装，用纯 `torch` 张量运算手写出未参数化自注意力的三步：打分 \(\omega\) → 归一化 \(\alpha\) → 加权求和上下文向量 \(\boldsymbol{z}\)。
3. 解释缩放点积注意力（scaled dot-product attention）为什么要引入三组可学习的投影矩阵，以及为什么 softmax 之前要除以 \(\sqrt{d_k}\)。
4. 读懂仓库中多头注意力（multi-head attention）的 `repeat` + `bmm` + `permute` 实现，并能独立追踪每一步张量形状。
5. 理解因果掩码（causal mask）与位置编码（positional encoding）分别堵上自注意力的哪两个「先天缺陷」，并能用示例代码演示掩码效果。
6. 完成综合实践：为 6 个词的句子从零实现一个带缩放、带掩码选项的多头注意力函数。

本讲全部代码都可以在没有 GPU 的环境运行，只需要 `torch`。

## 2. 前置知识

本讲是全书从 RNN 跨入 Transformer 的转折点，用到的旧知识如下，忘记的可以回看对应讲义：

- **RNN 的困境（u15-l1）**：RNN 沿时间步递推 \(\boldsymbol{h}_t=\tanh(W_{xh}\boldsymbol{x}_t+W_{hh}\boldsymbol{h}_{t-1}+\boldsymbol{b}_h)\)，序列开头的信息要经过一长串乘法才能到达末尾，路径长度是 \(O(T)\)；且 `tanh` 饱和区导数趋近 0，长序列上梯度消失。**注意力机制的动机正是：让任意两个位置的词直接「点对点」交互，路径长度降为 \(O(1)\)**。
- **词嵌入（u15-l2）**：`nn.Embedding` 是一张「整数词 ID → 稠密向量」的查找表。自注意力的输入就是一排这样的向量。
- **矩阵乘法与形状变换（u12-l1）**：`matmul`、转置 `.T`、批量矩阵乘 `torch.bmm`、维度重排 `permute`。本讲的形状记账比以往任何一讲都重要，建议手边放纸笔。
- **softmax（u12-l3）**：把一组任意实数分数变成非负、和为 1 的概率分布。本讲中它是「把相似度打分变成加权系数」的工具。
- **梯度饱和的直觉（u11-l1）**：sigmoid 在两端饱和、导数趋 0。softmax 也一样——当输入分数差距悬殊时，输出接近 one-hot，梯度几乎消失。这是理解 \(\sqrt{d_k}\) 缩放的关键。

一个值得先记住的对比：RNN 用**循环**处理序列，自注意力用**矩阵乘法**一次处理整个序列——这也是 Transformer 训练可以大规模并行的根源。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch16/ch16-part1-self-attention.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py) | 本讲核心：从零实现基本自注意力、缩放点积注意力、多头注意力 |
| [ch16/ch16-part1-self-attention.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.ipynb) | 同一内容的 notebook 版，保留每步真实输出与插图，本讲引用的数值结果均出自此处 |
| `ch16/figures/16_04.png` ~ `16_09.png` | 章节插图：注意力权重示意（16_04）、QKV 结构（16_05）、Transformer 总架构（16_06）、多头（16_07）、因果掩码（16_08）、位置编码（16_09） |
| [ch16/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/README.md) | 第 16 章大纲 |

**一个必须先说清的仓库事实**：脚本中「因果掩码」（L352）与「位置编码」（L358）两节**只有标题、没有任何可运行代码**，notebook 里对应位置只嵌入了插图。本讲第 4.5 节对这两个主题做概念讲解，演示用的代码均为明确标注的「示例代码」，不属于仓库原有内容。第 16 章的可运行代码从 Part 2（GPT-2 微调）才恢复。

## 4. 核心概念与源码讲解

### 4.1 单词嵌入表构建

#### 4.1.1 概念说明

自注意力机制的输入不是文字，而是**一组向量**，每个词一个。把词变成向量靠的是嵌入表（u15-l2 已建立过这个概念）：表的第一维是词表大小，第二维是嵌入维度 \(d\)；给定一个整数 ID，查表就得到该词的 \(d\) 维向量。

仓库选了一个 8 词句子 "Can you help me to translate this sentence"，先用第 15 章的方式把它编码成整数 ID 序列，再查一张 `Embedding(10, 16)` 的表，得到形状为 `(8, 16)` 的张量：8 个词、每词 16 维。

有两点值得强调：

- **词表大小是 10 而不是 8**：ID 只要落在 `[0, 10)` 内即可，词表留了余量。
- **`torch.manual_seed(123)`**：嵌入表初始值是随机数，钉死种子才能复现书里的数值（这与我们在 u2-l3 强调的可复现性习惯一致）。

#### 4.1.2 核心流程

```text
句子字符串
  → 人工字典编码为整数 ID 张量（形状 (8,)）
  → nn.Embedding(10, 16) 查表
  → embedded_sentence，形状 (8, 16) = (词数 T, 嵌入维度 d)
  → .detach() 脱离计算图（本节只做数值演示，不训练）
```

#### 4.1.3 源码精读

句子编码为整数 ID，注释里就是「词 → ID」的小字典（can→0、you→7、help→1、me→2、to→5、translate→6、this→4、sentence→3）：

[ch16/ch16-part1-self-attention.py:L90-L99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L90-L99)
—— 把 8 个词的句子写成整数张量；行内注释给出了每个 ID 对应的单词，等价于一张迷你词表。

[ch16/ch16-part1-self-attention.py:L109-L112](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L109-L112)
—— 固定随机种子后创建 `Embedding(10, 16)` 并查表，`detach()` 把结果从自动微分图上摘下来：本节只是演示注意力计算，嵌入不参与训练。notebook 中记录的输出形状为 `torch.Size([8, 16])`。

#### 4.1.4 代码实践

**实践目标**：亲手建立 6 词版本的数据，为后续模块做准备。

**操作步骤**（示例代码，基于仓库写法裁剪）：

```python
import torch

torch.manual_seed(123)
sentence = torch.tensor([0, 7, 1, 2, 5, 6])  # "can you help me to translate"
embed = torch.nn.Embedding(10, 16)
embedded_sentence = embed(sentence).detach()
print(sentence.shape, embedded_sentence.shape)
```

**需要观察的现象**：输出 `(6,)` 与 `(6, 16)`。

**预期结果**：6 个词得到 6 个 16 维向量。若把 ID 改成 10 会触发索引越界错误——词表大小 10 意味着合法 ID 是 0~9。具体数值随 `torch` 版本可能略有差异，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`Embedding(10, 16)` 的两个参数各控制什么？

**答案**：第一维 10 是词表大小（合法 ID 为 0~9），第二维 16 是每个词的嵌入维度 \(d\)。查表输出形状是 `(序列长度, 16)`。

**练习 2**：这里为什么调用 `.detach()`？

**答案**：嵌入表本身是可训练参数，查表会建立计算图。本节只演示注意力的前向数值、不训练模型，`detach()` 切断梯度追踪，既省内存也明确表达「此处不做反向传播」的意图。

### 4.2 未参数化自注意力：打分 ω → 归一化 α → 上下文向量 z

#### 4.2.1 概念说明

自注意力的朴素想法：**让序列里的每个词直接和所有词（包括自己）算相似度，再按相似度把大家的向量加权平均，得到该词的「上下文向量」**。上下文向量 \(\boldsymbol{z}^{(i)}\) 是词 \(i\) 融合了全句信息后的新表示。

在这一朴素版本里，**每个词的向量 \(\boldsymbol{x}^{(i)}\) 同时扮演三个角色**：

- 作为**查询（query）**：拿自己去和别人比；
- 作为**键（key）**：被别人拿来比；
- 作为**值（value）**：按相似度权重被取出、汇总进上下文。

这就像在图书馆检索：查询是你写下的检索词，键是每本书的索引标签，值是书的内容。检索词和标签匹配度越高，这本书的内容被引用得越多。4.3 节将把这三个角色拆成三组不同的投影；本节先让同一个向量身兼三职，看清注意力的骨架。

与 RNN 对比：RNN 把整段历史压缩进一个隐状态向量，序列开头的信息逐层稀释；自注意力对序列中任意位置都是**直接访问**，没有信息瓶颈。代价是计算量随序列长度平方增长（每对词都要算一次内积）。

#### 4.2.2 核心流程

三步公式（沿用仓库记号）：

\[
\omega_{ij} = \boldsymbol{x}^{(i)\top}\boldsymbol{x}^{(j)}
\]

\[
\alpha_{ij} = \frac{\exp(\omega_{ij})}{\sum_{j=1}^{T}\exp(\omega_{ij})} = \operatorname{softmax}\left([\omega_{ij}]_{j=1\ldots T}\right),
\qquad \sum_{j=1}^{T}\alpha_{ij}=1
\]

\[
\boldsymbol{z}^{(i)} = \sum_{j=1}^{T}\alpha_{ij}\,\boldsymbol{x}^{(j)}
\]

1. **打分**：两两做内积，得到 \(T \times T\) 的打分矩阵 \(\omega\)（第 \(i\) 行是词 \(i\) 对所有词的相似度）。内积越大方向越一致，即「越相关」。
2. **归一化**：对 \(\omega\) 的**每一行**做 softmax，得到注意力权重 \(\alpha\)——每一行非负、和为 1，是一个概率分布。
3. **加权求和**：词 \(i\) 的上下文向量 = 全部词向量按 \(\alpha_{i\cdot}\) 加权平均。

教学上仓库先用显式循环实现（帮助理解定义），再展示等价的一行矩阵乘法（工程写法），并用 `torch.allclose` 验证两者一致。

#### 4.2.3 源码精读

打分矩阵的教学版——双重循环逐对做内积：

[ch16/ch16-part1-self-attention.py:L125-L129](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L125-L129)
—— 预分配 `(8, 8)` 空矩阵，双重循环填入 `torch.dot(x_i, x_j)`，这就是 \(\omega_{ij}=\boldsymbol{x}^{(i)\top}\boldsymbol{x}^{(j)}\) 的逐元素实现。

工程版一行搞定：

[ch16/ch16-part1-self-attention.py:L136](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L136)
—— `X.matmul(X.T)` 一次算完所有内积：`(8,16) @ (16,8) → (8,8)`，第 \(i\) 行第 \(j\) 列恰是 \(\boldsymbol{x}^{(i)\top}\boldsymbol{x}^{(j)}\)。[L141](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L141) 用 `torch.allclose` 验证与循环版一致，notebook 记录结果为 `True`。

按行 softmax 归一化：

[ch16/ch16-part1-self-attention.py:L153-L154](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L153-L154)
—— `F.softmax(omega, dim=1)` 沿第 1 维（列方向）归一化，即**每一行**内部变成概率分布。注意 `dim=1` 不是「全矩阵归一化」：每个查询 \(i\) 在所有键 \(j\) 上分配总和为 1 的注意力。

[ch16/ch16-part1-self-attention.py:L161](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L161)
—— 逐行求和验证归一化，notebook 记录输出为 8 个 `1.0000`。（notebook 里该处的文字说明写成「columns sum up to one」，与代码实际语义相反，以代码为准：是**每行**和为 1。）

上下文向量的教学版——对第 2 个词逐个加权累加：

[ch16/ch16-part1-self-attention.py:L173-L179](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L173-L179)
—— `context_vec_2` 从零向量起，对 8 个词逐一累加 `attention_weights[1, j] * x_j`，这正是 \(\boldsymbol{z}^{(2)}=\sum_j \alpha_{2j}\boldsymbol{x}^{(j)}\) 的直译。notebook 记录的结果首元素约为 `-9.40e-01`。

工程版再次回到一次矩阵乘法：

[ch16/ch16-part1-self-attention.py:L186-L190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L186-L190)
—— `attention_weights.matmul(embedded_sentence)` 一次算出全部 8 个上下文向量：`(8,8) @ (8,16) → (8,16)`，第 \(i\) 行即 \(\boldsymbol{z}^{(i)}\)；`allclose` 验证与循环版一致（notebook 记录 `True`）。

**小结一条贯穿全书的模式**：双重循环定义语义、一次 `matmul` 实现计算，这与第 11 章手写 MLP 时「逐样本循环与向量化更新数学等价」是同一个思想。

#### 4.2.4 代码实践

**实践目标**：不借助任何封装，为 6 个词 (d=16) 手写注意力权重计算，得到 6×6 权重矩阵并验证归一化。

**操作步骤**（示例代码）：

```python
import torch
import torch.nn.functional as F

torch.manual_seed(123)
sentence = torch.tensor([0, 7, 1, 2, 5, 6])        # "can you help me to translate"
X = torch.nn.Embedding(10, 16)(sentence).detach()  # (6, 16)

# 第 1 步：打分——先按定义用双重循环
omega = torch.empty(6, 6)
for i, x_i in enumerate(X):
    for j, x_j in enumerate(X):
        omega[i, j] = torch.dot(x_i, x_j)

# 用矩阵乘法验证等价
assert torch.allclose(omega, X.matmul(X.T))

# 第 2 步：逐行 softmax 归一化
attn_weights = F.softmax(omega, dim=1)

# 第 3 步：验证每一行和为 1
row_sums = attn_weights.sum(dim=1)
print(attn_weights)
print(row_sums)
assert torch.allclose(row_sums, torch.ones(6))
```

**需要观察的现象**：`attn_weights` 是 6×6 矩阵，所有元素在 0~1 之间；`row_sums` 为 6 个 1.0。

**预期结果**：归一化验证通过。注意对角线元素通常较大——每个词与自己的内积是向量范数的平方，一般大于与其他词的内积，所以「自己关注自己」在朴素版本里几乎总是占优（这也是后面引入可学习投影要解决的问题之一）。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 softmax 要**每行**归一化，而不是把整个 6×6 矩阵归一化成总和 1？

**答案**：每一行对应一个查询词 \(i\) 对所有词的注意力分配，必须各自构成概率分布，其上下文向量才是合法的加权平均。若全矩阵归一化，每个词分到的权重总和不再为 1，上下文向量的量纲会随序列长度变化，失去「加权平均」的含义。

**练习 2**：验证等价性时为什么用 `torch.allclose(a, b)` 而不是 `a == b`？

**答案**：浮点运算的求和顺序不同（循环累加与 `matmul` 的分块实现）会带来 \(10^{-6}\) 量级的舍入差异，严格相等几乎必然失败；`allclose` 在容差内比较，才是浮点数等价性的正确检验。

**练习 3**：这套计算里序列长度 \(T\) 出现在哪里？计算量随 \(T\) 如何增长？

**答案**：打分矩阵是 \(T \times T\)，注意力权重的存储与计算量随 \(T\) 平方增长——这是自注意力在长文档上的主要开销，也是各类「稀疏/线性注意力」改进的出发点。

### 4.3 缩放点积注意力参数化

#### 4.3.1 概念说明

4.2 的朴素版本有三个问题：

1. **角色混同**：同一个向量既当查询又当键又当值，「提问的能力」与「被取用的内容」没有分工。
2. **不可学习**：打分固定为原始嵌入的内积，网络无法学到「翻译这个任务里，动词应该多关注宾语」这类模式。
3. **数值不稳**：嵌入维度 \(d_k\) 大时，内积的数值幅度随维度增长，softmax 会饱和成近似 one-hot，梯度几乎消失（回忆 u11 的饱和区直觉）。

缩放点积注意力对症下药：

- 引入**三组可学习的投影矩阵** \(U_{query}, U_{key}, U_{value}\)（每个 \(d \times d\)），把每个词向量投影成三个不同角色：

\[
\boldsymbol{q}^{(i)} = U_{query}\,\boldsymbol{x}^{(i)},\qquad
\boldsymbol{k}^{(i)} = U_{key}\,\boldsymbol{x}^{(i)},\qquad
\boldsymbol{v}^{(i)} = U_{value}\,\boldsymbol{x}^{(i)}
\]

查询与键负责「匹配」（只进打分公式），值负责「内容」（只进加权求和）。检索类比至此完全展开：查询是检索词、键是索引标签、值是文档内容。

- 打分除以 \(\sqrt{d_k}\)：

\[
\omega_{ij} = \frac{\boldsymbol{q}^{(i)\top}\boldsymbol{k}^{(j)}}{\sqrt{d_k}},
\qquad
\alpha_{ij} = \operatorname{softmax}_j(\omega_{ij}),
\qquad
\boldsymbol{z}^{(i)} = \sum_j \alpha_{ij}\,\boldsymbol{v}^{(j)}
\]

**为什么是 \(\sqrt{d_k}\)**：设 \(\boldsymbol{q},\boldsymbol{k}\) 的各分量独立、均值 0、方差 1，则内积 \(\boldsymbol{q}^\top\boldsymbol{k}=\sum_{l=1}^{d_k} q_l k_l\) 的均值是 0、**方差是 \(d_k\)**（标准差 \(\sqrt{d_k}\)）。\(d_k=16\) 时分数天然有约 4 倍的摆动幅度，\(d_k=512\) 时就是 22 倍——softmax 输入被拉开几十，输出就饱和成 one-hot，梯度趋零。除以 \(\sqrt{d_k}\) 恰好把方差拉回 1，让 softmax 工作在梯度健康的区间。除以 \(d_k\) 会矫枉过正（方差变成 \(1/d_k\)），把分数差异压得过平，注意力失去分辨力。

#### 4.3.2 核心流程

以「计算第 2 个词的上下文向量」为例（仓库正是这么做的）：

```text
X (T, d)
  → U_query @ x_2            → query_2                (d,)     查询：我要找什么
  → U_key  @ X.T 再转置       → keys                   (T, d)   键：每个人如何被索引
  → U_value @ X.T 再转置      → values                 (T, d)   值：每个人提供什么内容
  → query_2 · keys.T          → omega_2                (T,)     打分
  → softmax(omega_2 / √d)     → attention_weights_2    (T,)     归一化（注意除以 √d）
  → weights_2 @ values        → context_vector_2       (d,)     加权求和取「值」
```

#### 4.3.3 源码精读

三组随机投影矩阵（真实场景中它们由训练学得）：

[ch16/ch16-part1-self-attention.py:L201-L206](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L201-L206)
—— 取 \(d=16\)，创建三个 `(16, 16)` 的 `U_query / U_key / U_value`。注意三者相互独立：查询、键、值各有各的变换，这就是「角色分工」的参数化。

先对第 2 个词算查询、键、值：

[ch16/ch16-part1-self-attention.py:L211-L218](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L211-L218)
—— `query_2 = U_query.matmul(x_2)` 等三行：同一个 \(\boldsymbol{x}^{(2)}\) 被投成三个不同角色的向量。

再对**整个序列**算键和值，并验证与逐词版一致：

[ch16/ch16-part1-self-attention.py:L223-L230](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L223-L230)
—— 形状记账的关键一步：`U_key.matmul(embedded_sentence.T)` 是 `(16,16) @ (16,8) → (16,8)`，再 `.T` 得 `(8, 16)` 的 `keys`，**行对齐词、与 `embedded_sentence` 同形**。`allclose` 确认 `keys[1]` 与逐词算的 `key_2` 一致（notebook 记录 `True`）。

用查询对所有键打分：

[ch16/ch16-part1-self-attention.py:L235-L242](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L235-L242)
—— 先 `query_2.dot(keys[2])` 算出单个分数（notebook 记录约 `14.37`），再用 `query_2.matmul(keys.T)` 一次得到对全部 8 个词的分数向量 `omega_2`，notebook 记录为 `[-25.16, 9.36, 14.37, 32.15, 53.90, 46.66, -1.21, -32.94]`——跨度近 90，可见不缩放的话 softmax 输入会多么极端。

**本讲的灵魂一行**——缩放 + softmax：

[ch16/ch16-part1-self-attention.py:L247](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L247)
—— `F.softmax(omega_2 / d**0.5, dim=0)`：除以 \(\sqrt{16}=4\) 后再 softmax。这里 `dim=0` 是因为 `omega_2` 是一维向量（与 4.2 中二维矩阵用 `dim=1` 的情形对照，语义相同：沿「键」的方向归一化）。notebook 记录的权重为 `[2.2e-09, 1.2e-05, 4.4e-05, 3.7e-03, 0.856, 0.140, 8.9e-07, 3.2e-10]`——即便缩放之后，"you" 仍把 85.6% 的注意力给了 "to"、14.0% 给了 "translate"。由于投影矩阵是未训练的随机数，这个分布没有语言学含义，只演示数值行为。

[ch16/ch16-part1-self-attention.py:L253-L257](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L253-L257)
—— 被注释掉的循环版加权求和，与 4.2 的写法同构，留给读者对照。

取「值」做加权平均，得到最终的上下文向量：

[ch16/ch16-part1-self-attention.py:L262](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L262)
—— `attention_weights_2.matmul(values)`：`(8,) @ (8,16) → (16,)`。与 4.2 的关键区别：**加权平均的对象从原始嵌入 \(\boldsymbol{x}\) 换成了投影后的值 \(\boldsymbol{v}\)**。

#### 4.3.4 代码实践

**实践目标**：在 6 词句子上实现带 \(U\) 投影的打分，对比「缩放前 / 缩放后」两版注意力权重的分布差异，亲手验证 \(\sqrt{d_k}\) 的作用。

**操作步骤**（示例代码）：

```python
import torch
import torch.nn.functional as F

torch.manual_seed(123)
sentence = torch.tensor([0, 7, 1, 2, 5, 6])
X = torch.nn.Embedding(10, 16)(sentence).detach()   # (6, 16)

d = X.shape[1]
U_query, U_key = torch.rand(d, d), torch.rand(d, d)

# Q = X @ U_query.T、K = X @ U_key.T（每行是 U·x），scores = Q @ K.T
scores = X.matmul(U_query.T).matmul(U_key).matmul(X.T)  # 所有查询对所有键打分 (6, 6)

w_raw  = F.softmax(scores, dim=1)          # 不缩放
w_scaled = F.softmax(scores / d**0.5, dim=1)  # 除以 sqrt(16) = 4

print("分数标准差:", scores.std().item(), " 理论参考 sqrt(d)=", d**0.5)
print("不缩放   每行最大权重:", w_raw.max(dim=1).values)
print("缩放后   每行最大权重:", w_scaled.max(dim=1).values)
print("两版每行之差的总和:", (w_raw - w_scaled).abs().sum(dim=1))
```

**需要观察的现象**：

1. `scores` 的标准差与 \(\sqrt{d}=4\) 在同一量级（均匀随机数不是零均值，只会更大）；
2. `w_raw` 每行最大权重非常接近 1（近似 one-hot，即「赢家通吃」）；
3. `w_scaled` 每行最大权重明显下降，分布更平缓。

**预期结果**：缩放把分数除以 4，softmax 从饱和区回到正常工作区，权重分布从「尖峰」变「平缓」——训练时梯度才能顺畅回传。具体数值待本地验证（可在 `manual_seed(123)` 下与同学比对）。

#### 4.3.5 小练习与答案

**练习 1**：为什么除以 \(\sqrt{d_k}\) 而不是 \(d_k\)？

**答案**：内积的方差是 \(d_k\)，除以 \(\sqrt{d_k}\) 恰好把方差恢复为 1；若除以 \(d_k\)，方差变成 \(1/d_k\)，分数被过度压平，softmax 输出接近均匀分布，注意力丧失分辨不同词的能力（什么都关注等于什么都不关注）。

**练习 2**：本小节 softmax 用 `dim=0`，4.2 用 `dim=1`，矛盾吗？

**答案**：不矛盾。`omega_2` 是一维向量（一个查询对所有键），唯一的维度就是 0；4.2 的 `omega` 是 `(T, T)` 矩阵，沿 `dim=1` 对每行归一化。两者的语义相同：都是沿「键」的方向做归一化。写代码时先想清楚张量是几维、键排在哪个轴上。

**练习 3**：训练完成后，「哪个词关注哪个词」由什么决定？

**答案**：由学到的 \(U_{query}\) 与 \(U_{key}\) 决定——它们定义了「什么算相关」。这正是参数化相对于朴素内积的核心收益：注意力模式从固定规则变成可学习函数。

### 4.4 多头注意力模块拼接

#### 4.4.1 概念说明

一组 \(U_{query}/U_{key}/U_{value}\) 只能在一个「视角」下衡量相关性（比如句法相依）。**多头注意力**用 \(h\) 组相互独立的投影并行做 \(h\) 次注意力，每个头在自己的表示子空间里自由学到不同的关注模式（一个头盯句法、一个头盯主题、一个头盯邻近词……），最后把各头输出**拼接**起来，过一个线性层投影回 \(d\) 维，让模型综合多个视角。

一个如实的仓库说明：原论文《Attention Is All You Need》中每个头的维度是 \(d/h\)（总参数量与单头持平）；本书的教学实现**没有做降维**，每个头都在完整 16 维上投影，因此参数量是单头的 \(h\) 倍——这是为讲清结构而做的简化。

另外，仓库代码**省略了「逐头计算注意力」这一步**：`multihead_z_2` 直接用一个随机张量占位（见源码精读），只演示「拼接 + 线性投影」的收尾动作。阅读时要清楚这一段是不完整的。

#### 4.4.2 核心流程

```text
X (T, d)
  → repeat 堆叠 8 份           → stacked_inputs   (h, d, T)
  → bmm: 8 个头各自投影        → multihead_keys   (h, d, T) → permute → (h, T, d)
  → （书中省略）每个头按 4.3 计算 z → z^(k), k = 1..h，每个 (T, d)
  → 把第 i 个词在 8 个头的输出拼起 → (h*d,) = (128,)
  → Linear(128, 16)            → 该词最终上下文向量 (16,)
```

#### 4.4.3 源码精读

8 组独立的投影参数，一次打包成三维张量：

[ch16/ch16-part1-self-attention.py:L276-L287](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L276-L287)
—— 先定义 `one_U_query` 作为单头对照（提醒你多头就是把它复制成 \(h\) 份），随后 `multihead_U_query/key/value` 的形状是 `(8, 16, 16)`：8 个头、每头一个 `(16,16)` 投影，全部存在一个张量里。

利用广播对单个词同时算 8 个头的查询：

[ch16/ch16-part1-self-attention.py:L292-L299](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L292-L299)
—— `multihead_U_query.matmul(x_2)`：`(8,16,16) @ (16,)`，`matmul` 把三维张量当作 8 个独立矩阵分别与向量相乘，输出 `(8, 16)`——第 \(k\) 行是第 \(k\) 个头为 "you" 算出的查询。这正是 u12-l1 讲过的广播矩阵乘。

为整句做 8 头投影的形状三部曲：

[ch16/ch16-part1-self-attention.py:L309-L310](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L309-L310)
—— `embedded_sentence.T.repeat(8, 1, 1)`：先把 `(8,16)` 转置成 `(16,8)`（列变词），再沿第 0 维复制 8 份得 `(8, 16, 8)`，给每个头备一份输入。

[ch16/ch16-part1-self-attention.py:L315-L322](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L315-L322)
—— `torch.bmm` 做批量矩阵乘：`(8,16,16) @ (8,16,8) → (8,16,8)`，8 个头互不干扰地各自投影；`permute(0, 2, 1)` 把轴重排成 `(8, 8, 16) = (头, 词, 特征)`，与前面所有「行对齐词」的约定接轨。[L327](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L327) 索引 `multihead_keys[2, 1]`（第 3 个头的第 2 个键），notebook 记录其数值与 L304 逐词算出的 `multihead_key_2[2]` 完全一致。

值向量同样处理，随后是诚实的占位与收尾：

[ch16/ch16-part1-self-attention.py:L332-L333](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L332-L333)
—— 值投影与键同理（这里用 `torch.matmul`，对三维张量它就是批量矩阵乘，与 `bmm` 等价）。

[ch16/ch16-part1-self-attention.py:L338](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L338)
—— **注意**：`multihead_z_2 = torch.rand(8, 16)` 是随机占位符，书中省略了「每个头跑一遍 4.3 的缩放点积注意力」这一步，其结果应是 `(8, 16)`：8 个头各给 "you" 一个 16 维上下文向量。

[ch16/ch16-part1-self-attention.py:L347-L349](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L347-L349)
—— 拼接 + 投影：`flatten()` 把 `(8, 16)` 摊平成 `(128,)`（即 \(h \times d\)），`Linear(8*16, 16)` 投回 16 维，得到该词最终上下文向量，形状 `torch.Size([16])`。这个线性层让各头信息互相融合，而不是简单堆放。

#### 4.4.4 代码实践

**实践目标**：把多头形状三部曲在 4 头、6 词的小例子上独立跑通，盯住每一步形状。

**操作步骤**（示例代码）：

```python
import torch

torch.manual_seed(123)
T, d, h = 6, 16, 4
X = torch.nn.Embedding(10, d)(torch.tensor([0, 7, 1, 2, 5, 6])).detach()  # (6, 16)

multihead_U_key = torch.rand(h, d, d)          # 4 个头各自的键投影
stacked = X.T.repeat(h, 1, 1)                  # (4, 16, 6)
keys = torch.bmm(multihead_U_key, stacked)     # (4, 16, 6)
keys = keys.permute(0, 2, 1)                   # (4, 6, 16) = (头, 词, 特征)
print(stacked.shape, keys.shape)

# 抽查：第 2 个头为第 2 个词算出的键 == 逐词算的版本
key_2_head2 = multihead_U_key[1].matmul(X[1])
assert torch.allclose(keys[1, 1], key_2_head2)
```

**需要观察的现象**：形状链 `(6,16) → (4,16,6) → (4,16,6) → (4,6,16)`，断言通过。

**预期结果**：`permute` 后 `keys[头, 词]` 与「取出该头的投影矩阵、单独乘该词」逐一致——`bmm` 只是把这个过程批量化的手段。具体数值待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`repeat`、`bmm`、`permute` 各自在流程里解决什么问题？

**答案**：`repeat` 给每个头复制一份输入（形状对齐成可批量的 `(h, d, T)`）；`bmm` 让 \(h\) 个头**互不干扰**地各自做矩阵乘（普通 `matmul` 对形状合适的三维张量效果等价）；`permute` 只重排轴、不改数据，把结果从 `(头, 特征, 词)` 调整回 `(头, 词, 特征)` 的约定布局。

**练习 2**：本书实现的多头注意力参数量是单头的几倍？原论文呢？

**答案**：本书每头用完整 `(16,16)` 投影、共 8 头，三组投影参数量是单头的 8 倍（外加最后的 `Linear(128,16)`）；原论文每头维度 \(d/h=2\)，8 头合计参数量与单头 `(d,d)` 持平，只是把同一预算切给多个视角。

**练习 3**：如果去掉最后的 `Linear(h*d, d)`、直接把拼接向量当输出，会怎样？

**答案**：输出维度变成 \(h \cdot d\)（此处 128），后续层输入维度不再统一；更重要的是各头结果只是被「并排放着」，缺少一个可学习的融合变换。线性投影让模型学习如何组合各头信息，是多头结构不可省的一环。

### 4.5 因果掩码与位置编码：补上自注意力的两个缺口

> 本节在仓库中**没有可运行代码**：[L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L352) 与 [L358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch16/ch16-part1-self-attention.py#L358) 只有两节标题，notebook 中对应位置只有插图 `16_08.png`（因果掩码）与 `16_09.png`（位置编码）。以下讲解配合**示例代码**演示，请注意区分。

#### 4.5.1 概念说明

**缺口一：自注意力「偷看未来」。** 4.2/4.3 的计算里，每个词对所有词打分——包括它**右边**的词。但语言模型的任务是「看过前 \(t\) 个词预测第 \(t+1\) 个词」（回忆 u15-l3 的自回归生成：生成时未来词根本还不存在）。若训练时允许第 \(t\) 个词关注第 \(t+1\) 个词，模型等于拿着答案做题，学不到真正的预测能力。**因果掩码**的解法：在 softmax **之前**，把打分矩阵上三角（\(j > i\) 的位置，即「未来的键」）设为 \(-\infty\)，softmax 之后这些位置的权重精确为 0，上下文向量只由位置 \(\le i\) 的词聚合。

**缺口二：自注意力「看不见词序」。** 把句子的词任意打乱，4.2 的打分矩阵只是同步交换了行和列，算出的上下文向量集合完全不变——自注意力是**置换不变**的，天然没有顺序概念（这与 RNN 形成尖锐对比：RNN 的递推顺序就是词序）。但 "dog bites man" 与 "man bites dog" 对注意力而言毫无区别。**位置编码**的解法：给每个位置 \(pos\) 构造一个与嵌入同维的向量，**逐元素加到词嵌入上**后再送入注意力，让「第几个词」成为向量的一部分。原论文用固定正弦/余弦编码：

\[
PE_{(pos,\,2i)} = \sin\!\left(\frac{pos}{10000^{2i/d}}\right),\qquad
PE_{(pos,\,2i+1)} = \cos\!\left(\frac{pos}{10000^{2i/d}}\right)
\]

不同维度 \(i\) 对应不同波长的正弦波，高维变化快、低维变化慢，像一组「位置刻度尺」。GPT-2 等后续模型改用可学习的位置嵌入，思想相同：**把位置信息注入向量**。

#### 4.5.2 核心流程

带因果掩码的缩放点积注意力（对 4.3 流程只加一步）：

```text
scores = Q @ K.T / √d                 # (T, T)
mask = torch.tril(torch.ones(T, T))   # 下三角为 1（含对角线），上三角为 0
scores = scores.masked_fill(mask == 0, float('-inf'))
attn = softmax(scores, dim=1)         # 上三角 exp(-inf)=0，自动权重为 0
out = attn @ V
```

位置编码的注入点（在进入注意力之前）：

```text
X = Embedding(ids)               # (T, d)  只含「是什么词」
X = X + PE                       # (T, d)  逐元素加上「在第几位」
→ 送入注意力
```

#### 4.5.3 源码精读

本节没有源码可读，只有两行标题作为书内对应章节的路标：

[ch16/ch16-part1-self-attention.py:L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L352)
—— 「Learning a language model: decoder and masked multi-head attention」一节的标题行，之下无代码；书中该节靠插图 `figures/16_08.png` 讲解解码器与因果掩码。

[ch16/ch16-part1-self-attention.py:L358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L358)
—— 「Implementation details: positional encodings and layer normalization」一节的标题行，同样无代码；书中靠插图 `figures/16_09.png` 讲解位置编码（以及 Transformer 块中的层归一化，本讲不展开）。

下面的**示例代码**（不属于仓库）把两个概念补成可运行的演示：

```python
import torch
import torch.nn.functional as F

torch.manual_seed(123)
T, d = 6, 16
X = torch.randn(T, d)

# --- 因果掩码 ---
scores = X.matmul(X.T) / d**0.5                       # (6, 6)
mask = torch.tril(torch.ones(T, T))                   # 下三角（含对角线）为 1
causal_scores = scores.masked_fill(mask == 0, float('-inf'))
attn = F.softmax(causal_scores, dim=1)
print("上三角权重是否全为 0:", bool((attn.triu(1) == 0).all()))
print("每行和:", attn.sum(dim=1))

# --- 位置编码 ---
pos = torch.arange(T).unsqueeze(1)                    # (T, 1)
i = torch.arange(0, d, 2).float()                     # 偶数维索引
denom = torch.pow(10000, i / d)
PE = torch.zeros(T, d)
PE[:, 0::2] = torch.sin(pos / denom)
PE[:, 1::2] = torch.cos(pos / denom)
X_with_pos = X + PE
```

#### 4.5.4 代码实践

**实践目标**：验证因果掩码的两个数学性质：上三角权重精确为 0；每行仍是合法概率分布。

**操作步骤**：运行上面的示例代码（可单独保存为小脚本执行）。

**需要观察的现象**：`attn.triu(1)`（上三角去掉对角线）全部元素为 0；`attn.sum(dim=1)` 为 6 个 1.0；第 0 行（第一个词）只关注自己。

**预期结果**：掩码版注意力的第 \(i\) 行在 \(j > i\) 处为 0、行和为 1。因为对角线永远保留（`tril` 含对角线），不存在整行全掩的死行。具体数值待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：为什么掩码必须在 softmax **之前**做，而不是把 softmax 之后的上三角权重直接置 0？

**答案**：先 softmax 再置零，每行剩下的权重总和小于 1，不再是概率分布，上下文向量的量纲被系统性缩小；先置 \(-\infty\) 再 softmax，分母自动只累计允许的位置，输出仍是每行和为 1 的分布，屏蔽与归一化一次完成。

**练习 2**：位置编码应该加在哪里？

**答案**：加在嵌入之后、进入任何注意力层之前（`X = Embedding(ids) + PE`），逐元素相加。它不能加在注意力输出上——那样每个位置的顺序信号已经无法影响「谁关注谁」的打分过程。

**练习 3**：为什么第 0 个词（第一个词）的掩码行没有出现全 \(-\infty\) 导致的 NaN？

**答案**：因果掩码用下三角且**含对角线**，每个词至少能看到自己，任何一行都至少有一个有限分数，softmax 分母恒正。若构造出全 \(-\infty\) 的行（比如把对角线也掩掉），softmax 会得到 NaN。

## 5. 综合实践

把本讲四个模块串成一个完整可运行的小项目：**不使用 `nn.MultiheadAttention`，为 6 个词的句子实现一个带 \(\sqrt{d}\) 缩放、可选因果掩码的缩放点积注意力函数，并组装一个多头版本**。

```python
import torch
import torch.nn.functional as F

torch.manual_seed(123)
T, d, h = 6, 16, 4
ids = torch.tensor([0, 7, 1, 2, 5, 6])               # "can you help me to translate"
X = torch.nn.Embedding(10, d)(ids).detach()          # (6, 16)

def scaled_dot_product_attention(Q, K, V, mask=None):
    """Q,K,V: (T, d)；mask: (T, T)，1 表示允许关注。返回 (输出, 注意力权重)。"""
    scores = Q.matmul(K.T) / d**0.5                  # 模块三：打分 + sqrt(d) 缩放
    if mask is not None:
        scores = scores.masked_fill(mask == 0, float('-inf'))  # 模块五：因果掩码
    attn = F.softmax(scores, dim=1)                  # 模块二：按行归一化
    return attn.matmul(V), attn

def multihead_attention(X, h, causal=False):
    """简化多头：每头独立投影到 d 维（教学版，未按原论文降维到 d/h）。"""
    outs = []
    for k in range(h):
        Uq, Uk, Uv = (torch.rand(d, d) for _ in range(3))
        Q, K, V = Uq.matmul(X.T).T, Uk.matmul(X.T).T, Uv.matmul(X.T).T  # 模块三
        mask = torch.tril(torch.ones(T, T)) if causal else None          # 模块五
        out_k, _ = scaled_dot_product_attention(Q, K, V, mask)           # 模块二/三
        outs.append(out_k)                                               # (T, d) 每头
    Z = torch.cat(outs, dim=1)            # (T, h*d) 模块四：沿特征维拼接
    return torch.nn.Linear(h * d, d)(Z)   # 模块四：投影回 d 维

# --- 验证清单 ---
out, attn = scaled_dot_product_attention(X, X, X)
assert torch.allclose(attn.sum(dim=1), torch.ones(T))       # ① 每行和为 1
w_raw, _ = F.softmax(X.matmul(X.T), dim=1), None            # 不缩放对照
print("缩放前后最大权重（第 1 行）:",
      w_raw[1].max().item(), "vs", attn[1].max().item())    # ② 缩放更平缓

causal_mask = torch.tril(torch.ones(T, T))
_, attn_c = scaled_dot_product_attention(X, X, X, causal_mask)
assert bool((attn_c.triu(1) == 0).all())                    # ③ 上三角权重为 0

print("多头输出形状:", multihead_attention(X, h).shape)      # ④ (6, 16)
print("带因果掩码的多头输出形状:", multihead_attention(X, h, causal=True).shape)
```

逐项检查（对应代码中①~④）：

1. 权重每行和为 1（模块二的归一化）；
2. 同一查询下，缩放版的最大权重低于不缩放版，分布更平缓（模块三的 \(\sqrt{d}\)）；
3. 掩码版上三角权重精确为 0，行和仍为 1（模块五）；
4. 多头输出形状 `(6, 16)`，与输入同形——拼接后投影回原维度（模块四）。

进一步实验（可选）：把 `h` 从 4 改成 1 与 8，观察输出是否变化并解释（随机投影下每次运行不同属正常；固定种子后，头数改变会改变参数集合）；把句子换成乱序（如 `[6, 2, 0, 5, 1, 7]`），验证**不加位置编码时**输出上下文向量集合与原句完全相同——亲手证实置换不变性，以及位置编码的必要性。具体数值待本地验证。

## 6. 本讲小结

- 自注意力三步曲：**打分（内积）→ 归一化（逐行 softmax）→ 加权求和**，全部是矩阵乘法，无循环、可全并行；任意两词的交互路径长度 \(O(1)\)，直接回应 RNN 的长程依赖与梯度消失（u15-l1）。
- 朴素版本里词向量身兼查询、键、值三职；引入 \(U_{query}/U_{key}/U_{value}\) 三组**可学习投影**完成角色分工，「关注什么」从固定规则变成可学习函数。
- **\(\sqrt{d_k}\) 缩放**是数值稳定的保险丝：内积方差为 \(d_k\)，不缩放则 softmax 饱和成 one-hot、梯度趋零；除以 \(\sqrt{d_k}\) 恰好把方差拉回 1。
- **多头注意力** = \(h\) 组独立投影在各自子空间并行注意力 + 拼接 + 线性投影回 \(d\)；仓库实现用 `repeat`/`bmm`/`permute` 完成批量形状记账，且省略了逐头计算（`multihead_z_2` 为随机占位）、未按原论文做 \(d/h\) 降维。
- **因果掩码**（softmax 前置 \(-\infty\)）堵住「偷看未来」，支撑语言模型的自回归训练；**位置编码**（嵌入上逐元素加位置向量）补回自注意力天生缺失的词序信息——仓库中这两节只有标题与插图，本讲用示例代码补齐了演示。
- 仓库事实清单：Part 1 的可运行代码止于多头拼接的 `Linear(8*16, 16)`；`omega_2`、`attention_weights_2` 等 notebook 记录输出均可在固定种子 `123` 下复现。

## 7. 下一步学习建议

1. **下一讲 u16-l2（GPT-2 微调做情感分类）**：本讲手写的模块在真实工程里由 `transformers` 库的预训练 GPT-2 封装好，下一讲聚焦「加载预训练权重、冻结基础层、替换分类头、AdamW 线性衰减调度」等微调实务。
2. **对照官方实现**：阅读 PyTorch 文档中 `torch.nn.MultiheadAttention` 的参数表（`embed_dim`、`num_heads`、`mask`），逐项对应你在综合实践里手写的部件，检验理解。
3. **回读原文**：Vaswani 等人《Attention Is All You Need》（2017）第 3.2 节正是本讲 4.3/4.4 的公式来源，读原文时重点体会「scaled dot-product」与「multi-head」两小节的动机论证。
4. **顺藤摸瓜**：同一作者的 `rasbt/LLMs-from-scratch` 仓库把本讲的思路一路扩展成完整的 GPT 实现（含因果掩码与可学习位置嵌入的完整代码），可作为本讲两个「缺口」主题的进阶读物。
5. 若想巩固形状记账能力，回到 [ch16/ch16-part1-self-attention.py:L309-L333](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L309-L333)，在不运行代码的前提下手推每个张量的形状，再上机核对。
