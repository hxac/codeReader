# VHDL 包头：类型与公共 API

## 1. 本讲目标

本讲把目光从「概念」移到「VHDL 源码」。我们将完整拆解 `hdl/en_cl_fix_pkg.vhd` 的**包头（package header）**：它定义了哪些类型、对外暴露了哪些函数、以及这些函数的默认参数约定。

读完本讲，你应当能够：

1. 说出 `FixFormat_t` / `FixRound_t` / `FixSaturate_t` / `RegisterMode_t` 四个核心类型的字段与取值，并理解它们为何这样定义。
2. 看懂包头里 40 多个公共函数的**五大分组**（格式函数 / 字符串转换 / 类型转换 / 舍入饱和 / 数学函数），并能给任意函数归类。
3. 解释默认参数的三条通用约定（`round := Trunc_s`、`saturate := Warn_s`、`result_fmt := NullFixFormat_c`），并说清楚 `NullFixFormat_c` 这个「哨兵」值的作用。
4. 理解包对 VHDL-93 的合规性要求，以及由此带来的几个工程化取舍。

本讲**只读包头**（声明），不深入函数体（实现）。包体的逐行精读留给后续 u5-l2 / u5-l3；私有工具函数留给 u5-l4。

## 2. 前置知识

本讲需要你已经建立以下认知（来自 u1-l2 与 u2-l1）：

- **三语言镜像架构**：en_cl_fix 以 VHDL 为语义金标准，Python 同名同参数地镜像它作参考模型，MATLAB 只做数据封装后调用 Python。因此，**VHDL 包头里每一个 `cl_fix_*` 函数，都对应 Python 里的一个同名函数**——你看到 VHDL 接口，就等于看到了整套库的对外契约。
- **定点格式 `[S,I,F]`**：S 是符号位数（只能 0 或 1），I 是整数位，F 是小数位，I 与 F 可为负。总位宽 \( W = S + I + F \)。
- **VHDL-93 双标准**：可综合 RTL（含本包）按 VHDL-93 编译，测试台按 VHDL-2008 编译。这一点会解释包里若干「为什么不直接用语言内置特性」的取舍。

如果你对 VHDL 的 `record`、枚举类型（`type ... is (...)`）、子程序默认参数（`:=`）还不熟悉，下面会用最小例子顺带带过。

## 3. 本讲源码地图

本讲几乎全部内容来自下面这一个文件（以及一个组件用于佐证 `RegisterMode_t`）：

| 文件 | 角色 | 本讲关注 |
| --- | --- | --- |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 主包：类型 + 全部公共函数 | 包头 L34–L266 的声明 |
| [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd) | 可流水线化的舍入组件 | 仅看它如何消费 `RegisterMode_t` |

包内还引用了一个私有包 `work.en_cl_fix_private_pkg`（提供 `maximum`/`minimum`/`choose` 等工具），它的细节是 u5-l4 的主题，本讲只在用到时提及。

整个文件分为两大块：

- **包头（L34–L266）**：`package en_cl_fix_pkg is ... end package;`，只放类型与函数声明——本讲主战场。
- **包体（L271–L1324）**：`package body en_cl_fix_pkg is ... end;`，放函数实现——后续讲义精读。

## 4. 核心概念与源码讲解

### 4.1 类型定义：record 与枚举

#### 4.1.1 概念说明

en_cl_fix 对外的一切计算都建立在四个类型之上：

- `FixFormat_t`：描述一个定点格式 `[S,I,F]`，用 VHDL 的 **record（记录）** 表达。
- `FixRound_t`：舍入模式，用**枚举（enumeration）** 列出七种语义。
- `FixSaturate_t`：饱和模式，用枚举列出四种语义。
- `RegisterMode_t`：寄存器插入策略，用枚举列出三种语义（见 4.2）。

为什么用 record + 枚举，而不是裸 `integer` 或 `std_logic_vector`？因为类型系统能在**编译期**挡住大量误用：

- `S : natural range 0 to 1` 直接禁止把符号位写成 2。
- 枚举类型让 `Trunc_s` 这种符号比裸整数 `0` 可读得多，并且函数签名能精确表达「这里要的是一个舍入模式，而不是任意整数」。

#### 4.1.2 核心流程

定点格式三元组与位宽的关系：

