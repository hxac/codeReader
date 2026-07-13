# 掩码的内部表示与构造

## 1. 本讲目标

在入门层我们学过：一个 `MaskedArray` 由 `data`、`mask`、`fill_value` 三件套组成，其中 `mask` 是一个布尔数组，`True` 表示该位置被屏蔽。本讲要钻进 `mask` 的「内部」——回答四个问题：

1. **「没有屏蔽」这件事在内存里到底怎么存的？** 为什么要引入 `nomask` 单例？
2. **结构化 dtype（带字段）的数组，mask 长什么样？** `make_mask_descr` 如何递归地把每个字段都变成布尔？
3. **`getmask` 和 `getmaskarray` 有什么区别？** 为什么一个可能返回 `False`，另一个一定返回同形状数组？
4. **如何从一个普通数组「造」出一个合规格码，又如何把两个掩码合并？** `make_mask` / `mask_or` 各自的处理流程是什么？

学完本讲，你应当能：解释 `nomask` 的省内存意义；说出 `getmask` 与 `getmaskarray` 在「无屏蔽」时返回值的不同；为结构化数组手算 `make_mask_descr` 的输出 dtype；并预测 `mask_or` 合并两个掩码的结果。

## 2. 前置知识

本讲默认你已学过 **u1-l4（data、mask、fill_value）**，知道下列事实：

- `MaskedArray` 是 `ndarray` 的子类，`.data` 是含全部原始值（含坏值）的视图，`.mask` 是同形状布尔数组，`.fill_value` 是对外填充值。
- `nomask` 是一个特殊对象，表示「整个数组没有任何屏蔽位」，它在打印时显示为 `False`。
- `getdata` / `getmask` / `getmaskarray` 是模块级函数，用鸭子类型从任意输入安全取值。

此外需要一点 NumPy 基础概念：

- **dtype（数据类型）**：描述数组每个元素的类型，例如 `int64`、`float32`。
- **结构化 dtype（structured dtype）**：一个元素由多个命名字段组成，例如 `[('foo', '<f4'), ('bar', '<i8')]` 表示每个元素有 `foo`（4 字节浮点）和 `bar`（8 字节整数）两个字段。
- **`is` 与 `==` 的区别**：`a is b` 判断两个变量是否指向**同一个对象**（身份）；`a == b` 判断**值是否相等**。`nomask` 在全代码库里都用 `is` 做身份判断，这是本讲的关键细节。
- **逻辑或（logical_or）**：布尔运算，只要有一个为 `True` 结果即为 `True`，记作 \( a \lor b \)。

> 术语提示：「flexible dtype / 复合 dtype」在 numpy.ma 源码里就指带字段的**结构化 dtype**。本讲交替使用这两个说法。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 一个文件里，涉及以下符号：

| 符号 | 行号 | 作用 |
|------|------|------|
| `MaskType` / `nomask` | L87-L88 | 布尔类型别名与「无屏蔽」单例 |
| `_replace_dtype_fields_recursive` | L1331-L1360 | 递归把 dtype 的每个叶子替换成布尔 |
| `_replace_dtype_fields` | L1363-L1374 | 上面函数的对外包装（先 coerce 成 dtype） |
| `make_mask_descr` | L1377-L1408 | 把任意 dtype 转成「同结构的全布尔 dtype」 |
| `getmask` | L1411-L1468 | 返回内部 `_mask`（可能是 `nomask`） |
| `getmaskarray` | L1474-L1525 | 永远返回同形状布尔数组 |
| `is_mask` | L1528-L1594 | 判断一个对象是否「合格布尔掩码」 |
| `_shrink_mask` | L1597-L1604 | 全 False 时压缩为 `nomask` |
| `make_mask` | L1607-L1695 | 把任意数组/序列规整成布尔掩码 |
| `make_mask_none` | L1698-L1746 | 生成指定形状的全 False 掩码 |
| `_recursive_mask_or` | L1749-L1756 | 对结构化掩码逐字段做 `logical_or` |
| `mask_or` | L1759-L1813 | 合并两个掩码 |

