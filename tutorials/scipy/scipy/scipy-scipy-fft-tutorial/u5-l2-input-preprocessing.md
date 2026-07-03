# 输入预处理：_asfarray / _fix_shape / _normalization / _init_nd_shape_and_axes

## 1. 本讲目标

本讲聚焦 `scipy.fft` 四层架构的最底层——计算核心 `_duccfft` 中**进入 C 扩展 `pyduccfft` 之前**的那一道「输入预处理」工序。

在 [u5-l1](./u5-l1-ducc-basic-kernels.md) 中我们看到，14 个公共基础变换最终都汇聚到 `c2c`/`r2c`/`c2r` 三个 Python 内核，再下沉到 C 扩展 `pyduccfft`（别名 `pfft`）。但 C 扩展是「挑剔」的：它要求传入的数组必须是**浮点或复数 dtype、原生字节序、内存对齐**；它要求**变换长度和形状已经被确定**；它要求**归一化模式用整数而非字符串**表达。如果让每个内核函数自己去处理这些杂事，代码会重复 14 遍。

`scipy/fft/_duccfft/helper.py` 正是把这些「进入 C 前的统一杂事」抽成一组小工具，被所有内核复用。学完本讲，你应当能够：

- 说清 `_asfarray` 如何把任意输入升级成「C 内核可吃」的浮点数组，以及它为何要处理字节序与对齐。
- 读懂 `_fix_shape` 如何用**纯切片**同时实现「截断（取视图，零拷贝）」和「补零（新建数组）」两种行为，并理解它和公共参数 `n` / `s` 的对应。
- 看清 `_normalization` 如何把用户友好的 `norm` 字符串映射成 `pyduccfft` 需要的整数 `0/1/2`，以及 `2 - inorm` 这个翻转为何能保证正逆变换恒可逆。
- 掌握 `_init_nd_shape_and_axes` 如何把多维变换的 `s` 与 `axes` 参数标准化成「目标形状 + 变换轴列表」，处理好负轴、默认值与 `-1` 通配。
- 能够手动复刻一个简化版 `_fix_shape`，并与源码行为逐一对照。

## 2. 前置知识

本讲默认你已掌握以下概念（来自前置讲义，此处只做最简回顾，不重复展开）：

- **四层架构**（[u1-l2](./u1-l2-directory-layout.md)）：公共 API 层 → uarray 分派层 → 后端层（`*_backend.py`）→ 计算核心层（`_duccfft` → C 扩展 `pyduccfft`）。本讲处在最底层内部。
- **c2c / r2c / c2r 三个内核与 `functools.partial` 派生**（[u5-l1](./u5-l1-ducc-basic-kernels.md)）：`fft = functools.partial(c2c, True)`，`ifft = functools.partial(c2c, False)`；`forward` 参数只决定 DFT 方向与归一化归属，不决定实/复属性。
- **公共参数 `n` / `axis` / `s` / `axes` / `norm`**（[u2-l1](./u2-l1-complex-fft.md)、[u2-l3](./u2-l3-multidimensional-fft.md)）：`n` 是 1-D 目标长度，`s` 是 N-D 各轴目标长度，`norm` 是 `backward`/`ortho`/`forward` 三选一。
- **DFT 基本公式**：正变换 \(X[k]=\sum_{j=0}^{n-1}x[j]\,e^{-2\pi ijk/n}\)，逆变换带 \(1/n\) 因子。

此外，本讲会用到几个 NumPy 基础概念，先用一句话解释：

| 术语 | 一句话解释 |
|---|---|
| `dtype.kind` | dtype 的「种类字母」，如 `'f'`=浮点、`'c'`=复数、`'i'`=整数、`'b'`=布尔。`kind in 'fc'` 即「浮点或复数」。 |
| 字节序（byte order） | 多字节数据在内存里的高低字节排列方式。大端/小端/原生（`'='`）。C 扩展通常只认原生字节序。 |
| 内存对齐（aligned） | 数据起始地址是否是某字节（如 16）的整数倍。SIMD 指令要求对齐才能用。`x.flags['ALIGNED']` 可查询。 |
| 视图（view） vs 拷贝（copy） | 切片可能返回共享内存的视图（零拷贝），也可能返回新数组。`np.shares_memory(a, b)` 可判断。 |

## 3. 本讲源码地图

本讲几乎全部内容都在一个文件里，外加一个内核文件用于观察「预处理如何被调用」。

| 文件 | 作用 | 本讲用到的主要符号 |
|---|---|---|
| [_duccfft/helper.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) | 进入 C 内核前的统一预处理工具集 | `_asfarray`、`_datacopied`、`_fix_shape`、`_fix_shape_1d`、`_NORM_MAP`、`_normalization`、`_init_nd_shape_and_axes`、`_iterable_of_int` |
| [_duccfft/basic.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py) | 三个内核 `c2c`/`r2c`/`c2r` 及其 N-D 版本 | 观察它们如何按固定顺序调用 helper 中的工具 |

整个预处理流程在内核里是**固定顺序**的，以 `c2c` 为例（1-D 复变换）：

```
c2c(forward, x, n, axis, norm, overwrite_x, workers, plan):
    1. 拒绝 plan                          # basic.py:14
    2. tmp = _asfarray(x)                 # 浮点化 + 字节序 + 对齐   ← 模块 4.1
    3. overwrite_x = overwrite_x or _datacopied(tmp, x)   # 是否已拷贝
    4. norm = _normalization(norm, forward)               # 字符串→整数 ← 模块 4.3
    5. workers = _workers(workers)
    6. 若给了 n：tmp, copied = _fix_shape_1d(tmp, n, axis) # 截断/补零 ← 模块 4.2
    7. out = (tmp if overwrite_x and 复数 else None)       # 决定能否原地写
    8. return pfft.c2c(tmp, (axis,), forward, norm, out, workers)  # 进入 C
```

N-D 版本 `c2cn` 把第 6 步换成「先 `_init_nd_shape_and_axes`（模块 4.4）再 `_fix_shape`（模块 4.2）」。把握住这个顺序，本讲四个模块的位置就清晰了。

