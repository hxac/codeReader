# u7-l1 多数投票集成从零实现

## 1. 本讲目标

学完本讲，你应该能够：

1. 用二项分布推导「多数投票集成的错误率」公式 `ensemble_error`，说清楚集成优于单个分类器的两个前提条件（成员好于随机猜测、成员错误相互独立）。
2. 逐行读懂 `ch07/ch07.py` 中手写的 `MajorityVoteClassifier`：组合（composite）模式如何聚合多个估计器的 `fit` / `predict` / `predict_proba`，`LabelEncoder` 与 `clone` 各自解决什么问题。
3. 理解 `fit` 中的两处参数校验（`vote` 取值、`weights` 长度），以及 `predict` 在 `probability` 模式下直接复用 `predict_proba` 从而保证两条预测路径一致的设计。
4. 复现书中的对比实验：用 `cross_val_score`（10 折、`scoring='roc_auc'`）比较三个基线模型与集成，再用测试集 ROC 曲线直观对比。

## 2. 前置知识

- **集成学习（ensemble learning）**：把多个不同模型的预测组合成一个最终预测。本讲关注最简单的一种——**多数投票（majority vote）**：每个成员模型投一票，得票最多的类别作为集成输出。
- **二项分布**：做 \( n \) 次相互独立、成功率同为 \( e \) 的伯努利试验，「成功」次数 \( K \) 服从二项分布，\( P(K=k)=\binom{n}{k}e^{k}(1-e)^{n-k} \)。本讲用「成功」表示「分类器分错」。
- **三个 numpy 积木**：`np.bincount`（对非负整数计数，支持 `weights` 加权）、`np.average`（沿指定轴做加权平均）、`np.argmax`（取最大值所在索引）。它们分别是「硬投票」与「软投票」的核心。
- **scikit-learn 估计器范式**（u3-l1、u6-l1 已讲）：所有估计器遵循 `fit` / `predict` / `score` 接口；`BaseEstimator` 提供 `get_params` / `set_params`，`ClassifierMixin` 提供基于准确率的 `score`；`Pipeline` 把缩放器与模型绑定，让交叉验证的每一折自动重新拟合缩放器，防止数据泄漏。
- **交叉验证与 ROC AUC**（u6-l1、u6-l3 已讲）：分层 K 折以「均值 ± 标准差」汇报性能；`roc_auc` 评分需要估计器提供 `predict_proba` 或 `decision_function`，度量的是与阈值无关的分数排序能力。
- **运行环境提醒**：`ch07/ch07.py` 顶部统一导入全章依赖（含 `xgboost`），所以在 Jupyter 中逐段运行本章 notebook 前，需要按 u1-l2 搭好完整环境（包括 xgboost）；本讲只用到该文件的前半部分。

## 3. 本讲源码地图

| 文件 / 代码段 | 作用 |
| --- | --- |
| [ch07/ch07.py:106-110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L106-L110) | `ensemble_error` 函数：二项分布计算集成错误率 |
| [ch07/ch07.py:122-147](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L122-L147) | 扫描基错误率 0~1 并绘制「集成误差 vs 基误差」曲线 |
| [ch07/ch07.py:158-176](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L158-L176) | 两块投票积木的演示：加权 `bincount` 与加权 `average` |
| [ch07/ch07.py:190-313](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L190-L313) | `MajorityVoteClassifier` 类：本讲主角 |
| [ch07/ch07.py:323-366](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L323-L366) | 鸢尾花两类子集准备、三个基线模型、10 折 CV |
| [ch07/ch07.py:371-385](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L371-L385) | 集成参与同一套 10 折 CV 的对比实验 |
| [ch07/ch07.py:395-425](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L395-L425) | 测试集 ROC 曲线对比（AUC 写进图例） |
| [ch07/ch07.py:497-516](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L497-L516) | 用 `GridSearchCV` 调集成内部成员的超参数 |
| ch07/ch07.ipynb | 同一内容的 notebook 形态，推荐在其中逐段做本讲实验（见 u1-l3 双轨工作流） |

外部依赖 `sklearn.base`（`BaseEstimator`、`ClassifierMixin`、`clone`、`_name_estimators`）是 scikit-learn 库的组成部分，不在本仓库内，讲解时只说明其行为。

## 4. 核心概念与源码讲解

### 4.1 集成为什么可能更强：ensemble_error 的二项分布推导

#### 4.1.1 概念说明

直觉：单个分类器会犯错，但如果几个**各自略好于随机猜测、且犯的错误互不相同**的分类器一起投票，那么「多数人同时错」的概率应当远小于单个人错的概率——就像让 11 位水平中等的评委打分，只要他们不是集体犯同样的错，多数票通常更可靠。

这一直觉需要两个前提：

1. 每个成员的错误率 \( e < 0.5 \)（好于随机猜测）；
2. 成员之间的错误**相互独立**（不是同一个错误被复制 n 遍）。

