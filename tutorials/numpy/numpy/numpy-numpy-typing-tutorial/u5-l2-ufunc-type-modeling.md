# ufunc 的类型建模：_ufunc.pyi 详解

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚为什么 `np.add`、`np.divmod`、`np.matmul` 不能共用同一个类型签名，NumPy 又是如何把 `ufunc` 拆成若干「只在类型检查时存在」的私有子类来建模的。
- 读懂 `_UFunc_Nin2_Nout1.__call__` 的 5 个 `@overload`，并能判断「输入是标量还是数组 / 有没有给 `out`」会落到哪一条重载、返回什么类型。
- 理解 `Never`（参数）与 `NoReturn`（返回）这对组合如何表达「这个方法在该 ufunc 上不可用」。
- 理解 `Unpack[TypedDict]`（PEP 692）如何把 ufunc 的一大堆关键字参数（`where`/`casting`/`order`/`subok`/`signature`）精确化成有名、有类型的键。

本讲是单元 5 的第 2 讲，承接 u5-l1 讲过的「`.py` 运行时轨 / `.pyi` 类型检查轨双轨制」与 `@type_check_only` 幻影类，把它落到一个最复杂、也最典型的样本——ufunc——上。

## 2. 前置知识

本讲默认你已经掌握前几讲建立的术语，这里只做最小回顾：

- **双轨制与 `@type_check_only`（u5-l1）**：同一模块的 `.py` 与 `.pyi` 并存时，类型检查器只读 `.pyi`、忽略 `.py`；`@type_check_only` 装饰的类是「只在类型检查时存在」的虚构子类，运行时根本不存在。
- **`ArrayLike` 与 `_DualArrayLike`（u2-l1）**：`ArrayLike` 描述「一切能转成数组的对象」，由缓冲区、带 `__array__` 的对象、内置标量、嵌套序列拼成；它刻意避开 object 数组。
- **`Protocol` 与 `_SupportsArray` / `_SupportsArrayFunc`（u3-l1）**：结构子类型（按方法形状匹配而非继承），`@runtime_checkable` 打开 `isinstance` 开关。
- **`NDArray` / `ndarray` 泛型（u2-l3）**：`NDArray[ScalarT] = np.ndarray[_AnyShape, np.dtype[ScalarT]]`，元素类型是它的类型参数。

另外需要两个本讲会用到、但前面没细讲的 Python 类型系统概念：

- **`@overload`（PEP 484）**：用一连串「存根签名」（只有 `...` 没有函数体）描述同一个函数在不同输入下的不同返回类型，最后跟一条「实现签名」兜底。类型检查器按**从上到下、第一条匹配即生效**的顺序选择重载。
- **`TypedDict` 与 `Unpack`（PEP 589 / 692）**：`TypedDict` 把一个字典的字面键各自赋予类型；`Unpack[SomeTypedDict]` 用在 `**kwargs` 上，表示「这些关键字参数有固定的名字和类型」，而不是 `**kwargs: SomeType`（后者表示「所有值的类型都是 `SomeType`」）。

