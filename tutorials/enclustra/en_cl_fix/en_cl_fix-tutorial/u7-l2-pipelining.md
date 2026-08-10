# 推荐流水线与 RegisterMode

## 1. 本讲目标

本讲承接 [u7-l1 可综合 RTL 组件](u7-l1-rtl-components.md)，那里我们已经知道 `en_cl_fix_round`/`en_cl_fix_saturate`/`en_cl_fix_resize` 三个实体的统一骨架是「一行组合函数调用 + 由编译期常量 `use_reg_c` 控制的 `generate` 可选寄存器」。本讲要回答的是其中**最容易被一笔带过、却决定整个模块延迟与时序**的那一行：

> `cl_fix_recommended_pipelining` 到底在什么条件下返回 0、什么条件下返回 1？`reg_mode_g`（`Auto_s`/`Yes_s`/`No_s`）又是如何把它翻译成「到底插不插寄存器」？

学完本讲你应当能够：

1. 说出 `cl_fix_recommended_pipelining` 三个重载各自的「零逻辑（zero logic）」判据，并据此推断某一组格式/模式是否需要寄存器。
2. 给定任一 RTL 实体与一组 generic，准确算出 `Auto_s`/`Yes_s`/`No_s` 三档下的**流水线级数与延迟（latency，单位为时钟周期）**。
3. 解释 `resize` 的延迟为什么是「两级之和」，并看清 VHDL 函数 `cl_fix_recommended_pipelining(... round, saturate)` 与 `en_cl_fix_resize` 内部两级实体实例化之间**一一对应**的关系。
4. 说明 `meta_width_g` 旁路通道（sideband）的作用，以及它与数据通路、复位之间的关系。

## 2. 前置知识

本讲默认你已经掌握 u7-l1 的内容，即：

- 三个 RTL 实体的统一骨架：组合函数调用 + `use_reg_c` 控制的 `generate` 可选寄存器，算法逻辑全部复用 `en_cl_fix_pkg` 中的同名组合函数。
- `valid` 单向握手（只有 `in_valid`/`out_valid`，无 `ready`），`meta` 是与数据同拍到达、不参与运算的旁路。
- `resize = round → saturate`，顺序不可换。

此外需要两点背景直觉（来自更早的讲义）：

- **舍入（round）只在「减少小数位 F」时才真正干活**：除截断 `Trunc_s` 外，其余六种模式都是「先加一个偏移量、再向下取整」，会生成一个真正的加法器（见 [u4-l1](u4-l1-rounding.md)）。
- **饱和（saturate）只在「减少整数位 I 或符号位 S」且选择了饱和模式时才真正干活**：回绕（`None_s`/`Warn_s`）只是截断高位（纯连线），饱和（`Sat_s`/`SatWarn_s`）才需要比较器 + 多路选择器去钳到端点（见 [u4-l2](u4-l2-saturation.md)）。

这两点正是本讲「推荐流水线」判定的物理依据：**只有生成了真正的组合逻辑（加法器、比较器、选择器），才值得插一级寄存器去切断组合路径、改善时序；如果运算退化成纯连线（zero logic），插寄存器就只是白白增加延迟。**

> 名词解释：
> - **延迟（latency）**：数据从 `in_data` 到 `out_data` 经过的时钟周期数。纯组合通路延迟为 0；每插一级寄存器加 1。
> - **流水线级数（pipeline stages）**：数据路径上寄存器的级数，数值上等于延迟（本库每一级寄存器恰好对应一拍）。
> - **零逻辑（zero logic）**：运算在综合后不产生任何实质逻辑门，只表现为连线（截断、补零、符号扩展）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | 定义 `RegisterMode_t` 枚举与 `cl_fix_recommended_pipelining` 的三个重载（判定逻辑全部在此）。 |
| [hdl/en_cl_fix_round.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd) | 舍入 RTL 实体：用 `use_reg_c` 把 `reg_mode_g` 翻译成「是否寄存一拍」。 |
| [hdl/en_cl_fix_saturate.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd) | 饱和 RTL 实体：结构与 round 完全同构，判据不同。 |
| [hdl/en_cl_fix_resize.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd) | resize 实体：不重写算法，而是例化 round + saturate 两级，延迟为两级之和。 |
| [tb/cl_fix_resize_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd) | resize 仿真：用 `i mod reg_mode_count_c` 在三档 `reg_mode` 间轮换，验证寄存器只改延迟、不改数据。 |

