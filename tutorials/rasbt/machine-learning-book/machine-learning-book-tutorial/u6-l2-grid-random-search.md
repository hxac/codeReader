# 网格/随机/减半搜索与嵌套交叉验证

> 对应《Machine Learning with PyTorch and Scikit-Learn》第 6 章 "Fine-tuning machine learning models via grid search" 一节。
> 仓库代码：`ch06/ch06.py`（等价 notebook：`ch06/ch06.ipynb`）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 看懂并会写 `GridSearchCV` 的参数网格，特别是「双列表 `param_grid`」这种为不同核函数定义不同搜索子空间的写法。
2. 正确解读 `best_score_`、`best_params_`、`best_estimator_`，并理解 `refit=True` 帮你省掉了什么。
3. 会用 `RandomizedSearchCV` 的 `n_iter` 控制采样预算，并理解 `scipy.stats.loguniform`（对数均匀分布）为什么比线性网格更适合跨数量级的超参数。
4. 理解 `HalvingRandomSearchCV` 的「资源递增 + 逐轮淘汰」（successive halving）思想。
5. 掌握嵌套交叉验证的嵌套写法——把整个 `GridSearchCV` 对象塞进外层 `cross_val_score(gs, ...)`——并能用它无偏地比较 SVM 与决策树两套「算法 + 调参流程」。

## 2. 前置知识

本讲建立在 u6-l1（Pipeline、交叉验证与学习/验证曲线）之上。开始前请确认以下概念：

- **参数 vs 超参数**：参数（parameter）是模型在 `fit` 中从数据学出来的量（如 SVC 的支持向量系数）；超参数（hyperparameter）是训练**之前**由人设定的量（如 `SVC` 的 `C`、`gamma`、`kernel`、决策树的 `max_depth`）。超参数不能靠梯度下降学习，只能靠「试 + 评估」搜索。
- **验证曲线的局限**：u6-l1 用 `validation_curve` 扫描了逻辑回归的单个参数 `C`。但现实中往往要同时调多个超参数（SVM 要同时定 kernel、C、gamma），组合数是**乘法**增长的，一次只扫一个参数的工具不够用了。
- **Pipeline 与双下划线参数名**：`make_pipeline(StandardScaler(), SVC())` 生成的管线里，SVM 的 `C` 要写成 `'svc__C'`（小写类名 + 双下划线）。GridSearchCV 的参数网格就用这种名字寻址。这是 u6-l1 已建立的约定，本讲直接沿用。
- **训练集 / 测试集的分工**：乳腺癌数据集 569 个样本，80/20 分层划分后 `X_train` 有 455 个样本、`X_test` 有 114 个（[ch06/ch06.py:L139-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L139-L142)）。本讲所有搜索与交叉验证都**只在 `X_train` 上进行**，`X_test` 只在最后评估一次。
- **对数刻度常识**：SVM 的 `C` 与 `gamma` 合理取值往往横跨多个数量级（10⁻⁴ ~ 10³），所以搜索网格几乎总按数量级（×10）设置，画图用对数轴。

一个贯穿本讲的直觉：**调参 = 在「超参数空间」里做搜索**，搜索策略有三个演进方向——

| 搜索方式 | 核心问题 | 一句话策略 |
|---|---|---|
| 网格搜索 GridSearchCV | 穷举太贵 | 每个候选都给全额预算（全量数据 + k 折 CV） |
| 随机搜索 RandomizedSearchCV | 网格组合爆炸 | 固定预算 `n_iter`，随机撒点 |
| 减半搜索 HalvingRandomSearchCV | 好坏候选一视同仁 | 先给所有候选人一点点资源，逐轮淘汰，好候选拿越来越多资源 |

## 3. 本讲源码地图

| 位置 | 内容 |
|---|---|
| [ch06/ch06.py:L19-L24](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L19-L24) | 本讲的 import：`GridSearchCV`、`SVC`、`RandomizedSearchCV`、实验性 halving 开关、`DecisionTreeClassifier` |
| [ch06/ch06.py:L334-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L352) | 网格搜索：`pipe_svc`、双列表 `param_grid`、`gs.fit` 与 `best_score_`/`best_params_` |
| [ch06/ch06.py:L357-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L357-L363) | `best_estimator_` 与 `refit=True` 的注释说明、测试集评估 |
| [ch06/ch06.py:L370-L399](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L370-L399) | 随机搜索：`RandomizedSearchCV(n_iter=20, ...)` 与其 `best_score_`/`best_params_` |
| [ch06/ch06.py:L410-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L410-L416) | `scipy.stats.loguniform` 连续分布采样示例（注意：脚本里没 import scipy，见 4.2.3 的坑） |
| [ch06/ch06.py:L427-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L447) | 减半搜索：`HalvingRandomSearchCV(n_candidates='exhaust', resource='n_samples', factor=1.5)` 及评估 |
| [ch06/ch06.py:L459-L481](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L481) | 嵌套交叉验证：SVC 管线（2×5）与决策树（2×5）两段 |
| [ch06/ch06.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.ipynb) | notebook 版，保留了每段代码的运行输出（`best_score_` 数值等），本讲引用的输出均出自这里 |

数据加载与划分（`df = pd.read_csv(...)` 到 `train_test_split`，[ch06/ch06.py:L102-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L142)）在 u6-l1 已逐行讲过，本讲直接复用其产物 `X_train / X_test / y_train / y_test`，不再重复。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：参数网格双列表（含 `best_score_`/`best_params_`/`best_estimator_`）、随机搜索与 loguniform、减半搜索的资源递增、嵌套交叉验证。

### 4.1 网格搜索与「双列表」参数网格

#### 4.1.1 概念说明

网格搜索（grid search）是最朴素的超参数优化：把每个超参数的候选值列成表，**穷举所有组合**，每个组合用 k 折交叉验证打分，选平均分最高的组合。

它要解决的第一个现实问题是：**不同算法分支需要的超参数不一样**。对 SVM 来说：

