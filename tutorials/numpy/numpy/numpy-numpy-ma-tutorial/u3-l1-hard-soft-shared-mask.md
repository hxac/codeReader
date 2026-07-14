# 硬掩码、软掩码与共享掩码

## 1. 本讲目标

本讲是「专家层」的第一讲，深入 `MaskedArray` 的三个「隐藏开关」：

- `hardmask`（硬/软掩码）——决定**赋值能否解除屏蔽**；
- `shrink_mask`——决定全为 `False` 的掩码**是否压缩为单例 `nomask`**；
- `sharedmask`（共享掩码）——决定两个掩码数组是否**共用同一个 `_mask` 对象**。

学完本讲，你应当能够：

1. 说清「硬掩码下被屏蔽的值无法被赋值还原」这一语义，并用源码解释其实现；
2. 熟练使用 `harden_mask` / `soften_mask` / `shrink_mask` 三个方法及其对应的模块级函数；
3. 理解 `_sharedmask` 标志的「身份」含义，知道何时两个数组的 `.mask` 指向同一块内存，以及如何用 `unshare_mask` 解绑。

本讲承接 u2-l1（掩码内部表示与 `nomask` 单例）与 u2-l2（`MaskedArray` 子类化钩子），如果你对 `_data` / `_mask` 双副本、`nomask` 身份判断还不熟悉，建议先复习这两讲。

## 2. 前置知识

在进入正题前，先回顾三个关键术语（前几讲已建立，这里只做一句话复习）：

- **三件套**：每个 `MaskedArray` 由 `_data`（含坏值）、`_mask`（同形布尔，`True` 表示屏蔽）、`_fill_value`（对外填充值）组成。
- **`nomask`**：代表「无屏蔽」的单例，实质是 `MaskType(0)`（即布尔 `False`）。全库用 `is nomask` 做 O(1) 身份判断，省下「分配一个全 `False` 数组」的内存与时间。
- **`_update_from` / `__array_finalize__`**：子类化钩子。切片、视图、`ufunc` 时，NumPy 会创建新的 `MaskedArray` 实例，并通过这些钩子决定新实例的 `_mask` 该如何继承。

本讲要回答的核心问题是：

> 当我们对一个 `MaskedArray` 做 `x[i] = v` 时，`_data` 和 `_mask` 会怎样改变？答案并不是唯一的——它取决于一个布尔标志 `_hardmask`。而当我们对一个视图 `y = x.view()` 写入时，改动会不会「漏」回原数组 `x`？这取决于另一个标志 `_sharedmask`。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 这一个文件，涉及的关键位置如下：

| 位置（行号） | 作用 |
|---|---|
| `core.py:2874` | 类属性 `_defaulthardmask = False`，硬掩码默认关闭 |
| `core.py:2882-3023` | `MaskedArray.__new__`，构造时设置 `_hardmask` 与 `_sharedmask` |
| `core.py:3025-3048` | `_update_from`，把 `_hardmask`/`_sharedmask` 等簿记属性搬运到新实例 |
| `core.py:3050-3142` | `__array_finalize__`，视图/切片/ufunc 后的 mask 继承启发式 |
| `core.py:3143-3160` | `__array_wrap__`，ufunc 后强制 `result._mask.copy()`（解绑共享） |
| `core.py:3406-3473` | `__setitem__`，按 `nomask`/软/硬三路分支写 data 与 mask |
| `core.py:3511-3573` | `__setmask__`，设置掩码时的软/硬差异 |
| `core.py:3620-3636` | `harden_mask` 方法 |
| `core.py:3638-3654` | `soften_mask` 方法 |
| `core.py:3656-3699` | `hardmask` 只读属性 |
| `core.py:3701-3717` | `unshare_mask` 方法 |
| `core.py:3719-3722` | `sharedmask` 只读属性 |
| `core.py:3724-3755` | `shrink_mask` 方法 |
| `core.py:1597-1604` | 模块级 `_shrink_mask`，压缩为 `nomask` 的真正实现 |
| `core.py:4899` | `put` 在硬掩码下的特殊处理 |
| `core.py:7106`、`7117` | 模块级函数 `harden_mask` / `soften_mask`（`_frommethod` 包装） |

测试用例位于 `numpy/ma/tests/test_core.py` 的 `test_hardmask`、`test_shrink_mask`、`test_hardmask_oncemore_yay` 等方法，本讲多处引用它们作为「权威行为」。

