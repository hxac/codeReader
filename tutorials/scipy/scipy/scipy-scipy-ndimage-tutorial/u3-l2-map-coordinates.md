# 坐标映射插值 map_coordinates

## 1. 本讲目标

本讲承接 u3-l1（样条预滤波），把「样条系数」真正用起来。`scipy.ndimage.map_coordinates` 是整个插值子包里**最通用、最直接**的一个函数：你显式给出「要在输入的哪些坐标上取值」，它就在这些位置上做样条插值，把结果摆成一个数组还给你。`zoom`、`shift`、`affine_transform`、`geometric_transform` 这些更「友好」的函数，本质上都是在内部自动生成坐标，最后仍走到同一条 C 内核路径。

读完后你应当能够：

- 说清 `coordinates` 数组的形状约定——**第 0 维是各轴坐标，其余维才是目标输出形状**，并能解释为什么 `output_shape = coordinates.shape[1:]`；
- 画出 `map_coordinates` 从参数校验到 C 内核的完整执行流程，并能指出**预滤波装配块**（`prefilter and order > 1`）在其中扮演的角色；
- 讲清 `_get_output` 在本函数里如何**带着 `shape=output_shape`** 被调用，从而让输出形状完全由坐标数组决定、而与输入形状脱钩；
- 区分插值场景下两对容易混淆的边界模式：**`constant` vs `grid-constant`**、**`wrap` vs `grid-wrap`**，并知道为什么它们在 `_extend_mode_to_code` 里被映射成**不同的**整数码。

本讲只读一个 Python 文件 `_interpolation.py`（以及一处 `_ni_support.py`），并向下指明它最终调用的 C 内核 `_nd_image.geometric_transform`。

## 2. 前置知识

本讲默认你已经掌握 u1-l4（共享支撑工具）与 u3-l1（样条预滤波）的内容。这里快速回顾两个关键事实：

- **`_ni_support._extend_mode_to_code(mode, is_filter=False)`**：把边界模式字符串翻译成 C 内核需要的整数码。本讲要特别强调：在插值函数里它**不带** `is_filter`（用默认值 `False`），所以 `grid-constant` 与 `constant`、`grid-wrap` 与 `wrap` 会得到**不同**的码——这一点和滤波函数（u2 系列）正好相反，是本讲 4.4 节的核心。
- **样条系数 ≠ 样本值**：当 `order >= 2` 时，必须先把样本反解成样条系数（即「预滤波」），插值曲线才会经过样本点。`map_coordinates` 的 `prefilter` 参数（默认 `True`）内部就调用了 `spline_filter`。

下面补一个本讲要用到、但前面没专门讲的小概念。

### 2.1 「拉」（pull / backward）重采样

`map_coordinates` 用的是 **拉重采样**：我们遍历的是**输出**的每个位置，对每个输出位置去**输入**里「拉」一个值回来。具体到这里，输出位置到输入坐标的映射不是由函数内部计算，而是**由你给的 `coordinates` 数组直接指定**。换句话说，你提供的 `coordinates[..., i, j]` 就是「输出像素 (i,j) 应该去输入的哪个坐标取值」。这种「输出坐标 → 输入坐标」的反向模型，是后面 `affine_transform`（u3-l3）讲「pull resampling」时还会再遇到的同一套思想。

### 2.2 输出形状从哪里来

和滤波函数不同，`map_coordinates` 没有 `output_shape` 形参。它的输出形状**完全由 `coordinates` 数组决定**：丢掉 `coordinates` 的第 0 维，剩下的就是输出形状。这个「形状从坐标推导」的设计是本讲最容易踩坑的地方，4.3 节会结合 `_get_output` 讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_interpolation.py](_interpolation.py) | 本讲主角 `map_coordinates` 在此；预滤波装配块、`_prepad_for_spline_filter` 也都在这个文件里 |
| [_ni_support.py](_ni_support.py) | `_get_output(output, input, shape=...)` 与 `_extend_mode_to_code(mode)` 两个共享工具在此 |
| [_ni_docstrings.py](_ni_docstrings.py) | `mode_interp_constant` 文档串定义了 8 种插值模式的确切边界语义，是 4.4 节的权威出处 |
| C 内核 `_nd_image`（编译自 `src/ni_interpolation.c`） | `map_coordinates` 最后调用 `_nd_image.geometric_transform(...)`，在 C 端做真正的样条采样。本讲只到 Python↔C 边界，C 内部细节留到 u6-l3 |

## 4. 核心概念与源码讲解

### 4.1 map_coordinates：坐标驱动的前向查找

#### 4.1.1 概念说明

`map_coordinates` 解决的问题是：**给定输入数组和一个坐标表，在坐标表指定的每个位置上对输入做样条插值，把结果排成数组返回。**

