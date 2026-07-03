# 样条预滤波

## 1. 本讲目标

本讲是「插值与几何变换」单元（u3）的地基。读完后你应当能够：

- 说清 **样条阶数 `order`（0–5）与样条系数的关系**，并能解释为什么 `order > 1` 时必须先做一次「预滤波」才能插值；
- 读懂 `_interpolation.py` 中的三个函数：`spline_filter1d`、`spline_filter`、`_prepad_for_spline_filter`，并知道它们各自在哪一层、互相如何串联；
- 解释 `_prepad_for_spline_filter` 为什么只对 `nearest` 和 `grid-constant` 两种边界做 **12 点填充**；
- 说清 `map_coordinates`、`affine_transform` 等函数里 `prefilter=True`（默认开启）内部究竟做了什么，从而能用手动 `spline_filter` + `prefilter=False` 复现 `prefilter=True` 的结果。

本讲只读一个 Python 文件 `_interpolation.py`，并下探到 C 内核 `src/ni_splines.c` 与 `src/nd_image.c` 中与样条滤波直接相关的最小片段。

## 2. 前置知识

本讲默认你已经掌握 u1-l4 讲过的共享支撑工具，尤其是：

- `_ni_support._extend_mode_to_code(mode)`：把边界模式字符串翻译成 C 内核需要的整数码（`nearest→0`、`mirror→3`、`grid-constant→6` 等）。
- `_ni_support._get_output(output, ...)`：统一处理 `output` 的 `None` / dtype / 已有数组三种形态。

下面补充两个本讲要用到、但还没讲过的概念。

### 2.1 什么是 B 样条插值

给定一组离散样本 \(s[0], s[1], \dots, s[N-1]\)，我们希望构造一条连续曲线 \(S(x)\)，使得 \(S(k)=s[k]\)（过每个样本点），并在样本之间做平滑拟合。scipy.ndimage 的所有插值函数（`map_coordinates`、`affine_transform`、`zoom`、`shift`、`rotate`）内部都用 **B 样条（B-spline）** 作为基底：

\[
S(x) = \sum_{k} c[k]\,\beta^{n}(x-k)
\]

其中 \(\beta^{n}\) 是 \(n\) 阶 B 样条核函数（一个紧支撑的平滑钟形函数），\(c[k]\) 是 **样条系数**，\(n\) 就是 `order`。

关键点：**样本值 \(s[k]\) 和样条系数 \(c[k]\) 不是一回事**。只有当 `order` 为 0（最近邻）或 1（线性）时，系数恰好等于样本值 \(c[k]=s[k]\)；当 `order >= 2`，系数需要由样本反解出来——这个反解过程就叫 **预滤波（prefilter）**。本讲的主角就是它。

| order | 插值名称 | 是否需要预滤波 | 说明 |
|-------|----------|----------------|------|
| 0 | 最近邻 | 否 | 系数 = 样本 |
| 1 | 双线性 | 否 | 系数 = 样本 |
| 2 | 二次 B 样条 | **是** | 1 个极点 |
| 3 | 三次 B 样条（默认） | **是** | 1 个极点 |
| 4 | 四次 B 样条 | **是** | 2 个极点 |
| 5 | 五次 B 样条 | **是** | 2 个极点 |

### 2.2 预滤波为什么是「递归滤波」

把样本 \(s\) 变成系数 \(c\) 的过程，等价于对一个滤波器求逆。该滤波器的传递函数由若干个极点 \(z_i\) 决定（`order` 越高极点越多），而求逆可以在时域用 **因果 + 反因果两次递归扫描** 高效完成——这正是 C 内核 `ni_splines.c` 的做法。具体极点和扫描公式放在 4.1 讲。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_interpolation.py](_interpolation.py) | 全部三个目标函数都在这里：`spline_filter1d`、`spline_filter`、`_prepad_for_spline_filter`；以及 `map_coordinates` 里调用它们的预滤波装配块 |
| [src/ni_splines.c](src/ni_splines.c) | C 内核：`get_filter_poles` 给出各阶极点，`apply_filter` + `_apply_filter` 做因果/反因果递归扫描 |
| [src/nd_image.c](src/nd_image.c) | `Py_SplineFilter1D` 是 Python↔C 的胶水包装，在 `methods[]` 分发表里登记为 `spline_filter1d` |
| [_ni_support.py](_ni_support.py) | `_extend_mode_to_code` 把边界模式字符串编码成整数，喂给 C 内核 |

