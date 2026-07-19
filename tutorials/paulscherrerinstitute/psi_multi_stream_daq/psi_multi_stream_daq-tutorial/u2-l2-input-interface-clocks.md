# 输入逻辑 psi_ms_daq_input：接口、时钟域与缓冲

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `psi_ms_daq_input` 这个实体的 7 个 generic 各自控制什么，以及它的端口如何按「流输入 / 配置 / DAQ 控制 / DAQ 数据 / 时间戳」五组来理解。
- 在源码中准确指出**三个时钟域**（流时钟 `Str_Clk`、寄存器时钟 `ClkReg`、内存时钟 `ClkMem`）的边界。
- 针对**每一处跨时钟域**，解释为什么用 `psi_common_status_cc`、`psi_common_bit_cc`、`psi_common_pulse_cc` 或异步 FIFO 中的一种，而不是其它几种。
- 看懂数据从 `Str_Data` 进入后，如何被打包成 64 位（或 `IntDataWidth_g` 位）内部字，再经**异步数据 FIFO + `pl_stage` 流水级**两级缓冲送到 `Daq_Data`。

本讲只讲「接口、时钟域、缓冲结构」，**不**展开记录模式、触发、后触发计数、超时、时间戳锁存等内部算法——那是 u2-l3、u2-l4 的内容。本讲先把「管道」修好，下一讲再讲「阀门」。

## 2. 前置知识

在读本讲前，你需要先具备以下概念（来自 u1 单元和 u2-l1）：

- **时钟域（clock domain）**：由一个独立时钟驱动的所有触发器集合。信号从一个时钟域进入另一个时钟域时必须做**跨时钟域同步（CDC, Clock Domain Crossing）**，否则会采样到亚稳态（metastability）。
- **Ready/Valid 握手**：本 IP 内部数据流普遍采用 `Vld`（数据有效）+ `Rdy`（下游准备好）握手，二者同为 1 时完成一次传输。
- **psi_common 子库**：本项目依赖的 PSI 通用元件库（见 u1-l2）。本讲会用到其中 5 个元件：`psi_common_status_cc`（多位状态量 CDC）、`psi_common_bit_cc`（单比特 CDC）、`psi_common_pulse_cc`（脉冲 CDC）、`psi_common_async_fifo`（异步 FIFO）、`psi_common_pl_stage`（流水级）。它们在本仓库里只被例化、不在本仓库实现，因此本讲只讲「它们在这里承担什么角色」。
- **`Input2Daq_Data_t` 记录类型**（u2-l1 已介绍）：含 `Last`、`Data`、`Bytes`、`IsTo`、`IsTrig` 五个字段，是输入逻辑输出给下游 DMA 的数据载体，其中 `Data`/`Bytes` 的位宽随 `IntDataWidth_g` 动态确定。
- **`RecMode_t`**（u2-l1）：2 位的记录模式子类型，有 `RecMode_Continuous_c`/`TriggerMask_c`/`SingleShot_c`/`ManuelMode_c` 四个取值。

> 提示：本讲会出现 `IntDataWidth_g` 这个 generic。在早期版本里它是写死的 64，近期 `feature/se32` 分支才把它提取为可参数化 generic（详见 u4-l4）。本讲默认按 64 位理解，但所有公式都按 generic 写。

## 3. 本讲源码地图

本讲几乎只围绕**一个文件**展开：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [hdl/psi_ms_daq_input.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd) | 单路流的输入逻辑：采样、打包、跨时钟域、缓冲 | generic/port、三时钟域、5 个 psi_common 元件例化 |

辅助参考（不展开，只引用其中类型定义）：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 定义 `Input2Daq_Data_t`、`RecMode_t` 等公共类型 |

`psi_ms_daq_input` 在顶层 `psi_ms_daq_axi` 里被一个 `generate` 语句**每路流例化一次**（见 u1-l3 的 `g_input`），所以本讲讨论的所有结构在每一路流上都独立存在一份。

## 4. 核心概念与源码讲解

### 4.1 entity psi_ms_daq_input 的 generic 与端口

#### 4.1.1 概念说明

`psi_ms_daq_input` 是「单路流」的入口模块。它要做三件事：

1. 在**流时钟域**里接收外部数据（`Str_Data` + `Str_Vld`/`Str_Rdy`）、触发（`Str_Trig`）和时间戳（`Str_Ts`）。
2. 在内部把若干个窄样本（8/16/32 位）拼成一个宽的内部字（`IntDataWidth_g` 位，通常 64 位），并附带「有效字节数」「是否超时帧」「是否触发帧」等元数据。
3. 把拼好的字通过**异步 FIFO** 安全地送到**内存时钟域**，交给下游 DMA 引擎。

generic 控制这条管道的「容量」与「行为」，端口则暴露给上层（寄存器接口 + DMA 状态机）。

#### 4.1.2 核心流程

```text
外部流 ──Str_Data/Vld/Rdy/Trig/Ts──▶ [流时钟域 p_comb/p_seq]
                                          │  拼样本为内部字 + 算字节/触发/超时标志
                                          ▼
                                    [异步数据 FIFO]  ──▶ [pl_stage 流水级] ──▶ Daq_Data/Vld（内存时钟域）
                                    [异步时间戳 FIFO] ──▶ Ts_Data/Vld（内存时钟域）
配置 ──PostTrigSpls/Mode/Arm/ToDisable/FrameTo──▶ [寄存器时钟域] ──CDC──▶ 流时钟域
状态 ◀──IsArmed/IsRecording──────────────────── [流时钟域] ──CDC──▶ 寄存器时钟域
```

