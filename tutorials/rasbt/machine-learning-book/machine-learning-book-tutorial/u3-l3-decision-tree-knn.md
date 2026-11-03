# 决策树、KNN 与不纯度指标

## 1. 本讲目标

学完本讲，你应该能够：

1. 理解并亲手复现熵（entropy）、基尼不纯度（gini impurity）、分类误差（classification error）三种不纯度度量，明白它们在 p=0.5 处同时达到最大值、在 p=0 和 p=1 处为 0 的共同形状，以及三者的区别。
2. 理解决策树如何用「信息增益」选择分裂点，为什么它的决策边界由平行于坐标轴的线段组成，以及 `max_depth` 剪枝如何抑制过拟合。
3. 会用 `tree.plot_tree` 把训练好的树画出来并读懂每个节点。
4. 理解 KNN 的惰性学习（lazy learning）方式，掌握 `n_neighbors`、`p`、`metric` 三个参数的含义，并明白为什么 KNN 必须标准化特征而决策树不需要。
5. 知道维数灾难（curse of dimensionality）为什么让 KNN 在高维特征空间里失效。

本讲是第 3 章「分类器漫游」的最后一讲。前面两讲里的感知机、逻辑回归、SVM 都属于「参数化模型」：训练阶段学出一组固定参数（权重 w 和偏置 b），预测时只需一次线性打分。本讲的决策树和 KNN 则代表了两种完全不同的思路——一个用「如果…就…」的规则递归切分特征空间，一个干脆不训练、预测时才去查邻居。

## 2. 前置知识

阅读本讲前，请确认你已理解前置讲义（u3-l1、u3-l2）中的以下概念，本讲直接使用、不再重复推导：

- **fit / predict / score 范式**：所有 scikit-learn 估计器的统一接口。换模型只换类名，流程不变。
- **train_test_split 与分层抽样**：`stratify=y` 保证训练集/测试集类别比例与整体一致；`random_state` 固定划分结果可复现。
- **StandardScaler 与数据泄漏**：只在训练集上 `fit`，测试集只 `transform`。
- **plot_decision_regions 与 test_idx**：铺网格逐点预测后填色，画出模型在整个特征平面上的决策区域；`test_idx=range(105, 150)` 用空心圆圈出测试样本。

再补充两个本讲要用的新直觉：

- **不纯度（impurity）**：一个节点里样本类别的「混杂程度」。如果节点里 100% 都是同一类，它最「纯」；如果两类各占一半，它最「脏」。决策树生长的每一步，都在寻找能把「脏」节点切得更「纯」的分裂条件。
- **距离度量**：KNN 依赖「距离近的样本标签相似」。二维平面上最常见的欧氏距离（直线距离）只是 Minkowski 距离家族中 p=2 的特例。

## 3. 本讲源码地图

本讲几乎全部内容来自一个文件：

| 文件 | 作用 |
| --- | --- |
| `ch03/ch03.py` | 第 3 章的脚本版代码。本讲涉及其中三段：L596–664（三种不纯度函数与对比曲线）、L668–702（决策树训练与 `tree.plot_tree` 可视化）、L729–751（KNN 训练） |
| `ch03/ch03.ipynb` | 同一内容的 notebook 版，适合交互式跑本讲的实践 |

辅助上下文（数据与工具函数，在前两讲已精读，本讲只引用行号）：

- `ch03/ch03.py:93-106` — 加载鸢尾花数据、取花瓣长度/宽度两列、70/30 分层划分。
- `ch03/ch03.py:121-124` — StandardScaler 标准化。
- `ch03/ch03.py:160-201` — `plot_decision_regions` 函数。
- `ch03/ch03.py:208-209` — 拼接训练/测试集的 `X_combined_std` 与 `y_combined`。

> **关于 export_graphviz 的说明**：本讲大纲中提到的 `export_graphviz`（把树导出为 Graphviz 的 DOT 格式再渲染）是 scikit-learn 提供的一种树可视化接口，但**当前仓库的 ch03.py 并没有使用它**，而是直接调用 matplotlib 后端的 `tree.plot_tree`（见 [ch03/ch03.py:697-699](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L697-L699)）。本讲以仓库真实代码为准讲解 `tree.plot_tree`。

## 4. 核心概念与源码讲解

### 4.1 三种不纯度指标：gini / entropy / error

#### 4.1.1 概念说明

决策树在每个节点上要回答的问题是：**按哪个特征、在哪个阈值处切一刀，能让子节点最纯？** 要回答它，首先得给「纯」定一个数字标准。书中手写了三个标准，均以「节点中正类比例 p」为自变量（二分类情形）：

1. **熵（entropy）**：来自信息论。熵衡量「弄清一个样本的标签平均需要多少比特信息」。

   \[ I_H(t) = -\sum_{i=1}^{C} p(i \mid t) \log_2 p(i \mid t) \]

   二分类时退化为 \(-p\log_2 p - (1-p)\log_2(1-p)\)，最大值 1（p=0.5 时），单位是比特。

