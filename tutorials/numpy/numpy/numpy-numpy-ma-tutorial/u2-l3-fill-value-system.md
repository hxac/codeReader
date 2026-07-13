# 填充值（fill_value）系统

## 1. 本讲目标

本讲专门拆解 `numpy.ma` 中「填充值（fill_value）」这一条独立子系统。学完后你应当能够：

- 说清楚 `fill_value` 的两层含义：**约定俗成的缺失标记**（`default_fill_value`）与**为 min/max 归约服务的类型极值**（`minimum/maximum_fill_value`）。
- 读懂 `_recursive_fill_value` 如何把一个标量规则递归推广到结构化 dtype 与子数组 dtype。
- 会用 `set_fill_value` / `get_fill_value`（以及 `MaskedArray.fill_value` 这个 property）读写填充值，并理解它「惰性求值 + 始终存成 0 维数组」的实现。
- 看懂 `_check_fill_value` 如何在写入时做类型校验与强制转换，并理解 `common_fill_value` 在两数组协作时的作用。
- 能用一句话解释「为什么 `maximum_fill_value` 返回的却是 dtype 的**最小**可表示值」，并能据此推导 masked `max` 运算的行为。

## 2. 前置知识

本讲承接 [u1-l4 读取与提取：data、mask、fill_value](u1-l4-data-mask-fill-value.md)，那里我们已经建立了掩码数组的「三件套」模型：

- `data`：保存全部原始值（含坏值）的普通 ndarray 视图。
- `mask`：同形状布尔数组，`True` 表示该位被屏蔽；无屏蔽时压缩为单例 `nomask`（即 `False`）以省内存。
- `fill_value`：屏蔽位「对外」展示或参与某些运算时使用的替代值。

在 [u1-l4](u1-l4-data-mask-fill-value.md) 里我们看到 `.fill_value` 默认整数是 `999999`、浮点是 `1e20`，也知道 `filled()` 会用 `fill_value` 把屏蔽位替换掉、保持原形状返回普通 ndarray。本讲要回答两个 u1-l4 留下的问题：

1. 这个 `999999` / `1e20` 是从哪里、按什么规则算出来的？
2. `fill_value` 除了「填充展示」之外，还在掩码数组的哪些地方被悄悄使用？

需要补充的两个背景术语：

- **dtype.kind**：NumPy dtype 的「类别字符」，例如 `i`（整数）、`f`（浮点）、`c`（复数）、`b`（布尔）、`O`（对象）、`S/U`（字节/字符串）。本讲里你会看到代码用它做字典查找。
- **0 维数组（0d array）**：形状为 `()` 的标量数组。掩码数组内部把 `fill_value` 存成一个 0d ndarray，访问时再用 `[()]` 解包成 Python/NumPy 标量。这样做是为了在子类传播和就地写入时共享同一块内存。

## 3. 本讲源码地图

本讲全部源码集中在 `numpy/ma/core.py` 一个文件。涉及的代码点按「自底向上」排列如下：

| 代码点 | 作用 |
| --- | --- |
| `default_filler` 字典（core.py:164-180） | 按 dtype 类别给出「约定俗成」的缺失标记 |
| `_minvals`/`_maxvals` 与 `max_filler`/`min_filler`（core.py:185-214） | 用 `iinfo`/`finfo` 算出每种类型的可表示极值，并（反直觉地）命名 |
| `_recursive_fill_value`（core.py:218-247） | 把一个标量规则递归推广到结构化/子数组 dtype |
| `_get_dtype_of`（core.py:250-257） | 把「数组 / dtype / 标量」统一成 dtype |
| `default_fill_value`（core.py:260-314） | 公开函数：返回默认缺失标记 |
| `_extremum_fill_value`（core.py:317-328） | 极值填充值的内部模板 |
| `minimum_fill_value` / `maximum_fill_value`（core.py:331-432） | 公开函数：返回归约用的极值填充值 |
| `_check_fill_value`（core.py:467-517） | 写入时的类型校验与强制转换，输出恒为 0d 数组 |
| `set_fill_value` / `get_fill_value` / `common_fill_value`（core.py:520-628） | 模块级读写与比较工具 |
| `MaskedArray.fill_value` property（core.py:3793-3855） | 实例级惰性读写入口 |
| `MaskedArray.max`（core.py:5973-6077） | 展示极值填充值在归约中如何决定屏蔽值取舍 |

> 提示：本讲引用的行号基于当前 HEAD `b21650c4f6`。所有永久链接都指向该 commit。

## 4. 核心概念与源码讲解

### 4.1 default_fill_value 与递归生成 _recursive_fill_value

#### 4.1.1 概念说明

`fill_value` 有两种截然不同的「语义来源」，本模块讲第一种：**约定俗成的缺失标记**。

很多领域都习惯用一个「明显不可能是真实数据」的大数来标记缺失：气象数据常用 `-999`，浮点统计常用 `1e20`。`numpy.ma` 把这种约定固化成一张表，按 dtype 类别给出默认值：

