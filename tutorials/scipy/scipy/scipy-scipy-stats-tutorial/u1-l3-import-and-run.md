# 如何导入与运行：从安装到调用第一个统计函数

## 1. 本讲目标

上一讲（u1-l2）我们看清了 `scipy/stats/` 目录的文件分层：聚合壳、子领域实现、编译扩展、子包。本讲往前走一步，回答三个「上手」问题：

1. **导入**：`from scipy import stats` 之后，那些散落在几十个 `_xxx.py` 里的函数，是怎么「聚」成一个统一命名空间的？
2. **调用**：第一次真正调用一个统计函数（描述性统计 `describe`）和一个分布方法（`norm.pdf`）时，返回的是什么、怎么读懂它？
3. **测试**：模块自带的测试套件入口 `stats.test()` 在哪里、怎么只跑一个子集？

学完本讲，你应当能够：
- 说清 `__all__` 是如何用一行列表推导自动生成的，以及它和「属性访问」的差别；
- 追踪一个名字（例如 `norm`）从底层定义文件一路被导入到 `scipy.stats` 命名空间的完整链路；
- 写出并运行一段调用 `describe` 与 `norm.pdf` 的脚本，读懂它们的返回对象；
- 用 `stats.test()` 跑通模块测试，并能只挑选一部分来跑。

## 2. 前置知识

阅读本讲前，建议你已经具备：

- **Python 导入基础**：知道 `import x`、`from x import y`、`from x import *` 的区别，以及 `__all__` 对 `import *` 的影响。
- **NumPy 数组基础**：能创建 `np.array` / `np.arange`，理解「按轴（axis）计算」的直觉即可，不必精通。
- **本手册 u1-l1、u1-l2**：u1-l1 给出了 scipy.stats 的功能地图与边界声明；u1-l2 讲清了目录里「聚合壳」与「实现文件」的分工。本讲直接承接 u1-l2 的「`__init__.py` 是纯路由层」这一结论，把它从「结论」展开成「可追踪的链路」。

