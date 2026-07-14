# 核心转换：round / saturate / resize 实现

## 1. 本讲目标

本讲带你走进 `en_cl_fix` 库最核心的「格式转换」实现层。读完本讲，你应当能够：

- 说清楚 `convert()` 如何在不做任何舍入/饱和的前提下，把一个 `[S,I,F]` 定点数搬到另一个格式（含二进制小数点对齐、符号扩展、低位补零）。
- 看懂 `get_half` / `get_unit_bit` / `resize_sensible` 三个内部小工具各自解决什么问题。
- 逐行跟踪 `cl_fix_round` 的「构造 mid 格式 → 加偏移 → 截断」三段式，并理解七种舍入模式为什么只差一个 `±1` 的微调。
- 读懂 `cl_fix_saturate` 的「恒先回绕、再按需钳位、按需告警」三段式。
- 理解 `cl_fix_resize = cl_fix_round ⟶ cl_fix_saturate` 这个组合的顺序为什么不可交换。

> 本讲只讲「转换」类函数的**位运算实现**。舍入/饱和/格式预测的**语义与数学推导**已在 u2-l2、u2-l3、u3-l3 建立，本讲不再重复，而是在它们之上落到真实的 VHDL 位级操作。

## 2. 前置知识

本讲假设你已经掌握（来自前置讲义）：

- **定点格式 `[S,I,F]`**（u2-l1）：S 符号位、I 整数位、F 小数位，总宽 `W = S+I+F`，位权重以最低位 `2^{-F}` 为锚点。
- **舍入模式 `FixRound_t`**（u2-l2）：七种模式唯一差别在「平局（tie）」处理；补码直接截断等于朝 `-∞` 取整（floor）。
- **饱和模式 `FixSaturate_t`**（u2-l3）：`None/Warn/Sat/SatWarn` 是「是否钳位 × 是否告警」的笛卡尔积；不钳位则高位被丢弃，补码下表现为回绕（wrap）。
- **结果格式预测**（u3-l3）：`cl_fix_round_fmt` 会预测「非 Trunc 模式整数位 +1」的结果格式。
- **VHDL 包头公共 API**（u5-l1）：类型 `FixFormat_t`、默认参数约定（`round→Trunc_s`、`saturate→Warn_s`、`result_fmt→NullFixFormat_c`）、私有包 `en_cl_fix_private_pkg`（提供 `choose/to01/maximum/minimum` 等，因 VHDL-93 不自带）。

几个本讲会用到的 VHDL 小知识：