2. **基尼不纯度（gini impurity）**：可以理解为「从节点中随机抽一个样本、再按类别分布随机猜它的标签，猜错的概率」。

   \[ I_G(t) = 1 - \sum_{i=1}^{C} p(i \mid t)^2 \]

   二分类时等于 \(2p(1-p)\)，最大值 0.5（p=0.5 时）。计算不需对数，比熵快，是 scikit-learn 的默认准则。

3. **分类误差（classification error）**：把节点里占比最多的类当作预测、得到的错误率。

   \[ I_E(t) = 1 - \max_i p(i \mid t) \]

   二分类时等于 \(1 - \max(p, 1-p)\)，最大值同样 0.5。

三者的共同点：p=0（全负类）和 p=1（全正类）处都为 0（最纯），p=0.5 处都取最大值（最不纯）。区别在「形状」：熵和基尼是光滑的抛物线形曲线，对节点内类别比例的变化敏感；分类误差是两条直线拼起来的折线，在 p 偏离 0.5 的过程中大部分区间斜率恒定、对概率变化不敏感。这个差异有实际后果：用分类误差来**生长**树往往选不出好的分裂点（切完概率从 0.5 变到 0.6，误差纹丝不动），但它对**剪枝**（pruning，删除贡献小的分支）更友好。这也是实践中生长阶段用 gini 或 entropy 的原因。

#### 4.1.2 核心流程

复现书中对比图的流程：

```text
1. 构造自变量网格：x = np.arange(0.0, 1.0, 0.01)   # 100 个 p 值
2. 对每个 p 分别计算三条曲线：
   ent    = entropy(p)          # p=0 时跳过（log2(0) 无定义）
   sc_ent = 0.5 * entropy(p)    # 缩放熵，与 gini 同尺度对比
   gini   = gini(p)
   err    = error(p)
3. 用不同线型/颜色画在同一坐标系，横轴 p(i=1)、纵轴 Impurity index
```

注意两处细节：

- **p=0 的特殊处理**：\(\log_2 0\) 在数学上发散、在 numpy 中产生 `-inf` 并伴随运行时警告，所以代码在 p=0 处填 `None`（matplotlib 会跳过该点，不画）。
- **熵乘 0.5 的目的**：纯熵最大值是 1，基尼和误差最大值是 0.5，三条线不在一个量纲上。把熵缩放一半后，三条曲线才能在同一张图里直观比较形状。

#### 4.1.3 源码精读

仓库中 `entropy` 先单独出现一次用于画熵曲线，随后在对比图一节中与 `gini`、`error` 并排定义：

三个手写不纯度函数——[ch03/ch03.py:629-638](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L629-L638)：

```python
def gini(p):
    return p * (1 - p) + (1 - p) * (1 - (1 - p))


def entropy(p):
    return - p * np.log2(p) - (1 - p) * np.log2((1 - p))


def error(p):
    return 1 - np.max([p, 1 - p])
```

这三行就是上一节三个公式在二分类情形下的直接翻译。`gini` 的写法值得单独看一眼：`p*(1-p) + (1-p)*(1-(1-p))` 字面上是「正类比例×抽到负类的概率 + 负类比例×抽到正类的概率」（即两次独立抽样标签不一致的概率），代数展开后正好等于 \(1 - p^2 - (1-p)^2 = 2p(1-p)\)，与教科书形式 \(I_G = 1 - \sum_i p_i^2\) 完全一致——同一公式的两种读法。

熵的首次定义与 p=0 的跳过逻辑——[ch03/ch03.py:604-609](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L604-L609)：

```python
def entropy(p):
    return - p * np.log2(p) - (1 - p) * np.log2((1 - p))

x = np.arange(0.0, 1.0, 0.01)
ent = [entropy(p) if p != 0 else None 
       for p in x]
```

条件表达式 `entropy(p) if p != 0 else None` 是整个片段的关键：用列表推导逐点计算，唯独在 p=0 处返回 `None`，避免对 0 取对数。

四条曲线的绘制——[ch03/ch03.py:640-664](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L640-L664)：

```python
x = np.arange(0.0, 1.0, 0.01)

ent = [entropy(p) if p != 0 else None for p in x]
sc_ent = [e * 0.5 if e else None for e in ent]
err = [error(i) for i in x]

fig = plt.figure()
ax = plt.subplot(111)
for i, lab, ls, c, in zip([ent, sc_ent, gini(x), err], 
                          ['Entropy', 'Entropy (scaled)', 
                           'Gini impurity', 'Misclassification error'],
                          ['-', '-', '--', '-.'],
                          ['black', 'lightgray', 'red', 'green', 'cyan']):
    line = ax.plot(x, i, label=lab, linestyle=ls, lw=2, color=c)
```

这里用 `zip` 把「数据、图例文字、线型、颜色」四组序列打包成一次循环，四条曲线共用一个 `ax.plot` 调用——比写四遍 plot 简洁得多，是值得学会的 matplotlib 惯用法。`sc_ent = [e*0.5 if e else None for e in ent]` 里 `if e` 隐式把 `None` 和 0 都跳过（本题中 p>0 时熵恒为正，不会误伤）。最后 `ax.axhline(y=0.5)` 与 `ax.axhline(y=1.0)` 画出两条参考虚线，直观标出各指标的最大值（见 L658-659）。

