# MaskedArray 类与 ndarray 子类化机制

## 1. 本讲目标

学完本讲后，你应该能够：

- 理解 `MaskedArray` 之所以是 `ndarray` 的子类（`class MaskedArray(ndarray)`），以及这种设计带来的收益与代价。
- 说清 `__new__` 如何从一段普通数据「原地升级」出一个带掩码的数组，而不复制数据。
- 说清 `_update_from` 这个属性搬运工做了什么、为什么需要它。
- 说清 `__array_finalize__` 为什么被 NumPy 官方注释称作「guesswork and heuristics」（猜测与启发式），以及它在视图、切片、ufunc 输出时如何被触发又被谁覆盖。
- 说清 `__array_wrap__` 作为 ufunc 专用钩子，如何根据输入掩码与「域(domain)」计算真正的输出掩码，从而覆盖 `__array_finalize__` 的默认猜测。
- 能够对一段掩码数组做切片与加法，结合源码解释 `.mask` 为何这样传播。

## 2. 前置知识

本讲建立在前面几讲已建立的概念之上，这里只做最简回顾：

- **掩码数组三件套**：`data`（含坏值的原始数据）、`mask`（同形状布尔数组，`True` 表示屏蔽；无屏蔽时压缩为单例 `nomask`）、`fill_value`（屏蔽位对外填充值）。详见 u1-l4。
- **`nomask` 单例**：定义在 [core.py:L87-L88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88) 为 `MaskType = np.bool`、`nomask = MaskType(0)`，即 `np.False_`。全库用 `is nomask` 做身份判断以省内存。详见 u2-l1。
- **`getmask` / `getmaskarray`**：前者忠实返回内部 `_mask`（可能为 `nomask`），后者永远返回同形状全布尔数组。详见 u2-l1。
- **`make_mask_descr`**：把任意 dtype 递归地映射为布尔掩码 dtype。详见 u2-l1。
- **什么是 ndarray 子类**：NumPy 的 `ndarray` 是用 C 实现的底层缓冲区容器。要在 Python 层扩展它（加属性、改行为），标准做法是 `class MyArray(ndarray)`，并实现若干「钩子方法」让 NumPy 在创建新数组时通知子类。本讲要讲的四个钩子就是这个机制的核心。

如果你对「为什么子类化 ndarray 要写 `__new__` 而不是 `__init__`」还不熟悉，本讲第 4.1 节会顺带解释。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 一个文件里：

| 位置 | 作用 |
| --- | --- |
| [core.py:L2770](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2770) | `class MaskedArray(ndarray):` 类声明，以及紧随其后的类级属性（`__array_priority__`、`_baseclass` 等）。 |
| [core.py:L2882-L3023](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2882-L3023) | `MaskedArray.__new__`：从零构造一个掩码数组。 |
| [core.py:L3025-L3048](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3025-L3048) | `_update_from`：把模板对象的「簿记属性」搬到 `self`。 |
| [core.py:L3050-L3141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3050-L3141) | `__array_finalize__`：视图/切片/ufunc 输出创建时被调用，用启发式决定 mask。 |
| [core.py:L3143-L3201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3143-L3201) | `__array_wrap__`：ufunc 计算完成后被调用，根据输入 mask 与 domain 算出真正输出 mask。 |
| [core.py:L3277-L3400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3277-L3400) | `__getitem__`：索引/切片入口，会显式切分 mask，是理解「切片时 mask 如何传播」的关键（见第 4.3 节实践）。 |
| [core.py:L6859-L6872](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6859-L6872) | 模块级 `array(...)` 函数：只是转发的薄壳，最终进入 `MaskedArray.__new__`。 |

辅助函数（本讲会引用但不展开）：

- [core.py:L1759](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1759) `mask_or`：用 logical_or 合并两个掩码，含 `nomask` 短路。
- [core.py:L844-L845](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L844-L845) `ufunc_domain = {}`、`ufunc_fills = {}`：注册每个 ufunc 的「域检查函数」与「填充值」，供 `__array_wrap__` 使用（域机制的深入讲解见 u2-l4）。
- [core.py:L467](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L467) `_check_fill_value`：校验并规整 `fill_value`。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：`__new__` 构造流程、`_update_from` 属性复制、`__array_finalize__` mask 传播启发式、`__array_wrap__` ufunc 钩子。它们之间有清晰的依赖与分工，最后在第 5 节综合实践里串成一条完整的「创建 → 视图/切片 → ufunc」调用链。

### 4.1 `__new__` 构造流程

#### 4.1.1 概念说明

在 Python 中，普通对象的初始化靠 `__init__`；但 `ndarray` 的内存是在 `__new__` 阶段分配的，所以**子类化 ndarray 必须重写 `__new__` 而不是 `__init__`**。`MaskedArray.__new__` 的职责是：

1. 把任意 `data` 规整成一个普通 `ndarray`（底层缓冲区）。
2. 用 `ndarray.view(...)` 把这个普通数组「原地升级」为 `MaskedArray`（或其子类）——**不复制数据**，只是换一个类型标签并挂上额外属性。
3. 规整 `mask`：处理 `nomask`、标量布尔掩码、形状不一致、`keep_mask` 合并等。
4. 规整 `fill_value`、`hard_mask`、`_baseclass`。
5. 返回这个已经「武装好」的对象。

注意：模块级 [array(...)](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6859-L6872) 函数只是把参数换个顺序后转发给 `MaskedArray.__new__`，而 `masked_array` 直接是 `MaskedArray` 的别名（[core.py:L6856](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6856)）。所以 `ma.array`、`ma.masked_array`、`MaskedArray(...)` 三者最终都走同一个 `__new__`。

#### 4.1.2 核心流程