\[
W = S + I + F
\]

其中 \( S \in \{0,1\} \)，\( I, F \in \mathbb{Z} \)（可负）。库内部约定 \( I + F \ge 0 \)，从而 \( W \ge S \ge 0 \)，保证位宽非负。舍入与饱和则分别用各自的枚举值表达「丢小数位时怎么取舍」「越界时怎么处理」。

四种类型的取值一览：

| 类型 | 含义 | 取值 |
| --- | --- | --- |
| `FixFormat_t` | 格式 `[S,I,F]` | 任意满足约束的三元组 |
| `FixRound_t` | 舍入模式 | `Trunc_s`、`NonSymPos_s`、`NonSymNeg_s`、`SymInf_s`、`SymZero_s`、`ConvEven_s`、`ConvOdd_s` |
| `FixSaturate_t` | 饱和模式 | `None_s`、`Warn_s`、`Sat_s`、`SatWarn_s` |
| `RegisterMode_t` | 寄存器策略 | `Auto_s`、`Yes_s`、`No_s` |

#### 4.1.3 源码精读

`FixFormat_t` 是一个三字段 record，关键字段都带子类型约束：

```vhdl
type FixFormat_t is record
    S   : natural range 0 to 1;  -- Sign bit.
    I   : integer;               -- Integer bits.
    F   : integer;               -- Fractional bits.
end record;
```

这段声明了格式的「骨架」。`S` 用 `natural range 0 to 1` 把符号位钉死在 0/1。详见 [hdl/en_cl_fix_pkg.vhd:39-43](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L39-L43)，这里定义了贯穿全库的核心数据类型。

紧跟其后的是数组类型与哨兵常量：

```vhdl
constant NullFixFormat_c    : FixFormat_t := (0, 0, -1);
type FixFormatArray_t is array(natural range <>) of FixFormat_t;
```

`FixFormatArray_t` 让测试台可以一次读入「一整批格式」（例如从 cosim 生成的数据文件里读到一串格式）。`NullFixFormat_c` 是个特殊常量，它的位宽 \( W = 0 + 0 + (-1) = -1 \)，是个**非法格式**——这正是它作为「哨兵」的关键，详见 4.4。位置见 [hdl/en_cl_fix_pkg.vhd:45-47](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L45-L47)。

两个语义枚举：

```vhdl
type FixRound_t is ( Trunc_s, NonSymPos_s, NonSymNeg_s, SymInf_s,
                     SymZero_s, ConvEven_s, ConvOdd_s );

type FixSaturate_t is ( None_s, Warn_s, Sat_s, SatWarn_s );
```

舍入七态与饱和四态的语义已在 u2-l2、u2-l3 详述，这里只看声明：它们是纯枚举，没有任何附加字段。位置见 [hdl/en_cl_fix_pkg.vhd:49-66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L49-L66)。注意枚举字面量都带 `_s` 后缀（s = state/样式），这是全包统一的命名风格，既能与人名/变量区分，也方便后文的字符串互转。

#### 4.1.4 代码实践

**实践目标**：确认 record 的子类型约束确实在编译期生效。

**操作步骤**：

1. 在任意 VHDL 文件里 `use work.en_cl_fix_pkg.all;`。
2. 尝试声明 `constant bad : FixFormat_t := (2, 4, 4);`（符号位写成 2）。
3. 编译（用 GHDL 或你手头的仿真器）。

**需要观察的现象**：编译器应报错，提示 `S` 不满足 `natural range 0 to 1`。

**预期结果**：约束在** elaboration / 编译期**就拦截非法值，无需运行期检查。若你手头没有仿真器，可标注「待本地验证」，转而做源码阅读型实践：在包体里搜索 `fmt.S` 的所有用法，确认所有分支都隐含假设 `S ∈ {0,1}`。

#### 4.1.5 小练习与答案

**练习 1**：`NullFixFormat_c = (0,0,-1)` 的位宽是多少？为什么它不可能是一个合法格式？

**答案**：\( W = 0 + 0 + (-1) = -1 \)。位宽为负，不可能对应任何真实存储，因此它永远不会与一个用户真正想用的格式冲突——这正是它能当「未指定」哨兵的原因（详见 4.4）。

