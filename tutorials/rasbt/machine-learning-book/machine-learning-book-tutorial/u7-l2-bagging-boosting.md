# Bagging 与 Boosting 家族

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 bagging（bootstrap aggregating）的两大组成：**bootstrap 有放回采样**与**聚合**，以及它为什么主要降低方差。
2. 读懂 `BaggingClassifier` 的每个关键参数：`base_estimator`、`n_estimators`、`max_samples`、`max_features`、`bootstrap`、`bootstrap_features`，并理解**袋外（out-of-bag, OOB）样本**从哪里来、怎么当免费的验证集用。
3. 跟着 ch07.py 中那段 10 个样本的手工计算，逐步推出 SAMME 提升算法的权重更新公式，理解 AdaBoost「让后续弱学习器专注前一轮分错的样本」的机制。
4. 理解梯度提升（gradient boosting）「拟合损失函数负梯度」的思想，以及 `XGBClassifier` 与 scikit-learn 自带 `GradientBoostingClassifier` 的接口差异。
5. 在同一份 Wine 二分类数据上，亲手对比深单树、Bagging、AdaBoost、XGBoost 的训练/测试准确率与决策边界。

## 2. 前置知识

本讲是第 7 章集成学习的下半场，需要以下已建立的概念：

- **多数投票集成（上一讲 u7-l1）**：集成收益来自成员错误的独立性；只要单个成员的错误率低于 0.5 且错误相互独立，投票错误率随成员数指数下降。上一讲用「不同类型的模型」（逻辑回归 + 决策树 + KNN）制造多样性；本讲的两类方法改用**数据**制造多样性——bagging 重采样样本，boosting 重新加权样本。
- **决策树（u3-l3）**：不纯度指标、`max_depth` 剪枝。本讲所有集成的基学习器（base learner）都是决策树：bagging 用不剪枝的深树，AdaBoost 用深度为 1 的树桩（stump）。
- **数据划分与评估（u6 系列）**：`train_test_split(stratify=...)` 分层划分、`accuracy_score`、训练集/测试集准确率对比是本讲的评估手段。
- **bootstrap（自助采样）**：从 n 个样本中**有放回地**抽 n 次，得到一个大小仍为 n、但内容略有不同的「新数据集」。某个特定样本一次都没被抽中的概率是

  \( P(\text{从未抽中}) = \left(1 - \frac{1}{n}\right)^n \approx e^{-1} \approx 0.368 \)

  这个 36.8% 就是后面 OOB 评估的数学来源。
- **版本提示**：本仓库钉死 scikit-learn 1.0 与 xgboost 1.5.0（见 [ch07/ch07.py:L52-L60](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L52-L60) 的版本检查，其中 `'sklearn': '1.0'`、`'xgboost': '1.5.0'`）。在更新的 scikit-learn 里，`BaggingClassifier` 与 `AdaBoostClassifier` 的 `base_estimator` 参数已改名为 `estimator`（1.2 引入、1.4 移除旧名）；`AdaBoostClassifier` 的 `algorithm='SAMME.R'` 也已在 1.4 被弃用、1.6 起移除。如果你用新版环境跑本讲代码，需要做这些替换。

> 一个诚实的更正：本讲大纲原文写的是「对比 gradient boosting 与 XGBoost 在乳腺癌数据集上的表现」，但仓库 ch07.py 实际使用的**始终是 Wine 数据集的二类子集**（详见 4.1.3），本讲以源码为准。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [ch07/ch07.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L552-L890) | 第 7 章全部可执行代码。前半部分（L106-L548）是上一讲的多数投票集成；本讲聚焦 L552-L890：bagging（L552-L694）、AdaBoost（L698-L833）、梯度提升与 XGBoost（L836-L890） |
| [ch07/ch07.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.ipynb) | 与 .py 等价的 notebook，**内含当年运行的输出**（各模型准确率），本讲引用的数值都来自这些保存的输出 |
| [ch07/wine.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/wine.data) | Wine 数据集的本地副本。代码默认从 UCI 网址下载，断网时可用它兜底 |

相关导入集中在 [ch07/ch07.py:L29-L32](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L29-L32)：`BaggingClassifier`、`accuracy_score`、`AdaBoostClassifier`、`xgboost`。

## 4. 核心概念与源码讲解

### 4.1 Bagging：bootstrap 聚合与 BaggingClassifier

#### 4.1.1 概念说明

上一讲的多数投票靠「模型种类不同」制造多样性。bagging 反其道而行：**用同一个模型配方，喂不同的数据副本**。

- **bootstrap（自助采样）**：从训练集 D（n 个样本）中有放回地抽 n 个样本，得到 D₁；再抽得到 D₂……每个 D_b 都与 D 大小相同、约 63.2% 的样本来自原集（有些重复出现），另外约 36.8% 的样本没被抽到。
- **aggregating（聚合）**：在每个 D_b 上训练一个基学习器 h_b，分类任务投票、回归任务平均。

