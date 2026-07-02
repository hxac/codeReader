# 目录结构、构建与包导出

## 1. 本讲目标

上一篇（u1-l1）我们俯瞰了 `scipy.constants` 的「三大能力」，并知道它的公开符号都来自以 `_` 开头的私有模块。本讲继续往下挖一层，回答四个工程层面的问题：

1. 这个子包到底由哪几个文件组成，各管什么事？
2. 这些文件是怎么被构建系统「打包安装」到用户机器上的？
3. `__init__.py` 用了什么技巧，把私有模块里的几百个名字统一提升成顶层公开 API，并且让 `__all__`、文档表格「自动生成」？
4. 那两个不带下划线、却又标注「将在 v2.0.0 移除」的 `codata.py` / `constants.py` 是做什么用的？

学完本讲，你应该能：看懂任何一个 SciPy 子包的 `meson.build` 与 `__init__.py`，理解 `import *`、动态 `__all__`、文档模板替换、弃用垫片这几套在 SciPy 里反复出现的惯用法。

## 2. 前置知识

- **Python 包（package）与 `__init__.py`**：一个目录里放了 `__init__.py`，Python 就把它当作一个包；`__init__.py` 在 `import 包名` 时第一个被执行，相当于这个包的「门面」。
- **`from 模块 import *`**：把目标模块里所有「公开」（在 `__all__` 里，或不下划线开头）的名字搬到当前命名空间。这是 SciPy 把私有实现文件（`_xxx.py`）里的符号「提升」到顶层包的常用手段。
- **`__all__`**：一个字符串列表，声明「`from 包 import *` 时要导出哪些名字」。它也常被 IDE、文档工具、类型检查器当作「公开 API 清单」。
- **Meson 构建系统**：SciPy 用来替代旧版 `setup.py` 的构建工具。构建逻辑写在 `meson.build` 文件里，语法是一种小语言（不是 Python）。我们只需读懂，不需要会写。
- **PEP 562（模块级 `__getattr__`）**：Python 3.7+ 允许在模块里定义 `__getattr__(name)`，当访问模块上「不存在的属性」时被调用。SciPy 用它实现「惰性弃用重定向」。

> 如果你对这些概念还陌生，先记住一句话即可：**`__init__.py` 是门面，`meson.build` 是施工图，`__getattr__` 是「找不到东西时的兜底」。** 细节会在下面边读源码边讲。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `scipy/constants/` 目录下：

| 文件 | 角色 | 本讲解读重点 |
|---|---|---|
| `meson.build` | 子包的构建脚本 | 哪些源文件被安装、安装到哪个子目录 |
| `__init__.py` | 子包门面 / 公开 API 聚合 | `import *`、`__doc__` 模板替换、动态 `__all__` |
| `_codata.py` | CODATA 物理常数数据库（私有实现） | 本讲只看它「被谁导入」 |
| `_constants.py` | 数学常数 / 前缀 / 单位换算（私有实现） | 本讲只看它「被谁导入」 |
| `codata.py` | 弃用垫片（重定向到 `_codata`） | 惰性弃用机制 |
| `constants.py` | 弃用垫片（重定向到 `_constants`） | 惰性弃用机制 |
| `tests/meson.build` | 测试的构建脚本 | `install_tag: 'tests'` 的含义 |

本讲**不深入** `_codata.py` / `_constants.py` 的内部实现（那是 u1-l3、u1-l4 和整个 u2 的任务），只关注它们「如何被发现、被安装、被导出」。

## 4. 核心概念与源码讲解

### 4.1 构建脚本 meson.build：子包如何被注册与安装

#### 4.1.1 概念说明

SciPy 用 Meson 构建。构建配置分散在每个目录的 `meson.build` 里，由父目录通过 `subdir('子目录名')` 串起来，形成一棵和源码目录一一对应的「构建树」。`scipy.constants` 子包要被安装到用户机器上，必须满足两件事：

1. **被父构建发现**：父级 `scipy/meson.build` 里有一行 `subdir('constants')`，把本子包挂进构建树。
2. **声明要安装哪些文件**：本子包的 `meson.build` 用 `py3.install_sources(...)` 明确列出要复制到安装目录的 Python 文件。

这里出现的 `py3`，是 Meson 的 `python` 模块提供的「Python 安装辅助对象」，在项目顶层 `meson.build` 中通过 `import('python')` 创建，所有子包共享它。

