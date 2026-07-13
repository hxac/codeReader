# dtype 与标量类型体系

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「NumPy 标量类型」「dtype 实例」「DType 类」这三者是什么、彼此什么关系，不再把它们混为一谈。
- 看懂 NumPy 的标量类型层次树：从 `generic` 一路分叉到 `int64`、`float32` 这样的叶子类型，并能用 `np.issubdtype` 判断归属。
- 读懂 `dtype` 对象的关键属性：`kind`、`char`、`num`、`itemsize`、`str`、`byteorder`，理解它们各自描述什么。
- 解释为什么 `np.float64`、`np.double`、`np.dtype("float64")`、`np.dtype("f8")` 会指向同一种数据类型——也就是 NumPy 庞大的「别名体系」是怎么搭起来的。
- 在源码中定位这三个关键文件：`numpy/_core/numerictypes.py`、`numpy/dtypes.py`、`numpy/_core/_type_aliases.py`。

## 2. 前置知识

本讲默认你已经学过 u2-l1「数组创建方式全览」，知道 `np.array`、`np.asarray` 怎么用，也知道每个 `ndarray` 都带一个 `.dtype` 属性。下面补充三个本讲会用到的概念。

### 2.1 标量（scalar）与数组（array）

一个 Python 整数 `5` 是「标量」，它是一个单独的值；`np.array([5, 6, 7])` 是「数组」，是一组值。NumPy 给数组里的「每一个元素」都准备了一个对应的 Python 类型，称为**标量类型（scalar type）**。例如 `int64` 数组的元素取出来是 `np.int64(5)` 这样一个对象，`np.int64` 就是标量类型。所有 NumPy 标量类型都继承自根类型 `numpy.generic`。

### 2.2 「类型」与「描述符」

注意区分两个层次：

- **标量类型**（如 `np.float64`）：一个 Python 类，描述「数组里一个元素是什么」。
- **dtype 实例**（如 `np.dtype("float64")`）：一个对象，描述「整块内存怎么解读」——包括字节宽度、字节序、是否对齐等。

二者一一对应：`np.dtype("float64").type is np.float64` 为 `True`。本讲要讲清楚它们的层次关系。

### 2.3 抽象类与具体类

Python 里有 `numbers.Number → numbers.Real → numbers.Integral` 这样的抽象基类（ABC）层级，`isinstance(5, numbers.Real)` 为 `True`。NumPy 也仿照这个思路，建了一套**抽象标量类型**（`number`、`integer`、`floating` 等），用来表达「一族类型」，方便做 `issubdtype` 这类判断。抽象类不能用来创建数组（`np.dtype(np.floating)` 没有意义），只用于归类。

## 3. 本讲源码地图

本讲围绕三个文件展开，它们正好对应「类型体系」的三层：

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| [numpy/_core/numerictypes.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py) | 定义标量类型对象、类型层次、`issubdtype`/`isdtype` 等查询函数 | 类型层次的「主表」与查询入口 |
| [numpy/dtypes.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.py) | 公开 DType 类的命名空间（`np.dtypes.Float64DType` 等） | 「dtype 的类型」这一层 |
| [numpy/_core/_type_aliases.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py) | 生成 `allTypes` / `sctypeDict` / `sctypes` 三张别名表 | 把「一个类型」对应到「一堆名字」 |

另外会引用两个辅助证据文件：类型存根 [numpy/dtypes.pyi](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.pyi)（列出全部公开 DType 类名）和 C 模块入口 [numpy/_core/src/multiarray/multiarraymodule.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c)（展示 DType 类如何被注册进 `numpy.dtypes`）。

---

## 4. 核心概念与源码讲解

### 4.1 标量类型层次（_core/numerictypes.py）

#### 4.1.1 概念说明

NumPy 的所有内置标量类型构成一棵**继承树**，根节点是 `generic`。这棵树同时回答两个问题：

1. **这个类型属于哪一族？**（归类）
2. **它能不能被当作另一种更宽泛的类型使用？**（子类型关系）

整棵树的形状被写死在 `numerictypes.py` 的模块文档字符串里，这是 NumPy 类型体系最权威的一份「地图」：

