# u9-l1 线性回归与 RANSAC

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解**回归（regression）**与分类的区别：预测的目标变量从「离散类别」变成了「连续数值」。
2. 逐行读懂手写的 `LinearRegressionGD` 类，看明白「回归版梯度下降」的更新式，并意识到它与第 2 章的 `AdalineGD` 在数学上完全同构。
3. 会用 scikit-learn 的 `LinearRegression`，读懂它给出的斜率 `coef_` 与截距 `intercept_`，并用书中附带的「正规方程」手工算一遍验证两者一致。
4. 理解离群点如何把普通最小二乘的回归线「拉歪」，以及 `RANSACRegressor` 如何用 `inlier_mask_`（内点掩码）把离群点排除在拟合之外。
5. 学会用**残差图**和 MSE / MAE / R² 三类指标诊断一个回归模型的好坏。

本讲源码集中在 `ch09/ch09.py` 的 L212–L514（手写梯度下降 → sklearn 线性回归 → RANSAC → 评估），后面的正则化回归与树回归留给 u9-l2。

## 2. 前置知识

本讲默认你已读过前面几讲的概念，先快速回顾，再补充回归特有的新概念。

**已学过的（简要回顾）：**

- **梯度下降**（u2-l2）：整批梯度下降每轮用全部样本算一次梯度，更新式为 \( w \leftarrow w + \eta \cdot \frac{2}{n} X^T(y - \hat{y}) \)；学习率 \(\eta\) 是最敏感的超参数，特征未缩放时极易发散。
- **标准化**（u2-l3、u6-l1）：`StandardScaler` 把每列特征变成均值 0、方差 1；参数只能在训练集上 `fit`，新数据只做 `transform`。
- **估计器范式**（u3-l1）：sklearn 的模型统一走 `fit` / `predict` 接口，`LinearRegression` 与之前的分类器用法完全一致。

**本讲新增的概念：**

1. **回归 vs 分类**。前面预测的是鸢尾花品种、肿瘤良恶性这类**离散标签**；本讲预测的是房价这类**连续数值**。模型的输出不再是类别编号，而是一个实数，因此评估方式也从「准确率」换成 MSE、R² 等数值误差指标。

2. **简单线性回归模型**。用一条直线（多元时是超平面）拟合数据：

   \[ y = w_1 x_1 + w_2 x_2 + \dots + w_m x_m + b + \epsilon \]

   其中 \( w \) 是权重（回归里叫**斜率 / 系数**），\( b \) 是偏置（**截距**），\( \epsilon \) 是无法解释的噪声。训练目标是最小化**均方误差（MSE，即平方误差和 SSE 的平均）**：

   \[ J(w, b) = \frac{1}{n}\sum_{i=1}^{n}\left(y^{(i)} - \hat{y}^{(i)}\right)^2 \]

   只用一个特征（如居住面积）预测房价叫**简单线性回归**，用多个特征叫**多元线性回归**。

3. **残差（residual）**。单个样本的预测误差 \( y^{(i)} - \hat{y}^{(i)} \)。把残差画成图是回归里最重要的诊断工具：好的模型残差应随机散布在 0 附近，若残差呈漏斗形或曲线形，说明模型有问题（详见 4.4）。

4. **离群点（outlier）与鲁棒回归**。MSE 对误差取平方，误差被平方放大——一个偏离 30 万美元的豪宅能把整条回归线拉向自己。RANSAC（RANdom SAmple Consensus，随机样本共识）通过反复随机抽样、只信任「大多数点都同意」的模型，自动识别并剔除离群点（详见 4.3）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch09/ch09.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py) | 第 9 章全部代码的脚本版。本讲覆盖 L142–L514：数据载入、`LinearRegressionGD`、sklearn `LinearRegression`、`RANSACRegressor`、残差图与评估指标 |
| [ch09/AmesHousing.txt](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/AmesHousing.txt) | Ames 住房数据集本地副本（2 930 行数据 × 82 列，制表符分隔）。脚本原代码从 URL 下载，本地实践可直接读它 |
| [ch09/ch09.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.ipynb) | 等价 notebook 版，内含各单元格的真实运行输出。本讲引用的全部数值（斜率、MSE、R² 等）都来自这里保存的输出 |
| [ch09/figures/09_06.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_06.png) | `LinearRegressionGD` 的 epoch–MSE 损失曲线 |
| [ch09/figures/09_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_09.png) | RANSAC 拟合结果：蓝色圆点为内点（inliers），绿色方块为离群点（outliers） |
| [ch09/figures/09_11.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_11.png) | 训练集 / 测试集残差图 |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py) | 第 2 章 `AdalineGD`（L307–L390），本讲用来与 `LinearRegressionGD` 对照 |

数据集背景：**Ames Housing**（艾姆斯住房数据集，爱荷华州 Ames 市 2006–2010 年的真实成交记录）。原始数据 82 列，脚本只挑了 6 列（[ch09/ch09.py:L142-L143](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L142-L143)）：

| 列名 | 含义 |
| --- | --- |
| `Overall Qual` | 房屋整体材料与装修评分（1 差 – 10 优） |
| `Overall Cond` | 房屋整体状况评分（1 差 – 10 优） |
| `Gr Liv Area` | 地上居住面积（平方英尺） |
| `Central Air` | 是否有中央空调（N/Y） |
| `Total Bsmt SF` | 地下室总面积（平方英尺） |
| `SalePrice` | 成交价（美元），本讲要预测的**目标变量** |

