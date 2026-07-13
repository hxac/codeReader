# 测试体系与导入时 sanity check

## 1. 本讲目标

学完本讲，你应该能够：

- 说出 `numpy.test()` 是怎么把 pytest 接到每一个子包上的，并能用它运行 NumPy 的测试。
- 说出 `numpy.testing` 提供了哪些常用断言工具，以及它们为什么和普通 `assert` 不一样。
- 解释 NumPy 在「导入时」执行的 `_sanity_check` / `_mac_os_check` 检测的是哪一类错误，并理解为什么要在导入时而不是测试时做这件事。

本讲只读三个关键文件，加上两个 pytest 配置文件作为佐证：`numpy/_pytesttester.py`、`numpy/testing/__init__.py`、`numpy/__init__.py`，以及 `numpy/conftest.py` 和 `pytest.ini`。

## 2. 前置知识

- **pytest**：Python 生态里最常用的测试框架。只要把测试函数命名成 `test_` 开头、里面写 `assert`，pytest 就会自动发现并运行它们。
- **标记（marker）**：pytest 允许用 `@pytest.mark.slow` 给测试打标签，再用命令行 `-m` 选项筛选「只跑带某标记 / 不跑带某标记」的测试。
- **严格标记（`--strict-markers`）**：pytest 的一个开关，开启后，如果用了没在配置里登记过的标记，会直接报错，而不是悄悄忽略。
- **导入时副作用**：Python 在执行 `import numpy` 时，会从头到尾运行一遍 `numpy/__init__.py` 里的顶层代码。NumPy 利用这一点，在导入的**末尾**跑几个「健康检查」。
- **`__getattr__`（PEP 562）**：在模块级别定义 `__getattr__` 函数，可以让一个模块在访问它本不存在的属性时「按需加载」（懒加载）。这在上一讲 u1-l3 已经讲过。

**前置讲义**：本讲承接 u1-l2（构建、安装与运行）和 u1-l3（目录结构与导出）。你需要记得两件事：`_core` 是用 C 写的底层核心扩展；`numpy/__init__.py` 是把各子包名字汇聚成 `np.` 命名空间的装配入口。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [numpy/_pytesttester.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py) | 定义 `PytestTester` 类，把 pytest 包装成可以挂到任意子包上的 `test()` 函数 |
| [numpy/testing/__init__.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/testing/__init__.py) | 汇聚测试断言工具（`assert_allclose` 等），并给自己的子包注册一个 `test` |
| [numpy/testing/_private/utils.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/testing/_private/utils.py) | 这些断言函数的**真实实现**所在地 |
| [numpy/__init__.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py) | 顶层装配；在导入末尾注册 `test` 并运行 `_sanity_check` / `_mac_os_check` |
| [numpy/conftest.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/conftest.py) | pytest 配置；登记 `slow` 等标记 |
| [pytest.ini](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pytest.ini) | 全局 pytest 选项（`-l -ra --strict-markers --strict-config`） |

## 4. 核心概念与源码讲解

### 4.1 PytestTester 机制：把 pytest 挂到每一个子包

#### 4.1.1 概念说明

NumPy 的测试很多，分散在 `_core`、`lib`、`linalg`、`fft`、`random`、`ma` 等十几个子包里。一个很自然的需求是：**每个子包都能用自己的 `test()` 方法只跑自己的测试**，比如 `np.linalg.test()` 只跑线性代数的测试，`np.fft.test()` 只跑 FFT 的测试。

但 pytest 本身并不知道「numpy 子包」这个概念，它只会按文件路径或 `--pyargs` 参数去发现测试。NumPy 的做法是写一个**很薄的包装类 `PytestTester`**：它记住自己负责哪个模块名，被调用时把模块名翻译成 pytest 命令行参数，再交给 `pytest.main()` 去跑。

于是每个子包只需要在 `__init__.py` 里写三行，就拥有了一个 `test` 函数：

```python
from numpy._pytesttester import PytestTester
test = PytestTester(__name__)
del PytestTester
```

这段「样板代码」正是 `_pytesttester.py` 模块开头就给出的说明，见 [numpy/_pytesttester.py:1-30](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L1-L30)。在仓库里用 `grep` 搜 `PytestTester(__name__)`，可以找到 11 个子包都用了这套样板：顶层 `numpy`、`_core`、`lib`、`linalg`、`fft`、`random`、`ma`、`polynomial`、`matrixlib`、`f2py`、`testing` 自己。

