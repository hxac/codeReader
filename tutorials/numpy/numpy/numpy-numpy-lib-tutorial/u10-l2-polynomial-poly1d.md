# 多项式与 poly1d

## 1. 本讲目标

本讲聚焦 `numpy/lib/_polynomial_impl.py` 这一个文件，讲解 numpy 中一组**旧式（legacy）多项式工具**：从求值、求根、构造，到四则运算、微积分，再到最小二乘拟合，最后用 `poly1d` 类把它们封装成「可以像数学公式一样写」的对象。学完后你应当能够：

- 看懂 `polyval` 用 **Horner 嵌套求值**把 \(n\) 次多项式从 \(n\) 次乘法降到 \(n\) 次乘加，并理解它对 `poly1d` 入参的「合成多项式」处理。
- 用 `poly`（卷积法）与 `roots`（友矩阵特征值法）在「根」与「系数」之间互转，理解二者的对偶关系。
- 掌握 `polyfit` 的最小二乘实现：**范德蒙矩阵 + `lstsq`**，并看懂 `rcond` 截断、列缩放、`RankWarning` 与协方差 `cov` 四个细节。
- 用 `polyadd/polysub/polymul/polydiv` 做多项式四则运算，理解它们共享的 `truepoly` + `atleast_1d` 对齐模式。
- 用 `polyint/polyder` 做形式积分/求导，理解「系数除以/乘以降序自然数」的递归套路。
- 用 `poly1d` 类把上述函数串起来，看懂它的属性别名（`c/r/o`）、运算符重载（`+ - * / **`）与 `__call__` 委托。

## 2. 前置知识

在阅读本讲前，建议你已经建立以下概念（前面几讲已反复出现）：

- **系数向量约定**：本讲的「多项式」一律表示成**一维系数数组**，且按**降幂**排列。即长度为 \(N\) 的数组 \(p\) 表示

  \[
  p(x)=p[0]\,x^{N-1}+p[1]\,x^{N-2}+\cdots+p[N-2]\,x+p[N-1]
  \]

  `p[0]` 是最高次系数、`p[-1]` 是常数项。这与新 API `numpy.polynomial` 按**升幂**排列正好相反，是迁移时最易踩的坑。

- **NEP-18 dispatcher + impl 双函数写法**：本讲几乎所有公开函数都装饰了 `@array_function_dispatch(_xxx_dispatcher)`，dispatcher 只负责把参与运算的数组参数收集成元组、供 `__array_function__` 协议拦截，真正的逻辑写在被装饰的函数体里（详见 u1-l2）。
- **`__array__` 协议**：任何定义了 `__array__` 方法的对象都能被 `np.asarray` 转成 ndarray，`poly1d` 正是靠它「伪装」成数组、混入 numpy 函数。
- **最小二乘与 SVD**：`polyfit` 内部调用 `numpy.linalg.lstsq`（最小二乘求解），它用奇异值分解（SVD）处理可能秩亏的方程组；`rcond` 控制「多小的奇异值被视为 0」。这些细节会在 4.4 节展开。

> 一个贯穿全讲的核心认知：**这一整套是「系数数组的代数游戏」**。除了 `roots`（友矩阵）和 `polyfit`（最小二乘）借助了线性代数，其余函数都只是在对系数数组做切片、拼接、卷积、逐元素乘除。理解了这一点，你会觉得这些函数的源码都「短得不可思议」。
>
> 另一个重要提醒：这组函数构成 numpy 的**旧 polynomial API**，每个函数的 docstring 都写着「Since version 1.4, the new polynomial API defined in `numpy.polynomial` is preferred」。它们没有被正式弃用（不会抛 `DeprecationWarning`），但官方推荐新代码用 `numpy.polynomial` 子包。本讲仍讲它们，一是为了读懂大量旧代码与教学材料，二是它们是理解「系数向量代数」的最佳范例。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲引用的关键函数 |
| --- | --- | --- |
| [numpy/lib/_polynomial_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py) | 旧式多项式 API 的全部实现：求值、根/系数互转、四则、微积分、最小二乘拟合，以及 `poly1d` 封装类 | `polyval`、`poly`、`roots`、`polyadd`、`polysub`、`polymul`、`polydiv`、`polyint`、`polyder`、`polyfit`、`poly1d` |

文件内部存在明显的依赖关系，这也决定了本讲的讲解顺序：

1. `polyval` 是**最底层原语**——`poly1d.__call__` 直接委托给它。
2. `poly`（卷积）与 `roots`（友矩阵）互为逆运算，构成「根 ↔ 系数」对偶。
3. `polyadd/polysub/polymul/polydiv` 与 `polyint/polyder` 六个函数共享同一种「`truepoly` 标志 + `atleast_1d` 对齐 + 操纵系数数组」的写法。
4. `polyfit` 站在它们之上，用范德蒙矩阵把「拟合」翻译成「最小二乘」。
5. `poly1d` 是顶层封装，把 `polyval/poly/roots/polyadd/.../polyint/polyder` 全部接到运算符上。

## 4. 核心概念与源码讲解

### 4.1 polyval：Horner 嵌套求值原语

#### 4.1.1 概念说明

给定系数 \(p=[p_0,p_1,\ldots,p_{N-1}]\)（降幂），求多项式在某点 \(x\) 的值，最朴素的办法是逐项算 \(p_0 x^{N-1}+p_1 x^{N-2}+\cdots\)。这要算很多次幂，既慢又容易累积舍入误差。

**Horner 嵌套求值**（Horner's scheme）把同一个多项式重写成嵌套形式：

\[
p(x)=\Big(\big((p_0\,x+p_1)x+p_2\big)x+\cdots+p_{N-2}\Big)x+p_{N-1}
\]

这样一来，求一个 \(N-1\) 次多项式的值只需 \(N-1\) 次「乘 + 加」，**没有独立的幂运算**，乘法次数从 \(O(n^2)\) 降到 \(O(n)\)，数值上也更稳定。`polyval` 就是 Horner 法的直接实现。

`polyval` 还有一个特殊行为：当传入的 `x` 本身是一个 `poly1d` 对象时，它返回的是两个多项式的**复合** \(p(x(t))\)（即把一个多项式代入另一个多项式），结果仍是 `poly1d`。

#### 4.1.2 核心流程

