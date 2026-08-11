# 硬件前端与多板系统

## 1. 本讲目标

学完本讲，你应当能够：

- 说清这个数据采集系统**不止是一块 FPGA**，而是由「Nexys 4 DDR 开发板 + 四块外围 PCB + PC」组成的多板系统，并能说出每一块板在信号链中的职责。
- 识别 `TOP.v` 顶层模块中**真正与外部硬件打交道**的端口（`adc_read`、`clock_adc_out`、`adc_pwdn`、`adj`、`serial_in`、`serial_out`），并指出它们各自连向哪一块板。
- 理解 `adj[2:0]` 这 3 根线是 FPGA 对模拟前端**唯一的反向控制通道**，能复述它如何由 PC 的 `D` 命令在线改写，从而远程调节模拟前端的放大/衰减。
- 正确区分「可从源码/文本文档读到的部分」与「锁在 Altium 二进制工程里、无法用文本精读的部分」，对后者如实标注「待确认」，不臆造电路细节。

## 2. 前置知识

本讲属于专家层（advanced），默认你已完成：

- [u1-l2 仓库结构与复现方式](u1-l2-repo-structure-and-reproduction.md)：知道仓库里哪些是可读源码、哪些是二进制产物（比特流、Altium PCB、LabVIEW 工程）。
- [u2-l3 ADC 接口与 AD9215 时钟分频](u2-l3-adc-interface-and-clock-divider.md)：知道 AD9215 是 10 位并行 ADC，`read_adc` 主要负责生成采样时钟与掉电控制。

下面先把本讲用到的几个硬件术语讲清楚：

- **多板系统（multi-board system）**：完整的电子系统被拆成多块印制电路板（PCB），各板通过接插件/排针互连。本项目里 FPGA 板只是「大脑」，信号的调理、数字化、与 PC 通信、供电分别由不同的板承担。
- **模拟前端（Analog Front-End，AFE）**：传感器输出的真实世界信号（电压、电流、光、皮肤电阻）在进入 ADC 之前，要经过放大、衰减、滤波、电平偏移等处理，这段「ADC 之前」的模拟电路统称模拟前端。
- **可编程增益（Programmable Gain Amplifier，PGA）**：放大倍数可由数字信号选择的放大器。本项目的 `adj[2:0]` 就是用来选择增益/衰减档位的控制线。
- **AD9215**：Analog Devices 公司的 10 位、最高 210 MSPS 的并行 ADC，是本系统的数字化核心（详见 u2-l3）。
- **MCP2200**：Microchip 公司的 USB 2.0 转 UART（串口）桥接芯片。它一端接 PC 的 USB，另一端给出/接收标准的 UART 串口信号（TX/RX），让 FPGA 不必自己实现 USB 协议。
- **Altium Designer 与 `.PcbDoc`/`.SchDoc`**：Altium Designer 是常见的 EDA 画板软件。`.SchDoc` 是原理图（schematic），`.PcbDoc` 是印制板布局（PCB layout），`.PrjPcb` 是工程文件。三者都是**二进制格式**，无法用文本编辑器阅读，必须用 Altium Designer 打开。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 | 是否可文本精读 |
| --- | --- | --- |
| `readme.md` | 项目说明：三大功能、目录结构、复现三步法、团队信息 | ✅ 可读 |
| `verilog files/TOP.v` | 顶层模块，给出与外部硬件对接的端口（尤其 `adj`、`adc_read`、串口）与 `adj` 的改写逻辑 | ✅ 可读 |
| `Electronic boards design/` | 四块 PCB 的 Altium 工程文件（示波器板 / 心率板 / MCP2200 板 / 电源板） | ❌ 二进制，待确认 |

> 提醒：本仓库的文件名/模块名/例化名之间存在错位（详见 u1-l2、u2-l2），但本讲关注的是 `TOP` 的**对外端口**和**外部目录**，这两部分命名直观、可直接对照。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 多板系统组成**：系统由哪些板构成，信号如何在板与板之间、板与 FPGA 之间流动。
- **4.2 adj 模拟前端控制**：FPGA 用 `adj[2:0]` 这 3 根线反向控制模拟前端的增益/衰减，并可被 PC 远程改写。

