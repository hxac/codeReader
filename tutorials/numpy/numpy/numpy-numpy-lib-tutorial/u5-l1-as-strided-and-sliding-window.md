# as_strided 与 sliding_window_view

## 1. 本讲目标

本讲深入 `numpy/lib/_stride_tricks_impl.py`，拆解 numpy 里「最强大也最危险」的一组视图构造工具。读完本讲，你应当能够：

- 说出「步长（stride）」与「视图（view）」在内存层面的关系，理解为什么改写 shape/strides 不需要复制数据。
- 看懂 `DummyArray` 这个替身对象如何借助 `__array_interface__` 协议，让 numpy 凭空「造」出一个拥有任意 shape/strides 的视图。
- 独立追读 `as_strided` 的实现，并能用 `check_bounds=True` 借助 `byte_bounds` 做越界校验。
- 解释 `_maybe_view_as_subclass` 为什么在 `subok=True` 时把结果重新 `view` 成子类并调用 `__array_finalize__`。
- 安全地用 `sliding_window_view` 做滚动窗口（移动平均、卷积窗口），并理解它最终不过是 `as_strided` 的一层「安全封装」。

一个贯穿全讲的关键认知：**`as_strided` 是 `sliding_window_view` 的内核，二者只是「手工调参」与「自动算参」的差别**。一旦理解了滑窗的 shape/strides 是怎么算出来的，你也就理解了 `as_strided` 在干什么。

## 2. 前置知识

在动手读源码前，先把几个底层概念铺平。本讲依赖 `u2-l3` 建立的 `__array_interface__` 与 `byte_bounds` 认知，这里只补充本讲特需的部分。

1. **步长（stride）到底在描述什么**
   一个 ndarray 在内存里是一段**一维**字节序列。多维只是「想象」出来的。`strides[i]` 告诉 numpy：沿第 `i` 维把下标加 1，指针要在内存里前进多少字节。例如 `x = np.arange(6)`（int64，itemsize=8），`x.strides` 是 `(8,)`：

   ```text
   下标:      0      1      2      3      4      5
   字节:   [0..7][8..15][16..23][24..31][32..39][40..47]
   strides=(8,)  →  每前进一格 +8 字节
   ```

   步长可以是 0（同一个字节读多次，即「广播」）、可以是负数（反向读）、也可以大于 itemsize（跳着读）。`a[::2]` 的 strides 就是 `(16,)`。

2. **视图（view）= 换一套 shape/strides 去读同一块内存**
   `y = x[::2]` 并没有复制数据，它只是造了一个新数组对象，其 `data` 指针、`shape`、`strides` 都变了，但底层的字节缓冲区与 `x` 完全相同。这就是 view。判断 view 的金标准：`np.shares_memory(x, y)` 为 `True`。

3. **`__array_interface__` 协议（来自 `u2-l3`）**
   任何暴露这个属性的 Python 对象，都把自己描述成「一段带 shape/strides 的内存」。关键字段：`data`（`(指针整数, 是否只读)`）、`shape`、`strides`、`typestr`/`descr`（dtype 描述）。numpy 的 `np.asarray` 在拿到一个有 `__array_interface__` 的对象时，会**直接复用**它指向的内存构造 ndarray，不复制数据。这一条是本讲的命脉：它是 Python 层唯一能「注入任意 shape/strides」的官方入口。

4. **`byte_bounds`（来自 `u2-l3`）**
   `byte_bounds(a)` 返回 `(low, high)`，即数组实际访问的最低与「恰越过最高」字节地址。本讲**不重复**它的算法推导（见 `u2-l3` 第 4.2 节），只把它当作现成的「内存边界尺子」，看 `as_strided` 如何用它做越界判断。

5. **`base` 链与内存归属**
   ndarray 有个 `.base` 属性：如果当前数组是某块内存的视图，`.base` 指向真正持有这块内存的对象；若 `.base is None`，说明自己就是 owner。视图的视图会形成一条 `base` 链，链的末端才是真正 owner。这在 `check_bounds` 里要用到。

## 3. 本讲源码地图

| 文件 | 作用 | 公开路径 |
| --- | --- | --- |
| [_stride_tricks_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py) | `as_strided`、`sliding_window_view`、`DummyArray`、`_maybe_view_as_subclass` 的实现 | 经薄模块暴露 |
| [stride_tricks.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/stride_tricks.py) | 1 行薄模块，把 `_stride_tricks_impl` 的 `as_strided`/`sliding_window_view` 搬出去 | `np.lib.stride_tricks.*` |
| [_array_utils_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py) | `byte_bounds`（越界校验的尺子） | `np.lib.array_utils.byte_bounds` |
| [array_utils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/array_utils.py) | 薄模块，把 `byte_bounds` 等搬出去 | `np.lib.array_utils` |
| [tests/test_stride_tricks.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py) | `as_strided` 与滑窗的权威测试（含越界用例） | — |
| [tests/test_array_utils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_array_utils.py) | `byte_bounds` 的典型用例 | — |

