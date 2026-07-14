# 幂级数的创建与算术运算

## 1. 本讲目标

本讲深入 `numpy.polynomial.polynomial` 模块的「创建」与「算术」两组函数式 API，把它们和上一讲（u3-l1）讲过的 `polyutils` 通用工具联系起来。读完本讲你应当能够：

- 说清 `polyline`、`polyfromroots` 如何构造多项式，并理解 `pu._fromroots` 的分治（balanced pairing）乘法树。
- 掌握 `polyadd`/`polysub` 是对 `pu._add`/`pu._sub` 的薄委托，以及「按短数组对齐 + `trimseq` 收尾」的实现套路。
- 理解 `polymul` 用 `np.convolve`、`polymulx` 乘以 \(x\) 时的零多项式边界处理。
- 区分 `polydiv`（幂级数专用、原地长除法）与通用模板 `pu._div`（逐次消元、靠 `mul_f` 构造移位基），并能手算两者。
- 理解 `polypow` 对 `pu._pow` 的委托、`maxpower` 防护，以及它与 `_fromroots` 在「是否分治」上的区别。

## 2. 前置知识

本讲默认你已掌握以下内容（来自前置讲义）：

- **系数约定**：多项式用 1-D 数组表示，下标即次数，\(p(x)=c_0+c_1 x+\cdots+c_n x^n\)，从低次到高次（u1-l1）。
- **三层委托链**：便捷类（`_polybase.py`）→ 函数式 API（`polynomial.py` 的 `polyadd` 等）→ 通用工具（`polyutils.py` 的 `pu._add` 等）。命名规律为「前缀=基、后缀=功能」（u1-l4、u2-l1）。
- **polyutils 基石**：`as_series` 是输入总闸（转 1-D、去尾零、求公共 dtype 并返回副本），`trimseq` 是轻量结构去尾零（u3-l1）。

两个本讲会反复用到的术语：

- **首一多项式（monic polynomial）**：最高次项系数为 1 的多项式。\( (x-r_0)(x-r_1)\cdots(x-r_n) \) 展开后必然首一。
- **卷积与多项式乘法**：两个多项式 \(a(x)\)、\(b(x)\) 乘积的系数数组，正好是它们系数数组的离散卷积：\((a*b)_k=\sum_{i+j=k} a_i b_j\)。所以 `np.convolve(c1, c2)` 直接给出乘积系数。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| `polynomial.py` | 幂级数模块：函数式 API + `Polynomial` 便捷类 | `polyline`、`polyfromroots`、`polyadd`/`polysub`、`polymulx`/`polymul`、`polydiv`、`polypow` |
| `polyutils.py` | 全包共享工具 | `_fromroots`、`_add`/`_sub`、`_div`、`_pow`、`trimseq`、`as_series` |

一句话定位：`polynomial.py` 里的算术函数大多只是「门面」，真正的算法要么特化写在本地（`polymul`、`polydiv`），要么下沉到 `polyutils.py` 的通用下划线函数（`_add`、`_sub`、`_div`、`_pow`、`_fromroots`）。这正是 u1-l4 提炼的策略——「能复用就复用、有性能优势就特化」。

## 4. 核心概念与源码讲解

### 4.1 创建多项式：polyline 与 polyfromroots

#### 4.1.1 概念说明

构造幂级数最基本的两条路径：

- `polyline(off, scl)`：返回一次多项式 \(off + scl\cdot x\) 的系数，是最小的事件工厂。
- `polyfromroots(roots)`：给定一组根 \(r_0,\dots,r_n\)，返回首一多项式 \(\prod_i (x-r_i)\) 的系数。

两者关系紧密——`polyfromroots` 内部正是用 `polyline` 生成每一个一次因子 \((x-r_i)=\) `polyline(-r, 1)`。

#### 4.1.2 核心流程

`polyline` 的逻辑极简：

```text
若 scl != 0：返回 [off, scl]      # off + scl·x
否则        ：返回 [off]           # 退化为常数 off
```

`polyfromroots` 把「若干一次因子相乘」组织成一棵**平衡二叉乘法树**（分治），而不是从左到右逐个乘：

