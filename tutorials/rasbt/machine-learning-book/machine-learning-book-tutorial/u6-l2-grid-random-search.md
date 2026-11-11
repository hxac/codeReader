# 网格/随机/减半搜索与嵌套交叉验证

> 对应《Machine Learning with PyTorch and Scikit-Learn》第 6 章 "Fine-tuning machine learning models via grid search" 一节。
> 仓库代码：`ch06/ch06.py`（等价 notebook：`ch06/ch06.ipynb`，下文引用行号一律取自 `.py` 文件）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 会写 `GridSearchCV` 的参数网格，特别是「双列表 `param_grid`」——为不同核函数定义不同搜索子空间——这种写法，并能算出一个网格要跑多少次 `fit`。
2. 正确解读 `best_score_`、`best_params_`、`best_estimator_`，理解 `refit=True` 帮你省掉了什么，并会用 `cv_results_` 检查全部候选组合的排名。
3. 会用 `RandomizedSearchCV` 的 `param_distributions` + `n_iter` 控制搜索预算，理解 `scipy.stats.loguniform`（对数均匀分布）为什么比线性网格更适合跨数量级的超参数。
4. 理解 `HalvingRandomSearchCV` 的「资源递增 + 逐轮淘汰」（successive halving）思想，以及它为什么省算力。
5. 掌握嵌套交叉验证的写法——把整个 `GridSearchCV` 对象塞进外层 `cross_val_score(gs, ...)`——并能用它无偏地比较「SVM 管线」与「决策树」两套算法+调参流程。

## 2. 前置知识

本讲直接承接 u6-l1（Pipeline、交叉验证与学习/验证曲线）。开始前请确认以下概念：

- **参数 vs 超参数**：参数（parameter）是模型在 `fit` 中从数据学出来的量（如 SVC 的支持向量系数）；超参数（hyperparameter）是训练**之前**由人设定的量（如 `SVC` 的 `C`、`gamma`、`kernel`，决策树的 `max_depth`）。超参数无法靠梯度下降学习，只能「试 + 评估」地搜索。
- **验证曲线的局限**：u6-l1 用 `validation_curve` 一次只扫逻辑回归的**单个**参数 `C`。而 SVM 要同时定 kernel、C、gamma，候选组合按**乘法**增长，单参数扫描工具不够用了——这正是本讲多参数搜索登场的理由。
- **Pipeline 与双下划线参数名**：`make_pipeline(StandardScaler(), SVC())` 生成的管线里，SVM 的 `C` 写作 `'svc__C'`（小写类名 + 双下划线，u6-l1 已建立此约定）。本讲所有搜索都用这种名字寻址。
- **数据与划分**：乳腺癌数据集 569 个样本、30 个特征，类别 M（恶性）编码为 1、B（良性）编码为 0；80/20 分层划分后 `X_train` 455 个样本、`X_test` 114 个（[ch06/ch06.py:L139-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L139-L142)）。本讲所有搜索与交叉验证**只在 `X_train` 上进行**，`X_test` 只在最后评估一次。
- **对数刻度常识**：SVM 的 `C` 与 `gamma` 合理取值往往横跨多个数量级（10⁻⁴ ~ 10³），所以网格几乎总按数量级（×10）设置。

一个贯穿本讲的直觉：**调参 = 在「超参数空间」里做搜索**。搜索策略有三个演进方向：

| 搜索方式 | 核心痛点 | 一句话策略 |
|---|---|---|
| 网格搜索 `GridSearchCV` | 穷举太贵 | 每个候选组合都给全额预算（全量数据 + 10 折 CV） |
| 随机搜索 `RandomizedSearchCV` | 组合爆炸 | 固定预算 `n_iter`，随机撒点 |
| 减半搜索 `HalvingRandomSearchCV` | 好坏候选一视同仁 | 先给所有候选一点点资源，逐轮淘汰，幸存者拿越来越多资源 |

而这三种搜索给出的分数都只在**训练集内部**产生，用来「选」模型；要无偏地「比较」两套算法流程，还需要本讲最后登场的**嵌套交叉验证**。

## 3. 本讲源码地图

本讲的全部源码集中在 `ch06/ch06.py` 的 "Fine-tuning machine learning models via grid search" 一节：

| 位置 | 内容 |
|---|---|
| [ch06/ch06.py:L19-L24](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L19-L24) | 本讲 import：`GridSearchCV`、`SVC`、`RandomizedSearchCV`、实验性 halving 开关、`DecisionTreeClassifier` |
| [ch06/ch06.py:L334-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L352) | 网格搜索：`pipe_svc`、双列表 `param_grid`、`gs.fit` 与 `best_score_`/`best_params_` |
| [ch06/ch06.py:L357-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L357-L363) | `best_estimator_`、`refit=True` 注释与测试集评估 |
| [ch06/ch06.py:L370-L399](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L370-L399) | `RandomizedSearchCV`（`n_iter=20`）的构建与拟合 |
| [ch06/ch06.py:L410-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L410-L416) | 对数均匀分布 `scipy.stats.loguniform` 演示 |
| [ch06/ch06.py:L427-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L447) | `HalvingRandomSearchCV` 的构建、拟合与测试集评估 |
| [ch06/ch06.py:L459-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L467) | 嵌套交叉验证：SVM 管线版（外 5 × 内 2） |
| [ch06/ch06.py:L473-L481](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L473-L481) | 嵌套交叉验证：决策树版 |

另外两处背景代码：数据加载 [ch06/ch06.py:L102-L104](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L104) 与标签编码 [ch06/ch06.py:L124-L127](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L124-L127)，已在 u6-l1 讲过，本讲直接复用其产物 `X_train`/`y_train`/`X_test`/`y_test`。

