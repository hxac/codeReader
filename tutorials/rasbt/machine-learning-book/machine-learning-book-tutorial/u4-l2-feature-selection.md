# 序列特征选择 SBS 与特征重要性

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「为什么特征越少反而可能更好」——维数灾难与过拟合的关系。
2. 逐行读懂 `ch04/ch04.py` 中手写的 `SBS` 类：贪心式序列后向删除的两层循环、`_calc_score` 的打分封装、`indices_` / `subsets_` / `scores_` 三个属性的记录方式。
3. 会用 KNN 配合 SBS 画出「特征数 vs 验证准确率」曲线，并解读曲线与最终选出的 3 特征子集。
4. 会用 `RandomForestClassifier` 的 `feature_importances_` 得到另一种特征排序，理解它与 SBS 结果的一致与冲突之处，以及为什么会冲突。

## 2. 前置知识

### 2.1 维数灾难：特征不是越多越好

上一讲我们把 wine 数据集（178 个样本、13 个特征、3 个类别）清洗、编码、划分成了训练集和测试集。一个自然的想法是：13 个特征全用上，信息最全，效果应该最好。但在样本量有限时恰恰相反——特征越多，模型越容易「记住」训练数据里的噪声而不是规律，这叫**过拟合**；同时，基于距离的模型（如 KNN）在高维空间里样本彼此的距离会趋于相同，「最近邻」失去意义，这就是**维数灾难（curse of dimensionality）**。所以我们要做**特征选择（feature selection）**：从 13 个特征里挑出一个尽量小、但判别力尽量强的子集。

### 2.2 特征选择的三个流派

| 流派 | 思路 | 本章例子 |
| --- | --- | --- |
| 过滤式（filter） | 先按某个统计指标给特征打分，与模型无关 | 本章未展开 |
| 包裹式（wrapper） | 把「训练一个模型并评估」当作黑盒打分器，搜索最好的特征子集 | 手写的 `SBS` |
| 嵌入式（embedded） | 特征选择发生在模型训练内部 | L1 正则化逻辑回归（本章前文）、随机森林 `feature_importances_` |

本讲的两个主角 `SBS` 和 `feature_importances_` 分别是包裹式和嵌入式的代表。

### 2.3 贪心算法与 itertools.combinations

从 13 个特征里选子集，理论上有 \( 2^{13} = 8192 \) 种组合，穷举并逐一训练模型代价太大。**贪心算法（greedy algorithm）**的思路是：每一步只做「当前看起来最好」的局部选择，不回头。SBS（Sequential Backward Selection，序列后向选择）的贪心策略是——**从全集出发，每轮试着删掉一个特征，删哪个？删了之后验证分数最高的那种删法**。

「删掉一个」用 Python 标准库实现很方便：`itertools.combinations(indices, r=dim-1)` 会枚举从当前特征集合中选出 `dim-1` 个的所有组合。从 \( d \) 个里选 \( d-1 \) 个的组合数是

\[
\binom{d}{d-1} = d
\]

也就是说每一轮恰好有 \( d \) 个候选子集（每个候选对应「删掉某一个特征」）。

### 2.4 本讲会用到的其他工具

- `sklearn.base.clone(estimator)`：复制一个估计器，保留超参数但不保留训练出的权重，避免污染调用方传入的对象。
- `sklearn.metrics.accuracy_score(y_true, y_pred)`：打分函数，注意 `SBS` 里传的是**函数本身**（`scoring=accuracy_score`），不是字符串 `'accuracy'`。
- `np.argmax`：返回最大值所在下标；`np.argsort(...)[::-1]`：得到降序排列的下标。
- wine 数据的加载、70/30 分层划分与标准化在上一讲已跑通，本讲直接复用其产物 `X_train_std`、`X_test_std`、`y_train`、`y_test`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch04/ch04.py:L537-L585](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L537-L585) | 手写 `SBS` 类：构造参数、`fit` 的贪心循环、`transform`、`_calc_score` 打分 |
| [ch04/ch04.py:L591-L627](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L591-L627) | 用 KNN 驱动 SBS：画特征数-准确率曲线、取 3 特征子集、与全特征 KNN 对比 |
| [ch04/ch04.py:L636-L680](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L636-L680) | 随机森林 `feature_importances_` 排序、条形图、`SelectFromModel` 按阈值筛选 |
| [ch04/ch04.py:L357-L405](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L357-L405) | wine 数据加载（UCI URL，本地 `wine.data` 备用）、列名、70/30 分层划分、标准化（上一讲已讲） |
| [ch04/ch04.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.ipynb) | 同一份代码的 notebook 版，保存了运行输出（本讲引用的准确率数值来自它） |
| [ch04/wine.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/wine.data) | wine 数据本地副本，UCI 下载失败时可改用它 |

