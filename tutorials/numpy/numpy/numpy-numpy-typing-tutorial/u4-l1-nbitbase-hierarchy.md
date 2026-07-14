# NBitBase 精度层次（及其 2.3 弃用）

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `NBitBase` 解决的是什么问题——把「数值精度」抽象成一个**类型参数**，让类型检查器能推理精度间的提升（promotion）关系。
- 读懂 `_nbit_base.py` 中那条 `_128Bit → _96Bit → … → _8Bit` 的继承链，并解释为什么「越下层越低精度」。
- 解释三道保护机制：`@final`（禁止随意派生）、`@set_module`（改写 `__module__`）、`__init_subclass__`（按名字白名单校验合法子类）。
- 对比 `_nbit_base.py`（运行时）与 `_nbit_base.pyi`（类型检查桩）的差异，说清桩文件为何额外多出 `_256Bit` 与 `_80Bit`。
- 理解 NumPy 2.3 对 `NBitBase` 的弃用（`@deprecated` + 运行时 `DeprecationWarning`），以及官方推荐的现代替代写法。

## 2. 前置知识

本讲假定你已经读过 [u1-l2 公共 API 与目录结构](u1-l2-public-api-and-layout.md)，知道 `numpy.typing` 是「公共壳」、`numpy._typing` 是「私有实现」，并且理解 `.py` 与 `.pyi` 双轨制（类型检查器优先读 `.pyi`，运行时跑 `.py`）。下面补充两个本讲会用到的概念。

### 2.1 把「属性」当类型参数：泛型类的类型变量

Python 的泛型类（PEP 484）可以带类型参数。NumPy 把 `np.floating`、`np.integer` 等标量基类也建模成泛型类：

```python
np.floating[_64Bit]   # 一个「64 位浮点」的静态类型
np.integer[_32Bit]    # 一个「32 位整数」的静态类型
```

这里的 `_64Bit`、`_32Bit` 就是本讲的主角——它们不是普通类型，而是**精度的「刻度」**，用来在类型层面区分 `float64` 和 `float32`。

### 2.2 不变（invariant）泛型参数

