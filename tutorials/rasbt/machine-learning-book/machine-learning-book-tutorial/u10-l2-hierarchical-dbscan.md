# 层次聚类与 DBSCAN

## 1. 本讲目标

上一讲（u10-l1）我们学习了 k-means：它需要预先指定簇数 k、只擅长「凸形团块」，这两条局限在月牙形数据面前会彻底失效。本讲介绍两种回应这些局限的聚类算法，学完后你应该能够：

1. 用 scipy 的 `pdist` / `squareform` 计算并正确展示样本间的距离矩阵，并知道 `linkage` 接受哪种形式的距离输入（这里有一个书里特别警告过的经典误用）。
2. 看懂 `linkage` 输出的「合并记录表」每一列的含义，并会用 `dendrogram` 把它画成树状图、拼到热图旁边。
3. 使用 sklearn 的 `AgglomerativeClustering`，理解 `linkage`（连接方式）参数与新旧版本中 `affinity` / `metric` 参数的更替。
4. 理解 DBSCAN 的核心点 / 边界点 / 噪声点三分类、`eps` 与 `min_samples` 的几何含义，以及它为什么能分离 k-means 与层次聚类都搞不定的月牙形簇。

## 2. 前置知识

- **凝聚式层次聚类（agglomerative clustering）**：与 k-means「自顶向下拆」不同，凝聚法自底向上：一开始每个样本自成一簇，每一步把「距离最近」的两个簇合并，直到满足停止条件（通常是指定簇数）。整个过程会形成一棵二叉合并树，无需预先假设簇的形状是球形。
- **簇间距离（连接方式 linkage）**：合并前先要回答「两个簇之间的距离是多少」。对簇 \(A\)、\(B\)，两种极端定义是：

  \[ d_{\text{complete}}(A, B) = \max_{a \in A,\, b \in B} \|a - b\| \qquad d_{\text{single}}(A, B) = \min_{a \in A,\, b \in B} \|a - b\| \]

  complete 连接取两簇**最远**一对点的距离，倾向产生紧凑、大小接近的簇；single 连接取**最近**一对点，容易产生「链式效应」（chain effect）——像链条一样把细长的形状串成一簇。这既是缺点（离群点会拖出长链）也是优点（能沿非凸形状延伸）。
- **密度聚类（DBSCAN）**：不借助任何「簇中心」或「簇间距离」，只看局部密度。参数 `eps`（邻域半径 \(\epsilon\)）和 `min_samples` 共同定义「密集」：如果一个点周围 \(\epsilon\) 半径内至少有 `min_samples` 个邻居，它就是密集区域的「核心点」；簇就是互相连成一片的核心点及其边界。
- **噪声点（noise / label = -1）**：DBSCAN 不强制所有点都有归属，既不是核心点也不挨着核心点的样本会被标为 -1。这是它与 k-means、层次聚类的本质区别之一——内置离群点检测。
- 建议先完成 u10-l1（k-means、肘部法、轮廓系数），本讲多次与其对照。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch10/ch10.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py) | 第 10 章脚本体，本讲聚焦「Organizing clusters as a hierarchical tree」（L318 起）与「Locating regions of high density via DBSCAN」（L486 起）两节 |
| [ch10/ch10.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.ipynb) | 与脚本等价的 notebook 版本，交互实验用它更方便（u1-l3 已建立的双轨工作流） |
| [ch10/figures/](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/figures) | 书中插图，本讲对应 `10_11.png`（树状图）、`10_12.png`（树状图+热图）、`10_14.png`（月牙数据）、`10_15.png`（k-means 与层次聚类失败对比）、`10_16.png`（DBSCAN 成功） |
| [python_environment_check.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/python_environment_check.py) | 各章共用的包版本检查脚本（u1-l2 已讲解），ch10.py 开头会调用它并要求 scipy>=1.7.0、sklearn>=1.0 |

本讲涉及的导入语句集中在 [ch10/ch10.py:L13-L19](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L13-L19)：`pdist`、`squareform`（距离矩阵）、`linkage`、`dendrogram`（层次聚类）、`AgglomerativeClustering`、`make_moons`、`DBSCAN`。

## 4. 核心概念与源码讲解

### 4.1 pdist 与 squareform：距离矩阵的两种形态