| dtype 类别 | 默认 fill_value | 含义 |
| --- | --- | --- |
| `b`（布尔） | `True` | 布尔只有两个值，缺失就取「真」 |
| `i` / `u`（整数/无符号整数） | `999999` | 一个不太会出现在真实整数数据里的中等大数 |
| `f`（浮点） | `1.e20` | 足够大、但仍有限的浮点哨兵值 |
| `c`（复数） | `1.e20 + 0.0j` | 浮点规则的复数版 |
| `O`（对象） | `'?'` | 对象数组没有「哨兵值」概念，用一个占位字符串 |
| `S`（字节串）/ `U`（字符串）/ `T`（StringDType） | `b'N/A'` / `'N/A'` / `'N/A'` | 文本类的「不可用」标记 |
| `V`（void） | `b'???'` | 原始字节 |

注意几个关键点：

- 这些值是**惯例**，不是 dtype 的「极限值」。整数默认 `999999` 远小于 `int64` 的上限 `9.2e18`。
- `1e20` 选成浮点哨兵是因为它「大到通常不会撞上真实数据」却又「有界」（不是 `inf`），方便后续参与运算或被检测。
- 日期时间类（`M8`/`m8`）额外用 `NaT`（Not a Time）作为默认缺失标记。

#### 4.1.2 核心流程

`default_fill_value(obj)` 的执行流程是一个典型的「统一入口 + 递归派发」结构：

```
default_fill_value(obj)
  ├── _get_dtype_of(obj)        # 把数组/标量/dtype 统一成一个 dtype
  └── _recursive_fill_value(dtype, _scalar_fill_value)
        ├── dtype.names 不为 None？  → 结构化 dtype：逐字段递归，拼成结构化标量
        ├── dtype.subdtype 不为 None？→ 子数组字段（如 shape=(2,3)）：递归子类型后 np.full
        └── 否则（标量 dtype）       → 调用 _scalar_fill_value(dtype) 查表
```

其中 `_scalar_fill_value` 是查表闭包：对日期时间类用 `dtype.str[1:]`（如 `"m8[s]"`）精确查，其它用 `dtype.kind`（如 `'f'`）查 `default_filler` 字典。

为什么需要递归？因为 NumPy 的 dtype 可以是**结构化**的（多个命名字段，每个字段又可以是结构化的），也可以含**子数组**字段（一个字段本身是一个固定形状的小数组）。这两种情况下「标量填充值」无法直接使用，必须按字段展开。

#### 4.1.3 源码精读

先看那张默认值表（注意它就是一张普通的 Python dict）：

[core.py:164-180](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L164-L180) 定义了 `default_filler` 字典并追加日期时间类。它按 `dtype.kind` 给出标量默认值，下方循环为每种时间单位（`Y/M/D/...`）填入 `NaT`。

[core.py:218-247](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L218-L247) 是递归核心 `_recursive_fill_value(dtype, f)`。三个分支分别处理结构化、子数组、标量：

```python
if dtype.names is not None:          # 结构化：逐字段递归
    ...
    return np.array(tuple(vals), dtype=dtype)[()]   # 解包成 void 标量
elif dtype.subdtype:                 # 子数组字段：递归子类型再 np.full
    subtype, shape = dtype.subdtype
    subval = _recursive_fill_value(subtype, f)
    return np.full(shape, subval)
else:                                # 标量：交给回调查表
    return f(dtype)
```

末尾的 `[()]` 是一个关键技巧：`np.array(..., dtype=dtype)` 得到一个 0 维结构化数组，用 `[()]` 把它「解包」成 `np.void` 标量，从而让结构化填充值表现得像一个普通标量。

[core.py:260-314](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L260-L314) 是公开函数 `default_fill_value`。它的核心只有两行：先用 `_get_dtype_of` 把输入归一成 dtype，再调 `_recursive_fill_value` 配上一个查表闭包 `_scalar_fill_value`。函数 docstring 里给出的几个例子就是后续实践的预期输出（这些是源码里的可执行 doctest）：

```
>>> np.ma.default_fill_value(1)                  # → 999999
>>> np.ma.default_fill_value(np.array([1.1,...]))# → 1e+20
>>> np.ma.default_fill_value(np.dtype(complex))  # → (1e20+0j)
```

#### 4.1.4 代码实践

**实践目标**：亲手验证三种 dtype 的默认填充值，并体会结构化 dtype 的递归展开。

**操作步骤**（在装好 numpy 的环境里执行）：

```python
import numpy as np
import numpy.ma as ma

# 1) 三种基本 dtype 的默认填充值
print(ma.default_fill_value(np.array([1, 2, 3], dtype='i8')))   # 整数
print(ma.default_fill_value(np.array([1., 2., 3.], dtype='f8')))# 浮点
print(ma.default_fill_value(np.array([1+2j], dtype='c16')))     # 复数

# 2) 直接传 dtype 或 Python 标量也可以
print(ma.default_fill_value(np.dtype('complex')))
print(ma.default_fill_value(1))

# 3) 结构化 dtype：每个字段各取其默认值
dt = np.dtype([('x', 'i4'), ('y', 'f4'), ('name', 'U5')])
print(ma.default_fill_value(dt))
```

