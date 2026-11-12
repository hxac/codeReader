# 图神经网络入门

## 1. 本讲目标

学完本讲，你应该能够：

1. 掌握**把图像构造成图数据**的思路：28×28 的 MNIST 数字就是一个 784 个节点的图，每个像素是一个节点，像素灰度是节点特征，像素间的空间邻近关系由邻接矩阵刻画。
2. 精读仓库中最基础的高斯滤波 GNN：邻接矩阵如何用**欧氏距离 + 高斯权重 + 截断 + 对称归一化**四步预计算出来，前向传播如何用 `torch.bmm` 一次完成「邻居加权求和」。
3. 看穿这个模型的一个有趣本质：它**没有任何非线性激活层**，整体仍是一个线性分类器——这解释了它 91.63% 的测试准确率为何与本手册 u2-l2 的 softmax 回归（91.77%）几乎相同。
4. 理解两个进阶变体：用小网络**预测边**（把手工设计的高斯邻接矩阵换成可学习的），以及基于**谱图卷积**（拉普拉斯特征分解 + 图傅里叶变换）的实现路线。
5. 延续本手册「读码以代码为准」的传统：本讲的两个 notebook 里各藏着一处 markdown 与代码不一致、以及一处 `nn.Parameter` 却注册成 buffer 的真实细节，我们会逐一拆穿。

## 2. 前置知识

**什么是图？** 图 \( G=(V, E) \) 由节点集合 \( V \) 和边集合 \( E \) 组成。图数据在深度学习里无处不在：社交网络（用户是节点、关注是边）、分子（原子是节点、化学键是边）、推荐系统（用户与商品是节点）。刻画一个 \( N \) 节点图最直接的容器是 **\( N \times N \) 邻接矩阵 \( A \)**：\( A_{ij} \) 表示节点 \( i \) 与 \( j \) 之间边的强度（0 表示无边）。节点自身的信息存成 **特征矩阵 \( X \)**（形状 \( N \times C \)，每行是一个节点的 \( C \) 维特征）。本讲只需这两个对象。

**CNN 的两种本质（承接 u3-l1）。** u3-l1 讲过卷积的两个关键性质：**局部连接**（每个输出位置只看输入的一小片邻域）与**权值共享**（同一个核滑过全图）。换个角度看：卷积核其实就是一个**预先固定的邻接关系 + 预先固定的邻域权重**——3×3 卷积把每个像素与它周围 8 个邻居连边，权重就是 9 个核参数。本讲的三个 notebook 正是这个视角的展开：高斯滤波 GNN 用**手工设计的**邻域权重；edge 版把权重换成**可学习的**；谱图卷积版则从另一种数学（频率域）重新推导「图上的卷积」。

**没有激活层的网络是线性的（承接 u2-l3）。** u2-l3 强调过：没有 ReLU 等非线性，再深的线性堆叠也坍缩为单层线性模型。本讲的高斯滤波 GNN 恰好是一个**故意只用线性运算**的模型，这让它成为一个绝佳的「可控实验」：我们会推导它与 softmax 回归在数学上几乎同构。

**几个术语：**

- **度（degree）**：节点连边强度的总和 \( D_i = \sum_j A_{ij} \)，衡量一个节点的「邻居总量」。
- **拉普拉斯矩阵（graph Laplacian）**：\( L = I - D^{-1/2} A D^{-1/2} \)，图的「二阶差分算子」，刻画图的平滑程度；它的特征向量是图上的「振动模式」（频率基）。
- **消息传递（message passing）**：GNN 的一层 = 每个节点收集邻居的特征并加权聚合（「传消息」），再变换自己。本讲模型的一层就是 \( X^{(l+1)} = A X^{(l)} W^{(l)} \)。
- **buffer（缓冲区）**：`register_buffer` 登记的张量会随模型一起 `.to(device)`、一起存进 `state_dict`，但**不是参数、不参与训练**——与 `nn.Parameter` 相对。这个区别在本讲 4.3 节会变成一个大坑。

## 3. 本讲源码地图

本讲涉及三个 notebook（均为 Jupyter JSON 文件，**下文所有行号均指 `.ipynb` 原始 JSON 文件中的行号**，与 GitHub 页面左侧行号一致）：

| 文件 | 作用 | 关键内容 |
|---|---|---|
| `pytorch_ipynb/gnn/gnn-basic-1.ipynb` | 主讲义：手工高斯滤波建图的最简 GNN | `precompute_adjacency_matrix`、对称归一化、`torch.bmm` 消息传递；SGD 训练 20 epoch，测试 91.63% |
| `pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb` | 姊妹篇：把手工邻接矩阵换成小网络预测的边 | `make_coordinate_array`、`pred_edge_fc`（Linear→ReLU→Linear→Tanh）；Adam 训练 50 epoch，测试 90.36% |
| `pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb` | 进阶：谱图卷积版 | `get_graph_laplacian`、`eigsh` 特征分解、图傅里叶域滤波；测试 84.55% |

三者的关系是一次逐步替换：**同一份骨架（MNIST 数据管线 + 单层图卷积 + 线性分类头 + 五步训练循环），只换「图怎么来 / 卷积怎么算」这一处**——正是 u2-l4 以来反复使用的「克隆基线、只改一处」受控对比法。三个 notebook 都源自 Boris Knyazev 的图神经网络教程（notebook 顶部 markdown 给出了链接）。