`ensemble_error` 函数把这条直觉变成可以计算的公式，是全书集成学习章节的理论起点。

#### 4.1.2 核心流程

设集成有 \( n \) 个分类器，每个错误率同为 \( e \)，错误相互独立。把「第 \( i \) 个分类器分错」看作一次伯努利试验，则分错的个数 \( K \sim \text{Binomial}(n, e) \)。多数投票集成出错，当且仅当**至少一半以上**的成员同时分错：

\[
P_{\text{ens}}(n, e) \;=\; \sum_{k=\lceil n/2 \rceil}^{n} \binom{n}{k}\, e^{k}\, (1-e)^{n-k}
\]

计算流程（伪代码）：

```text
输入: n_classifier（成员数）, error（单个成员错误率 e）
1. k_start = ceil(n_classifier / 2)        # 触发集成出错的最少错误票数
2. 对 k = k_start, ..., n_classifier:
       累加 C(n, k) * e^k * (1-e)^(n-k)     # 恰好 k 个成员错的概率
3. 返回累加和
```

代入 \( n=11,\ e=0.25 \)：求和各项约为 \( 0.0268 + 0.0064 + 0.0011 + \cdots \approx 0.034 \)。也就是说，11 个错误率 25% 的独立分类器投票，集成错误率只有约 3.4%——误差被压缩了一个数量级。

#### 4.1.3 源码精读

[ch07/ch07.py:106-110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L106-L110) 定义了 `ensemble_error`：`k_start` 用 `math.ceil(n_classifier / 2.)` 取「过半」的最小整数（11 → 6）；`scipy.special.comb` 计算组合数 \( \binom{n}{k} \)；列表推导对 \( k \) 从 `k_start` 累加到 \( n \)，最后 `sum` 求和。注意 `n_classifier / 2.` 是浮点除法，配合 `ceil` 对奇偶数都适用（但偶数时会包含平局项，见练习 2）。

[ch07/ch07.py:115](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L115) 调用 `ensemble_error(n_classifier=11, error=0.25)`，即上面算出的约 0.034（待本地验证）。

[ch07/ch07.py:122-124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L122-L124) 让基错误率 `error` 从 0 到 1 每 0.01 取一个点，对每个点计算 11 成员集成的错误率，得到曲线数据。

[ch07/ch07.py:131-147](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L131-L147) 绘制两条线：实线是集成错误率，虚线是对角线 \( y=e \)（基错误率本身）。读图结论：

- \( e < 0.5 \) 的整段区间里，实线**远低于**虚线——集成放大了成员的优势；
- 两条线恰好在 \( e = 0.5 \) 处相交（奇数 \( n \) 时积分恰好为 0.5，见练习 1）；
- \( e > 0.5 \) 时实线反超虚线——成员系统性错的时候，投票会把错误也放大。这正是「成员必须好于随机」这一前提的图像化表达。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「成员越多、误差压得越低」，以及曲线在 0.5 处交叉。

**操作步骤**（示例代码，可在 Jupyter 中直接运行）：

```python
# 示例代码：复用书中 ensemble_error 的定义（ch07/ch07.py L106-110）
import numpy as np
import matplotlib.pyplot as plt

for n in [1, 5, 11, 25]:
    errs = [ensemble_error(n_classifier=n, error=e) for e in np.arange(0.0, 1.01, 0.01)]
    plt.plot(np.arange(0.0, 1.01, 0.01), errs, label=f'n={n}')
plt.plot([0, 1], [0, 1], 'k--', label='Base error')
plt.xlabel('Base error'); plt.ylabel('Ensemble error'); plt.legend(); plt.grid(alpha=0.5)
plt.show()

for n in [1, 5, 11, 25]:
    print(n, round(ensemble_error(n_classifier=n, error=0.25), 4))
```

**需要观察的现象**：\( n \) 越大，曲线在 \( e<0.5 \) 区间压得越低、在 \( e>0.5 \) 区间翘得越高，且所有曲线都过 (0.5, 0.5) 点。

**预期结果**：\( e=0.25 \) 时，\( n=1/5/11/25 \) 对应的集成错误率约为 0.25 / 0.10 / 0.034 / 0.002（待本地验证）。`n=1` 时「集成」退化为单分类器，曲线与对角线重合。

#### 4.1.5 小练习与答案

1. **问**：`ensemble_error(n_classifier=11, error=0.5)` 等于多少？为什么？
   **答**：恰好 0.5。\( n \) 为奇数时，由对称性 \( P(K \ge 6) = P(K \le 5) \)，两者之和为 1，所以各占一半。这就是曲线过 (0.5, 0.5) 的原因。
2. **问**：把 `n_classifier` 改成偶数 10，`k_start` 是多少？这会把哪种情况也算进「集成出错」？
   **答**：`ceil(10/2) = 5`，求和从 \( k=5 \) 开始，即 5:5 平局也被计入集成出错。这是偏保守的估计（实际实现里平局由 `argmax` 的取值规则打破，见 4.3）。