**需要观察的现象**：

- 整数输出 `999999`，浮点输出 `1e+20`，复数输出 `(1e+20+0j)`。
- 结构化 dtype 输出一个「结构化标量」，形如 `(999999, 1e+20, 'N/A')`——即三个字段分别取 `i`/`f`/`U` 类别的默认值，这正是 `_recursive_fill_value` 走 `dtype.names is not None` 分支递归逐字段查表的结果。

**预期结果**（与源码 docstring 中的 doctest 一致）：

```
999999
1e+20
(1e+20+0j)
(1e+20+0j)
999999
(999999, 1.e+20, 'N/A')
```

#### 4.1.5 小练习与答案

**练习 1**：为什么整数默认用 `999999` 而不用 `int64` 的最大值 `9223372036854775807`？

**答案**：`default_fill_value` 返回的是「**约定俗成的缺失哨兵**」，不是类型极限。`999999` 是一个「通常不会出现在真实数据里、又便于人类识别」的中等大数；类型极限值则留给 `minimum/maximum_fill_value`（见 4.3）做归约用途。两者服务不同目的，所以选不同的数。

**练习 2**：若把 `default_filler` 字典里 `'f'` 的值改成 `float('nan')`，`ma.array([1.,2.], mask=[1,0]).filled()` 会得到什么？这样做有什么坏处？

**答案**：会得到 `[nan, 2.]`。坏处是 `nan` 有「传染性」：它会让后续 `sum/mean/max` 等归约结果变成 `nan`，失去「填充后能安全参与普通运算」的性质。这正是 [u1-l1](u1-l1-numpy-ma-overview.md) 讲过的 NaN 污染问题，也是默认填充值选「有界大数」而非 `nan` 的原因。

### 4.2 极值填充值 minimum/maximum_fill_value

#### 4.2.1 概念说明

`fill_value` 的第二种语义，是**为 min/max 归约量身定制的类型极值**。这也是本讲最容易让人迷惑的一处命名陷阱，务必先记住结论：

| 函数名 | 它返回的值 | 直觉理解 |
| --- | --- | --- |
| `maximum_fill_value(obj)` | dtype 的**最小**可表示值（如 `int8 → -128`、`float32 → -inf`） | 「用来做 **max** 运算时填充屏蔽位的值」 |
| `minimum_fill_value(obj)` | dtype 的**最大**可表示值（如 `int8 → 127`、`float32 → +inf`） | 「用来做 **min** 运算时填充屏蔽位的值」 |

为什么「反着来」？因为掩码数组的归约策略是**「把屏蔽位填上一个值，让它一定赢不了」**：

- 求 `max` 时，把屏蔽位填成「最小可能值」，那么任何真实数据都大于它，屏蔽位在比较中永远落败 → 等价于「跳过屏蔽位」。
- 求 `min` 时则相反，填成「最大可能值」。

所以函数名的 `maximum/minimum` 描述的是「**这个填充值是给哪一种归约用的**」，而不是「它本身是极大还是极小」。这一点在源码 docstring 里写得很直白：`maximum_fill_value` 的说明是 "Return the **minimum** value that can be represented by the dtype"。

#### 4.2.2 核心流程

极值表的构造逻辑（[core.py:185-214](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L185-L214)）：

```
遍历所有已注册的标量类型 sctypeDict：
  ├── 日期时间类 → 取 int64 的 (min+1, max)
  ├── 整数        → iinfo(sctype).min / max
  ├── 浮点        → finfo(sctype).min / max
  ├── 布尔        → (0, 1)
  └── 其它        → (None, None)

max_filler = _minvals          # 注意：max 用的是「最小值」表！
  并把 4 种实浮点的值覆盖为 -inf
  并把 3 种复浮点的值覆盖为 complex(-inf,-inf)

min_filler = _maxvals          # min 用的是「最大值」表！
  并把 4 种实浮点的值覆盖为 +inf
  并把 3 种复浮点的值覆盖为 complex(+inf,+inf)
```

关键观察：浮点类的极值填充值被专门**覆盖成无穷大**，而不是 `finfo` 给出的有限最小/最大值。原因仍是「让屏蔽位绝对赢不了」——`-inf` 比任何有限浮点都小，比较结果最确定。

两个公开函数 `minimum_fill_value` / `maximum_fill_value` 都是薄壳，共享同一个模板 `_extremum_fill_value`：

[core.py:317-328](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L317-L328) 定义 `_extremum_fill_value(obj, extremum, extremum_name)`，内部闭包 `extremum[dtype.type]` 查表，查不到就抛 `TypeError`（说明该类型不适合做极值归约，比如对象数组）。

[core.py:331-380](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L331-L380) 与 [core.py:383-432](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L383-L432) 分别是 `minimum_fill_value`（传入 `min_filler`）和 `maximum_fill_value`（传入 `max_filler`），二者各只有一行实现。

#### 4.2.3 源码精读：max 如何用极值填充值「跳过」屏蔽位