## 4. 核心概念与源码讲解

### 4.1 spline_filter1d：一维样条滤波原子操作

#### 4.1.1 概念说明

`spline_filter1d` 是整个插值子包里「把样本变成样条系数」的原子操作：它沿**单个轴**对每一条线做一维预滤波。理解了它，`spline_filter`（多维）只是把它逐轴循环调用一遍。

为什么 `order > 1` 必须预滤波？因为高阶 B 样条的系数 \(c[k]\) 满足一个递推关系：样本是被 B 样条核「平滑」过的，要从平滑后的样本还原出系数，必须做一次反滤波（即预滤波）。如果跳过这一步（等价于把样本直接当成系数），那么插值曲线 \(S(x)\) **不会经过样本点**——这是初学者最容易踩的坑，也是 `prefilter` 参数存在的全部理由。

#### 4.1.2 核心流程

`spline_filter1d` 的执行流程：

1. 校验 `order` 必须在 0–5 之间；
2. 准备输出数组（复数输入会拆成实部/虚部独立处理，见 u2-l1 的 `_complex_via_real_components` 思路）；
3. **短路**：若 `order in [0, 1]`，直接把输入拷给输出（因为系数=样本，无需滤波）；
4. 否则：把 `mode` 编码成整数、规范 `axis`，调用 C 内核 `_nd_image.spline_filter1d`。

C 内核内部（`ni_splines.c`）做的事：

1. `get_filter_poles(order)` 查出极点 \(z_i\)（`order/2` 个）；
2. `_apply_filter_gain`：先乘上增益 \(\prod_i (1-z_i)(1-1/z_i)\)；
3. 对每个极点跑一次 `_apply_filter`：
   - **因果扫描**（从左到右）：\(c[i] \mathrel{+}= z\,c[i-1]\)；
   - **反因果扫描**（从右到左）：\(c[i] = z\,(c[i+1]-c[i])\)；
   - 两次扫描的起点由边界模式决定（`mirror` / `wrap` / `reflect` 各有一对精确的初始化函数）。

增益有个好记的结论：`order=2` 时增益为 8，`order=3` 时增益为 6（你可以用极点值自己验证）。

#### 4.1.3 源码精读

Python 端入口（注意 0/1 阶的短路分支）：

[_interpolation.py:L48-L134](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L48-L134) —— `spline_filter1d` 全函数。关键三段：

```python
if order < 0 or order > 5:
    raise RuntimeError('spline order not supported')        # 阶数合法性
...
if order in [0, 1]:
    output[...] = np.array(input)                           # 短路：0/1 阶系数=样本
else:
    mode = _ni_support._extend_mode_to_code(mode)           # mode → 整数码
    axis = normalize_axis_index(axis, input.ndim)
    _nd_image.spline_filter1d(input, order, axis, output, mode)  # 进入 C 内核
```

C 端极点表（`order/2` 个，全部为负、绝对值小于 1，保证递归稳定）：

