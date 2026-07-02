# 讲义 u1-l1：项目概览与定位

> 本讲是「scipy.constants 子包学习手册」的**第一篇**。它不要求你事先读过任何 SciPy 源码，只要求你会基本的 Python。
> 读完后你将知道：`scipy.constants` 是什么、它对外提供了哪些能力、这些能力在源码里长什么样。

---

## 1. 本讲目标

读完本讲，你应当能够：

- 用一句话说清 `scipy.constants` 子包在 SciPy 项目里的**定位与职责**。
- 说出它的**三大能力**：常用数学/物理常数、单位换算因子、CODATA 物理常数数据库。
- 打开 `scipy/constants/__init__.py`，看懂其中的 **模块 docstring（常量表格 + autosummary）**、`physical_constants` 数据对象、`__all__` 自动生成，以及 `CODATA2022` 参考文献这四块内容之间的关系。
- 学会通过「模块 docstring + `__all__`」自己查阅 `scipy.constants` 的**全部公开符号**。

本讲只做"俯瞰"，**不**深入 CODATA 解析、派生常数计算等内部机制——那些放在进阶篇（u2）。

---

## 2. 前置知识

在开始前，确认你了解下面几个概念。它们都很基础，但本讲会反复用到。

- **子包（subpackage）**：Python 里一个目录就是一个包。`scipy/constants/` 这个目录对应 `import scipy.constants`。这个子包是 SciPy 大项目里专门管"常数与单位"的一小块。
- **模块 docstring**：一个 `.py` 文件开头的三引号字符串。它既是给人看的文档，在 SciPy 里也是给 Sphinx 文档系统看的原料。本讲的 `__init__.py` 几乎一半内容都是 docstring。
- **`__all__`**：一个模块里定义的字符串列表，用来声明"对外公开哪些名字"。`from scipy.constants import *` 时，只有 `__all__` 里的名字会被导入。
- **SI 单位制**：国际单位制（米、千克、秒、安培……）。`scipy.constants` 里的物理常数和换算因子默认都换算到 SI 基本单位。
- **CODATA**：国际科技数据委员会（Committee on Data for Science and Technology）。它每隔几年发布一次"基本物理常数推荐值"，是目前全球公认的物理常数标准。`scipy.constants` 里的物理常数就来自 CODATA 的 **2022 年版本**。

> 名词速记：本讲会出现 **autosummary**（Sphinx 的一个指令，用来自动生成函数清单）和 **docstring 表格**（在文档里用 `=====` 画出来的表格）。它们都只是"写文档的工具"，不影响代码运行。

---

## 3. 本讲源码地图

本讲只围绕**一个**核心文件展开：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `scipy/constants/__init__.py` | 子包的入口文件，负责"对外展示"和"拼装公开 API" | docstring 表格、`physical_constants` 导入、`__all__` 生成、`CODATA2022` 引用 |

为了让"三大能力"讲得清楚，本讲还会**顺带对照**两个被它 `import` 的兄弟文件（只看头部，不深入）：

| 文件 | 作用 |
| --- | --- |
| `scipy/constants/_constants.py` | 定义数学常数（如 `pi`、`golden`）、SI/二进制前缀、单位换算因子、温度转换函数。注意文件名以 `_` 开头，是"私有"实现。 |
| `scipy/constants/_codata.py` | 定义 `physical_constants` 物理常数数据库及 `value()` / `unit()` / `precision()` / `find()` 查找函数。数据来自 CODATA 2022。 |

> 经验法则：在 SciPy 里，文件名以 `_` 开头（如 `_constants.py`）表示"内部实现"，外部用户通常不应直接 `import` 它。`__init__.py` 通过 `from ._constants import *` 把里面的公开名字"提"到 `scipy.constants` 顶层供大家使用。

---

## 4. 核心概念与源码讲解

### 4.1 三大能力与模块 docstring 中的常量表格

#### 4.1.1 概念说明

打开 SciPy 的官方文档，你会看到 `scipy.constants` 被概括为一句话：