这是本讲的高潮。看 `MaskedArray.max`（[core.py:5973-6077](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5973-L6077)）的核心几行：

```python
_mask = self._mask
newmask = _check_mask_axis(_mask, axis, **kwargs)   # 先算出结果上应有的掩码
if fill_value is None:
    fill_value = maximum_fill_value(self)            # ① 缺省时用「最小可表示值」
if out is None:
    result = self.filled(fill_value).max(            # ② 把屏蔽位填成最小值，再普通 max
        axis=axis, out=out, **kwargs).view(type(self))
    if result.ndim:
        result.__setmask__(newmask)                  # ③ 把掩码盖回结果
        if newmask.ndim:
            np.copyto(result, result.fill_value, where=newmask)
    elif newmask:
        result = masked                               # ④ 全屏蔽的标量结果 → masked 单例
    return result
```

把这四步翻译成大白话：

1. **取极值填充值**：用户没传 `fill_value` 时，默认调 `maximum_fill_value(self)`，得到 dtype 的最小可表示值（浮点是 `-inf`）。
2. **填充后做普通 max**：`self.filled(fill_value)` 把所有屏蔽位替换成这个最小值，于是这些位置在任何「比大小」里都输；再调用普通 ndarray 的 `.max()`，等价于「只在非屏蔽元素中取最大」。
3. **掩码回填**：算出结果数组每个位置是否「整条轴都被屏蔽」（`newmask`），把这些位置的值再用 `np.copyto(..., where=newmask)` 覆盖回填充值——避免 `-inf` 这种填充痕迹泄漏到结果里。
4. **标量全屏蔽特判**：若结果是个标量且整条都被屏蔽，直接返回 `masked` 单例而不是一个数值。

所以 masked `max` 的「跳过屏蔽位」并非真的逐元素过滤，而是**用极值填充值把屏蔽位「伪装」成必败者，再复用普通 `max`**。`minimum_fill_value` 对 `min` 起完全对称的作用（[core.py:5875-5972](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5875-L5972) 的 `MaskedArray.min` 用 `minimum_fill_value`，即「最大可表示值」）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `maximum_fill_value` 返回的是「最小可表示值」，并验证 masked `max` 正是靠它跳过屏蔽位的。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# (a) 三种 dtype 的极值填充值
print('maxfill int8 :', ma.maximum_fill_value(np.array([0], dtype='i1')))  # → -128
print('maxfill int32:', ma.maximum_fill_value(np.array([0], dtype='i4')))  # → -2147483648
print('maxfill f4   :', ma.maximum_fill_value(np.array([0.], dtype='f4'))) # → -inf
print('minfill int8 :', ma.minimum_fill_value(np.array([0], dtype='i1')))  # → 127

# (b) masked max 跳过屏蔽位
a = ma.array([1., 5., 9., 2.], mask=[0, 0, 1, 0], dtype='f8')  # 屏蔽了 9.
print('a.max()      :', a.max())                                  # → 5.0，不是 9.
print('filled view  :', a.filled(ma.maximum_fill_value(a)))      # 屏蔽位变成了 -inf
print('plain max    :', a.filled(ma.maximum_fill_value(a)).max())# → 5.0，印证上面
```

**需要观察的现象**：

- `maximum_fill_value` 对 `int8` 返回 `-128`（`int8` 的最小值），对 `float32` 返回 `-inf`——与「maximum」字面意思相反，正好印证 4.2.1 的命名陷阱。
- `a.max()` 得到 `5.0` 而非 `9.0`，因为被屏蔽的 `9.` 在 `filled` 阶段被替换成 `-inf`，在普通 `max` 比较中输给了 `5.0`。
- 打印 `filled view` 时，被屏蔽的位置显示为 `-inf`，正是 `maximum_fill_value` 填进去的值。

**预期结果**（数值与源码 docstring 的 doctest 一致）：

```
maxfill int8 : -128
maxfill int32: -2147483648
maxfill f4   : -inf
minfill int8 : 127
a.max()      : 5.0
filled view  : [  1.   5. -inf   2.]
plain max    : 5.0
```

> 待本地验证：复数类型 `maximum_fill_value` 会返回 `complex(-inf, -inf)`；对象数组（`dtype=object`）会因查表失败抛 `TypeError`。你可以自行构造例子确认。

#### 4.2.5 小练习与答案

**练习 1**：对 `ma.array([1., 2.], mask=[1, 1])` 调 `.max()`，结果是什么？为什么？

**答案**：结果是 `masked`（屏蔽单例），而不是某个数。因为两个元素都被屏蔽，`filled` 后整条都是 `-inf`，普通 `max` 虽然返回 `-inf`，但 `newmask` 表明「整条轴都被屏蔽」，于是 `max` 的第 4 步特判把它替换成 `masked` 单例（见 [core.py:6060-6061](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6060-L6061)）。

**练习 2**：若手动给 `a.max(fill_value=9.)` 传一个很大的 `fill_value`，会发生什么？为什么这个用法通常是错的？

**答案**：屏蔽位会被填成 `9.`，于是它可能「赢」过真实数据，导致 `a.max()` 返回一个本该被屏蔽的值。这违背了「跳过屏蔽位」的语义，所以做 `max` 时应当用 `maximum_fill_value`（最小可表示值）而不是随便一个大数。这也解释了为什么 `max` 把缺省 `fill_value` 硬绑定到 `maximum_fill_value`。

### 4.3 设置与读取 fill_value（set / get 与 property）

#### 4.3.1 概念说明

知道了填充值的「两种来源」之后，下一个问题是：**一个具体数组的 `fill_value` 到底怎么存、怎么读、怎么改？**

答案有三套等价入口：

| 层次 | 写 | 读 |
| --- | --- | --- |
| 模块级函数 | `ma.set_fill_value(a, v)` | `ma.get_fill_value(a)` |
| 实例方法 | `a.set_fill_value(v)` | `a.get_fill_value()` |
| property 语法糖 | `a.fill_value = v` | `a.fill_value` |

其中实例方法 `get_fill_value` / `set_fill_value` 其实就是 property 的 `fget` / `fset` 别名（见 4.3.3），三者背后是同一份逻辑。还有两个关键性质：

- **惰性求值**：数组内部用 `_fill_value = None` 表示「尚未计算」。只有第一次访问 `.fill_value` 时，才调 `_check_fill_value(None, dtype)` 算出默认值并存下来。
- **存成 0 维数组**：内部存的不是 Python 标量，而是一个 0d ndarray；访问时用 `[()]` 解包成标量。这样做是为了在子类传播（`__array_finalize__`）和就地写入（`_fill_value[()] = target`）时**共享同一块内存**，而不必每次复制。

模块级 `set_fill_value` 还有一个「鸭子类型」的容错：传入的不是 `MaskedArray` 时**静默返回**，不报错（方便对混合类型的容器统一调用）。

#### 4.3.2 核心流程

读取 `.fill_value` 的流程（[core.py:3823-3832](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3823-L3832)）：

```
访问 a.fill_value
  ├── _fill_value is None?  → 调 _check_fill_value(None, dtype) 懒算默认值并缓存
  └── 返回：若是 ndarray → _fill_value[()]（解包成标量）；否则原样返回
