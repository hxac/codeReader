# TLP 处理总览与 128 位流

## 1. 本讲目标

本讲是 PCIe 核心内部的「TLP（事务层包）调度总枢纽」总览篇。学完后你应该能够：

- 说清 `pcileech_pcie_tlp_a7` 在整条数据通路中的位置：它夹在 PCIe 硬核（经 `pcileech_tlps128_src64`/`dst64` 位宽转换）与系统路由中枢 `fifo`（经 `IfPCIeFifoTlp` 契约）之间。
- 画出本模块内部的两条数据流：**接收（RX）方向**——`tlps_rx` 被三路并行消费，其中过滤后的一路转发给主机；**发送（TX）方向**——4 路来源经 `sink_mux1` 仲裁后合并到 `tlps_tx`。
- 看懂 `IfAXIS128` 这条 128 位 AXI-Stream 契约，以及它的 `source/sink` 与 `source_lite/sink_lite` 两套 modport 的本质区别（是否有反压握手）。
- 理解静态 TLP（`tlps_static`）这条由 `cfg` 模块注入的「预置报文」通道的用途。

本讲只做**总览**，先建立「黑盒 + 数据流」的大图；过滤器的位级判定、多路复用器的状态机细节分别留给 u3-l4、u3-l5 深入。

## 2. 前置知识

### 2.1 什么是 TLP

**TLP（Transaction Layer Packet，事务层包）** 是 PCIe 链路上承载一次「事务请求」或「完成」的基本数据包。一个 TLP 由「头（Header）+ 可选数据 + 校验」组成。常见的 TLP 类型有：

| 类型 | 含义 | 典型用途 |
|------|------|----------|
| `MRd` / `MWr` | 内存读 / 写 | 读写主存（DMA 的核心动作） |
| `CfgRd` / `CfgWr` | 配置读 / 写 | 枚举时主机读写设备配置空间 |
| `Cpl` / `CplD` | 完成 / 带数据的完成 | 对读请求的应答（CplD 里带数据） |

一个 TLP 的头第一个 DWORD 的最高几位编码了它的 **Fmt（格式）+ Type（类型）**，本模块的过滤器正是用 `tdata[31:25]` 这 7 位来识别包类型的。

### 2.2 关键直觉：FPGA 既是「转发器」也是「假设备」

pcileech-fpga 在 TLP 层做两件不同的事：

1. **转发给主机**：把链路上看到的 TLP（特别是 `Cpl/CplD` 这类完成包）通过 USB 送给主机软件（PCILeech），让主机读到目标机器的内存数据。
2. **本地应答**：对于主机发给「FPGA 自己」的 `CfgRd/CfgWr`（读配置空间）或 BAR 读写，FPGA **不转发**，而是**自己扮演一个真实设备**当场给出 `CplD` 应答。

`pcileech_pcie_tlp_a7` 同时承担这两件事——这正是它内部结构看似复杂的原因。

### 2.3 AXI-Stream 握手速览

本模块内部所有 TLP 流都走 `IfAXIS128` 这条 128 位 AXI-Stream 风格的总线。它的握手规则很经典：

- 生产者拉高 `tvalid` 表示「数据有效」；消费者拉高 `tready` 表示「我准备好了」。
- 当且仅当 `tvalid && tready` 同时为 1，这一拍的数据被传递（称为一次「传输」transfer）。
- `tlast` 标记一个包的最后一拍；`tuser[0]`（即 `first`）标记一个包的第一拍。

本模块还大量使用一个「无握手」的精简版 `source_lite/sink_lite`——它**没有 `tready`**，生产者只要 `tvalid` 就直接发出，消费者必须每拍都能吞下。为什么需要它，是本讲的一个重点，见 4.2。

### 2.4 承接 u3-l1

u3-l1 已经讲清：PCIe 硬核 `pcie_7x_0` 的 64 位接收流经 `pcileech_tlps128_src64` 转成 128 位的 `tlps_rx`，发送方向则经 `pcileech_tlps128_dst64` 把 128 位的 `tlps_tx` 转回 64 位送进硬核。本讲要打开的 `pcileech_pcie_tlp_a7`，正是这两条 128 位流**中间**的处理核心。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv) | 本讲主角。顶层 `pcileech_pcie_tlp_a7` 模块 + 5 个子模块定义（`dst_fifo`/`filter`/`src_fifo`/`sink_mux1` 在本文件内；`bar_controller`/`cfgspace_shadow` 在同目录其他文件） |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 契约定义。本讲重点读 `IfAXIS128`（128 位流）与 `IfPCIeFifoTlp`（与 fifo 的 TLP 契约）、`IfShadow2Fifo`（控制开关） |
| PCIeSquirrel/src/pcileech_pcie_a7.sv | 上一讲主角，这里只参考它如何**例化**本模块、`tlps_rx`/`tlps_static`/`tlps_tx` 如何接入 |
| PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv | BAR 读写引擎（u4-l2 精读，本讲当黑盒） |
| PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv | 配置空间影子（u4-l1 精读，本讲当黑盒） |

> 说明：本讲把 `bar_controller` 与 `cfgspace_shadow` 当作黑盒，只关心它们的「输入 = `tlps_rx`，输出 = 响应流」。它们的内部细节是 u4 单元的内容。

## 4. 核心概念与源码讲解

### 4.1 模块全景：pcileech_pcie_tlp_a7 的位置与职责

#### 4.1.1 概念说明