## 4. 核心概念与源码讲解

### 4.1 `cl_fix_recommended_pipelining` 三个重载：判定「零逻辑」

#### 4.1.1 概念说明

`cl_fix_recommended_pipelining` 回答一个纯静态的问题：**给定输入格式、输出格式与（舍入/饱和）模式，这一步运算在综合后会不会生成「值得用寄存器切断」的实质组合逻辑？**

- 返回 `0`：运算退化为连线（零逻辑），不需要寄存器。
- 返回 `1`：运算生成了加法器或比较器/选择器，**推荐**插一级寄存器以改善时序。

注意它是「推荐」而非「强制」：真正是否插寄存器还由 `reg_mode_g` 决定（见 4.2）。`Auto_s` 档正是「听推荐的」；`Yes_s` 是「无论推荐与否都插」；`No_s` 是「无论推荐与否都不插」。

它有三个同名重载，分别对应 round、saturate、resize 三种运算：

[hdl/en_cl_fix_pkg.vhd:164-185](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L164-L185) 声明了这三个重载（注释里写明各自对应哪个运算）。

#### 4.1.2 核心流程

**round 重载**的判定可以用下面伪代码概括（返回值即「推荐寄存器数」）：

```
recommended_round(a_fmt, result_fmt, round):
    若 fmt_check: 断言 result_fmt == cl_fix_round_fmt(a_fmt, result_fmt.F, round)
    若 round == Trunc_s:           return 0     # 截断 = 纯连线
    若 result_fmt.F >= a_fmt.F:    return 0     # 没有减少小数位 = 没有舍入发生
    否则:                          return 1     # 真正的舍入加法器
```

**saturate 重载**的判定：

```
recommended_saturate(a_fmt, result_fmt, saturate):
    断言 result_fmt.F == a_fmt.F                # 饱和不允许改变小数位
    若 saturate ∈ {None_s, Warn_s}: return 0    # 回绕 = 纯连线（Warn 只是仿真期断言）
    若 result_fmt.I >= a_fmt.I 且 result_fmt.S == a_fmt.S:
                                    return 0    # 没有减少整数位、也没动符号位 = 永远不越界
    否则:                          return 1     # 真正的比较器 + 钳位选择器
```

**resize 重载**不是重新判定，而是把 resize 拆成 round → saturate 两段，把上面两个重载的结果**相加**：

```
recommended_resize(a_fmt, result_fmt, round, saturate):
    round_fmt = cl_fix_round_fmt(a_fmt, result_fmt.F, round)
    return recommended_round(a_fmt, round_fmt, round)
         + recommended_saturate(round_fmt, result_fmt, saturate)
```

因此 resize 的推荐寄存器数 ∈ {0, 1, 2}。

#### 4.1.3 源码精读

**round 重载的函数体**——两条「零逻辑」分支分别对应截断与「未减少小数位」：

[hdl/en_cl_fix_pkg.vhd:1043-1071](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1043-L1071)

```vhdl
-- (1) During truncation.
if round = Trunc_s then
    return 0;
...
-- (2) If the number of fractional bits isn't being decreased.
if result_fmt.F >= a_fmt.F then
    return 0;
end if;
return 1;
```

这与组合函数 `cl_fix_round` 的实现严格呼应：偏移加法被包在 `if result_fmt.F < a_fmt.F then ... case round ...` 里，`Trunc_s` 分支是 `null`（见 [hdl/en_cl_fix_pkg.vhd:950-972](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L950-L972)）。两个条件任一成立，综合后都只剩 `convert`（对齐/符号扩展，纯连线），故无需寄存器。

**saturate 重载的函数体**——两条「零逻辑」分支分别对应回绕与「未减少整数位/符号位」：

[hdl/en_cl_fix_pkg.vhd:1073-1099](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1073-L1099)

```vhdl
-- (1) During wrapping.
if saturate = None_s or saturate = Warn_s then
    return 0;
...
-- (2) If the number of integer bits is not being decreased, and the number of sign bits is
--     not being changed.
if result_fmt.I >= a_fmt.I and result_fmt.S = a_fmt.S then
    return 0;
end if;
return 1;
```