```

写入 `a.fill_value = v` 的流程（[core.py:3835-3851](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3835-L3851)）：

```
赋值 a.fill_value = value
  ├── target = _check_fill_value(value, dtype)   # 校验 + 强制成 0d 数组
  ├── target.ndim != 0?  → 发出 DeprecationWarning（非标量填充值已弃用）
  └── 若 _fill_value 已存在 → _fill_value[()] = target   # 就地写入，保内存共享
     否则                  → _fill_value = target        # 首次创建属性
```

注意「就地写入」这一步：它不是 `self._fill_value = target`（换引用），而是 `self._fill_value[()] = target`（改内容）。这正是为了保持「子类/视图共享同一 `_fill_value`」的传播链不被打断。

#### 4.3.3 源码精读

[core.py:3793-3855](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3793-L3855) 是 `MaskedArray.fill_value` 这个 property。getter 做惰性初始化与 `[()]` 解包；setter 走 `_check_fill_value` 校验后就地写入。末尾两行尤其值得注意：

```python
# kept for compatibility
get_fill_value = fill_value.fget
set_fill_value = fill_value.fset
```

这意味着 `a.get_fill_value()` 和 `a.fill_value` 走的是**同一个 getter**，`a.set_fill_value(v)` 和 `a.fill_value = v` 走的是**同一个 setter**——只是历史遗留的两种写法。

模块级 `set_fill_value` 在 [core.py:520-582](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L520-L582)，核心是：

```python
def set_fill_value(a, fill_value):
    if isinstance(a, MaskedArray):
        a.set_fill_value(fill_value)
```

非 `MaskedArray` 直接被忽略（docstring 用 `list(range(5))` 和普通 `ndarray` 演示了「什么都不发生」）。`get_fill_value`（[core.py:585-595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L585-L595)）同理：是 `MaskedArray` 就读 `a.fill_value`，否则回退到 `default_fill_value(a)`。

`common_fill_value(a, b)`（[core.py:598-628](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L598-L628)）用于两个数组协作（例如二元运算）前判断能否统一填充值：若两者 `fill_value` 相等则返回该值，否则返回 `None`。它内部就调 `get_fill_value` 比较。

#### 4.3.4 代码实践

**实践目标**：用三种写法读写同一个数组的 `fill_value`，并验证「写 `None` 会重置回默认」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.masked_array([1., 2., 3.], mask=[0, 1, 0])
print('初始 fill_value :', a.fill_value)        # 默认 1e+20

# 三种等价的「写」
a.fill_value = -999.0                            # property 赋值
print('改后 fill_value :', a.fill_value, '| 方法:', a.get_fill_value())

ma.set_fill_value(a, 3.14)                       # 模块级函数
print('再改 fill_value:', a.fill_value)

a.set_fill_value(0.0)                            # 实例方法
print('三改 fill_value:', a.fill_value)

# 写 None 重置为默认
a.fill_value = None
print('重置 fill_value:', a.fill_value)          # 又回到 1e+20

# 静默容错：对普通 ndarray 调用不报错也不改
b = np.arange(3)
ma.set_fill_value(b, 100)
print('普通 ndarray  :', b)                      # 仍是 [0,1,2]
```

