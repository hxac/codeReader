# 多维相关卷积、footprint 与 axes 展开

## 1. 本讲目标

学完本讲后，你应该能够：

- 读懂 `correlate` / `convolve` 两个公开函数的签名，知道它们的 `weights` 是一个**和输入同维数**的 N-D 数组，并能解释 `axes` 参数的作用。
- 跟着 `_correlate_or_convolve` 这一台「统一引擎」走完全流程：复数分派 → `axes` 规范化 → `weights` / `origin` 展开 → **卷积翻转** → `origin` 合法性校验 → 内存别名处理 → 调用 C 内核 `_nd_image.correlate`。
- 说清 `_expand_footprint` / `_expand_origin` / `_expand_mode` 这「展开三件套」分别在解决什么问题：当 `axes` 只是全部维度的**子集**时，把只覆盖子集的 `weights` / `origin` / `mode` 「补零」扩展到全部维度。
- 理解 N-D 卷积为什么要把 `weights` **沿每个轴都翻转一次**，以及为什么偶数长度的轴还要再额外 `origin -= 1`。

本讲是 u2-l1 的直接延续：u2-l1 讲的是「一根线上」的一维相关 / 卷积，本讲把它推广到「任意维度的邻域」。你会发现，N-D 版本没有新的 C 内核——它复用同一个 `_nd_image.correlate`，多出来的全部是 **Python 层的「把任意形状的邻域塞进 C 内核」的胶水代码**。

---

## 2. 前置知识

本讲默认你已经掌握 u2-l1 和 u1-l4 的内容。这里只做最简短的回顾与补位。

### 2.1 从一维到多维：邻域变成「方块」

在一维里，核 `[1, 3]` 是一条长度为 2 的线。到了二维，核就变成了一个小矩阵，例如一个 3×3 的「十字形」：

```
[[0, 1, 0],
 [1, 1, 1],
 [0, 1, 0]]
```

把这个小矩阵盖在图像某个像素上、逐格相乘再求和，就得到该像素的输出。多维相关 / 卷积的物理意义和一维完全一致，只是邻域从「一段线」变成了「一个 N-D 方块」。

> 一个核心事实：在 `correlate` / `convolve` 里，`weights` **本身就是**邻域的描述（它既给出邻域形状，又给出每格的权重）。这一点和 u2-l5 将要讲的秩滤波里的「布尔 `footprint`」是同一类东西——本讲你会看到，`_correlate_or_convolve` 甚至**直接把 `weights` 喂给了一个叫 `_expand_footprint` 的函数**。

### 2.2 相关 vs 卷积（N-D 版的一句话）

- **相关**：核不翻转，直接盖上去逐格相乘求和。
- **卷积**：先把核**沿每个轴都翻转 180°**（即 `W[::-1, ::-1, ...]`），再做相关。

对于「中心对称」的核（如上面的十字形），翻转后不变，相关 = 卷积。差异只在**非对称**核上才体现。

数学上，记输入为 \(I\)、核为 \(W\)、中心偏移由 `origin` 决定，那么（\(\mathbf{p}\) 为输出像素坐标，\(\mathbf{q}\) 遍历核内坐标）：

\[
\text{correlate:}\quad R(\mathbf{p})=\sum_{\mathbf{q}} I(\mathbf{p}+\mathbf{q}-\mathbf{c})\,W(\mathbf{q})
\]

\[
\text{convolve:}\quad C(\mathbf{p})=\sum_{\mathbf{q}} I(\mathbf{p}-\mathbf{q}+\mathbf{c})\,W(\mathbf{q})
\]

其中 \(\mathbf{c}\) 是核的中心索引。两者的差别正是 \(\mathbf{q}\) 前面的正负号——对应到代码里就是把 `weights` 沿每个轴翻转。

### 2.3 三个共享支撑工具（来自 u1-l4）

本讲会反复用到这三个已在 u1-l4 讲过的工具，这里只列作用：

- `_ni_support._check_axes(axes, ndim)`：把 `None` / 标量 / 序列统一成「合法、唯一、非负」的轴元组；`None` 表示「全部轴」。
- `_ni_support._normalize_sequence(x, rank)`：标量广播成长度 `rank` 的列表，序列则校验长度。
- `_ni_support._extend_mode_to_code(mode)`：把 mode 字符串翻译成 C 内核需要的整数码（0–6）。
- `_ni_support._get_output(output, input)`：处理 `output` 的三种形态（`None` / dtype / 已有数组）。

