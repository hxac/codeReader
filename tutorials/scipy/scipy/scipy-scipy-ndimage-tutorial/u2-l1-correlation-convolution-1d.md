# 一维相关与卷积基础

## 1. 本讲目标

学完本讲后，你应该能够：

- 用自己的话说清「相关（correlation）」和「卷积（convolution）」的数学差异，并知道 `scipy.ndimage` 是如何用**同一个 C 内核**实现两者的。
- 读懂 `correlate1d` / `convolve1d` 的参数校验顺序：`dtype` → `weights` 维度 → `axis` → `origin` 合法性 → `mode` 编码 → 调用 C 内核。
- 解释 `origin` 参数的几何含义、合法取值范围，以及为什么**偶数长度**的核需要额外的 origin 修正。
- 理解为什么复数输入会被拆成「实部 / 虚部」四路实数卷积再线性组合（`_complex_via_real_components`）。

本讲是整个「滤波」单元（u2）的地基。后面所有二维 / 多维滤波、高斯 / 微分 / 秩滤波，最终都会回落到本讲讲的这一行 C 调用：`_nd_image.correlate1d(...)`。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。如果你已经熟悉，可以快速跳过。

### 2.1 什么是「滑动窗口加权求和」

无论相关还是卷积，本质都是同一件事：拿一个长度为 \(L\) 的小权重向量 \(W\)（叫**核 / kernel / weights**），沿着信号的每个位置 \(i\) 滑过去，在每个位置把核与信号的一段邻域**逐项相乘再求和**，得到输出 \(y[i]\)。

> 关键差别只在于「核的左右是否要翻转」「核的哪一格对齐当前像素」。这两个细节决定了你做的是相关还是卷积。

### 2.2 边界怎么办（mode）

当核滑到数组边缘时，核的一部分会「伸出」数组之外。`mode` 参数决定用什么值来填补这些不存在的位置。`scipy.ndimage` 默认 `mode="reflect"`（半样本对称：边缘样本被重复一次再镜像）。本讲的所有手算示例都用 `reflect`。

> 关于 7 种 mode 字符串如何被编码成 C 内核需要的整数码，已经在 u1-l4 详细讲过（`_extend_mode_to_code`）。本讲只关心「mode 字符串 → 整数码」这一步会发生。

### 2.3 相关 vs 卷积（一句话）

- **相关**：核不翻转，直接滑动相乘求和。
- **卷积**：先把核**左右翻转**，再做相关。

对于**对称**核（如 `[1,2,1]`），翻转后不变，所以相关 = 卷积。差异只在非对称核（如 `[1,3]`）上才体现出来。

---

## 3. 本讲源码地图

本讲只深入一个文件，但会触碰到它依赖的两个支撑点：

| 文件 | 作用 | 本讲用到什么 |
|------|------|--------------|
| [_filters.py](_filters.py) | 滤波功能域的全部 Python 包装 | `correlate1d`、`convolve1d`、`_invalid_origin`、`_complex_via_real_components` |
| [_ni_support.py](_ni_support.py) | 跨功能域的共享支撑工具（u1-l4 讲过） | `_get_output`、`_extend_mode_to_code` |
| [src/nd_image.c](src/nd_image.c) | 真正干活的 C 扩展 | `Py_Correlate1D`（被 `_nd_image.correlate1d` 调到） |

调用链一览（自上而下）：

```
convolve1d  ──翻转 weights / 调整 origin──▶  correlate1d
                                                    │
                                  复数? ──是──▶ _complex_via_real_components
                                                    │ (拆成 4 路实数调用)
                                                    ▼
                                  _invalid_origin 校验 + _get_output 备输出
                                                    │
                                                    ▼
                                  _extend_mode_to_code(mode) → 整数码
                                                    │
                                                    ▼
                                  _nd_image.correlate1d(...)   ← C 内核 Py_Correlate1D
```

---

## 4. 核心概念与源码讲解

### 4.1 correlate1d：一维相关

#### 4.1.1 概念说明

`correlate1d` 是整个滤波子包最底层的「原子操作」。它在数组的**指定轴**上，把每一条线（1-D 序列）与给定的 `weights` 做相关。二维或多维数组的每一行 / 每一列，都会被当作一条独立的 1-D 序列来处理，彼此互不影响。

数学上，对长度为 \(L\) 的核 \(W\)、origin \(o\)、扩展后的信号 \(x_\text{ext}\)，输出为：

