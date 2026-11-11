# scikit-learn 初体验:训练范式与逻辑回归

## 1. 本讲目标

学完本讲,你应该能够:

1. 掌握 scikit-learn 的 `fit` / `predict` / `score` 通用训练范式,理解任何估计器都遵守同一套接口约定。
2. 会用 `train_test_split` 做带分层抽样(stratify)的训练/测试划分,并能验证划分后类别比例不变。
3. 理解 `StandardScaler` 在「训练集拟合、测试集只变换」流程中的位置,以及它为什么能防止测试集信息泄漏。
4. 读懂手写版 `LogisticRegressionGD`:sigmoid 激活、对数似然(log loss)代价函数,以及它与上一讲 `AdalineGD` 惊人相似的梯度更新式。
5. 理解 L2 正则化强度参数 `C` 与过拟合的关系,会用权重系数轨迹图观察正则化的影响。

本讲是「第 3 章:scikit-learn 分类器漫游」的第一站,从此我们不再只用手写类,而是把「自己实现」和「调库」两条腿并起来走路。

## 2. 前置知识

本讲默认你已完成 u2 系列讲义(感知机、AdalineGD、AdalineSGD),以下概念会被直接使用:

- **净输入(net input)**:线性加权求和 \( z = \boldsymbol{w}^{\mathsf{T}}\boldsymbol{x} + b \)。感知机和 Adaline 都先算它,再决定如何激活。
- **梯度下降**:沿代价函数梯度的反方向小步更新权重,`w += eta * 梯度项`。
- **特征标准化(z-score)**:每列减均值、除标准差。u2-l3 已证明未标准化数据会让梯度下降收敛困难,本讲的 `StandardScaler` 是同一件事的 scikit-learn 版。
- **训练集/测试集**:用没见过的数据评估模型,才能反映泛化能力。

本讲的新概念会边讲边解释:

- **估计器(estimator)**:scikit-learn 里所有模型对象的统称,约定俗成地提供 `fit`(从数据学参数)、`predict`(对新数据给预测)等方法。
- **几率(odds)与 logit**:事件发生与不发生的概率之比 \( \frac{p}{1-p} \),取对数后叫 logit。逻辑回归就是把线性净输入建模成 logit。
- **正则化(regularization)**:在代价函数里加一个「惩罚权重过大」的项,抑制过拟合。

## 3. 本讲源码地图

本讲只涉及一个源码文件(这正是 scikit-learn 风格的好处:代码极简):

| 文件 | 作用 |
| --- | --- |
| [ch03/ch03.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py) | 第 3 章配套脚本:iris 加载与划分、标准化、sklearn 感知机、手写 `LogisticRegressionGD`、sklearn `LogisticRegression` 与正则化实验,后半部分(SVM、决策树、KNN)留给后续讲义 |

同目录下的 `ch03.ipynb` 是等价的 notebook 版本(u1-l1 讲过双轨关系):想在讲义代码之间穿插实验就开 notebook,想核对行号就开 `.py`。

## 4. 核心概念与源码讲解

### 4.1 scikit-learn 第一步:fit/predict/score 范式与 train_test_split 划分

#### 4.1.1 概念说明

scikit-learn 的核心设计是**统一的估计器接口**:

- `estimator.fit(X_train, y_train)`:从训练数据学习参数(感知机学权重、树学分裂点)。
- `estimator.predict(X_test)`:对新样本输出类别标签。
- `estimator.score(X_test, y_test)`:分类器默认返回准确率,一步完成「预测 + 比对」。

这意味着你学会了训练一个模型,就几乎学会了训练所有模型——把 `Perceptron` 换成 `LogisticRegression` 只是换个类名,三步曲不变。本讲先验证这一点,再进入逻辑回归。

在训练之前必须先回答一个问题:**用什么数据评估?** 如果用训练时见过的数据评估,得到的准确率会虚高(模型可能只是「背下了」训练样本)。标准做法是把数据集切成两部分:

- **训练集**(这里 70%):喂给 `fit`。
- **测试集**(这里 30%):模拟「未来新数据」,只在最后评估时使用。

还有一个容易忽视的细节:**分层抽样(stratified sampling)**。iris 三个品种各 50 条,随机划分可能碰巧让某个品种在测试集里偏多或偏少,让评估失真。`stratify=y` 参数保证训练集和测试集内部的类别比例与原始数据一致。

#### 4.1.2 核心流程

