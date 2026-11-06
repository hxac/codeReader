# u16-l1 自注意力与多头注意力从零实现

## 1. 本讲目标

本讲是 Transformer 单元的第一讲。学完后你应该能够：

1. **读懂未参数化的自注意力**：给定 8 个词的嵌入矩阵，理解注意力得分 \(\omega_{ij}\)、注意力权重 \(\alpha_{ij}\) 与上下文向量 \(z^{(i)}\) 三步计算，并解释 `F.softmax(omega, dim=1)` 里 `dim=1` 的含义。
2. **理解缩放点积注意力的两个改造**：用 \(U_{query}\)、\(U_{key}\)、\(U_{value}\) 三个矩阵把「自己和自己算内积」升级为「三路投影」，以及为什么除以 \(\sqrt{d_k}\) 能防止 softmax 饱和、稳定梯度。
3. **理解多头注意力的并行结构**：`(h, d, d)` 的权重堆叠、`torch.bmm` 批量矩阵乘、`permute` 调整轴顺序、8 个头的输出拼接后经 `nn.Linear(8*16, 16)` 投影回原维度。
4. **说清因果掩码与位置编码为什么必要**：语言模型为什么不许第 \(i\) 个词看第 \(i\) 之后的内容；自注意力为什么是「集合运算」而丢失了词序。
5. **不借助任何封装手写一遍注意力**：用双层循环和手写 softmax 算出 6×6 权重矩阵，验证每行和为 1，并对比缩放前后的权重分布差异。

本讲所有结论锚定在仓库真实代码上：[ch16/ch16-part1-self-attention.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py) 用不到 100 行的 PyTorch 代码，从一张 8×16 的词嵌入矩阵出发，一步步搭出多头注意力的全部零件。

## 2. 前置知识

### 2.1 RNN 的瓶颈：注意力机制的出发点

u15 系列讲义中，RNN 把整条序列压缩进一个固定维度的隐状态，解码时所有信息都要从这个「瓶颈」里挤过去；序列越长，早期信息被稀释得越厉害，而且必须逐时间步串行计算。ch16 开篇的「给 RNN 加注意力」一节（仅插图，无代码）讲的正是历史方案：让解码器在每一步**回头看**编码器全部时间步的输出，按相关性加权取用——这就是「注意力」名字的由来。自注意力则更彻底：干脆去掉 RNN，让序列里的每个词直接和所有词交互。

### 2.2 词嵌入：从整数到向量

u15-l2 的 IMDb 管线里已经用过 `nn.Embedding`。它是一张可查找的表：`nn.Embedding(10, 16)` 有 10 行（词表里 10 个词 ID）、每行 16 维，前向传播就是**按行查表**——输入整数 ID，输出对应的行向量。它和 u11-l1 手写的 one-hot 编码 + 权重乘法在数学上等价，但实现上是 O(1) 查表。

### 2.3 你需要带走的旧工具

- **softmax**（u12-l3、u3-l1）：\(\mathrm{softmax}(s)_j = \frac{e^{s_j}}{\sum_k e^{s_k}}\)，把一组实数变成和为 1 的概率分布。
- **矩阵乘法与维度相消**（u12-l1）：`A @ B` 要求 A 的列数等于 B 的行数；`X @ X.T` 的 \((i,j)\) 元素恰好是第 \(i\) 行与第 \(j\) 行的内积；三维张量下 `torch.bmm` 与 `torch.matmul` 等价（逐个批次做二维矩阵乘）。
- **置换等变**：一个运算若对输入同时重排后、输出只是跟着同样重排，则它本身不包含顺序信息。本讲 4.5 节会用它论证位置编码的必要性。

### 2.4 查询-键-值类比

注意力机制借用了数据库检索的词汇：

- **查询 query**：当前词「我想找什么」；
- **键 key**：每个词「我可以被什么样的查询匹配」；
- **值 value**：每个词「匹配上之后我实际提供的内容」。

一个词的查询和所有词的键做点积得到**相关性得分**，得分经 softmax 归一化成**注意力权重**，再用权重对值加权求和，得到融合了上下文信息的**上下文向量**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch16/ch16-part1-self-attention.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py) | 本讲主源码：词嵌入构建 → 未参数化自注意力 → 缩放点积注意力 → 多头注意力，约 90 行核心代码。开头按全书惯例用 `check_packages` 检查 `torch>=1.9.0` |
| [ch16/ch16-part1-self-attention.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.ipynb) | notebook 版：额外携带已运行的真实输出（本讲引用的 `omega_2` 数值、softmax 权重均来自这里）和章节插图 |
| [ch16/figures/16_04.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_04.png) | 未参数化自注意力示意图（notebook 中加载） |
| [ch16/figures/16_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_05.png) | 缩放点积注意力（查询-键-值三路投影）示意图 |
| [ch16/figures/16_07.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_07.png) | 多头注意力拼接与投影示意图 |
| [ch16/figures/16_08.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_08.png) | 因果掩码多头注意力（解码器）示意图——仓库中该节**只有插图、没有代码** |
| [ch16/figures/16_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_09.png) | 位置编码与层归一化示意图——同上，仅插图 |