#### 4.1.3 源码精读

**generic 声明**（7 个）：

[hdl/psi_ms_daq_input.vhd:L30-L38](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L30-L38) —— 定义整条管道的可参数化项：

| generic | 类型/默认值 | 含义 |
| --- | --- | --- |
| `StreamWidth_g` | `positive range 8 to 64 := 16` | 单个样本位宽，**必须**是 8/16/32/64 之一（源码末尾有断言强制）。 |
| `StreamBuffer_g` | `positive range 1 to 65535 := 1024` | 数据 FIFO 深度，单位是 QWORD（即内部字，通常 64 位）。 |
| `StreamTimeout_g` | `real := 1.0e-3` | 超时阈值，单位**秒**。 |
| `StreamClkFreq_g` | `real := 125.0e6` | 流时钟频率，单位 Hz，用来把秒换算成时钟周期数。 |
| `StreamTsFifoDepth_g` | `positive := 16` | 时间戳 FIFO 深度。 |
| `StreamUseTs_g` | `boolean := true` | 是否启用时间戳采集；为 `false` 时整个时间戳 FIFO 用 `generate` 跳过。 |
| `IntDataWidth_g` | `positive := 64` | 内部数据宽度（近期才 generic 化，见 u4-l4）。 |

注意 `StreamTimeout_g` 和 `StreamClkFreq_g` **成对出现**：单独的超时秒数硬件无法直接使用，必须乘以时钟频率换算成周期数。这个换算发生在编译期常量里：

[hdl/psi_ms_daq_input.vhd:L86](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L86) —— `TimeoutLimit_c := integer(StreamClkFreq_g * StreamTimeout_g) - 1`，把「1 ms 超时 @ 125 MHz」换算成 `125_000 - 1` 个周期。这是 u2-l4 超时逻辑的上限。

**端口声明**按 5 组（源码里用注释分隔）：

[hdl/psi_ms_daq_input.vhd:L39-L74](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L39-L74) —— 全部端口。按下表分组理解：

| 组 | 端口 | 方向 | 所在时钟域（物理） |
| --- | --- | --- | --- |
| 数据流输入 | `Str_Clk` | in | **流时钟** |
| | `Str_Vld`/`Str_Rdy`/`Str_Data`/`Str_Trig`/`Str_Ts` | in/out/in | 流时钟 |
| 配置 | `ClkReg`/`RstReg` | in | **寄存器时钟** |
| | `PostTrigSpls`/`Mode`/`Arm`/`ToDisable`/`FrameTo` | in | 寄存器时钟（需 CDC 到流时钟） |
| | `IsArmed`/`IsRecording` | out | 寄存器时钟（由流时钟 CDC 回来） |
| DAQ 控制 | `ClkMem`/`RstMem` | in | **内存时钟** |
| DAQ 数据 | `Daq_Vld`/`Daq_Rdy`/`Daq_Data`/`Daq_Level`/`Daq_HasLast` | out/in | 内存时钟 |
| 时间戳 | `Ts_Vld`/`Ts_Rdy`/`Ts_Data` | out/in | 内存时钟 |

关键细节：`Daq_Data` 的类型是**带约束的** `Input2Daq_Data_t`：

[hdl/psi_ms_daq_input.vhd:L66](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L66) —— `Daq_Data : out Input2Daq_Data_t(Data(IntDataWidth_g-1 downto 0), Bytes(log2ceil(IntDataWidth_g/8) downto 0))`。这是 VHDL 里给 record 的 unconstrained 字段在端口处「现场定型」的写法，让 `Data`/`Bytes` 的位宽随 `IntDataWidth_g` 自动伸缩。这就是 u2-l1 强调的「`Input2Daq_Data_t` 的 `Data`/`Bytes` 宽度随 `IntDataWidth_g` 动态确定」的落点。

> 旁注：源码第 26 行有一行 `-- $$ testcases=... $$` 元注解，声明了这个实体的 testbench 覆盖哪些用例（`single_frame`/`multi_frame`/`timeout`/`ts_overflow`/`trig_in_posttrig`/`always_trig`/`backpressure`/`modes`）。这是 PsiSim/CI 用的约定，u5-l1 会详细讲。

#### 4.1.4 代码实践

**实践目标**：把 generic 和真实工程场景对应起来。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_input.vhd:L30-L38](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L30-L38)。
2. 假设你要采集一路 **16 位 @ 125 MHz** 的 ADC 数据，希望数据 FIFO 能缓存约 2048 个 64 位字，超时设为 0.5 ms，需要时间戳。
3. 写出你会给这路流设置的 6 个 generic 值（`StreamWidth_g`、`StreamBuffer_g`、`StreamTimeout_g`、`StreamClkFreq_g`、`StreamTsFifoDepth_g`、`StreamUseTs_g`）。
4. 再算一下 `TimeoutLimit_c` 的值。

**需要观察的现象 / 预期结果**（待本地验证）：