`__new__` 的骨架可以概括为下面这段伪代码：

```
__new__(cls, data, mask=nomask, ..., subok=True, keep_mask=True, shrink=True, ...):
    # (1) 规整数据
    _data = np.array(data, dtype=dtype, copy=copy, subok=True, ndmin=ndmin)
    _baseclass = getattr(data, '_baseclass', type(_data))

    # (2) 原地升级类型（不复制数据）
    if isinstance(data, cls) and subok:
        _data = ndarray.view(_data, type(data))   # 保留更具体的子类
    else:
        _data = ndarray.view(_data, cls)          # 升级为 MaskedArray

    mdtype = make_mask_descr(_data.dtype)

    # (3) 规整掩码：两条大分支
    if mask is nomask:
        # Case 1：调用者没给 mask
        #   - keep_mask=False 时按 shrink 决定 nomask 或全 False
        #   - data 本身是 MaskedArray 时，继承其 _mask（copy 时拷贝，否则共享）
        #   - data 是 (list/tuple) of MaskedArray 时，尝试逐元素取 mask
    else:
        # Case 2：调用者显式给了 mask
        #   - 标量 True/False 展开为同形状全 True/全 False
        #   - 形状不一致时 resize/reshape 或抛 MaskError
        #   - keep_mask=True 时与 data 原有 mask 做 logical_or 合并

    # (4) 规整 fill_value / hard_mask / baseclass
    _data._fill_value = _check_fill_value(fill_value, _data.dtype)
    _data._hardmask   = ...
    _data._baseclass  = _baseclass
    return _data
```

两个值得记住的设计：

- **`subok` 控制「是否保留更具体的子类」**。当传入的 `data` 已经是某个 `MaskedArray` 子类（例如 `mrecarray`）时，`subok=True` 会让结果保留该子类类型，`subok=False` 则降级为纯 `MaskedArray`。
- **`keep_mask` 控制「新 mask 与旧 mask 是合并还是覆盖」**。`keep_mask=True`（默认）时，显式传入的 `mask` 会与 `data` 自带的 `_mask` 做 `logical_or` 合并；`keep_mask=False` 时直接用新 `mask` 覆盖。

#### 4.1.3 源码精读

**第 1 步：规整数据并原地升级类型**，见 [core.py:L2893-L2908](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2893-L2908)：

```python
# Process data.
copy = None if not copy else True
_data = np.array(data, dtype=dtype, copy=copy, order=order, subok=True, ndmin=ndmin)
_baseclass = getattr(data, '_baseclass', type(_data))
...
# Here, we copy the _view_, so that we can attach new properties to it
if isinstance(data, cls) and subok and not isinstance(data, MaskedConstant):
    _data = ndarray.view(_data, type(data))
else:
    _data = ndarray.view(_data, cls)
```

关键点：`ndarray.view(_data, cls)` 把同一段内存缓冲区重新解释为 `MaskedArray` 类型，**没有数据拷贝**。注意它特意避开 `MaskedConstant`（即全局单例 `ma.masked`），因为给单例做 `.view` 会破坏身份比较（u3-l3 会详讲）。

**第 3 步 Case 1（没有传入 mask）**，见 [core.py:L2918-L2954](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2918-L2954)，核心是「继承 `data` 自带的 mask」：

```python
if mask is nomask:
    if not keep_mask:
        if shrink: _data._mask = nomask
        else:      _data._mask = np.zeros(_data.shape, dtype=mdtype)
    elif isinstance(data, (tuple, list)):
        # data 是一组 masked array，逐元素取 getmaskarray 拼成 mask
        ...
    else:
        _data._sharedmask = not copy
        if copy:
            _data._mask = _data._mask.copy()
            ...
```

注意 `_data._sharedmask = not copy`：默认（`copy=False`）时新数组与原数据**共享**同一个 mask 对象，只有显式 `copy=True` 才会复制 mask。

**第 3 步 Case 2（显式传入 mask）**，见 [core.py:L2955-L3009](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2955-L3009)。其中 `keep_mask=True` 时用 `logical_or` 合并新旧掩码：

```python
else:
    if _data.dtype.names is not None:
        def _recursive_or(a, b):        # 结构化 dtype 逐字段合并
            ...
        _recursive_or(_data._mask, mask)
    else:
        _data._mask = np.logical_or(mask, _data._mask)
    _data._sharedmask = False
```

合并后置 `_sharedmask = False`，因为 mask 已经被改写、不再与原始共享。

**第 4 步：fill_value / hard_mask / baseclass**，见 [core.py:L3011-L3022](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3011-L3022)：

```python
if fill_value is None:
    fill_value = getattr(data, '_fill_value', None)
if fill_value is not None:
    _data._fill_value = _check_fill_value(fill_value, _data.dtype)
if hard_mask is None:
    _data._hardmask = getattr(data, '_hardmask', False)
else:
    _data._hardmask = hard_mask
_data._baseclass = _baseclass
return _data
```

`_baseclass` 记录「这个 MaskedArray 在剥掉 mask 语义后，本质是哪种 ndarray」（普通 `ndarray`、`matrix`、或某个子类），它会在 `.data`、`filled()` 等地方用来还原成「裸」数组。

#### 4.1.4 代码实践

1. **实践目标**：验证 `ma.array` 不复制数据，以及 `subok` 与 `keep_mask` 的作用。
2. **操作步骤**：