## 4. 核心概念与源码讲解

### 4.1 网格搜索与「双列表」param_grid

#### 4.1.1 概念说明

网格搜索（grid search）是最朴素的超参数优化：把每个超参数想尝试的取值列成表，**穷举所有组合**，每个组合用 k 折交叉验证打分，分数最高者胜。它做的事可以写成：

\[
\text{best params} = \arg\max_{p \in \text{网格}} \frac{1}{k}\sum_{i=1}^{k} \text{score}\big(M_p, D_{\text{train}}^{(i)}\big)
\]

其中 \(M_p\) 是用参数组合 \(p\) 构建的模型，\(D_{\text{train}}^{(i)}\) 是第 \(i\) 折的训练部分。

网格搜索最大的问题是组合爆炸：候选组合数是各参数取值数的**乘积**。本讲的例子有一处精妙设计：**linear 核的 SVM 根本没有 `gamma` 参数**，如果把 `gamma` 也放进它的搜索空间，就会浪费大量算力去评估对 linear 核毫无意义的取值。scikit-learn 的解法就是「双列表」`param_grid`：它是一个**由字典组成的列表**，每个字典描述一个独立的搜索子空间，各子空间彼此独立、结果取并集。

#### 4.1.2 核心流程

1. 用 `make_pipeline(StandardScaler(), SVC(random_state=1))` 把缩放器和 SVM 绑成管线（SVM 基于距离，标准化是硬前提，见 u3-l2）。
2. 构造双列表 `param_grid`：
   - 子空间 1（linear 核）：`svc__C`（8 个值）× `svc__kernel`（1 个值）= 8 种组合；
   - 子空间 2（rbf 核）：`svc__C`（8 个值）× `svc__gamma`（8 个值）× `svc__kernel`（1 个值）= 64 种组合。
3. 组合总数 = 8 + 64 = **72**；`cv=10` 意味着每个组合要 fit 10 次，共 **720 次 fit**。
4. `gs.fit(X_train, y_train)` 启动搜索：对每个组合做 10 折交叉验证，取平均分。
5. 排名第一的组合被记录进 `gs.best_params_`、`gs.best_score_`。

伪代码：

```text
for 子空间 in param_grid:                # 2 个子空间
    for 组合 in 笛卡尔积(子空间.items()):  # 8 种 / 64 种
        for fold in 10 折:
            clone(管线).set_params(组合).fit(该折训练部分)
            记录该折 score
        该组合得分 = 10 折平均
best = 72 个组合中得分最高者
```

#### 4.1.3 源码精读

先搭管线、列出候选值（[ch06/ch06.py:L334-L337](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L337)）：

```python
pipe_svc = make_pipeline(StandardScaler(),
                         SVC(random_state=1))

param_range = [0.0001, 0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0]
```

这段代码把 `StandardScaler` 与 `SVC` 组成管线，并定义 8 个数量级上的候选值（10⁻⁴ 到 10³），同时供 `C` 和 `gamma` 使用。

双列表 `param_grid`（[ch06/ch06.py:L339-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L343)）：

```python
param_grid = [{'svc__C': param_range, 
               'svc__kernel': ['linear']},
              {'svc__C': param_range, 
               'svc__gamma': param_range, 
               'svc__kernel': ['rbf']}]
```

第一个字典是 linear 核子空间（只搜 `C`），第二个字典是 rbf 核子空间（同时搜 `C` 与 `gamma`）。注意 `'svc__kernel': ['linear']` 即便只有一个值也要写成**列表**——`param_grid` 里每个值都必须是可迭代的取值集合；参数名用 `svc__` 前缀定位管线中的 SVM 步骤。

搜索器的构建与拟合（[ch06/ch06.py:L345-L350](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L350)）：

```python
gs = GridSearchCV(estimator=pipe_svc, 
                  param_grid=param_grid, 
                  scoring='accuracy', 
                  refit=True,
                  cv=10)
gs = gs.fit(X_train, y_train)
```

这段代码创建网格搜索器：`scoring='accuracy'` 指定用准确率打分，`cv=10` 用 10 折交叉验证，`refit=True`（详见 4.2），随后在 `X_train` 上启动 720 次 fit 的穷举。

#### 4.1.4 代码实践（源码阅读 + 心算型）

1. **实践目标**：不动代码，练会「读网格、算预算」。
2. **操作步骤**：
   - 打开 [ch06/ch06.py:L339-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L343)，分别写出两个子空间的组合数与总数；
   - 若把 `param_range` 每个参数从 8 个值扩到 10 个值（10⁻⁴ 到 10⁵），先心算新的组合总数与 fit 次数；
   - 再思考：如果把两个字典合并成一个同时含 `C`、`gamma`、`kernel` 的字典，组合数会变成多少？多出的组合浪费在哪里？
3. **需要观察的现象**：自己算出的数字 vs 下面的答案。
4. **预期结果**：
   - 当前：8 + 64 = 72 组合，72 × 10 = 720 次 fit；
   - 扩到 10 个值：10 + 100 = 110 组合，110 × 10 = 1100 次 fit；
   - 若合并成一个字典：kernel 2 种 × C × gamma = 2×8×8 = 128 组合，其中 linear 核的 64 组合里 `gamma` 完全不起作用（SVC 在 linear 核下忽略 `gamma`），白白多算 64-8=56 种重复评估——这正是双列表写法存在的理由。
5. 本实践为纯阅读型，无需运行环境；数值结论可直接推导，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`param_grid` 为什么是「字典的列表」而不是一个大字典？
**答案**：大字典意味着所有参数做**统一**的笛卡尔积，不同核函数会被迫共享同一套参数空间；而 linear 核没有 `gamma` 参数，统一网格会评估大量对它无意义的组合。字典的列表允许每个子空间自定义参数集，搜索空间取各子空间的**并集**。

