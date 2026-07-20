# DMA 引擎 psi_ms_daq_daq_dma：接口与缓存结构

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `psi_ms_daq_daq_dma` 在整个 IP 核里「承上启下」的位置：上游是谁、下游是谁、它搬运的是什么。
- 对照实体端口，把 DMA 引擎的信号分成「控制 / 命令响应 / 流数据 / 内存」四组，并解释每组的握手关系。
- 指出命令 FIFO、响应 FIFO、缓冲 FIFO（DatFifo）和 Remaining Data RAM 这四个缓存结构分别解决什么问题、为什么缺一不可。
- 解释 Remaining Data RAM 存在的根本原因（一次突发可能在一个内部数据字中间结束），以及 `FirstDma` 寄存器在复位后每个流首次传输时的作用。

本讲只看「结构与接口」，刻意不展开 DMA 内部状态机的字节移位与计数细节——那是下一讲 u2-l6 的主题。本讲把 DMA 当作一个「黑盒加四个缓存」来理解。

## 2. 前置知识

在进入 DMA 引擎之前，先用通俗语言澄清几个本讲反复用到的概念。

**DMA（Direct Memory Access，直接内存访问）**
CPU 把「把这段数据搬到内存的某个地址」这件事委托给一个专用硬件模块去做，自己不用逐字节搬。在 psi_multi_stream_daq 里，DMA 引擎就是把多路采集到的样本数据搬运到 DDR 内存的那块硬件。

**FIFO（First-In First-Out，先进先出队列）**
一种「水管」式的缓存：数据从一端写进、从另一端按相同顺序读出。FIFO 用来把「生产者」和「消费者」从时间上解耦——生产者偶尔快、消费者偶尔慢都没关系，FIFO 吸收这个波动。本讲遇到的是**同步 FIFO**（读写共用同一个时钟，`psi_common_sync_fifo`）。

**almost-full（将近满）反压**
FIFO 内部有一个「快满了」的预警标志。当缓冲 FIFO 快满时，DMA 就**暂停**继续从输入端取数据，等内存侧消费掉一些再继续。这种「用快满信号回头踩刹车」的机制叫反压（backpressure）。

**字（word）与字节（byte）**
本讲里「字」特指一个 `IntDataWidth_g` 位宽的内存传输单位（默认 64 位 = 8 字节）；「字节」是 8 位。内存接口 `Mem_DatData` 一次送出一个完整的字。

**SDP RAM（Simple Dual-Port RAM，简单双口 RAM）**
一块只有一个写端口、一个读端口的存储器。本讲的 Remaining Data RAM 用 `psi_common_sdp_ram` 实现。

**承上启下的两条链路（来自 u1-l3、u1-l4）**
回顾顶层已经建立的全局图：数据走「输入逻辑 → DMA → AXI Master → 内存」，控制走「AXI Slave → 寄存器 → 控制状态机」。本讲的 DMA 引擎正好夹在中间：它从**控制状态机**（`psi_ms_daq_daq_sm`）接收命令、从**输入逻辑**（`psi_ms_daq_input`）接收样本数据，把数据拼成内存字后交给 **AXI 主接口**（`psi_ms_daq_axi_if`）写入 DDR，再把「这次搬了多少、有没有遇到触发」作为响应回送给控制状态机。模块间通信用到的记录类型 `DaqSm2DaqDma_Cmd_t`、`DaqDma2DaqSm_Resp_t`、`Input2Daq_Data_t` 已在 u2-l1 的公共包里定义，本讲直接使用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_daq_dma.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd) | 本讲主角：DMA 引擎实体与架构。包含实体端口、四个缓存结构（CmdFifo/RspFifo/DatFifo/Remaining Data RAM）的例化，以及 `FirstDma` 寄存器。 |
| [hdl/psi_ms_daq_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd) | 公共类型包（u2-l1 已讲）。本讲引用其中的命令/响应记录类型与 `ToStdlv`/`FromStdlv` 转换函数，以及尺寸常量。 |
| [hdl/psi_ms_daq_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd) | 顶层。本讲引用其中例化 DMA 引擎的 `i_dma` 片段，说明它如何与状态机、输入、AXI 主接口连线。 |
| [tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd) | DMA 引擎的 testbench「非对齐」用例，是本讲代码实践的观察对象。 |
| [tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd) | testbench 辅助过程包，定义 `ApplyCmd`/`ApplyData`/`CheckResp`/`CheckMemData` 等激励与校验过程。 |

## 4. 核心概念与源码讲解

### 4.1 DMA 引擎的接口与「承上启下」角色

#### 4.1.1 概念说明

DMA 引擎是整个 IP 核数据通路上的「搬运工」。它要同时面对三方：

- **控制状态机（上游控制方）**：告诉它「把流 N 的数据搬到内存地址 A，最多搬 MaxSize 字节」。
- **输入逻辑（上游数据方）**：把流 N 采集到的样本数据按节拍送过来。
- **AXI 主接口（下游消费方）**：接收拼好的内存字，写进 DDR，并回报「写完了」。

