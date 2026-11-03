# u10-l1 KMeans、肘部法与轮廓系数

## 1. 本讲目标

前面九章处理的数据都带着标签 `y`（鸢尾花品种、良性/恶性、影评情感、房价），模型学的是「输入 → 已知答案」的映射。本讲开始进入**无监督学习**的另一大分支——**聚类（clustering）**：数据没有标签，我们要让算法自己发现「哪些样本天生属于一伙」。

学完本讲，你应该能够：

1. 讲清楚 k-means 算法的「分配—更新」两步循环，以及它为什么对初始质心敏感。
2. 熟练使用 scikit-learn 的 `KMeans`，读懂它暴露的三个关键属性：`cluster_centers_`、`labels_`、`inertia_`。
3. 理解 k-means++ 初始化如何缓解「坏初始化 → 坏局部最优」的问题。
4. 用**肘部法**（扫描 `inertia_` 曲线找拐点）和**轮廓系数**（silhouette coefficient，衡量簇内紧凑、簇间分离）两种工具为聚类挑选簇数 k，并会用 `silhouette_samples` 画出书中标志性的「半圆刀片」轮廓图。

本讲全部源码来自 `ch10/ch10.py` 的 k-means 部分（L91–L314），这正是本章 README 里「Grouping objects by similarity using k-means」一小节的代码。

## 2. 前置知识

**无监督学习回顾**：在 u5-l1（PCA）里我们已经见过第一种无监督方法——PCA 只用特征矩阵 X、不用标签 y，目标是「投影方差最大」。聚类是第二种：目标是「组内相似、组间有别」。两者都只有 X，没有标准答案可对，所以评估方式与监督学习完全不同——这是本讲的核心难点。

**距离度量**：k-means 建立在欧氏距离上（u3-l3 讲 KNN 时提过 Minkowski 距离，p=2 即欧氏距离）。因此与 KNN 一样，**真实数据上先做特征标准化（u2-l3 的 StandardScaler）是硬前提**，否则量纲大的特征会主导距离。本讲演示用的 `make_blobs` 合成数据两个特征方差相同，所以书中代码省略了缩放这一步。

**SSE（误差平方和，书里叫 distortion / 畸变）**：把每个样本到它所属簇质心的欧氏距离平方全部加起来，就是 k-means 要最小化的目标函数。我们在 u2-l2 讲 Adaline 时见过同结构的 SSE 代价函数，区别在于那里拟合的是一条直线，这里拟合的是 k 个点（质心）。

**簇数 k 是超参数**：与 u6-l2 调 SVM 的 C、gamma 类似，k 也要人给定；但聚类没有交叉验证准确率可用（没有标签！），所以出现了本讲的两个替代工具：肘部法与轮廓系数。

**版本提示**：本书环境钉住 scikit-learn 1.0（见 u1-l2 的 environment.yml）。在新版 scikit-learn（≥1.4）中 `KMeans` 的 `n_init` 默认值由 10 改为 `'auto'`，行为兼容；按书复现建议使用书中指定的显式参数。

## 3. 本讲源码地图

| 源码位置 | 作用 |
| --- | --- |
| [ch10/ch10.py:L99-L119](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L99-L119) | 用 `make_blobs` 生成 150×2 的三团合成数据并画原始散点图 |
| [ch10/ch10.py:L126-L166](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L126-L166) | `KMeans(init='random')` 训练、`fit_predict`、按簇着色散点并叠加质心 `cluster_centers_` |
| [ch10/ch10.py:L170-L176](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L170-L176) | 「k-means++」「硬聚类 vs 软聚类」两节的标题注释（书正文为纯讲解，脚本身只有标题） |
| [ch10/ch10.py:L182-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L182-L201) | 打印 `inertia_`；k=1..10 循环拟合，绘制肘部曲线 |
| [ch10/ch10.py:L211-L244](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L211-L244) | k=3 的轮廓系数计算与「半圆刀片」轮廓图 |
| [ch10/ch10.py:L251-L314](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L251-L314) | 反面教材：k=2 的聚类结果与轮廓图，用来说明「坏聚类长什么样」 |
| [ch10/README.md](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/README.md) | 本章大纲 |
| ch10/figures/10_01.png – 10_06.png | 上述代码依次产出的六张图，可对照阅读 |