把 PCIe 硬核想象成一根「64 位的水管」，主机软件（PCILeech）是另一根「32 位的水管」，`pcileech_pcie_tlp_a7` 就是夹在中间的**净水厂**：它接收来自链路的 128 位 TLP 流（`tlps_rx`），决定哪些转发给主机、哪些自己消化并产生应答；同时把主机想发的、以及自己产生的应答，排好队合并成一条 128 位流（`tlps_tx`）送回链路。

它**不直接**接 PCIe 硬核的 64 位管脚，也不直接接 USB。它对外只暴露 4 条 interface + 2 个端口，与上下游都以 128 位 AXIS 流或 fifo 契约相连。

#### 4.1.2 核心流程

先看一张总图（箭头表示数据流方向）：

```
                         ┌─────────────── pcileech_pcie_tlp_a7 ───────────────┐
                         │                                                    │
  来自 PCIe 硬核         │   ┌──> bar_controller ──────> tlps_bar_rsp ───┐     │
  (经 src64 转成 128位)  │   │                                          ├─►    │  去 PCIe 硬核
  tlps_rx ─────────────►─┼───┼──> cfgspace_shadow ─────> tlps_cfg_rsp ──┤mux│──> tlps_tx
                         │   │                                          │(4) │     │ (经 dst64
                         │   └──> filter ──> tlps_filtered ──> dst_fifo ─┤    │      转回64位)
                         │                                  │           │    │
                         │                                  ▼           │    │
                         │                            dfifo (IfPCIeFifoTlp) │
                         │                                  │           ▼    │
                         │                  主机方向 ◄──────┘      src_fifo │
                         │       主机方向 ──> dfifo.tx ──> src_fifo > tlps_rx_fifo ─┘
                         │                                                    │
                         │   cfg 模块 ──> tlps_static ─────────────────────┘
                         └────────────────────────────────────────────────────┘
```

要点：

- **接收方向**：`tlps_rx` 被 **三路并行**消费（bar_controller、cfgspace_shadow、filter），只有 `filter → dst_fifo → dfifo` 这一路走向主机。
- **发送方向**：`sink_mux1`（图中 `mux(4)`）把 **4 路**来源按优先级合并成 `tlps_tx`：`cfg_rsp`、`bar_rsp`、`rx_fifo`（主机注入）、`static`。
- `tlps_static` 是 cfg 模块送进来的「预置 TLP」通道，独立于 RX/TX 主流。

#### 4.1.3 源码精读

本模块的端口声明只有 7 个，非常干净：

模块声明：定义复位、两个时钟、4 条 interface 与 `pcie_id` 端口。[pcileech_pcie_tlp_a7.sv:13-25](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L13-L25)

```systemverilog
module pcileech_pcie_tlp_a7(
    input                   rst,
    input                   clk_pcie,        // PCIe 用户时钟域
    input                   clk_sys,         // 系统（主机侧）时钟域
    IfPCIeFifoTlp.mp_pcie   dfifo,           // 与 fifo 的 TLP 契约
    IfAXIS128.source        tlps_tx,         // 发往 PCIe 硬核的 128 位流
    IfAXIS128.sink_lite     tlps_rx,         // 来自 PCIe 硬核的 128 位流
    IfAXIS128.sink          tlps_static,     // 来自 cfg 模块的静态 TLP
    IfShadow2Fifo.shadow    dshadow2fifo,    // 控制开关 + 配置空间影子桥接
    input [15:0]            pcie_id          // 本设备的 Bus/Dev/Func 拼成的 ID
    );
```

注意几个关键 modport：

- `tlps_tx` 是 `source`（本模块是生产者，要响应下游 `tready` 反压）。
- `tlps_rx` 是 `sink_lite`（本模块是消费者，但**无反压**——上游只管发，本模块必须每拍吞下）。
- `tlps_static` 是 `sink`（有反压的消费者）。

紧接着，模块内部声明了 3 条本地 interface 作为子模块之间的「内线」：

内部流声明：`tlps_bar_rsp`、`tlps_cfg_rsp`、`tlps_filtered` 三条 128 位内线。[pcileech_pcie_tlp_a7.sv:27-33](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L27-L33)

随后的 6 个例化就是上面总图的 6 个方块（本文件内可见其中 4 个 + mux，另两个 bar/cfg 在外部文件）。这部分会在 4.3、4.4 逐一展开。

> 关于子模块数量：本模块实际例化了 **6 个**子模块（`bar_controller`、`cfgspace_shadow`、`filter`、`dst_fifo`、`src_fifo`、`sink_mux1`）。它们按 RX / TX 分成两组，是本讲「总览」的主角。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍「模块在工程里的接线」，确认它在数据通路中的位置。

**操作步骤**：

1. 打开 `PCIeSquirrel/src/pcileech_pcie_a7.sv`，定位 `i_pcileech_pcie_tlp_a7` 的例化。
2. 对照上面 4.1.2 的总图，在纸上写下 `tlps_rx`、`tlps_tx`、`tlps_static`、`dfifo` 四个端口分别连到了 `pcileech_pcie_a7.sv` 里的哪个信号、哪个生产者/消费者。

**预期结果**：你应该得到如下对应关系（参考 [pcileech_pcie_a7.sv:73-111](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L73-L111)）：

| 本模块端口 | 上游/下游 | 对端模块与 modport |
|------------|-----------|--------------------|
| `tlps_rx`（输入） | 上游 | `pcileech_tlps128_src64` 的 `.tlps_out ( tlps_rx.source_lite )`（64→128 转换器） |
| `tlps_tx`（输出） | 下游 | `pcileech_tlps128_dst64` 的 `.tlps_in ( tlps_tx.sink )`（128→64 转换器） |
| `tlps_static`（输入） | 上游 | `pcileech_pcie_cfg_a7` 的 `.tlps_static ( tlps_static.source )`（cfg 模块生产） |
| `dfifo` | 双向 | 顶层 `pcileech_squirrel_top` 的 `dtlp` interface（连到 fifo） |