为什么这能奏效？深决策树是**低偏差、高方差**的模型——换一批训练数据，长出来的树可能大不相同。bagging 对 500 棵这样的树取平均，把「单棵树对特定数据集的敏感度」平均掉，方差显著下降，而偏差几乎不变。这与多数投票集成的数学基础（上一讲的二项分布误差压缩）一脉相承：只要每棵树的错误率低于 0.5 且错误不完全相关，投票就会更准。

**OOB（out-of-bag）样本**是 bagging 的免费午餐：对第 b 棵树来说，约 36.8% 的训练样本从未参与它的训练。用这些样本去「考核」这棵树，再把所有树在各自 OOB 样本上的表现聚合，就得到了一个**不需要额外划分验证集**的泛化误差估计，称为 OOB 分数。

bagging 与多数投票的关键区别：

| 维度 | 多数投票（u7-l1） | bagging |
| --- | --- | --- |
| 成员多样性来源 | 不同种类的模型 | 同一种模型 + 不同的 bootstrap 样本 |
| 成员间关系 | 异质，并行训练 | 同质，并行训练 |
| 主要作用 | 降方差（兼可降偏差） | 降方差 |
| 典型参数 | `classifiers` 列表 | `base_estimator` + 采样参数 |

#### 4.1.2 核心流程

bagging 训练与预测的伪代码：

```text
训练（可并行）:
for b in 1..B:                       # B = n_estimators, 本讲取 500
    D_b = 从训练集 D 中有放回抽 n 个样本   # max_samples=1.0 → 抽满 n 个
    h_b = base_estimator.fit(D_b)    # 每棵树只看到 D_b

预测:
    分类: 各树投票（或平均 predict_proba）
    回归: 各树输出取平均

OOB 评估:
    对样本 i, 只用「没见过 i」的那些树给 i 预测, 聚合后与 y_i 比对
```

#### 4.1.3 源码精读

**（1）数据准备：Wine 二类子集**

[ch07/ch07.py:L570-L603](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L570-L603) 这段代码读入 Wine 数据、把它变成一个二分类问题并完成划分：

```python
df_wine = pd.read_csv('https://archive.ics.uci.edu/ml/'
                      'machine-learning-databases/wine/wine.data',
                      header=None)
...
# drop 1 class
df_wine = df_wine[df_wine['Class label'] != 1]

y = df_wine['Class label'].values
X = df_wine[['Alcohol', 'OD280/OD315 of diluted wines']].values

le = LabelEncoder()
y = le.fit_transform(y)

X_train, X_test, y_train, y_test = train_test_split(X, y,
                             test_size=0.2,
                             random_state=1,
                             stratify=y)
```

要点：

- Wine 有 3 类（59/71/48 个样本），`!= 1` 丢掉第 1 类后剩 119 个样本，问题变成「第 2 类 vs 第 3 类」的二分类。
- 只取两个特征（Alcohol、OD280/OD315），方便画二维决策区域。
- `LabelEncoder` 把标签 2/3 变成 0/1——这一步对后面的 XGBoost 也是必须的（它要求类别标签是从 0 开始的整数）。
- 分层 80/20 划分后：约 95 个训练样本、24 个测试样本。
- 断网时可以改用本地文件（代码注释里就写了）：[ch07/ch07.py:L580-L584](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L580-L584)，即 `pd.read_csv('wine.data', header=None)`。

**（2）构造深树与 BaggingClassifier**

[ch07/ch07.py:L609-L620](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L609-L620) 创建了基学习器和集成器：

```python
tree = DecisionTreeClassifier(criterion='entropy',
                              max_depth=None,
                              random_state=1)

bag = BaggingClassifier(base_estimator=tree,
                        n_estimators=500,
                        max_samples=1.0,
                        max_features=1.0,
                        bootstrap=True,
                        bootstrap_features=False,
                        n_jobs=1,
                        random_state=1)
```

参数逐个解释：

| 参数 | 值 | 含义 |
| --- | --- | --- |
| `base_estimator` | `tree` | 基学习器：一棵**完全不剪枝**（`max_depth=None`）的熵决策树——低偏差、高方差的极端选择，正是 bagging 最喜欢的原料 |
| `n_estimators` | 500 | 集成 500 棵树 |
| `max_samples` | 1.0 | 每棵树的 bootstrap 样本数 = 训练集样本数（1.0 × 95） |
| `max_features` | 1.0 | 每棵树用全部特征 |
| `bootstrap` | True | **样本**有放回抽样——bagging 的开关 |
| `bootstrap_features` | False | 特征不做有放回抽样（它设 True 时就是「随机子空间法」） |
| `n_jobs` / `random_state` | 1 / 1 | 并行度与随机种子 |

注意 `base_estimator` 传入的是**未拟合**的 `tree` 对象，`BaggingClassifier` 内部会为每棵树 clone 一份再各自拟合——与上一讲 `MajorityVoteClassifier.fit` 里 `clone(clf).fit(...)` 的套路一致。

**（3）训练与准确率对比**

[ch07/ch07.py:L627-L643](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L627-L643) 先后拟合单棵深树和 bagging 集成，各自打印训练/测试准确率：

