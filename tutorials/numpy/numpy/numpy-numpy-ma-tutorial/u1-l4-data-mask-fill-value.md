# 读取与提取：data、mask、fill_value

## 1. 本讲目标

上一讲我们学会了「用各种方式创建掩码数组」。本讲解决一个更基础的问题：**创建好以后，怎么把它里面的东西读出来？**

掩码数组在概念上是「三件套」：原始数据（data）、屏蔽标记（mask）、填充值（fill_value）。学完本讲你应当能够：

1. 用 `.data`、`.mask`、`.fill_value` 三个属性正确访问三大组成部分，并理解它们各自的类型与「是不是视图」。
2. 用 `filled()` 把屏蔽位换成填充值、得到一个**普通 ndarray**（保持原形状）。
3. 用 `compressed()` 直接丢掉所有屏蔽位、得到一个**一维普通 ndarray**。
4. 用模块级函数 `getdata` / `getmask` / `getmaskarray` 安全地从「可能是、也可能不是掩码数组」的对象里取数据与掩码。

> 本讲所有源码都来自 [core.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py)，HEAD 为 `b21650c4f6`。

## 2. 前置知识

在动手之前，先建立三个直觉。

**直觉一：掩码数组「自己就是」数据。**
普通 NumPy 数组把数值存在一段连续内存里。`MaskedArray` 继承自 `ndarray`（`class MaskedArray(ndarray)`），所以它**本身就是**这段内存的容器——数据并没有被复制到别处。因此 `.data` 并不是去某个「外部盒子」里取东西，而是「把掩码数组自身当作普通 ndarray 看一眼」。

**直觉二：mask 和 fill_value 才是「额外」存的东西。**
真正需要额外内存的只有 mask（一个同形状的布尔数组）。当整个数组没有任何屏蔽元素时，连这个布尔数组都省了——用单例 `nomask`（就是 `False`）表示。fill_value 只是一个标量。

**直觉三：「读出来」有两层含义。**
- 读**带标记**的原始信息：`.data`（含坏值）、`.mask`（屏蔽标记）、`.fill_value`（填充值）。这三者合起来完整描述了这个掩码数组。
- 读**不带标记**的「干净」数据：要么 `filled()`（保留形状、屏蔽位用值替换），要么 `compressed()`（丢掉屏蔽位、压成一维）。注意这二者返回的都**不再是 MaskedArray**。

本讲会反复用到上一讲（u1-l3）提到的 `masked_array`、`masked_where` 等构造函数。如果对 `nomask`、`fill_value` 的概念已经生疏，可先回顾 u1-l1。

## 3. 本讲源码地图

本讲只涉及一个文件，但会反复在「模块级函数」和「`MaskedArray` 方法」之间来回对照。

| 源码位置 | 作用 |
|---|---|
| [core.py:87-88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88) | 定义 `MaskType` 与 `nomask` 单例（即 `False`）。 |
| [core.py:260-277](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L260-L277) | `default_fill_value`，文档里给出了各 dtype 的默认填充值表。 |
| [core.py:720-769](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L720-L769) | 模块级 `getdata(a, subok=True)`。 |
| [core.py:1411-1468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1411-L1468) | 模块级 `getmask(a)`：返回掩码或 `nomask`。 |
| [core.py:1474-1525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1474-L1525) | 模块级 `getmaskarray(arr)`：永远返回同形状布尔数组。 |
| [core.py:3583-3595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3583-L3595) | `MaskedArray.mask` 属性（getter 返回视图）。 |
| [core.py:3762-3780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3762-L3780) | `_get_data` 方法及 `_data`、`data` 两个属性。 |
| [core.py:3792-3851](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3792-L3851) | `MaskedArray.fill_value` 属性（含 getter / setter）。 |
| [core.py:3857-3936](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3857-L3936) | `MaskedArray.filled(fill_value=None)` 方法。 |
| [core.py:3938-3972](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3938-L3972) | `MaskedArray.compressed()` 方法。 |
| [core.py:7275-7311](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7275-L7311) | 模块级 `compressed(x)`，转发到方法版。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：①三大属性；②`filled`；③`compressed`；④`getdata/getmask` 函数族。

---

### 4.1 三大属性：data / mask / fill_value

#### 4.1.1 概念说明

