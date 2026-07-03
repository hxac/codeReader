# 辛普森法 simpson 与 cumulative_simpson

## 1. 本讲目标

本讲继续学习「固定样本积分」。学完本讲后，你应该能够：

- 用一句话说清辛普森（Simpson）法为什么比梯形法更准——它用「抛物线」而非「直线」去贴近被积函数。
- 看懂 `simpson` 的源码：它如何用复合辛普森公式对样本求和，以及在样本数为偶数时如何用 Cartwright 公式做「末段修正」。
- 看懂 `cumulative_simpson` 的源码：它如何对每一个子区间分别用抛物线估算积分，再向前/向后两次扫描、交错拼接、最后累加得到累计积分曲线。
- 在相同样本下，定量比较梯形法与辛普森法的误差，体会「阶数更高 = 误差更小」。

本讲只依赖上一篇 [u2-l1 梯形法](u2-l1-trapezoid.md) 建立的「固定样本积分」与「复合公式」概念，不引入 ODE 等其他主题。

## 2. 前置知识

### 2.1 从「直线」到「抛物线」

上一篇我们学了梯形法：相邻两个样本点之间连一条**直线**（梯形的斜边），把所有小梯形面积加起来。

辛普森法的思路再进一步：每**三个**相邻样本点确定一条**抛物线**（二次曲线），用抛物线下方的面积作为这一段的积分估计。因为抛物线比直线更能贴合弯曲的函数图像，所以同样数量的样本点，辛普森法通常更准。

### 2.2 一条关键性质：等距 + 奇数点 ⇒ 对三次多项式精确

在**等距**样本、且样本点数为**奇数**（即子区间数为偶数）时，复合辛普森法对**不超过三次**的多项式是**精确**的——也就是误差为 0。这是一个非常强的性质，源码文档里专门写了这一点。

> 对初学者：所谓「精确」是说，哪怕你只给很少几个样本点，只要被积函数本身是三次以内的多项式，辛普森法算出来的积分就等于真实积分，一点不差。

### 2.3 与梯形法对照的术语回顾

| 概念 | 梯形法（上一篇） | 辛普森法（本讲） |
|---|---|---|
| 每段用什么曲线拟合 | 直线（2 点） | 抛物线（3 点） |
| 等距时的代数精度 | 1 次（对线性函数精确） | 3 次（对三次函数精确） |
| 公式核心 | `dx/2 * (y0 + y1)` | `dx/3 * (y0 + 4*y1 + y2)` |
| 是否需要误差估计 | 否（固定样本积分都没有） | 否 |

> 提醒：和梯形法一样，本讲的两个函数也属于「固定样本积分」——你手里只有离散采样点 `(x_i, y_i)`，不知道也无法再调用被积函数，所以**没有误差估计**返回。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 本讲关注的内容 |
|---|---|
| `scipy/integrate/_quadrature.py` | `simpson`、`cumulative_simpson` 及其内部辅助函数 |

具体涉及的函数（按调用关系自顶向下）：

- `simpson(y, x, dx, axis)`：对整段样本做一次性辛普森积分，返回单个数值。
  - `_basic_simpson(y, start, stop, x, dx, axis, xp)`：复合辛普森的「纯计算」核心，分等距/非等距两种公式。
- `cumulative_simpson(y, x, dx, axis, initial)`：返回逐点累计的积分曲线。
  - `_cumulatively_sum_simpson_integrals(...)`：累计积分的调度器，做「前向 + 反向」两次扫描再交错累加。
  - `_cumulative_simpson_equal_intervals(...)`：等距时的子区间积分公式。
  - `_cumulative_simpson_unequal_intervals(...)`：非等距时的子区间积分公式。

这些函数都带 `@xp_capabilities()` 装饰器，支持 NumPy 之外的数组后端（CuPy/JAX/Dask 等），这一点上一篇已经讲过，本讲不再展开。

## 4. 核心概念与源码讲解

### 4.1 simpson：复合辛普森与偶/奇段处理

#### 4.1.1 概念说明

`simpson` 解决的问题是：给定一组样本 `(x_i, y_i)`，用复合辛普森公式估算定积分 \(\int y(x)\,dx\)，返回**一个数值**。

它和上一篇的 `trapezoid` 是同一类工具（固定样本、一次性、无误差估计），区别只在于「用抛物线代替直线」，因此精度更高。