## 4. 核心概念与源码讲解

### 4.1 SBS：贪心序列后向选择与 fit 的两层循环

#### 4.1.1 概念说明

SBS 解决的问题是：**在给定评估器（这里是 KNN）的前提下，找到一组合适大小的特征子集，使该评估器在未见数据上的表现最好**。它属于包裹式方法——「好不好」不是靠统计指标衡量，而是真的训练一次模型、在验证集上打一次分来衡量。

它的贪心流程从特征全集开始，每轮固定删掉一个特征，直到剩 `k_features` 个为止。为什么「删」而不是「加」？从全集开始能保证每一步都在「当前最优」的底子上做减法，而且实现简单；代价是它**不保证全局最优**——如果第 2 轮删错了一个特征，后面再也无法挽回。这是所有贪心算法共同的局限。

#### 4.1.2 核心流程

```
输入: 特征 X, 标签 y, 目标特征数 k（这里 k=1）
1. 把 X 再切一份内部 训练集/验证集（75/25, random_state=1）
2. S ← 全部 13 个特征的列号; 用全集打一次分并记录
3. while |S| > k:
     候选 ← combinations(S, |S|-1)   # 恰好 |S| 个候选, 每个等于"删掉一个特征"
     对每个候选 p: 在内部训练集的 p 列上 fit, 在内部验证集的 p 列上 predict+打分
     S ← 分数最高的候选; 记录 S 与其分数
     |S| ← |S| - 1
4. k_score_ ← 最后一次记录的分数
```

从 13 个特征降到 1 个，`_calc_score` 总共被调用

\[
1 + \sum_{d=2}^{13} d = 1 + (2+3+\cdots+13) = 1 + 90 = 91
\]

次（1 次全集打分 + 每轮 \( d \) 个候选）。这个数字在后面的实践里会用代码验证。

#### 4.1.3 源码精读

先看构造函数。[ch04/ch04.py:L537-L544](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L537-L544) 定义了五个参数并立即用 `clone` 复制估计器：

```python
class SBS:
    def __init__(self, estimator, k_features, scoring=accuracy_score,
                 test_size=0.25, random_state=1):
        self.scoring = scoring
        self.estimator = clone(estimator)
        self.k_features = k_features
        self.test_size = test_size
        self.random_state = random_state
```

- `estimator`：任意遵循 fit/predict 范式的评估器（可替换，这正是包裹式的灵活性）。
- `scoring`：默认是 `accuracy_score` **函数对象**——因为 sklearn 的打分函数签名是 `(y_true, y_pred)`，可以直接传入。
- `clone(estimator)`：拿到一个全新未训练的副本，后续 `_calc_score` 反复 `fit` 的是这个副本，不会动调用方手里的对象（比如后面 [L618](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L618) 复用同一个 `knn` 变量时不会带上 SBS 的残留状态）。

再看 `fit` 的开头，[ch04/ch04.py:L546-L556](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L546-L556)：这段代码先把传入的（外层）训练数据再切成内部训练/验证两份，然后初始化 `dim`、全集列号 `indices_`、子集列表 `subsets_` 和分数列表 `scores_`，并用全部 13 个特征先打一次基准分。

```python
X_train, X_test, y_train, y_test = \
    train_test_split(X, y, test_size=self.test_size,
                     random_state=self.random_state)

dim = X_train.shape[1]
self.indices_ = tuple(range(dim))
self.subsets_ = [self.indices_]
score = self._calc_score(X_train, y_train, 
                         X_test, y_test, self.indices_)
self.scores_ = [score]
```

注意两点：其一，这里内部再切一刀，是为了**不碰外层测试集**——特征子集的选择信息只能来自训练侧，否则就是数据泄漏（承接 u3-l1 的原则）。传入的 `X_train_std` 有 124 个样本，`test_size=0.25` 再切后内部验证集约 31 个样本、内部训练集约 93 个（可用 `print` 验证）。其二，这个内部切分**没有加 `stratify`**，是这份教学实现的一个可以讨论的细节。

核心的两层循环在 [ch04/ch04.py:L558-L574](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L558-L574)：

```python
while dim > self.k_features:
    scores = []
    subsets = []

    for p in combinations(self.indices_, r=dim - 1):
        score = self._calc_score(X_train, y_train, 
                                 X_test, y_test, p)
        scores.append(score)
        subsets.append(p)

    best = np.argmax(scores)
    self.indices_ = subsets[best]
    self.subsets_.append(self.indices_)
    dim -= 1

    self.scores_.append(scores[best])
self.k_score_ = self.scores_[-1]
```