```text
roots 为空           → 返回 [1]（多项式 1）
否则：
    roots.sort()
    p = [ polyline(-r, 1) for r in roots ]   # 每个是一次因子 (x-r)
    while len(p) > 1:
        两两配对相乘，得到下一层 p
        若个数为奇数，把落单的那个并入下一层的首个
    返回 p[0]
```

为何要分治？从左到右连乘 \((((p_0 p_1)p_2)\cdots)\) 时，中间结果越来越长，却始终拿一个长多项式去乘一个二次因子；平衡配对则让乘法发生在「规模相近」的多项式之间，乘法树深度从 \(O(n)\) 降到 \(O(\log n)\)，对缓存更友好，对某些正交基还更稳。乘法总次数仍是 \(n-1\) 次，但组织方式更优。

#### 4.1.3 源码精读

`polyline`，注意斜率为 0 时退化为常数的特例：

[polynomial.py:114-149](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L114-L149) —— 当 `scl != 0` 返回 `[off, scl]`，否则返回 `[off]`，保证结果始终满足「无尾部零」的约定。

`polyfromroots` 只有一行实现，把活儿全交给通用工具：

[polynomial.py:152-213](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L152-L213) —— `return pu._fromroots(polyline, polymul, roots)`，把「构造一次因子」的 `polyline` 和「相乘」的 `polymul` 作为函数参数注入。

真正的分治算法在 `polyutils._fromroots`：

[polyutils.py:443-470](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L443-L470) —— 关键三步：空根返回 `np.ones(1)`（L456-457）；用 `line_f(-r, 1)` 生成所有一次因子（L461）；`while n > 1` 循环里 `divmod(n, 2)` 配对相乘，奇数时把 `p[-1]` 并入 `tmp[0]`（L463-469）。注意它接收的是 `line_f`、`mul_f` 两个**函数**，所以同一份代码可服务于六大基——这正是「通用工具 + 注入基函数」的设计。

#### 4.1.4 代码实践

1. **目标**：验证 `polyfromroots` 的展开结果，并理解分治过程。
2. **操作步骤**（示例代码）：

```python
import numpy as np
from numpy.polynomial import polynomial as P

c = P.polyfromroots([1, 2, 3])
print(c)            # 期望 [-6., 11., -6., 1.]  即 x^3 - 6x^2 + 11x - 6
print(P.polyval(1, c), P.polyval(2, c), P.polyval(3, c))  # 三个根处应都为 0
```

3. **需要观察的现象**：系数数组为 `[-6., 11., -6., 1.]`，首项（最高次）系数为 `1.`（首一）；在 \(x=1,2,3\) 处求值都接近 `0.0`。
4. **预期结果**：输出近似 `[-6. 11. -6. 1.]` 与三个 `0.0`（可能有极小舍入误差）。
5. 若你的环境无法运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习 1**：`P.polyfromroots([])`（空根）返回什么？为什么？
  - **答案**：返回 `array([1.])`，即多项式 \(1\)。因为零个因子的连乘积是空积（empty product），按约定等于 1。对应源码 L456-457。
- **练习 2**：`P.polyline(5, 0)` 返回什么？它代表什么多项式？
  - **答案**：返回 `array([5])`，代表常数多项式 \(5\)。斜率为 0 时退化为常数，避免产生多余的尾部零。

---

### 4.2 加法与减法：polyadd/polysub 与 pu._add/pu._sub

#### 4.2.1 概念说明

多项式加减是逐项进行的：\( (a\pm b)_i = a_i \pm b_i \)。难点只是两个系数数组长度可能不同，需要对齐。`polynomial.py` 的 `polyadd`/`polysub` 不写算法，直接委托 `pu._add`/`pu._sub`。

#### 4.2.2 核心流程

`_add` 的套路（`_sub` 同构）：

```text
[c1, c2] = as_series([c1, c2])      # 规整 + 去尾零 + 公共 dtype + 副本
若 len(c1) > len(c2)：
    c1 的前 len(c2) 个元素 += c2；返回 trimseq(c1)
否则：
    c2 的前 len(c1) 个元素 += c1；返回 trimseq(c2)
```