```
def polyval(p, x):
    p = asarray(p)
    if x 是 poly1d:
        y = 0                  # 标量 0 起点，靠 poly1d 自身的 __rmul__/__radd__ 做多项式代数
    else:
        x = asanyarray(x)
        y = zeros_like(x)      # 数组起点：逐元素求值
    for pv in p:
        y = y * x + pv         # Horner 一步：累乘 + 加当前系数
    return y
```

要点：

1. **循环里只有一行** `y = y * x + pv`，这就是 Horner 法的全部。
2. **起点 `y` 随 `x` 类型而变**：`x` 是普通数组 → `y=zeros_like(x)`，做逐元素数值求值；`x` 是 `poly1d` → `y=0`（Python 整数），后续 `y*x+pv` 全部走 `poly1d` 的 `__rmul__`/`__radd__`，自动完成多项式复合。
3. 因为 `x` 可以是任意形状的数组，`polyval` 天然支持「在许多点上一次性求值」（向量化）。

#### 4.1.3 源码精读

`polyval` 的 Horner 主循环（注意循环体只有一行）：

[_polynomial_impl.py:782-790](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L782-L790) —— 先按 `x` 是否 `poly1d` 选起点（`0` 或 `zeros_like(x)`），再对每个系数执行 `y = y*x + pv`，正是 Horner 嵌套求值。

`x` 为 `poly1d` 时的「复合」分支（这解释了 `np.polyval(np.poly1d([3,0,1]), np.poly1d(5))` 为何返回 `poly1d([76])`）：

[_polynomial_impl.py:783-784](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L783-L784) —— 当 `x` 是 `poly1d`，把 `y` 初始化为标量 `0`，让后续运算落到 `poly1d` 的运算符重载上，从而得到多项式复合。

#### 4.1.4 代码实践

**实践目标**：用 `polyval` 求值，并对比 Horner 法与「朴素逐项幂」法的计算量与数值差异。

**操作步骤**（示例代码）：

```python
import numpy as np

p = [3, 0, 1]                      # 3*x**2 + 1
print(np.polyval(p, 5))            # 76 == 3*25 + 1

# 在多个点上一次性求值（向量化）
x = np.array([0, 1, 2, 3])
print(np.polyval(p, x))            # [1 4 13 28]

# 对比：朴素逐项幂写法（仅作理解，不要在生产代码里用）
def naive(p, x):
    N = len(p)
    return sum(c * x ** (N - 1 - i) for i, c in enumerate(p))
print(naive(p, x))                 # 与 polyval 一致
```

**需要观察的现象与预期结果**：

1. `np.polyval(p, 5)` 得 `76`，对应 \(3\cdot5^2+0\cdot5+1=76\)。
2. 传入数组 `x` 时，返回**同形状**数组，每个元素是对应点的值——Horner 循环天然向量化。
3. 朴素写法在阶数高时会出现明显的累积误差与性能下降；Horner 法更稳更快。

#### 4.1.5 小练习与答案

**练习 1**：用 `polyval` 验证 `roots` 求出的根确实使多项式为 0。

**答案**：`p = [1, 0, -1]`（\(x^2-1\)），`r = np.roots(p)` 得 `[-1., 1.]`，再 `np.polyval(p, r)` 得近似 `[0, 0]`（仅含 \(\sim10^{-16}\) 级浮点噪声）。

**练习 2**：`np.polyval([3,0,1], np.poly1d(5))` 与 `np.polyval([3,0,1], 5)` 的返回类型有何不同？为什么？

**答案**：前者返回 `poly1d([76])`，后者返回整数 `76`。因为前者 `x` 是 `poly1d`，走 4.1.1 的「复合」分支，结果保持 `poly1d` 类型；后者走数值分支，返回标量。

---

### 4.2 poly 与 roots：根与系数的互转

#### 4.2.1 概念说明

一个最高次系数为 1（**首一**，monic）的多项式可以由它的根唯一确定：

\[
p(x)=\prod_{i=1}^{n}(x-r_i)=x^{n}-(\textstyle\sum_i r_i)x^{n-1}+\cdots+(-1)^{n}\prod_i r_i
\]

`poly` 与 `roots` 就是这条双向通道：

- `np.poly(seq_of_zeros)`：给定一组根，返回**首一多项式的系数**（降幂，首项恒为 1）。
- `np.roots(p)`：给定系数，返回所有根。

二者在算法上完全不同：

- `poly` 用**卷积**：\(\prod(x-r_i)\) 就是把每个一次因子 \([1,-r_i]\) 依次做多项式乘法，而多项式乘法 = 系数数组的离散卷积。
- `roots` 用**友矩阵特征值**：把求根问题转化为求一个特殊矩阵（companion matrix）的特征值，交给 `numpy.linalg.eigvals`。

`poly` 还有一个彩蛋：如果传入一个**方阵**，它返回该矩阵的**特征多项式**系数——因为方阵的特征值就是其特征多项式的根，于是「方阵 → 特征值 → `poly`」自然得到特征多项式。

#### 4.2.2 核心流程

**`poly` 的卷积法**：

```
def poly(seq_of_zeros):
    a = atleast_1d(seq_of_zeros)
    if a 是方阵: a = eigvals(a)              # 彩蛋：方阵 → 特征值
    elif a 是 1D: 用 mintypecode 选最小可容纳类型
    else: 报错
    result = [1]                              # 起点多项式：常数 1
    for zero in a:
        result = convolve(result, [1, -zero]) # 卷上一次因子 (x - zero)
    若根全是共轭对 → 取实部
    return result
```

数学上，每一步卷积对应一次多项式乘法：

\[
\text{result}^{(k)}(x)=\text{result}^{(k-1)}(x)\cdot(x-r_k)
\]

**`roots` 的友矩阵法**：对 \(n\) 次多项式 \(p[0]x^{n}+p[1]x^{n-1}+\cdots+p[n]\)，先**剥离前导零与尾随零**（尾随零的个数 = 0 根的个数），再构造友矩阵

\[
A=\begin{bmatrix}
-p[1]/p[0] & -p[2]/p[0] & \cdots & -p[n]/p[0]\\
1 & 0 & \cdots & 0\\
0 & 1 & \cdots & 0\\
\vdots & & \ddots & \vdots\\
0 & \cdots & 1 & 0
\end{bmatrix}
\]