#### 4.1.4 代码实践

**实践目标**：亲手复现书中的不纯度对比图（书中图 03_19），并验证三条曲线在特殊点上的取值。

**操作步骤**（可在 `ch03/ch03.ipynb` 的新单元格中运行，或写一个独立脚本）：

1. 复制 [ch03/ch03.py:629-638](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L629-L638) 的三个函数定义。
2. 运行 [ch03/ch03.py:640-664](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L640-L664) 的绘图代码（若在脚本中，请保留 `plt.show()`；在 notebook 中它会自动显示）。
3. 追加几个断言式打印：

   ```python
   # 示例代码：验证三个特殊点
   import numpy as np
   for p in (0.0, 0.5, 1.0):
       g = gini(p)
       e = error(p)
       h = entropy(p) if 0 < p < 1 else 0.0  # 端点极限为 0
       print(f'p={p}: gini={g:.3f}, error={e:.3f}, entropy={h:.3f}')
   print('gini(0.5) 与 error(0.5) 相等:', gini(0.5) == error(0.5))   # True
   print('gini 展开式与 2p(1-p) 相等:', 
         np.isclose(gini(0.3), 2*0.3*0.7))                            # True
   ```

**需要观察的现象**：

- 图中四条曲线在 p=0 与 p=1 处都汇于 0，在 p=0.5 处都达到各自峰值；缩放后的熵（浅灰实线）几乎贴着基尼（红色虚线），说明二者形状高度相似，实践中效果通常相当。
- 分类误差（绿色点划线）是折线：p∈[0, 0.5] 段斜率为 +1，p∈[0.5, 1] 段斜率为 −1，在 p∈(0.34, 0.66) 之外它「压平」为零——节点里多数类占 66% 以上时，分类误差已经看不出继续变纯的收益，而熵和基尼仍严格单调下降。

**预期结果**：三个打印点分别给出 `(0, 0, 0)`、`(0.5, 0.5, 1.0)`、`(0, 0, 0)`；两个 `True`。曲线形态与书中图 03_19 一致。（图中具体视觉细节待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么代码要单独定义 `sc_ent`（缩放熵），直接把未缩放的熵和 gini、error 画在一起不行吗？

**答案**：可以画，但未缩放的熵峰值是 1，gini 与 error 峰值是 0.5，纵轴被拉到 [0,1] 后 gini 和 error 会被压扁在下半部分，形状对比不直观。把熵乘 0.5 后三条曲线峰值对齐到同一量纲，缩放熵与 gini 几乎重合，读者一眼就能看出「熵和基尼形状几乎一样、只是纵尺度不同」，而分类误差的折线形状明显不同。

**练习 2**：把 `error(p)` 的定义改成 `1 - np.max([p, 1-p])` 中的 `max` 换成 `min`，函数含义会变成什么？它还能当不纯度指标吗？

**答案**：换成 `min` 后函数变为 `1 - min(p, 1-p)`，在 p=0.5 处取最小值 0.5，在 p=0 和 p=1 处取最大值 1——正好与「不纯度」要求的方向相反（节点越纯值越大、越混值越小）。它衡量的是「少数类的存在感」，不能直接作不纯度指标；决策树需要的是「越混越大」的凸函数。

**练习 3**：书中说分类误差「对类别概率的变化不那么敏感」。用 p 从 0.5 变到 0.58 的例子具体说明。

**答案**：error(0.5)=0.5，error(0.58)=1−0.58=0.42，下降 0.08；gini(0.5)=0.5，gini(0.58)=2×0.58×0.42≈0.487，只降 0.013；熵(0.5)=1，熵(0.58)≈0.981，只降约 0.019。（若多数类从 50% 提高到 58%，误差下降明显。）但真正的问题出在折线的平坦段：p 从 0.9 变到 0.98 时 error 恒为 0.1→0.02 线性变化，而更关键的生长场景是——把一个 p=0.9 的节点切出一个 p=0.95 的子节点时，error 只从 0.10 降到 0.05；且 error 对 (0.66, 1] 区间内的一切变化都线性响应，无法体现「越接近纯，每一点提升越难」。更典型的例子是：一个分裂把子节点概率从 (0.5, 0.5) 变为 (0.6, 0.4) 这种对半切附近的改善，gini/entropy 持续给出平滑递减的奖励信号，而 error 的响应分段线性、在生长常用的候选阈值上经常取相同值，导致并列最优、选不出有意义的分裂。

> 注：练习 3 的数值部分为「示例代码」级别的人工计算，建议读者用上面 4.1.4 的打印方式自行验证。

### 4.2 DecisionTreeClassifier：信息增益与 max_depth 剪枝

#### 4.2.1 概念说明

决策树从根节点（全部训练样本）开始，反复做一件事：**遍历每个特征的每个候选阈值，选出能让「不纯度下降」最大的切分，把节点一分为二**。这个下降量叫**信息增益（information gain）**：

\[ IG(D_p, f) = I(D_p) - \frac{N_{left}}{N_p} I(D_{left}) - \frac{N_{right}}{N_p} I(D_{right}) \]