这是本讲的核心模块。一个 `MaskedArray` 对象对外暴露三样东西：

| 属性 | 含义 | 类型 | 典型表现 |
|---|---|---|---|
| `.data` | 全部原始数值（**含**被屏蔽的坏值） | `ndarray`（baseclass 视图） | `array([1, 2, 3, 4, 5])` |
| `.mask` | 屏蔽标记，`True` 表示该位被屏蔽 | 布尔 `ndarray` 或 `nomask` | `array([F, F, T, F, T])` |
| `.fill_value` | 屏蔽位「对外」用的填充值 | 标量 | `999999`（int）/ `1e20`（float） |

需要特别强调两点「反直觉」的事实：

1. **`.data` 返回的不是 MaskedArray，而是普通 ndarray。** 它是把掩码数组「换一个视角」看成它的 baseclass（通常是 `ndarray`）后的视图。所以 `type(a.data)` 是 `numpy.ndarray`，而非 `MaskedArray`。
2. **`.mask` 返回的是视图（view）。** 源码注释明确写道：返回视图是为了防止使用者改掉掩码的 dtype 和 shape，但仍允许你改其中的值。

另外要记住 `nomask` 是什么：它是模块顶部定义的一个单例，本质上就是布尔值 `False`。

#### 4.1.2 核心流程

读取三大属性时，内部大致是这样：

```text
a.data       →  ndarray.view(a, a._baseclass)   # 把自身当作 baseclass 看
a.mask       →  a._mask.view()                   # 掩码的视图（保护 shape/dtype）
a.fill_value →  若 a._fill_value is None：
                    生成默认值（按 dtype 查表）
                返回 a._fill_value
```

`fill_value` 是**懒加载**的：构造时不一定马上算出默认值，第一次访问 `.fill_value` 时如果 `_fill_value` 还是 `None`，才调用 `_check_fill_value(None, self.dtype)` 按数据类型查表生成。各 dtype 的默认填充值由 `default_fill_value` 的文档表给出。

#### 4.1.3 源码精读

**`nomask` 单例**——它就是 `False`，表示「没有掩码」：

[core.py:87-88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88) — 定义掩码的布尔类型和 `nomask` 单例：

```python
MaskType = np.bool
nomask = MaskType(0)
```

中文说明：`nomask` 就是 `MaskType(0)`，等价于布尔 `False`。当一个数组没有任何屏蔽元素时，`_mask` 被设成 `nomask`，从而省下整块布尔数组的内存。

**`.data` 与 `._data`**——它们其实是同一个 property：

[core.py:3762-3780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3762-L3780) — `_get_data` 方法及两个属性绑定：

```python
def _get_data(self):
    """Returns the underlying data, as a view of the masked array..."""
    return ndarray.view(self, self._baseclass)

_data = property(fget=_get_data)
data = property(fget=_get_data)
```

中文说明：`_data` 和 `data` 指向**同一个** getter `_get_data`，它通过 `ndarray.view(self, self._baseclass)` 把掩码数组自身「看作」它的 baseclass（一般是 `ndarray`）后返回。这就是为什么 `a.data` 是普通 ndarray——它是一次「视角转换」，而不是拷贝。

**`.mask`**——返回掩码的视图：

[core.py:3583-3595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3583-L3595) — `mask` 属性的 getter 与 setter：

```python
@property
def mask(self):
    """ Current mask. """
    # Return a view so that the dtype and shape cannot be changed in place
    # This still preserves nomask by identity
    return self._mask.view()

@mask.setter
def mask(self, value):
    self.__setmask__(value)
```

中文说明：getter 返回 `self._mask.view()`——一个视图。注释解释了用意：返回视图可以让使用者改掩码里的「值」，但无法通过这个视图改掉掩码的 dtype / shape（视图会阻止这类操作）。赋值 `a.mask = ...` 则走 setter，转交给 `__setmask__`（掩码设置逻辑，进阶层讲义会展开）。

**`.fill_value`**——懒加载 + 按类型查表：

[core.py:3823-3832](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3823-L3832) — `fill_value` 的 getter：

```python
if self._fill_value is None:
    self._fill_value = _check_fill_value(None, self.dtype)

# 临时绕过 str/bytes 标量不能用 () 索引的问题
if isinstance(self._fill_value, ndarray):
    return self._fill_value[()]
return self._fill_value
```

