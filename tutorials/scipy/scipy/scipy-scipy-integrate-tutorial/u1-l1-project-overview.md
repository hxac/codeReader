# 项目定位与 scipy.integrate 全景

## 1. 本讲目标

学完本讲你应该能够：

- 用一句话说清 `scipy.integrate` 这个子包是做什么的、它在 SciPy 里处于什么位置。
- 区分四大类公开 API：**函数积分**、**固定样本积分**、**ODE 初值问题**、**ODE 边值问题**，外加一类「旧 API」。
- 打开 `__init__.py` 就能看懂它是如何把各个子模块的名字「搬」进 `scipy.integrate` 命名空间的。
- 自己动手列出每个类别包含哪些函数，并知道每个函数来自哪个源文件。

## 2. 前置知识

下面几个术语如果不熟没关系，先建立直觉即可：

- **积分（integration）**：求一个函数「曲线下的面积」。例如 \(\int_0^\pi \sin(x)\,dx = 2\)。「数值积分」就是用计算机算这个面积。
- **固定样本积分**：你已经有一堆离散数据点 \((x_i, y_i)\)，要估算它们围成的面积。比如实验测了一组温度随时间的点，想算「累积热量」。
- **函数积分**：你手里有一个可调用的函数 \(f(x)\)，让算法自己决定在哪里采样来算积分。
- **ODE（ordinary differential equation，常微分方程）**：形如 \(\frac{dy}{dt} = f(t, y)\) 的方程，描述「变化率」。比如「速度 = 加速度随时间的积分」。
- **初值问题（IVP，initial value problem）**：已知起点 \(t_0\) 时刻的状态 \(y(t_0)\)，想求之后的 \(y(t)\)。
- **边值问题（BVP，boundary value problem）**：已知的是两端（例如 \(x=a\) 和 \(x=b\)）的条件，要反推整段解。
- **Python 包的 `__init__.py`**：一个目录要成为 Python 的「包（package）」，里面通常要有 `__init__.py`。当你写 `from scipy import integrate` 时，Python 执行的就是这个文件，它决定了 `scipy.integrate` 命名空间里能看到哪些名字。
- **`from ._xxx import *`**：从当前包下的某个子模块 `_xxx` 里，把它「公开」的名字全部导入进来。

## 3. 本讲源码地图

本讲围绕一个核心文件：

| 文件 | 作用 |
|------|------|
| `scipy/integrate/__init__.py` | 包入口。它既是给 Sphinx 文档生成器看的「API 目录」（`autosummary` 注释），又是真正执行导入、把名字搬进 `scipy.integrate` 命名空间的代码。 |

为了建立全局印象，下面这张「源文件 → 公开 API」对照表，依据就是 `__init__.py` 里的导入语句（精确位置见 4.1.3）：

| 源文件 | 导出的代表性公开名字 |
|--------|---------------------|
| `_quadrature.py` | `trapezoid`、`cumulative_trapezoid`、`simpson`、`cumulative_simpson`、`romb`、`newton_cotes`、`fixed_quad`、`qmc_quad` |
| `_quadpack_py.py` | `quad`、`dblquad`、`tplquad`、`nquad`、`IntegrationWarning` |
| `_quad_vec.py` | `quad_vec` |
| `_cubature.py` | `cubature` |
| `_tanhsinh.py` | `tanhsinh`、`nsum` |
| `_lebedev.py` | `lebedev_rule` |
| `_odepack_py.py` | `odeint`、`ODEintWarning` |
| `_ode.py` | `ode`、`complex_ode` |
| `_bvp.py` | `solve_bvp` |
| `_ivp/`（子包） | `solve_ivp`、`RK23`、`RK45`、`DOP853`、`Radau`、`BDF`、`LSODA`、`OdeSolver`、`DenseOutput`、`OdeSolution` |

> 注意：以下划线开头的模块（如 `_quadrature`）按 Python 惯例是「内部模块」，用户不应直接 `import scipy.integrate._quadrature`，而应通过 `__init__.py` 暴露的公开名字来使用。

## 4. 核心概念与源码讲解

### 4.1 `__init__.py` 模块导出

#### 4.1.1 概念说明

`scipy.integrate` 是 SciPy 里专门做「数值积分」和「常微分方程求解」的子包。它解决两大类数学问题：

1. **积分**：算面积、算总量。又分「给你函数，自己采样」（自适应函数积分）和「给你一堆点，估算面积」（固定样本积分）两种情况。
2. **求解 ODE**：算一个随时间/空间演化的系统。又分「知道起点求未来」（初值问题 IVP）和「知道两端求整段」（边值问题 BVP）两种。

