# 跨时钟域：status_cc 与 pulse_cc 策略

## 1. 本讲目标

本讲聚焦封装层 `data_rec_vivado_wrp` 如何在**两个时钟域**之间安全地搬运信号：

- **数据时钟域** `Clk`（核心记录器 `data_rec` 与采集逻辑所在的域）；
- **AXI 时钟域** `s00_axi_aclk`（AXI4 Slave 寄存器/存储解码所在的域，也是主机读写的域）。

学完后你应当能够：

1. 区分**状态型（status_cc）**与**脉冲型（pulse_cc）**两类跨时钟域元件的适用场景；
2. 说出封装层里 4 个 CC 实例（status 两路 + pulse 两路）各自的源/目的域，以及每路承载了哪些信号；
3. 解释 `Done` 中断如何经 `pulse_cc` 从数据域跨到 AXI 域、最终产生 `Done_Irq`；
4. 说清楚为什么 `Arm` 必须走 `pulse_cc`、而 `SwTrig` 却可以走 `status_cc`——即「事件」与「电平」的本质区别。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个基础直觉。

### 2.1 为什么需要跨时钟域（CDC）

当发送方用时钟 A、接收方用时钟 B，且 A、B 异步（频率不同、相位关系不固定）时，接收方的触发器直接采样发送方的信号，可能正好采到信号的翻转瞬间。此时触发器内部会发生**亚稳态（metastability）**：输出在一段时间内既不是干净的 0 也不是干净的 1，最终随机收敛到 0 或 1，而且收敛时间不确定。这会导致后级逻辑看到「毛刺」「数据撕裂」「控制错乱」。

标准对策是插入**同步器**：用两级（或多级）级联触发器把亚稳态「等」过去。但同步器只能保证「电平最终稳定」，它不保证「数据正确」——多比特总线若各比特分别同步，各比特到达时间略有差异，就会读到**新旧比特混拼**的错误值。因此多比特数据不能逐比特同步，必须用专门的元件。

### 2.2 两类信号，两种元件

跨时钟域的信号本质上只有两类，封装层据此选用了两种 psi_common 元件：

| 信号性质 | 例子 | 含义 | 选用元件 |
| --- | --- | --- | --- |
| **电平 / 多比特值** | 状态码、计数器、配置寄存器 | 信号会在较长时间内保持某个稳定值，跨域后只要取到一个完整、一致的快照即可 | `psi_common_status_cc` |
| **单拍事件 / 脉冲** | Arm（启动）、Ack（确认）、Done（完成） | 信号只在高电平「一个时钟周期」内有效，代表「这件事发生了一次」，跨域后必须**精确复制成一次**脉冲 | `psi_common_pulse_cc` |

一句话记忆：**「值」走 status，「事件」走 pulse**。本讲要回答的核心问题，就是封装层如何把二三十个信号正确地归入这两类、并把它们打包进 4 个 CC 实例。

> 说明：`psi_common_status_cc` 与 `psi_common_pulse_cc` 的实现在外部依赖库 `psi_common` 中，不在本仓库内。本讲依据它们在本文件中的**实例化方式与端口用法**来描述其行为，不臆测其内部 RTL。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它是整个封装层：

| 文件 | 作用 |
| --- | --- |
| [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) | 顶层封装。实例化 AXI 解码、4 个 CC、核心记录器 `data_rec` 与每通道双口 RAM。本讲的全部内容都在其中第 166–202 行（CC 信号声明）、第 228 行（复位）、第 349–455 行（4 个 CC 实例）以及第 313、316、325 行（解码）之间。 |

寄存器地址常量来自 [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd)（详见 u2-l2），本讲会引用其中 `Reg_Stat_Addr_c`、`Reg_Stat_StateDone_c`、`Reg_Cfg_ArmIdx_c`、`Reg_SwTrig_TrigIdx_c` 等。

## 4. 核心概念与源码讲解

### 4.1 跨时钟域策略总览：status_cc 与 pulse_cc

#### 4.1.1 概念说明

封装层一共有 **4 个跨时钟域实例**，按「方向」和「性质」组成一个 2×2 矩阵：

|  | **AXI → Data**（主机写给核心） | **Data → AXI**（核心回报给主机） |
| --- | --- | --- |
| **status（电平/值）** | `i_cc_status_fromAxi`：所有配置寄存器 | `i_cc_status_toAxi`：状态码 + 两个诊断计数器 |
| **pulse（单拍事件）** | `i_cc_pulse_fromAxi`：Arm / Ack / TrigCntClr | `i_cc_pulse_toAxi`：Done |

