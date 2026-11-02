# u1-l3 Notebook 与脚本双轨工作流

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚仓库里 `.ipynb` 与 `.py` 两种文件的关系：**同一份代码的两种形式**，notebook 是「源」，脚本是「生成物」。
2. 会打开并运行 Jupyter notebook，逐单元格（cell）执行 ch02 开头的鸢尾花数据加载与可视化代码。
3. 掌握 `pd.read_csv` 读入无表头 CSV、`df.head()/df.tail()` 快速查看数据、`df.iloc` 抽取行列的用法。
4. 会用 `matplotlib.pyplot.scatter` 画出一类样本的散点图，并加上坐标轴标签与图例。
5. 理解 `.convert_notebook_to_script.py` 和 `update_python_from_notebook.sh` 这两个维护脚本如何保证双轨同步。

本讲是全书第一次「真正跑代码」：虽然只是读一个 CSV、画一张散点图，但这条 `读取数据 → 查看数据 → 可视化` 的流水线是后面 18 章所有实验的起点。

## 2. 前置知识

- **Jupyter notebook（.ipynb）**：一种把代码拆成一个个「单元格」交互执行的文档。单元格分两种：`markdown` 单元格放文字说明，`code` 单元格放可执行的 Python 代码。选中单元格按 `Shift + Enter` 即可运行并跳到下一个单元格，运行结果（包括图表）直接显示在单元格下方。它的本质是一个 JSON 文件，所以既存代码也存「上次运行的输出」。
- **魔法命令（magic command）**：以 `%` 开头、只在 notebook（IPython）里有效的特殊命令，例如 `%matplotlib inline` 表示让 matplotlib 图形直接内嵌显示在单元格下方。Python 脚本里不能用魔法命令——这正是双轨转换时要处理的一个细节。
- **CSV 与 DataFrame**：CSV 是「逗号分隔的纯文本表格」。`pandas` 把它读成一个 `DataFrame`（可理解为带行列索引的二维表），之后就能用 `.head()` / `.tail()` / `.iloc` 等方法查看和切片。
- **matplotlib**：Python 最基础的绘图库，本书所有可视化都用它完成。**注意**：本仓库的可视化代码只使用 matplotlib，并未使用 seaborn（大纲里提到 seaborn，但经检索 `import seaborn` 在仓库中不存在，以实际源码为准）。最常用的接口是 `matplotlib.pyplot`，习惯上简写为 `plt`。
- **鸢尾花（Iris）数据集**：经典入门数据集，150 条记录、每条 4 个特征（萼片长度/宽度、花瓣长度/宽度，单位 cm）加 1 个品种标签，共 3 个品种各 50 条。它是第 2、3 章的主要实验数据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ch02/ch02.ipynb](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.ipynb) | 第 2 章的 notebook 版本，交互轨。含 markdown 说明、全部代码及上次运行的输出；最后一个单元格演示了如何把它转换成脚本 |
| [ch02/ch02.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py) | 第 2 章的脚本版本（611 行），由 notebook 自动转换生成。讲义引用行号时以它为准 |
| [ch02/iris.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/iris.data) | 鸢尾花数据集本地副本：150 行、逗号分隔、无表头，5 个字段 |
| [.convert_notebook_to_script.py](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/.convert_notebook_to_script.py) | 仓库根目录下的转换脚本：调用 `jupyter nbconvert` 把 notebook 转成 .py 并清洗格式（ch01 目录下有一份内容完全相同的副本） |
| [update_python_from_notebook.sh](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/update_python_from_notebook.sh) | 批量脚本：遍历所有 `chXX` 目录，把每个 notebook 重新转换覆盖对应的 .py |

> 阅读提示：GitHub 对 `.ipynb` 的渲染是「按单元格」的网页，行号锚点（`#L…`）在 notebook 上不可靠；因此本讲引用 notebook 时给出整文件链接并标注单元格编号，**精确行号一律引用等价的 `.py` 文件**——这也是 u1-l1 已确立的读码约定。

## 4. 核心概念与源码讲解

### 4.1 Jupyter notebook：交互轨的正确打开方式

#### 4.1.1 概念说明

为什么同一章要提供两种文件？因为两种学习方式各有优势：