```python
import numpy as np
import numpy.ma as ma

base = np.arange(6)                       # 普通数组
a = ma.array(base, mask=[0,1,0,0,1,0])    # 不传 copy，默认 False

# (1) 验证数据共享：修改 base 看 a.data 是否跟着变
base[0] = 999
print("a.data[0] =", a.data[0])           # 预期 999，说明未复制数据

# (2) 验证 keep_mask 合并
b = ma.array([1,2,3], mask=[1,0,0])       # data 无 mask，mask 直接用
c = ma.array(b, mask=[0,1,0], keep_mask=True)
print("c.mask =", c.mask)                 # 预期 [True, True, False]（逻辑或）
d = ma.array(b, mask=[0,1,0], keep_mask=False)
print("d.mask =", d.mask)                 # 预期 [False, True, False]（覆盖）
```

3. **需要观察的现象**：`a.data[0]` 跟随 `base` 变化；`c.mask` 是 `b.mask` 与新 mask 的或，`d.mask` 是新 mask 覆盖。
4. **预期结果**：`a.data[0] = 999`；`c.mask = [ True True False]`；`d.mask = [False True False]`。
5. 数据共享行为已可由源码确定；若你在不同 NumPy 版本上观察 `base[0] = 999` 后 `a.data[0]` 的值，请以实际为准，此处标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `MaskedArray` 重写的是 `__new__` 而不是 `__init__`？

**答案**：因为 `ndarray` 的内存缓冲区在 `__new__` 阶段就由 C 层分配好了，`__init__` 时缓冲区已经定型、无法再附加子类所需的属性挂载流程。子类必须在自己的 `__new__` 里完成「分配 → view 升级 → 挂属性」这一整套动作。

**练习 2**：`ma.array(data, mask=m, keep_mask=True)` 中，如果 `data` 本身已经带 mask，最终 mask 是什么？

**答案**：是 `data` 原有 mask 与 `m` 的按位逻辑或（`np.logical_or`）；对结构化 dtype 则逐字段做或。若想用 `m` 覆盖而非合并，需显式传 `keep_mask=False`。

### 4.2 `_update_from` 属性复制

#### 4.2.1 概念说明

`_update_from(self, obj)` 是一个**属性搬运工**：它把模板对象 `obj` 上的「簿记属性」（`_fill_value`、`_hardmask`、`_sharedmask`、`_isfield`、`_baseclass`、`_optinfo`、`_basedict`）搬到 `self` 上。它**不**搬运数据缓冲区，也**不**搬运 mask——那两者由更专门的逻辑处理。

它被两个钩子复用：
- `__array_finalize__` 在第一步调用它（见 4.3）。
- `__array_wrap__` 在把 ufunc 结果重新 view 成子类后调用它（见 4.4）。

之所以单独抽出来，是因为「把一组附加属性从一个数组搬到另一个数组」这个动作在多处都需要，避免重复代码。

#### 4.2.2 核心流程

```
_update_from(self, obj):
    _baseclass = type(obj) if isinstance(obj, ndarray) else ndarray
    _optinfo = {}
    _optinfo.update(obj._optinfo)            # 合并两份可选信息
    _optinfo.update(obj._basedict)
    if not isinstance(obj, MaskedArray):
        _optinfo.update(obj.__dict__)        # 非 MA 对象：把整个 __dict__ 也并进来
    _dict = {
        '_fill_value':  getattr(obj, '_fill_value', None),
        '_hardmask':    getattr(obj, '_hardmask', False),
        '_sharedmask':  getattr(obj, '_sharedmask', False),
        '_isfield':     getattr(obj, '_isfield', False),
        '_baseclass':   getattr(obj, '_baseclass', _baseclass),
        '_optinfo':     _optinfo,
        '_basedict':    _optinfo,
    }
    self.__dict__.update(_dict)
    self.__dict__.update(_optinfo)
```

几个要点：

- 全程用 `getattr(obj, name, default)` 取值，对「`obj` 不是 MaskedArray」也安全——这正是 `__array_finalize__` 可能在普通 `ndarray` 上被调用时的兜底。
- `_optinfo` 与 `_basedict` 存的是「可选的、可被子类自定义的」扩展信息，合并时**新建字典**（注释明确说「avoid backward propagation」，避免反向传播）。
- `_sharedmask` 在这里被设成模板的值（默认 `False`），但后续路径（如 `__getitem__` 的 `dout._sharedmask = True`，或 `__array_wrap__` 的 `result._sharedmask = False`）会覆盖它。

#### 4.2.3 源码精读

见 [core.py:L3025-L3048](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3025-L3048)：

```python
def _update_from(self, obj):
    """Copies some attributes of obj to self."""
    if isinstance(obj, ndarray):
        _baseclass = type(obj)
    else:
        _baseclass = ndarray
    # We need to copy the _basedict to avoid backward propagation
    _optinfo = {}
    _optinfo.update(getattr(obj, '_optinfo', {}))
    _optinfo.update(getattr(obj, '_basedict', {}))
    if not isinstance(obj, MaskedArray):
        _optinfo.update(getattr(obj, '__dict__', {}))
    _dict = {'_fill_value': getattr(obj, '_fill_value', None),
                 '_hardmask': getattr(obj, '_hardmask', False),
                 '_sharedmask': getattr(obj, '_sharedmask', False),
                 '_isfield': getattr(obj, '_isfield', False),
                 '_baseclass': getattr(obj, '_baseclass', _baseclass),
                 '_optinfo': _optinfo,
                 '_basedict': _optinfo}
    self.__dict__.update(_dict)
    self.__dict__.update(_optinfo)
```

注意最后一行 `self.__dict__.update(_optinfo)`：把 `_optinfo` 里的键直接摊平到 `self.__dict__`。这意味着子类如果把自定义属性塞进 `_optinfo`，它们会作为普通属性出现在实例上——这是子类扩展状态的传播通道（u3-l2 子类化讲义会用到）。

#### 4.2.4 代码实践

1. **实践目标**：观察 `_update_from` 让一个空壳视图继承模板的 `fill_value`。
2. **操作步骤**：