**练习 2**：`FixSaturate_t` 的四个值可以看作「是否钳位」与「是否告警」两个独立开关的笛卡尔积。请把四个值填入下表。

**答案**：

| | 不告警 | 告警 |
| --- | --- | --- |
| **不钳位** | `None_s` | `Warn_s` |
| **钳位** | `Sat_s` | `SatWarn_s` |

### 4.2 RegisterMode_t 与推荐流水线

#### 4.2.1 概念说明

`RegisterMode_t` 与前面三个类型不同：它**不参与定点数值计算**，而是服务于「可综合 RTL 的时序」。en_cl_fix 除了提供纯函数（`cl_fix_round` 等），还提供三个可实例化的流水线组件（`en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize`，见 u6-l1）。这些组件需要在「插不插寄存器」上做选择，`RegisterMode_t` 就是这个开关。

关键在于：**并非所有舍入/饱和都需要寄存器**。例如纯截断（`Trunc_s`）只是丢几位，是零逻辑、零延迟；而真正的舍入（加偏移再截断）才可能需要一拍寄存器来改善时序。`cl_fix_recommended_pipelining` 这个纯函数就是用来在**综合期**算出「推荐插几拍」。

#### 4.2.2 核心流程

`RegisterMode_t` 三态的语义（与组件注释一致）：

- `Auto_s`：按 `cl_fix_recommended_pipelining` 的推荐值插寄存器——推荐 0 拍就 0 拍，推荐 1 拍就 1 拍。延迟随 generics 变化。
- `Yes_s`：**总是**插寄存器，延迟恒为 1。用于需要跨配置保持固定延迟的场合。
- `No_s`：**绝不**插寄存器，延迟恒为 0。通常劣化时序，谨慎使用。

组件内部把它们归约成一个布尔判定：

```
use_reg = (mode == Yes_s) 或 (mode == Auto_s 且 recommended > 0)
```

`recommended` 由 `cl_fix_recommended_pipelining` 给出，目前只能是 0 或 1。

#### 4.2.3 源码精读

枚举声明本身在包头，每个字面量都带说明性注释：

```vhdl
type RegisterMode_t is
(
    Auto_s,   -- Inserts the recommended registering. See cl_fix_recommended_pipelining.
    Yes_s,    -- Inserts all registering. Can be useful for consistent latency.
    No_s      -- Inserts no registering. Use with caution (poor timing performance).
);
```

位置见 [hdl/en_cl_fix_pkg.vhd:68-73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L68-L73)。这里定义了组件层的时序开关。

`cl_fix_recommended_pipelining` 在包头有**三个重载**，分别对应 round / saturate / resize 三类组件：

```vhdl
-- Recommended pipeline stages for cl_fix_round
function cl_fix_recommended_pipelining(
    a_fmt       : FixFormat_t;
    result_fmt  : FixFormat_t;
    round       : FixRound_t;
    fmt_check   : boolean := true
) return natural;

-- Recommended pipeline stages for cl_fix_saturate
function cl_fix_recommended_pipelining(
    a_fmt       : FixFormat_t;
    result_fmt  : FixFormat_t;
    saturate    : FixSaturate_t
) return natural;

-- Recommended pipeline stages for cl_fix_resize
function cl_fix_recommended_pipelining(
    a_fmt       : FixFormat_t;
    result_fmt  : FixFormat_t;
    round       : FixRound_t;
    saturate    : FixSaturate_t
) return natural;
```

VHDL 允许**同名函数按参数列表区分**（重载），调用方靠实参类型自动选择。位置见 [hdl/en_cl_fix_pkg.vhd:165-185](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L165-L185)。

舍入版的实现清楚地展示了「何时 0 拍、何时 1 拍」的判定（截断或不动小数位 → 0 拍，否则 1 拍），见 [hdl/en_cl_fix_pkg.vhd:1041-1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069)。

最后看组件如何消费它——`en_cl_fix_round` 把三态归约成一行布尔常量：

```vhdl
constant recommended_c  : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, fmt_check_g);
constant use_reg_c      : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
```

随后用两个互斥的 `generate` 分支分别给出「带寄存器」与「不带寄存器」的实现，见 [hdl/en_cl_fix_round.vhd:85-86](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L85-L86) 与 [hdl/en_cl_fix_round.vhd:95-111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L95-L111)。这就是 `Auto_s` 能在「无需寄存器时自动 0 拍」的根源。

