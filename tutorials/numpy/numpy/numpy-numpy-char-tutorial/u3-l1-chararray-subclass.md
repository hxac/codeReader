# chararray 的 ndarray 子类化机制

## 1. 本讲目标

本讲进入 NumPy 2.5 已被软弃用、但仍承担大量历史代码的 `chararray` 类内部。我们只盯住一个问题：**`chararray` 是如何作为 `ndarray` 的子类，靠四个钩子方法把「定宽字符串数组 + 取值自动剥空白 + ufunc 自动包回 chararray」这三件事拼出来的。**

学完本讲，你应该能够：

- 说清 `chararray` 为什么是 `ndarray` 的子类，以及这种子类化在 NumPy 里由哪三个钩子（`__new__` / `__array_finalize__` / `__array_wrap__`）支撑。
- 读懂 `chararray.__new__` 如何把「类型 + 字符宽度」打包成 dtype，并用 `ndarray.__new__` 从零造出一个定宽字符串数组。
- 解释 `__array_finalize__` 如何充当「dtype 守门员」，在 `view()` / 切片等派生路径上用 `'VSUbc'` 校验拒绝非字符串数据。
- 解释 `__getitem__` 如何在取出标量时自动 `rstrip()`，从而兑现 chararray 最有特色的「取值即剥空白」承诺。
- 解释 `__array_wrap__` 如何在 ufunc 执行后把字符串结果重新包回 chararray。
- 动手构造一个 chararray 并验证上述四条路径。

## 2. 前置知识

本讲承接前几讲已建立的认知，不重复其推导，只引用结论：

- **三层关系**（u1-l1）：`numpy.char` 是门面，实现落在 `numpy/_core/defchararray.py`，多数字符串函数再委托给 `numpy.strings`。`chararray` 是 `defchararray` 中**本地独有**的名字之一。
- **字符串 dtype**（u1-l2）：`str_`（kind `U`，UCS-4，4 字节/字符）、`bytes_`（kind `S`，1 字节/字符），二者共同基类 `character`；定宽公式
  \[
  \text{itemsize}_{\text{字节}} = \text{字符数} \times c,\quad c \in \{1\ (\text{bytes\_}),\ 4\ (\text{str\_})\}.
  \]
  `chararray.__new__` 收到的 `itemsize` 是**字符数**，而非字节数。
- **比较运算符的 rstrip 语义**（u2-l3）：六个比较自由函数固定传 `rstrip=True`，`chararray` 的 `__eq__/__lt__/...` 也委托给它们。
- **`@set_module("numpy.char")`**（u2-l4）：只改写 `__module__`，让定义在 `_core` 的类/函数对外伪装成 `numpy.char` 成员。
- **软弃用**（u2-l1）：访问 `np.char.chararray` 会触发 `DeprecationWarning`，本讲所有动手实践都需要先压制或接受这个警告。

本讲需要补充的几个新概念：

- **ndarray 子类化**：在 NumPy 里继承 `ndarray` 不能只靠写 `__init__`，因为 `ndarray` 是用 C 实现的「不可变式」对象，实例创建走 `__new__`；而且数组会频繁地被「派生」（切片、`view`、ufunc 输出），子类需要钩子在派生时介入。这套机制就是 **`__new__` / `__array_finalize__` / `__array_wrap__` 三件套**。
- **`__new__` 与 `__init__`**：对不可变类型，对象内存布局在 `__new__` 阶段就定死（如 dtype、shape），`__init__` 来不及改。所以 `chararray` 重写的是 `__new__`。
- **`dtype.char`**：每种 dtype 有一个单字符码（`S`/`U`/`V`/`b`/`c`/`i`/`f`…），`chararray` 用它做轻量的「是不是字符串」判断。
- **`view()`**：在不拷贝数据的前提下，把同一段内存重新解释成另一种 dtype 或子类。子类化里大量出现 `arr.view(chararray)`。
- **标量与 `character`**：当你用整数下标从字符串数组里取「一个元素」时，得到的是 `str_`/`bytes_` **标量**（不是 0 维数组），它们都是 `character` 的子类。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `numpy/_core/defchararray.py` | `chararray` 类，L404–L599 | `chararray` 的全部子类化逻辑：`__new__`、`__array_wrap__`、`__array_finalize__`、`__getitem__` |
| `numpy/_core/numerictypes.py` | 类型层级图，L70–L76 | 确认 `character` 是 `str_`/`bytes_` 的共同基类，是 `__getitem__` 里 `isinstance(val, character)` 判断的依据 |

