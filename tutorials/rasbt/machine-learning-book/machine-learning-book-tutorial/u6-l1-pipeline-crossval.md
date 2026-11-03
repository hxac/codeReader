# u6-l1 Pipeline、交叉验证与学习/验证曲线

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解为什么要把「预处理 + 模型」绑成一条 `Pipeline`，以及它如何在交叉验证中从机制上防止测试集（验证折）信息泄漏。
2. 看懂并使用 `StratifiedKFold` 分层 K 折交叉验证：既能手写切折循环，也能用 `cross_val_score` 一行完成，并知道 `n_jobs` 参数如何并行加速。
3. 用 `learning_curve`（学习曲线）诊断模型是高偏差（欠拟合）还是高方差（过拟合）。
4. 用 `validation_curve`（验证曲线）扫描单个超参数（如逻辑回归的 `C`），并根据曲线形态找出「甜蜜点」。

本讲全部源码来自 `ch06/ch06.py` 的前三节（管线、交叉验证、曲线诊断），后面的网格搜索与评估指标留给 u6-l2、u6-l3。

## 2. 前置知识

本讲默认你已读过前面几讲的概念，先快速回顾并补充三个新概念。

**已学过的（简要回顾）：**

- **估计器范式**（u3-l1）：scikit-learn 的模型统一走 `fit` / `predict` / `score` 接口，换模型只换类名。
- **转换器范式**（u2-l3、u4-l1、u5-l1）：`StandardScaler` 这类预处理工具有 `fit_transform` / `transform` 之分——统计参数（均值、标准差）**只能在训练集上估计**，新数据只做 `transform`，否则会造成「数据泄漏」（data leakage）：测试集的信息经由预处理参数混进了模型。
- **`C` 的语义**（u3-l1）：逻辑回归默认 L2 正则化，`C` 与正则化强度互为倒数——`C` 越小正则化越强、权重收缩越狠；`C` 越大正则化越弱。
- **双下划线参数名**（u5-l2）：`'logisticregression__C'` 这种「步骤名__参数名」的写法用于指定管线中某一步的超参数，本讲的验证曲线会再次用到。

**本讲新增的三个概念：**

1. **留出法（holdout）的局限**。我们之前一直用 `train_test_split` 划出一份测试集，再把训练集再切一小块当验证集来调参。问题：模型表现的好坏依赖于「恰好」落进验证集的那几个样本——换一个随机种子，验证分数就会波动。当数据量不大时，这种波动足以误导调参方向。
2. **K 折交叉验证（k-fold cross-validation）**。把训练集（注意：全程不动测试集）无重叠地切成 \( k \) 份，轮流用其中 1 份当验证折、其余 \( k-1 \) 份训练，得到 \( k \) 个分数，用它们的均值与标准差汇报：

   \[ \mathrm{CV} = \frac{1}{k}\sum_{i=1}^{k} \mathrm{score}_i, \qquad \bar{s} \pm \sigma_s \]

   「分层（stratified）」指每折中**类别比例与整体一致**。乳腺癌数据良/恶性比例约 6:4，若不分层，随机切折可能某折里恶性样本特别少，让该折分数失真。
3. **偏差—方差分解（bias–variance decomposition）**。模型期望误差可粗略分解为：

   \[ \mathrm{Err} = \mathrm{bias}^2 + \mathrm{variance} + \mathrm{noise} \]

   - **高偏差（欠拟合）**：模型太简单，训练分数和验证分数**都低**且互相贴近——再多数据也救不了，要换更强的模型或减正则化。
   - **高方差（过拟合）**：模型把训练集的噪声也背下来了，训练分数很高、验证分数明显更低，两者之间**间隙（gap）大**——要加正则化、降复杂度或加数据。

   学习曲线和验证曲线就是分别在「样本数」和「超参数」两个坐标轴上看这个间隙的工具。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch06/ch06.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py) | 第 6 章全部代码的脚本版（本讲覆盖 L93–L322：数据载入、Pipeline、交叉验证、两条曲线） |
| [ch06/wdbc.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/wdbc.data) | 威斯康星乳腺癌数据集的本地副本（569 行、无表头、逗号分隔），实践时可离线加载 |
| [ch06/ch06.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.ipynb) | 等价 notebook 版，内含各单元格的真实运行输出，本讲引用的数值即来自这里 |
| [ch06/figures/06_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_05.png) | 书中学习曲线配图（训练/验证准确率 vs 训练样本数） |
| [ch06/figures/06_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_06.png) | 书中验证曲线配图（训练/验证准确率 vs 参数 C） |

数据集背景：Breast Cancer Wisconsin（Diagnostic），共 569 个样本、32 列——第 0 列是 ID，第 1 列是诊断结果（`M` 恶性 / `B` 良性），第 2–31 列是从细胞核图像算出的 30 个实数特征（半径、纹理、周长等）。其中良性 357 例、恶性 212 例。