```text
load_iris() 取花瓣长度/宽度两列
        │
        ▼
train_test_split(test_size=0.3, stratify=y)
        │
        ├── X_train, y_train(70%,每类 35 条)
        └── X_test,  y_test(30%,每类 15 条)
        │
        ▼
np.bincount 逐级核对类别数量
        │
        ▼
Perceptron.fit(X_train_std, y_train)   ← 4.2 标准化后再训练
        │
        ▼
y_pred = predict(X_test_std)
        │
        ├── accuracy_score(y_test, y_pred)   ← 两条等价路径
        └── ppn.score(X_test_std, y_test)
```

#### 4.1.3 源码精读

**第一步:加载数据并取两列特征。**

[ch03/ch03.py:93-97](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L93-L97) 从 scikit-learn 内置的 iris 数据集中取第 2、3 列(花瓣长度、花瓣宽度),标签 `y` 已经是整数 0/1/2:

```python
iris = datasets.load_iris()
X = iris.data[:, [2, 3]]
y = iris.target
print('Class labels:', np.unique(y))
```

与 u1-l3 用 pandas 读 `iris.data` 文本不同,这里的数据来自 sklearn 内置对象,省去表头处理和 `np.where` 手工编码——标签天然是整数。

**第二步:70/30 分层划分。**

[ch03/ch03.py:105-106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L105-L106) 完成划分,三个关键参数各司其职:

```python
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.3, random_state=1, stratify=y)
```

- `test_size=0.3`:测试集占 30%(105 条训练 / 45 条测试)。
- `random_state=1`:固定随机种子,保证任何人任何时刻重跑都得到同一份划分——这是科学可复现的关键。
- `stratify=y`:按标签分层,三个品种比例保持 1:1:1。

[ch03/ch03.py:111-113](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L111-L113) 用 `np.bincount` 验证分层生效:

```python
print('Labels counts in y:', np.bincount(y))
print('Labels counts in y_train:', np.bincount(y_train))
print('Labels counts in y_test:', np.bincount(y_test))
```

`np.bincount` 统计每个非负整数出现的次数。由于 50×0.7=35、50×0.3=15 都是整数,输出应为 `[50 50 50]` / `[35 35 35]` / `[15 15 15]`(待本地验证)。

**第三步:fit / predict / score 三步曲(以 sklearn 感知机为例)。**

[ch03/ch03.py:133-151](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L133-L151) 是整个范式的最小示例:

```python
ppn = Perceptron(eta0=0.1, random_state=1)
ppn.fit(X_train_std, y_train)

y_pred = ppn.predict(X_test_std)
print('Misclassified examples: %d' % (y_test != y_pred).sum())
print('Accuracy: %.3f' % accuracy_score(y_test, y_pred))
print('Accuracy: %.3f' % ppn.score(X_test_std, y_test))
```

- `eta0` 就是学习率 η(加 `0` 是为了与「每轮衰减后的学习率」区分,感知机内部不衰减)。
- `(y_test != y_pred).sum()` 直接数出预测错了几条;书中示例输出为错分 1 条、准确率 0.978(45 条测对 44 条,待本地验证)。
- 最后两行完全等价:`score` 内部就是先 `predict` 再算准确率。

注意一个与 u2-l1 的有趣差异:ch02 手写的 `Perceptron` 只能二分类(`predict` 输出 0/1),而 sklearn 的 `Perceptron` 在 `fit` 时看到 3 个类别,自动采用 OvR(one-vs-rest)策略训练多个二分类器——库替你处理了多类扩展。

#### 4.1.4 代码实践

**实践目标**:亲手验证「分层划分保持类别比例」以及 `random_state` 的作用。

**操作步骤**(在仓库根目录启动 Python 或新建 notebook 单元):

1. 复制 [ch03/ch03.py:93-106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L93-L106) 的加载与划分代码并运行,打印三行 `bincount`。
2. 把 `stratify=y` 删掉再跑一次,对比 `y_test` 的 bincount。
3. 把 `test_size` 改成 `0.4`,重跑第 1 步。

**需要观察的现象**:

- 第 1 步:三个集合的 bincount 是整齐的 `[50 50 50]` / `[35 35 35]` / `[15 15 15]`。
- 第 2 步:去掉分层后测试集 bincount 通常不再是严格的 `[15 15 15]`(具体数值待本地验证)。
- 第 3 步:训练/测试变为 90/60 条。

**预期结果**:分层抽样保证划分后类别比例与整体一致;`random_state=1` 不变时多次运行结果完全相同。

#### 4.1.5 小练习与答案

**练习 1**:为什么必须固定 `random_state`?如果两个研究者在不同种子下划分数据,可能发生什么?

