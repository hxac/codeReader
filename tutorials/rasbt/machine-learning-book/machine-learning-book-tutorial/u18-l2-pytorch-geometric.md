# PyTorch Geometric 图分类实战

## 1. 本讲目标

上一讲（u18-l1）我们用纯 PyTorch 手写了图卷积层、全局求和池化和 `collate_graphs` 批处理。本讲把这三件事全部交给图学习专用库 **PyTorch Geometric（PyG）**，精读仓库脚本 `ch18/ch18_part2.py`，完成一个端到端的分子图机器学习项目。读完本讲你应该能：

1. 会加载 PyG 内置的 QM9 分子图数据集，并读懂 `Data` 对象里的每个字段。
2. 理解 `edge_index` 稀疏边表与上一讲稠密邻接矩阵 \( A \) 的对应关系。
3. 读懂 `ExampleNet` 中 `NNConv`（边条件图卷积）的结构，并能把它与手写的 `BasicGraphConvolutionLayer` 逐项对照。
4. 掌握 PyG 图级 `DataLoader` 的自动批处理：它替我们做了上一讲 `collate_graphs` 的全部工作。
5. 理解图级任务的输出头与损失计算，并说清楚「图级分类」与仓库实际实现的「图级回归」差在哪两行。

**先纠正三个预期**（以仓库真实代码为准，本讲全部基于真实源码）：

- 大纲里写的「ToxMoon 分子图分类」在仓库中并不存在。仓库实际加载的是 **QM9** 数据集（QM9 正是 MoleculeNet 基准套件中的分子数据集之一），任务是**回归**：预测分子的各向同性极化率（isotropic polarizability），损失是 MSE，评估指标是平均损失而不是准确率。
- `ExampleNet` 用的卷积层是 **`NNConv`**（带边特征的边条件卷积），不是 `GCNConv`。二者的关系会在 4.3 讲清：`NNConv` 与上一讲手写层的聚合方式（求和、不归一化）一一对应，`GCNConv` 是它的「度归一化」变体（本仓库未使用，作概念对照）。
- 因此「验证准确率」在本任务中应为「验证平均损失」；代码实践任务也据此适配（见第 5 节）。

## 2. 前置知识

本讲默认你已学完 u18-l1 与 u12-l2（Dataset/DataLoader 契约）。在此基础上补充三个新概念：

- **PyTorch Geometric（PyG）**：图深度学习最主流的 PyTorch 扩展库。它提供三类东西：图数据容器 `Data`、图神经网络层（`NNConv`、`GCNConv` 等）、能批处理「大小不一的图」的 `DataLoader`。它不在本书的 `environment.yml` 里，需要单独安装：`pip install torch_geometric`。仓库代码的版本检查要求 `torch_geometric >= 2.0.2`（notebook 实跑版本为 2.0.3）。
- **分子的图表示**：分子天然是图——原子是节点、化学键是边。QM9 是一个收录了 130,831 个小分子（最多 9 个重原子）的量子化学数据集，每个分子附带 19 个由 DFT 计算得到的物性标签（偶极矩、极化率、HOMO/LUMO 等）。它是 MoleculeNet 基准（Wu et al., 2017）中的回归任务数据集。
- **`Data` 对象**：PyG 用一个类字典对象存一张图，属性都是张量：节点特征 `x`、边表 `edge_index`、边特征 `edge_attr`、标签 `y` 等。它继承自 u12-l2 讲过的 Dataset 契约之上——`QM9` 数据集的每个样本就是一个 `Data` 对象。

一句话定位：**上一讲我们手搓的零件（边表批处理、消息传递、读出池化），PyG 都造好了轮子；本讲学的是「轮子怎么用」，以及轮子内部对应我们手搓的哪一段。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch18/ch18_part2.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py) | 本讲主源码：用 PyG 加载 QM9、定义 `ExampleNet`、训练/验证/测试分子极化率回归 |
| [ch18/ch18_part2.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.ipynb) | 同一份代码的 notebook 形态，保留了真实运行输出（数据集规模、模型结构、训练日志），本讲引用的输出全部来自这里 |
| [ch18/ch18_part1.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py) | 上一讲的手写实现，本讲作为对照：`BasicGraphConvolutionLayer`、`global_sum_pool`、`collate_graphs` |
| [ch18/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/README.md) | 第 18 章目录：part1 手写 GNN，part2「Implementing a GNN using the PyTorch Geometric library」 |