## 4. 核心概念与源码讲解

### 4.1 乳腺癌数据集与 Pipeline 工作流

#### 4.1.1 概念说明

到上一讲为止，我们的流程都是「手动三步」：先 `StandardScaler().fit_transform(X_train)`，再 `sc.transform(X_test)`，最后训练模型。这种方式有两个痛点：

1. **繁琐**：每换一个模型、每跑一次评估，都要重复一遍预处理调用。
2. **危险**：一旦进入交叉验证，每一折都要**重新**在「该折的训练部分」上拟合缩放器。若贪图省事对整个 `X_train` 先缩放再切折，验证折的均值/方差信息就泄漏进了训练过程——这正是 u5-l1 里「PCA 只能在训练集拟合」原则的加强版。

`Pipeline` 把若干转换器和一个估计器绑成**一个**估计器：对它调 `fit`，内部自动按顺序对每一步 `fit_transform`、最后一步只 `fit`；对它调 `predict`/`score`，内部自动依次 `transform` 再预测。于是「切折 → 缩放 → 训练 → 评分」的每折全过程被原子化，泄漏无从发生。

#### 4.1.2 核心流程

```text
数据读入 wdbc.data（无表头）
    ↓
X = 第 2 列之后的 30 个特征；y = 第 1 列诊断字母
    ↓ LabelEncoder：B→0，M→1（按字母序）
train_test_split(test_size=0.20, stratify=y, random_state=1)
    ↓ 455 个训练样本 / 114 个测试样本
make_pipeline(StandardScaler(), PCA(n_components=2), LogisticRegression())
    ↓ pipe_lr.fit(X_train)  →  scaler.fit_transform → pca.fit_transform → lr.fit
    ↓ pipe_lr.score(X_test) →  scaler.transform → pca.transform → lr.predict → 算准确率
```

`make_pipeline` 是便捷构造器：不需要给每步起名字，它自动用类名的小写形式做步骤名（`standardscaler`、`pca`、`logisticregression`）。等价的显式写法是 `Pipeline([('sc', StandardScaler()), ('pca', PCA(n_components=2)), ('clf', LogisticRegression())])`——步骤名自取，后续「步骤名__参数名」的引用要与这里一致。书中用 `make_pipeline`，故本讲统一采用自动命名。

#### 4.1.3 源码精读

载入数据（UCI 在线地址，无表头 CSV；注释里给出了本地路径的备用方案，仓库里就带有 [ch06/wdbc.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/wdbc.data)）：

- [ch06/ch06.py:L102-L104](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L102-L104) — 用 pandas `read_csv` 从 UCI 读取乳腺癌数据，`header=None` 表示首行即数据。
- [ch06/ch06.py:L106-L110](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L106-L110) — 注释说明：UCI 暂时不可用时改从本地 `wdbc.data` 读取。

特征与标签的切分、编码：

- [ch06/ch06.py:L124-L125](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L124-L125) — `X` 取第 2 列起的 30 个特征，`y` 取第 1 列诊断字母。
- [ch06/ch06.py:L126-L133](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L126-L133) — `LabelEncoder` 把字母标签变成整数；`le.classes_` 为 `['B' 'M']`（按字母序），故 **B→0、M→1**，恶性（M）是正类 1。这一映射在 u6-l3 讲混淆矩阵时会再次用到。

分层划分训练/测试集：

- [ch06/ch06.py:L139-L142](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L139-L142) — 80/20 分层划分（`stratify=y` 保持良/恶性比例），`random_state=1` 保证可复现。**这份测试集从现在起被「锁进保险柜」，本讲所有交叉验证只在 455 个训练样本上进行。**

构建并使用管线：

- [ch06/ch06.py:L151-L153](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L151-L153) — `make_pipeline(StandardScaler(), PCA(n_components=2), LogisticRegression())`：标准化 → 压到 2 个主成分 → 逻辑回归，三步绑成一个估计器 `pipe_lr`。
- [ch06/ch06.py:L155-L158](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L155-L158) — 对管线直接 `fit` / `predict` / `score`，用法与单个模型完全一样。仓库 notebook 中保存的输出为 `Test accuracy: 0.956`。

#### 4.1.4 代码实践

**实践目标**：亲手搭一条管线，体验「一次绑定、处处可用」，并确认本地数据可离线复现书中结果。

**操作步骤**（在仓库根目录下新建脚本或 notebook 运行，示例代码）：

