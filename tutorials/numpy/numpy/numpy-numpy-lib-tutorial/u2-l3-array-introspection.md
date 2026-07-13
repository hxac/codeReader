# 数组内省工具：CPU 分发、内存边界与算子协议

## 1. 本讲目标

本讲串讲 numpy.lib 里三类「窥探或接入数组内部」的工具。读完本讲，你应当能够：

- 用 `np.lib.introspect.opt_func_info` 查询任意 ufunc 在当前机器上实际启用的 CPU 指令集分发目标。
- 理解 `byte_bounds` 如何借助 `__array_interface__` 计算数组占用的字节范围，尤其是负步长（reverse）情形。
- 看懂 `_binary_method` / `_numeric_methods` 如何把一个 ufunc 包装成 Python 的魔术方法（`__add__` 等）。
- 用 `NDArrayOperatorsMixin` + `__array_ufunc__` 写出一个「不继承 ndarray 却支持全套运算符」的自定义数组类。

这三件工具看似互不相干，但有一个共同主题：它们都站在数组「外面」，用协议（`__array_interface__`、`__array_ufunc__`、CPU 分发表）去观察或接管数组的内部行为。

## 2. 前置知识

在进入源码前，先用通俗语言铺几个概念。

1. **`__array_interface__`（数组接口协议）**
   任何暴露这个属性的 Python 对象，都把自己描述成「一块带形状/步长的内存」。它是一个字典，关键字段包括：
   - `data`：形如 `(指针整数, 是否只读)` 的元组；
   - `shape`：各维长度组成的元组；
   - `strides`：沿每一维前进一个元素需要跨越的字节数，可为负。
   上一讲 `u2-l2` 里 `_info` 打印的 `data pointer / strides / byteorder` 就取自这里。本讲的 `byte_bounds` 同样吃这个字典。

2. **ufunc（通用函数）**
   numpy 的向量化运算核心，如 `np.add`、`np.multiply`、`np.less`。它们的特点是「逐元素、类型分发、可重写」。`NDArrayOperatorsMixin` 的全部魔法，就是让 Python 运算符最终落到某个 ufunc 上。

3. **Python 双下方法的三种形态**
   - 正向：`a + b` 触发 `a.__add__(b)`；
   - 反射：当 `a.__add__` 返回 `NotImplemented`（或 a 不支持 b 的类型）时，Python 再尝试 `b.__radd__(a)`；
   - 就地：`a += b` 触发 `a.__iadd__(b)`。
   `_binary_method` / `_reflected_binary_method` / `_inplace_binary_method` 正好一一对应这三种。

4. **NEP-0013 与 `__array_ufunc__` 协议**
   当一个对象出现在 ufunc 调用里（如 `np.add(a, 1)`），如果它定义了 `__array_ufunc__`，numpy 会把执行权交还给它，由它决定怎么算。`NDArrayOperatorsMixin` 正是建立在「运算符 → ufunc → `__array_ufunc__`」这条链路上的。

5. **再导出层 vs `_impl`（来自 `u1-l2`）**
   本讲三个工具的「暴露方式」并不相同，正好可以做个对比：`opt_func_info` 和 `NDArrayOperatorsMixin` 直接写在公开模块里；而 `byte_bounds` 则藏在 `_array_utils_impl.py`，再由只有 7 行的薄模块 `array_utils.py` 再导出。

## 3. 本讲源码地图

| 文件 | 作用 | 公开路径 |
| --- | --- | --- |
| [introspect.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/introspect.py) | `opt_func_info`：查询 ufunc 的 CPU 分发目标 | `np.lib.introspect.opt_func_info` |
| [_array_utils_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py) | `byte_bounds`（以及再导出的 `normalize_axis_*`） | 经薄模块暴露为 `np.lib.array_utils.byte_bounds` |
| [array_utils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/array_utils.py) | 薄再导出模块，把 `_array_utils_impl` 的名字搬出去 | `np.lib.array_utils` |
| [mixins.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py) | `NDArrayOperatorsMixin` 及 `_binary_method` 等辅助工厂 | `np.lib.mixins.NDArrayOperatorsMixin` |
| [tests/test_array_utils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_array_utils.py) | `byte_bounds` 的权威示例（连续/转置/反向/步进） | — |
| [tests/test_mixins.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_mixins.py) | `ArrayLike` 示例与运算符覆盖测试 | — |

这三个模块在 [numpy/lib/\_\_init\_\_.py:35-38](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L35-L38) 被作为公开子模块导入，并出现在 [\_\_all\_\_](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L49-L50) 里，所以 `np.lib.array_utils` / `np.lib.introspect` / `np.lib.mixins` 都是稳定公开入口。