## 4. 核心概念与源码讲解

### 4.1 QM9 数据集加载与 Data 对象

#### 4.1.1 概念说明

PyG 内置了 MoleculeNet 系列数据集，`QM9` 是其中的回归任务：加载后得到 130,831 个分子图，每个图自带节点特征（原子属性）、边特征（化学键属性）和 19 个物性标签。理解本讲的第一个关卡是读懂单个样本 `Data` 对象的打印——它把「一张图需要哪些张量」一次摊开给你看。

#### 4.1.2 核心流程

```text
QM9('.')
  ├── 首次运行：从远程下载并解压到当前目录 '.'，之后直接读本地缓存
  └── 返回 dataset：长度 130,831，dset[i] 取出第 i 个分子的 Data 对象
        Data 对象字段（以第 1 个分子为例）：
        x          [5, 11]   5 个原子 × 11 维节点特征
        edge_index [2, 8]    8 条有向边（COO 稀疏边表）
        edge_attr  [8, 4]    每条边 4 维边特征
        y          [1, 19]   19 个物性标签
        pos        [5, 3]    原子三维坐标
        z          [5]       原子序数
```

`Data` 还是「动态容器」：可以随时挂新属性、可以整体搬运到 GPU。

#### 4.1.3 源码精读

QM9 的导入与加载只有两行——[ch18/ch18_part2.py:L9-L11](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L9-L11) 从 PyG 导入数据集类、图级 DataLoader 和两个网络组件；[ch18/ch18_part2.py:L69-L70](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L69-L70) 下载数据集并查看规模，notebook 中的真实输出是 `130831`。

取出第一个分子并打印——[ch18/ch18_part2.py:L75-L87](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L75-L87)：`data.z` 打印出 `tensor([6, 1, 1, 1, 1])`——1 个碳（原子序数 6）加 4 个氢，即甲烷（CH₄）；`data.new_attribute = torch.tensor([1, 2, 3])` 演示了 `Data` 可以随时挂载自定义属性，打印 `data` 时新属性会出现在末尾。

设备搬运——[ch18/ch18_part2.py:L92-L94](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L92-L94)：`data.to(device)` 把 `Data` 里**所有**张量一次性搬到 GPU（有 GPU 选 `cuda:0`，否则 CPU），随后 `data.new_attribute.is_cuda` 返回 `True` 验证搬运成功。这与 u13-l2 讲过的「张量携带 device 信息」一致，只是 PyG 把遍历工作封装进了 `Data.to()`。

#### 4.1.4 代码实践

1. **实践目标**：亲手把玩 QM9 的第一个分子，建立对 `Data` 字段的直觉。
2. **操作步骤**（示例代码，需已安装 torch_geometric）：

   ```python
   from torch_geometric.datasets import QM9
   dset = QM9('.')
   print(len(dset))          # 预期 130831
   data = dset[0]
   print(data)               # 对照 notebook 输出
   print(data.z, data.x.shape, data.edge_index.shape)
   ```

3. **需要观察的现象**：`len(dset)` 是否为 130831；`data` 打印是否与 notebook 一致：`Data(x=[5, 11], edge_index=[2, 8], edge_attr=[8, 4], y=[1, 19], pos=[5, 3], idx=[1], name='gdb_1', z=[5])`。
4. **预期结果**：一致；且 `data.edge_index.shape[1] // 2 = 4` 恰好等于甲烷的 C–H 键数（每条无向键存两条有向边）。
5. 首次运行需要下载数据集、具体数值与耗时**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么甲烷只有 4 条化学键，`edge_index` 的形状却是 `[2, 8]`？
**答案**：QM9 按有向边存储：每条无向键 (i, j) 同时存 `(i→j)` 和 `(j→i)` 两个方向，4 条键 × 2 = 8 条有向边；第一行是起点、第二行是终点，各 8 个元素。

**练习 2**：`data.to(device)` 之后，`data.x` 与你另外定义的普通张量 `t = torch.ones(3)` 在设备上有什么差别？
**答案**：`data.x` 已在 GPU 上（`Data.to()` 递归搬运所有属性张量），而 `t` 仍在 CPU——设备信息跟着张量本身走，两者相加会报设备不匹配错误，需 `t.to(device)`。

