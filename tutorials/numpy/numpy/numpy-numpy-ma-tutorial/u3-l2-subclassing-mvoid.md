# 子类化 MaskedArray 与 mvoid

## 1. 本讲目标

本讲是「专家层」的第二讲，承接 u2-l2（`MaskedArray` 作为 `ndarray` 子类的 `__new__` / `__array_finalize__` / `__array_wrap__` 钩子）。那一讲讲清楚了「掩码如何在切片、视图、`ufunc` 中传播」；本讲要回答的是另一个层面的问题：

> 当我继承了 `MaskedArray`，写了一个带额外属性（比如单位、采样时间戳、来源标签）的子类时，**这些额外信息在一次加法、一次切片之后还在不在？如果不在，我该怎么让它留下？**

围绕这个问题，本讲拆成四个最小模块：

1. **`subok` 与 `get_masked_subclass`**——运算结果到底返回父类 `MaskedArray` 还是你的子类，由谁裁决？
2. **`mvoid`**——结构化 `dtype` 的掩码数组，取一个**标量**元素时得到的「单条屏蔽记录」到底是什么类。
3. **`__array_finalize__` 与 `_optinfo`**——子类属性的正确传递姿势，以及「为什么随手写的 `self.info = ...` 会丢」。
4. **子类化测试范例**——精读 `tests/test_subclassing.py`，把上面三条用断言钉死。

学完本讲，你应当能够：

- 解释 `subok=True/False`、`asarray` / `asanyarray` 对子类的不同处理，并用 `get_masked_subclass` 的源码说明「最年轻子类胜出」规则；
- 说清 `mvoid` 出现的时机、它与 `masked` 单例、与普通 `MaskedArray` 的区别，并能读懂它的 `__getitem__` / `filled` / `tolist`；
- 写出一个「属性在切片和 `ufunc` 后都不丢」的 `MaskedArray` 子类，并知道该把属性放进 `_optinfo` 而不是 `self.__dict__`；
- 看懂 `test_subclassing.py` 里几组关键断言，并能仿照它的风格为自己写测试。

## 2. 前置知识

本讲默认你已经掌握前几讲建立的术语。这里只做一句话复习：

- **三件套**：`MaskedArray` 由 `_data`（含坏值的普通 `ndarray`）、`_mask`（同形布尔，`True` 表示屏蔽，无屏蔽时为单例 `nomask`）、`_fill_value`（对外填充值）组成。
- **`masked` 单例**：全局唯一的「被屏蔽标量」，单个屏蔽元素被取出来时返回它（详见 u3-l3）。
- **`nomask`**：`MaskType(0)`，即布尔 `False`，代表「无屏蔽」的省内存单例，全库用 `is nomask` 做身份判断。
- **子类化三钩子**（u2-l2 详讲）：`__new__` 负责分配内存与初始化属性；`__array_finalize__` 在切片、视图、`np.empty_like` 等「隐式构造」时被调用，负责把属性搬过来；`__array_wrap__` 在 `ufunc` 执行完毕后调用，负责设置结果的 `_mask`。
- **`_update_from`**（u2-l2）：一个「属性搬运工」，把 `_fill_value` / `_hardmask` / `_sharedmask` / `_isfield` / `_baseclass` / `_optinfo` 等簿记属性从一个对象拷到另一个，被两个钩子复用。

一个贯穿全讲的直觉：

> `MaskedArray` 是 `ndarray` 的子类，而它内部的 `_data` **也可以**是 `ndarray` 的某个子类（比如带 `info` 字典的 `SubArray`）。于是「子类」这个词在本讲里有两层含义——一是「`MaskedArray` 的子类」（如 `mvoid`、你自己写的 `MyMA`），二是「`_data` 的子类」（如 `SubArray`）。两者都会被 `subok` / `get_masked_subclass` 照顾到。

## 3. 本讲源码地图

本讲源码集中在 `numpy/ma/core.py` 与 `numpy/ma/tests/test_subclassing.py` 两个文件：

| 位置（行号） | 作用 |
|---|---|
| `core.py:693-717` | `get_masked_subclass`，从一组数组里挑出「最年轻的 `MaskedArray` 子类」作为运算结果类型 |
| `core.py:720-766` | `getdata(a, subok=True)`，取 `_data`，`subok` 控制是否保留 `_data` 的子类 |
| `core.py:1023-1026` | 一元掩码 `ufunc` 用 `get_masked_subclass(a)` 决定结果类型 |
| `core.py:1101-1107` | 二元掩码 `ufunc` 用 `get_masked_subclass(a, b)` 决定结果类型 |
| `core.py:8298` | `inner`/点积用 `get_masked_subclass(a, b)` |
| `core.py:2798-2800` | `MaskedArray.__new__` 文档对 `subok` 的定义 |
| `core.py:2882-3023` | `MaskedArray.__new__`，构造时按 `subok` 决定是否 `.view(type(data))` |
| `core.py:3025-3048` | `_update_from`，搬运 `_optinfo` 等簿记属性（子类信息存活的关键） |
| `core.py:3050-3142` | `__array_finalize__`，隐式构造时的属性继承 |
| `core.py:3143-3201` | `__array_wrap__`，`ufunc` 后 `_update_from(self)` 把属性搬到结果 |
| `core.py:6859-6872` | 模块级 `array(...)`，默认 `subok=True` |
| `core.py:8628-8675` | `asarray`，硬编码 `subok=False`（丢弃子类） |
| `core.py:8678-8725` | `asanyarray`，硬编码 `subok=True`（保留子类） |
| `core.py:6544-6677` | `mvoid` 类，结构化 `dtype` 的单条屏蔽记录 |
| `tests/test_subclassing.py:32-55` | `SubArray`（带 `info` 的 `ndarray` 子类） |
| `tests/test_subclassing.py:58-63` | `SubMaskedArray`（纯 `MaskedArray` 子类，信息存 `_optinfo`） |
| `tests/test_subclassing.py:66-81` | `MSubArray`（同时是 `SubArray` 与 `MaskedArray` 的子类） |
| `tests/test_subclassing.py:196-297` | `test_data_subclassing` / `test_subclasspreservation` 等关键断言 |
| `tests/test_subclassing.py:372-382` | `test_pure_subclass_info_preservation`（`_optinfo` 跨 `ufunc` 存活） |

---

## 4. 核心概念与源码讲解

### 4.1 subok 与 get_masked_subclass：运算的返回类型由谁决定

#### 4.1.1 概念说明

假设你写了一个子类：

```python
class MyMA(np.ma.MaskedArray):
    ...
```

那么 `myma + 1` 的结果，到底是 `MyMA` 还是普通的 `MaskedArray`？答案不是「`__add__` 怎么写」那么简单——`numpy.ma` 的运算结果类型由两套机制共同决定：