> Physical and mathematical constants and units.（物理与数学常数，以及单位。）

这句话其实点出了子包的**三大能力**：

1. **数学常数**：`pi`、`golden`（黄金比例）等。它们是纯 Python 字面量，与 CODATA 无关。
2. **物理常数与单位换算因子**：`c`（光速）、`h`（普朗克常数）、`kilo`、`mile`、`psi` 等。这类里大部分是"换算因子"（一个乘起来就能换算的数），少部分（如 `c`、`h`）是 CODATA 物理常数。
3. **CODATA 物理常数数据库**：一个名为 `physical_constants` 的大字典，里面装着几百个带"数值 / 单位 / 不确定度"的物理常数，并配套 4 个查找函数。

这三大能力**并不是分散在不同地方**，而是全部汇总在 `__init__.py` 的**模块 docstring** 里，通过一张张表格呈现给用户。换句话说，`__init__.py` 的 docstring 就是 `scipy.constants` 的"产品说明书"。

#### 4.1.2 核心流程

`__init__.py` 的 docstring 是如何组织的？可以把它理解成一份目录式文档：

```text
模块标题：Constants
├─ 一句话总览："Physical and mathematical constants and units."
├─ Mathematical constants（数学常数表）：pi / golden / golden_ratio
├─ Physical constants（物理常数表）：c / h / k / G / m_e ... 一张表
├─ Constants database（常数数据库）
│    ├─ autosummary：value / unit / precision / find / ConstantWarning
│    └─ .. data:: physical_constants（解释这个字典的格式）
├─ Units（单位与换算）
│    ├─ SI prefixes / Binary prefixes（前缀表）
│    └─ Mass / Angle / Time / Length / Pressure ... 各物理量换算因子表
│         （其中 Optics 段又用 autosummary 列出 lambda2nu / nu2lambda）
└─ References：CODATA2022
```

关键点：**"表格"用来罗列常量/因子，"autosummary"用来罗列函数**。两者都是给 Sphinx 文档工具的指令，运行时不影响代码，但读源码时它们就是一份现成的 API 清单。

#### 4.1.3 源码精读

**总览句**：docstring 开头一句话点题——

```python
Physical and mathematical constants and units.
```

详见 [\_\_init\_\_.py:L1-L8](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L1-L8)（模块标题与一句话总览，标明了本子包只管"常数与单位"）。

**数学常数表**：用 `=====` 画出，列出三个名字——

```text
================  =================================================================
``pi``            Pi
``golden``        Golden ratio
``golden_ratio``  Golden ratio
================  =================================================================
```

见 [\_\_init\_\_.py:L11-L18](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L11-L18)（数学常数表，说明 `pi`、`golden`、`golden_ratio` 这三个名字属于本子包的公开数学常数）。

**物理常数表**：列名是 `Attribute / Quantity / Units`，给出 `c`、`speed_of_light`、`mu_0`、`epsilon_0`、`h`、`Planck`、`G`、`e`、`R`、`alpha`、`N_A`、`k`、`Boltzmann`、`sigma`、`m_e` 等。注意很多量有两个别名（如 `c` 与 `speed_of_light`、`h` 与 `Planck`），都指向同一个物理量。见 [\_\_init\_\_.py:L21-L59](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L21-L59)（物理常数表，列出顶层可直接访问的物理常数及其单位）。

**Constants database 段的两个 autosummary**：这里用 `.. autosummary::` 自动列出 5 个与数据库相关的"函数/类"——`value`、`unit`、`precision`、`find`、`ConstantWarning`：

```text
.. autosummary::
   :toctree: generated/

   value      -- Value in physical_constants indexed by key
   unit       -- Unit in physical_constants indexed by key
   precision  -- Relative precision in physical_constants indexed by key
   find       -- Return list of physical_constant keys with a given string
   ConstantWarning -- Constant sought not in newest CODATA data set
```

见 [\_\_init\_\_.py:L62-L76](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L62-L76)（用 autosummary 把数据库相关的 5 个 API 一次性列出，是查找类 API 的入口清单）。