```python
# 示例代码：等价于 ch06.py L102-L158，改用仓库自带的本地数据副本
import pandas as pd
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.decomposition import PCA
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline

df = pd.read_csv('ch06/wdbc.data', header=None)   # 本地副本，无需联网
X = df.loc[:, 2:].values
y = LabelEncoder().fit_transform(df.loc[:, 1].values)   # B->0, M->1

X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=1)

pipe_lr = make_pipeline(StandardScaler(),
                        PCA(n_components=2),
                        LogisticRegression())
pipe_lr.fit(X_train, y_train)
print(f'Test accuracy: {pipe_lr.score(X_test, y_test):.3f}')
```

**需要观察的现象**：`df.shape` 为 `(569, 32)`；管线一次 `fit` 就完成缩放、降维、训练三件事。

**预期结果**：`Test accuracy: 0.956`，与 [ch06.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.ipynb) 中保存的输出一致（同版本依赖下应逐位相同；不同 sklearn 版本可能有微小差异，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `make_pipeline` 换成显式的 `Pipeline([('sc', StandardScaler()), ('clf', LogisticRegression())])`（去掉 PCA），后面想对 `C` 画验证曲线，`param_name` 应该写什么？

**答案**：`'clf__C'`——双下划线前缀必须与显式步骤名 `clf` 一致；若沿用 `make_pipeline` 自动命名，则步骤名是 `logisticregression`，写成 `'logisticregression__C'`。

**练习 2**：为什么说「先对整个 `X_train` 做 `StandardScaler().fit_transform`，再手动切折做交叉验证」是错误的？

**答案**：因为每折的验证部分在切折之前就参与了均值/标准差的计算，它的统计信息经由缩放参数泄漏给了训练过程，会高估泛化分数。正确做法是把缩放器放进 `Pipeline`，让交叉验证在每个训练折上重新 `fit` 缩放器、只对验证折 `transform`。

**练习 3**：`pipe_lr.fit(X_train, y_train)` 时，`PCA` 收到的是原始特征还是标准化后的特征？

**答案**：标准化后的。管线 `fit` 时前面的步骤依次 `fit_transform`，输出作为下一步的输入；这也呼应了 u5-l1 的结论——PCA 之前必须标准化，否则量纲大的特征会主导主成分。

### 4.2 StratifiedKFold 分层交叉验证与 cross_val_score

#### 4.2.1 概念说明

4.1 节的 `Test accuracy: 0.956` 是单次留出评估，它只回答了「这一个 20% 测试集上表现如何」。想更稳健地估计模型性能，用 K 折交叉验证：`k=10` 时每个样本恰好当一次验证样本，得到 10 个分数。

**为什么必须「分层」**？训练集 455 个样本中良性约 285、恶性约 170（约 6.3 : 3.7）。若用普通 `KFold` 顺序切块，某些折的恶性占比可能明显偏离整体，导致个别折分数失真、波动虚高。`StratifiedKFold` 保证每折的类别比例与整体几乎一致——源码里那列 `Class distr.: [256 153]` 正是这一性质的直接证据。

`cross_val_score` 则是 K 折评估的「一行版」：传入估计器、数据、`cv=10`，返回长度为 10 的分数数组。它的 `n_jobs` 参数控制并行：`n_jobs=1` 单进程（书中默认，便于调试），`n_jobs=-1` 把 10 个折分派到全部 CPU 核心同时训练，折与折之间彼此独立、天然可并行。

#### 4.2.2 核心流程

手写分层 K 折的伪代码：

```text
kfold = StratifiedKFold(n_splits=10).split(X_train, y_train)
对每一折 (train_idx, valid_idx):
    pipe_lr.fit(X_train[train_idx], y_train[train_idx])   # 管线内部重新 fit 缩放器
    score = pipe_lr.score(X_train[valid_idx], y_train[valid_idx])
    记录 score，并打印 np.bincount(y_train[train_idx]) 观察类分布
汇总：mean(scores) ± std(scores)
```

`cross_val_score` 做的就是这件事的封装：

```text
scores = cross_val_score(estimator, X, y, cv=10, n_jobs=1)
# 对分类器 + 整数 cv，sklearn 内部自动选用 StratifiedKFold
```

#### 4.2.3 源码精读

**手写版**（教学目的：看清每一折发生了什么）：

- [ch06/ch06.py:L187](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L187) — 创建 10 折分层切分器并对 `X_train` 做 `split`，得到 (训练下标, 验证下标) 的生成器。
- [ch06/ch06.py:L189-L197](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L189-L197) — 循环体：每折用管线重新 `fit`（缩放器在训练折上重新拟合），在验证折上 `score`；`np.bincount` 打印该折训练部分的类分布，`f'Fold: {k+1:02d}'` 把折号格式化成两位数字。
- [ch06/ch06.py:L199-L201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L199-L201) — 用 numpy 汇总：`np.mean(scores)` 与 `np.std(scores)`。

**封装版**（工程实践首选）：