这个矩阵是本讲的总纲。记忆方法是先看「方向」再看「性质」：

- **主机 → 核心**几乎都是配置（电平），唯独 Arm、Ack、TrigCntClr 三个是「点一下就够」的事件，因此配置走 status、三个动作走 pulse，两路并行。
- **核心 → 主机**几乎都是供主机读取的值（状态码、触发次数、Done 持续时长），唯独 Done 是「录制完成」的一次性事件，因此值走 status、Done 走 pulse，两路并行。

#### 4.1.2 核心流程

把封装层的信号流画成一张图，就是下面这样（`Clk`=数据域，`axi`=AXI 域）：

```
        AXI 域 (s00_axi_aclk)                         数据域 (Clk)
        ================                             ============

   reg_wdata/reg_wr ──(解码)──┬─> PreTrig/TotSpl/... ─status_fromAxi─> port_pretrig/...
                              │                      (电平,多比特)
                              ├─> SwTrig(电平) ───────status_fromAxi──> port_swtrig
                              │
                              └─> Arm/Ack/TrigCntClr ─pulse_fromAxi──> port_cfg_arm/ack/...
                                                     (单拍脉冲)


   port_stat_state/            ┌─ status_toAxi ──────> reg_stat_state/...
   port_trigcnt/port_donetime ─┘  (电平,多比特)        └─> 喂回 reg_rdata 供主机读

   port_done ─────────────────── pulse_toAxi ────────> Done_Irq (中断给主机)
                                  (单拍脉冲)
```

注意几个细节，后面会逐一落到源码：

1. status_cc 承载的是**多比特总线**，所以要把多个信号「拼」进一个宽向量里一起过同步器，到了对端再「拆」开（见 4.4）。
2. pulse_cc 承载的是**若干独立单比特脉冲**，每个脉冲各占 1 位，互不影响。
3. 数据域的复位 `Rst` 经 `status_toAxi` 产出一份处理后复位 `RstProc`，再喂给核心 `data_rec`（见 4.2.3）。
4. AXI 域复位是低有效的 `s00_axi_aresetn`，封装层在第 228 行取反成高有效的 `AxiRst`。

#### 4.1.3 源码精读

CC 实例集中在一个标注好的区段，先看它的标题与复位准备：

封装层先把 AXI 的低有效复位翻成高有效，供所有 AXI 域的 CC 实例使用：[hdl/data_rec_vivado_wrp.vhd:L228](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L228)

```vhdl
AxiRst <= not s00_axi_aresetn;
```

随后是整个 Clock Crossings 区段的总标题：[hdl/data_rec_vivado_wrp.vhd:L345-L349](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L345-L349)

4 个实例在源码中出现的顺序为：status_toAxi（L354）→ status_fromAxi（L386）→ pulse_fromAxi（L418）→ pulse_toAxi（L440）。这 4 处是本讲全部源码精读的锚点，下面分模块展开。

#### 4.1.4 代码实践（阅读型：定位 4 个实例）

1. **目标**：在源码中亲眼确认 4 个 CC 实例的存在与方向。
2. **步骤**：打开 `hdl/data_rec_vivado_wrp.vhd`，搜索 `psi_common_status_cc` 与 `psi_common_pulse_cc`，各应命中 2 次。
3. **观察**：对每个实例，记录它的 `a_clk_i` 和 `b_clk_i` 分别接到 `Clk` 还是 `s00_axi_aclk`，从而判断源域与目的域。
4. **预期**：`status_toAxi` 与 `pulse_toAxi` 的 `a_clk_i` 接 `Clk`（数据域为源）；`status_fromAxi` 与 `pulse_fromAxi` 的 `a_clk_i` 接 `s00_axi_aclk`（AXI 域为源）。

#### 4.1.5 小练习与答案

**练习 1**：如果要把一个「核心持续输出的 32 位触发计数」送回主机读出，该用 status_cc 还是 pulse_cc？为什么？
**答案**：status_cc。它是持续保持的多比特「值」，需要读到一致快照，属于电平/值类；pulse_cc 只适合单拍事件，且通常按位独立、不适合多比特整体值。

**练习 2**：4 个 CC 实例里，哪些是「AXI 域为源（a_clk_i = s00_axi_aclk）」？
**答案**：`i_cc_status_fromAxi` 与 `i_cc_pulse_fromAxi`（它们把主机写下来的配置/动作送到数据域）。

---

### 4.2 psi_common_status_cc：两路 status 实例

#### 4.2.1 概念说明