## 4. 核心概念与源码讲解

### 4.1 像素图构建：把 MNIST 图像变成图

#### 4.1.1 概念说明

图像在 PyTorch 里是 `[B, C, H, W]` 张量（u3-l1 的 NCHW 约定），CNN 对它做滑窗卷积。要换成图的视角，需要回答三个问题：

1. **节点是什么？** 28×28 的每个像素各算一个节点，共 \( N = 28 \times 28 = 784 \) 个。
2. **节点特征是什么？** 像素灰度本身（`ToTensor` 后已在 [0,1] 区间），即 \( X \in \mathbb{R}^{784 \times 1} \)，每个节点一维特征。
3. **边是什么？** 这是唯一需要设计的地方。直觉：相邻像素相关性最强，距离越远权重越小。于是用**欧氏距离 + 高斯型衰减**给每一对像素赋权——这就是标题里的「高斯滤波」。

为什么值得这样做？因为「邻居加权平均」正是对图像做**模糊滤波**（类似高斯模糊），而模糊后的像素再过线性分类器，等价于让分类器看「局部纹理平均」而非单点灰度——一种手工版的特征提取。这套表示法一旦成立，换数据时只需换掉「边怎么定义」，模型代码完全不动：社交网络、分子、交通网都能塞进同一个 `GraphNet`，这正是 GNN 作为通用框架的卖点。

#### 4.1.2 核心流程

建图函数 `precompute_adjacency_matrix(img_size)` 的四步流水线：

```text
① 坐标表：meshgrid 生成 784 个像素的 (col,row) 坐标，除以 img_size 归一化到 [0,1)
② 距离阵：cdist 计算 784×784 的两两欧氏距离 d_ij
③ 高斯权重：A_ij = exp(-d_ij / σ²)，σ = 0.05π；再置 A[A<0.01] = 0（截断远邻）
④ 对称归一化：D = A 按行求和；Â = D^(-1/2) · A · D^(-1/2)（Kipf & Welling 2017）
```

两处细节值得先算一算（后面实践会用到）：

- **注意公式形态**：代码是 \( A_{ij} = \exp(-d_{ij}/\sigma^2) \)，距离取**一次方**——不是教科书高斯的 \( \exp(-d^2/2\sigma^2) \)。以代码为准，它是「随距离指数衰减」的滤波，衰减速度由 \( \sigma^2 \) 控制。
- **截断阈值决定了实际邻域半径**。\( A_{ij} \geq 0.01 \) 等价于 \( d_{ij} \leq \sigma^2 \ln 100 \approx 0.1137 \)（归一化坐标下），而相邻像素的归一化距离是 \( 1/28 \approx 0.0357 \)，所以截断半径约为 \( 0.1137 \times 28 \approx 3.2 \) 个像素——每个节点实际保留**欧氏半径约 3 格圆盘内**的邻居（约 30 个），而非整幅图。

#### 4.1.3 源码精读

先看坐标表与距离阵（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L216-L226](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L216-L226)）：`np.meshgrid` 生成行列网格，`np.stack(...).reshape(-1, 2)` 把 784 个像素的坐标摊平成 `784×2` 数组并除以 `img_size` 归一化到 [0,1)；`cdist(coord, coord, metric='euclidean')` 一步算出 784×784 的两两欧氏距离矩阵。

接着是高斯滤波与截断（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L228-L231](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L228-L231)）：`sigma = 0.05 * np.pi`（≈0.157，注意后面用的是 `sigma ** 2`），`A = np.exp(- dist / sigma ** 2)` 对距离做指数衰减；`A[A < 0.01] = 0` 把弱边清零——如上一小节所算，这一步把图限制在约 3 像素邻域。

最后是对称归一化（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L233-L238](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L233-L238)）：

```python
D = A.sum(1)                       # 每个节点的度 (N,)
D_hat = (D + 1e-5) ** (-0.5)       # 防除零
A_hat = D_hat.view(-1, 1) * A * D_hat.view(1, -1)
```

这段实现 \( \hat{A} = D^{-1/2} A D^{-1/2} \)（注释注明出处是 Kipf & Welling, ICLR 2017 的 GCN 论文）。直觉：不同节点度数差异大（角落节点邻居少、中心节点邻居多），若直接用 \( A \) 聚合，特征幅度会随度数涨落；除以两端度数的平方根后，聚合近似「加权平均」而非「加权求和」，且 \( \hat{A} \) 保持**对称**——这两点对训练稳定性与后面的谱分析都重要。

数据管线部分（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L162-L186](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L162-L186)）沿用 u4-l1 讲过的 `Subset` + 索引区间方式：前 59000 张训练、后 1000 张验证，与 helper 模块的 `SubsetRandomSampler` 方案殊途同归。超参集中在 L128-L137：`LEARNING_RATE = 0.05`、`NUM_EPOCHS = 20`、`BATCH_SIZE = 128`（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L128-L137](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L128-L137)）。

#### 4.1.4 代码实践

**实践目标**：亲手量化「σ 与截断阈值如何控制邻域大小」，为综合实践打底。

