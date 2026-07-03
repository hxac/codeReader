# 高斯与均匀平滑滤波

> 本讲属于「进阶：滤波与傅里叶」单元（u2），承接 u2-l1（一维相关与卷积）与 u2-l2（多维相关卷积）。
> 在前两讲里，我们学会了让一个**任意权重核**沿数组滑动做加权求和（`correlate1d`）。
> 本讲要回答的问题是：**这个权重核从哪儿来？** 也就是说，当我们调用 `gaussian_filter` 或 `uniform_filter` 时，scipy 是如何「造」出一个高斯核或均匀核，并把它应用到多维数组上的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `_gaussian_kernel1d` 是如何用「矩阵算子 D + P」一次性生成任意 `order` 的高斯导数核的，而不用手写复杂的解析公式。
- 解释 `gaussian_filter1d` 为什么要把核**翻转**后再交给 `correlate1d`，以及 `gaussian_filter` 如何把 N-D 高斯滤波**分离**成逐轴的 1-D 滤波。
- 解释 `truncate` 与 `radius` 如何决定核的实际长度，以及 `sigma`、`order`、`axes` 的逐轴语义。
- 说清 `uniform_filter` 为何也是一个**可分离**滤波器，以及它为什么走一条独立的 C 内核 `_nd_image.uniform_filter1d`，而不是复用 `correlate1d`。
- 亲手验证 `gaussian_filter1d(order=1)` 与「手动 `convolve1d` + `_gaussian_kernel1d`」结果完全一致。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 平滑滤波 = 用一个「钟形」或「盒子」核做加权平均

「平滑」（smoothing / blurring）的本质是：每个输出像素 = 它和它邻居的加权平均。

- **高斯核**：邻居权重按高斯钟形曲线衰减，离中心越远权重越小，平滑最自然。
- **均匀核（box filter）**：窗口内所有邻居权重相等，就是简单求平均（移动平均）。

一维高斯函数（未归一化）为：

\[
\phi(x) = \exp\!\left(-\frac{x^2}{2\sigma^2}\right)
\]

归一化后令其和为 1，即可作为卷积核。

### 2.2 什么是「高斯导数核」

有时我们不想要平滑，而想要**导数**（边缘、梯度）。直接对离散数组做差分会放大噪声。一个好办法是「先高斯平滑，再求导」，它等价于直接用「高斯函数的 n 阶导数」作为核去做卷积。于是 `gaussian_filter` 的 `order` 参数就表示「用高斯的第 `order` 阶导数当核」：

- `order=0` → 高斯本身（平滑）。
- `order=1` → 高斯一阶导（负半正、正半负的「过零」形状，用于检测斜坡边缘）。
- `order=2` → 高斯二阶导（Mexican hat / LoG 形状，用于检测零交叉）。

### 2.3 可分离性（separability）：N-D 滤波 = 多次 1-D 滤波

这是本讲最重要的一个数学性质。多维高斯核可以**因式分解**为各维 1-D 高斯的乘积：

\[
G(\mathbf{x}) = \prod_{i=1}^{D} g(x_i)
\]

因此，用 D 维高斯做卷积，等价于**沿每一维依次用 1-D 高斯做卷积**。均匀（盒子）核同理：一个 D 维超立方体盒子 = 各维 1-D 盒子的乘积。

这个性质意味着：我们**不需要**真的构造一个 D 维大核（内存与计算量都会爆炸），只要写好 1-D 版本，然后在 N-D 函数里沿各轴循环调用即可。`gaussian_filter` 和 `uniform_filter` 的全部「多维」魔法，都建立在这条性质上。

> 概念衔接：`order`（样条/导数阶数）、`correlate1d`（一维相关，见 u2-l1）、`axes` 子集（见 u2-l2 的 `_expand_*`，本讲的 `gaussian_filter`/`uniform_filter` 直接用 `_check_axes` 处理）。

## 3. 本讲源码地图

本讲全部内容集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [_filters.py](_filters.py) | 滤波子包的全部 Python 实现。本讲只读其中 5 个函数。 |