- **notebook（交互轨）**：把「文字讲解 + 代码 + 运行结果 + 图片」放在同一个文档里，可以逐段运行、随时改参数重跑，适合第一次学习和做实验。仓库里每个 notebook 都保存着作者上次运行的输出，**即使不运行也能「读」到结果**。
- **脚本（.py）**：纯代码 + 注释，适合整体执行、精读和引用行号。

#### 4.1.2 核心流程

用 notebook 学习一章的典型流程：

1. 在仓库根目录启动 `jupyter lab`（或 `jupyter notebook`），进入 `ch02/` 打开 `ch02.ipynb`。
2. 从第一个单元格开始依次 `Shift + Enter` 运行：先跑「版本检查」单元格，确认环境正常（上一讲 `python_environment_check.py` 的作用）。
3. 运行到「Reading-in the Iris data」单元格得到 DataFrame，再运行「Plotting the Iris data」单元格得到散点图。
4. 想做实验就改单元格里的参数重跑，交互式观察变化。

#### 4.1.3 源码精读

notebook 的开头几个单元格建立了全书统一的「开场白」结构：

- 单元格 3–5 是版本检查：把 `sys.path.insert(0, '..')` 把仓库根目录加入模块搜索路径，从而 `from python_environment_check import check_packages`，再用一个字典声明本章依赖的最低版本并检查。对应脚本版：[ch02/ch02.py:L21-L35](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L21-L35)——这段代码声明 numpy/matplotlib/pandas 三个包并调用 `check_packages(d)`。notebook 单元格 5 中保存的真实输出是：

  ```text
  [OK] Your Python version is 3.9.6 ...
  [OK] numpy 1.22.0
  [OK] matplotlib 3.5.1
  [OK] pandas 1.3.5
  ```

- 单元格 8 是本章目录（Overview），是一段 markdown 列表；转换后变成脚本里的 `#` 注释目录：[ch02/ch02.py:L43-L54](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L43-L54)。
- 单元格 30（画散点图）的第一行是魔法命令 `%matplotlib inline`，让图形内嵌显示。注意脚本版里**没有**这一行——转换过程把它删掉了（原因见 4.4 节）。
- notebook 的**最后一个代码单元格（单元格 64）**是一条 shell 命令，演示了双轨同步本身：

  ```bash
  ! python ../.convert_notebook_to_script.py --input ch02.ipynb --output ch02.py
  ```

  它保存的真实输出是 `[NbConvertApp] Writing 16083 bytes to ch02.py`——也就是说，作者每改完 notebook，就在末尾运行这个单元格把 `.py` 刷新一遍。

#### 4.1.4 代码实践

1. **实践目标**：跑通 notebook 的前半段，亲眼看到数据表和散点图。
2. **操作步骤**：
   - 激活上一讲创建的 `pyml-book` 环境；
   - 在仓库根目录执行 `jupyter lab`（没装可 `pip install jupyterlab`）；
   - 打开 `ch02/ch02.ipynb`，从第一个单元格依次运行到「Plotting the Iris data」（约第 30 个单元格）为止。
   - 不想本地装环境也可以：按 u1-l2 介绍的方案在 Google Colab 中上传运行 notebook（本讲标为「待本地验证」的步骤均可换到 Colab 完成）。
3. **需要观察的现象**：版本检查输出全为 `[OK]`；读数据单元格先打印 `From URL: ...`（或本地路径），`df.tail()` 显示 145–149 行；画图单元格下方出现一张彩色散点图。
4. **预期结果**：逐单元格运行不报错；如果某一步报 `ModuleNotFoundError`，回到上一讲检查环境。
5. 本实践依赖本地 Jupyter 环境，具体显示效果**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：notebook 单元格 5 检查了哪几个包？为什么每章开头都要检查一遍？
  **答案**：检查 numpy、matplotlib、pandas（ch02 只用到这三个）。因为读者可能只跑某一章，逐章检查能在章节开头就发现版本不满足的问题，而不是训练到一半才报错。
- **练习 2**：`%matplotlib inline` 能不能写进 `.py` 脚本？
  **答案**：不能。它是 IPython 魔法命令，普通 Python 解释器无法识别；所以转换脚本会把它（转换后以 `get_ipython()` 开头的等价行）从生成物里删掉，`ch02.py` 中检索不到任何 `get_ipython` 残留。

### 4.2 pandas read_csv 与 head/tail：读入鸢尾花数据

#### 4.2.1 概念说明

