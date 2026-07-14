# 数值微分与积分：diff/gradient/trapezoid

## 1. 本讲目标

学完本讲，读者应该能够：

- 读懂 `numpy.diff` 的实现：它如何用「错位切片相减」做一阶差分，如何递归实现 `n` 阶差分，以及 `prepend`/`append` 与布尔数组的特殊处理。
- 读懂 `numpy.gradient` 的实现：它如何在内点用二阶精度的中心差分、在边界用一阶或二阶精度的单边差分，并理解均匀间距与非均匀间距两套公式的区别。
- 读懂 `numpy.trapezoid` 的实现：它如何用「相邻点求和」实现复合梯形积分，并理解 `x`（坐标数组）与 `dx`（标量间距）两条路径的差异。
- 把这三个函数串起来理解一个完整的「数值微积分」链路：`trapezoid` 求积分、`diff`/`gradient` 求导数，三者底层都依赖同一个招数——**沿某轴做错位切片**。

## 2. 前置知识

本讲是单元 6 的第一讲，承接 u1-l2 建立的两个关键认知，本讲直接复用、不再展开：

- **dispatcher + impl 双函数写法**：每个公开函数（`diff`/`gradient`/`trapezoid`）都以 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只 yield 参与运算的数组参数，交给 NEP-18 的 `__array_function__` 协议派发；真正的逻辑在同名 impl 函数里。
- **薄再导出 vs 顶层直接暴露**：`diff`/`gradient`/`trapezoid` 的实现都藏在私有文件 `numpy/lib/_function_base_impl.py`，再由顶层 `numpy/__init__.py` 直接 `import` 挂到 `np.` 命名空间（不经薄模块）。

除此外，本讲只需要中学微积分常识：

- **导数**是函数的瞬时变化率，几何上是切线斜率。数值上无法取「瞬时」，只能在离散采样点上用「相邻点之差 ÷ 间距」近似。
- **定积分**是函数曲线下方的面积。梯形法把曲线下方切成一排梯形，把每个梯形面积加起来近似总面积。

三者共享的底层招数是**沿某轴做错位切片**。以一维数组 `a = [a0, a1, a2, a3]` 为例：

```text
切片 a[1:]  = [a1, a2, a3]      # 去掉第一个
切片 a[:-1] = [a0, a1, a2]      # 去掉最后一个
两者逐元素相减 a[1:] - a[:-1] = [a1-a0, a2-a1, a3-a2]
```

这一招 numpy 里写起来就是「构造 `slice1`/`slice2` 两个切片对象、沿 `axis` 错开一位、再做逐元素运算」。`diff` 相减、`gradient` 中心差分、`trapezoid` 求和，全都建立在这套切片之上。记住这个招数，三个函数的源码就懂了一大半。

