# u9-l2 正则化回归与树模型回归

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **Ridge（岭回归）、Lasso、ElasticNet** 三种正则化回归各自在 MSE 之上叠加了什么惩罚项，以及 \(\alpha\) 如何控制系数被「收缩」的程度。
2. 解释一个在书码里真实发生的现象：为什么在**未标准化**的 Ames 数据上 `Lasso(alpha=1.0)` 训练出的系数与普通最小二乘几乎一模一样——惩罚项必须与 MSE 同量纲才起作用，\(\alpha\) 的「有效取值范围」完全由数据量纲决定。
3. 会做 **\(\alpha\) 网格系数轨迹实验**（在对数网格上扫 \(\alpha\)、画出每个系数随 \(\alpha\) 变化的曲线），并用 `ElasticNetCV` 的 5 折交叉验证自动选出最优 \(\alpha\)（示例代码）。
4. 读懂 `DecisionTreeRegressor` 的**分段常数**预测与 `RandomForestRegressor(n_estimators=1000)` 的回归流程，并用 notebook 里保存的真实输出（R² train 0.98 / test 0.85）对比上一讲的线性基线（R² 0.77 / 0.75）。
5. 把 u7-l2 学过的梯度提升迁移到回归任务上，会用 `XGBRegressor` 的 `eta`（学习率）与列采样参数做非线性回归（示例代码，API 依据 ch07 的 `XGBClassifier` 用例）。

本讲源码集中在 [ch09/ch09.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py) 的 L518–L566（正则化回归）与 L768–L865（决策树与随机森林回归）。需要先说明一点：仓库里 ch09 的代码**没有** `ElasticNetCV` 与 `XGBRegressor` 的用例（书中正文有讨论，脚本只演示到三种正则化估计器的实例化与随机森林），本讲涉及这两者的代码均明确标注为「示例代码」，其 API 依据分别来自 sklearn 接口约定与 ch07 的 XGBoost 分类用例。

## 2. 前置知识

**已学过的（简要回顾）：**

- **L2 正则化**（u3-l1）：sklearn 逻辑回归默认带 L2 惩罚，参数 `C` 是正则化强度的**倒数**；u8-l1 网格搜索时的 `penalty='l2'` 也是它。本讲的 Ridge 就是「L2 惩罚 + 线性回归」。
- **train_test_split 与数据泄漏**（u3-l1、u6-l1）：`StandardScaler` 等预处理只能在训练集上 `fit`，测试集只做 `transform`。
- **上一讲的诊断结论**（u9-l1）：5 特征线性回归基线 R² train 0.77 / test 0.75，两线接近且都不高——模型**轻微欠拟合**（偏差主导），残差图还暴露了非线性与异方差。本讲的两条路线（正则化、非线性模型）分别回应「系数稳定性」与「拟合非线性」两个问题。
- **bagging 与 boosting**（u7-l2）：随机森林 = bootstrap 采样 + 并行深树 + 聚合，主要降方差；梯度提升 = 串行让新树拟合损失负梯度（平方损失下就是残差），主要降偏差；`XGBClassifier` 已在 Wine 数据上用过。
- **树模型不需要标准化**（u3-l3）：决策树按阈值切分特征，切分点随量纲自动缩放。

**本讲新增的概念：**

1. **惩罚项（penalty term）与收缩（shrinkage）**：在 MSE 之外按系数大小加一笔「罚金」，训练被迫在「拟合误差小」与「系数别太大」之间折中，系数整体被压向 0（可能压得小，也可能压成精确的 0）。
2. **L1 稀疏性**：Lasso 的 L1 惩罚（系数绝对值之和）在几何上对应菱形约束区域，其顶点落在坐标轴上，最优解容易恰好停在某几个系数为 0 的顶点处——所以 Lasso 天然做**特征选择**；Ridge 的 L2 惩罚对应圆形约束，解一般只缩小、不归零。
3. **系数轨迹（regularization path）**：固定数据，让 \(\alpha\) 从大到小滑动，把每个系数 \(w_j(\alpha)\) 画成曲线。\(\alpha\) 很大时所有系数被压成 0；\(\alpha\) 逐渐减小，系数按「与目标的相关性强弱」依次「长回来」。它是一张直观展示正则化强度的地图。
4. **分段常数预测**：回归树在每个叶子里输出同一个数（叶内样本目标均值），所以整条预测曲线是阶梯状的，而不是直线或光滑曲线。
5. **行采样与列采样**：随机森林 / XGBoost 中制造树与树差异的两类随机化——`subsample` 控制每棵树见多少**样本行**，`colsample_bytree` / `colsample_bylevel` 控制每棵树 / 每层分裂时见多少**特征列**（后者即「列采样」）。

## 3. 本讲源码地图

| 文件 / 位置 | 作用 |
| --- | --- |
| [ch09/ch09.py:L442-L449](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L442-L449) | 5 特征 `X` / 目标 `y` 的定义与 `train_test_split`（70/30，`random_state=123`）。正则化一节与随机森林一节**共用同一套划分**，指标才可比 |
| [ch09/ch09.py:L518-L539](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L518-L539) | 「Using regularized methods for regression」一节：唯一真正 `fit` 的是 `Lasso(alpha=1.0)`，并打印系数与 MSE/R² |
| [ch09/ch09.py:L542-L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L542-L566) | `Ridge`、`Lasso`、`ElasticNet` 三个估计器的**实例化存根**（只创建、未训练），演示 API 写法 |
| [ch09/ch09.py:L570-L764](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L570-L764) | 多项式回归（`PolynomialFeatures` 显式构造 \(x^2\)、\(x^3\) 特征后仍用线性回归拟合）。本讲作为「非线性化的另一条路线」略读 |
| [ch09/ch09.py:L768-L799](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L768-L799) | 决策树回归：`DecisionTreeRegressor(max_depth=3)` 拟合单特征数据并画阶梯状拟合线 |
| [ch09/ch09.py:L803-L865](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L803-L865) | 随机森林回归：`RandomForestRegressor(n_estimators=1000)` 在 5 特征划分上训练，输出 MAE / R² 与残差图 |
| [ch09/ch09.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.ipynb) | 等价 notebook 版。本讲引用的全部数值（Lasso 系数、森林的 MAE/R² 等）都来自它保存的真实运行输出 |
| [ch07/ch07.py:L866-L890](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L866-L890) | 仓库中唯一的 XGBoost 用例（分类版 `XGBClassifier`）。4.4 的 `XGBRegressor` 示例代码以它的参数写法为依据 |
| [ch06/ch06.py:L339-L345](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L345) | 第 6 章的 `param_grid` + `GridSearchCV` 写法。4.2 的调参思想与之同源 |
| [ch09/figures/09_15.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_15.png) | 决策树回归的阶梯状拟合线（深度 3） |
| [ch09/figures/09_16.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_16.png) | 随机森林回归的训练 / 测试残差图 |
| [ch09/AmesHousing.txt](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/AmesHousing.txt) | Ames 住房数据集本地副本（制表符分隔）。脚本原代码从 URL 下载，本地实践直接读它更快 |