阅读提示（承接 u1-l1 的双轨工作流）：读代码和引用行号用 `.py`；看插图和运行输出用 `.ipynb`。

## 4. 核心概念与源码讲解

### 4.1 单词嵌入表构建：从整数句子到 8×16 矩阵

#### 4.1.1 概念说明

一切的起点是一条长度 \(T=8\) 的句子 `"Can you help me to translate this sentence"`。自注意力不直接吃整数——它需要每个词是一个实数向量。这一步做两件事：

1. 用字典把词映射成整数 ID（u15-l2 已做过，这里直接给出现成结果）；
2. 用 `nn.Embedding(10, 16)` 查表，把 8 个 ID 变成 8 个 16 维向量，拼成形状 \((8, 16)\) 的矩阵 \(X\)。

这里的 10 是**词表大小**（表格有 10 行，本句只用到其中 8 个不同 ID），16 是**嵌入维度** \(d\)。后续所有计算都围绕这张 \((T, d) = (8, 16)\) 的矩阵展开。

#### 4.1.2 核心流程

```text
句子（8 个词）
  → 字典编码: [0, 7, 1, 2, 5, 6, 4, 3]        # 每个词一个整数 ID
  → nn.Embedding(10, 16) 按行查表
  → embedded_sentence: 形状 (8, 16) 的嵌入矩阵 X
     第 i 行 = 第 i 个词的 16 维向量 x^(i)
```

#### 4.1.3 源码精读

句子先被编码成整数张量，注释里标出了每个 ID 对应的词（[ch16/ch16-part1-self-attention.py:90-99](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L90-L99)）：`sentence = torch.tensor([0, 7, 1, 2, 5, 6, 4, 3])`，对应 can/you/help/me/to/translate/this/sentence。

接着固定随机种子、建嵌入表并查表（[ch16/ch16-part1-self-attention.py:109-112](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L109-L112)）：

```python
torch.manual_seed(123)
embed = torch.nn.Embedding(10, 16)
embedded_sentence = embed(sentence).detach()
```

三个细节：`manual_seed` 让随机初始化的表可复现；`detach()` 把结果从计算图上切下来（本讲是纯演示、不训练，承接 u13-l1 对 autograd 图的讲解）；notebook 输出确认 `embedded_sentence.shape` 为 `torch.Size([8, 16])`。

#### 4.1.4 代码实践

1. **实践目标**：验证 `nn.Embedding` 的前向就是按行查表。
2. **操作步骤**：

   ```python
   import torch
   torch.manual_seed(123)
   embed = torch.nn.Embedding(10, 16)
   sentence = torch.tensor([0, 7, 1, 2, 5, 6, 4, 3])
   X = embed(sentence).detach()
   print(embed.weight.shape)                    # 表本身的形状
   print(torch.equal(X, embed.weight[sentence]))  # 查表等价性
   ```

3. **需要观察的现象**：`embed.weight` 的形状是 `(10, 16)`——表比句子大；第二个打印是布尔值。
4. **预期结果**：`torch.Size([10, 16])` 与 `True`。由于查表只是取行、没有任何算术运算，连 `torch.equal`（比 `allclose` 更严格）也必为 `True`；换一个 `manual_seed` 数值会变，但相等关系不变。

#### 4.1.5 小练习与答案

1. **问**：句子只有 8 个词，为什么嵌入表建 10 行？
   **答**：10 是词表大小，即字典里不同 ID 的总数；序列长度 8 只决定查多少行。两者独立。
2. **问**：`embed(sentence)` 和 `embed.weight[sentence]` 有什么关系？
   **答**：完全等价。`nn.Embedding` 的前向就是用输入 ID 做行索引，返回表中被引用的那些行。
3. **问**：`.detach()` 去掉行不行？
   **答**：运行结果数值不变，但返回的张量会带 `requires_grad` 之外的计算图引用；本讲后续只做手动演示不训练，`detach()` 让张量保持「纯数据」身份，语义更干净。

### 4.2 未参数化自注意力：attn_scores、attn_weights 与上下文向量

#### 4.2.1 概念说明

最朴素的自注意力不需要任何可学习参数，只用「点积度量词与词的相关性」。对嵌入矩阵 \(X\) 的第 \(i\) 行 \(x^{(i)}\)，定义三步：

注意力得分（两两内积）：

\[\omega_{ij} = x^{(i)\top} x^{(j)}\]

注意力权重（对每个查询 \(i\)，沿键轴做 softmax 归一化）：

\[\alpha_{ij} = \frac{\exp(\omega_{ij})}{\sum_{j=1}^{T}\exp(\omega_{ij})}, \qquad \sum_{j=1}^{T}\alpha_{ij} = 1\]

上下文向量（用权重对词向量加权求和）：

\[z^{(i)} = \sum_{j=1}^{T} \alpha_{ij}\, x^{(j)}\]