**练习 2**：把 `cv` 从 10 改成 5，fit 次数怎么变？分数的质量可能怎么变？
**答案**：fit 次数减半（720 → 360），因为次数 = 组合数 × 折数。每折的验证集变大（训练数据 455 个样本时从约 45 个变成约 91 个），单折分数的方差更小，但平均分的样本只有 5 个，对模型表现的估计会略粗糙——这是「算力」与「估计稳定性」的取舍。

**练习 3**：为什么 `pipe_svc` 里必须放 `StandardScaler`？
**答案**：SVM 基于样本间距离与内积构造最大间隔，乳腺癌 30 个特征量纲差异悬殊，不标准化则距离被大数值特征主导（u3-l2 已验证过）。放进管线还保证了搜索的每一折都重新拟合缩放器，避免数据泄漏（u6-l1 的核心结论）。

### 4.2 best_score_ / best_params_ / best_estimator_ 与 cv_results_

#### 4.2.1 概念说明

`fit` 结束后，搜索对象暴露三件套：

- `best_score_`：最优组合的 **k 折交叉验证平均分**——注意它是训练集内部的「选美分数」，**不是**泛化性能；
- `best_params_`：最优组合的参数字典；
- `best_estimator_`：用最优参数在**全部** `X_train` 上**重新 fit** 出的模型（前提 `refit=True`）。

`refit=True` 的意义：交叉验证只是为了打分，打分用的模型各自只见过 9/10 的数据；搜索结束后，scikit-learn 自动用冠军参数在 100% 的训练数据上再 fit 一次，得到的 `best_estimator_` 可直接用于测试集评估，不需要你手写 `clf.fit(X_train, y_train)`。

`cv_results_` 则是一张「全部候选的成绩单」字典（本讲仓库代码没有显式用它，但它是三种搜索器共有的标准属性）：键如 `'params'`（每个组合）、`'mean_test_score'`（平均分）、`'rank_test_score'`（排名），用来检查冠军与亚军的差距、诊断搜索空间设计得好不好。

一个必须建立的概念：`best_score_` 存在**选择性乐观偏差**——它是 72 个交叉验证分数中的**最大值**。取 max 的动作本身会推高这个数，参数组合越多推得越高。所以它只配用来「选」，最终性能要用没参与过任何搜索的 `X_test` 来报。

#### 4.2.2 核心流程

1. `gs = gs.fit(X_train, y_train)` 完成后：
2. `print(gs.best_score_)` → 打印最优组合的 10 折平均准确率；
3. `print(gs.best_params_)` → 打印最优参数组合；
4. `clf = gs.best_estimator_` → 取出在全量 `X_train` 上重训过的冠军模型；
5. `clf.score(X_test, y_test)` → 在从未参与搜索的测试集上出最终成绩。

#### 4.2.3 源码精读

打印最优分数与参数（[ch06/ch06.py:L350-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L350-L352)）：

```python
gs = gs.fit(X_train, y_train)
print(gs.best_score_)
print(gs.best_params_)
```

这段代码在 720 次 fit 后输出最优的 10 折平均准确率与对应参数组合。

`best_estimator_` 与 `refit=True` 的说明（[ch06/ch06.py:L357-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L357-L363)）：

```python
clf = gs.best_estimator_

# clf.fit(X_train, y_train) 
# note that we do not need to refit the classifier
# because this is done automatically via refit=True.

print(f'Test accuracy: {clf.score(X_test, y_test):.3f}')
```

源码注释明确说明：不需要再手写 `clf.fit(...)`，因为构造 `gs` 时传了 `refit=True`（[ch06/ch06.py:L345-L349](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L345-L349)），fit 完成时已自动用最优参数在全量训练集上重训；随后直接在测试集上评估。

notebook 中保存的真实输出（`ch06/ch06.ipynb` 对应单元格）：

```text
0.9846859903381642
{'svc__C': 100.0, 'svc__gamma': 0.001, 'svc__kernel': 'rbf'}
Test accuracy: 0.974
```

即：rbf 核、`C=100`、`gamma=0.001` 以 98.47% 的 10 折 CV 准确率夺魁；该模型在测试集上的准确率为 97.4%——测试分（0.974）低于 CV 最高分（0.985），正体现了 `best_score_` 的乐观偏差与真实泛化差距。

`cv_results_` 的用法（**示例代码**，不在仓库源码中）：

```python
import pandas as pd
results = pd.DataFrame(gs.cv_results_)
top5 = results.nsmallest(5, 'rank_test_score')[
    ['rank_test_score', 'mean_test_score', 'params']]
print(top5)
```

这段示例代码把成绩单字典转成 DataFrame，按排名取前 5，检查冠军与亚军的差距有多大（若只差千分之几，说明多组参数都够好，搜索空间可以缩小）。

#### 4.2.4 代码实践

1. **实践目标**：用 `cv_results_` 摸清 72 个组合的整体表现，而不只盯着冠军。
2. **操作步骤**：
   - 先完整复现 [ch06/ch06.py:L334-L352](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L352)（加载划分数据后运行网格搜索，耗时数分钟）；
   - 再运行上面的 `cv_results_` 示例代码；
   - 补一句统计：`results['mean_test_score'].groupby(results['param_svc__kernel']).mean()`，对比 linear 与 rbf 两个子空间的平均分。