- 线性核 `kernel='linear'`：只有 `C` 一个超参数；
- RBF 核 `kernel='rbf'`：有 `C` 和 `gamma` 两个超参数（回顾 u3-l2：`C` 控制软间隔惩罚、`gamma` 控制单个样本的影响半径）。

如果把 `kernel`、`C`、`gamma` 写在同一个网格字典里，GridSearchCV 会把三个列表做笛卡尔积，生成「线性核 + gamma」这种**无意义组合**（线性核根本不用 gamma），白白浪费 8 次完整评估。scikit-learn 的解法是允许 `param_grid` 传一个**字典列表**：每个字典是一个独立的搜索子空间，总候选数是各子空间之和（而非乘积）——这就是「双列表 param_grid」的由来。

#### 4.1.2 核心流程

```text
构造 pipe_svc = StandardScaler + SVC(random_state=1)
    ↓
定义 param_range（8 个数量级的 C 值）与 param_grid（两个子空间字典）
    ↓
GridSearchCV(pipe_svc, param_grid, scoring='accuracy', cv=10, refit=True)
    ↓
gs.fit(X_train, y_train)
    ├── 子空间1（linear）: 8 个 C              → 8 个候选
    ├── 子空间2（rbf）   : 8 个 C × 8 个 gamma → 64 个候选
    ├── 共 72 个候选，每个做 10 折 CV          → 72 × 10 = 720 次完整 fit
    ├── 记录每个候选的 10 折平均 accuracy
    ├── best_params_ ← 平均分最高的候选
    ├── best_score_  ← 该候选的 10 折平均分
    └── refit=True   ← 用 best_params_ 在全部 455 个训练样本上重训一次
    ↓
gs.best_estimator_ 直接可用于 X_test 评估
```

候选规模可以用一个简单式子概括：

\[ N_{\text{候选}} = |C_{\text{linear}}| + |C_{\text{rbf}}| \times |\gamma| = 8 + 8 \times 8 = 72 \]

\[ N_{\text{fit}} = N_{\text{候选}} \times k = 72 \times 10 = 720 \]

每次「fit」包含一次 StandardScaler 拟合 + 一次 SVC 训练（Pipeline 作为整体被拟合）。

#### 4.1.3 源码精读

先看本讲用到的搜索类 import（第 19–24 行，减半搜索的实验性开关也在其中）：

[ch06/ch06.py:L19-L24](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L19-L24)

```python
from sklearn.model_selection import GridSearchCV
from sklearn.svm import SVC
from sklearn.model_selection import RandomizedSearchCV
from sklearn.experimental import enable_halving_search_cv  # noqa
from sklearn.model_selection import HalvingRandomSearchCV
from sklearn.tree import DecisionTreeClassifier
```

这段集中导入本讲全部「搜索工具」：网格、随机、减半三种搜索器加两个被调模型（SVC、决策树）。注意 `enable_halving_search_cv` 本身不使用，import 它只是为了让处于实验阶段的 halving API 可用（详见 4.3.3）。

接着构造被调对象与双列表网格（第 334–343 行）：

[ch06/ch06.py:L334-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L343)

```python
pipe_svc = make_pipeline(StandardScaler(),
                         SVC(random_state=1))

param_range = [0.0001, 0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0]

param_grid = [{'svc__C': param_range,
               'svc__kernel': ['linear']},
              {'svc__C': param_range,
               'svc__gamma': param_range,
               'svc__kernel': ['rbf']}]
```

这段做了三件事：① 建立「标准化 + SVM」管线（SVM 基于距离，StandardScaler 是硬前提，回顾 u3-l2）；② `param_range` 按数量级给出 8 个 `C` 候选；③ `param_grid` 是**含两个字典的列表**——第一个字典只搜线性核的 `C`（8 个候选），第二个字典搜 RBF 核的 `C × gamma`（64 个候选）。参数名用 `'svc__C'` 这种双下划线形式寻址管线内的估计器。

然后是搜索本身（第 345–352 行）：

[ch06/ch06.py:L345-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L352)

```python
gs = GridSearchCV(estimator=pipe_svc,
                  param_grid=param_grid,
                  scoring='accuracy',
                  refit=True,
                  cv=10)
gs = gs.fit(X_train, y_train)
print(gs.best_score_)
print(gs.best_params_)
```

`scoring='accuracy'` 指定打分指标；`cv=10` 即每个候选 10 折分层交叉验证（对分类器，`cross_val_score`/GridSearchCV 默认使用分层 K 折，u6-l1 已验证）；`refit=True`（默认值，书里显式写出以示强调）让搜索结束后自动用最优参数在全量训练集上重训。notebook 中的运行输出：

```text
0.9846859903381642
{'svc__C': 100.0, 'svc__gamma': 0.001, 'svc__kernel': 'rbf'}
```

即 RBF 核、`C=100`、`gamma=0.001` 胜出，10 折平均准确率约 0.985。

最后取回最优模型并在测试集上评估（第 357–363 行）：

[ch06/ch06.py:L357-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L357-L363)

```python
clf = gs.best_estimator_

# clf.fit(X_train, y_train)
# note that we do not need to refit the classifier
# because this is done automatically via refit=True.

print(f'Test accuracy: {clf.score(X_test, y_test):.3f}')
```

注释点明了本段唯一的知识点：因为 `refit=True`，`best_estimator_` 已经是在全部训练数据上训练好的模型，**不需要再 `fit` 一次**。测试集输出 `Test accuracy: 0.974`——注意它低于 `best_score_` 的 0.985，原因见下面练习 2。

#### 4.1.4 代码实践：用 cv_results_ 检查 72 个候选的完整得分表

`ch06.py` 只打印了最优结果；实际上 GridSearchCV 把每个候选的详细得分都存在 `cv_results_` 字典里（本讲规格的学习目标之一）。下面的示例代码展示如何把它变成可排序的表格（**示例代码**，仓库中没有这段）：