要点：**以较长数组为容器**，把较短数组加到它的前缀上，避免重新分配；最后 `trimseq` 去掉可能产生的尾部零（例如 \(x + (-x) = 0\)）。

#### 4.2.3 源码精读

`polyadd`/`polysub` 的函数体各只有一行委托：

[polynomial.py:216-249](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L216-L249) —— `polyadd` 在 L249 `return pu._add(c1, c2)`。

[polynomial.py:252-286](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L252-L286) —— `polysub` 在 L286 `return pu._sub(c1, c2)`。

通用实现 `_add`：

[polyutils.py:555-565](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L555-L565) —— 先 `as_series` 取得规整副本（L558），再用「长数组当前缀容器」策略（L559-564），最后 `trimseq(ret)`（L565）。

`_sub` 的差别在于「较短或等长」分支里先把 `c2` 取负再相加：

[polyutils.py:568-579](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L568-L579) —— L576 `c2 = -c2` 后 `c2[:c1.size] += c1`，等价于 `c1 - c2`，复用了与 `_add` 相同的容器策略。

#### 4.2.4 代码实践

1. **目标**：观察「对齐 + trim 收尾」，特别是相加后抵消出尾部零的情况。
2. **操作步骤**：

```python
from numpy.polynomial import polynomial as P

print(P.polyadd([1, 2, 3], [3, 2, 1]))   # [4., 4., 4.]
print(P.polyadd([1, 2], [-1, -2]))       # [0.]  ← 1+2x 减自身，trim 后只剩 [0.]
print(P.polysub([1, 2, 3], [1, 2, 3]))   # [0.]
```

3. **现象**：第二个例子若不做 `trimseq` 会得到 `[0., 0., 0.]`；这里被规整成 `[0.]`，符合「零多项式的规范表示」。
4. **预期结果**：`[4. 4. 4.]`、`[0.]`、`[0.]`。
5. 无法运行则标注「待本地验证」。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `_add` 要选较长数组做容器，而不是 `np.zeros(max(len))` 新建数组？
  - **答案**：避免一次额外分配与拷贝。把短数组「原地」加到长数组前缀上，只产生一次 `as_series` 的副本，再 `trimseq`（可能返回视图），更高效。
- **练习 2**：`P.polyadd([1, 2, 3], [0, 0, 0])` 的结果长度是多少？
  - **答案**：结果是 `[1., 2., 3.]`。因为 `as_series` 默认 `trim=True` 会先把 `[0,0,0]` 去成 `[0]`，再加到 `[1,2,3]` 上得到 `[1,2,3]`，`trimseq` 后不变。

---

### 4.3 乘法：polymul（卷积）与 polymulx（乘以 x 的边界处理）

#### 4.3.1 概念说明

多项式乘法在系数层面就是卷积。`polymul` 直接用 `np.convolve`，这是幂级数的**特化**实现（不走某个通用 `_mul`，因为 NumPy 的卷积已经足够快且通用）。`polymulx(c)` 是「乘以 \(x\)」的特化：把系数整体右移一位、最低位补 0，即 \(x\cdot c(x)\)。

#### 4.3.2 核心流程

`polymul`：

```text
[c1, c2] = as_series([c1, c2])
ret = np.convolve(c1, c2)
return trimseq(ret)         # 卷积结果最高项可能为 0，需收尾
```

`polymulx`（乘以 \(x\)）：

```text
[c] = as_series([c])
若 c 是零多项式（len==1 且 c[0]==0）：直接返回 c   # 边界保护
否则：prd = empty(len(c)+1); prd[0] = c[0]*0; prd[1:] = c; 返回 prd
```

#### 4.3.3 源码精读

`polymul` 用卷积实现乘法：

[polynomial.py:331-366](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L331-L366) —— L364-365 先 `as_series` 再 `np.convolve(c1, c2)`，L366 `trimseq(ret)` 收尾。

`polymulx` 的零多项式边界处理是本节重点：

[polynomial.py:289-328](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L289-L328) —— L322-323 判断零多项式直接返回；L325-327 正常分支 `prd[0] = c[0]*0`（用 `c[0]*0` 而非字面量 `0` 是为了保持 dtype，兼容 `Decimal` 等对象类型），`prd[1:] = c` 实现整体右移。