**答案**:不固定种子,每次划分的训练/测试集不同,评估出的准确率会波动,结果无法复现也无法对比。不同种子下某人运气好、测试集恰好简单,可能得出「模型 A 优于 B」的错误结论。

**练习 2**:ch02 手写感知机只能处理两个类别,sklearn 的 `Perceptron` 却能对 3 类 iris 训练。它内部最可能采用什么策略?

**答案**:OvR(one-vs-rest)多分类策略——为每个类别训练一个「本类 vs 其余所有类」的二分类器,预测时取得分最高的那个二分类器对应的类别。

### 4.2 StandardScaler:训练集拟合、测试集只变换

#### 4.2.1 概念说明

u2-l3 已经从优化角度证明:标准化让代价曲面从扁椭圆变近似同心圆,学习率可选范围大幅放宽。本讲把同一件事放进 scikit-learn 工作流,重点不在「怎么算」而在「在哪里算」:

- `sc.fit(X_train)`:只在**训练集**上计算每列的均值和标准差。
- `sc.transform(X_train)` / `sc.transform(X_test)`:用**训练集的**统计量变换两份数据。

为什么不 `fit` 测试集?因为测试集模拟「未来的未知数据」——真实部署时它甚至还不存在。更致命的是,如果标准化时用了测试集的统计量,测试集的分布信息就悄悄混进了流程,这叫**数据泄漏(data leakage)**,评估分数会系统性偏乐观。这就是 u2-l3 提过的 `fit_transform` / `transform` 分离原则,在完整工作流中的样子。

#### 4.2.2 核心流程

```text
X_train ──fit──▶ 记录 μ_train, σ_train
X_train ──transform(μ_train, σ_train)──▶ X_train_std(每列均值≈0,标准差≈1)
X_test  ──transform(μ_train, σ_train)──▶ X_test_std(均值、标准差不必是 0/1!)
```

#### 4.2.3 源码精读

[ch03/ch03.py:121-124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L121-L124) 是全书记载的标准写法:

```python
sc = StandardScaler()
sc.fit(X_train)
X_train_std = sc.transform(X_train)
X_test_std = sc.transform(X_test)
```

后续所有基于距离/梯度的模型(感知机、逻辑回归、SVM、KNN)都在 `X_train_std` / `X_test_std` 上训练评估;而决策树、随机树这类基于阈值切分的模型对本讲稍后的章节则直接用原始 `X_train`——标准化对它们不是必需的。

#### 4.2.4 代码实践

**实践目标**:亲眼确认「训练集变换后均值 0、标准差 1,测试集变换后不是」。

**操作步骤**(示例代码,接在上一步的环境中进行):

```python
print('训练集均值:', X_train_std.mean(axis=0), '标准差:', X_train_std.std(axis=0))
print('测试集均值:', X_test_std.mean(axis=0), '标准差:', X_test_std.std(axis=0))
```

**需要观察的现象**:训练集每列均值 ≈ 0、标准差 ≈ 1(浮点误差量级);测试集均值偏离 0、标准差偏离 1。

**预期结果**:测试集统计量不是 0/1 是**正确且必要**的——它被训练集的统计量变换,这正是我们想要的行为。若测试集也恰好是 0/1,反而说明泄漏发生了(待本地验证)。

#### 4.2.5 小练习与答案

**练习**:如果不小心写了 `sc.fit(X_test)` 再变换两份数据,模型的测试准确率会更高、更低还是不变?为什么这个分数不可信?

**答案**:通常更高(或至少虚高)。测试集统计量参与变换后,测试样本的分布被「拉向」模型所见的训练分布,评估条件比真实部署时更有利;这个分数混入了测试集信息,不能反映模型对真正未知数据的泛化能力。

### 4.3 LogisticRegressionGD:sigmoid 与对数似然损失的从零实现

#### 4.3.1 概念说明

Adaline 的输出是连续实数,再和阈值 0.5 比较得到类别;感知机的阶跃函数不可导,无法做梯度下降。逻辑回归在这两者之间找到了黄金平衡点:用 **sigmoid(logistic)函数**把净输入压到 (0, 1) 区间,直接解释为**类别概率**:

\[ \sigma(z) = \frac{1}{1 + e^{-z}} \]

- \( z \to +\infty \) 时 \( \sigma(z) \to 1 \),\( z \to -\infty \) 时 \( \sigma(z) \to 0 \),\( z = 0 \) 时 \( \sigma(z) = 0.5 \)。
- 它单调、光滑、可导,处处有良好定义的梯度。