物理依据：`cl_fix_saturate` 在回绕模式下只调用 `convert`（截高位=取模，纯连线）；而即便选了饱和模式，只要整数位和符号位都没减少，值就永远落在范围内，那两个 `cl_fix_compare` 比较器会被综合工具优化掉，钳位选择器退化为常通——同样是零逻辑。

**resize 重载的函数体**——拆分 + 求和：

[hdl/en_cl_fix_pkg.vhd:1101-1111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1101-L1111)

```vhdl
constant round_fmt_c : FixFormat_t := cl_fix_round_fmt(a_fmt, result_fmt.F, round);
begin
    return cl_fix_recommended_pipelining(a_fmt, round_fmt_c, round)
         + cl_fix_recommended_pipelining(round_fmt_c, result_fmt, saturate);
```

注意它用 `cl_fix_round_fmt` 算出中间格式 `round_fmt_c`，其 `F` 恒等于 `result_fmt.F`（见 [hdl/en_cl_fix_pkg.vhd:608-628](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L608-L628)），所以把它喂给 saturate 重载时，`result_fmt.F == a_fmt.F` 的断言自动满足。这个 `round_fmt_c` 不是凭空构造的——它正是 `en_cl_fix_resize` 实体里真正例化的两级之间的接口格式（见 4.3）。

#### 4.1.4 代码实践（阅读型）

阅读 [hdl/en_cl_fix_pkg.vhd:1056-1065](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1056-L1065) 与 [hdl/en_cl_fix_pkg.vhd:1083-1092](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1083-L1092) 两段 `else` 分支里的 `assert`。

- 目标：理解库如何防御「未来新增舍入/饱和模式」。
- 步骤：对照 `FixRound_t`/`FixSaturate_t` 枚举（[hdl/en_cl_fix_pkg.vhd:49-66](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L49-L66)），确认 `assert` 列出了除已处理项之外的全部合法值。
- 观察现象：一旦有人给枚举新增一个值却忘了在 `recommended_pipelining` 里处理，仿真会立即以 `Failure` 报错，而不是静默返回错误的寄存器数。
- 预期结果：你能解释这两条 `assert` 是「穷举式哨兵」，保证判定逻辑与枚举定义不脱节。

#### 4.1.5 小练习与答案

**练习 1**：对 `a_fmt = [1,4,8]`、`result_fmt = [1,4,8]`、`round = Trunc_s`，`recommended_round` 返回什么？为什么？

**答案**：返回 `0`。两个零逻辑条件都成立——既是 `Trunc_s`（截断），又没有减少小数位（`8 >= 8`）。运算退化为同名格式间的 `convert`（纯连线），无需寄存器。

**练习 2**：对 `a_fmt = [0,4,4]`、`result_fmt = [0,2,4]`、`saturate = Sat_s`，`recommended_saturate` 返回什么？

**答案**：返回 `1`。不是回绕（`Sat_s`），且整数位被减少（`2 < 4`），所以会生成比较器 + 钳位选择器，推荐一级寄存器。

---

### 4.2 RegisterMode 与 `use_reg_c`：寄存器插入的控制逻辑

#### 4.2.1 概念说明

`RegisterMode_t` 是一个三值枚举，决定 RTL 实体如何对待「推荐」：

[hdl/en_cl_fix_pkg.vhd:68-73](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L68-L73)

```vhdl
type RegisterMode_t is
(
    Auto_s,   -- Inserts the recommended registering. See cl_fix_recommended_pipelining.
    Yes_s,    -- Inserts all registering. Can be useful for consistent latency.
    No_s      -- Inserts no registering. Use with caution (poor timing performance).
);
```

三档语义：

| `reg_mode_g` | 含义 | 是否寄存 |
| --- | --- | --- |
| `Auto_s` | 听推荐的——推荐几级就插几级 | 当且仅当 `recommended > 0` |
| `Yes_s` | 永远插——延迟恒定，便于对齐 | 永远插 |
| `No_s` | 永远不插——纯组合，时序差 | 永远不插 |

