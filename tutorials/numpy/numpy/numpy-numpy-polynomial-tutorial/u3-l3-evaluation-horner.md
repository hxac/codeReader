# 多项式求值与 Horner 法

## 1. 本讲目标

本讲聚焦 `numpy.polynomial.polynomial` 中**求值（evaluation）**这一最核心、最高频的操作。读完后你应当能够：

1. 看懂 `polyval` 用 **Horner（秦九韶）法** 把求值复杂度从 \(O(n^2)\) 降到 \(O(n)\) 的实现；
2. 说清 `tensor=True/False` 在多维系数下如何改变输出形状，并能据此预测形状；
3. 理解 `polyvalfromroots` 为何用「连乘」而非「先转系数再求值」，以及它的数值意义；
4. 掌握 `polyval2d/3d/nd` 与 `polygrid2d/3d` 如何复用 `polyutils._valnd` / `_gridnd`，把 N 维求值拆成一连串 1 维 `polyval` 调用。

本讲承接 [u3-l2](u3-l2-power-series-creation-arithmetic.md)：你已经知道「系数从低次到高次」的约定与 `polyadd`/`polymul` 等算术 API。本讲要回答的是：**给定一组系数与一组点 x，如何把多项式「算出来」。**

## 2. 前置知识

- **多项式的系数约定**：`c[i]` 是第 `i` 次项系数，\(p(x)=c_0 + c_1 x + \dots + c_n x^n\)。
- **NumPy 广播（broadcasting）**：形状从右往左对齐、缺位补 1、相同或其中之一为 1 才能对齐。本讲里「形状魔法」几乎全靠它。
- **便捷类与函数式 API 的委托关系**：`Polynomial.__call__` 内部调用虚函数 `_val`，而 `Polynomial._val = staticmethod(polyval)`。即「便捷类求值」最终落到本讲的 `polyval`。
- **domain / window 线性映射**：便捷类 `p(x)` 会先把 x 从 `domain` 映射到 `window` 再求值（详见 [u2-l2](u2-l2-domain-window-mapping.md)）。本讲的函数式 `polyval` **不做**这个映射——它只管「给我系数和点，算值」。

一个贯穿全讲的直觉：**求值 = 沿系数某一根轴做一次累加循环**。Horner 法、多维求值、grid 求值，本质都是这个累加循环在不同轴上的反复应用。

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
|------|---------|------|
| `polynomial.py` | `polyval` | 1 维系数在点 x 上用 Horner 法求值，是全包求值的地基 |
| `polynomial.py` | `polyvalfromroots` | 用根直接表示的多项式求值，靠 `np.prod` 连乘 |
| `polynomial.py` | `polyval2d` / `polyval3d` / `polyvalnd` | 在「配对」点集上求 N 维多项式的值 |
| `polynomial.py` | `polygrid2d` / `polygrid3d` | 在「笛卡尔积网格」上求 N 维多项式的值 |
| `polynomial.py` | `polyvander` | 构造范德蒙矩阵，与 `polyval` 存在等价关系 |
| `polyutils.py` | `_valnd` / `_gridnd` | 把 N 维求值拆成一串 1 维 `polyval` 的共享引擎 |
| `_polybase.py` | `ABCPolyBase.__call__` | 便捷类的求值入口，先映射 domain→window 再委托 `_val` |

## 4. 核心概念与源码讲解

### 4.1 Horner 法 polyval

#### 4.1.1 概念说明

给定 \(p(x)=c_0 + c_1 x + c_2 x^2 + \dots + c_n x^n\)，最朴素的求值方式是先算 \(x^0, x^1, \dots, x^n\)，再逐项乘系数求和。这样需要 \(O(n^2)\) 次乘法（算幂就要 \(O(n^2)\)）。

**Horner 法**（中国称秦九韶算法）的关键是改写嵌套形式：

\[
p(x) = \bigl(\dots\bigl((c_n\,x + c_{n-1})\,x + c_{n-2}\bigr)\,x + \dots + c_1\bigr)\,x + c_0
\]