## 4. 核心概念与源码讲解

### 4.1 正则化回归三兄弟：Ridge、Lasso 与 ElasticNet

#### 4.1.1 概念说明

上一讲结尾的诊断是：5 特征线性模型 R² train 0.77 / test 0.75，**轻微欠拟合**。那为什么还要学正则化（它通常用来抑制过拟合）？因为线性回归还有一个老毛病：**系数不稳定**。当特征之间高度相关（Ames 里 `Gr Liv Area` 与 `Total Bsmt SF` 都反映房屋大小）或样本量不大时，最小二乘解会把「功劳」在相关特征之间任意分摊，系数的方差很大、解读不可靠。正则化通过给系数「瘦身」来稳定它们——即便对欠拟合模型不能显著提升测试分数，也能让系数估计更稳健、更可解释。

三种方法都是在 MSE 之上加一笔惩罚：

**Ridge（岭回归，L2 惩罚）**——sklearn 最小化的目标函数是

\[ \lVert y - Xw \rVert_2^2 + \alpha \lVert w \rVert_2^2 \]

即「残差平方和 + \(\alpha\) 乘系数平方和」。它把所有系数**按比例缩小**，但几乎不会把系数压成精确的 0。

**Lasso（L1 惩罚）**——sklearn 的目标函数是

\[ \frac{1}{2n}\lVert y - Xw \rVert_2^2 + \alpha \lVert w \rVert_1, \qquad \lVert w \rVert_1 = \sum_j |w_j| \]

绝对值之和的惩罚在 0 点「有棱角」（导数跳变），能把不重要的系数**一路推到精确的 0**，等价于自动特征选择（与 u4-l2 的 SBS 目标相通，但方式是嵌入式而非包裹式）。

**ElasticNet（弹性网络）**——两者按 `l1_ratio`（记 \(r\)）混合：

\[ \frac{1}{2n}\lVert y - Xw \rVert_2^2 + r\alpha\lVert w \rVert_1 + \frac{1-r}{2}\,\alpha\lVert w \rVert_2^2 \]

\(r=1\) 时退化为 Lasso，\(r=0\) 时退化为（相差常数因子的）Ridge；\(0<r<1\) 兼得稀疏性与稳定性，在相关特征成群出现时比纯 Lasso 更稳。

**本模块最重要的一点：\(\alpha\) 是有量纲的。** 惩罚项要和 MSE 项「掰手腕」，两者必须处于同一数量级。\(\alpha=1.0\) 这个默认值绝不是「温和正则化」的通用符号——它的效果完全取决于 y 与 X 的尺度。书码恰好提供了一个绝佳的反例，见 4.1.3。

#### 4.1.2 核心流程

```text
读入 Ames 数据（5 特征 + SalePrice，已在脚本前文完成清洗）
    ↓
train_test_split(X, y, test_size=0.3, random_state=123)   # 与上一讲、与 4.3 的森林完全同一套划分
    ↓
选定估计器（Ridge / Lasso / ElasticNet）与 α、l1_ratio
    ↓
estimator.fit(X_train, y_train)      # 最小化 MSE + 惩罚
    ↓
predict(X_train / X_test) → MSE、R²
    ↓
观察：α 相对 MSE 量级太小时 → 结果 ≈ 普通最小二乘
      α 合适时           → 系数收缩，Lasso 还会归零部分系数
      α 过大时           → 欠拟合（全部被压向 0）
```

#### 4.1.3 源码精读

三个类都从 `sklearn.linear_model` 导入（[ch09/ch09.py:L18-L20](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L18-L20)），用法与所有 sklearn 估计器一致：`fit` → `predict`，系数同样存在 `coef_` 里。

**数据划分**（[ch09/ch09.py:L442-L449](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L442-L449)）：`target = 'SalePrice'`，其余 5 列做特征，70/30 划分。注意两点：其一，**没有接 `StandardScaler`**——正则化一节是在原始量纲上训练的；其二，这段代码在 [L807-L814](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L807-L814) 被原样复跑一遍（相同的 `random_state=123`），所以本节的 Lasso 指标和 4.3 的森林指标可以直接对比。

**真正被训练的只有 Lasso**（[ch09/ch09.py:L524-L528](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L524-L528)）：

```python
lasso = Lasso(alpha=1.0)
lasso.fit(X_train, y_train)
y_train_pred = lasso.predict(X_train)
y_test_pred = lasso.predict(X_test)
print(lasso.coef_)
```

notebook 保存的真实输出（[ch09/ch09.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.ipynb)）是：

```text
[26251.38276394   804.70816337    41.94651964 11364.80761309
    55.67855548]
```

五个系数对应 `Overall Qual、Overall Cond、Gr Liv Area、Central Air、Total Bsmt SF`（[L142-L143](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L142-L143) 的列顺序）。**没有一个系数是 0**——`alpha=1.0` 的 Lasso 一点也不稀疏。紧接着的指标（[ch09/ch09.py:L533-L539](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L533-L539)）输出：

```text
MSE train: 1497216262.014, test: 1516576825.348
R^2 train: 0.769, 0.752
```

（顺带一提：第二行只打印了一个前缀 `R^2 train:`，两个数挤在一起——源码的 f-string 忘了写第二个数的标签，属于书码的小笔误，读输出时别被骗到。）