**需要观察的现象**：

- 初始 `fill_value` 是 `1e+20`（首次访问时由 `_check_fill_value(None, ...)` 懒算得到）。
- 三种写法（property / 模块函数 / 方法）效果完全一致。
- 赋值 `None` 会重新走默认分支，回到 `1e+20`。
- 对普通 `ndarray` 调 `set_fill_value` 静默无效。

**预期结果**：

```
初始 fill_value : 1e+20
改后 fill_value : -999.0 | 方法: -999.0
再改 fill_value: 3.14
三改 fill_value: 0.0
重置 fill_value: 1e+20
普通 ndarray  : [0 1 2]
```

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接把 `fill_value` 存成 Python 标量（如 `float`），而要存成 0d ndarray 并用 `[()]` 解包？

**答案**：因为 `MaskedArray` 是 `ndarray` 的子类，切片、视图、ufunc 都会产生新的 `MaskedArray`，它们常常需要**共享同一份 `_fill_value`**（参考 [u2-l2](u2-l2-maskedarray-ndarray-subclass.md) 讲的 `__array_finalize__` 传播）。把 `_fill_value` 存成可变 0d 数组，setter 用 `_fill_value[()] = target` **就地改内容**，就能让所有共享者同步看到新值；若存成不可变的 Python 标量，每次赋值都会换引用，传播链就会被切断。

**练习 2**：`common_fill_value(a, b)` 返回 `None` 意味着什么？

**答案**：意味着两个数组的 `fill_value` 不相等。调用方据此知道无法直接用一个统一的填充值同时表达两者，需要在后续运算里特殊处理（比如各填各的，或抛错）。

### 4.4 _check_fill_value 校验与 common_fill_value 的协作

#### 4.4.1 概念说明

`_check_fill_value` 是前面所有读写路径都会经过的「**关卡**」。它解决三类问题：

1. **缺省归一**：`fill_value=None` 时，按 dtype 算出默认值（4.1 的 `default_fill_value`）。
2. **类型强制**：把用户给的值强制转换成数组的 dtype（比如把 `1e20` 转成整数，或把字符串转成字符串 dtype）。
3. **错误拦截**：当用户给的值与 dtype 完全不兼容（如给数值数组填字符串）时，把 NumPy 抛出的 `OverflowError`/`ValueError` 统一包装成更清晰的 `TypeError`。

它的输出**恒为一个 0d 数组**（`np.array(fill_value)`），这是保证后续「就地写入」语义的前提。

这里还有几处「历史包袱」式的小逻辑值得知道：

- **无符号整数特判**：dtype 是 `u`（无符号整数）时，默认填充值会额外 `np.uint(...)` 一次。源码注释解释这是 NumPy 2.x 的 cast safety 变化所致——默认的 `int(999999)` 对 `uint` 不再是「同类型转换」，会被判为不安全。
- **结构化 dtype 分支**：结构化数组的填充值走 `_recursive_set_fill_value` 逐字段构造。
- **字符串→非字符串拦截**：用户给字符串当 `fill_value`、但 dtype 又不是 `O/S/T/V/U` 这几类文本型时，直接抛 `TypeError`。

#### 4.4.2 核心流程

`_check_fill_value(fill_value, ndtype)`（[core.py:467-517](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L467-L517)）的决策树：

```
ndtype = np.dtype(ndtype)
if fill_value is None:                       # ① 缺省：算默认
    fill_value = default_fill_value(ndtype)
    if ndtype.kind == 'u':                   #    无符号整数：再包 np.uint
        fill_value = np.uint(fill_value)
elif ndtype.names is not None:               # ② 结构化：逐字段构造
    ... _recursive_set_fill_value ...
elif fill_value 是 str 且 dtype 不是文本型:    # ③ 字符串误用：TypeError
    raise TypeError(...)
else:                                        # ④ 普通强制转换
    try: fill_value = np.asarray(fill_value, dtype=ndtype)
    except (OverflowError, ValueError): raise TypeError(...)
return np.array(fill_value)                  # 恒返回 0d 数组
```

结构化分支用到的 `_recursive_set_fill_value`（[core.py:435-464](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L435-L464)）会把传入的填充值 `np.resize` 到字段个数，再逐字段转换，返回一个嵌套 tuple。

`_check_fill_value` 在源码里被四个时机调用，构成了 fill_value 的「全生命周期」：

1. **构造**：`MaskedArray.__new__` 里（[core.py:3015-3016](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3015-L3016)），用户传 `fill_value=` 时校验。
2. **finalize**：`__array_finalize__` 里（[core.py:3137-3141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3137-L3141)），子类传播后重新对齐 dtype。
3. **读**：property getter 的惰性初始化（[core.py:3823-3824](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3823-L3824)）。
4. **写**：property setter（[core.py:3836](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3836)）。

#### 4.4.3 源码精读

