# 搭建流水线数据通路：meta 与延迟一致性

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 `meta_width_g` 边带（sideband）信号在流水线各级中的透传作用，并知道为什么它必须与 `data`、`valid` 同拍穿越每一级寄存器。
- 区分 `RegisterMode_t` 的三种取值，重点掌握 `Auto_s`（延迟随配置变化）与 `Yes_s`（延迟恒定）的工程取舍，知道何时该选哪一个。
- 看懂 `en_cl_fix_resize` 是如何用「一个 round 子组件 + 一个 saturate 子组件」级联搭出两级流水线的，并能计算其延迟。
- 独立用一个由「乘法 → 舍入 → 饱和」级联的定点通路，标注每级的格式函数与寄存器模式选择。
- 在测试台（testbench）中看懂 UUT 的例化方式，以及它如何利用随机 meta 来验证边带透传的正确性。

本讲承接 u6-l1（已讲解三个可流水线化组件 `en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize` 的统一端口与 `RegisterMode_t` 的基本语义）。本讲不再重复单个组件内部如何选寄存器，而是把视角抬到「把多个组件串成一条完整数据通路」的层面，关注 **透传一致性** 和 **延迟一致性** 两个工程主题。

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个概念。

### 2.1 什么是「边带信号 meta」

在真实的数字信号处理数据通路里，一个数据采样（sample）往往不只携带数值本身，还携带一些「属于这一拍」的伴随信息，例如：

- 这个采样来自第几个通道（channel id）；
- 这个采样是否有效（其实 `valid` 本身就是一种最简单的边带）；
- 时间戳、帧头标志、用户自定义标签等等。

这些信息不参与定点运算，但必须和数据一起「走完」整条流水线，到达输出端时仍然和当初那一拍的数据对齐。这种信号叫 **边带信号（sideband）**。en_cl_fix 把它抽象成一个宽度可配的通用比特向量，命名为 `meta`。

### 2.2 为什么要担心「延迟一致性」

一条数据通路往往由多个组件串联（级联）。每个组件可能插 0 拍或 1 拍寄存器。整条通路的总延迟就是各级延迟之和。

问题在于：有些寄存器是「按需插入」的——只有当这一级确实做了有意义的运算（比如真的丢小数位）时才插。于是当你改变某个 generic（比如把舍入模式从 `Trunc_s` 改成 `NonSymPos_s`），这一级可能会从 0 拍变成 1 拍，**整条通路的延迟就变了**。

延迟变化在很多场景下是无害的；但在另一些场景下是灾难性的——例如：

- 多条并行通路要在输出端按拍对齐求和（延迟不一致就加错了拍）；
- 外部协议要求固定延迟（fixed latency）；
- 控制通路要精确知道数据在第几拍到达。

因此 en_cl_fix 给了你一个开关：**`Yes_s` 永远插寄存器，换来延迟恒定**。这就是本讲第二个核心主题。

### 2.3 一个记号约定

本讲用「延迟（latency）」指从 `in_data` 被采样到对应的 `out_data` 出现之间经过的时钟周期数。组合逻辑（不插寄存器）延迟为 0，插 1 级寄存器延迟为 1。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd) | 可流水线化的舍入组件，是讲解 meta 透传与 `Auto_s`/`Yes_s` 取舍的最佳范本。 |
| [hdl/en_cl_fix_saturate.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd) | 饱和组件，结构与 round 组件几乎一致，用于对比。 |
| [hdl/en_cl_fix_resize.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd) | 「两级级联」的范本：内部例化一个 round 子组件和一个 saturate 子组件，是学习如何用组件搭通路的关键。 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 包头定义 `RegisterMode_t`，包体提供 `cl_fix_recommended_pipelining` 三个重载，以及 `cl_fix_round_fmt`、`cl_fix_mult_fmt` 等格式函数。 |
| [tb/cl_fix_round_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) | round 组件的测试台，展示 UUT 例化方式与用随机 meta 校验透传。 |

## 4. 核心概念与源码讲解

### 4.1 meta_width_g 透传机制：让边带信号和数据同拍到达

#### 4.1.1 概念说明

`meta` 是一个宽度由 generic `meta_width_g` 决定的通用 `std_logic_vector`，默认值为 0（即不使用）。它的内容对组件来说完全「不透明」——组件既不解析它、也不修改它，只负责把它和数据一起搬运到输出端。