---

## 4. 核心概念与源码讲解

### 4.1 `_asfarray`：把输入变成 C 内核能吃的浮点数组

#### 4.1.1 概念说明

`pyduccfft` 是用 C++（pybind11）写的，底层 ducc 库的 FFT 内核只认**浮点或复数**的连续数值数组——它不会替你把整数、布尔、字符串转成浮点，遇到非法类型直接崩溃或给出错误结果。此外，C 内核为了用 SIMD 加速，通常还要求：

- **原生字节序**（native byte order）：非原生字节序的数组（罕见，多见于跨架构读取的二进制数据）需转换。
- **内存对齐**：未对齐的数组无法走 SIMD 快路径。

`_asfarray` 就是这道「安检口」：把任意 array-like 统一升级成「浮点/复数 + 原生字节序 + 对齐」的 NumPy 数组，同时尽量**避免不必要的拷贝**（拷贝既费内存又破坏 `overwrite_x` 的语义）。

函数名里的 `asfarray` = "as floating array"，源自 NumPy 老牌函数 `asfarray`（把输入转成浮点数组）。这里加了下划线表示私有。

#### 4.1.2 核心流程

```
_asfarray(x):
  1. 若 x 没有 dtype 属性（如 list、scalar）：先 np.asarray(x) 兜底
  2. 若 dtype 是 float16：升级为 float32          # 半精度太窄，FFT 会溢出
  3. 否则若 dtype.kind 不属于 'fc'（即整数/布尔等）：转为 float64
  4. 否则（已是 float32/64/complex）：
       a. 强制原生字节序：dtype = x.dtype.newbyteorder('=')
       b. 若数组未对齐：强制拷贝；否则按 copy_if_needed 策略
       c. 返回 np.array(x, dtype=dtype, copy=copy)
```

注意第 4 步的两个细节：

- `newbyteorder('=')` 把字节序声明改成「原生」，**但并不立即搬动数据**——只有当原数组本就是原生字节序时这步是无开销的标记操作；若不是，配合 `np.array(..., dtype=...)` 才会真正做字节重排。
- `copy_if_needed`（从 `scipy._lib._util` 导入）是一个三态拷贝策略常量，语义近似 NumPy 2.x 的 `copy=` 参数：仅在必要时才拷贝，避免对已经合格的输入再做一次冗余拷贝。

#### 4.1.3 源码精读

[_duccfft/helper.py:L114-L132](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L114-L132) —— `_asfarray` 的完整实现，做「dtype 升级 + 字节序统一 + 对齐拷贝」三件事：

```python
def _asfarray(x):
    """Convert to array with floating or complex dtype.
    float16 values are also promoted to float32.
    """
    if not hasattr(x, "dtype"):
        x = np.asarray(x)

    if x.dtype == np.float16:
        return np.asarray(x, np.float32)
    elif x.dtype.kind not in 'fc':
        return np.asarray(x, np.float64)

    # Require native byte order
    dtype = x.dtype.newbyteorder('=')
    # Always align input
    copy = True if not x.flags['ALIGNED'] else copy_if_needed
    return np.array(x, dtype=dtype, copy=copy)
```

逐行对照：

- 第 120–121 行：`hasattr(x, "dtype")` 为假说明 `x` 不是 ndarray（可能是 list、tuple、Python 标量），用 `np.asarray` 先包装一层。此后 `x` 必有 `.dtype`。
- 第 123–124 行：`float16`（半精度，仅约 3 位十进制有效数字）直接升 `float32`——FFT 中间过程的动态范围远超 float16 容量，不升级会严重失真。
- 第 125–126 行：`kind not in 'fc'` 捕获所有「非浮点非复数」类型，包括 `int8/16/32/64`、`uint*`、`bool`，统一升 `float64`。
- 第 129 行：`newbyteorder('=')` 把字节序标记为原生。
- 第 131 行：`x.flags['ALIGNED']` 为假（未对齐）时强制 `copy=True`；否则用 `copy_if_needed` 把「是否拷贝」的决策权交给 NumPy。

> **关键结论**：`_asfarray` 可能返回**与输入共享内存的视图**（当输入已是合格的 float32/64/complex 且对齐时），也可能返回**全新数组**（整数/布尔输入、float16、未对齐）。这个「是否拷贝」的差别，会直接传递给下一步 `_datacopied`，进而影响 `overwrite_x` 的最终取值。

#### 4.1.4 代码实践

**实践目标**：观察 `_asfarray` 对不同 dtype 输入的升级行为，以及「是否拷贝」的差别。

**操作步骤**（需在本地装有 scipy 的环境运行）：

```python
import numpy as np
from scipy.fft._duccfft.helper import _asfarray, _datacopied

# (a) 整数输入 -> float64，且必然是新数组
xi = np.arange(4)
ti = _asfarray(xi)
print("int ->", ti.dtype, "shares_memory=", np.shares_memory(ti, xi))

# (b) float16 -> float32，新数组
xh = np.arange(4, dtype=np.float16)
th = _asfarray(xh)
print("float16 ->", th.dtype, "shares_memory=", np.shares_memory(th, xh))

# (c) 已合格的 float64：可能共享内存（视图）
xf = np.arange(4, dtype=np.float64)
tf = _asfarray(xf)
print("float64 ->", tf.dtype, "shares_memory=", np.shares_memory(tf, xf))

# (d) 用 _datacopied 判断"是否真的拷贝了"
print("datacopied(int) =", _datacopied(ti, xi))
```

**需要观察的现象与预期结果**（依据源码推导，供本地核对）：

- (a) `int -> float64 shares_memory= False`：整数升浮点必产生新内存。
- (b) `float16 -> float32 shares_memory= False`：精度提升必拷贝。
- (c) `float64 -> float64 shares_memory= True`：已合格输入返回视图，零拷贝（具体取决于 NumPy 版本对 `copy_if_needed` 的实现，个别情况下也可能为 `False`）。
- (d) `datacopied(int) = True`。

> 若运行环境与本讲一致，结果应如上；不同 NumPy 版本下 (c) 的 `shares_memory` 可能有差异，但 `_datacopied` 对整数输入恒为 `True`。