```python
import numpy as np, numpy.ma as ma

a = ma.array([1.0, 2.0, 3.0], mask=[0,1,0])
a.set_fill_value(-99.0)

# 用 ndarray.view 直接造一个「裸」MaskedArray 视图（绕过 __new__），
# 它此时还没有 fill_value；调用 _update_from 从 a 继承
raw = a.data.view(ma.MaskedArray)          # 只有数据，没有 MA 属性
print("before _update_from, fill_value =", getattr(raw, '_fill_value', '<missing>'))
raw._update_from(a)
print("after  _update_from, fill_value =", raw._fill_value)
```

3. **需要观察的现象**：`view` 之后 `_fill_value` 还不存在；`_update_from(a)` 之后变为 `-99.0`。
4. **预期结果**：第一行打印 `<missing>`（或抛 AttributeError，取决于访问方式），第二行打印 `-99.0`。
5. 此处主要演示属性搬运；`raw` 的 mask 不会被 `_update_from` 设置（仍需 `__array_finalize__` 处理），这一点请「待本地验证」`raw._mask` 的状态。

#### 4.2.5 小练习与答案

**练习 1**：`_update_from` 会不会复制 mask？为什么？

**答案**：不会。`_update_from` 只搬运 `_fill_value`、`_hardmask`、`_sharedmask`、`_isfield`、`_baseclass` 等「簿记属性」，mask 的处理交给 `__array_finalize__` / `__getitem__` / `__array_wrap__` 等更专门的逻辑。把 mask 与簿记属性分开，是因为 mask 有形状、共享、复制等复杂语义，不能简单 `getattr` 搬运。

**练习 2**：为什么 `_optinfo` 要新建一个字典再 `update`，而不是直接引用 `obj._optinfo`？

**答案**：为了避免「反向传播」——若直接引用同一个字典对象，对 `self._optinfo` 的修改会改到 `obj` 上。新建字典再做浅拷贝合并，保证两个实例的扩展信息互不影响。

### 4.3 `__array_finalize__` mask 传播启发式

#### 4.3.1 概念说明

`__array_finalize__(self, obj)` 是 NumPy 子类化协议里**最重要也最棘手**的钩子。NumPy 在很多场景下会「从已有数组派生出新数组」——切片、视图、`astype`、`empty_like`、ufunc 输出分配等等——并在新数组创建后调用 `__array_finalize__(new, original)`，让子类有机会把附加属性（mask、fill_value……）搬过来。

棘手之处在于：**NumPy 不会告诉 `__array_finalize__` 这次派生是为什么发生的**。它只给一个 `obj`（模板），既不知道 `self` 是切片还是 ufunc 输出，也不知道用户期望 mask 被共享、复制还是清空。源码注释对此直言不讳，称其「at best … based on guesswork and heuristics」「This is also horribly broken but somewhat less so」。

因此 MaskedArray 的策略是：**`__array_finalize__` 只提供一个「默认猜测」，真正需要精确语义的场景由更专门的入口覆盖它**——切片/索引走 `__getitem__`（4.3 节实践会看到），ufunc 走 `__array_wrap__`（4.4 节）。理解这一点，才能解释为什么切片和加法得到的 mask 都是「对的」。

#### 4.3.2 核心流程

`__array_finalize__` 的逻辑分三步：

```
__array_finalize__(self, obj):
    # 第 1 步：搬运簿记属性
    self._update_from(obj)

    # 第 2 步：用启发式决定 self._mask
    if isinstance(obj, ndarray):
        _mask = getmaskarray(obj) if obj.dtype.names is not None else getmask(obj)

        if _mask is not nomask and obj的数据基址 != self的数据基址:
            # 新数组指向不同内存（如 ufunc 输出、astype、部分切片 a[1:]）
            # → 复制 mask（用 astype，顺便处理 dtype/顺序变化）
            _mask = _mask.astype(_mask_dtype, order)
        else:
            # 同一基址（如整段视图 a[...]）或无掩码
            # → 取 .view()，共享缓冲区但形状变化不回传
            _mask = _mask.view()
    else:
        _mask = nomask

    self._mask = _mask

    # 第 3 步：把 mask reshape 到 self.shape，失败则退回 nomask
    if self._mask is not nomask:
        try:
            self._mask = self._mask.reshape(self.shape)
        except ValueError:
            self._mask = nomask
        except (TypeError, AttributeError):
            pass

    # 第 4 步：规整 fill_value
    if self._fill_value is not None:
        self._fill_value = _check_fill_value(self._fill_value, self.dtype)
    elif self.dtype.names is not None:
        self._fill_value = _check_fill_value(None, self.dtype)
```

启发式的核心是**「比较数据基址」**这一步：

- 若 `self` 与 `obj` 指向**同一段内存起始地址**（典型是整段视图 `a[...]`、`a.view()`），说明 `self` 是 `obj` 的「整体视图」→ 让 mask 也共享（`.view()`）。
- 若基址**不同**（典型是 ufunc 新分配的输出、`astype` 转换、或**部分切片** `a[1:]`，因为切片起点有偏移），说明 `self` 不是 `obj` 的整体视图 → **复制** mask，避免后续修改 `self` 反向污染 `obj`。

注意一个微妙之处：对部分切片 `a[1:]`，`self`（切片）与 `obj`（原数组）的基址不同，所以 `__array_finalize__` 会去**复制整段 mask 再 reshape**，而整段 mask 的尺寸（= 原数组大小）往往和 `self.shape`（= 切片大小）对不上，reshape 会抛 `ValueError`，于是 mask 被退回 `nomask`——这正是 `__array_finalize__` 「horribly broken」的一面。也正因如此，**MaskedArray 重写了 `__getitem__` 来亲自处理切片的 mask**，不依赖这个启发式。