类型参数有三种变型（variance）：协变、逆变、不变。NumPy 官方文档明确指出，精度被当作**不变（invariant）**泛型参数处理（见 `numpy/typing/__init__.py` 的 *Number precision* 一节）。通俗讲：即便 `float64` 运行时是 `floating` 的子类，在静态类型里 `np.floating[_64Bit]` 与 `np.floating[_32Bit]` 也**不能互相替换**——精度是一个「钉死」的标签。这正是 `NBitBase` 体系的设计基石（协变/逆变的深入对比见 [u3-l2](u3-l2-nested-sequence-protocol.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_typing/_nbit_base.py` | 运行时实现：定义 `NBitBase` 与 `_128Bit.._8Bit` 精度子类，含 `@final`、`@set_module`、`__init_subclass__` 三道保护。 |
| `numpy/_typing/_nbit_base.pyi` | 类型检查桩：同样的层次，但额外声明 `_256Bit`、`_80Bit`，并用 `@deprecated` 标记 `NBitBase`。 |
| `numpy/_typing/_nbit.py` | 平台精度别名（如 `_NBitIntP = _32Bit | _64Bit`），消费上面的精度子类（深入见 [u4-l2](u4-l2-nbit-platform-and-scalars-co.md)）。 |
| `numpy/_typing/__init__.py` | 私有聚合层：从 `_nbit_base` 再导出 `NBitBase` 与六个精度子类。 |
| `numpy/typing/__init__.py` | 公共壳：通过模块级 `__getattr__` 为 `NBitBase` 发出运行时 `DeprecationWarning`。 |
| `numpy/typing/tests/data/reveal/nbit_base_example.pyi` | 官方 reveal 夹具，展示 `NBitBase` 的典型用法与期望推断结果。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：`NBitBase` 的精度抽象 → `_128Bit.._8Bit` 精度子类链 → 三道保护机制（`@final`/`@set_module`/`__init_subclass__`）→ `.py` 与 `.pyi` 的差异及 2.3 弃用（`@deprecated`）。

### 4.1 NBitBase：把「数值精度」抽象为不变类型参数

#### 4.1.1 概念说明

科学计算里有一类很常见的函数签名需求：

> 输入两个不同精度的数（比如 `float16` 与 `int64`），返回一个精度「取两者最大」的结果（`float64`）。

运行时，NumPy 用一套 C 级的类型提升规则（promotion rules）自动决定结果精度。但在**静态类型检查**里，类型检查器不会去执行代码，它只能靠注解推理。如果 `np.floating` 没有精度信息，类型检查器就无法区分 `float32` 和 `float64`，也就无法表达「结果精度是输入精度的某种组合」。

`NBitBase` 就是为填补这个缺口而引入的：它定义了一组**有序的精度刻度**，让精度本身成为一个可以被 `TypeVar` 约束、可以做并集（`|`）运算的类型参数。于是「精度提升」就能在类型层面被表达。

注意它「**仅用于静态类型检查**」——`NBitBase` 在运行时不参与任何数值计算，它只是一个空壳标记类。源码文档字符串第一句话就说得很明白：

> A type representing `numpy.number` precision during static type checking.

#### 4.1.2 核心流程

`NBitBase` 的典型用法（取自其文档字符串）是这样的：

```python
from typing import TYPE_CHECKING
import numpy as np
import numpy.typing as npt

def add[S: npt.NBitBase, T: npt.NBitBase](
    a: np.floating[S], b: np.integer[T]
) -> np.floating[S | T]:
    return a + b
```

逐步拆解：

1. `S: npt.NBitBase`、`T: npt.NBitBase`：声明两个 `TypeVar`，上界是 `NBitBase`——即 `S`/`T` 只能取某个精度刻度。
2. `np.floating[S]`、`np.integer[T]`：把精度刻度作为标量泛型类的类型参数，于是输入的精度信息被「捕获」进 `S`/`T`。
3. 返回类型 `np.floating[S | T]`：用类型层面的**并集**表达「取两个输入精度的合并」。

精度刻度之间是有偏序关系的：`_64Bit > _32Bit > _16Bit`（注意方向：**越上层越宽/越高精度**）。并集 `S | T` 在概念上对应「取较宽者」。用偏序记号：

\[
\text{result\_bits} = \max(S, T)
\]

而由于精度是不变参数，`np.floating[S]` 与 `np.floating[T]`（`S \neq T`）不可互换，类型检查器据此能精确区分不同精度的浮点。

#### 4.1.3 源码精读

`NBitBase` 的运行时定义在 [_nbit_base.py:L1-L9]：

[numpy/_typing/_nbit_base.py:L1-L9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L1-L9)

```python
"""A module with the precisions of generic `~numpy.number` types."""
from typing import final

from numpy._utils import set_module


@final  # Disallow the creation of arbitrary `NBitBase` subclasses
@set_module("numpy.typing")
class NBitBase:
```

这段做了三件事（细节在 4.3 节展开）：导入 `final` 与 `set_module`；用 `@final` 禁止任意派生；用 `@set_module("numpy.typing")` 把 `__module__` 改写成公共包名。

类本身的文档字符串非常详尽，直接给出了上面的 `add` 示例和期望的 `reveal_locals()` 输出，见 [_nbit_base.py:L10-L53]：

[numpy/_typing/_nbit_base.py:L10-L53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L10-L53)

文档里还带了一条版本与弃用标注：

- `.. versionadded:: 1.20` —— `NBitBase` 自 1.20 引入。
- `.. deprecated:: 2.3` —— 自 2.3 起弃用，改用 `@typing.overload` 或以标量类为上界的 `TypeVar`（详见 4.4 节）。

官方在公共壳文档里也用一段话点明了「精度即不变泛型参数」的设计，见 [numpy/typing/__init__.py:L69-L88]：

[numpy/typing/__init__.py:L69-L88](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L69-L88)

> The precision of `numpy.number` subclasses is treated as an invariant generic parameter (see `NBitBase`), simplifying the annotating of processes involving precision-based casting.

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍文档里的 `add` 示例，观察精度刻度如何被推断。

**操作步骤**：

1. 新建 `probe_nbit.py`，把 4.1.2 节的 `add` 函数与文档示例抄进去。
2. 用 mypy 跑静态检查并打印推断类型（mypy 需装 numpy 与 `--enable-incomplete-features` 或较新版本以支持泛型标量，具体以本地 mypy 版本为准）：

   ```bash
   python -m mypy --reveal-types probe_nbit.py
   ```

3. 也可直接对照仓库自带的 reveal 夹具，它把期望结果用 `assert_type` 写死，见 [nbit_base_example.pyi:L1-L17]：

   [numpy/typing/tests/data/reveal/nbit_base_example.pyi:L1-L17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L1-L17)

   ```python
   assert_type(add(f8, i8), np.floating[_64Bit])
   assert_type(add(f4, i8), np.floating[_32Bit | _64Bit])
   ```

**需要观察的现象**：`add(f4, i8)`（`float32 + int64`）的推断结果是 `np.floating[_32Bit | _64Bit]`，即并集；而 `add(f8, i8)`（`float64 + int64`）则是 `np.floating[_64Bit]`。

**预期结果**：类型检查器报告的精度刻度与 `assert_type` 完全一致。

**注意**：如果你本地 mypy 版本较旧或配置不同，可能无法完整支持泛型标量推断——这种情况下记为「待本地验证」，重点理解「精度以并集形式出现在返回类型里」这一思想即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NBitBase` 在运行时不参与数值计算，却仍然有用？

> 参考答案：它的价值在**静态类型检查阶段**。类型检查器不执行代码，需要注解来推理精度关系；`NBitBase` 提供了精度刻度，使 `TypeVar` 能约束并组合精度。运行时它只是一个空标记类，真正的提升由 NumPy 的 C 级规则完成。

**练习 2**：`np.floating[_32Bit]` 和 `np.floating[_64Bit]` 在静态类型里能互相替换吗？为什么？

> 参考答案：不能。精度是不变（invariant）泛型参数，`_32Bit ≠ _64Bit`，两者不可互换。这与运行时「`float64` 是 `floating` 子类」的认知不同，是「类型比运行时更严格」的又一体现。

---

### 4.2 精度子类链 _128Bit → _8Bit：一条被 @final 锁死的继承链

#### 4.2.1 概念说明

`NBitBase` 只是「精度刻度的根」。真正的刻度是一组**有序子类**：`_128Bit`、`_96Bit`、`_64Bit`、`_32Bit`、`_16Bit`、`_8Bit`。它们按「宽度」排成一条单链：

\[
_128Bit \succ _96Bit \succ _64Bit \succ _32Bit \succ _16Bit \succ _8Bit
\]

方向约定是：**越靠近根（`NBitBase`）越高精度**。所以 `_128Bit` 最宽、`_8Bit` 最窄。继承关系在这里被「借用」来表达偏序——子类表示「更窄的精度」。这是一种对继承语义的**非常规复用**：通常子类表示「更具体的能力」，而这里子类表示「更少的位数」。

每个子类本身都被 `@final` 装饰，意味着它们不能再被继承——精度刻度是一个**封闭集合**，用户不能私自添加 `_7Bit` 之类。

#### 4.2.2 核心流程

精度子类的构造模式高度统一：

```
@final
@set_module("numpy._typing")
class _XBit(父刻度):  # type: ignore[misc]   # pyright: ignore[reportGeneralTypeIssues]
    pass
```

要点：

1. 每个 `_XBit` 都是 `pass` 的空类——运行时没有任何行为，纯标记。
2. 每个都 `@set_module("numpy._typing")`，让 `__module__` 报告为私有包（见 4.3 节）。
3. `# type: ignore[misc]` 与 `# pyright: ignore[reportGeneralTypeIssues]`：因为父类 `NBitBase`（及链上父刻度）被 `@final` 标记，类型检查器本应禁止派生；这里用忽略注释告诉检查器「这是故意的」。

链的根是 `_128Bit(NBitBase)`，之后逐层窄化：

| 子类 | 直接父类 | 位数 |
| --- | --- | --- |
| `_128Bit` | `NBitBase` | 128 |
| `_96Bit` | `_128Bit` | 96 |
| `_64Bit` | `_96Bit` | 64 |
| `_32Bit` | `_64Bit` | 32 |
| `_16Bit` | `_32Bit` | 16 |
| `_8Bit` | `_16Bit` | 8 |

#### 4.2.3 源码精读

整条链定义在 [_nbit_base.py:L64-L93]：

[numpy/_typing/_nbit_base.py:L64-L93](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L64-L93)

```python
@final
@set_module("numpy._typing")
class _128Bit(NBitBase):  # type: ignore[misc]  # pyright: ignore[reportGeneralTypeIssues]
    pass

@final
@set_module("numpy._typing")
class _96Bit(_128Bit):  # type: ignore[misc]  # pyright: ignore[reportGeneralTypeIssues]
    pass
# … _64Bit(_96Bit)、_32Bit(_64Bit)、_16Bit(_32Bit)、_8Bit(_16Bit) 同构 …
```

这些精度刻度随后被 `_nbit.py` 用作平台别名的基础积木，例如 [_nbit.py:L5-L16]：

[numpy/_typing/_nbit.py:L5-L16](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit.py#L5-L16)

```python
type _NBitByte = _8Bit
type _NBitShort = _16Bit
type _NBitIntC = _32Bit
type _NBitIntP = _32Bit | _64Bit          # 平台相关：32 或 64 位指针宽度
type _NBitLong = _32Bit | _64Bit
type _NBitLongDouble = _64Bit | _96Bit | _128Bit
```

可见「平台相关精度」正是用**精度刻度的并集**表达的（如 `_NBitIntP` 在不同机器上可能是 32 或 64 位）。这块深入讲解留给 [u4-l2](u4-l2-nbit-platform-and-scalars-co.md)。

#### 4.2.4 代码实践

**实践目标**：直观感受精度刻度是一条 Python 继承链。

**操作步骤**：

```python
# 示例代码
from numpy._typing import _8Bit, _16Bit, _32Bit, _64Bit, _96Bit, _128Bit, NBitBase

# 打印偏序关系：子类 issubclass 父刻度
print(issubclass(_8Bit, _16Bit))    # True：_8Bit 是更窄的精度
print(issubclass(_64Bit, _128Bit))  # True
print(issubclass(_128Bit, NBitBase))# True：根
# 反方向不成立
print(issubclass(_64Bit, _32Bit))   # False：_64Bit 比 _32Bit 更宽
```

**需要观察的现象**：`issubclass(_8Bit, _16Bit)` 为 `True`，但 `issubclass(_64Bit, _32Bit)` 为 `False`——精度刻度按「窄→宽」方向 issubclass 成立。

**预期结果**：上面四行依次输出 `True / True / True / False`。

> 注意：访问 `numpy._typing` 的私有名仅供学习观察；正式代码应只用公共 `npt.NBitBase`（且它已被弃用，见 4.4 节）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_8Bit` 是 `_16Bit` 的子类，而不是反过来？

> 参考答案：本体系刻意让「子类 = 更窄精度」，从而用继承方向表达偏序 `_128Bit \succ … \succ _8Bit`。`_8Bit` 是更窄的刻度，因此排在链的下层，是 `_16Bit` 的子类。

**练习 2**：运行时这些 `_XBit` 类有方法或属性吗？这说明了什么？

> 参考答案：没有，全部是 `pass` 的空类。这说明它们是**纯静态标记**，仅服务于类型检查，运行时不承载任何行为。

---

### 4.3 三道保护机制：@final、@set_module、__init_subclass__

`NBitBase` 体系是封闭的——它必须保证「精度刻度的集合是固定的」。源码用三道机制把这条封闭性钉死。

#### 4.3.1 概念说明

**`@final`（来自 `typing`）**：标记一个类「不可被继承」。类型检查器看到 `@final` 的类被派生时会报错。但 `@final` 只是一个**给类型检查器看的声明**，运行时 Python 并不强制禁止继承——所以还需要下面两道运行时/语义层面的补充保护。

**`@set_module("...")`（来自 `numpy._utils`）**：NumPy 自带的装饰器，作用是改写对象的 `__module__` 属性。它把在私有文件 `_nbit_base.py` 里定义的类「化妆」成属于 `numpy.typing`（对 `NBitBase`）或 `numpy._typing`（对 `_XBit`）。这样 `repr`、文档、错误信息里报告的模块名就和公共 API 对齐，而不暴露内部文件布局。

**`__init_subclass__`（Python 内置钩子）**：每当有类继承本类时，Python 会自动调用父类的 `__init_subclass__(cls)`。NumPy 在 `NBitBase` 里重写它，**按名字白名单校验**子类名：只有那几个预定义的名字（`_128Bit`、`_96Bit` 等）才允许，否则抛 `TypeError`。这是运行时的硬保护——即便 `@final` 在运行时不生效，用户也无法凭空造出 `_7Bit`。

#### 4.3.2 核心流程

`__init_subclass__` 的判定流程：

```
有类继承 NBitBase（或其子刻度）
        │
        ▼
Python 自动调用 NBitBase.__init_subclass__(cls)
        │
        ▼
检查 cls.__name__ 是否在 allowed_names 白名单内？
        ├── 是 ──► 调用 super().__init_subclass__()，正常完成
        └── 否 ──► raise TypeError('cannot inherit from final class "NBitBase"')
```

白名单内容（取自源码）正是 `NBitBase` 自身加六个合法刻度：

```python
allowed_names = {
    "NBitBase", "_128Bit", "_96Bit", "_64Bit", "_32Bit", "_16Bit", "_8Bit"
}
```

注意：**白名单不含 `_256Bit` 与 `_80Bit`**——这两个名字只存在于 `.pyi` 桩中（见 4.4 节），运行时根本无法创建。

#### 4.3.3 源码精读

`__init_subclass__` 的实现在 [_nbit_base.py:L56-L62]：

[numpy/_typing/_nbit_base.py:L56-L62](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L56-L62)

```python
def __init_subclass__(cls) -> None:
    allowed_names = {
        "NBitBase", "_128Bit", "_96Bit", "_64Bit", "_32Bit", "_16Bit", "_8Bit"
    }
    if cls.__name__ not in allowed_names:
        raise TypeError('cannot inherit from final class "NBitBase"')
    super().__init_subclass__()
```

`@set_module` 装饰器本身的定义在 [numpy/_utils/__init__.py:L17-L37]：

[numpy/_utils/__init__.py:L17-L37](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_utils/__init__.py#L17-L37)

```python
def set_module(module):
    """Private decorator for overriding __module__ on a function or class."""
    def decorator(func):
        if module is not None:
            ...
            func.__module__ = module   # 关键：直接改写 __module__
        return func
    return decorator
```

`NBitBase` 上同时挂了 `@final` 与 `@set_module("numpy.typing")`，见 [_nbit_base.py:L7-L9]：

[numpy/_typing/_nbit_base.py:L7-L9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L7-L9)

```python
@final  # Disallow the creation of arbitrary `NBitBase` subclasses
@set_module("numpy.typing")
class NBitBase:
```

三道机制的分工小结：

| 机制 | 作用层 | 作用 |
| --- | --- | --- |
| `@final` | 静态（类型检查器） | 声明不可继承；检查器对派生报错 |
| `@set_module` | 运行时（`__module__`） | 改写模块归属，对齐公共 API 命名 |
| `__init_subclass__` | 运行时（继承钩子） | 按名字白名单拒绝非法子类 |

#### 4.3.4 代码实践（本讲核心实践任务之一）

**实践目标**：亲手触发 `__init_subclass__` 的 `TypeError`，验证白名单保护。

**操作步骤**：

```python
# 示例代码
import warnings
warnings.simplefilter("ignore", DeprecationWarning)  # 先压住 NBitBase 的弃用警告，专注看 TypeError
import numpy.typing as npt

# 1) 合法名字：与白名单中某个名字相同，能创建（仅演示机制；正式代码勿模仿）
class _16Bit(npt.NBitBase):   # 名字恰好在白名单里
    pass
print("合法名字 _16Bit 创建成功")

# 2) 非法名字：不在白名单
try:
    class _7Bit(npt.NBitBase):
        pass
except TypeError as e:
    print("捕获 TypeError:", e)
```

**需要观察的现象**：

- 第 1 步能成功创建（因为 `_16Bit` 在白名单里）——这揭示了 `__init_subclass__` **只看名字、不看身份**的「松散」之处：它防的是「陌生名字」，而非「真正的官方刻度」。这也是为什么还需要 `@final` 在静态层把关。
- 第 2 步抛出 `TypeError: cannot inherit from final class "NBitBase"`。

**预期结果**：依次打印 `合法名字 _16Bit 创建成功` 与 `捕获 TypeError: cannot inherit from final class "NBitBase"`。

> 说明：之所以先 `simplefilter("ignore", DeprecationWarning)`，是因为 2.3 起 `npt.NBitBase` 在公共壳里被延迟弃用（见 4.4 节），访问会触发 `DeprecationWarning`。这里我们暂时压住它，把注意力放在 `TypeError` 上。

#### 4.3.5 小练习与答案

**练习 1**：`@final` 已经声明「不可继承」，为什么 NumPy 还要额外写 `__init_subclass__`？

> 参考答案：`@final` 只是给类型检查器看的声明，**运行时 Python 不强制禁止继承**。`__init_subclass__` 提供运行时硬保护，确保即便有人绕过类型检查、或用不支持 `@final` 的工具，也无法创建非法精度刻度。

**练习 2**：`__init_subclass__` 是按名字校验的，这种做法有什么隐患？

> 参考答案：它只比对 `cls.__name__` 字符串，不验证身份，所以用户只要把子类命名为 `_16Bit` 等白名单名字就能蒙混过关。正因如此，封闭性需要 `@final`（静态）与 `__init_subclass__`（运行时）**双重**把关，而非单靠其一。

---

### 4.4 .py 与 .pyi 双轨：桩文件为何多出 _256Bit / _80Bit，以及 2.3 弃用

#### 4.4.1 概念说明

回顾 [u1-l3](u1-l3-pep561-py-typed-stubs.md) 与 [u5-l1](u5-l1-py-pyi-dual-track.md) 讲过的双轨制：当同一模块同时存在 `_nbit_base.py`（运行时）与 `_nbit_base.pyi`（类型检查桩）时，**类型检查器优先读 `.pyi` 并忽略 `.py` 的注解**。于是同一个「精度模块」可以呈现两张不同的面孔：

- **运行时面孔（`.py`）**：只有 `NBitBase` 与 `_128Bit.._8Bit` 共 7 个名字；`__init_subclass__` 也只允许这 7 个。
- **类型检查面孔（`.pyi`）**：额外多出 `_256Bit`（位于 `_128Bit` 之上）与 `_80Bit`（位于 `_96Bit` 与 `_64Bit` 之间），共 9 个名字。

这种「桩比实现多」的刻意分歧，正是为了在**静态层面**给类型检查器提供更细的精度刻度——例如 80 位的 x87 扩展精度、256 位的超长 double——即便 NumPy 运行时的 `_nbit.py` 平台别名当前并未引用它们。换句话说：桩文件把「类型系统希望拥有的精度词汇」提前声明出来，而运行时维持一个更保守、更小的集合。这体现了「静态类型可以比运行时更丰富/更严格」的双轨哲学（`type_check_only` 精神的体现，详见 [u5-l1](u5-l1-py-pyi-dual-track.md)、[u5-l2](u5-l2-ufunc-type-modeling.md)）。

本模块还要讲清 **2.3 弃用**：`NBitBase` 自 NumPy 2.3（2025-05-01）起弃用，弃用在静态侧与运行侧各有一套表达。

#### 4.4.2 核心流程

**静态侧（桩 `.pyi`）如何表达弃用**：用 `typing_extensions.deprecated` 装饰器给 `NBitBase` 打标记，类型检查器（pyright/mypy）看到它被使用时就会报「已弃用」提示。注意桩里**没有** `__init_subclass__`——桩只描述类型形状，不描述运行时行为。

**运行侧（公共壳 `numpy/typing/__init__.py`）如何表达弃用**：用 PEP 562 的模块级 `__getattr__`，在用户首次访问 `npt.NBitBase` 时**懒加载**并发出 `DeprecationWarning`（深入见 [u5-l4](u5-l4-module-getattr-lazy-deprecation.md)）。这样未访问就不报警，把弃用噪音降到最低。

两套精度层次的对比：

| 名字 | `.py`（运行时） | `.pyi`（桩） | 链中位置（桩） |
| --- | :-: | :-: | --- |
| `NBitBase` | ✓ | ✓（`@deprecated`） | 根 |
| `_256Bit` | ✗ | ✓ | `NBitBase` 之上 |
| `_128Bit` | ✓ | ✓ | `_256Bit` 之下 |
| `_96Bit` | ✓ | ✓ | `_128Bit` 之下 |
| `_80Bit` | ✗ | ✓ | `_96Bit` 与 `_64Bit` 之间 |
| `_64Bit` | ✓ | ✓ | `_80Bit` 之下 |
| `_32Bit` | ✓ | ✓ | `_64Bit` 之下 |
| `_16Bit` | ✓ | ✓ | `_32Bit` 之下 |
| `_8Bit` | ✓ | ✓ | `_16Bit` 之下 |

#### 4.4.3 源码精读

桩文件 `_nbit_base.pyi` 顶部先关闭一批与弃用、`@final` 派生相关的检查噪音，再用 `@deprecated` 标记 `NBitBase`，见 [_nbit_base.pyi:L1-L15]：

[numpy/_typing/_nbit_base.pyi:L1-L15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.pyi#L1-L15)

```python
# pyright: reportDeprecated=false
# pyright: reportGeneralTypeIssues=false
# mypy: disable-error-code=misc

from typing import final
from typing_extensions import deprecated

# Deprecated in NumPy 2.3, 2025-05-01
@deprecated(
    "`NBitBase` is deprecated and will be removed from numpy.typing in the "
    "future. Use `@typing.overload` or a type parameter with a scalar-type as upper "
    "bound, instead. (deprecated in NumPy 2.3)",
)
@final
class NBitBase: ...
```

随后是**比运行时多出 `_256Bit` 与 `_80Bit`** 的完整链，见 [_nbit_base.pyi:L17-L39]：

[numpy/_typing/_nbit_base.pyi:L17-L39](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.pyi#L17-L39)

```python
@final
class _256Bit(NBitBase): ...  # type: ignore[deprecated]   # 仅桩存在

@final
class _128Bit(_256Bit): ...

@final
class _96Bit(_128Bit): ...

@final
class _80Bit(_96Bit): ...                                    # 仅桩存在

@final
class _64Bit(_80Bit): ...
# … _32Bit、_16Bit、_8Bit 同构 …
```

两个要点：

1. 桩里 `_256Bit` 直接继承 `NBitBase`，因此需要 `# type: ignore[deprecated]` 来压住「继承了一个 `@deprecated` 类」的告警；而 `_128Bit(_256Bit)` 等继承自 `_256Bit`（非 `@deprecated`），所以无需该忽略注释。
2. `_256Bit`、`_80Bit` **运行时不存在**：`_nbit_base.py` 没定义它们，`numpy/_typing/__init__.py` 也只再导出了 `_8Bit.._128Bit`，见 [_typing/__init__.py:L110-L118]：

   [numpy/_typing/__init__.py:L110-L118](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py#L110-L118)

   ```python
   from ._nbit_base import (  # type: ignore[deprecated]
       NBitBase as NBitBase,  # pyright: ignore[reportDeprecated]
       _8Bit as _8Bit,
       _16Bit as _16Bit,
       _32Bit as _32Bit,
       _64Bit as _64Bit,
       _96Bit as _96Bit,
       _128Bit as _128Bit,
   )
   ```

   所以 `from numpy._typing import _80Bit` 在运行时会抛 `ImportError`——它是一个**纯类型检查实体**。

运行侧的弃用则在公共壳里，用模块级 `__getattr__` 懒发警告，见 [numpy/typing/__init__.py:L187-L199]：

[numpy/typing/__init__.py:L187-L199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L187-L199)

```python
def __getattr__(name: str) -> object:
    if name == "NBitBase":
        import warnings
        # Deprecated in NumPy 2.3, 2025-05-01
        warnings.warn(
            "`NBitBase` is deprecated and will be removed from numpy.typing in the "
            "future. Use `@typing.overload` or a `TypeVar` with a scalar-type as upper "
            "bound, instead. (deprecated in NumPy 2.3)",
            DeprecationWarning,
            stacklevel=2,
        )
        return NBitBase
```

官方推荐的现代替代写法（以标量类为上界的 `TypeVar`，或 `@overload`）写在公共壳文档里，见 [numpy/typing/__init__.py:L90-L120]：

[numpy/typing/__init__.py:L90-L120](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L90-L120)

```python
from typing import TypeVar
import numpy as np

S = TypeVar("S", bound=np.floating)   # 现代写法 1：以标量类为上界

def func(a: S, b: S) -> S:
    ...
```

或用 `@overload` 表达「不同输入精度 → 不同输出精度」：

```python
from typing import overload
import numpy as np

@overload
def phase(x: np.complex64) -> np.float32: ...
@overload
def phase(x: np.complex128) -> np.float64: ...
@overload
def phase(x: np.clongdouble) -> np.longdouble: ...
def phase(x: np.complexfloating) -> np.floating:
    ...
```

这两种现代写法不再需要 `NBitBase`，正是它被弃用的根本原因（`@overload`/`TypeVar` 的深入见 [u4-l3](u4-l3-modern-typevar-overload.md)）。

#### 4.4.4 代码实践（本讲核心实践任务之二）

**实践目标**：对比 `.py` 与 `.pyi` 的精度集合，验证 `_256Bit`/`_80Bit` 运行时不存在，并捕获 `NBitBase` 的运行时弃用警告。

**操作步骤**：

```python
# 示例代码
import warnings, numpy.typing as npt

# A) 运行时弃用警告：访问 npt.NBitBase 会懒发 DeprecationWarning
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = npt.NBitBase
    print("捕获警告数:", len(w))
    print("类别:", w[0].category.__name__ if w else "无")
    print("消息片段:", str(w[0].message)[:40] if w else "无")

# B) 运行时能拿到的精度刻度（来自私有聚合）
import numpy._typing as _t
runtime_bits = [n for n in ("_8Bit","_16Bit","_32Bit","_64Bit","_80Bit","_96Bit","_128Bit","_256Bit")
                if hasattr(_t, n)]
print("运行时存在的刻度:", runtime_bits)

# C) _80Bit / _256Bit 运行时拿不到
try:
    from numpy._typing import _80Bit   # 预期失败
except ImportError as e:
    print("ImportError:", e)
```

**需要观察的现象**：

- A 段：捕获到 1 条 `DeprecationWarning`，消息以 `` `NBitBase` is deprecated `` 开头。
- B 段：运行时存在的刻度列表**不含** `_80Bit` 与 `_256Bit`。
- C 段：`from numpy._typing import _80Bit` 抛 `ImportError`。

**预期结果**：A 段 `捕获警告数: 1`、`类别: DeprecationWarning`；B 段列表为 `['_8Bit', '_16Bit', '_32Bit', '_64Bit', '_96Bit', '_128Bit']`；C 段抛 `ImportError`。

> 若本地 numpy 版本 < 2.3，则 A 段不会触发 `DeprecationWarning`——届时请记为「待本地验证（需 numpy ≥ 2.3）」，但 B/C 两段在更早版本同样成立（`_80Bit`/`_256Bit` 始终仅存于桩）。

**思考题（对应实践任务要求）**：既然桩里的 `_80Bit`/`_256Bit` 运行时不存在，为什么类型检查器却「认为」它们存在？这种桩与实现不一致的意义是什么？

> 参考答案：类型检查器只读 `.pyi`，而 `.pyi` 把它们声明出来了，所以检查器「看得到」。意义在于：让静态类型系统拥有更完整的精度词汇（如 80 位扩展精度），用于精确推理；同时运行时维持一个更小、更稳健的集合，避免维护无实际用途的运行时类。这正是「双轨制」的精髓——静态与运行时可以有意分歧，各取所需。

#### 4.4.5 小练习与答案

**练习 1**：`_256Bit` 和 `_80Bit` 分别插在精度链的什么位置？为什么只有桩里才有？

> 参考答案：`_256Bit` 在 `_128Bit` 之上（最宽），`_80Bit` 在 `_96Bit` 与 `_64Bit` 之间。它们只为类型检查提供更细的精度刻度，运行时既无定义也无再导出，所以只在 `.pyi` 中存在。

**练习 2**：`NBitBase` 的弃用在静态侧和运行侧分别如何体现？

> 参考答案：静态侧用 `typing_extensions.deprecated` 装饰 `NBitBase`（在 `.pyi`），让检查器报弃用提示；运行侧用公共壳 `numpy/typing/__init__.py` 的模块级 `__getattr__`，在首次访问 `npt.NBitBase` 时懒发 `DeprecationWarning`。两套机制互不替代，分别面向检查器与运行时。

**练习 3**：官方推荐用什么替代 `NBitBase`？

> 参考答案：用「以标量类（如 `np.floating`）为上界的 `TypeVar`」表达「同精度进、同精度出」；用 `@typing.overload` 表达「不同输入精度映射到不同输出精度」。两者都不再依赖 `NBitBase` 这套精度刻度。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小任务：**读懂并扩展一条精度安全的函数签名**。

1. **阅读**：打开 [_nbit_base.py:L10-L53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L10-L53) 的 `add` 文档示例，以及仓库 reveal 夹具 [nbit_base_example.pyi:L1-L17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L1-L17)。
2. **改写为现代写法**：用「以标量类为上界的 `TypeVar`」或 `@overload` 重写 `add`，去掉对 `npt.NBitBase` 的依赖（参考 [u4-l3](u4-l3-modern-typevar-overload.md)）。
3. **验证保护机制**：写一个非法名字的子类（如 `_7Bit`）触发 `__init_subclass__` 的 `TypeError`，再尝试 `from numpy._typing import _80Bit` 触发 `ImportError`，把两个异常都打印出来。
4. **对比双轨**：用文本编辑器并排打开 `_nbit_base.py` 与 `_nbit_base.pyi`，列出桩多出的两个名字及其在链中的位置，写一句话说明「类型检查器读到 9 个刻度、运行时只有 7 个」。

完成后，你应当能向别人解释清楚：`NBitBase` 是什么、它的精度链如何组织、靠哪三道机制保持封闭、为什么桩和实现会有意不一致、以及它为什么在 2.3 被弃用。

## 6. 本讲小结

- `NBitBase` 把「数值精度」抽象成一组**有序类型刻度**，让精度成为标量泛型类（`np.floating[T]` 等）的**不变类型参数**，从而能在静态层面推理精度提升。
- 精度刻度是一条单链 `_128Bit \succ _96Bit \succ … \succ _8Bit`，子类表示「更窄精度」——这是对继承语义的非常规复用。
- 三道保护机制共同维持精度集合的封闭：`@final`（静态声明不可继承）、`@set_module`（改写 `__module__` 对齐公共命名）、`__init_subclass__`（运行时按名字白名单拒绝非法子类）。
- `_nbit_base.pyi` 比运行时多出 `_256Bit` 与 `_80Bit` 两个**纯桩实体**，体现「静态类型可比运行时更丰富」的双轨哲学。
- 自 NumPy 2.3（2025-05-01）起 `NBitBase` 被弃用：静态侧用 `@deprecated`、运行侧用模块级 `__getattr__` 懒发 `DeprecationWarning`；官方推荐改用以标量类为上界的 `TypeVar` 或 `@overload`。

## 7. 下一步学习建议

- 接着读 [u4-l2 平台精度 _nbit 与标量协变别名 _scalars](u4-l2-nbit-platform-and-scalars-co.md)：看 `_nbit.py` 如何用本讲的 `_XBit` 刻度的并集表达平台相关精度（如 `_NBitIntP = _32Bit | _64Bit`），以及 `_scalars.py` 的 `_co` 协变别名。
- 再读 [u4-l3 现代精度表达：TypeVar 与 @overload](u4-l3-modern-typevar-overload.md)：系统学习如何用 `bound` TypeVar 与 `@overload` 彻底替代 `NBitBase`。
- 若想深挖双轨制与 `type_check_only`，跳到 [u5-l1 运行时实现与桩文件双轨制](u5-l1-py-pyi-dual-track.md)；想深挖模块级 `__getattr__` 的延迟弃用机制，见 [u5-l4 模块级 __getattr__ 与延迟弃用](u5-l4-module-getattr-lazy-deprecation.md)。