`Auto_s` 的延迟会随其它 generic（格式、模式）变化；`Yes_s`/`No_s` 的延迟与 generic 无关、是常数。所以**当你需要固定延迟（比如多个数据通道要对齐，或下游按节拍接收），用 `Yes_s`；当你追求最少延迟且能接受延迟随配置浮动，用 `Auto_s`；`No_s` 几乎只用于对延迟极其敏感、且组合路径本来就极短的场景。** 每个 RTL 实体的注释都写了「If unsure, set to Yes_s」。

#### 4.2.2 核心流程

三个 RTL 实体用**完全相同的一行**把 `reg_mode_g` + `recommended` 折叠成一个布尔常量 `use_reg_c`，再用它选择两段互斥的 `generate`：

```
recommended_c = cl_fix_recommended_pipelining(...)        # 0 或 1（resize 实体里仍是单级视角）
use_reg_c     = (reg_mode_g == Yes_s)
              or (reg_mode_g == Auto_s and recommended_c > 0)

若 use_reg_c:  生成「时钟进程寄存 out_valid/out_meta/out_data」
否则:          生成「组合直通 out_valid/out_meta/out_data」
```

关键：`use_reg_c` 是编译期常量（依赖的全是 generic），所以综合后**只会保留两段 `generate` 中的一段**，另一段被完全剔除——不存在「运行时选择」的面积浪费。

由此可列出每个实体三档下的延迟表（注释里写得很明确，见 4.2.3）：

| 实体 | `No_s` | `Yes_s` | `Auto_s` |
| --- | --- | --- | --- |
| `en_cl_fix_round` | 0 | 1 | `recommended_round` ∈ {0,1} |
| `en_cl_fix_saturate` | 0 | 1 | `recommended_saturate` ∈ {0,1} |
| `en_cl_fix_resize` | 0 | 2 | `recommended_round + recommended_saturate` ∈ {0,1,2} |

`resize` 的 `Yes_s` 是 2 而非 1，是因为它内部串了两级实体、`reg_mode_g` 被原样传给两级（见 4.3）。

#### 4.2.3 源码精读

**round 实体的控制逻辑**：

[hdl/en_cl_fix_round.vhd:85-86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L85-L86) 把 `reg_mode_g` 与推荐值折叠成 `use_reg_c`：

```vhdl
constant recommended_c : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, fmt_check_g);
constant use_reg_c     : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
```

随后 [hdl/en_cl_fix_round.vhd:95-111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L95-L111) 用 `use_reg_c` 二选一：寄存路径是一个 `rising_edge(clk)` 进程，把 `result`（即组合函数 `cl_fix_round(...)` 的输出，见 [hdl/en_cl_fix_round.vhd:92](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L92)）打一拍；非寄存路径是简单连续赋值。

注意寄存路径里复位只对 `out_valid` 生效：`out_valid <= in_valid and not rst;`（[hdl/en_cl_fix_round.vhd:99](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L99)）。`out_data`/`out_meta` 不复位——它们在 `out_valid` 为低时本就是「无关项（don't care）」。

实体头部的注释给出了延迟的权威定义，[hdl/en_cl_fix_round.vhd:27-35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L27-L35)：

```
-- Auto_s: Latency = cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g).
-- Yes_s : Latency = 1.
-- No_s  : Latency = 0.
```

**saturate 实体结构完全同构**，只是把推荐函数换成 saturate 重载、判据换成饱和的「零逻辑」条件：

[hdl/en_cl_fix_saturate.vhd:84-85](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L84-L85) 是同样的 `recommended_c`/`use_reg_c` 两行；[hdl/en_cl_fix_saturate.vhd:94-110](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L94-L110) 是同样的两段 `generate`。延迟语义见 [hdl/en_cl_fix_saturate.vhd:27-35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L27-L35)（`Yes_s`→1、`No_s`→0、`Auto_s`→`cl_fix_recommended_pipelining(..., saturate_g)`）。

**`meta_width_g` 旁路通道**。三个实体的 generic 里都有 `meta_width_g : natural := 0`，端口里都有 `in_meta`/`out_meta : std_logic_vector(meta_width_g-1 downto 0)`（见 [hdl/en_cl_fix_round.vhd:56](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L56) 与 [hdl/en_cl_fix_round.vhd:69](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L69)、[hdl/en_cl_fix_round.vhd:75](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L75)）。它的作用：

