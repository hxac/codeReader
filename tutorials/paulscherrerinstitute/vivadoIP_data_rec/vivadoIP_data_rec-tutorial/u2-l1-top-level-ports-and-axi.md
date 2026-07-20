# 顶层封装端口与 AXI4 Slave 接口

## 1. 本讲目标

本讲是从「能跑起来」迈向「能读源码」的桥梁。`data_rec` 这个 IP 核真正对外暴露的「门面」不是核心记录器 `data_rec.vhd`，而是它的 Vivado 封装层 `data_rec_vivado_wrp.vhd`。学完本讲你应该能够：

- 列出 `data_rec_vivado_wrp` 的全部端口，并按 **数据 / 触发 / AXI / 时钟复位 / 中断** 五组分类。
- 说出 AXI4 Slave 五个通道（读地址、读数据、写地址、写数据、写响应）各自包含哪些信号，以及 VALID/READY 握手的含义。
- 明确区分**数据时钟域**（`Clk`）与 **AXI 时钟域**（`s00_axi_aclk`），并指出每个端口属于哪个域。
- 解释 `Done_Irq` 中断与 `Trig_Out` 触发转发端口的来源、使能条件与所属时钟域的差异。

> 承接：u1-l4 已经讲过 IP 打包（`package.tcl` / `component.xml` / xgui 三件套）以及 AXI 默认地址宽度 `C_S00_AXI_ADDR_WIDTH=14`（16 KiB 空间）。本讲进入这 16 KiB 空间「长什么样」的入口——顶层端口本身；具体的寄存器/存储地址地图留给 u2-l2。

---

## 2. 前置知识

在阅读端口列表之前，先用三段通俗的话建立直觉。

### 2.1 什么是「封装层（wrapper）」

FPGA 里有两类代码：一类是**纯算法逻辑**（`data_rec.vhd`，只认 `Clk`、做采样记录），它不关心外面的世界怎么访问自己；另一类是**封装层**（`data_rec_vivado_wrp.vhd`），它负责把算法逻辑「翻译」成 Vivado / Zynq PS（处理器的硬核）能识别的标准接口。封装层要做三件事：

1. 把算法需要的配置/状态信号，对接到一条标准总线上（这里是 AXI4 Slave）。
2. 处理两个时钟域之间的数据搬运（数据时钟 vs AXI 时钟）。
3. 实例化存储 RAM 并把读出端口接到总线上。

所以封装层像一个「插座+翻译器」：核心记录器插进去，外部 CPU 通过标准插座就能驱动它。

### 2.2 什么是 AXI4

AXI（Advanced eXtensible Interface）是 ARM 提出的片上总线协议，Xilinx Zynq / Versal 里 PS 和 PL（可编程逻辑）之间的通信几乎都用它。AXI4 分三种变体，本 IP 用的是 **AXI4（含 burst）** 的 Slave 角色——也就是说 CPU 是 Master（主），`data_rec` 是 Slave（从）。软件读写寄存器、读回录制样本，都走这条总线。

AXI4 最核心的特点是**五个独立的通道**，每个通道都靠一对 `VALID`/`READY` 信号握手：只有双方同时为 1，这一次传输才生效。

### 2.3 什么是时钟域，为什么要分两个

「时钟域」就是被同一个时钟驱动的所有触发器的集合。如果两块逻辑跑在不同的时钟频率上，它们之间的信号就不能直接连线——否则会出现「建立时间不满足」的亚稳态（metastability），采集到错误的值。因此需要**跨时钟域（Clock Domain Crossing, CDC）** 同步电路（本仓库用 `psi_common_status_cc` / `psi_common_pulse_cc`，细节在 u5-l2）。

本 IP 有意设计成双时钟：

- **数据时钟域** `Clk`：核心记录器采样数据的地方，频率可能很高、和外部 ADC 对齐。
- **AXI 时钟域** `s00_axi_aclk`：总线访问的地方，通常等于 Zynq PS 的总线时钟（如 100 MHz 或 150 MHz）。

