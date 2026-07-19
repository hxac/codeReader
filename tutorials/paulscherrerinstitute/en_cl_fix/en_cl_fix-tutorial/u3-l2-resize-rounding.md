# cl_fix_resize 的舍入机制

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cl_fix_resize` 为什么是整个 en_cl_fix 库的「心脏」——加减乘、移位、均值、绝对值等所有运算最终都会落到它身上。
- 解释「**先加偏移、再截断**」这条贯穿三语言实现的统一舍入思路。
- 准确说出 `DropFracBits`、`NeedRound`、`CarryBit`、`AddSignBit` 四个派生量的计算公式与各自的作用。
- 推导 `HalfMinusDelta = 2^(DropFracBits-1) − 1` 这个常量的含义，并读懂 VHDL 中 `GetHalfMinusDelta` 的位串实现。
- 对照七种 `FixRound` 模式，分别写出 VHDL、Python narrow（双精度浮点）、Python wide（任意精度整数）三套实现里「加什么偏移」的表达式。
- 完成一次「输入 `(true,3,4)` → `(true,3,1)`」的七模式舍入实验，并手算一个平局（tie）样例验证不同模式的分歧。

## 2. 前置知识

本讲建立在前几讲已引入的概念之上，这里只做最简回顾，不重复细节：

- **定点格式 `[S,I,F]`**（u1-l2）：一个三元组 `Signed, IntBits, FracBits` 决定位串如何解释为实数，数值 \( V = N \cdot 2^{-F} \)，其中 \( N \) 是位串表示的整数。
- **舍入模式 `FixRound`**（u1-l4）：七种模式只在小数丢位、且结果恰落在两网格点正中间（即 0.5 LSB **平局**）时才有差异；非平局时除 `Trunc_s` 外六种行为完全一致。`Trunc_s=0, NonSymPos_s=1, NonSymNeg_s=2, SymInf_s=3, SymZero_s=4, ConvEven_s=5, ConvOdd_s=6` 是三语言共享的整数编码；`Round_s` 是 `NonSymPos_s` 的别名。
- **饱和模式 `FixSaturate`**（u1-l5）：`None_s / Warn_s / Sat_s / SatWarn_s`，决定丢整数位（溢出）时是回绕（wrap，取模）还是夹紧（clip）。
- **数值与位串的转换**（u3-l1）：VHDL 把数存为 `std_logic_vector` 位串，Python/MATLAB 把数存为 `double` 实数。本讲的舍入在「实数域」（Python narrow / MATLAB）与「位串/整数域」（VHDL / Python wide）两套表示里各有一份实现，但语义完全一致。

一个贯穿本讲的关键直觉：

> **舍入 = 在「丢掉低位」之前，先给数值加上一个「偏移量」，让截断的方向符合想要的策略。**

不同舍入模式的差别，本质上只是这个**偏移量**不同。把这句话记住，本讲剩下内容就是在三套实现里反复印证它。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 包，定点库的唯一源文件 | `cl_fix_resize` 函数体（位串域实现）、`GetHalfMinusDelta`、`DropFracBits/NeedRound/CarryBit/AddSignBit` 派生量、`TempFmt_c` |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 主体函数库 | `cl_fix_resize` 的 narrow 舍入分支（实数域）、`cl_fix_is_wide` 派发 |
| `python/src/en_cl_fix_pkg/wide_fxp.py` | Python >53 位大位宽实现 | `wide_fxp.resize` 的整数域舍入分支 |
| `matlab/src/cl_fix_resize.m` | MATLAB 端 resize | 用浮点「加偏移再 `floor`」实现七模式，可与 Python narrow 对照 |

> 说明：MATLAB 实现与 Python narrow 路径在数学上等价（都在实数域加偏移），本讲把它作为「实数域思路」的第二个佐证，重点仍放在 VHDL（位串域）与 Python（narrow + wide）上。

## 4. 核心概念与源码讲解

### 4.1 cl_fix_resize（VHDL）：库的心脏与统一流程

#### 4.1.1 概念说明

`cl_fix_resize` 把一个定点数从格式 `a_fmt` 重新表示为格式 `result_fmt`。它要做三件事：

1. **小数位对齐**：可能要丢掉一些小数位（`a_fmt.FracBits > result_fmt.FracBits`），这时需要**舍入**；也可能要补一些小数位（无损左移）。
2. **整数位对齐**：可能要丢掉一些整数位（`a_fmt.IntBits > result_fmt.IntBits`），这时可能**溢出**，需要按 `FixSaturate` 决定回绕还是夹紧（这是下一讲 u3-l3 的主题）。
3. **符号位对齐**：无符号 ↔ 有符号之间的转换。

它之所以是「心脏」，是因为库中几乎所有运算都遵循同一套范式：**先把操作数 `resize` 到一个足够宽、能容纳精确结果的中间格式 `TempFmt`，在上面做无精度损失的加减乘，最后再 `resize` 一次到目标格式做舍入与饱和**。因此 `cl_fix_add / cl_fix_sub / cl_fix_mult / cl_fix_shift / cl_fix_mean / cl_fix_abs …` 的函数体末尾几乎都有一句 `cl_fix_resize(temp_v, TempFmt_c, result_fmt, round, saturate)`。理解了 resize 的舍入，就理解了全库运算误差的唯一来源。

`cl_fix_resize` 的默认参数是 `round = Trunc_s`、`saturate = Warn_s`（注意 u1-l5 提过：Python 端默认 `None_s`，跨语言调用应显式传参）。

#### 4.1.2 核心流程

VHDL 版本的执行流程（伪代码）：

```
1. 计算派生量：
   DropFracBits = a_fmt.FracBits - result_fmt.FracBits   # 要丢的小数位数
   NeedRound    = (round /= Trunc_s) and (DropFracBits > 0)
   CarryBit     = NeedRound and (saturate /= None_s)
   AddSignBit   = (无符号→无符号) and (saturate /= None_s)

