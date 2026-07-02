# 子包概览与运行方式

## 1. 本讲目标

本讲是 `scipy.differentiate` 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 `scipy.differentiate` 这个子包**是什么**、**解决什么问题**；
- 列出子包对外暴露的三个公开函数 `derivative`、`jacobian`、`hessian` 及它们各自解决哪一类求导问题；
- 看懂子包的目录结构，理解 `__init__.py`、`meson.build`、文档 `.rst` 文件各自扮演的角色；
- 在本地 Python 环境里导入并调用 `scipy.differentiate`，跑通一个最简单的数值求导例子。

本讲**只做「黑盒」认识**：我们暂时不关心算法内部是怎么实现的，重点是把子包的定位、边界和入口搞清楚，为后续白盒剖析打好基础。

## 2. 前置知识

在开始之前，最好对下面几个概念有基本的直觉。如果你完全不熟悉，也不必担心，本讲会用通俗的方式再解释一遍。

- **数值微分（Numerical Differentiation）**：当你有一个函数 $f(x)$，但只知道它的「黑盒」输入输出（例如它是一段程序、一个仿真、一个第三方库的函数），无法写出解析导数时，可以用「在 $x$ 附近取若干个点，计算函数值差商」的方式去**逼近**导数 $f'(x)$。最直观的就是

  \[
  f'(x) \approx \frac{f(x+h) - f(x-h)}{2h},
  \]

  这就是「中心差分」。$h$ 称为步长（step size）。

- **有限差分（Finite Difference）**：上面这种「取若干点 + 差商」的一整套方法统称有限差分。选取多少个点、点怎么排布、如何加权组合，就构成了不同的「差分公式」。

- **雅可比矩阵（Jacobian）** 与 **海森矩阵（Hessian）**：对于多元函数，一阶偏导组成的矩阵叫雅可比矩阵；二阶偏导组成的矩阵叫海森矩阵。本子包的 `jacobian`、`hessian` 就是分别求它们的数值版本。

- **Python 包的导入机制**：`import scipy.differentiate` 时，Python 会执行该子包目录下的 `__init__.py`，由它决定对外暴露哪些名字。`__all__` 列表控制 `from scipy.differentiate import *` 会导入哪些名字。

- **Meson 构建系统**：SciPy 使用 Meson（配合 Ninja）作为构建工具。`meson.build` 文件描述「源码文件如何被打包安装」。本讲只需要看懂它声明了哪些 `.py` 文件即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `scipy/differentiate/__init__.py` | 子包的入口文件，定义模块文档、对外公开的 API 列表（`__all__`）和测试入口。 |
| `scipy/differentiate/_differentiate.py` | 子包的**全部核心实现**，`derivative`、`jacobian`、`hessian` 三个函数都在这里。 |
| `scipy/differentiate/meson.build` | 构建脚本，声明要安装哪些 Python 源文件，以及包含 `tests` 子目录。 |
| `doc/source/reference/differentiate.rst` | 文档入口，用 Sphinx 的 `automodule` 指令自动从源码生成 API 参考页。 |
| `scipy/__init__.py` | SciPy 顶层包入口，把 `differentiate` 注册为可导入的子模块。 |

一句话总结：`__init__.py` 决定「暴露什么」，`meson.build` 决定「装什么」，`.rst` 决定「文档怎么生成」，而 `_differentiate.py` 才是真正的「干货实现」。

## 4. 核心概念与源码讲解

按三个最小模块拆分：**子包定位与公开 API**、**目录与构建入口**、**导入与运行方式**。

### 4.1 子包定位与公开 API

#### 4.1.1 概念说明

`scipy.differentiate` 是 SciPy 中专门做**有限差分数值微分**的子包。它的核心定位是：

> 对一个「黑盒」函数 $f$（只要能调用、能返回函数值，不需要解析表达式），逐元素地、自适应地逼近它的导数。

子包对外只暴露**三个**公开函数：

- `derivative(f, x, ...)`：对逐元素的实值标量函数 $f$，求每个元素处的一阶导数 $f'(x)$。
- `jacobian(f, x, ...)`：对向量值函数，求雅可比矩阵。
- `hessian(f, x, ...)`：对标量值多元函数，求海森矩阵。

这三个函数是**分层**关系：`derivative` 是地基，`jacobian` 在 `derivative` 之上构建，`hessian` 又在 `jacobian` 之上构建（本质是「雅可比的雅可比」）。这个层次关系是后续讲义的主线，本讲先记住名字和用途即可。