其特征值恰为该多项式的根。numpy 的构造方式略有不同：先放一条次对角线全 1，再用 `-p[1:]/p[0]` 覆盖第 0 行。

```
def roots(p):
    p = atleast_1d(p); 要求 1D
    non_zero = 非零系数下标
    if 全零: return []
    trailing_zeros = 尾随零个数（= 0 根的个数）
    p = 去掉前导零与尾随零，转成浮点
    if len(p) > 1:
        A = diag(ones(N-2), -1)       # 次对角线 1
        A[0,:] = -p[1:]/p[0]          # 第 0 行放系数比
        roots = eigvals(A)            # 求特征值 = 多项式的根
    把 trailing_zeros 个 0 补到尾部
    return roots
```

#### 4.2.3 源码精读

`poly` 的卷积主循环（每一轮「乘上一次因子」）：

[_polynomial_impl.py:148-159](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L148-L159) —— `a=ones((1,))` 起步，对每个根 `zero` 执行 `convolve(a, [1,-zero])`，等价于乘以因子 \((x-\text{zero})\)；末尾若所有复根都成共轭对，就把结果取实部（`a.real.copy()`）。

`poly` 对方阵输入的「特征多项式」处理：

[_polynomial_impl.py:139-146](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L139-L146) —— 二维且为方阵时，先用 `eigvals(seq_of_zeros)` 取特征值，把「方阵」归约为「根序列」，再走同一条卷积路；非 1D/方阵则抛 `ValueError`。

`roots` 的友矩阵构造与特征值求解：

[_polynomial_impl.py:247-261](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L247-L261) —— `diag(ones((N-2,)), -1)` 画次对角线 1，`A[0,:]=-p[1:]/p[0]` 写系数比，`eigvals(A)` 得到根；再把 `trailing_zeros` 个 0 根用 `hstack` 补到尾部。

`roots` 对尾随零（0 根）的剥离与补回：

[_polynomial_impl.py:237-241](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L237-L241) —— `trailing_zeros = len(p)-non_zero[-1]-1` 数出尾部 0 的个数，`p = p[non_zero[0]:non_zero[-1]+1]` 去掉前后零，避免友矩阵退化。

#### 4.2.4 代码实践

**实践目标**：验证 `poly` 与 `roots` 互为逆运算，并观察方阵的「特征多项式」彩蛋。

**操作步骤**（示例代码）：

```python
import numpy as np

r = np.array([-0.5, 0.0, 0.5])
c = np.poly(r)                  # 由根构造系数
print(c)                        # [ 1.    0.   -0.25  0.  ]  即 x**3 - x/4

r2 = np.roots(c)                # 由系数求根
print(np.sort(r2), np.sort(r))  # 二者（排序后）一致

# 彩蛋：方阵 → 特征多项式
P = np.array([[0, 1/3], [-1/2, 0]])
print(np.poly(P))               # [1. 0. 0.16666667]，对应 t**2 - det(P)
```

**需要观察的现象与预期结果**：

1. `np.poly([-0.5,0,0.5])` 返回首项为 1 的系数数组 `[1,0,-0.25,0]`，对应 \(x^3-x/4\)。
2. `np.roots(c)` 求回的根与原根（排序后）在浮点误差内一致，体现 `poly ↔ roots` 对偶。
3. 方阵 `P` 的 `np.poly(P)` 返回特征多项式系数，其常数项恰为 `det(P)`（符号按 \(\det(tI-A)\) 约定）。
4. 若根全是共轭复数对，`poly` 返回实系数数组（共轭对消去了虚部）。

#### 4.2.5 小练习与答案

**练习 1**：`np.poly((0,0,0))` 返回什么？为什么首项是 1？

**答案**：返回 `array([1.,0.,0.,0.])`，即 \(x^3\)。因为 `poly` 永远返回**首一**多项式（首项系数固定为 1），三个 0 根对应因子 \((x-0)^3=x^3\)。

**练习 2**：为什么 `np.roots([0,0,2])` 返回两个 0 根？

**答案**：系数 `[0,0,2]` 去掉前导零后是 `[2]`（次数 0），尾随零个数 = 2，这两个尾随零就是两个 0 根，被 `hstack` 补到结果尾部。

---

### 4.3 系数四则与微积分：polyadd/polysub/polymul/polydiv/polyint/polyder

#### 4.3.1 概念说明

这一组六个函数直接在**系数数组**上做符号运算，不涉及任何线性代数。它们共享同一种写法：

- 用 `truepoly = isinstance(x, poly1d)` 记住「输入是不是 `poly1d`」，从而决定输出要不要包回 `poly1d`；
- 用 `atleast_1d` 把输入抬到至少一维；
- 对系数数组做切片/拼接/卷积/逐元素运算；
- 若 `truepoly`，把结果包成 `poly1d` 返回。

四则运算的语义就是多项式的加减乘除：

| 运算 | 数学含义 | 实现招式 |
| --- | --- | --- |
| `polyadd` | 系数逐项相加 | **高位补零对齐**后逐项加 |
| `polysub` | 系数逐项相减 | 同上，把加换成减 |
| `polymul` | 多项式乘法 | **卷积** `convolve(a1,a2)` |
| `polydiv` | 带余除法，返回 `(商, 余数)` | **长除法循环** |

微积分则是「对系数数组做一次位移」：

- `polyder(p)`（求导）：把 \(p(x)=p_0x^n+\cdots+p_n\) 求导得 \(p_0 n x^{n-1}+\cdots+p_{n-1}\)，等价于丢掉常数项、其余系数乘上**降序自然数** \([n,n-1,\ldots,1]\)。
- `polyint(p)`（积分）：积分的逆操作——系数**除以**降序自然数，并在末尾补一个积分常数。

#### 4.3.2 核心流程

**`polyadd`/`polysub` 的对齐**：

```
def polyadd(a1, a2):
    truepoly = ...是否含 poly1d...
    a1, a2 = atleast_1d(a1), atleast_1d(a2)
    diff = len(a2) - len(a1)
    if diff == 0: val = a1 + a2                # 等长直接加
    elif diff > 0: 在 a1 高位补 diff 个 0，再 + a2
    else:          在 a2 高位补 |diff| 个 0，再 a1 + ...
    return val（可能包 poly1d）
```

关键：在**高位（数组前部）**补零，因为高位对应高次项，缺失的高次项系数为 0。