3. **问**：如果 11 个分类器犯的是**同一个**错（错误完全相关），集成错误率是多少？
   **答**：等于单成员错误率 \( e \) 本身——它们要么全对要么全错，投票不改变任何事。这说明独立性前提被破坏时，公式给出的误差压缩完全失效。

### 4.2 投票的两块积木：加权 bincount 与加权 average

#### 4.2.1 概念说明

`MajorityVoteClassifier` 的两种投票模式最终都落到两个 numpy 调用上。书中在定义类之前先用两个小例子把积木演示清楚，这是一种值得学习的写码习惯：先验证原子操作，再组装复杂逻辑。

- **硬投票（classlabel）**：收集每个成员的**类别预测**，按类别计数取众数——对应 `np.bincount` + `np.argmax`。
- **软投票（probability）**：收集每个成员的**类别概率**，加权平均后取最大——对应 `np.average` + `np.argmax`。

两者都支持 `weights`：可以给更可信的模型更大的话语权。

#### 4.2.2 核心流程

```text
硬投票（3 个成员对 1 个样本）:
  成员预测 = [0, 0, 1]，权重 = [0.2, 0.2, 0.6]
  bincount → 类 0 得 0.2+0.2=0.4 票，类 1 得 0.6 票
  argmax([0.4, 0.6]) → 1

软投票（3 个成员、2 类的概率矩阵，形状 (3 成员, 2 类)）:
  np.average(probas, axis=0, weights) → 每类的加权平均概率，形状 (2,)
  argmax → 概率最大的类
```

#### 4.2.3 源码精读

[ch07/ch07.py:158-159](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L158-L159) 演示硬投票：`np.bincount([0, 0, 1], weights=[0.2, 0.2, 0.6])` 返回 `[0.4, 0.6]`（每个类别槽位上的加权票数），套上 `np.argmax` 得到 1——虽然两个成员投类 0，但权重 0.6 的成员一票定音。

[ch07/ch07.py:164-171](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L164-L171) 构造 `ex`：3 个成员对 2 个类别的 `predict_proba` 输出，形状 `(3, 2)`；`np.average(ex, axis=0, weights=[0.2, 0.2, 0.6])` 沿「成员」轴加权平均，得到形状 `(2,)` 的平均概率。手工验算：类 0 列 \( 0.2\times0.9+0.2\times0.8+0.6\times0.4=0.58 \)，类 1 列 \( 0.2\times0.1+0.2\times0.2+0.6\times0.6=0.42 \)。

[ch07/ch07.py:176](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L176) 对 `[0.58, 0.42]` 取 `np.argmax` 得 0。注意与前一个例子的对照：同样的权重下，硬投票选类 1、软投票选类 0——软投票能利用概率中的「置信度」信息，两个只投类 0 的成员置信度都不低（0.9、0.8），合力压过了置信度 0.6 的类 1 成员。

#### 4.2.4 代码实践