- `StreamWidth_g => 16`，`StreamBuffer_g => 2048`，`StreamTimeout_g => 0.5e-3`，`StreamClkFreq_g => 125.0e6`，`StreamTsFifoDepth_g` 用默认 16 即可，`StreamUseTs_g => true`。
- `TimeoutLimit_c = integer(125.0e6 * 0.5e-3) - 1 = 62_500 - 1 = 62_499`。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `StreamWidth_g` 设成 20，会发生什么？

**答案**：实体声明里 `StreamWidth_g` 的范围是 `8 to 64`，20 在范围内、能通过编译；但末尾 [hdl/psi_ms_daq_input.vhd:L587](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L587) 的 `assert` 会在仿真里报 `###ERROR###: ... StreamWidth_g must be 8, 16, 32 or 64`。此外字节计数 `case StreamWidth_g`（见 4.3.3）没有 20 的分支，逻辑也不会正确。

**练习 2**：`StreamClkFreq_g` 为什么必须是 `real` 而不能用 `integer`？

**答案**：因为 `StreamTimeout_g` 是秒级小数（如 `1.0e-3`），二者相乘才能得到周期数；若 `StreamClkFreq_g` 是整数，乘法结果的精度处理会很别扭。用 `real` 相乘后再 `integer()` 取整是最自然的写法。

---

### 4.2 三时钟域与 status_cc / bit_cc / pulse_cc 跨时钟域例化

#### 4.2.1 概念说明

这个模块横跨**三个时钟域**，这是它最容易出错、也最值得讲清楚的地方：

- **流时钟域 `Str_Clk`**：外部数据到来的时钟，也是 `p_comb`/`p_seq` 主进程运行的时钟。本模块「绝大部分逻辑」都在这里。
- **寄存器时钟域 `ClkReg`**：AXI Slave 寄存器接口的时钟。CPU 写下来的配置（`PostTrigSpls`/`Mode`/`Arm`/`ToDisable`/`FrameTo`）和读上去的状态（`IsArmed`/`IsRecording`）都在这个域。
- **内存时钟域 `ClkMem`**：下游 DMA / AXI Master 的时钟。输出数据 `Daq_*`、时间戳 `Ts_*` 都在这个域。

把信号从一个域搬到另一个域，**不能**简单拉一根线。本项目用 psi_common 的三种 CDC 元件 + 异步 FIFO 来分类处理，选型规则是：

| 信号性质 | 选用元件 | 原因 |
| --- | --- | --- |
| 多位**电平**信号（值会较长时间保持稳定） | `psi_common_status_cc` | 用 Gray 码两级同步，只有在信号稳定时采样才正确，因此适合「准静态」配置/计数。 |
| 单比特**电平**信号 | `psi_common_bit_cc` | 两级触发器打拍即可，无需握手。 |
| 单周期**脉冲**信号 | `psi_common_pulse_cc` | 脉冲可能被慢时钟漏采，必须用请求/应答握手保证「至少采到一次」。 |
| 连续**数据流** | `psi_common_async_fifo` | 用异步 FIFO 解耦两侧时钟与吞吐，自带 Ready/Valid。 |

#### 4.2.2 核心流程

```text
寄存器时钟域 ClkReg                         流时钟域 Str_Clk
├─ PostTrigSpls/Mode/ToDisable/FrameTo ─status_cc─▶ (配置 consumed by p_comb)
├─ Arm ────────────────────────────────  pulse_cc─▶ (单次脉冲)
└─ RstReg ──────────────────────────────   bit_cc ─▶┐
                                                    ├─▶ Str_Rst (喂给 p_seq 与两个 FIFO 的 in_rst)
内存时钟域 ClkMem ── RstMem ────────────   bit_cc ─▶┘

Str_Clk ── IsArmed/RecEna ─────────────────  bit_cc ─▶ ClkReg（状态回读）
Str_Clk ── r.TLastCnt ──────────────────── status_cc ─▶ ClkMem（TLAST 计数）
Str_Clk ── 数据流 ─────────────────────── async_fifo ─▶ ClkMem（见 4.3）
Str_Clk ── 时间戳流 ───────────────────── async_fifo ─▶ ClkMem（见 4.4）
```

#### 4.2.3 源码精读

**(a) `i_cc_reg_status`：多位配置 ClkReg → Str_Clk**

[hdl/psi_ms_daq_input.vhd:L408-L425](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L408-L425) —— 把 4 个配置信号**打包成 36 位**（`PostTrigSpls` 32 位 + `Mode` 2 位 + `ToDisable` 1 位 + `FrameTo` 1 位）一次性用 `status_cc` 同步到流时钟域，输出 `PostTrigSpls_Sync`/`Mode_Sync`/`ToDisable_Sync`/`FrameTo_Sync`。这些都是「CPU 写一次后长期保持」的准静态值，正好适合 `status_cc`。

**(b) `i_cc_status`：单比特状态 Str_Clk → ClkReg**

[hdl/psi_ms_daq_input.vhd:L427-L437](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L427-L437) —— 把流时钟域的 `r.IsArmed` 和 `r.RecEna` 两路单比特电平用 `bit_cc` 同步回寄存器时钟域，映射成对外端口 `IsArmed` 和 `IsRecording`，供 CPU 通过寄存器（`MODE` 寄存器的 `IsArmed`/`IsRecording` 位，见 u3-l5）轮询。

**(c) `i_cc_reg_pulse`：Arm 脉冲 ClkReg → Str_Clk**