> 说明：本讲的永久链接全部指向当前 HEAD `4e7f3b33df3e5ed2e9f46f6febdee62364520c70`。文件 `defchararray.py` 的真实路径是 `numpy/_core/defchararray.py`（`numpy/char/__init__.py` 只是门面，不含类定义）。

## 4. 核心概念与源码讲解

### 4.1 ndarray 子类化三件套：总览

#### 4.1.1 概念说明

`chararray` 的类定义只有一行继承关系：

```python
@set_module("numpy.char")
class chararray(ndarray):
```

[numpy/_core/defchararray.py:404-405](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L404-L405) —— `chararray` 继承自 `ndarray`，`@set_module` 把它的 `__module__` 改写为 `numpy.char`。

但「继承」本身并不能让子类在所有派生场景下都保持行为。NumPy 数组会被切片、被 `view`、被 ufunc 计算后产生新数组，这些新数组默认只是普通 `ndarray`，**不会自动变成 `chararray`**。要让「派生出来的也是 chararray、且 dtype 合法、且取值会剥空白」，NumPy 提供了三个钩子：

| 钩子 | 触发时机 | chararray 用它做什么 |
| --- | --- | --- |
| `__new__` | 显式构造 `chararray(...)` 时 | 决定 dtype（`str_`/`bytes_`）与字符宽度，调 `ndarray.__new__` 真正分配内存 |
| `__array_finalize__(self, obj)` | 任何「从模板派生新数组」之后（含 `__new__` 内部、`view`、切片、ufunc 输出回填） | 校验 `self.dtype.char` 是否合法，不合法就抛 `ValueError` |
| `__array_wrap__(self, arr, context)` | ufunc（等）执行完、准备返回结果之前 | 若结果是字符串型，重新 `view` 回 `chararray`；否则保持普通 `ndarray` |

注意 `__getitem__` 不属于「三件套」协议，但它是 chararray 实现「取值即剥空白」的关键重写，本讲单列一节（4.4）。

#### 4.1.2 核心流程：三种创建路径分别走哪些钩子

用伪代码描述 chararray 的三条典型生命周期：

```
路径 A：显式构造   chararray((3,), itemsize=5)
   → __new__ 被调用
        → 内部 ndarray.__new__(cls, ...) 分配内存
             → __array_finalize__(self, obj=None)   # 校验 dtype
        → 填充 filler（若有）
   → 得到 chararray

路径 B：视图转换   some_array.view(chararray)
   → ndarray.__new__ 不直接被调，但 view 会触发
        → __array_finalize__(self, obj=some_array)  # 校验 dtype
   → 得到 chararray 或抛 ValueError

路径 C：ufunc 运算   np.strings.add(a, b)  （a 是 chararray）
   → ufunc 在 base ndarray 上算出结果 arr
        → __array_wrap__(self=a, arr=结果)           # 决定要不要包回 chararray
   → 返回 chararray 或普通 ndarray
```

关键直觉：`__new__` 管「**第一次造**」，`__array_finalize__` 管「**所有派生**（含造）后的合法性」，`__array_wrap__` 管「**算完之后**的再包装」。三者覆盖了数组「生 → 衍生 → 计算」的全周期。

#### 4.1.3 源码精读

源码里有一段「实现说明」直接点出了本类的方法委托策略，也暗示了 `__array_wrap__` 存在的原因：

```python
# IMPLEMENTATION NOTE: Most of the methods of this class are
# direct delegations to the free functions in this module.
# However, those that return an array of strings should instead
# return a chararray, so some extra wrapping is required.
```

[numpy/_core/defchararray.py:601-604](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L601-L604) —— 这条注释是理解整个 chararray 类的钥匙：**方法体大多委托给本模块的自由函数，凡是返回字符串数组的，都要额外包装回 chararray**。这条原则同时解释了 `__array_wrap__`（自动包装 ufunc 结果）和后续讲义 u3-l2 会讲到的「方法委托 + `asarray(...)` 包裹」。

