# 求导与积分

## 1. 本讲目标

本讲解析 `numpy.polynomial.polynomial` 中一对互逆的函数式 API：`polyder`（逐项求导）与 `polyint`（逐项积分）。学完后你应当能够：

- 说清 `polyder` 为什么是「系数下移一位 + 乘原下标」的系数移位操作；
- 说清 `polyint` 如何逐项除以新下标，以及积分常数 `k`、下界 `lbnd` 的精确含义；
- 解释 `lbnd` 如何借助 `polyval` 回代，把不定积分「锚定」到指定点取指定值；
- 解释 `scl` 参数在变量替换 \(u=ax+b\) 下为何取 \(1/a\)，以及它如何通过链式法则把函数式 API 与便捷类的 `deriv`/`integ` 串联起来；
- 用 `axis` 参数对多维系数数组沿任意一维求导或积分。

## 2. 前置知识

本讲默认你已经掌握前置讲义中的以下概念，不再重复：

- **系数表示约定**：`c[i]` 是标准幂基 \(x^i\) 的系数，从低次到高次排列，故 `c=[1,2,3]` 表示 \(1+2x+3x^2\)（见 u1-l1）。
- **函数式 API 与薄委托**：`polyder`/`polyint` 是模块本体，便捷类的 `_der`/`_int` 通过 `_der = staticmethod(polyder)` 等绑定把活交给它们（见 u1-l4、u2-l1）。
- **Horner 求值**：`polyval(x, c)` 用秦九韶算法高效计算 \(p(x)\)（见 u3-l3），本讲里 `polyint` 会反向调用它。
- **domain/window 与线性映射**：便捷类携带 `domain`（用户坐标）与 `window`（系数参考坐标），`mapparms` 算出 \(u=\text{off}+\text{scl}\cdot x\) 把 domain 映到 window（见 u2-l2）。这一条是理解 `scl` 的关键。

一个朴素的微积分事实作为出发点：对幂函数逐项微分与积分有

\[
\frac{d}{dx}\bigl(c_i x^i\bigr)=i\,c_i\,x^{i-1},
\qquad
\int c_i x^i\,dx=\frac{c_i}{i+1}\,x^{i+1}+C.
\]