1. **实践目标**：确认网格确实评估了 72 个候选、验证 `best_params_` 就是得分最高的一行，并观察 linear 与 rbf 两个子空间的成绩差异。
2. **操作步骤**：先执行 [ch06/ch06.py:L102-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L142)（数据加载划分）与 [ch06/ch06.py:L334-L350](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L350)（gs 搜索），然后运行：

   ```python
   # 示例代码：检查 cv_results_
   import pandas as pd

   results = pd.DataFrame(gs.cv_results_)   # 72 行 × 若干列
   print(results.shape)                      # 预期 (72, ...)
   cols = ['rank_test_score', 'mean_test_score', 'std_test_score', 'params',
           'mean_fit_time']
   print(results[cols].sort_values('rank_test_score').head(10))
   ```

3. **需要观察的现象**：
   - `results.shape[0]` 是否等于 72（8 个 linear + 64 个 rbf）；
   - `rank_test_score` 为 1 的那一行，其 `params` 是否与 `gs.best_params_` 一致、`mean_test_score` 是否等于 `gs.best_score_`；
   - 前 10 名里 `params` 的 `kernel` 取值分布（是否几乎全是 `rbf`）。
4. **预期结果**：72 行；第 1 名是 `{'svc__C': 100.0, 'svc__gamma': 0.001, 'svc__kernel': 'rbf'}`，`mean_test_score ≈ 0.9847`，与 `gs.best_score_` 一致。前 10 名中 linear 核的排名与得分分布情况**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `param_grid` 写成单个字典 `{'svc__C': param_range, 'svc__gamma': param_range, 'svc__kernel': ['linear', 'rbf']}`，候选数变成多少？有什么问题？

**答案**：变成 \(8 \times 8 \times 2 = 128\) 个候选。其中 64 个「linear + gamma」组合是冗余的（线性核不使用 gamma，拟合结果与不带 gamma 的同名配置相同），等于把 64 次完整的 10 折评估浪费在重复配置上；而且 `kernel` 还不能不写 gamma 之外的差异。双列表写法用 72 个候选覆盖了同样的有效空间，省下约 44% 的计算量。

**练习 2**：为什么 `gs.best_score_`（0.985）比测试集准确率（0.974）高？这说明 `best_score_` 有什么性质？

**答案**：`best_score_` 是「72 个候选各自的 10 折平均分」中的**最大值**——我们先看了验证分数再挑最大，这个「取 max」的操作引入了选择性乐观偏差；且 455 个训练样本的 CV 分数本身也有波动。测试集是搜索过程从未参与过的数据，评估是无偏的，通常略低。结论：`best_score_` 适合用来**在候选之间做选择**，不适合当作模型最终性能的报告值；要更可靠的性能估计，需要本讲 4.4 的嵌套交叉验证。

**练习 3**：把 `cv=10` 改成 `cv=5`，总的 fit 次数是多少？

**答案**：\(72 \times 5 = 360\) 次。fit 次数与候选数和折数都是线性关系，这正是后面随机搜索、减半搜索要削减的对象。

### 4.2 随机搜索与 loguniform 参数分布

#### 4.2.1 概念说明

网格搜索的两个痛点：① 超参数一多，组合数按乘法爆炸（本例 72 个还算温和）；② 网格点分布「死板」——8 个 `C` × 8 个 `gamma` 的网格里，`C` 实际只有 8 种取值，如果恰好的最优点在 `C=30` 附近，网格就永远错过它。

随机搜索（randomized search）换一种思路：不做穷举，而是从参数空间**随机采样固定数量**（`n_iter`）的配置。直觉来自 Bergstra 与 Bengstra（2012）的观察：高维超参数空间里往往只有少数几个维度真正重要，随机撒点在重要维度上探索到的**不同取值**远多于同预算的网格（网格把预算浪费在不重要维度的所有组合上）。此外，随机搜索允许给参数指定**连续分布**（而不只是离散列表）——比如对数均匀分布 `loguniform`，让搜索可以命中 `C=30.7` 这种网格外的值。

#### 4.2.2 核心流程

```text
RandomizedSearchCV(estimator, param_distributions, n_iter=20, cv=10, random_state=1)
    ↓
rs.fit(X_train, y_train)
    ├── 采样第 i 个配置（i = 1..20）：
    │      先从 param_grid 的两个字典中等概率挑一个子空间
    │      再在该字典内，对每个参数从其列表（均匀）或 scipy 分布（rvs）抽一个值
    ├── 每个配置做 10 折 CV → 共 20 × 10 = 200 次 fit（约为网格 720 次的 28%）
    ├── best_score_ / best_params_ / best_estimator_ 与 GridSearchCV 同一套语义
    └── refit=True 同样自动全量重训
```

对数均匀分布的含义：若 \(X \sim \text{loguniform}(a, b)\)，则 \(\log X\) 在 \([\log a, \log b]\) 上均匀分布，其密度为

\[
f(x) = \frac{1}{x \, (\ln b - \ln a)}, \quad x \in [a, b]
\]

直观效果：**每个数量级区间被抽中的概率相同**。对横跨 10⁻⁴ ~ 10³ 七个数量级的 `C`/`gamma` 来说，这远比线性均匀合理——线性均匀下几乎全部样本会落在 [100, 1000] 这一个区间里。

#### 4.2.3 源码精读

随机搜索的构造与调用（第 370–399 行）。注意书里先重建了 `pipe_svc` 和 `param_grid`，保证与前一段实验互不干扰：

[ch06/ch06.py:L370-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L370-L388)

```python
pipe_svc = make_pipeline(
    StandardScaler(),
    SVC(random_state=1))

param_grid = [{'svc__C': param_range,
               'svc__kernel': ['linear']},
              {'svc__C': param_range,
               'svc__gamma': param_range,
               'svc__kernel': ['rbf']}]


rs = RandomizedSearchCV(estimator=pipe_svc,
                        param_distributions=param_grid,
                        scoring='accuracy',
                        refit=True,
                        n_iter=20,
                        cv=10,
                        random_state=1,
                        n_jobs=-1)
```

