# scipy.stats 是什么：模块定位与功能地图

## 1. 本讲目标

本讲是整本 `scipy.stats` 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 `scipy.stats` 在整个 SciPy 项目以及科学计算生态中的地位与职责边界。
- 仅凭模块文档字符串，列举出 `scipy.stats` 覆盖的若干个功能大类（例如概率分布、描述性统计、假设检验、重采样、QMC 等），并能为每一类举出一个具体的函数名。
- 区分 `scipy.stats` 与 `statsmodels` / `pandas` / `scikit-learn` / `PyMC` 等相邻库各自负责什么、不负责什么。
- 看懂 `__init__.py` 是如何把分散在几十个文件里的功能「聚合」成一个统一命名空间的。

本讲不要求你立刻掌握任何算法细节，重点是建立「全局地图」。后续每一篇讲义都会落在这张地图的某个区域上。

## 2. 前置知识

在开始之前，请确认你了解以下基础概念（不熟悉的术语后面也会再解释）：

- **Python 包与模块**：一个 `.py` 文件就是一个模块，一个含 `__init__.py` 的目录就是一个包。`scipy.stats` 本质上是 SciPy 包内部的一个子包。
- **导入语法**：`import scipy.stats as stats` 之后，`stats.xxx` 就能调用里面的函数。
- **概率分布（粗略概念）**：描述随机变量取不同值的可能性，例如「正态分布」「泊松分布」。本讲只需知道有这么一类对象即可，细节留到第 3 单元。
- **统计检验（粗略概念）**：给定一批数据，判断某个假设（比如「两组数据来自同一个分布」）是否成立，通常输出一个 p 值。
- **阅读源码的能力**：本讲会引用 `__init__.py` 中的真实代码片段，建议你一边读讲义一边对照源码。

> 提示：如果你完全没接触过统计学，也不用担心。本讲是「地图课」，我们只标注每个领域在哪里、叫什么名字，具体的数学原理会在对应的后续讲义里从零讲起。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它是整个 `scipy.stats` 的「总入口」，信息量很大：

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| `__init__.py` | `scipy.stats` 子包的入口文件，既包含一段很长的模块文档字符串（功能地图），也包含把各子模块功能聚合进来的导入语句。 | 模块文档字符串（1–596 行）、导入语句（598–631 行）、`__all__` 与测试入口（640–644 行）。 |

可以这样理解 `__init__.py` 的双重身份：

- **对使用者**：它是文档。当你 `import scipy.stats` 时看到的「目录页」就写在它的文档字符串里。
- **对维护者**：它是「装配车间」。它把几十个实现文件（`_stats_py.py`、`_morestats.py`、`distributions.py`……）里的函数拉到同一个命名空间下。

> 命名约定预告：以 `_` 开头的文件（如 `_stats_py.py`）是「私有实现文件」，普通用户不需要直接导入它们；不以 `_` 开头的（如 `contingency.py`、`mstats`、`qmc`）是公开子包/子模块。这个规律会在 [u1-l2 目录结构] 详讲。

## 4. 核心概念与源码讲解

本讲围绕 `__init__.py` 的模块文档字符串，拆成四个最小模块来学：先定位（4.1），再画功能地图（4.2），再划清生态边界（4.3），最后看命名空间是怎么装配的（4.4）。

### 4.1 模块定位：scipy.stats 是什么、解决什么问题

#### 4.1.1 概念说明

`scipy.stats` 是 SciPy（发音类似「赛派」）项目中的一个子模块，专门负责**统计计算**。SciPy 本身是 Python 科学计算的「基础工具箱」，其下分了若干子模块：

- `scipy.linalg`：线性代数
- `scipy.optimize`：优化
- `scipy.integrate`：数值积分
- `scipy.stats`：统计

——而 `scipy.stats` 正是其中体量最大、子领域最多的一个。它的目标是：让你**不必自己实现**常见的统计过程（算分位数、做 t 检验、估计密度、生成低差异序列……），直接调用经过社区长期验证的函数即可。