#### 4.1.5 小练习与答案

**练习 1**：把一个 `bool` 数组 `np.array([True, False, True])` 传入 `_asfarray`，输出 dtype 是什么？为什么？

**答案**：`float64`。因为 `bool` 的 `dtype.kind` 是 `'b'`，不属于 `'fc'`，命中第 125–126 行的 `elif` 分支，转 `float64`。`True` 会变成 `1.0`，`False` 变成 `0.0`。

**练习 2**：为什么 `_asfarray` 对 `float16` 单独升级到 `float32`，而不是像整数那样升到 `float64`？

**答案**：`float16` 已经是浮点，问题只是**精度/动态范围太窄**；FFT 中间结果的量级可能远超 `float16` 的表示范围（最大约 65504），升到 `float32`（约 \(3.4\times10^{38}\)，7 位有效数字）已足够且比 `float64` 省一半内存。整数则完全不是浮点，需要先「跨类型」成浮点，默认给最通用的 `float64`。

---

### 4.2 `_fix_shape` / `_fix_shape_1d`：截断与补零

#### 4.2.1 概念说明

公共参数 `n`（1-D）或 `s`（N-D）让用户能指定「我希望在长度为 n 的信号上做 FFT」，而不管输入实际长度。这衍生出两种情况：

- **输入比 n 长**：截断（truncate），只取前 n 个样本。
- **输入比 n 短**：补零（zero-pad），在末尾补 0 凑到 n。

`_fix_shape` 就是干这件事的，而且有一个精妙的设计目标：**截断时零拷贝（返回视图），补零时才新建数组**。这是因为截断只是「少读几个元素」，完全可以靠切片视图实现；而补零要往不存在的位置写 0，必须分配新内存。

这个「是否拷贝」的布尔返回值会一路传回内核，参与决定 `overwrite_x`（能否原地写入输出）——如果预处理已经拷贝过，那后面就可以放心原地写，因为写的已经不是用户的原数组了。

`_fix_shape_1d` 是 `_fix_shape` 的 1-D 薄封装，把单个 `(n, axis)` 包装成 `(n,), (axis,)` 的列表形式，顺带校验 `n >= 1`。

#### 4.2.2 核心流程

```
_fix_shape(x, shape, axes):        # shape[i] 控制 axes[i]
  1. must_copy = False
  2. 构造一个全选切片 index = [slice(None)] * x.ndim
  3. 对每一对 (n, ax) in zip(shape, axes):
       若 x.shape[ax] >= n：index[ax] = slice(0, n)        # 截断：只读前 n 个
       否则：index[ax] = slice(0, x.shape[ax]); must_copy = True  # 读全部，待会补零
  4. 若 not must_copy：return x[index], False              # 纯截断 → 视图
  5. 否则：
       a. 构造目标全形状 s（把 axes 各维替换成 n）
       b. z = np.zeros(s, x.dtype)
       c. z[index] = x[index]                              # 把原数据拷进零数组
       d. return z, True
```

关键洞见：第 4 步的 `x[index]` 在纯截断时返回**视图**（`shares_memory` 为真），零拷贝；第 5 步的补零路径才真正分配 `np.zeros` 并拷贝。注意 `index` 是一个 N 维切片元组，`x[index]` 同时处理「某些轴截断、某些轴保持不变」的混合情况。

`_fix_shape_1d` 的逻辑：

```
_fix_shape_1d(x, n, axis):
  1. 若 n < 1：raise ValueError("invalid number of data points")
  2. return _fix_shape(x, (n,), (axis,))
```

#### 4.2.3 源码精读

[_duccfft/helper.py:L146-L170](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L146-L170) —— `_fix_shape`，用纯切片同时实现截断与补零：

```python
def _fix_shape(x, shape, axes):
    """Internal auxiliary function for _raw_fft, _raw_fftnd."""
    must_copy = False

    # Build an nd slice with the dimensions to be read from x
    index = [slice(None)]*x.ndim
    for n, ax in zip(shape, axes):
        if x.shape[ax] >= n:
            index[ax] = slice(0, n)
        else:
            index[ax] = slice(0, x.shape[ax])
            must_copy = True

    index = tuple(index)

    if not must_copy:
        return x[index], False

    s = list(x.shape)
    for n, axis in zip(shape, axes):
        s[axis] = n

    z = np.zeros(s, x.dtype)
    z[index] = x[index]
    return z, True
```

逐段对照：

- 第 151 行：`index = [slice(None)]*x.ndim` 初始化为「每条轴都全选」的切片列表。
- 第 152–157 行循环：按位置 `zip(shape, axes)` 配对（这正是 [u2-l3](./u2-l3-multidimensional-fft.md) 强调的「`s[i]` 控 `axes[i]`，非第 i 条绝对轴」）。对每条目标轴：够长就截到 `n`，不够长就标记 `must_copy`。
- 第 161–162 行：纯截断分支，`x[index]` 是视图，返回 `(视图, False)`。
- 第 164–170 行：补零分支。先把目标形状 `s` 里需要变换的轴改成 `n`，`np.zeros` 建零数组，再用 `z[index] = x[index]` 把原数据搬进对应位置，其余位置自然是 0。

[_duccfft/helper.py:L173-L178](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L173-L178) —— `_fix_shape_1d`，1-D 封装加长度校验：

```python
def _fix_shape_1d(x, n, axis):
    if n < 1:
        raise ValueError(
            f"invalid number of data points ({n}) specified")

    return _fix_shape(x, (n,), (axis,))
```

再看它在内核里如何被调用。[_duccfft/basic.py:L11-L31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L11-L31) —— `c2c` 内核中第 22–24 行：

```python
    if n is not None:
        tmp, copied = _fix_shape_1d(tmp, n, axis)
        overwrite_x = overwrite_x or copied
```

`copied` 会被「或」进 `overwrite_x`：只要预处理拷贝过，后续就允许原地写。这是预处理与 `overwrite_x` 语义联动的关键一行。