#### 4.1.2 核心流程

子包从「源码」到「用户 site-packages」的路径可以画成：

```text
顶层 meson.build
        │  import('python')  →  得到 py3 安装辅助对象
        ▼
scipy/meson.build
        │  subdir('constants')            ← 把 constants 挂进构建树
        ▼
scipy/constants/meson.build
        │  ① 定义 python_sources 列表（5 个 .py）
        │  ② py3.install_sources(python_sources, subdir: 'scipy/constants')
        │       → 复制到 <site-packages>/scipy/constants/
        │  ③ subdir('tests')
        ▼
scipy/constants/tests/meson.build
        │  安装测试文件，带 install_tag: 'tests'
        ▼
安装完成：用户 import scipy.constants 即可使用
```

要点：**构建树和源码树同构**，`subdir` 一层一层往下挂；**只有显式 `install_sources` 的文件才会进安装包**，没列进去的文件（比如临时脚本）不会污染用户的 site-packages。

#### 4.1.3 源码精读

先看子包自己的构建脚本，只有 15 行：

[scipy/constants/meson.build:1-7](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/meson.build#L1-L7)

```meson
python_sources = [
  '__init__.py',
  '_codata.py',
  '_constants.py',
  'codata.py',
  'constants.py'
]
```

> 这段定义了一个 Meson 变量 `python_sources`，列出本子包全部 5 个要安装的 Python 文件。注意它**不带 `tests/`**——测试由下一行的 `subdir('tests')` 单独处理。

接着是真正的安装动作：

[scipy/constants/meson.build:10-13](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/meson.build#L10-L13)

```meson
py3.install_sources(
  python_sources,
  subdir: 'scipy/constants'
)
```

> `py3.install_sources` 把上面 5 个文件复制到安装根目录下的 `scipy/constants/` 子目录。`subdir:` 参数决定了文件最终落在 `<site-packages>/scipy/constants/`，从而能被 `import scipy.constants` 找到。

最后一行把测试子目录也挂进构建：

[scipy/constants/meson.build:15](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/meson.build#L15)

```meson
subdir('tests')
```

而本子包又是被父级挂进去的，关键的一行在父级构建脚本：

[scipy/meson.build:731](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/meson.build#L731)

```meson
subdir('constants')
```

> 父级 `scipy/meson.build` 在第 720–739 行用一连串 `subdir(...)` 把所有子包（linalg、special、stats、fft、optimize……）依次挂进构建树，`constants` 在第 731 行。如果删掉这一行，整个 `scipy.constants` 子包就不会被构建、不会被安装。

测试子目录的构建脚本则多了一个 `install_tag`：

[scipy/constants/tests/meson.build:7-11](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/tests/meson.build#L7-L11)

```meson
py3.install_sources(
  python_sources,
  subdir: 'scipy/constants/tests',
  install_tag: 'tests'
)
```

> `install_tag: 'tests'` 是一个标签，让打包工具（如 pip、wheel 构建器）能够把测试文件单独归类。这样发行版可以选择「只安装运行所需、不装测试」，缩小安装体积。

#### 4.1.4 代码实践

**实践目标**：确认本子包的 5 个源文件在安装后确实出现在 site-packages 的对应路径下。

**操作步骤**：

1. 在装好 SciPy 的环境里，找到 `scipy.constants` 的安装路径。
2. 列出该目录下的 `.py` 文件，与本节 `python_sources` 列表对照。

**参考代码**（示例代码）：

```python
import os
import scipy.constants as C

pkg_dir = os.path.dirname(C.__file__)
print("包目录:", pkg_dir)

py_files = sorted(f for f in os.listdir(pkg_dir) if f.endswith('.py'))
print("安装的 .py 文件:")
for f in py_files:
    print("  ", f)
```

**需要观察的现象**：输出里应该能看到 `__init__.py`、`_codata.py`、`_constants.py`、`codata.py`、`constants.py` 这 5 个文件，与 `meson.build` 里 `python_sources` 列表一致（可能还多出编译产物 `__pycache__` 等，属正常）。

**预期结果**：5 个源文件一一对应。如果某个文件缺失，说明构建/安装异常。

**无法在本地运行时**：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `scipy/meson.build` 里的 `subdir('constants')` 删掉，重新安装 SciPy，`import scipy.constants` 会发生什么？

**参考答案**：会抛 `ModuleNotFoundError`。因为该子包根本没被构建、没被安装进 site-packages，Python 找不到 `scipy/constants/__init__.py`。

**练习 2**：`tests/meson.build` 里的 `install_tag: 'tests'` 对普通用户 `import scipy.constants` 有影响吗？

**参考答案**：没有。`install_tag` 只影响「打包/安装时是否包含测试文件」，不影响运行时导入。即便测试文件没被安装，`import scipy.constants` 也照常工作。

---

### 4.2 包导出 __init__.py：import * 的汇聚机制

#### 4.2.1 概念说明

`scipy/constants/__init__.py` 是这个子包的「门面」。它本身几乎不定义任何常数或函数，真正的实现都在私有模块 `_codata.py`（CODATA 数据库）和 `_constants.py`（数学常数、前缀、单位换算）里。

`__init__.py` 的核心职责是：**把私有模块里的公开符号「汇聚」到 `scipy.constants` 这个顶层名字空间**，让用户写 `from scipy.constants import c, kilo` 而不是 `from scipy.constants._codata import c`。

这套汇聚靠的就是 `from ._xxx import *`。带下划线的模块名（`_codata`、`_constants`）是 SciPy 的约定：**下划线 = 内部实现，不保证稳定，用户不应直接 import**。`__init__.py` 用 `import *` 把它们「洗白」成公开 API。

#### 4.2.2 核心流程

`__init__.py` 的导入段做三件事：

```text
from ._codata import *            ← CODATA 的物理常数 + value/unit/precision/find/ConstantWarning
from ._constants import *         ← 数学常数、SI/二进制前缀、单位换算因子、convert_temperature 等
from ._codata import _obsolete_constants, physical_constants   ← 额外显式取两个「下划线/不带下划线」名字
```

为什么第三行要单独再 `import` 一次？因为：

- `physical_constants`（不带下划线）本身会被 `import *` 带进来吗？取决于 `_codata.py` 的 `__all__`。为了**保险**，`__init__.py` 显式再导入一次，确保它一定存在于顶层。
- `_obsolete_constants`（带下划线）**不会**被 `import *` 带进来（下划线开头默认是私有的），但 `__init__.py` 后面构建文档表格时需要用到它来过滤「已废弃常量」，所以必须显式导入。

导入完成后，`scipy.constants` 命名空间里就同时有了两份来源的所有公开符号，用户无需关心它们各自来自哪个文件。

#### 4.2.3 源码精读

导入段位于一大段模块 docstring 之后：

[scipy/constants/__init__.py:334-336](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L334-L336)

```python
# Modules contributed by BasSw (wegwerp@gmail.com)
from ._codata import *
from ._constants import *
from ._codata import _obsolete_constants, physical_constants
```

> 注释 `# Modules contributed by BasSw` 是历史署名。三行 import 完成了「私有 → 公开」的汇聚：前两行用通配符搬入两份公开符号，第三行补取文档生成所必需的 `physical_constants` 与 `_obsolete_constants`。

紧接着是一行看似多余、实则关键的「弃用命名空间」导入：

[scipy/constants/__init__.py:338-339](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L338-L339)

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import codata, constants
```

> 这两个不带下划线的模块 `codata` / `constants` 是**弃用垫片**（4.4 节详讲）。这里把它们导入到顶层，是为了让老代码 `scipy.constants.codata.value(...)` 仍能工作（同时发出弃用警告）。注释明确写了「将在 v2.0.0 移除」。

> 顺带一提：docstring 结尾的 `"""  # noqa: E501`（[第 332 行](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L332)）用一个 `# noqa: E501` 关闭了「行太长」检查——因为里面的 RST 表格行普遍超长，这是文档型 docstring 的常见处理。

#### 4.2.4 代码实践

**实践目标**：搞清楚某个顶层符号到底来自哪个私有模块。

**操作步骤**：

1. 分别用 `_codata.py` / `_constants.py` 的 `__all__`（或 `dir()`）判断符号出处。
2. 验证 `c`（光速）来自 `_codata`，而 `kilo`（SI 前缀）来自 `_constants`。

**参考代码**（示例代码）：

```python
import scipy.constants as C
from scipy.constants import _codata, _constants

in_codata   = set(_codata.__all__)   if hasattr(_codata, '__all__')   else set(dir(_codata))
in_const    = set(_constants.__all__) if hasattr(_constants, '__all__') else set(dir(_constants))

for name in ['c', 'speed_of_light', 'kilo', 'mile', 'convert_temperature', 'value', 'pi']:
    src = []
    if name in in_codata: src.append('_codata')
    if name in in_const:  src.append('_constants')
    print(f"{name:20s} -> {', '.join(src) or '(未在 __all__/dir 中)'}")
```

**需要观察的现象**：`c`、`speed_of_light`、`value` 来自 `_codata`；`kilo`、`mile`、`convert_temperature`、`pi` 来自 `_constants`。

**预期结果**：每个符号都能追溯到唯一（或主）来源，验证 `import *` 的汇聚逻辑。

**无法在本地运行时**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么第三行要写成 `from ._codata import _obsolete_constants, physical_constants`，而不是也用 `import *`？

**参考答案**：`import *` 默认不会带入「下划线开头」的名字（除非目标模块的 `__all__` 显式包含它）。`_obsolete_constants` 以下划线开头，必须显式导入才能在 `__init__.py` 里用；`physical_constants` 虽不带下划线，但显式再导一次是防御性写法，保证它一定可用。

**练习 2**：如果用户直接 `from scipy.constants._codata import value`，会发生什么？能用吗？

**参考答案**：运行时**能用**（Python 不强制私有），但这违反了 SciPy 的下划线约定——`_codata` 是内部模块，未来版本可能改名或重构，SciPy 不保证其稳定性。正确写法是 `from scipy.constants import value`。

---

### 4.3 动态生成 __all__ 与 __doc__ 模板替换

#### 4.3.1 概念说明

`scipy.constants` 里有几百个常量，而且每次 CODATA 更新（2002→2022）都会增删。如果靠人手维护「文档表格」和「`__all__` 名单」，既枯燥又容易漏。

`__init__.py` 用两招自动化解决：

1. **文档表格自动生成**：模块 docstring 里留一个占位符 `%(constant_names)s`，运行时用 `physical_constants` 字典的内容「填空」，生成一张「常量名 / 值 / 单位」的 RST 表格塞进 docstring。
2. **`__all__` 自动生成**：用一行列表推导 `[s for s in dir() if not s.startswith('_')]`，把当前命名空间里「所有不带下划线的名字」收作公开 API。

这两招合起来体现了一个设计哲学：**单一数据源（single source of truth）**——常量只在 `_codata.py` 里定义一次，文档和 API 清单都从它派生，永远不会再「对不上」。

#### 4.3.2 核心流程

文档表格的生成过程：

```text
physical_constants  (name -> (value, unit, uncertainty))
        │
        │  过滤掉 _obsolete_constants 里的废弃键
        ▼
_constant_names_list = [(name.lower(), name, (value, unit, uncert)), ...]
        │
        │  按 name 排序，每行格式化成 RST：``name``  value unit
        ▼
_constant_names  (一段多行字符串)
        │
        │  __doc__ % dict(constant_names=_constant_names)
        ▼
__doc__  ← 占位符 %(constant_names)s 被替换为真实表格
```

`__all__` 的生成则简单得多：

```text
dir()                        ← 此刻模块命名空间里的所有名字
   │  过滤掉以 '_' 开头的（私有）
   ▼
__all__ = [s for s in dir() if not s.startswith('_')]
```

> 关键点：在执行这行之前，`__init__.py` 已经把所有临时变量（`_constant_names`、`_constant_names_list`）用 `del` 删掉了，所以它们不会污染 `__all__`。

#### 4.3.3 源码精读

先看 docstring 里的占位符。在第 92–94 行那张「Available constants」表格中间，有一行 `%(constant_names)s`：

[scipy/constants/__init__.py:90-94](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L90-L94)

```rst
Available constants:

======================================================================  ====
%(constant_names)s
======================================================================  ====
```

> 这是 Python 旧式字符串格式化（`%` 运算符）的占位符。`%(name)s` 表示「将来用字典里 `name` 这个 key 的字符串值替换此处」。

下面是「填空」逻辑。先构造一个三元组列表，把 `physical_constants` 里**非废弃**的常量挑出来：

[scipy/constants/__init__.py:341-343](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L341-L343)

```python
_constant_names_list = [(_k.lower(), _k, _v)
                        for _k, _v in physical_constants.items()
                        if _k not in _obsolete_constants]
```

> 每个元素是 `(名字的小写形式, 原始名字, (value, unit, uncertainty))`。小写形式留作排序键（`sorted` 默认按大写排在小写之前，统一小写可让 'alpha' 和 'Alpha' 类名字排序更稳定），原始名字用于显示，第三个是值元组。`if _k not in _obsolete_constants` 把废弃常量从文档里剔除。

再把列表格式化成一段 RST 表格文本：

[scipy/constants/__init__.py:344-346](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L344-L346)

```python
_constant_names = "\n".join(["``{}``{}  {} {}".format(_x[1], " "*(66-len(_x[1])),
                                                  _x[2][0], _x[2][1])
                             for _x in sorted(_constant_names_list)])
```

> 每行形如 `` `名字`<补齐到 66 列的空格>  值 单位 ``。`" "*(66-len(_x[1]))` 用空格把名字字段补到固定宽度，让表格左列对齐。多行用 `"\n".join` 拼接。注意这里用了 `sorted(_constant_names_list)`——因为元组第一个元素是小写名，所以是按「名字不区分大小写的字母序」排列。

然后做模板替换：

[scipy/constants/__init__.py:347-348](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L347-L348)

```python
if __doc__:
    __doc__ = __doc__ % dict(constant_names=_constant_names)
```

> `__doc__ % dict(...)` 就是把 docstring 里的 `%(constant_names)s` 替换成上面拼好的表格。`if __doc__:` 是防御：如果 Python 以 `-OO` 启动（剥离 docstring），`__doc__` 会是 `None`，此时跳过替换，避免 `None % dict(...)` 报错。

替换完毕，临时变量已无用处，立刻删除，防止它们混进 `__all__`：

[scipy/constants/__init__.py:350-351](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L350-L351)

```python
del _constant_names
del _constant_names_list
```

最后一行生成公开 API 清单：

[scipy/constants/__init__.py:353](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L353)

```python
__all__ = [s for s in dir() if not s.startswith('_')]
```

> `dir()` 返回此刻模块命名空间内的全部名字。列表推导过滤掉所有以 `_` 开头的（私有），剩下的就是公开 API。这是一种「约定优于枚举」的写法：**只要给内部名字加下划线前缀，它就自动被排除出公开 API**，新增常量时无需手动登记。

紧跟其后是把 `test` 钩子挂上（SciPy 每个子包都这样，便于 `scipy.constants.test()` 跑该子包的测试）：

[scipy/constants/__init__.py:355-357](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L355-L357)

```python
from scipy._lib._testutils import PytestTester
test = PytestTester(__name__)
del PytestTester
```

> `test` 是一个不带下划线的名字，所以它会出现在 `__all__` 里（在 4.3.4 实践中你会观察到这一点）。`del PytestTester` 又一次体现「用完即删，避免污染 `__all__`」。

#### 4.3.4 代码实践

**实践目标**：遍历 `scipy.constants.__all__`，按首字母分组统计；并验证文档表格确实由 `physical_constants` 派生。

**操作步骤**：

1. 用 `collections.defaultdict` 把 `__all__` 按首字母分组、计数。
2. 观察其中是否混入了非「常量」的名字（提示：`codata`、`constants`、`test`）。
3. 对照 `help(scipy.constants)` 或源码 docstring，确认表格行数 ≈ `len(physical_constants) - 废弃数`。

**参考代码**（示例代码）：

```python
import scipy.constants as C
from collections import defaultdict

# 1) 按首字母分组统计 __all__
groups = defaultdict(list)
for name in C.__all__:
    groups[name[0].upper()].append(name)

print(f"__all__ 公开符号总数: {len(C.__all__)}")
for letter in sorted(groups):
    print(f"  {letter}: {len(groups[letter])} 个")

# 2) 观察混入的「非常量」名字
for special in ['codata', 'constants', 'test', 'physical_constants', 'value', 'find']:
    print(f"{special:20s} 在 __all__ 中? {special in C.__all__}")

# 3) 文档表格行数 vs physical_constants 体积
from scipy.constants._codata import _obsolete_constants
non_obs = [k for k in C.physical_constants if k not in _obsolete_constants]
print(f"physical_constants(非废弃) 数量: {len(non_obs)}")
```

**需要观察的现象**：

- `__all__` 里除了大量常量名，还混着 `codata`、`constants`（4.2 节导入的弃用垫片模块）、`test`（PytestTester 钩子）、`physical_constants`、`value`、`unit`、`precision`、`find`、`ConstantWarning`、`convert_temperature`、`lambda2nu`、`nu2lambda` 等非「常量」符号——它们都不带下划线，所以被自动收进 `__all__`。
- 文档表格的行数（`%(constant_names)s` 展开后的行数）应等于 `physical_constants` 中非废弃常量的数量。

**预期结果**：分组统计能跑通；`codata`/`constants`/`test` 三个特殊名字的「是」结果，正好印证「`__all__` 按下划线前缀过滤」的机制。

**无法在本地运行时**：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `del _constant_names` 这一行，`scipy.constants.__all__` 会多出哪个名字？为什么不会是 `_constant_names_list`？

**参考答案**：会多出 `_constant_names` 吗？——不会！因为 `_constant_names` 以下划线开头，会被 `if not s.startswith('_')` 过滤掉。所以单独漏删其实不影响 `__all__`。但 `_obsolete_constants`、`_codata`（模块对象）等下划线名字本来就被排除，`del` 的真正意义是「保持命名空间干净、释放内存、避免在 `dir()` 里制造噪音」。这道题的陷阱是：**下划线前缀本身就是过滤条件，`del` 是双保险。**

**练习 2**：为什么用 `__doc__ % dict(...)` 而不是 `.format()` 或 f-string？

**参考答案**：因为 docstring 里还含有大量 RST 内容（比如 `%` 字符可能出现在别处，但更关键的是）`%`-格式化的具名占位符 `%(constant_names)s` 可以**只替换指定字段、保留其余文本原样**，且占位符语法对文档作者而言醒目、易识别。f-string 会把所有 `{}` 都当作插值，docstring 里的 `{}` 会被误解析。`%`-格式化在「只填一个坑」的场景下更安全。

---

### 4.4 弃用命名空间垫片 codata / constants

#### 4.4.1 概念说明

历史上，用户曾用 `scipy.constants.codata.value(...)` 或 `scipy.constants.constants.c` 这样的「二级模块」路径来访问常量。后来 SciPy 把实现搬进了私有的 `_codata.py` / `_constants.py`，并希望统一用 `scipy.constants` 顶层命名空间。

但直接删掉旧路径会**破坏成千上万行老代码**。SciPy 的折中方案是：**保留 `codata.py` / `constants.py` 这两个「垫片（shim）」文件，里面不放任何真实实现，只做一件事——当用户访问它的任何属性时，发一条弃用警告，然后把请求「转发」到新的私有模块。** 等到 v2.0.0，再连垫片一起删除。

这种「转发」靠 PEP 562 的模块级 `__getattr__` 实现：访问一个模块上不存在的属性时，Python 会调用该模块的 `__getattr__(name)`。

#### 4.4.2 核心流程

```text
用户写: scipy.constants.codata.value('...')
                │
                │  codata 模块里并没有名为 value 的真实属性
                ▼
        触发 codata.__getattr__('value')
                │
                ▼
   _sub_module_deprecation(
       sub_package="constants",
       module="codata",
       private_modules=["_codata"],
       all=__all__,
       attribute="value")
                │
                │  ① 发出 DeprecationWarning（提示改用 scipy.constants）
                │  ② 从 _codata 取出真正的 value 返回给用户
                ▼
   用户拿到正确的 value，同时看到弃用警告
```

垫片文件里还有两个配套约定：

- `__all__`：列出「老路径下曾经能访问的名字」，但**这些名字在文件里根本没定义**——它们只存在于 `__all__` 这个字符串列表里，真正的取值靠 `__getattr__` 延迟完成。
- `__dir__()`：让 `dir(scipy.constants.codata)` 能列出这些名字，方便 IDE 自动补全和 `tab` 补齐。

#### 4.4.3 源码精读

先看 `codata.py` 垫片，文件头明确写了「不供公共使用、将在 v2.0.0 移除」：

[scipy/constants/codata.py:1-5](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/codata.py#L1-L5)

```python
# This file is not meant for public use and will be removed in SciPy v2.0.0.
# Use the `scipy.constants` namespace for importing the functions
# included below.

from scipy._lib.deprecation import _sub_module_deprecation
```

> 这是 SciPy 全项目统一的弃用垫片模板：开头三行注释 + 从 `scipy._lib.deprecation` 引入 `_sub_module_deprecation` 工具函数（该函数负责发警告 + 转发取值，本讲不深入其实现）。

接着是 `__all__`，注意末尾的 `# noqa: F822`：

[scipy/constants/codata.py:7-11](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/codata.py#L7-L11)

```python
__all__ = [  # noqa: F822
    'physical_constants', 'value', 'unit', 'precision', 'find',
    'ConstantWarning', 'k', 'c',

]
```

> `F822` 是 pyflakes 的检查项「`__all__` 中存在未定义的名字」。这个文件里确实**没有**定义 `physical_constants`、`value` 等，它们只在 `__all__` 里作为字符串出现。`# noqa: F822` 告诉 linter「我知道它们没定义，这是故意的」，避免 lint 报错。

然后是 `__dir__` 和真正干活的 `__getattr__`：

[scipy/constants/codata.py:14-21](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/codata.py#L14-L21)

```python
def __dir__():
    return __all__


def __getattr__(name):
    return _sub_module_deprecation(sub_package="constants", module="codata",
                                   private_modules=["_codata"], all=__all__,
                                   attribute=name)
```

> `__dir__()` 让 `dir()` 返回 `__all__`，保证补全可用。`__getattr__(name)` 是核心：任何对 `scipy.constants.codata.xxx` 的访问，只要 `xxx` 不是模块真实属性，就会走到这里，由 `_sub_module_deprecation` 发警告并从 `_codata` 取回真正的对象。参数 `private_modules=["_codata"]` 告诉它「真正实现在哪个私有模块」。

`constants.py` 垫片的结构完全一样，只是 `__all__` 列表更长（涵盖了 `_constants.py` 里几乎所有公开名字），转发目标是 `_constants`：

[scipy/constants/constants.py:50-53](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/constants.py#L50-L53)

```python
def __getattr__(name):
    return _sub_module_deprecation(sub_package="constants", module="constants",
                                   private_modules=["_constants"], all=__all__,
                                   attribute=name)
```

> 对比 `codata.py`：`module` 参数不同（`"constants"` vs `"codata"`），`private_modules` 指向不同的私有模块（`["_constants"]` vs `["_codata"]`），其余机制一字不差。这是「复制粘贴式模板」——好处是全项目一致、维护成本低。

回到 `__init__.py`，那行 `from . import codata, constants`（4.2 节已见）正是为了让这两个垫片模块「存在」于顶层包，老代码的 `scipy.constants.codata.xxx` 才能触发垫片的 `__getattr__`。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次弃用警告，看清它建议你改用什么。

**操作步骤**：

1. 用 `warnings.catch_warnings(record=True)` 捕获警告。
2. 访问 `scipy.constants.codata.value`（或 `scipy.constants.constants.c`）。
3. 打印警告类别与文案。

**参考代码**（示例代码）：

```python
import warnings
import scipy.constants as C

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter('always')
    # 通过垫片访问，触发 __getattr__ → 弃用警告 + 正确返回值
    v = C.codata.value('speed of light in vacuum')
    print("拿到的值:", v)

if w:
    print("警告类别:", w[0].category.__name__)
    print("警告文案:", str(w[0].message))
else:
    print("未捕获到警告")
```

**需要观察的现象**：程序既打印了正确的光速值，又打印出一条 `DeprecationWarning`，文案大意是「`scipy.constants.codata` 已弃用，请改用 `scipy.constants` 命名空间」。

**预期结果**：`v` 等于 `C.value('speed of light in vacuum')`（约 `299792458.0`）；警告类别为 `DeprecationWarning`。文案的精确措辞**待本地验证**（取决于 `scipy._lib.deprecation._sub_module_deprecation` 的实现）。

**无法在本地运行时**：待本地验证。

> 进阶观察：把 `C.codata.value` 换成 `C.constants.c`，应得到类似警告，转发目标变成 `_constants`。这印证「两个垫片共用一套模板，只是转发目标不同」。

#### 4.4.5 小练习与答案

**练习 1**：`codata.py` 的 `__all__` 里写了 `'value'`，但文件里并没有 `def value(...)`。为什么 `scipy.constants.codata.value` 还能用？

**参考答案**：因为访问 `value` 时，Python 发现它不是模块的真实属性，于是调用 PEP 562 的模块级 `__getattr__('value')`，后者经 `_sub_module_deprecation` 从私有模块 `_codata` 取回真正的 `value` 函数。`__all__` 在这里只是「补全/文档清单」，不代表真实定义。

**练习 2**：`# noqa: F822` 如果删掉，程序运行会出错吗？

**参考答案**：运行时**不会**出错——它只是 linter（pyflakes/flake8）的提示标记，与 Python 解释器无关。删掉后，代码照常运行，但 lint 检查会因为「`__all__` 里有未定义名字」而报警告，干扰 CI。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小考察任务：**「画一张 constants 子包的施工与门面关系图」**。

1. **读构建**：打开 `scipy/constants/meson.build` 与 `scipy/constants/tests/meson.build`，画一张「哪些文件被安装、装到哪、带什么 tag」的表。
2. **读门面**：打开 `scipy/constants/__init__.py`，标注出：①汇聚导入（3 行 `import` + 弃用导入）；②文档表格生成段；③`__all__` 生成行；④`test` 钩子。
3. **读垫片**：打开 `codata.py` / `constants.py`，指出它们与 `_codata.py` / `_constants.py` 的转发对应关系。
4. **动手验证**：运行下面这段「体检脚本」（示例代码），把输出填进你的关系图。

```python
import warnings, scipy.constants as C

# A. 构建产物自检
import os
print("[A] __file__:", C.__file__)

# B. 门面汇聚自检：c 来自哪、kilo 来自哪
from scipy.constants import _codata, _constants
print("[B] c in _codata.__all__?  ", 'c'    in _codata.__all__)
print("[B] kilo in _constants.__all__?", 'kilo' in _constants.__all__)

# C. __all__ 自检
print("[C] __all__ 含 'codata'?", 'codata' in C.__all__,
      "| 含 'test'?", 'test' in C.__all__,
      "| 总数:", len(C.__all__))

# D. 垫片自检
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter('always')
    _ = C.codata.unit('speed of light in vacuum')
print("[D] 垫片触发警告类别:", w[0].category.__name__ if w else "无")
```

**完成标志**：你能不看源码，用自己的话讲清楚「一个常量从 `_codata.py` 定义，到被 `meson.build` 安装，到被 `__init__.py` 汇聚进 `__all__`，再到老代码经垫片访问触发警告」的完整链路。

## 6. 本讲小结

- **构建同构**：`scipy/constants/meson.build` 用 `python_sources` 列表 + `py3.install_sources` 把 5 个 `.py` 安装到 `<site-packages>/scipy/constants/`；它由父级 `scipy/meson.build:731` 的 `subdir('constants')` 挂进构建树。
- **门面汇聚**：`__init__.py` 靠 `from ._codata import *` 与 `from ._constants import *` 把两个私有模块的公开符号提升为顶层 API，下划线 = 私有是全包约定。
- **单一数据源**：文档表格由 `physical_constants` 运行时生成，经 `__doc__ % dict(...)` 填入 `%(constant_names)s` 占位符；常量只在 `_codata.py` 定义一次，文档永不脱节。
- **自动 `__all__`**：`__all__ = [s for s in dir() if not s.startswith('_')]`，配合 `del` 清理临时变量，实现「加下划线即私有、无需手工登记」。
- **弃用垫片**：`codata.py` / `constants.py` 不含实现，只靠模块级 `__getattr__` + `_sub_module_deprecation` 发警告并转发到 `_codata` / `_constants`，`# noqa: F822` 用于压制「`__all__` 含未定义名字」的 lint 告警，计划在 v2.0.0 移除。

## 7. 下一步学习建议

本讲只回答了「文件怎么组织、怎么构建、怎么导出」，**还没真正读常量本身的内容**。下一篇：

- **u1-l3《数学常数与 SI / 二进制前缀》**：进入 `_constants.py`，看 `pi`、`golden`、`kilo`…`quecto`、`kibi`…`yobi` 这些纯 Python 字面量是如何定义和命名的。
- **u1-l4《单位与线性换算因子》**：继续读 `_constants.py` 的后半部分，理解质量/长度/能量等单位换算因子，以及哪些因子其实派生自 CODATA（`from ._codata import value as _cd`）。

读完 u1 全单元后，进阶篇 **u2** 会带你在 `_codata.py` 里深挖 CODATA 文本解析、精确派生常数计算、多版本合并与别名机制——届时你会发现，本讲看到的 `physical_constants` 字典其实是一段相当精彩的解析流水线的产物。