**Units 段**：随后是一大批单位换算因子表，覆盖 SI 前缀（`quetta`…`quecto`）、二进制前缀（`kibi`…`yobi`）以及 Mass / Angle / Time / Length / Pressure / Area / Volume / Speed / Temperature / Energy / Power / Force 各物理量。其中 Optics 段又用了一个 autosummary 列出光学函数 `lambda2nu`、`nu2lambda`。整体见 [\_\_init\_\_.py:L97-L323](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L97-L323)（单位与换算因子的全部表格，以及 Optics 段的 autosummary）。

> 小结：docstring 用"表格 + autosummary"两件套，把**全部对外名字**都列在了文档里。你不必记住这些名字，只要知道"翻 docstring 就能查到"即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证"docstring 里写的名字，在 import 后真的能访问到"。

**操作步骤**（需本地安装了 SciPy）：

```python
import scipy.constants as C

# 1) 数学常数
print("pi =", C.pi)
print("golden =", C.golden)

# 2) 物理常数（注意 c 和 speed_of_light 应该相等）
print("c =", C.c)
print("speed_of_light =", C.speed_of_light)
print("c is speed_of_light?", C.c == C.speed_of_light)

# 3) 单位换算因子：把 60 mile/minute 换算成 m/s
speed_ms = 60 * C.mile / C.minute
print("60 mile/min in m/s =", speed_ms)
```

**需要观察的现象**：

- `c` 和 `speed_of_light` 应输出同一个数（约 \(2.99792458\times10^{8}\) m/s），因为它们是同一个物理量的两个别名。
- `60 * mile / minute` 能直接算出米/秒，说明单位换算因子就是"普通的数"，可以参与运算。

**预期结果**：`c is speed_of_light?` 打印 `True`；速度约为 \(1609\text{ m}\times 60 / 60\text{ s}\approx1609.34\) m/s。

> 说明：本环境无法直接运行 SciPy，以上数值为**待本地验证**。具体数值请以你本地运行结果为准。

#### 4.1.5 小练习与答案

**练习 1**：在物理常数表里，`k` 和 `Boltzmann` 都标注为 "Boltzmann constant"。它们的单位是什么？二者在数值上应该相等吗？

**答案**：单位是 `J K^-1`（焦耳每开尔文）。它们是同一个物理常数（玻尔兹曼常数）的两个别名，因此数值相等。

**练习 2**：docstring 里 `.. data:: physical_constants` 这一段（见 [\_\_init\_\_.py:L78-L94](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L78-L94)）说字典里每个值的格式是 `(value, unit, uncertainty)`。请猜一下 `uncertainty` 表示什么。

**答案**：表示该常数的不确定度（测量误差范围）。注意 docstring 还特别说明：当一个常数在 CODATA 里是"精确值"时，这里会写 `0.0` 表示"无不确定度"，但它仍受双精度浮点数本身的截断误差影响。

---

### 4.2 physical_constants 数据对象

#### 4.2.1 概念说明

前两节提到的 `c`、`h`、`k` 等，是子包**预先挑好、直接挂在顶层**的常用物理常数。但 CODATA 2022 数据库里有**几百个**常数，不可能给每一个都起一个顶层变量名。于是子包把它们统一装进一个**字典**：

- **名字**：`physical_constants`
- **键**：常数的英文全名（字符串），如 `"speed of light in vacuum"`、`"Boltzmann constant"`。
- **值**：三元组 `(value, unit, uncertainty)`，分别对应"数值、单位、相对/绝对不确定度"。

这是 `scipy.constants` 第三大能力的核心载体。`__init__.py` 自己**并不定义**这个字典，而是从 `_codata.py` 把它**导入**进来，再写进 docstring 给用户看。

#### 4.2.2 核心流程

`physical_constants` 在 `__init__.py` 里的生命周期：