涉及的函数与职责：

| 函数 | 行号区间 | 职责 |
| --- | --- | --- |
| `_gaussian_kernel1d` | L656–L684 | 私有：生成 1-D 高斯核（或其 `order` 阶导数核）。 |
| `gaussian_filter1d` | L688–L754 | 沿单轴应用高斯核。 |
| `gaussian_filter` | L758–L861 | N-D 高斯：逐轴循环调用 `gaussian_filter1d`。 |
| `uniform_filter1d` | L1502–L1549 | 沿单轴应用均匀核，直接走 C 内核 `_nd_image.uniform_filter1d`。 |
| `uniform_filter` | L1553–L1621 | N-D 均匀：逐轴循环调用 `uniform_filter1d`。 |

> 提示：这 5 个函数构成清晰的「1-D 内核 → 1-D 函数 → N-D 函数」三层。先读懂 1-D，N-D 只是循环。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① 高斯核的生成 `_gaussian_kernel1d`；② 高斯滤波 `gaussian_filter1d` / `gaussian_filter`；③ 均匀滤波 `uniform_filter1d` / `uniform_filter`。

### 4.1 高斯核生成器 `_gaussian_kernel1d`

#### 4.1.1 概念说明

`_gaussian_kernel1d(sigma, order, radius)` 是一个**私有**函数，它返回长度为 `2*radius+1` 的一维数组，内容是：

- `order=0`：归一化高斯（和为 1）。
- `order>0`：高斯第 `order` 阶导数，乘以归一化高斯。

它的巧妙之处在于：**不写死任何导数的解析公式**，而是把「求多项式导数 + 乘以 p'(x)」这件事写成一个矩阵算子，然后反复作用 `order` 次。这样无论 `order` 是 1、2 还是 5，代码都不变。

#### 4.1.2 核心流程

设目标函数 \( f(x) = q(x)\cdot\phi(x) \)，其中 \(\phi(x)=\exp(p(x))\)，\(p(x)=-x^2/(2\sigma^2)\)，\(q(x)\) 是多项式。

- 初始：\(q(x)=1\)（即 \(f=\phi\)，对应 `order=0`）。
- 想要第 `order` 阶导数核，就要对 \(f\) 求 `order` 次导。由乘积求导法则：

\[
f'(x)=\bigl(q'(x)+q(x)\,p'(x)\bigr)\,\phi(x), \qquad p'(x)=-\frac{x}{\sigma^2}
\]

- 关键观察：\(q'(x)\) 和 \(q(x)\,p'(x)\) 都是多项式，且它们的系数都可以**用同一个矩阵**从 \(q\) 的系数线性得到。定义：