而 `__init__.py` 就是这个子包的「门面」和「目录」。它同时干两件事：

- **当文档用**：文件顶部一大段注释用 Sphinx 的 `autosummary` 指令，把所有公开函数按主题分组列出来——这既是给文档站点用的，也是给人类读者看的「API 清单」。
- **当代码用**：下面十几行 `from ._xxx import ...` 真正把名字搬进 `scipy.integrate` 命名空间，这样你写 `from scipy import integrate; integrate.quad(...)` 才能用。

#### 4.1.2 核心流程

当你执行 `from scipy import integrate` 时，Python 大致经历：

```
1. Python 找到 scipy/integrate/__init__.py 并执行它
2. 逐行执行 import 语句：
   from ._quadrature import *     → 把 _quadrature 的公开名字搬进来
   from ._odepack_py import *     → 搬进 odeint 等
   from ._quadpack_py import *    → 搬进 quad 等
   from ._ode import *            → 搬进 ode / complex_ode
   from ._bvp import solve_bvp    → 只搬 solve_bvp
   from ._ivp import (...)        → 从子包搬进 solve_ivp 和各求解器类
   from ._quad_vec import quad_vec
   from ._tanhsinh import nsum, tanhsinh
   from ._cubature import cubature
   from ._lebedev import lebedev_rule
3. 计算 __all__ = [s for s in dir() if not s.startswith('_')]
   → 把此刻命名空间里所有「不以 _ 开头」的名字收集成公开 API 列表
4. 之后 integrate.quad、integrate.solve_ivp 等都能直接访问
```

关键设计要点：

- 用 `import *` 配合每个子模块自己的 `__all__`，让 `__init__.py` 不必逐个罗列名字，减少维护成本。
- 最后那行 `__all__ = [s for s in dir() if not s.startswith('_')]` 是「自动汇总」：凡是搬进来时不以下划线开头的名字，都算公开 API。这是一种「约定优于配置」的写法。

#### 4.1.3 源码精读

**① 顶部文档：autosummary 五大分区**

文件开头是一段模块文档字符串，用标题和 `.. autosummary::` 把公开 API 分成几组。第一组是「给定函数对象做积分」（自适应函数积分）：

[__init__.py:8-25](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L8-L25) —— 这里以标题加 `autosummary` 列出了 `quad`、`quad_vec`、`cubature`、`dblquad`、`tplquad`、`nquad`、`tanhsinh`、`fixed_quad`、`newton_cotes`、`lebedev_rule`、`qmc_quad`、`IntegrationWarning`。这一组的特点是：你传一个**可调用函数**进去，算法自己决定怎么采样。

第二组「给定固定样本做积分」：

[__init__.py:28-39](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L28-L39) —— 列出 `trapezoid`、`cumulative_trapezoid`、`simpson`、`cumulative_simpson`、`romb`。这一组的特点是：你只给一堆已经采好的点 `(y, x)`，用梯形/抛物线等几何公式估算面积。

第三组「求和（Summation）」：

[__init__.py:46-52](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L46-L52) —— 只有 `nsum` 一个，把「无穷级数求和」转化为积分来做（与 `tanhsinh` 同在 `_tanhsinh.py`）。

第四组「ODE 初值问题」：

[__init__.py:54-72](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L54-L72) —— 既有便捷函数 `solve_ivp`，也有各求解器类 `RK23`/`RK45`/`DOP853`（显式）、`Radau`/`BDF`（隐式，用于刚性）、`LSODA`（自动切换），以及基类 `OdeSolver`、插值类 `DenseOutput`、连续解类 `OdeSolution`。文档里还说明了这些类既能直接用（低层）也能通过 `solve_ivp` 用（便捷）。

其中「Old API」是初值问题下的一个小节，专门标注了**旧接口**：

[__init__.py:75-90](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L75-L90) —— `odeint`、`ode`、`complex_ode`、`ODEintWarning`。文档明确说明：这些是更早开发的、包装老 Fortran 求解器的接口，不如新 API 方便，但求解器本身质量好、速度快。

第五组「ODE 边值问题」：

[__init__.py:93-99](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L93-L99) —— 只有 `solve_bvp` 一个函数。

**② 真正执行导入的代码**

文档字符串结束（第 100 行 `"""`）之后，才是真正干活的导入语句：

[__init__.py:103-116](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L103-L116) —— 这是核心，我们逐行看：

