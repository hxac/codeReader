# 域 domain、窗口 window 与线性映射

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `domain` 与 `window` 这两个区间各自的含义，以及为什么要把它们分开。
- 手算 `pu.mapparms(old, new)` 给出的线性映射 \(L(x)=\text{off}+\text{scl}\cdot x\)，并用它解释 `ABCPolyBase.mapparms()`。
- 解释 `__call__`、`roots`、`fit`、`deriv`、`integ` 分别在哪里、朝哪个方向做了坐标换算。
- 理解为什么只有 `Laguerre` 的默认 `domain`/`window` 是 `[0, 1]`，而其余五族都是 `[-1, 1]`。

本讲承接 u2-l1：你已经知道六大便捷类都继承自 `ABCPolyBase`，子类通过「虚函数委托」提供基函数算法。本讲聚焦 `ABCPolyBase` 上另一个贯穿全包的设计支柱——**双区间 + 线性映射**。

## 2. 前置知识

阅读本讲前，你需要：

- 知道 numpy.polynomial 用 1-D 系数数组表示多项式，`coef[i]` 是第 `i` 个基函数 `P_i(x)` 的系数（见 u1-l1）。
- 知道 `p(x)` 求值会委托给虚函数 `_val`（见 u2-l1）。
- 会一点线性代数：理解仿射变换 \(L(x)=a+bx\)（一条直线）。
- 了解最小二乘与条件数的直觉即可：当矩阵各列数值差异巨大时，求解会变得不稳定。