与 `GridSearchCV` 的关键差异有三处：参数名换成 `param_distributions`；新增 `n_iter=20` 把预算钉死在 20 个配置；新增 `random_state=1` 固定采样种子保证可复现（网格搜索是穷举，没有随机性，也就不需要种子）。`n_jobs=-1` 表示用满全部 CPU 核并行各折。

[ch06/ch06.py:L393-L399](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L393-L399)

```python
rs = rs.fit(X_train, y_train)
print(rs.best_score_)
print(rs.best_params_)
```

notebook 输出：

```text
0.9737681159420291
{'svc__kernel': 'rbf', 'svc__gamma': 0.001, 'svc__C': 10.0}
```

只试了 20/72 个组合，最优 CV 分数 0.974，仅比网格搜索的 0.985 低约 1 个百分点，而计算量不到其三成。

接着书中演示了连续分布写法（第 410–416 行）：

[ch06/ch06.py:L410-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L410-L416)

```python
param_range = [0.0001, 0.001, 0.01, 0.1,
               1.0, 10.0, 100.0, 1000.0]

param_range = scipy.stats.loguniform(0.0001, 1000.0)

np.random.seed(1)
param_range.rvs(10)
```

第一行还是旧的离散列表；第二行立刻用 `loguniform(0.0001, 1000.0)` **覆盖**了它——这是在展示：`param_distributions` 里任何一个参数的取值既可以给 list，也可以给一个带 `rvs`（随机变数抽样）方法的 scipy 分布对象。`rvs(10)` 抽 10 个样本，notebook 输出：

```text
array([8.30145146e-02, 1.10222804e+01, 1.00184520e-04, 1.30715777e-02,
       1.06485687e-03, 4.42965766e-04, 2.01289666e-03, 2.62376594e-02,
       5.98924832e-02, 5.91176467e-01])
```

可以看到样本散布在 10⁻⁴ ~ 10¹ 各个数量级上，而不是挤在某个区间。

> ⚠️ **一个真实的坑**：`ch06.py` 里并没有 `import scipy`——notebook 中 `import scipy.stats` 是独立单元格（见 [ch06/ch06.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.ipynb) 中 "Exploring hyperparameter configurations more widely with randomized search" 一节），转换成脚本时该行丢失了。如果你从上到下顺序执行 `ch06.py`，会在第 413 行抛 `NameError: name 'scipy' is not defined`。自己复现时请先补一句 `import scipy.stats`。

#### 4.2.4 代码实践：n_iter 从 20 加到 100，最优参数与分数变不变

这是本讲综合实践的第 2 部分，也可以单独做：

1. **实践目标**：体会「采样预算」对随机搜索结果的影响——预算加大后，最优配置与最优分数是否逼近网格搜索的穷举结果。
2. **操作步骤**：

   ```python
   # 示例代码：在 4.2.3 的 rs 基础上继续
   import time
   from sklearn.model_selection import RandomizedSearchCV

   for n_iter in [20, 100]:
       rs = RandomizedSearchCV(estimator=pipe_svc,
                               param_distributions=param_grid,
                               scoring='accuracy',
                               refit=True,
                               n_iter=n_iter,
                               cv=10,
                               random_state=1,
                               n_jobs=-1)
       t0 = time.time()
       rs.fit(X_train, y_train)
       elapsed = time.time() - t0
       print(f'n_iter={n_iter:3d} | best_score={rs.best_score_:.4f} | '
             f'best_params={rs.best_params_} | {elapsed:.1f}s')
   ```

3. **需要观察的现象**：两次运行打印的 `best_score_` 与 `best_params_` 是否相同；`n_iter=100` 的最优分数是否更接近网格搜索的 0.9847；耗时是否大约按 5 倍增长。
4. **预期结果**：`n_iter=20` 复现 notebook 的 `{'svc__kernel': 'rbf', 'svc__gamma': 0.001, 'svc__C': 10.0}`、分数约 0.9738（固定 `random_state=1`，可精确复现）。`n_iter=100` 采样是前 20 个采样点无法推导的**新序列**（同一 `random_state` 下采出的 100 个配置包含但不同于 20 个版本），其最优参数与分数**待本地验证**——合理的预期是分数 ≥ 0.9738 且更接近 0.9847：候选越多，越可能覆盖到网格最优解 `C=100, gamma=0.001` 附近的组合；但若最优区域很窄，也可能仍差零点几个百分点。这正说明随机搜索是「预算换精度」。

#### 4.2.5 小练习与答案

**练习 1**：随机搜索只试了 20/72 ≈ 28% 的组合，为什么最优分数只差约 1 个百分点？

**答案**：72 个候选的得分分布高度不均匀——大量「C 与 gamma 数量级不匹配」的组合表现平平，真正有竞争力的配置集中在少数几个数量级组合附近。随机采样 20 次大概率能命中这个好区域的一两个点；反之网格搜索多付出的 72% 算力大多花在平庸组合上。这也是随机搜索在高维空间里性价比更高的原因。

**练习 2**：对 `C` 用 `scipy.stats.loguniform(0.0001, 1000)` 与用 `uniform(0.0001, 1000)`（线性均匀）采样，结果分布有什么本质区别？

**答案**：线性均匀下，落在 \([100, 1000]\) 一个区间的概率是 \(900/999.9999 \approx 90\%\)，几乎抽不到小值；对数均匀下每个数量级（10⁻⁴~10⁻³、…、10²~10³ 共 7 个区间）概率各约 1/7，与「合理值可能出现在任何数量级」的先验一致。所以跨数量级超参数的连续搜索一律用 loguniform。

**练习 3**：`GridSearchCV` 没有也没有 `random_state` 参数，为什么 `RandomizedSearchCV` 必须有？

**答案**：网格搜索穷举固定列表，评估顺序与结果完全确定；随机搜索的第一步是从分布中**随机采样**配置，不同种子得到不同的候选集合，结果会波动，因此必须提供 `random_state` 才能复现实验（书中设 `random_state=1`）。

### 4.3 减半搜索：HalvingRandomSearchCV 的资源递增思想