> 术语提示：`slice(None)` 就是 Python 里的 `:`，`slice(1, None)` 就是 `1:`，`slice(None, -1)` 就是 `:-1`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_function_base_impl.py` | 三个函数的全部实现所在：`gradient`（L1013–L1406）、`diff`（L1413–L1542）、`trapezoid`（L4919–L5051）。本讲只读这一个文件。 |
| `numpy/__init__.py` | 顶层入口，把这三个函数从 `._function_base_impl` 直接挂到 `np.` 命名空间。 |
| `numpy/lib/tests/test_function_base.py` | 对应测试，含 `TestDiff`（L851）、`TestGradient`（L1114）、`TestTrapezoid`（L2381），是理解边界行为的最佳材料，本讲的实践与断言多取材于此。 |

一个统一观察：三个函数都遵循「先规整轴 → 构造 `slice1`/`slice2` 错位切片 → 逐元素运算 → 沿轴归约或回填」的骨架。下面逐个拆解。

## 4. 核心概念与源码讲解

### 4.1 diff：n 阶离散差分

#### 4.1.1 概念说明

`np.diff(a, n=1, axis=-1)` 计算数组沿指定轴的 **n 阶离散差分**。一阶差分就是相邻元素之差：

\[ \text{out}[i] = a[i+1] - a[i] \]

`n` 阶差分是「对一阶差分再取 `n-1` 次差分」，所以 `diff` 内部是递归的。它解决的问题是「我想知道一个序列的变化有多快」——一阶差分就是最朴素的离散导数。

两个容易踩坑的细节：

1. **布尔数组用「不等」而非「相减」**：对 `dtype=bool` 的数组，`True - True` 在整数意义下是 0，但 numpy 故意改用 `not_equal`，让「相邻元素不同」记为 `True`、相同记为 `False`。这与「差分」的直觉一致：有变化才为真。
2. **`prepend`/`append` 扩展边界**：有时你想让差分结果与原数组等长（比如 `unwrap` 里要算相邻相位差），就可以在差分前往序列前后各补一个值。标量会被广播成沿轴长度为 1 的数组。

输出形状沿 `axis` 比 `a` 缩短 `n`，因为每做一次差分会少一个元素。

#### 4.1.2 核心流程

```text
diff(a, n=1, axis=-1, prepend=?, append=?)
  │
  ├─ n == 0 ?  → 原样返回 a（连拷贝都不做，身份相等）
  ├─ n < 0  ?  → 抛 ValueError
  │
  ├─ a = asanyarray(a)；要求 a.ndim >= 1（标量报错）
  ├─ axis = normalize_axis_index(axis, nd)        # 把负轴、越界轴规整
  │
  ├─ 处理 prepend/append：
  │     标量 → broadcast_to 成「沿 axis 长度 1、其余维同 a」的数组
  │     combined = [prepend?, a, append?]
  │     若 combined 长度 > 1：a = concatenate(combined, axis)
  │
  ├─ 构造错位切片：
  │     slice1[axis] = 1:     （即 a[1:]）
  │     slice2[axis] = :-1    （即 a[:-1]）
  │
  ├─ 选算子：op = not_equal if a.dtype==bool else subtract
  │
  ├─ for _ in range(n):                        # 递归 n 次做 n 阶差分
  │     a = op(a[slice1], a[slice2])
  │
  └─ return a
```

数学上，n 阶差分等价于

\[ \Delta^n a[i] = \sum_{k=0}^{n} (-1)^{n-k} \binom{n}{k} a[i+k] \]

但 numpy 没有套这个闭式公式，而是老老实实递归相减 `n` 次——实现更简单、也复用了同一套切片逻辑。

#### 4.1.3 源码精读

**dispatcher 与函数签名**——[_diff_dispatcher 与 diff 定义：L1409-L1414](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1409-L1414)。dispatcher 把 `(a, prepend, append)` 三个数组交给 NEP-18 派发；注意 `prepend`/`append` 默认值是哨兵 `np._NoValue` 而非 `None`，这样才能区分「用户没传」和「用户显式传了 None」。

**边界与轴规整**——[n 的合法性检查与轴规整：L1497-L1507](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1497-L1507)。`n==0` 直接 `return a`（注意测试里用 `assert_(diff(x, n=0) is x)` 验证是身份相等、零拷贝）；`n<0` 报错；标量（`nd==0`）报错。

**prepend/append 合并**——[标量广播与 concatenate：L1509-L1529](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1509-L1529)。标量被广播成「沿 axis 长度为 1」的数组，再和 `a` 一起 `concatenate`。这一段是 `unwrap`（相位解卷绕）能正常工作的前提——`unwrap` 第一步就是 `diff(p, axis=axis)`。

**错位切片与算子选择**——[切片构造 + bool/非bool 算子：L1531-L1538](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1531-L1538)。关键一行是：

```python
op = not_equal if a.dtype == np.bool else subtract
```

这行是布尔数组差分语义的根源（见 4.1.1 的踩坑点 1）。

**递归差分**——[for 循环做 n 阶差分：L1539-L1542](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1539-L1542)。每次循环把 `a` 替换成「错位相减后的新数组」，循环 `n` 次即得 n 阶差分。每轮数组沿 `axis` 缩短 1，所以最终长度为 `len(a) - n`（不足则为空数组）。

#### 4.1.4 代码实践

**实践目标**：验证 `diff` 的「递归相减」语义、布尔数组的 `not_equal` 行为，以及 `prepend` 的边界扩展。

**操作步骤**（这是一段可运行的源码阅读型实践，取材于 `TestDiff.test_basic`/`test_n`）：

```python
# 示例代码
import numpy as np

# 1. 一阶与多阶差分
x = np.array([1, 4, 6, 7, 12])
print(np.diff(x))          # [3 2 1 5]   即 [4-1, 6-4, 7-6, 12-7]
print(np.diff(x, n=2))     # [-1 -1 4]  即对一阶差分再做一次