最核心的公式是等距三点辛普森（子区间宽度 \(h\)）：

\[
\int_{x_0}^{x_2} f(x)\,dx \;\approx\; \frac{h}{3}\bigl(f(x_0) + 4 f(x_1) + f(x_2)\bigr)
\]

把整段样本按「每三个相邻点一组、步长为 2、不重叠地覆盖」的方式切成若干段，分别套上式再相加，就得到**复合辛普森公式**。可以证明它等价于：

\[
\int_a^b f(x)\,dx \approx \frac{h}{3}\Bigl[f_0 + 4\!\!\sum_{\text{奇 }j} f_j + 2\!\!\sum_{\substack{\text{偶 }j\\ j\neq 0,\,N-1}} f_j + f_{N-1}\Bigr]
\]

这正是 `_basic_simpson` 在等距分支里用「错位切片、向量化求和」直接实现的。

#### 4.1.2 核心流程

`simpson` 的执行可以概括为：

1. **解析输入**：拿到样本数组 `y`，以及可选的采样坐标 `x` 或标量间距 `dx`；确定积分轴 `axis` 和该轴上的样本数 `N`。
2. **按 N 的奇偶分流**：
   - 若 `N` 为**奇数**（子区间数为偶数，最理想）：直接对全部样本套复合辛普森公式。
   - 若 `N` 为**偶数**（多出一个点，无法整齐分组）：
     - 退化情形 `N == 2`：点太少连一条抛物线都凑不齐，退化为梯形。
     - 一般情形 `N > 2`：对前 `N-3` 个点正常套复合辛普森，最后一段（3 个点、2 个子区间）用 Cartwright 公式做特殊修正。
3. **返回**一个标量（沿指定轴积分后的结果）。

用伪代码描述主分流：

```
N = y.shape[axis]
if N 是奇数:
    result = _basic_simpson(全部点)          # 干净的复合辛普森
else:                                       # N 是偶数，多一个点
    if N == 2:
        result = 梯形(最后两点)               # 退化
    else:
        result = _basic_simpson(前 N-3 个点) # 主体
        result += Cartwright末段修正(最后三点) # 补最后一段
return result
```

#### 4.1.3 源码精读

**(a) 复合辛普森的纯计算核心 `_basic_simpson`**

等距分支只有一行，但它是整篇讲义的灵魂：

[_quadrature.py:366-367](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L366-L367) —— 等距复合辛普森：用 `step=2` 错位切出三组点 `slice0/slice1/slice2`，按 `dx/3 * (y0 + 4*y1 + y2)` 向量化求和。

```python
if x is None:  # Even-spaced Simpson's rule.
    result = dx / 3.0 * xp.sum(y[slice0] + 4.0*y[slice1] + y[slice2], axis=axis)
```

这里的 `slice0 = slice(start, stop, 2)`、`slice1 = slice(start+1, stop+1, 2)`、`slice2 = slice(start+2, stop+2, 2)`，三组点两两错开一位、步长为 2，正好把样本不重叠地切成「每三个一组」。这一行的向量化写法和上一篇梯形法的 `y[1:]+y[:-1]` 是同一种风格。

当 `x` 给出且不等距时，公式稍复杂（每段的两个子宽度 `h0, h1` 不等），见 [_quadrature.py:371-385](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L371-L385)。其本质仍是「过三点的抛物线积分」，只是把等距公式推广到了不等距。注意里面大量出现的 `xpx.apply_where(... != 0, ..., xp.divide, fill_value=0.)` 是数组 API 的「安全除法」（除零时填 0），用于兼容 Dask 等惰性后端，可先忽略。

**(b) 主函数 `simpson` 的奇/偶分流**

[_quadrature.py:389-390](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L389-L390) —— 函数签名，`x` 与 `dx` 二选一提供采样信息。

`N` 为偶数时分流入口：

[_quadrature.py:467-468](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L467-L468) —— 判断 `N % 2 == 0` 进入「多出一个点」的特殊处理。

退化情形 `N == 2`（点太少，退化为梯形）：

[_quadrature.py:472-480](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L472-L480) —— 只有两点时，用 `0.5*dx*(y[-1]+y[-2])`，即梯形公式。