`ch10.ipynb` 是同一份代码的 notebook 版本（u1-l1 讲过双轨关系），交互实验建议在 notebook 里做。

## 4. 核心概念与源码讲解

### 4.1 make_blobs 合成数据

#### 4.1.1 概念说明

研究聚类算法时，我们面临一个鸡生蛋问题：真实数据没有标签，怎么知道聚类结果对不对？答案是先用**合成数据**（synthetic data）——`sklearn.datasets.make_blobs` 从 k 个高斯团块里各采一批二维点，天然自带「真实分团」供对照。等我们确信算法行为符合预期，再上真实数据。

`make_blobs` 的关键参数：

- `n_samples`：总样本数；
- `centers`：簇数（传整数）或显式坐标列表（传数组，可精确控制团块位置）；
- `cluster_std`：每个团块的标准差，越大团越散、越容易相互重叠；
- `shuffle` / `random_state`：打乱与随机种子，固定种子保证可复现（u2-l3 讲过同样的道理）。

#### 4.1.2 核心流程

```text
make_blobs(150 样本, 2 特征, 3 中心, std=0.5)
        │
        ├──► 返回 X (150×2 特征矩阵)、y (150 个真实簇编号, 仅作对照)
        └──► plt.scatter 画出"未着色"的原始散点 → 肉眼看到三团
```

注意返回的 `y` 是「上帝视角」的真实分团，**聚类算法只会拿到 X**，y 留给我们最后比对用。

#### 4.1.3 源码精读

[ch10/ch10.py:L99-L104](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L99-L104) 生成三团数据：150 个样本、2 个特征、3 个中心、每团标准差 0.5，洗牌并固定随机种子 0。

[ch10/ch10.py:L111-L119](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L111-L119) 把所有点画成统一白色的散点——此刻不涂颜色，正是为了模拟「无标签」状态。对应书图 `ch10/figures/10_01.png`：三个椭圆形团块彼此分开，边界清晰。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `cluster_std` 对团块分离度的影响。
2. **操作步骤**（示例代码，可直接跑在 notebook 里）：

```python
# 示例代码：改编自 ch10/ch10.py L99-L104
from sklearn.datasets import make_blobs
import matplotlib.pyplot as plt

X, y = make_blobs(n_samples=150, n_features=2, centers=3,
                  cluster_std=2.0,        # 原书为 0.5，这里放大 4 倍
                  shuffle=True, random_state=0)
plt.scatter(X[:, 0], X[:, 1], c='white', marker='o', edgecolor='black', s=50)
plt.xlabel('Feature 1'); plt.ylabel('Feature 2'); plt.grid()
plt.show()
```

3. **需要观察的现象**：三团明显相互渗透、边界消失。
4. **预期结果**：`cluster_std=2.0` 时团块大面积重叠，后续 KMeans 即使 k=3 也会把交叠区切出「穿过数据内部的边界」，轮廓系数显著低于原书设置。具体数值**待本地验证**。
5. 可再用 `centers=[[-5, 0], [0, 0], [10, 8]]`（坐标列表形式）控制团块间距，为综合实践做准备。

#### 4.1.5 小练习与答案

- **练习 1**：`centers=3`（整数）与 `centers=[[-5,0],[0,0],[10,8]]`（列表）有何区别？
  答案：整数只指定簇数，各中心坐标由随机过程生成（受 `random_state` 影响）；列表则显式钉死每个团块的位置，便于设计「间距不均匀」的实验场景。
- **练习 2**：`make_blobs` 返回的 y 会被 `KMeans.fit(X)` 用到吗？
  答案：不会。聚类只吃 X；y 只在事后用来对照聚类与真实分团的吻合度。
- **练习 3**：为什么聚类算法（如 k-means）通常要先标准化特征？
  答案：k-means 基于欧氏距离，量纲大的特征会淹没量纲小的特征（u2-l3、u3-l3 的同一结论）；`make_blobs` 两特征同量纲同方差，故演示中可省略。

### 4.2 KMeans 训练循环与 cluster_centers_ / labels_ / inertia_

#### 4.2.1 概念说明

k-means 的目标是最小化 SSE（distortion）：