1. 打开 `gnn-basic-1.ipynb`，只运行 Imports、Settings、Dataset、`precompute_adjacency_matrix` 四段（无需 GPU）。
2. **示例代码**（非 notebook 原有代码）：

```python
import time, numpy as np
for sigma_scale, thresh in [(0.05, 0.01), (0.05, 0.05), (0.15, 0.01), (0.01, 0.01)]:
    t0 = time.perf_counter()
    col, row = np.meshgrid(np.arange(28), np.arange(28))
    coord = np.stack((col, row), axis=2).reshape(-1, 2) / 28
    from scipy.spatial.distance import cdist
    dist = cdist(coord, coord)
    A = np.exp(-dist / (sigma_scale * np.pi) ** 2)
    A[A < thresh] = 0
    elapsed = (time.perf_counter() - t0) * 1000
    print(f"sigma={sigma_scale}π, thresh={thresh}: "
          f"非零边比例={(A != 0).mean():.4f}, 建图耗时={elapsed:.1f} ms")
```

3. **需要观察的现象**：`sigma_scale` 从 0.15π 缩到 0.01π 时，非零边比例从「大面积」缩到「几乎只剩对角线」；而**建图耗时几乎不变**——因为 `cdist` 与 `exp` 都是对固定 784×784 矩阵的整体计算，截断只是把部分元素置零，并不省计算。
4. **预期结果**：σ=0.01π 时邻接矩阵趋近单位矩阵（每个节点几乎只连自己），后续 4.2 的分析告诉我们这会把模型退化回 softmax 回归；σ=0.15π 时邻域覆盖大半张图，聚合变成「全图平均」，局部形状信息被抹平。具体准确率变化见第 5 节综合实践（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：把截断阈值从 0.01 提高到 0.05，截断半径变为几个像素？
**答案**：条件变为 \( d \leq \sigma^2 \ln 20 \approx 0.0247 \times 3.0 \approx 0.074 \)，即约 \( 0.074 \times 28 \approx 2.1 \) 个像素——阈值提高 5 倍，半径从约 3.2 像素缩到约 2 像素（对数关系，不是线性）。

**练习 2**：为什么用对称归一化 \( D^{-1/2} A D^{-1/2} \)，而不是简单地把每行除以度（行归一化 \( D^{-1} A \)）？
**答案**：行归一化 \( D^{-1}A \) 不再对称（\( A_{ij}/D_i \neq A_{ji}/D_j \)），而对称性是 4.3 节谱分析（实对称矩阵才有实特征分解、正交特征向量）的前提；同时 \( D^{-1/2}AD^{-1/2} \) 让每一对节点的相互影响都被两端度数共同缩放，聚合近似加权平均，避免高度数节点主导。这正是 GCN 论文的传播规则。

**练习 3**：邻接矩阵是在训练循环里每个 batch 重新算的吗？
**答案**：不是。它在模型构造函数里一次性预计算并用 `register_buffer('A', A)` 登记（见 4.2.3），之后随 `.to(DEVICE)` 搬到 GPU、随 `state_dict` 保存，训练全程不变——「图结构固定、只学权重」是这一版 GNN 的设定。

### 4.2 高斯滤波消息传递：`torch.bmm`、线性本质与可学习的边

#### 4.2.1 概念说明

有了 \( \hat{A} \)，一层图卷积就是一次**矩阵乘**：\( X^{(l+1)} = \hat{A} X^{(l)} W^{(l)} \)。逐节点读这行公式：节点 \( i \) 的新特征等于 \( \sum_j \hat{A}_{ij} \cdot (\text{节点 } j \text{ 的特征}) \)——**按邻接权重收集邻居的消息**，这就是「消息传递」。当 \( X \) 是一维灰度时，\( \hat{A}X \) 恰是对图像做一次高斯型模糊；随后的 \( W \) 把每个节点的模糊值线性映射到类别分数。

这一版模型有个必须点破的事实：**整个前向只有线性运算**（`bmm` + `Linear`），没有激活函数。按 u2-l3 的结论，它是线性模型：

\[ \text{logits} = W\,(\hat{A}x) = (W\hat{A})\,x \]

即等效权重 \( W_{\text{eff}} = W\hat{A} \) 的一个 10×784 线性打分器。当 \( \hat{A} \) 满秩时，\( W_{\text{eff}} \) 可以取到任意 10×784 矩阵，**假设空间与 softmax 回归完全一致**。notebook 的测试准确率 91.63% 与 u2-l2 softmax 回归的 91.77% 几乎重合，不是巧合，而是数学同构的实证；小差异来自优化路径不同（先过 \( \hat{A} \) 改变了特征尺度，等于改了有效学习率）。所以这个 notebook 的正确定位是：**GNN 概念的最小教具**，而非更强的分类器。

**edge 版的动机**随之自然浮现：手工高斯图未必最优，那能不能**让边也变成可学习的**？`gnn-basic-edge-1.ipynb` 把 \( A \) 换成一个小 MLP 的输出——注意它的输入是**固定的像素坐标对**（与输入图像无关），所以学到的是「哪种空间邻接结构对分类最有用」，一个全局共享、随训练演化的图；类比 CNN，它学到的角色接近「可学习的感受野形状」。

#### 4.2.2 核心流程

高斯版 `GraphNet.forward`（每个 batch）：

