# 有限差分权重 _derivative_weights

## 1. 本讲目标

本讲打开 `derivative` 黑盒的「数学内核」：有限差分公式的**权重（weights）**是如何算出来的。

读完本讲你应当能够：

- 用 **Taylor 展开**推导出任意一组求值点对应的差分权重，理解它本质是一个 **Vandermonde 线性方程组**。
- 说清楚**中心差分 stencil** 与**单侧差分 stencil** 各自的几何结构（求值点摆在 `x` 的什么位置、为什么这样摆）。
- 解释 `diff_state` 的**权重缓存机制**：何时复用、何时重算，以及为什么权重要单独包进一个 `_RichResult`。

本讲只聚焦「权重从哪来」，**不**展开「每轮新增哪些求值点」「如何拼接函数值」——那是 u2-l3（`pre_func_eval`）与 u2-l4（`post_func_eval`）的主题。

---

## 2. 前置知识

- **Taylor 展开**：对足够光滑的函数 \(f\)，在点 \(x\) 附近有

  \[
  f(x+\delta)=f(x)+f'(x)\,\delta+\frac{f''(x)}{2!}\,\delta^{2}+\frac{f'''(x)}{3!}\,\delta^{3}+\cdots
  \]

  它告诉我们：只要知道 \(f\) 在 \(x\) 附近若干点的函数值，就能「反推」出 \(f'(x)\)。

- **有限差分**：用若干离散点上的函数值的**加权组合**去逼近导数。承接 u1-l2，`derivative` 默认使用「8 阶」差分公式，这里的「阶」指误差对步长的幂次，而非求导阶数（只算一阶导）。

- **线性方程组与 Vandermonde 矩阵**：形如 \(A\mathbf{w}=\mathbf{b}\) 的线性方程组；Vandermonde 矩阵是各幂次构成的方阵，是多项式插值的经典工具。

- **承接 u2-l1**：`derivative` 主流程在校验完输入后，把参数重命名为内部短名——`step_factor`→`fac`、`initial_step`→`h0`、`step_direction`→`hdir`、`order`→`terms=(order+1)//2`（即 `n`，阶数的一半，因为阶数恒为偶数）。本讲里出现的 `fac`、`work.terms`、`work.diff_state` 都来自这个 `work` 对象。

---

## 3. 本讲源码地图

本讲几乎全部围绕单文件中的一个函数：

| 位置 | 作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py:L601-L718`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L601-L718) | `_derivative_weights`：本讲主角，计算中心 / 单侧差分权重，含 Taylor 推导注释与缓存逻辑。 |
| [`scipy/differentiate/_differentiate.py:L434-L441`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L434-L441) | `derivative` 中 `work` 对象的构建，初始化 `terms` 与 `diff_state`（缓存容器）。 |
| [`scipy/differentiate/_differentiate.py:L449-L493`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L449-L493) | `pre_func_eval`：按 stencil 生成实际求值横坐标 `x_eval`（本讲用以核对 stencil 几何）。 |
| [`scipy/differentiate/_differentiate.py:L545-L549`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L545-L549) | `post_func_eval`：调用 `_derivative_weights` 并把权重作用到函数值上得到 `df`。 |

---

## 4. 核心概念与源码讲解

### 4.1 Taylor 展开与 Vandermonde 方程组

#### 4.1.1 概念说明

数值微分的核心问题可以这样表述：

> 给定一组「相对步长」\(h_0,h_1,\dots,h_{m}\)（无量纲的偏移量），能否找到一组**权重** \(w_0,w_1,\dots,w_m\)，使得把函数在这 \(m+1\) 个点 \(x+h_i H\) 上的值加权求和后，恰好得到 \(H\,f'(x)\)（再多除以 \(H\) 就是导数 \(f'(x)\)）？这里 \(H\) 是当前迭代的实际步长（`work.h`）。

注意一个关键设计：**权重只依赖各点的「相对位置」\(h_i\)，与具体步长 \(H\) 无关**。所以同一组权重可以用于每一轮迭代——只是每次换一个更小的 \(H\)。这正是后续缓存机制能成立的前提。

把每个点的 Taylor 展开代入「加权求和」表达式：

\[
\sum_i w_i\, f(x+h_i H)=\sum_i w_i\sum_{k=0}^{\infty}\frac{f^{(k)}(x)}{k!}(h_i H)^{k}
=\sum_{k=0}^{\infty}\frac{f^{(k)}(x)}{k!}H^{k}\underbrace{\left(\sum_i w_i\,h_i^{k}\right)}_{\text{权重的「矩」}}
\]

我们希望左式 \(=H f'(x)\)，即只保留 \(k=1\) 那一项。于是对权重提出**矩条件**：

\[
\sum_i w_i\,h_i^{k}=
\begin{cases}
1, & k=1\\
0, & k\neq 1
\end{cases}
\]

这就是一个**线性方程组**：未知数是权重 \(w_i\)，系数矩阵的第 \((k,i)\) 个元素是 \(h_i^{k}\)。这个矩阵正是 **Vandermonde 矩阵**。

#### 4.1.2 核心流程

`_derivative_weights` 把上述推导「直译」为代码：

1. 给定一组相对偏移 `h`（一个长度为 \(2n+1\) 的数组，\(n\) = `terms` = `order//2`）。
2. 构造 Vandermonde 矩阵 `A`，满足 `A[k, i] = h[i]**k`。
3. 构造右端 `b`：`b[1] = 1`，其余为 0（对应矩条件里 \(k=1\) 取 1）。
4. 解 `A @ weights = b` 得到权重。
5. 对中心差分额外**强加对称性**以提升数值精度。

用伪代码概括：

```
h   = 由 stencil 决定的一组相对偏移        # 长度 2n+1
A   = Vandermonde(h) 的转置                # A[k,i] = h[i]**k
b   = zeros(2n+1);  b[1] = 1              # 只取一阶导项
w   = solve(A, b)                          # 解出权重
# 中心差分：强加反对称  w[n]=0, w[-i-1]=-w[i]
```

误差阶数：共有 \(2n+1\) 个权重，可满足 \(k=0,1,\dots,2n\) 共 \(2n+1\) 个矩条件，于是加权和中第一个未被消去的项是 \(k=2n+1\)，对应导数估计的误差为 \(O(H^{2n})\)。默认 `order=8`（\(n=4\)）即误差 \(O(H^8)\)，这与 docstring 中「8 阶公式」的说法一致。

#### 4.1.3 源码精读

函数开头是一段长注释，把上面的 Taylor 推导讲得非常清楚，并以最简单的 3 点中心差分（`f(x)`、`f(x+h)`、`f(x-h)`）为例：

[`scipy/differentiate/_differentiate.py:L612-L630`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L612-L630) — 注释用 3 点推出 \(f'(x)\approx (w_1 f(x)+w_2 f(x+h)+w_3 f(x-h))/h\)，误差 \(O(h^2)\)，即「二阶」逼近。

真正求解权重的是这几行（中心差分分支，下节细讲 `h` 的构造）：

```python
A = np.vander(h, increasing=True).T
b = np.zeros(2*n + 1)
b[1] = 1
weights = np.linalg.solve(A, b)
```

[`scipy/differentiate/_differentiate.py:L688-L691`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L688-L691) — 这四行就是「Taylor → Vandermonde → solve」的全部实现。

要特别注意 `np.vander(h, increasing=True).T` 的方向。`np.vander(h, increasing=True)` 返回矩阵 `M[i,j] = h[i]**j`（行索引点是 `i`，列索引幂次是 `j`）；`.T` 转置后 `A[j,i] = h[i]**j`。于是 `(A @ weights)[j] = sum_i h[i]**j * weights[i]`，配上 `b[1]=1`、其余 `b[j]=0`，正是矩条件 \(\sum_i w_i h_i^{j}=\delta_{j,1}\)。**方向之所以要转置，是为了让 `A @ weights` 直接给出按幂次 `j` 排列的矩**，与右端 `b` 对齐。

> 小贴士：这里**用的是 NumPy 而非 `xp`**（跨后端命名空间）。注释 [`L682-L683`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L682-L683) 说明：权重只算一次再缓存，用 NumPy 没有性能问题；算完后再转成对应后端的数组（见 4.3.3 的 `return`）。

#### 4.1.4 代码实践

**实践目标**：用一个最小例子（3 点）亲眼看 Vandermonde 方法复现教科书里的中心差分公式。

**操作步骤**（示例代码，独立运行，不依赖 SciPy 内部接口）：

```python
import numpy as np

# 3 点中心差分：相对偏移 h = [-1, 0, 1]
h = np.array([-1.0, 0.0, 1.0])
A = np.vander(h, increasing=True).T   # A[k,i] = h[i]**k
b = np.zeros(3); b[1] = 1             # 只取一阶导项
w = np.linalg.solve(A, b)
print("weights =", w)
```

**需要观察的现象**：解出的权重应形如 `[-0.5, 0, 0.5]`，于是

\[
f'(x)\approx \frac{-0.5\,f(x-H)+0\cdot f(x)+0.5\,f(x+H)}{H}=\frac{f(x+H)-f(x-H)}{2H},
\]

正是经典二阶中心差分公式。

**预期结果**：`weights = [-0.5, 0.0, 0.5]`（待本地验证：数值可能有 ~1e-17 量级的浮点误差）。这一步验证了「Vandermonde + `b[1]=1`」确实在抽取一阶导系数。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `b` 改成 `b[2] = 1`（其余为 0），解出的权重在逼近什么？

**参考答案**：矩条件变成 \(\sum_i w_i h_i^{k}=\delta_{k,2}\)，加权求和中保留的是 \(k=2\) 项，即 \(\frac{f''(x)}{2!}H^{2}\)，再除以 \(H^{2}\) 就得到二阶导 \(f''(x)\)。这正是把同一套 Vandermonde 思路用于「二阶导」的做法（SciPy 的 `hessian` 走的是另一条路：嵌套 `jacobian`，见 u3-l2）。

**练习 2**：为什么矩条件里 \(k=0\) 必须为 0（即 \(\sum_i w_i=0\)）？

**参考答案**：\(k=0\) 对应 \(f(x)\) 本身（常数项）。导数与函数的「绝对大小」无关，只与「变化率」有关，所以常数项必须被消去，否则导数估计会带上一个与 \(f(x)\) 成正比的偏差。

---

### 4.2 中心差分 stencil

#### 4.2.1 概念说明

**stencil（差分模板）**指差分公式所用到的那组求值点的空间布局。中心差分 stencil 在 `x` 左右两侧对称取点。

默认 `order=8`（\(n=4\)）、`step_factor=2`（即 `fac=2`）时，源码注释给出的中心 stencil 是：

\[
x-\tfrac{H}{c^{3}},\;x-\tfrac{H}{c^{2}},\;x-\tfrac{H}{c},\;x-H,\;x,\;x+H,\;x+\tfrac{H}{c},\;x+\tfrac{H}{c^{2}},\;x+\tfrac{H}{c^{3}}
\]

其中 \(c=\text{fac}=2\)，\(H\) 是当前步长。两侧各 4 个点 + 中心点 \(x\)，共 \(2n+1=9\) 个点。

注意一个反直觉之处：**点距 `x` 的远近按几何级数（公比 \(1/c\)）排布**，而不是均匀分布。最远的点是 \(x\pm H\)，越往外（按数组顺序）越靠近 `x`。这样设计的动机是「**嵌套（nesting）**」——下一轮把步长缩小为 \(H/c\) 后，绝大多数点可以复用（详见 u2-l3）。

#### 4.2.2 核心流程

`_derivative_weights` 用三行生成中心 stencil 的「相对偏移」数组 `h`：

```python
i = np.arange(-n, n + 1)   # 整数下标 [-n .. n]
p = np.abs(i) - 1.         # 每个点的「幂次」
s = np.sign(i)             # 每个点的符号：左负、中零、右正
h = s / fac ** p           # 相对偏移 = 符号 / fac 的幂
```

逐点拆开（\(n=4, c=2\)）：

| 下标 `i` | 符号 `s` | 幂次 `p` | 相对偏移 `h` | 对应求值点 |
| :--: | :--: | :--: | :--: | :-- |
| -4 | -1 | 3 | \(-1/c^{3}\) | \(x-H/8\) |
| -3 | -1 | 2 | \(-1/c^{2}\) | \(x-H/4\) |
| -2 | -1 | 1 | \(-1/c\) | \(x-H/2\) |
| -1 | -1 | 0 | \(-1\) | \(x-H\) |
|  0 |  0 | -1 | \(0\)（注释：中心点 `p=-1` 但 `s=0`，故为 0） | \(x\) |
| +1 | +1 | 0 | \(+1\) | \(x+H\) |
| +2 | +1 | 1 | \(+1/c\) | \(x+H/2\) |
| +3 | +1 | 2 | \(+1/c^{2}\) | \(x+H/4\) |
| +4 | +1 | 3 | \(+1/c^{3}\) | \(x+H/8\) |

中心点取 `p=-1` 是个小技巧：因为 `s=0`，无论 `fac**p` 是多少，`0 / 任何数 = 0`，所以中心点的相对偏移自动为 0，无需特判（注释 [`L684`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L684)）。

随后用上节的 Vandermonde 方法解出权重，并**强加对称性**：

```python
# Enforce identities to improve accuracy
weights[n] = 0
for i in range(n):
    weights[-i-1] = -weights[i]
```

[`scipy/differentiate/_differentiate.py:L693-L696`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L693-L696) — 强行令权重关于中心反对称：中心为 0，右半是左半的相反数。

为什么可以这样做？因为中心 stencil 本身关于 `x` 对称，**精确解**必然满足反对称 \(w(-i)=-w(i)\)、\(w(0)=0\)。浮点 `solve` 会引入极小的对称性误差，这里把它「抹平」回理论值，能在高阶情形下挽回几位有效数字。

#### 4.2.3 源码精读

中心 stencil 的构造与求解集中在这一段：

```python
if len(diff_state.central) != 2*n + 1:
    i = np.arange(-n, n + 1)
    p = np.abs(i) - 1.  # center point has power `p` -1, but sign `s` is 0
    s = np.sign(i)

    h = s / fac ** p
    A = np.vander(h, increasing=True).T
    b = np.zeros(2*n + 1)
    b[1] = 1
    weights = np.linalg.solve(A, b)

    # Enforce identities to improve accuracy
    weights[n] = 0
    for i in range(n):
        weights[-i-1] = -weights[i]

    diff_state.central = weights
```

[`scipy/differentiate/_differentiate.py:L679-L700`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L679-L700) — 中心差分权重的完整计算：构造 `h`、解 Vandermonde 系统、强加对称性、写入缓存。`if len(...) != 2*n+1` 是缓存判断（见 4.3.3）。

> 求出的权重**顺序**是 `[-1/c³, -1/c², -1/c, -1, 0, +1, +1/c, +1/c², +1/c³]`（按幂次 `p` 排列，并非按物理位置排列）。`post_func_eval` 必须把函数值按同样顺序拼好才能与权重点乘——这就是 u2-l4 要讲的「拼接技巧」。

#### 4.2.4 代码实践

**实践目标**：用 NumPy 完整复现 `_derivative_weights` 中**默认参数（`order=8`、`fac=2.0`）下的中心差分权重**，并验证源码强加的对称性。

**操作步骤**（示例代码，直接照搬源码逻辑）：

```python
import numpy as np

n   = 4                # order = 2*n = 8
fac = 2.0              # step_factor

# 1) 构造相对偏移 h = s / fac**p  （与源码 _differentiate.py:L683-L687 一致）
i = np.arange(-n, n + 1)
p = np.abs(i) - 1.
s = np.sign(i)
h = s / fac ** p
print("h =", h)

# 2) Vandermonde 系统并求解  （与源码 L688-L691 一致）
A = np.vander(h, increasing=True).T
b = np.zeros(2 * n + 1); b[1] = 1
w_raw = np.linalg.solve(A, b)
print("raw weights   =", w_raw)

# 3) 强加对称性  （与源码 L694-L696 一致）
w = w_raw.copy()
w[n] = 0
for k in range(n):
    w[-k - 1] = -w[k]
print("sym weights   =", w)

# 4) 验证对称性 weights[-i-1] == -weights[i]
for k in range(n):
    assert np.isclose(w[-k - 1], -w[k])
assert w[n] == 0
print("对称性验证通过 ✓")

# 5) 用这组权重估一个导数，看是否合理
H, x = 0.25, 1.0
fc = np.exp(x + h * H)        # 在 stencil 点上取值
df = fc @ w / H               # 加权求和 / 步长
print(f"df 估计 = {df:.10f}, 真值 = {np.exp(1.0):.10f}")
```

**需要观察的现象**：

- `h` 是一个长度 9、关于中心反对称的数组（左半负、右半正、中心 0）。
- `raw weights` 已经**近似**反对称（左右大小相近、符号相反），但并非严格相等。
- `sym weights` 被强行设为严格反对称，中心为 0。
- 用这组权重算出的 `df` 与 \(e^1\approx 2.718281828\) 高度吻合（误差量级远小于步长效应）。

**预期结果**：第 4 步断言全部通过；第 5 步 `df` 与真值的前若干位有效数字一致（待本地验证具体位数；定性上应明显比 2 点差分更精确）。

#### 4.2.5 小练习与答案

**练习 1**：把脚本里的 `fac` 改成 `1.5`（即调用 `derivative(..., step_factor=1.5)`），重算 `h` 与权重。stencil 的点会变密还是变疏？对称性还成立吗？

**参考答案**：`fac` 变小意味着相邻幂次之间 `fac**p` 的差距变小，所以 stencil 点在 `x` 附近排得更**密**（最远点仍是 \(x\pm H\)，但内侧点更靠近 \(H\)）。对称性依然严格成立，因为它是由 stencil 的对称结构决定的，与 `fac` 数值无关。

**练习 2**：为什么不直接把中心点的权重设为它「自然」的浮点解，而要强制 `weights[n]=0`？

**参考答案**：理论上中心点对**一阶**中心差分没有贡献（其 Taylor 展开里没有正比的 \(h\) 项被它单独提供），精确权重应为 0。浮点 `solve` 会给出一个 ~1e-16 的非零残差；强制清零避免了这点微小误差在高阶公式里被放大。

---

### 4.3 单侧差分 stencil 与权重缓存

#### 4.3.1 概念说明

当 `x` 落在函数定义域的边界附近（例如 \(f\) 只在 \(x\ge 0\) 有定义），中心差分向左取点会越界。这时需要**单侧差分**：所有点都取在 `x` 的同一侧。`step_direction` 决定用哪一侧（详见 u4-l2）。

右侧（`step_direction>0`）单侧 stencil 在默认参数下取：

\[
x,\;x+H,\;x+\tfrac{H}{d},\;x+\tfrac{H}{d^{2}},\;\dots,\;x+\tfrac{H}{d^{7}}
\]

其中 \(d=\sqrt{c}=\sqrt{\text{fac}}\)。共 \(2n+1=9\) 个点，从 \(x\) 出发向右以公比 \(1/d\) 几何递减地排布。左侧单侧 stencil 只是把所有偏移取负号。

**为什么单侧用 \(\sqrt{c}\) 而不是 \(c\) 作为公比？** 注释 [`L655-L656`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L655-L656) 解释：当步长每轮缩小因子 \(c=d^{2}\) 时，几何级数的下标整体平移 2，于是每轮恰有 **2 个最远点被丢弃、2 个新点被加入**——与中心差分「每轮新增 2 点」的节奏一致。这就保证了中心 / 单侧两种 stencil 在同一迭代框架下函数调用次数同步。

> 与中心差分不同，单侧 stencil **不对称**，因此没有可强加的反对称性；其权重直接采用 `solve` 的结果。左侧权重 = 右侧权重的相反数，由调用方在 `post_func_eval` 里取负实现（见 4.3.3）。

**权重缓存**：权重只依赖 `fac` 与 `n`，与具体的 `x`、`H` 无关。因此同一次 `derivative` 调用里，无论迭代多少轮、有多少个 `x` 元素，权重都只需算**一次**。`_derivative_weights` 用挂在 `work` 上的 `diff_state` 对象缓存它。

#### 4.3.2 核心流程

单侧权重的构造几乎与中心分支同构，区别只在 `h` 的生成（指数与底数不同）：

```python
i = np.arange(2*n + 1)          # [0, 1, ..., 2n]
p = i - 1.                      # 幂次 [-1, 0, ..., 2n-1]
s = np.sign(i)                  # [0, 1, ..., 1]  （首项符号为 0 → 偏移为 0，即 x 本身）
h = s / np.sqrt(fac) ** p       # 相对偏移，公比 1/sqrt(fac)
A = np.vander(h, increasing=True).T
b = np.zeros(2*n + 1); b[1] = 1
weights = np.linalg.solve(A, b) # 不强加对称性
```

缓存判断与失效逻辑（同时服务于中心与单侧两套权重）：

```
fac = float(work.fac)                       # 统一转双精度，避免权重引入额外误差
if fac != diff_state.fac:                   # step_factor 变了 → 作废缓存
    diff_state.central = []
    diff_state.right   = []
    diff_state.fac     = fac
if len(diff_state.central) != 2*n + 1:      # 还没算过 / order 变了 → 重算
    (计算 central 与 right 两套权重并写回 diff_state)
return (xp.asarray(diff_state.central, dtype=work.dtype),
        xp.asarray(diff_state.right,   dtype=work.dtype))
```

两个 `if` 协同覆盖三种情形：① 首次调用（`central==[]`，长度 0 ≠ \(2n+1\)）；② `fac` 改变（先把两套清空，长度变 0 → 触发第二个 `if`）；③ `order` 改变（`n` 变 → \(2n+1\) 变 → 触发第二个 `if`）。

#### 4.3.3 源码精读

**单侧权重**的计算段：

```python
i = np.arange(2*n + 1)
p = i - 1.
s = np.sign(i)

h = s / np.sqrt(fac) ** p
A = np.vander(h, increasing=True).T
b = np.zeros(2 * n + 1)
b[1] = 1
weights = np.linalg.solve(A, b)

diff_state.right = weights
```

[`scipy/differentiate/_differentiate.py:L705-L715`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L705-L715) — 单侧（右）权重：同样的 Vandermonde 系统，只是 `h` 的几何公比换成 \(1/\sqrt{fac}\)。注释 [`L702-L704`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L702-L704) 指出左侧权重 = −右侧权重，故不单独计算。

**双精度与缓存的衔接**：

[`scipy/differentiate/_differentiate.py:L665`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L665) — `fac = float(work.fac)`：即使用户传入的 `x` 是单精度，这里也强制用双精度算权重，避免权重本身引入额外误差。

[`scipy/differentiate/_differentiate.py:L673-L677`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L673-L677) — 缓存失效判断：`fac` 变化时清空两套权重并更新记录的 `fac`。

**返回时的后端转换**：

```python
return (xp.asarray(diff_state.central, dtype=work.dtype),
        xp.asarray(diff_state.right, dtype=work.dtype))
```

[`scipy/differentiate/_differentiate.py:L717-L718`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L717-L718) — 缓存里存的是 NumPy 数组；返回时按当前后端 `xp` 和 `dtype` 转换，使其能与 `post_func_eval` 里的函数值（`fc`、`fo`）做点乘。

**缓存的载体 `diff_state`**：在 `derivative` 主流程构造 `work` 时初始化：

```python
diff_state=_RichResult(central=[], right=[], fac=None)
```

[`scipy/differentiate/_differentiate.py:L441`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L441) — `diff_state` 初值为空列表 + `fac=None`，保证首轮一定触发重算。

为什么要把权重包进一个嵌套的 `_RichResult`，而不是直接当作 `work` 的数组属性？看上一行的注释 [`L439-L440`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L439-L440)：

> Store the weights in an object so they can't get compressed.

`eim._loop` 框架（u2-l6）每轮会把 `work` 里**已收敛元素**压缩掉以省算力。`work.fs`、`work.df` 这些数组都会被按活跃元素重新切片。但权重是「全体元素共享、与元素无关」的，绝不能被压缩。把它放进一个不透明的 `_RichResult` 对象里，框架就把它当成不可拆分的整体保留下来。这是「逐元素迭代框架」与「全局共享权重」之间的一处精巧折中。

**权重如何被使用**：在 `post_func_eval` 里，`_derivative_weights` 返回的 `wc`（中心）/`wo`（单侧）与函数值点乘再除以步长：

```python
wc, wo = _derivative_weights(work, n, xp)
work.df = xpx.at(work.df)[ic].set(fc @ wc / work.h[ic])   # 中心
work.df = xpx.at(work.df)[io].set(fo @ wo / work.h[io])   # 单侧（右）
work.df = xpx.at(work.df)[il].multiply(-1)                # 单侧（左）= 右取负
```

[`scipy/differentiate/_differentiate.py:L545-L549`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L545-L549) — 注意左侧（`il`）复用右侧权重 `wo`，再整体乘 −1，正好对应「左侧权重 = −右侧权重」。`fc @ wc` 即 \(\sum_i f_i w_i\)，除以 `work.h` 还原出导数。

#### 4.3.4 代码实践

**实践目标**：复现单侧权重，验证「左侧 = −右侧」；再用一个计数器验证缓存确实只让 `_derivative_weights` 的重计算发生一次（跨多轮迭代）。

**操作步骤一：单侧权重复现**

```python
import numpy as np
n, fac = 4, 2.0
d = np.sqrt(fac)

# 右侧 stencil 的相对偏移（与源码 L705-L709 一致）
i2 = np.arange(2 * n + 1)
p2 = i2 - 1.
s2 = np.sign(i2)
h2 = s2 / d ** p2
print("right h =", h2)

A2 = np.vander(h2, increasing=True).T
b2 = np.zeros(2 * n + 1); b2[1] = 1
wo = np.linalg.solve(A2, b2)
print("right weights =", wo)

# 用右侧权重估 exp 在 x=1 的右导数（H=0.25）
H, x = 0.25, 1.0
fr = np.exp(x + h2 * H)
print("right df =", fr @ wo / H, " true =", np.exp(1.0))
```

**需要观察的现象**：`right h` 全部非负（首项为 0，其余递减），stencil 全在 `x` 右侧；`right weights` 不具反对称性（有正有负、大小不一）；估出的右导数与真值吻合。

**操作步骤二：观察缓存命中**（通过 `derivative` 的 `callback` 间接验证，避免改源码）

```python
from scipy.differentiate import derivative
import numpy as np

calls = {"n": 0}
def f(x):
    calls["n"] += 1
    return np.exp(x)

# 强制跑满多轮，看函数总调用次数是否远小于「每轮都重算 stencil」所需的量
res = derivative(f, 1.0, tolerances=dict(atol=0, rtol=0), maxiter=6)
print("nit =", res.nit, " nfev =", res.nfev)
```

**需要观察的现象**：`nfev` 的增长符合 docstring 描述的「首轮 `order+1` 个点，之后每轮只新增 2 点」——这间接说明权重 / stencil 被**复用**而非每轮重建（若每轮都重新设计完全独立的 stencil，函数调用次数会高得多）。

**预期结果**：步骤一估出的右导数与 \(e\) 的前几位有效数字一致；步骤二中 `nfev` 随 `nit` 大致按「首轮 9 点 + 每轮 2 点」增长（待本地验证具体数值）。

#### 4.3.5 小练习与答案

**练习 1**：若用户在一次 `derivative` 调用里把 `step_factor` 从 2 改成 3（注意：`step_factor` 是标量，整个调用只有一个值），`diff_state` 会怎样反应？

**参考答案**：一次调用内 `fac` 恒定，不存在「中途改变」。`diff_state` 在首轮进入 `_derivative_weights` 时，因 `fac=None != 3` 且 `central==[]`，会按 `fac=3` 算一次权重并缓存；之后所有迭代、所有元素都复用这同一组权重。`fac != diff_state.fac` 这个分支主要防御的是「跨调用复用 `work`」或测试中切换参数的场景。

**练习 2**：为什么单侧权重的 `h` 用 `np.sqrt(fac)` 作底，而中心用 `fac`？

**参考答案**：为了让单侧 stencil 在「步长每轮缩小 \(c\)」时的嵌套节奏与中心一致。中心点按 \(c\) 的幂排布，步长缩 \(c\) 下标平移 1，每侧丢 1 点；单侧若也按 \(c\) 排布会每轮只平移 1、丢 1 点，与其「单边」几何不匹配。改用 \(d=\sqrt{c}\) 作底，步长缩 \(c=d^{2}\) 时下标平移 2，每轮丢 2 点、加 2 点，与中心「每轮加 2 点」对齐，从而两类 stencil 共用同一套迭代 / 估值流程。

---

## 5. 综合实践

把本讲的三块知识（Vandermonde 推导、中心 / 单侧 stencil、权重与函数值的点乘）串起来，**手工实现一个简化版 `derivative` 单步估计**，并与 SciPy 的 `derivative` 对照。

**任务**：

1. 选定 `order=6`（即 \(n=3\)）、`step_factor=2.0`、当前步长 `H=0.5/2=0.25`。
2. 按本讲方法，分别算出**中心**与**右侧单侧**两组权重（不强加对称性的 raw 版本，以及对中心强加对称性的版本，比较差异）。
3. 对 \(f(x)=\sin(x)\) 在 \(x=1.0\)：
   - 用中心权重估 \(f'(1)\)，与 \(\cos(1)\) 比较；
   - 用右侧单侧权重估 \(f'(1)\)，与 \(\cos(1)\) 比较。
4. 调用 `derivative(np.sin, 1.0, order=6, step_factor=2.0, maxiter=1)`，把它的 `res.df` 与你的手算结果对照（`maxiter=1` 让 SciPy 只做首轮估计，便于对比）。

**参考框架**（示例代码）：

```python
import numpy as np
from scipy.differentiate import derivative

n, fac, H, x = 3, 2.0, 0.25, 1.0

# 中心权重
ic = np.arange(-n, n + 1)
hc = np.sign(ic) / fac ** (np.abs(ic) - 1)
wc = np.linalg.solve(np.vander(hc, increasing=True).T, np.eye(2*n+1)[1])

# 右侧单侧权重
ir = np.arange(2*n + 1)
hr = np.sign(ir) / np.sqrt(fac) ** (ir - 1)
wo = np.linalg.solve(np.vander(hr, increasing=True).T, np.eye(2*n+1)[1])

fc, fr = np.sin(x + hc * H), np.sin(x + hr * H)
print("手算 中心 df =", fc @ wc / H, " 真值 =", np.cos(1.0))
print("手算 右侧 df =", fr @ wo / H, " 真值 =", np.cos(1.0))

res = derivative(np.sin, 1.0, order=6, step_factor=2.0, maxiter=1,
                 tolerances=dict(atol=0, rtol=0))
print("SciPy  derivative df =", res.df)
```

**预期结果**：你的手算中心估计与 SciPy `maxiter=1` 的 `res.df` 应当**几乎完全相等**（因为两者用同一套 stencil 与权重，差别仅可能在浮点末位）。若两者显著不符，先检查你的 `hc`/`hr` 顺序是否与权重顺序一致——这正是本讲反复强调的「权重顺序 = stencil 幂次顺序」。

> 待本地验证：把中心权重的 raw 版与强加对称版都算出来，记录两者给出的 `df` 差异（通常在 1e-15 量级），体会源码注释里「improve accuracy」的含义。

---

## 6. 本讲小结

- 有限差分权重的本质是解一个 **Vandermonde 线性方程组** \(A\mathbf{w}=\mathbf{b}\)：矩条件 \(\sum_i w_i h_i^{k}=\delta_{k,1}\) 要求右端 `b[1]=1`、其余为 0，从而抽出一阶导系数。
- **中心 stencil** 关于 `x` 对称，点按公比 \(1/\text{fac}\) 几何排布；其精确权重具有反对称性，源码**强加** `weights[n]=0`、`weights[-i-1]=-weights[i]` 以抹平浮点残差、提升精度。
- **单侧 stencil** 全在 `x` 一侧，按公比 \(1/\sqrt{\text{fac}}\) 排布（取 \(\sqrt{c}\) 是为了让每轮「丢 2 点、加 2 点」与中心同步）；左侧权重 = −右侧权重，故只算右侧。
- 权重只依赖 `fac` 与 `n`，与 `x`、`H` 无关，因此同一次调用里**只需算一次**；`diff_state` 按挂在 `work` 上的 `_RichResult` 缓存，并在 `fac` 或 `order` 变化时失效重算。
- 把权重包进嵌套 `_RichResult`，是为了防止 `eim._loop` 的「已收敛元素压缩」误伤这块全局共享数据。
- 算出的权重最终在 `post_func_eval` 里以 `fc @ wc / H` 的形式作用到函数值上；权重顺序与函数值顺序的对齐是 u2-l4 的主题。

---

## 7. 下一步学习建议

- **u2-l3（`pre_func_eval`）**：本讲只说了「stencil 长什么样」，下一讲讲「每一轮迭代如何按嵌套规则**生成新的求值点** `x_eval`」——即首轮生成 `order` 个点、之后每轮只生成 2 个新点的具体实现，以及左 / 中 / 右三类元素的索引分流。
- **u2-l4（`post_func_eval`）**：本讲提到「函数值要按权重顺序拼好」，下一讲讲 `work_fc` / `work_fo` 的拼接逻辑、函数值复用、`df` 的加权计算与 `error` 估计。
- **回看源码**：建议再读一遍 [`_differentiate.py:L601-L718`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L601-L718) 的整段注释，它在源码里完整复述了本讲的 Taylor 推导，是巩固理解的最佳一手材料。