从最高次系数 \(c_n\) 开始，每步做一次「乘 x、加低一次的系数」，共 n 步，**只要 \(O(n)\) 次乘法与 \(O(n)\) 次加法**。它不仅更快，而且因为避免了单独计算高次幂，**数值上通常也更稳定**（不产生极大的中间 \(x^n\)）。

#### 4.1.2 核心流程

`polyval` 的求值循环（伪代码）：

```
c0 = c[n]              # 从最高次系数起步
for k = n-1 down to 0:
    c0 = c[k] + c0 * x # 乘 x、加下一项系数
return c0
```

注意起点是 **最高次系数**，循环往**低次**走——这正好对应嵌套形式「从最内层 \(c_n\) 往外剥」。

用 \(c=[1,2,3]\)（即 \(1+2x+3x^2\)）、\(x=1\) 验算：

1. `c0 = 3`
2. `c0 = 2 + 3·1 = 5`
3. `c0 = 1 + 5·1 = 6`

返回 `6.0`，与函数文档示例一致。

#### 4.1.3 源码精读

完整的 `polyval` 定义见 [polynomial.py:663-759](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L663-L759)，它承担「输入规整 + tensor 处理 + Horner 循环」三件事。

输入规整与 tensor 预处理（[polynomial.py:747-754](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L747-L754)）：

```python
c = np.array(c, ndmin=1, copy=None)              # 保证 c 至少 1 维
if c.dtype.char in '?bBhHiIlLqQpP':              # 整数/布尔系数
    c = c + 0.0                                  # 提升为浮点，避免整数除法等坑
if isinstance(x, (tuple, list)):
    x = np.asarray(x)                            # 只对 list/tuple 转数组，标量保持原样
if isinstance(x, np.ndarray) and tensor:
    c = c.reshape(c.shape + (1,) * x.ndim)       # 关键：给系数补尾随 1 轴
```

要点：
- `ndmin=1` 让标量系数也变成 1 维，循环才好写。
- 整数系数用 `+ 0.0` 提升为浮点——否则后续 `c0 * x` 可能在整数下溢出或截断。
- **标量 x 不转数组**：这是 `polyval` 能作用于任意「可乘可加」对象（如 `Decimal`、符号对象）的前提。

Horner 循环本体（[polynomial.py:756-759](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L756-L759)）：

```python
c0 = c[-1] + x * 0          # 起点：最高次系数；x*0 把它「铺」成 x 的形状/dtype
for i in range(2, len(c) + 1):
    c0 = c[-i] + c0 * x     # 剥下一层：乘 x、加更低一次系数
return c0
```

这里有两个精妙之处：

1. **`c[-1] + x * 0`**：当 x 是数组时，`x * 0` 携带了 x 的形状与 dtype，加上标量 `c[-1]` 后 `c0` 立刻获得正确形状，后续每次 `c0 * x` 与 `c[-i]` 都能顺利广播。`x * 0` 不是浪费，而是**借一次乘法完成形状/dtype 对齐**。
2. **下标 `-i`**：`c[-1]` 是最高次，`c[-2]`、`c[-3]` 依次往低次走，正好契合 Horner「从高次起步」的方向。

`Polynomial` 等便捷类的求值最终就委托到这里。便捷类 `__call__`（[_polybase.py:510-512](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L510-L512)）只多做了一步 domain→window 映射：

```python
def __call__(self, arg):
    arg = pu.mapdomain(arg, self.domain, self.window)
    return self._val(arg, self.coef)        # _val 即 polyval
```

所以「函数式 `polyval`」是裸求值器，「便捷类 `p(x)`」是带坐标映射的求值器。

#### 4.1.4 代码实践

**实践目标**：亲手验证 Horner 循环的正确性，并与「朴素逐项求和」对比结果一致、体会速度差异。

操作步骤（示例代码，需自行运行）：

```python
import numpy as np
from numpy.polynomial.polynomial import polyval

c = np.array([1, 2, 3, 4, 5])      # p = 1 + 2x + 3x^2 + 4x^3 + 5x^4
x = np.linspace(-1, 1, 1000)

# (a) 用 polyval（Horner）
y_horner = polyval(x, c)

# (b) 朴素逐项求和作为对照
y_naive = np.zeros_like(x)
for i, ci in enumerate(c):
    y_naive += ci * x**i

print(np.allclose(y_horner, y_naive))   # 预期 True（舍入误差内相等）
```