---

### 4.1 多板系统组成

#### 4.1.1 概念说明

很多初学者会把「FPGA 工程」等同于「整个系统」。但对这个项目而言，`TOP.v` 只是系统里负责数字处理的那一部分。一个完整的数据采集系统，从真实世界信号到 PC 屏幕上的波形，至少要经过四件事：

1. **信号调理**：把传感器微弱/危险的模拟信号调理到 ADC 能接受的范围（放大、衰减、滤波、偏置）。
2. **数字化**：由 ADC 把模拟电压采成数字码字。
3. **数字处理**：在 FPGA 内做 FFT、幅度计算等（前面 u2、u3 单元讲过的 DSP 链）。
4. **与 PC 通信**：把结果送到上位机显示，并接收上位机的控制命令。

在本项目里，这四件事分别落在不同的板子上：模拟前端与 ADC 在「示波器板」上、数字处理在 Nexys 4 DDR 上、与 PC 通信靠「MCP2200 板」做 USB↔串口桥接、所有板由「电源板」供电；此外还有一块专做心率模拟前端的「心率板」。也就是说，**系统 = 多块 PCB + Nexys FPGA + PC**，缺一不可。


#### 4.1.2 核心流程

把系统看成一个「传感器 → … → PC」的信号通路，再叠加一条「PC → FPGA」的控制通路：

**下行数据通路（真实信号 → PC 显示）：**

```
传感器(探头/心率传感器/GSR电极)
   │  模拟信号
   ▼
模拟前端板(示波器板/心率板): 放大/衰减/滤波/偏置, 增益由 adj[2:0] 选择
   │  调理后的模拟电压
   ▼
AD9215 ADC: 10 位并行码字, 由 clock_adc_out 驱动采样
   │  10 位并行数字
   ▼
FPGA (Nexys 4 DDR, TOP.v): 采集→ram1→FFT→幅度→开方→ram3 (见 u2/u3 单元)
   │  打包后的串行数据 (serial_out, TX)
   ▼
MCP2200 板: UART ↔ USB 桥接
   │  USB
   ▼
PC: LabVIEW GUI 显示波形/频谱
```

**上行控制通路（PC 命令 → FPGA / 模拟前端）：**

```
PC GUI 发 ASCII 命令 (P/A/B/C/D)
   │  USB
   ▼
MCP2200 板: USB ↔ UART
   │  串口 RX (serial_in)
   ▼
FPGA (TOP.v): serial_rx 解析命令
   ├── 触发采集 (P)
   ├── 配置时基/触发电平/斜率 (A/B/C)
   └── 写 adj[2:0] → 模拟前端板改增益 (D)
```

注意两个方向：`serial_in`（FPGA 的 RX，PC 发来）走控制，`serial_out`（FPGA 的 TX，发给 PC）走数据。二者都经过 MCP2200 这座「USB↔串口桥」。

#### 4.1.3 源码精读

**① `TOP` 模块对外的硬件端口**——这是 FPGA 与四块板发生关系的唯一接口：

[verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31) 给出了顶层端口，每个端口都对应一块外部板：

```verilog
module TOP(input clk_in,             // Nexys 板载 100MHz 晶振 → 喂给 PLL
           input [9:0] adc_read,     // AD9215 送来的 10 位并行数据
           input serial_in,          // 串口 RX: 来自 MCP2200(来自 PC)
           output adc_pwdn,          // ADC 掉电控制(高有效)
           output clock_adc_out,     // 送给 AD9215 的采样时钟
           output reg [9:0] leds,    // LED 调试条
           output serial_out,        // 串口 TX: 发往 MCP2200(发往 PC)
           output reg [2:0] adj      // 模拟前端增益/衰减控制(3 位)
           );
```

把这 7 个对外端口和四块板对应起来：