什么是 ufunc？ufunc（universal function）是 NumPy 对「逐元素运算」的统一抽象：`np.add`、`np.multiply`、`np.sin`、`np.divmod`、`np.matmul` 都是 ufunc 实例。它们在运行时都是 `numpy.ufunc` 的实例（`type(np.add) is numpy.ufunc`），但彼此的「形状」差别极大——这正是本讲要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_typing/_ufunc.pyi` | 本讲主角：828 行的纯类型桩，用 `@type_check_only` 子类 + `@overload` 给所有形状的 ufunc 建模。 |
| `numpy/_typing/_ufunc.py` | 运行时轨：仅 3 行占位别名，证明桩里的类型类运行时并不存在（双轨制）。 |
| `numpy/_typing/_array_like.py` | 提供 `ArrayLike`、`NDArray`、`_ArrayLikeBool_co`、`_ArrayLikeInt_co` 等别名，被 ufunc 桩当作输入/输出类型。 |
| `numpy/_typing/_scalars.py` | 提供 `_ScalarLike_co`，是 ufunc「标量分支」的输入类型。 |
| `numpy/typing/tests/data/reveal/ufuncs.pyi` | reveal 夹具：用 `assert_type` 把 ufunc 的实际推断结果钉死，是本讲最重要的「行为证据」。 |

> 说明：本讲的永久链接对 `_typing` 下的文件使用规范路径 `numpy/_typing/...`；这些文件位于私有包，但它们才是 ufunc 类型建模的真实实现地。

## 4. 核心概念与源码讲解

### 4.1 按 nin/nout 拆分 ufunc：四个 @type_check_only 子类

#### 4.1.1 概念说明

一个 ufunc 的「形状」由两个整数刻画：

- `nin`：输入参数个数（如 `np.add` 是 2，`np.sin` 是 1）。
- `nout`：输出个数（如 `np.add` 是 1，`np.divmod` 是 2，因为它同时返回商和余数）。

如果只用一个 `ufunc` 类来标注所有 ufunc，类型系统就没法区分「调用 `np.add(a, b)` 返回一个数组」和「调用 `np.divmod(a, b)` 返回两个数组组成的元组」。模块文档字符串直白地点明了这一点：

[_ufunc.pyi:1-7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L1-L7) ——「ufunc 的签名变化太大，无法用单个类合理地标注；于是把 `ufunc` 展开成若干私有子类，每种 nin/nout 组合一个」。

NumPy 的解法是：**在类型检查的世界里，虚构出几个 `ufunc` 的子类**，每个子类对应一种 `nin`/`nout`（甚至是否带「核心维度签名」）。这些子类全部用 `@type_check_only` 标注——意思是「别在运行时找我，我只为类型检查器而存在」。

这一招和 u5-l1 讲的双轨制是一脉相承的：运行时 `np.add` 就是普通的 `numpy.ufunc` 实例，类型检查时却被「化妆」成更精确的子类。

#### 4.1.2 核心流程

桩文件里定义的核心子类（按形状分类）：

```
_UFunc_Nin2_Nout1   nin=2, nout=1   工作主力：add/multiply/subtract/less/...
_UFunc_Nin2_Nout2   nin=2, nout=2   多输出：divmod/modf
_GUFunc_Nin2_Nout1  nin=2, nout=1   广义 ufunc（gufunc，带核心维度签名）：matmul/vecdot
_PyFunc_Nin1_Nout1  nin=1, nout=1   frompyfunc 造的 Python 回调 ufunc（1 进 1 出）
_PyFunc_Nin2_Nout1  nin=2, nout=1   frompyfunc（2 进 1 出）
_PyFunc_Nin3P_Nout1 nin≥3, nout=1   frompyfunc（可变输入）
_PyFunc_Nin1P_Nout2P nin≥1, nout≥2  frompyfunc（可变输入、多输出）
```

本讲聚焦前三个 `_UFunc`/`_GUFunc`（本讲规格要求），`_PyFunc_*` 家族结构相似，留作拓展。

每个子类都带一组**类型参数**，把「这个具体 ufunc 的不变量」编码进类型：

- `NameT: LiteralString`：ufunc 的名字，如 `"add"`。
- `NTypesT: int`：该 ufunc 注册的循环（loop）数量，如 `np.add` 是 22。
- `IdentT`：`identity` 属性的值（`np.add.identity == 0`、`np.multiply.identity == 1`、`np.logical_or.identity == False`）。
- `SignatureT: LiteralString`（仅 `_GUFunc`）：核心维度签名串，如 `"(n?,k),(k,m?)->(n?,m?)"`。

这些类型参数随后通过 `@property` 暴露，于是类型检查器能把 `np.add.__name__` 推断成 `Literal["add"]`，把 `np.add.ntypes` 推断成 `Literal[22]`。

#### 4.1.3 源码精读

子类声明的头部（PEP 695 泛型类语法，类型参数带约束）：

[_ufunc.pyi:77-78](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L77-L78) —— `_UFunc_Nin2_Nout1` 继承 `ufunc`，带 `NameT: LiteralString`、`NTypesT: int`、`IdentT` 三个类型参数；`# type: ignore[misc]` 是给 mypy 的让步（继承 C 扩展类型时的常规处理）。

类型参数如何流动到属性：

[_ufunc.pyi:79-94](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L79-L94) —— `__name__ -> NameT`、`ntypes -> NTypesT`、`identity -> IdentT`；而 `nin`/`nout`/`nargs` 这三个对整类都一样的量，直接钉成 `Literal[2]`/`Literal[1]`/`Literal[3]`。`signature` 返回 `None`（普通 ufunc 没有核心维度签名）。

`_UFunc_Nin2_Nout2` 与 `_GUFunc_Nin2_Nout1` 的差异：

[_ufunc.pyi:276-293](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L276-L293) —— `_UFunc_Nin2_Nout2`：`nout -> Literal[2]`、`nargs -> Literal[4]`，`signature` 仍是 `None`。

[_ufunc.pyi:336-353](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L336-L353) —— `_GUFunc_Nin2_Nout1`：多一个类型参数 `SignatureT: LiteralString`，且 `signature -> SignatureT`（把 gufunc 的核心维度签名串原样带回类型世界）。

「这些类运行时不存在」的证据——运行时轨 `_ufunc.py` 只有 3 行：