```python
tree = tree.fit(X_train, y_train)
y_train_pred = tree.predict(X_train)
y_test_pred = tree.predict(X_test)
tree_train = accuracy_score(y_train, y_train_pred)
tree_test = accuracy_score(y_test, y_test_pred)
...
bag = bag.fit(X_train, y_train)
...
bag_train = accuracy_score(y_train, y_train_pred)
bag_test = accuracy_score(y_test, y_test_pred)
```

notebook 中保存的输出是：

```text
Decision tree train/test accuracies 1.000/0.833
Bagging train/test accuracies 1.000/0.917
```

解读：单棵深树把训练集背得滚瓜烂熟（1.000），但测试只有 0.833——典型的**高方差/过拟合**；bagging 同样记住了训练集，测试却升到 0.917（24 个测试样本错 4 个 → 错 2 个）。这正是「平均掉方差」的直接证据。

**（4）决策区域可视化**

[ch07/ch07.py:L650-L694](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L650-L694) 用网格预测 + `contourf` 画出两幅并排的决策区域（与上一讲 u2-l1 介绍的 `plot_decision_regions` 思路相同，这里是手写版本）：

```python
xx, yy = np.meshgrid(np.arange(x_min, x_max, 0.1),
                     np.arange(y_min, y_max, 0.1))
...
for idx, clf, tt in zip([0, 1],
                        [tree, bag],
                        ['Decision tree', 'Bagging']):
    clf.fit(X_train, y_train)
    Z = clf.predict(np.c_[xx.ravel(), yy.ravel()])
    Z = Z.reshape(xx.shape)
    axarr[idx].contourf(xx, yy, Z, alpha=0.3)
```

预期现象：左图（单树）的决策边界由许多平行于坐标轴的小碎块拼成；右图（bagging）的边界明显更平滑、更连贯——500 棵树的「碎块」投票平均后，噪声式的碎块被抹掉了。

**（5）OOB 评估：仓库没写，我们补上**

ch07.py 的 `BaggingClassifier` **没有启用** `oob_score` 参数（源码事实，别在书里找不存在的代码）。它是可选参数，下面是示例代码（非仓库代码，基于 scikit-learn 1.0 的 API）：

```python
# 示例代码：OOB 评估
bag_oob = BaggingClassifier(base_estimator=tree,        # sklearn>=1.2 改用 estimator=
                            n_estimators=500,
                            max_samples=1.0,
                            max_features=1.0,
                            bootstrap=True,
                            oob_score=True,             # 打开 OOB 开关
                            random_state=1)
bag_oob.fit(X_train, y_train)
print(bag_oob.oob_score_)               # OOB 准确率
print(bag_oob.oob_decision_function_)   # 形状 (n_train_samples, n_classes)
```

`oob_score_` 应当落在训练准确率（1.000）与测试准确率（0.917）之间，且通常更接近后者——因为它评估的是「模型对没见过的数据的预测」。`oob_decision_function_` 则给出每个训练样本由「没见过它的那些树」投票出的类别得分，可以看到 bagging 其实也能输出软投票。

#### 4.1.4 代码实践

**实践目标**：复现「bagging 误分类样本突出显示图」——在仓库决策区域图的基础上，把分错的样本圈出来，直观感受 bagging 相比单树减少了哪些错误。

**操作步骤**（示例代码，基于 [ch07/ch07.py:L650-L694](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L650-L694) 的绘图骨架扩展）：

1. 先按 4.1.3 的（1）（2）（3）跑完数据准备与两个模型的 `fit`。
2. 第一次尝试——在**训练集**上找误分样本：

   ```python
   # 示例代码
   mis_train = np.where(bag.predict(X_train) != y_train)[0]
   print(f'bagging 在训练集上误分数: {len(mis_train)}')
   ```

3. 你会发现 `mis_train` 是**空的**（训练准确率 1.000），训练集上无圈可画——这本身就是重要观察。
4. 改到**测试集**上，把两个模型的误分样本都圈出来：

   ```python
   # 示例代码：在测试集上圈出误分样本
   for idx, clf, tt in zip([0, 1], [tree, bag],
                           ['Decision tree', 'Bagging']):
       mis = np.where(clf.predict(X_test) != y_test)[0]
       Z = clf.predict(np.c_[xx.ravel(), yy.ravel()]).reshape(xx.shape)
       axarr[idx].contourf(xx, yy, Z, alpha=0.3)
       axarr[idx].scatter(X_test[:, 0], X_test[:, 1], c='gray', s=30)
       axarr[idx].scatter(X_test[mis, 0], X_test[mis, 1],
                          s=250, facecolors='none',
                          edgecolors='red', linewidths=2)
       axarr[idx].set_title(f'{tt} ({len(mis)} misclassified)')
   ```

   （`xx, yy` 与 `axarr` 来自仓库绘图代码；这里把散点换成了测试样本，误分样本加红圈。）

**需要观察的现象**：红圈出现在两幅图中两类样本交叠、决策边界附近的位置；左图（单树）红圈更多。

**预期结果**：按 notebook 记录的测试准确率（0.833 vs 0.917）与 24 个测试样本推算，单树约 4 个误分、bagging 约 2 个误分，且 bagging 的误分点大概率也落在单树误分点附近（错误边界区域）。具体圈中哪些样本**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `bootstrap=True` 改成 `bootstrap=False`（且保持 `max_samples=1.0`），集成会发生什么？

