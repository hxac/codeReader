# 创建掩码数组的多种方式

## 1. 本讲目标

上一讲（u1-l1）我们建立了「掩码数组 = `data` + `mask` + `fill_value`」的整体印象，知道它能把坏值「贴标签」排除在运算之外。这一讲解决一个更具体的问题：**我手里有一个普通数组，怎么把它变成一个掩码数组？**

读完本讲后，你应当能够：

1. 用 `array` / `masked_array` 从零构造一个指定掩码和填充值的掩码数组，并知道它内部最终都走 `MaskedArray.__new__`。
2. 用 `masked_where` 按「条件」屏蔽元素，理解它是几乎所有 `masked_xxx` 便捷函数的共同基石。
3. 区分 `masked_equal`、`masked_values`、`masked_object` 三个「相等性屏蔽」函数各自的适用场景（整数精确相等 / 浮点近似相等 / 对象精确相等）。
4. 用 `masked_invalid` 一行屏蔽所有 `NaN`/`inf`，并理解它和 `masked_inside`/`masked_outside` 等区间/比较类快捷函数都是 `masked_where` 的「语法糖」。

本讲假设你已经完成 u1-l1（理解掩码数组三件套），会用 `numpy` 创建数组、写布尔条件。子类化的内部钩子（`__array_finalize__` 等）属于 u2-l2，本讲只在用到时一笔带过。

---

## 2. 前置知识

在动手之前，先回顾四个概念：

- **条件（condition）**：一个与原数组同形状的布尔数组。`True` 的位置就是要被屏蔽的位置。`numpy.ma` 用它来决定「哪些元素该贴上屏蔽标签」。
- **`nomask`**：`numpy.ma` 里的一个特殊单例，表示「没有任何元素被屏蔽」。它不是 `False`，而是一个 `bool(0)` 的标量对象，作用是**省内存**——全 `False` 的大布尔数组不必真存下来。它的定义见 [core.py:L87-L88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88)：
  ```python
  MaskType = np.bool
  nomask = MaskType(0)
  ```
- **视图（view）**：NumPy 里「换个角度看待同一块内存」的机制。`masked_where` 内部大量用 `.view()` 在不复制数据的前提下把一个普通 `ndarray`「升级」成 `MaskedArray`，这一点会在源码精读里看到。
- **填充值（fill_value）**：被屏蔽位置「对外展示」时用的占位值。整数默认是 `999999`，浮点默认是 `1e+20`。很多 `masked_xxx` 函数会顺手把 `fill_value` 设成触发屏蔽的那个值，方便你之后用 `filled()` 还原。

一句话：本讲所有函数，本质上都在回答同一个问题——**「给你一个数组，请把满足某条件的元素标记为屏蔽」**，区别只在于「条件」长什么样。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它是 `numpy.ma` 的地基：