记住这条主线，本讲所有的端口分类都围绕它展开。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `hdl/data_rec_vivado_wrp.vhd` | 封装层，唯一的 entity `data_rec_vivado_wrp` | entity 的 generic 与全部 port；`Done_Irq` 的产生；`Trig_Out` 的直通 |
| `component.xml` | IP-XACT 描述，Vivado 实际读取的「IP 身份证」 | 总线接口（aximm/clock/reset/interrupt）的声明、端口使能条件 |

> 说明：封装层内部还实例化了 AXI 解码器、跨时钟域、TDP RAM 等，但**本讲只看「端口表面」**。内部解码与跨时钟域分别在 u5-l1、u5-l2 专讲。

---

## 4. 核心概念与源码讲解

### 4.1 data_rec_vivado_wrp 实体：generic 与数据/触发/控制端口

#### 4.1.1 概念说明

封装层的 entity 名叫 `data_rec_vivado_wrp`。它的端口可以粗分为两大块：

- **「算法侧」端口**：直接对接核心记录器的数据、触发、时钟复位信号。这些端口是 IP 真正「干活」的地方。
- **「总线侧」端口**：全部以 `s00_axi_` 开头，对接 CPU（下一节专讲）。

generic（类属参数）决定了端口的**数量和宽度**——这是 IP 可重配置的关键。读者要建立的第一个直觉是：**改 generic，端口形状就跟着变**，这正是 u1-l4 讲过的「可选端口按 generic 使能」在源码层面的体现。

#### 4.1.2 核心流程

1. 用户在 Vivado GUI 里设置 generic（如 `NumOfInputs_g`、`InputWidth_g`）。
2. 这些值通过 `component.xml` → 综合时传入 entity 的 generic 列表。
3. entity 里用 generic 推导端口宽度：`In_Data0` 宽度 = `InputWidth_g`，`Trig_In` 宽度 = `TrigInputs_g`。
4. `component.xml` 中的使能条件（`dependency`）根据 generic 决定哪些端口在最终 IP 上「存在」。

#### 4.1.3 源码精读

先看 generic 声明，注意每个参数的取值范围与默认值：

[hdl/data_rec_vivado_wrp.vhd:24-36](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L24-L36>) —— 五个「算法」generic（`NumOfInputs_g` 1–8 默认 4、`InputWidth_g` 1–32 默认 8、`MemoryDepth_g` 默认 128、`TrigInputs_g` 0–8 默认 1、`TrigForwarding_g` 布尔默认 false）加两个 AXI generic（`C_S00_AXI_ID_WIDTH` 默认 1、`C_S00_AXI_ADDR_WIDTH` 默认 14）。

再看「算法侧」端口：

[hdl/data_rec_vivado_wrp.vhd:39-55](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L39-L55>) —— 这段定义了三组端口：

- **控制信号** `Clk`/`Rst`（数据时钟域的时钟与高有效复位）；
- **数据端口** `In_Data0..In_Data7`（每个宽度为 `InputWidth_g`）加一个共用的 `In_Vld`（有效标志，所有通道共享）；
- **触发端口** `Trig_In`（宽度 `TrigInputs_g`）、`Trig_Out`、`Done_Irq`。

注意一个细节：`In_Data0..7` 共声明了 8 个独立端口（不是数组），实际用几个由 `NumOfInputs_g` 决定；`Trig_In` 则是一个真正的向量，宽度由 `TrigInputs_g` 决定。**数据通道数与外部触发数是两个独立的 generic**，这是 v1.1.0 起「最多 8 路外部触发」特性的体现。

对应的端口使能条件在 `component.xml` 里，以 `In_Data0` 为例：

[component.xml:532](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L532>) —— `dependency="$NumOfInputs_g > 0"`，即第 `i` 路数据端口当 `NumOfInputs_g > i` 时才存在。`Trig_Out` 的使能见 4.3.3。