中文说明：只有当内部 `_fill_value` 为 `None` 时，才调用 `_check_fill_value(None, self.dtype)` 按当前 dtype 生成默认填充值并缓存。默认值的具体取值见下表。

[core.py:266-277](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L266-L277) — `default_fill_value` 文档中的默认填充值表：

```
===========  ========
datatype      default
===========  ========
bool         True
int          999999
float        1.e20
complex      1.e20+0j
object       '?'
string       'N/A'
StringDType  'N/A'
===========  ========
```

中文说明：这是「按 dtype 决定默认填充值」的规则。整数是 `999999`、浮点是 `1e20`、复数是 `1e20+0j`、布尔是 `True`、对象是 `'?'`、字符串是 `'N/A'`。这就是你打印一个新建 `ma.array` 时看到 `fill_value=999999` 的来源。

[core.py:3834-3851](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3834-L3851) — `fill_value` 的 setter（节选）：

```python
@fill_value.setter
def fill_value(self, value=None):
    target = _check_fill_value(value, self.dtype)
    ...
    _fill_value = self._fill_value
    if _fill_value is None:
        self._fill_value = target          # 首次：直接创建属性
    else:
        _fill_value[()] = target           # 已存在：就地填充（为了传播）
```

中文说明：给 `a.fill_value = v` 赋值时，若设为 `None` 会回到默认值；若 `_fill_value` 已存在，则**就地写入**（`[()] = target`）而非替换对象，这样在子类化/视图传播时引用关系不会被破坏。

#### 4.1.4 代码实践

**实践目标**：亲手验证三大属性的类型与「`.mask` 是视图」这一行为。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

x = ma.array([1, 2, 3, 4, 5], mask=[0, 0, 1, 0, 1], fill_value=-999)

print("data      :", x.data, type(x.data))
print("mask      :", x.mask, type(x.mask))
print("fill_value:", x.fill_value, type(x.fill_value))
```

**需要观察的现象**：

1. `x.data` 的内容是 `array([1, 2, 3, 4, 5])`——**注意第 3、5 个元素 3 和 5 依然在里面**，被屏蔽的值并没有被删掉。
2. `type(x.data)` 是 `numpy.ndarray`，不是 `MaskedArray`。
3. `x.mask` 是 `array([False, False, True, False, True])`，`x.fill_value` 是 `-999`。

**进一步验证 `.mask` 是视图**（修改它会影响真实掩码）：

```python
m = x.mask          # 拿到视图
m[0] = True         # 通过视图改第 0 位
print("修改后 x.mask :", x.mask)
print("修改后 x       :", x)
```

**预期结果**：因为 `m` 是 `self._mask` 的视图，`m[0] = True` 会让真实掩码的第 0 位也变 `True`，于是打印 `x` 时第 0 个元素会显示成 `--`（被屏蔽）。

> 待本地验证：以上输出基于 [core.py:3762-3780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3762-L3780) 与 [core.py:3583-3595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3583-L3595) 的源码语义推断；如运行结果与预期不符，请优先检查本机 NumPy 版本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `type(a.data)` 是 `numpy.ndarray` 而不是 `MaskedArray`？

**参考答案**：因为 `data` 属性的 getter `_get_data` 执行的是 `ndarray.view(self, self._baseclass)`，它把掩码数组「转换视角」成它的 baseclass（通常是 `ndarray`）后再返回，所以类型是普通 `ndarray`。见 [core.py:3762-3780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3762-L3780)。

**练习 2**：一个**完全没有屏蔽元素**的掩码数组，`a.mask` 会返回什么？

**参考答案**：返回 `nomask`（即 `False`）。此时内部 `_mask` 被设成单例 `nomask`，省下了整块布尔数组。可用 `a.mask == ma.nomask` 验证为 `True`。见 [core.py:87-88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88)。

**练习 3**：为什么新建一个 `ma.array([1,2,3])` 后，它的 `fill_value` 是 `999999` 而不是 `None`？

**参考答案**：`fill_value` 是懒加载属性。第一次访问时，由于内部 `_fill_value` 为 `None`，getter 会调用 `_check_fill_value(None, self.dtype)` 按整数 dtype 查 `default_fill_value` 表得到 `999999`。见 [core.py:3823-3832](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3823-L3832) 与 [core.py:266-277](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L266-L277)。

---

### 4.2 filled 方法：用填充值替换屏蔽位

#### 4.2.1 概念说明

`filled` 的作用是：**把所有被屏蔽的位置换成某个值，返回一个普通 `ndarray`。** 它**保持原数组的形状**，只是把屏蔽标记「抹平」成一个具体的数值。

关键性质：

- 返回值**不是** `MaskedArray`，而是 `ndarray`（源码 Notes 明确写了 "The result is **not** a MaskedArray!"）。
- 若**没有任何屏蔽元素**，则直接返回 `self._data`，**不做拷贝**——这是一个性能优化。
- `fill_value` 参数缺省时，使用数组自身的 `a.fill_value`。

它有一个**模块级函数版** `ma.filled(a, fill_value=None)` 和一个**方法版** `a.filled(fill_value=None)`，二者等价。

#### 4.2.2 核心流程

方法版 `MaskedArray.filled` 的决策流程：

```text
m = self._mask
├─ m is nomask ?                →  return self._data          # 完全无掩码，直接返回
├─ fill_value is None ?         →  fill_value = self.fill_value
├─ self is masked_singleton ?   →  return asanyarray(fill_value)
├─ 结构化 dtype ?               →  copy 数据，按字段递归填充 (_recursive_filled)
├─ m.any() == False ?           →  return self._data          # 有掩码对象但全 False，不拷贝
└─ 否则                         →  copy 数据，np.copyto(result, fill_value, where=m)
```

模块级函数 `ma.filled(a, fill_value)` 则更简单：优先委托给对象自己的 `a.filled` 方法；若 `a` 压根不是掩码数组（只是普通 `ndarray`），就直接原样返回。

#### 4.2.3 源码精读

**模块级 `filled`**——能处理「不是掩码数组」的输入：

[core.py:681-690](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L681-L690) — 模块级 `filled` 的主体：

```python
if hasattr(a, 'filled'):
    return a.filled(fill_value)        # 是掩码数组：委托方法版