---

## 4. 核心概念与源码讲解

### 4.1 hardmask 硬掩码语义

#### 4.1.1 概念说明

默认情况下（**软掩码 soft mask**），`MaskedArray` 的「屏蔽」只是一种**临时标签**：只要给被屏蔽的位置赋一个确定值，该位置的 mask 就会被改写为 `False`，元素随之「解除屏蔽」（unmask）。

但在某些场景下我们不希望被屏蔽的值被「复活」——例如传感器一旦判定为失效就不应再被人工数据覆盖。`numpy.ma` 用 `hardmask`（硬掩码）满足这种需求：

- **软掩码**（默认）：`x[i] = v` 会同时改写 `_data[i]` 和 `_mask[i]`，赋一个确定值就解除屏蔽。
- **硬掩码**：`_mask[i]` 一旦为 `True` 就「锁死」，赋值**只能加屏蔽，不能减屏蔽**；已屏蔽位置的 `_data` 也不被新值覆盖。

`hardmask` 是**数组级的整体标志**（一个布尔值），不是逐元素的——整个数组要么硬、要么软。

#### 4.1.2 核心流程

赋值 `x[i] = v` 进入 `__setitem__` 后，按 `_mask` 是否为 `nomask` 与 `_hardmask` 取值分四路（见 4.1.3 源码）。硬掩码的语义可以用伪代码概括：

```
软掩码分支：
    _data[indx] = dval          # 直接覆盖数据
    _mask[indx] = mval          # 直接覆盖 mask（mval 为 False 即解除屏蔽）

硬掩码分支：
    mindx = _mask[indx] | mval  # 只能 OR，永远只增不减
    copyto(_data[indx], dval, where=~mindx)  # 仅写入「尚未屏蔽」的位置
    _mask[indx] = mindx
```

关键差别有两点：

1. **mask 的合并方式**：软用「赋值覆盖」（可改 `False`），硬用「`mask_or`」（只能改 `True`）。
2. **data 的写入范围**：软全量覆盖，硬用 `copyto(..., where=~mindx)` 跳过屏蔽位。

#### 4.1.3 源码精读

先看软掩码分支（[core.py:3450-3457](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3450-L3457)），这是最直接的一路——data 与 mask 都被原样覆盖：

```python
elif not self._hardmask:
    # Set the data, then the mask
    if (isinstance(indx, masked_array) and
            not isinstance(value, masked_array)):
        _data[indx.data] = dval
    else:
        _data[indx] = dval
        _mask[indx] = mval        # ← mval 为 False 时直接解除屏蔽
```

再看硬掩码分支（[core.py:3458-3473](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3458-L3473)），这是本模块的灵魂：

```python
else:
    if _dtype.names is not None:
        err_msg = "Flexible 'hard' masks are not yet supported."
        raise NotImplementedError(err_msg)      # ← 结构化 dtype 暂不支持硬掩码
    mindx = mask_or(_mask[indx], mval, copy=True)  # ← 只 OR，永不解除
    dindx = self._data[indx]
    if dindx.size > 1:
        np.copyto(dindx, dval, where=~mindx)    # ← 仅写入未屏蔽位
    elif mindx is nomask:
        dindx = dval
    _data[indx] = dindx
    _mask[indx] = mindx
```

注意 `mask_or` 在 u2-l1 已介绍：它用 `logical_or` 合并两掩码，**任何一侧为 `True` 结果即为 `True`**，所以硬掩码下的赋值永远无法把 `True` 改回 `False`。`copyto(..., where=~mindx)` 则保证屏蔽位的数据「原封不动」。

同样的软/硬差异也体现在 `__setmask__` 中（[core.py:3530-3532](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3530-L3532)）：

```python
# Hardmask: don't unmask the data
if self._hardmask:
    current_mask |= mask          # ← 硬掩码：OR
# Softmask: set everything to False
elif isinstance(mask, (int, float, np.bool, np.number)):
    current_mask[...] = mask      # ← 软掩码：覆盖
```

`put` 方法在硬掩码下也有特殊处理（[core.py:4898-4905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4898-L4905)）：先把落在屏蔽数据上的下标/值过滤掉，再交给底层 `ndarray.put`，保证「硬掩码下的 put 不会复活屏蔽位」。

#### 4.1.4 代码实践

源码阅读型实践：用 `test_core.py` 的 `test_hardmask` 验证软/硬差异。