#### 4.1.4 代码实践：给三个钩子各加一行「脚印」

**实践目标**：用「打日志」的方式亲眼看到三个钩子在什么操作下被触发。

**操作步骤**（示例代码，需要本地有可 import 的 NumPy）：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", DeprecationWarning)  # 压制 chararray 软弃用警告

import numpy.core.defchararray as dc

# 给三个钩子套一层打印（仅用于观察，不改源码——用子类）
class SpyChar(dc.chararray):
    def __new__(cls, *a, **k):
        print("  -> __new__ called")
        return super().__new__(cls, *a, **k)
    def __array_finalize__(self, obj):
        print("  -> __array_finalize__ called; dtype.char =", self.dtype.char)
        super().__array_finalize__(obj)
    def __array_wrap__(self, arr, context=None, return_scalar=False):
        print("  -> __array_wrap__ called; arr.dtype.char =", arr.dtype.char)
        return super().__array_wrap__(arr, context, return_scalar)

print("[A] 显式构造"); c = SpyChar((3,), itemsize=5, unicode=True); c[:] = ["ab   ", "cde ", "f    "]
print("[B] view 转换"); v = np.array(["x", "y"], dtype="U1").view(SpyChar)
print("[C] ufunc 运算"); r = np.strings.add(c, c)
print("type(r) =", type(r).__name__)
```

**需要观察的现象**：路径 A 同时打印 `__new__` 和 `__array_finalize__`；路径 B 只打印 `__array_finalize__`；路径 C 打印 `__array_wrap__`，并且 `type(r)` 仍是 `chararray`。

**预期结果**：三条路径的打印顺序与上表一致。**待本地验证**：不同 NumPy 版本里 `__array_wrap__` 是否一定被调（取决于 ufunc 是否走 legacy 协议），若未打印，记录实际类型即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 chararray 重写的是 `__new__` 而不是 `__init__`？
**答案**：`ndarray` 是 C 实现的类型，其内存布局（dtype、shape、itemsize）在 `__new__` 阶段就已确定且不可在 `__init__` 中修改；chararray 需要把 `unicode`/`itemsize` 翻译成具体 dtype 并传给 `ndarray.__new__`，这必须在「分配内存之前」完成，所以只能重写 `__new__`。

**练习 2**：如果一个钩子你忘记写，会出现什么症状？（一句话）
**答案**：不写 `__array_finalize__`，则非法 dtype 能混入 chararray；不写 `__array_wrap__`，则 ufunc 结果会退化为普通 `ndarray`，丢失 chararray 的方法与「剥空白」语义。

---

### 4.2 `__new__`：从零构造一个定宽字符串数组

#### 4.2.1 概念说明

`chararray.__new__` 负责把用户友好的参数（`unicode` 布尔、`itemsize` 字符数）翻译成底层 `ndarray` 认识的 dtype，然后**借用父类的 `__new__`** 真正分配内存。它处理三件事：

1. **选类型**：`unicode=True` 用 `str_`，否则用 `bytes_`。
2. **定宽度**：`itemsize` 是字符数，要强制成 Python `int`（避免 NumPy 整数类型带来的「`.itemsize` 误用」陷阱，见 u1-l2）。
3. **填初值**：当传入的 `buffer` 是 Python `str` 时，因为 `str` 没有缓冲区接口，需要先记下当 filler，构造完再 `self[...] = filler` 赋值。

#### 4.2.2 核心流程

```
输入: shape, itemsize=1(字符数), unicode=False, buffer=None
  ├─ dtype = str_ if unicode else bytes_
  ├─ itemsize = int(itemsize)            # 规避 NumPy 整数类型
  ├─ 若 buffer 是 str: filler=buffer, buffer=None   # str 无 buffer 接口
  ├─ self = ndarray.__new__(cls, shape, (dtype, itemsize), ...)   # (类型, 字符数) 打包成 dtype
  │        └─ 内部触发 __array_finalize__(self, None)              # 见 4.3
  └─ 若 filler 非空: self[...] = filler