\[
y[i] \;=\; \sum_{j=0}^{L-1} W[j]\cdot x_\text{ext}\bigl[i + j - (\lfloor L/2 \rfloor + o)\bigr]
\]

也就是说，`origin` 等价于把核的「锚点」从默认的 \(\lfloor L/2\rfloor\) 偏移到 \(\lfloor L/2\rfloor + o\)。可以推出 \(y_o[i] = y_0[i - o]\)：**origin 为正时，整体结果向右平移**，这与官方文档对 `origin` 的描述一致。

#### 4.1.2 核心流程

`correlate1d` 的函数体几乎是一份「参数校验清单」，最后才把活交给 C：

```
1. np.asarray(input) / np.asarray(weights)
2. 若 input 或 weights 是复数：
     - weights 取共轭（对齐 np.correlate 的复数约定）
     - 委托给 _complex_via_real_components（见 4.4），提前 return
3. _get_output(output, input)              ← 准备输出数组（u1-l4）
4. weights 强制转 float64；校验 weights 必须是一维且非空
5. axis = normalize_axis_index(axis, input.ndim)
6. _invalid_origin(origin, len(weights))   ← 校验 origin 合法性（见 4.3）
7. mode = _extend_mode_to_code(mode)       ← 字符串 → 整数码（u1-l4）
8. _nd_image.correlate1d(input, weights, axis, output, mode, cval, origin)  ← C 内核
9. return output
```

注意第 2 步：**只要输入或核里有一个是复数，就走完全不同的分支并提前返回**，根本不会执行第 3 步之后的实数路径。这是理解「复数为何拆开处理」的入口（详见 4.4）。

#### 4.1.3 源码精读

函数签名与文档串（注意 `@_ni_docstrings.docfiller` 装饰器会把 `%(mode_reflect)s` 等占位符替换成共享文档，这是 u1-l4 讲过的 `docfiller` 机制）：

[`correlate1d` 的定义与签名] [_filters.py:555-584](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L555-L584) —— 函数头、参数文档与 docstring 示例。

复数分支（提前 return，跳过实数路径）：

[`correlate1d` 复数分支] [_filters.py:585-596](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L585-L596) —— `complex_weights` 时先 `weights.conj()`，然后委托 `_complex_via_real_components`。

实数分支（参数校验 + C 调用）：

[`correlate1d` 实数分支] [_filters.py:598-612](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L598-L612) —— `_get_output` 备输出、`weights` 转 float64 并校验一维、`normalize_axis_index`、`_invalid_origin`、`_extend_mode_to_code`，最后 `_nd_image.correlate1d(...)`。

被调用的 C 入口（仅作锚点，深入留到 u6）：

[C 端 `Py_Correlate1D` 包装函数] [src/nd_image.c:177](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L177) 与 [methods[] 分发表中的映射条目] [src/nd_image.c:1326](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1326) —— Python 名 `"correlate1d"` 通过这张表映射到 C 函数 `Py_Correlate1D`。

#### 4.1.4 代码实践

**实践目标**：用手算验证 `correlate1d` 的「锚点在 \(\lfloor L/2\rfloor\)」这一约定。

**操作步骤**：

1. 运行下面这段脚本（结果取自函数 docstring，已验证）：

```python
# 示例代码
from scipy.ndimage import correlate1d
import numpy as np

x = np.array([2, 8, 0, 4, 1, 9, 9, 0])
print(correlate1d(x, weights=[1, 3]))
# 期望输出: [ 8 26  8 12  7 28 36  9]
```

2. 手算 `out[1] = 26`：核 `[1, 3]`，\(L=2\)，`origin=0`，锚点 \(\lfloor L/2\rfloor = 1\)。`reflect` 模式下 \(x_\text{ext}[-1] = x[0] = 2\)。

\[
y[1] = W[0]\cdot x_\text{ext}[1+0-1] + W[1]\cdot x_\text{ext}[1+1-1] = 1\cdot x[0] + 3\cdot x[1] = 1\cdot2 + 3\cdot8 = 26
\]

**需要观察的现象**：`W[1]`（值 3）对齐到当前像素 `x[1]=8`，`W[0]`（值 1）对齐到左邻 `x[0]=2`。

**预期结果**：脚本输出 `[ 8 26  8 12  7 28 36  9]`，与 docstring 一致。

#### 4.1.5 小练习与答案

**练习 1**：上例中 `out[0] = 8`，请用 `reflect` 模式的边界扩展解释它为什么等于 8。

