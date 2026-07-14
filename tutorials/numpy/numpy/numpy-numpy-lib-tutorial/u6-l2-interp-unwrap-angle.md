# 插值与相位：interp / unwrap / angle

## 1. 本讲目标

本讲围绕 `numpy/lib/_function_base_impl.py` 中的四个「数值信号处理」函数展开。读完后你应该能够：

- 说清 `interp` 是如何做一维线性插值的，以及 `period` 参数如何把「角度插值」变成普通插值。
- 解释 `unwrap` 如何把一条发生 \(2\pi\) 跳变的相位序列「解卷绕」成连续曲线。
- 区分 `angle(deg=True)` 与 `angle(deg=False)` 的输出，并理解它对纯实数输入的处理。
- 理解 `sort_complex` 为什么对实数输入也返回复数类型，以及 numpy 复数排序的「先实部、后虚部」规则。

这四个函数都遵循本系列前面（u1-l2、u6-l1）建立的「dispatcher + impl 双函数」写法，并由顶层 `numpy/__init__.py` 直接挂到 `np.` 命名空间，不经薄再导出模块。本讲不再重复这套机制，而把重点放在四个函数各自的数值逻辑上。

## 2. 前置知识

- **线性插值**：已知两点 \((x_i, y_i)\) 与 \((x_{i+1}, y_{i+1})\)，要求它们之间某横坐标 \(x\) 处的纵坐标，就用直线连接两点：
  \[ y = y_i + \frac{y_{i+1}-y_i}{x_{i+1}-x_i}\,(x - x_i) \]
- **复数与辐角**：复数 \(z = a + b\mathrm{i}\) 可以看成复平面上的点 \((a, b)\)，其「辐角」就是从正实轴逆时针转到该向量的角度，范围 \((-\pi, \pi]\)，用 `arctan2(b, a)` 计算。
- **相位卷绕（wrapping）**：角度本质上是模 \(2\pi\) 的，所以 \(\pi\) 与 \(3\pi\)、\(-\pi\) 在「角度值」上不同，但在「方向」上等价。一条连续增长的相位序列经过 `np.angle` 或取模后，往往会出现 \(2\pi\) 的「跳变」，`unwrap` 就是用来把这些跳变补回去。
- **NEP-18 dispatcher 写法**：公开函数用 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只返回参与运算的数组参数，便于第三方数组类型通过 `__array_function__` 拦截。这一点 u1-l2 已详细讲过。
- **错位切片**：`unwrap` 内部复用了上一讲（u6-l1）的 `diff`，而 `diff` 的内核正是 `a[1:] - a[:-1]` 这对错位切片。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_function_base_impl.py` | 本讲四个函数的全部 Python 实现，是唯一需要精读的文件。 |
| `numpy/_core/multiarray.py`（C 内核 `interp` / `interp_complex`） | `interp` 真正的「二分搜索 + 线性插值」由 C 实现，Python 侧只负责参数预处理。本讲只引用其入口别名，不进入 C 源码。 |
| `numpy/lib/tests/test_function_base.py` | `sort_complex` 的单元测试，本讲用作实践依据。 |

四个函数在 `_function_base_impl.py` 中的大致位置：

- `_interp_dispatcher` / `interp`：约 L1545–L1686
- `_angle_dispatcher` / `angle`：约 L1689–L1744
- `_unwrap_dispatcher` / `unwrap`：约 L1747–L1863
- `_sort_complex` / `sort_complex`：约 L1866–L1905

## 4. 核心概念与源码讲解

### 4.1 interp：一维线性插值

#### 4.1.1 概念说明

`np.interp` 解决的问题是：已知一组**单调递增**的采样点 \((xp_i, fp_i)\)，给定一批新横坐标 `x`，求对应的纵坐标。它的策略非常朴素——**先用二分搜索定位 `x` 落在哪两个相邻采样点之间，再用线性插值公式算出结果**。超出采样范围的点分别用 `left`、`right` 兜底。

值得注意的是，`interp` 的「二分搜索 + 线性插值」内核是用 C 写的（`numpy/_core/multiarray` 模块里的 `interp` / `interp_complex`），Python 层只做三件事：

1. 判断 `fp` 是实数还是复数，选择对应的 C 内核；
2. 处理 `period`（周期）参数，把「角度插值」归一化成普通插值；
3. 把预处理后的数组交给 C 内核。

#### 4.1.2 核心流程

非周期（`period=None`）路径非常直接，完全由 C 内核完成：

```
x  ─┐
xp ─┼─► C 内核 interp(x, xp, fp, left, right)
fp ─┘   (内部对每个 x 二分定位 + 线性插值，越界用 left/right)
```

周期（`period` 非 `None`）路径多了一层「归一化」预处理，这是理解 `interp` 的关键：

```
1. period = abs(period)；left/right 被忽略（强制为 None）
2. x  = x  % period      # 把所有横坐标折进 [0, period)
   xp = xp % period