2. 构造中间格式 TempFmt（足够宽，预留 carry/sign 位）
3. 把 a 符号扩展/零扩展后放入 temp_v
4. if NeedRound:
        case round:
            Trunc_s    -> 不加偏移
            其它六种   -> temp_v += 某个偏移量   # ← 本讲核心
5. if 发生整数位溢出 and saturate /= None_s:
        按 Sat/Warn 夹紧或仅告警   # ← u3-l3 详讲
6. 从 temp_v 切出 result_fmt 宽度的位串返回
```

第 4 步的「加偏移」就是本讲全部内容；第 5 步的饱和留到 u3-l3。

#### 4.1.3 源码精读

函数签名与默认参数（注意 `saturate` 默认是 `Warn_s`）：

[en_cl_fix_pkg.vhd:2027-2032](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2027-L2032) — VHDL `cl_fix_resize` 的入口，`round` 默认 `Trunc_s`，`saturate` 默认 `Warn_s`。

中间格式 `TempFmt_c` 的构造（这是 u3-l3 饱和的关键，但它的存在也保证第 4 步加偏移不会因进位而溢出）：

[en_cl_fix_pkg.vhd:2053-2058](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2053-L2058) — `TempFmt` 的小数位取 `max(a_fmt.FracBits, result_fmt.FracBits)`（先保留全部输入小数位），整数位预留 carry/sign。

把输入符号扩展后放进 `temp_v`：

[en_cl_fix_pkg.vhd:2072-2078](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2072-L2078) — 有符号走 `resize(signed(a_v))`，无符号走 `resize(unsigned(a_v))`，统一扩展到 `TempWidth_c`。

舍入的 case 语句（本讲最关键的一段，逐模式给出偏移）：

[en_cl_fix_pkg.vhd:2079-2104](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2079-L2104) — `if NeedRound_c then ... case round is ...`，七种模式各自给 `temp_v` 加不同偏移。其中 `Trunc_s => null;` 表示不加任何偏移、直接截断。

末尾从 `temp_v` 切出结果位串（即「截断」这一步，丢掉 `CutFracBits_c` 位低位、丢掉高位溢出位）：

[en_cl_fix_pkg.vhd:2124-2125](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2124-L2125) — `result_v := temp_v(ResultWidth_c+CutFracBits_c-1 downto CutFracBits_c)`，从偏移后的 `temp_v` 中切出结果格式宽度。

最后这段切片就是数学上的「向下取整截断」：丢掉低 `CutFracBits_c` 位 = 除以 \( 2^{DropFracBits} \) 后取整。所有舍入模式的差异，只在于切片**之前**给 `temp_v` 加了什么。

#### 4.1.4 代码实践

源码阅读型实践：定位「加偏移」与「截断」两步。

1. 打开 `vhdl/src/en_cl_fix_pkg.vhd`，跳到 `cl_fix_resize`（约 2027 行）。
2. 找到 `case round is`（约 2085 行），确认 `Trunc_s => null;`。
3. 找到函数末尾的 `result_v := ... downto CutFracBits_c`（约 2124 行），理解它就是把加偏移后的整数右移并丢弃低位。
4. 在脑中用一句话概括：「**case 语句负责决定偏移量，末尾切片负责截断**」。

#### 4.1.5 小练习与答案

**练习 1**：`cl_fix_resize` 的 `round` 默认值是什么？如果不显式传 `round`，会得到哪种舍入行为？

**答案**：默认 `Trunc_s`（截断，向 \( -\infty \) 取整，不加任何偏移）。不显式传 `round` 意味着不做四舍五入，直接丢低位。

**练习 2**：为什么说库里的 `cl_fix_mult` 的舍入误差最终都来自 `cl_fix_resize`？

**答案**：因为乘法本身在 `TempFmt`（全精度中间格式）上是精确的（整数乘法不丢位），误差只在最后把全精度结果 `resize` 到目标格式时，由 `cl_fix_resize` 的舍入偏移 + 截断引入。

---

### 4.2 DropFracBits、NeedRound、CarryBit、AddSignBit

#### 4.2.1 概念说明

`cl_fix_resize` 在函数开头一口气算了四个派生量，它们是整段舍入/饱和逻辑的「开关」。读懂它们，就懂了函数何时会加偏移、何时需要预留进位位：

- **`DropFracBits`**：要丢掉的小数位数 = `a_fmt.FracBits − result_fmt.FracBits`。它为正才需要舍入；为零或负（补小数位）则无损、无需舍入。
- **`NeedRound`**：是否真的需要加偏移。条件是 `round ≠ Trunc_s` **且** `DropFracBits > 0`。`Trunc_s` 本来就是「截断不加偏移」，所以即使要丢位，`Trunc_s` 也置 `NeedRound = FALSE`。
- **`CarryBit`**：是否要在中间格式里多留一个整数位来接住「舍入进位」。例如对 `1.5` 做 half-up 舍入到整数会得到 `2.0`，这个进位会让整数部分多一位。**只有当 `NeedRound` 为真、且 `saturate ≠ None_s` 时**才需要这个 carry 位——因为回绕模式（`None_s`）下进位直接被取模吸收，不需要额外位来「看见」它。
- **`AddSignBit`**：一个为无符号→无符号、且开启饱和时预留的额外符号位（源码注释自承「undocumented」，作用较隐蔽，本讲只作了解）。

#### 4.2.2 核心流程

四个派生量之间的依赖：

```
DropFracBits = a_fmt.FracBits - result_fmt.FracBits
NeedRound    = (round /= Trunc_s) and (DropFracBits > 0)
CarryBit     = NeedRound and (saturate /= None_s)
AddSignBit   = (not a_fmt.Signed) and (not result_fmt.Signed) and (saturate /= None_s)
```

它们的下游用途：

- `NeedRound` → 门控第 4.1 节的 `case round is` 整段（为假则跳过，省掉加法器）。
- `CarryBit` / `AddSignBit` → 进入 `TempFmt_c.IntBits` 的计算，决定中间格式多宽。

#### 4.2.3 源码精读

四个常量的定义紧挨在一起：

[en_cl_fix_pkg.vhd:2033-2038](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2033-L2038) — `DropFracBits_c`、`NeedRound_c`、`CarryBit_c`、`AddSignBit_c` 四个派生量。注释明确写出 `CarryBit` 的含义：「Rounding addition is performed with an additional integer bit (carry bit)」。

`CarryBit_c` 如何影响中间格式的整数位数：

[en_cl_fix_pkg.vhd:2056-2056](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2056-L2056) — `IntBits => max(a_fmt.IntBits + toInteger(CarryBit_c), result_fmt.IntBits) + toInteger(AddSignBit_c)`。当 `CarryBit_c` 为真时，输入整数位 +1，给舍入进位留位置。

`NeedRound_c` 如何门控整个 case 段：

[en_cl_fix_pkg.vhd:2079-2080](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2079-L2080) — `if NeedRound_c then ...`。只有需要舍入时才执行 case；否则直接走到末尾切片，等价于纯截断。

#### 4.2.4 代码实践

参数扫描型实践：在脑中（或草稿纸上）跑四组参数，预测四个派生量的真假。

设 `a_fmt = (true,3,4)`、`result_fmt = (true,3,1)`（本讲综合实践的同款），逐组判断：

| `round` | `saturate` | `DropFracBits` | `NeedRound` | `CarryBit` |
| --- | --- | --- | --- | --- |
| `Trunc_s` | `None_s` | 3 | FALSE（因为是 Trunc） | FALSE |
| `NonSymPos_s` | `None_s` | 3 | TRUE | FALSE（None_s） |
| `NonSymPos_s` | `Sat_s` | 3 | TRUE | TRUE |

预期：第三行 `CarryBit` 为真，所以 `TempFmt` 会比第一、二行多一个整数位。**待本地验证**：可在 VHDL testbench 里打印 `TempFmt_c.IntBits` 比对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CarryBit` 在 `saturate = None_s`（回绕）时一定为假？

