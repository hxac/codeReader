# SPI 主控时序与引擎集成

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `ClockDivider_g` 与 `CsHighCycles_g` 这两个参数分别决定了 SPI 哪一段时序，并能估算 SCK 频率与片选高电平最短时长。
- 准确描述 CPOL/CPHA 四种 SPI 模式下「采样边沿」与「切换边沿」的对应关系，并能根据 `spi_simple` 的 generic 推断 SCK 空闲电平。
- 看懂 `spi_simple.vhd` 中 `psi_common_spi_master` 的实例化，理清 generic 与 port 的端到端映射：从顶层 wrapper → `spi_simple` → SPI 引擎。
- 读懂测试平台 `top_tb.vhd` 里 `p_spi` 进程如何用「apply 边沿 / transfer 边沿」仿真一个 SPI 从机，并能据此画出 Mode 0 的 8 bit 时序图。

本讲承接 [u2-l2](u2-l2-spi-core-architecture.md)（核心架构与 SpiStart/SpiBusy/SpiDone 握手），把视线从「命令/响应 FIFO」下沉到「真正的 SPI 物理时序」。

## 2. 前置知识

### 2.1 SPI 是「主从式、串行、同步」总线

SPI（Serial Peripheral Interface）用四根线通信：

| 信号 | 方向（相对 Master） | 作用 |
|------|-------------------|------|
| SCK | 输出 | 串行时钟，由 Master 产生 |
| MOSI | 输出 | Master Out Slave In，主→从数据 |
| MISO | 输入 | Master In Slave Out，从→主数据 |
| CS_n | 输出 | 片选，**低有效**，拉低选中某个从机 |

一次 SPI 传输是「全双工移位」：Master 在每个时钟沿把 MOSI 的一位推给从机，同时在同一位时钟沿把 MISO 的一位收进来。所以 N bit 的传输就是 N 个 SCK 周期，主从各完成一次 N 位移位。

### 2.2 什么是 generic

VHDL 的 `generic` 是「综合期参数」，类似于软件里编译期常量。在 IP 化的场景里，generic 决定了硬件的「形状」（位宽、深度、分频比、极性）。`spi_simple` 把所有时序相关参数都做成 generic，从而能在 Vivado GUI 里被用户配置（详见 [u3-l1](u3-l1-configurable-generics.md)）。

### 2.3 psi_common_spi_master 是「外部引擎」

`spi_simple` 自己**不**实现 SPI 移位时序，而是例化 PSI 生态里的成熟组件 `psi_common_spi_master`（见 [u1-l3](u1-l3-toolchain-and-dependencies.md) 关于 `psi_common` 依赖的说明）。`spi_simple` 的职责是：在引擎外面套上命令 FIFO、响应 FIFO、状态/中断逻辑（u2-l2 讲过），再把用户配置的 generic **原样透传**给引擎。理解这条「透传链」是本讲的核心。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `hdl/spi_simple.vhd` | SPI 核心 RTL | 时序 generic 声明、`psi_common_spi_master` 实例化 |
| `hdl/spi_vivado_wrp.vhd` | 顶层 wrapper | generic 默认值与注释（权威的参数语义说明）、向 `spi_simple` 的透传 |
| `tb/top_tb.vhd` | 测试平台 | DUT 的 generic 实参、`p_spi` 从机仿真进程的边沿逻辑 |

> 说明：真正的移位/分频计数器在 `psi_common_spi_master` 内部（属外部依赖 `psi_common`，不在本仓库），本仓库只能看到「它被怎样例化、参数怎样传」。涉及引擎内部精确计数关系处，我会标注「待本地验证」并指向 `psi_common` 源码。

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**时钟分频与片选时序**、**CPOL/CPHA 四种模式**、**spi_master 实例化映射**。

---

### 4.1 时钟分频与片选时序

#### 4.1.1 概念说明

SPI 的 SCK 频率不能高于从机允许的最大值，也常常需要远低于系统时钟。`spi_simple` 用两个 generic 控制两段不同的时序：

