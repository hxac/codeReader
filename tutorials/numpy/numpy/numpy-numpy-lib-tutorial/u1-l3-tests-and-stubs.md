# 测试入口与类型存根（.pyi）

## 1. 本讲目标

前两讲我们建立了 numpy.lib 的「骨架认知」：知道它是杂项函数库，知道公开函数藏在私有 `_xxx_impl.py`、再由薄模块或顶层 `numpy/__init__.py` 往上暴露。这一讲我们把目光从「功能代码」转向「支撑代码」，回答三个问题：

1. 我怎么一键运行 numpy.lib 的所有测试？—— **`PytestTester`**。
2. 测试代码放在哪里、按什么约定组织？—— **`tests/` 目录**。
3. 我没用过这些函数，怎么在运行之前就知道它们的参数和返回类型？—— **`.pyi` 类型存根**。

学完后你应该能够：

- 用 `numpy.lib.test(...)` 运行整个子包或单个测试模块，并能区分 `label` 与 `tests` 两个参数的不同含义。
- 看懂 `test_xxx.py` 测试文件与 `_xxx_impl.py` 实现文件的一一对应关系。
- 读懂 `.pyi` 存根文件，并理解它和 `.py` 文件为什么会有差异。

## 2. 前置知识

- **pytest**：Python 最常用的测试框架。测试函数以 `test_` 开头，断言用 `assert`，运行后给出「通过 / 失败 / 跳过」的统计。
- **测试包（test package）**：一个目录只要包含 `__init__.py`，就被 Python 当作「包」，里面的 `test_xxx.py` 可以被 pytest 自动发现。
- **类型注解（type annotation）与存根文件（stub）**：Python 是动态类型语言，但可以用 `x: int` 这样的注解描述类型。`.pyi` 文件只写注解、不写实现，专门给静态类型检查器（如 mypy、pyright）和 IDE 看。运行时 Python **不会**执行 `.pyi`。
- **PEP 562 模块级 `__getattr__`**：允许在模块上动态拦截属性访问。u1-l2 已讲过它被用来给 NumPy 2.0 移除的别名抛出迁移指引；本讲会看到 `.pyi` 里也借助这一点「对外声明所有子模块都可访问」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `numpy/lib/__init__.py` | lib 子包总入口 | 第 56–59 行挂载 `test = PytestTester(__name__)`，是测试入口的「装配点」 |
| `numpy/_pytesttester.py` | 定义 `PytestTester` 类（在 lib 之外、numpy 顶层） | `test()` 真正干活的实现，理解参数语义必读 |
| `numpy/lib/tests/__init__.py` | 空文件 | 仅把 `tests/` 变成一个 Python 包，本身没有代码 |
| `numpy/lib/tests/test_ufunclike.py` | 测 `_ufunclike_impl`（`fix`/`isposinf`/`isneginf`） | 本讲代码实践的运行对象 |
| `numpy/lib/_version.pyi` | `NumpyVersion` 的类型存根 | 最小、最完整的存根示例 |
| `numpy/lib/__init__.pyi` | lib 子包的类型存根 | 展示存根如何「再声明」公开 API |

> 说明：`PytestTester` 不在 `numpy/lib/` 内，而在 `numpy/_pytesttester.py`。它的设计注释里写明「这个模块被每个 numpy 子包导入，所以放在顶层以避免循环导入」。本讲会越过 lib 的边界引用它，因为这是理解 `test()` 行为的唯一途径。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**PytestTester**、**tests/ 目录**、**.pyi 类型存根**。

### 4.1 测试入口：PytestTester

#### 4.1.1 概念说明

很多大型库都会在每个子包里挂一个 `test()` 函数，让用户写 `numpy.lib.test()` 就能跑测试，而不必记住 `pytest numpy/lib --pyargs` 这种命令行。

NumPy 把这件事抽成了一个可复用的类 `PytestTester`：你把「要测哪个包」告诉它，它就帮你拼好 pytest 命令行、调用 `pytest.main(...)`、最后返回一个布尔值表示成功还是失败。关键在于——**`test` 不是写死的函数，而是「一个被实例化的对象」**，调用 `numpy.lib.test(...)` 实际上是调用这个对象的 `__call__` 方法。

#### 4.1.2 核心流程

`PytestTester` 的工作可以概括为三步：