**答案**：每棵树抽到的都是**完整的原始训练集**（不放回抽满 n 个 = 全量数据），500 棵树长得几乎一样（仅因 `random_state` 下的平局处理可能有极微差异），聚合后近似等于单棵树，方差降低的收益消失。这从反面说明：bagging 的多样性完全来自 bootstrap 的随机性。

**练习 2**：既然每棵树只见到约 63.2% 的训练样本，为什么 bagging 反而更准，而不是「每棵树学得更差导致更差」？

**答案**：单棵树确实略有变弱，但两件事补偿了它：其一，bootstrap 样本与原样本同分布，单树期望性能只是略降；其二，500 棵树的错误互不相关（对同一测试点，不同树犯错的「方向」不同），投票平均后方差按上一讲的二项分布规律指数级压缩。bagging 用「略弱但独立」换「平均后更稳」，净收益为正。

**练习 3**：OOB 分数能替代测试集评估吗？

**答案**：它是泛化误差的**近似无偏估计**，量级上通常接近测试集表现（本例约 0.9 上下），可用于选超参数（如 `n_estimators`、`max_samples`）而无需再切验证集；但最终汇报性能时，仍建议用一次从未参与任何决策的测试集（或交叉验证）确认，因为 OOB 样本毕竟来自训练数据分布。

### 4.2 AdaBoost：SAMME 权重提升与 AdaBoostClassifier

#### 4.2.1 概念说明

bagging 是**并行**算法：各树互不相识，事后聚合。boosting 是**串行**算法：第 b 轮的训练依赖于第 b-1 轮的结果，每一轮把注意力投向前面分错的样本。

AdaBoost（adaptive boosting，自适应提升）的套路是**样本重加权**：

1. 初始时每个训练样本权重相等（均为 1/n）。
2. 训练一个**弱学习器**（weak learner）——只比随机猜测略好一点，比如深度为 1 的树桩。
3. 计算它的**加权错误率** ε（分错样本的权重和）。
4. 给这个弱学习器分配话语权 α，并**上调分错样本的权重、下调分对样本的权重**。
5. 用新权重重新训练下一个弱学习器……如此往复 B 轮。
6. 最终预测是所有弱学习器的**加权投票**，权重即各自的 α。

权重更新的数学（SAMME 算法，标签取 ±1）：

\[ \varepsilon_j = \sum_{i} w_i \, \mathbb{1}\{y_i \neq \hat{y}_j^{(i)}\} \qquad \alpha_j = \frac{1}{2}\ln\frac{1-\varepsilon_j}{\varepsilon_j} \]

\[ w_i \leftarrow w_i \cdot e^{-\alpha_j\, y_i\, \hat{y}_j^{(i)}} \qquad (\text{随后归一化}) \]

直觉：乘积 \( y_i \hat{y}_j^{(i)} \) 在预测正确时为 +1、错误时为 −1。于是正确的样本权重乘 \( e^{-\alpha} \)（变小），错误的样本权重乘 \( e^{+\alpha} \)（变大）。ε 越接近 0（该学习器越准），α 越大，权重重新分配越猛烈。要求 ε < 0.5 保证了 α > 0——弱学习器至少要比随机好，「比随机差」的学习器在数学上等价于把它的预测取反来用，没有意义。

关于 scikit-learn 的 `algorithm` 参数（本讲学习目标之一）：sklearn 1.0 的 `AdaBoostClassifier` 默认 `algorithm='SAMME.R'`——R 指 real-valued，用 `predict_proba` 的概率估计代替硬标签来计算 α 与权重，通常收敛更快；`algorithm='SAMME'` 则是上面公式的离散版本。ch07.py **没有显式传这个参数**，走的是默认 SAMME.R。多分类时 SAMME 的 α 还要加上修正项 \( \ln(K-1) \)。

#### 4.2.2 核心流程

```text
初始化: w_i = 1/n, 集成 F = 空
for j in 1..B:
    1. 用权重 w 训练弱学习器 h_j（树桩）
    2. ε_j = 加权错误率
    3. α_j = 0.5 * ln((1-ε_j)/ε_j)        # 话语权
    4. 分对的样本: w *= exp(-α_j)          # 缩小
       分错的样本: w *= exp(+α_j)          # 放大
    5. 归一化 w 使 Σw = 1
预测: sign( Σ_j α_j * h_j(x) )            # α 加权投票
```

#### 4.2.3 源码精读

**（1）10 个样本的手工权重计算**

这是 ch07.py 里最珍贵的教学片段——用 10 个样本把 SAMME 一轮更新完整算一遍。[ch07/ch07.py:L712-L725](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L712-L725)：

```python
y = np.array([1, 1, 1, -1, -1, -1,  1,  1,  1, -1])
yhat = np.array([1, 1, 1, -1, -1, -1, -1, -1, -1, -1])
correct = (y == yhat)
weights = np.full(10, 0.1)

epsilon = np.mean(~correct)
print(epsilon)

alpha_j = 0.5 * np.log((1-epsilon) / epsilon)
print(alpha_j)
```