从几率(odds)角度理解更直观:事件概率 \( p \) 的几率是 \( \frac{p}{1-p} \),取对数得 logit 函数 \( \log\frac{p}{1-p} \)。逻辑回归假设 logit 是特征的线性函数:

\[ \log\frac{p(y=1\mid\boldsymbol{x})}{1-p(y=1\mid\boldsymbol{x})} = \boldsymbol{w}^{\mathsf{T}}\boldsymbol{x} + b \quad\Longleftrightarrow\quad p(y=1\mid\boldsymbol{x}) = \sigma(\boldsymbol{w}^{\mathsf{T}}\boldsymbol{x} + b) \]

训练用**对数似然损失(log loss,也叫二元交叉熵)**,对 n 个样本取平均:

\[ L(\boldsymbol{w}, b) = -\frac{1}{n}\sum_{i=1}^{n}\Big[ y^{(i)} \log\big(\sigma(z^{(i)})\big) + \big(1-y^{(i)}\big)\log\big(1-\sigma(z^{(i)})\big) \Big] \]

直觉:如果真实标签 \( y=1 \) 而模型只给了 \( \sigma(z)=0.01 \),损失是 \( -\log(0.01)\approx 4.6 \),惩罚很重;预测对了则损失趋近 0。它是凸函数,梯度下降保证收敛到全局最优。

最漂亮的部分是它对 \( w_j \) 的偏导数。对单个样本求导并整理后:

\[ \frac{\partial L}{\partial w_j} = -\frac{1}{n}\sum_{i=1}^{n}\big(y^{(i)} - \sigma(z^{(i)})\big)x_j^{(i)} \]

形式上与 Adaline 的 SSE 梯度完全一样,只是把「连续输出」换成了「sigmoid 概率」。于是更新式沿用 u2-l2 的老朋友:

\[ \boldsymbol{w} \leftarrow \boldsymbol{w} + \eta \cdot \frac{\boldsymbol{X}^{\mathsf{T}}(\boldsymbol{y} - \boldsymbol{\sigma(z)})}{n}, \qquad b \leftarrow b + \eta \cdot \mathrm{mean}(\boldsymbol{y} - \boldsymbol{\sigma(z)}) \]

这不是巧合:sigmoid + log loss 是「线性激活 + SSE」在概率建模上的自然对应,两者同属广义线性模型家族中的「可分离」组合(激活函数是代价函数反函数的规范链接)。

#### 4.3.2 核心流程

`LogisticRegressionGD.fit` 的伪代码(与 AdalineGD 同构):

```text
初始化: w ~ N(0, 0.01), b = 0, losses_ = []
重复 n_iter 轮:
    z   = X·w + b            # net_input
    out = sigmoid(z)          # activation:裁剪到 [-250, 250] 再算
    err = y - out             # 残差:标签减概率
    w  += eta * Xᵀ·err / n    # 向量化梯度更新
    b  += eta * mean(err)
    记录本轮流式 log loss 到 losses_
预测: sigmoid(X·w + b) >= 0.5 ? 1 : 0
```

#### 4.3.3 源码精读

**sigmoid 与两条损失曲线。**

[ch03/ch03.py:233-234](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L233-L234) 定义了用于画图的 sigmoid:

```python
def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))
```

[ch03/ch03.py:268-273](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L268-L273) 分别给出 y=1 与 y=0 时的单样本损失,用于绘制损失随预测概率变化的曲线:

```python
def loss_1(z):
    return - np.log(sigmoid(z))        # y=1:预测概率→1 时损失→0,→0 时损失→∞

def loss_0(z):
    return - np.log(1 - sigmoid(z))    # y=0:预测概率→0 时损失→0,→1 时损失→∞
```

**LogisticRegressionGD 类本体。**

[ch03/ch03.py:320-323](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L320-L323) 的 `__init__` 只存超参数(学习率 0.01、轮数 50、随机种子 1),不做任何计算——这是 scikit-learn 风格约定:构造函数里不碰数据。

[ch03/ch03.py:341-344](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L341-L344) 用小随机数初始化权重、偏置清零:

```python
rgen = np.random.RandomState(self.random_state)
self.w_ = rgen.normal(loc=0.0, scale=0.01, size=X.shape[1])
self.b_ = np.float_(0.)
self.losses_ = []
```

> 版本提示:`np.float_` 在 NumPy 2.0 中已被移除。本书锁定 numpy 1.21.2(u1-l2 的 environment.yml)不受影响;若你在 NumPy ≥ 2.0 的环境里运行,需把这行改为 `self.b_ = 0.`(待本地验证)。