其中 \(D_p\) 是父节点数据集，\(f\) 是候选特征（及其分裂阈值），\(N_p\) 是父节点样本数，\(I(\cdot)\) 是任一不纯度函数（gini、entropy 等）。两个子节点按样本量加权——把 95% 的样本切进一个很纯的节点、5% 留在不纯节点，未必比均匀二分更好，权重项就是防止这种偏斜被高估。

树的每一个内部节点形如「petal width ≤ 0.8 吗？」——**判断条件永远只看单个特征和一个阈值**。几何上，「特征 i ≤ 阈值」就是特征空间中一条平行于另一根坐标轴的直线。决策树不断在两根轴之间交替（或重复）切分，所以它画出的决策边界永远由**水平线段和垂直线段拼接**而成。在二维特征平面上用 `plot_decision_regions` 着色后，你会看到一块块矩形的「阶梯」，而不是感知机/逻辑回归/SVM 那样的斜线或光滑曲线——这就是决策边界呈「平行于坐标轴的分段矩形」的成因。切分越深，台阶越小越碎；深度无限时每个训练样本都能被单独圈出来，训练准确率 100%、测试性能却下降——这就是过拟合。

**剪枝（pruning）** 是对策：`max_depth` 限制树最多长几层，是最直接的一种预剪枝（pre-pruning，在生长过程中提前停止）。深度越小，模型越简单、边界越粗；深度越大，边界越碎、越容易过拟合。

还有一个工程上很实用的性质：**决策树对特征缩放不敏感**。切分条件是「特征值 ≤ 阈值」，阈值本身就是在该特征的原始取值中挑的，把特征整体乘个系数、挪个原点，最优切分点跟着等比例移动，树结构不变。所以你会看到本节代码直接用**未标准化**的 `X_train` 训练树——这与 KNN（4.4 节）形成直接对照。

#### 4.2.2 核心流程

scikit-learn 的 CART（Classification and Regression Trees）训练流程（二叉树版本）：

```text
fit(X_train, y_train):
1. 根节点 = 全部训练样本
2. 对当前节点：
   a. 对每个特征 j、每个候选阈值 t（通常取相邻样本值的中点）：
        计算 IG(j, t) = I(父) − 加权子节点不纯度
   b. 选使 IG 最大的 (j*, t*)，把样本分成左右两个子节点
   c. 对每个子节点递归重复 2
3. 停止条件（预剪枝）：
   - 节点样本全部同类（不纯度为 0）
   - 达到 max_depth
   - 分裂不再带来正的信息增益等
predict(x):
    从根出发，逐节点比较 x 的特征值与阈值，走到叶子，
    返回该叶子中样本数最多的类别
```

#### 4.2.3 源码精读

训练决策树并画决策区域——[ch03/ch03.py:673-689](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L673-L689)：

```python
tree_model = DecisionTreeClassifier(criterion='gini', 
                                    max_depth=4, 
                                    random_state=1)
tree_model.fit(X_train, y_train)

X_combined = np.vstack((X_train, X_test))
y_combined = np.hstack((y_train, y_test))
plot_decision_regions(X_combined, y_combined, 
                      classifier=tree_model,
                      test_idx=range(105, 150))

plt.xlabel('Petal length [cm]')
plt.ylabel('Petal width [cm]')
```

逐参数解读：

- `criterion='gini'`：指定 4.1 节的不纯度函数为基尼（也可选 `'entropy'`）。
- `max_depth=4`：预剪枝——最多 4 层，防止树把每个训练样本都圈成独立叶子。
- `random_state=1`：当多个分裂点的信息增益并列最优时，CART 需要随机打破平局；固定种子让结果可复现。
- `fit(X_train, y_train)`：注意这里是**原始单位**的花瓣长度/宽度（厘米），不是 `X_train_std`。决策树按阈值切分、与量纲无关，可省去标准化。
- `X_combined = np.vstack((X_train, X_test))`：这一行是本节**临时**重新拼接的未标准化版本；前面 KNN 等章节用的 `X_combined_std`（[ch03/ch03.py:208](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L208)）是标准化版本。两个变量名只差一个 `_std` 后缀，对照阅读时留意不要混淆。
- `test_idx=range(105, 150)`：沿用前两讲的用法，45 个测试样本以空心圆高亮——决策区域图上可以直观检查「台阶」是否把测试点圈进了正确的色块。

紧随其后还有一段随机森林代码（[ch03/ch03.py:712-725](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L712-L725)），它把 25 棵这样的树投票组合。随机森林属于第 7 章集成学习的正式内容，本讲只需记住：它印证了「单棵树容易过拟合，组合多棵树是常见解法」。

#### 4.2.4 代码实践

**实践目标**：观察 `max_depth` 从 1 到 5 变化时，决策边界的「台阶」如何从粗到碎，并定量看到训练集与测试集准确率的差距如何变化。

**操作步骤**（示例代码，基于本讲已运行过的数据准备部分）：