---

## 3. 本讲源码地图

本讲只涉及一个文件，但要在其中精读 **6 个函数**：

| 函数 | 行号 | 作用 |
|---|---|---|
| `correlate` | [_filters.py:1313-1379](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1313-L1379) | 公开入口：N-D 相关。仅把 `convolution=False` 转交引擎。 |
| `convolve` | [_filters.py:1383-1384](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1383-L1384) | 公开入口：N-D 卷积。仅把 `convolution=True` 转交引擎。 |
| `_correlate_or_convolve` | [_filters.py:1253-1309](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1253-L1309) | **统一引擎**：相关与卷积的全部真正逻辑都在这里。 |
| `_expand_origin` | [_filters.py:516-525](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L516-L525) | 把只覆盖 `axes` 子集的 `origin` 补 0 到全维。 |
| `_expand_footprint` | [_filters.py:528-540](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L528-L540) | 把只覆盖 `axes` 子集的 `weights`/`footprint` 插入尺寸为 1 的新轴，升到全维。 |
| `_expand_mode` | [_filters.py:543-552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L543-L552) | 把只覆盖 `axes` 子集的 `mode` 序列补 `'constant'` 到全维。 |

> 注意一个反直觉的事实：`_expand_mode` 虽然存在，但 `_correlate_or_convolve` **并不调用它**——相关 / 卷积函数明确拒绝「逐轴 mode 序列」。`_expand_mode` 是给 u2-l5 的秩 / 极值滤波用的。本讲会在 4.3 讲清这个区别。

---

## 4. 核心概念与源码讲解

### 4.1 公开入口：correlate 与 convolve

#### 4.1.1 概念说明

`correlate` 和 `convolve` 是 `scipy.ndimage` 暴露给用户的两个 N-D 滤波原语。它们承担的工作**极少**——只负责把参数原样打包，加上一个布尔标志 `convolution`，然后交给 `_correlate_or_convolve`。

这种「两个薄壳 + 一个厚引擎」的设计正是 u2-l1 里 `correlate1d` / `convolve1d` 共用同一个 C 内核思想的多维翻版：相关与卷积的差别只是一个「翻转核」的步骤，没必要写两份主体逻辑。

#### 4.1.2 核心流程

```
用户调用 correlate(input, weights, ...)
        │  convolution = False
        ▼
用户调用 convolve(input, weights, ...)
        │  convolution = True
        ▼
_correlate_or_convolve(..., convolution, axes)   ← 真正干活的地方
```

两者的参数集合完全相同：`input, weights, output=None, mode='reflect', cval=0.0, origin=0, *, axes=None`。唯一区别就是传给引擎的那个布尔值。

#### 4.1.3 源码精读

`correlate` 的函数体只有一行 `return`：

```python
# _filters.py:1378-1379 —— 相关：convolution=False
return _correlate_or_convolve(input, weights, output, mode, cval,
                              origin, False, axes)
```

`convolve` 的函数体同样只有一行，只是把 `False` 换成 `True`：

```python
# _filters.py:1497-1498 —— 卷积：convolution=True
return _correlate_or_convolve(input, weights, output, mode, cval,
                              origin, True, axes)
```

签名上唯一值得留意的是只关键字参数 `axes`（[_filters.py:1313-1314](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1313-L1314)）：

```python
def correlate(input, weights, output=None, mode='reflect', cval=0.0,
              origin=0, *, axes=None):
```

`axes=None` 表示「沿全部维度滤波」；若传 `axes=(0,)`，则只在第 0 轴上做邻域操作，其余轴「按原样透传」。这正是 4.3 节「展开三件套」要解决的问题。

> 读到这里你可能会问：`weights` 的形状到底该是什么？答案在签名文档里：`weights` 应当与 `input` **维数相同**（当 `axes=None` 时）。例如二维输入就要传二维 `weights`。如果 `axes` 是子集，`weights` 的维数则要等于 `len(axes)`——引擎内部会再把它展开到全维。

#### 4.1.4 代码实践

**实践目标**：用最经典的「十字形核」跑一次 `correlate`，直观感受 N-D 相关就是对一个邻域逐格相乘求和。

**操作步骤**：

