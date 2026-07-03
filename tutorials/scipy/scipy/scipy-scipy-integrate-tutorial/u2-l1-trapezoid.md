# 梯形法 trapezoid 与 cumulative_trapezoid

## 1. 本讲目标

本讲聚焦 `scipy.integrate` 中最基础的「固定样本积分」工具。读完本讲你应当能够：

- 说清楚**复合梯形公式**的数学含义，以及它在等距样本、非等距样本、多维数组上分别如何执行。
- 区分 `trapezoid`（一次算出整段定积分）和 `cumulative_trapezoid`（逐点累加得到「积分曲线」）在用途与实现上的差别。
- 看懂 `_quadrature.py` 里两个函数的核心实现，特别是 `slice1`/`slice2` 切片技巧和「`sum` vs `cumulative_sum`」这一对照。
- 理解函数签名上方的 `@xp_capabilities()` 装饰器为什么存在，以及它和函数体内 `xp = array_namespace(y)` 的协作关系。

本讲是第 2 单元（固定样本数值积分）的第一篇。在 [u1-l3](u1-l3-getting-started.md) 中你已经跑通过 `trapezoid` 的最简单调用；本讲我们真正打开它的源码看里面发生了什么。

## 2. 前置知识

### 2.1 什么是「固定样本积分」

在 [u1-l3](u1-l3-getting-started.md) 里我们区分了两类积分：

- **函数积分**（如 `quad`）：你手里有一个可计算的函数 \(f(x)\)，算法自己决定在哪里取点、取多少点，还能给出误差估计。
- **固定样本积分**（本讲主题）：你手里只有**已经采好样**的一组点 \((x_i, y_i)\)，比如实验测得的离散数据，你不知道产生这些数据的函数表达式，也**无法再多采点**。这时只能用几何公式（梯形、辛普森……）在现有样本上估算面积，而且**一般拿不到误差估计**。

本讲的 `trapezoid` 与 `cumulative_trapezoid` 都属于第二类。

### 2.2 定积分的几何直觉

定积分 \(\int_a^b f(x)\,dx\) 就是函数曲线下方（带正负号）的面积。当只有离散样本时，最朴素的近似是：把相邻两点用直线连起来，把这些小梯形的面积加起来——这就是「梯形法」。

### 2.3 NumPy 切片与广播

源码大量使用 `slice(1, None)`（从第 1 个元素到最后）、`slice(None, -1)`（从开头到倒数第二个）这样的切片，以及沿某个 `axis` 的广播。如果你对这些还不熟，建议先复习 NumPy 的 indexing 与 broadcasting 基础。

## 3. 本讲源码地图

本讲只涉及一个源文件，但会反复在不同行段之间切换：

| 文件 | 关键内容 | 本讲角色 |
|------|----------|----------|
| `scipy/integrate/_quadrature.py` | 所有「固定样本」积分函数的家 | 唯一精读对象 |
| 同上，`trapezoid`（约 L22–L163） | 复合梯形法，返回定积分 | 最小模块 1 |
| 同上，`cumulative_trapezoid`（约 L259–L353） | 复合梯形法，返回累计积分 | 最小模块 2 |
| 同上，`tupleset`（L253–L256） | 辅助：替换元组里的一个元素 | 实现 `cumulative_trapezoid` 时构造切片的小工具 |
| `scipy/_lib/_array_api.py`，`xp_capabilities`（约 L839–L880） | 数组 API 能力声明装饰器 | 解释最小模块 3 |

> 提示：`trapezoid`、`cumulative_trapezoid` 这些公开名字能通过 `from scipy import integrate` 直接用，是因为 `__init__.py` 把 `_quadrature.py` 里的名字搬到了顶层命名空间（见 [u1-l1](u1-l1-project-overview.md)）。

## 4. 核心概念与源码讲解

### 4.1 复合梯形公式与 `trapezoid`

#### 4.1.1 概念说明

