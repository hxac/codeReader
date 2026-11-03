# 数据清洗与类别特征编码

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 pandas 读入 CSV 数据，用 `isnull().sum()` 快速探查每一列的缺失值数量，并用 `dropna` 家族（`axis`、`how`、`thresh`、`subset`）按不同口径删除缺失数据。
2. 用 `SimpleImputer`（fit/transform 两步）或 `fillna` 把缺失值填补为列均值、中位数等统计量，并理解两者在工程上的差别。
3. 区分**标称特征**（nominal，无序，如颜色）与**序数特征**（ordinal，有序，如 S/M/L）：会用 `map` 加字典做自定义顺序的序数编码，会用 `LabelEncoder` 编码类别标签并用 `inverse_transform` 还原，会用 `get_dummies` / `OneHotEncoder` 做独热（one-hot）编码，并明白为什么不能用 LabelEncoder 编码无序的标称特征。
4. 掌握 wine 数据集的读入、`iloc` 特征/标签切分，以及 `train_test_split` 的 70/30 分层划分。
5. 理解「脏数据进、干净数据出」是建模之前的第一道工序：缺失值与类别文本是真实数据最常见的两类「不能直接喂给模型」的问题。

前三讲里，数据（鸢尾花）都是现成的数值矩阵，拿过来就能训练。本讲进入第 4 章的第一半：**真实世界的数据先要清洗和编码**。本章后半（特征缩放回顾与特征选择）留给下一讲。

## 2. 前置知识

阅读本讲前，请确认你已理解前置讲义（u1-l3、u2-l3、u3-l1）中的以下概念，本讲直接使用、不再重复推导：

- **pandas 读 CSV 与 DataFrame**：`pd.read_csv(path, header=None)` 把无表头 CSV 读成表格型 DataFrame；`.iloc` 按位置切行 列，`.values` 取出底层 numpy 数组（见 u1-l3 的鸢尾花加载）。
- **fit / transform 与数据泄漏**：`StandardScaler` 遵循「只在训练集 `fit`、新数据只 `transform`」的原则（u2-l3、u3-l1）。本讲的 `SimpleImputer` 是同一范式下的另一个转换器。
- **train_test_split 与 stratify 分层抽样**：`stratify=y` 保持训练/测试集类别比例与整体一致，`random_state` 保证划分可复现（u3-l1）。
- **估计器统一接口**：换模型只换类名，流程不变（u3-l1）。

再补充四个本讲要用的新直觉：

- **NaN（Not a Number）**：pandas 表示「缺失值」的记号。CSV 里空着的单元格读进来就是 `NaN`；任何数与 `NaN` 运算结果还是 `NaN`，所以缺失值必须先处理。
- **标称特征（nominal feature）**：取值只是「名字」，之间没有大小顺序，如颜色 green/red/blue、衬衫颜色。直接编码成 0/1/2 会凭空制造顺序和距离。
- **序数特征（ordinal feature）**：取值有自然顺序但间隔未必相等，如衣服尺码 M < L < XL。这类特征适合映射成保持顺序的整数。
- **多重共线性（multicollinearity）**：若干特征之间存在严格的线性关系（如一组的 0/1 列之和恒等于 1），会让线性模型的权重求解不稳定——这是独热编码要「删掉一列」的原因。

## 3. 本讲源码地图

本讲内容几乎全部来自一个文件：

| 文件 | 作用 |
| --- | --- |
| `ch04/ch04.py` | 第 4 章脚本版代码。本讲涉及四段：L82–113（缺失值探查）、L117–184（dropna 删除与 SimpleImputer/fillna 填补）、L198–349（类别特征编码：map、LabelEncoder、OneHotEncoder、get_dummies）、L353–386（wine 数据集读入与 train_test_split 划分） |
| `ch04/ch04.ipynb` | 同一内容的 notebook 版，适合交互式跑本讲实践 |
| `ch04/wine.data` | wine 数据集本地副本（178 行、无表头、逗号分隔），书里从 UCI 网址下载失败时的备选路径，也是本讲实践推荐的数据源 |

背景说明（本讲不精读，仅交代位置）：`ch04/ch04.py:390-403` 紧接着是 `MinMaxScaler` / `StandardScaler` 的特征缩放回顾（已在 u2-l3 讲过）；`ch04/ch04.py:537` 起是手写的 SBS 序列特征选择类，属于下一讲 u4-l2 的内容。

## 4. 核心概念与源码讲解

### 4.1 缺失值探查：isnull().sum() 与 df.values

#### 4.1.1 概念说明

真实表格数据里，缺失值几乎是必然出现的：问卷漏答、传感器掉线、数据库字段为空。缺失值在 pandas 里用 `NaN` 表示，它有一个麻烦的性质——**传染性**：任何包含 `NaN` 的算术运算结果都是 `NaN`。所以模型训练之前必须先回答两个问题：缺了多少？缺在哪？

书中用一个手写的迷你 CSV 演示。这个 CSV 有 3 行 4 列，其中 C 列第 2 行、D 列第 3 行是空的：

```text
A,    B,    C,    D
1.0,  2.0,  3.0,  4.0
5.0,  6.0,  ,     8.0      ← C 缺失
10.0, 11.0, 12.0,          ← D 缺失
```

探查工具是两步组合：

- `df.isnull()` 返回一个**同形状的布尔 DataFrame**，缺失处为 `True`，其余为 `False`。
- 对布尔表 `.sum()` 时 pandas 把 `True` 当 1、`False` 当 0 按列求和，得到「每列缺失个数」的 Series。

#### 4.1.2 核心流程

