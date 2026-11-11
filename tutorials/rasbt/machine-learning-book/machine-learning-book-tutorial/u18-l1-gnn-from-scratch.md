# u18-l1 图卷积层从零实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清「一张图」在代码里如何用**节点特征矩阵 X** 与**邻接矩阵 A** 两个矩阵共同表示，并理解二者行顺序必须对齐的原因。
2. 读懂 numpy 版图卷积公式 `h = X·W1 + A·X·W2`，并从矩阵乘法「行视角」解释它为什么等价于「每个节点把邻居的特征加起来」。
3. 逐行读懂 PyTorch 版 `BasicGraphConvolutionLayer` 的 `forward`，以及 `NodeNetwork` 如何把卷积层、ReLU、全局池化与全连接层串成图分类网络。
4. 理解**不同大小的图为什么不能直接堆成张量批次**，以及 `global_sum_pool`、`get_batch_tensor`、`collate_graphs` 三件套如何用「块对角邻接矩阵 + 0/1 成员矩阵」解决不规则数据的批处理。
5. 掌握 `ExampleDataset` 这种「每个样本是一个 dict」的自定义 Dataset 写法，并知道此时必须给 `DataLoader` 传自定义 `collate_fn`。

本讲是第 18 章（图神经网络）的第一讲，只依赖第 14 章的 CNN 基础（局部聚合、卷积输出尺寸）与前面已建立的 `nn.Module`、`Dataset`/`DataLoader` 知识，不需要任何图神经网络背景。

## 2. 前置知识

**（1）图（graph）的三个词。** 图 \( G=(V, E) \) 由节点集合 \( V \) 和边集合 \( E \) 组成。**无向图**的边没有方向（如「原子之间有化学键」），**有向图**的边有方向（如「论文引用」）。与某个节点直接相连的边数称为它的**度（degree）**。本书用分子做例子：原子是节点、化学键是边、原子自身属性（元素种类等）是节点特征。

**（2）邻接矩阵（adjacency matrix）。** \( n \) 个节点的图对应 \( n \times n \) 的 0/1 矩阵 \( A \)：若节点 \( i \) 与 \( j \) 之间有边则 \( A_{ij}=1 \)，否则为 0。无向图的 \( A \) 是对称矩阵。对角线为 0（自己不连自己，即没有自环）。

**（3）矩阵乘法的「行视角」。** 我们惯用 \( (AB)_{ij} = \sum_k A_{ik}B_{kj} \)，但本讲更需要另一种读法：**乘积的第 \( i \) 行 = 左矩阵第 \( i \) 行对右矩阵各行做加权组合**。特别地，当左矩阵是 0/1 矩阵时，第 \( i \) 行等于「把右矩阵中被打 1 标记的那些行**原样加起来**」。这是理解 `A @ X` 聚合邻居、`batch_mat @ X` 图池化这两件事的同一把钥匙。

**（4）为什么 CNN/RNN 处理不了一般的图。** 第 14 章的卷积核在固定大小的网格窗口上滑动，隐含「像素有空间顺序、邻居数固定」；第 15 章的 RNN 假设输入是链式序列。而一般图：节点数不固定、每个节点邻居数不同、节点之间没有天然先后顺序（把节点重新编号，图还是同一张图，即**置换不变性**）。所以图神经网络需要一套新的「卷积」定义。

**（5）复习两个旧知识。** 一是 [u13-l1] 的参数注册规则：自定义层里必须用 `nn.Parameter` 包装张量，`parameters()`、`state_dict()` 才认领它；二是 [u12-l2] 的 Dataset 契约：实现 `__len__` 与 `__getitem__` 即可交给 `DataLoader`。本讲会把这两条规则用到图数据上。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [ch18/ch18_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L1-L385) | 第 18 章前半部分：从 networkx 构图、numpy 版图卷积直觉，到 PyTorch 版 `NodeNetwork`、`BasicGraphConvolutionLayer`、`global_sum_pool`、`collate_graphs`、`ExampleDataset` 与推理验证 |

文件内部的代码段地图（行号供快速跳转）：

| 行号 | 内容 |
| --- | --- |
| L4-L12、L32-L38 | 导入与包版本检查（networkx 2.6.2、torch 1.8.0、numpy 1.21.2） |
| L108-L120 | 用 networkx 构建第一张 4 节点图并导出邻接矩阵 |
| L125-L133 | `build_graph_color_label_representation`：颜色 → one-hot 节点特征 |
| L148-L151 | numpy 版图卷积 `h = np.dot(X,W_1) + np.dot(np.dot(A, X), W_2)` |
| L168-L184 | `NodeNetwork`：两层图卷积 + 全局池化 + 全连接 + softmax |
| L195-L215 | `BasicGraphConvolutionLayer`：本讲主角 |
| L222-L226 | `global_sum_pool` |
| L235-L245 | `get_batch_tensor`：生成 0/1 成员矩阵 |
| L250-L273 | `collate_graphs`：把一批图拼成块对角大图 |
| L280-L318 | `get_graph_dict` 与 4 张示例图 G1~G4 |
| L329-L347 | `ExampleDataset` 与带 `collate_fn` 的 `DataLoader` |
| L354-L370 | 前向推理与「单图前向 vs 批处理前向」一致性验证 |

注意：该脚本**没有训练循环**，只演示前向传播与批处理正确性；真正的分子图数据集训练在 `ch18/ch18_part2.py`（下一讲 u18-l2）。

## 4. 核心概念与源码讲解

### 4.1 networkx 图构建与邻接矩阵

#### 4.1.1 概念说明

要让神经网络处理图，第一步是把图「翻译」成数值。本模块介绍官方的图构建工具 networkx，以及图的**双矩阵表示法**：

- **邻接矩阵 \( A \in \{0,1\}^{n \times n} \)**：描述**结构**（谁连谁），与节点属性无关；
- **节点特征矩阵 \( X \in \mathbb{R}^{n \times f} \)**：描述**内容**，第 \( i \) 行是节点 \( i \) 的特征向量。

两者行数相同且**第 \( i \) 行必须指同一个节点**——这是本模块最重要的正确性约定。此外，本模块还包含书里先用 numpy 手写的图卷积公式，它是后面 PyTorch 层的数学原型。

#### 4.1.2 核心流程