> **关键结论**：`_fix_shape` 的返回值 `(数组, 是否拷贝)` 中，第二个布尔量不是「装饰」，它直接决定内核能否安全地把计算结果写回 `tmp`（见 `c2c` 第 29 行 `out = (tmp if overwrite_x and tmp.dtype.kind == 'c' else None)`）。

#### 4.2.4 代码实践

**实践目标**：手动实现一个简化版 `_fix_shape`，与源码在三种情形（纯截断、纯补零、混合）下逐一对照。

**操作步骤**：

```python
import numpy as np
from scipy.fft._duccfft.helper import _fix_shape

def my_fix_shape(x, shape, axes):
    """简化版：与源码行为对照。返回 (out, copied)。"""
    must_copy = False
    index = [slice(None)] * x.ndim
    for n, ax in zip(shape, axes):
        if x.shape[ax] >= n:
            index[ax] = slice(0, n)
        else:
            index[ax] = slice(0, x.shape[ax])
            must_copy = True
    index = tuple(index)
    if not must_copy:
        return x[index], False
    s = list(x.shape)
    for n, axis in zip(shape, axes):
        s[axis] = n
    z = np.zeros(s, x.dtype)
    z[index] = x[index]
    return z, True

# 三种情形对照
x1 = np.arange(10, dtype=np.float64)          # 1-D
for desc, shape, axes in [("截断 10->8", (8,), (0,)),
                          ("补零 10->16", (16,), (0,))]:
    o_mine, c_mine = my_fix_shape(x1, shape, axes)
    o_ref,  c_ref  = _fix_shape(x1, shape, axes)
    print(desc, "| mine: copied=%d shares=%d | ref: copied=%d shares=%d | equal=%s"
          % (c_mine, np.shares_memory(o_mine, x1),
             c_ref,  np.shares_memory(o_ref,  x1),
             np.array_equal(o_mine, o_ref)))

x2 = np.ones((4, 5), dtype=np.float64)         # 2-D 混合：轴0截断、轴1补零
o_mine, c_mine = my_fix_shape(x2, (3, 8), (0, 1))
o_ref,  c_ref  = _fix_shape(x2, (3, 8), (0, 1))
print("2D (4,5)->(3,8) | mine: copied=%d shape=%s | ref: copied=%d shape=%s | equal=%s"
      % (c_mine, o_mine.shape, c_ref, o_ref.shape, np.array_equal(o_mine, o_ref)))
```

**需要观察的现象与预期结果**（依据源码推导）：

| 情形 | `copied`（mine/ref） | `shares_memory` | `equal` |
|---|---|---|---|
| 截断 10→8 | `False / False` | `True`（视图） | `True` |
| 补零 10→16 | `True / True` | `False`（新数组） | `True` |
| 2D (4,5)→(3,8) | `True / True`（轴1需补零） | `False` | `True` |

你的 `my_fix_shape` 应与源码在数值、形状、拷贝标志上**完全一致**。这也印证了源码确实只用了「切片 + 条件零数组」这一朴素手段。

#### 4.2.5 小练习与答案

**练习 1**：若调用 `_fix_shape(x, (10,), (0,))`，而 `x` 恰好长度也是 10，会发生什么？返回的数组与 `x` 共享内存吗？

**答案**：进入循环时 `x.shape[0] >= 10` 为真，`index[0] = slice(0, 10)`（等价于全选），`must_copy` 保持 `False`，走第 161–162 行返回 `x[index], False`。`x[slice(0,10)]` 是 `x` 的视图，共享内存。即「长度恰好相等」被归入截断分支，零拷贝。

**练习 2**：为什么补零路径要用 `z[index] = x[index]` 而不是先把 `x` 拷进 `z` 再填零？

**答案**：因为可能只有**部分轴**需要补零、其他轴是截断或不变。`z = np.zeros(...)` 已经把所有位置初始化为 0，再用 `z[index] = x[index]` 一次性把「该读的子区域」搬进去，剩下没覆盖的位置天然是 0。这比「先全拷再补零」更省事，也正确处理了多维混合情形。

**练习 3**：`_fix_shape_1d` 为什么要单独校验 `n < 1`，而 `_fix_shape` 没有？

**答案**：`_fix_shape` 处理 N-D，其 `shape` 的合法性（每个 `s >= 1`）由上游 `_init_nd_shape_and_axes`（模块 4.4）统一校验（见其第 107–109 行）。而 1-D 路径（`c2c`/`r2c`/`c2r`）可能直接拿用户传入的 `n` 调 `_fix_shape_1d`，未经 `_init_nd_shape_and_axes`，所以 1-D 封装自己补一道 `n < 1` 校验。

---

### 4.3 `_normalization` 与 `_NORM_MAP`：norm 字符串到整数

#### 4.3.1 概念说明

公共 API 让用户用人类友好的字符串 `norm='backward'/'ortho'/'forward'`（或 `None`）指定归一化方式，但 C 扩展 `pyduccfft` 只接受一个**整数**模式。`_normalization` 就是这个翻译器。

回忆三种归一化模式（[u2-l1](./u2-l1-complex-fft.md) 已建立，这里用数学式写清）对正变换 \(X[k]=\sum_j x[j]e^{-2\pi ijk/n}\) 与逆变换的缩放约定：

- **backward**（默认）：正变换不缩放，逆变换乘 \(1/n\)。
- **forward**：正变换乘 \(1/n\)，逆变换不缩放。
- **ortho**：正、逆都乘 \(1/\sqrt{n}\)。

ducc/FFTPack 家族的传统做法是：用一个整数 `inorm` 表示「**当前这次变换**要施加多大的 \(1/n\) 权重」，约定：

\[ \text{实际缩放} \in \{1,\ 1/\sqrt{n},\ 1/n\} \quad\Longleftrightarrow\quad \text{inorm} \in \{0,\ 1,\ 2\} \]

也就是 `inorm` 直接编码了缩放因子里 \(1/n\) 的指数（0、1/2、1，离散化为 0、1、2）。

#### 4.3.2 核心流程

`_NORM_MAP` 把字符串翻译成「基础 inorm」：

```python
_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}
```