**答案**：回绕模式下，舍入产生的进位会被最后的取模运算自然吸收（多出的高位直接丢弃并回绕），不需要在中间格式里「看见」这个进位去触发饱和判断，所以不必预留 carry 位，节省一个整数位的硬件资源。

**练习 2**：若 `a_fmt.FracBits = 2`、`result_fmt.FracBits = 5`，`DropFracBits` 是多少？`NeedRound` 会为真吗？

**答案**：`DropFracBits = 2 − 5 = −3`（要补 3 个小数位，无损左移）。`NeedRound` 为假（条件要求 `DropFracBits > 0`），不会加任何偏移。

---

### 4.3 GetHalfMinusDelta 与 HalfMinusDelta 常量

#### 4.3.1 概念说明

舍入偏移的「积木」是一个叫 **`HalfMinusDelta`** 的常量。先定义清楚两个基本量（都以**输入小数位的 LSB** 为单位，即权重 \( 2^{-aFmt.FracBits} \)）：

- **half**（半格）：\( h = 2^{DropFracBits - 1} \)，恰好等于结果格式半个 LSB 的权重。当 `DropFracBits = 1` 时 \( h = 1 \)（输入 LSB）。
- **HalfMinusDelta**（半格减一个输入 LSB）：\( h_\Delta = 2^{DropFracBits - 1} - 1 \)，即**严格小于半格的最大整数**（在输入 LSB 单位下）。

为什么需要 \( h_\Delta \)？因为「加 half 再截断」会让所有平局都朝同一个方向（这正是 `NonSymPos` 的做法，简单但引入直流偏差）。要让平局的方向**取决于某个条件**（符号、奇偶等），就需要把偏移拆成「half − 1」再加一个「条件性的 1」：

\[ \text{offset} = \underbrace{(2^{DropFracBits-1} - 1)}_{\text{HalfMinusDelta}} + \underbrace{\text{条件位}\in\{0,1\}}_{\text{由模式决定}} \]

这样，当条件位为 0 时偏移 = half − 1（平局向下），条件位为 1 时偏移 = half（平局向上）。于是「平局往哪边」就被这个条件位完全控制——这就是除 `Trunc_s`、`NonSymPos_s` 之外五种模式的统一写法。

#### 4.3.2 核心流程

`HalfMinusDelta` 的值依赖 `DropFracBits`：

\[ h_\Delta = 2^{DropFracBits-1} - 1 \]

- `DropFracBits ≤ 1`：\( h_\Delta = 0 \)（因为 `DropFracBits = 1` 时 \( 2^0 - 1 = 0 \)；`DropFracBits < 1` 时 `NeedRound` 为假、根本用不到，返回 0 占位）。
- `DropFracBits > 1`：\( h_\Delta = 2^{DropFracBits-1} - 1 \)，二进制为 `DropFracBits − 1` 个 1，即 `0b11…1`。