- **`ClockDivider_g`**：决定 SCK 相对系统时钟 `Clk` 的分频比，也就是「SCK 跑多快」。
- **`CsHighCycles_g`**：决定两次连续传输之间，片选 CS_n 必须保持「高（非激活）」状态至少多少个系统时钟周期，也就是「两次访问之间的最小间隔」。

两者单位都是**系统时钟 `Clk` 的周期数**，这是理解它们的关键。

#### 4.1.2 核心流程

设系统时钟周期为 \( T_{\text{clk}} \)，频率为 \( f_{\text{clk}} \)。

- SCK 频率：`ClockDivider_g` 的 wrapper 注释明确写「**Must be a multiple of two**」（必须是 2 的倍数）。这强烈暗示 SCK 的一个完整周期占用 `ClockDivider_g` 个 `Clk` 周期，其中高、低电平各占一半（`ClockDivider_g/2`），因此：

\[
f_{\text{sck}} \approx \frac{f_{\text{clk}}}{\text{ClockDivider\_g}}
\]

- 片选最短高电平时间：

\[
T_{\text{cs\_high}} = \text{CsHighCycles\_g} \times T_{\text{clk}}
\]

> 引擎内部用计数器实现这些分频，确切的计数值（是否含 ±1 偏移、是否每半拍都计数）取决于 `psi_common_spi_master` 的实现，**待本地验证**；但「`ClockDivider_g` 越大 → SCK 越慢」「`CsHighCycles_g` 越大 → 两次传输间隔越久」是确定的。

#### 4.1.3 源码精读

`spi_simple` 实体里这两个 generic 的声明（注意 `spi_simple` 本身没有给默认值，默认值由 wrapper 给）：

- [hdl/spi_simple.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L29) — `ClockDivider_g : natural range 4 to 1_000_000`，取值范围 4 到一百万，决定了 SCK 的可调范围。
- [hdl/spi_simple.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L31) — `CsHighCycles_g : positive`，片选高电平最小周期数。

更权威的语义说明在 wrapper 的注释里（wrapper 给了默认值并标注用途）：

- [hdl/spi_vivado_wrp.vhd:27](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L27) — `ClockDivider_g := 4`，注释「Must be a multiple of two」。
- [hdl/spi_vivado_wrp.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L29) — `CsHighCycles_g := 20`，注释「Minimum chip-select high-time between two transfers in clock-cycles」。

测试平台的实参与系统时钟频率（仿真里为了跑得快，系统时钟故意取 125 MHz）：

- [tb/top_tb.vhd:58](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L58) — `ClockFrequencyAxi_c : real := 125.0e6`，即 \( T_{\text{clk}} = 8\,\text{ns} \)。
- [tb/top_tb.vhd:89](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L89) — `ClockDivider_g => 20`。
- [tb/top_tb.vhd:91](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L91) — `CsHighCycles_g => 50`。

代入估算：\( f_{\text{sck}} \approx 125\,\text{MHz}/20 = 6.25\,\text{MHz} \)；\( T_{\text{cs\_high}} = 50 \times 8\,\text{ns} = 400\,\text{ns} \)。

#### 4.1.4 代码实践

**实践目标**：体会两个参数对时序尺度的影响。

**操作步骤**：

1. 打开 `tb/top_tb.vhd`，确认 DUT 例化的 `ClockDivider_g => 20`、`CsHighCycles_g => 50`。
2. 在脑中（或用计算器）算出 SCK 周期 ≈ 160 ns、CS 高电平最短 400 ns。
3. 把 `ClockDivider_g` 改成 `4`（最小值），重新估算 \( f_{\text{sck}} \approx 31.25\,\text{MHz} \)；再把 `CsHighCycles_g` 改成 `2`，估算 \( T_{\text{cs\_high}} = 16\,\text{ns} \)。
4. 若本地有 Modelsim/Questa，按 [u1-l4](u1-l4-simulation-and-regression.md) 跑 `sim/run.tcl`，在波形窗口用游标测量 `spi_sck` 的相邻上升沿间隔，与估算值对比。

