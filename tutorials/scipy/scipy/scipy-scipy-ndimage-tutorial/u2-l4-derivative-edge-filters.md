# 微分与边缘滤波

> 本讲属于「进阶：滤波与傅里叶」单元（u2），承接 u2-l3（高斯与均匀平滑滤波）。
> 在 u2-l3 里，我们知道了「权重核从哪儿来」：`_gaussian_kernel1d` 用 `order` 参数能造出高斯的**任意阶导数核**（`order=1` 是过零的一阶导、`order=2` 是 Mexican-hat 二阶导），而 `gaussian_filter1d` 只是「造核 → 翻转 → 交给 `correlate1d`」。
> 本讲要回答的问题是：**把这些「导数核」和「平滑核」组合起来，能做些什么？** 答案就是图像处理里最经典的一族算子——**边缘检测滤波**：`prewitt`、`sobel`、`laplace`、`gaussian_laplace`、`gaussian_gradient_magnitude`。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `prewitt` 与 `sobel` **不是**单纯的一阶差分 `[1,0,-1]`，而是「沿一个轴求一阶导 + 沿其余轴平滑」的可分离组合，并能解释为何 `sobel` 的平滑核是 `[1,2,1]`、`prewitt` 是 `[1,1,1]`。
- 解释 `laplace` 是「各轴二阶差分 `[1,-2,1]` 之和」，而 `generic_laplace` 是一个**通用引擎**，接受任意「二阶导函数」`derivative2` 并把各轴结果累加。
- 解释 `generic_gradient_magnitude` 如何用「各轴一阶导的平方和开根号」计算梯度模长，以及它与 `generic_laplace` 在「平方后求和 vs 直接求和」上的根本区别。
- 说清 `gaussian_laplace`（Laplacian-of-Gaussian，LoG）= `generic_laplace` + `gaussian_filter(order=2)`、`gaussian_gradient_magnitude` = `generic_gradient_magnitude` + `gaussian_filter(order=1)` 的组合关系。
- 亲手构造一张含阶跃边缘的图，观察 `sobel` 的「单峰响应」与 `laplace` 的「零交叉（zero-crossing）」，并体会 `gaussian_laplace` 改变 `sigma` 对边缘定位的影响。

## 2. 前置知识

### 2.1 什么是「边缘」：导数视角

一张图像里的**边缘**，就是亮度发生剧烈变化的位置。从微积分看：

- **一阶导数**在边缘处出现极值（一个尖峰）——因为它度量「变化率」。`sobel`/`prewitt` 走这条路。
- **二阶导数**在边缘处**过零**（先正后负、或先负后正，中间穿过 0）——因为它度量「变化率的变化」。`laplace`/`gaussian_laplace` 走这条路。