假设我们在区间 \([a, b]\) 上有 \(n+1\) 个样本点 \((x_0, y_0), (x_1, y_1), \dots, (x_n, y_n)\)，其中 \(y_i = f(x_i)\)（但函数 \(f\) 本身对算法不可见）。把相邻两点之间的曲线近似成一条直线，就得到一个小梯形。整个区间上的定积分近似为所有小梯形面积之和：

\[
\int_a^b f(x)\,dx \;\approx\; \sum_{i=0}^{n-1} \frac{y_i + y_{i+1}}{2}\,(x_{i+1} - x_i)
\]

这就是**复合梯形公式（composite trapezoidal rule）**。

当样本等距、间距为 \(h\) 时，上式化简为常见形式：

\[
\int_a^b f(x)\,dx \;\approx\; \frac{h}{2}\Big(y_0 + 2y_1 + 2y_2 + \cdots + 2y_{n-1} + y_n\Big)
\]

`trapezoid` 函数就是把上面这个求和用**向量化、支持多维、支持任意数组后端**的方式实现出来。它的关键设计点有三个：

1. **间距 `d` 的三种来源**：当 `x=None` 时用标量 `dx`；当 `x` 是一维时用 `x[1:] - x[:-1]`（每段宽度可能不同）；当 `x` 与 `y` 同维时按对应位置算宽度。
2. **`y_i + y_{i+1}` 配对的向量化**：用两个错位切片一次取出所有「右端点」和「左端点」。
3. **沿任意 `axis` 求和**：`y` 可以是多维数组，积分只发生在指定轴上，其它轴保持不变（结果是「降一维」的数组）。

#### 4.1.2 核心流程

`trapezoid(y, x=None, dx=1.0, axis=-1)` 的执行可以用下面这段伪代码概括：

```text
1. xp = 输入数组所属的命名空间（numpy / cupy / torch ...）
2. 把 y 转成 xp 数组，并确定一个「强制浮点」的结果类型
3. 构造两个错位切片：
     slice1[axis] = [1:]      # 右端点：y1, y2, ..., yn
     slice2[axis] = [:-1]     # 左端点：y0, y1, ..., y_{n-1}
4. 计算每段宽度 d：
     若 x 为 None   -> d = dx（标量）
     若 x 是一维    -> d = x[1:] - x[:-1]，并沿 axis 广播
     若 x 与 y 同维 -> d = x[slice1] - x[slice2]
5. ret = sum( d * (y[slice1] + y[slice2]) / 2.0, 沿 axis )   # ← 核心
6. 返回 ret
```

第 5 步是整个函数的灵魂：`y[slice1] + y[slice2]` 一次性配出所有相邻点之和，乘以 `d/2` 得到每个梯形面积，再用 `sum` 沿 `axis` 求和——一行代码完成全部复合梯形公式的计算。

#### 4.1.3 源码精读

先看签名与装饰器。注意函数上方的 `@xp_capabilities()`——它的意义我们在 4.3 节专门讲，这里先知道它「声明本函数支持多种数组后端」即可：

[scipy/integrate/_quadrature.py:22-23](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L22-L23) —— 装饰器声明数组后端能力，函数签名定义了 `y / x / dx / axis` 四个参数。

接着是函数体的开场。第一行 `xp = array_namespace(y)` 是「数组 API」可移植性的关键：它根据输入 `y` 是什么类型的数组（NumPy / CuPy / PyTorch / JAX / Dask）返回对应的命名空间，后续所有运算都写成 `xp.xxx`，而不是写死 `np.xxx`。`force_floating=True` 保证即使 `y` 是整数数组，累加结果也会用浮点类型，避免整数求和溢出或被截断：

[scipy/integrate/_quadrature.py:125-131](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L125-L131) —— 取命名空间、转数组、确定浮点结果类型、记录维度。

接下来构造那对关键的错位切片。`nd` 是 `y` 的总维数；`slice1` 和 `slice2` 初始都是「全选」，然后把积分轴 `axis` 上的切片分别替换成 `[1:]` 和 `[:-1]`：