直觉：\(z^{(i)}\) 是「第 \(i\) 个词眼里整句话的摘要」——它和谁的内积大，就多吸收谁的信息。注意此版本里词向量**同时扮演**查询、键、值三个角色，这为 4.3 的参数化埋下伏笔。

#### 4.2.2 核心流程

```text
X: (8, 16)
  ① 得分矩阵   omega = X @ X.T            → (8, 8)，(i,j) 元素 = x_i·x_j
  ② 行归一化   attention_weights = softmax(omega, dim=1) → (8, 8)，每行和 = 1
  ③ 加权求和   context_vectors = A @ X    → (8, 16)，第 i 行即 z^(i)
```

仓库特意先用**双层循环**写第①步（教学版），再用**一次矩阵乘**重写（工程版），并用 `torch.allclose` 验证两者一致——这和 u11-l2 「向量化更新与逐权重循环数学等价」是同一个道理。

#### 4.2.3 源码精读

双层循环逐对算点积，结果存进 `torch.empty(8, 8)` 预分配的缓冲（[ch16/ch16-part1-self-attention.py:125-129](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L125-L129)）：

```python
omega = torch.empty(8, 8)
for i, x_i in enumerate(embedded_sentence):
    for j, x_j in enumerate(embedded_sentence):
        omega[i, j] = torch.dot(x_i, x_j)
```

一次矩阵乘等价替换（[ch16/ch16-part1-self-attention.py:136](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L136)）：`omega_mat = embedded_sentence.matmul(embedded_sentence.T)`，随后 [第 141 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L141) 的 `torch.allclose(omega_mat, omega)` 在 notebook 中输出 `True`。

softmax 归一化（[ch16/ch16-part1-self-attention.py:153-154](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L153-L154)）：

```python
attention_weights = F.softmax(omega, dim=1)
```

`dim=1` 是本模块最容易搞错的地方：`omega` 的**行**是查询、**列**是键，`dim=1` 表示沿键轴归一化——每个查询对全部键的权重合成 1。[第 161 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L161) 用 `attention_weights.sum(dim=1)` 验证，notebook 输出 8 个 `1.0000`。

上下文向量同样先循环后矩阵乘：循环版逐项累加（[ch16/ch16-part1-self-attention.py:173-179](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L173-L179)），

```python
x_2 = embedded_sentence[1, :]
context_vec_2 = torch.zeros(x_2.shape)
for j in range(8):
    x_j = embedded_sentence[j, :]
    context_vec_2 += attention_weights[1, j] * x_j
```

矩阵乘版一次算出全部 8 个上下文向量（[ch16/ch16-part1-self-attention.py:186-187](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L186-L187)）：`context_vectors = torch.matmul(attention_weights, embedded_sentence)`，[第 190 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L190) 验证 `context_vectors[1]` 与循环版一致（notebook 输出 `True`）。

#### 4.2.4 代码实践

这是本讲主实践的第一部分：**不借助封装手写 6×6 注意力权重**。

1. **实践目标**：不用 `F.softmax`、不用 `matmul`，纯手工算出 6 个词的注意力权重矩阵并验证归一化。
2. **操作步骤**：

   ```python
   import torch
   torch.manual_seed(123)
   embed = torch.nn.Embedding(10, 16)
   words = torch.tensor([0, 7, 1, 2, 5, 4])   # 6 个词：can you help me to this
   X = embed(words).detach()                   # (6, 16)

   # ① 手写得分：双层循环点积
   attn_scores = torch.empty(6, 6)
   for i in range(6):
       for j in range(6):
           attn_scores[i, j] = torch.dot(X[i], X[j])

   # ② 手写行 softmax：减最大值防溢出，再归一化
   def softmax_rows(m):
       m = m - m.max(dim=1, keepdim=True).values
       e = torch.exp(m)
       return e / e.sum(dim=1, keepdim=True)

   attn_weights = softmax_rows(attn_scores)
   print(attn_weights)              # 6x6 权重矩阵
   print(attn_weights.sum(dim=1))   # 归一化验证

   # ③ 上下文向量（允许用 matmul）
   context_vectors = attn_weights.matmul(X)
   print(context_vectors.shape)
   ```

3. **需要观察的现象**：6×6 矩阵每行都是 0 到 1 之间的数；第二行打印全为 1；`context_vectors` 形状为 `(6, 16)`。
4. **预期结果**：行和恒等于 1（softmax 的数学保证，减最大值不改变结果，只防 `exp` 上溢）；对角线上的权重（词和自己的内积）通常较大但不一定是行最大。矩阵具体数值依赖随机初始化，**待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：为什么 softmax 用 `dim=1` 而不是 `dim=0`？
   **答**：归一化必须沿着「一个查询对所有键」的方向。行是查询、列是键，所以沿列方向（`dim=1`）归一化；`dim=0` 归一化的是「所有查询对同一个键的得分」，语义完全错误。
2. **问**：权重矩阵每行和为 1，每列和也是 1 吗？
   **答**：不是。列和是「某个词被所有词关注的总量」，可以远大于或小于 1——中心词（被大家都关注）列和大，边缘词列和小。