返回 self
```

注意 `(dtype, itemsize)` 这种「元组当 dtype」的写法：NumPy 允许用 `(类型对象, 宽度)` 来表达「这个类型、每元素这么宽」，对字符串类型即「字符宽度」。

#### 4.2.3 源码精读

```python
def __new__(cls, shape, itemsize=1, unicode=False, buffer=None,
            offset=0, strides=None, order='C'):
    if unicode:
        dtype = str_
    else:
        dtype = bytes_

    # force itemsize to be a Python int, since using NumPy integer
    # types results in itemsize.itemsize being used as the size of
    # strings in the new array.
    itemsize = int(itemsize)
    ...
    if buffer is None:
        self = ndarray.__new__(cls, shape, (dtype, itemsize), order=order)
    else:
        self = ndarray.__new__(cls, shape, (dtype, itemsize),
                               buffer=buffer, offset=offset, strides=strides,
                               order=order)
    if filler is not None:
        self[...] = filler
    return self
```

[numpy/_core/defchararray.py:550-580](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L550-L580) —— 这是 chararray 的「出生证明」。`int(itemsize)` 的强转注释解释了 u1-l2 提到的「NumPy 整数陷阱」；`ndarray.__new__(cls, shape, (dtype, itemsize), ...)` 把类型与字符宽度打包传入，是子类化里「**借父类构造器造自己**」的标准手法。

#### 4.2.4 代码实践：镜像源码手搓一个 chararray

**实践目标**：完全模仿 `__new__` 的写法，体会 `(dtype, itemsize)` 打包与「字符数 vs 字节数」的差别。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", DeprecationWarning)

# 镜像源码：unicode=True → str_，itemsize=5 是「字符数」
a = np.char.chararray((3,), itemsize=5, unicode=True)
a[:] = ["ab   ", "cde ", "f    "]   # 故意带尾部空白
print("itemsize(字节) =", a.itemsize)   # 预期 5 字符 × 4 = 20
print("dtype.char    =", a.dtype.char)  # 预期 'U'
print("repr          =", repr(a))       # 内部存储仍保留填充空白
```

**需要观察的现象**：`itemsize` 是 **20**（字节），不是 5；因为 `chararray` 收到字符数 5，底层按 UCS-4 每字符 4 字节展开。`repr` 显示的元素**仍带尾部空白**——剥空白发生在「取标量」时（4.4），不在构造时。

**预期结果**：`itemsize=20`、`dtype.char='U'`、repr 含 `'ab   '`。**待本地验证**：repr 的精确格式（是否显示 `chararray([...], dtype='<U5')`）。

#### 4.2.5 小练习与答案

**练习 1**：把上面的 `unicode=True` 改成 `unicode=False`，`itemsize` 会变成多少？为什么？
**答案**：变成 5。`unicode=False` 用 `bytes_`，每字符 1 字节，5 字符 = 5 字节。

**练习 2**：为什么源码要把 `itemsize = int(itemsize)` 强转一次？
**答案**：若 `itemsize` 是 NumPy 整数类型（如 `np.int64`），它自身也有 `.itemsize` 属性（=8），后续构造逻辑可能误把这个 8 当成字符串宽度。强转成 Python `int` 可消除该二义性。

---

### 4.3 `__array_finalize__`：dtype 守门员

#### 4.3.1 概念说明

`__array_finalize__` 是子类化里最容易被忽视、却最关键的钩子。它在**每一次派生新数组之后**被调用——无论是 `__new__` 内部、`view` 转换、切片，还是 ufunc 回填。它拿到一个 `obj`（派生源），可以在「新数组的 dtype 已经定下来、但还没交给用户」这个时间点上做校验或继承属性。

chararray 对它的用法极其简洁：**只校验 dtype 是否字符串型，不合法就拒绝**。这是 chararray 的「守门员」——保证无论你怎么 view，都不能把一个整数数组伪装成 chararray。

#### 4.3.2 核心流程

```
__array_finalize__(self, obj):
  └─ if self.dtype.char not in 'VSUbc':
         raise ValueError("Can only create a chararray from string data.")
```