#### 4.1.1 概念说明

层次聚类的原料是「样本两两之间的距离」。对 \(n\) 个样本，完整距离矩阵是一个 \(n \times n\) 对称矩阵，对角线为 0。但对称矩阵有一半是冗余的，scipy 因此定义了**浓缩距离矩阵（condensed distance matrix）**：只保留上三角的 \(\frac{n(n-1)}{2}\) 个距离，摊平成长度 \(\frac{n(n-1)}{2}\) 的一维数组。

- `scipy.spatial.distance.pdist(X, metric=...)`：输入 \(n \times m\) 的样本矩阵，输出浓缩距离矩阵。
- `scipy.spatial.distance.squareform(condensed)`：把浓缩形态还原成 \(n \times n\) 方阵（反向调用 `squareform(square_matrix)` 也能压回浓缩形态）。

为什么要有两种形态？`linkage` 等算法只需要浓缩形态（省内存），而人类查看、给 DataFrame 加行列标签时需要方阵。

#### 4.1.2 核心流程

```text
原始样本矩阵 X (n 行样本 × m 列特征)
        │  pdist(X, metric='euclidean')
        ▼
浓缩距离矩阵 (长度 n(n-1)/2 的一维数组)          ← linkage 直接吃这个
        │  squareform(...)
        ▼
n × n 对称方阵（对角线为 0）                      ← 给人看、贴行列标签
```

欧氏距离的数学定义（\(x_i\)、\(x_j\) 为两个样本向量）：

\[ d(i, j) = \sqrt{\sum_{k=1}^{m} (x_{ik} - x_{jk})^2} \]

#### 4.1.3 源码精读

书中先用 5 个样本、3 个特征构造一个小到可以心算的玩具数据集：

[ch10/ch10.py:L330-L337](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L330-L337) —— 固定随机种子 `np.random.seed(123)`，生成 5×3 的随机数并乘 10，装进带行标签（ID_0~ID_4）和列名（X/Y/Z）的 DataFrame。注意这里的变量名 `X` **复用了前面 k-means 段落里 make_blobs 生成的 150×2 数据**——notebook 风格代码的常见陷阱，读脚本时必须留意变量在哪一行被重新绑定。

[ch10/ch10.py:L347-L350](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L347-L350) —— 一行完成「浓缩再还原」：`pdist(df, metric='euclidean')` 算出 5×4/2 = 10 个两两距离，`squareform` 还原成 5×5 方阵，再用 `pd.DataFrame(..., columns=labels, index=labels)` 给行列都贴上 ID 标签，得到一张对角线为 0 的对称「距离表」。

#### 4.1.4 代码实践

1. **实践目标**：验证 `pdist` 输出的顺序约定，并体会浓缩/方阵两种形态的换算。
2. **操作步骤**：在本章环境（u1-l2 搭建的 pyml-book）中执行下面这段示例代码：

   ```python
   # 示例代码：验证 pdist 的顺序约定
   import numpy as np
   from scipy.spatial.distance import pdist, squareform

   X = np.array([[0, 0], [3, 4], [6, 8]])   # 三点共线，距离可手算
   cond = pdist(X, metric='euclidean')
   print(cond)                    # 期望 [5. 10. 5.]：(0,1), (0,2), (1,2)
   print(squareform(cond))
   print(squareform(squareform(cond)))   # 方阵再压回浓缩形态
   ```
3. **需要观察的现象**：`cond` 长度为 3（= 3×2/2），顺序按行对 (0,1)、(0,2)、(1,2) 排列；方阵对角线为 0 且对称。
4. **预期结果**：因为 (0,0)-(3,4) 距离为 \(\sqrt{3^2+4^2}=5\)，输出应为 `[5. 10. 5.]`。具体打印结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：5 个样本的浓缩距离矩阵长度是多少？100 个样本呢？

**答案**：\(\frac{5 \times 4}{2} = 10\)；\(\frac{100 \times 99}{2} = 4950\)。浓缩形态把存储从 \(O(n^2)\) 降到一半，且 `linkage` 只接受这种形态（或原始数据）。

**练习 2**：如果把 `metric='euclidean'` 换成 `metric='cityblock'`，距离矩阵会怎么变？

