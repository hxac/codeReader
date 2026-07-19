# 乘法 cl_fix_mult

## 1. 本讲目标

本讲承接 [u4-l1（加减法）](u4-l1-add-sub.md)建立的「中间全精度格式 → 精确运算 → `cl_fix_resize`」统一架构，把它落到定点乘法上。学完后你应当能够：

- 用 `FixFormat.ForMult` 手算两个定点格式相乘后的**精确全精度结果格式**，并解释整数位「相加再 +1」、小数位「直接相加」的来源。
- 读懂 VHDL `cl_fix_mult` 中按 a/b 是否有符号分出的**四种乘法分支**，尤其是用 `"0" &` 把无符号数零扩展成非负补码数再复用有符号乘法器的技巧。
- 理解 Python / MATLAB 端为何只需一行 `a * b` / `a .* b`，以及 Python 在位宽 > 53 时如何经 `cl_fix_is_wide` 派发到 `wide_fxp`。
- 说清楚「乘法本身在中间格式上是精确的，误差只来自最后一步 resize 的舍入/饱和」，并因此理解「乘法后优先用 `Trunc_s`」的资源建议。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **定点格式 `[S, I, F]`**（[u1-l2](u1-l2-fixformat-type.md)）：数值 \(V = N \cdot 2^{-F}\)，总位宽 \(W = S + I + F\)；有符号采用二进制补码。
- **舍入 `FixRound` 与饱和 `FixSaturate`**（[u1-l4](u1-l4-rounding-modes.md)、[u1-l5](u1-l5-saturation-modes.md)）：决定量化网格点归属与溢出处理。
- **`cl_fix_resize` 的舍入与饱和**（[u3-l2](u3-l2-resize-rounding.md)、[u3-l3](u3-l3-resize-saturation.md)）：丢小数位先加偏移再截断，丢整数位按模式饱和或回绕。
- **加减法的统一模式**（[u4-l1](u4-l1-add-sub.md)）：先构造能无损装下精确结果的中间格式 `ForAdd/ForSub`，两操作数无损扩展后在中间格式上做精确加减，最后 resize。

补充一个本讲要用到的乘法位宽常识：两个 \(W_a\) 位与 \(W_b\) 位的整数相乘，乘积最多 \(W_a + W_b\) 位即可无损表示——这是补码乘法器的经典结论，`ForMult` 的位增长规则正是它的定点版本。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [python/src/en_cl_fix_pkg/en_cl_fix_types.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py) | `FixFormat.ForMult` 静态方法：计算乘法的全精度结果格式。 |
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | `cl_fix_mult` 函数体：构造 `TempFmt_c`，按四种符号组合做精确乘法，再 resize；文件头 doxygen 注释里有一个完整乘法示例。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | Python `cl_fix_mult`：实数域 `a * b`，位宽过大时派发到 `wide_fxp`。 |
| [matlab/src/cl_fix_mult.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mult.m) | MATLAB `cl_fix_mult`：`a .* b` 逐元素相乘后 resize。 |

## 4. 核心概念与源码讲解

### 4.1 FixFormat.ForMult：全精度乘积格式

#### 4.1.1 概念说明

乘法与加减法一样，遵循 [u4-l1](u4-l1-add-sub.md) 的统一架构：**先算出一个能无损装下精确乘积的中间格式，把两操作数扩展上去做精确乘法，最后由 `cl_fix_resize` 统一完成舍入与饱和**。这个中间格式由 `FixFormat.ForMult` 给出。

为什么乘积的格式会「变大」？直觉有三点：

1. **小数位相加**：\(a\) 的分辨率是 \(2^{-F_a}\)，\(b\) 是 \(2^{-F_b}\)。乘积的最小步长是 \(2^{-F_a} \cdot 2^{-F_b} = 2^{-(F_a+F_b)}\)，所以 \(F_{\text{prod}} = F_a + F_b\)。
2. **整数位相加**：两个数的整数部分量级相乘，整数位数量级相加，所以基础值是 \(I_a + I_b\)。
3. **再 +1 个整数位**：这是补码乘法的「角落情况」——两个最负值 \((-2^{I_a}) \cdot (-2^{I_b}) = +2^{I_a+I_b}\) 是正数，恰好需要比 \(I_a+I_b\) 多一个整数位才能装下。这个 +1 同时也充当结果的有符号位。

