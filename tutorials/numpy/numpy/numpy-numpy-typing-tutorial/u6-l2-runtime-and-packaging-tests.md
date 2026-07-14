# 运行时类型测试与打包完整性测试

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分「静态类型测试」（上一讲 u6-l1 的 mypy reveal/pass/fail）与「运行时类型测试」（本讲）——前者检查注解是否符合类型规则，后者检查这些类型对象在真正的 Python 进程里能否被正常使用。
- 理解 PEP 695 `type` 语句产生的 `TypeAliasType` 在运行时是**可内省**的：可以用 `__value__` 剥壳，再用 `get_args` / `get_origin` / `get_type_hints` 拆解。
- 读懂 `test_runtime.py` 如何用一张 `TYPES` 字典 + `test_keys` 保证测试用例与公共 `__all__` 永远同步。
- 理解 `@runtime_checkable` 协议（`_SupportsArray` 等）在运行时的 `isinstance` / `issubclass` 行为，以及它与静态检查的差异。
- 理解 `test_isfile.py` 为什么是「打包完整性」的兜底测试：它确保 PEP 561 的 `py.typed` 标记与各 `.pyi` 桩文件真的随包安装到了用户机器上。

---

## 2. 前置知识

本讲承接 u5-l1（`.py` / `.pyi` 双轨制）与 u3-l1（`_SupportsArray` / `_SupportsArrayFunc` 协议）。在进入源码前，先用三段话把几个运行时概念讲清楚。

### 2.1 PEP 695 `type` 语句与 `TypeAliasType`

Python 3.12 的 PEP 695 引入了新语法：

```python
type ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]
```