## 4. 核心概念与源码讲解

### 4.1 nomask 与 MaskType

#### 4.1.1 概念说明

掩码本质上是一个「同形状的布尔数组」。但绝大多数实际使用的 `MaskedArray`（尤其是运算中间结果）**根本没有任何被屏蔽的元素**。如果给每个这样的数组都分配一个和 `data` 一样大的全 `False` 布尔数组，会浪费大量内存。

于是 numpy.ma 设计了一个**单例** `nomask`，用「同一个对象」来代表「没有任何屏蔽位」。整个代码库里凡是想知道「这个数组有没有屏蔽」，都用 `x is nomask` 这种**身份判断**（而不是 `x == False` 这种值判断）。

与之配套，`MaskType` 是掩码元素类型的别名，就是 `np.bool`。

#### 4.1.2 核心流程

- `MaskType = np.bool`：掩码元素统一用布尔类型。
- `nomask = MaskType(0)`：即 `np.bool_(0)`，它的值就是 `False`，但作为一个**固定对象**被全模块复用。
- 当一个 `MaskedArray` 没有屏蔽位时，它的 `_mask` 属性被直接设成 `nomask`，**不分配任何数组**。
- 需要判断「是否无屏蔽」时，写 `self._mask is nomask`，而非 `not self._mask.any()`——前者是 O(1) 的指针比较，后者要扫描整个数组。

#### 4.1.3 源码精读

两行定义位于文件顶部，紧挨着 `__all__` 列表之后：

[core.py:87-88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88) —— 定义掩码元素类型与「无屏蔽」单例：

```python
MaskType = np.bool
nomask = MaskType(0)
```

判断一个对象是否「合格掩码」的 `is_mask`，只检查元素类型是否就是 `MaskType`：

[core.py:1591-1594](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1591-L1594) —— 注意它返回 `m.dtype.type is MaskType`，是身份比较；结构化（带字段）的掩码反而被判为「不合格」，因为它的 `dtype.type` 不是布尔：

```python
try:
    return m.dtype.type is MaskType
except AttributeError:
    return False
```

`_shrink_mask` 展示了 `nomask` 的典型用法——一个全是 `False` 的普通掩码会被「压缩」回 `nomask`：

[core.py:1597-1604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1597-L1604) —— 仅当 mask 是普通（无字段）且没有任何 `True` 时，才返回 `nomask`：

```python
def _shrink_mask(m):
    if m.dtype.names is None and not m.any():
        return nomask
    else:
        return m
```

#### 4.1.4 代码实践

**目标**：亲眼看到「无屏蔽数组」的内部 `_mask` 就是 `nomask`，且屏蔽一个元素后 `_mask` 变成真正的数组。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([1, 2, 3])          # 不传 mask，没有任何屏蔽
print(a._mask is ma.nomask)       # 身份判断
print(ma.is_mask(a._mask))