# 2. 手动复现递归过程
d1 = x[1:] - x[:-1]
d2 = d1[1:] - d1[:-1]
print(d2)                  # 应与 np.diff(x, n=2) 完全一致

# 3. 布尔数组：用 not_equal 而非 subtract
b = np.array([True, True, False, False])
print(np.diff(b))          # [False  True False]  ← 相邻不同才为 True
print(np.diff(b, n=2))     # [True True]

# 4. prepend：让差分结果与原数组等长
print(np.diff(x, prepend=0))   # 在前面补 0，结果长度变 5：[1 3 2 1 5]

# 5. n==0 原样返回（身份相等）
print(np.diff(x, n=0) is x)    # True
```

**需要观察的现象**：

- 步骤 2 里手写的 `d2` 与 `np.diff(x, n=2)` 完全相同，印证「递归相减」。
- 步骤 3 布尔差分结果是布尔型而非整型，且 `True` 出现在「相邻值翻转」处。
- 步骤 4 加了 `prepend=0` 后输出长度从 4 变成 5。

**预期结果**：以上注释里给出的数组。如果你得到 `[True True]` 之外的布尔结果，说明你用了普通整数减法而非 `not_equal`——这正是源码 L1538 那一行存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：对一个长度为 5 的数组 `x`，`np.diff(x, n=5)` 的结果形状是什么？为什么？

> **答案**：空数组，形状 `(0,)`。每做一次差分长度减 1，做 5 次后长度为 `5-5=0`。测试 `test_n` 正是用 `len(output_n) == max(0, len(x) - n)` 来断言这一点。

**练习 2**：`np.diff(np.array([1,0], dtype=np.uint8))` 为什么得到 `255` 而不是 `-1`？怎样得到 `-1`？

> **答案**：无符号整数 `uint8` 的减法在 `2^8=256` 处回绕，`0-1 = -1 ≡ 255 (mod 256)`，这是 `subtract` ufunc 的正常行为，与 `0-1` 直接运算一致。要得到 `-1`，应先把数组转成更大的有符号类型：`np.diff(x.astype(np.int16))` → `[-1]`。

**练习 3**：`np.diff` 对 `datetime64` 数组会返回什么类型？

> **答案**：返回 `timedelta64`。两个 `datetime64` 相减自然得到时间差，这正是 `diff` 沿用 `subtract` 的副作用，docstring 里也明确标注。

---

### 4.2 gradient：中心差分与边界处理

#### 4.2.1 概念说明

`np.gradient(f, *varargs, axis=None, edge_order=1)` 估计 N 维数组 `f` 在每个采样点上的**数值梯度**。与 `diff` 不同，`gradient` 的输出形状与输入**完全相同**——它要给每个点（包括边界）都算一个导数估计值。

它的核心设计是「**内点用二阶精度中心差分，边界用单边差分**」，从而在整个定义域上保持尽可能高的精度：

- **内点（中心差分）**：用点左右两个邻居。均匀间距 \(h\) 下：

\[ f'_i \approx \frac{f_{i+1} - f_{i-1}}{2h} + O(h^2) \]

  这是经典的二阶精度中心差分，误差是 \(O(h^2)\)。

- **边界（单边差分）**：边界点没有「一侧的邻居」，只能用同侧的两个点。`edge_order=1` 用一阶前向/后向差分（\(O(h)\) 精度），`edge_order=2` 用二阶单边差分（\(O(h^2)\) 精度，但要求该轴至少有 3 个点）。

对于**非均匀间距**，中心差分公式要重新推导（因为左右步长不等）。numpy 用的是基于泰勒展开的最小一致性误差公式：

\[ \hat f_i^{(1)} = \frac{h_s^2 f(x_i+h_d) + (h_d^2 - h_s^2) f(x_i) - h_d^2 f(x_i-h_s)}{h_s h_d (h_d + h_s)} \]

其中 \(h_s\) 是到左邻点的距离、\(h_d\) 是到右邻点的距离。当 \(h_s = h_d = h\) 时，上式退化为标准的 \((f_{i+1}-f_{i-1})/(2h)\)。

它解决的典型问题是「我有一组离散采样，想知道每个点处的斜率」，比如本讲综合实践里对 `sin` 采样求数值导数。

#### 4.2.2 核心流程

```text
gradient(f, *varargs, axis=None, edge_order=1)
  │
  ├─ f = asanyarray(f)；N = f.ndim
  ├─ axes = 规整 axis（None → 全部轴）
  │
  ├─ 解析间距 varargs（4 种合法形态）：
  │     0 个参数        → dx = [1.0] * len_axes          # 全轴单位间距
  │     1 个标量        → dx = [该标量] * len_axes         # 全轴同一间距
  │     恰好 len_axes 个 → 逐轴处理：
  │                          标量         → 保持标量
  │                          1D 坐标数组  → diff(坐标)，若等距则坍缩回标量（提速）
  │     其他            → TypeError("invalid number of arguments")
  │
  ├─ edge_order > 2 → ValueError
  │
  ├─ 推导输出 dtype（otype）：
  │     datetime64 → 对应 timedelta64，并把 f view 成 timedelta
  │     timedelta64/inexact → 保持
  │     integer   → f 转成 float64，otype = float64（避免模运算）
  │
  └─ for axis, ax_dx in zip(axes, dx):        # 逐轴求导
       ├─ 该轴长度 < edge_order+1 → ValueError（点太少）
       ├─ out = empty_like(f, dtype=otype)
       ├─ uniform_spacing = (ax_dx 是标量)
       │
       ├─【内点】slice1[axis]=1:-1（中心点）
       │     均匀： out[1:-1] = (f[2:] - f[:-2]) / (2*ax_dx)
       │     非均匀：out[1:-1] = a*f[:-2] + b*f[1:-1] + c*f[2:]
       │              （a,b,c 由 h_s=dx1, h_d=dx2 算出）
       │
       ├─【边界】edge_order==1：
       │     out[0]  = (f[1] - f[0])   / dx_0
       │     out[-1] = (f[-1] - f[-2]) / dx_n
       │
       ├─【边界】edge_order==2：
       │     out[0]  = a*f[0]  + b*f[1]  + c*f[2]    （前向三点）
       │     out[-1] = a*f[-3] + b*f[-2] + c*f[-1]   （后向三点）
       │
       └─ outvals.append(out)；复位该轴切片为 ":"

  len_axes==1 ? 返回单个数组 : 返回 tuple(各轴梯度)