[hdl/psi_ms_daq_input.vhd:L439-L451](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L439-L451) —— `Arm` 是 CPU 写一次寄存器产生的**单次脉冲**，必须用 `pulse_cc`（内部带请求/应答握手），否则当 `ClkReg` 比 `Str_Clk` 快时脉冲会被漏掉，导致永远不触发。输出 `Arm_Sync` 喂给 `p_comb`。

**(d) `icc_reg_rst` 与 `icc_mem_rst`：两个复位 bit_cc**

[hdl/psi_ms_daq_input.vhd:L454-L473](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L454-L473) —— 把 `RstReg`（来自 `ClkReg`）和 `RstMem`（来自 `ClkMem`）各自用 `bit_cc` 同步到流时钟域，得到 `RstReg_Sync` 和 `RstAcq_Sync`，再 **OR** 起来形成流时钟域的统一复位 `Str_Rst`：

```vhdl
Str_Rst <= RstReg_Sync or RstAcq_Sync;
```

这样无论寄存器侧还是内存侧发生复位，流时钟域的逻辑都会被可靠地复位（且复位释放经过同步，避免亚稳态）。`Str_Rst` 随后被 `p_seq` 和两个异步 FIFO 的 `in_rst_i` 使用。

**(e) `i_cc`：TLAST 计数 Str_Clk → ClkMem**

[hdl/psi_ms_daq_input.vhd:L477-L489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L477-L489) —— 流时钟域里每写完一个带末帧标志的字，`r.TLastCnt` 就自增（见 [hdl/psi_ms_daq_input.vhd:L266-L268](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L266-L268)）。这个**多比特计数器**要用 `status_cc` 同步到内存时钟域成为 `InTlastCnt`，供 `p_outlast` 在内存时钟域里判断「FIFO 里还有几个末帧」（即 `Daq_HasLast`，见 4.4.3）。宽度由常量 `TlastCntWidth_c` 决定。

#### 4.2.4 代码实践

这正是本讲规格里要求的核心实践：**标注每个跨时钟域实例**。

**实践目标**：把本模块里所有跨时钟域实例整理成一张表，说清楚「跨哪两个域、搬的是数据还是控制/状态、为什么用这个元件」。

**操作步骤**：

1. 在源码里定位以下 7 个实例：`i_cc_reg_status`、`i_cc_status`、`i_cc_reg_pulse`、`icc_reg_rst`、`icc_mem_rst`、`i_cc`、时间戳 FIFO（`i_tsfifo`）。
2. 对每个实例，读它的 `a_clk_i`/`b_clk_i`（或 `in_clk_i`/`out_clk_i`）接的是哪个时钟，从而确定方向。
3. 按 4.2.1 的选型规则，填出下表。

**需要观察的现象 / 预期结果**：

| 实例 | 元件 | 方向 | 搬运内容 | 选型理由 |
| --- | --- | --- | --- | --- |
| `i_cc_reg_status` | `status_cc` | ClkReg → Str_Clk | 控制/配置（PostTrigSpls 等 36 位，准静态） | 多位电平，必须 Gray 同步 |
| `i_cc_status` | `bit_cc` | Str_Clk → ClkReg | 状态（IsArmed/RecEna 单比特电平） | 单比特电平，打两拍即可 |
| `i_cc_reg_pulse` | `pulse_cc` | ClkReg → Str_Clk | 控制（Arm 单次脉冲） | 脉冲需握手，防漏采 |
| `icc_reg_rst` | `bit_cc` | ClkReg → Str_Clk | 控制（RstReg 复位） | 单比特电平 |
| `icc_mem_rst` | `bit_cc` | ClkMem → Str_Clk | 控制（RstMem 复位） | 单比特电平 |
| `i_cc` | `status_cc` | Str_Clk → ClkMem | 状态（TLastCnt 多位计数） | 多位电平，Gray 同步 |
| `i_tsfifo`（时间戳） | `async_fifo` | Str_Clk → ClkMem | 数据（时间戳流） | 连续数据流，需解耦吞吐 |

**预期结论**：本模块**没有**任何「多位信号直接拉线跨域」的情况——凡是多位必走 `status_cc` 或异步 FIFO，凡是单比特电平走 `bit_cc`，凡是脉冲走 `pulse_cc`。这是 PSI 代码的一贯风格，也是 u5 单元 testbench 里 `backpressure` 等用例能放心做随机时钟的底气。