注意一个跨文件依赖：[_stride_tricks_impl.py:10](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L10) 直接 `from numpy.lib._array_utils_impl import byte_bounds`——越界校验这把尺子，在实现层就跨过薄模块被直接引用了。这正是本讲把 `byte_bounds` 列为最小模块之一的原因。

---

## 4. 核心概念与源码讲解

### 4.1 DummyArray：挂载 `__array_interface__` 的「替身」对象

#### 4.1.1 概念说明

问题来了：`as_strided` 想做的，是「拿一个数组 `x`，换一套全新的 `shape` 和 `strides`，得到一个**指向同一块内存**的新数组」。可是 numpy 并没有提供 `ndarray.__new__(..., strides=...)` 这样的 Python 构造器——你没法直接对一个 C 对象说「请用这组 strides」。

那有没有「后门」？有，就是 `__array_interface__`。`np.asarray(obj)` 一旦发现 `obj` 有 `__array_interface__`，就会按那份字典里的 `data/shape/strides/typestr` 去**共享**那块内存、造出一个 ndarray。所以思路很直接：

> 造一个「只负责携带一份 `__array_interface__` 字典」的替身对象，把改写好的 shape/strides 塞进字典，再 `np.asarray` 它，就能拿到想要的视图。

这个替身就是 `DummyArray`。它的全部职责是「挂载一个接口字典，并顺带保住对原数组的引用」。

#### 4.1.2 核心流程

```text
DummyArray(interface, base=base)
  │
  ├─ self.__array_interface__ = interface   ← numpy 之后读的就是它
  └─ self.base = base                        ← 保住对底层缓冲区的引用，防止被回收
```

随后 `np.asarray(dummy)` 会：

1. 读取 `dummy.__array_interface__`；
2. 取其中的 `data[0]`（指针）、`shape`、`strides`、`descr`；
3. 构造一个**共享该内存**的 ndarray（不复制数据）。

因为 `DummyArray` 同时持有 `base`，原数组的缓冲区在构造期间不会被垃圾回收，新视图得以继续指向有效内存。

#### 4.1.3 源码精读

`DummyArray` 定义极简，连方法都只有一个 `__init__`：[_stride_tricks_impl.py:15-22](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L15-L22)

```python
class DummyArray:
    """Dummy object that just exists to hang __array_interface__ dictionaries
    and possibly keep alive a reference to a base array.
    """

    def __init__(self, interface, base=None):
        self.__array_interface__ = interface
        self.base = base
```

类文档一句话点明了它的存在意义：「挂着 `__array_interface__` 字典、并可能保住对 base 数组引用的假对象」。注意它**不是** ndarray 的子类，也没有任何数组行为——它唯一的「超能力」就是那个被 numpy 识别的属性名。

#### 4.1.4 代码实践

**实践目标**：亲手复现 `as_strided` 的核心招式——用 `DummyArray` + `__array_interface__` 造一个零拷贝视图，验证它确实共享 `x` 的内存。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib._stride_tricks_impl import DummyArray   # 内部类，仅用于演示

x = np.arange(6)                       # int64, strides=(8,)
# 复制 x 的接口字典，改写 shape 与 strides
iface = dict(x.__array_interface__)
iface['shape'] = (3, 2)                # 想要 3x2
iface['strides'] = (16, 8)             # 第 0 维每步跳 2 个元素
dummy = DummyArray(iface, base=x)
y = np.asarray(dummy)
y._set_dtype(x.dtype)                  # 与 as_strided 一致：显式钉住 dtype