脚本载入后打印的形状是 `(2930, 6)`（notebook 真实输出）；`Central Air` 用 `map` 把 N/Y 转成 0/1（[ch09/ch09.py:L159](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L159)），`Total Bsmt SF` 有 1 个缺失值，`dropna` 删除后剩 2 929 行（[ch09/ch09.py:L164-L172](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L164-L172)）。本讲 4.1–4.3 只用 `Gr Liv Area` 一个特征预测 `SalePrice`；4.4 用全部 5 个特征。

## 4. 核心概念与源码讲解

### 4.1 LinearRegressionGD：回归版梯度下降

#### 4.1.1 概念说明

怎么找到让 MSE 最小的 \( w \) 和 \( b \)？第 2 章我们已经学过一个工具：**梯度下降**——沿着代价函数的负梯度方向一小步一小步往下走。本模块的手写类 `LinearRegressionGD` 就是把这套思路原封不动搬到回归任务上。

一个值得先建立的直觉：这个类与第 2 章的 `AdalineGD` 几乎是**同一个东西**。`AdalineGD` 训练时用恒等激活函数、以 SSE 为代价，这两点正是线性回归的定义；区别只在用途——`AdalineGD` 在 `predict` 里加了个 0.5 阈值把连续输出切成 0/1 类别（分类器），而 `LinearRegressionGD` 的 `predict` 直接返回连续值（回归器）。对照读这两个类，你会对「分类与回归共享同一套线性模型 + 梯度下降骨架」有更深的体会。

另外一个关键点：**必须先标准化**。`SalePrice` 的量级是 \(10^5\) 美元，`Gr Liv Area` 的量级是 \(10^3\) 平方英尺，代价曲面会扁得像峡谷，学习率稍大就发散（和 u2-l2 的实验结论一致）。所以代码对 X 和 y **分别**用 `StandardScaler` 缩放，预测完再用 `inverse_transform` 把标准化尺度上的预测值换算回美元。

#### 4.1.2 核心流程

```text
读入 Ames 数据 → 取 X = Gr Liv Area（n×1）、y = SalePrice（n,）
    ↓
sc_x.fit_transform(X)          # 特征标准化
sc_y.fit_transform(y[:, None]).flatten()   # 目标也标准化（二维进、一维出）
    ↓
LinearRegressionGD(eta=0.1).fit(X_std, y_std)
    每轮 epoch：
      output = X·w + b                    # 全体样本的预测
      errors = y - output                 # 残差向量
      w ← w + η · (2/n) · Xᵀ·errors       # 权重沿负梯度更新
      b ← b + η · 2 · errors.mean()       # 截距更新
      losses_.append( (errors**2).mean() )  # 记录本轮 MSE
    ↓
画 epoch–MSE 曲线；lin_regplot 画散点 + 回归线
    ↓
预测 2500 平方英尺的房价：先 transform 到标准化尺度 → predict → inverse_transform 还原成美元
```

其中梯度更新式来自对 MSE 求偏导（对照 u2-l2 的推导，只是把激活函数换成恒等）：

\[
\frac{\partial J}{\partial w} = -\frac{2}{n}X^T(y - \hat y), \qquad
\frac{\partial J}{\partial b} = -\frac{2}{n}\sum_i (y^{(i)} - \hat y^{(i)})
\]

#### 4.1.3 源码精读

**类的全貌**（[ch09/ch09.py:L212-L237](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L212-L237)）：整个 `LinearRegressionGD` 只有三个方法——`fit`（训练）、`net_input`（线性输出 \( Xw+b \)）、`predict`（直接返回 `net_input`，没有任何阈值或激活）。对比 `AdalineGD` 的定义 [ch02/ch02.py:L307-L390](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L307-L390)，你会发现它连 docstring 都更短——因为回归不需要讲激活函数的故事。

**fit 方法**（[ch09/ch09.py:L218-L231](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L218-L231)）：先用固定种子的 `RandomState` 把权重初始化为均值 0、标准差 0.01 的小随机数，截距初始化为 0；然后循环 `n_iter` 轮，每轮对**全部样本**整批计算一次梯度并更新。

**核心更新两行**（[ch09/ch09.py:L227-L228](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L227-L228)）：

```python
self.w_ += self.eta * 2.0 * X.T.dot(errors) / X.shape[0]
self.b_ += self.eta * 2.0 * errors.mean()
```

这与 `AdalineGD.fit` 的更新行 [ch02/ch02.py:L372-L373](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L372-L373) **逐字符相同**——`w` 的梯度是 \( \frac{2}{n}X^T e \)，`b` 的梯度是 \( 2\bar{e} \)。每轮的 MSE 追加进 `self.losses_`，供事后画收敛曲线。

**数据准备与双缩放器**（[ch09/ch09.py:L242-L259](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L242-L259)）：取出单特征 `X` 和目标 `y`；`StandardScaler` 默认按列处理二维数组，所以目标要先 `y[:, np.newaxis]` 变成 (n,1) 再缩放、`flatten()` 压回一维。随后以 `eta=0.1` 训练——注意这个学习率在标准化之前是灾难性的（u2-l2 里 0.1 就发散了），标准化之后则平稳收敛。