VHDL 用**位串字面量**而非整数算术来实现它，这是为了支持 `DropFracBits > 32` 的大位宽（普通 VHDL `integer` 只有 32 位，详见 Unit 6 的 wide 支持）。

#### 4.3.3 源码精读

`GetHalfMinusDelta` 函数：

[en_cl_fix_pkg.vhd:2041-2050](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2041-L2050) — 返回 `unsigned`。`DropFracBits_c <= 1` 时返回 `"0"`；否则返回 `(DropFracBits_c-2 downto 0 => '1')`，即 `DropFracBits_c - 1` 位全 1，数值正是 \( 2^{DropFracBits-1} - 1 \)。注释解释了为何用 unsigned：「to support >32 bits」。

把它固化成常量：

[en_cl_fix_pkg.vhd:2052-2052](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2052-L2052) — `constant HalfMinusDelta_c : unsigned := GetHalfMinusDelta;`，整个 resize 内只算一次。

`HalfMinusDelta_c` 在 case 语句里被五种模式反复使用（4.4 节详列），例如 `NonSymNeg_s => temp_v := temp_v + HalfMinusDelta_c;`（[en_cl_fix_pkg.vhd:2088-2088](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2088-L2088)）。

#### 4.3.4 代码实践

手算型实践：填出 `DropFracBits` 从 1 到 5 时 `HalfMinusDelta` 的值。

| `DropFracBits` | half \( h = 2^{DropFracBits-1} \) | HalfMinusDelta \( h_\Delta \) | VHDL 返回位串 |
| --- | --- | --- | --- |
| 1 | 1 | 0 | `"0"` |
| 2 | 2 | 1 | `"1"` |
| 3 | 4 | 3 | `"11"` |
| 4 | 8 | 7 | `"111"` |
| 5 | 16 | 15 | `"1111"` |

预期：第 3 行 `DropFracBits = 3` 时 `HalfMinusDelta = 3`，正是本讲综合实践 `(true,3,4) → (true,3,1)` 会用到的值。

#### 4.3.5 小练习与答案

**练习 1**：为什么 VHDL 用 `(N downto 0 => '1')` 这种位串写法，而不是写 `to_unsigned(2**(DropFracBits-1)-1, ...)`？

**答案**：因为 VHDL `integer` 只有 32 位，`2**(DropFracBits-1)` 在 `DropFracBits > 32` 时会溢出。用 `unsigned` 位串字面量可以表达任意位宽，从而支持 >53 位的大位宽格式（wide 场景）。

**练习 2**：`HalfMinusDelta` 与 half 相差多少？这个差值在舍入中起什么作用？

**答案**：相差恰好 1 个输入 LSB。这个差值正是「条件位」的可操作空间：偏移取 half 时平局向上，取 half − 1 时平局向下，五种种舍入模式靠附加的条件位（符号、奇偶）在这一格内切换方向。

---

### 4.4 七种舍入模式的偏移表达式（VHDL case）

#### 4.4.1 概念说明

把 4.3 节的积木拼起来，就能逐模式写出偏移。统一记号（全部以**输入 LSB** 为单位，权重 \( 2^{-aFmt.FracBits} \)）：

- \( h = 2^{DropFracBits-1} \)：half（半个结果 LSB）
- \( h_\Delta = h - 1 \)：HalfMinusDelta
- `sign`：输入的符号位（有符号时为最高位，无符号时恒 0）
- `LSB_r`：结果格式的最低整数位在输入中的位置，即 `a_v(DropFracBits)`，是「被保留的最高一位」

七种模式的偏移如下（加到 `temp_v` 后再截断）：

| 模式 | 偏移（输入 LSB 单位） | 平局方向 |
| --- | --- | --- |
| `Trunc_s` | \( 0 \) | 向 \( -\infty \)（截断） |
| `NonSymPos_s` | \( +h \) | 向 \( +\infty \) |
| `NonSymNeg_s` | \( +h_\Delta \) | 向 \( -\infty \) |
| `SymInf_s` | \( +h_\Delta + (1 - \text{sign}) \) | 远离零（away） |
| `SymZero_s` | \( +h_\Delta + \text{sign} \) | 朝向零 |
| `ConvEven_s` | \( +h_\Delta + \text{LSB}_r \) | 朝偶数 |
| `ConvOdd_s` | \( +h_\Delta + (1 - \text{LSB}_r) \) | 朝奇数 |

读这张表的窍门：

- `NonSymPos` 是唯一只加 `h`（不加条件位）的「上舍入」。
- 其余五种都是 `hΔ + 条件位`：条件位由该模式的「平局偏好」决定——符号位 `sign`（对称类）或结果的奇偶 `LSB_r`（收敛类）。
- `Trunc_s` 加 0，根本不进 case 的加法（被 `NeedRound` 排除）。

#### 4.4.2 核心流程

`sign_v` 的取值在进入 case 前算好（[en_cl_fix_pkg.vhd:2080-2084](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2080-L2084)）：有符号取最高位，无符号取 `'0'`。然后 case 给七种模式各加一个偏移。注意 `ConvEven_s / ConvOdd_s` 有一个边界保护：当 `DropFracBits_c >= a_v'length` 时（要丢的位比整个输入还多），`a_v(DropFracBits_c)` 会越界，于是退化为用 `sign_v` 做隐式符号扩展。

#### 4.4.3 源码精读

完整的 case 语句（本讲最值得逐行读的一段）：

[en_cl_fix_pkg.vhd:2085-2103](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2085-L2103) — 七种模式的偏移逐行对应上表。要点：