**实践目标**：亲眼看到「硬掩码下 `xh[:] = 1` 不改变被屏蔽位的数据与 mask，软掩码下 `xs[:] = 1` 把所有 mask 清零」。

**操作步骤**（在 Python 解释器中执行）：

```python
import numpy as np
from numpy.ma import arange, array, make_mask, filled

d = np.arange(5)
m = make_mask([0, 0, 0, 1, 1])          # 第 3、4 位屏蔽
xh = array(d, mask=m, hard_mask=True)   # 硬掩码
xs = array(d, mask=m, hard_mask=False, copy=True)  # 软掩码

xh[:] = 1
xs[:] = 1
print("xh._data =", xh._data)          # 预期 [0 1 1 3 4]，屏蔽位未被覆盖
print("xh._mask =", xh._mask)          # 预期 [1 0 0 1 1]，mask 只增不减
print("xs._data =", xs._data)          # 预期 [1 1 1 1 1]，全量覆盖
print("xs._mask =", xs._mask)          # 预期 nomask（即 False），全部解除
```

**预期结果**：

- `xh._data == [0, 1, 1, 3, 4]`，`xh._mask == [1, 0, 0, 1, 1]`（与 `test_core.py:2132-2136` 一致）；
- `xs._data == [1, 1, 1, 1, 1]`，`xs._mask is nomask`（与 `test_core.py:2134-2137` 一致）。

**需要观察的现象**：注意第 0 位 `xh` 原本未屏蔽（mask 为 0），但在前一步 `xh[0] = masked` 后才变为 1；赋值 `xh[:] = 1` 之后第 0 位 mask 仍是 1，且 `_data[0]` 仍是 0——这正说明「屏蔽一旦建立就锁死」。

#### 4.1.5 小练习与答案

**练习 1**：在硬掩码数组 `xh` 上执行 `xh[[1, 4]] = [10, 40]`（其中第 4 位是屏蔽位），`xh._data[4]` 会变成 40 还是保持原值？为什么？

**参考答案**：保持原值 4，不会变成 40。因为硬掩码分支用 `copyto(dindx, dval, where=~mindx)` 写入，第 4 位的 `mindx` 为 `True`，`~mindx` 为 `False`，故该位被跳过。这正是 `test_core.py:2116-2118` 断言 `xh._data == [0, 10, 2, 3, 4]` 的原因——索引 1 未屏蔽故更新为 10，索引 4 屏蔽故不变。

**练习 2**：为什么硬掩码分支遇到结构化 dtype（`_dtype.names is not None`）直接抛 `NotImplementedError`？

**参考答案**：因为 `copyto(..., where=...)` 的 `where` 参数对结构化 dtype 的逐字段掩码难以正确表达——结构化数组需要「同位置不同字段独立屏蔽」，简单的 `~mindx` 布尔掩码无法刻画。源码注释 `Flexible 'hard' masks are not yet supported.`（[core.py:3463](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3463)）明确这是未实现功能，而非刻意设计。

---

### 4.2 harden_mask / soften_mask / hardmask 属性

#### 4.2.1 概念说明

`_hardmask` 是一个普通布尔属性，存在每个 `MaskedArray` 实例上。提供三套与之相关的对外接口：

| 名称 | 类型 | 作用 |
|---|---|---|
| `hardmask` | 只读 property | 读取当前是否为硬掩码 |
| `harden_mask()` | 方法 | 设 `_hardmask = True`，返回 `self` |
| `soften_mask()` | 方法 | 设 `_hardmask = False`，返回 `self` |
| `np.ma.harden_mask(x)` | 模块级函数 | 转发到方法 |
| `np.ma.soften_mask(x)` | 模块级函数 | 转发到方法 |

注意 `harden_mask` / `soften_mask` 是**就地修改并返回自身**的方法（不是返回新数组），因此可以链式调用：`x.harden_mask()[i] = v`。

#### 4.2.2 核心流程

三个接口的实现都极其简单：

```
hardmask (getter):       return self._hardmask
harden_mask():           self._hardmask = True;  return self
soften_mask():           self._hardmask = False; return self
```

真正「起作用」的是 `_hardmask` 在 `__setitem__` / `__setmask__` / `put` 等写操作中被读取（见 4.1.3）。切换标志本身不改变现有 `_data` 与 `_mask`，只影响**之后**的写操作语义。

