# 乘法/取反/绝对值/移位结果格式与 union

## 1. 本讲目标

上一讲（u3-l1）我们学会了用 `for_add` / `for_sub` / `for_addsub` 推导加减法的「全精度中间格式」mid_fmt。本讲把同样的思想推广到其余几类运算的结果格式推导：

- `for_mult`：两个定点数相乘后，结果需要多少位？这里藏着一个有趣的「1 位有符号特例」。
- `for_neg` / `for_abs`：取反与绝对值的结果格式，关键是补码不对称带来的整数位增长。
- `for_shift` / `for_round`：移位与舍入的结果格式——前者无损地搬动小数点，后者因进位可能多一位。
- `union`：把若干格式「合并」成能同时容纳它们的格式，是 `for_addsub`、`for_abs`、`cl_fix_compare` 共用的底层工具。

学完本讲，你应能：

1. 手算任意两个格式相乘后的 mid_fmt，并能解释何时整数位 \(+1\)、\(-1\) 或不变。
2. 说出取反一个有符号数为什么常常多一位整数，以及 1 位无符号的特例。
3. 推导移位与舍入的结果格式，理解「舍入可能进位」这一事实如何体现为 \(+1\) 整数位。
4. 把 `union` 当作格式级别的「取最大值」工具，看懂它在多个函数里的复用。

## 2. 前置知识

本讲承接 u3-l1，复用以下已建立的概念，不再重复推导细节：

- **mid_fmt（全精度中间格式）**：所有算术函数都遵循「先算 mid_fmt → 在其中无损运算 → 最后 resize 到目标 r_fmt」的三段式。mid_fmt 同时满足「够用」（任何合法输入都不溢出）与「不浪费」（整数位已最小）。
- **保守推导**：只看格式的取值范围、按最坏情况算，不看具体数值。代码注释原话：*"This is a conservative calculation (it assumes that a and b may take any values)."* 因此若输入值受限，更窄的格式或许可行，但库不会替你假设。
- **rmax / rmin 双极值**：加减法用 `rmax=amax+bmax`、`rmin=amin+bmin` 这对真实极值决定整数位增长。本讲的乘法沿用同一思路，只是乘积的极值组合更复杂。
- **[S, I, F] 与位权**：\(S\in\{0,1\}\) 为符号位，\(I\) 整数位，\(F\) 小数位，总位宽 \(S+I+F\)；有符号数符号位权重为 \(-2^{I}\)（详见 u1-l2）。
- **union 的直觉**：u3-l1 里 `for_addsub = union(for_add, for_sub)` 已经用到它——对 S/I/F 三字段分别取 max。本讲 4.4 节会展开它的实现与所有复用点。

一个贯穿全讲的「心法」：**格式推导只关心范围，不关心数值**。理解了这一点，后面所有看似复杂的分支都会变得自然。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py) | Python 参考模型。`FixFormat` 类的静态方法 `for_mult`/`for_neg`/`for_abs`/`for_shift`/`for_round`/`union` 全在这里，注释里写满了数学推导。本讲的主要精读对象。 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | 可综合 VHDL 包。`cl_fix_mult_fmt`/`cl_fix_neg_fmt`/`cl_fix_abs_fmt`/`cl_fix_shift_fmt`/`cl_fix_round_fmt` 与内部 `union` 是 Python 的逐分支镜像，用于硬件侧编译期求值。 |
| [bittrue/tests/python/format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) | 穷举对拍测试：对每个 `for_*` 函数，用真实极值验证结果格式「够用且不浪费」。本讲实践会借用它的思路。 |

> 三语言 bit-true：Python 与 VHDL 的格式推导函数同名同义，经 `format_tests.py` 穷举（\(S\in\{0,1\}\)、\(I,F\in[-6,6]\) 全组合）逐分支对拍，二者必然给出一致结果。本讲引用代码时两种语言对照着看即可。

## 4. 核心概念与源码讲解

### 4.1 for_mult：乘法结果格式与 1-bit signed 特例

#### 4.1.1 概念说明

两个定点数相乘，结果格式有三件事要定：符号位 \(S\)、整数位 \(I\)、小数位 \(F\)。

- **小数位最简单**：两个数分别有 \(a.F\) 和 \(b.F\) 位小数，相乘后小数位直接相加，\(F = a.F + b.F\)。直觉：\(2^{-a.F} \times 2^{-b.F} = 2^{-(a.F+b.F)}\)，乘积的最小粒度是两个粒度之积。
- **整数位最微妙**：朴素地想，\(a.I+b.I\) 位整数「差不多够」，但符号组合与边界值会让它 \(+1\)、\(-1\) 或不变。这是本节的核心。
- **符号位也有特例**：通常「任一输入有符号则结果有符号」，但两个 **1 位有符号数**（即宽度为 1、\(S=1\)）相乘，结果恒非负，反而变成无符号。