判据是 `dtype.char` 是否落在字符串 `'VSUbc'` 这 5 个字符里：

| `dtype.char` | 含义 | chararray 是否接受 |
| --- | --- | --- |
| `U` | `str_`（Unicode） | ✅ 真正的字符串 |
| `S` | `bytes_`（字节串） | ✅ 真正的字符串 |
| `V` | void / 原始字节 | ✅ 容忍（原始字节缓冲） |
| `b` | bool | ✅ 容忍（源码注释：用于「重建」） |
| `c` | complex | ✅ 容忍 |
| `i`/`f`/`l`… | 整数 / 浮点 | ❌ 拒绝，抛 ValueError |

#### 4.3.3 源码精读

```python
def __array_finalize__(self, obj):
    # The b is a special case because it is used for reconstructing.
    if self.dtype.char not in 'VSUbc':
        raise ValueError("Can only create a chararray from string data.")
```

[numpy/_core/defchararray.py:590-593](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L590-L593) —— 全方法只有一行实质判断。注释只解释了 `'b'`：布尔类型被放行是因为它在数组的「重建」（如 pickle / `__reduce__` 恢复）路径里会出现。`'V'`、`'c'` 同样被容忍，源码未给理由，我们**不做超出注释的推断**——只需记住守门员对真正的字符串类型（`S`/`U`）一定放行，对常见数值类型（`i`/`f`/`l`）一定拒绝。

> 小提示：`obj` 在「显式构造」时为 `None`，在「view/切片派生」时是源数组。chararray 完全没用 `obj`，只看 `self.dtype`，所以它的守门逻辑与派生路径无关——**只认 dtype，不认来源**。

#### 4.3.4 代码实践：触发守门员的拒绝路径

**实践目标**：亲手触发 `ValueError`，验证守门员在 `view()` 路径上生效。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", DeprecationWarning)

# 1) 合法 view：bytes_ 数组可以 view 成 chararray
b = np.array([b"hi", b"yo"], dtype="|S2")
ca = b.view(np.char.chararray)
print("合法 view 成功:", type(ca).__name__, ca.dtype.char)   # 预期 chararray S

# 2) 非法 view：整数数组试图伪装成 chararray → ValueError
n = np.array([1, 2, 3])
try:
    n.view(np.char.chararray)
except ValueError as e:
    print("被守门员拦下:", e)   # 预期 "Can only create a chararray from string data."
```

**需要观察的现象**：第 1 步成功，`dtype.char` 为 `S`；第 2 步抛 `ValueError`，信息正是 `Can only create a chararray from string data.`。

**预期结果**：与上面注释一致。这条 `view()` → `__array_finalize__` → `ValueError` 的链路是本节最确定、可稳定复现的行为。

#### 4.3.5 小练习与答案

**练习 1**：用 `np.array([True, False]).view(np.char.chararray)` 会不会抛异常？为什么？
**答案**：不会抛异常。`bool` 的 `dtype.char` 是 `'b'`，落在 `'VSUbc'` 里，守门员放行。这正是源码注释所说「`b` 用于重建」的特殊放行。

**练习 2**：如果你重写一个 ndarray 子类，希望从 `view` 派生时继承父数组的某个自定义属性，应该在哪写？
**答案**：在 `__array_finalize__(self, obj)` 里写 `self.myattr = getattr(obj, 'myattr', 默认值)`。这正是 `obj` 参数的用途——chararray 没用它，但通用子类化里它是「跨派生继承属性」的唯一入口。

---

### 4.4 `__getitem__`：取值自动 rstrip

#### 4.4.1 概念说明

chararray 文档承诺的第一条增值功能是：「values automatically have whitespace removed from the end when indexed」（取值时自动去掉尾部空白）。这条承诺的实现不在比较运算符里（那是 u2-l3 讲的 `rstrip=True`），而在 **`__getitem__`**：每次「取一个标量元素」时，把结果 `.rstrip()` 一遍。

关键区分：**标量取值 vs 切片取值**。

- `c[0]` → 取出 1 个元素，结果是 `str_`/`bytes_` **标量** → 会 `rstrip`。
- `c[0:2]` → 取出一段，结果是**子数组**（仍是 chararray） → 不走 rstrip 分支。

之所以能用 `isinstance(val, character)` 区分这两种情况，是因为标量 `str_`/`bytes_` 都是 `character` 的子类，而「子数组」是 `ndarray` 的实例、不是 `character`。

#### 4.4.2 核心流程

```
__getitem__(self, idx):
  ├─ val = ndarray.__getitem__(self, idx)      # 先按普通 ndarray 取值
  ├─ if isinstance(val, character):            # 取出的是「单个字符串标量」
  │      return val.rstrip()                   # 剥掉尾部空白后返回
  └─ return val                                # 子数组 / 其它：原样返回