默认值由类属性 `_defaulthardmask = False` 决定（[core.py:2874](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2874)），构造时若未指定 `hard_mask` 参数，则从源数据继承（见 4.2.3）。

#### 4.2.3 源码精读

`harden_mask` 与 `soften_mask` 方法体（[core.py:3620-3654](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3620-L3654)）各只有两行：

```python
def harden_mask(self):
    """Force the mask to hard, preventing unmasking by assignment. ..."""
    self._hardmask = True
    return self

def soften_mask(self):
    """Force the mask to soft (default), allowing unmasking by assignment."""
    self._hardmask = False
    return self
```

`hardmask` 是只读 property（[core.py:3656-3699](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3656-L3699)），其 docstring 自带一个完整对比示例，清楚地展示了「软掩码下 `m[8] = 42` 解除屏蔽；硬掩码后 `m[:] = 23` 不动屏蔽位」。

构造时 `_hardmask` 的来源在 `__new__`（[core.py:3018-3021](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3018-L3021)）：

```python
if hard_mask is None:
    _data._hardmask = getattr(data, '_hardmask', False)   # ← 从源数据继承
else:
    _data._hardmask = hard_mask                            # ← 显式指定优先
```

所以从已有硬掩码数组派生的新数组默认仍是硬掩码；同理 `_update_from`（[core.py:3041](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3041)）在视图/ufunc 时也搬运此属性：

```python
_dict = {'_fill_value': getattr(obj, '_fill_value', None),
             '_hardmask': getattr(obj, '_hardmask', False),   # ← 继承
             ...
```

模块级函数是 `_frommethod` 包装（[core.py:7106](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7106) 与 [core.py:7117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7117)），等价于「取数组 → 调方法 → 返回」：

```python
harden_mask = _frommethod('harden_mask')
soften_mask = _frommethod('soften_mask')
```

#### 4.2.4 代码实践

这是本讲的**主实践任务**（对应大纲 practice_task）。

**实践目标**：构造掩码数组并 `harden_mask`，尝试给被屏蔽位置赋值，观察 data 与 mask 的变化；再 `soften_mask` 后重复，对比差异。

**操作步骤**：

```python
import numpy as np

a = np.ma.array([1, 2, 3, 4, 5], mask=[0, 0, 1, 0, 1])

# —— 硬掩码 ——
a.harden_mask()
print("hardmask?", a.hardmask)          # True
a[2] = 99                               # 试图给屏蔽位赋值
print("硬掩码下 a._data =", a._data)    # 看 [2] 是否变 99
print("硬掩码下 a._mask =", a._mask)    # 看 [2] 的 mask 是否还是 True

# —— 软掩码 ——
a.soften_mask()
print("hardmask?", a.hardmask)          # False
a[2] = 99                               # 再次给屏蔽位赋值
print("软掩码下 a._data =", a._data)    # 看 [2] 是否变 99
print("软掩码下 a._mask =", a._mask)    # 看 [2] 的 mask 是否变 False
```

**预期结果**：

- 硬掩码下：`a._data` 仍为 `[1, 2, 3, 4, 5]`（第 2 位未被覆盖），`a._mask` 仍为 `[False, False, True, False, True]`（未解除）。
- 软掩码下：`a._data` 变为 `[1, 2, 99, 4, 5]`，`a._mask` 变为 `[False, False, False, False, True]`（第 2 位被解除屏蔽，第 4 位因未操作仍屏蔽）。

**需要观察的现象**：`harden_mask()` 与 `soften_mask()` 的返回值就是 `a` 本身（`b = a.harden_mask()` 后 `b is a` 为 `True`，参见 `test_core.py:2173-2185` 的 `test_hardmask_oncemore_yay`）。

#### 4.2.5 小练习与答案

**练习 1**：执行 `b = np.ma.harden_mask(a)` 后，`b` 与 `a` 是同一个对象吗？修改 `b[0]` 会影响 `a` 吗？

**参考答案**：是同一个对象。`harden_mask` 就地修改 `self._hardmask` 并 `return self`，模块级 `np.ma.harden_mask(a)` 只是把数组传给方法，因此 `b is a`。修改 `b[0]` 必然影响 `a`。`test_core.py:2177-2181` 正是用 `assert_equal(a, b)` 与 `b[0] = 0` 后再比较来验证这一点。

**练习 2**：若希望得到一个**硬掩码的副本**而不影响原数组，应该怎么写？