```text
1. 构造含空单元格的 CSV 字符串
2. pd.read_csv(StringIO(csv_data)) 读入 DataFrame
   （空单元格自动变成 NaN；read_csv 也接受文件路径/URL）
3. df.isnull().sum()  → 逐列缺失计数
4. df.values          → 取出底层 numpy 数组（ sklearn 估计器最终消费的形态）
```

一个容易忽略的细节：CSV 里的空单元格只有**在数值列**里才会成为 `NaN`；如果一整列都被解析成字符串，空单元格就成了空字符串而不是 `NaN`，`isnull()` 就数不出来了。所以「先确认各列 dtype 是否符合预期」是探查缺失前的隐含步骤。

#### 4.1.3 源码精读

构造迷你 CSV 并读入 DataFrame——[ch04/ch04.py:89-101](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L89-L101)：

```python
csv_data = '''A,B,C,D
1.0,2.0,3.0,4.0
5.0,6.0,,8.0
10.0,11.0,12.0,'''

df = pd.read_csv(StringIO(csv_data))
```

这段代码用 `StringIO` 把多行字符串包装成「像文件一样的对象」，于是 `pd.read_csv` 可以不落盘直接读。日常使用时把 `StringIO(csv_data)` 换成文件路径或 URL 即可——第 4.4 节读 wine 数据集用的就是 URL。

逐列统计缺失值——[ch04/ch04.py:106](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L106)：

```python
df.isnull().sum()
```

在迷你 CSV 上的输出是 `A:0, B:0, C:1, D:1`，与上面的表格一一对应。

取出底层 numpy 数组——[ch04/ch04.py:111-113](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L111-L113)：

```python
# access the underlying NumPy array
# via the `values` attribute
df.values
```

`values` 会丢掉列名与行索引，只留二维 ndarray。scikit-learn 的估计器接受 DataFrame 也接受 ndarray，但书里大量示例在交给模型前先 `.values`，让数据的「形状」一目了然。

#### 4.1.4 代码实践

1. **实践目标**：亲手复现缺失值探查，看懂两步组合各自的返回形态。
2. **操作步骤**：新建 Python 会话（无需下载数据），逐行运行：

   ```python
   import pandas as pd
   from io import StringIO

   csv_data = '''A,B,C,D
   1.0,2.0,3.0,4.0
   5.0,6.0,,8.0
   10.0,11.0,12.0,'''

   df = pd.read_csv(StringIO(csv_data))
   print(df)                  # 看 NaN 出现在哪里
   print(df.isnull())         # 布尔表：True 即缺失
   print(df.isnull().sum())   # 每列缺失计数
   print(df.values)           # numpy 数组形态
   ```

3. **需要观察的现象**：`print(df)` 中缺失处显示 `NaN`；`df.isnull()` 的表格形状与 `df` 完全相同；`df.values` 的输出没有列名。
4. **预期结果**：`isnull().sum()` 输出 `A 0, B 0, C 1, D 1`；`df.values` 是 3×4 的 float 数组，缺失位置显示 `nan`。

#### 4.1.5 小练习与答案

**练习 1**：把 C 列第 1 行也改成缺失（`df.iloc[0, 2] = np.nan`），`df.isnull().sum()` 会输出什么？

> **答案**：`A 0, B 0, C 2, D 1`。第 1 行（索引 0）的 C 由 3.0 变成 NaN，C 列计数从 1 变 2。

**练习 2**：为什么 `df.isnull().sum()` 能数出每列缺失个数，而不是全表总数？

> **答案**：pandas 的 `sum()` 默认沿 `axis=0`（按列）汇总；布尔列求和时 `True` 记 1。若想看全表总数，再补一次 `.sum()`（即 `df.isnull().sum().sum()`）。

**练习 3**：`df` 与 `df.values` 的核心区别是什么？为什么书里常在建模前 `.values`？

> **答案**：`df` 是带行列标签的 DataFrame，`df.values` 是剥掉标签的 numpy ndarray。scikit-learn 估计器接受两种输入，取 `.values` 后形状与 dtype 更直观，也避免了 pandas 索引对齐带来的隐性麻烦。

### 4.2 处理缺失值：dropna 删除与 SimpleImputer / fillna 填补

#### 4.2.1 概念说明

处理缺失值只有两条大路线：

1. **删除（drop）**：把含缺失的行或列整个丢掉。简单粗暴，数据本来就多、缺失很少时可行；缺失集中在少数几列时删列也合理。代价是丢信息——删掉 30% 的行，样本量就少 30%。
2. **填补（impute）**：用该列的统计量（均值、中位数、众数）或模型预测值把洞填上。保留样本量，但引入了「猜测值」——方差被人为压低，均值填补对离群值敏感（中位数更稳健）。

`dropna` 的四个参数决定了删除的口径：

| 参数 | 含义 | 迷你 CSV 上的效果 |
| --- | --- | --- |
| `axis=0` / `axis=1` | 删行 / 删列 | 删行只剩第 0 行；删列只剩 A、B 两列 |
| `how='all'` | 整行全是 NaN 才删 | 没有这样的行，原样保留 |
| `thresh=4` | 至少保留 4 个非缺失值 | 只有第 0 行有 4 个实值，只剩它（等价于删行） |
| `subset=['C']` | 只看 C 列是否缺失 | 删掉第 1 行，保留第 0、2 行 |

填补用的 `SimpleImputer` 是一个**转换器（transformer）**：`fit` 从数据里学出每列的统计量（比如均值），`transform` 把统计量填进缺失位置。这与 `StandardScaler` 完全同构——同一套 fit/transform 接口意味着它可以进 Pipeline、可以把训练集学到的统计量原样应用到新数据（回忆 u3-l1 的防泄漏原则）。而 `fillna(df.mean())` 是 pandas 的一次性快捷操作，学到的统计量不会被「记住」，不能复用到新数据。

