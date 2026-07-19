# cl_fix_resize 的饱和与回绕

## 1. 本讲目标

学完本讲，你应当能够：

- 解释 `cl_fix_resize` 在丢**整数位**（而非小数位）时如何处理溢出：饱和（clip，夹紧到边界）与回绕（wrap，取模丢弃高位）两条路径。
- 说出 `TempFmt_c` 这个中间格式为何必须「足够宽」——它要同时容纳输入的全部整数位、结果的整数位、以及为饱和预留的 carry/sign 位。
- 精确解释 `CarryBit_c` 与 `AddSignBit_c` 两个额外整数位的来源与作用，并**揭开 `AddSignBit_c` 的真实用途**（上一讲 u3-l2 留作「undocumented」的悬念）。
- 推导 `CutIntSignBits_c = TempWidth − (ResultWidth + CutFracBits)` 这个窗口为何能判定溢出，并区分有符号（检查「全 0 或全 1」）与无符号（检查「全 0」）两种检测条件。
- 读懂 VHDL 中饱和 clip 的位填充技巧：用 `NOT(MSB)` 填充低位，配合一行对结果符号位的改写，用同一段代码同时实现「夹紧到最大值」与「夹紧到最小值」。
- 把 VHDL 的位串饱和对应到 Python narrow（实数域 `np.where` clip + 取模回绕）与 `wide_fxp`（任意精度整数取模）两套实现，理解三者位真一致。
- 完成一次「`(true,4,0)` 大正值 → `(true,2,0)`」的溢出实验：预测 `Sat_s` 与 `None_s` 两种结果，并用 `CutIntSignBits_c` 解释为何饱和得到最大值。

## 2. 前置知识

本讲是 **u3-l2《cl_fix_resize 的舍入机制》** 的姊妹篇，直接承接其结论，这里只做最简回顾：

- **`cl_fix_resize` 是全库心脏**（u3-l2）：所有运算（加减乘、移位、均值、绝对值）先在全精度中间格式 `TempFmt` 上做无损计算，最后由 `cl_fix_resize` 统一完成「舍入 + 饱和」。u3-l2 讲了**舍入**（丢小数位），本讲专讲**饱和/回绕**（丢整数位）。
- **四个派生量**（u3-l2 §4.2）：`DropFracBits`（要丢的小数位）、`NeedRound`（是否加舍入偏移）、`CarryBit`（舍入进位位）、`AddSignBit`（无符号→无符号饱和时的额外位）。u3-l2 已给出它们的公式与对舍入的影响；本讲从**饱和视角**重新审视后两个，特别是 `AddSignBit`。
- **饱和模式 `FixSaturate`**（u1-l5）：`None_s / Warn_s / Sat_s / SatWarn_s`。名字带 `Sat` 的会夹紧（clip），带 `Warn` 的会告警；`None_s`/`Warn_s` 走回绕，`Sat_s`/`SatWarn_s` 走饱和。三语言共享 0–3 整数编码。
- **表示域**（u3-l1）：VHDL 把数存为 `std_logic_vector` 位串（位串域），Python narrow / MATLAB 存为 `double`（实数域），`wide_fxp` 存为任意精度整数（整数域）。同一套饱和语义在三套载体里各有一份实现。

一个贯穿本讲的关键直觉：

> **溢出处理 = 把「超出结果格式能表示范围」的高位，要么直接砍掉（回绕），要么用边界值替换（饱和）。**

砍掉高位 = 取模，几乎零成本，但可能让一个大正数变成负数（符号反转）；替换为边界值 = clip，结果永远合法，代价是要先**检测**溢出、再**改写**位串。本讲全篇就是在三套实现里反复印证这两个动作。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
| --- | --- | --- |
| `vhdl/src/en_cl_fix_pkg.vhd` | VHDL 包，定点库唯一源文件 | `cl_fix_resize` 的 `TempFmt_c` 构造、`CarryBit_c/AddSignBit_c`、`CutIntSignBits_c`、饱和检测与 clip 分支（位串域） |
| `python/src/en_cl_fix_pkg/en_cl_fix_pkg.py` | Python 主体函数库 | `cl_fix_resize` 的 narrow 饱和（`np.where` clip）与回绕（取模，含 `convertToWide` 子分支） |
| `python/src/en_cl_fix_pkg/wide_fxp.py` | Python >53 位大位宽实现 | `wide_fxp.resize` 的整数域取模回绕与 `np.where` 饱和 |
| `matlab/src/cl_fix_resize.m` | MATLAB 端 resize | 实数域 `mod` 回绕与逻辑索引 clip，作为 Python narrow 的第二个佐证 |

> 说明：本讲重点放在 VHDL（位串域饱和的核心实现）与 Python（narrow + wide 两路径），MATLAB 与 Python narrow 在数学上等价（都在实数域），作为跨语言一致性佐证。

## 4. 核心概念与源码讲解

### 4.1 TempFmt_c：为饱和预留宽度的中间格式

#### 4.1.1 概念说明

`cl_fix_resize` 并不直接在原始位串上做截断，而是先把输入 `a` 放进一个**足够宽的中间格式 `TempFmt_c`**，所有舍入偏移加法、溢出检测、clip 改写都在 `temp_v` 上完成，最后才从 `temp_v` 切出结果。

为什么需要这个中间格式？因为饱和 clip 必须**先看见溢出，才能改写**。如果直接在结果宽度的位串上操作，一旦数值超出范围，高位早就被截掉了，根本无从判断「原本是不是溢出」。所以 `TempFmt` 必须宽到能同时容纳：

1. **输入的全部整数位**（`a_fmt.IntBits`，可能比结果还宽，否则放不下原始值）；
2. **舍入偏移可能产生的进位**（`CarryBit_c`，详见 4.2）；
3. **结果的整数位**（`result_fmt.IntBits`，clip 时要把边界值写进这个区域）；
4. **无符号饱和时所需的额外位**（`AddSignBit_c`，详见 4.2）。