**答案**：cityblock（曼哈顿距离）把欧氏距离里的平方和开根号换成绝对值求和：\(d(i,j)=\sum_k |x_{ik}-x_{jk}|\)。同样的两点，曼哈顿距离 ≥ 欧氏距离（本节三点共线例子中 (0,0)-(3,4) 会从 5 变成 7）。度量选择会直接改变后续层次聚类的合并顺序。

### 4.2 linkage 与 dendrogram：从合并记录到树状图

#### 4.2.1 概念说明

`scipy.cluster.hierarchy.linkage` 执行凝聚聚类的核心步骤：每轮找到当前「簇间距离最小」的两簇合并，共执行 \(n-1\) 次，把整棵合并树记录成一个 \((n-1) \times 4\) 的矩阵。`dendrogram` 则把这棵树画出来：横轴是叶子（原始样本），纵轴是每次合并发生时的簇间距离——**纵轴高度越高，说明两个分支越晚合并、关系越疏远**。看树状图找簇的直觉：在某个高度「剪一刀」，被切开的分支就是簇。

#### 4.2.2 核心流程

linkage 输出矩阵的每一行是一次合并，四列分别是：

\[ \text{簇}_u,\quad \text{簇}_v,\quad d(u, v),\quad |\,c_u \cup c_v\,| \]

- 前两列是参与合并的两个簇的**编号**。约定：0 到 n-1 号是原始样本；第 i 次合并产生的新簇编号为 \(n + i\)（即 n 到 2n-2 号）。
- 第三列是合并时两簇的距离（按 `method` 定义的簇间距离计算）。
- 第四列是合并后新簇包含的样本数。

```text
重复 n-1 次：
    ① 按连接方式（complete/single/average）计算所有当前簇两两之间的距离
    ② 取距离最小的一对簇 (u, v) 合并为新簇，编号 n+i
    ③ 记录 [u, v, 距离, 新簇样本数]
dendrogram 把 ③ 的记录逐行画成 U 形连线，U 底的高度 = 合并距离
```

凝聚法的时间复杂度约为 \(O(n^3)\)（朴素实现），远高于 k-means 的每次迭代 \(O(nkt)\)，这是层次聚类在大数据集上的代价。

#### 4.2.3 源码精读

书里用三种写法调用 `linkage`，其中第一种是**故意演示的错误用法**：

[ch10/ch10.py:L361-L366](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L361-L366) —— 错误写法：把 `squareform` 还原后的**方阵** `row_dist` 直接传给 `linkage`。scipy 不会报错，而是把这个 5×5 矩阵当成「5 个 5 维样本」去重新计算距离——距离值完全错误。书中在 [ch10/ch10.py:L353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L353) 的注释里特别警告：应传 `pdist` 的浓缩矩阵，或直接传原始数据并指定 `metric`；传方阵「会得到不同的距离值，尽管整体聚类结果可能碰巧相同」。这是一个静默出错的典型陷阱。

[ch10/ch10.py:L373-L378](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L373-L378) —— 正确写法一：传浓缩距离矩阵 `pdist(df, metric='euclidean')`，并指定 `method='complete'`。输出被装进四列表（`row label 1`、`row label 2`、`distance`、`no. of items in clust.`）的 DataFrame，行索引是 `cluster 1` 到 `cluster 4`（n-1 = 4 次合并）。

[ch10/ch10.py:L385-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L385-L390) —— 正确写法二：传原始数据 `df.values`，同时指定 `metric='euclidean'` 与 `method='complete'`，结果与写法一等价。

[ch10/ch10.py:L400-L409](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L400-L409) —— 调用 `dendrogram(row_clusters, labels=labels)` 画树状图，纵轴标签 `ylabel('Euclidean distance')`。注释掉的 `color_threshold=np.inf` 可以让整棵树不按簇着色（配合 `set_link_color_palette(['black'])` 得到纯黑树状图）。