- **外层 `while`**：控制特征数从当前值一路降到 `k_features`，每轮 `dim -= 1`。
- **内层 `for`**：枚举「从当前 `self.indices_` 里保留 `dim-1` 个」的所有组合，逐个调 `_calc_score` 训练+打分。
- `np.argmax(scores)` 选出分数最高的候选（`argmax` 遇到并列时取第一个，因此结果是确定性的），把它设为新的 `self.indices_` 并追加进 `self.subsets_`。
- 循环结束后 `k_score_` 记录最终 `k_features` 个特征时的分数。

#### 4.1.4 代码实践

**实践目标**：不改任何源码，验证「从 13 特征降到 1 特征，估计器的 `fit` 恰好被调用 91 次」，并体会 `k_features` 对计算量的影响。

**操作步骤**（在能连网的机器上，先执行 ch04.ipynb 中到 `X_train_std` 为止的单元，并运行 SBS 类定义单元；离线时把 [L357-L359](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L357-L359) 的 URL 换成本地 `wine.data`）。然后运行下面的示例代码：

```python
# 示例代码：用子类给 fit 计数，不修改源码
from sklearn.neighbors import KNeighborsClassifier

class CountingKNN(KNeighborsClassifier):
    calls = 0
    def fit(self, X, y):
        CountingKNN.calls += 1
        return super().fit(X, y)

CountingKNN.calls = 0
sbs = SBS(CountingKNN(n_neighbors=5), k_features=1)
sbs.fit(X_train_std, y_train)
print('k_features=1 时 fit 调用次数:', CountingKNN.calls)

CountingKNN.calls = 0
sbs7 = SBS(CountingKNN(n_neighbors=5), k_features=7)
sbs7.fit(X_train_std, y_train)
print('k_features=7 时 fit 调用次数:', CountingKNN.calls)
```

**需要观察的现象**：两个打印出的次数。

**预期结果**：`k_features=1` 时为 91（= 1 + 2 + … + 13）；`k_features=7` 时为 64（= 1 + 8 + 9 + … + 13）。若与预期不符，检查是否重复运行了单元格导致计数器累加。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SBS.fit` 内部还要再做一次 `train_test_split`，而不是直接在传入的数据上打分？

**答案**：子集的「好坏」必须用一个没有参与拟合的数据来衡量。若用训练集自身的分数，特征越多分数几乎总越高，冗余特征永远删不掉；同时，若用外层测试集打分，选择过程就泄漏了测试集信息，最终汇报的泛化性能会被高估。所以在训练侧再切一份内部验证集（75/25）专门用于打分。

**练习 2**：`combinations(self.indices_, r=dim - 1)` 在 `dim=13` 时会生成多少个候选？它们分别是什么？

**答案**：13 个。每个候选等于「从当前 13 个特征中删掉恰好一个」，对应 \(\binom{13}{12} = 13\) 种删法。

**练习 3**：SBS 为什么可能选不到全局最优子集？

**答案**：它是贪心算法，每轮基于「删一个后当前验证分数最高」做不可逆的局部决策。早期删掉的特征无法恢复；而且打分本身受内部验证集（仅约 31 个样本）噪声影响，存在随机波动，所以只能得到近似最优解。

### 4.2 子集与分数的记录：indices_、subsets_、scores_ 与 _calc_score 封装

#### 4.2.1 概念说明

特征选择跑完后，我们最想知道的是两件事：**每一步到底留下了哪些特征**、**每种特征数对应的验证分数是多少**。`SBS` 用三个「带下划线后缀」的属性（sklearn 的惯例：训练后才存在的属性以 `_` 结尾）把整个搜索轨迹完整记录下来，方便事后画曲线和取子集。打分逻辑则被封装在一个私有方法 `_calc_score` 里，供全集基准分和循环内候选打分复用。

先澄清一个容易混淆的点：一些资料（包括本手册规划稿）把这个打分方法记作 `_accuracy_score`，但**源码里实际叫 `_calc_score`**，「准确率」并不是写死的——它通过 `scoring` 参数注入，默认值是 `sklearn.metrics.accuracy_score`。想换 F1、平衡准确率都只是传参问题。

#### 4.2.2 核心流程

三个属性与一个方法的分工：

| 成员 | 含义 | 例子（wine, k_features=1） |
| --- | --- | --- |
| `indices_` | tuple，最终选中的 `k_features` 个特征列号 | `(0, 1, 11)` 之类 |
| `subsets_` | list，从全集到 k 特征每一步的最优子集；第 `i` 个元素含 `13-i` 个特征 | `subsets_[10]` 含 3 个特征 |
| `scores_` | list，与 `subsets_` 一一对应的内部验证分数 | `scores_[0]` 是 13 特征的分数 |
| `k_score_` | 最终 `k_features` 特征的分数，即 `scores_[-1]` | 1 特征时的分数 |
| `transform(X)` | 按 `indices_` 取列，返回降维后的矩阵 | `X[:, self.indices_]` |
| `_calc_score(...)` | 私有打分器：fit → predict → scoring | 被调用 91 次 |

下标对应关系值得记住：`subsets_[i]` 的特征数 = 原始特征数 − i。因为 `subsets_[0]` 是 13 个特征，每循环一轮少一个。

#### 4.2.3 源码精读

`transform` 与 `_calc_score` 在 [ch04/ch04.py:L578-L585](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L578-L585)：

```python
def transform(self, X):
    return X[:, self.indices_]