```
1. G = nx.Graph()                          # 创建空无向图
2. add_nodes_from([...])                   # 加节点，附带 color 属性
3. add_edges_from([...])                   # 加边
4. A = nx.adjacency_matrix(G).todense()    # 导出邻接矩阵（scipy 稀疏 → 稠密）
5. build_graph_color_label_representation  # 把 color 属性编成 one-hot 矩阵 X
6. h = X·W_1 + (A·X)·W_2                   # numpy 版图卷积（原型公式）
```

numpy 原型公式（与书中一致）：

\[ h = X W_1 + (A X) W_2 \]

其中 \( W_1, W_2 \in \mathbb{R}^{f_{in} \times f_{out}} \)。对第 \( i \) 个节点展开（利用行视角，\( \mathcal{N}(i) \) 是 \( i \) 的邻居集合）：

\[ (AX)_{i,:} = \sum_{j \in \mathcal{N}(i)} X_{j,:} \qquad\Longrightarrow\qquad h_{i,:} = X_{i,:}W_1 + \Big(\sum_{j \in \mathcal{N}(i)} X_{j,:}\Big) W_2 \]

即每个新表示 = **自身变换**（\( W_1 \) 通道）+ **邻居求和后变换**（\( W_2 \) 通道）。由于 \( A \) 对角线为 0，邻居求和里不含自己，自身信息由 \( W_1 \) 单独补上——这就是后面代码里 `root_update` 与 `W1` 的来历。

#### 4.1.3 源码精读

**构图与邻接矩阵导出**（[ch18/ch18_part1.py:L108-L120](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L108-L120)）：用 `add_nodes_from` 加 4 个节点、每个节点带一个十六进制颜色串作为 `color` 属性；`add_edges_from` 加 4 条边 (1,2)、(2,3)、(1,3)、(3,4)；最后 `nx.adjacency_matrix(G).todense()` 得到 scipy 稀疏矩阵再转稠密 numpy 数组并打印。

```python
G.add_nodes_from([(1, {"color": blue}),
                  (2, {"color": orange}),
                  (3, {"color": blue}),
                  (4, {"color": green})])
G.add_edges_from([(1, 2),(2, 3),(1, 3),(3, 4)])
A = np.asarray(nx.adjacency_matrix(G).todense())
```

打印出的 \( A \)（行、列按节点加入顺序 1~4 排列）：

```
[[0 1 1 0]
 [1 0 1 0]
 [1 1 0 1]
 [0 0 1 0]]
```

第 1 行读作「节点 1 与节点 2、3 相连」。注意 \( A \) 对称、对角线全 0。

**颜色属性 → one-hot 特征矩阵**（[ch18/ch18_part1.py:L125-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L125-L133)）：`mapping_dict` 把三种颜色映射到列下标 {green:0, blue:1, orange:2}；`nx.get_node_attributes(G, 'color').values()` 按节点加入顺序取出颜色，转成下标数组 `[1, 2, 1, 0]`；再用 numpy 花式索引一次性置 1。

```python
one_hot_idxs = np.array([mapping_dict[v] for v in
                         nx.get_node_attributes(G, 'color').values()])
one_hot_encoding = np.zeros((one_hot_idxs.size, len(mapping_dict)))
one_hot_encoding[np.arange(one_hot_idxs.size), one_hot_idxs] = 1
```

得到的 \( X \)（4 节点 × 3 种颜色）：

```
[[0 1 0]    # 节点1 蓝
 [0 0 1]    # 节点2 橙
 [0 1 0]    # 节点3 蓝
 [1 0 0]]   # 节点4 绿
```

关键点：`nx.adjacency_matrix` 与 `get_node_attributes` 都按**同一节点顺序**（networkx 按节点插入顺序）排列，所以 \( A \) 的第 \( i \) 行与 \( X \) 的第 \( i \) 行自动对齐。如果你自己构造数据，必须亲自保证这个对齐，否则图卷积会「张冠李戴」地聚合错误邻居的特征。

**numpy 版图卷积原型**（[ch18/ch18_part1.py:L148-L151](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L148-L151)）：`f_in=3`（三种颜色），`f_out=6`，两个随机权重矩阵，一行算出 4×6 的新节点表示：

```python
f_in, f_out = X.shape[1], 6
W_1 = np.random.rand(f_in, f_out)
W_2 = np.random.rand(f_in, f_out)
h = np.dot(X,W_1) + np.dot(np.dot(A, X), W_2)
```

由结合律 \( A(XW_2) = (AX)W_2 \)，两种括号方式结果相同；后面 PyTorch 版选择先变换（`X @ W2`）再聚合（`A @ ...`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「\( A \) 左乘 \( X \) = 按行求和邻居特征」这一本讲最核心的矩阵事实。

**操作步骤**（以下为示例代码，可直接在装好 numpy 与 networkx 的环境中运行；若未装请回到 [u1-l2] 建环境）：

```python
import numpy as np
import networkx as nx

G = nx.Graph()
blue, orange, green = "#1f77b4", "#ff7f0e", "#2ca02c"
G.add_nodes_from([(1, {"color": blue}), (2, {"color": orange}),
                  (3, {"color": blue}),   (4, {"color": green})])
G.add_edges_from([(1, 2), (2, 3), (1, 3), (3, 4)])

A = np.asarray(nx.adjacency_matrix(G).todense())

# 手工构造与 build_graph_color_label_representation 等价的 X
X = np.array([[0,1,0],   # 蓝
              [0,0,1],   # 橙
              [0,1,0],   # 蓝
              [1,0,0]])  # 绿

AX = A @ X
print("A @ X =\n", AX)

# 手算对照：节点 1 的邻居是 2 和 3
manual = X[1] + X[2]
print("节点1 邻居特征之和 =", manual)      # 预期 [0 1 1]
print("A@X 第 1 行        =", AX[0])
print("两者相等:", np.allclose(AX[0], manual))
```

**需要观察的现象**：`A @ X` 的每一行恰好等于对应节点全部邻居的特征之和；节点 1 的行 = `X[2] + X[3]`（0 基下标的 X[1]+X[2]），节点 4 的行只等于 `X[3]`（它只有一个邻居）。

**预期结果**（手推值，待本地验证）：`A @ X` 为

