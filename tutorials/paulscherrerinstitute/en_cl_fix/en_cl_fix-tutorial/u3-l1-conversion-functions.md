# 数值与字符串转换函数

## 1. 本讲目标

定点数在程序里以两种「形态」存在：一种是**位串（bit pattern）**，即一串 0/1，VHDL 里是 `std_logic_vector`；另一种是**实数值（real）**，即数学意义上的小数，Python/MATLAB 里就是 `double`。要把一个真实世界的小数装进定点格式，或者把定点数再读回成小数、整数、二进制串、十六进制串，就需要一组「转换函数」。

本讲聚焦 `en_cl_fix` 中完成这些转换的十个函数，学完后你应当能够：

- 说清 `cl_fix_from_real` / `cl_fix_to_real` 如何在「实数域」与「位串域」之间搬运数据，以及它**不受 `FixRound` 控制**的固定量化方式。
- 掌握 `cl_fix_from_int` / `cl_fix_to_int` 的整数装载与「丢弃小数位（隐式 `Trunc_s`）」行为。
- 理解 `cl_fix_from_bin` / `cl_fix_to_bin`、`cl_fix_from_hex` / `cl_fix_to_hex` 的字符串解析、长度校验与 `0b`/`0x` 前缀处理。
- 理解 `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int` 为何是「最高效的文件读写桥梁」，以及它**忽略小数点、按位回绕**的关键特性。
- 牢记一个跨语言事实：**这十个函数只有 `from_real` 与 `bits_as_int` 这一对在 Python/MATLAB 中存在，其余都是 VHDL 独有**——这直接决定了你能用哪种语言做哪种数据交换。

## 2. 前置知识

本讲假设你已掌握 u1-l2 的 `[S, I, F]` 三元组格式（总位宽 \(W = S + I + F\)，数值 \(V = N \cdot 2^{-F}\)）、u1-l4 的七种舍入模式 `FixRound`、以及 u1-l5 的四种饱和模式 `FixSaturate`（`None_s` / `Warn_s` / `Sat_s` / `SatWarn_s`）。这里再强调两个本讲反复用到的直觉：

- **位串域 vs 实数域**：VHDL 把定点数存成 `std_logic_vector`（一串比特），要变成小数必须显式「翻译」；Python/MATLAB 把定点数直接存成 `double`（已经是小数），所以很多「转换」在那里近乎恒等。这是本讲最重要的思维模型。
- **量化（quantization）**：把一个任意小数映射到间距为 \(2^{-F}\) 的离散网格上，必然产生不超过半个 LSB 的误差。`from_real` 干的就是这件事，而它用的是**固定的**舍入规则，与你传入的 `FixRound` 无关（`FixRound` 只在 `cl_fix_resize` 里起作用，详见 u3-l2）。

此外请记住三个语言里饱和模式的默认值约定（u1-l5 已详述）：`from_real` 的默认饱和模式是 `SatWarn_s`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 包，**唯一**同时包含全部十个转换函数的实现。函数声明在 L393–L486，函数体在 L1585–L1851。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | Python 实现。本讲只用到其中三个函数：`cl_fix_from_real`(L149)、`cl_fix_from_bits_as_int`(L173)、`cl_fix_get_bits_as_int`(L184)。 |
| [matlab/src/cl_fix_from_real.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m) | MATLAB 端唯一的转换函数 `cl_fix_from_real`，用浮点 `round` 实现，与 VHDL 的量化方式一致。 |
| [vhdl/tb/en_cl_fix_pkg_tb.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd) | VHDL 测试台。对 `from_real`(L107)、`to_real`(L131)、`from_bits_as_int`(L155)、`get_bits_as_int`(L163) 有直接用例；`from_int`/`to_int`/`from_bin`/`from_hex` 等当前**没有直接用例**。 |

> **跨语言函数可用性总览**（务必记住）：