**实践目标**：确认两个积木的输出与手算一致，并体会 weights 的作用。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
print(np.bincount([0, 0, 1], weights=[0.2, 0.2, 0.6]))   # 期望 [0.4 0.6]
ex = np.array([[0.9, 0.1], [0.8, 0.2], [0.4, 0.6]])
p = np.average(ex, axis=0, weights=[0.2, 0.2, 0.6])
print(p, np.argmax(p))                                    # 期望 [0.58 0.42] 0
# 把权重换成均匀的 [1/3, 1/3, 1/3] 再看软投票结果
print(np.average(ex, axis=0), np.argmax(np.average(ex, axis=0)))
```

**需要观察的现象**：加权与均匀权重下软投票的 `argmax` 是否改变。

**预期结果**：均匀权重时平均概率为 `[0.7, 0.3]` 附近（精确值 \( (0.9+0.8+0.4)/3 \approx 0.7 \)、\( (0.1+0.2+0.6)/3 = 0.3 \)），仍选类 0；权重是把「票」从类 0 挪向类 1 的旋钮，但 0.6 的权重不足以翻转软投票结果。

#### 4.2.5 小练习与答案

1. **问**：`np.bincount` 为什么要求输入是非负整数？这对后面的 `MajorityVoteClassifier.fit` 提出了什么要求？
   **答**：`bincount` 直接把输入值当作「计数槽位的索引」，槽位从 0 开始。因此传入的类别标签必须编码为 \( 0, 1, \dots, c-1 \)——这正是 `fit` 中要用 `LabelEncoder` 的原因（见 4.3.3）。
2. **问**：`np.average(ex, axis=0)` 与 `np.mean(ex, axis=0)` 的关系是什么？
   **答**：`average` 在权重全相等（或缺省均匀权重）时等价于 `mean`；`average` 额外接受 `weights` 参数做加权平均，是软投票支持模型权重的关键。
3. **问**：硬投票里权重 0.6 的成员「一票定音」，说明什么？
   **答**：权重本质上是把投票从「一人一票」变成「一股一票」；当某成员权重超过 0.5 时，它单独就能决定硬投票结果，集成的多样性优势随之消失。

### 4.3 MajorityVoteClassifier：组合模式的 fit / predict / predict_proba

#### 4.3.1 概念说明

`MajorityVoteClassifier` 是一个**手写的 scikit-learn 兼容估计器**，核心设计是**组合模式（composite pattern）**：它自己不实现任何学习算法，而是持有一组现成的估计器（`self.classifiers`），把 `fit` / `predict` / `predict_proba` 这些标准接口「转发+聚合」给每个成员：

- `fit`：逐个训练成员的**克隆副本**，存入 `self.classifiers_`；
- `predict`：收集成员预测，按 `vote` 参数走硬投票（bincount）或软投票（average）路径；
- `predict_proba`：对成员概率做加权平均；
- `get_params`：把成员的超参数展开成 `成员名__参数名` 形式，让 `GridSearchCV` 能直接调集成内部的超参数。

它继承了两个 sklearn 基类：`ClassifierMixin` 提供基于准确率的 `score` 方法，`BaseEstimator` 提供 `get_params` / `set_params`。这样整个集成对象就能无缝塞进 `cross_val_score`、`Pipeline`、`GridSearchCV` 等 sklearn 生态的任何位置——**只要遵守接口约定，自定义模型就能像官方模型一样被使用**。（讲义规格中提到的 `TransformerMixin` 并未出现在本章代码里：它用于需要 `transform` 方法的预处理器，本类是分类器而非变换器，不需要它。）

#### 4.3.2 核心流程

`fit(X, y)` 的流程：

```text
1. 校验 vote ∈ {'probability', 'classlabel'}，否则 ValueError
2. 校验 weights 长度 == classifiers 数量，否则 ValueError
3. LabelEncoder 编码 y → 标签变为 0..c-1（bincount/argmax 需要）
4. 对每个成员 clf:
       fitted = clone(clf).fit(X, 编码后的 y)   # 克隆副本，不动原始对象
       存入 self.classifiers_
5. 返回 self
```

`predict(X)` 的两条路径：

```text
vote='probability'（软投票）:
    maj_vote = argmax( self.predict_proba(X), axis=1 )     # 复用 predict_proba

vote='classlabel'（硬投票）:
    predictions = [clf.predict(X) for clf in classifiers_].T   # (n样本, n成员)
    对每一行: argmax( bincount(该行, weights=weights) )          # 加权众数