[scipy/integrate/_quadrature.py:132-135](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L132-L135) —— 构造错位切片，使 `y[slice1]` 与 `y[slice2]` 沿 `axis` 错开一位。

然后是「算宽度 `d`」的三分支逻辑。注意一维 `x` 分支里，算出每段宽度后还要插入一个 `None` 维（`slice3`）把它广播到与 `y` 对齐的形状——否则一个长度为 `n` 的一维 `d` 没法直接和 `y` 相乘：

[scipy/integrate/_quadrature.py:136-149](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L136-L149) —— 三种方式计算每段宽度 `d`：标量 `dx`、一维 `x` 的差分（带广播）、多维 `x` 的逐位置差分。

最后是核心计算。`d * (y[slice1] + y[slice2]) / 2.0` 就是「每个梯形的面积」，`xp.sum(..., axis=axis)` 把它们沿积分轴加起来。外层 `try/except ValueError` 是一个兜底：某些后端在 `subok=True` 数组上做运算可能失败，此时退回普通 `asarray` 再算一次：

[scipy/integrate/_quadrature.py:150-163](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L150-L163) —— 复合梯形公式的全部计算：配对、求面积、沿 `axis` 求和，带一个后端兼容兜底。

> 小结：`trapezoid` 的实现极为紧凑——主体就是一个错位切片加一个 `sum`。理解了这一点，下面 `cumulative_trapezoid` 几乎就是「把 `sum` 换成 `cumulative_sum`」。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `trapezoid` 在等距样本与非等距样本上的行为，体会 `x` 参数的作用。

**操作步骤**（把下面的脚本存为 `trap_demo.py` 并运行）：

```python
# 示例代码：演示 trapezoid 的三种间距来源
import numpy as np
from scipy import integrate

# 被采样的“真函数”是 sin(x)，但 trapezoid 并不知道这一点
# 解析积分：∫ sin(x) dx = -cos(x) + C，所以 ∫_0^π sin(x) dx = 2

# (1) 等距样本，不给 x：默认 dx=1，所以这里结果没有几何意义，仅看机制
y_eq = np.sin(np.linspace(0, np.pi, 5))      # 5 个点，dx 默认为 1
print("不给 x（dx=1）：", integrate.trapezoid(y_eq))

# (2) 等距样本，给 x
x_eq = np.linspace(0, np.pi, 5)
print("等距 x      ：", integrate.trapezoid(y_eq, x=x_eq), "（解析值 2）")

# (3) 非等距样本：在中间加密采样
x_non = np.array([0, 0.5, 1.0, 1.5, np.pi])  # 间距不等
y_non = np.sin(x_non)
print("非等距 x    ：", integrate.trapezoid(y_non, x=x_non), "（解析值 2）")

# (4) 反向 x：积分方向反过来，结果变号
x_rev = np.array([np.pi, 1.5, 1.0, 0.5, 0])
print("反向 x      ：", integrate.trapezoid(np.sin(x_rev), x=x_rev), "（应为 -2）")
```

**需要观察的现象**：
- 第 (2)、(3) 行都应接近 2（因为只有 5 个点，会有梯形法误差，不会精确等于 2）。
- 第 (1) 行是 `dx=1` 默认间距下把样本当成「整数索引」算的，结果与 2 无关——这提醒你：**几何上有意义时一定要传 `x` 或正确的 `dx`**。
- 第 (4) 行应为负值，因为 `d = x[1:] - x[:-1]` 在递减 `x` 下取负号。

**预期结果**：等距与非等距样本都给出接近 2 的近似值；反向 `x` 给出接近 -2 的值。具体数值因点数有限而与解析值略有偏差（梯形法误差量级为 \(O(h^2)\)）。

> 注：上述脚本未在本讲义环境中实际运行，若数值与描述有出入，请以本地运行结果为准（「待本地验证」细节）。

#### 4.1.5 小练习与答案