```python
import numpy as np
from scipy.ndimage import correlate

a = np.arange(16).reshape(4, 4)
print(a)
# [[ 0  1  2  3]
#  [ 4  5  6  7]
#  [ 8  9 10 11]
#  [12 13 14 15]]

cross = np.array([[0, 1, 0],
                  [1, 1, 1],
                  [0, 1, 0]])

out = correlate(a, cross, mode='constant', cval=0.0)
print(out)
```

**需要观察的现象**：盯着输出里的 `[1, 1]` 位置（输入值是 5）。十字形核在这里盖住的 5 个格子是「自身 + 上 + 下 + 左 + 右」。

**预期结果**（手算）：`[1,1]` 处的输出 = \(5\)（自身）+ \(1\)（上 `a[0,1]`）+ \(9\)（下 `a[2,1]`）+ \(4\)（左 `a[1,0]`）+ \(6\)（右 `a[1,2]`）= **25**。由于 `[1,1]` 是内部点，`mode` 取 `'constant'` 或默认的 `'reflect'` 结果都一样。可运行脚本确认 `out[1, 1] == 25`。

> 这是 `correlate` 官方文档示例（5×5 图、十字核、`[2,2]` 处得 60）的缩小版，逻辑完全一致。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `cross` 换成 `np.ones((3, 3))`（3×3 全 1 核），`out[1, 1]` 会变成多少？

**答案**：全 1 核会把 3×3 邻域（含 4 个对角）全部相加。`[1,1]` 的 3×3 邻域是
```
0 1 2
4 5 6
8 9 10
```
求和 = **45**。与十字核的 25 相比，多出来的 20 正是 4 个对角（\(0+2+8+10=20\)）。这说明 `weights` 里某个位置填 0，等价于「该邻居不参与求和」——这就是「加权 footprint」的含义。

**练习 2**：`correlate(a, cross)` 与 `convolve(a, cross)` 的结果相同吗？为什么？

**答案**：相同。因为十字形核在 180° 翻转后形状不变（中心对称）。只有用**非对称**核时两者才会不同——这一点会在 4.2 节用代码验证。

---

### 4.2 统一引擎：_correlate_or_convolve

#### 4.2.1 概念说明

`_correlate_or_convolve`（[_filters.py:1253-1309](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1253-L1309)）是相关与卷积共用的「主体逻辑」。它的职责是把用户给的、形态各异的参数（标量 `origin`、子集 `axes`、可能复数的 `weights`……）**规整成 C 内核 `_nd_image.correlate` 能消化的固定形式**：一个连续的 float64 `weights`、一个长度等于 `input.ndim` 的 `origins` 列表、一个整数码 `mode`。

可以把这个引擎理解成一条流水线，每一站都在「把自由度收紧一点」，直到最后只剩一次 C 调用。

#### 4.2.2 核心流程

引擎的 9 个步骤，按代码顺序：

```
1. np.asarray(input / weights)
2. 复数输入或复数 weights？ ──是──▶ _complex_via_real_components 拆成实部虚部分别处理，提前 return
3. axes = _check_axes(axes, input.ndim)          # 规范成轴元组
4. weights = float64 化
5. weights = _expand_footprint(...)              # 子集 axes → 全维（见 4.3）
   origins = _expand_origin(...)                 # 同上
6. 校验 weights 的有效维数 == input.ndim
7. 若 convolution：沿每个轴翻转 weights，并修正 origins（关键！）
8. 逐轴校验 origin 合法性（_invalid_origin）
9. 处理 input/output 内存别名 → _extend_mode_to_code(mode) → _nd_image.correlate(...)
```

#### 4.2.3 源码精读

**第 1–2 步：复数分派。** 注意这里和 u2-l1 的一维版有一处微妙差别：

```python
# _filters.py:1257-1269 —— 复数分派
complex_input = input.dtype.kind == 'c'
complex_weights = weights.dtype.kind == 'c'
if complex_input or complex_weights:
    if complex_weights and not convolution:
        # As for np.correlate, conjugate weights rather than input.
        weights = weights.conj()
    ...
    return _complex_via_real_components(_correlate_or_convolve, input,
                                        weights, output, cval, **kwargs)
```

复数 `weights` **只在相关时**取共轭（`not convolution`）。这符合数学约定：相关定义里核要共轭，卷积定义里核不共轭。随后由 `_complex_via_real_components`（u2-l1 已详述）拆成至多 4 路实数卷积线性组合，绕过 C 内核只认实数的限制。命中此分支就**提前 return**，后面的流程只针对实数情形。

