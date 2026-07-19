# 移位、均值、取反与绝对值

## 1. 本讲目标

本讲继续把 u4-l1 / u4-l2 已经建立的「**中间全精度格式 → 精确运算 → `cl_fix_resize` 舍入/饱和**」统一架构，落到四类常用一元/二元运算上：

- `cl_fix_shift`：无损移位（左移放大、右移缩小），只改变格式不丢精度。
- `cl_fix_mean`：求均值，本质是「先相加、再右移一位」。
- `cl_fix_neg` / `cl_fix_abs`：取反与绝对值，必须为有符号数预留一个额外整数位（`ForNeg`）以容纳「最负值取反」这个角落情况。
- `cl_fix_sneg` / `cl_fix_sabs`：资源优化变体，用「按位取反」替代「补码取反（取反加一）」，省掉一个加法器和一个整数位，代价是负数结果最多差 1 LSB。

学完本讲，你应当能够：

1. 说清 `FixFormat.ForShift` 的公式，并能解释为什么移位是「改变格式而非截断」。
2. 画出 `cl_fix_mean` 的两步数据流（`cl_fix_add` → `cl_fix_shift(-1)`），并解释中间和的 `+1` 整数位为何会被随后的 `/2` 抵消。
3. 解释 `ForNeg` 为何要给有符号数加一个整数位，并用「最负值取反」的例子验证。
4. 区分 `abs`/`neg`（精确，有 `+1` 加法器）与 `sabs`/`sneg`（省资源，负数差 1 LSB）的实现差异。
5. 识别 `enable` 参数在三种语言间的一致性陷阱。

## 2. 前置知识

在进入本讲前，请确认你已掌握以下概念（来自 u1、u3、u4-l1）：

- **[S,I,F] 三元组**：`Signed`（是否有符号位）、`IntBits`（整数位）、`FracBits`（小数位），总位宽 `W = S + I + F`，数值 `V = N × 2^(-F)`（N 为位串按补码解释的整数）。`I`、`F` 均可为负（u1-l2）。
- **`cl_fix_resize` 是全库的心脏**：所有运算最终都汇聚到它，由它统一完成舍入（u3-l2）与饱和/回绕（u3-l3）。本讲的每个函数最后一步都是 `cl_fix_resize`。
- **中间全精度格式**：运算先在「足以无损装下精确结果」的 `TempFmt` 上进行，精度只会在最后一步 resize 时丢失（u4-l1 的 `ForAdd`/`ForSub`、u4-l2 的 `ForMult`）。
- **二进制小数点的物理含义**：左移一位 = 小数点不动、数值翻倍 = 等价于把一位从「小数侧」搬到「整数侧」；右移一位反之。本讲的关键直觉就建立在这上面。

一个贯穿全讲的核心事实：**补码系统是不对称的**。对 `(true, I, F)` 格式，可表示范围是

\[
-2^{I} \le V \le 2^{I} - 2^{-F}
\]

负方向能走到 `-2^I`，正方向最多到 `2^I - 2^(-F)`。于是「最负值 `-2^I` 取反得到 `+2^I`」在原格式里**装不下**——这正是 `ForNeg` 要多一个整数位的根本原因，也是 `s` 变体 1 LSB 误差的来源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | 定义 `FixFormat.ForShift`、`ForNeg` 等格式增长规则（Python） |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | `cl_fix_shift` / `mean` / `abs` / `sabs` / `neg` / `sneg` 的 Python 实现 |
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | 上述函数的 VHDL 实现（综合用，位真基准） |
| [matlab/src/cl_fix_shift.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_shift.m) | MATLAB 移位实现 |
| [matlab/src/cl_fix_mean.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean.m) | MATLAB 均值实现 |
| [matlab/src/cl_fix_abs.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_abs.m) | MATLAB 绝对值实现 |
| [matlab/src/cl_fix_neg.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_neg.m) | MATLAB 取反实现 |
| [python/unittest/en_cl_fix_pkg_test.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py) | 这些函数的单元测试（本讲代码实践的依据） |

## 4. 核心概念与源码讲解

### 4.1 无损移位：FixFormat.ForShift 与 cl_fix_shift

#### 4.1.1 概念说明

「移位」在定点库里有一个反直觉但极其重要的设计：**`cl_fix_shift` 不截断任何位**。它的语义是「先把数值精确地乘以 `2^shift`（左移为正、右移为负），放进一个**刚好好装下**的中间格式，再由最后一步 `cl_fix_resize` 决定要不要舍入/饱和」。

换句话说，移位本身是**纯格式变换**：左移 k 位，相当于把二进制小数点向左挪 k 位——数值翻倍 `2^k`，但底层的位模式一个都没丢。真正可能丢精度（舍掉多余小数位）或溢出（砍掉多余整数位）的，只有最后那一步 resize，而那一步受你传入的 `round` / `saturate` 控制。

这一点和「先把数截成 k 位再移」的朴素做法完全不同，也是 en_cl_fix 一致性的基石：移位是**精确运算**，误差只来自 resize。

#### 4.1.2 核心流程

左移 `shift` 位（`shift > 0`）后，数值变成 `a × 2^shift`，它正好可以用一个比原格式「整数位 +shift、小数位 −shift」的格式**无损**装下。这个格式由 `ForShift` 给出：

\[
\text{ForShift}(aFmt,\ \text{minShift},\ \text{maxShift}) = (aFmt.Signed,\ aFmt.IntBits + \text{maxShift},\ aFmt.FracBits - \text{minShift})
\]