```
[[0 1 1]     # 节点1：橙(0,0,1) + 蓝(0,1,0)
 [0 2 0]     # 节点2：蓝 + 蓝
 [1 1 1]     # 节点3：蓝 + 橙 + 绿
 [0 1 0]]    # 节点4：蓝
```

且最后一行打印 `两者相等: True`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `mapping_dict` 改成 `{green: 1, blue: 0, orange: 2}`（交换 green 与 blue 的列号），\( X \) 会怎么变？图卷积的数学结构受影响吗？

**答案**：\( X \) 的第 0 列与第 1 列互换（blue 变 [1,0,0]、green 变 [0,1,0]），其余不变。数学结构完全不受影响——这只是特征的另一种编码；由于 \( W_1、W_2 \) 是可学习参数，网络会自动适配列的重排，学习到的模型是等价的。

**练习 2**：本模块的图中节点 3 的度是多少？它对应 \( (AX) \) 的哪一行、该行是多少个邻居向量的和？

**答案**：节点 3 与 1、2、4 相连，度为 3；对应 \( (AX) \) 第 2 行（0 基），是 3 个邻居特征向量（蓝 + 橙 + 绿）之和，即 [1,1,1]。这也暴露了求和聚合的一个特点：度越大的节点，聚合结果的数值越大，后面 4.2 会再提这个「未归一化」问题。

**练习 3**：为什么 `A` 的对角线全为 0？如果给节点加上自环（对角线为 1），公式 \( h = XW_1 + AXW_2 \) 里自身特征会被算几次？

**答案**：因为 networkx 的简单无向图不允许自环，邻接矩阵只记录「节点对之间的边」。若加自环，\( AX \) 的邻居求和中会包含 \( X_{i,:} \) 本身一次，再加上 \( XW_1 \) 的自身通道，自身特征共参与两次（且两次系数不同）。一些经典 GNN（如 GCN）正是显式加自环后再归一化的，而本书实现选择不加自环、用独立的 \( W_1 \) 通道处理自身信息。

### 4.2 BasicGraphConvolutionLayer 的 forward

#### 4.2.1 概念说明

`BasicGraphConvolutionLayer` 是把 4.1 的 numpy 原型搬进 `torch.nn.Module` 的可学习版本，也是全书的「图卷积」最小实现。它做的事叫**消息传递（message passing）**：

1. 每个节点把自己的特征经 \( W_2 \) 变换成一条「消息」；
2. 通过 \( A \) 把邻居发来的消息**求和**收进本节点；
3. 自身特征另经 \( W_1 \) 变换（代码里叫 `root_update`，即「根节点更新」，指节点自己）；
4. 两路相加再加偏置，得到新的节点表示。

与 CNN 卷积对比可加深理解：CNN 是「固定大小窗口 + 权重共享」的局部聚合，图卷积是「**不定大小的邻居集** + 权重共享」的局部聚合——\( W_1、W_2 \) 对所有节点、所有边共享，但每个节点聚合几个邻居由 \( A \) 决定，可多可少。

#### 4.2.2 核心流程

`forward(X, A)` 的四步（形状记号：\( n \) 为节点数）：

```
输入: X ∈ (n, f_in),  A ∈ (n, n)
1. potential_msgs  = X @ W2        # (n, f_out)  先变换：每个节点备好消息
2. propagated_msgs = A @ potential_msgs   # (n, f_out) 再聚合：收邻居消息
3. root_update     = X @ W1        # (n, f_out) 自身变换通道
4. output = propagated_msgs + root_update + bias   # (n, f_out) 逐元素相加
输出: H ∈ (n, f_out)，仍是「每节点一行」的表示
```

矩阵形式：

\[ H = A X W_2 + X W_1 + \mathbf{b}, \qquad W_1, W_2 \in \mathbb{R}^{f_{in} \times f_{out}},\ \mathbf{b} \in \mathbb{R}^{f_{out}} \]

逐节点读就是 \( H_{i,:} = X_{i,:}W_1 + \big(\sum_{j \in \mathcal{N}(i)} X_{j,:}\big) W_2 + \mathbf{b} \)。**输入 \( n \) 个节点就输出 \( n \) 行**——图卷积是节点级操作，不改变节点数量，这与 CNN 的卷积改变特征图尺寸、但保留「每个位置一个表示」的精神一致。

#### 4.2.3 源码精读

**参数定义**（[ch18/ch18_part1.py:L195-L208](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L195-L208)）：`W2`、`W1` 形状都是 `(in_channels, out_channels)`，`bias` 形状 `(out_channels,)`。三者也用 `Parameter` 包装（`from torch.nn.parameter import Parameter`，见 [L9](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L9)），因此会被 `net.parameters()` 收录、可交给优化器——这是 [u13-l1] 参数注册规则的直接应用。

```python
self.W2 = Parameter(torch.rand(
     (in_channels, out_channels), dtype=torch.float32))
self.W1 = Parameter(torch.rand(
     (in_channels, out_channels), dtype=torch.float32))
self.bias = Parameter(torch.zeros(
         out_channels, dtype=torch.float32))
```

两个可留意的初始化细节：权重用 `torch.rand`（[0,1) 均匀分布，本书此层未用 Xavier 初始化）；偏置置 0。另外注意形状约定与 `nn.Linear` 不同：`nn.Linear` 的权重是 `(out, in)` 且前向做 `x @ W.T`（见 [u12-l3]），这里直接存 `(in, out)`、前向做 `X @ W`，更贴近数学公式。

**forward 四步**（[ch18/ch18_part1.py:L210-L215](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L210-L215)）：`torch.mm` 是二维矩阵乘法；变量名把「消息传递」的三段拆得很清楚——`potential_msgs`（潜在消息）、`propagated_msgs`（已传播/收齐的消息）、`root_update`（根=自身的更新）：

```python
def forward(self, X, A):
    potential_msgs = torch.mm(X, self.W2)
    propagated_msgs = torch.mm(A, potential_msgs)
    root_update = torch.mm(X, self.W1)
    output = propagated_msgs + root_update + self.bias
    return output
```

偏置形状 `(f_out,)` 与 `(n, f_out)` 相加时按 numpy 风格广播到每一行（见 [u11-l1] 的广播概念）。