#### 4.1.2 核心流程

`TempFmt_c` 三个字段的构造逻辑：

```
TempFmt.Signed   = a_fmt.Signed or result_fmt.Signed        # 只要有一端有符号，中间格式就必须有符号
TempFmt.FracBits = max(a_fmt.FracBits, result_fmt.FracBits)  # 保留全部小数位，舍入在丢位前完成
TempFmt.IntBits  = max(a_fmt.IntBits + CarryBit, result_fmt.IntBits) + AddSignBit
```

读这三行的窍门：

- **Signed 取「或」**：因为无符号数放进有符号中间格式只要高位补 0 即可（无损）；反过来有符号数放进无符号格式会丢失符号。所以「保守地取有符号」永远安全。
- **FracBits 取 max**：先保留输入与结果中较多的小数位，舍入偏移加法在小数位齐全时进行，最后再统一丢到结果的小数位。这保证舍入（u3-l2）与饱和的顺序正确。
- **IntBits 取 max**：`a_fmt.IntBits + CarryBit` 保证输入值连同舍入进位能完整放下；`result_fmt.IntBits` 保证 clip 后的边界值有地方写；`+ AddSignBit` 是无符号饱和的额外需求。

`TempFmt` 还顺带保证了一件事：**舍入偏移加法不会因为自身进位而错误触发饱和**。例如对接近结果最大值的数做 half-up 舍入，进位会让整数部分多一位，`CarryBit` 预留的这位接住了进位，使后续的溢出检测只针对「真正的数值溢出」，而非「舍入进位」。

#### 4.1.3 源码精读

`TempFmt_c` 的构造（本讲与 u3-l2 共同的关键常量）：

[en_cl_fix_pkg.vhd:2053-2058](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2053-L2058) — `Signed` 取 `a_fmt.Signed or result_fmt.Signed`（注释 `-- must stay like this!` 强调不可改顺序）；`IntBits` 用 `max(...) + AddSignBit_c` 同时容纳输入、进位、结果与无符号额外位；`FracBits` 取 `max` 保留全部小数位。

由 `TempFmt` 派生的几个位宽常量（4.3 节会用到）：

[en_cl_fix_pkg.vhd:2059-2063](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2059-L2063) — `TempWidth_c = cl_fix_width(TempFmt_c)`；`ResultWidth_c = cl_fix_width(result_fmt)`；`CutFracBits_c = TempFmt.FracBits − result_fmt.FracBits`（底部要丢的小数位）；`CutIntSignBits_c = TempWidth − (ResultWidth + CutFracBits)`（顶部要丢的整数/符号位，溢出检测窗口）。

把输入符号扩展后放进 `temp_v`：

[en_cl_fix_pkg.vhd:2072-2078](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2072-L2078) — 有符号走 `resize(signed(a_v), ...)` 做符号扩展，无符号走 `resize(unsigned(a_v), ...)` 做零扩展，统一填到 `TempWidth_c` 宽。注意输入只填到 `temp_v'high downto MoreFracBits_c`，底部 `MoreFracBits_c` 位（补出来的额外小数位）保持为 0。

#### 4.1.4 代码实践

源码阅读型实践：在脑中为一个具体场景推出 `TempFmt_c`。

设 `a_fmt = (true,4,0)`、`result_fmt = (true,2,0)`、`round = Trunc_s`、`saturate = Sat_s`（本讲综合实践的同款）：

- `DropFracBits = 0 − 0 = 0` → `NeedRound = FALSE` → `CarryBit = FALSE`。
- `AddSignBit = FALSE`（结果是有符号）。
- `TempFmt.Signed = true`；`FracBits = max(0,0) = 0`；`IntBits = max(4+0, 2) + 0 = 4`。
- 故 `TempFmt = (true,4,0)`，`TempWidth = 5`，`ResultWidth = 3`，`CutFracBits = 0`，`CutIntSignBits = 5 − 3 = 2`。

需要观察的现象：`CutIntSignBits = 2` 意味着 `temp_v` 顶部有 2 位（加上结果符号位共 3 位）参与溢出检测——这正是 4.3 节的窗口。预期：输入 `15`（`01111`）的顶部 3 位 `011` 既非全 0 也非全 1，将被判定为溢出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TempFmt.Signed` 用 `a_fmt.Signed or result_fmt.Signed`，而不是直接取 `result_fmt.Signed`？

**答案**：若输入是有符号、结果是无符号（如把负数转成无符号格式），中间计算必须先以有符号形式容纳这个负数，再在饱和阶段决定如何处理（负数对无符号结果是下溢，会被 clip 到 0）。若直接用无符号中间格式，符号信息在放入 `temp_v` 时就被零扩展抹掉了，无法正确检测溢出。

**练习 2**：`TempFmt.IntBits` 里取 `max(a_fmt.IntBits + CarryBit, result_fmt.IntBits)`，为什么要在 `a_fmt.IntBits` 上加 `CarryBit` 而不在 `result_fmt.IntBits` 上加？

**答案**：`CarryBit` 是给**舍入偏移加法**的进位预留的，而舍入加法发生在「输入值」一侧（先把 `a` 放进 `temp_v`，再加偏移），所以进位会抬升输入整数部分的最高位，必须在 `a_fmt.IntBits` 这一项上预留。`result_fmt.IntBits` 那一项只是保证 clip 后的边界值（如最大值 `011…1`）有足够位数写下。

---

### 4.2 CarryBit_c 与 AddSignBit_c：两个额外整数位的来源与作用

#### 4.2.1 概念说明

`TempFmt_c.IntBits` 公式里有两个布尔「加位」开关：`CarryBit_c` 与 `AddSignBit_c`。u3-l2 §4.2 已从舍入视角介绍过它们，本讲从**饱和视角**重新审视，并揭开 `AddSignBit` 的真实用途。

- **`CarryBit_c`**：当 `NeedRound_c`（要丢小数位且非截断）**且** `saturate /= None_s`（开启了饱和/告警）时为真。它给舍入偏移加法预留一个进位位。u3-l2 解释了为何 `None_s`（回绕）时不需要它：回绕会把进位自然取模吸收。从饱和视角看，它的作用是**防止舍入进位被误判为数值溢出**——例如对恰为最大值的数做 half-up 舍入，进位会让整数位多一位，若不预留，这个进位会被 4.3 节的溢出检测当成「超出范围」而错误 clip。

- **`AddSignBit_c`**：仅当**输入与结果都是无符号**（`a_fmt.Signed = false` 且 `result_fmt.Signed = false`）**且** `saturate /= None_s` 时为真。源码注释自承「undocumented」，u3-l2 也留作悬念。本讲给出它的真实用途：

> **`AddSignBit` 是为了让无符号饱和的 clip 逻辑能正确产生「最大值」（全 1），而不是错误地产生「最小值」（全 0）。**

它的机制在 4.4 节揭晓——简单说，无符号饱和的 clip 用 `NOT(temp_v 的最高位)` 填充所有位，要得到全 1，必须保证那个最高位是 0；`AddSignBit` 就是预留这么一个恒为 0 的「假符号位」。

#### 4.2.2 核心流程

两个开关的计算与下游影响：

```
CarryBit_c  = NeedRound_c and (saturate /= None_s)
AddSignBit_c = (not a_fmt.Signed) and (not result_fmt.Signed) and (saturate /= None_s)