> 这一步纯阅读，无需运行；目的是确认「本模块不碰 64 位硬核管脚，只碰 128 位流」。

#### 4.1.5 小练习与答案

**练习 1**：本模块声明了 `clk_pcie` 和 `clk_sys` 两个时钟，但 `bar_controller`、`cfgspace_shadow`、`filter` 三个 RX 消费者中，有没有哪个运行在 `clk_sys`？

**答案**：没有。`bar_controller` 和 `filter` 完全运行在 `clk_pcie`；`cfgspace_shadow` 同时声明了 `clk_pcie` 和 `clk_sys`，但它消费 `tlps_rx` 的逻辑在 `clk_pcie` 域，`clk_sys` 只用于它内部与主机侧配置空间 BRAM 的桥接。RX 主流始终在 `clk_pcie`。

**练习 2**：为什么 `tlps_rx` 用 `sink_lite`（无反压）而不是 `sink`（有反压）？

**答案**：因为 `tlps_rx` 由上游 `pcileech_tlps128_src64` 驱动，而它在本模块内**同时**被三个子模块读取（扇出）。若有反压，三个消费者的 `tready` 很难一致；用 `sink_lite` 即「上游只管发、下游保证每拍吞」，把流量控制的责任交给上游保证（链路速率与处理速率已匹配）。这是 AXIS 的常见用法。

---

### 4.2 IfAXIS128：128 位 TLP 数据流契约

#### 4.2.1 概念说明

`IfAXIS128` 是本模块内部以及与位宽转换器之间的「统一货币」：一条 128 位（4 个 DWORD）宽、带包边界标记的 AXI-Stream 流。它的设计目标是用尽可能少的信号承载一个 TLP 的所有元数据：数据本身、哪些 DWORD 有效、包的首/末拍、以及命中了哪个 BAR。

它一共定义了 **4 个 modport**，分成「有反压」和「无反压」两套，这是它最容易被忽视却最关键的设计。

#### 4.2.2 核心流程

每个 128 位传输（一拍）携带的信息：

| 字段 | 位宽 | 含义 |
|------|------|------|
| `tdata` | 128 | 4 个 DWORD 数据 |
| `tkeepdw` | 4 | 每个 DWORD 的有效位（类似字节使能，但粒度是 DWORD） |
| `tvalid` | 1 | 本拍数据有效 |
| `tlast` | 1 | 本拍是一个包的最后一拍 |
| `tuser` | 9 | 元数据，见下 |
| `tready` | 1 | 消费者就绪（仅 `source/sink` 有） |
| `has_data` | 1 | 生产者「有整包待发」（仅 `source/sink` 有） |

`tuser` 的 9 位被复用编码了三件事：

```
tuser[0]     = first   （本拍是包的第一拍）
tuser[1]     = last    （本拍是包的最后一拍，与 tlast 冗余备份）
tuser[8:2]   = BAR hit （2=BAR0, 3=BAR1, … 7=BAR5, 8=EXPROM）
```

`BAR hit` 位由上游 `pcileech_tlps128_src64` 在 64→128 转换时统计 DWORD 并抽出（u3-l1 已讲），告诉下游「这个包命中了哪个 BAR」，这样 `bar_controller` 不必再去解地址。

两套 modport 的区别：

```
有反压（握手）：
  source : input  tready
           output tdata,tkeepdw,tvalid,tlast,tuser,has_data
  sink   : output tready
           input  tdata,tkeepdw,tvalid,tlast,tuser,has_data

无反压（精简，单生产者对多消费者或保证不丢的链路）：
  source_lite : output tdata,tkeepdw,tvalid,tlast,tuser
  sink_lite   : input  tdata,tkeepdw,tvalid,tlast,tuser
```

`_lite` 版本**没有** `tready` 与 `has_data`：生产者拉 `tvalid` 即视为已发送，消费者不得阻塞。

#### 4.2.3 源码精读

接口定义与 `tuser` 编码注释：[pcileech_header.svh:165-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)

```systemverilog
interface IfAXIS128;
    wire [127:0]    tdata;
    wire [3:0]      tkeepdw;
    wire            tvalid;
    wire            tlast;
    wire [8:0]      tuser;      // [0] = first
                                // [1] = last
                                // [8:2] = BAR, 2=BAR0, 3=BAR1, .. 7=BAR5, 8=EXPROM
    wire            tready;
    wire            has_data;
```

四个 modport：注意 `source_lite`/`sink_lite` 比 `source`/`sink` 少了 `tready` 和 `has_data`。[pcileech_header.svh:177-193](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L177-L193)

回到本模块，可以看到这套契约是如何「因地制宜」选用的：

- 上游给的 `tlps_rx` 是 `sink_lite`（多消费者，无反压）——见 [pcileech_pcie_tlp_a7.sv:21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L21)。
- `filter` 的输出 `tlps_filtered` 也是 `source_lite`/`sink_lite` 串到 `dst_fifo`——[pcileech_pcie_tlp_a7.sv:54-69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L54-L69)，因为 filter 到 dst_fifo 是单生产者单消费者、且时钟域由 `dst_fifo` 内的 FIFO 吸收。
- 进入 `sink_mux1` 的 4 路（`tlps_cfg_rsp`/`tlps_bar_rsp`/`tlps_rx_fifo`/`tlps_static`）都改成有反压的 `sink`——[pcileech_pcie_tlp_a7.sv:86-94](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L86-L94)，因为 mux 要按整包仲裁、必须能压住没被选中的源。