#### 4.3.3 源码精读

**第 1 步与那段著名的自白注释**，见 [core.py:L3050-L3090](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3050-L3090)：

```python
def __array_finalize__(self, obj):
    """Finalizes the masked array."""
    # Get main attributes.
    self._update_from(obj)

    # We have to decide how to initialize self.mask, based on
    # obj.mask. This is very difficult. ... This method can
    # be called in all kinds of places for all kinds of reasons -- could
    # be empty_like, could be slicing, could be a ufunc, could be a view.
    # The numpy subclassing interface simply doesn't give us any way
    # to know, which means that at best this method will be based on
    # guesswork and heuristics. ...
    if isinstance(obj, ndarray):
        if obj.dtype.names is not None:
            _mask = getmaskarray(obj)
        else:
            _mask = getmask(obj)
```

结构化 dtype 用 `getmaskarray`（要逐字段完整布尔数组），普通 dtype 用 `getmask`（可能是 `nomask`）。

**第 2 步：基址比较 → 复制或共享**，见 [core.py:L3099-L3121](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3099-L3121)：

```python
if (_mask is not nomask and obj.__array_interface__["data"][0]
        != self.__array_interface__["data"][0]):
    # We should make a copy. But we could get here via astype,
    # in which case the mask might need a new dtype as well ...
    if self.dtype == obj.dtype:
        _mask_dtype = _mask.dtype
    else:
        _mask_dtype = make_mask_descr(self.dtype)
    if self.flags.c_contiguous:   order = "C"
    elif self.flags.f_contiguous: order = "F"
    else:                          order = "K"
    _mask = _mask.astype(_mask_dtype, order)     # 复制
else:
    # Take a view so shape changes, etc., do not propagate back.
    _mask = _mask.view()                          # 共享
```

`__array_interface__["data"][0]` 是数组底层缓冲区的起始指针（一个整数）。比较两个整数即可 O(1) 判断「是否指向同一段内存」。`.astype(...)` 默认 `copy=True`，所以是复制；`.view()` 不复制，共享缓冲区。

**第 3 步：reshape 到 self.shape**，见 [core.py:L3125-L3141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3125-L3141)：

```python
self._mask = _mask
if self._mask is not nomask:
    try:
        self._mask = self._mask.reshape(self.shape)
    except ValueError:
        self._mask = nomask
    except (TypeError, AttributeError):
        # When _mask.shape is not writable (because it's a void)
        pass

if self._fill_value is not None:
    self._fill_value = _check_fill_value(self._fill_value, self.dtype)
elif self.dtype.names is not None:
    self._fill_value = _check_fill_value(None, self.dtype)
```

`except ValueError: self._mask = nomask` 就是前文说的「reshape 失败就丢掉 mask」的兜底——这恰恰说明 `__array_finalize__` 不能单独胜任切片场景。

**对比：`__getitem__` 如何亲自处理切片的 mask**，见 [core.py:L3277-L3400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3277-L3400)。关键几行：

```python
def __getitem__(self, indx):
    dout = self.data[indx]                 # 切数据（普通 ndarray 切片）
    _mask = self._mask
    ...
    if _mask is not nomask:
        mout = _mask[indx]                 # 切掩码：与数据同索引
    ...
    # 非标量结果：
    dout = dout.view(type(self))          # 这里会触发 __array_finalize__(dout, self)
    dout._update_from(self)
    ...
    if mout is not nomask:
        dout._mask = reshape(mout, dout.shape)   # 显式覆盖 __array_finalize__ 设的 mask
        dout._sharedmask = True
    return dout
```

注意 `dout.view(type(self))` 会触发一次 `__array_finalize__(dout, self)`，但随后 `dout._mask = reshape(mout, dout.shape)` **显式覆盖**了它。注释（[core.py:L3284-L3287](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3284-L3287)）说得很直白：本可以直接用 `ndarray.__getitem__`，但那样就得改 `__array_finalize__` 防止它在 mask 没准备好时乱 reshape——「So it's easier to stick to the current version」（干脆自己写 `__getitem__` 更省事）。

> 一句话总结这一节：`__array_finalize__` 是「兜底的默认猜测」，`__getitem__` 与 `__array_wrap__` 是「精确覆盖」。后者之所以存在，正是因为前者的启发式不可靠。

#### 4.3.4 代码实践

本节实践就是本讲的总实践任务之一：对掩码数组做切片 `a[1:]`，观察并解释 mask 传播。

1. **实践目标**：验证切片后 mask 正确保留，并能用 `__getitem__` + `__array_finalize__` 源码解释。
2. **操作步骤**：

```python
import numpy as np, numpy.ma as ma

a = ma.array([10, 20, 30], mask=[0, 1, 0])
print("a.mask         =", a.mask)          # [False True False]

b = a[1:]                                   # 切片
print("b.mask         =", b.mask)          # 预期 [True False]（由 __getitem__ 显式切分）
print("b._sharedmask  =", b._sharedmask)   # 预期 True
```

3. **需要观察的现象**：`b.mask` 是 `a.mask` 对应位置的切片 `[True False]`，而非 `nomask`。
4. **预期结果**：`a.mask = [False True False]`；`b.mask = [ True False]`；`b._sharedmask = True`。
5. **结合源码解释**：`a[1:]` 走的是 `__getitem__`（[core.py:L3277](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3277)）。它在 [core.py:L3320](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3320) 用 `mout = _mask[indx]` 把掩码与数据用**同一索引**切分，再在 [core.py:L3397](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3397) 用 `dout._mask = reshape(mout, dout.shape)` 显式赋值——这一步**覆盖**了 `dout.view(type(self))` 触发的 `__array_finalize__` 所设的 mask。如果只靠 `__array_finalize__`，由于切片基址与原数组不同、整段 mask 尺寸又对不上 `b.shape`，reshape 会失败并把 mask 退回 `nomask`（这正是 [core.py:L3128-L3131](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3128-L3131) 的 `except ValueError` 分支）。所以 `b.mask` 之所以正确，是因为 `__getitem__` 没有把这件事交给那个「horribly broken」的启发式。