把这三行和上一讲普通最小二乘在同一划分上的 notebook 真实输出并排放：

| 模型 | MSE train | MSE test | R² train | R² test |
| --- | --- | --- | --- | --- |
| `LinearRegression`（u9-l1） | 1497216245.85 | 1516565821.00 | 0.77 | 0.75 |
| `Lasso(alpha=1.0)` | 1497216262.014 | 1516576825.348 | 0.769 | 0.752 |

**两组数在第 5 位有效数字之后才分出差异**——\(\alpha=1.0\) 的正则化在这里形同虚设。原因可以手算：Lasso 目标函数里 MSE 项约 \(1.5\times 10^9\)，而惩罚项 \(\alpha\lVert w\rVert_1 = 1.0\times(26251.4+804.7+41.9+11364.8+55.7)\approx 3.9\times 10^4\)，比 MSE 项小了约 **5 个数量级**，优化器几乎感觉不到它的存在。再进一步说：各特征量纲悬殊（`Overall Qual` 在 1–10 之间，`Gr Liv Area` 上千），惩罚项对所有系数一视同仁，真要起作用时最先被压扁的会是小量纲特征的系数——这正是「正则化前必须标准化」的第二个理由（第一个理由见 u2-l3 的梯度下降）。

**三个实例化存根**（[ch09/ch09.py:L542-L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L542-L566)）：书码随后只给出 API 写法而未训练——`ridge = Ridge(alpha=1.0)`（[L548](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L548)）、`lasso = Lasso(alpha=1.0)`（[L557](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L557)）、`elanet = ElasticNet(alpha=1.0, l1_ratio=0.5)`（[L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L566)）。`l1_ratio=0.5` 即 4.1.1 公式里的 \(r=0.5\)：L1 与 L2 惩罚各占一半。这三个对象与上一讲 `LinearRegression` 完全同构（`fit`/`predict`/`coef_`），所以 4.2 的轨迹实验可以毫无障碍地套在它们身上。

#### 4.1.4 代码实践

**实践目标**：亲手验证「\(\alpha\) 的量纲陷阱」——在未标准化数据上 `alpha=1.0` 无效，放大若干个数量级后才见效。

**操作步骤**：

1. 按脚本前文准备数据（本地读取，省去联网下载）：

```python
# 示例代码：等价于 ch09.py L142-L171 的数据准备
import pandas as pd
from sklearn.model_selection import train_test_split

columns = ['Overall Qual', 'Overall Cond', 'Gr Liv Area',
           'Central Air', 'Total Bsmt SF', 'SalePrice']
df = pd.read_csv('ch09/AmesHousing.txt', sep='\t', usecols=columns)
df['Central Air'] = df['Central Air'].map({'N': 0, 'Y': 1})
df = df.dropna(axis=0)

X = df[df.columns[df.columns != 'SalePrice']].values
y = df['SalePrice'].values
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.3, random_state=123)
```

2. 依次运行 [ch09/ch09.py:L454-L458](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L454-L458)（OLS 基线）与 [L524-L539](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L524-L539)（Lasso），按 4.1.3 的表格记录两边数字。
3. 手算 \(\lVert w\rVert_1 = \sum_j |w_j|\)（对 notebook 的系数应得约 38 518.5），再算 \(\lVert w\rVert_1 \,/\, \text{MSE train}\)，确认差距约 \(10^{-5}\) 量级。
4. 再跑一个放大版（示例代码）：`Lasso(alpha=1e5, max_iter=100000).fit(X_train, y_train)`，打印 `coef_` 与测试集 R²。

**需要观察的现象**：步骤 2 中 Lasso 与 OLS 的系数、MSE、R² 几乎逐位相同；步骤 4 中部分系数被压成**精确的 0**（量纲小的特征最先阵亡），MSE 显著升高。

**预期结果**：步骤 2 应复现 4.1.3 表格里的 notebook 数值（相同 `random_state` 下应完全一致）；步骤 4 的具体哪些系数归零、R² 跌到多少，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `alpha=1.0` 在这个数据集上几乎没有正则化效果？如果先对特征和目标都做标准化，同一个 `alpha=1.0` 会更弱还是更强？

**答案**：因为惩罚项（约 \(10^4\)）比 MSE 项（约 \(1.5\times 10^9\)）小约 5 个数量级，优化器基本无视它。标准化后 MSE 变成 \(O(1)\) 量级、系数也是 \(O(1)\) 量级，同样的 `alpha=1.0` 反而可能**过强**、把系数几乎全压没——所以 \(\alpha\) 的合理区间完全取决于数据尺度，没有万能默认值。

**练习 2**：`ElasticNet(alpha=1.0, l1_ratio=0.5)` 中把 `l1_ratio` 改成 `1.0` 或 `0.0`，分别等价于什么模型？

**答案**：`l1_ratio=1.0` 时 L2 项消失，等价于 Lasso；`l1_ratio=0.0` 时 L1 项消失，等价于 Ridge（惩罚系数与 Ridge 公式相差一个常数因子 1/2，可被 \(\alpha\) 吸收）。

**练习 3**：Ridge 能把某个系数压成精确的 0 吗？从两种惩罚的梯度形状说明原因。

**答案**：几乎不能。L2 惩罚 \(\alpha w_j^2\) 对 \(w_j\) 的梯度是 \(2\alpha w_j\)，\(w_j\) 越接近 0 推力越小，永远差一点点到不了 0；L1 惩罚 \(\alpha|w_j|\) 的梯度是恒定的 \(\pm\alpha\)（与 \(w_j\) 大小无关），能把系数一路推到 0 并停住。这正是 Lasso 稀疏、Ridge 只收缩的根源。

### 4.2 alpha 网格实验与 ElasticNetCV 自动选 alpha

#### 4.2.1 概念说明

\(\alpha\)（以及 ElasticNet 的 `l1_ratio`）是**超参数**——它不由 `fit` 学出来，只能由我们指定。怎么选？u6-l2 学过通用答案：交叉验证 + 网格搜索。回归正则化还有两个更顺手的工具：