```

非均匀间距内点公式的三个系数（对应源码里的 `a/b/c`，其中 `dx1 = h_s`、`dx2 = h_d`）：

\[ a = -\frac{h_d}{h_s(h_s+h_d)}, \quad b = \frac{h_d - h_s}{h_s h_d}, \quad c = \frac{h_s}{h_d(h_s+h_d)} \]

即 `out[i] = a*f[i-1] + b*f[i] + c*f[i+1]`。

#### 4.2.3 源码精读

**dispatcher 与签名**——[_gradient_dispatcher 与 gradient 定义：L1008-L1014](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1008-L1014)。dispatcher 用 `yield f; yield from varargs` 把函数本身和所有间距参数都交给派发协议。`edge_order` 默认 `1`。

**间距解析的四种形态**——[varargs 解析：L1242-L1272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1242-L1272)。这是理解「`gradient` 为何能同时接受标量、数组、混合」的关键。注意 [L1261-L1270](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1261-L1270) 两处巧思：

1. 整数坐标先 `astype(np.float64)`，避免 `np.diff` 在整数上做模运算。
2. `diff(坐标)` 若发现所有相邻间距都相等，就**坍缩回标量**（`diffx = diffx[0]`），后续走更快的「均匀」分支——注释说是「a consistent speedup」。

**dtype 推导**——[otype 推导与整数→float64：L1288-L1304](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1288-L1304)。datetime64 被改写成同名单位的 timedelta64 并 `view`；整数输入转 `float64`。这一段解释了为什么 `gradient(np.array([1,2,4]))` 返回浮点而非整数。

**内点中心差分**——[二阶精度内点：L1317-L1340](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1317-L1340)。均匀分支 [L1323-L1324](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1323-L1324) 一行 `(f[2:] - f[:-2]) / (2*ax_dx)` 就是教科书公式；非均匀分支 [L1326-L1340](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1326-L1340) 用上面给的 `a/b/c` 系数，并 `reshape` 成可广播形状（只在 `axis` 维有长度、其余维为 1）。

**边界：一阶单边差分**——[edge_order==1 的边界：L1342-L1356](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1342-L1356)。两端各用两个相邻点做前向/后向差分，注释里贴心地写了等价的 1D 表达式 `out[0] = (f[1]-f[0])/dx_0`。

**边界：二阶单边差分**——[edge_order==2 的边界：L1358-L1394](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1358-L1394)。两端各用三个点。均匀分支的系数 [L1365-L1367](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1365-L1367) 是 `-1.5/h, 2/h, -0.5/h`，对应标准二阶前向公式 `(-3f[0]+4f[1]-f[2])/(2h)`；后向端 [L1383-L1385](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1383-L1385) 是 `0.5/h, -2/h, 1.5/h`，对应 `(3f[-1]-4f[-2]+f[-3])/(2h)`。

**返回单值或元组**——[结尾返回：L1404-L1406](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1404-L1406)。单轴返回单个数组，多轴返回元组——这是 `gradient` 与 `diff`（永远返回单数组）的接口差异。

#### 4.2.4 代码实践

**实践目标**：对 `sin` 函数等距采样，用 `gradient` 求数值导数，并与解析导数 `cos` 比较，验证「内点二阶精度」。

**操作步骤**：

```python
# 示例代码
import numpy as np