- `Trunc_s => null;`（不加偏移）。
- `NonSymPos_s` 对 `temp_v(TempWidth_c-1 downto DropFracBits_c-1)` 切片 `+ 1`——等价于在第 `DropFracBits_c-1` 位（即 half 位）加 1，正是「加 \( h \)」。
- `NonSymNeg_s` 整体 `+ HalfMinusDelta_c`（加 \( h_\Delta \)）。
- `SymInf_s` 加 `HalfMinusDelta_c + not sign_v`（正数 `not sign=1` → 加 half；负数 → 加 \( h_\Delta \)，即更负，远离零）。
- `SymZero_s` 加 `HalfMinusDelta_c + sign_v`（正数 → 加 \( h_\Delta \)，向下回零；负数 → 加 half，向上回零）。
- `ConvEven_s` 加 `HalfMinusDelta_c + a_v(DropFracBits_c)`（结果最低位为 1/奇数时加 half 进位成偶；为 0/偶数时加 \( h_\Delta \) 保持偶）。
- `ConvOdd_s` 加 `HalfMinusDelta_c + not a_v(DropFracBits_c)`（反过来）。

`ConvEven_s` 的越界保护分支：

[en_cl_fix_pkg.vhd:2091-2096](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2091-L2096) — 当 `DropFracBits_c >= a_v'length` 时取 `sign_v` 做「隐式符号扩展」，避免位索引越界。

#### 4.4.4 代码实践

源码定位型实践（综合实践的预热）：在上面的 case 语句里，把每个模式对应的「VHDL 加偏移语句」摘抄下来，填进 4.4.1 的表格第三列。例如 `SymInf_s` 一行应填 `temp_v + HalfMinusDelta_c + ("" & not sign_v)`。完成后，这张表就是你「模式 → 偏移表达式」对照表的 VHDL 列。

#### 4.4.5 小练习与答案

**练习 1**：用一句话解释为什么 `SymInf_s` 对正数等价于 `NonSymPos_s`、对负数等价于 `NonSymNeg_s`。

**答案**：`SymInf` 的偏移是 \( h_\Delta + (1 - \text{sign}) \)。正数 `sign=0` → 偏移 \( h_\Delta + 1 = h \)，与 `NonSymPos`（\( +h \)）相同；负数 `sign=1` → 偏移 \( h_\Delta \)，与 `NonSymNeg`（\( +h_\Delta \)）相同。所以「远离零」=「正数向上、负数向下」。

**练习 2**：`ConvEven_s` 为什么用 `a_v(DropFracBits_c)`（结果最低位）做条件，而不用符号位？

**答案**：收敛舍入要让平局去到「最近的偶数」。结果是否为偶数由它的最低位（即被保留的最低位 `a_v(DropFracBits)`）决定：该位为 0 时结果已偶，平局应向下保持；该位为 1 时结果奇，平局应向上进位成偶。所以条件位必须取结果最低位，而非符号位。

---

### 4.5 Python narrow 与 MATLAB：实数域的同一套偏移

> 本节对应规格中的最小模块「cl_fix_resize（Python 舍入分支）」，并把 MATLAB 作为实数域的第二个佐证一并对照。

#### 4.5.1 概念说明

Python 的 `cl_fix_resize` 在真正做舍入前先做一次**派发**：用 `cl_fix_is_wide(rFmt)` 判断目标格式是否超过 53 位（IEEE754 双精度能精确表示的整数范围上限）。若超过，走 `wide_fxp` 整数路径（4.6 节）；否则走 **narrow 路径**——用 `numpy` 的双精度浮点数组在**实数域**直接加偏移、再 `np.floor` 截断。

narrow 路径的偏移与 VHDL 完全等价，只是单位从「输入 LSB」换成了「实数」。换算关系：

- 半格在实数域：\( h_{\text{real}} = 2^{-rFmt.FracBits - 1} \)（结果格式的半个 LSB）。
- 一个输入 LSB 在实数域：\( \Delta_{\text{real}} = 2^{-aFmt.FracBits} \)。
- 恒等关系：\( h_{\text{real}} / \Delta_{\text{real}} = 2^{aFmt.FracBits - rFmt.FracBits - 1} = 2^{DropFracBits-1} = h \)，所以「VHDL 加 \( h \) 个输入 LSB」就是「Python 加 \( h_{\text{real}} \)」。

MATLAB 的 `cl_fix_resize` 与 Python narrow 在数学上几乎逐行相同（都在实数域 `加偏移 → floor`），可互为印证。

#### 4.5.2 核心流程

Python narrow 路径的舍入分支（伪代码）：

```
if rFmt.FracBits < aFmt.FracBits:          # 要丢小数位才需舍入
    h   = 2**(-rFmt.FracBits - 1)          # 半格（实数）
    d   = 2**(-aFmt.FracBits)              # 一个输入 LSB（实数）
    switch rnd:
        Trunc_s     -> 不加
        NonSymPos_s -> a += h
        NonSymNeg_s -> a += h - d
        SymInf_s    -> a += h - d*(a < 0)
        SymZero_s   -> a += h - d*(a >= 0)
        ConvEven_s  -> a += h - d*((floor(a*2**rFmt.FracBits)+1) % 2)
        ConvOdd_s   -> a += h - d*((floor(a*2**rFmt.FracBits)) % 2)
# 截断到结果网格
rounded = floor(a * 2**rFmt.FracBits) * 2**(-rFmt.FracBits)
```

注意 `ConvEven_s` 的条件 `(floor(a*2**rFmt.FracBits)+1) % 2`：它为 1 当且仅当 `floor(...)` 为偶数——即结果最低位为 0（偶）。此时减去一个 \( d \)（偏移变成 \( h - d \)，平局向下保持偶）；结果为奇时不减（偏移 \( h \)，向上进位成偶）。这与 VHDL「`+ LSB_r`」等价。