1. **构造期的 `subok` 开关**：在 `MaskedArray(...)` / `ma.array(...)` 里，`subok=True`（默认）表示「如果输入已经是某个 `MaskedArray` 子类，就保留它」；`subok=False` 表示「一律降级成普通 `MaskedArray`」。
2. **运算期的 `get_masked_subclass`**：在掩码 `ufunc`（如 `ma.add`、`ma.log`）内部，用一个工具函数从所有参与运算的数组里挑出「最年轻的子类」，把结果 `.view()` 成它。

这两个机制配合 `__array_wrap__`（u2-l2 讲过，`ufunc` 结束后调用）共同保证：**只要输入里有一个是你的子类，结果通常也是你的子类**。

> 名词解释：**最年轻子类（youngest subclass）** 指继承链最靠下的那个类。比如 `MaskedArray` ← `MyMA` ← `MyMA2`，那么 `MyMA2` 比 `MyMA` 更年轻。运算结果会「就高不就低」地取最年轻的那个。

#### 4.1.2 核心流程

`get_masked_subclass(*arrays)` 的判定逻辑可以用伪代码概括：

```
只有一个输入 a：
    若 a 是 MaskedArray（或子类） → 返回 type(a)
    否则                          → 返回 MaskedArray

有多个输入 a, b, ...：
    从第一个的类型出发，逐个检查后面的类型 cls：
        若 cls 是当前结果类型的子类（更年轻） → 更新结果类型为 cls
    最后再兜底：若结果是 MaskedConstant，则退回 MaskedArray
```

关键点有三条：

1. **「更年轻」才覆盖**：`issubclass(cls, rcls)` 为真时才更新，所以多个同级兄弟类型参与运算时，**第一个列出的胜出**（源码注释明确写了 `In case of siblings, the first listed takes over.`）。
2. **非 `MaskedArray` 不参与**：如果第一个参数根本不是 `MaskedArray` 子类，结果类型直接被重置为 `MaskedArray`。
3. **`MaskedConstant` 永不外泄**：因为 `masked` 单例不可复制（见 u3-l3），如果挑出来的是 `MaskedConstant`，要退回 `MaskedArray`，避免制造新的 `masked` 实例破坏单例身份比较。

而 `subok` 的作用点在构造期：它只决定「`.view()` 用 `cls`（`MaskedArray` 本身）还是 `type(data)`（输入的更具体子类）」。

#### 4.1.3 源码精读

先看 `get_masked_subclass` 的完整实现（[core.py:693-717](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L693-L717)）：

```python
def get_masked_subclass(*arrays):
    """
    Return the youngest subclass of MaskedArray from a list of (masked) arrays.
    In case of siblings, the first listed takes over.
    """
    if len(arrays) == 1:
        arr = arrays[0]
        if isinstance(arr, MaskedArray):
            rcls = type(arr)
        else:
            rcls = MaskedArray
    else:
        arrcls = [type(a) for a in arrays]
        rcls = arrcls[0]
        if not issubclass(rcls, MaskedArray):
            rcls = MaskedArray
        for cls in arrcls[1:]:
            if issubclass(cls, rcls):
                rcls = cls
    # Don't return MaskedConstant as result: revert to MaskedArray
    if rcls.__name__ == 'MaskedConstant':
        return MaskedArray
    return rcls
```

这段代码做了三件上面提到的事：单输入直接取 `type`、多输入逐个「更年轻覆盖」、`MaskedConstant` 退回 `MaskedArray`。

再看它在运算里被使用的样子。一元掩码 `ufunc`（如 `ma.log`）在算完结果后，会把结果「升级」成输入的子类（[core.py:1023-1026](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1023-L1026)）：

```python
# Transform to
masked_result = result.view(get_masked_subclass(a))   # ← 升级成 a 的子类
masked_result._mask = m
masked_result._update_from(a)                          # ← 顺便把 a 的属性搬过来
return masked_result
```

二元情形（如 `ma.add`）几乎一样，只是从两侧 `a, b` 里挑（[core.py:1101-1107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1101-L1107)）：

```python
# Transforms to a (subclass of) MaskedArray
masked_result = result.view(get_masked_subclass(a, b))
masked_result._mask = m
if isinstance(a, MaskedArray):
    masked_result._update_from(a)
elif isinstance(b, MaskedArray):
    masked_result._update_from(b)
return masked_result
```

注意一个细节：`_update_from` 只从**第一个** `MaskedArray` 输入搬运属性。这就是为什么「兄弟类型里第一个胜出」不仅是类型层面的规则，也影响属性继承——属性跟着第一个 `MaskedArray` 走。

现在看 `subok` 在构造期的落点。`MaskedArray.__new__` 在把 `_data` 升级成 `MaskedArray` 时，有两条路（[core.py:2897-2908](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2897-L2908)）：

```python
_baseclass = getattr(data, '_baseclass', type(_data))
...
# we must never do .view(MaskedConstant), as that would create a new
# instance of np.ma.masked, which make identity comparison fail
if isinstance(data, cls) and subok and not isinstance(data, MaskedConstant):
    _data = ndarray.view(_data, type(data))   # ← subok=True：保留更具体子类
else:
    _data = ndarray.view(_data, cls)          # ← subok=False：降到 cls（通常是 MaskedArray）
```

这就是 `subok` 的全部秘密：`True` 时 `.view(type(data))`，`False` 时 `.view(cls)`。注释里那句「we must never do .view(MaskedConstant)」和 `get_masked_subclass` 里的 `MaskedConstant` 退回是同一件事的两面——**绝不能让运算或构造凭空产生第二个 `masked` 单例**。

最后看三个入口函数的 `subok` 默认值差异，这是最容易踩坑的地方：

- `ma.array`（[core.py:6859-6872](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6859-L6872)）：默认 `subok=True`，保留子类。
- `ma.asarray`（[core.py:8673-8675](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8673-L8675)）：**硬编码 `subok=False`**，永远返回基类 `MaskedArray`。
- `ma.asanyarray`（[core.py:8678-8725](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8678-L8725)）：硬编码 `subok=True`，保留子类。

```python
# asarray：明确丢弃子类
return masked_array(a, dtype=dtype, copy=False, keep_mask=True,
                    subok=False, order=order)
```

一句话总结：**想让子类穿过构造与运算存活，就用 `asanyarray` / `ma.array`（默认 `subok=True`），别用 `asarray`。**

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `subok` 与 `get_masked_subclass` 如何决定运算结果类型。

**操作步骤**：