**使用者：NodeNetwork**（[ch18/ch18_part1.py:L168-L184](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L168-L184)）：两层图卷积（3 → 32 → 32）叠加，每层后 `.clamp(0)` 即 `ReLU`（把负值截为 0，就是 [u12-l3] 介绍过的激活函数）；随后 `global_sum_pool` 把节点维消掉（下一模块细讲）；再过两个全连接层 16 → 2，最后 `F.softmax(output, dim=1)` 输出二分类概率：

```python
def forward(self, X, A, batch_mat):
    x = self.conv_1(X, A).clamp(0)
    x = self.conv_2(x, A).clamp(0)
    output = global_sum_pool(x, batch_mat)
    output = self.fc_1(output)
    output = self.out_layer(output)
    return F.softmax(output, dim=1)
```

两点值得注意：其一，`conv_2` 仍接收**同一个** `A`——每层卷积都在原始图结构上做一轮消息传递，两层即「信息扩散两跳」：第 2 层的每个节点已含 2 跳邻居的信息。其二，`fc_1` 是 `torch.nn.Linear(32, 16)`，它要求输入「每行 32 个数」——节点数任意都行，但**每张图必须先变成一行**，这正是下一模块 `global_sum_pool` 存在的理由。

顺带一提本实现的一个已知局限（书 18.6 节 "Normalization" 讨论，本文件无代码）：求和聚合随节点度增大而数值变大，没有除以度数做归一化；下一讲 PyTorch Geometric 的 `GCNConv` 会用归一化的聚合方式缓解。

#### 4.2.4 代码实践

**实践目标**：确认 `BasicGraphConvolutionLayer` 对**不同节点数**的图都能前向，输出形状始终是「(该图节点数, out_channels)」；并验证 `A @ (X @ W2)` 与 `(A @ X) @ W2` 数值一致（结合律在张量里同样成立）。

**操作步骤**：`import ch18_part1` 会执行整章脚本（含包检查与画图），不推荐；建议把 [L195-L215](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L195-L215) 的 `BasicGraphConvolutionLayer` 逐字复制到自己的练习脚本（以下为示例代码）：

```python
import torch
torch.manual_seed(1)

class BasicGraphConvolutionLayer(torch.nn.Module):   # ← 从 ch18_part1.py 原样复制
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.W2 = Parameter(torch.rand((in_channels, out_channels), dtype=torch.float32))
        self.W1 = Parameter(torch.rand((in_channels, out_channels), dtype=torch.float32))
        self.bias = Parameter(torch.zeros(out_channels, dtype=torch.float32))
    def forward(self, X, A):
        potential_msgs = torch.mm(X, self.W2)
        propagated_msgs = torch.mm(A, potential_msgs)
        root_update = torch.mm(X, self.W1)
        return propagated_msgs + root_update + self.bias

from torch.nn.parameter import Parameter

conv = BasicGraphConvolutionLayer(3, 8)      # f_in=3, f_out=8
print("W1:", conv.W1.shape, "W2:", conv.W2.shape, "bias:", conv.bias.shape)

X1 = torch.tensor([[0.,1.,0.],[0.,0.,1.],[0.,1.,0.],[1.,0.,0.]])   # 4 节点
A1 = torch.tensor([[0.,1.,1.,0.],[1.,0.,1.,0.],[1.,1.,0.,1.],[0.,0.,1.,0.]])
X2 = torch.eye(3)                                                   # 3 节点
A2 = torch.tensor([[0.,1.,0.],[1.,0.,1.],[0.,1.,0.]])

print("图1 输出:", conv(X1, A1).shape)
print("图2 输出:", conv(X2, A2).shape)

# 结合律验证：先变换再聚合 == 先聚合再变换
lhs = torch.mm(A1, torch.mm(X1, conv.W2))
rhs = torch.mm(torch.mm(A1, X1), conv.W2)
print("结合律一致:", torch.allclose(lhs, rhs))
```

**需要观察的现象**：两个节点数不同的图（4 与 3）共用**同一组** `W1/W2/bias`，输出形状分别为 `(4, 8)` 与 `(3, 8)`——同一个层对象天然支持任意大小的图，这正是「权重共享、结构外置」的收益。

**预期结果**（待本地验证）：`W1: torch.Size([3, 8]) W2: torch.Size([3, 8]) bias: torch.Size([8])`；两图输出分别为 `torch.Size([4, 8])` 与 `torch.Size([3, 8])`；`结合律一致: True`。

#### 4.2.5 小练习与答案

**练习 1**：`W1` 与 `W2` 各自负责什么？把 `root_update` 那一行删掉，输出的哪部分会消失？

**答案**：`W2` 负责「发给邻居的消息」的变换（先变换再被 `A` 聚合），`W1` 负责「节点自身」的变换。删掉 `root_update` 后输出只剩 `A @ (X @ W2) + bias`，即每个节点的表示只由邻居决定、不含自身原始特征——由于 `A` 对角线为 0，一个没有邻居的孤立节点将只能得到常数偏置 `bias`。

**练习 2**：`forward` 里为什么用 `torch.mm` 而不是 `@` 或 `torch.matmul`？三者有区别吗？

**答案**：功能上无区别（`@` 是 `torch.matmul` 的运算符写法）。`torch.mm` 只支持二维矩阵乘法，维度不符会直接报错；`torch.matmul` 还支持广播与批量维度。这里 X、A 恒为二维，用 `torch.mm` 语义最明确，出了形状错误也最容易定位。

**练习 3**：两层 `BasicGraphConvolutionLayer` 叠加后，第 2 层输出的每个节点大约包含了几跳邻居的信息？为什么？

**答案**：两跳。第 1 层后每个节点的表示含其 1 跳邻居的信息；第 2 层在新表示上再做一轮 `A` 聚合，等于把「已含 1 跳信息的邻居」再收一遍，于是覆盖到 2 跳。层数就是信息扩散的跳数——这也是 GNN 层数通常很浅的原因之一（过深会过度平滑，所有节点表示趋同）。

### 4.3 global_sum_pool 与 get_batch_tensor

#### 4.3.1 概念说明

图卷积输出仍是「每节点一行」，但 `NodeNetwork` 的分类头 `torch.nn.Linear(32, 16)` 需要的是**每张图一行**的定长向量。把一整张图的全部节点表示压成一个向量的操作叫**全局池化（global pooling）**，本书实现为求和版 `global_sum_pool`：

