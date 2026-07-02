# 项目总览：scipy.cluster 是什么、解决什么问题

## 1. 本讲目标

本讲是整个 `scipy.cluster` 学习手册的第一篇。读完后你应当能够：

- 说清楚 `scipy.cluster` 这个包是做什么的，它把哪些算法归在一处；
- 区分它的两个子模块 `vq`（向量量化 / k-means）与 `hierarchy`（层次 / 凝聚式聚类）各自的职责；
- 掌握聚类、向量量化、码本（code book）、畸变（distortion）这几个核心术语；
- 看懂三个 `__init__.py` 里 `__all__` 与 `from . import ...` 决定的「对外接口面」，知道 `scipy.cluster` 到底把哪些名字暴露给用户。

本讲**不**深入任何算法实现，只做「定位」和「认路」。后续讲义才会逐层拆开 `_vq_impl.py`、`_hierarchy_impl.py` 等实现文件。

## 2. 前置知识

在阅读源码前，先用最朴素的语言把几个名词讲清楚。

### 2.1 聚类（clustering）

聚类是一种**无监督学习**任务：给你一堆数据点，没有任何标签，你希望把「彼此相似」的点归到同一组（簇 / cluster）。它和分类的区别是——分类事先知道有哪些类别，而聚类事先不知道。

### 2.2 向量量化（vector quantization）

向量量化来自**信息论与压缩**领域。把每个数据点看作一个「向量」，我们希望用一个**有限的小集合**（比如 256 个代表向量）去近似表示无穷多的原始向量。每个代表向量叫一个**码字（code word）**，所有码字构成的表叫**码本（code book）**。原始向量被替换成「离它最近的码字的编号」，这个编号就叫**code**。

为什么这和 k-means 是同一件事？因为「找一组码字使总误差最小」的数学目标，和 k-means「找一组簇心使总畸变最小」完全等价。所以 k-means 天然就是构造码本的算法，这也正是源码 docstring 里那句「vector quantization is a natural application for k-means」的含义。

### 2.3 畸变（distortion）

k-means 的优化目标是**畸变**，定义为每个观测向量到它所属簇心（dominating centroid）的平方距离之和：

\[
\text{distortion} = \sum_{i} \| x_i - c_{\text{dom}(i)} \|^2
\]

其中 \(x_i\) 是第 \(i\) 个观测，\(c_{\text{dom}(i)}\) 是离它最近的簇心。算法通过反复「重新分配 → 重算簇心」来不断降低畸变，直到簇心稳定。

### 2.4 层次聚类（hierarchical / agglomerative clustering）

层次聚类不走「先定簇数 k」的路子，而是从「每个点自成一簇」开始，每一步把最相似的两簇合并，最终合并成一棵**树**（树状图 / dendrogram）。这种「自底向上逐步合并」的方式叫**凝聚式（agglomerative）**。树的好处是：你可以事后在任意高度「切一刀」得到任意数量的簇，而不用重新跑算法。

> 小结：`vq` 关心「给定 k 个簇心怎么把点分进去、怎么迭代出好簇心」；`hierarchy` 关心「不预设簇数，先把所有可能的合并层次算出来，再决定在哪一层切平」。

## 3. 本讲源码地图

本讲只涉及三个文件，它们都是「入口 / 导出」性质的 `__init__.py`，本身不含算法，只负责声明对外接口。

| 文件 | 作用 |
| --- | --- |
| [scipy/cluster/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py) | `scipy.cluster` 顶层包入口，只声明两个子模块 `vq`、`hierarchy`。 |
| [scipy/cluster/vq/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py) | `scipy.cluster.vq` 子模块入口，导出 `whiten`/`vq`/`kmeans`/`kmeans2`/`ClusterError`，并给出 k-means 与向量量化的背景说明。 |
| [scipy/cluster/hierarchy/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py) | `scipy.cluster.hierarchy` 子模块入口，导出 `linkage`/`fcluster`/`dendrogram` 等约 30 个名字，按功能分组。 |

> 一句话定位：`cluster/__init__.py` 是「门牌」，`vq/__init__.py` 与 `hierarchy/__init__.py` 是「两间屋子各自挂在门口的功能牌」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：顶层包、`vq` 子模块、`hierarchy` 子模块。