> 一句话记忆：**「多读/直连」用 lite，「要排队仲裁」用 sink/source。**

#### 4.2.4 代码实践

**实践目标**：给 `IfAXIS128` 做一份信号说明表，并验证 modport 方向自洽。

**操作步骤**：

1. 打开 `pcileech_header.svh` 的 `IfAXIS128` 段。
2. 画一张 7 行 × 3 列（信号 / 位宽 / 方向）的表，分别填 `source` 和 `sink` 两个 modport。
3. 检查：同一个信号在 `source` 和 `sink` 中方向是否**互补**（一个 `input` 一个 `output`）。

**预期结果**：`tready` 在 `source` 是 `input`、在 `sink` 是 `output`；其余 `tdata/tkeepdw/tvalid/tlast/tuser/has_data` 在 `source` 是 `output`、在 `sink` 是 `input`。完全互补，这正是 modport「契约」的体现（插错方向综合期会报错）。

#### 4.2.5 小练习与答案

**练习 1**：`tuser[1]` 标记 `last`，而接口里又有一个独立的 `tlast` 信号，二者重复吗？为什么同时存在？

**答案**：功能上重复（都表示「包末拍」），属于冗余编码。`tlast` 是 AXI-Stream 标准信号，供常规握手逻辑使用；`tuser[1]` 把末拍信息打包进 `tuser`，方便跨 FIFO 时把整个元数据当作一个位向量整体搬运（本模块的 `fifo_134_134` 就是把 `{first, tlast, tkeepdw, tdata}` 拼成 134 位一起过 FIFO）。二者并用既兼容标准、又便于打包。

**练习 2**：一条 `IfAXIS128` 流上传输一个「3 DWORD 头 + 0 数据」的配置读请求，`tkeepdw` 和 `tlast` 在各拍分别是什么？

**答案**：3 DWORD 头可放进一个 128 位（4 DWORD）拍里。首拍 `tuser[0]=1`（first），`tkeepdw=4'b0111`（低 3 个 DWORD 有效），`tlast=1`（同时也是末拍），`tvalid=1`。整个包只需一拍。

---

### 4.3 接收（RX）方向：三路并行消费与过滤转发

#### 4.3.1 概念说明

接收方向的核心问题是：**一条 `tlps_rx` 流进来，该交给谁？** 本模块的答案是「同时交给三个处理者，各取所需」：

1. **`bar_controller`**：看里面有没有「读写 BAR（内存空间）」的请求。若有，且 `bar_en` 打开，就由它生成 `CplD`（带数据完成）或执行写动作，应答走 `tlps_bar_rsp`。
2. **`cfgspace_shadow`**：看里面有没有 `CfgRd/CfgWr`（读写 256 字节配置头）。若有，就查/改内部的配置空间影子 BRAM，应答走 `tlps_cfg_rsp`。
3. **`filter`**：把这条流「过滤一遍」后转发给主机——例如丢掉 `CfgRd/CfgWr`（这些已被 cfgspace_shadow 本地处理，不必再烦主机），或只保留 `Cpl/CplD`。

这三者**并行**读同一份 `tlps_rx`，互不干扰：前两个是「本地应答」（产出新的 TLP 回链路），第三个是「转发给主机」（走向 USB）。

#### 4.3.2 核心流程

```
tlps_rx (sink_lite, clk_pcie)
   ├──> bar_controller   (bar_en 控制)  ──产出──> tlps_bar_rsp   (source)  ──> TX 方向
   ├──> cfgspace_shadow  (cfgtlp...)    ──产出──> tlps_cfg_rsp   (source)  ──> TX 方向
   └──> filter (alltlp_filter/cfgtlp_filter)
              ──过滤──> tlps_filtered (source_lite)
                          └──> dst_fifo (clk_pcie -> clk_sys, 128->4×32)
                                   └──> dfifo.rx_*  ──> 主机 (fifo -> mux -> FT601)
```

关键点：

- `tlps_rx` 是 `sink_lite`，所以「一个源、三个读」是合法且无冲突的（读 wire 不消耗）。
- 只有 `filter → dst_fifo → dfifo` 这一条路通向主机；`dfifo` 是 `IfPCIeFifoTlp` 契约，它把 128 位流拆成 4 路 32 位 `rx_data[4]` 送给 fifo（u2-l3/u2-l4 已讲过 fifo 如何再把这 4 路打包回 256 位送 USB）。
- `dst_fifo` 在这里同时完成**两件事**：跨时钟域（`clk_pcie → clk_sys`）和**位宽拆分**（128 位 → 4×32 位）。

#### 4.3.3 源码精读

三路并行消费的例化：bar_controller、cfgspace_shadow、filter 都把 `.tlps_in` 接到同一个 `tlps_rx`。[pcileech_pcie_tlp_a7.sv:35-61](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L35-L61)

