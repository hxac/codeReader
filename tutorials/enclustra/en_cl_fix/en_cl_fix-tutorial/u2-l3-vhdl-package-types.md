# VHDL 包镜像：en_cl_fix_pkg 的类型与接口

## 1. 本讲目标

本讲是单元 2 的第三篇，把目光从 Python 参考模型转到 **VHDL 实现**。

前两篇你已经认识了 Python 侧的三大核心类型（`FixFormat`/`FixRound`/`FixSaturate`）和 `cl_fix_*` 函数地图。本讲只读一个文件——可综合包 `hdl/en_cl_fix_pkg.vhd`——专门精读它的 **package 头部**：类型定义与公共函数签名。

读完本讲，你应当能够：

- 读懂 VHDL 的 `FixFormat_t` record，并理解它与 Python `FixFormat` 的关键差异（**类型层不强制 `I+F>=0`**）。
- 解释 `FixRound_t` / `FixSaturate_t` / `RegisterMode_t` 三个枚举与 Python 的对应关系。
- 看懂 `NullFixFormat_c` 这个「哨兵常数」的作用。
- 按四大类（Format Functions、Rounding and Saturation、Math Functions、String/Type Conversions）读懂所有 `cl_fix_*` 函数签名，包括默认参数的含义。
- 在 VHDL 里定义格式常数，调用 `cl_fix_width` / `cl_fix_add_fmt` 并用 `report` 打印结果。

本讲**只讲签名与类型**，不展开算法实现。舍入/饱和/resize 的内部算法留到单元 4，结果格式（mid_fmt）的推导留到单元 3，可综合 RTL 实体（`en_cl_fix_round.vhd` 等）留到单元 7。

## 2. 前置知识

本讲假设你已经掌握（来自 u1-l2 与 u2-l1）：

- **定点格式 `[S, I, F]`**：S 是符号位个数（0 或 1），I 是整数位，F 是小数位，总位宽 `S+I+F`。
- **三大类型**：`FixFormat` 描述数值怎么放，`FixRound` 描述小数位变少时怎么取舍（7 种），`FixSaturate` 描述整数位/符号位变少时怎么处理（4 种）。
- **Python 的 `r_fmt` 缺省约定**：不指定结果格式就给「全精度中间格式」。

本讲还需要一点点 VHDL 常识（不熟悉也没关系，下面会解释）：

- **package（包）**：VHDL 里用来集中声明类型、常数和函数的「头文件」。`package ... is ... end package;` 是声明（头部），`package body ... is ... end;` 是实现（函数体）。本讲主要看头部。
- **record（记录）**：VHDL 的结构体类型，类似 C 的 struct，把几个字段打包。
- **枚举类型 `type T is (A, B, C);`**：一组命名常量，和 Python 的 `Enum` 对应。
- **函数默认参数 `:= 值`**：调用时可省略，等价于 Python 函数签名里的 `= 值`。

> 关键直觉：en_clustra 把同一套定点运算用 **三种语言** 各写一遍，且函数名一一对应（`cl_fix_width` 在 Python、VHDL、MATLAB 里都叫这个名字）。本讲读的 VHDL 包，就是 Python `en_cl_fix.py` 的**镜像**。学会读这份头部，你就掌握了三语言 API 的「VHDL 那一面」。

## 3. 本讲源码地图

本讲只涉及一个源文件（但会顺带提及它依赖的私有包和一个现成 testbench）：

| 文件 | 作用 | 本讲用到哪里 |
|------|------|--------------|
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | 可综合定点库的 VHDL 包，类型 + 函数签名 + 函数体 | 全讲主线 |
| hdl/en_cl_fix_private_pkg.vhd | 私有辅助包（`maximum`/`minimum`/字符串解析） | 仅说明依赖来源 |
| [tb/en_cl_fix_pkg_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/en_cl_fix_pkg_tb.vhd) | 现成的 VUnit 测试，已经调用 `cl_fix_width` / `cl_fix_add_fmt` | 综合实践的「标准答案」 |

整个 `en_cl_fix_pkg.vhd` 的布局很清晰，记住这四大块即可：