需要观察的现象：

1. `allclose` 返回 `True`，说明 Horner 与朴素法结果一致；
2. 把 `c` 的长度加到几千、`x` 的点数加大，分别计时，Horner 通常明显更快；
3. 把系数改成整数数组（如 `c = np.array([1,2,3])`），在循环里打印 `polyval(2, c)` 的类型，确认输出是浮点（因为 `+ 0.0` 提升）。

预期结果：两者在数值上一致；Horner 更快；整数系数被提升为浮点输出。若计时无明显差异，说明多项式阶数太低，可加大规模。

#### 4.1.5 小练习与答案

**练习 1**：`polyval(2, [0, 0, 0, 1])` 的值是多少？用 Horner 循环手算。

**参考答案**：`c=[0,0,0,1]` 即 \(p(x)=x^3\)。`c0=1` → `c0=0+1·2=2` → `c0=0+2·2=4` → `c0=0+4·2=8`。结果 `8.0`。

**练习 2**：为什么 `polyval` 用 `c[-1] + x*0` 而不是直接 `c0 = c[-1]`？

**参考答案**：当 x 是数组时，`x*0` 携带 x 的形状与 dtype，让 `c0` 从第一步就获得正确形状，后续 `c0*x`（数组乘数组）和 `c[-i]`（标量）才能按广播规则对齐；直接赋标量 `c0` 在遇到数组 x 时会丢失形状信息。

**练习 3**：若把 `for i in range(2, len(c)+1)` 误写成 `range(1, len(c))`，会发生什么？

**参考答案**：会少算两端。`range(2, len(c)+1)` 取 `i=2..len(c)`，对应下标 `-2..-len(c)`，覆盖除最高次（`-1`，作为起点）外的全部系数；改成 `range(1, len(c))` 后起止都错位，常数项 `c[0]`（下标 `-len(c)`）会被漏掉，结果系统性出错。

---

### 4.2 tensor 广播

#### 4.2.1 概念说明

当系数 `c` 是**多维数组**时，`polyval` 把它的**第 0 轴**当作次数轴，其余轴「枚举多个多项式」。例如 `c.shape == (deg+1, M)` 表示「M 个多项式，每个的最高次为 deg」，`c[:, j]` 是第 j 个多项式的系数。

`tensor` 参数决定**点 x 与「多个多项式」如何组合**：

- `tensor=True`（默认）：每个多项式都在**每一个** x 上求值，输出形状 = `c.shape[1:] + x.shape`（外积式）。
- `tensor=False`：x 在多项式轴上**逐元素配对**广播，输出形状 = `c.shape[1:]`（要求 x 的形状能广播到这些轴）。

一句话区别：`True` 是「全部多项式 × 全部点」，`False` 是「第 j 个多项式配第 j 组点」。

#### 4.2.2 核心流程

`tensor` 的作用点只有一行（已在 4.1.3 引用）：

```python
if isinstance(x, np.ndarray) and tensor:
    c = c.reshape(c.shape + (1,) * x.ndim)
```

- `tensor=True`：给 `c` 的形状**末尾追加** `x.ndim` 个 1。这样广播时，系数的多项式轴在前、x 的轴在后，自然得到 `c.shape[1:] + x.shape`。
- `tensor=False`：不追加。于是 x 直接与 `c.shape[1:]` 对齐广播。

形状速查（设 `c.shape[1:] = P`，`x.shape = X`）：

| 设置 | 输出形状 | 语义 |
|------|----------|------|
| `tensor=True`  | `P + X` | 每个多项式在每个点求值 |
| `tensor=False` | `P`（x 广播到 P） | 多项式与点逐元素配对 |

#### 4.2.3 源码精读

结合文档示例（[polynomial.py:736-744](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L736-L744)）读这两行配置：

```python
>>> coef = np.arange(4).reshape(2, 2)   # shape (2,2): 2 个多项式，每个 1 次
>>> polyval([1, 2], coef, tensor=True)  # 输出 shape (2,)+(2,) = (2,2)
array([[2.,  4.],
       [4.,  7.]])
>>> polyval([1, 2], coef, tensor=False) # 输出 shape (2,)：第 j 列配 x[j]
array([2., 7.])
```