关键性质：**只要任一操作数有符号，乘积就可能为负，结果必须有符号**；只有两个无符号数相乘，结果才是无符号的。

#### 4.1.2 核心流程

VHDL 与 MATLAB 采用**紧形式**（最小且恰好够用的全精度格式）：

\[
\text{ForMult}(a,b) = \big(\, S_a \lor S_b,\;\; I_a + I_b + \mathbb{1}(S_a \lor S_b),\;\; F_a + F_b \,\big)
\]

其中 \(\mathbb{1}(\cdot)\) 把布尔值映射为 0/1。它的总位宽恰好等于两操作数位宽之和：

\[
W_{\text{prod}} = \underbrace{(S_a \lor S_b)}_{S} + \underbrace{(I_a + I_b + (S_a \lor S_b))}_{I} + \underbrace{(F_a + F_b)}_{F} = W_a + W_b
\]

这正是「\(W_a\) 位乘 \(W_b\) 位得 \(W_a+W_b\) 位」的定点实现。

Python 采用**统一形式**（永远有符号、永远 +1）：

\[
\text{ForMult}_{\text{py}}(a,b) = \big(\,\text{True},\;\; I_a + I_b + 1,\;\; F_a + F_b \,\big)
\]

两种形式的差别只在「够用」的宽松程度：

| 情形 | 紧形式（VHDL/MATLAB） | Python 统一形式 | 关系 |
| --- | --- | --- | --- |
| 有符号 × 有符号 | (True, \(I_a+I_b+1\), \(F_a+F_b\)) | (True, \(I_a+I_b+1\), \(F_a+F_b\)) | 完全相同（恰好紧形式） |
| 有符号 × 无符号 | (True, \(I_a+Ib+1\), …) | (True, \(I_a+I_b+1\), …) | 相同 |
| 无符号 × 无符号 | (False, \(I_a+I_b\), …) | (True, \(I_a+I_b+1\), …) | Python 多 2 位（仍是超集） |

> 结论：Python 的统一形式永远是一个**无损超集**——对于「两个都有符号」它和紧形式完全一致，对其它情形只是多留了若干高位（这些高位恒为 0）。这些多余的高位会在最后的 `cl_fix_resize` 里被自然截掉，因此**两种形式产生位真一致的结果**。VHDL 作为要综合成硬件的实现，选择了更紧的形式以节省中间信号位宽；Python 作为软件参考模型，选择了更简单的统一公式。

#### 4.1.3 源码精读

Python 的 `ForMult`，统一形式一目了然：