3. **需要观察的现象**：排名第 1 与第 5 的 `mean_test_score` 差多少；linear 与 rbf 子空间谁的平均分高、谁的方差大。
4. **预期结果**：第 1 名应与 notebook 保存输出一致（rbf、`C=100.0`、`gamma=0.001`，约 0.9847）；其余排名的具体数值**待本地验证**。rbf 子空间平均分大概率低于 linear 子空间，因为 rbf 网格里含大量 `gamma` 过大/过小的失败组合。
5. 若懒得等 720 次 fit，可先把 `param_range` 截短为 `[0.01, 0.1, 1.0, 10.0]` 快速跑通流程，再恢复完整网格。

#### 4.2.5 小练习与答案

**练习 1**：`best_score_` 能当作模型上线后的预期准确率汇报吗？
**答案**：不能。它是 72 个 CV 分数的最大值，带选择性乐观偏差；且全部计算都发生在训练集上。诚实的外汇报价是 `best_estimator_` 在从未参与调参的 `X_test` 上的分数（本例 0.974）。

**练习 2**：`refit=True` 省掉的是哪一步？如果设 `refit=False` 会怎样？
**答案**：省掉「用最优参数在全量训练集上重训」这一步。`refit=False` 时没有 `best_estimator_` 属性，你得自己 `pipe_svc.set_params(**gs.best_params_).fit(X_train, y_train)`；当搜索只为「比较参数」而无需产出模型时（如纯研究场景）可以省这次重训的算力。

**练习 3**：`cv_results_` 里 `mean_test_score` 的 "test" 指测试集 `X_test` 吗？
**答案**：不是。它指交叉验证中每折的**验证折**（held-out fold）分数的平均，全程没有接触 `X_test`。命名容易误导，记住「搜索器内部的一切分数都来自训练集内部切分」。

### 4.3 RandomizedSearchCV：预算控制与对数均匀分布

#### 4.3.1 概念说明

网格搜索的算力随参数个数乘法增长。随机搜索（randomized search）换了一个思路：**不穷举，撒点**——指定采样次数 `n_iter`，从参数分布中随机抽 `n_iter` 个组合，每个照常做 k 折 CV。它有两个网格搜索没有的优点：

1. **预算可控**：算力 = `n_iter × k`，与参数个数无关。本例 20 × 10 = 200 次 fit，只有网格搜索 720 次的 28%。
2. **覆盖更广**：当某些参数比其他参数更重要时，随机组合能把采样「摊」到每个参数的更多不同取值上，而网格会在不重要的参数维度上重复堆叠。书中以图 06_11 对比了两种搜索在二维参数空间中的采样分布。

`param_distributions` 的写法比 `param_grid` 更灵活：值既可以是**列表**（从中离散等概率抽取，写法与 `param_grid` 完全兼容，双列表照样可用），也可以是 **scipy 的分布对象**（连续抽样）。对跨 7 个数量级的 `C`/`gamma`，最合适的连续分布是**对数均匀分布** `scipy.stats.loguniform(a, b)`：它在 \(\log\) 空间上均匀，即

\[
X \sim \text{loguniform}(a, b) \iff \log X \sim U(\log a, \log b)
\]

若改用普通均匀分布 \(U(0.0001, 1000)\)，超过 90% 的采样会落在 \([100, 1000]\) 这一个数量级里，小数值区间几乎采不到；对数均匀则保证每个数量级被采中的机会均等。

#### 4.3.2 核心流程

1. 重建（内容相同的）`pipe_svc` 与双列表 `param_grid`；
2. 构造 `RandomizedSearchCV`：`param_distributions` 传双列表，`n_iter=20` 限定只抽 20 个组合，`random_state=1` 保证可复现，`n_jobs=-1` 并行；
3. `rs.fit(X_train, y_train)`：20 组合 × 10 折 = 200 次 fit；
4. 同样读 `rs.best_score_` / `rs.best_params_` / `rs.best_estimator_`（接口与 GridSearchCV 完全一致）；
5. 想要连续采样时，把网格里的某个列表换成 `scipy.stats.loguniform(...)`，每轮从分布中现抽一个值。

#### 4.3.3 源码精读

随机搜索的构建（[ch06/ch06.py:L381-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L381-L388)）：

```python
rs = RandomizedSearchCV(estimator=pipe_svc,
                        param_distributions=param_grid,
                        scoring='accuracy',
                        refit=True,
                        n_iter=20,
                        cv=10,
                        random_state=1,
                        n_jobs=-1)
```

这段代码把 4.1 的双列表直接交给 `param_distributions`——列表值会被离散等概率抽取；`n_iter=20` 把预算钉死在 200 次 fit；`random_state=1` 让随机采样可复现；`n_jobs=-1` 启用全部 CPU 核并行。

拟合与结果（[ch06/ch06.py:L393-L399](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L393-L399)）：

```python
rs = rs.fit(X_train, y_train)
print(rs.best_score_)

print(rs.best_params_)
```

notebook 保存的真实输出：

```text
0.9737681159420291
{'svc__kernel': 'rbf', 'svc__gamma': 0.001, 'svc__C': 10.0}
```

20 次随机组合的冠军是 rbf、`C=10.0`、`gamma=0.001`，CV 分 0.9738——比网格搜索的 0.9847 低约 1 个百分点，但只花了 28% 的算力；抽到的最优参数也与网格冠军（`C=100.0`）不同，说明 20 个样本没抽中真正的全局最优组合。

对数均匀分布演示（[ch06/ch06.py:L410-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L410-L416)）：

```python
param_range = [0.0001, 0.001, 0.01, 0.1,
               1.0, 10.0, 100.0, 1000.0]

param_range = scipy.stats.loguniform(0.0001, 1000.0)

np.random.seed(1)
param_range.rvs(10)
```