TempFmt.IntBits = max(a_fmt.IntBits + CarryBit, result_fmt.IntBits) + AddSignBit
```

观察两点：

- 两个开关都要求 `saturate /= None_s`——回绕（`None_s`）模式下它们都为假，`TempFmt` 取最小宽度（刚好容下输入与结果），高位直接被取模丢弃，无需任何预留。
- `AddSignBit` 只在有符号路径不出现：因为结果有符号时，`TempFmt.Signed = true`，走的是有符号 clip 分支，那个分支自带符号位，不需要额外「假符号位」。

#### 4.2.3 源码精读

两个开关的定义（u3-l2 已引用，本讲关注其饱和含义）：

[en_cl_fix_pkg.vhd:2036-2038](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2036-L2038) — `CarryBit_c` 注释「Rounding addition is performed with an additional integer bit (carry bit)」；`AddSignBit_c` 注释坦白「It is not clear what this extra bit is for (undocumented)」——本讲 4.4 节正是要消除这个「不清楚」。

它们如何抬高 `TempFmt.IntBits`：

[en_cl_fix_pkg.vhd:2056-2056](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2056-L2056) — `IntBits => max(a_fmt.IntBits + toInteger(CarryBit_c), result_fmt.IntBits) + toInteger(AddSignBit_c)`。`CarryBit` 加在输入一侧，`AddSignBit` 加在最外层（无条件 +1，因为它要成为新的最高位）。

#### 4.2.4 代码实践

反例推演型实践：**如果不加 `AddSignBit`，无符号饱和会出错**。

设 `a_fmt = (false,4,0)`、`result_fmt = (false,2,0)`、`saturate = Sat_s`，输入值 `15`（无符号最大正数）。

- **假设没有 `AddSignBit`**：`TempFmt = (false, max(4,2), 0) = (false,4,0)`，`TempWidth = 4`，`temp_v = "1111"`。
  - `CutIntSignBits = 4 − 2 = 2`。溢出检测窗口（无符号）= 顶部 2 位 = `"11"`，非零 → 判溢出。
  - clip：`temp_v := NOT(temp_v 最高位) = NOT('1') = '0'` → 全 0 → `"0000"`。
  - 取结果低 2 位 = `"00" = 0`。**错误！** 输入 15 溢出上界，饱和应得到最大值 `3`，却得到了最小值 `0`。

- **有 `AddSignBit`（真实实现）**：`TempFmt.IntBits = max(4,2) + 1 = 5`，`TempFmt = (false,5,0)`，`TempWidth = 5`，`temp_v = "01111"`（最高位是预留的 0）。
  - `CutIntSignBits = 5 − 2 = 3`。窗口 = 顶部 3 位 = `"011"`，非零 → 判溢出。
  - clip：`temp_v := NOT(temp_v 最高位) = NOT('0') = '1'` → 全 1 → `"11111"`。
  - 取结果低 2 位 = `"11" = 3`。**正确！** 饱和到最大值。

预期：`AddSignBit` 预留的恒 0 最高位，使 `NOT(MSB) = 1`，从而把整个 `temp_v` 填成全 1，饱和到最大值。**待本地验证**：可在 VHDL testbench 里对 `(false,4,0)→(false,2,0)` 输入 15 实跑 `Sat_s`，应得 3。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `CarryBit_c` 和 `AddSignBit_c` 都要求 `saturate /= None_s`？

**答案**：回绕（`None_s`）模式下，溢出处理是「直接砍掉高位取模」，既不需要检测溢出，也不需要 clip 改写。舍入进位会被取模自然吸收，无符号也无须假符号位来生成边界值。所以两个预留位都不需要，`TempFmt` 取最小宽度以节省硬件。

**练习 2**：把「无符号→无符号饱和」改成「无符号→有符号饱和」，`AddSignBit` 还会为真吗？为什么？

**答案**：不会。`AddSignBit` 要求结果也无符号（`not result_fmt.Signed`）。一旦结果有符号，`TempFmt.Signed = true`，走的是有符号 clip 分支，那个分支用「`NOT(MSB)` 填充 + 改写结果符号位」实现双向夹紧（见 4.4），自带真符号位，不需要额外预留假符号位。

---

### 4.3 CutIntSignBits_c：从位宽差推出溢出检测窗口

#### 4.3.1 概念说明

知道 `TempFmt` 有多宽之后，下一步是判断「输入放进 `temp_v` 并做完舍入后，数值是否超出了结果格式的表示范围」。这靠一个派生量 **`CutIntSignBits_c`**——它数出 `temp_v` 顶部「会被砍掉的整数/符号位」有多少个，这些位就是溢出检测的窗口。

公式：

\[ \text{CutIntSignBits} = \text{TempWidth} - (\text{ResultWidth} + \text{CutFracBits}) \]

直观理解：`temp_v` 总共 `TempWidth` 位；最终保留的结果是底部 `ResultWidth + CutFracBits` 位（`ResultWidth` 位结果 + 其下方 `CutFracBits` 位将被丢弃的小数位，切片时一起切出再丢低位）；剩下顶部的 `CutIntSignBits` 位就是「溢出区」。如果这些高位符合符号扩展规则，说明数值在结果范围内；否则就是溢出。

**有符号**与**无符号**的「符合规则」不同，这正是检测条件分两套的原因：

- **有符号结果**：合法值要求顶部溢出位**连同结果符号位**「全 0」（正数）或「全 1」（负数，符号扩展）。若既非全 0 也非全 1，则溢出。
- **无符号结果**：合法值要求顶部溢出位「全 0」（无符号只能表示非负数，不存在符号扩展）。若任意一位为 1，则溢出（必然是上溢）。

#### 4.3.2 核心流程

溢出检测的伪代码（`slice` 为顶部检测窗口）：

```
CutIntSignBits = TempWidth - (ResultWidth + CutFracBits)