[ch03/ch03.py:346-353](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L346-L353) 是训练循环,每轮整批更新一次并记录 log loss:

```python
for i in range(self.n_iter):
    net_input = self.net_input(X)
    output = self.activation(net_input)      # sigmoid 概率
    errors = (y - output)                    # 标签 - 概率
    self.w_ += self.eta * X.T.dot(errors) / X.shape[0]
    self.b_ += self.eta * errors.mean()
    loss = (-y.dot(np.log(output)) - (1 - y).dot(np.log(1 - output))) / X.shape[0]
    self.losses_.append(loss)
```

对照 u2-l2 的 `AdalineGD`:唯一实质区别就是第 2 行从恒等激活换成了 sigmoid;第 352 行的 `loss` 正是上面公式 \( L(\boldsymbol{w},b) \) 的向量化写法。

[ch03/ch03.py:356-362](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L356-L362) 的净输入与激活:

```python
def net_input(self, X):
    return np.dot(X, self.w_) + self.b_

def activation(self, z):
    return 1. / (1. + np.exp(-np.clip(z, -250, 250)))
```

`np.clip(z, -250, 250)` 是工程保险:z 很小时 `np.exp(-z)` 会数值溢出(返回 inf 而非报错),裁剪把指数限制在 \( e^{\pm 250} \) 内,sigmoid 值域不受影响(在 |z|>250 处本来就是 0 或 1 的浮点极限)。

[ch03/ch03.py:364-366](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L364-L366) 的预测:概率 ≥ 0.5 判为 1(等价于净输入 z ≥ 0,因为 σ(0)=0.5):

```python
def predict(self, X):
    return np.where(self.activation(self.net_input(X)) >= 0.5, 1, 0)
```

**训练两类子集。**

[ch03/ch03.py:372-377](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L372-L377) 用布尔掩码取出 setosa(0)与 versicolor(1)两类训练样本,再用较大的学习率 0.3、1000 轮训练:

```python
X_train_01_subset = X_train_std[(y_train == 0) | (y_train == 1)]
y_train_01_subset = y_train[(y_train == 0) | (y_train == 1)]

lrgd = LogisticRegressionGD(eta=0.3, n_iter=1000, random_state=1)
lrgd.fit(X_train_01_subset, y_train_01_subset)
```

这个类只支持二分类——`predict` 只输出 0/1。注意标准化数据上 η=0.3 也能稳定收敛(u2-l3 的结论在这里兑现:标准化放宽了学习率可选范围),且这批两类数据本身线性可分,决策边界近乎完美(书中图 03_05,待本地验证)。

#### 4.3.4 代码实践

**实践目标**:观察 sigmoid 化后损失曲线的形态,以及「线性可分数据上损失几乎降到 0」。

**操作步骤**(示例代码):

```python
import numpy as np
import matplotlib.pyplot as plt
# 假设已按 4.1/4.2 得到 X_train_std, y_train,并定义了 LogisticRegressionGD

X01 = X_train_std[(y_train == 0) | (y_train == 1)]
y01 = y_train[(y_train == 0) | (y_train == 1)]

lrgd = LogisticRegressionGD(eta=0.3, n_iter=1000, random_state=1)
lrgd.fit(X01, y01)
print('最终权重:', lrgd.w_, '偏置:', lrgd.b_)
print('最终损失:', lrgd.losses_[-1])

plt.plot(range(1, len(lrgd.losses_) + 1), lrgd.losses_)
plt.xlabel('Epoch'); plt.ylabel('Log loss'); plt.yscale('log')
plt.show()
```

**需要观察的现象**:损失单调下降,后期因线性可分而趋近 0(取对数纵轴便于看清尾部);训练后样本的预测概率大多非常接近 0 或 1。

**预期结果**:决策边界是一条把两类完全分开的直线。若把 `n_iter` 减到 50,损失停在高位、边界位置略有偏移(待本地验证)。

#### 4.3.5 小练习与答案

**练习 1**:`activation` 里的 `np.clip(z, -250, 250)` 去掉后,什么情况下会出问题?

**答案**:当净输入 z 是绝对值很大的负数时,`np.exp(-z)` 计算的是极大正数的指数,会溢出为 `inf`,得到 `1/inf = 0` 之外还触发 RuntimeWarning;权重较大或输入异常时(尤其训练初期未标准化数据)可能发生。裁剪用微小代价换数值稳定,且 |z|>250 时 sigmoid 本来就无可分辨地接近 0 或 1。

**练习 2**:`predict` 为什么用 0.5 作阈值?阈值 0.5 对应净输入 z 等于多少?