- `minShift` / `maxShift` 用来支持「移位量是数组」的情形：中间格式必须同时覆盖所有可能移位量，故整数位按最大左移量增长、小数位按最大右移量（即最小 shift）增长。
- 若 shift 是常数，`minShift = maxShift = shift`，公式退化为 `(Signed, I+shift, F−shift)`，**总位宽 `S+I+F` 不变**——左移只是把位从小数侧搬到整数侧。

于是整个 `cl_fix_shift` 的流程是：

```
1. 计算 temp_fmt = ForShift(aFmt, shift)            # 无损容纳 a×2^shift 的格式
2. 把 a×2^shift 放进 temp_fmt（这一步不丢精度）
3. return cl_fix_resize(移位后的值, temp_fmt, result_fmt, round, saturate)
```

#### 4.1.3 源码精读

**`ForShift` 的定义**（Python 类型文件）：

[en_cl_fix_types.py:38-44](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L38-L44) —— 注意它同时接受 `minShift` 和 `maxShift`，并断言 `minShift <= maxShift`；整数位按 `maxShift` 增长、小数位按 `minShift` 减少。

```python
@staticmethod
def ForShift(aFmt, minShift, maxShift=None):
    if maxShift is None:
        maxShift = minShift
    assert minShift <= maxShift, ...
    return FixFormat(aFmt.Signed, aFmt.IntBits + maxShift, aFmt.FracBits - minShift)
```

**Python `cl_fix_shift`**：注释直接点明「lossless shift, then resize, 初始移位不截断」：

[en_cl_fix_pkg.py:393-428](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L393-L428) —— 窄路径（narrow）在 [L427-L428](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L427-L428) 用 `np.min(shift)` / `np.max(shift)` 构造 `temp_fmt`，再做 `a * 2.0 ** shift` 后 resize。

```python
# 窄（双精度）路径
temp_fmt = FixFormat.ForShift(aFmt, np.min(shift), np.max(shift))
return cl_fix_resize(a * 2.0 ** shift, temp_fmt, rFmt, rnd, sat)
```

> 大位宽（>53 位）走 `wide_fxp` 路径 [en_cl_fix_pkg.py:402-425](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L402-L425)：它甚至不真的做算术移位，而是「保持内部大整数 `data` 不变、只换一个 `temp_fmt` 标签」，再 resize——因为改变格式标签就等价于移位。这是 wide 实现里非常优雅的一点（详见 Unit 6）。

**MATLAB `cl_fix_shift`** 与 Python 窄路径逐行对应：