一般偶数情形：先对前 `N-3` 个点做正常复合辛普森，再补最后一段：

[_quadrature.py:483](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L483) —— 主体：`_basic_simpson(y, 0, N-3, x, dx, axis, xp=xp)`。

最后一段的 Cartwright 修正（用最后三个点 `slice1/slice2/slice3` 和最后两个子区间宽度 `h[0],h[1]` 计算三个系数 `alpha, beta, eta`）：

[_quadrature.py:509-521](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L509-L521) —— 末段修正：`result += alpha*y[-1] + beta*y[-2] - eta*y[-3]`。

> 为什么偶数样本要单独处理？因为复合辛普森要求子区间数是偶数（样本数是奇数）。当样本数是偶数时，前面的点能整齐分组，但最后会「多出一个子区间」，于是源码用一个三次精度的不等距公式（Cartwright 2017，公式 8）单独补上这一段。源码注释里也贴了 Wikipedia 的推导链接。

`N` 为奇数时最干净，一行搞定：

[_quadrature.py:525](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L525) —— `result = _basic_simpson(y, 0, N-2, x, dx, axis, xp=xp)`。

**(c) 文档里的「三次精确」性质**

[_quadrature.py:424-427](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L424-L427) —— 明确说明：等距 + 奇数样本时，对不超过三次的多项式精确；非等距时只对不超过二次的多项式精确。这是理解本函数精度的关键。

#### 4.1.4 代码实践

**实践目标**：亲眼验证「等距辛普森对三次多项式精确」，并观察偶数/奇数样本的行为差异。

**操作步骤**：

```python
# 示例代码：验证 simpson 的精度性质
import numpy as np
from scipy import integrate

# 1) 三次多项式 f(x)=x^3，等距、奇数样本点 -> 应当精确
x = np.linspace(1, 7, 7)          # 7 个点（奇数），等距
y = x**3
print("simpson (奇数点):", integrate.simpson(y, x=x))
print("解析积分      :", 7**4/4 - 1**4/4)   # ∫x^3 dx = x^4/4

# 2) 同样区间，但取偶数个点 -> 触发 Cartwright 末段修正
x2 = np.linspace(1, 7, 6)         # 6 个点（偶数）
y2 = x2**3
print("simpson (偶数点):", integrate.simpson(y2, x=x2))
```

**需要观察的现象**：
- 奇数点情形下，`simpson` 的结果应与解析积分 `x^4/4` 的差值**几乎为 0**（仅浮点误差量级），验证「三次精确」。
- 偶数点情形下，结果也非常接近，但走的是另一条代码路径（末段 Cartwright 修正）。

**预期结果**：奇数点结果与解析值差在 `1e-10` 量级以内；偶数点结果同样接近解析值（因为末段修正也保持高精度）。

> 说明：本实践未在此处实际运行，结果数值供你本地对照。若数值有较大偏差，请检查 SciPy 版本（`cumulative_simpson` 自 1.12.0 起加入）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `simpson` 在 `N == 2` 时退化为梯形，而不是报错？

**参考答案**：辛普森法每段需要 3 个点才能确定一条抛物线。只有 2 个点时无法构造抛物线，源码因此退化为「两点能做的最好的事」——梯形公式（见 [_quadrature.py:472-480](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L472-L480)）。这是一种「优雅降级」，保证函数对任何 `N >= 1` 都有合理输出。

**练习 2**：若被积函数是 `f(x)=x^4`（四次），等距奇数点下 `simpson` 还精确吗？为什么？

**参考答案**：不再精确。辛普森法的代数精度是 3（见 [_quadrature.py:424-427](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L424-L427)），对四次多项式会有一个与 \(h^4\) 同阶的小误差，需要加密样本才能逼近真实值。

---

### 4.2 cumulative_simpson：逐点累计的抛物线积分

#### 4.2.1 概念说明

`cumulative_simpson` 和上一篇的 `cumulative_trapezoid` 是一对：它返回的不是单个数值，而是一条**逐点累计的积分曲线**——在第 `i` 个样本点处，给出 \(\int_{x_0}^{x_i} y(x)\,dx\) 的估计。

它的核心思想是：对**每一个子区间** `[x_i, x_{i+1}]`，都假设被积函数在该区间附近是一条抛物线（用它与左右相邻点共三个点确定），单独估算这一小段的积分，再把所有小段「累加」起来得到累计曲线。相比 `cumulative_trapezoid` 用直线，它每段都用抛物线，因此精度更高。