`psi_common_status_cc` 用于**多比特电平/值**的跨域。它的核心思想是：在源域先把待传数据打一拍（保证数据相对源时钟已稳定、且与一个源时钟边沿对齐），再用一个握手/请求位告诉目的域「可以采样了」，目的域用同步器采到请求后，对整个多比特总线做一次**整体采样**。这样目的域拿到的永远是一个内部各比特「同一拍」的一致快照，避免了多比特撕裂。

封装层用了两路 status：

- **`i_cc_status_toAxi`（数据 → AXI）**：把核心产出的状态码与诊断计数器送到 AXI 域，供主机经寄存器回读。
- **`i_cc_status_fromAxi`（AXI → 数据）**：把主机写下的全部配置送到数据域，供核心使用。

#### 4.2.2 核心流程

**ToAxi 路**承载 3 个值，拼成一个 68 位宽向量：

\[ \text{width} = \underbrace{4}_{\text{StatState}} + \underbrace{32}_{\text{TrigCnt}} + \underbrace{32}_{\text{DoneTime}} = 68 \]

源端（数据域）把 `port_stat_state`、`port_trigcnt`、`port_donetime` 按 `subtype` 切片塞进 `CcSToAxIn`；目的端（AXI 域）从 `CcSToAxOut` 按同样切片拆出，赋给 `reg_stat_state`/`reg_trigcnt`/`reg_donetime`，后者再由第 312、338、339 行回写到 `reg_rdata` 供主机读取。

**FromAxi 路**承载所有配置（PreTrig、TotSpl、SelfTrigLo/Hi/ChEna/OnExit/OnEnter、TrigEna、**SwTrig**、MinRecPeriod、EnableExtTrig），总宽随 generics 变化。源端（AXI 域）打包进 `CcSFromAxIn`，目的端（数据域）拆出赋给 `port_*` 系列信号，再接到核心端口。

> 这里有一个关键对比点（在 4.1 与练习中也会用到）：**`SwTrig` 走的是 status_cc**，因为它在封装层被解码成**电平**（见 4.4.3 与第 325 行），具有 sticky 粘滞语义（详见 u4-l3）；而 `Arm` 走的是 pulse_cc。

#### 4.2.3 源码精读

先看 ToAxi 路的信号声明与切片定义：[hdl/data_rec_vivado_wrp.vhd:L166-L172](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L166-L172)

```vhdl
-- Status CC to Axi
subtype CcSToAxi_StatState_Rng is natural range 3 downto 0;
subtype CcSToAxi_TrigCnt_Rng   is natural range CcSToAxi_StatState_Rng'left+32 downto CcSToAxi_StatState_Rng'left+1;
subtype CcSToAxi_DoneTime_Rng  is natural range CcSToAxi_TrigCnt_Rng'left+32 downto CcSToAxi_TrigCnt_Rng'left+1;
constant CcSToAxi_Width_c      : natural := CcSToAxi_DoneTime_Rng'high+1;
```

这是「链式」宽度定义：每个字段的范围都从前一个字段的 `'left`（即高位）往上接，`Width` 取最高位 +1。代入数字即得 68 位。

打包（源端 = 数据域）与实例化：[hdl/data_rec_vivado_wrp.vhd:L350-L367](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L350-L367)

```vhdl
CcSToAxIn(CcSToAxi_StatState_Rng) <= port_stat_state;
CcSToAxIn(CcSToAxi_TrigCnt_Rng)   <= port_trigcnt;
CcSToAxIn(CcSToAxi_DoneTime_Rng)  <= port_donetime;

i_cc_status_toAxi : entity work.psi_common_status_cc
    generic map ( width_g => CcSToAxi_Width_c )
    port map (
        a_clk_i  => Clk,            -- 源：数据域
        a_rst_i  => Rst,
        a_rst_o  => RstProc,        -- 数据域处理后复位，喂给核心
        a_dat_i  => CcSToAxIn,
        b_clk_i  => s00_axi_aclk,   -- 目的：AXI 域
        b_rst_i  => AxiRst,
        b_rst_o  => open,
        b_dat_o  => CcSToAxOut
    );
```

注意 `a_rst_o => RstProc`：核心 `data_rec` 的复位（第 471 行 `Rst => RstProc`）就取自这里。这是一处容易被忽略的细节——核心用的是一份经 status_cc 处理过的复位，而不是原始的 `Rst`。

目的端（AXI 域）拆包，再回读给主机：[hdl/data_rec_vivado_wrp.vhd:L369-L371](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L369-L371)

```vhdl
reg_stat_state <= CcSToAxOut(CcSToAxi_StatState_Rng);
reg_trigcnt    <= CcSToAxOut(CcSToAxi_TrigCnt_Rng);
reg_donetime   <= CcSToAxOut(CcSToAxi_DoneTime_Rng);
```