3. **问**：为什么 `X @ X.T` 可以替换双层循环？
   **答**：矩阵乘的定义就是 \((XX^\top)_{ij} = \sum_l X_{il}X_{lj} = x^{(i)} \cdot x^{(j)}\)，两种写法逐元素相等，向量化版快得多。

### 4.3 缩放点积注意力：三路投影 + \(\sqrt{d_k}\) 缩放

#### 4.3.1 概念说明

朴素版有两个缺陷：一是词向量同时扮演查询/键/值三种角色，网络无法学到「我用来匹配的表示」和「我实际提供的内容」的区别；二是没有可学习参数。缩放点积注意力的改造：

1. **三路投影**：引入三个可学习的 \(d \times d\) 矩阵 \(U_{query}\)、\(U_{key}\)、\(U_{value}\)，每个词向量分别投影出查询 \(q = U_{query}x\)、键 \(k = U_{key}x\)、值 \(v = U_{value}x\)；
2. **缩放**：得分除以 \(\sqrt{d_k}\) 后再 softmax：

\[\alpha_{ij} = \mathrm{softmax}\!\left(\frac{q^{(i)\top} k^{(j)}}{\sqrt{d_k}}\right), \qquad z^{(i)} = \sum_j \alpha_{ij} v^{(j)}\]

**为什么除以 \(\sqrt{d_k}\)**：若 \(q\)、\(k\) 的分量是相互独立、零均值、单位方差的随机变量，则点积 \(q^\top k = \sum_{l=1}^{d_k} q_l k_l\) 的方差是 \(d_k\)（标准差 \(\sqrt{d_k}\))。维度越大、得分绝对值越大，softmax 被推向饱和区——权重近似 one-hot，而 softmax 在饱和区对得分的导数趋近 0（与 u11-l1 sigmoid 饱和导致梯度消失是同一回事），训练失去梯度信号。除以 \(\sqrt{d_k}\) 恰好把方差拉回 1。本例 \(d_k = 16\)，除数是 4。

#### 4.3.2 核心流程

以「第 2 个词（下标 1）」为查询的完整流程：

```text
x_2 (16,)
  ① 三路投影  query_2 = U_query @ x_2；keys = (U_key @ X.T).T  → (8,16)
              values = (U_value @ X.T).T                       → (8,16)
  ② 打分      omega_2 = query_2 @ keys.T                      → (8,)  第 2 词对 8 个键的得分
  ③ 缩放+归一化 attention_weights_2 = softmax(omega_2 / sqrt(d), dim=0) → (8,)
  ④ 加权求和  context_vector_2 = attention_weights_2 @ values → (16,)
```

#### 4.3.3 源码精读

三个投影矩阵用 `torch.rand(d, d)` 随机初始化（[ch16/ch16-part1-self-attention.py:201-206](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L201-L206)，真实代码里它们只是演示用的随机张量，未参与训练）：

```python
d = embedded_sentence.shape[1]
U_query = torch.rand(d, d)
U_key = torch.rand(d, d)
U_value = torch.rand(d, d)
```

单个词的投影与全句批量投影并存（[ch16/ch16-part1-self-attention.py:211-218](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L211-L218)、[223-230](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L223-L230)）：

```python
query_2 = U_query.matmul(x_2)          # (16,16)@(16,) → (16,)
keys = U_key.matmul(embedded_sentence.T).T   # (16,16)@(16,8)→(16,8)，转置→(8,16)
values = U_value.matmul(embedded_sentence.T).T
```

`torch.allclose(key_2, keys[1])` 与 `torch.allclose(value_2, values[1])` 均为 `True`（notebook 输出），再次验证「逐个算」和「批量算」等价。

打分与缩放 softmax（[ch16/ch16-part1-self-attention.py:235-236](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L235-L236)、[241-242](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L241-L242)、[247-248](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L247-L248)）：

```python
omega_2 = query_2.matmul(keys.T)                      # (16,)@(16,8) → (8,)
attention_weights_2 = F.softmax(omega_2 / d**0.5, dim=0)
```

两个易错点：其一，这里 `dim=0` 与上一节的 `dim=1` 不矛盾——`omega_2` 是一维向量，`dim=0` 就是键轴；**归一化方向永远跟着键轴走**。其二，`/ d**0.5` 即 \(\sqrt{d_k}=4\) 的缩放，上一节没有这一步。

notebook 真实数值能直观感受缩放的必要性：`omega_2` 的 8 个得分从 -32.9 到 **53.9**（`torch.rand` 生成的投影矩阵非零均值，把得分绝对值推得很大）。除以 4 之后 softmax 输出最大权重 0.856（落在 "to" 上）；若**不**缩放，最大与次大得分相差 7.24，可推算未缩放最大权重 \(\approx 1/(1+e^{-7.24}) \approx 0.9993\)——几乎 one-hot。注意这些权重并无语义，因为投影矩阵是随机、未训练的。