```systemverilog
pcileech_tlps128_bar_controller i_pcileech_tlps128_bar_controller(
    .rst            ( rst                           ),
    .clk            ( clk_pcie                      ),
    .bar_en         ( dshadow2fifo.bar_en           ),  // 功能开关
    .pcie_id        ( pcie_id                       ),
    .tlps_in        ( tlps_rx                       ),  // 同一输入流
    .tlps_out       ( tlps_bar_rsp.source           )   // 本地应答
);

pcileech_tlps128_cfgspace_shadow i_pcileech_tlps128_cfgspace_shadow(
    .rst            ( rst                           ),
    .clk_pcie       ( clk_pcie                      ),
    .clk_sys        ( clk_sys                       ),
    .tlps_in        ( tlps_rx                       ),  // 同一输入流
    .pcie_id        ( pcie_id                       ),
    .dshadow2fifo   ( dshadow2fifo                  ),  // 主机改写配置空间的桥
    .tlps_cfg_rsp   ( tlps_cfg_rsp.source           )   // 本地应答
);

pcileech_tlps128_filter i_pcileech_tlps128_filter(
    .rst            ( rst                           ),
    .clk_pcie       ( clk_pcie                      ),
    .alltlp_filter  ( dshadow2fifo.alltlp_filter    ),  // 过滤策略
    .cfgtlp_filter  ( dshadow2fifo.cfgtlp_filter    ),
    .tlps_in        ( tlps_rx                       ),  // 同一输入流
    .tlps_out       ( tlps_filtered.source_lite     )   // 过滤后副本
);
```

注意三个功能开关 `bar_en`、`alltlp_filter`、`cfgtlp_filter` 全部来自 `dshadow2fifo`——即主机写 `rw` 寄存器后由 fifo 翻译过来的控制位（u2-l5 已讲）。这就是「主机软件能一键开关 BAR 仿真 / 配置包过滤」的硬件落点。

`filter` 模块内部用一个 7 位的 Fmt/Type 匹配判定包类型（细节留 u3-l4），这里只看它的判定片段，体会「按 tdata[31:25] 分类」：

filter 的包类型识别：用首拍 `tdata[31:25]` 匹配 Cpl/CplD/CfgRd/CfgWr。[pcileech_pcie_tlp_a7.sv:178-187](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L178-L187)

转发给主机的桥 `dst_fifo`：用一个 `fifo_134_134_clk2` 双时钟 FIFO，把 `{first, tlast, tkeepdw, tdata}` 共 134 位从 `clk_pcie` 搬到 `clk_sys`，再拆成 4 路 32 位写进 `dfifo`。[pcileech_pcie_tlp_a7.sv:118-146](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L118-L146)

```systemverilog
fifo_134_134_clk2 i_fifo_134_134_clk2 (
    .wr_clk     ( clk_pcie          ),      // 写侧：PCIe 域
    .rd_clk     ( clk_sys           ),      // 读侧：系统域
    .din        ( { tlps_in.tuser[0], tlps_in.tlast, tlps_in.tkeepdw, tlps_in.tdata } ),
    ...
);
// 读出后拆成 4 个 DWORD
assign dfifo.rx_data[0] = tdata[31:0];
...
assign dfifo.rx_last[3] = tlast && (tkeepdw == 4'b1111);  // 末拍落在第 4 个 DWORD
```

> 关于 `134`：128（tdata）+ 4（tkeepdw）+ 1（tlast）+ 1（first）= 134，这就是 IP 名 `fifo_134_134` 的由来。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「主机读目标机内存」时返回的 `CplD` 包，在 RX 方向走了哪条路。

**操作步骤**：

1. 假设主机经 USB 下发了一个「读目标机地址 X」的 `MRd` 请求（这条走 TX 方向，下一节讲）。
2. 目标机内存控制器返回一个带数据的 `CplD`，被 PCIe 硬核接收，经 `src64` 转成 `tlps_rx`。
3. 回答：这个 `CplD` 会被 bar_controller / cfgspace_shadow / filter 三者中的哪个「相中」并转发给主机？

**预期结果**：

- `bar_controller` 不关心它（它只处理对本设备 BAR 的访问）。
- `cfgspace_shadow` 不关心它（它只处理 `CfgRd/CfgWr`）。
- `filter` 默认放行 `Cpl/CplD`（注意 `filter_next` 里 `Cpl` 被特意排除在 `alltlp_filter` 之外），所以 `CplD` → `tlps_filtered` → `dst_fifo` → `dfifo` → fifo → mux → FT601 → 主机软件，主机据此拿到内存数据。

> 现象待本地验证：若有硬件，可用 PCILeech 的 `MemRead` 命令读目标机一个已知地址，观察是否返回正确数据——这验证了整条 RX 转发链。

#### 4.3.5 小练习与答案

**练习 1**：如果主机把 `cfgtlp_filter` 置 1，链路上的 `CfgRd` 会怎样？

**答案**：`CfgRd` 在 `filter` 里被判定为「应过滤」，于是**不会**出现在 `tlps_filtered`，也就不会转发给主机。但 `cfgspace_shadow` 仍会看到它并给出本地 `CplD` 应答（走 `tlps_cfg_rsp` 回链路）。所以主机既不会收到这份配置读的原始包，目标系统也能得到正常的配置应答——这正是「FPGA 假装是真设备」的关键。

**练习 2**：`dst_fifo` 为什么不在 `clk_sys` 单时钟运行，而要做 `clk_pcie→clk_sys` 跨域？

**答案**：因为输入 `tlps_filtered` 由 `filter` 在 `clk_pcie` 域产生，而输出要送给 fifo/USB 一侧的 `clk_sys` 域。两个时钟异步，必须用双时钟 FIFO 做安全跨越（单时钟 FIFO 会因亚稳态采样出错）。这也是后续 u5-l1「跨时钟域」要展开的主题。

---

### 4.4 发送（TX）方向：src_fifo 打包与 sink_mux1 四路仲裁

#### 4.4.1 概念说明

发送方向解决两个问题：