```text
1. _codata.py 里先把六个版本（2002~2022）的常数合并成一个 dict
   physical_constants = {}  →  依次 .update(_physical_constants_20XX)
   （最终以 2022 版为当前数据集，见 _codata.py:L2063-L2070）

2. __init__.py 用一行把它导入顶层命名空间：
   from ._codata import _obsolete_constants, physical_constants

3. __init__.py 的 docstring 用 .. data:: physical_constants 说明它的格式

4. 用户访问：from scipy.constants import physical_constants
            physical_constants["speed of light in vacuum"]
            →  (299792458.0, 'm s^-1', 0.0)
```

要点：`physical_constants` 是一个**普通 Python 字典**，所以你可以像用任何字典一样 `keys()`、`items()`、按名取值。

#### 4.2.3 源码精读

**导入语句**——在 docstring 结束后，`__init__.py` 第一批真正的可执行代码就是三个 `import`：

```python
from ._codata import *
from ._constants import *
from ._codata import _obsolete_constants, physical_constants
```

见 [\_\_init\_\_.py:L333-L336](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L333-L336)（从 `_codata`、`_constants` 把公开符号和 `physical_constants` 导入到顶层；`_obsolete_constants` 也会用到，下一节解释）。

**docstring 里的格式说明**——用 `.. data::` 指令专门介绍这个字典：

```text
.. data:: physical_constants

   Dictionary of physical constants, of the format
   ``physical_constants[name] = (value, unit, uncertainty)``.
```

见 [\_\_init\_\_.py:L78-L94](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L78-L94)（官方文档对 `physical_constants` 数据格式与"精确值用 `0.0` 标记"规则的说明）。

**真正的"生产地"在 _codata.py**（本讲只看一眼，深入留到 u2）：

```python
physical_constants: dict[str, tuple[float, str, float]] = {}
physical_constants.update(_physical_constants_2002)
...
physical_constants.update(_physical_constants_2022)
```

见 [_codata.py:L2063-L2069](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2063-L2069)（用空字典起步，依次合并六个 CODATA 版本；后 `update` 的会覆盖同名旧值，因此最终以 2022 版为准）。配套的查找函数 `value / unit / precision / find` 也定义在 [_codata.py:L2130-L2208](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L2130-L2208)。

#### 4.2.4 代码实践

**实践目标**：亲手感受 `physical_constants` 的三元组结构。

**操作步骤**：

```python
from scipy.constants import physical_constants

# 取三个不同物理量的常数
for key in ["speed of light in vacuum",
            "Boltzmann constant",
            "Newtonian constant of gravitation"]:
    value, unit, uncertainty = physical_constants[key]
    print(f"{key}\n  value={value}\n  unit={unit}\n  uncertainty={uncertainty}\n")

# 数一数库里一共有多少个常数
print("total constants:", len(physical_constants))
```

**需要观察的现象**：

- `speed of light in vacuum` 的 `uncertainty` 应为 `0.0`（光速在 SI 中是精确定义的）。
- `Newtonian constant of gravitation`（万有引力常数 G）的 `uncertainty` 应是一个**非零**的正数，因为 G 至今仍是测量精度最差的基本常数之一。
- `len(physical_constants)` 给出常数总数（约三百多个，**待本地验证**具体数值）。

**预期结果**：三元组按顺序对应"值/单位/不确定度"；总数与 CODATA 2022 推荐值条目数一致。

> 说明：本环境无法运行 SciPy，`len()` 的精确值请本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`physical_constants["speed of light in vacuum"]` 返回的元组里，第三个元素为什么是 `0.0`？

**答案**：因为光速 \(c\) 在 SI 单位制中是被**精确定义**为 \(299792458\text{ m/s}\) 的，没有测量不确定度，所以 uncertainty 记为 `0.0`。

**练习 2**：如果直接 `import scipy.constants as C`，然后 `C.physical_constants`，能拿到这个字典吗？为什么？

**答案**：能。因为 `__init__.py` 通过 `from ._codata import ... physical_constants` 把它放进了顶层命名空间，所以它就是 `scipy.constants` 的一个公开属性。

---

### 4.3 \_\_all\_\_ 与 \_\_doc\_\_ 的自动生成