为什么需要这样一个模块？因为统计计算既涉及大量「查表 / 积分 / 反函数 / 随机模拟」的数值技巧，又涉及对边界情况（NaN、删失数据、小样本）的细致处理。把这些统一封装，能让使用者的代码更短、更可靠。

#### 4.1.2 核心流程

从「我有数据」到「我得到结论」，`scipy.stats` 通常扮演这样的角色：

```text
原始数据  ──►  描述性统计（描述数据长什么样）
         ──►  概率分布（建模数据的生成机制）
         ──►  假设检验（判断某个假设是否成立）
         ──►  重采样/蒙特卡洛（在无法用解析公式时构造零分布）
         ──►  结论：统计量、p 值、置信区间
```

整本手册的单元顺序，基本就是顺着这条链设计的。

#### 4.1.3 源码精读

模块文档字符串开头的这一句，是 `scipy.stats` 最权威的「自我介绍」：

[__init__.py:10-13](__init__.py#L10-L13)

```python
This module contains a large number of probability distributions,
summary and frequency statistics, correlation functions and statistical
tests, masked statistics, kernel density estimation, quasi-Monte Carlo
functionality, and more.
```

翻译过来就是：「本模块包含大量的概率分布、汇总与频率统计、相关函数与统计检验、掩码数组统计、核密度估计、准蒙特卡洛功能，以及更多。」

这句话几乎就是整本手册的目录缩影。注意它用了「and more」结尾——这意味着文档字符串后面还列了很多内容，光看这句是不够的。

#### 4.1.4 代码实践

**实践目标**：亲手读到这段「自我介绍」，确认它确实存在于源码里，而不是文档网站凭空写的。

**操作步骤**：

1. 打开仓库中的 `scipy/stats/__init__.py`。
2. 找到第 10–13 行（即上面引用的那段）。
3. 把这段话里列出的名词逐一标出来。

**需要观察的现象**：你会发现这段话一口气列出了至少 6 类东西（概率分布、汇总统计、频率统计、相关/检验、掩码统计、KDE、QMC）。

**预期结果**：你能用自己的话复述「`scipy.stats` 至少包含这 6 类功能」，而不再只把它理解为「算均值的地方」。

#### 4.1.5 小练习与答案

**练习 1**：模块文档字符串开头说它「and more」，请举出至少两个「and more」里、但开头那句话没点名提到的东西。

> **参考答案**：例如「假设检验里的列联表检验 `chi2_contingency`」「灵敏度分析 `sobol_indices`」「数据变换 `boxcox`」都属于开头那句话没点名、但模块确实提供的功能。

**练习 2**：`scipy.stats` 是「子模块」还是「子包」？依据是什么？

> **参考答案**：它是一个**子包**（package）。依据是 `scipy/stats/` 目录下存在 `__init__.py` 文件，凡含 `__init__.py` 的目录在 Python 中就是包；而「模块」通常指单个 `.py` 文件。

---

### 4.2 功能地图：从模块文档字符串读取功能分类

#### 4.2.1 概念说明

模块文档字符串不仅是「自我介绍」，更是一张**带超链接的功能地图**。它用 Sphinx 的 `autosummary` 指令把每个公开函数/类列成一张表，并按主题分组。读懂这张地图，你以后找函数就不必盲搜，而是先定位到主题区，再在区内找名字。

#### 4.2.2 核心流程

读这张地图的流程是：

1. 先看一级标题（如 `Summary statistics`、`Hypothesis Tests`），它代表一个**功能大类**。
2. 再看标题下的 `autosummary` 列表，每一行就是一个**具体函数**，通常还带一句注释说明它是干什么的。
3. 嫌注释不够时，再到对应实现文件里看完整文档。

下面这张表把文档字符串里的主要大类整理出来，方便你建立全局印象（函数名为示例，并非完整清单）：

| 功能大类 | 文档字符串中的章节 | 代表函数示例 |
| --- | --- | --- |
| 概率分布（连续/多元/离散） | Probability distributions | `norm`、`gamma`、`binom`、`multivariate_normal` |
| 汇总统计（描述性统计） | Summary statistics | `describe`、`gmean`、`skew`、`kurtosis` |
| 频率统计 | Frequency statistics | `quantile`、`binned_statistic`、`percentileofscore` |
| 假设检验 | Hypothesis Tests | `ttest_ind`、`mannwhitneyu`、`shapiro`、`chi2_contingency` |
| 重采样与蒙特卡洛 | Resampling and Monte Carlo Methods | `bootstrap`、`permutation_test`、`monte_carlo_test` |
| 新一代随机变量 | Random Variables | `make_distribution`、`Normal`、`Mixture` |
| 准蒙特卡洛（子包） | Other statistical functionality → stats.qmc | `Sobol`、`Halton`、`discrepancy` |
| 数据变换 | Transformations | `boxcox`、`yeojohnson`、`zscore` |
| 拟合与生存分析 | Fitting / Survival Analysis | `fit`、`goodness_of_fit`、`ecdf`、`logrank` |
| 核密度估计 | Kernel density estimation | `gaussian_kde` |
| 灵敏度分析 | Sensitivity Analysis | `sobol_indices` |

这张表也正好对应了本手册后面大部分单元的主题，你可以把它当成「目录的目录」。

#### 4.2.3 源码精读

概率分布部分的开头说明了 SciPy 分布的基本组织方式——每个分布都是 `rv_continuous`（连续）或 `rv_discrete`（离散）的子类实例：

[__init__.py:32-44](__init__.py#L32-L44)

```python
Probability distributions
=========================

Each univariate distribution is an instance of a subclass of `rv_continuous`
(`rv_discrete` for discrete distributions):
```

这段话是第 3、4 单元（概率分布入门与分布基础设施）的「总纲」，记下它即可，本讲不展开。

再看「汇总统计」这一大类，它用一个 `autosummary` 块列出了一整排函数：

[__init__.py:239-275](__init__.py#L239-L275)

```python
Summary statistics
==================
.. autosummary::
   describe          -- Descriptive statistics
   gmean             -- Geometric mean
   hmean             -- Harmonic mean
   ...
   entropy
   differential_entropy
   median_abs_deviation
```

这正是 [u2-l1 描述性统计] 要逐个精读的函数清单。注意每个名字后面用 `--` 跟了一句极短的注释，这是 SciPy 文档的统一风格，扫一眼就能粗略知道函数用途。

「假设检验」大类则更进一步细分了若干子标题（单样本/配对、关联/相关、独立样本、重采样……）：

[__init__.py:298-308](__init__.py#L298-L308)

```python
Hypothesis Tests and related functions
======================================
SciPy has many functions for performing hypothesis tests that return a
test statistic and a p-value, and several of them return confidence intervals
and/or other related information.

The headings below are based on common uses of the functions within, but due to
the wide variety of statistical procedures, any attempt at coarse-grained
categorization will be imperfect. ...
```

这段话特别诚实：它提醒读者「这种粗分类是不完美的，同一标题下的检验并不一定能互换」。这是一个值得记住的提醒——选检验时不能只看它在哪个分类下，还要看它的分布假设。

#### 4.2.4 代码实践

**实践目标**：完成规格里要求的核心实践——从模块文档中归纳出 5 个功能大类，并各举一个函数名。

**操作步骤**：

1. 打开 `scipy/stats/__init__.py`，浏览第 32 行到第 580 行之间的各章节标题（形如 `Summary statistics`、`Hypothesis Tests` 等下面带 `====` 的行）。
2. 挑出 5 个你最感兴趣的大类。
3. 在每个大类对应的 `autosummary` 列表里，各挑一个函数名，并读它后面的 `--` 注释。

**需要观察的现象**：你会看到 `autosummary` 是按主题成块出现的，每块前面都有一个标题；不同块之间偶尔会有交叉（比如 `multiscale_graphcorr` 既可看作相关性检验也可看作重采样思想）。

**预期结果**：得到一张类似下表的小结（下面是**示例答案**，你可自行替换）：

| 你挑出的大类 | 举例函数 | 它做什么（据注释） |
| --- | --- | --- |
| Summary statistics | `describe` | 描述性统计 |
| Frequency statistics | `quantile` | 计算分位数 |
| Hypothesis Tests | `ttest_ind` | 两样本 t 检验 |
| Resampling and Monte Carlo Methods | `bootstrap` | 自助法 |
| Kernel density estimation | `gaussian_kde` | 高斯核密度估计 |

#### 4.2.5 小练习与答案

**练习 1**：在文档字符串里，「Resampling and Monte Carlo Methods」这一节一共显式列出了哪几个顶层函数？

> **参考答案**：列出的是 `monte_carlo_test`、`permutation_test`、`bootstrap`、`power` 四个（见 `__init__.py` 第 436–439 行的 `autosummary` 块）。此外该节还列出了三个可传入检验的「方法对象」：`MonteCarloMethod`、`PermutationMethod`、`BootstrapMethod`。

**练习 2**：`quantile` 和 `scoreatpercentile` 都和「分位数」有关，它们出现在哪个大类下？

> **参考答案**：出现在「Frequency statistics（频率统计）」大类下（第 276–287 行），与 `estimated_cdf`、`cumfreq`、`relfreq`、`percentileofscore` 列在一起。

---

### 4.3 边界与生态：scipy.stats 与其他统计库的分工

#### 4.3.1 概念说明

统计是一个极其庞大的领域，没有任何一个库能包打天下。`scipy.stats` 在自己的文档里**主动声明**了哪些事情「不归我管」，并指明了对应的替代库。这种「边界声明」非常重要——它能避免你在一个不合适的库里死磕，也能帮你理解 `scipy.stats` 的定位是「基础、通用、算法层面的统计原语」，而不是「端到端的统计建模框架」。

简单概括：`scipy.stats` 提供**积木**，而像 statsmodels、scikit-learn 这样的库提供**搭好的房子**。

#### 4.3.2 核心流程

当你有一项统计任务时，可以先按下面的判断流程定位该用哪个库：

```text
你的任务
  ├─ 算一个统计量 / 做一个检验 / 用一个分布  ──► scipy.stats（积木）
  ├─ 建回归方程、线性模型、时间序列          ──► statsmodels
  ├─ 处理表格数据、时间序列数据框            ──► pandas
  ├─ 分类 / 回归 / 模型选择（机器学习）       ──► scikit-learn
  ├─ 贝叶斯建模 / 概率编程                   ──► PyMC
  └─ 统计可视化                              ──► seaborn
```

当然，这些库之间会互相调用——例如 statsmodels 和 seaborn 内部都会用到 `scipy.stats`。

#### 4.3.3 源码精读

模块文档字符串在「自我介绍」之后，紧接着就给出了「范围之外」的清单：

[__init__.py:15-29](__init__.py#L15-L29)

```python
Statistics is a very large area, and there are topics that are out of scope
for SciPy and are covered by other packages. Some of the most important ones
are:

- `statsmodels`: regression, linear models, time series analysis, ...
- `Pandas`: tabular data, time series functionality, ...
- `PyMC`: Bayesian statistical modeling, probabilistic machine learning.
- `scikit-learn`: classification, regression, model selection.
- `Seaborn`: statistical data visualization.
- `rpy2`: Python to R bridge.
```

这段话翻译过来就是：统计学领域太大，有些主题不在 SciPy 的范围内，而由其他包覆盖；其中最重要的几个是 statsmodels（回归、线性模型、时间序列）、Pandas（表格数据、时间序列）、PyMC（贝叶斯统计建模、概率机器学习）、scikit-learn（分类、回归、模型选择）、Seaborn（统计可视化）、rpy2（Python 调用 R 的桥接）。

理解这条边界，能帮你回答一个常见疑问：「为什么 `scipy.stats` 里没有线性回归？」——因为有 `linregress` 做最简单的简单线性回归，但完整的多元线性回归/广义线性模型被明确划给了 statsmodels。`linregress` 本身也只是返回斜率、截距、相关系数等基本量。

#### 4.3.4 代码实践

**实践目标**：把「边界清单」与你的实际任务对上号，养成「先选对库」的习惯。

**操作步骤**：

1. 重新阅读 `__init__.py` 第 15–29 行。
2. 针对你自己手头（或假设）的一个数据分析任务，判断它应该落在 `scipy.stats` 还是上述某个外部库。
3. 写下一句话理由。

**需要观察的现象**：你会发现「同一个目标」可能横跨多个库。例如「我想看两组数据是否不同」既可以用 `scipy.stats.mannwhitneyu`，也可以在 seaborn 里画图、在 statsmodels 里建模——区别在于你想要**检验结论**还是**可视化**还是**模型**。

**预期结果**：例如——「我想对房价做多元线性回归并看系数显著性 → 这属于 statsmodels，不是 scipy.stats；但若我只想算两个变量的皮尔逊相关系数，就用 `scipy.stats.pearsonr`。」

#### 4.3.5 小练习与答案

**练习 1**：我想做贝叶斯推断（给参数一个先验，求后验），应该用文档里提到的哪个库？`scipy.stats` 行不行？

> **参考答案**：应该用 **PyMC**。`scipy.stats` 不提供概率编程/贝叶斯建模框架（它只提供频率派意义下的分布、统计量与检验），所以做完整贝叶斯推断应选 PyMC。

**练习 2**：为什么文档要专门列出 `rpy2`？

> **参考答案**：因为 R 语言在统计界有大量成熟的实现和参考结果。`rpy2` 是 Python 调用 R 的桥梁；事实上 `scipy.stats` 的测试套件就经常拿 R/mpmath 的结果作为数值参考来交叉验证（这一点会在 [u18-2 测试体系] 详讲）。

---

### 4.4 命名空间聚合机制：从导入到 `__all__`

#### 4.4.1 概念说明

前面三节都在读「文档」，这一节看点「代码」：`__init__.py` 是怎么把几十个实现文件里的函数，变成 `scipy.stats.xxx` 这种统一调用形式的？答案就在文件末尾的导入语句和一行 `__all__`。

理解这一节有两个好处：

- 以后在 `scipy.stats` 里看到一个函数名，你能反查它来自哪个实现文件。
- 你会明白 `import scipy.stats as stats; stats.test()` 这行测试代码为什么能用。

#### 4.4.2 核心流程

聚合的流程可以概括为三步：

1. **各实现文件**定义函数（例如 `_stats_py.py` 里定义了 `describe`）。
2. `__init__.py` 用 `from ._xxx import *` 或显式 `from ._xxx import name` 把它们「搬运」到本模块命名空间。
3. 一行 `__all__` 自动收集所有「不以 `_` 开头」的名字，作为模块的公开 API。

#### 4.4.3 源码精读

文件末尾的导入语句就是「搬运工」：

[__init__.py:598-631](__init__.py#L598-L631)

```python
from ._warnings_errors import (ConstantInputWarning, NearConstantInputWarning,
                               DegenerateDataWarning, FitError)
from ._stats_py import *
from ._variation import variation
from .distributions import *
from ._morestats import *
...
from ._resampling import (bootstrap, monte_carlo_test, permutation_test, power,
                          MonteCarloMethod, PermutationMethod, BootstrapMethod)
from ._entropy import *
from ._hypotests import *
...
from ._distribution_infrastructure import (
    make_distribution, Mixture, order_statistic, truncate, exp, log, abs
)
from ._new_distributions import Normal, Logistic, Uniform, Binomial
from ._mgc import multiscale_graphcorr
from ._correlation import chatterjeexi, spearmanrho, theilslopes, siegelslopes
from ._quantile import quantile, estimated_cdf
```

读这段代码要注意两种风格：

- `from ._stats_py import *`：星号导入，把该文件里所有「公开名字」一次性搬过来。像 `describe`、`gmean`、`ttest_ind` 这些常用函数都来自 `_stats_py`。
- `from ._resampling import (bootstrap, ...)`：显式逐个导入，只搬指定名字。这种方式更可控，新功能（如 `_distribution_infrastructure`、`_new_distributions`、`_quantile`）通常采用它。

接下来这行是点睛之笔，它自动生成公开 API 列表：

[__init__.py:640](__init__.py#L640)

```python
__all__ = [s for s in dir() if not s.startswith("_")]  # Remove dunders.
```

`dir()` 返回当前模块命名空间里所有名字；列表推导式过滤掉以 `_` 开头的（包括私有文件名、dunder 方法等），剩下的就是公开 API。这种写法的好处是：维护者新增一个 `from ._new_module import foo` 后，`foo` 会**自动**出现在 `__all__` 里，无需手工维护清单。

最后，模块还挂了一个测试入口：

[__init__.py:642-644](__init__.py#L642-L644)

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

这使得 `scipy.stats.test()` 可用——它会用 pytest 跑本子包的测试。`del PytestTester` 是为了不让 `PytestTester` 这个类名污染 `scipy.stats` 的公开命名空间（注意它以大写 `P` 开头但确实不是公开 API，靠手工删除来隐藏）。

#### 4.4.4 代码实践

**实践目标**：亲手验证「一个函数名来自哪个实现文件」，并体验 `__all__` 的自动聚合效果。

**操作步骤**（可在装有 SciPy 的环境运行；若本地暂未装 SciPy，则按下面的「源码阅读型」做法对照源码完成）：

1. 写一段脚本：

   ```python
   # 示例代码：演示命名空间聚合
   import scipy.stats as stats

   # 1) 看 __all__ 里有多少个公开名字
   print("公开 API 数量:", len(stats.__all__))

   # 2) 验证某个名字确实在 __all__ 里
   print("describe 在公开 API 中?", "describe" in stats.__all__)

   # 3) 反查一个函数来自哪个文件（看它的模块路径）
   print("describe 的定义模块:", stats.describe.__module__)
   print("bootstrap 的定义模块:", stats.bootstrap.__module__)
   ```

2. 对照源码：`describe` 来自 `_stats_py`，`bootstrap` 来自 `_resampling`，与上面 `__init__.py` 的导入语句一致。

**需要观察的现象**：`stats.describe.__module__` 会显示类似 `scipy.stats._stats_py` 的路径，说明它虽然以 `stats.describe` 的形式被调用，但「真身」在 `_stats_py.py` 里。

**预期结果**：

- 公开 API 数量是一个较大的数（数百量级）。
- `describe` 在公开 API 中为 `True`。
- 各函数的 `__module__` 能对应到 `__init__.py` 里某条 `from ._xxx import` 的文件。

> 待本地验证：具体的 `len(stats.__all__)` 数值会随版本变化，请以你本地安装的 SciPy 版本为准；本讲不假定具体数字。

#### 4.4.5 小练习与答案

**练习 1**：`from ._stats_py import *` 和 `from ._quantile import quantile, estimated_cdf` 这两种导入方式，哪一种更容易控制「哪些名字被公开」？为什么？

> **参考答案**：显式逐个导入（`from ._quantile import quantile, estimated_cdf`）更可控，因为它只搬指定的名字。星号导入虽然省事，但会把实现文件里所有公开名字都搬过来，容易把本意不想公开的名字也带出来。

**练习 2**：为什么 `__init__.py` 在 `from scipy._lib._testutils import PytestTester` 之后要写一句 `del PytestTester`？

> **参考答案**：因为 `PytestTester` 这个类名本身不是 `scipy.stats` 想暴露给用户的公开 API；不 `del` 的话，由于它不以 `_` 开头，会被 `__all__ = [s for s in dir() if not s.startswith("_")]` 自动收进公开列表，造成污染。`del` 之后它就不再出现在 `dir()` 里了。

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个「读图 + 溯源」小任务：

> **任务**：从 `scipy.stats` 的模块文档字符串里，挑出 **5 个功能大类**，为每个大类举 **1 个代表函数**，然后**反查**每个函数来自哪个实现文件，最后判断它是否属于 `scipy.stats` 的职责范围（而非 statsmodels/sklearn 等）。

**建议步骤**：

1. 打开 `scipy/stats/__init__.py`，浏览文档字符串中的章节标题，挑出 5 个大类（例如 Summary statistics、Hypothesis Tests、Resampling and Monte Carlo Methods、Kernel density estimation、Sensitivity Analysis）。
2. 在每个大类的 `autosummary` 列表里各选 1 个函数。
3. 跳到 `__init__.py` 末尾的导入区（第 598–631 行），找到该函数是从哪个 `from ._xxx import` 进来的，从而确定其实现文件。
4. 写一句话说明该函数属于 `scipy.stats` 的哪一类职责，并确认它没有更适合交给 statsmodels / scikit-learn / pandas 的理由。

**示例产出（你的答案可以不同）**：

| 大类 | 函数 | 实现文件（据导入区） | 职责说明 |
| --- | --- | --- | --- |
| Summary statistics | `describe` | `_stats_py` | 一次性返回均值/方差/偏度/峰度等摘要，属基础描述统计 |
| Frequency statistics | `quantile` | `_quantile` | 计算经验分位数，属基础频率统计 |
| Hypothesis Tests | `mannwhitneyu` | `_mannwhitneyu` | 两样本秩和检验，属非参数检验 |
| Resampling and Monte Carlo Methods | `bootstrap` | `_resampling` | 自助法置信区间，属重采样 |
| Sensitivity Analysis | `sobol_indices` | `_sensitivity_analysis` | 全局方差灵敏度指数 |

> 提示：以上「实现文件」一列均可通过核对 `__init__.py` 第 598–631 行的导入语句、或在运行环境中查看 `func.__module__` 来验证。这是一个**源码阅读型综合实践**，不强求运行，但鼓励你在装好 SciPy 的环境里用 `func.__module__` 复核一遍。

## 6. 本讲小结

- `scipy.stats` 是 SciPy 中负责**统计计算**的子包，是整个项目里体量最大、子领域最多的模块。
- 它的「自我介绍」写在 `__init__.py` 第 10–13 行，一口气列出了概率分布、汇总/频率统计、相关与检验、掩码统计、KDE、QMC 等功能。
- 模块文档字符串本身就是一张**功能地图**，用 `autosummary` 按主题分组列出了几乎所有公开函数；找函数应先定位主题区，再在区内找名字。
- 它**主动声明了边界**：回归/线性模型/时间序列归 statsmodels，表格数据归 pandas，贝叶斯建模归 PyMC，机器学习归 scikit-learn，可视化归 seaborn。
- `__init__.py` 通过大量 `from ._xxx import` 把实现文件里的功能「搬运」进统一命名空间，再用一行 `__all__ = [s for s in dir() if not s.startswith("_")]` 自动生成公开 API。
- `scipy.stats.test()` 之所以可用，是因为文件末尾挂载了 `PytestTester`。

## 7. 下一步学习建议

本讲建立的是「全局地图」。接下来建议按手册的单元顺序往下走：

- **紧接着看 [u1-l2 目录结构]**：弄清 `scipy/stats/` 目录下哪些是纯 Python 文件、哪些是 Cython/C 扩展、哪些是子包，为后续阅读源码打基础。
- **再看 [u1-l3 导入与运行]**：亲手 `import scipy.stats`，调用第一个统计函数，并尝试跑测试。
- **进入第 2 单元（描述性统计）**：从 `describe`、`gmean` 等最简单的函数开始接触真实算法。
- **进入第 3 单元（概率分布入门）**：本讲提到的 `rv_continuous` / `rv_discrete` 会在那里从零讲起。

> 阅读建议：在进入后续讲义前，建议你先把本讲的「功能地图」表（4.2.2 节）记个大概——它会反复出现在后续每一篇讲义的「本讲源码地图」里，作为定位锚点。