### 4.1 cluster.__init__：scipy.cluster 的顶层入口

#### 4.1.1 概念说明

`scipy.cluster` 本身**不直接提供算法函数**，它只是一个「容器包」，把两套相对独立的聚类算法——向量量化（`vq`）和层次聚类（`hierarchy`）——收纳在同一个命名空间下。顶层 `__init__.py` 的唯一职责就是：声明这个包对外只暴露这两个子模块，并提供一个 `test()` 入口方便运行测试。

这种「顶层只做聚合」的设计意味着：用户实际调用的函数都来自 `scipy.cluster.vq.xxx` 或 `scipy.cluster.hierarchy.xxx`，而不是 `scipy.cluster.xxx`。

#### 4.1.2 核心流程

顶层包加载时只做三件事：

1. 写一段 docstring，说明 `vq` 与 `hierarchy` 各自负责什么；
2. 用 `__all__` 声明「对外只暴露 `vq` 和 `hierarchy` 这两个名字」；
3. `from . import vq, hierarchy` 真正把两个子模块加载进来；
4. 附带挂一个 `test` 对象（`PytestTester`），让用户能 `scipy.cluster.test()` 跑该包的测试。

#### 4.1.3 源码精读

顶层 docstring 一开篇就给整个包定了调子——聚类算法「在信息论、目标检测、通信、压缩等领域有用」，并立刻划清两个子模块的边界：

> `cluster/__init__.py` 顶部 docstring 说明 vq 只支持向量量化与 k-means，hierarchy 提供层次/凝聚式聚类：[\_\_init\_\_.py:8-16](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L8-L16)

```python
Clustering algorithms are useful in information theory, target detection,
communications, compression, and other areas. The `vq` module only
supports vector quantization and the k-means algorithms.

The `hierarchy` module provides functions for hierarchical and
agglomerative clustering. ...
```

紧接着的导出声明非常干净，只有两行核心：

> `cluster/__init__.py` 用 `__all__` 和 `from . import` 把对外接口限定为两个子模块：[\_\_init\_\_.py:25-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L25-L27)

```python
__all__ = ['vq', 'hierarchy']

from . import vq, hierarchy
```

最后挂上测试入口（这是 SciPy 全包通用的惯例，不是 cluster 特有的逻辑）：

> 顶层 `test` 来自 `PytestTester`，运行 `scipy.cluster.test()` 即可执行本包的 pytest 测试：[\_\_init\_\_.py:29-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L29-L31)

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

#### 4.1.4 代码实践

这是一个「源码阅读型」实践，目标是亲手确认「顶层包确实只是个容器」。

1. **实践目标**：验证 `scipy.cluster` 命名空间里除了 `vq`、`hierarchy` 之外，没有别的算法函数。
2. **操作步骤**：在装好 SciPy 的环境里执行：
   ```python
   import scipy.cluster as c
   print([n for n in dir(c) if not n.startswith('_')])
   ```
3. **需要观察的现象**：输出里应该出现 `vq`、`hierarchy`、`test`，而**不**会出现 `kmeans`、`linkage` 这类具体函数名。
4. **预期结果**：你看到的是子模块对象，而非算法函数；这印证了顶层只做聚合。若你的环境没装 SciPy，可改成直接对照源码第 25 行 `__all__` 得出同样结论（**待本地验证**：实际 `dir()` 输出依赖你的 SciPy 版本）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `scipy.cluster` 顶层没有 `kmeans`？要调用它该写完整路径吗？

**参考答案**：因为顶层 `__all__` 只导出了 `vq`、`hierarchy` 两个子模块，`kmeans` 定义在 `vq` 子模块里。完整调用应写 `scipy.cluster.vq.kmeans`（或先 `from scipy.cluster import vq` 再 `vq.kmeans`）。

**练习 2**：`scipy.cluster.test()` 是从哪里来的？它和聚类算法有关吗？