```python
# 示例代码
import numpy as np
import numpy.ma as ma

class MyMA(ma.MaskedArray):
    pass

x = MyMA([1, 2, 3, 4, 5], mask=[0, 1, 0, 0, 0])

# 步骤 1：运算结果类型
print("x + 1      的类型：", type(x + 1).__name__)
print("ma.add(x,x)的类型：", type(ma.add(x, x)).__name__)
print("ma.log(x)  的类型：", type(ma.log(x)).__name__)   # 注意有屏蔽位，log 可能报域

# 步骤 2：subok 三个入口的差别
print("ma.array(x, subok=True)  ：", type(ma.array(x, subok=True)).__name__)
print("ma.array(x, subok=False) ：", type(ma.array(x, subok=False)).__name__)
print("ma.asarray(x)            ：", type(ma.asarray(x)).__name__)
print("ma.asanyarray(x)         ：", type(ma.asanyarray(x)).__name__)

# 步骤 3：混合运算——MyMA 与普通 MaskedArray
plain = ma.array([1, 2, 3, 4, 5])
print("ma.add(x, plain) 的类型：", type(ma.add(x, plain)).__name__)   # x 在前
print("ma.add(plain, x) 的类型：", type(ma.add(plain, x)).__name__)   # x 在后
```

**需要观察的现象**：

- 步骤 1 里，`x + 1`、`ma.add`、`ma.log` 的结果都应当是 `MyMA`——因为输入里有 `MyMA`，`get_masked_subclass` 把结果升级成了最年轻的子类。
- 步骤 2 里，`subok=False` 与 `asarray` 应当把结果降级成 `MaskedArray`，其余保留 `MyMA`。
- 步骤 3 里，两次都应是 `MyMA`（因为 `MyMA` 比 `MaskedArray` 更年轻，无论在前在后都会被挑中）。

**预期结果**：步骤 1 全为 `MyMA`；步骤 2 中 `subok=False` 与 `asarray` 为 `MaskedArray`，其余为 `MyMA`；步骤 3 全为 `MyMA`。若 `ma.log(x)` 因为屏蔽位触发了 `RuntimeWarning`，可用 `with np.errstate(invalid='ignore')` 包起来，不影响类型结论。

> 待本地验证：上述类型名结论依赖当前实现，建议实际运行确认；尤其 `x + 1`（走 `__add__` → `__array_wrap__`）与 `ma.add(x, x)`（走掩码 `ufunc` 的 `__call__`）走的是两条不同代码路径，但类型结论应当一致。

#### 4.1.5 小练习与答案

**练习 1**：定义 `class A(ma.MaskedArray)` 与 `class B(A)`，对 `a = A([1,2,3])`、`b = B([1,2,3])`，预测 `ma.add(a, b)` 与 `ma.add(b, a)` 的结果类型。

**答案**：两者都是 `B`。因为 `B` 是 `A` 的子类（更年轻），无论在前在后，`get_masked_subclass` 都会挑出 `B`。

**练习 2**：为什么 `get_masked_subclass` 里要特判 `MaskedConstant` 退回 `MaskedArray`？

**答案**：`masked` 是全局单例（u3-l3 详讲），其身份比较靠 `is`。如果让结果 `.view(MaskedConstant)`，就会凭空造出第二个 `masked` 实例，破坏 `x is np.ma.masked` 这类判断。所以凡是可能产生 `MaskedConstant` 类型的地方都要退回 `MaskedArray`。

---

### 4.2 mvoid：结构化 dtype 的单个屏蔽标量

#### 4.2.1 概念说明

到目前为止，我们见到的 `MaskedArray` 都是「一整块同型数据 + 同形布尔掩码」。但当 `dtype` 是**结构化的**（有字段名，比如 `[('x', float), ('y', float)]`）时，会出现一个新角色：`mvoid`。

先建立一个直觉：

> 一个**普通**的掩码数组，取单个标量元素 `a[i]` 时，得到的是 `masked`（若该元素被屏蔽）或一个普通标量（若未屏蔽）。  
> 一个**结构化**的掩码数组，取单个标量元素 `a[i]` 时，得到的是 **`mvoid`**——「一条带字段级掩码的记录」。

`mvoid` 的名字来自 NumPy 的 `np.void`（结构化数组里单个记录的类型，即 `arr[i]` 的类型）。`mvoid` 就是「masked void」：**一条结构化记录，其中每个字段可以独立地被屏蔽**。它本身是 `MaskedArray` 的子类，但代表的是「0 维的单条记录」。

为什么需要它？因为结构化 dtype 下，掩码也是结构化的（每个字段一个布尔，见 u2-l1 的 `make_mask_descr`）。当你把整条记录取出来时，这条记录的某些字段可能被屏蔽、某些没有，你无法用一个 `True/False` 表达，也无法用 `masked` 单例（那只能表达「整体屏蔽」）。于是需要一个对象，把「这条记录的 data」和「这条记录的逐字段 mask」打包在一起——这就是 `mvoid`。

#### 4.2.2 核心流程

`mvoid` 的典型生命周期：

```
1. 构造一个结构化 dtype 的 MaskedArray A（带逐字段 mask）
2. 取标量：rec = A[0]            ← 触发 MaskedArray.__getitem__，返回 mvoid
3. 访问字段：rec['x']            ← 触发 mvoid.__getitem__，按字段屏蔽语义返回
4. 整条记录屏蔽时：rec 返回的字段会变成 masked
```

`mvoid.__new__` 的构造流程（伪代码）：

```
mvoid(data, mask=nomask, ...):
    _data = np.array(data, ...).view(mvoid)       # 把数据 view 成 mvoid
    _data._hardmask = hardmask
    if mask is not nomask:
        把 mask 规整成 np.void（一条结构化布尔记录）
        特殊处理：mask 可能是 0d 数组、np.void、或需要用 make_mask_descr 重建的列表
    if fill_value is not None:
        _data.fill_value = fill_value
    return _data
```

关键在于 mask 的规整：`mvoid` 的 `_mask` **不是**普通布尔数组，而是一个 `np.void`（结构化标量），其 dtype 由 `make_mask_descr(data.dtype)` 生成——即把 data 的每个字段都换成布尔。

#### 4.2.3 源码精读

先看类定义与 `__new__`（[core.py:6544-6568](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6544-L6568)）：

```python
class mvoid(MaskedArray):
    """
    Fake a 'void' object to use for masked array with structured dtypes.
    """

    def __new__(cls, data, mask=nomask, dtype=None, fill_value=None,
                hardmask=False, copy=False, subok=True):
        copy = None if not copy else True
        _data = np.array(data, copy=copy, subok=subok, dtype=dtype)
        _data = _data.view(cls)
        _data._hardmask = hardmask
        if mask is not nomask:
            if isinstance(mask, np.void):
                _data._mask = mask
            else:
                try:
                    # Mask is already a 0D array
                    _data._mask = np.void(mask)
                except TypeError:
                    # Transform the mask to a void
                    mdtype = make_mask_descr(dtype)
                    _data._mask = np.array(mask, dtype=mdtype)[()]
        if fill_value is not None:
            _data.fill_value = fill_value
        return _data
```

注意 `_mask` 的三路规整：已经是 `np.void` 就直接用；能被 `np.void(...)` 转成 0d 的就转；否则用 `make_mask_descr(dtype)` 重建结构化布尔 dtype 再 `[()]` 取出 void 标量。`[()]` 这个「空元组下标」是 NumPy 里把 0d 结构化数组拆成 `void` 标量的惯用写法（u2-l3 讲填充值时也见过）。