```text
x: [B, 1, 28, 28]（灰度图）
 ├─ x.view(B, -1, 1)          → [B, 784, 1]   每个像素一个节点的 1 维特征
 ├─ Â.expand(B, -1, -1)       → [B, 784, 784] 同一个图复制到 batch 维（视图，不占显存）
 ├─ torch.bmm(Â, x)           → [B, 784]      消息传递：每个节点 = 邻居加权平均
 └─ self.fc(...)              → [B, 10]       线性分类头（784→10，无 bias）
```

edge 版只多一步：forward 一开始先用 `pred_edge_fc(coord)` 现算 `self.A = ...`（[B 前的 784×784]），其余与高斯版逐行相同。边预测网络是 `Linear(4→32) → ReLU → Linear(32→1) → Tanh`，输出落在 [-1, 1]——允许「抑制性」边（负权重），比高斯版只能非负更灵活；代价是没有归一化（edge 版删掉了 \( D^{-1/2}AD^{-1/2} \) 那段）。

#### 4.2.3 源码精读

**模型定义与 buffer 登记**（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L275-L284](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L275-L284)）：`self.fc = nn.Linear(n_rows, num_classes, bias=False)` 是唯一可学习层；`A = precompute_adjacency_matrix(img_size)` 建图后 `self.register_buffer('A', A)` 登记——`A` 不是参数、不会被优化，但会随设备搬运与存档，正是 4.1.5 练习 3 说的机制。

**前向的消息传递**（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L291-L308](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L291-L308)）：

```python
A_tensor = self.A.unsqueeze(0)        # 冗余行：结果未被使用
A_tensor = self.A.expand(B, -1, -1)   # [784,784] → [B,784,784]（共享视图）
x_reshape = x.view(B, -1, 1)          # [B,1,28,28] → [B,784,1]
avg_neighbor_features = torch.bmm(A_tensor, x_reshape).view(B, -1)  # [B,784]
logits = self.fc(avg_neighbor_features)
```

两个读码点：其一，`unsqueeze(0)` 那行算出的张量随即被下一行覆盖，`expand` 本可以直接把 `[784,784]` 扩到 `[B,784,784]`（在最前面新增维度），这是一处无害的冗余；其二，`expand` 生成共享底层存储的视图、并不真的复制 B 份图，`bmm`（batch matrix multiply，批量矩阵乘）则对 batch 内每个样本独立做 `784×784 · 784×1` 的乘法——**稠密实现**，计算量远大于 3×3 卷积（这正是图像任务用 CNN 而非通用 GNN 的效率原因：网格图的消息传递本可写成小核卷积）。

**edge 版的坐标对特征**（[pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L229-L243](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L229-L243)）：`make_coordinate_array` 先把 784 个像素坐标做 z-score 标准化（`L235`，减均值除标准差——注意这与高斯版「除以 img_size」不同），再用 `repeat` + `cat` 拼出 `[784, 784, 4]` 的特征张量：每个有序节点对 \( (i,j) \) 的 4 维特征 =（节点 i 的 x,y，节点 j 的 x,y）。因为特征里 i、j 坐标的**位置不对称**（前两维是 i、后两维是 j），MLP 输出的 \( A_{ij} \neq A_{ji} \)，学出的是**有向图**。

**边预测网络与前向**（[pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L247-L290](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L247-L290)）：`self.coord` 通过 `register_buffer` 登记（`L255`）；`pred_edge_fc` 定义在 `L259-L262`；`L271` 的 `self.A = self.pred_edge_fc(self.coord).squeeze()` 在每次 forward 现算邻接矩阵并**存成实例属性**（不是 buffer，也因此训练后可以用 `model.A` 取出学到的图可视化，见下）。此处有一个本手册熟悉的名场面：notebook 顶部 markdown 写的是 `nn.Linear(coord_features, 64)`（[pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L58-L63](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L58-L63)），而真实代码是 **32**——markdown 过时、代码为准（u3-l5、u7-l4 都遇到过同类案例）。