if CutIntSignBits > 0 and saturate /= None_s:        # 有高位可检且开启了饱和
    if result_fmt.Signed:                             # 有符号结果
        slice = temp_v(high downto high-CutIntSignBits)     # CutIntSignBits+1 位（含结果符号位）
        overflow = (slice /= 0) and (NOT slice /= 0)        # 既非全 0 也非全 1
    else:                                             # 无符号结果
        slice = temp_v(high downto high-CutIntSignBits+1)   # CutIntSignBits 位（不含结果最高值位）
        overflow = (slice /= 0)                             # 任意一位为 1
    if overflow:
        触发告警 / 执行 clip（见 4.4）
```

注意两个 `downto` 范围的细微差异：

- 有符号检查 `high downto high − CutIntSignBits`，共 **CutIntSignBits + 1** 位——多出的那 1 位是**结果的符号位**（结果的最高位）。因为合法的有符号值要求「溢出位 + 结果符号位」整体一致（全 0 或全 1），所以必须把结果符号位也纳入比较。
- 无符号检查 `high downto high − CutIntSignBits + 1`，共 **CutIntSignBits** 位——不含结果最高位，因为无符号结果的最高位是**值位**而非符号位，它属于「保留结果」的一部分，不应参与溢出判断。

#### 4.3.3 源码精读

`CutIntSignBits_c` 的定义：

[en_cl_fix_pkg.vhd:2062-2063](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2062-L2063) — `CutFracBits_c = TempFmt.FracBits − result_fmt.FracBits`；`CutIntSignBits_c = TempWidth_c − (ResultWidth_c + CutFracBits_c)`。两者一上一下，分别数出顶部与底部要丢弃的位数。

饱和检测的总门与两条分支：

[en_cl_fix_pkg.vhd:2105-2108](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2105-L2108) — 外层 `if CutIntSignBits_c > 0 and saturate /= None_s then` 是总开关（无高位可丢或回绕模式直接跳过）；有符号分支的条件 `temp_v(...) /= 0 and not temp_v(...) /= 0` 正是「既非全 0 也非全 1」。

无符号分支的检测条件：

[en_cl_fix_pkg.vhd:2115-2116](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2115-L2116) — `temp_v(high downto high-CutIntSignBits_c+1) /= 0`，注意范围比有符号少了 1 位（不含结果最高值位），只要顶部任意一位非零即判溢出。

#### 4.3.4 代码实践

手算型实践：为本讲综合实践 `(true,4,0) → (true,2,0)`、输入 `15`，走一遍溢出检测。

- 由 4.1.4：`TempWidth = 5`、`ResultWidth = 3`、`CutFracBits = 0`、`CutIntSignBits = 2`。
- `temp_v = "01111"`（15 的 5 位有符号表示，最高位 0 = 正数）。
- `saturate = Sat_s ≠ None_s` 且 `CutIntSignBits = 2 > 0` → 进入检测。
- 有符号分支：`slice = temp_v(4 downto 4−2) = temp_v(4 downto 2) = "011"`（3 位，含结果符号位 bit2）。
  - `"011" /= 0`？是（非全 0）。
  - `NOT("011") = "100"`，`"100" /= 0`？是（非全 1）。
  - 故 `overflow = TRUE`。

预期：判定为正方向溢出（`temp_v` 最高位 0 表示正数），4.4 节会把它 clip 到结果最大值 `3`。**待本地验证**：可用 Python `cl_fix_resize(15, FixFormat(True,4,0), FixFormat(True,2,0), FixRound.Trunc_s, FixSaturate.Sat_s)` 应返回 `3.0`。

#### 4.3.5 小练习与答案

**练习 1**：有符号检测窗口为何是 `CutIntSignBits + 1` 位，而无符号是 `CutIntSignBits` 位？

**答案**：有符号结果的最高位是符号位，合法值要求「顶部溢出位 + 结果符号位」整体为全 0（正）或全 1（负），所以必须把结果符号位一起纳入比较，窗口多 1 位。无符号结果的最高位是值位，属于保留结果，不参与溢出判断，窗口就是纯溢出位 `CutIntSignBits` 位。

**练习 2**：若 `a_fmt = (true,2,0)`、`result_fmt = (true,4,0)`（结果比输入更宽），`CutIntSignBits` 会是多少？还需要溢出检测吗？

**答案**：`TempFmt.IntBits = max(2, 4) = 4`（结果更宽，取结果），`TempWidth = 5`，`ResultWidth = 5`，`CutFracBits = 0`，`CutIntSignBits = 5 − 5 = 0`。窗口为 0，`if CutIntSignBits > 0` 为假，直接跳过整个饱和块——把数放进更宽的格式不可能溢出，符合直觉。

---

### 4.4 饱和 clip 分支：有符号与无符号两套夹紧逻辑

#### 4.4.1 概念说明

一旦 4.3 节判定溢出，`cl_fix_resize` 根据 `saturate` 决定动作：

- `Warn_s`：**只告警不夹紧**（`assert ... severity warning` 打印 `Saturation Warning`），数值原样保留（仍走末尾切片，等价回绕结果）。
- `Sat_s` / `SatWarn_s`：**夹紧到边界值**（`SatWarn_s` 同时告警）。

夹紧的难点在于：要用**同一段位操作代码**同时处理「正方向溢出 → 夹到最大值」与「负方向溢出 → 夹到最小值」。VHDL 的解法非常巧妙——**用 `temp_v` 当前最高位（MSB）作为方向指示，对低位取反填充**：

- **有符号结果**：正溢出时 `MSB = 0`，负溢出时 `MSB = 1`。
  - 先把最高位以下全部设为 `NOT(MSB)`：正溢出（MSB=0）→ 低位全 1；负溢出（MSB=1）→ 低位全 0。
  - 再把结果符号位（保留结果的最高位）强制设为 `MSB`：正溢出 → 符号位 0，得到 `0_111…1` = **最大值**；负溢出 → 符号位 1，得到 `1_000…0` = **最小值**。
- **无符号结果**：只可能正方向溢出（无符号无负数）。把**所有位**设为 `NOT(MSB)`。要让结果为全 1（最大值），必须 `MSB = 0`——这正是 4.2 节 `AddSignBit` 预留恒 0 最高位的用武之地。

#### 4.4.2 核心流程

clip 的伪代码（溢出已判定为真）：

```
# 先告警（若带 Warn）
assert saturate == Sat_s  else  report "Saturation Warning!" severity warning