3. 按 xp 排序，fp 同步重排
4. 在两端各拼接一份「跨周期」的镜像点：
   xp = [xp[-1]-period, xp..., xp[0]+period]
   fp = [fp[-1],        fp..., fp[0]       ]
5. 交给 C 内核做普通插值
```

第 4 步的拼接是整个周期处理的精髓：把最后一个采样点「向左平移一个周期」放到队首、第一个采样点「向右平移一个周期」放到队尾，于是在区间两端各补了一个正确的外推锚点，C 内核无需任何改动就能正确处理「角度在 0 与 period 之间来回跳」的情形。

#### 4.1.3 源码精读

先看 dispatcher 与函数签名（标准 NEP-18 写法）：

[文件路径:L1545-L1550](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1545-L1550) — `_interp_dispatcher` 只返回 `(x, xp, fp)` 三个数组参数，`interp` 签名为 `interp(x, xp, fp, left=None, right=None, period=None)`。

实现体先做「实数 / 复数」分流，选出 C 内核：

[文件路径:L1653-L1660](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1653-L1660) — `fp = np.asarray(fp)`，若 `fp` 是复数对象则用 `compiled_interp_complex` 并把输入当作 `complex128`，否则用 `compiled_interp`（float64）。这里的 `compiled_interp` 其实就是文件头部 `from numpy._core.multiarray import ... interp as compiled_interp` 导入的 C 内核。

周期参数的预处理：

[文件路径:L1662-L1684](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1662-L1684) — 这一段是本函数最有信息量的部分。`period == 0` 直接抛 `ValueError`；取绝对值并清空 `left/right`；把 `x`、`xp` 折进 \([0, \text{period})\)；用 `np.argsort(xp)` 同步重排 `xp`、`fp`；最后用 `np.concatenate` 在两端拼接跨周期的镜像点（注意切片 `xp[-1:]` 取的是最后一个点本身，再减去 `period` 把它平移到左边）。

最后一步把所有情况统一交给 C 内核：

[文件路径:L1686-L1686](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1686) — 无论是否走周期路径，最终都执行 `return interp_func(x, xp, fp, left, right)`，由 C 代码完成二分搜索与线性插值。

#### 4.1.4 代码实践

**目标**：在非均匀节点上做插值，并观察 `left/right` 的兜底行为。

**操作步骤**：

```python
import numpy as np
xp = np.array([0.0, 1.0, 4.0])      # 节点不均匀：1→4 跨度大
fp = np.array([0.0, 10.0, 40.0])    # 一条近似 y=10x 的折线
x  = np.array([-1.0, 0.5, 2.0, 5.0])