#### 4.3.1 概念说明

你可能会问：`__init__.py` docstring 里列了那么多名字，还要手动维护一份 `__all__` 列表吗？SciPy 的答案是**不**。它用了一个很巧的小技巧：

- **`__all__` 自动生成**：扫描当前模块命名空间里所有"不以 `_` 开头"的名字，自动组装成公开列表。
- **`__doc__` 动态填充**：docstring 里留了一个占位符 `%(constant_names)s`，运行时再把 `physical_constants` 里所有常数名拼成一张表填进去。

这样设计的好处是：每次 CODATA 升级、常数增减，**不需要手改 `__all__`**，命名空间变了公开 API 就自动跟着变。

#### 4.3.2 核心流程

`__init__.py` 末尾的"装配车间"流程：

```text
A. 把 physical_constants 的键整理成 (小写名, 原名, 三元组) 列表
   —— 但跳过 _obsolete_constants（已废弃的常数，不进文档表）
   _constant_names_list = [...]

B. 把列表格式化成一段 docstring 表格文本（每行：`名字  值 单位`）
   _constant_names = "\n".join([...])

C. 用这串文本替换 docstring 里的占位符
   __doc__ = __doc__ % dict(constant_names=_constant_names)

D. 清理临时变量
   del _constant_names, _constant_names_list

E. 自动生成 __all__：抓所有不以 '_' 开头的名字
   __all__ = [s for s in dir() if not s.startswith('_')]
```

#### 4.3.3 源码精读

**第 A 步——过滤掉废弃常数**：

```python
_constant_names_list = [(_k.lower(), _k, _v)
                        for _k, _v in physical_constants.items()
                        if _k not in _obsolete_constants]
```

见 [\_\_init\_\_.py:L341-L343](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L341-L343)（遍历 `physical_constants`，**排除** `_obsolete_constants`，并记录小写名用于排序）。这正是为什么 4.2.2 里要单独导入 `_obsolete_constants`——它的唯一用途就是在这里做"黑名单"过滤。

**第 B 步——格式化成表格行**：

```python
_constant_names = "\n".join(["``{}``{}  {} {}".format(_x[1], " "*(66-len(_x[1])),
                                                  _x[2][0], _x[2][1])
                             for _x in sorted(_constant_names_list)])
```

见 [\_\_init\_\_.py:L344-L346](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L344-L346)（把每个常数格式化成 `` ``名字``  值 单位 `` 的一行，用空格补齐到 66 列对齐；`_x[2][0]`/`_x[2][1]` 分别是三元组里的 value 和 unit）。这里用 `sorted(...)` 按**小写名**排序，保证文档表是字母序的。

**第 C 步——替换占位符**：

```python
if __doc__:
    __doc__ = __doc__ % dict(constant_names=_constant_names)
```

见 [\_\_init\_\_.py:L347-L348](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L347-L348)（用 Python 字符串 `%` 格式化，把 docstring 里 `%(constant_names)s` 占位符替换成上面拼好的表——对应 docstring 中的 [\_\_init\_\_.py:L93](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L93) 那一行 `%(constant_names)s`）。

**第 D、E 步——清理与生成 `__all__`**：

```python
del _constant_names
del _constant_names_list

__all__ = [s for s in dir() if not s.startswith('_')]
```

见 [\_\_init\_\_.py:L350-L353](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L350-L353)（先删掉两个临时变量以免"漏"进公开列表，再用 `dir()` 收集所有不以 `_` 开头的名字作为 `__all__`）。注意顺序很重要：**先 `del` 临时变量，再生成 `__all__`**，否则 `_constant_names` 这类带下划线的虽不会入选，但顺序上保持清晰；更重要的是这一步会把前面 `import *` 进来的所有公开名字一网打尽。

> 这是 SciPy 里很常见的"`__all__` 惯用法"：不手写列表，靠命名约定（公开名不带 `_`，私有名带 `_`）自动收敛公开 API。

#### 4.3.4 代码实践