[core.py:467-517](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L467-L517) 是 `_check_fill_value` 全文。最值得品味的是第 ④ 分支里的异常包装：

```python
try:
    fill_value = np.asarray(fill_value, dtype=ndtype)
except (OverflowError, ValueError) as e:
    # Raise TypeError instead of OverflowError or ValueError.
    # OverflowError is seldom used, and the real problem here is
    # that the passed fill_value is not compatible with the ndtype.
    err_msg = "Cannot convert fill_value %s to dtype %s"
    raise TypeError(err_msg % (fill_value, ndtype)) from e
return np.array(fill_value)
```

注释点明了设计意图：用户传错 `fill_value` 的本质是「类型不兼容」，用 `TypeError` 表达比 `OverflowError` 更准确。最后一句 `return np.array(fill_value)` 保证返回值恒为 0d 数组。

`common_fill_value` 的实现（[core.py:598-628](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L598-L628)）非常短：

```python
t1 = get_fill_value(a)
t2 = get_fill_value(b)
if t1 == t2:
    return t1
return None
```

它是「两个数组能否共用一个填充值」的探测器，主要服务后续的二元运算/拼接（在 [u2-l5 二元运算](u2-l5-binary-ufunc-divide-domain.md) 与 [u2-l8 extras](u2-l8-extras-utilities.md) 里会用到）。

#### 4.4.4 代码实践

**实践目标**：触发 `_check_fill_value` 的不同分支，观察「缺省、强制转换、错误拦截」三种行为。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

# ① 缺省：构造时不传 fill_value，首次访问才懒算
a = ma.array([1, 2, 3], mask=[0, 1, 0])
print('首次访问 :', a.fill_value)             # 999999（int 默认）

# ② 强制转换：给浮点数组传整数 fill_value，会被转成 float
b = ma.array([1., 2.], mask=[1, 0], fill_value=-1)
print('强制转换 :', b.fill_value, type(b.fill_value).__name__)   # -1.0 float

# ③ 类型不兼容：给整数数组传字符串 → TypeError
try:
    c = ma.array([1, 2], fill_value="N/A")
except TypeError as e:
    print('错误拦截 :', e)

# ④ 无符号整数的特殊默认
u = ma.array(np.array([1, 2], dtype='u4'), mask=[0, 1])
print('uint 默认:', u.fill_value, type(u.fill_value).__name__)
```

**需要观察的现象**：

- ① 首次访问 `a.fill_value` 才触发懒计算，得到整数默认 `999999`。
- ② 用户传的 `-1`（int）被 `_check_fill_value` 强制成浮点 `-1.0`，类型名是 `float64`。
- ③ 整数 dtype 传字符串 `"N/A"` 走第 ③ 分支，抛 `TypeError: Cannot set fill value of string with array of dtype ...`。
- ④ `uint` 的默认填充值是 `np.uint64(999999)`，类型名 `uint64`——印证了 `_check_fill_value` 里 `np.uint(...)` 的特判。

**预期结果**：

```
首次访问 : 999999
强制转换 : -1.0 float64
错误拦截 : Cannot set fill value of string with array of dtype int64
uint 默认: 999999 uint64
```

> 待本地验证：第 ③ 条的具体错误文案可能随 NumPy 版本略有差异；若你的环境里整数默认 dtype 是 `int32`，错误信息中的 `int64` 会变成 `int32`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_check_fill_value` 要把 `OverflowError` 也一并捕获并转成 `TypeError`？举一个会触发 `OverflowError` 的例子。

**答案**：因为 `OverflowError` 对调用方而言「语义不清」——它本来多用于 IO/序列化场景，而这里真正的故障是「填充值放不进目标 dtype」。例如给 `int8` 数组设 `fill_value=10**20`，`np.asarray(10**20, dtype='i1')` 会抛 `OverflowError`，包装后用户看到的是清晰的 `TypeError: Cannot convert fill_value ... to dtype int8`。

**练习 2**：`_check_fill_value` 的返回值为什么永远是 0d 数组？这与 4.3 讲的「就地写入」有什么关系？

**答案**：因为 setter 用 `_fill_value[()] = target` 来**就地改内容**以保持传播链共享。这要求 `target` 是一个可索引的数组对象；Python 标量无法用 `[()]` 索引。因此 `_check_fill_value` 统一返回 `np.array(fill_value)`（0d 数组），让 setter 的就地写入语义对所有类型都成立。

## 5. 综合实践

把本讲的四个模块串起来，做一个「**自定义哨兵 + 复用极值归约**」的小任务。

**任务背景**：你有一组传感器读数（浮点），其中部分读数失效需要屏蔽。你想：

1. 用一个**自定义**的、可被同事一眼识别的缺失哨兵（比如 `-8888.`）作为 `fill_value`，而不是默认的 `1e20`。
2. 对屏蔽后的数据求 `max`，确认失效位被正确跳过。
3. 验证 `max` 内部确实用了 `maximum_fill_value`（即 `-inf`）来做「填充后比较」，而不是用你自定义的 `-8888.`。

**参考实现**：