它和「滤波」的最大区别在于「邻域」不再是固定的滑窗，而是**任意稀疏的坐标点**。这些坐标可以是亚像素的（如 `0.5`、`2.7`），也可以落在输入数组范围之外（此时由 `mode` 决定怎么取值）。

坐标数组的形状约定是本函数一切行为的出发点：

- `coordinates.shape[0]` 必须等于输入的维数 `input.ndim`（每一行是一个轴的坐标）；
- `coordinates.shape[1:]` 就是输出形状。

例如输入是 2D（`ndim=2`），坐标数组形状 `(2, 5, 7)`，则输出形状是 `(5, 7)`；`coordinates[0, i, j]` 是输出像素 `(i,j)` 在输入第 0 轴的坐标，`coordinates[1, i, j]` 是第 1 轴的坐标。

#### 4.1.2 核心流程

`map_coordinates` 的执行流程可以分成五步：

1. **校验 `order`**：`order` 必须在 0–5 之间，否则报错。
2. **规范化输入与坐标**：`np.asarray`；并禁止坐标为复数。
3. **推导输出形状并校验维度**：`output_shape = coordinates.shape[1:]`；要求输入和输出都至少 1 维；要求 `coordinates.shape[0] == input.ndim`。
4. **准备输出数组**：调用 `_get_output(output, input, shape=output_shape, ...)`。
5. **预滤波（若需要）→ 编码 mode → 调 C 内核**：若 `prefilter and order > 1`，先做 `_prepad_for_spline_filter` + `spline_filter`；然后把 `mode` 编成整数码，调用 `_nd_image.geometric_transform(...)`，其中 `mapping=None`、坐标数组作为第三个实参传入。

用伪代码概括：

```text
def map_coordinates(input, coordinates, output=None, order=3,
                    mode='constant', cval=0.0, prefilter=True):
    assert 0 <= order <= 5
    coordinates = asarray(coordinates)            # 不允许复数坐标
    output_shape = coordinates.shape[1:]          # ← 输出形状由坐标决定
    assert coordinates.shape[0] == input.ndim
    output = _get_output(output, input, shape=output_shape)
    if prefilter and order > 1:
        padded, npad = _prepad_for_spline_filter(input, mode, cval)
        filtered = spline_filter(padded, order, mode=mode)   # 样本 → 系数
    else:
        npad, filtered = 0, input
    mode_code = _extend_mode_to_code(mode)        # 插值场景：不传 is_filter
    _nd_image.geometric_transform(filtered, None, coordinates, None, None,
                                  output, order, mode_code, cval, npad, None, None)
    return output
```

注意最后一步：`map_coordinates` **没有自己专属的 C 内核**，它复用了 `_nd_image.geometric_transform`——只要把 `mapping` 传成 `None`、把坐标数组传到第三个位置，C 端就会「直接用你给的坐标表」而不是「调用 mapping 回调」去查找。这正是 `map_coordinates` 与 `geometric_transform` 同源的根本原因。

#### 4.1.3 源码精读

先看函数签名与文档（`mode` 默认 `'constant'`、`prefilter` 默认 `True`、`order` 默认 3）：

[_interpolation.py:L374-L376](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L374-L376) —— `@docfiller` 装饰的 `map_coordinates` 函数签名，`output=None` 表示默认会新建输出数组。

参数校验与坐标规范化的核心段：

[_interpolation.py:L446-L456](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L446-L456) —— 这 11 行做了三件事：① 校验 `order ∈ [0,5]`；② `np.asarray` 输入与坐标，并**禁止复数坐标**（`if np.iscomplexobj(coordinates): raise TypeError`）；③ 推导 `output_shape = coordinates.shape[1:]`，并断言输入、输出都至少 1 维、且 `coordinates.shape[0] == input.ndim`。其中 L452 那行 `output_shape = coordinates.shape[1:]` 就是「输出形状 = 丢掉第 0 维」这条规则的代码体现。

复数输出的拆分（实部、虚部独立插值）：

[_interpolation.py:L457-L466](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L457-L466) —— 当**输入**（不是坐标）是复数时，把实部、虚部分别递归调用一次 `map_coordinates`，写进同一个复数 `output` 的 `.real` / `.imag`。注意：能复用 `output` 是因为 `_get_output` 在复数情形下分配了复数 dtype（见 4.3.3）。

预滤波装配块 + 编码 mode + 调 C 内核（函数的「执行核心」）：