- **系数轨迹实验**：在对数网格上取一串 \(\alpha\)，逐个 `fit`，把每个系数 \(w_j(\alpha)\) 画成曲线。它能一眼看出「哪些特征先被淘汰、\(\alpha\) 多大时全军覆没」。对标准化后的数据存在一个理论上的 \(\alpha_{\max}=\max_j|\frac{1}{n}X_j^T y|\)，超过它所有系数全为 0——sklearn 的 `LassoCV` / `ElasticNetCV` 在不指定 `alphas` 时正是自动按这个上界生成对数网格。
- **`ElasticNetCV`**：把「生成 \(\alpha\) 网格 + K 折交叉验证 + 选最优 + 全量重拟合」打包成一个估计器。它沿正则化路径利用坐标下降的**热启动**（warm start）逐点求解，比裸用 `GridSearchCV` 快得多；但两者本质相同——本章讲义用哪种都行（`GridSearchCV` 的写法见 [ch06/ch06.py:L339-L345](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L345)）。

为什么网格要**对数均匀**（`np.logspace`）？因为 \(\alpha\) 的作用横跨数量级（\(10^{-4}\) 到 \(10^{0}\) 的行为完全不同），线性均匀取样会把点全浪费在无关紧要的区间里——u6-l2 对 `loguniform` 采样讲过同样的道理。

再次强调：`ElasticNetCV` **不在** ch09 的仓库代码里，本模块代码均为示例代码，骨架（划分、`fit`/`predict`、R²）全部沿用 [ch09/ch09.py:L442-L458](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L442-L458)。

#### 4.2.2 核心流程

```text
对 X_train、y_train 分别标准化            # 让系数与 MSE 同量纲，alpha 网格才有统一意义
    ↓
给定 alpha 候选网格（对数均匀，如 10⁻⁴ … 10⁰，共 40 点）
    ↓
Ridge / Lasso 轨迹：对每个 alpha 逐一 fit，收集 coef_ → 画系数曲线
    ↓
ElasticNetCV(l1_ratio=0.5, alphas=网格, cv=5)：
    对每个 alpha：
        5 折交叉验证 → mse_path_[j] = 5 个验证 MSE（即「5 折验证曲线」）
        （实现上沿路径热启动，越算越快）
    按折平均 → 平均验证 MSE 曲线 → 取最小点 → alpha_
    ↓
用全部训练数据 + alpha_ 自动重新拟合 → coef_、l1_ratio_
    ↓
在轨迹图上画竖线标出 alpha_，检查它落在「系数已收缩但未全灭」的区间
```

#### 4.2.3 源码精读