设计这样一个透传通道，是因为 en_cl_fix 的组件只关心「定点数值运算」，不想把任何业务语义（通道号、时间戳等）耦合进库内部。把边带抽象成裸比特向量，既保持了库的通用性，又让用户有能力把任意伴随信息随数据一起送过流水线。

#### 4.1.2 核心流程

`meta` 的透传规则可以用一句话概括：**meta 在每一级都和 data、valid 走完全相同的时序路径**。

- 当组件插入寄存器（`use_reg_c` 为真）时：`out_meta` 在时钟上升沿被打一拍，与 `out_data`、`out_valid` 同步。
- 当组件不插寄存器（纯组合逻辑）时：`out_meta` 直接接到 `in_meta`，与 `out_data`、`out_valid` 同样保持组合一致。

也就是说，无论组件内部是寄存型还是组合型，meta 的延迟永远等于 data 的延迟。这一点是整个透传机制能够成立的关键。

#### 4.1.3 源码精读

先看 round 组件的 generic 与端口声明。`meta_width_g` 默认为 0，`in_meta`/`out_meta` 的位宽都由它决定：

[hdl/en_cl_fix_round.vhd:56](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L56) 定义 `meta_width_g` 为 sideband metadata 宽度，默认 0 表示未使用。

[hdl/en_cl_fix_round.vhd:69](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L69) 与 [hdl/en_cl_fix_round.vhd:75](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L75) 分别声明输入边带 `in_meta`（带 `(others => 'X')` 默认值，方便不接 meta 时编译通过）与输出边带 `out_meta`。

再看两条 generate 分支，这是 meta 透传的真正落点：

```vhdl
-- With pipeline register
g_register : if use_reg_c generate
    process(clk)
    begin
        if rising_edge(clk) then
            out_valid <= in_valid and not rst;
            out_meta  <= in_meta;          -- meta 与 data 同拍打一拍
            out_data  <= result;
        end if;
    end process;
end generate;

-- Without pipeline register
g_no_register : if not use_reg_c generate
    out_valid <= in_valid;
    out_meta  <= in_meta;                  -- meta 与 data 一起走组合逻辑
    out_data  <= result;
end generate;
```

[hdl/en_cl_fix_round.vhd:95-104](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L95-L104) 是插寄存器分支，注意 `out_meta <= in_meta` 与 `out_data <= result` 写在同一个时钟进程里——三者被同一时钟沿一起采样，保证同拍到达。

[hdl/en_cl_fix_round.vhd:107-111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L107-L111) 是不插寄存器分支，`out_meta <= in_meta` 是组合直通。

> 注意一个细节：`out_valid <= in_valid and not rst;`——valid 在经过寄存器时会被复位清零，而 `out_meta` 和 `out_data` 不做这种处理。这意味着复位期间 `out_meta`/`out_data` 的内容是「未定义但保持」的，而 valid 会明确拉低。这是合理的：只要 valid 为低，下游就不该采信这一拍的 meta/data。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，验证「meta 的延迟永远等于 data 的延迟」这一论断，并理解 `meta_width_g = 0` 的退化情形。

**操作步骤**：

1. 打开 [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd)，分别在 `g_register` 和 `g_no_register` 两个分支里数一下 `out_meta`、`out_data`、`out_valid` 三者的赋值方式。
2. 打开 [hdl/en_cl_fix_saturate.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd)，确认它的两条 generate 分支与 round 组件是否逐字一致。
3. 思考：当 `meta_width_g = 0` 时，`std_logic_vector(meta_width_g-1 downto 0)` 即 `std_logic_vector(-1 downto 0)`，这是一个 **空区间（null range）** 的向量。此时 `in_meta`/`out_meta` 实际是 0 位宽的「空线」，赋值语句仍然合法但对综合不产生任何硬件。

**需要观察的现象**：三个组件对 meta 的处理完全一致；饱和组件与舍入组件在 meta 透传上没有任何差别。

**预期结果**：你能得出结论——meta 是一个「和 data 同节拍、同延迟」的纯粹伴生通道，组件不关心其内容。

**待本地验证**：若你手头有仿真器，可把 round 组件的 `meta_width_g` 设为 8，输入一串已知 meta，观察输出 meta 是否在对应延迟拍上原样出现。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `out_meta <= in_meta;` 从 `g_register` 分支里删掉，会发生什么？
**答案**：寄存型配置下 `out_meta` 会悬空（综合工具通常给出警告并可能接 0），meta 不再到达输出端，边带信息丢失。这正是为什么它必须和 data 写在同一个进程里。