[ch10/ch10.py:L418-L443](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L418-L443) —— 树状图拼热图：先用 `fig.add_axes` 手工指定两个子图位置，树状图用 `orientation='left'` 横放（[L422](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L422)）；关键的**行列标注绘图技巧**在 [L425](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L425)：`df.iloc[row_dendr['leaves'][::-1]]` 按树状图叶子顺序**重排数据行**，使热图行序与树一致（`row_dendr['leaves']` 是 dendrogram 返回的叶子从下到上的顺序）；[L436](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L436) 用 `matshow(..., interpolation='nearest', cmap='hot_r')` 画热图；[L439-L440](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L439-L440) 的 `set_xticklabels([''] + list(...))` 开头多塞一个空字符串，是 `matshow` 刻度定位从 1 开始而标签列表从 0 对齐的 matplotlib 陷阱。

#### 4.2.4 代码实践

1. **实践目标**：对比 complete 与 single 两种连接方式对合并顺序的影响，学会读 linkage 输出表。
2. **操作步骤**：在跑通 L330-L390 的基础上执行示例代码：

   ```python
   # 示例代码：对比不同 method 的合并记录
   from scipy.cluster.hierarchy import linkage
   import pandas as pd

   for method in ['complete', 'single', 'average']:
       rc = linkage(pdist(df, metric='euclidean'), method=method)
       out = pd.DataFrame(rc,
                          columns=['row label 1', 'row label 2',
                                   'distance', 'no. of items in clust.'],
                          index=[f'cluster {i+1}' for i in range(rc.shape[0])])
       print(f'--- method={method} ---')
       print(out)
   ```
3. **需要观察的现象**：三张表的前两列（谁和谁合并）与第三列（合并距离）不同；complete 的合并距离普遍大于 single；新簇编号从 5（= n）开始。
4. **预期结果**：不同 `method` 得到不同的合并树；单看「最后一次合并」的距离，complete 通常明显更大（它取两簇最远点）。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：linkage 输出矩阵中，为什么新簇编号从 n 开始而不是从 0 开始？

**答案**：0 到 n-1 已被原始样本占用。第 i 次（从 0 计）合并产生的新簇编号为 n+i，这样前两列出现的每个编号都能无歧义地指向「原始样本」或「某次合并的产物」，画树状图时据此递归展开整棵树。

**练习 2**：树状图里两条最深的 U 形线意味着什么？如果想把它切成 2 个簇怎么办？

**答案**：U 形线越深（底部越高），说明两分支越晚才合并、相似度越低。在对应高度画一条水平线（想象剪一刀），线以上的连通分支就是簇；两刀最深的 U 形对应 2 个簇。`dendrogram` 返回值里的 `color_threshold` 参数控制着色分组，sklearn 的 `AgglomerativeClustering(n_clusters=2)` 则等价于自动在合适高度剪树。

**练习 3**：为什么 `linkage(row_dist, method='complete', metric='euclidean')` 不报错却是错的？

**答案**：`linkage` 对二维输入的解析规则是「每行一个样本」。传方阵时它把 n×n 的距离矩阵误认为 n 个 n 维样本，重新计算样本间距离，得到的第三列距离值毫无意义。scipy 不会检查你的意图，只能靠阅读文档（和书里 L353 的注释）避免。

### 4.3 AgglomerativeClustering：sklearn 版凝聚聚类与一个版本坑

#### 4.3.1 概念说明

scipy 的 `linkage` 只负责「算出整棵树」，要得到扁平的簇标签，可以剪树，也可以直接用 sklearn 的 `AgglomerativeClustering`：指定 `n_clusters`，它内部完成同样的凝聚过程并在达到目标簇数时停下，输出与 k-means 同款的 `fit_predict` 标签。两个关键参数：

- `linkage`：簇间距离定义（`'complete'` / `'single'` / `'average'` / `'ward'`）。
- `metric`（旧名 `affinity`）：计算点与点距离的度量（如 `'euclidean'`）。

注意区分：`metric` 决定**点**之间怎么量距离，`linkage` 决定**簇**之间怎么由点距汇总。sklearn 1.2 把 `affinity` 参数改名为 `metric` 并在 1.4 中彻底移除了旧名——书中代码为此做了版本分支，这也引出本模块要重点讲解的两个真实陷阱。

#### 4.3.2 核心流程

```text
AgglomerativeClustering(n_clusters=k, metric='euclidean', linkage='complete')
    │ fit_predict(X)
    ▼
① 视每行为一个样本，计算距离（metric）
② 循环：按 linkage 定义找最近的两簇合并
③ 剩下 k 个簇时停止，返回长度为 n 的标签数组
（无 predict 方法：新样本不能直接分派，需重新拟合或用 fit 的模型变换）
```