**练习 1**：对同一个函数 \(f(x)=\sin(x)\) 在 \([0,\pi]\) 上，分别用 5、21、101 个等距点调用 `trapezoid(y, x)`，观察结果如何逼近 2。误差大致按什么规律缩小？

**参考答案**：点数越多，误差越小；梯形法误差量级为 \(O(h^2)\)，其中 \(h\) 是步长。点数翻倍（步长减半）时，误差大约缩小到原来的 \(1/4\)。

**练习 2**：把 `axis` 设为 0，对一个形状为 `(3, 4)` 的二维数组调用 `trapezoid`，返回结果的形状是什么？为什么？

**参考答案**：返回形状为 `(4,)`。因为沿 `axis=0`（长度 3）积分后，该轴被「积掉」，结果比输入少一维，剩余轴长度仍为 4。

---

### 4.2 `cumulative_trapezoid` 累计积分

#### 4.2.1 概念说明

`trapezoid` 返回的是一个**数字**（整段的定积分）。但很多场景下我们想要的是「积分随自变量变化的过程」——比如已知速度 \(v(t)\) 的离散样本，想求每个时刻的位置 \(s(t)=\int_0^t v(\tau)\,d\tau\)，得到一条随时间增长的位置曲线。这正是 `cumulative_trapezoid`（累计梯形积分）的用途。

它的数学定义是：对每个上界 \(x_m\)，输出之前所有小梯形面积的累加：

\[
C_m \;=\; \sum_{i=0}^{m-1} \frac{y_i + y_{i+1}}{2}\,(x_{i+1} - x_i), \qquad m = 1, 2, \dots, n
\]

对比 `trapezoid` 只取最后的总和 \(C_n\)，`cumulative_trapezoid` 保留全部中间值 \(C_1, C_2, \dots, C_n\)。从实现上看，这等价于「把每个梯形面积算出来后做一次前缀和（prefix sum）」，而不是只做一次总求和。

`cumulative_trapezoid` 还多了一个 `initial` 参数：设为 `0` 时会在最前面补一个 0，使输出长度与 `y` 相同（方便与 `y` 同图绘制）；默认 `None` 时输出比 `y` 少一个元素。

#### 4.2.2 核心流程

`cumulative_trapezoid(y, x=None, dx=1.0, axis=-1, initial=None)` 的流程与 `trapezoid` 高度相似，区别只在最后一步：

```text
1. xp = array_namespace(y)；y = xp.asarray(y)
2. 校验：沿 axis 至少要有 1 个点
3. 计算每段宽度 d（与 trapezoid 同理，但用 xp.diff）
4. 校验：x 沿 axis 的长度必须与 y 一致
5. 构造 slice1=[1:]、slice2=[:-1]（用 tupleset 辅助）
6. res = cumulative_sum( d * (y[slice1] + y[slice2]) / 2.0, 沿 axis )   # ← 核心：前缀和
7. 若 initial==0：在 axis 前方拼接一个 0
8. 返回 res
```

把第 6 步和 4.1.2 的第 5 步并排看，就能一眼抓住两个函数的本质差异：**`trapezoid` 用 `sum`，`cumulative_trapezoid` 用 `cumulative_sum`**。

#### 4.2.3 源码精读

同样先看装饰器与签名（注意比 `trapezoid` 多一个 `initial` 参数）：

[scipy/integrate/_quadrature.py:259-260](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L259-L260) —— `cumulative_trapezoid` 声明数组后端能力，签名包含 `initial`。

开场取命名空间并做空数组保护（沿积分轴没有任何点时直接报错）：

[scipy/integrate/_quadrature.py:311-315](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L311-L315) —— 取命名空间、转数组；沿 `axis` 长度为 0 时报错。

算宽度 `d` 的三分支。这里用的是 `xp.diff`（等价于 `x[1:] - x[:-1]`），一维分支用 `xp.reshape` 把 `d` 调整到能广播的形状；并额外校验 `d` 沿 axis 的长度恰好比 `y` 少 1：