> 待本地验证：如果你想亲眼看到这些 CDC 生效，可以在 Modelsim 里跑 `sim/run.tcl` 指定的 input testbench（它会把 `Str_Clk` 和 `ClkMem` 设成不同频率），在波形上对照 `Arm` 与 `Arm_Sync`、`r.TLastCnt` 与 `InTlastCnt` 的相位差。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Arm` 用 `pulse_cc` 而不是 `bit_cc`？

**答案**：`Arm` 是 CPU 写寄存器产生的单周期脉冲。如果用 `bit_cc`（纯打两拍），当 `ClkReg` 频率高于 `Str_Clk` 时，这个脉冲可能在两次 `Str_Clk` 采样之间就已消失，被彻底漏掉，导致触发永远不来。`pulse_cc` 内部用请求/应答握手，保证脉冲「至少被对侧采到一次」。

**练习 2**：`i_cc` 同步的是 `r.TLastCnt`，为什么不用异步 FIFO 而用 `status_cc`？

**答案**：`r.TLastCnt` 是一个**单调递增的计数值**（电平），不是一个带握手的数据流——内存侧只需要知道「当前累计了几个 TLAST」这个数值，不需要对每一拍都做 Ready/Valid 应答。`status_cc` 用 Gray 码同步多位计数器正是标准做法（异步 FIFO 内部的读/写指针也是这么跨域的）。

**练习 3**：`Str_Rst` 为什么要把 `RstReg_Sync` 和 `RstAcq_Sync` OR 在一起？

**答案**：流时钟域的逻辑可能因为「寄存器侧复位」（CPU 写 `GCFG` 软复位）或「内存侧复位」（全局硬件复位）任一事件而需要复位。把两者同步后 OR，保证两种复位源都能可靠地复位流时钟域，且复位**释放**沿经过 `bit_cc` 同步，不会在 `Str_Clk` 上产生亚稳态。

---

### 4.3 数据异步 FIFO（psi_common_async_fifo）例化与字/字节打包

#### 4.3.1 概念说明

流时钟域里 `p_comb` 每收到一个 `Str_Data` 样本，就把它**移位拼进**一个内部宽字（`IntDataWidth_g` 位）。只有当凑满一整个字、或者发生超时/触发需要冲刷残余时，才向数据 FIFO 写一拍。这个数据 FIFO 是**异步**的：写侧在 `Str_Clk`，读侧在 `ClkMem`，它既是跨时钟域桥梁，也是「削峰填谷」的弹性缓冲。

要把样本拼成字并正确记录「这个字里有几个有效字节」，需要几个编译期常量：

- `WconvFactor_c`：一个内部字能装下几个样本 = `IntDataWidth_g / StreamWidth_g`。
- `BytesWidth_c`：记录「有效字节数」需要的位数 = `log2ceil(IntDataWidth_g/8) + 1`。
- `DataFifoWidth_c`：数据 FIFO 单条记录的总位宽 = `IntDataWidth_g + BytesWidth_c + 2`（数据 + 字节数 + IsTo + IsTrig）。

#### 4.3.2 核心流程

拼字与计数（在 `p_comb` 里，属于 u2-l3 详讲的内容，这里只看结构）：

```text
每来一个样本 (Str_Vld=1 & DataFifo_InRdy=1):
  r.WordCnt += 1
  把 Str_Data 移入 r.DataSftReg 的对应切片
  if r.WordCnt == WconvFactor_c-1:    # 凑满一个字
      DataFifoVld := 1                # 申请写 FIFO
凑满 / 超时 / 触发时：
  打包 DataFifo_InData = [IsTrig, IsTo, DataFifoBytes, DataSftReg]
  写入异步 FIFO (Str_Clk → ClkMem)