最后加权求和（[ch16/ch16-part1-self-attention.py:262](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L262)）：`context_vector_2 = attention_weights_2.matmul(values)`，(8,)@(8,16) → (16,)。与朴素版的区别：加权对象从 \(x^{(j)}\) 换成了投影后的 \(v^{(j)}\)。

#### 4.3.4 代码实践

主实践第二部分：**对比缩放前后的权重分布**。

1. **实践目标**：用 4.2.4 的 `attn_scores`，量化 \(\sqrt{d_k}\) 缩放对 softmax 分布的影响。
2. **操作步骤**（接 4.2.4 的变量）：

   ```python
   d = X.shape[1]                                  # 16
   unscaled = softmax_rows(attn_scores)            # 复用手写 softmax
   scaled   = softmax_rows(attn_scores / d**0.5)

   print('未缩放 每行最大权重：', unscaled.max(dim=1).values)
   print('缩放后 每行最大权重：', scaled.max(dim=1).values)

   def entropy(w):                                 # 分布集中度：越小越接近 one-hot
       return -(w * torch.log(w)).sum(dim=1)
   print('未缩放 行熵：', entropy(unscaled))
   print('缩放后 行熵：', entropy(scaled))
   ```

3. **需要观察的现象**：两套权重每行和都是 1；未缩放的最大权重整体更接近 1、行熵更小；缩放后分布更平缓、行熵更大。
4. **预期结果**：缩放把行间得分的**相对差距**除以 4，softmax 温度降低，权重从「尖锐」变「平缓」——这正是防止梯度消失的意义。熵的具体数值取决于随机嵌入，**待本地验证**；但「未缩放熵 ≤ 缩放熵」这一单调关系在本数据上成立（若某行得分全相等则两者相等）。

#### 4.3.5 小练习与答案

1. **问**：为什么除以 \(\sqrt{d_k}\) 而不是 \(d_k\)？
   **答**：目的是把点积方差从 \(d_k\) 拉回 1，标准差是 \(\sqrt{d_k}\)；除以 \(d_k\) 会把方差压到 \(1/d_k\)，矫枉过正。
2. **问**：query、key、value 三者的分工是什么？
   **答**：query 是当前词的「提问表示」，key 是每个词的「被检索表示」，两者点积度量匹配度；value 是匹配成功后实际被加权取走的内容，让「用于匹配的表示」与「用于传递的信息」解耦、各自可学习。
3. **问**：朴素版里 \(z^{(i)}=\sum_j \alpha_{ij}x^{(j)}\)，参数化版改成了 \(\sum_j \alpha_{ij}v^{(j)}\)，这一改动带来什么自由度？
   **答**：输出不再局限于输入向量的线性组合（凸组合），value 投影让每个词能以任意线性变换后的「内容」参与聚合，表达能力更强。

### 4.4 多头注意力：h 个头的并行计算与拼接

#### 4.4.1 概念说明

一个注意力头只能学一种「相关性模式」（比如句法依赖）。多头注意力同时做 \(h\) 组独立的缩放点积注意力：每个头有自己的 \(U_{query}^{(m)}\)、\(U_{key}^{(m)}\)、\(U_{value}^{(m)}\)，相当于把 \(d\) 维表示空间切成 \(h\) 个子空间并行交互，再把各头的输出**拼接**（concatenate）成一个 \(h \times d\) 维长向量，最后用一个线性层投影回 \(d\) 维。直觉类比 u7-l1 的集成：多个「视角」各自关注不同的匹配模式，融合后比单头更丰富。

#### 4.4.2 核心流程

```text
X: (8, 16)，h = 8 个头
  ① 堆叠权重    multihead_U_key: (8, 16, 16)   # 每个头一套 (d, d)
  ② 复制输入    stacked_inputs = X.T.repeat(8, 1, 1) → (8, 16, 8)   # (头, d, T)
  ③ 批量投影    multihead_keys = bmm(multihead_U_key, stacked_inputs) → (8, 16, 8)
  ④ 调整轴序    permute(0, 2, 1) → (8, 8, 16)   # (头, 词, 维)，可按 [头, 词] 直接索引
  ⑤ （每个头各自做 4.3 的缩放点积注意力 → 每头得到 (T, d) 的上下文向量）
  ⑥ 拼接+投影   每头 (T,d) → 拼成 (T, 8*16=128) → nn.Linear(128, 16) → (T, 16)
```

仓库代码演示到 ④ 和 ⑥，第 ⑤ 步与前一小节完全相同故直接用随机张量占位。

#### 4.4.3 源码精读

单头与多头的对照（[ch16/ch16-part1-self-attention.py:276-287](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L276-L287)）：

```python
h = 8
multihead_U_query = torch.rand(h, d, d)
multihead_U_key = torch.rand(h, d, d)
multihead_U_value = torch.rand(h, d, d)
```

`one_U_query` 是上一节的 (16,16) 单头矩阵；多头只是在外面多套一层「头」维度，变成 `(h, d, d)`。

**广播批量投影**：三维权重直接乘一维词向量（[ch16/ch16-part1-self-attention.py:292-299](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L292-L299)）：