- `numeric_std` 的 `resize(x, n)`：对 `unsigned` 高位补零、对 `signed` 做**符号扩展**；但 `signed` 的 `resize` 在**截断**时会保留符号位，这与「普通截断」不同——这正是本讲要自己实现 `resize_sensible` 的原因。
- VHDL 里给 `natural` 子类型的常量赋负值，会在**编译/细化期**直接报错——这是 `convert()` 用来「禁止减少小数位」的廉价护栏。
- 本包遵循 **VHDL-93**，所以 `maximum/minimum/'image` 都不能用语言自带的，要走私有包。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 本讲关注的部分 | 作用 |
|------|--------------|------|
| [`hdl/en_cl_fix_pkg.vhd`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | `convert`、`get_half`、`get_unit_bit`、`resize_sensible`、`cl_fix_round`、`cl_fix_saturate`、`cl_fix_resize` | 转换层全部实现 |
| [`hdl/en_cl_fix_private_pkg.vhd`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd) | `choose`、`to01` | 转换层依赖的私有工具 |

一句话定位：`convert` 是「格式搬运工」，`cl_fix_round` 和 `cl_fix_saturate` 是「裁剪工」，`cl_fix_resize` 是把两位裁剪工串起来的「调度员」。`get_half/get_unit_bit/resize_sensible` 是它们共用的三把小工具。

## 4. 核心概念与源码讲解

### 4.1 convert()：无舍入、无饱和的格式搬运

#### 4.1.1 概念说明

很多时候我们只是想把一个定点数从 `aFmt` 搬到 `rFmt`，**不改变它的数值**，只是换一种位宽/小数点位置来表示。这就是 `convert` 的职责：纯粹地重排位、做小数点对齐、必要时做符号/零扩展。

关键约束（来自源码注释）：

- `convert` **不支持** `rFmt.F < aFmt.F`（减少小数位）。要减少小数位必须走 `cl_fix_round`（哪怕用 `Trunc_s`）。
- `convert` **支持** `(rFmt.S+rFmt.I) < (aFmt.S+aFmt.I)`（减少整数/符号位），但**不做饱和**——多余高位直接丢弃，等价于 `cl_fix_saturate` 在 `None_s` 模式下的「回绕」。

换句话说：`convert` 自身就是一个「回绕式」的格式转换器，它是 `cl_fix_round`/`cl_fix_saturate`/所有数学函数共用的底层积木。

#### 4.1.2 核心流程

设 `offset = rFmt.F − aFmt.F`（结果比输入多出多少个小数位）：

1. 申请一个宽度 `cl_fix_width(rFmt)` 的结果向量，初值全 `'0'`。
2. 把输入按二进制小数点对齐写入结果的高位段：`result(高 downto offset) := 扩展后的输入`。
3. 低 `offset` 位保持为 `'0'`（这就是「F 变大时低位补零」）。
4. 扩展方式：无符号输入走 `resize(unsigned)` 零扩展；有符号输入走 `resize_sensible(signed)` 符号扩展。

数学上，`convert` 保持数值 \(v\) 不变，只是改变表示：

\[
v = \sum_{k} b_k \cdot 2^{k - F_a} = \sum_{k} b'_k \cdot 2^{k - F_r}, \qquad \text{当 } F_r > F_a \text{ 时低位补 } 0.
\]

当 `rFmt.S+rFmt.I < aFmt.S+aFmt.I` 时高位被丢弃，数值可能因回绕而改变——这是调用者的责任。

#### 4.1.3 源码精读

[`hdl/en_cl_fix_pkg.vhd`:L329-L351](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L329-L351) 是 `convert` 的全部实现：

- L339 `constant offset_c : natural := rFmt.F - aFmt.F;` —— 注意类型是 `natural`。若 `rFmt.F < aFmt.F`，右端为负，给 `natural` 赋负值会在**细化期直接报错**。这是「禁止减少小数位」的廉价编译期护栏，不需要运行期 `assert`。
- L340 结果向量 `result_v` 初值全 `'0'`：低位补零正是靠这个默认值实现的。
- L344–L348 按符号性二选一：无符号用 `resize(unsigned(...))`（零扩展高位），有符号用 `resize_sensible(signed(...))`（符号扩展高位）。写入区间都是 `(r_width-1 downto offset_c)`，即对齐到小数点后跳过低 `offset_c` 位。

注释 L333–L337 把上面两条约束写得很清楚，值得直接读原文。

#### 4.1.4 代码实践（源码阅读型）

**目标**：验证 `convert` 在 `rFmt.F > aFmt.F` 时如何「低位补零」。

**操作**：在 [`hdl/en_cl_fix_pkg.vhd`:L329-L351](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L329-L351) 中代入 `aFmt=(0,4,1)`、`rFmt=(0,4,4)`，手算 `convert(0x05, (0,4,1), (0,4,4))`：

1. `offset_c = 4 − 1 = 3`。
2. `r_width = 0+4+4 = 8`，`result_v` 初值 `= "00000000"`。
3. 写入区间 `(7 downto 3)`，宽度 `8−3=5`。输入 `0x05` 是 `[0,4,1]` 下的 `0b00101`（数值 `5/2 = 2.5`），零扩展为 5 位 `0b00101`。
4. `result_v(7 downto 3) := "00101"`，低 3 位保持 `0`，最终 `result_v = "00101_000" = 0x28`。
5. 在 `[0,4,4]` 下 `0x28 = 40`，`40/16 = 2.5`，数值不变 ✓。

**预期现象**：低 `offset_c=3` 位被填零，相当于把小数点左移 3 位、数值保持 2.5。

**待本地验证**：若你有仿真器，可在 testbench 里 `report to_string(convert(x"05", (0,4,1), (0,4,4)), (0,4,4));` 观察输出字符串，确认低位为 `000`。

#### 4.1.5 小练习与答案

**Q1**：为什么 `convert` 用 `natural` 类型的 `offset_c` 而不是加一个运行期 `assert` 来禁止 `rFmt.F < aFmt.F`？

**答**：`natural` 子类型在**编译/细化期**就会对越界赋值报错，错误更早暴露、零运行成本；运行期 `assert` 要等仿真跑到那一行才发现。

**Q2**：`convert` 减少整数位时不饱和、直接丢高位，这在数值上等价于 `cl_fix_saturate` 的哪种模式？

**答**：等价于 `None_s`（回绕、不告警）。

---

### 4.2 三件套小工具：get_half / get_unit_bit / resize_sensible

#### 4.2.1 概念说明

舍入与饱和的实现反复需要三件小事，于是抽出三个内部函数：

- **`get_half(aFmt, rFmt)`**：生成一个「半个结果 LSB 权重」的常量向量。它是所有非 Trunc 舍入偏移的基础（`+0.5` 里的那个 `0.5`）。
- **`get_unit_bit(a, aFmt, rFmt)`**：取出「结果最低有效位（unit / LSB）」那一比特的值，供收敛舍入（Convergent）判断凑偶/凑奇。
- **`resize_sensible(a, n)`**：一个「更合理」的 `signed` 位宽调整——扩展时正常符号扩展，截断时**普通截断**而非 `numeric_std.resize` 的「保符号位截断」。

#### 4.2.2 核心流程

**get_half**：结果 LSB 的权重是 \(2^{-rFmt.F}\)，其一半是 \(2^{-(rFmt.F+1)}\)。在 `aFmt` 表示下，权重 \(2^{-(rFmt.F+1)}\) 对应的位下标是：

\[
\text{tie\_c} = aFmt.F - rFmt.F - 1
\]

于是构造一个 `aFmt` 宽度的全零向量，把第 `tie_c` 位置 `1`。

**get_unit_bit**：结果 LSB 在 `aFmt` 中的下标是 `unit_c = aFmt.F - rFmt.F`。

- 若 `unit_c < cl_fix_width(aFmt)`：该位是显式存在的，直接取 `a(unit_c)`。
- 若 `unit_c >= cl_fix_width(aFmt)`：结果 LSB 落在「隐式高位扩展区」，其值等于符号位（正数隐含无穷多个 `0`、负数无穷多个 `1`），故返回 `cl_fix_sign(a, aFmt)`。

**resize_sensible**：

- `n >= a'length`（要变宽）：调用标准 `resize`，做符号扩展。
- `n < a'length`（要变窄）：直接取低 `n` 位 `a(n-1 downto 0)`，做**普通截断**。

#### 4.2.3 源码精读

- [`hdl/en_cl_fix_pkg.vhd`:L290-L297](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L290-L297) `get_half`：L291 算 `tie_c`，L295 置位。注意返回的是 `aFmt` 宽度的 `unsigned`，便于直接与 `aFmt` 宽度的中间值相加。
- [`hdl/en_cl_fix_pkg.vhd`:L299-L311](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L299-L311) `get_unit_bit`：L302 算 `unit_c`，L304–L310 区分「隐式 MSB 扩展」与「显式取位」两分支。`cl_fix_sign` 见 [`hdl/en_cl_fix_pkg.vhd`:L1315-L1323](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1315-L1323)（无符号或 0 位返回 `'0'`，否则取最高位）。
- [`hdl/en_cl_fix_pkg.vhd`:L313-L327](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L313-L327) `resize_sensible`：L318–L320 扩展走标准 `resize`；L321–L325 截断走纯位切片，注释 L322–L323 解释了为何不用 `numeric_std.resize`（后者截断时保留符号位，对定点「丢精度」场景不合适）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：手算 `get_half` 与 `get_unit_bit` 的位下标，建立「权重↔下标」的直觉。

**操作**：设 `aFmt=(0,4,4)`、`rFmt=(0,4,1)`（与 4.3 的舍入示例一致）：

1. `get_half`：`tie_c = 4 − 1 − 1 = 2` → 返回 `aFmt` 宽 8 位、第 2 位为 1 的向量 `0b00000100 = 4`。权重 \(2^{2-4}=2^{-2}=0.25\)，恰是结果 LSB \(2^{-1}=0.5\) 的一半 ✓。
2. `get_unit_bit`：`unit_c = 4 − 1 = 3`，宽度 8，`3 < 8`，故取 `a(3)`——即结果 LSB 在中间值里的那一比特。

**预期结果**：`half_c = 0b100`，`unit_v = a 的第 3 位`。

#### 4.2.5 小练习与答案

**Q1**：若 `rFmt.F` 比 `aFmt.F` 大很多，`get_unit_bit` 的 `unit_c` 可能变成负数吗？此时会发生什么？

**答**：不会变负被用到——`cl_fix_round` 只在 `result_fmt.F < a_fmt.F`（即确实在减少小数位）时才调用 `get_unit_bit`，此时 `unit_c = aFmt.F − rFmt.F ≥ 0`。

**Q2**：为什么 `resize_sensible` 截断时不直接用 `numeric_std` 的 `resize`？

**答**：`numeric_std.resize` 对 `signed` 截断时会保留符号位（为保符号），而定点的精度收敛需要的是「纯丢低位」的普通截断，二者语义不同。

---

### 4.3 cl_fix_round：mid 格式 + 偏移截断

#### 4.3.1 概念说明

`cl_fix_round` 解决「减少小数位时的舍入」。它的核心思想一句话：**所有舍入模式 = 截断(x + offset)**，七种模式只在 `offset` 上差一个 `±1` 的微调。

为此它先构造一个比 `result_fmt` 多 1 位小数的**中间格式 `mid_fmt`**，把输入无损搬进去，加上合适偏移，再截断低位得到结果。`mid_fmt` 额外那 1 位小数，正是为了容纳「平局位」与「进位」。

#### 4.3.2 核心流程

\[ \text{round}(x) = \text{trunc}\bigl(x + \text{offset}(\text{mode}, \text{sign}, \text{unit})\bigr) \]

具体步骤（见源码 L916–L975）：

1. **格式契约检查**（可选）：若 `fmt_check=true`，断言 `result_fmt = cl_fix_round_fmt(a_fmt, result_fmt.F, round)`，即调用者给的格式必须等于库预测的最坏情况格式。这就是 u3-l3 讲的「格式契约」。
2. **构造 mid_fmt**：`mid_fmt = (result_fmt.S, result_fmt.I, max(result_fmt.F+1, a_fmt.F))`。强制至少 `result_fmt.F+1` 位小数，留出平局位与进位空间。
3. **搬入中间值**：`mid_v := convert(a, a_fmt, mid_fmt)`（无损对齐）。
4. **按模式加偏移**（仅当 `result_fmt.F < a_fmt.F`，即确有小数位被丢弃时；否则无需舍入）：

   | 模式 | 偏移（相对 `half_c`） | 直觉 |
   |------|----------------------|------|
   | `Trunc_s` | `+0` | 纯截断 |
   | `NonSymPos_s` | `+half_c` | `floor(x+0.5)`，平局朝 +∞ |
   | `NonSymNeg_s` | `+half_c − 1` | 平局朝 −∞ |
   | `SymInf_s` | `+half_c − sign` | 平局朝远离 0 |
   | `SymZero_s` | `+half_c − (¬sign)` | 平局朝 0 |
   | `ConvEven_s` | `+half_c − (¬unit)` | 平局凑偶 |
   | `ConvOdd_s` | `+half_c − unit` | 平局凑奇 |

   其中 `half_c` 是 `get_half`（半个结果 LSB），`sign` 是输入符号位，`unit` 是结果 LSB（`get_unit_bit`）。`("" & bit)` 是把单比特拼成 1 位向量、按 0/1 参与无符号运算的惯用写法。

5. **截断低位**：`result_v := mid_v(width(result_fmt)+out_offset-1 downto out_offset)`，丢掉低 `out_offset = mid_fmt.F − result_fmt.F` 位。

#### 4.3.3 源码精读

[`hdl/en_cl_fix_pkg.vhd`:L910-L976](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L910-L976) 是 `cl_fix_round` 全部实现：

- L925–L929 `mid_fmt_c`：构造中间格式，`max(result_fmt.F+1, a_fmt.F)` 是关键——既保证留出平局位，又不浪费（`a_fmt.F` 已经够大时直接复用）。
- L930–L932 `in_offset_c / out_offset_c / half_c`：`half_c` 由 `get_half(mid_fmt_c, result_fmt)` 得到（注意是用 `mid_fmt` 当 `aFmt` 算下标，因为偏移是加到 `mid_v` 上的）。
- L933 `sign_c := cl_fix_sign(...)`：提取符号位供对称/收敛模式用。
- L939–L942 **格式契约** `assert`：`result_fmt = cl_fix_round_fmt(...)`，与 u3-l3 衔接。
- L945 `mid_v := unsigned(convert(...))`：无损搬入中间格式。
- L948–L970 **case 分支**：仅当 `result_fmt.F < a_fmt.F` 才进入；七种模式各加不同偏移，注释 L947「add an appropriate offset before truncating」点明统一机制。
- L973 **截断**：取 `mid_v` 的高位段，丢弃低 `out_offset_c` 位，落回 `result_fmt` 宽度。

> 一个常被忽略的细节：L948 的 `if result_fmt.F < a_fmt.F then` 守卫意味着——**当结果小数位不少于输入时，根本不做任何偏移**，因为此时没有「丢位」也就没有「平局」，`convert` 的低位补零已经足够。这也解释了为何 `cl_fix_recommended_pipelining` 在 `result_fmt.F >= a_fmt.F` 时返回 0（无需寄存器）。

#### 4.3.4 代码实践（动手跟踪型）

**目标**：完整跟踪一个真实舍入 `cl_fix_round(a, (0,4,4), (0,4,1), NonSymPos_s)` 的全过程。

**操作步骤**：取输入值 `2.375`。在 `[0,4,4]` 下，`2.375 = 0b10.0110`，即 `a = "00100110" = 0x26`（验证：`38/16 = 2.375` ✓）。

1. **格式契约**：`cl_fix_round_fmt((0,4,4), 1, NonSymPos_s)` 因非 Trunc → 整数位 +1，得 `(0,5,1)`……但这里 `result_fmt=(0,4,1)`。**注意**：`NonSymPos_s` 理论上可能让整数位 +1，所以严格预测格式是 `(0,5,1)`。本例若用 `(0,4,1)` 作 `result_fmt`，`fmt_check` 默认为 `true` 会触发 assert 失败。
   - 为了让示例可跑通，把 `result_fmt` 设成 `(0,5,1)`（留出进位位），或把 `fmt_check` 设 `false`。下面按「数值演示」继续，先忽略契约，只看位运算。
2. **mid_fmt**：`(0, 4, max(1+1, 4)) = (0,4,4)`，与 `a_fmt` 相同。
3. **搬入**：`mid_v = 0x26 = 0b00100110 = 38`。
4. **half_c**：`get_half((0,4,4),(0,4,1))` → `tie_c=2` → `0b100 = 4`（权重 0.25）。
5. **加偏移（NonSymPos_s）**：`mid_v = 38 + 4 = 42 = 0b00101010`。
6. **截断**：`out_offset_c = 4−1 = 3`，`width(result_fmt)=5`，取 `mid_v(5+3-1 downto 3) = mid_v(7 downto 3) = 0b00101 = 5`。
7. **读回**：在 `[0,4,1]` 下 `5/2 = 2.5`，正是 `floor(2.375 + 0.5) = floor(2.875) = 2.5` ✓。

**需要观察的现象**：偏移 `+half_c` 把 `0b0110` 的「平局位以上」推过半，截断后正好实现 `floor(x+0.5)`。

**预期结果**：输出整数 `5`（在 `[0,4,1]` 下表示 `2.5`）。

**关于格式契约**：真实代码里 `result_fmt` 应传 `cl_fix_round_fmt((0,4,4), 1, NonSymPos_s)` 的返回值。你可以阅读 [`bittrue/tests/python/cl_fix_round_test.py`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py) 看测试如何用预测格式驱动 `cl_fix_round`，并用 numpy 参考实现逐模式比对。

#### 4.3.5 小练习与答案

**Q1**：为什么 `mid_fmt` 强制 `result_fmt.F+1` 位小数，而不是直接用 `a_fmt.F`？

**答**：当 `a_fmt.F` 比 `result_fmt.F` 只多 1 位时，`max(result_fmt.F+1, a_fmt.F)` 仍保证至少多 1 位，留出平局位与可能的进位；当 `a_fmt.F` 已更大时则直接复用，不浪费。

**Q2**：`SymInf_s` 的偏移 `+half_c − sign`，为什么正数 `sign=0` 时等价于 `NonSymPos_s`、负数 `sign=1` 时等价于 `NonSymNeg_s`？

**答**：`sign=0` 时偏移就是 `+half_c`（= NonSymPos）；`sign=1` 时偏移是 `+half_c − 1`（= NonSymNeg）。对称「朝外」= 正数半-up、负数半-down，正好远离零。

**Q3**：`ConvEven_s`（凑偶）为什么用 `+half_c − (¬unit)`？

**答**：当结果 LSB `unit=1`（当前为奇）时，偏移为 `+half_c`（半-up，凑成偶）；当 `unit=0`（当前为偶）时，偏移为 `+half_c − 1`（半-down，保持偶）。平局时总是偏向偶数。

---

### 4.4 cl_fix_saturate：告警 + 钳位（先回绕、后钳位）

#### 4.4.1 概念说明

`cl_fix_saturate` 解决「减少整数位/符号位时的越界处理」。它把四种饱和模式拆成三个独立动作：

1. **恒先回绕**：调用 `convert` 把数值搬到 `result_fmt`，多余高位直接丢弃（这一步永远做，对应 `None_s` 的回绕）。
2. **按需告警**：`Warn_s` / `SatWarn_s` 模式下，用 `assert ... severity Warning` 在越界时报警。
3. **按需钳位**：`Sat_s` / `SatWarn_s` 模式下，比较输入与 `result_fmt` 的极值，越界则钉在 `cl_fix_min_value` / `cl_fix_max_value`。

约束：饱和**不允许改变小数位**（`assert result_fmt.F = a_fmt.F`），因为饱和只管整数/符号位的范围。

#### 4.4.2 核心流程

\[ \text{out} = \begin{cases} v_{\min} & \text{若 } v < v_{\min} \text{ 且启用 Sat} \\ v_{\max} & \text{若 } v > v_{\max} \text{ 且启用 Sat} \\ \text{wrap}(v) & \text{否则（含 None/Warn 的回绕）} \end{cases} \]

步骤（见源码 L978–L1007）：

1. `assert result_fmt.F = a_fmt.F`（小数位不可变）。
2. 若模式含 `Warn`：`assert cl_fix_in_range(...)` 以 `Warning` 严重级别在越界时报警。
3. `result_v := convert(a, a_fmt, result_fmt)`——**永远先回绕**。
4. 若模式含 `Sat`：用 `cl_fix_compare` 比较 `a` 与极值，越下界则钳到 `cl_fix_min_value`，越上界则钳到 `cl_fix_max_value`。

> 注意第 3 步：钳位是在**回绕之后**的条件赋值覆盖。对于不越界的值，回绕结果与钳位结果一致；对于越界值，钳位覆盖回绕值。`cl_fix_compare` 和 `cl_fix_in_range` 都是在**原始 `a_fmt` 与 `result_fmt` 之间跨格式比较**，先把双方 `convert` 到公共 `union` 格式再比，所以比较的是真实数值而非位模式。

#### 4.4.3 源码精读

[`hdl/en_cl_fix_pkg.vhd`:L978-L1007](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L978-L1007) 是 `cl_fix_saturate` 全部实现：

- L986 `assert result_fmt.F = a_fmt.F`：小数位不变的前置条件。
- L989–L992 **告警**：`Warn_s`/`SatWarn_s` 时，`assert cl_fix_in_range(...)` 以 `Warning` 严重级别触发。`cl_fix_in_range`（[`hdl/en_cl_fix_pkg.vhd`:L1024-L1039](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1024-L1039)）会先按舍入模式把 `a` 量化、再跨格式比范围——所以告警判定与后续实际 resize 用的舍入一致。
- L995 **回绕**：无条件 `convert`，等价于 `None_s` 行为。
- L998–L1004 **钳位**：含 `Sat` 时，`cl_fix_compare("<", a, a_fmt, min, result_fmt)` 越下界钳到 `cl_fix_min_value(result_fmt)`，`">"` 越上界钳到 `cl_fix_max_value(result_fmt)`。

`cl_fix_compare` 本身见 [`hdl/en_cl_fix_pkg.vhd`:L1274-L1313](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1274-L1313)：把两操作数 `convert` 到 `union(aFmt,bFmt)` 后按符号性用 `signed`/`unsigned` 比较。

#### 4.4.4 代码实践（源码阅读型）

**目标**：定位「触发告警的 assert」与「钳位的比较」，确认四种模式分别走到哪些分支。

**操作**：

1. 在 [`hdl/en_cl_fix_pkg.vhd`:L978-L1007](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L978-L1007) 中，对照下表把每个模式映射到命中的动作：

   | 模式 | 命中 L989 告警？ | 命中 L998 钳位？ | 行为 |
   |------|:---:|:---:|------|
   | `None_s` | 否 | 否 | 纯回绕 |
   | `Warn_s` | 是 | 否 | 回绕 + 告警 |
   | `Sat_s` | 否 | 是 | 钳位 |
   | `SatWarn_s` | 是 | 是 | 钳位 + 告警 |

2. 再看 [`bittrue/tests/python/cl_fix_saturate_test.py`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_saturate_test.py)（参考实现测试），确认四种模式的期望输出与上表一致。

**预期结果**：四种模式恰好由「告警开关」与「钳位开关」两个独立布尔组合得到，没有第五种行为。

#### 4.4.5 小练习与答案

**Q1**：为什么 `cl_fix_saturate` 要 `assert result_fmt.F = a_fmt.F`？

**答**：饱和只处理整数/符号位的范围越界；改变小数位属于「舍入」职责（`cl_fix_round`），二者必须分离。`convert` 本来也不支持减少小数位。

**Q2**：`Sat_s` 模式下，一个不越界的值会经历哪些步骤？

**答**：跳过告警（不含 `Warn`）；执行 `convert` 回绕（对不越界值等价于无变化）；执行钳位比较，因不越界而两个 `if/elsif` 都不成立，最终返回回绕值。

---

### 4.5 cl_fix_resize：先舍入、后饱和的组合器

#### 4.5.1 概念说明

真实设计里，把一个数从 `a_fmt` 转到 `result_fmt` 通常**同时**需要减少小数位（舍入）和减少整数/符号位（饱和）。`cl_fix_resize` 就是把这两步安全串起来的顶层函数。

它的顺序是固定的：**先 `cl_fix_round`，后 `cl_fix_saturate`**，且不可交换。原因有二：

1. **饱和要求小数位不变**（见 4.4），所以必须先由 round 把小数位减到目标，饱和阶段才能合法。
2. **舍入可能产生整数位 +1 的进位**（见 u3-l3 的 `cl_fix_round_fmt`），这个进位必须**先发生**，饱和阶段才能正确判断是否越界。

#### 4.5.2 核心流程

\[ \text{resize}(a) = \text{sat}\bigl(\text{round}(a),\ \text{round\_fmt}\bigr) \]

步骤（见源码 L1009–L1022）：

1. 用 `cl_fix_round_fmt(a_fmt, result_fmt.F, round)` 预测「舍入后」的格式 `rounded_fmt`（可能比 `a_fmt` 多 1 位整数，承载进位）。
2. 调 `cl_fix_round(a, a_fmt, rounded_fmt, round)` 得到舍入后的中间值 `rounded`。
3. 调 `cl_fix_saturate(rounded, rounded_fmt, result_fmt, saturate)` 把整数/符号位收敛到 `result_fmt`。

注意 `rounded_fmt.F == result_fmt.F`（都是 `result_fmt.F`），所以第 3 步的 `assert result_fmt.F = a_fmt.F`（此时「a」是 `rounded_fmt`）自然满足。

#### 4.5.3 源码精读

[`hdl/en_cl_fix_pkg.vhd`:L1009-L1022](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1009-L1022) 是 `cl_fix_resize` 全部实现——只有两行有效逻辑：

- L1017 `rounded_fmt_c := cl_fix_round_fmt(...)`：预测舍入后格式。`cl_fix_round_fmt`（[`hdl/en_cl_fix_pkg.vhd`:L608-L628](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L608-L628)）在非 Trunc 且确在减少小数位时令整数位 +1，正是为承载「半-up 进位」。
- L1018 `rounded_c := cl_fix_round(a, a_fmt, rounded_fmt_c, round)`：执行舍入。
- L1021 `return cl_fix_saturate(rounded_c, rounded_fmt_c, result_fmt, saturate)`：执行饱和。

> 这正是为什么 u4-l1 把 `cl_fix_resize` 称为「精度收敛器」：所有数学函数（`cl_fix_add/mult/...`）都先在全精度 `mid_fmt` 下做无损运算，最后统一交给 `cl_fix_resize` 把精度收敛到目标格式。`resize` 的两步顺序，决定了「舍入误差」与「饱和范围」被正确地分开处理。

#### 4.5.4 代码实践（源码阅读型）

**目标**：跟踪 `cl_fix_resize` 内部两步格式的变化，理解「先 round 后 saturate」的必要性。

**操作**：设 `a_fmt=(1,7,8)`（有符号、8 位小数），`result_fmt=(1,3,2)`，`round=NonSymPos_s`，`saturate=SatWarn_s`。

1. `rounded_fmt = cl_fix_round_fmt((1,7,8), 2, NonSymPos_s)`：非 Trunc、且 `2 < 8` 确在减少小数位 → 整数位 +1 → `(1,8,2)`。
2. `rounded = cl_fix_round(a, (1,7,8), (1,8,2), NonSymPos_s)`：把小数位从 8 减到 2（舍入 6 位），整数位因进位留到 8。
3. `cl_fix_saturate(rounded, (1,8,2), (1,3,2), SatWarn_s)`：整数位从 8 收敛到 3，越界则钳位并告警。

**需要观察的现象**：若把顺序反过来（先饱和到 `(1,3,8)` 再 round 到 `(1,3,2)`），舍入产生的进位会丢失或落到错误范围——这正是顺序不可交换的实证。

**预期结果**：`rounded_fmt=(1,8,2)`，最终饱和到 `(1,3,2)`。

#### 4.5.5 小练习与答案

**Q1**：`cl_fix_resize` 为什么不直接调一次 `convert`？

**答**：`convert` 既不能减少小数位（`natural` 偏移护栏），也不做饱和。resize 需要的「舍入 + 饱和」必须由 round 与 saturate 两步完成。

**Q2**：如果 `result_fmt.F >= a_fmt.F`（不减少小数位），`cl_fix_resize` 的 round 步骤实际做了什么？

**答**：`cl_fix_round_fmt` 此时整数位不变（不减位则不进位），`cl_fix_round` 因 `result_fmt.F >= a_fmt.F` 跳过偏移、仅做 `convert` 低位补零；实质上 round 步骤退化为无损搬运，真正起作用的只剩 saturate 步骤。

**Q3**：`rounded_fmt_c.F` 一定等于 `result_fmt.F` 吗？这对第 3 步的 assert 有何意义？

**答**：是的，`cl_fix_round_fmt` 的第三个参数就是 `result_fmt.F`，返回格式的 F 即为该值；所以第 3 步 `cl_fix_saturate` 的 `assert result_fmt.F = a_fmt.F`（此处 a 是 `rounded_fmt`）恒成立，不会误触发。

## 5. 综合实践

把本讲的四个函数串成一个完整的「精度收敛」追踪任务。

**场景**：模拟一个乘法后端——两个数相乘得到全精度积，再收敛到一个窄格式。设：

- `a = cl_fix_from_real(1.6, (0,2,4))`，`b = cl_fix_from_real(2.4, (0,2,4))`。
- 全精度积格式 `mid_fmt = cl_fix_mult_fmt((0,2,4),(0,2,4)) = (0,4,8)`（参考 u3-l2：无符号乘，整数位 2+2=4，小数位 4+4=8）。
- 目标格式 `result_fmt = (0,2,1)`，`round=NonSymPos_s`，`saturate=SatWarn_s`。

**任务**：

1. 手算 `1.6 × 2.4 = 3.84`，确认它在 `(0,4,8)` 下可精确表示（`3.84 × 256 = 983.04`，需确认量化值；写出 `mid_v` 的整数表示）。
2. 调用 `cl_fix_resize(mid_v, (0,4,8), (0,2,1), NonSymPos_s, SatWarn_s)`，按本讲 4.5 的两步法展开：
   - 求 `rounded_fmt = cl_fix_round_fmt((0,4,8), 1, NonSymPos_s)` → 预期 `(0,5,1)`。
   - 求 `rounded = cl_fix_round(...)`（跟踪 4.3 的偏移 + 截断）。
   - 求 `cl_fix_saturate(rounded, (0,5,1), (0,2,1), SatWarn_s)`（跟踪 4.4 的回绕 + 钳位 + 告警）。
3. 在源码中定位每一步用到的函数与行号（`cl_fix_mult` L1214–L1255、`cl_fix_resize` L1009–L1022、`cl_fix_round` L910–L976、`cl_fix_saturate` L978–L1007）。
4. 对照 [`bittrue/cosim/cl_fix_resize/cosim.py`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/cosim/cl_fix_resize/cosim.py) 或 [`bittrue/tests/python/`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/) 下的相关测试，验证你的手算与库行为一致。

**预期产出**：一张「输入值 → mid_v → rounded → 饱和输出」的四列跟踪表，每列标注所用函数与源码行号；并说明 `3.84` 经舍入到 `(0,2,1)`（范围 0~3.5）后是否触发饱和告警（提示：`NonSymPos` 舍入后 `3.84 → 4.0 > 3.5`，会触发 `SatWarn_s` 的告警并钳位到 `3.5`）。

## 6. 本讲小结

- **`convert`** 是纯格式搬运：二进制小数点对齐、`natural` 偏移护栏禁止减少小数位、减少整数位时直接丢高位（= `None_s` 回绕）；`rFmt.F > aFmt.F` 时靠结果向量初值全零实现低位补零。
- **三件套**：`get_half` 生成半个结果 LSB 的偏移基底；`get_unit_bit` 取结果 LSB（含隐式 MSB 扩展特例）；`resize_sensible` 在截断时用普通截断而非 `numeric_std.resize` 的保符号截断。
- **`cl_fix_round`** = 构造 `mid_fmt`（至少多 1 位小数）→ `convert` 无损搬入 → 按模式加偏移 → 截断低位；七种模式只差一个 `±1` 微调，统一机制是 `trunc(x+offset)`。
- **`cl_fix_saturate`** = 恒先 `convert` 回绕 → 按需 `assert` 告警 → 按需 `cl_fix_compare` + 钳位；四种模式 = 「告警」×「钳位」两开关。
- **`cl_fix_resize`** = 先 `cl_fix_round`（用 `cl_fix_round_fmt` 预测含进位的中间格式）、后 `cl_fix_saturate`；顺序不可交换，因为饱和要求小数位不变、且舍入进位必须先发生。
- 这四个函数是所有数学函数（`add/sub/mult/...`）共用的「精度收敛」底座：先全精度算、最后 `resize` 收敛。

## 7. 下一步学习建议

- **u5-l3（VHDL 数学函数）**：看 `cl_fix_add/sub/mult` 如何复用本讲的 `convert` + `cl_fix_resize` 三段式模板，以及 `cl_fix_mult` 为何要在包体内局部定义 `signed*unsigned` 重载。
- **u6-l1（流水线组件）**：本讲的三个纯函数被封装成 `en_cl_fix_round/saturate/resize` 三个可实例化组件，配合 `RegisterMode_t` 和 `cl_fix_recommended_pipelining`（L1041–L1109）插入寄存器——建议接着读组件如何把「组合逻辑纯函数」变成「带时钟的流水线」。
- **u7-l2（VUnit 测试台）**：看 [`tb/cl_fix_round_tb.vhd`](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) 如何读取 cosim 生成的黄金数据，逐拍比对 `cl_fix_round` 的输出，把本讲的位级理解变成可运行的验证闭环。