#### 4.1.2 核心流程

调用 `np.test(...)`（或任意子包的 `test(...)`）时，内部流程是：

1. `PytestTester.__init__` 只记下模块名 `self.module_name`。
2. 调用 `__call__(label='fast', ...)` 时，根据模块名从 `sys.modules` 取出模块对象，拿到它所在的目录。
3. 拼接 pytest 命令行参数：基础参数 `-l -q`、若干 `-W` 警告过滤、根据 `label` 加 `-m` 筛选、根据 `tests` 加 `--pyargs`。
4. 调用 `_show_numpy_info()` 打印 NumPy 版本和 CPU 特性。
5. 调用 `pytest.main(pytest_args)` 真正跑测试，拿到退出码。
6. 返回 `code == 0`，也就是「**全部通过返回 True，否则返回 False**」。

有一个**开发模式 vs 发布模式**的区别值得记住：测试行为取决于仓库根目录有没有 `pytest.ini` 文件。

- **有 `pytest.ini`（开发模式）**：除了显式过滤掉的警告，其余警告一律当成错误抛出。开发者在仓库里跑 `spin test` 时就是这种模式，目的是让任何「悄悄冒出来的警告」都尽早暴露。
- **没有 `pytest.ini`（发布模式）**：`DeprecationWarning` / `PendingDeprecationWarning` 被忽略，其他警告放行。用户 `pip install` 装好的 NumPy 跑测试时通常是这种模式。

这段说明来自 [numpy/_pytesttester.py:13-25](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L13-L25)。

#### 4.1.3 源码精读

**类定义与构造**。`PytestTester` 很薄，构造时只存模块名，并把 `self.__module__` 也设成模块名（这样它在帮助文档里看起来更像属于那个子包）：

```python
class PytestTester:
    def __init__(self, module_name):
        self.module_name = module_name
        self.__module__ = module_name
```
见 [numpy/_pytesttester.py:45-77](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L45-L77)。注意它不是公开 API（注释里明确说「not publicly exposed」），因为它做了一些 NumPy 专属的警告抑制。

**调用签名**。真正干活的是 `__call__`，默认 `label='fast'`：

```python
def __call__(self, label='fast', verbose=1, extra_argv=None,
             doctests=False, coverage=False, durations=-1, tests=None):
```
完整签名与参数说明见 [numpy/_pytesttester.py:79-125](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L79-L125)。其中 `label` 只文档了两个取值：`'fast'`（跳过 `slow` 标记的测试）和 `'full'`（跑全部）。

**参数拼接（关键逻辑）**。这段最值得读，因为它解释了「label 到底怎么变成 pytest 行为」：

```python
pytest_args = ["-l", "-q"]
pytest_args += ["-W ignore:Not importing directory", ...]   # 过滤烦人的导入警告
if label == "fast":
    pytest_args += ["-m", "not slow"]      # fast = 排除 slow
elif label != "full":
    pytest_args += ["-m", label]           # 其他 label 当作标记名来筛
...
pytest_args += ["--pyargs"] + list(tests)  # 用模块名定位测试
```
见 [numpy/_pytesttester.py:131-176](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L131-L176)。注意中间这个分支：**如果 `label` 既不是 `'fast'` 也不是 `'full'`，它会被原样当成一个 pytest 标记名传给 `-m`**。这一点在下面的实践里很重要。

**运行并返回布尔值**：