一个关键直觉先放在前面：scipy.stats 里的函数**不是写在一个大文件里**，而是按子领域分散在 `_stats_py.py`、`_continuous_distns.py`、`_morestats.py` 等文件中。`__init__.py` 的全部职责，就是把这些分散的名字**搬运**到一个统一的 `scipy.stats` 命名空间里。本讲要解决的就是「搬运的机制」和「搬运完之后怎么用」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py) | 子包入口，纯路由/聚合层 | `from ._xxx import *` 聚合区、`__all__` 生成、`PytestTester` 接线 |
| [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py) | 分布的二级聚合壳 | `norm` 等分布名如何被汇总 |
| [`_continuous_distns.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py) | 连续分布的真正实现 | `norm_gen` 类与 `norm = norm_gen(name='norm')` |
| [`_stats_py.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py) | 大多数描述性/检验统计函数 | `describe` 与 `DescribeResult` |
| [`_common.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_common.py) | 极小的共享工具 | `ConfidenceInterval` 命名元组 |

> 说明：`PytestTester` 的实现不在 `stats/` 目录内，而在 SciPy 公共库 `scipy/_lib/_testutils.py` 中（本讲解环境无法读取该文件，下文只依据 `__init__.py` 中可验证的接线代码来讲解，对 `PytestTester` 自身的 API 描述会标注其来源）。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：
- **4.1 命名空间聚合机制**：`from ._xxx import *` 与 `__all__` 自动生成；
- **4.2 调用第一个统计函数**：`describe` 与 `norm.pdf`，以及它们的返回对象；
- **4.3 运行模块测试**：`PytestTester` 入口与 `stats.test()`。

### 4.1 命名空间聚合：从分散文件到统一的 `scipy.stats`

#### 4.1.1 概念说明

「聚合」（aggregation）是 scipy.stats 命名空间的核心设计。它解决一个问题：**实现要按子领域分文件以便维护，但使用时要有一个统一入口**。

Python 提供的机制是：在子包的 `__init__.py` 里写一系列 `from ._xxx import *`，把各实现文件里的公开名字「倾倒」进当前模块的命名空间。而 `import *` 倾倒哪些名字，由被导入模块的 `__all__`（如果定义了）或「所有不以 `_` 开头的名字」决定。

scipy.stats 的做法更聪明：它**不手工维护** `__all__` 列表，而是在所有 `import` 完成后，用一行列表推导**自动收集**当前命名空间里所有不以 `_` 开头的名字作为公开 API。这意味着：新增一个函数，只要它被某个 `from ._xxx import` 带进来、且名字不以 `_` 开头，就会**自动**出现在公开 API 里——无需到处登记。

#### 4.1.2 核心流程

聚合的执行顺序（对应 `__init__.py` 文件的真实顺序）：

1. 执行模块顶部那段长长的文档字符串（即 u1-l1 讲过的「功能地图」），它本身不产生名字。
2. 依次执行各条 `from ._xxx import *` / `from ._xxx import (a, b, c)`，把实现文件里的公开名字注入当前命名空间。
3. 执行 `__all__ = [s for s in dir() if not s.startswith("_")]`，对当前命名空间做一次「快照」，凡是名字不以 `_` 开头的都算公开 API。
4. **之后**才导入并接线测试工具 `PytestTester`（见 4.3），并且这条导入发生在 `__all__` 生成**之后**——这个顺序是刻意的。

#### 4.1.3 源码精读

聚合区——把各实现文件的公开名字搬运进来：

[__init__.py:598-631](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L598-L631) —— 这一整段就是「搬运工」。关键几行（节选）：

```python
from ._stats_py import *            # 描述性统计、t 检验、相关等大量函数
from ._variation import variation
from .distributions import *        # 所有连续/离散分布（含 norm、binom）
from ._morestats import *
# ... 中间还有许多 from ._xxx import ...
from ._distribution_infrastructure import (
    make_distribution, Mixture, order_statistic, truncate, exp, log, abs
)
from ._new_distributions import Normal, Logistic, Uniform, Binomial
```

注意两种写法并存：
- `from ._stats_py import *`：把对方文件 `__all__`（或所有非 `_` 名字）整体倾倒进来；
- `from ._fit import fit, goodness_of_fit`：显式列举，只搬指定名字。显式列举常用于「想精确控制暴露面」或「避免被 `*` 污染」的场合。

`__all__` 的自动生成——一行列表推导：

[__init__.py:640](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L640)

```python
__all__ = [s for s in dir() if not s.startswith("_")]  # Remove dunders.
```

- `dir()` 在模块作用域里返回**当前已绑定的所有名字**（即前面所有 `import` 注入的结果）；
- `not s.startswith("_")` 过滤掉双下划线（dunder，如 `__name__`、`__file__`）和以下划线开头的私有名（如 `_stats_py` 模块对象本身）；
- 得到的列表就是 `from scipy.stats import *` 时会被导出的公开 API。

> **关键细节**：这一行在 [第 640 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L640) 执行，而 `PytestTester` 的导入在 [第 642 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L642)。也就是说 `__all__` 快照**先于**测试工具的导入被冻结。这解释了为什么 `stats.test` 能用、却不出现在 `__all__` 里（见 4.3）。

**追踪一个名字：`norm` 是怎么来到 `scipy.stats` 的？**

理解聚合最好的办法是追一条具体链路。`stats.norm` 跨越了三层文件：

1. **定义层**：`norm_gen` 类与实例在 `_continuous_distns.py` 里。

   [_continuous_distns.py:506](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L506) —— `norm = norm_gen(name='norm')`，把类实例化、绑定到名字 `norm`。

2. **二级聚合层**：`distributions.py` 把所有分布汇总。

   [distributions.py:13-24](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L13-L24) —— `from ._continuous_distns import *` 把 `norm` 带进来，再用 `__all__ += _continuous_distns._distn_names` 显式登记分布名清单：

   ```python
   from ._continuous_distns import *  # noqa: F403
   # ...
   # Add only the distribution names, not the *_gen names.
   __all__ += _continuous_distns._distn_names
   ```
   
   这里有个**过滤**：注释明说「只加分布名，不加 `*_gen` 类名」。所以 `norm` 会进入 `distributions.__all__`，而 `norm_gen` 不会——这正是「只想暴露实例、不想暴露构造类」的精细控制。

3. **顶层聚合层**：`__init__.py` 把 `distributions` 的内容再搬一次。

   [__init__.py:602](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L602) —— `from .distributions import *`，`norm` 最终进入 `scipy.stats` 命名空间，并被第 640 行的 `__all__` 收录。

于是 `from scipy import stats; stats.norm` 才成立。这条 **`_continuous_distns.py` → `distributions.py` → `__init__.py`** 的三级链路，正是 u1-l2 所说「聚合壳」的具体形态：每一层都只搬运、不实现算法。

#### 4.1.4 代码实践

**实践目标**：亲手「看见」聚合机制，验证 `__all__` 是自动生成且包含 `norm`、`describe`，并追踪 `norm` 的来源文件。

**操作步骤**（在已安装 scipy 的 Python 环境中运行）：

```python
# 文件名：probe_namespace.py（示例代码，非项目原有文件）
import scipy.stats as stats

# 1) __all__ 是否包含 norm 和 describe？
print("norm in __all__:", "norm" in stats.__all__)
print("describe in __all__:", "describe" in stats.__all__)

# 2) test 在 __all__ 里吗？（注意 u1-l1/u1-l2 没细讲这点）
print("test in __all__:", "test" in stats.__all__)
print("hasattr(stats, 'test'):", hasattr(stats, "test"))

# 3) 追踪 norm 的来源文件
print("norm's home module:", stats.norm.__class__.__module__)
```

**需要观察的现象**：
- 第 1 组应输出两个 `True`：`norm`、`describe` 都在公开 API 中。
- 第 2 组应输出 `test in __all__: False` 但 `hasattr(stats, 'test'): True`——印证「`__all__` 控制的是 `import *`，而非属性访问」。
- 第 3 组应指向 `_continuous_distns` 模块，印证 4.1.3 追踪的链路终点。

**预期结果**：`norm` 的类定义模块为 `scipy.stats._continuous_distns`。其余输出如上。若你的环境中 `test in __all__` 为 `True`，说明版本与本文分析不符，请以本地 `help(stats)` 为准（**待本地验证**：不同 scipy 版本的 `__init__.py` 顺序可能微调）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__init__.py` 不需要手工把 `describe`、`norm` 等几百个名字写进 `__all__`？
**答案**：因为 `__all__ = [s for s in dir() if not s.startswith("_")]` 会在所有 `import` 执行完后，自动收集命名空间里所有非下划线开头的名字。只要某个名字被某条 `from ._xxx import` 带进来，就会被自动收录。

**练习 2**：`from .distributions import *` 里有个注释「Add only the distribution names, not the `*_gen` names」。如果它也把 `norm_gen` 暴露出来，会对使用者造成什么困扰？
**答案**：使用者会同时看到实例 `norm` 和构造类 `norm_gen`，容易混淆「到底该用哪个」。scipy.stats 的约定是只暴露实例（已配置好名字的「现成分布」），把 `_gen` 类留作「想自定义分布时才继承」的内部构造工具（详见后续 u3/u4）。

---

### 4.2 调用你的第一个统计函数：`describe` 与 `norm.pdf`

#### 4.2.1 概念说明

聚合的目的是「能用」。本节用两个最典型的入口，建立「scipy.stats 函数长什么样、返回什么」的直觉：

- **`describe`**（描述性统计）：输入一维或二维数组，一次性返回「观测数、最值、均值、方差、偏度、峰度」六项摘要。它代表「**返回结构化结果对象**」的一类函数。
- **`norm.pdf`**（正态分布的概率密度）：输入一个数 `x`，返回标准正态分布在 `x` 处的密度值。它代表「**分布方法**」这一类调用。

两者合起来正好覆盖 scipy.stats 的两大调用风格：摘要统计函数、分布对象方法。

#### 4.2.2 核心流程

**`describe` 的执行要点**：
1. 接收数组 `a`、轴向 `axis`、自由度修正 `ddof`、偏度修正 `bias`、缺省值策略 `nan_policy`；
2. 沿 `axis` 计算观测数、最小/最大值、均值、方差、偏度、峰度；
3. 把这六项打包成一个**命名元组** `DescribeResult` 返回——便于既当元组解包、又当对象按属性访问。

**`norm.pdf(x)` 的执行要点**：
1. `norm` 是 `norm_gen` 的一个实例，`pdf` 是它继承自基类 `rv_continuous` 的方法（基类机制详见 u3/u4，本讲先用、不深究）；
2. 对标准正态（默认 `loc=0, scale=1`），密度函数为：

\[ f(x) = \frac{1}{\sqrt{2\pi}} \exp\!\left(-\frac{x^2}{2}\right) \]

3. 底层落到一个常量 `_norm_pdf_C = \sqrt{2\pi}` 的除法，保证数值与速度。

#### 4.2.3 源码精读

**`describe` 的签名与返回约定**：

[_stats_py.py:1446-1447](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1446-L1447) —— 函数定义（其上的 `@xp_capabilities` 装饰器与数组后端能力声明有关，本讲先忽略）：

```python
@xp_capabilities(marray=True)
def describe(a, axis=0, ddof=1, bias=True, nan_policy='propagate'):
```

返回类型 `DescribeResult` 是一个**命名元组**，定义在函数正上方：

[_stats_py.py:1441-1443](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1441-L1443)

```python
DescribeResult = namedtuple('DescribeResult',
                            ('nobs', 'minmax', 'mean', 'variance', 'skewness',
                             'kurtosis'))
```

这是 scipy.stats 里非常普遍的模式：**用命名元组把多个返回值组织成「既能 `.mean` 取属性、又能解包」的对象**。`describe` 不返回置信区间，所以它的结果对象很简单；下一类检验函数会返回更丰富的「结果类」，那时就会用到 `_common.py`（见本节末尾）。

函数自带的可运行示例（即「文档即测试」的一部分）：

[_stats_py.py:1503-1509](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1503-L1509) —— 这段就是你能直接照抄的调用：

```python
>>> from scipy import stats
>>> a = np.arange(10)
>>> stats.describe(a)
DescribeResult(nobs=10, minmax=(0, 9), mean=4.5,
               variance=9.166666666666666, skewness=0.0,
               kurtosis=-1.2242424242424244)
```

输出里 `skewness=0.0`、`kurtosis` 为负，是因为 `0..9` 是完全均匀对称的整数，偏度为 0、峰度比正态「更平」（负超额峰度）。

**`norm.pdf` 的落点**：

[_continuous_distns.py:394-425](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L394-L425) —— `norm_gen` 类定义了它的 `_pdf` 钩子：

```python
class norm_gen(rv_continuous):
    ...
    def _pdf(self, x):
        # norm.pdf(x) = exp(-x**2/2)/sqrt(2*pi)
        return _norm_pdf(x)
```

而 `_norm_pdf` 是模块级的一个轻量实现：

[_continuous_distns.py:362-363](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L362-L363)

```python
def _norm_pdf(x):
    return np.exp(-x**2/2.0) / _norm_pdf_C
```

其中 `_norm_pdf_C` 即 \(\sqrt{2\pi}\)。当你调用 `stats.norm.pdf(0)` 时，求值路径是 `norm.pdf → ... → norm_gen._pdf → _norm_pdf`，最终算出 \(1/\sqrt{2\pi} \approx 0.39894\)。

> 提醒：本讲只把 `norm_gen` / `rv_continuous` 当「黑盒」用，理解「分布是一个对象、`pdf` 是它的方法」即可。`_pdf` 钩子如何被基类调度、`loc/scale` 如何平移缩放，留到 u3（分布入门）和 u4（基础设施）细讲。

**附带认识 `_common.py`**：

[_common.py:1-6](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_common.py#L1-L6) —— 整个文件只有 5 行，定义了一个共享的命名元组：

```python
from collections import namedtuple

ConfidenceInterval = namedtuple("ConfidenceInterval", ["low", "high"])
ConfidenceInterval.__doc__ = "Class for confidence intervals."
```

它是 scipy.stats 里「**反向依赖**」的典型：`describe` 用不到它，但凡是会返回**置信区间**的函数（如 `_resampling.py`、`_hypotests.py`、`_binomtest.py`、`_relative_risk.py`、`_odds_ratio.py`）都 `from ._common import ConfidenceInterval` 来复用同一形状。所以 `_common.py` 虽小，却是「结果对象体系」的共享砖块——这正是它在源码地图里占一席之地的原因，也为后续 u6（假设检验）的结果类埋下伏笔。

#### 4.2.4 代码实践

**实践目标**：调用 `describe` 与 `norm.pdf`，分别用「属性访问」和「解包」两种方式读取结果，并验证 `norm.pdf(0)` 与数学公式一致。

**操作步骤**：

```python
# 文件名：first_calls.py（示例代码，非项目原有文件）
import numpy as np
from scipy import stats

# --- 描述性统计 ---
a = np.arange(10)
res = stats.describe(a)
print(res)                 # 直接打印整个 DescribeResult
print("属性访问 mean =", res.mean)          # 当对象用
nobs, minmax, mean, var, skew, kurt = res   # 当元组解包
print("解包 mean =", mean, " var =", var)

# --- 分布方法 ---
x = np.array([-1.0, 0.0, 1.0])
print("pdf at x =", stats.norm.pdf(x))
print("理论值 pdf(0) =", 1 / np.sqrt(2 * np.pi))
```

**需要观察的现象**：
- `res.mean` 应为 `4.5`，`res.variance` 应为 `9.1666...`，与 4.2.3 文档示例一致；
- 解包得到的 `mean` 与 `res.mean` 完全相同——命名元组兼顾两种用法；
- `stats.norm.pdf(0)` 应约等于 `0.39894`，且与 `1/np.sqrt(2*np.pi)` 在浮点精度内一致。

**预期结果**：`pdf at x` 输出形如 `[0.24197072 0.39894228 0.24197072]`（标准正态在 ±1 处约为 0.242、在 0 处约为 0.399，左右对称）。`理论值 pdf(0)` 与 `pdf(x)[1]` 应一致到小数点后多位。**待本地验证**：精确到多少位取决于浮点环境，但两侧相等这一关系是确定的。

#### 4.2.5 小练习与答案

**练习 1**：`stats.describe(a)` 返回的是 `DescribeResult`，它既是元组又能 `.mean`。这种「命名元组」相比返回普通 `dict` 或普通 `tuple`，分别有什么好处？
**答案**：相比普通 `tuple`，命名元组可以用 `res.mean` 这样有意义的属性名访问，不必记「第几个位置是均值」；相比 `dict`，它仍是 `tuple` 子类，**不可变、轻量、可解包**（`mean, var = ...` 风格也能用），且与位置参数解包兼容。

**练习 2**：`stats.norm.pdf(0)` 和 `stats.norm.pdf(0, loc=0, scale=1)` 结果一样吗？为什么？
**答案**：一样。`norm` 默认就是标准正态（`loc=0, scale=1`），显式传入默认值不改变结果。`loc/scale` 是平移与缩放参数，详见 u3-l2；本节只是先用默认值。

**练习 3**：`describe` 的结果对象里**没有**置信区间，但 `_common.py` 仍定义了 `ConfidenceInterval`。这说明 scipy.stats 的「结果对象」大致分几档？
**答案**：至少两档——简单摘要（如 `DescribeResult`，纯命名元组、无区间）与带推断的结果（如各类检验返回的对象，常含 `.confidence_interval()`，复用 `_common.ConfidenceInterval`）。后者是 u6 假设检验的主线。

---

### 4.3 运行模块自带测试：`PytestTester` 入口

#### 4.3.1 概念说明

SciPy 每个子模块都挂了一个 `test()` 方法，scipy.stats 也不例外：`stats.test()` 会用 pytest 跑该模块自己的测试目录（`scipy/stats/tests/`）。这是「**装好就能自检**」的设计——你不必记住测试目录在哪，只要 `stats.test()` 就行。

提供这个能力的类叫 `PytestTester`，它**不在 stats 包里实现**，而是 SciPy 公共库提供的共享工具。scipy.stats 只在 `__init__.py` 里用三行把它「接线」到本模块。

#### 4.3.2 核心流程

接线的三步（顺序很关键）：

1. 把 `PytestTester` 类导入进来；
2. 用本模块名实例化它，绑定到名字 `test`——实例化时传入 `__name__`（即 `"scipy.stats"`），`PytestTester` 据此知道去哪个模块的 `tests/` 子目录找测试；
3. 立刻 `del PytestTester`，把类名从模块命名空间删掉，避免它残留成「看似公开」的名字。

由于这三步发生在 `__all__` 生成（第 640 行）**之后**，`test` 和 `PytestTester` 都不在 `__all__` 里——但 `test` 仍是 `scipy.stats` 的一个普通属性，所以 `stats.test()` 照样能调用。这是「**`__all__` 只管 `import *`、不管属性访问**」的一个活生生例子。

#### 4.3.3 源码精读

三行接线代码：

[__init__.py:642-644](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L642-L644)

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

逐行解读：
- 第 642 行：`PytestTester` 来自 `scipy._lib._testutils`（位于 stats 包之外，本沙箱不可读）；
- 第 643 行：`__name__` 在该处等于 `"scipy.stats"`，于是这个 `test` 实例专门跑 `scipy.stats` 的测试；
- 第 644 行：`del` 清理类名，使其不残留。

> 关于 `PytestTester.test()` 的参数：其实现位于 `scipy/_lib/_testutils.py`，是 SciPy 所有子模块共享的公共约定。按 SciPy 通用文档，`test()` 通常接受 `label`（如 `'fast'` 默认跳过慢测试、`'full'` 全跑）、`verbose`、`extra_argv`（透传给 pytest 的额外参数）、`doctests`（是否顺带跑文档示例）、`coverage`、`tests`（指定测试文件名）等参数。**这些参数的精确签名请以你本地 `help(stats.test)` 或 `scipy/_lib/_testutils.py` 为准**（待本地确认：不同版本可能略有增减）。

一个特别实用的点：因为 `extra_argv` 会**透传给 pytest**，所以可以用 pytest 自身的 `-k` 过滤表达式只跑名字里含某关键词的测试，从而「跑一个子集」而不必等全套跑完。

#### 4.3.4 代码实践

**实践目标**：确认 `stats.test` 可调用但不在 `__all__`，并只跑一个很小的子集（避免等待整个测试套件）。

**操作步骤**：

```python
# 示例代码，非项目原有文件
import scipy.stats as stats

# 1) test 是属性，但不在 __all__
print("callable(stats.test):", callable(stats.test))
print("'test' in stats.__all__:", "test" in stats.__all__)

# 2) 只跑名字里含 'describe' 的测试，最多几秒
#    （在命令行里执行更直观，也可在脚本里调用）
# stats.test(extra_argv=["-k", "describe", "-q"])
```

更推荐在**命令行**直接驱动（脚本里跑 pytest 会输出大量信息）：

```bash
# 只跑 describe 相关测试（用 -k 过滤）
python -c "import scipy.stats as s; s.test(extra_argv=['-k','describe','-q'])"

# 想跑某个具体测试文件（tests 参数指定文件名）
python -c "import scipy.stats as s; s.test(tests='test_describe.py' if False else None)"
```

> 上面的命令里 `extra_argv=['-k','describe','-q']` 是「跑子集」的关键：`-k describe` 让 pytest 只匹配名字含 `describe` 的用例，`-q` 减少输出。

**需要观察的现象**：
- `callable(stats.test)` 为 `True`，`'test' in stats.__all__` 为 `False`——印证 4.3.2 的顺序结论；
- `-k describe` 应只收集到少量测试用例（围绕 `describe` 的若干测试），并在数秒内给出 `passed`/`failed` 计数，而不是把 `scipy/stats/tests/` 下成百上千个用例全跑一遍。

**预期结果**：能很快看到 pytest 的收集与通过计数。若 `-k describe` 匹配数为 0，说明该版本中相关测试命名不含关键词，可改用具体测试文件名或放宽关键词（**待本地验证**：测试函数命名随版本可能调整）。

#### 4.3.5 小练习与答案

**练习 1**：`stats.test` 不在 `__all__` 里，但 `stats.test()` 仍能调用。请用一句话解释这个「矛盾」。
**答案**：`__all__` 只决定 `from scipy.stats import *` 时导出哪些名字，不影响通过 `stats.test` 这样的属性访问；`test` 是模块的真实属性，所以始终可调用。

**练习 2**：为什么 `__init__.py` 在 `__all__` 生成**之后**才导入 `PytestTester`？
**答案**：这样 `PytestTester` 和随后绑定的 `test` 都不会进入 `__all__` 快照，保持公开 API 清爽；同时第 644 行的 `del PytestTester` 进一步防止类名残留。三者共同把测试入口定位成「可用但不属于公开 API 导出面」的工具。

**练习 3**：你想确认本机 scipy.stats 的 `describe` 实现没被改动，最快的方式是？
**答案**：用 `stats.test(extra_argv=['-k','describe','-q'])` 只跑相关用例，几秒内即可得到通过/失败结论，比跑全套测试快得多。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：「**探针脚本**」——一次性验证聚合、调用、测试三件事。

**任务**：写一个脚本 `stats_probe.py`，完成以下三步并打印结论：

1. **聚合检查**：打印 `len(stats.__all__)`，并断言 `"norm"` 与 `"describe"` 都在 `__all__` 中、而 `"test"` 不在。
2. **首次调用**：用 `np.linspace` 生成一组数据，调用 `stats.describe` 打印结果；再对一组 `x` 调用 `stats.norm.pdf`，断言 `pdf(0)` 与 `1/np.sqrt(2*np.pi)` 数值接近（用 `np.isclose`）。
3. **测试入口**：打印 `callable(stats.test)` 与 `"test" in stats.__all__`，并在注释里写出「如何只跑 describe 相关测试」的命令。

参考框架（示例代码，非项目原有文件）：

```python
import numpy as np
from scipy import stats

# 1) 聚合检查
print("公开 API 数量:", len(stats.__all__))
assert "norm" in stats.__all__ and "describe" in stats.__all__
assert "test" not in stats.__all__

# 2) 首次调用
data = np.linspace(0, 10, 11)         # [0,1,...,10]
print("describe:", stats.describe(data))
x = np.array([-2, -1, 0, 1, 2])
pdf = stats.norm.pdf(x)
print("pdf:", pdf)
assert np.isclose(stats.norm.pdf(0), 1 / np.sqrt(2 * np.pi))

# 3) 测试入口
print("callable(stats.test):", callable(stats.test))
print("'test' in __all__:", "test" in stats.__all__)
# 只跑 describe 相关测试：
#   python -c "import scipy.stats as s; s.test(extra_argv=['-k','describe','-q'])"
print("OK")
```

**验收标准**：脚本无断言错误地打印 `OK`；`describe` 输出中 `mean=5.0`（0..10 均匀分布均值）、`skewness=0.0`（对称）；`pdf` 左右对称且 `pdf(0)` 最大。若第 3 步你想真跑测试，取消注释那条命令行用法即可（**待本地验证**：测试耗时取决于机器）。

---

## 6. 本讲小结

- scipy.stats 用「`from ._xxx import *` 聚合 + 一行 `__all__` 列表推导」实现**自动化的命名空间聚合**，新增公开函数无需手工登记（[4.1.3](#__init__py-598-631)）。
- 一个名字（如 `norm`）要经过 `_continuous_distns.py → distributions.py → __init__.py` **三级搬运**才到达 `scipy.stats`；每一级聚合壳都只搬运、不实现算法。
- `__all__` 控制 `import *` 的导出面，**不**影响 `stats.test` 这类属性访问；并且 `__all__` 在 `PytestTester` 导入之前就已被冻结，这是刻意安排的顺序。
- `describe` 返回**命名元组** `DescribeResult`，兼顾属性访问与解包；`norm.pdf` 落到 `_norm_pdf` 与常量 \(\sqrt{2\pi}\)，标准正态在 0 处密度为 \(1/\sqrt{2\pi}\)。
- `_common.py` 虽只有 5 行，却用 `ConfidenceInterval` 命名元组统一了所有「带置信区间」结果对象的形状，是结果对象体系的共享砖块。
- 模块测试入口 `stats.test()` 由三行 `PytestTester` 接线提供；用 `extra_argv=['-k','关键词','-q']` 可以只跑测试子集。

## 7. 下一步学习建议

本讲你已经能「导入、调用、跑测试」了。接下来建议：

- **学描述性统计的全貌**：进入 u2-l1《描述性统计：describe 与各种平均》，把 `gmean`/`hmean`/`pmean` 与 `axis`、权重参数吃透，它们都在 `_stats_py.py` 里，紧挨着本讲的 `describe`。
- **学分布的用法**：进入 u3-l1《scipy.stats 分布体系与 rv_continuous/rv_discrete 基类》，弄清本讲当作「黑盒」的 `norm.pdf` 背后那套 `rv_continuous` 基类与 `pdf/cdf/ppf/rvs` 公共方法。
- **想深挖聚合与基础设施**：可先读 [`__init__.py` 的导入区](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L598-L631) 和 [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py)，对照本讲的 `norm` 链路，自己再追一条（例如 `binom` 是怎么从 `_discrete_distns.py` 走到 `scipy.stats` 的）。