**练习 2**：为什么 `in_meta` 的声明带 `:= (others => 'X')` 默认值，而 `in_data` 不带？
**答案**：`in_data` 的位宽由格式决定、使用者必须显式连接；而 `meta` 是可选边带，当用户不需要 meta 时可以干脆不连 `in_meta`，默认值让端口不连也能编译通过（空区间时尤其有用）。

---

### 4.2 RegisterMode 与延迟：Auto_s vs Yes_s 的工程权衡

#### 4.2.1 概念说明

`RegisterMode_t` 是一个三态枚举，它只控制「要不要插寄存器」，**完全不影响数值结果**——同一组 generics 下，无论选哪种模式，`out_data` 的最终数值序列都一样，差别只在到达时间。

三种取值的工程含义如下（摘自包头注释）：

```vhdl
type RegisterMode_t is
(
    Auto_s,         -- Inserts the recommended registering. See cl_fix_recommended_pipelining.
    Yes_s,          -- Inserts all registering. Can be useful for consistent latency.
    No_s            -- Inserts no registering. Use with caution (poor timing performance).
);
```

- `Auto_s`：只在「有实质运算、值得插寄存器」时才插，否则走组合逻辑。延迟会随 generics 变化。
- `Yes_s`：无论是否需要，恒插寄存器。延迟恒定，适合需要固定延迟的场景。
- `No_s`：恒不插寄存器，延迟恒为 0，但时序性能通常变差，注释明确提示「Use with caution」。

#### 4.2.2 核心流程

组件用一个综合期常量 `use_reg_c` 在两条 generate 分支里二选一：

\[ \text{use\_reg\_c} = (\text{reg\_mode\_g} = \text{Yes\_s}) \;\lor\; \bigl(\text{reg\_mode\_g} = \text{Auto\_s} \;\land\; \text{recommended\_c} > 0\bigr) \]

其中 `recommended_c` 由纯函数 `cl_fix_recommended_pipelining` 算出，取值为 0 或 1。含义是：

- `Yes_s` 无条件插；
- `Auto_s` 仅当推荐值 > 0（确有运算）才插；
- `No_s` 既不满足 `Yes_s` 也不满足 `Auto_s and ...`，于是 `use_reg_c` 为假，走组合逻辑。

对 round 组件，三种模式的延迟分别为：

| `reg_mode_g` | 延迟 |
| --- | --- |
| `Auto_s` | `cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g)`，取 0 或 1 |
| `Yes_s` | 1 |
| `No_s` | 0 |

注意 `Yes_s` 的延迟写死为 1（round 组件），即使本次舍入其实不需要任何寄存器也会硬插一拍——这正是「用面积换延迟恒定」的代价。

#### 4.2.3 源码精读

[en_cl_fix_pkg.vhd:68-73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L68-L73) 定义 `RegisterMode_t` 三态枚举，注释直接点明 `Yes_s` 的用途是「consistent latency」。

round 组件的核心两行：

```vhdl
constant recommended_c : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, fmt_check_g);
constant use_reg_c     : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
```

[hdl/en_cl_fix_round.vhd:85-86](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L85-L86) 把上面的判定逻辑落地。`recommended_c` 被约束在 `0 to 1`，所以单级 round 最多插一拍。

饱和组件用几乎相同的两行：