elif isinstance(a, ndarray):
    return a                           # 普通 ndarray：原样返回
elif isinstance(a, dict):
    return np.array(a, 'O')
else:
    return np.array(a)
```

中文说明：这是「鸭子类型」处理。只要对象有 `filled` 方法就交给它；否则对普通 ndarray 原样返回（因为没屏蔽位需要填）。这让 `ma.filled` 可以安全地用在「可能是掩码数组、也可能是普通数组」的代码里。

**方法版 `MaskedArray.filled`**——真正的填充逻辑：

[core.py:3903-3923](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3903-L3923) — 方法版主体（节选关键分支）：

```python
m = self._mask
if m is nomask:
    return self._data                  # 分支 1：完全无掩码，零拷贝

if fill_value is None:
    fill_value = self.fill_value       # 分支 2：缺省则用数组自身填充值
else:
    fill_value = _check_fill_value(fill_value, self.dtype)

if self is masked_singleton:
    return np.asanyarray(fill_value)   # 分支 3：masked 单例

if m.dtype.names is not None:
    result = self._data.copy('K')      # 分支 4：结构化 dtype，递归填
    _recursive_filled(result, self._mask, fill_value)
elif not m.any():
    return self._data                  # 分支 5：掩码对象存在但全 False，零拷贝
else:
    result = self._data.copy('K')      # 分支 6：真正拷贝并替换
    try:
        np.copyto(result, fill_value, where=m)
    except (TypeError, AttributeError):
        ...
```

中文说明：核心替换发生在分支 6 的 `np.copyto(result, fill_value, where=m)`——把 `result` 中掩码为 `True` 的位置覆盖成 `fill_value`，其余位置保持原数据。分支 1 和分支 5 是两个「无屏蔽就零拷贝」的快路径。

#### 4.2.4 代码实践

**实践目标**：对比 `filled()` 在「缺省填充值」「指定填充值」下的输出，并确认返回类型。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

x = ma.array([1, 2, 3, 4, 5], mask=[0, 0, 1, 0, 1], fill_value=-999)

print(x.filled())              # 用自身 fill_value=-999
print(x.filled(fill_value=1000))   # 临时指定 1000
print(type(x.filled()))        # 确认返回类型
```