def _calc_score(self, X_train, y_train, X_test, y_test, indices):
    self.estimator.fit(X_train[:, indices], y_train)
    y_pred = self.estimator.predict(X_test[:, indices])
    score = self.scoring(y_test, y_pred)
    return score
```

这两个方法都用了 NumPy 的**花式索引** `X[:, indices]`：`indices` 是特征列号组成的 tuple，按列抽取子矩阵——这就是「特征选择」在代码层面的物理实现，训练和预测必须用**同一组列号**切片，保证模型看到的特征一致。

`_calc_score` 的封装模式值得学习：它把「训练 → 预测 → 打分」三步固化成一个函数，`fit` 里两处调用（全集基准分、循环内候选打分）写法完全一致；评估器和打分函数都来自构造参数，方法内部零硬编码。这使得 `SBS` 天然支持「换评估器、换指标」的实验——本讲综合实践正是利用这一点。

另外注意：`SBS` 只实现了 `fit` 和 `transform`，没有 `predict`——它本身不是分类器，而是一个**类转换器（transformer）**的角色：吃全特征矩阵，吐精选子特征矩阵。分类仍要交给外部的评估器。

#### 4.2.4 代码实践

**实践目标**：把 SBS 的整条搜索轨迹（特征数、特征名、验证分数）打印成一张表，直观理解 `subsets_`/`scores_` 的对齐关系。

**操作步骤**：先按 [ch04/ch04.py:L591-L595](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L591-L595) 跑一遍 SBS（`sbs = SBS(knn, k_features=1); sbs.fit(X_train_std, y_train)`），再运行下面的示例代码：

```python
# 示例代码：打印每一步留下的特征与内部验证分数
for i, (sub, sc) in enumerate(zip(sbs.subsets_, sbs.scores_)):
    names = list(df_wine.columns[1:][list(sub)])
    print(f'{13-i:>2d} 个特征 | 验证准确率 {sc:.3f} | {names}')
```

**需要观察的现象**：13 行输出，特征数从 13 递减到 1；分数总体维持高位、在特征很少时明显下滑。

**预期结果**：特征数在 13 到 5~8 的区间内分数应接近 1.0（内部验证集只有约 31 个样本，分数以约 1/31≈0.032 的粒度跳变）；特征数降到 1~2 时准确率显著下降。另一个值得注意的现象：**`scores_` 不是单调递减的**——某些冗余特征被删掉后分数可能持平甚至上升。具体每一步的数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sbs.subsets_[10]` 恰好是 3 个特征的子集？

**答案**：`subsets_[0]` 含全部 13 个特征，此后每追加一个元素特征数减 1，所以 `subsets_[i]` 含 \( 13 - i \) 个特征；\( 13 - 10 = 3 \)。

**练习 2**：SBS 跑完后想直接把新的数据降到选中的 k 个特征，用哪个成员？

**答案**：`sbs.transform(X)`——它按 `self.indices_` 做列切片，返回 `X[:, self.indices_]`。

**练习 3**：想把打分标准从准确率换成平衡准确率（balanced accuracy），怎么改？

**答案**：`scoring` 参数接收任何签名为 `(y_true, y_pred)` 的可调用对象，例如 `from sklearn.metrics import balanced_accuracy_score` 之后 `SBS(knn, k_features=1, scoring=balanced_accuracy_score)`。这也是 `_calc_score` 不把指标写死的好处。

### 4.3 KNN 配合 SBS 评估：特征数-准确率曲线与三特征子集

#### 4.3.1 概念说明

`SBS` 是包裹式方法，它的结论**依赖所包裹的评估器**。本章选 KNN（`n_neighbors=5`）作为驱动评估器，原因有二：一是 KNN 是惰性学习、单次训练极快，适合被反复调用 91 次；二是 KNN 基于距离计算，对冗余特征和量纲极其敏感（承接 u2-l3、u3-l3 的结论），是展示「特征选择收益」的好载体。