| 文件 | 作用 |
| --- | --- |
| [`core.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py) | 约 7000 行的核心实现。本讲用到的全部构造函数（`array`、`masked_array`、`masked_where`、`masked_equal`、`masked_values`、`masked_object`、`masked_invalid`、`masked_inside`、`masked_outside` 以及 `masked_greater` 系列比较函数）都定义在这里，`MaskedArray` 类本体也在这里。 |

如果想在 Python 里确认某个函数确实来自 `core.py`，可以 `print(np.ma.masked_where.__module__)`，结果会是 `numpy.ma.core`（详见 u1-l2 讲过的 re-export 机制）。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 构造主干：`array` / `masked_array` 与 `MaskedArray.__new__`
2. `masked_where`：按条件屏蔽（所有便捷函数的基石）
3. 相等性屏蔽三兄弟：`masked_equal` / `masked_values` / `masked_object`
4. `masked_invalid` 与区间/比较类快捷函数

---

### 4.1 构造主干：`array` / `masked_array` 与 `MaskedArray.__new__`

#### 4.1.1 概念说明

如果你已经同时手里有「数据」「掩码」「填充值」三样东西，最直接的构造方式是调用 `ma.array(data, mask=..., fill_value=...)`。它是一个「快捷方式」，参数顺序按使用频率排过，方便你随手写。

而 `ma.masked_array(...)` 其实**就是 `MaskedArray` 类本身的别名**——也就是说 `masked_array(data, mask=...)` 等价于直接实例化 `MaskedArray(data, mask=...)`。这两条路最终都会进入同一个真正的构造逻辑：`MaskedArray.__new__`。

理解这一层关系很重要：`numpy.ma` 提供了一大堆 `masked_xxx` 函数，但**「最终造出一个掩码数组」这件事只发生在 `MaskedArray.__new__` 里**。其它函数要么直接调用它，要么调用 `masked_where`（而 `masked_where` 内部用 `.view(MaskedArray)` 走另一条同样合法的构造路径）。

#### 4.1.2 核心流程

`array` / `masked_array` 的构造可以概括为：

1. 接收 `data`、`mask`、`dtype`、`copy`、`fill_value`、`keep_mask`、`hard_mask`、`shrink`、`ndmin` 等参数。
2. 把 `data` 先变成一个普通 `ndarray`（必要时升维到 `ndmin`、转成 `dtype`）。
3. 处理 `mask`：没有传就根据 `keep_mask`/`shrink` 决定是沿用 `data` 自带的掩码还是清空；传了就把布尔/数组形式的 `mask` 调整成与 `data` 同形状、同掩码 dtype。
4. 把数据「视图」成 `MaskedArray` 类型，挂上 `_data`、`_mask`、`_fill_value`、`_baseclass` 等属性。

#### 4.1.3 源码精读

先看 `array` 函数本体——它真的是一个「薄壳」，只是把参数换了个顺序转发给 `MaskedArray`：[core.py:L6859-L6875](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6859-L6875)

```python
def array(data, dtype=None, copy=False, order=None,
          mask=nomask, fill_value=None, keep_mask=True,
          hard_mask=False, shrink=True, subok=True, ndmin=0):
    """Shortcut to MaskedArray. ..."""
    return MaskedArray(data, mask=mask, dtype=dtype, copy=copy,
                       subok=subok, keep_mask=keep_mask,
                       hard_mask=hard_mask, fill_value=fill_value,
                       ndmin=ndmin, shrink=shrink, order=order)
```

而 `masked_array` 就是一行别名：[core.py:L6856](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6856)

```python
masked_array = MaskedArray
```

真正的构造逻辑在 `MaskedArray.__new__`。先看它如何处理 `data` 并把它「升级」成 `MaskedArray` 视图：[core.py:L2882-L2908](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2882-L2908)

```python
def __new__(cls, data=None, mask=nomask, dtype=None, copy=False,
            subok=True, ndmin=0, fill_value=None, keep_mask=True,
            hard_mask=None, shrink=True, order=None):
    # Process data.
    copy = None if not copy else True
    _data = np.array(data, dtype=dtype, copy=copy,
                     order=order, subok=True, ndmin=ndmin)
    _baseclass = getattr(data, '_baseclass', type(_data))
    ...
    if isinstance(data, cls) and subok and not isinstance(data, MaskedConstant):
        _data = ndarray.view(_data, type(data))
    else:
        _data = ndarray.view(_data, cls)
```

这段做了两件事：先用 `np.array(...)` 把 `data` 转成普通 `ndarray`（注意 `copy = None if not copy else True`，所以 `copy=False` 时会尽量不复制）；再用 `ndarray.view(_data, cls)` 把它的类型「贴」成 `MaskedArray`（或其子类）。`view` 不复制数据，只换类型——这就是为什么掩码数组能把普通数组「原地升级」。

接着是 `mask` 的处理。掩码的 dtype 不是普通的 `bool`，而是用 `make_mask_descr` 根据 `_data.dtype` 推导出来的（结构化 dtype 会得到结构化的掩码 dtype，普通 dtype 则是 `bool`）：[core.py:L2917-L2967](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2917-L2967)

```python
        # Type of the mask
        mdtype = make_mask_descr(_data.dtype)
        if mask is nomask:
            # Case 1. : no mask in input.
            ...
        else:
            # Case 2. : With a mask in input.
            if mask is True and mdtype == MaskType:
                mask = np.ones(_data.shape, dtype=mdtype)
            elif mask is False and mdtype == MaskType:
                mask = np.zeros(_data.shape, dtype=mdtype)
            ...
            mask = np.array(mask, copy=copy, dtype=mdtype)
```

几个关键点：

- `mask=True`（标量）会被展开成「全屏蔽」数组，`mask=False` 展开成「全不屏蔽」数组。
- 传进来的 `mask` 最终都会被 `np.array(..., dtype=mdtype)` 统一成「与 data 同掩码 dtype、同形状」的布尔数组。
- 当 `mask is nomask`（默认值）时，走的是另一条分支，根据 `keep_mask` 决定是否沿用 `data` 自带的掩码——这正是 `array` 能「保留原掩码」的原因。

> 关于 `make_mask_descr` 的细节（结构化 dtype 如何得到结构化掩码）属于 u2-l1「掩码的内部表示」，本讲只需知道「mask 的 dtype 由 data 的 dtype 决定」即可。

#### 4.1.4 代码实践

**实践目标**：用 `array` 显式构造一个掩码数组，并对比它与 `masked_array`（即直接实例化 `MaskedArray`）是否等价。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 用 array 显式指定 mask 和 fill_value
a = ma.array([1, 2, 3, 4], mask=[0, 1, 0, 0], fill_value=-99)
print(a)                       # 看 data 与 mask
print(a.fill_value)            # 应为 -99

# 用 masked_array（= MaskedArray 的别名）做同样的事
b = ma.masked_array([1, 2, 3, 4], mask=[0, 1, 0, 0], fill_value=-99)
print(b)

# 验证两者确实走同一个类
print(type(a) is type(b))      # True
print(ma.masked_array is ma.MaskedArray)   # True
```

**需要观察的现象**：`a` 和 `b` 打印结果一致，第二个元素都显示为 `--`；`masked_array is MaskedArray` 为 `True`，证明 `masked_array` 就是类本身。

**预期结果**：`a` 和 `b` 的 `mask` 都是 `[False, True, False, False]`，`fill_value` 都是 `-99`，类型都是 `numpy.ma.core.MaskedArray`。

> 说明：本实践不依赖任何「待本地验证」的行为，可以直接在装有 NumPy 的环境运行。

#### 4.1.5 小练习与答案

**练习 1**：`ma.array([1, 2, 3], mask=True)` 会得到什么？为什么？

**参考答案**：得到一个**全部元素都被屏蔽**的掩码数组，打印为 `[--, --, --]`。因为在 `__new__` 的 Case 2 分支里，标量 `mask=True` 且掩码 dtype 为普通 `bool` 时，会被展开成 `np.ones(shape, dtype=bool)`，即每个位置都是 `True`。

**练习 2**：如果你把一个已经有掩码的 `MaskedArray` 当作 `data` 传给 `ma.array`，默认情况下它的掩码会丢失吗？

**参考答案**：不会。默认 `keep_mask=True`，`__new__` 在 `mask is nomask` 分支里会沿用 `data` 自带的掩码。只有显式传 `keep_mask=False` 才会清空/重置掩码。

---

### 4.2 `masked_where`：按条件屏蔽（所有便捷函数的基石）

#### 4.2.1 概念说明

`masked_where(condition, a)` 是 `numpy.ma` 里最重要的一个构造函数：**返回 `a` 的一个掩码版本，凡 `condition` 为 `True` 的位置都被屏蔽**。

它之所以是「基石」，是因为后面要讲的 `masked_equal`、`masked_greater`、`masked_inside`、`masked_invalid`……几乎全部是它的「语法糖」——把某种 `condition` 写好后再调用 `masked_where`。

它有两个值得专门记的特点：

1. **`condition` 和 `a` 可以不是同一个数组**。你可以「用 `a` 的条件去屏蔽 `b`」，只要二者形状一致。这在实际数据处理里非常实用（比如用温度传感器的故障标记去屏蔽同时刻的湿度数据）。
2. **它会合并 `a` 上已有的掩码**。如果 `a` 本身已经屏蔽了一些元素，`masked_where` 会把新条件屏蔽的元素与旧掩码「取或」，而不是覆盖。

#### 4.2.2 核心流程

`masked_where` 的执行过程（参见 [core.py:L1885-L2005](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1885-L2005)）：

1. 把 `condition` 用 `make_mask(...)` 规整成一个合法掩码。
2. 把 `a` 转成普通 `ndarray`（`copy` 控制是否复制）。
3. 校验 `condition` 与 `a` 的形状是否一致，不一致就抛 `IndexError`。
4. 如果 `a` 已经带掩码（`hasattr(a, '_mask')`），用 `mask_or(cond, a._mask)` 把新掩码与旧掩码合并；否则掩码就是 `cond`。
5. 用 `a.view(cls)` 把 `a` 升级成 `MaskedArray`（沿用 `a` 的子类类型），把合并后的掩码赋给 `result.mask`。
6. 处理 `copy=False` 的特殊路径：当 `a` 是 `MaskedArray` 但其掩码是 `nomask` 时，需要把结果掩码「同步」回 `a._mask`，保证就地修改生效。

#### 4.2.3 源码精读

`masked_where` 的实现主体非常紧凑，关键几行如下：[core.py:L1985-L2005](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1985-L2005)

```python
    # Make sure that condition is a valid standard-type mask.
    cond = make_mask(condition, shrink=False)
    a = np.array(a, copy=copy, subok=True)

    (cshape, ashape) = (cond.shape, a.shape)
    if cshape and cshape != ashape:
        raise IndexError("Inconsistent shape between the condition and the input"
                         f" (got {cshape} and {ashape})")
    if hasattr(a, '_mask'):
        cond = mask_or(cond, a._mask)
        cls = type(a)
    else:
        cls = MaskedArray
    result = a.view(cls)
    # Assign to *.mask so that structured masks are handled correctly.
    result.mask = _shrink_mask(cond)
    ...
    return result
```

逐行说明：

- `make_mask(condition, shrink=False)`：把任意「类布尔」输入规整成掩码；`shrink=False` 是为了保留全 `False` 的形状信息，后面再统一收缩。
- `mask_or(cond, a._mask)`：把「条件掩码」与「`a` 已有掩码」做按位或，对应逻辑「新屏蔽 ∪ 旧屏蔽」。这是 `masked_where` 不丢失原有掩码的关键。
- `result = a.view(cls)`：用 `.view()` 把 `a` 升级成 `MaskedArray`（或 `type(a)` 这个子类），不复制数据。
- `result.mask = _shrink_mask(cond)`：赋值给 `mask` property（而不是直接给 `_mask`），这样结构化掩码也能被正确处理，并在全 `False` 时收缩为 `nomask`。

一个常被忽略的细节在 `copy` 参数上：默认 `copy=True`，结果与原数组不共享内存；传 `copy=False` 则会就地改写 `a` 本身。docstring 里的示例清楚地展示了这种差异（修改结果会反向影响原数组）：[core.py:L1942-L1963](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1942-L1963)。

#### 4.2.4 代码实践

**实践目标**：体会 `masked_where` 的「条件与数据可以分离」以及「掩码合并」两个特性。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = np.arange(4)                       # [0, 1, 2, 3]

# 特性 1：用 a 的条件去屏蔽另一个数组 b
b = ['a', 'b', 'c', 'd']
print(ma.masked_where(a == 2, b))      # 屏蔽第 3 个，因为 a[2]==2

# 特性 2：合并已有掩码
m = ma.masked_where(a == 2, a)         # a 中 2 被屏蔽 -> [0, 1, --, 3]
n = ma.masked_where(m == 0, m)         # 再屏蔽等于 0 的位置
print(n)                               # [--, 1, --, 3]，两处屏蔽都保留
```

**需要观察的现象**：第一次调用把字符串数组 `b` 的第 3 个元素 `'c'` 屏蔽了，证明条件来自 `a` 而非 `b`；第二次调用结果同时屏蔽了位置 0（来自新条件 `m == 0`）和位置 2（来自 `m` 自带的掩码），没有覆盖。

**预期结果**：
```
masked_array(data=[0, 1, --, 3], ...)      # 注意：第一个打印的是 a==2 屏蔽 a，docstring 示例风格
masked_array(data=['a', 'b', --, 'd'], ...)
masked_array(data=[--, 1, --, 3], ...)
```

> 说明：上面 `ma.masked_where(a == 2, a)` 这一行其实和 docstring 第一个示例等价。实际运行时第一个 `print` 传入的是 `b`，请以你实际传入的数组为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `masked_where` 在 docstring 里反复提示「用浮点相等做条件时，请改用 `masked_values`」？

**参考答案**：因为浮点数直接用 `==` 比较不可靠（`0.1 + 0.2 != 0.3` 这类精度问题）。`masked_where(a == 0.1, a)` 很可能因为精度误差一个元素都屏蔽不到。`masked_values` 内部用的是 `np.isclose`（带容差），适合浮点场景。

**练习 2**：`masked_where` 内部用 `mask_or` 合并 `a` 的旧掩码，`mask_or` 的语义是什么？

**参考答案**：按位「或」——只要新条件或旧掩码任一处为 `True`，结果就为 `True`。也就是「新屏蔽的位置 ∪ 原本已屏蔽的位置」，绝不缩小已有掩码。

---

### 4.3 相等性屏蔽三兄弟：`masked_equal` / `masked_values` / `masked_object`

#### 4.3.1 概念说明

「把等于某个值的元素屏蔽掉」是最常见的需求，但「等于」这个词在不同数据类型下含义不同。`numpy.ma` 一次提供了三个函数：

| 函数 | 适用类型 | 「相等」的含义 | 是否设置 `fill_value` |
| --- | --- | --- | --- |
| `masked_equal(x, value)` | 整数等 | 精确相等（`==`） | 是，设为 `value` |
| `masked_values(x, value, rtol, atol)` | 浮点 | 近似相等（`isclose`，带容差） | 是，设为 `value` |
| `masked_object(x, value)` | 对象数组（`dtype=object`） | 对象精确相等（`==`） | 是，设为 `value` |

记住一句话：**整数用 `masked_equal`，浮点用 `masked_values`，对象用 `masked_object`**。三者最终都把 `fill_value` 设为 `value`，方便日后 `filled()` 还原。

#### 4.3.2 核心流程

- `masked_equal`：构造条件 `equal(x, value)`，调用 `masked_where`，再把结果的 `fill_value` 设为 `value`。
- `masked_values`：先把 `x` 用 `value` 填充（消除已有屏蔽位对比较的干扰），浮点类型用 `np.isclose(x, value, atol, rtol)` 生成掩码，非浮点类型退化为 `umath.equal`；最后用 `masked_array(...)` 装配，并按需 `shrink_mask`。
- `masked_object`：直接用 `umath.equal` 对 `_data` 做对象级比较，再与旧掩码 `mask_or` 合并。

#### 4.3.3 源码精读

`masked_equal` 是最薄的一层，几乎就是「调用 `masked_where` + 设置 `fill_value`」：[core.py:L2143-L2173](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2143-L2173)

```python
def masked_equal(x, value, copy=True):
    """..."""
    output = masked_where(equal(x, value), x, copy=copy)
    output.fill_value = value
    return output
```

注意它用的是 `equal(x, value)` 而不是 `x == value`——这里的 `equal` 是 `numpy.ma` 自己的掩码版 `equal` ufunc（在 u1-l1 里提到过「两个掩码数组比较结果仍是掩码数组」）。这就是 `masked_equal` 能正确处理「`x` 本身带掩码」的原因。

`masked_values` 多了一层「浮点近似」逻辑：[core.py:L2389-L2397](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2389-L2397)

```python
    xnew = filled(x, value)
    if np.issubdtype(xnew.dtype, np.floating):
        mask = np.isclose(xnew, value, atol=atol, rtol=rtol)
    else:
        mask = umath.equal(xnew, value)
    ret = masked_array(xnew, mask=mask, copy=copy, fill_value=value)
    if shrink:
        ret.shrink_mask()
    return ret
```

关键点：

- `filled(x, value)` 先用 `value` 把 `x` 里已屏蔽的位置填上，避免屏蔽位干扰比较（`filled` 定义在 [core.py:L631](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L631)）。
- 浮点用 `np.isclose`，默认 `rtol=1e-5, atol=1e-8`——这与 `numpy.isclose` 的默认容差一致。
- 非浮点（比如整数）退化为 `umath.equal`，此时 `masked_values` 行为与 `masked_equal` 相同。
- `shrink_mask()`：如果没有任何元素被屏蔽，把掩码收缩为 `nomask`（即打印出来是 `mask=False` 而不是一长串 `False`）。

`masked_object` 走的是另一条更直接的路径，针对 `dtype=object`：[core.py:L2317-L2324](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2317-L2324)

```python
    if isMaskedArray(x):
        condition = umath.equal(x._data, value)
        mask = x._mask
    else:
        condition = umath.equal(np.asarray(x), value)
        mask = nomask
    mask = mask_or(mask, make_mask(condition, shrink=shrink))
    return masked_array(x, mask=mask, copy=copy, fill_value=value)
```

它直接对底层 `_data` 做对象相等比较（`umath.equal`），再与旧掩码合并。注意它**不**像 `masked_values` 那样先 `filled`——因为对象数组的「填充」语义和数值数组不同，这里走的是更朴素的「比较 `_data`」路线。

#### 4.3.4 代码实践

**实践目标**：用三种方式屏蔽同一个值，验证三者掩码一致（这正是本讲义规格要求的实践任务）。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 准备一个同时含整数和近似浮点值的数组
x = np.array([1, 2, 2.0, 3, 2], dtype=float)

# 方式 A：masked_where 显式条件
a = ma.masked_where(x == 2, x)

# 方式 B：masked_equal（精确相等）
b = ma.masked_equal(x, 2)

# 方式 C：masked_values（近似相等，默认容差）
c = ma.masked_values(x, 2)

print("A:", a)
print("B:", b)
print("C:", c)
print("mask 相同吗？",
      np.array_equal(a.mask, b.mask),
      np.array_equal(b.mask, c.mask))
print("fill_value:", a.fill_value, b.fill_value, c.fill_value)
```

**需要观察的现象**：三种方式得到的 `mask` 都应为 `[False, True, True, False, True]`（位置 1、2、3 中的「值等于 2」被屏蔽，注意下标 1、2、4）；`fill_value` 都被设为 `2`（`masked_equal`/`masked_values` 会设，`masked_where` 不会——所以这里 `a.fill_value` 是默认的 `1e+20`，而 `b`、`c` 是 `2`，请留意这个差异）。

**预期结果**：

- 三者 `mask` 两两相等（`True True`）。
- `a.fill_value` 为 `1e+20`（`masked_where` 不改 `fill_value`），`b.fill_value` 与 `c.fill_value` 为 `2.0`。

> 这说明：**掩码可以一致，但 `fill_value` 不一定一致**——`masked_where` 不顺手设 `fill_value`，而另两者会。这是一个值得记住的差异。

#### 4.3.5 小练习与答案

**练习 1**：对 `x = np.array([1.0, 1.1, 2.0, 1.1])`，为什么 `masked_values(x, 1.1)` 能屏蔽两个 `1.1`，而 `masked_where(x == 1.1, x)` 不一定能？

**参考答案**：浮点数 `1.1` 在二进制下无法精确表示，`x == 1.1` 做的是逐位精确比较，可能因表示误差而漏掉某些「看起来相等」的元素。`masked_values` 用 `np.isclose`（`|a - b| <= atol + rtol*|b|`）做容差比较，能可靠地命中所有近似相等的元素。

**练习 2**：`masked_object` 和 `masked_equal` 都用 `umath.equal` 比较，它们的区别是什么？

**参考答案**：主要区别在**适用类型**和**是否取 `_data`**。`masked_equal` 经 `masked_where` 走，会处理掩码传播与形状校验，适合数值/整数数组；`masked_object` 直接对 `_data` 做对象级比较，专为 `dtype=object`（如字符串、自定义对象）设计，并且对「没屏蔽到任何元素」的情况会把掩码收缩为 `nomask`。

---

### 4.4 `masked_invalid` 与区间/比较类快捷函数

#### 4.4.1 概念说明

最后一组是「现成条件」的快捷函数，它们都把某种常见条件包好，最终委托给 `masked_where`：

- **`masked_invalid(a)`**：屏蔽所有 `NaN`/`inf`（即「非有限值」）。这是上一讲 u1-l1 提到的高频入口。
- **比较类**（`masked_greater` / `masked_greater_equal` / `masked_less` / `masked_less_equal` / `masked_not_equal`）：把 `>`、`>=`、`<`、`<=`、`!=` 这些条件包好。
- **区间类**（`masked_inside(x, v1, v2)` / `masked_outside(x, v1, v2)`）：屏蔽「落在区间内」或「落在区间外」的元素。

它们的共同点是：**你完全可以自己用 `masked_where` 写出来，但这些函数更短、更不容易写错**。

#### 4.4.2 核心流程

- 比较类：直接 `return masked_where(<比较>(x, value), x, copy=copy)`，一行搞定。
- `masked_invalid`：`condition = ~np.isfinite(a)`，调用 `masked_where`；额外保证返回的掩码不是 `nomask`（历史原因，见下文源码注释）。
- 区间类：先把 `v1, v2` 排序（`v1 <= v2`），用 `filled(x)` 把已屏蔽位置填上避免干扰比较，再生成 `(x>=v1)&(x<=v2)`（inside）或 `(x<v1)|(x>v2)`（outside）的条件，最后委托 `masked_where`。

#### 4.4.3 源码精读

先看比较类的典型实现（以 `masked_greater` 为例，其余四个结构完全一样）：[core.py:L2008-L2032](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2008-L2032)

```python
def masked_greater(x, value, copy=True):
    """..."""
    return masked_where(greater(x, value), x, copy=copy)
```

确实是「一行语法糖」。`greater` 同样是掩码版 ufunc，能正确处理带掩码输入。`masked_greater_equal`、`masked_less`、`masked_less_equal`、`masked_not_equal` 五个函数（[core.py:L2008-L2140](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2008-L2140)）都是这个套路。

再看 `masked_invalid`——它有一个很容易被忽略的细节：[core.py:L2428-L2434](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2428-L2434)

```python
    a = np.array(a, copy=None, subok=True)
    res = masked_where(~(np.isfinite(a)), a, copy=copy)
    # masked_invalid previously never returned nomask as a mask and doing so
    # threw off matplotlib (gh-22842).  So use shrink=False:
    if res._mask is nomask:
        res._mask = make_mask_none(res.shape, res.dtype)
    return res
```

要点：

- 条件就是 `~np.isfinite(a)`——「不是有限数」即 `NaN` 或 `±inf`。这正对应 u1-l1 里说的「`masked_invalid` 等价于 `masked_where(~np.isfinite(a), a)`」。
- 最后那段 `if res._mask is nomask` 是一个**兼容性补丁**：历史上 `masked_invalid` 即使没有屏蔽任何元素也返回一个「真实的全 `False` 数组」而不是 `nomask` 单例，后来一旦改成返回 `nomask` 就破坏了 `matplotlib`（issue gh-22842），于是又改回「强制不收缩」。这是「真实项目里 API 兼容性高于代码简洁性」的一个鲜活例子。`make_mask_none` 生成一个全 `False` 的真实掩码数组（定义见 [core.py:L1698](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1698)）。

最后看区间类。`masked_inside`：[core.py:L2210-L2214](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2210-L2214)

```python
    if v2 < v1:
        (v1, v2) = (v2, v1)
    xf = filled(x)
    condition = (xf >= v1) & (xf <= v2)
    return masked_where(condition, x, copy=copy)
```

注意两件事：

1. `v2 < v1` 时会自动交换，所以 `masked_inside(x, 0.3, -0.3)` 和 `masked_inside(x, -0.3, 0.3)` 等价——区间端点顺序无所谓。
2. 用 `filled(x)`（默认填充值）先填掉已屏蔽位置，再比较，避免屏蔽位干扰条件。`masked_outside`（[core.py:L2251-L2255](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2251-L2255)）结构完全对称，只是条件换成 `(xf < v1) | (xf > v2)`。

#### 4.4.4 代码实践

**实践目标**：用 `masked_invalid` 处理含 `NaN`/`inf` 的数据，并用比较/区间函数练习「现成条件」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 1) masked_invalid：屏蔽 NaN/inf
a = np.array([0.0, 1.0, np.nan, np.inf, 4.0])
m = ma.masked_invalid(a)
print("invalid:", m)
print("mask is nomask?", m.mask is ma.nomask)   # False（兼容性补丁）
print("mean:", m.mean())                          # 自动跳过屏蔽位