**预期结果**（取自 [core.py:3886-3892](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3886-L3892) 的官方示例）：

```
[   1    2 -999    4 -999]
[   1    2 1000    4 1000]
<class 'numpy.ndarray'>
```

**需要观察的现象**：第 3、5 位被分别换成 `-999` 和 `1000`，形状不变；`type(...)` 是 `numpy.ndarray` 而非 `MaskedArray`。

#### 4.2.5 小练习与答案

**练习 1**：`filled()` 的返回值是 `MaskedArray` 吗？

**参考答案**：不是。源码 Notes 明确写道 "The result is **not** a MaskedArray!"，返回的是普通 `ndarray`。见 [core.py:3879-3881](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3879-L3881)。

**练习 2**：一个**没有任何屏蔽元素**的掩码数组调用 `filled()`，会发生拷贝吗？

**参考答案**：不会。`m is nomask`（分支 1）或 `not m.any()`（分支 5）时直接 `return self._data`，零拷贝。见 [core.py:3903-3919](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3903-L3919)。

**练习 3**：为什么 `ma.filled(some_plain_ndarray)` 不会报错？

**参考答案**：模块级 `filled` 用鸭子类型——输入没有 `filled` 方法但属于 `ndarray` 时，直接原样返回该数组。见 [core.py:681-686](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L681-L686)。

---

### 4.3 compressed 方法：丢弃屏蔽位、压成一维

#### 4.3.1 概念说明

`compressed` 与 `filled` 是一对「互补」的提取手段：

| 方法 | 对屏蔽位的处理 | 形状 | 返回类型 |
|---|---|---|---|
| `filled()` | 用值**替换** | **保持原形状** | `ndarray` |
| `compressed()` | **直接丢弃** | **压成一维** | `ndarray` |

`compressed` 返回所有「未被屏蔽」的数据，按 C 顺序（行优先）拍平成一维。当你要对「有效数据」做统计（比如求均值、画直方图）而不关心原始位置时，用 `compressed` 最直接。

> ⚠️ 不要和 `compress(condition, axis)` 混淆。源码在 `compress` 的注释里专门提醒：`compressed` 返回的是**纯 ndarray、没有掩码**；`compress` 返回的仍是 `MaskedArray`、带有掩码。见 [core.py:4001-4003](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4001-L4003)。

#### 4.3.2 核心流程

```text
data = ndarray.ravel(self._data)                       # 先把数据拍平
if self._mask is not nomask:
    data = data.compress(logical_not(ravel(self._mask)))  # 只留 mask=False 的位置
return data
```

核心是 `np.logical_not(mask)`：把「掩码取反」得到「哪些位置有效」，再用 `ndarray.compress` 选出这些位置的数据。

#### 4.3.3 源码精读

[core.py:3968-3972](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3968-L3972) — `MaskedArray.compressed` 的全部实现：

```python
data = ndarray.ravel(self._data)
if self._mask is not nomask:
    data = data.compress(np.logical_not(ndarray.ravel(self._mask)))
return data
```

中文说明：只有两行逻辑。先把 `_data` 拍平；如果有掩码（不是 `nomask`），就用「掩码取反」作为条件，`compress` 出所有有效元素。注意这里用 `ndarray.ravel` / `ndarray.compress`（显式走基类方法），避免被 MaskedArray 自身的同名方法干扰。

模块级函数版只是转发：

[core.py:7311](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7311) — 模块级 `compressed`：

```python
return asanyarray(x).compressed()
```

中文说明：模块级 `ma.compressed(x)` 把输入转成掩码数组（若还不是），再调用方法版 `.compressed()`。

`compressed` 与 `compress` 的区别（来自 `compress` 方法的注释）：

[core.py:4001-4003](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4001-L4003)：

```
Please note the difference with :meth:`compressed` !
The output of :meth:`compress` has a mask, the output of
:meth:`compressed` does not.
```

#### 4.3.4 代码实践

**实践目标**：用 `compressed` 丢弃屏蔽位，并与 `filled` 对比「保持形状 vs 压成一维」的差异。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 二维数组，两个屏蔽位
x = ma.array([[1, 2], [3, 4]], mask=[[1, 0], [0, 1]])