### 4.2 edge_index 稀疏边表示与邻接矩阵的对应

#### 4.2.1 概念说明

上一讲用稠密邻接矩阵 \( A \in \{0,1\}^{N \times N} \) 表示图结构，「左乘 \( A \) 即按行求和聚合邻居」。PyG 改用 **COO 稀疏边表** `edge_index`（形状 `[2, |E|×2]`）：只存「哪两个点之间有边」，不存 \( N \times N \) 的大矩阵。分子图极度稀疏——存储从 \( O(N^2) \) 降到 \( O(|E|) \)，消息传递也只需遍历边表做 scatter-add，不必做稠密矩阵乘。

#### 4.2.2 核心流程

两种表示的等价性（第 \( i \) 个节点聚合邻居）：

\[ \text{稠密视角：} \quad (A X)_i = \sum_{j} A_{ij}\, x_j \qquad\Longleftrightarrow\qquad \text{稀疏视角：} \quad \sum_{j \in \mathcal{N}(i)} x_j \]

批处理时二者也一一对应：上一讲手工把各图的 \( A \) 沿对角线拼成块对角大矩阵；PyG 则把各图的 `edge_index` 直接首尾拼接，并把第 k 张图的边端点编号**加上前面所有图的节点总数**作偏移——数学上与块对角矩阵完全等价，但只需加法不需构造大矩阵。

#### 4.2.3 源码精读

边表来自数据本身：`data = dset[0]` 的 `edge_index=[2, 8]`（见 [ch18/ch18_part2.py:L75-L76](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L75-L76)，真实输出见上一节）。

作为对照，上一讲的稠密路线在 [ch18/ch18_part1.py:L250-L266](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L250-L266)：`collate_graphs` 先统计各图节点数，再用 [L261-L266](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L261-L266) 的双重循环把每个邻接矩阵写进 `batch_adj` 的对角块。而本讲 forward 里传入的 `edge_index`（[ch18/ch18_part2.py:L114](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L114)）由 PyG 的 DataLoader 自动完成「拼接 + 偏移」（见 4.4），不再需要任何手工拼块代码。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到两种表示的规模差异。
2. **操作步骤**（示例代码）：对甲烷分别计算稠密与稀疏存储的元素数。

   ```python
   import numpy as np
   N, E_directed = 5, 8
   print(N * N, 2 * E_directed)   # 稠密 A 的元素数 vs 稀疏边表的元素数
   ```

3. **需要观察的现象**：输出 `25` 和 `16`。
4. **预期结果**：对更大的分子（QM9 最多 29 个原子），稠密要 841 个元素、稀疏仍只与键数成正比；图越稀疏，稀疏表示越省。
5. 本实践可纯手算验证，无需运行环境。

#### 4.2.5 小练习与答案

**练习**：批处理两个图（图 a 有 3 个节点，图 b 有 4 个节点），图 b 内有一条边 `(1, 2)`（b 的局部编号）。拼批后这条边在 `edge_index` 中应写成什么？
**答案**：`(1+3, 2+3) = (4, 5)`——图 b 的所有端点编号都要加上图 a 的节点数 3。若不偏移，这条边会错误地连到图 a 的节点上，这正是上一讲块对角矩阵「块外全零」所避免的事。

### 4.3 ExampleNet：NNConv 边条件图卷积

#### 4.3.1 概念说明

`ExampleNet` 的两个卷积层用的是 `NNConv`——来自论文 *Neural Message Passing for Quantum Chemistry*（Gilmer et al., 2017，即 MPNN）的边条件卷积。上一讲的 `BasicGraphConvolutionLayer` 里，所有边共用同一个变换矩阵 \( W_2 \)；`NNConv` 把它升级为**每条边一个专属矩阵**，由该边的特征经一个小网络生成：