```

#### 4.3.3 源码精读

**关键常量**：

[hdl/psi_ms_daq_input.vhd:L87-L90](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L87-L90)

```vhdl
constant WconvFactor_c   : positive := IntDataWidth_g / StreamWidth_g;
constant BytesWidth_c    : positive := log2ceil(IntDataWidth_g/8) + 1;
constant TlastCntWidth_c : positive := log2ceil(StreamBuffer_g) + 1;
constant DataFifoWidth_c : positive := IntDataWidth_g + BytesWidth_c + 2;
```

以默认 `IntDataWidth_g=64`、`StreamWidth_g=16` 为例：`WconvFactor_c = 4`（4 个 16 位样本拼一个 64 位字）；`BytesWidth_c = log2ceil(8)+1 = 4`；`DataFifoWidth_c = 64 + 4 + 2 = 70` 位。

**拼字逻辑**（在 `p_comb` 内，u2-l3 会详讲算法，本讲只点出它产出 `DataSftReg` 和 `DataFifoBytes`）：

[hdl/psi_ms_daq_input.vhd:L277-L303](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L277-L303) —— 每个样本按 `r.WordCnt` 计算出的切片位置移入 `DataSftReg`；并把 `WordCnt` 按 `StreamWidth_g` 换算成字节数 `DataFifoBytes`（8 位 ×1、16 位 ×2、32 位 ×4、64 位 ×8，用末尾补零位实现乘法）。

**FIFO 输入打包**：

[hdl/psi_ms_daq_input.vhd:L492-L495](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L492-L495) —— 把 `DataSftReg`、`DataFifoBytes`、`DataFifoIsTo`、`DataFifoIsTrig` 拼成一条 `DataFifo_InData`：

```text
高位 →低位:  [IsTrig][IsTo][DataFifoBytes : BytesWidth_c 位][DataSftReg : IntDataWidth_g 位]
```

这种「数据 + 元数据平铺进 FIFO」的写法很关键：下游 DMA 拿到的不仅是数据本身，还知道这一字里有几个字节有效、是不是触发帧/超时帧。

**异步 FIFO 例化 `i_dfifo`**：

[hdl/psi_ms_daq_input.vhd:L497-L516](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L497-L516) —— 写侧 `in_clk_i => Str_Clk`、`in_rst_i => Str_Rst`；读侧 `out_clk_i => ClkMem`、`out_rst_i => '0'`。深度 `depth_g => StreamBuffer_g`，宽度 `width_g => DataFifoWidth_c`。注意 `afull_on_g => false`、`aempty_on_g => false`——数据 FIFO **不**启用 almost-full 反压（反压靠 `Str_Rdy` 直接回传给外部流，见第 534 行 `Str_Rdy <= DataFifo_InRdy`）。读侧输出 `DataFifo_PlData`/`DataFifo_PlVld`，并给出 `DataFifo_Level`（填充水位，用于 `Daq_Level` 上报）。

[hdl/psi_ms_daq_input.vhd:L534](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L534) —— `Str_Rdy <= DataFifo_InRdy`：直接把 FIFO 写端口就绪信号当作外部流的就绪信号，所以当 FIFO 满时，外部流的 `Str_Vld` 会被反向顶住（backpressure）。

#### 4.3.4 代码实践

**实践目标**：亲手算一次 FIFO 宽度与字节计数，验证对打包方式的理解。

**操作步骤**：

1. 假设 `IntDataWidth_g = 64`、`StreamWidth_g = 32`。
2. 手算 `WconvFactor_c`、`BytesWidth_c`、`DataFifoWidth_c`。
3. 模拟「来了 1 个 32 位样本就发生触发」的场景，根据 [hdl/psi_ms_daq_input.vhd:L300](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L300) 的 `when 32 => v.DataFifoBytes := (r.WordCnt + AddSamples_v) & "00"` 算出 `DataFifoBytes` 的值（`AddSamples_v` 在非超时时为 1）。
4. 画出此时 `DataFifo_InData` 70 位里每一段的内容。

**预期结果**：

- `WconvFactor_c = 64/32 = 2`；`BytesWidth_c = 4`；`DataFifoWidth_c = 70`。
- 1 个样本、非超时：`r.WordCnt=0`，`AddSamples_v=1`，`(0+1)="01"`，`"01"&"00" = "0100" = 4`，即 `DataFifoBytes = 4`（4 个有效字节）。
- `DataFifo_InData`：`[bit69=IsTrig][bit68=IsTo][bit67:64=0100][bit63:0=只有低 32 位是样本, 高 32 位为 0]`。

**待本地验证**：在 testbench 里把 `StreamWidth_g` 设成 32 跑 `single_frame` 用例，观察 FIFO 写入波形是否与上述一致。

#### 4.3.5 小练习与答案

**练习 1**：数据 FIFO 为什么**不**开 `afull_on_g`（almost-full），而时间戳 FIFO 却开了？

**答案**：数据 FIFO 的反压路径是直接把 `in_rdy_o` 接到外部 `Str_Rdy`（第 534 行），FIFO 满时自然顶住上游，不需要额外几乎满告警。时间戳 FIFO 不同：时间戳是「内部产生」的（在触发时锁存 `Str_Ts`），没有外部上游可以反压，所以需要 `alm_full` 标志来让内部逻辑在 FIFO 快满时改写为全 1（`0xFF...`）以标记溢出（见 u2-l4）。

**练习 2**：`DataFifoWidth_c` 公式里的 `+ 2` 对应哪两个字段？

**答案**：`IsTo` 和 `IsTrig` 各 1 位，共 2 位。它们和 `DataSftReg`（数据）、`DataFifoBytes`（字节数）一起平铺进 FIFO。

---

### 4.4 psi_common_pl_stage 流水级、输出解包与时间戳 FIFO

#### 4.4.1 概念说明

异步 FIFO 的读侧直接驱动下游 `Daq_Data` 在时序上可能太紧（FIFO 输出寄存器 → 组合解包 → DMA 输入，路径较长）。因此源码在数据 FIFO 之后**再加一级 `psi_common_pl_stage` 流水级**，把「时序路径」切短。这一级带 Ready/Valid 握手（`use_rdy_g => true`），所以它不只是打一拍寄存器，还能像一个小「弹性节」一样在 FIFO 与 DMA 之间缓冲一拍。

输出侧还有一段 `p_outlast` 进程，负责在内存时钟域里维护「FIFO 里还剩几个末帧标志」和「数据水位」，分别上报为 `Daq_HasLast` 和 `Daq_Level`，供 DMA 状态机判断是否值得发起一次传输。

时间戳 FIFO 在结构上是「又一个异步 FIFO」，但它有独立的几乎满反压和「无有效时间戳时输出全 1」的特殊处理。

#### 4.4.2 核心流程

```text
ClkMem 域:
  DataFifo_PlData/PlVld ──▶ [pl_stage] ──▶ DataFifo_OutData/Daq_Vld_I
                                              │ 解包
                                              ▼
                                         Daq_Data_I.{Data,Bytes,IsTo,IsTrig,Last}
  p_outlast: OutTlastCnt(读侧消费) vs InTlastCnt(status_cc 同步过来) → Daq_HasLast
             DataFifo_Level + DataPl_Level → Daq_Level