它和传统的 `ArrayLike = Buffer | ...`（直接赋值）有一个关键区别：`type` 语句创建的不是「赋值后的那个表达式对象」，而是一个 [`types.TypeAliasType`](https://docs.python.org/3/library/types.html#types.TypeAliasType) 实例。这个实例像一个「包装盒」，盒子上贴着名字（`__name__`），盒子里装着真正的类型表达式（`__value__`）。

为什么要套一层盒子？因为带类型参数的别名（如 `type NDArray[ScalarT] = ...`）需要一个对象来承载 `ScalarT` 这个类型参数。盒子让「别名本身」和「别名展开后的类型表达式」成为两个可区分的对象。

这对测试很重要：要在运行时拆开 NumPy 的类型别名，就必须先 `alias.__value__` 剥掉这层盒子，再交给 `typing.get_args` 等工具。

### 2.2 `get_args` / `get_origin` / `get_type_hints`

这三个函数来自标准库 `typing`，是「类型内省三件套」：

| 函数 | 输入 | 输出 | 举例 |
|---|---|---|---|
| `get_origin` | 一个泛型类型 | 它的「原始类」 | `get_origin(list[int])` → `list` |
| `get_args` | 一个泛型类型 | 它的参数元组 | `get_args(list[int])` → `(int,)` |
| `get_type_hints` | 一个函数/类 | 解析后的注解字典 | 把字符串注解 `"int"` 解析成真正的 `int` |

注意：对**普通类**（非泛型），`get_args` 返回空元组 `()`、`get_origin` 返回 `None`。本讲会看到 `NBitBase` 正是这种情况——它是一个真实的运行时类，而不是类型别名。

### 2.3 `@runtime_checkable` 协议的运行时检查

u3-l1 讲过：`Protocol` 子类加上 `@runtime_checkable` 装饰器后，可以用 `isinstance` / `issubclass` 检查。但运行时检查是**浅层**的：它只看「这个属性/方法名是否存在」，不看签名、不看返回类型。本讲的 `TestRuntimeProtocol` 正是验证这些协议在运行时确实「能被 isinstance」。

---

## 3. 本讲源码地图

本讲只围绕两个测试文件展开（外加少量被测对象的定义）：

| 文件 | 作用 |
|---|---|
| [numpy/typing/tests/test_runtime.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py) | 运行时类型测试：验证 PEP 695 别名可内省、`__all__` 与测试同步、协议可被 `isinstance`。 |
| [numpy/typing/tests/test_isfile.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py) | 打包完整性测试：验证 `py.typed` 与各子包 `__init__.pyi` 随包安装。 |
| [numpy/typing/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py) | 公共壳，提供 `__all__` 与四个被测别名（u1-l2 已讲）。 |
| numpy/_typing/_array_like.py 等 | 被测别名的真实定义（ArrayLike/DTypeLike/NDArray/NBitBase），仅供追溯。 |

> 提示：本讲的两个测试文件都在 `numpy/typing/tests/` 下。永久链接的 base 指向 `numpy/typing/`，所以测试文件的相对路径是 `tests/test_runtime.py`。

---

## 4. 核心概念与源码讲解

### 4.1 运行时可内省性：`TypeAliasType` 与 `get_args`/`get_origin`/`get_type_hints`

#### 4.1.1 概念说明

类型检查器（mypy/pyright）关心的是「这些注解是否合法、是否冲突」。但 NumPy 还想保证另一件事：**这些类型别名在真正的 Python 解释器里也是「正常对象」**——可以被 `get_args` 拆解、可以作为函数注解、可以通过字符串反查回来。

为什么要单独测这件事？因为 PEP 695 的 `type` 语句是较新的语法，它的运行时对象（`TypeAliasType`）是否被标准库 `typing` 的内省函数正确支持，曾经是不确定的。如果某天 CPython 改了实现、导致 `get_args(npt.ArrayLike.__value__)` 抛异常，那么任何在运行时依赖类型反射的下游库（如 pydantic、beartype）都会跟着崩。`test_runtime.py` 就是这个契约的守门员。

#### 4.1.2 核心流程

整个 4.1 的测试逻辑可以概括为「剥壳 → 拆解 → 回填」三步：

```
PEP 695 别名 (TypeAliasType 盒子)
        │  alias.__value__      ← 剥壳
        ▼
真正的类型表达式 (union / generic alias)
        │  get_args / get_origin ← 拆解
        ▼
(args 元组, origin 类)            ← 记录为参考值 ref
        │  作为函数注解 a: typ
        ▼
get_type_hints(func)              ← 回填：能否解析回来
```

关键点：测试并不硬编码「ArrayLike 的 args 必须等于某个魔法元组」，而是**自洽地**记录——先用 `from_type_alias` 算出参考值存起来，再在测试里重新算一遍，断言两次结果相等。它真正断言的是「**不抛异常 + 结果稳定可复现**」，而不是某个具体值。

#### 4.1.3 源码精读

测试的入口是一个 `NamedTuple` 容器 `TypeTup`，以及它的工厂方法 `from_type_alias`：

[numpy/typing/tests/test_runtime.py:L20-L30](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L20-L30) —— 定义 `TypeTup`，用 `alias.__value__` 剥掉 PEP 695 的 `TypeAliasType` 盒子，再交给 `get_args` / `get_origin`：

```python
class TypeTup(NamedTuple):
    typ: type                 # 类型表达式（剥壳后的值）
    args: tuple[type, ...]    # 泛型参数
    origin: type | None       # 如 UnionType 或 GenericAlias

    @classmethod
    def from_type_alias(cls, alias: TypeAliasType, /) -> Self:
        # PEP 695 `type _ = ...` 别名把类型表达式包成
        # types.TypeAliasType，其 __value__ 才是真正的表达式
        tp = alias.__value__
        return cls(typ=tp, args=get_args(tp), origin=get_origin(tp))
```

注意三个细节：

1. `alias.__value__` 这一步是核心——它把 `TypeAliasType`（盒子）展开成真正的类型表达式（如 `Buffer | _DualArrayLike[...]`）。没有这一步，`get_args(npt.ArrayLike)` 拿到的是盒子的参数（空），而不是类型表达式的参数。
2. 参数类型标注 `alias: TypeAliasType`（[第 7 行](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L7) 从 `typing` 导入）显式声明「我只接受 PEP 695 别名」。
3. `cls(...)` 用关键字参数构造 `NamedTuple`，字段一一对应。

接下来是三个参数化测试，它们对 `TYPES` 里的每一项都跑一遍：

[numpy/typing/tests/test_runtime.py:L41-L66](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L41-L66) —— `test_get_args` / `test_get_origin` / `test_get_type_hints`，分别验证拆解与回填：

```python
@pytest.mark.parametrize("name,tup", TYPES.items(), ids=TYPES.keys())
def test_get_args(name: type, tup: TypeTup) -> None:
    typ, ref = tup.typ, tup.args
    out = get_args(typ)
    assert out == ref
```

三个测试的模式完全一致：从 `tup` 取出参考值 `ref`，重新调用内省函数得到 `out`，断言 `out == ref`。其中 `test_get_type_hints` 稍有不同——它把类型表达式**当函数注解**用，再让 `get_type_hints` 解析回来：

```python
def test_get_type_hints(name: type, tup: TypeTup) -> None:
    typ = tup.typ
    def func(a: typ) -> None: pass      # 类型表达式当注解
    out = get_type_hints(func)
    ref = {"a": typ, "return": type(None)}
    assert out == ref
```

这验证了「这些类型对象能经历 `注解 → get_type_hints` 的完整往返」。

还有一个字符串版本，验证注解写成字符串（前向引用风格）也能解析：

[numpy/typing/tests/test_runtime.py:L69-L78](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L69-L78) —— `test_get_type_hints_str`，用 `"npt.ArrayLike"` 这种字符串注解，期望解析回 `getattr(npt, name)`：

```python
def test_get_type_hints_str(name: type, tup: TypeTup) -> None:
    typ_str, typ = f"npt.{name}", tup.typ
    def func(a: typ_str) -> None: pass
    out = get_type_hints(func)
    ref = {"a": getattr(npt, str(name)), "return": type(None)}
    assert out == ref
```

> **微妙之处**：直接注解版（`test_get_type_hints`）的 `typ` 是 `alias.__value__`（剥壳后的表达式），而字符串版（`test_get_type_hints_str`）解析回来的是 `getattr(npt, name)`——即 `TypeAliasType` 盒子**本身**（`npt.ArrayLike`，未剥壳）。两种写法的注解值不同，但都合法、都能被 `get_type_hints` 接受。

#### 4.1.4 代码实践

**实践目标**：亲手复刻 `TypeTup.from_type_alias`，对 `npt.ArrayLike` 调用 `get_args` / `get_origin`，记录并解释结果。

**操作步骤**：

1. 新建 `introspect_alias.py`：

```python
# 示例代码：复刻 test_runtime 的剥壳与拆解
from types import UnionType
from typing import get_args, get_origin
import numpy.typing as npt

def peek(alias):
    tp = alias.__value__            # 剥掉 TypeAliasType 盒子
    print(f"别名        : {alias}")
    print(f"  __value__ : {tp}")
    print(f"  type      : {type(tp).__name__}")
    print(f"  get_origin: {get_origin(tp)}")
    print(f"  get_args  : {get_args(tp)}")
    print()

peek(npt.ArrayLike)
peek(npt.DTypeLike)
peek(npt.NDArray)          # 注意：泛型别名，未订阅
```

2. 运行：`python introspect_alias.py`（需要 Python 3.12+ 与 numpy 2.x）。

**需要观察的现象 / 预期结果**（部分细节待本地验证，以实际输出为准）：

- `ArrayLike` 与 `DTypeLike` 的 `__value__` 是用 `|` 拼出的联合类型，`type(...)` 应为 `UnionType`，`get_origin` 返回 `types.UnionType`，`get_args` 返回顶层各分支组成的元组。
- `ArrayLike` 的 `get_args` 大致是 `(Buffer, _DualArrayLike[np.dtype, complex | bytes | str])` 两个顶层成员——`|` 会展平嵌套联合，但**不会**展开被订阅的类型别名 `_DualArrayLike[...]`，它作为一个整体保留。具体元素的 `repr` 待本地验证。
- `DTypeLike` 的 `get_args` 大致是 `(type, str, np.dtype, _SupportsDType[np.dtype], _VoidDTypeLike)` 五个分支。
- `NDArray` 是**带类型参数**的别名（`type NDArray[ScalarT: np.generic] = ...`），其 `__value__` 是一个泛型别名，`get_origin` 应为 `numpy.ndarray`，`get_args` 为形状与 dtype 两个参数（其中含类型参数 `ScalarT` 的精确 repr 待本地验证）。

> 对比 `NBitBase`：它**不是** `type` 别名而是普通类，没有 `__value__`，所以测试里被单独处理（见 4.2.3）。你可以顺手 `print(get_args(npt.NBitBase), get_origin(npt.NBitBase))`，应得到 `() None`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `from_type_alias` 必须先 `alias.__value__`，而不能直接 `get_args(alias)`？

> **答案**：`alias` 是 `TypeAliasType` 盒子，盒子本身的「参数」是它的类型形参（如 `NDArray` 的 `ScalarT`），而不是右边类型表达式的参数。`get_args(alias.__value__)` 才能拆到真正的联合分支或泛型实参。

**练习 2**：`test_get_args` 断言 `get_args(typ) == tup.args`，而 `tup.args` 本身就是 `get_args(tp)` 算出来的。这个测试岂不是「永远为真」？它到底在测什么？

> **答案**：它测的是「**可内省性 + 稳定性**」：调用 `get_args` / `get_origin` 不抛异常、对同一对象两次调用结果一致（确定性），并且 `TypeAliasType.__value__` 这个属性确实存在可用。它不是在断言某个硬编码的魔法值，而是在守护「PEP 695 别名 + 标准库内省」这条契约不 regress。

**练习 3**：把脚本里的 `peek(npt.NDArray)` 换成 `peek(npt.NDArray[np.float64])`（先订阅再剥壳），`get_args` 会变成什么？

> **答案**：订阅后 `__value__` 里的 `ScalarT` 被替换为 `np.float64`，`get_args` 的 dtype 分支应变成 `numpy.dtype[numpy.float64]`（形状 `_AnyShape` 不变）。具体 repr 待本地验证。

---

### 4.2 `TYPES` 字典与 `test_keys`：测试与 `__all__` 同步

#### 4.2.1 概念说明

`numpy.typing` 的公共面由 `__all__` 声明（u1-l2 讲过：四个名字 `ArrayLike` / `DTypeLike` / `NBitBase` / `NDArray`）。本讲的 `TYPES` 字典是测试自己维护的一份「我要测哪些别名」清单。两份清单一旦不一致，就会出现「公开了却没测」或「测了却没公开」的漏洞。`test_keys` 用一个断言把两者绑死，防止人为遗忘。

这是一个很值得学习的工程模式：**测试清单与公共 API 契约之间应该有自动同步检查**。

#### 4.2.2 核心流程

```
TYPES = {"ArrayLike": ..., "DTypeLike": ..., "NBitBase": ..., "NDArray": ...}
                                    │
                                    ▼
                        test_keys: TYPES.keys() == set(npt.__all__) ?
                                    │
                        ┌───────────┴───────────┐
                     一致 → 通过              不一致 → 失败
                  (双向覆盖)               (有人改了一边忘了另一边)
```

#### 4.2.3 源码精读

`TYPES` 字典把四个公共别名装进 `TypeTup`：

[numpy/typing/tests/test_runtime.py:L33-L38](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L33-L38) —— `TYPES` 字典，三个别名走 `from_type_alias`，`NBitBase` 单独手工构造：

```python
TYPES = {
    "ArrayLike": TypeTup.from_type_alias(npt.ArrayLike),
    "DTypeLike": TypeTup.from_type_alias(npt.DTypeLike),
    "NBitBase": TypeTup(npt.NBitBase, (), None),  # type: ignore[deprecated]
    "NDArray": TypeTup.from_type_alias(npt.NDArray),
}
```

注意 `NBitBase` 这一行有两个细节：

1. 它**不走** `from_type_alias`，而是直接 `TypeTup(npt.NBitBase, (), None)`——因为它是一个真实的运行时类（[定义在 numpy/_typing/_nbit_base.py:L9-L62](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L9-L62)），不是 `type` 别名，没有 `__value__`。对普通类，`get_args` 返回 `()`、`get_origin` 返回 `None`，所以手工填这两个参考值。
2. 行尾的 `# type: ignore[deprecated]`（以及 `# pyright: ignore[reportDeprecated]`）是为了**静态**层面消音——因为 `NBitBase` 自 2.3 起被 `@deprecated` 标记（u5-l4 讲过，信号来自桩文件 `_nbit_base.pyi` 上的 `typing_extensions.deprecated`）。这正是 u5-l4 的结论的一个活样本：**静态弃用警告与运行时无关**，这里测试代码要在静态层面把它关掉，运行时该测试照常跑。

然后是同步检查：

[numpy/typing/tests/test_runtime.py:L81-L85](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L81-L85) —— `test_keys` 断言测试清单与公共 `__all__` 完全一致：

```python
def test_keys() -> None:
    """Test that ``TYPES.keys()`` and ``numpy.typing.__all__`` are synced."""
    keys = TYPES.keys()
    ref = set(npt.__all__)
    assert keys == ref
```

对照公共壳里的 `__all__`：[numpy/typing/\_\_init\_\_.py:L177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L177) 声明 `__all__ = ["ArrayLike", "DTypeLike", "NBitBase", "NDArray"]`，两者必须逐字相等。

#### 4.2.4 代码实践

**实践目标**：亲手制造一次「不同步」并观察 `test_keys` 失败。

**操作步骤**：

1. 在本地 clone 里，临时给 `numpy/typing/__init__.py` 的 `__all__` 末尾加一个名字（例如 `"Dummy"`），**不**改测试。
2. 运行：`python -m pytest numpy/typing/tests/test_runtime.py::test_keys -v`。
3. 观察失败信息里的集合差异。
4. **务必还原** `__all__`（不要提交这个改动）。

**预期结果**：`test_keys` 失败，报类似 `AssertionError: assert {'ArrayLike', ..., 'Dummy'} == {'ArrayLike', ...}`，多出的 `'Dummy'` 会被高亮。这直观展示了该测试如何挡住「公共面悄悄扩张、测试却没跟上」。

#### 4.2.5 小练习与答案

**练习 1**：如果有人在 `__all__` 里加了一个新别名 `FooLike`，同时在 `TYPES` 里也加了对应条目，`test_keys` 会通过吗？4.1 的参数化测试会自动覆盖它吗？

> **答案**：`test_keys` 会通过（两边集合相等）。4.1 的 `test_get_args` 等用 `@pytest.mark.parametrize(..., TYPES.items(), ...)` 参数化，所以新条目会**自动**生成新的测试用例——前提是它在 `TYPES` 里登记了。这正是把清单集中到 `TYPES` 一处的好处。

**练习 2**：为什么 `NBitBase` 在 `TYPES` 里要用 `TypeTup(..., (), None)` 而不是 `from_type_alias`？

> **答案**：`NBitBase` 是普通运行时类，不是 PEP 695 `type` 别名，没有 `__value__`；且它非泛型，`get_args` 恒为 `()`、`get_origin` 恒为 `None`，故直接手工填参考值。

---

### 4.3 `@runtime_checkable` 协议的运行时 `isinstance` / `issubclass`

#### 4.3.1 概念说明

u3-l1 讲过 `_SupportsArray`（`__array__`）和 `_SupportsArrayFunc`（`__array_function__`），u3-l2 讲过 `_NestedSequence`（嵌套序列）。它们都是 `@runtime_checkable` 的 `Protocol`。本讲的 `TestRuntimeProtocol` 验证这些协议在**运行时**确实能被 `isinstance` / `issubclass` 检查，并给出正反两组样本（一个真数组 / 一个 `None`）。

这里要再次强调 u3-l1 的核心结论：运行时检查是**浅层**的——只看属性/方法名在不在，不看签名。所以「运行时 isinstance 为真」并不等于「静态类型匹配」。

#### 4.3.2 核心流程

```
PROTOCOLS = {协议名: (协议类, 样本对象)}
                │
                ▼  pytest 参数化（按类组织）
        TestRuntimeProtocol
          ├─ test_isinstance: isinstance(样本, 协议) 为真；isinstance(None, 协议) 为假
          └─ test_issubclass: issubclass(type(样本), 协议) 为真；issubclass(type(None), 协议) 为假
```

#### 4.3.3 源码精读

先看协议清单与样本：

[numpy/typing/tests/test_runtime.py:L88-L92](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L88-L92) —— `PROTOCOLS` 字典，每个协议配一个「应当匹配」的样本：

```python
PROTOCOLS: dict[str, tuple[type[Any], object]] = {
    "_SupportsArray": (_npt._SupportsArray, np.arange(10)),
    "_SupportsArrayFunc": (_npt._SupportsArrayFunc, np.arange(10)),
    "_NestedSequence": (_npt._NestedSequence, [1]),
}
```

样本选择有讲究：

- `_SupportsArray` / `_SupportsArrayFunc` 用 `np.arange(10)`——`numpy.ndarray` 同时实现了 `__array__` 与 `__array_function__`（u3-l1 讲过，ndarray 是同时满足这两个协议的对象）。
- `_NestedSequence` 用 `[1]`——`list` 是序列，满足嵌套序列协议。

注意这三个协议都从**私有** `numpy._typing` 导入（`import numpy._typing as _npt`，[第 16 行](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L16)）。这些是内部积木，测试有权直接访问。

然后用一个参数化**类**把两个测试组织在一起：

[numpy/typing/tests/test_runtime.py:L95-L103](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L95-L103) —— `TestRuntimeProtocol`，验证正反两组 `isinstance` / `issubclass`：

```python
@pytest.mark.parametrize("cls,obj", PROTOCOLS.values(), ids=PROTOCOLS.keys())
class TestRuntimeProtocol:
    def test_isinstance(self, cls: type[Any], obj: object) -> None:
        assert isinstance(obj, cls)
        assert not isinstance(None, cls)

    def test_issubclass(self, cls: type[Any], obj: object) -> None:
        assert issubclass(type(obj), cls)
        assert not issubclass(type(None), cls)
```

两个断言一正一反：

- 正：样本对象/类型匹配协议（`isinstance(np.arange(10), _SupportsArray)` 为真）。
- 反：`None` 不匹配（`isinstance(None, _SupportsArray)` 为假）——这一步很重要，否则一个「永远返回真」的错误协议实现会被漏过。

> `@pytest.mark.parametrize` 装饰**类**时，参数会注入到类里**每个**测试方法。所以这里每个协议生成 2 个用例（isinstance + issubclass），3 个协议共 6 个用例，`ids=PROTOCOLS.keys()` 让用例名可读（如 `TestRuntimeObject._SupportsArray.test_isinstance`）。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `@runtime_checkable` 协议的 `isinstance` 是「浅层」的——用一个「有同名属性但签名不对」的假对象骗过它。

**操作步骤**：

1. 写 `fake_protocol.py`：

```python
# 示例代码：证明运行时协议检查只看名字
import numpy._typing as _npt

class FakeArray:
    # 故意写成无参 property 之外的形式也没关系，运行时只看名字在不在
    def __array__(self, dtype=None):  # 名字对就行
        import numpy as np
        return np.arange(3)

obj = FakeArray()
print("isinstance(FakeArray(), _SupportsArray) =", isinstance(obj, _npt._SupportsArray))
print("isinstance([1], _NestedSequence)        =", isinstance([1], _npt._NestedSequence))
print("isinstance(None, _SupportsArray)        =", isinstance(None, _npt._SupportsArray))
```

2. 运行：`python fake_protocol.py`。

**预期结果**：三条都符合预期——`FakeArray` 即使不是 `np.ndarray` 子类、签名也未必与协议严格一致，只要 `__array__` 名字存在，`isinstance` 就为真；`[1]` 满足 `_NestedSequence`；`None` 为假。

**需要观察的现象**：这正是 u3-l1 强调的「运行时为真 ≠ 静态匹配」——静态检查器会严格比对 `__array__` 的签名与返回类型，运行时只看名字。这两套检查是互补的，不能互相替代。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `test_isinstance` 里要同时断言 `assert not isinstance(None, cls)`？

> **答案**：防止协议实现退化成「永远返回真」。一个错误地把所有对象都判为匹配的 `@runtime_checkable`，只有用反例（如 `None`）才能暴露。

**练习 2**：如果把 `_NestedSequence` 的样本从 `[1]` 换成 `1`（整数），`isinstance(1, _NestedSequence)` 会是什么结果？为什么？

> **答案**：为假。`int` 没有 `__getitem__` / `__iter__` 等序列方法，不满足 `_NestedSequence` 协议。运行时检查会发现这些方法名不存在而返回假。

---

### 4.4 打包完整性：`test_isfile` 验证桩文件随包安装

#### 4.4.1 概念说明

u1-l3 讲过 PEP 561：NumPy 是「自带类型」的包（inline typed），靠根目录的 `py.typed` 空标记文件宣告「我自带类型」，并靠各模块的 `.pyi` 桩文件提供类型信息。

这里有个工程陷阱：`.pyi` 是**纯类型文件**，对运行时毫无作用。如果打包（wheel / sdist）时漏装了某个 `.pyi`，**运行时一切正常**，但类型检查器会悄悄退化——找不到桩就回退到读 `.py` 注解，甚至完全失去类型信息。这种「静默退化」极难发现。

`test_isfile.py` 就是兜底：它直接检查已安装包目录里这些关键文件**确实存在**，把「漏装桩文件」变成一个会失败的测试。

#### 4.4.2 核心流程

```
ROOT = numpy 包的安装目录 (Path(np.__file__).parents[0])
        │
        ▼
FILES = [ROOT/"py.typed", ROOT/"__init__.pyi", ROOT/"<子包>"/"__init__.pyi", ...]
        │
        ▼  对每个文件
os.path.isfile(file)  →  全部为真才通过
```

#### 4.4.3 源码精读

先定位 numpy 包根目录并列出要检查的文件：

[numpy/typing/tests/test_isfile.py:L9-L24](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L9-L24) —— `ROOT` 与 `FILES` 清单，覆盖 `py.typed` 与所有子包的 `__init__.pyi`：

```python
ROOT = Path(np.__file__).parents[0]
FILES = [
    ROOT / "py.typed",
    ROOT / "__init__.pyi",
    ROOT / "ctypeslib" / "__init__.pyi",
    ROOT / "_core" / "__init__.pyi",
    ROOT / "f2py" / "__init__.pyi",
    ROOT / "fft" / "__init__.pyi",
    ROOT / "lib" / "__init__.pyi",
    ROOT / "linalg" / "__init__.pyi",
    ROOT / "ma" / "__init__.pyi",
    ROOT / "matrixlib" / "__init__.pyi",
    ROOT / "polynomial" / "__init__.pyi",
    ROOT / "random" / "__init__.pyi",
    ROOT / "testing" / "__init__.pyi",
]
```

`Path(np.__file__).parents[0]` 指向**已安装**的 numpy 包目录（不是源码树）——这很关键：测试检查的是「用户机器上实际安装的包」，而不是开发仓库。这样能抓住「源码里有桩、但打包配置漏装」的问题。

清单里每一条都是一个 PEP 561 关键文件：

- `py.typed`：PEP 561 的总标记（u1-l3 讲过）。
- 各子包的 `__init__.pyi`：包级桩文件（u5-l1 讲过 `__init__.pyi` 给检查器一个干净入口）。

然后是检查本身：

[numpy/typing/tests/test_isfile.py:L27-L35](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L27-L35) —— `TestIsFile.test_isfile`，逐个断言文件存在；带 `thread_unsafe` 标记：

```python
@pytest.mark.thread_unsafe(
    reason="os.path has a thread-safety bug (python/cpython#140054). "
           "Expected to only be a problem in 3.14.0"
)
class TestIsFile:
    def test_isfile(self):
        """Test if all ``.pyi`` files are properly installed."""
        for file in FILES:
            assert_(os.path.isfile(file))
```

两点说明：

1. 用 `numpy.testing.assert_`（[第 7 行](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_isfile.py#L7) 导入）而非裸 `assert`——这是 NumPy 测试工具的一个薄封装，行为等价但能在断言失败时给出更一致的报告。
2. `@pytest.mark.thread_unsafe` 是 NumPy 自定义的 pytest 标记，声明此测试在「线程并行」（如 `pytest-xdist` 的线程模式）下不安全，原因是 CPython 3.14.0 里 `os.path` 有一个线程安全 bug（`python/cpython#140054`）。这是「测试本身受运行时环境限制」的真实例子——和被测的「类型」无关，但提醒我们：运行时测试要考虑运行时环境的怪癖。

#### 4.4.4 代码实践

**实践目标**：在自己安装的 numpy 里定位 `py.typed` 与若干 `__init__.pyi`，复刻 `test_isfile` 的核心断言。

**操作步骤**：

1. 写 `check_stubs.py`：

```python
# 示例代码：复刻 test_isfile 的核心断言
from pathlib import Path
import numpy as np

root = Path(np.__file__).parents[0]
print("numpy 安装目录:", root)

targets = [
    root / "py.typed",
    root / "__init__.pyi",
    root / "typing" / "__init__.pyi",   # 本讲所在子包的桩
    root / "linalg" / "__init__.pyi",
]
for f in targets:
    print(f"{'OK ' if f.is_file() else 'MISSING '}  {f}")
```

2. 运行：`python check_stubs.py`。

**预期结果**：所有文件都应标记为 `OK`。`py.typed` 是空文件（可以用 `ls -la` 看到它存在但字节数为 0）；`__init__.pyi` 含类型声明。

**需要观察的现象**：

- 如果某项显示 `MISSING`，说明你装的 numpy 版本/打包有问题，类型检查器会静默退化——这正是 `test_isfile` 要拦截的场景。
- 打开 `root / "typing" / "__init__.pyi"` 看，它应只含 `import` 与 `__all__`，不含 `__getattr__` 等运行时逻辑（u5-l1 讲过的双轨制样本）。

> 「待本地验证」：不同 numpy 版本的子包清单可能略有出入；本讲引用的 `FILES` 清单以当前 HEAD 为准。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ROOT = Path(np.__file__).parents[0]` 指向的是「已安装的包」而不是「源码仓库」？

> **答案**：`np.__file__` 是「被 import 的那个 numpy」的 `__init__.py` 路径，取决于 `sys.path` 解析。在已安装环境里它指向 site-packages，在开发仓库里才指向源码树。测试用这个动态路径，就能检查「当前实际在用的那个 numpy」是否带齐了桩文件。

**练习 2**：如果某次发版 wheel 漏装了 `linalg/__init__.pyi`，运行 `np.linalg` 还正常吗？`test_isfile` 会怎样？

> **答案**：运行完全正常——`.pyi` 对运行时无作用，`np.linalg` 照常可用。但 `test_isfile` 会失败，指出该文件缺失。这正是该测试的价值：把「类型静默退化」变成可见的失败。

---

## 5. 综合实践

设计一个把本讲四块内容串起来的小任务：**给你的某个自定义类型别名补一个「运行时 + 打包」双保险测试**。

任务步骤：

1. 在一个示例包 `mypkg/` 里，用 PEP 695 定义一个类型别名并暴露：

```python
# mypkg/__init__.py
type MyArrayLike = list[float] | tuple[float, ...]
__all__ = ["MyArrayLike"]
```

2. 给它配一个 `py.typed` 空文件，并建一个最小的 `mypkg/__init__.pyi`（与 `.py` 的 `__all__` 一致）。

3. 仿照本讲的 `test_runtime.py`，写 `tests/test_runtime.py`，包含：
   - 一个 `TYPES = {"MyArrayLike": TypeTup.from_type_alias(MyArrayLike)}` 字典；
   - 用 `@pytest.mark.parametrize` 对它跑 `get_args` / `get_origin` / `get_type_hints`；
   - 一个 `test_keys` 断言 `TYPES.keys() == set(__all__)`。

4. 仿照 `test_isfile.py`，写 `tests/test_isfile.py`，断言 `py.typed` 与 `__init__.pyi` 存在。

5. 故意删掉 `py.typed`、或给 `__all__` 加一个不在 `TYPES` 里的名字，分别观察两个测试如何失败；然后还原。

**验收标准**：能清楚说出「运行时类型测试守护的是类型对象的可内省与公共面同步」「打包测试守护的是类型文件真的送到用户机器」这两件事的区别。

---

## 6. 本讲小结

- 运行时类型测试（`test_runtime.py`）与静态类型测试（u6-l1 的 mypy）是两套互补的检查：前者关心类型对象在真实 Python 里能不能用，后者关心注解是否合法。
- PEP 695 `type` 别名是 `TypeAliasType` 盒子，必须 `alias.__value__` 剥壳后才能用 `get_args` / `get_origin` 拆解；`NBitBase` 是普通类没有盒子，故在 `TYPES` 里被单独手工构造。
- `test_get_type_hints`（直接注解）与 `test_get_type_hints_str`（字符串注解）验证类型表达式能完整往返；注意两者解析回来的对象不同（剥壳值 vs 盒子本身）。
- `test_keys` 用 `TYPES.keys() == set(npt.__all__)` 把测试清单与公共 API 契约绑死，防止某一边被遗忘。
- `TestRuntimeProtocol` 验证 `@runtime_checkable` 协议（`_SupportsArray` / `_SupportsArrayFunc` / `_NestedSequence`）的运行时 `isinstance` / `issubclass`，并强调运行时检查是浅层的（只看名字）。
- `test_isfile.py` 是打包完整性兜底：检查 `py.typed` 与各 `__init__.pyi` 确实随包安装，把「类型静默退化」变成可见失败。

---

## 7. 下一步学习建议

- 下一讲 u6-l3（综合实战）会把 `ArrayLike` / `DTypeLike` / `NDArray` / `@overload` 串起来，要求你为自己的函数写完整注解并补一个 reveal 夹具——本讲学到的 `get_type_hints` 往返与 `assert_type` 思路会直接派上用场。
- 建议回看 u6-l1 的 mypy 测试方法论，对比「mypy 静态断言」与「本讲的运行时 `get_args` 断言」如何分工守护同一批类型别名。
- 想深入 PEP 695 运行时对象，可阅读 CPython 文档里 [`types.TypeAliasType`](https://docs.python.org/3/library/types.html#types.TypeAliasType) 与 [`typing.get_args`](https://docs.python.org/3/library/typing.html#typing.get_args) 的官方说明，并自己给带类型参数的别名（如 `NDArray`）做剥壳实验。