\[ P(X)_g = \sum_{j \in \text{图 } g} X_{j,:} \]

它解决的是「图大小不一」的问题：无论一张图有 3 个还是 6 个节点，池化后都是同一个 `f_out` 维向量，后续全连接层才能工作。配套的 `get_batch_tensor` 则在批处理时生成一个 0/1 **成员矩阵** \( B \in \{0,1\}^{C \times N} \)（\( C \) 为批内图数、\( N \) 为批内节点总数），\( B_{g,j}=1 \) 表示节点 \( j \) 属于图 \( g \)。这样池化又变回了一次矩阵乘法：

\[ P(X) = B X \in \mathbb{R}^{C \times f_{out}} \]

第 \( g \) 行 = 第 \( g \) 张图全部节点行之和。这和 4.1 的「`A @ X` 聚合邻居」是同一招：**0/1 矩阵左乘 = 按行选择性求和**。

#### 4.3.2 核心流程

```
global_sum_pool(X, batch_mat):
  若 batch_mat 为 None 或一维(单图场景):
      返回 X 沿节点维求和 → (f_out,)，再 unsqueeze(0) → (1, f_out)
  否则(批场景):
      返回 batch_mat @ X → (批内图数 C, f_out)

get_batch_tensor(graph_sizes):        # 例: [4, 5]
  starts = [0, 4]; stops = [4, 9]
  生成 (C, N) = (2, 9) 的 0/1 矩阵:
      第 0 行 1 的位置: 0..3    (属于图 1 的节点)
      第 1 行 1 的位置: 4..8    (属于图 2 的节点)
```

在 `NodeNetwork.forward` 中的位置：`conv_1 → conv_2`（节点级，形状 `(N, 32)`）→ `global_sum_pool`（图级，`(C, 32)`）→ `fc_1`（`(C, 16)`）→ `out_layer`（`(C, 2)`）→ softmax。**池化是节点级表示与图级预测之间的桥梁**。

#### 4.3.3 源码精读

**global_sum_pool**（[ch18/ch18_part1.py:L222-L226](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L222-L226)）：两条分支。单图分支（`batch_mat is None`，即 `get_graph_dict` 造出的样本里 `'batch': None`）对 `dim=0`（节点维）求和得到一维向量，`unsqueeze(0)` 补回批次维，保证单图与批处理的输出都是二维、能统一过 `Linear`；批分支用一次 `torch.mm` 同时算出批内所有图的池化结果：

```python
def global_sum_pool(X, batch_mat):
    if batch_mat is None or batch_mat.dim() == 1:
        return torch.sum(X, dim=0).unsqueeze(0)
    else:
        return torch.mm(batch_mat, X)
```

条件 `batch_mat.dim() == 1` 是留给「一维成员向量（每节点一个图编号）」这种常见批表示的入口，本仓库代码实际走的是 `None` 分支与矩阵分支。

**get_batch_tensor**（[ch18/ch18_part1.py:L235-L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L235-L245)）：由各图节点数算出每张图在大特征矩阵中的行区间 `[start, stop)`，先建全 0 的 `(C, N)` 矩阵，再把每行自己的区间置 1：

```python
starts = [sum(graph_sizes[:idx]) for idx in range(len(graph_sizes))]
stops = [starts[idx]+graph_sizes[idx] for idx in range(len(graph_sizes))]
...
batch_mat = torch.zeros([batch_size, tot_len]).float()
for idx, starts_and_stops in enumerate(zip(starts, stops)):
    batch_mat[idx, start:stop] = 1
```

以 `graph_sizes=[4, 5]` 为例（手推结果）：`starts=[0,4]`、`stops=[4,9]`，得到

```
B = [[1 1 1 1 0 0 0 0 0],
     [0 0 0 0 1 1 1 1 1]]
```

于是 `B @ X`（X 为 (9, 32)）的第 0 行 = 图 1 的 4 个节点行之和，第 1 行 = 图 2 的 5 个节点行之和，一次矩阵乘完成两张图的池化。

#### 4.3.4 代码实践

**实践目标**：用一个小张量验证「成员矩阵乘 = 分图求和」，并确认输入节点数不同时池化输出维度恒定。

**操作步骤**（示例代码；`global_sum_pool`、`get_batch_tensor` 从 [L222-L226](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L222-L226) 与 [L235-L245](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L235-L245) 原样复制）：

```python
import torch
# ...（此处粘贴 global_sum_pool 与 get_batch_tensor 的定义）

B = get_batch_tensor([4, 5])
print("batch_mat:\n", B)

# 模拟 9 个节点、每个 2 维表示的大 X
X = torch.arange(18).float().reshape(9, 2)

pooled = global_sum_pool(X, B)
print("批池化形状:", pooled.shape)          # 预期 (2, 2)
print("第0行:", pooled[0], "== 手算:", X[:4].sum(dim=0))
print("第1行:", pooled[1], "== 手算:", X[4:].sum(dim=0))

# 单图分支
single = global_sum_pool(X[:4], None)
print("单图池化形状:", single.shape)          # 预期 (1, 2)
```

**需要观察的现象**：`get_batch_tensor([4,5])` 产出的 0/1 矩阵与上面手推一致；批池化第 0/1 行分别等于前 4 行、后 5 行之和；单图分支输出 `(1, 2)` 而不是 `(2,)`——`unsqueeze(0)` 把批次维补上了。

**预期结果**（待本地验证）：`pooled` 形状 `torch.Size([2, 2])`，第 0 行 = `[24., 28.]`（0+2+4+6 与 1+3+5+7），第 1 行 = `[40., 44.]`（8+10+12+14+16 与 9+11+13+15+17）；`single` 形状 `torch.Size([1, 2])`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `global_sum_pool` 的求和换成取平均（`X.mean(dim=0)` 与 `batch_mat` 每行除以该图节点数后再乘），会带来什么不同？

**答案**：求和版池化会让大图的向量幅值普遍偏大（节点越多加得越多），网络可能把「图的大小」当成隐含特征；平均版除以了节点数，幅值与图大小无关，但会丢掉「图有多大」这一信息。两种都在实践中使用（sum/mean/max 池化），选择取决于任务是否关心图规模。