**`polymul` 直接卷积**：

```
a1, a2 = poly1d(a1), poly1d(a2)
val = convolve(a1, a2)        # 多项式乘 = 系数卷积
```

**`polydiv` 的长除法**：

```
m, n = len(u)-1, len(v)-1
scale = 1./v[0]
q = zeros(m-n+1)              # 商
r = u.copy()                  # 余数，初始为被除数
for k in range(m-n+1):
    d = scale * r[k]          # 本轮商系数
    q[k] = d
    r[k:k+n+1] -= d * v       # 从余数里减掉 d*v*x**(m-n-k)
while r[0] 近似为 0 且 len(r)>1: r = r[1:]   # 去掉前导零
return q, r
```

**`polyder` 的「乘降序自然数」**：

```
n = len(p) - 1
y = p[:-1] * arange(n, 0, -1)   # 丢常数项，其余乘 n,n-1,...,1
递归 m-1 次
```

**`polyint` 的「除降序自然数 + 补常数」**：

```
y = concatenate((p / arange(len(p), 0, -1), [k[0]]))  # 除以 n,n-1,...,1 并补积分常数
递归 m-1 次，每轮消耗一个常数 k
```

#### 4.3.3 源码精读

`polyadd` 的对齐加法（注意是在**高位**补零）：

[_polynomial_impl.py:849-863](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L849-L863) —— `diff=len(a2)-len(a1)` 决定谁需要补零；`polysub` 与之结构完全一致，只把 `+a2` 换成 `-a2`（见 [_polynomial_impl.py:906-920](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L906-L920)）。

`polymul` 的卷积实现（多项式乘 = 离散卷积）：

[_polynomial_impl.py:979-984](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L979-L984) —— 直接 `NX.convolve(a1, a2)` 得到乘积系数；这正是 `poly` 里「乘一次因子」用到的同一条卷积招式。

`polydiv` 的长除法循环与去前导零：

[_polynomial_impl.py:1054-1064](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1054-L1064) —— `scale=1./v[0]`，每轮 `d=scale*r[k]`、`q[k]=d`、`r[k:k+n+1]-=d*v`，最后用 `allclose(r[0],0)` 循环剥除近似为 0 的前导系数。

`polyder` 的「乘降序自然数」核心（一行完成一阶导）：

[_polynomial_impl.py:445-446](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L445-L446) —— `n=len(p)-1; y=p[:-1]*arange(n,0,-1)`：丢掉常数项 `p[-1]`，剩余系数乘上 \(n,n-1,\ldots,1\)，正是求导法则 \( \frac{d}{dx}c_k x^k = k c_k x^{k-1}\) 的向量化。

`polyint` 的「除降序自然数 + 补积分常数」核心：

[_polynomial_impl.py:366-367](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L366-L367) —— `y=concatenate((p/arange(len(p),0,-1),[k[0]]))`：系数除以 \(n,n-1,\ldots,1\) 并在末尾补本轮积分常数 `k[0]`，然后递归 `polyint(y, m-1, k=k[1:])` 消耗下一个常数。

#### 4.3.4 代码实践

**实践目标**：用四则与微积分函数手工验证一个微积分恒等式。

**操作步骤**（示例代码）：

```python
import numpy as np

p = np.array([2.0, 0.0, -3.0, 1.0])   # 2x**3 - 3x + 1

# 求导再积分，应当还原（积分常数取 0）
dp = np.polyder(p)        # [ 6. 0. -3.]
back = np.polyint(dp)     # [ 2. 0. -3. 0.]
print(back)               # [ 2. 0. -3. 0.]  常数项变 0（积分常数默认 0）

# 多项式乘法 + 除法验证恒等式：(x+1)(x-1) = x**2 - 1
a = np.array([1.0, 1.0])
b = np.array([1.0, -1.0])
print(np.polymul(a, b))   # [ 1. 0. -1.]
q, r = np.polydiv([1, 0, -1], [1, 1])
print(q, r)               # 商 [1,-1]，余数 ~[0]
```

**需要观察的现象与预期结果**：

1. `polyder([2,0,-3,1])` 得 `[6,0,-3]`，即 \(6x^2-3\)。
2. `polyint` 再积分回来得 `[2,0,-3,0]`——前三项与原多项式一致，**常数项变成积分常数**（默认 0）。要还原原多项式，可传 `k=1`：`np.polyint(dp, k=1)`。
3. `polymul([1,1],[1,-1])` 得 `[1,0,-1]`，即 \(x^2-1\)。
4. `polydiv([1,0,-1],[1,1])` 返回商 `[1,-1]`（即 \(x-1\)）与近似 `[0]` 的余数。

> **待本地验证**：不同 numpy 版本的浮点噪声可能使 `polydiv` 的余数显示为极小值（如 `1e-16`）而非精确 0，这是源码里 `allclose(r[0],0,rtol=1e-14)` 阈值截断的体现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `polyadd` 在**高位**补零而不是低位？

**答案**：系数按**降幂**排列，高位（数组前部）对应高次项。两个次数不同的多项式相加时，次数较低者的高次项系数为 0，故在高位补零对齐后才能逐项相加。

**练习 2**：`np.polyder(p, m)` 中 `m` 很大（超过多项式次数）时会返回什么？

**答案**：返回 `poly1d([0])`（或 `[0]`），即零多项式。源码里每阶求导都会让系数数组缩短，最终归约到 `[0]`（参见 docstring 示例 `np.polyder(p, 4)` 得 `poly1d([0])`）。

**练习 3**：`polyint` 的积分常数 `k` 为何在「除降序自然数」时还要在末尾 `concatenate` 一个值？

**答案**：不定积分会引入一个任意常数项，它对应系数数组的**最低位**（末尾）。每做一次积分补一个常数；`m` 次积分需要 `m` 个常数 `k[0..m-1]`，按「最高阶项对应的常数在前」的顺序递归消耗。

---

### 4.4 polyfit：最小二乘多项式拟合

#### 4.4.1 概念说明

`polyfit(x, y, deg)` 解决的问题是：给定一组数据点 \((x_i, y_i)\)，找一个 \(d=\mathtt{deg}\) 次多项式

\[
p(x)=p_0 x^{d}+p_1 x^{d-1}+\cdots+p_d
\]