机器学习实验的第一步永远是「把数据变成 DataFrame」。这一步有两个易错点在本数据集上同时出现：

1. **无表头**：`iris.data` 第一行就是数据（`5.1,3.5,1.4,0.2,Iris-setosa`），没有列名，必须传 `header=None`，否则 pandas 会把第一行数据吃掉当列名。
2. **数据来源容错**：书中代码优先从 UCI 的 URL 在线读取，失败时回退到本地文件 `iris.data`，让笔记本离线也能跑。

先认识数据文件本身。[ch02/iris.data](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/iris.data) 共 150 行记录（文件末尾还有一个空行，`read_csv` 默认跳过空行），每行 5 个逗号分隔的字段：萼片长度、萼片宽度、花瓣长度、花瓣宽度、品种名。**行顺序固定**：第 1–50 行 `Iris-setosa`，第 51–100 行 `Iris-versicolor`，第 101–150 行 `Iris-virginica`。这个顺序很重要——后面代码用 `iloc[0:100]` 直接按位置取前两个品种，正依赖于数据按品种排好序。

#### 4.2.2 核心流程

```text
尝试 pd.read_csv(UCI 的 URL, header=None)
        │
        ├─ 成功 → df 就绪，打印 "From URL: ..."
        └─ 抛异常 → except 分支改读本地 'iris.data'
        │
df.tail()   ← 在 notebook 里单元格最后一行表达式自动显示
```

#### 4.2.3 源码精读

读数据的代码在 [ch02/ch02.py:L178-L192](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L178-L192)：

```python
try:
    s = 'https://archive.ics.uci.edu/ml/machine-learning-databases/iris/iris.data'
    print('From URL:', s)
    df = pd.read_csv(s, header=None, encoding='utf-8')
except HTTPError:
    s = 'iris.data'
    print('From local Iris path:', s)
    df = pd.read_csv(s, header=None, encoding='utf-8')

df.tail()
```

这段代码做了三件事：优先在线读 UCI 数据、失败回退本地、最后 `df.tail()` 查看末尾 5 行。notebook 单元格 27 保存的真实输出（尾 5 行全是 `Iris-virginica`，印证了数据的排序规律）：

```text
       0    1    2    3               4
145  6.7  3.0  5.2  2.3  Iris-virginica
146  6.3  2.5  5.0  1.9  Iris-virginica
147  6.5  3.0  5.2  2.0  Iris-virginica
148  6.2  3.4  5.4  2.3  Iris-virginica
149  5.9  3.0  5.1  1.8  Iris-virginica
```

三个值得注意的细节：

- 列名是整数 `0–4`：`header=None` 时 pandas 自动分配数字列名。
- **一个真实的源码隐患**：当前文件顶部的导入列表（[ch02/ch02.py:L4-L10](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L4-L10)）里**并没有** `from urllib.error import HTTPError`，全文件检索也只在 `except` 处出现这一次。这意味着：在线读取一旦失败，Python 在匹配 `except HTTPError:` 时会先抛 `NameError: name 'HTTPError' is not defined`，回退逻辑实际走不通。修复方法是补上那行导入，或干脆直接读本地文件。这也印证了 u1-l1 的提醒：读源码要亲自核对，书、代码之间可能有出入。
- **双轨行为差异**：在 notebook 里，单元格最后一行的 `df.tail()` 是「表达式」，结果会自动渲染成表格；而在 `.py` 脚本里这行什么也不会打印——脚本里想看内容必须写 `print(df.tail())`。这是 notebook 与脚本的一个本质区别（REPL 自动回显 vs. 静默语句）。

#### 4.2.4 代码实践

1. **实践目标**：验证 `header=None` 的必要性，并熟悉 `head/tail`。
2. **操作步骤**（示例代码，在 `ch02/` 目录下运行）：

   ```python
   # 示例代码：对比有无 header=None 的读取结果
   import pandas as pd

   df = pd.read_csv('iris.data', header=None, encoding='utf-8')
   print(df.shape)          # 期望 (150, 5)
   print(df.head())         # 前 5 行，全是 Iris-setosa
   print(df.tail())         # 后 5 行，全是 Iris-virginica

   df_wrong = pd.read_csv('iris.data', encoding='utf-8')
   print(df_wrong.shape)    # 观察：少了 1 行，第一行数据去哪了？
   print(list(df_wrong.columns))  # 列名变成了 5.1、3.5、1.4、0.2、Iris-setosa
   ```