最后 lablenc_.inverse_transform(maj_vote) 把 0..c-1 还原成原始标签
```

`predict_proba(X)`：`probas` 形状为 `(n成员, n样本, n类别)`，沿成员轴 `np.average(..., axis=0, weights=weights)` 得到 `(n样本, n类别)`。

`predict` 在软投票路径下**直接调用 `predict_proba`**，保证两条预测路径对同一输入永远给出一致的结果——这就是两个方法之间天然的「一致性检查」；而 `fit` 开头的两个 `ValueError` 则是构造期的参数合法性检查，把错误拦截在训练之前。

#### 4.3.3 源码精读

**类声明与基类顺序**。[ch07/ch07.py:182-190](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L182-L190)：先根据 sklearn 版本选好基类元组 `base_classes`（0.16 及以上把 `ClassifierMixin` 放在前面；代码注释指出这是与读者讨论 [#205](https://github.com/rasbt/machine-learning-book/discussions/205) 确认的兼容性修正——Python 多继承按 MRO 从左到右解析方法，基类顺序影响继承方法的优先级），再用 `class MajorityVoteClassifier(*base_classes):` 的星号解包语法动态继承。类文档字符串 [ch07/ch07.py:191-208](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L191-L208) 完整说明了三个参数：`classifiers`（成员列表）、`vote`（默认 `'classlabel'` 硬投票；`'probability'` 为概率平均的软投票，推荐用于概率校准良好的成员）、`weights`（默认 `None` 即均匀权重）。

**`__init__`**。[ch07/ch07.py:209-215](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L209-L215)：除了保存三个参数，还用 sklearn 的 `_name_estimators` 给每个成员生成唯一名字存入 `self.named_classifiers`（小写类名，重复类型加 `-1`、`-2` 后缀），供 `get_params` 与网格调参使用。

**`fit`：校验、编码、克隆**。[ch07/ch07.py:233-240](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L233-L240) 是两处参数检查：`vote` 取值不合法、`weights` 长度与成员数不符都立即抛 `ValueError`——快速失败优于训练到一半才崩溃。[ch07/ch07.py:242-250](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L242-L250)：`LabelEncoder` 把 `y` 编码成从 0 开始的整数（注释明确说明这是 `predict` 里 `np.argmax` 的前提），编码后的类别表存入 `self.classes_`；随后循环 `clone(clf).fit(X, self.lablenc_.transform(y))`。`clone` 生成一个**全新的、未拟合的**副本，作用有二：一是 `cross_val_score` 会对同一估计器反复 `fit` 10 次，克隆保证每次都从干净状态开始；二是不污染调用方传入的原始对象。

**`predict`：两条投票路径**。[ch07/ch07.py:267-268](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L267-L268) 是软投票路径，一行 `np.argmax(self.predict_proba(X), axis=1)` 直接复用概率方法；[ch07/ch07.py:269-280](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L269-L280) 是硬投票路径：列表推导收集各成员的 `predict` 结果后 `.T` 转置成 `(n样本, n成员)`，再 `np.apply_along_axis` 对每行套用 4.2 节的 `argmax(bincount(x, weights=self.weights))`。[ch07/ch07.py:281-282](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L281-L282) 用 `lablenc_.inverse_transform` 把 0/1 索引还原为原始标签后返回。

**`predict_proba`**。[ch07/ch07.py:299-302](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L299-L302)：收集每个成员的 `predict_proba` 得到三维数组，`np.average(..., axis=0, weights=self.weights)` 沿成员轴加权平均。返回矩阵每行对应一个样本、每列一个类别的平均概率——这也让整个集成天然支持 `roc_auc` 评分（见 4.4）。

**`get_params`**。[ch07/ch07.py:304-313](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L304-L313)：重写基类方法，先放入「成员名 → 成员对象」映射，再递归展开每个成员自己的参数为 `成员名__参数名` 键。这正是 [ch07/ch07.py:497-504](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L497-L504) 里网格 `'decisiontreeclassifier__max_depth': [1, 2]`、`'pipeline-1__clf__C': [0.001, 0.1, 100.0]` 能直接调到**集成内部成员**超参数的原因——与 u6-l1 讲过的 Pipeline 双下划线参数命名一脉相承。

#### 4.3.4 代码实践

**实践目标**：在真实小数据上验证两种投票路径的输出一致性与标签还原。

**操作步骤**（示例代码，前提是已在 notebook 中运行到 `X_train, y_train` 可用、`MajorityVoteClassifier` 已定义，即 ch07.ipynb 相应小节）：

```python
# 示例代码
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.neighbors import KNeighborsClassifier

pipe1 = Pipeline([['sc', StandardScaler()],
                  ['clf', LogisticRegression(penalty='l2', C=0.001,
                                             solver='lbfgs', random_state=1)]])
clf2 = DecisionTreeClassifier(max_depth=1, criterion='entropy', random_state=0)
pipe3 = Pipeline([['sc', StandardScaler()],
                  ['clf', KNeighborsClassifier(n_neighbors=1, p=2,
                                               metric='minkowski')]])

mv_hard = MajorityVoteClassifier(classifiers=[pipe1, clf2, pipe3],
                                 vote='classlabel').fit(X_train, y_train)
mv_soft = MajorityVoteClassifier(classifiers=[pipe1, clf2, pipe3],
                                 vote='probability').fit(X_train, y_train)

print(mv_hard.predict(X_train[:10]))
print(mv_soft.predict(X_train[:10]))
print(mv_hard.predict_proba(X_train[:3]))      # 每行和应为 1
print(mv_hard.lablenc_.classes_)               # 编码表
```

**需要观察的现象**：两种 `vote` 模式对同样 10 个样本的预测是否完全相同；`predict_proba` 每行之和是否为 1。

**预期结果**：本数据集上两种模式通常给出一致或几乎一致的预测（待本地验证）；`predict_proba` 每行和为 1（成员概率行和为 1，凸组合仍为 1）。

#### 4.3.5 小练习与答案

1. **问**：`fit` 里为什么用 `clone(clf).fit(...)` 而不是直接 `clf.fit(...)`？
   **答**：`clone` 产生结构与超参数相同但未拟合的新对象。直接 `fit` 原对象会导致：`cross_val_score` 十次重复拟合共享同一状态、以及调用方传入的估计器被意外改动；克隆让每次拟合都从干净初始状态开始，也保证了原始对象不被污染。
2. **问**：如果不用 `LabelEncoder`，而标签本身是 1 和 2，硬投票路径会发生什么？
   **答**：`np.bincount` 会开出 0、1、2 三个槽位，第 0 槽恒为 0；若两类的加权票数相同，`argmax` 会错误地选中从未出现的类 0；即使不撞上平局，`inverse_transform` 一步也无从谈起。所以「标签从 0 开始」是这套 bincount/argmax 实现的硬前提。
3. **问**：4 个成员、硬投票出现 2:2 平局时，`predict` 返回什么？
   **答**：`np.bincount` 得到如 `[2, 2]`，`np.argmax` 返回**第一个**最大值的索引 0，即平局偏向编码值较小的类别。这一细节对下一节实践里「加入第四个成员」的实验很重要。
4. **问**：`vote='probability'` 模式对成员有什么额外要求？
   **答**：所有成员都必须实现 `predict_proba`（例如 `SVC` 默认 `probability=False` 时就没有）；且文档字符串提醒该模式推荐用于概率校准良好的分类器——未校准的概率平均后可能误导排序。

### 4.4 用 10 折交叉验证与 ROC 曲线对比基线与集成

#### 4.4.1 概念说明

理论（4.1）说集成可能更强，**是否真的更强必须用实验回答**。书中的实验设计分两层：

1. **训练集上的 10 折交叉验证**（`cross_val_score`，`scoring='roc_auc'`）：对三个基线模型与集成各报「均值 ± 标准差」，这是模型选择阶段的公平比较；
2. **测试集上的 ROC 曲线**：四个估计器各自 `fit` 训练集后在同一份测试集上画 ROC，AUC 写进图例，直观展示阈值无关的排序能力（承接 u6-l3 的 ROC 三步法）。

数据用的是鸢尾花的两类子集（versicolor 与 virginica），只用两个特征，方便画决策区域图。三个基线模型刻意选择了**异质**的算法：逻辑回归（线性模型）、深度为 1 的决策树（树模型）、1 近邻（惰性模型）——多样性正是 4.1 节独立性前提在实践中的近似。

#### 4.4.2 核心流程

```text
1. 取 iris 后 100 个样本（两类），特征 = [萼片宽度, 花瓣长度]
   LabelEncoder 把标签 1/2 → 0/1；50/50 分层切分训练/测试集