注意 `None` 与 `'backward'` 都映射到 0——这与「默认 `norm=None` 等价于 `backward`」的公共约定一致（见 [u2-l1](./u2-l1-complex-fft.md)）。

`_normalization(norm, forward)` 再根据**方向**做一次翻转：

```
_normalization(norm, forward):
  1. inorm = _NORM_MAP[norm]                  # 0 / 1 / 2
  2. 若 forward（正变换）：return inorm
  3. 否则（逆变换）：  return 2 - inorm
  4. 若 norm 不在表中：raise ValueError(...)
```

为什么逆变换要 `2 - inorm`？为了让「正逆配对」的缩放乘积恰好是 \(1/n\)，从而保证 `ifft(fft(x)) == x`（数值上 `allclose`）。下面用一张表看清：

| `norm`（用户字符串） | `inorm`（基础） | 正变换 `forward=True` 得到的整数 | 逆变换 `forward=False` 得到的整数 | 正×逆 缩放乘积 |
|---|---|---|---|---|
| `backward`/`None` | 0 | 0（正变换不缩放，权重 1） | 2（逆变换 \(1/n\)） | \(1 \times \tfrac{1}{n}=\tfrac{1}{n}\) |
| `ortho` | 1 | 1（\(1/\sqrt{n}\)） | \(2-1=1\)（\(1/\sqrt{n}\)） | \(\tfrac{1}{\sqrt n}\times\tfrac{1}{\sqrt n}=\tfrac{1}{n}\) |
| `forward` | 2 | 2（正变换 \(1/n\)） | \(2-2=0\)（逆变换不缩放） | \(\tfrac{1}{n}\times 1=\tfrac{1}{n}\) |

三种模式下正逆配对的乘积**都是 \(1/n\)**。再加上 DFT 求和本身在「正变换不缩放 + 逆变换求和」时会产生一个 \(n\) 的因子，最终正逆合起来的净缩放为 \(n \times \tfrac{1}{n}=1\)，即完美还原。这就是 `2 - inorm` 这一行的全部意义。

> 用公式概括这个翻转的不变量：设正变换得到的整数为 \(p\)、逆变换为 \(q\)，则有 \(p+q=2\)（backward/forward）或 \(p=q=1\)（ortho），两者都满足「\(p\) 与 \(q\) 对应的缩放乘积为 \(1/n\)」。

#### 4.3.3 源码精读

[_duccfft/helper.py:L181](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181) —— 映射表（单行）：

```python
_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}
```

[_duccfft/helper.py:L184-L192](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L184-L192) —— `_normalization`，查表 + 方向翻转 + 友好报错：

```python
def _normalization(norm, forward):
    """Returns the pyduccfft normalization mode from the norm argument"""
    try:
        inorm = _NORM_MAP[norm]
        return inorm if forward else (2 - inorm)
    except KeyError:
        raise ValueError(
            f'Invalid norm value {norm!r}, should '
            'be "backward", "ortho" or "forward"') from None
```

两个细节值得注意：

- 第 187 行：`_NORM_MAP[norm]` 用 `try/except KeyError` 捕获非法字符串，转成带提示的 `ValueError`。`from None` 链式语法屏蔽掉底层 `KeyError` 的 traceback，只给用户一句清晰提示。
- 第 188 行：`return inorm if forward else (2 - inorm)` 是整个归一化的「方向相关」核心，一行搞定。

再看它在内核里的调用。[_duccfft/basic.py:L19](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L19) —— `c2c` 中：

```python
    norm = _normalization(norm, forward)
```

注意传入的是内核自己的 `forward` 形参（`c2c` 被 `partial(c2c, True)` 派生成 `fft`，故 `fft` 调用时 `forward=True`；`ifft` 则 `forward=False`）。算出的整数 `norm` 随后原样传给 C 扩展 [_duccfft/basic.py:L31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L31)：`pfft.c2c(tmp, (axis,), forward, norm, out, workers)`。

> **关键结论**：`_normalization` 把「字符串 norm + 布尔 forward」二元组压缩成单个整数；`2 - inorm` 这一翻转是保证「任意 norm 模式下正逆配对都还原原始信号」的数学不变量。C 层（`pyduccfft`）拿到这个整数后，按 \(0/1/2 \to 1/\tfrac{1}{\sqrt n}/\tfrac{1}{n}\) 的约定施加缩放。

#### 4.3.4 代码实践

**实践目标**：用一个表穷举所有 `(norm, forward)` 组合，验证 `_normalization` 的输出符合上文的「配对求和」不变量。

**操作步骤**：

```python
from scipy.fft._duccfft.helper import _normalization, _NORM_MAP

print("NORM_MAP =", _NORM_MAP)
print("%-10s %-8s %-8s" % ("norm", "fft(+)", "ifft(-)"))
for nm in (None, "backward", "ortho", "forward"):
    fwd = _normalization(nm, True)     # 正变换
    inv = _normalization(nm, False)    # 逆变换
    print("%-10r %-8d %-8d  sum=%d" % (nm, fwd, inv, fwd + inv))

# 验证报错
try:
    _normalization("sideways", True)
except ValueError as e:
    print("error:", e)
```

**需要观察的现象与预期结果**（依据源码推导）：

```
NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}
norm       fft(+)   ifft(-)
None       0        2          sum=2
'backward' 0        2          sum=2
'ortho'    1        1          sum=2
'forward'  2        0          sum=2
error: Invalid norm value 'sideways', should be "backward", "ortho" or "forward"
```

四种合法组合的 `fft(+)+ifft(-)` **都等于 2**，正是「配对乘积为 \(1/n\)」的整数表现。非法字符串触发带提示的 `ValueError`。

#### 4.3.5 小练习与答案

**练习 1**：用户调用 `scipy.fft.fft(x, norm='forward')` 再 `scipy.fft.ifft(X, norm='forward')`。两次调用各自传给 C 扩展的整数 `norm` 是多少？乘积对应的缩放是什么？

