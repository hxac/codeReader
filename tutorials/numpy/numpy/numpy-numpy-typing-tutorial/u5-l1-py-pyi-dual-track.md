# 运行时实现与桩文件双轨制（.py / .pyi / __init__.pyi）

> 单元 5 · 第 1 讲（u5-l1）· 阶段：advanced
> 依赖：u1-l3（PEP 561 与 `.pyi` 桩文件）、u2-l1（ArrayLike）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「同一个模块同时存在 `.py` 与 `.pyi`」时，类型检查器（mypy / pyright）到底读哪一个、忽略哪一个，以及为什么这样设计。
- 解释 `_ufunc.py`（运行时仅几行占位）与 `_ufunc.pyi`（数百行重载签名）为何能并存：运行时根本不需要那些丰富的类型类。
- 理解 `typing.type_check_only` 装饰器如何标记「只在类型检查时存在的幻影类」，以及它和运行时占位别名如何分工。
- 说清一个包为什么要有 `__init__.pyi`，以及 `numpy/typing/__init__.py`（运行时）与 `numpy/typing/__init__.pyi`（桩）各承担什么职责。

## 2. 前置知识

本讲承接 u1-l3 已经建立的两条结论，不再从头证明：

1. **`.pyi` 桩文件只含类型信息、无运行逻辑**；当 `.py` 与 `.pyi` 并存时，类型检查器**优先读 `.pyi`，并忽略 `.py` 的注解**。运行时（CPython 解释器）则永远只执行 `.py`。
2. **`py.typed` 标记**让 NumPy 成为「自带类型」的包，pip 安装即得类型。

本讲在这条链路上往前走一步：当 `.pyi`「压过」`.py` 之后，`.py` 还有什么用？为什么 NumPy 要为同一个模块**同时维护两份内容截然不同的文件**？核心是三个概念：

- **双轨制（dual-track）**：运行时一条轨（`.py`），类型检查一条轨（`.pyi`），两轨的「名字」必须对得上，但「内容」可以完全不同。
- **`type_check_only`**：`typing` 提供的装饰器，告诉类型检查器「这个类只在静态检查时存在，运行时不要指望它」。
- **`__init__.pyi`**：一个包目录的 `__init__.py` 对应的桩文件，给类型检查器一个干净的包级入口视图。

> 名词约定：本讲把 ufunc 称作「通用函数」（NumPy 对逐元素运算的向量化对象，如 `np.add`、`np.multiply`）；把 `.pyi` 里那些带丰富重载、仅用于类型检查的 `_UFunc_*` 类称作「幻影类」。

## 3. 本讲源码地图

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| [`numpy/_typing/_ufunc.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.py) | 5 | **运行时轨**：把三个名字别名为真实的 `ufunc` 类，仅作占位 |
| [`numpy/_typing/_ufunc.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi) | 828 | **类型检查轨**：一堆 `@type_check_only` 的 `ufunc` 子类 + 数百个 `@overload` |
| [`numpy/typing/__init__.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py) | 217 | **运行时轨**：模块文档、`__dir__`/`__getattr__`、文档拼接、`PytestTester` |
| [`numpy/typing/__init__.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.pyi) | 8 | **类型检查轨**：仅一行再导出 + `__all__`，给检查器一个干净入口 |
| [`numpy/_typing/__init__.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py) L141-L145 | — | 运行时把 `_ufunc` 的三个名字再导出，串起导入图 |
| [`numpy/__init__.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi) L108-L112, L7637+ | — | 顶层桩用 `_UFunc_Nin2_Nout1[...]` 给 `np.add` 等具体 ufunc 标注类型 |

## 4. 核心概念与源码讲解

### 4.1 .py / .pyi 双轨制：运行时与类型检查的分野

#### 4.1.1 概念说明

一个 Python 模块在「被运行」和「被类型检查」时，其实可以走两条完全不同的代码路径：

- **运行时**（`python -c "import numpy"`）：CPython 解释器查找 `.py` 文件并执行。它**完全不认识 `.pyi`**。
- **类型检查时**（`mypy your_code.py`）：检查器查找 `.pyi` 桩文件；一旦找到，它**只读 `.pyi`，完全忽略同名 `.py`**。