- 携带**与数据同拍到达、但不参与运算**的旁路信息（sideband），例如「包尾 last 标志」「通道号」「用户自定义比特」。
- 它必须与数据**同步前进**：数据寄存一拍时，meta 也寄存一拍；数据直通时，meta 也直通（见 [hdl/en_cl_fix_round.vhd:100](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L100) 与 [hdl/en_cl_fix_round.vhd:109](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L109)）。这样无论插不插寄存器，meta 始终与 `out_data` 对齐。
- `meta_width_g = 0` 时向量范围是 `(-1 downto 0)`，即空向量，旁路退化为一根线都没有；`in_meta` 给了默认值 `(others => 'X')` 表示「不接也合法」。
- `meta_width_g` **不影响延迟，也不进入 `cl_fix_round`/`cl_fix_saturate` 计算**——它只是把旁路寄存器/直通线按位展宽。换句话说，它是「搭便车」的并行通道。

#### 4.2.4 代码实践（配置对比型）

不用跑仿真，仅靠查表回答下列三组 round 实体配置在 `Auto_s` 下的延迟，再用源码核对你的推理：

1. `in=[1,4,8] → out=[1,4,4]`，`round = Trunc_s`
2. `in=[1,4,8] → out=[1,5,8]`，`round = NonSymPos_s`
3. `in=[1,4,8] → out=[1,5,4]`，`round = NonSymPos_s`

- 目标：内化「零逻辑」两条件。
- 步骤：对每组，分别判断「是否截断」「是否减少了小数位」，套用 4.1.2 的伪代码。
- 预期结果：① `Trunc_s` → 0；② `NonSymPos_s` 但 `out.F(8) >= in.F(8)`，未减少小数位 → 0；③ `NonSymPos_s` 且 `out.F(4) < in.F(8)` → 1。
- 观察：只有第 3 组会真正生成舍入加法器，因此只有它在 `Auto_s` 下延迟为 1。

#### 4.2.5 小练习与答案

**练习 1**：同一个 round 实体，`reg_mode_g` 从 `Auto_s` 改成 `Yes_s`，`out_data` 的**数值**会变吗？延迟会变吗？

**答案**：数值不变，延迟可能变。寄存器只是把同一个组合结果 `cl_fix_round(...)` 延迟若干拍输出，位级结果完全相同；`Yes_s` 强制寄存一拍，所以原本 `Auto_s` 返回 0（不寄存）的零逻辑情形，延迟会从 0 变成 1。

**练习 2**：为什么 `out_data` 和 `out_meta` 不参与复位，只有 `out_valid` 用 `and not rst` 拉低？

**答案**：`out_valid` 是下游判断「当前拍数据是否有效」的唯一标志，复位后必须为 0 以防下游采到无效数据；而 `out_data`/`out_meta` 在 `out_valid=0` 时是 don't care，复位它们只会白白增加寄存器宽度、浪费面积，对功能毫无帮助。

---

### 4.3 resize 的延迟求和：函数与结构的一一对应

#### 4.3.1 概念说明

`en_cl_fix_resize` 不重写任何算法，而是**例化** `en_cl_fix_round` 与 `en_cl_fix_saturate` 两个子实体，把它们串成两级流水线。这一点在 u7-l1 已点出，本讲要把它与「延迟」彻底打通：

- 函数侧 `cl_fix_recommended_pipelining(... round, saturate)` 把 resize 拆成 round 段 + saturate 段并**求和**（4.1）。
- 结构侧 `en_cl_fix_resize` 把 resize 拆成 `i_round` + `i_saturate` 两个实例并**串联**。
- 关键之处：`reg_mode_g` 被**原样传给两个子实例**，于是每一级各自独立地用 `use_reg_c` 决定寄不寄存。

这意味着 resize 的 `Auto_s` 延迟，是两个**独立**的 0/1 决策之和，能取到 0、1、2 三种值——这是 round/saturate 单实体（最多 1）做不到的。

#### 4.3.2 核心流程

```
                ┌─────────── en_cl_fix_resize ───────────┐
in_data ──────► │ ┌─────────┐  round_fmt_c  ┌──────────┐ │ ──────► out_data
in_valid ─────► │ │ i_round │ ────────────► │ i_saturate│ │ ──────► out_valid
in_meta  ─────► │ └─────────┘               └──────────┘ │ ──────► out_meta
                │   reg_mode_g => reg_mode_g (两级都是)    │
                └────────────────────────────────────────┘
```