**需要观察的现象**：SCK 周期随 `ClockDivider_g` 线性变化；两次传输之间 `spi_cs_n` 的高电平宽度随 `CsHighCycles_g` 线性变化。

**预期结果**：`ClockDivider_g=20` 时测得 SCK 周期约 160 ns（±1 个 `Clk`，因为引擎计数实现**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：系统时钟 100 MHz，要把 SCK 跑到 5 MHz，`ClockDivider_g` 应取多少？是否需要是 2 的倍数？

**答案**：\( 100\,\text{MHz}/5\,\text{MHz} = 20 \)。取 `ClockDivider_g => 20`。是，必须为 2 的倍数。

**练习 2**：为什么 `CsHighCycles_g` 的单位是「系统时钟周期」而不是「SCK 周期」？

**答案**：因为 CS_n 的去断言、再断言都由引擎在 `Clk` 域里用计数器控制，与 SCK 的生成是同一个计数基准；用 `Clk` 周期数描述最直接，也避免 SCK 还没生成时无法计数。

---

### 4.2 CPOL/CPHA 模式与采样边沿

#### 4.2.1 概念说明

SCK 是一个方波，但「空闲时是高还是低」「在哪个边沿采样数据」并不唯一。SPI 用两位约定出 4 种模式：

- **CPOL（Clock Polarity，时钟极性）**：SCK 空闲时的电平。`0` = 空闲低，`1` = 空闲高。
- **CPHA（Clock Phase，时钟相位）**：在哪个边沿采样（读）数据。wrapper 注释写得很明确：`0` = 在**前导边沿（leading edge）**采样，`1` = 在**后随边沿（trailing edge）**采样。

由此，另一个边沿就是「切换/输出」边沿（主从都在那里改变自己要发的下一位）。

> 前导边沿 = 一个 SCK 脉冲的第一个边沿；后随边沿 = 第二个边沿。CPOL=0（空闲低）时，脉冲是「低→高→低」，前导=上升沿、后随=下降沿；CPOL=1（空闲高）时，脉冲是「高→低→高」，前导=下降沿、后随=上升沿。

#### 4.2.2 核心流程：四种模式对照

| 模式 | CPOL | CPHA | SCK 空闲 | 采样边沿 | 切换边沿 | 第一位数据何时呈现 |
|------|------|------|---------|---------|---------|------------------|
| 0 | 0 | 0 | 低 | 前导=上升 | 后随=下降 | 第一个 SCK 沿**之前**就稳定 |
| 1 | 0 | 1 | 低 | 后随=下降 | 前导=上升 | 在前导边沿上切换出 |
| 2 | 1 | 0 | 高 | 前导=下降 | 后随=上升 | 第一个 SCK 沿**之前**就稳定 |
| 3 | 1 | 1 | 高 | 后随=上升 | 前导=下降 | 在前导边沿上切换出 |

记忆窍门：**CPHA=0 → 采样在前导沿（数据提前就位）；CPHA=1 → 采样在后随沿（数据在前导沿才切换出来）**。这与 wrapper 注释完全一致。

#### 4.2.3 源码精读

`spi_simple` 的实体声明（注意：默认值与语义说明在 wrapper 里）：

- [hdl/spi_simple.vhd:32-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L32-L33) — `SpiCPOL_g`、`SpiCPHA_g`，`natural range 0 to 1`。

wrapper 给出最权威的语义注释：

- [hdl/spi_vivado_wrp.vhd:30](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L30) — `SpiCPOL_g := 0`，注释「0 = idle low, 1 = idle high」。
- [hdl/spi_vivado_wrp.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L31) — `SpiCPHA_g := 0`，注释「0 sample on leading edge, 1 sample on trailing edge」。

测试平台用 `p_spi` 进程仿真一个 SPI 从机，它的「apply 边沿 / transfer 边沿」正是 CPHA/CPOL 的直接编码。先看它的本地常量（testbench 把模式硬编码成 Mode 0 来做对照仿真）：

- [tb/top_tb.vhd:323-326](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L323-L326) — `SpiCPHA_c := 0`、`SpiCPOL_c := 0`、`LsbFirst_c := false`。