- [ch06/ch06.py:L207-L211](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L207-L211) — `cross_val_score(estimator=pipe_lr, X=X_train, y=y_train, cv=10, n_jobs=1)`：10 折评估一行完成；`n_jobs=1` 表示串行。
- [ch06/ch06.py:L212-L214](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L212-L214) — 打印 10 个分数数组和 `均值 +/- 标准差`。

仓库 [ch06.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.ipynb) 保存的真实输出（两段代码各运行一次）：

```text
Fold: 01, Class distr.: [256 153], Acc.: 0.935
Fold: 02, Class distr.: [256 153], Acc.: 0.935
...（中间各折 0.93~0.98）
Fold: 10, Class distr.: [257 153], Acc.: 0.956
CV accuracy: 0.950 +/- 0.014

CV accuracy scores: [0.93478261 0.93478261 0.95652174 0.95652174 0.93478261
 0.95555556 0.97777778 0.93333333 0.95555556 0.95555556]
CV accuracy: 0.950 +/- 0.014
```

三个值得注意的细节：

1. `Class distr.: [256 153]`（或 `[257 153]`）逐折几乎不变——这就是「分层」的可视化证据；256+153=409，即每折 9/10 的训练样本。
2. **两段代码给出完全相同的 10 个分数**（0.935 就是 0.93478261 的三位小数舍入）：`cross_val_score` 在「估计器是分类器 + `cv` 传整数」时内部自动使用 `StratifiedKFold`，与手写版等价。
3. 汇报 `0.950 ± 0.014` 比「单次 holdout 的某个数」信息量大得多：既给出了中心估计，也给出了对划分方式敏感程度的度量。

#### 4.2.4 代码实践

**实践目标**：亲手跑通「手写分层循环」与「`cross_val_score`」两个版本，验证二者等价，并体验 `n_jobs` 的并行效果。

**操作步骤**（示例代码，接 4.1.4 的数据划分）：

```python
# 示例代码：对应 ch06.py L187-L214
import numpy as np
from sklearn.model_selection import StratifiedKFold, cross_val_score

# 版本 A：手写分层 K 折
kfold = StratifiedKFold(n_splits=10).split(X_train, y_train)
scores = []
for k, (train, valid) in enumerate(kfold):
    pipe_lr.fit(X_train[train], y_train[train])
    score = pipe_lr.score(X_train[valid], y_train[valid])
    scores.append(score)
    print(f'Fold: {k+1:02d}, Class distr.: {np.bincount(y_train[train])}, '
          f'Acc.: {score:.3f}')
print(f'\nCV accuracy: {np.mean(scores):.3f} +/- {np.std(scores):.3f}')

# 版本 B：cross_val_score（先试 n_jobs=1，再改成 -1 对比耗时）
scores = cross_val_score(estimator=pipe_lr, X=X_train, y=y_train,
                         cv=10, n_jobs=1)
print(f'CV accuracy: {np.mean(scores):.3f} +/- {np.std(scores):.3f}')
```

**需要观察的现象**：版本 A 打印的每折类分布是否恒为 `[256 153]`/`[257 153]`；版本 B 的 10 个分数是否与版本 A 一一对应相同；把 `n_jobs=1` 改为 `n_jobs=-1` 后用 `%timeit` 或 `time.time()` 计时，总耗时是否下降。

**预期结果**：两个版本均为 `CV accuracy: 0.950 +/- 0.014`（与 notebook 保存输出一致）。`n_jobs=-1` 的加速比与机器核数和数据规模有关（本数据集很小，加速有限甚至可能因进程启动开销变慢），具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`StratifiedKFold(n_splits=10)` 的每折验证部分有多少个样本？依据是什么？

**答案**：约 45 或 46 个。`X_train` 共 455 个样本（569 × 0.8），10 折无重叠划分，455 = 10 × 45 + 5，故 5 个折有 46 个验证样本、5 个折有 45 个；相应训练部分为 409 或 410，与输出中 `256 + 153 = 409` 吻合。

**练习 2**：如果把 `cross_val_score` 的 `estimator` 换成回归器、`cv=10` 保持整数，sklearn 内部还会用分层 K 折吗？

**答案**：不会。整数 `cv` 的规则是：估计器是分类器且 `y` 为二/多分类标签时用 `StratifiedKFold`，其余情况（含回归）用普通 `KFold`。回归任务如需复现实验，通常配合 `KFold(shuffle=True, random_state=...)` 自行传入。

**练习 3**：10 折 CV 得到 `0.950 ± 0.014`，能否据此说「模型在测试集上一定是 95.0%」？

**答案**：不能。CV 只是对泛化性能的估计，±0.014 是**折间**标准差而非置信区间；最终仍要用从未参与调参的测试集评估（如 4.1.3 中的 `Test accuracy: 0.956`，与 CV 估计接近但不必相等）。