这段代码先用列表回顾旧写法，随即把 `param_range` 重绑定为对数均匀分布对象，并用 `rvs(10)` 抽 10 个样本展示其跨越数量级的取值。notebook 保存的输出为：

```text
array([8.30e-02, 1.10e+01, 1.00e-04, 1.31e-02, 1.06e-03, 4.43e-04,
       2.01e-03, 2.62e-02, 5.99e-02, 5.91e-01])
```

10 个样本散布在 10⁻⁴ 到 10¹ 之间，每个数量级都有机会被采中——这正是把它交给 `param_distributions` 后随机搜索能「广撒网」的原因。

> **复现提示**：ch06.py 这个文件里**没有** `import scipy`，直接照抄运行 [L413](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L413) 会报 `NameError`。notebook 对应单元格里其实有 `import scipy.stats` 这一行，只是转换成脚本时被略去了；自己复现时请补上这句 import。
>
> 另一个 Python 细节：[L413](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L413) 把 `param_range` 这个**名字**重新绑到了分布对象上，但此前 `param_grid` 里存的是旧列表的引用，不受影响——所以后面的减半搜索（L429）与嵌套 CV（L460）用的仍是列表版网格。

#### 4.3.4 代码实践

1. **实践目标**：体会「离散列表 → 连续分布」的差异：分布采样不再落在 ×10 的整数档位上。
2. **操作步骤**：
   - 复现 [ch06/ch06.py:L370-L388](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L370-L388) 得到 `rs`（注意文件开头需自行 `import scipy.stats`）；
   - 构造连续版分布（**示例代码**）：

     ```python
     import scipy.stats
     param_dist = [{'svc__C': scipy.stats.loguniform(0.0001, 1000.0),
                    'svc__kernel': ['linear']},
                   {'svc__C': scipy.stats.loguniform(0.0001, 1000.0),
                    'svc__gamma': scipy.stats.loguniform(0.0001, 1000.0),
                    'svc__kernel': ['rbf']}]
     rs2 = RandomizedSearchCV(pipe_svc, param_dist, n_iter=20, cv=10,
                              scoring='accuracy', random_state=1, n_jobs=-1)
     rs2.fit(X_train, y_train)
     print(rs2.best_score_, rs2.best_params_)
     ```
   - 与 `rs`（列表版）的 0.9738 / `{'svc__C': 10.0, ...}` 对比。
3. **需要观察的现象**：`rs2.best_params_` 里的 `C`、`gamma` 是否还是整齐的 10⁻⁴、10⁻³… 档位；分数与列表版差多少。
4. **预期结果**：连续版的参数应是 8.30e-02、1.10e+01 这类「非整档」值（与 `rvs` 演示同源）；最优分数与列表版相近但通常不完全相同，具体数值**待本地验证**。`kernel` 是离散字符串，必须仍用列表，不能换成分布。
5. 若时间紧张，把 `cv` 降为 5 折即可把每次搜索的 fit 数从 200 降到 100。

#### 4.3.5 小练习与答案

**练习 1**：`n_iter=20` 时随机搜索比网格搜索省多少算力？代价是什么？
**答案**：fit 次数 200 vs 720，省 72%（假设单次 fit 同价）。代价是只覆盖了 72 个离散组合中的约 20 个，全局最优组合可能没被抽中——本例 `best_score_` 低了约 1 个百分点（0.974 vs 0.985）。

**练习 2**：为什么用 `loguniform` 而不是 `uniform(0.0001, 1000)` 采样 `C`？
**答案**：`C` 的合理范围跨 7 个数量级。均匀分布下区间 \([100, 1000]\) 占总长度约 90%，会被过度采样，而最有用的小数值区间几乎抽不到；`loguniform` 在对数空间均匀，每个数量级（如 10⁻⁴~10⁻³ 与 10²~10³）被采中的概率相同。

**练习 3**：`random_state=1` 与 `np.random.seed(1)` 各管什么？
**答案**：`random_state` 是传给 `RandomizedSearchCV` 的参数，由 sklearn 内部独立的随机数生成器使用，决定抽哪些参数组合；`np.random.seed` 设置的是全局 numpy 随机态，只影响代码里直接调用的 `np.random.*` / `rvs`（在 [L415-L416](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L415-L416) 中是为了让 `param_range.rvs(10)` 的演示可复现）。两者互不干扰。

### 4.4 HalvingRandomSearchCV：资源递增与逐轮淘汰

#### 4.4.1 概念说明

前两种搜索对**所有候选一视同仁**：无论组合好坏，都享受全量数据 + 全额 k 折。减半搜索（successive halving，源自多臂老虎机研究）引入了**淘汰赛**：

- **资源（resource）**：训练时投入的算力度量，这里选 `resource='n_samples'`——投入的训练样本数；
- **因子（factor）**：每轮资源 ×factor、幸存者 ÷factor。`factor=1.5` 表示每轮训练样本变 1.5 倍，只有排名前约 1/1.5 ≈ 67% 的候选晋级；
- 第 0 轮：**所有**候选各用一小撮样本（如几十个）训练并 CV 打分；第 1 轮：幸存者样本加倍再比；……直到资源耗尽（用满训练集），最后一名即冠军。

直觉类比：招聘先看简历（廉价粗筛）再逐轮面试（昂贵细察），而不是对每个候选人都做五轮终面。代价也显而易见：**早期的评估是嘈杂的**——几十个样本打出的分数方差大，真正的好候选可能被误杀在第一轮。

#### 4.4.2 核心流程