b = ma.array([1, 2, 3], mask=[0, 1, 0])  # 屏蔽第二个元素
print(b._mask is ma.nomask)       # 现在不再是 nomask
print(type(b._mask))
```

**需要观察的现象**：

- `a._mask is ma.nomask` 应为 `True`——说明没分配数组，直接复用单例。
- `b._mask is ma.nomask` 应为 `False`，`type(b._mask)` 是 `numpy.ndarray`。

**预期结果**：依次打印 `True`、`True`、`False`、`<class 'numpy.ndarray'>`。

> 说明：这里访问的是「私有」属性 `_mask` 仅用于教学观察，日常代码请用 `getmask` / `.mask`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 numpy.ma 用 `x is nomask` 而不是 `x == False` 来判断「无屏蔽」？

**参考答案**：`is` 是 O(1) 的对象身份比较，不扫描数组内容；而且 `nomask` 是全模块共享的固定单例，身份判断既快又准确。`== False` 对大数组要逐元素比较，且语义上有歧义（一个全 `False` 的真数组也会等于 `False`，但它并不是 `nomask`）。

**练习 2**：`is_mask(np.array([(True, False)], dtype=[('a', bool), ('b', bool)]))` 返回什么？为什么？

**参考答案**：返回 `False`。因为这个数组是结构化的，`dtype.type` 不是 `MaskType`（它的元素类型是 `void`，不是 `bool`）。`is_mask` 只认「普通布尔数组」为合格掩码。

---

### 4.2 make_mask_descr 结构化掩码

#### 4.2.1 概念说明

普通数组的 mask 很简单：一个同形状的 `bool` 数组。但**结构化数组**的每个元素由多个字段组成（比如 `('foo', 'i8')` 和 `('bar', 'f4')`），屏蔽也必须**字段级**——你可以只屏蔽某个元素的 `foo` 字段而不屏蔽 `bar`。

因此结构化数组的 mask 必须有和 data **完全相同的字段结构**，只是每个字段的类型从原来的 `i8`/`f4` 换成 `bool`。`make_mask_descr(ndtype)` 就是做这件事：输入一个 dtype，输出一个「结构一模一样、但所有叶子类型都变成布尔」的新 dtype。这个新 dtype 就是结构化数组 `_mask` 的 dtype。

#### 4.2.2 核心流程

`make_mask_descr` 是个一行函数，把活儿全交给 `_replace_dtype_fields`，后者再交给递归函数 `_replace_dtype_fields_recursive`。递归对每个 dtype 分三种情况：

1. **有命名字段**（`dtype.names is not None`）：逐个字段递归处理，字段名和结构保持不变。
2. **是子数组类型**（`dtype.subdtype`，例如 `(float, 2)` 表示长度为 2 的子数组）：递归处理其基础类型。
3. **基本类型**（既无字段也不是子数组）：直接替换成 `MaskType`（布尔）。

用文字描述递归结果：

```
make_mask_descr( [('foo','<f4'), ('bar','<i8')] )
  → 逐字段：'foo' 是基本类型 → bool；'bar' 是基本类型 → bool
  → 结果：[('foo','|b1'), ('bar','|b1')]
```

对于普通（非结构化）dtype，`make_mask_descr(np.float32)` 直接返回 `dtype('bool')`。

#### 4.2.3 源码精读

`make_mask_descr` 的全部实现就一行——把 dtype 的每个字段换成 `MaskType`：

[core.py:1408](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1408) —— 真正的工作在 `_replace_dtype_fields(ndtype, MaskType)`：

```python
return _replace_dtype_fields(ndtype, MaskType)
```

`_replace_dtype_fields` 先把入参 coerce 成 dtype，再调用递归版本：

[core.py:1372-1374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1372-L1374) —— coerce 之后转交递归函数：

```python
dtype = np.dtype(dtype)
primitive_dtype = np.dtype(primitive_dtype)
return _replace_dtype_fields_recursive(dtype, primitive_dtype)
```

真正的递归逻辑，三种分支一目了然：

[core.py:1336-1354](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1336-L1354) —— 命名字段逐个递归 / 子数组递归 / 基本类型直接替换：

```python
if dtype.names is not None:
    descr = []
    for name in dtype.names:
        field = dtype.fields[name]
        ...
        descr.append((name, _recurse(field[0], primitive_dtype)))
    new_dtype = np.dtype(descr)
elif dtype.subdtype:
    descr = list(dtype.subdtype)
    descr[0] = _recurse(dtype.subdtype[0], primitive_dtype)
    new_dtype = np.dtype(tuple(descr))
else:
    new_dtype = primitive_dtype
```

末尾还有一段「保 dtype 身份」的小优化：

[core.py:1356-1358](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1356-L1358) —— 若替换后 dtype 与原来相等，就返回原对象，避免创建等价的新 dtype：

```python
if new_dtype == dtype:
    new_dtype = dtype
```

#### 4.2.4 代码实践

**目标**：手算并验证 `make_mask_descr` 对结构化 dtype 的输出。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma

dt = np.dtype([('foo', np.float32), ('bar', np.int64)])
print("原 dtype:   ", dt)
print("mask dtype: ", ma.make_mask_descr(dt))
print("普通类型:   ", ma.make_mask_descr(np.float32))
```