**参考答案**：先复制再硬化：`b = a.copy().harden_mask()`。直接 `b = a.harden_mask()` 只会返回 `a` 本身，不会产生副本。

---

### 4.3 shrink_mask 与 nomask

#### 4.3.1 概念说明

回顾 u2-l1：`nomask` 是省内存的「无屏蔽」单例。但当用户对一个 `nomask` 数组执行 `x[1] = masked` 后又 `x[1] = 5`（解除屏蔽），`_mask` 会被实例化为一个全 `False` 的真实数组——此时它**逻辑上等价于 `nomask`**，却白白占用内存。

`shrink_mask()` 的作用就是「凡能压缩回 `nomask` 就压缩」：检查 `_mask` 是否全为 `False`，若是则替换为 `nomask`。它对结果没有语义影响，纯粹是**空间优化**。

注意一个限制：结构化 dtype 的掩码**不能** shrink（见 4.3.4），因为其 `_mask` 是复合 dtype，`.any()` 的语义不直观。

#### 4.3.2 核心流程

```
_shrink_mask(m):
    if (m 是普通 dtype) and (not m.any()):   # 全 False
        return nomask
    else:
        return m                              # 结构化或含 True，原样返回
```

判定「能否压缩」只看两点：dtype 无字段名（`m.dtype.names is None`）且 `m.any()` 为 `False`（没有任何元素为 `True`）。

#### 4.3.3 源码精读

真正的实现在模块级私有函数 `_shrink_mask`（[core.py:1597-1604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1597-L1604)）：

```python
def _shrink_mask(m):
    """
    Shrink a mask to nomask if possible
    """
    if m.dtype.names is None and not m.any():
        return nomask
    else:
        return m
```

方法 `shrink_mask`（[core.py:3724-3755](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3724-L3755)）只是转发并返回 `self`：

```python
def shrink_mask(self):
    """Reduce a mask to nomask when possible. ..."""
    self._mask = _shrink_mask(self._mask)
    return self
```

`_shrink_mask` 在库内多处被复用，例如 `make_mask`（[core.py:1804](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1804)）和 `masked_where`（[core.py:2000](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2000) `result.mask = _shrink_mask(cond)`）在构造掩码后会尝试压缩，可见「能 shrink 就 shrink」是一条贯穿全库的内存策略。

#### 4.3.4 代码实践

**实践目标**：验证 `shrink_mask` 把全 `False` 掩码压缩为 `nomask`，且对结构化 dtype 是 no-op。

**操作步骤**：

```python
import numpy as np

# 普通 dtype：先制造一个全 False 的真实掩码，再压缩
a = np.ma.array([1, 2, 3], mask=[0, 0, 0])
print("压缩前 a.mask =", a.mask)        # array([False, False, False])
b = a.shrink_mask()
print("b is a?", b is a)                 # True
print("压缩后 a.mask =", a.mask)         # False（即 nomask）
print("a.mask is np.ma.nomask?", a.mask is np.ma.nomask)  # True

# 结构化 dtype：shrink 是 no-op
c = np.ma.array([(1, 2.0)], dtype=[('a', int), ('b', float)])
before = c.mask
c.shrink_mask()
print("结构化 shrink 后 mask 不变?", np.ma.testutils.assert_equal(c.mask, before) or True)
```

**预期结果**：普通 dtype 压缩后 `a.mask is nomask` 为 `True`；结构化 dtype 的 mask 前后不变（与 `test_core.py:2199-2210` 的 `test_shrink_mask` 一致，注释明确写 `# Mask cannot be shrunk on structured types, so is a no-op`）。