1. **主机想发的包怎么进来？** 主机经 USB → fifo → `dfifo.tx_data`（32 位）送来的原始 TLP，需要重新打包成 128 位流——这是 `src_fifo` 的活（注意它叫 src 是站在「向 PCIe 核送数」的视角，实质是把主机来的 32 位流「拼成」128 位）。
2. **4 路来源谁先发？** `tlps_tx` 同一时刻只能发一路，于是 `sink_mux1` 做优先级仲裁：`cfg_rsp` > `bar_rsp` > `rx_fifo`（主机注入）> `static`。

`sink_mux1` 的核心原则是「**整包不可打断**」：一旦选中某路，必须等它发完整个包（`tvalid && tlast`）才允许切换到下一路。

#### 4.4.2 核心流程

```
主机注入：dfifo.tx_data(32b)/tx_valid/tx_last  (clk_sys)
             │
             ▼
        src_fifo   ──32→128 位宽转换──> tlps_rx_fifo (source, clk_pcie)
                                          │
   cfg 模块 ──> tlps_cfg_rsp ──────────────┤  (优先级最高)
   bar_controller ──> tlps_bar_rsp ───────┤
   src_fifo ──> tlps_rx_fifo ─────────────┤  (主机注入)
   cfg 模块 ──> tlps_static ───────────────┤  (优先级最低)
                                          ▼
                                     sink_mux1   ──按整包仲裁──> tlps_tx (source)
                                          │                            │
                                          │                            ▼
                                          │                     dst64 (128->64) -> PCIe 硬核
```

`sink_mux1` 的仲裁规则（伪代码）：

```
每一拍：
  if (当前没选中任何源 id==0) 或 (当前包刚发完 tvalid && tlast):
      id_next = 编号最小的 has_data 的源   # 重新选最高优先级
  else:
      id_next = id                          # 维持当前源，发完整包
  被选中的源 tready = 下游 tready && (id_next == 它)
```

优先级 `id_next_newsel` 是固定的 `1 > 2 > 3 > 4`，分别对应 `cfg_rsp > bar_rsp > rx_fifo > static`。

#### 4.4.3 源码精读

`src_fifo` 例化：把 `dfifo` 的 32 位 TX 数据转成 128 位 `tlps_rx_fifo`。[pcileech_pcie_tlp_a7.sv:76-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L76-L84)

`sink_mux1` 例化：4 路输入按固定顺序接入，注意 in1=cfg_rsp（最高）、in4=static（最低）。[pcileech_pcie_tlp_a7.sv:86-94](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L86-L94)

```systemverilog
pcileech_tlps128_sink_mux1 i_pcileech_tlps128_sink_mux1(
    .rst            ( rst                           ),
    .clk_pcie       ( clk_pcie                      ),
    .tlps_out       ( tlps_tx                       ),  # 合并后输出
    .tlps_in1       ( tlps_cfg_rsp.sink             ),  # 优先级 1：配置应答
    .tlps_in2       ( tlps_bar_rsp.sink             ),  # 优先级 2：BAR 应答
    .tlps_in3       ( tlps_rx_fifo.sink             ),  # 优先级 3：主机注入
    .tlps_in4       ( tlps_static                   )   # 优先级 4：静态 TLP
);
```

`sink_mux1` 的仲裁逻辑：`id_next_newsel` 选最小编号有数据者；`id_next` 保证整包发送不打断。[pcileech_pcie_tlp_a7.sv:332-342](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L332-L342)

```systemverilog
wire [2:0] id_next_newsel = tlps_in1.has_data ? 1 :
                            tlps_in2.has_data ? 2 :
                            tlps_in3.has_data ? 3 :
                            tlps_in4.has_data ? 4 : 0;

wire [2:0] id_next = ((id==0) || (tlps_out.tvalid && tlps_out.tlast))
                       ? id_next_newsel : id;     // 当前包没发完就维持

assign tlps_in1.tready = tlps_out.tready && (id_next==1);
...
```

`src_fifo` 内部用「每来一个有效 DWORD 就移位填入 128 位」的状态机做 32→128 拼装（细节留 u3-l5），并额外用一个 1 位深的双时钟 FIFO 做包计数（`pkt_count`）跨域传递「有几个整包待发」，从而支撑 mux 的 `has_data` 判断。[pcileech_pcie_tlp_a7.sv:245-282](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L245-L282)

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：画出 `sink_mux1` 的 4 路输入框图，并标注每路来源模块——这是本讲规格要求的实践任务。

**操作步骤**：

1. 读 [pcileech_pcie_tlp_a7.sv:86-94](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L86-L94) 的 `sink_mux1` 例化，记下 `tlps_in1..in4` 各接哪条内线。
2. 反查每条内线的 `.source` 端在哪个例化里被驱动，找到来源模块。
3. 画出框图，按优先级从高到低排列。

**预期结果**（参考答案框图）：

```
   pcie_cfg_a7 (cfg模块)
        │ tlps_cfg_rsp  (CfgRd/CfgWr 的 CplD 应答)
        ▼
   ┌───────── in1 (优先级1，最高) ─────────┐
   │                                      │
   pcileech_tlps128_cfgspace_shadow       │
   (本模块内的配置空间影子)                │
                                          ▼
   ─── in2 (优先级2) ◄── tlps_bar_rsp ◄── pcileech_tlps128_bar_controller (BAR 引擎)
                                          │
   ─── in3 (优先级3) ◄── tlps_rx_fifo ◄── pcileech_tlps128_src_fifo (主机注入 dfifo.tx)
                                          │
   ─── in4 (优先级4，最低) ◄── tlps_static ◄── pcileech_pcie_cfg_a7 (cfg 模块的静态 TLP)
                                          ▼
                                     sink_mux1 ──> tlps_tx ──> dst64 ──> PCIe 硬核
```