**需要观察的现象**：字段名 `foo`/`bar` 保留不变，但类型从 `<f4`/`<i8` 全部变成 `|b1`（即 1 字节布尔）。

**预期结果**：

```
原 dtype:    dtype([('foo', '<f4'), ('bar', '<i8')])
mask dtype:  dtype([('foo', '|b1'), ('bar', '|b1')])
普通类型:    dtype('bool')
```

#### 4.2.5 小练习与答案

**练习 1**：嵌套结构化 dtype `[('a', 'i8'), ('b', [('b1', 'f4'), ('b2', 'f8')])]` 经过 `make_mask_descr` 后是什么？

**参考答案**：`dtype([('a', '|b1'), ('b', [('b1', '|b1'), ('b2', '|b1')])])`。因为递归会深入嵌套字段 `b`，把其中的 `b1`/`b2` 也变成布尔，结构完全保持。

**练习 2**：为什么 `make_mask_descr(np.dtype('bool'))` 要返回**同一个** dtype 对象而不是新建一个？

**参考答案**：因为全布尔 dtype 替换后和原 dtype 相等，函数末尾的 `if new_dtype == dtype: new_dtype = dtype` 让它返回原对象，避免无谓的对象创建，也让后续的 `is`/`==` 比较更稳定。

---

### 4.3 getmask vs getmaskarray

#### 4.3.1 概念说明

这两个函数名字很像，都是「取掩码」，但返回值在「无屏蔽」时截然不同：

- **`getmask(a)`**：返回数组内部真实的 `_mask`。如果该数组没有屏蔽位，就原样返回 `nomask`（也就是值 `False`）。它的特点是「**忠实但可能不完整**」——返回值可能是一个布尔数组，也可能是单个 `False`，调用方必须自己处理这种二义性。优点是零拷贝、零分配。
- **`getmaskarray(arr)`**：**永远返回一个和 `arr` 同形状的布尔数组**。没有屏蔽时它会把 `nomask` 展开成一个全 `False` 的同形状数组。它的特点是「**安全且统一**」——返回值恒为数组，适合直接拿去做布尔索引、和别的数组逐元素运算。

一句话记忆：`getmask` 返回「可能压缩」的原始 mask；`getmaskarray` 返回「一定展开」的同形状数组。

#### 4.3.2 核心流程

`getmask` 的实现就是一句 `getattr`——能取到 `_mask` 就取，取不到就用 `nomask` 兜底：

```
返回 getattr(a, '_mask', nomask)
```

`getmaskarray` 在 `getmask` 之上加一步：如果拿到的是 `nomask`（无屏蔽），就用 `make_mask_none(shape, dtype)` 生成一个同形状、同字段结构的全 `False` 数组：

```
mask = getmask(arr)
若 mask is nomask:
    mask = make_mask_none(shape(arr), dtype(arr))
返回 mask
```

用真值表归纳：

| 输入 `a` 的状态 | `getmask(a)` | `getmaskarray(a)` |
|----------------|--------------|-------------------|
| 有屏蔽位 | 同形状布尔数组 | 同形状布尔数组 |
| 无屏蔽位（`_mask is nomask`） | `nomask`（值 `False`） | 同形状**全 `False`** 数组 |
| 不是 MaskedArray | `nomask` | 按 `np.shape(a)` 生成全 `False` 数组 |

#### 4.3.3 源码精读

`getmask` 的核心——直接返回内部 `_mask` 或 `nomask`：

[core.py:1468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1468) —— 注意是返回 `_mask` 的原始引用，不做任何展开或拷贝：

```python
return getattr(a, '_mask', nomask)
```

紧接着还有一个别名，让旧名字也能用：

[core.py:1471](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1471) —— `get_mask` 是 `getmask` 的别名：

```python
get_mask = getmask
```

`getmaskarray` 在 `getmask` 之上加一个 `nomask` 展开分支：

[core.py:1522-1525](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1522-L1525) —— 用 `is nomask` 做身份判断，命中就调 `make_mask_none` 造全 `False` 数组：