> **答案**：`out[0] = W[0]·x_ext[−1] + W[1]·x_ext[0]`。`reflect` 下 `x_ext[−1] = x[0] = 2`，所以 `1·2 + 3·2 = 8`。

**练习 2**：如果把 `weights` 换成对称的 `[2, 2]`，`correlate1d` 的结果会怎样？这说明了什么？

> **答案**：核对称时翻转不变，结果就是把每个邻域 `(左, 当前)` 加权 `(2,2)` 求和。这也预示着：**对称核下相关与卷积结果相同**（见 4.2）。

---

### 4.2 convolve1d：一维卷积

#### 4.2.1 概念说明

`convolve1d` 在 `scipy.ndimage` 里**没有独立的 C 内核**。它只是一层薄薄的数学变换：把卷积问题**改写成一个等价的相关问题**，然后直接调用 `correlate1d`。这正是「C 层比 Python 公开层更聚合」这一架构特点的最小例证（参见 u1-l2）。

卷积与相关的差别，可以浓缩成三步变换：

1. **翻转核**：`weights = weights[::-1]`
2. **origin 取反**：`origin = -origin`
3. **偶数长度修正**：若 \(L\) 为偶数，再 `origin -= 1`

#### 4.2.2 核心流程

`convolve1d` 的全部逻辑只有 8 行（不含文档）：

```
1. weights = np.asarray(weights)
2. weights = weights[::-1]          # 翻转核
3. origin  = -origin                # origin 取反
4. if not (L & 1):                  # 偶数长度
        origin -= 1                 #   额外修正
5. if weights 是复数:
        weights = weights.conj()    # 预先共轭，抵消 correlate1d 内部的共轭
6. return correlate1d(input, weights, axis, output, mode, cval, origin)
```

**为什么偶数长度要多减 1？** 偶数长度的核没有「真正的中心样本」——中心落在两个样本之间。翻转 + 取反之后，锚点会落在「偏右半格」的位置；`origin -= 1` 把它拉回「偏左半格」，使得卷积结果在几何上保持与奇数核一致的居中方式。

#### 4.2.3 源码精读

[`convolve1d` 的实现] [_filters.py:645-653](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L645-L653) —— 翻转、取反、偶数修正、复数预共轭，最后 `return correlate1d(...)`。注意它**没有**像 `correlate1d` 那样调用 `_get_output` / `_extend_mode_to_code`——这些事都由被调用的 `correlate1d` 统一完成。

复数预共轭的注释 [`_filters.py:650-652`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L650-L652) 解释了为何这里要 `.conj()`：因为 `correlate1d` 的复数分支（4.1.3）会对 weights 再共轭一次，这里预先共轭正好抵消，保证卷积的复数语义正确。

#### 4.2.4 代码实践

**实践目标**：用 `weights=[1, 3]`（非对称偶数核）亲眼看到「卷积 ≠ 相关」，并理解 origin 自动调整。

**操作步骤**：

```python
# 示例代码
from scipy.ndimage import correlate1d, convolve1d
import numpy as np

x = np.array([2, 8, 0, 4, 1, 9, 9, 0])
print("correlate1d:", correlate1d(x, weights=[1, 3]))  # [ 8 26  8 12  7 28 36  9]
print("convolve1d: ", convolve1d(x, weights=[1, 3]))   # [14 24  4 13 12 36 27  0]
```

手算 `convolve1d` 的 `out[0] = 14`：`convolve1d` 内部把 `[1,3]` 翻转成 `[3,1]`，origin 从 0 → 0 →（偶数修正）`-1`，于是 `correlate1d` 的锚点为 \(\lfloor L/2\rfloor + o = 1 + (-1) = 0\)：

\[
y[0] = W'[0]\cdot x_\text{ext}[0] + W'[1]\cdot x_\text{ext}[1] = 3\cdot2 + 1\cdot8 = 14
\]

**需要观察的现象**：`correlate1d` 里权重 3 乘的是 `x[i]`（当前像素）；`convolve1d` 里权重 3 乘的是 `x_ext[i]`（翻转后变成乘当前像素），但因为锚点从 1 移到了 0，两个结果完全不同。

**预期结果**：`correlate1d` 得 `[ 8 26  8 12  7 28 36  9]`，`convolve1d` 得 `[14 24  4 13 12 36 27  0]`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `correlate1d(x, [1,3])` 的 `out[1]=26`，而 `convolve1d(x, [1,3])` 的 `out[1]=24`？请用「翻转 + origin 修正」解释。