- `from ._quadrature import *`：搬入固定样本积分（梯形、辛普森、龙贝格）+ `fixed_quad`/`newton_cotes`/`qmc_quad`。
- `from ._odepack_py import *`：搬入旧 API 的 `odeint` 和 `ODEintWarning`。
- `from ._quadpack_py import *`：搬入自适应函数积分主力 `quad`，以及多重积分 `dblquad`/`tplquad`/`nquad`，还有 `IntegrationWarning`。
- `from ._ode import *`：搬入旧 API 的 `ode`、`complex_ode`。
- `from ._bvp import solve_bvp`：只显式搬入 `solve_bvp`（没用 `*`，因为这个模块对外主要就是它）。
- `from ._ivp import (...)`：从 `_ivp` **子包**搬入新 API 的 `solve_ivp` 和全套求解器类。注意这是从一个**子目录**的 `__init__.py` 导入。
- `from ._quad_vec import quad_vec` / `from ._tanhsinh import nsum, tanhsinh` / `from ._cubature import cubature` / `from ._lebedev import lebedev_rule`：分别显式搬入各自模块的对外名字。

**③ 已废弃命名空间**

[__init__.py:115-116](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L115-L116) —— `from . import dop, lsoda, vode, odepack, quadpack`。这一行导入了几个「加载桩」模块（如 `quadpack.py`、`vode.py`），它们只是为向后兼容而保留，计划在 v2.0.0 移除。这就是为什么你偶尔还会看到 `scipy.integrate.quadpack` 这种写法——它是历史遗留，不要在新代码里依赖。

**④ 自动汇总 `__all__`**

[__init__.py:118](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L118) —— `__all__ = [s for s in dir() if not s.startswith('_')]`。`dir()` 返回当前命名空间里的所有名字；过滤掉下划线开头的（私有/内部），剩下的就是对外公开的 API。这就是「自动生成公开 API 列表」的机制。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `__init__.py` 到底把哪些名字搬进了 `scipy.integrate`，并按五大类别整理成表。

**操作步骤**：

1. 阅读上面的 `__init__.py` 第 8–99 行（文档分区）和第 103–116 行（导入语句）。
2. 写一小段 Python 脚本，用 `scipy.integrate.__all__` 直接拿到这个子包的公开 API 列表：

```python
# 示例代码：列出 scipy.integrate 的全部公开 API
from scipy import integrate

names = sorted(integrate.__all__)
print("公开 API 共", len(names), "个：")
for n in names:
    obj = getattr(integrate, n)
    kind = type(obj).__name__  # 是函数还是类？
    print(f"  {n:25s}  ({kind})")
```

3. 对照第 1 步读到的文档分区，把打印出的名字填进下面这张分类表。

**需要观察的现象**：

- `__all__` 里既有函数（`function`，如 `quad`），也有类（`type`，如 `OdeSolver`、`RK45`、`IntegrationWarning`）。
- 所有名字都不以下划线 `_` 开头（因为第 118 行的过滤）。
- 数量比 `autosummary` 注释里列的略多，因为 `import *` 还会带进一些辅助名字。

**预期结果**（按五大类别整理，关键项如下）：

| 类别 | 包含的公开名字 |
|------|----------------|
| 函数积分（自适应，给函数） | `quad`、`quad_vec`、`dblquad`、`tplquad`、`nquad`、`cubature`、`tanhsinh`、`nsum`、`fixed_quad`、`newton_cotes`、`lebedev_rule`、`qmc_quad`、`IntegrationWarning` |
| 固定样本积分（给样本点） | `trapezoid`、`cumulative_trapezoid`、`simpson`、`cumulative_simpson`、`romb` |
| ODE 初值问题（新 API） | `solve_ivp`、`RK23`、`RK45`、`DOP853`、`Radau`、`BDF`、`LSODA`、`OdeSolver`、`DenseOutput`、`OdeSolution` |
| ODE 边值问题 | `solve_bvp` |
| 旧 API（老 Fortran 封装） | `odeint`、`ode`、`complex_ode`、`ODEintWarning` |

> 说明：源码文档把 `nsum` 单列在「Summation（求和）」一节，但因为它和 `tanhsinh` 同在 `_tanhsinh.py`、思想都是「函数积分」，本表把它归入「函数积分」一类。
>
> 如果本地 SciPy 版本与本讲义 HEAD（`5f09bd7`）不同，个别名字可能有增减，以你打印出的 `__all__` 为准——这就是需要「待本地验证」的部分。

#### 4.1.5 小练习与答案

**练习 1**：`quad` 和 `trapezoid` 最根本的区别是什么？

> **答案**：`quad` 属于「函数积分」——你传一个可调用的 `f(x)`，算法自适应地决定在哪里采样；`trapezoid` 属于「固定样本积分」——你已经有一组点 `(y, x)`，它只用现成的点按梯形公式估算面积。前者有「函数」，后者只有「数据」。