```
第 34–266 行   package 头部（本讲全部内容）
   ├── Types                    (39–73)
   ├── Format Functions         (76–101)   纯格式/位宽计算，无数据
   ├── String Conversions       (103–118)
   ├── Type Conversions         (120–129)  real/integer ↔ slv
   ├── Rounding and Saturation  (131–185)  round/saturate/resize/in_range
   └── Math Functions           (187–264)  add/sub/mult/...
第 271–1326 行  package body（函数体，本讲只瞄一眼）
```

---

## 4. 核心概念与源码讲解

### 4.1 package 头部 Types：FixFormat_t 与三大枚举

#### 4.1.1 概念说明

VHDL 包的头部第一件事就是声明「类型」。en_cl_fix 把定点格式本身设计成一个 **record**（结构体），把舍入/饱和/寄存器模式设计成 **枚举**。

这套类型是整个库的「公共语言」：后面所有函数的参数和返回值都用它们。所以读懂这 40 行，等于拿到了阅读全部签名的钥匙。

它与 Python 侧的对应关系是：

| VHDL | Python | 备注 |
|------|--------|------|
| `FixFormat_t`（record） | `FixFormat(S,I,F)`（class） | 见下方重要差异 |
| `NullFixFormat_c`（常数） | （无直接对应，是 VHDL 特有哨兵） | 表示「未指定结果格式」 |
| `FixFormatArray_t` | （内部用 list） | VHDL 的格式数组类型 |
| `FixRound_t` | `FixRound` | 7 个枚举值，名称几乎逐字相同 |
| `FixSaturate_t` | `FixSaturate` | 4 个枚举值 |
| `RegisterMode_t` | （Python 无） | VHDL 专属：控制 RTL 寄存器插入 |

#### 4.1.2 核心流程

类型声明本身没有「流程」，但有一个**关键差异**必须记住：

- **Python** 在 `FixFormat.__init__` 里用 `assert` 强制 `S ∈ {0,1}` 且 `I+F >= 0`，非法格式当场报错。
- **VHDL** 的 `FixFormat_t` 只把 `S` 约束为 `range 0 to 1`，而 `I`、`F` 都是**不受约束的 `integer`**。换句话说，**VHDL 类型层允许 `I+F < 0` 这种「负位宽」格式**。库靠每个函数内部的断言（`assert ... severity Failure`）来兜底，而不是靠类型本身。

这一点在阅读签名时要心里有数：VHDL 的「类型安全网」比 Python 薄，更多责任落在了函数体里。

#### 4.1.3 源码精读

先看库依赖。包用到了 IEEE 标准库和一个**私有辅助包**：

[en_cl_fix_pkg.vhd:23-29](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L23-L29) — 引入 `ieee.std_logic_1164` / `numeric_std` / `math_real`，并 `use work.en_cl_fix_private_pkg.all`。私有包提供 `maximum`/`minimum`/`toLower`/字符串解析等工具（这些函数不是本库的定点逻辑，只是 VHDL 辅助，故藏在 `_private_pkg` 里）。

核心 record：

[en_cl_fix_pkg.vhd:39-43](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L39-L43) — `FixFormat_t` 有三个字段：`S : natural range 0 to 1`（符号位只能是 0 或 1），`I : integer`（整数位，可正可负），`F : integer`（小数位，可正可负）。注意只有 `S` 有范围约束，`I/F` 完全自由。

紧跟一个**哨兵常数**：

[en_cl_fix_pkg.vhd:45](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L45) — `NullFixFormat_c : FixFormat_t := (0, 0, -1);`。这是一个**特殊标记值**（它的位宽 `0+0+(-1) = -1`，本身是「非法」的），专门用来在算术函数里表示「调用者没有指定结果格式，请用全精度中间格式」。后面 4.4 会看到它怎么用。

格式数组类型：

[en_cl_fix_pkg.vhd:47](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L47) — `FixFormatArray_t is array(natural range <>) of FixFormat_t;`，不定长数组，用于需要传一组格式的场合（如 `cl_fix_shift_fmt` 的区间版重载内部）。

舍入枚举（7 个值，逐字对应 Python）：