`mvoid` 还重写了 `_data` 这个 property（[core.py:6570-6573](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6570-L6573)），确保读出来的数据是 `np.void` 而不是 0d 数组：

```python
@property
def _data(self):
    # Make sure that the _data part is a np.void
    return super()._data[()]
```

`__getitem__` 是 `mvoid` 最有教学价值的方法（[core.py:6575-6595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6575-L6595)），它处理「按字段取值」的屏蔽语义：

```python
def __getitem__(self, indx):
    m = self._mask
    if isinstance(m[indx], ndarray):
        # Can happen when indx is a multi-dimensional field
        # ...The result is no longer mvoid! See also issue #6724.
        return masked_array(
            data=self._data[indx], mask=m[indx],
            fill_value=self._fill_value[indx],
            hard_mask=self._hardmask)
    if m is not nomask and m[indx]:
        return masked
    return self._data[indx]
```

读懂这段代码的三条规则：

1. **如果取出的字段本身是多维的**（比如字段 `'A'` 是一个形状 `(2,)` 的子数组），那么 `m[indx]` 是个 `ndarray` 而不是单个布尔——此时结果退化回普通的 `masked_array`（不再是 `mvoid`）。源码注释里的例子 `A = masked_array(data=[([0,1],)], ...)` 正是说这种情况。
2. **如果该字段被屏蔽**（`m[indx]` 为真），返回全局单例 `masked`。
3. **否则**返回该字段的原始数据。

`tolist`（[core.py:6656-6677](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6656-L6677)）把 `mvoid` 转成 Python 元组，被屏蔽的字段变成 `None`：

```python
def tolist(self):
    """
    Transforms the mvoid object into a tuple.
    Masked fields are replaced by None.
    """
    _mask = self._mask
    if _mask is nomask:
        return self._data.tolist()
    result = []
    for (d, m) in zip(self._data, self._mask):
        if m:
            result.append(None)
        else:
            # .item() makes sure we return a standard Python object
            result.append(d.item())
    return tuple(result)
```

`filled`（[core.py:6632-6654](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6632-L6654)）借道 `asarray(self).filled(fill_value)[()]`——先把自己当成普通结构化 `MaskedArray` 填充，再 `[()]` 取回 `np.void`：

```python
def filled(self, fill_value=None):
    """Return a copy with masked fields filled with a given value. ..."""
    return asarray(self).filled(fill_value)[()]
```

最后看 `__setitem__`（[core.py:6597-6602](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6597-L6602)），它复现了 u3-l1 讲过的硬/软掩码差异，只不过作用在字段级：

```python
def __setitem__(self, indx, value):
    self._data[indx] = value
    if self._hardmask:
        self._mask[indx] |= getattr(value, "_mask", False)   # 硬：只能 OR
    else:
        self._mask[indx] = getattr(value, "_mask", False)     # 软：直接覆盖
```

#### 4.2.4 代码实践

**实践目标**：构造一个结构化 `dtype` 的掩码数组，取出一条记录，确认它是 `mvoid`，并观察字段级屏蔽。

**操作步骤**：

```python
# 示例代码
import numpy as np
import numpy.ma as ma

# 1. 构造结构化 dtype 的掩码数组，逐字段屏蔽
dt = np.dtype([('x', float), ('y', float)])
A = ma.masked_array(
    data=[(1.0, 2.0), (3.0, 4.0)],
    mask=[(False, True), (True, False)],   # 第一条记录的 y 被屏蔽
    dtype=dt,
)
print("A 的 mask dtype：", A.mask.dtype)   # 应为 [('x','?'),('y','?')]

# 2. 取出一条记录
rec = A[0]
print("rec 的类型：", type(rec).__name__)   # 期望 mvoid
print("rec['x'] =", rec['x'])               # 未屏蔽 → 1.0
print("rec['y'] is ma.masked：", rec['y'] is ma.masked)   # 期望 True

# 3. tolist：屏蔽字段变 None
print("rec.tolist() =", rec.tolist())      # 期望 (1.0, None)

# 4. filled：用填充值替换屏蔽字段，得到 np.void
print("rec.filled(-99.0) =", rec.filled(-99.0))
```

**需要观察的现象**：

- `A.mask.dtype` 是结构化布尔 dtype（`[('x', '|b1'), ('y', '|b1')]`，即 `make_mask_descr` 的产物，见 u2-l1）。
- `A[0]` 的类型是 `mvoid`，而不是 `MaskedArray`，更不是 `masked`。
- `rec['y']` 因为被屏蔽，返回的是 `masked` 单例（`is ma.masked` 为真）。
- `rec.tolist()` 把被屏蔽的 `y` 变成 `None`。

**预期结果**：`rec` 为 `mvoid`；`rec['x'] == 1.0`；`rec['y'] is ma.masked` 为真；`rec.tolist() == (1.0, None)`；`rec.filled(-99.0)` 返回一个 `y` 为 `-99.0` 的 `np.void`。

> 待本地验证：`np.void` 的具体打印格式与 dtype 字节序（`'|b1'` 还是 `'?'`）可能因平台而略有差异，但「逐字段屏蔽」的语义结论稳定。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mvoid.__getitem__` 在 `m[indx]` 是 `ndarray` 时要返回普通 `masked_array` 而不是 `mvoid`？

**答案**：当字段本身是多维子数组（如 `dtype=[("A", ">i2", (2,))]`）时，取出的「字段」是一个有多个元素的数组，不再是「单条记录」。`mvoid` 的语义是「一条 0 维记录」，无法表示多维结果，于是退化回普通 `masked_array`。这正是源码注释引用 issue #6724 的场景。

**练习 2**：`mvoid` 的 `_mask` 为什么是一个 `np.void` 而不是布尔数组？

**答案**：因为结构化 dtype 下，一条记录有多个字段，每个字段需要独立屏蔽。`mvoid` 代表 0 维的单条记录，其 `_mask` 自然也是 0 维的结构化标量（`np.void`），dtype 由 `make_mask_descr` 把每个字段换成布尔得到。这与「普通 `MaskedArray` 的 `_mask` 是同形布尔数组」是一致的，只是降到了 0 维 + 结构化。

---

### 4.3 __array_finalize__ 与 _optinfo：子类属性的正确传递姿势

#### 4.3.1 概念说明

这是本讲最实用的一节。很多初学者写子类时会犯同一个错：

```python
class MyMA(ma.MaskedArray):
    def __new__(cls, data, ...):
        obj = super().__new__(cls, data, ...)
        obj.tag = "hello"        # ← 直接挂一个属性
        return obj