解读 `coef = [[0,1],[2,3]]`：按「列存多项式」约定，第 0 列系数 `[0,2]` 即 \(p_0(x)=0+2x\)，第 1 列 `[1,3]` 即 \(p_1(x)=1+3x\)。

- `tensor=True`：`c` 被重塑为 `(2,2,1)`（追加 x.ndim=1 个 1）。输出 `[[2,4],[4,7]]` 形状 `(2,2)`：第 0 行是 \(p_0\) 在 \(x=1,2\) 的值 `[2,4]`，第 1 行是 \(p_1\) 在 \(x=1,2\) 的值 `[4,7]`。
- `tensor=False`：`c` 保持 `(2,2)`。输出 `[2,7]`：\(p_0(1)=2\)、\(p_1(2)=7\)，**第 j 个多项式只用在第 j 个点上**。

注意：`tensor` 仅在 `x` 是 `np.ndarray` 时才生效（标量 x 不触发重塑），这也是函数文档强调「scalars have shape (,)」的原因。

#### 4.2.4 代码实践

**实践目标**：用同一组二维系数，对比 `tensor=True/False` 的输出形状与数值，固化对「外积 vs 配对」的理解。

```python
import numpy as np
from numpy.polynomial.polynomial import polyval

c = np.arange(4).reshape(2, 2)        # [[0,1],[2,2]]... 实为 [[0,1],[2,3]]
x = np.array([1, 2])

yt = polyval(x, c, tensor=True)
yf = polyval(x, c, tensor=False)
print(yt.shape, yf.shape)             # 预期 (2,2)  (2,)
print(yt)                             # [[2,4],[4,7]]
print(yf)                             # [2,7]
```

需要观察的现象：

1. `tensor=True` 输出 `(2,2)`，且第 j 行是第 j 个多项式在所有 x 上的值；
2. `tensor=False` 输出 `(2,)`，第 j 个元素 = 第 j 个多项式在 x[j] 上的值；
3. 把 `x` 改成标量 `polyval(1, c, tensor=True)`，输出形状变为 `c.shape[1:] = (2,)`（标量不触发重塑）。

预期结果与上面注释一致。形状规律是本实践要带走的核心结论。

#### 4.2.5 小练习与答案

**练习 1**：`c.shape=(3,4)`、`x.shape=(5,)`，`polyval(x,c,tensor=True)` 与 `tensor=False` 的输出形状分别是什么？

**参考答案**：`True` → `c.shape[1:] + x.shape = (4,)+(5,) = (4,5)`；`False` → `c.shape[1:] = (4,)`（要求 x 能广播到 (4,)，但 (5,) 不能广播到 (4,)，所以 `False` 在此例会因形状不兼容而报错）。

**练习 2**：为什么 `tensor=False` 时要把 `x` 广播到 `c.shape[1:]`，而不是反过来？

**参考答案**：因为 `tensor=False` 的语义是「多项式与点逐元素配对」，多项式由 `c.shape[1:]` 枚举，所以 x 必须能对齐到这个形状；若 x 维度更多，说明使用者的意图与 `False` 语义冲突，应改用 `True`。

---

### 4.3 polyvalfromroots

#### 4.3.1 概念说明

当一个多项式以**根**的形式给出（即首一多项式 \(p(x)=\prod_{n}(x-r_n)\)），`polyvalfromroots` 直接用这个连乘定义求值，**不先转成系数再走 Horner**。

这样做有两个好处：

1. **省去一次基转换**：不必调用 `polyfromroots` 先算系数（那本身是 \(O(n^2)\) 的卷积树），再求值；
2. **数值更稳**：当根已知时，每个因子 \((x-r_n)\) 的量级受控，连乘不易像展开后的高次幂那样剧烈放大。

数学定义：

\[
p(x) = \prod_{n=1}^{N}(x - r_n)
\]

#### 4.3.2 核心流程

```
若 tensor=True：把 r 重塑为 r.shape + (1,)*x.ndim，使每个根多项式作用到每个点
p = x - r            # 广播：得到「每个点减每个根」
return prod(p, axis=0)   # 沿「根轴」(axis 0) 连乘
```