**答案**：`fft` 的 `forward=True`，`_normalization('forward', True)=2`；`ifft` 的 `forward=False`，`_normalization('forward', False)=2-2=0`。整数对 `(2, 0)`，对应「正变换 \(1/n\)、逆变换不缩放」，乘积 \(1/n\)。配合求和的 \(n\) 因子，净缩放为 1，能还原 `x`。

**练习 2**：为什么 `_NORM_MAP` 里 `None` 和 `'backward'` 都映射到 0？

**答案**：因为公共约定是「`norm=None` 等价于默认的 `backward`」（见 [u2-l1](./u2-l1-complex-fft.md)）。把两者映射到同一整数 0，就在 `_normalization` 内部统一了「不传 norm」和「显式传 backward」两种用法，无需在内核里写 `if norm is None: norm = 'backward'` 的特判。

**练习 3**：若把第 188 行的 `2 - inorm` 改成 `inorm`（去掉翻转），会发生什么？

**答案**：正逆变换会得到相同的整数。例如 `backward` 模式下 `fft` 和 `ifft` 都得到 0（都不缩放），于是 `ifft(fft(x))` 会比 `x` 大 \(n\) 倍（少了逆变换的 \(1/n\)），**不再可逆**。翻转 `2 - inorm` 正是为了把 \(1/n\) 在正逆之间正确分配，保证可逆。

---

### 4.4 `_init_nd_shape_and_axes`：N-D 形状与轴的标准化

#### 4.4.1 概念说明

N-D 变换（`fftn`/`rfftn`/`hfftn` 等）接收两个公共参数 `s`（各轴目标长度）和 `axes`（要变换哪些轴）。这两个参数非常灵活：

- 都不传 → 在**所有轴**上变换，形状取原数组形状。
- 只传 `axes` → 只变换指定轴，形状取这些轴的原长度。
- 只传 `s` → 默认作用在**最后 `len(s)` 条轴**，形状用 `s`。
- 都传 → 按**位置配对**（`s[i]` 配 `axes[i]`），长度必须相等。

此外还要处理：负轴索引（`-1` 表示最后一条轴）、`s` 里的 `-1` 通配（表示「这一轴用原长度」）、各种合法性校验（轴不越界、不重复、形状非零）。

`_init_nd_shape_and_axes` 就是把这些灵活的输入**标准化**成一个确定的 `(shape, axes)` 元组：`shape` 是各变换轴的目标长度列表，`axes` 是变换轴的**非负**索引列表。它是 N-D 变换的「参数解析器」，地位类似 1-D 中「`n` 与 `axis` 的合体校验」。

#### 4.4.2 核心流程

```
_init_nd_shape_and_axes(x, shape, axes):
  noshape = (shape is None); noaxes = (axes is None)

  # ---- 处理 axes ----
  if axes 给了:
      axes = _iterable_of_int(axes)                 # 标量/序列 -> int 列表
      axes = [a + x.ndim if a < 0 else a for a in axes]   # 负轴转正
      校验：每条轴 0 <= a < x.ndim；轴不重复

  # ---- 处理 shape ----
  if shape 给了:
      shape = _iterable_of_int(shape)
      if axes 也给了 且 长度不等: raise
      if axes 没给:
          if len(shape) > x.ndim: raise
          axes = range(x.ndim - len(shape), x.ndim)  # 默认最后 len(shape) 条轴
      shape = [x.shape[a] if s == -1 else s for s, a in zip(shape, axes)]  # -1 通配
  elif axes 没给:        # 两者都没给
      shape = list(x.shape); axes = range(x.ndim)
  else:                  # 只给了 axes
      shape = [x.shape[a] for a in axes]

  if any(s < 1 for s in shape): raise
  return tuple(shape), list(axes)
```

其中 `_iterable_of_int`（[_duccfft/helper.py:L22-L44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L22-L44)）是个辅助函数：把「标量或序列」统一成「int 列表」——若传入单个数字就用 `(x,)` 包一层，再对每个元素调 `operator.index` 强制转 int（拒绝 `3.5` 这类）。

#### 4.4.3 源码精读

[_duccfft/helper.py:L47-L111](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L47-L111) —— `_init_nd_shape_and_axes` 完整实现：

```python
    noshape = shape is None
    noaxes = axes is None

    if not noaxes:
        axes = _iterable_of_int(axes, 'axes')
        axes = [a + x.ndim if a < 0 else a for a in axes]

        if any(a >= x.ndim or a < 0 for a in axes):
            raise ValueError("axes exceeds dimensionality of input")
        if len(set(axes)) != len(axes):
            raise ValueError("all axes must be unique")

    if not noshape:
        shape = _iterable_of_int(shape, 'shape')

        if axes and len(axes) != len(shape):
            raise ValueError("when given, axes and shape arguments"
                             " have to be of the same length")
        if noaxes:
            if len(shape) > x.ndim:
                raise ValueError("shape requires more axes than are present")
            axes = range(x.ndim - len(shape), x.ndim)

        shape = [x.shape[a] if s == -1 else s for s, a in zip(shape, axes)]
    elif noaxes:
        shape = list(x.shape)
        axes = range(x.ndim)
    else:
        shape = [x.shape[a] for a in axes]

    if any(s < 1 for s in shape):
        raise ValueError(
            f"invalid number of data points ({shape}) specified")

    return tuple(shape), list(axes)
```

逐段对照：

- 第 80–87 行（处理 `axes`）：先 `_iterable_of_int` 规整，再把负轴 `a + x.ndim` 转正（如 3 维下 `-1 → 2`）。随后两道校验：不越界（`0 <= a < x.ndim`）、不重复（`len(set(axes)) == len(axes)`）。
- 第 89–100 行（处理 `shape` 且 `shape` 已给）：规整后，若 `axes` 也给了则要求长度相等；若 `axes` 没给则默认取**最后 `len(shape)` 条轴**（`range(x.ndim - len(shape), x.ndim)`）。最后第 100 行把 `shape` 中的 `-1` 替换成该轴原长 `x.shape[a]`——这就是 `-1` 通配语义。
- 第 101–105 行（`shape` 没给的两种子情况）：两者都没给 → 全轴、原形状；只给 `axes` → 形状取这些轴的原长。
- 第 107–109 行：最终校验所有目标长度 `>= 1`。