```

然后发现：`MyMA([1,2,3]) + 1` 之后，结果的 `.tag` 不见了，`AttributeError`。

原因在于：**`+1` 走的是 `__array_wrap__`，而 `__array_wrap__` 只调用 `_update_from(self)`**，后者只搬运一组**固定**的簿记属性（`_fill_value` / `_hardmask` / `_sharedmask` / `_isfield` / `_baseclass` / `_optinfo`），并不会搬运你随手挂在 `__dict__` 上的 `tag`。

`numpy.ma` 给子类作者预留的「正规属性口袋」就是 `_optinfo`：一个普通字典。`_update_from` 会把它整体拷过去，于是放在 `_optinfo` 里的东西就能跨切片、跨 `ufunc` 存活。

所以子类化 `MaskedArray` 有两种正确姿势：

- **姿势 A（推荐）**：把要保留的信息存进 `_optinfo`，让 `_update_from` 自动搬运。
- **姿势 B（手动）**：重写 `__array_finalize__`，显式地用 `getattr(obj, 'tag', <默认>)` 把属性搬过来（与 `ndarray` 子类的标准做法一致）。

`test_subclassing.py` 里两套范例并存：`SubMaskedArray` 用姿势 A，`SubArray`（一个 `ndarray` 子类，被包进 `MaskedArray` 当 `_data`）用姿势 B。

> 名词解释：**`_optinfo`** 是 `MaskedArray` 实例上的一个字典属性，专门用来存「运算后希望保留、但不属于固定簿记集合」的可选信息。`_update_from` 把它当成整体搬运。

#### 4.3.2 核心流程

理解属性传递，关键是看清三个钩子里 `_update_from` 的调用链：

```
构造（ma.array / MyMA(...)）
    → MaskedArray.__new__            （显式设置属性，可用 _optinfo）
切片 / 视图 / np.empty_like
    → __array_finalize__(self, obj)
        → _update_from(obj)          （搬运 _optinfo 等）
ufunc（a + b / ma.add）
    → __array_wrap__(self, obj, context)
        → _update_from(self)         （搬运 _optinfo 等）
        （掩码 ufunc 的 __call__ 里也会 _update_from(a)）
```

三条路径都会经过 `_update_from`，所以**只要属性在 `_optinfo` 里，三条路径都能保住它**；反之，若属性只挂在 `__dict__` 上，只有显式重写 `__array_finalize__`（姿势 B）才能在切片/视图时保住它，而 `ufunc` 路径仍会丢（除非你连 `__array_wrap__` 也重写）。

`_update_from` 内部对 `_optinfo` 的处理（[core.py:3034-3048](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3034-L3048)）：

```python
_optinfo = {}
_optinfo.update(getattr(obj, '_optinfo', {}))
_optinfo.update(getattr(obj, '_basedict', {}))
if not isinstance(obj, MaskedArray):
    _optinfo.update(getattr(obj, '__dict__', {}))
_dict = {'_fill_value': ...,
         '_hardmask': ...,
         '_sharedmask': ...,
         '_isfield': ...,
         '_baseclass': ...,
         '_optinfo': _optinfo,
         '_basedict': _optinfo}
self.__dict__.update(_dict)
self.__dict__.update(_optinfo)        # ← _optinfo 里的键被“展开”到 self.__dict__
```

最后那行 `self.__dict__.update(_optinfo)` 是个巧思：`_optinfo` 里的每个键值都被直接挂到实例上，于是你既能用 `self._optinfo['info']` 访问，也能直接用 `self.info` 访问（前提是键叫 `info`）。这就是 `test_attributepropagation` 里 `hasattr(mxsub, 'info')` 为真的原因。

#### 4.3.3 源码精读

先看 `test_subclassing.py` 里的两个范例。

**姿势 A：`SubMaskedArray`**（[tests/test_subclassing.py:58-63](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L58-L63)）把信息塞进 `_optinfo`：

```python
class SubMaskedArray(MaskedArray):
    """Pure subclass of MaskedArray, keeping some info on subclass."""
    def __new__(cls, info=None, **kwargs):
        obj = super().__new__(cls, **kwargs)
        obj._optinfo['info'] = info
        return obj
```

它甚至**不需要**重写 `__array_finalize__`——因为 `_optinfo` 会被 `_update_from` 自动搬运。下面的测试 `test_pure_subclass_info_preservation`（[tests/test_subclassing.py:372-382](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L372-L382)）就验证了这一点：经过 `np.subtract` 和 `-` 运算后，`_optinfo['info']` 依然在：

```python
def test_pure_subclass_info_preservation(self):
    # Test that ufuncs and methods conserve extra information consistently; see gh-7122.
    arr1 = SubMaskedArray('test', data=[1, 2, 3, 4, 5, 6])
    arr2 = SubMaskedArray(data=[0, 1, 2, 3, 4, 5])
    diff1 = np.subtract(arr1, arr2)
    assert_('info' in diff1._optinfo)
    assert_(diff1._optinfo['info'] == 'test')
    diff2 = arr1 - arr2
    assert_('info' in diff2._optinfo)
    assert_(diff2._optinfo['info'] == 'test')
```

**姿势 B：`SubArray`**（[tests/test_subclassing.py:32-47](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L32-L47)）是一个 `ndarray` 子类（不是 `MaskedArray` 子类），它显式重写 `__array_finalize__` 来搬运 `info`：

```python
class SubArray(np.ndarray):
    def __new__(cls, arr, info={}):
        x = np.asanyarray(arr).view(cls)
        x.info = info.copy()
        return x

    def __array_finalize__(self, obj):
        super().__array_finalize__(obj)
        self.info = getattr(obj, 'info', {}).copy()   # ← 手动搬运

    def __add__(self, other):
        result = super().__add__(other)
        result.info['added'] = result.info.get('added', 0) + 1
        return result
```

`SubArray` 的意义在于：它常被当作 `_data` 包进 `MaskedArray`（见 `MSubArray`，[tests/test_subclassing.py:66-81](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L66-L81)）。此时 `_update_from` 走的是 `if not isinstance(obj, MaskedArray): _optinfo.update(getattr(obj, '__dict__', {}))` 这条分支——把 `SubArray` 的 `__dict__`（含 `info`）折进 `_optinfo`，再展开到 `MaskedArray` 实例上。`test_attributepropagation` 最后几行（[tests/test_subclassing.py:267-270](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L267-L270)）正是验证这个「`_data` 子类的属性被提升到 `MaskedArray` 上」：

```python
xsub = subarray(x, info={'name': 'x'})
mxsub = masked_array(xsub)
assert_(hasattr(mxsub, 'info'))
assert_equal(mxsub.info, xsub.info)
```

回到 `__array_finalize__` 本身（[core.py:3050-3056](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3050-L3056)），它第一件事就是调 `_update_from`：

```python
def __array_finalize__(self, obj):
    """
    Finalizes the masked array.
    """
    # Get main attributes.
    self._update_from(obj)
    ...