| 函数 | VHDL | Python | MATLAB |
|------|:----:|:------:|:------:|
| `cl_fix_from_real` | ✓ | ✓ | ✓ |
| `cl_fix_to_real` | ✓ | ✗ | ✗ |
| `cl_fix_from_int` / `cl_fix_to_int` | ✓ | ✗ | ✗ |
| `cl_fix_from_bin` / `cl_fix_to_bin` | ✓ | ✗ | ✗ |
| `cl_fix_from_hex` / `cl_fix_to_hex` | ✓ | ✗ | ✗ |
| `cl_fix_from_bits_as_int` | ✓ | ✓ | ✗ |
| `cl_fix_get_bits_as_int` | ✓ | ✓ | ✗ |

也就是说：**只有 `from_real` 是三语言共有；`bits_as_int` 这一对是 VHDL+Python 共有；其余字符串/整数转换都是 VHDL 独有**。Python 与 MATLAB 之所以「缺」这些函数，是因为它们把定点数存成 `double`，本就在实数域里，不需要「位串↔实数」的显式翻译。

## 4. 核心概念与源码讲解

### 4.1 cl_fix_from_real 与 cl_fix_to_real：实数域 ⇄ 位串域

#### 4.1.1 概念说明

`cl_fix_from_real` 把一个数学小数装进某个 `[S,I,F]` 格式，是「实数域 → 定点」的入口。它做两件事：

1. **量化**：把任意小数对齐到网格间距 \(2^{-F}\) 上。它用一套**固定**的舍入规则，与 `FixRound` 参数**无关**（VHDL/Python 函数签名里根本没有 `round` 形参）。
2. **饱和**：按 `FixSaturate` 参数（默认 `SatWarn_s`）处理越界。

`cl_fix_to_real` 是反方向「定点 → 实数域」的出口，它把位串按二进制补码重新解释成 `real`。它**只在 VHDL 中存在**——因为只有 VHDL 把数存成位串；Python/MATLAB 存的本来就是 `double`，不存在这一步。

#### 4.1.2 核心流程

`from_real` 的统一流程（三语言一致的部分）：

```
输入 a (real) + rFmt + saturate(默认 SatWarn_s)
   │
   ├─ [量化] 把 a 折算到 2^(-F) 网格上：n = 量化函数(a * 2^F)
   │         · VHDL/MATLAB: round(...)   半值远离零 = SymInf
   │         · Python:      floor(x+0.5) 半值向上   = NonSymPos
   │
   ├─ [饱和] 越界时按 saturate 决定 clip / wrap / warn
   │
   └─ 输出: VHDL 返回 std_logic_vector；Python/MATLAB 返回 double
```

量化的数学表达（Python 形式）：

\[
n = \left\lfloor a \cdot 2^{F} + \tfrac{1}{2} \right\rfloor,\qquad V_{\text{out}} = n \cdot 2^{-F}
\]

`to_real`（仅 VHDL）则是对宽度为 \(W\) 的位串做加权求和，有符号时单独处理最高符号位：

\[
V = \begin{cases}
\displaystyle\sum_{k} b_k \cdot 2^{-F} & \text{无符号} \\[6pt]
-2^{I} \cdot (\text{符号位}) + \displaystyle\sum_{k} b_k \cdot 2^{-F} & \text{有符号}
\end{cases}
\]

#### 4.1.3 源码精读

**VHDL `cl_fix_from_real`** 先无条件夹紧到 `[min_real, max_real]`，再用 `round` 量化，最后按 30 位一段「切块」搬进 `std_logic_vector`（切块是为了绕开 `integer` 仅 32 位的限制，支持大位宽）：

- [en_cl_fix_pkg.vhd:1660-1682](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1660-L1682) — 注意 L1662–L1668 的 `if/elsif` **无条件夹紧**（不引用 `saturate` 形参），L1671 的 `round(...)` 是半值远离零。
- 量化公式注释见 Doxygen 说明 [en_cl_fix_pkg.vhd:414](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L414)：`Rounds symmetrically away from zero (implicit usage of SymInf_s)`。

**VHDL `cl_fix_to_real`** 用 `Correction_v` 记住符号位权重、清零符号位后做无符号加权求和，同样按 30 位切块以支持大位宽：

- [en_cl_fix_pkg.vhd:1697-1721](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1697-L1721) — L1702–L1705 处理符号位，L1711–L1715 切块累加。