```python
mask = getmask(arr)
if mask is nomask:
    mask = make_mask_none(np.shape(arr), getattr(arr, 'dtype', None))
return mask
```

辅助函数 `make_mask_none` 负责生成指定形状的全 `False` 掩码，结构化时也走 `make_mask_descr`：

[core.py:1742-1745](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1742-L1745) —— 无 dtype 时用 `MaskType`，有 dtype 时用 `make_mask_descr(dtype)`：

```python
if dtype is None:
    result = np.zeros(newshape, dtype=MaskType)
else:
    result = np.zeros(newshape, dtype=make_mask_descr(dtype))
return result
```

> 补充：`.mask` 这个 property（[core.py:3583-3591](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3583-L3591)）返回的是 `self._mask.view()`，是 `_mask` 的视图。它和 `getmask` 行为接近（无屏蔽时也与 `nomask` 相关），但 `getmask` 是函数、对任意输入都安全，是更推荐的取值方式。

#### 4.3.4 代码实践

**目标**：验证「无屏蔽」时 `getmask` 返回 `False`、`getmaskarray` 返回同形状全 `False` 数组。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma

b = ma.masked_array([[1, 2], [3, 4]])   # 不屏蔽任何元素
gm = ma.getmask(b)
gma = ma.getmaskarray(b)

print("getmask:      ", gm, "  is nomask?", gm is ma.nomask)
print("getmaskarray:\n", gma)
print("shape:", gma.shape, " dtype:", gma.dtype)
```

**需要观察的现象**：`getmask` 返回的 `gm` 与 `ma.nomask` 是同一个对象（`is` 为 `True`）；`getmaskarray` 返回一个 \( 2 \times 2 \) 的全 `False` 布尔数组。

**预期结果**：

```
getmask:       False   is nomask? True
getmaskarray:
 [[False False]
 [False False]]
shape: (2, 2)  dtype: bool
```

#### 4.3.5 小练习与答案

**练习 1**：为什么写 `arr[getmaskarray(x)]` 来取出 `x` 中所有被屏蔽元素是安全的，而用 `getmask` 就可能出错？

**参考答案**：当 `x` 无屏蔽时，`getmask(x)` 返回 `nomask`（值 `False`），用作索引会被当成「取第 0 个元素」而非「什么都不取」，行为错误。`getmaskarray(x)` 永远返回同形状布尔数组，无屏蔽时是全 `False`，布尔索引结果是空数组，行为正确。

**练习 2**：`getmask` 返回 `nomask` 时，为什么用 `== False` 判断为真、却仍推荐用 `is nomask`？

**参考答案**：`nomask` 的值就是 `False`，所以 `== False` 确实为真；但这会误伤——一个全 `False` 的真数组也会 `== False`（在 numpy 里会广播成全 `True` 的数组，语义混乱）。`is nomask` 只匹配单例本身，语义精确，是源码统一的判断方式。

---

### 4.4 make_mask / mask_or

#### 4.4.1 概念说明

知道掩码的「形状」之后，还要会「造」和「合」。

- **`make_mask(m, copy=False, shrink=True, dtype=MaskType)`**：把「任何能转成整数」的输入规整成一个合格布尔掩码。规则是 **0 → `False`，非 0 → `True`**（所以 `[1, 0, 2, -3]` 会变成 `[True, False, True, True]`）。它还提供两个开关：`shrink=True` 时若结果全是 `False` 就压回 `nomask`；`dtype` 可指定结构化 dtype 来生成字段级掩码。
- **`mask_or(m1, m2, copy=False, shrink=True)`**：用 `logical_or` 合并两个掩码，结果是「在 `m1` 或 `m2` 任一中被屏蔽的位置」都为 `True`。用公式表达：

\[
\text{out}_i = (m1_i) \lor (m2_i)
\]

合并时有个重要的「短路」优化：若其中一个输入是 `nomask`，结果直接取另一个（可能共享视图，不分配新数组）。

#### 4.4.2 核心流程

`make_mask` 的流程：

```
若 m is nomask: 直接返回 nomask
dtype = make_mask_descr(dtype)              # 确保是合规格码 dtype
（legacy）若 m 是有字段的 ndarray 且 dtype==bool: 返回全 True 数组
result = np.array(filled(m, True), copy=..., dtype=dtype, subok=True)
若 shrink: result = _shrink_mask(result)    # 全 False 则压回 nomask
返回 result
```

其中 `filled(m, True)` 会把 `m` 里的屏蔽位（如果 `m` 本身是 MaskedArray）填成 `True`，保证不丢信息。

`mask_or` 的流程，按「快路径」优先排列：

```
若 m1 is nomask 或 m1 is False: 用 m2 造 mask 返回
若 m2 is nomask 或 m2 is False: 用 m1 造 mask 返回   ← 共享视图优化
若 m1 is m2 且 is_mask(m1): 直接返回（shrink 后的）m1
若两者 dtype 不同: 抛 ValueError
若 dtype 有字段（结构化）: 用 _recursive_mask_or 逐字段 logical_or
否则: make_mask(logical_or(m1, m2))
```

结构化掩码的合并交给 `_recursive_mask_or`：遍历每个字段，叶子字段调 `umath.logical_or`，嵌套字段继续递归。

#### 4.4.3 源码精读

`make_mask` 的主体，可见 `nomask` 短路、`make_mask_descr` 规整、legacy 特例、填充与收缩：

[core.py:1679-1695](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1679-L1695) —— 注意 `shrink` 默认 `True`，所以全 `False` 输入会被压成 `nomask`：

```python
if m is nomask:
    return nomask