### 4.3 learning_curve：用学习曲线诊断偏差与方差

#### 4.3.1 概念说明

学习曲线回答的问题是：**如果训练数据变多，模型会变好吗？** 横轴是参与训练的样本数，纵轴是准确率，画两条线：训练准确率与（交叉验证的）验证准确率。诊断表：

| 曲线形态 | 诊断 | 处方 |
| --- | --- | --- |
| 两条线都低、早早汇合、间隙小 | 高偏差（欠拟合） | 换更复杂模型、减弱正则化、加特征 |
| 训练线高、验证线低、间隙大 | 高方差（过拟合） | 收集更多数据、加强正则化、降复杂度 |
| 训练线随样本增多而下降、验证线上升，二者逐渐靠近 | 健康状态，小间隙即轻微过拟合 | 可接受，或继续微调 |

注意一个反直觉点：**训练准确率随样本增多往往下降**（样本少时模型更容易把训练集全背下来），而验证准确率上升——两线相向而行并收敛，正是学习曲线的常态。

#### 4.3.2 核心流程

```text
train_sizes = np.linspace(0.1, 1.0, 10)     # 10%, 20%, ..., 100% 共 10 档
learning_curve(pipe_lr, X_train, y_train, train_sizes=..., cv=10)
    ↓ 对每一档样本量 m：
        10 折 CV：用 m 个样本训练，在验证折上评分
        再对完整训练子集评分（训练准确率）
返回三件套：
    train_sizes  形状 (10,)
    train_scores 形状 (10, 10)   # 10 档 × 10 折
    test_scores  形状 (10, 10)   # 验证折分数
    ↓ axis=1 上求均值/标准差 → 每档两个数 ± 一条误差带
绘图：plt.plot 折线 + plt.fill_between 画 ±1 个标准差的半透明带
```

一个容易漏看的细节：**这里的 `pipe_lr` 被重建了**，去掉了上一节的 `PCA(n_components=2)`，只保留 `StandardScaler + LogisticRegression`，让逻辑回归直接用全部 30 个特征。

#### 4.3.3 源码精读

- [ch06/ch06.py:L231-L232](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L231-L232) — 重建管线：`make_pipeline(StandardScaler(), LogisticRegression(penalty='l2', max_iter=10000))`；`max_iter=10000` 放宽迭代上限，保证较难收敛的 lbfgs 优化能跑完。
- [ch06/ch06.py:L234-L239](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L234-L239) — 调 `learning_curve`：`train_sizes=np.linspace(0.1, 1.0, 10)` 生成 10 档样本量，`cv=10` 每档做 10 折，`n_jobs=1` 串行。
- [ch06/ch06.py:L241-L244](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L241-L244) — 沿 `axis=1`（折的方向）求均值和标准差，把 (10, 10) 矩阵压成每档 1 个中心值 + 1 个离散度。
- [ch06/ch06.py:L246-L263](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L246-L263) — 绘图：蓝色实线圆点为训练准确率、绿色虚线方点为验证准确率；`fill_between(均值+std, 均值-std, alpha=0.15)` 给两条线各加一条 ±1 标准差的半透明误差带，交叉验证的波动一目了然。
- [ch06/ch06.py:L265-L272](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L265-L272) — 加网格与坐标标签，`ylim([0.8, 1.03])` 放大关注区间，`plt.show()` 出图；被注释的 `savefig` 行是把图存成 [ch06/figures/06_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_05.png) 的语句。

对照书中配图 [06_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_05.png)：训练准确率（蓝）始终很高，验证准确率（绿）随样本数增加稳步抬升，两者逐渐靠近但末端仍留小间隙——**轻微过拟合**，模型整体表现已经不错，收集更多数据还能再赚一点。

#### 4.3.4 代码实践

**实践目标**：复现学习曲线，并故意制造一个「高偏差」版本作对照，学会用曲线形态下诊断。

**操作步骤**（示例代码）：

```python
# 示例代码：对应 ch06.py L231-L272 的绘图部分
import numpy as np
import matplotlib.pyplot as plt
from sklearn.model_selection import learning_curve
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

pipe_lr = make_pipeline(StandardScaler(),
                        LogisticRegression(penalty='l2', max_iter=10000))

train_sizes, train_scores, test_scores = learning_curve(
    estimator=pipe_lr, X=X_train, y=y_train,
    train_sizes=np.linspace(0.1, 1.0, 10), cv=10, n_jobs=1)

train_mean = np.mean(train_scores, axis=1)
train_std = np.std(train_scores, axis=1)
test_mean = np.mean(test_scores, axis=1)
test_std = np.std(test_scores, axis=1)

plt.plot(train_sizes, train_mean, color='blue', marker='o',
         markersize=5, label='Training accuracy')
plt.fill_between(train_sizes, train_mean + train_std,
                 train_mean - train_std, alpha=0.15, color='blue')
plt.plot(train_sizes, test_mean, color='green', linestyle='--',
         marker='s', markersize=5, label='Validation accuracy')
plt.fill_between(train_sizes, test_mean + test_std,
                 test_mean - test_std, alpha=0.15, color='green')
plt.grid(); plt.xlabel('Number of training examples')
plt.ylabel('Accuracy'); plt.legend(loc='lower right')
plt.ylim([0.8, 1.03]); plt.tight_layout(); plt.show()
```