`for_mult` 的设计目标依旧是 mid_fmt 的两条标尺：**够用**（覆盖最坏乘积）且**不浪费**（整数位最少）。

#### 4.1.2 核心流程

`for_mult(a_fmt, b_fmt)` 的推导分三步，分别决定 \(I\) 的 rmax 侧、rmin 侧，以及 \(S\)：

1. **rmax（正向极值）决定整数位上限**。按符号组合分两种：
   - 两边都有符号：最大正积来自两个最负值相乘，\(r_{\max}=a_{\min}\cdot b_{\min}=(-2^{a.I})(-2^{b.I})=2^{a.I+b.I}\)，正好是 2 的幂，需要 \(a.I+b.I+1\) 位整数（因为有符号下表示 \(2^{n}\) 需要 \(n+1\) 位整数）。
   - 否则：\(r_{\max}=a_{\max}\cdot b_{\max}\)，通常需要 \(a.I+b.I\) 位整数；但当一个数「很小」（\(x=a.I+a.F\le 1\) 或 \(y=b.I+b.F\le 1\)）时，乘积够不到 \(2^{a.I+b.I-1}\)，反而能省 1 位，即 \(a.I+b.I-1\)。
2. **rmin（负向极值）决定是否还需要更多整数位**。只有「一个有符号、一个无符号」时负积才可能比 rmax 更苛刻：此时 \(r_{\min}=a_{\max}\cdot b_{\min}\)（或对称），所需的整数位正好等于 `for_neg(无符号那一边).I + 另一边.I`，取它与 rmaxI 的较大值。两边同号（全无符号或全有符号）时 rmin 不会超过 rmaxI，直接忽略。
3. **符号位**：若两输入都是「1 位有符号」，结果是 \(\{-1,0,1\}\) 的乘积即 \(\{0,1\}\)，非负 → \(S=0\)；否则 \(S=\max(a.S, b.S)\)。

用一个心智模型概括整数位的 ±1 调整：

\[
I_{\text{result}} = \max\bigl(\,r_{\max}\text{ 侧需求},\ \ r_{\min}\text{ 侧需求}\,\bigr),\qquad F_{\text{result}} = a.F + b.F
\]

其中 rmax 侧需求由上面的符号分支给出（\(a.I+b.I+1\) / \(a.I+b.I-1\) / \(a.I+b.I\)）。

#### 4.1.3 源码精读

Python 实现是一个静态方法，注释里完整记录了上述推导：

[en_cl_fix_types.py:201-269](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L201-L269) — `for_mult` 全函数：先算 rmax 侧整数位，再算 rmin 侧，最后定符号位与小数位。

rmax 侧的三分支，对应「两有符号 +1 / 小格式 −1 / 一般不变」：

```python
# en_cl_fix_types.py:230-235
if a_fmt.S == 1 and b_fmt.S == 1:
    rmaxI = a_fmt.I + b_fmt.I + 1
elif a_fmt.I+a_fmt.F <= 1 or b_fmt.I+b_fmt.F <= 1:
    rmaxI = a_fmt.I + b_fmt.I - 1
else:
    rmaxI = a_fmt.I + b_fmt.I
```

注释 [en_cl_fix_types.py:223-229](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L223-L229) 给出了「−1 位」条件的化简过程：令 \(x=a.I+a.F\)、\(y=b.I+b.F\)，不等式最终归约为 \((2^{x}-2)(2^{y}-2)<2\)，在 \(x,y\ge 0\) 下当且仅当 \(x\le 1\) 或 \(y\le 1\) 成立——这正是上面 `elif` 的判据。

rmin 侧复用 `for_neg`（下一节详述）来处理「有符号×无符号」的负向极值：

```python
# en_cl_fix_types.py:254-259
if a_fmt.S == 0 and b_fmt.S == 1:
    I = max(rmaxI, FixFormat.for_neg(a_fmt).I + b_fmt.I)
elif a_fmt.S == 1 and b_fmt.S == 0:
    I = max(rmaxI, a_fmt.I + FixFormat.for_neg(b_fmt).I)
else:
    I = rmaxI
```

符号位特例——**两个 1 位有符号数相乘结果为无符号**：