```python
_show_numpy_info()
try:
    code = pytest.main(pytest_args)
except SystemExit as exc:
    code = exc.code
return code == 0
```
见 [numpy/_pytesttester.py:178-186](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L178-L186)。`_show_numpy_info()` 会打印 NumPy 版本和检测到的 CPU 特性，见 [numpy/_pytesttester.py:37-42](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_pytesttester.py#L37-L42)。

**顶层注册**。在 `numpy/__init__.py` 里，这三行就是 `np.test` 的来源：

```python
# Pytest testing
from numpy._pytesttester import PytestTester
test = PytestTester(__name__)
del PytestTester
```
见 [numpy/__init__.py:781-784](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L781-L784)。`del PytestTester` 是为了不让 `PytestTester` 这个名字泄露到 `np.` 命名空间。

#### 4.1.4 代码实践

**实践目标**：亲手调用 `np.test`，看清它返回什么、打印什么；并搞清楚 `label` 参数的边界。

**操作步骤**：

1. 在装好 NumPy（最好是本讲 u1-l2 里从源码 `spin build` 出来的开发版）的环境里启动 Python：

   ```python
   import numpy as np
   print(type(np.test))          # <class 'method'>，绑定在 PytestTester 实例上
   print(np.test.__self__.module_name)   # 'numpy'
   ```

2. 完整的 `np.test('fast')` 在大机器上也可能要跑很久（几千个测试）。为了快速看到效果，**先在一个小子包上跑**：

   ```python
   ok = np.linalg.test('fast')
   print("返回值 =", ok)          # True 表示全部通过
   ```

3. 观察输出开头两行（`_show_numpy_info` 打印的版本号和 CPU 特性）以及末尾 pytest 的总结行，形如 `N passed, M skipped in X.XX seconds`。

**关于 `numpy.test('quick')` 的提醒**：本讲的实践任务原文想让你运行 `numpy.test('quick')`，但需要特别说明——**`'quick'` 并不是 NumPy 注册过的 pytest 标记**。`PytestTester.__call__` 只文档了 `'fast'` 和 `'full'` 两个 label（见上面源码精读）。如果你传 `'quick'`，会走到 `pytest_args += ["-m", "quick"]` 这一支；而仓库的 [pytest.ini:1-3](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/pytest.ini#L1-L3) 开了 `--strict-markers`，于是 pytest 会报错说 `'quick'` 不是已知标记，并 deselect 所有测试。所以**请用 `numpy.test('fast')` 或不传参数（默认就是 `'fast'`）**。

**需要观察的现象**：

- `_show_numpy_info()` 打印的版本号和 CPU 特性行。
- pytest 的彩色进度条和最后的总结行。

**预期结果**：

- 在一个干净的、正确构建的 NumPy 上，`np.linalg.test('fast')` 应返回 `True`，并打印类似 `N passed, M skipped in ...` 的总结（具体数字「待本地验证」，因为它取决于平台和被跳过的测试）。
- 如果传 `'quick'`，会看到类似 `'quick' not found in markers configuration option` 的错误，且无测试运行。

> 说明：本讲不假装已经运行过命令。测试数量、耗时与跳过数请在你本地实际运行后填入。

#### 4.1.5 小练习与答案

**练习 1**：`np.test` 是一个普通函数吗？为什么 `type(np.test)` 是 `method`？

> **答案**：不是普通函数。`test = PytestTester(__name__)` 创建的是一个 `PytestTester` **实例**，而 `np.test` 取的是这个实例的 `__call__` 绑定方法，所以 `type(np.test)` 显示为 `method`。你可以用 `np.test.__self__` 拿到那个 `PytestTester` 实例。

**练习 2**：`np.test()` 默认的 `label` 是什么？它对应 pytest 的哪个命令行选项？

> **答案**：默认 `label='fast'`，对应 `pytest -m "not slow"`，也就是跳过所有打了 `@pytest.mark.slow` 标记的测试。

**练习 3**：为什么开发模式下「警告会被当成错误」？

> **答案**：开发模式下仓库里有 `pytest.ini`，pytest 按它配置运行；这种模式希望任何新冒出来的警告都尽早被开发者看到并修复，所以除显式过滤的以外都转成错误。发布模式则对最终用户更宽容，忽略弃用类警告。

---

### 4.2 testing 工具集：面向数组计算的断言

#### 4.2.1 概念说明

写测试离不开断言。Python 自带的 `assert` 比较两个对象是否「相等」，但科学计算里我们常常需要：

- 比较两个**浮点数组**是否「近似相等」（因为浮点误差，直接 `==` 几乎一定失败）。
- 比较两个数组的**形状和每个元素**是否都相同。
- 容忍 `NaN`、允许指定相对/绝对容差。
- 断言某段代码**应该抛出**特定异常或警告。

`numpy.testing` 就是这一组「为数值数组量身定做」的断言工具的集合。它本身**不是**给最终用户用的运行时检查（那是 `np.isclose` 的活），而是给写测试的人用的。

#### 4.2.2 核心流程

`numpy.testing` 的组织非常简单，本质是「汇聚 + 再导出」：

1. 所有断言函数的**真实实现**写在 `numpy/testing/_private/utils.py` 里（放在 `_private` 下，暗示「内部实现，别直接 import」）。
2. `numpy/testing/__init__.py` 用 `from ._private.utils import *` 把它们批量再导出，并组合出 `__all__`。
3. 用户写 `from numpy.testing import assert_allclose` 就能拿到。

同时，`testing/__init__.py` 自己也用 4.1 节的样板注册了一个 `test`，所以 `np.testing.test()` 可以只跑 testing 模块自己的测试。

#### 4.2.3 源码精读

**整个 `testing/__init__.py` 非常短**，可以一次读完：

```python
from unittest import TestCase
from . import _private, overrides
from ._private import extbuild
from ._private.utils import *
from ._private.utils import _assert_valid_refcount, _gen_alignment_data

__all__ = (
    _private.utils.__all__ + ['TestCase', 'overrides']
)

from numpy._pytesttester import PytestTester
test = PytestTester(__name__)
del PytestTester
```
见 [numpy/testing/__init__.py:1-22](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/testing/__init__.py#L1-L22)。要点：

- `from ._private.utils import *` 拉进所有公开断言；末尾两行单独 import 两个下划线开头的名字（它们不在 `__all__` 里，`import *` 拿不到，但 NumPy 内部测试要用）。
- `__all__` 直接复用 `_private.utils.__all__`，再补上标准库的 `TestCase` 和 `overrides`。
- `extbuild` 是用来在测试里**现场编译小 C 扩展**的工具（NumPy 不少测试要验证 C-API，所以需要能在测试时临时编译一段 C 代码）。

**这些断言函数都有哪些**？看 `_private/utils.py` 顶部的 `__all__`：

```python
'assert_equal', 'assert_almost_equal', 'assert_approx_equal',
'assert_array_equal', 'assert_array_less', 'assert_string_equal',
'assert_array_almost_equal', 'assert_raises', ...
'assert_allclose', ...
'assert_warns', 'assert_no_warnings', ...
'assert_array_almost_equal_nulp', 'assert_array_max_ulp',
```
见 [numpy/testing/_private/utils.py:33-43](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/testing/_private/utils.py#L33-L43)。最常用的几个：

| 断言 | 用途 |
|---|---|
| `assert_allclose(a, b, rtol=1e-7, atol=0)` | 浮点数组近似比较（最常用） |
| `assert_equal(a, b)` | 精确相等，但对形状、`NaN` 有数组语义 |
| `assert_array_equal(a, b)` | 逐元素精确相等 |
| `assert_raises(Exc, func, *args)` | 断言调用会抛出指定异常 |
| `assert_warns(Warning, func)` | 断言调用会产生指定警告 |

为什么不用普通 `assert a == b`？因为对两个 `ndarray`，`a == b` 返回的是**元素级布尔数组**，`assert` 一个数组会抛 `ValueError: The truth value of an array is ambiguous`。`assert_array_equal` 内部会正确地做形状检查 + 逐元素比较 + 生成可读的 diff 报错信息。

#### 4.2.4 代码实践

**实践目标**：体会 `numpy.testing` 的断言和普通 `assert` 的差别。

**操作步骤**：

```python
import numpy as np
from numpy.testing import assert_allclose, assert_array_equal, assert_raises

# 1) 浮点近似比较：直接 == 会因为浮点误差失败
a = np.array([0.1 + 0.2])
b = np.array([0.3])
print(a == b)                  # [False]，浮点误差
assert_allclose(a, b)          # 通过，默认 rtol=1e-7

# 2) 断言抛异常
assert_raises(ValueError, np.array, [1, 2, 3], dtype=np.void)

# 3) 试一试普通 assert 在数组上的表现（会抛 ValueError）
try:
    assert (np.array([1, 2]) == np.array([1, 2]))
except ValueError as e:
    print("普通 assert 失败：", e)
```

**需要观察的现象**：第 1 步 `a == b` 是 `[False]` 但 `assert_allclose` 不报错；第 3 步普通 `assert` 直接抛 `ValueError`。

**预期结果**：`assert_allclose` 通过；普通 `assert` 报 `truth value of an array is ambiguous`。这就是 `numpy.testing` 存在的核心意义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `numpy/testing/_private/utils.py` 要放在 `_private` 目录下？

> **答案**：下划线前缀是 NumPy 用来表示「内部实现细节」的约定。用户应当从 `numpy.testing` 顶层导入断言函数，而不是直接深入 `numpy.testing._private.utils`，这样 NumPy 可以自由重构内部实现而不破坏外部用法。

**练习 2**：`assert_allclose(a, b)` 的默认容差是怎么定义的？它和 `np.isclose` 是什么关系？

> **答案**：默认 `rtol=1e-7, atol=0`，判定条件是 `|a - b| <= (atol + rtol * |b|)`。`assert_allclose` 内部正是基于 `np.isclose` 的逻辑，只是再加上「形状一致」「逐元素比较」「失败时报可读 diff」这些测试断言所需的行为。

**练习 3**：`numpy.testing` 自己有没有 `test` 方法？为什么？

> **答案**：有。它的 `__init__.py` 末尾同样写了 `test = PytestTester(__name__)` 三行样板，所以可以 `np.testing.test()` 只跑 testing 模块自身的测试。

---

### 4.3 导入时 sanity check：在出错前先自检

#### 4.3.1 概念说明

NumPy 的底层计算很多依赖 BLAS/LAPACK（线性代数库）和操作系统提供的加速后端（如 macOS 的 Accelerate）。这些外部库**装错版本、ABI 不匹配、或者被多个包管理器（pip / conda / apt）混装**时，会出现一种很糟糕的情况：

> NumPy 能正常 `import`，普通计算也对，但**某些特定运算会悄悄返回错误结果**。

这种 bug 比直接崩溃难发现得多，因为它「看起来在工作」。NumPy 的对策是：**在导入末尾主动跑几个数值已知的运算，如果结果不对就立刻抛 `RuntimeError`**，把问题挡在最早。这就是 `_sanity_check`（通用自检）和 `_mac_os_check`（macOS 专用自检）的用途。

这两个函数定义在 `numpy/__init__.py` 里，并且**在导入流程的最后被立即调用**，调用完立刻 `del` 删除，不让它们留在 `np.` 命名空间。

#### 4.3.2 核心流程

`_sanity_check` 的检测原理（一个点积）：

构造 \( x = [1.0,\ 1.0] \)（`float32`），计算它的点积 \( x \cdot x \)：

\[
x \cdot x = 1\cdot 1 + 1\cdot 1 = 2.0
\]

理论上结果应当是 `2.0`。如果 BLAS 链接错误，这个点积可能返回 `4.0`、`0.0` 或乱七八糟的值。于是检查：

\[
|x \cdot x - 2.0| < 10^{-5}
\]

如果不成立，就抛 `RuntimeError`，提示「很可能是链接了错误的 BLAS，或混用了不同包管理器」。

`_mac_os_check` 的检测原理（一个最小二乘拟合）：在 macOS 上调用 `polyfit` 触发 LAPACK 的 `dgelsd`。如果系统自带的 Accelerate 后端有 bug，这一步会发出 `RankWarning`。NumPy 捕获到这个警告就抛 `RuntimeError`，提示「很可能用了有 bug 的 Accelerate 后端」。

这些检查只在一个前提下执行：**NumPy 不是在「构建自己」的过程中被导入**。`numpy/__init__.py` 开头有一个 `__NUMPY_SETUP__` 守卫：

```python
if __NUMPY_SETUP__:
    sys.stderr.write('Running from numpy source directory.\n')
else:
    ...  # 真正的初始化，包括 sanity check
```
见 [numpy/__init__.py:95-104](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L95-L104)。构建 NumPy 自身时 `__NUMPY_SETUP__` 为真，此时 C 扩展还没编译出来，自然不能也不需要做这些检查。

#### 4.3.3 源码精读

**`_sanity_check` 全文**（很短，建议整段读）：

```python
def _sanity_check():
    try:
        x = ones(2, dtype=float32)
        if not abs(x.dot(x) - float32(2.0)) < 1e-5:
            raise AssertionError
    except AssertionError:
        msg = ("The current Numpy installation ({!r}) fails to "
               "pass simple sanity checks. This can be caused for example "
               "by incorrect BLAS library being linked in, or by mixing "
               "package managers (pip, conda, apt, ...). ...")
        raise RuntimeError(msg.format(__file__)) from None

_sanity_check()
del _sanity_check
```
见 [numpy/__init__.py:786-810](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L786-L810)。注意三个细节：

1. 用 `float32` 而不是默认的 `float64`——因为很多 BLAS ABI 错误在单精度下更容易暴露。
2. 用 `abs(...) < 1e-5` 而不是 `==`，容忍极小的浮点误差。
3. `raise ... from None` 故意切掉异常链，让最终用户只看到那句友好的提示，而不是 `AssertionError`。

**`_mac_os_check` 全文**：

```python
def _mac_os_check():
    try:
        c = array([3., 2., 1.])
        x = linspace(0, 2, 5)
        y = polyval(c, x)
        _ = polyfit(x, y, 2, cov=True)
    except ValueError:
        pass

if sys.platform == "darwin":
    from . import exceptions
    with warnings.catch_warnings(record=True) as w:
        _mac_os_check()
        if len(w) > 0:
            for _wn in w:
                if _wn.category is exceptions.RankWarning:
                    ... raise RuntimeError(...)   # 提示 buggy Accelerate
del _mac_os_check
```
见 [numpy/__init__.py:812-848](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L812-L848)。注意它只在 `sys.platform == "darwin"`（macOS）下执行，并且**只关心 `RankWarning` 这一类警告**（其他警告可能无关，见注释里的 gh-25433）。

**相邻的同类检查**。紧随其后，`numpy/__init__.py` 还有一个 `blas_fpe_check`（针对 Apple Silicon 上 Accelerate 因 SME 产生虚假浮点异常的问题）和一个 `hugepage_setup`（Linux 大页内存优化）。前者见 [numpy/__init__.py:850-872](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L850-L872)。它们和 `_sanity_check` 一起，构成了 NumPy「导入时的环境自检与适配」序列：先验证结果正确，再做平台相关的微调。

#### 4.3.4 代码实践

**实践目标**：阅读 `_sanity_check` 源码，说清它检测哪类错误；并亲手复现它「正确通过」的那次计算。

**操作步骤**：

1. 打开 [numpy/__init__.py:786-810](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/__init__.py#L786-L810)，读一遍 `_sanity_check`。
2. 在 Python 里复现它的核心计算：

   ```python
   import numpy as np
   x = np.ones(2, dtype=np.float32)
   print(x.dot(x))                       # 2.0
   print(abs(x.dot(x) - np.float32(2.0)) < 1e-5)   # True
   ```

3. 回答：如果某个错误的 BLAS 让 `x.dot(x)` 算成了 `4.0`，这段代码会怎样？

**需要观察的现象 / 预期结果**：正常构建的 NumPy 上 `x.dot(x)` 应为 `2.0`，检查为 `True`。如果结果不是 `2.0`（偏差超过 `1e-5`），`_sanity_check` 会抛 `AssertionError` 并被转成 `RuntimeError`，提示信息里会点名「incorrect BLAS library being linked in, or by mixing package managers」。

**结论（回答实践任务的第二问）**：`_sanity_check` 检测的是「**数值结果错误**」类问题——具体是由**链接了 ABI 不兼容的 BLAS 库**，或**混用 pip/conda/apt 等包管理器**导致底层线性代数运算返回错误数值的情况。它用「一个结果已知为 2.0 的点积」作为探针，把这种「能跑但算错」的隐患在导入时就暴露成显式的 `RuntimeError`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_sanity_check` 用 `float32` 而不是默认的 `float64`？

> **答案**：因为很多 BLAS ABI 错误（例如把单精度接口和双精度接口搞混）在单精度 `float32` 下更容易显现异常结果；用 `float32` 能更灵敏地捕捉到这类错误。

**练习 2**：`_sanity_check` 和 `_mac_os_check` 定义完为什么紧接着 `del`？

> **答案**：它们只在导入时需要执行一次，执行完就不应再出现在 `np.` 命名空间里，也不应被用户当作公开 API 调用。`del` 把名字清理掉，保持顶层命名空间干净。

**练习 3**：如果用户在 NumPy 构建自己的过程中 `import numpy`，`_sanity_check` 会跑吗？

> **答案**：不会。此时 `__NUMPY_SETUP__` 为真，`numpy/__init__.py` 走 `if __NUMPY_SETUP__:` 分支，只打印一行 `Running from numpy source directory.` 就返回，根本不会执行 `else:` 分支里的 sanity check（那时 C 扩展还没编译好，也无法执行）。

---

## 5. 综合实践

把本讲三块内容串起来，做一个小任务：**写一个最小测试脚本，并用 `numpy.test` 风格的方式理解它的运行**。

1. 新建一个文件 `my_test.py`，写两个用 `numpy.testing` 断言的测试函数，并故意让其中一个测试函数打上「假标记」观察行为：

   ```python
   # 示例代码（非 NumPy 仓库原有代码）
   import numpy as np
   from numpy.testing import assert_allclose, assert_array_equal
   import pytest

   def test_addition():
       assert_array_equal(np.array([1, 2]) + 1, np.array([2, 3]))

   def test_float_close():
       assert_allclose(np.array([0.1 + 0.2]), np.array([0.3]))

   @pytest.mark.slow
   def test_slow_one():
       assert True
   ```

2. 用 pytest 直接跑这个文件，分别体验「全跑」和「排除 slow」两种模式：

   ```bash
   pytest my_test.py                 # 三个都跑
   pytest my_test.py -m "not slow"   # 只跑前两个（模拟 PytestTester 的 'fast'）
   ```

3. 回到本讲主线，回答三个问题（这就把三个最小模块串起来了）：
   - 第 2 步的 `-m "not slow"`，对应 `PytestTester` 里哪一段源码？（答：`label == "fast"` 分支里的 `pytest_args += ["-m", "not slow"]`。）
   - 你的 `assert_allclose` 来自 `numpy/testing/_private/utils.py`，它是怎么出现在 `numpy.testing` 下的？（答：`testing/__init__.py` 的 `from ._private.utils import *` 再导出。）
   - 假设你的 NumPy 链接了一个坏 BLAS，`import numpy` 时会在哪一步炸？（答：导入末尾的 `_sanity_check()`，因为 `ones(2).dot(ones(2))` 不等于 2.0。）

**预期结果**：`pytest my_test.py` 全过（3 passed）；`pytest my_test.py -m "not slow"` 跳过 slow 那个（2 passed, 1 deselected）。运行结果「待本地验证」。

## 6. 本讲小结

- `numpy.test()` 不是魔法：它来自一个很薄的包装类 `PytestTester`，每个子包用三行样板（`test = PytestTester(__name__)`）就能拥有自己的 `test()`，内部最终调用 `pytest.main()`，并返回「是否全部通过」的布尔值。
- `PytestTester.__call__` 的 `label` 参数只文档了 `'fast'`（排除 `slow`）和 `'full'`；传别的字符串会被当成 pytest 标记名，在 `--strict-markers` 下会报错（所以 `numpy.test('quick')` 实际不可用）。
- `numpy.testing` 是一组「为数值数组量身定做」的断言工具（`assert_allclose` / `assert_array_equal` / `assert_raises` 等），实现都在 `_private/utils.py`，再导出到 `numpy.testing` 顶层。
- 普通数组比较返回布尔数组会导致 `assert` 失败，这正是 `numpy.testing` 断言存在的核心理由。
- NumPy 在导入末尾运行 `_sanity_check`：用一个结果应为 `2.0` 的 `float32` 点积，探测「错误 BLAS / 混用包管理器」导致的**数值错误**；macOS 上额外用 `_mac_os_check` 探测有 bug 的 Accelerate 后端。
- 这些自检只在「非构建自身」时执行（由 `__NUMPY_SETUP__` 守卫），执行后立刻 `del`，不污染顶层命名空间。

## 7. 下一步学习建议

- **继续往下读子包**：下一篇进入单元 2（数组创建与数据类型）。建议先读 `numpy/_core/__init__.py`，它和本讲的 `testing/__init__.py` 是同一种「汇聚 + 再导出 + 注册 test」的套路。
- **想深入测试本身**：直接读 `numpy/testing/_private/utils.py` 里 `assert_allclose` 的实现，看它如何处理形状、NaN 和容差；这会强化你对浮点比较的理解。
- **想深入导入流程**：顺着 `numpy/__init__.py` 从第 95 行的 `__NUMPY_SETUP__` 守卫往下读，你会看到 `_distributor_init`、`__config__` 导入、`_core` 导入，再到本讲的 sanity check 序列——这是一条完整的「NumPy 启动调用链」，值得作为后面 u9（底层架构）的预习。