为何要专门保护零多项式？若对 `[0]` 走正常分支，会得到 `[0, 0]`——一个带尾部零的「长」零表示，违反去尾零约定，可能干扰次数推断。守卫保证结果仍是规范的 `[0]`。

#### 4.3.4 代码实践

1. **目标**：验证卷积乘法与 `polymulx` 的右移行为及零多项式保护。
2. **操作步骤**：

```python
import numpy as np
from numpy.polynomial import polynomial as P

print(P.polymul([1, 2, 3], [3, 2, 1]))   # [3., 8., 14., 8., 3.]
print(P.polymulx([1, 2, 3]))             # [0., 1., 2., 3.]  ← x(1+2x+3x^2)
print(P.polymulx([0]))                   # [0]  ← 零多项式保护，而非 [0, 0]

# 手算校验：(1+2x+3x^2)(3+2x+x^2)
# = 3 + (2+6)x + (1+4+9)x^2 + (2+6)x^3 + 3x^4 = 3+8x+14x^2+8x^3+3x^4
```

3. **现象**：`polymul` 结果与手算一致；`polymulx([0])` 长度为 1 而非 2。
4. **预期结果**：`[3. 8. 14. 8. 3.]`、`[0. 1. 2. 3.]`、`[0]`（或 `array([0])`）。
5. 无法运行则标注「待本地验证」。

#### 4.3.5 小练习与答案

- **练习 1**：用 `np.convolve` 手动复现 `P.polymul([1,1],[1,1])`，结果代表什么？
  - **答案**：`np.convolve([1,1],[1,1]) = [1,2,1]`，即 \((1+x)^2 = 1+2x+x^2\)。
- **练习 2**：`P.polymulx([5])` 返回什么？为什么不触发零多项式守卫？
  - **答案**：返回 `[0., 5.]`，代表 \(5x\)。因为 `c=[5]` 虽然 `len==1` 但 `c[0]==5 != 0`，不满足零多项式条件，走正常右移分支。

---

### 4.4 除法：polydiv 的特化长除法 与 pu._div 通用模板

#### 4.4.1 概念说明

多项式带余除法：\(c_1 = q\cdot c_2 + r\)，\(\deg r < \deg c_2\)。本模块有**两套**实现，理解它们的差异是本节核心：

- `pu._div(mul_f, c1, c2)`（通用模板）：通过反复「拿当前余数的最高项去除以除式最高项」求商，每次用 `mul_f` 显式构造移位基 \([0,\dots,0,1]\cdot c_2\) 再相减。因为接收 `mul_f`，所以同一份代码服务六大基。
- `polydiv`（幂级数特化）：用下标算术**原地**完成同样的消元，省去反复构造数组与反复调用 `polymul`。源码注释直言它「比 `pu._div(polymul, c1, c2)` 更高效」。

这正是 u1-l4「能复用就复用、有性能优势就特化」的活样本——幂级数因为基就是 \(x^i\)，可以用下标直接表达移位，从而特化。

#### 4.4.2 核心流程

通用 `_div` 的逐次消元（从高次到低次）：

```text
[c1, c2] = as_series([c1, c2])
若 c2 最高项为 0：抛 ZeroDivisionError
若 len(c1) < len(c2)：商 = 0，余 = c1
若 len(c2) == 1（除式为常数）：整体除以该常数，余 = 0
否则对 i = (lc1-lc2) ... 0 倒序：
    p  = mul_f([0]*i + [1], c2)     # c2 左移 i 位（乘以 x^i）
    q  = rem[-1] / p[-1]            # 当前最高项系数之比 = 商的第 i 次项
    rem = rem[:-1] - q * p[:-1]     # 消去最高次
    quo[i] = q
返回 quo, trimseq(rem)
```

特化 `polydiv` 等价但更省：先把 `c2` 去掉最高项并除以最高项系数（归一化），然后用 `c1[i:j] -= c2 * c1[j]` 在 `c1` 上原地消元，最后一次性切出商与余。

#### 4.4.3 源码精读

`polydiv` 特化实现，注释点明它比通用版更高效：