#### 4.3.5 小练习与答案

**练习 1**：`__array_finalize__` 用什么信息判断「该共享还是复制 mask」？这个判断为什么不可靠？

**答案**：它比较 `obj.__array_interface__["data"][0]`（模板的缓冲区起始指针）与 `self` 的同值，相等则共享（`.view()`），不等则复制（`.astype`）。不可靠是因为「基址相同」并不等价于「`self` 是 `obj` 的整体视图」——比如 `self` 可能是 `obj` 的某一行但碰巧同址，或具有奇怪 strides；而「基址不同」也不一定意味着该复制（部分切片的 mask 理应与原数组联动）。源码注释承认这只是「a heuristic it's not bad」。

**练习 2**：既然 `__array_finalize__` 会处理 mask，为什么 `MaskedArray` 还要重写 `__getitem__`？

**答案**：因为切片时 `self.shape`（切片大小）通常小于 `obj.shape`（原数组大小），`__array_finalize__` 拿到的是整段 mask，reshape 到切片形状会失败并退回 `nomask`，丢失屏蔽信息。`__getitem__` 用 `_mask[indx]` 把掩码与数据同步切分，再显式赋给 `dout._mask`，绕开了这个缺陷。

### 4.4 `__array_wrap__` ufunc 钩子

#### 4.4.1 概念说明

`__array_wrap__(self, obj, context=None)` 在 **ufunc 计算完成之后**被 NumPy 调用。参数含义：

- `obj`：ufunc 刚算出来的「原始结果数组」（可能已经过一次 `__array_finalize__`，带了一个启发式 mask）。
- `context`：三元组 `(func, args, out_i)`，`func` 是被调用的 ufunc（如 `np.add`），`args` 是输入参数，`out_i` 是输出索引。

它的职责是：**根据所有输入的 mask 与「域(domain)」重新计算结果的正确 mask，覆盖 `__array_finalize__` 留下的默认猜测**。如果说 `__array_finalize__` 是「猜」，`__array_wrap__` 就是「算」——因为它手里有 `context`，知道这是哪个 ufunc、输入是谁，从而能用 `mask_or` 把输入掩码合并、用 `ufunc_domain` 把超出定义域的结果屏蔽。

> 触发顺序（以 `a + 1` 为例）：NumPy 先分配输出数组（触发 `__array_finalize__`，得到一个启发式 mask）→ 执行 `a.data + 1` 写入输出 → 调用 `__array_wrap__`（用 `mask_or` 重算 mask，覆盖前者）。因此对 ufunc 结果，**`__array_wrap__` 是 mask 的最终裁决者**。

#### 4.4.2 核心流程

```
__array_wrap__(self, obj, context=None, return_scalar=False):
    # 第 1 步：拿到 result（in-place 时直接复用 self）
    if obj is self:              # 就地操作，如 a += b
        result = obj
    else:
        result = obj.view(type(self))   # 重新 view 成子类
        result._update_from(self)       # 搬运 fill_value 等簿记属性

    # 第 2 步：若有 ufunc 上下文，重算 mask
    if context is not None:
        result._mask = result._mask.copy()      # 先复制，绝不反向污染
        func, args, out_i = context
        input_args = args[:func.nin]            # 只取真正输入，排除输出参数(gh-10459)

        # 2a：合并所有输入的 mask（logical_or）
        m = functools.reduce(mask_or, [getmaskarray(arg) for arg in input_args])

        # 2b：域检查（如 divide 的除零、sqrt 的负数）
        domain = ufunc_domain.get(func)
        if domain is not None:
            with np.errstate(divide='ignore', invalid='ignore'):
                d = filled(domain(*input_args).astype(bool, copy=False), True)
            if d.any():
                fill_value = ufunc_fills[func]            # 取该 ufunc 的填充值
                np.copyto(result, fill_value, where=d)    # 把越域结果改写为填充值
                m = (m | d) if m is not nomask else d      # 域掩码并入总掩码

        # 2c：标量屏蔽结果返回 masked 单例
        if result is not self and result.shape == () and m:
            return masked
        else:
            result._mask = m
            result._sharedmask = False

    return result
```

两条主线：

- **mask 合并（2a）**：结果的 mask = 所有输入 mask 的逻辑或。`a + 1` 中 `1` 无掩码，所以结果 mask = `a` 的 mask；`a + b`（两者都有 mask）则结果 mask = `a.mask | b.mask`。这正是「掩码具有传染性」在 ufunc 层的实现。
- **域屏蔽（2b）**：某些 ufunc 注册了「域检查函数」（`ufunc_domain[func]`），它返回一个布尔数组标记「哪些位置的结果无效」（如除零、对负数开方）。`__array_wrap__` 把这些位置既改写成填充值（`np.copyto`），又并入结果 mask。这一机制是 `ma.sqrt([-1,0,1])` 会把 `-1` 屏蔽的根因，u2-l4 会深入展开。

注意 `result._mask = result._mask.copy()` 与 `result._sharedmask = False`：ufunc 结果的 mask 永远是**独立副本**，修改它不会反向污染任何输入数组。

#### 4.4.3 源码精读

**第 1 步：就地 vs 重新 view**，见 [core.py:L3150-L3154](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3150-L3154)：