#### 4.1.4 代码实践

**实践目标**：体会「generic 决定端口形状」。

**操作步骤**：

1. 打开 `hdl/data_rec_vivado_wrp.vhd`，定位到 24–55 行的 generic 与端口声明。
2. 假设把 generic 改成 `NumOfInputs_g => 2, InputWidth_g => 16, TrigInputs_g => 4`。
3. 在纸上列出：哪些 `In_Data` 端口实际有效？每个的位宽是多少？`Trig_In` 的位宽是多少？

**需要观察的现象**：端口数量与位宽随 generic 变化，但 entity 源码本身一行都不用改——这就是 generic 参数化的威力。

**预期结果（自检答案）**：有效数据端口为 `In_Data0`、`In_Data1`（共 2 个，每个 16 位）；`In_Data2..7` 在 IP 中不存在（被使能条件裁掉）；`In_Vld` 仍是 1 位；`Trig_In` 为 4 位（`downto 0` 即 3 downto 0）。

#### 4.1.5 小练习与答案

**练习 1**：为什么数据通道数（`NumOfInputs_g`）和外部触发数（`TrigInputs_g`）要设计成两个独立的 generic，而不是合二为一？

> **答案**：因为两者语义不同。数据通道是被记录的模拟/数字样本源；外部触发是「告诉记录器现在该触发」的控制信号。一个系统可能有 6 路数据但只有 1 路触发，也可能 1 路数据但用 4 路触发做 OR 合成。分开后才能覆盖所有组合，不至于被迫「触发数=数据数」。

**练习 2**：`In_Vld` 为什么是所有通道共用一个，而不是每路各一个？

> **答案**：本 IP 假定所有通道**同步采样**——每个 `Clk` 周期只要 `In_Vld=1`，就把 8 路 `In_Data` 同时当作一个完整样本写入存储。因此一个有效标志即可，省端口也简化了后续打包/对齐逻辑。

---

### 4.2 AXI4 Slave 接口（s00_axi_*）：五个通道

#### 4.2.1 概念说明

`data_rec_vivado_wrp` 的「总线侧」端口全部以 `s00_axi_` 开头，`s00` 表示「第 0 个 Slave 端口」。这一大段端口实现了一个**AXI4 Slave**，CPU 通过它读写寄存器、读回录制数据。

AXI4 把一次完整通信拆成**五个独立通道**，每个通道都是单向的、自带握手：

| 通道 | 缩写前缀 | 方向（相对 Slave） | 作用 |
| --- | --- | --- | --- |
| 读地址 | `AR` | Master→Slave | 告诉 Slave「我要读这个地址」 |
| 读数据 | `R` | Slave→Master | Slave 返回数据与响应 |
| 写地址 | `AW` | Master→Slave | 告诉 Slave「我要写这个地址」 |
| 写数据 | `W` | Master→Slave | Master 送出数据（含字节使能 `WSTRB`） |
| 写响应 | `B` | Slave→Master | Slave 报告本次写是否成功 |

读操作用 AR+R 两个通道；写操作用 AW+W+B 三个通道。读、写之间彼此独立，可以并发。

#### 4.2.2 核心流程

一次**写寄存器**（例如软件写 Arm 位启动录制）的流程：

1. Master 在 **AW 通道**给出地址，拉高 `AWVALID`；Slave 接受后拉高 `AWREADY`。
2. Master 在 **W 通道**给出 32 位数据与 `WSTRB`（选择哪几个字节有效），握手。
3. Slave 在 **B 通道**返回 `BRESP`（00=OKAY）确认。

一次**读样本**的流程：

1. Master 在 **AR 通道**给出地址，握手。
2. Slave 在 **R 通道**返回 32 位 `RDATA` 与 `RLAST`（突发最后一拍）。

每个通道的核心是 **VALID/READY 握手**：