**第 3–6 步：规范 axes、展开 weights。**

```python
# _filters.py:1271-1281 —— 规范 axes、展开、维数校验
axes = _ni_support._check_axes(axes, input.ndim)
weights = np.asarray(weights, dtype=np.float64)

# expand weights and origins if num_axes < input.ndim
weights = _expand_footprint(input.ndim, axes, weights, "weights")
origins = _expand_origin(input.ndim, axes, origin)

wshape = [ii for ii in weights.shape if ii > 0]
if len(wshape) != input.ndim:
    raise RuntimeError(f"weights.ndim ({len(wshape)}) must match "
                       f"len(axes) ({len(axes)})")
```

要点：
- `_check_axes` 把 `axes=None` 变成 `tuple(range(ndim))`，把标量变成单元素元组，并校验唯一与非负。
- `_expand_footprint` 把只覆盖 `axes` 子集的 `weights` 升维到 `input.ndim`（细节见 4.3）。注意它第 4 个参数传的是字符串 `"weights"`，仅用于报错信息——这印证了「weights 在这里被当成 footprint 处理」。
- `wshape` 过滤掉尺寸为 0 的轴（边界保护），随后断言「展开后 weights 的有效维数」必须等于输入维数。这一步在 `axes` 覆盖全部维度时（不展开）才真正起校验作用：它抓住「用户传了维度不对的 weights」这一类错误。

**第 7 步：卷积翻转（本讲最重要的细节）。**

```python
# _filters.py:1282-1287 —— 卷积 = 翻转核 + 修正 origin
if convolution:
    weights = weights[tuple([slice(None, None, -1)] * weights.ndim)]
    for ii in range(len(origins)):
        origins[ii] = -origins[ii]
        if not weights.shape[ii] & 1:
            origins[ii] -= 1
```

这一段把 u2-l1 一维情况下的「翻转核 + origin 取反 + 偶数长度再减 1」推广到 N-D：

- `slice(None, None, -1)` 重复 `weights.ndim` 次，意思是**沿每一个轴都反转**，即把核旋转 180°。这正是 2.2 节公式里 \(\mathbf{q}\) 变号所要求的。
- 随后对**每一个轴**做和一维相同的 origin 修正：先取反；若该轴长度为偶数（`weights.shape[ii] & 1 == 0`），再额外减 1。

> 为什么偶数轴要再减 1？回忆 u2-l1：把一个长度为 \(L\) 的核反转后，「中心锚点」从原索引位置变了。奇数长度反转后中心格不动，所以 origin 只需取反；偶数长度反转后中心会错开一格，必须再 `-= 1` 把它拉回对齐。N-D 版本只是对每个轴独立做一次同样的判断。

翻转之后，**卷积就完全等价于「用翻转后的核做相关」**，于是后续可以共用同一段相关逻辑。

**第 8 步：逐轴 origin 合法性。**

```python
# _filters.py:1288-1292 —— 逐轴 origin 校验
for origin, lenw in zip(origins, wshape):
    if _invalid_origin(origin, lenw):
        raise ValueError('Invalid origin; origin must satisfy '
                         '-(weights.shape[k] // 2) <= origin[k] <= '
                         '(weights.shape[k]-1) // 2')
```

`_invalid_origin`（[_filters.py:483-484](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L483-L484)）就是 u2-l1 讲过的同一个函数：

```python
def _invalid_origin(origin, lenw):
    return (origin < -(lenw // 2)) or (origin > (lenw - 1) // 2)
```

只不过现在对每个轴各判一次。注意校验发生在**翻转之后**，所以卷积的 origin 取值范围与相关一致（都是 `-(L//2) ≤ o ≤ (L-1)//2`）。

**第 9 步：内存别名、mode 编码、调用 C 内核。**

```python
# _filters.py:1294-1308 —— 收尾
if not weights.flags.contiguous:
    weights = weights.copy()
output = _ni_support._get_output(output, input)
temp_needed = np.may_share_memory(input, output)
if temp_needed:
    # input and output arrays cannot share memory
    temp = output
    output = _ni_support._get_output(output.dtype, input)
if not isinstance(mode, str) and isinstance(mode, Iterable):
    raise RuntimeError("A sequence of modes is not supported")
mode = _ni_support._extend_mode_to_code(mode)
_nd_image.correlate(input, weights, output, mode, cval, origins)
if temp_needed:
    temp[...] = output
    output = temp
return output
```