使残差平方和最小：

\[
E=\sum_{i}\big(p(x_i)-y_i\big)^2
\]

这是一个经典的**线性最小二乘**问题。把每个数据点代入多项式，得到一个超定方程组 \(Vc \approx y\)，其中 \(V\) 是**范德蒙矩阵**：

\[
V=\begin{bmatrix}
x_0^{d} & x_0^{d-1} & \cdots & x_0 & 1\\
x_1^{d} & x_1^{d-1} & \cdots & x_1 & 1\\
\vdots & & & & \vdots\\
x_{M-1}^{d} & \cdots & \cdots & x_{M-1} & 1
\end{bmatrix},\qquad c=[p_0,p_1,\ldots,p_d]^{\top}
\]

求解 \(c\) 就是 `numpy.linalg.lstsq` 的事。`polyfit` 的价值在于把「多项式拟合」包装成一行调用，并在外围处理四件事：

1. **`rcond` 截断**：范德蒙矩阵常常病态（高次列数值巨大），SVD 会把「小于最大奇异值 `rcond` 倍」的奇异值当 0 忽略，避免拟合出振荡的伪解。
2. **列缩放（scaling）**：求解前对 \(V\) 的每一列除以其 2-范数，改善条件数；解出后再把缩放「除回去」。
3. **`RankWarning`**：当有效秩不足 `deg+1`（矩阵病态）时，发出 `RankWarning` 提示拟合可能不可靠。
4. **协方差 `cov`**：可选返回系数估计的协方差矩阵，对角线即各系数的方差。

#### 4.4.2 核心流程

```
def polyfit(x, y, deg, rcond=None, full=False, w=None, cov=False):
    order = deg + 1
    x = asarray(x)+0.0; y = asarray(y)+0.0           # 强制浮点
    # 参数校验：deg>=0、x 1D、x 非空、y 1D/2D、x 与 y 同长

    if rcond is None:
        rcond = len(x) * finfo(x.dtype).eps           # 默认截断阈值 ≈ M·2e-16

    lhs = vander(x, order)                            # 范德蒙矩阵
    rhs = y
    if w is not None:                                 # 加权：左右各乘 w
        lhs *= w[:, None]; rhs *= w（或 w[:,None]）

    scale = sqrt((lhs*lhs).sum(axis=0))               # 每列 2-范数
    lhs /= scale                                      # 列缩放
    c, resids, rank, s = lstsq(lhs, rhs, rcond)       # 最小二乘求解
    c = (c.T / scale).T                               # 把缩放除回系数

    if rank != order and not full:
        warn(RankWarning)                             # 病态提示

    if full: return c, resids, rank, s, rcond
    elif cov:
        Vbase = inv(dot(lhs.T, lhs))                  # 未缩放协方差
        Vbase /= outer(scale, scale)
        fac = resids / (len(x) - order)               # 按 chi2/dof 缩放（unscaled 时 fac=1）
        return c, Vbase * fac
    else: return c
```

几个关键细节：

- **默认 `rcond`**：`len(x) * finfo(x.dtype).eps`。对双精度浮点，\(\varepsilon\approx 2.2\times10^{-16}\)，所以阈值随数据点数线性增大，点数越多、容忍的相对噪声越大。
- **列缩放的意义**：范德蒙矩阵高次列（\(x^d\)）数值远大于低次列（\(x^0=1\)），直接求解条件数极差。先按列归一化、求解、再把缩放除回去，能显著改善数值稳定性。
- **`RankWarning` 触发条件**：`rank != order and not full`。即「有效秩不足」且用户没有要 `full` 诊断信息时才告警——因为要了 `full` 的人会自己看 `rank` 字段。
- **协方差缩放因子 `fac`**：默认 `fac=resids/(M-order)`（即残差平方和除以自由度 \(M-(d+1)\)），相当于「权重只有相对意义、按约化 \(\chi^2=1\) 归一」；`cov='unscaled'` 时 `fac=1`，适用于 `w=1/sigma` 且 `sigma` 已知可靠的情形。源码注释特别提到，旧的 `-2`（「贝叶斯不确定性分析」）已被去掉（见 gh-11196/11197）。

#### 4.4.3 源码精读

`rcond` 默认值的设定（点数 × 机器 ε）：

[_polynomial_impl.py:654-655](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L654-L655) —— `rcond = len(x) * finfo(x.dtype).eps`，使截断阈值随采样点数自适应。

范德蒙矩阵构造与加权：

[_polynomial_impl.py:657-672](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L657-L672) —— `lhs=vander(x, order)` 生成范德蒙矩阵；若给定权重 `w`，则 `lhs*=w[:,None]`、`rhs*=w`（对 2D `y` 用 `rhs*=w[:,None]`），即对每行的残差加权。

列缩放 + 最小二乘求解 + 缩放回代：

[_polynomial_impl.py:675-678](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L675-L678) —— `scale=sqrt((lhs*lhs).sum(axis=0))` 算每列 2-范数，`lhs/=scale` 列归一化，`lstsq(lhs,rhs,rcond)` 求解，最后 `c=(c.T/scale).T` 把列缩放除回系数。

`RankWarning` 触发条件：

[_polynomial_impl.py:681-683](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L681-L683) —— 仅当 `rank != order`（有效秩不足，说明病态）且 `not full`（用户没要诊断信息）时发出 `RankWarning`。

协方差 `cov` 分支（含 `unscaled` 与按自由度缩放两条路）：

[_polynomial_impl.py:687-704](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L687-L704) —— `Vbase=inv(dot(lhs.T,lhs))` 取未缩放协方差，再 `/=outer(scale,scale)` 还原列缩放；`cov=='unscaled'` 时 `fac=1`，否则 `fac=resids/(len(x)-order)` 按 \(\chi^2/\text{dof}\) 缩放（旧式 `-2` 已废弃）。

#### 4.4.4 代码实践

**实践目标**：用 `polyfit` 拟合带噪二次数据，对比「合适阶数」与「过高阶数」的差异，并触发 `RankWarning`。

**操作步骤**（示例代码）：