搬运结束后，DMA 还要把结果（搬了多少字节、这一段里有没有遇到触发）作为**响应**回送给控制状态机，状态机据此推进窗口、生成中断。所以 DMA 是「命令进、数据进 → 内存出、响应出」的十字路口。

#### 4.1.2 核心流程

一次完整的搬运可以用下面这条主线串起来（状态机的细节留待 u2-l6，这里只看数据走向）：

1. 控制状态机发来一条命令（`DaqSm_Cmd` + `DaqSm_Cmd_Vld`），含 `Address`、`MaxSize`、`Stream`。
2. DMA 锁定目标流 `Stream`，开始从该流的输入端（`Inp_Data[Stream]`）按节拍取数据。
3. 取到的字节被拼装成完整的内部字，送给内存接口（`Mem_DatData` + `Mem_DatVld`），由 AXI 主接口写进 DDR。
4. 传输因为「达到 MaxSize」「遇到触发/超时/帧末」而结束。
5. DMA 发出内存写命令（`Mem_CmdAddr/Size/Vld`），并把响应（`DaqSm_Resp`：实际字节数 `Size`、是否触发 `Trigger`、流号 `Stream`）回送给状态机。

端口可以按职责分成清晰的四组：**控制**（时钟复位）、**状态机连接**（命令/响应/HasLast）、**输入连接**（多路流数据）、**内存连接**（写命令 + 写数据）。

#### 4.1.3 源码精读

实体只有两个 generic，都是顶层透传下来的常量（[psi_ms_daq_daq_dma.vhd:32-35](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L32-L35)）：

- `Streams_g`：流数（1~32，默认 4）。它决定了下面所有缓存的深度。
- `IntDataWidth_g`：内部数据宽度（默认 64）。它就是「一个字多少位」，同时决定 `Mem_DatData` 的位宽与 `Bytes` 字段的位宽。

端口按四组列出（[psi_ms_daq_daq_dma.vhd:36-62](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L36-L62)）：

**① 控制**（[L37-L39](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L37-L39)）：单一时钟 `Clk`（注释里标 200 MHz，驱动 control/input/mem_cmd/mem_dat 四个进程域，但都在同一个时钟下）与高有效复位 `Rst`。

**② 状态机连接**（[L41-L47](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L41-L47)）：

- `DaqSm_Cmd : in DaqSm2DaqDma_Cmd_t` —— 命令记录（`Address`/`MaxSize`/`Stream`，定义见 [psi_ms_daq_pkg.vhd:46-50](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L46-L50)）。
- `DaqSm_Cmd_Vld` / 配合握手。
- `DaqSm_Resp : out DaqDma2DaqSm_Resp_t` —— 响应记录（`Size`/`Trigger`/`Stream`，定义见 [psi_ms_daq_pkg.vhd:55-59](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L55-L59)）。
- `DaqSm_Resp_Vld` / `DaqSm_Resp_Rdy`。
- `DaqSm_HasLast : out std_logic_vector(Streams_g-1 downto 0)` —— 每流一位，表示该流是否已经搬过一个「带末帧标志」的字。这是反馈给状态机用于窗口判断的旁路信号。

**③ 输入连接**（[L49-L52](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L49-L52)）：`Inp_Vld`/`Inp_Rdy` 是每路一位的握手向量；`Inp_Data` 是 `Input2Daq_Data_a` 数组，每路一个记录，含 `Data`（`IntDataWidth_g` 位）、`Bytes`（本次有效字节数）、`Last`/`IsTo`/`IsTrig` 标志（记录定义见 [psi_ms_daq_pkg.vhd:37-43](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L37-L43)）。注意 `Bytes` 的位宽是 `log2ceil(IntDataWidth_g/8)`，即默认 3 位，可表示 0~7 字节——这是 u2-l1 里强调的「`Input2Daq_Data_t` 的 `Data`/`Bytes` 宽度随 `IntDataWidth_g` 动态确定」在实体端口上的直接体现。

**④ 内存连接**（[L54-L61](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L54-L61)）：写命令一组（`Mem_CmdAddr`/`Mem_CmdSize` 各 32 位、`Mem_CmdVld`/`Mem_CmdRdy`），写数据一组（`Mem_DatData` 宽 `IntDataWidth_g`、`Mem_DatVld`/`Mem_DatRdy`）。这里只有写通道、没有读通道——DMA 只往内存写，永不读。

最后看一眼顶层是怎么把这块「十字路口」接进系统的（[psi_ms_daq_axi.vhd:395-418](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L395-L418)）：`DaqSm_Cmd*` 接到状态机送来的 `SmDma_Cmd*`；`Inp_Data` 接到各路输入逻辑；`Mem_Cmd*`/`Mem_Dat*` 接到信号 `DmaMem_*`，而这些 `DmaMem_*` 又在 [psi_ms_daq_axi.vhd:438-444](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L438-L444) 连到 AXI 主接口 `i_memif`。这就是「承上启下」的实物连线。

#### 4.1.4 代码实践：端口到顶层连线的对照阅读