# 1. 等距采样 sin(x)，x in [0, 2π]
N = 50
x = np.linspace(0, 2*np.pi, N)
h = x[1] - x[0]            # 等距间距
y = np.sin(x)

# 2. 用 gradient 求数值导数（默认 edge_order=1）
dy_num = np.gradient(y, h)

# 3. 解析导数 cos(x) 作为真值
dy_true = np.cos(x)

# 4. 误差分析
err = np.abs(dy_num - dy_true)
print("最大误差:", err.max())
print("内点最大误差:", err[1:-1].max())     # 内点应是 O(h^2)
print("边界误差:", err[0], err[-1])         # 边界是 O(h)，通常更大

# 5. 换 edge_order=2，看边界是否改善
dy_num2 = np.gradient(y, h, edge_order=2)
err2 = np.abs(dy_num2 - dy_true)
print("edge_order=2 边界误差:", err2[0], err2[-1])

# 6. 直接给坐标数组 x，让 gradient 自己算间距
print("与传标量一致:", np.allclose(np.gradient(y, x), dy_num))
```

**需要观察的现象**：

- 内点误差应远小于边界误差（约 \(h^2 \approx 0.016\) 量级 vs 边界 \(h \approx 0.13\) 量级）。
- `edge_order=2` 时边界误差显著下降，接近内点精度。
- 传坐标数组 `x` 与传标量 `h` 结果一致（因为 `x` 等距，源码 L1268 会把 `diff(x)` 坍缩回标量 `h`）。

**预期结果**：内点最大误差约在 `1e-2` 量级以下；`edge_order=2` 的边界误差比 `edge_order=1` 小约一个数量级。具体数值与 `N` 有关，**待本地验证**你机器上的精确数字，但相对大小关系应如上。

#### 4.2.5 小练习与答案

**练习 1**：`np.gradient(f, x)` 与 `np.gradient(f, x, axis=0)` 在 `f` 为一维时等价吗？在 `f` 为二维时呢？

> **答案**：一维时等价（只有一个轴）。二维时不等价：`np.gradient(f, x)` 要求 `x` 必须对应第一个（默认）轴；而 `np.gradient(f, x, axis=0)` 显式指定沿 0 轴。若想分别给两个轴间距，要传两个参数，如 `np.gradient(f, dx0, dx1)`，且数量必须等于被求导的轴数，否则触发 [L1271-L1272](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1271-L1272) 的 `TypeError("invalid number of arguments")`。

**练习 2**：对一个只有 2 个元素的数组，`edge_order=2` 会发生什么？

> **答案**：抛 `ValueError`。源码 [L1307-L1310](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1307-L1310) 检查 `f.shape[axis] < edge_order + 1`，二阶单边差分需要 3 个点，2 个元素不够。`edge_order=1` 只需 2 个点，可以用。

**练习 3**：为什么整数输入的 `gradient` 结果是 `float64`？

> **答案**：差分涉及除以 `2*ax_dx`，必须用浮点；更隐蔽的是整数减法在某些宽度下会模运算回绕。源码 [L1302-L1304](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L1302-L1304) 显式把整数 `f` 转 `float64`、并把 `otype` 设为 `float64`，正是为此。

---

### 4.3 trapezoid：复合梯形积分

#### 4.3.1 概念说明

`np.trapezoid(y, x=None, dx=1.0, axis=-1)` 用**复合梯形法则**沿指定轴求定积分的数值近似。

\[ \int y(x)\,dx \approx \sum_{i=0}^{n-2} \frac{y_i + y_{i+1}}{2}\,(x_{i+1} - x_i) \]

直观地说：把相邻两个采样点 `(x_i, y_i)` 与 `(x_{i+1}, y_{i+1})` 之间看成一个小梯形，梯形面积 = `(上底+下底)/2 × 高` = `(y_i + y_{i+1})/2 × (x_{i+1} - x_i)`，再把所有小梯形面积加起来。

它有两个等价的入口：

- **给定坐标数组 `x`**：间距就是 `diff(x)`，每段高不一样，能处理非均匀采样。`x` 是 1D 时按上述公式逐段算；`x` 与 `y` 同维时按指定轴算。
- **给定标量 `dx`**（`x=None`）：假设等距采样，所有段高都是 `dx`，公式里 `d` 直接取标量 `dx`。

注意 `trapezoid` 是 NumPy 2.0 引入的名字（`.. versionadded:: 2.0.0`），取代了旧名 `trapz`。它解决的问题是「我有一组离散采样，想估算曲线下方面积」，是 `gradient`（求导）的逆操作。

#### 4.3.2 核心流程

```text
trapezoid(y, x=None, dx=1.0, axis=-1)
  │
  ├─ y = asanyarray(y)
  ├─ 计算每段宽度 d：
  │     x is None  → d = dx（标量）
  │     x.ndim==1  → d = diff(x)；再 reshape 成「axis 维=len(d)，其余维=1」可广播
  │     x.ndim>1   → d = diff(x, axis=axis)             # 与 y 同维
  │
  ├─ 构造错位切片：slice1[axis]=1:，slice2[axis]=:-1
  │
  ├─ ret = (d * (y[1:] + y[:-1]) / 2.0).sum(axis)
  │     ↑ 这一行同时完成「求平均高度 × 宽度」和「沿轴求和」
  │
  ├─ 若上式抛 ValueError（通常是子类如 matrix 不兼容）：
  │     回退到 add.reduce(...)，并先把 d、y 强转成普通 ndarray
  │
  └─ return ret