\[ \text{传输发生} \iff \text{VALID}=1 \land \text{READY}=1 \quad (\text{在同一个上升沿}) \]

只有 `VALID` 不够（Slave 没准备好），只有 `READY` 也不算（Master 没发）。这正是 AXI 与简单「读/写使能」总线的本质区别。

#### 4.2.3 源码精读

AXI 端口完整声明在这里（注意每个通道用注释分开）：

[hdl/data_rec_vivado_wrp.vhd:59-101](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L59-L101>) —— 依次是：系统信号（`s00_axi_aclk`/`s00_axi_aresetn`）、读地址通道、读数据通道、写地址通道、写数据通道、写响应通道。

几个值得逐一看的信号：

- `s00_axi_araddr`（读地址）/ `s00_axi_awaddr`（写地址）：宽度 `C_S00_AXI_ADDR_WIDTH`（默认 14），这就是 u1-l4 提到的 16 KiB 地址空间入口。
- `s00_axi_rdata`（读数据，固定 32 位）/ `s00_axi_wdata`（写数据，32 位）：寄存器和存储都以 32 位字为单位访问。
- `s00_axi_wstrb`（写字节使能，4 位）：允许软件只改一个字里的某几个字节——本 IP 在寄存器解码时会用到这一特性。
- 每个 `VALID` 都配一个 `READY`，方向相反（VALID 由发起方给，READY 由接受方给）。

`component.xml` 把这些端口正式「绑定」成一个 AXI 总线接口：

[component.xml:8-11](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L8-L11>) —— 接口名 `s00_axi`，总线类型 `xilinx.com:interface:aximm:1.0`（Xilinx 对 AXI 内存映射总线的标准定义）。

随后从第 15 行到第 296 行是一长串 `portMap`，把逻辑名（如 `AWADDR`）映射到物理端口（如 `s00_axi_awaddr`）。Vivado 正是靠这份映射知道「这些端口属于同一条 AXI 总线」，从而在 Block Design 里把它们画成一根粗线、自动连到 PS 的 AXI 主口。

#### 4.2.4 代码实践

**实践目标**：在源码里把五个通道的信号数清楚，并与 `component.xml` 的 portMap 对照。

**操作步骤**：

1. 在 `hdl/data_rec_vivado_wrp.vhd` 第 59–101 行，按注释把端口分成五组（AR / R / AW / W / B）。
2. 数一数每个通道有几个信号，并标出每对 `VALID/READY`。
3. 打开 `component.xml`，搜索 `AWADDR`、`WDATA`、`BRESP`、`ARVALID`、`RLAST`，确认它们的物理端口名与 VHDL 完全一致。

**需要观察的现象**：VHDL 端口名与 `component.xml` 的 physicalPort 名一一对应；逻辑名（AWADDR 等）则是 AXI 标准规定的不变的「插座定义」。

**预期结果**：

| 通道 | 信号数（含握手对） | VALID / READY 对 |
| --- | --- | --- |
| 读地址 AR | 10 | ARVALID / ARREADY |
| 读数据 R | 6 | RVALID / RREADY |
| 写地址 AW | 10 | AWVALID / AWREADY |
| 写数据 W | 5 | WVALID / WREADY |
| 写响应 B | 4 | BVALID / BREADY |

> 上述计数来自源码第 63–101 行的逐行声明，读者可自行核对；若你的统计略有出入，以源码为准。

#### 4.2.5 小练习与答案

**练习 1**：`WSTRB`（写使能，4 位）出现在哪个通道？为什么需要它？

> **答案**：出现在 **W（写数据）通道**。`wdata` 是 32 位（4 字节），`WSTRB` 每一位对应一个字节，允许软件只更新其中某些字节而保持其余不变。本 IP 的寄存器字段往往共享一个 32 位字，逐字段写入就靠它。

**练习 2**：`C_S00_AXI_ADDR_WIDTH` 默认为 14，对应的地址空间是多大？为什么 u1-l4 说它是 16 KiB？