[_interpolation.py:L467-L476](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L467-L476) —— 这是全函数最关键的 10 行：
- L467–L472：若 `prefilter and order > 1`，先 `_prepad_for_spline_filter` 给 `nearest`/`grid-constant` 边界补 12 点（见 4.2），再 `spline_filter(...)` 把样本变成样条系数 `filtered`，同时记下填充量 `npad`（C 端采样时要扣回去）；否则 `npad=0`、`filtered=input`（`order` 0/1 或显式关掉预滤波时走这里）。
- L473：`_extend_mode_to_code(mode)`——**注意没有传 `is_filter`**，因此 `grid-constant`→6、`grid-wrap`→5（详见 4.4）。
- L474–L475：调用 `_nd_image.geometric_transform`，其中第 2 个实参 `mapping=None`、第 3 个实参是 `coordinates`——C 端据此判定「直接用坐标表查找」而不是「调回调」。末尾两个 `None` 对应 `extra_arguments` / `extra_keywords`（`map_coordinates` 不支持回调，所以永远是 `None`）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「坐标数组第 0 维 = 各轴坐标、其余维 = 输出形状」这条约定，并体会 `mode='nearest'` 与默认 `mode='constant'` 的取值差异。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage

a = np.arange(12.).reshape(4, 3)
# a =
# [[ 0.  1.  2.]
#  [ 3.  4.  5.]
#  [ 6.  7.  8.]
#  [ 9. 10. 11.]]

# 坐标形状 (2, 2)：第 0 行是「轴 0 的坐标」，第 1 行是「轴 1 的坐标」
# 含义：取 a[0.5, 0.5] 与 a[2, 1] 两个点，输出形状 = (2,) 即一维长度 2
coords = np.array([[0.5, 2],
                   [0.5, 1]])
print(ndimage.map_coordinates(a, coords, order=1))
# 预期（线性插值）：a[0.5,0.5] = (0+1+3+4)/4 = 2.0；a[2,1] = 7.0 → [2. 7.]
```

这与官方文档字符串里的示例完全一致：

[_interpolation.py:L431-L432](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L431-L432) —— 文档示例 `ndimage.map_coordinates(a, [[0.5, 2], [0.5, 1]], order=1)` 返回 `array([2., 7.])`，正好印证上面的坐标约定。

**接着把第二个点移到数组外**，对比两种 mode：

```python
inds = np.array([[0.5, 2],
                 [0.5, 4]])   # 第二个点的「轴 1 坐标 = 4」已超出 a 的列数 3
print(ndimage.map_coordinates(a, inds, order=1, cval=-33.3))            # constant
print(ndimage.map_coordinates(a, inds, order=1, mode='nearest'))       # nearest
```

**需要观察的现象**：

- `coords` 形状为 `(2, 2)`，输出是一维长度 2——验证「丢掉第 0 维」。
- 第一组（`constant`）第二个值应是 `cval=-33.3`，因为 `a[2, 4]` 落在数组外的常量填充区；
- 第二组（`nearest`）第二个值应是 `a[2, 2] = 8.0`，因为越界时复制最近的边缘像素。

**预期结果**：`[2. -33.3]` 与 `[2. 8.]`（与文档示例 [_interpolation.py:L437-L441](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L437-L441) 一致）。若你在本地得到不同数字，请优先检查 `coords` 的第 0 维长度是否等于 `a.ndim`。

#### 4.1.5 小练习与答案

**练习 1**：对上面的 `a`，想取出 `a[1, 2]`、`a[3, 0]`、`a[0, 1]` 三个点，输出为一维长度 3 的数组，`coordinates` 应该怎么写？

**答案**：`coordinates = np.array([[1, 3, 0], [2, 0, 1]])`，形状 `(2, 3)`，第 0 行是轴 0 坐标、第 1 行是轴 1 坐标。输出形状 `(3,)`。

**练习 2**：如果误把 `coordinates` 写成形状 `(4, 3)`（与输入同形状），对同样的 `a` 调用 `map_coordinates` 会发生什么？

**答案**：`coordinates.shape[0] == 4 != a.ndim == 2`，触发 L455–L456 的 `RuntimeError('invalid shape for coordinate array')`。

**练习 3**：为什么 `map_coordinates` 没有 `output_shape` 形参，而 `geometric_transform` / `affine_transform` 都有？

**答案**：因为 `map_coordinates` 的输出形状由 `coordinates.shape[1:]` 唯一决定（L452），不需要也不能再单独指定；而 `geometric_transform` 用回调生成坐标，输出形状事先未知，必须由 `output_shape` 告诉它。

---

### 4.2 prefilter 调用：把样本变成样条系数

#### 4.2.1 概念说明

u3-l1 已经讲过：`order >= 2` 时样本值不等于样条系数，必须先「预滤波」反解出系数，插值曲线才会经过样本点。`map_coordinates` 把这件事用一个 `prefilter` 形参（默认 `True`）封装好了——你通常不需要手动 `spline_filter`。

但理解这一步在函数里**究竟发生在哪、做了什么**，对正确使用高阶插值、以及用「手动 `spline_filter` + `prefilter=False`」复现结果都很关键。

需要特别强调的是**边界填充**：C 端样条预滤波对 `nearest` 和 `grid-constant` 两种边界没有精确的递归初值，所以 Python 层先用 `np.pad` 在四周补 12 个点（基于极点衰减 `|z|^12 ≈ 4×10⁻⁵` 足够小），滤波后再让 C 端把多出来的 `npad` 扣掉。其他边界模式（`reflect`/`mirror`/`wrap`/`grid-wrap`/`constant`/`grid-mirror`）在 C 端有精确实现，无需预填充（`npad=0`）。

#### 4.2.2 核心流程

预滤波装配块（在 `map_coordinates`、`geometric_transform`、`affine_transform`、`shift`、`zoom` 五个函数里几乎逐字相同）：

```text
if prefilter and order > 1:
    padded, npad = _prepad_for_spline_filter(input, mode, cval)
    filtered = spline_filter(padded, order, output=np.float64, mode=mode)