均值填补的语义是：对含缺失的第 j 列，用训练数据中该列观测值的均值 \(\bar{x}_j = \frac{1}{n_j}\sum_{i \in \text{observed}} x_{ij}\) 替换所有 `NaN`。

#### 4.2.2 核心流程

```text
删除路线：
  df.dropna(axis=0)            # 任一列缺失 → 删该行
  df.dropna(axis=1)            # 任一行缺失 → 删该列
  df.dropna(how='all')         # 整行全缺才删
  df.dropna(thresh=k)          # 非缺失值不足 k 个才删
  df.dropna(subset=[...])      # 只按指定列判断

填补路线（估计器风格）：
  imr = SimpleImputer(missing_values=np.nan, strategy='mean')
  imr = imr.fit(X)        # 学每列均值
  X_filled = imr.transform(X)  # 填洞（fit_transform 一步完成）

填补路线（pandas 快捷方式）：
  df.fillna(df.mean())
```

注意 `SimpleImputer` 的输入是二维结构（DataFrame 或形状 `(n_samples, n_features)` 的数组）；喂一维 Series 会报错，这和所有 sklearn 转换器一致。

#### 4.2.3 源码精读

dropna 家族的五种删除口径——[ch04/ch04.py:121-158](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L121-L158)：

```python
# remove rows that contain missing values
df.dropna(axis=0)

# remove columns that contain missing values
df.dropna(axis=1)

# only drop rows where all columns are NaN
df.dropna(how='all')

# drop rows that have fewer than 3 real values
df.dropna(thresh=4)

# only drop rows where NaN appear in specific columns (here: 'C')
df.dropna(subset=['C'])
```

五行分别对应上表的五种口径。`thresh=4` 那行源码注释写的是 "fewer than 3 real values"——保留条件是非缺失值 ≥ thresh，即 4 列全有实值的行才留下，在本例与 `axis=0` 结果相同但口径不同（若某行只缺一个值，`axis=0` 会删它、`thresh=4` 也会删它；而 `thresh=3` 就会留下它）。

用 SimpleImputer 按列均值填补——[ch04/ch04.py:172-178](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L172-L178)：

```python
imr = SimpleImputer(missing_values=np.nan, strategy='mean')
imr = imr.fit(df.values)
imputed_data = imr.transform(df.values)
```

三步就是转换器范式的标准姿势：构造（指定缺失记号与策略）→ `fit` 学每列均值 → `transform` 填洞。迷你 CSV 上 C 列均值是 (3.0+12.0)/2 = 7.5，D 列均值是 (4.0+8.0)/2 = 6.0，两个洞分别被这两个数填上。

pandas 一行等价写法——[ch04/ch04.py:184](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L184)：

```python
df.fillna(df.mean())
```

`df.mean()` 本身就跳过 NaN（所以均值不会被子集里的 NaN 拉歪），fillna 把各列均值填回各列。结果数值与 SimpleImputer 相同，差别在「是否留下一个可复用的估计器对象」。

补充一个书中本章未使用、但同属清洗工具箱的方法：真实数据里缺失常被记成哨兵值（如 0 或 `'?'`），可用 `df.replace(0, np.nan)` 先换成 NaN 再统一走上面的流程——此为通用 pandas 技法，非本章源码内容。

#### 4.2.4 代码实践

1. **实践目标**：在同一个迷你 CSV 上跑遍五种删除口径和两种填补方式，把「口径 → 结果形状」对应起来。
2. **操作步骤**：

   ```python
   import pandas as pd
   import numpy as np
   from io import StringIO
   from sklearn.impute import SimpleImputer

   csv_data = '''A,B,C,D
   1.0,2.0,3.0,4.0
   5.0,6.0,,8.0
   10.0,11.0,12.0,'''
   df = pd.read_csv(StringIO(csv_data))

   for label, out in [('axis=0', df.dropna(axis=0)),
                      ('axis=1', df.dropna(axis=1)),
                      ("how='all'", df.dropna(how='all')),
                      ('thresh=4', df.dropna(thresh=4)),
                      ("subset=['C']", df.dropna(subset=['C']))]:
       print(f'{label:14s} → shape {out.shape}')

   imr = SimpleImputer(missing_values=np.nan, strategy='mean')
   print(imr.fit_transform(df.values))
   print(df.fillna(df.mean()).values)
   ```

3. **需要观察的现象**：五种 dropna 得到的形状各不相同；两种填补得到的数组数值完全一致。
4. **预期结果**：形状依次为 `(1, 4)`、`(3, 2)`、`(3, 4)`、`(1, 4)`、`(2, 4)`；填补数组中 C 列的洞是 `7.5`、D 列的洞是 `6.0`。若把 `strategy='mean'` 改成 `'median'`，本例数值不变（每列只有两个观测值时均值=中位数）。

#### 4.2.5 小练习与答案

**练习 1**：`df.dropna(subset=['C'])` 与 `df.dropna(axis=0)` 在迷你 CSV 上的结果差在哪一行？为什么？

> **答案**：`subset=['C']` 多保留了第 3 行（索引 2）。该行 D 缺失但 C=12.0 完好——`axis=0` 因「任一列缺失」删它，`subset` 只盯 C 列，D 缺不缺不影响判断。

**练习 2**：把 `strategy='mean'` 换成 `'median'` 和 `'most_frequent'` 各适合什么场景？

