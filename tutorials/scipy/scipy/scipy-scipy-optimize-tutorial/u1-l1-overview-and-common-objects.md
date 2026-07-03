# 模块定位与公共对象：OptimizeResult 与 show_options

## 1. 本讲目标

本讲是 `scipy.optimize` 学习手册的第一篇，目标是让你在还没有深入任何单个算法之前，先建立一个「全局地图」。

读完本讲你应该能够：

- 说清楚 `scipy.optimize` 这个子包到底负责解决哪几类数值问题（最小化、求根、线性规划、最小二乘、曲线拟合、指派等）。
- 理解所有求解器共享的结果对象 `OptimizeResult` 的结构，知道 `fun`、`x`、`success`、`message`、`nfev`、`nit` 等字段分别代表什么。
- 理解 `OptimizeWarning` 这个统一警告类的作用，以及它在什么场景下被触发。
- 学会用 `show_options` 在不查网页文档的情况下，直接在代码里查阅某个求解器的全部可配置选项。

本讲不要求你懂数值优化算法本身，它只建立「骨架认知」。

## 2. 前置知识

- **什么是优化（optimization）**：在一个集合里找一个让目标函数 \(f(x)\) 取值最小（或最大）的点 \(x^\*\)。例如求 \(\min_x f(x)\)。
- **什么是求根（root finding）**：找一个 \(x^\*\) 使得 \(g(x^\*) = 0\)。求根和求最小值在数值方法上是两套不同的算法。
- **Python 包与模块**：`scipy.optimize` 是 SciPy 库里的一个子包（一个目录 + 一个 `__init__.py`）。导入 `scipy.optimize` 实际上执行了它的 `__init__.py`。
- **dict 子类**：`OptimizeResult` 本质上是一个 `dict` 的子类，同时支持「字典风格」`res['fun']` 和「属性风格」`res.fun` 两种访问方式。如果你用过 `dict`，本讲的代码就不会陌生。

> 提示：本讲用到的公式只有经典的 Rosenbrock 函数
>
> \[ f(\mathbf{x}) = \sum_{i=1}^{n-1} \left[\,100\,(x_{i+1} - x_i^2)^2 + (1 - x_i)^2\,\right] \]
>
> 它在 \(\mathbf{x} = (1,1,\dots,1)\) 处取得最小值 \(0\)，是 SciPy 自带的测试函数。

## 3. 本讲源码地图

本讲只看两个核心文件，外加一个基类文件：

| 文件 | 作用 | 本讲关注的内容 |
| --- | --- | --- |
| `scipy/optimize/__init__.py` | 子包入口，决定 `import scipy.optimize` 后能看到哪些名字 | 模块顶部 docstring（能力地图）、`autosummary` 列表、`from ... import *` 导入段 |
| `scipy/optimize/_optimize.py` | 大量算法与公共对象的「老家」 | `OptimizeResult`、`OptimizeWarning`、`show_options` 三个公共对象的定义 |
| `scipy/_lib/_util.py` | SciPy 内部工具库 | `_RichResult` 基类，是 `OptimizeResult` 的父类（位于 `optimize` 目录之外，但行为关键） |

一句话概括它们的关系：`__init__.py` 负责「对外暴露」，`_optimize.py` 负责「定义公共对象」，`_util.py` 里的 `_RichResult` 负责「让结果对象既能当字典又能当属性用」。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. `scipy.optimize` 的能力地图（看 `__init__.py` 文档）。
2. 公共结果对象 `OptimizeResult`。
3. 警告对象 `OptimizeWarning`。
4. 选项查阅工具 `show_options`。

---

### 4.1 scipy.optimize 的能力地图：模块文档与 autosummary 列表

#### 4.1.1 概念说明