再看它在 N-D 内核里的调用。[_duccfft/basic.py:L126-L149](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L126-L149) —— `c2cn`（N-D 复变换）第 136–137 行：

```python
    shape, axes = _init_nd_shape_and_axes(tmp, s, axes)
    ...
    tmp, copied = _fix_shape(tmp, shape, axes)
```

注意 N-D 路径是「先用 `_init_nd_shape_and_axes` 解析出标准 `(shape, axes)`，再交给模块 4.2 的 `_fix_shape` 去做实际的截断/补零」。两个函数职责分明：前者**解析参数**，后者**修改数组**。

> **关键结论**：`_init_nd_shape_and_axes` 把灵活的 `(s, axes)` 输入归一化为「非负轴列表 + 对应目标长度列表」，并处理好「默认最后几轴」「负轴」「`-1` 通配」三大便利。它只做**解析与校验**，不碰数组数据；真正改形状是 `_fix_shape` 的事。

#### 4.4.4 代码实践

**实践目标**：用一个 3-D 数组 `(4,5,6)` 穷举四种 `(s, axes)` 组合，观察标准化结果，验证「默认最后几轴」与「负轴转正」。

**操作步骤**：

```python
import numpy as np
from scipy.fft._duccfft.helper import _init_nd_shape_and_axes

x = np.zeros((4, 5, 6))

# (a) 都不传 -> 全轴、原形状
print("(a) None,None ->", _init_nd_shape_and_axes(x, None, None))
# (b) 只传 axes=[0,-1] -> 形状取这些轴原长，-1 转成 2
print("(b) None,[0,-1] ->", _init_nd_shape_and_axes(x, None, [0, -1]))
# (c) 只传 s=(7,8) -> 默认最后 2 条轴(1,2)，形状 (7,8)
print("(c) (7,8),None ->", _init_nd_shape_and_axes(x, (7, 8), None))
# (d) 都传，s 含 -1 通配：轴 0 用原长 4，轴 2 用 8
print("(d) (-1,8),(0,2) ->", _init_nd_shape_and_axes(x, (-1, 8), (0, 2)))

# (e) 校验：重复轴
try:
    _init_nd_shape_and_axes(x, None, [0, 0])
except ValueError as e:
    print("(e) dup axes:", e)
```

**需要观察的现象与预期结果**（依据源码推导）：

```
(a) None,None -> ((4, 5, 6), [0, 1, 2])
(b) None,[0,-1] -> ((4, 6), [0, 2])
(c) (7,8),None -> ((7, 8), [1, 2])
(d) (-1,8),(0,2) -> ((4, 8), [0, 2])
(e) dup axes: all axes must be unique
```

要点核对：

- (a) 全轴、原形状，`axes` 是 `range` 转成的 `[0,1,2]`。
- (b) `-1` 被转成 `2`，形状取轴 0、轴 2 的原长 `4`、`6`。
- (c) 只给 `s=(7,8)` → 默认最后 2 条轴 `[1,2]`，形状就是 `(7,8)`。
- (d) `s` 中的 `-1` 被替换成轴 0 的原长 `4`，得到 `(4,8)`。
- (e) 重复轴 `[0,0]` 触发 `all axes must be unique`。

#### 4.4.5 小练习与答案

**练习 1**：对一个 `(4,5,6)` 数组调用 `_init_nd_shape_and_axes(x, (8,3), (0,-1))`，返回的 `shape` 和 `axes` 各是什么？

**答案**：`axes`：先 `_iterable_of_int` 得 `[0,-1]`，负轴 `-1+3=2`，转成 `[0,2]`；两者都在 `[0,3)` 内且不重复。`shape`：`[8,3]` 无 `-1`，按位置配对 `(0,2)` 直接得 `[8,3]`。最终 `((8, 3), [0, 2])`。

**练习 2**：调用 `_init_nd_shape_and_axes(x, (7,8,9,10), None)`，对 3-D 数组 `x` 会怎样？

**答案**：`shape` 给了但 `axes` 没给，进入第 95–98 行：`len(shape)=4 > x.ndim=3`，触发 `ValueError("shape requires more axes than are present")`。即「只给 `s` 时，`s` 的长度不能超过数组维数」，因为它要默认作用到最后 `len(s)` 条轴上。

**练习 3**：`s` 里的 `-1` 和 `axes` 里的 `-1` 含义一样吗？

**答案**：不一样。`axes` 里的 `-1` 是**负索引**，表示「倒数第一条轴」（第 82 行 `a + x.ndim` 转正）。`s` 里的 `-1` 是**通配符**，表示「这一轴用原数组的长度」（第 100 行 `x.shape[a] if s == -1`）。两者都写成 `-1`，但语义完全不同，分别在第 82 行和第 100 行被处理。

---

## 5. 综合实践

**综合任务**：把本讲四个模块串起来，写一个**最小可用的「伪 c2c」**——它不真正调用 C 扩展做 FFT，而是完整复刻 `c2c` 内核里「进入 C 之前」的全部预处理步骤，并在最后一步用一个占位函数代替 `pfft.c2c`。目标是让你直观看到：一次 `fft(x, n=..., norm=...)` 调用，在抵达 C 之前，输入经历了哪些变换。

**操作步骤**：