[polynomial.py:369-424](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L369-L424) —— L407 注释 `note: this is more efficient than pu._div(polymul, c1, c2)`；L415-417 先求 `dlen` 与归一化除式 `c2 = c2[:-1]/scl`；L418-423 `while i >= 0` 原地消元 `c1[i:j] -= c2 * c1[j]`；L424 切出商 `c1[j+1:]/scl` 与余 `trimseq(c1[:j+1])`。

通用模板 `_div`，靠 `mul_f` 构造移位基：

[polyutils.py:519-552](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L519-L552) —— L548 `p = mul_f([0] * i + [1], c2)` 即「\(c_2\) 乘以 \(x^i\)」；L549 `q = rem[-1] / p[-1]`；L550 `rem = rem[:-1] - q * p[:-1]` 消去最高次。注意 `_div` 第一参数是 `mul_f`，这是它能复用于六大基的关键。

#### 4.4.4 代码实践

1. **目标**：用 `polydiv` 验证 \((x-1)(x-2)(x-3)\div(x-1)\)，并手算对照 `_div` 的逐次消元。
2. **操作步骤**：

```python
from numpy.polynomial import polynomial as P

c   = P.polyfromroots([1, 2, 3])   # [-6, 11, -6, 1]
fac = P.polyline(-1, 1)            # [-1, 1]  即 (x-1)
quo, rem = P.polydiv(c, fac)
print(quo, rem)                    # 期望 [6., -5., 1.] 与 [0.]
```

3. **手算 `_div` 的逐次消元**（取 `mul_f = np.convolve`，\(c_1=[-6,11,-6,1]\)，\(c_2=[-1,1]\)，`lc1=4, lc2=2`）：

   ```text
   初值 rem = [-6, 11, -6, 1], quo = [·, ·, ·]
   i=2: p = conv([0,0,1],[-1,1]) = [0,0,-1,1]; q = rem[-1]/p[-1] = 1/1 = 1
        rem = [-6,11,-6] - 1·[0,0,-1] = [-6,11,-5]; quo[2]=1
   i=1: p = conv([0,1],[-1,1]) = [0,-1,1]; q = -5/1 = -5
        rem = [-6,11] - (-5)·[0,-1] = [-6,6]; quo[1]=-5
   i=0: p = conv([1],[-1,1]) = [-1,1]; q = 6/1 = 6
        rem = [-6] - 6·[-1] = [0]; quo[0]=6
   结果 quo=[6,-5,1], rem=[0]
   ```

   商 \([6,-5,1]\) 正是 \((x-2)(x-3)=x^2-5x+6\)，余为 \(0\)。
4. **现象与预期**：`polydiv` 与手算 `_div` 结果一致；商的最高项系数为 `1.`（首一），余数为 `[0.]`。
5. 无法运行则标注「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：`P.polydiv([2, 4, 6], [2])` 走的是 `_div`/`polydiv` 的哪条分支？结果是什么？
  - **答案**：除式 `len==1`（常数分支），整体除以 `2`，商 `[1., 2., 3.]`、余 `[0.]`。
- **练习 2**：为何 `polydiv` 比 `pu._div(polymul, c1, c2)` 更高效？
  - **答案**：`_div` 每轮都要 `mul_f([0]*i+[1], c2)` 显式构造一个全长数组再相减，反复分配；`polydiv` 把除式预先归一化、去掉最高项，用 `c1[i:j] -= c2*c1[j]` 原地消元，省去了反复构造移位数组与反复调用 `polymul`（含其 `as_series`）。

---

### 4.5 求幂：polypow 与 pu._pow

#### 4.5.1 概念说明

`polypow(c, pow, maxpower=None)` 计算 \(c(x)^{pow}\)。和 `polydiv` 一样，`polypow` 也是一行委托 `pu._pow(np.convolve, c, pow, maxpower)`——但注意它注入的是 `np.convolve` 而**非** `polymul`，源码注释解释：这样可以避免在循环里反复调用 `as_series`。

需要澄清一个常见误解：`_pow` 用的是**简单的迭代连乘**（`prd = prd * c`，重复 `pow-1` 次），并不是「分治/快速幂」。源码注释甚至写道「This can be made more efficient by using powers of two in the usual way」——即它本可以用平方求幂优化，但目前没有。真正用到分治的是 4.1 节的 `_fromroots`。