dtype = make_mask_descr(dtype)
if isinstance(m, ndarray) and m.dtype.fields and dtype == np.bool:
    return np.ones(m.shape, dtype=dtype)
copy = None if not copy else True
result = np.array(filled(m, True), copy=copy, dtype=dtype, subok=True)
if shrink:
    result = _shrink_mask(result)
return result
```

`mask_or` 的快路径与结构化分支：

[core.py:1797-1804](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1797-L1804) —— 任一输入是 `nomask`/`False` 时，直接用另一边造掩码（可能共享视图）；两边是同一对象时短路返回：

```python
if (m1 is nomask) or (m1 is False):
    dtype = getattr(m2, 'dtype', MaskType)
    return make_mask(m2, copy=copy, shrink=shrink, dtype=dtype)
if (m2 is nomask) or (m2 is False):
    dtype = getattr(m1, 'dtype', MaskType)
    return make_mask(m1, copy=copy, shrink=shrink, dtype=dtype)
if m1 is m2 and is_mask(m1):
    return _shrink_mask(m1) if shrink else m1
```

dtype 一致性检查与结构化/普通两条收尾路径：

[core.py:1805-1813](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1805-L1813) —— 结构化走 `_recursive_mask_or`，普通走 `logical_or` 再 `make_mask`：

```python
(dtype1, dtype2) = (getattr(m1, 'dtype', None), getattr(m2, 'dtype', None))
if dtype1 != dtype2:
    raise ValueError(f"Incompatible dtypes '{dtype1}'<>'{dtype2}'")
if dtype1.names is not None:
    newmask = np.empty(np.broadcast(m1, m2).shape, dtype1)
    _recursive_mask_or(m1, m2, newmask)
    return newmask
return make_mask(umath.logical_or(m1, m2), copy=copy, shrink=shrink)
```

结构化逐字段合并的递归实现：

[core.py:1749-1756](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1749-L1756) —— 叶子字段调 `logical_or`，嵌套字段继续递归：

```python
def _recursive_mask_or(m1, m2, newmask):
    names = m1.dtype.names
    for name in names:
        current1 = m1[name]
        if current1.dtype.names is not None:
            _recursive_mask_or(current1, m2[name], newmask[name])
        else:
            umath.logical_or(current1, m2[name], newmask[name])