```python
import numpy as np
import warnings

# 真相：y = 2x**2 - 3x + 1，加噪声
rng = np.random.default_rng(0)
x = np.linspace(0, 5, 20)
y = 2*x**2 - 3*x + 1 + rng.normal(0, 1.0, size=x.size)

# 用 2 次拟合（阶数与真相同），应接近 [2, -3, 1]
c2 = np.polyfit(x, y, 2)
print(c2)                          # 约为 [ 2.0x  -3.0x  1.0x ]

# 用过高的阶数（数据点 20 个，阶数 15），触发 RankWarning
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter('always')
    c15 = np.polyfit(x, y, 15)
    print("告警数:", len(w),
          "是否 RankWarning:",
          any(issubclass(wi.category, np.exceptions.RankWarning) for wi in w))

# 用 full=True 看诊断信息（rank、奇异值），此时不再告警
c, resids, rank, s, rc = np.polyfit(x, y, 15, full=True)
print("有效秩 rank:", rank, " 阶数+1:", 16)

# 用 cov=True 取协方差，对角线是各系数方差
c2fit, V = np.polyfit(x, y, 2, cov=True)
print("各系数标准差:", np.sqrt(np.diag(V)))
```

**需要观察的现象与预期结果**：

1. 2 次拟合得到的 `c2` 应接近 `[2, -3, 1]`（在噪声幅度内）。
2. 用 15 次拟合时，因范德蒙矩阵严重病态，会触发 `RankWarning`（「Polyfit may be poorly conditioned」）。
3. 用 `full=True` 时**不再告警**，而是从返回的 `rank` 看到有效秩小于 16，可自行诊断。
4. `cov=True` 返回的 `V` 是 \(3\times3\) 协方差矩阵，其对角线开根号给出各系数的标准差估计。

> **待本地验证**：`c2` 的具体数值与随机种子有关；`rank` 的精确值取决于 numpy/BLAS 版本，但应小于 `deg+1=16`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `polyfit` 在调用 `lstsq` 前要先做列缩放？

**答案**：范德蒙矩阵的高次列（如 \(x^{15}\)）数值远大于低次列（常数列 1），条件数极大。先按每列 2-范数归一化再求解，可显著改善条件数；解出后再把缩放除回系数（`c=(c.T/scale).T`），数学上等价但数值上更稳。

**练习 2**：默认 `rcond` 是 `len(x)*eps`。如果手动把 `rcond` 调得**更小**（比默认还小），拟合结果会怎样？

**答案**：更小的 `rcond` 意味着保留更小的奇异值，等效「接受更多病态方向」。拟合在数据点上会更贴近，但会引入剧烈振荡（Runge 现象），把数值噪声当成信号放大。docstring 明确警告「the resulting fit may be spurious」。

**练习 3**：`cov='unscaled'` 与默认 `cov=True` 的区别是什么？何时用前者？

**答案**：默认 `cov=True` 用 `fac=resids/(M-order)` 把协方差按约化 \(\chi^2\) 缩放，适合「权重只有相对意义」的场景；`cov='unscaled'` 用 `fac=1`，适合权重 `w=1/sigma` 且 `sigma` 是可靠绝对估计的场景——此时无需额外归一化。

---

### 4.5 poly1d 类：封装与运算符重载

#### 4.5.1 概念说明

前面四节的函数都接受「裸系数数组」。`poly1d` 是一个**便利封装类**：它把系数数组包成一个对象，让你用「像数学公式一样」的语法操作多项式：

```python
p = np.poly1d([1, 2, 3])     # 表示 x**2 + 2x + 3
p(0.5)                       # 直接调用求值 → 4.25
p + p                        # 多项式相加
p * p                        # 多项式相乘
(p**3 + 4) / p               # 带余除法，返回 (商, 余数)
```

它的设计要点有三：

1. **属性别名**：`p.c`/`p.coeffs`/`p.coef`/`p.coefficients` 都指系数；`p.r`/`p.roots` 指根；`p.o`/`p.order` 指次数；还有 `p.variable` 指打印用的变量名。这些别名让不同习惯的人都能顺手。
2. **运算符重载**：`__add__`/`__sub__`/`__mul__`/`__truediv__`/`__pow__` 等双下方法把 `+ - * / **` 翻译成对应的 `polyadd/polysub/polymul/polydiv` 调用，且保持返回类型仍是 `poly1d`。
3. **`__call__` 委托 `polyval`**：`p(x)` 直接调 `polyval(self.coeffs, x)`，于是 4.1 节的 Horner 求值（含 `poly1d` 复合）自动可用。

此外，`poly1d` 实现了 `__array__`，因此能被 `np.asarray` 转成系数数组，混入任意接受数组的 numpy 函数——但这意味着 `np.square(p)` 是「对每个系数平方」而非「多项式平方」，要小心区分（docstring 里有专门示例）。

#### 4.5.2 核心流程

**构造 `poly1d`**：

```
def __init__(self, c_or_r, r=False, variable=None):
    if c_or_r 是 poly1d: 复制其系数与变量名（带 FutureWarning 兜底扩展属性）
    if r: c_or_r = poly(c_or_r)            # r=True 表示传入的是根，先转系数
    c_or_r = atleast_1d(c_or_r)
    要求 1D
    c_or_r = trim_zeros(c_or_r, 'f')       # 去掉前导零（降幂高位）
    if 全空: c_or_r = [0]                    # 零多项式兜底
    self._coeffs = c_or_r
    self._variable = variable or 'x'
```

**求值 `__call__`**：

```
def __call__(self, val):
    return polyval(self.coeffs, val)       # 委托 4.1 节的 Horner
```

**运算符（以乘法为例）**：

```
def __mul__(self, other):
    if isscalar(other):
        return poly1d(self.coeffs * other)        # 标量：逐系数乘
    else:
        return poly1d(polymul(self.coeffs, poly1d(other).coeffs))  # 多项式乘
```

**`__getitem__`（按下标取某次幂的系数）**：

```
def __getitem__(self, val):
    ind = self.order - val                  # 把「幂次」翻译成「数组下标」
    if val 越界 or val<0: return 0
    return self.coeffs[ind]
```

#### 4.5.3 源码精读

`poly1d` 的类声明与 `__hash__ = None`（不可哈希）：

[_polynomial_impl.py:1095-1206](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1095-L1206) —— 用 `@set_module('numpy')` 把类的 `__module__` 钉为 `numpy`（即便实现在 `lib._polynomial_impl`）；`__hash__ = None` 显式声明不可哈希（因为它是可变的，`__setitem__` 可改系数）。