---

## 4. 核心概念与源码讲解

### 4.1 opt_func_info：窥探 ufunc 的 CPU 分发目标

#### 4.1.1 概念说明

numpy 的每个 ufunc 在编译期会被「克隆」出多份机器码，分别针对不同的 CPU 指令集扩展（SSE、AVX2、AVX-512、FMA 等）。运行时，numpy 探测当前 CPU 支持哪些扩展，把调用**分发（dispatch）**到它能跑的最快那一份。这套机制是 numpy 在数值计算上很快的关键之一，但它通常是「黑盒」——你很难知道 `np.add` 在你这台机器上到底用了 AVX2 还是 AVX-512。

`opt_func_info` 就是打开这个黑盒的钥匙：它返回一个字典，告诉你每个被优化的函数、每种数据类型签名，当前实际选中的指令集（`current`）和所有可选的指令集（`available`）。

#### 4.1.2 核心流程

```text
opt_func_info(func_name=None, signature=None)
  │
  ├─ 从 C 层取全量分发表：__cpu_targets_info__
  │     结构: { 函数名: { 签名字符串: {"current":..., "available":...} } }
  │
  ├─ 若给定 func_name：用 re.compile(func_name).search 按函数名过滤
  │
  ├─ 若给定 signature：对每个签名字符串，逐字符 c 判断
  │     sig_pattern.search(c)          # 直接匹配字符，如 "d"
  │     或 sig_pattern.search(dtype(c).name)  # 或匹配类型名，如 "float64"
  │
  └─ 返回过滤后的字典
```

这里的「签名字符串」沿用 numpy ufunc 的类型字符约定：`d`=float64、`f`=float32、`F`=complex64、`D`=complex128 等。例如 `np.add` 的签名 `'ddd'` 表示「两个 float64 输入、一个 float64 输出」。

#### 4.1.3 源码精读