[scipy/integrate/_quadrature.py:317-335](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L317-L335) —— 用 `xp.diff` 算每段宽度，并做形状与长度校验。

构造切片用到了小工具 `tupleset`：它把一个全选元组 `(slice(None),)*nd` 在 `axis` 位置替换成 `[1:]` 或 `[:-1]`，本质和 `trapezoid` 里的 `slice1/slice2` 一回事，只是写法更显式：

[scipy/integrate/_quadrature.py:253-256](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L253-L256) —— `tupleset`：返回把元组第 `i` 个元素替换为 `value` 后的新元组。

[scipy/integrate/_quadrature.py:337-340](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L337-L340) —— 构造错位切片，并对每个梯形面积做**累计求和** `xp.cumulative_sum`。这一行就是「累计积分」的全部实现。

最后是 `initial` 参数处理：只接受 `None` 或 `0`；为 `0` 时沿 `axis` 在最前面拼接一个 0，使输出与 `y` 等长：

[scipy/integrate/_quadrature.py:342-353](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L342-L353) —— 校验 `initial`，并在 `initial=0` 时把一个 0 拼到结果前面。

#### 4.2.4 代码实践

**实践目标**：用 `cumulative_trapezoid` 得到 \(\sin(x)\) 的累计积分曲线，并与解析解 \(-\cos(x)\)（差一个常数）对比绘图。

**操作步骤**（脚本 `cumtrap_demo.py`）：

```python
# 示例代码：累计梯形积分 vs 解析解
import numpy as np
import matplotlib.pyplot as plt
from scipy import integrate

# 等距样本
x = np.linspace(0, 2 * np.pi, 200)
y = np.sin(x)

# 累计积分；initial=0 让输出长度与 x 相同，方便画图
y_int = integrate.cumulative_trapezoid(y, x, initial=0)

# 解析解：∫ sin = -cos，从 x[0] 起算的定积分 = -cos(x) - (-cos(x[0]))
analytical = -np.cos(x) - (-np.cos(x[0]))

plt.plot(x, y_int, label="cumulative_trapezoid")
plt.plot(x, analytical, "--", label="解析 -cos(x)+C")
plt.legend(); plt.xlabel("x"); plt.ylabel("累计积分")
plt.title("cumulative_trapezoid 与解析解对比")
plt.show()

# 打印最大误差
print("最大绝对误差：", np.max(np.abs(y_int - analytical)))
```

**需要观察的现象**：
- 两条曲线几乎重合；误差随点数增加而减小。
- `y_int[0]` 恰好是 0（因为 `initial=0`），这正是累计积分从起点开始的含义。
- 若不传 `initial=0`，输出会比 `x` 少一个点，绘图前需要相应截断 `x`。

**预期结果**：用 200 个点时，最大绝对误差通常在 \(10^{-4}\) 量级；点数翻倍后误差约缩小到原来的 \(1/4\)（梯形法的二阶收敛）。

> 注：绘图所需的图形界面依运行环境而定；如无法显示，可去掉 `plt.show()` 改为 `plt.savefig("cumtrap.png")`，或只查看打印的最大误差数值。「待本地验证」具体数值。

#### 4.2.5 小练习与答案

**练习 1**：不传 `initial` 与传 `initial=0`，返回数组的长度分别是多少？为什么 `cumulative_trapezoid` 默认比 `y` 少一个点？

**参考答案**：不传 `initial` 时输出比 `y` 少一个点（因为 \(n+1\) 个样本只能定义 \(n\) 个梯形、得到 \(n\) 个累计值 \(C_1,\dots,C_n\)）；传 `initial=0` 时在最前补一个 0（对应起点 \(C_0=0\)），输出与 `y` 等长。

**练习 2**：把 `axis` 设为 `axis=0`，对形状 `(100, 3)` 的数组（可理解为 3 条独立信号、每条 100 个时间点）调用 `cumulative_trapezoid`，会得到什么形状？这说明它的「批量」能力是什么？