```python
import numpy as np
import numpy.ma as ma

# 1) 自定义哨兵作为 fill_value
raw = [10.0, -999.0, 25.0, 8.0, 30.0]
a = ma.masked_equal(raw, -999.0)     # 屏蔽失效值
a.fill_value = -8888.0                # 改成易识别的哨兵
print('data       :', a.data)
print('mask       :', a.mask)
print('fill_value :', a.fill_value)
print('filled()   :', a.filled())     # 屏蔽位用 -8888. 替换，保持形状

# 2) masked max 跳过失效位
print('a.max()    :', a.max())        # → 30.0，跳过了被屏蔽的 -999

# 3) 印证 max 用的是 maximum_fill_value，而非自定义 fill_value
print('max_fv     :', ma.maximum_fill_value(a))      # → -inf
inner = a.filled(ma.maximum_fill_value(a))           # 屏蔽位变 -inf
print('inner view :', inner)
print('inner max  :', inner.max())                    # → 30.0，与 a.max() 一致
```

**需要观察并解释的现象**：

- `a.filled()` 用的是你**自定义**的 `-8888.`（4.1/4.3 的 fill_value）。
- 但 `a.max()` 内部用的是 `maximum_fill_value(a)` = `-inf`（4.2 的极值填充值），与你的自定义值无关。这印证了一个核心结论：**「展示用的填充值」与「归约用的填充值」是两套独立的值**，前者由 `default_fill_value`/用户设置驱动，后者由 `minimum/maximum_fill_value` 驱动。
- `a.max()` 与 `inner.max()` 结果一致（都是 `30.0`），说明 4.2 讲的「填充 → 普通 max」模型成立。

**预期结果**：

```
data       : [ 10. -999.  25.   8.  30.]
mask       : [False  True False False False]
fill_value : -8888.0
filled()   : [  10. -8888.   25.    8.   30.]
a.max()    : 30.0
max_fv     : -inf
inner view : [  10.  -inf   25.    8.   30.]
inner max  : 30.0
```

## 6. 本讲小结

- `fill_value` 有**两种独立语义**：①`default_fill_value` 给出的「约定俗成缺失标记」（int `999999`、float `1e20`、complex `1e20+0j`）；②`minimum/maximum_fill_value` 给出的「为 min/max 归约服务的类型极值」。
- `default_fill_value` 通过 `_recursive_fill_value` 递归地把一张按 `dtype.kind` 的标量表推广到结构化 dtype（逐字段）与子数组 dtype（`np.full`），结构化结果用 `[()]` 解包成 void 标量。
- **命名陷阱**：`maximum_fill_value` 返回的是 dtype 的**最小**可表示值（浮点是 `-inf`），`minimum_fill_value` 返回**最大**可表示值。名字描述的是「给哪种归约用」，不是「值本身的大小」。
- masked `max`/`min` 的「跳过屏蔽位」靠的是**用极值填充值把屏蔽位伪装成必败者，再复用普通归约**：`self.filled(maximum_fill_value).max()`，随后用 `newmask` 把全屏蔽的位置盖回。
- 读写 `fill_value` 有模块级函数、实例方法、property 三套等价入口；内部**惰性求值**（`_fill_value=None` 表示未算），且**存成 0d 数组**，setter 用 `_fill_value[()] = target` 就地写入以保持子类/视图间的传播共享。
- `_check_fill_value` 是全部读写路径的关卡：负责缺省归一、类型强制（含 uint 特判与结构化分支）、错误拦截（把 `OverflowError/ValueError` 包成 `TypeError`），返回值恒为 0d 数组。`common_fill_value` 则用于判断两数组能否共用一个填充值。

## 7. 下一步学习建议

- 本讲只讲了「极值填充值在 `max`/`min` 里如何决定取舍」。同样的「先填充再普通运算」思想会在 [u2-l5 掩码二元运算与除法域](u2-l5-binary-ufunc-divide-domain.md) 里再次出现——`_MaskedBinaryOperation` 用 `fillx`/`filly` 填充两侧屏蔽位、`_DomainedBinaryOperation` 用域屏蔽除零，建议接着读，体会「填充」在 ufunc 体系里的统一地位。
- 想看「极值填充值 + 排序」如何配合，可继续读 [u2-l7 归约、统计与排序](u2-l7-reductions-stats-sort.md)，那里会讲 `sort`/`argsort` 的 `endwith` 参数如何决定屏蔽元素排在首尾，本质上仍是对 fill_value 的运用。
- 若你对「`_fill_value` 作为 0d 数组在子类/视图间如何传播」感兴趣，应回头精读 [u2-l2 MaskedArray 类与 ndarray 子类化机制](u2-l2-maskedarray-ndarray-subclass.md) 中 `__array_finalize__` 和 `_update_from` 对 `_fill_value` 的搬运逻辑。
- 直接阅读 [core.py:467-517 的 `_check_fill_value`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L467-L517) 与 [core.py:5973-6077 的 `max`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L5973-L6077)，亲手单步跟踪一次 `a.max()` 的调用链，是巩固本讲最快的办法。