对照实验：把 `LogisticRegression(penalty='l2', max_iter=10000)` 换成 `LogisticRegression(C=1e-4, max_iter=10000)`（超强正则化）再画一次。

**需要观察的现象**：原图中验证曲线是否随样本数单调上升、与训练曲线的间隙是否收窄；对照图中两条曲线是否**一起显著变低**且互相贴近。

**预期结果**：原图应与 [06_05.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_05.png) 形态一致（轻微过拟合的小间隙）；`C=1e-4` 的对照图呈典型高偏差形态（两线齐低、间隙小），具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`learning_curve` 返回的 `train_scores.shape` 是多少？为什么不是 `(10,)`？

**答案**：`(10, 10)`——10 档样本量 × 10 折交叉验证。每档样本量的性能不是一个数，而是 10 折的分布，所以绘图前要沿 `axis=1` 聚合。

**练习 2**：若学习曲线显示两条线已汇合但都停在 70% 准确率，给数据翻倍有帮助吗？该怎么做？

**答案**：没帮助。两线汇合说明方差不是瓶颈，卡在 70% 是高偏差问题——应换更强的模型（如非线性模型）、增加特征或减弱正则化，而不是加数据。

**练习 3**：为什么绘图时要用 `fill_between` 画标准差带，而不是只画均值折线？

**答案**：均值抹掉了折间波动。标准差带能显示估计的稳定性：某档样本量处带子很宽，说明该档分数对切折方式敏感、结论不可靠；带子窄且两线分开的间隙才可信。

### 4.4 validation_curve：单参数扫描找出甜蜜点

#### 4.4.1 概念说明

验证曲线回答的是另一个问题：**某个超参数取多少最合适？** 横轴换成超参数的取值（对数轴），纵轴仍是训练/验证两条准确率曲线。本讲扫描逻辑回归的 `C`（回顾 u3-l1：`C` 是正则化强度的倒数）：

- `C` 很小 → 正则化过强 → 权重被压死 → **欠拟合**：两线都低、靠近。
- `C` 很大 → 正则化形同虚设 → 模型渐渐把训练集「背」下来 → **过拟合**：训练线抬升逼近 1.0，验证线见顶回落，间隙拉大。
- 中间某处验证准确率最高、间隙还小——**甜蜜点（sweet spot）**。

它与 `learning_curve` 的 API 几乎同构，只是把 `train_sizes` 换成 `param_name` + `param_range`：一次调用内部自动对每个参数值做 10 折交叉验证。参数通过 `'logisticregression__C'` 这个双下划线名字注入管线里的逻辑回归步骤——正是 u5-l2 网格搜索里 `'logisticregression__C'`、`'svc__gamma'` 同一套命名规则。

#### 4.4.2 核心流程

```text
param_range = [0.001, 0.01, 0.1, 1.0, 10.0, 100.0]   # 6 个量级的 C
validation_curve(pipe_lr, X_train, y_train,
                 param_name='logisticregression__C',
                 param_range=param_range, cv=10)
    ↓ 对每个 C 值：10 折 CV，记录训练分与验证分
返回 train_scores / test_scores，形状 (6, 10)
    ↓ axis=1 聚合 → 每档均值±标准差
绘图：x 轴 plt.xscale('log') 对数刻度
```

#### 4.4.3 源码精读

- [ch06/ch06.py:L282](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L282) — 定义参数扫描范围 `[0.001, 0.01, 0.1, 1.0, 10.0, 100.0]`，跨 6 个数量级，适合画对数轴。
- [ch06/ch06.py:L283-L289](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L283-L289) — 调 `validation_curve`：`param_name='logisticregression__C'` 用双下划线定位到管线中的逻辑回归步骤的 `C` 参数，`cv=10` 每个候选值做 10 折评估。
- [ch06/ch06.py:L291-L294](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L291-L294) — 与 4.3 节完全相同的 `axis=1` 均值/标准差聚合。
- [ch06/ch06.py:L296-L312](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L296-L312) — 同款「折线 + `fill_between` 误差带」双曲线绘制。
- [ch06/ch06.py:L314-L322](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L314-L322) — `plt.xscale('log')` 把 x 轴设为对数刻度（参数跨数量级时的标准做法），配图保存为 [ch06/figures/06_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_06.png)。