三个要点：

1. **内存别名保护**：如果 `output` 和 `input` 共享内存（比如用户把 `input` 自身当 `output`），C 内核边读边写会出错。引擎于是另开一块临时 `output`，算完再拷回用户给的数组。
2. **拒绝逐轴 mode**：`if not isinstance(mode, str) and isinstance(mode, Iterable)` 显式抛错。这意味着 `correlate` / `convolve` **不支持** `mode=('reflect', 'nearest')` 这种逐轴序列——只能传单个 mode 字符串，作用于全部轴。这是它与秩 / 极值滤波（u2-l5）的一个实在区别。
3. **最终一次 C 调用**：`_nd_image.correlate(input, weights, output, mode, cval, origins)`。注意不论相关还是卷积，调用的都是同一个 C 函数 `correlate`——卷积的差别已在第 7 步通过翻转核「吸收」掉了。`origins` 是一个长度等于 `input.ndim` 的列表（每轴一个 origin）。

#### 4.2.4 代码实践

**实践目标**：用一个**非对称**核，亲眼看到卷积确实把核旋转了 180°，并验证 `convolve(a, w)` 与 `correlate(a, w 翻转)` 结果一致。

**操作步骤**：

```python
import numpy as np
from scipy.ndimage import correlate, convolve

a = np.arange(1, 17, dtype=float).reshape(4, 4)
w = np.array([[1., 2., 0.],      # 非对称核：右上角明显比左下角重
              [0., 0., 0.],
              [0., 0., 0.]])

cv = convolve(a, w, mode='constant', cval=0.0)
# 手动把核翻转 180°，再做相关
w_flipped = w[::-1, ::-1]
cr = correlate(a, w_flipped, mode='constant', cval=0.0)

print("convolve == correlate(翻转核) ?", np.array_equal(cv, cr))
```

**需要观察的现象**：脚本应打印 `True`，说明引擎内部的「翻转核」确实等价于手动 `w[::-1, ::-1]` 后做相关。

**预期结果**：`np.array_equal(cv, cr)` 为 `True`。

> 这个等价关系正是 [_filters.py:1282-1283](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1282-L1283) 那一行 `weights[tuple([slice(None, None, -1)] * weights.ndim)]` 的可运行注解。如果你把核换成对称的（如全 1），`convolve` 与 `correlate`（不翻转）也会相等——这就是练习 4.1.5/2 的结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_correlate_or_convolve` 里「复数 weights 取共轭」要加 `and not convolution` 这个条件？

**答案**：因为相关定义要求核取共轭（\(\overline{W}\)），而卷积定义里核不取共轭。所以只有相关（`convolution=False`）才执行 `weights.conj()`。这与 `np.correlate` / `np.convolve` 的约定一致。

**练习 2**：尝试 `correlate(a, w, mode=('reflect', 'nearest'))`，会发生什么？为什么？

**答案**：会抛 `RuntimeError: A sequence of modes is not supported`（[_filters.py:1302-1303](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1302-L1303)）。因为 `_correlate_or_convolve` 明确只接受**单个** mode 字符串，由它统一作用于所有轴。需要逐轴不同边界模式时，应改用秩 / 极值滤波（u2-l5），那里才会调用 `_expand_mode`。

---

### 4.3 axes 子集展开三件套

#### 4.3.1 概念说明

很多用户只想「沿某一个轴」做邻域操作。例如一张二维图，只想沿行方向（轴 1）做 3 点加权平均，列方向保持不动。这时候会写：

```python
correlate(img, weights=[1, 2, 1], axes=(1,))
```

问题来了：C 内核 `_nd_image.correlate` 期望的 `weights` 是一个**与 `input` 维数相同**的数组，`origins` 是一个**长度等于 `input.ndim`** 的列表。但用户给的 `weights` 是 1-D 的（维数 1），`origin` 是标量（长度 1），它们都只覆盖了 `axes=(1,)` 这一个轴。

「展开三件套」`_expand_origin` / `_expand_footprint` / `_expand_mode` 就是为这个落差服务的：**把只覆盖 `axes` 子集的参数，「补默认值」扩展到全部维度**，让 C 内核拿到形状一致的数据。

> 一句话直觉：`_expand_*` 系列做的事情，就是把「只在某些轴上有定义」的参数，在其余轴上插入「中性值」，使其在数学上等价、在形状上对齐。`origin` 的中性值是 0；`footprint/weights` 的中性值是「插入一个长度为 1 的新轴」（即该轴邻域只取自身）；`mode` 的中性值是 `'constant'`。

#### 4.3.2 核心流程

三个函数的形态高度一致，都遵循同一个模式：

```
若 len(axes) == input.ndim（全覆盖）：
    不做任何事，原样返回