| `TOP` 端口 | 方向 | 连向哪块板 | 作用 |
| --- | --- | --- | --- |
| `clk_in` | 输入 | Nexys 板载晶振 | 100 MHz，PLL 输入 |
| `adc_read[9:0]` | 输入 | 示波器板上的 AD9215 | 10 位并行采样数据 |
| `clock_adc_out` | 输出 | 示波器板上的 AD9215 | 送给 ADC 的转换时钟 |
| `adc_pwdn` | 输出 | 示波器板上的 AD9215 | 掉电控制（高有效） |
| `adj[2:0]` | 输出 | 示波器板/前端模拟电路 | 选增益/衰减档（见 4.2） |
| `serial_in` | 输入 | MCP2200 板 | PC→FPGA 命令 |
| `serial_out` | 输出 | MCP2200 板 | FPGA→PC 数据 |

**② 四块 PCB 的目录证据**——来自仓库实际文件清单（`git ls-files`），目录与文件均为 Altium 二进制：

| 目录 | 关键文件 | 推断职责 | 依据 |
| --- | --- | --- | --- |
| `Electronic boards design/Oscilloscope_board/` | `scope_in_pcb.PcbDoc`、`scope_in_sch.SchDoc` | 示波器模拟前端 + AD9215 ADC | 目录名 `scope_in` |
| `Electronic boards design/Heartbeat_measurement_board/` | `Heartbeat_pcb.PcbDoc`、`Heartbeat_sch.SchDoc` | 心率测量模拟前端（体积描记法） | 目录名 `Heartbeat` |
| `Electronic boards design/MCP2200_board/` | `pcb_mcp2200.PcbDoc`、`sch_mcp2200.SchDoc` | USB↔UART 桥接 | 目录名 `MCP2200`；预览里还有 `sch_mcp2200_izolat_v2`（隔离版） |
| `Electronic boards design/Power Supply_board/` | `PCB_NexisIV_ps.PcbDoc`、`SCH_NexisIV_ps.SchDoc` | 系统供电 | 目录名 `Power Supply`、文件名 `_ps`（power supply） |

> 说明：上表「推断职责」一栏来自**目录名与文件名**这类文本证据，可信；但各板的具体电路（用了哪颗运放、几档增益、电源输出几伏）锁在 `.SchDoc`/`.PcbDoc` 二进制里，**待确认**。MCP2200 板预览里出现的 `izolat_v2`（罗马尼亚语 izolat = isolated，隔离）暗示存在一个电气隔离版本，这在医疗/EDA 类设备里常见（保障人身安全），但具体隔离方式**待确认**。

**③ readme 的复现三步法**——印证了「多板」这一事实：复现项目不只是烧比特流，还要把 PCB 板接上：

[readme.md:28-34](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L28-L34) 三步分别是：①把比特流烧进 Nexys 板；②复现 PCB 板并连到 Nexys；③打开 GUI 连 PC。第二步「复现 PCB 并连接」正是本讲强调的多板系统——没有这些板，FPGA 的 `adc_read`/`adj`/串口端口就无处可接。

#### 4.1.4 代码实践

**实践目标：** 用源码和文件清单，独立还原出系统的硬件信号通路。

**操作步骤：**

1. 打开 [verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31)，把 7 个对外端口逐个列在纸上。
2. 运行 `git ls-files 'Electronic boards design/*'`（或直接浏览该目录），把四块板的名字记下来。
3. 把 4.1.2 节那张「下行数据通路 / 上行控制通路」的框图**凭记忆**重画一遍，每条线上标注对应的 `TOP` 端口名（`adc_read`/`clock_adc_out`/`adj`/`serial_in`/`serial_out`）。

**需要观察的现象：**

- 你会发现 `TOP` 的对外端口刚好「一对一」地落在示波器板（ADC 三件套 + adj）、MCP2200 板（串口两根线）上；心率板与电源板不直接出现在 `TOP` 端口里——心率板是通过「共享模拟前端 → ADC」间接进入 `adc_read` 的，电源板只供电、不传信号。

**预期结果：**

- 画出一张包含「传感器 → 前端板(含 AD9215) → FPGA → MCP2200 → PC」的主链路，外加「adj[2:0] 从 FPGA 回流到前端板」「PC 命令经 MCP2200 进 serial_in」两条控制/回流线。

**待本地验证：**

- 各板内部电路无法从文本确认，需用 Altium Designer 打开 `.SchDoc` 才能核对你画的连接是否与原理图一致。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `adc_read` 是 `input`（输入），而 `clock_adc_out` 是 `output`（输出）？方向是否矛盾？