```

一句话总结：**`trapezoid` = `diff`（算宽度）+ 错位求平均 + `sum`**。它内部直接调用了 4.1 的 `diff` 来算 `diff(x)`。

当 `x` 等距且间距为 `h` 时，公式退化为：

\[ \int y\,dx \approx h\left( \frac{y_0 + y_{n-1}}{2} + \sum_{i=1}^{n-2} y_i \right) \]

即「首尾各算一半，中间全加」——这就是经典的梯形法。

#### 4.3.3 源码精读

**dispatcher 与签名**——[_trapezoid_dispatcher 与 trapezoid 定义：L4915-L4920](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L4915-L4920)。dispatcher 返回 `(y, x)`——注意只把 `y` 和 `x`（可能为 `None`）交给派发，`dx` 是标量不参与。

**宽度 `d` 的三种来源**——[x 为 None / x 为 1D / x 为 N-D：L5026-L5038](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5026-L5038)。最值得注意的巧思在 [L5033-L5036](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5033-L5036)：当 `x` 是 1D 但 `y` 是多维时，把 `diff(x)` reshape 成「只有 axis 维有真实长度、其余维都是 1」的形状，这样它就能和 `y[1:]+y[:-1]` 正确广播——这正是 4.1 错位切片招数的广播化扩展。

**核心一行 + 子类回退**——[ret 计算与 ValueError 回退：L5039-L5050](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5039-L5050)。核心是：

```python
ret = (d * (y[slice1] + y[slice2]) / 2.0).sum(axis)
```

其中 `y[slice1]=y[1:]`、`y[slice2]=y[:-1]`。`try/except ValueError` 是为兼容 `matrix` 等子类——这类对象的 `*` 是矩阵乘法而非逐元素乘，会抛 `ValueError`，于是回退到 `add.reduce` 并先把 `d`、`y` 强转成普通 `ndarray` 再算。

**返回**——[return ret：L5051](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5051)。1D 输入返回标量，多维输入沿 `axis` 降一维。

#### 4.3.4 代码实践

**实践目标**：用 `trapezoid` 估算 \(\int_0^1 x^2\,dx = 1/3\)，并比较「给坐标 `x`」与「给标量 `dx`」两条路径。

**操作步骤**：

```python
# 示例代码
import numpy as np