**apply 边沿**（从机在此沿之后把下一位推到 MISO，使数据在采样沿之前稳定）：

- [tb/top_tb.vhd:352-365](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L352-L365) — CPHA=1 时在前导沿 apply（CPOL=0 上升、CPOL=1 下降），且跳过最后一位；CPHA=0 时在后随沿 apply（CPOL=0 下降、CPOL=1 上升），且跳过第一位（因为第一位在 CS 一拉低就要立刻就位）。

紧接其后从机驱动 MISO 并左移（MSB first 时取最高位）：

- [tb/top_tb.vhd:366-373](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L366-L373) — `spi_miso <= ShiftRegTx_v(MSB)`。

**transfer 边沿**（从机在此沿采样 MOSI）：

- [tb/top_tb.vhd:374-380](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L374-L380) — `(CPOL=0,CPHA=0)` 或 `(CPOL=1,CPHA=1)` 时在上升沿采样；否则在下降沿采样。这正对应「Mode 0/3 上升沿采样、Mode 1/2 下降沿采样」。

随后把 MOSI 的一位并入接收移位寄存器：

- [tb/top_tb.vhd:381-386](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L381-L386) — `ShiftRegRx_v := ShiftRegRx_v(...) & spi_mosi`。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的模式表与 testbench 代码逐条对上，验证「表 = 代码」。

**操作步骤**：

1. 打开 [tb/top_tb.vhd:352-380](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L352-L380)。
2. 对 Mode 0（`SpiCPOL_c=0, SpiCPHA_c=0`），代入 apply 段：因 CPHA=0 且 `i/=0`，进入 `elsif` 分支，CPOL=0 → `wait until falling_edge(spi_sck)`；即「在下降沿切换 MISO」。代入 transfer 段：`(CPOL=0,CPHA=0)` → `wait until rising_edge(spi_sck)`；即「在上升沿采样 MOSI」。
3. 把同样的代入法用在 Mode 1/2/3 上，逐格填出一张「apply 边沿 / transfer 边沿」表。
4. 与 4.2.2 的对照表比对，确认每一行都吻合。

**需要观察的现象**：四种模式都能由这两段 `if/elsif` 完整推导，无歧义。

**预期结果**：Mode 0 apply=下降、transfer=上升；Mode 1 apply=上升、transfer=下降；Mode 2 apply=上升、transfer=下降；Mode 3 apply=下降、transfer=上升。与你从模式表推得的结论一致。

#### 4.2.5 小练习与答案

**练习 1**：某个 SPI EEPROM 数据手册要求「SCK 空闲高，在下降沿采样数据」。应配置 `SpiCPOL_g`、`SpiCPHA_g` 各为多少？属于哪种模式？

**答案**：空闲高 → CPOL=1；下降沿采样。CPOL=1 时前导沿=下降，后随沿=上升，下降沿即前导沿 → CPHA=0。这是 Mode 2。

**练习 2**：为什么 CPHA=0 时，`p_spi` 的 apply 段要跳过 `i=0`？

**答案**：CPHA=0 在第一个（前导）边沿就要采样，意味着第一位必须在第一个 SCK 沿出现之前就稳定在 MOSI/MISO 上。Master 在 CS 拉低后、第一个 SCK 沿之前就输出了 b7，从机也必须在 CS 拉低后立刻把 b7 推到 MISO，不能等任何边沿，所以 `i=0` 时跳过 apply 等待、直接驱动。

---

### 4.3 spi_master 实例化与 generic/port 映射

#### 4.3.1 概念说明

`spi_simple` 不亲自做 SPI 移位，而是把所有时序 generic 与握手信号接到 `psi_common_spi_master` 上。理解这条连接，就理解了「参数如何从顶层用户一路流到物理 SCK」。

#### 4.3.2 核心流程：端到端透传链