[cl_fix_shift.m:39-42](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_shift.m#L39-L42) —— `temp_fmt = (Signed, I+shift, F−shift)`，再 `cl_fix_resize(a*2^shift, temp_fmt, result_fmt, ...)`。

```matlab
temp_fmt = cl_fix_format(a_fmt.Signed, a_fmt.IntBits+shift, a_fmt.FracBits-shift);
result  = cl_fix_resize(a*2^shift, temp_fmt, result_fmt, round, saturate);
```

**VHDL `cl_fix_shift`** 用了一个等价但更巧妙的编码：它不从 `a_fmt` 推 `temp_fmt`，而是从 `result_fmt` 反推一个「移位前」的格式，然后**一次 resize 直接到位**：

[en_cl_fix_pkg.vhd:2558-2574](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2558-L2574) —— `TempFmt_c = (result_fmt.Signed, result_fmt.IntBits - shift, result_fmt.FracBits + shift)`，然后 `return cl_fix_resize(a, a_fmt, TempFmt_c, round, saturate)`。

```vhdl
constant TempFmt_c : FixFormat_t := (
    Signed   => result_fmt.Signed,
    IntBits  => result_fmt.IntBits - shift,
    FracBits => result_fmt.FracBits + shift);
begin
    return cl_fix_resize(a, a_fmt, TempFmt_c, round, saturate);
```

这里的精妙之处：把 `a`（值 `v`）resize 进 `TempFmt_c`（其 `FracBits = result_fmt.FracBits + shift`），得到的位串如果被调用方按 `result_fmt`（`FracBits` 少 `shift`）来读，数值正好是 `v × 2^shift`；而 resize 期间的饱和边界也按 `TempFmt_c` 计算，恰好对应「移位后再按 `result_fmt` 饱和」的边界。所以一次 resize 就同时完成了「移位 + 舍入 + 饱和」，结果与 Python/MATLAB 位真一致。

#### 4.1.4 代码实践

**目标**：验证「左移改变格式、保持位宽、数值无损放大」。

**操作步骤**（在 `python/unittest` 目录运行，先 `export PYTHONPATH=../src` 或在脚本里 `sys.path.append("../src")`）：

```python
from en_cl_fix_pkg import *

aFmt   = FixFormat(True, 3, 4)        # 8 位有符号，范围 -8 .. 7.9375，LSB=0.0625
rFmt   = FixFormat(True, 5, 2)        # 也是 8 位，范围 -32 .. 31.75，LSB=0.25
a      = 1.5                           # 在 aFmt 中可精确表示

# 1) 手算 ForShift
fs = FixFormat.ForShift(aFmt, 2)       # 常数移位：min=max=2
print("ForShift =", fs)                # 预期 (True, 5, 2)，位宽仍是 8

# 2) 左移 2 位，结果格式正好等于 ForShift
r = cl_fix_shift(a, aFmt, 2, rFmt, FixRound.Trunc_s, FixSaturate.None_s)
print("shift result =", r)             # 预期 6.0（= 1.5 × 4）
```

**需要观察的现象 / 预期结果**（按算法手动推导，待本地运行确认）：

- `ForShift((true,3,4), 2) = (true, 5, 2)`，位宽 `1+5+2 = 8`，**与输入位宽相同**——左移只是把两位从 `FracBits` 搬到 `IntBits`。
- 数值 `1.5 × 2² = 6.0`，在 `(true,5,2)` 内精确表示（`6.0 / 0.25 = 24`，落在 `−128..127` 内）。
- 因为 `result_fmt` 恰好等于 `ForShift`，没有多余位要砍，所以**没有舍入、没有饱和**，结果严格等于 `6.0`。

把 `rFmt` 改成 `FixFormat(True, 3, 4)`（整数位装不下 `6.0`？不会，`(true,3,4)` 上界 `7.9375 > 6.0`，仍能装下），结果应仍为 `6.0`；再改成 `FixFormat(True, 2, 4)`（上界 `3.9375 < 6.0`）配合 `Sat_s`，则应饱和到 `3.9375`——这证明了「移位本身无损，溢出由 resize 的饱和处理」。

> 若无法本地运行，以上为按源码逻辑推导的预期值，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：把 `aFmt = (false, 3, 2)`、`shift = −1`（右移一位）代入 `ForShift`，写出中间格式与位宽。
**答案**：`ForShift((false,3,2), −1) = (false, 3+(−1), 2−(−1)) = (false, 2, 3)`，位宽 `0+2+3 = 5`，与输入 `(false,3,2)` 的 `5` 相同。右移一位 = 数值减半 = 一位从整数侧搬到小数侧。

**练习 2**：为什么 `ForShift` 需要同时接收 `minShift` 和 `maxShift` 两个参数？
**答案**：当 `shift` 是数组（每个元素移位量不同）时，中间格式必须能无损容纳**所有**移位结果。整数位按最大左移量 `maxShift` 增长（最猛的放大需要最多整数位），小数位按最大右移量（即最小 shift `minShift`）增长（最猛的缩小需要最多小数位）。常数移位时二者相等，退化为单参数。

---

### 4.2 均值：cl_fix_mean = cl_fix_add 后 shift(−1)

#### 4.2.1 概念说明

求两个数的均值 `(a+b)/2`，在定点里最自然的实现就是「先按 u4-l1 的 `cl_fix_add` 把它们加起来，再右移一位（除以 2）」。`cl_fix_mean` 正是这么做的——它不是一个新算法，而是 `cl_fix_add` 与 `cl_fix_shift` 的两步组合。

这里有一个**容易被误解**的细节：中间和的格式 `TempFmt` 比两个操作数多一个整数位（这是 `ForAdd` 的 `+1`，用来吸收两个大正数相加的进位）。但均值是「先加再除以 2」，那个 `+1` 整数位**正好被随后的右移一位抵消**。所以最终的无损均值格式，整数位与输入**相同**、小数位**多一位**（因为 `/2` 会多出一个最低小数位）。

换句话说：「均值需要多一个整数位」这句话，**对中间和（TempFmt）成立，对最终结果不成立**——这是本讲要澄清的一个关键点。

#### 4.2.2 核心流程

设 `aFmt = (Sa, Ia, Fa)`、`bFmt = (Sb, Ib, Fb)`：

```
TempFmt = (Sa or Sb, max(Ia, Ib) + 1, max(Fa, Fb))     # 即 ForAdd，多 1 个整数位装和的进位
sum     = cl_fix_add(a, aFmt, b, bFmt, TempFmt, Trunc_s, None_s)   # 无损求和
result  = cl_fix_shift(sum, TempFmt, -1, result_fmt, round, saturate)  # 右移一位 = /2
```

无损均值格式 = `ForShift(TempFmt, −1) = (Sa or Sb, max(Ia,Ib)+1−1, max(Fa,Fb)+1) = (Sa or Sb, max(Ia,Ib), max(Fa,Fb)+1)`。可见整数位回到 `max(Ia,Ib)`（与输入齐平），小数位 `+1`。

数值上也可证明：若 `a, b ∈ [−2^I, 2^I)`，则 `a+b ∈ [−2^(I+1), 2^(I+1))`，再 `/2` 回到 `[−2^I, 2^I)`——均值范围与输入相同，不需要额外整数位。

#### 4.2.3 源码精读

**Python `cl_fix_mean`**：两行核心，与上面的流程一一对应：

[en_cl_fix_pkg.py:378-384](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L378-L384)

```python
def cl_fix_mean(a, aFmt, b, bFmt, rFmt, rnd=Trunc_s, sat=None_s):
    temp_fmt = FixFormat.ForAdd(aFmt, bFmt)                       # 多 1 个整数位
    temp = cl_fix_add(a, aFmt, b, bFmt, temp_fmt, Trunc_s, None_s) # 无损求和
    return cl_fix_shift(temp, temp_fmt, -1, rFmt, rnd, sat)        # 右移一位 = /2
```

**VHDL `cl_fix_mean`** 把 `ForAdd` 内联展开（`max(IntBits)+1`），同样两步：

[en_cl_fix_pkg.vhd:2487-2508](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2487-L2508) —— `TempFmt_c` 在 [L2495-L2500](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2495-L2500) 构造（注意 `IntBits => max(...)+1`），求和在 [L2505](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2505)，右移在 [L2506](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2506)。

```vhdl
constant TempFmt_c : FixFormat_t := (
    Signed   => a_fmt.Signed or b_fmt.Signed,
    IntBits  => max(a_fmt.IntBits, b_fmt.IntBits) + 1,   -- ForAdd 的 +1
    FracBits => max(a_fmt.FracBits, b_fmt.FracBits));
...
temp_v   := cl_fix_add (a, a_fmt, b, b_fmt, TempFmt_c, Trunc_s, None_s);
result_v := cl_fix_shift (temp_v, TempFmt_c, -1, result_fmt, round, saturate);
```

**MATLAB `cl_fix_mean`**：先调用 `cl_fix_constants` 自初始化 `Round`/`Sat`，再同样两步：

[cl_fix_mean.m:38-44](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mean.m#L38-L44)

```matlab
temp_fmt = cl_fix_format(a_fmt.Signed || b_fmt.Signed, ...
                         max(a_fmt.IntBits, b_fmt.IntBits)+1, max(a_fmt.FracBits, b_fmt.FracBits));
temp   = cl_fix_add(a, a_fmt, b, b_fmt, temp_fmt, Round.Trunc_s, Sat.None_s);
result = cl_fix_shift(temp, temp_fmt, -1, result_fmt, round, saturate);
```

三语言实现可逐行互译，构成位真一致性的基础。

#### 4.2.4 代码实践

**目标**：验证「均值 = 先加后右移」，并确认均值的无损结果整数位与输入相同、小数位 `+1`。

**操作步骤**：

```python
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 4)
a, b = 1.5, 2.25                       # 均在 (true,3,4) 中精确

# 无损均值格式 = ForShift(ForAdd(aFmt,aFmt), -1)
temp_fmt = FixFormat.ForAdd(aFmt, aFmt)          # (true, 4, 4)
mean_fmt = FixFormat.ForShift(temp_fmt, -1)      # (true, 3, 5)
print("mean_fmt =", mean_fmt)                    # 预期 (True, 3, 5)：整数位回到 3，小数位 4->5

r = cl_fix_mean(a, aFmt, b, aFmt, mean_fmt, FixRound.Trunc_s, FixSaturate.None_s)
print("mean =", r)                               # 预期 1.875 = (1.5+2.25)/2
```

**预期结果**（手动推导，待本地确认）：

- `ForAdd((true,3,4),(true,3,4)) = (true, 4, 4)`；`ForShift((true,4,4), −1) = (true, 3, 5)`。整数位 `4 → 3`（`+1` 被 `/2` 抵消），小数位 `4 → 5`。
- 均值 `(1.5 + 2.25)/2 = 1.875`，在 `(true,3,5)`（LSB `1/32 = 0.03125`）中精确表示（`1.875 / 0.03125 = 60`），无舍入无饱和。

**需要观察的现象**：把 `mean_fmt` 改回 `(true, 3, 4)`（少一个小数位），`1.875` 在 LSB `0.0625` 下无法精确表示，配合 `Trunc_s` 会得到 `1.8125`（截断），配合 `NonSymPos_s` 会得到 `1.875`（恰好半值向上）——这再次说明误差只来自最后一步 resize。

#### 4.2.5 小练习与答案

**练习 1**：两个 `(true, 3, 4)` 的数求均值，无损结果格式是什么？为什么整数位不需要 `+1`？
**答案**：无损均值格式 `(true, 3, 5)`。中间和格式是 `ForAdd = (true, 4, 4)`（多 1 个整数位装进位），但右移一位 `/2` 把这个 `+1` 抵消，所以最终整数位回到 `3`；`/2` 同时多出一个最低小数位，故小数位 `4 → 5`。

**练习 2**：如果直接用 `cl_fix_add` 求和而不做 `/2`，结果格式与 `cl_fix_mean` 的 `TempFmt` 有何关系？
**答案**：完全相同。`cl_fix_mean` 的 `TempFmt` 就是 `ForAdd(aFmt, bFmt)`，即 `cl_fix_add` 的全精度格式。均值只是在求和之后多接了一个 `shift(−1)`。

---

### 4.3 取反与绝对值：ForNeg 与「最负值」问题

#### 4.3.1 概念说明

`cl_fix_neg`（取反）和 `cl_fix_abs`（绝对值）都面对同一个由补码不对称性带来的角落情况：**最负值 `-2^I` 取反/求绝对值后得到 `+2^I`，它在原格式 `(true, I, F)` 里装不下**（正方向上界只有 `2^I − 2^(-F)`）。

为了保证「对任何合法输入都精确」，精确版 `neg`/`abs` 必须把结果放进一个**多一个整数位**的中间格式 `ForNeg`：

\[
\text{ForNeg}(aFmt) = (\text{true},\ aFmt.IntBits + \mathbb{1}[aFmt.Signed],\ aFmt.FracBits)
\]

- 仅当输入是有符号时才加这个整数位（无符号数不需要取反/绝对值的扩展，且 `neg` 本就要求输入有符号）。
- 结果强制为有符号（`true`），因为取反可能产生负数（如把正数取反），或绝对值需要承载 `+2^I` 这个原本「正方向够不到」的值。

`abs` 的语义是「负数取绝对值、正数原样通过」；`neg` 的语义（按 Doxygen 文档）是「`enable='1'` 时取反、`enable='0'` 时原样通过」——但实现层面有个跨语言陷阱，见 4.3.4。

#### 4.3.2 核心流程

**`cl_fix_abs`**：

```
midFmt = ForNeg(aFmt)                            # 有符号则 +1 整数位
对每个元素：若 a < 0，取 -a（真正的补码取反：按位取反再加 1）；否则原值
return cl_fix_resize(选择后的值, midFmt, result_fmt, round, saturate)
```

**`cl_fix_neg`**（精确版）：

```
midFmt = ForNeg(aFmt)
neg_v = -a            # 真正的补码取反（NOT a + 1）
return cl_fix_resize(neg_v, midFmt, result_fmt, round, saturate)
```

关键：「真正的补码取反」= 按位取反再加 1（`NOT a + 1`）。这个 `+1` 加法器正是 `s` 变体要省掉的硬件。

#### 4.3.3 源码精读

**`ForNeg` 定义**：

[en_cl_fix_types.py:33-36](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L33-L36)

```python
@staticmethod
def ForNeg(aFmt):
    return FixFormat(True, aFmt.IntBits + int(aFmt.Signed), aFmt.FracBits)
```

**Python `cl_fix_abs`**：先取反负数、再按符号选择，全部在 `midFmt = ForNeg` 上进行：

[en_cl_fix_pkg.py:286-294](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L286-L294)

```python
def cl_fix_abs(a, aFmt, rFmt, rnd=Trunc_s, sat=None_s):
    midFmt = FixFormat.ForNeg(aFmt)
    aNeg = cl_fix_neg(a, aFmt, midFmt)          # 负数取反（精确，进 midFmt）
    aPos = cl_fix_resize(a, aFmt, midFmt)       # 正数原样进 midFmt
    a = np.where(a < 0, aNeg, aPos)             # 按符号选择
    return cl_fix_resize(a, midFmt, rFmt, rnd, sat)
```

**VHDL `cl_fix_abs`**：用「前置符号位 + `NOT + 1`」实现真正的补码取反，`TempFmt_c` 即 `ForNeg`：

[en_cl_fix_pkg.vhd:2211-2237](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2211-L2237) —— `TempFmt_c` 在 [L2218-L2223](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2218-L2223)（`IntBits => a_fmt.IntBits + toInteger(a_fmt.Signed)`），取反在 [L2228-L2232](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2228-L2232)。

```vhdl
constant TempFmt_c : FixFormat_t := (
    Signed   => a_fmt.Signed,
    IntBits  => a_fmt.IntBits + toInteger(a_fmt.Signed),   -- ForNeg：有符号才 +1
    FracBits => a_fmt.FracBits);
...
if a_fmt.Signed then
    temp_v := a_v(a_v'high) & a_v;                         -- 前置符号位，腾出额外整数位
    if a_v(a_v'high) = '1' then                            -- 负数
        temp_v := std_logic_vector(unsigned(not temp_v) + 1); -- 真正的补码取反：NOT + 1
    end if;
else
    temp_v := a_v;                                          -- 无符号：原样
end if;
return cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate);
```

注意 `not temp_v + 1` 正是补码取反的标准实现，这里的 `+1` 加法器就是精确版相对 `s` 版多出的硬件成本。

**MATLAB `cl_fix_abs`**：直接用内置 `abs`，结果放进 `IntBits+1` 的 `temp_fmt`：

[cl_fix_abs.m:37-44](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_abs.m#L37-L44)

```matlab
if a_fmt.Signed
    temp_fmt = cl_fix_format(a_fmt.Signed, a_fmt.IntBits+1, a_fmt.FracBits);
    result   = cl_fix_resize(abs(a), temp_fmt, result_fmt, round, saturate);
else
    result   = cl_fix_resize(a, a_fmt, result_fmt, round, saturate);
end
```

#### 4.3.4 代码实践

**目标**：亲手验证「最负值取反需要 `ForNeg` 的额外整数位」。

**操作步骤**：

```python
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 2, 2)            # 范围 -4 .. 3.75，最负值 = -4
print("ForNeg =", FixFormat.ForNeg(aFmt))   # 预期 (True, 3, 2)：多 1 个整数位

# 最负值 -4 取反得 +4，原格式 (true,2,2) 装不下（上界 3.75）
r_sat = cl_fix_neg(-4.0, aFmt, FixFormat(True, 2, 2), FixRound.Trunc_s, FixSaturate.Sat_s)
r_wrap = cl_fix_neg(-4.0, aFmt, FixFormat(True, 2, 2), FixRound.Trunc_s, FixSaturate.None_s)
print("sat =", r_sat, " wrap =", r_wrap)
```

**预期结果**（手动推导，待本地确认）：

- `ForNeg((true,2,2)) = (true, 3, 2)`（宽 6 位）。
- `-4` 取反 = `+4`，在 `ForNeg` 的 `(true,3,2)` 里位串是 `010000`（`+4 × 4 = 16`）。结果格式 `(true,2,2)`（宽 5 位，上界 `3.75`）：
  - `Sat_s`：`+4 > 3.75`，饱和到 `3.75`。
  - `None_s`：回绕——取 `010000` 的低 5 位得 `10000`，按 `(true,2,2)` 补码读为 `-16 × 2^-2 = -4.0`，即**回绕回自身**。这正是「最负值取反在原格式装不下」的直接体现。

> 与之对照，单元测试 [en_cl_fix_pkg_test.py:491-495](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L491-L495) 用 `-4.0` 在 `(true,2,4)` 上取反到 `(true,2,2)`，分别断言 `Sat_s → 3.75`、`None_s → -4.0`（回绕回自身）——可作为你本地结果的参照基准。

**关于 `enable` 的跨语言陷阱**（重要）：Doxygen 文档 [en_cl_fix_pkg.vhd:742-756](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L742-L756) 声称 `cl_fix_neg` 在 `enable='1'` 时取反、`enable='0'` 时原样通过。但实际实现并不一致：

- **MATLAB** `cl_fix_neg` [cl_fix_neg.m:44](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_neg.m#L44) 用 `a.*(-1).^(enable~=0)` **确实遵循 enable**（条件取反）。
- **VHDL** `cl_fix_neg` [en_cl_fix_pkg.vhd:2271-2285](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2271-L2285) 虽然签名里有 `enable`，但函数体**无条件**计算 `Neg_v := -signed(AFull_v)`，**忽略了 enable**（永远取反）。
- **Python** `cl_fix_neg` [en_cl_fix_pkg.py:301-310](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L301-L310) 干脆**没有 enable 参数**，永远取反。

结论：**如果你依赖「`enable='0'` 时原样通过」这一行为，目前只有 MATLAB 给出文档承诺的结果**；VHDL/Python 的 `cl_fix_neg` 总是取反。需要条件取反时，建议显式用 `np.where(enable, cl_fix_neg(a,...), a)` 自行选择。（注：`s` 变体 `cl_fix_sneg` 在三种语言里都正确遵循 enable，见 4.4。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ForNeg` 只在有符号输入时才加一个整数位？
**答案**：无符号数没有负数，「最负值取反溢出」的问题不存在；而且 `neg` 要求输入有符号、`abs` 对无符号数是恒等操作，都不需要额外整数位。`ForNeg` 中 `IntBits + int(aFmt.Signed)` 正是「有符号才 +1」。

**练习 2**：`cl_fix_abs` 对一个 `(true, 2, 2)` 的 `-4.0`（最负值）求绝对值，精确结果应放进什么格式？
**答案**：`ForNeg((true,2,2)) = (true, 3, 2)`，范围 `-8 .. 7.75`，能装下 `|-4| = 4`。若结果格式小于此（如仍用 `(true,2,2)`，上界 `3.75`），则需配合 `Sat_s` 饱和到 `3.75`，或 `None_s` 回绕。单元测试 [en_cl_fix_pkg_test.py:473-474](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L473-L474) 即验证此例（`Sat_s → 3.75`）。

---

### 4.4 资源优化变体：cl_fix_sneg 与 cl_fix_sabs

#### 4.4.1 概念说明

`s` 前缀（simple / saving）变体是 en_cl_fix 里一类重要的工程取舍：**用「按位取反（`NOT`）」替代「补码取反（`NOT + 1`）」，省掉那个 `+1` 加法器，并因此不再需要 `ForNeg` 的额外整数位**，从而在 FPGA 上换来更小的面积和更短的时序路径。代价是：对负数（或被取反的数），结果会比精确值**少 1 个 LSB**。

数学上，补码取反与按位取反的关系是：

\[
-x = (\text{NOT}\ x) + 1 \quad\Longrightarrow\quad \text{NOT}\ x = -x - 1
\]

也就是说，「只取反不加一」得到的值比真正的 `-x` 小了正好 1 个 LSB。对于绝对值：负数 `a` 的精确绝对值是 `-a`，而 `s` 变体给出 `NOT a = -a - 1`，即 `|a| - 1`（少 1 LSB）。正数则原样通过，没有误差。

| 函数 | 实现 | 额外整数位 | 负数误差 | 硬件成本 |
|------|------|-----------|---------|---------|
| `cl_fix_abs` / `cl_fix_neg` | `NOT + 1`（真补码取反） | 需要（`ForNeg`） | 0（精确） | 多一个加法器 + 一位 |
| `cl_fix_sabs` / `cl_fix_sneg` | `NOT`（仅按位取反） | 不需要 | ≤ 1 LSB | 仅取反器，更省 |

这与 u4-l1 讲过的 `cl_fix_saddsub`（减法时把 `b` 按位取反再做加法，省掉补码的 `+1`）是完全相同的设计思想——「**牺牲最多 1 LSB 精度，换取面积/时序**」。

#### 4.4.2 核心流程

**`cl_fix_sneg`**（条件取反，遵循 enable）：

```
temp_fmt = (aFmt.Signed, aFmt.IntBits, max(aFmt.FracBits, result_fmt.FracBits))   # 注意：无额外整数位
temp = cl_fix_resize(a, aFmt, temp_fmt, Trunc_s, None_s)
若 enable：temp = NOT temp                          # 按位取反，不加一
return cl_fix_resize(temp, temp_fmt, result_fmt, round, saturate)
```

**`cl_fix_sabs`**：等价于 `cl_fix_sneg(a, aFmt, enable=(a<0), ...)`——负数（`a<0` 为真）时取反，正数时原样通过。

#### 4.4.3 源码精读

**Python `cl_fix_sneg`**：用 `(-1)**enable` 实现「条件取反」，并减去 `enable × LSB` 体现「不加一」：

[en_cl_fix_pkg.py:312-325](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L312-L325)

```python
def cl_fix_sneg(a, aFmt, enable, rFmt, rnd=Trunc_s, sat=None_s):
    enable = np.array(enable)
    temp_fmt = FixFormat(True, aFmt.IntBits, max(aFmt.FracBits, rFmt.FracBits))  # 无额外整数位
    temp = cl_fix_resize(a, aFmt, temp_fmt, Trunc_s, None_s)
    ...
    # enable=1: temp = -temp - LSB  （等价于 NOT，比真取反少 1 LSB）
    # enable=0: temp = temp          （原样通过）
    temp = -(enable.astype(int))*2 ** -temp_fmt.FracBits + (-1.0) ** enable.astype(int)*temp
    return cl_fix_resize(temp, temp_fmt, rFmt, rnd, sat)
```

**Python `cl_fix_sabs`**：一行，把 enable 设为「是否为负数」：

[en_cl_fix_pkg.py:296-299](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L296-L299)

```python
def cl_fix_sabs(a, aFmt, rFmt, rnd=Trunc_s, sat=None_s):
    return cl_fix_sneg(a, aFmt, a < 0, rFmt, rnd, sat)
```

**VHDL `cl_fix_sneg`**：先断言「不能对无符号数取反」，再 `NOT`（无 `+1`），`TempFmt_c` 无额外整数位：

[en_cl_fix_pkg.vhd:2289-2316](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2289-L2316)

```vhdl
assert a_fmt.Signed report "cl_fix_sneg : Cannot negate an unsigned value." severity failure;
...
temp_v := cl_fix_resize(a, a_fmt, TempFmt_c, Trunc_s, None_s);
if to01(enable) = '1' then
    temp_v := not temp_v;          -- 仅按位取反，没有 +1
end if;
result_v := cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate);
```

**VHDL `cl_fix_sabs`**：符号位为 1 时取反（`NOT`），否则原样；注意 `TempFmt_c` 的 `IntBits` 不再 `+1`：

[en_cl_fix_pkg.vhd:2241-2267](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2241-L2267)

```vhdl
constant TempFmt_c : FixFormat_t := (
    Signed   => a_fmt.Signed,
    IntBits  => a_fmt.IntBits,                          -- 无额外整数位
    FracBits => max(a_fmt.FracBits, result_fmt.FracBits));
...
if a_fmt.Signed then
    temp_v := cl_fix_resize(a, a_fmt, TempFmt_c, Trunc_s, None_s);
    if temp_v(temp_v'high) = '1' then                   -- 负数
        temp_v := not temp_v;                            -- NOT，无 +1 -> 少 1 LSB
    end if;
    ...
```

#### 4.4.4 代码实践

**目标**：对比 `cl_fix_abs` 与 `cl_fix_sabs`，亲眼看到负数结果差 1 LSB。

**操作步骤**：

```python
from en_cl_fix_pkg import *

aFmt = FixFormat(True, 3, 4)            # LSB = 0.0625
rFmt = FixFormat(True, 3, 4)
a    = -4.0                             # 负数，在 (true,3,4) 内（范围 -8 .. 7.9375）

abs_exact = cl_fix_abs (a, aFmt, rFmt, FixRound.Trunc_s, FixSaturate.None_s)
abs_save  = cl_fix_sabs(a, aFmt, rFmt, FixRound.Trunc_s, FixSaturate.None_s)
print("abs  =", abs_exact)
print("sabs =", abs_save)
print("diff =", abs_exact - abs_save)
```

**预期结果**（按源码手动推导，待本地确认）：

- `-4.0` 在 `(true,3,4)` 的 8 位位串是 `11000000`（`-4.0 × 16 = -64`，8 位补码 `-64 = 0b11000000`）。
- **`cl_fix_abs`**（`NOT + 1`）：真补码取反 → `+4.0`，位串 `01000000`。
- **`cl_fix_sabs`**（仅 `NOT`）：`NOT(11000000) = 00111111`，按 `(true,3,4)` 读为 `+63 × 2^-4 = 3.9375`。
- 差值 `4.0 - 3.9375 = 0.0625`，**正好 1 个 LSB**。✓

**需要观察的现象**：把 `a` 改成正数（如 `+4.0`），`abs` 与 `sabs` 结果应**完全相同**（正数路径不取反，无误差）；只有负数才差 1 LSB。这与单元测试 [en_cl_fix_pkg_test.py:747-752](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L747-L752) 的断言一致：`sabs(2.25)=2.25`（正数无损），`sabs(-2.25)=2.0`（负数差 `0.25 = 1 LSB`，因结果格式 `(false,2,2)` 的 LSB=`0.25`）。

> 若本地环境暂不可用，以上为依据源码 `NOT`（无 `+1`）逻辑推导的预期，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：用一句话概括 `s` 变体（`sneg`/`sabs`/`saddsub`）的共同设计取舍。
**答案**：用「按位取反 `NOT`」替代「补码取反 `NOT+1`」，省掉一个加法器和一个额外整数位，换取面积/时序优化，代价是被取反（减法/负数）的结果最多差 1 LSB。

**练习 2**：为什么 `cl_fix_sneg` 的 `TempFmt` 不需要 `ForNeg` 那个额外整数位？
**答案**：`ForNeg` 的额外整数位是为了容纳「最负值 `-2^I` 真正取反后的 `+2^I`」。而 `sneg` 只做 `NOT`，得到的是 `-x - 1`，永远不会超出原格式的正范围（最负值 `NOT` 后得到 `2^I - 1` 个 LSB 的正值，恰在原范围内），所以不需要额外整数位——这也正是它能省资源的原因之一。

**练习 3**：`cl_fix_sabs(-2.25, (true,3,3), (false,2,2))` 的预期结果是多少？为什么？
**答案**：`2.0`。`(false,2,2)` 的 LSB = `0.25`。精确 `|-2.25| = 2.25`，但 `sabs` 对负数只做 `NOT`，少 1 LSB → `2.25 - 0.25 = 2.0`。这正是测试 [en_cl_fix_pkg_test.py:751-752](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L751-L752) 的断言。

---

## 5. 综合实践

把本讲四个主题串起来，完成下面这个「定点信号缩放 + 求差绝对值」的小任务，模拟一个常见的 DSP 子模块。

**场景**：信号 `x` 在 `(true, 3, 4)` 格式中，参考值 `ref` 在 `(true, 2, 4)`。要求计算 `y = mean(|x|, |ref|)` 的近似值，结果放进 `(true, 4, 4)`，并全程使用资源友好型实现。

**操作步骤**：

```python
from en_cl_fix_pkg import *

xFmt   = FixFormat(True, 3, 4)
refFmt = FixFormat(True, 2, 4)
yFmt   = FixFormat(True, 4, 4)

x   = -3.5      # 在 xFmt 内
ref =  1.25     # 在 refFmt 内

# 第 1 步：用 sabs 取绝对值（省资源，注意负数差 1 LSB）
ax  = cl_fix_sabs(x,   xFmt,   FixFormat(False, 4, 4), FixRound.Trunc_s, FixSaturate.Sat_s)
are = cl_fix_sabs(ref, refFmt, FixFormat(False, 4, 4), FixRound.Trunc_s, FixSaturate.Sat_s)
print("|x|_s  =", ax, " |ref|_s =", are)

# 第 2 步：用 cl_fix_mean 求均值（先加后右移一位）
y = cl_fix_mean(ax, FixFormat(False,4,4),
                are, FixFormat(False,4,4),
                yFmt, FixRound.NonSymPos_s, FixSaturate.Sat_s)
print("y =", y)
```

**跟踪要点**（手动推导，待本地确认）：

1. **`sabs` 步**：`x = -3.5` 是负数，`sabs` 给出 `|-3.5| - 1 LSB = 3.5 - 0.0625 = 3.4375`（结果格式 `(false,4,4)` 的 LSB=`0.0625`）。`ref = 1.25` 是正数，`sabs` 原样通过 = `1.25`。这里体现了 4.4 的 1 LSB 代价。
2. **`mean` 步**：`TempFmt = ForAdd = (false, 5, 4)`；和 `3.4375 + 1.25 = 4.6875`；右移一位 `/2` → 无损均值格式 `(false, 4, 5)` 中的 `2.34375`；最后 resize 到 `(true,4,4)`（LSB `0.0625`），用 `NonSymPos_s` 舍入。请本地运行确认最终 `y` 的舍入结果。
3. **对照精确版**：把 `cl_fix_sabs` 换成 `cl_fix_abs`，重跑一遍，比较两个 `y` 是否相差（最多）1 LSB——这正是「资源优化」引入的端到端误差。

**需要观察的现象**：

- `mean` 内部 `TempFmt` 的整数位 `+1` 在右移后被抵消，最终结果 `(true,4,4)` 的整数位 `4` 足够装下均值（均值范围与输入齐平）。
- 若把 `yFmt` 改成 `(true, 3, 4)`（整数位不足），配合 `Sat_s` 会观察到饱和。

> 本实践涉及多步舍入，端到端数值建议本地运行确认；以上中间值为按各函数源码逻辑逐步推导的预期。

## 6. 本讲小结

- **移位是格式变换，不是截断**：`cl_fix_shift` 先把 `a × 2^shift` 无损放进 `ForShift(aFmt, shift) = (Signed, I+shift, F−shift)`，再由最后一步 `cl_fix_resize` 决定舍入/饱和。常数移位下总位宽不变，左移只是把位从小数侧搬到整数侧。
- **均值 = 先加后右移**：`cl_fix_mean = cl_fix_add(...) → cl_fix_shift(−1)`。中间和沿用 `ForAdd` 的 `+1` 整数位，但被随后的 `/2` 抵消，故均值的整数位与输入齐平、小数位 `+1`。
- **取反/绝对值要给最负值留位**：`ForNeg = (true, I + 有符号?1:0, F)`。精确版用真补码取反 `NOT + 1`，能正确处理 `-2^I → +2^I`。
- **`s` 变体用精度换资源**：`sneg`/`sabs`（以及 u4-l1 的 `saddsub`）用 `NOT` 替代 `NOT+1`，省掉加法器和额外整数位，代价是被取反/负数结果最多差 1 LSB。
- **`enable` 跨语言陷阱**：`cl_fix_neg` 的 `enable` 仅 MATLAB 遵循；VHDL 接受但忽略（恒取反），Python 无此参数。需要条件取反时请显式 `np.where`，或改用三种语言都遵循 enable 的 `cl_fix_sneg`。
- **统一架构再印证**：`shift`、`mean`、`neg`、`abs`、`sneg`、`sabs` 每一个的最后一步都是 `cl_fix_resize`——全库所有运算最终都汇聚到它。

## 7. 下一步学习建议

- **横向对照 `s` 变体家族**：回到 u4-l1 重读 `cl_fix_saddsub`，把它与本讲的 `sneg`/`sabs` 放在一起，总结「按位取反换 1 LSB」这一统一手法的适用边界（何时安全、何时不可接受）。
- **进入文件 IO 与位真交换（Unit 5）**：本讲的 `cl_fix_get_bits_as_int` / `cl_fix_from_bits_as_int`（u3-l1 已提及）是把 `shift`/`mean` 的结果跨语言搬运的桥梁，下一讲将系统讲文件读写。
- **攻坚大位宽（Unit 6）**：当 `shift` 让位宽超过 53 位、或 `mean` 的中间和过宽时，Python 会经 `cl_fix_is_wide` 派发到 `wide_fxp`——那里的「换格式标签即移位」实现（[en_cl_fix_pkg.py:402-425](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L402-L425)）是理解任意精度定点的好材料。
- **架构总结（Unit 7）**：届时可把 `TempFmt` 构造模式（`ForShift`/`ForAdd`/`ForMult`/`ForNeg`）统一成一张「位增长规则表」，并结合 `-- synthesis translate_off` 理解 `Warn_s` 分支如何被排除在综合之外。