else:
    npad = 0
    filtered = input
```

- **`prefilter and order > 1`**：两个条件缺一不可。`order ∈ {0,1}` 时系数就是样本，无需预滤波；显式 `prefilter=False` 时跳过（用于你已经手动预滤波过的输入）。
- **`_prepad_for_spline_filter`**：决定要不要补 12 点。
- **`spline_filter(..., output=np.float64)`**：预滤波结果**强制存成 `float64`**，避免中间精度损失。
- **`npad`**：传给 C 内核，让它在采样后把坐标偏移回来（`filtered` 比 `input` 大了 `2*npad`）。

#### 4.2.3 源码精读

`_prepad_for_spline_filter` 的全部实现：

[_interpolation.py:L212-L225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L212-L225) —— 只对 `nearest`（用 `np.pad(mode='edge')`）和 `grid-constant`（用 `np.pad(mode='constant', constant_values=cval)`）补 `npad=12`；其余模式 `npad=0`、原样返回。注释 L221–L222 点明「其他模式有精确边界条件，无需预填充」。

`map_coordinates` 里的预滤波装配块本身就是 4.1.3 引用的 L467–L472：

[_interpolation.py:L467-L472](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L467-L472) —— 注意 `spline_filter` 的 `output=np.float64`：即使你给 `map_coordinates` 传了 `output=np.float32`，**预滤波仍用 float64 中间精度**，只有最终写进 `output` 时才降精度。

`prefilter` 形参的官方文档说明（由 `docfiller` 注入）：

[_ni_docstrings.py:L193-L200](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_docstrings.py#L193-L200) —— 解释了「默认 `True` 会在 `order>1` 时建一个临时 `float64` 滤波数组；若手动设 `False` 且输入没预滤波过，输出会略微模糊」。

#### 4.2.4 代码实践

**实践目标**：用「手动 `spline_filter` + `prefilter=False`」精确复现 `prefilter=True`（默认）的结果，从而亲手验证装配块的作用。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage

step = np.zeros(8)
step[4:] = 1.0                      # 一个阶跃，用来放大「是否预滤波」的差异
coord = np.array([[3.5]])           # 在亚像素 3.5 处取一个点

# (A) 默认：内部自动预滤波
a_default = ndimage.map_coordinates(step, coord, order=3, mode='nearest')

# (B) 关掉预滤波，直接拿原始样本当系数
b_nofilter = ndimage.map_coordinates(step, coord, order=3, mode='nearest',
                                     prefilter=False)

# (C) 手动预滤波 + 关掉内部预滤波  —— 应当与 (A) 完全一致
coeffs = ndimage.spline_filter(step, order=3, mode='nearest')
c_manual = ndimage.map_coordinates(coeffs, coord, order=3, mode='nearest',
                                   prefilter=False)

print("default      :", a_default)
print("no prefilter :", b_nofilter)
print("manual+False :", c_manual)
```

**需要观察的现象**：

- (A) 与 (C) 应**逐位相等**（因为 (A) 内部做的就是「`spline_filter` 后再以系数采样」）；
- (B) 与 (A) 不同，且在阶跃附近可能出现**欠冲/过冲不一致**——这正是 u3-l1 讲过的「不预滤波则曲线不经过样本」的体现。

**预期结果**：(A)≈(C)，且都 ≠ (B)。具体数值**待本地验证**（取决于样条系数与采样点的精确组合），但「(A) 与 (C) 相等、与 (B) 不等」这一关系是确定的。

#### 4.2.5 小练习与答案

**练习 1**：把上面示例的 `order` 从 3 改成 1，(A)、(B)、(C) 三者关系会变成什么？

**答案**：三者应全部相等。因为 `order=1`（线性）时系数就是样本，预滤波是空操作（`prefilter and order > 1` 为假，`filtered=input`），所以 (A) 与 (B) 同；(C) 里 `spline_filter` 对 `order=1` 也是恒等，故 (C) 也同。