若 len(axes) < input.ndim（子集）：
    把参数扩展到 input.ndim 维：
      - 先校验「子集参数的维数/长度 == len(axes)」
      - 再为「不在 axes 里的轴」补默认值
```

#### 4.3.3 源码精读

**`_expand_origin`（[_filters.py:516-525](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L516-L525)）**：把 `origin` 扩展为长度 `input.ndim` 的列表，未滤波轴补 0。

```python
def _expand_origin(ndim_image, axes, origin):
    num_axes = len(axes)
    origins = _ni_support._normalize_sequence(origin, num_axes)
    if num_axes < ndim_image:
        # set origin = 0 for any axes not being filtered
        origins_temp = [0,] * ndim_image
        for o, ax in zip(origins, axes):
            origins_temp[ax] = o
        origins = origins_temp
    return origins
```

逻辑：
1. `_normalize_sequence(origin, num_axes)`：`origin=0`（标量）→ `[0]*num_axes`；`origin=(1,2)` → 校验长度等于 `num_axes`。
2. 若子集，先造一个全 0 的 `ndim_image` 长列表，再把用户给的值**放回各自对应的轴位置**。例如 `ndim=2, axes=(1,), origin=0` → `[0, 0]`；`axes=(1,), origin=1` → `[0, 1]`。

**`_expand_footprint`（[_filters.py:528-540](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L528-L540)）**：给 `weights`/`footprint` 在「未滤波轴」上插入长度为 1 的新轴。

```python
def _expand_footprint(ndim_image, axes, footprint,
                      footprint_name="footprint"):
    num_axes = len(axes)
    if num_axes < ndim_image:
        if footprint.ndim != num_axes:
            raise RuntimeError(f"{footprint_name}.ndim ({footprint.ndim}) "
                               f"must match len(axes) ({num_axes})")

        footprint = np.expand_dims(
            footprint,
            tuple(ax for ax in range(ndim_image) if ax not in axes)
        )
    return footprint
```

逻辑：
1. 若子集，先校验 `footprint.ndim == num_axes`（即 weights 的维数必须等于你选的轴数）。`footprint_name` 用于把报错里的「footprint」换成调用方真正的参数名——`_correlate_or_convolve` 传的就是 `"weights"`，所以你看到的报错是 `weights.ndim ... must match len(axes)`。
2. `np.expand_dims` 在「不在 `axes` 里的轴」位置插入尺寸为 1 的新轴。例如 `ndim=2, axes=(0,), weights=[1,2,1]`（形状 `(3,)`）→ 在轴 1 处插入新轴 → 形状 `(3, 1)`。形状 `(3,1)` 的核在轴 1 上邻域只有 1 格（即自身），所以等价于「只在轴 0 上做 3 邻域操作」。

**`_expand_mode`（[_filters.py:543-552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L543-L552)）**：把「逐轴 mode 序列」补 `'constant'` 到全维。

```python
def _expand_mode(ndim_image, axes, mode):
    num_axes = len(axes)
    if not isinstance(mode, str) and isinstance(mode, Iterable):
        # set mode = 'constant' for any axes not being filtered
        modes = _ni_support._normalize_sequence(mode, num_axes)
        modes_temp = ['constant'] * ndim_image
        for m, ax in zip(modes, axes):
            modes_temp[ax] = m
        mode = modes_temp
    return mode
```

逻辑：
- **仅当 `mode` 是一个序列**（不是字符串）时才展开；否则（单个字符串）原样返回。
- 展开方式与 `_expand_origin` 同构：未滤波轴补 `'constant'`。

> ⚠️ 重要区别：`_correlate_or_convolve` 里**只调用了 `_expand_footprint` 和 `_expand_origin`，没有调用 `_expand_mode`**（见 [_filters.py:1275-1276](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1275-L1276)）。相反，它在 [_filters.py:1302-1303](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1302-L1303) 直接拒绝任何 mode 序列。`_expand_mode` 的真正用户是 u2-l5 的 `_rank_filter`（[_filters.py:1949](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1949)）。也就是说：相关 / 卷积「不支持逐轴 mode」，而秩 / 极值滤波「支持逐轴 mode」——这是两个家族的一个分水岭。

#### 4.3.4 代码实践

**实践目标**：用一个 4×4 数组验证「`axes` 子集下 weights 如何被展开到全维」，并证明 `correlate(a, [1,1,1], axes=(0,))` 等价于 `correlate1d(a, [1,1,1], axis=0)`。

**操作步骤**：

```python
import numpy as np
from scipy.ndimage import correlate, correlate1d
from scipy.ndimage._filters import _expand_footprint, _expand_origin