```

而 `__array_wrap__`（[core.py:3143-3154](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3143-L3154)）在 `ufunc` 后同样调 `_update_from`：

```python
def __array_wrap__(self, obj, context=None, return_scalar=False):
    if obj is self:  # for in-place operations
        result = obj
    else:
        result = obj.view(type(self))
        result._update_from(self)        # ← ufunc 后搬运属性
    ...
```

注意它用 `obj.view(type(self))`——这就是为什么 `ufunc` 结果能保留你的子类类型（与 4.1 的 `get_masked_subclass` 是同一条「保留子类」主线在两套代码路径上的体现：掩码 `ufunc` 走 `__call__` 用 `get_masked_subclass`，普通 `np` `ufunc` 走 `__array_wrap__` 用 `type(self)`）。

#### 4.3.4 代码实践

**实践目标**：亲手对比「随手挂属性（会丢）」与「存进 `_optinfo`（不丢）」两种写法，并写出正确版子类。

**操作步骤**：

```python
# 示例代码
import numpy as np
import numpy.ma as ma

# ---- 反例：随手挂属性，运算后丢失 ----
class BadMA(ma.MaskedArray):
    def __new__(cls, data, tag="default", **kwargs):
        obj = super().__new__(cls, data, **kwargs)
        obj.tag = tag                    # ← 挂在 __dict__，不在 _optinfo
        return obj

b = BadMA([1, 2, 3], tag="hello")
print("构造后  b.tag      =", b.tag)     # 'hello'，还在
try:
    print("加法后  (b+1).tag =", (b + 1).tag)
except AttributeError as e:
    print("加法后丢属性：", e)            # ← 预期：AttributeError

# ---- 正例 A：存进 _optinfo，自动存活 ----
class GoodMA(ma.MaskedArray):
    def __new__(cls, data, tag="default", **kwargs):
        obj = super().__new__(cls, data, **kwargs)
        obj._optinfo['tag'] = tag        # ← 存进口袋
        return obj

g = GoodMA([1, 2, 3], tag="hello")
print("构造后  g._optinfo['tag'] =", g._optinfo['tag'])
print("切片后  g[1:]._optinfo['tag'] =", g[1:]._optinfo['tag'])
print("加法后  (g+1)._optinfo['tag'] =", (g + 1)._optinfo['tag'])

# ---- 正例 B：重写 __array_finalize__，手动搬运 ----
class ManualMA(ma.MaskedArray):
    def __new__(cls, data, tag="default", **kwargs):
        obj = super().__new__(cls, data, **kwargs)
        obj.tag = tag
        return obj

    def __array_finalize__(self, obj):
        super().__array_finalize__(obj)
        # 显式从模板对象搬运；obj 可能为 None（裸构造）
        self.tag = getattr(obj, 'tag', "default")

m = ManualMA([1, 2, 3], tag="hello")
print("构造后  m.tag =", m.tag)
print("切片后  m[1:].tag =", m[1:].tag)   # 走 __array_finalize__，保住
```

**需要观察的现象**：

- `BadMA`：构造后 `b.tag` 在，但 `(b+1).tag` 抛 `AttributeError`——`__array_wrap__` 的 `_update_from` 不搬运 `tag`。
- `GoodMA`：构造、切片、加法三种路径下 `_optinfo['tag']` 都在。
- `ManualMA`：因为重写了 `__array_finalize__`，切片后 `tag` 保住；但请注意**仅靠 `__array_finalize__` 不一定能保住 `ufunc` 后的 `tag`**（`ufunc` 走 `__array_wrap__`，若想连 `ufunc` 也保住，姿势 A 更省心）。

**预期结果**：`BadMA` 加法后丢属性；`GoodMA` 三路径全保留；`ManualMA` 切片保留。

> 待本地验证：`ManualMA` 经 `+1` 后 `.tag` 是否保留取决于 `__array_wrap__` 是否也会触发你重写的 `__array_finalize__`（`.view(type(self))` 会触发）。建议实际运行确认；这正是「姿势 A 更省心」的实证理由——`_optinfo` 同时覆盖三条路径，无需逐个重写钩子。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BadMA` 构造后 `tag` 在，但 `b + 1` 后就没了？

**答案**：构造走 `__new__`，显式设了 `tag`。但 `b + 1` 走 `__array_wrap__`，它创建新对象后只调 `_update_from(self)`，而 `_update_from` 只搬运固定的簿记属性 + `_optinfo`，不搬运 `__dict__` 里随手加的 `tag`，于是结果没有 `tag`。

**练习 2**：`_update_from` 最后那行 `self.__dict__.update(_optinfo)` 有什么好处？

**答案**：它把 `_optinfo` 字典里的每个键直接展开成实例属性。这样子类作者既能用 `self._optinfo['tag']`（字典式）访问，也能直接用 `self.tag`（属性式）访问，使用更自然。代价是 `_optinfo` 的键若与内置属性重名会覆盖，所以应避免与 `_mask`/`_data` 等重名。

---

### 4.4 子类化测试范例：从 test_subclassing.py 学到的四件事

#### 4.4.1 概念说明

`tests/test_subclassing.py` 是 `numpy.ma` 官方对子类化行为的「契约文档」。它用断言把前几节的抽象规则钉死成可执行代码。这一节不引入新机制，而是带读四组最有代表性的测试，让你学会「怎么验证我写的子类是对的」。

四件事分别是：

1. **`_data` 可以是 `ndarray` 子类**：`masked_array(SubArray(...))._data` 仍是 `SubArray`。
2. **`subok` 控制是否保留 `MaskedArray` 子类**：`subok=False` / `asarray` 降级，`subok=True` / `asanyarray` 保留。
3. **`ufunc` 保留最年轻子类**：`add(MSubArray, ...)` 仍是 `MSubArray`。
4. **单个屏蔽元素的取值走 `baseclass`**：`mxcsub[1]` 仍是 `ComplicatedSubArray`，`mxcsub[0]`（屏蔽位）是 `masked`。

#### 4.4.2 核心流程

这几组测试共享一套「构造 → 操作 → `isinstance` / `assert_equal` 断言」的套路。读测试时抓住两个问题就能定位它的意图：

- 它在断言**结果的类型**（`isinstance(z, SomeClass)`），还是在断言**结果的值**（`assert_equal(...)`）？
- 它走的是**构造路径**（`masked_array` / `asarray`）、**切片路径**（`a[1:]`）、还是 **`ufunc` 路径**（`a + 1` / `ma.add`）？

#### 4.4.3 源码精读

**第一件事：`_data` 保留 `ndarray` 子类**——`test_data_subclassing`（[tests/test_subclassing.py:196-204](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L196-L204)）：

```python
def test_data_subclassing(self):
    # Tests whether the subclass is kept.
    x = np.arange(5)
    m = [0, 0, 1, 0, 0]
    xsub = SubArray(x)
    xmsub = masked_array(xsub, mask=m)
    assert_(isinstance(xmsub, MaskedArray))
    assert_equal(xmsub._data, xsub)
    assert_(isinstance(xmsub._data, SubArray))   # ← _data 仍是 SubArray
```