3. **需要观察的现象**：两种读法得到的形状不同；错误读法的列名恰是第一行数据。
4. **预期结果**：`df.shape == (150, 5)`；`df_wrong.shape == (149, 5)` 且列名被污染——这就是必须写 `header=None` 的原因。前 5 行的第一列依次为 5.1、4.9、4.7、4.6、5.0（与 `iris.data` 文件内容一致）。
5. 以上数值来自对 `iris.data` 文件本身的直接核对；在你机器上的运行输出**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：`df.iloc[0:100, 4]` 和 `df.iloc[0:100, [0, 2]]` 分别取到什么？
  **答案**：前者是前 100 行第 4 列（品种名字符串），后者是前 100 行的第 0 列（萼片长度）和第 2 列（花瓣长度）组成的二维数组。`iloc` 按「行位置, 列位置」切片，单个整数得到 `Series`/一维数组，列表包起来得到二维结构。
- **练习 2**：如果 UCI 网站临时无法访问，这段代码会发生什么？
  **答案**：`read_csv` 抛出网络相关异常，Python 接着求值 `except HTTPError:`，由于 `HTTPError` 未导入，抛出 `NameError`（而不是想象中的回退到本地文件）。稳妥做法是补导入或直接读本地 `iris.data`。

### 4.3 matplotlib 快速可视化：两类样本的散点图

#### 4.3.1 概念说明

拿到数据后第一件事不是训练模型，而是**画出来看看**。散点图（scatter plot）把每个样本按两个特征画成平面上的一个点，能直观回答：「这两类样本在这两个特征上分得开吗？」——如果肉眼就能看到一条分界线，后面的感知机才学得动。

#### 4.3.2 核心流程

```text
y = 第 4 列前 100 行 → np.where(== 'Iris-setosa', 0, 1)   # 品种名 → 0/1 整数标签
X = 第 0、2 列前 100 行 → 二维数组 (100, 2)                  # 萼片长度、花瓣长度
第 1 类：scatter(X[:50],  红色圆点 o)
第 2 类：scatter(X[50:100], 蓝色方块 s)
加 xlabel / ylabel / legend → plt.show()
```

其中「品种名映射为 0/1」由 `np.where(条件, 0, 1)` 完成：条件成立取 0，否则取 1。后续章节的分类算法都需要**数值标签**，这一步是把现实数据变成算法输入的典型清洗动作。

#### 4.3.3 源码精读

画图代码在 [ch02/ch02.py:L201-L219](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L201-L219)：

```python
# select setosa and versicolor
y = df.iloc[0:100, 4].values
y = np.where(y == 'Iris-setosa', 0, 1)

# extract sepal length and petal length
X = df.iloc[0:100, [0, 2]].values

# plot data
plt.scatter(X[:50, 0], X[:50, 1], color='red', marker='o', label='Setosa')
plt.scatter(X[50:100, 0], X[50:100, 1], color='blue', marker='s', label='Versicolor')

plt.xlabel('Sepal length [cm]')
plt.ylabel('Petal length [cm]')
plt.legend(loc='upper left')
plt.show()
```

逐行要点：

- `.values` 把 pandas 的 `Series`/`DataFrame` 转成 numpy 数组，后续章节的手写算法都基于 numpy 数组运算。
- 两次 `scatter` 各画一类：`X[:50]` 是 setosa（红色圆点 `o`），`X[50:100]` 是 versicolor（蓝色方块 `s`）；`label` 参数配合 `plt.legend()` 生成图例。
- `plt.xlabel/ylabel` 写清**特征名和单位**，这是可视化规范，值得从第一天就养成。
- 被注释掉的 `plt.savefig(...)` 是作者保存高清图片的语句，需要时取消注释即可。

#### 4.3.4 代码实践

1. **实践目标**：亲手画出 setosa 与 versicolor 的散点图，直观感受「线性可分」。
2. **操作步骤**：在 notebook 里新建一个单元格（或写一个独立脚本），输入 4.3.3 的代码并运行；然后尝试把 `[0, 2]` 改成 `[1, 3]`（萼片宽度、花瓣宽度）再画一次。
3. **需要观察的现象**：默认两列的图中两簇点分得很开；换成另外两列后两簇出现重叠。
4. **预期结果**：用「萼片长度–花瓣长度」两列时，红色圆点集中在左下角（花瓣长度约 1–2 cm），蓝色方块在右上方（约 3–5 cm），中间有明显空隙——两朵「花」在平面上几乎可以一刀切开；换成宽度两列后界限模糊得多。这说明**特征选择本身影响可分性**。具体图形**待本地验证**。
5. 如果在脚本（非 notebook）中运行，图形会在 `plt.show()` 处弹窗阻塞展示；在 notebook 中则内嵌显示。