print(np.interp(x, xp, fp))                    # 默认越界用端点值
print(np.interp(x, xp, fp, left=-99, right=99))# 自定义越界值
```

**需要观察的现象**：

- `x=2.0` 落在节点 1 与 4 之间，按公式应为 \(10 + \frac{40-10}{4-1}(2-1) = 20\)。
- `x=-1.0` 在最左节点左侧，默认返回 `fp[0]=0`，第二种调用返回 `-99`。
- `x=5.0` 在最右节点右侧，默认返回 `fp[-1]=40`，第二种调用返回 `99`。

**预期结果**：

```
[ 0.  5. 20. 40.]
[-99.   5.  20.  99.]
```

（结果已根据插值公式推得，可在本地直接运行验证。）

#### 4.1.5 小练习与答案

**练习 1**：节点 `xp=[1,2,3]`、`fp=[3,2,0]`，求 `np.interp(2.5, xp, fp)`。

**参考答案**：在节点 (2,2) 与 (3,0) 之间插值，\(2 + \frac{0-2}{3-2}(2.5-2) = 2 - 1 = 1.0\)。

**练习 2**：为什么 `xp` 里不能包含 `NaN`？

**参考答案**：源码注释明确写道「NaN is unsortable, `xp` also cannot contain NaNs」。周期路径会用 `argsort(xp)` 排序，非周期路径由 C 内核二分搜索，二者都依赖 `xp` 的可比较性；`NaN` 与任何值比较结果都不定，会让定位失效，插值结果无意义。

**练习 3**：`np.interp` 支持复数 `fp`，但它支持的「复数」具体指什么？

**参考答案**：指 `np.iscomplexobj(fp)` 为真的数组。源码据此切换到 `compiled_interp_complex`（输入提升为 `complex128`），实部和虚部各自独立做线性插值。

### 4.2 unwrap：相位解卷绕

#### 4.2.1 概念说明

很多信号（如雷达相位、声学相位、角度编码器读数）只能落在 \((-\pi, \pi]\) 或 \([0, 2\pi)\) 这样的一个周期区间里。当真实相位连续增长、跨过周期边界时，记录值会突然「跳」一个周期，出现 \(-\pi\to\pi\) 这种几乎 \(2\pi\) 的跳变。`unwrap` 的任务就是把这些跳变「补回去」，还原出一条连续曲线。

它的判定准则很直观：**如果相邻两个值的差大于半个周期，就认为这里发生了一次卷绕，给它加上或减去若干个周期，把差值压回半个周期以内。**

#### 4.2.2 核心流程

设输入序列为 \(p\)，周期为 \(P\)（默认 \(2\pi\)），`discont` 为判定阈值（默认 \(P/2\)）。流程如下：

1. 计算相邻差分 \(dd_i = p_i - p_{i-1}\)（复用 `diff`）。
2. 把每个差分「折」进半个周期的对称区间 \([-P/2, P/2)\)：
   \[ \text{ddmod}_i = \big((dd_i + P/2) \bmod P\big) - P/2 \]
3. 处理边界歧义：当差分恰好等于半个周期的整数倍时，按原始差分的符号把 `ddmod` 校正到 \(\pm P/2\)。
4. 相位修正量 \( \text{ph\_correct}_i = \text{ddmod}_i - dd_i \)。
5. **关键**：只保留「足够大」的跳变对应的修正；对 \(|dd_i| < \text{discont}\) 的小跳变，把修正清零（不改动）。
6. 把修正量做**累加和**，加回原序列从第二个元素开始的部分：
   \[ \text{up}_i = p_i + \sum_{j \le i} \text{ph\_correct}_j \]

之所以要「累加」，是因为一次卷绕之后，后面所有点都要跟着平移同样的周期数；而清零小跳变，则是为了避免把真正的、小幅度的相位变化也误判成卷绕。

#### 4.2.3 源码精读

dispatcher 与签名（注意 `period` 是仅关键字参数）：

[文件路径:L1747-L1752](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1747-L1752) — `_unwrap_dispatcher` 只返回 `(p,)`；`unwrap(p, discont=None, axis=-1, *, period=2 * pi)`，默认周期为 \(2\pi\)，默认沿最后一轴操作。

差分与半周期区间的计算：

[文件路径:L1836-L1852](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1836-L1852) — `dd = diff(p, axis=axis)` 取相邻差分；`discont` 缺省时取 `period/2`。接着用 `np.result_type(dd, period)` 推一个能同时容纳差分与周期的 dtype。这里对**整数 dtype** 做了特判：用 `divmod(period, 2)` 算 `interval_high`，并用 `rem == 0` 标记「半周期正好落在整数上」的歧义情形；浮点则 `interval_high = period/2` 且恒为歧义。最后 `ddmod = mod(dd - interval_low, period) + interval_low` 把差分折进 \([-\text{period}/2, \text{period}/2)\)。

歧义校正与修正量清零：

[文件路径:L1853-L1860](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1853-L1860) — 当 `boundary_ambiguous` 时，对于原本差分为正、却被折成 \(-\text{period}/2\) 的点，用 `copyto` 把 `ddmod` 改写成 \(+\text{period}/2\)，使其与跳变方向一致。随后 `ph_correct = ddmod - dd` 算出每个点需要补的周期数，再用 `copyto(..., where=abs(dd) < discont)` 把小跳变的修正清零。

累加修正并写回：

[文件路径:L1861-L1863](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1861-L1863) — `up = array(p, copy=True, dtype=dtype)` 复制一份输入；构造 `slice1` 选中沿 `axis`「从第二个元素起」的所有位置；`up[slice1] = p[slice1] + ph_correct.cumsum(axis)` 把修正量的累加和加回去。注意第一个元素不参与修正，作为整条曲线的「相位基准」。

#### 4.2.4 代码实践

**目标**：构造一个发生 \(2\pi\) 跳变的相位序列，用 `unwrap` 还原。

**操作步骤**：

```python
import numpy as np
# 一条本该从 0 增长到 2π 的相位，但被 angle/取模卷绕回 (-π, π]
phase = np.array([0.0, np.pi/2, np.pi, -np.pi, -np.pi/2])
print("卷绕后:", phase)
print("解卷绕:", np.unwrap(phase))
```

**需要观察的现象**：第三个点 `π` 到第四个点 `-π` 出现了约 \(-2\pi\) 的跳变；`unwrap` 判定这是一次卷绕，把后面的点整体加上 \(2\pi\)，得到连续增长的结果。

**预期结果**：

```
卷绕后: [ 0.          1.57079633  3.14159265 -3.14159265 -1.57079633]
解卷绕: [ 0.          1.57079633  3.14159265  3.14159265  4.71238898]
```

（结果已据流程推得，可在本地运行验证。）

#### 4.2.5 小练习与答案

**练习 1**：`np.unwrap([0, 1, 2, -1, 0], period=4)` 的结果是什么？

**参考答案**：周期为 4，半周期为 2。差分为 `[1,1,-3,1]`，其中 \(-3\) 超过半周期，被折成 \(+1\)、修正量 \(4\)；其余差分很小、修正清零。累加后得到 `[0,1,2,3,4]`（与源码文档示例一致）。

**练习 2**：如果把 `discont` 设成一个比 `period/2` **更小**的值，会发生什么？

**参考答案**：会有更多原本「不算卷绕」的跳变被判定为需要修正。源码注释明确警告：`discont` 小于 `period/2` 时会被当作 `period/2` 处理（`Values below period/2 are treated as if they were period/2`），所以实际不会让判定更敏感——要让效果不同于默认，`discont` 必须大于 `period/2`。

**练习 3**：为什么修正量要做 `cumsum` 而不是直接加？

**参考答案**：因为一旦在某处补偿了 \(k\) 个周期，该处之后的所有点都偏移了同样的量。直接加 `ph_correct` 只会修正单点，曲线会再次出现跳变；累加和才能把这个「整体平移」传播到序列末尾。

### 4.3 angle：复数辐角

#### 4.3.1 概念说明

`np.angle` 返回复数在复平面上的辐角，即正实轴逆时针转到该向量所张的角，范围 \((-\pi, \pi]\)。它本质上就是 `arctan2(虚部, 实部)` 的一层薄封装，额外做了两件小事：

1. 对**非复数**输入（纯实数数组）也能工作——这时虚部视为 0。
2. 提供 `deg` 开关，把弧度换算成角度。

#### 4.3.2 核心流程

```
z = asanyarray(输入)
若 z 是复数 dtype：zimag = z.imag；zreal = z.real
否则            ：zimag = 0   ；zreal = z
a = arctan2(zimag, zreal)        # ∈ (-π, π]
若 deg：a *= 180 / π
返回 a
```

由于它直接调 `arctan2`，因此也继承了 `arctan2` 在零向量处的约定（实虚都为 0 时返回 0，符号受 ±0.0 影响）。

#### 4.3.3 源码精读

dispatcher 与签名：

[文件路径:L1689-L1694](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1689-L1694) — `_angle_dispatcher(z, deg=None)` 返回 `(z,)`；`angle(z, deg=False)` 默认返回弧度。

实现体：

[文件路径:L1733-L1744](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1733-L1744) — `z = asanyarray(z)`；用 `issubclass(z.dtype.type, _nx.complexfloating)` 判断是否复数类型，是则取 `.imag`/`.real`，否则虚部直接设为 0、实部用 `z` 本身。`a = arctan2(zimag, zreal)` 得到弧度；`deg` 为真时乘 `180/pi` 换算。

#### 4.3.4 代码实践

**目标**：对比 `deg=True/False`，并观察纯实数输入的行为。

**操作步骤**：

```python
import numpy as np
print(np.angle([1.0, 1.0j, 1+1j]))      # 弧度
print(np.angle(1+1j, deg=True))          # 角度
print(np.angle([1.0, -2.0, 0.0]))        # 纯实数输入：虚部为 0
```

**需要观察的现象**：

- `1.0j` 辐角为 \(\pi/2\)（90°）。
- `1+1j` 辐角为 \(\pi/4\)（45°）。
- 纯实数输入：正实数辐角为 0，负实数辐角为 \(\pi\)，零辐角为 0。

**预期结果**：

```
[0.         1.57079633 0.78539816]
45.0
[ 0.          3.14159265  0.        ]
```

（结果已据 `arctan2` 推得，可在本地运行验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `np.angle([0., -0., complex(0.,-0.), complex(-0.,-0.)])` 会得到带符号 0 和负 π 的结果？

**参考答案**：因为 `angle` 把实虚部交给 `arctan2`，而 `arctan2` 严格区分 `+0.0` 与 `-0.0` 的符号。文档示例给出的结果是 `[0, π, -0, -π]`，这正是 `arctan2` 在各象限符号约定下的产物。

**练习 2**：`np.angle(3)`（传入一个 Python 整数）能运行吗？

**参考答案**：能。`asanyarray(3)` 会把它变成 0 维实数数组，虚部视为 0、实部为 3，`arctan2(0, 3)` 返回 `0.0`。

**练习 3**：用 `angle` 和 `unwrap` 配合，如何从一段「复指数信号」还原出连续相位？

**参考答案**：先 `phase_wrapped = np.angle(signal)` 得到卷绕在 \((-\pi,\pi]\) 的相位，再 `phase = np.unwrap(phase_wrapped)` 把跳变补回，即可得到连续相位（这正是本讲综合实践的思路）。

### 4.4 sort_complex：复数排序

#### 4.4.1 概念说明

`np.sort_complex` 把一个数组排序后**强制以复数类型**返回。numpy 对复数的排序规则是「字典序」：**先比实部，实部相同再比虚部**。即使输入是纯实数，它也会提升为复数类型再返回，这是它与普通 `np.sort` 的主要区别。

#### 4.4.2 核心流程

```
b = array(a, copy=True)
b.sort()                              # numpy 内置排序：复数按 (实部, 虚部) 字典序
若 b 不是复数 dtype：
    按 b 的字符码提升：bhBH → 'F'(complex64)，'g' → 'G'(longcomplex)，其它 → 'D'(complex128)