print("filled    :", x.filled())      # 保持 2x2 形状
print("compressed:", x.compressed())  # 压成一维
print("shape 对比:", x.filled().shape, x.compressed().shape)
```

**预期结果**（取自 [core.py:3962-3966](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3962-L3966) 的官方示例）：

```
filled    : [[999999      2]
             [     3 999999]]
compressed: [2 3]
shape 对比: (2, 2) (2,)
```

**需要观察的现象**：`filled` 仍是 2×2（屏蔽位被填成 `999999`），`compressed` 变成长度 2 的一维数组 `[2, 3]`（只剩两个有效值）。

#### 4.3.5 小练习与答案

**练习 1**：一个 2×3 的掩码数组有 2 个屏蔽位，`compressed()` 返回几个元素？维度是多少？

**参考答案**：返回 4 个元素（总共 6 个减去 2 个屏蔽位），是一维数组。

**练习 2**：`compressed()` 和 `compress(condition)` 有什么本质区别？

**参考答案**：`compressed()` 返回**纯 ndarray、没有掩码**；`compress(condition)` 返回**仍是 MaskedArray、带掩码**。见 [core.py:4001-4003](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4001-L4003)。

**练习 3**：`ma.compressed(plain_ndarray)` 会发生什么？

**参考答案**：模块级 `compressed` 先 `asanyarray(x)` 再调用 `.compressed()`。普通 ndarray 的 `_mask` 为 `nomask`，所以不进入 compress 分支，直接返回拍平后的数据（一维）。

---

### 4.4 getdata / getmask / getmaskarray 函数族

#### 4.4.1 概念说明

属性 `.data` / `.mask` 很好用，但有一个前提：**你已经确定对象是 `MaskedArray`**。如果代码里拿到的是「可能是普通 ndarray、也可能是掩码数组」的通用输入，直接 `.mask` 在普通 ndarray 上会抛 `AttributeError`。

为此 core 提供了一组**模块级函数**，它们对任意输入都安全：

| 函数 | 返回 | 无掩码时 |
|---|---|---|
| `getdata(a)` | 底层数据 `ndarray` | 原样返回 |
| `getmask(a)` | 掩码，或 `nomask` | 返回 `nomask`（`False`） |
| `getmaskarray(arr)` | 永远是同形状布尔数组 | 返回**全 `False` 数组** |

`getmask` 与 `getmaskarray` 的区别是本模块的重点：

- `getmask(a)` 返回内部 `_mask` 对象**本身**（可能是 `nomask`）。适合「我只想知道有没有掩码」。
- `getmaskarray(arr)` **保证**返回一个和 `arr` 同形状的布尔数组。适合「我后面要用布尔数组做运算（比如 indexing），不想要 `nomask` 这种特殊情况」。

#### 4.4.2 核心流程

```text
getdata(a):
    try: return a._data
    except AttributeError: return np.array(a, subok=subok)

getmask(a):
    return getattr(a, '_mask', nomask)      # 没有 _mask 属性就当 nomask

getmaskarray(arr):
    mask = getmask(arr)
    if mask is nomask:
        mask = make_mask_none(shape(arr), dtype)   # 造一个全 False 的同形状数组
    return mask
```

#### 4.4.3 源码精读

**`getdata`**——优先取 `_data`，取不到就转 ndarray：

[core.py:763-769](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L763-L769) — `getdata` 主体：

```python
try:
    data = a._data
except AttributeError:
    data = np.array(a, copy=None, subok=subok)
if not subok:
    return data.view(ndarray)
return data
```

中文说明：对掩码数组直接取 `_data`；对普通对象用 `np.array` 转换。`subok=False` 时强制压成纯 `ndarray` 视图。注意 `getdata` 取的是 `_data`（即 `ndarray.view(self, baseclass)`），所以和 `.data` 属性等价。

**`getmask`**——一行实现，返回原始 `_mask` 引用：

[core.py:1468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1468) — `getmask` 主体：

```python
return getattr(a, '_mask', nomask)
```

中文说明：对象有 `_mask` 就返回它，否则返回 `nomask`。**注意它返回的是内部 `_mask` 的原始引用，而非视图**——这和 `.mask` 属性（返回 `self._mask.view()`）不同。

**`getmaskarray`**——保证同形状布尔数组：

[core.py:1522-1525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1522-L1525) — `getmaskarray` 主体：

```python
mask = getmask(arr)
if mask is nomask:
    mask = make_mask_none(np.shape(arr), getattr(arr, 'dtype', None))