对应关系表：

| mux 输入 | 内线 | 来源模块 | 含义 | 优先级 |
|----------|------|----------|------|--------|
| `tlps_in1` | `tlps_cfg_rsp` | `cfgspace_shadow`（本地配置空间影子） | 对 `CfgRd/CfgWr` 的 `CplD` 应答 | 1（最高） |
| `tlps_in2` | `tlps_bar_rsp` | `bar_controller`（BAR 读写引擎） | 对 BAR 读写的 `CplD` 应答 | 2 |
| `tlps_in3` | `tlps_rx_fifo` | `src_fifo`（主机注入） | 主机经 USB 下发的任意 TLP | 3 |
| `tlps_in4` | `tlps_static` | `pcileech_pcie_cfg_a7`（cfg 模块） | 预置/触发的静态 TLP | 4（最低） |

> 思考题（不必写答）：为什么把「本地应答」放在比「主机注入」更高的优先级？——因为本地应答（`CplD`）若被主机注入的包挤掉，会导致 PCIe 链路上出现「超时未完成」的请求，引发重传甚至链路异常；而主机注入的包晚几拍发出去并无大碍。

#### 4.4.5 小练习与答案

**练习 1**：`id_next` 表达式 `((id==0) || (tlps_out.tvalid && tlps_out.tlast)) ? id_next_newsel : id` 中，两个重新选择条件分别解决什么问题？

**答案**：`id==0` 处理「当前空闲、没有正在发的包」，此时应立刻选一个有数据的源。`tlps_out.tvalid && tlps_out.tlast` 处理「当前包正好发完最后一拍」，此时可以安全切换。两者都不满足时维持 `id`，保证一个包从同一源完整发出、不被中途打断。

**练习 2**：`tlps_static` 接的是 `tlps_static`（来自端口，`sink` modport），而前三个接的是内部声明的 `.sink`。这有区别吗？

**答案**：没有功能区别，都是 `IfAXIS128.sink`（有反压的消费者）。`tlps_static` 直接复用了模块对外的 `tlps_static` 端口（由外部 cfg 模块驱动），而另外三路是本模块内部声明的内线（由内部子模块驱动）。对 mux 而言四路完全同质。

---

### 4.5 静态 TLP（tlps_static）：来自 cfg 模块的注入通道

#### 4.5.1 概念说明

`tlps_static` 是一条「特殊」的发送来源：它**不是**对某个收到的请求的应答，而是 cfg 模块**主动**产生的一个预置 TLP。它的典型用途是在特定时刻（例如设备上线、或主机触发某个控制位时）向链路注入一个预先拼好的包——比如一条配置写、一个消息、或一次中断相关的 TLP。

把它放在 mux 的**最低优先级**（`in4`）是合理的：静态注入是「锦上添花」的主动动作，绝不应抢占对正常请求的应答。

#### 4.5.2 核心流程

```
pcileech_pcie_cfg_a7 (cfg 模块，运行于 clk_pcie/clk_sys)
        │ 在条件满足时拼好一个 TLP，经 .tlps_static (source) 输出
        ▼
   tlps_static  ─── 接入 sink_mux1 的 in4 (最低优先级) ───> tlps_tx ──> 链路
```

它和 `tlps_cfg_rsp` 都来自「cfg 相关模块」，但二者不同：

- `tlps_cfg_rsp` 是 cfgspace_shadow 对**收到**的 `CfgRd/CfgWr` 的应答（被动）。
- `tlps_static` 是 cfg 模块按内部逻辑**主动**发出的包（主动）。

#### 4.5.3 源码精读

cfg 模块生产 `tlps_static`：在 `pcileech_pcie_a7.sv` 里，`pcileech_pcie_cfg_a7` 的 `.tlps_static ( tlps_static.source )` 表明 cfg 模块是这条流的生产者。[pcileech_pcie_a7.sv:73-81](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L73-L81)

本模块消费 `tlps_static`：作为 `sink` 接入 mux 的 `in4`。[pcileech_pcie_tlp_a7.sv:22](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L22) 与 [pcileech_pcie_tlp_a7.sv:93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L93)

> `tlps_static` 的具体内容由 cfg 模块的内部逻辑决定，涉及 `cfg_mgmt` 与中断/配置寄存器，属于 u3-l2（cfg 管理）的延伸，本讲只确认它在 TLP 总览里的位置：一条独立的、最低优先级的主动注入通道。

#### 4.5.4 代码实践

**实践目标**：确认 `tlps_static` 这条线的两端各是谁。

**操作步骤**：

1. 在 `pcileech_pcie_a7.sv` 中搜 `tlps_static`，列出它出现的每一处。
2. 指出生产者（`.source` 端）和消费者（`.sink` 端）分别是哪个模块。

**预期结果**：

- 生产者：`pcileech_pcie_cfg_a7`（`.tlps_static ( tlps_static.source )`）。
- 消费者：`pcileech_pcie_tlp_a7`（`.tlps_static ( tlps_static.sink )`），进而连到 `sink_mux1` 的 `in4`。
- 注释 `// static tlp transmit from cfg->tlp`（[pcileech_pcie_a7.sv:46](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L46)）一句话点明了它的方向：从 cfg 流向 tlp。

#### 4.5.5 小练习与答案

**练习 1**：如果把 `tlps_static` 从 mux 的 `in4` 挪到 `in1`，会有什么风险？