这条断言对应 README 里「`_data` 可以是任意 `ndarray` 子类」的设计目标（见本讲 4.5 引用的 README 第 59 行）。

**第二件事：`subok` 开关**——`test_subclasspreservation`（[tests/test_subclassing.py:272-297](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L272-L297)），节选关键四段：

```python
def test_subclasspreservation(self):
    x = np.arange(5)
    m = [0, 0, 1, 0, 0]
    xinfo = list(zip(x, m))
    xsub = MSubArray(x, mask=m, info={'xsub': xinfo})
    #
    mxsub = masked_array(xsub, subok=False)
    assert_(not isinstance(mxsub, MSubArray))    # ← 降级
    assert_(isinstance(mxsub, MaskedArray))
    assert_equal(mxsub._mask, m)
    #
    mxsub = asarray(xsub)
    assert_(not isinstance(mxsub, MSubArray))    # ← asarray 也降级
    assert_(isinstance(mxsub, MaskedArray))
    #
    mxsub = masked_array(xsub, subok=True)
    assert_(isinstance(mxsub, MSubArray))        # ← subok=True 保留
    assert_equal(mxsub.info, xsub.info)
    assert_equal(mxsub._mask, xsub._mask)
    #
    mxsub = asanyarray(xsub)
    assert_(isinstance(mxsub, MSubArray))        # ← asanyarray 保留
    assert_equal(mxsub.info, xsub.info)
```

这正是 4.1.3 讲的「`subok=False` / `asarray` 降级、`subok=True` / `asanyarray` 保留」的可执行契约。

**第三件事：`ufunc` 保留最年轻子类**——`test_attributepropagation`（[tests/test_subclassing.py:239-260](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L239-L260)），节选：

```python
ym = msubarray(x)                       # MSubArray 实例
...
z = (ym + 1)
assert_(isinstance(z, MaskedArray))
assert_(isinstance(z, MSubArray))       # ← +1 后仍是 MSubArray
assert_(isinstance(z._data, SubArray))  # ← _data 仍是 SubArray
assert_(z._data.info['added'] > 0)      # ← 走了 SubArray.__add__，计数 +1
# Test that inplace methods from data get used (gh-4617)
ym += 1
assert_(isinstance(ym, MSubArray))
assert_(ym._data.info['iadded'] > 0)    # ← 原地加法也走 SubArray.__iadd__
```

这段断言同时验证了三件事：`MSubArray` 类型保留、`SubArray` 的 `_data` 类型保留、以及 `_data` 子类自定义的 `__add__` / `__iadd__` 真的被调用（`info['added']` / `info['iadded']` 自增）。

**第四件事：单个屏蔽元素走 `baseclass`**——`test_subclass_items`（[tests/test_subclassing.py:299-320](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L299-L320)），节选：

```python
xcsub = ComplicatedSubArray(x)
mxcsub = masked_array(xcsub, mask=[True, False, True, False, False])
...
# now that it propagates inside the MaskedArray
assert_(isinstance(mxcsub[1], ComplicatedSubArray))        # ← 未屏蔽单元素：baseclass
assert_(isinstance(mxcsub[1, ...].data, ComplicatedSubArray))
assert_(mxcsub[0] is masked)                               # ← 屏蔽单元素：masked 单例
assert_(isinstance(mxcsub[0, ...].data, ComplicatedSubArray))
```