> **答案**：\(2^{14} = 16384\) 字节 = 16 KiB。这 16 KiB 既要放 32 个 32 位寄存器（共 128 字节），又要放每通道的录制样本存储（最多 8 通道 × `MemoryDepth_g` × 4 字节），地址地图在 u2-l2 展开。

---

### 4.3 Done_Irq 中断与 Trig_Out 触发转发

#### 4.3.1 概念说明

封装层除了「被动」接受总线访问，还有两个**主动输出**端口：

- **`Done_Irq`**：一段录制完成后，向 CPU 发出的中断信号（IRQ = Interrupt ReQuest）。这样软件不必轮询状态寄存器，录制一好就被通知。
- **`Trig_Out`**：把 IP 内部**经裁决后真正使用的触发**转发出去，供级联其他记录器或与外部逻辑同步。这是 v2.4 新增的可选端口（见 Changelog 与 u1-l1）。

这两个端口看似都是「输出一个信号」，但在**时钟域归属**和**是否跨域**上有重要差别——这正是本讲的第三个核心认知。

#### 4.3.2 核心流程

- **`Done_Irq` 的产生链**：核心记录器在数据时钟域 `Clk` 上产生一个 `Done` 脉冲（`port_done`）→ 经**脉冲型跨时钟域** `psi_common_pulse_cc` 同步到 AXI 时钟域 `s00_axi_aclk` → 成为 `Done_Irq`。
  - 为什么要跨到 AXI 域？因为中断最终要被连到 Zynq PS 的 GIC（通用中断控制器），而 GIC 跑在 AXI 时钟上。
- **`Trig_Out` 的产生链**：核心记录器在数据时钟域产生内部 `Trigger_2` 信号 → **直接**赋值给 `Trig_Out`，**没有任何跨时钟域电路**。
  - 为什么不跨域？因为 `Trig_Out` 的使用者通常是同处数据时钟域的相邻 FPGA 逻辑（如另一个记录器），它们共用 `Clk`。

由此可见，封装层并非「把所有信号都同步到一个域」，而是**按目标接收方所在的时钟域决定是否同步**。

#### 4.3.3 源码精读

`Done_Irq` 的跨时钟域链路：

[hdl/data_rec_vivado_wrp.vhd:437-455](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L437-L455>) —— 第 438 行把数据域的 `port_done` 喂进 `CcPToAxIn`；第 440–453 行实例化 `psi_common_pulse_cc`，注意 `a_clk_i => Clk`（源端数据时钟）、`b_clk_i => s00_axi_aclk`（目标 AXI 时钟）；第 455 行 `Done_Irq <= CcPToAxOut(...)`，输出已在 AXI 时钟域。

`Trig_Out` 的直通（注意：封装层只是把它从 `data_rec` 引出，未做任何同步）：

[hdl/data_rec_vivado_wrp.vhd:460-509](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L460-L509>) —— 第 484 行 `Trig_Out => Trig_Out`，把核心记录器的 `Trig_Out` 直接连到顶层端口。源端定义在 `data_rec.vhd` 第 393 行 `Trig_Out <= r.Trigger_2`，完全在数据时钟域 `Clk` 上。

`Trig_Out` 的使能条件（v2.4 新增）：

[component.xml:774](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L774>) —— `dependency="$TrigForwarding_g = true"`，只有 generic `TrigForwarding_g` 为真时 `Trig_Out` 端口才在 IP 中存在。这与 u1-l4 讲的 xgui 参数 `TrigForwarding_g` 默认 `false` 对应——不开转发时该端口根本不出现。

`Done_Irq` 在 `component.xml` 中被声明为中断接口：

[component.xml:346-366](<https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L346-L366>) —— 接口名 `Done_Irq`，总线类型 `xilinx.com:signal:interrupt:1.0`，角色为 `<spirit:master/>`（IP 是中断的发送方），敏感度 `SENSITIVITY = LEVEL_HIGH`。这样 Vivado 会自动把它接到 PS 的中断汇总（concat）上。

