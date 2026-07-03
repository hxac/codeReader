# 任意映射 geometric_transform 与 shift/zoom/rotate

## 1. 本讲目标

本讲是「插值与几何变换」单元（u3）的收尾篇，承接 u3-l3 讲过的 `affine_transform`。学完后你应该能够：

- 说清 `geometric_transform` 的「任意映射回调」契约：`mapping` 把**输出坐标**变换为**输入坐标**，函数在该处做样条插值采样。
- 区分两种回调形态：纯 Python 可调用对象，以及用 `scipy.LowLevelCallable` 包装的 C 回调；并理解 `extra_arguments` / `extra_keywords` 如何被注入回调。
- 看懂 `shift`、`zoom`、`rotate` 三个便捷函数如何把人话（「平移多少」「放大几倍」「旋转几度」）翻译成底层 `zoom_shift` / `affine_transform` 已经能直接消化的 `matrix` / `offset`。
- 动手验证「平移 = 单位阵仿射」「缩放 = 对角阵仿射 + 重采样」「旋转 = 旋转阵仿射」这三条等价关系。

## 2. 前置知识

本讲默认你已掌握 u3-l1（样条预滤波）、u3-l2（`map_coordinates` 的坐标表约定）、u3-l3（`affine_transform` 的 pull 模型）。这里只复述三条最关键的直觉。

**(1) pull（反向）重采样。** ndimage 的所有几何函数都按「拉」模型工作：对每一个**输出**像素 `o`，先算出它对应的**输入**坐标，再到输入数组里插值取出值。即

\[ \text{output}[o] = \text{input}\big[\,\text{mapping}(o)\,\big] \]

`mapping` 是「输出 → 输入」的映射。如果你手上的矩阵是「输入 → 输出」的 push（正向）变换，要先求逆再用（见 u3-l3）。

**(2) 两条 C 内核路径。** `_interpolation.py` 里所有几何函数最终只走两个 C 内核：

- `_nd_image.zoom_shift`：可分离（对角）路径，公式 `cc = zoom * (o + shift)`，对每个轴独立缩放/平移，最快。
- `_nd_image.geometric_transform`：通用路径，既能吃任意 `mapping` 回调，也能吃完整矩阵 `matrix` + `offset`，做 `cc = matrix @ o + offset`。

`affine_transform`（u3-l3）就是按 `matrix` 形状在这两条路径之间二选一。本讲的四个函数全部复用这两条路径，没有任何新的 C 内核。

**(3) 预滤波装配块。** 凡是 `order > 1` 且 `prefilter=True`（默认）的函数，都会先执行同一段「预填充 → `spline_filter` → C 重采样」流程（见 u3-l1/u3-l2）。本讲四个函数都有这一段，后文不再逐字重复，只标注行号。

**(4) LowLevelCallable 是什么。** `scipy.LowLevelCallable` 是 SciPy 提供的一个包装器，把一个 C 函数指针（通常以 `PyCapsule` 形式给出）交给底层 C 代码直接调用，避免「每个像素都跨一次 Python/C 边界」。它适合在 `geometric_transform` / `generic_filter` 这类「逐元素回调」场景里提速。本讲会讲清它在 `geometric_transform` 里的精确 C 签名。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_interpolation.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py) | 本讲全部 Python 实现：`geometric_transform`、`shift`、`zoom`、`rotate` |
| [src/nd_image.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c) | C 包装函数 `Py_GeometricTransform` / `Py_ZoomShift`，以及把 Python 回调桥接成 C 回调的 `Py_Map` |
| [tests/test_c_api.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_c_api.py) | `geometric_transform` 的 Python 回调、Cython 回调、`LowLevelCallable` 回调四者等价性测试，是理解回调契约的最佳示例 |

## 4. 核心概念与源码讲解

### 4.1 geometric_transform：任意映射回调

#### 4.1.1 概念说明

`geometric_transform` 是 ndimage 里**最通用**的几何变换：它不假定映射是线性的。你只要给它一个 `mapping` 回调，告诉它「输出坐标 `o` 应该去输入的哪个坐标取值」，它就用样条插值把值取回来填到输出里。

这和 `map_coordinates`（u3-l2）的区别在于坐标的来源：

- `map_coordinates`：调用者**一次性**给出整张坐标表（形状 `(ndim, *output_shape)`），C 内核直接查表。
- `geometric_transform`：调用者只给一个**函数**，C 内核遍历每个输出像素时**逐个调用**该函数算坐标。