关键：连乘沿 **axis=0**（根所在轴）进行，结果自动去掉这根轴，留下点轴与「多个多项式」轴。

#### 4.3.3 源码精读

完整实现见 [polynomial.py:762-846](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L762-L846)，核心计算只有几行（[polynomial.py:836-846](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L836-L846)）：

```python
r = np.array(r, ndmin=1, copy=None)
if r.dtype.char in '?bBhHiIlLqQpP':
    r = r.astype(np.double)                  # 整数根提升为双精度
if isinstance(x, (tuple, list)):
    x = np.asarray(x)
if isinstance(x, np.ndarray):
    if tensor:
        r = r.reshape(r.shape + (1,) * x.ndim)         # 与 polyval 同款的尾随 1 轴
    elif x.ndim >= r.ndim:
        raise ValueError("x.ndim must be < r.ndim when tensor == False")
return np.prod(x - r, axis=0)                # 沿根轴连乘
```

三个要点：

1. **`np.prod(..., axis=0)`**：`x - r` 经广播后，根排在 axis 0，沿它连乘正是 \(\prod(x-r_n)\)。
2. **`tensor` 与 `polyval` 完全同构**：同样靠「给 r 追加尾随 1 轴」实现外积语义。
3. **`tensor=False` 的额外护栏**：`x.ndim >= r.ndim` 时抛 `ValueError`。因为 `False` 要求 x 广播到 r 的「多项式轴」(r.shape[1:])，若 x 维度反而更高则语义无定义，必须显式报错（对比 `polyval` 的 `tensor=False` 没有这道检查，因为系数与根的形状含义略不同）。

验算 `polyvalfromroots(1, [1,2,3])`（[polynomial.py:814-816](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L814-L816)）：\(p(x)=(x-1)(x-2)(x-3)\)，\(p(1)=0\)，文档示例返回 `0.0`。

#### 4.3.4 代码实践

**实践目标**：验证 `polyvalfromroots` 与「先 `polyfromroots` 转系数，再 `polyval`」两条路径结果一致。

```python
import numpy as np
from numpy.polynomial.polynomial import polyvalfromroots, polyfromroots, polyval

roots = np.array([1.0, 2.0, 3.0])
x = np.array([0.0, 1.5, 4.0])

# 路径 A：直接用根求值
yA = polyvalfromroots(x, roots)

# 路径 B：先转系数，再 Horner
coef = polyfromroots(roots)
yB = polyval(x, coef)

print(yA, yB, np.allclose(yA, yB))   # 预期 True
```

需要观察的现象：

1. 两条路径数值一致（`allclose=True`）；
2. 当根的数量很大、点很多时，`polyvalfromroots` 通常更省事（省一次基转换）；
3. 把某个根改成复数（如 `1+0j`），输出自动为复数 dtype，`x - r` 的广播保持正确。

预期结果：`yA ≈ yB`。若追求极致速度对比，需要更大规模才能看出差异，可标注「待本地验证」具体加速比。

#### 4.3.5 小练习与答案

**练习 1**：`polyvalfromroots(2, [2, 2, 5])` 的结果是多少？它对应怎样的多项式？

**参考答案**：\(p(x)=(x-2)^2(x-5)\)，\(p(2)=0\)。结果 `0.0`。注意重根情形连乘仍正确。

**练习 2**：为什么 `tensor=False` 时要检查 `x.ndim < r.ndim`，而 `polyval` 的 `tensor=False` 不做类似检查？

**参考答案**：`polyvalfromroots` 的输出形状由 `np.prod(..., axis=0)` 决定，连乘会消去 axis 0；若 `x.ndim >= r.ndim`，x 与 r 的广播会让「根轴」与「点轴」错位，连乘语义无法成立，必须报错。`polyval` 用 Horner 累加，不存在「消去一根轴」的步骤，故无需此护栏。

---

### 4.4 _valnd / _gridnd 多维

#### 4.4.1 概念说明

二维多项式定义为：

\[
p(x, y) = \sum_{i,j} c_{i,j}\, x^i y^j
\]

更高维同理。`numpy.polynomial` 提供两组函数：