#### 4.3.1 概念说明

网格与随机搜索有个共同的浪费：**好候选与坏候选拿到完全相同的预算**。一个 `gamma=1000` 的烂配置也会用全部 455 个样本训练 10 折——但通常用 50 个样本就能看出它没戏。

减半搜索（successive halving，逐次减半）借自多臂老虎机的「比赛制」思想：

- 第 0 轮：让**很多**候选各自在**少量资源**（`resource='n_samples'` 表示资源 = 训练样本数）上训练、评估；
- 每轮结束：按得分淘汰，只保留前 \(1/\text{factor}\) 的候选；
- 下一轮：幸存者的资源乘以 `factor`，继续比；
- 资源耗尽（达到全量训练集）时，最后一轮的胜者即为最优。

这样，多数算力自然集中到少数有希望的候选身上。scikit-learn 同时提供 `HalvingGridSearchCV`（候选来自穷举网格）与 `HalvingRandomSearchCV`（候选来自随机采样）；本书用的是后者，减半机制两者完全一致。

#### 4.3.2 核心流程

设第 0 轮资源为 \(r_0\)、候选数为 \(n_0\)，`factor` 记作 \(f\)，则第 \(k\) 轮：

\[
r_k = r_0 \cdot f^{\,k}, \qquad n_k \approx n_0 / f^{\,k}
\]

资源单调上升、候选单调减少，且 \(n_k \times r_k \approx n_0 \times r_0\)——**每一轮的总计算量大致恒定**（这是 successive halving 的核心性质）。比赛在 \(r_k\) 达到全量训练样本数（455）时结束。

以书中设置 `factor=1.5` 为例：每轮淘汰 1/3 的候选、幸存者资源 ×1.5，轮数多、淘汰温和；若 `factor=3`，则每轮只留 1/3、资源 ×3，轮数少、淘汰狠、更快但更容易误杀。

```text
HalvingRandomSearchCV(pipe_svc, param_distributions, n_candidates='exhaust',
                      resource='n_samples', factor=1.5)
    ↓
hs.fit(X_train, y_train)
    ├── 第 0 轮：尽可能多的候选 × 极少样本（r_0 由算法按 budget 推出）
    ├── 每轮：按得分保留前 ~1/1.5，资源 ×1.5
    └── 最后一轮（资源≈全量）：胜出者 → best_score_ / best_params_ / best_estimator_
```

#### 4.3.3 源码精读

首先是第 22–23 行的实验性开关（已在本讲源码地图引用过）：

[ch06/ch06.py:L22-L23](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L22-L23)

```python
from sklearn.experimental import enable_halving_search_cv
from sklearn.model_selection import HalvingRandomSearchCV
```

`Halving*` 系列 API 位于 `sklearn.experimental`，必须**先**导入 `enable_halving_search_cv` 这个开关模块才能从 `sklearn.model_selection` 导入 halving 搜索类。scikit-learn 官方说明 experimental API 的行为可能随版本调整——不同版本上的细节（默认参数、警告信息）可能不同，这是使用时要有心理预期的。

然后是搜索器构造与评估（第 427–447 行）：

[ch06/ch06.py:L427-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L434)

```python
hs = HalvingRandomSearchCV(
    pipe_svc,
    param_distributions=param_grid,
    n_candidates='exhaust',
    resource='n_samples',
    factor=1.5,
    random_state=1,
    n_jobs=-1)
```

参数逐个读：`param_distributions` 沿用 4.1 的双列表网格（列表在此被当作离散分布采样）；`n_candidates='exhaust'` 表示第一轮候选数取「预算能支撑的最大值」——即让资源递增过程恰好把训练数据用尽，而不是固定一个候选数；`resource='n_samples'` 定义「资源」为训练样本数；`factor=1.5` 是每轮的资源放大倍数（也是候选淘汰率的倒数）。

[ch06/ch06.py:L439-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L439-L447)

```python
hs = hs.fit(X_train, y_train)
print(hs.best_score_)
print(hs.best_params_)


clf = hs.best_estimator_
print(f'Test accuracy: {hs.score(X_test, y_test):.3f}')
```

notebook 输出：

```text
0.9676470588235293
{'svc__kernel': 'rbf', 'svc__gamma': 0.0001, 'svc__C': 100.0}
Test accuracy: 0.965
```

把三种搜索的结果放在一起看（前两行来自 4.1/4.2，均出自 notebook 输出）：

| 搜索器 | 预算（fit 级别） | best_score_ | best_params_ | 测试集 |
|---|---|---|---|---|
| GridSearchCV(cv=10) | 72×10 = 720 | 0.985 | rbf, C=100, γ=0.001 | 0.974 |
| RandomizedSearchCV(n_iter=20, cv=10) | 20×10 = 200 | 0.974 | rbf, C=10, γ=0.001 | —（书中未打印） |
| HalvingRandomSearchCV(factor=1.5) | 远小于 720（多数轮次只用零头样本） | 0.968 | rbf, C=100, γ=0.0001 | 0.965 |

注意两点：① 三者的 `best_score_` **不可直接比较**——网格/随机的 best_score_ 是全量数据 10 折 CV 的平均分，而 halving 的 best_score_ 是其最后一轮评估产生的分数，评估时的数据量与流程都不同；② halving 以小得多的代价拿到了测试集 0.965，与网格的 0.974 只差约 1 个百分点，体现了「快速淘汰烂候选」的性价比。

#### 4.3.4 代码实践：调整 factor 观察淘汰节奏

1. **实践目标**：直观感受 `factor` 对「轮数 / 候选淘汰速度 / 结果质量」的影响。
2. **操作步骤**（**示例代码**，基于 [ch06/ch06.py:L427-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L434) 改写）：

   ```python
   for f in [1.5, 3.0]:
       hs = HalvingRandomSearchCV(
           pipe_svc,
           param_distributions=param_grid,
           n_candidates='exhaust',
           resource='n_samples',
           factor=f,
           random_state=1,
           n_jobs=-1).fit(X_train, y_train)
       print(f'factor={f}: best_score={hs.best_score_:.4f}, '
             f'best_params={hs.best_params_}')
       print(f'  每轮候选数: {list(hs.n_candidates_)}')
   ```

   （`hs.n_candidates_` 是拟合后记录每轮候选数的属性；若你的 scikit-learn 版本没有该属性，可改看 `hs.cv_results_` 的行数与 `n_resources` 列。）