```

#### 4.4.4 代码实践

**目标**：用 `mask_or` 合并两个掩码，并观察 `nomask` 短路优化与 `shrink` 行为。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma

m1 = ma.make_mask([0, 1, 1, 0])
m2 = ma.make_mask([1, 0, 0, 0])
print("m1        :", m1)
print("m2        :", m2)
print("mask_or   :", ma.mask_or(m1, m2))

# shrink 行为：全 False 输入会被压成 nomask
zeros = np.zeros(4)
print("shrink=True :", ma.make_mask(zeros))             # nomask (False)
print("shrink=False:", ma.make_mask(zeros, shrink=False))  # 全 False 数组

# nomask 短路：一边是 nomask 时直接返回另一边的视图
r = ma.mask_or(ma.nomask, m1)
print("nomask短路:", r, " 和 m1 同对象?", r is m1)
```

**需要观察的现象**：

- `mask_or(m1, m2)` = `[True, True, True, False]`（逐位或）。
- `shrink=True` 时全 `False` 变成 `False`（`nomask`）；`shrink=False` 时保留为 4 元全 `False` 数组。
- `mask_or(nomask, m1)` 直接返回 `m1`（`r is m1` 为 `True`，体现共享视图优化）。

**预期结果**：`r is m1` 为 `True`；`shrink=True` 输出 `False`，`shrink=False` 输出 `array([False, False, False, False])`。

#### 4.4.5 小练习与答案

**练习 1**：`make_mask([1, 0, 2, -3])` 的结果是什么？为什么？

**参考答案**：`array([True, False, True, True])`。因为 `make_mask` 的规则是「0 → False，非 0 → True」，`2` 和 `-3` 都是非 0，所以都是 `True`，只有 `0` 是 `False`。

**练习 2**：`mask_or` 在什么情况下会返回一个**共享视图**而非新数组？这样做的好处是什么？

**参考答案**：当其中一个输入是 `nomask`（或 `False`）时，`mask_or` 直接用另一边调 `make_mask`，在 `copy=False` 时可能返回另一边的视图而非拷贝。好处是省去一次 `logical_or` 计算和一次内存分配，对「一个数组有屏蔽、另一个没有」这种常见场景更高效。

**练习 3**：若 `m1`、`m2` 是结构化掩码且 dtype 不同，`mask_or` 会怎样？

**参考答案**：抛 `ValueError`（信息为 `Incompatible dtypes ...`）。`mask_or` 要求两个掩码 dtype 完全一致，结构化掩码的字段结构必须匹配才能逐字段 `logical_or`。

---

## 5. 综合实践

本综合实践把四个最小模块串起来，完成本讲规格指定的任务。

**任务**：构造一个结构化 dtype 的掩码数组，观察其 `mask` 的 dtype（`make_mask_descr` 产物）；再对比 `getmask` 与 `getmaskarray` 在「无屏蔽」与「有屏蔽」两种情况下的返回差异；最后用 `mask_or` 合并两个结构化掩码。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma

# ---- 第 1 步：结构化 dtype 的掩码描述 ----
dt = np.dtype([('foo', np.float32), ('bar', np.int64)])
print("make_mask_descr:", ma.make_mask_descr(dt))

# 用该 dtype 建一个不屏蔽任何元素的掩码数组
a = ma.masked_array(np.zeros(3, dtype=dt))
print("a._mask is nomask?", a._mask is ma.nomask)   # 预期 True

# 屏蔽部分字段后，mask 的 dtype 就是 make_mask_descr 的产物
b = ma.masked_array(
    np.array([(1.0, 1), (2.0, 2), (3.0, 3)], dtype=dt),
    mask=[(True, False), (False, True), (True, True)],
)
print("b.mask.dtype:", b.mask.dtype)
print("b.mask:\n", b.mask)

# ---- 第 2 步：getmask vs getmaskarray（无屏蔽 vs 有屏蔽）----
print("--- 无屏蔽的 a ---")
print("getmask(a):      ", ma.getmask(a), " is nomask?", ma.getmask(a) is ma.nomask)
print("getmaskarray(a):\n", ma.getmaskarray(a))

print("--- 有屏蔽的 b ---")
print("getmask(b) is b._mask?", ma.getmask(b) is b._mask)