**练习 2**：`NodeNetwork` 里如果把 `global_sum_pool` 换成 `x.max(dim=0)`（每列取最大值），`forward` 还能对任意大小的图工作吗？输出形状会怎么变？

**答案**：能。max 池化同样把节点维消掉，输出仍是「每图一行、32 列」，`fc_1` 照常工作（取最大值需处理批场景，例如用 `torch_scatter` 式分段 max，比求和实现麻烦一些）。核心结论是：任何「把不定行数的节点表示映射为定长向量、且对节点顺序不敏感」的函数都可用作全局池化。

**练习 3**：`B @ X` 与 `A @ X` 都是 0/1 矩阵左乘，它们各自的语义是什么？

**答案**：`A @ X` 在**一张图内部**沿边聚合邻居特征（图级 → 图级，第 2 个下标是「邻居」）；`B @ X` 在**一批图之间**按成员关系聚合每张图的全部节点（批级 → 图级，第 2 个下标是「属于本图的节点」）。前者做消息传递，后者做读出（readout）。

### 4.4 collate_graphs 与 ExampleDataset

#### 4.4.1 概念说明

`DataLoader` 的默认 `collate_fn` 会把一批样本**堆叠**成规则张量（如 (B, C, H, W)），这在 [u12-l2] 中对图像很好用。但图不行：G1 有 4 个节点（A 是 4×4），G2 有 5 个（A 是 5×5），`4×4` 与 `5×5` **无法堆叠**。更深一层，即使强行补零对齐，也会让「不存在的位置」混进邻接与特征，污染消息传递。

`collate_graphs` 给出的解法优雅而经典——**把一批图拼成一张不连通的大图**：

- 大邻接矩阵取**块对角**：各图自己的 A 放在对角块上，块外全 0。块外的 0 意味着**图与图之间没有边**，于是 `A @ X` 的每一行只会聚合到本图节点的特征——图卷积在大图上跑，效果与各图单独跑完全一致；
- 大特征矩阵沿节点维 `torch.cat`；
- 各图输出用 `get_batch_tensor` 的成员矩阵 B 区分，池化时各自求和。

`ExampleDataset` 则示范了「每个样本是一个 dict」的自定义 Dataset：`__getitem__` 返回 `{'A':…, 'X':…, 'y':…, 'batch': None}`，dict 里装着大小不一的张量——正是这种样本形态使得自定义 `collate_fn` 成为必需。

#### 4.4.2 核心流程

`collate_graphs(batch)` 的输入是样本 dict 列表（长度 = batch_size），输出是描述大图的 dict：

```
输入: [graph_1, graph_2, ...]，每个含 A(n_i×n_i)、X(n_i×f)、y、batch
  ├─ sizes = [n_1, n_2, ...];  N = Σn_i;  C = 批内图数
  ├─ batch_mat = get_batch_tensor(sizes)      # (C, N) 成员矩阵
  ├─ X'  = cat([各图 X], dim=0)               # (N, f)
  ├─ y'  = cat([各图 y], dim=0)               # (C, 2)
  └─ A'  = N×N 全 0，逐块把各图 A 填进对角块    # 块对角大邻接矩阵
输出: {'A': A', 'X': X', 'y': y', 'batch': batch_mat}
```

批前向的完整链路（以第一个批 [G1, G2]、sizes=[4,5]、N=9 为例）：

```
X'(9,3) → conv_1 → (9,32) → conv_2 → (9,32)
        → global_sum_pool(B(2,9) @ ·) → (2,32)
        → fc_1 → (2,16) → out_layer → (2,2) → softmax
```

#### 4.4.3 源码精读

**collate_graphs**（[ch18/ch18_part1.py:L250-L273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L250-L273)）：先收集各图邻接矩阵并算尺寸；`get_batch_tensor` 生成成员矩阵；`X`、`y` 直接 `torch.cat`；最后建 `tot_size × tot_size` 全 0 大矩阵，用累加游标 `accum` 把每张图的 A 逐块贴到对角线上：

```python
batch_adj = torch.zeros([tot_size, tot_size], dtype=torch.float32)
accum = 0
for adj in adj_mats:
    g_size = adj.shape[0]
    batch_adj[accum:accum+g_size, accum:accum+g_size] = adj
    accum = accum + g_size
```

以第一个批为例（手推）：G1 的 4×4 贴进 `batch_adj[0:4, 0:4]`，G2 的 5×5 贴进 `batch_adj[4:9, 4:9]`，其余位置为 0——不连通大图。

**get_graph_dict：从 networkx 图到样本 dict**（[ch18/ch18_part1.py:L280-L287](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L280-L287)）：把 numpy 的 A、X 转成 `torch.float` 张量；`y` 硬编码为 `[[1, 0]]`——注释里明说是 "kludge"（权宜之计），因为本章没有真实任务标签，只为演示二分类输出的形状；`'batch': None` 表示单图样本：

```python
A = torch.from_numpy(np.asarray(nx.adjacency_matrix(G).todense())).float()
X = torch.from_numpy(build_graph_color_label_representation(G,mapping_dict)).float()
y = torch.tensor([[1, 0]]).float()
return {'A': A, 'X': X, 'y': y, 'batch': None}
```

**四张示例图**（[ch18/ch18_part1.py:L291-L318](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L291-L318)）：G1~G4 的节点数分别是 **4、5、6、3**——刻意各不相同，正是为了演示不规则批处理。每张图用 `get_graph_dict` 转成 dict，存进 `graph_list`。

**ExampleDataset 与 DataLoader**（[ch18/ch18_part1.py:L329-L347](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L329-L347)）：Dataset 只是把 dict 列表包一层，`__len__` 返回图数、`__getitem__` 原样返回 dict——满足 [u12-l2] 的最小契约。关键在最后一行：`collate_fn=collate_graphs` 把默认的「堆叠」换成我们的「拼大图」，否则 DataLoader 会因为无法 stack 大小不一的 dict 内容而报错：

```python
class ExampleDataset(Dataset):
    def __init__(self, graph_list):
        self.graphs = graph_list
    def __len__(self):
        return len(self.graphs)
    def __getitem__(self,idx):
        mol_rep = self.graphs[idx]
        return mol_rep

dset = ExampleDataset(graph_list)
loader = DataLoader(dset, batch_size=2, shuffle=False, collate_fn=collate_graphs)
```