# 1. 估算 ∫_0^1 x^2 dx = 1/3
x = np.linspace(0, 1, num=50)
y = x**2
print("trapezoid(y, x) =", np.trapezoid(y, x))   # ≈ 0.33340...
print("精确值 1/3      =", 1/3)

# 2. 等距用标量 dx
h = x[1] - x[0]
print("trapezoid(y, dx=h) =", np.trapezoid(y, dx=h))   # 应与上面几乎相等

# 3. 手动复现公式
manual = np.sum((y[1:] + y[:-1]) / 2.0 * np.diff(x))
print("手动复现          =", manual)

# 4. 沿指定轴积分 2D 数组
a = np.arange(6).reshape(2, 3)
print("沿 axis=0:", np.trapezoid(a, axis=0))   # 形状 (3,)
print("沿 axis=1:", np.trapezoid(a, axis=1))   # 形状 (2,)

# 5. 验证「首尾各半、中间全加」的等距简化公式
n = len(y)
simplified = h * (0.5*y[0] + y[1:n-1].sum() + 0.5*y[-1])
print("等距简化公式      =", simplified)
```

**需要观察的现象**：

- 步骤 1、2、3、5 给出几乎相同的数值，且都接近 `1/3`（有 `O(h^2)` 的离散误差）。
- 步骤 4 中 `axis=0` 结果形状 `(3,)`、`axis=1` 结果形状 `(2,)`，印证「沿指定轴降一维」。

**预期结果**：`np.trapezoid(y, x)` ≈ `0.3334`，比真值 `0.3333...` 略大（梯形法对下凸函数高估）。`trapezoid(y, x)` 与 `trapezoid(y, dx=h)` 在等距时应完全相等。

#### 4.3.5 小练习与答案

**练习 1**：`np.trapezoid([1, 2, 3])` 为什么等于 `4.0`？

> **答案**：默认 `x=None, dx=1.0`，即等距间距 1。三个点形成两个梯形：`(1+2)/2 × 1 + (2+3)/2 × 1 = 1.5 + 2.5 = 4.0`。也可用等距简化式：`1×(0.5×1 + 2 + 0.5×3) = 0.5+2+1.5 = 4.0`。

**练习 2**：`np.trapezoid([1, 2, 3], x=[8, 6, 4])` 为什么是负数 `-8.0`？

> **答案**：`diff([8,6,4]) = [-2, -2]`，宽度为负意味着沿 `x` 减小方向积分。结果 `(-2)×(1.5+2.5) = -8.0`。这对应 docstring 里「decreasing x corresponds to integrating in reverse」。

**练习 3**：`trapezoid` 和 `gradient` 在数学上是什么关系？

> **答案**：互为（离散意义下的）逆操作。`gradient` 是离散求导（微分），`trapezoid` 是离散求积（积分）。在源码层面两者还直接联动：`trapezoid` 内部 [L5032](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_function_base_impl.py#L5032) 调用 `diff(x)` 来算积分用的宽度。

---

## 5. 综合实践

把三个函数串成一条完整的「数值微积分」链路，验证微积分基本定理的离散版本：**先微分再积分，应还原出原函数（差一个常数）**。

**任务**：取 \(f(x) = \sin(x)\) 在 \([0, 2\pi]\) 上等距采样。

1. 用 `gradient` 求离散导数 \(f'\)（数值上应逼近 \(\cos(x)\)）。
2. 用 `trapezoid` 对数值导数 \(f'\) 做积分，重建原函数 \(\hat f(x) = \int_0^x f'(t)\,dt\)。
3. 比较 \(\hat f\) 与原始 \(\sin(x)\)，它们应当只差一个常数（即 \(\sin(0)=0\)）。
4. 用 `diff` 验证 `trapezoid` 的内部宽度计算：确认 `np.diff(x)` 与你手动给的间距一致。

**参考实现**（示例代码）：

```python
import numpy as np