**参考答案**：得到形状 `(99, 3)`（或传 `initial=0` 时 `(100, 3)`）。说明 `cumulative_trapezoid` 可以一次性对多条信号做累计积分，互不干扰——这是固定样本积分函数普遍支持的「沿指定轴批量处理」能力。

---

### 4.3 `@xp_capabilities` 与数组 API 可移植性

#### 4.3.1 概念说明

你可能注意到 `trapezoid` 和 `cumulative_trapezoid` 签名上方都有 `@xp_capabilities()`。`xp` 是「数组命名空间（namespace）」的通用记号，`xp_capabilities` 就是「声明本函数支持哪些数组后端」。

为什么需要它？SciPy 历史上只支持 NumPy 数组，所有代码里写满了 `np.sum`、`np.diff`。但用户可能用 **CuPy（GPU）**、**PyTorch**、**JAX**、**Dask（分块/并行）** 等数组库。如果代码写死 `np.`，就只能在 NumPy 上跑。

数组 API（Array API）标准提供了一条出路：只要所有运算都通过一个「命名空间对象 `xp`」来调用，且 `xp` 是根据输入数组的类型自动选择的（`xp = array_namespace(y)`），同一段代码就能在多个后端上工作。`@xp_capabilities()` 装饰器则负责**声明并记录**这种支持能力。

#### 4.3.2 核心流程

装饰器本身**不改变函数的运行时计算逻辑**（它不替换 `np` 为别的后端），它主要做两件事：

```text
1.（测试侧）配合测试框架，自动为不同后端生成 SKIP / XFAIL 标记，
   并对 Dask、JAX 等做额外的后端专属校验。
2.（文档侧）在函数 docstring 里自动追加一张表，列出“已在哪些后端上测试通过”。
```

真正让函数「可移植」的是函数体里的 `xp = array_namespace(y)` 以及随后所有用 `xp.` 调用的运算（`xp.sum`、`xp.diff`、`xp.cumulative_sum`、`xp.reshape`……）。装饰器是「能力声明 + 测试驱动」，`xp.` 写法才是「实现手段」。两者配合：声明说我支持这些后端，实现确实只用数组 API 标准函数，测试又逐一验证过，于是声明可信。

#### 4.3.3 源码精读

先看装饰器定义，重点关注它 docstring 里写的「两个作用」：