[en_cl_fix_pkg.vhd:49-58](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L49-L58) — `FixRound_t` 列出 `Trunc_s`（截断）、`NonSymPos_s`（半向上）、`NonSymNeg_s`（半向下）、`SymInf_s`（向 ±∞ 对称）、`SymZero_s`（向 0 对称）、`ConvEven_s`（向偶数收敛）、`ConvOdd_s`（向奇数收敛）。每个值后面的注释就是它的语义，和 Python `FixRound` 完全一致。

饱和枚举（4 个值）：

[en_cl_fix_pkg.vhd:60-66](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L60-L66) — `FixSaturate_t` 列出 `None_s`（不饱和、不告警=直接回绕）、`Warn_s`（不饱和、只告警）、`Sat_s`（只饱和、不告警）、`SatWarn_s`（饱和且告警）。这正好是「是否饱和」「是否告警」两个布尔维度的 4 种组合（去掉两个无意义的）。

VHDL 专属的寄存器模式枚举：

[en_cl_fix_pkg.vhd:68-73](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L68-L73) — `RegisterMode_t` 列出 `Auto_s`（插入推荐寄存器）、`Yes_s`（全插，便于固定延迟）、`No_s`（全不插，慎用）。这是**可综合 RTL 组件**（单元 7）才用得到的参数，Python 模型里没有对应物——因为软件模型不需要考虑时序/寄存器。

#### 4.1.4 代码实践

**目标**：亲手构造几个 `FixFormat_t` 常数，验证位宽与 Python 一致。

**操作步骤**（阅读型实践）：在脑中（或纸 上）写下下列格式，先用 `S+I+F` 算位宽，再对照后面 4.2 的 `cl_fix_width`：

```vhdl
-- 示例代码：仅演示常数声明，本讲不要求运行
constant F1_c : FixFormat_t := (1, 4, 8);   -- 有符号，预期位宽 13
constant F2_c : FixFormat_t := (0, 2, 8);   -- 无符号，预期位宽 10
constant F3_c : FixFormat_t := (1, -2, 3);  -- 合法：I+F = 1 >= 0，位宽 2
```

**需要观察的现象**：注意 `F3_c` 的 `I = -2` 是合法的 record（类型层不拦），但它的位宽只有 2，是个「小数点跑到整数位左边」的奇怪格式——这正是 u1-l2 讲过的「负整数位」情形。

**预期结果**：位宽分别为 13、10、2。

#### 4.1.5 小练习与答案

**练习 1**：`NullFixFormat_c = (0,0,-1)` 的位宽是多少？为什么它是「非法」的，却还能当常数用？

> **答案**：位宽 `0+0+(-1) = -1`，是负数，所以「非法」。它能当常数用，是因为 VHDL 的 record 类型不校验位宽；它从不被传给真正计算位宽的路径，只作为算术函数里 `result_fmt` 的「未指定」哨兵参与相等比较。

**练习 2**：Python 的 `FixFormat` 在构造 `(0,0,-1)` 时会发生什么？对照 VHDL 说明了什么？

> **答案**：Python 会因 `assert I+F >= 0`（`0+(-1) = -1 < 0`）立刻抛异常。这说明 Python 用类型构造器把关、VHDL 用函数体断言把关——同一种「语义约束」，两种语言用了不同的拦截位置。

---

### 4.2 Format Functions 签名：只算格式、不碰数据

#### 4.2.1 概念说明

有一类函数**只接受格式、返回格式或位宽**，完全不接触实际的定点数据（`std_logic_vector`）。它们的作用是：在任何运算之前，先帮你算清楚「结果该用什么格式」「需要多少位」。

这一类叫 **Format Functions**。它们是纯函数（同样的输入永远给同样的输出），可以在**编译期/ elaboration 期**就求值，非常适合写成 `constant`。

#### 4.2.2 核心流程

最基础的两个：

- `cl_fix_width(fmt)` 返回 `S+I+F`。
- `cl_fix_max_value` / `cl_fix_min_value` 返回该格式能表示的最大/最小值的比特向量（注意，这俩返回 `std_logic_vector`，是少数「不纯」的格式函数，但结果只取决于格式）。

位宽公式：

\[
\text{width} = S + I + F
\]

最大值（有符号时符号位强制为 0）：

\[
\text{max\_value} = 2^{I} - 2^{-F}
\]