```python
import numpy as np
from scipy.fft._duccfft.helper import (
    _asfarray, _datacopied, _normalization, _workers)

def fake_pfft_c2c(tmp, axis_tuple, forward, norm_int, out, workers):
    """占位：不真算 FFT，只汇报 C 层会收到的参数。"""
    print("  -> C 层收到：dtype=%s shape=%s forward=%s norm_int=%d workers=%d out_is=%s"
          % (tmp.dtype, tmp.shape, forward, norm_int, workers,
             "inplace_buf" if out is not None else None))
    return tmp  # 仅占位，返回预处理后的数组

def my_c2c(forward, x, n=None, axis=-1, norm=None, overwrite_x=False,
           workers=None, plan=None):
    """复刻 basic.c2c 的预处理链路，最后用 fake_pfft_c2c 代替真正的 FFT。"""
    if plan is not None:
        raise NotImplementedError("plan not supported")
    # 模块 4.1：浮点化
    tmp = _asfarray(x)
    overwrite_x = overwrite_x or _datacopied(tmp, x)
    # 模块 4.3：norm 字符串 -> 整数
    norm_int = _normalization(norm, forward)
    workers = _workers(workers)
    # 模块 4.2：截断/补零（这里手写 1-D 版，等价于 _fix_shape_1d）
    if n is not None:
        if n < 1:
            raise ValueError("invalid n")
        if tmp.shape[axis] >= n:
            idx = [slice(None)] * tmp.ndim
            idx[axis] = slice(0, n)
            tmp = tmp[tuple(idx)]            # 截断：视图
            copied = False
        else:
            s = list(tmp.shape); s[axis] = n
            z = np.zeros(s, tmp.dtype)
            idx = [slice(None)] * tmp.ndim
            idx[axis] = slice(0, tmp.shape[axis])
            z[tuple(idx)] = tmp[tuple(idx)]
            tmp = z; copied = True           # 补零：新数组
        overwrite_x = overwrite_x or copied
    # 决定能否原地写（仅复数路径）
    out = tmp if (overwrite_x and tmp.dtype.kind == 'c') else None
    return fake_pfft_c2c(tmp, (axis,), forward, norm_int, out, workers)

# ---- 三组用例，观察预处理效果 ----
print("用例1：复数信号，n=16(补零)，norm='forward'")
my_c2c(True, np.arange(10) + 1j*np.arange(10), n=16, norm="forward")

print("用例2：整数信号(->float64)，n=8(截断)，默认 norm")
my_c2c(True, np.arange(10), n=8)

print("用例3：逆变换 ifft，norm='ortho'")
my_c2c(False, np.ones(8, dtype=np.complex128), norm="ortho")
```

**需要观察的现象与预期结果**（依据源码推导，供本地核对）：

- 用例 1：输入 `int` 的实/虚部经 `_asfarray` 升 `float64`，但因为是 `int+1j*int`（复数），dtype 应为 `complex128`；`n=16>10` 触发补零 → `shape=(16,)`、`copied=True`、`out=inplace_buf`；`norm='forward'`、`forward=True` → `norm_int=2`。
- 用例 2：纯整数输入升 `float64`（实数，`kind='f'`），故 `out=None`（实数路径不原地写）；`n=8<10` 触发截断 → `shape=(8,)`、`copied=False`；默认 `norm` → `norm_int=0`。
- 用例 3：已是 `complex128`，无 `n` 不改形状 → `shape=(8,)`；`forward=False`、`norm='ortho'` → `norm_int=2-1=1`。

**延伸思考**：把 `fake_pfft_c2c` 换成真正的 `scipy.fft._duccfft.pyduccfft.c2c`，你的 `my_c2c` 就能算出和 `scipy.fft.fft` 完全一致的结果——这正说明「预处理 + C 内核」是可分离的两段职责。

## 6. 本讲小结

- `_asfarray` 是进入 C 内核的「安检口」：把任意输入升级为**浮点/复数 + 原生字节序 + 对齐**的数组，`float16→float32`、整数/布尔→`float64`，已合格输入尽量返回视图避免拷贝。
- `_datacopied` 严格判断「预处理是否产生了与原数组无关的新内存」，其结果被「或」进 `overwrite_x`，决定后续能否原地写入输出。
- `_fix_shape` 用**纯切片**同时实现截断（够长→取前 n 个→视图零拷贝）与补零（不够长→`np.zeros` 新建并拷入），返回 `(数组, 是否拷贝)`；`_fix_shape_1d` 是其 1-D 封装并补 `n<1` 校验。
- `_NORM_MAP` 把 `norm` 字符串映射成基础整数 `0/1/2`，`_normalization` 再用 `2 - inorm` 翻转方向，保证任意 norm 模式下正逆配对的缩放乘积恒为 \(1/n\)，从而 `ifft(fft(x))==x`。
- `_init_nd_shape_and_axes` 把灵活的 `(s, axes)` 标准化为 `(非负轴列表, 目标长度列表)`，处理「默认最后几轴、负轴转正、`s` 中 `-1` 通配」三大便利，只做解析校验不碰数据；N-D 路径是它先解析、`_fix_shape` 再改形状。
- 四个工具在内核里**按固定顺序**串联：`_asfarray → _datacopied → _normalization → _workers → _fix_shape(_1d) / _init_nd_shape_and_axes+_fix_shape`，最终把一份「干净」的输入交给 C 扩展 `pyduccfft`。

## 7. 下一步学习建议

本讲把「进入 C 之前的预处理」讲透了。接下来建议：

- **[u5-l3 并行 workers：set_workers / get_workers / threading.local](./u5-l3-workers-multithreading.md)**：补齐本讲一笔带过的 `_workers`——它如何把负数/零回绕成合法线程数，以及 `set_workers` 如何用 `threading.local` 实现线程级隔离的默认 worker 数。这是 `_duccfft/helper.py` 里本讲未展开的另一半。
- **回顾 [u5-l1 ducc 内核](./u5-l1-ducc-basic-kernels.md)**：现在再看 `c2c`/`c2cn` 的函数体，应能逐行说出每一步调用了本讲的哪个工具、为何这么排。
- **前瞻 [u6-l1 _execute_1D/_execute_nD](./u6-l1-execute-numpy-xp-dispatch.md)**：后端层在调用本讲的 `_duccfft` 之前，还有一道「numpy 直连 vs `xp.fft` vs 转 numpy 回退」的三分支路由；届时可对比「后端层预处理」与「核心层预处理（本讲）」的分工边界。
- **源码延伸阅读**：[_duccfft/helper.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) 中本讲未涉及的部分（`set_workers`/`get_workers`/`_workers` 的完整校验逻辑），以及 [_duccfft/pyduccfft.cxx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/pyduccfft.cxx) 中 `m.def("c2c", ...)` 如何把 Python 侧的 `forward/norm/workers` 翻译成 C 层的 `forward/inorm/nthreads`。