\[
D\,\hat q = \widehat{q'} \quad(\text{求导，超对角}), \qquad
P\,\hat q = \widehat{q\cdot p'} \quad(\text{乘以 }-x/\sigma^2\text{，次对角})
\]

其中 \(\hat q\) 是 \(q(x)\) 的系数向量。于是 \(Q_{\text{deriv}} = D+P\) 作用一次 = 求一次 \(f'\) 的分子多项式系数。作用 `order` 次即得到第 `order` 阶导数核的分子系数，最后在采样点 \(x=-\text{radius}\dots\text{radius}\) 上求值并乘回 \(\phi(x)\)。

伪代码：

```text
phi_x = exp(-x^2 / (2 sigma^2)); phi_x /= phi_x.sum()   # 归一化高斯
if order == 0: return phi_x
q = [1, 0, ..., 0]                       # 多项式 q(x)=1 的系数
D = diag([1,2,...,order], 1)             # 求导算子
P = diag([-1/sigma^2]*order, -1)         # 乘 p'(x) 算子
Q_deriv = D + P
repeat order times: q = Q_deriv . q      # 反复求导
return (x[:,None]^exponent_range) . q * phi_x   # 求值 × 高斯
```

#### 4.1.3 源码精读

归一化高斯部分（`order=0` 直接返回）：

[_filters.py:660-669](_filters.py#L660-L669) —— 先做合法性校验，再算归一化钟形 `phi_x`；`order==0` 时直接返回。

```python
if order < 0:
    raise ValueError('order must be non-negative')
exponent_range = np.arange(order + 1)
sigma2 = sigma * sigma
x = np.arange(-radius, radius+1)
phi_x = np.exp(-0.5 / sigma2 * x ** 2)
phi_x = phi_x / phi_x.sum()

if order == 0:
    return phi_x
```

矩阵算子 D、P 的构造与反复求导（这是全函数最精妙的一段）：

[_filters.py:671-684](_filters.py#L671-L684) —— 把注释里的数学（\(f'= (q'+q\,p')\phi\)）翻译成两个对角矩阵，循环 `order` 次完成求导，最后在采样点求值并乘回 `phi_x`。

```python
q = np.zeros(order + 1)
q[0] = 1
D = np.diag(exponent_range[1:], 1)          # D @ q(x) = q'(x)
P = np.diag(np.ones(order)/-sigma2, -1)     # P @ q(x) = q(x) * p'(x)
Q_deriv = D + P
for _ in range(order):
    q = Q_deriv.dot(q)
q = (x[:, None] ** exponent_range).dot(q)   # 在采样点 x 上对多项式求值
return q * phi_x
```

逐行解读这两个对角矩阵为何正好对应求导与乘 \(p'\)：

- 多项式 \(q(x)=\sum_i q_i x^i\) 用系数向量 `q=[q0,q1,...]` 表示。
- **超对角矩阵 `D`**：`(D@q)[i] = (i+1)*q[i+1]`，恰好是 \(q'(x)=\sum_i (i+1)q_{i+1}x^i\) 的系数。
- **次对角矩阵 `P`**：`(P@q)[i] = (-1/sigma2)*q[i-1]`，恰好是 \((-x/\sigma^2)\cdot q(x)\) 的系数（因为乘 \(x\) 会把系数整体上移一格）。
- 两者相加 `Q_deriv = D+P`，作用在系数向量上 =「分子多项式求一次导」。循环 `order` 次即得第 `order` 阶导数分子的系数。
- 最后 `(x[:,None] ** exponent_range).dot(q)` 是**多项式在采样点上的批量求值**（范德蒙矩阵乘系数向量），再乘 `phi_x` 还原成「多项式 × 高斯」。

#### 4.1.4 代码实践

1. **目标**：直观看到 `order` 如何改变核的形状。
2. **操作步骤**：运行下面脚本（示例代码，非项目原有代码）：

   ```python
   import numpy as np
   import matplotlib.pyplot as plt
   from scipy.ndimage import _gaussian_kernel1d   # 私有函数，可直接导入

   sigma, radius = 2.0, 8
   x = np.arange(-radius, radius + 1)
   for order in (0, 1, 2):
       k = _gaussian_kernel1d(sigma, order, radius)
       plt.plot(x, k, marker='o', label=f'order={order}')
   plt.axhline(0, color='k', lw=0.5)
   plt.legend(); plt.title('Gaussian kernels of different orders')
   plt.show()
   ```
3. **观察现象**：
   - `order=0`：标准钟形，全为正，面积（和）为 1。
   - `order=1`：反对称（负-零-正），在中心过零。
   - `order=2`：Mexican hat 形状（中间正、两侧负）。
4. **预期结果**：三条曲线分别对应平滑核、一阶导核、二阶导核；`order=0` 的核求和应为 1.0。
5. 若本地无 matplotlib，可用 `print(_gaussian_kernel1d(2.0, 0, 4).round(3))` 查看数值（待本地验证具体小数）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `order=0` 的核必须归一化（除以 `phi_x.sum()`），而 `order=1` 的核不需要（也不应该）归一化？

> **答案**：`order=0` 是平滑核，归一化保证「直流分量不变」（常数数组平滑后仍是原常数）。`order=1` 是导数核，常数数组的导数应为 0，导数核的**和必然为 0**（正负抵消），归一化没有意义。

**练习 2**：把 `D` 和 `P` 的位置（超对角 vs 次对角）对调会怎样？

> **答案**：会得到错误的算子。`D` 必须是超对角（求导降低多项式每一项的次数、提升系数下标），`P` 必须是次对角（乘 \(x\) 提升次数、压低系数下标）。对调后 `Q_deriv@q` 不再表示 \(q'+q\,p'\)，生成的核也就不是高斯导数。

---

### 4.2 高斯滤波 `gaussian_filter1d` / `gaussian_filter`

#### 4.2.1 概念说明

有了核生成器，`gaussian_filter1d` 的工作就很简单：**算出核 → 翻转 → 交给 `correlate1d`**。

- 为什么翻转？因为 `correlate1d` 做的是「相关」而不是「卷积」。对 `order=0` 高斯核是对称的，翻不翻转无所谓；但对 `order` 为奇数（反对称）的导数核，翻转会改变符号。为了让 `gaussian_filter1d` 的语义始终是「**卷积**高斯核」，它在调用 `correlate1d` 前主动把核翻转一次（回顾 u2-l1：卷积 = 翻转核的相关）。
- `gaussian_filter`（N-D）则利用 2.3 节的**可分离性**，沿每个轴依次调用 `gaussian_filter1d`，并且**就地链式复用**同一个输出缓冲区。

#### 4.2.2 核心流程

`gaussian_filter1d` 的流程：

```text
sd = float(sigma)
lw = int(truncate * sd + 0.5)        # 默认 truncate=4.0
if radius is not None: lw = radius   # radius 显式指定则覆盖
校验 lw 是非负整数
weights = _gaussian_kernel1d(sigma, order, lw)[::-1]   # 造核并翻转
return correlate1d(input, weights, axis, output, mode, cval, origin=0)
```

`gaussian_filter` 的流程（N-D 可分离）：

```text
output = _get_output(output, input)
axes = _check_axes(axes, input.ndim)                  # 规范 axes（含 None/标量/序列）
把 sigma/order/mode/radius 各自 _normalize_sequence 到 num_axes 长
只保留 sigma > 1e-15 的轴（sigma≈0 的轴跳过，等价于不滤波）
for 每个保留轴:
    gaussian_filter1d(input, sigma, axis, order, output, ...)   # 结果写回 output
    input = output                                  # 下一轴以上一轴结果为输入（就地链式）
若没有任何轴被滤波: output[...] = input[...]
return output
```

> 关键设计：`input = output` 这一行让多次 1-D 滤波**共用同一块输出内存**，避免每轴都分配新数组。这也是文档字符串里「中间数组以 output 的 dtype 存储，低精度 output 可能带来精度损失」这句警告的由来（见 L800-L807）。

#### 4.2.3 源码精读

`gaussian_filter1d` 的核心三步——算半径、造核翻转、调用 `correlate1d`：

[_filters.py:745-754](_filters.py#L745-L754) —— `lw` 由 `truncate*sigma` 四舍五入得到（或被 `radius` 覆盖），核翻转后以 `origin=0` 调用 `correlate1d`。

```python
sd = float(sigma)
# make the radius of the filter equal to truncate standard deviations
lw = int(truncate * sd + 0.5)
if radius is not None:
    lw = radius
if not isinstance(lw, numbers.Integral) or lw < 0:
    raise ValueError('Radius must be a nonnegative integer.')
# Since we are calling correlate, not convolve, revert the kernel
weights = _gaussian_kernel1d(sigma, order, lw)[::-1]
return correlate1d(input, weights, axis, output, mode, cval, 0)
```

逐轴参数规范化 + 可分离循环（`gaussian_filter` 的主体）：

[_filters.py:843-861](_filters.py#L843-L861) —— 先 `_get_output` 准备输出，`_check_axes` 处理 `axes`，把 4 个逐轴参数规范化，过滤掉 `sigma≈0` 的轴，然后沿每个轴就地调用 `gaussian_filter1d`。

```python
input = np.asarray(input)
output = _ni_support._get_output(output, input)

axes = _ni_support._check_axes(axes, input.ndim)
num_axes = len(axes)
orders = _ni_support._normalize_sequence(order, num_axes)
sigmas = _ni_support._normalize_sequence(sigma, num_axes)
modes = _ni_support._normalize_sequence(mode, num_axes)
radiuses = _ni_support._normalize_sequence(radius, num_axes)
axes = [(axes[ii], sigmas[ii], orders[ii], modes[ii], radiuses[ii])
        for ii in range(num_axes) if sigmas[ii] > 1e-15]
if len(axes) > 0:
    for axis, sigma, order, mode, radius in axes:
        gaussian_filter1d(input, sigma, axis, order, output,
                          mode, cval, truncate, radius=radius)
        input = output
else:
    output[...] = input[...]
return output
```

要点提炼：

- `sigma`、`order`、`mode`、`radius` 都通过 `_normalize_sequence`（见 u1-l4）从标量广播成「每轴一个值」，于是你可以传 `sigma=[2, 1]` 让两轴平滑程度不同。
- `if sigmas[ii] > 1e-15`：标准差近似为 0 的轴等价于恒等映射，直接跳过；若所有轴都被跳过，走 `else` 分支把输入原样拷进输出。
- `input = output`：可分离滤波的就地链式复用。注意 `_check_axes` 返回的轴已去重排序，与 u2-l2 的 `axes` 子集语义一致。

#### 4.2.4 代码实践

1. **目标**：验证 `gaussian_filter1d(order=1)` 与「手动 `convolve1d` + `_gaussian_kernel1d`」结果完全一致，从而理解「翻转 + 相关」与「卷积」的等价关系。
2. **操作步骤**（示例代码）：

   ```python
   import numpy as np
   from scipy.ndimage import gaussian_filter1d, convolve1d, _gaussian_kernel1d

   x = np.array([2., 8., 0., 4., 1., 9., 9., 0.], dtype=float)
   sigma, order, truncate = 1.5, 1, 4.0

   # (A) 库函数：内部做 weights = kernel[::-1]; correlate1d(...)
   a = gaussian_filter1d(x, sigma, order=order, truncate=truncate)

   # (B) 手动：复刻 gaussian_filter1d 的半径公式，造核后用 convolve1d
   lw = int(truncate * sigma + 0.5)
   kernel = _gaussian_kernel1d(sigma, order, lw)      # 注意：不翻转
   b = convolve1d(x, kernel, mode='reflect')

   print(np.allclose(a, b))   # 期望 True
   print(a.round(4)); print(b.round(4))
   ```
3. **观察现象**：`a` 与 `b` 数值逐元素相同。
4. **预期结果**：`np.allclose(a, b)` 为 `True`。
5. **为什么相等**：回顾 u2-l1，`convolve1d(x, k)` 内部把 `k` 翻转成 `k[::-1]` 再做 `correlate1d`；而 `gaussian_filter1d` 也是把 `_gaussian_kernel1d(...)` 翻转后做 `correlate1d`。两者翻转的是同一个核、用的是同一个 `correlate1d`，故必然相等。这正是源码注释「Since we are calling correlate, not convolve, revert the kernel」的含义。

#### 4.2.5 小练习与答案

**练习 1**：调用 `gaussian_filter(a, sigma=[3, 0.0])`（第二轴 `sigma=0`）会发生什么？结果与 `gaussian_filter1d(a, 3, axis=0)` 有何关系？

> **答案**：第二轴 `sigma=0` 会被 `if sigmas[ii] > 1e-15` 过滤掉，只沿 axis=0 做高斯平滑，axis=1 原样保留。因此结果在数学上等价于 `gaussian_filter1d(a, 3, axis=0)`。

**练习 2**：为什么 `gaussian_filter` 的文档警告「低精度 output 会带来精度损失」？

> **答案**：因为 N-D 滤波是逐轴链式进行的，且通过 `input = output` 复用同一输出缓冲。每一步中间结果都按 `output` 的 dtype 存储。若 `output` 是低精度（如 float32），多轮 1-D 卷积的中间结果会被反复截断，误差累积放大。

---

### 4.3 均匀滤波 `uniform_filter1d` / `uniform_filter`

#### 4.3.1 概念说明

均匀滤波（box filter / 移动平均）是最简单的平滑：窗口内每个值权重相等，输出 = 窗口内 `size` 个值的平均。

它和 `gaussian_filter1d` 有两个关键不同：

1. **没有 Python 层的「造核」步骤**。均匀核的权重全相等（`1/size`），不必显式构造一个权重数组，C 内核直接用「滑动求和」实现，时间复杂度是 \(O(n)\)，与 `size` 无关；而 `correlate1d` 是 \(O(n\cdot\text{size})\)。所以 `uniform_filter1d` **不复用** `correlate1d`，而是直接调用专用 C 内核 `_nd_image.uniform_filter1d`。
2. 同样依赖**可分离性**：`uniform_filter` 沿各轴循环调用 `uniform_filter1d`。

#### 4.3.2 核心流程

`uniform_filter1d`：

```text
校验 size >= 1
complex_output = (input 是复数)
output = _get_output(..., complex_output=...)
校验 origin 合法 (size//2 + origin 在 [0, size) 内)
mode = _extend_mode_to_code(mode)            # 边界模式字符串 → 整数码（见 u1-l4）
if 实数:
    _nd_image.uniform_filter1d(input, size, axis, output, mode, cval, origin)
else:  # 复数：实部、虚部分别各调一次
    _nd_image.uniform_filter1d(input.real, ..., output.real, ...)
    _nd_image.uniform_filter1d(input.imag, ..., output.imag, ...)
return output
```

`uniform_filter`（N-D 可分离，结构与 `gaussian_filter` 几乎对称）：

```text
output = _get_output(..., complex_output=...)
axes = _check_axes(axes, input.ndim)
sizes/origins/modes 各自 _normalize_sequence 到 num_axes 长
只保留 size > 1 的轴（size==1 等价于不滤波）
for 每个保留轴:
    uniform_filter1d(input, int(size), axis, output, mode, cval, origin)
    input = output                              # 就地链式
若没有轴被滤波: output[...] = input[...]
return output
```

#### 4.3.3 源码精读

`uniform_filter1d` 的参数校验 + C 内核分派：

[_filters.py:1531-1549](_filters.py#L1531-L1549) —— 注意它**不**经过 `correlate1d`，而是直接 `_nd_image.uniform_filter1d(...)`；复数输入被拆成实部、虚部各调一次（与 u2-l1 的 `_complex_via_real_components` 思路一致：C 内核只认实数）。

```python
input = np.asarray(input)
axis = normalize_axis_index(axis, input.ndim)
if size < 1:
    raise RuntimeError('incorrect filter size')
complex_output = input.dtype.kind == 'c'
output = _ni_support._get_output(output, input,
                                 complex_output=complex_output)
if (size // 2 + origin < 0) or (size // 2 + origin >= size):
    raise ValueError('invalid origin')
mode = _ni_support._extend_mode_to_code(mode)
if not complex_output:
    _nd_image.uniform_filter1d(input, size, axis, output, mode, cval,
                               origin)
else:
    _nd_image.uniform_filter1d(input.real, size, axis, output.real, mode,
                               np.real(cval), origin)
    _nd_image.uniform_filter1d(input.imag, size, axis, output.imag, mode,
                               np.imag(cval), origin)
return output
```

`uniform_filter` 的可分离循环（与 `gaussian_filter` 对照阅读）：

[_filters.py:1604-1621](_filters.py#L1604-L1621) —— 结构与 `gaussian_filter` 的 L843–L861 几乎完全对称：`_get_output` → `_check_axes` → `_normalize_sequence` → 过滤 `size>1` 的轴 → 逐轴就地调用 `uniform_filter1d`。

```python
input = np.asarray(input)
output = _ni_support._get_output(output, input,
                                 complex_output=input.dtype.kind == 'c')
axes = _ni_support._check_axes(axes, input.ndim)
num_axes = len(axes)
sizes = _ni_support._normalize_sequence(size, num_axes)
origins = _ni_support._normalize_sequence(origin, num_axes)
modes = _ni_support._normalize_sequence(mode, num_axes)
axes = [(axes[ii], sizes[ii], origins[ii], modes[ii])
        for ii in range(num_axes) if sizes[ii] > 1]
if len(axes) > 0:
    for axis, size, origin, mode in axes:
        uniform_filter1d(input, int(size), axis, output, mode,
                         cval, origin)
        input = output
else:
    output[...] = input[...]
return output
```

对照要点：

| 维度 | `gaussian_filter` | `uniform_filter` |
| --- | --- | --- |
| 跳过条件 | `sigma > 1e-15` | `size > 1` |
| 1-D 实现 | 经 `correlate1d`（用造好的核） | 直接 C 内核 `_nd_image.uniform_filter1d` |
| 复杂度（单轴） | \(O(n\cdot\text{lw})\) | \(O(n)\)（滑动求和） |
| 可分离 | 是 | 是 |
| 复数处理 | 由 `correlate1d` 内部 `_complex_via_real_components` 完成 | 在本函数内显式拆 real/imag 各调一次 |

> 思考：为什么均匀核能 \(O(n)\)？因为窗口滑动一格时，新窗口的和 = 旧窗口和 − 离开窗口的元素 + 进入窗口的元素，只需 \(O(1)\) 维护一个累加器。这正是专用 C 内核存在的价值。

#### 4.3.4 代码实践

1. **目标**：验证均匀滤波就是「窗口均值」，并理解其可分离性。
2. **操作步骤**（示例代码）：

   ```python
   import numpy as np
   from scipy.ndimage import uniform_filter1d, uniform_filter

   x = np.array([2., 8., 0., 4., 1., 9., 9., 0.])
   print(uniform_filter1d(x, size=3))          # 窗口均值（reflect 边界）

   # 手动验证一个内部点：x[1] 的窗口 = [x0,x1,x2] = [2,8,0] 均值 = 10/3
   print((2 + 8 + 0) / 3)

   # 可分离性：2D uniform_filter 等价于逐轴 uniform_filter1d
   a = np.arange(25, dtype=float).reshape(5, 5)
   full = uniform_filter(a, size=3)
   step = uniform_filter1d(uniform_filter1d(a, 3, axis=0), 3, axis=1)
   print(np.allclose(full, step))              # 期望 True
   ```
3. **观察现象**：`uniform_filter1d(x, 3)` 的内部点恰好等于相邻三点的算术平均；2D 结果与逐轴两次 1-D 结果一致。
4. **预期结果**：第一个 `print` 输出 `[4, 3.33, 4, 1.33, 4.67, 6.33, 6, 3]`（边界受 `reflect` 影响，精确小数待本地验证）；`np.allclose` 为 `True`。

#### 4.3.5 小练习与答案

**练习 1**：用 `uniform_filter1d` 处理一个全 1 的常数数组，结果应是多少？为什么？

> **答案**：仍是全 1。因为均匀核是「窗口均值」，常数数组任何窗口的均值都等于该常数。这与 `gaussian_filter` 的归一化高斯核一样，保证直流分量不变。

**练习 2**：`uniform_filter1d` 为什么不像 `gaussian_filter1d` 那样调用 `correlate1d`？

> **答案**：均匀核权重全相等，专用 C 内核可用「滑动累加」做到 \(O(n)\)，远快于 `correlate1d` 的 \(O(n\cdot\text{size})\)。专用内核是以性能为动机的设计选择。

---

## 5. 综合实践

把本讲三个模块串起来：**自己用 `_gaussian_kernel1d` + `correlate1d` 重新实现一个简化版 `gaussian_filter`，并与官方实现逐元素比对**。

任务步骤（示例代码）：

```python
import numpy as np
from scipy.ndimage import (
    gaussian_filter, correlate1d, _gaussian_kernel1d, _ni_support
)

def my_gaussian_filter(input, sigma, order=0, truncate=4.0, mode='reflect'):
    # 1. 准备输出（复用官方 _get_output，体会它对 None/dtype/array 的多态）
    input = np.asarray(input)
    output = _ni_support._get_output(None, input)
    # 2. 沿每个轴可分离地做 1-D 高斯卷积
    result = input
    for axis in range(input.ndim):
        lw = int(truncate * sigma + 0.5)
        kernel = _gaussian_kernel1d(sigma, order, lw)[::-1]   # 翻转→卷积语义
        correlate1d(result, kernel, axis, output=output, mode=mode)
        result = output                                       # 就地链式
    return output

a = np.random.default_rng(0).standard_normal((6, 6))
ref = gaussian_filter(a, sigma=1.5, order=0, truncate=4.0, mode='reflect')
mine = my_gaussian_filter(a, sigma=1.5, order=0, truncate=4.0, mode='reflect')
print(np.allclose(ref, mine))   # 期望 True
```

完成后再尝试：

- 把 `order` 改成 1，确认仍然 `allclose`（验证翻转处理对反对称核也正确）。
- 把 `my_gaussian_filter` 里的逐轴循环改成只对 `axes=(0,)` 操作，对照官方 `gaussian_filter(a, 1.5, axes=(0,))`，理解 `axes` 子集语义。

这个综合实践一次性覆盖了：① 核生成 `_gaussian_kernel1d`；② 翻转 + `correlate1d`；③ N-D 可分离与就地链式复用；④ `_get_output` 的输出数组管理。

## 6. 本讲小结

- `_gaussian_kernel1d` 用「矩阵算子 `D+P`」反复作用 `order` 次来生成任意阶高斯导数核，无需手写解析公式；`order=0` 返回归一化钟形，`order≥1` 返回「多项式 × 高斯」。
- `gaussian_filter1d` = 算半径 `lw=int(truncate*sigma+0.5)`（或用 `radius`）→ 造核并翻转 → 调 `correlate1d`；翻转是为了把「相关」修正为「卷积」语义。
- `gaussian_filter` 利用高斯核的**可分离性**，沿各轴依次调用 `gaussian_filter1d`，并通过 `input = output` 就地链式复用同一缓冲。
- `sigma`、`order`、`mode`、`radius` 均可逐轴指定（`_normalize_sequence`），`sigma≈0` 的轴会被跳过。
- `uniform_filter1d` 不走 `correlate1d`，而是直接调用专用 C 内核 `_nd_image.uniform_filter1d`，用滑动求和做到 \(O(n)\)；复数输入在本函数内显式拆成实/虚部各调一次。
- `uniform_filter` 与 `gaussian_filter` 结构对称，同样基于盒子核的可分离性逐轴循环；`size==1` 的轴被跳过。

## 7. 下一步学习建议

本讲掌握了「核从哪来」与「可分离 N-D 滤波」两大模式。建议接着学习：

- **u2-l4 微分与边缘滤波**：会看到 `sobel`、`prewitt`、`gaussian_laplace`、`gaussian_gradient_magnitude` 等函数，它们正是把本讲的 `gaussian_filter1d(order=...)`（高斯导数核）与 `correlate1d`（一阶/二阶差分核）**组合**起来的产物。
- **u2-l5 秩/选择/通用滤波**：了解非线性的邻域滤波（中值、最大最小），它们不可分离，因此实现路径与本讲不同。
- 想下探到 C 层的读者，可预先浏览 `src/ni_filters.c` 中 `correlate1d` 的内层循环，理解 `_gaussian_kernel1d` 造出的核在 C 端是如何被逐元素乘加的（对应专家单元 u6）。