1. 导入实验性开关 `enable_halving_search_cv` 与 `HalvingRandomSearchCV`（sklearn 中该功能尚属实验性，必须先导入开关模块）；
2. 构造：候选来自与 `rs` 相同的 `param_grid`（随机撒点），`n_candidates='exhaust'` 表示首轮候选数取到「资源能支撑的最大值」，`resource='n_samples'` + `factor=1.5` 定义淘汰规则；
3. `hs.fit(X_train, y_train)` 内部自动执行多轮「训练→打分→淘汰→加倍」；
4. 读 `hs.best_score_` / `hs.best_params_` / `hs.best_estimator_`，接口与前两者完全一致。

#### 4.4.3 源码精读

实验性开关的导入（[ch06/ch06.py:L22-L23](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L22-L23)）：

```python
from sklearn.experimental import enable_halving_search_cv
from sklearn.model_selection import HalvingRandomSearchCV
```

这两行先启用实验性 halving 功能（不导入 `enable_halving_search_cv` 就无法从 `sklearn.model_selection` 导入 halving 类），再导入减半随机搜索类。

减半搜索的构建（[ch06/ch06.py:L427-L434](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L434)）：

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

这段代码指定：候选组合随机抽自双列表网格；首轮候选数尽可能多（`'exhaust'`，让最后一轮恰好用尽资源）；淘汰用的资源是训练样本数，每轮 ×1.5。这里没有传 `scoring` 与 `cv`，分别按 sklearn 默认取估计器自身的 `score`（分类器即准确率）与 5 折。

结果与测试集评估（[ch06/ch06.py:L439-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L439-L447)）：

```python
hs = hs.fit(X_train, y_train)
print(hs.best_score_)
print(hs.best_params_)

clf = hs.best_estimator_
print(f'Test accuracy: {hs.score(X_test, y_test):.3f}')
```

notebook 保存的真实输出：

```text
0.9676470588235293
{'svc__kernel': 'rbf', 'svc__gamma': 0.0001, 'svc__C': 100.0}
Test accuracy: 0.965
```

减半搜索选出 rbf、`C=100.0`、`gamma=0.0001`，测试准确率 0.965，与网格搜索的 0.974 同一水平，但总 fit 成本显著更低——大部分候选早在只吃几十个样本的轮次里就被淘汰了。

三种搜索在本数据集上的对照（均为 notebook 保存输出）：

| 搜索器 | 预算（fit 次数上限） | best_score_ | best_params_ | 测试准确率 |
|---|---|---|---|---|
| GridSearchCV | 72 组合 × 10 折 = 720 | 0.9847 | rbf, C=100.0, gamma=0.001 | 0.974 |
| RandomizedSearchCV (n_iter=20) | 20 × 10 = 200 | 0.9738 | rbf, C=10.0, gamma=0.001 | —（书中未评估） |
| HalvingRandomSearchCV | 按轮次递增，远低于 720 | 0.9676 | rbf, C=100.0, gamma=0.0001 | 0.965 |

注意 `best_score_` 三者**不可直接横比**：网格/随机的分数是全量数据 10 折平均，减半的分数产生于末轮（资源与折数都不同）的评估条件。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「每轮资源 ×factor、候选 ÷factor」的淘汰过程。
2. **操作步骤**（**示例代码**，基于 [ch06/ch06.py:L427-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L427-L447) 复现后追加）：

   ```python
   print('迭代轮数:', hs.n_iterations_)
   print('每轮候选数:', hs.n_candidates_)
   print('每轮资源(样本数):', hs.n_resources_)
   ```
3. **需要观察的现象**：`n_candidates_` 是否逐轮约按 1/1.5 衰减、`n_resources_` 是否逐轮 ×1.5 直至逼近 455（训练集样本数）。
4. **预期结果**：两个列表长度相同（= 轮数），首行候选数最多、资源最少，末行候选寥寥、资源接近训练集规模；具体数值**待本地验证**（也可从 `hs.cv_results_` 的 `'iter'`、`'n_resources'` 列查看同样信息）。
5. 试试把 `factor` 从 1.5 改成 3：轮数应变少、每轮淘汰更狠，观察最优参数是否改变，并思考为什么更激进的淘汰更依赖早期评估的可靠性。

#### 4.4.5 小练习与答案

**练习 1**：减半搜索「省算力」的机制是什么？省下来的算力从哪里抠出来的？
**答案**：让大概率平庸的候选只在**少量样本**上被粗评就淘汰，只把全量数据的训练留给少数幸存者。省的是被淘汰候选在后期轮次（大资源）里本要花费的训练成本。

**练习 2**：`factor=1.5` 与 `factor=3` 分别意味着什么取舍？
**答案**：`factor` 越大，每轮淘汰越狠、搜索越快，但早期嘈杂评估误杀好候选的风险越高；`factor` 接近 1 时行为趋近普通随机搜索（每轮几乎不淘汰）。1.5 是较温和的设置。

**练习 3**：为什么这段代码必须先 `from sklearn.experimental import enable_halving_search_cv`？
**答案**：在本书使用的 scikit-learn 1.0 中，halving 系列搜索仍标记为实验性 API，scikit-learn 要求显式导入这个开关模块表示你知晓 API 未来可能变动，否则无法从 `sklearn.model_selection` 导入 `HalvingRandomSearchCV`。

### 4.5 嵌套交叉验证：内层选模型，外层评估

#### 4.5.1 概念说明

前三种搜索回答「哪组超参数最好」，但有一个无法回避的偏差：`best_score_` 是在同一批训练数据上**边搜边选**得到的（72 个 CV 分数取 max），天然偏乐观。如果想回答更根本的问题——

> 「SVM 这套算法（含调参流程）和决策树那套算法（含调参流程），在这个数据上谁更强？」

——就需要**嵌套交叉验证（nested cross-validation）**：用两层循环把「选模型」和「评模型」严格分开：