#### 4.3.3 源码精读

[ch10/ch10.py:L453-L465](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L453-L465) —— 用 `version.parse(sklearn.__version__) > version.parse("1.2")` 分支：新 sklearn 传 `metric="euclidean"`，旧版传 `affinity="euclidean"`，`n_clusters=3` 与 `linkage="complete"` 不变；随后 `ac.fit_predict(X)` 打印 5 个样本的簇标签。注意此处的 `X` 是 L335 覆盖后的 5×3 随机数据（不是开头的 make_blobs 数据），标签数组长度应为 5。

[ch10/ch10.py:L470-L482](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L470-L482) —— 同样的版本分支，把 `n_clusters` 改为 2 再跑一遍，观察同一份数据在不同簇数下的标签变化。

**两个真实的坑（读源码核实过，不是猜测）**：

1. **缺少 `import sklearn`**：ch10.py 与 ch10.ipynb 的导入区（[L3-L19](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L3-L19)）只有 `from sklearn.cluster import KMeans` 这类成员导入，它**不会**把 `sklearn` 这个名字绑定进当前命名空间；而 L453 / L470 却引用了 `sklearn.__version__`。在全新进程里从头运行会在 L453 抛出 `NameError: name 'sklearn' is not defined`。这段版本分支由 2024 年的提交 `f224410`（"affinity -> metric"）引入，作者漏加了 `import sklearn`。修复方式：在文件头部补一行 `import sklearn`。
2. **月牙对比段没有版本守卫**：后面 L526-L528 直接写 `affinity='euclidean'`（见 4.4.3），在 sklearn ≥ 1.4 的环境中会因旧参数被移除而报错。书中钉定的 sklearn 1.0 环境不受影响；用新环境复现代码时应把 `affinity` 改为 `metric`。

#### 4.3.4 代码实践

1. **实践目标**：亲手修复 `import sklearn` 缺失的问题，并观察 `n_clusters` 取 3 和 2 时标签的差异。
2. **操作步骤**：
   - 打开 `ch10/ch10.ipynb`（不要改仓库文件，复制到自己的工作目录），在导入单元格加一行 `import sklearn`；
   - 顺序执行到 L482 对应的两个单元格，记下两次打印的 `Cluster labels`。
3. **需要观察的现象**：第一次输出是长度 5、取值 {0,1,2} 的数组；第二次是长度 5、取值 {0,1} 的数组，且两种划分互相兼容（3 簇的某两簇在 2 簇时被合并）。
4. **预期结果**：标签数组长度均为 5；具体数值由 seed=123 的数据决定，待本地验证。若不补 `import sklearn`，第一个单元格就会 NameError。

#### 4.3.5 小练习与答案

**练习 1**：`AgglomerativeClustering` 与 `KMeans` 在接口上有一个重要差异：前者没有 `predict` 方法。为什么？

**答案**：k-means 有显式的质心，新样本只需找最近质心即可分派；凝聚聚类的簇由「样本间的合并历史」定义，没有可推广到新样本的中心或判别函数，新数据进来只能重新拟合。这也是它与第 3 章 KNN 类似的「惰性」一面。

**练习 2**：书中为什么在 L453 写版本分支，而不是统一用 `metric` 或统一用 `affinity`？

**答案**：`affinity` 在 sklearn 1.2 被弃用、1.4 被移除，`metric` 又只在 1.2 及以后存在。仓库钉定的环境是 sklearn 1.0（见 [L39-L46](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L39-L46) 的版本检查字典），而读者很可能用新版 sklearn 运行，分支让两边的解释器都不报错。这是维护长期教学代码应对上游 API 演进的常见手段。

**练习 3**：如果把 `linkage` 从 `'complete'` 换成 `'ward'`，`metric` 还能随便选吗？

**答案**：不能。Ward 连接最小化合并带来的簇内平方误差增量，其推导依赖欧氏距离，sklearn 要求 ward 只能搭配欧氏度量。complete/single/average 则可搭配任意 `metric`。

### 4.4 DBSCAN：密度可达与月牙形数据