if saturate /= Warn_s:                      # Sat_s 或 SatWarn_s 才真正改写
    if result_fmt.Signed:                    # 有符号：双向夹紧
        temp_v(high-1 downto 0) := NOT(temp_v(high))    # 低位全部取反填充
        temp_v(ResultWidth+CutFracBits-1)   := temp_v(high)   # 结果符号位 = MSB
    else:                                    # 无符号：夹到最大值
        temp_v := NOT(temp_v(high))          # 全部取反填充（依赖 AddSignBit 保证 MSB=0）
# 之后照常从 temp_v 切出结果
```

对应的边界值（与 u1-l3 的 `max_value`/`min_value` 一致）：

| 结果格式 | 最大值位串 | 最小值位串 |
| --- | --- | --- |
| 有符号 `(true,I,F)` | `0` 后跟全 `1`（符号位 0） | `1` 后跟全 `0`（符号位 1） |
| 无符号 `(false,I,F)` | 全 `1` | 全 `0` |

#### 4.4.3 源码精读

有符号 clip 分支：

[en_cl_fix_pkg.vhd:2106-2114](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2106-L2114) — `assert saturate = Sat_s report "..." severity warning` 实现「仅 Warn_s 之外的模式才告警」；`if saturate /= Warn_s then` 门控真正改写；`temp_v(high-1 downto 0) := (others => not temp_v(high))` 把高位以下全部取反填充；`temp_v(ResultWidth+CutFracBits-1) := temp_v(high)` 把结果符号位改写为 MSB，从而区分最大/最小值。

无符号 clip 分支：

[en_cl_fix_pkg.vhd:2115-2122](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2115-L2122) — 检测条件 `temp_v(high downto high-CutIntSignBits+1) /= 0`；clip 为 `temp_v := (others => not temp_v(high))`，**整个** `temp_v` 取反填充。这条线能否产生全 1（最大值），完全取决于 `temp_v(high)` 是否为 0——也就是 4.2 节 `AddSignBit` 是否预留了那个恒 0 最高位。

末尾从 `temp_v` 切出结果（无论是否 clip 都执行）：

[en_cl_fix_pkg.vhd:2124-2125](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2124-L2125) — `result_v := temp_v(ResultWidth+CutFracBits-1 downto CutFracBits)`。clip 改写的就是这片区域的值，切片后即得饱和结果。

#### 4.4.4 代码实践

手算型实践：把 4.3.4 的溢出场景走完 clip。

- 输入 `15`，`temp_v = "01111"`，已判定正方向溢出，`saturate = Sat_s`。
- `assert saturate = Sat_s` → 真，**不**打印告警（只有 `Warn_s`/`SatWarn_s` 才打印）。
- `saturate /= Warn_s`（Sat_s）→ 执行改写。
- 有符号分支：`temp_v(3 downto 0) := NOT(temp_v(4)) = NOT('0') = '1'` → `temp_v = "0_1111"`（bit4 仍 0，bit3..0 全 1）。
- `temp_v(ResultWidth+CutFracBits−1 = 2) := temp_v(4) = '0'` → 把 bit2 改回 0 → `temp_v = "01011"`（bit4=0,bit3=1,bit2=0,bit1=1,bit0=1）。
- 切片：`result_v = temp_v(2 downto 0) = "011" = 3`（有符号）。

预期：饱和到最大值 `3`（`(true,2,0)` 的 `max_value` = `2^2 − 2^0 = 3`）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：有符号 clip 里，为什么先 `NOT(MSB)` 填充低位，又把结果符号位改回 `MSB`？

**答案**：第一步 `NOT(MSB)` 填充是为了产生「最大值除去符号位后的全 1」或「最小值除去符号位后的全 0」主体。但结果符号位本身必须是 `MSB`（正溢出 MSB=0 → 最大值符号位 0；负溢出 MSB=1 → 最小值符号位 1），而它在第一步被设成了 `NOT(MSB)`，所以需要第二步把它改回 `MSB`。两步合起来：正溢出 → `0_111…1` = 最大值；负溢出 → `1_000…0` = 最小值。

**练习 2**：`Warn_s` 模式下，数值会发生变化吗？

**答案**：不会。`Warn_s` 只触发 `assert ... severity warning` 打印告警，但 `if saturate /= Warn_s` 为假，跳过 clip 改写，`temp_v` 保持溢出后的原值，随后照常切片——等价于回绕（wrap）的结果。即 `Warn_s` = 「回绕 + 告警」。

---

### 4.5 回绕 wrap：Python narrow 与 wide_fxp 的取模实现

#### 4.5.1 概念说明

当 `saturate = None_s` 或 `Warn_s` 时（不夹紧），三套实现都走**回绕**：把数值对结果格式的「值域宽度」取模，丢弃超出范围的高位。回绕在数学上是一个取模运算，但在三种载体里写法不同：

- **VHDL**：不显式取模——`CarryBit`/`AddSignBit` 都为假，`TempFmt` 取最小宽度，饱和块被 `saturate /= None_s` 跳过，直接走末尾切片 `result_v := temp_v(ResultWidth+CutFracBits−1 downto CutFracBits)`。切片本身丢弃高位，等价于取模。
- **Python narrow**：在实数域做取模。无符号 `rounded % 2^IntBits`；有符号用「平移到非负、取模、再平移回去」的标准二进制补码回绕技巧。
- **wide_fxp**：在任意精度整数域做取模，公式形式与 narrow 相同，但作用在「未归一化的大整数」上（`data = value × 2^FracBits`），所以取模的跨度要多乘一个 `2^FracBits`。

关键直觉——二进制补码回绕的统一公式。设结果值域宽度为 \( W \)（有符号 \( W = 2^{\text{IntBits}+1} \)，无符号 \( W = 2^{\text{IntBits}} \)，实数单位；整数单位下乘以 \( 2^{\text{FracBits}} \)）。回绕把任意值 \( x \) 映射到有符号区间 \( [−W/2, W/2) \) 或无符号区间 \( [0, W) \)：

\[ \text{signed: } x \mapsto \left( (x + W/2) \bmod W \right) - W/2, \qquad \text{unsigned: } x \mapsto x \bmod W \]

#### 4.5.2 核心流程

Python narrow 的回绕（实数域，伪代码）：

```
if sat in (None_s, Warn_s):
    # 先判断「平移相加」是否会超出 53 位精度
    if rFmt.Signed:
        addFmt = ForAdd(roundedFmt, (0, IntBits+1, 0))     # rounded + 2^IntBits 的格式
        convertToWide = cl_fix_is_wide(addFmt)
    if convertToWide:
        # 退到大整数运算，避免 float64 丢精度
        rounded_int = floor(rounded * 2^FracBits)
        span = 2^(IntBits + FracBits)
        result = (signed)  ((rounded_int + span) % (2*span)) - span
                 (unsigned) rounded_int % span
        result = result / 2^FracBits
    else:
        # 直接 float64 取模
        result = (signed)  ((rounded + 2^IntBits) % 2^(IntBits+1)) - 2^IntBits
                 (unsigned) rounded % 2^IntBits