3. **需要观察的现象**：`factor=3.0` 的 `n_candidates_` 序列是否更短、下降更陡；两种 factor 下 `best_score_` 与 `best_params_` 是否一致。
4. **预期结果**：factor=1.5 时轮数多、每轮淘汰 1/3，能较从容地区分候选；factor=3 时轮数少、淘汰狠，总耗时更短，但最优参数可能因早期小样本评估的噪声而与 factor=1.5 不同。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么书中 halving 的 `best_score_`（0.968）低于网格搜索的（0.985），这个对比公平吗？

**答案**：不公平。两个数字的「出身」不同：网格的 0.985 是最优候选在**全量训练集 10 折 CV** 上的平均分；halving 的 0.968 是它最后一轮评估流程产生的分数，且中途的淘汰决策基于小样本评估、带有噪声——一个好配置可能在数据很少时表现平平而被提前淘汰。两者的正确对比方式是各自模型在**同一个测试集**上的表现：0.974 vs 0.965，差距缩小到约 1 个百分点，而 halving 的计算量小得多。

**练习 2**：`resource='n_samples'` 中的「资源」为什么选样本数而不是折数？

**答案**：因为「少量数据就能预判好坏」正是减半算法的前提假设——一个烂的超参数配置用 50 个样本训练往往已经很烂。折数（CV 重数）改变的是评估的稳定性而非模型见到的信息量，无法实现「先粗筛后精评」的梯度；样本数则是天然的单调递增资源。对某些模型也可以把资源设成迭代次数等其他量（scikit-learn 允许指定别的参数名作为 resource），原理相同。

**练习 3**：`HalvingGridSearchCV` 与 `HalvingRandomSearchCV` 有何异同？

**答案**：减半机制（资源递增、逐轮淘汰、factor、refit）完全相同，区别只在第一轮候选从哪来：前者穷举 `param_grid` 的全部组合，后者按 `param_distributions` 随机采样 `n_candidates` 个。正如 GridSearchCV 与 RandomizedSearchCV 的关系。

### 4.4 嵌套交叉验证：外层 cross_val_score(gs, ...)

#### 4.4.1 概念说明

4.1 练习 2 已经暴露了一个问题：`gs.best_score_` 因为「挑了最大值」而系统性偏乐观，不能当作「这套调参流程的真实性能」。如果你还想回答一个更上层的问题——**「SVM 这套流程和决策树那套流程，哪个更适合我的数据？」**——在同一份训练集上比较两个各自的 `best_score_` 就更不可靠了：两个数字都含乐观偏差，且偏差大小不同。

嵌套交叉验证（nested cross-validation）的解法很优雅：**把「GridSearchCV 调参」整体当作一个待评估的估计器**，再套一层外层交叉验证去评估它：

- **内层（inner）**：`GridSearchCV(..., cv=2)`——在外层划出的训练子集上做完整的超参数搜索，选出参数、拟合成模型；
- **外层（outer）**：`cross_val_score(gs, ..., cv=5)`——每次留出一折从未参与调参的数据，让整套「搜索 + 训练」流程在其上打分。

外层的每一折都是「干净」的：留出折的数据既没参与内层选参，也没参与最终拟合。因此外层 5 个分数的均值 ± 标准差，是对「**这套调参流程在新数据上的期望表现**」的近似无偏估计，可以用来在多个算法/流程之间做公平比较。

#### 4.4.2 核心流程

```text
cross_val_score(gs, X_train, y_train, cv=5)   # gs = GridSearchCV(pipe_svc, param_grid, cv=2)
    ↓ 外层第 k 折（k = 1..5）
    ├── 外层训练子集（约 4/5 × 455 ≈ 364 样本）→ 交给 gs.fit
    │       └── 内层 2 折 × 72 候选 = 144 次 fit，选出参数并在该子集上重训
    ├── 外层留出折（约 91 样本）→ gs.predict 打分（accuracy）
    └── 记录一个分数
    ↓
5 个分数 → mean ± std
```

总拟合次数约为 \(5 \times (72 \times 2) = 720\)——和一次 10 折网格搜索同量级，但换来的是近似无偏的流程性能估计。

注意分工口诀：**内层选模型（选超参数），外层评流程（估性能）**。嵌套 CV 给出的分数用于「选算法」，选出之后仍要用全部训练数据重跑一次 `gs.fit(X_train, y_train)` 得到最终部署的模型（如 4.1 所示），并用从未参与过的 `X_test` 做最终验收。

#### 4.4.3 源码精读

SVM 管线的嵌套 CV（第 459–467 行）：

[ch06/ch06.py:L459-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L467)

```python
gs = GridSearchCV(estimator=pipe_svc,
                  param_grid=param_grid,
                  scoring='accuracy',
                  cv=2)

scores = cross_val_score(gs, X_train, y_train,
                         scoring='accuracy', cv=5)
print(f'CV accuracy: {np.mean(scores):.3f} '
      f'+/- {np.std(scores):.3f}')
```

两行核心：内层 `gs` 沿用同一个 `pipe_svc` 和双列表 `param_grid`，但 `cv=2`（内层折数刻意少，控制总开销）；外层直接把**未 fit 的 `gs` 对象**传给 `cross_val_score`——scikit-learn 的估计器协议在此发挥作用：`cross_val_score` 会把 `gs` 当作普通估计器，在每个外层训练子集上调用它的 `fit`（触发完整网格搜索）与 `score`。注意与 4.1 不同，这里没有 `refit` 相关操作——外层每一折 fit 出的模型只用于当折打分，用完即弃。notebook 输出：

```text
CV accuracy: 0.974 +/- 0.015
```