> **答案**：`'median'` 适合有离群值的数值列（中位数不被极端值拉动）；`'most_frequent'` 适合类别列（均值无意义，只能用众数），也可用于离散数值列。

**练习 3**：从「防数据泄漏」的角度，`SimpleImputer` 应该在什么时候 `fit`？

> **答案**：只在训练集上 `fit`，测试集/新数据只 `transform`——填洞用的均值/中位数是训练集的统计量，若拟合时偷看了测试集就构成泄漏。书中 L175-178 是在演示玩具数组上直接整体拟合，未做划分；到了真实流程（以及本讲综合实践）应先划分再对训练集拟合。

### 4.3 类别特征编码：map、LabelEncoder 与 get_dummies

#### 4.3.1 概念说明

scikit-learn 的估计器只吃数值数组，但真实数据的列常常是文字。处理前先分清两类：

- **序数特征**：有序，如尺码 M < L < XL。映射成 1/2/3 恰好保留顺序信息，是**正确**做法。
- **标称特征**：无序，如颜色 green/red/blue。若也映射成 1/2/3，模型会认为 blue(0) < green(1) < red(2)，green 与 blue 的「距离」和 green 与 red 的「距离」不同——这些大小与距离关系全是凭空捏造的。

书中构造的演示表正好两者都有：

```text
color  size  price  classlabel
green  M     10.1   class2
red    L     13.5   class1
blue   XL    15.3   class2
```

- `size` 是序数 → 用 `map` 加自定义字典映射：`{'XL': 3, 'L': 2, 'M': 1}`，顺序由**你**定义，不是按字母序。反向还原只需把字典键值对调（字典推导式）。
- `classlabel` 是**类别标签**（要预测的目标 y）→ 标签编码成 0/1 即可（标签不进模型当特征，顺序无所谓），`LabelEncoder` 还提供 `inverse_transform` 把数字还原成原始文字。
- `color` 是标称特征 → 唯一安全的做法是**独热编码（one-hot）**：把一列拆成每类别一列的 0/1 指示变量。green → `[0, 1, 0]`、red → `[0, 0, 1]`、blue → `[1, 0, 0]`，三个向量两两等距，没有任何假顺序。

独热编码有一个数学副作用——多重共线性。设三列指示变量为 \(d_1, d_2, d_3\)，则每行必有 \(d_1 + d_2 + d_3 = 1\)，即第三列完全由前两列（加截距）决定。对线性模型，这会让权重求解不稳定。解决办法是删掉一个类别列（`drop_first=True`）：被删类别由「其余列全 0」隐式表示，信息无损。

实现工具三选一：`pd.get_dummies`（pandas 一行式，最常用）、`OneHotEncoder`（sklearn 转换器，可进 Pipeline）、`OneHotEncoder` + `ColumnTransformer`（只对指定列独热、其余列原样通过）。

#### 4.3.2 核心流程

```text
序数特征（map）：
  size_mapping = {'XL': 3, 'L': 2, 'M': 1}   # 自定义顺序
  df['size'] = df['size'].map(size_mapping)
  还原：{v: k for k, v in size_mapping.items()} 再 map 一次

类别标签（LabelEncoder）：
  手写版：class_mapping = {label: idx for idx, label in enumerate(np.unique(y))}
  sklearn 版：y = LabelEncoder().fit_transform(y)；inverse_transform(y) 还原

标称特征（one-hot）：
  pd.get_dummies(df[['price', 'color', 'size']])                # 全部标称列自动展开
  pd.get_dummies(..., drop_first=True)                          # 每组删 1 列防共线
  sklearn：OneHotEncoder + ColumnTransformer（passthrough 保留其余列）
```

#### 4.3.3 源码精读

构造演示表——[ch04/ch04.py:205-210](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L205-L210)：

```python
df = pd.DataFrame([['green', 'M', 10.1, 'class2'],
                   ['red', 'L', 13.5, 'class1'],
                   ['blue', 'XL', 15.3, 'class2']])

df.columns = ['color', 'size', 'price', 'classlabel']
```

四列正好覆盖三种角色：标称特征（color）、序数特征（size）、数值特征（price）、类别标签（classlabel）。

序数映射与还原——[ch04/ch04.py:218-229](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L218-L229)：

```python
size_mapping = {'XL': 3,
                'L': 2,
                'M': 1}

df['size'] = df['size'].map(size_mapping)
...
inv_size_mapping = {v: k for k, v in size_mapping.items()}
df['size'].map(inv_size_mapping)
```

`map` 拿字典做「查表替换」：M→1、L→2、XL→3，顺序关系被保留。`Series.map` 也接受函数（综合实践会用到），但字典最直观。还原用的字典推导式 `{v: k for k, v in d.items()}` 把键值对调，是通用的「逆映射」技巧。