else:  # Sat_s / SatWarn_s
    result = where(rounded > fmtMax, fmtMax, rounded)
    result = where(rounded < fmtMin, fmtMin, result)
```

`wide_fxp.resize` 的回绕（整数域，伪代码，`val` 是 `value × 2^FracBits` 的大整数）：

```
if sat in (None_s, Warn_s):
    span = 2^(rFmt.IntBits + FracBits)        # 整数单位的半域宽度
    val = (signed)  ((val + span) % (2*span)) - span
          (unsigned) val % span
else:
    val = where(val > MaxValue(rFmt).data, MaxValue(rFmt).data, val)
    val = where(val < MinValue(rFmt).data, MinValue(rFmt).data, val)
```

两个要点：

- **narrow 的 `convertToWide` 子分支**：当 `rounded + 2^IntBits` 这个中间值的格式超过 53 位（`cl_fix_is_wide(addFmt)` 为真），float64 无法精确表示，narrow 路径会临时退到 `object` dtype 的大整数做取模，再转回 float。这是 narrow 与 wide 两条路径在「回绕」处的交汇点。
- **wide 的跨度多了 `2^FracBits`**：因为 `wide_fxp` 存的是 `value × 2^FracBits`，值域宽度在整数单位下放大了 `2^FracBits` 倍。

#### 4.5.3 源码精读

Python narrow 回绕的入口与 `convertToWide` 判定：

[en_cl_fix_pkg.py:242-253](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L242-L253) — `if sat == None_s or sat == Warn_s:` 进回绕；有符号时构造 `offsetFmt = FixFormat(0, IntBits+1, 0)` 表示偏移量 `2^IntBits` 的格式，用 `ForAdd` 算出相加后的格式，再 `cl_fix_is_wide` 判断是否需要退到大整数。

narrow 的大整数回绕子分支与 float 回绕主分支：

[en_cl_fix_pkg.py:254-269](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L254-L269) — `convertToWide` 为真时把 `rounded` 转 `object` 整数、按 `satSpan = 2^(IntBits+FracBits)` 取模再转回 float；否则直接 float64 取模，有符号 `((rounded + 2.0**IntBits) % 2.0**(IntBits+1)) - 2.0**IntBits`，无符号 `rounded % 2.0**IntBits`。

narrow 的饱和 clip（与回绕对称的另一分支）：

[en_cl_fix_pkg.py:270-273](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L270-L273) — `Sat_s`/`SatWarn_s` 时用两次 `np.where` 把越界值夹到 `fmtMax`/`fmtMin`，正是 VHDL 位串 clip 在实数域的直白对应。

`wide_fxp.resize` 的回绕与饱和：

[wide_fxp.py:276-289](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L276-L289) — `satSpan = 2**(rFmt.IntBits + fr)`；有符号 `((val + satSpan) % (2*satSpan)) - satSpan`，无符号 `val % satSpan`；饱和用 `np.where` 与 `MaxValue`/`MinValue` 比较。`val` 是 `dtype=object` 的大整数数组，取模对任意位宽都精确。

`wide_fxp` 的边界值（饱和 clip 的比较基准）：

[wide_fxp.py:116-129](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L116-L129) — `MaxValue = 2^(IntBits+FracBits) − 1`；`MinValue` 有符号为 `−2^(IntBits+FracBits)`、无符号为 `0`。这些是大整数（内部数据表示），与 u1-l3 的实数边界公式只差一个 `2^FracBits` 的缩放因子。

MATLAB 的同款回绕与 clip（实数域第二个佐证）：

[cl_fix_resize.m:78-92](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_resize.m#L78-L92) — `case {Sat.None_s, Sat.Warn_s}` 用 `mod(result + 2^IntBits, 2^(IntBits+1)) - 2^IntBits`（有符号）/ `mod(result, 2^IntBits)`（无符号）回绕；`case {Sat.Sat_s, Sat.SatWarn_s}` 用逻辑索引把越界值赋为边界，与 Python narrow 的 `np.where` 思路一致。

#### 4.5.4 代码实践

手算型实践：用本讲综合实践 `(true,4,0)→(true,2,0)` 输入 `15`、`saturate = None_s`，分别在 narrow 与 wide 公式上验证回绕结果。

- **narrow（实数）**：`rounded = 15`，有符号。
  - `convertToWide`？`offsetFmt = (0,3,0)`，`addFmt = ForAdd((true,4,0),(0,3,0)) = (true,5,0)`，`cl_fix_is_wide`（宽 6 ≤ 53）→ 假。
  - 主分支：`((15 + 2^2) % 2^3) − 2^2 = ((15+4) % 8) − 4 = (19 % 8) − 4 = 3 − 4 = −1`。
- **wide（整数）**：`val = 15 × 2^0 = 15`，`span = 2^(2+0) = 4`。
  - `((15 + 4) % (2×4)) − 4 = (19 % 8) − 4 = 3 − 4 = −1`。

预期：两种公式都得 `−1`，与 VHDL 切片（`"01111"` 低 3 位 `"111"` = 有符号 −1）完全一致。**待本地验证**：`cl_fix_resize(15, FixFormat(True,4,0), FixFormat(True,2,0), FixRound.Trunc_s, FixSaturate.None_s)` 应返回 `-1.0`。

#### 4.5.5 小练习与答案

**练习 1**：为什么 Python narrow 的有符号回绕写 `((x + 2^IntBits) % 2^(IntBits+1)) − 2^IntBits`，而不是直接 `x % 2^(IntBits+1)`？

**答案**：因为实数（与 IEEE 取模）的 `x % W` 总是把结果映射到 `[0, W)`（非负），但二进制补码的有符号值域是 `[−W/2, W/2)`。所以先加 `W/2 = 2^IntBits` 把数值平移到非负区间，取模映射到 `[0, W)`，再减 `W/2` 平移回 `[−W/2, W/2)`。这等价于二进制补码「丢弃高位」的语义。

**练习 2**：narrow 路径在回绕时为何要判断 `convertToWide` 并可能退到大整数？

**答案**：有符号回绕需要计算 `rounded + 2^IntBits`，当结果格式很大（接近或超过 53 位）时，这个中间值会超出 float64 能精确表示的整数范围，取模会丢精度。`cl_fix_is_wide(addFmt)` 检测到这种情况后，narrow 路径临时切到 `dtype=object` 的任意精度整数做取模（结果精确），再转回 float，保证与 VHDL 位真一致。

---

## 5. 综合实践

**任务**：构造一个会溢出的场景，预测 `Sat_s`（饱和）与 `None_s`（回绕）两种结果，并用 `CutIntSignBits_c` 解释饱和为何 clip 到最大值。

### 5.1 实践目标

把本讲四个 VHDL 最小模块（`TempFmt_c` 构造、`CarryBit`/`AddSignBit`、`CutIntSignBits_c` 溢出检测、有符号 clip 分支）与 Python 回绕/饱和对照起来，亲历一次「溢出 → 检测 → clip/取模」的完整链路。

### 5.2 操作步骤

1. **场景设定**：`a_fmt = (true,4,0)`（5 位有符号，范围 `[−16, 15]`），`result_fmt = (true,2,0)`（3 位有符号，范围 `[−4, 3]`），`round = Trunc_s`，输入值 `15`（输入格式的最大正数，必然溢出结果上界）。

2. **派生量手算**（参考 4.1.4、4.3.4）：
   - `DropFracBits = 0` → `NeedRound = FALSE` → `CarryBit = FALSE`；`AddSignBit = FALSE`。
   - `TempFmt = (true,4,0)`，`TempWidth = 5`，`ResultWidth = 3`，`CutFracBits = 0`，`CutIntSignBits = 2`。
   - `temp_v = "01111"`。

3. **Sat_s 预测**（参考 4.3.4、4.4.4）：
   - 检测窗口 `temp_v(4 downto 2) = "011"`，既非全 0 也非全 1 → 溢出。
   - 正方向溢出（MSB=0）→ clip 到最大值 `3`（`"011"`）。

4. **None_s 预测**（参考 4.5.4）：
   - `saturate = None_s` → 跳过饱和块，直接切片。
   - `result_v = temp_v(2 downto 0) = "111"` = 有符号 `−1`（回绕）。

5. **Python 实跑验证**（在 `python/unittest` 目录下，`sys.path` 已含 `../src`）：

   ```python
   # 示例代码（非项目原有代码，供本实践使用）
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *

   aFmt  = FixFormat(True, 4, 0)
   rFmt  = FixFormat(True, 2, 0)
   a     = 15

   for sat in [FixSaturate.Sat_s, FixSaturate.None_s]:
       r = cl_fix_resize(a, aFmt, rFmt, FixRound.Trunc_s, sat)
       print(f"{sat.name:8s} -> {r}")
   ```

### 5.3 需要观察的现象

- `Sat_s` 模式：输入 `15` 远超结果上界 `3`，饱和应把它夹紧到 `3`——这正是 `temp_v` 顶部 `"011"` 被判溢出、clip 用 `NOT(MSB=0)=1` 填充并改写符号位的结果。
- `None_s` 模式：饱和块被跳过，直接取 `temp_v` 低 3 位 `"111"`，作为有符号数解释为 `−1`——一个大正数经回绕变成了负数，体现回绕的「符号反转」风险。
- 两者之差恰好是结果值域宽度 `2^(IntBits+1) = 8`（`15 − (−1) = 16`，但模 8 后 `15 ≡ 7 ≡ −1`），印证回绕是「对值域宽度取模」。

### 5.4 预期结果（待本地验证）

| `saturate` | 走的分支 | VHDL `temp_v` 处理 | 结果（实数） | 解释 |
| --- | --- | --- | --- | --- |
| `Sat_s` | 有符号 clip | MSB=0 → 低位填 `NOT(0)=1`，符号位置 `0` → `"011"` | `3` | 正溢出夹到最大值 |
| `None_s` | 回绕（切片） | 直接取低 3 位 `"111"` | `−1` | 模 8 回绕，符号反转 |

### 5.5 进阶（可选）

把输入换成负方向的极端值 `−9`（`(true,4,0)` 下 `−9` 在 `temp_v` 中为 `"10111"`），重跑 `Sat_s`：

- 检测窗口 `temp_v(4 downto 2) = "101"`，既非全 0 也非全 1 → 溢出。
- 负方向溢出（MSB=1）→ clip：低位填 `NOT(1)=0`，符号位置 `1` → `"100"` = `−4`（最小值）。
- 预期：`cl_fix_resize(−9, aFmt, rFmt, Trunc_s, Sat_s)` 返回 `−4.0`。**待本地验证**。

这印证 4.4 节的核心：同一段 clip 代码，靠 `MSB` 自动区分正/负溢出，分别夹到最大值与最小值。

## 6. 本讲小结

- `cl_fix_resize` 处理整数位溢出有两条路：**饱和**（`Sat_s`/`SatWarn_s`，夹紧到边界）与**回绕**（`None_s`/`Warn_s`，取模丢弃高位）。回绕零成本但可能符号反转，饱和更安全但要先检测后改写。
- 一切溢出处理都发生在足够宽的中间格式 `TempFmt_c` 上：它的 `IntBits = max(a_fmt.IntBits + CarryBit, result_fmt.IntBits) + AddSignBit` 同时容纳输入、舍入进位、结果宽度与无符号额外位。
- `CarryBit_c` 给舍入进位预留位置，防止进位被误判为数值溢出（仅 `saturate /= None_s` 时需要）；`AddSignBit_c` 为无符号→无符号饱和预留恒 0 的「假符号位」，使 clip 的 `NOT(MSB)` 填充能产生全 1（最大值）而非全 0——这是源码注释里「undocumented」一位的真实用途。
- 溢出检测靠 `CutIntSignBits_c = TempWidth − (ResultWidth + CutFracBits)` 这个顶部窗口：有符号检查「窗口+结果符号位」是否既非全 0 也非全 1（共 `CutIntSignBits+1` 位）；无符号检查窗口是否非全 0（共 `CutIntSignBits` 位）。
- VHDL 的 clip 用 `NOT(MSB)` 填充低位这一巧技：有符号再改写结果符号位为 `MSB`，一段代码同时实现夹到最大值（正溢出）与最小值（负溢出）；无符号全位填充依赖 `AddSignBit` 保证 `MSB=0`。
- Python narrow 在实数域用 `np.where` clip、用「平移-取模-平移」回绕（大格式时经 `convertToWide` 退到大整数）；`wide_fxp` 在任意精度整数域做同款取模，跨度多一个 `2^FracBits` 因子。三套实现位真一致。

## 7. 下一步学习建议

- 本讲与 u3-l2 合起来，完整覆盖了 `cl_fix_resize` 的舍入（丢小数位）与饱和/回绕（丢整数位）。下一讲 **u3-l4《取整辅助、比较与范围检查》** 讲基于 resize 的 `cl_fix_fix/floor/ceil/round`、`cl_fix_in_range` 与 `cl_fix_compare`，建议紧接着读，观察它们如何复用本讲的饱和机制。
- 进入 **Unit 4** 的运算函数（`cl_fix_add/sub/mult/shift/mean/abs`）后，重点观察它们如何构造各自的 `TempFmt_c`（如 `ForAdd`/`ForSub`/`ForMult`/`ForShift`/`ForNeg`）并在末尾调用 `cl_fix_resize`——你会看到本讲的饱和 clip 在真实运算链路（如乘法后饱和、加法溢出）里被反复触发。
- 想深入大位宽的读者可跳到 **Unit 6**：u6-l1 讲 `cl_fix_is_wide` 的 53 位边界与 narrow/wide 派发，u6-l2 讲 `wide_fxp` 类的整数存储与运算符重载。本讲 4.5 节的 `convertToWide` 子分支与 `wide_fxp` 取模是其前置缩影。
- 推荐继续精读的源码：[en_cl_fix_pkg.vhd:2105-2123](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L2105-L2123)（VHDL 饱和检测与 clip 双分支）与 [wide_fxp.py:276-289](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/wide_fxp.py#L276-L289)（wide 回绕/饱和），把两者并排读能最直观地体会「位串 clip」与「整数取模」的等价性。