**实践目标**：验证 `__all__` 是自动生成的，并搞清"哪些类别各有多少个公开符号"。

**操作步骤**：

```python
import scipy.constants as C

n = len(C.__all__)
print("公开符号总数:", n)

# 把它们按"类别"做个粗分（基于命名直觉的简单归类）
def classify(name):
    if name in ("pi", "golden", "golden_ratio"):
        return "数学常数"
    if name in ("value", "unit", "precision", "find", "ConstantWarning"):
        return "查找API"
    if name in ("convert_temperature", "lambda2nu", "nu2lambda"):
        return "函数"
    if name in ("physical_constants",):
        return "数据库"
    # 剩下基本是物理常数 + 换算因子
    return "常数/换算因子"

from collections import Counter
cnt = Counter(classify(nm) for nm in C.__all__)
for cat, count in cnt.most_common():
    print(f"  {cat}: {count}")
```

**需要观察的现象**：

- `__all__` 的总数应该 = 数学常数(3) + 顶层物理常数(若干) + 单位/换算因子(很多) + 查找API(5) + 光学/温度函数(3) + `physical_constants` + `test`（PytestTester，见 [\_\_init\_\_.py:L355-L357](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L355-L357)）。
- "常数/换算因子"这一类应占绝大多数。

**预期结果**：总数在三百多个量级（因为 `physical_constants` 里的常数并不进入 `__all__`——`__all__` 只含**顶层单值名**，字典本身是一个名字）。**待本地验证**精确值。

> 易错点：`physical_constants` 字典里的几百个常数名**不会**出现在 `__all__` 里。`__all__` 里只有"字典对象本身"这一个名字。要查常数名请用 `find()` 或 `physical_constants.keys()`。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 B 步要把名字补齐到 66 列（`" "*(66-len(_x[1]))`）？

**答案**：为了在 docstring 表格里**对齐列**，让每行的"名字"列宽度一致，后面的"值 单位"才整齐。这是一种纯展示用的排版技巧。

**练习 2**：如果有人往 `_constants.py` 里新增了一个叫 `foo` 的公开常量（不带下划线），`__all__` 会自动包含它吗？

**答案**：会。因为 `__init__.py` 先执行 `from ._constants import *`，把 `foo` 引入顶层命名空间；之后 `__all__ = [s for s in dir() if not s.startswith('_')]` 会自动把 `foo` 收进去，无需改 `__all__`。

---

### 4.4 参考文献 CODATA2022 与数据溯源

#### 4.4.1 概念说明

科研代码用到的"常数"必须有**出处**。`scipy.constants` 的所有物理常数都明确标注来自 **CODATA 2022 推荐值**（CODATA Recommended Values of the Fundamental Physical Constants 2022）。这在 docstring 里以参考文献 `[CODATA2022]_` 的形式出现，并给出了 NIST 官方链接。

理解这一点很重要：

- **为什么是 2022？** CODATA 每隔几年发布一次推荐值，2022 版是目前（截至本 HEAD）最新的。`_codata.py` 里其实保留了 2002~2022 六个历史版本，但"当前生效"的是 2022 版（见 4.2.3 的 `_current_constants`）。
- **为什么强调出处？** 因为物理常数会随测量精度提升而更新。你今天算出的某物理量结果，几年后可能因常数更新而微调。引用 CODATA 版本就是让结果**可复现、可追溯**。

#### 4.4.2 核心流程

参考文献在 `__init__.py` 里有两处呼应：

```text
1. 正文引用：Constants database 段写 "2022 CODATA recommended values [CODATA2022]_"
2. 参考文献定义：文末 References 段用 .. [CODATA2022] 给出完整出处与 URL
```

Sphinx 会把 `[CODATA2022]_`（引用）与 `.. [CODATA2022]`（定义）自动链接起来。

#### 4.4.3 源码精读

**正文引用**——在 Constants database 段点名数据来源：

```text
In addition to the above variables, :mod:`scipy.constants` also contains the
2022 CODATA recommended values [CODATA2022]_ database containing more physical
constants.
```