**答案**:两类先验相同时,0.5 是「两类概率相等」的自然分界点。由于 sigmoid 以 0 为中心、σ(0)=0.5,概率 0.5 恰好对应 z=0——所以逻辑回归的决策边界仍是超平面 \( \boldsymbol{w}^{\mathsf{T}}\boldsymbol{x} + b = 0 \),与感知机形式一致。

**练习 3**:对比 u2-l2 的 `AdalineGD`,`LogisticRegressionGD.fit` 循环体只有一处实质不同,是哪一处?为什么两者的权重更新式长得一样?

**答案**:实质不同在 `activation` 从恒等函数换成 sigmoid,以及记录的代价从 SSE 换成 log loss。更新式形式相同是因为:sigmoid 恰是 log loss 的规范链接函数,sigmoid+log loss 组合的梯度中求导后的激活项被代价函数的导数约掉,剩下 \( (y - \sigma(z))x \) 的线性残差形式,与 SSE+恒等激活的 \( (y - z)x \) 结构同构。

### 4.4 sklearn LogisticRegression、predict_proba 与正则化强度 C

#### 4.4.1 概念说明

手写版帮我们理解原理,实战则交给 `sklearn.linear_model.LogisticRegression`:它支持多分类(OvR)、默认自带 **L2 正则化**,并额外提供 `predict_proba` 输出概率。

**过拟合**是本模块的主角:模型把训练数据的噪声也学了进去,训练集分数很高、新数据表现差。缓解手段是在代价函数里加惩罚项,逼迫权重保持小:

\[ L_{reg}(\boldsymbol{w}) = L(\boldsymbol{w}) + \frac{\lambda}{2}\|\boldsymbol{w}\|^2 \quad (\text{L2}), \qquad L_{reg}(\boldsymbol{w}) = L(\boldsymbol{w}) + \lambda \|\boldsymbol{w}\|_1 \quad (\text{L1}) \]

- **L2**(默认):所有权重一起收缩,通常 0 但不为 0,适合普遍相关特征。
- **L1**:把不重要的权重压到恰好 0,产生稀疏解,自带特征选择效果。

**C 与 λ 的关系是一个反直觉的坑**:scikit-learn 用的是 λ 的**倒数**:

\[ C = \frac{1}{\lambda} \]

因此:

| C | 正则化强度 | 权重倾向 | 模型倾向 |
| --- | --- | --- | --- |
| 大(如 100) | 弱 | 越大 | 低偏差、可能过拟合 |
| 小(如 0.01) | 强 | 被压小 | 高偏差、可能欠拟合 |

调大 C 相当于「更相信数据、更少惩罚」;调小 C 则是「狠狠压制权重」。

`predict_proba` 返回每类概率,OvR 多分类下每个二分类器先各自给出概率再归一化,每行求和为 1;`predict` 就是取每行 `argmax`。

#### 4.4.2 核心流程

```text
LogisticRegression(C=100.0, solver='lbfgs', multi_class='ovr')
        │ fit(X_train_std, y_train)   ← 3 类一起训练(OvR)
        ▼
predict_proba(X_test_std[:3])  → 3×3 概率矩阵
        ├── .sum(axis=1)   → 每行 1.0
        ├── .argmax(axis=1) → [2 0 0 ...] 类别索引
        └── predict(...)    ← 等价于 argmax
正则化实验:对 c ∈ {-5..4},C = 10^c
        训练 → 记录 coef_[1](versicolor 分类器的两个权重)
        画「权重系数 vs C(对数轴)」曲线
```

#### 4.4.3 源码精读

**训练 sklearn 版逻辑回归。**

[ch03/ch03.py:397-398](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L397-L398) 用较强的 C(弱正则化)训练三分类:

```python
lr = LogisticRegression(C=100.0, solver='lbfgs', multi_class='ovr')
lr.fit(X_train_std, y_train)
```

- `solver='lbfgs'`:默认求解器,一种比普通梯度下降更聪明的拟牛顿优化算法,无需手动调学习率。
- `multi_class='ovr'`:一对多策略。版本提示:scikit-learn 1.5 起该参数已被弃用(新版本会自动处理多类),在本书锁定的 sklearn 1.0 环境下照常可用。
- 接着 [ch03/ch03.py:400-407](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L400-L407) 用 `plot_decision_regions` 画出了三个区域的边界(图中会看到三块着色区域与被圈出的测试样本)。

**predict_proba 四连看。**

[ch03/ch03.py:412-432](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L412-L432) 依次展示概率输出的四个视角:

