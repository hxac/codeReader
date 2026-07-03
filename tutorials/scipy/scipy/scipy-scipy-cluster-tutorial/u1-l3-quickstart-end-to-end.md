# 快速上手：从原始数据到聚类结果端到端示例

## 1. 本讲目标

前两讲我们已经认了路：`scipy.cluster` 是一个容器包，对外暴露 `vq`（向量量化 / k-means）和 `hierarchy`（层次 / 凝聚式聚类）两个子模块，公共 API 都重导出自带下划线的 `_*_impl` 实现层，热点循环则交给 Cython 编译后端。本讲不再讲定位、目录和构建，而是**真正跑起来**。

读完本讲，你应当能够：

- 用 `whiten` + `kmeans` 在一组二维点上跑通一个 k-means 聚类，读懂返回的「码本」和「畸变」。
- 用 `pdist` + `linkage` + `fcluster` 跑通一个层次聚类，读懂返回的 linkage matrix `Z` 和扁平簇标签。
- 牢牢记住三类核心输入/输出约定：观测矩阵 `obs`（M×N）、码本 `code_book`（k×N）、linkage matrix `Z`（(n-1)×4）。
- 理解为什么「向量量化」和「k-means」在数学上是同一件事，以及层次聚类「自底向上合并」的整体手感。

本讲只求**整体跑通、建立直觉**，函数内部的迭代细节、七种 linkage 方法的分发、Cython 后端的算法实现，都留给后续进阶讲义（u2、u3、u5）深入。

## 2. 前置知识

本讲假设你已经读过 [u1-l1 项目总览](u1-l1-project-overview.md) 和 [u1-l2 目录结构与双层架构](u1-l2-structure-and-build.md)。下面几个词会反复出现，先用一句话回顾：

- **观测矩阵（observation matrix）**：一个 M×N 的二维数组，每一行是一个观测点，每一列是一个特征。这是 `vq` 全家桶的标准输入。
- **码本（code book）**：一个 k×N 的二维数组，每一行是一个「码字」，也就是一个簇心（centroid）。码本是 k-means 的产物，也是 `vq` 函数的输入。
- **code / 标签**：每个观测被分配到最近簇心后得到的编号。`vq` 用 0 起编号，`fcluster` 用 1 起编号——这是一个容易踩的小坑。
- **畸变（distortion）**：衡量「观测点到自己簇心」的整体远近。标准 k-means 文献里畸变 = 各点到簇心**平方距离之和**；而 SciPy 的 `kmeans` 返回值 `distortion` 是各点到簇心**欧氏距离的均值**（注意是非平方、且是均值）。这两种定义在 [u1-l1](u1-l1-project-overview.md) 里已经点过，本讲会在源码处再确认一次。
- **凝聚式聚类（agglomerative clustering）**：从「每个点自成一簇」开始，每一步把距离最近的两簇合并，直到只剩一个根簇。整个过程被记录成一棵树（dendrogram），用 linkage matrix `Z` 编码。
- **pdist（压缩距离矩阵）**：`scipy.spatial.distance.pdist(X)` 把一个 n×m 的观测矩阵算成两两之间的距离，但因为距离矩阵对称且对角线为 0，只保留上三角，得到一个长度为 \(\binom{n}{2}\) 的一维向量。这是 `linkage` 的标准输入之一。

> 一个数组术语提醒：本讲所有「行 = 一个观测 / 一个点」「列 = 一个特征」的约定，全部来自 `vq` 模块文档里的明文规定，下文源码精读会引用原话。

## 3. 本讲源码地图

本讲只涉及 3 个文件，且只看其中 4 个公开函数的「入口签名 + 关键几行」，不钻内部算法：

| 文件 | 作用 | 本讲用到的公开函数 |
| --- | --- | --- |
| [vq/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py) | `vq` 子模块的导出层，把实现层的名字重新导出给用户；文档里写明了 M×N / k×N 的输入约定 | `whiten`、`kmeans`（经重导出） |
| [hierarchy/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py) | `hierarchy` 子模块的导出层，把约 30 个名字重新导出 | `linkage`、`fcluster`（经重导出） |
| [vq/_vq_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py) | `vq` 的实现层（纯 Python 封装 + 调用 Cython 后端 `_vq`） | `whiten`、`kmeans`、内部 `_kmeans` |
| [hierarchy/_hierarchy_impl.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py) | `hierarchy` 的实现层 | `linkage`、`fcluster` |