决策树的嵌套 CV（第 473–481 行）：

[ch06/ch06.py:L473-L481](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L473-L481)

```python
gs = GridSearchCV(estimator=DecisionTreeClassifier(random_state=0),
                  param_grid=[{'max_depth': [1, 2, 3, 4, 5, 6, 7, None]}],
                  scoring='accuracy',
                  cv=2)

scores = cross_val_score(gs, X_train, y_train,
                         scoring='accuracy', cv=5)
print(f'CV accuracy: {np.mean(scores):.3f} '
      f'+/- {np.std(scores):.3f}')
```

结构完全同构，只换了被调对象与网格：`DecisionTreeClassifier` 搜 `max_depth`（含 `None` 表示不限制深度，回顾 u3-l3）。注意决策树没有放进 Pipeline——它基于阈值切分、不依赖距离，无需标准化（u3-l3 的结论）。notebook 输出：

```text
CV accuracy: 0.934 +/- 0.016
```

两相对比：SVC 流程 0.974 ± 0.015，决策树流程 0.934 ± 0.016。两个估计的标准差相近、均值差约 4 个百分点，在乳腺癌数据集上可以下结论：**带标准化的 SVM 管线优于决策树**——这就是嵌套 CV 的用途：算法选择的依据。

#### 4.4.4 代码实践：手工展开外层循环，看清每一折发生了什么

`cross_val_score(gs, ...)` 把一切封装得看不见内部。下面用显式循环重演一遍（**示例代码**），确认它与一行版结果一致：

1. **实践目标**：亲手拆开嵌套 CV，看清「外层每折 → 内层一次完整网格搜索 → 留出折打分」的执行过程。
2. **操作步骤**：

   ```python
   # 示例代码：手工展开 5 折外层
   from sklearn.model_selection import StratifiedKFold

   outer_cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=0).split(X_train, y_train)
   scores = []
   for k, (outer_train, outer_test) in enumerate(outer_cv):
       gs = GridSearchCV(estimator=pipe_svc,
                         param_grid=param_grid,
                         scoring='accuracy',
                         cv=2)
       gs.fit(X_train[outer_train], y_train[outer_train])   # 内层完整搜索
       fold_score = gs.score(X_train[outer_test], y_train[outer_test])
       scores.append(fold_score)
       print(f'外层折 {k+1}: acc={fold_score:.3f}, 最优参数={gs.best_params_}')

   print(f'手工嵌套 CV: {np.mean(scores):.3f} +/- {np.std(scores):.3f}')
   ```

3. **需要观察的现象**：每个外层折各自打印出的 `best_params_` 是否相同——**通常不同**！不同折的数据略有差异，各自搜出的最优超参数可以不一样；外层评估的正是「重新调参」这一整套流程。
4. **预期结果**：手工版均值 ± 标准差与 [ch06/ch06.py:L464-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L464-L467) 的一行版（0.974 ± 0.015）**接近但不一定相同**——因为外层折的切分方式不同（`cross_val_score` 对分类器默认用不打乱、按类分层顺序的 StratifiedKFold 切分；这里为演示加了 `shuffle=True`）。若把 `shuffle=True, random_state=0` 去掉，两边折划分一致，分数应逐折相同（**待本地验证**）。

#### 4.4.5 小练习与答案

**练习 1**：嵌套 CV 的内层 `cv=2` 会不会太糙，导致选错超参数？

**答案**：2 折确实比 10 折的评估噪声大，可能个别外层折选到的不是全局最优参数。但这正是嵌套 CV 要刻画的真实情况——「在有限预算下调参」的流程本来就不完美，外层评估的对象是这个**流程**而非某个具体参数。若追求更稳的内层选择可加大内层折数，代价是总 fit 数线性上涨（内层折数 × 72 × 5）。

**练习 2**：2×5 与 5×2（外层 2 折、内层 5 折）两种嵌套结构有何取舍？

**答案**：外层折数决定性能估计的稳定性（5 个分数比 2 个分数的均值更稳、标准差更有意义），内层折数决定调参评估的质量（5 折选参更准但每折更贵）。书选 2×5：把更多预算留给外层估计。总开销相近时，「外层多折」通常对「比较算法」这个目标更有价值。

**练习 3**：能不能把嵌套 CV 得到的 0.974 直接报告为「模型部署后的准确率」？

**答案**：不能直接等同。0.974 是「这套调参流程」在**与训练集同分布的新数据**上的期望性能估计；它经过了外层的干净留出折，比 `gs.best_score_` 可靠得多，但仍是一个基于 455 个样本的估计（标准差 0.015 提示了其波动）。最终部署的模型还要用全部训练数据重新调参拟合（4.1 的流程），并在从未参与任何环节的 `X_test` 或更新的外部数据上做验收。

## 5. 综合实践

把本讲四个模块串成一个完整任务：**复现书中的 2×5 嵌套 CV，并做 n_iter 预算实验**（即本讲规格指定的实践任务）。

**任务描述**：对乳腺癌数据集的 SVC 管线，先用内层 2 折、外层 5 折的嵌套交叉验证输出均值与标准差，验证与书中一致；再把 `RandomizedSearchCV` 的 `n_iter` 从 20 改为 100，观察最优参数与嵌套分数的变化并解释。

**操作步骤**（**示例代码**，可直接在 `ch06/` 目录下的 notebook 或 Python 会话中运行；数据加载复用仓库代码）：