1. 确保已运行 [ch03/ch03.py:93-106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L93-L106)（数据与划分）与 [ch03/ch03.py:160-201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L160-L201)（`plot_decision_regions` 定义）。
2. 运行下面这段网格实验：

   ```python
   # 示例代码：max_depth 对边界与准确率的影响
   import matplotlib.pyplot as plt
   from sklearn.tree import DecisionTreeClassifier

   fig, axes = plt.subplots(1, 5, figsize=(22, 4), sharex=True, sharey=True)
   for depth, ax in zip([1, 2, 3, 4, 5], axes):
       tree_model = DecisionTreeClassifier(max_depth=depth, random_state=1)
       tree_model.fit(X_train, y_train)
       plt.sca(ax)  # 把后续绘图定向到当前子图
       plot_decision_regions(X_combined, y_combined,
                             classifier=tree_model,
                             test_idx=range(105, 150))
       ax.set_xlabel('Petal length [cm]')
       ax.set_ylabel('Petal width [cm]')
       ax.set_title(f'depth={depth}  '
                    f'train acc={tree_model.score(X_train, y_train):.2f}  '
                    f'test acc={tree_model.score(X_test, y_test):.2f}',
                    fontsize=9)
   plt.tight_layout()
   plt.show()
   ```

   （`plot_decision_regions` 内部调用的是当前 matplotlib「当前轴」，`plt.sca(ax)` 保证每个模型画进自己的子图；这是把仓库函数复用到多子图的最小改法。）

**需要观察的现象**：

- depth=1：只有一次切分，整个平面被一条水平或垂直线分成两块——一棵「树桩」（decision stump），必然欠拟合（三个类最多分出两个区域）。
- depth=2~3：台阶变多，三类区域成形，训练/测试准确率都上升。
- depth=4~5：台阶进一步变碎，训练准确率逼近 1.00，测试准确率基本不再提升（鸢尾花数据简单，甚至可能持平或略降）。

**预期结果**：训练准确率随深度单调不降；测试准确率先升后趋于平缓。图中具体的台阶位置与分数数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么决策树的决策边界总是平行于特征坐标轴？什么时候这种边界是缺点？

**答案**：因为每个内部节点的判断形如「特征 j 的值 ≤ 阈值 t」，在两特征平面上这恰是一条平行于另一特征轴的直线；树把这些半平面逐层求交，边界就是水平/垂直线段的拼接。缺点出现在真实边界是斜线或光滑曲线时（例如「花瓣长度 ≈ 2×花瓣宽度」这样的对角关系）——树必须用很多段小台阶去逼近斜线，树变深、模型变碎，且对训练数据的小扰动敏感。

**练习 2**：信息增益公式里为什么要给两个子节点的不纯度乘上 \(N_{left}/N_p\) 和 \(N_{right}/N_p\)？

**答案**：加权平均防止「把极少数样本切得很纯」的分裂被高估。例如父节点 100 个样本，某分裂把 2 个样本分进一个完全纯的叶子、98 个留在与父节点同样混杂的节点：如果不加权，两个子节点的平均不纯度会被那 2 个纯样本大幅拉低，显得增益很高；加权后纯叶子的贡献只占 2%，增益接近零，反映出这个分裂几乎没解决 98% 样本的问题。

**练习 3**：某节点有 50 个正样本、50 个负样本（p=0.5）。按 gini 计算，把它切分成「40 正 10 负」与「10 正 40 负」两个子节点后的信息增益是多少？

**答案**：父节点 gini = 2×0.5×0.5 = 0.5。两个子节点各有 50 个样本、权重各 0.5，gini 均为 2×0.8×0.2 = 0.32。所以 IG = 0.5 − 0.5×0.32 − 0.5×0.32 = 0.5 − 0.32 = 0.18。（若改用 entropy：父为 1.0，子为 −0.8log2 0.8 − 0.2log2 0.2 ≈ 0.722，IG ≈ 0.278。）

### 4.3 树可视化：tree.plot_tree 读懂每个节点

#### 4.3.1 概念说明

训练得到的树是一组嵌套的 `if-else` 规则，光看决策区域图无法知道「树到底问了哪些问题」。scikit-learn 内置了 `sklearn.tree.plot_tree`，可以把拟合好的模型直接渲染成节点图：每个节点标注切分条件（`feature <= threshold`）、该节点的 gini 值、样本数、三类的样本分布，以及占多数的类别；配合 `filled=True` 按类别给节点着色、`feature_names` 显示特征名而非 `x[0]`/`x[1]`。它是检查「模型学到了什么」的第一工具——例如一眼看出哪个特征在根部被使用（通常是信息量最大的特征）。

#### 4.3.2 核心流程

```text
1. 准备可读的特征名列表（与训练数据的列顺序一一对应）
2. tree.plot_tree(模型, feature_names=..., filled=True)
3. plt.show() 渲染
```

#### 4.3.3 源码精读

绘制决策树——[ch03/ch03.py:695-702](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L695-L702)：

```python
feature_names = ['Sepal length', 'Sepal width',
                 'Petal length', 'Petal width']
tree.plot_tree(tree_model,
               feature_names=feature_names,
               filled=True)

plt.show()
```