- `polyval2d/3d/nd`：在**配对**点 \((x,y)\) 上求值，要求 x、y 形状相同，输出形状 = `c.shape[N:] + x.shape`。
- `polygrid2d/3d`：在 x、y 的**笛卡尔积网格**上求值，输出形状 = `c.shape[N:] + x.shape + y.shape (+ z.shape)`。

核心设计巧思：**N 维求值不必新写算法**，只要把 1 维的 `polyval` 当作「沿一根轴折叠」的工具，**逐维折叠**即可。

直觉：把 \(p(x,y)\) 看作「关于 x 的多项式，其系数本身是关于 y 的多项式」。先沿 x 折叠一次，得到一个关于 y 的多项式，再沿 y 折叠一次，就得到数值。

#### 4.4.2 核心流程

`_valnd`（配对点）伪代码：

```
assert 所有 args（x,y,[z]）形状一致
c = val_f(x, c)                  # 第一维用 tensor=True（默认）：折叠 x，保留其余系数轴
for xi in 其余 args (y, [z]):
    c = val_f(xi, c, tensor=False)   # 后续维度用 tensor=False：逐点配对
return c
```

`_gridnd`（笛卡尔积）伪代码：

```
for xi in args (x, y, [z]):
    c = val_f(xi, c)             # 每一维都用 tensor=True：外积式扩张
return c
```

两者差别就一行：`_gridnd` 每步都用 `tensor=True`，所以每加入一个维度都做外积，形成网格；`_valnd` 只有第一步用 `tensor=True`，之后改 `tensor=False`，于是后续维度与已有轴逐元素配对，不做外积。

#### 4.4.3 源码精读

N 维引擎都在 `polyutils` 里，是跨六大正交族共享的（[polyutils.py:473-500](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L473-L500) 的 `_valnd`、[polyutils.py:503-516](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L503-L516) 的 `_gridnd`）：

```python
def _valnd(val_f, c, *args):
    args = [np.asanyarray(a) for a in args]
    shape0 = args[0].shape
    if not all(a.shape == shape0 for a in args[1:]):   # 配对点：形状必须一致
        ... raise ValueError('x, y are incompatible')  # 依维度数选报错文案
    it = iter(args)
    x0 = next(it)
    c = val_f(x0, c)                       # 第一维 tensor=True
    for xi in it:
        c = val_f(xi, c, tensor=False)     # 其余维 tensor=False
    return c

def _gridnd(val_f, c, *args):
    for xi in args:
        c = val_f(xi, c)                   # 每一维 tensor=True
    return c
```

注意 `_valnd` 里那句注释 `# use tensor on only the first`——它点明了「只首维用 tensor」的设计意图：第一步把 x 折叠进系数，得到一组「关于 y 的多项式系数」并保留所有点轴；之后每一维都用 `tensor=False` 把对应点逐元素代入，避免再产生外积。

`polynomial.py` 中的六个公开函数都只是**一行委托**：

- `polyval2d`：`return pu._valnd(polyval, c, x, y)`（[polynomial.py:855-904](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L855-L904)，核心在第 904 行）
- `polygrid2d`：`return pu._gridnd(polyval, c, x, y)`（[polynomial.py:907-960](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L907-L960)，第 960 行）
- `polyval3d`：`return pu._valnd(polyval, c, x, y, z)`（[polynomial.py:963-1013](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L963-L1013)）
- `polygrid3d`：`return pu._gridnd(polyval, c, x, y, z)`（[polynomial.py:1069-1125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1069-L1125)）
- `polyvalnd`：`return pu._valnd(polyval, c, *pts)`（[polynomial.py:1018-1067](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1018-L1067)）

这就是「父类/通用工具管流程、具体模块管算法」的又一次体现：`_valnd`/`_gridnd` 把「逐维折叠」的流程写死，具体「沿一根轴怎么折叠」由注入的 `polyval`（对 Chebyshev 族则是 `chebval`）决定。换基只需换注入函数，N 维逻辑完全复用。

**与范德蒙矩阵的等价关系**：`polyvander` 构造的矩阵满足 \(V[\dots, i] = x^i\)（[polynomial.py:1128-1192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1128-L1192)），因此对 1 维系数 `c` 有