#### 4.5.3 源码精读

派发逻辑：

[en_cl_fix_pkg.py:193-200](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L193-L200) — `if type(a) == wide_fxp or cl_fix_is_wide(rFmt):` 走 wide 路径；否则走 narrow。wide 路径结束后若 `rFmt` 不超 53 位，再 `to_narrow_fxp()` 转回浮点。

`cl_fix_is_wide` 的判定：

[en_cl_fix_pkg.py:23-38](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L23-L38) — 注释解释 IEEE754 双精度只能精确表示 ±2^53 内的整数，故 `return cl_fix_width(fmt) > 53`。

narrow 路径七种模式的偏移（与 4.4 表一一对应）：

[en_cl_fix_pkg.py:204-228](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L204-L228) — 注意每条分支末尾都设 `bitGrowth = 1`（除 `Trunc_s` 为 0），用来在 `roundedFmt` 里多预留一个整数位接 carry，对应 VHDL 的 `CarryBit_c`。

实数域「截断」一步：

[en_cl_fix_pkg.py:231-232](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L231-L232) — `roundedFmt` 多预留 carry 位；`rounded = np.floor(a * 2.0**rFmt.FracBits) * 2.0**-rFmt.FracBits` 即除以结果 LSB 后取整再乘回，等价于 VHDL 末尾的位串切片。

MATLAB 的同款偏移（实数域第二个佐证）：

[cl_fix_resize.m:40-59](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L40-L59) — 与 Python narrow 完全同构：`switch round` 七分支给 `a` 加同样的实数偏移，注意 `case Round.NonSymPos_s: a = a + 2^(-result_fmt.FracBits-1)` 正是 \( +h_{\text{real}} \)。

#### 4.5.4 代码实践

源码对照型实践：把 [en_cl_fix_pkg.py:204-228](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L204-L228) 与 [cl_fix_resize.m:43-55](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L43-L55) 并排打开，逐模式确认两边的实数偏移表达式字符级一致。应发现：Python 用 `(a < 0).astype(int)`，MATLAB 用 `(a < 0)`；Python 用 `((np.floor(a*2**rFmt.FracBits)+1) % 2)`，MATLAB 用 `mod(floor(a*2^result_fmt.FracBits)+1, 2)`——只是语法外壳不同。

#### 4.5.5 小练习与答案

**练习 1**：Python narrow 的 `SymInf_s` 分支为什么写 `2.0**(-rFmt.FracBits-1) - 2.0**-aFmt.FracBits * (a < 0)`，而不是像 VHDL 那样写 `+ not sign`？

**答案**：Python narrow 在实数域运算，没有「符号位」可取，只有「数值是否为负」的布尔数组 `(a < 0)`。负数时减一个输入 LSB \( \Delta_{\text{real}} \)（偏移变 \( h_\Delta \)），正数时不减（偏移 \( h \)），与 VHDL「`sign=1 → hΔ`，`sign=0 → h`」语义一致。

**练习 2**：narrow 路径里 `bitGrowth` 的作用对应 VHDL 哪个常量？

**答案**：对应 `CarryBit_c`。`bitGrowth = 1` 让 `roundedFmt.IntBits` 多一位，给舍入进位（如 1.5 half-up → 2.0 的进位）留位置。

---

### 4.6 wide_fxp.resize：整数域的同一套偏移

> 本节对应规格中的最小模块「wide_fxp.resize 舍入分支」。

#### 4.6.1 概念说明

当格式超过 53 位（`cl_fix_is_wide` 为真），双精度浮点已无法精确表示，Python 改走 `wide_fxp`：用 `dtype=object` 的 numpy 数组存**任意精度整数**。`wide_fxp` 内部存的不是实数，而是 \( \text{data} = \text{value} \times 2^{FracBits} \) 这个未归一化的大整数（例如 `1.25` 在 `(0,2,4)` 里存为 `20`，因为 \( 1.25 \times 16 = 20 \)）。

在整数域做舍入，思路仍是「**加偏移，再右移截断**」：

- 半格在整数域：\( h_{\text{int}} = 2^{f - fr - 1} \)，其中 `f = aFmt.FracBits`、`fr = rFmt.FracBits`，\( f - fr = DropFracBits \)，所以 \( h_{\text{int}} = 2^{DropFracBits-1} = h \)（与 VHDL 完全相同）。
- 截断 = 右移：`val >>= (f - fr)`，丢弃低 `DropFracBits` 位，等价于除以 \( 2^{DropFracBits} \) 取整。

七种模式的偏移（整数域）：

| 模式 | 偏移（加到 `val` 上） |
| --- | --- |
| `Trunc_s` | 不加 |
| `NonSymPos_s` | \( +2^{f-fr-1} \) |
| `NonSymNeg_s` | \( +2^{f-fr-1} - 1 \) |
| `SymInf_s` | \( +2^{f-fr-1} - [val < 0] \) |
| `SymZero_s` | \( +2^{f-fr-1} - [val \ge 0] \) |
| `ConvEven_s` | \( +2^{f-fr-1} - ((val \gg (f-fr)) + 1) \% 2 \) |
| `ConvOdd_s` | \( +2^{f-fr-1} - (val \gg (f-fr)) \% 2 \) |

其中 `[P]` 是布尔指示函数（真为 1、假为 0）。这与 4.4 的 VHDL 表逐行等价，只是把「位串加法」换成了「大整数算术」。

#### 4.6.2 核心流程

`wide_fxp.resize` 的舍入分支（伪代码，`val` 为整数数组）：

