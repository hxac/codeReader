# 外部触发：边沿检测与多路 OR

## 1. 本讲目标

本讲聚焦三类触发源中的**外部触发**（external trigger）。学完后你应该能够：

- 说清 `Trig_In` 多路外部触发信号如何被两拍流水寄存、再做上升沿检测；
- 解释 `EnableExtTrig` 寄存器如何按位独立使能每一路外部触发；
- 掌握多路外部触发如何 OR 合成单一的 `ExtTrigPending_2` 锁存位；
- 说明 `ExtTrigPending_2` 在哪些情况下被置位、又在哪些情况下（特别是重新 Arm 进入 PreTrig 时）被清除；
- 能读懂测试平台 `top_tb_case5` 是如何验证「逐路使能」与「多路 OR」这两条性质的。

本讲承接 [u4-l1 触发源总览与 TrigEna 掩码](u4-l1-trigger-sources-and-masking.md)：u4-l1 给出了「三类源 + TrigEna 掩码 + TrigNow 合成」的总框架，本讲把其中的**外部触发这一支**彻底拆透。

## 2. 前置知识

阅读本讲前，你需要理解以下概念（均在前面讲义中建立）：

- **两进程法与流水线后缀命名**（u3-l3）：记录器把组合逻辑放在 `p_comb`、寄存器更新放在 `p_seq`；信号名后的数字后缀（如 `_2`、`_3`）表示该信号所处的流水级。外部触发的关键信号 `ExtTrigPending_2` 就处在 Stage2。
- **状态机五状态**（u3-l2）：`Idle → PreTrig → WaitTrig → PostTrig → Done`。外部触发的「兑现」只发生在 `WaitTrig` 状态，而「清除」发生在 `PreTrig` 状态。
- **三类触发源与 TrigEna 掩码**（u4-l1）：外部、软件、自触发三类源各与 `TrigEna` 的一个位相与后 OR 成 `TrigNow_2`，最后再与 `r.In_Vld(1)` 相与。外部触发对应 `TrigEna` 的 bit0，即 `Reg_TrigEna_ExtIdx_c`。
- **寄存器地图**（u2-l2）：外部触发的逐路使能寄存器是 `Reg_EnableExtTrig_Addr_c`（字节地址 `0x0030`）。