\[
\texttt{np.dot}(V, c) \equiv \texttt{polyval}(x, c)
\]

（舍入误差内相等）。这正是 `polyfit` 用范德蒙矩阵做最小二乘的基础，也是综合实践要用到的等式。

#### 4.4.4 代码实践

**实践目标**：用同一组二维系数，分别走 `polyval2d`（配对点）与 `polygrid2d`（网格），对照输出形状，体会「外积 vs 配对」在多维下的体现；再用范德蒙矩阵复现一次 `polyval2d`。

```python
import numpy as np
from numpy.polynomial.polynomial import polyval2d, polygrid2d, polyvander2d

c = np.array([[1, 2, 3],
              [4, 5, 6]])      # c[i,j]: x^i y^j 的系数；i∈{0,1}, j∈{0,1,2}

# (a) 配对点：x 与 y 形状必须一致
xs = np.array([0.0, 1.0])
ys = np.array([0.0, 1.0])
y_val = polyval2d(xs, ys, c)
print(y_val.shape)             # 预期 (2,)：c.shape[2:] + xs.shape = () + (2,)

# (b) 网格：笛卡尔积
y_grid = polygrid2d(np.array([0., 1.]), np.array([0., 1.]), c)
print(y_grid.shape)            # 预期 (2,2)：x.shape + y.shape

# (c) 用 polyval2d 文档示例自检
print(polyval2d(1, 1, c))      # 预期 21.0
print(polygrid2d([0, 1], [0, 1], c))  # 预期 [[1,6],[5,21]]
```

需要观察的现象：

1. `polyval2d(xs, ys, c)` 输出形状 `(2,)`：它逐点算 \(p(x_0,y_0)\)、\(p(x_1,y_1)\)；
2. `polygrid2d` 输出形状 `(2,2)`：四个组合 \(p(x_i,y_j)\) 全部算出；
3. `polyval2d(1,1,c)=21.0` 与文档示例一致（手算：\(1+2+3+4+5+6=21\)）。

预期结果与注释一致。若想进一步用范德蒙等价：注意 `polyvander2d` 返回扁平化的二维矩阵，验证 `np.dot(polyvander2d(x, y, [1,2]).T, c.ravel())` 与对应 `polygrid2d` 的关系——这一步的精确形状对齐留作探索（具体写法待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`c.shape=(3,4)`（i∈0..2, j∈0..3），`polyval2d(x, y, c)` 与 `polygrid2d(x, y, c)` 输出形状各是什么（设 x、y 为 1 维数组，长度分别为 5、6）？

**参考答案**：`polyval2d` 要求 x、y 形状一致，所以 x、y 必须同长（题目给 5≠6 会先报 `x, y are incompatible`）；若都长 5，输出 `c.shape[2:] + x.shape = () + (5,) = (5,)`。`polygrid2d` 输出 `c.shape[2:] + x.shape + y.shape = (5,6)`。

**练习 2**：为什么 `_valnd` 第二维起改用 `tensor=False`，而 `_gridnd` 全程用 `tensor=True`？

**参考答案**：`_valnd` 处理「配对点」，第一维折叠后已经产生了点轴，后续维度必须与该轴逐元素对齐，所以用 `tensor=False` 避免外积；`_gridnd` 处理「笛卡尔积」，每一维都应与已有轴做外积扩张，所以全程 `tensor=True`。

**练习 3**：把 `polyval2d` 换成 `chebval2d`（Chebyshev 族），`_valnd` 的代码需要改动吗？

**参考答案**：不需要。`_valnd` 接收 `val_f` 作为注入参数，`polyval2d` 注入 `polyval`、`chebval2d` 注入 `chebval`。流程（逐维折叠、首维 tensor=True、余维 tensor=False）与基无关，这正是把 `_valnd` 放在共享的 `polyutils` 里的设计收益。

---

## 5. 综合实践

把本讲四个模块串起来：**用三种独立方法求同一个多项式的值，并互相比对**。

任务：取一个 4 次多项式 \(p(x)=1 - 2x + 3x^2 - 4x^3 + 5x^4\)（系数 `c=[1,-2,3,-4,5]`），在 \(x\in[-1,1]\) 上取 50 个点。