最小值（有符号时是符号位为 1、其余为 0）：

\[
\text{min\_value} = \begin{cases} -2^{I} & S = 1 \\ 0 & S = 0 \end{cases}
\]

格式推导函数（`cl_fix_add_fmt` / `sub_fmt` / `addsub_fmt` / `mult_fmt` / `neg_fmt` / `abs_fmt` / `shift_fmt` / `round_fmt`）的算法是单元 3 的主题，本讲只认它们的**签名**：都接受一两个 `FixFormat_t`（外加模式参数），返回一个 `FixFormat_t`。

#### 4.2.3 源码精读

Format Functions 全部签名（位宽、极值、八种格式推导、三元选择器 `choose`）：

[en_cl_fix_pkg.vhd:76-101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L76-L101) — 注意几个要点：
- `cl_fix_width` 返回 `natural`（非负整数）。
- `cl_fix_max_value` / `min_value` 返回 `std_logic_vector`。
- `cl_fix_shift_fmt` 有**两个重载**：一个给 `min_shift/max_shift` 区间（L96），一个给单个 `shift`（L97）。
- `choose(condition, a_fmt, b_fmt)`（L101）是 FixFormat 的三元运算符（`condition ? a : b`），因为 VHDL-93 没有内置三元表达式，库自己造了一个。

`cl_fix_width` 的实现极简，一眼能看懂：

[en_cl_fix_pkg.vhd:365-368](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L365-L368) — 就一句 `return fmt.S + fmt.I + fmt.F;`。这印证了 4.1 说的：位宽纯靠字段相加，**没有任何 `assert I+F>=0`**——所以传进负位宽格式会直接得到负数 `natural`（运行期可能溢出报错），类型层不保护你。

`cl_fix_add_fmt` 的签名（算法细节留到单元 3，这里只看它「吃两个格式、吐一个格式」）：

[en_cl_fix_pkg.vhd:84](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L84) — `function cl_fix_add_fmt(a_fmt, b_fmt : FixFormat_t) return FixFormat_t;`。它的函数体 [en_cl_fix_pkg.vhd:392-436](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L392-L436) 用一大段注释推导「加法何时整数位 +1」，结论浓缩成一行：`min(a.I, b.I) + min(a.F, b.F) > 0` 则整数位增长。本讲不必读懂推导，只要知道它能用即可。

`cl_fix_addsub_fmt`（add 与 sub 的并集）：

[en_cl_fix_pkg.vhd:500-506](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L500-L506) — 内部先算 add_fmt 和 sub_fmt，再取 `union`（逐字段取 max）。这告诉你 `addsub` 是「同时覆盖加和减两种极端」的保守格式。

#### 4.2.4 代码实践

**目标**：用格式推导函数预算位宽，体会「纯函数、可常量化」。

**操作步骤**（先心算，再对照现成 testbench）：

1. 心算 `(1,1,1) + (0,7,0)` 的加法结果格式。
2. 打开现成 testbench 对照。