两级之间的接口格式是常量 `round_fmt_c = cl_fix_round_fmt(in_fmt_g, out_fmt_g.F, round_g)`——与函数侧 `cl_fix_recommended_pipelining` 里用的那个 `round_fmt_c` **完全相同**。所以：

\[ \text{latency}_{\text{resize}}(\text{Auto}) = \text{latency}_{\text{round}}(\text{Auto}) + \text{latency}_{\text{saturate}}(\text{Auto}) \]

而 `Yes_s` 让两级都强制寄存 → 2；`No_s` 让两级都不寄存 → 0。

> 一个常被忽略的细节：因为 `reg_mode_g` 是同一个值传两级，**不存在「round 级用 Yes、saturate 级用 No」这种混合**。若你确实需要混合，应当在外部直接例化 `en_cl_fix_round` + `en_cl_fix_saturate` 自行串联（这正是 `en_cl_fix_resize` 内部做的事，参考 4.3.3）。

#### 4.3.3 源码精读

**中间格式常量**与 4.1 函数体里的同名常量一致：

[hdl/en_cl_fix_resize.vhd:85](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L85)

```vhdl
constant round_fmt_c : FixFormat_t := cl_fix_round_fmt(in_fmt_g, out_fmt_g.F, round_g);
```

**i_round 实例**：输入格式 `in_fmt_g`、输出格式 `round_fmt_c`，`reg_mode_g` 原样透传：

[hdl/en_cl_fix_resize.vhd:96-116](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L96-L116)（注意 [L101](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L101) 的 `reg_mode_g => reg_mode_g`）。

**i_saturate 实例**：输入格式 `round_fmt_c`、输出格式 `out_fmt_g`，同样透传 `reg_mode_g`：

[hdl/en_cl_fix_resize.vhd:121-141](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L121-L141)（注意 [L126](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L126) 的 `reg_mode_g => reg_mode_g`）。

两级之间的内部信号 `round_valid`/`round_meta`/`round_data` 宽度按 `round_fmt_c` 声明（[hdl/en_cl_fix_resize.vhd:87-89](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L87-L89)）。

实体头部注释给出 resize 的延迟权威定义，[hdl/en_cl_fix_resize.vhd:27-35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L27-L35)：

```
-- Auto_s: Latency = cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, saturate_g).
-- Yes_s : Latency = 2.
-- No_s  : Latency = 0.
```

**仿真侧的证据**：仓库自己的 testbench 就在三档 `reg_mode` 间轮换，且仍用同一份黄金参考比对数据——这直接证明了「reg_mode 只改延迟、不改数值」。[hdl/.../tb/cl_fix_resize_tb.vhd:67](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L67) 定义 `reg_mode_count_c := 1 + RegisterMode_t'pos(RegisterMode_t'high)`（即枚举值的个数 3），[tb/cl_fix_resize_tb.vhd:159](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L159) 用 `reg_mode_g => RegisterMode_t'val(i mod reg_mode_count_c)` 让不同测试用例分别落到 `Auto_s`/`Yes_s`/`No_s`。

#### 4.3.4 代码实践（推理型）

下面三组 resize 配置，分别推出 `Auto_s` 下的延迟，验证 resize 能取到 0/1/2 全部三档。设 `in=[1,4,8]`、`round=NonSymPos_s`：

| 组 | `out_fmt` | `saturate` | `round_fmt_c` | round 段 | saturate 段 | `Auto` 延迟 |
| --- | --- | --- | --- | --- | --- | --- |
| A | `[1,4,8]` | `None_s` | `[1,4,8]` | 0（未减 F） | 0（回绕） | **0** |
| B | `[1,5,4]` | `SatWarn_s` | `[1,5,4]` | 1（减 F，非截断） | 0（I 未减、S 不变） | **1** |
| C | `[1,2,4]` | `SatWarn_s` | `[1,5,4]` | 1（减 F） | 1（I 由 5 减到 2） | **2** |