一条重要的配套原则：**KNN 吃的是标准化后的 `X_train_std`**（上一讲 [ch04/ch04.py:L403-L405](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L403-L405) 产出）。Proline 的量级是数百到上千，而 Hue 只有零点几，不标准化的话 Proline 会主导所有距离，SBS 选出的子集也会失真。

#### 4.3.2 核心流程

1. 构造 `knn = KNeighborsClassifier(n_neighbors=5)`。
2. `sbs = SBS(knn, k_features=1)`，在 `X_train_std, y_train` 上 `fit`。
3. 横轴 `k_feat = [len(k) for k in sbs.subsets_]`（13, 12, …, 1），纵轴 `sbs.scores_`，画折线。
4. 从 `sbs.subsets_[10]` 取出 3 特征子集 `k3`，翻译成特征名。
5. 用**外层**测试集对比：全特征 KNN vs 仅 k3 特征的 KNN，看压缩代价有多大。

#### 4.3.3 源码精读

驱动与绘图在 [ch04/ch04.py:L591-L607](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L591-L607)：这段代码创建 KNN 评估器、以 `k_features=1` 运行 SBS，然后把每个子集的特征数作为 x 轴、对应内部验证准确率作为 y 轴画折线，y 轴范围固定在 [0.7, 1.02] 以放大细节。

```python
knn = KNeighborsClassifier(n_neighbors=5)

sbs = SBS(knn, k_features=1)
sbs.fit(X_train_std, y_train)

k_feat = [len(k) for k in sbs.subsets_]

plt.plot(k_feat, sbs.scores_, marker='o')
plt.ylim([0.7, 1.02])
plt.ylabel('Accuracy')
plt.xlabel('Number of features')
```

取出 3 特征子集并翻译成特征名在 [ch04/ch04.py:L612-L613](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L612-L613)：

```python
k3 = list(sbs.subsets_[10])
print(df_wine.columns[1:][k3])
```

`df_wine.columns[1:]` 跳过第一列 `Class label`，把 0 起的列号映射回特征名。notebook 中保存的运行结果是：

```
Index(['Alcohol', 'Malic acid', 'OD280/OD315 of diluted wines'], dtype='object')
```

最后是与全特征 KNN 的对照，[ch04/ch04.py:L618-L627](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L618-L627)：先用 13 个特征训练 KNN 并在**外层测试集**上评估，再只用 `k3` 三列（`X_train_std[:, k3]`，训练与测试用同一组列号切片）训练评估一次。notebook 记录的输出为：

| 特征 | 训练准确率 | 测试准确率 |
| --- | --- | --- |
| 全部 13 个 | 0.9677 | 0.9630 |
| 仅 3 个（k3） | 0.9516 | 0.9259 |

注意这里的 subtle 之处：SBS 内部验证曲线上 3 特征的分数看起来很高，但那是**挑选子集时用的那份内部验证集**（约 31 个样本）上的分数——子集正是在它上面取最大值挑出来的，天然带乐观偏差；换到真正的外层测试集（54 个样本）上，3 特征的 0.9259 低于 13 特征的 0.9630。结论不是「3 特征更好」，而是「用 1/4 的特征只损失约 3.7 个百分点的测试准确率」——如果目标是压缩成本，这笔交易可能划算。

#### 4.3.4 代码实践

**实践目标**：复现上表，并额外考察 5 特征子集，验证「特征越多未必越好」。

**操作步骤**：在 notebook 中依次运行 SBS 相关单元后，运行下面的示例代码：

```python
# 示例代码：k3 与 k5 子集在真正测试集上的表现
k5 = list(sbs.subsets_[8])          # 13 - 8 = 5 个特征
print('k5 特征:', list(df_wine.columns[1:][k5]))

for name, cols in [('13 特征', slice(None)), ('k5(5 特征)', k5), ('k3(3 特征)', k3)]:
    knn.fit(X_train_std[:, cols], y_train)
    print(f"{name:>10s} | train {knn.score(X_train_std[:, cols], y_train):.4f}"
          f" | test {knn.score(X_test_std[:, cols], y_test):.4f}")
```

**需要观察的现象**：三行的训练/测试准确率；k5 与 k3 的测试准确率谁更接近全特征版本。

**预期结果**：13 特征行为 0.9677/0.9630，k3 行为 0.9516/0.9259（与 notebook 保存输出一致）；k5 的测试准确率介于两者之间或与全特征接近（待本地验证）。如果 k5 的测试分数与 13 特征几乎相同，就说明另外 8 个特征对 KNN 是近似冗余的。

#### 4.3.5 小练习与答案

**练习 1**：为什么喂给 SBS 的是 `X_train_std` 而不是 `X_train`？