```
实例化  : PytestTester("numpy.lib")        # 记住要测的包名
调用    : test(label=..., tests=...)       # 即 __call__
   ├─ 1. 找到包对应的磁盘路径
   ├─ 2. 拼装 pytest 参数列表 pytest_args
   │      - 默认 -q（安静）
   │      - label='fast'  → 加 "-m not slow"（跳过慢测试）
   │      - label='full'  → 不加过滤
   │      - label=其它    → 加 "-m <label>"（按标记过滤）
   │      - tests=None    → 测整个包（--pyargs numpy.lib）
   │      - tests=xxx     → 只测指定模块/路径
   ├─ 3. _show_numpy_info() 打印版本与 CPU 特性
   └─ 4. code = pytest.main(pytest_args); return code == 0
```

一个极易踩的坑（也是本讲实践的重点）：**`label` 参数是用来「按 pytest 标记筛选测试」的，不是用来「指定测试文件名」的**。把一个文件名塞给 `label`，pytest 会去匹配一个同名的标记，匹配不到就「一个测试都不选」，看起来「全部通过」其实是「啥也没跑」。

#### 4.1.3 源码精读

**① 在 lib 包里把 `test` 装配出来（3 行三步走）**

[numpy/lib/\_\_init\_\_.py:L56-L59](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L56-L59)

```python
from numpy._pytesttester import PytestTester

test = PytestTester(__name__)
del PytestTester
```

这三行做的事情：从顶层把 `PytestTester` 类借进来；用 `__name__`（即字符串 `"numpy.lib"`）实例化一个对象赋给名字 `test`；然后 `del` 掉 `PytestTester` 这个名字，让它不污染 lib 的命名空间。注意 `__name__` 是模块名字符串，不是模块对象——这正是 `PytestTester.__init__` 要的参数。

**② `PytestTester` 类的定义与「包名」记忆**

[numpy/\_pytesttester.py:L45-L77](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L45-L77)

```python
class PytestTester:
    def __init__(self, module_name):
        self.module_name = module_name
        self.__module__ = module_name
```

类只是把 `module_name` 存起来。注释里特别强调：和旧的 nose 实现不同，**这个类不对外公开**（`del PytestTester`），因为它做了一些 NumPy 专属的告警抑制，不适合当通用 API。

**③ `__call__` 的签名——读懂参数语义的关键**

[numpy/\_pytesttester.py:L79-L86](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L79-L86)

```python
def __call__(self, label='fast', verbose=1, extra_argv=None,
             doctests=False, coverage=False, durations=-1, tests=None):
```

注意第一个位置参数是 `label`（默认 `'fast'`），而**真正指定「测哪些文件」的是最后一个关键字参数 `tests`**。这正是 `numpy.lib.test('test_ufunclike')` 容易误用的根源：字符串被 `label` 接走了，而不是 `tests`。

**④ `label` 与 `tests` 是如何分别影响命令行的**

[numpy/\_pytesttester.py:L164-L176](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L164-L176)

```python
if label == "fast":
    pytest_args += ["-m", "not slow"]
elif label != "full":
    pytest_args += ["-m", label]

if durations >= 0:
    pytest_args += [f"--durations={durations}"]

if tests is None:
    tests = [self.module_name]

pytest_args += ["--pyargs"] + list(tests)
```

- `label='fast'` → `-m "not slow"`：跳过带 `@pytest.mark.slow` 的测试。
- `label='full'` → 不加 `-m`：全跑。
- `label='test_ufunclike'`（既不是 fast 也不是 full）→ `-m test_ufunclike`：pytest 把它当成「标记名」筛选，几乎没有测试带这个标记 → **全部被 deselect**。
- `tests=None` → 用 `self.module_name`（即 `numpy.lib`），配合 `--pyargs` 让 pytest 按包名定位。
- 想只测一个模块，要靠 `tests=`，例如 `tests='numpy.lib.tests.test_ufunclike'`。

**⑤ 跑测试、返回布尔结果**

[numpy/\_pytesttester.py:L179-L186](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L179-L186)

```python
_show_numpy_info()

try:
    code = pytest.main(pytest_args)
except SystemExit as exc:
    code = exc.code

return code == 0
```

`pytest.main` 返回退出码：`0` 表示全部通过。函数把它归一化成 `True/False` 返回，方便脚本里写 `if numpy.lib.test(): ...`。`SystemExit` 的捕获是为了应付某些 pytest 插件直接 `sys.exit` 的情况。