- 10 个样本、初始权重各 0.1（注意此处权重未归一化到 1，`np.mean(~correct)` 直接把布尔取反求均值，因为权重均匀，加权错误率 = 简单错误率）。
- 第 7、8、9 个位置（y=1 却被预测为 −1）分错，所以 **ε = 3/10 = 0.3**。
- **α = 0.5·ln(0.7/0.3) ≈ 0.4236**——这个弱学习器略强于随机，话语权为正但不大。

[ch07/ch07.py:L730-L743](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L730-L743) 计算三种情况下的权重更新量：

```python
update_if_correct = 0.1 * np.exp(-alpha_j * 1 * 1)      # ≈ 0.1 * e^-0.4236 ≈ 0.0655
update_if_wrong_1 = 0.1 * np.exp(-alpha_j * 1 * -1)     # ≈ 0.1 * e^+0.4236 ≈ 0.1528
update_if_wrong_2 = 0.1 * np.exp(-alpha_j * -1 * 1)     # 与上一行相同
```

`1 * 1` 与 `1 * -1` 就是公式里的 \( y_i \hat{y}_j^{(i)} \)：正确为 +1，错误为 −1；`update_if_wrong_1/2` 写了两遍是为了强调「真实为 +1 预测为 −1」和「真实为 −1 预测为 +1」在公式里是同一回事。

[ch07/ch07.py:L748-L755](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L748-L755) 执行更新并归一化：

```python
weights = np.where(correct == 1, update_if_correct, update_if_wrong_1)
normalized_weights = weights / np.sum(weights)
```

数值演算（可自己手算验证）：

| 样本类别 | 更新前 | 归一化后 |
| --- | --- | --- |
| 分对的 7 个 | 0.0655 | 1/14 ≈ 0.0714 |
| 分错的 3 个 | 0.1528 | 1/6 ≈ 0.1667 |

一个非常漂亮的结论：归一化后，分对组总权重 = 7×(1/14) = **0.5**，分错组总权重 = 3×(1/6) = **0.5**。也就是说，这一轮更新把「3 个错误样本的集体受关注度」抬高到了与「7 个正确样本」平起平坐的水平——下一个树桩如果想降低加权错误率，就必须优先照顾这几个钉子户。这就是「自适应」三个字的含义。

**（2）构造 AdaBoostClassifier**

[ch07/ch07.py:L764-L771](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L764-L771)：

```python
tree = DecisionTreeClassifier(criterion='entropy',
                              max_depth=1,
                              random_state=1)

ada = AdaBoostClassifier(base_estimator=tree,
                         n_estimators=500,
                         learning_rate=0.1,
                         random_state=1)
```

- 基学习器从 bagging 的「不限深度的深树」换成 **`max_depth=1` 的树桩**——单刀直入的一次性切分，单独用时比随机好不了多少，是标准的弱学习器。
- `n_estimators=500`：500 轮提升。
- `learning_rate=0.1`：对每轮 α 做收缩（shrinkage），让提升过程更平缓、更稳，代价是需要更多轮数。
- 未显式传 `algorithm`，sklearn 1.0 下默认 `'SAMME.R'`（用概率的实数版提升）。

**（3）训练与准确率对比**

[ch07/ch07.py:L776-L792](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L776-L792) 先评估单独的树桩，再评估 AdaBoost，notebook 保存的输出：

```text
Decision tree train/test accuracies 0.916/0.875
AdaBoost train/test accuracies 1.000/0.917
```

解读：单个树桩欠拟合（训练仅 0.916），AdaBoost 把 500 个树桩串起来后，训练准确率提升到 1.000、测试 0.917。与 4.1 对照：bagging 是「把强模型的方差平均掉」，AdaBoost 是「把弱模型的偏差一点点啃掉」——两条路线殊途同归，测试都到了 0.917。

**（4）决策区域可视化**

[ch07/ch07.py:L797-L833](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L797-L833) 与 bagging 那节相同的绘图骨架，对比对象换成 `[tree, ada]`。预期现象：左图树桩只有一条笔直的决策边界（一次切分）；右图 AdaBoost 的边界是**许多条线段的拼合**——每个树桩贡献一次切分，α 加权投票拼出分段线性的复杂边界，这正是「弱学习器 + 加法组合」的几何形态。

#### 4.2.4 代码实践

**实践目标**：绘制 AdaBoost 训练/测试准确率**随迭代轮数变化**的曲线，亲眼看到「提升」发生的过程，并体会 `learning_rate` 的收缩作用。

**操作步骤**（示例代码，基于上面已拟合的 `ada`；`staged_score` 是 `AdaBoostClassifier` 的真实 API，逐轮返回「只使用前 t 个弱学习器」时的准确率）：

1. 确认基学习器就是 `DecisionTreeClassifier(max_depth=1)`（本讲实践任务要求的树桩配置，仓库代码 L764-L766 已如此）。
2. 用 `staged_score` 取出每轮的准确率并画图：

   ```python
   # 示例代码
   ada.fit(X_train, y_train)
   train_acc = list(ada.staged_score(X_train, y_train))
   test_acc  = list(ada.staged_score(X_test, y_test))

   plt.plot(range(1, 501), train_acc, label='train')
   plt.plot(range(1, 501), test_acc, label='test')
   plt.xlabel('n_estimators'); plt.ylabel('Accuracy')
   plt.legend(); plt.show()
   ```

