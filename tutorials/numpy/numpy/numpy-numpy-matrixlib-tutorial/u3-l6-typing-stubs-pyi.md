# 类型存根 defmatrix.pyi：为 matrix 子类提供静态类型信息

## 1. 本讲目标

本讲只盯住一个文件：`numpy/matrixlib/defmatrix.pyi`——`matrix`/`asmatrix`/`bmat` 的**类型存根（type stub）**。它是 `defmatrix.py` 的「静态类型影子」，不含任何运行时逻辑，只描述「函数收什么参数、返回什么类型」，供 pyright / mypy 这类静态检查器消费。

学完后你应该能够：

1. 读懂 `class matrix(np.ndarray[_ShapeT_co, _DTypeT_co])` 这行泛型签名，并解释为什么 `_ShapeT_co` 被绑死到 `_2D` 是「永远二维」这一运行时不变量的**类型层编码**。
2. 看懂 `sum`/`prod`/`mean`/`std`/`var` 等归约方法为何要用 **4 个 `@overload`** 才能完整表达 `axis` 与 `out` 的组合，并理解返回类型如何在「标量 / 二维 matrix / 透传 out」之间分流。
3. 区分 `_Matrix[Incomplete]` 与 `matrix[_2D, _DTypeT_co]` 这两种写法的语义差别：前者「形状确定、dtype 未知」，后者「形状与 dtype 都跟随调用者」。
4. 用 pyright 或 mypy 对一段 `np.matrix` 脚本做类型检查，读懂 `reveal_type` 输出，并对照存根解释推断结果。