a = np.arange(16).reshape(4, 4)

# (1) 直接观察「展开」这件事：1-D weights 被插了一个长度为 1 的新轴
w1d = np.array([1.0, 1.0, 1.0])
print("展开前 shape:", w1d.shape)                         # (3,)
print("展开后 shape:", _expand_footprint(2, (0,), w1d).shape)  # (3, 1)
print("origin 展开  :", _expand_origin(2, (0,), 0))       # [0, 0]

# (2) axes=(0,) 的 N-D 调用 vs 对应的一维调用
nd   = correlate(a, [1, 1, 1], axes=(0,), mode='constant', cval=0.0)
oned = correlate1d(a, [1, 1, 1], axis=0,  mode='constant', cval=0.0)
print("N-D(axes=0) == 1-D(axis=0) ?", np.array_equal(nd, oned))
```

**需要观察的现象**：
- `_expand_footprint(2, (0,), w1d)` 把 `(3,)` 变成 `(3, 1)`——多出来的那个长度为 1 的轴就是「第 1 轴不滤波、邻域只取自身」。
- N-D 调用 `correlate(a, [1,1,1], axes=(0,))` 与一维 `correlate1d(a, [1,1,1], axis=0)` 结果逐元素相等。

**预期结果**：
- 展开后 shape 为 `(3, 1)`，origin 展开为 `[0, 0]`。
- `out[1, 1]` 处（轴 0 上的 3 点和）= `a[0,1] + a[1,1] + a[2,1]` = \(1 + 5 + 9 = 15\)（`mode='constant'`，`[1,1]` 是内部点）。
- `np.array_equal(nd, oned)` 为 `True`。

> 这个等价性正是 `_expand_footprint` 的设计目的：让 N-D 接口能用统一的 `_nd_image.correlate` 内核表达「只沿某几轴」的操作，而不必为「子集滤波」单独写一套代码。

#### 4.3.5 小练习与答案

**练习 1**：对二维输入调用 `correlate(a, [[1,1,1]], axes=(1,))`（注意 weights 是 1×3），`_expand_footprint` 会把它变成什么形状？语义是什么？

**答案**：会**报错**。`axes=(1,)`、`num_axes=1`，而 `[[1,1,1]]` 的 `ndim=2`。`_expand_footprint` 在做任何 `expand_dims` 之前，先在 [_filters.py:532-534](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L532-L534) 校验 `footprint.ndim != num_axes`，于是直接抛 `weights.ndim (2) must match len(axes) (1)`，根本走不到 `expand_dims`。**结论**：子集 `axes` 时，`weights` 的维数必须**严格等于** `len(axes)`；想沿轴 1 用 3 点核，应传一维 `[1,1,1]`（ndim=1），它会被展开成 `(1, 3)`。多嵌一层括号变成二维就会报错。

**练习 2**：`correlate` 里 `_expand_mode` 没有被调用，那 `_expand_mode` 存在的意义是什么？

**答案**：它是给 `_rank_filter`（以及极值滤波）家族准备的。那一族函数（u2-l5）允许用户传**逐轴** mode 序列，例如 `mode=('reflect', 'nearest')`，此时 `_expand_mode` 把它补成全维。相关 / 卷积因为显式拒绝了 mode 序列，所以用不到它——但函数本身是模块级的公共工具，被多个滤波家族共享。

---

## 5. 综合实践

把本讲的三个最小模块（公开入口、统一引擎、axes 展开）串起来，完成下面这个「逆向还原」任务。

**任务**：下面这段代码用 N-D `correlate` 做了一次「十字形邻域求和」：

```python
import numpy as np
from scipy.ndimage import correlate