- `generic`（根）
  - `bool`（布尔，kind=`b`）
  - `number`（所有数值）
    - `integer`（整数）
      - `signedinteger`（有符号：`int8/16/32/64`、`byte/short/intc/intp/int_/longlong`，kind=`i`）
      - `unsignedinteger`（无符号：`uint8/16/32/64` 等，kind=`u`）
    - `inexact`（不精确表示）
      - `floating`（浮点：`float16/32/64`、`single/double/longdouble`，kind=`f`）
      - `complexfloating`（复数：`complex64/128`，kind=`c`）
  - `flexible`（长度可变）
    - `character`（`bytes_` kind=`S`、`str_` kind=`U`）
    - `void`（kind=`V`）
  - `object_`（Python 对象，kind=`O`）

要特别记住：**`bool` 并不是 `number` 的子类**，它直接挂在 `generic` 下面；而 `datetime64`/`timedelta64`（kind 分别为 `M`/`m`）也是数值之外的特殊类型。

#### 4.1.2 核心流程

类型层次的判断最终都落在 Python 内置的 `issubclass` 上。NumPy 做的事情是：

1. 在 C 层创建所有标量类型，并按上面的树设置好继承关系。
2. 把抽象类型（`generic`、`number`、`integer`、`floating` 等）和具体类型（`int64`、`float32` 等）都暴露到 `numerictypes` 模块。
3. 提供 `issubdtype(arg1, arg2)`：先把两个参数都归一化成「标量类型」，再调用 `issubclass(arg1, arg2)`。

也就是说，`np.issubdtype(np.int32, np.integer)` 本质等价于 `issubclass(np.int32, np.integer)`。这种「把类型层次映射成 Python 类继承」的设计，让类型判断可以用最朴素的 `issubclass` 完成，无需任何特殊机制。

#### 4.1.3 源码精读

**(1) 类型层次树——模块文档字符串**

[type tree: numerictypes.py:40-77](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L40-L77) 这段 ASCII 图就是 4.1.1 里那棵树的原文，是理解整个类型体系的起点。每个叶子后面括注了对应的 `kind` 字符（如 `(kind=i)`）。

**(2) `issubdtype` 的实现**

[issubdtype: numerictypes.py:411-474](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L411-L474) 核心只有结尾三行：

```python
if not issubclass_(arg1, generic):
    arg1 = dtype(arg1).type      # 把 "float64"/np.float64 统一成标量类型
if not issubclass_(arg2, generic):
    arg2 = dtype(arg2).type
return issubclass(arg1, arg2)
```

它先用 `issubclass_`（一个不会抛异常的包装）判断参数是否已经是标量类型；若不是（比如传了字符串 `"i4"` 或一个 `ndarray`），就用 `dtype(...).type` 取出其标量类型，最后交给内置 `issubclass`。文档字符串里的例子也点出一个关键事实：**同族不同宽度的类型互不为子类型**——`np.issubdtype(np.float64, np.float32)` 是 `False`，但二者都是 `floating` 的子类型。

**(3) 把所有标量类型注册为模块属性**

[globals loop: numerictypes.py:541-546](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L541-L546) 这段循环把 `allTypes` 字典里的每一项（既包括 `int64` 这样的具体类型，也包括 `integer` 这样的抽象类型）挂到 `numerictypes` 模块的全局命名空间，并加入 `__all__`：

```python
for key in allTypes:
    globals()[key] = allTypes[key]
    __all__.append(key)
```

正因为这一步，你才能写 `np.int64`、`np.integer`、`np.floating`——它们其实是 `numerictypes.int64` 等经 `numpy/__init__.py` 再导出后的名字。`allTypes` 字典本身则来自 `_type_aliases.py`（见 4.3）。

**(4) 与 Python `numbers` ABC 桥接**

[_register_types: numerictypes.py:562-569](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L562-L569) 把 NumPy 抽象类型注册成 Python 标准库 `numbers` 模块的抽象基类：

```python
def _register_types():
    numbers.Integral.register(integer)
    numbers.Complex.register(inexact)
    numbers.Real.register(floating)
    numbers.Number.register(number)
```

这让你可以写 `isinstance(np.float64(1.0), numbers.Real)` 得到 `True`——NumPy 的类型层次和 Python 原生的数值抽象基类打通了。

#### 4.1.4 代码实践

**实践目标**：亲手探查一个 `dtype` 实例的各项属性，建立对 `kind`/`char`/`num`/`itemsize` 的直觉。

**操作步骤**（在装好 NumPy 的环境里执行）：