```python
# en_cl_fix_types.py:261-267
if a_fmt.width == 1 and a_fmt.S == 1 and b_fmt.width == 1 and b_fmt.S == 1:
    # Special case: 1-bit signed * 1-bit signed is unsigned
    S = 0
else:
    S = max(a_fmt.S, b_fmt.S)
```

VHDL 侧 [en_cl_fix_pkg.vhd:508-577](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L508-L577) 是同一份逻辑的镜像，用 `choose(cond, a, b)` 替代 Python 的条件表达式，分支一一对应：rmaxI 在 [en_cl_fix_pkg.vhd:534-540](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L534-L540)，rmin 在 [en_cl_fix_pkg.vhd:559-565](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L559-L565)，符号特例在 [en_cl_fix_pkg.vhd:568-574](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L568-L574)。

#### 4.1.4 代码实践

**实践目标**：手算两个乘法的结果格式，再用 Python 验证，体会整数位的 ±1 调整与符号特例。

**操作步骤**（在仓库根目录运行）：

```python
# 1) 准备环境（u1-l4 已安装依赖）
import sys
sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

# 用例 A：[1,1,1] * [1,1,1]
aA, bA = FixFormat(1,1,1), FixFormat(1,1,1)
print("A:", FixFormat.for_mult(aA, bA))   # 预期 (1, 3, 2)

# 用例 B：[0,1,0] * [1,0,1]
aB, bB = FixFormat(0,1,0), FixFormat(1,0,1)
print("B:", FixFormat.for_mult(aB, bB))   # 预期 (1, 0, 1)

# 彩蛋：1-bit signed 特例
aC, bC = FixFormat(1,0,0), FixFormat(1,0,0)
print("C:", FixFormat.for_mult(aC, bC))   # 预期 (0, 1, 0) —— 结果无符号！
```

**手算对照**：

- 用例 A：两边都有符号 → rmaxI \(=1+1+1=3\)；两边同号 → rmin 不超过 rmaxI，\(I=3\)；宽度非 1 → \(S=\max(1,1)=1\)；\(F=1+1=2\)。得 \((1,3,2)\)。校验：\([1,1,1]\) 取值 \([-2,1.5]\)，最大积 \((-2)\times(-2)=4\)，\([1,3,2]\) 上界 \(2^3-2^{-2}=7.75\)，\(4\le 7.75\) ✓。
- 用例 B：非两有符号；\(a.I+a.F=1\le 1\) 命中「−1 位」→ rmaxI \(=1+0-1=0\)；有符号×无符号 → 需 `for_neg((0,1,0)).I + b.I`，而 `for_neg((0,1,0))` 命中 1 位无符号特例等于 \((1,0,0)\)、\(I=0\)，故 \(I=\max(0,0)=0\)；\(S=\max(0,1)=1\)；\(F=0+1=1\)。得 \((1,0,1)\)。校验：\([0,1,0]\in\{0,1\}\)、\([1,0,1]\in[-1,0.5]\)，乘积 \(\{-1,-0.5,0,0.5\}\)，正好被 \((1,0,1)\in[-1,0.5]\) 精确覆盖 ✓。
- 用例 C：两个 1 位有符号（取值均为 \(\{-1,0\}\)... 实为 \(\{0,-1\}\)），乘积 \(\{0,1\}\) 恒非负 → \(S=0\)，得 \((0,1,0)\)。

**预期结果**：三条 `print` 分别输出 `(1, 3, 2)`、`(1, 0, 1)`、`(0, 1, 0)`。若一致即通过。

#### 4.1.5 小练习与答案

**练习 1**：手算 `for_mult(FixFormat(0,3,0), FixFormat(0,3,0))`（两个 3 位无符号整数相乘）。

**答案**：非两有符号；\(a.I+a.F=3>1\)、\(b.I+b.F=3>1\)，不命中「−1 位」→ rmaxI \(=3+3=6\)；两无符号 → rmin 不超过 rmaxI → \(I=6\)；\(S=\max(0,0)=0\)；\(F=0\)。得 \((0,6,0)\)。校验：\(7\times 7=49\)，\([0,6,0]\) 上界 \(2^6-1=63\) ✓。

**练习 2**：为什么「两边都有符号」时整数位是 \(+1\)，而「一有一无」时却常常不需要 \(+1\)？

**答案**：两边有符号时，最大正积来自两个最负值 \((-2^{a.I})(-2^{b.I})=2^{a.I+b.I}\)，它是 2 的整数幂，有符号表示 \(2^{n}\) 需要 \(n+1\) 位整数，所以 \(+1\)。一有一无时，最大正积来自两个最大正值 \(a_{\max}b_{\max}<2^{a.I}2^{b.I}\)，严格小于 2 的幂，\(a.I+b.I\) 位已足够，无需 \(+1\)。