`ComplicatedSubArray` 重写了 `__getitem__`，确保「即便是标量也返回自己的类型」（[tests/test_subclassing.py:136-141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py#L136-L141)）。这个测试说明：`MaskedArray.__getitem__` 对未屏蔽的单元素会**走 `_data` 的 `__getitem__`**（于是 `ComplicatedSubArray.__getitem__` 生效），而对屏蔽的单元素返回 `masked`。`mxcsub[0, ...]` 与 `mxcsub[0]` 的区别在于：前者用 `...` 保留数组维度，于是走的是「数组结果」分支而非「标量」分支，故 `.data` 仍是 `ComplicatedSubArray`。

#### 4.4.4 代码实践

**实践目标**：仿照 `test_subclassing.py` 的风格，为自己的子类写一组断言。

**操作步骤**：

```python
# 示例代码（可作为 pytest 用例保存为 test_myma.py）
import numpy as np
import numpy.ma as ma
from numpy.ma.core import MaskedArray
from numpy.testing import assert_

class MyMA(ma.MaskedArray):
    def __new__(cls, data, tag="default", **kwargs):
        obj = super().__new__(cls, data, **kwargs)
        obj._optinfo['tag'] = tag       # 姿势 A：存进口袋
        return obj

def test_subclass_type_preserved_on_ufunc():
    a = MyMA([1, 2, 3, 4, 5], tag="hello", mask=[0, 1, 0, 0, 0])
    z = a + 1
    assert_(isinstance(z, MaskedArray))
    assert_(isinstance(z, MyMA))               # ufunc 保留子类
    assert_(z._optinfo['tag'] == "hello")      # _optinfo 跨 ufunc 存活

def test_subok_drops_subclass():
    a = MyMA([1, 2, 3], tag="hello")
    assert_(isinstance(ma.asanyarray(a), MyMA))   # asanyarray 保留
    assert_(not isinstance(ma.asarray(a), MyMA))  # asarray 降级
    assert_(isinstance(ma.asarray(a), MaskedArray))

def test_slice_keeps_optinfo():
    a = MyMA([1, 2, 3, 4, 5], tag="hello")
    assert_(a[1:]._optinfo['tag'] == "hello")     # 切片保住 _optinfo

# 运行：pytest test_myma.py -v
```

**需要观察的现象**：三条断言全部通过，分别对应「`ufunc` 保留子类 + `_optinfo`」、「`subok` 开关」、「切片保住 `_optinfo`」三个行为契约。

**预期结果**：三条用例全绿。若 `test_subclass_type_preserved_on_ufunc` 失败，多半是忘了把 `tag` 存进 `_optinfo`（或写成了普通属性）。

> 待本地验证：在仓库根目录用 `python -m pytest numpy/ma/tests/test_myma.py -v`（或把文件放进 `numpy/ma/tests/`）实际跑一遍确认。

#### 4.4.5 小练习与答案

**练习 1**：`test_attributepropagation` 里 `my + 1`（`my = masked_array(subarray(x))`）的结果**不是** `MSubArray`，但 `ym + 1`（`ym = msubarray(x)`）是。为什么？

**答案**：`my` 是用 `masked_array(SubArray(...))` 构造的，它本身是普通 `MaskedArray`（`_data` 是 `SubArray`），并不是 `MSubArray`。运算结果类型由「参与运算的 `MaskedArray` 子类」决定，`my` 不是 `MSubArray`，所以结果也不是；只有 `_data` 那一层保住了 `SubArray`。而 `ym` 本身就是 `MSubArray`，结果自然也是。

**练习 2**：`mxcsub[0]` 与 `mxcsub[0, ...]` 有何区别？为什么前者是 `masked`，后者的 `.data` 是 `ComplicatedSubArray`？

**答案**：`mxcsub[0]` 取单个标量，且该位置被屏蔽，`MaskedArray.__getitem__` 直接返回 `masked` 单例。`mxcsub[0, ...]` 加了 `...`，保留了数组维度，走的是「数组结果」分支——返回一个 0 维 `MaskedArray`，其 `_data` 来自 `ComplicatedSubArray.__getitem__`，故 `.data` 仍是 `ComplicatedSubArray`。这正是「标量分支 vs 数组分支」的差别。

---

## 5. 综合实践

把本讲四条主线串起来，完成一个小任务：**写一个「带单位标签」的掩码数组子类 `QuantityMA`，要求它在切片、`ufunc`、`asanyarray` 后都保留单位，并能正确处理结构化 dtype 的字段级屏蔽。**

建议步骤：

1. **定义子类**（姿势 A）：

   ```python
   # 示例代码
   import numpy as np
   import numpy.ma as ma

   class QuantityMA(ma.MaskedArray):
       def __new__(cls, data, units="dimensionless", mask=ma.nomask, **kwargs):
           obj = super().__new__(cls, data, mask=mask, **kwargs)
           obj._optinfo['units'] = units      # 存进口袋，跨运算存活
           return obj

       @property
       def units(self):
           return self._optinfo.get('units', 'dimensionless')
   ```

2. **验证类型与单位保留**（对照 4.1、4.3）：

   ```python
   q = QuantityMA([1.0, 2.0, 3.0, 4.0], units="m", mask=[0, 1, 0, 0])
   assert type(q + 1.0) is QuantityMA          # ufunc 保留
   assert (q + 1.0).units == "m"               # _optinfo 跨 ufunc
   assert type(q[1:]) is QuantityMA            # 切片保留
   assert q[1:].units == "m"
   assert type(ma.asanyarray(q)) is QuantityMA # asanyarray 保留
   assert type(ma.asarray(q)) is ma.MaskedArray  # asarray 降级（不是 QuantityMA）
   ```

3. **验证 `subok` 与混合运算**（对照 4.1）：

   ```python
   plain = ma.array([1.0, 2.0, 3.0, 4.0])
   assert type(ma.add(q, plain)) is QuantityMA   # q 在前，最年轻子类胜出
   ```

4. **验证结构化 dtype 的字段级屏蔽**（对照 4.2）：

   ```python
   dt = np.dtype([('x', float), ('y', float)])
   A = QuantityMA([(1.0, 2.0), (3.0, 4.0)],
                  units="m",
                  mask=[(False, True), (True, False)], dtype=dt)
   rec = A[0]
   assert type(rec).__name__ == 'mvoid'         # 结构化单条记录是 mvoid
   assert rec['y'] is ma.masked                 # 字段级屏蔽
   assert rec.tolist() == (1.0, None)           # tolist 屏蔽字段变 None
   ```

5. **写成 pytest**（对照 4.4）：把第 2–4 步的断言整理成 `test_quantityma.py`，放到 `numpy/ma/tests/` 下，用 `python -m pytest numpy/ma/tests/test_quantityma.py -v` 跑通。

> 待本地验证：第 4 步里 `QuantityMA` 用结构化 dtype 构造、再取 `A[0]` 是否一定得到 `mvoid`，取决于 `MaskedArray.__getitem__` 对结构化标量的分支。建议实际运行确认；若得到的是别的类型，回顾本讲 4.2.3 里 `__getitem__` 对多维字段的退化分支。

完成本任务后，你就把「`subok` / `get_masked_subclass` 决定类型」「`_optinfo` 保留属性」「`mvoid` 表示字段级屏蔽」「用测试钉死契约」四件事融会贯通了。

## 6. 本讲小结

- **运算结果类型由两套机制决定**：构造期的 `subok` 开关（`ma.array` 默认 `True`、`asarray` 硬编码 `False`、`asanyarray` 硬编码 `True`）与运算期的 `get_masked_subclass`（挑「最年轻子类」，兄弟类型第一个胜出，`MaskedConstant` 退回 `MaskedArray`）。
- **`get_masked_subclass`** 在掩码 `ufunc` 的 `__call__`（一元 [core.py:1023](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1023)、二元 [core.py:1101](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1101)）以及点积 [core.py:8298](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L8298) 等处被调用，把结果 `.view()` 成最年轻子类。
- **`mvoid`** 是结构化 dtype 掩码数组取单条记录时的返回类型（[core.py:6544](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6544)），其 `_mask` 是 `np.void`（逐字段布尔），`__getitem__` 按字段屏蔽语义返回 `masked` 或原值，多维字段会退化回普通 `masked_array`。
- **子类属性要放进 `_optinfo`**：`_update_from`（[core.py:3025](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3025)）只搬运固定簿记属性 + `_optinfo`，并在最后把 `_optinfo` 展开到 `__dict__`。随手挂在 `__dict__` 的属性会在 `ufunc` 后丢失。
- **三个钩子都过 `_update_from`**：`__new__` 显式设、`__array_finalize__`（切片/视图）与 `__array_wrap__`（`ufunc`）都调它；所以 `_optinfo` 能同时覆盖三条路径，是最省心的属性口袋。
- **`test_subclassing.py` 是契约文档**：`test_data_subclassing`（`_data` 子类保留）、`test_subclasspreservation`（`subok` 开关）、`test_attributepropagation`（`ufunc` 保留最年轻子类）、`test_subclass_items`（单元素走 `baseclass`，屏蔽位返 `masked`）四组断言把子类化行为钉死。

## 7. 下一步学习建议

- **u3-l3（`masked` 单例与打印）**：本讲多次提到「屏蔽单元素返回 `masked`」「`MaskedConstant` 不可复制」，下一讲会彻底讲清这个全局单例的构造、不可变保护与打印符号 `--` 的实现。
- **u3-l4（pickle、重建与深拷贝）**：子类化引入的「类型」与「`_optinfo`」在序列化时如何被还原？`_mareconstruct` 如何在反序列化时重建子类？这是子类化的下一个深度。
- **u3-l5（mrecords 与字段级屏蔽）**：本讲的 `mvoid` 只是「单条屏蔽记录」，`mrecords` 则把整个结构化数组升级为支持字段级屏蔽的 `mrecarray`，是 `mvoid` 思想的数组级推广。
- **继续读源码**：精读 [core.py:2882-3023](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2882-L3023)（`__new__`）与 [tests/test_subclassing.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_subclassing.py) 全文，并把本讲「综合实践」的 `QuantityMA` 跑通，是检验你是否真正掌握子类化的最佳方式。