FromAxi 路方向相反（源 AXI → 目的数据），打包与拆包在第 374–411 行，实例化如下：[hdl/data_rec_vivado_wrp.vhd:L386-L399](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L386-L399)

```vhdl
i_cc_status_fromAxi : entity work.psi_common_status_cc
    generic map ( width_g => CcSFromAxi_Width_c )
    port map (
        a_clk_i  => s00_axi_aclk,   -- 源：AXI 域
        a_rst_i  => AxiRst,
        a_rst_o  => open,
        a_dat_i  => CcSFromAxIn,
        b_clk_i  => Clk,            -- 目的：数据域
        b_rst_i  => Rst,
        b_rst_o  => open,
        b_dat_o  => CcSFromAxOut
    );
```

#### 4.2.4 代码实践（阅读型：核对宽度）

1. **目标**：手算 ToAxi 路的总位宽，验证 `CcSToAxi_Width_c = 68`。
2. **步骤**：按第 167–170 行的链式定义，逐步代入。StatState 占 `3 downto 0`（4 位）；TrigCnt 起点为 `3+1=4`、终点 `3+32=35`（32 位）；DoneTime 起点 `35+1=36`、终点 `35+32=67`（32 位）。
3. **观察**：`CcSToAxi_DoneTime_Rng'high = 67`，故 `CcSToAxi_Width_c = 68`。
4. **预期**：与 `generic map ( width_g => CcSToAxi_Width_c )` 一致；若你在 FromAxi 路，可改 generics（如 `NumOfInputs_g=4, InputWidth_g=8, MemoryDepth_g=128, TrigInputs_g=1`）按第 183–194 行同样手算，得到一个具体的 `CcSFromAxi_Width_c`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 State/TrigCnt/DoneTime 三个值可以「拼成一个 68 位向量」一起过 status_cc，而不需要 3 个独立实例？
**答案**：因为它们都属于多比特电平值，且 status_cc 的握手机制保证目的域对整个向量做**整体采样**，各比特来自源域同一拍，不会撕裂；合并成一路省资源、也保证三者快照时间一致。

**练习 2**：核心 `data_rec` 的复位 `Rst` 端口接的是哪个信号？它从哪来？
**答案**：接的是 `RstProc`（第 471 行），它来自 `i_cc_status_toAxi` 实例的 `a_rst_o`（第 361 行），即数据域的一份经 status_cc 处理过的复位。

---

### 4.3 psi_common_pulse_cc：两路 pulse 实例

#### 4.3.1 概念说明

`psi_common_pulse_cc` 用于**单拍脉冲（事件）**的跨域。它的难点在于：源域的一个单周期脉冲，若目的时钟较慢，可能根本采不到；若直接用 status 思路当电平同步，又会在目的域拉成不定宽度。pulse_cc 的标准做法是「边沿检测 + 握手回告」：源域检测到脉冲上升沿后翻转一个请求电平，目的域同步该电平并检测其翻转，从而在目的域**复现出恰好一个周期**的脉冲；多路脉冲互不影响，每路占 1 位。

封装层同样用了两路：

- **`i_cc_pulse_fromAxi`（AXI → 数据）**：3 个脉冲——Arm、Ack、TrigCntClr。
- **`i_cc_pulse_toAxi`（数据 → AXI）**：1 个脉冲——Done。

#### 4.3.2 核心流程

**FromAxi 路**承载 3 个独立脉冲，每位一个事件：

| 位 | 常量 | 含义 | 源（AXI 域）产生方式 |
| --- | --- | --- | --- |
| 0 | `CcPFromAxi_Arm_c` | 启动一次录制 | `reg_cfg_arm`，由第 316 行 `reg_wr and reg_wdata(ArmIdx)` 解码成单拍 |
| 1 | `CcPFromAxi_Ack_c` | 确认 Done、回 Idle | `AckDone`，由第 313 行「读状态寄存器且当前在 Done 态」产生 |
| 2 | `CcPFromAxi_TrigCntClr_c` | 清触发计数 | `reg_cfg_trigcntclr`，由第 317 行 `reg_wr and reg_wdata(TrgCntClr_Idx)` 解码成单拍 |

三者都满足「单拍事件」的特征：要么来自 `reg_wr`（写脉冲）相与，要么来自组合条件产生的单拍（`AckDone`）。过 pulse_cc 后在数据域复现为 `port_cfg_arm`/`port_cfg_ack`/`port_cfg_trigcntclr`，接到核心端口。