\[
SSE = \sum_{j=1}^{k}\sum_{i \in C_j} \left\| \mathbf{x}^{(i)} - \boldsymbol{\mu}^{(j)} \right\|_2^2
\]

其中 \(C_j\) 是第 j 个簇，\(\boldsymbol{\mu}^{(j)}\) 是它的质心。算法用交替优化逼近这个非凸目标：

1. **初始化**：从数据点中挑 k 个初始质心（`init='random'` 是随机挑）。
2. **分配（assignment）**：每个样本划给最近的质心（按欧氏距离）。
3. **更新（update）**：每个质心移动到其成员的均值位置。
4. 重复 2–3，直到质心不再移动（或达到 `max_iter` / 满足 `tol` 容差）。

**初始化敏感性**是这个算法的命门：SSE 目标非凸，随机初始化可能把两个质心扔进同一团数据，收敛后两个簇瓜分一团、另一团被硬生生劈开——一个稳定的**局部最优**，但不是我们想要的解。两个补救手段在源码里都有体现：

- `n_init=10`：完整跑 10 次不同初始化，按 `inertia_` 最小的一次作为最终结果；
- `init='k-means++'`：让初始质心**彼此远离**——第 1 个质心均匀随机选；之后每个新质心以正比于 \(D(\mathbf{x})^2\)（x 到最近已有质心距离的平方）的概率被选中。质心散开后，两个质心挤在同一团的概率大幅下降。

（硬聚类 vs 软聚类的对比、k-means++ 的完整推导在书正文里，`ch10.py` L170–L176 只保留了两节标题注释，没有独立代码；k-means++ 在代码中的落点就是后文 `init='k-means++'` 这个参数。）

#### 4.2.2 核心流程

```text
km = KMeans(n_clusters=3, init='random', n_init=10, max_iter=300, tol=1e-04, random_state=0)
y_km = km.fit_predict(X)
        │
        ├── km.cluster_centers_ : (3, 2) 数组，三个质心坐标
        ├── km.labels_          : (150,) 数组，每个样本的簇编号 0/1/2（与 y_km 相同）
        └── km.inertia_         : 标量，最终解的 SSE
```

`fit_predict(X)` 等价于先 `fit(X)` 再取 `labels_`，一步拿到全部簇标签。

#### 4.2.3 源码精读

[ch10/ch10.py:L126-L131](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L126-L131) 构造 KMeans 估计器：3 个簇、随机初始化、重启 10 次、最多迭代 300 轮、质心移动量小于 1e-04 判定收敛、固定随机种子。注意这里故意用 `init='random'` 演示朴素初始化；肘部法一节会切换成 `init='k-means++'`。

[ch10/ch10.py:L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L133) 一行完成训练与打标：`y_km` 就是每个样本的簇编号。

[ch10/ch10.py:L138-L157](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L138-L157) 用布尔掩码 `X[y_km == 0, 0]`（u1-l3 讲过 iloc/掩码技巧）分别取出三个簇着不同颜色画散点；L153–L157 把 `km.cluster_centers_` 的两列作为 x、y，用红色大星形（`s=250, marker='*'`）叠画质心。

[ch10/ch10.py:L182](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L182) 打印 `km.inertia_`——书里称这个量为 distortion，即上面 SSE 公式的值。它由 KMeans 在 `fit` 时自动算好存放。

运行后应得到书图 `ch10/figures/10_02.png`：三团各一种颜色，红色星形质心基本落在每团的重心处。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看一次「坏初始化」以及 `n_init` 如何救场。
2. **操作步骤**（示例代码）：

```python
# 示例代码：观察初始化敏感性与 n_init 的作用
from sklearn.cluster import KMeans

for n_init in [1, 1, 1, 10]:
    km = KMeans(n_clusters=3, init='random', n_init=n_init,
                max_iter=300, tol=1e-04, random_state=1).fit(X)  # 换个种子多试几次
    print(f'n_init={n_init:>2}, inertia={km.inertia_:.2f}')
```