```python
# 第 0 步：环境与数据（对应 ch06/ch06.py L102-L142 的复用）
#   注意运行目录需能读到 ch06 的相对路径，或直接在 ch06/ 下打开 notebook
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler, LabelEncoder
from sklearn.model_selection import (train_test_split, GridSearchCV,
                                     RandomizedSearchCV, cross_val_score)
from sklearn.pipeline import make_pipeline
from sklearn.svm import SVC

df = pd.read_csv('https://archive.ics.uci.edu/ml/'
                 'machine-learning-databases'
                 '/breast-cancer-wisconsin/wdbc.data', header=None)
X = df.loc[:, 2:].values
y = LabelEncoder().fit_transform(df.loc[:, 1].values)   # M→1, B→0
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=1)

pipe_svc = make_pipeline(StandardScaler(), SVC(random_state=1))
param_range = [0.0001, 0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0]
param_grid = [{'svc__C': param_range, 'svc__kernel': ['linear']},
              {'svc__C': param_range, 'svc__gamma': param_range,
               'svc__kernel': ['rbf']}]

# 第 1 步：复现 2x5 嵌套 CV（对应 ch06/ch06.py L459-L467）
gs = GridSearchCV(estimator=pipe_svc, param_grid=param_grid,
                  scoring='accuracy', cv=2)
scores = cross_val_score(gs, X_train, y_train, scoring='accuracy', cv=5)
print(f'SVC 嵌套 CV: {np.mean(scores):.3f} +/- {np.std(scores):.3f}')

# 第 2 步：n_iter 预算实验（对应 ch06/ch06.py L370-L399 的扩展）
import time
for n_iter in [20, 100]:
    rs = RandomizedSearchCV(estimator=pipe_svc,
                            param_distributions=param_grid,
                            scoring='accuracy', refit=True,
                            n_iter=n_iter, cv=10,
                            random_state=1, n_jobs=-1)
    t0 = time.time()
    rs.fit(X_train, y_train)
    print(f'n_iter={n_iter:3d} | best_score={rs.best_score_:.4f} | '
          f'best_params={rs.best_params_} | {time.time()-t0:.1f}s')

# 第 3 步（选做）：把 n_iter=100 的 rs 也做一次嵌套 CV
scores_rs = cross_val_score(
    RandomizedSearchCV(estimator=pipe_svc, param_distributions=param_grid,
                       scoring='accuracy', n_iter=100, cv=2, random_state=1),
    X_train, y_train, scoring='accuracy', cv=5)
print(f'RS(n_iter=100) 嵌套 CV: {np.mean(scores_rs):.3f} +/- {np.std(scores_rs):.3f}')
```

**需要观察的三个层次**：

1. 第 1 步应复现 notebook 的 `0.974 +/- 0.015`（`train_test_split(random_state=1)` 与外层切分都是确定的，应精确一致）。
2. 第 2 步 `n_iter=20` 应复现 `best_score≈0.9738`、`{'svc__kernel': 'rbf', 'svc__gamma': 0.001, 'svc__C': 10.0}`；`n_iter=100` 的最优参数与分数是否变化、耗时是否约 5 倍——**待本地验证**。
3. 第 3 步比较「随机搜索流程」与「网格搜索流程」的嵌套分数——两者应非常接近（随机搜索以小预算逼近穷举），具体数值**待本地验证**。

**结果解释要点**（写实验记录时对照）：

- 若 `n_iter=100` 的 `best_params_` 变成更接近网格最优（如 `C=100`）的组合、`best_score_` 上升：说明预算加大让采样覆盖到了更优区域——随机搜索「预算换精度」。
- 若 `best_params_` 变了但 `best_score_` 几乎不变：说明这片区域的验证分平台很平（多个配置一样好），最优参数不唯一——这本身就值得记录。
- 嵌套 CV 分数（第 1、3 步）低于对应的 `best_score_`（第 2 步）是**正常且预期**的：前者无选择偏差，后者有。

## 6. 本讲小结

- `param_grid` 传**字典列表**可为不同算法分支定义独立搜索子空间：SVM 线性核只搜 `C`（8 候选）、RBF 核搜 `C×gamma`（64 候选），共 72 个候选、`cv=10` 下 720 次 fit；避免了单字典笛卡尔积产生「线性核 + gamma」式的冗余组合。
- `best_score_`/`best_params_` 是交叉验证选出的人工最大值，**带选择性乐观偏差**；`refit=True` 让 `best_estimator_` 已在全量训练集上重训，无需再 fit；`cv_results_` 保存全部候选的完整得分表。
- `RandomizedSearchCV` 用 `n_iter` 钉死预算（20 个配置仅 200 次 fit，最优分只比穷举低约 1 个百分点）；参数取值可给离散列表，也可给 `scipy.stats.loguniform` 这类连续分布——对跨数量级的 `C`/`gamma`，对数均匀让每个数量级机会均等。注意 `ch06.py` 缺 `import scipy.stats`，顺序执行会在 L413 报 `NameError`。
- `HalvingRandomSearchCV`（需先 `from sklearn.experimental import enable_halving_search_cv`）用「候选多 × 资源少 → 逐轮淘汰 1/factor、资源 ×factor」的比赛制，把算力集中到好候选上；各搜索器的 `best_score_` 评估口径不同，**不可直接横向比较**，要比就用测试集。
- 嵌套 CV 把 `GridSearchCV` 对象整体交给外层 `cross_val_score(gs, ..., cv=5)`：内层选参数、外层评流程，得到近似无偏的流程性能估计；据此 SVC 管线（0.974 ± 0.015）优于决策树（0.934 ± 0.016）。

## 7. 下一步学习建议

本讲所有搜索都用 `scoring='accuracy'` 打分，但准确率并不是唯一的、甚至不总是合适的性能度量——在类别不平衡或代价不对称的任务里它会严重误导。下一讲 **u6-l3 混淆矩阵、指标体系与 ROC 曲线** 将继续读 `ch06/ch06.py` 的后半部分（L498 起）：`confusion_matrix`、precision/recall/F1（含 micro/macro/weighted）、`make_scorer` 自定义打分器（其实本仓库在 [ch06/ch06.py:L578-L595](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L578-L595) 已经用 `make_scorer(f1_score, pos_label=0)` 做过一次预告——把自定义 scorer 直接传给 GridSearchCV 的 `scoring` 参数），以及 ROC 曲线与 AUC。读完 u6-l3 后，你可以回到本讲的任意搜索器，把 `scoring='accuracy'` 换成自己关心的指标，体会「搜索目标」与「评估口径」的解耦。