**ToAxi 路**只承载 1 个脉冲——`port_done`（核心在状态机命中 Done 时产生的单拍，详见 u3-l2）。它在 AXI 域复现后直接驱动顶层中断端口 `Done_Irq`：[hdl/data_rec_vivado_wrp.vhd:L455](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L455)

```vhdl
Done_Irq <= CcPToAxOut(CsPToAxi_Done_c);
```

这就是「Done 中断如何经 pulse_cc 跨到 AXI 域」的完整链路：核心 `port_done` → `CcPToAxIn` → `i_cc_pulse_toAxi` → `CcPToAxOut` → `Done_Irq`，再由 PS 侧的中断控制器（GIC）采样。

#### 4.3.3 源码精读

FromAxi 路的位编号定义与打包：[hdl/data_rec_vivado_wrp.vhd:L174-L180](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L174-L180) 与 [hdl/data_rec_vivado_wrp.vhd:L414-L431](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L414-L431)

```vhdl
-- Pulse CC from Axi
constant CcPFromAxi_Arm_c        : natural := 0;
constant CcPFromAxi_Ack_c        : natural := CcPFromAxi_Arm_c+1;
constant CcPFromAxi_TrigCntClr_c : natural := CcPFromAxi_Ack_c+1;
constant CcFromAxi_Width_c       : natural := CcPFromAxi_TrigCntClr_c+1;  -- = 3
...
CcPFromAxIn(CcPFromAxi_Arm_c)         <= reg_cfg_arm;
CcPFromAxIn(CcPFromAxi_Ack_c)         <= AckDone;
CcPFromAxIn(CcPFromAxi_TrigCntClr_c)  <= reg_cfg_trigcntclr;

i_cc_pulse_fromAxi : entity work.psi_common_pulse_cc
    generic map ( num_pulses_g => CcFromAxi_Width_c )   -- 注意 generic 名是 num_pulses_g
    port map (
        a_clk_i => s00_axi_aclk,  a_rst_i => AxiRst, a_rst_o => open,
        a_dat_i => CcPFromAxIn,
        b_clk_i => Clk,           b_rst_i => Rst,    b_rst_o => open,
        b_dat_o => CcPFromAxOut
    );

port_cfg_arm        <= CcPFromAxOut(CcPFromAxi_Arm_c);
port_cfg_ack        <= CcPFromAxOut(CcPFromAxi_Ack_c);
port_cfg_trigcntclr <= CcPFromAxOut(CcPFromAxi_TrigCntClr_c);
```

注意 pulse_cc 的 generic 是 `num_pulses_g`（脉冲个数），与 status_cc 的 `width_g`（位宽）不同——这正反映「每个脉冲占 1 位」。

ToAxi 路（Done → 中断）：[hdl/data_rec_vivado_wrp.vhd:L198-L202](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L198-L202) 与 [hdl/data_rec_vivado_wrp.vhd:L438-L455](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L438-L455)

```vhdl
-- Pulse CC to Axi
constant CsPToAxi_Done_c  : natural := 0;
constant CsPToAxi_Width_c : natural := CsPToAxi_Done_c+1;   -- = 1
...
CcPToAxIn(CsPToAxi_Done_c) <= port_done;

i_cc_pulse_toAxi : entity work.psi_common_pulse_cc
    generic map ( num_pulses_g => CsPToAxi_Width_c )
    port map (
        a_clk_i => Clk,          a_rst_i => Rst,    a_rst_o => open,
        a_dat_i => CcPToAxIn,
        b_clk_i => s00_axi_aclk, b_rst_i => AxiRst, b_rst_o => open,
        b_dat_o => CcPToAxOut
    );

Done_Irq <= CcPToAxOut(CsPToAxi_Done_c);
```

#### 4.3.4 代码实践（阅读型：解释 Arm 为何不能走 status_cc）

这是本讲的核心辨析题，请结合解码行亲自判断：

1. **目标**：用源码证据说明「Arm 必须走 pulse_cc，而 SwTrig 可以走 status_cc」。
2. **步骤**：
   - 看第 316 行 `reg_cfg_arm <= reg_wr(...) and reg_wdata(...)(Reg_Cfg_ArmIdx_c);`——它含有 `reg_wr`（AXI 解码器的**单拍写脉冲**），所以 `reg_cfg_arm` 在 AXI 域只高一个周期，是**事件**。
   - 看第 325 行 `reg_swtrig <= reg_wdata(...)(Reg_SwTrig_TrigIdx_c);`——它**没有** `reg_wr`，只取 `reg_wdata` 的电平，所以 `reg_swtrig` 是**电平**（写 1 保持 1、写 0 才变 0），具 sticky 语义。