```python
import numpy as np

for t in ['bool', 'int8', 'int32', 'uint64', 'float16', 'float64',
          'complex128', 'S5', 'U3', 'datetime64[s]', 'object']:
    d = np.dtype(t)
    print(f"{t:>14} -> kind={d.kind} char={d.char!r} num={d.num:>4} "
          f"itemsize={d.itemsize:>2} str={d.str!r}")
```

**需要观察的现象**：

- `kind` 只有一个字符，标识「族」（见 4.1.1 的 `kind=` 标注）。
- `char` 是更细的「类型字符」（如 `float64` 是 `'d'`，`bool` 是 `'?'`）。
- `num` 是一个全局整数编号（如 `float64` 是 `12`），与 `dtypes.pyi` 里 `_TypeCodes[..., L[12]]` 对应。
- `str` 是「数组协议类型字符串」，形如 `<f8`：`<` 表示小端字节序，`f` 是 kind 字符，`8` 是字节数。

**预期结果**（x86-64 小端机器；精确列宽以本地为准）：

```
          bool -> kind=b char='?' num=   0 itemsize= 1 str='|b1'
          int8 -> kind=i char='b' num=   1 itemsize= 1 str='|i1'
         int32 -> kind=i char='i' num=   5 itemsize= 4 str='<i4'
        uint64 -> kind=u char='Q' num=  10 itemsize= 8 str='<u8'
       float16 -> kind=f char='e' num=  23 itemsize= 2 str='<f2'
       float64 -> kind=f char='d' num=  12 itemsize= 8 str='<f8'
    complex128 -> kind=c char='D' num=  15 itemsize=16 str='<c16'
            S5 -> kind=S char='S' num=  18 itemsize= 5 str='|S5'
            U3 -> kind=U char='U' num=  19 itemsize=12 str='<U3'
 datetime64[s] -> kind=M char='M' num=  21 itemsize= 8 str='<M8[s]'
        object -> kind=O char='O' num=  17 itemsize= 8 str='|O'
```

其中 `num` 的具体值与 `kind`/`char`/`itemsize` 的对应关系是 NumPy 在 C 层固定的，若你的输出与上表不符（例如扩展精度 `longdouble` 的字节数），以本地结果为准——可标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：判断下列两个表达式的值，并解释原因。

```python
np.issubdtype(np.float64, np.floating)
np.issubdtype(np.float64, np.float32)
```

**答案**：第一个 `True`，第二个 `False`。因为 `float64` 和 `float32` 都是 `floating` 的子类（同族），但二者处于树的同一层、互不为父子，所以 `issubclass(np.float64, np.float32)` 为 `False`。这正是源码注释 [numerictypes.py:447-459](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L447-L459) 强调的点。

**练习 2**：为什么 `np.issubdtype(np.bool_, np.number)` 是 `False`？

**答案**：因为 `bool` 在类型树里直接挂在 `generic` 下，并不经过 `number` 节点（见 [numerictypes.py:42-44](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L42-L44)）。所以布尔值在 NumPy 的归类里不属于「数值」。如果你需要「布尔或数值」这样的判断，应当分别检查 `np.bool_` 和 `np.number`，或使用 4.1 节后面会提到的 `np.isdtype`。

---

### 4.2 公开 DType 类（numpy/dtypes.py）

#### 4.2.1 概念说明

NumPy 2.x 之后，类型体系里多了一个层次——**DType 类**。要理解它，先看三个容易混淆的对象：

| 对象 | 例子 | 是什么 |
|------|------|--------|
| 标量类型 | `np.float64` | 一个 Python 类；数组元素的类型 |
| dtype 实例 | `np.dtype("float64")` | 一个对象；描述一块内存如何解读 |
| DType 类 | `np.dtypes.Float64DType` | dtype 实例的「类型」，即 `type(np.dtype("float64"))` |

可以用 Python 的 `type()` 理解：普通对象的类型是类，而 `dtype` 实例的「类型」就是它的 DType 类。三者的关系可以记成一条链：

\[
\text{type}\big(\text{np.dtype}(\text{"float64"})\big) \;=\; \text{np.dtypes.Float64DType}, \quad
\text{np.dtype}(\text{"float64"}).\text{type} \;=\; \text{np.float64}
\]