> **答案：** 不矛盾。`adc_read` 是 AD9215 送给 FPGA 的「数据」，对 FPGA 是输入；`clock_adc_out` 是 FPGA 送给 AD9215 的「采样时钟」，对 FPGA 是输出。ADC 在 FPGA 提供的时钟节拍下转换，再把数据送回 FPGA——所以一根线进、一根线出，方向相反但配合工作（详见 u2-l3 的命名陷阱说明）。

**练习 2：** 如果不接 MCP2200 板，系统能正常工作吗？为什么？

> **答案：** 不能完整工作。FPGA 仍能采集和处理（DSP 链不需要 PC），但无法把结果送给 PC 显示，也收不到 PC 的 `P` 命令来触发采集——`serial_in`/`serial_out` 两根线没有对端。MCP2200 是 FPGA 与 PC 之间唯一的桥梁。

**练习 3：** 心率板似乎没有直接连到 `TOP` 的任何端口，它的信号是怎么进入 FPGA 的？

> **答案：** 心率板做的是模拟前端（体积描记法），它的输出仍是模拟电压，应当接到示波器板/前端板的模拟输入，再经同一颗 AD9215 数字化进入 `adc_read`。也就是说，三种功能（示波器/心率/GSR）复用同一条「模拟前端 → ADC → FPGA」数字骨架，区别主要在模拟前端，而不是 HDL。


---

### 4.2 adj 模拟前端控制

#### 4.2.1 概念说明

`adj[2:0]` 是 FPGA 对模拟前端**唯一的反向控制通道**。前面所有讲义里，FPGA 几乎都在「被动接收」ADC 的数据；唯独 `adj` 是 FPGA 主动去「调节」前端模拟电路的信号。

它的物理含义是：用 3 根数字线选择模拟前端的增益/衰减档位。3 位二进制能表示 \( 2^3 = 8 \) 个离散档（0~7）。代码注释写得很直白：

> `output reg [2:0] adj` —— adjustments for the analogic circuit (set the amplification/attenuation)
> （对模拟电路的调整：设定放大/衰减）

为什么要把增益做成可编程？因为不同传感器的信号幅度差异巨大：示波器探头可能送几百毫伏到几伏，心率光电传感器信号可能只有几毫伏，GSR 信号又是另一量级。用固定增益无法兼顾，所以需要 PC 根据当前测量对象，远程选一档合适的放大倍数——这正是 `adj` 的用途。

#### 4.2.2 核心流程

`adj` 的值由两条路径决定：

**① 上电默认值**——在 `init_state`（系统加载后只执行一次）里，`adj` 被设为默认档 `3'b100`（十进制 4），即 8 档的中位偏上：

```
init_state: adj <= 3'b100;   // 上电默认增益档 = 4
```

**② PC 在线改写（`D` 命令）**——这是 `adj` 真正有用的地方。PC 通过串口发 `D`，再发一个字节；FPGA 取该字节的低 3 位写入 `adj`。采用与 `A/B/C` 命令相同的「两步式」解析（详见 u5-l1）：

```
PC 发 'D' (0x44)        → conf_index <= 3'b100   // 记住"下一个字节是增益参数"
PC 发 参数字节           → adj <= 参数字节[2:0]    // 取低 3 位作为新增益档
                          conf_index <= 0         // 消费完, 复位
```

完整的命令分派见 [verilog files/TOP.v:266-311](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L266-L311)。

#### 4.2.3 源码精读

**① 默认增益档在初始化时设定：**

[verilog files/TOP.v:251-261](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L251-L261) 中，`init_state` 给一批配置寄存器赋初值，其中就包括 `adj`：

```verilog
init_state: begin
    trig_value<=8'b10000000;
    slope_adj<=1'b0;
    state<=wait_state;
    conf_index<=0;
    adj<=3'b100;        // ← 默认增益档 = 4
    ...
end
```

**② 命令字母的分派**——`A/B/C/D` 各自把 `conf_index` 置成不同值，标记「下一个字节属于哪一项配置」：

[verilog files/TOP.v:279-283](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L279-L283)