> 与 `simpson` 的区别：`simpson` 一次性给出整段积分；`cumulative_simpson` 给出沿轴逐步累加的「积分函数」曲线，便于绘图和取中间值。

#### 4.2.2 核心流程

`cumulative_simpson` 不是简单地「反复调用 `simpson`」，而是用了一套更聪明的「双向抛物线」算法：

1. **输入校验与轴向归一化**：把任意 `axis` 上的积分统一变换到最后一轴处理；样本不足 3 个时退化为 `cumulative_trapezoid`。
2. **选择子区间公式**：有 `x` 用非等距公式，否则用等距公式（由 `dx` 决定）。
3. **前向扫描（h1）**：对每个三元组 `(y_i, y_{i+1}, y_{i+2})`，用抛物线算出**左半子区间** `[x_i, x_{i+1}]` 的积分。
4. **反向扫描（h2）**：把数组翻转后重做一遍，相当于用抛物线算出每个**右半子区间** `[x_{i+1}, x_{i+2}]` 的积分。
5. **交错拼接**：把前向、反向两组子区间积分按奇偶位置交错填进一个数组（边界子区间只能用唯一可用的一侧抛物线）。
6. **累加**：对拼好的子区间积分做累计求和，得到累计曲线。
7. **（可选）补 `initial`**：在曲线最前面插入初值（常为 0），使结果长度与 `y` 对齐。

伪代码：

```
若样本数 < 3:  退化为 cumulative_trapezoid
子区间公式 = 等距 ? _cumulative_simpson_equal_intervals : _..._unequal_intervals
h1 = 前向扫描(y, dx)              # 每个左半子区间积分
h2 = 反向扫描(reverse(y), reverse(dx)) 然后翻回  # 每个右半子区间积分
sub_integrals = 交错拼接(h1, h2)   # 边界强制取唯一可用侧
res = cumulative_sum(sub_integrals)
若给了 initial: res = concat(initial, res + initial)
return res
```

#### 4.2.3 源码精读

**(a) 等距子区间公式**

[_quadrature.py:558-564](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L558-L564) —— 用抛物线（过 `f1,f2,f3` 三点）算出**左半子区间** `[x1,x2]` 的积分：

```python
d = dx[..., :-1]
f1 = y[..., :-2]; f2 = y[..., 1:-1]; f3 = y[..., 2:]
return d / 3 * (5 * f1 / 4 + 2 * f2 - f3 / 4)
```

> 这条公式可以手算验证：把过 `(0,f1),(d,f2),(2d,f3)` 的抛物线（拉格朗日插值）在 `[0,d]` 上积分，结果正是 \(d/3\cdot(5f_1/4 + 2f_2 - f_3/4)\)。这就是「左半子区间」的抛物线积分，对应 Cartwright 论文的公式 (10)。

非等距版本见 [_quadrature.py:567-588](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L567-L588)，思路相同，只是把 `d` 换成两个相邻子宽度 `x21, x32`，对应论文公式 (8)，函数文档里也给出了完整数学式（见下）。

**(b) 双向扫描与交错累加的调度器**

[_quadrature.py:535-549](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L535-L549) —— `_cumulatively_sum_simpson_integrals` 是累计积分的「大脑」：

```python
sub_integrals_h1 = integration_func(y, dx)                      # 前向：左半子区间
sub_integrals_h2 = xp.flip(integration_func(xp.flip(y, -1),     # 反向：右半子区间
                                             xp.flip(dx, -1)), -1)
# 交错填入：偶位用 h1，奇位用 h2，最后一位只能用 h2
sub_integrals[..., :-1:2] = sub_integrals_h1[..., ::2]
sub_integrals[..., 1::2]  = sub_integrals_h2[..., ::2]
sub_integrals[..., -1]    = sub_integrals_h2[..., -1]
res = xp.cumulative_sum(sub_integrals, axis=-1)                 # 累加成曲线
```

> 为什么需要「双向」？最左端的子区间左边没有邻点，只能用「向右看」的抛物线（h1）；最右端的子区间右边没有邻点，只能用「向左看」的抛物线（h2）；中间的子区间则在前向、反向两组结果里交错选用。这样每个子区间都尽量用上了三点的信息，比单方向更准。