属性别名表（`r/c/o` 都是 `roots/coeffs/order` 的短名）：

[_polynomial_impl.py:1246-1248](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1246-L1248) —— `r = roots`、`c = coef = coefficients = coeffs`、`o = order`，让 `p.r`、`p.c`、`p.o` 等短写都可用。

`coeffs`/`order`/`roots` 计算属性（注意 `order` 由系数长度推、`roots` 委托 `roots()`）：

[_polynomial_impl.py:1208-1233](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1208-L1233) —— `coeffs` 是普通 property，`order=len(self._coeffs)-1`，`roots` 直接 `return roots(self._coeffs)`（4.2 节的 `roots`）。

`__init__` 的构造逻辑（含 `r=True` 走根、`trim_zeros` 去前导零、零多项式兜底）：

[_polynomial_impl.py:1250-1275](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1250-L1275) —— `r=True` 时调 `poly(c_or_r)` 把根转成系数；`trim_zeros(..., trim='f')` 去掉前导（高位）零；去零后若为空则置 `[0]`。

`__call__` 委托 `polyval`（一行）：

[_polynomial_impl.py:1344-1345](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1344-L1345) —— `return polyval(self.coeffs, val)`，让 `p(x)` 复用 4.1 节的 Horner 求值（含 `poly1d` 复合语义）。

运算符重载（`__mul__`/`__add__`/`__sub__`/`__truediv__`/`__pow__` 等）：

[_polynomial_impl.py:1353-1403](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1353-L1403) —— 每个运算符都先判断 `other` 是否标量（标量走逐系数运算），否则委托对应的 `polymul/polyadd/polysub/polydiv`；`__pow__` 用 `polymul` 循环自乘 `val` 次（仅允许非负整数幂）。

`__getitem__`（把「幂次」翻译成「数组下标」）：

[_polynomial_impl.py:1417-1423](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1417-L1423) —— `ind=self.order-val`：因为系数降幂排列，`p[k]`（\(x^k\) 的系数）对应数组下标 `order-k`；越界或负幂返回 0。

`integ`/`deriv` 方法（委托 `polyint/polyder` 后包回 `poly1d`）：

[_polynomial_impl.py:1438-1462](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1438-L1462) —— `integ` 调 `poly1d(polyint(self.coeffs, m=m, k=k))`、`deriv` 调 `poly1d(polyder(self.coeffs, m=m))`，即把 4.3 节的微积分函数接到方法上。

`__str__` 与 `_raise_power`（把系数渲染成带指数的漂亮字符串）：

[_polynomial_impl.py:1291-1342](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1291-L1342) 配合 [_polynomial_impl.py:1067-1092](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1067-L1092) —— `__str__` 逐项格式化系数与幂次，`_raise_power` 用正则把 `x**3` 这种文本排成「指数在上一行、底数在下一行」的 ASCII 艺术（与 MATLAB 风格一致）。

#### 4.5.4 代码实践

**实践目标**：用 `poly1d` 体验「数学公式式」语法，并区分「多项式运算」与「系数逐元素运算」。

**操作步骤**（示例代码）：

```python
import numpy as np

p = np.poly1d([1, 2, 3])          # x**2 + 2x + 3
print(p)                          # 漂亮的多行字符串
print(p(0.5))                     # 4.25（__call__ → polyval）

# 运算符重载：结果仍是 poly1d
print(p * p)                      # poly1d([1,4,10,12,9])  多项式平方
print((p**2 + 1) / p)             # (商, 余数) 元组

# 属性别名
print(p.c, p.r, p.o, p.variable)  # 系数、根、次数、变量名

# 危险区分：多项式平方 vs 系数逐元素平方
print(p**2)                       # poly1d([1,4,10,12,9])   多项式乘法
print(np.square(p))               # array([1,4,9])          系数逐元素平方（截然不同！）
```

**需要观察的现象与预期结果**：

1. `print(p)` 输出形如 `1 x + 2 x + 3`（带 `2` 在上一行表示指数）的 ASCII 艺术字符串。
2. `p(0.5)` 得 `4.25`，与 `np.polyval([1,2,3], 0.5)` 完全一致。
3. `p*p` 与 `p**2` 都得 `poly1d([1,4,10,12,9])`——多项式乘法。
4. `np.square(p)` 得 `array([1,4,9])`——因为 `p` 经 `__array__` 变成系数数组 `[1,2,3]`，`np.square` 对其逐元素平方。**这是新手最易混淆的点**。
5. `p.r` 给出共轭复根 `[-1±1.41j]`，`p.o` 为 `2`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `poly1d` 要设 `__hash__ = None`？

**答案**：`poly1d` 是可变对象——`__setitem__`（如 `p[0]=5`）可以改写系数。可变对象若可哈希会破坏「哈希值在生命周期内不变」的契约，故显式设 `__hash__ = None` 禁止哈希。

**练习 2**：`p[1]` 返回什么？源码里是怎么算出这个值的？

**答案**：返回 \(x^1\) 项的系数。源码 `__getitem__` 里 `ind=self.order-val`，对 `p[1]` 即 `ind=2-1=1`，返回 `self.coeffs[1]`。对 `p=[1,2,3]`，`p[1]` 得 `2`。

**练习 3**：`np.poly1d([1,2], True)` 与 `np.poly1d([1,2])` 有何不同？

**答案**：前者 `r=True`，把 `[1,2]` 当作**根**，先经 `poly([1,2])` 转成系数 `[1,-3,2]`，表示 \((x-1)(x-2)=x^2-3x+2\)；后者把 `[1,2]` 直接当系数，表示 \(x+2\)。

---

## 5. 综合实践

把本讲的 `polyfit` + `polyval` + `poly1d` 串起来，完成一个完整的「数据拟合—求值—诊断」流程，这是本讲的实践任务。

**任务背景**：你测量了某个物理过程，得到带噪声的离散数据点，怀疑它是一个 3 次多项式信号。你需要用 `polyfit` 做三次拟合、用 `polyval` 生成拟合曲线、再用 `poly1d` 与微积分工具分析拟合结果。

**操作步骤**（示例代码）：