注意一个细节：`feature_names` 列出了全部四个特征名，而 `tree_model` 只在两列特征（花瓣长度、花瓣宽度）上训练。`plot_tree` 只按索引取用实际参与的特征名，列表多给几项并不报错；但更严谨的做法是让列表与训练数据的列严格对应（例如 `['Petal length', 'Petal width']`），否则在多特征场景下容易看错节点用的是哪个特征。`filled=True` 让节点按多数类着色、颜色深浅随纯度变化：叶子越纯颜色越深，混类节点颜色浅。

#### 4.3.4 代码实践

**实践目标**：把 4.2.4 中 depth=1 与 depth=4 两棵树分别可视化，对照节点图与决策区域图。

**操作步骤**（示例代码）：

```python
# 示例代码：对比浅树与深树的节点内容
from sklearn import tree
import matplotlib.pyplot as plt

for depth in (1, 4):
    tm = DecisionTreeClassifier(max_depth=depth, random_state=1)
    tm.fit(X_train, y_train)
    plt.figure(figsize=(10, 6))
    tree.plot_tree(tm,
                   feature_names=['Petal length', 'Petal width'],
                   class_names=['Setosa', 'Versicolor', 'Virginica'],
                   filled=True)
    plt.title(f'max_depth={depth}')
    plt.show()
```

（`class_names` 是 `plot_tree` 的另一个可用参数，让节点直接显示类名而非类别索引。）

**需要观察的现象**：

- depth=1 的图只有根节点和两个叶子；根节点的判断条件大概率是 `Petal length <= 某阈值` 或 `Petal width <= 某阈值`——树自动挑出了信息增益最大的特征。
- depth=4 的图层数更多、叶子更多；根节点条件与浅树相同（同一份数据的最优首分裂不变），但下游多了细化切分。

**预期结果**：两图根节点一致，depth=4 的叶子数量明显更多且更纯（gini 更接近 0）。具体阈值与叶子数待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：可视化图中某个中间节点显示 `gini = 0.5`，它对应 4.1 节曲线上的哪个点？如果这是二分类问题，节点内类别比例是多少？

**答案**：对应 p=0.5 处的最大值点（二分类 gini 峰值 0.5）。二分类时 gini=0.5 意味着正负各占一半，是最不纯的状态。（本数据是三分类，gini 峰值为 \(1 - 3 \times (1/3)^2 = 2/3\)；若某三分类节点 gini=0.5，说明类别分布介于完全混和完全纯之间，例如 (0.5, 0.5, 0)。）

**练习 2**：为什么建议 `feature_names` 与训练数据的列严格对应，而不是像仓库代码那样传全部四个特征名？

**答案**：`plot_tree` 按特征在训练数据中的**列索引**引用 `feature_names`。列表比训练列多时，多出的项永远不会被用到（本例无害）；但列表顺序一旦与列顺序不一致，节点上显示的特征名就是错的——多特征数据集里这会让你把分裂条件安到错误特征上，得出完全错误的模型解读。

### 4.4 KNN：惰性学习、k、p 与维数灾难

#### 4.4.1 概念说明

KNN（K-Nearest Neighbors，K 近邻）把「训练」压缩到了极致：`fit` 只是记住训练集，不拟合任何参数——所以叫**惰性学习（lazy learning）**，与感知机/逻辑回归/SVM/决策树这些「急切学习（eager learning）」模型相对。预测一个新样本 x 时，KNN 计算 x 与所有训练样本的距离，取最近的 k 个邻居，**投票**决定类别（多数表决；`predict_proba` 输出邻居中各类占比）。

三个关键参数：

1. **`n_neighbors`（k）**：决策边界平滑度的旋钮。k=1 时边界紧贴每个训练点，方差极大（某个点标错，边界立刻跟着错）；k 越大边界越平滑、越偏向多数类先验，k 过大则欠拟合（极端情形 k=全部样本时，所有预测都是训练集最多的类，与特征无关）。k 是**超参数**，需用第 6 章的交叉验证来选。
2. **`metric` 与 `p`**：距离定义。仓库代码用 `metric='minkowski'`，即 Minkowski 距离

   \[ d(\mathbf{x}, \mathbf{y}) = \left( \sum_i |x_i - y_i|^p \right)^{1/p} \]

   `p=1` 是曼哈顿距离（各坐标差绝对值求和），`p=2` 是欧氏距离。代码里 `p=2` + `metric='minkowski'` 组合的含义就是「用欧氏距离」。

3. **特征缩放**：KNN 的每一项计算都建立在距离上。花瓣长度（0~7 cm）与花瓣宽度（0~2.5 cm）量纲不同，未缩放时长度的数值差异天然主导距离，宽度几乎被忽略。所以 KNN 必须标准化——仓库代码用 `X_train_std` 而非 `X_train`，与决策树一节形成直接对照（这是树与 KNN 在工程上最重要的差别之一）。

**维数灾难（curse of dimensionality）**：KNN 的直觉是「近邻标签相似」。但随着特征维数 d 增长，高维空间中任意两点间的距离趋于相同（欧氏距离的分布随维度升高而集中），「最近」与「最远」的差别消失，投票退化成在几乎等距的样本里随机挑 k 个。同时，要让 k 个近邻真正「贴近」查询点，所需训练样本量随维度指数增长。因此 KNN 只适合特征维度较低、样本较密的场景。