```
f, fr = fmt.FracBits, rFmt.FracBits
if fr < f:                          # 要丢小数位
    h = 2**(f - fr - 1)             # 半格（整数）
    shift = f - fr
    switch rnd:
        Trunc_s      -> 不加
        NonSymPos_s  -> val += h
        NonSymNeg_s  -> val += h - 1
        SymInf_s     -> val += h - (val < 0)
        SymZero_s    -> val += h - (val >= 0)
        ConvEven_s   -> t = val >> shift; val += h - (t+1)%2
        ConvOdd_s    -> t = val >> shift; val += h - t%2
    val >>= shift                   # 截断 = 右移
elif fr > f:                        # 补小数位，无损左移
    val *= 2**(fr - f)
# 然后做饱和/回绕（u3-l3）
```

注意收敛类的条件 `(val >> shift) + 1) % 2`：`val >> shift` 就是截断后的结果整数，其奇偶决定平局方向——`(x+1) % 2` 为 1 当 x 偶，对应「偶时减 1（保持偶）」，与 4.4 的 `+ LSB_r` 完全一致。

#### 4.6.3 源码精读

`wide_fxp.resize` 的舍入分支：

[wide_fxp.py:225-263](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L225-L263) — 七种模式的整数偏移逐行对应上表。每行注释用「Half-up / Half-down / Half-away-from-zero / Half-towards-zero / Convergent-even / Convergent-odd」点明该模式的直觉含义，是全库最清晰的一份舍入模式注释。

`NonSymPos_s` 与 `NonSymNeg_s`：

[wide_fxp.py:230-235](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L230-L235) — 前者 `val + 2**(f-fr-1)`（加 half），后者 `val + (2**(f-fr-1) - 1)`（加 half − 1，即 HalfMinusDelta 的整数版）。

收敛类：

[wide_fxp.py:246-257](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L246-L257) — 先 `trunc_a = val >> (f-fr)` 取截断结果，再按奇偶决定条件位，注释明确「Half-down for trunc(val) even, else half-up」。

截断（右移）：

[wide_fxp.py:262-263](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L262-L263) — `shift = f - fr; val >>= shift`，丢弃低 `shift` 位，即除以 \( 2^{DropFracBits} \) 取整。

整数存储约定（为何能这样移位）：

[wide_fxp.py:359-363](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L359-L363) — `__init__` 断言 `data.dtype == object`（任意精度整数），`self._data` 就是 `value × 2^FracBits` 的大整数数组，所以 `>>`、`+`、`%` 都是精确的大整数运算，无精度损失。

#### 4.6.4 代码实践

手算型实践：用整数域公式重算 4.7 综合实践里的平局样例 `1.25`（格式 `(true,3,4)` → `(true,3,1)`）。

- `f = 4, fr = 1, shift = 3, h = 2^(4-1-1) = 4`。
- `val = 1.25 × 2^4 = 20`。
- `NonSymPos`：`val = 20 + 4 = 24`，`24 >> 3 = 3`，结果 `3 × 2^-1 = 1.5`。
- `NonSymNeg`：`val = 20 + 3 = 23`，`23 >> 3 = 2`，结果 `2 × 2^-1 = 1.0`。
- `ConvEven`：`t = 20 >> 3 = 2`（偶），条件 `(2+1)%2 = 1`，`val = 20 + 4 - 1 = 23`，`>>3 = 2` → `1.0`（保持偶）。
- `ConvOdd`：`t = 2`，`t%2 = 0`，`val = 20 + 4 - 0 = 24`，`>>3 = 3` → `1.5`（进位成奇）。

预期：与 VHDL/narrow 完全一致。**待本地验证**：可在 Python 里构造 `wide_fxp` 对象实际调用 `resize` 比对。

#### 4.6.5 小练习与答案

**练习 1**：为什么 `wide_fxp` 用 `val >> shift` 截断，而 Python narrow 用 `np.floor(a * 2**fr)`？

**答案**：`wide_fxp` 存的是整数（`value × 2^f`），右移 `shift = f - fr` 位直接丢掉低 `DropFracBits` 位，等价于向下取整的截断，且对任意位宽都精确。narrow 存的是浮点实数，只能先乘到结果网格坐标 `a × 2^fr` 再 `floor` 取整。两者数学等价，载体不同。

**练习 2**：`SymInf_s` 在整数域写 `val += h - (val < 0)`，这里的 `(val < 0)` 是什么类型？

**答案**：它是一个与 `val` 同形状的布尔数组（经 `.astype(int)` 转成 0/1 整数），表示每个元素是否为负。负元素减 1（偏移变 `h − 1`），正元素不减（偏移 `h`），实现「远离零」。

---

## 5. 综合实践

**任务**：完成规格要求的「模式 → 偏移表达式」对照表，并用 Python 跑一次七模式舍入实验。

### 5.1 实践目标

把本讲四个最小模块（VHDL case、Python narrow、MATLAB、wide_fxp）的偏移表达式汇总成一张表，验证它们语义一致；再用 Python 对一个**平局样例**实跑，亲眼看到七种模式在 0.5 LSB 处的分歧。

### 5.2 操作步骤

1. **建表**：新建一张三列表格 `模式 | 实数域偏移（Python narrow / MATLAB） | 整数域偏移（VHDL / wide_fxp）`，把 4.4、4.5、4.6 三节的偏移填入。例如 `NonSymPos_s` 一行：实数域 `+ 2^(-rFmt.FracBits-1)`，整数域 `+ 2^(DropFracBits-1)`。