一个大型子包最怕「不知道它能干嘛」。`scipy.optimize` 把自己的能力清单直接写在了 `__init__.py` 的模块 docstring 里。这份 docstring 同时是 [官方文档](https://docs.scipy.org/doc/scipy/reference/optimize.html) 的来源——文档构建工具通过 `autosummary` 指令把这里列出的函数自动生成参考页面。

所以，读 `__init__.py` 的 docstring 是了解 `scipy.optimize` 能力范围最权威、最不会过时的方式。

#### 4.1.2 核心流程

`__init__.py` 的组织顺序就是一份「能力分类表」：

1. 顶部一段总述：说明 `optimize` 提供「最小化、可能带约束」的求解器，覆盖非线性（局部+全局）、线性规划、约束/非线性最小二乘、求根、曲线拟合。
2. 一个「公共对象」`autosummary` 块：列出所有求解器共用的 `show_options`、`OptimizeResult`、`OptimizeWarning`。
3. 之后按主题分组，每个主题一个 `autosummary` 块：
   - Scalar functions optimization（标量最小化：`minimize_scalar`）
   - Local (multivariate) optimization（多元局部最小化：`minimize` + 一堆 method）
   - Global optimization（`basinhopping`、`brute`、`differential_evolution`、`shgo`、`dual_annealing`、`direct`）
   - Least-squares and curve fitting（`least_squares`、`nnls`、`lsq_linear`、`isotonic_regression`、`curve_fit`）
   - Root finding（`root_scalar`、`brentq`、`root`、`newton` 等）
   - Linear programming / MILP（`milp`、`linprog`）
   - Assignment problems（`linear_sum_assignment`、`quadratic_assignment`）
   - Utilities（`approx_fprime`、`check_grad`、`line_search`、`rosen` 系列等）
   - Legacy functions（`fmin*`、`leastsq`、`fsolve` 等旧接口，不建议新代码使用）

读懂这个分类，你就能在拿到一个数值问题时，快速判断「该去找 `optimize` 里的哪个函数」。

#### 4.1.3 源码精读

模块开头的总述，定义了 `optimize` 的定位：

[scipy/optimize/__init__.py:13-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L13-L17) —— 这几行说明 `optimize` 提供「最小化（或最大化）目标函数、可能带约束」的求解器，覆盖非线性（局部+全局）、线性规划、约束与非线性最小二乘、求根、曲线拟合。这是整个子包的「一句话定位」。

紧接着的「公共对象」`autosummary` 块尤其重要，因为它列出的就是本讲要讲的三件套：

[scipy/optimize/__init__.py:19-26](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L19-L26) —— 这里用 `autosummary` 列出三个被「所有求解器共享」的公共对象：`show_options`、`OptimizeResult`、`OptimizeWarning`。注意它们被放在最前面，与具体的某个算法并列时优先级更高。

而「这些函数到底从哪里来」则由文件末尾的一大段 `from ... import *` 决定。`OptimizeResult`、`OptimizeWarning`、`show_options` 都来自 `_optimize`：

[scipy/optimize/__init__.py:422-423](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L422-L423) —— `from ._optimize import *` 把 `_optimize.py` 里 `__all__` 中列出的名字（其中包含 `OptimizeResult`、`show_options`、`OptimizeWarning`）全部导出到子包顶层。

最后，`__init__.py` 用一行推导式动态生成最终的公开名字列表：

[scipy/optimize/__init__.py:456](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L456) —— `__all__ = [s for s in dir() if not s.startswith('_')]` 把当前命名空间里所有「不以下划线开头」的名字收为公开 API。这就是为什么 `dir(scipy.optimize)` 看不到下划线开头的内部模块（如 `_optimize`）。

#### 4.1.4 代码实践

**实践目标**：用代码亲自确认 `__init__.py` 文档里声称的「公共对象」确实被导出到了顶层。

**操作步骤**：

```python
# 文件名建议：explore_init.py
import scipy.optimize as opt

# 1) 直接打印模块 docstring 的前若干行，对应 __init__.py 顶部
print(opt.__doc__[:400])

# 2) 确认三个公共对象都在顶层命名空间里
for name in ["OptimizeResult", "OptimizeWarning", "show_options"]:
    print(f"{name}: {hasattr(opt, name)}")

# 3) 顶层导出的公开函数里不含下划线开头的内部模块
public = [n for n in dir(opt) if not n.startswith('_')]
print("公开名字数:", len(public))
print("_optimize 是否可见:", hasattr(opt, '_optimize'))
```

**需要观察的现象**：第 2 步三个 `hasattr` 都应输出 `True`；第 3 步 `_optimize 是否可见` 应为 `False`（因为它被 `__all__` 过滤掉了，尽管它确实存在于子包内）。

**预期结果**：三个公共对象都存在；公开名字数在几十个量级（具体数字待本地验证）；内部模块 `_optimize` 不可见。

#### 4.1.5 小练习与答案

**练习 1**：在 `__init__.py` 的 docstring 里，「Assignment problems」主题下列出了哪两个函数？

**答案**：`linear_sum_assignment`（线性指派）和 `quadratic_assignment`（二次指派）。见 `__init__.py` 的对应 `autosummary` 块。

**练习 2**：为什么 `import scipy.optimize` 之后，`scipy.optimize._optimize` 似乎「不存在」，但里面的函数却能用？

**答案**：`_optimize` 模块其实被导入了（否则函数用不了），只是 `__init__.py` 末尾的 `__all__ = [s for s in dir() if not s.startswith('_')]` 把它排除在「公开名字」之外，`from scipy.optimize import *` 不会带走它。但你仍可通过 `scipy.optimize._optimize` 显式访问内部模块。

---

### 4.2 公共结果对象 OptimizeResult

#### 4.2.1 概念说明

`optimize` 里几十个求解器返回的结果五花八门，但它们都返回同一种对象：`OptimizeResult`。它是一个「带属性访问的字典」，把求解结果（最优解、目标值、是否成功、迭代次数……）统一打包。你只要学会读这一个对象，就能读懂几乎任何求解器的输出。

它的关键设计是：**它既是字典，又是属性**。

- 字典风格：`res['fun']`、`res.keys()`
- 属性风格：`res.fun`、`res.success`

#### 4.2.2 核心流程

`OptimizeResult` 的「行为」来自它的父类 `_RichResult`，而 `_RichResult` 又是 `dict` 的子类。所以三者关系是：

```
dict  →  _RichResult  →  OptimizeResult
```

`_RichResult` 重写了三个魔法方法，把字典改造成「属性可访问」：

| 魔法方法 | 作用 |
| --- | --- |
| `__getattr__(name)` | 访问 `res.fun` 时，实际去查字典 `self['fun']` |
| `__setattr__` | 赋值 `res.fun = v` 时，实际写 `self['fun'] = v` |
| `__repr__()` | 打印时按固定顺序排版，方便阅读 |
| `__dir__()` | 让 IDE / 交互式环境能自动补全出 `res.<Tab>` 的字段 |

注意 `OptimizeResult` 本身的类体只有一个 `pass`——它不自己定义任何方法，全部继承自 `_RichResult`。它的价值在于**承载一段详尽的字段说明 docstring**，告诉用户 `x`/`fun`/`nfev` 等字段分别是什么含义。

#### 4.2.3 源码精读

`OptimizeResult` 的定义很简短，但 docstring 极其重要——它是全子包共享的「字段字典」：

[scipy/optimize/_optimize.py:112-154](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L112-L154) —— `class OptimizeResult(_RichResult)`，类体只有 `pass`，但 docstring 逐一说明了字段：`x`（最优解）、`success`（是否成功）、`status`（终止状态码）、`message`（终止原因描述）、`fun`（`x` 处的目标值）、`jac`/`hess`/`hess_inv`（梯度/海森/逆海森，可能是近似）、`nfev`/`njev`/`nhev`（函数/雅可比/海森的求值次数）、`nit`（迭代数）、`maxcv`（最大约束违反量）。docstring 还特别提醒：「不同求解器可能并不包含全部字段，也可能有额外字段，用 `OptimizeResult.keys()` 查看实际有哪些」。

真正赋予它「属性访问」能力的是父类 `_RichResult`：

[scipy/_lib/_util.py:945-952](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/_util.py#L945-L952) —— `__getattr__` 在属性访问时去字典里取值（取不到才抛 `AttributeError`）；`__setattr__ = dict.__setitem__`、`__delattr__ = dict.__delitem__` 让赋值/删除直接落到字典上。这就是 `res.fun` 等价于 `res['fun']` 的原因。

[scipy/_lib/_util.py:954-982](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/_lib/_util.py#L954-L982) —— `__repr__` 定义了一个固定的字段打印顺序（`message, success, status, fun, ...`），并跳过 `slack`、`con` 等冗余字段。这就是为什么你 `print(res)` 时看到的输出是整齐排版、而不是普通 dict 的乱序。

那么这些字段是谁填进去的？以 BFGS 求解器为例，它在结尾构造 `OptimizeResult`：

[scipy/optimize/_optimize.py:1520-1526](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1520-L1526) —— `_minimize_bfgs` 在收敛后 `result = OptimizeResult(fun=fval, jac=gfk, hess_inv=Hk, nfev=sf.nfev, njev=sf.ngev, status=warnflag, success=(warnflag == 0), message=msg, x=xk, nit=k)`。注意 `success` 是由 `warnflag == 0` 计算出来的：警告标志为 0 才算成功。这也是后面实践里你会看到 `success=True` 的来源。

#### 4.2.4 代码实践

**实践目标**：用 `minimize` 求解 Rosenbrock 函数，观察 `OptimizeResult` 的字典与属性两种访问方式，并印证字段含义。

**操作步骤**：

```python
# 文件名建议：inspect_result.py
import numpy as np
from scipy.optimize import minimize, rosen

x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])
res = minimize(rosen, x0, method='BFGS')

# 1) 直接打印 —— 走的是 _RichResult.__repr__
print(res)

# 2) 属性风格访问
print("x      =", res.x)
print("fun    =", res.fun)
print("success=", res.success)
print("nit    =", res.nit, " nfev =", res.nfev)

# 3) 字典风格访问（与属性等价）
print("fun again (dict) =", res['fun'])

# 4) 它真的是 dict 子类，且有 keys()
print("is dict subclass:", isinstance(res, dict))
print("keys:", list(res.keys()))
```

**需要观察的现象**：
- `print(res)` 输出按固定顺序排版（先 `message`、`success`，再 `fun`、`x`、`nit` 等）。
- `res.x` 应该接近 `[1, 1, 1, 1, 1]`（Rosenbrock 的全局最小点）。
- `res.fun` 应接近 `0`。
- `res.success` 为 `True`，因为 BFGS 收敛时 `warnflag == 0`。
- 属性访问和字典访问的 `fun` 完全一致。
- `isinstance(res, dict)` 为 `True`。

**预期结果**：`res.fun` 在 ~1e-8 或更小量级；`res.x` 各分量与 1 的偏差很小。具体的 `nit`/`nfev` 数值会随 SciPy 版本与浮点环境略有不同，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OptimizeResult` 类体里只有 `pass`，却能用 `res.fun` 这种属性访问？

**答案**：因为属性访问由父类 `_RichResult.__getattr__` 提供，它在访问时去字典里取值。`OptimizeResult` 只是把 `_RichResult` 作为父类，自身不需要再写方法。

**练习 2**：如果你调用 `minimize_scalar(...)` 得到的结果里没有 `hess_inv` 字段，这正常吗？为什么？

**答案**：正常。`OptimizeResult` 的 docstring 明确说「不同求解器可能不包含全部字段」。标量求解器本来就没有海森矩阵概念，自然不会填 `hess_inv`。用 `res.keys()` 查看实际字段即可。

**练习 3**：`res.success` 是怎么被算出来的？（提示：看 4.2.3 里 BFGS 的构造代码）

**答案**：求解器在构造 `OptimizeResult` 时传入 `success=(warnflag == 0)`。即警告标志为 0（无警告）才算成功。不同求解器判断「成功」的条件可能不同，但都遵循「显式传入一个布尔值」的模式。

---

### 4.3 警告对象 OptimizeWarning

#### 4.3.1 概念说明

求解过程中有些情况「不算错误，但值得提醒用户」，例如：你传了一个求解器不认识的选项、迭代达到最大次数但没收敛、或者输入数据有些小问题。这类情况 `optimize` 不会抛异常中断程序，而是发出一个**警告（warning）**。

`OptimizeWarning` 就是 `optimize` 自己定义的统一警告类型。所有 `optimize` 内部发出的「一般性提醒」都归类到它名下，方便你用 `warnings.filterwarnings` 统一过滤。

#### 4.3.2 核心流程

`OptimizeWarning` 继承自 Python 内置的 `UserWarning`，类体同样是 `pass`——它只是一个「标签类」，目的是让 `optimize` 的警告有一个共同的可识别类型。

它最常见的使用场景是「未知选项提醒」：当你在 `options` 字典里传了一个求解器不认识的关键字，`optimize` 不会直接报错，而是发一个 `OptimizeWarning` 提示「这些选项我没识别」，然后继续用默认值跑。

```text
用户传 options={'foo': 1}
   │
   ▼
求解器把认识的选项拿走，剩下的放进 unknown_options
   │
   ▼
_check_unknown_options(unknown_options)
   │  非空 → warnings.warn(..., OptimizeWarning)
   ▼
程序继续运行（不中断）
```

#### 4.3.3 源码精读

`OptimizeWarning` 的定义极其简洁：

[scipy/optimize/_optimize.py:157-159](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L157-L159) —— `class OptimizeWarning(UserWarning)`，docstring 写「General warning for scipy.optimize」，类体只有 `pass`。它是一个标签类：本身不携带逻辑，靠「类型」被识别。

最典型的使用处是「未知选项检查」：

[scipy/optimize/_optimize.py:176-182](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L176-L182) —— `_check_unknown_options(unknown_options)`：如果 `unknown_options` 非空，就把这些键名拼成字符串，用 `warnings.warn(f"Unknown solver options: {msg}", OptimizeWarning, stacklevel=4)` 发出警告。注意 `stacklevel=4` 的注释说明：调用链是「用户代码 → 某个 SciPy 函数 → `_minimize_*` → `_check_unknown_options`」，第 4 层正好落在用户代码，让警告看起来像是用户那一行触发的。

> 补充：`OptimizeWarning` 不只用在未知选项。例如求解器未收敛时也会用它提醒（见 `_print_success_message_or_warn` 在 [`_optimize.py:1529-1533`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1529-L1533)）。

#### 4.3.4 代码实践

**实践目标**：故意传一个不存在的选项，触发 `OptimizeWarning`，并用 `warnings.catch_warnings` 捕获，确认它的类型。

**操作步骤**：

```python
# 文件名建议：trigger_warning.py
import warnings
from scipy.optimize import minimize, rosen, OptimizeWarning
import numpy as np

x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    # 'totally_fake_option' 不是 BFGS 的合法选项
    res = minimize(rosen, x0, method='BFGS',
                   options={'totally_fake_option': 123})

print("捕获到", len(caught), "条警告")
for w in caught:
    print("  类别:", w.category.__name__)
    print("  信息:", str(w.message))
    print("  是 OptimizeWarning 吗?:",
          issubclass(w.category, OptimizeWarning))
```

**需要观察的现象**：至少有一条警告，类别为 `OptimizeWarning`，信息形如 `Unknown solver options: totally_fake_option`；并且 `issubclass(w.category, OptimizeWarning)` 为 `True`。

**预期结果**：求解照常完成（`res.success` 仍可能为 `True`，因为未知选项不影响真正的算法参数），但同时产生一条 `OptimizeWarning`。警告文本里会列出你不认识的那个选项名。

#### 4.3.5 小练习与答案

**练习 1**：`OptimizeWarning` 继承自哪个类？为什么这样设计？

**答案**：继承自内置的 `UserWarning`。这样它就属于 Python 默认会显示的警告家族，用户也能用标准的 `warnings` 机制（如 `warnings.filterwarnings`）来统一控制 `optimize` 的警告。

**练习 2**：传了一个未知选项后，程序是报错退出还是继续运行？

**答案**：继续运行。`_check_unknown_options` 只是 `warnings.warn(...)`，并不 `raise`。未知选项会被忽略（用默认值），程序继续求解，但会提醒用户。

---

### 4.4 选项查阅工具 show_options

#### 4.4.1 概念说明

`minimize`、`root`、`linprog` 这类「统一调度入口」都接受一个 `options` 字典，里面有大量方法相关的可调参数（步长、容差、最大迭代数等）。这些参数太多，没法全写进顶层函数签名，而是写在每个具体方法（`_minimize_bfgs` 等）的 docstring 里。

`show_options` 就是一个「在代码里直接查阅某个方法 docstring」的工具。你不用离开终端、不用查网页，就能知道某个求解器接受哪些 `options`。

#### 4.4.2 核心流程

`show_options` 的核心是一个「求解器 → 方法 → 文档字符串来源」的映射表 `doc_routines`。流程是：

```text
show_options(solver, method, disp)
   │
   ├─ solver=None?     → 汇总打印 minimize / minimize_scalar / root / linprog 四大类
   ├─ solver 给定、method=None? → 打印该 solver 下所有方法的文档
   └─ solver 与 method 都给定? → 只打印那一个方法的文档
   │
   ▼
根据 'solver.method' 字符串在 doc_routines 里查到形如
'scipy.optimize._optimize._minimize_bfgs' 的目标
   │
   ▼
__import__(模块) + getattr(模块, 函数名) 拿到函数对象
   │
   ▼
取 obj.__doc__，textwrap.dedent 去缩进
   │
   ▼
disp=True 则 print 并返回 None；disp=False 则返回字符串
```

关键点：`show_options` 打印的内容，**就是对应方法函数的 docstring 本身**。所以它永远和源码同步、不会过时。

#### 4.4.3 源码精读

`show_options` 的签名与参数说明：

[scipy/optimize/_optimize.py:3948-3967](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3948-L3967) —— 参数 `solver` 取值为 `'minimize'`、`'minimize_scalar'`、`'root'`、`'root_scalar'`、`'linprog'`、`'quadratic_assignment'` 之一；`method` 是具体方法名（如 `'BFGS'`）；`disp` 决定是打印还是返回字符串。返回值：`disp=True` 时返回 `None`，`disp=False` 时返回文本字符串。

`doc_routines` 映射表是「方法名 → 文档来源」的真相所在：

[scipy/optimize/_optimize.py:4057-4079](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L4057-L4079) —— 例如 `'minimize'` 下，`('bfgs', 'scipy.optimize._optimize._minimize_bfgs')` 表示「`minimize` 的 `BFGS` 方法的文档来自 `_minimize_bfgs` 函数的 docstring」。其余方法同样指向各自实现函数的 docstring。这就是 `show_options('minimize', 'BFGS')` 的内容来源。

分发与取 docstring 的逻辑：

[scipy/optimize/_optimize.py:4135-4149](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L4135-L4149) —— 先把 `solver` 转小写并在 `doc_routines` 里查；若给了 `method` 也转小写，查不到就 `raise ValueError(f"Unknown method {method!r}")`。所以方法名大小写不敏感。

[scipy/optimize/_optimize.py:4152-4169](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L4152-L4169) —— 用 `name.split('.')` 把字符串拆成模块路径和函数名，`__import__` 导入模块、`getattr` 取到函数对象，再 `obj.__doc__` 取 docstring，`textwrap.dedent(doc).strip()` 去掉缩进。最后根据 `disp` 决定 `print(text)` 还是 `return text`。

#### 4.4.4 代码实践

**实践目标**：用 `show_options` 查阅 BFGS 的全部选项，并验证「`disp=False` 返回的是字符串」「方法名大小写不敏感」。

**操作步骤**：

```python
# 文件名建议：use_show_options.py
from scipy.optimize import show_options

# 1) 直接打印 BFGS 的选项文档
show_options('minimize', 'BFGS')

# 2) 拿到字符串而不是打印
text = show_options('minimize', 'BFGS', disp=False)
print("类型:", type(text))            # 应为 str
print("包含 'gtol' 吗:", 'gtol' in text)   # BFGS 用 gtol 做梯度收敛

# 3) 方法名大小写不敏感（内部 .lower()）
text2 = show_options('minimize', 'bfgs', disp=False)
print("大小写结果一致:", text == text2)

# 4) 给一个不存在的方法名，观察报错
try:
    show_options('minimize', 'not-a-method')
except ValueError as e:
    print("捕获 ValueError:", e)
```

**需要观察的现象**：
- 第 1 步会打印出 BFGS 的完整 docstring，其中 `Options` 段落列出 `gtol`、`maxiter`、`norm` 等可调选项。
- 第 2 步 `type(text)` 是 `str`，且 `'gtol' in text` 为 `True`。
- 第 3 步 `'bfgs'` 与 `'BFGS'` 返回的文本完全相同。
- 第 4 步抛出 `ValueError: Unknown method 'not-a-method'`。

**预期结果**：以上四点均成立。具体打印出的选项列表内容以你本地 SciPy 版本的 `_minimize_bfgs` docstring 为准（**待本地验证**具体条目）。

#### 4.4.5 小练习与答案

**练习 1**：`show_options('minimize', 'BFGS')` 打印出来的内容，和网页文档是同一份吗？

**答案**：本质上是同一来源——都是 `_minimize_bfgs` 函数的 docstring。`show_options` 直接在运行时读取 `obj.__doc__`，所以它永远和源码同步，比网页文档更不容易过时。

**练习 2**：`disp=True` 和 `disp=False` 的返回值有什么区别？

**答案**：`disp=True` 时函数 `print(text)` 后返回 `None`；`disp=False` 时不打印，而是返回文本字符串，方便你把它存进变量、写进日志或做字符串匹配。

**练习 3**：如果我调用 `show_options('minimize', 'BFGS')` 但拼成了 `'MINIMIZE'`，会报错吗？

**答案**：不会。因为代码里对 `solver` 和 `method` 都做了 `.lower()`（见 `_optimize.py:4135` 和 `4146`），所以大小写不敏感。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「带诊断信息的最小化流程」小脚本。这个脚本覆盖：查选项（4.4）→ 求解（4.2）→ 看结果对象（4.2）→ 触发并捕获警告（4.3）。

```python
# 文件名建议：u1_l1_comprehensive.py
import warnings
import numpy as np
from scipy.optimize import minimize, rosen, show_options, OptimizeWarning

# ---- 第 1 步：先查阅 BFGS 支持哪些 options（不查网页）----
print("===== BFGS options =====")
print(show_options('minimize', 'BFGS', disp=False))

# ---- 第 2 步：用 Rosenbrock 跑一次 BFGS ----
x0 = np.array([1.3, 0.7, 0.8, 1.9, 1.2])

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    # 故意混入一个不存在的选项，触发 OptimizeWarning
    res = minimize(rosen, x0, method='BFGS',
                   options={'maxiter': 1000, 'bogus_option': 1})

# ---- 第 3 步：解读 OptimizeResult ----
print("\n===== OptimizeResult =====")
print(res)
print("成功?", res.success, "| 原因:", res.message)
print("最优解 x:", res.x, "(理论值应为全 1)")
print("目标值 fun:", res.fun, "(理论值应为 0)")
print("迭代 nit:", res.nit, "| 函数求值 nfev:", res.nfev)
print("实际字段 keys:", list(res.keys()))

# ---- 第 4 步：检查是否真的发出了 OptimizeWarning ----
print("\n===== 警告 =====")
for w in caught:
    print(f"[{w.category.__name__}] {w.message}")
ow = [w for w in caught if issubclass(w.category, OptimizeWarning)]
print("其中 OptimizeWarning 数量:", len(ow))
```

**完成标准**：
1. 第 1 步能打印出 BFGS 的 options 文档（含 `gtol`、`maxiter` 等关键字）。
2. 第 3 步 `res.success` 为 `True`、`res.fun` 接近 0、`res.x` 接近全 1。
3. 第 4 步至少捕获到 1 条 `OptimizeWarning`，信息里提到 `bogus_option`。
4. 你能用自己的话解释 `res.keys()` 里每个字段的含义（参考 4.2.3 的字段表）。

> 注：`res.nit`、`res.nfev` 的具体数值会因 SciPy 版本与运行环境而异，属正常现象。

## 6. 本讲小结

- `scipy.optimize` 是 SciPy 里负责「最小化、求根、线性规划、最小二乘、曲线拟合、指派」等数值问题的子包；它的能力清单就写在 [`__init__.py` 的模块 docstring](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L13-L26) 里，并通过 `autosummary` 自动生成文档。
- `OptimizeResult` 是所有求解器统一返回的结果对象，本质是 `dict` 子类（经 `_RichResult`），既支持 `res['fun']` 也支持 `res.fun`；常见字段有 `x`、`fun`、`success`、`message`、`status`、`nfev`、`nit` 等，具体哪些字段由求解器决定。
- `OptimizeWarning` 是 `optimize` 的统一警告类型（继承 `UserWarning`），最常用于「未知选项」提醒；它只警告、不中断程序。
- `show_options` 通过一张 `doc_routines` 映射表，在运行时直接读取各方法函数的 `__doc__`，让你在代码里就能查阅某个求解器的全部 options，且永远与源码同步。
- 三个公共对象之所以能跨所有求解器复用，是因为它们由调度层（`minimize`/`root`/`linprog`）统一构造/触发，具体算法只负责填值。

## 7. 下一步学习建议

本讲建立了「全局认知」，但还没有进入任何具体算法。建议按手册的学习顺序继续：

- 如果你想先从最简单的问题入手：下一讲看 **标量优化与一维求根**（单元 u2），从 `minimize_scalar` 和 `root_scalar` 开始，门槛最低。
- 如果你想理解 `minimize` 这个「总入口」到底怎么把请求分发到 BFGS / CG / SLSQP 等具体方法：看 **统一调度入口：minimize 与 minimize_scalar**（u1-l3）。
- 如果你想了解 `scipy/optimize` 这个目录的物理结构、哪些是 C/Cython 编译后端：看 **目录结构、构建方式与 C/Cython 后端入口**（u1-l2）。

继续阅读建议源码：[`_optimize.py` 的 `OptimizeResult` 段](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L112-L154) 与 [`show_options` 段](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3948-L4169) 可以反复对照本讲，确认你真的看懂了。