> **答案**：`convolve1d` 把核翻成 `[3,1]` 并把 origin 改成 `-1`（锚点 0）。`out[1] = 3·x[1] + 1·x[2] = 3·8 + 1·0 = 24`。核被翻转、且锚点左移了一格，所以乘的对象和 `correlate1d` 不同。

**练习 2**：用对称核 `[1, 2, 1]`（奇数长度）分别调用两个函数，比较结果。

> **答案**：核对称且为奇数长度，翻转后仍是 `[1,2,1]`，origin 为 0 不需要修正，因此 `correlate1d` 与 `convolve1d` 结果**完全相同**。这印证了「对称核 → 相关 = 卷积」。

---

### 4.3 _invalid_origin 与 origin 的合法范围

#### 4.3.1 概念说明

`origin` 控制「核的哪一格对齐当前像素」。它不能随便取：如果偏移太大，核会**整个滑出**邻域，失去意义。`_invalid_origin` 就是用来判定某个 `origin` 是否越界的工具函数。它只有一行，却被 `correlate1d`、`_correlate_or_convolve` 等多处复用，是一个名副其实的共享小工具。

#### 4.3.2 核心流程

合法范围由核长 \(L\) 决定：

\[
-\bigl\lfloor L/2 \bigr\rfloor \;\le\; o \;\le\; \bigl\lfloor (L-1)/2 \bigr\rfloor
\]

| 核长 \(L\) | 合法 origin | 默认 (0) 是否合法 |
|-----------|-------------|-------------------|
| 1 | {0} | 是 |
| 2（偶） | {−1, 0} | 是 |
| 3 | {−1, 0, 1} | 是 |
| 4（偶） | {−2, −1, 0} | 是 |
| 5 | {−2, −1, 0, 1, 2} | 是 |

注意偶数长度核的合法 origin **不对称**（负侧比正侧多一个），这正是 4.2 里「偶数核中心在两样本之间」的体现。

#### 4.3.3 源码精读

[`_invalid_origin` 工具函数] [_filters.py:483-484](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L483-L484) —— 返回 `True` 表示 origin 非法。两个边界正是上式。

它的调用点（出错信息里直接写出了合法区间）：

[`correlate1d` 中对 origin 的校验] [_filters.py:605-608](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L605-L608) —— 非法时抛出 `ValueError`，提示合法区间。

#### 4.3.4 代码实践

**实践目标**：体会 origin 对结果的「平移」效果，以及越界会报错。

**操作步骤**：

```python
# 示例代码
from scipy.ndimage import correlate1d
import numpy as np

x = np.array([2, 8, 0, 4, 1, 9, 9, 0], dtype=float)
w = [1, 0, 0]   # 长度 3，合法 origin ∈ {-1,0,1}
print("origin=-1:", correlate1d(x, w, origin=-1))
print("origin= 0:", correlate1d(x, w, origin=0))
print("origin=+1:", correlate1d(x, w, origin=1))

# 越界尝试（取消注释会抛 ValueError）
# correlate1d(x, w, origin=2)   # 2 > (3-1)//2 = 1，非法
```

**需要观察的现象**：权重 `[1,0,0]` 相当于「取锚点那一格」。`origin=0` 时锚点在中间格 → 输出≈原数组（边界受 reflect 影响）；`origin=+1` 时锚点右移 → 输出整体**向右平移一格**；`origin=-1` 则向左平移。

**预期结果**：三条输出是同一个数组左右平移后的版本；`origin=2` 会抛出 `ValueError: Invalid origin...`。具体数值**待本地验证**（取决于 reflect 边界细节）。

#### 4.3.5 小练习与答案

**练习 1**：对 `weights=[1, 3]`（\(L=2\)），`origin=-1` 合法吗？`origin=1` 呢？

> **答案**：合法区间是 \(-1 \le o \le 0\)。所以 `origin=-1` 合法，`origin=1` 非法（会抛 `ValueError`）。

**练习 2**：4.2 里 `convolve1d` 对 `[1,3]` 算出的内部 origin 是 `-1`。用本节的合法范围验证它确实在区间内。

> **答案**：\(L=2\)，合法 origin ∈ {−1, 0}，`-1` 在内，所以不会触发 `_invalid_origin`，调用合法。

---

### 4.4 _complex_via_real_components：复数输入的实部分解

#### 4.4.1 概念说明