```python
if obj is self:  # for in-place operations
    result = obj
else:
    result = obj.view(type(self))
    result._update_from(self)
```

`obj is self` 判断就地操作（如 `a += b`），此时直接复用 `self`，不新建视图。

**第 2 步：合并输入 mask**，见 [core.py:L3156-L3161](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3156-L3161)：

```python
if context is not None:
    result._mask = result._mask.copy()
    func, args, out_i = context
    # args sometimes contains outputs (gh-10459), which we don't want
    input_args = args[:func.nin]
    m = functools.reduce(mask_or, [getmaskarray(arg) for arg in input_args])
```

`func.nin` 是 ufunc 的输入个数（`add` 为 2，`sqrt` 为 1）。`args[:func.nin]` 排除可能混入的输出参数。`functools.reduce(mask_or, ...)` 把所有输入的 `getmaskarray` 逐个 OR 起来——`mask_or` 内部对 `nomask` 有短路优化，所以无掩码的输入（如标量 `1`）不增加任何屏蔽。

**第 3 步：域检查与填充**，见 [core.py:L3163-L3192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3163-L3192)：

```python
domain = ufunc_domain.get(func)
if domain is not None:
    with np.errstate(divide='ignore', invalid='ignore'):
        d = domain(*input_args).astype(bool, copy=False)
        d = filled(d, True)
    if d.any():
        try:
            fill_value = ufunc_fills[func][-1]   # 二元域：取最后一个
        except TypeError:
            fill_value = ufunc_fills[func]        # 一元域：直接用
        except KeyError:
            fill_value = self.fill_value          # 未识别域：退回自身 fill_value
        np.copyto(result, fill_value, where=d)
        if m is nomask:
            m = d
        else:
            m = (m | d)                            # 不原地改，避免反向传播
```

`np.errstate(divide='ignore', invalid='ignore')` 临时关掉除零/无效运算的浮点警告——因为域检查本身可能触发这些警告（例如 `_DomainSafeDivide` 会真去算除法来判断分母是否为 0），而这里我们只关心判断结果、不想惊动用户。

**第 4 步：标量屏蔽 → 返回 `masked` 单例**，见 [core.py:L3194-L3201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3194-L3201)：

```python
if result is not self and result.shape == () and m:
    return masked
else:
    result._mask = m
    result._sharedmask = False
return result
```

当 ufunc 产生一个 0 维（标量）且被屏蔽的结果时，直接返回全局单例 `masked`（`ma.masked`）——这就是 `ma.array([1,2,3])[1]`（取到被屏蔽的 `20`）会返回 `masked` 而非一个 0 维 MaskedArray 的原因。`result._sharedmask = False` 确保结果 mask 独立。

#### 4.4.4 代码实践

1. **实践目标**：验证 `a + 1` 的 mask 由 `__array_wrap__` 经 `mask_or` 计算而来，且为独立副本。
2. **操作步骤**：

```python
import numpy as np, numpy.ma as ma

a = ma.array([10, 20, 30], mask=[0, 1, 0])
print("a.mask        =", a.mask)            # [False True False]

c = a + 1                                   # ufunc: np.add(a, 1)
print("c.data        =", c.data)            # [11 21 31]
print("c.mask        =", c.mask)            # 预期 [False True False]
print("c._sharedmask =", c._sharedmask)     # 预期 False（独立副本）

# 验证 mask 独立：改 c.mask 不应影响 a.mask
c.mask[0] = True
print("after c.mask[0]=True -> a.mask =", a.mask)   # 预期仍是 [False True False]

# 验证两输入 mask 合并
b = ma.array([1, 2, 3], mask=[1, 0, 0])
d = a + b
print("(a+b).mask    =", d.mask)            # 预期 [True True False]（按位或）
```

3. **需要观察的现象**：`c.mask` 与 `a.mask` 一致（因为 `1` 无掩码，`mask_or` 短路）；改 `c.mask` 不污染 `a`；`a + b` 的 mask 是两者按位或。
4. **预期结果**：`c.mask = [False True False]`；`c._sharedmask = False`；改 `c.mask[0]` 后 `a.mask` 不变；`(a+b).mask = [ True True False]`。
5. **结合源码解释**：`a + 1` 触发 `np.add`，先分配输出（经 `__array_finalize__` 得到一个启发式 mask），再调用 `__array_wrap__`（[core.py:L3143](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3143)）。后者在 [core.py:L3161](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3161) 执行 `m = functools.reduce(mask_or, [getmaskarray(a), getmaskarray(1)])`：`getmaskarray(1)` 对标量返回无掩码，`mask_or` 短路得到 `a` 的 mask；又因 `add` 未在 `ufunc_domain` 注册（[core.py:L844](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L844)），跳过域检查；最终 [core.py:L3198-L3199](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3198-L3199) 把 `m` 赋给 `result._mask` 并置 `_sharedmask = False`。这就是 `(a+1).mask` 的来历——它来自 `__array_wrap__` 的精确计算，而非 `__array_finalize__` 的猜测。

#### 4.4.5 小练习与答案

**练习 1**：`a + 1` 的结果 mask 为什么和 `a.mask` 一样？如果改成 `a + b`（`b` 也有 mask），结果 mask 会怎样？

**答案**：`__array_wrap__` 用 `mask_or` 把所有输入的 `getmaskarray` 合并。`1` 是标量、无掩码，`mask_or` 对 `nomask` 短路，所以结果 mask = `a.mask`。若 `b` 也有 mask，则结果 mask = `a.mask` 与 `b.mask` 的按位逻辑或。

**练习 2**：`__array_wrap__` 里 `result._mask = result._mask.copy()` 和 `result._sharedmask = False` 的目的是什么？