3. 把 `learning_rate` 从 0.1 改为 1.0（sklearn 默认值），重复第 2 步，把两条测试曲线画到同一张图上对比。

**需要观察的现象**：

- `learning_rate=0.1`：训练准确率从树桩水平（≈0.9 附近）**缓慢爬升**，在几十到几百轮之间到达 1.000；测试准确率较早稳定在平台（约 0.917）后基本不动。
- `learning_rate=1.0`：训练准确率爬升显著更快（每轮 α 不收缩），但测试曲线可能更早到达平台后出现轻微波动或回落。

**预期结果**：两种设置最终训练准确率都到 1.000；小学习率曲线平滑、大学习率曲线陡峭。测试集上限都在 0.917 附近（24 个样本中 2 个错分是这组特征下较难消除的噪声点）。具体在第几轮到达 1.000 **待本地验证**（取决于随机种子与数据）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 AdaBoost 要求每一轮的加权错误率 ε < 0.5？如果某个弱学习器 ε = 0.6 会怎样？

**答案**：ε < 0.5（比随机猜测好）保证 α = 0.5·ln((1−ε)/ε) > 0，学习器获得正的投票权。若 ε = 0.6，则 α < 0，公式上等价于「把这个学习器的预测取反、按 |α| 加权使用」；scikit-learn 在实践中遇到不比随机好的候选弱学习器时会终止训练（避免无意义的轮次）。这也是「弱学习器假设」在算法里的落点。

**练习 2**：bagging 和 boosting 哪个可以并行训练成员？为什么？

**答案**：bagging 可以——每棵树的 bootstrap 样本互不依赖，500 棵树可同时开训（`BaggingClassifier` 的 `n_jobs` 参数就是干这个的，仓库里保守地设了 `n_jobs=1`）。boosting 不行——第 j 轮的样本权重由第 j−1 轮的预测结果决定，串行链条无法打断。

**练习 3**：把仓库的 `learning_rate=0.1` 改成 `1.0`，同时把 `n_estimators=500` 改成 `50`，哪个设置更可能出现测试准确率下降？

**答案**：`learning_rate=1.0 + n_estimators=50` 风险更大。大学习率让每轮修正过猛、单个错误学习器的破坏力被放大；而轮数少又拿不到「多轮平均」的平滑收益。反之 `0.1 + 500` 是典型的「小步慢走 + 长时间积累」组合。二者体现了同一权衡：`learning_rate` 与 `n_estimators` 应当成对调节。

### 4.3 梯度提升与 XGBoost

#### 4.3.1 概念说明

AdaBoost 用「重加权样本」表达「下一轮该学什么」。梯度提升（gradient boosting）给出了更一般化的回答：**下一棵树拟合当前损失函数的负梯度**。

把集成看成加法模型 \( F_M(x) = \sum_{m=1}^{M} \eta\, h_m(x) \)，每轮对每个样本计算**伪残差**（对二分类，损失作用在 log-odds 上，伪残差可理解为「当前预测还差多少」）：

\[ r_{im} = -\left[\frac{\partial L(y_i, F(x_i))}{\partial F(x_i)}\right]_{F=F_{m-1}} \]

然后让一棵回归树去拟合 \( r_{im} \)，再把这棵树乘以学习率 η 加进集成：

\[ F_m(x) = F_{m-1}(x) + \eta\, h_m(x) \]

当损失取平方误差时，负梯度恰好是残差 \( y_i - F(x_i) \)——「拟合残差」是「拟合负梯度」的特例。AdaBoost 的重加权是这个框架的又一特例（指数损失）。这也解释了为什么叫「梯度」提升：**在函数空间里做梯度下降，每棵树是梯度的一步**。

scikit-learn 的 `GradientBoostingClassifier` 与第三方的 **XGBoost**（eXtreme Gradient Boosting）都实现这个思想，差异在工程与正则化：

| 维度 | sklearn `GradientBoostingClassifier` | `xgboost.XGBClassifier` |
| --- | --- | --- |
| 来源 | scikit-learn 自带 | 独立库（陈天奇等），pip 安装 |
| 目标函数 | 梯度提升（一阶梯度） | 显式正则化目标：损失 + 叶子数惩罚 + 叶权重 L1/L2 |
| 优化 | 一阶导数 | 一阶 + **二阶导数**（牛顿法风格） |
| 常用特性 | learning_rate/subsample | 列采样、行采样、早停、原生分布式/GPU |
| 接口 | sklearn 估计器 | **兼容 sklearn 接口**（fit/predict/predict_proba） |
| 标签要求 | 任意类别标签 | 必须是从 0 开始的整数 |

本仓库的第 7 章只用 XGBoost 做演示（sklearn 版 `GradientBoostingClassifier` 在 ch07.py 中**没有出现**；它在第 9 章回归和第 10 章还会以不同身份露面，届时再展开）。

#### 4.3.2 核心流程