#### 4.4.2 核心流程

```text
fit(X_train_std, y_train):   # 惰性：只存储，不计算参数
predict(x):
1. 计算 x 与全部 N 个训练样本的 Minkowski(p=2) 距离
2. 取距离最小的 k 个邻居
3. 返回 k 个邻居中出现次数最多的类别
```

#### 4.4.3 源码精读

训练 KNN 并画决策区域——[ch03/ch03.py:738-751](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L738-L751)：

```python
knn = KNeighborsClassifier(n_neighbors=5, 
                           p=2, 
                           metric='minkowski')
knn.fit(X_train_std, y_train)

plot_decision_regions(X_combined_std, y_combined, 
                      classifier=knn,
                      test_idx=range(105, 150))

plt.xlabel('Petal length [standardized]')
plt.ylabel('Petal width [standardized]')
```

逐行解读：

- `n_neighbors=5`：每次预测由最近的 5 个邻居投票。
- `p=2, metric='minkowski'`：p=2 的 Minkowski 距离即欧氏距离——`p` 只有在 `metric='minkowski'` 时才生效，二者成对出现。
- `knn.fit(X_train_std, y_train)`：**标准化**数据。对照 [ch03/ch03.py:676](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L676) 的 `tree_model.fit(X_train, y_train)`（原始数据），同一份数据、两个模型、两种输入——这是「基于距离的模型要缩放、基于阈值的模型不必缩放」的直接代码证据。
- `plot_decision_regions(X_combined_std, ...)`：用的是 [ch03/ch03.py:208](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L208) 拼好的标准化组合集；坐标轴标签也相应标注 `[standardized]`。
- KNN 的边界在图中表现为围绕样本团的**局部光滑块状**区域：它没有全局的函数形式，完全由训练点的分布决定；在样本稀疏处，边界会出现孤岛状的碎片——这正是「记忆训练集」这种非参数方式的典型外观。

#### 4.4.4 代码实践

**实践目标**：观察 k 的大小对决策边界平滑度的影响，并实验 `p=1`（曼哈顿）与 `p=2`（欧氏）的差异。

**操作步骤**（示例代码，接在本讲已运行的数据准备与 `plot_decision_regions` 之后）：

```python
# 示例代码：k 与 p 对 KNN 决策区域的影响
import matplotlib.pyplot as plt
from sklearn.neighbors import KNeighborsClassifier

fig, axes = plt.subplots(2, 4, figsize=(20, 9), sharex=True, sharey=True)
for row, p in enumerate((1, 2)):
    for col, k in enumerate((1, 5, 10, 25)):
        knn = KNeighborsClassifier(n_neighbors=k, p=p, metric='minkowski')
        knn.fit(X_train_std, y_train)
        ax = axes[row, col]
        plt.sca(ax)
        plot_decision_regions(X_combined_std, y_combined,
                              classifier=knn,
                              test_idx=range(105, 150))
        ax.set_xlabel('Petal length [standardized]')
        ax.set_ylabel('Petal width [standardized]')
        ax.set_title(f'k={k}, p={p}  '
                     f'test acc={knn.score(X_test_std, y_test):.2f}',
                     fontsize=9)
plt.tight_layout()
plt.show()
```

**需要观察的现象**：

- k=1：边界破碎，出现包围单个训练点的孤岛色块，测试准确率波动大。
- k 增大：色块连片、边界平滑，孤岛消失；k=25 时三类区域已相当规整。
- p=1 与 p=2 两行的差别通常细微（同为 Minkowski 家族、阶数不同），但在样本稀疏的角落里边界形状会有出肉眼可辨的不同；欧氏距离沿「直线」度量，曼哈顿距离沿「坐标轴方向」度量，后者的等距线是菱形而非圆形，边界局部会呈现更贴近轴向的走形。

**预期结果**：k 从 1 到 25，边界单调变平滑；测试准确率大体先升后稳（鸢尾花两特征可分性好，各配置都可能接近 0.98~1.00）。具体分数待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 KNN 是「惰性」学习？它的预测成本与急切学习模型相比如何？

**答案**：KNN 的 `fit` 不估计任何参数，只是把训练集存起来；所有的计算（对每个训练样本算距离、排序取前 k、投票）都被推迟到 `predict` 时进行。因此它的训练几乎零成本，而预测成本为 O(N·d)（N 个训练样本、d 维特征）且每个查询都要重算一遍；急切学习模型（如逻辑回归）恰好相反——训练贵、预测只是一次点积 O(d)。样本量大时 KNN 的预测延迟是其主要工程缺点。

**练习 2**：`metric='minkowski'` 且 `p=2` 等价于哪个常见距离？若把 `p` 改为 1 呢？`p` 参数在 `metric='euclidean'` 时还有意义吗？

**答案**：p=2 的 Minkowski 即欧氏距离；p=1 即曼哈顿距离。当 `metric='euclidean'` 时距离已固定为 p=2 的情形，再传 `p` 不会被使用（无意义），`p` 只有在 `metric='minkowski'` 下才生效——这正是代码把两个参数写在一起的原因。

**练习 3**：k=1 的 KNN 在训练集上的准确率一定是多少？这说明它的偏差和方差特性如何？