[testbench 里的标准答案](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/en_cl_fix_pkg_tb.vhd#L70) — `check_equal(cl_fix_add_fmt((1, 1, 1), (0, 7, 0)), (1, 8, 1), ...)`，即结果是 `(1,8,1)`，位宽 10。

**为什么是 `(1,8,1)`？** S=max(1,0)=1；I：min(1,7)+min(1,0)=1+0=1>0 → 增长 1，max(1,7)+1=8；F=max(1,0)=1。所以 `(1,8,1)`。

**预期结果**：与 testbench 断言一致，`(1,8,1)`。

#### 4.2.5 小练习与答案

**练习 1**：用 `cl_fix_width` 算 `(1,-2,3)` 的位宽。它在 VHDL 里能正常调用吗？

> **答案**：位宽 `1+(-2)+3 = 2`。能正常调用——`cl_fix_width` 只做加法，不校验 `I+F>=0`，而这里 `I+F=1>=0` 本身也合法。

**练习 2**：`cl_fix_shift_fmt` 为什么需要两个重载？

> **答案**：定点移位有时移位量是固定值（用单值版 `shift`），有时是一个范围（用 `min_shift/max_shift` 版，结果格式要能容纳范围内的最坏情况，即整数位按 `max_shift` 增长、小数位按 `min_shift` 增长）。两个重载覆盖这两种用法。

---

### 4.3 Rounding and Saturation 签名：改变格式的三大操作

#### 4.3.1 概念说明

当要把数据从一个格式搬到另一个格式时，会遇到两类「装不下」：

- **小数位变少**（`result_fmt.F < a_fmt.F`）：低位要被丢弃，需要**舍入**（round）决定进不进位。
- **整数位/符号位变少**（`result_fmt` 的高位更窄）：高位装不下，需要**饱和**（saturate）决定是回绕还是钳位。

库提供三个核心函数：

- `cl_fix_round`：只处理小数位减少（舍入）。
- `cl_fix_saturate`：只处理整数位/符号位减少（饱和）。
- `cl_fix_resize`：**先 round 再 saturate**，一站式格式转换（最常见的入口）。

外加一个查询函数 `cl_fix_in_range`（只判断「resize 后是否会溢出」，不真的改）。

#### 4.3.2 核心流程

`cl_fix_resize` 的执行顺序（这是全库最重要的约定之一）：

```
resize(a, a_fmt, result_fmt, round, saturate)
   │
   ├─ 1. cl_fix_round(a, a_fmt, rounded_fmt, round)   # 先把小数位舍入到 result_fmt.F
   │       rounded_fmt = cl_fix_round_fmt(a_fmt, result_fmt.F, round)
   │
   └─ 2. cl_fix_saturate(rounded, rounded_fmt, result_fmt, saturate)  # 再把高位饱和到 result_fmt
```

**顺序不能对调**：必须先舍入、后饱和。因为舍入可能产生进位（例如 `0111.1` 舍入成 `1000`），这个进位可能恰好导致高位溢出，必须由随后的饱和来钳住。若先饱和再舍入，就会漏掉「舍入引发的溢出」。

注意默认参数：`round : FixRound_t := Trunc_s`、`saturate : FixSaturate_t := Warn_s`。也就是说，**不指定时默认是「截断 + 仅告警不饱和」**——这是个相对「危险」的默认（会回绕并打印告警），所以实际设计里常常显式写 `Sat_s` 或 `SatWarn_s`。

#### 4.3.3 源码精读

Rounding and Saturation 全部签名：

[en_cl_fix_pkg.vhd:131-162](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L131-L162) — 读签名时抓住这几条：
- 三个数据转换函数都接受 `(a, a_fmt, result_fmt)`，外加各自的模式参数和默认值。
- `cl_fix_round` 多一个 `fmt_check : boolean := true`：默认开启「结果格式合法性校验」——它会断言 `result_fmt` 必须等于 `cl_fix_round_fmt(a_fmt, result_fmt.F, round)`，防止你随手写一个位宽不够的结果格式。设成 `false` 可关掉（自负风险）。
- `cl_fix_in_range` 返回 `boolean`（只查不改），也带 `round` 参数——因为「舍入后是否还在范围内」取决于舍入模式。

紧接着是三个**同名的 `cl_fix_recommended_pipelining` 重载**：

[en_cl_fix_pkg.vhd:164-185](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L164-L185) — VHDL 允许函数重载（同名不同参数表）。这里分别给 round、saturate、resize 各一个重载，返回「推荐的流水线级数」。这是为单元 7 的可综合 RTL 服务的，本讲只需知道「它们存在、且返回 `natural`」。

`cl_fix_resize` 的函数体完美印证了上面的「先 round 后 saturate」流程：

[en_cl_fix_pkg.vhd:1011-1024](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1011-L1024) — 先用 `cl_fix_round_fmt` 算出舍入后的中间格式 `rounded_fmt_c`，调用 `cl_fix_round` 得到 `rounded_c`，再 `cl_fix_saturate` 到 `result_fmt`。整个函数就这两步，干净利落。

`cl_fix_saturate` 体内有一条关键断言：

[en_cl_fix_pkg.vhd:988](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L988) — `assert result_fmt.F = a_fmt.F ...`，即 **saturate 不允许改变小数位数**。要变小数位，请先 round。这正是为什么 resize 要把 round 放在前面。

#### 4.3.4 代码实践

**目标**：读懂 testbench 里一条完整的 resize 调用，验证「先 round 后 saturate」。

**操作步骤**（阅读型实践）：定位 testbench 里这条断言：

[en_cl_fix_pkg_tb.vhd:170-173](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/en_cl_fix_pkg_tb.vhd#L170-L173) — 把 `"0101"`（格式 `(1,2,1)`，即 +2.5）resize 到 `(1,2,0)`：
- 用 `Trunc_s`：截断小数位 → `"010"`（+2）。
- 用 `NonSymPos_s`：半向上舍入 → `"011"`（+3）。

**需要观察的现象**：同一条输入、同一个目标格式，仅仅换舍入模式，结果就不同（+2 vs +3）。这正是 round 参数的作用。

**预期结果**：`"010"` 与 `"011"`，与 testbench 断言一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cl_fix_saturate` 要断言 `result_fmt.F = a_fmt.F`？

> **答案**：饱和只解决「高位装不下」（整数位/符号位减少）的问题；它内部用 `convert` 做对齐，而 `convert` 明确不支持小数位减少（见函数体注释）。所以减少小数位必须先用 `cl_fix_round`， saturate 只在小数位不变的前提下工作。

**练习 2**：`cl_fix_resize` 不传 `round` 和 `saturate` 时，行为是什么？

> **答案**：`round` 默认 `Trunc_s`（截断），`saturate` 默认 `Warn_s`（不饱和、只告警）。也就是「丢弃小数位用截断，高位溢出会回绕并打印一条 Warning」。这在正式设计里通常不是你想要的，所以常需显式指定。

---

### 4.4 Math Functions 签名：算术、比较与哨兵默认值

#### 4.4.1 概念说明

Math Functions 是真正做**数值运算**的函数：取反 `cl_fix_neg`、绝对值 `cl_fix_abs`、加减 `cl_fix_add`/`sub`/`addsub`、乘 `cl_fix_mult`、移位 `cl_fix_shift`，外加比较 `cl_fix_compare` 和取符号位 `cl_fix_sign`。

它们有一个**统一的签名骨架**，理解了骨架，八个函数一通百通。

#### 4.4.2 核心流程

每个算术函数都遵循同一个「三段式」内部流程（虽然本讲只看签名，但知道流程能帮你理解默认参数）：

```
1. 算全精度中间格式  mid_fmt = cl_fix_xxx_fmt(...)   # 不会丢任何精度
2. 把输入对齐到 mid_fmt，做真正的加减乘
3. cl_fix_resize(...)  从 mid_fmt 收敛到调用者要的 result_fmt   # 这里才舍入/饱和
```

**关键约定**：`result_fmt` 的默认值是 `NullFixFormat_c`（4.1 讲的哨兵）。函数体里有一句 `choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)`——意思是：**你没指定结果格式，我就给你全精度的 `mid_fmt`**。这与 Python 侧「`r_fmt` 缺省即 `mid_fmt`」（u2-l2）是同一个约定，只是 VHDL 用一个哨兵常数来表达「缺省」。

#### 4.4.3 源码精读

Math Functions 全部签名：

[en_cl_fix_pkg.vhd:187-264](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L187-L264) — 抓住签名里的共同模式。以 `cl_fix_add` 为例：

[en_cl_fix_pkg.vhd:206-214](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L206-L214) — 参数依次是 `(a, a_fmt, b, b_fmt, result_fmt := NullFixFormat_c, round := Trunc_s, saturate := Warn_s)`，返回 `std_logic_vector`。八个算术函数几乎都是这个模板，区别只在：
- 一元函数（`abs`/`neg`）只有一个输入 `(a, a_fmt)`。
- `cl_fix_addsub` 多一个 `add : std_logic` 选择器（`'1'` 加、`'0'` 减）。
- `cl_fix_shift` 多一个 `shift : integer`（注意它的 `result_fmt` **没有默认值**，必须显式给）。

`cl_fix_add` 的函数体印证三段式与哨兵约定：

[en_cl_fix_pkg.vhd:1149-1172](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1149-L1172) — 先 `cl_fix_add_fmt(a_fmt, b_fmt)` 算 `mid_fmt_c`；再用 `choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)` 决定真正的 `r_fmt_c`；把 `a`、`b` 都 `convert` 到 `mid_fmt` 对齐小数点；用 `signed(a_v) + signed(b_v)` 做真正的加法（注释解释了为何一律用 `signed`：Vivado DSP 在 `unsigned` 上有 bug）；最后 `cl_fix_resize` 收敛到 `r_fmt_c`。

比较与取符号（不是算术，但归在同一块）：

[en_cl_fix_pkg.vhd:256-264](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L256-L264) — `cl_fix_compare(comparison, a, aFmt, b, bFmt) return boolean` 接受字符串比较符（`"<"`、`">="`、`"="` 等），先把两数对齐到 `union(aFmt,bFmt)` 再比较；`cl_fix_sign(a, aFmt) return std_logic` 返回符号位（无符号数恒 `'0'`）。

> 小提示：注意这几个函数里参数名一会是 `a_fmt`（带下划线），一会是 `aFmt`（驼峰）。这是历史遗留的不一致，阅读时按位置传参最稳妥。

#### 4.4.4 代码实践

**目标**：跟踪一条 `cl_fix_add` 调用，确认「不传 result_fmt → 得到全精度结果」。

**操作步骤**（阅读型实践）：看 testbench 这条：

[en_cl_fix_pkg_tb.vhd:313-317](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/en_cl_fix_pkg_tb.vhd#L313-L317) — `cl_fix_add(0.75@(0,0,4), 4.0@(0,4,-1), result_fmt => (0,5,5))`，期望 `4.75@(0,5,5)`。这里两个加数格式差异很大（一个纯小数、一个负小数位整数），但 `cl_fix_add` 内部把它们都对齐到 `mid_fmt` 再加，结果正确。

**需要观察的现象**：两个「小数点位置完全不同」的数能直接相加——因为 `convert` 会先把它们的小数点对齐。这正是定点库相对于「手写 `signed` 运算」的核心价值。

**预期结果**：`4.75`，位宽符合 `(0,5,5)`（即 `cl_fix_add_fmt((0,0,4),(0,4,-1))` 给出的全精度格式）。

#### 4.4.5 小练习与答案

**练习 1**：调用 `cl_fix_mult(a, a_fmt, b, b_fmt)` 不传 `result_fmt`，会得到几位结果？

> **答案**：得到 `cl_fix_mult_fmt(a_fmt, b_fmt)` 给出的全精度结果，符号位 = `max(a.S,b.S)`（含 1-bit 有符号相乘变无符号的特例），整数位 = `a.I+b.I`（或 ±1 的特例），小数位 = `a.F+b.F`。简言之：**位宽 = 两操作数位宽之和**（去掉特例微调）。

**练习 2**：为什么 `cl_fix_shift` 的 `result_fmt` 没有默认值，而别的算术函数都有？

> **答案**：因为移位的本意是「把数据搬到另一个格式」，调用者必须明确目标格式；给一个全精度默认值反而违背用法（移位本身不丢精度，但目标格式几乎总是被设计成更窄）。所以它强制要求显式 `result_fmt`。

---

## 5. 综合实践

**任务**：写一个最小 VHDL「练习台」，定义两个格式常数，调用 `cl_fix_width` 与 `cl_fix_add_fmt`，用 `report` 打印结果，并与现成 testbench 对拍。

**操作步骤**：

1. 阅读下面的示例代码（**示例代码，非项目原有文件**），理解每一行。

```vhdl
-- 示例代码：最小练习台，演示 Format Functions 的编译期求值
library ieee;
    use ieee.std_logic_1164.all;