- 目标：亲手算一遍 `recommended_resize = recommended_round + recommended_saturate`。
- 步骤：先算 `round_fmt_c = cl_fix_round_fmt([1,4,8], out.F, NonSymPos_s)`——注意 `out.F<8` 且非截断时整数位 +1；再分别套 4.1.2 的两条判据。
- 预期结果：A/B/C 的 `Auto` 延迟分别为 0、1、2，对应 0/1/2 级流水线。`Yes_s` 三组都是 2，`No_s` 三组都是 0。
- 若无法确定：标注「待本地验证」，再用 4.3.5 的方法核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 resize 的 `Yes_s` 延迟是 2，而不是 1？

**答案**：因为 `en_cl_fix_resize` 把 `reg_mode_g` 原样传给内部串联的 `i_round` 和 `i_saturate` 两个实体。`Yes_s` 对每个实体都意味着「强制寄存一拍」，两级串联 → 1 + 1 = 2 拍。

**练习 2**：若你希望 round 级寄存、saturate 级不寄存（混合），用 `en_cl_fix_resize` 能做到吗？该怎么做？

**答案**：做不到。`en_cl_fix_resize` 只暴露一个 `reg_mode_g`，两级共用。要混合，应在外部自行例化 `en_cl_fix_round`（`reg_mode_g => Yes_s`）与 `en_cl_fix_saturate`（`reg_mode_g => No_s`）并按 `round_fmt_c` 串联——即手动复刻 `en_cl_fix_resize` 的结构（4.3.3），但给两级不同的 `reg_mode_g`。

---

## 5. 综合实践

**任务**：对同一个 resize，分别设 `reg_mode = Auto_s / Yes_s / No_s`，报告三种配置下的流水线级数与延迟，并解释为什么三者产出的 `out_data` 数值完全一致。

选用 4.3.4 的 **B 组**配置（它在三档下给出三种不同延迟，最有区分度）：

```
in_fmt   = [1,4,8]        -- 有符号，13 位
out_fmt  = [1,5,4]        -- 有符号，10 位
round    = NonSymPos_s    -- 四舍五入（平局向上）
saturate = SatWarn_s      -- 饱和并告警
```

### 步骤 1：手算三档延迟

1. 算 `round_fmt_c = cl_fix_round_fmt([1,4,8], 4, NonSymPos_s)`。因为目标小数位 `4 < 8` 且非截断，整数位 +1 → `[1,5,4]`。
2. round 段：`out.F(4) < in.F(8)` 且非截断 → `recommended_round = 1`。
3. saturate 段：`round_fmt_c=[1,5,4] → out=[1,5,4]`，整数位未减（`5>=5`）、符号位不变 → `recommended_saturate = 0`。
4. 因此：

   | `reg_mode_g` | 流水线级数 | 延迟（周期） |
   | --- | --- | --- |
   | `Auto_s` | 1（仅 round 级寄存） | 1 |
   | `Yes_s` | 2（两级都寄存） | 2 |
   | `No_s` | 0（都不寄存） | 0 |

### 步骤 2：用 VHDL 打印推荐值核对（示例代码）

下面是一段**示例代码**（非仓库既有文件），可在任意 VHDL 仿真器里作为顶层跑，把推荐寄存器数 `report` 出来，用于核对你步骤 1 的手算：

```vhdl
-- 示例代码：打印 recommended_pipelining，核对步骤 1
library ieee;
    use ieee.std_logic_1164.all;
library work;
    use work.en_cl_fix_pkg.all;

entity probe_pipelining is
end entity;

architecture sim of probe_pipelining is
    constant in_fmt_c   : FixFormat_t := (1, 4, 8);
    constant out_fmt_c  : FixFormat_t := (1, 5, 4);
    constant rec_c      : natural :=
        cl_fix_recommended_pipelining(in_fmt_c, out_fmt_c, NonSymPos_s, SatWarn_s);
begin
    process
    begin
        report "resize recommended_pipelining (Auto latency) = "
            & integer'image(rec_c) severity note;
        wait;
    end process;
end architecture;
```

- 预期结果：打印 `... = 1`，与步骤 1 的 `Auto` 延迟一致。
- `Yes_s`/`No_s` 的延迟无需打印，由定义直接给出：分别为 2 与 0。
- 若本地无仿真器：标注「待本地验证」，步骤 1 的纯手算结论已足够。

### 步骤 3：解释「数值不变」

