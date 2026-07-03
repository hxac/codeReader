# 仿射变换 affine_transform

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `affine_transform` 采用的 **pull（反向）重采样模型**：对每个输出像素 `o`，先由 `matrix·o + offset` 算出它对应的输入坐标，再在该坐标做样条插值。
- 识别 `matrix` 参数的 **四种合法形状**——对角 `(ndim,)`、方阵 `(ndim,ndim)`、齐次 `(ndim+1,ndim+1)`、增广 `(ndim,ndim+1)`——以及源码如何逐一归约它们。
- 解释为何对角矩阵会走一条 **更快的快速路径** `_nd_image.zoom_shift`，并理解 Python 端那句看似奇怪的 `offset/matrix` 是怎么来的。
- 能够独立用 `affine_transform` 实现缩放、平移、绕中心旋转，并能用 `output_shape` 扩展画布。

## 2. 前置知识

本讲承接 u3-l1（样条预滤波）与 u3-l2（`map_coordinates`），假定你已经熟悉以下概念（若不熟请先回看）：

- **样条阶数 order（0–5）** 与 **预滤波 prefilter**：`order>1` 时需要先把样本反解为样条系数，才能让插值曲线经过样本点。本讲函数的 `prefilter=True`（默认）会在内部自动完成这一步。
- **拉（pull）重采样**：输出数组逐点追问“我这个像素的值应该来自输入的哪个坐标？”，再用插值读取。这与“推（push）”方向相反。
- **边界模式 mode**：越界坐标如何取值（`constant`/`nearest`/`wrap`/`grid-wrap` 等）。插值场景下 `constant` 与 `grid-constant` 不等价（见 u3-l2）。
- **共享支撑工具**：`_get_output`（处理 `output` 的 None/dtype/数组三种形态）、`_normalize_sequence`（标量广播成各维序列）、`_extend_mode_to_code`（模式串→整数码），见 u1-l4。

此外需要一点点线性代数：矩阵-向量乘法、齐次坐标（用 `(ndim+1)` 维向量把“平移”也写成矩阵乘法）、二维旋转矩阵。

\[
R(\theta)=\begin{bmatrix}\cos\theta & -\sin\theta\\ \sin\theta & \cos\theta\end{bmatrix}
\]

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_interpolation.py](_interpolation.py) | Python 包装层。`affine_transform`（L480–L655）是本讲主角；`shift`/`zoom`/`rotate` 都在内部复用它。 |
| src/ni_interpolation.c | C 内核。`NI_GeometricTransform`（L256 起）承载通用 2-D 仿射；`NI_ZoomShift`（L660 起）承载对角快速路径。 |
| src/nd_image.c | C 扩展入口。`Py_ZoomShift`（L787）解析参数并调用 `NI_ZoomShift`；`methods[]` 表（L1338）把 Python 名 `zoom_shift` 映射到该包装函数。 |

> 提示：本讲只读 `_interpolation.py` 与少量 C 代码。C 部分只需看懂“坐标怎么算”，不必深究样条权重与边界细节（那是 u6-l3 的内容）。

## 4. 核心概念与源码讲解

### 4.1 affine_transform 函数与 pull 重采样模型

#### 4.1.1 概念说明

**仿射变换（affine transform）** = “线性变换 + 平移”。在二维里，它涵盖缩放、旋转、剪切、翻转、平移及其任意组合。数学上写作：

\[
\mathbf{x}_{\text{in}} = A\,\mathbf{o} + \mathbf{b}
\]

其中 \(\mathbf{o}\) 是输出像素的索引向量，\(A\) 是 `matrix`，\(\mathbf{b}\) 是 `offset`，\(\mathbf{x}_{\text{in}}\) 是该输出像素应当去读取的输入坐标。

`affine_transform` 走的是 **pull（反向）重采样**：它遍历 *输出* 数组的每个像素，反问“我的值来自输入的哪个位置？”，再到输入里用样条插值读出来。这与日常描述旋转时习惯的 push（正向，把输入像素搬到输出）方向相反。文档明确提醒：如果你手上是一个 push 方向的矩阵，要先求逆 `numpy.linalg.inv(matrix)` 再传进来。

#### 4.1.2 核心流程

`affine_transform` 的执行流程（参数校验与装配基本与 `map_coordinates` 同构，见 u3-l2）：