```

类型层级依据（`character` 是 `str_`/`bytes_` 的基类）：

```
flexible
  └─ character
       ├─ bytes_   (kind S)
       └─ str_     (kind U)
```

[numpy/_core/numerictypes.py:70-76](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/numerictypes.py#L70-L76) —— 这段类型层级图证实了 `character` 是 `str_`/`bytes_` 的共同基类，是 `__getitem__` 判断的依据。

#### 4.4.3 源码精读

```python
def __getitem__(self, obj):
    val = ndarray.__getitem__(self, obj)
    if isinstance(val, character):
        return val.rstrip()
    return val
```

[numpy/_core/defchararray.py:595-599](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L595-L599) —— 整段只 4 行，却是 chararray 最有特色的行为来源。它**只重写「读」，不重写「写」**：写入时（`c[0] = 'ab   '`）尾部空白照常存进定宽缓冲；读取标量时才剥掉。所以同一份数据，`repr`（批量读、走数组路径）看到空白，`c[0]`（标量读）看不到。

#### 4.4.4 代码实践：标量被剥、切片不被剥

**实践目标**：亲眼对照「标量取值被 rstrip」与「切片取值保留空白」。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", DeprecationWarning)

c = np.char.array(["ab   ", "cde "])      # chararray，元素带尾部空白
print("[标量] c[0]         =", repr(c[0]))         # 预期 'ab'（被 rstrip）
print("[原生] ndarray 取   =", repr(np.ndarray.__getitem__(c, 0)))  # 'ab   '（未剥）
print("[切片] c[0:1]       =", repr(c[0:1]))       # 子数组，元素仍带空白
print("[切片类型]          =", type(c[0:1]).__name__)   # 预期 chararray
```

**需要观察的现象**：`c[0]` 得到 `'ab'`（已剥空白）；用 `np.ndarray.__getitem__(c, 0)` 绕过重写后得到 `'ab   '`（未剥）；切片 `c[0:1]` 的元素仍带空白，且类型仍是 `chararray`。

**预期结果**：如上。本节行为由源码 4 行直接决定，可稳定复现。

#### 4.4.5 小练习与答案

**练习 1**：`c[0]` 返回的 `'ab'` 是 Python 内置 `str` 还是 `numpy.str_`？
**答案**：是 `numpy.str_`（`character` 子类）。`rstrip()` 返回同类型的标量，不会跨类型转成内置 `str`。

**练习 2**：为什么切片 `c[0:1]` 的元素**不**被 rstrip？用源码解释。
**答案**：`ndarray.__getitem__(self, slice(0,1))` 返回的是子数组（`ndarray`/`chararray` 实例），不是 `character` 标量，`isinstance(val, character)` 为假，直接走 `return val`，不做 rstrip。

---

### 4.5 `__array_wrap__`：ufunc 输出再包装

#### 4.5.1 概念说明

当一个 chararray 参与 ufunc 运算（如 `a + a` 底层的 `numpy.strings.add`），NumPy 会在「结果数组算出来之后、返回给用户之前」调用 `__array_wrap__(self, arr, context)`，给子类一个机会「把结果重新包成自己的类型」。

chararray 的策略很直接：

- 若结果 `arr.dtype.char` 是字符串型（`'S'/'U'/'b'/'c'`），就 `arr.view(type(self))` 包回 chararray；
- 否则（如比较产生的 `bool` 数组），原样返回普通 `ndarray`。

这与 4.1.3 那条「返回字符串数组的，都要包回 chararray」的实现说明完全呼应。

#### 4.5.2 核心流程