**练习 2**：为什么 `spline_filter` 在装配块里被强制 `output=np.float64`，而不是用调用者传给 `map_coordinates` 的 `output` dtype？

**答案**：预滤波涉及因果/反因果递归扫描，对精度敏感；若用低精度（如 float32）存放中间系数会累积舍入误差。所以中间结果用 float64，只在最后写回 `output` 时降精度。

**练习 3**：`mode='reflect'` 时，`_prepad_for_spline_filter` 返回的 `npad` 是多少？为什么？

**答案**：`npad=0`。因为 `reflect`（及 `mirror`/`wrap`/`grid-wrap`/`constant`/`grid-mirror`）在 C 端样条内核里有**精确**的边界递归初值，不需要靠「补点 + 衰减近似」来处理边界；只有 `nearest` 和 `grid-constant` 缺精确初值才补 12 点。

---

### 4.3 _get_output（带 output_shape）：从坐标推导输出

#### 4.3.1 概念说明

`map_coordinates` 调 `_get_output` 时多传了一个关键字 `shape=output_shape`。这正是它和大多数滤波函数的区别：**输出形状不再等于输入形状，而是由坐标数组决定**。

`_get_output` 是 u1-l4 讲过的共享工具，它统一处理 `output` 的三种形态：

- `output=None`：按 `input` 的 dtype（复数则提升）新建一个形状为 `shape` 的数组；
- `output` 是 dtype 类或 dtype 对象（如 `np.float32`、`np.dtype('f')`）：按该 dtype 新建；
- `output` 是字符串（如 `'f'`）：先转成 dtype 再新建；
- `output` 是已有数组：**校验其形状必须等于 `shape`**，否则报错——即就地复用。

#### 4.3.2 核心流程

`map_coordinates` 里的输出准备只有一行（跨两行书写）：

```text
output = _ni_support._get_output(output, input, shape=output_shape,
                                 complex_output=complex_output)
```

- `shape=output_shape`：把「丢掉坐标第 0 维」得到的形状传进去；
- `complex_output`：当输入是复数时为 `True`，让 `_get_output` 分配复数 dtype，这样 4.1.3 里拆实/虚部时才能写进 `output.real` / `output.imag`。

#### 4.3.3 源码精读

`map_coordinates` 中的调用点：

[_interpolation.py:L458-L459](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L458-L459) —— 注意它带了 `shape=output_shape`；对比 `shift` 函数里的同名调用（不带 `shape`，输出形状=输入形状），就能看出 `map_coordinates` 的特殊性。

`_get_output` 的实现（四分支）：