1. 校验 `order` ∈ [0,5]；确定 `output_shape`（`None` 时取 `output.shape` 或 `input.shape`）。
2. 复数输入 → 拆实部、虚部递归调用自身。
3. **预滤波装配块**：`prefilter and order>1` 时，按 mode 决定是否 `_prepad_for_spline_filter` 补 12 点，再 `spline_filter` 求样条系数（强制 float64）。
4. `mode` 经 `_extend_mode_to_code` 编成整数码交给 C。
5. **归约 matrix**：按四种形状把 `matrix`/`offset` 统一成“方阵 + 平移”或“对角 + 平移”（详见 4.2）。
6. **分派**：对角矩阵走快速路径 `_nd_image.zoom_shift`；其余走通用 `_nd_image.geometric_transform`。

#### 4.1.3 源码精读

函数签名与 pull 模型的官方表述（这段是理解全函数的钥匙）：

[_interpolation.py:L480-L494](_interpolation.py#L480-L494) —— `affine_transform(input, Matrix, offset=0.0, output_shape=None, output=None, order=3, mode='constant', cval=0.0, prefilter=True)`，文档第一句即 `np.dot(matrix, o) + offset`，并点明这是 pull 方向。

下面这段是 **与 `map_coordinates` 共享的前置装配**（order 校验、output_shape、复数拆分、预滤波、mode 编码）。我们只看结构，细节已在 u3-l2 讲过：

[_interpolation.py:L593-L620](_interpolation.py#L593-L620) —— 依次完成：order 校验 → `output_shape` 缺省推导（`output` 为数组时取其形状，否则取 `input.shape`）→ 复数输入递归 → 预滤波块 → mode 编码。注意 `output_shape` 可以与输入形状不同，这是 `affine_transform` 区别于纯滤波器、能“换画布”的关键。

`offset` 的规范化（标量广播为各维序列、强制 float64、保证连续内存）：

[_interpolation.py:L642-L647](_interpolation.py#L642-L647) —— `offset = _ni_support._normalize_sequence(offset, input.ndim)`，再 `np.asarray(..., dtype=np.float64)`，非连续则 `.copy()`。

> 这一步之前还插着对 `matrix` 形状的归约（4.2 节），紧接着就是 4.3 节的分派。两个 `if` 分支决定了走哪条 C 内核。

#### 4.1.4 代码实践：用最小例子验证 pull 模型

**目标**：用一个 1-D 数组，亲手验证 `output[o] = input[matrix·o + offset]`。

**操作步骤**：

```python
import numpy as np
from scipy.ndimage import affine_transform

a = np.arange(6)                      # [0, 1, 2, 3, 4, 5]
# 对角 matrix=1，offset=1：input_coord = 1*o + 1 = o + 1
out = affine_transform(a, matrix=1, offset=1, order=1)
print(out)
```

**需要观察的现象**：每个输出像素读取的是输入的 `o+1` 位置，于是内容整体向索引 0 方向“左移”一位。

**预期结果**：`out = [1, 2, 3, 4, 5, 0]`。前 5 个像素分别为 `a[1..5]`；`out[5]` 对应 `a[6]` 越界，默认 `mode='constant'`、`cval=0` 故取 0。此处用 `order=1` 且坐标均为整数，无插值误差，可手算复核。

**反向验证 push 直觉**：把 `offset` 改成 `-1`，应得到 `[0, 0, 1, 2, 3, 4]`（`out[0]=a[-1]` 越界→0，`out[1]=a[0]=0`，依此类推）。

#### 4.1.5 小练习与答案

**练习 1**：若把上例的 `matrix` 从 `1` 改成 `2`（仍 `offset=1`），`out` 会是什么？

**答案**：`input_coord = 2·o + 1`，即 `out[0]=a[1]=1, out[1]=a[3]=3, out[2]=a[5]=5`，`out[3..5]` 越界取 0，故 `out = [1, 3, 5, 0, 0, 0]`（待本地验证）。

**练习 2**：为什么文档说“手上的 push 矩阵要先求逆”？

**答案**：pull 模型遍历输出、反查输入，用的是“输出→输入”的映射，即 push（“输入→输出”）映射的逆。所以若你按习惯写的是正向变换矩阵，需 `np.linalg.inv` 后再传入。

---

### 4.2 matrix 的四种形状分支

#### 4.2.1 概念说明

为了同时照顾“最简单的对角缩放”和“最一般的齐次坐标”，`matrix` 接受四种形状（设 `ndim = input.ndim`）：

| 形状 | 含义 | 走哪条路径 |
| --- | --- | --- |
| `(ndim,)` | 对角矩阵：各轴独立缩放 | 快速路径 `zoom_shift`（见 4.3） |
| `(ndim, ndim)` | 一般线性变换矩阵 \(A\) | 通用 `geometric_transform` |
| `(ndim+1, ndim+1)` | 齐次坐标矩阵，底行须为 `[0,…,0,1]` | 归约成方阵后走通用路径 |
| `(ndim, ndim+1)` | 齐次矩阵省略底行 | 同上 |

**齐次坐标** 的妙处：把平移也塞进矩阵乘法。一个 \((n+1)\) 维向量 \((o_1,\dots,o_n,1)^\top\) 左乘 \((n+1)\times(n+1)\) 矩阵，最后一列天然给出 offset。对 \((ndim+1, ndim+1)\) 形状，函数会 **忽略你传入的 `offset`**，直接从矩阵最后一列读取。

#### 4.2.2 核心流程

源码用一段嵌套 `if` 把后三种形状归约到“方阵 + 平移”：

1. `matrix = np.asarray(matrix, dtype=np.float64)`，先做基础校验：`ndim` 必须 ∈ {1,2} 且 `shape[0] >= 1`。
2. **齐次分支判定**：`shape[1] == ndim+1` 且 `shape[0] ∈ {ndim, ndim+1}` 时进入归约。
3. 若 `shape[0] == ndim+1`（完整齐次），校验底行等于 `[0,…,0,1]`，否则 `ValueError`。
4. 不论是 \((ndim, ndim+1)\) 还是 \((ndim+1, ndim+1)\)：`offset = matrix[:ndim, ndim]`（最后一列前 ndim 行），`matrix = matrix[:ndim, :ndim]`（左上 ndim×ndim 块）。
5. 兜底校验：归约后方阵的行数须等于 `ndim`，列数须等于 `output.ndim`。

归约后，matrix 要么是 1-D（对角），要么是 `(ndim,ndim)` 方阵，交给 4.3 的分派逻辑。

#### 4.2.3 源码精读

基础校验与齐次分支入口：

[_interpolation.py:L621-L625](_interpolation.py#L621-L625) —— `matrix` 转 float64；`ndim not in [1,2]` 或行数 `<1` 直接报错；进入齐次分支的判定条件。

齐次底行校验与 offset/matrix 提取（这是齐次形状的核心）：

[_interpolation.py:L626-L635](_interpolation.py#L626-L635) —— 对 \((ndim+1,ndim+1)\) 校验底行；随后 `offset = matrix[:input.ndim, input.ndim]`、`matrix = matrix[:input.ndim, :input.ndim]`，把齐次矩阵压缩回方阵 + 平移。

兜底形状校验：

[_interpolation.py:L636-L641](_interpolation.py#L636-L641) —— 行数必须等于 `input.ndim`；二维时列数必须等于 `output.ndim`；非连续则 `.copy()`。

> 注意 `(ndim,ndim)`（普通方阵）**不会**触发齐次分支（因其 `shape[1] != ndim+1`），原样进入兜底校验后走通用路径。

#### 4.2.4 代码实践：四种形状做同一件事

**目标**：用四种 `matrix` 形状表达同一个仿射——轴 0 缩放 2 倍、轴 1 缩放 0.5 倍、平移 (1, 2)，并验证结果完全一致。

**操作步骤**：

```python
import numpy as np
from scipy.ndimage import affine_transform

img = np.arange(16, dtype=float).reshape(4, 4)
m_lin = np.array([[2.0, 0.0], [0.0, 0.5]])   # (ndim,ndim)
m_diag = np.array([2.0, 0.5])                # (ndim,)
m_hom  = np.array([[2.0, 0.0, 1.0],          # (ndim,ndim+1)
                   [0.0, 0.5, 2.0]])
m_hom2 = np.array([[2.0, 0.0, 1.0],          # (ndim+1,ndim+1)
                   [0.0, 0.5, 2.0],
                   [0.0, 0.0, 1.0]])
off = (1.0, 2.0)

r1 = affine_transform(img, m_lin,  offset=off, order=1)
r2 = affine_transform(img, m_diag, offset=off, order=1)
r3 = affine_transform(img, m_hom,  offset=off, order=1)   # offset 被忽略
r4 = affine_transform(img, m_hom2, offset=off, order=1)   # offset 被忽略
print(np.array_equal(r1, r2), np.array_equal(r1, r3), np.array_equal(r1, r4))
```

**需要观察的现象**：四次调用的输出逐元素相等。

**预期结果**：打印三个 `True`。注意齐次形状 `m_hom`/`m_hom2` 把平移 (1,2) 编码进了最后一列，所以即便我们仍传了 `offset=off`，它也会被忽略——这正是文档“any value passed to offset is ignored”的含义。精确数值待本地验证，但三组 `array_equal` 应全为 True。

#### 4.2.5 小练习与答案

**练习 1**：传一个 `(ndim+1, ndim+1)` 矩阵，但底行写成 `[0,0,0]`（缺末尾的 1），会发生什么？

**答案**：[_interpolation.py:L628-L632](_interpolation.py#L628-L632) 会抛 `ValueError`，提示底行应为 `[0,…,0,1]`。

**练习 2**：为什么 `(ndim, ndim+1)` 形状不需要校验底行？

**答案**：因为它本来就省略了底行（约定底行恒为 `[0,…,0,1]`），代码直接取前 `ndim` 行，没有“第 ndim+1 行”可校验。

---

### 4.3 对角快速路径：_nd_image.zoom_shift

#### 4.3.1 概念说明

当 `matrix` 是对角的 `(ndim,)` 时，各轴 **完全独立**：轴 i 的输入坐标只依赖轴 i 的输出坐标。这种 **可分离性** 让重采样可以逐轴处理，无需做完整的 N-D 矩阵-向量乘法，因而更快。`affine_transform` 为此专门分派到 `_nd_image.zoom_shift`——这个内核也被 `shift` 和 `zoom` 复用。

#### 4.3.2 核心流程：为什么是 offset/matrix？

`zoom_shift` 内核（非 grid_mode）按下面的公式计算每个轴的输入坐标：

\[
\text{cc} = \text{zoom} \times (\text{output\_index} + \text{shift})
\]

即它把“缩放”和“平移”耦合成了“先加 shift 再乘 zoom”。而 `affine_transform` 承诺的是：

\[
\mathbf{x}_{\text{in}} = \text{matrix}\odot\mathbf{o} + \text{offset}
\]

（\(\odot\) 表示逐元素，因为是对角情形。）要让两者相等，只需令 `zoom = matrix`，并解出 `shift`：

\[
\text{matrix}\odot(\mathbf{o} + \text{shift}) = \text{matrix}\odot\mathbf{o} + \text{offset}
\quad\Longrightarrow\quad
\text{shift} = \text{offset} \,/\, \text{matrix}
\]

这就是源码里那句 `offset/matrix` 的来历——它把通用语义“先缩放后平移”翻译成 `zoom_shift` 专用的“先平移后缩放”。

> 副作用：若某个 `matrix[i] == 0`（对角有零，意味着该维被压扁），`offset/matrix` 会得到 `inf`/`nan`。对角仿射本就不该出现零对角元，使用时需留意。

对于非对角矩阵，函数改走 `_nd_image.geometric_transform`，它直接做完整的矩阵-向量乘 `A·o + offset`，没有这种平移/缩放耦合，因而也无需除法。

#### 4.3.3 源码精读

Python 端的分派（本讲的总开关）：

[_interpolation.py:L648-L654](_interpolation.py#L648-L654) —— `matrix.ndim == 1` 走 `_nd_image.zoom_shift(filtered, matrix, offset/matrix, output, order, mode, cval, npad, False)`；否则走 `_nd_image.geometric_transform(filtered, None, None, matrix, offset, output, ...)`。注意后者把 `mapping=None`、坐标数组传 `None`，仅用 `matrix`+`offset` 两个参数激活 C 端的“仿射分支”。

C 端 `zoom_shift` 的坐标公式（确认 4.3.2 的推导）：

[src/ni_interpolation.c:L787-L808](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_interpolation.c#L787-L808) —— 内核对每个轴 `jj`、每个输出索引 `kk`：`cc = kk; if (shifts) cc += shift; if (zooms) cc *= zoom;`（非 grid_mode 分支），即 `cc = zoom*(kk+shift)`，再加 `nprepad`，再做边界映射与样条取值。这正是 `offset/matrix` 必要性的根源。

C 端通用路径的矩阵乘法（确认 pull 模型对方阵成立）：

[src/ni_interpolation.c:L424-L438](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_interpolation.c#L424-L438) —— `NI_GeometricTransform` 在 `matrix` 非空时，对每个输入轴 `hh` 累加 `tmp = shift[hh] + Σ_ll io.coordinates[ll]*p++`（`p` 遍历矩阵该行），结果写入 `icoor[hh]`。展开即 \(\mathbf{x}_{\text{in}} = A\,\mathbf{o} + \mathbf{b}\)。

扩展入口与参数解析（顺带印证“`shift`/`zoom` 均可为 NULL”）：

[src/nd_image.c:L787-L812](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L787-L812) —— `Py_ZoomShift` 用 `"O&O&O&O&iidii"` 解析 `input/zoom/shift/output`，其中 zoom、shift 用 `NI_ObjectToOptionalInputArray`（允许为 None）；这与 `shift()` 传 `zoom=None`、`zoom()` 传 `shift=None` 相呼应。

#### 4.3.4 代码实践：对角快速路径 ≡ 对角方阵

**目标**：验证“对角 `(ndim,)`”与等价的“对角方阵 `(ndim,ndim)`”给出完全相同的结果，并体会前者走更快路径。

**操作步骤**：

```python
import numpy as np, time
from scipy.ndimage import affine_transform

rng = np.random.default_rng(0)
img = rng.random((512, 512))
m_diag = np.array([0.7, 1.3])              # 快速路径
m_full = np.diag(m_diag)                   # 等价方阵，走通用路径
off = (5.0, -3.0)

r1 = affine_transform(img, m_diag, offset=off, order=3)
r2 = affine_transform(img, m_full,  offset=off, order=3)
print("identical:", np.array_equal(r1, r2))

# 粗略计时（仅观察量级差异，非严格 benchmark）
t = time.perf_counter()
for _ in range(5): affine_transform(img, m_diag, offset=off, order=3)
print("diag  :", time.perf_counter() - t)
t = time.perf_counter()
for _ in range(5): affine_transform(img, m_full, offset=off, order=3)
print("full  :", time.perf_counter() - t)
```

**需要观察的现象**：`identical: True`；对角路径的耗时通常不高于（往往明显低于）通用方阵路径。

**预期结果**：`np.array_equal` 返回 `True`；计时上对角路径应更快或持平（具体倍数随机器与 order 而异，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`shift()` 函数调用 `zoom_shift` 时传的 `zoom` 是什么？为什么？

**答案**：传 `None`（见 [_interpolation.py:L758-L759](_interpolation.py#L758-L759)），表示“不缩放、仅平移”，于是内核公式退化为 `cc = kk + shift`；其 `shift` 取的是用户平移量的相反数，以符合 pull 模型。

**练习 2**：为什么对角矩阵“可分离”就能更快？

**答案**：对角时各轴输入坐标只依赖本轴输出坐标，N-D 重采样可分解为逐轴 1-D 处理，缓存友好且避免了完整矩阵乘法；通用方阵每个输入坐标都要遍历全部输出坐标求内积，代价更高。

---

## 5. 综合实践：绕图像中心旋转 30° 并扩展画布

把本讲三块知识串起来——pull 模型、matrix 形状、output_shape——实现一个真实需求：把图像绕中心旋转 30°，且不让角被裁掉。

**思路**：绕中心旋转的 pull 映射为

\[
\mathbf{x}_{\text{in}} = R(-\theta)\,(\mathbf{o} - \mathbf{c}_{\text{out}}) + \mathbf{c}_{\text{in}}
= R(-\theta)\,\mathbf{o} + \bigl(\mathbf{c}_{\text{in}} - R(-\theta)\,\mathbf{c}_{\text{out}}\bigr)
\]

其中 \(R(-\theta)=\begin{bmatrix}\cos\theta & \sin\theta\\ -\sin\theta & \cos\theta\end{bmatrix}\)（与 scipy 内置 `rotate` 的 `rot_matrix` 一致）。故 `matrix = R(-θ)`，`offset = c_in - matrix·c_out`。扩展画布的大小由旋转后输入四角的外接矩形决定。

**操作步骤**：

```python
import numpy as np
import matplotlib.pyplot as plt
from scipy.ndimage import affine_transform
from scipy.datasets import face      # 首次调用会下载测试数据

img = face(gray=True)                # 2-D 灰度图，形状约 (768, 1024)
H, W = img.shape

theta = np.deg2rad(30)
c, s = np.cos(theta), np.sin(theta)
matrix = np.array([[ c, s],           # R(-theta)
                   [-s, c]])

# 旋转后输入四角的外接矩形 → 新画布大小（与 scipy.rotate 同法）
out_bounds = matrix @ np.array([[0, 0, H, H],
                                [0, W, 0, W]], dtype=float)
out_shape = tuple((np.ptp(out_bounds, axis=1) + 0.5).astype(int))

c_in  = (np.array(img.shape) - 1) / 2
c_out = (np.array(out_shape) - 1) / 2
offset = c_in - matrix @ c_out

rot = affine_transform(img, matrix, offset=offset,
                       output_shape=out_shape, order=3, mode='constant')

fig, ax = plt.subplots(1, 2, figsize=(10, 5))
ax[0].imshow(img, cmap='gray'); ax[0].set_title('original')
ax[1].imshow(rot, cmap='gray'); ax[1].set_title('rotated 30°')
plt.show()
print(img.shape, '->', rot.shape)
```

**需要观察的现象**：

- 旋转后内容相对画布居中（因为 offset 用了 `c_in - matrix·c_out`，把输出中心对齐到输入中心）。
- 画布变大、四个角未被裁切，越界区域填 0（黑边，`mode='constant'`）。
- `rot.shape` 比原图更大，且与 `out_shape` 一致。

**预期结果**：右侧图呈现逆时针旋转约 30°、居中、带黑边的图像；形状从约 `(768, 1024)` 变为更大的近似方形（精确尺寸待本地验证）。可对照 `scipy.ndimage.rotate(img, 30, reshape=True)` 的结果，二者几何关系一致。

**延伸**：把 `matrix` 换成齐次形状 `[hstack(matrix, offset); 0 0 1]` 再调用一次（不再传 offset），应得到逐像素相同的结果——这能同时验证 4.2 的齐次归约。

## 6. 本讲小结

- `affine_transform` 是 **pull（反向）重采样**：对每个输出像素 `o`，先算 `matrix·o + offset` 得到输入坐标，再样条插值读取；手上的 push 矩阵要先 `inv` 再传。
- `matrix` 接受 **四种形状**：对角 `(ndim,)`、方阵 `(ndim,ndim)`、齐次 `(ndim+1,ndim+1)`（须底行 `[0,…,0,1]`，忽略传入 offset）、增广 `(ndim,ndim+1)`；后两者在 [_interpolation.py:L624-L635](_interpolation.py#L624-L635) 被归约成方阵+平移。
- **对角矩阵走快速路径** `_nd_image.zoom_shift`；其余走 `_nd_image.geometric_transform` 的仿射分支，分派见 [_interpolation.py:L648-L654](_interpolation.py#L648-L654)。
- `offset/matrix` 那一行不是笔误：`zoom_shift` 内核公式是 `cc = zoom·(o+shift)`，要还原 `matrix·o + offset` 必须 `shift = offset/matrix`（见 [src/ni_interpolation.c:L787-L808](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_interpolation.c#L787-L808)）。
- `output_shape` 让输出画布可与输入不同，这是实现“旋转不裁切”“换分辨率”的关键。
- `shift`/`zoom`/`rotate` 都是对 `affine_transform`（进而对 `zoom_shift`/`geometric_transform` 两条内核）的便捷封装。

## 7. 下一步学习建议

- **下一篇 u3-l4**：精读 `geometric_transform`（任意 mapping 回调、`LowLevelCallable` 的 C 签名）以及 `shift`/`zoom`/`rotate` 如何各自构造 matrix/offset 并复用本讲两条内核，把“仿射”推广到“任意几何映射”。
- **下沉到 C（u6-l3）**：当你想彻底弄清样条权重如何逐坐标取值、边界如何扩展时，回头读 `ni_interpolation.c` 的 `NI_ZoomShift`/`NI_GeometricTransform` 完整实现。
- **建议阅读源码**：对照本讲的 [_interpolation.py:L648-L654](_interpolation.py#L648-L654)，自行追踪一次 `rotate()`（[_interpolation.py:L986-L1017](_interpolation.py#L986-L1017)）是如何算出 `rot_matrix` 与 `offset` 并最终调到 `affine_transform` 的，巩固 pull 模型的实战理解。