**答案**：驱动评估器 KNN 依赖欧氏距离，未标准化时大量纲特征（Proline）会淹没小量纲特征（Hue），距离失真，选出的子集也不可信。树模型无此要求，但 KNN/逻辑回归/SVM 都必须先标准化。

**练习 2**：SBS 内部验证分数很高，外层测试集上却降到了 0.9259，怎么解释？

**答案**：一是子集挑选本身就是在那份约 31 个样本的内部验证集上反复取最大值，存在选择性乐观偏差；二是样本太少导致分数粒度粗（一步约 1/31）。外层测试集（54 个样本）是子集选择过程从未见过的数据，其分数才是对泛化能力的诚实估计。

**练习 3**：把 `k_features` 从 1 改成 7，`subsets_` 的长度和 `k_feat` 曲线的横轴范围分别变成多少？

**答案**：`subsets_` 长度为 7（13, 12, …, 7），`k_feat` 横轴从 7 到 13；循环只删 6 轮，`_calc_score` 调用次数也从 91 降到 64。

### 4.4 随机森林 feature_importances_：另一种特征排序

#### 4.4.1 概念说明

包裹式（SBS）之外，嵌入式方法在训练过程中「顺手」给出特征重要性。随机森林的做法是**平均不纯度减少（Mean Decrease Impurity, MDI）**：决策树每个节点分裂都为了让子节点更「纯」（本章前面用基尼不纯度度量，见 u3-l3）；某次分裂如果用特征 \( j\) 作为分裂特征、让不纯度下降了 \( \Delta I\)，且该节点覆盖了占总样本比例 \( p_t \) 的样本，就把这份「功劳」记给特征 \( j\)。对森林里 \( N \) 棵树求平均并归一化：

\[
\text{Importance}_j = \frac{1}{N} \sum_{\text{trees}} \sum_{\text{节点 } t:\ \text{按特征 } j \text{ 分裂}} p_t \, \Delta I_t,
\qquad \sum_j \text{Importance}_j = 1
\]

重要性高 ≈ 这个特征经常被用来做「收益大的分裂」。它训练一次就得到全部 13 个特征的排序，比 SBS 的 91 次 fit 便宜得多；但也要清楚它的两个偏差：对取值多/连续的特征更友好；**相关的特征会互相分摊重要性**（两个几乎等价的特征，各自只能拿到一半功劳）。

#### 4.4.2 核心流程

1. `feat_labels = df_wine.columns[1:]` 取特征名。
2. 训练 `RandomForestClassifier(n_estimators=500, random_state=1)`，**注意喂的是未标准化的 `X_train`**。
3. 读 `forest.feature_importances_`，用 `np.argsort(...)[::-1]` 得到降序下标。
4. 打印排序表并画条形图。
5. （可选延伸）`SelectFromModel(forest, threshold=0.1, prefit=True)` 按阈值自动筛特征。

#### 4.4.3 源码精读

训练与排序在 [ch04/ch04.py:L636-L649](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L636-L649)：这段代码在**未标准化**的 `X_train` 上训练 500 棵树的随机森林，取出 `feature_importances_`（长度 13、总和为 1 的数组），用 `argsort` 反转得到从大到小的特征下标 `indices`，再循环打印「排名-特征名-重要性」：

```python
feat_labels = df_wine.columns[1:]

forest = RandomForestClassifier(n_estimators=500,
                                random_state=1)

forest.fit(X_train, y_train)
importances = forest.feature_importances_

indices = np.argsort(importances)[::-1]

for f in range(X_train.shape[1]):
    print("%2d) %-*s %f" % (f + 1, 30, 
                            feat_labels[indices[f]], 
                            importances[indices[f]]))
```

为什么不用标准化？树的分裂只比较「某特征是否超过阈值」，对特征做单调缩放不会改变样本在该特征上的排序，分裂点集合不变——所以树模型对特征缩放天然免疫（承接 u3-l3 的结论，这里正好是一次印证）。

条形图在 [ch04/ch04.py:L651-L661](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L651-L661)，按重要性降序画柱、x 轴刻度旋转 90 度放特征名。notebook 保存的排序前五名是：

| 排名 | 特征 | 重要性 |
| --- | --- | --- |
| 1 | Proline | 0.185453 |
| 2 | Flavanoids | 0.174751 |
| 3 | Color intensity | 0.143920 |
| 4 | OD280/OD315 of diluted wines | 0.136162 |
| 5 | Alcohol | 0.118529 |

按阈值自动筛选在 [ch04/ch04.py:L667-L680](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L667-L680)：`SelectFromModel(forest, threshold=0.1, prefit=True)` 复用已训练好的森林，只保留重要性 ≥ 0.1 的特征，`transform` 后 `X_train` 从 13 列降到 5 列，随后打印这 5 个特征（即上表五行）：