**损失曲线与拟合图**（[ch09/ch09.py:L264-L287](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L264-L287)）：画出 `losses_` 随 epoch 的变化（前几轮骤降、随后趋平）；`lin_regplot` 是个三行小工具——散点画原始数据、`plt.plot(X, model.predict(X))` 画回归线，本章反复复用（[ch09/ch09.py:L274-L277](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L274-L277)）。

**预测与还原**（[ch09/ch09.py:L292-L301](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L292-L301)）：预测 2 500 平方英尺房子的价格，流程是 `sc_x.transform([[2500]])` → `lr.predict` → `sc_y.inverse_transform`。notebook 保存的真实输出：`Sale price: $292507.07`；标准化尺度下 `Slope: 0.707, Intercept: -0.000`——截距约为 0 不是巧合：标准化后 y 的均值是 0，最优回归线必然穿过数据中心点 \((\bar x, \bar y) = (0, 0)\)。

#### 4.1.4 代码实践

**实践目标**：亲眼看一遍「不标准化会发散、标准化后收敛」在回归场景重演，并体会学习率的作用。

**操作步骤**（示例代码，可在 `ch09/` 目录下以 notebook 或脚本运行）：

1. 按 L242–L259 准备 `X_std`、`y_std`，用 `eta=0.1` 训练并画出 `losses_` 曲线。
2. 保持 `eta=0.1` 不变，但改为在**未标准化**的 `X`、`y` 上直接 `fit`，再画 `losses_`。
3. 回到标准化数据，把 `eta` 依次换成 `0.001` 和 `0.5`，各画一条损失曲线。

```python
# 步骤 2 示例代码（对照步骤 1 的标准化版本）
lr_raw = LinearRegressionGD(eta=0.1, n_iter=50)
lr_raw.fit(X, y)                    # 未标准化的原始尺度
plt.plot(range(1, 51), lr_raw.losses_)
plt.ylabel('MSE'); plt.xlabel('Epoch'); plt.yscale('log')
```

**需要观察的现象**：步骤 1 曲线平滑下降趋近 0 附近；步骤 2 的 MSE 在几轮内暴涨（可能触发溢出警告，纵轴需用对数尺度才能看清）；步骤 3 中 `eta=0.001` 收敛明显变慢、`eta=0.5` 仍应收敛但更抖。

**预期结果**：与 u2-l2 的 Adaline 实验结论一致——量纲悬殊时梯度下降几乎不可用，标准化后 \(\eta=0.1\) 恰到好处。步骤 2、3 的具体曲线形态**待本地验证**（未标准化版是否溢出取决于 numpy 版本的数值范围处理）。

#### 4.1.5 小练习与答案

**练习 1**：`LinearRegressionGD.predict` 与 `AdalineGD.predict` 的返回值有什么本质区别？各自的用途是什么？

答案：`LinearRegressionGD.predict` 直接返回 `net_input` 的连续值（房价），用于回归；`AdalineGD.predict` 在净输入上套阈值函数（大于 0.5 返回 1，否则 0），把连续值离散成类别，用于分类。两者的训练代码几乎相同，差别全在预测端的这一步离散化。

**练习 2**：为什么标准化后训练出的截距 `b_` 几乎等于 0？

答案：标准化后 \(\bar x = 0\)、\(\bar y = 0\)，而最小二乘解必然满足回归线穿过样本中心 \((\bar x, \bar y)\)，即 \( \bar y = w\bar x + b \Rightarrow 0 = 0 + b \)，所以 \( b = 0 \)。

**练习 3**：`fit` 里为什么把权重初始化为很小的随机数（scale=0.01）而不是 0？

答案：对单特征线性回归来说全零初始化其实也能收敛（梯度不为零）；小随机初始化是神经网络时代的通用习惯——对多特征回归，全零初始化的各权重梯度仍然不同（依赖各自特征列），尚可工作，但在多层网络里全零初始化会让所有神经元学到相同的东西。这里沿用该习惯并同时用 `random_state` 固定随机种子保证可复现。

### 4.2 LinearRegression 的 coef_ / intercept_ 与正规方程

#### 4.2.1 概念说明

梯度下降是**迭代逼近**，但线性回归的 MSE 是凸函数，其实存在**一步到位的解析解**（closed-form solution），即著名的**最小二乘法 / 正规方程（normal equations）**：在 X 前面拼一列 1（吸收截距），则

\[
\hat w = (X_b^T X_b)^{-1} X_b^T y
\]

sklearn 的 `LinearRegression` 内部基于最小二乘求解（实际用更数值稳定的 SVD 实现，不需要调学习率、也不需要标准化）。训练后它把学到的参数放在两个属性里：

- `coef_`：斜率数组（每个特征一个系数）；
- `intercept_`：截距标量。

注意命名差异：手写类叫 `w_` / `b_`，sklearn 叫 `coef_` / `intercept_`——后者是 sklearn 线性模型家族（后面 u9-l2 的 Ridge、Lasso 也一样）的统一约定。

#### 4.2.2 核心流程