阅读 [tb/cl_fix_resize_tb.vhd:159](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L159)，确认仿真会令不同用例分别落到三档 `reg_mode`，却仍用同一份 `bittrue/cosim/cl_fix_resize/data/` 下的黄金参考做比对（数据由 cosim 的 Python 模型生成，见 [u8-l1](u8-l1-cosim-flow.md)）。请用一句话解释为何这能通过：

> 因为 `reg_mode_g` 只控制「是否给组合结果打拍」，组合结果 `cl_fix_round(...)`/`cl_fix_saturate(...)` 本身与 `reg_mode_g` 无关；插寄存器只改变 `out_data` 出现的时刻，不改变其位级取值。所以三档产出在各自延迟后逐位一致，同一份黄金参考可同时校验三档。

### 步骤 4（可选，需 VUnit 环境，待本地验证）

若已按 [u1-l4](u1-l4-quick-start-tests.md) 装好 `vunit-hdl`，可运行 resize 仿真，观察三档延迟：

```bash
python sim/run.py --simulator=ghdl "*cl_fix_resize_tb*"
```

- 观察现象：因为 testbench 按 `i mod reg_mode_count_c` 轮换三档，不同用例的 `out_valid` 相对 `in_valid` 的滞后周期数会不同（0/1/2），但所有用例的 `out_data` 比对都应通过。
- 预期结果：仿真零失败，证明「reg_mode 改延迟不改数据」。
- 若无 GHDL/ModelSim：跳过本步，标注「待本地验证」即可，前 3 步已完整覆盖本讲目标。

## 6. 本讲小结

- `cl_fix_recommended_pipelining` 是纯静态判定：运算在综合后若退化为**零逻辑（纯连线）**就返回 0、否则返回 1。round 的零逻辑条件是「截断」或「未减少小数位」；saturate 的是「回绕」或「未减少整数位且符号位不变」。
- `RegisterMode_t` 三档把「推荐」翻译成动作：`Auto_s` 听推荐的、`Yes_s` 永远插、`No_s` 永远不插。折叠公式 `use_reg_c = (Yes_s) or (Auto_s and recommended>0)` 是编译期常量，综合后只保留两段 `generate` 之一。
- 三个实体的延迟表：round/saturate 在 `No/Yes/Auto` 下为 `0 / 1 / {0,1}`；resize 为 `0 / 2 / {0,1,2}`。
- resize 的延迟是「两级之和」：函数侧 `cl_fix_recommended_pipelining(... round, saturate)` 拆成 round 段 + saturate 段求和，结构侧 `en_cl_fix_resize` 例化 `i_round` + `i_saturate` 串联并把 `reg_mode_g` 原样传两级——函数的「和」即硬件的「串」。
- `meta_width_g` 是与数据同拍前进、不参与运算的旁路通道：数据寄存它就寄存、数据直通它就直通，宽度只决定旁路位数、不影响延迟。
- 关键心法：**`reg_mode_g` 只改延迟、不改数值**——仓库 testbench 正是靠在三档间轮换并用同一份黄金参考比对来验证这一点。

## 7. 下一步学习建议

- 本讲只讲了 round/saturate/resize 三个「现成」RTL 实体的流水线。要理解**算术运算**（add/mult/...）如何被包成可综合 RTL，可关注这些组合函数本身（见 [u5-l2 算术运算链路](u5-l2-arithmetic-pipeline.md)），并思考：若你要给一个 `cl_fix_mult` 套上寄存器，应该参考本讲的哪一档 `reg_mode` 策略？
- 要真正「跑起来」观察延迟（综合实践步骤 4），需要进入验证体系：先读 [u8-l1 cosim 验证流程总览](u8-l1-cosim-flow.md) 理解黄金参考如何由 Python 生成，再读 [u8-l2 VUnit 仿真框架与 cosim_runner](u8-l2-vunit-runner.md) 掌握 `sim/run.py` 如何驱动 testbench。
- 想验证你对「零逻辑」判据的理解，可直接精读 [hdl/en_cl_fix_pkg.vhd:1043-1111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1043-L1111) 的三个函数体，并对照 [u4-l1 舍入](u4-l1-rounding.md) 与 [u4-l2 饱和](u4-l2-saturation.md) 的组合实现，确认「判定函数返回 1」与「组合函数生成实质逻辑」逐一对应。