事实上两者共用同一个 C 内核 `NI_GeometricTransform`：`map_coordinates` 把 `mapping` 传 `None`、把坐标表传进去；`geometric_transform` 把 `mapping` 传进去、坐标表传 `None`。这点在 u3-l2 已经点过，本讲聚焦 `mapping` 不为 `None` 的那一半能力。

#### 4.1.2 核心流程

`geometric_transform` 的执行流程：

1. 校验 `order` 范围、确定 `output_shape`（默认等于 `input.shape`）。
2. 复数输入拆实部/虚部递归（标准套路）。
3. 预滤波装配块：`order > 1` 且 `prefilter=True` 时，先 `_prepad_for_spline_filter` 再 `spline_filter`。
4. 把 `mode` 编码成整数码，交给 C 内核 `_nd_image.geometric_transform`，传入 `mapping`、`extra_arguments`、`extra_keywords`。
5. C 内核遍历每个输出像素，调用 `mapping` 得到输入坐标，插值采样。

回调契约（Python 形态）：

```python
def mapping(output_coords, *extra_arguments, **extra_keywords):
    # output_coords: 长度 = 输出维度的 int 元组
    # 返回: 长度 = 输入维度的 float 元组（输入坐标）
    return (input_coord_axis0, input_coord_axis1, ...)
```

注意：`output_coords` 是**整数**像素索引（C 端用 `PyLong_FromSsize_t` 构造），但返回的输入坐标是**浮点**（亚像素采样，由样条插值处理）。

#### 4.1.3 源码精读

**Python 函数签名与文档里的 LowLevelCallable C 签名** [_interpolation.py:L229-L232](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L229-L232)：

```python
def geometric_transform(input, mapping, output_shape=None,
                        output=None, order=3,
                        mode='constant', cval=0.0, prefilter=True,
                        extra_arguments=(), extra_keywords=None):
```

文档明确给出两种被接受的 C 回调签名 [_interpolation.py:L277-L282](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L277-L282)：

```c
int mapping(npy_intp *output_coordinates, double *input_coordinates,
            int output_rank, int input_rank, void *user_data)
int mapping(intptr_t *output_coordinates, double *input_coordinates,
            int output_rank, int input_rank, void *user_data)
```

参数方向与 Python 版完全一致：`output_coordinates` 进、`input_coordinates` 出；`output_rank` / `input_rank` 给出两个长度；`user_data` 是构造 `LowLevelCallable` 时附带的数据指针。返回值是错误状态——**1 表示成功，0 表示出错**（与通常 C 习惯相反，文档有强调）。

**Python 端的装配块与 C 调用** [_interpolation.py:L336-L371](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L336-L371)：

```python
if extra_keywords is None:
    extra_keywords = {}
...
if prefilter and order > 1:
    padded, npad = _prepad_for_spline_filter(input, mode, cval)
    filtered = spline_filter(padded, order, output=np.float64, mode=mode)
else:
    npad = 0
    filtered = input
mode = _ni_support._extend_mode_to_code(mode)
_nd_image.geometric_transform(filtered, mapping, None, None, None, output,
                              order, mode, cval, npad, extra_arguments,
                              extra_keywords)
```

注意 `_nd_image.geometric_transform` 的第 3/4/5 个位置参数（`coordinates` / `matrix` / `shift`）全是 `None`——这正是「走 mapping 回调路径」的信号；`map_coordinates` 把第 3 个填坐标表，`affine_transform` 把第 4/5 个填 `matrix`/`offset`，三者共用同一 C 内核、靠这几个 `None` 互相区分。

**C 包装函数 `Py_GeometricTransform`** [src/nd_image.c:L693-L785](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L693-L785)。它先解析 13 个参数（注意 `fnc` 是 mapping，可能是 Python 对象、`PyCapsule` 或 `LowLevelCallable`）[src/nd_image.c:L724-L732](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L724-L732)，然后根据 `fnc` 类型分派：

- 若是裸 `PyCapsule`（旧式 LowLevelCallable）：直接取函数指针 [src/nd_image.c:L746-L748](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L746-L748)。
- 否则用 `ccallback_prepare` 在 `callback_signatures` 表里匹配签名 [src/nd_image.c:L703-L719](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L703-L719)：匹配到 C 函数就直接用；匹配到 Python 函数就把 `extra_arguments`/`extra_keywords` 装进 `cbdata`，并把桥接函数设成 `Py_Map` [src/nd_image.c:L757-L767](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L757-L767)。