时间戳: Str_Clk 域 r.TsLatch ──async_fifo──▶ ClkMem 域 Ts_Data/Ts_Vld
```

#### 4.4.3 源码精读

**流水级例化 `i_dplstage`**：

[hdl/psi_ms_daq_input.vhd:L518-L533](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L518-L533) —— 注释明白写着「An additional pipeline stage after the FIFO is required for timing reasons」。它接 FIFO 读侧的 `DataFifo_PlVld`/`DataFifo_PlData`/`DataFifo_PlRdy`，输出 `Daq_Vld_I`/`DataFifo_OutData`，下游 ready 接 `Daq_Rdy`。`use_rdy_g => true` 让这一级具备完整的握手能力。

**输出解包**：

[hdl/psi_ms_daq_input.vhd:L536-L542](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L536-L542) —— 把 70 位的 `DataFifo_OutData` 按 4.3.3 的打包格式**逆向切片**回 `Daq_Data_I` 的各字段：

```vhdl
Daq_Data_I.Data   <= DataFifo_OutData(IntDataWidth_g-1 downto 0);
Daq_Data_I.Bytes  <= DataFifo_OutData(IntDataWidth_g+BytesWidth_c-1 downto IntDataWidth_g);
Daq_Data_I.IsTo   <= DataFifo_OutData(DataFifo_OutData'high-1);
Daq_Data_I.IsTrig <= DataFifo_OutData(DataFifo_OutData'high);
Daq_Data_I.Last   <= Daq_Data_I.IsTo or Daq_Data_I.IsTrig;
```

注意 `Last` 是 `IsTo or IsTrig` 的组合结果——无论超时帧还是触发帧都算「一帧的末字」，这正是下游 DMA 拼 TLAST 的依据。

**`p_outlast` 进程：水位与 HasLast 上报**：

[hdl/psi_ms_daq_input.vhd:L364-L402](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L364-L402) —— 在 `ClkMem` 域里：
- 每当下游消费一个 `Last=1` 的字，`OutTlastCnt` 自增（[L372-L374](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L372-L374)）。
- 比较 `OutTlastCnt`（已消费）与 `InTlastCnt`（流时钟域同步过来的总写入数），不相等就拉高 `Daq_HasLast_I`（[L377-L379](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L377-L379)），告诉 DMA「FIFO 里还有未消费的完整帧」。
- `Daq_Level` = 数据 FIFO 水位 + pl_stage 里那一拍（`DataPl_Level`），给 DMA 一个「可用数据量」的视图（[L391](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L391)）。注释 [L390](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L390) 解释「晚一拍没关系，因为 DAQ FSM 只在数据真正搬完之后才动作」。

**时间戳 FIFO `g_timestamp`**：

[hdl/psi_ms_daq_input.vhd:L545-L574](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L545-L574) —— 由 `if StreamUseTs_g generate` 包裹，**只有在启用时间戳时才综合**。它把流时钟域的 `r.TsLatch` 经异步 FIFO 送到内存时钟域。与数据 FIFO 的关键区别：

- 开了 `afull_on_g => True`，`afull_lvl_g => StreamTsFifoDepth_g - 1`（几乎满）。
- `ram_style_g => TsFifoStyle_c`：当深度 ≤ 64 时用分布式 RAM，否则用块 RAM（[L83](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L83)）。
- 写使能 `TsFifo_InVld <= r.DataFifoVld and r.DataFifoIsTrig`：**只在触发帧时**才写一个时间戳。
- [L572-L573](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L572-L573)：当 `Ts_Vld_I='0'`（FIFO 空、无可用时间戳）时，`Ts_Data` 输出全 1 作为「无效标记」。

[hdl/psi_ms_daq_input.vhd:L576-L579](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L576-L579) —— `g_ntimestamp`：`StreamUseTs_g=false` 时综合的「占位」分支，固定输出 `Ts_Vld<='0'`、`Ts_Data<=全1`，保证端口在两种配置下都有驱动。

#### 4.4.4 代码实践

**实践目标**：理解 pl_stage 在数据通路里的位置与作用，以及「水位上报」的意义。

**操作步骤**：

1. 在 [hdl/psi_ms_daq_input.vhd:L518-L533](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L518-L533) 阅读流水级例化。
2. 假设把这一级删掉、把 `DataFifo_PlData/PlVld` 直接接到 `Daq_Data_I/Daq_Vld_I`，回答：功能上还能跑吗？时序上会有什么风险？
3. 读 [hdl/psi_ms_daq_input.vhd:L381-L391](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L381-L391) 的 `Daq_Level` 计算逻辑，解释为什么要把 `DataPl_Level` 也加进去。

**需要观察的现象 / 预期结果**：

- 删掉 pl_stage 后**功能仍可能正确**（握手语义在），但 FIFO 读端口寄存器到 DMA 输入之间的组合路径变长，高频下（`ClkMem=200 MHz`）可能时序违例——这正是注释「required for timing reasons」的含义。
- `Daq_Level` 必须加上 `DataPl_Level`，因为 pl_stage 里「暂时卡着但还没被 DMA 读走」的那一拍数据也是真实可用的，不计入会让 DMA 低估可用数据量、可能延迟发起传输。

**待本地验证**：在 testbench 里跑 `backpressure` 用例，观察 pl_stage 的 `vld_o` 在 `rdy_i=0` 时是否能稳定保持数据（即具备反压下的保持能力）。

#### 4.4.5 小练习与答案

**练习 1**：`Daq_Data_I.Last` 为什么定义为 `IsTo or IsTrig`？

**答案**：一帧数据的最后一个字有两种产生方式——要么是「触发条件满足」自然结束（`IsTrig`），要么是「超时冲刷」强制结束（`IsTo`）。无论哪种，对下游 DMA 而言都意味着「这一字之后就是一帧的边界」，应当用 AXI 的 TLAST 标记，所以 `Last` 取二者之或。

**练习 2**：时间戳 FIFO 的 `TsFifoStyle_c` 是怎么决定的？

**答案**：见 [L83](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L83)：`choose(StreamTsFifoDepth_g <= 64, "distributed", "block")`。小 FIFO（深度 ≤ 64，默认值 16 即属此类）用分布式 RAM（查找表实现，省块 RAM 资源），大 FIFO 才用块 RAM。

---

## 5. 综合实践

**任务**：跟踪一个样本从外部进入、到出现在 `Daq_Data` 上的完整旅程，并标出每一次「时钟域切换」。

设场景：`IntDataWidth_g=64`、`StreamWidth_g=16`，外部依次送来 4 个 16 位样本（`Str_Vld=1` 连续 4 拍），第 4 个样本同时拉高 `Str_Trig`。`Str_Clk=125 MHz`、`ClkMem=200 MHz`。

请按下列步骤完成：

1. **拼字阶段（Str_Clk 域）**：根据 [hdl/psi_ms_daq_input.vhd:L277-L288](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L277-L288)，画出 4 拍里 `r.WordCnt` 从 0 涨到 `WconvFactor_c-1=3` 后、第 4 个样本写入时 `DataFifoVld` 被拉高的过程，并指出 `DataFifoBytes` 的最终值。
2. **FIFO 打包（Str_Clk → ClkMem）**：根据 [hdl/psi_ms_daq_input.vhd:L492-L516](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L492-L516)，写出这次写入对应的 70 位 `DataFifo_InData` 各字段，并说明这是**第一次跨时钟域切换**（数据经异步 FIFO 进入 `ClkMem` 域）。
3. **pl_stage（ClkMem 域）**：根据 [hdl/psi_ms_daq_input.vhd:L518-L542](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L518-L542)，说明数据被流水级打一拍后解包到 `Daq_Data_I.Data/Bytes/IsTrig/Last`。
4. **TLAST 上报（Str_Clk → ClkMem，又一次 CDC）**：根据 [hdl/psi_ms_daq_input.vhd:L266-L268](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L266-L268) 与 [hdl/psi_ms_daq_input.vhd:L477-L489](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L477-L489)、[hdl/psi_ms_daq_input.vhd:L377-L379](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L377-L379)，说明 `r.TLastCnt` 在流时钟域 +1 后，如何经 `i_cc`（`status_cc`）同步到 `ClkMem` 域成为 `InTlastCnt`，使 `Daq_HasLast` 拉高。

**预期产出**：一张包含「阶段 / 所在时钟域 / 关键信号变化 / 跨域元件」四列的表格，至少标出 2 处跨时钟域（数据经 async_fifo、TLAST 计数经 status_cc）。这是一次「源码阅读型实践」，无需运行仿真即可完成；若要验证，可跑 input testbench 的 `single_frame` + `trig_in_posttrig` 用例对照波形。

## 6. 本讲小结

- `psi_ms_daq_input` 的 7 个 generic 控制管道容量与行为：`StreamWidth_g` 限定样本位宽（必须 8/16/32/64）、`StreamBuffer_g` 定 FIFO 深度、`StreamTimeout_g`×`StreamClkFreq_g` 编译期换算成周期数、`StreamUseTs_g` 决定是否综合时间戳通路。
- 模块横跨**三个时钟域**：`Str_Clk`（流，主逻辑）、`ClkReg`（寄存器配置/状态）、`ClkMem`（DMA 输出）。
- 跨时钟域按信号性质分类处理：多位电平用 `status_cc`、单比特电平用 `bit_cc`、脉冲用 `pulse_cc`、连续数据流用异步 FIFO。共 5 个 CDC 元件例化 + 2 个异步 FIFO。
- 数据通路是「样本拼字 → 异步数据 FIFO → `pl_stage` 流水级 → 解包成 `Daq_Data`」；FIFO 把数据与元数据（字节效、IsTo、IsTrig）平铺成 `DataFifoWidth_c` 位。
- `Daq_Level` / `Daq_HasLast` 是输入逻辑给 DMA 状态机的两条关键反馈：前者报水位（含 pl_stage 那一拍），后者报「FIFO 里还有完整帧」。
- 时间戳 FIFO 是「又一个异步 FIFO」，但只在触发帧时写、开启 almost-full 并在无数据时输出全 1 作为无效标记。

## 7. 下一步学习建议

本讲把「管道」（接口、时钟域、缓冲）讲完了，接下来按顺序：

- **u2-l3（输入逻辑核心：记录模式、触发与后触发计数）**：进入 `p_comb` 内部，讲 `TrigMasked_v` 在四种 `RecMode` 下的屏蔽规则、`PostTrigCnt` 后触发计数、`IsArmed`/`RecEna` 的置位与清除。本讲刻意回避的算法细节都在那里。
- **u2-l4（输入逻辑：超时、时间戳与跨时钟域状态）**：展开 `TimeoutCnt` 超时冲刷、`ToDisable`/`FrameTo` 控制、时间戳锁存与溢出处理、`Daq_HasLast`/`Level` 上报细节。
- **u2-l5（DMA 引擎接口与缓存结构）**：继续沿数据通路往下游走，看 `Daq_Data`/`Daq_Vld` 被 DMA 引擎如何消费。
- 想立刻动手验证本讲所学的，可以并行阅读 **u5-l1（测试平台结构与 PsiSim 仿真流程）**，学会跑 input testbench 的 6 组 generic 配置（8/16/32/64 流宽 + Vld 脉冲），在波形上亲眼看到本讲描述的三时钟域与各 CDC 实例。