#### 4.2.4 代码实践

**实践目标**：理解 `Auto_s` 的延迟会随 generics 变化。

**操作步骤**：

1. 假想例化一个 `en_cl_fix_round`，`reg_mode_g => Auto_s`。
2. 场景 A：`in_fmt_g=(0,4,4)`、`out_fmt_g=(0,4,1)`、`round_g=>Trunc_s`。
3. 场景 B：同上，但 `round_g=>NonSymPos_s`。
4. 对每个场景，手工套用 `cl_fix_recommended_pipelining` 的判定：`round = Trunc_s` ⇒ 0；否则看 `result_fmt.F >= a_fmt.F`？1≥4 不成立 ⇒ 1。

**需要观察的现象**：场景 A 推荐拍数为 0（延迟 0），场景 B 推荐拍数为 1（延迟 1）。

**预期结果**：同一个 `Auto_s` 组件，仅因 `round_g` 不同而延迟不同——这正是组件注释里「latency changes when other generics are changed」的含义。若想固定延迟，应改用 `Yes_s`。这是源码阅读型实践，无需运行仿真。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cl_fix_recommended_pipelining` 要做成**纯函数**而不是组件里的硬编码？

**答案**：因为它只依赖格式与模式参数，可在综合期求值。做成纯函数后，组件用一个常量 `recommended_c` 接住结果，综合器能据此在 `generate` 之间二选一，把不需要的寄存器分支彻底优化掉。

**练习 2**：如果设计中要求「无论参数怎么配，这条舍单元永远是 1 拍延迟」，应选哪个 `RegisterMode_t`？

**答案**：`Yes_s`。它无条件插寄存器，延迟恒为 1，代价是即便推荐 0 拍也会多占一拍。

### 4.3 公共 API 的五大分组与默认参数

#### 4.3.1 概念说明

包头把全部公共函数按职责分成五组，每组用一个注释栏分隔。这种分组不是语法要求，而是**接口契约的地图**：读者扫一眼栏标题就能定位功能。五组分别是：

1. **Format Functions（格式函数）**：纯函数，输入格式 → 输出格式或位宽，不碰具体数据。可综合期求值。
2. **String Conversions（字符串转换）**：格式/枚举与字符串互转，主要用于测试台打印与配置文件解析。
3. **Type Conversions（类型转换）**：定点与 `real`/`integer` 之间的互转，常用于常量表与仿真校验。
4. **Rounding and Saturation（舍入与饱和）**：精度控制的三个原语 `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize`，外加范围判断与推荐流水线。
5. **Math Functions（数学函数）**：高层算术（abs/neg/add/sub/addsub/mult/shift）与比较/取符号。

#### 4.3.2 核心流程

全包遵循三条默认参数约定，理解了它们就能猜出大半函数的行为：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `round` | `Trunc_s` | 不显式指定就当「直接截断」 |
| `saturate` | `Warn_s` | 不显式指定就当「回绕 + 越界告警」 |
| `result_fmt` | `NullFixFormat_c`（仅数学函数） | 不显式指定就当「要全精度结果」 |

注意两个例外：

- `cl_fix_from_real` 的 `saturate` 默认是 `SatWarn_s`（因为从实数构造时回绕未实现，必须钳位）。
- `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` / `cl_fix_in_range` / `cl_fix_shift` 的 `result_fmt` **没有默认值**，必须显式给出——因为它们本身就是精度控制原语，结果格式是它们的核心输入。

也就是说：**数学函数是「高层」，允许你不指定结果格式；round/saturate/resize 是「底层原语」，必须显式。**

#### 4.3.3 源码精读

包头从 L75 开始就是五栏函数声明。这里按组摘录关键签名（完整签名见源码）。

**① 格式函数（[hdl/en_cl_fix_pkg.vhd:75-101](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L75-L101)）**——只吃格式、吐格式/位宽：

```vhdl
function cl_fix_width(fmt : FixFormat_t) return natural;
function cl_fix_max_value(fmt : FixFormat_t) return std_logic_vector;
function cl_fix_min_value(fmt : FixFormat_t) return std_logic_vector;
function cl_fix_add_fmt(a_fmt : FixFormat_t; b_fmt : FixFormat_t) return FixFormat_t;
function cl_fix_mult_fmt(a_fmt : FixFormat_t; b_fmt : FixFormat_t) return FixFormat_t;
function cl_fix_round_fmt(a_fmt : FixFormat_t; r_frac_bits : integer; rnd : FixRound_t) return FixFormat_t;
```

注意 `cl_fix_shift_fmt` 有**两个重载**（一个给 `min_shift/max_shift` 区间，一个给固定 `shift`）：

```vhdl
function cl_fix_shift_fmt(a_fmt : FixFormat_t; min_shift : integer; max_shift : integer) return FixFormat_t;
function cl_fix_shift_fmt(a_fmt : FixFormat_t; shift : integer) return FixFormat_t;
```

`choose` 也在这一组（条件选择格式，等价三元运算符）。

**② 字符串转换（[hdl/en_cl_fix_pkg.vhd:103-118](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L103-L118)）**——`to_string` 有四个重载（按第一参数类型区分）：

```vhdl
function to_string(a : std_logic_vector; a_fmt : FixFormat_t) return string;
function to_string(fmt : FixFormat_t) return string;
function to_string(rnd : FixRound_t) return string;
function to_string(sat : FixSaturate_t) return string;
function cl_fix_format_from_string(Str : string) return FixFormat_t;
```

**③ 类型转换（[hdl/en_cl_fix_pkg.vhd:120-129](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L120-L129)）**——定点 ↔ 实数/整数：

```vhdl
function cl_fix_from_real(a : real; result_fmt : FixFormat_t; saturate : FixSaturate_t := SatWarn_s) return std_logic_vector;
function cl_fix_to_real(a : std_logic_vector; a_fmt : FixFormat_t) return real;
function cl_fix_to_integer(a : std_logic_vector; aFmt : FixFormat_t) return integer;
function cl_fix_from_integer(a : integer; aFmt : FixFormat_t) return std_logic_vector;
```

这里能直接看到 `cl_fix_from_real` 的 `saturate` 默认值是 `SatWarn_s` 而非通用的 `Warn_s`。

**④ 舍入与饱和（[hdl/en_cl_fix_pkg.vhd:131-185](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L131-L185)）**——精度原语，`result_fmt` 均无默认值：

```vhdl
function cl_fix_round(a; a_fmt; result_fmt; round := Trunc_s; fmt_check := true) return std_logic_vector;
function cl_fix_saturate(a; a_fmt; result_fmt; saturate := Warn_s) return std_logic_vector;
function cl_fix_resize(a; a_fmt; result_fmt; round := Trunc_s; saturate := Warn_s) return std_logic_vector;
function cl_fix_in_range(a; a_fmt; result_fmt; round := Trunc_s) return boolean;
```

（上面用 `;` 省略了参数类型，仅为示意；完整签名见源码。）

**⑤ 数学函数（[hdl/en_cl_fix_pkg.vhd:187-264](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L187-L264)）**——高层算术，`result_fmt := NullFixFormat_c`：

```vhdl
function cl_fix_add(a; a_fmt; b; b_fmt; result_fmt : FixFormat_t := NullFixFormat_c;
                    round := Trunc_s; saturate := Warn_s) return std_logic_vector;