---

### 4.2 for_neg 与 for_abs：取反与绝对值

#### 4.2.1 概念说明

取反 \(-a\) 与绝对值 \(|a|\) 看似简单，但定点补码有一个著名的不对称性：**有符号数的最负值没有对应的正值**。例如 \([1,2,0]\)（有符号 3 位整数）取值 \([-4,3]\)，\(-(-4)=+4\) 超出原格式上界 3。所以：

- `for_neg`：取反一个有符号数，整数位通常 \(+1\)，才能装下「最负值的相反数」。
- `for_abs`：绝对值的结果「要么等于原值、要么等于其相反数」，所以结果格式必须同时容得下两者 → 正好是 `union(原格式, 取反格式)`。

#### 4.2.2 核心流程

`for_neg(a_fmt)`：

- **1 位无符号特例**：\(a.S=0\) 且宽度 \(=1\)（取值 \(\{0,1\}\) 或含负位权的等价 1 位无符号），取反得 \(\{0,-1\}\)，是一个 1 位有符号数，整数位比朴素估计少 1：返回 \((1,\ a.I+a.S-1,\ a.F)\)。
- **一般情况**：返回 \((1,\ a.I+a.S,\ a.F)\)。即总是变有符号（\(S=1\)）；若原本有符号（\(a.S=1\)）则整数位 \(+1\)，若原本是无符号且宽度 \(>1\)（\(a.S=0\)）则整数位不变——因为无符号范围 \([0,\dots]\) 取反成 \([\dots,0]\)，只需补一个符号位，整数位够用。

`for_abs(a_fmt) = union(a_fmt, for_neg(a_fmt))`：对三字段取 max。注意结果仍是有符号（\(S=1\)），即便绝对值本身非负——这是「格式只看范围、不看运行时符号」的保守选择的必然结果。

#### 4.2.3 源码精读

Python 的 `for_neg` 两条路径，注释点明了补码不对称：

[en_cl_fix_types.py:272-285](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L272-L285) — `for_neg` 全函数：

```python
# en_cl_fix_types.py:281-285
assert a_fmt.width > 0, "Data width must be positive"
# 1-bit unsigned inputs are special (neg is 1-bit signed)
if a_fmt.S == 0 and a_fmt.width == 1:
    return FixFormat(1, a_fmt.I+a_fmt.S-1, a_fmt.F)
return FixFormat(1, a_fmt.I+a_fmt.S, a_fmt.F)
```

`for_abs` 只有一行实质逻辑，把「原值」与「相反值」两种情况交给 `union`：

```python
# en_cl_fix_types.py:288-299
@staticmethod
def for_abs(a_fmt):
    ...
    neg_fmt = FixFormat.for_neg(a_fmt)
    return FixFormat.union(a_fmt, neg_fmt)
```

VHDL 镜像：[en_cl_fix_pkg.vhd:579-588](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L579-L588)（`cl_fix_neg_fmt`，特例在 582-584 行）与 [en_cl_fix_pkg.vhd:590-594](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L590-L594)（`cl_fix_abs_fmt`）。