# 2) 比较类 + 区间类
x = np.array([0.31, 1.2, 0.01, 0.2, -0.4, -1.1])
print(">0:", ma.masked_greater(x, 0))             # 屏蔽正数
print("inside:", ma.masked_inside(x, -0.3, 0.3))  # 屏蔽 [-0.3, 0.3] 内
print("outside:", ma.masked_outside(x, -0.3, 0.3))# 屏蔽区间外
```

**需要观察的现象**：

- `masked_invalid` 把 `nan`、`inf` 两处屏蔽，`mean()` 自动跳过它们（结果约为 `1.666...`，即 `(0+1+4)/3`）。
- `m.mask is ma.nomask` 为 `False`——即使你屏蔽的数组里全是有限值，`masked_invalid` 也会返回一个真实的全 `False` 数组而非 `nomask` 单例（这就是上面那个兼容性补丁的效果）。
- `masked_inside` 与 `masked_outside` 的屏蔽位置互补，两者合并正好屏蔽全部元素。

**预期结果**：
```
invalid: [0.0, 1.0, --, --, 4.0]
mean: 1.6666666666666667
```

> 说明：`mean` 跳过屏蔽位的行为来自掩码版归约（属于 u2-l7），本讲只需观察「屏蔽后均值正确」这一现象。

#### 4.4.5 小练习与答案

**练习 1**：用一个普通数组（无 `NaN`/`inf`）调用 `masked_invalid`，结果的 `.mask` 是什么？是 `nomask` 吗？

**参考答案**：是一个全 `False` 的**真实布尔数组**（与 `data` 同形状），而**不是** `nomask` 单例。这正是 [core.py:L2432-L2433](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2432-L2433) 那段兼容性补丁强制的结果——为了不破坏 `matplotlib`，`masked_invalid` 永不返回 `nomask`。

**练习 2**：`masked_inside(x, 0.3, -0.3)` 和 `masked_inside(x, -0.3, 0.3)` 结果一样吗？为什么？

**参考答案**：一样。因为函数体第一步 `if v2 < v1: (v1, v2) = (v2, v1)` 会自动把端点排成 `v1 <= v2`，所以端点顺序不影响结果。这是区间类函数相对「手写 `masked_where`」的一个便利点。

---

## 5. 综合实践

把本讲的四条主干串起来，完成下面这个**小任务：给一组带缺失与异常的传感器读数「清洗」成掩码数组，并按区间屏蔽可疑值**。

**任务描述**：某温度传感器记录了一天的读数（单位 ℃），其中 `NaN` 表示掉线，超出合理范围（`> 50` 或 `< -20`）的视为故障读数。请：

1. 用 `ma.array` 把读数构造成掩码数组，并显式屏蔽掉线位（`NaN`）。
2. 用 `masked_outside` 进一步屏蔽不在 `[-20, 50]` 区间内的读数。
3. 用 `masked_values` 尝试屏蔽所有「近似等于 0」的读数（视为传感器零漂），观察掩码如何与前面合并。
4. 最后用 `compressed()` 取出所有未被屏蔽的有效读数，并 `print` 出来。

**参考实现**：

```python
import numpy as np
import numpy.ma as ma