[scipy/_lib/_array_api.py:864-880](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_array_api.py#L864-L880) —— `xp_capabilities` 的 docstring 明确说明：装饰器一是配合测试生成 SKIP/XFAIL 标记并做后端专属校验，二是自动给被装饰函数的 docstring 追加一张支持矩阵表。

再看 `trapezoid` 函数体里真正「消费」这种可移植性的那行——根据输入取出命名空间：

[scipy/integrate/_quadrature.py:125-126](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L125-L126) —— `xp = array_namespace(y)`：`y` 是 NumPy 数组就返回 `numpy`，是 JAX 数组就返回 `jax.numpy`，依此类推；后续运算全部走 `xp.`。

把这两段对照起来读，就能区分「装饰器（声明/测试）」与「`xp.` 写法（实现）」各自的职责。顺带一提，本文件里也有反例：`newton_cotes` 用了 `@xp_capabilities(np_only=True)`（见 [_quadrature.py:958](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L958)），明确声明它**只**支持 NumPy——因为其内部确实直接用了 `np.linalg.inv` 等 NumPy 专属功能。对比之下，更能体会 `trapezoid`/`cumulative_trapezoid` 用纯 `xp.` 写法的可贵。

#### 4.3.4 代码实践

**实践目标**：从源码层面确认「`trapezoid` 没有写死 `np.`」，并理解这种写法带来的可移植性。

**操作步骤**（源码阅读型实践，无需运行）：

1. 打开 [_quadrature.py 的 trapezoid 实现](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L125-L163)，逐行查看它的所有数组运算。
2. 统计：函数体里有几处直接写 `np.`（提示：基本没有用于核心计算），又有几处写 `xp.`（如 `xp.sum`、`xp.asarray`、`xp.broadcast_to`）。
3. 思考：如果用户传入一个 JAX 数组，`array_namespace(y)` 会返回什么？函数能否在 GPU 上跑？

**需要观察的现象**：
- 核心计算（求和、切片乘除）全部经由 `xp.` 完成，不依赖 NumPy 专属函数。
- 因此传 CuPy/JAX/PyTorch 数组时，函数会在对应后端上执行，而非先把数据搬回 NumPy。

**预期结果**：确认 `trapezoid` 的实现是「数组 API 兼容」的；这与它声明 `@xp_capabilities()`（无 `np_only=True`）一致。这正是它能进入多后端测试矩阵的原因。

#### 4.3.5 小练习与答案

**练习 1**：本文件中 `newton_cotes` 标注了 `@xp_capabilities(np_only=True)`，而 `trapezoid` 只标了 `@xp_capabilities()`。从两者函数体里找出造成这一差异的具体原因。

**参考答案**：`newton_cotes` 内部直接调用了 `np.linalg.inv`、`np.arange`、`np.array`、`gammaln` 等 NumPy 专属或未走 `xp.` 的功能（见其函数体），所以只能支持 NumPy；`trapezoid` 的核心计算全部走 `xp.sum`/`xp.asarray`/`xp.broadcast_to`，符合数组 API 标准，因此可声明多后端支持。

**练习 2**：假设你给 `trapezoid` 传一个 Dask 数组，函数会不会立刻把整个数组计算出来？结合 `xp = array_namespace(y)` 的惰性特性谈谈。

**参考答案**：不会立刻算出。`array_namespace` 对 Dask 数组返回 Dask 命名空间，`xp.sum` 等返回的仍是（惰性的）Dask 数组，真正的计算要等到用户显式 `.compute()` 才发生。这种惰性正是 Dask 适合处理「大于内存」数据的原因，也是「数组 API」抽象带来的额外好处。

## 5. 综合实践

把本讲三个知识点串起来：**固定样本积分的几何含义**、**`trapezoid` vs `cumulative_trapezoid`**、**等距 vs 非等距样本**。

**任务背景**：你有一段 0–5 秒内物体的速度记录 \(v(t)=t\,e^{-t}\)（先加速后减速）。请你仅凭离散样本完成下面三件事：

```python
# 示例代码：综合实践——从速度样本到总位移与位置曲线
import numpy as np
import matplotlib.pyplot as plt
from scipy import integrate

# (A) 非等距采样：在 v 变化快的起点附近密采，后面稀疏
x_non = np.sort(np.concatenate([
    np.linspace(0, 1.0, 50),   # 起点附近加密
    np.linspace(1.0, 5.0, 20), # 后半段稀疏
]))
v_non = x_non * np.exp(-x_non)

# 1) 总位移 = trapezoid 一次算完
total = integrate.trapezoid(v_non, x=x_non)

# 2) 位置曲线 = cumulative_trapezoid 逐点累加
pos = integrate.cumulative_trapezoid(v_non, x=x_non, initial=0)

# (B) 对照：等距采样
x_eq = np.linspace(0, 5, 200)
v_eq = x_eq * np.exp(-x_eq)
total_eq = integrate.trapezoid(v_eq, x=x_eq)

# 解析总位移：∫0^5 t e^{-t} dt = [1 - (t+1)e^{-t}]_0^5 = 1 - 6e^{-5} ≈ 0.9596
analytical_total = 1 - 6 * np.exp(-5)
print(f"非等距总位移 = {total:.6f}")
print(f"等距总位移   = {total_eq:.6f}")
print(f"解析总位移   = {analytical_total:.6f}")

plt.plot(x_non, pos, "o-", label="位置（cumulative_trapezoid）")
plt.plot(x_non, v_non, "--", label="速度 v(t)")
plt.legend(); plt.xlabel("t"); plt.show()
```

**你要回答的问题**：
1. 非等距采样与等距采样算出的「总位移」是否都接近解析值？这说明 `trapezoid` 对样本间距是否有要求？
2. 位置曲线（`pos`）是否单调上升到一个平台？平台高度是否≈总位移？这说明了 `cumulative_trapezoid` 的最后一个值与 `trapezoid` 结果的关系。
3. （进阶）如果把 `v_non` 换成 `np.array(...)` 之外的类型，比如一个 CuPy 数组，函数能否照常工作？依据 4.3 节解释原因。

**预期结论**：
1. 两种采样给出的总位移都接近解析值 \(1-6e^{-5}\approx 0.9596\)；`trapezoid` 只要求知道每段宽度，**不要求等距**——非等距样本通过 `x` 参数同样有效。
2. 位置曲线单调上升到平台，平台高度≈总位移；`cumulative_trapezoid(...)[-1]` 与 `trapezoid(...)` 给出同一个定积分（差异仅来自浮点累计顺序）。
3. 可以。因为核心运算全走 `xp.`，`array_namespace` 会返回对应后端，函数对数组类型不敏感。

> 注：本脚本未在讲义环境中实际运行，请以本地运行结果核对数值（「待本地验证」）。

## 6. 本讲小结

- `trapezoid` 和 `cumulative_trapezoid` 都属于**固定样本积分**：只有离散点 \((x_i, y_i)\)，用复合梯形公式估算面积，不涉及被积函数表达式、也不给出误差估计。
- 复合梯形公式 \(\sum \frac{y_i+y_{i+1}}{2}(x_{i+1}-x_i)\) 在源码里被优雅地向量化为「错位切片 `y[1:]` 与 `y[:-1]` 配对 → 乘以宽度 `d` → 求和/累计求和」。
- 两个函数的本质差别只有一处：`trapezoid` 用 `xp.sum`（返回整段定积分），`cumulative_trapezoid` 用 `xp.cumulative_sum`（返回积分曲线）。
- 宽度 `d` 有三种来源：`x=None` 时用标量 `dx`；`x` 一维时用 `xp.diff(x)`；`x` 与 `y` 同维时按位置差分——因此函数**支持非等距样本**。
- 沿任意 `axis` 积分使两个函数都能对多维数组「批量」处理，结果在积分轴上降一维。
- `@xp_capabilities()` 负责声明并测试「支持哪些数组后端」，函数体里的 `xp = array_namespace(y)` 与 `xp.` 写法才是实现多后端（NumPy/CuPy/JAX/Dask）可移植的真正手段。

## 7. 下一步学习建议

- **本单元内**：进入 [u2-l2 辛普森法 simpson 与 cumulative_simpson](u2-l2-simpson.md)，看如何用抛物线（而非直线）连接相邻点，在相同样本数下获得更高精度（\(O(h^4)\)）；届时你会再次看到 `cumulative_sum` 与类似的错位切片技巧。
- **随后**：阅读 [u2-l3 龙贝格 romb、newton_cotes 与 fixed_quad](u2-l3-romb-newton-cotes-fixed-quad.md)，了解基于 \(2^k+1\) 等距样本的 Romberg 外推、牛顿-柯特斯权重，以及定阶高斯-勒让德积分 `fixed_quad`（它和 `trapezoid` 不同，需要传入**函数**而非样本）。
- **横向**：若想体会「函数积分」与「固定样本积分」的边界，可跳读 [u3-l1 quad 自适应积分](u3-l1-quad-adaptive.md)，对比 `quad`（持有函数、自适应取点、有误差估计）与本讲 `trapezoid`（只有样本、无误差估计）的差别。
- **源码延伸**：本讲多次出现的 `xp.diff` / `xp.cumulative_sum` / `xp.reshape` 都来自数组 API 标准；想深入可结合 [u13-l3 架构取舍与最佳实践](u13-l3-architecture-tradeoffs.md) 中关于 `xp_capabilities` 的系统讨论。