```python
lr.predict_proba(X_test_std[:3, :])            # 3 个样本 × 3 类的概率矩阵
lr.predict_proba(X_test_std[:3, :]).sum(axis=1)  # 每行求和 = [1. 1. 1.]
lr.predict_proba(X_test_std[:3, :]).argmax(axis=1)  # 每行最大值的列索引 = 类别
lr.predict(X_test_std[:3, :])                  # 与上一行结果一致
lr.predict(X_test_std[0, :].reshape(1, -1))    # 单样本必须 reshape 成二维
```

最后一条是新手高频坑:单个样本是一维数组(`shape=(2,)`),而 sklearn 约定输入必须是二维 `(n_samples, n_features)`,`reshape(1, -1)` 把它变成一行。

**正则化实验:权重随 C 的变化。**

[ch03/ch03.py:444-462](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L444-L462) 是本讲最重要的实验:

```python
weights, params = [], []
for c in np.arange(-5, 5):
    lr = LogisticRegression(C=10.**c, multi_class='ovr')
    lr.fit(X_train_std, y_train)
    weights.append(lr.coef_[1])
    params.append(10.**c)

weights = np.array(weights)
plt.plot(params, weights[:, 0], label='Petal length')
plt.plot(params, weights[:, 1], linestyle='--', label='Petal width')
plt.xscale('log')
```

- `np.arange(-5, 5)` 产生 -5 到 4 共 10 个指数,`10.**c` 即 C 从 \( 10^{-5} \) 扫到 \( 10^{4} \),覆盖 9 个数量级。
- OvR 下 `lr.coef_` 形状为 `(3, 2)`(3 个二分类器 × 2 个特征);`coef_[1]` 取**第 2 个(versicolor)分类器**的权重。
- 横轴取对数刻度(`plt.xscale('log')`),曲线呈单调上升趋势:C 越大 → 正则化越弱 → 权重系数越大(书中图 03_08,待本地验证)。

#### 4.4.4 代码实践

**实践目标**:按规格完成 C=[100, 10, 1.0, 0.1] 的对照实验,把「C 变小 → 权重收缩」变成亲眼所见。

**操作步骤**(示例代码,承接 4.1/4.2 的数据):

```python
for C in [100.0, 10.0, 1.0, 0.1]:
    lr = LogisticRegression(C=C, solver='lbfgs', multi_class='ovr')
    lr.fit(X_train_std, y_train)
    print(f'C={C:>6}: 权重={lr.coef_[1]}, 测试准确率={lr.score(X_test_std, y_test):.3f}')
```

**需要观察的现象**:

1. `lr.coef_[1]` 的两个分量随 C 减小而整体收缩(绝对值变小)。
2. 测试准确率在中间某个 C 附近最高,两端(C 很大/很小)持平或略降。
3. 决策边界随 C 变小而略微「保守」(边界方向由权重决定)。

**预期结果**:在这个仅 105 条训练样本、两特征的简单问题上,正则化对准确率影响可能不明显(所有 C 都在 0.93~0.98 一带,待本地验证),但**权重收缩**一定清晰可见——正则化直接作用的正是权重,准确率变化需要更大噪声/更多特征才显著。

#### 4.4.5 小练习与答案

**练习 1**:同时训练 `LogisticRegression(C=1000)` 和 `C=0.001`,前者的权重范数与后者相比如何?哪个模型更可能过拟合?

**答案**:C=1000(λ=0.001)正则化几乎不起作用,权重范数明显更大;它对训练数据拟合更激进,更可能过拟合。C=0.001 权重被压得很小,决策边界几乎由截距主导,偏向欠拟合。

**练习 2**:书中实验为什么取 `lr.coef_[1]` 而不是 `coef_[0]`?若想观察 setosa 分类器的权重应改为什么?

**答案**:OvR 模式下 `coef_` 的第 0/1/2 行分别对应类别 0/1/2 的「本类 vs 其余」分类器;`coef_[1]` 是类别 1(versicolor)分类器的权重,该类处在两类之间、权重变化最有代表性。观察 setosa 改用 `coef_[0]`。

**练习 3**:把 `penalty='l1'`(需同时指定 `solver='liblinear'`)替换默认 L2,权重会有什么质的不同?

**答案**:L1 正则化会把一部分权重精确压到 0,得到稀疏权重向量,相当于自动丢弃对应特征;L2 只是让所有权重整体变小、通常不精确为 0。这让 L1 兼具特征选择能力。

## 5. 综合实践