**答案**：静态 TLP 会获得最高优先级，可能持续抢占 `cfg_rsp`/`bar_rsp` 这些对正常请求的应答，导致 PCIe 链路上的请求长时间得不到完成（completion timeout），引发重传甚至链路降级/断开。所以主动注入类流量放最低优先级是安全的设计。

**练习 2**：`tlps_static` 用 `sink`（有反压）而不是 `sink_lite`，这意味着什么？

**答案**：意味着 mux 在没选中它时可以通过不拉 `tready` 把它「压住」，cfg 模块必须等待。这是必要的——因为 mux 一次只发一路，未被选中的源必须能被暂停，否则静态包会丢。`_lite` 无法做到这一点。

---

## 5. 综合实践

**任务**：把本讲的三条主线（RX 并行消费、TX 四路仲裁、`IfAXIS128` 契约）串成一张完整的「TLP 处理总览大图」，并用一个具体场景走通它。

请完成以下三步：

1. **画总图**：在一张 A4 纸上画出 `pcileech_pcie_tlp_a7` 的完整框图，包含：
   - 左侧输入：`tlps_rx`（来自 src64）、`tlps_static`（来自 cfg）、`dfifo.tx_*`（来自主机）。
   - 内部 6 个子模块方块及它们之间的内线（`tlps_bar_rsp`、`tlps_cfg_rsp`、`tlps_filtered`、`tlps_rx_fifo`）。
   - 右侧输出：`tlps_tx`（去 dst64）、`dfifo.rx_*`（去主机）。
   - 标出每条线的 modport 类型（`source/sink` 还是 `_lite`）和所在时钟域（`clk_pcie` 还是 `clk_sys`）。

2. **场景走查**：假设主机执行一次「读取本设备 BAR0 的 4 字节」操作，按顺序写出涉及的模块与流向：
   - 主机发的 `MRd`（针对 BAR0）→ 经 USB → fifo → `dfifo.tx` → `src_fifo` → `tlps_rx_fifo` → mux（in3）→ `tlps_tx` → dst64 → PCIe 硬核 → 链路。
   - 但因为目标地址命中本设备 BAR0，这个读请求其实**不会**走向链路——它被 `bar_controller` 在 `tlps_rx` 一侧拦下（`bar_en` 打开时），由 bar_controller 生成 `CplD` → `tlps_bar_rsp` → mux（in2）→ `tlps_tx` → … 回到主机。

3. **契约核验**：在你的总图上，检查所有「有反压」的连接（`source/sink`）是否都出现在需要「排队仲裁」或「跨模块暂停」的位置；所有 `_lite` 连接是否都出现在「多读者」或「上游保证不丢」的位置。若有一处不符合，说明为什么这里是例外。

**预期结果**：一张自洽的总图 + 一段能自圆其说的场景走查。这张图也是阅读 u3-l4（过滤器细节）和 u3-l5（mux/位宽转换细节）时的「定位地图」。

> 综合实践为源码阅读型，无需硬件；若条件允许，可在硬件上用 `lspci -x` 读取本设备配置空间，验证 cfgspace_shadow 这条本地应答链是否工作（应能看到自定义的配置空间内容）。

## 6. 本讲小结

- `pcileech_pcie_tlp_a7` 是 PCIe 硬核与系统 fifo 之间的 **TLP 调度枢纽**，只碰 128 位 AXIS 流，不碰 64 位硬核管脚。
- **接收方向**：一条 `tlps_rx` 被 `bar_controller`、`cfgspace_shadow`、`filter` **三路并行**消费；前两者产出本地应答，后者过滤后经 `dst_fifo`（跨 `clk_pcie→clk_sys`、128→4×32 位）转发给主机。
- **发送方向**：`sink_mux1` 把 `cfg_rsp`、`bar_rsp`、`rx_fifo`（主机注入）、`static` 四路按**固定优先级**且**整包不打断**地合并成 `tlps_tx`；`src_fifo` 负责把主机来的 32 位流拼成 128 位。
- `IfAXIS128` 提供两套 modport：`source/sink`（有反压，用于需要仲裁/暂停处）和 `source_lite/sink_lite`（无反压，用于多读者/保证不丢处）——选哪套由连接拓扑决定。
- 三个功能开关 `bar_en`、`cfgtlp_filter`、`alltlp_filter` 都来自 `dshadow2fifo`，是主机写 `rw` 寄存器后翻译出的控制位。
- `tlps_static` 是 cfg 模块**主动**注入的预置 TLP，位于 mux 最低优先级，区别于被动的 `tlps_cfg_rsp`。

## 7. 下一步学习建议

- **u3-l4（TLP 过滤与 FIFO 桥接）**：深入 `pcileech_tlps128_filter` 的 Fmt/Type 位级判定，以及 `dst_fifo`/`src_fifo` 内部位宽转换与跨时钟 FIFO 的实现细节。
- **u3-l5（TLP 多路复用与 64/128 位流转换）**：精读 `sink_mux1` 的仲裁状态机、`src_fifo` 的 32→128 拼装与 `pkt_count` 包计数机制。
- **u4-l1（自定义配置空间影子）**：打开本讲当黑盒的 `cfgspace_shadow`，看它如何用 BRAM 实现 `CfgRd/CfgWr` 的本地应答、并接受主机经 `dshadow2fifo` 改写。
- **u4-l2（BAR PIO 控制器）**：打开 `bar_controller`，看它的读/写引擎如何识别 BAR 访问并拼装 `CplD`。
- **u5-l1（跨时钟域设计与双时钟 FIFO）**：系统理解本讲多次出现的 `fifo_134_134_clk2`、`fifo_1_1_clk2` 这类双时钟 FIFO 在异步时钟域之间的安全传递原理。