C 内核 `_nd_image.correlate1d` 只认**实数**。但用户可能传入复数数组或复数核。`scipy.ndimage` 的解决办法不是去给 C 内核加复数支持，而是在 Python 层把一次复数相关**拆成至多 4 次实数相关**，再线性组合回去。这种「复数运算 = 实部分量的线性组合」是一个经典且优雅的技巧。

复数乘法 \((a+bi)(c+di) = (ac - bd) + (ad + bc)i\) 是它的数学基础：实部和虚部各自是实数乘积的线性组合。

#### 4.4.2 核心流程

设 \(x = x_r + i\,x_i\)，\(W = W_r + i\,W_i\)，相关 / 卷积是线性的，所以可以逐分量计算：

\[
\begin{aligned}
\mathrm{Re}(y) &= (x_r * W_r) \;-\; (x_i * W_i) \\
\mathrm{Im}(y) &= (x_r * W_i) \;+\; (x_i * W_r)
\end{aligned}
\]

代码按 `input` / `weights` 是否为复数分三种情形：

```
若 input 复 且 weights 复：  4 次实数调用（RR, II, RI, IR）       ← 完整公式
若只 input 复：               2 次（x_r*W → real,  x_i*W → imag）
若只 weights 复：             2 次（x*W_r → real,  x*W_i → imag）
                              并禁止「实输入 + 复 cval」
```

每一路都把对应实部分量喂给同一个 `func`（即 `correlate1d` 或 `_correlate_or_convolve`），写入 `output.real` 或 `output.imag`。其中 `RR`、`RI` 直接用 `output=` 复用输出缓冲，`II`、`IR` 用 `output=None` 临时分配再 `+=` / `-=`，避免覆盖。

#### 4.4.3 源码精读

[`_complex_via_real_components` 实现] [_filters.py:487-513](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L487-L513) —— 三个分支。重点看 `complex_input and complex_weights` 分支（491-501 行）：`output.real = RR - II`，`output.imag = RI + IR`，正是上面的公式。

`correlate1d` 复数分支中对 weights 的预先共轭 [`_filters.py:590-592`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L590-L592)：这与 `np.correlate` 的复数约定一致（相关时共轭第二个操作数）。`convolve1d` 会预先再共轭一次来抵消（4.2.3）。

#### 4.4.4 代码实践

**实践目标**：验证「复数卷积 = 4 路实数卷积的线性组合」确实成立。

**操作步骤**：

```python
# 示例代码
from scipy.ndimage import convolve1d
import numpy as np

x = np.array([2+1j, 8-2j, 0+0j, 4+3j, 1-1j])
w = np.array([1+1j, 3-1j])
y = convolve1d(x, w)
print("复数结果:", y)

# 手动用实部分量复现（卷积=翻转核的相关，故用 convolve1d 复现实部分量）
xr, xi = x.real, x.imag
wr, wi = w.real, w.imag
real_part = convolve1d(xr, wr) - convolve1d(xi, wi)
imag_part = convolve1d(xr, wi) + convolve1d(xi, wr)
print("手动复现:", real_part + 1j*imag_part)
```

**需要观察的现象**：两种方式得到的结果**逐元素相等**。

**预期结果**：`复数结果` 与 `手动复现` 完全一致，说明 `_complex_via_real_components` 的 4 路分解正确。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_complex_via_real_components` 里计算 `II` 和 `IR` 两路时用 `output=None`，而不是直接写入 `output.real` / `output.imag`？

> **答案**：因为 `output.real` / `output.imag` 已经被 `RR` / `RI` 两路写入了初值（分别要 `-= II` 和 `+= IR`）。如果直接覆盖，就会丢掉前两路的贡献。用 `output=None` 拿到独立的临时数组，再 `+=` / `-=` 累加进去。

**练习 2**：当 `weights` 是复数、`input` 是实数时，代码为何要求 `cval` 也必须是实数？

> **答案**：见 [_filters.py:508-510](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L508-L510)。此时两路实数调用共享同一个实数 `input`，边界填充值 `cval` 只有一个实数通道；若允许复数 `cval`，实部 / 虚部边界无法分别传给两路调用，语义会自相矛盾，故直接禁止。

---

## 5. 综合实践

把本讲的四个最小模块串起来，完成下面这个贯穿任务。

**任务**：用数组 `x = [2, 8, 0, 4, 1, 9, 9, 0]` 与核 `weights=[1, 3]`（偶数长度），分别计算 `correlate1d` 与 `convolve1d`，手算验证两者差异；再用 `weights=[1, 2, 3]`（奇数长度）观察 origin 的自动调整。

**步骤**：

1. **运行对照**：

```python
# 示例代码
from scipy.ndimage import correlate1d, convolve1d
import numpy as np