```verilog
if(dserial_in == 8'b01010000) state<=trig_state;     // 'P' → 立即触发
else if(dserial_in == 8'b01000001) conf_index<=3'b001;  // 'A' → 下字节配 timebase
else if(dserial_in == 8'b01000010) conf_index<=3'b010;  // 'B' → 下字节配 trig_value
else if(dserial_in == 8'b01000011) conf_index<=3'b011;  // 'C' → 下字节配 slope_adj
else if(dserial_in == 8'b01000100) conf_index<=3'b100;  // 'D' → 下字节配 adj(增益)
```

> 8 位 ASCII：`0x50`='P'、`0x41`='A'、`0x42`='B'、`0x43`='C'、`0x44`='D'。

**③ 参数字节的消费**——当 `conf_index==3'b100`（即上一步收到过 `D`）时，把下一个串口字节的低 3 位写进 `adj`：

[verilog files/TOP.v:301-304](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L301-L304)

```verilog
3'b100: begin          // 'D' 的参数字节
    conf_index<=3'b000;
    adj<=dserial_in[2:0];   // ← 取低 3 位作为增益档
end
```

几个关键结论：

- `adj` 是 **3 位**，所以只有 8 档（0~7）。每档对应的**实际放大倍数**取决于模拟前端电路（PGA/电阻网络），这部分在二进制 `.SchDoc` 里，**待确认**——源码只承诺「把 0~7 这 8 个码字送出去」，不承诺每个码字等于几倍。
- PC 只需发 `D` 再发「低 3 位为目标档位」的字节即可。例如想选第 6 档（`3'b110`），可发 `D` 再发 `0x06`（低 3 位 = 110）。
- `adj` 的改写发生在 `wait_state`（空闲/配置态），不在采集进行中改，避免采到一半换增益。


#### 4.2.4 代码实践

**实践目标：** 手动模拟一次 PC 远程调节增益的过程，算出 `adj` 最终的值。

**操作步骤：**

1. 打开 [verilog files/TOP.v:301-304](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L301-L304)，确认 `adj` 取自参数字节的 `[2:0]`。
2. 假设 PC 想把增益设到第 5 档（`3'b101`）。问：第二个字节应该发什么？写出至少两个合法的字节值。
3. 假设 PC 误把命令字母当成参数发了——例如连发两个 `D`（`0x44`、`0x44`）。算一算：第二个 `0x44` 会被当成参数字节吗？此时 `adj` 变成什么？

**需要观察的现象 / 预期结果：**

- 第 2 步：`adj` 只看低 3 位 `101`，所以第二个字节低 3 位必须是 `101`，例如 `0x05`（`00000101`）、`0x0D`（`00001101`）、`0xFD`（`11111101`）等都合法——高 5 位被忽略。
- 第 3 步：第一个 `D` 把 `conf_index` 置为 `3'b100`；第二个 `D`（`0x44 = 01000100`）此时**不是**命令字母（因为系统正在等参数），它的低 3 位 `100` 被写入 `adj`，于是 `adj` 变成 `3'b100`（第 4 档），同时 `conf_index` 清零。这是一个真实的「协议无校验」副作用（见 u6-l2 的协议缺陷讨论）。

**待本地验证：**

- 第 4 档（`3'b100`）对应的实际电压增益倍数，需查模拟前端原理图（`.SchDoc`）确认。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `adj` 是 3 位而不是 8 位或 16 位？位数与档位是什么关系？

> **答案：** n 位能表示 \( 2^n \) 个档。3 位 = 8 档（0~7），通常对示波器这类「按 1-2-5 序列分档」的应用已经够用（如 1×、2×、5×、10×…）。位数越多档位越细，但需要前端有相应的多路开关/电阻网络，也会占用更多 FPGA 引脚。具体档数是硬件前端的工程设计取舍，源码不解释原因，**待确认**。

**练习 2：** `adj` 的值在什么时刻才能被改写？为什么不在采集进行中改？

> **答案：** `adj` 的改写逻辑写在 `wait_state`（空闲/配置态）里。采集（`acq_state` 及之后）进行中不改，是为了避免「一帧数据前半段用高档增益、后半段用低档增益」，那会让波形不连续、FFT 失真。这也是工程上「配置与采集分离」的常见做法。