print(y)
# [[0 1]
#  [2 3]
#  [4 5]]
print(np.shares_memory(x, y))          # True —— 同一块内存，零拷贝
```

**需要观察的现象**：`y` 的内容与 `x.reshape` 等价，但 `np.shares_memory` 返回 `True`，说明数据没被复制；修改 `y[0,0]` 会同步改变 `x[0]`（小心：这是真实的内存写入）。

**预期结果**：`np.shares_memory(x, y)` 为 `True`。这正是 `as_strided` 之所以「快」的原因——它永远不复制数据。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `iface['strides']` 改成 `(0, 8)`，`y` 会变成什么样？

> **答案**：第 0 维步长为 0，意味着「沿第 0 维每前进一步都不动指针」，于是每一行都读同一份数据，结果是 `[[0,1],[0,1],[0,1]]`。这就是「广播」在 stride 层的样子。

**练习 2**：为什么 `DummyArray` 要存一个 `base`，而不直接让 `np.asarray` 自己去保活？

> **答案**：`__array_interface__` 字典本身只携带「指针整数 + 元信息」，并不持有对 Python 端原数组对象的引用。若不额外保活，原数组一旦被回收，那块内存就会被释放，新视图就成了野指针。`DummyArray.base` 把这个引用攥在手里，构造期间内存始终有效。

---

### 4.2 as_strided：手工改写 shape/strides 构造视图

#### 4.2.1 概念说明

`as_strided` 把 4.1 节那套「DummyArray + 接口字典 + asarray」封装成一个用户级函数。它接收一组**任意的** shape 和 strides，返回指向原内存的视图。功能强大，但也正因为「任意」，它可以轻易造出越界视图——读写到数组根本不拥有的内存，轻则结果错乱，重则段错误崩溃。源码 docstring 把它称为「必须极度小心使用」的函数（[L45](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L45)），并反复建议尽量改用 `sliding_window_view`。

#### 4.2.2 核心流程

```text
as_strided(x, shape, strides, subok, writeable, check_bounds)
  │
  1. base = np.array(x, copy=None, subok=subok)        ← 归一成 ndarray（可能保留子类）
  2. interface = dict(base.__array_interface__)         ← 复制接口字典（副本，不污染 base）
  3. 若给了 shape   → interface['shape']   = tuple(shape)
     若给了 strides → interface['strides'] = tuple(strides)
  4. array = np.asarray(DummyArray(interface, base))    ← 让 numpy 按新接口造视图
  5. array._set_dtype(base.dtype)                       ← 接口路径会丢结构化 dtype，显式补回
  6. view = _maybe_view_as_subclass(base, array)        ← subok 时还原成子类
  7. 若 writeable=False → view.flags.writeable = False  ← 默认建议只读
  8. 若 check_bounds=True → 用 byte_bounds 做越界校验（见 4.3）
  return view
```

第 2 步「复制字典」很关键：直接改 `base.__array_interface__` 会污染原数组，所以必须 `dict(...)` 取副本。

#### 4.2.3 源码精读

函数签名与构造主体：[_stride_tricks_impl.py:38-41](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L38-L41)（`@set_module("numpy.lib.stride_tricks")` 把 `__module__` 钉到公开路径）。核心构造在 [L132-L145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L132-L145)：

```python
# 归一输入，可能保留子类
base = np.array(x, copy=None, subok=subok)
# 复制接口字典，再改写 shape / strides
interface = dict(base.__array_interface__)
if shape is not None:
    interface['shape'] = tuple(shape)
if strides is not None:
    interface['strides'] = tuple(strides)

# 让 numpy 按 DummyArray 携带的新接口造视图
array = np.asarray(DummyArray(interface, base=base))
# 接口路径不保留结构化 dtype，显式补回
array._set_dtype(base.dtype)

view = _maybe_view_as_subclass(base, array)
```

第 143 行 `array._set_dtype(base.dtype)` 的注释解释了为什么要这步：经 `__array_interface__` 这条路构造出的数组，**结构化 dtype 信息会丢失**（接口字典的 `descr` 表达力有限），所以这里显式把 dtype 重新钉回去。

随后是只读设置：[L147-L148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L147-L148)

```python
if view.flags.writeable and not writeable:
    view.flags.writeable = False
```

`writeable` 默认是 `True`，但 docstring 强烈建议「能设 False 就设 False」（[L57-L60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L57-L60)），因为 `as_strided` 造出的视图常常**自重叠**——同一字节在视图中出现多次，向量化写入会得到不可预测的结果。

#### 4.2.4 代码实践

**实践目标**：不调用 `sliding_window_view`，**只用原始 `as_strided`** 手工造出滑窗，体会「shape + strides 两件套」如何唯一决定视图。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib.stride_tricks import as_strided

x = np.arange(6)                 # [0,1,2,3,4,5], strides=(8,)
# 目标：4 个长度为 3 的窗口
#   - axis 0（哪个窗口）：下一个窗口起点 +1 元素 → 步长 = x.strides[0] = 8
#   - axis 1（窗口内位置）：+1 元素 → 步长 = 8
v = as_strided(x, shape=(4, 3), strides=(8, 8), writeable=False)
print(v)
# [[0 1 2]
#  [1 2 3]
#  [2 3 4]
#  [3 4 5]]
```

**需要观察的现象**：结果与文档里 `sliding_window_view(x, 3)` 的示例（[L266-L276](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L266-L276)）完全一致——这并非巧合，而是因为 `sliding_window_view` 内部算出的 shape/strides 正是 `(4,3)` 和 `(8,8)`，再喂给同一个 `as_strided`（见 4.5 节）。

**预期结果**：`v` 是 `shape=(4,3)`、`strides=(8,8)` 的只读视图，`np.shares_memory(x, v)` 为 `True`。

#### 4.2.5 小练习与答案

**练习 1**：把上面 `strides` 改成 `(16, 8)`，结果会怎样？为什么？

> **答案**：第 0 维步长翻倍，意味着每个窗口起点比上一个多跳 2 个元素，结果是 `[[0,1,2],[2,3,4],[4,5,6],[6,7,8]]`——但 `x` 只有 6 个元素，最后两个窗口会读到数组之外的内存（越界）。这正是「`as_strided` 危险」的典型现场，需要 `check_bounds=True` 才能拦下。