[python/src/en_cl_fix_pkg/en_cl_fix_types.py:28-31](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_types.py#L28-L31) — 永远返回 `Signed=True`、`IntBits` 相加后再 `+1`、`FracBits` 直接相加。

VHDL 的紧形式写在 `cl_fix_mult` 体内的常量 `TempFmt_c` 里：

[vhdl/src/en_cl_fix_pkg.vhd:2586-2592](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2586-L2592) — `TempSigned_c := a_fmt.Signed or b_fmt.Signed`；`IntBits` 只在 `TempSigned_c` 为真时才 `+1`（`toInteger(TempSigned_c)`）；`FracBits` 相加。

MATLAB 同样是紧形式：

[matlab/src/cl_fix_mult.m:40-42](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mult.m#L40-L42) — `signed = a_fmt.Signed || b_fmt.Signed`，`IntBits` 加 `signed`（逻辑值作 0/1），与 VHDL 逐字符对应。

#### 4.1.4 代码实践

**目标**：验证 `ForMult` 的位增长公式与「总位宽 = 两操作数位宽之和」性质。

**操作步骤**（在 `python/unittest` 目录下，或把 `python/src` 加入 `sys.path`）：

```python
# 示例代码
from en_cl_fix_pkg import FixFormat

cases = [
    (FixFormat(True,  3, 5), FixFormat(True, 2, 8)),   # 有符号 × 有符号
    (FixFormat(False, 3, 5), FixFormat(False, 2, 8)),  # 无符号 × 无符号
    (FixFormat(True,  3, 5), FixFormat(False, 2, 8)),  # 有符号 × 无符号
]
for a, b in cases:
    p = FixFormat.ForMult(a, b)
    print(a, "*", b, "->", p, "  Wprod =", p.width(),
          "  Wa+Wb =", a.width() + b.width())
```

**需要观察的现象**：

1. 有符号 × 有符号：`IntBits = 3+2+1 = 6`，`FracBits = 5+8 = 13`，`Wprod = 20 = 9+11 = Wa+Wb`。
2. 无符号 × 无符号：`IntBits = 3+2+1 = 6`（Python 仍 +1），`FracBits = 13`，`Wprod = 20`，而 `Wa+Wb = 8+10 = 18`——Python 多了 2 位（即上文所说的无损超集）。
3. 任一操作数有符号时结果 `Signed=True`。

**预期结果**：三组都打印出 `(True, 6, 13)`；前两组的 `Wa+Wb` 分别为 20、18，可直观看到 Python 统一形式在无符号情形下的宽松。

#### 4.1.5 小练习与答案

**练习 1**：`FixFormat(True,2,3)` 与自身相乘，`ForMult` 给出什么格式？总位宽是多少？

**答案**：`(True, 2+2+1, 3+3) = (True, 5, 6)`，位宽 \(1+5+6=12\)，恰好等于 \(W_a+W_b = 6+6 = 12\)。

**练习 2**：为什么无符号 × 无符号时，紧形式不需要那个 +1？

**答案**：无符号数没有「两个最负值相乘得大正数」的补码角落情况，乘积范围是 \([0,\; (2^{I_a})(2^{I_b})) = [0,\; 2^{I_a+I_b})\)，用 \(I_a+I_b\) 个无符号整数位即可精确装下，不需要额外符号/进位位。

---

### 4.2 cl_fix_mult 的 VHDL 实现：四种符号组合

#### 4.2.1 概念说明

VHDL 把定点数存为 `std_logic_vector` 位串，没有「小数点」的概念，乘法时必须由我们告诉仿真器/综合器：这两个位串到底该按有符号补码（`signed`）还是无符号（`unsigned`）解释。a、b 各有两种可能，于是有四种符号组合。难点在于**混合符号**（一个有符号、一个无符号）时如何正确相乘。

VHDL 标准库（`numeric_std`）只提供同类型相乘：`signed * signed` 与 `unsigned * unsigned`。`cl_fix_mult` 的做法是：**当一边是无符号时，在它最高位前补一个 `'0'`，把它零扩展成一个「非负的有符号数」，然后统一走 `signed * signed`**。由于补的是 0，这个非负 signed 数的数值与原 unsigned 数完全相等，乘积也就正确。

#### 4.2.2 核心流程

```
输入: a (a_fmt), b (b_fmt), result_fmt, round, saturate

1. TempFmt_c = ForMult(a_fmt, b_fmt)            # 全精度中间格式
2. temp_v := <按 a_fmt.Signed, b_fmt.Signed 四选一的精确乘积>  # 位宽 = W_a+W_b
3. result_v := cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate)
4. return result_v
```

四种符号组合对应的精确乘法（伪代码）：

| a_fmt.Signed | b_fmt.Signed | 使用的乘法 | 说明 |
| --- | --- | --- | --- |
| 真 | 真 | `signed(a) * signed(b)` | 标准补码乘 |
| 真 | 假 | `signed(a) * signed("0" & b)` | b 零扩展成非负 signed |
| 假 | 真 | `signed("0" & a) * signed(b)` | a 零扩展成非负 signed |
| 假 | 假 | `unsigned(a) * unsigned(b)` | 标准无符号乘，结果无符号 |

注意第 4 行的结果天然没有符号位，与 `TempFmt_c.Signed = false` 相符；前三行结果都是有符号位串，与 `TempFmt_c.Signed = true` 相符。

#### 4.2.3 源码精读

函数声明（默认 `round = Trunc_s`、`saturate = Warn_s`）：

[vhdl/src/en_cl_fix_pkg.vhd:974-981](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L974-L981) — 入口签名与默认参数。

四分支精确乘法，核心就是上文那张表的直译：

[vhdl/src/en_cl_fix_pkg.vhd:2600-2612](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2600-L2612) — `if a_fmt.Signed then if b_fmt.Signed then ... else ...` 的嵌套 `if`；混合符号两支用 `"0" & signed(...)` 做零扩展。

最后一步 resize，把全精度乘积量化到目标格式：

[vhdl/src/en_cl_fix_pkg.vhd:2613](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2613) — `result_v := cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate);`，所有舍入/饱和都在这里发生。

文件头 doxygen 注释里有一个完整可读的乘法示例（也是本讲综合实践的依据）：

[vhdl/src/en_cl_fix_pkg.vhd:91-95](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L91-L95) — 用 `OpAFmt_c = (true,3,5)`、`OpBFmt_c = (true,2,8)`，并**手写**了 ForMult 公式来构造 `ResFmt_c`：`Signed` 取或、`IntBits` 相加 +1、`FracBits` 相加。

[vhdl/src/en_cl_fix_pkg.vhd:102-106](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L102-L106) — `cl_fix_from_real` 装入 `-3.134` 与 `0.1`，再用 `cl_fix_mult(..., Trunc_s, None_s)` 相乘。

#### 4.2.4 代码实践

**目标**：源码阅读型实践——跟踪四种符号组合分别走哪个分支，理解 `"0" &` 的作用。

**操作步骤**：

1. 打开 [vhdl/src/en_cl_fix_pkg.vhd:2600-2612](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2600-L2612)。
2. 对下表四种 `(a_fmt.Signed, b_fmt.Signed)` 组合，分别指出命中的分支、所用的乘法类型，以及是否出现 `"0" &`：

   | a 有符号 | b 有符号 | 命中行 | 是否零扩展 |
   | --- | --- | --- | --- |
   | 是 | 是 | 2602 | 否 |
   | 是 | 否 | 2604 | 是（扩展 b） |
   | 否 | 是 | 2608 | 是（扩展 a） |
   | 否 | 否 | 2610 | 否 |

3. 思考：为什么不直接写 `unsigned(a) * unsigned(b)` 来处理「有符号 × 无符号」？因为这样会丢掉 a 的符号位、把负数当成大正数，乘积错误。必须把 a 留作 `signed`、把 b 提升为非负 `signed`，才能保留 a 的负值语义。

**需要观察的现象**：四分支刚好覆盖全部组合，且 `TempFmt_c.Signed` 与「是否走 unsigned 分支」一一对应——只有第 4 行结果无符号。

**预期结果**：你能用一句话复述「混合符号时，给无符号那一边补一个 0 符号位，复用 signed 乘法器」这一核心技巧。

#### 4.2.5 小练习与答案

**练习 1**：`signed("0" & b)` 中那个 `"0"` 为什么必须补在**最高位之前**而不是最低位之后？

**答案**：补码的符号位是最高位。把 `'0'` 补在最高位前，等于给原无符号数加了一个值为 0 的符号位，得到一个非负的补码数，其数值与原数相等；若补在最低位后则相当于左移一位（乘 2），数值就错了。

**练习 2**：第 4 行 `unsigned(a) * unsigned(b)` 的结果位宽是多少？它和 `TempFmt_c` 的位宽吻合吗？

**答案**：`numeric_std` 中两个无符号位串相乘结果位宽为 `a'length + b'length`，而 `TempFmt_c`（无符号情形）位宽也是 \(W_a + W_b\)，二者吻合，赋值给 `temp_v` 不会截断或报错。

---

### 4.3 cl_fix_mult 的 Python 与 MATLAB 实现：实数域直接相乘

#### 4.3.1 概念说明

Python 与 MATLAB 把定点数存为 `double` 浮点（实数域），本就带有「真正的小数点」，所以**不需要区分四种符号组合**——直接 `a * b`（Python）或 `a .* b`（MATLAB，支持逐元素向量）即可得到精确乘积。符号、小数点对齐都由浮点运算自动处理。

唯一需要小心的是**精度边界**：IEEE754 双精度只有 53 位有效尾数（见 [u6-l1](u6-l1-narrow-wide-dispatch.md)）。当 `ForMult(aFmt,bFmt)` 给出的中间格式位宽超过 53 位时，浮点已无法精确表示每一个 LSB，Python 必须切换到任意精度整数路径 `wide_fxp`。

#### 4.3.2 核心流程

Python `cl_fix_mult` 流程：

```
1. midFmt = FixFormat.ForMult(aFmt, bFmt)
2. 若 a 或 b 已是 wide_fxp，或 cl_fix_is_wide(midFmt) 为真:
      把 a, b 都转换成 wide_fxp（任意精度整数路径）
3. return cl_fix_resize(a * b, midFmt, rFmt, rnd, sat)
```

MATLAB 流程几乎相同，只是没有 wide 分支（MATLAB 是功能子集，不做 > 52 位）：

```
1. signed  = a_fmt.Signed || b_fmt.Signed
2. temp_fmt = cl_fix_format(signed, a_fmt.IntBits+b_fmt.IntBits+signed, ...)
3. result = cl_fix_resize(a .* b, temp_fmt, result_fmt, round, saturate)
```

三语言最终都汇聚到 `cl_fix_resize`——再次印证 u4-l1 的结论：**所有运算的精度损失只发生在最后那一次 resize**。

#### 4.3.3 源码精读

Python 实现，注意 wide 派发与简洁的 `a * b`：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:430-439](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L430-L439) — `midFmt = FixFormat.ForMult(aFmt, bFmt)`；当任一操作数是 `wide_fxp`、或 `cl_fix_is_wide(midFmt)` 为真时，把两操作数都经 `wide_fxp.FromFxp` 转成任意精度整数；最后 `cl_fix_resize(a * b, midFmt, rFmt, rnd, sat)`。默认 `rnd=Trunc_s`、`sat=None_s`（与 VHDL 默认 `Warn_s` 不同，跨语言调用应显式传 `sat`，见 [u1-l5](u1-l5-saturation-modes.md)）。

53 位边界判定函数：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:23-30](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L23-L30) — `cl_fix_is_wide` 注释里点明 IEEE754 double 只有 52 位显式尾数 + 1 位隐含位 = 53 位有效整数，超过即必须走 wide。