函数定义与文档：[introspect.py:8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/introspect.py#L8)。它从 C 扩展里取分发表：

```python
# introspect.py:66-68 —— 真正的数据来源是 C 层的分发表
import re
from numpy._core._multiarray_umath import __cpu_targets_info__ as targets, dtype
```

`__cpu_targets_info__` 是 `_multiarray_umath` 暴露的 C 层字典，记录了所有已优化的函数及其按签名分组的分发信息。`dtype` 被一并导入，用来把单字符（如 `'d'`）转成类型名（`'float64'`）。

按函数名过滤：[introspect.py:70-77](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/introspect.py#L70-L77)

```python
# 用正则 search（不是 match），所以 "add" 也能命中 "absolute"
if func_name is not None:
    func_pattern = re.compile(func_name)
    matching_funcs = {k: v for k, v in targets.items() if func_pattern.search(k)}
else:
    matching_funcs = targets
```

按签名过滤：[introspect.py:79-93](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/introspect.py#L79-L93)

```python
# 对签名字符串里的"每个字符"都试一次匹配：既匹配字符本身，也匹配其 dtype 名
for chars, targets in v.items():
    if any(sig_pattern.search(c) or sig_pattern.search(dtype(c).name) for c in chars):
        matching_chars[chars] = targets
```

这段是本函数最精巧之处：`any(...)` 会遍历签名字符串的每个字符，只要其中任意一个字符（或它对应的类型名）命中正则，整条签名就被保留。因此 `signature="float64"` 能命中 `'ddd'`（因为 `'d'` 的 `dtype('d').name` 是 `'float64'`），`signature="complex"` 也能命中 `'Ff'`/`'Dd'`（复数类型）。

返回值的典型结构（取自 [docstring 示例 introspect.py:38-63](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/introspect.py#L38-L63)，具体指令集随机器变化）：

```json
{
  "add": {
    "ddd": {"current": "FMA3__AVX2", "available": "FMA3__AVX2 baseline(SSE SSE2 SSE3)"},
    "FFF": {"current": "FMA3__AVX2", "available": "FMA3__AVX2 baseline(SSE SSE2 SSE3)"}
  }
}
```

#### 4.1.4 代码实践

**实践目标**：亲手查询 `np.add` 在当前机器上对 `float64` 的分发目标，并观察 `current` 与 `available` 的区别。

**操作步骤**：

```python
import json
import numpy as np

info = np.lib.introspect.opt_func_info(func_name="^add$", signature="float64")
print(json.dumps(info, indent=2))
```

**需要观察的现象**：

- 返回字典里应包含键 `"add"`，其下又有一个形如 `"ddd"` 的签名键。
- `current` 是单一段（如 `FMA3__AVX2`），表示运行时实际选中的指令集；`available` 是一长串，列出所有可选目标，通常以 `baseline(...)` 收尾，即「保底」的纯标量实现。

**预期结果**：能稳定看到 `add -> ddd -> {current, available}` 的两层嵌套；具体指令集名称（SSE41/AVX2/AVX512F…）因 CPU 而异，属于「待本地验证」的内容。如果想看全部被优化的函数，把两个参数都省略即可（注意输出可能很大）。

#### 4.1.5 小练习与答案

**练习 1**：为什么传 `func_name="add"` 会同时返回 `add` 和 `absolute`，而 `func_name="^add$"` 只返回 `add`？

> **答案**：因为过滤用的是 `re.search`（子串匹配），`"add"` 是 `"absolute"` 的子串；`^add$` 用了行首行尾锚点，要求整个函数名严格等于 `add`。

**练习 2**：`signature="f"` 和 `signature="float32"` 的结果是否一定相同？

> **答案**：对于签名字符里含 `'f'` 的条目，二者都能命中——前者直接匹配字符 `'f'`，后者匹配 `dtype('f').name == 'float32'`。但 `signature="f"` 还可能误伤类型名里含字母 `f` 的其它签名，所以推荐用完整的类型名。

---

### 4.2 byte_bounds：计算数组的内存字节边界

#### 4.2.1 概念说明

一个 ndarray 的元素在内存里未必「挤在一起」。考虑切片 `a[::2]`（每隔一个取一个）或翻转 `a[::-1]`（步长为负），数组实际访问的内存地址会**稀疏**或**反向**。`byte_bounds(a)` 返回一个元组 `(low, high)`：

- `low`：数组所有元素里最低的字节地址；
- `high`：刚好越过数组最高字节的位置（即「one past the end」）。

它常用于内存映射、内存边界校验、把数组原始缓冲区整体拷贝出去等场景。注意：当数组不连续时，`low` 与 `high` 之间会夹着一些数组根本不用的字节。

#### 4.2.2 核心流程

设数组起始指针为 `p = data[0]`，每个元素占 `itemsize` 字节。

**连续数组（`strides is None`）**，元素紧凑排列：

\[ \text{low} = p,\qquad \text{high} = p + \text{size}\times\text{itemsize} \]

**一般数组**：逐维扫描。对每一维 `(shape, stride)`：

- 若 `stride >= 0`：沿该维最后一个元素在最远处（高地址方向），把 `high` 抬高 \((\text{shape}-1)\times\text{stride}\)；
- 若 `stride < 0`：沿该维「下标 0」反而在高地址、下标最大者落在低地址，于是把 `low` 降低 \((\text{shape}-1)\times\text{stride}\)（注意此时这是一项负数加法，相当于 `low` 往小地址移动）。

所有维度处理完后，再把 `high` 加上最后一个字节所属元素的 `itemsize`：

\[ \text{high} \mathrel{+}= \text{itemsize} \]

直观地说：`low` 累加所有「负步长维度」拉低的部分，`high` 累加所有「正步长维度」抬高的部分，最后补上单个元素的宽度。

#### 4.2.3 源码精读

函数定义：[_array_utils_impl.py:11-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py#L11-L12)。注意装饰器 `@set_module("numpy.lib.array_utils")`：

```python
# _array_utils_impl.py:11-12 —— 把 __module__ 改写为公开路径
@set_module("numpy.lib.array_utils")
def byte_bounds(a):
```

这是 `u1-l2` 讲过的「实现藏 `_impl`、对外露薄模块」模式的体现：`byte_bounds` 的代码物理上在 `_array_utils_impl.py`，但它的 `__module__` 被改写成了 `numpy.lib.array_utils`，再由 [array_utils.py:1-7](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/array_utils.py#L1-L7) 这个薄模块把名字搬出去。所以 `help(np.lib.array_utils.byte_bounds)` 看到的是干净的公开路径。

读取数组接口并初始化：[_array_utils_impl.py:45-51](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py#L45-L51)

```python
ai = a.__array_interface__
a_data = ai['data'][0]      # 起始字节地址
astrides = ai['strides']    # 各维步长（字节数），可为 None
ashape = ai['shape']        # 各维长度
bytes_a = asarray(a).dtype.itemsize   # 单个元素字节数

a_low = a_high = a_data     # 起点同时是当前的 low 和 high
```

`a.__array_interface__` 正是第 2 节铺垫的协议字典。这里用 `asarray(a).dtype.itemsize` 取 itemsize，是为了兼容那些「实现了 `__array_interface__` 但未必直接有 `.itemsize`」的类对象。

连续分支与一般分支：[_array_utils_impl.py:52-62](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py#L52-L62)

```python
if astrides is None:
    # 连续情况：直接用总字节数
    a_high += a.size * bytes_a
else:
    for shape, stride in zip(ashape, astrides):
        if stride < 0:
            a_low += (shape - 1) * stride      # 负步长把 low 拉低
        else:
            a_high += (shape - 1) * stride     # 正步长把 high 抬高
    a_high += bytes_a                            # 补上最后一个元素的宽度
return a_low, a_high
```

对照第 4.2.2 节的公式，这段循环就是对每一维做「正步长抬高 high、负步长拉低 low」的累加。返回值用 `(low, high)` 这种「左闭右开」约定，与 Python 切片的习惯一致。

#### 4.2.4 代码实践

**实践目标**：复现 `tests/test_array_utils.py` 里四个典型案例，亲手验证 `high - low` 的取值，尤其体会步进切片与反向切片的差异。

**操作步骤**：

```python
import numpy as np
from numpy.lib import array_utils

# 案例 1：连续数组 —— high-low 恰好等于 size*itemsize
a = np.arange(12).reshape(3, 4)
low, high = array_utils.byte_bounds(a)
assert high - low == a.size * a.itemsize

# 案例 2：转置（仍是正步长，但非 C 连续）
b = a.T
low, high = array_utils.byte_bounds(b)
assert high - low == b.size * b.itemsize

# 案例 3：反向（负步长）
c = a.T[::-1]
low, high = array_utils.byte_bounds(c)
assert high - low == c.size * c.itemsize

# 案例 4：步进切片 a[::2] —— 字节范围比 size*itemsize 更大
d = np.arange(12)[::2]        # 取 0,2,4,6,8,10，共 6 个元素
low, high = array_utils.byte_bounds(d)
print("strided high-low =", high - low,
      "  size*itemsize =", d.size * d.itemsize)
```

**需要观察的现象**：

- 前三个案例 `high - low` 都等于 `size * itemsize`（48 或对应字节数），因为转置和反向都没有让元素「散开」到更大的范围。
- 案例 4 的 `high - low` 会**大于** `d.size * d.itemsize`，因为步长为 2 让元素之间空出了一格。

**预期结果**：案例 4 的差值恰好满足 `test_strided` 的断言 `high - low == b.size * 2 * b.itemsize - b.itemsize`（见 [test_array_utils.py:26-32](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_array_utils.py#L26-L32)）。对 `int64`（itemsize=8）即 `6 * 2 * 8 - 8 = 88` 字节。

#### 4.2.5 小练习与答案

**练习 1**：对一个 `np.arange(5)[::-1]`（反向、负步长），`low` 指向的是 `arange` 的第 0 个元素还是最后一个元素？

> **答案**：指向最后一个元素（下标 4）。反向切片的 `data[0]` 仍是原缓冲区里下标 4 的位置（高地址），而负步长使 `low` 被拉低到下标 0（低地址）处。

**练习 2**：为什么源码里取 itemsize 要写 `asarray(a).dtype.itemsize`，而不是直接 `a.itemsize`？

> **答案**：`byte_bounds` 接受任何「实现了 `__array_interface__`」的对象，这类对象不一定有 `.itemsize` 属性；先 `asarray` 转成真正的 ndarray 再取，更稳健。

---

### 4.3 _binary_method 与 _numeric_methods：把 ufunc 包装成魔术方法

#### 4.3.1 概念说明

`NDArrayOperatorsMixin` 要为大约 40 个 Python 运算符各提供一个双下方法。如果手写，会是 40 段几乎一模一样的样板代码。numpy 的做法是用**工厂函数**批量生成：每个双下方法本质上只是「调用某个 ufunc」。

- `_binary_method(ufunc, name)`：生成正向方法（如 `__add__`），调用 `ufunc(self, other)`。
- `_reflected_binary_method(ufunc, name)`：生成反射方法（如 `__radd__`），调用 `ufunc(other, self)`——注意参数顺序对调。
- `_inplace_binary_method(ufunc, name)`：生成就地方法（如 `__iadd__`），调用 `ufunc(self, other, out=(self,))`。
- `_numeric_methods(ufunc, name)`：把上面三者打包成一个三元组，一次性赋值给 `__add__, __radd__, __iadd__`。
- `_unary_method(ufunc, name)`：生成一元方法（如 `__neg__`），调用 `ufunc(self)`。

此外还有一个关键辅助 `_disables_array_ufunc`：判断对方是否「主动退出」了 ufunc 协议。

#### 4.3.2 核心流程

正向二元的生成与调用过程：

```text
_binary_method(um.add, 'add')
  │  返回闭包 func，func.__name__ = '__add__'
  ▼
a + b
  │  Python 调用 a.__add__(b)  -> func(a, b)
  ▼
func(self=a, other=b):
  if _disables_array_ufunc(b):   # b 是否设置 __array_ufunc__ = None？
      return NotImplemented       # 让 Python 去试 b.__radd__(a)
  return um.add(a, b)            # 否则交给 add 这个 ufunc
```

`_disables_array_ufunc` 实现的是 NEP-0013 的「opt-out」语义：任何类只要把 `__array_ufunc__` 设为 `None`，就等于声明「我不想被当作数组参与 ufunc」。此时返回 `NotImplemented`，把控制权交还给 Python 的反射机制。

#### 4.3.3 源码精读

opt-out 判定：[mixins.py:9-14](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L9-L14)

```python
def _disables_array_ufunc(obj):
    """True when __array_ufunc__ is set to None."""
    try:
        return obj.__array_ufunc__ is None
    except AttributeError:
        return False
```

用 `try/except AttributeError` 是因为普通对象根本没有 `__array_ufunc__` 属性——那不算退出，返回 `False`；只有显式设为 `None` 才算退出。

正向方法工厂：[mixins.py:17-24](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L17-L24)

```python
def _binary_method(ufunc, name):
    """Implement a forward binary method with a ufunc, e.g., __add__."""
    def func(self, other):
        if _disables_array_ufunc(other):
            return NotImplemented
        return ufunc(self, other)
    func.__name__ = f'__{name}__'
    return func
```

要点：闭包捕获了 `ufunc` 与 `name`；`func.__name__` 被显式设成 `'__add__'` 这样的双下名，否则闭包的名字会都叫 `func`，不利于调试。

反射方法（参数对调）：[mixins.py:27-34](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L27-L34)，就地方法（带 `out`）：[mixins.py:37-42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L37-L42)，三者打包：[mixins.py:45-49](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L45-L49)：

```python
def _numeric_methods(ufunc, name):
    """Implement forward, reflected and inplace binary methods with a ufunc."""
    return (_binary_method(ufunc, name),
            _reflected_binary_method(ufunc, name),
            _inplace_binary_method(ufunc, name))
```

一元方法：[mixins.py:52-57](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L52-L57)，没有 `other`、没有反射概念，最简单。

#### 4.3.4 代码实践

**实践目标**：用「源码阅读 + 极小验证」理解工厂函数如何展开成方法。不运行大型代码，只追踪一处赋值。

**操作步骤**：

```python
from numpy.lib import mixins
from numpy._core import umath as um

# 等价于 NDArrayOperatorsMixin 里这一行：
#   __add__, __radd__, __iadd__ = _numeric_methods(um.add, 'add')
add_fwd, add_ref, add_iadd = mixins._numeric_methods(um.add, 'add')

print(add_fwd.__name__, add_ref.__name__, add_iadd.__name__)
# 预期：__add__  __radd__  __iadd__
```

**需要观察的现象**：三个闭包的名字分别是 `__add__`、`__radd__`、`__iadd__`，证明 `_numeric_methods` 一次性生成了「正向 + 反射 + 就地」三件套。

**预期结果**：打印出 `__add__ __radd__ __iadd__`。若想进一步体会 opt-out，可构造一个 `__array_ufunc__ = None` 的对象，调用 `add_fwd(some_arraylike, that_obj)`，应返回 `NotImplemented`（参见 [test_mixins.py 的 test_opt_out](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_mixins.py#L121-L147)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么比较运算符（`<`、`==` 等）只用 `_binary_method`，而不像算术运算那样用 `_numeric_methods`？

> **答案**：因为 Python 没有 `__rlt__` / `__ilt__` 这类反射或就地比较方法——比较只有正向一种形态。源码注释也写明 `comparisons don't have reflected and in-place versions`（见 [mixins.py:144-150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L144-L150)）。

**练习 2**：就地方法里 `ufunc(self, other, out=(self,))` 把结果写回 `self`，那为什么函数仍要 `return`？

> **答案**：就地运算符 `a += b` 会用 `a.__iadd__(b)` 的返回值重新绑定 `a`。虽然 ufunc 已把结果写进 `self`，但必须把（被改写后的）`self` 返回，`a += b` 才能正确生效。

---

### 4.4 NDArrayOperatorsMixin：组合所有运算符

#### 4.4.1 概念说明

`NDArrayOperatorsMixin` 是为「**不继承** `ndarray`、但又想拥有全套运算符」的类准备的基类（mixin）。它本身**不**实现 `__array_ufunc__`，而是把约 40 个运算符双下方法全部接到了对应的 ufunc 上（用上一节的工厂生成）。子类只需补上自己的 `__array_ufunc__`，就能让 `a + 1`、`a * b`、`-a`、`a == b` 全部走自己的逻辑。

它的典型用途：包装一个 `ndarray`、给计算加日志、做懒求值、实现单位/量纲系统等。numpy 文档里反复出现的 `ArrayLike` 示例就是最简版本：它把任意运算的结果都重新包回 `ArrayLike`。

#### 4.4.2 核心流程

当用户写下 `x + 1`（其中 `x` 是 `ArrayLike` 实例），完整的派发链是：

```text
x + 1
  │  ① Python 调用 x.__add__(1)
  │     该方法由 _binary_method(um.add,'add') 生成
  ▼
  ② 闭包体：检查 1 没有 opt-out，于是调用 um.add(x, 1)
  ▼
  ③ NEP-0013：um.add 发现 x 有 __array_ufunc__
  │     于是调用 x.__array_ufunc__(um.add, '__call__', x, 1)
  ▼
  ④ 用户写的 __array_ufunc__：拆包 x.value、算 add、再用 type(self) 包回去
  ▼
  ⑤ 返回一个新的 ArrayLike
```

关键点：运算符并不直接做加法，而是「借 ufunc 之名」触发 `__array_ufunc__`，把真正的计算权完全交给子类。这就是类名里 Operators 能「Override」的原因。

#### 4.4.3 源码精读

类定义与说明：[mixins.py:60-138](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L60-L138)。docstring 里给出了完整的 `ArrayLike` 示例（[mixins.py:73-132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L73-L132)），是本节实践的基础。

类体里 `__slots__ = ()`（[mixins.py:140](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L140)），表明它不持有任何实例状态，纯靠方法组合起作用——这是 mixin 的典型写法。

比较运算符（只有正向）：[mixins.py:145-150](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L145-L150)

```python
__lt__ = _binary_method(um.less, 'lt')
__le__ = _binary_method(um.less_equal, 'le')
__eq__ = _binary_method(um.equal, 'eq')
__ne__ = _binary_method(um.not_equal, 'ne')
__gt__ = _binary_method(um.greater, 'gt')
__ge__ = _binary_method(um.greater_equal, 'ge')
```

算术与位运算（正向 + 反射 + 就地）：[mixins.py:152-174](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L152-L174)

```python
__add__, __radd__, __iadd__ = _numeric_methods(um.add, 'add')
__sub__, __rsub__, __isub__ = _numeric_methods(um.subtract, 'sub')
__mul__, __rmul__, __imul__ = _numeric_methods(um.multiply, 'mul')
__matmul__, __rmatmul__, __imatmul__ = _numeric_methods(um.matmul, 'matmul')
# ... truediv / floordiv / mod / pow / lshift / rshift / and / xor / or
```

注意 `divmod` 比较特殊（[mixins.py:163-164](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L163-L164)）：它有正向与反射，但没有就地版本（Python 不存在 `__idivmod__`）。

一元运算符：[mixins.py:177-180](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L177-L180)

```python
__neg__ = _unary_method(um.negative, 'neg')
__pos__ = _unary_method(um.positive, 'pos')
__abs__ = _unary_method(um.absolute, 'abs')
__invert__ = _unary_method(um.invert, 'invert')
```

这一长串赋值，就是把 `operator` 模块里几乎全部运算符，一对一映射到 numpy 的 ufunc。整个类没有任何 `def` 方法体（除了工厂生成的闭包），却让子类瞬间获得完整的运算符支持——这是「数据驱动」式定义类的优雅范例。

#### 4.4.4 代码实践（本讲指定实践）

**实践目标**：子类化 `NDArrayOperatorsMixin`，实现一个包装 `ndarray` 的 `ArrayLike` 类，并验证 `a + 1` 的结果仍是 `ArrayLike`（而非普通 `ndarray`）。

**操作步骤**（采用 [mixins.py docstring 的 ArrayLike 示例 L73-L116](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L73-L116)，[test_mixins.py:10-48](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_mixins.py#L10-L48) 是其同名拷贝）：

```python
import numbers
import numpy as np

class ArrayLike(np.lib.mixins.NDArrayOperatorsMixin):
    def __init__(self, value):
        self.value = np.asarray(value)

    _HANDLED_TYPES = (np.ndarray, numbers.Number)

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        out = kwargs.get('out', ())
        # 只允许与「受支持类型」运算，否则交还 Python
        for x in inputs + out:
            if not isinstance(x, self._HANDLED_TYPES + (ArrayLike,)):
                return NotImplemented
        # 拆包：把 ArrayLike 换成内部的 ndarray
        inputs = tuple(x.value if isinstance(x, ArrayLike) else x for x in inputs)
        if out:
            kwargs['out'] = tuple(x.value if isinstance(x, ArrayLike) else x for x in out)
        # 真正交给 ufunc 执行
        result = getattr(ufunc, method)(*inputs, **kwargs)
        # 把结果重新包回 ArrayLike
        if type(result) is tuple:
            return tuple(type(self)(x) for x in result)
        elif method == 'at':
            return None
        else:
            return type(self)(result)

    def __repr__(self):
        return f'{type(self).__name__}({self.value!r})'

# 验证
a = ArrayLike([1, 2, 3])
print(a + 1)        # 期望: ArrayLike(array([2, 3, 4]))
print(type(a + 1))  # 期望: <class '...ArrayLike'>
print(1 - a)        # 反射：期望 ArrayLike(array([ 0, -1, -2]))
```

**需要观察的现象**：

- `a + 1` 的结果是 `ArrayLike(...)`，类型保持不变——说明运算确实走了 `__array_ufunc__` 并被重新包装。
- `1 - a`（反射情形）也返回 `ArrayLike`，说明 `__rsub__` 同样生效。
- `a + [1,2,3]`（与 `list` 运算）会抛 `TypeError`，因为 `list` 不在 `_HANDLED_TYPES` 里，`__array_ufunc__` 返回 `NotImplemented`，Python 找不到对方的支持后报错。

**预期结果**：与 docstring 交互示例（[mixins.py:124-132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/mixins.py#L124-L132)）一致，三种写法都打印 `ArrayLike(array(...))`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `__array_ufunc__` 整个删掉，`a + 1` 还能正常工作吗？

> **答案**：不能。`NDArrayOperatorsMixin` 提供的 `__add__` 会调用 `um.add(a, 1)`，而 numpy 一旦在 ufunc 参数里发现没有 `__array_ufunc__` 的非 ndarray 对象，会直接抛 `TypeError: operand ... does not support ...`。mixin 只负责「接线」，真正「通电」要靠子类的 `__array_ufunc__`。

**练习 2**：`__array_ufunc__` 里为什么要单独处理 `method == 'at'`？

> **答案**：`ufunc.at`（如 `np.negative.at`）是就地操作、没有返回值。若不特判，会把 `None` 包成 `ArrayLike(None)` 而出错。特判后返回 `None`，与 numpy 语义一致（见 [test_mixins.py 的 test_ufunc_at](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_mixins.py#L204-L207)）。

---

## 5. 综合实践

把本讲三块知识串起来：基于 `NDArrayOperatorsMixin` 写一个 **LoggedArray**——它不仅保持类型不变，还把每一次经过它的 ufunc 调用记录下来，便于事后审计计算图。同时，我们会用 `byte_bounds` 确认它包装的内部数组内存布局，并用 `opt_func_info` 印证这些 ufunc 确实是「被 CPU 优化的函数」。

**实践目标**

- 复用 4.4 的 `ArrayLike` 模式，但在 `__array_ufunc__` 里加一条「操作日志」。
- 用 `byte_bounds` 验证包装前后底层缓冲区一致（同一块内存）。
- 用 `opt_func_info` 查询日志里出现过的 ufunc（如 `add`）的分发目标。

**操作步骤**

```python
import numbers
import numpy as np
from numpy.lib import mixins, array_utils, introspect

class LoggedArray(mixins.NDArrayOperatorsMixin):
    def __init__(self, value):
        self.value = np.asarray(value)
        self.log = []                       # 记录 (ufunc 名, method)

    _HANDLED_TYPES = (np.ndarray, numbers.Number)

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        self.log.append((ufunc.__name__, method))     # 记账
        out = kwargs.get('out', ())
        for x in inputs + out:
            if not isinstance(x, self._HANDLED_TYPES + (LoggedArray,)):
                return NotImplemented
        inputs = tuple(x.value if isinstance(x, LoggedArray) else x for x in inputs)
        result = getattr(ufunc, method)(*inputs, **kwargs)
        return type(self)(result) if not isinstance(result, tuple) \
            else tuple(type(self)(r) for r in result)

    def __repr__(self):
        return f'LoggedArray({self.value!r})'

# —— 1. 做几步运算 ——
x = LoggedArray([1.0, 2.0, 3.0])
y = (x + 2.0) * 3.0          # 依次触发 add、multiply
print("result =", y)
print("log    =", x.log)     # 注意：y 是新对象，log 在各自对象上

# —— 2. 用 byte_bounds 验证底层缓冲区 ——
raw = np.arange(12).reshape(3, 4)
wrapped = LoggedArray(raw)
low_w, high_w = array_utils.byte_bounds(wrapped)   # 走 __array_interface__
low_r, high_r = array_utils.byte_bounds(raw)
print("same buffer?", (low_w, high_w) == (low_r, high_r))   # 预期 True

# —— 3. 用 opt_func_info 印证日志里的 ufunc 确实是优化函数 ——
info = introspect.opt_func_info(func_name="^add$", signature="float64")
print("add dispatch keys:", list(info.get("add", {}).keys()))
```

**需要观察的现象**

1. `y` 仍是 `LoggedArray`，且 `x.log` 里出现了 `('add', '__call__')`（`*3.0` 的 `multiply` 记录在中间那个新对象上，不在 `x` 上——体会「每次运算都产生新对象、日志分散」这一特性）。
2. `byte_bounds(wrapped)` 与 `byte_bounds(raw)` 返回完全相同的 `(low, high)`，说明 `LoggedArray` 暴露的 `__array_interface__` 直接复用了内部 ndarray 的缓冲区。
3. `opt_func_info` 对 `add` 的 `float64` 签名（`'ddd'`）返回非空，印证了日志里 `add` 这个 ufunc 确实是带 CPU 分发的优化函数。

**预期结果**：三个打印分别给出 `same buffer? True`、形如 `[('add', '__call__')]` 的日志、以及 `add dispatch keys: ['ddd']`（签名键随版本/平台可能略异，属「待本地验证」）。这个练习同时调用了本讲的三个工具，把「协议接入 → 内存内省 → CPU 优化确认」三条线连成了一条。

## 6. 本讲小结

- `opt_func_info` 从 C 层 `__cpu_targets_info__` 读取 ufunc 的 CPU 分发表，支持按函数名和签名（类型字符或类型名）双重正则过滤，是观察 numpy「为什么快」的窗口。
- `byte_bounds` 通过 `__array_interface__` 的 `data/strides/shape` 计算数组实际占用的字节范围；正步长抬高 `high`、负步长拉低 `low`，最后补上 `itemsize`，连续数组的 `high-low` 恰为 `size*itemsize`。
- `_binary_method` / `_numeric_methods` 等工厂函数把「调用某个 ufunc」封装成 Python 双下方法，是 `NDArrayOperatorsMixin` 用几十行覆盖约 40 个运算符的核心技巧。
- `_disables_array_ufunc` 实现 NEP-0013 的 opt-out：对方把 `__array_ufunc__` 设为 `None` 时返回 `NotImplemented`，交还 Python 的反射机制。
- `NDArrayOperatorsMixin` 自身不实现 `__array_ufunc__`，只负责「运算符 → ufunc」的接线；子类补上 `__array_ufunc__` 即可让 `a + 1` 等运算走自定义逻辑并保持类型不变。
- 三个工具的暴露方式各异：`opt_func_info`、`NDArrayOperatorsMixin` 直接写在公开模块；`byte_bounds` 则遵循 `_impl` 实现 + 薄模块 `array_utils.py` 再导出的分层（`u1-l2` 的模式）。

## 7. 下一步学习建议

- **深入 `__array_function__` 与 `__array_ufunc__` 的区别**：本讲的 mixin 走的是 ufunc 协议；而 `u1-l2` 讲的 `array_function_dispatch` 走的是 `__array_function__` 协议（NEP-18）。可对比阅读 `numpy/_core/overrides.py`，理解两类协议分别拦截「普通函数」与「ufunc」。
- **进入形状与维度操作**：掌握了「数组内省 + 协议接入」之后，后续 `u3` 单元（`expand_dims`、`stack/split` 等）会大量用到本讲再导出的 `normalize_axis_tuple`（与 `byte_bounds` 同在 `_array_utils_impl.py`），可以把这两讲连起来读。
- **步长进阶**：`byte_bounds` 处理的负步长/稀疏步长，在 `u5-l1` 的 `as_strided` / `sliding_window_view` 里会被推向极致——直接手工改写 `strides` 构造视图，届时可回顾本讲的边界计算作为安全基础。
- **继续阅读源码**：建议把 [tests/test_mixins.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_mixins.py) 整文件通读一遍，它用例覆盖了反射、就地、opt-out、子类优先、多输出 ufunc 等所有边界，是理解 `NDArrayOperatorsMixin` 行为的最佳索引。