**`callback_signatures` 表**就是文档里那两条签名的 C 端落地，外加若干「按平台 `npy_intp` 实际宽度」的等价签名（`short`/`int`/`long`/`long long`）[src/nd_image.c:L703-L719](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L703-L719)。这就是 `LowLevelCallable` 能接受多种整型指针的原因。

**Python 回调桥接函数 `Py_Map`** [src/nd_image.c:L654-L690](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L654-L690)。这是理解 `extra_arguments` 机制的关键：

```c
coors = PyTuple_New(orank);
for(ii = 0; ii < orank; ii++)
    PyTuple_SetItem(coors, ii, PyLong_FromSsize_t(ocoor[ii]));   // 输出坐标元组
tmp = Py_BuildValue("(O)", coors);
args = PySequence_Concat(tmp, cbdata->extra_arguments);          // (out_coords,) + extra
rets = PyObject_Call(callback->py_function, args, cbdata->extra_keywords);  // 调 mapping
for(ii = 0; ii < irank; ii++)
    icoor[ii] = PyFloat_AsDouble(PyTuple_GetItem(rets, ii));     // 读回输入坐标
...
return PyErr_Occurred() ? 0 : 1;                                 // 1=成功, 0=出错
```

可见 C 端对每个输出像素都构造 `args = (output_coords_tuple, *extra_arguments)`，再 `mapping(*args, **extra_keywords)` 调用。这正好解释了 `test_c_api.py` 里的写法。