#### 4.4.1 概念说明

DBSCAN（Density-Based Spatial Clustering of Applications with Noise）把「簇」重新定义为**密度相连的稠密区域**，对簇的形状不做任何假设。两个参数刻画密度：

- `eps`（\(\epsilon\)）：邻域半径。
- `min_samples`：构成「稠密」所需的最少邻居数（含点自身，依 sklearn 文档的计数方式）。

由此把每个点分为三类：

- **核心点**：\(\epsilon\)-邻域内的点数不少于 `min_samples`，即

  \[ |N_\epsilon(p)| \geq \text{min\_samples} \]

- **边界点**：自身不够稠密，但落在某个核心点的 \(\epsilon\)-邻域内。
- **噪声点**：两者都不是，最终标签为 **-1**。

**密度可达（directly density-reachable）**：点 q 在核心点 p 的 \(\epsilon\)-邻域内。**密度相连**：p、q 能通过一串互相密度可达的核心点连起来。一个簇就是所有密度相连点的极大集合——像墨水在纸上晕开，只要核心点彼此挨着，形状再弯曲也能连成一个簇。这正是它能分开月牙的原因：k-means 的质心把每个簇近似成圆形，层次聚类（complete 连接）偏好紧凑的团块，而月牙是细长弯曲的形状。

#### 4.4.2 核心流程

```text
DBSCAN(eps, min_samples)：
① 对每个点数一遍 ε-邻域内的邻居数 → 判定核心点
② 从任一未访问的核心点出发，沿「核心点的 ε-邻域」泛洪扩张，
   收集所有密度相连的点 → 形成一个簇（核心点 + 边界点）
③ 重复 ② 直到所有核心点都有归属
④ 剩下没进任何簇的点 → 噪声，标签 -1
```

参数直觉：

- `eps` 太小 → 大量点被误判为噪声、簇被切碎；`eps` 太大 → 不同簇被连通、合并成一团。
- `min_samples` 越大，对「稠密」要求越苛刻，核心点越少，噪声越多。数据维度越高，邻域越空，`eps` 越难选（可配合 k-距离图：把每个点到第 `min_samples` 近邻的距离排序，找拐点）。

#### 4.4.3 源码精读

[ch10/ch10.py:L496-L504](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L496-L504) —— 用 `make_moons(n_samples=200, noise=0.05, random_state=0)` 生成两弯交织的月牙（对应书中图 `figures/10_14.png`）。`X` 在这里第三次被重新绑定。noise 控制月牙的厚度——这个 0.05 的量级直接决定了后面 `eps=0.2` 是否够用。

[ch10/ch10.py:L511-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L511-L544) —— 失败对照实验（图 `10_15.png`）：左子图 `KMeans(n_clusters=2, random_state=0)`（[L513](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L513)），右子图 `AgglomerativeClustering(n_clusters=2, affinity='euclidean', linkage='complete')`（[L526-L528](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L526-L528)）。两者都会把月牙「拦腰切断」：k-means 用一条直线把两弯各劈一半，complete 层次聚类偏好大小均衡的紧凑簇，同样无法让一整弯月牙保持完整。注意这里 `affinity` 没有像 4.3 那样加版本守卫，sklearn ≥ 1.4 环境需自行改成 `metric`。

[ch10/ch10.py:L553-L570](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L553-L570) —— 成功实验（图 `10_16.png`）：`DBSCAN(eps=0.2, min_samples=5, metric='euclidean')`，`fit_predict` 得到的标签完美沿两条月牙各成一簇。书中的散点只画了标签 0 和 1 两个簇——在该参数下噪声点恰好为零或很少；在自己实验时应先 `np.unique(y_db)` 检查是否出现 -1，若有，用单独的灰色记号把噪声点画出来，而不是让它们悄悄混进 `X[y_db == -1]` 的筛选之外。

#### 4.4.4 代码实践

1. **实践目标**：体会 `eps` 对 DBSCAN 结果的敏感性，学会用「簇数 + 噪声点数」体检结果。
2. **操作步骤**：在月牙数据上扫描 `eps`，示例代码：

   ```python
   # 示例代码：eps 敏感性实验
   import numpy as np
   from sklearn.cluster import DBSCAN

   for eps in [0.05, 0.1, 0.2, 0.4, 0.8]:
       y_db = DBSCAN(eps=eps, min_samples=5).fit_predict(X)
       labels = np.unique(y_db)
       n_noise = int(np.sum(y_db == -1))
       print(f'eps={eps}: 簇标签 {labels}, 噪声点 {n_noise} 个')
   ```