2. **Python 实跑**（在 `python/unittest` 目录下，`sys.path` 已含 `../src`）：

   ```python
   # 示例代码（非项目原有代码，供本实践使用）
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *

   aFmt = FixFormat(True, 3, 4)    # (true,3,4)
   rFmt = FixFormat(True, 3, 1)    # (true,3,1)
   a    = 1.25                     # 在 (true,3,4) 下是精确值，且对 (true,3,1) 恰为平局

   for rnd in FixRound:
       r = cl_fix_resize(a, aFmt, rFmt, rnd, FixSaturate.None_s)
       print(f"{rnd.name:12s} -> {r}")
   ```

3. **VHDL 定位**：打开 [en_cl_fix_pkg.vhd:2085-2103](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2085-L2103)，把每个模式对应的 `temp_v := ...` 加偏移语句摘到表格的「整数域」列。

### 5.3 需要观察的现象

- `1.25` 在 `(true,3,1)` 下的两个候选网格点是 `1.0` 和 `1.5`（结果 LSB = 0.5），`1.25` 正好在正中间，是**平局**。
- 因此七种模式应分裂成两组：`{Trunc_s, NonSymNeg_s, SymZero_s, ConvEven_s} → 1.0` 与 `{NonSymPos_s, SymInf_s, ConvOdd_s} → 1.5`。

### 5.4 预期结果（待本地验证）

| 模式 | 偏移（VHDL/wide，整数域） | Python 结果 | 方向解释 |
| --- | --- | --- | --- |
| `Trunc_s` | `+0` | 1.0 | 截断向下 |
| `NonSymPos_s` | `+h` | 1.5 | 平局向 \( +\infty \) |
| `NonSymNeg_s` | `+hΔ` | 1.0 | 平局向 \( -\infty \) |
| `SymInf_s` | `+hΔ + (1−sign)`，正数 → `+h` | 1.5 | 平局远离零 |
| `SymZero_s` | `+hΔ + sign`，正数 → `+hΔ` | 1.0 | 平局朝向零 |
| `ConvEven_s` | `+hΔ + LSB_r`，`LSB_r=0` → `+hΔ` | 1.0 | 平局朝偶（2 为偶） |
| `ConvOdd_s` | `+hΔ + (1−LSB_r)`，`LSB_r=0` → `+h` | 1.5 | 平局朝奇（3 为奇） |

其中 \( h = 2^{DropFracBits-1} = 4 \)、\( h_\Delta = 3 \)、`LSB_r = (20 >> 3) & 1 = 0`（截断结果 2 为偶）。手算细节见 4.6.4。

> 若把输入换成非平局值 `1.1875`（`0001.0011`，丢弃位 `011` = 3 < half = 4），七种模式应**全部**得到 `1.0`——这印证 u1-l4 的结论：「差异只在平局处」。

### 5.5 进阶（可选）

把同一个 `1.25` 也喂给 MATLAB（`cl_fix_resize(1.25, cl_fix_format(true,3,4), cl_fix_format(true,3,1), Round.NonSymPos_s, Sat.None_s)`），应得到与 Python narrow 完全一致的 `1.5`，验证三语言位真一致。**待本地验证**（MATLAB 端无自动化测试，需手工跑）。

## 6. 本讲小结

- `cl_fix_resize` 是全库心脏：所有运算先在全精度 `TempFmt` 上做精确计算，最后由它统一完成舍入与饱和，运算误差的唯一来源就在这里。
- 舍入的统一思路是「**先加偏移、再截断**」：七种 `FixRound` 模式的差别只是偏移量不同；截断在 VHDL 里是末尾位串切片，在 Python narrow 里是 `np.floor`，在 wide_fxp 里是 `>>`。
- 四个派生量 `DropFracBits / NeedRound / CarryBit / AddSignBit` 是舍入与饱和的开关：`NeedRound` 门控整段 case；`CarryBit` 决定中间格式是否多留一个整数位接进位（仅 `saturate ≠ None_s` 时需要）。
- 偏移积木是 `HalfMinusDelta = 2^(DropFracBits-1) − 1`（严格小于半格的最大整数）；`NonSymPos` 加半格 `h`，其余五种加 `hΔ + 条件位`，条件位由符号（对称类）或结果最低位奇偶（收敛类）决定。
- 三套实现语义完全一致：VHDL 在位串域、Python narrow/MATLAB 在实数域、wide_fxp 在任意精度整数域，偏移表达式可逐行互译，这是位真一致性的算法基础。
- 收敛类（`ConvEven / ConvOdd`）有越界保护：当要丢的位多于输入位宽时，VHDL 退化为用符号位做隐式扩展。

## 7. 下一步学习建议

- 本讲只讲了 resize 的**舍入**（丢小数位），**饱和/回绕**（丢整数位、溢出处理）是下一讲 **u3-l3《cl_fix_resize 的饱和与回绕》**的主题，建议紧接着读，重点是 `CutIntSignBits_c` 的溢出检测与有/无符号 clip 分支。
- 读完 u3-l3 后，可以进入 Unit 4 的运算函数（`cl_fix_add/sub/mult/shift`），观察它们如何构造 `TempFmt_c` 并在末尾调用 `cl_fix_resize`，把本讲的舍入机制放到真实运算链路里验证。
- 想深入大位宽实现的读者，可在学完 u3-l3 后跳到 **Unit 6**：u6-l1 讲 `cl_fix_is_wide` 的 53 位边界与 narrow/wide 派发，u6-l2 讲 `wide_fxp` 类的整数存储与运算符重载，本讲 4.6 节是其前置缩影。
- 推荐继续精读的源码：[en_cl_fix_pkg.vhd:2079-2104](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2079-L2104)（VHDL case）与 [wide_fxp.py:225-263](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L225-L263)（wide 注释），后者是全库对七种舍入模式最清晰的文字说明。