本讲依赖你已经学过 [u2-l1 构造函数](#)、[u3-l2 归约方法与 `_collapse`/`_align`](#)、[u3-l3 矩阵属性 T/H/I/A/A1](#)：存根里几乎所有签名，都是在用类型语言「复述」那几讲讲过的运行时行为。

## 2. 前置知识

在进入源码前，先用大白话把几个静态类型概念过一遍。

- **类型存根（`.pyi` 文件）**：一个只写「签名」（参数类型、返回类型）、不写实现的文件。同名 `.py` 与 `.pyi` 并存时，类型检查器**优先读 `.pyi`**、忽略 `.py` 里的真实实现。NumPy 通过在包里随附 `.pyi` 来声明「我是带类型的」（[PEP 561](https://peps.python.org/pep-0561/) 的「partial stubs in package」模式）。所以 `defmatrix.pyi` 不参与运行，只参与静态检查。
- **TypeVar（类型变量）**：泛型的「占位符」。`T = TypeVar("T")` 之后，`def f(x: T) -> T` 表示「返回类型与入参类型一致」。带 `covariant=True` 表示协变——若 `A` 是 `B` 的子类型，则 `Container[A]` 也是 `Container[B]` 的子类型。
- **`@overload`（函数重载）**：同一个函数名写多份签名、用 `...` 占位（`.pyi` 里所有函数体都是 `...`），最后（在 `.py` 里）只留一份真实实现。类型检查器按**从上到下**的顺序，挑第一个能与调用实参匹配的签名，其返回类型就是这次调用的推断结果。
- **PEP 695 `type` 语句**：Python 3.12 起可以用 `type Name = ...`、`type Name[Param] = ...` 直接定义类型别名（含泛型别名）。它比老式 `Name = ...` 赋值更清晰，且支持方括号参数化。NumPy 的 `pyproject.toml` 把下限设为 `requires-python = ">=3.12"`（见 [pyproject.toml:L16](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/pyproject.toml#L16)），所以 `defmatrix.pyi` 里大量使用 PEP 695 的 `type` 语句是安全的。
- **泛型类 `np.ndarray`**：NumPy 把 `ndarray` 声明成一个带两个类型参数的泛型类 `class ndarray(Generic[_ShapeT_co, _DTypeT_co])`（见 [numpy/__init__.pyi:L2142](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.pyi#L2142)），第一个参数编码「形状」、第二个编码「dtype」。`matrix` 作为它的子类，自然也继承这套两参数泛型。

> 小提示：如果你对 `@overload`、`TypeVar`、协变这些词还陌生，建议先花十分钟读 typing 文档再回来；本讲不会从零讲 Python 类型系统，但会讲清楚它们在 `defmatrix.pyi` 里**为什么这样用**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
|---|---|---|
| [numpy/matrixlib/defmatrix.pyi](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi) | `matrix`/`asmatrix`/`bmat` 的类型存根，本讲主角 | 泛型签名、`_2D`、`_Matrix`、归约 `overload`、`squeeze/ravel/flatten`、属性签名 |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | 真实运行时实现，用于「对照」存根签名是否如实复述行为 | `sum`、`squeeze`、`A`/`A1`、`__array_priority__` 等 |
| [numpy/__init__.pyi](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.pyi) | 顶层类型存根，定义泛型 `ndarray` 与 `_ArrayOrScalarCommon` | `class ndarray(Generic[_ShapeT_co, _DTypeT_co])`、基类 `T` 属性 |
| [numpy/_typing/_shape.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_typing/_shape.py) | 私有类型工具，定义 `_Shape`/`_AnyShape`/`_ShapeLike` | `_AnyShape`、`_ShapeLike` 的来源 |

## 4. 核心概念与源码讲解

### 4.1 .pyi 存根概览：PEP 561、PEP 695 与 Incomplete 哨兵

#### 4.1.1 概念说明

打开 `defmatrix.pyi` 的顶部，你会发现它和 `defmatrix.py` 长得一点也不像：没有 `import ast`、没有 `warnings.warn`、没有函数体，每行都以 `...` 结尾。这是 `.pyi` 的常态——它只负责**对外承诺**「我接受什么、我返回什么」。

顶部有四个值得单独认识的「积木」：

1. `from _typeshed import Incomplete`——从 typeshed 引入的**哨兵类型**。`Incomplete` 表示「这里有个类型，但存根作者不想/无法精确写出」。它和 `Any` 的区别在于：`Any` 是「任意类型，检查器完全放行」，而 `Incomplete` 更多是「这块签名还没补完，不要据此做强推断」。在 `defmatrix.pyi` 里它出现 23 次，几乎都用在「结果 dtype 取决于运行时输入、静态无法确定」的位置。
2. `from typing import ..., overload`——重载装饰器，归约方法全靠它。
3. `from typing_extensions import TypeVar`——注意是从 `typing_extensions` 而非 `typing` 引入 `TypeVar`，因为 NumPy 需要用到「带 `default` 的 TypeVar」（PEP 696），老标准库 `typing.TypeVar` 在 3.13 之前不支持 `default=`。
4. PEP 695 的 `type _2D = ...`、`type _Matrix[...] = ...`——类型别名语句。

#### 4.1.2 核心流程

`.pyi` 的存在价值可以用一句话概括：

> 类型检查器看到 `import numpy.matrixlib` 时，加载 `defmatrix.pyi`（而不是 `defmatrix.py`），用存根里的签名给用户代码做类型推断。

存根顶部的「类型积木」按以下顺序被消费：

```text
Incomplete  ──┐
TypeVar      ──┼──►  顶层 type 别名 (_2D, _Matrix, _ToIndex1/2)
overload     ──┤           │
numpy._typing ┘           ▼
                   class matrix(np.ndarray[_ShapeT_co, _DTypeT_co]): ...
                            │
                            ▼
              每个方法用 @overload 描述 (axis, out) 组合的返回类型
```

#### 4.1.3 源码精读

存根顶部的导入与公开清单：

[numpy/matrixlib/defmatrix.pyi:L1-L18](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L1-L18) —— 这一段引入 `Incomplete`、`overload`、`TypeVar`，并从 `numpy._typing` 批量搬运 `ArrayLike`/`DTypeLike`/`NDArray`/`_AnyShape`/`_ArrayLikeInt_co`/`_NestedSequence`/`_ShapeLike`，最后用 `__all__` 把公开名字限定为 `["asmatrix", "bmat", "matrix"]`（与运行时 `defmatrix.py` 顶部的 `__all__` 完全一致）。

`_ShapeLike` 的定义在私有工具模块里，非常简单：

[numpy/_typing/_shape.py:L4-L8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_typing/_shape.py#L4-L8) —— `_Shape = tuple[int, ...]`（任意长度整数元组）、`_AnyShape = tuple[Any, ...]`（连元素类型都不定的元组）、`_ShapeLike = SupportsIndex | Sequence[SupportsIndex]`（「能被当成形状」的类型，单个 `int` 或 `int` 序列都算）。归约方法的 `axis: _ShapeLike` 正是借用它来表达「axis 可以是 `0`、`1`、`(0,1)` 等」。

#### 4.1.4 代码实践

1. **实践目标**：确认 `.pyi` 不参与运行、确认 PEP 695 语法依赖 Python 3.12+。
2. **操作步骤**：
   - 打开 `defmatrix.pyi`，对比 `defmatrix.py`，确认前者没有任何可执行逻辑。
   - 在仓库根目录执行 `python -c "import sys; print(sys.version_info)"`，确认解释器 ≥ 3.12（满足 `pyproject.toml` 的 `requires-python`）。
   - （可选）在 Python 3.11 环境里 `python -c "type _2D = tuple[int, int]"`，观察它抛 `SyntaxError`，印证 PEP 695 `type` 语句的版本门槛。
3. **需要观察的现象**：`.pyi` 全是 `...` 占位；3.12 能解析 `type` 语句，3.11 不能。
4. **预期结果**：运行时 `np.matrix` 的行为来自 `.py`，静态类型来自 `.pyi`，二者各司其职。
5. 运行结果「待本地验证」（环境相关）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `defmatrix.pyi` 要从 `typing_extensions` 而不是 `typing` 引入 `TypeVar`？
  - **答案**：因为 `_ShapeT_co`/`_DTypeT_co` 用到了 `default=` 参数（PEP 696），旧版标准库 `typing.TypeVar` 不支持，只能借助 `typing_extensions`。
- **练习 2**：`Incomplete` 和 `Any` 在类型检查器眼里有什么本质区别？
  - **答案**：`Any` 表示「任意类型」，检查器对其完全放行、不做任何约束；`Incomplete` 是 typeshed 的约定记号，表示「这块签名尚未补完」，语义上更接近「不要基于此做强推断」。在本存根里，它专用于「dtype 取决于运行时输入、静态无法确定」的位置。

---

### 4.2 matrix 的泛型签名与 `_2D` 形状类型

#### 4.2.1 概念说明

本讲最核心的一行代码是 `class matrix(np.ndarray[_ShapeT_co, _DTypeT_co])`。要读懂它，得先看它**重新定义**的两个 TypeVar。

回忆 `numpy/__init__.pyi` 里 `ndarray` 自身的形状参数：`_ShapeT_co = TypeVar("_ShapeT_co", bound=_Shape, default=_AnyShape, covariant=True)`——它允许**任意形状**（默认 `_AnyShape`，即任意长度的元组）。而 `defmatrix.pyi` **不复用**这个名字相同的 TypeVar，而是**重新声明**了一个同名但约束更紧的 `_ShapeT_co`，把 `bound` 从 `_Shape` 收窄到 `_2D`。这一收窄，就是把「matrix 永远二维」这个运行时不变量（见 u3-l1/u3-l2/u3-l4 讲过的索引保形、归约保形、ravel 保形）**提升为静态类型层的承诺**。

#### 4.2.2 核心流程

「永远二维」在运行时和类型层各自如何被守住：

```text
运行时（defmatrix.py）              类型层（defmatrix.pyi）
─────────────────────              ──────────────────────
__array_finalize__ 把掉维结果      _ShapeT_co bound=_2D
补回 (1,N)/(N,1)/(M,N)             ⇒ matrix 的形状参数只能是
                                   _2D = tuple[int, int]
```

也就是说，matrix 的形状合法取值集合是

\[
\text{shape}(m) \in \{(1,1),\ (1,N),\ (N,1),\ (M,N)\}
\]

而 `_2D = tuple[int, int]` 正是这整个集合在类型世界里的**最小上界**。

#### 4.2.3 源码精读

两个 TypeVar 与 `_2D` 的声明：

[numpy/matrixlib/defmatrix.pyi:L20-L23](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L20-L23) —— `_ShapeT_co` 被绑死到 `_2D`（`bound=_2D, default=_2D, covariant=True`）；`_DTypeT_co` 绑到 `np.dtype`；`_2D = tuple[int, int]` 用 PEP 695 `type` 语句定义。注意 `bound=_2D` 与 `default=_2D` 同时出现：`default` 决定「未写参数时的回退值」，`bound` 决定「参数允许取值的上界」。

对照基类 `ndarray` 自己的形状 TypeVar：

[numpy/__init__.pyi:L781-L782](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.pyi#L781-L782) —— ndarray 的 `_ShapeT_co` 是 `bound=_Shape, default=_AnyShape`，允许任意形状。matrix 把它收窄到 `_2D`，这正是子类「收窄类型参数上界」的标准手法。

泛型签名本体：

[numpy/matrixlib/defmatrix.pyi:L30-L31](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L30-L31) —— `class matrix(np.ndarray[_ShapeT_co, _DTypeT_co])` 继承泛型基类；`__array_priority__: ClassVar[float] = 10.0` 把 u3-l5 讲过的优先级常量也写进存根（`ClassVar` 标明它是类级而非实例级）。行尾的 `# pyright: ignore[reportIncompatibleMethodOverride]` 是因为基类把 `__array_priority__` 声明为只读 `property`（见 [numpy/__init__.pyi:L1778](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.pyi#L1778)），而 matrix 把它改写成类属性，签名「不兼容」，故显式噤声。

#### 4.2.4 代码实践

1. **实践目标**：用 pyright 看 `matrix` 与 `ndarray` 的形状参数差异。
2. **操作步骤**：写一段脚本 `t.py`：
   ```python
   import numpy as np
   m: np.matrix = np.matrix([[1, 2], [3, 4]])
   a: np.ndarray = np.array([[1, 2], [3, 4]])
   reveal_type(m)   # 期望看到 matrix[_2D, ...]
   reveal_type(a)   # 期望看到 ndarray[_2D, ...] 或 ndarray[tuple[int,int], ...]
   ```
   然后运行 `pyright t.py`（或 `mypy --reveal-type`，需 `reveal_type` 写法适配）。
3. **需要观察的现象**：`m` 的形状参数被推断为 `_2D`（或 `tuple[int, int]`），与 `a` 同形状时形式一致；但一旦尝试把 matrix 当作三维使用，类型层会拒绝。
4. **预期结果**：`reveal_type(m)` 形如 `matrix[tuple[int, int], dtype[...]]`，印证 `_ShapeT_co` 被绑死到 `_2D`。
5. 具体打印文本「待本地验证」（依赖 pyright 版本）。

#### 4.2.5 小练习与答案

- **练习 1**：`_ShapeT_co` 的 `bound=_2D` 和 `default=_2D` 各自起什么作用？如果只写 `default=_2D` 不写 `bound`，会有什么不同？
  - **答案**：`default` 是「用户不写形状参数时的回退值」；`bound` 是「形状参数允许取值的上界」。只写 `default` 不写 `bound` 时，理论上用户可以写出 `matrix[tuple[int,int,int], ...]` 这种三维形状参数而不被拒绝；同时有 `bound=_2D` 才能把三维形状参数判为非法，真正守住「二维」承诺。
- **练习 2**：为什么存根里 `__array_priority__` 后面要跟 `# pyright: ignore[reportIncompatibleMethodOverride]`？
  - **答案**：基类 `ndarray`（更确切说是 `_ArrayOrScalarCommon`）把 `__array_priority__` 声明为只读 `property`，而 matrix 在运行时把它定义为普通类属性，签名不兼容；为了让存根能通过 pyright 检查，作者显式噤声了这条告警。

---

### 4.3 `_Matrix` 类型别名：把「二维 matrix」打包成一个名字

#### 4.3.1 概念说明

`_Matrix` 是一个**泛型类型别名**：`type _Matrix[ScalarT: np.generic] = matrix[_2D, np.dtype[ScalarT]]`。它干的事是——给定一个标量类型 `ScalarT`（如 `np.int64`），产出 `matrix[_2D, np.dtype[ScalarT]]`，即「形状固定二维、dtype 固定为该标量」的 matrix。

它的存在让存根作者少写一长串：凡是要表达「一个二维 matrix，dtype 可能是任意标量」的地方，写 `_Matrix[Incomplete]`（dtype 未知）或 `_Matrix[np.intp]`（dtype 就是 intp）即可，而不必每次重复 `matrix[_2D, np.dtype[...]]`。

#### 4.3.2 核心流程

`_Matrix` 的展开规则：

```text
_Matrix[ScalarT]   ==展开==>   matrix[_2D, np.dtype[ScalarT]]

_Matrix[Incomplete]==展开==>   matrix[_2D, np.dtype[Incomplete]]   # 形状确定、dtype 未知
_Matrix[np.intp]   ==展开==>   matrix[_2D, np.dtype[np.intp]]      # 形状与 dtype 都确定
```

这与直接写 `matrix[_2D, _DTypeT_co]` 的差别在于：`_Matrix[...]` 的形状是**别名写死的 `_2D`**、dtype 是**别名参数指定的**；而 `matrix[_2D, _DTypeT_co]` 的 `_DTypeT_co` 是**跟随当前 matrix 实例自身**的类型参数（即「我是什么 dtype，返回就是什么 dtype」）。

#### 4.3.3 源码精读

别名的定义：

[numpy/matrixlib/defmatrix.pyi:L24](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L24) —— `type _Matrix[ScalarT: np.generic] = matrix[_2D, np.dtype[ScalarT]]`。`ScalarT` 被绑定到 `np.generic`（所有 numpy 标量的基类），保证只能填标量类型。

别名的典型用法：构造与运算的结果「形状确定但 dtype 随输入」：

[numpy/matrixlib/defmatrix.pyi:L33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L33) —— `__new__` 返回 `_Matrix[Incomplete]`：构造函数接受任意 `ArrayLike`，结果一定是二维 matrix，但具体 dtype 取决于运行时输入，静态无法确定，故用 `Incomplete`。

[numpy/matrixlib/defmatrix.pyi:L48-L49](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L48-L49) —— `__mul__`/`__rmul__` 也返回 `_Matrix[Incomplete]`：矩阵乘法结果 dtype 取决于两个操作数（见 u2-l5），同样静态不可知。

对比「跟随实例 dtype」的写法，留到 4.5 讲 `squeeze`/`ravel`/`flatten` 时展开。

#### 4.3.4 代码实践

1. **实践目标**：亲手展开 `_Matrix[Incomplete]`，理解它等价于什么。
2. **操作步骤**：在纸上（或注释里）按定义逐步替换：
   ```text
   _Matrix[Incomplete]
   = matrix[_2D, np.dtype[Incomplete]]        # 代入别名第 24 行
   = matrix[tuple[int, int], np.dtype[Incomplete]]   # 代入 _2D 第 23 行
   ```
3. **需要观察的现象**：展开后形状部分是确定的 `tuple[int, int]`，dtype 部分是 `Incomplete`。
4. **预期结果**：你能口头复述「`__new__` 承诺返回一个二维 matrix，但不承诺它的元素 dtype」。
5. 本练习是源码阅读型，无需运行。

#### 4.3.5 小练习与答案

- **练习 1**：`_Matrix[Incomplete]` 与 `matrix[_2D, _DTypeT_co]` 有何区别？
  - **答案**：前者把 dtype 钉死为 `Incomplete`（未知），与调用者无关；后者的 `_DTypeT_co` 是 matrix 实例自身的类型参数，会跟随实例——若实例是 `matrix[_2D, np.dtype[int64]]`，则返回也是 `matrix[_2D, np.dtype[int64]]`。
- **练习 2**：为什么 `__new__` 用 `_Matrix[Incomplete]` 而不是 `matrix[_2D, _DTypeT_co]`？
  - **答案**：`__new__` 是构造入口，对象此刻还不存在、没有「实例自身的 `_DTypeT_co`」可跟随；结果 dtype 完全由运行时输入决定，静态层面只能用 `Incomplete` 占位。

---

### 4.4 归约方法的多重 `overload`：用 4 个签名表达 axis/out 组合

#### 4.4.1 概念说明

u3-l2 讲过：`matrix.sum/mean/std/var/prod` 这些归约方法在运行时会同时遵守「保持二维 + 朝向正确」两条不变量——`axis=None` 得标量、`axis=0` 得行向量 `(1,N)`、`axis=1` 得列向量 `(N,1)`，并支持 `out=` 把结果写进指定数组。

静态类型层面，这一套「返回类型随 `axis`/`out` 取值而变」的行为无法用单个签名表达，于是存根为每个归约方法写了 **4 个 `@overload`**，让类型检查器按调用实参挑出正确的返回类型。这套 4 段式模板在 `sum`/`prod`/`mean`/`std`/`var` 上几乎逐字重复，源码里 `# keep in sync with ...` 注释就是提醒作者改一个要同步改其它。

#### 4.4.2 核心流程

以 `sum` 为例，4 个 overload 分别覆盖「不缩轴得标量 / 缩轴得二维 matrix / 位置参 `out` / 关键字参 `out`」四种调用形态：

```text
sum()                          → Incomplete           # axis=None, out=None：标量
sum(axis=1)                    → _Matrix[Incomplete]  # 给了 axis：二维 matrix
sum(axis=1, dtype=..., out=o)  → OutT                 # 位置传 out：返回 out 的类型
sum(*, out=o)                  → OutT                 # 关键字传 out：返回 out 的类型
```

类型检查器**自上而下**匹配：先看能不能匹配第 1 个，不行再看第 2 个……第一个匹配上的，其返回类型即为推断结果。

`max`/`min`/`ptp` 与 `argmax`/`argmin` 走的是另一种模板（第 1 个 overload 用 `self: NDArray[ScalarT]` 把实例窄化到标量类型，从而让 `axis=None` 返回标量类型 `ScalarT` 或 `np.intp`），但「4 个 overload 表达组合」的整体思路一致。

#### 4.4.3 源码精读

`sum` 的 4 个 overload：

[numpy/matrixlib/defmatrix.pyi:L56-L63](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L56-L63) ——
- 第 1 个：`axis: None = None, out: None = None` → `Incomplete`（整体求和得标量，dtype 未知）。
- 第 2 个：`axis: _ShapeLike, out: None = None` → `_Matrix[Incomplete]`（沿轴归约得二维 matrix）。
- 第 3 个：带泛型 `[OutT: np.ndarray]`，`out: OutT` 为位置参 → `OutT`（返回与 out 同型）。
- 第 4 个：`*, out: OutT` 为关键字参 → `OutT`。

行首的 `# keep in sync with prod and mean`（[L55](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L55)）提醒：`prod`/`mean` 的 4 段与之完全同构（见 [L65-L83](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L65-L83)），`std`/`var` 只是多了 `ddof: float = 0`（见 [L85-L107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L85-L107)）。

「位置 out」与「关键字 out」拆成两个 overload，是因为 Python 的重载无法用单个签名同时精确表达「`out` 既可位置又可关键字、且只在给定时才透传其类型」这一组合，故拆开覆盖。

运行时对照——存根第 2 个 overload（给了 axis 返回二维 matrix）对应的实现：

[numpy/matrixlib/defmatrix.py:L293-L325](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L293-L325) —— `sum` 实现 `return N.ndarray.sum(self, axis, dtype, out, keepdims=True)._collapse(axis)`：`keepdims=True` 保二维、`_collapse(axis)` 在 `axis=None` 时压成标量。这与存根「axis=None → 标量（Incomplete）、给了 axis → 二维 matrix」的分流**一一对应**。

`max`/`min`/`ptp` 的另一种模板——用 `self: NDArray[ScalarT]` 窄化：

[numpy/matrixlib/defmatrix.pyi:L130-L137](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L130-L137) —— 第 1 个 overload `def max[ScalarT: np.generic](self: NDArray[ScalarT], axis: None = None, out: None = None) -> ScalarT`：把 `self` 窄化到 `NDArray[ScalarT]`（即 `ndarray[_AnyShape, np.dtype[ScalarT]]`），从而把实例的 dtype 抽出来作为 `ScalarT`，让「不缩轴」时返回**精确的标量类型**而非 `Incomplete`。第 2 个 overload 给 `axis: _ShapeLike` 时返回 `matrix[_2D, _DTypeT_co]`（跟随实例 dtype，而非 `_Matrix[Incomplete]`）。

`argmax`/`argmin` 同构，只是标量固定为 `np.intp`、二维分支用 `_Matrix[np.intp]`（见 [L159-L177](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L159-L177)），呼应 u3-l2 讲的「`_align` 收尾」。

#### 4.4.4 代码实践

1. **实践目标**：观察 `m.sum(...)` 在不同参数下被推断成不同类型，验证 4 段 overload 的分流。
2. **操作步骤**：写 `t2.py`：
   ```python
   import numpy as np
   m = np.matrix([[1, 2], [3, 4]])
   reveal_type(m.sum())            # axis=None ⇒ 标量（Incomplete）
   reveal_type(m.sum(axis=1))      # 给 axis ⇒ _Matrix[Incomplete]（二维 matrix）
   out = np.zeros((2, 1))
   reveal_type(m.sum(axis=1, out=np.asmatrix(out)))  # 给 out ⇒ OutT
   ```
   运行 `pyright t2.py`。
3. **需要观察的现象**：三处 `reveal_type` 输出应分别为「标量类型」「`matrix[...]`」「传入 out 的类型」。
4. **预期结果**：`m.sum()` 推断为某个标量类型（`Incomplete` 往往显示为 `Unknown`）；`m.sum(axis=1)` 推断为 `matrix[tuple[int, int], dtype[Unknown]]`；带 `out=` 时返回 out 的类型。
5. 具体文本「待本地验证」（依赖 pyright 版本与 numpy 存根版本）。

#### 4.4.5 小练习与答案

- **练习 1**：为什么「位置 `out`」和「关键字 `out`」要拆成两个 overload，而不是合并？
  - **答案**：单个 Python 签名难以同时表达「`out` 可位置又可关键字、且仅在提供时才透传其类型、未提供时走别的返回类型」。拆成「位置 `out: OutT`（无默认值，强制靠前的实参）」与「`*, out: OutT`（纯关键字）」两段，能让检查器对两种调用形态都精确分流。
- **练习 2**：`max` 的第 1 个 overload 为何要写 `self: NDArray[ScalarT]` 而 `sum` 不用？
  - **答案**：`sum(axis=None)` 的结果是「把所有元素加起来」，其 dtype 与输入 dtype 关系复杂（可能向上提升），存根只好用 `Incomplete`，无需抽取标量类型；而 `max(axis=None)` 的结果就是「数组里的某个元素」，dtype 与元素完全相同，于是用 `self: NDArray[ScalarT]` 把实例窄化、抽出 `ScalarT`，返回精确标量类型 `ScalarT`。

---

### 4.5 `squeeze`/`ravel`/`flatten` 与属性签名：把「永远二维」写进类型

#### 4.5.1 概念说明

u3-l3（属性）与 u3-l4（形状方法）讲过：`matrix` 的 `squeeze`/`ravel`/`flatten` 永远返回二维 matrix（哪怕把列向量 squeeze 成 `(1,N)` 行向量），`A` 返回同形状的 `ndarray`、`A1` 返回一维 `ndarray`，`H` 返回二维 matrix。本模块看存根如何把这些运行时保证「翻译」成返回类型标注。

关键对比再次出现：这几个方法的返回类型用的是 `matrix[_2D, _DTypeT_co]`（**跟随实例 dtype**），而不是 `_Matrix[Incomplete]`。原因很直觉——`squeeze`/`ravel`/`flatten` 不改变 dtype，结果 dtype 与输入完全一致，于是可以用实例自身的 `_DTypeT_co` 精确表达，而不必退化为 `Incomplete`。

#### 4.5.2 核心流程

返回类型选择规则一览：

| 方法/属性 | 返回类型 | 为什么这么标 |
|---|---|---|
| `squeeze`/`ravel`/`flatten` | `matrix[_2D, _DTypeT_co]` | 不改 dtype，跟随实例；形状恒二维 |
| `H` | `matrix[_2D, _DTypeT_co]` | 共轭转置不改 dtype，形状仍二维 |
| `A` | `np.ndarray[_2D, _DTypeT_co]` | 脱壳为 ndarray，二维形状与 dtype 都保留 |
| `A1` | `np.ndarray[_AnyShape, _DTypeT_co]` | 脱壳并展平成一维，形状退化到 `_AnyShape` |
| `I` | `_Matrix[Incomplete]` | inv/pinv 结果通常升级为浮点，dtype 不可由输入静态确定 |
| `getT` | `Self` | 转置不改类型，返回自身同型 |

#### 4.5.3 源码精读

三个形状方法共用同一行注释与同一套返回类型：

[numpy/matrixlib/defmatrix.pyi:L185-L188](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L185-L188) —— 注释「these three methods will at least return a `2-d` array of shape (1, n)」直接道出设计意图；`squeeze`/`ravel`/`flatten` 三者签名一致，都返回 `matrix[_2D, _DTypeT_co]`。这里用 `_DTypeT_co`（跟随实例）而非 `Incomplete`，是因为它们不改 dtype。

运行时对照——`flatten` 实现确实只改形状不改 dtype：

[numpy/matrixlib/defmatrix.py:L380-L415](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L380-L415) —— `flatten` 一行委托 `N.ndarray.flatten(self, order=order)`，靠 `__array_finalize__` 把掉到一维的结果补回二维（见 u3-l4），dtype 全程不变。存根 `matrix[_2D, _DTypeT_co]` 正好如实复述。

属性签名：

[numpy/matrixlib/defmatrix.pyi:L191-L203](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L191-L203) ——
- `getT(self) -> Self`：转置不改类型，用 `Self`（PEP 673）表达「返回与自身同型」。
- `I` 属性 → `_Matrix[Incomplete]`：逆运算通常把整数矩阵升级成浮点，dtype 静态不可知，故退化（运行时见 [defmatrix.py:L798-L841](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L798-L841) 按 `M==N` 选 `inv`/`pinv`）。
- `A` → `np.ndarray[_2D, _DTypeT_co]`：脱壳为 ndarray，形状仍二维。
- `A1` → `np.ndarray[_AnyShape, _DTypeT_co]`：进一步 `ravel` 到一维，形状参数退化为 `_AnyShape`（注意：是 `_AnyShape` 而非 `_2D`，因为一维不再是「两个 int」）。
- `H` → `matrix[_2D, _DTypeT_co]`：共轭转置不改变 dtype，返回二维 matrix。

关于 `T` 为何没在存根里出现——`defmatrix.pyi` 第 190 行有一句注释 `# matrix.T is inherited from _ScalarOrArrayCommon`：

[numpy/matrixlib/defmatrix.pyi:L190](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.pyi#L190) —— 解释了 `T` 为何不单独声明：它继承自基类。不过注释里的类名 `_ScalarOrArrayCommon` 与真实基类名 `_ArrayOrScalarCommon`（见 [numpy/__init__.pyi:L1721-L1727](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.pyi#L1721-L1727)，其中 `T -> Self` 在 L1727）有一字之差，是源码注释里一处小笔误；实际提供 `T` 的是 `_ArrayOrScalarCommon`。

#### 4.5.4 代码实践

1. **实践目标**：对照存根解释为何 `squeeze` 的返回类型标注为 `matrix[_2D, _DTypeT_co]`。
2. **操作步骤**：写 `t3.py`：
   ```python
   import numpy as np
   c = np.matrix([[1], [2]])          # (2,1) 列向量
   reveal_type(c.squeeze())           # 期望 matrix[_2D, _DTypeT_co]
   reveal_type(c.A)                   # 期望 ndarray[_2D, _DTypeT_co]
   reveal_type(c.A1)                  # 期望 ndarray[_AnyShape, _DTypeT_co]
   reveal_type(c.H)                   # 期望 matrix[_2D, _DTypeT_co]
   ```
   运行 `pyright t3.py`。
3. **需要观察的现象**：`squeeze()` 与 `H` 都被推断为某种 `matrix[...]`；`A` 被推断为 `ndarray[...]`；`A1` 的形状参数不再是二维元组。
4. **预期结果 / 解释**：`squeeze` 返回 `matrix[_2D, _DTypeT_co]` 而非 `matrix[_AnyShape, ...]`，是因为 matrix 在类型层把形状参数绑死到 `_2D`（见 4.2），且 squeeze 不改 dtype（故用 `_DTypeT_co` 跟随实例，而非 `Incomplete`）。这与运行时「squeeze 把 `(N,1)` 变成 `(1,N)` 行向量、绝不降到一维」的行为完全吻合。
5. 具体文本「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：为什么 `A1` 的返回类型是 `np.ndarray[_AnyShape, _DTypeT_co]` 而不是 `np.ndarray[_2D, _DTypeT_co]`？
  - **答案**：`A1` 等价于 `np.asarray(self).ravel()`，结果是一维数组，不再是「两个 int」的二维形状，所以形状参数只能退化为 `_AnyShape`（任意元组），而 dtype 仍跟随实例。
- **练习 2**：`squeeze`/`ravel`/`flatten` 用 `matrix[_2D, _DTypeT_co]`，`I` 用 `_Matrix[Incomplete]`，同样是 matrix 的方法，为什么 dtype 部分一个跟随实例、一个退化？
  - **答案**：前三者不改 dtype（结果元素类型与输入一致），故能精确跟随实例的 `_DTypeT_co`；`I` 做矩阵求逆/伪逆，通常会把整数 dtype 升级为浮点，结果 dtype 无法仅凭输入静态确定，故退化为 `Incomplete`。

## 5. 综合实践

把 4.2–4.5 串起来，完成规格里指定的主任务：**用 pyright 或 mypy 对一段使用 `np.matrix` 的脚本做类型检查，观察 `m.sum(axis=1)` 推断出的类型，并对照存根解释 `squeeze` 的返回类型标注。**

1. **实践目标**：端到端验证「运行时保形行为 ↔ 存根返回类型」的对应关系。
2. **操作步骤**：
   - 准备脚本 `final.py`：
     ```python
     import numpy as np

     m = np.matrix([[1, 2], [3, 4]])

     # (a) 构造：__new__ → _Matrix[Incomplete]
     reveal_type(m)

     # (b) 归约分流：sum 的 4 个 overload
     reveal_type(m.sum())            # axis=None ⇒ 标量
     reveal_type(m.sum(axis=1))      # 给 axis ⇒ _Matrix[Incomplete]
     out = np.zeros((2, 1))
     reveal_type(m.sum(axis=1, out=np.asmatrix(out)))  # 给 out ⇒ OutT

     # (c) 形状方法：跟随实例 dtype 的二维 matrix
     reveal_type(m.squeeze())
     reveal_type(m.ravel())
     reveal_type(m.flatten())

     # (d) 属性：脱壳 ndarray / 共轭转置 matrix / 逆退化
     reveal_type(m.A)
     reveal_type(m.A1)
     reveal_type(m.H)
     reveal_type(m.I)
     ```
   - 运行 `pyright final.py`（或 `mypy --reveal-type final.py`，注意 mypy 用 `reveal_type` 是运行时函数、需实际执行；pyright 是静态识别）。
   - 把每条 `reveal_type` 的输出，回填到本讲 4.2–4.5 的对应表格/预期里。
3. **需要观察的现象**：
   - `m` 的形状参数为 `_2D`（`tuple[int, int]`），印证 4.2 的「绑死二维」。
   - `m.sum()` 为标量、`m.sum(axis=1)` 为二维 matrix、带 `out=` 时为 out 类型，印证 4.4 的 4 段分流。
   - `squeeze/ravel/flatten` 都返回二维 matrix 且 dtype 跟随 `m`，印证 4.5。
   - `A` 为 ndarray（二维）、`A1` 为 ndarray（一维，形状参数非 `_2D`）、`H` 为二维 matrix、`I` 为 dtype 未知的 matrix。
4. **预期结果**：上述四组观察全部成立。
5. 若本地未安装 pyright/mypy，或 numpy 存根版本不同导致文本不一致，相关输出「待本地验证」；但「形状参数 `_2D` / 归约按 axis·out 分流 / squeeze 跟随实例 dtype」这三条结构性结论可由源码直接断定。

## 6. 本讲小结

- `defmatrix.pyi` 是 `matrix`/`asmatrix`/`bmat` 的类型存根，只含签名、不含逻辑，供静态检查器消费；运行时行为仍来自 `defmatrix.py`。
- `class matrix(np.ndarray[_ShapeT_co, _DTypeT_co])` 把形状 TypeVar 绑死到 `_2D = tuple[int, int]`，在类型层复述了「永远二维」这一运行时不变量。
- `type _Matrix[ScalarT: np.generic] = matrix[_2D, np.dtype[ScalarT]]` 是「二维 matrix」的简写别名；`_Matrix[Incomplete]` 表示「形状确定、dtype 未知」，与 `matrix[_2D, _DTypeT_co]`（跟随实例 dtype）语义不同。
- 归约方法 `sum/prod/mean/std/var` 各用 4 个 `@overload` 表达 `axis`/`out` 组合的返回分流；`max/min/ptp/argmax/argmin` 走「`self: NDArray[ScalarT]` 窄化」的另一种同构模板。
- `Incomplete` 是 typeshed 哨兵，用于「dtype 取决于运行时输入、静态不可知」的位置（如 `__new__`、`__mul__`、`I`）；不改 dtype 的方法（`squeeze/ravel/flatten/H`）则用 `_DTypeT_co` 精确跟随实例。
- 存根里的「`# keep in sync with ...`」「`# pyright: ignore[...]`」等注释/噤声，反映了维护多份 overload 与兼容基类签名的工程现实；`T` 注释里的 `_ScalarOrArrayCommon` 是一处类名笔误（实为 `_ArrayOrScalarCommon`）。

## 7. 下一步学习建议

- 读完本讲后，建议结合 [u3-l7 测试体系](#)，去看 `tests/test_defmatrix.py` 里对 `sum(axis=1)`、`squeeze()` 等行为的断言，把「存根承诺 → 运行时实现 → 测试用例」三点连成一线。
- 想更系统地理解 numpy 的泛型设计，可继续阅读 `numpy/__init__.pyi` 中 `ndarray` 的 `_ShapeT_co`/`_DTypeT_co` 用法，以及 `numpy/_typing/_array_like.py` 里 `NDArray`、`_ArrayLikeInt_co` 等别名的定义，本讲的 `_Matrix` 正是模仿它们的写法。
- 如果你对静态类型本身感兴趣，可延伸阅读 PEP 695（`type` 语句）、PEP 696（TypeVar `default`）、PEP 673（`Self`）、PEP 484（`@overload`），这些是读懂 `defmatrix.pyi` 背后的全部语法基础。