MATLAB 实现，逐元素相乘后 resize：

[matlab/src/cl_fix_mult.m:38-42](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_mult.m#L38-L42) — 三行：算 `signed`、构造 `temp_fmt`、`cl_fix_resize(a .* b, ...)`。文件头注释（第 19 行）提醒：调用前必须先执行 `cl_fix_constants` 建立 `Sat`/`Round` 常量（见 [u2-l3](u2-l3-matlab-model.md)）。

#### 4.3.4 代码实践（本讲主实践）

**目标**：用 VHDL doxygen 示例的格式 `OpAFmt=(true,3,5)`、`OpBFmt=(true,2,8)`，手算 `ForMult` 的精确结果格式，再在 Python 中真正做一次乘法，验证整数位/小数位的增长与乘积数值。

**操作步骤**（在 `python/unittest` 目录运行，或确保 `python/src` 在 `sys.path` 中）：

```python
# 示例代码
from en_cl_fix_pkg import *

OpAFmt = FixFormat(True, 3, 5)     # 与 VHDL 示例一致
OpBFmt = FixFormat(True, 2, 8)

# 1. 手算 ForMult：(True, 3+2+1, 5+8) = (True, 6, 13)
ResFmt = FixFormat.ForMult(OpAFmt, OpBFmt)
print("ResFmt =", ResFmt, " width =", ResFmt.width())   # 预期 (True, 6, 13), 20

# 2. 装入操作数（默认 SatWarn_s；这里两个值都在范围内，不触发饱和）
a = cl_fix_from_real(-3.134, OpAFmt, FixSaturate.Sat_s)
b = cl_fix_from_real( 0.1,   OpBFmt, FixSaturate.Sat_s)
print("a =", a, " b =", b)

# 3. 乘法（默认 Trunc_s, None_s；ResFmt==全精度格式，故无舍入无饱和）
res = cl_fix_mult(a, OpAFmt, b, OpBFmt, ResFmt)
print("res =", res)
```

> 跨语言提示：Python 没有 `cl_fix_to_real`（该函数仅 VHDL 有，见 [u3-l1](u3-l1-conversion-functions.md)）。Python 的定点函数本就在实数域运算，`cl_fix_mult` 直接返回 `float`，所以 `print(res)` 打印出的就是实数值——它对应 VHDL 示例里 `cl_fix_to_real(Res_v, ResFmt_c)` 的输出。

**手算预期**（你可以先算，再运行对照）：

1. **格式**：`ForMult((true,3,5),(true,2,8)) = (true, 6, 13)`，位宽 20。
2. **量化操作数**：`-3.134` 在 `(true,3,5)` 量化到 `-100 · 2^{-5} = -3.125`；`0.1` 在 `(true,2,8)` 量化到 `26 · 2^{-8} = 0.1015625`。
3. **精确乘积**：`(-100 · 2^{-5}) · (26 · 2^{-8}) = -2600 · 2^{-13} = -0.3173828125`。
4. 由于 `ResFmt` 就是全精度格式 `(true,6,13)`，`cl_fix_resize` 在 `Trunc_s / None_s` 下不丢任何位（0 个小数位、0 个整数位），结果恰好等于 `-0.3173828125`。

**需要观察的现象**：

- `ResFmt` 打印 `(True, 6, 13)`：整数位 `3,2 → 6`（相加 +1），小数位 `5,8 → 13`（相加）。
- `res` 打印 `-0.3173828125`（或极其接近的浮点值），与上面手算一致。

**预期结果**：格式与数值均与手算吻合，证明「乘积落在 `ForMult` 给出的网格上、且在该格式内精确无损」。若你把 `ResFmt` 改小（例如 `(true,3,3)`），会看到 `res` 因 resize 丢位而偏离 `-0.3173828125`——那正是 [u3-l2](u3-l2-resize-rounding.md) 讲的舍入误差。

> 待本地验证：不同 numpy/Python 版本下浮点打印的小数位数可能略有差异，但数值应在 1e-12 内一致。

#### 4.3.5 小练习与答案

**练习 1**：把上面实践里的 `ResFmt` 改成 `FixFormat(True, 3, 3)`（即丢掉 3 个小数位、3 个整数位），分别用默认 `Trunc_s` 和 `FixRound.NonSymPos_s` 各跑一次，结果分别是多少？为什么不同？

**答案**：全精度乘积是 `-2600 · 2^{-13}`。resize 到 `(true,3,3)` 要丢 10 个小数位（`13→3`）。`-2600 · 2^{-13} = -0.3173828125`，除以新分辨率 `2^{-3}` 得 `-0.3173828125 · 8 = -2.5390625`。`Trunc_s` 截断（朝负无穷）得 `-3 · 2^{-3} = -0.375`；`NonSymPos_s` 加半格后截断，四舍五入得 `-2 · 2^{-3} = -0.25`（或按实现近似）。两者不同正是因为 [u1-l4](u1-l4-rounding-modes.md) 所说「非平局时除 Trunc_s 外六种一致」、而 Trunc_s 恒向负取整。（具体舍入结果以本地运行为准。）

**练习 2**：Python `cl_fix_mult` 在什么时候会把 `a * b` 切换到 `wide_fxp` 路径？给出一个会触发的格式例子。

**答案**：当 `FixFormat.ForMult(aFmt, bFmt)` 的位宽超过 53（即 `cl_fix_is_wide(midFmt)` 为真），或 a/b 本身已是 `wide_fxp` 时。例如 `aFmt = bFmt = FixFormat(True, 30, 0)`，`ForMult = (True, 61, 0)`，位宽 62 > 53，会触发 wide 路径（详见 [u6-l1](u6-l1-narrow-wide-dispatch.md)、[u6-l2](u6-l2-wide-fxp-class.md)）。

---

## 5. 综合实践

**任务**：用一个实验同时验证「位增长规则、符号组合处理、舍入对资源的意义」三件事。

**步骤**：

1. 选定 `aFmt = FixFormat(True, 2, 1)`、`bFmt = FixFormat(True, 1, 2)`（与 [python/unittest/en_cl_fix_pkg_test.py:408-434](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/unittest/en_cl_fix_pkg_test.py#L408-L434) 的测试用例同格式）。
2. 手算 `ForMult`：`(True, 2+1+1, 1+2) = (True, 4, 3)`，位宽 8 = `Wa+Wb = 4+4`。
3. 在 Python 中对四种符号取值（`2.5`、`-2.5` × `1.25`、`-1.25`）调用 `cl_fix_mult`，结果格式用 `FixFormat(True, 3, 3)`、舍入 `Trunc_s`、饱和 `None_s`，验证它们分别等于 `3.125`、`-3.125`、`3.125`、`-3.125` 附近（注意 `(true,3,3)` 比 `ForMult` 的 `(true,4,3)` 少 1 个整数位，`3.125` 已接近上界，观察是否触发饱和/回绕）。
4. 再把同一乘法的舍入从 `Trunc_s` 换成 `NonSymPos_s`，比较两者结果差异，并用 [README.md:121](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/README.md#L121) 的「Use *Trunc_s* wherever possible for lowest resource usage」解释：乘法器本身已经消耗了 DSP/大量 LUT，其后若再接一个需加法器的舍入模式（如 `NonSymPos_s`）会进一步增加面积/时序开销，因此乘法后**优先用零成本的 `Trunc_s`**，只有在算法确实需要四舍五入时才升级到 `NonSymPos_s`。

**预期**：你能画出 `aFmt,bFmt → ForMult((true,4,3)) → cl_fix_mult 精确乘 → cl_fix_resize 到 (true,3,3)` 的数据流，并说清楚每一步在哪里可能丢精度。

## 6. 本讲小结

- 定点乘法的中间格式由 `FixFormat.ForMult` 给出：**整数位相加再 +1、小数位直接相加、结果符号 = 两操作数符号之或**；其位宽恰好等于两操作数位宽之和（紧形式），是补码乘法器「\(W_a \times W_b \to W_a+W_b\)」的定点实现。
- VHDL `ForMult` 用**紧形式**（仅当任一操作数有符号才 +1）以最小化硬件中间位宽；Python 用**统一形式**（永远有符号、永远 +1），是有符号情形的紧形式、其它情形的无损超集——两者经最终 resize 后位真一致。
- VHDL `cl_fix_mult` 按 a/b 是否有符号分**四支**：纯 `signed*signed`、纯 `unsigned*unsigned`，以及两支混合符号——后者用 `"0" & signed(...)` 把无符号数零扩展成非负补码数，复用有符号乘法器。
- Python/MATLAB 在实数域直接 `a * b` / `a .* b`，无需区分符号组合；Python 在中间位宽 > 53 时经 `cl_fix_is_wide` 派发到 `wide_fxp` 任意精度整数路径。
- 乘积在中间格式上**精确无损**，所有误差只来自最后一步 `cl_fix_resize` 的舍入/饱和——这正是 u4-l1 统一架构在乘法上的再次落地。
- 资源建议：乘法器代价高，其后的 resize **优先用 `Trunc_s`**（零成本），需要四舍五入时再用 `NonSymPos_s`。

## 7. 下一步学习建议

- **[u4-l3（移位、均值、取反与绝对值）](u4-l3-shift-mean-abs-neg.md)**：继续在同一架构下学习 `cl_fix_shift`（无损移位再 resize）、`cl_fix_mean`（相加后右移一位）、`cl_fix_neg/abs` 及其 `s` 资源变体，会把本讲的「中间全精度格式」思路推广到更多运算。
- **[u6-l1](u6-l1-narrow-wide-dispatch.md) / [u6-l2](u6-l2-wide-fxp-class.md)**：如果对 `cl_fix_mult` 里 `cl_fix_is_wide` 派发到的 `wide_fxp` 任意精度路径感兴趣，可深入大位宽乘法的整数实现。
- **建议阅读源码**：把 [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) 中 `cl_fix_mult`（2577–2615 行）与 `cl_fix_add`/`cl_fix_sub` 横向对照，体会它们共享的「`TempFmt_c` → 精确运算 → `cl_fix_resize`」三段式结构。