1. **Horner 法**：`y1 = polyval(x, c)`。
2. **范德蒙矩阵法**：`V = polyvander(x, 4); y2 = V @ c`，验证 `np.allclose(y1, y2)`（体会 4.1.3 末尾的等价关系，也为下一讲 [u3-l5](u3-l5-vandermonde-leastsquares-fit.md) 的最小二乘拟合做铺垫）。
3. **根式法**：先用 `polyroots(c)` 求根 `r`，再用 `polyvalfromroots(x, r)` 得 `y3`（注意首一多项式与原多项式可能差一个常数倍，需用 `polyfromroots(r)` 还原后比对，或直接 `polyval(x, polyfromroots(r))` 与 `y1` 比）。

进阶：把 `c` 升级成二维 `c2 = np.vstack([c, c*2]).T`（shape `(5,2)`，两个多项式），分别用 `tensor=True/False` 在同一组 x 上求值，打印形状并解释每一行/每个元素对应哪个多项式与哪个点。

需要观察的现象：

- 三种 1 维方法在舍入误差内完全一致；
- `tensor=True` 给出 `(2,50)`（2 个多项式 × 50 个点），`tensor=False` 给出 `(2,)`（要求 x 长度为 2 才能配对）；
- 范德蒙法 `V @ c` 与 `polyval` 的等价是本实践最该记住的结论。

预期结果：`allclose` 全为 `True`；多维部分的形状符合 4.2 的速查表。具体计时与舍入误差量级「待本地验证」。

## 6. 本讲小结

- `polyval` 用 **Horner 法**把求值降到 \(O(n)\)：从最高次系数起步，每步「乘 x、加低一次系数」，并用 `c[-1] + x*0` 一次性完成形状/dtype 对齐。
- `tensor` 控制多维系数与点的组合方式：`True` 是外积（每个多项式 × 每个点，输出 `c.shape[1:] + x.shape`），`False` 是配对（第 j 个多项式配第 j 组点，输出 `c.shape[1:]`）。机制是「给 c 追加 `x.ndim` 个尾随 1 轴」。
- `polyvalfromroots` 直接用 \(\prod(x-r_n)\) 连乘求值，省去基转换、数值更稳；`tensor=False` 多一道 `x.ndim < r.ndim` 护栏。
- `polyval2d/3d/nd` 与 `polygrid2d/3d` 全部委托 `polyutils._valnd` / `_gridnd`，把 N 维求值拆成「逐维 1 维 `polyval` 折叠」；两者唯一差别是后续维度用 `tensor=False`（配对）还是 `tensor=True`（网格）。
- 设计哲学再现：通用工具 `_valnd`/`_gridnd` 写死「逐维折叠」流程，具体算法由注入的 `polyval`/`chebval` 决定，换基不换流程。
- 范德蒙等价：`np.dot(polyvander(x,n), c) ≡ polyval(x,c)`，是连接「求值」与「拟合」的桥梁。

## 7. 下一步学习建议

- 下一讲 [u3-l4 求导与积分](u3-l4-derivative-integral.md) 将把求值的对偶操作——`polyder`/`polyint`——讲清楚，其中积分常数 `k` 与下界 `lbnd` 的代入正是靠 `polyval` 回代完成，本讲的 Horner 是它的前置。
- 再下一讲 [u3-l5 Vandermonde 矩阵与最小二乘拟合](u3-l5-vandermonde-leastsquares-fit.md) 会深度利用本讲的范德蒙等价式，建议先把综合实践中「`V @ c` 与 `polyval` 一致」这条跑通。
- 若关心数值稳定性，可跳读 [u5-l1 数值稳定性与架构取舍](u5-l1-numerical-stability-tradeoffs.md)，那里会讨论「幂基在远离原点处病态」——正是 Horner 法在高次/远点也会遇到的局限，并解释 Chebyshev 等正交基为何更稳。
- 想理解便捷类如何把本讲的「裸求值」包装成带 domain/window 映射的 `p(x)`，回顾 [u2-l2 域 domain、窗口 window 与线性映射](u2-l2-domain-window-mapping.md) 的 `__call__` 一节即可。