> 真身都在 `_*_impl` 里：`from ._vq_impl import ... whiten`（[vq/__init__.py:74](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L74)）、`linkage` 在 [hierarchy/__init__.py:100-107](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/__init__.py#L100-L107) 的大 import 块里。这正是 [u1-l2](u1-l2-structure-and-build.md) 讲过的「沿 `from ._xxx_impl import` 找实现层」。

---

## 4. 核心概念与源码讲解

本讲围绕 4 个最小模块：`whiten` → `kmeans`（k-means 主线），`linkage` → `fcluster`（层次聚类主线）。

### 4.1 whiten：按特征白化（k-means 的预处理）

#### 4.1.1 概念说明

不同特征的量纲可能差很多：比如「身高（米）」和「体重（千克）」，体重的数值范围大得多，直接算欧氏距离时体重要「碾压」身高。k-means 用的是欧氏距离，所以正式聚类前通常先把**每一列（每个特征）除以它自己的标准差**，让每个特征都变成「单位方差」——这步叫**白化（whitening）**，类比信号处理里让每个频率等功率的「白噪声」。

白化后，每一列的标准差都变成 1，各特征在同一尺度上「公平竞争」，k-means 才不会被量纲大的特征带偏。

#### 4.1.2 核心流程

`whiten` 的逻辑非常直接，对输入 `obs`（M×N）按列做：

1. 求每一列的标准差 `std_dev = std(obs, axis=0)`，得到长度为 N 的向量。
2. 逐列相除：`result = obs / std_dev`，即 \( \text{result}[i,j] = \text{obs}[i,j] / \text{std\_dev}[j] \)。
3. 边界处理：如果某列标准差为 0（整列常数），除以 0 会出问题，于是把该列的除数**强制改成 1**（即该列保持原值），并发出 `RuntimeWarning` 提醒用户。

#### 4.1.3 源码精读

`whiten` 的完整实现只有十几行，定义在 [vq/_vq_impl.py:25-83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L25-L83)。核心计算部分如下：

```python
std_dev = xp.std(obs, axis=0)                 # L76：每列标准差
zero_std_mask = std_dev == 0                  # L77：找出零方差列
std_dev = xpx.at(std_dev, zero_std_mask).set(1.0)  # L78：零方差列除数置 1
if check_finite and xp.any(zero_std_mask):
    warnings.warn("Some columns have standard deviation zero. ...",
                  RuntimeWarning, stacklevel=2)   # L79-82：告警
return obs / std_dev                          # L83：逐列相除
```

逐行解读：

- [vq/_vq_impl.py:76](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L76)：`xp.std(obs, axis=0)`，`axis=0` 表示「沿行方向聚合」，即**按列**求标准差，结果长度等于列数 N。`xp` 是 array API 的命名空间（多数情况下就是 numpy）。
- [vq/_vq_impl.py:77-78](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L77-L78)：`xpx.at(std_dev, zero_std_mask).set(1.0)` 是 array API extra 提供的「带掩码的就地赋值」，等价于 `std_dev[zero_std_mask] = 1.0`，把零方差列的除数改成 1。
- [vq/_vq_impl.py:83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L83)：广播除法 `obs / std_dev`，numpy 会把长度 N 的 `std_dev` 自动广播到 M×N，实现对每一列分别除以自己的标准差。

至于 `M×N`、`k×N` 这些输入约定，文档原文写在 [vq/__init__.py:51-54](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/__init__.py#L51-L54)：「All routines expect obs to be an M by N array, where the rows are the observation vectors. The codebook is a k by N array ...」——这是整条 k-means 链路的形状契约。

#### 4.1.4 代码实践

**实践目标**：亲手验证 whiten 的两个关键性质——(a) 白化后每列标准差为 1；(b) 零方差列会触发告警且保持不变。

**操作步骤**：

```python
import numpy as np
import warnings
from scipy.cluster.vq import whiten

obs = np.array([
    [1.0, 5.0],   # 第 2 列全是常数 5.0 -> 零方差
    [2.0, 5.0],
    [3.0, 5.0],
    [4.0, 5.0],
])

# 捕获告警，方便观察
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    result = whiten(obs)

print("白化结果:\n", result)
print("白化后每列标准差:", result.std(axis=0))
print("触发的告警:", [str(x.message) for x in w])
```

**需要观察的现象**：

1. 白化结果的第 1 列（原标准差非 0）数值被缩放，且缩放后该列标准差为 1。
2. 第 2 列（原为常数 5.0）**保持 5.0 不变**，没有被除以 0。
3. 终端打印出一条 `RuntimeWarning: Some columns have standard deviation zero ...`。

**预期结果**：第 1 列原标准差为 \(\sqrt{5/4}\)（numpy 默认总体方差），白化后标准差为 1.0；第 2 列恒为 5.0。具体浮点数值**待本地验证**（取决于 numpy 的 `std` 默认 ddof）。

#### 4.1.5 小练习与答案

**练习 1**：如果不调用 `whiten` 直接把原始 `obs` 喂给 `kmeans`，会有什么后果？

**参考答案**：算法仍能跑，但量纲大的特征会主导欧氏距离，聚类结果会被该特征带偏。`whiten` 的作用就是消除这种量纲失衡。

**练习 2**：`whiten` 把零方差列的除数设成 1 而不是 0，为什么？

**参考答案**：设成 1 等于「该列不变」，避免除零产生 NaN/inf；同时这些常数列对欧氏距离没有任何区分度（所有点在该列相同），所以保持原值在数学上也无害。

---

### 4.2 kmeans：迭代求 k 个簇心与畸变

#### 4.2.1 概念说明

k-means 的目标是把 M 个观测点分成 k 个簇，找出 k 个簇心（centroid），使得「每个点到自己簇心的距离之和」尽量小。它通过反复执行两步来逼近最优：

1. **分配（assignment）**：每个点归到离它最近的簇心。
2. **更新（update）**：每个簇心更新为该簇所有点的均值。

反复交替，直到簇心稳定。这正是「向量量化」要做的事——把每个观测用「最近码字」来近似表示，因此 k-means 和向量量化在数学上是同一个问题（这也是子模块叫 `vq` 的原因，见 [u1-l1](u1-l1-project-overview.md)）。

SciPy 的 `kmeans` 有两个特点值得记住：

- 它会**多次重启**（默认 `iter=20` 次），每次随机初始化，最后保留畸变最小的那组簇心，以降低「坏初始化」的影响。
- 返回的畸变是**均值非平方欧氏距离**，和标准 k-means 文献里的「平方距离之和」定义不同。

#### 4.2.2 核心流程

`kmeans` 主函数负责「多次重启取最优」，真正干一次完整 k-means 的是内部函数 `_kmeans`：

```
kmeans(obs, k_or_guess, iter=20, ...):
    若 k_or_guess 是数组 -> 直接当初始簇心，调用一次 _kmeans
    若 k_or_guess 是整数 k -> 循环 iter 次:
        guess = 随机选 k 个观测点作为初始簇心
        book, dist = _kmeans(obs, guess)
        if dist < best_dist: 记录 best_book, best_dist
    返回 best_book, best_dist

_kmeans(obs, guess, thresh=1e-5):
    while 平均畸变变化 > thresh:
        obs_code, distort = vq(obs, code_book)          # 分配：每点找最近簇心
        code_book, has_members = update_cluster_means(...)  # 更新：簇心=簇内均值
        丢弃空簇 (has_members 为 False 的)
        比较前后两次平均畸变之差
    返回 code_book, 最终平均畸变
```

其中 `vq`（见 4.1 节同模块的 `vq` 函数）负责「分配」，`_vq.update_cluster_means`（Cython 后端）负责「更新」。收敛判据是「平均畸变的变化量小于阈值 `thresh`（默认 1e-5）」。

#### 4.2.3 源码精读

公开入口 `kmeans` 定义在 [vq/_vq_impl.py:279-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L279-L442)。最关键的「多次重启取最优」循环：

```python
best_dist = xp.inf
for i in range(iter):
    guess = _kpoints(obs, k, rng, xp)           # L437：随机选 k 个观测当初始簇心
    book, dist = _kmeans(obs, guess, thresh=thresh, xp=xp)  # L438：跑一次完整 k-means
    if dist < best_dist:                         # L439-441：保留畸变最小者
        best_book = book
        best_dist = dist
return best_book, best_dist                      # L442
```

- [vq/_vq_impl.py:435-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L435-L442)：注意 `iter` 参数是「重启次数」而非「k-means 内部迭代次数」（docstring 在 [L309-314](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L309-L314) 明确强调过这一点，是常见误解）。

真正的「分配-更新」循环在内部函数 `_kmeans`，定义在 [vq/_vq_impl.py:222-274](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L222-L274)：

```python
code_book = guess
diff = xp.inf
prev_avg_dists = deque([diff], maxlen=2)        # L257：只保留最近两次平均畸变
while diff > thresh:                             # L260：直到畸变变化足够小
    obs_code, distort = vq(obs, code_book, check_finite=False)        # L262：分配
    prev_avg_dists.append(xp.mean(distort, axis=-1))                  # L263：当前平均畸变
    obs_code = np.asarray(obs_code)
    code_book, has_members = _vq.update_cluster_means(np_obs, obs_code,
                                                      code_book.shape[0])  # L266-267：更新
    code_book = code_book[has_members]           # L268：丢弃空簇
    diff = xp.abs(prev_avg_dists[0] - prev_avg_dists[1])               # L270：畸变变化量
```

- [vq/_vq_impl.py:262](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L262)：`vq(...)` 返回 `(obs_code, distort)`，前者是每个观测的最近簇心编号，后者是每个观测到自己最近簇心的距离。
- [vq/_vq_impl.py:266-267](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L266-L267)：`_vq.update_cluster_means` 是 Cython 后端函数，把每个簇心更新为该簇成员的均值，同时返回 `has_members` 标记哪些簇非空。
- [vq/_vq_impl.py:257 与 L270](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L257)：用 `deque(maxlen=2)` 滚动保存最近两次平均畸变，它们的差就是收敛判据。

> 返回值提醒：`kmeans` 返回 `(codebook, distortion)`，其中 `distortion` 是**均值、非平方**欧氏距离（见 docstring [vq/_vq_impl.py:343-347](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L343-L347)）。别和标准 k-means 文献的「平方距离之和」混淆。

#### 4.2.4 代码实践

**实践目标**：在白化后的数据上跑 k-means，得到 2 个簇心和畸变，并观察「多次运行结果是否稳定」。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import whiten, kmeans

# 两组明显分开的点：靠近原点 4 个，靠近 (5,5) 4 个
features = np.array([
    [1.0, 1.0], [1.2, 0.8], [0.8, 1.1], [1.1, 1.0],
    [5.0, 5.0], [5.2, 4.8], [4.9, 5.1], [5.1, 5.0],
])

whitened = whiten(features)

# 固定 rng 让结果可复现
codebook, distortion = kmeans(whitened, 2, rng=np.random.default_rng(42))
print("簇心 codebook (k=2):\n", codebook)
print("畸变 distortion:", distortion)
```

**需要观察的现象**：

1. 返回的 `codebook` 是一个 2×2 数组（k=2 个簇心，每个 2 维）。
2. 两个簇心分别落在「靠近原点那组」和「靠近 (5,5) 那组」的白化坐标附近。
3. `distortion` 是一个较小的正数（因为两组点内部都很紧凑）。

**预期结果**：两个簇心能正确分开两组点；由于固定了 `rng`，多次运行结果一致。具体簇心坐标与畸变数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：把上面例子的 `kmeans(whitened, 2, rng=...)` 改成不传 `rng`，连续运行几次，簇心坐标会变吗？为什么？

**参考答案**：会变（轻微）。因为 [vq/_vq_impl.py:437](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L437) 每次重启都用 `_kpoints` 随机选初始点，不传 `rng` 时每次都新建随机源。但本例两组点分得很开，最终簇心通常收敛到几乎相同的位置——这正是多次重启（`iter`）+ 取最小畸变的意义。

**练习 2**：`kmeans` 的返回畸变是「平方距离之和」还是「均值非平方距离」？

**参考答案**：是**均值非平方**欧氏距离（[vq/_vq_impl.py:343-347](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L343-L347)），与标准 k-means 文献的「平方距离之和」定义不同。

---

### 4.3 linkage：把距离矩阵凝聚成层次树（linkage matrix Z）

#### 4.3.1 概念说明

层次聚类的输入不是原始观测，而是**观测之间的两两距离**。凝聚式（agglomerative，自底向上）做法是：一开始每个点自成一簇，每一步挑出距离最近的两簇合并，合并 n−1 次后就剩一个包含所有点的根簇。这 n−1 次合并被忠实地记录成一张 (n−1)×4 的表，叫 **linkage matrix**，记作 `Z`。

`Z` 的每一行 `Z[i]` 描述一次合并，四列含义是（[hierarchy/_hierarchy_impl.py:736-743](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L736-L743) 的原文定义）：

| 列 | 含义 |
| --- | --- |
| `Z[i, 0]` | 被合并的第一个簇的编号 |
| `Z[i, 1]` | 被合并的第二个簇的编号 |
| `Z[i, 2]` | 这两簇之间的距离（合并高度） |
| `Z[i, 3]` | 合并后新簇里包含的**原始观测点数** |

簇编号约定：编号 `< n` 表示第几个原始观测点（叶节点）；编号 `n+i` 表示在第 `i` 步合并出来的新簇（内部节点）。比如 n=8 时，第一次合并产生簇 8，第二次产生簇 9，依此类推。

`linkage` 接受两种输入（[hierarchy/_hierarchy_impl.py:727-734](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L727-L734)）：

- 1 维：`pdist` 返回的压缩距离矩阵（长度 \(\binom{n}{2}\)）。
- 2 维：原始观测矩阵（m×n），此时 `linkage` 会内部调用 `pdist` 算距离。

#### 4.3.2 核心流程

```
linkage(y, method='single', metric='euclidean'):
    校验 method 是否合法
    若 y 是 2 维观测矩阵 -> y = pdist(y, metric)   # 折叠成压缩距离矩阵
    求出原始观测数 n = num_obs_y(y)
    根据 method 分派到 Cython 后端:
        'single'                              -> mst_single_linkage
        'complete'/'average'/'weighted'/'ward'-> nn_chain
        其余 ('centroid'/'median')            -> fast_linkage
    返回 (n-1)×4 的 Z
```

`method` 决定了「两簇之间距离怎么定义」（最近点、最远点、均值、Ward 方差……共 7 种），但本讲只跑默认的 `'single'`（单链接：两簇距离 = 两簇间最近一对点的距离），七种方法的细节和分发原理留给 [u3-l2](u3-l2-linkage-dispatch.md)。

#### 4.3.3 源码精读

`linkage` 定义在 [hierarchy/_hierarchy_impl.py:723-974](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L723-L974)。几个关键节点：

输入折叠——若给的是 2 维观测矩阵，内部 `pdist`：

```python
if y.ndim == 1:
    distance.is_valid_y(y, throw=True, name='y')     # L934-935：校验压缩距离矩阵
elif y.ndim == 2:
    ...
    y = distance.pdist(y, metric)                    # L944：观测矩阵 -> 压缩距离矩阵
```

- [hierarchy/_hierarchy_impl.py:934-944](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L934-L944)：注意一个贴心告警——如果你传的 2 维方阵看起来像个「没压缩的距离矩阵」（对称、对角线为 0、非负），会警告你（[L937-943](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L937-L943)），避免误用。

按 method 分派到 Cython 后端的核心闭包 `cy_linkage`：

```python
def cy_linkage(y, validate):                         # L955
    if method == 'single':
        return _hierarchy.mst_single_linkage(y, n)   # L960-961
    elif method in ('complete', 'average', 'weighted', 'ward'):
        return _hierarchy.nn_chain(y, n, method_code)  # L962-963
    else:
        return _hierarchy.fast_linkage(y, n, method_code)  # L964-965
```

- [hierarchy/_hierarchy_impl.py:955-965](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L955-L965)：真正的合并算法都在 Cython 编译模块 `_hierarchy` 里（这正是 [u1-l2](u1-l2-structure-and-build.md) 讲的「实现层调用 Cython 后端」）。外层用 `xpx.lazy_apply(cy_linkage, y, ...)`（[L967-969](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L967-L969)）包一层，是为了兼容 dask 等惰性数组——普通 numpy 输入时会短路直接计算，本讲可暂时把它当成 `cy_linkage(y, ...)`。

#### 4.3.4 代码实践

**实践目标**：在 8 个点上跑 single linkage，亲手读懂返回的 `Z` 每一行。

**操作步骤**：

```python
import numpy as np
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage

# 复用 4.2 节的 8 个点
X = np.array([
    [1.0, 1.0], [1.2, 0.8], [0.8, 1.1], [1.1, 1.0],
    [5.0, 5.0], [5.2, 4.8], [4.9, 5.1], [5.1, 5.0],
])

# 经典写法：先 pdist 算两两距离，再 linkage 建树
Z = linkage(pdist(X), method='single')
print("Z 的形状:", Z.shape)   # 期望 (7, 4)，因为 n=8 -> n-1=7 次合并
print("Z =\n", Z)
```

**需要观察的现象**：

1. `Z.shape` 是 `(7, 4)`——n=8 个点需要 7 次合并才能聚成一棵树。
2. `Z[:, 3]`（第 4 列）从头到尾大致是 2, 2, 2, 2（小簇两两合并），最后变成 8（根簇包含全部 8 个点）。更准确地说，每一行的第 4 列 = 该行合并出的新簇的成员数。
3. `Z[:, 2]`（距离列）整体**单调递增**（single 方法下成立），因为越往后合并的两簇距离越远。
4. 簇编号会出现 ≥ 8 的值（8、9、10...），它们就是「合并出来的新簇」。

**预期结果**：前几次合并在「原点组」和「(5,5)组」各自内部完成（距离很小），最后一次才把两大组合并（距离 ≈ \(\sqrt{(5-1)^2+(5-1)^2}\approx 5.66\)，会出现在最后一行的距离列）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：n 个点的 linkage matrix 一共有多少行？为什么？

**参考答案**：n−1 行。因为每次合并减少一个簇，从 n 个单点簇聚成 1 个根簇，需要恰好 n−1 次合并，每次合并占一行。

**练习 2**：如果我直接传一个 8×8 的对称距离矩阵给 `linkage`（而不是 `pdist` 的结果），会发生什么？

**参考答案**：`linkage` 会把它当成 8 个 8 维观测点重新 `pdist`（[L944](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L944)），并触发「这看起来像未压缩的距离矩阵」的告警（[L937-943](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L937-L943)）。正确做法是先 `pdist` 或用 `squareform` 转成压缩形式。

---

### 4.4 fcluster：把层次树切平成扁平簇

#### 4.4.1 概念说明

`linkage` 给的是一棵完整的树（所有可能的合并层次），但实际应用中我们通常只想要「每个点属于哪个簇」这样的**扁平标签**。`fcluster` 就是干这个的：在树上的某个「高度」切一刀，刀口以下的子树各自成为一个扁平簇，从而把 n 个点分成若干组。

切的位置由 `criterion`（切分准则）和 `t`（阈值或目标簇数）共同决定。本讲只用最常见的两种：

- `criterion='distance'`：`t` 是距离阈值——合并高度 ≤ `t` 的都被连进同一簇。
- `criterion='maxclust'`：`t` 是「你想要几个簇」——系统自动反推一个距离阈值，使最终恰好（约）得到 `t` 个簇。

> 标签编号提醒：`fcluster` 返回的标签是 **1 起**编号（1, 2, 3...），而 `vq` 返回的 code 是 **0 起**编号。两种方法对比时要留意这个偏移。

#### 4.4.2 核心流程

```
fcluster(Z, t, criterion='inconsistent', depth=2, R=None, monocrit=None):
    校验 Z 是合法 linkage matrix
    n = Z.shape[0] + 1                  # 原始观测数
    准备长度 n 的标签数组 T（初始为 0）
    根据 criterion 分派到 Cython 后端:
        'inconsistent'    -> cluster_in(Z, R, T, t, n)        # 默认，需不一致矩阵 R
        'distance'        -> cluster_dist(Z, T, t, n)
        'maxclust'        -> cluster_maxclust_dist(Z, T, n, t)
        'monocrit' / 'maxclust_monocrit' -> ...
    返回 T（长度 n 的扁平簇标签）
```

#### 4.4.3 源码精读

`fcluster` 定义在 [hierarchy/_hierarchy_impl.py:2420-2599](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2420-L2599)。准核心是按 `criterion` 分派的 if 链：

```python
n = Z.shape[0] + 1                                  # L2571：原始观测数 = 行数+1
T = np.zeros((n,), dtype='i')                       # L2572：标签数组
...
if criterion == 'inconsistent':                     # L2579
    ...
    _hierarchy.cluster_in(Z, R, T, float(t), int(n))
elif criterion == 'distance':                       # L2589
    _hierarchy.cluster_dist(Z, T, float(t), int(n)) # L2590
elif criterion == 'maxclust':                       # L2591
    _hierarchy.cluster_maxclust_dist(Z, T, int(n), t)  # L2592
elif criterion == 'monocrit':                       # L2593
    _hierarchy.cluster_monocrit(Z, monocrit, T, float(t), int(n))
elif criterion == 'maxclust_monocrit':              # L2595
    _hierarchy.cluster_maxclust_monocrit(Z, monocrit, T, int(n), int(t))
...
return xp.asarray(T)                                # L2599
```

- [hierarchy/_hierarchy_impl.py:2571-2572](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2571-L2572)：`n = Z.shape[0] + 1`——因为 Z 有 n−1 行，所以加 1 才是原始点数；`T` 是长度 n 的标签数组，`T[i]` 是第 i 个观测的扁平簇号。
- [hierarchy/_hierarchy_impl.py:2589-2592](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2589-L2592)：`'distance'` 和 `'maxclust'` 这两种最常用准则分别落到 Cython 的 `cluster_dist` 和 `cluster_maxclust_dist`。前者直接按距离阈值切，后者先反推阈值再切。

#### 4.4.4 代码实践

**实践目标**：在 4.3 节得到的 `Z` 上，分别用 `'distance'` 和 `'maxclust'` 切出 2 个扁平簇，对比两者的标签。

**操作步骤**（接 4.3 节，已有 `Z`）：

```python
from scipy.cluster.hierarchy import fcluster

# 方法 A：按距离阈值切。两组之间距离约 5.66，组内距离 < 0.5，
# 取 t=2.0 应能把两大组各自切成一个簇
labels_dist = fcluster(Z, t=2.0, criterion='distance')
print("distance 准则标签:", labels_dist)

# 方法 B：直接指定「我要 2 个簇」
labels_maxclust = fcluster(Z, t=2, criterion='maxclust')
print("maxclust 准则标签:", labels_maxclust)
```

**需要观察的现象**：

1. 两个 `labels_*` 都是长度 8 的数组。
2. 它们都能把前 4 个点（原点组）和后 4 个点（(5,5)组）分成两类。
3. 标签是 **1、2** 这样的正整数（1 起编号），不是 0、1。

**预期结果**：`labels_dist` 与 `labels_maxclust` 大致都形如 `[1,1,1,1,2,2,2,2]`（或 `[2,2,2,2,1,1,1,1]`，编号本身无意义）。具体编号和顺序**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`criterion='distance'` 时把 `t` 设得非常大（比如 100），结果会是什么？

**参考答案**：所有合并高度都 ≤ 100，所以所有点被连进同一个簇，`fcluster` 返回全 1 的标签（只有 1 个簇）。反过来 `t` 设得非常小则每个点自成一类（n 个簇）。

**练习 2**：为什么 `fcluster` 返回的标签是 1 起编号，而 `vq` 返回的 code 是 0 起编号？对比两种方法时要注意什么？

**参考答案**：这是两套 API 各自的历史约定。对比时不能直接 `==`，要做「标签编号无关」的比较——即只看「同样的两个点是否被分到同一类」，而不是看编号是否相等。综合实践（第 5 节）会演示这种对比。

---

## 5. 综合实践

**任务**：构造一个 8×2 的小数据集，分别用 k-means（`vq.whiten + kmeans`）和层次聚类（`hierarchy.linkage + fcluster`）把它分成 2 类，并对比两种方法的结果是否一致。

**操作步骤**：

```python
import numpy as np
from scipy.cluster.vq import whiten, kmeans, vq
from scipy.spatial.distance import pdist
from scipy.cluster.hierarchy import linkage, fcluster

# ---- 1. 构造数据：两组明显分开的点 ----
X = np.array([
    [1.0, 1.0], [1.2, 0.8], [0.8, 1.1], [1.1, 1.0],   # 原点附近 4 个
    [5.0, 5.0], [5.2, 4.8], [4.9, 5.1], [5.1, 5.0],   # (5,5) 附近 4 个
])
print("观测矩阵 X 的形状:", X.shape)   # (8, 2) = M×N

# ---- 2. k-means 主线：whiten -> kmeans -> vq 得到每个点的簇号 ----
whitened = whiten(X)
codebook, distortion = kmeans(whitened, 2, rng=np.random.default_rng(42))
km_labels, _ = vq(whitened, codebook)   # km_labels: 0 起编号
print("\nk-means 簇心:\n", codebook)
print("k-means 畸变:", distortion)
print("k-means 标签 (0起):", km_labels)

# ---- 3. 层次聚类主线：pdist -> linkage -> fcluster 得到扁平簇 ----
Z = linkage(pdist(X), method='single')
hc_labels = fcluster(Z, t=2, criterion='maxclust')   # hc_labels: 1 起编号
print("\nlinkage matrix Z 形状:", Z.shape)            # (7, 4)
print("层次聚类标签 (1起):", hc_labels)

# ---- 4. 对比：标签编号本身无意义，只看「同组关系」是否一致 ----
# 简单办法：两种方法都应把 {0,1,2,3} 分成一类、{4,5,6,7} 分成另一类
km_groups = [set(np.where(km_labels == c)[0]) for c in np.unique(km_labels)]
hc_groups = [set(np.where(hc_labels == c)[0]) for c in np.unique(hc_labels)]
print("\nk-means 分组:", km_groups)
print("层次聚类分组:", hc_groups)
print("两种方法分组是否一致（忽略编号）:",
      sorted(sorted(g) for g in km_groups) == sorted(sorted(g) for g in hc_groups))
```

**需要观察的现象**：

1. `X.shape` 是 `(8, 2)`，对应「8 个观测、2 个特征」的 M×N 约定。
2. k-means 给出 2 个簇心（2×2）和一个畸变值；`km_labels` 是 0/1 编码。
3. `Z.shape` 是 `(7, 4)`，即 (n−1)×4 的 linkage matrix。
4. `hc_labels` 是 1/2 编码（注意是 1 起）。
5. **两种方法最终的「分组」一致**：都把前 4 个点放一组、后 4 个点放另一组——这正是「分组一致、编号不同」的典型情况，所以最后那行对比必须做「编号无关」的比较。

**预期结果**：在本例这种「两组明显分开」的干净数据上，k-means 与 single linkage 层次聚类会给出相同的二分结果（最后打印 `True`）。在噪声更大或簇间距离更接近的数据上，两种方法可能给出不同结果——这是它们算法本质不同的体现（k-means 假设球形簇，single linkage 对噪声/链式效应敏感）。

> 思考延伸：尝试把两组点拉近（比如第二组改成 `[2.0, 2.0]` 附近），再跑一遍，观察两种方法的结果是否还一致。这能帮你直观感受两种算法对「簇间距」的敏感度差异。

## 6. 本讲小结

- `whiten` 对观测矩阵**按列除以标准差**做白化，让每个特征单位方差；零方差列除数置 1 并告警（[vq/_vq_impl.py:76-83](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L76-L83)）。
- `kmeans` 通过「分配（`vq`）→ 更新（`_vq.update_cluster_means`）」迭代到畸变收敛，并多次重启取最优；返回的畸变是**均值非平方**欧氏距离（[vq/_vq_impl.py:222-442](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/vq/_vq_impl.py#L222-L442)）。
- `linkage` 把两两距离凝聚成 (n−1)×4 的 linkage matrix `Z`，四列分别是「两簇编号 / 合并距离 / 新簇成员数」；簇编号 `n+i` 表示第 i 步合并出的新簇（[hierarchy/_hierarchy_impl.py:723-974](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L723-L974)）。
- `fcluster` 按 `criterion`（`distance`/`maxclust`/`inconsistent`/...）把树切平，返回 **1 起**编号的扁平簇标签（[hierarchy/_hierarchy_impl.py:2420-2599](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/cluster/hierarchy/_hierarchy_impl.py#L2420-L2599)）。
- 三类输入/输出约定要刻在脑子里：观测矩阵 `obs`（M×N）、码本 `code_book`（k×N）、linkage matrix `Z`（(n−1)×4）。
- k-means 与层次聚类标签编号不同（0 起 vs 1 起），对比时要做「编号无关」的分组比较。

## 7. 下一步学习建议

本讲只是「跑通 + 认形状」，每条主线都还藏着大量细节，建议按以下顺序深入：

- **k-means 主线深入**：进 [u2 向量量化与 k-means](../scipy-scipy-cluster-tutorial/) 系列。先读 `whiten` 的 array API 抽象（u2-l1），再钻 `vq` 如何分派到 Cython 后端 `_vq.vq`（u2-l2），然后精读 `_kmeans` 的迭代与收敛（u2-l3），最后看 `kmeans2` 的多种初始化与空簇处理（u2-l4）。
- **层次聚类主线深入**：进 [u3 层次聚类基础](../scipy-scipy-cluster-tutorial/) 系列。重点是彻底吃透 linkage matrix `Z` 的数据结构（u3-l1）、`linkage` 对 7 种 method 的分发（u3-l2）、Python↔Cython 的桥接（u3-l3）以及统一的 Lance-Williams 距离更新公式（u3-l4）。
- **切平与统计**：[u5 把树切平](../scipy-scipy-cluster-tutorial/) 系列会系统讲 `fcluster` 的全部 criterion、`inconsistent` 不一致矩阵、`cophenet` 保真度等，是本讲 4.4 节的完整版。
- **可視化**：想画出本讲那棵树，直接进 [u6-l2 dendrogram 可视化](../scipy-scipy-cluster-tutorial/)，学会读 `dendrogram` 返回的绘图数据。

> 阅读源码时记住 [u1-l2](u1-l2-structure-and-build.md) 的追踪口诀：沿 `from ._xxx_impl import` 找实现层，再沿 `from . import _xxx` 找 Cython 后端。本讲引用的 `_vq`、`_hierarchy` 就是那两个编译后端。