**⑥ 跑测试前先打印环境信息**

[numpy/\_pytesttester.py:L37-L42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_pytesttester.py#L37-L42)

```python
def _show_numpy_info():
    import numpy as np
    print(f"NumPy version {np.__version__}")
    info = np.lib._utils_impl._opt_info()
    print("NumPy CPU features: ", (info or 'nothing enabled'))
```

这就是你运行 `numpy.lib.test()` 时最先看到「NumPy version ... / NumPy CPU features: ...」两行的来源——它甚至反向引用了 `numpy.lib._utils_impl._opt_info`（u2-l2 会专门讲 `_utils_impl`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`numpy.lib.test('test_ufunclike')` 其实没跑那个模块」，并找到正确写法。

**操作步骤**：

1. 准备一个装好 numpy 与 pytest 的环境。
2. 新建 `run_lib_tests.py`，内容如下：

   ```python
   # 示例代码：探究 numpy.lib.test 的参数语义
   import numpy as np

   # (A) 把文件名误传给 label
   r1 = np.lib.test('test_ufunclike', verbose=1)
   print("(A) label='test_ufunclike' ->", r1)

   # (B) 正确写法：用 tests 指定模块
   r2 = np.lib.test(tests='numpy.lib.tests.test_ufunclike')
   print("(B) tests='numpy.lib.tests.test_ufunclike' ->", r2)
   ```

3. 运行：`python run_lib_tests.py`

**需要观察的现象**：

- 第 (A) 段，pytest 输出里几乎没有 `test_isposinf`、`test_fix` 之类的用例名，而是出现类似 `deselected` / `no tests ran` 的字样（因为 `-m test_ufunclike` 找不到对应标记）。`_show_numpy_info` 打印的版本与 CPU 特性行仍然出现。
- 第 (B) 段，能看到 `TestUfunclike` 类里的若干测试被收集并执行。

**预期结果**：

- (A) 的返回值是 `True`（退出码 0，因为「没有测试失败」——本质上是一个都没跑），这正是一个**误导性的「通过」**。
- (B) 的返回值是 `True`，且确实跑了 `test_ufunclike.py` 里的用例（`test_isposinf`、`test_isneginf`、`test_fix` 等）。

> 待本地验证：若你的环境中 `numpy.lib.tests.test_ufunclike` 因安装方式（如 `--pyargs` 解析）不可达，可改用文件路径：`np.lib.test(tests='/绝对路径/numpy/lib/tests/test_ufunclike.py')`。是否可行取决于 numpy 的安装形态（源码树 vs wheel）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `numpy.lib.test()`（不带参数）能跑完整个 lib 的测试？请结合 `__call__` 的默认值与第 173–176 行回答。

> **答案**：不带参数时 `label='fast'`、`tests=None`；`tests is None` 分支把 `tests` 设为 `[self.module_name]`，即 `['numpy.lib']`，再加 `--pyargs`，于是 pytest 把整个 `numpy.lib` 包（含所有子测试模块）作为收集根。

**练习 2**：你想跳过所有「慢测试」，应该怎么调用？

> **答案**：保持默认 `label='fast'` 即可（它等价于 `-m "not slow"`）。想连同慢测试一起跑，才用 `label='full'`。

**练习 3**：`numpy.lib.test(...)` 的返回值是退出码 `code` 本身，还是 `code == 0`？

> **答案**：是 `code == 0`，即一个布尔值。`pytest.main` 返回 0 表示全部通过，这里把它归一化成 `True`，方便在脚本里直接做条件判断。

---

### 4.2 tests/ 目录与测试约定

#### 4.2.1 概念说明

`numpy/lib/tests/` 是 lib 子包的测试仓库。它最特别的一点是：**`tests/__init__.py` 是一个空文件**。空文件不是「忘了写」，而是有意为之——它只承担一个职责：让 `tests/` 成为 Python 包，从而被 pytest 发现、被 `PytestTester` 用 `--pyargs numpy.lib` 收集到。真正的测试逻辑全部在各个 `test_xxx.py` 里。

#### 4.2.2 核心流程

lib 的测试约定可以归纳为三条规律：

```
规律 1（命名）  : 测试文件以 test_ 开头；pytest 自动发现 test_ 开头的函数和类。
规律 2（对应）  : 几乎每个 test_xxx.py 都对应一个实现文件：
                 test_ufunclike.py  ↔ _ufunclike_impl.py
                 test_arraypad.py   ↔ _arraypad_impl.py
                 test_function_base ↔ _function_base_impl.py
                 test__version.py   ↔ _version.py
                 ... (共约 26 个 test_ 文件)
规律 3（断言）  : 测试用 numpy.testing 提供的 assert_* 系列断言，
                 它们对数组形状/数值做「逐元素 + 容差」比较。
```

#### 4.2.3 源码精读

**① 空的 `tests/__init__.py`**

[numpy/lib/tests/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/__init__.py)

这个文件 0 行、没有内容。它存在的唯一意义是把 `tests/` 标记为「包」。因为父包 `numpy.lib` 在 `__init__.py` 里 `from . import (...)` 一长串子模块时**并没有**导入 `tests`，所以 `tests` 是一个「独立、懒加载」的子包：只有当 pytest 或用户显式去访问它时才会被加载。这也是为什么日常 `import numpy` 不会把测试代码也一起执行。

**② 一个典型测试文件长什么样**

[numpy/lib/tests/test_ufunclike.py:L1-L6](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_ufunclike.py#L1-L6)

```python
import pytest

import numpy as np
from numpy import fix, isneginf, isposinf
from numpy.testing import assert_, assert_array_equal, assert_equal, assert_raises
```

注意它**从顶层 `numpy` 取被测对象**（`from numpy import fix, isneginf, isposinf`），而不是 `from numpy.lib._ufunclike_impl import ...`。这呼应了 u1-l2 讲过的「实现藏在 `_impl`、对外只露顶层」：测试针对的是公开 API，而非私有实现。

**③ 一个测试用例的内部结构**

[numpy/lib/tests/test_ufunclike.py:L8-L23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_ufunclike.py#L8-L23)

```python
class TestUfunclike:

    def test_isposinf(self):
        a = np.array([np.inf, -np.inf, np.nan, 0.0, 3.0, -3.0])
        out = np.zeros(a.shape, bool)
        tgt = np.array([True, False, False, False, False, False])

        res = isposinf(a)
        assert_equal(res, tgt)
        res = isposinf(a, out)
        assert_equal(res, tgt)
        assert_equal(out, tgt)

        a = a.astype(np.complex128)
        with assert_raises(TypeError):
            isposinf(a)
```

这段展示了 NumPy 测试的典型套路：

- 把输入 `a`、期望输出 `tgt`、可选的 `out` 缓冲都准备好；
- 先测「返回值正确」：`assert_equal(res, tgt)`；
- 再测「`out=` 参数能就地写出」：调用后再 `assert_equal(out, tgt)`；
- 最后测「异常分支」：复数类型传给 `isposinf` 应抛 `TypeError`，用 `assert_raises` 捕获。

**④ 测试与实现的对照表**

下列对应关系是阅读 lib 源码的「索引」，遇到 `_impl` 想验证行为，直接去同名 `test_` 文件：

| 测试文件 | 被测实现 |
| --- | --- |
| `test_ufunclike.py` | `_ufunclike_impl.py`（`fix`/`isposinf`/`isneginf`） |
| `test__version.py` | `_version.py`（`NumpyVersion`） |
| `test_arraypad.py` | `_arraypad_impl.py`（`pad`） |
| `test_function_base.py` | `_function_base_impl.py`（`diff`/`gradient`/`select`/...） |
| `test_format.py` | `_format_impl.py`（`.npy` 格式） |
| `test_io.py` / `test_loadtxt.py` | `_npyio_impl.py`（`load`/`save`/`loadtxt`/...） |

#### 4.2.4 代码实践

**实践目标**：用「断言」反推被测函数的行为，而不是靠记忆。

**操作步骤**：

1. 打开 `numpy/lib/tests/test_ufunclike.py`，定位 `test_isposinf`（第 10–23 行）。
2. 不查文档，只读断言，回答：`isposinf` 对 `np.nan` 返回什么？对 `out=` 参数有什么契约？复数输入会怎样？
3. 写一段最小脚本验证你的推断：

   ```python
   # 示例代码：根据断言理解 isposinf
   import numpy as np
   from numpy import isposinf

   a = np.array([np.inf, -np.inf, np.nan, 0.0])
   print("isposinf(a) =", isposinf(a))   # 推断：[True, False, False, False]
   ```

**需要观察的现象**：`np.nan` 既不是正无穷也不是负无穷，断言里它落在 `False` 的位置；`out=` 必须是布尔数组。

**预期结果**：输出 `[ True False False False]`，与 `test_isposinf` 里 `tgt` 一致。

#### 4.2.5 小练习与答案

**练习 1**：`tests/__init__.py` 为什么是空的？删掉它会怎样？

> **答案**：它只用于把 `tests/` 标记为包。删掉后，在较老的导入机制下 `tests` 可能不再被识别为子包，`--pyargs numpy.lib` 收集测试时可能漏掉它；保留空文件是最稳妥的「占位」做法。

**练习 2**：`test_ufunclike.py` 为什么用 `from numpy import fix`，而不是 `from numpy.lib._ufunclike_impl import fix`？

> **答案**：测试要钉住「公开 API 的行为」。如果哪天实现被搬到别的 `_impl`，只要顶层 `numpy.fix` 仍可用，测试就不必改；这也避免了测试依赖私有路径。

**练习 3**：`assert_raises(TypeError)` 这一句在测试什么？

> **答案**：它断言「把复数数组传给 `isposinf` 会抛 `TypeError`」，即「正无穷」这个概念对复数无定义，函数显式拒绝而非静默返回。

---

### 4.3 .pyi 类型存根

#### 4.3.1 概念说明

`.pyi` 文件叫**存根文件（stub）**，里面只有类型签名，没有函数体（函数体用 `...` 占位）。它服务于静态类型检查器（mypy、pyright）和 IDE 的自动补全，**运行时 Python 根本不会执行它**。

一个 `.py` 文件可以配一个同名 `.pyi`：当类型检查器分析 `import` 时，优先读 `.pyi`。这样做的好处是——**类型描述可以和实现分离**：实现里可能有复杂的分发逻辑（比如 u1-l2 讲的 `array_function_dispatch`），而存根只需给出「最终对外暴露的干净签名」。

#### 4.3.2 核心流程

存根的阅读规则：

```
规则 1 : 函数体一律是 ...（占位），不要在里面找实现。
规则 2 : 变量标注为 Final 表示「终值/不可变」，类型检查器会据此禁止重新赋值。
规则 3 : str | NumpyVersion 是 PEP 604 的「联合类型」写法，等价于 Union[str, NumpyVersion]。
规则 4 : 参数列表里的 / 表示「它左边的参数只能按位置传，不能用关键字」（PEP 570）。
规则 5 : 存根可以和 .py 不完全一致——它面向「对外契约」，可更宽松或更精确。
```

#### 4.3.3 源码精读

**① 最小存根：`_version.pyi`**

[numpy/lib/\_version.pyi:L1-L22](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.pyi#L1-L22)

```python
from typing import Final

__all__ = ["NumpyVersion"]

class NumpyVersion:
    __module__ = "numpy.lib"

    vstring: Final[str]
    version: Final[str]
    major: Final[int]
    minor: Final[int]
    bugfix: Final[int]
    pre_release: Final[str]
    is_devversion: Final[bool]

    def __init__(self, /, vstring: str) -> None: ...
    def __lt__(self, other: str | NumpyVersion, /) -> bool: ...
    def __le__(self, other: str | NumpyVersion, /) -> bool: ...
    def __eq__(self, other: str | NumpyVersion, /) -> bool: ...  # type: ignore[override]
    ...
```

把它和 [`_version.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L13-L49) 里的 `class NumpyVersion` 对比，可以看出存根「只说是什么，不说怎么做」：

- `vstring: Final[str]` 告诉类型检查器：实例上的 `vstring` 是字符串且初始化后不再变。
- `__init__(self, /, vstring: str)` 里的 `/` 表示 `vstring` 必须按位置传（只能写 `NumpyVersion("1.8.0")`，不能写 `NumpyVersion(vstring="1.8.0")`）。
- `other: str | NumpyVersion` 表示 `__lt__` 等比较运算既能和字符串比，也能和另一个 `NumpyVersion` 比——这正是 u2-l1 会讲到的「版本可与字符串直接比较」的类型依据。
- `# type: ignore[override]` 是给 mypy 的提示：这里故意「放宽」了 `__eq__` 的参数类型（基类 `object.__eq__` 只接受任意对象），属于已知、可接受的覆盖。

**② 子包级存根：`__init__.pyi` 如何「再声明」对外契约**

[numpy/lib/\_\_init\_\_.pyi:L1-L52](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.pyi#L1-L52)

这个文件比 `_version.pyi` 更值得品味，因为它和 `__init__.py` 有几处**有意为之的差异**。

**差异一：导入路径不同。**

[numpy/lib/\_\_init\_\_.pyi:L1-L2](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.pyi#L1-L2)

```python
from numpy._core.function_base import add_newdoc
from numpy._core.multiarray import add_docstring, tracemalloc_domain
```

而运行时的 [`__init__.py` 第 13–14 行](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L13-L14) 写的是：

```python
from numpy._core._multiarray_umath import add_docstring, tracemalloc_domain
from numpy._core.function_base import add_newdoc
```

`_multiarray_umath` 是 C 扩展模块，本身没有 `.pyi`；存根选择从「有类型存根的 `numpy._core.multiarray`」取同名对象，让类型检查器能解析到签名。这正是「存根可偏离实现、只为类型可见性服务」的实例。

**差异二：显式再导出全部私有 `_impl` 子模块。**

[numpy/lib/\_\_init\_\_.pyi:L4-L35](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.pyi#L4-L35) 顶部注释写道：

```python
# all submodules of `lib` are accessible at runtime through `__getattr__`,
# so we implicitly re-export them here
from . import (
    _array_utils_impl as _array_utils_impl,
    _arraypad_impl as _arraypad_impl,
    ...
)
```

这把 u1-l2 讲的 `__getattr__` 机制「翻译」给了类型检查器：运行时这些 `_impl` 是经模块级 `__getattr__` 动态暴露的，类型检查器看不到动态属性，所以存根用静态的 `from . import ...` 把它们「显式再声明」一遍。每个名字都用 `as _array_utils_impl` 这种写法，是为了让它们不进入存根的「公开再导出」（不会被 `*` 导出），但又能被 `numpy.lib._arraypad_impl` 这种写法解析到。

**差异三：`__all__` 与运行时一致。**

[numpy/lib/\_\_init\_\_.pyi:L39-L52](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.pyi#L39-L52) 的 `__all__` 与 [`__init__.py` 的 `__all__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L48-L52) 内容相同（12 个名字），但去掉了运行时的 `test = PytestTester(...)`——因为类型检查器不需要、也不应把「测试入口」当成 lib 的公开类型契约。

#### 4.3.4 代码实践

**实践目标**：体会「存根不参与运行」，并对比存根与实现的差异。

**操作步骤**：

1. 用 Python 解释器验证 `.pyi` 在运行时不被执行：

   ```python
   # 示例代码：证明 .pyi 不参与运行
   import numpy.lib._version as v
   # 看实现里 NumpyVersion 的真实源码行数
   print("源文件:", v.__file__)
   ```

   预期 `__file__` 指向 `_version.py`（而非 `.pyi`），说明运行时加载的是实现。

2. （可选，需安装 mypy 或 pyright）写一个故意违反存根的小脚本，看类型检查器如何报错：

   ```python
   # 示例代码：故意违反类型契约（仅用于类型检查，不必运行）
   from numpy.lib import NumpyVersion
   NumpyVersion("1.8.0", "extra")   # 多了一个参数
   ```

   静态检查应提示「参数个数不符」，但 `python` 直接运行反而会先在 `__init__` 里抛 `TypeError`（因为实现只接受一个参数）。

**需要观察的现象**：`v.__file__` 指向 `.py`；`.pyi` 只在静态检查阶段被读取。

**预期结果**：运行时永远走 `.py`；存根只影响 mypy/IDE 的提示。

> 待本地验证：若未安装 mypy/pyright，第 2 步的静态报错无法复现，可改为在 IDE 中悬停 `NumpyVersion(` 观察参数提示。

#### 4.3.5 小练习与答案

**练习 1**：`.pyi` 文件里的函数体为什么是 `...`？

> **答案**：存根只声明签名、不提供实现；`...` 是 Python 语法上合法的「占位体」（pass 的等价物），让文件能被解析，却不承担任何运行逻辑。

**练习 2**：为什么 `__init__.pyi` 里要从 `numpy._core.multiarray` 导入 `add_docstring`，而 `__init__.py` 却从 `numpy._core._multiarray_umath` 导入？

> **答案**：`_multiarray_umath` 是 C 扩展、无配套 `.pyi`，类型检查器无法解析；`multiarray` 有存根。两者运行时指向同一组对象，但存根选择「类型可见」的路径。

**练习 3**：`vstring: Final[str]` 中的 `Final` 对使用者意味着什么？

> **答案**：它表示 `vstring` 是「初始化后不再改变」的终值属性。类型检查器会拒绝 `nv.vstring = "2.0"` 这样的赋值，从而把「版本字符串不可变」这一约定变成可静态检查的规则。

## 5. 综合实践

把三个模块串起来，完成一次「从测试入口到实现、再到类型契约」的完整溯源。

任务：选定 `numpy.lib` 里的 `NumpyVersion`，完成下面四步并记录每一步的依据。

1. **看存根**：打开 [`numpy/lib/_version.pyi`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.pyi#L1-L22)，仅凭签名说出：`NumpyVersion` 的构造参数怎么传？它有哪些只读属性？比较运算能和什么类型比？
2. **跑测试**：用**正确的 `tests=` 写法**运行它的测试模块：

   ```python
   # 示例代码：综合实践第 2 步
   import numpy as np
   ok = np.lib.test(tests='numpy.lib.tests.test__version')
   print("测试通过:", ok)
   ```

   观察输出里的版本号、CPU 特性行（来自 `_show_numpy_info`），以及测试用例数。
3. **对照实现**：打开 [`numpy/lib/_version.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_version.py#L13-L49) 与 [`numpy/lib/tests/test__version.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test__version.py#L1-L8)，挑一个测试函数（如 `test_main_versions`），说明它断言了 `NumpyVersion` 的哪条行为。
4. **解释差异**：回到第 1 步的存根，指出至少一处「存根比实现更干净 / 与实现路径不同」的地方（提示：`Final`、`/`、`str | NumpyVersion`）。

**预期产出**：一段 200 字左右的溯源笔记，能清楚区分「存根描述的契约」「测试钉住的行为」「实现的真实代码」三者。

> 待本地验证：第 2 步的测试模块名与可达性取决于 numpy 安装形态；若 `--pyargs` 不可用，可退化为直接 `pytest numpy/lib/tests/test__version.py`。

## 6. 本讲小结

- `numpy.lib.test` 不是普通函数，而是一个 `PytestTester` **实例**：[`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L56-L59) 用 `PytestTester(__name__)` 装配它，再 `del PytestTester`。
- 调用 `test(label='fast', ..., tests=None)` 的第一个位置参数是**标记筛选** `label`，而**指定测试目标**靠末尾的 `tests=`；把文件名误传给 `label` 会得到「啥也没跑却返回 True」的误导性结果。
- `tests/` 目录的 `__init__.py` 是**空文件**，只把目录变成包；真正的测试按 `test_xxx.py ↔ _xxx_impl.py` 一一对应，并从顶层 `numpy` 取被测对象以钉住公开 API。
- `.pyi` 存根**不参与运行**，只供类型检查器与 IDE；它可与 `.py` 有意不同，例如 `__init__.pyi` 从 `numpy._core.multiarray`（而非 C 扩展 `_multiarray_umath`）取对象，并用 `from . import ...` 把动态 `__getattr__` 暴露的子模块静态化。
- `_version.pyi` 展示了现代类型注解三件套：`Final`（终值）、`/`（仅位置参数）、`str | NumpyVersion`（联合类型）。

## 7. 下一步学习建议

- 想深入「测试与断言」本身，建议阅读 `numpy/testing/`（`assert_equal`/`assert_array_equal`/`assert_allclose` 的实现），本讲的 `assert_*` 都来自那里。
- 想理解 `NumpyVersion` 的真实比较算法，进入 u2-l1「NumpyVersion 版本字符串比较」，它会逐段拆解 `_version.py` 的正则与 `_compare_*` 系列。
- 想继续看「支撑设施」，u2-l2 会讲 `PytestTester` 反向引用过的 `_utils_impl._opt_info` / `info` / `get_include` 等运行期信息工具。
- 若对类型存根感兴趣，可对比 `numpy/lib/__init__.pyi` 与 `numpy/_core/multiarray.pyi`，体会「C 扩展 + 存根」这一大型项目的类型工程做法。