**(c) 主函数与边界退化**

[_quadrature.py:591-592](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L591-L592) —— 函数签名，`x` 与 `dx` 均为关键字参数。

文档对「抛物线假设」与数学公式 (8) 的说明：

[_quadrature.py:655-660](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L655-L660) —— 给出非等距三点抛物线子区间积分的完整公式。

样本不足 3 个时退化为 `cumulative_trapezoid`：

[_quadrature.py:726-728](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L726-L728) —— `if y.shape[-1] < 3:` 时改用梯形累计，与 `simpson` 在 `N==2` 的退化思路一致。

等距/非等距的派发：

[_quadrature.py:742-758](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L742-L758) —— 有 `x` 用 `_cumulative_simpson_unequal_intervals`，否则用 `_cumulative_simpson_equal_intervals`，二者都交给 `_cumulatively_sum_simpson_integrals` 调度。

**（d）一个重要提醒：与「反复调用 simpson」不完全相同**

[_quadrature.py:692-712](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L692-L712) —— 文档明确指出：`cumulative_simpson` 的输出「类似于」对逐步增大的上限反复调用 `simpson`，但**并不完全相等**。原因是 `cumulative_simpson` 对每个子区间都用到了更多邻点信息（双向抛物线），通常**更准**；而反复调用 `simpson` 在子区间数为奇数时会有精度波动。这是初学者最容易混淆的一点，务必记住。

#### 4.2.4 代码实践

**实践目标**：用 `cumulative_simpson` 还原一条已知的积分曲线，并直观对比「累计辛普森」与「反复调用 simpson」的差异。

**操作步骤**：

```python
# 示例代码：还原 ∫x^2 dx = x^3/3 的累计曲线
import numpy as np
from scipy import integrate

x = np.linspace(0, 4, num=20)
y = x**2
y_int = integrate.cumulative_simpson(y, x=x, initial=0)   # 累计积分曲线
analytic = x**3 / 3 - x[0]**3 / 3                          # 解析累计积分

print("最大误差:", np.max(np.abs(y_int - analytic)))

# 对比：反复调用 simpson 作为参照（文档里的参考实现）
ref = np.asarray([integrate.simpson(y[:i], x=x[:i]) for i in range(2, len(y) + 1)])
print("与反复 simpson 是否完全相同:", np.allclose(y_int[1:], ref, atol=1e-15))
```

**需要观察的现象**：
- `y_int` 与解析曲线 `x^3/3` 几乎重合（误差极小，因为 `x^2` 是二次，等距下精确）。
- `y_int[1:]` 与「反复调用 `simpson`」的 `ref` **不完全相同**（`np.allclose` 在 `atol=1e-15` 下返回 `False`），印证源码文档的提醒。

**预期结果**：最大误差在浮点量级；与反复 `simpson` 的对比在某些位置返回 `False`，正是 [_quadrature.py:706-708](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L706-L708) 文档示例展示的现象。

> 说明：上述命令未在此处实际运行，结果供本地对照。

#### 4.2.5 小练习与答案

**练习 1**：`cumulative_simpson` 在样本不足 3 个时为什么不报错而是改用梯形？

**参考答案**：抛物线需要 3 个点，样本少于 3 个时无法构造抛物线，于是退化为「两点能用的最佳工具」——梯形累计（见 [_quadrature.py:726-728](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L726-L728)），与 `simpson` 在 `N==2` 的处理一致，体现「优雅降级」。

**练习 2**：`_cumulatively_sum_simpson_integrals` 为什么要做前向和反向**两次**扫描，而不是一次？

**参考答案**：一次扫描（只看「向右」的抛物线）会让最右端的子区间无邻点可用；反向扫描补上「向左」的抛物线后，两端子区间都有可用的三点抛物线估计，中间子区间也能交错选用更优的一侧。两次扫描保证了每个子区间都尽量用上三点信息，从而比单方向更准（见 [_quadrature.py:535-548](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L535-L548)）。

**练习 3**：`cumulative_simpson(y, x=x, initial=0)` 与不传 `initial` 时，返回数组长度有何不同？