#### 4.5.2 核心流程

```text
[c] = as_series([c])
power = int(pow)
若 power != pow 或 power < 0：抛 ValueError "Power must be a non-negative integer"
若 maxpower is not None 且 power > maxpower：抛 ValueError "Power is too large"
若 power == 0：返回 [1]            # c^0 = 1
若 power == 1：返回 c              # c^1 = c
否则：prd = c; 重复 power-1 次：prd = mul_f(prd, c); 返回 prd
```

关于 `maxpower` 的两层含义（承接 u2-l3）：

- 函数式 `polypow` 的默认 `maxpower=None`，即**不限制**（注意：其 docstring 写的「Default is 16」与代码实际默认 `None` 不一致，以代码为准）。
- 便捷类 `Polynomial.__pow__` 会把类属性 `maxpower=100` 传入，作为防失控安全阀——它限制的是「指数」`pow`，不是结果多项式的「次数」。

#### 4.5.3 源码精读

`polypow` 注入 `np.convolve` 以避免循环内反复 `as_series`：

[polynomial.py:427-463](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L427-L463) —— L461-463 注释「this is more efficient than `pu._pow(polymul, c1, c2)`, as it avoids calling `as_series` repeatedly」后 `return pu._pow(np.convolve, c, pow, maxpower)`。

通用实现 `_pow`：

[polyutils.py:670-700](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L670-L700) —— L685-689 三道校验（整数性、非负、`maxpower` 上限）；L690-693 `power==0` 返回 `[1]`、`power==1` 返回 `c`；L694-700 迭代连乘，L695 注释自承「可用 2 的幂次优化」。

#### 4.5.4 代码实践

1. **目标**：观察 `polypow` 的结果与「迭代连乘」语义，并验证 `maxpower` 只在显式传入时生效。
2. **操作步骤**：

```python
from numpy.polynomial import polynomial as P

print(P.polypow([1, 2, 3], 2))              # [1., 4., 10., 12., 9.]
# 手算：(1+2x+3x^2)^2 = 1 + 4x + (4+6)x^2... 逐项卷积自乘一次

# 函数式默认不限制指数：
print(P.polypow([1, 1], 200))               # 能正常算出长度 201 的结果

# 显式传 maxpower 才会触发上限：
try:
    P.polypow([1, 1], 5, maxpower=3)
except ValueError as e:
    print("ValueError:", e)                 # Power is too large
```

3. **现象**：`polypow([1,2,3], 2)` 与「自乘一次」一致；默认 `maxpower=None` 时 `pow=200` 也能算；显式 `maxpower=3` 对 `pow=5` 抛 `ValueError`。
4. **预期结果**：`[1. 4. 10. 12. 9.]`；长数组；`ValueError: Power is too large`。
5. 无法运行则标注「待本地验证」。

#### 4.5.5 小练习与答案

- **练习 1**：`P.polypow([1, 2, 3], 0)` 和 `P.polypow([1, 2, 3], 1)` 分别返回什么？
  - **答案**：`array([1.])`（任何多项式的 0 次幂为 1）与 `array([1., 2., 3.])`（1 次幂为自身，且因 `as_series` 返回副本，原数组不受影响）。
- **练习 2**：为何 `polypow` 注入 `np.convolve` 而非 `polymul`？
  - **答案**：`polymul` 内部会调用 `as_series` 做规整。`_pow` 的循环要连乘 `pow-1` 次，每次都走 `as_series` 是重复开销；直接用 `np.convolve` 省掉这些规整（输入在进入 `_pow` 前已由 `as_series` 规整过一次）。

## 5. 综合实践

把本讲的「创建 + 加减乘除 + 求幂」串起来，完成下面这个端到端小任务：

**任务**：用函数式 API 验证多项式恒等式 \((x-1)(x-2)(x-3) = (x-1)\cdot\bigl((x-2)(x-3)\bigr)\)，并沿途对照 `_fromroots`/`_div`/`_pow` 的行为。