# ---- 第 3 步：mask_or 合并两个结构化掩码 ----
m1 = ma.make_mask([(1, 0), (0, 0), (1, 0)], dtype=dt)
m2 = ma.make_mask([(0, 0), (0, 1), (0, 0)], dtype=dt)
combined = ma.mask_or(m1, m2)
print("m1:\n", m1)
print("m2:\n", m2)
print("mask_or(m1,m2):\n", combined)
```

**需要观察的现象与预期结果**：

1. `make_mask_descr(dt)` 输出 `dtype([('foo', '|b1'), ('bar', '|b1')])`——字段名不变，类型全布尔（模块 4.2）。
2. 无屏蔽的 `a`，其 `_mask is nomask` 为 `True`；`getmask(a)` 返回 `False`（即 `nomask`），而 `getmaskarray(a)` 返回一个 \( 3 \times \)结构、全 `False` 的数组（模块 4.1、4.3）。
3. 有屏蔽的 `b`，其 `.mask.dtype` 就是 `make_mask_descr` 的产物；`getmask(b) is b._mask` 为 `True`（无拷贝）（模块 4.3）。
4. `mask_or(m1, m2)` 逐字段做 `logical_or`：`foo` 列 `[1,0,1] | [0,0,0] = [1,0,1]`，`bar` 列 `[0,0,0] | [0,1,0] = [0,1,0]`（模块 4.4）。

> 若你的 numpy 版本对结构化 mask 的某些边界行为有差异（例如 `getmask(b) is b._mask` 在 `.mask` 经视图返回时可能为 `False`），请以本地实际输出为准，并据 `getmask` 源码（直接 `getattr` 取 `_mask`）解释原因。

## 6. 本讲小结

- **`nomask` 是省内存单例**：`MaskType = np.bool`，`nomask = MaskType(0)`。无屏蔽时 `_mask` 直接复用 `nomask`，全代码用 `is nomask` 做 O(1) 身份判断。
- **结构化掩码由 `make_mask_descr` 生成**：它递归地把 dtype 的每个字段换成布尔，结构（字段名/嵌套）完全保留，是结构化数组 `_mask` 的 dtype。
- **`getmask` 忠实、`getmaskarray` 安全**：前者直接返回内部 `_mask`（可能是 `nomask`/`False`），后者在无屏蔽时用 `make_mask_none` 展开成同形状全 `False` 数组。
- **`make_mask` 规整掩码**：规则「0 → False，非 0 → True」，`shrink=True` 时全 `False` 压回 `nomask`，支持结构化 dtype。
- **`mask_or` 用 `logical_or` 合并**：有 `nomask` 短路（共享视图）、dtype 一致性检查、结构化逐字段合并三条路径。
- **设计主线**：用「单例 + 身份判断」避免无屏蔽时的内存与计算浪费，同时通过 `getmaskarray` / `make_mask_none` 在需要统一形状时安全展开。

## 7. 下一步学习建议

本讲把「mask 自身」讲透了，但还没讲 mask 如何随**运算和切片传播**。建议接下来学习：

- **u2-l2 MaskedArray 类与 ndarray 子类化机制**：重点读 `__new__` / `__array_finalize__` / `__array_wrap__`，理解 `_mask` 在切片、视图、ufunc 后如何被复制或传播——这是本讲 `nomask` 优化在工程上能成立的关键。
- **u2-l4 掩码一元运算与域**：会用到本讲的 `mask_or` 来合并「域检查产生的屏蔽」与「原有屏蔽」，是 `mask_or` 最典型的真实调用场景。
- 如果你对结构化掩码感兴趣，可以提前翻阅 **u3-l5（mrecords 与字段级屏蔽）**，那里会用到本讲的 `make_mask_descr` 来构造字段级 `_fieldmask`。

继续阅读源码时，可以从 `make_mask_descr`（[core.py:1377](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1377)）出发，用 `grep` 搜它的调用点，观察「合规格码 dtype」是如何贯穿 `MaskedArray.__new__` 的整个构造流程的。