梯度提升一轮的伪代码（对应 notebook 中「Outlining the general gradient boosting algorithm」小节）：

```text
初始化: F_0(x) = 使损失最小的常数（如 log-odds 的初始值）
for m in 1..M:
    1. 计算伪残差 r_im = -∂L/∂F(x_i) | F=F_{m-1}
    2. 用一棵（回归）树 h_m 拟合 {(x_i, r_im)}
    3. F_m(x) = F_{m-1}(x) + learning_rate * h_m(x)
预测: 分类时把累加结果从 log-odds 映回概率（sigmoid）
```

#### 4.3.3 源码精读

**（1）版本确认**

[ch07/ch07.py:L874](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L874) 打印 `xgb.__version__`——作者用它确认运行环境是 xgboost 1.5.0（与 L52-L60 版本检查表一致）。

**（2）XGBClassifier 训练**

[ch07/ch07.py:L879-L890](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L879-L890)：

```python
model = xgb.XGBClassifier(n_estimators=1000, learning_rate=0.01,
                          max_depth=4, random_state=1,
                          use_label_encoder=False)

gbm = model.fit(X_train, y_train)

y_train_pred = gbm.predict(X_train)
y_test_pred = gbm.predict(X_test)

gbm_train = accuracy_score(y_train, y_train_pred)
gbm_test = accuracy_score(y_test, y_test_pred)
print(f'XGboost train/test accuracies '
      f'{gbm_train:.3f}/{gbm_test:.3f}')
```

notebook 保存的输出：

```text
XGboost train/test accuracies 0.968/0.917
```

要点：

- `n_estimators=1000, learning_rate=0.01`：与 AdaBoost 实验相反的搭配——**极小的步长 × 极多的轮数**，是梯度提升的典型用法（慢而稳）。
- `max_depth=4`：梯度提升常用浅树，远深于树桩、远浅于 bagging 的无限深。
- **接口与 sklearn 估计器同形**：`fit(X, y)` / `predict(X)` / 配合 `accuracy_score`——这意味着它可以直接塞进第 6 章学过的 `Pipeline` 和 `GridSearchCV`，替换模型时其余代码一行不改。
- `use_label_encoder=False` 在 xgboost 1.5.0 里用于关闭过时的标签编码提示；该参数在 1.6 起弃用、2.0 起移除，新版环境直接删掉即可。**标签必须已编码为 0 起的整数**，这正是 4.1.3 中 `LabelEncoder` 提前完成的准备工作。
- 结果解读：训练 0.968（没到 1.000，因为小学习率 + 正则化在压制过拟合），测试 0.917——与 bagging、AdaBoost 打平。在这份只有 95 个训练样本的小数据上，三种集成方法都触及了同一测试上限。

#### 4.3.4 代码实践

**实践目标**：在相同数据、相近超参数下，对比 sklearn `GradientBoostingClassifier` 与 `XGBClassifier` 的准确率与训练耗时，体会「接口兼容、性能与行为有差异」。

**操作步骤**（示例代码，仓库未包含此对比，需自行补齐 sklearn 的导入）：

```python
# 示例代码：GradientBoostingClassifier vs XGBClassifier
from sklearn.ensemble import GradientBoostingClassifier
import time

results = {}
for name, clf in [
    ('sklearn GB', GradientBoostingClassifier(n_estimators=1000,
                                              learning_rate=0.01,
                                              max_depth=4,
                                              random_state=1)),
    ('XGBoost',    xgb.XGBClassifier(n_estimators=1000, learning_rate=0.01,
                                     max_depth=4, random_state=1,
                                     use_label_encoder=False)),
]:
    t0 = time.time()
    clf.fit(X_train, y_train)
    elapsed = time.time() - t0
    results[name] = (accuracy_score(y_train, clf.predict(X_train)),
                     accuracy_score(y_test, clf.predict(X_test)),
                     elapsed)
    print(f'{name}: train/test acc = '
          f'{results[name][0]:.3f}/{results[name][1]:.3f}, '
          f'fit time = {elapsed:.3f}s')
```

**需要观察的现象**：两者的训练/测试准确率应非常接近（树模型在小数据上的表现主要由超参数决定）；训练时间上 XGBoost 通常不慢于 sklearn 实现。

**预期结果**：测试准确率都在 0.917 附近。耗时对比**待本地验证**——小数据（95 个样本）上两者都在秒级以内，差异可能不明显；数据放大后差距才会显现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `XGBClassifier` 要求标签是从 0 开始的整数，而 `GradientBoostingClassifier` 不需要？

**答案**：XGBoost 内部把类别标签直接当作**类别索引**使用（第 k 类对应第 k 个输出/叶子统计），字符串或 2/3 这样的标签会破坏索引映射；sklearn 的实现则在内部维护自己的 `classes_` 数组并与任意标签做映射。这也是 ch07.py 在划分前先做 `LabelEncoder().fit_transform(y)`（L597-L598）的原因之一。

**练习 2**：`learning_rate=0.01` 配 `n_estimators=1000`，如果只把 `n_estimators` 砍到 100 会发生什么？