function cl_fix_mult(a; a_fmt; b; b_fmt; result_fmt : FixFormat_t := NullFixFormat_c;
                     round := Trunc_s; saturate := Warn_s) return std_logic_vector;
```

注意 `cl_fix_shift` 是数学函数组里**唯一不给 `result_fmt` 默认值**的：

```vhdl
function cl_fix_shift(a; a_fmt; shift : integer; result_fmt : FixFormat_t;
                      round := Trunc_s; saturate := Warn_s) return std_logic_vector;
```

这一组的最后两个是比较与取符号，返回 `boolean` / `std_logic`，与算术同组：

```vhdl
function cl_fix_compare(comparison : string; a; aFmt; b; bFmt) return boolean;
function cl_fix_sign(a : std_logic_vector; aFmt : FixFormat_t) return std_logic;
```

#### 4.3.4 代码实践

**实践目标**：把包头的公共函数按五组归类，并标注每个函数的默认参数。这是本讲的**主实践（源码阅读型）**。

**操作步骤**：

1. 打开 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd)，从 L75 滚到 L264。
2. 按下表五列抄写每个函数：第一列函数名、第二列所属组、第三列返回类型、第四列「`result_fmt` 是否有默认值」、第五列「`round`/`saturate` 默认值」。
3. 特别标记 `cl_fix_from_real`（`saturate` 默认不同）与 `cl_fix_shift`（数学函数里却无 `result_fmt` 默认）。

**需要观察的现象**：除 `cl_fix_from_real` 外，所有出现 `saturate` 参数的函数默认值都是 `Warn_s`；除 `cl_fix_shift` 外，所有数学函数的 `result_fmt` 默认值都是 `NullFixFormat_c`。

**预期结果**：得到一张完整的 API 速查表。这是后续阅读包体（u5-l2/u5-l3）时的索引。

#### 4.3.5 小练习与答案

**练习 1**：`cl_fix_round` 和 `cl_fix_add` 都接受 `round` 参数，默认都是 `Trunc_s`。但 `cl_fix_round` 的 `result_fmt` 没有默认值，`cl_fix_add` 的有。为什么？

**答案**：`cl_fix_round` 是精度控制原语，它的全部职责就是把数据从 `a_fmt` 变到指定的 `result_fmt`，结果格式是核心输入、不可省；`cl_fix_add` 是高层算术，能自己用 `cl_fix_add_fmt` 算出全精度中间格式，`result_fmt` 只是「要不要再收窄」的可选项，省略即取全精度。

**练习 2**：`cl_fix_shift_fmt` 有两个重载，签名只差在第二/三参数。VHDL 编译器如何区分调用的是哪一个？

**答案**：靠**参数个数与类型**。给一个 `integer` 调用固定移位版；给两个 `integer`（`min_shift`, `max_shift`）调用区间版。VHDL 的重载解析按实参列表匹配唯一声明。

### 4.4 NullFixFormat_c：result_fmt 的「未指定」哨兵

#### 4.4.1 概念说明

`NullFixFormat_c` 是本包最重要的约定之一。它是一个**哨兵值（sentinel）**：当一个数学函数的 `result_fmt` 形参等于它时，表示「调用者没有指定结果格式」。

为什么需要哨兵？因为 VHDL-93 的默认参数必须是某个** compile-time 常量**，而「全精度结果格式」要等运行/综合期根据输入格式算出来，不能写成默认值。于是包用一个**非法格式**当标记：数学函数在体内检测到它，就改用自己算出的全精度中间格式 `mid_fmt`。

这同时呼应三语言镜像：Python 侧用 `None` 表达同样的「未指定」语义（见 u2-l4 / u4-l1），VHDL 侧因没有 `Option` 类型而用一个非法常量 `NullFixFormat_c = (0,0,-1)` 代替。两者语义一致，落地不同。

#### 4.4.2 核心流程

数学函数体内的统一模式是：

```
mid_fmt = cl_fix_<op>_fmt(a_fmt)            # 全精度结果格式
r_fmt   = 若 result_fmt == NullFixFormat_c   # 哨兵检测
          则 mid_fmt
          否则 result_fmt                    # 用户指定的（通常更窄的）格式