- **内层（模型选择）**：在**外层的每个训练折内部**跑一次网格搜索，选出该折的最优超参数、重训出模型——这一层完全看不到外层验证折；
- **外层（模型评估）**：`cross_val_score` 把「整个网格搜索流程」当作一个普通估计器，在 5 个外层折上轮流评估，得到 5 个分数。

这样每个外层分数都来自「从未参与该折调参」的数据，对**整套算法流程**（算法 + 搜索空间 + 调参策略）的泛化性能给出了接近无偏的估计。书中用 5 外折 × 2 内折的 5×2 配置（图 06_07）。

代价是算力按乘法叠加：SVM 网格 72 组合 × 2 内折 + 1 次重训 = 每个外层折 145 次 fit，5 个外层折共 \(5 \times 145 = 725\) 次 fit；决策树 8 组合只要 \(5 \times (8 \times 2 + 1) = 85\) 次。

#### 4.5.2 核心流程

以 SVM 管线为例：

1. 构造 `gs = GridSearchCV(pipe_svc, param_grid, cv=2)`——这就是**内层**（2 折选参数）；
2. `cross_val_score(gs, X_train, y_train, cv=5)`——这是**外层**（5 折评估）：
   - 外层第 \(i\) 折：把 `gs` **克隆**后在「外层训练部分」上完整跑一遍网格搜索（内层选参 + 重训），再在「外层验证折」上打分；
   - 注意传给 `cross_val_score` 的第一个参数不是模型，而是**搜索器对象**——scikit-learn 中任何有 `fit`/`predict` 的对象都是估计器，搜索器 fit 完也满足；
3. 5 个分数取均值 ± 标准差，即为该算法流程的嵌套 CV 性能；
4. 对决策树重复同样流程，两个数字即可公平比较。

#### 4.5.3 源码精读

SVM 管线的嵌套交叉验证（[ch06/ch06.py:L459-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L467)）：

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

这段代码先把 4.1 的搜索器降为内层 2 折（嵌套 CV 中内层折数可小一些以控制算力），再把**搜索器本身**作为被评估的估计器交给外层 5 折 `cross_val_score`——嵌套的全部奥义就在这一行 `cross_val_score(gs, ...)` 的参数上。

notebook 保存的真实输出：

```text
CV accuracy: 0.974 +/- 0.015
```

决策树的对照实验（[ch06/ch06.py:L473-L481](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L473-L481)）：

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

这段代码把估计器换成决策树（无需管线：树基于阈值切分，不依赖标准化，u3-l3 的结论），内层只搜 `max_depth` 这一个参数（含 `None` 即不限制深度）。notebook 保存的真实输出：

```text
CV accuracy: 0.934 +/- 0.016
```

结论：SVM 管线 0.974 ± 0.015 明显优于决策树 0.934 ± 0.016。另有一个耐人寻味的对照：SVM 的嵌套 CV 分数（0.974）比它自己的 `gs.best_score_`（0.985）低了约 1 个百分点——这 1 个百分点正是「在同一批数据上选参数」造成的乐观偏差，被嵌套结构显式地暴露出来了。

#### 4.5.4 代码实践（本讲主实践）

1. **实践目标**：复现 2×5 嵌套 CV；再把内层换成 `RandomizedSearchCV` 并把 `n_iter` 从 20 提到 100，观察最优参数与嵌套分数的变化。
2. **操作步骤**：
   - **第一步（复现基线）**：按 [ch06/ch06.py:L459-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L467) 原样运行，确认输出 `CV accuracy: 0.974 +/- 0.015`（约几分钟）；
   - **第二步（改预算）**：把内层换成随机搜索并扫 `n_iter`（**示例代码**）：

     ```python
     for n_iter in [20, 100]:
         rs = RandomizedSearchCV(estimator=pipe_svc,
                                 param_distributions=param_grid,
                                 scoring='accuracy', cv=2,
                                 n_iter=n_iter, random_state=1, n_jobs=-1)
         scores = cross_val_score(rs, X_train, y_train,
                                  scoring='accuracy', cv=5)
         print(f'n_iter={n_iter}: '
               f'{np.mean(scores):.3f} +/- {np.std(scores):.3f}')
     ```
   - **第三步（看参数）**：循环外单独 `rs.fit(X_train, y_train)` 一次，比较 `n_iter=20` 与 `100` 的 `rs.best_params_`、`rs.best_score_`（参考值：20 时为 0.9738 与 `{'svc__C': 10.0, 'svc__gamma': 0.001, 'svc__kernel': 'rbf'}`，见 4.3.3）。
3. **需要观察的现象**：
   - `n_iter=100` 的 `best_score_` 是否比 20 时更接近网格搜索的 0.9847？`best_params_` 是否变成/接近网格冠军（rbf, C=100.0, gamma=0.001）？
   - 嵌套分数（均值 ± 标准差）随 `n_iter` 变大是上升、持平还是波动？
4. **预期结果**：`n_iter=100` 已超过 72 个离散组合数，覆盖面大增，`best_score_` 预计逼近乃至达到 0.9847，`best_params_` 更可能命中网格冠军；嵌套分数预计仍在 0.97 附近小幅波动——因为外层评估的是「流程」的泛化能力，内层搜得再充分也不改变模型本身的上限。具体数值**待本地验证**。
5. **解释任务**：用一句话回答「为什么 n_iter 增大 5 倍，嵌套分数却未必提高？」——内层更充分只保证「选得更准」，而外层分数衡量的是选定模型在未见数据上的表现；当选参误差本来就小时，额外预算买不到泛化提升。

#### 4.5.5 小练习与答案