为什么要单独搞一层 DType 类？因为新一代 NumPy 允许第三方注册**自定义 dtype**（比如 `ml_dtypes` 的 `bfloat16`），DType 类就是「一种 dtype 的元类型」，承担类型解析、强制转换（cast）、循环注册等职责。对初学者而言，你通常**不需要直接用** `np.dtypes.Float64DType`——用 `np.float64` 或字符串 `"float64"` 就够了，但要知道它存在，并在读源码时认得它。

#### 4.2.2 核心流程

`numpy/dtypes.py` 这个文件本身**很薄**：它并不在 Python 里定义 DType 类，而是作为 DType 类的「公开容器」。真正的 DType 类由 C 层创建，然后被挂到这个模块上。流程是：

1. C 扩展 `_multiarray_umath` 启动时，为每个内置 dtype（bool、各宽度整数、浮点、复数、object、bytes/str/void、datetime/timedelta）创建一个 DType 类，并赋值给 `numpy.dtypes` 的同名属性（如 `Float64DType`）。
2. 新增的变长字符串类型 `StringDType` 由于有循环依赖，通过该模块里的 `_add_dtype_helper` 函数在初始化末尾再注册一次。
3. `numpy/__init__.py` 通过懒加载（`__getattr__`）让 `import numpy.dtypes` 只在第一次访问时发生。

#### 4.2.3 源码精读

**(1) 模块定位——文档字符串**