**答案**：确保 ufunc 结果的 mask 是独立副本，对它的任何修改都不会反向传播到输入数组。这与 `__array_finalize__` 在「同基址」分支用 `.view()` 共享 mask 形成对比：ufunc 结果必须独立，而整段视图可以共享。

**练习 3**：为什么 `__array_wrap__` 要用 `np.errstate(divide='ignore', invalid='ignore')` 包住域检查？

**答案**：域检查函数（如判断除零的 `_DomainSafeDivide`）本身会执行可能触发「除以零」「无效运算」的浮点运算来判定哪些位置越域。这些警告对用户没有意义（我们只是借运算做判断，真正的结果会被填充值覆盖），所以临时关掉以免惊扰用户。

## 5. 综合实践

把四个模块串成一条完整调用链：「创建 → 切片 → ufunc」，并用源码解释每一步 mask 的来历。

```python
import numpy as np, numpy.ma as ma

# (1) 创建：走 __new__
a = ma.array([10, 20, 30, 40], mask=[0, 1, 0, 0])
#     __new__ 在 core.py:L2882 把 data view 成 MaskedArray，
#     mask 走 Case 2（显式给掩码），见 core.py:L2955。

# (2) 切片：走 __getitem__，显式切分 mask
b = a[1:]                                   # 预期 mask=[True, False, False]
#     __getitem__ 用 _mask[indx] 切掩码，core.py:L3320；
#     随后 dout._mask = reshape(mout, dout.shape) 覆盖 __array_finalize__，core.py:L3397。

# (3) ufunc：走 __array_finalize__ + __array_wrap__
c = b + 1                                   # 预期 mask=[True, False, False]
#     __array_wrap__ 用 mask_or 合并输入 mask，core.py:L3161；
#     add 无 domain，直接 result._mask = m, _sharedmask=False, core.py:L3198。

print("a.mask =", a.mask)
print("b.mask =", b.mask)
print("c.mask =", c.mask)
```

**你要做的**：

1. 运行上述脚本，确认三行 mask 输出与注释一致。
2. 在 `__array_finalize__` 的 [core.py:L3099](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3099) 处与 `__array_wrap__` 的 [core.py:L3161](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3161) 处各加一行 `print`（例如 `print("finalize:", obj.shape, "->", self.shape)`），重新运行，观察：
   - 步骤 (2) 切片时 `__array_finalize__` 是否被触发？被触发后 `__getitem__` 又如何覆盖它？
   - 步骤 (3) 加法时 `__array_finalize__` 与 `__array_wrap__` 各被触发几次、先后顺序如何？
3. 把 `b + 1` 改成 `b + ma.array([1,2,3], mask=[1,0,0])`，预测 `(b + ...).mask`，再用 `__array_wrap__` 的 `mask_or` 逻辑验证你的预测。

> 提示：修改源码仅用于本地学习观察，请勿提交；若不便改源码，也可用 `breakpoint()` 在对应函数入口下断点观察调用栈。运行结果与 NumPy 版本相关，部分细节请「待本地验证」。

## 6. 本讲小结

- `MaskedArray` 是 `ndarray` 的子类（[core.py:L2770](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2770)），子类化必须在 `__new__` 里完成「分配 → view 升级 → 挂属性」，`__new__` 用 `ndarray.view` 不复制数据地完成升级。
- `_update_from` 是属性搬运工，只搬 `_fill_value` / `_hardmask` / `_sharedmask` / `_isfield` / `_baseclass` / `_optinfo` 等「簿记属性」，不搬数据也不搬 mask，被 `__array_finalize__` 与 `__array_wrap__` 复用。
- `__array_finalize__` 是「兜底的默认猜测」：用「数据基址是否相同」决定 mask 共享（`.view()`）还是复制（`.astype`），源码注释坦承这是「guesswork and heuristics」。
- 因为这个启发式不可靠（切片时 reshape 会失败退回 `nomask`），MaskedArray 重写 `__getitem__` 亲自切分 mask（`_mask[indx]`），显式覆盖 `__array_finalize__` 的结果。
- `__array_wrap__` 是 ufunc 专用钩子，在计算完成后用 `mask_or` 合并所有输入 mask、用 `ufunc_domain` 做域屏蔽，是 ufunc 结果 mask 的「最终裁决者」；它保证结果 mask 是独立副本（`_sharedmask = False`）。
- 三个钩子的分工：`__new__` 负责创建、`__array_finalize__` 负责默认传播、`__array_wrap__`（与 `__getitem__`）负责精确覆盖。理解「后者覆盖前者」是看懂 mask 传播的钥匙。

## 7. 下一步学习建议

- **紧接本讲**：阅读 u2-l3（fill_value 系统），看 `_check_fill_value` 与 `default_fill_value` 如何与 `__new__` / `__array_finalize__` 里对 `_fill_value` 的规整衔接。
- **ufunc 深入**：阅读 u2-l4（一元 ufunc 与 domain）和 u2-l5（二元 ufunc 与除法域），把本讲 `__array_wrap__` 里的 `ufunc_domain` / `ufunc_fills` / `np.copyto(result, fill_value, where=d)` 与具体的 `_DomainCheckInterval`、`_DomainSafeDivide` 对应起来。
- **索引与赋值**：阅读 u2-l6，系统看 `__getitem__` / `__setitem__` / `put` / `putmask` 如何维护 mask，本讲只触及了 `__getitem__` 的切片分支。
- **子类化与持久化**：到专家层后阅读 u3-l2（子类化与 mvoid）和 u3-l4（pickle 与重建），看 `_update_from` 搬运的 `_optinfo` 如何成为子类自定义属性传播通道，以及 `_mareconstruct` 如何在反序列化时还原子类。