#### 4.1.2 核心流程

从「用户视角」看，使用子包的流程非常简单：

1. 准备一个可调用的函数 $f$（签名形如 `f(xi, *args) -> ndarray`）。
2. 准备求导点 $x$（标量或数组）。
3. 调用 `derivative(f, x)`（或 `jacobian` / `hessian`）。
4. 从返回结果对象中读取 `df`（导数估计值）和 `error`（误差估计）。

伪代码：

```text
res = derivative(f, x)
导数近似 = res.df
误差估计 = res.error
```

子包内部如何一步步逼近、何时停止，是后续讲义的内容，本讲先建立「黑盒调用」的直觉。

#### 4.1.3 源码精读

子包对外暴露哪些名字，完全由 `__init__.py` 中的 `__all__` 决定。我们看真实源码：

[__init__.py:23 — 对外公开的 API 列表](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/__init__.py#L23)

这行 `__all__ = ['derivative', 'jacobian', 'hessian']` 明确告诉我们：子包只有这三个公开函数。也就是说 `from scipy.differentiate import *` 只会带入这三个名字，其它以下划线开头的内部函数（如 `_differentiate`、`_derivative_iv`）都不会被当作公开 API。

这三个名字从哪里来？看导入语句：

[__init__.py:21 — 从实现模块导入全部公开名字](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/__init__.py#L21)

`from ._differentiate import *` 把实现模块 `_differentiate.py` 里的（非下划线开头）名字搬进子包命名空间。三个函数的真实定义就在那个文件里，它们的签名分别是：

[_differentiate.py:67-69 — derivative 的函数签名](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L67-L69)

这是地基函数，默认 `order=8`（8 阶有限差分公式）、`initial_step=0.5`、`step_factor=2.0`、`maxiter=10`。这些参数的直观含义是后续讲义的重点，本讲只需看到「它接受一个函数 `f` 和求导点 `x`」。

[_differentiate.py:723-724 — jacobian 的函数签名](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L723-L724)

`jacobian` 的参数集是 `derivative` 的子集（没有 `args`、`preserve_shape`、`callback`），因为内部会自己把这些参数组装好再委托给 `derivative`。

[_differentiate.py:953-954 — hessian 的函数签名](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L953-L954)

`hessian` 的参数集更小，本讲先不展开。

`__init__.py` 顶部的模块文档里也用 Sphinx 的 `autosummary` 把这三个函数登记为文档条目：

[__init__.py:11-16 — 文档自动摘要登记的三个函数](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/__init__.py#L11-L16)

这段同时说明了子包的定位：「SciPy ``differentiate`` provides functions for performing finite difference numerical differentiation of black-box functions.」（为黑盒函数提供有限差分数值微分的函数）。

#### 4.1.4 代码实践

- **实践目标**：验证子包确实只暴露三个公开函数。
- **操作步骤**：在已安装 SciPy 的环境里执行下面这段脚本：

  ```python
  import scipy.differentiate as diff
  print(diff.__all__)
  print([name for name in dir(diff) if not name.startswith('_')])
  ```

- **需要观察的现象**：第一行打印出的 `__all__` 内容，第二行打印出子包命名空间里的公开名字。
- **预期结果**：`__all__` 应当是 `['derivative', 'jacobian', 'hessian']`（这一点可直接从上面引用的源码第 23 行确认，无需运行即可断言）。
- **运行时输出**：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么子包要把内部实现放在以下划线开头的 `_differentiate.py`，而对外只通过 `__all__` 暴露三个名字？

**参考答案**：以下划线开头是 Python 的「私有」约定，表示「这是实现细节，不在公开 API 范围内，将来可能改动」。`__all__` 则明确划定「对外契约」。这样既允许内部自由重构（例如改 `_derivative_weights` 的实现），又保证用户只依赖 `derivative/jacobian/hessian` 这三个稳定接口。

**练习 2**：如果某个用户写 `from scipy.differentiate import _derivative_iv`，会成功吗？这样做好不好？

**参考答案**：技术上能成功导入（`from ... import *` 不会带入它，但显式写出名字仍可导入），因为 Python 的下划线只是约定而非强制。但这是**坏习惯**：`_derivative_iv` 是内部输入校验函数，不在公开契约里，未来 SciPy 版本可能改名或删除，依赖它会让你的代码脆弱。

---

### 4.2 目录与构建入口

#### 4.2.1 概念说明

要理解一个 Python 子包「由什么组成」，最直接的方式是看它的目录结构和构建脚本。`scipy.differentiate` 的目录非常精简：

```text
scipy/differentiate/
├── __init__.py          # 子包入口：文档 + __all__ + 测试入口
├── _differentiate.py    # 全部核心实现（derivative/jacobian/hessian）
├── meson.build          # 构建脚本：声明要安装哪些源文件
└── tests/
    ├── __init__.py
    ├── meson.build      # 测试目录的构建脚本
    └── test_differentiate.py   # 单元测试
```

注意一个关键事实：**整个子包的核心实现只有一个文件 `_differentiate.py`**（约 5 万字符）。这是一个体量很小、但数学内涵很集中的子包。后续讲义几乎全部围绕这一个文件展开。

#### 4.2.2 核心流程

构建脚本 `meson.build` 的工作流程：

1. 声明一个 Python 源文件列表 `python_sources`。
2. 调用 SciPy 封装的 `py3.install_sources(...)`，把这些文件安装到目标子目录 `scipy/differentiate` 下。
3. 用 `subdir('tests')` 把测试子目录也纳入构建。

这样在执行 `pip install scipy`（或开发安装）后，`import scipy.differentiate` 才能找到这些文件。

#### 4.2.3 源码精读

[meson.build:1-4 — 声明子包的两个 Python 源文件](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/meson.build#L1-L4)

这两行说明，子包真正参与安装的源码只有 `__init__.py` 和 `_differentiate.py`。`meson.build` 本身、文档 `.rst` 都不是运行时需要的，所以不在 `python_sources` 里。

[meson.build:6-9 — 把源文件安装到 scip/differentiate 子目录](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/meson.build#L6-L9)

`subdir: 'scipy/differentiate'` 指定安装位置，`py3` 是 SciPy 顶层 Meson 配置里定义的一个工具对象（封装了 Python 模块安装逻辑）。

[meson.build:11 — 把 tests 子目录纳入构建](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/meson.build#L11)

`subdir('tests')` 让 Meson 进入 `tests/` 目录处理那里的 `meson.build`。测试文件的安装带有一个 `install_tag: 'tests'`（见 `tests/meson.build`），表示这些文件只有需要跑测试时才安装。

至于文档，子包的 API 参考页由一个极简的 `.rst` 文件驱动：

[doc/source/reference/differentiate.rst:1-5 — 用 automodule 自动生成 API 文档](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/doc/source/reference/differentiate.rst#L1-L5)

`automodule` 指令告诉 Sphinx：「直接从 `scipy.differentiate` 的 docstring 抓取内容来生成这一页文档」。这就是为什么 `__init__.py` 顶部那段 `autosummary` 文档会出现在 SciPy 官方文档站上——文档与源码是同一份。

#### 4.2.4 代码实践

- **实践目标**：亲手确认子包在磁盘上的文件组成。
- **操作步骤**：在 SciPy 源码根目录执行：

  ```bash
  ls -la scipy/differentiate/
  ls -la scipy/differentiate/tests/
  cat scipy/differentiate/meson.build
  ```

- **需要观察的现象**：`differentiate/` 目录下确实只有 `__init__.py`、`_differentiate.py`、`meson.build` 和 `tests/` 四项；`tests/` 下有 `test_differentiate.py`。
- **预期结果**：与本节给出的目录树一致；`meson.build` 内容与本讲引用的源码一致（两个源文件 + 安装 + `subdir('tests')`）。
- **运行时输出**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果有人想给子包新增一个公开函数 `laplacian`，至少需要改动哪几处？

**参考答案**：至少三处：(1) 在 `_differentiate.py` 里实现 `laplacian`；(2) 在 `__init__.py` 的 `__all__` 列表里加上 `'laplacian'`，并在顶部 `autosummary` 块里登记它（用于文档）；(3) 因为 `from ._differentiate import *` 已经会把 `_differentiate.py` 里的公开名字搬进来，所以通常无需额外显式 import——但如果 `laplacian` 定义在别的文件，则还要加 import 语句。

**练习 2**：为什么 `test_differentiate.py` 不放在 `python_sources` 里、而单独用 `subdir('tests')` 处理？

**参考答案**：测试代码不需要随正式发布包分发给普通用户，只在开发/测试场景使用。用独立的 `subdir('tests')` + `install_tag: 'tests'` 标记，可以让打包工具在打「精简发布包」时跳过测试文件，减小体积，同时仍能在开发环境中完整安装。

---

### 4.3 导入与运行方式

#### 4.3.1 概念说明

「能导入」这件事，背后涉及两个层面：

1. **SciPy 顶层是否把 `differentiate` 当作子模块**：在 `scipy/__init__.py` 里需要把这个子包纳入（注册到顶层命名空间 / `__all__`）。
2. **子包入口文件是否正确暴露 API**：即前两节讲的 `__init__.py`。

满足这两点后，用户就能用三种常见方式导入：

```python
import scipy.differentiate as diff        # 带别名
from scipy.differentiate import derivative # 直接导入函数
import scipy; scipy.differentiate         # 通过顶层访问
```

#### 4.3.2 核心流程

一次 `from scipy.differentiate import derivative` 的解析流程：

1. Python 先执行 SciPy 顶层 `scipy/__init__.py`，它（在导入流程中）使得 `scipy.differentiate` 可被发现。
2. Python 进入子包目录，执行 `scipy/differentiate/__init__.py`。
3. 该文件执行 `from ._differentiate import *`，触发执行 `_differentiate.py`（定义三个函数及一堆内部辅助函数）。
4. `__all__` 决定哪些名字随 `import *` 带入子包命名空间。
5. `derivative` 等名字出现在 `scipy.differentiate` 命名空间，导入完成。

此外，`__init__.py` 末尾还附带了一个测试便利入口：

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
```

这意味着 `scipy.differentiate.test()` 可以一键运行该子包的测试套件。

#### 4.3.3 源码精读

先看顶层 SciPy 如何把 `differentiate` 登记为子模块：

[scipy/\_\_init\_\_.py:15 — 顶层模块清单里对 differentiate 的一句话描述](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/__init__.py#L15)

这行出现在顶层包对各个子模块的「目录式注释」里，说明 `differentiate --- Finite difference differentiation tools`。

[scipy/\_\_init\_\_.py:99 — differentuate 被列入顶层 __all__](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/__init__.py#L99)

顶层 `__all__` 包含 `'differentiate'`，保证 `from scipy import *` 也能带上这个子包。

再看子包入口如何收尾：

[__init__.py:25-27 — 挂载一键测试入口 test()](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/__init__.py#L25-L27)

`PytestTester(__name__)` 创建一个针对本子包的 pytest 运行器，绑定到名字 `test`。随后 `del PytestTester` 把构造器本身从命名空间删掉，避免它污染公开 API。结果是：用户可以 `scipy.differentiate.test()` 跑测试，但 `PytestTester` 不会出现在 `dir(scipy.differentiate)` 里。

#### 4.3.4 代码实践

- **实践目标**：跑通「导入 + 一次最简数值求导」的全流程，验证子包可用。
- **操作步骤**：在已安装 SciPy 的环境里执行：

  ```python
  import numpy as np
  from scipy.differentiate import derivative

  # 打印子包公开 API
  import scipy.differentiate as diff
  print("public API:", diff.__all__)

  # 数值求 d/dx sin(x) 在 x=0 处的值
  res = derivative(np.sin, 0.0)
  print("df =", res.df)
  print("error =", res.error)
  print("analytic cos(0) =", np.cos(0.0))
  ```

- **需要观察的现象**：`__all__` 为三个函数；`res.df` 应当非常接近解析值 $\cos(0)=1.0$；`res.error` 是一个很小的误差估计。
- **预期结果**：`__all__ == ['derivative', 'jacobian', 'hessian']`（源码可确认）。由于 $\frac{d}{dx}\sin x = \cos x$，$\cos(0)=1.0$，所以 `res.df` 应近似等于 `1.0`，两者之差应与 `res.error` 同量级（很小的数）。
- **运行时输出**：待本地验证（`res.df` 与 `res.error` 的具体数值需在你本地环境实际运行后确认）。

> 说明：本实践改编自 `_differentiate.py` 里 `derivative` 函数 docstring 中给出的官方示例（对 `np.exp` 在若干点求导，见源码 Examples 段）。把被求导函数换成 `np.sin`、求导点换成 `0.0` 即可。

#### 4.3.5 小练习与答案

**练习 1**：执行 `import scipy.differentiate` 之后，`scipy.differentiate.test` 是什么？它从哪里来？

**参考答案**：它是一个绑定好的 `PytestTester` 实例，来自 `__init__.py` 末尾的 `test = PytestTester(__name__)`。调用它会针对 `scipy.differentiate` 子包运行 pytest 测试。注意 `PytestTester` 这个类本身在赋值后被 `del` 删除了，所以你只能用 `test` 这个名字，不能重新构造。

**练习 2**：三种导入写法 `import scipy.differentiate`、`from scipy.differentiate import derivative`、`import scipy` 后用 `scipy.differentiate.derivative`，它们得到的 `derivative` 是同一个对象吗？

**参考答案**：是同一个对象。无论哪种写法，Python 最终都会执行同一个 `__init__.py`，把同一个 `_differentiate.py` 里定义的 `derivative` 函数放入 `scipy.differentiate` 命名空间。三种写法只是访问路径不同，引用的是同一份函数定义。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个小任务：

**任务**：写一个脚本 `explore_differentiate.py`，完成以下事情，并把每一步的输出用注释解释清楚。

1. **API 探查**：导入 `scipy.differentiate`，打印 `__all__`，并打印 `derivative` 的 `__module__` 属性（验证它确实来自 `_differentiate` 模块）。
2. **构建/目录观察**：用 `os.path.dirname(scipy.differentiate.__file__)` 拿到子包在磁盘上的安装路径，用 `os.listdir` 列出该目录内容，验证它确实只有 `__init__.py`、`_differentiate.py` 等少量文件。
3. **黑盒求导**：定义 $f(x)=\sin(x)$，用 `derivative` 在 `x = np.linspace(-1, 1, 5)` 上求导，把数值结果 `res.df` 与解析值 `np.cos(x)` 放进一张表里对照，并打印最大绝对误差 `np.max(np.abs(res.df - np.cos(x)))`。

参考代码骨架（示例代码，非项目原有代码）：

```python
import os
import numpy as np
import scipy.differentiate as diff
from scipy.differentiate import derivative

# 1. API 探查
print("__all__:", diff.__all__)
print("derivative 来自模块:", derivative.__module__)

# 2. 目录观察
pkg_dir = os.path.dirname(diff.__file__)
print("子包目录:", pkg_dir)
print("目录内容:", sorted(os.listdir(pkg_dir)))

# 3. 黑盒求导对照
x = np.linspace(-1, 1, 5)
res = derivative(np.sin, x)
print("x        :", x)
print("数值导数 :", res.df)
print("解析导数 :", np.cos(x))
print("最大误差 :", np.max(np.abs(res.df - np.cos(x))))
```

**预期**：第 1 步的 `__all__` 为三个函数、`__module__` 为 `scipy.differentiate._differentiate`；第 2 步列出的文件与本讲目录树一致；第 3 步数值导数与解析导数高度吻合，最大误差很小。具体数值的运行时输出：待本地验证。

## 6. 本讲小结

- `scipy.differentiate` 是 SciPy 中做**有限差分数值微分**的子包，面向「黑盒函数」求导。
- 它对外**只暴露三个公开函数**：`derivative`（一阶导）、`jacobian`（雅可比）、`hessian`（海森），由 `__init__.py` 的 `__all__` 划定。
- 三者是**分层**关系：`jacobian` 建立在 `derivative` 之上，`hessian` 建立在 `jacobian` 之上。
- 子包结构极简：核心实现集中在**单个文件** `_differentiate.py`，加上入口 `__init__.py`、构建脚本 `meson.build` 和 `tests/` 目录。
- `meson.build` 负责声明要安装的源文件（`__init__.py` + `_differentiate.py`）并把 `tests` 纳入构建；文档则由 `.rst` 里的 `automodule` 自动从 docstring 生成。
- 通过 `from scipy.differentiate import derivative` 即可使用；`scipy.differentiate.test()` 可一键跑子包测试。

## 7. 下一步学习建议

本讲只是「认识门面」。下一步建议：

1. 先学 **u1-l2《derivative 快速上手与关键参数》**：动手玩 `derivative` 的 `order`、`initial_step`、`step_factor`、`maxiter` 等参数，建立对算法行为（步长缩减、收敛）的直觉。
2. 再学 **u1-l3《结果对象 _RichResult 与状态码》**：读懂返回对象的所有属性，学会判断一次求导是否真正收敛。
3. 在「会用」之后，再进入第二单元（u2），白盒精读 `_differentiate.py` 中 `derivative` 的输入校验、差分权重、迭代估值与终止条件等实现细节。

建议阅读源码顺序：先通读 `__init__.py`（本讲已做），再打开 `_differentiate.py` 只看 `derivative` 函数的 docstring 和 Examples 段（[_differentiate.py:70 起](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L70)），建立整体印象后再逐段精读。