**答案**：每步只走 1% 的修正、总共只走 100 步，集成还远未收敛，模型会明显欠拟合（训练准确率显著低于 0.968）。梯度提升里这两个参数是绑定的：`learning_rate` 越小，需要的 `n_estimators` 越多。也可以反过来用早停（XGBoost 支持传入验证集 + `early_stopping_rounds`，1.5.0 时代是构造器参数、2.0 起改为 `fit` 参数，以本地版本文档为准）自动找轮数。

**练习 3**：AdaBoost 和梯度提升都「串行地修正前面的错误」，它们对「下一棵树学什么」的指引有何本质不同？

**答案**：AdaBoost 通过**改变样本权重**间接引导——错误样本在下一轮拟合中占比更大；梯度提升直接给出**回归目标**——下一棵树去拟合当前损失的负梯度（残差）。前者只适用于能按样本加权的损失（指数损失），后者对任何可导损失都成立，因而可以无缝换成 huber、对数损失等，泛化能力更强。

## 5. 综合实践

**任务：Wine 二分类「集全家福」横评。** 把本讲五个模型放进同一个循环，复用同一份数据管线，统一输出指标并排可视化：

1. 数据准备照抄 [ch07/ch07.py:L570-L603](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L570-L603)（Wine 二类子集、两个特征、分层 80/20）。
2. 依次构建：深单树（L609-L611）、`BaggingClassifier`（L613-L620，并加上 `oob_score=True`）、树桩（L764-L766）、`AdaBoostClassifier`（L768-L771）、`XGBClassifier`（L879）。
3. 对每个模型记录：训练准确率、测试准确率、（bagging 的）OOB 分数。
4. 画 1×5 的决策区域子图行，标题写上各模型的测试准确率。
5. 对 AdaBoost 追加 4.2.4 的 staged 曲线；对 bagging 追加 4.1.3（5）的 OOB 检查。
6. 写三行结论，回答：哪些模型训练准确率是 1.000？哪些测试准确率最高？bagging 与 boosting 各自改善了什么（方差还是偏差）？

**预期结果**（对照 notebook 保存的输出，允许小幅波动）：深树 1.000/0.833，bagging 1.000/0.917，树桩 0.916/0.875，AdaBoost 1.000/0.917，XGBoost 0.968/0.917；深树决策边界最碎，bagging 最平滑，AdaBoost 呈分段线性，OOB 分数落在训练与测试准确率之间。**待本地验证**。

## 6. 本讲小结

- **bagging = bootstrap 采样 + 聚合**：同一配方、不同数据副本、并行训练、投票聚合；主要**降低方差**，对「深树」这类高方差基学习器效果最明显（Wine 上测试准确率 0.833 → 0.917）。
- `BaggingClassifier` 的关键参数是 `base_estimator` / `n_estimators` / `max_samples` / `max_features` / `bootstrap` / `bootstrap_features`；约 36.8% 的样本每轮抽不到，构成免费的 **OOB 验证集**（`oob_score=True` 后读 `oob_score_`）。
- **AdaBoost = 串行重加权**：ε → α = ½ln((1−ε)/ε) → 正确样本权重乘 e^(−α)、错误样本乘 e^(+α) → 归一化；ch07.py 的 10 样本手工算例显示一轮更新后「错样本组总权重 = 对样本组总权重 = 0.5」。sklearn 的 `algorithm` 参数区分离散 SAMME 与默认的实数版 SAMME.R。
- **梯度提升 = 拟合负梯度**：把「下一棵树学什么」统一为回归问题（残差是平方损失下的特例），`learning_rate` 是每步的收缩系数，与小步长搭配的是大轮数。
- **XGBClassifier 兼容 sklearn 接口**（fit/predict 可直接进 Pipeline/GridSearchCV），但要求标签从 0 开始编码，且自带正则化目标与二阶导数优化。
- 三条路线（平均方差、加权弱学习器、梯度修正）在这份小数据上殊途同归于 0.917，说明**集成方式不是魔法，基学习器的偏差-方差结构决定了它能改善什么**。

## 7. 下一步学习建议

- **顺序学习路线**：下一单元进入第 8 章（u8-l1 词袋模型与 TF-IDF），届时情感分类器会再次用到逻辑回归与网格搜索，集成本领可以随手带上；第 9 章（u9）回归部分还会见到 `XGBRegressor` 与 sklearn 梯度提升的回归形态，可与本讲 4.3 对照。
- **回望巩固**：翻回 u4-l2 的随机森林（`RandomForestClassifier` 的 `feature_importances_`）——它就是「bagging + 特征子采样」的组合，现在你能解释它每个参数的来历；再看 u7-l1 的 `MajorityVoteClassifier`，体会「投票/重采样/重加权」是构造集成的三条正交轴线。
- **延伸阅读**：scikit-learn 文档中 `sklearn.ensemble` 页面对 OOB、随机森林、梯度提升有权威说明；XGBoost 文档的「Tuning Parameters」一节系统梳理了 `eta`/`max_depth`/`min_child_weight` 等参数的调参顺序；书第 7 章末尾对梯度提升分类的 log-odds 推导（notebook 目录中的三个小节）值得对照公式精读一遍。