对一条一维阶跃信号 \( f(x) \)（左半为 0、右半为 1），其一阶导 \( f'(x) \) 是边缘处的一个冲激峰，二阶导 \( f''(x) \) 则是「正峰 + 负峰」且恰好在边缘位置过零：

\[
f'(x)\;\text{在边缘取极大}, \qquad f''(x)\;\text{在边缘处}\;0\;\text{并发生符号反转（零交叉）}
\]

> 关键结论：**找一阶导的极大值**，或**找二阶导的零交叉**，都能定位边缘。本讲的两组函数分别对应这两条路线。

### 2.2 一阶/二阶差分核长什么样

对离散数组，导数用「差分核」近似（回顾 u2-l1 的 `correlate1d`：核沿轴滑动加权求和）：

| 导数 | 差分核 | 含义 |
| --- | --- | --- |
| 一阶（中心差分） | `[-1, 0, 1]` | \( \text{out}[i] = \text{in}[i+1] - \text{in}[i-1] \) |
| 二阶（中心二阶差分） | `[1, -2, 1]` | \( \text{out}[i] = \text{in}[i-1] - 2\,\text{in}[i] + \text{in}[i+1] \) |

这两个小核就是本讲全部算子的「原子」。`sobel`/`prewitt` 用 `[-1,0,1]`，`laplace` 用 `[1,-2,1]`。

### 2.3 组合 = 可分离性的再次运用

u2-l3 讲过「可分离性」：一个多维核若能写成各维 1-D 核的乘积，N-D 卷积就等于逐轴 1-D 卷积。本讲的 `sobel`/`prewitt` 把这个性质用到了极致：

> 一个二维 Sobel 掩膜 = （某轴的一阶差分 `[-1,0,1]`）× （垂直方向的平滑 `[1,2,1]`）。

也就是说，**真正的边缘算子不是纯差分**，而是「在边缘方向求导 + 在垂直方向平滑」。平滑的作用是抑制垂直于边缘方向的噪声，让响应更稳。这条直觉贯穿 `prewitt`/`sobel` 的实现。

### 2.4 Laplacian-of-Gaussian（LoG）：先平滑再求拉普拉斯

直接对原图求二阶导会放大噪声（差分对高频极敏感）。经典做法是「先高斯平滑，再求拉普拉斯」。由于卷积与求导可交换：

\[
\nabla^2(G_\sigma * I) = (\nabla^2 G_\sigma) * I
\]

即「先平滑再拉普拉斯」等价于「直接用高斯的二阶导核做卷积」。这正是 u2-l3 里 `gaussian_filter(order=2)` 造出的核，也正是 `gaussian_laplace` 的数学基础。

> 概念衔接：`correlate1d`（u2-l1）、可分离性与 `input=output` 就地链式（u2-l3）、`gaussian_filter` 的 `order` 参数（u2-l3）、`_check_axes` / `_normalize_sequence`（u1-l4）。

## 3. 本讲源码地图

本讲全部内容集中在**一个文件**里：

| 文件 | 作用 |
| --- | --- |
| [`_filters.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py) | 滤波子包的全部 Python 实现。本讲只读其中 7 个函数（其中 2 个是「通用引擎」）。 |

涉及的函数与职责：

| 函数 | 行号区间 | 职责 |
| --- | --- | --- |
| `prewitt` | L865–L923 | 沿 `axis` 一阶差分 + 沿其余轴 `[1,1,1]` 平滑。 |
| `sobel` | L927–L981 | 沿 `axis` 一阶差分 + 沿其余轴 `[1,2,1]` 平滑。 |
| `generic_laplace` | L985–L1033 | **通用引擎**：各轴二阶导之和。 |
| `laplace` | L1037–L1071 | `generic_laplace` + 二阶差分核 `[1,-2,1]`。 |
| `gaussian_laplace` | L1075–L1139 | `generic_laplace` + `gaussian_filter(order=2)`（LoG）。 |
| `generic_gradient_magnitude` | L1143–L1196 | **通用引擎**：各轴一阶导平方和开根号。 |
| `gaussian_gradient_magnitude` | L1200–L1250 | `generic_gradient_magnitude` + `gaussian_filter(order=1)`。 |

> 阅读策略：先看两个「通用引擎」`generic_laplace`（L985）与 `generic_gradient_magnitude`（L1143），再看 `laplace`/`gaussian_laplace`/`gaussian_gradient_magnitude` 如何只定义一个内部函数就把引擎填满；`prewitt`/`sobel` 是另一条独立支线，自成一体。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：① `prewitt` / `sobel`（一阶导 + 垂直平滑）；② `laplace` / `generic_laplace`（二阶导之和的通用引擎）；③ `gaussian_laplace` / `generic_gradient_magnitude` / `gaussian_gradient_magnitude`（高斯导数的组合）。

### 4.1 `prewitt` / `sobel`：沿一轴求导，沿其余轴平滑

#### 4.1.1 概念说明

很多人误以为 `sobel` 就是 `[1,0,-1]` 差分。**不是**。看源码会发现，它做两件事：

1. 沿指定 `axis` 用 `[-1, 0, 1]` 做一阶中心差分（找边缘）。
2. 沿**其余所有轴**用一个平滑核再做一次 `correlate1d`（降噪、稳定响应）。

`prewitt` 与 `sobel` 的**唯一区别**就是第 2 步的平滑核：

- `prewitt`：`[1, 1, 1]`（盒子平滑，等权）。
- `sobel`：`[1, 2, 1]`（二项式平滑，近似一个小高斯，中心权重大，更平滑）。

为什么这样设计？因为真实的边缘往往带噪声，纯粹逐行差分会把噪声也当成边缘。「沿边缘方向求导 + 沿边缘走向（垂直方向）平滑」是一种简单有效的折中。而这之所以能用两次 1-D 卷积实现，靠的就是 2.3 节的**可分离性**：二维 Sobel 掩膜可分解为一维差分核与一维平滑核的外积。

#### 4.1.2 核心流程

`sobel` 与 `prewitt` 的流程几乎逐行相同，只有平滑核不同：

```text
input = np.asarray(input)
axis = normalize_axis_index(axis, input.ndim)      # 把 -1 等负轴解析成正轴
output = _get_output(output, input)                # 准备输出（见 u1-l4）
modes = _normalize_sequence(mode, input.ndim)      # 逐轴边界模式

# 第 1 步：沿 axis 做一阶中心差分，结果写进 output
correlate1d(input, [-1, 0, 1], axis, output, modes[axis], cval, 0)

# 第 2 步：沿【其余每个轴】平滑，就地读 output、写 output
axes = [ii for ii in range(input.ndim) if ii != axis]
for ii in axes:
    correlate1d(output, <平滑核>, ii, output, modes[ii], cval, 0)
return output
```

> 关键设计：第 1 步把差分结果写进 `output`；第 2 步的每次 `correlate1d` 都以 `output` 为**输入**又为**输出**（`input=output`），就地链式复用同一块缓冲——这正是 u2-l3 里 `gaussian_filter` 的同款手法。因此整个过程只分配**一个**临时数组。

#### 4.1.3 源码精读

`prewitt` 的实现（注意平滑核是 `[1, 1, 1]`）：

[`_filters.py:915-923`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L915-L923) —— 先沿 `axis` 差分写入 `output`，再沿其余每轴用 `[1,1,1]` 就地平滑。

```python
input = np.asarray(input)
axis = normalize_axis_index(axis, input.ndim)
output = _ni_support._get_output(output, input)
modes = _ni_support._normalize_sequence(mode, input.ndim)
correlate1d(input, [-1, 0, 1], axis, output, modes[axis], cval, 0)
axes = [ii for ii in range(input.ndim) if ii != axis]
for ii in axes:
    correlate1d(output, [1, 1, 1], ii, output, modes[ii], cval, 0,)
return output
```

`sobel` 与之逐行对应，**只**把平滑核换成 `[1, 2, 1]`：

[`_filters.py:973-981`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L973-L981) —— 与 `prewitt` 唯一的实质差异是平滑核 `[1,2,1]`（二项式，中心加权，更接近高斯）。

```python
input = np.asarray(input)
axis = normalize_axis_index(axis, input.ndim)
output = _ni_support._get_output(output, input)
modes = _ni_support._normalize_sequence(mode, input.ndim)
correlate1d(input, [-1, 0, 1], axis, output, modes[axis], cval, 0)
axes = [ii for ii in range(input.ndim) if ii != axis]
for ii in axes:
    correlate1d(output, [1, 2, 1], ii, output, modes[ii], cval, 0)
return output
```

要点提炼：

- 两个函数都**没有**调用 `gaussian_filter`，也没有 `axes`/`order` 参数——它们是独立的、固定 3×…×3 掩膜的边缘算子。
- 平滑核 `[1,1,1]` 与 `[1,2,1]` 都**未归一化**（没有除以 3 或 4）。这意味着它们的响应会随平滑核求和放大（详见 4.1.4 的数值验证）。
- 可分离性的几何意义：对一个二维图，`sobel(img, axis=0)` = 「沿行方向（axis=0）差分 + 沿列方向（axis=1）平滑」。把它写成掩膜就是经典 Sobel 水平梯度核：

\[
\begin{bmatrix}-1 & 0 & 1\\ -2 & 0 & 2\\ -1 & 0 & 1\end{bmatrix}
= \begin{bmatrix}1\\2\\1\end{bmatrix} \cdot \begin{bmatrix}-1 & 0 & 1\end{bmatrix}
\]

  即「平滑列向量 `[1,2,1]ᵀ`」与「差分行向量 `[-1,0,1]`」的外积。`correlate1d` 两次调用正是在分别施加这两个一维因子。

#### 4.1.4 代码实践

1. **目标**：验证「沿轴差分 + 垂直平滑」的组合，并观察 `sobel` 与 `prewitt` 在同一阶跃边缘上响应的差异。
2. **操作步骤**（示例代码，非项目原有代码）：

   ```python
   import numpy as np
   from scipy import ndimage

   # 64x64 图，第 32 列处有一条垂直阶跃边缘（左 0、右 100）
   img = np.zeros((64, 64), dtype=float)
   img[:, 32:] = 100.0

   # axis=1：沿列方向求一阶导 → 检测垂直边缘
   sobel_resp = ndimage.sobel(img, axis=1)
   prewitt_resp = ndimage.prewitt(img, axis=1)

   # 取中间一行的响应观察
   row = 32
   print("sobel  cols 29..34:", sobel_resp[row, 29:35])
   print("prewitt cols 29..34:", prewitt_resp[row, 29:35])
   print("sobel peak:", sobel_resp.max(), " prewitt peak:", prewitt_resp.max())
   ```
3. **观察现象**：
   - 两个响应都只在边缘附近（第 31、32 列）非零，其余平坦区域为 0——证实「一阶导在边缘取极值、平坦处为 0」。
   - 因为平滑核未归一化，且本例中沿 `axis=0` 各行相同（平滑相当于把恒定值按核求和）：`sobel` 的峰约等于 `100 × (1+2+1) = 400`，`prewitt` 约等于 `100 × (1+1+1) = 300`，二者比值约为 4∶3。
4. **预期结果**：`sobel peak ≈ 400`、`prewitt peak ≈ 300`（精确数值受边界 `reflect` 影响，**待本地验证**；中间行的内部列应严格满足该比值）。
5. **延伸思考**：把图像加上少量噪声（`img += np.random.default_rng(0).normal(0, 5, img.shape)`）再跑一次，会看到 `sobel` 的响应比 `prewitt` 更「干净」——这正是 `[1,2,1]` 比 `[1,1,1]` 平滑更强的体现。

#### 4.1.5 小练习与答案

**练习 1**：对一个**一维**数组调用 `sobel(x, axis=0)`，第 2 步的 `for ii in axes` 循环还会执行吗？结果与直接 `correlate1d(x, [-1,0,1])` 有何关系？

> **答案**：一维时 `axes = [ii for ii in range(1) if ii != 0] = []`，循环体不执行，只剩第 1 步的纯差分。因此 `sobel(1D)` 退化为 `[-1,0,1]` 中心差分，与 `correlate1d(x, [-1,0,1])` 完全等价（边界模式一致时）。`prewitt` 同理。

**练习 2**：为什么 `sobel` 的平滑核 `[1,2,1]` 不归一化（不除以 4）会影响「响应大小」但不影响「边缘位置」？

> **答案**：边缘位置由响应的**极值所在坐标**决定，与核的整体缩放无关（乘以常数不改变极值位置）。不归一化只是让响应幅值放大了一个常数倍（这里是 4），在「只关心边缘在哪」的应用里无所谓；若要把多通道响应做定量比较，则需留意这个隐含的缩放。

---

### 4.2 `laplace` / `generic_laplace`：二阶导之和的通用引擎

#### 4.2.1 概念说明

拉普拉斯算子（Laplacian）是「各轴二阶偏导之和」：

\[
\nabla^2 I = \sum_{i} \frac{\partial^2 I}{\partial x_i^2}
\]

在离散情形，每条轴的二阶导用 `[1, -2, 1]` 近似，所以 N-D `laplace` 就是「每条轴各做一次 `[1,-2,1]` 相关，再相加」。

`generic_laplace` 把这件事抽象成一个**通用引擎**：它本身不关心「二阶导具体怎么算」，只规定一个回调 `derivative2(input, axis, output, mode, cval, ...)`，由调用者提供。引擎负责：

- 在第一条目标轴上调用 `derivative2`，结果直接写入输出；
- 在其余每条轴上调用 `derivative2` 得到一个临时数组 `tmp`，再 `output += tmp` 累加。

`laplace` 则是最简单的填法：把 `derivative2` 定义为 `correlate1d(input, [1, -2, 1], axis, ...)`。换言之，`laplace` 只是 `generic_laplace` 的一个「具名预设」。

#### 4.2.2 核心流程

`generic_laplace` 的流程：

```text
if extra_keywords is None: extra_keywords = {}
input = np.asarray(input)
output = _get_output(output, input)
axes = _check_axes(axes, input.ndim)                # 规范 axes（None→全部、去重、转非负）
if len(axes) > 0:
    modes = _normalize_sequence(mode, len(axes))
    # 第一轴：直接写入 output
    derivative2(input, axes[0], output, modes[0], cval, *extra_args, **extra_kw)
    # 其余轴：算到临时数组 tmp，累加进 output
    for ii in range(1, len(axes)):
        tmp = derivative2(input, axes[ii], output.dtype, modes[ii], cval, ...)
        output += tmp
else:
    output[...] = input[...]                        # 没有要算的轴：原样拷贝
return output
```

> 注意一个易错点：**每一次** `derivative2` 的输入都是**原始 `input`**，而不是累积中的 `output`。累加只发生在 `output += tmp` 这一步。这是对的——拉普拉斯是「各轴二阶导之和」，每个二阶导都应基于原图。

`laplace` 的填法极简：

```text
def derivative2(input, axis, output, mode, cval):
    return correlate1d(input, [1, -2, 1], axis, output, mode, cval, 0)
return generic_laplace(input, derivative2, output, mode, cval, axes=axes)
```

#### 4.2.3 源码精读

`generic_laplace` 引擎本体：

[`_filters.py:1018-1033`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1018-L1033) —— 第一轴把二阶导写进 `output`；后续每轴算出 `tmp`（用 `output.dtype` 当 output 形参，从而得到一个新临时数组）再 `output += tmp` 累加。

```python
if extra_keywords is None:
    extra_keywords = {}
input = np.asarray(input)
output = _ni_support._get_output(output, input)
axes = _ni_support._check_axes(axes, input.ndim)
if len(axes) > 0:
    modes = _ni_support._normalize_sequence(mode, len(axes))
    derivative2(input, axes[0], output, modes[0], cval,
                *extra_arguments, **extra_keywords)
    for ii in range(1, len(axes)):
        tmp = derivative2(input, axes[ii], output.dtype, modes[ii], cval,
                          *extra_arguments, **extra_keywords)
        output += tmp
else:
    output[...] = input[...]
return output
```

逐行要点：

- `derivative2(input, axes[0], output, ...)`：这里第 3 个位置参数是 `output`（真输出数组），所以这一轴的二阶导直接落盘到输出。
- `derivative2(input, axes[ii], output.dtype, ...)`：第 3 个参数是 `output.dtype`（一个类型对象），按 u1-l4 的 `_get_output` 约定，这表示「按此 dtype 新建一个数组」并返回——于是得到临时 `tmp`，不污染 `output`。
- `output += tmp`：把该轴二阶导累加进输出，完成 \(\sum_i \partial^2/\partial x_i^2\)。
- `extra_arguments` / `extra_keywords`：让调用者（如 `gaussian_laplace`）能把额外参数（例如 `sigma`）透传给 `derivative2`。

`laplace` 只是给引擎喂一个最朴素的二阶差分：

[`_filters.py:1069-1071`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1069-L1071) —— 内部 `derivative2` 就是一句 `[1,-2,1]` 相关；整个 `laplace` 函数体实质只有「定义回调 + 调引擎」两步。

```python
def derivative2(input, axis, output, mode, cval):
    return correlate1d(input, [1, -2, 1], axis, output, mode, cval, 0)
return generic_laplace(input, derivative2, output, mode, cval, axes=axes)
```

#### 4.2.4 代码实践

1. **目标**：验证 `laplace` = 各轴 `[1,-2,1]` 之和，并观察二阶导在阶跃边缘的**零交叉**。
2. **操作步骤**（示例代码）：

   ```python
   import numpy as np
   from scipy import ndimage

   # 同 4.1.4 的垂直阶跃边缘图
   img = np.zeros((64, 64), dtype=float)
   img[:, 32:] = 100.0

   lap = ndimage.laplace(img)

   # 取中间一行，看边缘附近的二阶导响应
   row = 32
   print("laplace cols 29..34:", lap[row, 29:35])
   ```
3. **观察现象**：在边缘处 `laplace` 出现「`+100`（第 31 列）紧接 `-100`（第 32 列）」、两侧平坦区为 0 的形态——正峰与负峰之间的**符号反转点（零交叉）**恰好在第 31 与第 32 列之间，即边缘所在位置。
4. **预期结果**：`lap[row, 29:35]` 大致为 `[0, 0, 100, -100, 0, 0]`（对应列 29–34；精确值受边界影响，**待本地验证**，但中间行的 +100/-100 对是确定的）。
5. **为什么是 +100/-100**：对阶跃 0→100，`[1,-2,1]` 在跳变左侧给出 `0 - 0 + 100 = +100`，在跳变右侧给出 `0 - 200 + 100 = -100`，其余为 0。本例沿 `axis=0`（行方向）恒定，故行向二阶导为 0，整个 `laplace` 只剩列向二阶导。

#### 4.2.5 小练习与答案

**练习 1**：`laplace` 在二维图上等于「行向二阶导 + 列向二阶导」。如果一张图沿行方向是恒定的（每行相同），`laplace` 的行向分量会是多少？

> **答案**：为 0。沿恒定方向做 `[1,-2,1]` 相关，任何位置的 `a - 2a + a = 0`。因此对「只在列方向有变化」的图，`laplace` 退化为单纯的列向二阶差分，正如 4.2.4 中所见。

**练习 2**：`generic_laplace` 里第一轴调用 `derivative2(..., output, ...)`，后续轴调用 `derivative2(..., output.dtype, ...)`。为什么不能对**所有**轴都用 `output` 当输出？

> **答案**：第一轴把结果写进 `output` 后，`output` 已经持有「轴 0 的二阶导」。若第二轴仍把结果写进 `output`，就会**覆盖**而非累加轴 0 的结果，最终 `output` 只剩最后一轴的二阶导，丢了其他轴。用 `output.dtype` 让后续轴写到**新临时数组** `tmp`，再 `output += tmp` 才能正确求和。

---

### 4.3 `gaussian_laplace` / `generic_gradient_magnitude` / `gaussian_gradient_magnitude`：高斯导数的组合

#### 4.3.1 概念说明

把 4.2 的「二阶导引擎」和 u2-l3 的「高斯导数核」拼起来，就得到一族更强的边缘算子：

- **`gaussian_laplace`（LoG）**：把 `generic_laplace` 的 `derivative2` 换成「`gaussian_filter` 沿该轴取 `order=2`」（即高斯二阶导核）。由 2.4 节，这等价于「先高斯平滑、再拉普拉斯」。`sigma` 越大，平滑越强，边缘响应越宽、越低、越抗噪，但定位精度下降。
- **`generic_gradient_magnitude`**：另一个通用引擎，计算梯度**模长** \(\sqrt{\sum_i (\partial I/\partial x_i)^2}\)。它与 `generic_laplace` 的区别在于：先把每条轴的一阶导**平方**，再相加，最后开根号。
- **`gaussian_gradient_magnitude`**：把上面引擎的 `derivative` 换成「`gaussian_filter` 沿该轴取 `order=1`」（高斯一阶导核）。结果恒非负，给出「边缘强度图」。

一句话区分三者的「数学形状」：

| 算子 | 各轴用什么 | 怎么合并 | 结果符号 |
| --- | --- | --- | --- |
| `laplace` / `gaussian_laplace` | 二阶导（`[1,-2,1]` / 高斯 order=2） | 直接求和 | 可正可负（有零交叉） |
| `generic_gradient_magnitude` / `gaussian_gradient_magnitude` | 一阶导（差分 / 高斯 order=1） | 平方求和再开根号 | 恒 ≥ 0（边缘强度） |

#### 4.3.2 核心流程

`gaussian_laplace` 的组装（注意它如何处理 `axes` 子集下的 `sigma`）：

```text
input = np.asarray(input)
def derivative2(input, axis, output, mode, cval, sigma, **kwargs):
    order = [0]*input.ndim; order[axis] = 2          # 只在该轴取二阶导
    return gaussian_filter(input, sigma, order, output, mode, cval, **kwargs)

axes = _check_axes(axes, input.ndim)
sigma = _normalize_sequence(sigma, len(axes))        # 逐轴 sigma
if len(axes) < input.ndim:                           # 把未滤波轴的 sigma 置 0
    sigma_temp = [0]*input.ndim
    for s, ax in zip(sigma, axes): sigma_temp[ax] = s
    sigma = sigma_temp

return generic_laplace(input, derivative2, output, mode, cval,
                       extra_arguments=(sigma,), extra_keywords=kwargs, axes=axes)
```

`generic_gradient_magnitude` 引擎本体（与 `generic_laplace` 结构相似，但多一步「平方」与「开根号」）：

```text
if extra_keywords is None: extra_keywords = {}
input = np.asarray(input); output = _get_output(output, input)
axes = _check_axes(axes, input.ndim)
if len(axes) > 0:
    modes = _normalize_sequence(mode, len(axes))
    derivative(input, axes[0], output, modes[0], cval, ...)   # 第一轴一阶导写进 output
    np.multiply(output, output, output)                       # output = output^2
    for ii in range(1, len(axes)):
        tmp = derivative(input, axes[ii], output.dtype, modes[ii], cval, ...)
        np.multiply(tmp, tmp, tmp)                            # tmp = tmp^2
        output += tmp                                         # 累加平方
    np.sqrt(output, output, casting='unsafe')                 # 开根号
else:
    output[...] = input[...]
return output
```

`gaussian_gradient_magnitude` 的填法（与 `gaussian_laplace` 几乎对称，只把 `order` 从 2 改成 1、引擎换成 `generic_gradient_magnitude`）：

```text
def derivative(input, axis, output, mode, cval, sigma, **kwargs):
    order = [0]*input.ndim; order[axis] = 1
    return gaussian_filter(input, sigma, order, output, mode, cval, **kwargs)
return generic_gradient_magnitude(input, derivative, output, mode, cval,
                                  extra_arguments=(sigma,), extra_keywords=kwargs, axes=axes)
```

#### 4.3.3 源码精读

`gaussian_laplace` 的内部 `derivative2` 与 `sigma` 适配：

[`_filters.py:1118-1139`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1118-L1139) —— `derivative2` 用「该轴 `order=2`、其余轴 `order=0`」调 `gaussian_filter`，即高斯二阶导核；随后把 `sigma` 透传给引擎，并补齐未滤波轴的 `sigma=0`。

```python
input = np.asarray(input)

def derivative2(input, axis, output, mode, cval, sigma, **kwargs):
    order = [0] * input.ndim
    order[axis] = 2
    return gaussian_filter(input, sigma, order, output, mode, cval,
                           **kwargs)

axes = _ni_support._check_axes(axes, input.ndim)
num_axes = len(axes)
sigma = _ni_support._normalize_sequence(sigma, num_axes)
if num_axes < input.ndim:
    # set sigma = 0 for any axes not being filtered
    sigma_temp = [0,] * input.ndim
    for s, ax in zip(sigma, axes):
        sigma_temp[ax] = s
    sigma = sigma_temp

return generic_laplace(input, derivative2, output, mode, cval,
                       extra_arguments=(sigma,),
                       extra_keywords=kwargs,
                       axes=axes)
```

要点：

- `order = [0]*ndim; order[axis]=2`：构造「只在 `axis` 上取 2、其余轴取 0」的逐轴 order 序列，交给 `gaussian_filter`（回顾 u2-l3：`order=0` 是平滑、`order=2` 是二阶导）。这正是 LoG 里「高斯二阶导核」的来源。
- `sigma_temp` 把 `sigma=0` 填到未滤波轴，等价于「这些轴不做高斯平滑」——与 u2-l3 `gaussian_filter` 里 `sigma≈0` 的轴被跳过一致。
- `extra_arguments=(sigma,)`：把逐轴 `sigma` 通过 `generic_laplace` 的 `*extra_arguments` 透传给 `derivative2`。

`generic_gradient_magnitude` 引擎本体：

[`_filters.py:1177-1196`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1177-L1196) —— 与 `generic_laplace` 同构，但每算完一条轴的一阶导就**就地平方**（`np.multiply(x, x, x)`），最后 `np.sqrt(output, output, casting='unsafe')` 得到梯度模长。

```python
if extra_keywords is None:
    extra_keywords = {}
input = np.asarray(input)
output = _ni_support._get_output(output, input)
axes = _ni_support._check_axes(axes, input.ndim)
if len(axes) > 0:
    modes = _ni_support._normalize_sequence(mode, len(axes))
    derivative(input, axes[0], output, modes[0], cval,
               *extra_arguments, **extra_keywords)
    np.multiply(output, output, output)
    for ii in range(1, len(axes)):
        tmp = derivative(input, axes[ii], output.dtype, modes[ii], cval,
                         *extra_arguments, **extra_keywords)
        np.multiply(tmp, tmp, tmp)
        output += tmp
    # This allows the sqrt to work with a different default casting
    np.sqrt(output, output, casting='unsafe')
else:
    output[...] = input[...]
return output
```

逐行对比 `generic_laplace`：

| 步骤 | `generic_laplace` | `generic_gradient_magnitude` |
| --- | --- | --- |
| 各轴用什么 | `derivative2`（二阶导） | `derivative`（一阶导） |
| 合并方式 | `output += tmp`（直接和） | 先 `np.multiply` 平方，再 `output += tmp`，最后 `np.sqrt` |
| 结果符号 | 可正可负 | 恒非负 |

注意那条注释 `# This allows the sqrt to work with a different default casting`：若 `output` 是整型，`np.sqrt` 默认的 `casting` 规则可能拒绝把浮点结果写回整型数组，显式传 `casting='unsafe'` 才能让整型输出也得到（截断的）平方根结果。

`gaussian_gradient_magnitude` 的填法（与 `gaussian_laplace` 对称，`order` 由 2 变 1、引擎换成梯度模长）：

[`_filters.py:1240-1250`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_filters.py#L1240-L1250) —— `derivative` 用「该轴 `order=1`」调 `gaussian_filter`，交给 `generic_gradient_magnitude`。

```python
input = np.asarray(input)

def derivative(input, axis, output, mode, cval, sigma, **kwargs):
    order = [0] * input.ndim
    order[axis] = 1
    return gaussian_filter(input, sigma, order, output, mode,
                           cval, **kwargs)

return generic_gradient_magnitude(input, derivative, output, mode,
                                  cval, extra_arguments=(sigma,),
                                  extra_keywords=kwargs, axes=axes)
```

> 一个值得注意的不对称：`gaussian_gradient_magnitude` **没有**像 `gaussian_laplace` 那样显式把未滤波轴的 `sigma` 置 0。它直接把标量/序列 `sigma` 透传给 `gaussian_filter`，由 `gaussian_filter` 内部（u2-l3 的 `if sigmas[ii] > 1e-15`）去跳过 `sigma≈0` 的轴。两条路径效果一致，只是写法不同。

#### 4.3.4 代码实践

1. **目标**：观察 `gaussian_laplace` 的 `sigma` 如何同时影响「平滑强度」与「边缘定位」，并对比 `gaussian_gradient_magnitude` 的非负响应。
2. **操作步骤**（示例代码）：

   ```python
   import numpy as np
   from scipy import ndimage

   img = np.zeros((64, 64), dtype=float)
   img[:, 32:] = 100.0
   row = 32

   for sigma in (0.5, 1.0, 3.0):
       log = ndimage.gaussian_laplace(img, sigma=sigma)
       # 在中间行找到正峰与负峰的列，二者之间即零交叉（边缘）
       cols_pos = np.argmax(log[row])
       cols_neg = np.argmin(log[row])
       print(f"sigma={sigma}: +peak@col{cols_pos} ({log[row, cols_pos]:+.1f}), "
             f"-peak@col{cols_neg} ({log[row, cols_neg]:+.1f}), "
             f"零交叉介于二者之间")

   ggm = ndimage.gaussian_gradient_magnitude(img, sigma=1.0)
   print("ggm 最小值（应 >= 0）:", ggm.min(), " 边缘处强度:", ggm[row, 31:34])
   ```
3. **观察现象**：
   - `gaussian_laplace` 始终是「正峰 + 负峰」夹着边缘，**零交叉位置稳定在第 31/32 列之间**，与 `sigma` 无关；但 `sigma` 越大，正负峰的**幅值越小、宽度越大**（平滑把尖峰摊开了）。
   - `gaussian_gradient_magnitude` 全图非负，在边缘处形成一个单峰（强度最大），平坦处趋近 0。
4. **预期结果**：三个 `sigma` 的零交叉都落在同一边缘位置；`ggm.min() >= 0` 恒成立（精确幅值**待本地验证**）。
5. **结论**：`sigma` 是「抗噪 vs 定位精度」的旋钮——零交叉定位由边缘本身决定（鲁棒），但响应的锐利程度随 `sigma` 增大而下降。

#### 4.3.5 小练习与答案

**练习 1**：`gaussian_laplace` 内部 `derivative2` 里 `order[axis]=2`、其余轴 `order=0`。如果把**所有**轴都设成 `order=2`，结果会变成什么？

> **答案**：`gaussian_filter` 会对每条轴都做高斯二阶导。但 `generic_laplace` 是「逐轴算二阶导再求和」，每次只针对一条轴；若在 `derivative2` 内部把所有轴都置 2，则单次调用就已经是「全轴二阶导之和」（拉普拉斯）的一次性卷积，后续 `output += tmp` 会把这个和**重复累加** N 次，结果被放大 N 倍且语义错乱。所以必须严格「当前轴 2、其余轴 0」。

**练习 2**：为什么 `generic_gradient_magnitude` 在算 `np.sqrt` 时要显式写 `casting='unsafe'`，而 `generic_laplace` 完全不出现 `sqrt`？

> **答案**：梯度模长定义为 \(\sqrt{\sum (\partial I/\partial x_i)^2}\)，必须开根号；当输出数组是整型时，`np.sqrt` 默认的 `casting='same_kind'` 会拒绝把浮点结果写回整型而报错，故用 `casting='unsafe'` 放宽。拉普拉斯是「二阶导之和」，无开根号步骤，自然不涉及 `sqrt` 与类型转换问题。

---

## 5. 综合实践

把本讲三个模块串起来：**对同一张含阶跃边缘的图，分别用「一阶路线」和「二阶路线」检测边缘，并把 `gaussian_laplace` 的零交叉与 `sobel` 的极值画到一起对照**。

任务步骤（示例代码）：

```python
import numpy as np
from scipy import ndimage

# 1. 造一张含一条垂直阶跃边缘的 64x64 图
img = np.zeros((64, 64), dtype=float)
img[:, 32:] = 100.0

# 2. 一阶路线：sobel 沿 axis=1（列方向）求梯度
sobel_resp = ndimage.sobel(img, axis=1)              # 边缘处取极值

# 3. 二阶路线：laplace 与 gaussian_laplace
lap_resp = ndimage.laplace(img)                      # 边缘处零交叉
log_resp = ndimage.gaussian_laplace(img, sigma=2.0)  # 平滑后的零交叉

# 4. 边缘强度（非负）：gaussian_gradient_magnitude
ggm_resp = ndimage.gaussian_gradient_magnitude(img, sigma=2.0)

row = 32
# 在中间行打印各算子在边缘附近的响应，定位「边缘列」
for name, r in [("sobel(一阶)", sobel_resp),
                ("laplace(二阶)", lap_resp),
                ("gaussian_laplace", log_resp),
                ("gaussian_gradient_mag", ggm_resp)]:
    cols_3034 = r[row, 30:35].round(1)
    print(f"{name:24s} cols30..34 = {cols_3034}")
```

完成后请回答：

- `sobel` 与 `gaussian_gradient_magnitude`（都走一阶路线）的响应**符号**有何共性？（都反映「边缘强度」，`sobel` 可正可负取决于差分方向，`gaussian_gradient_magnitude` 恒非负。）
- `laplace` 与 `gaussian_laplace`（都走二阶路线）的响应**形状**有何共性？（都是「正峰紧接负峰」、在边缘处零交叉；`gaussian_laplace` 因先平滑，峰更宽更平。）
- 把 `img` 换成 `datasets.ascent()`（`from scipy import datasets`），重跑上述四行，对照各算子在真实图像上的差异，体会 `sigma` 对 `gaussian_laplace` / `gaussian_gradient_magnitude` 平滑程度的影响。

这个综合实践一次性覆盖了：① `sobel` 的「沿轴差分 + 垂直平滑」；② `laplace`/`generic_laplace` 的「二阶导之和」引擎；③ `gaussian_laplace`/`gaussian_gradient_magnitude` 对 `gaussian_filter(order=...)` 的组合复用；④ 一阶「找极值」与二阶「找零交叉」两条边缘检测路线的对照。

## 6. 本讲小结

- `prewitt` 与 `sobel` 都不是纯差分，而是「沿 `axis` 用 `[-1,0,1]` 做一阶中心差分 + 沿其余每轴就地平滑」的可分离组合；二者唯一区别是平滑核 `[1,1,1]`（prewitt）与 `[1,2,1]`（sobel），且都未归一化。
- `generic_laplace` 是「各轴二阶导之和」的通用引擎：第一轴写进 `output`，后续每轴算到临时 `tmp` 再 `output += tmp`；每次输入都是原图。`laplace` 只是给它喂一个 `[1,-2,1]` 的 `derivative2`。
- `laplace` 在阶跃边缘处呈「正峰 + 负峰」并在边缘位置**零交叉**；平坦区为 0。
- `gaussian_laplace`（LoG）= `generic_laplace` + 「该轴 `gaussian_filter(order=2)`」；`sigma` 越大边缘响应越宽越平，但零交叉定位不变（由边缘本身决定）。
- `generic_gradient_magnitude` 是「各轴一阶导平方和开根号」的引擎，结果恒非负；它与 `generic_laplace` 的根本差别是「平方后求和 vs 直接求和」。
- `gaussian_gradient_magnitude` = `generic_gradient_magnitude` + 「该轴 `gaussian_filter(order=1)`」，给出非负的边缘强度图。

## 7. 下一步学习建议

本讲把「高斯导数核」与「差分核」组合成了边缘算子，并见识了 `generic_laplace` / `generic_gradient_magnitude` 这种「通用引擎 + 回调」的设计。建议接着学习：

- **u2-l5 秩/选择/通用/向量化滤波**：认识 `generic_filter` / `generic_filter1d`——它们把「任意 Python（或 `LowLevelCallable`）回调」塞进邻域循环，是比本讲更通用的「回调式」滤波；本讲的两个 `generic_*` 引擎是它的简化版（回调只沿单轴作用）。
- **u2-l6 频域（傅里叶）滤波**：从空域（卷积）跳到频域（乘法核），看 `fourier_gaussian` 等如何在 FFT 域实现等价的高斯平滑/微分，与本讲的 `gaussian_filter(order=...)` 互为对照。
- 想下探到 C 层的读者，可在学完 u2-l5 后预习专家单元 u6 的 `ni_filters.c`，理解本讲那些 `correlate1d` 调用在 C 端是如何被 `NI_LineBuffer` + 边界扩展逐行执行的。