3. **需要观察的现象**：`eps` 很小时噪声点很多、可能没有成形的簇；逐渐增大时先正确分出 2 簇；过大时两弯月牙被连通成 1 簇。
4. **预期结果**：在 0.1~0.4 区间内应出现「2 个簇 + 少量或零噪声」的窗口，书里选的 0.2 落在其中；各 `eps` 的具体簇数待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：DBSCAN 标签里的 -1 是什么意思？k-means 会有类似输出吗？

**答案**：-1 表示噪声点——既不是核心点也不在任何核心点邻域内的样本。k-means 强制每个样本归入最近质心，永远没有 -1；因此数据含离群点时，k-means 的质心还会被离群点拉偏，而 DBSCAN 天然把离群点隔离出来。

**练习 2**：把 `min_samples` 从 5 调到 50（eps 不变），月牙数据会发生什么？

**答案**：核心点判据变苛刻，绝大多数点不再满足 \( |N_\epsilon(p)| \geq 50 \)，核心点稀少导致泛洪断链，月牙会碎成很多小簇并伴随大量噪声点。`min_samples` 的经验起点是 `2 × 数据维度`，再按噪声比例微调。

**练习 3**：`AgglomerativeClustering(n_clusters=2, linkage='single')`（single 连接）在月牙数据上大概率能成功，这与 4.4.3 中 complete 连接的失败矛盾吗？

**答案**：不矛盾，恰恰印证了「算法的形状偏好」这条主线。single 连接取两簇最近点的距离，能沿细长形状链式延伸，因此分得开有间隙的两弯月牙；complete 连接偏好紧凑团块所以失败。但这不意味着 single 万能：它对桥接噪声极其脆弱，几个散点就能把两个本应分开的簇串起来。具体结果待本地验证。

## 5. 综合实践

**任务：月牙 + 圆环混合数据的三算法对决。** 设计一份数据，让 k-means、complete 层次聚类、DBSCAN 的「形状偏好」差异一次性暴露出来。

1. **实践目标**：验证「只有 DBSCAN 能同时分离月牙和圆环」，并用密度语言解释原因。
2. **操作步骤**（示例代码，可直接跑在本章环境里）：

   ```python
   # 示例代码：月牙 + 圆环三算法对比
   import numpy as np
   import matplotlib.pyplot as plt
   from sklearn.datasets import make_moons, make_circles
   from sklearn.cluster import KMeans, AgglomerativeClustering, DBSCAN

   # ① 月牙（两簇）+ 圆环（一簇，整体平移到不与月牙重叠的位置）
   moons, _ = make_moons(n_samples=200, noise=0.05, random_state=0)
   ring, _ = make_circles(n_samples=200, noise=0.05, factor=0.4, random_state=2)
   ring = ring + np.array([3.0, 3.0])        # 平移圆环，远离月牙
   X = np.vstack([moons, ring])

   # ② 三种算法
   y_km = KMeans(n_clusters=2, random_state=0).fit_predict(X)
   y_ac = AgglomerativeClustering(n_clusters=2, metric='euclidean',
                                  linkage='complete').fit_predict(X)
   y_db = DBSCAN(eps=0.2, min_samples=5).fit_predict(X)

   # ③ 三张着色图；DBSCAN 的噪声点(-1)单独用黑色 x 画出来
   fig, axes = plt.subplots(1, 3, figsize=(15, 4))
   for ax, y, title in [(axes[0], y_km, 'KMeans (k=2)'),
                        (axes[1], y_ac, 'Agglomerative (complete)'),
                        (axes[2], y_db, 'DBSCAN (eps=0.2)')]:
       for lab in np.unique(y):
           mask = (y == lab)
           ax.scatter(X[mask, 0], X[mask, 1], s=25,
                      c='gray' if lab == -1 else None, marker='x' if lab == -1 else 'o',
                      label=f'noise' if lab == -1 else f'cluster {lab}')
       ax.set_title(title)
       ax.legend(fontsize=8)
   plt.tight_layout()
   plt.show()

   print('DBSCAN 簇标签:', np.unique(y_db), ' 噪声点:', int(np.sum(y_db == -1)))
   ```