1. **实践目标**：把 DMA 实体的四组端口与顶层 `i_dma` 例化、上下游模块逐一对应，建立「谁连谁」的清晰地图。
2. **操作步骤**：
   - 打开 [hdl/psi_ms_daq_daq_dma.vhd:36-62](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L36-L62)，抄下四组端口名。
   - 打开 [hdl/psi_ms_daq_axi.vhd:395-418](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L395-L418)，为每个端口找到它连接的顶层信号。
   - 再用 `Grep` 在顶层搜索 `SmDma_Cmd`、`DmaMem_CmdAddr`、`DmaSm_HasLast` 各自的另外一端连到了哪个模块（状态机 `i_statemachine`、AXI 主接口 `i_memif`）。
3. **需要观察的现象**：`DaqSm_Cmd*` 的另一端是状态机；`Mem_*` 的另一端是 AXI 主接口；`Inp_Data` 来自 `g_input`。
4. **预期结果**：得到一张「命令来自状态机、数据来自输入、写命令与写数据发往 AXI 主接口、响应与 HasLast 回送状态机」的对应表。
5. 本实践为源码阅读型，无需运行仿真；连线结论可对照上文「承上启下」段落自检。

#### 4.1.5 小练习与答案

**练习 1**：DMA 引擎的 `Mem_DatData` 端口位宽是 32 位还是 64 位？由谁决定？
**答案**：是 `IntDataWidth_g` 位（默认 64）。位宽直接写在端口声明 [L59](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L59)，由 generic `IntDataWidth_g` 决定。

**练习 2**：为什么 DMA 引擎只有内存写通道（`Mem_Cmd*`/`Mem_Dat*`），没有读通道？
**答案**：因为 DMA 的职责是把采集数据**搬进** DDR，永远只写不读；读内存这件事不属于它（参见端口组 [L54-L61](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L54-L61) 无任何 `Mem_*Rd*` 信号）。

---

### 4.2 命令 FIFO 与响应 FIFO（CmdFifo / RspFifo）

#### 4.2.1 概念说明

控制状态机和 DMA 引擎虽然跑在同一个时钟下，但两者的「节奏」并不一致：状态机算好一次 DMA 的地址和长度后，希望立刻把命令甩出去继续仲裁别的流；而 DMA 要等当前传输结束才能接下一条。如果让两者**直连**，状态机就必须等 DMA 空闲，吞吐和设计复杂度都会变差。

解决办法是在命令通路上插一个**命令 FIFO（CmdFifo）**：状态机随时把命令推进去，DMA 有空就取一条。同理，DMA 产出的响应也先进一个**响应 FIFO（RspFifo）**，状态机有空再来取。两个 FIFO 把「命令下发」和「命令执行」、「执行完成」和「响应处理」从时间上彻底解耦。

FIFO 只认比特向量（`std_logic_vector`），不认 VHDL 记录。所以命令/响应记录在进 FIFO 前要用 u2-l1 讲过的 `ToStdlv` 函数打成比特，出 FIFO 后再用 `FromStdlv` 还原成记录。

#### 4.2.2 核心流程

- **命令方向**：`DaqSm_Cmd`（记录）→ `DaqSm2DaqDma_Cmd_ToStdlv` → `CmdFifo_InData`（向量）→ CmdFifo → `CmdFifo_OutData`（向量）→ `DaqSm2DaqDma_Cmd_FromStdlv` → `CmdFifo_Cmd`（记录，供状态机消费）。
- **响应方向**：状态机算出的 `r.RspFifo_Data`（记录）→ `DaqDma2DaqSm_Resp_ToStdlv` → `RspFifo_InData` → RspFifo → `RspFifo_OutData` → `DaqDme2DaqSm_Resp_FromStdlv` → `DaqSm_Resp`。（注意还原函数名是 `DaqDme2DaqSm_Resp_FromStdlv`，`Dme` 是源码里既有的拼写，u2-l1 已提醒过。）

两个 FIFO 的深度都取 `2**StreamBits_c`，其中 `StreamBits_c := max(log2ceil(Streams_g), 1)`（[L75](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L75)）。选这个深度的依据是系统级的「每流最多一条在途命令」约定（状态机用 `OpenCommand` 机制保证，详见 u4-l5）：在途命令数 ≤ 流数，因此 FIFO 能装下所有在途命令而不会溢出。

#### 4.2.3 源码精读

命令 FIFO 例化见 [psi_ms_daq_daq_dma.vhd:304-322](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L304-L322)。关键点：

- `width_g => DaqSm2DaqDma_Cmd_Size_c`，这个尺寸常量在包里定义为 `32 + 16 + MaxStreamsBits_c`，即 32 位地址 + 16 位 MaxSize + 5 位流号 = **53 位**（[psi_ms_daq_pkg.vhd:51](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L51)）。
- `depth_g => 2**StreamBits_c`，默认 4 流时为 4。
- `ram_style_g => "distributed"`：用分布式 RAM（查找表）实现，因为深度很浅。
- 写握手 `vld_i => DaqSm_Cmd_Vld`、`rdy_i => r.CmdFifo_Rdy`（由状态机在 `Idle_s` 取命令时拉高）。