**推理与一致性验证**（[ch18/ch18_part1.py:L354-L370](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L354-L370)）：固定随机种子后建 `NodeNetwork(node_features=3)`；循环 loader 对每个批整体前向并 `detach()`；然后取单图样本单独前向，与批输出中的对应行比对：

```python
for b in loader:
    batch_results.append(net(b['X'], b['A'], b['batch']).detach())

G1_rep = dset[1]
G1_single = net(G1_rep['X'], G1_rep['A'], G1_rep['batch']).detach()
G1_batch = batch_results[0][1]
torch.all(torch.isclose(G1_single, G1_batch))
```

**一个值得指出的命名陷阱**：变量叫 `G1_rep`，但 `dset[1]` 取的是**下标 1，即第二张图 G2**；与之对照的 `batch_results[0][1]` 是第一个批（[G1, G2]）的**第 1 行，同样是 G2** 的输出。所以比对本身是「G2 单独跑 vs G2 在批里跑」，逻辑正确，只是变量名有误导性——读源码时要小心这类 0 基下标与命名不一致的地方。另注意用 `isclose` 而非 `==`：批内矩阵乘与单图求和的浮点求和顺序可能不同，容许极小的数值误差才是稳妥写法。最后一行 `torch.all(...)` 在 .py 脚本里没有打印（notebook 中会作为单元格输出显示），自己复现时记得套 `print()`。

#### 4.4.4 代码实践

**实践目标**：跑通「Dataset → 自定义 collate 的 DataLoader → 批前向」，逐批打印形状，并复现书中「单图前向与批前向一致」的验证。

**操作步骤**：直接运行整章脚本最省事——在仓库根目录执行（示例代码）：

```bash
cd ch18
python ch18_part1.py
```

若想自己组装，可把 [L250-L273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L250-L273) 的 `collate_graphs`、[L329-L340](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L329-L340) 的 `ExampleDataset` 复制出来，再为 4.1/4.2 的两张小图构造样本并加入如下检查代码（示例代码）：

```python
import torch

graph1 = {'A': A1, 'X': X1, 'y': torch.tensor([[1., 0.]]), 'batch': None}
graph2 = {'A': A2, 'X': X2, 'y': torch.tensor([[0., 1.]]), 'batch': None}
dset = ExampleDataset([graph1, graph2])
loader = torch.utils.data.DataLoader(dset, batch_size=2,
                                     shuffle=False, collate_fn=collate_graphs)

for b in loader:
    print({k: v.shape for k, v in b.items()})
    # 预期: A (7,7) 块对角 | X (7,3) | y (2,2) | batch (2,7)
    out = net(b['X'], b['A'], b['batch'])
    print("批前向输出:", out.shape)        # 预期 (2, 2)，每行和为 1

# 一致性验证（复现书中 L366-L370，注意 dset[0] 与批第 0 行同属 graph1）
single = net(graph1['X'], graph1['A'], None)
print(torch.all(torch.isclose(single[0], out[0])))   # 预期 True
```

**需要观察的现象**：collate 后 `A` 是 7×7（4+3 个节点）且右上、左下 4×3/3×4 区域全 0；`batch` 是 (2,7) 的 0/1 矩阵；softmax 输出每行两个数相加等于 1；单图前向与批前向第 0 行一致。

**预期结果**（待本地验证）：形状打印为 `{'A': torch.Size([7, 7]), 'X': torch.Size([7, 3]), 'y': torch.Size([2, 2]), 'batch': torch.Size([2, 7])}`；批输出 `torch.Size([2, 2])`；最后一行打印 `tensor(True)`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `collate_graphs` 里的大邻接矩阵从「块对角」改成「各图 A 简单 `torch.cat` 到一个 (C, n_max, n_max) 张量」（假设补零对齐），消息传递会发生什么？

**答案**：cat 成三维张量后 `BasicGraphConvolutionLayer.forward` 的 `torch.mm` 根本无法接收（它只吃二维）；即便改用批量矩阵乘 `torch.bmm` 绕过形状问题，补零对齐本身也会让「节点数」这一维度进入模型，不同图的补零位置成了虚假节点，聚合与池化都要额外遮罩处理。块对角方案的高明之处在于：**只用二维矩阵就复用了同一套层代码，且结构上天然屏蔽跨图信息**。

**练习 2**：书中 `y` 为什么硬编码为 `[[1, 0]]`？如果这是一个真实的三分类分子性质预测任务，`get_graph_dict` 与 `NodeNetwork` 各需要改哪一处？

**答案**：因为本章脚本没有真实任务，只是演示前向与批处理，作者用注释 "kludge since there is not specific task for this example" 说明这是权宜之计。真实三分类任务中：`get_graph_dict` 要把 `y` 换成该图的真实类别（one-hot 或类别索引）；`NodeNetwork` 的 `self.out_layer = torch.nn.Linear(16, 2)` 要改成输出 3 个单元（softmax 相应变为三类概率），下一讲 u18-l2 的分子图任务就是 2 分类版本的真实示例。

**练习 3**：`collate_graphs` 返回的 dict 里为什么还要带 `'batch'` 键？池化这一步放在 collate 里做掉（每张图直接算好图向量再堆叠）不行吗？

**答案**：`'batch'` 是批前向时 `global_sum_pool` 区分各图节点的唯一依据，`NodeNetwork.forward` 的第三个参数就来自它。把池化提前到 collate 里做会破坏「模型内部先卷积再读出」的结构——池化必须在（可学习的）卷积层之后进行，而 collate 发生在前向之前、不参与梯度计算；且多层卷积后读出是常见设计（readout 位置也是模型结构的一部分），提前池化就失去这种灵活性了。

## 5. 综合实践