#### 4.3.5 小练习与答案

- **练习 1**：为什么把 y 转成 0/1 而不直接用字符串 `'Iris-setosa'` / `'Iris-versicolor'`？
  **答案**：下一讲的感知机要用 `y` 与数值化预测做加减运算（`update = eta * (target - predict)`），字符串无法参与算术；0/1 整数标签是最简单的二分类编码。
- **练习 2**：图例 `legend(loc='upper left')` 的作用是什么？不写会怎样？
  **答案**：按各 `scatter` 的 `label` 在指定位置（左上角）绘制图例框，说明每种颜色/形状代表哪个品种；不写则图上没有颜色含义说明，读者无法解读。
- **练习 3**：想同时保存 300 dpi 的图片文件，怎么做？
  **答案**：在 `plt.show()` 之前取消 `plt.savefig('images/02_06.png', dpi=300)` 的注释（并确保输出目录存在）；`dpi` 控制分辨率。

### 4.4 双轨同步：从 notebook 到脚本的自动转换

#### 4.4.1 概念说明

如果 notebook 和 `.py` 需要手工维护两份，迟早会不一致。仓库的做法是把 notebook 当作**单一事实来源**：作者只改 notebook，再用工具把 `.py` 重新生成。这样文档说明、代码、结果始终一体，脚本永远只是投影。

#### 4.4.2 核心流程

```text
ch02.ipynb
   │  jupyter nbconvert --to script     （转成带 # In[...] 标记的粗糙脚本）
   ▼
粗糙 .py
   │  cleanup() 清洗：
   │    1. 删掉跳过清单里的行（Image(...)、# In[、get_ipython() 等）
   │    2. 把所有 import 语句收集、去重，集中放到文件顶部
   │    3. 文件头写上 # coding: utf-8
   ▼
最终 ch02.py（顶层结构：编码声明 → import 块 → 正文）
```

批量层面，`update_python_from_notebook.sh` 对每个 `chXX` 目录执行同样操作，一章一条命令完成全仓库同步。

#### 4.4.3 源码精读

转换脚本核心是三段：