对照书中配图 [06_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_06.png) 的形态：`C` 从 0.001 增大时两条曲线一起爬升（摆脱欠拟合）；**在 `C≈0.1` 附近验证准确率到达最高点**、且与训练曲线的间隙尚小，这就是最优区域；`C` 继续增大到 10、100 时训练准确率继续走高、验证准确率不再上升（甚至略有回落），间隙扩大——典型的过拟合信号。结论：对该管线而言 `C=0.1` 附近的正则化强度最划算。

#### 4.4.4 代码实践

**实践目标**：复现 C 的验证曲线，读出最优区域；再用 `np.argmax` 把「目测」变成「程序判定」。

**操作步骤**（示例代码）：

```python
# 示例代码：对应 ch06.py L282-L322
import numpy as np
import matplotlib.pyplot as plt
from sklearn.model_selection import validation_curve

param_range = [0.001, 0.01, 0.1, 1.0, 10.0, 100.0]
train_scores, test_scores = validation_curve(
    estimator=pipe_lr, X=X_train, y=y_train,
    param_name='logisticregression__C',
    param_range=param_range, cv=10)

train_mean = np.mean(train_scores, axis=1)
test_mean = np.mean(test_scores, axis=1)

best_idx = np.argmax(test_mean)
print(f'最优 C = {param_range[best_idx]}, '
      f'对应 CV 准确率 = {test_mean[best_idx]:.3f}, '
      f'此时 train-验证间隙 = {train_mean[best_idx] - test_mean[best_idx]:.3f}')

plt.plot(param_range, train_mean, color='blue', marker='o',
         markersize=5, label='Training accuracy')
plt.plot(param_range, test_mean, color='green', linestyle='--',
         marker='s', markersize=5, label='Validation accuracy')
plt.grid(); plt.xscale('log'); plt.legend(loc='lower right')
plt.xlabel('Parameter C'); plt.ylabel('Accuracy')
plt.ylim([0.8, 1.0]); plt.tight_layout(); plt.show()
```

**需要观察的现象**：验证曲线（绿虚线）在哪个 `C` 处到达峰值；峰值两侧曲线与间隙的形态——左端是否两线齐低（欠拟合），右端是否训练线走高而验证线走平/回落（过拟合）。

**预期结果**：验证准确率在 `C=0.1` 附近最高（约 0.97 以上），与书中配图 [06_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/figures/06_06.png) 一致；`C=0.001` 处明显欠拟合、`C=100` 处间隙最大。具体小数位随依赖版本可能略有差异，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果把管线改成 `make_pipeline(StandardScaler(), SVC(random_state=1))` 并想扫描核宽度，`param_name` 应写什么？

**答案**：`'svc__C'` 或 `'svc__gamma'`——`make_pipeline` 对 `SVC` 的自动步骤名是类名小写 `svc`，双下划线后接参数名。这正是 ch06.py 后续网格搜索（u6-l2）中 `'svc__gamma'` 的来历。

**练习 2**：验证曲线上 `argmax(test_mean)` 选出的 `C`，能直接报告为「模型最终测试准确率」吗？

**答案**：不能。验证曲线的最大值是在训练集的 10 折 CV 上选出的，带选择偏差（多个候选里挑最大，天然乐观）。选定 `C` 后应换完全没参与过的测试集评估最终性能；更严格的做法是 u6-l2 将讲的嵌套交叉验证。

**练习 3**：为什么 `param_range` 取 `[0.001, ..., 100.0]` 这种等比序列而不是 `[1, 2, 3, ..., 100]` 等差序列？

**答案**：`C` 的作用跨数量级（每放大 10 倍正则化强度就除以 10），等差序列会在高值区浪费计算、低值区采样过疏；等比（对数）网格用同样多的候选值覆盖每个数量级，且配合 `xscale('log')` 的曲线形态均匀、易于读出拐点。

## 5. 综合实践

把本讲四个模块串成一条完整流水线（对应本讲规格中的实践任务）。任务：**对乳腺癌数据集建一条 `StandardScaler + LogisticRegression` 管线，做 10 折分层交叉验证汇报均值与标准差，再画 `C` 的验证曲线并指认最优 `C` 附近两线的形态。**