> 提示：`Done_Irq` 在 AXI 时钟域上是 `pulse_cc` 输出的单拍脉冲，而 `component.xml` 把它声明为 `LEVEL_HIGH` 中断。两者并不矛盾——具体的采样与锁存由下游 Vivado 中断控制器/PS GIC 处理，本讲只指出源码这两处事实，不再展开（属于集成层细节）。

#### 4.3.4 代码实践

**实践目标**：亲手追踪两个输出端口的时钟域归属，体会「是否同步」的取舍。

**操作步骤**：

1. 在 `hdl/data_rec_vivado_wrp.vhd` 中找到第 438 行与第 455 行，确认 `Done_Irq` 中间夹着一个 `pulse_cc` 实例（440–453 行），其 `b_clk_i` 接的是 `s00_axi_aclk`。
2. 再找到第 484 行，确认 `Trig_Out` 是 `i_data_rec` 的端口映射，中间**没有任何 `*_cc` 实例**。
3. 翻到 `hdl/data_rec.vhd` 第 393 行，确认 `Trig_Out <= r.Trigger_2`，`Trigger_2` 是数据域流水线寄存器（`Clk` 驱动）。

**需要观察的现象**：`Done_Irq` 的赋值右侧来自跨时钟域实例的输出；`Trig_Out` 的赋值右侧来自核心记录器的寄存器，二者同步策略不同。

**预期结果（自检）**：

| 端口 | 源信号 | 是否跨时钟域 | 所在时钟域 |
| --- | --- | --- | --- |
| `Done_Irq` | `port_done`（数据域脉冲） | 是，经 `pulse_cc` | AXI 域 `s00_axi_aclk` |
| `Trig_Out` | `r.Trigger_2`（数据域寄存器） | 否，直通 | 数据域 `Clk` |