x = np.array([2, 8, 0, 4, 1, 9, 9, 0])

# (A) 偶数长度核 [1,3]
print("A correlate:", correlate1d(x, [1, 3]))   # [ 8 26  8 12  7 28 36  9]
print("A convolve: ", convolve1d(x, [1, 3]))    # [14 24  4 13 12 36 27  0]

# (B) 奇数长度核 [1,2,3]
print("B correlate:", correlate1d(x, [1, 2, 3]))
print("B convolve: ", convolve1d(x, [1, 2, 3]))
```

2. **手算 (A) 的差异**：参照 4.1.4 与 4.2.4，`out_A_correlate[1] = 1·x[0]+3·x[1] = 26`；`out_A_convolve[1] = 3·x[1]+1·x[2] = 24`。差异来自核翻转 + 偶数 origin 修正。

3. **解释 (B) 的 origin 自动调整**：对 `[1,2,3]`（\(L=3\)，奇数），`convolve1d` 内部把它翻转为 `[3,2,1]`，origin 取反得 0，因为 `3 & 1 = 1`（奇数），所以 **不再** 触发 `origin -= 1`。于是 `convolve1d(x,[1,2,3])` 等价于 `correlate1d(x, [3,2,1], origin=0)`。

4. **加日志验证内部 origin**（可选，源码阅读型实践）：临时在 `convolve1d` 的 `return correlate1d(...)` 之前 `print(f"flipped={weights}, origin={origin}")`，分别对 `[1,3]` 和 `[1,2,3]` 调用，确认前者 origin=-1、后者 origin=0。**注意：这是为了理解而做的临时修改，验证后请还原，不要提交对源码的改动。**

**预期结果**：

- (A) 两组结果**不同**（偶数、非对称核 → 相关 ≠ 卷积）。
- (B) 你会发现 `[1,2,3]` 翻转成 `[3,2,1]`、origin 保持 0；若改用对称核 `[1,2,1]`，则翻转后仍是 `[1,2,1]`，`correlate1d` 与 `convolve1d` 结果会**完全相同**。

> 若本地未安装可运行的 SciPy，步骤 1–3 的数值结果可对照官方 docstring（`correlate1d` / `convolve1d` 的示例）；步骤 4 的日志输出**待本地验证**。

---

## 6. 本讲小结

- `correlate1d` 是滤波子包的**原子操作**：一条线与一维核做滑动加权求和，锚点为 \(\lfloor L/2\rfloor + \text{origin}\)，最终落到 C 内核 `_nd_image.correlate1d`。
- `convolve1d` **没有独立内核**，它通过「翻转核 + origin 取反 + 偶数长度修正」把卷积改写成相关，复用 `correlate1d`。
- `origin` 控制 anchor 偏移，合法范围由 `_invalid_origin` 判定：\(-\lfloor L/2\rfloor \le o \le \lfloor(L-1)/2\rfloor\)；origin 为正使结果整体右移。
- 复数输入走 `_complex_via_real_components`：把一次复数相关拆成至多 4 次实数相关，按 \(\mathrm{Re}=RR-II,\ \mathrm{Im}=RI+IR\) 线性组合，绕过 C 内核只认实数的限制。
- 参数校验有固定顺序（dtype 复数判断 → weights 维度 → axis → origin → mode 编码），理解这条流水线后，后续高阶滤波函数的预处理都是它的变体。

---

## 7. 下一步学习建议

- **下一讲 u2-l2（多维相关卷积、footprint 与 axes 展开）**：会把 `correlate1d` 推广到 N-D 的 `correlate` / `convolve`，核心是 `_correlate_or_convolve` 与 `_expand_footprint` / `_expand_origin` / `_expand_mode`。建议先复习本讲的 origin 校验，因为多维版本对每一维都会调用 `_invalid_origin`。
- **延伸阅读**：直接打开 [_filters.py](_filters.py)，对照阅读 `gaussian_filter1d`（本文件 687-754 行）——你会发现它本质就是「生成一个高斯核，翻转，再调用 `correlate1d`」，是对本讲原子的第一次组合复用。
- **想下探 C 层**：可跳到 u6-l1 / u6-l2，看 `Py_Correlate1D`（[src/nd_image.c:177](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L177)）内部如何用 `NI_LineBuffer` 做边界扩展与逐行卷积。