\[ x'_i = W_1 x_i \; + \sum_{j \in \mathcal{N}(i)} \eta(e_{ij})\, x_j \]

- \( W_1 x_i \)：自身项，对应手写层的 `root_update`；
- \( \eta(e_{ij}) \)：把 4 维边特征（键的类型等）映射为 \( d_{in} \times d_{out} \) 矩阵的小神经网络，即「这条边该如何转译邻居的消息」——单键、双键、芳香键对消息的加工方式各不相同，这正是分子任务需要的归纳偏置；
- 求和聚合（`aggr=add`）：与手写层一样**不按度归一化**。

顺带把概念补全：`GCNConv`（Kipf & Welling）与 `NNConv` 同属消息传递家族，区别是把求和换成按节点度的对称归一化 \( \sum_j \frac{x_j}{\sqrt{d_i d_j}} \)，且不使用边特征。上一讲遗留的「求和聚合未归一化」问题，`GCNConv` 给出了库内答案；但仓库本文件没有使用它。

#### 4.3.2 核心流程

```text
ExampleNet(num_node_features=11, num_edge_features=4)
  conv1: NNConv(11 → 32)   边网络 conv1_net: Linear(4→32) → ReLU → Linear(32→ 11*32=352)
  conv2: NNConv(32 → 16)   边网络 conv2_net: Linear(4→32) → ReLU → Linear(32→ 32*16=512)
  fc_1:  Linear(16 → 32)
  out:   Linear(32 → 1)     # 图级回归：每张图输出 1 个标量（极化率）

forward(data):
  x = ReLU(conv1(x, edge_index, edge_attr))   # 节点表示 11 维 → 32 维
  x = ReLU(conv2(x, edge_index, edge_attr))   # 32 维 → 16 维
  x = global_add_pool(x, batch)               # 每张图读出为 1 个 16 维向量
  x = ReLU(fc_1(x))                           # 16 → 32
  output = out(x)                             # 32 → 1（每图一个预测值）
```

维度链条的三个耦合点：边网络最后一层必须输出 \( d_{in} \times d_{out} \)（352 = 11×32，512 = 32×16）；`fc_1` 的输入必须等于 `conv2` 的输出维；改任何一处，其余两处都要联动。

#### 4.3.3 源码精读

模型定义在 [ch18/ch18_part2.py:L99-L111](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L99-L111)：两个 `nn.Sequential`（[L102-L107](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L102-L107)）就是公式里的 \( \eta \)，末层 Linear 的输出宽度分别写成 `num_node_features*32` 与 `32*16`，显式暴露了「边特征 → 逐边 \( d_{in} \times d_{out} \) 矩阵」的设计；[L108-L111](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L108-L111) 把两个 `NNConv`（11→32、32→16）与 `fc_1`、`out` 两个全连接组装起来。

前向传播在 [ch18/ch18_part2.py:L113-L120](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L113-L120)：[L114](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L114) 一次性从 `Data` 对象解包出 `batch / x / edge_index / edge_attr` 四个张量；[L115-L116](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L115-L116) 两层「卷积 + ReLU」；[L117](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L117) `global_add_pool(x, batch)` 是 PyG 版的全局求和池化，与上一讲手写的 [global_sum_pool](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L222-L226) 等价（4.4 详述）；[L118-L120](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L118-L120) 是全连接分类头，输出每图 1 个标量。

notebook 真实打印的模型结构印证了维度耦合：`conv1` 的边网络末层 `Linear(in_features=32, out_features=352)`、`conv2` 的末层 `out_features=512`，且两个 `NNConv` 都标注 `aggr=add`（求和聚合、无归一化）。

对照上一讲：手写层 [ch18/ch18_part1.py:L195-L215](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L195-L215) 的 `forward` 是 `A·(X·W2) + X·W1 + bias`——`NNConv` 的 \( W_1 x_i \) 对应 `root_update`、\(\sum_j \eta(e_{ij}) x_j\) 对应 `propagated_msgs`（把共用的 \( W_2 \) 换成逐边的 \( \eta(e_{ij}) \)）；整体网络骨架也与 [NodeNetwork](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L168-L184) 的「两层卷积 → 池化 → 全连接 → 输出头」同构。

#### 4.3.4 代码实践

1. **实践目标**：用参数计数直观感受「改卷积宽度会牵动哪几处」。
2. **操作步骤**（示例代码，仅实例化模型、不训练）：

   ```python
   import torch
   from torch_geometric.nn import NNConv
   m = NNConv(11, 32, torch.nn.Linear(4, 11*32))
   print(sum(p.numel() for p in m.parameters()))
   ```

3. **需要观察的现象**：打印的参数量中，边网络贡献了 `4×352 + 352 + 352`（权重+偏置+逐边矩阵生成层）这类与 352 成正比的大头。
4. **预期结果**：边网络的参数量随 \( d_{in} \times d_{out} \) 增长——把 conv1 加宽到 64 会让末层变成 `Linear(32, 704)`，参数近似翻倍。具体数值**待本地验证**。
5. 此实践只需 CPU 与 torch_geometric，秒级完成。

#### 4.3.5 小练习与答案

**练习 1**：`conv1_net` 最后一层为什么是 `Linear(32, num_node_features*32)`，即输出 352 维？
**答案**：`NNConv` 要求边网络为每条边生成一个 \( d_{in} \times d_{out} \) 矩阵；conv1 是 11→32，矩阵含 11×32=352 个元素，PyG 内部把它 reshape 成 `[E, 11, 32]` 再与邻居特征相乘。

**练习 2**：把 `NNConv` 换成 `GCNConv(11, 32)` 需要删掉模型里的什么、又少了什么能力？
**答案**：删掉两个边网络 `conv1_net/conv2_net` 与 `edge_attr` 的传入（`GCNConv` 只吃 `x, edge_index`）；代价是丢失边条件——单键与双键将被同等对待。分子任务里键类型很关键，所以仓库选了 `NNConv`。（此变体在仓库中不存在，属示例改造。）

### 4.4 图级 DataLoader 与自动批处理

#### 4.4.1 概念说明

u12-l2 讲过：普通 `DataLoader` 靠 `__len__`/`__getitem__` 取样本、再堆叠成批。但图的大小不一（甲烷 5 个原子、别的分子二三十个原子），节点矩阵无法直接堆叠。上一讲为此手写了 `collate_graphs`；PyG 的 `DataLoader` 内置了图版 collate：**节点特征直接沿第 0 维拼接、边表拼接并偏移、再附带一个 `batch` 向量标记每个节点属于哪张图**。我们什么 collate 都不用写。

#### 4.4.2 核心流程

```text
DataLoader(train_set, batch_size=32, shuffle=True) 迭代出的每个 batch 是一个大 Data 对象：
  x          [ΣN_i, 11]     32 张图的节点特征纵向拼接（ΣN_i = 各图节点数之和）
  edge_index [2, ΣE_i]      各图边表拼接，端点已按累计节点数偏移（块对角等价）
  edge_attr  [ΣE_i, 4]      边特征随之拼接
  y          [32, 19]       每图 y=[1,19] 沿第 0 维堆叠 → 可直接按列取目标
  batch      [ΣN_i]         每个节点的「所属图编号」，即池化的分组依据
  num_graphs                图数（=32）
```

上一讲 `get_batch_tensor` 造的 0/1 成员矩阵 \( B \)，在 PyG 里被压成了一维 `batch` 向量；`global_add_pool(x, batch)` 等价于 \( B X \)（scatter-add 版本）。

#### 4.4.3 源码精读

数据划分在 [ch18/ch18_part2.py:L127](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L127)：`random_split(dset, [110000, 10831, 10000])` 复用 PyTorch 原生的随机切分（110000+10831+10000 = 130831，覆盖全部数据）。

三个加载器在 [ch18/ch18_part2.py:L129-L131](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L129-L131)：PyG 的 `DataLoader`（从 `torch_geometric.loader` 导入，见 [L10](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L10)）用法与 u12-l2 的普通 DataLoader 完全同名同参——`batch_size=32`、`shuffle=True`——但 collate 逻辑换成了图版拼接。

对照上一讲：[ch18/ch18_part1.py:L347](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L347) 当时必须显式传 `collate_fn=collate_graphs`，而本讲一行都不用传；池化侧，手写 [global_sum_pool](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part1.py#L222-L226) 用成员矩阵乘法，本讲 [L117](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L117) 的 `global_add_pool(x, batch)` 用 `batch` 向量做同样的求和——同一个数学对象（\( B X \)）的两种实现。

#### 4.4.4 代码实践

1. **实践目标**：解剖一个真实 batch，验证拼接与偏移。
2. **操作步骤**（示例代码）：

   ```python
   from torch.utils.data import random_split
   from torch_geometric.loader import DataLoader
   train_set, _, _ = random_split(dset, [110000, 10831, 10000])
   loader = DataLoader(train_set, batch_size=4, shuffle=False)
   b = next(iter(loader))
   print(b.num_graphs, b.x.shape, b.edge_index.shape, b.batch.shape, b.y.shape)
   print(b.edge_index.max())   # 不得超过 ΣN_i - 1
   ```

3. **需要观察的现象**：`num_graphs=4`；`x` 第一维等于 4 个分子节点数之和；`y` 形状为 `[4, 19]`；`edge_index.max() < b.x.shape[0]`。
4. **预期结果**：全部成立即证明「拼接 + 偏移 + batch 向量」三件事确实自动完成。具体数值随前 4 个分子而定，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `y` 批后形状是 `[32, 19]`，而 `x` 不是 `[32, N, 11]`？
**答案**：每张图的 `y` 都是 `[1, 19]`，等长，可沿第 0 维堆叠；各图节点数 N 不同，堆成 `[32, N, 11]` 需要 N 相同，故只能拼接成 `[ΣN, 11]` 并用 `batch` 向量区分归属。

**练习 2**：如果 shuffle=False 且 QM9 恰好按分子大小排序，训练会出什么问题？
**答案**：同一批内的图大小高度相近，各批的梯度分布随之系统性漂移，类似 u2-l3 讲过的「按类排序导致震荡」，所以代码对三个 loader 都设了 `shuffle=True`（测试集虽然概念上无需 shuffle，这里也保持一致）。

### 4.5 训练/验证循环与图级回归的输出与损失

#### 4.5.1 概念说明

这是全书五步训练循环（u12-l3）在图数据上的落地：`optimizer.zero_grad() → 前向 → 损失 → loss.backward() → optimizer.step()`。任务输出头是「每图一个标量」：`out = Linear(32, 1)` 后经池化得到 `[B, 1]`，与从 `batch.y` 取出的第 1 列目标做 MSE——这就是**图级回归**。若要做图级分类（如大纲设想的分子毒性二分类），只需两处改动：`out` 改成 `Linear(32, num_classes)`、损失换成 `CrossEntropyLoss`（对应上一讲 `NodeNetwork` 的 `Linear(16, 2)` + softmax 结构）。

#### 4.5.2 核心流程

```text
每个 epoch：
  net.train()
  遍历 trainloader：batch.to(device) → 五步循环 → 累计 loss.item() 与 batch.num_graphs
  train_avg_loss = epoch_loss / total_graphs
  net.eval()（无 backward、无 optimizer）
  遍历 validloader：纯前向 → 累计 → val_avg_loss
  打印两条平均损失

训练完毕：net.eval() + 遍历 testloader，收集 predictions/real，
用散点图直观检查预测值与真实极化率的相关性
```

回归目标的取出有一个形状细节：`batch.y` 是 `[32, 19]`，`batch.y[:, 1]` 取出极化率列得到 `[32]`，必须 `unsqueeze(1)` 变成 `[32, 1]` 才能与网络输出 `[32, 1]` 对齐做 MSE。

#### 4.5.3 源码精读

超参与目标选取在 [ch18/ch18_part2.py:L136-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L136-L142)：`qm9_node_feats, qm9_edge_feats = 11, 4` 与 4.1 的 Data 字段对应；优化器 Adam、`lr=0.01`；`target_idx = 1` 注释明确写着「各向同性极化率标签的下标」。（顺带一提：`epochs = 4` 在 [L137](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L137) 与 [L141](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L141) 赋了两次值，冗余无害。）

训练主循环在 [ch18/ch18_part2.py:L153-L181](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L153-L181)：[L158-L166](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L158-L166) 训练批循环——`batch.to(device)` 整批上卡、五步训练、`output = net(batch)` 把整个 Data 对象直接喂进 forward、损失在 [L162](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L162) 用 `F.mse_loss(output, batch.y[:, target_idx].unsqueeze(1))` 完成「取列 + 升维 + MSE」；[L171-L178](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L171-L178) 验证循环只有前向与累计（`net.eval()` 且无 backward/step）；[L181](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L181) 打印日志。notebook 真实训练日志为：`0.42/0.15 → 0.14/0.14 → 0.11/0.08 → 0.10/0.19`（训练损失稳步下降，验证损失末轮回升——4 轮内就出现了轻微过拟合迹象）。

测试评估在 [ch18/ch18_part2.py:L186-L197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L186-L197)：`net.eval()` 后逐批前向，`detach().cpu().numpy()` 把预测与真值搬回 CPU 收集、`np.concatenate` 拼成一维数组；[L204-L206](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L204-L206) 取前 500 个样本画「真实极化率 vs 预测极化率」散点图——点越贴近对角线，回归越好。这是回归任务的标配可视化，对应分类任务的混淆矩阵。

**一处值得辨析的细节**：`F.mse_loss` 默认 `reduction='mean'`，返回的是**批内均值**；[L168](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L168) 把各批均值求和后除以的是**图数**而非批数。设批大小为 B、共 m 批，则打印值 \( \approx \frac{1}{mB}\sum_{k=1}^{m} \text{MSE}_k = \frac{1}{B}\overline{\text{MSE}} \)，即真实逐图 MSE 的约 1/32（训练与验证都用 batch_size=32，趋势对比仍成立，但绝对数值被整体缩小了约 32 倍）。

#### 4.5.4 代码实践

1. **实践目标**：跑通 1 个 epoch，读懂日志里两个数字的含义。
2. **操作步骤**：在 GPU 或耐心足够的 CPU 上运行 [L153-L181](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/ch18_part2.py#L153-L181) 的循环（可先把 `epochs` 改为 1、或用 4.4 实践的小数据子集训练）。
3. **需要观察的现象**：每个 epoch 打印一行「epoch avg. loss | validation avg. loss」；验证损失与训练损失同量级。
4. **预期结果**：趋势与 notebook 日志类似（首轮训练损失明显高于其后各轮）；QM9 全量 11 万图训练 1 epoch 在 CPU 上可能需要很长时间，耗时**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：把 `batch.y[:, target_idx].unsqueeze(1)` 的 `unsqueeze(1)` 去掉会怎样？
**答案**：目标形状变为 `[32]` 而输出是 `[32, 1]`，`F.mse_loss` 会触发广播——`[32, 1]` 与 `[32]` 相减广播成 `[32, 32]`，即把第 i 个预测同时与全部 32 个真值比对，损失语义完全错误，且通常不报错（静默 bug）。

**练习 2**：训练循环与验证循环在代码上差哪三件事？
**答案**：① `net.train()` vs `net.eval()`（本模型无 dropout/BN，影响不大但属规范）；② 验证无 `loss.backward()` 与 `optimizer.step()`；③ 验证无需 `optimizer.zero_grad()`。本质上验证只是「批量前向 + 统计」。

**练习 3**：若把任务改成分子二分类（标签取自 19 列中的某离散列），模型与损失各改哪里？
**答案**：`self.out = nn.Linear(32, 1)` 改为 `nn.Linear(32, 2)`，损失 `F.mse_loss` 换成 `F.cross_entropy`（目标无需 unsqueeze，取 `[32]` 的长整型标签），评估从 MSE/散点图换成准确率/混淆矩阵。（仓库未含此变体，属示例改造。）

## 5. 综合实践

**任务：改造 ExampleNet 的卷积宽度，做一次受控对比实验。**

原实践设想「把两个 GCNConv 输出维度改成 [32, 16]」，但仓库代码的卷积层本来就是 `NNConv` 且已是 32→16（见 4.3），故本实践改为扫描另一组宽度并观察影响，操作要点完全一致：**改宽度 → 联动改边网络末层与全连接输入 → 重训 → 比验证损失**。

1. **实践目标**：亲手体会 4.3 提出的「三个维度耦合点」，并观察网络容量对图级回归验证损失的影响。

2. **操作步骤**（示例代码，在 ch18_part2.py 基础上修改）：

   ```python
   def make_net(h1, h2):
       """按指定隐藏宽度构造 ExampleNet 变体（示例代码）"""
       net = ExampleNet_variant(11, 4, h1, h2)   # 需把 32/16 替换为 h1/h2
       return net

   for h1, h2 in [(16, 8), (32, 16), (64, 32)]:
       # 1) conv1_net 末层: Linear(32, 11*h1)   如 (16,8)→176, (64,32)→704
       # 2) conv2_net 末层: Linear(32, h1*h2)   如 (16,8)→128, (64,32)→2048
       # 3) conv2 = NNConv(h1, h2, conv2_net)
       # 4) fc_1 = nn.Linear(h2, 32)
       # 记录参数量: sum(p.numel() for p in net.parameters())
       # 用相同的 random_split 种子与 loader 训练 2 个 epoch，记录 val_avg_loss
   ```

   三组改动清单（以 (64, 32) 为例）：`conv1_net` 末层改 `Linear(32, 11*64)`；`conv2_net` 末层改 `Linear(32, 64*32)`；`NNConv(64, 32, conv2_net)`；`fc_1 = nn.Linear(32, 32)`。对照 (16, 8)：末层分别是 `Linear(32, 176)`、`Linear(32, 128)`、`NNConv(16, 8)`、`fc_1 = nn.Linear(8, 32)`。注意 `random_split` 无固定种子的默认划分每次运行不同，严格对比应给 `torch.manual_seed(0)` 后再切分，或三组共用同一份 `train_set/valid_set`。

3. **需要观察的现象**：① 参数量随 h1×h2 显著变化（边网络末层是大头）；② 训练日志中三组的验证损失走势；③ 宽度大训练损失更低、验证损失未必更低。

4. **预期结果**：容量增大 → 训练平均损失下降更快，验证平均损失可能持平甚至变差（过拟合），且每 epoch 耗时上升。具体数值**待本地验证**；QM9 全量训练开销大，CPU 环境建议改用小子集（例如 `random_split(train_set, [5000, 105000])` 后只取前 5000）并将 epoch 减为 2，此时结论方向仍具参考性。

5. **选做延伸**：把 `shuffle=True` 改为 `False` 重跑一组，观察训练损失曲线是否出现震荡，验证 4.4 练习 2 的推断（待本地验证）。

## 6. 本讲小结

- 仓库第 18 章 part2 的真实实现是 **QM9 上的图级回归**（预测各向同性极化率、MSE 损失），不是大纲所写的 ToxMoon/Tox21 分类；QM9 本身就是 MoleculeNet 基准中的分子数据集，图级分类只需把输出头改为 `Linear(32, num_classes)` 并换交叉熵损失。
- `Data` 对象用一个类字典容器装下整张图：`x/edge_index/edge_attr/y/pos/z`，支持随时挂新属性、`to(device)` 整体搬设备。
- `NNConv` 把手写层中共享的 \( W_2 \) 升级为「由边特征生成的逐边矩阵 \( \eta(e_{ij}) \)」，公式 \( x'_i = W_1 x_i + \sum_{j\in\mathcal{N}(i)} \eta(e_{ij}) x_j \)，求和聚合与手写层一致、不做度归一化；`GCNConv` 是加度归一化、不用边特征的同类层（仓库未使用）。
- PyG `DataLoader` 自动完成上一讲 `collate_graphs` 的全部工作：拼接 `x`、偏移 `edge_index`、堆叠 `y`、生成 `batch` 向量；`global_add_pool(x, batch)` 等价于手写的成员矩阵乘 \( B X \)。
- 训练循环仍是五步骨架；回归目标取出要用 `batch.y[:, target_idx].unsqueeze(1)` 对齐 `[B, 1]`，散点图是回归的标配诊断；打印的平均损失实为逐图 MSE 的约 1/32（批均值求和后误除了图数），趋势对比有效、绝对值偏小。

## 7. 下一步学习建议

本讲结束了第 18 章与图神经网络单元。建议两件事：

1. **回看官方实现细节**：把 PyG 文档中 `NNConv`、`GCNConv`、`global_add_pool` 的说明页对照本讲读一遍，重点看 `GCNConv` 的归一化聚合如何回应 u18-l1 遗留的「求和未归一化」问题；书中本章末尾的「Spectral graph convolutions / Pooling / Normalization」小节（见 [ch18/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch18/README.md) 目录与 notebook 引用的 `figures/18_13.png`）给了图卷积谱视角与更丰富池化、归一化方法的文献指引。
2. **进入最后一个单元——强化学习（u19-l1）**：将回到「表格型 Q-learning」这种无需梯度的小规模世界，先理解 agent 与环境的交互循环，再看 u19-l2 如何用本课程一路走来的 PyTorch 神经网络把 Q 表换成函数近似。若想继续深挖 GNN，可以尝试在 QM9 的其余 17 个标签（如 HOMO/LUMO）上复用本讲的训练管线做迁移练习。