见 [\_\_init\_\_.py:L62-L67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L62-L67)（声明数据库来自 2022 CODATA 推荐值，并埋下引用锚点 `[CODATA2022]_`）。

**参考文献定义**——docstring 末尾给出完整出处：

```text
References
==========

.. [CODATA2022] CODATA Recommended Values of the Fundamental
   Physical Constants 2022.

   https://physics.nist.gov/cuu/Constants/
```

见 [\_\_init\_\_.py:L324-L330](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/__init__.py#L324-L330)（定义 `[CODATA2022]` 参考条目：CODATA 2022 基本物理常数推荐值，附 NIST 官网链接）。

**兄弟文件 _codata.py 的呼应**——那里的模块 docstring 同样写明"These constants are taken from CODATA Recommended Values of the Fundamental Physical Constants 2022"，见 [_codata.py:L1-L9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L1-L9)（`_codata.py` 头部声明数据来自 CODATA 2022，是真正的"数据源文件"）。两处说法一致，构成完整的**数据溯源链**：用户文档 → `__init__.py` docstring → `_codata.py` 数据块 → CODATA 2022 / NIST。

> 顺带一提：`_codata.py` 头部还保留了一句历史信息——"the 2018 CODATA recommended values ... became available on 20 May 2019"（见 [_codata.py:L20-L24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/constants/_codata.py#L20-L24)）。这是文档未及时随版本完全更新的痕迹，阅读源码时以 `[CODATA2022]` 与 `_current_constants` 为准。

#### 4.4.4 代码实践

**实践目标**：搞清"一个常数的值到底从哪来"，并学会核对版本。

**操作步骤**：

```python
import scipy.constants as C

# 1) 直接从 docstring 里看 CODATA 版本声明
print(CODATA_LINE := [ln for ln in C.__doc__.splitlines()
                      if "CODATA" in ln][:3])

# 2) 核对一个值的来源：光速是精确定义值，应当为 299792458
print("c =", C.c, "(SI 精确定义值，预期 299792458.0)")

# 3) 体会"版本可追溯"：用 find() 找到带版本关键词的键
print(C.find("Boltzmann"))
```

**需要观察的现象**：

- 第 1 步应打印出提及 CODATA 2022 的若干行，证明数据来源声明就在 docstring 里。
- 第 2 步 `c` 应等于 `299792458.0`。
- 第 3 步 `find("Boltzmann")` 会返回若干键名，其中包含 `"Boltzmann constant"`，这是 `physical_constants` 里的标准键。

**预期结果**：如上。具体打印行内容**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：docstring 里 `[CODATA2022]_`（带下划线）和 `.. [CODATA2022]`（带 `..`）分别是什么？

**答案**：`[CODATA2022]_` 是**引用**（在正文中使用，末尾下划线表示"指向某处定义"）；`.. [CODATA2022]` 是**定义**（在 References 段给出完整出处）。这是 reStructuredText 的脚注/引用语法，Sphinx 会自动把二者链接。

**练习 2**：为什么科研代码里强调引用 CODATA 的**版本年份**？

**答案**：因为物理常数的推荐值会随测量进步而更新（CODATA 约每 4 年发布一次）。同一公式用 2014 版和 2022 版常数算出的结果会有微小差异。标注版本年份，是为了让计算结果**可复现、可追溯**——别人能用同一版本常数复现你的数值。

---

## 5. 综合实践

把本讲四大模块串起来，完成下面这个小任务。

**任务：编写一个"scipy.constants 体检报告"脚本。**

要求脚本完成以下事项，并把你观察到的结论写在注释里：

1. 导入 `scipy.constants`，打印其 CODATA 数据来源版本（从 `__doc__` 里提取含 "CODATA" 的行）。
2. 打印 `pi`、`c`、`speed_of_light`，并断言 `c == speed_of_light`。
3. 从 `physical_constants` 中取出 `"speed of light in vacuum"` 与 `"Boltzmann constant"`，分别打印三元组，并指出哪一个的 uncertainty 为 `0.0`、为什么。
4. 统计 `__all__` 的总长度，并报告其中有多少个名字以小写字母开头（提示：应接近 100%，因为公开名都不以 `_` 开头）。
5. 找一个 CODATA 常数名（如 `"Stefan-Boltzmann constant"`），用 `find()` 反查它，确认该名字确实出现在 `find()` 的返回列表里。

参考实现框架（**示例代码**，需本地运行验证）：

```python
import scipy.constants as C

# 1) 数据来源版本
codata_lines = [ln.strip() for ln in C.__doc__.splitlines() if "CODATA" in ln]
print("[1] CODATA 来源声明：", codata_lines[:2])

# 2) 数学/物理常数
assert C.c == C.speed_of_light, "c 与 speed_of_light 必须相等"
print("[2] c == speed_of_light =", C.c)

# 3) physical_constants 三元组
for key in ["speed of light in vacuum", "Boltzmann constant"]:
    v, u, unc = C.physical_constants[key]
    print(f"[3] {key}: value={v}, unit={u}, uncertainty={unc}")

# 4) __all__ 统计
print("[4] __all__ 总数 =", len(C.__all__))
lower_started = sum(1 for s in C.__all__ if s[:1].islower())
print("    以小写开头的 =", lower_started)

# 5) find() 反查
hits = C.find("Stefan-Boltzmann")
print("[5] find('Stefan-Boltzmann') 命中：", hits)
assert "Stefan-Boltzmann constant" in hits
```

**自我检查问题**（写在脚本注释里）：

- 第 3 步里，哪个常数 uncertainty 为 `0.0`？为什么？
- 第 4 步的 `__all__` 总数，是否包含了 `physical_constants` 字典里那几百个常数名？为什么？（提示：不包含，因为 `__all__` 只列**顶层单值名**，字典本身算 1 个名字。）

> 说明：以上为示例代码，运行结果中涉及的具体数值与计数请**本地验证**。

---

## 6. 本讲小结

- `scipy.constants` 是 SciPy 里专门管"常数与单位"的子包，提供**三大能力**：数学常数、物理常数与单位换算因子、CODATA 物理常数数据库。
- `__init__.py` 的**模块 docstring** 是它的"产品说明书"，用"表格（列常量/因子）+ autosummary（列函数）"两件套罗列全部对外名字。
- `physical_constants` 是一个字典，格式为 `name -> (value, unit, uncertainty)`；它在 `_codata.py` 里由六个 CODATA 版本合并而成，当前生效版本是 **2022**。
- `__all__` **自动生成**：`[s for s in dir() if not s.startswith('_')]`，靠"下划线=私有"约定收敛公开 API；`__doc__` 则用 `%(constant_names)s` 占位符在运行时填入常数表。
- 所有物理常数都标注来源 **CODATA 2022**（参考文献 `[CODATA2022]`，NIST 官网），保证结果可复现、可追溯。
- 文件名以 `_` 开头（`_constants.py`、`_codata.py`）是"内部实现"，`__init__.py` 通过 `import *` 把它们的公开符号提到顶层。

---

## 7. 下一步学习建议

本讲只是"俯瞰"。接下来建议按手册顺序推进：

- **下一篇 u1-l2《目录结构、构建与包导出》**：看 `meson.build` 如何把这些 `.py` 文件安装成子包，弄清 `_codata / _constants / codata / constants` 四个文件的分工，理解"私有文件 + 入口装配"的全貌。
- **u1-l3、u1-l4**：动手用 SI/二进制前缀和各类单位换算因子做计算，把"换算因子就是普通数字"这件事变成肌肉记忆。
- **进阶篇 u2**：深入 `_codata.py`，搞懂 CODATA 文本是怎么被**固定列宽解析**成字典的、精确常数如何被**计算回填**，这才是 constants 子包最有技术含量的部分。

> 建议阅读源码顺序：先把 `__init__.py`（本讲）通读一遍，再跳到 `_constants.py`（最短、最易读），最后挑战 `_codata.py`（最长、最核心）。