```text
X（原始尺度，n×1）、y（原始尺度，美元）
    ↓
slr = LinearRegression(); slr.fit(X, y)     # 解析解，无需 eta、无需标准化
    ↓
slr.coef_       # 斜率：每增加 1 平方英尺，房价平均增加多少美元
slr.intercept_  # 截距：面积为 0 时的“基准价”（只是数学外推，未必有现实意义）
    ↓
验证：手工实现正规方程 (XᵀX)⁻¹Xᵀy，两边的斜率/截距应完全一致
```

#### 4.2.3 源码精读

**sklearn 拟合与系数读取**（[ch09/ch09.py:L313-L317](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L313-L317)）：

```python
slr = LinearRegression()
slr.fit(X, y)
y_pred = slr.predict(X)
print(f'Slope: {slr.coef_[0]:.3f}')
print(f'Intercept: {slr.intercept_:.3f}')
```

notebook 真实输出：`Slope: 111.666, Intercept: 13342.979`。斜率可直接解读为「其他条件不变时，居住面积每多 1 平方英尺，房价平均高约 111.7 美元」——**系数可解释性**正是线性回归在房价分析中长盛不衰的原因。注意这里用的是**原始尺度**的 X、y：解析解不需要学习率，标准化与否只影响参数的数值表达，不影响拟合的直线本身（本例单特征时拟合的直线在原始坐标系里完全一致，斜率不同只是坐标系不同）。

**原始尺度拟合图**（[ch09/ch09.py:L322-L328](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L322-L328)）：复用 `lin_regplot` 画散点与回归线，横轴回到「平方英尺」、纵轴回到「美元」，肉眼可检查直线是否穿过数据主趋势。

**正规方程对照**（[ch09/ch09.py:L336-L342](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L336-L342)）：

```python
Xb = np.hstack((np.ones((X.shape[0], 1)), X))   # 拼一列 1 吸收截距
w = np.zeros(X.shape[1])
z = np.linalg.inv(np.dot(Xb.T, Xb))
w = np.dot(z, np.dot(Xb.T, y))
```

这段「加一列 1 → 求 \((X_b^T X_b)^{-1}\) → 乘 \(X_b^T y\)」的代码输出 `Slope: 111.666, Intercept: 13342.979`，与 sklearn 完全一致——验证了 `LinearRegression` 就是最小二乘解析解的实现。

#### 4.2.4 代码实践

**实践目标**：验证「sklearn 结果 == 正规方程结果」，并练习解读系数。

**操作步骤**：

1. 运行 L313–L317 与 L336–L342 两段代码。
2. 在两段之后追加一行断言（示例代码）：

```python
# 示例代码：两个解应一致
assert np.allclose([slr.intercept_, slr.coef_[0]], w), '解析解与 sklearn 不一致'
print('两解一致:', w)
```

3. 用 `slr.coef_[0] * 500` 回答：「面积 500 平方英尺的房子比 400 平方英尺的贵多少？」

**需要观察的现象**：断言通过、无异常抛出。

**预期结果**：`两解一致: [13342.979... 111.665...]`；第 3 步答案约 \( 111.666 \times 100 \approx 11167 \) 美元（同截距抵消）。断言的通过情况**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：既然有解析解，为什么第 2 章（以及 4.1）还要学梯度下降版线性回归？

答案：(1) 正规方程要求计算矩阵求逆，特征数很大时 \((X^TX)^{-1}\) 的代价是 \(O(m^3)\)，而梯度下降在超大特征量/数据量下（配合随机化）更实用；(2) 梯度下降是逻辑回归（u3-l1）、神经网络（第 11 章）等**没有解析解**的模型的通用训练方式，线性回归是最好的教学载体。

**练习 2**：截距 13 342.98 美元的现实含义是什么？能说「0 平方英尺的房子值 1.3 万美元」吗？

答案：不能。截距只是直线与 y 轴交点的数学外推，`Gr Liv Area` 的实际范围是 334–5 642 平方英尺（数据文件可验证），0 远在数据之外，线性关系在那里不成立。对数据范围内的解释只应使用斜率。

**练习 3**：`slr.coef_` 为什么是数组而 `slr.intercept_` 是标量？

答案：每个输入特征各有一个斜率系数（本例只有 `Gr Liv Area` 一个特征，所以 `coef_` 长度为 1），而无论多少个特征，全局只有一个截距。

### 4.3 RANSACRegressor：离群掩码与鲁棒回归

#### 4.3.1 概念说明

回头看 4.2 的散点图：数据云里混着一些「面积很大却卖得极便宜」的点（数据文档说明这类异常成交确实存在）。MSE 用平方惩罚误差，这些离群点会强烈拉扯回归线。**RANSAC（随机样本共识）**换了一种思路：

1. 随机抽一小撮样本拟合一个候选模型；
2. 用候选模型检查**全体**样本：残差小于阈值 `residual_threshold` 的算「一致者」（inliers，内点），其余算「离群者」（outliers）；
3. 重复 `max_trials` 轮，保留**内点最多**的那个候选；
4. 最后**只用内点**重新拟合，得到最终模型。