```python
# 示例代码：综合实践（在仓库根目录运行；依赖见 u1-l2 的 pyml-book 环境）
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import (train_test_split, cross_val_score,
                                     validation_curve)
from sklearn.pipeline import make_pipeline

# 1) 数据：本地副本，30 特征 + 二分类标签（B->0 良性, M->1 恶性）
df = pd.read_csv('ch06/wdbc.data', header=None)
X = df.loc[:, 2:].values
y = LabelEncoder().fit_transform(df.loc[:, 1].values)
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.20, stratify=y, random_state=1)

# 2) 管线 + 10 折分层交叉验证（n_jobs=-1 并行 10 折）
pipe_lr = make_pipeline(StandardScaler(),
                        LogisticRegression(penalty='l2', max_iter=10000))
scores = cross_val_score(estimator=pipe_lr, X=X_train, y=y_train,
                         cv=10, n_jobs=-1)
print(f'10 折 CV 准确率: {np.mean(scores):.3f} +/- {np.std(scores):.3f}')

# 3) C 的验证曲线
param_range = [0.001, 0.01, 0.1, 1.0, 10.0, 100.0]
train_scores, test_scores = validation_curve(
    estimator=pipe_lr, X=X_train, y=y_train,
    param_name='logisticregression__C', param_range=param_range, cv=10)
train_mean, test_mean = (np.mean(s, axis=1) for s in (train_scores, test_scores))

best = int(np.argmax(test_mean))
print(f'最优 C={param_range[best]}, CV 准确率={test_mean[best]:.3f}, '
      f'间隙={train_mean[best]-test_mean[best]:.3f}')

plt.plot(param_range, train_mean, 'o-', color='blue', label='Training accuracy')
plt.plot(param_range, test_mean, 's--', color='green', label='Validation accuracy')
plt.xscale('log'); plt.grid()
plt.xlabel('Parameter C'); plt.ylabel('Accuracy')
plt.legend(loc='lower right'); plt.tight_layout(); plt.show()

# 4) 用选定的 C 在锁定的测试集上做最终评估
pipe_lr.set_params(logisticregression__C=param_range[best])
pipe_lr.fit(X_train, y_train)
print(f'最终测试集准确率: {pipe_lr.score(X_test, y_test):.3f}')
```

**检查清单**：

1. 第 2 步应输出约 `0.950 +/- 0.014`（与 ch06.ipynb 保存的输出一致；本实践用的是 30 特征版管线，与 ch06.py L231-L232 相同）。
2. 第 3 步最优 `C` 应落在 `0.1` 附近；观察该点形态——验证准确率接近全局最高，且训练/验证两线间隙很小。
3. 对照曲线两端：`C=0.001` 处两线齐低（高偏差），`C=100` 处训练线高、验证线回落、间隙最大（高方差）。
4. 第 4 步用 `set_params` 按双下划线名改参数，避免重建管线；测试集分数是对外汇报的最终数字。

若与书中数值有微小出入（如 sklearn 版本差异导致 0.001~0.01），记录你的版本号即可，形态结论不变；个别小数位差异属正常，待本地验证。

## 6. 本讲小结

- **Pipeline**（`make_pipeline`）把转换器与估计器绑成一个估计器：`fit` 时逐步 `fit_transform`、预测时逐步 `transform`，让「每折重新拟合预处理」自动发生，从机制上杜绝交叉验证中的数据泄漏。
- **分层 K 折**保证每折类别比例与整体一致；手写 `StratifiedKFold` 循环与 `cross_val_score(cv=10)` 对分类器完全等价（本例同为 `0.950 ± 0.014`），汇报「均值 ± 标准差」远比单次留出可信；`n_jobs=-1` 可并行各折。
- **学习曲线**（横轴样本数）诊断偏差—方差：两线齐低且汇合 = 高偏差（欠拟合），训练高验证低、间隙大 = 高方差（过拟合）；本数据集的管线呈轻微过拟合。
- **验证曲线**（横轴超参数，对数轴）扫描单个超参数；乳腺癌管线中 `C≈0.1` 是甜蜜点：验证准确率最高且间隙尚小，两端分别呈现欠拟合与过拟合形态。
- 双下划线命名（`'logisticregression__C'`、`'svc__C'`）是访问管线内部步骤参数的统一约定，贯穿验证曲线与后续网格搜索。
- 测试集全程只用于最终评估：CV 与曲线都只在训练集上进行，选定的参数要在「锁进保险柜」的测试集上做最后一次检验。

## 7. 下一步学习建议

本讲用 `validation_curve` 一次只能调**一个**参数；下一讲 **u6-l2 网格/随机/减半搜索与嵌套交叉验证** 将进入 `ch06/ch06.py` 的 L326 以后：`GridSearchCV` 用参数网格组合式地同时调 `svc__C`、`svc__gamma`、`svc__kernel`，`RandomizedSearchCV` 与 `HalvingRandomSearchCV` 用更少的资源搜索更大的空间，嵌套交叉验证则回答「SVM 和决策树谁更适合这份数据」。建议先通读 [ch06/ch06.py:L326-L447](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L326-L447) 的搜索代码，带着「它就是验证曲线的多参数推广」这个视角去读；学完后再到 u6-l3 看混淆矩阵、精确率/召回率、F1 与 ROC 曲线，补齐评估指标版图。