3. **观察与解释**：`Arm` 若误用 status_cc，由于它在源域只高 1 拍，status_cc 的握手未必能可靠捕捉这个短脉冲（当 AXI 域比数据域快或频率比不利时，可能整段错过），且即便捕捉到也无法保证在数据域只复现「一次」启动——可能丢失或重复。`pulse_cc` 专为单拍事件设计，无论两时钟频率比如何，都能在目的域精确复现一次脉冲。反过来 `SwTrig` 是持续电平，本就该走 status_cc，主机写 1 后电平稳定保持，status_cc 能采到一致快照。
4. **预期结论**：事件（Arm/Ack/TrigCntClr/Done）→ pulse_cc；电平/值（含 sticky 的 SwTrig）→ status_cc。这条规则解释了本 IP 全部 CC 选型。

#### 4.3.5 小练习与答案

**练习 1**：pulse_cc 的 generic 为什么叫 `num_pulses_g` 而不像 status_cc 那样叫 `width_g`？
**答案**：pulse_cc 每路脉冲恰好占 1 位，路数即位宽，用 `num_pulses_g` 强调「每 1 位 = 1 个独立事件」的语义；status_cc 传的是任意宽多比特值，用 `width_g` 表示总线位宽。

**练习 2**：Done 中断链路依次经过哪些信号与实例？
**答案**：核心 `port_done` →（打包）`CcPToAxIn(CsPToAxi_Done_c)` → `i_cc_pulse_toAxi`（数据域 `Clk` → AXI 域 `s00_axi_aclk`）→ `CcPToAxOut` → 顶层 `Done_Irq`。

---

### 4.4 CcS / CcP 信号拼接与范围定义

#### 4.4.1 概念说明

status_cc 要把多个不同位宽的值塞进**一个**宽向量过同步器，因此封装层用了一套优雅的「链式范围（chained subtype）」定义法：每个字段是一个 `subtype ... is natural range A downto B`，下一个字段的起点紧接上一个字段的 `'left`（高位 +1）。这样做的好处是：增删字段时只需改一处，后续字段自动后移，不会出错。pulse_cc 则简单得多——每个脉冲 1 位，用 `constant` 给出位编号即可。

#### 4.4.2 核心流程

四组定义的可读化对照：

**ToAxi status（数据→AXI，固定宽度 68 位）**

| 字段 | 范围 | 位宽 | 语义 |
| --- | --- | --- | --- |
| `CcSToAxi_StatState_Rng` | `3 downto 0` | 4 | 状态码 |
| `CcSToAxi_TrigCnt_Rng` | `35 downto 4` | 32 | 触发次数 |
| `CcSToAxi_DoneTime_Rng` | `67 downto 36` | 32 | Done 持续时长 |

**FromAxi pulse（AXI→数据，3 路）**

| 常量 | 值 | 语义 |
| --- | --- | --- |
| `CcPFromAxi_Arm_c` | 0 | 启动 |
| `CcPFromAxi_Ack_c` | 1 | 确认 |
| `CcPFromAxi_TrigCntClr_c` | 2 | 清计数 |

**FromAxi status（AXI→数据，宽度随 generics）**：从 `PreTrig`（`log2ceil(MemoryDepth_g)` 位）起，依次接 `TotSpl`（多 1 位）、`SelfTrigLo`/`Hi`（各 `InputWidth_g` 位）、`SelfTrigChEna`（`NumOfInputs_g` 位）、`OnExit`/`OnEnter`（各 1 位）、`TrigEna`（3 位）、`SwTrig`（1 位）、`MinRecPeriod`（32 位）、`EnableExtTrig`（`TrigInputs_g` 位），总宽 `CcSFromAxi_Width_c`。

**ToAxi pulse（数据→AXI，1 路）**：`CsPToAxi_Done_c = 0`。

#### 4.4.3 源码精读

FromAxi status 的链式定义是最值得读的一段（注意单 bit 字段用 `constant` 而非 `subtype`）：[hdl/data_rec_vivado_wrp.vhd:L182-L196](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L182-L196)