```
Vivado GUI / top_tb 实参
        │  (generic)
        ▼
spi_vivado_wrp.vhd  ──  wrapper 接收 generic，注释里给出语义
        │  ClockDivider_g => ClockDivider_g  (同名透传)
        ▼
spi_simple.vhd  ──  套 FIFO / 状态 / 中断
        │  clk_div_g => ClockDivider_g  (改名透传给引擎)
        ▼
psi_common_spi_master  ──  真正生成 SCK、移位、CS 时序（外部依赖）
```

握手层面（回顾 u2-l2）：`spi_simple` 用 `r.SpiStart` 单周期脉冲启动一次事务，引擎拉高 `SpiBusy`，事务结束后拉高 `SpiDone` 一个周期。

#### 4.3.3 源码精读

wrapper 把同名 generic 直接透传给 `spi_simple`：

- [hdl/spi_vivado_wrp.vhd:215-222](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L215-L222) — `ClockDivider_g => ClockDivider_g`、`CsHighCycles_g => CsHighCycles_g`、`SpiCPOL_g => SpiCPOL_g`、`SpiCPHA_g => SpiCPHA_g`、`LsbFirst_g => LsbFirst_g`、`MosiIdleState_g => MosiIdleState_g` 等。

`spi_simple` 把这些 generic **改名**后透传给引擎（注意 generic 名从 `SpiCPOL_g` 变成 `spi_cpol_g`，从 `ClockDivider_g` 变成 `clk_div_g`）：

- [hdl/spi_simple.vhd:271-284](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L271-L284) — 引擎的 generic 映射。关键几行：

```vhdl
i_spi : entity work.psi_common_spi_master
    generic map (
        clk_div_g          => ClockDivider_g,    -- SCK 分频
        trans_width_g      => TransWidth_g,      -- 每帧位数
        cs_high_cycles_g   => CsHighCycles_g,    -- CS 高电平最小周期
        spi_cpol_g         => SpiCPOL_g,         -- 时钟极性
        spi_cpha_g         => SpiCPHA_g,         -- 时钟相位
        slave_cnt_g        => SlaveCnt_g,        -- 从机数（决定 CS_n 宽度）
        lsb_first_g        => LsbFirst_g,        -- MSB/LSB 优先
        mosi_idle_state_g  => MosiIdleState_g    -- MOSI 空闲电平
        -- read_bit_pol_g / tri_state_pol_g / spi_data_pos_g 见 u3-l2（3-Wire 扩展）
    )
```

port 映射把启动/数据/物理线接到引擎：

- [hdl/spi_simple.vhd:285-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L285-L299) — `start_i => r.SpiStart`、`slave_i => CmdSlave`、`busy_o => SpiBusy`、`done_o => SpiDone`、`dat_i => CmdData`、`dat_o => SpiRxData`、`spi_sck_o => SpiSck`、`spi_mosi_o => SpiMosi`、`spi_miso_i => SpiMiso`、`spi_cs_n_o => SpiCs_n`。

其中握手三件套（详见 u2-l2）：

- [hdl/spi_simple.vhd:288-291](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L288-L291) — `start_i => r.SpiStart`（启动）、`busy_o => SpiBusy`（占用中）、`done_o => SpiDone`（完成脉冲）。

> 关于未在「时序」主线上的 generic：`ReadBitPol_g`、`TriStatePol_g`、`SpiDataPos_g` 服务于 3-Wire SPI 扩展，由 [u3-l2](u3-l2-three-wire-spi.md) 专门讲解；这里只需知道它们同样在 [hdl/spi_simple.vhd:280-282](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L280-L282) 透传给引擎。

#### 4.3.4 代码实践

**实践目标**：沿透传链反向追踪一个参数，确认「顶层实参 → 引擎 generic」一路无丢失。

**操作步骤**：

1. 选 `CsHighCycles_g`。在 `tb/top_tb.vhd:91` 看到 DUT 实参 `CsHighCycles_g => 50`。
2. 跳到 [hdl/spi_vivado_wrp.vhd:217](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L217)，确认 wrapper 把 `CsHighCycles_g => CsHighCycles_g` 传给 `spi_simple`。
3. 跳到 [hdl/spi_simple.vhd:275](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L275)，确认 `cs_high_cycles_g => CsHighCycles_g` 传给引擎。
4. 用同样方法追踪 `SpiCPHA_g`（testbench → wrapper L219 → spi_simple L277 `spi_cpha_g`）和 `LsbFirst_g`（testbench → wrapper L221 → spi_simple L279 `lsb_first_g`）。