3. **需要观察的现象**：
   - KMeans(k=2) 把整个数据集大致沿某个方向劈成两半，月牙和圆环都被切断；
   - complete 层次聚类同样给出两个「几何上规整但语义上错误」的团块；
   - DBSCAN 的 `np.unique(y_db)` 应出现 3 个簇标签（两条月牙各一 + 圆环一），可能还有少量 -1 噪声点。
4. **预期结果**：只有 DBSCAN 同时还原了「2 弯月牙 + 1 个圆环」共 3 个非凸簇。解释要点：k-means 的隐式假设是「簇 = 以质心为球心的凸区域」，n_clusters=2 也限死了它最多给 2 个簇；complete 连接偏好紧凑等大的团块，月牙和圆环这种中空 / 细长形状必被拆散；DBSCAN 不假设形状，只要 `eps` 内密度连成一片就算一簇，与「一共有几簇」无关——它甚至**不需要你预先指定簇数**。若圆环与月牙出现连通（合并成一簇），说明平移量或 `eps` 需要调整：把平移加大或把 `eps` 调小。具体簇数与噪声点数待本地验证。

## 6. 本讲小结

- 层次聚类的原料是距离矩阵：`pdist` 产出浓缩形态（长度 \(\frac{n(n-1)}{2}\)），`squareform` 还原成方阵；**给 `linkage` 传方阵是书中点名的静默错误**，要么传浓缩矩阵、要么传原始数据加 `metric`。
- `linkage` 的 \((n-1) \times 4\) 输出是逐次合并的流水账，新簇编号从 n 开始；`dendrogram` 画出合并树，纵轴即合并距离，剪一刀即得簇；热图拼接的关键是按 `row_dendr['leaves']` 重排数据行。
- sklearn 的 `AgglomerativeClustering` 用 `n_clusters` + `linkage`（簇间距离）+ `metric`（点间距离，旧名 `affinity`，sklearn 1.2 改名、1.4 移除）直接输出标签；书中代码的两处真实坑：L453 起的版本分支引用了从未 `import` 的 `sklearn`，L526 的 `affinity` 未加版本守卫。
- DBSCAN 用 `eps` 与 `min_samples` 定义稠密，把点分为核心点 / 边界点 / 噪声点（标签 -1），簇是密度相连的极大集合：**不指定簇数、不假设形状、自带离群点检测**。
- 形状偏好总结：k-means 凸球形、complete 层次紧凑团块、single 层次可沿细长形状延伸但怕链式噪声、DBSCAN 任意形状但要求密度均匀且对 `eps` 敏感。
- 上一讲的肘部法 / 轮廓系数针对「需要 k 的算法」；DBSCAN 的调参改用簇数—噪声点体检与 k-距离图，两讲合起来构成无监督学习的完整工具箱。

## 7. 下一步学习建议

- **衔接下一单元**：ch10 是经典机器学习部分的最后一章。下一讲进入 u11-l1「NeuralNetMLP 前向传播」，从零手写多层神经网络。你会发现它的 `forward` 本质上还是本单元反复出现的「加权求和 + 激活」，而训练用的 minibatch 随机梯度下降正是 u2-l3 `AdalinSGD` 的多层推广——建议回顾 ch02 的梯度更新式再出发。
- **继续阅读源码**：通读 [ch10/ch10.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.ipynb) 中本讲跳过的第 10 章前半部分（k-means、肘部法、轮廓图），把两种「选 k」工具与 DBSCAN 的免 k 特性对照；对照书中图 `figures/10_11.png`~`10_16.png` 检查自己的运行结果。
- **延伸阅读**（利用本讲学到的读文档习惯）：scipy 文档中 `pdist` 支持的度量列表与 `linkage` 的 `method` 说明；sklearn 用户指南中 DBSCAN 一节的 k-距离图调参建议，以及 `cluster.DBSCAN` 与 `cluster.HDBSCAN`（自适应密度，新版本 sklearn 提供）的关系。