```python
multihead_query_2 = multihead_U_query.matmul(x_2)   # (8,16,16)@(16,) → (8,16)
```

`matmul` 把后两个维度做矩阵-向量乘、批次维（头）自动广播，一次得到 8 个头各自的查询向量；notebook 确认形状 `torch.Size([8, 16])`。

**bmm 批量投影全句**（[ch16/ch16-part1-self-attention.py:309-316](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L309-L316)）：

```python
stacked_inputs = embedded_sentence.T.repeat(8, 1, 1)      # (16,8) → (8,16,8)
multihead_keys = torch.bmm(multihead_U_key, stacked_inputs)  # (8,16,16)@(8,16,8) → (8,16,8)
multihead_keys = multihead_keys.permute(0, 2, 1)            # → (8,8,16)
```

`repeat(8,1,1)` 沿第 0 维复制 8 份输入，供 8 个头各乘各的；`torch.bmm` 对每个头独立做 `(16,16)@(16,8)` 的矩阵乘（u12-l1 讲过：三维对三维时 `matmul` 就是 `bmm`，所以 values 那两行换用了 `torch.matmul`，见 [第 332-333 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L332-L333)）。`permute(0, 2, 1)` 把 (头, 维, 词) 换成 (头, 词, 维)，此后 `multihead_keys[2, 1]`（第 3 个头、第 2 个词的键，[第 321-327 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L321-L327)）与单头算出的 `multihead_key_2[2]` 数值逐位相同（notebook 输出一致），证明两条路径等价。

**拼接与输出投影**（[ch16/ch16-part1-self-attention.py:338-349](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L338-L349)）：

```python
multihead_z_2 = torch.rand(8, 16)              # 占位：8 个头的上下文向量 z^(2)
linear = torch.nn.Linear(8*16, 16)
context_vector_2 = linear(multihead_z_2.flatten())
```

诚实说明：`multihead_z_2` 在这里是**随机占位张量**——逐头注意力计算与 4.3 完全相同，仓库略去不写；本段要演示的是融合方式：8 头 × 16 维摊平成 128 维，再经 `nn.Linear(8*16, 16)` 投影回词嵌入维度，notebook 输出 `torch.Size([16])`。

#### 4.4.4 代码实践

1. **实践目标**：验证「批量广播 matmul」与「逐头循环」结果一致，并观察头数对形状的影响。
2. **操作步骤**：

   ```python
   torch.manual_seed(123)
   h, d = 8, 16
   multihead_U_key = torch.rand(h, d, d)
   x_2 = X[1]                                     # 沿用 4.2.4 的 X，16 维

   per_head = torch.stack([multihead_U_key[i].matmul(x_2) for i in range(h)])
   batched  = multihead_U_key.matmul(x_2)         # 仓库写法
   print(per_head.shape, batched.shape)           # (8,16) (8,16)
   print(torch.allclose(per_head, batched))       # True

   two_head = torch.rand(2, d, d).matmul(x_2)     # 换 h=2
   print(two_head.shape)                          # (2,16)
   ```

3. **需要观察的现象**：两种写法形状、数值完全一致；头数改为 2 后第一维跟着变。
4. **预期结果**：`torch.Size([8, 16]) torch.Size([8, 16])`、`True`、`torch.Size([2, 16])`。批量 matmul 只是把 Python 循环下沉到 C++/BLAS，数学上逐头等价。

#### 4.4.5 小练习与答案

1. **问**：`torch.bmm` 和 `torch.matmul` 在这里是什么关系？
   **答**：对两个三维张量，两者等价：都按第 0 维（批次/头）配对做二维矩阵乘。仓库 keys 用 `bmm`、values 用 `matmul`，纯属写法差异。
2. **问**：`permute(0, 2, 1)` 为什么必要？
   **答**：投影后的形状是 (头, 嵌入维, 词数)，而按语义我们想按 (头, 词, 嵌入维) 索引；交换后两名 `[2, 1]` 才能直接读出「第 3 个头对第 2 个词」的键向量。
3. **问**：拼接后为什么还要 `nn.Linear(8*16, 16)`，而不是直接用 128 维向量？
   **答**：一是让各头在不同子空间学到的模式重新融合成统一表示；二是把输出维拉回 \(d=16\)，使注意力块的输出与输入同形状——这是 Transformer 残差连接（输出加回输入）的前提。

### 4.5 因果掩码与位置编码：Transformer 的两块拼图

#### 4.5.1 概念说明

仓库中这两节只有标题和插图：[ch16/ch16-part1-self-attention.py:352-358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L352-L358) 对应 notebook 里加载 [figures/16_08.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_08.png)（解码器与因果掩码）和 [figures/16_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/figures/16_09.png)（位置编码与层归一化），**没有可运行代码**。以下讲解配的代码均为本讲义补充的示例代码。

**因果掩码（causal mask）**：语言模型的训练目标是「用前 \(i\) 个词预测第 \(i+1\) 个词」。自注意力天然让每个词看到全句——包括未来的词，这等于考试看答案。解决方法：把得分矩阵的上三角（\(j > i\) 的位置）填成 \(-\infty\) 再 softmax，\(\exp(-\infty)=0\)，未来词的权重恰好为 0，且每行仍然归一化。