a = np.arange(16).reshape(4, 4)
cross = np.array([[0,1,0],[1,1,1],[0,1,0]])
result_nd = correlate(a, cross, mode='constant', cval=0.0)
```

请完成：

1. **等价 footprint**：用「全 1 的 3×3 核」也调用 `correlate`，比较两者在 `[1,1]` 处的差值，并解释这个差值来自哪些格子（对应 4.1.4 / 练习 4.1.5 的结论）。
2. **等价 size**：`correlate` 没有 `size` 参数，但「全 1 的 3×3 核」等价于在一个支持 `size` 的滤波函数里设 `size=(3,3)`。请用 `minimum_filter(a, size=(3,3), mode='constant', cval=0.0)` 计算 3×3 邻域最小值，并指出它的邻域集合与「全 1 的 3×3 correlate」**完全相同**（只是一个求和、一个求最小）——由此体会 `weights` / `footprint` / `size` 三者描述的是同一个「邻域」概念。
3. **axes 子集**：计算 `correlate(a, [1,1,1], axes=(0,), mode='constant', cval=0.0)`，手算 `[1,1]` 的值，并与 `correlate1d(a, [1,1,1], axis=0)` 比较是否相等（对应 4.3.4 的结论）。
4. **画调用链**：为第 3 步的调用画一条从 `correlate` → `_correlate_or_convolve` → `_expand_footprint` / `_expand_origin` → `_nd_image.correlate` 的调用链，并在每一步标注「数据形状如何变化」（例如 weights 从 `(3,)` → `(3,1)`，origins 从标量 → `[0,0]`）。

**预期结果汇总**（建议本地运行确认）：

| 调用 | `[1,1]` 处的值 | 说明 |
|---|---|---|
| `correlate(a, cross)` | 25 | 自身 + 上下左右 4 邻居 |
| `correlate(a, np.ones((3,3)))` | 45 | 上述再 + 4 个对角 |
| `minimum_filter(a, size=(3,3))` | 0 | 同一个 3×3 邻域里的最小值 |
| `correlate(a, [1,1,1], axes=(0,))` | 15 | 轴 0 方向 3 点和 |

---

## 6. 本讲小结

- `correlate` 与 `convolve` 是两个**极薄的公开壳**，差别只在一个布尔标志 `convolution`；所有真正逻辑都在 `_correlate_or_convolve` 里。
- 引擎把参数规整成 C 内核 `_nd_image.correlate` 需要的固定形式：连续 float64 `weights`、长度为 `input.ndim` 的 `origins` 列表、单个整数码 `mode`。
- **N-D 卷积 = 沿每个轴翻转核（`weights[::-1,::-1,...]`）+ 逐轴 origin 取反 + 偶数长度轴再 `-=1`**，之后与相关共用同一份代码、同一个 C 内核。
- `_expand_footprint` / `_expand_origin` 解决「`axes` 是子集」时形状不对齐的问题：给未滤波轴插入「中性值」（长度 1 的新轴 / origin 0）。
- `_expand_mode` 虽然存在，但相关 / 卷积**不用它**——这一族函数显式拒绝逐轴 mode 序列；它真正的用户是 u2-l5 的秩 / 极值滤波。
- 在 `correlate` / `convolve` 中，`weights` 本身就是「加权的 footprint」：权为 0 的格子不参与求和，等价于布尔 footprint 里的 `False`。

---

## 7. 下一步学习建议

- **继续滤波单元**：下一讲 u2-l3（高斯与均匀平滑滤波）会把本讲的「自定义 `weights`」换成「自动生成的高斯 / 均匀核」，并展示如何通过反复调用 `correlate1d` 实现「可分离滤波」。你会发现 `gaussian_filter` 在内部就是沿每个轴各调一次 `correlate1d`——本讲的 `axes` 子集思想在那里被用到极致。
- **对比秩滤波**：学完 u2-l5 后，回头比较 `_correlate_or_convolve` 与 `_rank_filter`（[_filters.py:1928](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1928)），重点看 `_rank_filter` 调用了 `_expand_mode` 而 `_correlate_or_convolve` 没有——这解释了「为何只有秩 / 极值滤波支持逐轴 mode」。
- **下探 C 内核**：若想了解 `_nd_image.correlate` 这一行的 C 端实现，可跳到 u6-l2（C 端迭代器、行缓冲与边界扩展），看 `NI_FilterIterator` 如何遍历 N-D 邻域、`NI_ExtendLine` 如何实现 7 种边界模式。