把本讲四个模块串成一个完整任务:**同一份两类 iris 数据上,对比「手写梯度下降版」与「sklearn 求解器版」逻辑回归,再扫 C 观察正则化。**

以下为示例代码(可存为独立脚本或分段贴入 notebook;运行前需按 u1-l2 建好环境,并在仓库根目录下执行以便 `python_environment_check` 可导入):

```python
# 示例代码:综合实践
import numpy as np
import matplotlib.pyplot as plt
from sklearn import datasets
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

# ---- 模块 1+2:数据准备(对照 ch03.py L93-L124)----
iris = datasets.load_iris()
X, y = iris.data[:, [2, 3]], iris.target
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.3, random_state=1, stratify=y)
sc = StandardScaler().fit(X_train)
X_train_std, X_test_std = sc.transform(X_train), sc.transform(X_test)

# ---- 模块 3:两类子集 + 手写 LogisticRegressionGD ----
# (将 ch03.py L296-L366 的类定义复制到此处)
X01 = X_train_std[(y_train == 0) | (y_train == 1)]
y01 = y_train[(y_train == 0) | (y_train == 1)]
lrgd = LogisticRegressionGD(eta=0.3, n_iter=1000, random_state=1).fit(X01, y01)

# ---- 模块 4:sklearn 版(同两类子集,公平对比)----
lr_sk = LogisticRegression(C=100.0, solver='lbfgs').fit(X01, y01)

print('手写版权重 :', lrgd.w_, '偏置:', lrgd.b_)
print('sklearn权重:', lr_sk.coef_[0], '偏置:', lr_sk.intercept_[0])

# ---- 扫 C 观察正则化(对照 ch03.py L444-L462)----
for C in [100.0, 10.0, 1.0, 0.1]:
    m = LogisticRegression(C=C, solver='lbfgs').fit(X_train_std, y_train)
    print(f'C={C:>6}: coef_[1]={m.coef_[1]}, acc={m.score(X_test_std, y_test):.3f}')
```

判定标准:

1. 手写版与 sklearn 版的权重**方向相近**(分量符号一致、比值接近)。注意两者不会严格相等:手写版用固定学习率整批 GD 训练 1000 轮未必到最优,sklearn 的 lbfgs 收敛判据不同;若符号相反且训练准确率高,检查权重是否整体差一个正倍数(同一边界的等价参数化)。
2. C 从 100 到 0.1,`coef_[1]` 两个分量绝对值单调不增。
3. 决策区域图(用 `plot_decision_regions`,ch03.py L160-L201)上两版边界几乎重合。
具体数值待本地验证。

## 6. 本讲小结

- scikit-learn 的估计器遵循 `fit` → `predict` / `score` 三步曲,换模型只换类名不换流程;`Perceptron` 与手写版的另一个差别是库版本自动支持 OvR 多分类。
- `train_test_split(test_size, random_state, stratify=y)` 完成 70/30 可复现分层划分;`np.bincount` 是核对类别比例的最快手段。
- `StandardScaler` 只在训练集上 `fit`,再对训练/测试集分别 `transform`,防止测试集统计量泄漏进训练流程。
- `LogisticRegressionGD` 用 sigmoid 把净输入映射为概率,以 log loss 为凸代价函数;其梯度与 Adaline 的 SSE 梯度同构,更新式只差激活函数一处。
- scikit-learn 的 `C = 1/λ` 是正则化强度的**倒数**:C 大 → 正则化弱 → 权重大;扫 C 画权重轨迹可以直观看到收缩效应。
- `predict_proba` 每行和为 1,`predict` 等价于按行 `argmax`;单样本预测必须 `reshape(1, -1)`。

## 7. 下一步学习建议

逻辑回归用超平面切分类别,但只产生**线性**边界。下一讲 u3-l2《最大间隔分类:SVM 与核技巧》将介绍支持向量机:它同样依赖净输入,却通过「最大化决策边界到样本的间隔」找到更稳健的线性边界,并用核技巧在非线性问题上大显身手。阅读建议:

1. 预习 [ch03/ch03.py:466-497](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L466-L497) 中线性 SVC 的用法,注意它与本讲 `lr` 的接口完全一致——再次印证 fit/predict 范式。
2. 留意下一讲的 `C` 与本讲 `C` 的关系:两者都是「越大正则化越弱」,这同一个 C 会在 SVM 中扮演软间隔的角色。
3. 想巩固本讲,可回头对比 `ch02.py` 的 `AdalineGD` 与本讲 `LogisticRegressionGD` 的 `fit` 循环,亲手把两段代码并排读一遍。