... 在 mid_fmt 下做无损运算 ...
return cl_fix_resize(mid, mid_fmt, r_fmt)    # 收敛到 r_fmt（可能含舍入/饱和）
```

关键点：**运算本身在 `mid_fmt` 下是无损的**，所有精度损失都集中在最后那步 `cl_fix_resize`。当 `result_fmt` 是哨兵时，`r_fmt = mid_fmt`，`resize` 退化为无操作，返回全精度结果。这正是 u4-l1 讲过的「`r_fmt=None` → 无损」语义在 VHDL 里的落地。

#### 4.4.3 源码精读

哨兵定义在类型区紧下方：

```vhdl
constant NullFixFormat_c    : FixFormat_t := (0, 0, -1);
```

见 [hdl/en_cl_fix_pkg.vhd:45](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L45)。\( W = 0+0+(-1) = -1 \)，非法，故安全。

数学函数把 `result_fmt` 默认值设为它，例如 `cl_fix_add`（签名）：

```vhdl
function cl_fix_add(
    a : std_logic_vector; a_fmt : FixFormat_t;
    b : std_logic_vector; b_fmt : FixFormat_t;
    result_fmt  : FixFormat_t := NullFixFormat_c;
    round       : FixRound_t := Trunc_s;
    saturate    : FixSaturate_t := Warn_s
) return std_logic_vector;
```

见 [hdl/en_cl_fix_pkg.vhd:206-214](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L206-L214)。同样的 `:= NullFixFormat_c` 默认出现在 `cl_fix_abs` / `cl_fix_neg` / `cl_fix_sub` / `cl_fix_addsub` / `cl_fix_mult` 六处。

包体里最清楚地展示哨兵「决策」的是 `cl_fix_abs`：

```vhdl
constant mid_fmt_c  : FixFormat_t := cl_fix_abs_fmt(a_fmt);
constant r_fmt_c    : FixFormat_t := choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt);
```

见 [hdl/en_cl_fix_pkg.vhd:1118-1119](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1118-L1119)。`choose` 是包内的三元选择函数：`result_fmt` 等于哨兵时取 `mid_fmt_c`（全精度），否则取用户给的 `result_fmt`。`cl_fix_add` / `cl_fix_mult` 等都用了完全相同的一行（如 [L1157](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1157)、[L1240](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1240)）。

为什么 VHDL 不直接用 `maximum`/`minimum` 这类 VHDL-2008 内置、不在包头用 `'image`？这是同一类「VHDL-93 合规」取舍：`maximum`/`minimum` 在 VHDL-93 里不是预定义的，所以包改用私有包里的同名工具（`use work.en_cl_fix_private_pkg.all`，[L29](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L29)）；`to_string(FixRound_t)` 也不靠 `'image` 而是手写 `case`（见 [L700-714](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L700-L714)，注释明说「some synthesis tools do not support ... 'image」）。这些细节属 u5-l4 / u8-l2，这里只作背景。

#### 4.4.4 代码实践

**实践目标**：验证「不指定 `result_fmt` 时返回全精度」的契约。

**操作步骤**：

1. 阅读 `cl_fix_mult` 的包体（[L1214-L1255](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1214-L1255)）。
2. 找到 `mid_fmt_c := cl_fix_mult_fmt(a_fmt, b_fmt)` 与 `r_fmt_c := choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt)` 两行。
3. 推理：若调用 `cl_fix_mult(a, A, b, B)`（只给 4 个实参），`result_fmt` 取默认 `NullFixFormat_c`，故 `r_fmt_c = mid_fmt_c`，最后 `cl_fix_resize(mid, mid_fmt, mid_fmt, ...)` 退化为不损失精度的搬运。

**需要观察的现象**：当 `result_fmt` 缺省时，函数退化为「在 `mid_fmt` 下无损运算并原样返回」。

**预期结果**：与 u4-l1 讲的 Python 侧 `r_fmt=None ⇒ 无损` 完全对应——又一次「三语言镜像」。这是源码阅读型实践，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：能否把 `NullFixFormat_c` 定义成 `(0,0,0)`（位宽为 0 的合法空格式）？为什么不行？

**答案**：不行。`(0,0,0)` 位宽为 0，虽然边角但合法，可能与用户真正想用的「空格式」冲突，导致哨兵被误判成「用户指定」。选 `(0,0,-1)` 正是为了让位宽为负、绝不合法，从而保证唯一性。

**练习 2**：`cl_fix_shift` 也是数学函数，却**不**给 `result_fmt` 默认 `NullFixFormat_c`。结合 `cl_fix_shift` 的语义（无损移位后再 resize），猜猜为什么。

**答案**：移位改变了二进制小数点的位置，结果格式无法像 add/mult 那样从输入格式「自然」推出一个唯一的默认目标——调用者必须明确告诉它结果要落到什么格式（含 `shift` 已经改变过的 I/F）。因此 `result_fmt` 是必填项，没有哨兵默认。详见 u3-l2 的 `for_shift` 与 u5-l3 的 `cl_fix_shift` 实现。

## 5. 综合实践

把本讲内容串起来，完成一份**「en_cl_fix_pkg 包头 API 速查表」**：

1. **建表**：打开 [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) 的包头（L34–L266），按下表为每个公共函数填一行。

   | 函数 | 所属组 | 返回类型 | result_fmt 默认 | round 默认 | saturate 默认 |
   | --- | --- | --- | --- | --- | --- |

2. **分类核对**：确认五组的成员与 4.3.3 列出的一致；标出两个「破例」——`cl_fix_from_real`（`saturate=SatWarn_s`）与 `cl_fix_shift`（数学函数却无 `result_fmt` 默认）。

3. **哨兵追踪**：在包体里用搜索定位所有 `choose(result_fmt = NullFixFormat_c, ...)`，列出哪些函数用了这个模式（应有 6 个：abs/neg/add/sub/addsub/mult）。对每个，写出它对应的 `cl_fix_<op>_fmt` 是什么（如 `cl_fix_add` → `cl_fix_add_fmt`）。

4. **延迟推演**：任选一个 `cl_fix_recommended_pipelining` 重载，用自己的话写出它「何时返回 0、何时返回 1」，并对照 `en_cl_fix_round.vhd` 的 `use_reg_c` 说明 `Auto_s` 如何据此在 0 拍与 1 拍间切换。

5. **小结一句**：用一句话总结「为什么数学函数的 `result_fmt` 可以缺省、而 round/saturate/resize 不行」——把「高层 vs 原语」与 `NullFixFormat_c` 哨兵两条线索合并起来。

预期产出是一份可作为后续阅读包体（u5-l2、u5-l3）索引用的个人笔记。

## 6. 本讲小结

- en_cl_fix 的类型体系由四个类型构成：`FixFormat_t`（record）、`FixRound_t` / `FixSaturate_t` / `RegisterMode_t`（三个枚举），子类型约束（如 `S : natural range 0 to 1`）把非法值挡在编译期。
- `RegisterMode_t` 不参与数值计算，只控制流水线组件插不插寄存器；`Auto_s` 依据纯函数 `cl_fix_recommended_pipelining`（三个重载）的推荐值决定，`Yes_s`/`No_s` 分别恒为 1 拍/0 拍。
- 公共 API 按职责分五组：格式函数 / 字符串转换 / 类型转换 / 舍入饱和 / 数学函数；默认参数有三条通用约定（`round:=Trunc_s`、`saturate:=Warn_s`、`result_fmt:=NullFixFormat_c`）与两个破例（`cl_fix_from_real` 的 `SatWarn_s`、`cl_fix_shift` 无 `result_fmt` 默认）。
- `NullFixFormat_c = (0,0,-1)` 是位宽为 −1 的非法格式，充当「result_fmt 未指定」的哨兵；数学函数在体内用 `choose(result_fmt = NullFixFormat_c, mid_fmt, result_fmt)` 检测它，缺省时回退到全精度中间格式，从而返回无损结果——与 Python 侧的 `None` 语义镜像。
- 整个包对 VHDL-93 合规：`maximum`/`minimum` 走私有包、`to_string` 不依赖 `'image`，这些都是为兼容 VHDL-93 与各综合工具的工程化取舍（细节见 u5-l4、u8-l2）。

## 7. 下一步学习建议

本讲只读了**包头**。接下来建议：

- **u5-l2 核心转换实现**：进入包体，精读 `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 的实现，以及内部的 `convert`、`get_half`、`get_unit_bit`——你会看到本讲的 `result_fmt`、`round`、`saturate` 参数如何真正驱动位运算。
- **u5-l3 VHDL 数学函数**：精读 `cl_fix_add` / `cl_fix_mult` / `cl_fix_shift` 等，看「`mid_fmt` → 无损运算 → `cl_fix_resize`」三段式如何在 VHDL 里落地，以及为何统一用 `signed` 类型。
- **u5-l4 私有包与字符串解析**：深入 `en_cl_fix_private_pkg.vhd`，理解本讲多次提到的 `choose` / `maximum` / `minimum` / `to_string` 为何要手写而不依赖 VHDL 内置。
- **u6-l1 流水线组件**：把本讲的 `RegisterMode_t` 与 `cl_fix_recommended_pipelining` 放回组件语境，看 round/saturate/resize 三个实体如何共享同一套接口约定。

阅读时建议把本讲的「API 速查表」放在手边，遇到任何 `cl_fix_*` 调用先查它的分组与默认参数，再进包体看实现。