[_ufunc.py:1-5](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.py#L1-L5) —— 运行时 `_UFunc_Nin2_Nout1 = ufunc`（直接等于基类），没有继承、没有属性、没有重载。类型检查器读 `.pyi` 看到的是几百行精细模型，CPython 执行 `.py` 看到的是「三个名字都等于 `ufunc`」。这正是 u5-l1 双轨制的活样本。

reveal 夹具用 `assert_type` 钉死了类型参数的实际取值（由 `numpy._core` 的桩文件把 `np.add` 实例化为 `_UFunc_Nin2_Nout1[Literal["add"], Literal[22], Literal[0]]`，可由下列断言反推）：

[ufuncs.pyi:11-18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L11-L18) —— `np.add.__name__` 是 `Literal["add"]`、`ntypes` 是 `Literal[22]`、`identity` 是 `Literal[0]`、`nin`/`nout`/`nargs` 分别是 `Literal[2]`/`Literal[1]`/`Literal[3]`。

[ufuncs.pyi:46](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L46) —— `np.matmul.signature` 是 `Literal["(n?,k),(k,m?)->(n?,m?)"]`，证明 gufunc 的 `SignatureT` 确实承载了核心维度签名。

#### 4.1.4 代码实践

**实践目标**：用运行时确认「这些 `@type_check_only` 子类在运行时确实不存在」，从而亲手验证双轨制。

**操作步骤**（源码阅读 + 运行时确认）：

1. 打开 `_ufunc.py`，确认它只有 3 行占位，没有任何 `class`。
2. 写一个最小脚本（示例代码，非项目原有）：

   ```python
   # 示例代码
   import numpy as np
   from numpy._typing import _ufunc as _u
   print(type(np.add))            # 运行时类型
   print(_u._UFunc_Nin2_Nout1)    # 运行时它到底是什么
   print(_u._UFunc_Nin2_Nout1 is np.ufunc)
   ```

3. 同时打开 `_ufunc.pyi`，确认 `class _UFunc_Nin2_Nout1(...)` 是一个带几十个 `@overload` 的真实类定义。

**需要观察的现象**：

- `type(np.add)` 报告的是 `<class 'numpy.ufunc'>`，不是某个子类。
- `_u._UFunc_Nin2_Nout1 is np.ufunc` 为 `True`——运行时它就是基类本身。

**预期结果**：运行时世界里没有 `_UFunc_Nin2_Nout1` 这个「子类」，它纯粹是类型检查器在 `.pyi` 里看到的虚构。如果你用 mypy/pyright 看，`reveal_type(np.add.__name__)` 则会给出 `Literal["add"]`。

> 若本地未安装可导入的 numpy 或类型检查器，运行时部分与 `reveal_type` 部分**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`_UFunc_Nin2_Nout2` 的 `nargs` 属性返回 `Literal[4]`，而 `_UFunc_Nin2_Nout1` 返回 `Literal[3]`。为什么一个是 4、一个是 3？

**答案**：`nargs = nin + nout`。`_UFunc_Nin2_Nout1` 是 \(2+1=3\)，`_UFunc_Nin2_Nout2` 是 \(2+2=4\)。桩文件把这些「类级不变量」直接钉成字面量，类型检查器据此就能判断 `np.add.nargs` 的取值。

**练习 2**：为什么 `_GUFunc_Nin2_Nout1` 要比 `_UFunc_Nin2_Nout1` 多一个类型参数 `SignatureT`？

**答案**：gufunc（如 `np.matmul`）有「核心维度签名」（core dimension signature），描述它在哪些维度上做线性代数运算（如 `"(n?,k),(k,m?)->(n?,m?)"`）。普通 ufunc 的 `signature` 是 `None`，而 gufunc 的 `signature` 是一个有意义的字符串，需要作为类型信息保留下来，所以多一个 `SignatureT: LiteralString` 参数，并通过 `signature -> SignatureT` 暴露。

---

### 4.2 `__call__` 的 @overload 矩阵：标量 vs 数组 vs out

#### 4.2.1 概念说明

同一个 ufunc 调用，根据「输入是标量还是数组」「有没有传 `out`」，返回类型完全不同：

- `np.add(1.0, 2.0)` → 标量。
- `np.add(np.array([1.0]), 2.0)` → 数组。
- `np.add(np.array([1.0]), 2.0, out=buf)` → 写入 `buf` 并返回它。

`@overload` 的价值正在于此：**用多条存根签名，把「输入形状 → 输出类型」的映射逐条写清楚**，让类型检查器能精确推断。

#### 4.2.2 核心流程

`_UFunc_Nin2_Nout1.__call__` 一共 5 条重载，匹配顺序自上而下：

```
①  (x1: 标量, x2: 标量)                      → 标量(Incomplete)
②  (x1: ArrayLike, x2: ndarray)              → NDArray
③  (x1: ndarray, x2: ArrayLike)              → NDArray
④  (x1: ArrayLike, x2: ArrayLike, out=数组)   → NDArray
⑤  (x1: ArrayLike, x2: ArrayLike)            → NDArray | 标量(兜底)
```

判断逻辑可写成一条决策规则。设 \(s(\cdot)\) 表示「该实参能匹配标量分支 `_ScalarLike_co`」，\(a(\cdot)\) 表示「能匹配 `ArrayLike`」：

\[
\text{返回类型} =
\begin{cases}
\text{标量(Incomplete)} & \text{若 } s(x_1)\land s(x_2) \text{（命中①）}\\
\text{NDArray} & \text{若 } a(x_1)\lor a(x_2) \text{ 且显式给了 } out \text{（命中②③④）}\\
\text{NDArray}\cup\text{标量} & \text{否则（命中⑤，兜底）}
\end{cases}
\]

注意「标量」在前、「数组」兜底：因为标量是更窄的情形，必须放前面，否则会被宽泛的 `ArrayLike` 抢先匹配（`_ScalarLike_co` 的元素也是 `ArrayLike` 的子集）。

两个关键细节：

- `_ScalarLike_co = complex | str | bytes | np.generic`（[_scalars.py:20]）。Python 类型系统里 `complex` 隐含覆盖 `float`/`int`/`bool`，所以传 Python 的 `int`/`float`/`bool` 都会命中标量分支。
- `Incomplete` 是 typeshed 提供的「未知类型」哨兵（[_ufunc.pyi:9] 从 `_typeshed` 导入），在用户侧表现为 `Any`——numpy 用它表示「这里我不告诉你精确标量，但别报错」。

#### 4.2.3 源码精读

5 条重载按序排列，每条都用注释点明它的意图：

[_ufunc.pyi:96-150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L96-L150) —— 依次是 `(scalar,scalar)->scalar`、`(array-like,array)->array`、`(array,array-like)->array`、`(array-like,array-like,out=array)->array`、`(array-like,array-like)->array|scalar`。

第①条标量分支（注意 `out` 默认是 `EllipsisType | None`，即允许 `out=...` 这种哨兵写法）：

[_ufunc.pyi:96-106](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L96-L106) —— 两个入参都是 `_ScalarLike_co`，`out: EllipsisType | None = None`，返回 `Incomplete`。

兜底的第⑤条（`out` 可选、可给数组，返回 `NDArray | Incomplete`）：

[_ufunc.pyi:140-150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L140-L150) —— 两个入参都是 `ArrayLike`，返回 `NDArray[Incomplete] | Incomplete`，表达「可能是数组也可能是标量」。

多输出 ufunc 的对照——`_UFunc_Nin2_Nout2.__call__` 返回二元组：

[_ufunc.pyi:295-328](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L295-L328) —— 标量分支返回 `_2Tuple[Incomplete]`，数组分支返回 `_2Tuple[NDArray[Incomplete]]`；其中 `_2Tuple[T] = tuple[T, T]`（[_ufunc.pyi:34]）。

`_ArrayLikeBool_co`、`_ArrayLikeInt_co` 等输入别名的来源（被 kwargs 与 `at`/`reduceat` 复用）：

[_array_like.py:59-61](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L59-L61) —— `_ArrayLikeBool_co`、`_ArrayLikeInt_co` 是 `_DualArrayLike` 的收窄别名（u2-l1 讲过的 `_co` = coercible 引擎）。

reveal 夹具把决策规则的产出钉死：

[ufuncs.pyi:19-20](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L19-L20) —— `np.add(f8, f8)`（两个标量）推断为 `Any`（即 `Incomplete` 的用户态表现）；`np.add(AR_f8, f8)`（数组 + 标量）推断为 `npt.NDArray[Any]`。

[ufuncs.pyi:36-37](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L36-L37) —— `np.divmod(f8, f8)` 推断为 `tuple[Any, Any]`，`np.divmod(AR_f8, f8)` 推断为 `tuple[npt.NDArray[Any], npt.NDArray[Any]]`——正是 `_2Tuple` 展开后的结果。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：精读 `_UFunc_Nin2_Nout1.__call__` 的 5 条重载，画出「输入是标量还是数组 / 是否提供 `out`」对应的返回类型决策树。这是本讲规格指定的核心实践。

**操作步骤**：

1. 打开 [_ufunc.pyi:96-150](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L96-L150)，逐条抄下每条重载的「入参类型 → 返回类型」与行内注释。
2. 判断下列 6 个调用各命中哪一条重载（假设 `a: np.ndarray`，`s: float`）：

   | 调用 | 命中重载（填①~⑤） | 返回类型 |
   | --- | --- | --- |
   | `np.add(s, s)` |  |  |
   | `np.add(s, a)` |  |  |
   | `np.add(a, s)` |  |  |
   | `np.add(a, a)` |  |  |
   | `np.add(a, a, out=a)` |  |  |
   | `np.add(a, a, out=...)` |  |  |

3. 画出决策树（文字版即可），根节点问「两个实参都能匹配 `_ScalarLike_co` 吗？」。

**需要观察的现象 / 预期结果**：

参考答案——

| 调用 | 命中重载 | 返回类型 |
| --- | --- | --- |
| `np.add(s, s)` | ① | `Incomplete`（用户侧 `Any`） |
| `np.add(s, a)` | ② | `NDArray` |
| `np.add(a, s)` | ③ | `NDArray` |
| `np.add(a, a)` | ⑤ | `NDArray | Incomplete` |
| `np.add(a, a, out=a)` | ④ | `NDArray` |
| `np.add(a, a, out=...)` | ④（`out=...` 也匹配 `np.ndarray | tuple[...]`） | `NDArray` |

决策树：

```
两个实参都满足 _ScalarLike_co？
├─ 是 → ① 标量 → Incomplete
└─ 否 → 至少一个是 ArrayLike
        └─ 显式给了 out（ndarray 或 tuple）？
            ├─ 是 → ④ → NDArray
            └─ 否 → 落到兜底 ⑤ → NDArray | Incomplete
        （注：②③ 是「恰好一边是 ndarray」的更窄提前拦截，避免歧义）
```

4. 用 mypy 或 pyright 跑一段带 `reveal_type` 的脚本（示例代码），把推断结果与上表对照：

   ```python
   # 示例代码
   import numpy as np
   a: np.ndarray = np.array([1.0])
   reveal_type(np.add(1.0, 2.0))      # 期望: Any / Incomplete
   reveal_type(np.add(a, 2.0))        # 期望: ndarray[Any, dtype[...]]
   reveal_type(np.add(a, a, out=a))   # 期望: ndarray
   ```

**预期结果**：类型检查器输出的类型应与 reveal 夹具 [ufuncs.pyi:19-20] 一致。若本地未装类型检查器，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么第①条标量重载放在最前面，而不是把兜底的第⑤条放最前？

**答案**：`@overload` 按声明顺序、第一条匹配即生效。`_ScalarLike_co`（标量）是 `ArrayLike` 的子集，如果把宽泛的 `ArrayLike, ArrayLike`（第⑤条）放最前，所有调用都会被它截胡，标量分支永远命中不到。把更窄的标量分支前置，才能让「两个标量 → 标量」这条规则生效。

**练习 2**：`out` 参数的类型里为什么会出现 `EllipsisType`（即允许 `out=...`）？

**答案**：NumPy 的 ufunc 允许用 `...`（Ellipsis）作为「使用默认值」的哨兵，与 `None` 并列。桩文件把它写进 `out: EllipsisType | None = None`，类型检查器才不会对 `out=...` 这种写法报错。

**练习 3**：`np.divmod(a, b)` 的返回类型是 `tuple[Any, Any]` 而不是 `Any`，这靠什么实现？

**答案**：靠 `_UFunc_Nin2_Nout2`（`nout=2`）这个独立子类，其 `__call__` 返回 `_2Tuple[...] = tuple[T, T]`。如果共用 `_UFunc_Nin2_Nout1`，就无法表达「返回两个值」。

---

### 4.3 `Never` / `NoReturn`：表达「方法在此 ufunc 上不可用」

#### 4.3.1 概念说明

`reduce`、`accumulate`、`reduceat`、`outer` 这些方法只对「二进一出」的普通 ufunc 有意义。对其他形状的 ufunc，运行时调用它们会抛 `ValueError`。类型系统需要一个方式来表达「你别在这个 ufunc 上调这个方法」，NumPy 的做法是 `Never`（参数）+ `NoReturn`（返回）的组合：

- **`Never`**：类型论的「底类型」，没有任何值属于它。把方法参数标成 `Never`，等于说「你传任何实参都不合法」——调用处会触发 `arg-type` 报错。
- **`NoReturn`**：表示「函数不会正常返回」（要么抛异常要么死循环）。把返回类型标成 `NoReturn`，等于说「这次调用拿不到结果」。

两者合在一起，静态与运行时的认知就对齐了：调用它在类型上非法、在运行时会抛错。

#### 4.3.2 核心流程

桩文件顶部的 NOTE 把这条规则写得很明白：

[_ufunc.pyi:65-73](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L65-L73) —— `reduce/accumulate/reduceat/outer` 对「非二进一出」的 ufunc 会抛 `ValueError`，相应方法返回 `NoReturn`；多输出 ufunc 上 `at` 不定义、也返回 `NoReturn`。

于是不同子类对这些方法的态度不同：

```
_UFunc_Nin2_Nout1   reduce/accumulate/reduceat/outer/at  → 真实可用（带完整重载）
_UFunc_Nin2_Nout2   reduce/accumulate/reduceat/outer/at  → 全部 Never→NoReturn
_GUFunc_Nin2_Nout1  reduce/accumulate/reduceat/outer/at  → 全部 Never→NoReturn（gufunc 不支持）
```

> 注意一个超出 NOTE 字面的细节：gufunc（如 `np.matmul`）虽然也是 nin=2、nout=1，但它的 `reduce/accumulate/...` 仍是 `NoReturn`（见下文源码与 reveal）。即「不可用」的判定比 NOTE 的字面描述更宽——凡是不支持这些方法语义的 ufunc 都用 `Never/NoReturn`。

#### 4.3.3 源码精读

`_UFunc_Nin2_Nout2`（divmod/modf）把全部约简方法标成不可用：

[_ufunc.pyi:330-334](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L330-L334) —— `accumulate(self, array: Never, /) -> NoReturn`、`reduce(...) -> NoReturn`、`reduceat(...) -> NoReturn`、`outer(...) -> NoReturn`、`at(...) -> NoReturn`。参数一律 `Never`。

`_GUFunc_Nin2_Nout1`（matmul/vecdot）同样如此：

[_ufunc.pyi:387-391](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L387-L391) —— gufunc 的 `accumulate/reduce/reduceat/outer/at` 全部 `NoReturn`。

对比之下，`_UFunc_Nin2_Nout1` 的 `reduce` 是真正可用的、带多条 `@overload`（如 `out=None`→标量、`out=ndarray`→数组、`keepdims=True`→数组）：

[_ufunc.pyi:161-197](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L161-L197) —— 三条重载分别覆盖「默认」「给了 out」「`keepdims=True`」三种返回形态。

reveal 夹具用「`NoReturn` + `# type: ignore[arg-type]`」固化了「不可用」语义。注意这里同时做了两件事：`arg-type` 注释承认「传实参本身就不合法」，`assert_type(..., NoReturn)` 承认「返回类型是 `NoReturn`」：

[ufuncs.pyi:62-69](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L62-L69) —— `np.absolute.outer`、`np.frexp.outer`、`np.divmod.outer`、`np.matmul.outer` 均断言为 `NoReturn` 并附 `# type: ignore[arg-type]`。

[ufuncs.pyi:80-87](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L80-L87) —— `np.absolute.reduce`、`np.divmod.reduce`、`np.matmul.reduce` 同样断言为 `NoReturn`。

#### 4.3.4 代码实践

**实践目标**：亲手触发一次「不可用」的类型错误，体会 `Never`/`NoReturn` 如何与运行时的 `ValueError` 对应。

**操作步骤**（示例代码，非项目原有）：

1. 写一段会被类型检查器拒绝、且运行时会抛错的代码：

   ```python
   # 示例代码
   import numpy as np
   a = np.array([1.0, 2.0])
   r = np.divmod.reduce(a)   # 期望：mypy/pyright 报 arg-type；运行时抛 ValueError
   ```

2. 先用类型检查器跑（`mypy` 或 `pyright`），记录报错信息。
3. 再用 `python` 直接运行，记录抛出的异常类型与消息。

**需要观察的现象 / 预期结果**：

- 类型检查器：对 `np.divmod.reduce(a)` 报「`Argument has incompatible type ...; expected "Never"`」之类的 `arg-type` 错误（因为参数被标成 `Never`）。
- 运行时：抛 `ValueError`，大意是 `reduce` 只支持「二进一出」的 ufunc。

两侧结论一致：静态告诉你别这么调，运行时告诉你为什么别这么调。

> 具体报错文案因检查器版本而异，**待本地验证**；但「静态报错 + 运行时 `ValueError`」这个双重现象是确定的。

#### 4.3.5 小练习与答案

**练习 1**：这里为什么参数用 `Never`、返回用 `NoReturn`，而不是两者都用 `NoReturn`？

**答案**：`NoReturn` 是函数返回位置的标注（「不会正常返回」），不能直接用作参数类型来表达「别传值」。`Never` 才是「没有任何值属于此类型」的底类型，放在参数上能让「任何实参」都不匹配、从而在调用处触发 `arg-type` 报错。两者职责不同：`Never` 把「调用非法」钉在入参侧，`NoReturn` 把「拿不到结果」钉在返回侧。

**练习 2**：`np.matmul` 是 nin=2、nout=1，按 NOTE 字面似乎「应该支持 reduce」，为什么它的 `reduce` 也是 `NoReturn`？

**答案**：`np.matmul` 是 gufunc（带核心维度签名），`reduce/accumulate` 的「沿轴折叠」语义对核心维度运算不适用，运行时同样会抛 `ValueError`。所以「不可用」的真正判据不是单纯的 nin/nout，而是「该 ufunc 是否支持这些方法的语义」；gufunc 不支持，于是也用 `Never/NoReturn`。reveal 夹具 [ufuncs.pyi:86-87] 证实了这一点。

---

### 4.4 `Unpack` TypedDict kwargs：把关键字参数精确化

#### 4.4.1 概念说明

ufunc 调用除了位置输入，还接受一堆关键字参数：`where`（掩码）、`casting`（类型转换策略）、`order`（内存序）、`subok`（是否允许子类）、`signature`（强制固定 dtype）。问题是：`**kwargs` 该怎么标？

- 写成 `**kwargs: Any` 太松，类型检查器无法校验键名和取值。
- 写成 `**kwargs: str` 也不对——那表示「所有值都是 `str`」，而 `where` 是数组、`subok` 是 `bool`。

PEP 692 给出了正确答案：`**kwargs: Unpack[SomeTypedDict]`。`TypedDict` 给每个键单独定类型，`Unpack` 把它「展开」到 `**kwargs` 上，于是每个关键字参数都有了精确的名字和类型。

#### 4.4.2 核心流程

NumPy 为不同方法准备了不同的 kwargs TypedDict：

```
_UFunc3Kwargs        __call__/outer 用：where/casting/order/subok/signature
_ReduceKwargs        reduce 用：initial/where
_PyFunc_Kwargs_Nargs2/3/3P/4P   frompyfunc ufunc 用：含 dtype，signature 是 DTypeLike 元组
```

每个 TypedDict 都 `total=False`（所有键可选）。然后在方法签名里用 `**kwds: Unpack[_UFunc3Kwargs]` 引用。

#### 4.4.3 源码精读

`_UFunc3Kwargs` 的定义：

[_ufunc.pyi:52-58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L52-L58) —— `where: _ArrayLikeBool_co | None`、`casting: _CastingKind`、`order: _OrderKACF`、`subok: bool`、`signature: _3Tuple[str | None] | str | None`；`total=False`。其中 `_CastingKind`/`_OrderKACF` 是 `numpy` 暴露的 `Literal` 别名（[_ufunc.pyi:27] 从 `numpy` 导入），把 `casting` 限定为 `{"no","equiv","safe","same_kind","unsafe"}` 这类字面量。

`Unpack` 的导入与用法：

[_ufunc.pyi:11-24](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L11-L24) —— 从 `typing` 导入 `TypedDict`、`Unpack`、`overload`、`type_check_only` 等。

在 `__call__` 第①条重载里落到 `**kwds`：

[_ufunc.pyi:96-106](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L96-L106) —— `**kwds: Unpack[_UFunc3Kwargs]`，于是 `where=...`、`casting=...` 等都有名有型。

`reduce` 用的是另一套 kwargs（注意 `initial` 标成 `Incomplete`，因为它有个「无值」哨兵默认）：

[_ufunc.pyi:60-63](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L60-L63) 与 [_ufunc.pyi:161-173](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_ufunc.pyi#L161-L173) —— `_ReduceKwargs` 含 `initial: Incomplete` 与 `where`；`reduce` 重载用 `**kwargs: Unpack[_ReduceKwargs]`。

`_ArrayLikeBool_co`（`where` 的类型）来自 `_array_like`：

[_array_like.py:59](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L59) —— `_ArrayLikeBool_co = _DualArrayLike[np.dtype[np.bool], bool]`，即「能转成 bool 数组的东西」，正是 `where` 掩码该有的形状。

#### 4.4.4 代码实践

**实践目标**：对比「`Unpack[TypedDict]`」与「`**kwargs: SomeType`」两种写法在类型检查器下的差异，直观体会 PEP 692 的价值。

**操作步骤**（示例代码，非项目原有）：

1. 写两段对比代码，用 mypy 或 pyright 跑：

   ```python
   # 示例代码
   from typing import TypedDict, Unpack

   class Opts(TypedDict, total=False):
       where: bool
       casting: str

   # 写法 A：PEP 692，键名/类型都被校验
   def call_a(x: int, **kwds: Unpack[Opts]) -> int: ...
   call_a(1, where=True)        # OK
   call_a(1, casting="safe")    # OK
   call_a(1, were=True)         # 期望：报错（键名拼错）
   call_a(1, where="no")        # 期望：报错（值类型错）

   # 写法 B：传统 **kwargs，不校验键名
   def call_b(x: int, **kwargs: object) -> int: ...
   call_b(1, were=True)         # 不报错（键名不被检查）
   ```

2. 记录两种写法下，类型检查器对「拼错的键名」「错误的值类型」是否报警。

**需要观察的现象 / 预期结果**：

- 写法 A：`were`（拼错）和 `where="no"`（值类型不符 `bool`）都会被报错；这正是 `_UFunc3Kwargs` + `Unpack` 让 ufunc 关键字参数变得可校验的原理。
- 写法 B：键名错误不被发现——这正是 numpy 不采用它的原因。

> 不同检查器对 `Unpack` 的支持与报错文案略有差异（需较新版本），**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_UFunc3Kwargs` 要标 `total=False`？

**答案**：`total=False` 表示所有键都可选。ufunc 调用时 `where`/`casting`/`order` 等都有默认值，用户通常一个都不传；如果 `total=True`（默认），类型检查器会要求每个键都必须出现，那 `np.add(a, b)` 这种最普通的调用反而通不过。

**练习 2**：`reduce` 的 `initial` 为什么标成 `Incomplete` 而不是某个具体类型？

**答案**：`initial` 的运行时默认是一个特殊的「无值」哨兵（不是 `None`，表示「不提供 initial」），它的合法取值类型又取决于被约简的数组元素类型，难以用一个静态类型表达。标成 `Incomplete`（typeshed 的「未知」哨兵）等于告诉检查器「别对这个值做强校验」，避免误报。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「为一个真实 ufunc 调用画出完整的类型推断表，并补一个 reveal 夹具」的任务。

**任务**：选定 `np.divmod`（`_UFunc_Nin2_Nout2`），回答下列问题并产出一份 `.pyi` 风格的断言清单。

1. **形状定位**：`np.divmod` 属于哪个 `@type_check_only` 子类？它的 `nin`/`nout`/`nargs`/`signature` 各是什么 `Literal`？（依据 [ufuncs.pyi:28-35]。）
2. **返回形状**：`np.divmod(f8, f8)` 与 `np.divmod(AR_f8, f8)` 的返回类型分别是什么？为什么是二元组？（依据 [_ufunc.pyi:295-328] 与 [ufuncs.pyi:36-37]。）
3. **不可用方法**：`np.divmod.reduce(a)`、`np.divmod.outer(a, a)`、`np.divmod.at(...)` 各应断言成什么？为什么？（依据 [_ufunc.pyi:330-334] 与 [ufuncs.pyi:67/95/101]。）
4. **kwargs**：`np.divmod` 的 `__call__` 关键字参数由哪个 TypedDict 描述？其中 `where` 的类型是什么、来自哪里？（依据 [_ufunc.pyi:52-58] 与 [_array_like.py:59]。）
5. **产出**：仿照 reveal 夹具风格，写 5~8 行 `assert_type(...)`，覆盖「标量入、数组入、out、reduce 不可用」四种情形。

**参考产出**（示例代码）：

```python
# 示例代码（reveal 风格，非项目原有文件）
from typing import Any, NoReturn, assert_type
import numpy as np
import numpy.typing as npt

f8: np.float64
AR_f8: npt.NDArray[np.float64]

# 形状
assert_type(np.divmod.nout, Any)        # Literal[2]（由桩给定，用户侧见字面量）
# 返回形状：二元组
assert_type(np.divmod(f8, f8), tuple[Any, Any])
assert_type(np.divmod(AR_f8, f8), tuple[npt.NDArray[Any], npt.NDArray[Any]])
# 不可用方法：Never 参数 + NoReturn 返回
assert_type(np.divmod.reduce(AR_f8), NoReturn)       # type: ignore[arg-type]
assert_type(np.divmod.outer(AR_f8, AR_f8), NoReturn) # type: ignore[arg-type]
```

把你的断言清单与本仓库 [ufuncs.pyi:28-37](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi#L28-L37) 的官方版本对照，若一致则说明你已经掌握了 ufunc 类型建模的全链路。**若本地无 mypy 环境，断言的静态校验部分待本地验证**；但每条断言的「期望类型」可完全由本讲源码推出，不依赖运行。

## 6. 本讲小结

- NumPy 把 `ufunc` 按 `nin`/`nout`（以及是否为 gufunc）拆成若干 `@type_check_only` 的私有子类（`_UFunc_Nin2_Nout1`/`_UFunc_Nin2_Nout2`/`_GUFunc_Nin2_Nout1` 等），运行时它们都等于基类 `ufunc`（`_ufunc.py` 仅 3 行占位），只在 `.pyi` 里存在——这是 u5-l1 双轨制的最复杂样本。
- 子类用 PEP 695 类型参数（`NameT: LiteralString`、`NTypesT: int`、`IdentT`、`SignatureT`）把「名字/循环数/单位元/核心维度签名」编码进类型，再通过 `@property` 暴露，使 `np.add.__name__` 推断为 `Literal["add"]`。
- `__call__` 用一串 `@overload` 表达「标量入→标量 / 数组入→数组 / 给 `out`→数组」的映射；窄分支（标量、单边 ndarray）在前、宽分支（`ArrayLike, ArrayLike`）兜底在后，靠声明顺序消歧。
- `Never`（参数）+ `NoReturn`（返回）组合表达「该方法对该 ufunc 不可用」，与运行时的 `ValueError` 对齐；`_UFunc_Nin2_Nout2` 与 `_GUFunc_Nin2_Nout1` 的全部约简方法都是这种标记。
- ufunc 的关键字参数用 `Unpack[TypedDict]`（PEP 692）精确化：`_UFunc3Kwargs`/`_ReduceKwargs` 给每个键（`where`/`casting`/`order`/`subok`/`signature`）定类型，`total=False` 让它们全部可选。

## 7. 下一步学习建议

- **下一讲 u5-l3**会把视角从「单个模块的桩」拉到「文档生成」：看 `_add_docstring` 如何把 `ArrayLike`/`DTypeLike`/`NDArray` 这类 PEP 695 `type` 别名（它们没有 `__doc__`）的文档拼成 sphinx data 域文本。建议先回顾 u1-l2 里「公共壳转发的四个别名」。
- **横向阅读**：打开 `numpy/typing/tests/data/reveal/ufuncs.pyi` 通读一遍，它是本讲所有结论的「黄金标准」；再对照 `numpy/typing/tests/data/pass/` 与 `fail/` 下任何 ufunc 相关夹具，体会「reveal=钉死推断、pass=应通过、fail=应报错」三类夹具的分工（这会为 u6-l1 的静态测试方法论铺路）。
- **进阶拓展**：本讲未展开的 `_PyFunc_*` 家族（[_ufunc.pyi:429-828]）是 `np.frompyfunc` 造出的 Python 回调 ufunc 的模型，结构相似但用 `ReturnT` 类型参数承载「用户自定义返回类型」。学有余力可自行对比 `_PyFunc_Nin2_Nout1` 与 `_UFunc_Nin2_Nout1` 的异同，思考为什么前者到处出现 `NDArray[np.object_]`（提示：frompyfunc 的元素类型在静态期不可知）。