[dtypes.py:1-23](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.py#L1-L23) 开头说明：本模块是「DType 类的家」，类似于 Python 内置的 `types` 模块；并强调 `.. versionadded:: NumPy 1.25`——在 1.25 之前，DType 类只能间接访问。文档里明确提醒：**直接使用这些类并不常见**，因为它们的标量对应物（`np.float64`）或字符串（`"float64"`）就够用了。

**(2) 公开名字只有一个**

[dtypes.py:26](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.py#L26) 注意这行：`__all__ = ["register_dlpack_dtype"]`。这说明文件里**Python 代码**唯一主动声明公开的只有 `register_dlpack_dtype` 一个函数。其余几十个 `Float64DType` 之类的名字，都是 C 层在运行时动态塞进来的（见 4.2.3 第 (4) 点），或由 `_add_dtype_helper` 追加到 `__all__`。

**(3) 注册辅助函数**

[_add_dtype_helper: dtypes.py:75-87](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.py#L75-L87) 这个函数把一个 C 创建的 DType 类挂到 `numpy.dtypes` 上，并可选地加一个别名：

```python
def _add_dtype_helper(DType, alias):
    from numpy import dtypes
    setattr(dtypes, DType.__name__, DType)
    __all__.append(DType.__name__)
    if alias:
        alias = alias.removeprefix("numpy.dtypes.")
        setattr(dtypes, alias, DType)
        __all__.append(alias)
```

它避免了「把 DType 类直接塞进 `_multiarray_umath` 命名空间」这种写法，提供一个统一的注册通道。

**(4) 公开 DType 类的完整名单**

[dtypes.pyi:19-54](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.pyi#L19-L54) 类型存根里列出了 `numpy.dtypes` 全部公开 DType 类，从 `BoolDType`、`Int8DType` 一路到 `StringDType`。以 `Float64DType` 为例：

[Float64DType: dtypes.pyi:343-352](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.pyi#L343-L352) 存根声明了它的 `name`（`"float64"`）、`str`（`"<f8"` 或 `">f8"`）、`kind`（`"f"`）、`char`（`"d"`）、`num`（`12`）、`itemsize`（`8`）——这与 4.1.4 里 `np.dtype("float64")` 的属性完全一致，印证了「DType 类 = dtype 实例的类型，二者描述同一组元信息」。

**(5) C 层注册 `StringDType`**

[multiarraymodule.c:5367-5388](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5367-L5388) C 模块初始化末尾用 `_add_dtype_helper` 把 `StringDType` 注册进 `numpy.dtypes`，注释解释了为何要放在最后：为了避免「遗留 DType 类尚未就绪」的循环依赖。其他内置 DType 类则在更早的 C 初始化阶段直接挂上。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「标量类型 / dtype 实例 / DType 类」三者的关系。

**操作步骤**：

```python
import numpy as np

d = np.dtype("float64")          # dtype 实例
print("dtype 实例        :", d)
print("d.type            :", d.type)              # 标量类型 np.float64
print("type(d)           :", type(d))             # DType 类
print("DType 类是否在 np.dtypes:", type(d) is np.dtypes.Float64DType)

# DType 类也可以直接实例化得到 dtype（不常用）
print("Float64DType()    :", np.dtypes.Float64DType())
```

**需要观察的现象**：

- `d.type` 是 `<class 'numpy.float64'>`（标量类型）。
- `type(d)` 是 `<class 'numpy.dtypes.Float64DType'>`（DType 类）。
- 第三个比较为 `True`，验证了 4.2.1 里那条等式。
- `np.dtypes.Float64DType()` 直接得到 `dtype('float64')`。

**预期结果**：上述四点全部成立（精确的 `repr` 字符串以本地 NumPy 版本为准，可标注「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：`np.float64` 和 `np.dtypes.Float64DType` 是同一个对象吗？如果不是，它们分别属于 4.2.1 表里的哪一行？

**答案**：不是同一个对象。`np.float64` 是「标量类型」（数组元素的类型），`np.dtypes.Float64DType` 是「DType 类」（dtype 实例的类型）。它们通过 `np.dtype("float64")` 这个 dtype 实例联系起来：`d.type is np.float64` 且 `type(d) is np.dtypes.Float64DType`。

**练习 2**：为什么 `numpy/dtypes.py` 里的 `__all__` 只列了 `register_dlpack_dtype`，却能 `from numpy.dtypes import Float64DType`？

**答案**：因为其余 DType 类不是在这个 Python 文件里写死的，而是在 C 扩展初始化时由 `_add_dtype_helper` 或直接 `setattr` 动态挂到模块上的（见 [dtypes.py:75-87](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.py#L75-L87) 与 [multiarraymodule.c:5367-5388](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5367-L5388)）。`__all__` 只反映 Python 源码层面的静态导出，不反映运行时动态注入的名字。

---

### 4.3 类型别名表（_core/_type_aliases.py）

#### 4.3.1 概念说明

如果你翻 NumPy 的文档，会发现同一种 `float64` 有好多名字：`np.float64`、`np.double`、`np.dtype("float64")`、`np.dtype("f8")`、`np.dtype("d")`……这是历史遗留：NumPy 既要兼容 C 的类型名（`double`、`short`、`longlong`），又要提供按位宽命名的统一名（`float64`），还要支持 Python 内置名（`float`）。`_type_aliases.py` 的职责就是**把这些名字都规整成三张表**：

- `allTypes`：名字 → 标量类型，会作为 `numerictypes` 模块的属性暴露（即 `np.float64` 等都来自它）。
- `sctypeDict`：范围更广的别名 → 标量类型，支持 `np.dtype("float")` 这种「字符串查类型」。
- `sctypes`：按族分组的列表，如 `sctypes["int"]` 是所有有符号整数类型，`sctypes["float"]` 是所有浮点类型；`isdtype` 等函数靠它判断「这一族」。

#### 4.3.2 核心流程

别名表的搭建在 `_type_aliases.py` 里分四步：

1. **抽象类型**先入表：`generic`、`number`、`integer`、`floating` 等抽象类直接从 C 模块 `multiarray` 取出，放进 `allTypes`。
2. **具体类型**入表：遍历 C 层提供的 `typeinfo` 字典（它包含每个内置类型的 `type`、`kind`、`itemsize` 等信息），把每个具体标量类型同时写进 `allTypes` 和 `sctypeDict`。
3. **C 风格别名**追加：把 `double → float64`、`single → float32`、`int_ → intp` 等映射追加进两张表。
4. **Python 风格别名**只进 `sctypeDict`：`float → float64`、`complex → complex128`、`int → int_`、`str → str_` 等，专门用于 `np.dtype("float")` 这类字符串查询。
5. **分组表 `sctypes`**单独构建：对每个具体类型，用 `issubclass` 判断它属于 `signedinteger`/`unsignedinteger`/`floating`/`complexfloating` 中的哪一族，归入对应的组，最后按位宽排序。

注意第 3 步里 `int_` 指向 `intp`（指针宽度的整数），`uint` 指向 `uintp`——这是 NumPy 的「默认整数」选择，**与平台有关**（64 位机器上 `intp` 就是 `int64`）。

#### 4.3.3 源码精读

**(1) 三张表的职责说明**

[dicts docstring: _type_aliases.py:7-17](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L7-L17) 文档字符串明确写了三张字典各自的用途，是理解本模块的钥匙。

**(2) 抽象类型入表**

[abstract types: _type_aliases.py:31-39](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L31-L39) 这里列出了全部抽象类型名，并用 `getattr(ma, name)` 从 C 模块取出：

```python
_abstract_type_names = {
    "generic", "integer", "inexact", "floating", "number",
    "flexible", "character", "complexfloating", "unsignedinteger",
    "signedinteger"
}
for _abstract_type_name in _abstract_type_names:
    allTypes[_abstract_type_name] = getattr(ma, _abstract_type_name)
```

这正是 4.1.3 第 (3) 点里 `np.integer`、`np.floating` 的最终来源。

**(3) 具体类型从 `typeinfo` 入表**

[typeinfo loop: _type_aliases.py:41-49](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L41-L49) 遍历 C 层 `typeinfo`，区分两类键：以 `NPY_` 开头的是 C 常量名（进 `c_names_dict`），其余的是具体类型名，取 `v.type` 同时写进 `allTypes` 与 `sctypeDict`。`typeinfo` 本身由 C 扩展提供，是「类型元信息」的源头。

**(4) C 风格别名**

[_aliases: _type_aliases.py:51-66](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L51-L66) 这段定义了 `double`/`single`/`half`/`int_`/`uint` 等别名，并同时写进 `allTypes` 和 `sctypeDict`：

```python
_aliases = {
    "double": "float64",
    "cdouble": "complex128",
    "single": "float32",
    "csingle": "complex64",
    "half": "float16",
    "bool_": "bool",
    "int_": "intp",
    "uint": "uintp",
}
```

所以 `np.double is np.float64` 为 `True`，`np.int_ is np.intp` 也为 `True`。

**(5) Python 风格别名只进 `sctypeDict`**

[_extra_aliases: _type_aliases.py:70-82](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L70-L82) 这段把 Python 内置名映射到 NumPy 类型，但**只进 `sctypeDict`，不进 `allTypes`**：

```python
_extra_aliases = {
    "float": "float64",
    "complex": "complex128",
    "object": "object_",
    "bytes": "bytes_",
    "int": "int_",
    "str": "str_",
    "unicode": "str_",
}
```

注释解释了原因：为了支持 `np.dtype("float")` 这样的字符串访问。也正因为它没进 `allTypes`，`np.float` 这样的属性访问并不被鼓励（事实上 `np.float` 早被移除）——字符串查询和属性访问走的是两条不同的表。

**(6) 分组表 `sctypes`**

[sctypes building: _type_aliases.py:102-128](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L102-L128) 对每个具体类型，逐个尝试它是不是某个抽象类型的子类，归入 `int`/`uint`/`float`/`complex`/`others` 五个组，最后按 `(itemsize, name)` 排序保证顺序确定：

```python
for type_group, abstract_type in [
    ("int", ma.signedinteger), ("uint", ma.unsignedinteger),
    ("float", ma.floating), ("complex", ma.complexfloating),
    ("others", ma.generic)
]:
    if issubclass(concrete_type, abstract_type):
        sctypes[type_group].add(concrete_type)
        break
```

这张分组表随后被 `numerictypes.isdtype` 使用——还记得 [isdtype: numerictypes.py:374-391](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L374-L391) 吗？当传入 `"real floating"` 时，它就取 `sctypes["float"]` 整组类型来判断。

#### 4.3.4 代码实践

**实践目标**：验证别名体系，并观察「平台相关」类型的实际宽度。

**操作步骤**：

```python
import numpy as np

# (a) C 风格别名——同一对象
print("np.double is np.float64 :", np.double is np.float64)
print("np.single is np.float32 :", np.single is np.float32)
print("np.int_ is np.intp       :", np.int_ is np.intp)

# (b) 字符串别名——dtype 查询（走 sctypeDict）
for s in ["float", "complex", "int", "str", "bytes", "double", "f8", "d"]:
    print(f"{s!r:>10} -> {np.dtype(s)}")

# (c) 平台相关宽度
print("np.intp 的位宽           :", np.dtype(np.intp).itemsize * 8)
print("np.int_ 的位宽           :", np.dtype(np.int_).itemsize * 8)
print("np.longdouble 的位宽     :", np.dtype(np.longdouble).itemsize * 8)
```

**需要观察的现象**：

- 第 (a) 组全部为 `True`，证明别名指向同一标量类型对象。
- 第 (b) 组里 `"float"`、`"double"`、`"f8"`、`"d"` 都得到 `dtype('float64')`。
- 第 (c) 组：在 64 位机器上 `intp`/`int_` 通常是 64 位；`longdouble` 的宽度因平台而异（x86 Linux 常见 80 位=10 字节，Windows/ARM 常见 64 位=8 字节）。

**预期结果**：第 (a)(b) 组在所有平台上稳定一致；第 (c) 组的 `longdouble` 宽度需「待本地验证」。这也正是 `_type_aliases.py` 第 84-95 行要为扩展精度动态生成 `float96`/`float128` 这类「按位宽命名别名」的原因——同一份 `longdouble` 在不同平台对应不同名字。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.dtype("float")` 能工作，但访问 `np.float` 会失败？

**答案**：`"float"` 这个字符串别名只被写进了 `sctypeDict`（见 [_type_aliases.py:70-82](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L70-L82)），用于 `np.dtype(...)` 的字符串查询；它没有被写进 `allTypes`，也就不会作为 `np.float` 这样的模块属性暴露。字符串查询和属性访问走的是两张不同的表。

**练习 2**：在 32 位平台和 64 位平台上，`np.int_` 分别等价于哪个按位宽命名的类型？依据是哪一行源码？

**答案**：`np.int_` 永远等价于 `np.intp`（指针宽度的有符号整数），见 [_type_aliases.py:59-60](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_type_aliases.py#L59-L60)（`"int_": "intp"`）。而 `np.intp` 的实际位宽由 C 层 `typeinfo` 决定：32 位平台上 `intp` 是 `int32`，64 位平台上是 `int64`。所以 `np.int_` 的宽度是平台相关的，这也是 NEP 50 之后强调「不要假设默认整数位宽」的原因。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格里要求的核心任务：**写一个函数，输入任意数组，返回其 dtype 的 `kind`、`itemsize`、所属抽象父类（如 `integer`/`floating`），并用 `np.issubdtype` 验证。**

```python
import numpy as np

def describe_dtype(arr):
    """报告一个数组的 dtype 元信息与所属类型族。"""
    dt = np.asarray(arr).dtype      # 拿到 dtype 实例
    sc = dt.type                    # 拿到标量类型，如 np.float64

    # 从具体到抽象逐层判断「所属抽象父类」
    # 顺序很重要：complexfloating 必须在 floating 之前（复数不是实浮点）
    if np.issubdtype(dt, np.bool_):
        family, parent = 'bool', np.bool_
    elif np.issubdtype(dt, np.complexfloating):
        family, parent = 'complexfloating', np.complexfloating
    elif np.issubdtype(dt, np.floating):
        family, parent = 'floating', np.floating
    elif np.issubdtype(dt, np.integer):
        family, parent = 'integer', np.integer
    elif np.issubdtype(dt, np.character):
        family, parent = 'character', np.character
    elif np.issubdtype(dt, np.flexible):
        family, parent = 'flexible', np.flexible
    elif np.issubdtype(dt, np.datetime64):
        family, parent = 'datetime', np.datetime64
    else:
        family, parent = 'object/other', np.generic

    return {
        'dtype': dt,
        'kind': dt.kind,                 # 单字符族标识
        'itemsize': dt.itemsize,         # 每元素字节数
        'scalar_type': sc,               # 标量类型
        'family': family,                # 抽象父类名
        'parent_class': parent,          # 抽象父类对象
    }


# —— 验证 ——
cases = [
    np.array([1, 2, 3], dtype=np.int8),
    np.array([1, 2, 3], dtype=np.uint32),
    np.array([1.0, 2.0], dtype=np.float64),
    np.array([1+2j, 3-4j], dtype=np.complex128),
    np.array([True, False]),
    np.array([b'hi', b'yo'], dtype='S2'),
    np.array(['a', 'b'], dtype='U1'),
    np.array(['2020-01-01'], dtype='datetime64[D]'),
    np.array([object(), object()]),
]

for a in cases:
    info = describe_dtype(a)
    # 用 np.issubdtype 再次验证 family 判断是否正确
    ok = np.issubdtype(info['dtype'], info['parent_class'])
    print(f"{str(info['dtype']):>20}  kind={info['kind']}  "
          f"itemsize={info['itemsize']:>2}  family={info['family']:<16}  "
          f"issubdtype验证={ok}")
```

**实践要点（对应本讲三个模块）**：

1. `np.asarray(arr).dtype` 与 `dt.type` 的用法，呼应 4.1「标量类型 vs dtype 实例」。
2. `dt.kind` / `dt.itemsize` 直接来自 4.1.4 探查过的属性。
3. 「逐层 `issubdtype` 判断所属族」复用的正是 [numerictypes.py:411-474](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L411-L474) 的 `issubdtype`，以及 [numerictypes.py:548-556](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L548-L556) `typecodes` 背后的 `kind` 归类思想。
4. 判断顺序体现了 4.1.1 的类型树：`complexfloating` 在 `floating` 之前，因为复数浮点并非实浮点；`bool` 不在 `number` 下，所以单独判断。

**预期结果**（精确列宽以本地为准；`issubdtype验证` 列应全部为 `True`）：

```
              int8  kind=i  itemsize= 1  family=integer           issubdtype验证=True
             uint32  kind=u  itemsize= 4  family=integer           issubdtype验证=True
            float64  kind=f  itemsize= 8  family=floating          issubdtype验证=True
         complex128  kind=c  itemsize=16  family=complexfloating   issubdtype验证=True
               bool  kind=b  itemsize= 1  family=bool              issubdtype验证=True
                 S2  kind=S  itemsize= 2  family=character         issubdtype验证=True
                 U1  kind=U  itemsize= 4  family=character         issubdtype验证=True
      datetime64[D]  kind=M  itemsize= 8  family=datetime          issubdtype验证=True
             object  kind=O  itemsize= 8  family=object/other      issubdtype验证=True
```

**延伸思考**：尝试把函数里的判断改用 `np.isdtype(dt, "real floating")` 这类字符串形式（见 [numerictypes.py:321-408](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L321-L408)），对比它与 `issubdtype` 在表达「族」时的差别——`isdtype` 是 Array API 标准的新接口，不接受任意抽象类，只接受固定几个字符串名。

## 6. 本讲小结

- NumPy 的标量类型构成一棵以 `generic` 为根的继承树，`bool` 不属于 `number`，`datetime64`/`timedelta64` 也是独立分支；这棵树写死在 [numerictypes.py:40-77](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L40-L77)。
- `issubdtype` 的本质是「归一化成标量类型后调 `issubclass`」；同族不同宽度的类型互不为子类型（如 `float64` 不是 `float32` 的子类型）。
- 类型体系有三层对象：标量类型（`np.float64`）、dtype 实例（`np.dtype("float64")`）、DType 类（`np.dtypes.Float64DType`）；后两者通过 `type(d)` 相连。
- `numpy/dtypes.py` 本身很薄，DType 类由 C 层动态注册，`StringDTYPE` 通过 `_add_dtype_helper` 注入。
- 庞大的别名体系由 `_type_aliases.py` 用三张表（`allTypes`/`sctypeDict`/`sctypes`）搭起：`double`/`single`/`int_` 等同时进前两张，`float`/`complex`/`int` 等只进 `sctypeDict` 以支持字符串查询；`sctypes` 为按族分组表，被 `isdtype` 使用。
- `kind`（族字符）、`char`（类型字符）、`num`（编号）、`itemsize`（字节宽）、`str`（数组协议串）共同精确描述一个 dtype。

## 7. 下一步学习建议

- **下一讲 u2-l3「类型转换、提升规则与精度」**：本讲只讲了「类型是什么」，下一讲进入「类型之间如何互相转换、如何选择公共类型」——即 `astype`、`result_type`、`promote_types`，以及 NumPy 2.x 的 NEP 50 提升规则。建议先回顾本讲的 `kind` 与类型树，因为提升规则正是按这棵树和 `kind` 来决定的。
- **延伸阅读源码**：
  - [numpy/_core/numerictypes.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py) 里的 `isdtype`（[L321-L408](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numerictypes.py#L321-L408)），看它如何用 `sctypes` 把字符串 kind 名翻译成具体类型集合。
  - 类型存根 [numpy/dtypes.pyi](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/dtypes.pyi)，把每个 DType 类的 `kind`/`char`/`num`/`itemsize` 当成一张速查表来读。
- **官方文档**：`arrays.scalars`（标量类型总览）与 `arrays.dtypes`（dtype 详情），它们与本讲的源码树一一对应。