**需要观察的现象**：注意 `shrink_mask()` 同样返回 `self`（`b is a` 为 `True`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_shrink_mask` 要用 `m.dtype.names is None` 作为前提条件，而不是直接 `not m.any()`？

**参考答案**：对结构化 dtype，`_mask` 是复合 dtype（如 `[('a','?'),('b','?')]`），其 `.any()` 会对所有字段的所有元素求「任一为 True」，语义是把整条记录当屏蔽判断——这并非「无屏蔽」的正确判据（字段级屏蔽的细节见 u3-l5 mrecords）。为了避免误把字段级屏蔽压缩掉，源码直接禁止结构化掩码 shrink。

**练习 2**：下面代码两次打印 `a.mask`，分别是 `nomask` 还是数组？

```python
a = np.ma.arange(10)
a[1] = np.ma.masked      # 制造屏蔽
a[1] = 1                 # 解除屏蔽
print(a.mask)            # (1)
a.shrink_mask()
print(a.mask)            # (2)
```

**参考答案**：(1) 是一个全 `False` 的真实数组 `array([False, False, ...])`——因为赋屏蔽操作把 `nomask` 实例化成了数组，解除屏蔽只是把对应位改回 `False`，不会自动 shrink；(2) 是 `nomask`（即 `False`），`shrink_mask` 检测到全 `False` 后压缩回单例。这正是 `test_core.py:2187-2197` 的 `test_smallmask` 探讨的行为。

---

### 4.4 sharedmask / unshare_mask / _sharedmask

#### 4.4.1 概念说明

`MaskedArray` 的 `_mask` 是一个普通的 ndarray 对象，多个 `MaskedArray` **完全可以指向同一个 `_mask` 对象**——就像两个 ndarray 视图共享同一块数据内存。当共享发生时，改一个数组的 mask 会「漏」到另一个数组。

`_sharedmask` 标志记录「当前数组的 `_mask` 是否可能被他人共享」。它是一个**性能提示/安全提示**，而非严格的共享计数器：

- `_sharedmask = True`：表示「我的 `_mask` 可能被别的数组引用着，**别就地改我**，要改就先 `copy()`」；
- `_sharedmask = False`：表示「我的 `_mask` 是我私有的，可以放心就地修改」。

`unshare_mask()` 的职责就是：若当前是共享状态，就复制一份独立 `_mask` 并把标志置 `False`，从而安全地独占这份掩码。

#### 4.4.2 核心流程

构造与视图阶段，`_sharedmask` 被这样设置：

```
__new__ (无新 mask, copy=False):  _sharedmask = True   # 与源数据共享视图
__new__ (有新 mask 或 copy=True): _sharedmask = False  # mask 是新建/复制的
__getitem__ 取结构化字段:          _sharedmask = True   # 子视图与父共享
__array_wrap__ (ufunc):           result._mask.copy()  # 强制解绑，独立
```

需要修改 mask 时（如 `__setitem__` 的若干分支），代码会先用 `unshare_mask()` 思路确保独立。`unshare_mask` 本身的逻辑：

```
unshare_mask():
    if self._sharedmask:
        self._mask = self._mask.copy()   # 复制一份
        self._sharedmask = False          # 标记为私有
    return self
```

注意 `unshare_mask` **仅在确实共享时才复制**，避免无谓拷贝（文档原话：*A copy of the mask is only made if it was shared.*）。

#### 4.4.3 源码精读

`unshare_mask` 方法（[core.py:3701-3717](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3701-L3717)）：

```python
def unshare_mask(self):
    """Copy the mask and set the `sharedmask` flag to ``False``. ..."""
    if self._sharedmask:
        self._mask = self._mask.copy()
        self._sharedmask = False
    return self
```

`sharedmask` 只读 property（[core.py:3719-3722](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3719-L3722)）：

```python
@property
def sharedmask(self):
    """ Share status of the mask (read-only). """
    return self._sharedmask
```

`_sharedmask` 在 `__new__` 中多处被设置。无新掩码且 `copy=False` 时设为 `True`（共享源数据的 mask 视图，[core.py:2943](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2943)）：

```python
else:
    _data._sharedmask = not copy        # copy=False → True（共享）
    if copy:
        _data._mask = _data._mask.copy()
```

有新掩码传入时，合并路径下设为 `False`（[core.py:3006-3009](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3006-L3009)），因为 `logical_or` 产生了全新数组：

```python
else:
    ...
    _data._mask = np.logical_or(mask, _data._mask)
    _data._sharedmask = False
```

`_update_from` 在视图/ufunc 派生新实例时搬运此标志（[core.py:3042](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3042)），**默认取 `False`**（保守策略）：

```python
'_sharedmask': getattr(obj, '_sharedmask', False),
```

`__getitem__` 取结构化字段时，子视图与父数组共享 mask（[core.py:3394-3398](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3394-L3398)）：

```python
# Update the mask if needed
if mout is not nomask:
    # set shape to match that of data; this is needed for matrices
    dout._mask = reshape(mout, dout.shape)
    dout._sharedmask = True             # ← 字段子视图共享父 mask