**练习 2**：docstring 为什么反复强调「`as_strided` 造出的视图常常自重叠」？

> **答案**：很多有用配置（滑窗、转置块、广播）会让同一个字节在视图的多个位置出现。对只读视图这是好事（零拷贝）；但一旦向量化写入，不同写入顺序会互相覆盖，结果依赖执行顺序，因此 docstring 建议默认设 `writeable=False`。

---

### 4.3 byte_bounds 在 check_bounds 中的复用：越界校验

#### 4.3.1 概念说明

`as_strided` 给你「任意 shape/strides」的自由，代价是你得自己保证不越界。`check_bounds` 参数（默认 `None`，即不校验）就是一道可选的安全网：设为 `True` 时，函数会用 `byte_bounds` 把「视图实际访问的内存范围」和「底层缓冲区真正拥有的内存范围」比一比，一旦视图伸到了缓冲区之外就抛 `ValueError`。

`byte_bounds` 的算法本讲不重复（见 `u2-l3` 第 4.2 节），只需记住它返回 `(low, high)`：视图/数组实际触及的最低字节地址，与「恰越过最高字节」的地址。

#### 4.3.2 核心流程

校验逻辑有一个易错点：传进来的 `base` 本身可能就是一个视图（视图的视图）。比如 `x = big_arr[:2]`，再 `as_strided(x, ...)`。真正拥有内存的是 `big_arr`，不是 `x`。所以校验前要**顺着 `.base` 链下钻到 owner**：

```text
if check_bounds:
    1. while isinstance(base.base, np.ndarray):   ← 沿 base 链下钻
           base = base.base                          直到 base.base 不是 ndarray（即 owner）
    2. base_low, base_high = byte_bounds(base)       ← 真正的内存边界
       view_low, view_high = byte_bounds(view)       ← 视图触及的边界
    3. if view_low < base_low:  raise ValueError("...starts N bytes before lowest address")
       if view_high > base_high: raise ValueError("...ends N bytes after highest address")
```

边界用「左闭右开」约定（与 `byte_bounds` 一致）：`view_low >= base_low` 且 `view_high <= base_high` 才算安全。

#### 4.3.3 源码精读

`byte_bounds` 的引入在文件顶部：[_stride_tricks_impl.py:10](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L10)。校验块在 [L150-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L150-L167)：

```python
if check_bounds:
    while isinstance(base.base, np.ndarray):     # 下钻到真正 owner
        base = base.base

    base_low, base_high = byte_bounds(base)       # 底层缓冲区边界
    view_low, view_high = byte_bounds(view)       # 视图边界

    if view_low < base_low:                       # 视图伸到低地址之外
        raise ValueError(
            f"Given shape and strides would access memory out of bounds. "
            f"View starts {base_low - view_low} bytes before lowest address"
        )

    if view_high > base_high:                     # 视图伸到高地址之外
        raise ValueError(
            f"Given shape and strides would access memory out of bounds. "
            f"View ends {view_high - base_high} bytes after highest address"
        )
```

两个分支的错误信息都**定量**告诉你越界了多少字节（`base_low - view_low` 或 `view_high - base_high`），方便调试。注意校验用的是 `byte_bounds`（它接受任何实现了 `__array_interface__` 的对象，view 正好符合），而非逐元素枚举——这是 O(ndim) 而非 O(size) 的廉价检查。