```
__array_wrap__(self, arr, context=None, return_scalar=False):
  ├─ if arr.dtype.char in "SUbc":
  │      return arr.view(type(self))   # 字符串结果 → 包回 chararray
  └─ return arr                        # 非字符串结果（如 bool）→ 普通 ndarray
```

注意判据集合这里是 `"SUbc"`（**没有 `'V'`**），与 `__array_finalize__` 的 `'VSUbc'`（**有 `'V'`**）略有差异：构造/派生时容忍 void，但 ufunc 结果包装时不把 void 视作字符串。这种细节差异值得留意，但不要过度解读——两处都把 `S`/`U` 当作真正的字符串。

#### 4.5.3 源码精读

```python
def __array_wrap__(self, arr, context=None, return_scalar=False):
    # When calling a ufunc (and some other functions), we return a
    # chararray if the ufunc output is a string-like array,
    # or an ndarray otherwise
    if arr.dtype.char in "SUbc":
        return arr.view(type(self))
    return arr
```

[numpy/_core/defchararray.py:582-588](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L582-L588) —— `type(self)` 而非写死 `chararray`，是为了让 chararray 的子类（如你在 4.1.4 里写的 `SpyChar`）也能正确回包。注释直接说清了意图：ufunc 输出是字符串型才包回 chararray，否则退回普通 ndarray。

#### 4.5.4 代码实践：观察「字符串结果回包、bool 结果不回包」

**实践目标**：对比两种 ufunc 结果——字符串拼接 vs 相等比较——看类型差异。

**操作步骤**：

```python
import warnings, numpy as np
warnings.simplefilter("ignore", DeprecationWarning)

c = np.char.array(["ab  ", "cde "])

# (1) 字符串结果：a + a 走 strings.add，dtype 仍是 U → __array_wrap__ 包回 chararray
r1 = c + c
print("c + c        :", type(r1).__name__, r1.dtype.char)   # 预期 chararray U

# (2) 非字符串结果：== 走比较自由函数，返回 bool 数组 → 不包回 chararray
r2 = (c == "ab")
print("c == 'ab'    :", type(r2).__name__, r2.dtype.char)   # 预期 ndarray b（或 chararray?）
```

**需要观察的现象**：`c + c` 的结果类型仍是 `chararray`（被 `__array_wrap__` 包回）；`c == "ab"` 的结果是布尔数组。

**预期结果**：`r1` 为 `chararray`。**待本地验证**：`r2` 的精确类型——`chararray.__eq__` 直接调用 `equal(self, other)`，其返回值是否经过 `__array_wrap__` 可能因版本而异；若你观察到 `r2` 仍是 `chararray`，请记录并思考它与 `__array_wrap__` 判据（`'b'` 在 `"SUbc"` 里！）的关系。

> 思考题（不在预期结果里下结论）：布尔 dtype.char 是 `'b'`，而 `__array_wrap__` 的判据 `"SUbc"` **包含** `'b'`。这意味着即便 ufunc 输出是 bool，按源码也会被 `view(chararray)`。本节让你亲手观察这一现象，正是为了暴露「判据集合偏宽」这个真实存在的设计细节。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `__array_wrap__` 用 `arr.view(type(self))` 而不是 `chararray(arr)`？
**答案**：`view` 不拷贝数据，只改「类型标签」，开销极低；且 `type(self)` 让 chararray 的子类也能正确回包。`chararray(arr)` 会走完整构造路径，既慢又可能触发不必要的校验/拷贝。

**练习 2**：`__array_wrap__` 和 `__array_finalize__` 都可能「派生 chararray」，它们分工有何不同？
**答案**：`__array_finalize__` 是**守门员**（校验 dtype 合法性，在所有派生后触发）；`__array_wrap__` 是**包装员**（决定 ufunc 结果要不要贴回 chararray 标签，在 ufunc 后触发）。前者可能抛 `ValueError`，后者只决定返回类型。

---

## 5. 综合实践：四件套联动验证

**任务**：把本讲的四个钩子串成一条端到端的验证脚本，亲手走一遍 chararray 的「生 → 校验 → 取值 → 计算」全周期。

**步骤**：