**练习 1**：嵌套 CV 中内层和外层各自负责什么？为什么必须分开？
**答案**：内层负责**模型选择**（在训练折内挑超参数），外层负责**模型评估**（在未参与调参的验证折上打分）。若不分开（直接报 `best_score_`），数据既当过「考官」（产生分数供选择）又当过「考生」，分数带选择性乐观偏差——本例中 0.985 vs 嵌套的 0.974。

**练习 2**：本讲嵌套 CV 一共运行多少次 `fit`？为什么内层用 2 折而不是 10 折？
**答案**：SVM：每个外层折内层 72×2 + 1 次重训 = 145，共 5×145 = 725 次。内层折数降到 2 是为了控制总算力（若内层也 10 折则是 5×(72×10+1) = 3605 次）；内层只需「相对比较」参数好坏，2 折的噪声通常够用，而外层折数直接决定性能估计的稳定性，保留 5 折。

**练习 3**：决策树的 `param_grid` 里为什么要包含 `None`？
**答案**：`max_depth=None` 是「不限制深度、一直长到叶子纯净」的默认行为，是与 1~7 并列的一种候选策略（也是最容易过拟合的极端）；不放进网格就没法与其他深度公平比较。

## 5. 综合实践

**任务：在乳腺癌数据集上办一场「三种搜索器 + 嵌套验证」的对照实验。**

1. **准备**：复现 [ch06/ch06.py:L102-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L142) 得到 `X_train`/`X_test` 等；定义统一的 `pipe_svc` 与双列表 `param_grid`（[L334-L343](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L334-L343)）。
2. **计时跑三种搜索**（**示例代码**框架）：

   ```python
   import time
   searches = {
       'grid(720 fits)': GridSearchCV(pipe_svc, param_grid,
                                      scoring='accuracy', cv=10),
       'random(200 fits)': RandomizedSearchCV(pipe_svc, param_grid,
                                              scoring='accuracy', cv=10,
                                              n_iter=20, random_state=1, n_jobs=-1),
       'halving': HalvingRandomSearchCV(pipe_svc, param_grid,
                                        n_candidates='exhaust',
                                        resource='n_samples', factor=1.5,
                                        random_state=1, n_jobs=-1),
   }
   for name, s in searches.items():
       t0 = time.perf_counter()
       s.fit(X_train, y_train)
       dt = time.perf_counter() - t0
       print(f'{name}: {dt:.1f}s, cv分={s.best_score_:.4f}, '
             f'{s.best_params_}, 测试分={s.score(X_test, y_test):.3f}')
   ```
3. **汇总成表**：为每种方法记录「耗时、cv 分、最优参数、测试准确率」，与第 4.4.3 节的参考表对照（grid 0.9847/0.974，random 0.9738，halving 0.9676/0.965），并回答：
   - 耗时排序与 fit 次数排序一致吗？（注意 `n_jobs=-1` 并行会压缩墙钟时间差异）
   - 测试准确率上三者差距有多大？值得为最后的 0.01 多花 3 倍算力吗？
4. **收尾**：为表现最好的搜索流程跑一次 2×5 嵌套 CV（模仿 [L459-L467](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L459-L467)），把「选美分数」「测试分数」「嵌套分数」三个数字并排写在结论里，解释它们各自的含义与大小关系。
5. 具体耗时与并行下的数值**待本地验证**；三个参考分数已由 notebook 保存输出锚定。

## 6. 本讲小结

- **双列表 `param_grid`**：字典的列表为不同核函数定义独立搜索子空间，linear 核不搜 `gamma`，避免无意义组合；组合总数 8 + 64 = 72，`cv=10` 共 720 次 fit。
- **三件套**：`best_score_` 是训练集内部的选美分（带取 max 的乐观偏差），`best_params_` 是冠军组合，`best_estimator_` 是 `refit=True` 自动在全量训练集重训的成品模型；`cv_results_` 是全部候选的成绩单。
- **随机搜索**用 `n_iter` 把预算与参数个数解耦（20 组合 × 10 折 = 200 次 fit，约 28% 算力）；值可以是列表（离散抽）也可以是 `scipy.stats.loguniform`（连续抽，每个数量级机会均等，适配跨 7 个数量级的 `C`/`gamma`）。
- **减半搜索**是淘汰赛：`resource='n_samples'` + `factor=1.5`，每轮资源 ×1.5、幸存者 ÷1.5，`n_candidates='exhaust'` 首轮尽量多候选；省算力的代价是早期小样本评估可能误杀好候选。实验性 API 需 `enable_halving_search_cv` 开关。
- **嵌套交叉验证**把「选模型」关进内层（2 折 GridSearchCV）、「评流程」交给外层（`cross_val_score(gs, ..., cv=5)`），得到近乎无偏的算法流程性能：SVM 管线 0.974 ± 0.015 胜过决策树 0.934 ± 0.016；它比 `best_score_`（0.985）低的那 1 个百分点就是选择性偏差的度量。

## 7. 下一步学习建议

本讲把「训练集内部」的选模型与比模型讲完了，但一直用 `scoring='accuracy'` 打分。下一讲 u6-l3（混淆矩阵、指标体系与 ROC 曲线）将回到 [ch06/ch06.py:L485](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L485) 之后的代码，回答：在类别不平衡的医疗数据上，准确率为什么会骗人？precision/recall/F1 怎么算、怎么用 `make_scorer` 挂进本讲的搜索器（[L578-L595](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L578-L595) 正是把 F1 作为 scoring 的网格搜索）？ROC 曲线与 AUC 又如何刻画分类器在各阈值下的表现？建议先精读 [ch06/ch06.py:L498-L517](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L498-L517) 的混淆矩阵代码再进入下一讲。之后可带着「搜索器 + 自定义评分」的组合技巧进入第 7 章集成学习（u7-l1）。
