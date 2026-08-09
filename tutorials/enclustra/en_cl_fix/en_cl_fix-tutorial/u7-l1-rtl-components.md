# 可综合 RTL 组件：round / saturate / resize

## 1. 本讲目标

本讲从「库函数」跨入「可综合硬件」。读完本讲你应该能够：

1. 说出 `en_cl_fix_round`、`en_cl_fix_saturate`、`en_cl_fix_resize` 三个 RTL 实体的相同骨架——「组合函数调用 + `generate` 可选寄存器」。
2. 解释 `reg_mode_g`（`Auto_s`/`Yes_s`/`No_s`）如何借助 `cl_fix_recommended_pipelining` 决定是否插入寄存器、以及由此带来的延迟（0 / 1 / 2 拍）。
3. 读懂 `en_cl_fix_resize` 内部如何把 round 与 saturate **串联**成一个两级流水线，中间用 `round_fmt_c` 衔接。
4. 理解 `in_valid`/`out_valid` 握手与 `in_meta`/`out_meta` 旁路（sideband）端口的约定，并能据此推断延迟。

本讲只讲 **RTL 实体（hdl/*.vhd）本身的结构**，不重复舍入/饱和的算法细节——那些已在 u4-l1/u4-l2/u4-l3 讲透。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **`[S, I, F]` 格式与 `FixFormat_t`**（u1-l2、u2-l3）：S 符号位、I 整数位、F 小数位，总位宽 `S+I+F`。
- **`cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 三个库函数**（u4-l1、u4-l2、u4-l3）：round 处理「F 变少」、saturate 处理「I/S 变少」、resize = 先 round 后 saturate。
- **VHDL-93 可综合 RTL** 与 **VHDL-2008 testbench** 的区分（u1-l3）：本讲的三个实体属于可综合 RTL，编译时用 `vhdl_standard_rtl`。

补充两个本讲会用到的 VHDL 概念：

- **`generate` 语句**：编译期根据常量条件「生成或删除」一段硬件。本讲用它来在「插寄存器」与「不插寄存器」两条结构里二选一，综合后只保留一条路径。
- **`generic`（类属）**：例化时传入的编译期参数。本讲的 `in_fmt_g`、`reg_mode_g` 等都是 generic，意味着位宽与流水线结构在综合时就已固定。

一个贯穿全讲的关键直觉：**这三个实体都是「把已有的纯函数包进一层可选寄存器」**。算法逻辑一行没变，全部复用 `en_cl_fix_pkg` 里的同名函数；实体层只解决「要不要寄存一拍」和「valid/meta 怎么跟着走」两件事。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [hdl/en_cl_fix_round.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd) | 可综合 RTL：流水线化舍入 | 组合核 + generate 可选寄存器（本讲的「模板」） |
| [hdl/en_cl_fix_saturate.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd) | 可综合 RTL：流水线化饱和 | 与 round 同构，仅函数与判定常量不同 |
| [hdl/en_cl_fix_resize.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd) | 可综合 RTL：流水线化 resize | 串联 `i_round` → `i_saturate` 两级 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | VHDL 包：类型与函数 | `RegisterMode_t`、`cl_fix_recommended_pipelining` 三个重载 |

参考（实践用）：

| 文件 | 角色 |
|---|---|
| [tb/cl_fix_resize_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd) | 例化 `en_cl_fix_resize` 的完整 testbench，对拍 cosim 黄金参考 |
| [sim/run.py](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py) | VUnit 仿真驾驶舱 |

## 4. 核心概念与源码讲解

### 4.1 en_cl_fix_round 实体：组合核 + 可选寄存器

#### 4.1.1 概念说明

`en_cl_fix_round` 是三个 RTL 实体里最值得精读的一个，因为它确立了一个被 saturate 完全复用的骨架：

> **一行组合运算 + 一个由常量决定开/关的寄存器。**

它的工作只有一件——把库函数 `cl_fix_round(a, a_fmt, result_fmt, round)`（u4-l1 讲过的「加偏移再 floor」）变成一个带时钟的模块。是否寄存一拍，由 generic `reg_mode_g` 决定：

- `Auto_s`：只在「确实需要」时才插寄存器。是否需要由 `cl_fix_recommended_pipelining` 判定。
- `Yes_s`：永远插一拍。延迟恒为 1，便于在系统级固定时序。
- `No_s`：永远不插。延迟为 0，但通常劣化时序（组合路径变长），慎用。

为什么 `Auto_s` 是默认的好选择？因为舍入并不总是需要寄存器——当舍入实际上**不产生任何逻辑**时（例如纯截断 `Trunc_s`、或小数位根本没减少），插寄存器纯属浪费。`cl_fix_recommended_pipelining` 就是用来识别这种「零逻辑」情形的。

#### 4.1.2 核心流程

实体内部只有三步：

1. 用常量 `recommended_c` 调用 `cl_fix_recommended_pipelining`，得到「建议寄存器级数」（0 或 1）。
2. 用常量 `use_reg_c` 把 `reg_mode_g` 与 `recommended_c` 合成最终的「是否插寄存器」布尔值：

   \[
   \text{use\_reg} = (\text{reg\_mode} = \text{Yes\_s}) \;\lor\; \big(\text{reg\_mode} = \text{Auto\_s} \;\land\; \text{recommended} > 0\big)
   \]

3. 一条并发赋值算出组合结果 `result`；再用两个互斥的 `generate` 分支——`g_register`（寄存）或 `g_no_register`（直通）——把 `result`、`in_valid`、`in_meta` 三者一起送出。

`cl_fix_recommended_pipelining`（round 重载）的判定规则（见源码精读）可概括为：

\[
\text{recommended}_{\text{round}} = \begin{cases} 0 & \text{round} = \text{Trunc\_s} \\ 0 & \text{out\_fmt}.F \geq \text{in\_fmt}.F \\ 1 & \text{否则} \end{cases}
\]

即「截断」或「没有减少小数位」时不产生进位逻辑，无需寄存；其余舍入模式在减少小数位时会产生一个加法进位链，建议寄存一拍切断关键路径。

#### 4.1.3 源码精读

实体端口（[hdl/en_cl_fix_round.vhd:50-78](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L50-L78)）：generic 锁定 `in_fmt_g`/`out_fmt_g`/`round_g`/`reg_mode_g`，外加 `meta_width_g`（旁路数据宽度，默认 0 即不用）和 `fmt_check_g`（默认开）。端口分三组——时钟复位、输入（`in_valid`/`in_meta`/`in_data`）、输出（`out_valid`/`out_meta`/`out_data`）。注意 `in_data`/`out_data` 的位宽直接由 `cl_fix_width(in_fmt_g)`/`cl_fix_width(out_fmt_g)` 在编译期求出。

两个决定结构的常量（[hdl/en_cl_fix_round.vhd:85-86](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L85-L86)）：

```vhdl
constant recommended_c  : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, fmt_check_g);
constant use_reg_c      : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
```

这正是上面公式 ② 的直接翻译。`recommended_c` 的类型标注 `range 0 to 1` 也明示了它只可能是 0 或 1。

组合核只有一行（[hdl/en_cl_fix_round.vhd:92](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L92)）：

```vhdl
result <= cl_fix_round(in_data, in_fmt_g, out_fmt_g, round_g, fmt_check_g);
```

——算法逻辑全部委托给库函数，实体不重写任何舍入。

两条互斥的 `generate`（[hdl/en_cl_fix_round.vhd:95-111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L95-L111)）：`g_register` 在时钟上升沿把 `result`、`in_meta` 打一拍，并把 `out_valid` 写成 `in_valid and not rst`（复位时强制拉低）；`g_no_register` 则三者全部直通。综合时只有命中的那一条会被保留。

`recommended_c` 背后的判定函数体在包里（[hdl/en_cl_fix_pkg.vhd:1043-1071](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1043-L1071)）：`Trunc_s` 返回 0、`out_fmt.F >= in_fmt.F` 返回 0、否则返回 1，并在 `fmt_check` 开启时断言 `result_fmt = cl_fix_round_fmt(...)`（防止设计者给了一个会静默溢出的非法结果格式）。

#### 4.1.4 代码实践

**目标**：用 `reg_mode_g` 切换观察同一舍入的延迟差异。

**操作步骤**（源码阅读型 + 待本地仿真）：

1. 假设 `in_fmt_g = [1,4,8]`、`out_fmt_g = [1,4,4]`、`round_g = NonSymPos_s`（四舍五入）。
2. 手算 `recommended_c`：非截断、且 `out_fmt.F(4) < in_fmt.F(8)`，故为 1。
3. 据此推断三种 `reg_mode_g` 下的延迟：
   - `Auto_s` → `use_reg_c = (False) or (True and 1>0) = True` → 延迟 1
   - `Yes_s` → 延迟 1
   - `No_s` → 延迟 0
4. 把 `round_g` 换成 `Trunc_s` 重算：`recommended_c = 0`，于是 `Auto_s` → 延迟 0（与 `No_s` 相同）。

**需要观察的现象**：`Trunc_s` 时 `Auto_s` 与 `No_s` 延迟一致，因为舍入退化为纯截断、不产生逻辑。

**预期结果**：上表。**待本地验证**——可在 testbench 中例化后用波形看 `out_valid` 相对 `in_valid` 的拍数。

#### 4.1.5 小练习与答案

**练习 1**：`in_fmt_g=[0,2,8]`、`out_fmt_g=[0,2,8]`（格式完全相同）、`round_g=NonSymPos_s`，`Auto_s` 下延迟是多少？

**答案**：0。因为 `out_fmt.F(8) >= in_fmt.F(8)`，`recommended_c=0`，无需寄存。

**练习 2**：为什么文件头注释说 `No_s`「通常劣化时序」？

**答案**：`No_s` 强制走 `g_no_register` 直通，组合结果 `cl_fix_round(...)` 直接连到 `out_data`，舍入的加法进位链落在同一拍内，成为关键路径；插一拍寄存即可切断它。

---

### 4.2 en_cl_fix_saturate 实体：同构的饱和通路

#### 4.2.1 概念说明

`en_cl_fix_saturate` 与 round **结构完全同构**——同样的 generic 列表（除把 `round_g` 换成 `saturate_g`、且没有 `fmt_check_g`）、同样的端口、同样的「常量决定开/关寄存器」骨架。理解了 round，这里只需关注两处不同：

1. 组合核调用的函数换成 `cl_fix_saturate`（u4-l2：`None_s`/`Warn_s` 回绕，`Sat_s`/`SatWarn_s` 钳位）。
2. `cl_fix_recommended_pipelining`（saturate 重载）的「零逻辑」判据不同——饱和何时不需要寄存器。

#### 4.2.2 核心流程

saturate 的建议寄存器级数判定（见源码精读）：

\[
\text{recommended}_{\text{sat}} = \begin{cases} 0 & \text{saturate} \in \{\text{None\_s}, \text{Warn\_s}\} \\ 0 & \text{out\_fmt}.I \geq \text{in\_fmt}.I \;\land\; \text{out\_fmt}.S = \text{in\_fmt}.S \\ 1 & \text{否则} \end{cases}
\]

直觉：回绕（`None_s`/`Warn_s`）只是丢高位，硬件免费，无需寄存；只有真正钳位（`Sat_s`/`SatWarn_s`）**并且**整数位/符号位确实减少时，才需要比较器与多路选择器，值得寄存一拍。注意它还硬性断言 `result_fmt.F = a_fmt.F`——饱和不改小数位（要同时改 F 必须用 resize）。

`use_reg_c` 的合成公式与 round 完全一致（公式 ②）。

#### 4.2.3 源码精读

实体声明（[hdl/en_cl_fix_saturate.vhd:50-77](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L50-L77)）：与 round 几乎逐行相同，仅 generic 把 `round_g:FixRound_t` 换成 `saturate_g:FixSaturate_t`，且无 `fmt_check_g`。

两个常量（[hdl/en_cl_fix_saturate.vhd:84-85](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L84-L85)）与组合核（[hdl/en_cl_fix_saturate.vhd:91](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L91)）：

```vhdl
constant recommended_c : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, saturate_g);
constant use_reg_c     : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
...
result <= cl_fix_saturate(in_data, in_fmt_g, out_fmt_g, saturate_g);
```

两条 `generate` 分支（[hdl/en_cl_fix_saturate.vhd:94-110](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L94-L110)）与 round 一字不差。

saturate 版 `cl_fix_recommended_pipelining` 的判定（[hdl/en_cl_fix_pkg.vhd:1073-1099](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1073-L1099)）：先断言小数位不变；`None_s`/`Warn_s` 返回 0；`I` 未减且 `S` 未变返回 0；否则返回 1。

#### 4.2.4 代码实践

**目标**：体会「回绕免费、钳位才花钱」。

**操作步骤**：

1. 设 `in_fmt_g=[1,5,4]`、`out_fmt_g=[1,2,4]`、`saturate_g=SatWarn_s`：`I` 从 5 减到 2 且为钳位 → `recommended_c=1`。
2. 把 `saturate_g` 改成 `None_s`（回绕）：`recommended_c=0`，但注意此时硬件行为变成丢高位取模，**数值会回绕**。
3. 固定 `SatWarn_s`，把 `out_fmt_g` 改成 `[1,6,4]`（`I` 增加）：`out_fmt.I >= in_fmt.I` 且 `S` 不变 → `recommended_c=0`（向更宽格式饱和无需比较器）。

**需要观察的现象**：第 3 步虽然写的是「饱和」，但因目标更宽，实际不发生钳位，故无需寄存。

**预期结果**：上述三组 `recommended_c` 分别为 1 / 0 / 0。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`in_fmt_g=[1,4,8]`、`out_fmt_g=[0,4,8]`（去掉符号位）、`SatWarn_s`，`recommended_c` 是多少？

**答案**：1。`S` 从 1 变成 0（`out_fmt.S ≠ in_fmt.S`），属于「符号位改变」，需要比较器。

**练习 2**：saturate 实体为何没有 `fmt_check_g`？

**答案**：饱和不存在「结果格式需 +1 整数位」的进位问题（round 才有），其 `recommended_pipelining` 内部已用断言保证小数位不变，无需额外格式校验开关。

---

### 4.3 en_cl_fix_resize：串联 round → saturate

#### 4.3.1 概念说明

`en_cl_fix_resize` 不重写任何算法，而是**把 round 和 saturate 两个实体串成一个两级流水线**，精确对应 u4-l3 的结论：**resize = 先 round 后 saturate，顺序不可换**。它把「同时改 F 与 I/S」这件事拆成两段独立可寄存的处理，于是延迟也就是两段之和。

衔接两段的关键是一个编译期常量 `round_fmt_c`——round 级的输出格式、同时也是 saturate 级的输入格式：

\[
\text{round\_fmt\_c} = \text{cl\_fix\_round\_fmt}(\text{in\_fmt},\; \text{out\_fmt}.F,\; \text{round})
\]

即「F 取目标值、整数位按舍入模式可能 +1」。这正是 u4-l1 讲过的 `for_round`/`cl_fix_round_fmt` 的作用——给舍入进位预留整数位。

#### 4.3.2 核心流程

resize 实体内部的数据流：

```
in_data ──► [i_round: in_fmt → round_fmt_c] ──round_data──► [i_saturate: round_fmt_c → out_fmt] ──► out_data
in_valid/meta ──────────（随数据逐级寄存/直通）──────────► out_valid/meta
```

两个子实体都收到**同一个** `reg_mode_g`，于是：

- `Yes_s`：两级都插寄存器 → **延迟恒为 2**。
- `No_s`：两级都不插 → **延迟 0**。
- `Auto_s`：每级各自按 `recommended_c` 决定，延迟 = 两段之和。

resize 版 `cl_fix_recommended_pipelining` 正是这个求和（见源码精读）：

\[
\text{recommended}_{\text{resize}} = \text{recommended}_{\text{round}}(\text{in\_fmt}\to\text{round\_fmt\_c}) + \text{recommended}_{\text{sat}}(\text{round\_fmt\_c}\to\text{out\_fmt})
\]

文件头注释明确给出了三种 `reg_mode` 下的延迟：`Auto_s` = 上式、`Yes_s` = 2、`No_s` = 0。

#### 4.3.3 源码精读

衔接常量与中间信号（[hdl/en_cl_fix_resize.vhd:85-89](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L85-L89)）：

```vhdl
constant round_fmt_c : FixFormat_t := cl_fix_round_fmt(in_fmt_g, out_fmt_g.F, round_g);
signal round_valid   : std_logic;
signal round_meta    : std_logic_vector(meta_width_g-1 downto 0);
signal round_data    : std_logic_vector(cl_fix_width(round_fmt_c)-1 downto 0);
```

`round_data` 的位宽用 `cl_fix_width(round_fmt_c)` 求出——注意它可能比 `in_data`/`out_data` 都宽（舍入进位那 +1 整数位）。

第一级 `i_round`（[hdl/en_cl_fix_resize.vhd:96-116](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L96-L116)）：把外部 `in_*` 喂给 round 实体，`out_fmt_g => round_fmt_c`，输出接到中间信号 `round_*`。

第二级 `i_saturate`（[hdl/en_cl_fix_resize.vhd:121-141](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L121-L141)）：`in_fmt_g => round_fmt_c`（接住上一级）、`out_fmt_g => out_fmt_g`，输入是 `round_*`，输出就是实体最终的 `out_*`。两级共用同一 `clk`/`rst` 与同一 `reg_mode_g`/`meta_width_g`。

求和版的 `cl_fix_recommended_pipelining`（[hdl/en_cl_fix_pkg.vhd:1101-1111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L1101-L1111)）：先算 `round_fmt_c`，再返回 round 段与 saturate 段建议值之和——正是上面公式 ④。

#### 4.3.4 代码实践

**目标**：手算一个 resize 在三种 `reg_mode` 下的延迟，体会 `Auto_s` 可以比 `Yes_s` 更省。

**操作步骤**：

1. 取 `in_fmt=[1,4,8]`、`out_fmt=[1,5,4]`、`round=NonSymPos_s`、`saturate=SatWarn_s`（即「舍掉一半小数位，但整数位预留到 5」）。
2. 算 `round_fmt_c = cl_fix_round_fmt([1,4,8], 4, NonSymPos_s) = [1,5,4]`（非截断、F 减少故 I+1）。
3. round 段：F 减少(8→4)、非截断 → 1。
4. saturate 段：`SatWarn_s`；`out_fmt.I(5) >= round_fmt_c.I(5)` 且 `S` 不变 → 0。
5. 故 `Auto_s` = 1+0 = **1**；`Yes_s` = **2**；`No_s` = **0**。

**需要观察的现象**：`Auto_s` 比 `Yes_s` 少一拍——因为目标格式已为进位预留了整数位，saturate 段不发生钳位、无需寄存。

**预期结果**：延迟 1 / 2 / 0。**待本地验证**（可对比把 `out_fmt` 改回 `[1,2,4]`，则 saturate 段 `I` 减少 → `Auto_s`=2）。

#### 4.3.5 小练习与答案

**练习 1**：`in_fmt=[1,4,8]`、`out_fmt=[1,2,4]`、`round=Trunc_s`、`saturate=SatWarn_s`，`Auto_s` 延迟是多少？

**答案**：1。`round_fmt_c=[1,4,4]`（截断不增整数位）；round 段 `Trunc_s` → 0；saturate 段 `I` 由 4 减到 2、`SatWarn_s` → 1；合计 1。

**练习 2**：为什么 resize 文件头说 `Yes_s` 延迟「恒为 2」而不是「最多 2」？

**答案**：`Yes_s` 让两个子实体的 `use_reg_c` 都为真，无论各自 `recommended_c` 是 0 还是 1，每级都强制插一拍，共 2 拍。

---

### 4.4 valid / meta 端口约定：握手与旁路

#### 4.4.1 概念说明

三个实体共用同一套端口约定，目的是让它们能像积木一样串起来（resize 就是证据）：

- **`in_valid`/`out_valid`**：一个简单的 valid 握手（注意：没有 `ready`，是「单向 valid」）。数据在每个寄存器级随数据一起前进一拍；没有寄存器时则直通。
- **`in_meta`/`out_meta`**：sideband（旁路）元数据，典型用途是携带「这一拍数据对应的地址/通道号/帧号」等。它**不参与运算**，只和数据同步前进，宽度由 `meta_width_g` 决定，默认 0（空范围，即不用）。
- **`clk`/`rst`**：同步复位。复位仅在**寄存路径**中对 `out_valid` 生效（拉低），对 `out_meta`/`out_data` 只是普通打拍。

关键直觉：valid 与 meta 是「陪数据走相同拍数」的影子信号。只要每级的 `reg_mode` 一致，无论延迟是 0/1/2，valid 和 meta 都会与数据同时到达输出——这就是 testbench 里检查器敢用 `wait until out_valid='1'` 再读 `out_data`/`out_meta` 的原因。

#### 4.4.2 核心流程

寄存分支里三件套一起打拍（以 round 为例）：

```vhdl
if rising_edge(clk) then
    out_valid <= in_valid and not rst;   -- 复位时强制无效
    out_meta  <= in_meta;                -- 纯打拍，不 gated by rst
    out_data  <= result;
end if;
```

直通分支里三者全部并发直通：

```vhdl
out_valid <= in_valid;                   -- 注意：不 and not rst
out_meta  <= in_meta;
out_data  <= result;
```

一个容易忽略的细节：**只有寄存路径才会用 `and not rst` 把 valid 在复位期拉低**；直通路径里 `out_valid` 直接等于 `in_valid`，不受 `rst` 影响。这在把 `reg_mode` 从 `Yes_s` 改成 `No_s` 时会改变复位期的 valid 行为，需要注意。

`meta_width_g=0` 时，`in_meta`/`out_meta` 退化为 `std_logic_vector(-1 downto 0)`——一个空范围（null range）向量，相当于「不存在」，不影响综合。

#### 4.4.3 源码精读

端口声明见各实体，三处一致，例如 [hdl/en_cl_fix_round.vhd:59-77](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L59-L77)：`in_meta` 带默认值 `(others => 'X')`，便于上层不使用时留空。

寄存与直通对 valid 的不同处理见 [hdl/en_cl_fix_round.vhd:95-111](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_round.vhd#L95-L111)（saturate 同 [hdl/en_cl_fix_saturate.vhd:94-110](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_saturate.vhd#L94-L110)）。

resize 把 meta 随数据逐级传递：`in_meta → i_round.out_meta → round_meta → i_saturate.in_meta → out_meta`，见 [hdl/en_cl_fix_resize.vhd:104-141](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_resize.vhd#L104-L141)。

真实例化与 meta 检查的范例在 testbench：[tb/cl_fix_resize_tb.vhd:153-174](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L153-L174) 例化 `en_cl_fix_resize`，并在检查器 [tb/cl_fix_resize_tb.vhd:179-214](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L179-L214) 里用 `check_equal(out_meta, Random_v.RandSlv(meta_width_g))` 验证 meta 与数据同步到达——它用一个独立种子重新生成期望 meta，能成立的前提正是「meta 走了和数据完全相同的拍数」。

#### 4.4.4 代码实践

**目标**：验证 meta 与数据同步、且 `meta_width_g` 可开关。

**操作步骤**：

1. 阅读 [tb/cl_fix_resize_tb.vhd:138](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L138)：输入端每个有效拍用 `Random_v.RandSlv(meta_width_g)` 生成随机 meta。
2. 对照检查器 [tb/cl_fix_resize_tb.vhd:191](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L191)：用**同一个种子**重新生成同样的随机序列来比对 `out_meta`。
3. 注意 `sim/run.py` 对 resize TB 配了 `meta_width_g ∈ {0, 8}` 两个配置（[sim/run.py:187-193](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/run.py#L187-L193)），分别覆盖「不用 meta」与「8 位 meta」。

**需要观察的现象**：`MetaWidth=0` 配置下 meta 为空向量、检查仍通过；`MetaWidth=8` 下随机 meta 经若干拍后逐拍对上。

**预期结果**：两种配置都应输出 `SUCCESS! All tests passed.`（见 [tb/cl_fix_resize_tb.vhd:98](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L98)）。**待本地验证**（需安装仿真器）。

#### 4.4.5 小练习与答案

**练习 1**：若把 resize 的 `reg_mode_g` 设为 `Yes_s`，输入一个有效拍后，`out_meta` 上的对应值在第几拍出现？

**答案**：第 2 拍。`Yes_s` 下两级都插寄存器，meta 与数据一样延迟 2 拍。

**练习 2**：为什么 `in_meta` 的默认值是 `(others => 'X')` 而不是 `(others => '0')`？

**答案**：`'X'`（未知）表示「上层未驱动/不关心」，便于仿真时一眼发现「忘了接 meta 却又开了 `meta_width_g`」的错误；全 `'0'` 会掩盖这种悬空。

---

## 5. 综合实践

**任务**：在一个 testbench 中例化 `en_cl_fix_resize`，给定格式与模式，观察延迟与输出。

本仓库已自带一个完整的 resize testbench（[tb/cl_fix_resize_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd)），它由 `sim/run.py` 驱动、并由 cosim 脚本预先生成黄金参考文件。下面分两步：先跑通现成的，再写一个最小自造 TB。

### 步骤 1：运行仓库自带的 resize testbench

`sim/run.py` 要求同时给出仿真器名称与可执行路径（见 [sim/common.py:65-68](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/sim/common.py#L65-L68)），并用 cosim 生成参考文件（故**不要**加 `--disable-cosim`）。在仓库根目录执行（以 GHDL 为例，把路径换成你机器上 `ghdl` 所在目录）：

```bash
cd sim
python run.py --simulator ghdl --simulator-path /usr/local/bin "*resize*"
```

- 末尾的 `"*resize*"` 是 VUnit 的测试过滤器，只编译并运行 resize 相关 TB（含 `MetaWidth=0` 与 `MetaWidth=8` 两个配置）。
- 注意该 TB 会按 `RegisterMode_t'val(i mod reg_mode_count_c)` 在不同用例间**轮换** `reg_mode_g`（[tb/cl_fix_resize_tb.vhd:159](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L159)），因此一次运行就能覆盖 `Auto_s`/`Yes_s`/`No_s` 三种延迟。

**需要观察的现象**：终端最终打印 `SUCCESS! All tests passed.`；检查器既比对 RTL 输出 `out_data`，又独立调用函数 `cl_fix_resize(...)` 比对（[tb/cl_fix_resize_tb.vhd:204-208](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_resize_tb.vhd#L204-L208)），二者都应等于 cosim 黄金值。

**预期结果**：全部通过。若没有本地仿真器，本步骤标记为 **待本地验证**，可改为精读脚本理解数据流。

### 步骤 2：自造一个最小 TB（示例代码）

下面是一个**示例代码**骨架（非仓库原有文件），只例化一个固定配置的 `en_cl_fix_resize`、送一个值、用波形观察 `out_valid` 延迟。它依赖 `work.en_cl_fix_pkg`，需与 `hdl/*.vhd` 一起编译到 `lib` 库（可参照 `sim/run.py:46` 的编译方式）。

```vhdl
-- 示例代码：最小 resize 观测 TB（VHDL-2008）
library ieee;
    use ieee.std_logic_1164.all;
library work;
    use work.en_cl_fix_pkg.all;

entity resize_min_tb is
end entity;

architecture sim of resize_min_tb is
    constant in_fmt_c  : FixFormat_t := (1, 4, 8);   -- [1,4,8]
    constant out_fmt_c : FixFormat_t := (1, 2, 4);   -- [1,2,4]
    signal clk : std_logic := '0';
    signal rst : std_logic := '1';
    signal in_valid  : std_logic := '0';
    signal in_data   : std_logic_vector(cl_fix_width(in_fmt_c)-1 downto 0);
    signal out_valid : std_logic;
    signal out_data  : std_logic_vector(cl_fix_width(out_fmt_c)-1 downto 0);
begin
    clk <= not clk after 5 ns;

    -- 例化：round=NonSymPos_s, saturate=SatWarn_s, reg_mode=Auto_s
    dut : entity work.en_cl_fix_resize
        generic map (
            in_fmt_g    => in_fmt_c,
            out_fmt_g   => out_fmt_c,
            round_g     => NonSymPos_s,
            saturate_g  => SatWarn_s,
            reg_mode_g  => Auto_s,
            meta_width_g => 0
        )
        port map (
            clk => clk, rst => rst,
            in_valid => in_valid, in_meta => (others => 'X'), in_data => in_data,
            out_valid => out_valid, out_meta => open, out_data => out_data
        );

    stim : process
    begin
        -- 复位
        rst <= '1'; wait until rising_edge(clk);
        rst <= '0';
        -- 送一个值：1.5（[1,4,8] 下 = 2#0000000110000000）
        in_data  <= cl_fix_from_real(1.5, in_fmt_c);
        in_valid <= '1';
        wait until rising_edge(clk);
        in_valid <= '0';
        -- 观察几个拍，用波形看 out_valid 何时拉高、out_data 是否饱和到 1.9375
        for i in 0 to 4 loop
            wait until rising_edge(clk);
            report "out_valid=" & std_logic'image(out_valid) severity note;
        end loop;
        std.env.stop;
    end process;
end architecture;
```

**需要观察的现象与预期结果（待本地验证）**：

1. 用 4.3 节的公式手算：`round_fmt_c=[1,5,4]`；round 段 1、saturate 段（`I` 5→2 减少）1 → `Auto_s` 延迟 = 2。波形里 `out_valid` 应在 `in_valid` 拉高后第 2 个上升沿出现一个单拍脉冲。
2. 数值上：`1.5` 经 `NonSymPos_s` 舍入到 F=4 仍为 `1.5`，再饱和到 `[1,2,4]` 的上限 `1.9375`（`2#011111`），故 `out_data` 应为饱和最大值，不是 `1.5`。
3. 把 `reg_mode_g` 改成 `No_s` 重跑：`out_valid` 应在**同一拍**跟随 `in_valid`（延迟 0）；改成 `Yes_s` 仍是 2 拍（与本例相同）。

> 提示：若不想自造 TB，直接复用步骤 1 的自带 TB 即可完成本综合实践；步骤 2 仅用于孤立地、可重复地观察延迟这一行为。

## 6. 本讲小结

- 三个 RTL 实体共用同一骨架：**一行组合函数调用 + 一个由 `use_reg_c` 控制的 `generate` 可选寄存器**；算法逻辑全部复用 `en_cl_fix_pkg`，实体层只管「要不要寄存一拍」。
- `reg_mode_g` 三档：`Auto_s`（按需）、`Yes_s`（恒插，固定延迟）、`No_s`（不插，延迟 0 但伤时序）；是否「按需」由 `cl_fix_recommended_pipelining` 判定。
- round 与 saturate **结构同构**，仅函数与「零逻辑判据」不同：round 看「是否截断 / 是否减少小数位」，saturate 看「是否回绕 / 是否减少整数位或符号位」。
- `en_cl_fix_resize` 把 round→saturate **串联成两级**，用 `round_fmt_c` 衔接，延迟 = 两段之和（`Yes_s`=2、`No_s`=0、`Auto_s`=推荐值之和）。
- `valid` 是单向握手、随数据逐级前进；`meta` 是不参与运算的旁路，与数据同拍到达；`meta_width_g=0` 退化为空向量。
- 复位只对**寄存路径**的 `out_valid` 生效（`and not rst`），直通路径里 `out_valid` 不受 `rst` 影响。

## 7. 下一步学习建议

- **u7-l2（推荐流水线与 RegisterMode）**：本讲只用了 `cl_fix_recommended_pipelining` 的「返回值」，下一讲会系统讲它三个重载的判定细节，以及如何在系统级用 `Yes_s` 锁定固定延迟、用 `Auto_s` 让工具按需插拍。
- **u8-x（cosim 与文件 I/O）**：本讲综合实践里的「黄金参考」由 cosim 生成；想彻底理解 `data/*.txt` 怎么来、testbench 怎么读，进入单元 8。
- **继续阅读源码**：精读 [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) 中 `cl_fix_round`/`cl_fix_saturate`/`cl_fix_resize` 三个函数体（[L912](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L912)、[L980](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L980) 起），对照本讲确认「实体的组合核就是这三个函数」。