**训练配置与学到的图**：edge 版把优化器换成 `torch.optim.Adam`、`LEARNING_RATE = 0.0005`、`NUM_EPOCHS = 50`（[pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L139-L141](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L139-L141)、[L304](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L304)）——因为边预测网络的梯度尺度与高斯版完全不同。训练日志（`L328` 起）显示第 1 个 epoch 只有 54.80% 验证准确率：随机初始化的 `pred_edge_fc` 产出的是近乎无信息的图，模型要**先学出一个合理的图、再靠它分类**，收敛因此慢于第 1 个 epoch 即达 92.40% 的高斯版。训练结束后 `plt.imshow(model.A.to('cpu'))`（[pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L770-L772](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L770-L772)）直接画出了学到的邻接矩阵——可以看到明显沿对角线的亮带：模型自己收敛到了「邻近像素相连」的结构，与手工高斯直觉一致。最终验证 93.00%、测试 90.36%（[L842-L843](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-edge-1.ipynb#L842-L843)），略低于手工高斯版——「可学习」并不必然更好，它同时引入了更难的优化问题。

训练循环本体与 u2-l2 以来的五步范式完全一致（[pytorch_ipynb/gnn/gnn-basic-1.ipynb#L504-L545](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L504-L545)）：`forward → cross_entropy → zero_grad → backward → SGD.step`，不再赘述；`compute_acc`（[L486-L492](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L486-L492)）也是熟悉的三行计数器。高斯版最终验证 94.30%、测试 91.63%（[L626-L627](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-1.ipynb#L626-L627)）。

#### 4.2.4 代码实践

**实践目标**：用对照实验验证「消息传递 ≈ 高斯模糊、模型本质是线性分类器」这两个论断。

1. **论断一（图 = 模糊核）**：在 gnn-basic-1 环境下取一张测试图，**示例代码**：

```python
import torch
from torchvision import datasets
test = datasets.MNIST(root='data', train=False, transform=custom_transform)
img, label = test[0]                       # [1,28,28]，灰度
A_hat = model.A                            # 训练后的对称归一化邻接矩阵
blurred = A_hat @ img.view(-1, 1)          # 消息传递 = 一次图上模糊
import matplotlib.pyplot as plt
fig, ax = plt.subplots(1, 2)
ax[0].imshow(img.view(28, 28), cmap='Greys'); ax[1].imshow(blurred.detach().view(28, 28), cmap='Greys')
```

2. **论断二（线性同构）**：构造 `W_eff = model.fc.weight @ model.A`，比较 `model(img[None,...])[0]` 与 `W_eff @ img.view(-1)` 的输出——两者应逐元素一致（`torch.allclose`），证明模型确实等价于权重为 \( W\hat{A} \) 的 softmax 回归。
3. **需要观察的现象**：左图原字迹清晰、右图整体变糊但仍认得出数字（邻域半径约 3 像素的模糊）；论断二的两路输出最大绝对差应在浮点误差量级。
4. **预期结果**：两个论断都成立（模型无激活层，数学上可严格推出，待本地验证以建立手感）。

#### 4.2.5 小练习与答案

**练习 1**：`GraphNet` 里一个激活函数都没有，为什么还能到 91.63%？如果给消息传递后加一个 `nn.ReLU`，假设空间会怎么变？
**答案**：因为它是线性模型，等效权重 \( W_{\text{eff}} = W\hat{A} \)，\( \hat{A} \) 满秩时假设空间与 softmax 回归一致，91.63% ≈ 91.77% 正是同构的体现。加上 ReLU 后模型变成真正的单隐层网络（隐层宽度 784、每个隐单元配一个高斯型「感受野」），假设空间扩大，理论上限不再被线性限制——这大致就是「把高斯核换成可学核」的一小步。

**练习 2**：edge 版学到的邻接矩阵 `A` 依赖输入图像吗？对一个 batch 里的 128 张图，用的是同一个 `A` 吗？
**答案**：不依赖图像内容。`pred_edge_fc` 的输入是 `register_buffer` 登记的固定坐标特征 `coord`，`A` 只由网络参数决定；同一前向中 batch 内所有样本共享同一个 `A`。它学的是「全局最优的空间邻接结构」，类似 CNN 学感受野形状，而不是 per-sample 的动态图。

**练习 3**：edge 版的邻接矩阵为什么一般不对称？若想让它对称，可以怎么改？
**答案**：因为有向节点对特征 (i 的坐标, j 的坐标) 中两段坐标的**排列位置不同**，`MLP(φᵢ, φⱼ) ≠ MLP(φⱼ, φᵢ)`。想对称可以取 \( A_{ij} \leftarrow \frac{1}{2}(A_{ij} + A_{ji}) \)，或把特征构造成对称函数（如只用 \( φᵢ + φⱼ \) 与 \( |φᵢ - φⱼ| \)）。

### 4.3 谱图卷积：从邻域平均到图傅里叶滤波

#### 4.3.1 概念说明

前两节的卷积在「节点域」定义：直接用 \( \hat{A} \) 加权平均。**谱图卷积**（spectral graph convolution）换到「频率域」定义，路线类比一维信号处理：时域卷积 = 频域乘积（卷积定理）。图上同理，只是「频率基」要换成**图拉普拉斯矩阵的特征向量**：

1. 对称归一化拉普拉斯 \( L = I - D^{-1/2} A D^{-1/2} \) 是实对称半正定矩阵，可特征分解 \( L = V \Lambda V^\top \)，特征向量 \( V \) 的每一列是图上的一个「振动模式」（小特征值 = 低频 = 平缓变化的信号；大特征值 = 高频 = 剧烈振荡），特征值就是频率。
2. **图傅里叶变换**：把节点域信号投影到这组基上，\( \hat{x} = V^\top x \)；逆变换用 \( V \) 变回来。
3. **卷积定理**：图上两个信号的卷积 = 各自傅里叶变换逐元素相乘再逆变换。把其中一个信号取为**可学习的滤波器** \( W \)，就得到 notebook 顶部的谱卷积公式：

\[ X^{(l+1)} = V\left(V^\top X^{(l)} \odot V^\top W_{\text{spectral}}^{(l)}\right) \]

其中 \( \odot \) 是逐元素乘。直觉：节点域的「邻居加权平均」（高斯那种空间核）与频率域的「给每个频率分量乘一个增益」（低通/带通滤波器）是同一件事的两种说法——高斯核恰恰对应一个低通滤波器。谱路线的意义在于：滤波器直接在频率上定义，能表达节点域难写出的全局模式；代价是 \( V \) 依赖具体图、且特征分解昂贵。这条路线经一系列近似（Chebyshev 多项式、Kipf & Welling 的一阶近似）最终演化成现代空间域 GCN——本 notebook 展示的是最原始的「显式特征分解」版本。

#### 4.3.2 核心流程

谱版 `GraphNet` 的构造期流水线（一次性、与输入数据无关）：

```text
① A = precompute_adjacency_matrix(28)   # 复用 4.1 的高斯建图（此版不做 Â 归一化，直接返回 A）
② L = get_graph_laplacian(A)            # L = I − D^(−1/2) A D^(−1/2)
③ Λ, V = eigsh(L, k=20, which='SM')     # 只取最小 20 个特征值对应的低频特征向量，V: [784, 20]
④ W_spectral: [784, num_filters=2]      # 可学习(？)谱滤波器，kaiming_uniform 初始化
```

前向（每个 batch）：

```text
x: [B,1,28,28] → x_reshape [B,784,1]
X_hat = Vᵀ·x          # [B,20,1]  信号的谱系数（20 个频率分量）
W_hat = Vᵀ·W_spectral # [B,20,2]  滤波器的谱系数
Y     = V·(X_hat ⊙ W_hat)  # [B,784,2]  谱域相乘后逆变换回节点域
logits = fc(Y.reshape(B, 784*2))  # [B,10]
```

注意 \( k=20 \) 的截断：信号与滤波器都被压到 20 维低频子空间，相当于先做一次强低通滤波。这使得哪怕 `fc` 输入名义上有 1568 维，**有效自由度只有 20×2 = 40 维**——这是它准确率偏低（84.55%）的主要原因，后面小结会算这笔账。

#### 4.3.3 源码精读

**拉普拉斯矩阵**（[pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L249-L259](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L249-L259)）：`D = np.sum(A, 0)` 求度、`D_hat = np.diag((D + 1e-5)**(-0.5))`、`L = np.identity(N) - np.dot(D_hat, A).dot(D_hat)`，即 \( L = I - D^{-1/2}AD^{-1/2} \)（注释附上了教程出处）。同文件里的 `precompute_adjacency_matrix`（[L220-L247](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L220-L247)）与 4.1 精读的那份几乎相同，但 **Kipf & Welling 归一化被整段注释进了三引号字符串**（L239-L246），直接返回未归一化的 `A`——归一化改在拉普拉斯内部做。两个 `plt.imshow` 分别画出 `A` 与 `L`（[L281-L289](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L281-L289)、[L306-L310](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L306-L310)），值得一看：拉普拉斯在主对角线上是 1、邻域位置是负值——正是「自身减去邻居平均」的二阶差分形态。

**特征分解与参数登记**（[pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L325-L345](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L325-L345)）：

```python
self.fc = nn.Linear(n_rows*num_filters, num_classes, bias=False)   # 1568 → 10
A = precompute_adjacency_matrix(img_size)
L = get_graph_laplacian(A.numpy())
Λ, V = eigsh(L.numpy(), k=20, which='SM')   # 最小 20 个特征值 → 低频基
W_spectral = nn.Parameter(torch.ones((img_size**2, num_filters))).float()
torch.nn.init.kaiming_uniform_(W_spectral)
self.register_buffer('A', A); ...; self.register_buffer('V', V)
self.register_buffer('W_spectral', W_spectral)   # ← 注意这一行
```

`eigsh` 是 SciPy 针对对称稀疏矩阵的 Lanczos 特征分解，`which='SM'` 取**模最小**的 20 个特征值——对半正定的 \( L \) 即最小频率（最平滑的图信号；由于像素网格的低频基恰是二维余弦波，画出来会像平滑的波纹）。分类头输入是 `n_rows*num_filters = 784×2 = 1568` 维（`L330`）。

**这里藏着本讲最重要的读码发现**：`W_spectral` 用 `nn.Parameter(...)` 创建、做了 Kaiming 初始化，但随后被 `register_buffer('W_spectral', ...)` 登记为 **buffer 而非 parameter**（L345）。按第 2 节的术语，buffer 不进 `model.parameters()`，而优化器是 `torch.optim.SGD(model.parameters(), ...)`——所以**谱滤波器权重大概率从未被训练**，全程保持 Kaiming 随机初始化；真正在学的只有 `fc`。这解释了准确率的另一层：模型实际上是「固定随机谱滤波器 + 可学线性分类头」。两点旁证：其一，作者保存的训练输出里首个 epoch 验证仅 78.10%、50 个 epoch 缓慢爬到 88.70%（[L417](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L417) 起），比「滤波器也在学」的典型曲线更平缓；其二，最终测试 84.55%（[L907-L908](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L907-L908)）明显低于前两版，与「特征被截到 40 维有效自由度、且滤波器随机」的双重限制一致。另注意：notebook 运行于 torch 1.2（顶部 watermark），当时的 `register_buffer` 不拒绝 Parameter 对象；较新的 PyTorch 会对「把 Parameter 注册为 buffer」直接抛 `TypeError`。**此分析基于 PyTorch 的 buffer/parameter 语义推断，待本地验证**（验证方法见 4.3.4）。

**谱卷积前向**（[pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L355-L378](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L355-L378)）核心就是与公式一一对应的三行 `bmm`：

```python
X_hat = V_tensor_T.bmm(x_reshape)          # Vᵀx  → [B,20,1]   图傅里叶变换
W_hat = V_tensor_T.bmm(W_spectral_tensor)  # VᵀW  → [B,20,2]   滤波器变换到谱域
Y = V_tensor.bmm(X_hat * W_hat)            # V(·)  → [B,784,2] 逆变换回节点域
logits = self.fc(Y.reshape(B, -1))
```

`X_hat * W_hat` 是 `[B,20,1]` 与 `[B,20,2]` 的广播逐元素乘——每个频率分量上，信号系数分别与 2 个滤波器系数相乘，正对应卷积定理。顺带一提，settings 里 `DEVICE = torch.device("cuda:3" ...)`（[L131](https://github.com/rasbt/deeplearning-models/blob/59beda3aa25337e576564f1c18c6edd26f9f30e3/pytorch_ipynb/gnn/gnn-basic-graph-spectral-1.ipynb#L131)）是作者多卡机器的痕迹：单卡环境下 `torch.cuda.is_available()` 为 True 但没有第 4 块卡，运行会在这里报错——本地复现时把它改回 `cuda:0` 或 `cpu` 即可。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`W_spectral` 到底参不参与训练」，并感受 `k` 与 `num_filters` 两个旋钮的作用。

1. 打开 `gnn-basic-graph-spectral-1.ipynb`，把 `DEVICE` 改为可用的设备后，运行到模型构建处（无需训练）。
2. **示例代码**（非 notebook 原有代码）：

```python
model = GraphNet(img_size=28, num_classes=10)
print("parameters:", [n for n, _ in model.named_parameters()])
print("buffers:   ", [n for n, _ in model.named_buffers()])
print("V.shape:", model.V.shape, "W_spectral.shape:", model.W_spectral.shape)
```

3. 记录输出。若 `parameters` 里只有 `fc.weight` 而 `W_spectral` 出现在 `buffers` 里，则 4.3.3 的推断成立（滤波器不训练）；若你的 PyTorch 版本在构建时就抛出 `TypeError: cannot assign 'torch.nn.parameter.Parameter' ... as buffer`，说明你遇到了新版 PyTorch 对这段历史代码的保护，可把 `nn.Parameter(...)` 改成 `torch.Tensor(...)`（示例修改，等价于 buffer 语义）再观察。
4. **需要观察的现象**：`V.shape = [784, 20]`、`W_spectral.shape = [784, 2]`；参数列表的归属如上所述。
5. **预期结果**：在 torch 1.2–1.4 环境下 `W_spectral` 落在 buffers；训练曲线与作者输出同形（缓慢爬升）。在较新 torch 上构建即报错。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`eigsh(L, k=20, which='SM')` 取的是哪 20 个特征向量？为什么说这是「低频截断」？
**答案**：`'SM'` = smallest magnitude，取特征值模最小的 20 个；对半正定的 \( L \)，小特征值对应最平滑的图信号（变化最慢的「波纹」），类似图像的低频分量。截断到 20 维意味着信号与滤波器都只保留 20 个频率分量，高频细节被丢弃——等价于强低通滤波，这也是有效特征维度只有 40 的原因。

**练习 2**：谱版的准确率（84.55%）为什么明显低于高斯版（91.63%）？给出两层原因。
**答案**：第一层（表达力）：\( k=20 \) 的谱截断把每个样本的有效特征压到 20×2=40 维（低频子空间内），远低于高斯版的 784 维；第二层（优化，结合 4.3.3 的发现）：`W_spectral` 被 `register_buffer` 登记后不进 `model.parameters()`，优化器只更新 `fc`，谱滤波器停留在随机初始化——模型实际是「随机固定谱特征 + 线性分类头」。两层叠加压低了上限。

**练习 3**：高斯滤波的邻域平均（节点域卷积）与谱域的「给每个频率乘增益」有什么联系？
**答案**：卷积定理保证两者等价——节点域的加权平均核与谱域的逐频率增益是一对傅里叶变换。手工高斯核（宽 σ≈0.157、半径约 3 像素）正对应一个低通滤波器；谱版把滤波器从「手工给定核」换成「在 20 个频率上可配的增益」（理想情况下可学），是同一运算的两种参数化。

## 5. 综合实践

把本讲三个模块串起来：**做一次「图结构 → 分类性能」的受控实验**。任务来自讲义规格：修改 gnn-basic-1 中的高斯滤波参数与邻域半径，观察准确率与建图耗时的变化，并画出一个数字样本的邻接结构示意图。

**步骤**：

1. **参数化建图函数**（示例代码，在 notebook 里替换原函数）：

```python
def precompute_adjacency_matrix(img_size, sigma_scale=0.05, thresh=0.01):
    col, row = np.meshgrid(np.arange(img_size), np.arange(img_size))
    coord = np.stack((col, row), axis=2).reshape(-1, 2) / img_size
    dist = cdist(coord, coord, metric='euclidean')
    sigma = sigma_scale * np.pi
    A = np.exp(- dist / sigma ** 2)
    A[A < thresh] = 0
    A = torch.from_numpy(A).float()
    D = A.sum(1)
    D_hat = (D + 1e-5) ** (-0.5)
    return D_hat.view(-1, 1) * A * D_hat.view(1, -1)
```

2. **建图侧记录**：对 \((\sigma\_scale, \text{thresh}) \in \{(0.01, 0.01), (0.05, 0.01), (0.15, 0.01), (0.05, 0.05)\}\) 四组，用 4.1.4 的脚本记录非零边数、平均每节点邻居数与建图耗时（毫秒）。
3. **训练侧记录**：每组重建 `GraphNet`（把建图函数的参数传进去，模型构造函数里的调用处相应修改），沿用原配置（SGD、lr=0.05、20 epoch）训练，记录最终验证/测试准确率。
4. **画邻接结构示意图**：取第 5 行第 14 列的中心节点 `idx = 14*28 + 14`，把 `A_hat[idx].view(28, 28)` 用 `plt.imshow` 画出（这就是「站在该节点看出去」的邻接权重分布，应是一个以节点为中心、半径约 3 像素的光斑）；进阶做法是把它叠加到一张真实数字图上（`0.5*img + 0.5*mask`）对比。
5. **整理成一张对照表**：`σ_scale | thresh | 平均邻居数 | 建图耗时 ms | 测试准确率`。

**需要观察的现象与预期结果**（待本地验证）：

- σ=0.01π 时 A 近乎单位阵，模型退化为对原始像素的 softmax 回归，准确率应回到 91%–92% 附近——这是 4.2 线性同构论的又一次实证。
- σ=0.15π 时邻域覆盖大半张图，聚合近似全图平均，笔画细节被抹平，准确率应**下降**；最优值大概率在基准 0.05π 附近。
- **建图耗时四组几乎相同**：`cdist`/`exp`/截断都是对 784×784 的稠密计算，与稀疏度无关；训练耗时同样不变（`bmm` 用的是稠密矩阵）。这是本实践最反直觉的「负结果」：想让「更小的图」真正省钱，必须改用稀疏存储（如 `torch.sparse`）——留给读者的进阶方向。

## 6. 本讲小结

- **图像即图**：MNIST 的一张图就是 784 节点的图，节点特征是像素灰度，边由空间距离决定——`precompute_adjacency_matrix` 用「坐标 → cdist 欧氏距离 → 指数衰减 → 0.01 截断 → \( D^{-1/2}AD^{-1/2} \) 对称归一化」五步把它算成固定 buffer；σ=0.05π + 阈值 0.01 对应约 3 像素半径的邻域。
- **消息传递一层 = 一次 `bmm`**：\( X^{(l+1)} = \hat{A}X^{(l)}W^{(l)} \)，节点视角即「按邻接权重收集邻居特征」；图像视角即「一次高斯模糊 + 线性分类」。
- **没有激活层 ⇒ 线性模型**：高斯版等效权重 \( W\hat{A} \)，假设空间与 softmax 回归一致，91.63% vs 91.77% 是数学同构的实证——这份 notebook 是 GNN 概念教具，不是更强的分类器。
- **边可以学**：edge 版用 `Linear(4→32)→ReLU→Linear(32→1)→Tanh` 从坐标对预测邻接矩阵（有向、[-1,1]、全局共享、与样本无关），训练后自发出「邻近相连」结构，但 90.36% 略逊于手工图——可学习不必然更优。
- **谱路线 = 频率域的同一件事**：拉普拉斯 \( L = I - D^{-1/2}AD^{-1/2} \) 特征分解给出频率基，卷积定理把图卷积化为「谱系数相乘再逆变换」；`k=20` 低频截断把有效特征压到 40 维，是 84.55% 的第一层原因。
- **读码以代码为准**：edge 版 markdown 写 64 实际代码是 32；谱版 `nn.Parameter` 创建的 `W_spectral` 被 `register_buffer` 登记，按 PyTorch 语义不会进优化器（推断，待本地验证），新版 torch 还会直接报错——两个真实细节再次验证这条手册传统。

## 7. 下一步学习建议

- **读完本讲的三份 notebook 尾部链接**：作者标注的 Boris Knyazev 两篇教程（Part 1 空间域、Part 2 谱域）是这三份 notebook 的源头，适合作为系统化补充。
- **进入现代 GCN**：把本讲的手工 \( \hat{A} \) 与显式特征分解分别对应到 Kipf & Welling (2017) 的传播规则与 Chebyshev 近似——理解「显式谱分解 → 多项式近似 → 一阶近似（现代 GCN）」的演化，就能明白为什么现代图网络回到空间域实现。可配合 PyTorch Geometric 的 `GCNConv` 源码对照阅读。
- **在手册内**：u8 单元只剩最后一讲 u8-l7（TensorFlow 1.x 框架对照与收官），将对照 TF1 的计算图/会话范式并回顾整套学习路径；若想先巩固本讲的多卡/显存主题，可回看 u8-l5 的 DataParallel 与 u8-l4 的梯度检查点。
- **动手方向**：把综合实践的稠密 `bmm` 改成 `torch.sparse` 稀疏实现并实测提速；或仿照 edge 版给 4.2 的高斯版加上 ReLU 隐层（练习 1 的延伸），在同一个骨架上量化「非线性」带来的增益。