这就形成「双轨」：同一个模块名（如 `numpy._typing._ufunc`）对应两份实现，运行时跑 `.py`、检查时看 `.pyi`。两轨唯一的硬性约束是**对外暴露的名字必须对得上**——否则检查时认得的名字，运行时会因为找不到而 `ImportError`，反之亦然。

为什么要这样切？因为「运行时需要的」和「类型检查想要的」往往南辕北辙：

- 运行时只需要**真实存在、能被实例化和调用**的对象。
- 类型检查想要的是**精确到每个重载分支的签名**，哪怕这些签名对应的「类」在运行时根本不需要以独立形态存在。

NumPy 的 ufunc 是这种张力的极端样本：每个 ufunc（如 `np.add`）在运行时都是**同一个 `numpy.ufunc` 类**的实例，运行时无所谓「几入几出」的子类型；但类型检查为了让 `np.add(np.array(...), np.array(...))` 推断成数组、`np.add(1, 2)` 推断成标量，需要**几十条 `@overload`**。把这些重载塞进 `ufunc` 这一个类的运行时定义既无必要也无可读性，于是 NumPy 把它们放进桩文件里的「幻影子类」中。

#### 4.1.2 核心流程

双轨的解析流程可以这样画成两条平行链路：

```
【运行时链路】
import numpy._typing._ufunc
   → CPython 读 _ufunc.py
   → 执行 _UFunc_Nin2_Nout1 = ufunc  （三个名字都指向同一个 ufunc 类）
   → _typing/__init__.py 用 `from ._ufunc import X as X` 再导出
   → 名字在运行时真实存在（值就是 ufunc）

【类型检查链路】
mypy 解析 numpy.add 的标注
   → 读 numpy/__init__.pyi：add: _UFunc_Nin2_Nout1[L["add"], L[22], L[0]]
   → _UFunc_Nin2_Nout1 来自 numpy._typing
   → 读 numpy/_typing/_ufunc.pyi（.pyi 压过 .py）
   → 看到丰富的 @type_check_only 子类与 @overload
   → 按输入类型匹配重载，推断返回类型
```

两条链路在「名字」上交汇（`_UFunc_Nin2_Nout1` 在两边都存在），在「内容」上分家（一边是裸别名，一边是幻影类）。这就是双轨制的全部精髓。

#### 4.1.3 源码精读