否则：原样返回 b
```

之所以复数排序不需要专门实现，是因为 numpy 的排序算法（`ndarray.sort`）本身就把复数按 `(real, imag)` 的字典序比较——`sort_complex` 只是借用了这个既有行为，再保证返回类型一定是复数。

#### 4.4.3 源码精读

dispatcher 与实现：

[文件路径:L1866-L1905](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1866-L1905) — `_sort_complex(a)` 返回 `(a,)`；`sort_complex` 先 `b = array(a, copy=True)` 拷贝，再原地 `b.sort()`。随后判断：若 `b` 不是复数类型，就用一组 `dtype.char` 分支把整数/小整数/长双精度浮点等分别提升为对应的复数类型；若已经是复数则直接返回。

测试依据：

[文件路径:L4785-L4791](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_function_base.py#L4785-L4791) — 单元测试 `test_sort_complex` 用 `[2+3j, 1-2j, 1-3j, 2+1j]` 验证结果为 `[1-3j, 1-2j, 2+1j, 2+3j]`（实部升序，实部相同时虚部升序），并断言返回 dtype 为 `'D'`。

#### 4.4.4 代码实践

**目标**：观察「先实部、后虚部」的排序规则，以及实数输入被提升为复数。

**操作步骤**：

```python
import numpy as np
print(np.sort_complex([1+2j, 2-1j, 3-2j, 3-3j, 3+5j]))
r = np.sort_complex([5, 3, 6, 2, 1])
print(r, r.dtype)
```

**需要观察的现象**：

- 第一行：实部都不同的先按实部排；实部同为 3 的三个数 `3-3j, 3-2j, 3+5j` 再按虚部排。
- 第二行：实数输入 `[5,3,6,2,1]` 排序后得到 `[1,2,3,5,6]`，且 dtype 被提升为 `complex128`（显示为 `1.+0.j` 形式）。

**预期结果**：

```
[1.+2.j 2.-1.j 3.-3.j 3.-2.j 3.+5.j]
[1.+0.j 2.+0.j 3.+0.j 5.+0.j 6.+0.j] complex128
```

（结果与源码文档示例一致，可在本地运行验证。）

#### 4.4.5 小练习与答案

**练习 1**：`np.sort_complex([2+3j, 1-2j, 1-3j, 2+1j])` 的结果是什么？

**参考答案**：按 `(实部, 虚部)` 字典序排：`(1,-3), (1,-2), (2,1), (2,3)`，即 `[1-3j, 1-2j, 2+1j, 2+3j]`。这正是单元测试 `test_sort_complex` 的断言。

**练习 2**：为什么对 `dtype='bhBH'`（各类整数）的输入，提升目标是 `'F'`（complex64）而不是 `'D'`（complex128）？

**参考答案**：源码这样规定是为了按「等价精度」配对：整数位宽较小，映射到单精度复数 `complex128` 之外的 `complex64('F')`；而 `'g'`（long double）映射到 `'G'`（long complex），其余默认映射到 `'D'`（complex128）。这是一种固定的精度对应约定，不是动态推断。

**练习 3**：`np.sort_complex` 与 `np.sort` 对同一复数数组的排序结果数值上有差别吗？

**参考答案**：没有差别。两者底层都用同一套复数字典序比较；差别只在 `sort_complex` 保证返回复数 dtype（对实数输入会提升），而 `np.sort` 会保留输入的原 dtype。

## 5. 综合实践

本任务把 `interp` 与 `unwrap` 串起来，模拟「从不均匀采样的卷绕相位还原连续相位」这一常见信号处理场景。

**背景**：假设真实相位是一条随时间增长的连续曲线 \(\phi(t)=0.3t\)，但我们只能通过复指数 \(z(t)=\mathrm{e}^{\mathrm{i}\phi(t)}\) 观察它，且采样时刻 `t_obs` 不均匀。需要：

1. 用 `interp` 把不均匀采样的相位重采样到均匀网格；
2. 用 `unwrap` 把卷绕在 \((-\pi,\pi]\) 的相位解卷绕成连续曲线；
3. 用 `angle` 从复指数取回（卷绕的）相位。

**参考代码**：

```python
import numpy as np