补充一个数字电路常识：**上升沿检测**就是判断一个信号「上一拍为 0、本拍为 1」，即 `current AND NOT previous`。把输入先寄存一拍、再寄存一拍，比较这两个寄存值，就能得到一个干净、同步的上升沿脉冲，避免组合逻辑上的毛刺被误判。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器。本讲关注其中 `Trig_In` 的流水寄存、上升沿检测 for 循环、`ExtTrigPending_2` 的置位/清除。 |
| [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | Vivado 封装层。本讲关注 `Reg_EnableExtTrig_Addr_c` 寄存器的解码、复位默认值（全 1）以及它如何跨时钟域送到核心的 `EnableExtTrig` 端口。 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址与位索引常量。本讲用到 `Reg_TrigEna_ExtIdx_c`（=0）与 `Reg_EnableExtTrig_Addr_c`（=`0x0030`）。 |
| [testbench/top_tb/top_tb_case5_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd) | 验证「多路外部触发」的测试用例，是本讲代码实践的参照。 |
| [testbench/top_tb/top_tb_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd) | 测试平台公共过程。其中的 `InputSamples` 负责在指定样本时刻向指定外部触发路发脉冲。 |

## 4. 核心概念与源码讲解

### 4.1 Trig_In 信号与两拍流水线

#### 4.1.1 概念说明

记录器最多支持 8 路外部触发，由 generic `TrigInputs_g`（范围 0..8）决定**实际使用的路数**。注意：外部触发路数与数据通道数 `NumOfInputs_g` 相互独立——你可以有 4 路数据、8 路触发，反之亦然（见 u3-l1）。

外部触发端口 `Trig_In` 是一个位宽为 `TrigInputs_g` 的向量，每一位对应一路外部触发输入。为了让上升沿检测稳定、且与流水线对齐，记录器把 `Trig_In` 先寄存两拍（Stage0、Stage1），再用这两拍寄存值做比较。

> 说明：这里的「两拍流水」指用于边沿检测的两个寄存级 Stage0 与 Stage1。record 中 `Trig_In` 数组实际声明为 `TrigIn_t(0 to 2)`（三级），但上升沿检测只用到前两级；第三级随流水线一同移位，外部触发逻辑不再读它。

#### 4.1.2 核心流程

`Trig_In` 从端口到 `ExtTrigPending_2` 的搬运过程：

```text
Trig_In (端口, 组合输入)
   │  p_seq 时钟沿
   ▼
r.Trig_In(0)   ← Stage0：本拍寄存输入（"当前值"）
   │  再经一拍
   ▼
r.Trig_In(1)   ← Stage1：上一拍的 Stage0（"上一拍值"）
   │  比较 Stage0 与 Stage1
   ▼
上升沿脉冲 → 进入 4.2 的边沿检测
```

两拍流水由两段代码协作完成：

- 整体左移（在 `p_comb` 开头的流水搬运段）；
- Stage0 采入（在 `*** Stage 0 ***` 段）。

#### 4.1.3 源码精读

端口声明，位宽随 `TrigInputs_g` 变化：

- [hdl/data_rec.vhd:48](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L48) — `Trig_In : in std_logic_vector(TrigInputs_g-1 downto 0);`，外部触发输入向量。

record 中 `Trig_In` 是一个数组类型，承载多级流水寄存：

- [hdl/data_rec.vhd:89](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L89) — `type TrigIn_t is array(natural range <>) of std_logic_vector(TrigInputs_g-1 downto 0);`，每一路触发都是一个向量，数组下标代表流水级。
- [hdl/data_rec.vhd:98](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L98) — `Trig_In : TrigIn_t(0 to 2);`，三级流水寄存（本讲用 0、1 两级）。

流水搬运（每拍整体左移一级）：

- [hdl/data_rec.vhd:157](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L157) — `v.Trig_In(low+1 to high) := r.Trig_In(low to high-1);`，把 Stage0→Stage1、Stage1→Stage2 平移。

Stage0 采入原始输入：

- [hdl/data_rec.vhd:163](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L163) — `v.Trig_In(0) := Trig_In;`，本拍把端口值打入 Stage0。

封装层一侧，`Trig_In` 同样以 `TrigInputs_g` 位宽对外（直通到核心，不跨域，因为它本就在数据时钟域）：

- [hdl/data_rec_vivado_wrp.vhd:53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L53) — 封装层 `Trig_In` 端口，直连核心（见 [hdl/data_rec_vivado_wrp.vhd:483](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L483) 的 `Trig_In => Trig_In`）。

#### 4.1.4 代码实践

**实践目标**：确认 `Trig_In` 在核心内部确实被寄存了两拍，并理解为什么边沿检测要用两个寄存值而不是直接用端口。

**操作步骤**（源码阅读型）：

1. 打开 `hdl/data_rec.vhd`，定位到 [L157](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L157) 的流水搬运与 [L163](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L163) 的 Stage0 采入。
2. 在脑中画一个时序表：假设 `Trig_In(0)` 在第 5、6 拍为 `'1'`、其余拍为 `'0'`，推演 `r.Trig_In(0)(0)` 与 `r.Trig_In(1)(0)` 在第 5~8 拍的取值。

**需要观察的现象 / 预期结果**（待本地验证）：在第 6 拍，`r.Trig_In(0)(0)=1` 且 `r.Trig_In(1)(0)=0`，恰好满足上升沿条件；第 7 拍两者都为 1，不再满足。也就是一个持续两拍的高电平只产生一次上升沿，这正是边沿触发（而非电平触发）所要求的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Trig_In` 的两拍流水改成只寄存一拍（直接用端口和 Stage0 比较），功能上还正确吗？会有什么隐患？

> **参考答案**：功能上仍能检测到上升沿，但比较的一方是组合输入（端口），容易受到外部信号上的毛刺或布线歪斜影响，可能在时钟沿附近出现亚稳态或瞬时错误值，导致误触发。两拍寄存后，参与比较的两个值都已同步到 `Clk` 时钟域，检测更稳健。

**练习 2**：`Trig_In` 数组声明为 `TrigIn_t(0 to 2)`，即三级，但边沿检测只用 0、1 两级。第三级 `r.Trig_In(2)` 被读取了吗？

> **参考答案**：在 `p_comb` 中没有读取 `r.Trig_In(2)` 的地方，它只是随流水搬运段被一并左移、保持结构对称。综合工具通常会将其优化掉。它不影响外部触发行为。

### 4.2 上升沿检测与 EnableExtTrig 逐路使能

#### 4.2.1 概念说明

两拍流水得到「当前值」`r.Trig_In(0)(i)` 与「上一拍值」`r.Trig_In(1)(i)` 后，对第 `i` 路外部触发做上升沿检测：

\[ \text{Rise}_i \;=\; \text{Trig\_In}^{(0)}_i \;\wedge\; \overline{\text{Trig\_In}^{(1)}_i} \;\wedge\; \text{EnableExtTrig}_i \]

其中 `EnableExtTrig(i)` 是软件通过 AXI 写入的逐路使能位。只有当第 `i` 路被使能、且检测到上升沿时，这一路才算「有效触发」。

关键设计：`EnableExtTrig` 的使能作用在**边沿检测这一级**，而不是作用在最终的 `TrigNow` 上。也就是说，未使能的路根本不会贡献触发，连「待处理（pending）」都不会产生。这一点与 `TrigEna` 不同——`TrigEna` 是三类源之间的总开关（见 u4-l1），而 `EnableExtTrig` 是外部触发**各路之间**的细粒度开关。

#### 4.2.2 核心流程

核心用一个 `for i in 0 to TrigInputs_g-1 loop` 循环把最多 8 路外部触发 OR 在一起：

```text
for i in 0 .. TrigInputs_g-1 loop
    if (Stage0(i)=1) and (Stage1(i)=0) and EnableExtTrig(i)=1 then
        ExtTrigPending_2 := 1        -- 任一路有效上升沿即置位（多路 OR）
    end if;
end loop;
```

这正是「多路 OR」的来源：循环体只把单一的 `ExtTrigPending_2` 置 1，任何一路满足条件都足以置位；多路同时上升沿也只会置位一次。OR 的结果是一个**标量**锁存位，而非向量。

#### 4.2.3 源码精读

外部触发检测整段（本讲最核心的代码）：

- [hdl/data_rec.vhd:207-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L207-L215) — 外部触发块。其中：
  - [L208-210](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L208-L210)：在 `PreTrig_s` 状态下先把 `ExtTrigPending_2` 清 0（详见 4.3）。
  - [L211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215)：`for` 循环逐路做上升沿检测并 OR 置位。

边沿检测判定条件（L212）：

```vhdl
if r.Trig_In(0)(i) = '1' and r.Trig_In(1)(i) = '0' and EnableExtTrig(i) = '1' then
    v.ExtTrigPending_2 := '1';
end if;
```

- `r.Trig_In(0)(i) = '1'`：本拍为高；
- `r.Trig_In(1)(i) = '0'`：上一拍为低 → 合起来即上升沿；
- `EnableExtTrig(i) = '1'`：该路被软件使能。三个条件缺一不可。

`EnableExtTrig` 端口定义：

- [hdl/data_rec.vhd:67](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L67) — `EnableExtTrig : in std_logic_vector(TrigInputs_g-1 downto 0);`，逐路使能向量，注释明确「triggers are ORed」。

封装层把 AXI 写入的寄存器位送到核心：

- [hdl/data_rec_vivado_wrp.vhd:328](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L328) — `reg_enableexttrig <= reg_wdata(ToWordAddr(Reg_EnableExtTrig_Addr_c))(reg_enableexttrig'left downto 0);`，从 `0x0030` 寄存器抽取低 `TrigInputs_g` 位。
- 经 `psi_common_status_cc` 跨时钟域（[L384](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L384)、[L411](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L411)）变成 `port_enableexttrig`，再接核心的 `EnableExtTrig` 端口（[L502](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L502)）。

寄存器地址与默认值：

- [hdl/data_rec_register_pkg.vhd:57](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L57) — `Reg_EnableExtTrig_Addr_c : integer := 16#0030#;`。
- [hdl/data_rec_vivado_wrp.vhd:222-223](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L222-L223) — `RegRstVal_c` 把该寄存器复位默认值设为全 1，注释「Enable all external triggers by default」。

#### 4.2.4 代码实践

**实践目标**：用一个具体场景验证「逐路使能」——只使能第 2 路（VHDL 下标 1），向其余路发脉冲不应触发录制。

**操作步骤**（参照 `top_tb_case5` 的源码阅读型实践，有 Modelsim 环境则可实际运行）：

1. 设 `TrigInputs_g = 4`（与 [top_tb_pkg.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L35) 的 `TriggerInputs_c` 一致）。
2. 配置触发源只允许外部触发：写 `Reg_TrigEna_Addr_c` = `1*2**Reg_TrigEna_ExtIdx_c` = `0x1`（见 [top_tb_case5_pkg.vhd:58](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L58)）。
3. 只使能第 2 路（下标 1）：写 `Reg_EnableExtTrig_Addr_c` = `1*2**1` = `0x2`（对应 [top_tb_case5_pkg.vhd:68](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L68) 中 `ti_ena=1` 的情形）。
4. Arm（写 `Reg_Cfg_Addr_c` 的 bit0，见 [top_tb_case5_pkg.vhd:73](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L73)），等待进入 `WaitTrig`。
5. 分别向 `Trig_In(0)`、`Trig_In(2)`、`Trig_In(3)` 发单拍脉冲（用 `InputSamples(..., trigIdx => k)`，见 [top_tb_pkg.vhd:118-120](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L118-L120)）。
6. 最后向 `Trig_In(1)` 发单拍脉冲。

**需要观察的现象 / 预期结果**：前三个脉冲后，状态寄存器（`Reg_Stat_Addr_c`）仍读出 `Reg_Stat_StateWaitTrig_c`（=2），无 `Done_Irq`，说明未触发；只有 `Trig_In(1)` 的脉冲后状态变为 `Reg_Stat_StateDone_c`（=4）并出现 `Done_Irq`。这正是 [top_tb_case5_pkg.vhd:80-88](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L80-L88) 中 `ti_cur /= ti_ena` 走 `else` 分支断言「Wait Status」、`ti_cur = ti_ena` 断言「Done Status」所验证的行为。若环境不具备，标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：写「同时允许外部 + 自触发」两种源、且只使能外部触发第 0 和第 3 路时，`Reg_TrigEna_Addr_c` 与 `Reg_EnableExtTrig_Addr_c` 各应写什么值？

> **参考答案**：`TrigEna` 的 bit0=Ext、bit2=Self（见 [data_rec_register_pkg.vhd:51-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L51-L53)），故 `Reg_TrigEna` = `2**0 + 2**2` = `0x5`。`EnableExtTrig` 按路使能第 0、3 路 = `2**0 + 2**3` = `0x9`。

**练习 2**：为什么 `EnableExtTrig` 的使能要放在边沿检测的 `if` 条件里，而不是放在最后统一 mask 掉 `ExtTrigPending_2`？

> **参考答案**：放在边沿检测里，未使能的路**根本不会**在 `ExtTrigPending_2` 上留下任何痕迹（连 pending 都不产生）。若改成最后统一 mask，则未使能路的上升沿仍会被 OR 进 pending 并锁存，等软件后续使能该路时会「补触发」一段陈旧的触发，行为不直观、也容易误触发。当前实现保证了「未使能即完全无效」。

### 4.3 ExtTrigPending_2 的锁存、置位与清除

#### 4.3.1 概念说明

`ExtTrigPending_2` 是一个**单 bit 锁存位**（sticky pending）。它与软件触发的 sticky pending（u4-l3 会详述）形似但来源不同：外部触发是**边沿敏感**的——只有上升沿才置位，电平保持高不会重复置位。

「pending（待处理）」的含义是：一个外部触发事件已经发生并被捕捉，但记录器此刻不一定能立刻兑现（例如还处在 PreTrig 阶段没采集够前触发样本）。pending 把这个事件「挂起」，等记录器进入 `WaitTrig` 状态、且 `TrigEna` 允许外部触发、且 `In_Vld` 有效时，再通过 `TrigNow_2` 兑现。

`ExtTrigPending_2` 一共会在**四种情形**下被改写：

| 情形 | 动作 | 触发位置 |
| --- | --- | --- |
| 某路使能的上升沿 | 置 1 | 边沿检测 for 循环 |
| 处于 `PreTrig_s` 状态 | 清 0（每拍） | 进入 PreTrig 即清除 |
| 处于 MinRecPeriod 抑制期 | 清 0 | 丢弃「来得太早」的触发 |
| 复位 `Rst=1` | 清 0 | `p_seq` 同步复位 |

#### 4.3.2 核心流程

`ExtTrigPending_2` 的生命周期：

```text
                 ┌── 上升沿(某使能路) ──► 置 1（锁存 pending）
                 │
ExtTrigPending_2 ├── 处于 PreTrig_s ───► 清 0（重新 Arm 清除陈旧触发）
                 │
                 ├── MinRecPeriod 抑制 ► 清 0（背靠背触发被丢弃）
                 │
                 └── Rst=1 ───────────► 清 0（同步复位）

  兑现路径（只读 pending，不改它）：
  TrigNow_2 = ( ExtTrigPending_2 AND TrigEna(bit0) ) OR (其它源) ) AND In_Vld(1)
```

注意「清 0」与「置 1」在同一次 `p_comb` 中是**顺序执行**的变量赋值：先在 PreTrig 清 0，再在 for 循环里可能又置 1。所以即使身处 PreTrig，只要本拍恰好出现使能的上升沿，pending 仍会被置 1——但由于状态机在 PreTrig 阶段并不检查 pending（它只检查前触发样本数），这个 pending 会一直留到 `WaitTrig` 才兑现。

还要注意一个时序细节：`TrigNow_2` 用的是 `r.ExtTrigPending_2`（**当前寄存值**），而不是本拍刚算出的 `v.ExtTrigPending_2`。因此一次上升沿在第 N 拍把 `v` 置 1 → 第 N+1 拍成为 `r.ExtTrigPending_2=1` → 同一拍才可能驱动 `TrigNow_2=1`。这与 `ExtTrigPending_2` 的 `_2` 后缀（Stage2 对齐）一致。

#### 4.3.3 源码精读

置位（见 4.2.3 的 for 循环，[L211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215)）。

清除一：进入/处于 PreTrig 时清除（学习目标重点）：

- [hdl/data_rec.vhd:208-210](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L208-L210) — `if r.State_2 = PreTrig_s then v.ExtTrigPending_2 := '0'; end if;`。由于 Arm 后状态机必经 PreTrig（[L248-249](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L248-L249)），这条等价于「每次重新 Arm 都把陈旧的外部触发 pending 清掉」。

清除二：MinRecPeriod 抑制期丢弃：

- [hdl/data_rec.vhd:230-241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L230-L241) — 最小录制间隔计数段；其中 [L237](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L237) `v.ExtTrigPending_2 := '0';` 把「来得太早」的外部触发直接丢弃（不延后）。详细机制见 u4-l5。

清除三：同步复位：

- [hdl/data_rec.vhd:383](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L383) — `r.ExtTrigPending_2 <= '0';`，`p_seq` 中 `Rst=1` 时清 0。

兑现路径（外部触发贡献到 `TrigNow_2`）：

- [hdl/data_rec.vhd:225-228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L225-L228) — `TrigNow_2 := (... or (r.ExtTrigPending_2 and TrigEna(Reg_TrigEna_ExtIdx_c)) or ...) and r.In_Vld(1);`。注释标注外部触发为「Edge sensitive trigger」。最终是否兑现，还受 `TrigEna` bit0 与 `In_Vld(1)` 共同门控（u4-l1）。

record 字段声明：

- [hdl/data_rec.vhd:119](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L119) — `ExtTrigPending_2 : std_logic;`，单 bit。

#### 4.3.4 代码实践

**实践目标**：验证「重新 Arm 会清除陈旧的外部触发 pending」——在上一次录制结束后、尚未重新 Arm 时发一个外部触发脉冲，它不应被记入下一次录制。

**操作步骤**（源码阅读 + 思维推演，可结合 case5 运行）：

1. 完成一次正常录制并到达 `Done` 状态（状态码 4）。
2. 在 `Done` 状态下（尚未重新 Arm），向某使能路发一个 `Trig_In` 上升沿脉冲。此时 `r.State_2 = Done_s`，既不是 PreTrig（不会被清），上升沿会把 `v.ExtTrigPending_2` 置 1——**pending 被锁存住了**。
3. 重新 Arm：状态机 `Done_s → PreTrig_s`（[L270-271](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L270-L271)）。进入 PreTrig 的第一拍即命中 [L208-210](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L208-L210) 的清 0 分支，pending 被抹掉（除非该拍又恰好有新的使能上升沿）。
4. 观察：若 PreTrig 期间没有新的触发，记录器会一路走到 `WaitTrig` 并**停留等待**，而不会因为步骤 2 的那个陈旧脉冲提前触发。

**需要观察的现象 / 预期结果**（待本地验证）：步骤 3 之后状态进入 `WaitTrig`（状态码 2）并持续等待，证明陈旧 pending 已被 PreTrig 阶段清除。如果去掉 [L208-210](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L208-L210) 的清除逻辑，则步骤 2 的脉冲会在重新 Arm 后立刻兑现，造成一次「不受控」的提前录制——这正是该清除逻辑存在的意义。

#### 4.3.5 小练习与答案

**练习 1**：外部触发的 pending 是「边沿敏感」的，软件触发的 pending 是「sticky 电平」的（u4-l3）。如果外部触发也设计成电平敏感（`Trig_In(i)='1'` 就持续置 pending），会有什么问题？

> **参考答案**：电平敏感下，只要外部信号保持高电平，每拍都会持续把 pending 置 1；一旦进入 `WaitTrig` 就会立刻触发，且无法区分「我想在某个精确边沿触发」与「信号恰好维持高」。边沿敏感保证了「一个上升沿对应一次触发请求」，更适合精确的事件触发场景，也才支持多路 OR（多个边沿事件被归约成一次请求）。

**练习 2**：步骤「PreTrig 清 0」与「for 循环置 1」在同一拍都执行。若某拍既处于 PreTrig、又恰好出现使能的上升沿，`ExtTrigPending_2` 最终是 0 还是 1？会不会立刻触发录制？

> **参考答案**：因为是顺序变量赋值，清 0 在前、置 1 在后，最终为 1。但**不会立刻触发录制**——状态机在 `PreTrig_s` 分支（[L251-258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L251-L258)）只看前触发样本数是否采满，不检查 `TrigNow_2`；该 pending 会保留到进入 `WaitTrig` 后才兑现（若 `TrigEna`/`In_Vld` 允许）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「**多路外部触发 OR + 逐路使能**」的端到端验证设计。参照 [top_tb_case5_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd)，请你在阅读源码后回答并（有条件则实测）：

1. **逐路使能子任务**：`TrigInputs_g = 4`，写 `Reg_TrigEna = 0x1`、`Reg_EnableExtTrig = 0x4`（仅使能下标 2 这一路）。Arm 后依次向 4 路各发一个上升沿脉冲。哪几路发完后状态仍是 `WaitTrig`（=2）？哪一路发完后变为 `Done`（=4）？
2. **多路 OR 子任务**：写 `Reg_EnableExtTrig = 0xFFFFFFFF`（全使能，见 [top_tb_case5_pkg.vhd:96](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_case5_pkg.vhd#L96)）。每次 Arm 后只向其中一路发脉冲、循环 4 次。预期 4 次都能进入 `Done`——请用 [L211-215](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L211-L215) 的 for 循环解释「任一路都能触发」的代码依据。
3. **代码定位**：用一句话+行号说明，是哪一行代码保证了「向未使能路发脉冲不会触发录制」（答：[hdl/data_rec.vhd:212](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L212) 中的 `and EnableExtTrig(i) = '1'` 这一项）。

> 预期结论：子任务 1 中只有向 `Trig_In(2)` 发脉冲会触发（变 Done），其余三路保持 WaitTrig；子任务 2 中全使能时，任意单路脉冲都触发，体现 OR 语义。无 Modelsim/Questa 环境时标记「待本地验证」，但代码定位题可仅凭源码回答。

## 6. 本讲小结

- 外部触发端口 `Trig_In` 位宽为 `TrigInputs_g`（最多 8 路），与数据通道数相互独立；核心内部把它寄存成 `Trig_In(0 to 2)` 的三级流水，边沿检测只用 Stage0/Stage1 两级。
- 上升沿检测条件是 `r.Trig_In(0)(i)='1' and r.Trig_In(1)(i)='0'`，并 `and EnableExtTrig(i)` 在边沿这一级做逐路使能——未使能的路连 pending 都不产生。
- 一个 `for i in 0 to TrigInputs_g-1` 循环把所有使能路的上升沿 OR 成单一的锁存位 `ExtTrigPending_2`，这正是「多路外部触发 OR」的实现。
- `ExtTrigPending_2` 是边沿敏感的 sticky 锁存：上升沿置 1；在重新 Arm 进入 `PreTrig_s` 时被清除、在 MinRecPeriod 抑制期被丢弃、在复位时清 0。
- `ExtTrigPending_2` 经 `TrigEna(bit0)` 与 `In_Vld(1)` 门控后参与合成 `TrigNow_2`（u4-l1 框架），且因其 `_2` 后缀与 Stage2 对齐——上升沿在下一拍才可能兑现。
- `Reg_EnableExtTrig`（`0x0030`）复位默认全 1（「上电即允许所有外部触发」），经 `status_cc` 跨时钟域送到核心的 `EnableExtTrig` 端口。

## 7. 下一步学习建议

- 接着阅读 [u4-l3 软件触发：sticky pending 行为](u4-l3-software-trigger.md)，对比**软件触发的电平 sticky pending** 与本讲**外部触发的边沿 sticky pending** 的异同，理解 free-running 模式为何依赖软件触发。
- 再读 [u4-l4 自触发：范围检测、符号与进入/退出](u4-l4-self-trigger.md)，看第三类源如何把数据本身变成触发事件。
- 最后读 [u4-l5 最小录制间隔、计数器与 Trig_Out 转发](u4-l5-minrepperiod-counters-trigout.md)，弄清 [L230-241](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L230-L241) 中 MinRecPeriod 如何抑制背靠背的外部触发，以及 `Trig_Out` 如何转发真正兑现的 `Trigger_2`。
- 若想验证本讲行为，可直接运行 [u1-l3](u1-l3-run-simulation.md) 介绍的 PsiSim 回归仿真，重点关注 `top_tb_case5` 的 Transcript 输出。