**练习 2**：为什么 `__init__.py` 最后要写 `__all__ = [s for s in dir() if not s.startswith('_')]`，而不是手工把所有函数名列出来？

> **答案**：这是「约定优于配置」。前面每条 `from ._xxx import *` 已经把公开名字搬进来了；用 `dir()` 自动收集、只过滤掉下划线开头的私有名字，就不用每次新增/删除函数都去手动维护列表，降低出错和维护成本。

**练习 3**：下面这句话哪里说错了：「`from ._ivp import (...)` 表示 `solve_ivp` 等求解器来自一个叫 `_ivp.py` 的文件。」

> **答案**：错在「文件」。`_ivp` 是一个**子包（子目录）**，不是单个文件。`from ._ivp import ...` 实际执行的是 `scipy/integrate/_ivp/__init__.py`，再由它从 `ivp.py`、`rk.py`、`radau.py`、`bdf.py`、`lsoda.py`、`base.py`、`common.py` 等文件里汇集这些名字。

## 5. 综合实践

**任务**：写一个脚本 `explore_integrate.py`，完成「认识 → 分类 → 试用」三步，把本讲知识串起来。

1. **认识**：导入 `scipy.integrate`，打印它的 `__all__` 长度。
2. **分类**：写一个函数，用一张「关键词 → 类别」的小字典（例如名字含 `quad`/`simpson`/`trapezoid` 归「积分」、含 `solve_ivp`/`RK`/`BDF`/`Radau`/`LSODA`/`Ode` 归「ODE 初值」、含 `solve_bvp` 归「边值」），把每个公开名字自动归到五大类别之一，并打印分类统计。
3. **试用**：从「函数积分」「固定样本积分」「ODE 初值」三个类别各挑一个函数，写一行最小调用并打印结果：

```python
# 示例代码（骨架，需本地安装 SciPy 才能运行）
from scipy import integrate
import numpy as np

# 函数积分：quad 算 ∫₀^π sin(x) dx
val, err = integrate.quad(np.sin, 0, np.pi)
print("quad:", val, err)        # 期望约 2.0

# 固定样本积分：trapezoid 算离散点下的面积
x = np.linspace(0, np.pi, 100)
print("trapezoid:", integrate.trapezoid(np.sin(x), x))   # 期望约 2.0

# ODE 初值问题：solve_ivp 求 dy/dt = -y, y(0) = 1
sol = integrate.solve_ivp(lambda t, y: -y, [0, 5], [1.0], dense_output=True)
print("solve_ivp y(5):", sol.sol(5.0))   # 期望约 exp(-5) ≈ 0.0067
```

**检查点**：

- 三个调用都应跑通不报错（前提是本地装好了 SciPy）。
- `quad` 与 `trapezoid` 的结果都应接近 `2.0`（解析解 \(\int_0^\pi \sin(x)\,dx = 2\)）。
- `solve_ivp` 在 \(t=5\) 的值应接近 \(\mathrm{e}^{-5}\)。
- 如果本地没有 SciPy 环境，这一步属于「待本地验证」，可改为纯阅读型实践：手动对照 `__init__.py` 第 103–116 行，写出每个公开名字的「来源模块 → 类别」对应表。

## 6. 本讲小结

- `scipy.integrate` 是 SciPy 专做「数值积分」和「常微分方程（ODE）求解」的子包。
- 它的公开 API 在 `__init__.py` 顶部按主题分成五大类：函数积分、固定样本积分、ODE 初值问题、ODE 边值问题，外加一类「旧 API」。
- `__init__.py` 身兼两职：上半部分是给文档/人类看的 `autosummary` 目录，下半部分是真正执行的 `from ._xxx import ...` 导入语句。
- 各公开名字分别来自 `_quadrature.py`、`_quadpack_py.py`、`_ivp/` 子包等内部模块；下划线开头的是内部模块，应通过公开名字使用。
- `__all__ = [s for s in dir() if not s.startswith('_')]` 这行用「自动收集 + 过滤下划线」的方式生成公开 API 列表，是「约定优于配置」的典型写法。

## 7. 下一步学习建议

- 下一篇（u1-l2）会带你深入 `integrate/` 的**目录结构**和 `meson.build` 构建配置，看清「源文件 → 扩展模块 → 安装包」这条链路。
- 想直接动手用 API，可以先看 u1-l3 的上手示例，再看 u2（固定样本积分）。
- 想理解某类 API 的内部原理：函数积分看 u3 / u4 / u5，ODE 初值问题看 u6–u9，边值问题看 u10，旧 API 看 u11。
- 建议阅读顺序遵循 `depends_on` 依赖图：先把概览与上手（u1）打通，再按需进入各专题。