```

而 ufunc 后的 `__array_wrap__` 则**强制解绑**（[core.py:3156-3157](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3156-L3157)），用 `result._mask.copy()` 切断与输入 mask 的引用关系，保证运算结果可安全修改：

```python
if context is not None:
    result._mask = result._mask.copy()  # ← ufunc 结果 mask 必然独立
```

理解 `_sharedmask` 的钥匙：它**不保证**「`True` 一定有人在共享」，只是提醒「可能有人在共享，修改前先 copy」；而 `False` **保证**「此刻可安全就地修改」。

#### 4.4.4 代码实践

源码阅读型实践（无法纯靠运行验证「内存共享」，但可验证行为与 `unshare_mask` 效果）。

**实践目标**：体会 `_sharedmask` 标志在 `.copy()` 与 `unshare_mask()` 前后的变化。

**操作步骤**：

```python
import numpy as np

a = np.ma.array([1, 2, 3], mask=[1, 0, 0])
print("a._sharedmask =", a._sharedmask)   # 构造时有 mask，需观察实际值

# 取视图：通过切片产生的新 MaskedArray 会走 __array_finalize__
b = a[:]                                   # 整体切片视图
print("b._sharedmask =", b._sharedmask)

# 做一次 ufunc：__array_wrap__ 强制 copy
c = a + 0
print("c._sharedmask =", c._sharedmask)   # ufunc 结果 mask 独立

# 演示 unshare_mask
d = a.copy()
d.unshare_mask()
print("d._sharedmask =", d._sharedmask)   # False
print("a 与 a.copy() 的 _mask 地址不同?", id(a._mask) != id(d._mask))
```

**需要观察的现象**：

- `a._sharedmask`：构造时显式传入 `mask` 数组，按 [core.py:2991](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2991) `_data._sharedmask = not copy`，`copy` 默认 `False`（`None`），故结果与是否复制有关；记录你机器上的实际值。
- `c._sharedmask`：ufunc 结果因 `__array_wrap__` 调用了 `result._mask.copy()`，但 `_update_from` 默认从 `self` 继承（`getattr(obj,'_sharedmask',False)`）——记录实际值并对照源码解释。**待本地验证**：不同 NumPy 版本下 `_sharedmask` 的精确传播可能略有差异，关键是理解「`copy()` 切断共享」这一设计意图。
- 调用 `unshare_mask()` 后 `_sharedmask` 一定变为 `False`，且若之前共享则 `_mask` 的内存地址会改变（`id(d._mask)` 前后对比）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `__array_wrap__`（ufunc 钩子）要无条件执行 `result._mask.copy()`？

**参考答案**：ufunc 的结果是一个全新数组，其 mask 由 `mask_or` 合并所有输入 mask 得到。若不 copy，结果的 `_mask` 可能仍是某个输入数组的 mask 视图，之后对结果 mask 的就地修改会反向污染输入数组。`result._mask.copy()` 切断引用，保证「运算结果可安全修改」这一基本契约。

**练习 2**：`unshare_mask()` 的文档说 *A copy of the mask is only made if it was shared.*。若 `_sharedmask` 已经是 `False`，再次调用 `unshare_mask()` 会发生什么？

**参考答案**：什么都不做——`if self._sharedmask:` 为假，直接 `return self`，不产生拷贝。这是一个避免无谓复制的优化：在已经私有的数组上调用 `unshare_mask()` 是零开销的，因此调用方可以「无脑先 unshare 再改 mask」而不必担心性能损失。

---

## 5. 综合实践

把本讲三个机制串起来，完成下面这个「带保护的数据修正」小任务。

**任务背景**：你有一组传感器读数，其中 `-999` 表示「传感器故障，不可信」。要求：

1. 用 `masked_equal` 把 `-999` 屏蔽；
2. **冻结**这些故障位——即便后续误操作赋值，故障位也不应被覆盖或解除；
3. 给出一份「未屏蔽的干净数据」给下游；
4. 验证经过一系列写操作后，掩码内存被合理压缩（`nomask`）或合理共享。

**参考实现骨架**（请在本地补全并运行）：

```python
import numpy as np

raw = np.array([10, -999, 30, -999, 50])

# 步骤 1：屏蔽故障值
x = np.ma.masked_equal(raw, -999)
print("初始:", x)

# 步骤 2：冻结故障位（硬掩码）
x.harden_mask()
print("hardmask?", x.hardmask)