**测试示例：四种回调等价** [tests/test_c_api.py:L87-L102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_c_api.py#L87-L102)：

```python
def transform(output_coordinates, shift):
    return output_coordinates[0] - shift, output_coordinates[1] - shift
...
res = ndimage.geometric_transform(im, func(shift))                       # 闭包写法
std = ndimage.geometric_transform(im, transform, extra_arguments=(shift,))  # extra_arguments 写法
```

`func(shift)` 是「闭包捕获 shift」，`transform` 配 `extra_arguments=(shift,)` 是「运行时注入 shift」，两者结果应完全一致——`extra_arguments` 本质就是「不想写闭包时的参数注入通道」。

#### 4.1.4 代码实践

**实践目标**：用 `geometric_transform` + Python 回调实现「极坐标展开」——把笛卡尔图像里的圆环展开成一条水平直线。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

n = 64
y, x = np.mgrid[0:n, 0:n]
cy, cx = (n - 1) / 2, (n - 1) / 2
r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
img = (np.abs(r - 18) < 2).astype(float)   # 半径 18 的亮圆环

H, W = 40, 120            # 输出：rho 方向 40 行，theta 方向 120 列
rho_max = 30.0

def polar_map(out_coords):
    rho_idx, theta_idx = out_coords                 # 输出坐标（整数）
    rr = rho_max * rho_idx / (H - 1)
    ang = 2 * np.pi * theta_idx / (W - 1)
    iy = cy + rr * np.sin(ang)                      # 输入坐标（浮点，亚像素）
    ix = cx + rr * np.cos(ang)
    return (iy, ix)

polar = ndimage.geometric_transform(
    img, polar_map, output_shape=(H, W), order=1, mode='constant')
```

**需要观察的现象**：输出 `polar` 是 40×120 的数组。因为输入里的圆环在固定半径 `r≈18` 上，对应 `rho_idx ≈ 18/30*(H-1)` 那一行；又因为圆环在所有角度上都亮，所以那一行会是一整条水平的亮带（值≈1），其余行接近 0。这就是「把环拉直」。

**预期结果**：`polar` 形状为 `(40, 120)`；约第 23 行（`18/30*39≈23.4`）出现一条贯穿全宽的亮带。

**待本地验证**：亮带的精确行号与亮度取决于 `np.mgrid` 中心约定与 `mode`，请在本地运行确认；如想用 `extra_arguments` 改写，把 `rho_max` 通过 `extra_arguments=(rho_max,)` 注入即可，结果应与闭包版一致（对照 `test_c_api.py` 的等价性断言）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `mapping` 返回的元组长度不等于输入维度，会发生什么？
**答案**：`Py_Map` 里 `for(ii = 0; ii < irank; ii++) icoor[ii] = PyFloat_AsDouble(PyTuple_GetItem(rets, ii))` 会越界取 `rets`，触发 `IndexError`（Python 端）→ `PyErr_Occurred()` 为真 → `Py_Map` 返回 0 → C 内核中止并抛错。所以返回长度必须等于输入维度。

**练习 2**：为什么 C 回调的返回值约定是「1 成功、0 出错」，和很多 C 函数「0 成功」相反？
**答案**：因为底层遍历循环用返回值当「是否继续」标志，1 表示「本次映射成功、可继续下一个像素」，0 表示「出错了、立即停止」。文档明确写了这条约定，写 C 回调时务必遵守，否则正常的映射会被当成错误中止。

---

### 4.2 shift：平移作为对角仿射的特例

#### 4.2.1 概念说明

`shift(input, s)` 把数组沿各轴平移 `s`。在 pull 模型里，「输出 `o` 取输入 `o - s` 的值」即实现了内容整体平移 `s`：

\[ \text{output}[o] = \text{input}[o - s] \]

这正是 `zoom_shift` 内核公式 `cc = zoom * (o + shift)` 在 `zoom = 1` 时的情形，只要让内核参数 `shift = -s`。所以 `shift` 不走通用 `geometric_transform` 内核，而是直接调可分离的 `zoom_shift`——这是它能很快的原因。

#### 4.2.2 核心流程

1. 校验 `order`、`input.ndim >= 1`、准备 `output`。
2. 复数拆分。
3. 预滤波装配块。
4. `_normalize_sequence(shift, ndim)` 把标量广播成各轴序列。
5. **取负**：`shift = [-ii for ii in shift]`（pull 模型要求）。
6. 调 `_nd_image.zoom_shift(filtered, None, shift, ...)`，`zoom` 传 `None`（C 端视作 1）。

#### 4.2.3 源码精读

**取负与内核调用** [_interpolation.py:L753-L759](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L753-L759)：

```python
shift = _ni_support._normalize_sequence(shift, input.ndim)
shift = [-ii for ii in shift]            # 关键：取负
shift = np.asarray(shift, dtype=np.float64)
if not shift.flags.contiguous:
    shift = shift.copy()
_nd_image.zoom_shift(filtered, None, shift, output, order, mode, cval,
                     npad, False)
```

`zoom=None` 告诉 `zoom_shift`「缩放因子为 1」；第二个位置参数是 shift 数组（已取负）；最后 `False` 是 `grid_mode`（shift 不涉及网格模式，恒为 `False`）。

**与 affine_transform 的等价**：对照 u3-l3 的对角快速路径 [_interpolation.py:L648-L650](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L648-L650)：

```python
if matrix.ndim == 1:
    _nd_image.zoom_shift(filtered, matrix, offset/matrix, output, order,
                         mode, cval, npad, False)
```

令 `matrix = [1, 1, ...]`（单位对角）、`offset = -s`，则 `offset/matrix = -s`，与 `shift` 函数传入的 `-s` 完全一致。于是：

\[ \texttt{shift}(input,\ s) \;\equiv\; \texttt{affine\_transform}(input,\ \text{matrix}=\mathbf{1},\ \text{offset}=-s) \]

这就是「平移 = 单位阵仿射」的精确含义——两者调用的是同一个 `zoom_shift` C 内核、同一组参数。

#### 4.2.4 代码实践

**实践目标**：用 `np.allclose` 数值验证 `shift` 与 `affine_transform` 的等价关系。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

a = np.arange(12.).reshape(4, 3)
s = (0.5, 1.0)

r_shift   = ndimage.shift(a, s, order=1, mode='constant')
r_affine  = ndimage.affine_transform(
                a, matrix=np.ones(2), offset=[-0.5, -1.0],
                order=1, mode='constant')

print(np.allclose(r_shift, r_affine))
```

**需要观察的现象 / 预期结果**：应打印 `True`。

**待本地验证**：因浮点与 `order` 不同可能有极小差异，请本地运行确认；改用 `order=3` 时等价性仍应成立，但要求 `prefilter` 两边一致（默认都为 `True`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `shift` 要把用户传入的 `s` 取负，而 `affine_transform` 的 `offset` 不取负？
**答案**：两者其实表达的是同一件事，只是命名视角不同。pull 模型下 `output[o] = input[o - s]`。`shift` 让用户按「内容往正方向移动 s」的直觉给参数，所以内部补一个负号把 `o - s` 凑出来；`affine_transform` 的公式是 `output[o] = input[matrix·o + offset]`，要表达 `input[o - s]` 就直接令 `offset = -s`，无需再取负。两者最终送进 `zoom_shift` 的 shift 参数完全相同。

**练习 2**：`shift(a, (20, 0))`（文档示例）会把图像内容往哪个方向移动？
**答案**：内容沿第 0 轴（行，即垂直方向）正向移动 20 个像素、第 1 轴不动。因为 `output[o] = input[o - (20,0)]`，原本在第 `i` 行的内容出现在第 `i+20` 行——视觉上是「向下移动 20 像素」。

---

### 4.3 zoom：缩放、重采样网格与 grid_mode

#### 4.3.1 概念说明

`zoom(input, z)` 改变数组的**采样网格分辨率**：输出形状为 `round(input.shape * z)`，并在新网格上重新插值。它和「对角仿射」的关系比 `shift` 多一层微妙：

- `affine_transform(matrix=对角)` 默认把输出画在**和输入同样形状**的网格上，只是把内容拉伸/压缩——不改变像素数量。
- `zoom` 不仅做了「对角缩放」这件事，还**换了一张不同分辨率的网格**。

因此严格说 `zoom` 是「对角仿射 + 重采样到新分辨率网格」，比纯对角仿射多了一步「按新形状重算实际缩放因子」。它同样走 `zoom_shift` 内核（最快路径）。

#### 4.3.2 核心流程

1. `_normalize_sequence(zoom, ndim)`，算 `output_shape = round(input.shape * zoom)`。
2. **早退捷径**：若所有 `zoom == 1` 且 `prefilter=True`，直接把输入拷进输出返回（修复 gh-20999）。
3. 复数拆分；预滤波装配块。
4. **重算实际缩放因子**（关键），分 `grid_mode` 两种：
   - `grid_mode=False`（默认）：按「像素中心」对齐，`zoom = (input_shape - 1) / (output_shape - 1)`。
   - `grid_mode=True`：按「像素全程」对齐，`zoom = input_shape / output_shape`。
5. 调 `_nd_image.zoom_shift(filtered, zoom, None, ...)`，`shift=None`，并传入 `grid_mode` 标志。

#### 4.3.3 源码精读

**输出形状与早退** [_interpolation.py:L839-L850](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#LL839-L850)：

```python
zoom = _ni_support._normalize_sequence(zoom, input.ndim)
output_shape = tuple(
        [int(round(ii * jj)) for ii, jj in zip(input.shape, zoom)])
...
if all(z == 1 for z in zoom) and prefilter:  # early exit for gh-20999
    output = xpx.at(output)[...].set(input)
    return output
```

注释解释了为什么早退条件要带上 `prefilter`：`zoom=1` 的语义是「返回原图」，但若用户显式 `prefilter=False`，说明输入**不是**原图（而是已被当成样条系数），仍需走完流程去「抵消」滤波，不能早退。

**重算实际缩放因子** [_interpolation.py:L880-L893](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L880-L893)：

```python
zoom_div = np.array(output_shape)        # 输出形状 M
zoom_nominator = np.array(input.shape)   # 输入形状 N
if not grid_mode:
    zoom_div -= 1                        # M-1
    zoom_nominator -= 1                  # N-1
# zoom 为 0 处用 1 兜底（缩放到无穷大不可预测）
zoom = np.divide(zoom_nominator, zoom_div,
                 out=np.ones_like(input.shape, dtype=np.float64),
                 where=zoom_div != 0)
zoom = np.ascontiguousarray(zoom)
_nd_image.zoom_shift(filtered, zoom, None, output, order, mode, cval, npad,
                     grid_mode)
```

为什么默认用 `(N-1)/(M-1)` 而不是 `N/M`？因为 `grid_mode=False` 把数组看作「像素中心」的集合：长 `N` 的信号有 `N` 个中心，从位置 0 排到位置 `N-1`，跨度是 `N-1`。要让输出第 0 个中心对齐输入第 0 个中心、输出最后一个中心对齐输入最后一个中心，缩放比就是跨度之比 `(N-1)/(M-1)`。`grid_mode=True` 则把每个像素看作占据单位宽度的格子，整段长度就是 `N`，缩放比取 `N/M`。文档用一张像素条示意图说明了这两种「长度」定义 [_interpolation.py:L784-L799](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L784-L799)。

**grid_mode 与 mode 的联动告警** [_interpolation.py:L865-L877](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L865-L877)：当 `grid_mode=True` 时，若 `mode` 还是默认的 `'constant'`/`'wrap'`，会建议改用 `'grid-constant'`/`'grid-wrap'`，因为在「像素全程」语义下后者的边界行为更符合直觉（这点呼应 u1-l4 讲过的 `grid-*` 与普通 mode 的差异）。

#### 4.3.4 代码实践

**实践目标**：观察 `grid_mode` 对放大后边界对齐方式的影响。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

a = np.array([0., 0., 0., 1., 0., 0., 0.])   # 中间一个尖峰
g_false = ndimage.zoom(a, 3, order=1, grid_mode=False)
g_true  = ndimage.zoom(a, 3, order=1, grid_mode=True)
print("default  :", np.round(g_false, 3))
print("grid_mode:", np.round(g_true, 3))
```

**需要观察的现象 / 预期结果**：两种模式下尖峰都被插值放大；但因为端点对齐方式不同（默认对齐「中心」、`grid_mode=True` 对齐「全程」），放大后峰值的**绝对位置**和**边界附近的值**会有差异。

**待本地验证**：具体数值请本地运行确认。可顺便把 `mode` 设为 `'grid-constant'` 配合 `grid_mode=True`，观察告警是否消失。

#### 4.3.5 小练习与答案

**练习 1**：`zoom(a, 2)`（`a` 长 5）的 `output_shape` 是多少？默认 `grid_mode=False` 下送进内核的实际 `zoom` 因子是多少？
**答案**：`output_shape = round(5 * 2) = 10`；实际因子 `(5-1)/(10-1) = 4/9 ≈ 0.444`。注意它**不是** 0.5——因为端点要对齐。若 `grid_mode=True`，因子才是 `5/10 = 0.5`。

**练习 2**：为什么 `zoom=1` 时函数要专门做一个「早退拷贝」，而不是让 `zoom_shift` 内核自己处理？
**答案**：`zoom_shift` 用 `(N-1)/(M-1)` 算因子，当 `M=N` 时确实是 1，理论上也能得到原图。但早退有两个好处：一是避免任何浮点/滤波误差，保证 `zoom=1` **精确**返回原图（这是用户最常用来「无操作」的写法，必须无损）；二是跳过预滤波等开销。注释标注的 gh-20999 就是这类「无损往返」需求。

---

### 4.4 rotate：绕平面旋转与对 affine_transform 的复用

#### 4.4.1 概念说明

`rotate(input, angle, axes=(1,0))` 把数组在 `axes` 指定的两个轴构成的平面内旋转 `angle` 度。它和 `shift`/`zoom` 不同：旋转矩阵一般不是对角的，所以**不能**用 `zoom_shift` 快速路径。`rotate` 的做法是——构造好旋转矩阵和居中 offset，然后**直接调用 `affine_transform`**（完全复用，不重写）。

这正是它和前两个函数在实现策略上的根本差别：`shift`/`zoom` 自己调 `zoom_shift` 内核，`rotate` 调 `affine_transform` 这个 Python 函数。

#### 4.4.2 核心流程

1. 校验 `ndim >= 2`、`axes` 恰有两个整数、规范化负轴、排序。
2. 用 `special.cosdg(angle)` / `special.sindg(angle)`（度数版三角，比先转弧度更准）算 `c, s`，构造 2×2 旋转矩阵 `rot_matrix = [[c, s], [-s, c]]`。
3. 算输出平面形状：`reshape=True` 时按旋转后的角点外接框算 `out_plane_shape`；`reshape=False` 时保持原形状。
4. 算居中 offset：`offset = in_center - rot_matrix @ out_center`。
5. 构造 `output_shape`（旋转平面外的轴长度不变）。
6. **分两种维度**：
   - `ndim <= 2`：一次性 `affine_transform(input, rot_matrix, offset, output_shape, ...)`。
   - `ndim > 2`：对所有「平行于旋转平面」的 2D 切片逐个调 `affine_transform`。

#### 4.4.3 源码精读

**旋转矩阵与居中 offset** [_interpolation.py:L986-L1005](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L986-L1005)：

```python
c, s = special.cosdg(angle), special.sindg(angle)
rot_matrix = np.array([[c, s],
                       [-s, c]])
img_shape = np.asarray(input_arr.shape)
in_plane_shape = img_shape[axes]
if reshape:
    iy, ix = in_plane_shape
    out_bounds = rot_matrix @ [[0, 0, iy, iy],
                               [0, ix, 0, ix]]
    out_plane_shape = (np.ptp(out_bounds, axis=1) + 0.5).astype(int)
else:
    out_plane_shape = img_shape[axes]

out_center = rot_matrix @ ((out_plane_shape - 1) / 2)
in_center = (in_plane_shape - 1) / 2
offset = in_center - out_center
```

居中公式 `offset = in_center - R @ out_center` 的来历：pull 模型 `output[o] = input[R·o + offset]`，希望输出**中心** `out_center` 映射到输入**中心** `in_center`，即 `R·out_center + offset = in_center`，解出 `offset = in_center - R·out_center`。

**`reshape=True` 的输出形状**：把输入平面的四个角点用 `rot_matrix` 变换，取变换后坐标在两个轴上的极差（`np.ptp`）作为输出平面的边长 [_interpolation.py:L993-L999](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L993-L999)。这样旋转后的整幅图像都能塞进输出画布，不会裁切（文档示例里 512×512 转 45° 得到 724×724 即由此而来）。

**复用 affine_transform 的分维度分派** [_interpolation.py:L1015-L1031](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L1015-L1031)：

```python
if ndim <= 2:
    affine_transform(input_arr, rot_matrix, offset, output_shape, output,
                     order, mode, cval, prefilter)
else:
    # ndim > 2 时，对所有平行于 axes 的平面逐一旋转
    planes_coord = itertools.product(
        *[[slice(None)] if ax in axes else range(img_shape[ax])
          for ax in range(ndim)])
    out_plane_shape = tuple(out_plane_shape)
    for coordinates in planes_coord:
        ia = input_arr[coordinates]
        oa = output[coordinates]
        affine_transform(ia, rot_matrix, offset, out_plane_shape,
                         oa, order, mode, cval, prefilter)
```

`itertools.product` 生成所有「旋转轴用 `slice(None)`、其余轴用具体索引」的组合，即所有需要单独旋转的 2D 切片。对体数据（如 3D）这等价于「逐层旋转每一张切面图」。

注意：因为 `rot_matrix` 是 2×2（`ndim==2` 时），`affine_transform` 内部会走 **`geometric_transform` C 内核的 matrix 分支**（u3-l3 的 `else` 分支 [_interpolation.py:L651-L654](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L651-L654)），即通用矩阵路径，而非可分离的 `zoom_shift`。

#### 4.4.4 代码实践

**实践目标**：确认 `rotate` 就是「构造旋转矩阵 + 居中 offset + 调 `affine_transform`」，并理解 `reshape` 的作用。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import ndimage

img = np.zeros((6, 6))
img[2:4, 1:5] = 1.0                      # 一条水平亮条

r_false = ndimage.rotate(img, 30, reshape=False, order=1)
r_true  = ndimage.rotate(img, 30, reshape=True,  order=1)
print("reshape=False shape:", r_false.shape)   # 仍是 (6,6)
print("reshape=True  shape:", r_true.shape)    # 放大以容纳旋转
```

**需要观察的现象 / 预期结果**：`reshape=False` 时输出仍是 6×6，旋转后亮条的四角会被裁掉；`reshape=True` 时输出形状变大（约 8×8），整条亮条完整保留。

**待本地验证**：精确形状与像素值请本地运行确认。可进一步：用 `special.cosdg`/`special.sindg` 自行构造与 `rotate` 相同的 `rot_matrix` 和 `offset`，直接调 `affine_transform(img, rot_matrix, offset, output_shape, order=1)`，对比应与 `ndimage.rotate` 逐元素一致（这正是「rotate 是 affine_transform 的特例」的实证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rotate` 用 `special.cosdg/sindg` 而不是 `np.cos(np.deg2rad(angle))`？
**答案**：`cosdg`/`sindg` 直接以度为输入计算，对 30°、45°、90° 这类常见角度能给出更接近真值的结果，减少「度→弧度」转换引入的浮点误差。对于旋转这种对角度精度敏感的操作，这点精度很值得。

**练习 2**：`rotate` 在 3D 体数据上对 `axes=(1,0)` 旋转 30°，实际发生了什么？
**答案**：它不会调用任何「3D 旋转内核」，而是用 `itertools.product` 枚举所有「平行于 (0,1) 平面」的 2D 切片（即第 2 轴的每一层），对每一层 2D 切片分别调 `affine_transform` 做 30° 平面旋转，结果写回输出的对应层。等价于「逐层旋转每一张切面图」。

---

## 5. 综合实践

把本讲四条主线串起来：**用 `geometric_transform` 手写一个旋转，再和库函数 `rotate` 比对，最后用 `zoom` 把结果放大。**

```python
# 示例代码
import numpy as np
from scipy import ndimage
from scipy import special

img = np.zeros((10, 10))
img[4:6, 1:9] = 1.0                       # 水平亮条
H, W = img.shape
cy, cx = (H - 1) / 2, (W - 1) / 2
angle = 30
c, s = special.cosdg(angle), special.sindg(angle)

# 1) 用 geometric_transform 手写 pull 旋转（reshape=False，输出与输入同形）
#    仿照 rotate：output[o] = input[R·(o - out_center) + in_center]
def rot_map(out_coords):
    i, j = out_coords
    di, dj = i - cy, j - cx               # 相对输出中心
    ii = cy + c * di + s * dj             # R @ (di, dj) 再加输入中心
    jj = cx - s * di + c * dj
    return (ii, jj)

mine = ndimage.geometric_transform(
    img, rot_map, output_shape=(H, W), order=1, mode='constant')

# 2) 对照库函数
ref = ndimage.rotate(img, angle, reshape=False, order=1, mode='constant')
print("matches rotate:", np.allclose(mine, ref, atol=1e-6))

# 3) 用 zoom 把手写旋转结果放大 1.5 倍
big = ndimage.zoom(mine, 1.5, order=1, mode='constant')
print("zoomed shape:", big.shape)
```

**串联要点**：

- 第 1 步验证 `geometric_transform` 的 mapping 契约——返回的 `(ii, jj)` 是**输入**坐标，方向是「输出→输入」。
- 第 2 步把任意映射（`geometric_transform`）和矩阵映射（`rotate`→`affine_transform`）放在同一问题上对比，确认两者在数学等价时结果一致。
- 第 3 步展示「旋转后再缩放」的常见组合，且 `zoom` 走的是另一条 `zoom_shift` 内核。

**待本地验证**：`np.allclose` 是否为 `True` 取决于 `rotate` 内部 offset 的精确舍入与 `mode` 边界行为，请在本地确认；若有微小差异，通常集中在边界一两个像素（`mode='constant'` 下越界处），可用 `atol` 放宽或换 `mode='nearest'` 观察内部区域是否一致。

## 6. 本讲小结

- `geometric_transform` 是最通用的几何变换：`mapping` 把**输出坐标**映射为**输入坐标**，函数在该处样条插值采样；它和 `map_coordinates` 共用同一个 C 内核 `NI_GeometricTransform`，靠 `mapping` 是否为 `None` 区分。
- 回调有两套契约：Python 版 `mapping(out_coords_tuple, *extra_arguments, **extra_keywords)`；C 版（`LowLevelCallable`）签名 `int mapping(out_coor*, in_coor*, out_rank, in_rank, user_data)`，返回 **1=成功、0=出错**。`extra_arguments`/`extra_keywords` 在 C 端由 `Py_Map` 拼接到调用参数里。
- `shift(input, s)` ≡ `affine_transform(input, matrix=1, offset=-s)`：取负是为了在 pull 模型下凑出 `output[o]=input[o-s]`；两者共用 `zoom_shift` 内核。
- `zoom` 是「对角缩放 + 重采样到新分辨率网格」：`output_shape=round(input.shape*zoom)`，并按 `grid_mode` 把实际因子重算成 `(N-1)/(M-1)`（默认，像素中心对齐）或 `N/M`（`grid_mode=True`，像素全程对齐）；`zoom=1` 有无损早退捷径。
- `rotate` 直接复用 `affine_transform`：构造 `rot_matrix=[[c,s],[-s,c]]`、`offset=in_center - R@out_center`；`ndim<=2` 一次调用，`ndim>2` 逐平面切片调用。
- 四个函数没有引入任何新 C 内核，全部落在 `zoom_shift`（可分离）与 `geometric_transform`（通用）两条路径上——`shift`/`zoom`/对角-affine 走前者，`rotate`/全矩阵-affine/任意 mapping 走后者。

## 7. 下一步学习建议

- 向下深挖内核：进入 u6 单元精读 `src/ni_interpolation.c` 中的 `NI_GeometricTransform` 与 `NI_ZoomShift`，看清 C 端如何用样条权重对亚像素坐标采样、如何处理 `matrix` 分支与 `mapping` 回调分支的统一遍历。
- 横向对比回调机制：本讲的 `LowLevelCallable` 回调模式与 u2-l5 的 `generic_filter`/`generic_filter1d` 回调同源，可对照阅读，巩固「逐元素 C 回调」这一通用范式。
- 工程实践：尝试为一个真实需求（如医学图像配准、遥感图像几何校正）用 `affine_transform` + `geometric_transform` 组合实现 pull 重采样管线，体会 push/pull 矩阵求逆、`output_shape` 扩画布、`mode` 边界选择对结果的实际影响。