直觉：真实趋势由「大多数正常点」决定，离群点因为无法和大多数点同时达成一致，会自然被淘汰。RANSAC 不改变基模型（仍是 `LinearRegression`），只改变「用哪些样本去拟合」，因此属于**鲁棒回归**的元算法。拟合结束后，每个样本是否为内点记录在 `inlier_mask_`（布尔数组，长度等于样本数）——这就是本模块标题里的「离群掩码」，用它做 `np.logical_not` 即得离群点掩码。

#### 4.3.2 核心流程

```text
RANSACRegressor(LinearRegression(),
                max_trials=100,        # 最多尝试 100 个候选模型
                min_samples=0.95,      # 每轮随机抽取 95% 的样本拟合候选
                loss='absolute_error', # 用绝对误差（而非平方）衡量残差
                residual_threshold=None,  # None → 用 y 的 MAD 自动定阈值
                random_state=123)
    ↓ fit(X, y)
每轮：随机抽 min_samples 比例的样本 → 拟合候选直线 → 数内点
    ↓
保留内点最多者 → 用其内点集合重拟合 → estimator_（最终的 LinearRegression）
    ↓
ransac.inlier_mask_      # 布尔掩码：True=内点
outlier_mask = np.logical_not(inlier_mask)
    ↓
ransac.estimator_.coef_ / .intercept_   # 只属于内点的回归系数
```

#### 4.3.3 源码精读

**构造与拟合**（[ch09/ch09.py:L352-L360](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L352-L360)）：五个参数逐一看——`max_trials=100` 是默认值；`min_samples=0.95` 以比例形式传入，表示每轮随机抽约 95% 的样本参与拟合候选模型（取大头是为了让候选更稳，代价是「最小抽样」的随机性减弱）；`loss='absolute_error'` 用绝对误差计残差，天然比平方误差对大偏差更不敏感；`residual_threshold=None` 交给 sklearn 自动估计——默认用目标值 y 的 **MAD（median absolute deviation，中位数绝对偏差）**，即 \( \mathrm{median}(|y_i - \mathrm{median}(y)|) \)，比标准差更抗离群点污染。

**掩码的提取**（[ch09/ch09.py:L362-L363](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L362-L363)）：

```python
inlier_mask = ransac.inlier_mask_
outlier_mask = np.logical_not(inlier_mask)
```

`inlier_mask_` 是与样本等长的布尔数组；`np.logical_not` 按位取反得到离群掩码。这两个掩码既是可视化工具，也可以导出做进一步的数据审查。

**内点/离群点分色可视化**（[ch09/ch09.py:L365-L380](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L365-L380)）：用 `X[inlier_mask]`、`X[outlier_mask]` 布尔索引把两群点分别画成蓝色圆点与绿色方块（见配图 [figures/09_09.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_09.png)），再叠加 `ransac.predict` 的回归线。**注意一个代码细节**：`line_X = np.arange(3, 10, 1)` 只覆盖 x∈[3, 9]，而 `Gr Liv Area` 的实际范围是 334–5 642 平方英尺，这条黑线在图上几乎缩在原点处不可见——推测是作者从本书前版（对应特征的取值恰在该区间的数据集）沿袭下来的遗留写法，实践中建议改成 `np.arange(X.min(), X.max()+1, 100)` 之类覆盖数据范围的网格（综合实践里我们会修正它）。

**RANSAC 系数与 OLS 的对比**（[ch09/ch09.py:L385-L386](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L385-L386)）：最终模型藏在 `ransac.estimator_` 里。notebook 真实输出：`Slope: 106.348, Intercept: 20190.093`——对比 OLS 的 `111.666 / 13342.979`，斜率变小、截距变大：被剔除的离群点（大面积低价房）原本把线往下压。

**手动估计阈值尺度**（[ch09/ch09.py:L391-L394](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L391-L394)）：

```python
def mean_absolute_deviation(data):
    return np.mean(np.abs(data - np.mean(data)))
```

这个小工具算的是 y 相对其**均值**的平均绝对偏差，用来感受「目标值的典型波动量级」，帮助人工选阈值。注意它名字里的 deviation 是均值口径，与 sklearn 内部（`residual_threshold=None` 时）用的**中位数**口径 MAD 是两个不同的统计量，不要混淆。

**第二次实验：手工指定阈值**（[ch09/ch09.py:L399-L432](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L399-L432)）：把 `residual_threshold` 从 `None` 改为 `65000`（参考上一段的尺度估计取的整值），其余参数不变重新拟合。notebook 真实输出：`Slope: 105.631, Intercept: 18314.587`，与 MAD 自动阈值的解接近但不相同——**阈值是 RANSAC 最敏感的旋钮**：阈值越大，越多点被判为内点，RANSAC 越接近普通 OLS；阈值越小，剔除越激进，模型越「只信主流样本」。

#### 4.3.4 代码实践

**实践目标**：把掩码用起来——统计内点占比，并直观感受阈值对内点数量的影响。

**操作步骤**（示例代码）：

```python
# 示例代码：接在两次 RANSAC 拟合之后
for name, mask in [('MAD 自动阈值', inlier_mask), ('threshold=65000', inlier_mask2)]:
    ratio = mask.sum() / mask.shape[0]
    print(f'{name}: 内点 {mask.sum()} / {mask.shape[0]} ({ratio:.1%})')
```