```vhdl
-- Status CC from Axi
subtype CcSFromAxi_PreTrig_Rng       is natural range log2ceil(MemoryDepth_g)-1 downto 0;
subtype CcSFromAxi_TotSpl_Rng         is natural range CcSFromAxi_PreTrig_Rng'left+log2ceil(MemoryDepth_g)+1 downto CcSFromAxi_PreTrig_Rng'left+1;
subtype CcSFromAxi_SelfTrigLo_Rng     is natural range CcSFromAxi_TotSpl_Rng'left+InputWidth_g downto CcSFromAxi_TotSpl_Rng'left+1;
subtype CcSFromAxi_SelfTrigHi_Rng     is natural range CcSFromAxi_SelfTrigLo_Rng'left+InputWidth_g downto CcSFromAxi_SelfTrigLo_Rng'left+1;
subtype CcSFromAxi_SelfTrigChEna_Rng  is natural range CcSFromAxi_SelfTrigHi_Rng'left+NumOfInputs_g downto CcSFromAxi_SelfTrigHi_Rng'left+1;
constant CcSFromAxi_SelfTrigOnExit_c  : natural := CcSFromAxi_SelfTrigChEna_Rng'left+1;
constant CcSFromAxi_SelfTrigOnEnter_c : natural := CcSFromAxi_SelfTrigOnExit_c+1;
subtype CcSFromAxi_TrigEna_Rng        is natural range CcSFromAxi_SelfTrigOnEnter_c+3 downto CcSFromAxi_SelfTrigOnEnter_c+1;
constant CcSFromAxi_SwTrig_c          : natural := CcSFromAxi_TrigEna_Rng'left+1;
subtype CcSFromAxi_MinRecPeriod_Rng   is natural range CcSFromAxi_SwTrig_c+32 downto CcSFromAxi_SwTrig_c+1;
subtype CcSFromAxi_EnableExtTrig_Rng  is natural range CcSFromAxi_MinRecPeriod_Rng'left+TrigInputs_g downto CcSFromAxi_MinRecPeriod_Rng'left+1;
constant CcSFromAxi_Width_c           : natural := CcSFromAxi_EnableExtTrig_Rng'left+1;
```

可以看到三个 generics 直接进入位宽计算：`MemoryDepth_g`（决定 PreTrig/TotSpl 位宽）、`InputWidth_g`（决定 SelfTrig 阈值位宽）、`NumOfInputs_g`（决定通道使能掩码位宽）、`TrigInputs_g`（决定外部触发使能位宽）。这也是为什么改这些 generic 时，存储/端口/CC 宽度会自动联动（u1-l4 提到的「四处镜像」之一即在此）。

打包与拆包两段成对出现，互为镜像：打包在 [hdl/data_rec_vivado_wrp.vhd:L374-L384](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L374-L384)，拆包在 [hdl/data_rec_vivado_wrp.vhd:L401-L411](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L401-L411)。注意 SelfTrigLo/Hi 在打包时取了低 `InputWidth_g` 位（`reg_selftriglo(InputWidth_g-1 downto 0)`），因为寄存器侧虽然声明为 32 位以便符号扩展回读，但核心只需要 `InputWidth_g` 位。

#### 4.4.4 代码实践（阅读型：手算 FromAxi 宽度）

1. **目标**：给定一组 generics，手算 `CcSFromAxi_Width_c`，体会链式定义如何随 generic 变化。
2. **步骤**：取默认 `MemoryDepth_g=128`（故 `log2ceil=7`）、`InputWidth_g=8`、`NumOfInputs_g=4`、`TrigInputs_g=1`，按第 183–194 行逐字段累加位宽：
   PreTrig 7 + TotSpl 8 + SelfTrigLo 8 + SelfTrigHi 8 + SelfTrigChEna 4 + OnExit 1 + OnEnter 1 + TrigEna 3 + SwTrig 1 + MinRecPeriod 32 + EnableExtTrig 1。
3. **观察**：总和 = 7+8+8+8+4+1+1+3+1+32+1 = 74，即 `CcSFromAxi_Width_c = 74`。
4. **预期**：改 `NumOfInputs_g` 会改变 `SelfTrigChEna` 段宽度从而改变总量；这就是 CC 宽度与 generic 的联动关系。**待本地验证**：可在 Vivado 综合时查看 `i_cc_status_fromAxi` 的 `width_g` 实例化值确认。

#### 4.4.5 小练习与答案

**练习 1**：链式定义里，为什么单 bit 字段（OnExit、OnEnter、SwTrig）用 `constant` 而多 bit 字段用 `subtype`？
**答案**：`subtype ... range A downto B` 用于切片赋值（`CcSFromAxIn(某范围) <= 多bit信号`），需要的是一个范围；单 bit 字段只需一个标量下标做位赋值（`CcSFromAxIn(常量) <= 1bit信号`），用 `constant` 更直接。

**练习 2**：把 `NumOfInputs_g` 从 4 改成 8，`CcSFromAxi_Width_c` 变化多少？
**答案**：`SelfTrigChEna` 段从 4 位变 8 位，多出 4 位，故 `CcSFromAxi_Width_c` 增加 4（其余字段定义因链式关系整体上移，但总宽只增 4）。

---