> 说明：这里把 `byte_bounds` 当作「黑盒尺子」复用，不再展开其内部循环；推导见 `u2-l3` 第 4.2.3 节（[_array_utils_impl.py:45-62](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_array_utils_impl.py#L45-L62)）。

#### 4.3.4 代码实践

**实践目标**：复现 [test_stride_tricks.py:783-787](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L783-L787) 的越界用例，观察 `check_bounds` 开关的区别。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib.stride_tricks import as_strided

x = np.arange(10, dtype=np.int64)        # 10 个元素，80 字节

# strides=(32,) 让每步跳 4 个元素，shape=(5,) 要读 5 个起点
#   起点 0,4,8,12,16... 第 5 个起点已越过数组末尾 → 越界
v_bad = as_strided(x, shape=(5,), strides=(32,))   # check_bounds=None：不校验，静默造出野视图
print(v_bad)                                        # 包含数组之外的垃圾数据，行为未定义！

try:
    as_strided(x, shape=(5,), strides=(32,), check_bounds=True)
except ValueError as e:
    print("被拦下：", e)
```

**需要观察的现象**：

- `check_bounds` 为默认 `None` 时，函数**不报错**，返回一个读到越界内存的视图——这正是「危险」的来源，垃圾数据看起来像正常数字。
- `check_bounds=True` 时立即抛 `ValueError`，信息里写明越界字节数。

**预期结果**：第二个调用抛出含 `"out of bounds"` 的 `ValueError`，与测试断言 `pytest.raises(ValueError, match="out of bounds")` 一致。安全用例可参考 [test_as_strided_checked_2d_default_strides](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L743-L747)（默认 shape/strides 时视图与原数组等价、校验通过）：用默认 shape/strides 时视图与原数组等价、校验通过。

#### 4.3.5 小练习与答案

**练习 1**：`as_strided(x, shape=(5,), strides=(0,), check_bounds=True)`，其中 `x = np.array([42])`，会越界吗？

> **答案**：不会。步长为 0 意味着 5 个位置都读同一个字节，视图边界就是 `[p, p+itemsize]`，落在 `x` 的缓冲区内。这正是 [test_as_strided_checked_zero_stride_broadcasting](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L750-L757) 覆盖的「零步长广播」安全情形。

**练习 2**：为什么校验前要先 `while isinstance(base.base, np.ndarray): base = base.base`，而不是直接 `byte_bounds(base)`？

> **答案**：传进来的 `base` 可能是视图，`byte_bounds(base)` 给的是「视图触及的范围」而非「缓冲区真正拥有的范围」。只有下钻到 owner（其 `.base` 不是 ndarray），`byte_bounds` 才反映可合法访问的内存全量。直接对视图校验会把「owner 拥有但当前视图没用到的内存」误判为越界之外，也可能漏判视图恰好指向 owner 中段却向两端越界的情形。

---

### 4.4 _maybe_view_as_subclass：保留子类类型与 finalize

#### 4.4.1 概念说明

第 4.2 节第 4 步用 `np.asarray(DummyArray(...))` 造视图。问题在于：`asarray` 走 `__array_interface__` 这条路，**产物永远是基类 `ndarray`**，即便输入 `x` 是 `np.ma.MaskedArray` 之类的子类。如果用户传了 `subok=True`（意为「请保留子类」），这个产物就「降级」了。

`_maybe_view_as_subclass` 就是来修补这一点的：在需要时把基类结果重新 `view` 成原输入的子类类型，并调用子类的 `__array_finalize__` 让它有机会传播自己的状态（比如掩码、单位等）。

#### 4.4.2 核心流程

```text
_maybe_view_as_subclass(original_array, new_array)
  │
  ├─ 若 type(original_array) is type(new_array)：
  │     直接返回 new_array（类型本来就一致，无需处理）
  │
  └─ 否则（输入是子类、产物是基类）：
        1. new_array = new_array.view(type=type(original_array))   ← 视图成子类
        2. if new_array.__array_finalize__:                        ← 子类实现了 finalize
               new_array.__array_finalize__(original_array)        ← 让它传播状态
        return new_array
```

关键判断是 `type(...) is not type(...)`：只有当两边类型**严格不同**时才介入。普通 ndarray 进、普通 ndarray 出，直接走快路径。

#### 4.4.3 源码精读

定义在 [L25-L35](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L25-L35)：

```python
def _maybe_view_as_subclass(original_array, new_array):
    if type(original_array) is not type(new_array):
        # if input was an ndarray subclass and subclasses were OK,
        # then view the result as that subclass.
        new_array = new_array.view(type=type(original_array))
        # Since we have done something akin to a view from original_array, we
        # should let the subclass finalize (if it has it implemented, i.e., is
        # not None).
        if new_array.__array_finalize__:
            new_array.__array_finalize__(original_array)
    return new_array
```

注意第 33 行判断的是 `if new_array.__array_finalize__:`——基类 `ndarray` 的 `__array_finalize__` 是 `None`，所以这一步对普通数组是空操作；只有子类把该方法覆盖成真实函数时才会触发。`__array_finalize__` 是 numpy 子类协议的核心钩子（NEP-0013 体系的一部分），任何「从父类派生出新数组」的操作都会调用它，让子类把额外属性搬过来。

#### 4.4.4 代码实践

**实践目标**：用一个 ndarray 子类验证 `subok=True` 能让 `as_strided` 的结果保持子类类型。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib.stride_tricks import as_strided

class MyArray(np.ndarray):
    pass

x = np.arange(6).view(MyArray)            # 子类实例
print("输入类型：", type(x).__name__)      # MyArray

# subok=False（默认）：降级成普通 ndarray
v1 = as_strided(x, shape=(4, 3), strides=(8, 8), subok=False)
print("subok=False 类型：", type(v1).__name__)   # ndarray

# subok=True：经 _maybe_view_as_subclass 还原成 MyArray
v2 = as_strided(x, shape=(4, 3), strides=(8, 8), subok=True)
print("subok=True 类型：", type(v2).__name__)    # MyArray
```

**需要观察的现象**：`subok=True` 时产物类型是 `MyArray`，且 `np.shares_memory(x, v2)` 仍为 `True`。这印证了 `_maybe_view_as_subclass` 用 `.view(type=...)` 完成了类型还原，而 `.view` 本身也是零拷贝的。

**预期结果**：`subok=False` → `ndarray`；`subok=True` → `MyArray`。

#### 4.4.5 小练习与答案

**练习 1**：如果 `original_array` 和 `new_array` 都是普通 `ndarray`，`_maybe_view_as_subclass` 会做任何事吗？

> **答案**：不会。`type(a) is type(b)` 为真，函数直接 `return new_array`，连 `view` 和 `__array_finalize__` 都不碰。这是最常见的快路径。

**练习 2**：为什么源码用 `type(original_array) is not type(new_array)`，而不是 `isinstance(...)`？

> **答案**：`isinstance` 会把「子类 vs 父类」也判为 True，可能跳过本应执行的还原；而 `type(...) is ...` 要求**精确类型相等**。这里要的是「输入的精确类型与产物精确类型是否一致」，只有精确不一致时才需要重新 `view`，因此用 `is` 更准确。

---

### 4.5 sliding_window_view：安全的滚动窗口

#### 4.5.1 概念说明

`as_strided` 让你自由指定 shape/strides，但「自由」意味着你得自己算、自己防越界。大多数时候人们要的其实是同一种东西：**滚动窗口**（rolling/moving window）——在一个数组上滑动一个固定大小的窗口，每个位置取出一个子块。这就是 `sliding_window_view` 的职责。

它的设计哲学是：**不引入新机制，只把「窗口」翻译成正确的 shape/strides，然后交给 `as_strided`**。换句话说，它是 `as_strided` 的一层「参数计算器 + 安全封装」。

#### 4.5.2 核心流程

设输入 `x`，窗口在每个参与轴 `ax` 上的大小为 `window_shape[i]`。目标视图的两件套是：

**形状**——「窗口能滑动多少步」拼接上「窗口本身大小」。每个被滑窗的轴长度从 `n` 缩成 `n - (w - 1)`：

\[ \text{out\_shape} = \underbrace{x\_shape\_trimmed}_{\text{各轴 } n-(w-1)} \;+\; \underbrace{\text{window\_shape}}_{\text{窗口本身}} \]

**步长**——原数组的全部步长，再**追加**每个参与轴的步长。追加的那一段描述「在窗口内前进一格」该走多少字节，显然它就等于该轴本身的步长：

\[ \text{out\_strides} = x.\text{strides} \;+\; \big(\,x.\text{strides}[ax]\;\text{for } ax \in \text{axis}\,\big) \]

为什么这样拼？以 1D 为例，`out_shape = (n-w+1, w)`：

- 第 0 维（哪个窗口）：起点比上一窗口 +1 元素，步长 = `x.strides[0]`；
- 第 1 维（窗口内位置）：+1 元素，步长 = `x.strides[0]`。

于是 `out_strides = (s0, s0)`，正是「原 strides + 参与轴 strides」。多维情形同理，每个参与轴贡献一段自己的步长。

参数归一与校验流程：

```text
sliding_window_view(x, window_shape, axis, *, subok, writeable)
  │
  1. window_shape 归一成元组（单个整数 → (i,)）
  2. x = np.array(x, copy=None, subok=subok)
  3. 拒绝负的 window_shape
  4. 若 axis is None：axis = 全部轴，且 window_shape 长度必须 == x.ndim
     否则：normalize_axis_tuple(axis, ..., allow_duplicate=True)，长度需匹配 window_shape
  5. out_strides = x.strides + (x.strides[ax] for ax in axis)
  6. x_shape_trimmed：每个 ax 减去 (window_shape[i] - 1)，且不得小于窗口大小
  7. out_shape  = tuple(x_shape_trimmed) + window_shape
  8. return as_strided(x, strides=out_strides, shape=out_shape,
                        subok=subok, writeable=writeable)   ← 复用内核
```

`allow_duplicate=True`（第 4 步）是个细节：它允许**同一根轴被多次滑窗**，文档专门给了示例（[L320-L329](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L320-L329)）。

#### 4.5.3 源码精读

派发器与签名遵循 `u1-l2` 讲过的「dispatcher + impl 双函数」写法：[_sliding_window_view_dispatcher](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L172-L174) 只返回 `(x,)` 供 NEP-18 `__array_function__` 拦截，真正的逻辑在 [L409-L444](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L409-L444)：

```python
window_shape = (tuple(window_shape)
                if np.iterable(window_shape)
                else (window_shape,))          # 归一成元组
x = np.array(x, copy=None, subok=subok)

window_shape_array = np.array(window_shape)
if np.any(window_shape_array < 0):
    raise ValueError('`window_shape` cannot contain negative values')

if axis is None:
    axis = tuple(range(x.ndim))
    if len(window_shape) != len(axis):
        raise ValueError(...)                   # 必须为每一维都给窗口
else:
    axis = normalize_axis_tuple(axis, x.ndim, allow_duplicate=True)
    if len(window_shape) != len(axis):
        raise ValueError(...)                   # axis 与 window_shape 长度要匹配

out_strides = x.strides + tuple(x.strides[ax] for ax in axis)   # 拼步长

# 同一根轴可被多次滑窗
x_shape_trimmed = list(x.shape)
for ax, dim in zip(axis, window_shape):
    if x_shape_trimmed[ax] < dim:
        raise ValueError('window shape cannot be larger than input array shape')
    x_shape_trimmed[ax] -= dim - 1             # 每个轴缩 (w-1)
out_shape = tuple(x_shape_trimmed) + window_shape
return as_strided(x, strides=out_strides, shape=out_shape,
                  subok=subok, writeable=writeable)             # 复用 as_strided
```

读到这里应该有种「恍然大悟」：`sliding_window_view` 的全部智慧，就是算出 `out_strides` 和 `out_shape` 这两个元组；真正造视图的活儿，最后一行原封不动交给了 `as_strided`。`writeable=False` 是默认值，因为滑窗视图天然自重叠——窗口之间共享大量元素，写入一个位置会同时改掉多个窗口（见 docstring 示例 [L382-L405](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L382-L405)）。

#### 4.5.4 代码实践

**实践目标**：用 `sliding_window_view` 在 1D 数组上取长度为 3 的窗口，并求每个窗口的均值（移动平均）——这也是 docstring 给出的标准应用（[L348-L365](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L348-L365)）。

**操作步骤**：

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

x = np.arange(6)                       # [0,1,2,3,4,5]
v = sliding_window_view(x, 3)          # 默认 axis=None，对所有轴滑窗
print("shape:", v.shape)               # (4, 3)：4 个窗口，每个长 3
print(v)
# [[0 1 2]
#  [1 2 3]
#  [2 3 4]
#  [3 4 5]]

# 沿窗口维（最后一维）求均值 → 移动平均
moving_average = v.mean(axis=-1)
print("moving average:", moving_average)   # [1. 2. 3. 4.]
```

**需要观察的现象**：

- `v.shape` 是 `(4, 3)`：4 = `6 - (3-1)` 个滑动位置，3 是窗口长度——正好印证 `out_shape = x_shape_trimmed + window_shape = (4,) + (3,)`。
- `v` 的 strides 是 `(8, 8)`（int64），与第 4.2.4 节手工 `as_strided` 完全一致——证明二者同源。
- `moving_average` 是 `[1., 2., 3., 4.]`，即 `(0+1+2)/3, (1+2+3)/3, ...`。

**预期结果**：`v.mean(axis=-1)` 得到 `array([1., 2., 3., 4.])`，与 docstring 示例一致。

**延伸（可选）**：想调整滑动步长，不必改函数参数，直接对返回的视图切片即可——`v[::2]` 得到每隔一个的窗口（[L370-L372](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L370-L372)），`v[:, ::2]` 则在窗口内跳着取。

#### 4.5.5 小练习与答案

**练习 1**：对 `x = np.arange(6)` 调 `sliding_window_view(x, 3)`，为什么结果是 `(4, 3)` 而不是 `(6, 3)`？

> **答案**：第 0 个窗口覆盖下标 `[0,1,2]`，最后一个窗口必须完全落在数组内，其起点最大为 `6-3=3`，故起点取值 `0,1,2,3` 共 4 个，对应 `out_shape[0] = 6-(3-1)=4`。

**练习 2**：docstring 的 Notes 为什么说滑窗「常常不是最优解」，建议大窗口改用 `scipy.signal.fftconvolve` 之类？

> **答案**：滑窗把每个元素复制进多个窗口，复杂度是 \(O(N\cdot W)\)（N 数据量、W 窗口大小）。对大窗口这会爆炸——W=100 就可能比专用算法慢 100 倍。它适合小窗口、原型开发，或没有专用算法时的通用方案（见 [L253-L257](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L253-L257)）。

---

## 5. 综合实践

把本讲的知识串起来：**手工实现一个带步长的移动平均**，并全程用 `byte_bounds` / `check_bounds` 印证安全性。这个任务会同时用到 4.2（as_strided）、4.3（check_bounds + byte_bounds）、4.5（sliding_window_view 对照）三块。

任务：给定一个较长的一维信号，分别用三种方式计算窗口大小为 W、步长为 step 的移动平均，并比较结果与性能特征。

```python
# 示例代码（非项目原有代码）
import numpy as np
from numpy.lib.stride_tricks import as_strided, sliding_window_view
from numpy.lib import array_utils

signal = np.arange(20, dtype=np.float64)    # 20 个采样点
W, step = 4, 2

# —— 方式 A：sliding_window_view + 切片调步长（推荐）——
win = sliding_window_view(signal, W)        # shape=(17, 4)
ma_A = win[::step].mean(axis=-1)            # shape=(9,)

# —— 方式 B：原始 as_strided，手工算 shape/strides ——
n_windows = (len(signal) - W) // step + 1
out_strides = (signal.strides[0] * step, signal.strides[0])  # (步长字节, 单元素字节)
ma_B = as_strided(signal,
                  shape=(n_windows, W),
                  strides=out_strides,
                  writeable=False).mean(axis=-1)

# —— 方式 C：用 byte_bounds + check_bounds 印证 B 不越界 ——
view_for_check = as_strided(signal, shape=(n_windows, W),
                            strides=out_strides, check_bounds=True)  # 不抛异常即安全
low_b, high_b = array_utils.byte_bounds(signal)
low_v, high_v = array_utils.byte_bounds(view_for_check)
print("基类边界内：", low_v >= low_b and high_v <= high_b)   # True

print("A == B：", np.array_equal(ma_A, ma_B))               # True
print(ma_A)
```

**预期结果**：

- `ma_A == ma_B` 为 `True`——手工 `as_strided`（B）与官方 `sliding_window_view`（A）算出完全相同的移动平均，证明二者同源。
- `check_bounds=True` 不抛异常，且 `byte_bounds` 显示视图完全落在原信号缓冲区内（`low_v >= low_b and high_v <= high_b` 为 `True`）。
- 当 `step>1` 时，方式 B 的 `out_strides[0] = signal.strides[0]*step` 把「窗口起点步长」直接编码进 stride，比 A 的「先全量滑窗再切片」更省一次中间视图——这正是 `as_strided` 适合底层优化的场景。

**进阶思考**：尝试把 `W` 调大到 `len(signal)`，观察方式 B 在 `check_bounds=False`（默认）时会静默读到什么、`check_bounds=True` 时报什么错。这能帮你建立对「越界」的直觉。

## 6. 本讲小结

- **视图 = 换 shape/strides 读同一块内存**：步长（stride）描述沿某维前进一格的字节位移，可为 0（广播）、为负（反向）、大于 itemsize（跳读）；改写 shape/strides 不复制数据，因此 `as_strided` 总是零拷贝。
- **`DummyArray` 是注入任意 shape/strides 的官方后门**：它只负责挂载一份 `__array_interface__` 字典并保活 base，`np.asarray` 读到它就按字典共享内存造视图（[_stride_tricks_impl.py:15-22](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L15-L22)）。
- **`as_strided` 把这套招式封装成函数**：复制接口字典 → 改写 shape/strides → `asarray(DummyArray)` → `_set_dtype` 补回结构化 dtype → `_maybe_view_as_subclass` 还原子类 → 可选只读与越界校验（[L132-L169](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L132-L169)）。
- **`check_bounds` 借 `byte_bounds` 做越界校验**：先沿 `.base` 链下钻到 owner，再比对视图与 owner 的字节边界，越界即抛带定量信息的 `ValueError`（[L150-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L150-L167)）；`byte_bounds` 算法本身见 `u2-l3`。
- **`_maybe_view_as_subclass` 修补子类降级**：仅当输入精确类型与产物不同时，才用 `.view(type=...)` 还原并调用 `__array_finalize__` 传播子类状态（[L25-L35](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L25-L35)）。
- **`sliding_window_view` 是 `as_strided` 的安全封装**：它把「窗口」翻译成 `out_strides = x.strides + 参与轴 strides` 与 `out_shape = 各轴缩(w-1) + window_shape`，最后一行交给 `as_strided`（[L433-L444](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L433-L444)）；默认 `writeable=False`，因为滑窗视图天然自重叠。

## 7. 下一步学习建议

- **衔接 `u5-l2`（广播机制）**：同文件里的 `broadcast_to` / `broadcast_arrays` / `broadcast_shapes`（[L447-L656](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L447-L656)）是本讲的姊妹篇——广播的本质之一就是「插入 0 步长」，掌握了本讲的 stride 视角，再读广播会非常顺。
- **阅读 `__array_interface__` 的完整定义**：本讲只用到它的 `data/shape/strides/descr` 字段。完整协议（含 `version`、`maskna` 等）在 numpy 官方文档的 `arrays.interface` 章节有权威说明，理解它能帮你写出与 numpy 互操作的零拷贝类型。
- **回顾 `u2-l3` 的 `byte_bounds` 与 `NDArrayOperatorsMixin`**：本讲的 `check_bounds` 直接复用了那里的 `byte_bounds`，而 `_maybe_view_as_subclass` 调用的 `__array_finalize__` 又与 `NDArrayOperatorsMixin` 同属「子类协议」体系，连起来读能建立完整的「视图 + 子类 + 协议」心智模型。
- **进入 `u6`（数值处理函数）**：`diff`、`gradient`、`trapezoid` 等数值函数常与滑窗/步长视图配合使用（例如用滑窗做局部统计），把本讲的视图工具与 `u6` 的数值算法结合，能写出高效的滚动计算。