1. 按 L352–L363 拟合第一个 RANSAC（自动阈值），记下内点数。
2. 按 L399–L409 拟合第二个（`residual_threshold=65000`），分别保存两次的 `inlier_mask_`（示例代码里记作 `inlier_mask2`）。
3. 运行上面的统计，再取 `threshold=30000` 与 `threshold=200000` 各拟合一次，观察内点占比如何随阈值单调变化。

**需要观察的现象**：阈值从 30 000 → 65 000 → 200 000，内点占比从小到大单调上升；自动（MAD）阈值的内点数介于其中某处。

**预期结果**：`Gr Liv Area` 与 `SalePrice` 大体线性相关，绝大多数点应被判为内点（占比通常在 80% 以上）；各阈值下的确切占比**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：RANSAC 改变了基模型 `LinearRegression` 本身吗？最终用于预测的模型存在哪个属性里？

答案：没有改变。RANSAC 是包裹在基模型外的元算法，负责挑选「用哪些样本」拟合；最终只用内点重拟合出的 `LinearRegression` 存在 `ransac.estimator_` 中，`ransac.predict` 内部就是调用它。

**练习 2**：为什么 `loss` 选 `'absolute_error'` 而不是平方误差？`residual_threshold=None` 时 sklearn 用什么统计量定阈值、它为何抗离群？

答案：绝对误差对大残差不平方放大，让「数内点」这一步不被个别巨离样本牵制；阈值为 None 时用 y 的中位数绝对偏差 MAD = median(|y − median(y)|)，中位数本身不受极端值影响，因此用它估计「正常波动范围」比标准差稳健。

**练习 3**：如果 `residual_threshold` 设得极大（比如 \(10^9\)），RANSAC 会退化成什么？

答案：所有样本残差都小于阈值、全部算内点，内点数恒为最大值，第一次随机子集拟合的模型就会被保留——结果近似于（但随机子集上的）普通最小二乘，RANSAC 的鲁棒性完全失效。

### 4.4 残差图与 MSE / R² 诊断

#### 4.4.1 概念说明

模型拟合完了，怎么知道它好不好？回归诊断有两层：

**一层是「看图」——残差图（residual plot）**。横轴为预测值 \(\hat y\)、纵轴为残差 \( y - \hat y \)，并画一条 0 参考线。理想形态：点随机均匀散布在 0 线两侧、无明显形状。三种典型病灶：

- 残差随 \(\hat y\) 增大而发散（**漏斗形**）→ 异方差，误差方差不等；
- 残差呈弯曲的带状 → 存在非线性关系，直线不够用（u9-l2 会用多项式、树模型解决）；
- 个别点残差巨大 → 离群点，正是 4.3 RANSAC 处理的对象。

**另一层是「算数」——三个指标**：

\[ \mathrm{MSE} = \frac{1}{n}\sum_i (y^{(i)} - \hat y^{(i)})^2, \qquad
\mathrm{MAE} = \frac{1}{n}\sum_i |y^{(i)} - \hat y^{(i)}| \]

\[ R^2 = 1 - \frac{SS_{res}}{SS_{tot}} = 1 - \frac{\sum_i (y^{(i)} - \hat y^{(i)})^2}{\sum_i (y^{(i)} - \bar y)^2} \]

MSE / MAE 的量纲是美元的平方 / 美元，**只能在同一数据集上横向比较模型**；R² 则回答「比起『无脑预测均值』这个基准，模型把不可解释的方差减少了多少」：1 为完美，0 等于均值基准，越接近 1 越好。注意 MSE 对大误差平方放大（受离群点影响大），MAE 更贴近「平均差多少钱」的直觉。这一节还把模型从 1 个特征扩到全部 5 个特征，并首次引入**训练集 / 测试集划分**——防止「训练集上拟合得好」误导我们（思想承接 u6-l1 的评估纪律）。

#### 4.4.2 核心流程

```text
X = 全部 5 个特征（含编码后的 Central Air）；y = SalePrice
    ↓
train_test_split(test_size=0.3, random_state=123)   # 注意：未 stratify（回归无类别可分层）
    ↓
LinearRegression().fit(X_train) → y_train_pred / y_test_pred
    ↓
残差图：左子图测试集、右子图训练集，散点 (ŷ, ŷ − y)，加 y=0 参考线
    ↓
mean_squared_error / mean_absolute_error / r2_score 分别在 train、test 上计算
```

#### 4.4.3 源码精读