**一句话直觉**：你给多项式对象一个「对外宣称的自变量取值范围」`domain`（比如你采样的数据落在 `[0, 10]`），再给一个「基函数最舒服的取值范围」`window`（比如 Chebyshev 基在 `[-1, 1]` 上性质最好）。每次求值前，库会先把你的 `x` 从 `domain` 线性搬进 `window`，再在 `window` 里算基函数。这样数值既稳，接口又自然。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [_polybase.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | `ABCPolyBase` 的 `__init__`、`__call__`、`mapparms`、`roots`、`fit`、`deriv`、`integ`，集中体现双区间机制 |
| [polyutils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | `mapparms(old, new)`、`mapdomain(x, old, new)`、`getdomain(x)` 三个底层工具 |
| [polynomial.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | `Polynomial` 类的默认 `domain`/`window = [-1, 1]` |
| [laguerre.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/laguerre.py) | `Laguerre` 类的默认 `domain`/`window = [0, 1]`（唯一特殊的一族） |

---

## 4. 核心概念与源码讲解

### 4.1 domain 与 window：双区间概念

#### 4.1.1 概念说明

每个便捷类对象除了 `coef`，还随身携带两个长度为 2 的数组：

- **`domain`**：**对外**的自变量区间。它是「用户世界」的坐标。比如你的实验数据 `x` 落在 `[0, 10]`，就把 `domain` 设成 `[0, 10]`。
- **`window`**：**对内**的参考区间。它是「基函数世界」的坐标。比如 Chebyshev 多项式 `T_n` 在 `[-1, 1]` 上正交、性质最好，于是 `Chebyshev` 的默认 `window` 就是 `[-1, 1]`。

为什么要分两个区间？因为「用户希望输入什么坐标」和「基函数在哪个区间上数值最好」常常不一致。库的做法是：**系数始终按 `window` 变量表达，求值时自动把 `domain` 坐标搬到 `window` 坐标**。这样你既可以用自己顺手的坐标（`domain`），又能享受基函数在 `window` 上的数值优势，而无需手动做变量替换。

一个关键事实：**同一组系数，在不同 `domain`/`window` 下代表不同的函数**。因为求值前会先做线性映射，映射不同，最终函数值就不同。

#### 4.1.2 核心流程

对象的生命周期里，`domain` 与 `window` 的分工是：

1. 构造时：用户可显式传入 `domain`、`window`；不传则用类属性默认值。
2. 求值 `p(x)`：`x` 视作 `domain` 坐标 → 线性映射到 `window` 坐标 → 在 `window` 下用 `_val` 算基函数。
3. 求根 `p.roots()`：先用 `_roots` 在 `window` 下求根 → 再把根从 `window` 反向映射回 `domain`。
4. 拟合 `Polynomial.fit(...)`：把数据 `x` 从自动推断的 `domain` 映射到 `window`，在 `window` 下做最小二乘。

伪代码：

```
求值：   u = L_{domain→window}(x);  y = _val(u, coef)
求根：   u_roots = _roots(coef);    x_roots = L_{window→domain}(u_roots)
拟合：   u = L_{domain→window}(x);  coef = _fit(u, y, deg)
```

其中 \(L_{a\to b}\) 表示把区间 `a` 线性映射到区间 `b` 的那个仿射变换。

#### 4.1.3 源码精读

`domain`/`window` 在构造时被记录，默认回退到类属性：

[_polybase.py:292-306](_polybase.py#L292-L306) —— `__init__` 中，若用户没传 `domain`/`window`，就保留子类类属性（如 `Polynomial.domain`）；若传了则覆盖，并校验长度必须为 2：

```python
def __init__(self, coef, domain=None, window=None, symbol='x'):
    [coef] = pu.as_series([coef], trim=False)
    self.coef = coef
    if domain is not None:
        [domain] = pu.as_series([domain], trim=False)
        if len(domain) != 2:
            raise ValueError("Domain has wrong number of elements.")
        self.domain = domain
    if window is not None:
        ...
```

注意 `domain`/`window` 是「实例属性覆盖类属性」的经典用法：不传时 `self.domain` 解析到类属性，传了就在实例上新建同名属性。

`Polynomial` 的类属性默认值定义在：

[polynomial.py:1656-1657](polynomial.py#L1656-L1657) —— `Polynomial` 类体里把默认 `domain`/`window` 都设为 `polydomain`：

```python
domain = np.array(polydomain)
window = np.array(polydomain)
```

而 [polynomial.py:98](polynomial.py#L98) 中 `polydomain = np.array([-1., 1.])`。所以 `Polynomial` 默认 `domain == window == [-1, 1]`，此时映射为恒等（`off=0, scl=1`），用户通常感觉不到映射的存在——这正是默认情形「不添麻烦」的设计意图。

`ABCPolyBase` 的类文档把双区间的关系说得很清楚：[_polybase.py:33-39](_polybase.py#L33-L39) ——「The interval `[domain[0], domain[1]]` is mapped to the interval `[window[0], window[1]]` by shifting and scaling」。

#### 4.1.4 代码实践

1. 实践目标：直观感受「默认 `domain==window` 时映射为恒等」，以及显式改 `domain` 后对象行为如何变化。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial

   p_default = Polynomial([1, 2, 3])              # domain=window=[-1,1]
   print(p_default.domain, p_default.window)      # [-1.  1.] [-1.  1.]
   print(p_default.mapparms())                    # (0.0, 1.0)  ← 恒等

   p_wide = Polynomial([1, 2, 3], domain=[0, 10]) # window 仍取类默认 [-1,1]
   print(p_wide.domain, p_wide.window)            # [0. 10.] [-1.  1.]
   print(p_wide.mapparms())                       # (-1.0, 0.2)
   ```

3. 需要观察的现象：默认对象 `mapparms()` 返回 `(0.0, 1.0)`（恒等映射）；把 `domain` 改成 `[0, 10]` 后返回 `(-1.0, 0.2)`。
4. 预期结果：如上。`p_wide` 与 `p_default` 系数相同，但因为映射不同，它们是**不同的函数**——可以打印 `p_wide(0)` 与 `p_default(0)` 对比（前者 2，后者 0 附近，详见 4.3）。
5. 若数值与预期不符，请核对你用的 NumPy 版本下 `Polynomial.window` 确为 `[-1, 1]`。

#### 4.1.5 小练习与答案

**练习**：如果一个对象的 `domain == window`，`mapparms()` 一定返回 `(0, 1)` 吗？请用 `mapparms` 的定义验证。

**答案**：是的。当 `old == new` 时，`oldlen == newlen`，故 `scl = newlen/oldlen = 1`；而 `off = (old[1]*new[0] - old[0]*new[1])/oldlen`，把 `new=old` 代入得 `(old[1]*old[0] - old[0]*old[1])/oldlen = 0`。所以映射退化为恒等。

---

### 4.2 mapparms：线性映射的计算

#### 4.2.1 概念说明

两个区间之间的「平移 + 缩放」是一个仿射变换 \(L(x)=\text{off}+\text{scl}\cdot x\)，由两个端点对应关系唯一确定：

\[
L(\text{old}[0]) = \text{new}[0], \qquad L(\text{old}[1]) = \text{new}[1]
\]

解这个二元一次方程组得：

\[
\text{scl} = \frac{\text{new}[1]-\text{new}[0]}{\text{old}[1]-\text{old}[0]}, \qquad
\text{off} = \text{new}[0] - \text{scl}\cdot\text{old}[0]
\]

`polyutils.mapparms(old, new)` 就是算出这组 `(off, scl)`；`mapdomain(x, old, new)` 则是直接把 `L` 作用到一批点 `x` 上。注意该映射对复数同样成立，因此可以把复平面上的任一条线段映到另一条线段。

#### 4.2.2 核心流程

`mapparms` 的计算流程：

```
oldlen = old[1] - old[0]
newlen = new[1] - new[0]
off    = (old[1]*new[0] - old[0]*new[1]) / oldlen   # 等价于 new[0] - scl*old[0]
scl    = newlen / oldlen
return off, scl
```

`mapdomain` 的实现非常薄：先调 `mapparms` 拿到 `(off, scl)`，再返回 `off + scl*x`。它的 docstring 给出等价写法：

\[
x\_out = \text{new}[0] + m\cdot(x - \text{old}[0]), \qquad
m = \frac{\text{new}[1]-\text{new}[0]}{\text{old}[1]-\text{old}[0]}
\]

这与 `off + scl*x` 完全一致（展开即得）。

#### 4.2.3 源码精读

[polyutils.py:241-286](polyutils.py#L241-L286) —— `mapparms(old, new)` 的全部实现，核心四行在 [polyutils.py:282-286](polyutils.py#L282-L286)：

```python
oldlen = old[1] - old[0]
newlen = new[1] - new[0]
off = (old[1] * new[0] - old[0] * new[1]) / oldlen
scl = newlen / oldlen
return off, scl
```

`off` 用的是交叉乘积形式，与 `new[0] - scl*old[0]` 代数等价，但避免了先算 `scl` 再做一次减法的轻微开销（也便于复数域统一处理）。

[polyutils.py:288-356](polyutils.py#L288-L356) —— `mapdomain(x, old, new)`，关键两行在 [polyutils.py:354-355](polyutils.py#L354-L355)：

```python
off, scl = mapparms(old, new)
return off + scl * x
```

而 `ABCPolyBase` 把这两个工具包成了一个实例方法 [_polybase.py:816-843](_polybase.py#L816-L843)，`mapparms()` 直接委托：

```python
def mapparms(self):
    ...
    return pu.mapparms(self.domain, self.window)
```

所以 `p.mapparms()` 等价于 `pu.mapparms(p.domain, p.window)`，给出的是 **domain → window** 方向的映射参数。

#### 4.2.4 代码实践

1. 实践目标：手算与函数返回值对照，确认你真的理解了公式。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.polynomial import polyutils as pu

   # 把 [0, 10] 映到 [-1, 1]
   off, scl = pu.mapparms([0, 10], [-1, 1])
   print(off, scl)                       # -1.0, 0.2

   # 手算验证：off = new[0] - scl*old[0] = -1 - 0.2*0 = -1; scl = 2/10 = 0.2

   # 用 mapdomain 把一组点搬过去
   x = np.array([0, 2.5, 5, 7.5, 10])
   print(pu.mapdomain(x, [0, 10], [-1, 1]))   # [-1.  -0.5  0.   0.5  1. ]
   ```

3. 需要观察的现象：`mapdomain` 把 `0→-1`、`5→0`、`10→1`，是严格的线性拉伸。
4. 预期结果：如注释所示。你也可以验证 `mapdomain(mapdomain(x, a, b), b, a) == x`（往返恒等）。
5. 这是确定性算术，结果可在本地直接复现。

#### 4.2.5 小练习与答案

**练习 1**：把区间 `[-1, 1]` 映到 `[0, 10]`，`off` 和 `scl` 各是多少？

**答案**：`scl = (10-0)/(1-(-1)) = 5`；`off = 0 - 5*(-1) = 5`。即 `L(x) = 5 + 5x`。验证：`L(-1)=0`，`L(1)=10` ✓。这正是 4.3 中 `roots` 反向映射会用到的参数。

**练习 2**：`pu.mapparms((-1,1),(-1,1))` 返回什么？为什么？

**答案**：`(0.0, 1.0)`。`old==new` 时映射为恒等（见 4.1.5）。

---

### 4.3 `__call__` 与 `roots` 中的坐标换算

#### 4.3.1 概念说明

这是本讲最关键的一节：**同一个 `(off, scl)`，在求值和求根里方向相反。**

- **求值**：用户给的是 `domain` 坐标，需要搬到 `window` 才能喂给基函数 → 用 **domain → window** 映射。
- **求根**：`_roots`（友矩阵特征值）算出来的是 `window` 坐标下的根，需要搬回 `domain` 才能给用户 → 用 **window → domain** 映射。

同理：

- **`fit`**：把采样点 `x` 从 `domain` 搬到 `window`，再在 `window` 下构造范德蒙矩阵做最小二乘。
- **`deriv`**：链式法则。若 \(u = \text{off}+\text{scl}\cdot x\)，则 \(\frac{d}{dx}=\text{scl}\cdot\frac{d}{du}\)，所以导数系数乘 `scl`。
- **`integ`**：积分是求导的逆，且要把下界 `lbnd` 也搬到 `window`，积分系数乘 `1/scl`。

#### 4.3.2 核心流程

| 操作 | 内部坐标换算 | 方向 |
| --- | --- | --- |
| `p(x)` / `__call__` | `u = mapdomain(x, domain, window)`；`_val(u, coef)` | domain → window |
| `p.roots()` | `u = _roots(coef)`；`mapdomain(u, window, domain)` | window → domain |
| `Polynomial.fit(x,y,deg)` | `xnew = mapdomain(x, domain, window)`；`_fit(xnew, y, deg)` | domain → window |
| `p.deriv()` | `(off,scl)=mapparms()`；`_der(coef, m, scl)` | 用 scl 做链式 |
| `p.integ()` | `(off,scl)=mapparms()`；`lbnd=off+scl*lbnd`；`_int(coef, m, k, lbnd, 1/scl)` | 用 1/scl 做积分 |

记忆口诀：**「输入往 window 走，输出往 domain 回」**。

#### 4.3.3 源码精读

**求值 `__call__`** —— [_polybase.py:510-512](_polybase.py#L510-L512)：

```python
def __call__(self, arg):
    arg = pu.mapdomain(arg, self.domain, self.window)
    return self._val(arg, self.coef)
```

先把 `arg` 从 `domain` 映到 `window`，再委托虚函数 `_val`（`Polynomial` 下即 `polyval`，Horner 法）。这正是「求值先映射」的源头。

**求根 `roots`** —— [_polybase.py:900-913](_polybase.py#L900-L913)，关键两行在 [_polybase.py:912-913](_polybase.py#L912-L913)：

```python
roots = self._roots(self.coef)
return pu.mapdomain(roots, self.window, self.domain)
```

注意第二个参数是 `self.window`、第三个是 `self.domain`，方向与 `__call__` **相反**。`_roots`（友矩阵特征值）返回的是 `window` 坐标下的根，必须映回 `domain`。docstring 还提醒：根离 `domain` 越远，精度越差（[_polybase.py:903-904](_polybase.py#L903-L904)）。

**拟合 `fit`** —— [_polybase.py:1014-1026](_polybase.py#L1014-L1026)：当 `domain=None` 时用 `pu.getdomain(x)` 自动取数据的最小外包区间（[_polybase.py:1014-1015](_polybase.py#L1014-L1015)），然后把 `x` 映到 `window` 再拟合：

```python
if domain is None:
    domain = pu.getdomain(x)
    ...
xnew = pu.mapdomain(x, domain, window)
res = cls._fit(xnew, y, deg, w=w, rcond=rcond, full=full)
```

把数据搬到 `window`（如 `[-1,1]`）后再构造范德蒙矩阵，列的数值范围被压缩、条件数大幅改善——这是 `fit` 数值稳定的关键动机（u5-l1 会展开）。

**求导 `deriv`** —— [_polybase.py:896-897](_polybase.py#L896-L897)：

```python
off, scl = self.mapparms()
coef = self._der(self.coef, m, scl)
```

链式法则把 `scl` 乘进导数：复合函数 \(p(\text{off}+\text{scl}\,x)\) 对 `x` 求导多出一个 `scl` 因子。

**积分 `integ`** —— [_polybase.py:870-875](_polybase.py#L870-L875)：

```python
off, scl = self.mapparms()
if lbnd is None:
    lbnd = 0
else:
    lbnd = off + scl * lbnd       # 把用户给的 domain 下界搬到 window
coef = self._int(self.coef, m, k, lbnd, 1. / scl)   # 积分乘 1/scl
```

积分是求导的逆，所以因子是 `1/scl`；同时把用户在 `domain` 下给出的积分下界 `lbnd` 也搬到 `window`。

#### 4.3.4 代码实践（本讲主实践）

1. 实践目标：用 `mapparms()` 读出 `off/scl`，**手算** `p(0)` 与 `p(10)`，再与实际调用对比；最后检查 `roots()` 是否落在 `[0, 10]`。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial

   p = Polynomial([1, 2, 3], domain=[0, 10])   # window 默认 [-1, 1]
   off, scl = p.mapparms()
   print("off, scl =", off, scl)               # -1.0, 0.2

   # 手算：L(x) = -1 + 0.2x
   # p(0):  u = L(0)  = -1;  polyval(-1, [1,2,3]) = 1 + 2*(-1) + 3*(-1)**2 = 2
   # p(10): u = L(10) =  1;  polyval( 1, [1,2,3]) = 1 + 2* 1  + 3* 1 **2 = 6
   print("p(0)  =", p(0))                      # 2.0
   print("p(10) =", p(10))                     # 6.0

   # 求根：_roots 在 window 下求，再映回 domain
   print("roots =", p.roots())
   ```

3. 需要观察的现象：`p(0)` 恰为 `2.0`、`p(10)` 恰为 `6.0`，与手算一致。
4. 预期结果：`p(0)=2.0`、`p(10)=6.0`。这证明了「求值先做 domain→window 映射」。

   关于 `roots()`：`[1,2,3]` 在 `window` 下的多项式 \(1+2u+3u^2\) 判别式为 \(4-12<0\)，根是复数 \((-1\pm i\sqrt{2})/3\)。`mapdomain` 对复数同样适用，会把它们映成实部约 `3.33` 的复数（仍在 `[0,10]` 的实部范围内，但并非实根）。

   若想看到**干净落在 `[0,10]` 内的实根**，换一组在 `window` 下有实根的系数，例如希望 `window` 下根在 \(u=\pm 0.5\)，对应 \(u^2-0.25\)，即系数 `[-0.25, 0, 1]`：

   ```python
   q = Polynomial([-0.25, 0, 1], domain=[0, 10])
   print(q.roots())        # 约为 [2.5, 7.5]，落在 [0,10] 内
   ```

   手算验证：window 下根 \(\pm 0.5\)，反向映射 \(L^{-1}(u)=5+5u\)（见 4.2.5 练习 1），得 \(5+5(-0.5)=2.5\)、\(5+5(0.5)=7.5\)，均在 `[0,10]` 内 ✓。
5. 复根情况由源码逻辑可确定，但具体浮点值建议本地验证；实根例 `[2.5, 7.5]` 可由手算严格推出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `roots()` 里 `mapdomain` 的方向必须和 `__call__` 相反？

**答案**：`__call__` 把用户的 `domain` 输入搬进 `window` 才能算基函数；`_roots` 算出的根本身就在 `window` 坐标下，要交还给用户必须搬回 `domain`。两者一进一出，方向自然相反，否则坐标会错乱。

**练习 2**：若把 `p = Polynomial([1,2,3], domain=[0,10])` 的 `domain` 改成 `[-1,1]`（即默认），`p(0)` 还等于 `2.0` 吗？

**答案**：不等。默认 `domain==window==[-1,1]`，映射恒等，`p(0) = polyval(0, [1,2,3]) = 1`。可见**同系数不同 `domain` 即不同函数**。

**练习 3**：`p.deriv()` 的系数为什么会乘 `scl`？

**答案**：对象实际表达的是复合函数 \(p(\text{off}+\text{scl}\,x)\)。对 `x` 求导时链式法则引入因子 \(\text{scl}\)，所以 `_der` 收到的缩放参数是 `scl`。

---

### 4.4 Laguerre 的 `domain=[0, 1]` 特殊性

#### 4.4.1 概念说明

六大便捷类中，有五族（`Polynomial`、`Chebyshev`、`Legendre`、`Hermite`、`HermiteE`）的默认 `domain`/`window` 都是 `[-1, 1]`，**唯独 `Laguerre` 的默认是 `[0, 1]`**。

这并非随意：Laguerre 多项式在权函数 \(e^{-x}\) 下于半无界区间 \([0, +\infty)\) 上正交，它的「自然栖息地」从 `0` 开始，而不是关于原点对称。因此 `Laguerre` 选用从 `0` 出发的参考区间 `[0, 1]` 作为默认 `window`，比 `[-1, 1]` 更贴合这一族的定义域直觉。`Hermite`/`HermiteE` 虽然定义在全实轴，没有有限的「自然」区间，便沿用通用的 `[-1, 1]` 约定。

对用户的影响：用 `Laguerre` 拟合时，若不显式指定 `domain`，`fit` 会自动用 `getdomain(x)` 取数据范围、再映到 `[0, 1]`；而其余族映到 `[-1, 1]`。

#### 4.4.2 核心流程

- 模块级常量 `lagdomain = [0., 1.]` 是唯一从 `0` 起步的区间常量。
- `Laguerre` 类把 `domain` 与 `window` 都绑到 `lagdomain` 的拷贝。
- 因为 `domain == window == [0,1]`，默认情形下 `Laguerre` 对象的映射也是恒等——和其他族「默认恒等」的行为一致，只是恒等的参考区间不同。

#### 4.4.3 源码精读

[laguerre.py:202](laguerre.py#L202) —— 模块级区间常量：

```python
lagdomain = np.array([0., 1.])
```

[laguerre.py:1727-1728](laguerre.py#L1727-L1728) —— `Laguerre` 类把它作为默认 `domain`/`window`：

```python
domain = np.array(lagdomain)
window = np.array(lagdomain)
```

对照 [polynomial.py:98](polynomial.py#L98) 的 `polydomain = np.array([-1., 1.])`，可见六族中只有 `Laguerre` 起步于 `0`。其余族（`chebdomain`、`legdomain`、`hermdomain`、`hermedomain`）同为 `[-1., 1.]`，可自行用 Grep 验证。

#### 4.4.4 代码实践

1. 实践目标：确认六族的默认区间，并观察 `Laguerre` 默认映射也是恒等。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.polynomial import (
       Polynomial, Chebyshev, Legendre, Laguerre, Hermite, HermiteE)

   for cls in (Polynomial, Chebyshev, Legendre, Laguerre, Hermite, HermiteE):
       print(cls.__name__, cls.domain, cls.window,
             cls(1).mapparms())   # 默认 domain==window → (0.0, 1.0)
   ```

3. 需要观察的现象：只有 `Laguerre` 一行显示 `[0. 1.] [0. 1.]`，其余都是 `[-1.  1.] [-1.  1.]`；六族的 `mapparms()` 都返回 `(0.0, 1.0)`（默认恒等）。
4. 预期结果：如上表所示。
5. 结果确定，可本地复现。

#### 4.4.5 小练习与答案

**练习**：若你有一组 `x` 落在 `[2, 5]`，分别用 `Polynomial.fit` 和 `Laguerre.fit` 拟合，两者把 `x` 映到的 `window` 分别是什么？

**答案**：`Polynomial.fit`（默认 `domain=None`）先用 `getdomain(x)` 得到 `[2, 5]`，再映到类默认 `window=[-1, 1]`；`Laguerre.fit` 同样得到 `domain=[2, 5]`，但映到类默认 `window=[0, 1]`。两个 `window` 不同，因而系数在不同基、不同区间下表达，不能直接比较数值。

---

## 5. 综合实践

把本讲的双区间与线性映射串起来，完成下面这个调查任务：

> 给定数据点 `x = np.linspace(0, 10, 20)`、`y = np.sin(x)`。
>
> 1. 用 `Polynomial.fit(x, y, 5)` 拟合，打印返回对象的 `domain`、`window`、`mapparms()`。
> 2. 解释：为什么 `domain` 大约是 `[0, 10]` 而 `window` 是 `[-1, 1]`？拟合系数是在哪个变量下表达的？
> 3. 用 `p(0)`、`p(5)`、`p(10)` 求值，确认它们分别近似 `sin(0)=0`、`sin(5)`、`sin(10)≈-0.544`，验证「求值自动做 domain→window 映射」。
> 4. 对比错误用法：直接 `np.polynomial.polynomial.polyval(x, p.coef)`（**不**做映射）会得到错误结果，请解释为什么——这正是 `p(x)` 要先 `mapdomain` 的原因。

参考要点：

- `fit` 中 `domain=None` 触发 `pu.getdomain(x)` 自动取 `[min(x), max(x)] ≈ [0, 10]`，`window` 取类默认 `[-1, 1]`。
- 拟合系数是 `window` 变量 \(u\) 下的，即 `polyval(u, coef)` 才是正确求值；用 `p(x)` 会自动完成 \(x\to u\) 的映射，所以正确。
- 直接 `polyval(x, p.coef)` 把 `x`（domain 坐标，量级到 10）当成了 `u`（window 坐标，量级到 1），坐标错配，结果错误。

## 6. 本讲小结

- 每个便捷类对象都带 `domain`（用户坐标区间）与 `window`（基函数参考区间）两个属性；**系数始终按 window 变量表达**。
- `pu.mapparms(old, new)` 算出仿射映射 \(L(x)=\text{off}+\text{scl}\,x\)，把 `old` 端点映到 `new` 端点；`mapdomain` 是它对一批点的批量作用。
- 求值 `__call__` 把输入 **domain→window** 映射后再算基函数；求根 `roots` 把 `window` 下的根 **window→domain** 反向映射后返回——方向相反，口诀「输入往 window 走，输出往 domain 回」。
- `fit` 自动用 `getdomain` 取数据范围并映到 `window`，是数值稳定的关键；`deriv` 乘 `scl`、`integ` 乘 `1/scl` 体现链式法则与逆运算。
- 六族中**只有 `Laguerre` 的默认 `domain`/`window` 是 `[0, 1]`**（源于它在半无界区间 \([0,\infty)\) 上的正交性），其余五族均为 `[-1, 1]`。
- 同一组系数配不同 `domain`/`window` 代表不同函数；默认 `domain==window` 时映射为恒等，用户通常无感。

## 7. 下一步学习建议

- 想看映射如何**改善条件数**、让拟合更稳：进入 u5-l1「数值稳定性与架构取舍」，那里会用 `[100,110]` 上拟合的例子量化对比。
- 想看映射如何体现在**打印**里（自变量变成 `(off+scale·x)`）：阅读 u2-l4 / u5-l2 的 `_generate_string` 与 `_format_term`。
- 想深入 `fit` 内部 `_fit` 的列归一化与 `lstsq`：阅读 u3-l5「Vandermonde 矩阵与最小二乘拟合」。
- 建议直接打开 [_polybase.py:816-843](_polybase.py#L816-L843)（`mapparms`）、[_polybase.py:510-512](_polybase.py#L510-L512)（`__call__`）、[_polybase.py:900-913](_polybase.py#L900-L913)（`roots`）三处，对照本讲把「一进一出」的映射方向在脑中走一遍。