**需要观察的现象**：每一个时序 generic 都能形成完整链路，没有断点、没有改名导致的错连。

**预期结果**：六个时序相关 generic（`ClockDivider/TransWidth/CsHighCycles/SpiCPOL/SpiCPHA/LsbFirst/MosiIdleState`）在 wrapper↔spi_simple↔engine 三层之间一一对应。

#### 4.3.5 小练习与答案

**练习 1**：`spi_simple` 传给引擎的 generic 叫 `clk_div_g`，而自己的 generic 叫 `ClockDivider_g`。为什么会有改名？

**答案**：两层属于不同命名规范——`spi_simple`/wrapper 用 PSI IP 习惯的 `XxxYyy_g` 大驼峰命名，而 `psi_common_spi_master` 作为 `psi_common` 公共组件用 `xxx_yyy_g` 小写下划线命名。改名透传让两边各自保持自己的风格，generic map 的 `=>` 正是用来做这种「换名对接」。

**练习 2**：若用户在 Vivado GUI 把 `ClockDivider_g` 从 20 改成 40，引擎会看到什么？

**答案**：经 wrapper→`spi_simple` 两层透传，引擎的 `clk_div_g` 得到 40，SCK 频率约下降到原来的一半（\( f_{\text{sck}} \approx f_{\text{clk}}/40 \)）。

---

## 5. 综合实践

**任务**：为 testbench 里「Write/Read Transaction」场景（[tb/top_tb.vhd:192-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L192-L205)，Master 发 0x12、Slave 回 0x34），画出 **Mode 0（CPOL=0/CPHA=0）、MSB first** 下完整 8 bit 的 `CS_n / SCK / MOSI / MISO` 时序图，并说明每个采样沿主从各自捕获到哪一位。

**操作步骤**：

1. 由 [tb/top_tb.vhd:324-326](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L324-L326) 确认仿真模式 = Mode 0、MSB first。
2. 展开两个发送字节（MSB 在前）：
   - Master 发 0x12 = `0b0001_0010` → 位序列 b7..b0 = `0,0,0,1,0,0,1,0`。
   - Slave 发 0x34 = `0b0011_0100` → 位序列 b7..b0 = `0,0,1,1,0,1,0,0`。
3. 由 4.2 得：Mode 0 在**上升沿采样**、**下降沿切换**；第一位 b7 在首个 SCK 沿之前就位。

**理想波形（示意，未按比例；SCK 一个完整周期 ≈ `ClockDivider_g` 个 `Clk`）**：

```
          idle   b7    b6    b5    b4    b3    b2    b1    b0    idle
CS_n : ‾‾‾‾‾‾\________________________________________________________/‾‾‾‾‾
            | assert(low)                                     deassert
            | b7 就位                                          CS_n 回高
SCK  : _____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|_____|‾‾‾‾‾|___
            ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T   ↑ L   ↓ T
           sample sample sample sample sample sample sample sample
            b7    b6    b5    b4    b3    b2    b1    b0
MOSI :       0     0     0     1     0     0     1     0      (0x12, MSB first)
MISO :       0     0     1     1     0     1     0     0      (0x34, MSB first)

L = 前导沿（Mode 0 下为上升沿，采样）；T = 后随沿（Mode 0 下为下降沿，切换下一位）
```

**逐拍采样表**（更易读）：