**练习 3：** 如果完全不接模拟前端板（`adj` 三根线悬空），FPGA 还能跑吗？`adj` 还有意义吗？

> **答案：** FPGA 仍能正常跑——`adj` 是 `output`，FPGA 内部照样会按 `init_state`/`D` 命令改变它的值，DSP 链也照常工作。但悬空的 `adj` 不会驱动任何模拟电路，增益调节形同虚设，`adc_read` 也会因为没接 ADC 而无意义。这再次说明：FPGA 工程能「编译通过」不等于「系统能用」，必须板子接齐。

---

## 5. 综合实践

把本讲两个最小模块串起来，完成一张**带控制的硬件信号通路图**。

**任务：** 画一张系统全图，要求同时体现「下行数据通路」「上行控制通路」与「adj 回流控制」三部分，并在每条线上写出对应的 `TOP` 端口名与时钟域。

**建议步骤：**

1. 先画主干（传感器 → 前端板 → AD9215 → FPGA → MCP2200 → PC），对照 [verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31) 给每段标端口：`adc_read`、`clock_adc_out`、`serial_out`。
2. 再叠加控制通路：PC → MCP2200 → `serial_in` → FPGA 的命令解析（[verilog files/TOP.v:266-311](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L266-L311)）。
3. 最后画出 **`adj[2:0]` 的回流箭头**：从 FPGA 指回前端板的「增益/衰减」框，并标注它由 `D` 命令驱动（[verilog files/TOP.v:301-304](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L301-L304)）。
4. 在图旁用一句话注明「电源板给所有板供电」「MCP2200 板做 USB↔串口桥」「心率/GSR 的特色在模拟前端而非 HDL」。

**自检：** 如果你的图里出现了「PC 直接发命令到 AD9215」或「adj 控制 ADC 时钟」之类的箭头，就画错了——`adj` 只控模拟前端的增益，ADC 时钟是 `clock_adc_out`（见 u2-l1、u2-l3）。

## 6. 本讲小结

- 这个系统**不止是 FPGA**，而是「Nexys 4 DDR + 四块 PCB（示波器板/心率板/MCP2200 板/电源板）+ PC」的多板系统；缺任何一类板，系统都无法完整工作。
- `TOP.v` 的对外端口（`adc_read`/`clock_adc_out`/`adc_pwdn`/`adj`/`serial_in`/`serial_out`）就是 FPGA 与这些板发生关系的唯一接口，可逐个对应到示波器板的 AD9215 与 MCP2200 板的串口。
- `adj[2:0]` 是 FPGA 对模拟前端**唯一的反向控制通道**，3 位 = 8 个增益/衰减档；上电默认第 4 档（`3'b100`），由 PC 的 `D` 命令取参数字节低 3 位在线改写。
- 三种功能（示波器/心率/GSR）**复用同一条数字骨架**（模拟前端→AD9215→FPGA→MCP2200→PC），差异主要在模拟前端板，而不是 HDL。
- 四块板的内部电路都是 Altium 二进制工程（`.SchDoc`/`.PcbDoc`），无法用文本精读，**细节待确认**；本讲所有电路层面的结论只依据目录名/文件名/源码端口，没有臆造。

## 7. 下一步学习建议

- 想从「系统视角」收尾，建议接着学 [u6-l2 PC 命令协议与 LabVIEW GUI 集成](u6-l2-pc-protocol-and-labview-gui.md)，把本讲的硬件通路与上位机协议对齐。
- 想看项目的整体局限与可扩展方向（多通道、更高波特率、把心率/GSR 做进 HDL 等），读 [u6-l4 扩展实践与改进方向](u6-l4-extensions-and-improvements.md)。
- 若手头有 Altium Designer，可以打开 `Electronic boards design/` 下的 `.SchDoc` 核实本讲标注「待确认」的电路细节（增益档位、电源电压、MCP2200 的隔离方式），把二进制工程转成可读结论。
- 配套资料：根目录 `XUP Documentation.pdf` 与 readme 里的 YouTube 演示视频（<https://www.youtube.com/watch?v=AU6mxlfZs50>）可能包含硬件实物图，值得作为本讲的视觉补充。