响应 FIFO 例化见 [L324-L344](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L324-L344)，结构与命令 FIFO 对称，宽度为 `DaqDma2DaqSm_Resp_Size_c = 16 + 1 + MaxStreamsBits_c = 22 位`（[psi_ms_daq_pkg.vhd:60](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L60)）。注意 [L325-L326](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L325-L326) 有一句重要注释：

> `-- Ready not required for system reasons: There is never more commands open than streams.`

含义是：因为「每流最多一条在途命令」，响应数量永远 ≤ 流数 ≤ FIFO 深度，所以**理论上不会溢出**，反压并非正确性所必需；代码里仍然把 `rdy_i => DaqSm_Resp_Rdy` 接上，让状态机有序消费。

#### 4.2.4 代码实践：跟一条命令穿过 CmdFifo、再等响应从 RspFifo 回来

1. **实践目标**：用 testbench 的「非对齐」用例，观察「命令进 → 响应出」的一对一关系，体会两个 FIFO 的解耦作用。
2. **操作步骤**：
   - 打开 [psi_ms_daq_daq_dma_tb_case_unaligned.vhd:86-96](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L86-L96)（「End Unaligned」子用例）。
   - 读到三条 `ApplyCmd(2, Addr, MaxSize, ...)` 紧跟三条 `CheckResp(2, Size, NoEnd_s, ...)`。`ApplyCmd` 把命令驱动到 `DaqSm_Cmd`（即 CmdFifo 写口），`CheckResp` 在 `DaqSm_Resp`（即 RspFifo 读口）上等响应并校验 `Size`/`Trigger`/`Stream`。
   - 打开 [psi_ms_daq_daq_dma_tb_pkg.vhd:107-142](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd#L107-L142)，确认 `ApplyCmd` 只维持一拍 `DaqSm_Cmd_Vld`，而 `CheckResp` 会一直等到 `DaqSm_Resp_Vld='1'`。
3. **需要观察的现象**：命令是一次性快速「甩」进去的（一拍有效），响应则是 DMA 把数据搬完后才「慢慢」出来——两者被 CmdFifo/RspFifo 隔开。
4. **预期结果**：三条命令、三条响应，`Size` 分别为 30、29、30（`NoEnd_s` 表示响应里 `Trigger=0`）。
5. 运行方式：参照 u1-l2，在 Modelsim 里 `source sim/run.tcl` 跑回归；若只想看这一条用例，可关注日志里 `>> -- Unaligned --` 之后、`>> End Unaligned` 子用例的输出。**待本地验证**具体波形。

#### 4.2.5 小练习与答案

**练习 1**：默认 `Streams_g=4` 时，CmdFifo 的深度是多少？为什么够用？
**答案**：`2**StreamBits_c = 2**2 = 4`。够用是因为系统约定每流最多一条在途命令（≤ 4 条），FIFO 不会溢出（见 [L308](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L308) 与注释 [L325](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L325)）。

**练习 2**：`DaqSm2DaqDma_Cmd_Size_c` 等于多少位？由哪几部分组成？
**答案**：53 位 = 32（Address）+ 16（MaxSize）+ `MaxStreamsBits_c`（5 位 Stream），见 [psi_ms_daq_pkg.vhd:51](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L51)。

---

### 4.3 缓冲 FIFO 与 alm_full 反压（DatFifo）

#### 4.3.1 概念说明

数据从「移位拼字」到「送上内存接口」之间还隔着一个**缓冲 FIFO（DatFifo）**。它的存在是为了应对下游 AXI 主接口偶尔「没准备好收更多数据」的情况。

如果没有这个缓冲，那么只要内存侧一踩刹车（`Mem_DatRdy='0'`），DMA 内部整条拼字流水线就得立刻同步停下来，反压逻辑要贯穿整个模块——既复杂又容易出时序问题。加一个不深的缓冲 FIFO 后，DMA 可以继续拼几个字塞进 FIFO，等内存侧恢复后再迅速排空；只有当 FIFO **快满**时才回头让拼字逻辑暂停。这样反压只需要在「拼字 → FIFO」这一处用 `alm_full` 信号实现，整条流水线其余部分无需关心反压。

#### 4.3.2 核心流程

- **写入**：DMA 在拼出一个完整字后置 `r.Mem_DataVld='1'`，把 `r.DataSft` 的低半字（`IntDataWidth_g` 位）写进 DatFifo（`dat_i`）。
- **读出**：DatFifo 直接输出到 `Mem_DatData`/`Mem_DatVld`，由 AXI 主接口用 `Mem_DatRdy` 消费。
- **反压**：DatFifo 的 `alm_full_o` 引到内部信号 `DatFifo_AlmFull`；状态机在传输态检查它，为 1 就停止接收新数据。

阈值设为深度的一半（16），既给下游留了恢复时间，又给上游留了继续拼字的余量。

#### 4.3.3 源码精读

缓冲 FIFO 例化见 [psi_ms_daq_daq_dma.vhd:350-369](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L350-L369)。关键 generic：

- `width_g => IntDataWidth_g`（默认 64），即一个字宽。
- `depth_g => BufferFifoDepth_c`，常量定义在 [L71](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L71) 为 32。
- `alm_full_on_g => true`、`alm_full_level_g => BufferFifoDepth_c / 2 = 16`：开启「快满」标志，阈值 16。
- `dat_i => r.DataSft(IntDataWidth_g-1 downto 0)`、`vld_i => r.Mem_DataVld`：写入拼好的低半字。
- `dat_o => Mem_DatData`、`vld_o => Mem_DatVld`、`rdy_i => Mem_DatRdy`：直连内存数据接口。
- `alm_full_o => DatFifo_AlmFull`：反压信号。

[L347-L349](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L347-L349) 的注释把设计意图说得很清楚：缓冲 FIFO 让 DMA 在「内存接口暂时不收」的几拍里继续吸收数据，从而不必把反压处理铺满整条流水线；rdy 不必连，因为整条数据流水线是依据 almost-full 标志停下的（这里 `rdy_i` 接的是下游真正的 `Mem_DatRdy`）。

反压在状态机里的落点见 [L196](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L196)：传输态的取数分支以 `elsif DatFifo_AlmFull = '0' then` 作前提——快满时整个分支跳过，DMA 这一拍不拼新字也不取输入，等于踩刹车。

#### 4.3.4 代码实践：观察 alm_full 反压的效果

1. **实践目标**：用一个「内存侧故意慢」的用例，体会缓冲 FIFO 与 alm_full 如何吸收下游抖动。
2. **操作步骤**：
   - 打开 [psi_ms_daq_daq_dma_tb_case_unaligned.vhd:338-343](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L338-L343)（「QWord Split, Rdy Toggling」子用例的 `mem_dat` 进程）。
   - 注意 `CheckMemData(..., RdyDelay => 5, ...)`：第二个参数 5 表示每收一个字前先「不 ready」若干拍。这正是模拟 AXI 侧临时不收数据。
   - 对照 [psi_ms_daq_daq_dma_tb_pkg.vhd:204-233](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_pkg.vhd#L204-L233)，看到 `CheckMemData` 会先把 `Mem_DatRdy` 拉低、等若干拍再拉高收一个字。
3. **需要观察的现象**：尽管下游周期性「不收」，数据校验仍然正确（字节序、偏移都对），说明 DMA 没丢数据——多余的几个字被缓冲 FIFO 暂存了。
4. **预期结果**：用例通过（无 `###ERROR###`），三条内存数据校验全部命中。
5. 若要更直观地看到 `DatFifo_AlmFull` 拉高，可在仿真波形里加 `i_fifodata` 的 `alm_full_o` 与 `DatFifo_Level_Dbg` 信号观察。**待本地验证**波形细节。

#### 4.3.5 小练习与答案

**练习 1**：DatFifo 的 `alm_full_level_g` 是多少？设成「深度的一半」有什么好处？
**答案**：16（`BufferFifoDepth_c/2`，[L355](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L355)）。一半既给下游恢复留了 16 个字的空间，又给上游留了 16 个字的余量继续拼字，是安全的折中。

**练习 2**：DatFifo 的 `rdy_i` 接到哪个端口？为什么反压还要额外用 `alm_full_o`？
**答案**：`rdy_i` 接 `Mem_DatRdy`（[L366](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L366)）。`Mem_DatRdy` 只能停在 FIFO 出口；为了让 FIFO **不溢出**，必须提前用 `alm_full_o` 回头让拼字逻辑暂停（[L196](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L196)）。

---

### 4.4 Remaining Data RAM（psi_common_sdp_ram）

#### 4.4.1 概念说明

这是本讲最关键、也最容易让人困惑的一个缓存。它的存在回答了一个问题：**一次传输可能在一个内部数据字的中间结束，那没拼满的半个字怎么办？**

具体来说：

- 内存接口 `Mem_DatData` 是按**整字**（`IntDataWidth_g` 位 = 8 字节）送出的。
- 但一次 DMA 传输会因为「达到 MaxSize」「遇到触发 / 超时 / 帧末」而结束，结束时刻未必恰好是整字边界。比如某次传输结束时，当前字只攒了 5 个有效字节，剩下 3 个字节位置还空着。
- 这 5 个字节属于该流的数据，但它们还不足以构成一个完整字，不能就这样送出去；丢掉又会破坏数据连续性。

解决办法是给**每个流**预留一个「残余」存储槽，把这次没拼满的半个字连同它的字节位置、以及还没上报的 `Trigger`/`Last` 标志一起**存起来**；等到这个流的**下一次**传输来时，先把残余读回来、预填进移位寄存器，让新数据接着把这个词拼完。这样跨传输的字节边界就被正确衔接了。

这个「每流一格」的存储就是 **Remaining Data RAM**，用 `psi_common_sdp_ram`（简单双口 RAM）实现，按流号寻址——所以它的深度等于流数（`2**StreamBits_c`），每个流独占一格。

#### 4.4.2 核心流程

残余的写读时序围绕状态机的 `Done_s`（写）和 `RemRd1_s`/`RemRd2_s`（读）两段（状态机的完整分析留待 u2-l6）：

- **写（本次传输结束时）**：在 `Done_s`，DMA 计算残余字节位置 `RemWrBytes`、残余数据 `RemData`、待挂起的 `RemWrTrigger`/`RemWrLast`，拼成一根向量 `Rem_Data_Fifo_In`，用 `r.StreamStdlv`（当前流号）作地址、`r.RemWen='1'` 写入 RAM。
- **读（下次传输开始时）**：在 `RemRd1_s`/`RemRd2_s`，DMA 用同一个流号 `r.StreamStdlv` 作地址读 RAM，把 `Rem_RdBytes`/`Rem_Data`/`Rem_Trigger`/`Rem_Last` 还原，预填到 `DataSft` 的高半字与字节偏移 `HndlSft`，使新数据从正确位置继续拼。

**`FirstDma` 的作用**：刚复位时，Remaining Data RAM 里是未定义的垃圾值。如果第一次传输照常去读 RAM，垃圾就会污染第一个字。为此每个流有一个 `FirstDma` 位：复位时全部置 1；当某流的**首次**传输走到 `RemRd2_s` 时，因为 `FirstDma(stream)='1'`，状态机**跳过读 RAM**，直接把偏移、移位寄存器、挂起标志全部清零，得到一个干净起点；随后把该流的 `FirstDma` 清 0，之后的传输就正常读 RAM 里的真实残余了。

> 关于本讲实践任务里「每次使能后首次传输」的说法需要澄清范围：**在本模块内**，`FirstDma` 只在复位（`Rst='1'`）时被重新置全 1（见下文 [L293](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L293)），并不是每次「软件使能某流」都重置。软件层面的「禁用 → 重新使能」会重置写指针与窗口上下文，那是**控制状态机**用 `FirstAfterEna` 处理的（u4-l5 详述）；在 DMA 引擎看来，重新使能后该流只是从已有的残余状态继续，并不会再次跳过 RAM。所以严格地说，本模块的 `FirstDma` 保护的是「复位后每个流的第一次传输」。

#### 4.4.3 源码精读

残余相关信号在架构声明区已就位（[psi_ms_daq_daq_dma.vhd:90-95](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L90-L95)）：`Rem_RdBytes`/`Rem_Data`/`Rem_Trigger`/`Rem_Last` 是读回的还原值，`Rem_Data_Fifo_In/Out` 是打包前后的整根向量，宽度为 `BytesWidth_c + IntDataWidth_g + 1` 再加 1 位（即字节位置 + 数据 + Trigger + Last）。

**写入打包**（[L372-L375](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L372-L375)）：把 `RemWrLast`（最高位）、`RemWrTrigger`、`RemWrBytes`、`RemData` 从高到低拼进一根向量。

**RAM 例化**（[L377-L393](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L377-L393)）：

- `depth_g => 2**StreamBits_c`（默认 4 流 → 4 格，每流一格）。
- `width_g => 1 + 1 + BytesWidth_c + IntDataWidth_g`（默认 `1+1+3+64 = 69` 位）。
- `is_async_g => false`：同步模式（单时钟）。此时读时钟 `rd_clk_i` 不被使用，代码把它接到 `Rst` 仅作占位（这是同步 SDP RAM 在 `is_async_g=false` 下的既有用法）。
- `wr_addr_i => r.StreamStdlv` 与 `rd_addr_i => r.StreamStdlv`：**写地址和读地址都是当前流号**，所以每流独占一格、跨传输保留。

**读出拆包**（[L395-L398](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L395-L398)）：把向量还原成 `Rem_Last`/`Rem_Trigger`/`Rem_RdBytes`/`Rem_Data`。

**`FirstDma` 在记录里的声明**（[L122](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L122)）：`FirstDma : std_logic_vector(Streams_g - 1 downto 0)`，每流一位。

**复位置全 1**（[L293](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L293)）：`r.FirstDma <= (others => '1');`——复位后每个流都标记「还没搬过第一次」。

**读回时 `FirstDma` 的分支**（[L171-L190](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L171-L190)，`RemRd2_s` 段）：

```vhdl
if r.FirstDma(r.HndlStream) = '1' then
  v.HndlSft    := (others => '0');   -- 忽略 RAM，偏移清零
  v.RdBytes    := (others => '0');
  v.DataSft    := (others => '0');   -- 移位寄存器清零
  v.RemTrigger := '0';
  v.RemLast    := '0';
else
  v.HndlSft    := unsigned(Rem_RdBytes);                 -- 用真实残余偏移
  v.DataSft(2*IntDataWidth_g-1 downto IntDataWidth_g) := Rem_Data;  -- 预填高半字
  v.RdBytes    := resize(unsigned(Rem_RdBytes), v.RdBytes'length);
  v.RemTrigger := Rem_Trigger;
  v.RemLast    := Rem_Last;
end if;
v.FirstDma(r.HndlStream) := '0';   -- 用过即清，下次正常读 RAM
```

这就是 `FirstDma` 的全部逻辑：首次传输跳过 RAM、清零起步，之后正常衔接残余。

**残余的写入发生在 `Done_s`**（[L230-L249](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L230-L249)）：计算 `RemWrBytes`/`RemData`/`RemWrTrigger`/`RemWrLast`，并在结尾 `v.RemWen := '1'`（[L249](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L249)）触发一次 RAM 写。`RemWrBytes` 是否为 0 还会在 `Cmd_s` 决定响应的 `Trigger` 是否上报（[L258-L262](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L258-L262)）：只有当残余字节数为 0、即触发所在的字已经完整写进内存时，`RspFifo_Data.Trigger` 才置 1——这是「触发不能在半个字里上报」的体现，字节计数细节留给 u2-l6。

#### 4.4.4 代码实践：解释 Remaining Data RAM 的必要性与 FirstDma 的作用（本讲主实践）

1. **实践目标**：用自己的话讲清两件事——(a) 为什么 DMA 必须保留跨传输的残余；(b) `FirstDma` 在复位后首次传输中起什么作用。并用 testbench 用例佐证。
2. **操作步骤**：
   - **阅读「残余」用例**：打开 [psi_ms_daq_daq_dma_tb_case_unaligned.vhd:152-174](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L152-L174)。这里有两个对照子用例：「Unaligned end by trigger (with rem-word)」与「(without rem-word)」。两条命令的 `MaxSize` 都是 64（不构成限制），但触发落在字内不同位置，导致是否产生需要保留的残余字。注意两次 `CheckResp` 期望的 `Size` 不是 8 的倍数（如 29、25），说明传输确实在字的中间结束。
   - **理解连续性**：对照 `input` 进程里 `ApplyData(2, 30+29, Trigger_s, ..., Offset=>0)` 与随后的 `ApplyData(2, 30, NoEnd_s, ..., Offset=>30+29)`（[L230-L232](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L230-L232)），可以看到字节流在两条命令之间是**连续递增**的（偏移从 0 接到 30+29 再接到下一块）。如果 DMA 不保留残余，下一条命令开头的字节就会拼错位置。
   - **定位 `FirstDma`**：在 [hdl/psi_ms_daq_daq_dma.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd) 中用 `Grep` 搜 `FirstDma`，确认它只在三处出现：声明 [L122](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L122)、`RemRd2_s` 的判断与清除 [L174-L187](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L174-L187)、复位置 1 [L293](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L293)。这印证了上文「`FirstDma` 只在复位时重置」的结论。
3. **需要观察的现象**：
   - 即使命令的 `Size` 是非整字节数（如 29），下一条命令的数据依然能正确接续——`mem_dat` 进程的 `CheckMemData` 全部字节序命中（[L362-L374](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L362-L374)），说明残余被正确保留与衔接。
   - 复位后立即跑的用例（任何子用例都是复位后首次传输）能正确产出数据，说明 `FirstDma` 成功屏蔽了 RAM 里的垃圾初值。
4. **预期结果**：你能写出两段解释——
   - 「因为内存按整字传输、而传输可能在字中间结束，残余字节必须按流保存并在下次传输预填回去，否则跨命令的字节边界会错位；这就是 Remaining Data RAM 存在的原因。」
   - 「`FirstDma` 在复位后为全 1，使每个流的首次传输忽略 RAM 的未定义初值、以零偏移干净起步；首次传输后清 0，之后各次传输才读真实残余。」
5. 字节级精确计数（`RemWrBytes` 如何由 `RdBytes`/`HndlMaxSize` 推出）属于 u2-l6 的状态机内容，本讲不展开，标为「待 u2-l6 验证」。

#### 4.4.5 小练习与答案

**练习 1**：Remaining Data RAM 的深度为什么取 `2**StreamBits_c`，而不是取 1 或取一个固定大数？
**答案**：因为它是「按流号寻址、每流一格」的存储，深度只需等于（或略大于）流数即可让每个流独占一格（[L379](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L379)）；`StreamBits_c=max(log2ceil(Streams_g),1)` 保证即使单流也有 ≥2 格（[L75](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L75)），代价是少量查找表，换来代码不必为单流特判。

**练习 2**：如果去掉 `FirstDma` 机制（复位后直接读 RAM），首次传输会出现什么问题？
**答案**：复位后 RAM 内容未定义，首次传输会把垃圾 `Rem_Data`/`Rem_RdBytes` 当成真实残余预填进 `DataSft`，导致该流第一个内存字的内容与字节位置错误。`FirstDma` 用全 1 初值强制首次传输清零起步（[L174-L187](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L174-L187)）来规避这一点。

**练习 3**：为什么 `Cmd_s` 里只有当 `RemWrBytes=0` 且 `Trigger='1'` 时，响应才上报 `Trigger=1`（[L258-L262](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L258-L262)）？
**答案**：触发所在的字必须**完整写进内存**才算真正落地；如果该字还有残余字节没写完（`RemWrBytes≠0`），说明触发只发生在半个字里，必须等下一次传输把字拼完后再上报，避免状态机提前以为这一段已完整结束。

---

## 5. 综合实践：把四个缓存串成一次完整搬运

**任务**：选取 testbench 的「End Unaligned」子用例（[psi_ms_daq_daq_dma_tb_case_unaligned.vhd:86-96](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_daq_dma/psi_ms_daq_daq_dma_tb_case_unaligned.vhd#L86-L96)），画一张时序-结构图，标注一条命令从进入 DMA 到响应返回的完整路径上，**四个缓存结构分别在什么时刻发挥作用**。

**建议步骤**：

1. 在图上画出五个角色：控制状态机（TB 的 `control` 进程模拟）、CmdFifo、DMA 状态机+DatFifo、Remaining Data RAM、AXI 主接口/TB 的 `mem_cmd`/`mem_dat` 进程、RspFifo。
2. 用箭头标注一条 `ApplyCmd` 命令：它先被 `DaqSm_Cmd_Vld` 推进 **CmdFifo**；DMA 在 `Idle_s` 取出后进入 `RemRd2_s`，从 **Remaining Data RAM** 读残余（首条命令则被 `FirstDma` 屏蔽）；然后在 `Transfer_s` 把拼好的字写进 **DatFifo**，由 `mem_dat` 进程消费；传输结束在 `Done_s` 把新残余写回 **Remaining Data RAM**；最后在 `Cmd_s` 把响应推进 **RspFifo**，被 `CheckResp` 读出。
3. 在图上另标一处「下游不 ready」的假想抖动，说明 **DatFifo 的 alm_full** 在何处回头踩刹车。
4. 写一段总结：四个缓存两两配对——CmdFifo/RspFifo 负责「与状态机解耦命令/响应时序」，DatFifo 负责「吸收内存侧反压」，Remaining Data RAM 负责「衔接跨传输的字边界」。任何一块去掉，DMA 都无法正确工作。

**自检**：如果你的图能让一个没读过源码的同事回答出「命令在哪排队、数据在哪缓冲、残余在哪存、首次传输靠谁保护」这四个问题，就算过关。本实践为源码阅读+画图型，无需运行仿真；若要验证，可在 Modelsim 中跑 `sim/run.tcl` 并在 `>> End Unaligned` 处观察波形。

## 6. 本讲小结

- `psi_ms_daq_daq_dma` 是数据通路上的「搬运工」，端口分四组：控制、状态机连接（命令/响应/HasLast）、输入连接（多路流数据）、内存连接（只写不读）。
- **CmdFifo** 与 **RspFifo** 是两个同步 FIFO，用 `ToStdlv`/`FromStdlv` 把命令/响应记录序列化，把状态机的命令下发、响应处理与 DMA 的执行节奏解耦；深度取 `2**StreamBits_c`，依据是「每流最多一条在途命令」。
- **DatFifo**（缓冲 FIFO）位于拼字输出与内存接口之间，用 `alm_full`（阈值 16）实现反压，避免把反压逻辑铺满整条流水线。
- **Remaining Data RAM** 用 `psi_common_sdp_ram`、按流号寻址，保存一次传输结束时未拼满的残余字节、位置与挂起的 Trigger/Last，使跨传输的字边界正确衔接——这是它的根本存在理由。
- **`FirstDma`** 寄存器在复位时置全 1，使每个流的首次传输跳过 RAM 的未定义初值、以零偏移干净起步，之后清 0；它**只在本模块复位时**重置，软件级「重新使能」由状态机的 `FirstAfterEna` 处理（u4-l5）。
- 本讲只看结构与接口；状态机内部的字节移位、`RdBytes`/`WrBytes`/`RemWrBytes` 计数与触发上报细节是下一讲 u2-l6 的主题。

## 7. 下一步学习建议

- **下一讲 u2-l6《DMA 引擎状态机：传输流程与字节对齐》**：深入 `Idle_s/RemRd1_s/RemRd2_s/Transfer_s/Done_s/Cmd_s` 状态机，看 `DataSft` 如何按字节拼字、`RdBytes`/`WrBytes`/`HndlSft` 如何受 `MaxSize` 约束、以及末帧与 `Trigger` 何时上报——把本讲留下的字节级问题补齐。
- 之后 **u2-l7《AXI 主接口 psi_ms_daq_axi_if》**：看 DMA 产出的 `Mem_Cmd*`/`Mem_Dat*` 如何被 `psi_common_axi_master_full` 包装成 AXI 写事务写入 DDR，并产生 `Done` 信号。
- 建议同步阅读：`tb/psi_ms_daq_daq_dma/` 下的其余 case 文件（`aligned`、`cmd_full`、`data_full`、`errors` 等），它们从不同角度 exercised 了本讲讲的四个缓存结构，是理解「为什么这么设计」的最佳佐证。