把本讲四个模块串起来：**从构图到「每图一个定长向量」的完整前向**（示例代码；`BasicGraphConvolutionLayer`、`global_sum_pool` 从 [L195-L215](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L195-L215)、[L222-L226](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch18/ch18_part1.py#L222-L226) 原样复制）：

```python
import torch
from torch.nn.parameter import Parameter
torch.manual_seed(7)

# ── 第 1 步：构造两张 3~4 节点的小图，特征维度 f=3 ──────────────
# 图 A：4 节点链+1 边，X 自定义为任意 3 维特征
X_A = torch.tensor([[1., 0., 0.], [0., 1., 0.],
                    [0., 0., 1.], [1., 1., 0.]])
A_A = torch.tensor([[0., 1., 0., 1.],
                    [1., 0., 1., 0.],
                    [0., 1., 0., 1.],
                    [1., 0., 1., 0.]])
# 图 B：3 节点三角形
X_B = torch.tensor([[1., 0., 1.], [0., 1., 1.], [1., 1., 1.]])
A_B = torch.tensor([[0., 1., 1.], [1., 0., 1.], [1., 1., 0.]])

# ── 第 2 步：实例化图卷积层，打印前向输出形状 ──────────────────
conv = BasicGraphConvolutionLayer(3, 8)
print("图A:", conv(X_A, A_A).shape)   # 预期 torch.Size([4, 8])
print("图B:", conv(X_B, A_B).shape)   # 预期 torch.Size([3, 8])

# ── 第 3 步：仿 NodeNetwork 的前向（ReLU + 全连接）────────────
class MiniGNN(torch.nn.Module):
    def __init__(self, f_in):
        super().__init__()
        self.conv_1 = BasicGraphConvolutionLayer(f_in, 8)
        self.conv_2 = BasicGraphConvolutionLayer(8, 8)
        self.fc = torch.nn.Linear(8, 4)          # 图级分类头
    def forward(self, X, A, batch_mat):
        x = self.conv_1(X, A).clamp(0)           # ReLU
        x = self.conv_2(x, A).clamp(0)
        output = global_sum_pool(x, batch_mat)   # (图数, 8)
        return self.fc(output)                   # (图数, 4)

net = MiniGNN(3)

# 单图逐张前向：每张图得到一个 4 维向量
for name, X, A in [("图A", X_A, A_A), ("图B", X_B, A_B)]:
    out = net(X, A, None)
    print(name, "图向量形状:", out.shape)        # 均为 (1, 4)

# ── 第 4 步：拼成批（块对角 A + 成员矩阵 B），验证与单图一致 ────
X   = torch.cat([X_A, X_B], dim=0)               # (7, 3)
A   = torch.zeros(7, 7); A[:4, :4] = A_A; A[4:, 4:] = A_B
B   = torch.tensor([[1., 1., 1., 1., 0., 0., 0.],
                    [0., 0., 0., 0., 1., 1., 1.]])
out_batch = net(X, A, B)
print("批输出形状:", out_batch.shape)             # 预期 (2, 4)
print("图A 一致:", torch.allclose(net(X_A, A_A, None)[0], out_batch[0]))
print("图B 一致:", torch.allclose(net(X_B, A_B, None)[0], out_batch[1]))
```

**预期结果**（待本地验证）：

- 第 2 步打印 `torch.Size([4, 8])` 与 `torch.Size([3, 8])`——同一层、不同节点数，输出行数随图变、列数恒为 `f_out`；
- 第 3 步两张图的「图向量」形状都是 `(1, 4)`——节点数 4 与 3 消失在 `global_sum_pool` 里，**每张图得到一个固定维向量**；
- 第 4 步批输出 `(2, 4)`，两个 `allclose` 均为 `True`——块对角拼图后各图卷积互不干扰，批前向与单图前向等价。

若想再进一步，可把这个 MiniGNN 接上交叉熵损失与优化器，给两张图配上不同标签训练几个 epoch，观察图向量能否被拉开——这就逼近下一讲的真实图分类任务了。

## 6. 本讲小结

- 图用**双矩阵**表示：邻接矩阵 \( A \) 编码结构、节点特征矩阵 \( X \) 编码内容；两者行顺序必须一一对应，`nx.adjacency_matrix` 与 `get_node_attributes` 按同一节点顺序导出保证了这一点。
- 基本图卷积 \( H = AXW_2 + XW_1 + \mathbf{b} \)：`A @ X` 用 0/1 矩阵行视角实现「邻居特征求和」，\( W_2 \) 造消息、\( A \) 传消息、\( W_1 \) 保自身；层是节点级操作，输入输出行数都是节点数，且对任意大小的图共享同一组参数。
- `global_sum_pool` 用成员矩阵 \( B \) 左乘（`B @ X`）一次完成批内所有图的读出，把「每节点一行、行数不定」压成「每图一行、列数固定」，架起图卷积与全连接分类头之间的桥。
- 不规则图的批处理 = **块对角大邻接矩阵 + 沿节点维拼接的特征 + 0/1 成员矩阵**：图与图之间无边，卷积自动隔离在各图内部，因此批前向与单图前向数值一致（书中用 `torch.isclose` 验证）。
- 每个样本是一个大小不一的 dict 时，`ExampleDataset` 满足 `__len__`/`__getitem__` 最小契约即可，但 `DataLoader` 必须显式传 `collate_fn=collate_graphs` 替换默认堆叠。
- 阅读源码留了个心眼：书中 `dset[1]` 实际取的是 G2（变量名 `G1_rep` 有误导）；本实现未做度归一化，大度节点聚合值偏大，这些细节分别见验证代码与下一讲的 `GCNConv`。

## 7. 下一步学习建议

1. **进入 u18-l2（PyTorch Geometric 图分类实战）**：阅读 [ch18/ch18_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch18/ch18_part2.py)，对照本讲的 `collate_graphs` 理解 `torch_geometric` 的 `DataLoader` 与稀疏边表示 `edge_index`，并看 `GCNConv` 如何把本讲的求和聚合升级为归一化聚合。
2. **回头精读第 18 章书的文字部分**：尤其 "Other GNN layers and recent developments" 一节（谱图卷积、池化、归一化，见 [ch18/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch18/README.md#L20-L25) 的目录），本讲的 `BasicGraphConvolutionLayer` 是其中最简化的一档。
3. **动手实验建议**：在本讲综合实践的基础上，把 `global_sum_pool` 换成 mean 池化、把层数加到 3 层，分别观察对图向量数值范围的影响——直观感受「求和池化随图变大」与「深层过平滑」两个现象。
4. **串联复习**：不确定 `Parameter` 注册、Dataset 契约或 `clamp(0)`（ReLU）的细节时，分别回看 u13-l1、u12-l2 与 u12-l3；CNN 的「固定窗口局部聚合」对照可回看 u14-l1。