3. **需要观察的现象**：`n_init=1` 的三次运行（换不同 `random_state`）`inertia_` 时大时小；`n_init=10` 稳定给出较小值。
4. **预期结果**：单次随机初始化偶会陷入较高 SSE 的局部最优（两个质心同属一团）；`n_init=10` 取 10 次中最小的 inertia，结果稳定。由于结果依赖随机种子，具体数值**待本地验证**。
5. 追问：把 `init` 换成 `'k-means++'` 再跑，单次初始化的 inertia 是否也普遍变小？（同样待本地验证。）

#### 4.2.5 小练习与答案

- **练习 1**：`fit_predict` 的返回值与 `km.labels_` 是什么关系？
  答案：完全相同；`fit_predict(X)` 就是 `fit(X)` 后返回 `labels_` 的便捷写法。
- **练习 2**：`n_init=10` 为什么能缓解初始化敏感？
  答案：它完整重启 10 次不同初始化的 k-means，最终保留 `inertia_`（SSE）最小的那次解；多次独立局部最优中挑最好，等价于提升找到好解的概率。
- **练习 3**：k-means++ 与随机初始化的本质区别是什么？
  答案：随机初始化每个质心独立均匀抽取；k-means++ 依次选取质心，且每个新质心被选中的概率正比于它到最近已选质心距离的平方，使初始质心互相远离，从源头减少「两质心同团」的坏开局。

### 4.3 肘部法：inertia_ 肘部曲线

#### 4.3.1 概念说明

SSE 有个令人头疼的性质：**k 越大它必然越小**。极端情况 k = n（每个样本自成一簇），每点到质心距离为 0，SSE = 0。所以「把 SSE 压到最小」选不出 k——必须找**边际收益骤减的拐点**：k 增大前期，每加一个质心都能吞掉一大块簇内方差，曲线陡降；过了真实簇数后，新增质心只能在团内部切细，曲线骤缓。曲线由陡转缓的「手肘」处即为建议的 k。

肘部法的缺点也要心里有数：团块分离度差、大小悬殊或尺度不一时，肘部会很圆滑、难以定位——这正是需要轮廓系数补刀的地方（见 4.4）。

#### 4.3.2 核心流程

```text
for k in 1..10:
    用同一份数据拟合 KMeans(k)
    记录 km.inertia_
画 k–inertia 折线 → 找肘
```

注意 k 从 1 开始也是合法的（一个质心取全体均值），这给我们提供了一个「没有任何结构时的 SSE 基线」。

#### 4.3.3 源码精读

[ch10/ch10.py:L187-L196](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L187-L196) 肘部法核心循环：`distortions` 列表收集每个 k 的 `inertia_`；这里初始化换成了 `init='k-means++'`（配合 `n_init=10`），保证每个 k 的结果都接近该 k 的最优解，否则坏初始化的噪声会盖住肘部形状。

[ch10/ch10.py:L196-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L196-L201) 以 k 为横轴、Distortion 为纵轴画折线并加圆点标记，得到书图 `ch10/figures/10_03.png`：曲线在 k=3 处明显拐折——k 从 1→2→3 陡降，k>3 后近乎躺平。

#### 4.3.4 代码实践

1. **实践目标**：验证「SSE 随 k 单调下降且在 k=n 时归零」。
2. **操作步骤**（示例代码）：

```python
# 示例代码：把 k 推到极端，理解肘部法的边界
distortions = []
for i in range(1, 11):
    km = KMeans(n_clusters=i, init='k-means++', n_init=10,
                max_iter=300, random_state=0).fit(X)
    distortions.append(km.inertia_)
print(distortions)          # 观察单调下降，以及 3 之后降幅骤减
```

3. **需要观察的现象**：`distortions` 逐项递减；相邻两项的差值在 k=3 之前很大、之后骤然变小。
4. **预期结果**：与书图 10_03 一致，肘部落在 k=3。若把范围扩到 `range(1, 151)`，k=150 时 distortion 应为 0（待本地验证，耗时较长）。
5. 思考：如果曲线没有明显肘部（4.1 节把 `cluster_std` 调大后就是这种情况），你该怎么办？——这正是下一节轮廓系数的用武之地。

#### 4.3.5 小练习与答案

- **练习 1**：为什么不能直接选让 inertia_ 最小的 k？
  答案：inertia_ 关于 k 单调不增，k=n 时必为 0；最小化 inertia_ 永远推出 k=n，毫无意义。肘部法找的是边际下降的拐点。