**Python `cl_fix_from_real`** 用 `np.floor(...+0.5)` 量化、`np.where` 饱和，并对数组向量化：

- [en_cl_fix_pkg.py:149-171](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L149-L171) — L155–L161 是 `Warn` 告警分支，L164 是 half-up 量化，L167–L169 是 `Sat` 夹紧。

**MATLAB `cl_fix_from_real`** 与 VHDL 同源（都用 `round`）：

- [cl_fix_from_real.m:29-63](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_from_real.m#L29-L63) — L30 注释 `same as VHDL`，L46–L60 按 `saturate` 分支处理 wrap/clip。

> **⚠️ 跨语言量化差异**：从源码可直接读出，VHDL/MATLAB 用 `round()`（半值远离零，对应 `SymInf_s`），Python 用 `floor(x+0.5)`（半值朝正无穷，对应 `NonSymPos_s`）。两者**仅在负数恰好落在半值网格点时相差 1 LSB**（例如把 −2.5 量化到整数：Python 得 −2，VHDL/MATLAB 得 −3）。当前 testbench 的 `from_real` 用例（见 4.1.4）均不是负数半值，故未覆盖该差异；做严格位真比对时应避开负数半值或显式记录这一点。

#### 4.1.4 代码实践

**实践目标**：用 testbench 现有用例验证 `from_real`/`to_real` 的量化与往返。

1. 打开 [en_cl_fix_pkg_tb.vhd:107-129](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L107-L129)，定位 `from_real` 段。
2. 手算 `cl_fix_from_real(-3.24, (true,3,1))`：`-3.24 × 2¹ = -6.48`，`round(-6.48) = -6`（−6.48 离 −6 更近），`-6 × 2⁻¹ = -3.0`，对应 5 位补码 `-6 → 11010`。
3. 对照 L121–L123，确认期望位串正是 `"11010"`。
4. 再看 `to_real` 段 [en_cl_fix_pkg_tb.vhd:145-147](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L145-L147)：`to_real(from_real(-3.24,...))` 期望 `-3.0`，证明量化误差在 `to_real` 出口处表现为 −0.24 的损失。

**预期结果**：手算的 `11010` / `-3.0` 与 testbench 期望值一致。

#### 4.1.5 小练习与答案

1. **Q**：`cl_fix_from_real(2.5, (true,2,0))` 在 VHDL 中得到什么位串？
   **A**：`2.5 × 2⁰ = 2.5`，`round(2.5) = 3`（远离零），3 在 3 位有符号补码中是 `011`。

2. **Q**：为什么 Python 没有 `cl_fix_to_real`？
   **A**：Python 把定点数存成 `double`，本身就在实数域，输出即实数，无需「位串→实数」的翻译；只有把数存成位串的 VHDL 才需要它。

3. **Q**：把 −2.5 量化到 `(true,2,0)`，Python 与 VHDL 结果是否相同？
   **A**：不同。Python `floor(-2.5+0.5)=floor(-2.0)=-2`；VHDL `round(-2.5)=-3`（远离零）。相差 1 LSB，这正是 4.1.3 末尾指出的差异。

---

### 4.2 cl_fix_from_int 与 cl_fix_to_int：整数 ⇄ 定点

#### 4.2.1 概念说明

这两个函数只在 VHDL 中存在。`from_int` 把一个 VHDL `integer` 装进定点格式的**整数部分**（小数位恒为 0），并按 `saturate` 处理越界。`to_int` 反向把定点数读成 `integer`，**直接丢弃小数位**（文档明确写「implicit usage of `Trunc_s`」），即截断而非四舍五入。

#### 4.2.2 核心流程

`from_int` 的有符号情形把整数夹紧到 \([-2^{I},\ 2^{I}-1]\)，无符号情形夹紧到 \([0,\ 2^{I}-1]\)，然后写入位串的整数段、小数段填 0：

```
from_int(a, rFmt, saturate):
  if Signed:  范围 = [-2^I, 2^I - 1]
  else:       范围 = [0,    2^I - 1]
  按 saturate 决定是否 Warn / 夹紧
  写入 result[high downto FracBits] = 补码(a)   # 小数位保持 0
```

`to_int` 则取出整数段、忽略小数位；当 `FracBits < 0` 时（数值是粗粒度的 2 的幂的倍数），左移 `2^(-FracBits)` 还原：

```
to_int(a, aFmt):
  if IntBits <= 0:  return 0          # 没有整数位
  取 a[high downto FracBits] 解释为 signed/unsigned
  if FracBits < 0:  return 上述值 * 2^(-FracBits)
```

#### 4.2.3 源码精读

**`cl_fix_from_int`** 体：

- [en_cl_fix_pkg.vhd:1591-1614](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1591-L1614) — L1595–L1598 是 `Warn`/`SatWarn` 告警 assert，L1599–L1601 是夹紧，L1602–L1603 用 `to_signed` 写入整数段、小数段保持初值 `'0'`。

**`cl_fix_to_int`** 体：

- [en_cl_fix_pkg.vhd:1623-1646](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1623-L1646) — L1628–L1632 处理 `FracBits` 正/负两种情况；注意 L1625 的 `-- TODO: range check on a!`，说明当前**不校验输入位串宽度**。

> 与 `from_bits_as_int`（4.5）的关键区别：`from_int` **会饱和**（夹紧到整数范围），而 `from_bits_as_int` **按位回绕**（不饱和）。这是本讲容易混淆的两个入口。

#### 4.2.4 代码实践

**实践目标**：用源码推导两个边界行为，加深「整数装载会饱和」的印象（本函数无现成 testbench 用例，属源码阅读型实践）。

1. 读 [en_cl_fix_pkg.vhd:1599-1603](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1599-L1603)。
2. 推导 `cl_fix_from_int(20, (false,4,0), Sat_s)`：无符号范围 `[0, 15]`，20 被夹紧到 15，位串 `"1111"`。
3. 推导 `cl_fix_to_int(x"6.4", (true,3,1))`（即位串表示 3.5）：取整数段 = 3，丢弃小数位 .5，返回 `3`（截断，非四舍五入）。

**预期结果**：`from_int(20,...,Sat_s) → "1111"`；`to_int(3.5 的位串) → 3`。**待本地验证**（无现成用例，建议你按 u2-l2 的 `CheckStdlv`/`CheckInt` 格式自行加一条断言跑仿真确认）。

#### 4.2.5 小练习与答案

1. **Q**：`cl_fix_from_int(-1, (false,3,0), SatWarn_s)` 会发生什么？
   **A**：无符号格式不接受负数，−1 越下界 → 触发 `Saturation Warning`（assert severity warning），且因 `SatWarn_s` 夹紧到 0，结果 `"000"`。

2. **Q**：`to_int` 对小数部分用的是哪种舍入？
   **A**：`Trunc_s`（截断）。Doxygen 注释 [en_cl_fix_pkg.vhd:403](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L403) 明确写 `implicit usage of Trunc_s`。

3. **Q**：为什么 `FracBits` 为负时 `to_int` 要乘 `2^(-FracBits)`？
   **A**：负 `FracBits` 表示数值是 \(2^{|F|}\) 的倍数（粗粒度），位串代表的整数需要左移还原成真实整数倍数。

---

### 4.3 cl_fix_from_bin 与 cl_fix_to_bin：二进制串 ⇄ 定点

#### 4.3.1 概念说明

这两个函数只在 VHDL 中存在。`from_bin` 把一个二进制字符串（如 `"0b1101"` 或 `"11_01"`）解析成 `std_logic_vector`；`to_bin` 反向把位串输出成可读的二进制字符串。它们是调试时「以人类可读形式查看位串」的便利工具。

#### 4.3.2 核心流程

`from_bin` 逐字符扫描，支持三种特殊处理：

```
from_bin(a, result_fmt):
  遇 '0'/'1': 写入对应位
  遇 'b'/'B': 仅当它是 "0b" 前缀的第二个字符时跳过（否则报错）
  道遇 '_':  跳过（分隔符，提高可读性）
  遇其它:    report ... severity error
  扫完后 assert 有效位数 == cl_fix_width(result_fmt)
```

`to_bin` 直接委托给内部助手 `toString`，把每位 `std_logic` 转成 `'0'`/`'1'` 字符。

#### 4.3.3 源码精读

**`cl_fix_from_bin`** 体：

- [en_cl_fix_pkg.vhd:1731-1755](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1731-L1755) — L1742–L1745 处理 `"0b"` 前缀，L1746 跳过下划线，L1748–L1749 非法字符报 `severity error`，L1752–L1754 校验长度。

**`cl_fix_to_bin`** 体（仅一行委托）：

- [en_cl_fix_pkg.vhd:1763-1765](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1763-L1765) — 直接 `return toString(a)`。

**助手 `toString`**：

- [en_cl_fix_pkg.vhd:1271-1280](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1271-L1280) — 用查表常量 `StdLogicCharacter_c` 把 `std_logic` 位置映射成字符。

#### 4.3.4 代码实践

**实践目标**：阅读型实践——确认长度校验逻辑，并预测一个非法输入的报错。

1. 读 [en_cl_fix_pkg.vhd:1752-1754](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1752-L1754) 的 assert。
2. 推导 `cl_fix_from_bin("1011", (true,2,1))`：`width(true,2,1) = 1+2+1 = 4`，字符串有效位 4，匹配，结果位串 `"1011"`。
3. 推导 `cl_fix_from_bin("111", (true,2,1))`：有效位 3 ≠ 4 → `assert` 失败，输出 `cl_fix_from_bin : The binary string doesn't have the correct length!`，`severity error`。

**预期结果**：合法输入得到 `"1011"`；长度不匹配触发 `severity error` 报告（`###ERROR###` 风格，参见 u2-l2）。**待本地验证**。

#### 4.3.5 小练习与答案

1. **Q**：字符串 `"0b1010"` 解析后有效位数是多少？
   **A**：4。`0b` 前缀被跳过（见 L1742–L1745），剩下 `1010` 四位。

2. **Q**：`"10_10"` 是否合法？
   **A**：合法，下划线被跳过（L1746），有效位仍为 4。

3. **Q**：`from_bin` 与 `from_bits_as_int` 都能把数据装进位串，主要区别是什么？
   **A**：`from_bin` 从**人类可读字符串**装载并**校验长度**；`from_bits_as_int` 从**整数**装载、**不校验也不饱和**（按位回绕），面向高效机器读写而非可读性。

---

### 4.4 cl_fix_from_hex 与 cl_fix_to_hex：十六进制串 ⇄ 定点

#### 4.4.1 概念说明

这对函数同样只在 VHDL 中存在，逻辑与 `from_bin`/`to_bin` 平行，差别在于每个十六进制字符代表 4 位。`from_hex` 支持大小写 `a`–`f`/`A`–`F`、`0x` 前缀与下划线，并对「位宽不是 4 的倍数」时的**空闲高位**做额外校验。

#### 4.4.2 核心流程

```
from_hex(a, result_fmt):
  每个十六进制字符 → 4 位二进制
  遇 'x'/'X': 仅当 "0x" 前缀第二字符时跳过
  遇 '_':     跳过
  assert: 4*(字符数) >= Width 且 4*(字符数-1) < Width   # 长度恰好覆盖
  if Width 不是 4 的倍数:
      assert 未用到的高位全为 0           # 否则报 "unused bits ... not all zero"
  截取低 Width 位返回
```

`to_hex` 委托给助手 `toHexString`，每 4 位转一个十六进制字符。

#### 4.4.3 源码精读

**`cl_fix_from_hex`** 体（注意它显式展开了 0–9、a–f、A–F 全部分支）：

- [en_cl_fix_pkg.vhd:1776-1815](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1776-L1815) — L1797–L1800 处理 `"0x"` 前缀，L1807–L1809 是长度断言，L1810–L1814 是「空闲高位必须为 0」断言。

**`cl_fix_to_hex`** 体（一行委托）：

- [en_cl_fix_pkg.vhd:1823-1825](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1823-L1825) — `return toHexString(a)`。

**助手 `toHexString`**：

- [en_cl_fix_pkg.vhd:1284-1295](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1284-L1295) — 先把位串左侧补 0 到 4 的倍数，再每 4 位用 `HexCharacter_c` 查表输出。

#### 4.4.4 代码实践

**实践目标**：理解「位宽非 4 的倍数」时的空闲高位校验。

1. 读 [en_cl_fix_pkg.vhd:1807-1814](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1807-L1814)。
2. 推导 `cl_fix_from_hex("0xA", (true,2,1))`：`width = 1+2+1 = 4`，`0xA` 解析为 `"1010"`，恰好 4 位，无空闲高位，结果 `"1010"`。
3. 推导 `cl_fix_from_hex("1F", (false,3,0))`：`width = 3`，`1F` = `"00011111"` 共 8 位，但只需低 3 位 `"111"`，高 5 位 `00011` 含 1 → 触发 `unused bits ... not all zero`，`severity error`。

**预期结果**：`"0xA"` 合法得 `"1010"`；`"1F"` 因空闲高位非零报错。**待本地验证**。

#### 4.4.5 小练习与答案

1. **Q**：对 `(true,3,3)`（宽度 7），最少需要几个十六进制字符？
   **A**：2 个（覆盖 8 位，多余 1 位必须为 0）。由 L1807 的 `4*(len-1) < Width <= 4*len` 决定。

2. **Q**：`from_hex` 接受大写 `A` 吗？
   **A**：接受。L1791 显式列出 `'a' | 'A'` 同样映射到 `"1010"`。

3. **Q**：为什么 `to_hex` 对非 4 的倍数宽度要左侧补 0？
   **A**：十六进制每字符固定 4 位，不足 4 位的高位必须补 0 才能凑成完整字符（见 L1288 的 `value_i := (others => '0')`）。

---

### 4.5 cl_fix_from_bits_as_int 与 cl_fix_get_bits_as_int：位整数 ⇄ 定点（高效读写桥梁）

#### 4.5.1 概念说明

这是本讲最实用的一对，也是**唯一在 Python 和 VHDL 中都存在**的非 `from_real` 转换。它们把定点数的**原始位串**看成一个普通整数（忽略小数点位置），用于在文件里高效存取定点数据：写文件时把位串压成一个整数，读文件时再还原。

关键特性（与 `from_int` 截然不同）：

- **忽略小数点**：`get_bits_as_int` 直接读原始位，`FracBits` 不参与计算——testbench 注释明写 `binary point position is not important`。
- **按位回绕，不饱和**：`from_bits_as_int` 把整数按补码/无符号塞进固定位宽，超出部分**直接丢弃（回绕）**，不夹紧、不告警。

#### 4.5.2 核心流程

```
get_bits_as_int(a, aFmt):
  把位串 a 按 aFmt.Signed 解释为有符号/无符号整数，返回 integer
  # FracBits 完全不影响结果

from_bits_as_int(a, aFmt):
  if Signed:  to_signed(a, width(aFmt))     # 回绕到 [-2^(W-1), 2^(W-1)-1]
  else:       to_unsigned(a, width(aFmt))   # 回绕到 [0, 2^W - 1]
  # 不饱和、不告警
```

数学上，对一个 \(W\) 位无符号回绕：

\[
a_{\text{wrap}} = a \bmod 2^{W}
\]

#### 4.5.3 源码精读

**VHDL `cl_fix_from_bits_as_int`** 体（仅按符号选择 `to_signed`/`to_unsigned`）：

- [en_cl_fix_pkg.vhd:1832-1838](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1832-L1838) — 注意没有任何 `saturate` 形参，完全靠 `to_signed/unsigned` 的固定位宽回绕。

**VHDL `cl_fix_get_bits_as_int`** 体：

- [en_cl_fix_pkg.vhd:1845-1851](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1845-L1851) — 仅按符号选择 `signed`/`unsigned` 解释。

**VHDL testbench 用例**（验证「回绕」与「忽略小数点」）：

- [en_cl_fix_pkg_tb.vhd:155-168](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L155-L168) — L161 `from_bits_as_int(17, (false,4,0)) → "0001"`（17 mod 16 = 1，回绕！），L160/L168 注释 `binary point position is not important`。

**Python `cl_fix_from_bits_as_int`** 体（narrow 路径除以 \(2^F\) 还原成实数）：

- [en_cl_fix_pkg.py:173-182](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L173-L182) — L179 `a/2**FracBits` 把位整数还原成 double；L175/L180 用 `cl_fix_in_range` 做范围校验，越界**抛 `ValueError`**（注意：Python 这里是抛异常，而非 VHDL 的静默回绕）。

**Python `cl_fix_get_bits_as_int`** 体：

- [en_cl_fix_pkg.py:184-188](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L184-L188) — narrow 路径 `np.round(a*2**FracBits)` 取位整数；wide 路径直接返回内部大整数 `a.data`（大位宽详见 Unit 6）。

> **跨语言行为差异**：VHDL 的 `from_bits_as_int` 对越界**静默回绕**（如 17→1），Python 的 `from_bits_as_int` 对越界**抛 `ValueError`**。做位真数据交换时，务必保证写入的位整数本就在格式范围内。

#### 4.5.4 代码实践

**实践目标**：在 Python 中亲手验证「忽略小数点」与「回绕/校验」两个特性。

1. 在 `python/unittest` 目录启动 Python，导入包：

   ```python
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *
   import numpy as np
   ```

2. 验证忽略小数点：`cl_fix_get_bits_as_int(np.array(-3.0), FixFormat(True,3,0))` 与 `cl_fix_get_bits_as_int(np.array(-3.0), FixFormat(True,1,2))` 应返回相同的整数 `-3`（小数点位置不同，位整数相同）。
3. 验证范围校验：`cl_fix_from_bits_as_int(20, FixFormat(False,4,0))` 应抛 `ValueError`（无符号 4 位最大 15，20 越界）——对照 VHDL 的 `17 → "0001"` 静默回绕，体会两语言差异。

**预期结果**：步骤 2 两次都得 `-3`；步骤 3 抛 `cl_fix_from_bits_as_int: Value not in number format range`。

#### 4.5.5 小练习与答案

1. **Q**：`cl_fix_get_bits_as_int("1101", (true,1,2))` 等于多少？为什么？
   **A**：等于 `-3`。`"1101"` 是 4 位补码 = −3；`FracBits=2` 不影响位整数（注释 `binary point position is not important`，见 testbench L168）。

2. **Q**：VHDL 中 `cl_fix_from_bits_as_int(17, (false,4,0))` 为何得 `"0001"`？
   **A**：无符号 4 位回绕，\(17 \bmod 16 = 1\)，不饱和、不告警（L1836 的 `to_unsigned` 固定位宽）。

3. **Q**：用这对函数做文件读写比 `from_real`/`to_real` 高效在哪里？
   **A**：位整数是普通整数，可用 `std.textio` 的整数读写（或 Python 整数）直接 I/O，免去浮点格式化与解析开销；且它天然携带精确位信息，是跨语言位真数据交换的首选载体（Unit 5 详述）。

## 5. 综合实践

把本讲五个模块串起来，完成规格要求的「Python 位整数往返」任务，并与 VHDL 的 `to_real` 出口对照。

**任务**：用 Python 把实数 3.14 装入 `(true,3,4)`，取出位整数，再还原，验证往返一致性。

**操作步骤**（在 `python/unittest` 目录运行）：

```python
import sys; sys.path.append("../src")
from en_cl_fix_pkg import *
import numpy as np

fmt = FixFormat(True, 3, 4)          # 宽度 = 1+3+4 = 8 位，LSB = 2^-4 = 0.0625

# 步骤1: from_real 量化装入
v = cl_fix_from_real(np.array(3.14), fmt)
print("from_real      ->", float(v))  # 预期 3.125

# 步骤2: 取出位整数（忽略小数点的原始位）
bits = cl_fix_get_bits_as_int(v, fmt)
print("get_bits_as_int->", int(bits)) # 预期 50（= 0b00110010）

# 步骤3: 从位整数还原
v2 = cl_fix_from_bits_as_int(int(bits), fmt)
print("from_bits_as_int->", float(v2))# 预期 3.125

# 步骤4: 验证往返一致性
print("round-trip OK ->", float(v) == float(v2))  # 预期 True
```

**手算验证**：

- \(3.14 \times 2^{4} = 50.24\)，half-up 量化 \(n = \lfloor 50.24 + 0.5 \rfloor = 50\)，\(V = 50 \times 2^{-4} = 3.125\)。
- 位整数 50 = `0b00110010`（8 位）。
- 还原：\(50 / 2^{4} = 3.125\)，与量化结果一致。

**关于「最后用 `cl_fix_to_real` 验证」**：**Python 没有 `cl_fix_to_real`**——因为 Python 的定点数本就存成 `double`，`v2` 已经是实数 3.125，无需再翻译。等价的「位串 → 实数」验证只在 VHDL 中显式存在，参见 testbench [en_cl_fix_pkg_tb.vhd:145-147](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L145-L147)：`cl_fix_to_real(cl_fix_from_real(-3.24, (true,3,1)), (true,3,1))` 期望 `-3.0`，正是把位串经 `to_real` 读回实数后的值。

**需要观察的现象**：

1. 量化误差：原始 3.14 → 装入后 3.125，损失 0.015（小于半个 LSB 0.03125，符合量化理论）。
2. 往返无损：`from_real → get_bits_as_int → from_bits_as_int` 完全可逆，最终值 3.125 与中间位整数 50 严格对应。
3. 若把步骤 3 的 `int(bits)` 改成越界值（如 300），Python 会抛 `ValueError`，而 VHDL 会静默回绕——这是 4.5.3 指出的跨语言差异。

**预期结果**：输出 `3.125 / 50 / 3.125 / True`。**待本地验证**（受运行环境影响，若 `../src` 路径或 numpy 不可用，可改为在 `python/src/en_cl_fix_pkg` 目录下直接 `from en_cl_fix_pkg import *`）。

## 6. 本讲小结

- 转换函数在「实数域 ⇄ 位串域」之间搬运数据；**VHDL 把数存成位串**故需要全套十个函数，**Python/MATLAB 把数存成 double** 故只需 `from_real`（外加 Python 的 `bits_as_int` 一对）。
- `cl_fix_from_real` 的量化是**固定**的（VHDL/MATLAB 用 `round` 即 `SymInf`，Python 用 `floor(x+0.5)` 即 half-up），**不受 `FixRound` 控制**；饱和才受 `FixSaturate` 控制，默认 `SatWarn_s`。
- `cl_fix_to_real`（仅 VHDL）用「符号位单独加权 + 切块累加」支持任意位宽，是位串回到实数的唯一出口。
- `cl_fix_from_int` **会饱和**夹紧到整数范围；`cl_fix_to_int` **截断小数位**（隐式 `Trunc_s`）。
- `cl_fix_from_bin`/`from_hex` 是面向可读性的字符串解析，带长度与前缀校验；`to_bin`/`to_hex` 委托给 `toString`/`toHexString`。
- `cl_fix_from_bits_as_int`/`get_bits_as_int` 是最高效的文件读写桥梁：**忽略小数点**、**按位回绕不饱和**（VHDL）或**越界抛异常**（Python），是跨语言位真数据交换的首选载体。

## 7. 下一步学习建议

- 本讲的 `from_real` 量化是「一次性、固定舍入」的特例；下一讲 **u3-l2《cl_fix_resize 的舍入机制》** 将深入通用 resize 管线，讲解 `DropFracBits`、`NeedRound`、`HalfMinusDelta` 如何把七种 `FixRound` 模式统一实现成「加偏移再截断」。
- 如果你对文件读写感兴趣，可跳到 **Unit 5《文件 IO 与位真数据交换》**，看 `cl_fix_read_int`/`write_int` 等如何以本讲的 `bits_as_int` 为底层载体，配合 `std.textio` 完成跨语言数据交换。
- 想理解大位宽（>53 位）下这些转换如何工作，可预习 **Unit 6**：Python 的 `wide_fxp` 正是 `from_real`/`from_bits_as_int` 在 wide 路径上的实现（本讲已多次出现 `cl_fix_is_wide` 分支与 `a.data` 入口）。