library work;
    use work.en_cl_fix_pkg.all;     -- 引入本讲讲的类型与函数

entity demo is
end entity;

architecture sim of demo is
    -- 两个格式常数（可在 elaboration 期求值）
    constant A_fmt_c : FixFormat_t := (1, 4, 8);   -- 位宽 13
    constant B_fmt_c : FixFormat_t := (0, 2, 8);   -- 位宽 10
begin
    process
    begin
        report "A width = " & integer'image(cl_fix_width(A_fmt_c));
        report "B width = " & integer'image(cl_fix_width(B_fmt_c));
        report "A+B fmt  = " & to_string(cl_fix_add_fmt(A_fmt_c, B_fmt_c));
        wait;
    end process;
end architecture;
```

2. **手动预测**（关键步骤，先别看下面）：
   - `cl_fix_width((1,4,8))` = ？
   - `cl_fix_width((0,2,8))` = ？
   - `cl_fix_add_fmt((1,4,8),(0,2,8))` = ？（提示：S=max；I 看 `min(I)+min(F)>0`；F=max）

3. 用 GHDL/NVC/ModelSim 仿真（若环境就绪），或对照下面的预期结果。

**需要观察的现象**：`report` 打印出三条消息；`to_string(FixFormat_t)` 把格式渲染成 `(S,I,F)` 字符串。

**预期结果**（**待本地验证**，以下为按本讲解读推得）：
- A width = 13
- B width = 10
- A+B fmt = `(1,5,8)`（推导：S=max(1,0)=1；`min(4,2)+min(8,8)=2+8=10>0` → 整数位 +1，I=max(4,2)+1=5；F=max(8,8)=8）

**交叉验证**：把示例里的格式换成 testbench 已经断言过的 `(1,1,1)` 与 `(0,7,0)`，预期 `cl_fix_add_fmt` 返回 `(1,8,1)`——这与 [tb/en_cl_fix_pkg_tb.vhd:70](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/en_cl_fix_pkg_tb.vhd#L70) 的标准答案完全一致。若一致，说明你的环境与解读都正确。

> 进阶（可选）：若已装好 VUnit（见 u1-l4），可直接运行现成的 `en_cl_fix_pkg_tb`，看到 `SUCCESS! All tests passed.` 即证明本讲引用的全部签名都能正常工作。

## 6. 本讲小结

- `hdl/en_cl_fix_pkg.vhd` 的头部声明了库的全部**类型与函数签名**，是 Python `en_cl_fix.py` 的 VHDL 镜像。
- `FixFormat_t` 是三字段 record（`S` 限 0/1，`I/F` 不受限）；**VHDL 类型层不强制 `I+F>=0`**，这一点与 Python 的构造期断言不同，校验责任落在函数体。
- `FixRound_t`（7 种）/ `FixSaturate_t`（4 种）与 Python 逐字对应；`RegisterMode_t` 是 VHDL 专属，控制可综合 RTL 的寄存器插入。
- `NullFixFormat_c = (0,0,-1)` 是「未指定结果格式」的哨兵，算术函数用它实现「缺省即全精度 mid_fmt」。
- 头部函数分四块：**Format Functions**（纯格式计算）、**Type/String Conversions**、**Rounding and Saturation**（round/saturate/resize，注意 resize 先 round 后 saturate）、**Math Functions**（算术，统一三段式：mid_fmt → 运算 → resize）。
- 函数普遍带默认参数：`round := Trunc_s`、`saturate := Warn_s`、`result_fmt := NullFixFormat_c`；`cl_fix_shift` 的 `result_fmt` 是少数无默认值的。

## 7. 下一步学习建议

本讲只读了**签名**，把「怎么调用」讲清楚了。接下来：

- **单元 3（u3-l1/u3-l2）**：深入 `cl_fix_add_fmt` / `mult_fmt` / `neg_fmt` 等的**推导算法**——本讲刻意跳过的大段注释，那里会逐条讲清「整数位何时增长」。
- **单元 4（u4-l1～u4-l3）**：深入 `cl_fix_round` / `saturate` / `resize` 的**实现**——本讲只看到了 `resize` 的两步骨架，那里会讲 7 种舍入模式怎么用「加偏移再截断」统一实现。
- **单元 5（u5-l2）**：把本讲 4.4 的「三段式」展开，看 `convert → 运算 → resize` 的完整算术链路。
- **单元 7（u7-l1/u7-l2）**：把头部的 `cl_fix_recommended_pipelining` 与 `RegisterMode_t` 用起来——进入可综合 RTL 实体（`en_cl_fix_round.vhd` 等）。

建议在本讲基础上，先把 `en_cl_fix_pkg_tb.vhd` 通读一遍：它是本讲所有签名的「活字典」，每条 `check_equal` 都是一条现成的调用范例。