`polyder` 和 `polyint` 的全部实现，本质就是把这两条规则落到系数数组的下标操作上。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [polynomial.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 本讲主角。`polyder`（L466）与 `polyint`（L546）两个函数式 API 的全部实现都在这里；`polyint` 内部反向调用同文件的 `polyval`。 |
| [_polybase.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | 便捷类的 `deriv`（L878）与 `integ`（L845）方法，展示 `scl` 如何从 `mapparms` 取得并通过链式法则传入。 |
| polyutils.py | 提供 `pu._as_int`（把 `m`/`axis` 转成整数并校验）、`normalize_axis_index` 的底层支持（见 u3-l1），本讲只引用不展开。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：系数移位求导、积分常数 `k`、下界 `lbnd` 与 `polyval` 回代、`scl` 与变量替换、`axis` 与多维系数。

### 4.1 polyder：系数移位与阶乘乘法

#### 4.1.1 概念说明

对一个用标准幂基表示的多项式 \(p(x)=\sum_i c_i x^i\)，逐项求导后常数项 \(c_0\) 消失，每个 \(c_i\) 的系数「下移一位」并乘以它原来的下标 \(i\)：

\[
p'(x)=\sum_{i\ge 1} i\,c_i\,x^{i-1}.
\]

也就是说，新系数数组的第 \(j\) 个元素是 \((j+1)\,c_{j+1}\)。这就是 **系数移位**：所有系数整体左移一格，每个乘以移位前的下标。连求 `m` 次导，等价于把每个 \(c_i\) 乘以下降阶乘 \(i(i-1)\cdots(i-m+1)=i!/(i-m)!\)，并放到第 \(i-m\) 位：

\[
\frac{d^m}{dx^m}\sum_i c_i x^i=\sum_{i\ge m}\frac{i!}{(i-m)!}\,c_i\,x^{i-m}.
\]

#### 4.1.2 核心流程

`polyder(c, m=1, scl=1, axis=0)` 的执行过程（伪代码）：

1. 把输入规整为至少 1 维的数组；若 dtype 是整数类（含布尔），先 `+0.0` 提升为浮点。
2. 用 `pu._as_int` 校验 `m`、`axis` 为整数；`m<0` 抛 `ValueError`；`normalize_axis_index` 把 `axis` 规范到合法范围。
3. `m==0` 时直接原样返回（求 0 次导）。
4. `np.moveaxis(c, axis, 0)`：把要操作的「次数轴」搬到第 0 轴，循环只对着这一轴做下标运算，其余轴（多个多项式）原样保留。
5. 若 `m >= len(c)`：求导次数超过多项式长度，结果是零多项式 `[0]`。
6. 否则循环 `m` 次，每次：整体乘 `scl`；新建长度减 1 的数组 `der`，令 `der[j-1] = j*c[j]`（即系数移位）；用 `der` 替换 `c`。
7. `moveaxis` 把次数轴搬回原位，返回。

#### 4.1.3 源码精读

求导主循环（[polynomial.py:531-544](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L531-L544)）：这段先把次数轴搬到第 0 位，处理「求导超过次数」的退化情形，再做移位循环。

```python
c = np.moveaxis(c, iaxis, 0)
n = len(c)
if cnt >= n:
    c = c[:1] * 0          # 退化：结果为零多项式
else:
    for i in range(cnt):
        n = n - 1
        c *= scl            # 每次求导整体乘 scl（链式法则用）
        der = np.empty((n,) + c.shape[1:], dtype=cdt)
        for j in range(n, 0, -1):
            der[j - 1] = j * c[j]   # 系数移位：第 j 项乘 j，落到 j-1
        c = der
c = np.moveaxis(c, 0, iaxis)
return c
```

关键一行是 `der[j - 1] = j * c[j]`（[polynomial.py:540-541](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L540-L541)）：它一次性完成了「乘原下标」与「下移一位」两件事。注意循环里 `c[j]` 读的是**移位前**的数组（`c` 长度仍是 `n+1`），新数组 `der` 长度才是 `n`；`c[0]`（常数项）永远不被写入 `der`，自然消失。

前面的输入规整与校验（[polynomial.py:517-529](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L517-L529)）负责整数提升、`m`/`axis` 合法性、`m==0` 早退。

#### 4.1.4 代码实践

**实践目标**：亲手验证「系数移位」规律与退化分支。

操作步骤（以官方文档串中的例子为准）：

```python
from numpy.polynomial import polynomial as P
c = (1, 2, 3, 4)            # 1 + 2x + 3x^2 + 4x^3
P.polyder(c)                # 预期 array([ 2., 6., 12.])   即 2 + 6x + 12x^2
P.polyder(c, 3)             # 预期 array([24.])           即 24（= 3*4 *... 手算: 4x^3 求导3次=24）
P.polyder(c, 4)             # 预期 array([0.])            m>=len(c)，退化为零
```

需要观察的现象：

- `P.polyder(c)` 的结果恰好是原系数 `[2,3,4]` 逐个乘以下标 `[1,2,3]`，`1` 被丢弃。
- `P.polyder(c, 4)` 命中 `if cnt >= n` 分支，返回单个零。

预期结果如上（取自函数 docstring 的权威示例）。若你的运行结果不同，请确认 numpy 版本。

#### 4.1.5 小练习与答案

**Q1**：`P.polyder([5])` 返回什么？为什么？
**答**：返回 `array([0.])`。`[5]` 是常数 5，`len(c)=1`，`m=1 >= 1`，命中退化分支返回零多项式。

**Q2**：`c=[0,0,0,6]`（即 \(6x^3\)）求一次导，结果是？
**答**：`array([0., 0., 18.])`，即 \(18x^2\)。因为 `der[j-1]=j*c[j]` 中只有 `j=3` 非零：`der[2]=3*6=18`。

### 4.2 polyint：逐项积分与积分常数 k

#### 4.2.1 概念说明

逐项积分是求导的逆，但每积分一次会多出一个**自由常数**。对每个 \(c_i x^i\)：

\[
\int c_i x^i\,dx=\frac{c_i}{i+1}x^{i+1}+C.
\]

落到系数数组上：每个系数「上移一位」并除以**新**下标，常数项留给自由常数 \(C\)。连积分 `m` 次会引入 `m` 个常数，`polyint` 用参数 `k` 指定它们。

`k` 的精确语义（见 4.3 推导）：`k[i]` 是**第 i 次积分得到的不定积分在 `lbnd` 处的取值**。默认 `lbnd=0`，所以 `k[i]` 就是该不定积分的常数项（即在 \(x=0\) 处的值）。`k=[]`（默认）表示所有常数取 0；`m==1` 时 `k` 可直接给标量。

#### 4.2.2 核心流程

`polyint(c, m=1, k=[], lbnd=0, scl=1, axis=0)` 的执行过程：

1. 规整输入、整数提升为浮点；把非可迭代的 `k` 包成单元素列表。
2. 校验：`m>=0`、`len(k) <= m`、`lbnd` 与 `scl` 都是标量（`ndim==0`），否则抛 `ValueError`。
3. `m==0` 原样返回。
4. 把 `k` 用 0 补齐到长度 `m`（`k = list(k) + [0]*(m-len(k))`），保证后续每步都有对应常数。
5. `moveaxis` 把次数轴搬到第 0 位。
6. 循环 `m` 次，每次：整体乘 `scl`；构造长度加 1 的 `tmp`，令 `tmp[0]=0`、`tmp[1]=c[0]`、`tmp[j+1]=c[j]/(j+1)`（逐项积分）；再用 `tmp[0] += k[i] - polyval(lbnd, tmp)` 锚定常数（见 4.3）。
7. `moveaxis` 还原，返回。

#### 4.2.3 源码精读

`k` 补齐与逐项积分主循环（[polynomial.py:644-660](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L644-L660)）：

```python
k = list(k) + [0] * (cnt - len(k))   # 把常数补齐到 m 个
c = np.moveaxis(c, iaxis, 0)
for i in range(cnt):
    n = len(c)
    c *= scl                          # 每次积分整体乘 scl（变量替换用）
    if n == 1 and np.all(c[0] == 0):
        c[0] += k[i]                  # 零多项式积分 = 常数 k[i]
    else:
        tmp = np.empty((n + 1,) + c.shape[1:], dtype=cdt)
        tmp[0] = c[0] * 0
        tmp[1] = c[0]
        for j in range(1, n):
            tmp[j + 1] = c[j] / (j + 1)   # 逐项积分：除以新下标 j+1
        tmp[0] += k[i] - polyval(lbnd, tmp)  # 锚定常数（见 4.3）
        c = tmp
c = np.moveaxis(c, 0, iaxis)
return c
```

注意三处细节：

- `tmp[j + 1] = c[j] / (j + 1)`（[polynomial.py:655-656](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L655-L656)）就是逐项积分的除法；`tmp[1] = c[0]` 单独写是因为 `c[0]` 积分后落在下标 1，分母是 1，循环从 `j=1` 起跑。
- 整数提升为浮点（[polynomial.py:622-625](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L622-L625)）是必须的：积分要除以 `j+1`，整数相除会丢精度。
- 零多项式分支（[polynomial.py:649-650](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L649-L650)）保护 `∫0 dx = 常数`，直接把 `k[i]` 写进唯一那个系数。

参数校验（[polynomial.py:627-638](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L627-L638)）保证了「常数个数不超过积分次数」「`lbnd`/`scl` 必须是标量」这些前置条件。

#### 4.2.4 代码实践

**实践目标**：验证 `polyint` 默认行为与 `k` 的「常数项即 x=0 处取值」语义。

```python
from numpy.polynomial import polynomial as P
c = (1, 2, 3)
P.polyint(c)          # 预期 array([0., 1., 1., 1.])  即 x + x^2 + x^3
P.polyint(c, 3)       # 预期 array([0,0,0, 1/6, 1/12, 1/20])
P.polyint(c, k=3)     # 预期 array([3., 1., 1., 1.])  常数项 = k = 3
```

需要观察：`P.polyint(c)` 得到的 \([0,1,1,1]\) 求导回去应回到 `[1,2,3]`（见综合实践）。`P.polyint(c, k=3)` 的结果与默认结果只差常数项 `3`，印证 `k` 仅改变 `tmp[0]`。

预期结果取自 docstring 权威示例。运行环境不同时浮点尾数可能略有差异。

#### 4.2.5 小练习与答案

**Q1**：`P.polyint([0,0,6])`（即 \(6x^2\)）积分一次，默认参数，结果是？
**答**：`array([0., 0., 0., 2.])`，即 \(2x^3\)。按源码：`c=[0,0,6]`，`tmp[1]=c[0]=0`、`tmp[2]=c[1]/2=0`、`tmp[3]=c[2]/3=2`，常数项回代 `polyval(0,...)=0` 不变，故 `[0,0,0,2]`。求导回去 `polyder` 得 `[0,0,6]`，验证正确。

**Q2**：为什么 `polyint` 把整数 dtype 提升为浮点，而 `polyder` 也这么做（即使求导只用到乘法）？
**答**：为了 API 一致性，也为了避免整数溢出——`der[j-1]=j*c[j]` 在高次大系数时整数会溢出，统一用浮点更安全；同时也是 `polyint` 必须除法的自然要求。

### 4.3 lbnd 下界与 polyval 回代

#### 4.3.1 概念说明

不定积分带一个自由常数 \(C\)，单靠 `k` 还不够直观——`k` 是「在 `lbnd` 处的取值」，但读者更常想要「让积分在某个下界处为零」，也就是计算**定积分** \(\int_{\text{lbnd}}^{x} f(t)\,dt\)。`lbnd` 就是这个下界。

`polyint` 用一个巧妙的回代把 `k` 与 `lbnd` 统一：先把逐项积分得到的「零常数不定积分」放进 `tmp`，再用

\[
\texttt{tmp[0]} \mathrel{+}= k[i] - \text{polyval}(\text{lbnd},\ \text{tmp})
\]

调整常数项。这一行的效果是：调整后的多项式在 `lbnd` 处取值恰好等于 `k[i]`。证明：记调整前 `tmp` 在 `lbnd` 处的值为 \(V=\text{polyval}(\text{lbnd},\text{tmp})\)，调整只改了常数项 `tmp[0]`，故新多项式在 `lbnd` 处的值是 \(V + (k[i]-V)=k[i]\)。

#### 4.3.2 核心流程

- 默认 `k=[0]` 时，回代让第 i 次积分在 `lbnd` 处为 0，等价于「以 `lbnd` 为下界的定积分」。
- 默认 `lbnd=0` 时，`polyval(0, tmp)=tmp[0]`（调整前为 0），回代退化为 `tmp[0] += k[i]`，即 `k[i]` 直接成为常数项。

这两条合起来给出 `k[i]` 的精确含义：**第 i 次积分在 `lbnd` 处的取值**。

#### 4.3.3 源码精读

回代那一行（[polynomial.py:652-657](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L652-L657)）：

```python
tmp = np.empty((n + 1,) + c.shape[1:], dtype=cdt)
tmp[0] = c[0] * 0
tmp[1] = c[0]
for j in range(1, n):
    tmp[j + 1] = c[j] / (j + 1)
tmp[0] += k[i] - polyval(lbnd, tmp)   # 关键回代：锚定 lbnd 处取值为 k[i]
```

`polyval` 是同文件的 Horner 求值（见 u3-l3），此处被「反向」使用——不是用来求最终函数值，而是用来读出「当前不定积分在 `lbnd` 处的值」，从而算出需要补多少常数。

#### 4.3.4 代码实践（本讲核心练习）

**实践目标**：解释为何 `P.polyint(c, lbnd=-2)` 与 `P.polyint(c, k=6)` 对 `c=[1,2,3]` 给出相同结果。

```python
from numpy.polynomial import polynomial as P
c = (1, 2, 3)                      # 1 + 2x + 3x^2
P.polyint(c, lbnd=-2)              # 预期 array([6., 1., 1., 1.])
P.polyint(c, k=6)                  # 预期 array([6., 1., 1., 1.])  —— 两者相同！
```

手算解释（结合源码）：

1. 两种调用都先算出零常数不定积分 `tmp=[0,1,1,1]`（即 \(x+x^2+x^3\)，它正是 \(\frac{d}{dx}\) 的逆中常数项为 0 的那一个）。
2. `lbnd=-2, k=[0]`：回代 `tmp[0] += 0 - polyval(-2, [0,1,1,1])`。而 \(\text{polyval}(-2,[0,1,1,1])=0-2+4-8=-6\)，故 `tmp[0] += 6`，得 `[6,1,1,1]`。几何上这是 \(\int_{-2}^{x}(1+2t+3t^2)\,dt\)。
3. `k=6, lbnd=0`：回代 `tmp[0] += 6 - polyval(0, [0,1,1,1]) = 6 - 0 = 6`，也得 `[6,1,1,1]`。

**为什么相等**：`lbnd=-2` 强制积分在 \(x=-2\) 处为 0；`k=6` 强制积分在 \(x=0\) 处为 6。同一个不定积分 \([6,1,1,1]\) 同时满足这两条：它在 \(-2\) 处 \(=6-2+4-8=0\)，在 \(0\) 处 \(=6\)。本质原因是 \(\int_{-2}^{0}(1+2t+3t^2)\,dt=[t+t^2+t^3]_{-2}^{0}=0-(-6)=6\)——把下界平移到 0 恰好引入常数 6。`k` 与 `lbnd` 是同一自由常数的两种等价指定方式。

预期结果 `[6,1,1,1]` 取自 docstring（`lbnd=-2` 一例为权威示例，`k=6` 由源码回代推导得出）。**待本地验证**两者字节级相等。

#### 4.3.5 小练习与答案

**Q1**：若想得到「积分在 \(x=1\) 处为 5」的不定积分，该怎么调用？
**答**：`P.polyint(c, lbnd=1, k=5)`。因为 `k[i]` 就是积分在 `lbnd` 处的取值。

**Q2**：`P.polyint([1,2,3], lbnd=0, k=0)` 与 `P.polyint([1,2,3])` 结果是否相同？
**答**：相同。后者默认 `lbnd=0, k=[]→[0]`，回代 `tmp[0] += 0 - polyval(0,tmp)=0`，常数项保持 0。

### 4.4 scl 与变量替换：链式法则如何串联 deriv/integ

#### 4.4.1 概念说明

`scl` 不是缩放系数本身，而是「每次求导/积分后整体乘以 `scl`」，连做 `m` 次等于乘 \(scl^m\)。它的设计动机是**线性变量替换**。设 \(u=ax+b\)，则 \(du=a\,dx\)，即 \(dx=du/a\)，于是

\[
\int f(x)\,dx=\int f(x)\,\frac{du}{a},\qquad \frac{d}{dx}=\frac{du}{dx}\cdot\frac{d}{du}=a\cdot\frac{d}{du}.
\]

所以在积分侧，换元后整体要乘 \(1/a\)；这正是 docstring 强调的「 buyer beware：`scl` 常取成你直觉以为的倒数」。

#### 4.4.2 核心流程（便捷类如何用 scl）

便捷类的系数是按 **window 变量** \(u\) 表达的，而求值/拟合面向 **domain 变量** \(x\)，二者关系 \(u=\text{off}+\text{scl}\cdot x\)（`mapparms` 给出 `off`、`scl`）。由链式法则：

- 对 domain 变量求导：\(\frac{d}{dx}q(u)=q'(u)\cdot\text{scl}\)，故 `deriv` 把 `scl` 传给 `polyder`。
- 对 domain 变量积分：\(\int q(u)\,dx=\frac{1}{\text{scl}}\int q(u)\,du\)，故 `integ` 把 \(1/\text{scl}\) 传给 `polyint`。

口诀：**求导乘 scl，积分乘 1/scl**，正是链式法则的两面。

#### 4.4.3 源码精读

便捷类 `deriv`（[_polybase.py:878-898](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L878-L898)）传 `scl`：

```python
def deriv(self, m=1):
    off, scl = self.mapparms()
    coef = self._der(self.coef, m, scl)              # 求导乘 scl
    return self.__class__(coef, self.domain, self.window, self.symbol)
```

便捷类 `integ`（[_polybase.py:845-876](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L845-L876)）传 `1/scl`，并把用户给的 `lbnd`（domain 坐标）先映射成 window 坐标：

```python
def integ(self, m=1, k=[], lbnd=None):
    off, scl = self.mapparms()
    if lbnd is None:
        lbnd = 0
    else:
        lbnd = off + scl * lbnd        # domain 的 lbnd → window 坐标
    coef = self._int(self.coef, m, k, lbnd, 1. / scl)   # 积分乘 1/scl
    return self.__class__(coef, self.domain, self.window, self.symbol)
```

注意 `lbnd = off + scl*lbnd` 这一行：用户传入的 `lbnd` 是 domain 坐标，而 `polyint` 在 window 系数上运算，所以要先映射。

函数式侧的官方注释也讲清了这一点（[polynomial.py:597-603](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L597-L603)）：`u=ax+b` 时 `dx=du/a`，故 `scl` 应取 `1/a`。

#### 4.4.4 代码实践

**实践目标**：直观体会 `scl` 在求导/积分里的「乘法」角色。

```python
from numpy.polynomial import polynomial as P
c = (1, 2, 3, 4)
P.polyder(c, scl=-1)     # 预期 array([-2., -6., -12.])  即 d/d(-x)
P.polyder(c, 2, -1)      # 预期 array([6., 24.])        d^2/d(-x)^2
P.polyint(c, scl=-2)     # 预期 array([0., -2., -2., -2., -2.])
```

需要观察：`scl=-1` 等价于把自变量换成 \(-x\)（因为 \(d/d(-x)=-d/dx\)），结果逐项取反。`polyint(c, scl=-2)` 在逐项积分后又整体乘 \(-2\)。

预期结果取自 docstring 权威示例。

#### 4.4.5 小练习与答案

**Q1**：便捷类 `Polynomial([1,2,3], domain=[0,10])` 的 `deriv()`，传给 `polyder` 的 `scl` 是多少？
**答**：`mapparms(domain=[0,10], window=[-1,1])` 给出 `scl = (1-(-1))/(10-0) = 0.2`，故 `polyder` 收到 `scl=0.2`。

**Q2**：为什么 `integ` 传 \(1/\text{scl}\) 而不是 `scl`？
**答**：积分是求导的逆运算；求导乘 `scl`，逆运算就乘 \(1/\text{scl}\)。这也与 \(\int q(u)\,dx=(1/a)\int q\,du\) 一致。

### 4.5 axis 与多维系数

#### 4.5.1 概念说明

系数数组可以是多维的：第 0 轴是某变量的次数轴，其余轴枚举「一批多项式」或「其它变量」。例如 `[[1,2],[1,2]]` 当 `axis=0` 是 \(x\)、`axis=1` 是 \(y\) 时，表示 \(1+1x+2y+2xy\)。`polyder`/`polyint` 用 `axis` 指定沿哪一维做微积分，其余维原样保留。

#### 4.5.2 核心流程

两函数统一用 `np.moveaxis(c, axis, 0)` 把目标轴搬到第 0 位，循环只对 `len(c)`（即该轴长度）和 `c.shape[1:]`（其余轴）做运算，结束后 `moveaxis(c, 0, axis)` 搬回。这样「沿哪一维求导」就被规约成「永远对第 0 轴求导」。

#### 4.5.3 源码精读

搬轴发生在主循环两侧。求导侧（[polynomial.py:531](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L531) 与 [543](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L543)）：

```python
c = np.moveaxis(c, iaxis, 0)      # 目标轴 → 第 0 位
...
c = np.moveaxis(c, 0, iaxis)      # 搬回原位
```

新建数组时 `der = np.empty((n,) + c.shape[1:], ...)`（[polynomial.py:539](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L539)）保留了 `c.shape[1:]`，所以「一批多项式」被整体并行处理；`polyint` 的 `tmp = np.empty((n+1,) + c.shape[1:], ...)`（[polynomial.py:652](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L652)）同理。

#### 4.5.4 代码实践

**实践目标**：观察 `axis` 决定沿哪个变量求导。

```python
import numpy as np
from numpy.polynomial import polynomial as P
c2 = np.array([[1,2],[1,2]])     # 1 + 1*x + 2*y + 2*x*y
P.polyder(c2, axis=0)            # 对 x 求导：预期 [[1,2]]        (1 + 2y)
P.polyder(c2, axis=1)            # 对 y 求导：预期 [[2],[2]]      (2 + 2x)
```

需要观察：`axis=0` 时返回形状 `(1,2)`（x 的次数轴缩短 1），`axis=1` 时返回形状 `(2,1)`（y 的次数轴缩短 1）。手算：\(\partial/\partial x(1+x+2y+2xy)=1+2y\) 对应 `[[1,2]]`；\(\partial/\partial y=2+2x\) 对应 `[[2],[2]]`。

预期结果由源码搬轴逻辑与逐项规则推出，**待本地验证**。

#### 4.5.5 小练习与答案

**Q1**：`P.polyder([[1,2],[1,2]], axis=1, m=2)` 结果形状与数值？
**答**：y 方向长度为 2，求二阶导命中 `cnt>=n`（`2>=2`），返回形状 `(2,1)` 全零：`[[0],[0]]`。

**Q2**：为何 `polyder` 对 2-D 输入默认 `axis=0`？
**答**：按系数约定，第 0 轴是「主变量」的次数轴，默认沿它求导最符合一维多项式的直觉。

## 5. 综合实践

把本讲四条主线串起来：**「积分 ↔ 求导互逆」**、**`k`/`lbnd` 等价**、**`scl` 与链式法则**、**便捷类的 domain 映射**。

任务：对 `c=[1,2,3]`，完成下面四步并解释每一步。

```python
import numpy as np
from numpy.polynomial import polynomial as P

# 步骤 1：积分再求导，应回到原系数
ci = P.polyint(c)
back = P.polyder(ci)
assert np.allclose(back, c)          # 预期通过：polyder 是 polyint 的左逆

# 步骤 2：求导再积分，会差一个常数（自由常数 C）
cd = P.polyder(c)
ci2 = P.polyint(cd)                  # 常数项被补成 0
# 预期 ci2 != c，但 ci2 与 c 仅常数项不同；用 k 补回：
ci2_fix = P.polyint(cd, k=c[0])      # 把常数项强制为 c[0]=1

# 步骤 3：k 与 lbnd 等价
assert np.allclose(P.polyint(c, lbnd=-2), P.polyint(c, k=6))

# 步骤 4：便捷类的链式法则
p = np.polynomial.Polynomial(c, domain=[0,10], window=[-1,1])
print(p.mapparms())                  # (off, scl)，scl=0.2
print(p.deriv().coef)                # 等价于 polyder(c, scl=0.2)
print(p.integ().coef)                # 等价于 polyint(c, scl=1/0.2=5, lbnd=映射后)
```

需要观察与解释：

1. **步骤 1**：`polyder(polyint(c))` 回到 `c`，因为求导消掉了积分引入的自由常数。但 `polyint(polyder(c))` 不一定回到 `c`——求导丢掉了原常数项 `c[0]`，积分补的常数默认是 0，需用 `k=c[0]` 补回（步骤 2）。
2. **步骤 3**：印证 4.3 的结论，`lbnd=-2` 与 `k=6` 是同一常数的两种写法。
3. **步骤 4**：便捷类 `deriv`/`integ` 的系数，恰好是函数式 `polyder(c, scl=0.2)` / `polyint(c, scl=5)` 的结果——`scl` 来自 `mapparms`，体现链式法则。

若步骤 4 的系数对不上，请检查 `mapparms` 返回的 `off`/`scl`，以及 `integ` 内部把用户 `lbnd` 映射到 window 坐标这一步。

## 6. 本讲小结

- `polyder` 的本质是 **系数移位**：`der[j-1]=j*c[j]`，常数项消失；`m>=len(c)` 时退化为零多项式。
- `polyint` 的本质是 **逐项积分**：`tmp[j+1]=c[j]/(j+1)`，并引入自由常数；整数 dtype 必先提升为浮点。
- `k[i]` 的精确含义是「第 i 次积分在 `lbnd` 处的取值」；默认 `lbnd=0` 时 `k[i]` 即常数项。
- `lbnd` 通过 `tmp[0] += k[i] - polyval(lbnd, tmp)` 把不定积分「锚定」到指定点取指定值，反向复用了 Horner 求值 `polyval`。
- `lbnd=-2` 与 `k=6` 对 `c=[1,2,3]` 给出同一结果，因为它们是同一自由常数的等价指定。
- `scl` 是「每次微积分后整体乘的因子」；变量替换 \(u=ax+b\) 下积分取 `scl=1/a`。便捷类 `deriv` 传 `scl`、`integ` 传 `1/scl`，正是链式法则的两面。
- `axis` 靠 `moveaxis` 把目标次数轴搬到第 0 位再运算，从而支持多维系数（如二元多项式）沿任意变量求导/积分。

## 7. 下一步学习建议

- 下一讲 **u3-l5「Vandermonde 矩阵与最小二乘拟合」** 会把求值（`polyval`）与系数操作延伸到 `polyvander` 与 `polyfit`，理解了本讲的逐项规则后，拟合时的「列归一化」会更易接受。
- 若想从全局把握「系数操作 → 求值 → 拟合 → 求根」这条主链，建议回头对照 u3-l3（Horner 求值）与本讲，体会 `polyval` 在 `polyint`（回代）和将来 `polyfit`（构造矩阵）中反复出现的中枢地位。
- 进阶可阅读 `chebyshev.py` 的 `chebder`/`chebint`，对比它们用 z-series 卷积实现微积分的方式与本讲的逐项规则有何不同（见 u4-l1）。