| SCK 上升沿 | MOSI 位（Master→Slave） | MISO 位（Slave→Master） | 主采样到 | 从采样到 |
|-----------|----------------------|----------------------|---------|---------|
| 第 1 拍 | b7=0 | b7=0 | MISO b7=0 | MOSI b7=0 |
| 第 2 拍 | b6=0 | b6=0 | MISO b6=0 | MOSI b6=0 |
| 第 3 拍 | b5=0 | b5=1 | MISO b5=1 | MOSI b5=0 |
| 第 4 拍 | b4=1 | b4=1 | MISO b4=1 | MOSI b4=1 |
| 第 5 拍 | b3=0 | b3=0 | MISO b3=0 | MOSI b3=0 |
| 第 6 拍 | b2=0 | b2=1 | MISO b2=1 | MOSI b2=0 |
| 第 7 拍 | b1=1 | b1=0 | MISO b1=0 | MOSI b1=1 |
| 第 8 拍 | b0=0 | b0=0 | MISO b0=0 | MOSI b0=0 |

- 8 拍后 Master 收齐 MISO = `0b0011_0100` = 0x34，与 testbench 在 [tb/top_tb.vhd:205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L205) 的 `axi_single_expect(..., 16#34#, ...)` 断言一致。
- 从机收齐 MOSI = `0b0001_0010` = 0x12，与 [tb/top_tb.vhd:392](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L392) 的 `StdlvCompareStdlv (ExpLatch_v, ShiftRegRx_v, ...)`（`ExpLatch_v` 来自 `ExpectedSlaveRx <= X"12"`）一致。

**若本地有仿真器**：按 [u1-l4](u1-l4-simulation-and-regression.md) 跑 `sim/run.tcl`，把 `spi_sck/spi_cs_n/spi_mosi/spi_miso` 加入波形窗口，放大到该场景的一次传输，与上图逐位核对；如不便于运行，本任务即作为「源码阅读 + 推演型实践」，结论已由 testbench 断言保证。

## 6. 本讲小结

- `ClockDivider_g`（必须为 2 的倍数，4–1,000,000）决定 SCK 频率，近似 \( f_{\text{sck}} \approx f_{\text{clk}}/\text{ClockDivider\_g} \)；`CsHighCycles_g`（以 `Clk` 周期计）决定两次传输间 CS_n 的最小高电平时长。
- CPOL 决定 SCK 空闲电平（0=低、1=高），CPHA 决定采样边沿（0=前导沿、1=后随沿），二者组合出 Mode 0/1/2/3；wrapper 注释是这套语义的权威出处。
- testbench 的 `p_spi` 进程用「apply 边沿（驱动 MISO）/ transfer 边沿（采样 MOSI）」两段 `if/elsif` 把四种模式精确编码出来，可用代入法逐模式验证。
- `spi_simple` 不实现移位，而是把时序 generic **改名透传**给 `psi_common_spi_master`（`ClockDivider_g→clk_div_g`、`SpiCPOL_g→spi_cpol_g` 等），握手用 `start_i/busy_o/done_o`。
- 整条透传链为：Vivado 实参 → `spi_vivado_wrp`（同名透传）→ `spi_simple`（改名透传）→ `psi_common_spi_master`（真正生成 SCK）。
- Mode 0 是「空闲低、上升沿采样、下降沿切换」，第一位在首个 SCK 沿之前就位——这是 testbench 默认仿真模式，也是综合实践时序图的依据。

## 7. 下一步学习建议

- 下一讲 [u2-l5](u2-l5-fifo-and-backpressure.md) 转 FIFO 缓冲与背压：当 SPI 引擎被 `ClockDivider_g` 拖慢时，命令/响应 FIFO 如何吸收 AXI 与 SCK 的速度差。
- 若想深挖物理时序的精确计数实现，建议拉取依赖 `psi_common`（见 [u1-l3](u1-l3-toolchain-and-dependencies.md)），阅读其中 `psi_common_spi_master.vhd` 的分频计数器与 CS 控制状态机，验证本讲标注「待本地验证」的精确 SCK 周期公式。
- 想了解 3-Wire SPI 如何复用单根数据线与本讲的 `spi_tri`/`ReadBitPol_g` 等参数，继续看 [u3-l2](u3-l2-three-wire-spi.md)；想了解 LE（锁存使能）输出时序，看 [u3-l3](u3-l3-latch-enable-output.md)。