**参考答案**：不传 `initial` 时，结果沿积分轴比 `y` 少一个元素（不返回 `x[0]` 处的值）；传 `initial=0` 后，结果在最前面插入一个初值，长度与 `y` 对齐，便于直接和 `x` 一起绘图（见 [_quadrature.py:760-771](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L760-L771)）。

## 5. 综合实践

**任务**：对同一个高曲率函数 `f(x) = x·sin(x)`，在**完全相同**的样本下，分别用 `trapezoid` 与 `simpson` 积分，并与解析积分对比，定量体会辛普森法的精度优势。再用 `cumulative_simpson` 画出累计积分曲线。

`x·sin(x)` 的解析积分为：

\[
\int x\sin(x)\,dx = \sin(x) - x\cos(x) + C
\]

```python
# 示例代码：梯形 vs 辛普森 精度对比 + 累计曲线
import numpy as np
from scipy import integrate

a, b = 0.0, 10.0
N = 33                          # 奇数个点，让 simpson 走最干净的路径
x = np.linspace(a, b, N)
y = x * np.sin(x)

I_trap = integrate.trapezoid(y, x=x)
I_simp = integrate.simpson(y, x=x)
I_exact = np.sin(b) - b*np.cos(b) - (np.sin(a) - a*np.cos(a))

print(f"解析积分   : {I_exact:.10f}")
print(f"梯形法     : {I_trap:.10f}   误差 = {abs(I_trap - I_exact):.2e}")
print(f"辛普森法   : {I_simp:.10f}   误差 = {abs(I_simp - I_exact):.2e}")

# 累计积分曲线（便于绘图）
y_cum = integrate.cumulative_simpson(y, x=x, initial=0)
```

**需要观察的现象与预期结果**：
- 辛普森法的误差应当比梯形法**小好几个数量级**（典型地，梯形误差是 \(O(h^2)\)，辛普森误差是 \(O(h^4)\)，在 `N=33` 时差距非常明显）。
- 把 `N` 改成偶数（如 32）再跑一次，观察 `simpson` 走 Cartwright 末段修正路径后结果依然准确。
- 若装了 matplotlib，可绘制 `x` vs `y_cum` 与解析累计曲线 `\sin(x)-x\cos(x)`，二者应几乎重合。

> 说明：本实践未在此处实际运行，数值供本地对照。建议你亲手改 `N` 与函数，观察误差随样本密度的下降速度——这正是「代数精度」的直观体现。

## 6. 本讲小结

- 辛普森法用**抛物线**（3 点）代替梯形法的**直线**（2 点）拟合样本，代数精度从 1 提升到 3，同样本数下误差更小。
- `simpson` 的核心是等距复合公式 `dx/3·(y0+4·y1+y2)`；当样本数为偶数时，主体走复合辛普森，最后一段用 **Cartwright 修正**补齐，`N==2` 时退化为梯形。
- 等距 + 奇数样本时，`simpson` 对不超过三次的多项式**精确**；非等距时降至二次精确。
- `cumulative_simpson` 返回逐点累计的积分曲线；它对每个子区间用「双向抛物线」（前向 h1 + 反向 h2）估计，再交错拼接、累计求和。
- `cumulative_simpson` 的输出与「反复调用 `simpson`」**类似但不完全相同**，且通常更准，因为它每个子区间都用上了更多邻点信息。
- 两函数在样本不足时都会优雅退化为梯形家族，`@xp_capabilities` 使其支持多种数组后端。

## 7. 下一步学习建议

- **横向对比**：下一篇 [u2-l3 龙贝格 romb / newton_cotes / fixed_quad](u2-l3-romb-newton-cotes-fixed-quad.md) 会讲另外几种固定样本/固定阶积分方法，届时可以把本讲的辛普森法与龙贝格外推、高斯-勒让德积分放在同一张精度对照表里。
- **纵向深入**：若想理解「自适应取点」（函数能自己决定在哪里多采样），可跳到第 3 单元学习基于 QUADPACK 的 `quad`——它和本讲的固定样本积分是互补的两类工具。
- **源码延伸阅读**：直接打开 `scipy/integrate/_quadrature.py`，对照本讲给出的行号阅读 `_basic_simpson`、`simpson`、`_cumulatively_sum_simpson_integrals` 三个函数，并查阅 Cartwright 2017 论文（源码注释中有引用）理解末段修正与非等距公式的推导。