# 1) 真实连续相位与不均匀采样时刻
t_obs  = np.array([0.0, 1.1, 2.0, 3.3, 4.0, 5.2, 6.0, 7.1, 8.0, 9.5, 10.0])
phi_true = 0.3 * t_obs                      # 真实相位（连续）
z = np.exp(1j * phi_true)                   # 只能观察到的复指数信号

# 2) 从复指数取回相位 —— 必然被卷绕到 (-π, π]
phase_wrapped = np.angle(z)
print("卷绕相位:", np.round(phase_wrapped, 3))

# 3) 解卷绕，还原连续相位
phase_unwrapped = np.unwrap(phase_wrapped)
print("解卷绕相位:", np.round(phase_unwrapped, 3))
print("真实相位  :", np.round(phi_true, 3))

# 4) 把不均匀采样重采样到均匀网格上
t_uniform = np.linspace(0, 10, 11)
phi_uniform = np.interp(t_uniform, t_obs, phase_unwrapped)
print("均匀网格插值:", np.round(phi_uniform, 3))
```

**需要观察的现象**：

- `phase_wrapped` 在真实相位跨过 \(\pi\) 时出现约 \(-2\pi\) 的跳变。
- `phase_unwrapped` 与 `phi_true` 几乎完全相等（误差仅来自浮点），说明 `unwrap` 正确还原。
- 最后一步 `interp` 在非均匀节点 `t_obs` 上插值，得到均匀网格的相位；由于原数据本身就近似在直线上，插值结果应与 \(0.3 \times t_{\text{uniform}}\) 非常接近。

**预期结果**：`phase_unwrapped` 与 `phi_true` 逐项近似相等；`phi_uniform` 与 `0.3 * t_uniform` 近似相等。（具体数值待本地运行确认，但「解卷绕≈真实」「插值≈直线」这两个定性结论是确定的。）

**延伸思考**：如果把第 3 步的 `unwrap` 换成对 `t` 排序前的乱序输入会发生什么？由于 `unwrap` 严格按 `axis` 上相邻元素做差分，乱序输入会让差分失去物理意义。这提示我们：`unwrap` 假定输入沿 `axis` 已经是「时间顺序」排列的。

## 6. 本讲小结

- `interp` 的 Python 层只做两件事——选实/复 C 内核、处理 `period` 周期归一化；真正的二分搜索与线性插值由 C 内核完成。周期路径靠 `x%period` 折叠并在两端拼接跨周期镜像点，把角度插值归约为普通插值。
- `unwrap` 用「差分取模折回半周期 → 算修正量 → 小跳变清零 → 修正量累加和加回」四步，把卷绕相位还原成连续曲线；`discont` 小于 `period/2` 时按 `period/2` 处理。
- `angle` 是 `arctan2(虚部, 实部)` 的薄封装，对纯实数输入把虚部视为 0，`deg` 控制弧度/角度，结果范围 \((-\pi,\pi]\)。
- `sort_complex` 借用 numpy 复数「先实部后虚部」的字典序排序，并保证返回值一定是复数 dtype（实数输入按精度规则提升）。
- 四个函数都是「dispatcher + impl」双函数写法，由顶层 `numpy/__init__.py` 直接挂到 `np.`，`unwrap` 内部还复用了上一讲的 `diff`。

## 7. 下一步学习建议

- 本讲的 `interp` 是**一维**线性插值；若需要多维、样条或更灵活的插值，应转向 `scipy.interpolate`，源码注释的 `See Also` 也指向了它。
- `unwrap` 与 `angle` 是相位/复信号处理的入口。若对「频率估计」「瞬时相位」感兴趣，可继续阅读 `np.fft`（本系列另有 fft 子包讲义）。
- 下一讲（u6-l3）将讲解同在 `_function_base_impl.py` 的 `select` / `piecewise` / `extract` / `place` 等条件赋值与数组编辑函数，与本讲的「数值函数」构成同一文件里的另一族工具。
- 想深入了解 `interp` 的 C 内核（二分搜索的具体实现、`left/right` 的优先级），可阅读 `numpy/_core/src/multiarray/compiled_base.c` 中的 `interp` 与 `interp_complex` 函数。