> 验证依据：`format_tests.py` 对每个 `a_fmt` 算出 \(r_{\max}=\max(a_{\max},-a_{\min})\)、\(r_{\min}=\min(a_{\min},-a_{\max})\)，断言 `for_neg`/`for_abs` 的结果格式既「够用」又「不浪费」，见 [format_tests.py:225-280](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py#L225-L280)。

#### 4.2.4 代码实践

**实践目标**：观察 `for_neg` 的两条路径与 `for_abs` 的整数位增长。

```python
import sys; sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

# 特例：1 位无符号
print(for_neg(FixFormat(0,1,0)))   # 预期 (1, 0, 0)
# 一般有符号：整数位 +1
print(for_neg(FixFormat(1,1,1)))   # 预期 (1, 2, 1)
# abs = union(原, 取反)
print(for_abs(FixFormat(1,1,1)))   # 预期 (1, 2, 1)
```

**手算**：\((0,1,0)\) 命中特例 → \((1,\,1+0-1,\,0)=(1,0,0)\)，校验 \(\{0,1\}\) 取反为 \(\{0,-1\}=[1,0,0]\) ✓。\((1,1,1)\) 走一般分支 → \((1,\,1+1,\,1)=(1,2,1)\)，校验 \([-2,1.5]\) 取反为 \([-1.5,2]\)，\(2\) 需要 \(I=2\)（\([1,1,1]\) 上界仅 \(1.5\)）✓。`for_abs((1,1,1))=union((1,1,1),(1,2,1))=(1,2,1)`。

**预期结果**：`(1, 0, 0)`、`(1, 2, 1)`、`(1, 2, 1)`。

#### 4.2.5 小练习与答案

**练习 1**：`for_neg(FixFormat(0,2,0))`（宽度 2 的无符号）的结果是什么？为什么整数位不增长？

**答案**：\((1,2,0)\)。因为无符号 \([0,2,0]\in[0,3]\)，取反 \([-3,0]\)，只需把符号位从 0 变 1，整数位 2 已能表示到 \(\pm 3\)（\([1,2,0]\) 范围 \([-4,3]\)）。走一般分支 \((1,\,2+0,\,0)=(1,2,0)\)，整数位不变。

**练习 2**：对一个恒非负的有符号输入（运行时保证不取负值），`for_abs` 的结果格式是否「浪费」？这违背 mid_fmt 的「不浪费」原则吗？

**答案**：可能多一位整数（当输入格式含最负值时，`for_neg` 给 \(+1\)，`union` 继承之）。但这不违背原则——`for_abs` 是**按范围保守推导**的，它不知道「运行时恒非负」这个约束。代码注释明确：若值受限，更窄格式或许可行。要利用该约束需自行指定更窄的 r_fmt。

---

### 4.3 for_shift 与 for_round：移位与舍入的结果格式

#### 4.3.1 概念说明

- **移位 `for_shift`**：左移 \(n\) 等价于乘 \(2^{n}\)，只是把小数点相对搬动，**完全不损失信息**。所以移位本身不改总位宽的「信息量」，只是把整数位和小数位重新分配。难点在于移位量往往是一个范围 \([n_{\min},n_{\max}]\)（例如可变增益），格式必须同时容纳所有可能的移位。
- **舍入 `for_round`**：把小数位从 \(a.F\) 减少到 \(r\) 位。截断（`Trunc_s`）只会丢低位、不会让数值变大；但其他 6 种舍入模式会「进位」，可能把 \(1.111\ldots\) 抬成 \(10.000\ldots\)，从而**溢出 1 位整数**。

#### 4.3.2 核心流程

`for_shift(a_fmt, minShift, maxShift=None)`（`maxShift` 缺省等于 `minShift`）：

\[
(S,\ I,\ F)_{\text{result}} = \bigl(a.S,\ \ a.I + \text{maxShift},\ \ a.F - \text{minShift}\bigr)
\]

直觉：最左移（maxShift）决定整数位最多涨多少；最右移（minShift，可为负）决定小数位最多涨多少。总位宽 \(=a\) 的位宽 \(+(\text{maxShift}-\text{minShift})\)，恰好为整个移位范围预留空间。assert 要求 `minShift <= maxShift`。

`for_round(a_fmt, rFracBits, rnd)`：

- 若 \(r \ge a.F\)（没减少小数位）或 `rnd == Trunc_s`：整数位不变 \(I=a.I\)。
- 否则（其他 6 种模式在减少小数位）：\(I=a.I+1\)，预留进位溢出。
- 最后强制结果至少 1 位宽：若 \(a.S+I+r<1\)，抬高 \(I\) 补齐。

#### 4.3.3 源码精读

`for_shift` 一行核心：

[en_cl_fix_types.py:302-315](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L302-L315) — `for_shift` 全函数：

```python
# en_cl_fix_types.py:311-315
assert a_fmt.width > 0, "Data width must be positive"
if maxShift is None:
    maxShift = minShift
assert minShift <= maxShift, f"minShift ({minShift}) must be <= maxShift ({maxShift})"
return FixFormat(a_fmt.S, a_fmt.I + maxShift, a_fmt.F - minShift)
```

`for_round` 的三分支与「至少 1 位」兜底：

[en_cl_fix_types.py:318-342](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L318-L342) — `for_round` 全函数：

```python
# en_cl_fix_types.py:328-342
if rFracBits >= a_fmt.F:
    I = a_fmt.I                       # 没减少小数位
elif rnd == FixRound.Trunc_s:
    I = a_fmt.I                       # 截断不进位
else:
    I = a_fmt.I + 1                   # 其余模式可能进位 +1
# Force result to be at least 1 bit wide
if a_fmt.S + I + rFracBits < 1:
    I = -a_fmt.S - rFracBits + 1
return FixFormat(a_fmt.S, I, rFracBits)
```

VHDL 镜像：`cl_fix_shift_fmt` 两个重载（定长与范围）在 [en_cl_fix_pkg.vhd:596-606](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L596-L606)，`cl_fix_round_fmt` 在 [en_cl_fix_pkg.vhd:608-628](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L608-L628)，分支与 Python 完全一致。

> 旁证：`cl_fix_round` 在运行时会用 `assert result_fmt = cl_fix_round_fmt(...)` 校验调用者给的 r_fmt 是否「恰好等于」推导值（[en_cl_fix_pkg.vhd:941-944](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L941-L944)），说明 `for_round` 的输出是「唯一正确」的目标格式——这正是 +1 整数位必须被尊重的原因。

#### 4.3.4 代码实践

**实践目标**：手算移位与两种舍入的结果格式，体会「进位 +1」与「无损移位」。

```python
import sys; sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

# 移位范围 [-2, +3]
print(for_shift(FixFormat(1,2,4), minShift=-2, maxShift=3))   # 预期 (1, 5, 6)
# 舍入到 1 位小数：进位模式 vs 截断
print(for_round(FixFormat(1,1,3), 1, FixRound.NonSymPos_s))  # 预期 (1, 2, 1)
print(for_round(FixFormat(1,1,3), 1, FixRound.Trunc_s))      # 预期 (1, 1, 1)
```

**手算**：

- 移位：\((1,\,2+3,\,4-(-2))=(1,5,6)\)。位宽 \(1+5+6=12\)，原位宽 \(7\)，多出 \(5=\text{maxShift}-\text{minShift}=3-(-2)\) 位用于覆盖整个移位范围。
- 进位舍入：\(r=1<a.F=3\) 且非截断 → \(I=1+1=2\) → \((1,2,1)\)。校验：\([1,1,3]\) 最大值 \(1.875\)，四舍五入到 1 位小数得 \(2.0\)，超出 \([1,1,1]\) 上界 \(1.5\)，需要 \([1,2,1]\) ✓。
- 截断：\(I=1\) → \((1,1,1)\)。截断 \(1.875\to 1.0\) 不会上溢，无需多一位。

**预期结果**：`(1, 5, 6)`、`(1, 2, 1)`、`(1, 1, 1)`。

#### 4.3.5 小练习与答案

**练习 1**：`for_shift(FixFormat(0,3,0), shift=2)`（定长左移 2）的结果与位宽？

**答案**：`maxShift=minShift=2` → \((0,\,3+2,\,0-2)=(0,5,-2)\)。位宽 \(0+5-2=3\)，与原位宽相同——无损左移只重分配整数/小数位，不增信息量。负的小数位 \(F=-2\) 表示小数点落在物理位之外、隐含两个零（u1-l2 已述）。

**练习 2**：为何 `for_round` 在「不减少小数位」（\(r\ge a.F\)）时整数位不变，即使是非截断模式？

**答案**：舍入的进位风险只发生在「丢掉低位」时；若小数位不减反增（或不变），没有任何低位被丢弃，也就没有进位，整数位自然不变。所以该分支对所有舍入模式都返回 \(I=a.I\)。

---

### 4.4 union：格式合并工具

#### 4.4.1 概念说明

`union` 回答一个问题：**给定若干格式，哪一个最窄的格式能同时精确表示它们全部？** 答案是对 S/I/F 三字段分别取最大值。它是格式级别的「最大公约上界」：

- 小数位取 max：要容纳更细粒度的那个。
- 整数位取 max：要容纳更大范围的那个。
- 符号位取 max：只要有一个是有符号，结果就得有符号。

它被三处复用，是本讲多个函数的「黏合剂」：`for_addsub`、`for_abs`、以及 `cl_fix_compare`（把两个操作数对齐到同一格式再比较）。

#### 4.4.2 核心流程

\[
\text{union}(f_1,\dots,f_n) = \bigl(\max_k f_k.S,\ \ \max_k f_k.I,\ \ \max_k f_k.F\bigr)
\]

实现接受两种入参：两个 `FixFormat`，或一个 `FixFormat` 的集合（`b_fmt=None` 时把第一个参数当成集合）。流程是「以第一个格式为起点，逐个与其余取 max」。

#### 4.4.3 源码精读

Python 实现，用 `shallow_copy` 保留首个格式再逐字段取 max：

[en_cl_fix_types.py:345-363](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L345-L363) — `union` 全函数：

```python
# en_cl_fix_types.py:352-362
if b_fmt is None:
    fmts = a_fmt              # 集合入参
else:
    fmts = (a_fmt, b_fmt)     # 双参数入参
r_fmt = shallow_copy(fmts[0])
for i in range(1, len(fmts)):
    r_fmt.S = max(r_fmt.S, fmts[i].S)
    r_fmt.I = max(r_fmt.I, fmts[i].I)
    r_fmt.F = max(r_fmt.F, fmts[i].F)
return r_fmt
```

复用点一览：

- `for_addsub = union(for_add, for_sub)`，见 [en_cl_fix_types.py:196-198](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/models/python/en_cl_fix_pkg/en_cl_fix_types.py#L196-L198)（u3-l1 已讲）。
- `for_abs = union(a_fmt, for_neg(a_fmt))`，见 4.2.3。
- `cl_fix_compare` 用 `union(aFmt, bFmt)` 对齐两操作数，见 [en_cl_fix_pkg.vhd:1283](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1283)。

VHDL 内部函数 `union` 在 [en_cl_fix_pkg.vhd:353-360](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L353-L360)，同样三字段 `maximum`，`cl_fix_addsub_fmt` 在 [en_cl_fix_pkg.vhd:500-506](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L500-L506) 调用它。

#### 4.4.4 代码实践

**实践目标**：手算两个格式的 union，并验证它确实等于 `for_addsub` 的合成结果。

```python
import sys; sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *

# 直接 union
print(FixFormat.union(FixFormat(0,2,2), FixFormat(1,1,2)))   # 预期 (1, 2, 2)
# 验证 for_addsub 内部确实用了 union
a, b = FixFormat(0,2,2), FixFormat(1,1,2)
addsub = FixFormat.for_addsub(a, b)
manual = FixFormat.union(FixFormat.for_add(a,b), FixFormat.for_sub(a,b))
print(addsub == manual, addsub)   # 预期 True (1, 2, 2)
```

**手算**：\(\text{union}((0,2,2),(1,1,2))=(\max(0,1),\max(2,1),\max(2,2))=(1,2,2)\)。

**预期结果**：`(1, 2, 2)` 与 `True (1, 2, 2)`。

#### 4.4.5 小练习与答案

**练习 1**：`union((0,1,4),(1,3,2),(0,2,8))`（三个格式）的结果？

**答案**：\((\max(0,1,0),\max(1,3,2),\max(4,2,8))=(1,3,8)\)。`union` 支持集合入参，对任意个数格式都成立。

**练习 2**：`for_abs` 为何写成 `union(a, for_neg(a))` 而不是直接写 `max(a.I, for_neg(a).I)+...`？

**答案**：复用 `union` 避免重复实现「三字段取 max」逻辑，且语义清晰——abs 的结果「要么是 \(a\)、要么是 \(-a\)」，其格式必须能同时表示两者，正是 union 的定义。这也让 `for_abs` 的正确性直接建立在 `union` 与 `for_neg` 之上，便于测试与维护。

---

## 5. 综合实践

**任务**：把「乘法」与「绝对值」串起来，亲手走一遍格式推导，并用穷举对拍验证，最后体会「保守推导的代价」。

设定：\(a\) 为有符号 \([1,2,2]\)（取值 \([-4,3.75]\)），\(b\) 为无符号 \([0,2,2]\)（取值 \([0,3.75]\)）。求它们的乘积格式，以及「对该乘积格式取绝对值」的结果格式。

**第 1 步：手算乘积 mid_fmt**

- 非两有符号（\(b.S=0\)）；\(a.I+a.F=4>1\)、\(b.I+b.F=4>1\) → rmaxI \(=2+2=4\)。
- 有符号×无符号 → \(I=\max(4,\ a.I+\text{for\_neg}(b).I)\)。`for_neg((0,2,2))`：无符号且宽度 \(4\ne1\) → 一般分支 \((1,2,2)\)，\(I=2\)。故 \(I=\max(4,2+2)=4\)。
- \(S=\max(1,0)=1\)；\(F=2+2=4\)。**得 \((1,4,4)\)**。

**第 2 步：手算 abs(mid_fmt)**

`for_abs((1,4,4))=union((1,4,4), for_neg((1,4,4)))`。\((1,4,4)\) 有符号 → `for_neg` 走一般分支 \((1,\,4+1,\,4)=(1,5,4)\)。`union((1,4,4),(1,5,4))=(1,5,4)`。**得 \((1,5,4)\)**——比乘积格式多一位整数。

**第 3 步：Python 验证 + 穷举对拍**

```python
import sys; sys.path.append("bittrue/models/python")
from en_cl_fix_pkg import *
import numpy as np

a, b = FixFormat(1,2,2), FixFormat(0,2,2)
mult_fmt = FixFormat.for_mult(a, b)
abs_fmt  = FixFormat.for_abs(mult_fmt)
print("mult_fmt:", mult_fmt)   # 预期 (1, 4, 4)
print("abs_fmt :", abs_fmt)    # 预期 (1, 5, 4)

# 穷举所有 a*b 乘积，看真实取值范围
amin, amax = cl_fix_min_value(a), cl_fix_max_value(a)
bmin, bmax = cl_fix_min_value(b), cl_fix_max_value(b)
prods = [x*y for x in np.arange(amin, amax+2**-a.F, 2**-a.F)
               for y in np.arange(bmin, bmax+2**-b.F, 2**-b.F)]
print("实际乘积范围:", min(prods), "~", max(prods))   # -15 ~ 14.0625
print("实际 |乘积| 最大值:", max(abs(p) for p in prods))  # 15
```

**第 4 步：观察与解释**

- `mult_fmt=(1,4,4)`：实际乘积 \([-15,14.0625]\) 被 \((1,4,4)\in[-16,15.9375]\) 精确覆盖 ✓。
- **关键现象**：实际 \(|乘积|\) 最大只有 \(15\)，本可放进 \((1,4,4)\)（上界 \(15.9375\)）；但 `for_abs` 返回 \((1,5,4)\)，多了一位整数。为什么？因为 `for_abs` 只看「乘积格式 \((1,4,4)\) 的理论范围 \([-16,15.9375]\)」，它假设输入可能取到最负值 \(-16\)，而 \(|-16|=16\) 超出 \((1,4,4)\) 上界，必须 \(+1\) 整数位。它并不知道「乘积实际到不了 \(-16\)」。

这正是代码注释反复强调的 **保守推导的代价**：格式函数只看范围、不看值，组合（mult → abs）会把保守性层层传递。若你确知乘积受限，可自行指定更窄的 r_fmt 来省掉这一位——但库不会替你假设。

**预期结果**：`mult_fmt` 与 `abs_fmt` 与手算一致；实际 \(|乘积|\) 最大为 \(15\)，小于 `for_abs` 给出的上界 \(2^5-2^{-4}=31.9375\)，差值即保守余量。

> 待本地验证：穷举的浮点列表在边界处可能因浮点误差多算一个点；若看到 `实际乘积范围` 略有偏差，属正常，关注量级与符号即可。

## 6. 本讲小结

- **乘法小数位直接相加**（\(F=a.F+b.F\)）；**整数位**以 \(a.I+b.I\) 为基准，按符号组合 \(+1\)（两有符号，最大正积是 2 的幂）、\(-1\)（一边「很小」\(I+F\le1\)）或不变。
- **两个 1 位有符号数相乘结果为无符号**，是 `for_mult` 唯一的符号位特例。
- **取反**：有符号数因补码不对称（最负值无正对应）通常整数位 \(+1\)；1 位无符号是特例。**绝对值** \(=\text{union}(a,\text{for\_neg}(a))\)，必须同时容纳原值与相反值。
- **移位**无损，只是把整数位/小数位按 \([n_{\min},n_{\max}]\) 重新分配：\((a.S,\ a.I+n_{\max},\ a.F-n_{\min})\)。
- **舍入**在减少小数位时，除截断外的 6 种模式都可能进位，整数位 \(+1\)；这正是 `cl_fix_round_fmt` 与运行时 assert 的依据。
- **union** 对三字段取 max，是 `for_addsub`/`for_abs`/`cl_fix_compare` 共用的「格式合并」黏合剂；所有 `for_*` 函数都遵循**保守（只看范围）**推导，组合时会累积保守余量。

## 7. 下一步学习建议

至此，单元 3（结果格式推导）已完整覆盖 add/sub/addsub/mult/neg/abs/shift/round 全家族。接下来：

- **单元 4（舍入、饱和与 resize）**：本讲的 `for_round` 只是「格式」层面；单元 4 的 u4-l1 会进入 `cl_fix_round` 的**运算**实现，看 7 种模式如何用「加偏移再截断」落地，与本讲的 \(+1\) 整数位互为表里。
- **单元 5（数据转换与算术链路）**：u5-l2 会把本讲的 `for_mult` 接进 `cl_fix_mult` 的三段式（convert → 乘 → resize），看格式推导如何驱动真实运算与中间表示。
- **建议精读**：直接对照 [format_tests.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/bittrue/tests/python/format_tests.py) 的「够用 + 不浪费」断言，它是验证你手算结果最可靠的标尺。