**位置编码（positional encoding）**：自注意力是「集合运算」。若把句子的词序打乱（对 \(X\) 的行做置换 \(P\)），则得分矩阵变为 \(P\Omega P^\top\)、权重矩阵同样被共轭置换，上下文向量只是跟着重排：\(Z' = PZ\)。网络输出对词序**置换等变**——它根本不知道「猫追狗」和「狗追猫」的区别。RNN 靠逐时间步计算天然携带位置；自注意力必须额外把位置信息加进嵌入。原论文用固定的正弦/余弦编码：

\[PE_{(pos, 2i)} = \sin\left(\frac{pos}{10000^{2i/d}}\right), \qquad PE_{(pos, 2i+1)} = \cos\left(\frac{pos}{10000^{2i/d}}\right)\]

不同维度对应不同波长的正弦波，每个位置得到独一无二的「指纹」，直接加到词嵌入上。层归一化（LayerNorm）则对每个位置的特征向量做标准化，稳定深层网络训练，详见插图 16_09。

#### 4.5.2 核心流程

```text
因果掩码（语言模型解码器）：
  scores (T, T) → 取上三角掩码 mask（j > i 为 True）
  → scores.masked_fill(mask, -inf) → softmax(dim=1)
  → 第 i 行只有前 i+1 个键有权重，未来位置全 0，行和仍为 1

位置编码（加在注意力之前）：
  X (T, d) + PE (T, d) → 再进注意力；PE 每行不同、与词内容无关
```

#### 4.5.3 源码精读

仓库中可引用的「源码」只有两个小节标题（[ch16/ch16-part1-self-attention.py:352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L352)、[358](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L358)），其正文是 notebook 里的两幅插图。掩码实现的最小示例（**示例代码，非仓库原有**）：

```python
# 示例代码：因果掩码
T = 4
scores = torch.randn(T, T)
mask = torch.triu(torch.ones(T, T), diagonal=1).bool()   # 上三角为 True
scores = scores.masked_fill(mask, float('-inf'))
w = torch.softmax(scores, dim=1)
```

位置编码的最小示例（**示例代码，非仓库原有**）：

```python
# 示例代码：正弦位置编码
import math
def positional_encoding(T, d):
    pe = torch.zeros(T, d)
    pos = torch.arange(T).unsqueeze(1)
    div = torch.exp(torch.arange(0, d, 2) * (-math.log(10000.0) / d))
    pe[:, 0::2] = torch.sin(pos * div)     # 偶数维 sin
    pe[:, 1::2] = torch.cos(pos * div)     # 奇数维 cos
    return pe
```

下一讲 u16-l2 将看到这一切在真实工程里的样子：GPT-2 的解码器内部就是「因果掩码多头注意力 + 位置嵌入」。

#### 4.5.4 代码实践

1. **实践目标**：亲眼确认因果掩码「屏蔽未来、保持归一化」的两个性质。
2. **操作步骤**（运行上面的示例代码后）：

   ```python
   print(w)              # 4x4 权重矩阵
   print(w.sum(dim=1))   # 行和
   print(torch.triu(w, diagonal=1).abs().max())   # 上三角的最大绝对值
   ```

3. **需要观察的现象**：矩阵上三角全为 0；第 0 行只有一个 1（只能看自己）；每行和为 1。
4. **预期结果**：行和恒为 1、上三角恒为 0（把 \(-\infty\) 填进 softmax 的数学必然）；`triu` 部分最大绝对值为 0。第 0 行恰为 `[1, 0, 0, 0]`——因果掩码下第一个词没有任何可看的过去。

#### 4.5.5 小练习与答案

1. **问**：为什么掩码要在 softmax **之前**填 \(-\infty\)，而不是在 softmax 之后把权重置 0？
   **答**：之前填 \(-\infty\) 让 \(\exp=0\)，剩余键自动重新归一化、行和仍为 1；之后置 0 会导致行和小于 1，上下文向量的整体幅度被压低，破坏加权平均的含义。
2. **问**：RNN 不需要位置编码，自注意力为什么需要？
   **答**：RNN 按时间步串行展开，顺序信息编码在计算顺序里；自注意力的三步运算（内积、softmax、加权求和）对行置换等变，本身不携带任何顺序信息，必须显式注入。
3. **问**：多头注意力 + 因果掩码同时使用时，掩码需要为每个头单独准备吗？
   **答**：不需要。所有头共享同一个「未来不可见」规则，得分张量形状为 (h, T, T) 时，(T, T) 的掩码会广播到每个头。

## 5. 综合实践

把本讲四个模块串成一个**最小多头自注意力函数**（示例代码，非仓库原有；输入输出约定与仓库 notebook 一致）：

```python
import torch
torch.manual_seed(123)

def multihead_self_attention(X, h=8, mask=None):
    T, d = X.shape                                   # (词数, 嵌入维)
    U_q, U_k, U_v = (torch.rand(h, d, d) for _ in range(3))

    Xr = X.T.repeat(h, 1, 1)                         # (h, d, T)：复制输入
    Q = torch.bmm(U_q, Xr).permute(0, 2, 1)          # (h, T, d)
    K = torch.bmm(U_k, Xr).permute(0, 2, 1)
    V = torch.bmm(U_v, Xr).permute(0, 2, 1)

    scores = torch.bmm(Q, K.transpose(1, 2)) / d**0.5   # (h, T, T)，√d_k 缩放
    if mask is not None:
        scores = scores.masked_fill(mask, float('-inf'))
    attn = torch.softmax(scores, dim=2)              # 沿键轴归一化

    Z = torch.bmm(attn, V)                           # (h, T, d)
    Z = Z.permute(1, 0, 2).reshape(T, h * d)         # (T, h*d)：拼接 8 个头
    out = torch.nn.Linear(h * d, d)(Z)               # 投影回 d 维
    return out, attn

sentence = torch.tensor([0, 7, 1, 2, 5, 6, 4, 3])
X = torch.nn.Embedding(10, 16)(sentence).detach()

out, attn = multihead_self_attention(X, h=8)
print(out.shape, attn.shape, attn.sum(dim=2))

T = X.shape[0]
causal = torch.triu(torch.ones(T, T), diagonal=1).bool()
out_m, attn_m = multihead_self_attention(X, h=8, mask=causal)
print(torch.triu(attn_m[0], diagonal=1).abs().max())   # 应为 0
```

任务清单与检验点：

1. **跑通并核对形状**：`out` 为 `torch.Size([8, 16])`，`attn` 为 `torch.Size([8, 8, 8])`——(头, 查询, 键)。
2. **核对归一化**：`attn.sum(dim=2)` 的每个元素都是 1，共 8×8 个 (头, 查询) 对。
3. **核对因果性**：传入掩码后任取一个头，其权重矩阵上三角最大绝对值为 0。
4. **对照仓库代码**：`bmm + permute` 对应 [ch16-part1-self-attention.py:309-322](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L309-L322)，拼接投影对应 [347-349 行](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part1-self-attention.py#L347-L349)——你刚刚把 notebook 里散落的所有零件组装成了一件完整工具。
5. 输出的具体数值依赖随机权重，**待本地验证**；上述形状与归一化性质由代码结构保证。

## 6. 本讲小结

- 自注意力三步曲：**点积得分** \(\omega_{ij} = x^{(i)\top}x^{(j)}\) → **沿键轴 softmax** 得到每行和为 1 的注意力权重 → **加权求和**得到上下文向量；双层循环与 `X @ X.T`、`A @ X` 两种矩阵乘写法逐元素等价。
- 缩放点积注意力做两个升级：\(U_{query}/U_{key}/U_{value}\) 三路投影让「匹配用的表示」与「传递的内容」解耦且可学习；点积方差随 \(d_k\) 增长，除以 \(\sqrt{d_k}\) 把它拉回 1，防止 softmax 饱和导致的梯度消失（notebook 实例：未缩放最大权重 ≈ 0.9993，缩放后 0.856）。
- 多头注意力 = \(h\) 套独立投影并行做注意力：权重堆叠成 `(h, d, d)`，`repeat` 复制输入、`bmm` 批量投影、`permute` 把轴序调成 (头, 词, 维)，各头输出拼接成 \(h \times d\) 维后经 `nn.Linear(h*d, d)` 融合回原维度。
- 因果掩码在 softmax 之前把「未来位置」填 \(-\infty\)，权重归零且行和仍为 1，是语言模型解码器的标配；自注意力对词序置换等变，必须用位置编码显式注入顺序信息——这两节在仓库中只有插图（16_08、16_09），本讲示例代码补齐了最小实现。
- 注意 `F.softmax` 的维度参数跟着**键轴**走：二维得分矩阵用 `dim=1`，一维得分向量用 `dim=0`——ch16 源码两处写法不同正是这个原因。

## 7. 下一步学习建议

本讲实现的是「教科书版」注意力：随机权重、无训练、逐零件演示。下一讲 **u16-l2（GPT-2 微调做情感分类）** 将看到工业级形态：加载预训练 GPT-2 权重、冻结基础层、只训练新分类头，并用 AdamW 与线性衰减学习率完成微调——其中解码器每一层都用到了本讲的因果掩码多头注意力与位置编码。建议阅读顺序：

1. 先跑通本讲综合实践，确保 8×16 输入、(h, T, T) 权重、行归一化三件事在脑中有画面；
2. 再打开 [ch16/ch16-part2-gpt2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch16/ch16-part2-gpt2.py)，重点看 `AutoModelForSequenceClassification` 如何在预训练 GPT-2 顶部换一个随机初始化的分类头；
3. 若想先补背景，可回看 u15-l1 的语言模型一节（自回归预测下一词正是因果掩码保护的目标）和 u14-l3 的迁移学习两步改造法（冻结 + 替换头，与本讲微调流程同构）。