1. **压制软弃用警告**（访问 `chararray` 会触发，见 u2-l1）：
   ```python
   import warnings, numpy as np
   warnings.simplefilter("ignore", DeprecationWarning)
   ```
2. **构造（触发 `__new__` + `__array_finalize__`）**：模仿源码，造一个 unicode、字符宽 5 的 chararray，存入带尾空白的字符串：
   ```python
   c = np.char.chararray((3,), itemsize=5, unicode=True)
   c[:] = ["ab   ", "cde ", "f    "]
   print("itemsize字节 =", c.itemsize)   # 预期 20
   ```
3. **取值（触发 `__getitem__`）**：对照标量与原生取值，验证「标量被剥、内部留存」：
   ```python
   assert str(c[0]) == "ab", c[0]                                  # 标量被 rstrip
   assert str(np.ndarray.__getitem__(c, 0)) == "ab   "             # 原生存空白
   ```
4. **守门员（触发 `__array_finalize__` 拒绝路径）**：整数数组 view 成 chararray 应失败：
   ```python
   try:
       np.array([1, 2, 3]).view(np.char.chararray)
       raise SystemExit("应当抛 ValueError 却没有")
   except ValueError as e:
       assert "string data" in str(e), e
   ```
5. **包装（触发 `__array_wrap__`）**：字符串 ufunc 结果应保持 chararray：
   ```python
   r = c + c
   assert type(r).__name__ == "chararray", type(r)
   ```

**预期结果**：第 2 步 `itemsize=20`；第 3 步两条断言都成立；第 4 步抛 `ValueError` 且信息含 `string data`；第 5 步 `r` 为 `chararray`。

**待本地验证**：第 5 步在部分 NumPy 版本上，若 `+` 走的 `__add__` 直接返回自由函数结果而未必触发 `__array_wrap__`，类型可能不同；如不符，请记录实际类型并结合 4.5 的源码分析原因。

## 6. 本讲小结

- `chararray` 是 `ndarray` 的子类，靠 **`__new__` / `__array_finalize__` / `__array_wrap__`** 三件套（外加重写的 `__getitem__`）支撑其全部特色行为。
- `__new__` 把 `unicode`/`itemsize`（字符数）翻译成 dtype，用 `(dtype, itemsize)` 元组打包后**借 `ndarray.__new__` 造内存**，并对 `itemsize` 强制 `int()` 规避 NumPy 整数陷阱。
- `__array_finalize__` 是「dtype 守门员」，用 `dtype.char in 'VSUbc'` 校验所有派生路径，对整数/浮点等非字符串数据抛 `ValueError`；它只认 dtype、不认来源。
- `__getitem__` 用 `isinstance(val, character)` 区分标量与子数组：**取标量时 `rstrip()`，取切片时不剥**，兑现「取值即剥空白」承诺。
- `__array_wrap__` 在 ufunc 后触发，对字符串型结果（`dtype.char in "SUbc"`）用 `arr.view(type(self))` 包回 chararray，否则退回普通 ndarray。
- 三件套覆盖了数组「生 → 衍生 → 计算」的全周期；判据集合 `'VSUbc'`（finalize）与 `"SUbc"`（wrap）略有差异，是真实存在的设计细节。

## 7. 下一步学习建议

- **u3-l2 运算符重载与方法委托**：本讲只讲了四个钩子，chararray 还重载了 `__eq__/__add__/__mul__/__mod__` 等运算符并委托大量字符串方法，下一讲系统精读。
- **u3-l3 array / asarray 工厂函数**：本讲的 `__new__` 是底层构造器，实际代码多用 `np.char.array` / `np.char.asarray`，它们对 str/bytes/list/object 做分支归一化后最终也会落到 `chararray`，下一讲衔接。
- **u3-l4 弃用迁移**：chararray 已在 2.5 软弃用，学完内部机制后应学习如何迁移到「普通 `str_`/`bytes_` 数组 + `numpy.strings`」。
- **延伸阅读**：NumPy 官方文档「Subclassing ndarray」一章，是理解 `__array_finalize__`/`__array_wrap__`/`__array_ufunc__` 协议的权威资料；可对照本讲四个钩子加深理解。