```python
import numpy as np
import warnings

# 1. 造数据：真相 y = 0.5*x**3 - 2*x**2 + 3 （加噪声）
rng = np.random.default_rng(42)
x = np.linspace(-1, 4, 25)
y_true = 0.5*x**3 - 2*x**2 + 3
y = y_true + rng.normal(0, 0.3, size=x.size)

# 2. polyfit 做 3 次最小二乘拟合，取系数
coeffs = np.polyfit(x, y, 3)
print("拟合系数（降幂）:", coeffs)        # 应接近 [0.5, -2, 0, 3]

# 3. 用 polyval 在密集网格上生成拟合曲线点
x_dense = np.linspace(-1, 4, 200)
y_fit = np.polyval(coeffs, x_dense)

# 4. 用 poly1d 封装，享受运算符语法
p = np.poly1d(coeffs)
print("多项式对象:\n", p)
print("p(2.0) = ", p(2.0))               # 与 polyval(coeffs, 2.0) 一致

# 5. 复用 4.3 节微积分：求拟合曲线的导数，找极值点
dp = p.deriv()                            # poly1d 对象
print("导数多项式:\n", dp)
crit = dp.r                               # 临界点 = 导数的根
print("临界点:", crit)

# 6. 诊断：用 full=True 看 rank，确认是否病态
c, resids, rank, s, rc = np.polyfit(x, y, 3, full=True)
print(f"有效秩 {rank} / 阶数+1 {4}, 残差平方和 {resids}")

# 7. 诊断：用 cov=True 估计系数不确定度
c2, V = np.polyfit(x, y, 3, cov=True)
print("各系数标准差:", np.sqrt(np.diag(V)))
```

**需要观察的现象与预期结果**：

1. 拟合系数应接近真相 `[0.5, -2, 0, 3]`（在噪声幅度内）。
2. `polyval(coeffs, x_dense)` 与 `p(x_dense)` 结果完全一致——前者是 4.1 节 Horner，后者经 `poly1d.__call__` 委托到同一个 `polyval`。
3. `p.deriv()` 返回一个 `poly1d`，其 `.r` 给出导数的根，即原拟合曲线的临界点。
4. `full=True` 时不再触发 `RankWarning`，且 `rank` 应等于 4（点数 25 充足，3 次拟合通常满秩）；若把阶数提到 20+，会看到 `rank` 小于 `阶数+1`。
5. `cov=True` 的对角线开根号给出各系数的标准差，反映拟合不确定度。

> **待本地验证**：临界点的精确位置、系数标准差的具体数值取决于随机种子与 numpy 版本。可尝试把噪声标准差从 0.3 调到 3.0，观察系数标准差如何随之放大。

**进阶挑战**：

- 把 `deg` 改成 10，观察 `RankWarning` 是否触发、拟合曲线是否在数据点之间剧烈振荡（Runge 现象）。
- 用 `np.polyfit(x, y, 3, cov='unscaled')` 对比默认 `cov=True` 的协方差差异，体会 4.4 节「按 \(\chi^2/\text{dof}\) 缩放」的效果。
- 用 `np.roots(coeffs)` 求拟合多项式的根，再用 `np.polyval(coeffs, roots)` 验证这些根处函数值近似为 0（呼应 4.2 节的对偶）。

## 6. 本讲小结

- **全讲主线是「系数数组的代数游戏」**：除 `roots`（友矩阵）与 `polyfit`（最小二乘）借助线性代数外，其余函数都只是对系数数组做切片/拼接/卷积/逐元素乘除，源码都极短。
- **系数按降幂排列**是本讲贯穿始终的约定，`p[0]` 是最高次、`p[-1]` 是常数项；这与新 API `numpy.polynomial`（升幂）相反，是迁移最大坑点。
- **`polyval` 用 Horner 嵌套求值**（`y=y*x+pv` 循环），把 \(O(n^2)\) 的逐项幂降到 \(O(n)\) 乘加；当 `x` 是 `poly1d` 时返回多项式复合，这是 `poly1d.__call__` 委托的底层原语。
- **`poly` 与 `roots` 互为对偶**：前者用卷积把每个一次因子 \([1,-r_i]\) 连乘（方阵输入走特征多项式彩蛋），后者构造友矩阵求特征值；尾随零 = 0 根的个数。
- **`polyfit` = 范德蒙矩阵 + `lstsq`**，外围四件事：默认 `rcond=len(x)*eps` 截断小奇异值、按列 2-范数缩放改善条件数、`rank!=order` 时发 `RankWarning`、可选返回按 \(\chi^2/\text{dof}\) 缩放的协方差（`cov='unscaled'` 时不缩放）。
- **`poly1d` 是封装层**：属性别名（`c/r/o`）+ 运算符重载（`+ - * / **` 委托到 `polyadd/.../polydiv`）+ `__call__` 委托 `polyval` + `__array__` 伪装成数组；注意 `np.square(p)` 是系数逐元素平方，与 `p**2`（多项式平方）截然不同。

## 7. 下一步学习建议

- **迁移到新 polynomial API**：本讲反复提到「旧 API 自 1.4 起被 `numpy.polynomial` 取代」。建议接着阅读 `numpy/polynomial/polynomial.py` 的 `Polynomial` 类，对比它与 `poly1d` 的三大差异：系数**升幂**排列、用 `domain/window` 做区间映射改善条件数（正是 4.4 节列缩放思想的形式化）、`fit` 类方法内置稳定拟合。这是读完本讲最自然的下一步。
- **承接结构化数组与记录函数**：u14 单元的 `recfunctions` 会处理带命名字段的 dtype，与本讲「系数数组」的纯数值 1D 数组形成对照；理解本讲对「系数向量」的约定，有助于在 u14 区分「字段偏移」与「数组下标」两套寻址。
- **深入线性代数内核**：本讲的 `roots` 调 `eigvals`、`polyfit` 调 `lstsq`/`inv`，都来自 `numpy.linalg`。想看懂友矩阵特征值的底层，可阅读 `numpy/linalg/_linalg.py` 中 `eigvals` 与 `_to_real_if_close`（roots 用它把「虚部为 0 的特征值」还原为实数）的实现。
- **源码阅读建议**：把 `poly1d.__call__`（4.5）→ `polyval`（4.1）→ `polyder`/`polyint`（4.3）这条委托链连起来读，再对照 `poly` 卷积循环（4.2）与 `polydiv` 长除法循环（4.3），体会「同一套系数数组代数」如何被函数复用与类封装两次表达。