```python
import numpy as np
from numpy.polynomial import polynomial as P

# 1) 创建：从根构造完整多项式
c123 = P.polyfromroots([1, 2, 3])          # [-6, 11, -6, 1]

# 2) 除法：除以 (x-1)，应回到 (x-2)(x-3)
fac = P.polyline(-1, 1)                    # [-1, 1]
quo, rem = P.polydiv(c123, fac)            # quo 期望 [6, -5, 1], rem 期望 [0]

# 3) 乘法：用商与 (x-1) 重新相乘，应还原 c123
reconstructed = P.polymul(quo, fac)        # 期望 [-6, 11, -6, 1]
reconstructed = P.polytrim(reconstructed)  # 规整（去数值噪声尾部零）

# 4) 求幂：((x-1)(x-2)(x-3))^2 与 c123 自乘一致
sq_pow = P.polypow(c123, 2)
sq_mul = P.polymul(c123, c123)

# 5) 求值验证：在 x=1,2,3 处 c123 为 0，平方亦为 0
checks = [P.polyval(r, c123) for r in (1, 2, 3)]

print("c123        =", c123)
print("quo, rem    =", quo, rem)
print("reconstructed=", reconstructed)
print("pow==mul    ?", np.allclose(sq_pow, sq_mul))
print("roots check =", checks)
```

**预期**：`quo = [6,-5,1]`、`rem = [0]`、`reconstructed` 与 `c123` 数值一致、`pow==mul` 为 `True`、`roots check` 三个值都接近 `0.0`。

**进阶思考**（可选）：

- 把 `P.polydiv(c123, fac)` 的 `_div` 版本在草稿纸上手算一遍（参考 4.4.4），确认它给出同样的 `quo/rem`，体会「特化 vs 通用」只是同一数学过程的两种实现。
- 试把 `fac` 换成 `P.polyfromroots([2])`，验证除以 \((x-2)\) 得到 \((x-1)(x-3)=x^2-4x+3\to[3,-4,1]\)。

> 若运行环境不可用，本任务可改为纯阅读型：依据 4.4.4 的手算过程，在纸上推出 `P.polydiv([-6,11,-6,1], [-2,1])` 的商与余。

## 6. 本讲小结

- `polyfromroots` 通过 `pu._fromroots(polyline, polymul, roots)` 构造首一多项式；`_fromroots` 用**平衡配对分治**组织乘法树，深度 \(O(\log n)\)。
- `polyadd`/`polysub` 是 `pu._add`/`pu._sub` 的薄委托，套路是「`as_series` 规整 → 以长数组为容器对齐 → `trimseq` 收尾」。
- `polymul` 特化为 `np.convolve`；`polymulx` 乘以 \(x\) 时对零多项式做边界保护，避免产生带尾部零的 `[0,0]`。
- 除法有两套实现：通用模板 `_div`（靠 `mul_f` 构造移位基、逐次消元，服务六大基）与幂级数特化 `polydiv`（归一化除式 + 下标原地消元，更高效），二者数学等价。
- `polypow` 注入 `np.convolve` 委托给 `_pow`；`_pow` 是**迭代连乘**（非快速幂，源码注释自承可优化），函数式默认 `maxpower=None` 不限指数，便捷类的 `maxpower=100` 才是安全阀。
- 通篇印证了 u1-l4 的设计哲学：「能复用就复用（`_add`/`_sub`/`_div`/`_pow`/`_fromroots` 通用模板），有性能优势就特化（`polymul`/`polydiv`/`polypow` 注入卷积）」。

## 7. 下一步学习建议

- **下一讲 u3-l3（多项式求值与 Horner 法）** 将讲 `polyval` 的 Horner 展开与 `tensor` 广播，与本讲的「创建/算术」直接衔接——你可以先把本讲构造的多项式喂给 `polyval` 求值。
- 若对「为何 `polydiv` 要特化」想看更工程化的视角，可跳读 u5-l1（数值稳定性与架构取舍）。
- 想横向对比六大基的同名函数（如 `chebdiv`/`legdiv` 是否也走 `_div`），可预习 u4 单元的正交多项式族；届时你会发现 `chebyshev.py` 里 `chebdiv` 没有特化、直接 `return pu._div(chebmul, c1, c2)`，与本讲的 `polydiv` 形成对照。