return mask
```

中文说明：先用 `getmask` 拿掩码；如果是 `nomask`，就用 `make_mask_none(shape, dtype)` 造一个同形状、全 `False` 的布尔数组返回。这就保证了调用者永远拿到「形状一致、可直接用于运算」的布尔数组。

#### 4.4.4 代码实践

**实践目标**：体会 `getmask` 与 `getmaskarray` 在「无掩码」时的差异，以及它们对普通 ndarray 的容错。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

plain = np.array([[1, 2], [3, 4]])          # 普通 ndarray，不是掩码数组
masked_arr = ma.masked_equal([[1, 2], [3, 4]], 2)

# 1) 普通数组上调用：都不会报错
print("getmask(plain)       :", ma.getmask(plain))
print("getmaskarray(plain)  :", ma.getmaskarray(plain))

# 2) 无掩码的掩码数组
b = ma.masked_array([[1, 2], [3, 4]])
print("getmask(b)           :", ma.getmask(b), "  == nomask ?", ma.getmask(b) == ma.nomask)
print("getmaskarray(b)      :", ma.getmaskarray(b))

# 3) 有掩码的数组
print("getmask(masked_arr)  :", ma.getmask(masked_arr))
print("getdata(masked_arr)  :", ma.getdata(masked_arr))
```

**预期结果**：

- `getmask(plain)` → `False`（即 `nomask`，普通 ndarray 没有 `_mask` 属性）。
- `getmaskarray(plain)` → `[[False, False], [False, False]]`（同形状全 False）。
- `getmask(b)` → `False`，且 `== nomask` 为 `True`。
- `getmaskarray(b)` → `[[False, False], [False, False]]`。
- `getmask(masked_arr)` → `[[False, True], [False, False]]`。
- `getdata(masked_arr)` → `[[1, 2], [3, 4]]`。

**需要观察的现象**：`getmask` 在无掩码时返回标量 `False`（`nomask`），而 `getmaskarray` 永远返回同形状数组——这就是两者在后续运算中表现不同的根源。

> 待本地验证：上述输出依据 [core.py:1468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1468) 与 [core.py:1522-1525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1522-L1525) 的源码语义。

#### 4.4.5 小练习与答案

**练习 1**：对一个**完全无屏蔽**的掩码数组 `b`，`getmask(b)` 和 `getmaskarray(b)` 分别返回什么？

**参考答案**：`getmask(b)` 返回 `nomask`（即 `False`）；`getmaskarray(b)` 返回与 `b` 同形状的全 `False` 布尔数组。见 [core.py:1468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1468) 与 [core.py:1522-1525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1522-L1525)。

**练习 2**：`getmask(a)` 返回的对象与 `a.mask` 返回的对象有什么区别？

**参考答案**：`getmask(a)` 直接 `getattr(a, '_mask', nomask)`，返回内部 `_mask` 的**原始引用**；`a.mask` 属性返回 `self._mask.view()`，是一个**视图**（保护 dtype/shape 不被改）。两者底层指向同一块布尔数据。

**练习 3**：如果你后面要用掩码去做布尔索引 `a[mask]`，应该用 `getmask` 还是 `getmaskarray`？

**参考答案**：用 `getmaskarray`。因为它保证返回同形状布尔数组，不会出现 `nomask`（标量 `False`）导致索引失败的情况。`getmask` 适合只判断「有没有掩码」。

---

## 5. 综合实践

**任务背景**：假设你有 6 个温度传感器读数，其中第 3、5 个传感器故障（读数无效）。请用本讲学到的全部手段，把这套数据「读出来」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# 1) 构造带故障的读数：data 是原始值，mask 标出故障位
readings = ma.array([22.4, 23.1, 99.0, 21.8, 99.0, 20.9],
                    mask=[0, 0, 1, 0, 1, 0])