仓库内可直接引用的骨架是上一讲已精读过的三段：数据划分（[ch09/ch09.py:L442-L449](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L442-L449)）、`fit`/`predict`（[L454-L458](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L454-L458)）与 R² 计算（[L511-L514](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L511-L514)）；三个估计器的构造写法见 [L548](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L548) 与 [L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L566)。调参的「网格 + 交叉验证」思想在仓库中的真实用例是第 6 章（[ch06/ch06.py:L339-L345](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch06/ch06.py#L339-L345)：`param_grid` 字典 + `GridSearchCV(estimator=..., ...)`），把那里的 `estimator` 换成 `Ridge()`、`param_grid` 换成 `{'alpha': np.logspace(...)}` 就是本模块的通用版实现。

一个值得记住的细节：R² 对 y 的线性变换不变——若 \(\hat y_{\text{std}} = (\hat y-\mu)/\sigma\)、\(y_{\text{std}} = (y-\mu)/\sigma\) 使用同一组 \(\mu,\sigma\)，则 \(R^2(y,\hat y) = R^2(y_{\text{std}},\hat y_{\text{std}})\)。所以「在标准化尺度上算 R²」与「换算回美元再算 R²」结果相同，模型对比不受影响（MSE、MAE 则**会**差一个 \(\sigma^2\)、\(\sigma\) 因子，比较时要用同一尺度）。

#### 4.2.4 代码实践

**实践目标**：在标准化数据上复现 Ridge / Lasso 的系数轨迹图，并用 `ElasticNetCV` 的 5 折交叉验证选出最优 \(\alpha\)、在图上标出。

**操作步骤**（以下均为示例代码）：

```python
# 示例代码：系数轨迹 + ElasticNetCV（基于 ch09.py L442-L449 的数据划分）
import numpy as np
import matplotlib.pyplot as plt
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge, Lasso, ElasticNetCV
from sklearn.metrics import r2_score

# 1) 训练集上标准化特征与目标（防泄漏：sc_y 也只在训练集 fit）
sc_x, sc_y = StandardScaler(), StandardScaler()
X_tr = sc_x.fit_transform(X_train)
y_tr = sc_y.fit_transform(y_train[:, np.newaxis]).flatten()

alphas = np.logspace(-4, 0, 40)

# 2) 系数轨迹：每个 alpha 拟合一次，收集 coef_
ridge_coefs = [Ridge(alpha=a).fit(X_tr, y_tr).coef_ for a in alphas]
lasso_coefs = [Lasso(alpha=a, max_iter=100000).fit(X_tr, y_tr).coef_
               for a in alphas]

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4), sharex=True)
for j in range(X_tr.shape[1]):
    ax1.plot(alphas, [c[j] for c in ridge_coefs])
    ax2.plot(alphas, [c[j] for c in lasso_coefs])
for ax, name in [(ax1, 'Ridge'), (ax2, 'Lasso')]:
    ax.set_xscale('log'); ax.set_xlabel('alpha'); ax.set_title(name)

# 3) ElasticNetCV：5 折交叉验证自动选 alpha
encv = ElasticNetCV(l1_ratio=0.5, alphas=alphas, cv=5,
                    max_iter=100000).fit(X_tr, y_tr)
ax2.axvline(encv.alpha_, color='k', ls='--',
            label=f'ElasticNetCV alpha={encv.alpha_:.4g}')
ax2.legend()

# 4) 测试集 R²（标准化尺度上算即可，见 4.2.3 的说明）
X_te = sc_x.transform(X_test)
y_te = sc_y.transform(y_test[:, np.newaxis]).flatten()
print('ElasticNetCV alpha:', encv.alpha_)
print('test R^2:', r2_score(y_te, encv.predict(X_te)))
```

**需要观察的现象**：右图（Lasso）中 5 条曲线随 \(\alpha\) 增大**陆续触零**并保持在 0；左图（Ridge）中曲线只是**渐近地贴近 0**、始终不触零（呼应 4.1.5 练习 3）；虚线（CV 选中的 \(\alpha\)）应落在「系数已开始收缩、但远未全灭」的区间。

**预期结果**：由于上一讲已确认线性模型是欠拟合的，交叉验证大概率选出**偏小的 \(\alpha\)**（弱正则化），test R² 与 0.75 的线性基线相差无几（具体数值**待本地验证**）。另可尝试把 `l1_ratio` 改成列表（如 `[0.1, 0.5, 1.0]`）并删去 `alphas` 参数，让模型把混合比例也自动搜出来（`encv.l1_ratio_` 查看结果）。

#### 4.2.5 小练习与答案

**练习 1**：轨迹实验为什么必须先标准化？不标准化的两个后果是什么？

**答案**：后果一（4.1 已证）：\(\alpha\) 网格覆盖的有效范围随量纲漂移，`logspace(-4, 0)` 可能整个区间都无效；后果二：各系数量纲悬殊，同一 \(\alpha\) 对不同特征的「杀伤力」完全不对等，轨迹图的形状没有可读性。标准化后 \(\alpha_{\max}\approx\max_j|\mathrm{corr}(x_j,y)|\le 1\)，网格 `logspace(-4, 0)` 恰好覆盖「全灭到无正则化」的全过程。

**练习 2**：如果 `ElasticNetCV` 选中的 \(\alpha\) 正好等于网格的最小端点，说明什么？该怎么办？

**答案**：说明验证 MSE 在网格范围内单调下降、真正的最优 \(\alpha\) 在网格**左边界之外**（更弱的正则化）。应把网格向更小的 \(\alpha\) 扩展（如 `logspace(-6, 0)`）重跑；这通常也提示模型本身欠拟合、不太需要正则化——与上一讲的诊断一致。

**练习 3**：`encv.mse_path_` 的形状是什么含义？怎么由它画出「5 折验证曲线」？

**答案**：标量 `l1_ratio` 时形状为 `(len(alphas), cv=5)`——每个候选 \(\alpha` 在 5 个折上各记录一个验证 MSE。对轴 1 取平均得到平均验证 MSE 曲线，逐折画则得到 5 条抖动曲线，二者叠加就是典型的「验证曲线」图（画法与 u6-l1 的 `validation_curve` 一致）。

### 4.3 从直线到阶梯与森林：决策树回归与随机森林回归

#### 4.3.1 概念说明`

上一讲残差图里弯曲的形态说明：房价与特征之间不是纯线性关系。脚本里其实给出了**两条路线**：

- **路线 A：改造特征**（[ch09/ch09.py:L570-L764](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L570-L764)）：用 `PolynomialFeatures` 显式造出 \(x^2\)、\(x^3\) 列，再交给**同一个** `LinearRegression`——模型本身还是线性的，只是特征空间弯了。缺点：要手工挑次数、挑特征，且容易外推失控。
- **路线 B：换模型**（本模块）：直接用天生非线性的树模型。

**回归树**怎么工作？它和第 3 章的分类树共享同一套递归切分骨架，只换了两样东西：

1. **分裂准则**：从「信息增益 / 基尼」换成「MSE 削减」——每次切分选使两个子节点的加权平方误差最小的特征与阈值（即 [ch09/ch09.py:L822](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L822) 森林里的 `criterion='squared_error'`，它是 sklearn 1.0 里旧名 `'mse'` 的新名字，两者等价）。
2. **叶节点输出**：分类树输出众数类别，回归树输出**叶内样本的目标均值**。因此预测函数是**分段常数**（阶梯）——这是回归树最重要的指纹。

单棵树的问题在 u7-l2 讨论过：树深则方差大、不稳定。解法也一样——**随机森林**：bootstrap 抽样造多份数据、每份训练一棵不剪枝的回归树、预测时把所有树的输出**取平均**（分类森林投票，回归森林平均）。方差被平均掉，非线性能力保留。此外它继承了 u3-l3 的结论：基于阈值切分，**不需要标准化**——所以 [L821-L825](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L821-L825) 直接在原始量纲的 `X_train` 上 `fit`。

#### 4.3.2 核心流程

```text
【回归树】（max_depth=3 时递归 3 层）
    每个节点：遍历 (特征 j, 阈值 t)，选使
        MSE(父) − [n_L·MSE(左) + n_R·MSE(右)]/n   最大的切分
    → 直到达 max_depth 或节点样本过少
    预测：新样本落到哪个叶，就输出那个叶的训练目标均值

【随机森林】
    for b in 1..n_estimators(1000):
        从训练集有放回抽 n 个样本            # bootstrap，约 36.8% 样本每轮未被抽中
        训练一棵深回归树                     # 每次分裂只考虑一部分特征（随机化）
    预测：ŷ = 1000 棵树输出的平均
```

#### 4.3.3 源码精读

**决策树回归**（[ch09/ch09.py:L778-L793](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L778-L793)）：

```python
X = df[['Gr Liv Area']].values
y = df['SalePrice'].values

tree = DecisionTreeRegressor(max_depth=3)
tree.fit(X, y)
sort_idx = X.flatten().argsort()

lin_regplot(X[sort_idx], y[sort_idx], tree)
```

只取 `Gr Liv Area` 一个特征。`sort_idx = X.flatten().argsort()` 是个值得学的绘图细节：`lin_regplot`（[L274-L277](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L274-L277)）内部用 `plt.plot(X, model.predict(X))` 画折线，而 `X_train` 是乱序的——不排序的话阶梯线会来回折返成毛刺。`argsort` 返回「排好序后的原下标」，让散点与阶梯线都按面积从小到大排。成图见 [ch09/figures/09_15.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_15.png)：黑色阶梯线就是深度 3 的回归树预测——只有 \(2^3=8\) 个叶节点，每个台阶输出一个常数。

紧接着的评分（[ch09/ch09.py:L798-L799](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L798-L799)）notebook 真实输出为 `0.5144569334885711`。注意它**不能**与后面的 0.85 直接比较：这里只用 1 个特征、且在同一批数据上拟合又评分（是拟合优度，不是泛化估计）。

**随机森林回归**（[ch09/ch09.py:L807-L827](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L807-L827)）：先原样复跑一遍 5 特征划分（`random_state=123`，与 Lasso 一节完全一致），然后：

```python
forest = RandomForestRegressor(n_estimators=1000,
                               criterion='squared_error',
                               random_state=1,
                               n_jobs=-1)
forest.fit(X_train, y_train)
```

四个参数各司其职：`n_estimators=1000`——森林里 1000 棵树，树越多平均的方差削减越充分（且不像深单树那样加剧过拟合）；`criterion='squared_error'`——按 MSE 削减选切分；`random_state=1`——钉死 bootstrap 与特征子集的随机性保证可复现；`n_jobs=-1`——bagging 的树彼此独立，可用全部 CPU 核并行训练（u7-l2 讲过 bagging 可并行、boosting 必须串行，这里是前者的直接体现）。

指标（[ch09/ch09.py:L830-L839](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L830-L839)）的 notebook 真实输出：

```text
MAE train: 8305.18
MAE test: 20821.77
R^2 train: 0.98
R^2 test: 0.85
```

与线性基线（同一划分）对照：

| 模型 | MAE train | MAE test | R² train | R² test |
| --- | --- | --- | --- | --- |
| `LinearRegression`（5 特征） | 25983.03 | 24921.29 | 0.77 | 0.75 |
| `RandomForestRegressor(1000)` | 8305.18 | 20821.77 | 0.98 | 0.85 |

两个读数：**测试集 R² 从 0.75 提到 0.85、MAE 从约 2.49 万美元降到约 2.08 万美元**——非线性能力的真实收益；同时 train R² 高达 0.98、train MAE 只有 8305 美元，训练误差远小于测试误差——单看会喊「过拟合」，但它的测试成绩仍显著优于线性模型。这正是随机森林的典型行为：深树个体过拟合，bootstrap + 平均把方差压了下去，「在测试集上依然更强」。诊断不能只看 train/test 间隙，要**和替代模型横向比**。最后的残差图（[ch09/ch09.py:L843-L865](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L843-L865)，成图 [09_16.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_16.png)）复用了上一讲的画法：左（测试）残差散布范围明显大于右（训练），与指标结论互相印证。

#### 4.3.4 代码实践

**实践目标**：跑通仓库的树 / 森林段落并量化 `n_estimators` 的影响。

**操作步骤**：

1. 依次运行 [ch09/ch09.py:L778-L799](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L778-L799)（单特征树）与 [L807-L839](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L807-L839)（5 特征森林），记录 4 个数字：树的 R²（≈0.514）、森林的 MAE train/test 与 R² train/test（≈8305/20822、0.98/0.85）。
2. 把 [L821](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L821) 的 `n_estimators` 依次改成 `10`、`100`、`1000`（`random_state=1` 保持不变），各训一次，把 6 个指标填进表格。
3. 观察 `n_jobs=-1` 与去掉它（默认单核）在 `n_estimators=1000` 时的训练耗时差别。

**需要观察的现象**：树的拟合图呈 8 级台阶；森林在三种树数下 train R² 都接近 1，而 test R² 大体稳定在 0.85 附近——树数增加主要让预测更平滑、结果更稳定，而不是继续推高测试分数；`n_jobs=-1` 明显更快。

**预期结果**：步骤 1 的四个数字应与 notebook 保存输出一致；步骤 2 中 `n_estimators=10` 的 test R² 可能略低或略高（小森林方差大）、`100` 与 `1000` 接近，具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么随机森林可以直接吃原始量纲的 `X_train`，而 Ridge/Lasso 轨迹实验必须先标准化？

**答案**：树的切分点是逐特征独立扫描阈值得到的，把某列乘以 1000 只会让选中的阈值同步乘以 1000，树结构不变——基于阈值的模型对单调缩放不敏感（u3-l3 已下过同样结论）。而正则化惩罚把所有系数放在**同一把尺子**下罚款，量纲不统一时罚款对各特征不对等，必须先标准化。

**练习 2**：森林 train R²=0.98、test R²=0.85，间隙很大，为什么书里仍然推荐它而不是「间隙更小」的线性模型？

**答案**：模型好坏的最终标准是测试集表现。间隙大只说明模型有记忆训练集的能力（方差大），bagging 已经把这部分方差平均掉了大半；横向对比测试集：0.85 > 0.75、MAE 低约 4000 美元，森林胜出。若还想缩小间隙，可以限制树深或加大 `min_samples_leaf`，但通常会牺牲一点测试分数。

**练习 3**：`tree_r2 = 0.514` 与森林的 `0.85` 为什么不可直接比较？举出两个原因。

**答案**：原因一，特征集不同——树只用了 `Gr Liv Area` 1 个特征，森林用了全部 5 个（含信息量更大的 `Overall Qual`）；原因二，评估协议不同——树是在**全量数据**上「拟合又自评」（拟合优度），森林是 train 拟合、test 评分（泛化估计）。做公平比较必须固定特征集与划分（如 4.3.4 步骤 2 那样只动 `n_estimators`）。

### 4.4 XGBRegressor：梯度提升回归与列采样

#### 4.4.1 概念说明

第三条非线性路线是 u7-l2 的**梯度提升**搬到回归：串行训练一串小树，每棵新树拟合当前模型损失函数的负梯度——平方损失下负梯度就是残差 \(y - F(x)\)。更新式为

\[ F_0(x) = \bar y, \qquad F_m(x) = F_{m-1}(x) + \eta \cdot h_m(x) \]

其中 \(h_m\) 是第 m 棵小树，\(\eta\) 是**学习率**（XGBoost 里写作 `learning_rate`，别名 **`eta`**）。\(\eta\) 把每棵树的贡献收缩得越小，需要的树越多，但泛化通常越稳（小步慢走的偏差-方差权衡）。

XGBoost 之所以成为工业界标配，核心在于它的目标函数**自带正则化**——不只是拟合损失，还惩罚模型复杂度：

\[ \mathcal{L} = \sum_i l\!\left(\hat y_i, y_i\right) + \sum_k \Omega(f_k), \qquad \Omega(f) = \gamma T + \tfrac{1}{2}\lambda \lVert w \rVert_2^2 \]

\(T\) 是某棵树的叶节点数（\(\gamma\) 罚叶子多），\(w\) 是叶输出权重（\(\lambda\) 罚输出大）——这正好呼应本讲 4.1 的主题：正则化思想不止用在线性模型的系数上，也内建在树的生长与叶输出里。

**列采样**是 XGBoost 从随机森林借来的随机化：`colsample_bytree` 让每棵树只见到一部分特征列，`colsample_bylevel` 进一步在每层分裂时再抽一次（`subsample` 则是行采样）。特征子集让树与树差异更大，提升的每一步「纠错」更多样，抗过拟合更好。

**事实核查（重要）**：ch09 的仓库代码到随机森林为止，**没有** `XGBRegressor` 用例；仓库中唯一的 XGBoost 代码是第 7 章分类用的 `XGBClassifier`（[ch07/ch07.py:L866-L890](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L866-L890)，版本检查钉在 `'xgboost': '1.5.0'`，见 [L58](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L58)）。`XGBRegressor` 与 `XGBClassifier` 的 sklearn 接口参数名完全一致（`fit`/`predict`/`n_estimators`/`learning_rate`/`max_depth`…），只是内部目标函数不同（回归默认 `reg:squarederror`）。本模块代码为示例代码。

#### 4.4.2 核心流程

```text
初始化 F₀ = 训练集目标均值（只预测一个常数的模型）
    ↓
for m in 1..n_estimators:
    伪残差 r = y − F_{m−1}(X)                # 平方损失的负梯度
    用行/列采样的数据拟合一棵 max_depth 浅的树 h_m   # 只纠错、不求全功
    F_m = F_{m−1} + eta · h_m                 # 小学习率收缩这棵树的贡献
    ↓
预测：F(X) = F₀ + eta·h₁(X) + … + eta·h_M(X)
```

与 4.3 森林的对照：森林的树**互相独立、可以并行**（`n_jobs=-1` 有用）；提升的树**必须串行**（第 m 棵依赖前 m−1 棵的结果），XGBoost 的并行发生在单棵树内部（分裂搜索），不在树与树之间。

#### 4.4.3 源码精读

仓库依据是第 7 章的 XGBoost 分类用例（[ch07/ch07.py:L879-L890](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L879-L890)）：

```python
model = xgb.XGBClassifier(n_estimators=1000, learning_rate=0.01,
                          max_depth=4, random_state=1,
                          use_label_encoder=False)
gbm = model.fit(X_train, y_train)
y_train_pred = gbm.predict(X_train)
```

参数解读（回归版沿用同名参数）：`n_estimators=1000` 配 `learning_rate=0.01`——典型的「小步 × 多树」组合（u7-l2 讲过小学习率需配大轮数）；`max_depth=4`——每棵树都很浅，只纠一部分错；`random_state=1`——钉死采样随机性；`use_label_encoder=False` 是**分类器专属**的历史参数（新版 xgboost 已废弃），写 `XGBRegressor` 时直接去掉即可。`import xgboost as xgb` 见 [ch07/ch07.py:L32](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L32)。

#### 4.4.4 代码实践

**实践目标**：用 `XGBRegressor` 在与森林**完全相同**的 5 特征划分上做回归，比较测试集 R²，并感受 `eta` 与列采样的作用。

**操作步骤**（示例代码，需先在环境中安装 xgboost，仓库版本检查钉在 1.5.0）：

```python
# 示例代码：XGBRegressor 与随机森林同台对比（复用 ch09.py L807-L814 的划分）
from xgboost import XGBRegressor

for eta in [0.01, 0.1, 0.3]:
    xgb_reg = XGBRegressor(n_estimators=1000, eta=eta, max_depth=4,
                           colsample_bytree=0.8, subsample=0.8,
                           random_state=1)
    xgb_reg.fit(X_train, y_train)          # 树模型：直接用原始量纲
    print(f'eta={eta}: '
          f'train R2={r2_score(y_train, xgb_reg.predict(X_train)):.3f}, '
          f'test R2={r2_score(y_test, xgb_reg.predict(X_test)):.3f}')
```

再固定 `eta=0.1`，把 `colsample_bytree` 在 `{0.5, 0.8, 1.0}` 之间扫一遍，观察列采样对 train/test R² 间隙的影响。

**需要观察的现象**：`eta=0.01` 时 1000 棵树可能还欠拟合（train R² 明显低于 1）；`eta=0.3` 收敛快但 test R² 未必最高；列采样从 1.0 降到 0.5 通常 train R² 下降、间隙收窄（随机化抑制记忆）。

**预期结果**：调参得当时 test R² 应能达到甚至略超森林的 0.85（Ames 只有 5 个特征、约 2000 条训练样本，两种树集成差距不大）；具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`eta=0.01` 与 `eta=0.3`（其余参数相同）各有什么风险？

**答案**：`eta` 太小 + 树数不足 → 欠拟合（每步纠错太保守，走不到最优附近）；`eta` 太大 → 每步走得太猛，容易过拟合且对个别树的错误敏感、训练曲线抖动。实践中常用 0.01–0.1 配大 `n_estimators`，或配合早停（`early_stopping_rounds`）自动定树数。

**练习 2**：`colsample_bytree` 与随机森林的哪个机制相似？它们的目的有什么共同点？

**答案**：与随机森林每次分裂只用一部分特征（sklearn 里的 `max_features`）相似。共同点是制造树与树之间的**差异**：树看到的数据/特征不同，犯的错就不同，聚合（平均或累加）之后个体错误互相抵消，泛化更好。

**练习 3**：XGBoost 的目标函数里 \(\lambda\lVert w\rVert_2^2\) 罚的是什么「系数」？它和 Ridge 的 L2 惩罚有什么对应关系？

**答案**：罚的是**每个叶节点的输出权重** \(w\)（叶子上输出的数值大小），不是线性模型的系数。思想与 Ridge 同源——都是 L2 范数收缩、防止单个分量过大；区别在于作用对象（叶输出 vs 线性权重）与求解方式（XGBoost 的增益公式里 \(\lambda\) 直接进入每个叶的最优输出 \(w_j^* = G_j/(H_j+\lambda)\) 的分母，起「平滑收缩」作用）。

## 5. 综合实践

**任务**：写一个完整脚本 `regression_showdown.py`，把本讲三条线串起来，对 Ames 房价给出一份「模型对比报告」。

**要求**：

1. **数据与划分**：本地读取 `ch09/AmesHousing.txt`（复用 4.1.4 步骤 1 的代码，等价于 [ch09/ch09.py:L142-L171](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L142-L171)），并固定 `train_test_split(X, y, test_size=0.3, random_state=123)`——保证所有模型的测试集逐行相同。
2. **正则化族（在标准化尺度上做）**：
   - 对 `X_train`、`y_train` 分别标准化（scaler 只在训练集 `fit`）；
   - 画 Ridge 与 Lasso 的系数轨迹双子图（`alphas = np.logspace(-4, 0, 40)`）；
   - 用 `ElasticNetCV(l1_ratio=0.5, alphas=alphas, cv=5)` 自动选 \(\alpha\)，在 Lasso 子图上画竖线并打印 `encv.alpha_`。
3. **非线性族（在原始量纲上做）**：`RandomForestRegressor(n_estimators=1000, criterion='squared_error', random_state=1, n_jobs=-1)` 与 `XGBRegressor(n_estimators=1000, eta=0.1, max_depth=4, random_state=1)`（如未装 xgboost 则跳过并注明）。
4. **报告**：打印一张对比表，列为「模型 / test R² / test MAE」，行含：`LinearRegression`、`ElasticNet(CV选参)`、`RandomForestRegressor`、`XGBRegressor`。注意正则化族的 R² 可直接用标准化尺度计算（见 4.2.3），但 **MAE 必须把预测值 `inverse_transform` 回美元后再算**，否则与树模型不可比。

**预期结果**（可先填好再验证）：

| 模型 | test R² | 依据 |
| --- | --- | --- |
| LinearRegression | ≈ 0.75 | notebook 已验证（上一讲） |
| ElasticNet（CV 选小 \(\alpha\)） | ≈ 0.75（与线性几乎持平） | 待本地验证 |
| RandomForestRegressor(1000) | ≈ 0.85 | notebook 已验证（本讲 4.3.3） |
| XGBRegressor | ≈ 0.85 上下 | 待本地验证 |

**思考题**：若报告显示 ElasticNet 与 LinearRegression 几乎打平，结合上一讲「欠拟合」的诊断，下一步你会优先调「正则化强度」还是「换更强的非线性模型」？为什么？（答：换模型——正则化主要改善系数稳定性/方差，对偏差主导的欠拟合帮助有限；这正是书稿把正则化放前、树模型放后的叙事逻辑。）

## 6. 本讲小结

- **三种正则化回归只差一个惩罚项**：Ridge 加 \(\alpha\lVert w\rVert_2^2\)（只收缩、不归零），Lasso 加 \(\alpha\lVert w\rVert_1\)（能压出精确 0，兼做特征选择），ElasticNet 用 `l1_ratio` \(r\) 在两者间插值（[ch09/ch09.py:L524-L566](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L524-L566)）。
- **\(\alpha\) 是有量纲的超参数**：未标准化的 Ames 数据上 MSE 项约 \(1.5\times10^9\)、惩罚项约 \(3.9\times10^4\)，`alpha=1.0` 形同虚设——notebook 真实输出显示 Lasso 与 OLS 的 MSE、R² 在第 5 位有效数字内一致；正则化前必须把特征（必要时含目标）标准化。
- **\(\alpha\) 靠交叉验证选**：系数轨迹图展示「系数随 \(\alpha\) 陆续归零 / 渐近缩小」的全过程；`ElasticNetCV` 沿正则化路径做 5 折交叉验证自动选 \(\alpha\)（示例代码），与 u6-l2 的 `GridSearchCV` 思想同源。
- **回归树输出分段常数**：分裂准则换成 MSE 削减、叶节点输出均值，深度 3 的单特征树在 Ames 上拟合优度 R²≈0.514（[ch09/ch09.py:L783-L799](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L783-L799)）；`argsort` 排序是画阶梯线的关键细节。
- **随机森林回归 = bootstrap + 回归树 + 平均**：与线性基线同一划分上，MAE 20821.77 vs 24921.29 美元、R² 0.85 vs 0.75（notebook 已验证）；train R² 0.98 间隙大但测试仍更强——评估要横向对比，不要只看间隙。
- **XGBoost 把正则化内建进树**：`eta` 收缩每棵树贡献、`colsample_bytree`/`colsample_bylevel` 列采样制造树间多样性、目标函数罚叶数与叶权重；仓库中的 API 依据是 ch07 的 `XGBClassifier`（[ch07/ch09 对应行号见 4.4.3](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch07/ch07.py#L879-L890)），回归版为示例代码。

## 7. 下一步学习建议

- **下一单元 u10-l1（KMeans、肘部法与轮廓系数）**：本讲结束了监督学习（分类 + 回归）主线，第 10 章进入**无监督的聚类**——没有 y、没有 R²，评估方式换成肘部法与轮廓系数；预习时先浏览 `ch10/ch10.py` 的 `make_blobs` 与 `silhouette` 段落。
- **回看 u6-l2（网格/随机搜索）**：本讲 `ElasticNetCV` 是「专用快捷版」的调参器，把 4.2 的 Ridge 换成 `Pipeline([('sc', StandardScaler()), ('ridge', Ridge())])` 再交给 `GridSearchCV` 搜 `ridge__alpha`，就是「通用版」的完整写法——两种实现对照一遍，调参功底就扎实了。
- **延伸阅读源码**：`ch09/ch09.py` L570–L764 的多项式回归是本讲略读的「路线 A」，跑出 09_13/09_14 两张图，对比「特征改造 vs 换模型」两条非线性化路线的代价；u7-l2 的 `AdaBoostClassifier`/`GradientBoostingClassifier` 段落则可与 4.4 的 `XGBRegressor` 互为参照。