**答案**：100%。每个训练样本的最近邻就是它自己，必然被正确分类。这说明 k=1 的模型方差极大（对训练数据完全「过拟合」，任何单个样本的移动或标签噪声都会直接改写它附近的边界）、偏差极小；增大 k 是用偏差换方差（边界更平滑、更稳定，但开始偏离局部细节）。这与 4.2 节 `max_depth` 的作用方向一致：模型复杂度旋钮（树的深度、KNN 的 1/k）都在调节偏差-方差权衡。

## 5. 综合实践

把本讲三个知识点串成一个小实验：**在同一份鸢尾花两特征数据上，系统比较「不纯度准则 × 树深度 × KNN 的 k」并解读可视化结果**。

**任务步骤**（示例代码框架）：

1. **准备**：运行 [ch03/ch03.py:93-106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L93-L106)、[ch03/ch03.py:121-124](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L121-L124)、[ch03/ch03.py:160-201](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L160-L201)、[ch03/ch03.py:208-209](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L208-L209)。
2. **准则对比**：固定 `max_depth=3`，分别用 `criterion='gini'` 与 `criterion='entropy'` 训练两棵树，用 `plot_decision_regions` 画成 1×2 子图。多数情况下两图几乎一样（4.1 节已解释原因）；如果不同，说明某次分裂出现信息增益并列、被随机打破。
3. **深度扫描**：完成 4.2.4 的 depth∈[1,2,3,4,5] 实验，记录每个深度的训练/测试准确率，找出测试准确率最高且模型最简单的深度。
4. **KNN 对照**：完成 4.4.4 的 k∈[1,5,10,25] × p∈[1,2] 实验。
5. **树图解读**：用 4.3.4 的方法把第 3 步选出的最优树可视化，回答：根节点用的是哪个特征？最深一层叶子还剩几个样本？有没有 gini=0 的叶子？
6. **总结**：写三五行结论，回答——(a) 树与 KNN 的边界形态差异及原因；(b) 树用原始特征、KNN 用标准化特征的原因；(c) `max_depth` 与 `n_neighbors` 这两个旋钮的共同点（提示：都在调偏差-方差权衡）。

**预期结果**：两准则的树几乎一致；测试准确率随 depth 和 k 的增大先升后稳；最优树根节点大概率是花瓣长度或花瓣宽度（具体待本地验证）。整个实验大约 100 行示例代码，跑完后你应当能对任何一份表格数据，独立完成「训练树→可视化→调深度→用 KNN 对照」的完整流程。

## 6. 本讲小结

- 三种不纯度指标（熵、基尼、分类误差）都是「p=0.5 最大、两端为 0」的凸函数；熵与基尼形状几乎相同（后者无需对数、更快，是 sklearn 默认），分类误差是折线、对概率变化不敏感，更适合剪枝而非生长。
- 决策树用信息增益（父节点不纯度减去样本量加权的子节点不纯度）逐节点选择「特征+阈值」切分，边界由平行于坐标轴的线段拼成；`max_depth` 预剪枝限制层数，防止台阶过碎而过拟合。
- `tree.plot_tree` 把拟合好的树渲染成节点图（切分条件、gini、样本数、类别分布），是检查模型学到了什么的第一工具；特征名列表必须与训练数据的列对应。
- KNN 是惰性学习：`fit` 只存储，`predict` 时才算距离、取 k 近邻投票；`n_neighbors` 控制边界平滑度（偏差-方差权衡），`metric='minkowski'` + `p` 组合定义距离（p=1 曼哈顿、p=2 欧氏）。
- 基于距离的 KNN 必须标准化特征（代码用 `X_train_std`），基于阈值的决策树不必（代码直接用 `X_train`）——同章代码里的这一对照是判断「模型是否需要缩放」的最好经验法则。
- 维数灾难使 KNN 在高维空间失效：距离失去区分度、所需样本量指数增长，KNN 只适合低维稠密场景。

## 7. 下一步学习建议

本讲结束了第 3 章的分类器漫游。至此你已见过感知机、逻辑回归、SVM、决策树、KNN 五类模型，它们在鸢尾花数据上的准确率都已接近上限——是时候换更难的数据和更系统的评估方法了。建议顺序：

1. **下一单元 u4（第 4 章）**：真实数据集（wine）上的数据清洗、缺失值处理、类别编码与特征选择。你会发现模型只是流程的一小部分，数据预处理才是日常工作的主体。
2. **回看本章随机森林**：[ch03/ch03.py:712-725](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch03/ch03.py#L712-L725) 的 `RandomForestClassifier` 组合了 25 棵本讲这样的树。读完 u7（第 7 章集成学习）后回来重看，你会明白 `n_estimators`、bootstrap 与投票为什么能显著降低单棵树的方差。
3. **带着问题进入第 6 章（u6）**：本讲反复调 `max_depth` 和 `n_neighbors`，靠的是肉眼和单一测试集。第 6 章的交叉验证、验证曲线会把这些「手动扫描」变成严谨、可复现的流程——尤其 `validation_curve` 正是为扫描 `n_neighbors` 这类超参数设计的。