2. 构造三个成员：
   pipe1 = StandardScaler + LogisticRegression(C=0.001)   # 缩放飞入管道
   clf2  = DecisionTreeClassifier(max_depth=1, entropy)
   pipe3 = StandardScaler + KNeighborsClassifier(n_neighbors=1, p=2)
3. 对 [pipe1, clf2, pipe3] 逐个 10 折 CV（roc_auc）→ 基线成绩
4. mv_clf = MajorityVoteClassifier([pipe1, clf2, pipe3])
   对 [pipe1, clf2, pipe3, mv_clf] 再跑同一套 10 折 CV → 对比
5. 测试集 ROC：每个估计器 fit(训练集) → predict_proba(测试集)[:, 1]
   → roc_curve → auc → 画线，图例标注 AUC
```

注意成员中需要标准化的两个都封装在 `Pipeline` 里：交叉验证的每一折，缩放器只在该折的训练部分重新 `fit`——这是 u6-l1 讲过的防泄漏机制在集成场景的延续。而集成对象本身不需要标准化（它的输入原样传给各成员管道）。

#### 4.4.3 源码精读

**数据准备**。[ch07/ch07.py:323-331](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L323-L331)：`iris.data[50:, [1, 2]]` 取第 50 行起（versicolor+virginica 各 50）的第 1、2 列特征（萼片宽度、花瓣长度）；`LabelEncoder` 把标签 1/2 变成 0/1；`train_test_split(test_size=0.5, random_state=1, stratify=y)` 做对半分层划分——训练集与测试集各 50 个样本。

**三个成员**。[ch07/ch07.py:338-354](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L338-L354)：逻辑回归 `C=0.001`（强正则，u3-l1 讲过 C 与正则化强度互为倒数）、决策树只允许一次切分（`max_depth=1`，即决策树桩）、KNN 取 1 近邻欧氏距离（`p=2, metric='minkowski'`，u3-l3 讲过）；两个需要量纲统一的成员分别包进含 `StandardScaler` 的管道。

**基线 CV**。[ch07/ch07.py:356-366](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L356-L366)：循环三个基线，`cross_val_score(estimator=clf, X=X_train, y=y_train, cv=10, scoring='roc_auc')`，打印 `均值 (+/- 标准差)`。书中报告的典型输出约为：逻辑回归 0.92 (±0.15)、决策树 0.87 (±0.15)、KNN 0.85 (±0.15)（待本地验证）。

**集成加入对比**。[ch07/ch07.py:371-385](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L371-L385)：`mv_clf = MajorityVoteClassifier(classifiers=[pipe1, clf2, pipe3])` 用默认硬投票构造集成；然后把 `mv_clf` 追加进 `all_clf` 与 `clf_labels`，用**完全相同**的 CV 设置重跑循环。书中集成的典型成绩约为 0.98 (±0.05)（待本地验证）——均值超过所有基线且标准差更小，正是 4.1 节理论预测的「误差压缩」。`cross_val_score` 之所以能直接接受 `mv_clf`，是因为它实现了 `fit` / `predict_proba`（`roc_auc` 评分取概率列）；每一折内部，集成又被 `clone` 后重新 `fit`，成员管道里的缩放器随之在折内重拟合。

**测试集 ROC 对比**。[ch07/ch07.py:395-425](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L395-L425)：对四个估计器统一执行 `fit(X_train, y_train).predict_proba(X_test)[:, 1]`（注释说明假设正类是标签 1——这正是 u6-l3 强调的正类方向问题），经 `roc_curve` 得到 FPR/TPR 序列、`auc` 算曲线下面积，用不同颜色与线型画在一张图上，图例写成 `名称 (auc = X.XX)`，并叠加灰色对角随机基线。

**延伸：调集成的内部超参数**。[ch07/ch07.py:497-516](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L497-L516)：得益于 4.3.3 讲的自定义 `get_params`，`GridSearchCV` 可以用 `'decisiontreeclassifier__max_depth'` 与 `'pipeline-1__clf__C'` 这样的键直接搜索成员超参数（u6-l2 的方法论在集成上的应用），`cv=10, scoring='roc_auc'`，最后打印 `best_params_` 与 `best_score_`。

#### 4.4.4 代码实践

**实践目标**：复现书中 10 折 CV 对比输出，确认集成优于各基线。

**操作步骤**：在 Jupyter 中按顺序运行 ch07.ipynb 从「Combining classifiers via majority vote」到「Evaluating and tuning the ensemble classifier」之前的各单元（或运行 ch07.py 的对应段落），观察两轮 CV 的打印输出。也可以把两轮输出收集成字典后手动画条形图（完整代码见第 5 节综合实践）。

**需要观察的现象**：第二轮循环里新增的 `[Majority voting]` 一行的均值与标准差，与三个基线相比如何。

**预期结果**：按书中结果，集成 ROC AUC 均值最高（约 0.98）、波动最小（约 ±0.05）（待本地验证）。若本地数值略有出入属正常（库版本差异），关注的是相对关系。

#### 4.4.5 小练习与答案

1. **问**：为什么用 `scoring='roc_auc'` 而不是默认的准确率？
   **答**：承接 u6-l3——AUC 度量与阈值无关的分数排序能力，对阈值选择和类别比例更稳健；代价是要求估计器提供 `predict_proba` 或 `decision_function`，而 `MajorityVoteClassifier` 恰好实现了前者。
2. **问**：`cross_val_score` 作用在 `mv_clf` 上时，每一折内部发生了什么？
   **答**：先 `clone` 整个集成（连同三个成员一起复制成未拟合状态），在 9 折数据上调用集成的 `fit`（内部再对每个成员 `clone` 并拟合，缩放器在各折训练部分内重新学习参数），然后在留出折上调用 `predict_proba` 计算 ROC AUC。全程无数据泄漏。
3. **问**：ROC 对比代码里 `predict_proba(X_test)[:, 1]` 的 `[:, 1]` 在取什么？为什么是 1？
   **答**：取概率矩阵的第二列，即「样本属于正类（编码标签 1）的概率」作为 ROC 的分数。因为二分类 ROC 需要一个正类方向的分数；本数据标签经编码后 0/1，代码注释明确假设正类是 1——若关心的类别方向相反，必须换列（u6-l3 的 `pos_label` 教训）。
4. **问**：三个基线模型为什么选「逻辑回归 + 树桩 + 1NN」这样异质的组合，而不是三个逻辑回归？
   **答**：4.1 节的独立性前提要求成员错误互不相同。同类模型的错误高度相关（相当于复制同一个错），投票无法压缩误差；异质模型在不同区域犯错，才最接近「独立投票」的理想设定。

## 5. 综合实践

**任务**：给 `MajorityVoteClassifier` 加入第四个成员 `KNeighborsClassifier(p=2, n_neighbors=1)`，把书中以文本打印的 10 折交叉验证对比重画成**条形图（带误差棒）**，并回答：集成分数提升了吗？为什么？

这个实验设计得很巧妙：第四个成员与 `pipe3` 配置完全相同，等于**把同一个模型数了两遍**——它直接检验 4.1 节的独立性前提：错误高度相关的成员不应带来提升，甚至可能因为「偶数成员 + 平局规则」引入噪声。

**操作步骤**（示例代码，在已运行 ch07.ipynb 相应小节、`MajorityVoteClassifier` / `pipe1` / `clf2` / `pipe3` / `X_train` / `y_train` 就绪后执行）：

```python
# 示例代码：第四个成员（与 pipe3 同配置的 1NN）
import numpy as np
import matplotlib.pyplot as plt
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score