**运行时轨**——整个 [`_ufunc.py` 仅 5 行](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.py#L1-L5)：

```python
from numpy import ufunc

_UFunc_Nin2_Nout1 = ufunc
_UFunc_Nin2_Nout2 = ufunc
_GUFunc_Nin2_Nout1 = ufunc
```

三个名字全部赋值为真实的 `ufunc` 类。这三行只是为了让 `numpy._typing.__init__` 的再导出能成功导入（[再导出代码 L141-L145](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py#L141-L145) 在 `numpy/_typing/__init__.py`）：

```python
from ._ufunc import (
    _GUFunc_Nin2_Nout1 as _GUFunc_Nin2_Nout1,
    _UFunc_Nin2_Nout1 as _UFunc_Nin2_Nout1,
    _UFunc_Nin2_Nout2 as _UFunc_Nin2_Nout2,
)
```

注意 `import X as X` 的写法——这是 PEP 484 规定的「显式再导出」标记：表示这些名字是 `_typing` 包**有意对外暴露**的（即便它们以 `_` 开头），类型检查器据此把它们计入 `from numpy._typing import ...` 的可解析范围。运行时这等价于普通 `import`，但语义上声明了「这是公共再导出，不是内部使用」。

**类型检查轨**——[`_ufunc.pyi` 的开头 L1-L7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L1-L7) 自报家门：

```python
"""A module with private type-check-only `numpy.ufunc` subclasses.

The signatures of the ufuncs are too varied to reasonably type
with a single class. So instead, `ufunc` has been expanded into
four private subclasses, one for each combination of
`~ufunc.nin` and `~ufunc.nout`.
"""
```

这份桩文件把 `ufunc` 拆成多个子类（按 `nin`/`nout` 组合），每个子类挂着大量 `@overload`。然后**顶层桩 [`numpy/__init__.pyi` L108-L112](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L108-L112) 把具体 ufunc 标注成这些子类的实例**（节选自 L7637+）：

```python
add: _UFunc_Nin2_Nout1[L["add"], L[22], L[0]]
multiply: _UFunc_Nin2_Nout1[L["multiply"], L[23], L[1]]
matmul: _GUFunc_Nin2_Nout1[L["matmul"], L[19], None, L["(n?,k),(k,m?)->(n?,m?)"]]
```

于是类型检查器看到 `np.add` 时，匹配 `_UFunc_Nin2_Nout1.__call__` 的某条重载，给出精确返回类型；而运行时 `type(np.add)` 永远是 `<class 'numpy.ufunc'>`。**两轨各行其是，互不干扰。**

> 关键点：因为 `.pyi` 存在，检查器**根本不会**读 `_ufunc.py` 的那 5 行。即便你把丰富类型写进 `.py`，检查器也看不见。所以丰富类型**必须**放进 `.pyi`；而 `.py` 又**必须**提供那三个名字（否则运行时导入失败）。这正是双轨「内容分家、名字对齐」的必然结果。

#### 4.1.4 代码实践

**实践目标**：亲手验证「运行时三个名字只是 `ufunc` 的别名」，并体会「同一个模块名在两轨下的内容差异」。

**操作步骤**：

1. 写一个脚本 `dual_track_probe.py`：

```python
# 示例代码（非项目原有）
import numpy as np
from numpy._typing import _ufunc as _u

# (1) 运行时这三个名字是什么？
print("_UFunc_Nin2_Nout1 is np.ufunc :", _u._UFunc_Nin2_Nout1 is np.ufunc)
print("_UFunc_Nin2_Nout2 is np.ufunc :", _u._UFunc_Nin2_Nout2 is np.ufunc)
print("_GUFunc_Nin2_Nout1 is np.ufunc:", _u._GUFunc_Nin2_Nout1 is np.ufunc)

# (2) 运行时 np.add 的真实类型
print("type(np.add)                  :", type(np.add))
print("isinstance(np.add, np.ufunc)  :", isinstance(np.add, np.ufunc))

# (3) 运行时它们并没有 nin/nout 之类的「子类」差异
print("np.add.nout, np.multiply.nout :", np.add.nout, np.multiply.nout)
```

2. 运行 `python dual_track_probe.py`。

**需要观察的现象**：三个 `is np.ufunc` 比较应全部为 `True`；`type(np.add)` 应为 `numpy.ufunc`（而不是某个 `_UFunc_Nin2_Nout1` 子类）。

**预期结果**：

```
_UFunc_Nin2_Nout1 is np.ufunc : True
_UFunc_Nin2_Nout2 is np.ufunc : True
_GUFunc_Nin2_Nout1 is np.ufunc: True
type(np.add)                  : <class 'numpy.ufunc'>
isinstance(np.add, np.ufunc)  : True
np.add.nout, np.multiply.nout : 1 1
```

3. （可选）用命令对比两份文件体量（路径按你的环境调整）：

```bash
wc -l numpy/_typing/_ufunc.py numpy/_typing/_ufunc.pyi
```

**结论**：运行时 `_ufunc` 模块里**根本没有** `_UFunc_Nin2_Nout1` 这个「子类」——它就是 `ufunc`。那 828 行丰富的类型定义只活在类型检查器读取的 `.pyi` 里。这就是双轨制最直接的证据。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `_ufunc.pyi` 删掉（只留 `_ufunc.py`），类型检查器对 `np.add(1, 2)` 的返回类型推断会发生什么变化？

**参考答案**：检查器会改读 `_ufunc.py`，于是 `_UFunc_Nin2_Nout1` 退化成裸 `ufunc` 别名，`np.add` 被当作普通 `ufunc`。它失去了那几十条 `__call__` 重载，无法再区分「标量进→标量出」与「数组进→数组出」，返回类型会变得粗糙（往往退化为 `Any` 或最宽的联合）。运行时不受任何影响——因为运行时本来就没读过 `.pyi`。

**练习 2**：为什么 `_typing/__init__.py` 要用 `from ._ufunc import X as X`（带 `as X`），而不是 `from ._ufunc import X`？

**参考答案**：`import X as X` 是 PEP 484 的显式再导出语法，向类型检查器声明「`X` 是本包对外提供的名字，`from numpy._typing import X` 应当解析得到它」。若只写 `import X`，部分检查器会把它当作「内部使用、不对外再导出」，从而在顶层桩 `from numpy._typing import _UFunc_Nin2_Nout1` 处报「未导出」错误。运行时两者等价，差别只在类型检查语义。

---

### 4.2 type_check_only：只在类型检查时存在的「幻影类」

#### 4.2.1 概念说明

`typing.type_check_only` 是一个装饰器，语义是：**被它标记的类只在静态类型检查时存在，运行时不要依赖它**。它通常出现在 `.pyi` 桩文件里，用来声明那些「为了给类型系统看而虚构出来的类」。

为什么需要它？因为类型系统有时想用一个**运行时根本不存在的类**来表达精确的类型。例如：

- 你想让检查器认为 `np.add` 的类型是「一个 nin=2、nout=1 的特殊 ufunc」，但运行时根本没有这种子类，所有 ufunc 都是同一个 `ufunc`。
- 你虚构出一个 `_UFunc_Nin2_Nout1` 类，把重载挂在它身上，然后用 `@type_check_only` 告诉检查器：「这是幻影，别让用户去实例化它，也别假设它运行时存在」。

配合双轨制，效果是：`.pyi` 里有幻影类（检查器看得见），`.py` 里有同名占位别名（运行时用）。`type_check_only` 是这出戏的「免责声明」。

#### 4.2.2 核心流程

幻影类的生命周期：

```
写桩：在 _ufunc.pyi 里
  @type_check_only
  class _UFunc_Nin2_Nout1[...](ufunc): ...（带 @overload）
        ↓
检查器：把 _UFunc_Nin2_Nout1 当成「真实但仅供检查」的类型，
       允许 np.add: _UFunc_Nin2_Nout1[...] 这样的标注，并按重载推断
        ↓
运行时：_UFunc_Nin2_Nout1 这个名字也存在（来自 _ufunc.py），但它的值是 ufunc；
       幻影类的重载签名在运行时本就不存在（@overload 在运行时被擦除）
```

`type_check_only` 的约束主要面向**用户代码**：如果有人在自己的 `.py` 里写 `from numpy._typing._ufunc import _UFunc_Nin2_Nout1` 并试图 `isinstance(x, _UFunc_Nin2_Nout1)` 或实例化它，类型检查器会警告「这是 type_check_only 的类，不应在运行时使用」。这是一种**契约**，而非运行时强制——Python 解释器并不阻止你，但类型检查器会提醒你违背了设计意图。

#### 4.2.3 源码精读

`_ufunc.pyi` 中**每一个类**都标了 `@type_check_only`。第一个出现的是协议 [`_SupportsArrayUFunc` L42-L50](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L42-L50)：

```python
@type_check_only
class _SupportsArrayUFunc(Protocol):
    def __array_ufunc__(
        self,
        ufunc: ufunc,
        method: Literal["__call__", "reduce", "reduceat", "accumulate", "outer", "at"],
        *inputs: Any,
        **kwargs: Any,
    ) -> Any: ...
```

主力幻影类 [`_UFunc_Nin2_Nout1` L77-L94](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L77-L94) 继承自真实的 `ufunc`，把 `nin`/`nout` 钉死成 `Literal`：

```python
@type_check_only
class _UFunc_Nin2_Nout1[NameT: LiteralString, NTypesT: int, IdentT](ufunc):
    @property
    def __name__(self) -> NameT: ...
    @property
    def nin(self) -> Literal[2]: ...
    @property
    def nout(self) -> Literal[1]: ...
    @property
    def nargs(self) -> Literal[3]: ...
    ...
```

它的 [`__call__` 挂着多条 `@overload` L96-L106](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L96-L106)，区分「标量进、标量出」与「数组进、数组出」。第一条（标量分支）：

```python
    @overload  # (scalar, scalar) -> scalar
    def __call__(
        self,
        x1: _ScalarLike_co,
        x2: _ScalarLike_co,
        /,
        out: EllipsisType | None = None,
        *,
        dtype: DTypeLike | None = None,
        **kwds: Unpack[_UFunc3Kwargs],
    ) -> Incomplete: ...
```

> 注意这里的 `ArrayLike`、`DTypeLike`、`_ScalarLike_co` 都来自 u2-l1/u2-l2 讲过的私有别名（`from ._array_like import ArrayLike, NDArray, ...`、`from ._dtype_like import DTypeLike`、`from ._scalars import _ScalarLike_co`，见 [`_ufunc.pyi` 导入区 L29-L31](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L29-L31)）。本讲的关注点是「这些丰富签名只活在 `.pyi`」。

`type_check_only` 还配合 `Never`/`NoReturn` 表达「某些方法对这个子类不可用」。例如两输出的 [`_UFunc_Nin2_Nout2` L330-L334](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L330-L334) 把 `reduce`/`accumulate` 等的返回类型标成 `NoReturn`：

```python
    def accumulate(self, array: Never, /) -> NoReturn: ...  # type: ignore[override]
    def reduce(self, array: Never, /) -> NoReturn: ...  # type: ignore[override]
    def reduceat(self, array: Never, /, indices: Never) -> NoReturn: ...
    def outer(self, A: Never, B: Never, /) -> NoReturn: ...
    def at(self, a: Never, indices: Never, b: Never, /) -> NoReturn: ...
```

含义：对于「两入两出」的 ufunc，`reduce` 等方法在运行时会抛 `ValueError`（注释见 [`_ufunc.pyi` L65-L70](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L65-L70)）；类型系统用 `NoReturn` 把这种「调用即错误」编码进签名，让检查器在你写「对两输出 ufunc 调 reduce」时就能报警。这些精巧的建模全靠幻影类承载——而它们**全都**只存在于 `.pyi`。

#### 4.2.4 代码实践

**实践目标**：用一个 `.pyi` 实验，直观感受 `@type_check_only` 类对类型检查器「可见」、对运行时「并非真实子类」的差异。

**操作步骤**：

1. 新建目录 `play/`，放两份文件（示例代码，非项目原有）。

`play/_demo.py`（运行时轨，提供同名占位）：

```python
class _Real:            # 运行时真实存在的类
    pass

_Other = _Real          # 运行时占位：_Other 就是 _Real
```

`play/_demo.pyi`（类型检查轨，虚构一个幻影类）：

```python
from typing import type_check_only

@type_check_only
class _Other:
    def hi(self) -> str: ...

def make() -> _Other: ...
```

2. 写 `play/probe.py`：

```python
from play import _demo
x = _demo.make()
print(x.hi())          # 类型检查器认为 x 是幻影 _Other，有 hi 方法
```

3. 分别用运行时和类型检查器跑：

```bash
python -c "from play import _demo; print(_demo._Other is _demo._Real)"  # 运行时：True
mypy play/probe.py    # 检查器：按 .pyi 推断，x.hi() 合法
```

**需要观察的现象**：运行时 `_demo._Other` 就是 `_Real`（没有 `hi` 方法）；但类型检查器因为读 `.pyi`，会认为 `x` 是幻影 `_Other`、`hi()` 存在且返回 `str`。

**预期结果**：

- 运行时比较输出 `True`。
- mypy 对 `probe.py` 不报错（因为按 `.pyi`，`hi` 存在）；但若你直接写 `isinstance(x, _demo._Other)` 之类依赖幻影运行时形态的代码，检查器会提示 `_Other` 是 `type_check_only`。
- 若实际执行 `python play/probe.py`（真去调 `x.hi()`），会因为运行时 `_Other` 实为 `_Real`、无 `hi` 方法而 `AttributeError`——这正是「幻影类只存在于类型检查」的可观察后果。

> 结论：`type_check_only` + 双轨 = 给类型检查器一套精确的「虚构类型」，同时用运行时占位维持导入图合法。两轨名字相同、内容不同。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 `_UFunc_Nin2_Nout1` 这些丰富类写进 `_ufunc.py`（运行时文件），而要单独放到 `.pyi`？

**参考答案**：两个原因。其一（决定性）：只要 `_ufunc.pyi` 存在，类型检查器**只读 `.pyi`、忽略 `.py`**，所以写在 `.py` 里的丰富类型检查器根本看不到，必须放进 `.pyi`。其二：把这些虚构类放进运行时 `.py` 会在导入时真正创建这些类对象，徒增运行时开销与维护负担，还会诱导用户误以为它们是可用的运行时类；放进 `.pyi` 并标 `@type_check_only`，既满足检查器，又保持运行时干净。

**练习 2**：`_UFunc_Nin2_Nout2.reduce` 的返回类型为何是 `NoReturn`？这和「运行时不调用就不会报错」矛盾吗？

**参考答案**：`NoReturn`（`Never`）是类型层面对「这个调用永远不会正常返回」的编码——对两输出 ufunc 调 `reduce` 在运行时会抛 `ValueError`。它不矛盾：`NoReturn` 不是说「运行时不能写这行代码」，而是告诉类型检查器「凡是在这个子类上调用 `reduce` 的代码，正常流程之后都不可达」。这样检查器能在静态阶段就把这类「调用即错误」暴露给用户，而不是等到运行时炸。

---

### 4.3 __init__.pyi：包级桩文件的意义

#### 4.3.1 概念说明

一个包目录里，`__init__.py` 是运行时入口；`__init__.pyi` 是它对应的桩。和普通模块一样，**当两者并存时，类型检查器读 `__init__.pyi`、忽略 `__init__.py`**。

那为什么一个包要特意配一份 `__init__.pyi`？因为运行时的 `__init__.py` 常常塞满**类型检查器不关心、甚至难以理解**的东西：

- 动态的模块级 `__getattr__` / `__dir__`（PEP 562）——它们让运行时按需返回名字，但检查器很难精确建模。
- 运行时才执行的文档拼接（`__doc__ += ...`）。
- 测试入口（如 `PytestTester`）。
- 各种 `del`、条件分支、副作用代码。

把这些原样交给类型检查器，既增加解析负担，也可能让检查器对「包对外暴露什么」给出错误或混乱的结论。一份精简的 `__init__.pyi` 则给检查器一个**干净、显式、最小**的包级契约：再导出哪些名字、`__all__` 是什么。`numpy.typing` 正是这么做的。

#### 4.3.2 核心流程

`numpy.typing` 包的两轨分工：

```
运行时：import numpy.typing
  → 执行 numpy/typing/__init__.py（217 行）
  → 建立模块文档、注册 __dir__/__getattr__、拼接 _docstrings、挂上 PytestTester
  → 对外提供 ArrayLike / DTypeLike / NBitBase / NDArray

类型检查：解析 `import numpy.typing as npt`
  → 读 numpy/typing/__init__.pyi（8 行）
  → 只看到：from numpy._typing import (四个名字) + __all__
  → 干净地把 numpy.typing 等同于那四个公共别名
```

两轨的 `__all__` 必须一致（这是 u1-l3 已强调的约束），否则检查时和运行时对「公共面」的认知会错位。

#### 4.3.3 源码精读

**运行时轨** [`numpy/typing/__init__.py` L175-L177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175-L177)，公共面只有一行再导出：

```python
from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray

__all__ = ["ArrayLike", "DTypeLike", "NBitBase", "NDArray"]
```

但运行时这份文件还干了许多别的事：模块级 [`__dir__`/`__getattr__`（PEP 562）L184-L204](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L184-L204) 实现 `NBitBase` 的延迟 `DeprecationWarning` 与白名单访问控制（这部分是 u5-l4 的主题，这里只指出「它是运行时动态逻辑」）；运行时 [`文档拼接与测试入口 L207-L216`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L207-L216)：

```python
if __doc__ is not None:
    from numpy._typing._add_docstring import _docstrings
    __doc__ += _docstrings
    __doc__ += '\n.. autoclass:: numpy.typing.NBitBase\n'
    del _docstrings

from numpy._pytesttester import PytestTester

test = PytestTester(__name__)
del PytestTester
```

这些 `__doc__ +=`、`del`、`PytestTester` 全是运行时设施，类型检查器既不需要、也不该据此推断 `numpy.typing` 的对外类型。

**类型检查轨** [`numpy/typing/__init__.pyi` 全文 L1-L8](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.pyi#L1-L8) 极简：

```python
from numpy._typing import (  # type: ignore[deprecated]
    ArrayLike,
    DTypeLike,
    NBitBase,
    NDArray,
)

__all__ = ["ArrayLike", "DTypeLike", "NBitBase", "NDArray"]
```

它给检查器的信息只有两点：四个公共名字来自 `numpy._typing`；`__all__` 列出它们。注意 `# type: ignore[deprecated]`——`numpy._typing` 是私有/已弃用的模块，从它导入会被检查器标记弃用；公共壳在桩里用 `type: ignore` 显式抑制这条警告（运行时 `.py` 不需要这个注释，因为运行时不做这类静态弃用检查）。

> 关键点：因为存在 `__init__.pyi`，检查器**根本不读**那份 217 行的 `__init__.py`，因此 `__getattr__` 的动态分发、文档拼接、`PytestTester` 都不会干扰检查器对 `numpy.typing` 公共面的认知。两份 `__all__` 一致，保证「检查时」与「运行时」对外暴露的四个名字完全相同。

#### 4.3.4 代码实践

**实践目标**：通过「缺失 `__init__.pyi`」的对照实验，体会包级桩文件对类型检查器可见公共面的影响。

**操作步骤**：

1. 新建一个最小包 `pkg/`（示例代码，非项目原有）。

`pkg/__init__.py`（只有运行时轨，故意放一个动态名字）：

```python
from typing import Any

def __getattr__(name: str) -> Any:   # PEP 562：运行时按需返回
    if name == "Hidden":
        return 42
    raise AttributeError(name)

__all__ = ["Visible"]
Visible = 1
```

2. 写 `use.py`：

```python
import pkg
print(pkg.Visible)   # 1
print(pkg.Hidden)    # 运行时：42（来自 __getattr__）
```

3. 先不加 `__init__.pyi`，跑 `mypy use.py`；然后在 `pkg/` 下加一份 `pkg/__init__.pyi`：

```python
Visible: int
__all__ = ["Visible"]
```

再跑 `mypy use.py`，对比两次对 `pkg.Hidden` 的报告。

**需要观察的现象**：

- 没有 `__init__.pyi` 时，检查器读 `__init__.py`，看到 `__getattr__` 返回 `Any`，对 `pkg.Hidden` 可能不报错但类型是 `Any`（不同检查器行为略有差异）。
- 加了 `__init__.pyi` 后，检查器只认桩里声明的 `Visible`，`pkg.Hidden` 会被报为「模块 pkg 没有属性 Hidden」——因为桩**压过**了 `.py`，动态 `__getattr__` 被「无视」。

**预期结果**：包级桩让 `numpy.typing` 这类包能把「检查器可见的公共面」收窄到一份显式清单，屏蔽运行时的动态魔法。这正是 `numpy/typing/__init__.pyi` 存在的意义：给检查器一个干净的、只有四个名字的入口。

> 待本地验证：不同 mypy / pyright 版本对无桩时 `__getattr__` 的处理细节可能不同；重点观察「加桩前后 `pkg.Hidden` 报告的差异」，而非具体报错文案。

#### 4.3.5 小练习与答案

**练习 1**：`numpy/typing/__init__.pyi` 里那行 `# type: ignore[deprecated]` 解决了什么问题？为什么运行时的 `__init__.py` 不需要它？

**参考答案**：`numpy._typing` 是私有且被标记为已弃用的模块，类型检查器会对 `from numpy._typing import ...` 报弃用警告；公共壳 `numpy.typing` 必须从它搬运四个公共别名，于是在桩里用 `# type: ignore[deprecated]` 抑制这条静态警告。运行时的 `__init__.py` 不需要这个注释，因为「弃用」是类型检查器层面的概念，CPython 运行时不会基于静态标记发弃用警告（`numpy.typing` 的 `NBitBase` 弃用是另一套——靠运行时 `__getattr__` 主动 `warnings.warn`，见 u5-l4）。

**练习 2**：如果有人改了运行时 `__init__.py` 的 `__all__`（比如新增一个名字），却忘了同步 `__init__.pyi`，会出现什么不一致？

**参考答案**：会出现「运行时可见、类型检查不可见」的错位——用户 `python -c "import numpy.typing as npt; print(npt.NewName)"` 能拿到，但 `mypy` 会报 `numpy.typing` 没有该属性（因为检查器只读桩）。反之亦然。这就是为什么 u1-l3 强调两份 `__all__` 必须一致，也是 `numpy/typing/tests/test_isfile.py` 这类完整性测试存在的意义（保证桩文件随包安装、两轨契约对齐）。

---

## 5. 综合实践

把本讲三个最小模块（双轨制、`type_check_only`、`__init__.pyi`）串起来，做一个「全链路追踪」任务。

**任务**：追踪类型检查器解析 `np.add(a, b)` 返回类型的完整路径，并解释每一步分别读了哪条轨的哪个文件。

**操作步骤**：

1. 写 `trace_add.py`（示例代码）：

```python
import numpy as np

a = np.array([1, 2, 3])
b = np.array([4, 5, 6])
c = np.add(a, b)
reveal_type(c)        # mypy 专用：打印推断出的类型
```

2. 用 mypy 跑：`mypy trace_add.py`（NumPy 已自带类型，无需额外配置）。

3. 对照下面的「解析链」逐条核对，回答每一步读的是 `.py` 还是 `.pyi`：

| 步骤 | 符号 | 检查器读取的文件（轨） |
| --- | --- | --- |
| ① `np.add` 的类型 | `add: _UFunc_Nin2_Nout1[L["add"], L[22], L[0]]` | `numpy/__init__.pyi`（桩） |
| ② `_UFunc_Nin2_Nout1` 从哪来 | `from numpy._typing import _UFunc_Nin2_Nout1` | `numpy/_typing/__init__.py` 的再导出 |
| ③ 该名字的真实类型定义 | `@type_check_only class _UFunc_Nin2_Nout1(ufunc)` 及其 `@overload __call__` | `numpy/_typing/_ufunc.pyi`（桩，压过 `.py`） |
| ④ 匹配 `__call__` 重载 | `(array-like, array-like) -> array` 那条 | 同上 `_ufunc.pyi` |
| ⑤ 运行时实际执行 | `type(np.add) is numpy.ufunc`，C 实现逐元素相加 | `numpy/_typing/_ufunc.py`（运行时轨，三个名字 = `ufunc`） |

**需要观察的现象**：mypy 对 `reveal_type(c)` 应输出形如 `numpy.ndarray[Any, numpy.dtype[<某种标量>]]` 的数组类型（具体元素类型取决于 mypy 版本与推断策略）。

**结论**（写进你的笔记）：`np.add` 的精确返回类型**完全由桩文件链**（`numpy/__init__.pyi → _typing/__init__.py → _ufunc.pyi`）决定；运行时那条轨（`_ufunc.py` 的三行别名）只负责让导入图合法，对类型推断**毫无贡献**。你能复述这条链，就真正理解了双轨制。

> 待本地验证：`reveal_type` 的确切输出文案随 mypy 版本变化，重点是「它是一个数组类型，且来自桩里的重载匹配」，而非具体文案。

## 6. 本讲小结

- **双轨制**：同一模块名同时有 `.py`（运行时执行）与 `.pyi`（类型检查读取），检查器**优先读 `.pyi`、忽略 `.py`**；两轨「名字对齐、内容分家」。
- **`_ufunc` 是双轨的极端样本**：运行时 [`_ufunc.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.py) 仅 3 行别名（三个名字都 = `ufunc`）；类型检查轨 [`_ufunc.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi) 是 828 行的 `@type_check_only` 子类与数百个 `@overload`。运行时根本不需要那些丰富的类型类。
- **`type_check_only`**：`typing` 装饰器，标记「只在类型检查时存在的幻影类」；配合 `.py` 里的同名占位别名，既满足检查器的精确建模，又保持运行时干净。
- **`__init__.pyi`**：包级桩文件给检查器一个干净、显式、最小的包入口视图，屏蔽运行时 `__init__.py` 里的 `__getattr__`/`__dir__`、文档拼接、`PytestTester` 等动态逻辑；两份 `__all__` 必须一致。
- **顶层消费**：`numpy/__init__.pyi` 用 `_UFunc_Nin2_Nout1[...]` 给 `np.add` 等具体 ufunc 标注类型，这些标注只在桩里、运行时不存在。
- **设计哲理**：运行时只要「真实可调用」的对象，类型检查想要「精确到每个重载」的签名；双轨制让两者各得其所，互不拖累。

## 7. 下一步学习建议

- **u5-l2（ufunc 的类型建模：`_ufunc.pyi` 详解）**：本讲只点到 `_UFunc_Nin2_Nout1` 的存在，下一讲深入拆解它的 `@overload` 决策树、`Never`/`NoReturn` 的「不可用」编码，以及 `Unpack[TypedDict]` 的 kwargs 建模。
- **u5-l4（模块级 `__getattr__` 与延迟弃用）**：本讲提到运行时 `__init__.py` 用 PEP 562 的 `__getattr__`/`__dir__` 收窄公共面并延迟 `NBitBase` 弃用，那里有完整的动态分发逻辑值得细读。
- **延伸阅读源码**：对照 [`numpy/_typing/_ufunc.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi) 与顶层 [`numpy/__init__.pyi` L7637+](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L7637-L7653)，亲手把 `np.multiply`、`np.matmul` 的标注映射回对应的幻影类，巩固「桩文件消费链」。