## 5. 综合实践

把本讲全部内容串成一张完整的「跨时钟域信号流向图」。

**任务**：阅读 [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) 第 166–202、228、313、316、325、345–455 行，绘制一张含两列（左 AXI 域 `s00_axi_aclk`、右数据域 `Clk`）的流向图，要求：

1. 画出 4 条跨域通道，分别标注元件名、源域、目的域：
   - `i_cc_status_toAxi`：`port_stat_state`/`port_trigcnt`/`port_donetime`（数据→AXI，status）；
   - `i_cc_status_fromAxi`：PreTrig/TotSpl/SelfTrig*/TrigEna/**SwTrig**/MinRecPeriod/EnableExtTrig（AXI→数据，status）；
   - `i_cc_pulse_fromAxi`：Arm/Ack/TrigCntClr（AXI→数据，pulse）；
   - `i_cc_pulse_toAxi`：Done → `Done_Irq`（数据→AXI，pulse）。
2. 在图上标注 `State`/`TrigCnt`/`DoneTime` 走 status；`Arm`/`Ack`/`TrigCntClr`/`Done` 走 pulse。
3. 用一段话解释 **Arm 为何不能走 status_cc**：依据第 316 行 `reg_cfg_arm` 含单拍 `reg_wr`，是事件而非电平；status_cc 无法保证可靠捕捉并精确复现一次短脉冲，pulse_cc 才能胜任。并对照第 325 行 `reg_swtrig`（无 `reg_wr`、纯电平）说明 SwTrig 为何适合 status_cc。
4. **附加验证**：在 testbench 的双时钟定义里确认两个域的真实频率——数据域 `Clk` 为 160 MHz（[testbench/top_tb/top_tb_pkg.vhd:L27](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb_pkg.vhd#L27)），AXI 域 `s00_axi_aclk` 为 125 MHz（[testbench/top_tb/top_tb.vhd:L70](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/testbench/top_tb/top_tb.vhd#L70)）。这两个频率不成整数倍，正说明为什么必须用专门 CDC 元件而不能简单打两拍。

**预期产出**：一张双列流向图 + 一段 Arm/SwTrig 选型论证 + 双时钟频率佐证。本任务无需运行仿真，属源码阅读型实践。

## 6. 本讲小结

- 封装层在**数据域 `Clk`** 与 **AXI 域 `s00_axi_aclk`** 之间设了 4 个 CC 实例，按「方向 × 性质」构成 2×2 矩阵：status 两路（toAxi、fromAxi）+ pulse 两路（fromAxi、toAxi）。
- **status_cc 传多比特电平/值**（State、TrigCnt、DoneTime 与全部配置，含 sticky 的 SwTrig），保证目的域拿到一致快照；**pulse_cc 传单拍事件**（Arm、Ack、TrigCntClr、Done），无论频率比如何都精确复现一次。
- 核心辨析：`reg_cfg_arm`（第 316 行，含 `reg_wr`）是事件必须走 pulse_cc；`reg_swtrig`（第 325 行，纯 `reg_wdata`）是电平可走 status_cc——「值走 status，事件走 pulse」。
- `Done` 经 `i_cc_pulse_toAxi` 跨到 AXI 域驱动 `Done_Irq` 中断（第 455 行），是核心回报主机的关键事件链路。
- status_cc 用**链式 subtype** 把多个字段拼进一个宽向量，宽度随 `MemoryDepth_g/InputWidth_g/NumOfInputs_g/TrigInputs_g` 自动联动；pulse_cc 每路占 1 位，generic 用 `num_pulses_g`。
- 数据域核心复位 `RstProc` 取自 `i_cc_status_toAxi` 的 `a_rst_o`（第 361 行），AXI 域复位由 `s00_axi_aresetn` 取反得到 `AxiRst`（第 228 行）。

## 7. 下一步学习建议

- 接下来读 **u5-l3 录制存储：每通道双端口 RAM 与读出**，那里会用到本讲之外的另一类跨域——**双口 RAM 的两个端口分别处于 `Clk` 与 `s00_axi_aclk`**（见第 555、562 行 `a_clk_i => Clk`、`b_clk_i => s00_axi_aclk`），是「用存储器天然跨域」的另一种思路。
- 若想深究 `psi_common_status_cc`/`psi_common_pulse_cc` 的内部 RTL（握手、边沿检测、亚稳态同步链），可去外部依赖库 `psi_common` 的对应源文件阅读。
- 回顾 **u5-l1** 中 IPIC 信号都活在 AXI 域的结论，结合本讲即可完整画出「AXI 域解码 → CC → 数据域核心」的全链路。