# 2) 用「属性」读三大组成
print("=== 三大属性 ===")
print("data      :", readings.data)        # 注意 99.0 还在里面
print("mask      :", readings.mask)
print("fill_value:", readings.fill_value)  # 浮点默认 1e20

# 3) 用 getdata / getmask / getmaskarray 函数族
print("\n=== 函数族 ===")
print("getdata       :", ma.getdata(readings))
print("getmask       :", ma.getmask(readings))
print("getmaskarray  :", ma.getmaskarray(readings))

# 4) 两种「去屏蔽」方式对比
print("\n=== filled vs compressed ===")
print("filled()    :", readings.filled(np.nan))   # 保持 6 个位置，故障位填 nan
print("compressed  :", readings.compressed())     # 丢掉故障位，剩 4 个有效读数

# 5) 真实用途：对有效读数求均值
valid = readings.compressed()
print("\n有效读数均值:", valid.mean())
print("全部读数均值 :", readings.mean())           # 掩码版 mean 会自动跳过屏蔽位
```

**需要观察与思考的现象**：

1. `readings.data` 里 `99.0` 仍在——掩码数组从不删数据，只贴标签。
2. `readings.fill_value` 是 `1e20`（浮点默认值，见默认填充值表）。
3. `filled(np.nan)` 保持 6 个位置，把故障位换成 `nan`；`compressed()` 只剩 4 个有效值、变一维。
4. `readings.mean()` 和 `valid.mean()` **结果相同**——掩码数组的统计方法会自动跳过屏蔽位（这正是 u1-l1 讲的「掩码数组的价值」）。

**进阶思考**：如果你要把这套读数喂给一个**只接受普通 ndarray、不认识掩码**的旧函数，该用 `filled()` 还是 `compressed()`？答案是看那个函数是否依赖「位置/形状」：依赖就用 `filled(np.nan)`（保持形状，让旧函数自己处理 nan），不依赖就用 `compressed()`。

> 待本地验证：第 4、5 步的数值结果需在本机运行确认；`readings.mean()` 会跳过屏蔽位这一行为会在 u2-l7「归约、统计与排序」中深入讲解。

## 6. 本讲小结

- **三大属性**：`.data`（baseclass 视图，普通 ndarray，含坏值）、`.mask`（视图，或 `nomask`）、`.fill_value`（按 dtype 懒加载，int→999999、float→1e20）。
- **`nomask`** 就是 `False`，是「没有掩码」的省内存单例，定义在 [core.py:87-88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88)。
- **`.mask` 返回视图**（保护 shape/dtype），而 `getmask()` 返回原始 `_mask` 引用——两者底层同源、用途不同。
- **`filled()`** 用值**替换**屏蔽位、**保持形状**，返回普通 `ndarray`；无屏蔽位时零拷贝。
- **`compressed()`** 直接**丢弃**屏蔽位、**压成一维**，返回普通 `ndarray`；注意它和带掩码的 `compress(condition)` 不同。
- **`getdata/getmask/getmaskarray`** 是对「任意输入」安全的模块级函数；`getmask` 可能返回 `nomask`，`getmaskarray` 永远返回同形状布尔数组。

## 7. 下一步学习建议

本讲你掌握了「读」掩码数组。接下来的进阶层（u2）会带你深入这些机制的内部：

1. **u2-l1 掩码的内部表示与构造**：本讲多次提到 `nomask`、`getmaskarray` 内部用的 `make_mask_none`，下一讲会系统讲 `make_mask_descr`（结构化 dtype 的掩码描述）、`mask_or`（合并两个掩码）等构造工具。
2. **u2-l3 填充值系统**：本讲只展示了 `default_fill_value` 的查表结果，u2-l3 会讲 `_check_fill_value` 校验、`minimum/maximum_fill_value`（极值填充值，掩码 `min/max` 运算会用）。
3. **u2-l7 归约、统计与排序**：本讲综合实践中 `readings.mean()` 能自动跳过屏蔽位——下一阶段会讲清掩码归约（`sum/mean/var`）和排序（`sort` 的 `endwith` 参数）究竟如何处理屏蔽元素。

建议在进入 u2 之前，先把本讲的「综合实践」完整跑一遍，确保你能凭直觉区分 `data / mask / fill_value`、`filled / compressed`、`getmask / getmaskarray` 这三组易混概念。