[src/ni_splines.c:L117-L153](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_splines.c#L117-L153) —— `get_filter_poles`，例如 `order=3` 时 `poles[0] = sqrt(3)-2 ≈ -0.2679`。

因果/反因果递归扫描的核心（一正一反两个 for 循环）：

[src/ni_splines.c:L252-L265](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_splines.c#L252-L265) —— `_apply_filter`：

```c
causal_init(c, n, z);                  // 用边界模式算出最左端起点 c[0]
for (i = 1; i < n; ++i)
    c[i] += z * c[i - 1];              // 因果：向右递推
anticausal_init(c, n, z);              // 用边界模式算出最右端起点 c[n-1]
for (i = n - 2; i >= 0; --i)
    c[i] = z * (c[i + 1] - c[i]);      // 反因果：向左递推
```

> 说明：上式里两次扫描把「样本」就地改写成「样条系数」。边界起点 `causal_init` / `anticausal_init` 之所以要分 `mirror` / `wrap` / `reflect` 三套，是为了让递归在数组两端也能精确满足对应的边界对称性（这一点直接决定了 4.3 的填充逻辑）。

C 包装函数与分发表登记（Python 名 `spline_filter1d` → C 函数 `Py_SplineFilter1D` → 内核 `NI_SplineFilter1D`）：

[src/nd_image.c:L635-L652](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L635-L652) —— `Py_SplineFilter1D`：用 `PyArg_ParseTuple` 解析 `(input, order, axis, output, mode)`，调 `NI_SplineFilter1D`。
[src/nd_image.c:L1336](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1336) —— `methods[]` 里的登记条目 `{"spline_filter1d", ...}`。

#### 4.1.4 代码实践

**目标**：直观看到「预滤波把样本变成了另一组数（系数）」，并验证 0/1 阶短路。

**步骤**：

```python
import numpy as np
from scipy.ndimage import spline_filter1d

a = np.array([0., 0., 0., 1., 1., 1., 1., 0., 0., 0.])  # 含一个脉冲 plateau
print("order=1:", spline_filter1d(a, order=1))           # 应与 a 完全相同（短路）
print("order=3:", spline_filter1d(a, order=3))           # 系数，会出现 <0 和 >1 的值
```

**观察与预期**：

- `order=1` 的输出与输入逐元素相等（走了 `output[...] = input` 短路）。
- `order=3` 的输出不再是样本值，在 plateau 两侧会出现**负值**、在 plateau 边缘出现**大于 1 的值**——这正是「反滤波放大了高频」的表现，也是高阶样条能精确还原 sharp 边缘的代价（与 4.4 的振铃现象对应）。
- 数值的具体量级「待本地验证」，但定性结论（出现负/超调）是确定的。

#### 4.1.5 小练习与答案

**练习 1**：`spline_filter1d(a, order=0)` 和 `order=1` 的结果分别是什么？为什么？

> **答**：两者都等于输入 `a` 本身。因为 0 阶（最近邻）和 1 阶（线性）插值的样条系数就等于样本值，函数在第 128–129 行直接短路拷贝，根本不进入 C 内核。

**练习 2**：用本节给出的极点值 \(z=\sqrt{3}-2\)，手算 `order=3` 的滤波增益 \((1-z)(1-1/z)\)。

> **答**：\(z\approx -0.2679\)，\(1-z\approx 1.2679\)，\(1-1/z=1-(-3.732)\approx 4.732\)，乘积 \(\approx 1.2679\times 4.732 \approx 6.0\)。即三阶 B 样条滤波增益为 6，这也是 `_apply_filter_gain` 会先把系数整体放大约 6 倍的原因。

---

### 4.2 spline_filter：多维逐轴串联

#### 4.2.1 概念说明

`spline_filter` 是 `spline_filter1d` 的多维包装。B 样条核是 **可分离** 的：一个 N 维 B 样条可以分解为各维一维 B 样条的乘积。因此把样本变成 N 维系数，只需沿每个轴依次做一次一维预滤波即可（这点和 u2-l3 高斯滤波的可分离性同源）。

注意一个重要差别：`spline_filter1d` 接受 `order` 0–5，但 `spline_filter` **只接受 2–5**（`order < 2` 直接报错），因为对它而言 0/1 阶没有意义——它本来就是给高阶插值准备系数用的。

#### 4.2.2 核心流程

1. 校验 `order` 必须在 2–5；
2. 准备输出数组（同样支持复数拆分）；
3. 对 `range(input.ndim)` 的每个轴，反复调用 `spline_filter1d`，并用 `input = output` 让上一轴的结果就地成为下一轴的输入（链式复用同一块缓冲，只分配一个临时数组）；
4. 退化情形（0 维数组）直接拷贝。

#### 4.2.3 源码精读

[_interpolation.py:L136-L209](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L136-L209) —— `spline_filter`。核心循环：

```python
if order < 2 or order > 5:
    raise RuntimeError('spline order not supported')      # 注意：比 1d 版更严
...
if order not in [0, 1] and input.ndim > 0:
    for axis in range(input.ndim):
        spline_filter1d(input, order, axis, output=output, mode=mode)
        input = output                                    # 下一轴以上一轴结果为输入（就地）
else:
    output[...] = input[...]
```

> 说明：`input = output` 这一招让多维滤波只占用一份输出缓冲，每个轴的结果直接覆盖在上面。这也意味着如果 `output` 是低精度 dtype（如 `np.float32`），中间结果会逐轴累积截断误差——这正是函数 docstring「intermediate results may be stored with insufficient precision」警告的来源。

#### 4.2.4 代码实践

**目标**：验证 `spline_filter` 等价于「逐轴手动调用 `spline_filter1d`」。

**步骤**：

```python
import numpy as np
from scipy.ndimage import spline_filter, spline_filter1d

rng = np.random.default_rng(0)
a = rng.random((4, 5))

# 一次性多维滤波
m = spline_filter(a, order=3, mode='mirror')

# 手动逐轴（链式）
tmp = spline_filter1d(a, order=3, axis=0, mode='mirror')
hand = spline_filter1d(tmp, order=3, axis=1, mode='mirror')

print(np.allclose(m, hand))   # 预期 True
```

**预期**：`True`。这印证了「多维 = 逐轴一维的串联」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `spline_filter` 不像 `spline_filter1d` 那样接受 `order=1`？

> **答**：`spline_filter` 的唯一用途是给高阶（`order>1`）插值准备系数。1 阶插值系数等于样本，根本不需要这个函数；调用它本身就说明用户想用高阶样条，因此把 `order<2` 当作误用直接报错，避免静默返回毫无意义的「恒等」结果。

**练习 2**：把上面实践的 `mode='mirror'` 换成 `mode='nearest'` 再比一次，结果还相等吗？

> **答**：仍然相等。`spline_filter` 内部对每个轴都用同一个 `mode` 调 `spline_filter1d`，可分离性不依赖具体边界模式。`mode` 只影响每条线两端的系数精度（见 4.3）。

---

### 4.3 _prepad_for_spline_filter：边界模式与 12 点填充

#### 4.3.1 概念说明

这是本讲最微妙的一个函数。回到 4.1：C 内核 `apply_filter` 的边界起点 `causal_init`/`anticausal_init` 只为三类边界做了**精确**初始化——`mirror`、`wrap`（对应 `grid-wrap`）、`reflect`。对 `nearest`（重复边缘值）和 `grid-constant`（边界外填 `cval`）这两种边界，C 端没有写精确的初始化公式。

`_prepad_for_spline_filter` 的解决办法是：在滤波**之前**先给数组四周补一圈像素（`nearest` 用 `edge` 即重复边缘，`grid-constant` 用 `constant` 即填 `cval`），补完之后再做（基于 mirror 的）预滤波。因为递归滤波的影响随距离按 \(|z|^{\text{距离}}\) 衰减，只要补得足够宽，边界误差就小到可以忽略。代码里固定补 **12 点**。

为什么是 12？看 4.1 的极点表，`order=5` 时主导极点绝对值最大，约 \(|z|\approx 0.4306\)。补 12 点后，边界近似误差量级为：

\[
|z|^{12} \approx 0.4306^{12} \approx 4\times 10^{-5}
\]

这个量级远小于常规插值的精度需求，因此 12 点是个稳妥的工程折中。对 `mirror` / `wrap` / `reflect` 等已有精确边界的模式，`npad=0`，不补。

#### 4.3.2 核心流程

```
若 mode == 'nearest'        → npad=12, padded = np.pad(input, 12, mode='edge')
若 mode == 'grid-constant'  → npad=12, padded = np.pad(input, 12, mode='constant', constant_values=cval)
其他模式                    → npad=0,  padded = input   # 已有精确边界，无需补
返回 (padded, npad)
```

返回的 `npad` 会在 4.4 里传给 `geometric_transform`，让最终输出**裁掉**这圈填充，所以用户看到的输出形状不受影响。

#### 4.3.3 源码精读

[_interpolation.py:L212-L225](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L212-L225) —— `_prepad_for_spline_filter` 全函数：

```python
def _prepad_for_spline_filter(input, mode, cval):
    if mode in ['nearest', 'grid-constant']:
        npad = 12
        if mode == 'grid-constant':
            padded = np.pad(input, npad, mode='constant', constant_values=cval)
        elif mode == 'nearest':
            padded = np.pad(input, npad, mode='edge')
    else:
        # other modes have exact boundary conditions implemented so
        # no prepadding is needed
        npad = 0
        padded = input
    return padded, npad
```

> 说明：注释「other modes have exact boundary conditions implemented」对应 C 内核 `apply_filter` 里 `mirror` / `wrap` / `reflect` 三套精确初始化函数。可对照 [src/ni_splines.c:L284-L319](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_splines.c#L284-L319) 的 `apply_filter` 的 switch：`NI_EXTEND_MIRROR`/`WRAP` 走 `_init_causal_mirror`，`NI_EXTEND_GRID_WRAP` 走 `_init_causal_wrap`，`NI_EXTEND_NEAREST`/`REFLECT` 走 `_init_causal_reflect`——唯独 `nearest`、`grid-constant` 的精确边界没有专门实现，所以才需要在 Python 端预先补点。

#### 4.3.4 代码实践

**目标**：直接观察 `_prepad_for_spline_filter` 对不同 `mode` 的输出差异。

**步骤**：

```python
import numpy as np
from scipy.ndimage._interpolation import _prepad_for_spline_filter

a = np.arange(6.).reshape(2, 3)
for mode in ['mirror', 'nearest', 'grid-constant']:
    padded, npad = _prepad_for_spline_filter(a, mode, cval=-1.0)
    print(mode, "npad=", npad, "shape=", padded.shape)
```

**预期**：

- `mirror`：`npad=0`，`shape=(2,3)`（原样返回）。
- `nearest`：`npad=12`，`shape=(26,27)`，外圈是边缘值的重复。
- `grid-constant`：`npad=12`，`shape=(26,27)`，外圈全是 `-1.0`。

填充值的具体排布「待本地验证」，但 `npad` 与形状的定性结论是确定的。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `npad` 从 12 改成 2，对 `order=5` 的 `nearest` 模式会有什么影响？

> **答**：边界处系数误差会显著变大。\(0.4306^{2}\approx 0.185\)，即边界附近约 18% 的误差，远高于 12 点时的 \(4\times10^{-5}\)。所以 12 是为了让最高阶（主导极点最大）也能把误差压到可忽略。

**练习 2**：为什么 `mirror` 模式不需要补点，明明 C 端也用 `mirror` 初始化？

> **答**：因为 C 端的 `_init_causal_mirror` / `_init_anticausal_mirror` 已经把 `mirror` 边界的精确解析初值算出来了（见 `_apply_filter` 调用前的 init），递归从精确初值出发，无需任何近似。`nearest`/`grid-constant` 缺的就是这样一对精确初值函数，只能靠预填充来近似。

---

### 4.4 prefilter=True 的自动调用：从 map_coordinates 看三者如何串联

#### 4.4.1 概念说明

`spline_filter1d` / `spline_filter` / `_prepad_for_spline_filter` 三个零件，平常用户不会直接调用——它们被 `map_coordinates`、`affine_transform`、`geometric_transform`、`shift`、`zoom`、`rotate` 在内部自动组装起来。这些插值函数都有一个 `prefilter` 参数，**默认 `True`**，含义是「请自动帮我把样本转成样条系数」。

也就是说：当你调 `map_coordinates(input, coords, order=3)` 时，函数内部先 `_prepad_for_spline_filter` → `spline_filter`，再把得到的系数数组交给 C 内核 `geometric_transform` 做真正的几何重采样，最后裁掉填充。

#### 4.4.2 核心流程

`map_coordinates` 的预滤波装配（`order>1` 且 `prefilter=True` 时）：

```
padded, npad = _prepad_for_spline_filter(input, mode, cval)   # 按需补 12 点
filtered     = spline_filter(padded, order, mode=mode)        # 样本 → 系数
# 把 filtered（系数）+ npad 交给 C 内核做几何重采样
_nd_image.geometric_transform(filtered, ..., output, order, mode, cval, npad, ...)
```

若 `prefilter=False` 或 `order<=1`，则跳过这两步，`filtered=input`、`npad=0`，直接进入 C 内核。

#### 4.4.3 源码精读

[_interpolation.py:L467-L475](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L467-L475) —— `map_coordinates` 里的装配块：

```python
if prefilter and order > 1:
    padded, npad = _prepad_for_spline_filter(input, mode, cval)
    filtered = spline_filter(padded, order, output=np.float64, mode=mode)
else:
    npad = 0
    filtered = input
mode = _ni_support._extend_mode_to_code(mode)
_nd_image.geometric_transform(filtered, None, coordinates, None, None,
                              output, order, mode, cval, npad, None, None)
```

> 说明：同样的装配块在 `affine_transform`（[L746-L748](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L746-L748)）、`geometric_transform`（[L360-L362](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L360-L362)）、`zoom`（[L859-L861](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L859-L861)）、`rotate`（[L614-L616](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_interpolation.py#L614-L616)）里几乎逐字重复——它们共享同一段「按需预填充 + 预滤波」逻辑。注意 `filtered` 强制写成 `np.float64`，是为了保证样条系数的精度。

#### 4.4.4 代码实践

**目标**：验证「手动 `spline_filter` + `prefilter=False`」能精确复现 `prefilter=True` 的结果（这是理解整套机制的试金石）。

**步骤**（用 `mode='mirror'`，因为它 `npad=0`，匹配最干净）：

```python
import numpy as np
from scipy import ndimage

a = np.array([0., 0., 0., 1., 1., 1.], dtype=float)
coords = np.array([[0.0, 0.5, 1.0, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]])  # 亚像素采样

# 路径 A：让函数自动预滤波
auto = ndimage.map_coordinates(a, coords, order=3, mode='mirror', prefilter=True)

# 路径 B：手动预滤波，然后关掉 prefilter
coeffs = ndimage.spline_filter(a, order=3, mode='mirror')
manual = ndimage.map_coordinates(coeffs, coords, order=3, mode='mirror', prefilter=False)

print("auto  =", np.round(auto, 4))
print("manual=", np.round(manual, 4))
print("equal ?", np.allclose(auto, manual))   # 预期 True
```

**预期**：`equal ? True`。因为路径 A 内部做的事（`mirror` 不补点，所以就是 `spline_filter` + `geometric_transform(prefilter=False)`）与路径 B 完全一致。

**进阶观察（振铃）**：把 `prefilter` 在路径 A 改成 `False` 再算一次：

```python
wrong = ndimage.map_coordinates(a, coords, order=3, mode='mirror', prefilter=False)
print("wrong =", np.round(wrong, 4))
```

- `auto`（正确）在 `x=2.5` 附近会出现 **超过 1 的过冲** 和 0 一侧的 **负值**——这是高阶样条精确还原 sharp 边缘时固有的振铃（Gibbs 现象），它正是 4.1 里「系数出现超调」在重建曲线上的体现。
- `wrong`（未预滤波）则把样本当成系数，重建出一条被 B 样条核平滑过的、**没有过冲**的斜坡，但它**不再精确经过原样本点**。

过冲的具体数值「待本地验证」，但「正确预滤波 → 出现振铃过冲；未预滤波 → 平滑但失真」这一对比是确定的。

#### 4.4.5 小练习与答案

**练习 1**：如果你已经对同一张图要连续做 `shift` 和 `rotate` 两次高阶插值，把两次的 `prefilter` 都设成 `True` 会怎样？怎样更高效？

> **答**：每次 `prefilter=True` 都会在内部重做一次 `spline_filter`，两次变换 = 两次预滤波 + 两次反变换（重建），会累积精度损失且浪费算力。更高效的做法是先手动 `coeffs = spline_filter(img, order=3)`，两次几何变换都用 `prefilter=False`，最后只在需要还原成「样本域」时再考虑反滤波。不过注意 `spline_filter` 不可逆地丢了一些信息边界处理，实战中仍需权衡。

**练习 2**：`map_coordinates` 里 `filtered` 为什么强制 `output=np.float64`，而不是沿用输入 dtype？

> **答**：样条系数的动态范围比样本大（会出现负值和超调，见 4.1），用低精度（如 float32）存系数会丢失有效数字，进而让插值结果失真。强制 float64 是为了在重采样前保留系数的精度。

## 5. 综合实践

把本讲三个模块串起来，做一个完整的「预滤波开关」对照实验，并亲手复现 `prefilter=True`。

```python
import numpy as np
from scipy import ndimage

# 1) 构造一张含 sharp 阶跃的 1D 信号
sig = np.zeros(16); sig[8:] = 1.0
xs  = np.linspace(0, 15, 200)            # 亚像素密采样
coords = xs.reshape(1, -1)

# 2) 三条曲线
y_true    = ndimage.map_coordinates(sig, coords, order=3, mode='mirror', prefilter=True)
y_nopre   = ndimage.map_coordinates(sig, coords, order=3, mode='mirror', prefilter=False)
coeffs    = ndimage.spline_filter(sig, order=3, mode='mirror')   # 手动预滤波
y_manual  = ndimage.map_coordinates(coeffs, coords, order=3, mode='mirror', prefilter=False)

# 3) 断言与观察
print("复现成功 ?", np.allclose(y_true, y_manual))               # 预期 True
print("过冲 max  :", round(float(y_true.max()), 4), " > 1 ?")   # 预期 > 1（振铃）
print("未滤波 max:", round(float(y_nopre.max()), 4))            # 预期 ≤ 1（平滑、无过冲）
```

任务要求：

1. 跑通上面脚本，确认 `复现成功 ? True`——这是本讲最核心的验证点（手动预滤波 = 自动预滤波）。
2. 解释为什么 `y_true` 会超过 1.0，而 `y_nopre` 不会（用 4.1 的「系数超调」和 4.4 的振铃来回答）。
3. 把 `mode` 换成 `'nearest'`，再用同样的「手动 `spline_filter` + `prefilter=False`」去复现 `prefilter=True`，观察是否还能 `allclose`——并联系 4.3 解释原因（提示：`nearest` 有 12 点填充，手动复现时也要补点才能精确匹配；若不补点会有微小差异，这就是「待本地验证」的地方）。

## 6. 本讲小结

- 高阶（`order>1`）B 样条插值要求先把 **样本** 转成 **样条系数**，这个反滤波过程叫预滤波；`order=0/1` 时系数等于样本，无需预滤波。
- `spline_filter1d` 是一维预滤波原子，0/1 阶短路拷贝，2–5 阶进入 C 内核 `_nd_image.spline_filter1d`；C 端用「增益 + 因果扫描 + 反因果扫描」的递归滤波，极点由 `get_filter_poles` 给出。
- `spline_filter` 是多维包装，靠可分离性沿各轴串联调用 `spline_filter1d`，并用 `input=output` 就地链式复用同一缓冲；它只接受 `order` 2–5。
- `_prepad_for_spline_filter` 只对 `nearest`/`grid-constant` 补 12 点（因为这两种边界在 C 端没有精确初始化）；12 来自主导极点 \(|z|^{12}\approx4\times10^{-5}\) 的精度考量。
- `map_coordinates` 等插值函数的 `prefilter=True`（默认）内部就是「`_prepad_for_spline_filter` → `spline_filter` → C 重采样」，因此可以用手动 `spline_filter` + `prefilter=False` 精确复现。
- 正确预滤波会让 sharp 边缘出现振铃过冲；不预滤波则曲线被平滑、不经过样本点——这正是 `prefilter` 参数存在的意义。

## 7. 下一步学习建议

- 下一讲 **u3-l2（map_coordinates：坐标映射插值）** 会用本讲准备好的「样条系数」，讲解如何在任意亚像素坐标上完成重采样，并讨论 `mode` 在插值中的细微差别（`constant` vs `grid-constant`、`wrap` vs `grid-wrap`）。
- 之后 **u3-l3（affine_transform）** 与 **u3-l4（geometric_transform / shift / zoom / rotate）** 都会复用本讲的预滤波装配块，建议学完后回头比较 `_interpolation.py` 中那五处几乎相同的 `if prefilter and order > 1:` 代码块。
- 想深入 C 端递归滤波细节，可读 [src/ni_splines.c](src/ni_splines.c) 的 `_init_causal_mirror` / `_init_anticausal_mirror` 等边界初值函数，这部分将在 **u6-l3（C 滤波/插值/样条内核）** 系统讲解。