- **练习 2**：肘部曲线里 k=1 的 distortion 有什么用途？
  答案：它是「不聚类」的基线（全体到总均值的距离平方和），给出 SSE 的量级上限，帮助判断后续下降幅度是否显著。
- **练习 3**：肘部法失效（肘部圆滑）时怎么办？
  答案：改用或联用轮廓系数（4.4 节）、Gap statistic 等内部指标，或直接结合下游任务需求定 k；也可以换对簇形状不敏感的算法（下一讲的层次聚类、DBSCAN）。

### 4.4 silhouette_samples 与轮廓半圆图

#### 4.4.1 概念说明

轮廓系数同时度量一个样本「在自己簇里挤不挤」和「离最近的外簇远不远」。对样本 i（属簇 c）：

\[
a(i) = \frac{1}{|C_c| - 1}\sum_{j \in C_c,\, j \neq i} d(i, j)
\qquad
b(i) = \min_{c' \neq c}\ \frac{1}{|C_{c'}|}\sum_{j \in C_{c'}} d(i, j)
\]

\[
s(i) = \frac{b(i) - a(i)}{\max\{a(i),\ b(i)\}}
\]

- \(a(i)\)：簇内不相似度——到同簇其他成员的平均距离，越小说明本簇越紧凑；
- \(b(i)\)：到**最近**异簇的平均距离，越大说明与外簇分得越开；
- \(s(i) \in [-1, 1]\)：接近 1 表示「抱团紧、离敌远」；接近 0 表示骑在边界上；**为负表示离隔壁簇比离自己人还近，多半分错了簇**。

两个推论：其一，k=1 时 \(b(i)\) 无从定义（没有异簇），所以 `silhouette_samples` 要求至少 2 个簇——这解释了为何综合实践中肘部法扫 k=1..10，轮廓图只画 k=2..4。其二，全数据集的平均轮廓系数 \(\bar{s}\) 可用来横向比较不同 k。

**半圆刀片图的画法**（本讲最值得学的可视化技巧）：对每个簇，取该簇全部样本的轮廓系数、**从小到大排序**后用 `barh` 逐条水平画出（条高 1.0），簇与簇在纵轴上首尾相接堆叠；最后用红色虚线标出全体平均值 \(\bar{s}\)。排序让每簇轮廓呈一片平滑的「刀片」，多片拼成半圆轮廓，肉眼即可完成三查：刀片是否越过平均线、有没有伸进负值区、各刀片宽度（=簇的样本数）是否悬殊。

#### 4.4.2 核心流程

```text
silhouette_samples(X, y_km, metric='euclidean')      # 每个样本一个 s(i)
for 每个簇 c（按簇编号顺序）:
    取该簇的 s 值 → sort 升序
    在纵轴 [y_ax_lower, y_ax_upper) 画水平条
    记录刀片中点做 y 刻度；更新 y_ax_lower
画红色虚线 = mean(全部 s(i))
```

#### 4.4.3 源码精读

[ch10/ch10.py:L211-L221](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L211-L221) 用 k=3 重新聚类（这次 `init='k-means++'`），`np.unique(y_km)` 取出簇编号并数出簇数，`silhouette_samples(X, y_km, metric='euclidean')` 一次性算出全部 150 个样本的轮廓系数。

[ch10/ch10.py:L222-L233](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L222-L233) 刀片堆叠的核心循环：`c_silhouette_vals.sort()` 排序成刀片形状；`y_ax_upper += len(...)` 计算当前簇占据的纵向区间；`cm.jet(float(i)/n_clusters)` 取与簇编号对应的 jet 色系颜色；`plt.barh(range(y_ax_lower, y_ax_upper), ..., height=1.0)` 无缝铺条；`yticks` 记下刀片纵向中点，稍后把刻度标签设成簇号。

[ch10/ch10.py:L235-L244](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L235-L244) 计算平均轮廓系数并以红色虚线 `axvline` 画出，设置坐标轴标签后成图，对应书图 `ch10/figures/10_04.png`：k=3 时三片刀片长度与厚度都相近，几乎全部越过红色虚线，没有负值——教科书级的好聚类。

[ch10/ch10.py:L251-L284](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L251-L284) 反面教材：同样的数据强制 k=2。散点图（书图 10_05）里可以看到其中两个真实团被并成一个簇，边界从两团中间穿过。

[ch10/ch10.py:L289-L314](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L289-L314) 对这个 k=2 的结果重复一遍轮廓图代码（书图 10_06）：大簇那片刀片又宽又矮，大量样本的 s 值很低、不少跌进负值，两片刀片长度严重不对称——与 k=3 的图对照，「坏聚类长什么样」一目了然。

#### 4.4.4 代码实践

1. **实践目标**：用平均轮廓系数把 k=2 与 k=3 的优劣量化成数字。
2. **操作步骤**（示例代码）：

```python
# 示例代码：用平均轮廓系数比较不同 k
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_samples

for k in [2, 3, 4]:
    y_km = KMeans(n_clusters=k, init='k-means++', n_init=10,
                  max_iter=300, tol=1e-04, random_state=0).fit_predict(X)
    s = silhouette_samples(X, y_km, metric='euclidean')
    print(f'k={k}: 平均轮廓系数 = {np.mean(s):.3f}')
```

3. **需要观察的现象**：三个 k 的平均轮廓系数高低排序。
4. **预期结果**：k=3 最高（约 0.6–0.7 量级），k=2 明显偏低且负值样本集中在被合并的大簇，k=4 略低于 k=3（把整团多切一刀会压低部分样本的 s 值）。具体数值**待本地验证**。
5. 对照 `ch10/figures/10_04.png` 与 `10_06.png`，确认你的数字结论与书中的图形结论一致。

#### 4.4.5 小练习与答案

- **练习 1**：为什么综合实践中肘部法扫 k=1..10，轮廓图却只画 k=2..4？
  答案：轮廓系数需要 \(b(i)\)（到最近异簇的距离），k=1 时不存在异簇，`silhouette_samples` 要求 2 ≤ 簇数，因此 k=1 无法计算轮廓系数。
- **练习 2**：某样本轮廓系数为 −0.2，说明什么？
  答案：\(a(i) > b(i)\)，它到本簇成员的平均距离比到最近异簇还远，大概率被划错了簇（或处在两簇交叠带）。
- **练习 3**：半圆刀片图中，判断「好聚类」的三个目测要点是什么？
  答案：① 各刀片绝大部分越过红色平均虚线；② 几乎没有伸入负值区的部分；③ 各刀片宽度（簇样本数）大体均匀——宽窄悬殊说明有簇被撑爆、有簇过小。

## 5. 综合实践

**任务**：把本讲四个模块串成一条完整流水线——合成数据（不均匀版）→ 肘部法选 k → 轮廓图验证 → 给出最终结论。

**数据设计**：仍然 3 团，但把其中一个 center 拉远、拉开团块间距，制造「不均匀」的实验条件，看看两种工具还稳不稳。

```python
# 示例代码：综合实践（改编自 ch10/ch10.py L99-L104、L187-L201、L211-L244）
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import cm
from sklearn.datasets import make_blobs
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_samples

# ── 1. 不均匀数据：3 团，其中一团拉到远处 ─────────────────────
X, y = make_blobs(n_samples=150, n_features=2,
                  centers=[[-2.0, 0.0], [0.5, 0.0], [8.0, 6.0]],  # 第三团拉远
                  cluster_std=0.5, shuffle=True, random_state=0)

# ── 2. 肘部法：k = 1..10 ─────────────────────────────────────
distortions = []
for i in range(1, 11):
    km = KMeans(n_clusters=i, init='k-means++', n_init=10,
                max_iter=300, random_state=0).fit(X)
    distortions.append(km.inertia_)
plt.plot(range(1, 11), distortions, marker='o')
plt.xlabel('Number of clusters'); plt.ylabel('Distortion')
plt.tight_layout(); plt.show()

# ── 3. 轮廓图：k = 2..4（k=1 无法计算轮廓系数）──────────────
for n_clusters in range(2, 5):
    km = KMeans(n_clusters=n_clusters, init='k-means++', n_init=10,
                max_iter=300, tol=1e-04, random_state=0)
    y_km = km.fit_predict(X)

    cluster_labels = np.unique(y_km)
    silhouette_vals = silhouette_samples(X, y_km, metric='euclidean')
    y_ax_lower, y_ax_upper, yticks = 0, 0, []
    for i, c in enumerate(cluster_labels):
        c_silhouette_vals = silhouette_vals[y_km == c]
        c_silhouette_vals.sort()
        y_ax_upper += len(c_silhouette_vals)
        color = cm.jet(float(i) / n_clusters)
        plt.barh(range(y_ax_lower, y_ax_upper), c_silhouette_vals,
                 height=1.0, edgecolor='none', color=color)
        yticks.append((y_ax_lower + y_ax_upper) / 2.)
        y_ax_lower += len(c_silhouette_vals)
    plt.axvline(np.mean(silhouette_vals), color='red', linestyle='--')
    plt.yticks(yticks, cluster_labels + 1)
    plt.ylabel('Cluster'); plt.xlabel('Silhouette coefficient')
    plt.title(f'k = {n_clusters}')
    plt.tight_layout(); plt.show()
    print(f'k={n_clusters}: 平均轮廓系数 = {np.mean(silhouette_vals):.3f}')
```

**操作步骤**：

1. 在 pyml-book 环境的 notebook（或脚本）中运行上述代码。
2. 第 2 步输出肘部曲线，观察 k 从几开始曲线躺平。
3. 第 3 步输出三张刀片图与三个平均轮廓系数。
4. 写下你的结论：最终选哪个 k，依据是什么（肘部位置 + 平均轮廓系数 + 刀片图三查）。

**预期结果**（待本地验证）：三个团分离度好（std=0.5、间距大），肘部仍在 k=3 附近出现明显拐折；k=2 的刀片图重演书中反面教材——两团被并成一片又宽又矮、大量低值/负值的刀片；k=3 三片均匀、几乎全部越过红虚线、平均轮廓系数最高；k=4 则因把某团多切一刀而拉低部分样本的 s 值，平均值略降。最终结论应为 **k=3**：肘部拐点与最高平均轮廓系数互相印证，且刀片图无负值、宽度均匀。

**延伸思考**：回到 4.1 的实验，把 `cluster_std` 加到 2.0 再跑一遍本实践，观察肘部变得圆滑、轮廓系数整体下滑——体会这两种工具各自的失效边界。

## 6. 本讲小结

- k-means 以 SSE（distortion）为目标，做「分配到最近质心 ↔ 质心移到成员均值」的交替迭代；`cluster_centers_`、`labels_`、`inertia_` 分别是质心坐标、簇标签和最终 SSE。
- 目标非凸导致初始化敏感：`n_init` 多次重启取 inertia_ 最小的一次，`init='k-means++'` 让初始质心按 \(D(x)^2\) 加权概率彼此远离，双管齐下缓解坏局部最优。
- `make_blobs` 合成数据自带真实分团，是验证聚类行为的第一站；真实数据上 k-means 依赖欧氏距离，必须先标准化。
- 肘部法利用「SSE 随 k 单调下降、越过真实簇数后边际收益骤减」找拐点，但肘部可能圆滑难辨。
- 轮廓系数 \(s(i) = (b-a)/\max(a,b)\) 度量「簇内紧凑、簇间分离」，需至少 2 个簇；刀片图的排序 + 堆叠 + 平均虚线画法可直接复用到任何聚类结果上。
- 两种选 k 工具互补：肘部看整体方差结构，轮廓看逐样本归属质量；k=3 时书中数据的轮廓图三片均匀无负值，k=2 则暴露出合并两团的失败模式。

## 7. 下一步学习建议

本讲的 k-means 有两个先天局限：必须预先指定 k，且只擅长「凸圆形」团块。下一讲 **u10-l2 层次聚类与 DBSCAN** 将逐一回应：`scipy` 的 `linkage`/`dendrogram` 树状图让你「先看结构再剪枝选簇数」（不需要预先定 k），`AgglomerativeClustering` 实现自底向上合并，而 `DBSCAN` 用密度可达直接挖出任意形状的簇并标出噪声点。阅读源码时建议从 [ch10/ch10.py:L318](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch10/ch10.py#L318)（层次聚类一节）开始，并把本讲的 `silhouette_samples` 评估方法带过去对比各类算法在同一份数据上的表现。