raw = np.array([36.5, np.nan, 42.0, 0.0, 0.0001, 55.0, -30.0, 25.0])

# 1) 构造并屏蔽 NaN
nan_mask = ~np.isfinite(raw)
data = ma.array(raw, mask=nan_mask)
print("step1:", data)

# 2) 屏蔽区间外的故障读数（注意它会与已有掩码合并）
data = ma.masked_outside(data, -20, 50)
print("step2:", data)

# 3) 屏蔽近似为 0 的零漂（mask_or 合并）
data = ma.masked_values(data, 0, atol=1e-2)
print("step3:", data)

# 4) 取出有效读数
print("有效读数:", data.compressed())
```

**预期结果**：`step1` 屏蔽掉线位（`nan`）；`step2` 在此基础上再屏蔽 `55.0` 和 `-30.0`（区间外）；`step3` 又屏蔽 `0.0` 和 `0.0001`（近似 0）；`compressed()` 最终只留下 `[36.5, 42.0, 25.0]`。

这个练习综合用到了：`ma.array` 的显式构造（4.1）、`masked_where` 的掩码合并语义（4.2，被 `masked_outside`/`masked_values` 间接调用）、近似相等屏蔽（4.3）、区间类快捷函数与 `compressed` 还原（4.4 与 u1-l1）。

---

## 6. 本讲小结

- `ma.array` 与 `ma.masked_array`（=`MaskedArray` 的别名）是「显式构造」的两条主干，二者最终都进入 `MaskedArray.__new__`；后者负责把数据 `.view()` 成 `MaskedArray` 并规整 `mask` 的形状与 dtype。
- `masked_where(condition, a)` 是**几乎所有 `masked_xxx` 函数的基石**：它的两个关键特性是「条件可与数据分离」和「与旧掩码取或合并」（`mask_or`）。
- 「相等性屏蔽」要按类型选函数：整数用 `masked_equal`（精确），浮点用 `masked_values`（`isclose` 容差），对象数组用 `masked_object`（直接比较 `_data`）；三者都会把 `fill_value` 设为 `value`。
- `masked_invalid` = `masked_where(~np.isfinite(a), a)`，但它因兼容性（gh-22842）永不返回 `nomask`；比较类（`masked_greater` 等）与区间类（`masked_inside`/`masked_outside`）都是 `masked_where` 的一行式语法糖。
- 掩码一致不等于 `fill_value` 一致：`masked_where` 不改 `fill_value`，而 `masked_equal`/`masked_values`/`masked_object` 会把它设成触发屏蔽的值——这是实践中容易踩坑的差异。

---

## 7. 下一步学习建议

本讲你已经掌握了「如何造出一个掩码数组」。下一讲 **u1-l4「读取与提取：data、mask、fill_value」** 会从「读取」角度继续，讲清楚 `.data` / `.mask` / `.fill_value` 三个属性的精确语义，以及 `filled()`、`compressed()`、`getdata()`、`getmask()` 这些「把掩码数组变回普通数据」的手段。

如果你对本讲涉及的内部机制感兴趣，可以先记下两个方向，留到进阶层再看：

- `masked_where` 里反复出现的 `nomask`、`make_mask`、`mask_or`、`make_mask_descr` 等掩码构造/合并函数，会在 **u2-l1「掩码的内部表示与构造」** 系统讲解。
- `MaskedArray.__new__` 里用到的 `.view()` 与 `__array_finalize__` 钩子（决定切片/运算后掩码如何传播），会在 **u2-l2「MaskedArray 类与 ndarray 子类化机制」** 展开。

建议你现在动手把第 5 节的综合实践跑一遍，确认每个步骤的掩码都能按预期合并——这是检验你是否真正理解 `masked_where` 的最好方式。