类别标签编码（手写字典版）——[ch04/ch04.py:240-256](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch04/ch04.py#L240-L256)：

```python
class_mapping = {label: idx for idx, label in enumerate(np.unique(df['classlabel']))}
df['classlabel'] = df['classlabel'].map(class_mapping)
...
inv_class_mapping = {v: k for k, v in class_mapping.items()}
df['classlabel'] = df['classlabel'].map(inv_class_mapping)
```

`np.unique` 先排序再去重（得到 `['class1', 'class2']`），`enumerate` 配上字典推导式生成 `{'class1': 0, 'class2': 1}`——标签按**字母序**编号。这与上一节的 `size_mapping` 是同一个 map 套路，只是映射表由代码自动生成。

类别标签编码（sklearn 版）与还原——[ch04/ch04.py:263-271](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L263-L271)：

```python
class_le = LabelEncoder()
y = class_le.fit_transform(df['classlabel'].values)

# reverse mapping
class_le.inverse_transform(y)
```

`LabelEncoder` 与手写字典结果一致（也是按排序编号），但作为估计器它**记住了映射表**：`inverse_transform` 随时把预测出的整数标签还原成原始文字——报告里要显示 "class2" 而不是 1 时就靠它。

对 color 列做 LabelEncoder 编码（一个**反面示例**）——[ch04/ch04.py:279-282](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L279-L282)：

```python
X = df[['color', 'size', 'price']].values
color_le = LabelEncoder()
X[:, 0] = color_le.fit_transform(X[:, 0])
```

按字母序 blue=0、green=1、red=2，color 列变成 `[1, 2, 0]`。代码能跑，但正如 4.3.1 分析的，这给无序的颜色强加了顺序——书里放这一段正是为了引出独热编码。

独热编码的三种实现——[ch04/ch04.py:288-323](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L288-L323)：

```python
color_ohe = OneHotEncoder()
color_ohe.fit_transform(X[:, 0].reshape(-1, 1)).toarray()

c_transf = ColumnTransformer([ ('onehot', OneHotEncoder(), [0]),
                               ('nothing', 'passthrough', [1, 2])])
c_transf.fit_transform(X).astype(float)

pd.get_dummies(df[['price', 'color', 'size']])

pd.get_dummies(df[['price', 'color', 'size']], drop_first=True)

color_ohe = OneHotEncoder(categories='auto', drop='first')
c_transf = ColumnTransformer([ ('onehot', color_ohe, [0]),
                               ('nothing', 'passthrough', [1, 2])])
c_transf.fit_transform(X).astype(float)
```

四个片段层层递进：① `OneHotEncoder` 只吃二维输入，所以单列要 `reshape(-1, 1)`（-1 表示行数自动推断），输出稀疏矩阵需 `toarray()` 看内容；② `ColumnTransformer` 对第 0 列独热、第 1、2 列 `'passthrough'` 原样放行——混合类型特征表的一次性处理方案；③ `get_dummies` 一行完成：自动找出标称列展开、数值列不动，`size` 此刻已是 4.3.3 开头映射过的 1/2/3，故被当作数值列保留；④ `drop_first=True` 与 `OneHotEncoder(drop='first')` 是同一「删一列防共线」思想的 pandas/sklearn 双实现。

关于 `get_dummies` 的一个版本细节：书中钉住的 pandas 1.3.2 生成 0/1 整数列；pandas 2.0 起默认生成 `bool` 列，若后续模型或运算需要数值，补一步 `.astype(int)` 即可（通用版本说明，非本章源码内容）。

可选的阈值编码（ordinal 的另一种处理）——[ch04/ch04.py:333-349](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac337afc3c0a91/ch04/ch04.py#L333-L349)：

```python
df['x > M'] = df['size'].apply(lambda x: 1 if x in {'L', 'XL'} else 0)
df['x > L'] = df['size'].apply(lambda x: 1 if x == 'XL' else 0)

del df['size']
```

当序数值之间的「差」没有明确含义时，可以改用阈值拆分：M/L/XL 变成两列 0/1（`x > M`、`x > L`），等价于一种二值化的独热变体。`apply` + lambda 是 pandas 逐元素自定义变换的通用写法。

#### 4.3.4 代码实践

1. **实践目标**：在同一个演示表上完成序数映射、标签编码与独热编码，并亲眼对比 `drop_first` 前后的列变化。
2. **操作步骤**：

   ```python
   import numpy as np
   import pandas as pd
   from sklearn.preprocessing import LabelEncoder

   df = pd.DataFrame([['green', 'M', 10.1, 'class2'],
                      ['red', 'L', 13.5, 'class1'],
                      ['blue', 'XL', 15.3, 'class2']])
   df.columns = ['color', 'size', 'price', 'classlabel']

   # 1) 序数映射 + 还原
   size_mapping = {'XL': 3, 'L': 2, 'M': 1}
   df['size'] = df['size'].map(size_mapping)
   print(df['size'].tolist())                       # [1, 2, 3]
   inv = {v: k for k, v in size_mapping.items()}
   print(df['size'].map(inv).tolist())              # ['M', 'L', 'XL']

   # 2) 标签编码 + 还原
   le = LabelEncoder()
   y = le.fit_transform(df['classlabel'].values)
   print(y)                                          # [1 0 1]
   print(le.inverse_transform(y))                    # ['class2' 'class1' 'class2']

   # 3) 独热编码：对比 drop_first
   d1 = pd.get_dummies(df[['price', 'color', 'size']])
   d2 = pd.get_dummies(df[['price', 'color', 'size']], drop_first=True)
   print(d1.columns.tolist())
   print(d2.columns.tolist())
   ```

3. **需要观察的现象**：`size` 映射后再逆映射能完全复原；`LabelEncoder` 把 class1 排在 class2 前面；`drop_first=True` 后 color 只剩两个指示列。
4. **预期结果**：`y` 为 `[1 0 1]`；`d1` 的列是 `['price', 'color_blue', 'color_green', 'color_red', 'size']`，`d2` 少了 `color_blue`（按字母序删第一列），blue 由 green/red 全 0 隐式表示。

#### 4.3.5 小练习与答案

**练习 1**：为什么对 `color` 列用 `LabelEncoder` 是错的，对 `classlabel` 列用却是可以的？

> **答案**：`color` 是**输入特征**，编码成 0/1/2 后模型会把数值大小与差值当真实信息（假顺序、假距离）；`classlabel` 是**输出标签**，不参与距离计算，整数编号只是类别的代号，预测后可用 `inverse_transform` 还原，所以顺序无碍。

**练习 2**：`size_mapping` 映射后 `df['size']` 是 `[1, 2, 3]`，写出逆映射字典并说明为什么能完全复原。

> **答案**：`{1: 'M', 2: 'L', 3: 'XL'}`。字典推导式 `{v: k for k, v in size_mapping.items()}` 把键值互换；因为原映射是一一对应（双射），互换后仍是合法映射，逐一查表即可复原。

**练习 3**：`get_dummies(..., drop_first=True)` 删掉了哪个类别列？为什么删一列不损失信息？

> **答案**：删掉按字母序的第一个类别列（本例 `color_blue`）。任一行各组指示列之和恒为 1，故「其余列全 0」即代表被删类别，信息可由剩余列完全推出；同时消除了与截距项的严格线性关系（多重共线性）。

### 4.4 wine 数据集：iloc 切分与 train_test_split 分层划分

#### 4.4.1 概念说明

前面三节都在玩具表上练手，本节换成贯穿本章及第 5 章（PCA）的真实数据集——**wine（葡萄酒）数据集**：178 个样本、13 个数值特征（酒精度、苹果酸、灰分、镁含量……），标签是意大利三个产区（类别 1/2/3，各 59/71/48 个样本）。它是多分类（3 类）问题，且特征量纲差异巨大（Proline 上千、Hue 个位数）——后者正是下一章 PCA 必须先标准化的原因。

本节的两个动作：

1. **特征与标签切分**：第 0 列是 `Class label`，其余 13 列是特征。`df_wine.iloc[:, 1:].values` 取 X（形状 (178, 13)），`df_wine.iloc[:, 0].values` 取 y（形状 (178,)）。注意与鸢尾花流程相反——那里标签在最后一列。
2. **分层划分**：`train_test_split(X, y, test_size=0.3, random_state=0, stratify=y)`。`stratify=y` 让训练/测试集里三类比例与整体 59:71:48 一致——类别不平衡或小数据集时，纯随机划分可能让某一类在测试集里寥寥无几，分层抽样消除这种运气成分。

关于标签编码：wine 的标签**本来就是整数** 1/2/3（`np.unique` 输出 `[1 2 3]`），不需要 `LabelEncoder`。但 4.3 节的 `LabelEncoder` / `inverse_transform` 在标签为字符串时（例如鸢尾花的品种名）就派上用场——先把标签变成整数交给模型，预测后再还原成文字。

#### 4.4.2 核心流程

```text
1. 读入：pd.read_csv(URL 或本地 'wine.data', header=None)
   → 178×14 的 DataFrame（无表头，第一列就是标签）
2. 赋列名：df_wine.columns = ['Class label', 'Alcohol', ..., 'Proline']
3. 检查：np.unique(df_wine['Class label']) → [1 2 3]；df_wine.head() 肉眼确认
4. 切 X / y：X = iloc[:, 1:].values (178×13)；y = iloc[:, 0].values (178,)
5. 划分：train_test_split(X, y, test_size=0.3, random_state=0, stratify=y)
   → X_train (124, 13)、X_test (54, 13)、y_train (124,)、y_test (54,)
```

#### 4.4.3 源码精读

读入 wine 数据集（网络优先、本地兜底）——[ch04/ch04.py:357-365](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L357-L365)：

```python
df_wine = pd.read_csv('https://archive.ics.uci.edu/'
                      'ml/machine-learning-databases/wine/wine.data',
                      header=None)

# if the Wine dataset is temporarily unavailable from the
# UCI machine learning repository, un-comment the following line
# of code to load the dataset from a local path:

# df_wine = pd.read_csv('wine.data', header=None)
```

`header=None` 告诉 pandas 第一行不是表头（与 u1-l3 读鸢尾花一致）。UCI 的 URL 有时失效，注释里留了本地兜底——仓库的 `ch04/wine.data` 就是这个文件（178 行），在 `ch04/` 目录下运行时直接取消注释即可，本讲实践推荐用本地副本。

赋列名并检查标签——[ch04/ch04.py:368-375](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L368-L375)：

```python
df_wine.columns = ['Class label', 'Alcohol', 'Malic acid', 'Ash',
                   'Alcalinity of ash', 'Magnesium', 'Total phenols',
                   'Flavanoids', 'Nonflavanoid phenols', 'Proanthocyanins',
                   'Color intensity', 'Hue', 'OD280/OD315 of diluted wines',
                   'Proline']

print('Class labels', np.unique(df_wine['Class label']))
df_wine.head()
```

读入时无表头，列名来自 UCI 官方文档 `wine.names.txt`（仓库 `ch04/` 下有），手工赋给 `df_wine.columns`——13 个特征名从此可用，比 `df_wine.iloc[:, 4]` 这类位置引用可读得多。`np.unique` 顺带完成「标签取值盘点」：`[1 2 3]`。

特征/标签切分与 70/30 分层划分——[ch04/ch04.py:381-386](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L381-L386)：

```python
X, y = df_wine.iloc[:, 1:].values, df_wine.iloc[:, 0].values

X_train, X_test, y_train, y_test =    train_test_split(X, y,
                     test_size=0.3,
                     random_state=0,
                     stratify=y)
```

一行完成两件事：`iloc[:, 1:]` 丢掉第 0 列标签得 X，`iloc[:, 0]` 得 y，`.values` 直接落到 numpy。`train_test_split` 的三个参数各司其职：`test_size=0.3` 划出 30%（54 个）样本做测试；`random_state=0` 固定随机种子，任何人任何机器重跑都得到同一划分；`stratify=y` 按类别分层。178 × 0.3 = 53.4，向上取整得 54 个测试样本、124 个训练样本。这里的 X、y 与划分结果会被本章后续（缩放、SBS）和第 5 章（PCA/LDA）反复使用。

#### 4.4.4 代码实践

1. **实践目标**：从本地 `wine.data` 走完「读入 → 赋列名 → 切 X/y → 分层划分」，并核对每一步的形状与类别分布。
2. **操作步骤**：在 `ch04/` 目录下（保证能找到 `wine.data`）运行：

   ```python
   import numpy as np
   import pandas as pd
   from sklearn.model_selection import train_test_split

   df_wine = pd.read_csv('wine.data', header=None)
   df_wine.columns = ['Class label', 'Alcohol', 'Malic acid', 'Ash',
                      'Alcalinity of ash', 'Magnesium', 'Total phenols',
                      'Flavanoids', 'Nonflavanoid phenols', 'Proanthocyanins',
                      'Color intensity', 'Hue', 'OD280/OD315 of diluted wines',
                      'Proline']

   print('整体形状:', df_wine.shape)                      # (178, 14)
   print('标签取值:', np.unique(df_wine['Class label']))   # [1 2 3]
   print('各类计数:', np.bincount(df_wine['Class label'].values))

   X, y = df_wine.iloc[:, 1:].values, df_wine.iloc[:, 0].values
   print('X 形状:', X.shape, '| y 形状:', y.shape)        # (178, 13) | (178,)

   X_train, X_test, y_train, y_test = train_test_split(
       X, y, test_size=0.3, random_state=0, stratify=y)
   print('X_train:', X_train.shape, 'X_test:', X_test.shape)
   print('训练集各类:', np.bincount(y_train))
   print('测试集各类:', np.bincount(y_test))
   ```

   注意 `np.bincount` 要求非负整数，标签 1/2/3 正好满足（它会从 0 开始计数，第 0 位为 0 属正常）。
3. **需要观察的现象**：划分后训练集与测试集的类别比例都接近 59:71:48；换一个 `random_state` 划分结果不同，但比例仍然接近。
4. **预期结果**：`df_wine.shape` 为 `(178, 14)`；`X_train.shape` 为 `(124, 13)`、`X_test.shape` 为 `(54, 13)`；三类的训练/测试计数大致按比例分配（精确计数请以本地输出为准，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：wine 数据集里 X 和 y 分别取哪些列？与鸢尾花流程有什么不同？

> **答案**：X 取第 1 到 13 列（`iloc[:, 1:]`，13 个化学特征），y 取第 0 列（`iloc[:, 0]`，Class label）。鸢尾花数据里标签在**最后一列**（u1-l3 用 `iloc[:, [0, 2]].values` 取特征、`pd.factorize`/`np.where` 处理第 4 列标签），wine 则标签在**第一列**——切分前必须先看清标签在哪。

**练习 2**：去掉 `stratify=y` 可能带来什么风险？`random_state=0` 的作用又是什么？

> **答案**：纯随机划分在小样本（每类 48~71 个）上可能让某类在测试集中占比明显偏低甚至缺失，评估结果受运气影响；`stratify=y` 强制两类子集类别比例与整体一致。`random_state=0` 固定随机数种子，让划分可复现——不同种子给出不同划分，但都会遵守分层约束。

**练习 3**：wine 的标签需要 `LabelEncoder` 吗？什么时候需要？

> **答案**：不需要——标签已是整数 1/2/3（`np.unique` 直接输出）。当标签是字符串（如鸢尾花的 `Setosa/Versicolor/Virginica`）时才需要：`fit_transform` 编码成整数供模型使用，预测后 `inverse_transform` 还原成原始文字。

## 5. 综合实践

把本讲四个模块串成一条完整的清洗管线，任务是规格中指定的：**对 wine 数据集删除含 NaN 的行、用中位数填补另一列、把 Alcohol 列映射为自定义类别、对两个标称列独热编码，最后分层划出 30% 测试集，并打印每一步后的形状。**

wine 原始数据没有缺失，所以第一步先在**副本**上人为制造两个 NaN（模拟真实场景）。完整脚本（示例代码，在 `ch04/` 目录下运行；只改内存中的 DataFrame，不动 `wine.data` 文件本身）：

```python
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.model_selection import train_test_split

# Step 0: 读入 + 赋列名
df = pd.read_csv('wine.data', header=None)
df.columns = ['Class label', 'Alcohol', 'Malic acid', 'Ash',
              'Alcalinity of ash', 'Magnesium', 'Total phenols',
              'Flavanoids', 'Nonflavanoid phenols', 'Proanthocyanins',
              'Color intensity', 'Hue', 'OD280/OD315 of diluted wines',
              'Proline']
print('Step 0 读入:', df.shape)                 # 预期 (178, 14)

# Step 1: 制造缺失 → 删除含 NaN 的行（dropna 路线）
df.loc[[0, 5], 'Ash'] = np.nan
print('Step 1 缺失计数:', df.isnull().sum().sum())   # 预期 2
df = df.dropna(axis=0)
print('Step 1 删行后:', df.shape)               # 预期 (176, 14)

# Step 2: 另一列制造缺失 → SimpleImputer 中位数填补（impute 路线）
df.loc[10, 'Magnesium'] = np.nan
imr = SimpleImputer(missing_values=np.nan, strategy='median')
df['Magnesium'] = imr.fit_transform(df[['Magnesium']].values).ravel()
print('Step 2 填补后:', df.shape, '| 剩余 NaN:', df.isnull().sum().sum())  # 预期 (176, 14) | 0

# Step 3: 把 Alcohol 映射为自定义类别（map 也接受函数，按阈值分箱）
df['Alcohol'] = df['Alcohol'].map(
    lambda v: 'low' if v < 12 else ('mid' if v < 13 else 'high'))
# 再造第二个标称列，供 Step 4 独热使用（同样用 map + 函数）
df['Malic acid'] = df['Malic acid'].map(
    lambda v: 'low' if v < 2.0 else 'high')
print('Step 3 映射后:', df.shape)               # 预期 (176, 14)，两列已变文字

# Step 4: 对两个标称列独热编码（get_dummies + drop_first 防共线）
y = df['Class label'].values
X = pd.get_dummies(df.drop(columns=['Class label']),
                   columns=['Alcohol', 'Malic acid'], drop_first=True)
print('Step 4 独热后:', X.shape)                # 预期 (176, 14)

# Step 5: 分层划分 30% 测试集
X_train, X_test, y_train, y_test = train_test_split(
    X.values.astype(float), y, test_size=0.3, random_state=0, stratify=y)
print('Step 5 划分后:', X_train.shape, X_test.shape)   # 预期 (123, 14) (53, 14)
```

**分步说明与预期形状**（每一步都打印形状，这正是本实践的要求）：

| 步骤 | 操作 | 预期形状 |
| --- | --- | --- |
| 0 | 读入 wine.data（178×14：1 标签 + 13 特征） | `(178, 14)` |
| 1 | Ash 列第 0、5 行设 NaN → `dropna(axis=0)` 删 2 行 | `(176, 14)` |
| 2 | Magnesium 第 10 行设 NaN → `SimpleImputer(strategy='median')` 填补 | `(176, 14)`，NaN 计数 0 |
| 3 | Alcohol 按阈值 <12 / <13 / 其余 → low/mid/high；Malic acid 按 <2.0 → low/high（`map` 接受函数，值仍是文字、形状不变） | `(176, 14)` |
| 4 | `get_dummies(columns=['Alcohol', 'Malic acid'], drop_first=True)`：13 个特征中 2 列文字标称被展开——Alcohol 3 类留 2 列 + Malic 2 类留 1 列，加上其余 11 个数值列 | `(176, 14)` |
| 5 | `train_test_split(test_size=0.3, stratify=y)`：176×0.3=52.8 → 53 个测试 | `(123, 14)` / `(53, 14)` |

**需要观察与思考的**：

1. Step 2 填补用的中位数可以从 `imr.statistics_` 属性读出（fit 后估计器把学到的统计量存在这里），打印它并与 `df['Magnesium'].median()` 对比，应一致——这就是「fit 记住了什么」。
2. Step 4 后打印 `X.columns.tolist()`，确认被删掉的是每组字母序的第一个类别列（`Alcohol_high`、`Malic acid_high`，若 pandas 2.x 注意指示列是 bool 型，可 `.astype(int)`）。
3. 严谨性提醒：本实践为了跟书演示顺序一致，**先填补后划分**——把统计量泄漏给了测试集。严格做法是先 `train_test_split`，再只用训练集 `fit` imputer（见 4.2.5 练习 3）。请把这个差异写进你的笔记。
4. 各步的精确数值（中位数大小、独热后每列 0/1 计数、分层后每类样本数）**待本地验证**——形状上表可先行核对。

## 6. 本讲小结

- 缺失值用 `df.isnull().sum()` 逐列探查；处理只有两条路线——`dropna` 家族删除（`axis`/`how`/`thresh`/`subset` 四个口径）与填补（`SimpleImputer` 的 fit/transform 估计器风格，或 `fillna(df.mean())` 的 pandas 快捷方式）。
- `SimpleImputer` 与 `StandardScaler` 同属转换器范式：fit 学统计量、transform 应用，可复用、可进 Pipeline；防泄漏要求只在训练集上 fit。
- 类别数据先分清角色：序数特征用 `map` 加**自定义顺序**字典（逆映射用字典推导式）；类别标签用 `LabelEncoder`（`inverse_transform` 还原文字）；无序的标称特征必须独热编码——`pd.get_dummies`（`drop_first=True` 删一列防多重共线性）或 `OneHotEncoder` + `ColumnTransformer`（passthrough 保留其余列）。
- wine 数据集：178 样本、13 数值特征、3 类（59/71/48）；标签在第 0 列，`iloc[:, 1:]` 取 X、`iloc[:, 0]` 取 y；`train_test_split(test_size=0.3, random_state=0, stratify=y)` 得到 124/54 的分层划分。
- 标签已是整数时无需 LabelEncoder；标签是字符串时才需要，且预测后要会还原。

## 7. 下一步学习建议

下一讲（u4-l2《序列特征选择 SBS 与特征重要性》）继续第 4 章的后半：`ch04/ch04.py:395-403` 的 `MinMaxScaler`/`StandardScaler` 缩放回顾之后，重点精读 [ch04/ch04.py:537](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch04/ch04.py#L537) 起的手写 `SBS` 类——它用贪心的序列后向删除配合 KNN 评估「留下几个特征最合适」，再用随机森林的 `feature_importances_` 提供另一种特征排序视角。建议带着一个问题去读：13 个 wine 特征里，到底哪些真正贡献了类别区分信息？

本讲产出并验证过的 `X_train/X_test/y_train/y_test`（以及综合实践里清洗过的 DataFrame）会直接被下一讲和第 5 章（PCA/LDA 降维，见 `ch05/ch05.py`）复用，建议把综合实践脚本保存为 `my_wine_pipeline.py` 放在自己的练习目录（不要写进仓库源码目录），后续讲义在其基础上继续叠加。