- [`.convert_notebook_to_script.py:L12-L14`](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/.convert_notebook_to_script.py#L12-L14)：`convert()` 直接子进程调用 `jupyter nbconvert --to script` 完成基础转换。
- [`.convert_notebook_to_script.py:L17-L52`](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/.convert_notebook_to_script.py#L17-L52)：`cleanup()` 是精华。第 19–24 行定义**跳过清单** `skip_lines_startwith`——以 `Image(filename=`、`# In[`、`from IPython.display import Image`、`get_ipython()` 等开头的行整行删除；第 36–44 行把散落各处的 `import` 收集去重；第 48 行重组文件为 `['# coding: utf-8\n\n\n'] + imports + clean_content`。这解释了我们在 ch02.py 里观察到的三个现象：markdown 单元格变成了 `#` 注释；`import` 全部集中在 [ch02/ch02.py:L4-L10](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L4-L10)；notebook 里的插图调用和 `%matplotlib inline`（nbconvert 会把它改写成 `get_ipython().run_line_magic(...)`，命中跳过清单）在 `.py` 中无影无踪。
- [`.convert_notebook_to_script.py:L55-L78`](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/.convert_notebook_to_script.py#L55-L78)：`argparse` 命令行入口，接收 `--input`（notebook 路径）和 `--output`（脚本路径）。

批量脚本 [update_python_from_notebook.sh:L1-L12](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/update_python_from_notebook.sh#L1-L12)：

```bash
for ch in ch[0-9][0-9] ; do
  pushd "${ch}"
  for ipynb in *.ipynb ; do
    py="${ipynb/ipynb/py}"
    if [ -e "${py}" ]; then
      python ../.convert_notebook_to_script.py --input "${ipynb}" --output "${py}"
    fi
  done
  popd
done
```

要点：`ch[0-9][0-9]` 通配所有章节目录；`${ipynb/ipynb/py}` 把扩展名换掉得到目标脚本名；**`if [ -e "${py}" ]` 保证只在 `.py` 已存在时才重新生成**——即维护既有双轨，不为新 notebook 擅自创建脚本。另据核对，仓库根目录与 `ch01/` 下各有一份内容完全相同的 `.convert_notebook_to_script.py`（`diff` 验证一致），脚本里 `../.convert_notebook_to_script.py` 用的是根目录那份。

#### 4.4.4 代码实践

1. **实践目标**：亲眼看到「notebook 一行改动 → 脚本同步变化」。
2. **操作步骤**（源码阅读型 + 可选运行）：
   - 打开 `ch02/ch02.py`，找三处转换痕迹：文件头 `# coding: utf-8`（L1）、集中式 import 块（L4–L10）、从 markdown 变来的注释目录（L43–L54）；
   - （可选，需本地装有 jupyter）在 `ch02/` 下运行 `python ../.convert_notebook_to_script.py --input ch02.ipynb --output ch02.py`，再用 `git diff ch02.py` 查看：如果 notebook 自上次转换后没改过，diff 应为空。
   - 风险提示：该命令会**覆盖** `ch02.py`。请先确认 `git status` 干净，便于用 `git checkout -- ch02.py` 还原。
3. **需要观察的现象**：转换命令输出 `Converting notebook ... / Writing ... bytes to ch02.py`；重跑后 `.py` 内容与仓库版本一致。
4. **预期结果**：验证「.py 是生成物」这一机制真实可用；同时理解为什么**不应直接手改 `.py`**——下次转换会覆盖你的修改，改动应做在 notebook 里。
5. 运行效果依赖本地 jupyter 环境，**待本地验证**。

#### 4.4.5 小练习与答案

- **练习 1**：为什么跳过清单里要有 `'# In['`？
  **答案**：`nbconvert` 会把每个单元格的位置写成 `# In[?]` 之类的标记注释；它们只对 notebook 有意义，放在几百行的脚本里只是噪音，所以整类删除。
- **练习 2**：如果你直接修改了 `ch02.py` 加了一行日志，会发生什么？
  **答案**：当下能跑，但只要有人（包括你自己）重新运行转换脚本或 `update_python_from_notebook.sh`，`.py` 会按 notebook 重新生成，你的日志被无痕覆盖。持久改动应写进 notebook 再转换。
- **练习 3**：`update_python_from_notebook.sh` 为什么用 `if [ -e "${py}" ]` 而不是无条件转换？
  **答案**：这是一个保守策略——只为「已经确立了双轨关系」的章节做同步，不会替新的 notebook 自作主张创建脚本；是否提供脚本版由维护者决定。

## 5. 综合实践

把本讲三个模块（notebook 工作流、pandas 读取、matplotlib 可视化）串成一个完整任务：**不依赖网络，只用本地 `ch02/iris.data` 完成一次「读取 → 查看 → 抽取 → 可视化」**。

**任务要求**：用 pandas 读取 `ch02/iris.data`（注意无表头、逗号分隔），打印前 5 行和后 5 行，抽取前 100 个样本的萼片长度与花瓣长度两列以及品种标签，并画出两类样本的散点图。

**参考实现（示例代码，在 `ch02/` 目录下保存为 `my_iris_look.py` 运行，或在 notebook 单元格中执行）**：

```python
# 示例代码：综合实践参考实现
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# 1. 读取：无表头 → header=None；末尾空行 pandas 自动跳过
df = pd.read_csv('iris.data', header=None, encoding='utf-8')

# 2. 查看：前 5 行与后 5 行（脚本里必须用 print 才会显示）
print(df.head())
print(df.tail())
print('形状:', df.shape)          # 期望 (150, 5)

# 3. 抽取：前 100 个样本（setosa + versicolor）
#    第 0 列 = 萼片长度，第 2 列 = 花瓣长度，第 4 列 = 品种
y = df.iloc[0:100, 4].values                # 品种名
y = np.where(y == 'Iris-setosa', 0, 1)      # 转成 0/1 数值标签
X = df.iloc[0:100, [0, 2]].values           # 形状 (100, 2)
print('X 形状:', X.shape, 'y 中 0/1 各:', (y == 0).sum(), (y == 1).sum())

# 4. 可视化：两类分别画散点
plt.scatter(X[:50, 0],  X[:50, 1],  color='red',  marker='o', label='Setosa')
plt.scatter(X[50:100, 0], X[50:100, 1], color='blue', marker='s', label='Versicolor')
plt.xlabel('Sepal length [cm]')
plt.ylabel('Petal length [cm]')
plt.legend(loc='upper left')
plt.show()
```

**操作步骤**：

1. 激活 `pyml-book` 环境，`cd` 到仓库的 `ch02/` 目录。
2. 新建脚本或 notebook 单元格，输入上述代码运行。
3. 运行成功后，再完成两个小变体：(a) 换成 `df.iloc[50:150]` 画 versicolor 与 virginica 的对比图；(b) 把两次散点的列换成第 1、3 列（两种宽度）。

**预期结果与现象**：

- `df.head()` 前 5 行全部是 `Iris-setosa`，第 0 列依次为 5.1、4.9、4.7、4.6、5.0；`df.tail()` 后 5 行全部是 `Iris-virginica`（与 4.2.3 节引用的 notebook 真实输出一致）。
- `X.shape == (100, 2)`，y 中 0 和 1 各 50 个。
- 主图：setosa（红圆点）聚在左下、versicolor（蓝方块）在右上，两簇之间有肉眼可见的空隙——**这两个类在这两个特征上近似线性可分**，这正是下一讲感知机能在这个子问题上学好的前提。
- 变体 (a)：versicolor 与 virginica 的两簇会有明显**重叠**，同一套直觉告诉你感知机会遇到困难——这是留给下一讲的伏笔。

**待本地验证**：以上运行输出与图形均需你在本地环境实际执行确认（`head/tail` 的数值已对照 `iris.data` 原文核实）。

## 6. 本讲小结

- 仓库为每章提供 `.ipynb`（交互轨：说明 + 代码 + 输出一体，适合学习实验）与 `.py`（生成物：适合精读与行号引用）两种等价形式，notebook 是**单一事实来源**，不要手改 `.py`。
- 读数据的关键参数是 `header=None`（`iris.data` 无表头，150 行 × 5 列，按 setosa/versicolor/virginica 各 50 行有序排列）；`df.head()/tail()` 用于快速体检，脚本中需 `print` 才可见。
- `iloc` + `.values` 把 DataFrame 切成 numpy 数组，`np.where` 把品种名转成 0/1 数值标签——这是「现实表格 → 算法输入」的标准清洗两步。
- `plt.scatter` 分两次画两类样本（不同颜色/形状 + `label` + `legend`），散点图直观揭示了 setosa 与 versicolor 在「萼片长度–花瓣长度」平面上近似线性可分。
- 双轨同步由 `.convert_notebook_to_script.py`（nbconvert 转换 + cleanup 清洗：删 `Image/get_ipython/# In[` 行、集中 import、加编码头）和 `update_python_from_notebook.sh`（按章批量、仅覆盖已存在的 .py）自动完成。
- 读源码要亲自核对：例如当前 `ch02.py` 未导入 `HTTPError`，`except HTTPError:` 在 URL 失败时会抛 `NameError`——教程代码同样存在瑕疵，保持怀疑与验证的习惯。

## 7. 下一步学习建议

环境已就绪、数据流水线已跑通，下一讲 **u2-l1「感知机从零实现」** 将正式进入算法：逐行精读 [ch02/ch02.py:L92-L158](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L92-L158) 的 `Perceptron` 类（`fit`/`net_input`/`predict`），你会看到本讲准备的 `X`、`y` 如何被喂进权重更新规则，并用 `plot_decision_regions`（[ch02/ch02.py:L246-L272](https://github.com/rasbt/machine-learning-book/blob/63bee951ca45420ac33ccf803ceb337afc3c0a91/ch02/ch02.py#L246-L272)）画出第一条决策边界。继续阅读建议：(1) 用 5. 节的脚本试验不同列组合，体会特征对可分性的影响；(2) 通读 `ch02.ipynb` 全部单元格，对照 `ch02.py` 找出 3 处「转换痕迹」，巩固双轨思维；(3) 若 numpy 切片不熟，可先补 `.values`、`iloc`、布尔掩码（`X[y == cl]`）这三个本讲出现的用法，下一讲会大量使用。