> 这张表是本讲最重要的结论之一，也是 u5-l2（跨时钟域策略）的引子。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Done_Irq` 必须跨到 AXI 时钟域，而 `Trig_Out` 却留在数据时钟域？

> **答案**：因为接收方不同。`Done_Irq` 的接收方是 CPU（Zynq PS 的中断控制器），它跑在 AXI 时钟上，必须同步过去才能被可靠采样。`Trig_Out` 的典型接收方是同一 FPGA 内、共用 `Clk` 的相邻逻辑（如另一个级联记录器），直接给同一时钟域的信号即可，加同步反而徒增延迟。

**练习 2**：若用户没有在 Vivado GUI 勾选 `TrigForwarding_g`，`Trig_Out` 端口会怎样？

> **答案**：根据 `component.xml` 第 774 行的 `dependency="$TrigForwarding_g = true"`，该端口在最终 IP 中**不存在**（被使能条件裁掉），Block Design 里看不到这根线，也不会占用引脚。VHDL 里第 484 行的映射依然正确，只是该 generic 为 false 时综合后端口不外露。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这张「端口分组与时钟域标注」任务（这是本讲规格指定的代码实践）。

**实践目标**：为 `data_rec_vivado_wrp` 画一张端口分组图，把所有端口按 **数据 / 触发 / AXI / 时钟复位 / 中断** 五组归类，并标注每个端口属于**数据时钟域**还是 **AXI 时钟域**。

**操作步骤**：

1. 通读 `hdl/data_rec_vivado_wrp.vhd` 第 23–104 行的完整 entity。
2. 在纸上（或 Markdown 表格 / 任意画图工具）画一个大框代表 `data_rec_vivado_wrp`，框上分五个区域。
3. 把每个端口拖进对应区域，并在端口名后用 `(D)` 标数据域、`(A)` 标 AXI 域。
4. 判断时钟域的依据：看该端口最终被哪个时钟驱动——可参考封装层内部实例（第 250–306 行的 AXI 解码器用 `s00_axi_aclk`；第 460–509 行的 `data_rec` 用 `Clk`；第 437–455 行的 `pulse_cc` 把 `Done` 从 `Clk` 跨到 `s00_axi_aclk`）。
5. 用 `component.xml` 的总线接口声明（第 8–389 行：`s00_axi`/`s00_axi_aclk`/`s00_axi_aresetn`/`Done_Irq`/`Clk`）交叉验证你的分组。

**需要观察的现象**：`Done_Irq` 虽然语义上是「中断」，但物理上属于 AXI 时钟域；`Trig_Out` 属于数据时钟域；`Trig_In` 与所有 `In_Data`/`In_Vld` 都属于数据时钟域。

**预期结果（参考答案表）**：

| 分组 | 端口 | 时钟域 |
| --- | --- | --- |
| 时钟复位（数据） | `Clk`、`Rst` | 数据域 (D) |
| 时钟复位（AXI） | `s00_axi_aclk`、`s00_axi_aresetn` | AXI 域 (A) |
| 数据 | `In_Data0..7`、`In_Vld` | 数据域 (D) |
| 触发 | `Trig_In`、`Trig_Out` | 数据域 (D) |
| 中断 | `Done_Irq` | AXI 域 (A) |
| AXI 总线 | 全部 `s00_axi_ar*`/`r*`/`aw*`/`w*`/`b*` | AXI 域 (A) |

> 进阶思考（可写入学习笔记）：`Rst` 是数据域的高有效复位；AXI 侧的 `s00_axi_aresetn` 是**低有效**复位，在封装层第 228 行被取反为 `AxiRst`。两个域各自有独立的复位，这也是双时钟域设计的必然结果，细节在 u5-l2 展开。

---

## 6. 本讲小结

- `data_rec_vivado_wrp` 是 IP 的「门面」，端口分**算法侧**（数据/触发/时钟复位）和**总线侧**（`s00_axi_*`）两大块。
- 五个 generic（`NumOfInputs_g`/`InputWidth_g`/`MemoryDepth_g`/`TrigInputs_g`/`TrigForwarding_g`）决定端口数量与宽度，`In_Data0..7` 的使能由 `NumOfInputs_g > i` 控制。
- AXI4 Slave 由**五个独立通道**组成：AR（读地址）、R（读数据）、AW（写地址）、W（写数据）、B（写响应），每通道靠 `VALID/READY` 握手。
- 默认 `C_S00_AXI_ADDR_WIDTH=14`，对应 16 KiB 地址空间，`rdata`/`wdata` 固定 32 位。
- 本 IP 是**双时钟域**设计：数据域 `Clk` 与 AXI 域 `s00_axi_aclk`；`Done_Irq` 经 `pulse_cc` 跨到 AXI 域，`Trig_Out` 直通留在数据域——是否同步取决于**接收方在哪个域**。
- `Trig_Out` 是 v2.4 新增可选端口，由 `TrigForwarding_g = true` 使能；`Done_Irq` 在 `component.xml` 中声明为 `LEVEL_HIGH` 中断接口。

---

## 7. 下一步学习建议

- 想知道这 16 KiB 地址空间里**具体哪个地址是哪个寄存器**？进入 **u2-l2 寄存器与存储地址地图**，它讲解 `data_rec_register_pkg.vhd` 的全部地址常量与 `MemAddr` 函数。
- 想深入封装层**如何把 AXI 访问解码成寄存器读写和存储访问**？进入 **u5-l1 AXI4 Slave 寄存器与存储解码**（`psi_common_axi_slave_ipif` 实例）。
- 想彻底搞懂 **`status_cc` / `pulse_cc` 的跨时钟域策略**？进入 **u5-l2 跨时钟域：status_cc 与 pulse_cc 策略**。
- 建议的阅读顺序：u2-l2（地址地图）→ u3（核心记录器状态机）→ 回头看 u5（封装内部），这样「先用起来、再懂机制」。