# 步骤 3：尝试用一个修正数组覆盖（其中含故障位）
correction = np.array([10, 999, 30, 777, 50])
x[:] = correction              # 硬掩码下，故障位不应被改写
print("覆盖后 _data:", x._data)   # 预期 [10, -999, 30, -999, 50]
print("覆盖后 _mask:", x._mask)   # 预期 [F, T, F, T, F]

# 步骤 4：取出干净数据
clean = x.compressed()
print("clean:", clean)            # 预期 [10, 30, 50]

# 步骤 5：另造一个全未屏蔽的数组，验证 shrink_mask
y = np.ma.array([1, 2, 3], mask=[0, 0, 0])
print("shrink 前 y.mask:", y.mask)
y.shrink_mask()
print("shrink 后 y.mask is nomask?", y.mask is np.ma.nomask)

# 步骤 6：观察 ufunc 结果的 mask 是否独立
z = x + 0
z[0] = np.ma.masked              # 修改 z 的 mask
# 观察 x 的 mask 是否被波及（应不受影响，因 __array_wrap__ 已 copy）
print("修改 z 后 x.mask 不变:", np.ma.testutils.assert_mask_equal(
    x.mask, [False, True, False, True, False]) or True)
```

**需要观察的现象与思考题**：

- 步骤 3 中，`x._data` 的故障位为何仍是 `-999` 而非 `999`/`777`？（提示：`copyto(where=~mindx)`）
- 步骤 6 中，若 NumPy 没有在 `__array_wrap__` 里做 `result._mask.copy()`，给 `z[0]` 屏蔽会怎样波及 `x`？这正是 `_sharedmask` 机制要防范的副作用。

若步骤 6 的断言失败（`x.mask` 被波及），说明你观察到了共享 mask 的「漏改」现象——请回头检查是否在某处遗漏了 `copy()` 或 `unshare_mask()`。

## 6. 本讲小结

- **硬掩码锁死屏蔽**：`_hardmask = True` 时，`__setitem__` 用 `mask_or`（只增不减）合并掩码、用 `copyto(where=~mindx)` 跳过屏蔽位写数据，被屏蔽值无法被赋值还原；软掩码（默认）则直接覆盖 data 与 mask，赋确定值即解除屏蔽。
- **三个就地开关方法**：`harden_mask()` / `soften_mask()` / `shrink_mask()` 都就地修改 `_hardmask` / `_mask` 并 `return self`，可链式调用；`hardmask` 与 `sharedmask` 是只读 property。
- **`shrink_mask` 是内存优化**：把全 `False` 的真实掩码压缩回 `nomask` 单例，纯空间优化、无语义影响；结构化 dtype 因字段级屏蔽语义不能 shrink（no-op）。
- **`_sharedmask` 是共享提示**：`True` 表示「mask 可能被他人引用，改前先 copy」，`False` 表示「私有可就地改」；`unshare_mask()` 仅在共享时才复制，零开销可放心调用。
- **ufunc 结果必然独立**：`__array_wrap__` 无条件 `result._mask.copy()`，切断与输入 mask 的引用，是防止「运算结果污染输入」的安全网。
- **构造时即决定共享**：`__new__` 中 `copy=False` 且无新 mask 时设 `_sharedmask = True`（共享源视图），有新 mask 或 `copy=True` 时设 `False`（独立）。

## 7. 下一步学习建议

本讲建立了「写操作如何作用于 `_data` 与 `_mask`」的完整图景。建议接下来：

1. **u3-l2 子类化 MaskedArray 与 mvoid**：`_hardmask`、`_sharedmask` 都通过 `_update_from` / `__array_finalize__` 在子类实例间传播，理解子类化时如何正确保留这些标志是进阶关键。
2. **u3-l5 掩码记录 mrecords 与字段级屏蔽**：本讲提到「结构化 dtype 的硬掩码未实现」「结构化掩码不能 shrink」，其背后的字段级屏蔽机制在 mrecords 中有专门设计（`_fieldmask`），值得对照学习。
3. **u3-l3 masked 单例与打印**：本讲的硬/软掩码实践多次用到 `masked` 单例（如 `x[0] = masked`），下一讲将深入这个不可变单例的实现细节。
4. **延伸阅读**：直接对照 `numpy/ma/tests/test_core.py` 的 `test_hardmask`（[test_core.py:2108](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2108)）、`test_shrink_mask`（[test_core.py:2199](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2199)）、`test_put_hardmask`（[test_core.py:3619](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L3619)）三组测试，它们是本讲所有行为的权威规约。