```python
sfm = SelectFromModel(forest, threshold=0.1, prefit=True)
X_selected = sfm.transform(X_train)
print('Number of features that meet this threshold criterion:', 
      X_selected.shape[1])
```

两点提醒：其一，notebook 里该处的 markdown 说明写的是「print the 3 features」，但代码按 `X_selected.shape[1]` 打印，实际输出 5 个特征——以代码和输出为准（这也呼应 u1-l1「书、代码不一致时以代码为准」的原则）。其二，`prefit=True` 在本书使用的 scikit-learn 1.0 中有效，但在更新的 sklearn 版本中该参数已被弃用/移除，需改为先 `fit` 再 `transform` 或使用 Pipeline（具体行为待本地验证）。

#### 4.4.4 代码实践

**实践目标**：通过扫描 `SelectFromModel` 的阈值，建立「重要性阈值 ↔ 保留特征数」的直觉。

**操作步骤**：运行完森林单元后，执行下面的示例代码：

```python
# 示例代码：阈值扫描，观察保留特征数
from sklearn.feature_selection import SelectFromModel

for th in [0.20, 0.15, 0.10, 0.05]:
    sfm = SelectFromModel(forest, threshold=th, prefit=True)
    n_kept = sfm.transform(X_train).shape[1]
    kept = list(feat_labels[sfm.get_support()])
    print(f'threshold={th:<5} 保留 {n_kept} 个特征: {kept}')
```

**需要观察的现象**：每个阈值下保留的特征个数与特征名。

**预期结果**：基于 notebook 记录的重要性数值可推算：threshold=0.10 时保留 5 个（与 notebook 输出一致）；threshold=0.15 时保留 3 个（Proline、Flavanoids、Color intensity）；threshold=0.20 时保留 0 个（最高的重要性 0.185 < 0.2，此时会报错或给出空集，属于「阈值过高」的教训）；threshold=0.05 时保留个数大于 5（具体数值待本地验证，依赖 sklearn 版本带来的重要性差异）。若 `prefit` 报错，请按上一小节的提醒改写。

#### 4.4.5 小练习与答案

**练习 1**：随机森林这里为什么可以直接用未标准化的 `X_train`？

**答案**：决策树在每个节点只做「特征值 > 阈值」的比较，单调变换（如标准化、归一化）不改变样本在单特征上的排序，最优分裂等价，因此树与森林不需要特征缩放。

**练习 2**：`forest.feature_importances_` 这个数组所有元素之和是多少？它和某一棵树的 importances 有何不同？

**答案**：sklearn 会归一化，和为 1；森林的值是 500 棵树各自重要性的平均再归一化，方差更小、更稳定，单棵树的重要性噪声很大。

**练习 3**：对比 SBS 选出的 k3 与随机森林 top-5，指出一致与冲突，并给出至少两条原因。

**答案**：一致：`Alcohol` 和 `OD280/OD315 of diluted wines` 同时出现在两份名单里，说明这两个特征对多种模型都有用。冲突：随机森林前三名（Proline、Flavanoids、Color intensity）不在 k3 里，而 k3 的 `Malic acid` 不在森林 top-5 里。原因：① 评估器不同——SBS 的子集是「让 KNN（k=5，标准化数据，局部距离）表现最好」的子集，森林的重要性度量的是「对全局树分裂不纯度的贡献」；② 森林中相关特征（如 Flavanoids 与 Total phenols、OD280 高度相关）会互相分摊重要性，SBS 则只需子集里保留一个代表即可；③ 两者用的数据版本（标准化/未标准化）与打分集（31 样本内部验证/整棵树的全量分裂统计）也不同。

## 5. 综合实践

**任务**：把 SBS 内部的评估器从 KNN 换成 `LogisticRegression`，重新跑出「特征数 vs 验证准确率」曲线，比较选出的特征子集是否与 KNN 版本一致，并解释差异。

**实践目标**：亲身体会包裹式特征选择「结论随评估器而变」的本质，并交叉验证三种视角（KNN-SBS、LR-SBS、随机森林重要性）。

**操作步骤**（在跑通 ch04 数据准备与 SBS 类定义的前提下）：

1. 用逻辑回归替换驱动评估器，注意它和 KNN 一样需要标准化数据，参数写法对齐书中 [L460](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L460) 的用法：