**特征扩展与划分**（[ch09/ch09.py:L442-L449](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch09/ch09.py#L442-L449)）：`target = 'SalePrice'`、其余 5 列全做特征；70/30 划分。与分类场景的一个细节差异：`train_test_split` **没有传 `stratify`**——分层抽样依赖离散类别比例，回归的连续目标无类可分。

**拟合与预测**（[ch09/ch09.py:L454-L458](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L454-L458)）：对训练集 `fit`，然后**分别**对训练集与测试集 `predict`——评估的要义是同一模型在两套数据上的对照。

**残差图**（[ch09/ch09.py:L463-L484](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L463-L484)）：先取两侧预测值的公共范围 `x_max` / `x_min` 让参考线贯通两个子图；`plt.subplots(1, 2, sharey=True)` 并排画测试集（左、方块）与训练集（右、圆点），纵轴都是 `y_pred - y` 的残差，`ax.hlines(y=0, ...)` 画黑色 0 参考线（效果见 [figures/09_11.png](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/figures/09_11.png)）。一个容易看反的细节：**左图才是测试集**，`ax1` 涂的是 `y_test_pred - y_test`。

**三组指标**（[ch09/ch09.py:L491-L514](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L491-L514)）：`mean_squared_error`、`mean_absolute_error`、`r2_score` 的调用方式完全一致——传入 (真实值, 预测值)。notebook 真实输出：

| 指标 | 训练集 | 测试集 |
| --- | --- | --- |
| MSE | 1 497 216 245.85 | 1 516 565 821.00 |
| MAE | 25 983.03 | 24 921.29 |
| R² | 0.77 | 0.75 |

解读：训练 / 测试 R² 相当接近（0.77 vs 0.75），说明这个 5 特征线性模型**没有明显过拟合**，误差主要来自「模型太简单」——残差图里能看到散点随预测值增大而发散的异方差趋势与个别巨大残差，这些都是 u9-l2 引入正则化与非线性模型的动机。MSE 数值巨大（美元平方的量纲）再次提醒：**MSE 只用于相对比较，MAE 才能直接读成「平均差约 2.5 万美元」**。

#### 4.4.4 代码实践

**实践目标**：学会亲手画残差图，并把残差诊断与 RANSAC 的离群掩码串起来。

**操作步骤**（示例代码）：

1. 按 L442–L458 准备 `y_train_pred`、`y_test_pred`。
2. 复刻 L463–L484 的双子图残差图（建议顺手把 `ax1`/`ax2` 的 `label` 改清楚，避免看反）。
3. 追加分析（示例代码）：找出测试集残差绝对值最大的 5 个样本，再与 4.3 中 RANSAC 的 `outlier_mask` 对应样本求交集。

```python
# 示例代码：残差极端样本 vs RANSAC 离群点
resid_test = np.abs(y_test - y_test_pred)
worst_idx = np.argsort(resid_test)[-5:]
# 需要保留 train_test_split 返回的原始下标才能对齐（见下方说明）
```

**需要观察的现象**：残差点大体随机分布在 0 线附近，但右侧（高房价预测值）散布明显更宽；残差最大的样本与 RANSAC 判定的离群点高度重合。

**预期结果**：与书中图 09_11 一致的喇叭口形态；第 3 步的具体交集数量**待本地验证**（`train_test_split` 不返回原始索引，需在划分前用 `np.arange(len(y))` 作为伴随数组一并切分，或改用自行记录下标的方式对齐）。

#### 4.4.5 小练习与答案

**练习 1**：某模型训练集 R² = 0.95、测试集 R² = 0.50，诊断它的问题并给出至少两条改进方向。

答案：训练 / 测试差距大是典型**高方差（过拟合）**。可尝试：减少特征或用 u9-l2 的正则化（Ridge/Lasso）收缩系数、收集更多数据、换更简单的模型。残差图上训练集残差应明显更小。

**练习 2**：为什么 R² 的基准（\(SS_{tot}\) 用均值预测）让它「天然抗盲目乐观」？R² 可能为负吗？

答案：\(SS_{tot}\) 是「永远预测样本均值」这一最笨模型的残差平方和；R² 度量的是相对它的改进，若模型连均值基准都不如（\(SS_{res} > SS_{tot}\)），R² 为负。在训练集上 R² ≥ 0（最小二乘不会差过均值），但在测试集上完全可能为负——这本身就是过拟合的报警信号。

**练习 3**：本节 `train_test_split` 为什么不像 u6-l1 那样传 `stratify=y`？

答案：分层抽样要求 y 是离散类别，按类别比例切分；回归的 y 是连续值（房价），没有类别比例可言，所以回归划分只控制 `test_size` 与随机种子。

## 5. 综合实践

**任务**：对 Ames 住房数据，用**普通最小二乘**与 **RANSAC** 分别拟合 `Gr Liv Area → SalePrice`，绘制两者的残差图对比，并统计 RANSAC 内点占总样本的比例——把本讲四个模块串成一条完整流水线。

下面是完整的可运行脚本（示例代码，基于本地数据副本，避免依赖外网下载）：

```python
# 示例代码：u9-l1 综合实践
# 在 ch09/ 目录下运行，或把路径改为指向 ch09/AmesHousing.txt
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from sklearn.linear_model import LinearRegression, RANSACRegressor

# 1. 数据准备（等价于 ch09.py L142-L172，改读本地副本）
columns = ['Overall Qual', 'Overall Cond', 'Gr Liv Area',
           'Central Air', 'Total Bsmt SF', 'SalePrice']
df = pd.read_csv('AmesHousing.txt', sep='\t', usecols=columns)
df['Central Air'] = df['Central Air'].map({'N': 0, 'Y': 1})
df = df.dropna(axis=0)
X = df[['Gr Liv Area']].values
y = df['SalePrice'].values

# 2. 普通最小二乘（ch09.py L313-L317）
ols = LinearRegression().fit(X, y)

# 3. RANSAC（ch09.py L399-L404，手工阈值）
ransac = RANSACRegressor(LinearRegression(), max_trials=100,
                         min_samples=0.95, loss='absolute_error',
                         residual_threshold=65000, random_state=123)
ransac.fit(X, y)
inlier_mask = ransac.inlier_mask_
print(f'OLS     斜率 {ols.coef_[0]:.3f}  截距 {ols.intercept_:.3f}')
print(f'RANSAC  斜率 {ransac.estimator_.coef_[0]:.3f}  截距 '
      f'{ransac.estimator_.intercept_:.3f}')
print(f'内点 {inlier_mask.sum()} / {len(y)} '
      f'({inlier_mask.sum()/len(y):.1%})')

# 4. 两个模型的残差图对比（改写 ch09.py L463-L484）
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 3.5), sharey=True)
for ax, model, name in [(ax1, ols, 'OLS'), (ax2, ransac, 'RANSAC')]:
    resid = y - model.predict(X)
    ax.scatter(model.predict(X), resid, c='steelblue', s=15, alpha=0.5)
    ax.hlines(y=0, xmin=0, xmax=model.predict(X).max(), color='black', lw=2)
    ax.set_title(f'{name} residuals'); ax.set_xlabel('Predicted values')
ax1.set_ylabel('Residuals')
plt.tight_layout(); plt.show()
```

**操作步骤**：

1. 通读脚本，把每一段与正文引用的 `ch09.py` 行号对应起来（数据 L142–L172、OLS L313–L317、RANSAC L399–L409、残差图 L463–L484）。
2. 运行脚本，记录打印的两组斜率 / 截距与内点占比。
3. 对比两张残差图：数一数纵轴方向超出 ±10 万美元的极端残差点，分别在两张图里有多少。
4. 进阶（可选）：把 `residual_threshold` 换成 `None`（MAD 自动阈值）重跑一遍，比较内点占比与残差图的变化；再顺手把 4.3 提到的 `line_X = np.arange(3, 10, 1)` 遗留问题改成覆盖数据范围的网格，重画一张 RANSAC 拟合图。

**需要观察 / 预期结果**：

- 斜率：OLS 约 111.7、RANSAC 约 105.6 美元/平方英尺（notebook 真实输出为 111.666 与 105.631，你的本地值应与之几乎一致）。
- RANSAC 残差图中超出 ±10 万美元的极端点明显少于 OLS 图——被剔除的离群点不再参与拟合，其残差在图中「摊平」不再牵动回归线。
- 内点占比应为大多数（预计 80% 以上，确切数值**待本地验证**）。

## 6. 本讲小结

- **回归与分类共享骨架**：`LinearRegressionGD`（[ch09.py:L212-L237](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch09/ch09.py#L212-L237)）的梯度更新式与第 2 章 `AdalineGD` 逐字符相同，区别只在预测端是否离散化；量纲悬殊时必须先标准化，y 也要缩放、预测后用 `inverse_transform` 还原。
- **线性回归有解析解**：sklearn `LinearRegression` 训练后把参数放进 `coef_`（斜率）与 `intercept_`（截距），书中用正规方程 \( \hat w = (X_b^T X_b)^{-1}X_b^T y \) 手工复算验证一致（111.666 / 13342.979）；斜率单位「美元/平方英尺」可直接解读。
- **RANSAC 用掩码换鲁棒**：反复「随机子集拟候选 → 按残差阈值数内点 → 保留最优 → 只用内点重拟合」，最终模型在 `ransac.estimator_`，内点布尔掩码在 `ransac.inlier_mask_`；`residual_threshold` 是最敏感的旋钮，None 时用 y 的 MAD 自动估计。
- **残差图是回归的第一诊断工具**：横轴预测值、纵轴残差，看点是否随机散布于 0 线两侧；漏斗形 → 异方差，弯曲 → 非线性，极端点 → 离群点。
- **指标要配套使用**：MSE（美元平方，只做相对比较）、MAE（平均差多少钱）、R²（相对均值基准的方差削减比例，测试集上可为负）；5 特征模型 train/test R² = 0.77/0.75，欠拟合而非过拟合，为下一讲埋下伏笔。
- **带着怀疑读代码**：RANSAC 绘图里的 `line_X = np.arange(3, 10, 1)` 与特征范围 334–5 642 严重不匹配，是遗留写法——书码也会有瑕疵，核对数据范围是必备习惯。

## 7. 下一步学习建议

- **下一讲 u9-l2（正则化回归与树模型回归）**：本讲残差图暴露的「非线性 + 异方差」问题，将由 Ridge/Lasso/ElasticNet 的系数收缩（对比 `coef_` 如何被压向 0）与 `RandomForestRegressor` / `XGBRegressor` 的非线性回归接手；阅读 `ch09.py` L518 起的正则化一节作为预习。
- **回看 u6-l1**：本讲的评估纪律（训练/测试划分、只在测试集上算最终指标）正是 u6-l1 Pipeline 思想在回归里的最简形态；等学完 u9-l2 后，可以尝试把 `StandardScaler + Ridge` 组装成 `Pipeline` 再做交叉验证调 `alpha`。
- **延伸阅读源码**：`ch09/ch09.py` L599–L721（本讲范围之后）演示了多项式回归与树回归如何拟合非线性关系，可先跑图感受「直线 → 曲线 → 分段常数」的演进，为 u9-l2 做铺垫。