N = 200
x = np.linspace(0, 2*np.pi, N)
h = x[1] - x[0]
f = np.sin(x)

# 1. 数值导数
df = np.gradient(f, h)

# 2. 对导数积分，重建原函数（逐点累积积分）
#    trapezoid 默认返回标量，这里用累加方式逐点重建
f_reconstructed = np.zeros_like(f)
for i in range(1, N):
    f_reconstructed[i] = np.trapezoid(df[:i+1], dx=h)

# 3. 与原函数比较（应差一个常数，这里常数≈0）
err = np.abs(f_reconstructed - f)
print("重建最大误差:", err.max())

# 4. 用 diff 验证间距
print("间距均匀:", np.allclose(np.diff(x), h))
print("gradient 输出与输入同形:", df.shape == f.shape)
print("diff 输出比输入短 1:", np.diff(f).shape[0] == N - 1)
```

**预期结果**：重建最大误差应在 `1e-2` 量级以内（受 `gradient` 的边界一阶精度影响，端点误差会偏大，可用 `edge_order=2` 改善）。这个实践同时用到三个函数，并直观展示：

- `gradient`：输出与输入**同形**（每点一个导数）。
- `diff`：输出比输入**短**（相邻差，少一个）。
- `trapezoid`：输出**降一维**（积分掉一个轴）。

三者的形状变化规则，正是它们角色不同的最直观体现。

## 6. 本讲小结

- **`diff`、`gradient`、`trapezoid` 共享同一招**：沿某轴构造 `slice1=1:` 与 `slice2=:-1` 两个错位切片，再做逐元素运算——相减得差分、相加得梯形、相减除以间距得导数。
- **`diff` 是递归相减**：一阶差分 `a[1:]-a[:-1]`，n 阶差分循环 n 次；布尔数组改用 `not_equal`；`prepend`/`append` 通过 `concatenate` 扩展边界；输出沿轴缩短 n。
- **`gradient` 是「内点中心差分 + 边界单边差分」**：内点二阶精度 `(f[2:]-f[:-2])/(2h)`，边界由 `edge_order` 选一阶或二阶单边公式；均匀与非均匀间距走两套系数；输出与输入**同形**，单轴返回数组、多轴返回元组。
- **`trapezoid` 是「`diff(x)` 算宽度 + 错位求平均 + `sum`」**：核心一行 `(d*(y[1:]+y[:-1])/2).sum(axis)`；`x=None` 走标量 `dx`、`x` 为 1D 时 reshape 成可广播形状；对 `matrix` 等子类有 `add.reduce` 回退。
- **三者形成离散微积分对偶**：`gradient`/`diff` 是微分（`trapezoid` 内部还调用 `diff` 算宽度），`trapezoid` 是积分；形状变化分别是「同形 / 缩短 / 降维」。
- **dtype 处理体现健壮性**：`diff` 让 `datetime64` 自然产生 `timedelta64`；`gradient` 把整数转 `float64` 防止模运算、把 `datetime64` view 成 `timedelta64`；这些都写在源码而非依赖用户注意。

## 7. 下一步学习建议

- **继续本单元（单元 6）**：下一讲 **u6-l2（interp/unwrap/angle）** 会深入同文件的 `interp`（一维线性插值）、`unwrap`（相位解卷绕）。`unwrap` 的第一步就是 `diff(p, axis=axis)`——本讲的 `diff` 是它的直接前置。读完 u6-l2 你会发现 `diff` 在信号处理里无处不在。
- **追统计与归约**：单元 7 的 **u7-l1（_ureduce/median/cov）** 讲解 `_ureduce` 这个通用归约框架。`trapezoid` 末尾的 `.sum(axis)` 与 `add.reduce` 就是归约操作，理解 `_ureduce` 能帮你看清 numpy 所有「沿轴归约」函数的统一骨架。
- **延伸阅读**：本讲的 `trapezoid` 是「一次性积分」——沿轴 `sum` 后降一维。若你想要「累积积分」（不降维、返回每个点处的累积面积），只需把核心一行里的 `.sum(axis)` 换成 `np.cumsum(..., axis)`，这正是综合实践里逐点重建原函数用到的思想。NumPy 本身不提供 `cumulative_trapezoid`，等价的累积版本在 SciPy（`scipy.integrate.cumulative_trapezoid`）中，可作为对照阅读。