[hdl/en_cl_fix_saturate.vhd:84-85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd#L84-L85) 调用 `cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, saturate_g)` 得到推荐值，`use_reg_c` 判定逻辑与 round 完全相同。

那么「推荐值」到底怎么算？看包体的三个重载。round 的版本：

[en_cl_fix_pkg.vhd:1041-1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069)：当 `round = Trunc_s`（纯截断，无加法）时返回 0；当 `result_fmt.F >= a_fmt.F`（没有丢小数位，无需运算）时返回 0；否则返回 1。即「只有真正做舍入加偏移且丢小数位」才推荐插寄存器。

saturate 的版本：

[en_cl_fix_pkg.vhd:1071-1097](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1071-L1097)：当模式为 `None_s`/`Warn_s`（只回绕不钳位）时返回 0；当 `result_fmt.I >= a_fmt.I and result_fmt.S = a_fmt.S`（整数位和符号位都没被压缩，无需钳位比较）时返回 0；否则返回 1。

> 这两个函数揭示了一个关键事实：`Auto_s` 的延迟之所以「会变」，正是因为「是否需要运算」取决于 generics。例如把 `round_g` 从 `Trunc_s` 改成 `NonSymPos_s`，round 组件就会从 0 拍跳到 1 拍。`Yes_s` 则无视这一切，永远给 1 拍。

#### 4.2.4 代码实践

**实践目标**：通过查表推算，体会 `Auto_s` 延迟随 generics 变化、而 `Yes_s` 延迟恒定。

**操作步骤**：

1. 假设 round 组件 `in_fmt_g = (0,8,8)`、`out_fmt_g = (0,8,4)`。
2. 对 `round_g ∈ {Trunc_s, NonSymPos_s}` 两种取值，分别套用 [en_cl_fix_pkg.vhd:1041-1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069) 的规则，算出 `recommended_c`。
3. 列表对比 `Auto_s` 和 `Yes_s` 在这两种配置下的延迟。

**需要观察的现象**：`Trunc_s` 时 `Auto_s` 延迟为 0，`NonSymPos_s` 时 `Auto_s` 延迟为 1；而 `Yes_s` 在两种配置下延迟恒为 1。

**预期结果**：

| `round_g` | `Auto_s` 延迟 | `Yes_s` 延迟 |
| --- | --- | --- |
| `Trunc_s` | 0 | 1 |
| `NonSymPos_s` | 1 | 1 |

这正好说明了：当你在同一份设计里只改舍入模式，`Auto_s` 会让通路延迟发生跳变，`Yes_s` 则保持不变。

**待本地验证**：可在一个顶层实体里例化两个 round 组件（同 generics、仅 `round_g` 不同），用波形观察两者 `out_valid` 相对 `in_valid` 的延迟差。

#### 4.2.5 小练习与答案

**练习 1**：某条通路要求无论舍入模式怎么改，输出延迟都必须是固定 1 拍。该选哪种 `reg_mode_g`？为什么 `Auto_s` 不行？
**答案**：选 `Yes_s`。因为 `Auto_s` 的延迟由 `cl_fix_recommended_pipelining` 决定，`Trunc_s` 时为 0、非截断时为 1，会随模式跳变；`Yes_s` 恒为 1，满足固定延迟要求。

**练习 2**：`No_s` 和「`Auto_s` 且推荐值为 0」最终都不插寄存器，二者在硬件上有区别吗？
**答案**：在该具体配置下二者硬件结果一致（都是组合逻辑），但语义不同：`No_s` 是「永远不插，即便需要也不插」（时序风险），而 `Auto_s` 是「按需插，这次恰好不需要」。如果之后改了 generics，`Auto_s` 会自动插寄存器，`No_s` 仍然不插。

---

### 4.3 级联搭建数据通路：resize 的两级实现与延迟叠加

#### 4.3.1 概念说明

单个 round / saturate 组件只能做一件事。真实设计需要把它们串起来。en_cl_fix 直接在库里给了一个「如何级联」的范本：`en_cl_fix_resize`。

回顾 u5：`cl_fix_resize` 这个纯函数的语义是「先 round、后 saturate」（顺序不可交换）。`en_cl_fix_resize` 组件忠实地把这个语义落地为硬件——它**不重写任何运算逻辑**，而是直接例化一个 `en_cl_fix_round` 子组件，再串一个 `en_cl_fix_saturate` 子组件。这种「用组件搭组件」的做法正是本讲想教给你的核心技能。

#### 4.3.2 核心流程

resize 组件内部的数据流是：

```
in_data ──▶ [en_cl_fix_round] ──▶ round_data ──▶ [en_cl_fix_saturate] ──▶ out_data
in_meta ──▶                   ──▶ round_meta  ──▶                      ──▶ out_meta
in_valid──▶                   ──▶ round_valid ──▶                      ──▶ out_valid
```

两级之间用一个「中间格式」`round_fmt_c` 衔接：它是 round 的输出格式，也是 saturate 的输入格式。这个格式由 `cl_fix_round_fmt` 在综合期算出。

延迟是两级之和。设 round 子组件延迟为 \(L_r\)、saturate 子组件延迟为 \(L_s\)，则 resize 总延迟为：

\[ L_{\text{resize}} = L_r + L_s \]

对三种 `reg_mode_g`：

| `reg_mode_g` | \(L_r\) | \(L_s\) | \(L_{\text{resize}}\) |
| --- | --- | --- | --- |
| `Auto_s` | `cl_fix_recommended_pipelining`（round 部分） | `cl_fix_recommended_pipelining`（sat 部分） | 二者之和（0、1 或 2） |
| `Yes_s` | 1 | 1 | 2 |
| `No_s` | 0 | 0 | 0 |

注意 `Yes_s` 下 resize 恒为 2 拍延迟——这点写在了组件头部的注释里（见下文源码引用）。这正是「固定延迟」的代价：即使某一级本不需要寄存器，`Yes_s` 也会硬插，换来跨配置的恒定 2 拍。

meta 的透传在级联里依然成立：meta 进入 round 子组件，被打拍/直通到 `round_meta`，再进入 saturate 子组件，再被打拍/直通到 `out_meta`。每一级 meta 与该级的 data 同步，所以最终 `out_meta` 与 `out_data` 仍然同拍到达。

#### 4.3.3 源码精读

resize 组件头部注释明确给出了三种模式的延迟：

[hdl/en_cl_fix_resize.vhd:27-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L27-L35)：`Auto_s` 延迟等于 `cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, saturate_g)`；`Yes_s` 延迟恒为 2；`No_s` 延迟为 0。

中间格式 `round_fmt_c` 的推导——注意它和包体内 `cl_fix_recommended_pipelining`（resize 重载）用的是同一个公式：

```vhdl
constant round_fmt_c : FixFormat_t := cl_fix_round_fmt(in_fmt_g, out_fmt_g.F, round_g);
```

[hdl/en_cl_fix_resize.vhd:85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L85)：用 `cl_fix_round_fmt` 算出 round 之后的格式（含可能的 +1 整数位），作为两级之间的衔接格式。

round 子组件的例化——它把 `reg_mode_g` 原样下传，并透传 meta：

[hdl/en_cl_fix_resize.vhd:96-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L96-L116)：例化 `i_round`，`out_fmt_g => round_fmt_c`，`in_meta => in_meta`、`out_meta => round_meta`，把第一级的输出连到中间信号。

saturate 子组件的例化——它的输入格式正是上一级的 `round_fmt_c`：

[hdl/en_cl_fix_resize.vhd:121-141](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L121-L141)：例化 `i_saturate`，`in_fmt_g => round_fmt_c`、`in_meta => round_meta`、`out_meta => out_meta`，完成第二级。meta 经两级透传最终到达 `out_meta`。

包体里 resize 的 `cl_fix_recommended_pipelining` 重载就是把两级的推荐值相加，正好对应级联结构：

[en_cl_fix_pkg.vhd:1099-1109](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1099-L1109)：先用 `cl_fix_round_fmt` 算 `round_fmt_c`，再返回 `pipelining(a_fmt, round_fmt_c, round) + pipelining(round_fmt_c, result_fmt, saturate)`。这与组件内部「round 子组件 + saturate 子组件」的结构逐拍对应。

而 `cl_fix_round_fmt` 本身（在 u3-l3 已讲过，这里复习它为何能让 resize 的衔接格式正确）：

[en_cl_fix_pkg.vhd:608-628](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L608-L628)：非 `Trunc_s` 且确实减少小数位时，整数位 +1（因为舍入可能进位）；否则整数位不变；并强制结果至少 1 位宽。这个 +1 整数位正是 `round_fmt_c` 比 `out_fmt_g` 可能更宽的原因，也是 saturate 级「有事可做」的来源。

#### 4.3.4 代码实践

**实践目标**：用 resize 组件的源码作为模板，手工把一条「乘法 → 舍入 → 饱和」通路的各级格式与延迟推算出来。

**操作步骤**：

1. 设输入 `a_fmt = (1,1,8)`、`b_fmt = (1,1,8)`，两者相乘。
2. 用 `cl_fix_mult_fmt` 算出乘法全精度中间格式 `mult_fmt`（小数位 = 8+8 = 16；整数位与符号位按 u3-l2 的规则）。
3. 假设希望最终输出 `out_fmt = (1,3,4)`，于是后面接一个 resize：`in_fmt_g = mult_fmt`、`out_fmt_g = out_fmt`、`round_g = NonSymPos_s`、`saturate_g = SatWarn_s`。
4. 用 [en_cl_fix_pkg.vhd:1099-1109](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1099-L1109) 的公式算出该 resize 在 `Auto_s` 下的延迟（先算 round 部分是否插拍，再算 saturate 部分是否插拍）。
5. 列表给出 `Auto_s` / `Yes_s` 两种选择下整条通路的总延迟（假设乘法本身外部已处理为 1 拍）。

**需要观察的现象**：`mult_fmt` 的小数位 16 远大于目标 4，所以 round 部分一定会插拍（丢小数位 + 非截断）；整数位被压缩、模式为 `SatWarn_s`，所以 saturate 部分也会插拍。

**预期结果**：该 resize 在 `Auto_s` 下延迟为 2；在 `Yes_s` 下延迟也为 2。整条「乘法(1) + resize」通路在两种模式下总延迟分别为 3 和 3。**但如果你把 `round_g` 改成 `Trunc_s`**，`Auto_s` 下 round 部分不再插拍，resize 延迟降为 1，整条通路延迟变为 2——而 `Yes_s` 仍是 3。这正是 `Auto_s` 延迟可变、`Yes_s` 延迟恒定的活生生例子。

**待本地验证**：乘法结果格式 `mult_fmt` 的整数位/符号位需对照 u3-l2 规则确认；若不确定，可在 Python 端用 `FixFormat.for_mult` 验证。

#### 4.3.5 小练习与答案

**练习 1**：resize 组件在 `Yes_s` 下延迟为什么是 2 而不是 1？
**答案**：因为 resize 内部级联了 round 和 saturate **两个**子组件，每个子组件在 `Yes_s` 下都恒插 1 拍，2 + 2 的结构里两级各贡献 1 拍，合计 2 拍。

**练习 2**：为什么不直接在 resize 组件里写一份「先 round 后 saturate」的组合逻辑，而要例化两个子组件？
**答案**：例化子组件可以复用每个子组件内部已经写好的「按 `use_reg_c` 在寄存型/组合型之间二选一」的逻辑，让 resize 自动获得「两级各自独立判断是否插拍」的能力，并自动透传 meta。若重写一份，就要重复实现一遍寄存器选择与 meta 透传，既冗余又容易和子组件行为不一致。

---

### 4.4 testbench 中 UUT 的例化方式与 meta 校验

#### 4.4.1 概念说明

了解了组件如何级联、meta 如何透传之后，最后一个问题是：**怎么验证 meta 真的被正确透传了？** 答案在 `tb/cl_fix_round_tb.vhd` 里。这个测试台展示了一个标准的「输入进程 → UUT → 检查进程」三段式结构，并用一种巧妙手段检验 meta：在输入端给每一拍注入一个 **随机** 的 meta 值，在输出端用 **相同的随机种子** 重新生成同一序列，逐拍比对 `out_meta` 是否等于期望值。

如果 meta 在寄存器里被错位、丢失或延迟不一致，输出端的随机序列就会和输入端错位，比对立刻失败。

#### 4.4.2 核心流程

测试台对每个测试用例（由 `g_test_case` generate 展开）做三件事：

1. **输入进程 `p_input`**：复位后，用 `for a in Amin to Amax` 穷举该格式的所有可能取值（与 cosim 脚本的计数器一致），每一拍把 `in_data` 设为当前值、把 `in_meta` 设为一个由 OSVVM 随机数生成的 `RandSlv(meta_width_g)`。
2. **UUT**：例化 `en_cl_fix_round`，把 `reg_mode_g` 设为 `RegisterMode_t'val(i mod reg_mode_count_c)`——即在不同测试用例间轮换三种寄存器模式，确保三种模式都被覆盖。
3. **检查进程 `p_check`**：用 **同一个随机种子** 重新生成 meta 序列，每收到一拍 `out_valid` 就比对 `out_meta` 与期望值，并比对 `out_data` 与 cosim 黄金数据。

关键点：输入进程和检查进程都调用 `Random_v.InitSeed(RandSeed_c)`，且 `RandSeed_c` 对同一个测试用例是同一个字符串（`"Metadata seed " & to_string(i)`），因此两边生成的随机 meta 序列完全一致——这就是能逐拍比对的前提。

#### 4.4.3 源码精读

输入进程给每一拍注入随机 meta：

[tb/cl_fix_round_tb.vhd:122-147](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L122-L147)：复位后穷举 `Amin..Amax`，第 137 行 `in_meta <= Random_v.RandSlv(meta_width_g);` 每拍注入一个随机边带。

UUT 的例化——注意 `reg_mode_g` 的轮换写法和 `meta_width_g` 的透传：

[tb/cl_fix_round_tb.vhd:152-172](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L152-L172) 例化 `i_uut : entity work.en_cl_fix_round`，其中：

- `in_fmt_g => a_fmt_c(i)`、`out_fmt_g => r_fmt_c(i)`：格式从 cosim 生成的文件读入；
- `round_g => FixRound_t'val(rnd_c(i))`：舍入模式从 `rnd.txt` 读入的整数还原为枚举；
- `reg_mode_g => RegisterMode_t'val(i mod reg_mode_count_c)`：**用例索引对寄存器模式数取模，轮换三种模式**；
- `meta_width_g => meta_width_g`：把 TB 顶层 generic 透传进 UUT。

检查进程里的 meta 校验：

[tb/cl_fix_round_tb.vhd:177-203](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L177-L203)：第 182 行 `Random_v.InitSeed(RandSeed_c)` 用同一种子重置随机源；第 188 行 `check_equal(out_meta, Random_v.RandSlv(meta_width_g), "Metadata mismatch")` 逐拍比对输出 meta 与重新生成的期望序列。

寄存器模式计数的定义在前面：

[tb/cl_fix_round_tb.vhd:66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L66) `reg_mode_count_c : positive := 1 + RegisterMode_t'pos(RegisterMode_t'high)`，算出枚举总数（这里是 3），用于上面的取模轮换。

`meta_width_g` 这个顶层 generic 又由 `sim/run.py` 在装配时配成两个值：

[sim/run.py:160-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L160-L165) 对 `cl_fix_round_tb` 用 `for meta_width in [0, 8]` 生成两组配置 `MetaWidth=0` 与 `MetaWidth=8`，分别验证「不用 meta」和「8 位 meta」两种情形。

#### 4.4.4 代码实践

**实践目标**：通过阅读测试台，理解「同种子双随机源」校验 meta 的原理，并把它迁移到级联通路的验证思路中。

**操作步骤**：

1. 在 [tb/cl_fix_round_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) 中，定位 `p_input` 里 `InitSeed` 与 `RandSlv` 的调用，再定位 `p_check` 里同样这两次调用，确认它们使用同一个 `RandSeed_c`。
2. 解释为什么校验 meta 时**不能**直接把输入端的 `in_meta` 信号接到检查进程去比对，而要重新生成一遍。
3. 打开 [tb/cl_fix_resize_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_resize_tb.vhd)，确认它的 `p_input`/`p_check` 结构与 round TB 几乎一致（resize TB 额外多了一处直接调用 `cl_fix_resize` 纯函数的自检，见第 203-208 行），说明这套 meta 校验模板在多级通路里同样适用。

**需要观察的现象**：输入与检查两侧的随机序列因同种子而完全同步；这种设计对任意延迟都鲁棒——只要 meta 真的和 data 同拍到达，比对就通过，与组件插了几拍无关。

**预期结果**：你能用自己的话讲清「为什么用两个独立随机源而非直接接线比对」：因为检查进程只信任 `out_valid` 节拍上的输出，无法直接访问输入端的历史 `in_meta`；用同种子再生序列，相当于在检查侧重建了一个「期望 meta 发生器」，天然与 UUT 的实际延迟解耦。

**待本地验证**：若运行仿真（见 u1-l3 的 `python sim/run.py`），观察 `MetaWidth=8` 配置下「Metadata mismatch」断言是否从不触发。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `p_check` 里的 `Random_v.InitSeed(RandSeed_c)` 删掉，meta 校验会发生什么？
**答案**：检查侧的随机源不会重置到与输入侧相同的起点，再生出的 meta 序列与输入侧错位，`check_equal(out_meta, ...)` 几乎必然失败。这恰恰反证了同种子是该校验成立的必要条件。

**练习 2**：为什么 TB 要用 `i mod reg_mode_count_c` 轮换三种寄存器模式，而不是固定用一种？
**答案**：因为 meta 透传和延迟行为在三种模式下都应正确。轮换模式让同一套测试用例天然覆盖 `Auto_s`/`Yes_s`/`No_s` 三种配置，确保「插寄存器」「不插寄存器」两种 data/meta 路径都被验证过。

---

## 5. 综合实践

**任务**：设计一个由「乘法 → 舍入 → 饱和」级联的定点通路草图，并标注每级的格式函数、寄存器模式选择与 meta 贯穿方式。

**要求**：

1. 选定两个输入格式，例如 `a_fmt = (1,1,8)`、`b_fmt = (1,1,8)`（两个 1.1.8 的有符号小数）。
2. 第一级「乘法」：标注其全精度中间格式用 `cl_fix_mult_fmt(a_fmt, b_fmt)` 算出（参考 [en_cl_fix_pkg.vhd:508](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L508)）。假设乘法本身在一个外部 DSP 块里完成、带 1 拍寄存器。
3. 第二级「舍入 + 饱和」：用一个 `en_cl_fix_resize` 组件，`in_fmt_g = mult_fmt`、`out_fmt_g = (1,3,4)`、`round_g = NonSymPos_s`、`saturate_g = SatWarn_s`。标注它内部衔接格式由 `cl_fix_round_fmt` 推导（参考 [en_cl_fix_resize.vhd:85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L85)）。
4. 画出数据流框图，在每一级上标出：该级用到的格式函数、`reg_mode_g` 的选择（`Auto_s` 或 `Yes_s`）、以及该级贡献的延迟拍数。
5. 用一句话说明 meta 如何贯穿：meta 从最前端注入，经乘法寄存器、resize 内部的 round 子组件、saturate 子组件逐级透传，与 data 同拍到达末端。
6. **关键决策**：如果你被告知「这条通路会被复制成 4 条并行支路，末端要在同一拍求和」，请说明你应该把所有级的 `reg_mode_g` 设成什么，并解释原因。

**参考答案要点**：

- 框图：`a,b → [mult, 1拍] → mult_fmt → [en_cl_fix_resize] → out_fmt`，resize 内部再展开为 `[round] → round_fmt → [saturate]`。
- 格式函数：乘法用 `cl_fix_mult_fmt`；resize 衔接格式用 `cl_fix_round_fmt`；总延迟可用 `cl_fix_recommended_pipelining`（resize 重载）核对。
- meta 贯穿：每一级都把 `in_meta` 与 `in_data` 同步搬运到输出，参见 [en_cl_fix_round.vhd:95-111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L95-L111) 的两条 generate 分支。
- 并行求和场景：4 条支路必须延迟完全一致才能同拍求和，因此所有级应选 `Yes_s`（round 组件恒 1 拍、resize 恒 2 拍），避免某条支路因 `Auto_s` 在某些 generics 下少插一拍而导致错位相加。

## 6. 本讲小结

- `meta` 是宽度可配（`meta_width_g`，默认 0）的通用边带向量，组件不解析其内容，只负责把它和 `data`、`valid` 一起按相同的时序路径搬运到输出端；`meta_width_g = 0` 时退化为空区间向量。
- meta 透传的关键是：无论组件走寄存型（`g_register`）还是组合型（`g_no_register`），`out_meta` 的延迟永远等于 `out_data` 的延迟——三者写在同一个进程或同一组并发赋值里。
- `RegisterMode_t` 三态只控时序、不影响数值：`Auto_s` 按 `cl_fix_recommended_pipelining` 按需插拍（延迟随 generics 变化），`Yes_s` 恒插拍（延迟恒定），`No_s` 恒不插拍（延迟 0，时序差）。
- `Yes_s` 适合需要固定延迟的场景（多路并行对齐、协议固定延迟）；其代价是即便无需运算也插寄存器，round 组件恒 1 拍、resize 组件恒 2 拍。
- `en_cl_fix_resize` 是「用组件搭组件」的范本：内部例化一个 round 子组件 + 一个 saturate 子组件，中间用 `cl_fix_round_fmt` 推导衔接格式，总延迟为两级之和，meta 经两级透传到达末端。
- 测试台用「同种子双随机源」校验 meta：输入端按拍注入随机 meta，检查端用相同种子再生序列逐拍比对，该校验对任意延迟都鲁棒，并已通过 `run.py` 的 `MetaWidth=0/8` 两组配置覆盖。

## 7. 下一步学习建议

- 进入 **U7（专家层）**：本讲的测试台只是「逐拍比对」的末端。建议接着学 u7-l1「协同仿真概念与 cosim 脚本」，看看 `p_check` 里读的 `test*_output.txt` 黄金数据是如何由 Python cosim 脚本提前生成的，理解「Python 参考模型 → 黄金数据 → HDL 比对」的完整闭环。
- 学 u7-l2「VUnit 测试台与文件 I/O」，深入 `cl_fix_read_format_file` / `cl_fix_read_file` 如何把 cosim 数据喂给本讲看到的 `p_input`/`p_check`，以及 `en_cl_fix_fileio_pkg` 对 `en_tb` 的封装。
- 若想再练手组合：尝试仿照 `en_cl_fix_resize` 的写法，自己画一个「shift → resize」或「add → resize」的级联实体草图，标注每级的格式函数与 `reg_mode_g` 选择，并用本讲的延迟叠加公式算出 `Auto_s` 与 `Yes_s` 下的总延迟。