**参考答案**：它来自 [__init__.py:29-31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py#L29-L31) 的 `PytestTester(__name__)`，是 SciPy 全包通用的测试运行器，和聚类算法本身无关，只是方便开发者一键跑测试。

### 4.2 vq 子模块：向量量化与 k-means

#### 4.2.1 概念说明

`vq`（vector quantization）子模块提供 k-means 聚类及其周边能力。它的核心是四个函数：

- `whiten`：把观测按特征（列）归一化到「单位方差」，是 k-means 常用的预处理；
- `vq`：给定码本，把每个观测分配到最近的码字，返回「code（最近码字编号）+ dist（畸变）」；
- `kmeans`：跑一次或多次 k-means，迭代出簇心（码本），返回「码本 + 平均畸变」；
- `kmeans2`：`kmeans` 的另一套实现，提供更多初始化方式（如 k-means++）和空簇处理策略。

还有一个异常类 `ClusterError`，当 k-means 出现「空簇」等异常时抛出。

#### 4.2.2 核心流程

从用户视角，一次典型的 k-means 向量量化流程是：

1. 拿到观测矩阵 `obs`（M 行 N 列，每行一个观测，每列一个特征）；
2. 用 `whiten(obs)` 把各列缩放到单位方差；
3. 用 `kmeans(obs_whitened, k)` 得到码本 `codebook`（k 行 N 列）；
4. 用 `vq(obs_whitened, codebook)` 把每个观测编码成最近的码字编号。

`vq/__init__.py` 的 docstring 用一个**图像压缩**的例子把这套流程讲透了：一张 24-bit 彩色图（每像素 R/G/B 各一字节），用 `kmeans(k=256)` 学出 256 个代表颜色作为码本，之后每像素只传 8-bit 的码字编号，从而把数据量压到原来的三分之一。这正是「向量量化 = 压缩」的直觉来源。

#### 4.2.3 源码精读

`vq/__init__.py` 的 docstring 顶部的 `autosummary` 块，正是该子模块对外的「功能牌」，列出四个函数和一个异常：

> vq 子模块 docstring 用 autosummary 列出 whiten/vq/kmeans/kmeans2 与 ClusterError：[vq/\_\_init\_\_.py:9-23](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L9-L23)

```python
whiten -- Normalize a group of observations so each feature has unit variance
vq -- Calculate code book membership of a set of observation vectors
kmeans -- Perform k-means on a set of observation vectors forming k clusters
kmeans2 -- A different implementation of k-means with more methods
        -- for initializing centroids
```

docstring 的「Background information」一节用信息论术语点明了 k-means 与向量量化的等价性，并给出了畸变的精确定义（平方距离之和）：

> vq docstring 说明 k-means 最小化畸变，并把向量量化里 code / code book 的术语对应过来：[vq/\_\_init\_\_.py:43-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L43-L49)

```python
Since vector quantization is a natural application for k-means,
information theory terminology is often used. The centroid index
or cluster index is also referred to as a "code" and the table
mapping codes to centroids ... is often referred to as a "code book".
```

紧接着 docstring 用一段约定固定了**输入输出形状**——这是后续所有算法实现的共同契约：

> vq docstring 约定 obs 为 M×N、codebook 为 k×N，二者特征维度相同：[vq/\_\_init\_\_.py:51-54](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L51-L54)

```python
All routines expect obs to be an M by N array, where the rows are
the observation vectors. The codebook is a k by N array, where the
ith row is the centroid of code word i.
```

最后是真正的导入语句——注意所有函数其实来自一个**实现模块** `_vq_impl`（下划线开头，表示「私有」，不应对用户直接使用）：

> vq 从实现模块 `_vq_impl` 导入全部对外函数：[vq/\_\_init\_\_.py:74-77](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74-L77)

```python
from ._vq_impl import ClusterError, kmeans, kmeans2, vq, whiten
from ._vq_impl import py_vq

__all__ = ["ClusterError", "kmeans", "kmeans2", "vq", "whiten"]
```

> 备注：`py_vq` 是被标记为 deprecated（已弃用）的旧版纯 Python 实现，它被加进了 `__all__` 但注释里写明「deprecated attributes」（[vq/\_\_init\_\_.py:79-80](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L79-L80)）。初学者可以忽略它，重点放在 `kmeans`/`vq`/`whiten` 上。

#### 4.2.4 代码实践

1. **实践目标**：亲手跑通「whiten → kmeans → vq」三步，体会 k-means 如何把码本（簇心）学出来，再用 `vq` 把观测编码成码字编号。
2. **操作步骤**：在装好 SciPy 的环境里执行：
   ```python
   import numpy as np
   from scipy.cluster.vq import whiten, kmeans, vq

   # 8 个二维观测，肉眼看分两堆：左边 4 个、右边 4 个
   obs = np.array([[1, 1], [1.1, 0.9], [0.9, 1.1], [1.0, 1.0],
                   [5, 5], [5.1, 4.9], [4.9, 5.1], [5.0, 5.0]])
   w = whiten(obs)              # 1. 各列归一化到单位方差
   codebook, distortion = kmeans(w, 2)   # 2. 学出 2 个簇心（码本）
   codes, dist = vq(w, codebook)         # 3. 每个观测编码成最近码字编号
   print("codes =", codes)               # 前 4 个应为同一编号，后 4 个为另一编号
   ```
3. **需要观察的现象**：`codes` 应当形如 `[0,0,0,0,1,1,1,1]`（具体 0/1 哪组在前取决于初始化），即左堆 4 个点共享一个码字、右堆 4 个点共享另一个码字。
4. **预期结果**：8 个点被正确分成两组，且同一组的 code 相同。若你的 SciPy 版本默认 `kmeans` 多次重启，结果会稳定。**待本地验证**：由于 `kmeans` 内部用到随机种子，极端情况下分组编号可能互换，但「两堆分开」的结论不变。

#### 4.2.5 小练习与答案

**练习 1**：`whiten` 这一步为什么有必要？跳过它会怎样？

**参考答案**：`whiten` 让每个特征（列）具有单位方差，避免某个量纲很大、方差很大的特征「独霸」距离计算。跳过它，k-means 会主要按那个大方差特征聚类，相当于无意识地加权了某些维度。

**练习 2**：`vq/__init__.py` 里函数真正定义在哪个文件？为什么用下划线开头？

**参考答案**：定义在 `_vq_impl.py`（[vq/\_\_init\_\_.py:74](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74)）。下划线开头是 Python 惯例，表示「内部实现，请勿直接 import」，用户只应通过 `scipy.cluster.vq.kmeans` 这样的公开路径访问。本系列后续讲义会深入这个 `_vq_impl.py`。

### 4.3 hierarchy 子模块：层次聚类与树操作

#### 4.3.1 概念说明

`hierarchy` 子模块是 `scipy.cluster` 里更大的一块，围绕「层次 / 凝聚式聚类」提供一整套工具。它的对外函数可大致分成五组：

- **凝聚式聚类入口**：`linkage`（总入口）和 `single`/`complete`/`average`/`weighted`/`centroid`/`median`/`ward` 七个方法包装；
- **切平 / 取簇**：`fcluster`（按多种准则把树切成 flat 簇）、`fclusterdata`（一站式流水线）、`leaders`（找每簇代表）；
- **统计量**：`cophenet`（cophenetic 距离与相关系数）、`inconsistent`（不一致矩阵）、`maxdists`/`maxinconsts`/`maxRstat`；
- **树的表示与可视化**：`ClusterNode`、`to_tree`、`cut_tree`、`leaves_list`、`optimal_leaf_ordering`、`dendrogram`、`set_link_color_palette`；
- **校验与数据结构**：`is_valid_linkage`/`is_valid_im`/`is_monotonic`/`correspond`/`num_obs_linkage`/`is_isomorphic`，外加 `DisjointSet`（并查集）。

本讲只需记住三个名字：`linkage`（建树）、`fcluster`（切树）、`dendrogram`（画树），它们构成层次聚类的最小工作链。

#### 4.3.2 核心流程

一次典型的层次聚类流程是：

1. 用 `pdist`（来自 `scipy.spatial.distance`）算出原始观测两两之间的距离；
2. 用 `linkage(距离, method='ward')` 把这些距离「自底向上」逐步合并，得到一棵树，结果存成一个 (n-1)×4 的 **linkage matrix Z**；
3. 用 `fcluster(Z, t, criterion='...')` 在树上的某个阈值处「切一刀」，得到每个观测的 flat 簇标签；
4. （可选）用 `dendrogram(Z)` 把树画出来。

注意：与 `vq` 不同，`hierarchy` 的主输入通常是**距离矩阵**而不是原始观测，这让它对「非欧氏距离」也很友好。

#### 4.3.3 源码精读

`hierarchy/__init__.py` 的 docstring 把所有对外函数按功能分了组。凝聚式聚类那一组列出了 `linkage` 总入口和七个方法：

> hierarchy docstring 列出 linkage 与 single/complete/average/weighted/centroid/median/ward 七种方法：[hierarchy/\_\_init\_\_.py:18-30](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L18-L30)

```python
.. autosummary::

   linkage
   single
   complete
   average
   weighted
   centroid
   median
   ward
```

切平与可视化相关的 `fcluster`/`dendrogram` 也在 docstring 里分组列出：

> hierarchy docstring 列出 fcluster/fclusterdata/leaders 与 dendrogram 等切分/可视化函数：[hierarchy/\_\_init\_\_.py:11-50](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L11-L50)

真正把这些名字「实例化」的是文件末尾的一大段 import——所有函数来自实现模块 `_hierarchy_impl`：

> hierarchy 从实现模块 `_hierarchy_impl` 批量导入约 30 个对外名字：[hierarchy/\_\_init\_\_.py:100-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L100-L107)

```python
from ._hierarchy_impl import (
    ClusterNode, ClusterWarning, DisjointSet, average, centroid, complete, cophenet,
    correspond, cut_tree, dendrogram, fcluster, fclusterdata, from_mlab_linkage,
    inconsistent, is_isomorphic, is_monotonic, is_valid_im, is_valid_linkage, leaders,
    leaves_list, linkage, maxRstat, maxdists, maxinconsts, median, num_obs_linkage,
    optimal_leaf_ordering, set_link_color_palette, single, to_mlab_linkage, to_tree,
    ward, weighted
)
```

而 `__all__` 列表（[hierarchy/\_\_init\_\_.py:109-116](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L109-L116)）与上面的 import 一一对应，锁定了子模块的对外接口面。注意其中还混入了两个「跨模块借来的」工具：`DisjointSet`（来自 `scipy._lib._disjoint_set`，并查集数据结构）和 `ClusterWarning`（自定义告警类）——它们是后续讲义会单独讲的工程化主题。

#### 4.3.4 代码实践

1. **实践目标**：跑通「linkage → fcluster」最小链路，直观看到层次聚类如何「不预设簇数也能给出分组」。
2. **操作步骤**：
   ```python
   import numpy as np
   from scipy.spatial.distance import pdist
   from scipy.cluster.hierarchy import linkage, fcluster

   # 复用 4.2 里那 8 个点
   obs = np.array([[1,1],[1.1,0.9],[0.9,1.1],[1.0,1.0],
                   [5,5],[5.1,4.9],[4.9,5.1],[5.0,5.0]])
   Z = linkage(pdist(obs), method='ward')   # 建树，Z 形状 (7, 4)
   labels = fcluster(Z, t=2, criterion='maxclust')   # 切成 2 个簇
   print("labels =", labels)                # 前 4 个为一簇，后 4 个为另一簇
   print("Z shape =", Z.shape)              # (7, 4) = (n-1, 4)
   ```
3. **需要观察的现象**：`labels` 把前 4 个点和后 4 个点分成两组（编号可能是 1/2，与 vq 的 0/1 不同）；`Z` 的形状是 `(7, 4)`，即 8 个点合并 7 次得到 4 列记录。
4. **预期结果**：分组结果与 4.2 的 k-means 一致（都是左堆 / 右堆两簇），但层次聚类是「先建完整棵树再切」，而 k-means 是「一开始就指定 k=2」。**待本地验证**：`fcluster` 的具体标签编号取决于树的内部顺序，可能不是严格的 `[1,1,1,1,2,2,2,2]`，但分组归属应一致。

#### 4.3.5 小练习与答案

**练习 1**：`linkage` 的输入通常是「距离矩阵」而非「原始观测」，这有什么好处？

**参考答案**：因为层次聚类只依赖点与点之间的距离。直接喂距离矩阵，意味着你可以用任意自定义距离（字符串相似度、编辑距离等非欧氏度量），只要能算出两两距离就能聚类，灵活性远高于必须用欧氏距离的算法。

**练习 2**：`hierarchy/__init__.py` 里 `__all__` 大约有 30 个名字，而本讲只重点讲了 3 个。请按 docstring 的分组，说出其余名字分别属于哪一组。

**参考答案**：参照 [hierarchy/\_\_init\_\_.py:11-90](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L11-L90) 的分组：`single/.../ward` 属「凝聚式聚类」；`fcluster/fclusterdata/leaders` 属「切平取簇」；`cophenet/inconsistent/maxdists/maxinconsts/maxRstat/from_mlab_linkage/to_mlab_linkage` 属「统计量」；`ClusterNode/leaves_list/to_tree/cut_tree/optimal_leaf_ordering` 属「树表示」；`dendrogram/set_link_color_palette` 属「可视化」；`is_valid_*`/`is_isomorphic`/`is_monotonic`/`correspond`/`num_obs_linkage` 属「校验」；`DisjointSet` 属「数据结构」。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务（纯阅读 + 一段对比代码）：

1. 打开三个 `__init__.py`，分别用**一句话**概括 `vq` 和 `hierarchy` 的核心能力，写在你的笔记里。参考答案：
   - `vq`：提供 k-means 聚类与向量量化，把观测迭代压缩成一组代表向量（码本）。
   - `hierarchy`：提供层次 / 凝聚式聚类，从距离矩阵建出一棵合并树，并支持切平、统计、可视化与校验。
2. 写一段注释（贴在你的练习脚本顶部），用自己的话解释**为什么向量量化是 k-means 的天然应用**。要点提示：两者的数学目标都是「选 k 个代表点，使所有点到其代表点的平方距离之和（畸变）最小」；k-means 解出来的簇心天然就是一组最优码字，`vq` 则是把新向量映射到最近码字编号。
3. 用**同一组** 8 个二维点，分别跑 `vq.kmeans(k=2)` 和 `hierarchy.linkage+fcluster(2簇)`，对比两者的输出形态：
   - k-means 给你「簇心（码本）+ 编码」；
   - 层次聚类给你「linkage matrix Z + 簇标签」。
   观察并记录：两者对「分两堆」这件事的结论是否一致？输出的数据结构有何不同？这能帮你建立「同一问题、两套思路」的直觉。

## 6. 本讲小结

- `scipy.cluster` 是一个**容器包**，顶层 [__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/__init__.py) 只暴露 `vq` 和 `hierarchy` 两个子模块，本身不含算法函数。
- `vq` 子模块聚焦 **k-means 与向量量化**，对外核心是 `whiten`/`vq`/`kmeans`/`kmeans2`，术语上「簇心 = 码字」「簇心集合 = 码本」「簇编号 = code」，优化目标是**畸变**（平方距离之和）。
- `hierarchy` 子模块聚焦**层次 / 凝聚式聚类**，对外约 30 个名字按「建树 / 切平 / 统计 / 树表示 / 可视化 / 校验」分组，最小工作链是 `linkage → fcluster → dendrogram`。
- 两个子模块的真正实现都藏在带下划线的「私有」模块里（`_vq_impl`、`_hierarchy_impl`），`__init__.py` 只负责把它们重新导出为公开 API。
- k-means 与向量量化在数学上等价（都是最小化畸变），这是 `vq` 子模块名字的由来。
- `vq` 通常喂原始观测矩阵，`hierarchy` 通常喂距离矩阵——这一输入差异决定了两者适用场景的不同。

## 7. 下一步学习建议

本讲只看了「门牌」，还没进任何一间屋子。建议按以下顺序继续：

1. **先读 [u1-l2](u1-l2-structure-and-build.md)**：了解 `cluster/` 的目录结构、Meson 构建方式，以及「Python 实现层 `_*_impl.py` + Cython 编译层 `_*.pyx`」的双层架构——这将解释为什么 `_vq_impl`、`_hierarchy_impl` 背后还有一层编译代码。
2. **再读 [u1-l3](u1-l3-quickstart-end-to-end.md)**：用一个端到端例子把 `whiten→kmeans` 与 `pdist→linkage→fcluster` 两条链路完整跑通。
3. 之后再按兴趣分两条主线深入：想搞懂 k-means 就进第 2 单元（`vq`），想搞懂层次聚类就进第 3 单元（`hierarchy` 的 linkage matrix 与 `linkage()` 入口）。