clf4 = KNeighborsClassifier(n_neighbors=1, p=2, metric='minkowski')
pipe4 = Pipeline([['sc', StandardScaler()], ['clf', clf4]])

mv_clf4 = MajorityVoteClassifier(classifiers=[pipe1, clf2, pipe3, pipe4])

labels = ['Logistic\nregression', 'Decision\ntree', 'KNN',
          'Majority vote\n(3 members)', 'Majority vote\n(4 members)']
all_clf = [pipe1, clf2, pipe3, mv_clf, mv_clf4]

means, stds = [], []
for clf, label in zip(all_clf, labels):
    scores = cross_val_score(estimator=clf, X=X_train, y=y_train,
                             cv=10, scoring='roc_auc')
    means.append(scores.mean()); stds.append(scores.std())
    print(f'ROC AUC: {scores.mean():.2f} ({scores.std():.2f}) [{label}]')

x = np.arange(len(labels))
plt.bar(x, means, yerr=stds, capsize=6,
        color='lightgray', edgecolor='black')
plt.xticks(x, labels)
plt.ylabel('ROC AUC (10-fold CV)')
plt.ylim(0.5, 1.05)
plt.show()
```

**需要观察的现象**：

1. 「4 成员集成」的均值与标准差相对「3 成员集成」和三个基线的变化幅度；
2. `mv_clf4` 的成员命名：因为多了第二个 `Pipeline` 与第二个 `KNeighborsClassifier`，`_name_estimators` 会生成带 `-1`/`-2` 后缀的名字（可用 `mv_clf4.get_params()` 查看）；
3. （可选延伸）把 `vote` 换成 `'probability'` 再跑一次，观察偶数成员下软投票是否比硬投票更稳。

**预期结果与解释**（待本地验证）：4 成员集成的分数大概率与 3 成员集成**大体持平或仅有微小变化**，而不会像从 1 个模型到 3 个异质模型那样跳升。原因有二：其一，重复的 1NN 与原 1NN 在同样输入上给出完全相同的预测，错误完全相关，违反 4.1 节推导的独立性假设——公式中的误差压缩对「复制成员」无效；其二，成员数从奇数 3 变为偶数 4 后，硬投票可能出现 2:2 平局，`np.argmax(np.bincount(...))` 会偏向编码较小的类（见 4.3.5 练习 3），等价于给某些样本加了系统性偏置。若把第四个成员换成真正不同的模型（例如 `KNeighborsClassifier(n_neighbors=5)` 或线性 SVC），提升的可能性更大——「集成收益来自多样性，而不是成员数量」。

## 6. 本讲小结

- 集成误差公式 \( P_{\text{ens}}=\sum_{k=\lceil n/2\rceil}^{n}\binom{n}{k}e^{k}(1-e)^{n-k} \) 表明：只要成员好于随机（\( e<0.5 \)）且错误相互独立，多数投票就能指数级压缩误差（11 个 25% 错误率的成员 → 集成约 3.4%）；\( e>0.5 \) 时投票反而放大错误。
- `MajorityVoteClassifier` 是组合模式的 sklearn 兼容估计器：`fit` 里 `clone` 每个成员再训练（可重复拟合、不污染原对象），`LabelEncoder` 保证标签从 0 开始以适配 `bincount`/`argmax`，预测前再 `inverse_transform` 还原标签。
- 两种投票路径：硬投票 `argmax(bincount(成员预测, weights))`、软投票 `argmax(加权平均概率)`；`predict` 的软路径直接复用 `predict_proba`，保证两法一致；`fit` 开头对 `vote` 与 `weights` 做快速失败校验。
- 自定义 `get_params` 把成员参数展开为 `成员名__参数名`，使 `GridSearchCV` 能直接调集成内部超参数；`ClassifierMixin`/`BaseEstimator` 混入让集成可进 `cross_val_score`、`Pipeline` 等全部 sklearn 设施。
- 对比实验用「训练集 10 折 CV（roc_auc）+ 测试集 ROC 曲线」双层设计：书中集成均值最高、波动最小；成员管道内的缩放器在每折内重拟合，避免泄漏。
- 综合实践表明：加入与现有成员完全相同的模型不会带来提升——集成收益来自**多样性（错误独立性）**而非成员数量；偶数成员还会引入平局偏向问题。

## 7. 下一步学习建议

- **下一讲 u7-l2（Bagging 与 Boosting 家族）**：多数投票靠「人工挑选异质模型」获得多样性，bagging 则用 bootstrap 重采样自动制造多样性（`BaggingClassifier` 的 `oob_score_`），boosting 更进一步用加权样本序列逼着后续模型纠正前面的错误（AdaBoost 的 SAMME、XGBoost）。读完本讲再读下一讲，会明显感觉「集成三流派」的谱系关系。
- **回看本讲源码**：[ch07/ch07.py:497-516](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L497-L516) 的网格调参部分值得自己跑一遍，把 `param_grid` 换成搜索 `kneighborsclassifier__n_neighbors`（注意重名成员的 `-1`/`-2` 后缀），体会自定义 `get_params` 的威力。
- **扩展阅读**：scikit-learn 内置的 `sklearn.ensemble.VotingClassifier` 是本讲手写类的「工业版」，支持同样的 `voting='hard'/'soft'` 与 `weights` 参数，可对照阅读其文档，验证你对手写实现的理解。