```python
# 示例代码：评估器换成 LogisticRegression 的 SBS
from sklearn.linear_model import LogisticRegression

lr = LogisticRegression(penalty='l2', C=1.0, solver='liblinear',
                        multi_class='ovr', random_state=0)
sbs_lr = SBS(lr, k_features=1)
sbs_lr.fit(X_train_std, y_train)

k_feat = [len(k) for k in sbs_lr.subsets_]
plt.plot(k_feat, sbs_lr.scores_, marker='o', label='LR')
plt.plot(k_feat, sbs.scores_, marker='^', label='KNN')
plt.ylim([0.7, 1.02]); plt.xlabel('Number of features')
plt.ylabel('Accuracy'); plt.legend(); plt.grid()
plt.show()

k3_lr = list(sbs_lr.subsets_[10])
print('LR 选出的 3 特征:', list(df_wine.columns[1:][k3_lr]))
print('KNN 选出的 3 特征:', list(df_wine.columns[1:][k3]))
```

2. （若 sklearn 版本较新，`multi_class` 参数可能触发弃用警告，可去掉该参数重跑，结果可能略有差异。）
3. 把 LR 的 3 特征子集与 KNN 的 `['Alcohol', 'Malic acid', 'OD280/OD315 of diluted wines']`、随机森林 top-5（Proline、Flavanoids、Color intensity、OD280/OD315、Alcohol）放在一起对比，统计交集。
4. 用 4.3.4 的方式在**外层测试集**上评估「LR + 全特征」vs「LR + k3_lr」，看压缩代价。
5. 写下你对差异的解释，对照 4.4.5 练习 3 的三条原因。

**需要观察的现象**：两条曲线的形状差异（LR 在少特征时通常衰减更平缓，待本地验证）；两个 3 特征子集的交集大小；LR 是否更偏爱与其线性可分类别边界相关的特征。

**预期结果**：两个子集大概率**不完全一致**，但会有明显交集（`OD280/OD315 of diluted wines`、`Alcohol`、`Flavanoids`、`Proline` 这几个特征在 wine 数据上对多种模型都重要，具体组合待本地验证）。差异的根源：KNN 用局部距离、无参数模型，偏好「在局部邻域内区分样本」的特征组合；逻辑回归是线性模型，偏好「能被线性组合分开类别」的特征；MDI 则只统计分裂贡献、并受相关特征分摊影响。这正是包裹式方法「为特定模型定制子集」的含义——没有放之四海皆准的「最重要特征」，只有「对某个模型、某个指标而言最好的子集」。

## 6. 本讲小结

- 特征选择三大流派：包裹式（SBS）、嵌入式（随机森林 MDI、L1 正则）、过滤式；SBS 把「训练+验证打分」当黑盒来搜索子集。
- `SBS.fit` 的两层循环：外层 `while` 控制特征数降到 `k_features`，内层用 `combinations(indices, dim-1)` 枚举「删一个」的 \( d \) 个候选、逐个 `_calc_score`，`np.argmax` 贪心保留最优；13→1 共调用 91 次 fit。
- 轨迹记录靠 `indices_`（最终子集列号）、`subsets_`（第 i 项含 13−i 个特征）、`scores_`（对齐的内部验证分）；`transform` 用花式索引 `X[:, indices_]` 取列；打分函数经 `scoring` 参数注入，默认 `accuracy_score`。
- KNN 驱动的 SBS 在内部验证集上 3 特征即可保持高准确率，但在真正测试集上为 0.9259（全特征 0.9630）——内部验证分数有选择性乐观偏差，最终判断要用外层测试集。
- 随机森林 `feature_importances_` 是平均不纯度减少（MDI），一次训练得到全部特征排序（top-5：Proline、Flavanoids、Color intensity、OD280/OD315、Alcohol），树模型无需标准化；`SelectFromModel` 可按阈值自动筛选。
- 两套排序部分重合（Alcohol、OD280/OD315），冲突源于评估器差异、相关特征分摊重要性与数据缩放——特征重要性是「相对于某模型/某度量」的概念。

## 7. 下一步学习建议

下一讲（u5-l1）进入第 5 章降维主题：**PCA 主成分分析**。你会发现 PCA 与本讲的 SBS 形成有趣对照——SBS 是「从原有特征里挑选子集」（保留物理含义），PCA 则是「把 13 个特征线性组合成少数几个主成分」（保留最大方差但失去原始含义）。建议带着两个问题去读：

1. 本讲中反复出现的 `X_train_std`（标准化）在 PCA 里为什么从「建议」升级为「必须」？
2. 随机森林告诉我们的 Proline（量纲最大）与 PCA 的第一主成分方向有什么关系？

源码上可以直接衔接 [ch05/ch05.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch05/ch05.py) 中对同一份 wine 数据的手写 PCA 五步流程；此外也可以回头看看本章前文 [ch04/ch04.py:L441-L527](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L441-L527) 的 L1 正则化稀疏解——它与本讲的 MDI 同属嵌入式特征选择，第三种「边训练边筛特征」的思路。