[_ni_support.py:L78-L107](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_support.py#L78-L107) —— 关键看几点：
- L79–L80：`shape is None` 时回退到 `input.shape`（滤波函数走这条）；`map_coordinates` 传了 `shape`，所以走指定形状。
- L81–L86：`output is None` 分支，按 `input.dtype.name`（复数则 `promote_types(input.dtype, complex64)`）新建。
- L87–L92：`output` 是 dtype 类时新建；若 `complex_output` 但 dtype 非复数，会**告警并提升为复数**。
- L100–L104：`output` 是已有数组时，**强制校验 `output.shape == shape`**——这就是为什么你给 `map_coordinates` 传一个预先分配好的数组时，它的形状必须正好等于 `coordinates.shape[1:]`。

字符串 `output` 的实测用例可在测试里看到：

[tests/test_interpolation.py:L601-L606](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_interpolation.py#L601-L606) —— `test_map_coordinates_with_string_output` 验证 `output='f'` 时返回数组 dtype 为 `np.dtype('f')`，对应 `_get_output` 的 L93–L99 字符串分支。

而「传已有数组、其形状必须匹配」的行为，由这个测试守护：

[tests/test_interpolation.py:L580-L598](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_interpolation.py#L580-L598) —— `test_map_coordinates_endianness_with_output_parameter` 用 `np.empty_like(expected)` 作为 `output` 传入，其形状与 `expected`（即 `coordinates.shape[1:]`）一致，故能就地写入。

#### 4.3.4 代码实践

**实践目标**：直接观察 `_get_output` 在 `map_coordinates` 里如何根据 `output` 的不同形态分配或复用数组，并亲手触发「形状不匹配」报错。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage
from scipy.ndimage import _ni_support

a = np.arange(12.).reshape(4, 3)
coords = np.array([[0.5, 2], [0.5, 1]])     # 输出形状 = (2,)

# 形态 1：output=None → 新建同 dtype 数组
out1 = ndimage.map_coordinates(a, coords, order=1)
print("None      :", out1.dtype, out1.shape)   # float64 (2,)

# 形态 2：output=np.float32 → 按 dtype 新建
out2 = ndimage.map_coordinates(a, coords, order=1, output=np.float32)
print("float32   :", out2.dtype, out2.shape)   # float32 (2,)

# 形态 3：output=已有数组 → 就地写入（形状必须正好是 (2,)）
buf = np.empty(2, dtype=np.float64)
ret  = ndimage.map_coordinates(a, coords, order=1, output=buf)
print("in-place  :", ret is buf, buf)          # True  [2. 7.]

# 形态 4：形状不匹配 → 触发 _get_output 的 RuntimeError
bad = np.empty(3, dtype=np.float64)            # 形状 (3,) ≠ (2,)
try:
    ndimage.map_coordinates(a, coords, order=1, output=bad)
except RuntimeError as e:
    print("shape mismatch raised:", e)
```

**需要观察的现象**：

- 前三种形态都返回正确结果 `[2. 7.]`，但 dtype 与「是否就地」不同；
- 第三种 `ret is buf` 为 `True`，证明传入数组被就地复用；
- 第四种抛出 `RuntimeError: output shape not correct`（来自 [_ni_support.py:L103-L104](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_support.py#L103-L104)）。

**预期结果**：如上。具体打印格式**待本地验证**，但「四种形态分别给出 None 新建 / dtype 新建 / 就地复用 / 形状不符报错」这四类行为是确定的。

#### 4.3.5 小练习与答案

**练习 1**：想把结果存成一个 `bool` 数组（如同文档示例 `output=bool`），`_get_output` 会走哪个分支？

**答案**：走 L87–L92 的「dtype 类」分支（`isinstance(output, type)` 为真，`bool` 是类型）。`np.zeros(shape, dtype=bool)` 得到布尔数组，随后样条插值结果被强制转换写入（非零→True）。

**练习 2**：为什么 `map_coordinates` 必须把 `shape=output_shape` 传给 `_get_output`，而 `correlate1d` 之类的滤波函数不需要？

**答案**：滤波函数输出形状恒等于输入形状（`shape` 缺省时 `_get_output` 用 `input.shape`）；`map_coordinates` 的输出形状由坐标数组决定，可能放大、缩小或改变维数，所以必须显式传 `shape`。

**练习 3**：若输入是复数数组、但你传了 `output=np.float64`，会发生什么？

**答案**：`_get_output` 在 L89–L91 检测到 `complex_output=True` 但 dtype 非复数，会**告警**（`promoting specified output dtype to complex`）并把 dtype 提升为复数，随后正常完成实/虚部分别插值。

---

### 4.4 mode 在插值中的细微差别：constant/grid-constant、wrap/grid-wrap

#### 4.4.1 概念说明

这是本讲学习目标里特意点出的一对易混淆点。ndimage 一共有 8 种边界模式字符串，其中有两组「看起来只差一个 `grid-` 前缀」：

- `constant` vs `grid-constant`
- `wrap` vs `grid-wrap`

在**滤波**函数里（u2 系列），`_extend_mode_to_code(mode, is_filter=True)` 会把它们**合并**（`grid-constant`→4 同 `constant`、`grid-wrap`→1 同 `wrap`），因为纯滤波只关心「越界像素取什么值」。

但在**插值**函数里（`map_coordinates` 等），`_extend_mode_to_code(mode)` **不带** `is_filter`（用默认 `False`），于是它们保持**不同的整数码**：`grid-constant`→6、`grid-wrap`→5。差别在于：

| 模式 | 码 | 边界语义（插值场景） |
|------|----|----------------------|
| `constant` | 4 | 越界处**不做插值**，直接取 `cval`（边缘像素本身仍是真值） |
| `grid-constant` | 6 | 越界处用 `cval` 填充后**仍参与插值**，样条会平滑地「滑入」常量区 |
| `wrap` | 1 | 周期延拓，但首末样本**重叠**，重叠点取值不明确 |
| `grid-wrap` | 5 | 周期延拓，首末样本**不重叠**（干净周期） |

#### 4.4.2 核心流程

`map_coordinates` 把 `mode` 字符串编成整数码的那一行（4.1.3 的 L473）就是本节焦点：

```text
mode = _ni_support._extend_mode_to_code(mode)   # 注意：没有 is_filter=True
```

因为没有 `is_filter=True`，分支落到「插值专用」的码上：

- `mode == 'grid-constant'` → 不满足 L54（`is_filter` 假）→ 落到 L56–L57 → **6**
- `mode == 'constant'` → L48 → **4**
- `mode == 'grid-wrap'` → 不满足 L50 → 落到 L52–L53 → **5**
- `mode == 'wrap'` → L42 → **1**

#### 4.4.3 源码精读

`_extend_mode_to_code` 的完整实现：

[_ni_support.py:L37-L59](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_support.py#L37-L59) —— 重点对比 L48（`constant`→4）与 L56–L57（`grid-constant`→6）、L42（`wrap`→1）与 L52–L53（`grid-wrap`→5）。`is_filter` 只在 L50、L54 两个 `and` 条件里出现——也就是说它**只**影响 `grid-constant` 与 `grid-wrap` 两个名字。`map_coordinates` 不传 `is_filter`，所以这两个名字各自独立编码。

`map_coordinates` 里调用它的一行：

[_interpolation.py:L473](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L473) —— `mode = _ni_support._extend_mode_to_code(mode)`，无 `is_filter`。

两种 `constant` 的官方文字描述（权威出处）：

[_ni_docstrings.py:L91-L99](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_docstrings.py#L91-L99) —— `constant`：*"No interpolation is performed beyond the edges of the input."*；`grid-constant`：*"Interpolation occurs for samples outside the input's extent as well."* 这一字之差，就是 4 与 6 的全部区别。

两种 `wrap` 的官方文字描述：

[_ni_docstrings.py:L109-L122](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_ni_docstrings.py#L109-L122) —— `grid-wrap` 是干净周期 `a b c d | a b c d | a b c d`；`wrap` 则是首末重叠 `b c d b c | a b c d | b c a b c`，重叠点取值「not well defined」。

#### 4.4.4 代码实践

**实践目标**：在同一组「靠近/越过边界」的坐标上，对比 `constant` 与 `grid-constant`、`wrap` 与 `grid-wrap` 的输出差异，体会「是否参与插值」的影响。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage

x = np.array([0., 0., 0., 1., 0., 0., 0.])     # 一个孤立峰，便于看边界扩散
# 在 [-1.5, 5.5] 范围内取 8 个点，覆盖越界区域
coords = np.array([np.linspace(-1.5, 5.5, 8)])

for m in ['constant', 'grid-constant']:
    print(m, ndimage.map_coordinates(x, coords, order=3, mode=m, cval=0.0))
for m in ['wrap', 'grid-wrap']:
    print(m, ndimage.map_coordinates(x, coords, order=3, mode=m))
```

**需要观察的现象**：

- `constant`：越界点直接是 `cval=0`，边缘附近**不**出现由峰扩散过来的值；
- `grid-constant`：越界点虽然也是 0，但**靠近边界的内侧点**会因为「常量区参与样条插值」而与 `constant` 不同；
- `wrap` 与 `grid-wrap`：两者都做周期延拓，但因首末是否重叠，边界附近的数值会有细微差别。

**预期结果**：`constant ≠ grid-constant`、`wrap ≠ grid-wrap`（在越界或近界坐标上）。具体数值**待本地验证**，但「成对不相等」这一结论由不同的整数码（4 vs 6、1 vs 5）保证。

> 小贴士：差异在 `order >= 2` 时最明显，因为高阶样条的支撑域更宽、越界影响更深远；`order=1`（线性）时差异较小但依然存在。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_extend_mode_to_code` 要用 `is_filter` 参数把滤波和插值两种场景分开？

**答案**：纯滤波（u2 系列）只关心「越界像素取什么常量值」，`grid-constant` 与 `constant`、`grid-wrap` 与 `wrap` 行为等价，合并成同一个 C 码可以复用同一段内核代码；而插值要区分「越界处是否仍做样条插值」，所以必须保留独立码（6 与 4、5 与 1）。`is_filter=True` 即「我是滤波，请合并」，`is_filter=False`（默认）即「我是插值，请保留区别」。

**练习 2**：`reflect` 和 `grid-mirror` 在 `_extend_mode_to_code` 里是同一个码吗？

**答案**：是，都是 2（L44–L45：`elif mode in ['reflect', 'grid-mirror']: return 2`）。这两个名字在所有场景下都完全等价，`grid-mirror` 只是为了和 `grid-constant`/`grid-wrap` 命名一致而设的别名。

**练习 3**：在 `map_coordinates` 里传一个不存在的 `mode='foo'` 会怎样？

**答案**：`_extend_mode_to_code` 走完所有 `elif` 都不匹配，落到 L58–L59 抛出 `RuntimeError('boundary mode not supported')`。

---

## 5. 综合实践

**任务**：用一个 4×4 数组，分别用 `map_coordinates` 与 `zoom` 做 2 倍上采样（放大到 8×8），验证两者在 `order=3` 下结果一致，从而亲手证明「`zoom` 只是 `map_coordinates` 的一个自动生成坐标的特例」。然后再换不同 `mode` 观察边界差异。

**背景**：`zoom(factor=2, order=3)` 在 `grid_mode=False`（默认）下，把输出像素索引 `o` 映射到输入坐标 `o * (in_shape-1)/(out_shape-1) = o * 3/7`，恰好就是 `np.linspace(0, 3, 8)`。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage

a = np.arange(16., dtype=np.float64).reshape(4, 4)

# (1) 用 zoom 直接放大
z = ndimage.zoom(a, 2, order=3, mode='constant')   # 默认 grid_mode=False

# (2) 用 map_coordinates 手动构造同样的坐标网格
g = np.linspace(0, 3, 8)                            # 0, 3/7, 6/7, ..., 3
yy, xx = np.meshgrid(g, g, indexing='ij')
coords = np.stack([yy, xx], axis=0)                 # 形状 (2, 8, 8)
m = ndimage.map_coordinates(a, coords, order=3, mode='constant')

print("zoom shape           :", z.shape)
print("map_coordinates shape:", m.shape)
print("max abs diff         :", np.max(np.abs(z - m)))

# (3) 换 mode 观察边界：constant vs grid-constant vs nearest
for mode in ['constant', 'grid-constant', 'nearest']:
    out = ndimage.map_coordinates(a, coords, order=3, mode=mode)
    print(f"{mode:15s} 边角值 =", out[0, 0], out[-1, -1])
```

**需要观察的现象与预期**：

1. `z.shape` 与 `m.shape` 都是 `(8, 8)`——因为 `map_coordinates` 的输出形状 = `coords.shape[1:] = (8,8)`；
2. `np.max(np.abs(z - m))` 应**接近 0**（理想为 0，可能有极小浮点误差），证明 `zoom` 与「用对应坐标的 `map_coordinates`」等价；
3. 三种 mode 在边角（`(0,0)`、`(7,7)`）的取值不同：`constant` 受 `cval` 影响、`grid-constant` 让常量区参与插值、`nearest` 复制边缘像素。

**预期结果**：第 2 项「最大绝对差≈0」、第 3 项「三种 mode 边角值不同」。具体数值**待本地验证**（取决于本地 SciPy/NumPy 版本与浮点实现），但「`zoom` ≈ `map_coordinates`」「mode 间边角取值不同」这两条关系是确定的。

> 思考延伸：若把 `zoom` 改成 `grid_mode=True`，对应的输入坐标网格会变成 `np.linspace(0, 4, 8) - 0.5`（包含像素全宽）。可以试着改造上面的 `g`，看看 `grid_mode` 下 `map_coordinates` 与 `zoom` 是否仍能对齐——这是通向 u3-l4（`zoom`/`geometric_transform`）的预热。

## 6. 本讲小结

- `map_coordinates` 是**坐标驱动**的插值：你给坐标表，它在那些位置做样条采样；坐标数组形状为 `(ndim, *output_shape)`，因此**输出形状 = `coordinates.shape[1:]`**，函数没有也不需要 `output_shape` 形参。
- 它**复用** `_nd_image.geometric_transform` 这一 C 内核：只要把 `mapping` 传 `None`、坐标数组传到第三实参，C 端就「直接用坐标表查找」。
- **预滤波装配块**（`if prefilter and order > 1:`）在采样前先把样本变成样条系数：对 `nearest`/`grid-constant` 边界先补 12 点（`_prepad_for_spline_filter`），再 `spline_filter(..., output=np.float64)`，并把 `npad` 传给 C 端用于回偏；可用「手动 `spline_filter` + `prefilter=False`」精确复现默认行为。
- `_get_output` 在本函数里**带 `shape=output_shape`** 调用，使输出形状与输入形状脱钩；`output` 支持 `None`/dtype/字符串/已有数组四种形态，已有数组时强制校验形状匹配。
- **插值场景下 `_extend_mode_to_code` 不带 `is_filter`**，因此 `constant`(4)≠`grid-constant`(6)、`wrap`(1)≠`grid-wrap`(5)：前者区分「越界是否仍做插值」，后者区分「首末是否重叠」。`reflect`≡`grid-mirror`(2)。
- 复数输入被拆成实部、虚部分别递归调用，写进同一个复数 `output`；复数**坐标**则被直接禁止。

## 7. 下一步学习建议

- **u3-l3（affine_transform）**：把本讲的「坐标由你直接给」推广到「坐标由矩阵 + 偏移生成」，理解 `matrix` 的四种形状分支与 pull 重采样模型，并看清它与 `map_coordinates` 共用同一段预滤波装配块、同一条 `_nd_image.geometric_transform` 内核。
- **u3-l4（geometric_transform / shift / zoom / rotate）**：`geometric_transform` 用回调生成坐标（`mapping` 不为 `None`），`shift`/`zoom`/`rotate` 则是 `affine_transform` 的便捷封装；学完你会拥有完整的「坐标从哪来」图谱。
- **继续阅读源码**：把 `_interpolation.py` 里五个插值函数（`map_coordinates`/`geometric_transform`/`affine_transform`/`shift`/`zoom`）的「预滤波装配块」并排对照，确认它们几乎逐字相同——这是识别「公共骨架」的好练习。若想下探到 C 端的样条采样实现，等到 u6-l3（C 滤波/插值/样条内核）。
