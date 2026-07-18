# 接口与 modport：模块间的契约层

## 1. 本讲目标

在上一讲（u1-l4）里，我们已经看到顶层 `pcileech_squirrel_top.sv` 把 `com`、`fifo`、`pcie_a7` 三大子系统，通过 5 个「interface 实例」连接起来。本讲就钻进这层「接线」，搞清楚：

- SystemVerilog 的 **interface**（接口）和 **modport**（模块端口视图）到底是什么，为什么把它们称为「模块间的契约」。
- 逐个精读 `pcileech_header.svh` 中定义的核心接口：`IfComToFifo`、`IfPCIeFifoTlp`、`IfAXIS128`、`IfShadow2Fifo`，看懂每根信号的**方向**和**含义**。
- 掌握 `source`/`sink`、`mp_com`/`mp_fifo` 这类 modport 命名背后的「生产者—消费者」模型，并能独立读懂任意一个接口的信号表。
- 看懂 `IfAXIS128` 里 `tuser` 字段如何用 9 位同时编码「首拍、末拍、命中哪个 BAR」。

学完后，你应该能打开任意一个 `pcileech_*.sv` 模块，看懂它的 interface 端口连了哪一侧、方向如何，而不再被一大堆 `wire` 吓到。

## 2. 前置知识

### 2.1 为什么需要 interface

在传统 Verilog 里，两个模块之间要传递一组信号，必须在**两边各写一遍**一模一样的端口列表，再在顶层用一束 `wire` 把它们一一接上。信号一多（比如 PCIe 有几十根状态线），端口列表又长又容易写错，改一处还得改三处。

SystemVerilog 的 **interface** 就是为了解决这个问题：把一组「总是一起出现、一起连接」的信号**打包成一个容器**。模块只要声明一个 interface 类型的端口，就等于一次性接上了整束信号。顶层也只需要声明一个 interface 实例，像一根「粗电缆」那样把它插到两个模块上。

### 2.2 modport：给每个模块限定方向

光打包还不够。interface 里的信号对两端来说方向是相反的——同一根 `com_dout`，对 `com` 模块是 **输出**，对 `fifo` 模块是 **输入**。如果两个模块都把同一根线当输出驱动，就会短路。

**modport**（module port 的缩写）就是给 interface 里的信号，**从某个模块的视角**标明方向（`input` 还是 `output`）。于是同一个 interface 可以定义多个 modport 视图，例如 `mp_com`（com 侧视角）和 `mp_fifo`（fifo 侧视角）。模块例化时写明用哪个 modport，综合工具就会按这个方向检查——这就是「契约」二字的由来：**谁负责驱动、谁只能读，在声明时就钉死了**。

> 直觉比喻：interface 是一根**多芯电缆**，modport 是电缆两头的**插头型号**。同一根电缆，一头是公头（output 多）、一头是母头（input 多），插错型号就插不进去。

### 2.3 如何在代码里使用

定义 interface（含若干 modport）后，使用分两步：

1. **在顶层声明实例**：`IfComToFifo dcom_fifo();` —— 实例名 `dcom_fifo`，括号里目前为空。
2. **在模块例化时指定 modport**：`.dfifo ( dcom_fifo.mp_com )` —— 把实例 `dcom_fifo` 的 `mp_com` 视图接到模块的 `dfifo` 端口上。

如果一个 interface 端口**不指定 modport**，模块会把所有信号当成无方向限制的双向线，容易出错。pcileech-fpga 的做法是：每个使用 interface 的端口都明确写出 `.modport名`，让方向在编译期就被强制。

### 2.4 AXIS 握手（先记住一句话）

`IfAXIS128` 遵循 AXI-Stream（AXIS）握手约定：源端（source）拉高 `tvalid` 表示「数据有效」，宿端（sink）拉高 `tready` 表示「我准备好收」。只有当两者在同一时钟上升沿**同时为高**，这一拍数据才算真正传过去：

\[ \text{transfer}_n = \text{tvalid}_n \,\wedge\, \text{tready}_n \]

理解了这个「与」关系，后面看 `tready`/`has_data` 的方向就不会混淆。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，外加两个「使用方」来佐证。

| 文件 | 作用 |
| --- | --- |
| `PCIeSquirrel/src/pcileech_header.svh` | **核心**。所有 interface 的定义集中于此，是被各设备工程共享的「契约总表」。 |
| `PCIeSquirrel/src/pcileech_squirrel_top.sv` | 顶层。声明 5 个 interface 实例，并把它们以不同 modport 接到三大子系统上。 |
| `PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv` | TLP 处理模块。内部大量例化 `IfAXIS128`，是观察 `source`/`sink`/`source_lite`/`sink_lite` 真实用法的最佳样本。 |

> 说明：`pcileech_header.svh` 是被多设备复用的公共头文件（见 u1-l2），所以本讲虽然以 PCIeSquirrel 为例，结论对其他设备同样成立。

## 4. 核心概念与源码讲解

### 4.1 IfComToFifo：通信核心与路由中枢的契约

#### 4.1.1 概念说明

`IfComToFifo` 是最简单的一个接口，连接 **com**（USB3 通信核心）与 **fifo**（路由与控制中枢）。它只有两个方向的数据通路：

- **下行**（主机 → FPGA 内部）：64 位数据，每拍一个 64 位字，外加一个有效标志。
- **上行**（FPGA 内部 → 主机）：256 位数据，由 fifo 打包后整包交给 com 发出去。

它是学习 interface/modport 的最佳入口，因为信号少、方向干净，又能完整体现「同一根线两端方向相反」。

#### 4.1.2 核心流程

```text
下行（主机进来）:  com 模块  --com_dout[63:0]/com_dout_valid-->  fifo 模块
上行（发回主机）:  fifo 模块  --com_din[255:0]/com_din_wr_en-->   com 模块
                                                              <--com_din_ready--  (反压握手)
```

- `com_dout` / `com_dout_valid`：com 把从 USB 收到的数据**输出**给 fifo。
- `com_din` / `com_din_wr_en`：fifo 把要发回主机的数据**输出**给 com，`com_din_wr_en` 是写使能。
- `com_din_ready`：com 反过来告诉 fifo「我现在能不能收这 256 位」，是上行的反压信号。

注意方向成对：下行一进一出，上行也是一进一出，没有悬空信号。

#### 4.1.3 源码精读

接口定义见 [pcileech_header.svh:19-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L35)。关键部分：

```systemverilog
interface IfComToFifo;
    wire [63:0]     com_dout;
    wire            com_dout_valid;
    wire [255:0]    com_din;
    wire            com_din_wr_en;
    wire            com_din_ready;

    modport mp_com (
        output com_dout, com_dout_valid, com_din_ready,
        input  com_din, com_din_wr_en
    );

    modport mp_fifo (
        input  com_dout, com_dout_valid, com_din_ready,
        output com_din, com_din_wr_en
    );
endinterface
```

- `mp_com`（com 侧视角）：**输出** `com_dout*`（下行数据）和 `com_din_ready`（上行反压），**输入** `com_din*`（上行数据）。
- `mp_fifo`（fifo 侧视角）：方向**完全相反**。同一根 `com_din`，对 com 是 `input`，对 fifo 是 `output`——这正是 modport 的核心价值。

顶层里它的实例化和接线见 [pcileech_squirrel_top.sv:67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L67)（声明实例 `dcom_fifo`），com 模块以 `mp_com` 接入（[第 106 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L106)），fifo 模块以 `mp_fifo` 接入（[第 134 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L134)）。两端 modport 名不同、方向互补，契约就此成立。

#### 4.1.4 代码实践

**实践目标**：亲手验证「同一信号在两个 modport 里方向相反」。

**操作步骤**：

1. 打开 [pcileech_header.svh:19-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L35)。
2. 在纸上画一张 2 列表格，左列写 `mp_com`，右列写 `mp_fifo`。
3. 对 5 个信号 `com_dout`、`com_dout_valid`、`com_din`、`com_din_wr_en`、`com_din_ready`，分别在两列里填 `input` 或 `output`。

**预期结果**：每一行左右两列必然**相反**（一个 `input` 一个 `output`）。如果出现某信号在两边都是 `output`，说明接口定义有 bug——而 modport 机制正是让这种错误在综合阶段就被报出来。

> 待本地验证：若你已按 u1-l3 生成 Vivado 工程，可尝试把顶层 [第 106 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L106) 的 `dcom_fifo.mp_com` 故意改成 `dcom_fifo.mp_fifo`，重新综合，观察 Vivado 是否因「多驱动」报错。

#### 4.1.5 小练习与答案

**Q1**：为什么 `com_din_ready` 在 `mp_com` 里是 `output`，而在 `mp_fifo` 里是 `input`？

> **答**：`com_din_ready` 表示「com 这一侧是否准备好接收上行 256 位数据」。它由 com 模块根据自身状态产生并驱动，所以对 com 是输出；fifo 作为发送方只能读取它来决定要不要发，所以对 fifo 是输入。

**Q2**：下行通路（`com_dout`）为什么**没有**对应的 `ready` 反压信号？

> **答**：这是一个简化设计——fifo 侧假定总能及时消化 com 送来的下行数据（fifo 内部有缓冲 FIFO 兜底），所以省略了下行反压。代价是若 fifo 长期不取数，下行数据可能被覆盖；这是工程在「简单」与「健壮」之间的取舍。

---

### 4.2 IfPCIeFifoTlp：TLP 四路并行的收发契约

#### 4.2.1 概念说明

`IfPCIeFifoTlp` 连接 **fifo** 与 **PCIe TLP 处理子模块**，专门承载 PCIe 事务层包（TLP）的收发。它的特殊之处在于：**接收方向有 4 路并行通道**。这是因为 PCIe 一次发来的数据可能要分流到多个处理路径（如过滤、配置影子、BAR 控制等），用 4 个独立的 32 位通道并行传递，避免单通道成为瓶颈。

#### 4.2.2 核心流程

```text
发送（fifo -> PCIe 核）: tx_data[31:0] / tx_valid / tx_last  （单路 32 位，带包尾标志）
接收（PCIe 核 -> fifo）: rx_data[4][31:0] / rx_valid[4] / rx_first[4] / rx_last[4]  （4 路并行）
反压: rx_rd_en  （fifo 告诉 PCIe：可以往这 4 路里推数据了）
```

- `tx_data`/`tx_valid`/`tx_last`：fifo 要发给 PCIe 核的原始 TLP，`tx_last` 标记一个包的最后一个 32 位字。
- `rx_data[4]` 等：注意带 `[4]`，这是 SystemVerilog 的**数组信号**，相当于 4 组同型信号并排。每路各有自己的 `valid`/`first`/`last`。
- `rx_rd_en`：fifo 侧给出的读使能，控制 PCIe 侧何时推进接收数据。

#### 4.2.3 源码精读

定义见 [pcileech_header.svh:220-239](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L239)：

```systemverilog
interface IfPCIeFifoTlp;
    wire    [31:0]      tx_data;
    wire                tx_last;
    wire                tx_valid;   
    wire    [31:0]      rx_data[4];
    wire                rx_first[4];
    wire                rx_last[4];
    wire                rx_valid[4];
    wire                rx_rd_en;

    modport mp_fifo (
        output tx_data, tx_last, tx_valid, rx_rd_en,
        input  rx_data, rx_first, rx_last, rx_valid
    );

    modport mp_pcie (
        input  tx_data, tx_last, tx_valid, rx_rd_en,
        output rx_data, rx_first, rx_last, rx_valid
    );
endinterface
```

要点：

- `rx_data[4]` 这种「带维度的 wire」是**数组 of wire**，在 modport 里 `input rx_data` 会把整个 4 路数组一起声明为输入，不用逐路列举。
- `mp_fifo` 把所有 `rx_*` 当输入、把 `tx_*` 和 `rx_rd_en` 当输出；`mp_pcie` 正好相反。方向互补原则依旧。
- 顶层中 `dtlp` 实例见 [pcileech_squirrel_top.sv:71](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L71)，fifo 用 `dtlp.mp_fifo`（[第 137 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L137)），pcie 模块用 `dtlp.mp_pcie`（[第 161 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L161)）。

#### 4.2.4 代码实践

**实践目标**：理解「数组信号」如何让一份 modport 描述覆盖多路通道。

**操作步骤**：

1. 阅读 [pcileech_header.svh:224-228](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L224-L228)，数一下接收方向一共声明了几个带 `[4]` 的信号。
2. 设想若不用数组、要把这 4 路展开成普通 wire，`mp_pcie` 的 `output` 列表会膨胀到多少项。
3. 在 `pcileech_fifo.sv` 里搜索 `dtlp.rx_data` 或 `rx_data[`，观察代码如何用下标 `[0]`..`[3]` 分别访问每一路。

**预期结果**：4 个数组信号（`rx_data/rx_first/rx_last/rx_valid`），展开后 `mp_pcie` 至少要写 \(4 \times 4 = 16\) 项 output。数组写法把这一长串压缩成 4 项，可读性大幅提升。

#### 4.2.5 小练习与答案

**Q1**：`tx_last` 和 `rx_last[4]` 各自标记什么？

> **答**：`tx_last` 标记**发送方向**当前 32 位字是某个 TLP 的最后一个字；`rx_last[i]` 标记**接收第 i 路**当前字是该路某个包的最后一个字。它们都是「包边界」标记，便于接收方知道一个包结束了。

**Q2**：为什么 `rx_rd_en` 是单根线（不带 `[4]`），而 `rx_valid` 是 `[4]`？

> **答**：`rx_rd_en` 是 fifo 对「整个接收子系统」给出的统一读使能，4 路共享同一节拍；而每路数据是否有效是独立的（4 路可能并不同步有数据），所以 `rx_valid` 必须每路一根。这是一个「控制统一、数据分散」的典型设计。

---

### 4.3 IfAXIS128：128 位 AXIS 数据流与 source/sink

#### 4.3.1 概念说明

`IfAXIS128` 是本讲最重要的接口，也是 pcileech-fpga 内部 TLP 处理子模块之间**最常用**的连接方式。它遵循 AXI-Stream 协议，每次传一个 **128 位**（4 个 DWORD）的数据拍（beat）。它的两个核心 modport 直接命名为 **`source`（源/生产者）** 和 **`sink`（宿/消费者）**，是教科书式的握手模型。

此外它还提供了 **`source_lite`/`sink_lite`** 两个「精简版」modport——去掉握手信号 `tready`/`has_data`，用于不需要反压的链路，省去流控逻辑。

#### 4.3.2 核心流程

一次完整的 AXIS 数据传输：

```text
source 端:  准备好数据 -> 拉高 tvalid -------------------------> 
            (数据放在 tdata，tkeepdw 标有效 dword，
             tlast=1 表示包尾，tuser 标 first/last/BAR)
sink 端:    有空位 -> 拉高 tready <---------------------------
            ↓ 同一拍 tvalid && tready 都为 1 ↓
            -> 采到这一拍 128 位数据
```

握手条件就是上一节给出的公式：\[ \text{transfer}_n = \text{tvalid}_n \,\wedge\, \text{tready}_n \]。

`has_data` 是 source 提供给 sink 的辅助指示（「我这边还有数据要发」），便于 sink 提前做调度决策，与 `tvalid` 配合使用。

#### 4.3.3 源码精读

定义见 [pcileech_header.svh:165-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)：

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

    modport source(
        input  tready,
        output tdata, tkeepdw, tvalid, tlast, tuser, has_data
    );

    modport sink(
        output tready,
        input  tdata, tkeepdw, tvalid, tlast, tuser, has_data
    );

    modport source_lite(
        output tdata, tkeepdw, tvalid, tlast, tuser
    );

    modport sink_lite(
        input  tdata, tkeepdw, tvalid, tlast, tuser
    );
endinterface
```

**`tuser` 字段的精妙之处**：它用 9 位把三件事打包在一起——

- `tuser[0] = first`：当前拍是某个包的**第一拍**。
- `tuser[1] = last`：当前拍是某个包的**最后一拍**（与独立的 `tlast` 信号冗余备份，便于不同消费者取用）。
- `tuser[8:2]`：**命中的 BAR 编号**，按位对应：bit2=BAR0、bit3=BAR1、…、bit7=BAR5、bit8=EXPROM（扩展 ROM）。这是一个 one-hot 风格的指示，告诉接收方「这一拍数据落在哪个 BAR 空间」。

> 为什么要把 first/last/BAR 塞进 `tuser`？因为 PCIe 的一个 TLP 可能跨多拍（128 位=4 DWORD，但一个包可能 16 DWORD），接收方需要包头/包尾标记来切包；同时不同 BAR 的处理逻辑不同（BAR0 可能是配置区，BAR2 可能是 DMA 区），所以必须把「命中哪个 BAR」随数据一起传。用 `tuser` 携带这些元信息，是 AXIS 协议的标准用法。

**真实使用样例**——在 TLP 模块里，`IfAXIS128` 实例随处可见（[pcileech_pcie_tlp_a7.sv:27-33](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L27-L33)、[第 74 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L74)）。最典型的是「一个模块以 `source` 产出响应、另一个模块以 `sink` 消费」：

```systemverilog
// bar_controller 产出 BAR 读响应 -> 作为 source
.tlps_out ( tlps_bar_rsp.source )          // 见 pcileech_pcie_tlp_a7.sv:41

// cfgspace_shadow 产出配置空间读响应 -> 作为 source
.tlps_cfg_rsp ( tlps_cfg_rsp.source )      // 见 pcileech_pcie_tlp_a7.sv:51

// sink_mux1 把多路响应汇成一路 -> 各输入端是 sink
.tlps_in1 ( tlps_cfg_rsp.sink )            // 见 pcileech_pcie_tlp_a7.sv:90
.tlps_in2 ( tlps_bar_rsp.sink )            // 见 pcileech_pcie_tlp_a7.sv:91
.tlps_in3 ( tlps_rx_fifo.sink )            // 见 pcileech_pcie_tlp_a7.sv:92
```

可以看到：同一个实例 `tlps_cfg_rsp`，在 `cfgspace_shadow` 那一头是 `source`（驱动 tdata 等），在 `sink_mux1` 这一头是 `sink`（驱动 tready）——又一次完美演示「一根电缆两头插头相反」。

`source_lite`/`sink_lite` 的样例见过滤模块与 dst_fifo 之间（[第 60 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L60) `source_lite`、[第 67 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L67) `sink_lite`），这条链路确定下游总能及时收，所以省掉了 `tready`/`has_data`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手为 `IfAXIS128` 整理一份完整的信号说明表，并解释 `source` 与 `sink` 的区别。这是本讲的核心练习。

**操作步骤**：

1. 打开 [pcileech_header.svh:165-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)。
2. 针对下表列出的每个信号，自己先填「位宽」和「在 source/sink 中的方向」，再对照下方参考答案核对。
3. 用一句话写下 `source` 和 `sink` 的根本区别。

**参考答案表（信号说明表）**：

| 信号 | 位宽 | source 方向 | sink 方向 | 含义 |
| --- | --- | --- | --- | --- |
| `tdata` | 128 | output | input | 数据本体，一拍 4 个 DWORD |
| `tkeepdw` | 4 | output | input | DWORD 使能，每位对应 `tdata` 中一个 DWORD 是否有效 |
| `tvalid` | 1 | output | input | 源端声明「本拍数据有效」 |
| `tlast` | 1 | output | input | 本拍是某个包的最后一拍 |
| `tuser` | 9 | output | input | 元信息：[0]=first、[1]=last、[8:2]=命中 BAR（bit2..8 = BAR0..5,EXPROM） |
| `tready` | 1 | input | output | 宿端声明「我准备好接收」 |
| `has_data` | 1 | output | input | 源端辅助指示「还有数据要发」，配合 `tvalid` 做调度 |

**`source` 与 `sink` 的区别**：

- **`source`（生产者）**：驱动所有**数据类**信号（`tdata/tkeepdw/tvalid/tlast/tuser/has_data` 都是 output），只接收一个反压信号 `tready`（input）。它决定「发什么、何时发」。
- **`sink`（消费者）**：反过来，驱动 `tready`（output），读取所有数据类信号（input）。它决定「何时收」。
- 两者方向严格互补，握手成立的前提是 `tvalid`（source 给）与 `tready`（sink 给）同时为高。
- **`source_lite`/`sink_lite`**：去掉 `tready` 和 `has_data` 的无反压精简版，仅保留 5 个数据类信号；适合「下游永远来得及收」的内部短链路，逻辑更简单、资源更省。

#### 4.3.5 小练习与答案

**Q1**：若某条 AXIS 链路两端**都不**指定 modport（直接写 `.port( tlps_xxx )`），会发生什么风险？

> **答**：两端都看不到方向限制，理论上任一端都可能驱动任一信号。如果两边代码都赋值了 `tdata`，就会形成多驱动短路；即使没短路，也失去了 modport 在编译期的方向保护，错误只能留到仿真或上板才暴露。pcileech-fpga 始终显式写 modport，正是为了避免这种情况。

**Q2**：`tuser[8:2]` 表示 BAR，bit8 对应 EXPROM。一个落在 BAR2 上的 TLP，`tuser` 应该是多少（只看 [8:2] 段，其余位先记 0）？

> **答**：BAR2 对应 bit4（因为 bit2=BAR0, bit3=BAR1, bit4=BAR2）。所以 `tuser[8:2] = 7'b0000100`，即 `tuser[4]=1`，其余 BAR 位为 0。换算成 9 位 `tuser`，至少 bit4=1。

**Q3**：为什么要有 `has_data`，既然已经有 `tvalid`？

> **答**：`tvalid` 是**逐拍**的「本拍有效」，可能在包间隙被拉低；而 `has_data` 是**更宏观**的「我还有后续数据」，让 sink（尤其是多路复用器）能提前规划轮询和带宽分配，不必等到 `tvalid` 真拉高才反应。两者粒度不同、配合使用。

---

### 4.4 IfShadow2Fifo：配置空间影子与 BAR 控制的契约

#### 4.4.1 概念说明

`IfShadow2Fifo` 是「较新设备」才有的接口（见 u1-l2 的代际分界），连接 **fifo** 与 **配置空间影子 / BAR 控制器**（它们位于 PCIe 子系统内部）。它的职责有两块：

1. **主机对配置空间影子的读写**：主机经 USB→fifo 下发命令，通过这个接口去读写那块「自定义配置空间 BRAM」。
2. **各种过滤/使能开关**：`cfgtlp_en`、`bar_en`、`alltlp_filter` 等控制位，由 fifo 写入寄存器后，经此接口送给 PCIe 侧的 TLP 处理模块。

信号多达 16 个，是本讲里最「宽」的契约。

#### 4.4.2 核心流程

```text
fifo 侧 (modport fifo) -- 驱动 --> shadow 侧 (modport shadow):
   读/写命令: rx_rden / rx_wren / rx_be / rx_data / rx_addr / rx_addr_lo
   控制开关: cfgtlp_wren / cfgtlp_zero / cfgtlp_en / cfgtlp_filter / alltlp_filter / bar_en

shadow 侧 -- 驱动 --> fifo 侧:
   读回数据: tx_valid / tx_data / tx_addr / tx_addr_lo
```

- `rx_*` 是「fifo → shadow」方向的命令（读使能、写使能、字节使能、数据、地址），shadow 据此操作 BRAM。
- `tx_*` 是「shadow → fifo」方向的**读回结果**（shadow 把读出的数据送回 fifo，最终回主机）。
- `cfgtlp_*` / `alltlp_filter` / `bar_en` 是一组**功能开关**：决定要不要处理配置 TLP、要不要把 TLP 过滤丢弃、要不要启用 BAR 设备仿真。这些开关的源头是 fifo 里的 rw 寄存器（见 u2-l5）。

#### 4.4.3 源码精读

定义见 [pcileech_header.svh:267-295](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L267-L295)：

```systemverilog
interface IfShadow2Fifo;
    wire                rx_rden;
    wire                rx_wren;
    wire    [3:0]       rx_be;
    wire    [31:0]      rx_data;
    wire    [9:0]       rx_addr;
    wire                rx_addr_lo;
    wire                tx_valid;
    wire    [31:0]      tx_data;
    wire    [9:0]       tx_addr;
    wire                tx_addr_lo;
    wire                cfgtlp_wren;
    wire                cfgtlp_zero;
    wire                cfgtlp_en;
    wire                cfgtlp_filter;
    wire                alltlp_filter;
    wire                bar_en;

    modport fifo (
        output cfgtlp_wren, cfgtlp_zero, rx_rden, rx_wren, rx_be, rx_addr, rx_addr_lo,
               rx_data, cfgtlp_en, cfgtlp_filter, alltlp_filter, bar_en,
        input  tx_valid, tx_addr, tx_addr_lo, tx_data
    );

    modport shadow (
        input  cfgtlp_wren, cfgtlp_zero, rx_rden, rx_wren, rx_be, rx_addr, rx_addr_lo,
               rx_data, cfgtlp_en, cfgtlp_filter, alltlp_filter, bar_en,
        output tx_valid, tx_addr, tx_addr_lo, tx_data
    );
endinterface
```

要点：

- `fifo` modport：**输出**所有命令与开关（`rx_*`、`cfgtlp_*`、`*_filter`、`bar_en`），**输入**读回结果（`tx_*`）。说明 fifo 是「发号施令」的一方。
- `shadow` modport：方向完全相反——接收命令、回送数据。它是「执行者」。
- `rx_addr[9:0]` + `rx_addr_lo`：地址分成高位（10 位 dword 地址）和低位（选 dword 内的字节/字），这是配置空间按 DWORD 寻址的典型拆分。
- 顶层中 `dshadow2fifo` 实例见 [pcileech_squirrel_top.sv:73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L73)，fifo 用 `dshadow2fifo.fifo`（[第 139 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L139)），pcie 模块用 `dshadow2fifo.shadow`（[第 163 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L163)）。

> 这里的 modport 名不再是 `mp_*` 而是 `fifo` / `shadow`，命名风格不同但本质一样：都是「从某一侧看进去的方向视图」。

#### 4.4.4 代码实践

**实践目标**：从契约反推「谁是主、谁是从」，并定位功能开关的真正源头。

**操作步骤**：

1. 阅读 [pcileech_header.svh:286-294](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L286-L294)，统计 `modport fifo` 里 `output` 有几个、`input` 有几个。
2. 由于 `bar_en`、`cfgtlp_en` 等是 fifo 的 output，它们的值必然在 fifo 模块内部被赋值。在 `pcileech_fifo.sv` 中搜索 `dshadow2fifo.bar_en` 或 `bar_en`，找到驱动它的寄存器位。
3. 在 PCIe 侧的 `pcileech_tlps128_bar_controller` 例化处（[pcileech_pcie_tlp_a7.sv:38](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L38)）确认 `bar_en` 是作为 input 接入的。

**预期结果**：`modport fifo` 有 12 个 output、4 个 input（`tx_*`），可见 fifo 是命令发出方、shadow 是执行方。`bar_en` 由 fifo 的某个 rw 寄存器位驱动，经 `dshadow2fifo` 这条「电缆」一路传到 bar_controller，决定 BAR 设备仿真是否启用。这就把「主机写 rw 寄存器 → fifo 驱动 interface → PCIe 侧行为改变」这条控制链打通了。

> 待本地验证：若你已打开 `pcileech_fifo.sv`，可定位到 `_pcie_core_config` 相关赋值段，确认 `bar_en` 是否来自某个 rw 寄存器位（具体位布局见 u2-l5）。

#### 4.4.5 小练习与答案

**Q1**：为什么 `rx_addr` 是 10 位、还要配一个 `rx_addr_lo`？

> **答**：配置空间按 **DWORD**（32 位）为基本单位编址，`rx_addr[9:0]` 选 1024 个 DWORD 之一（覆盖 4KB 配置空间）。但有时候命令需要更细粒度（比如写单个字节/字），`rx_addr_lo` 配合 `rx_be`（字节使能）来指定 DWORD 内的更小单位。这是一种「粗地址 + 细使能」的二级寻址。

**Q2**：`cfgtlp_zero` 这个开关最可能控制什么？

> **答**：从名字（zero）和它属于 fifo→shadow 方向的输出看，它很可能控制「配置 TLP 处理时是否把数据按零填充」（例如把读到的配置内容清零，用于隐藏真实配置）。这正是 u4-l1「自定义配置空间影子」会深入讨论的内容——本讲只需记住：这类开关通过 `IfShadow2Fifo` 从 fifo 传到 PCIe 侧。

**Q3**：`tx_*`（读回数据）为什么是 `modport fifo` 的 input？

> **答**：当主机想读配置空间时，fifo 发出 `rx_rden` 命令，shadow 从 BRAM 取出数据后用 `tx_data/tx_valid` 回送。这些数据对 fifo 而言是「收到的结果」，所以是 input；fifo 再把它转交回 com，最终经 USB 回到主机。

---

## 5. 综合实践

**任务：绘制 pcileech_squirrel_top 的「interface 接线总图」并做一次方向自检。**

把本讲四个接口（外加第 5 个 `IfPCIeFifoCfg`/`IfPCIeFifoCore`，它们和 `IfPCIeFifoTlp` 结构同构，可作为延伸）串起来，完成下面三步：

1. **画图**：在一张大图中央画 `fifo` 模块，左边画 `com`，右边画 `pcie_a7`（右侧再细分为 cfg/tlp/shadow 等子模块）。用 5 条粗线代表 5 个 interface 实例（`dcom_fifo`、`dcfg`、`dtlp`、`dpcie`、`dshadow2fifo`）把它们连起来，每条线上标注用到的 modport 名（如 `dcom_fifo`: `mp_com` ↔ `mp_fifo`）。可参考顶层 [pcileech_squirrel_top.sv:67-73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L67-L73) 的实例声明与 [第 106-163 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L98-L164) 的例化接线。

2. **方向自检**：对每条 interface，挑一根信号，确认它在「fifo 侧 modport」和「对端 modport」里方向**相反**。例如：
   - `dcom_fifo.com_din`：fifo 侧 `mp_fifo`=output，com 侧 `mp_com`=input ✓
   - `dtlp.rx_rd_en`：fifo 侧 `mp_fifo`=output，pcie 侧 `mp_pcie`=input ✓
   - `dshadow2fifo.bar_en`：fifo 侧 `fifo`=output，pcie 侧 `shadow`=input ✓

3. **故障推演**：回答——如果有人把 pcie_a7 实例的 `.dfifo_cfg ( dcfg.mp_pcie )` 误写成 `.dfifo_cfg ( dcfg.mp_fifo )`，会发生什么？

   > **参考答案**：此时 pcie_a7 会拿到 fifo 的 modport 视图，`tx_data`（原本对 pcie 是 input）变成了它要驱动的 output，而 fifo 侧也在驱动 `tx_data`——同根线两个驱动者，综合会报 multi-driver 错误；即使侥幸不报，功能也完全错乱。这就是 modport「契约」的保护意义：插错插头，编译期就该被发现。

完成这张图后，pcileech-fpga 顶层「三大子系统如何对话」的全貌就清晰了，后续 u2-l2（com）、u2-l3（fifo）都能在这张图上找到对应位置。

## 6. 本讲小结

- **interface** 把一组相关信号打包成「粗电缆」，**modport** 从某一侧的视角规定每根信号的方向，二者合起来构成模块间的「连接契约」，根治了传统 Verilog 端口列表重复易错的问题。
- **`IfComToFifo`** 是最简契约：64 位下行 + 256 位上行 + 一个反压位，两端 modport（`mp_com`/`mp_fifo`）方向严格互补。
- **`IfPCIeFifoTlp`** 用**数组信号** `rx_*[4]` 表达 4 路并行接收通道，一份 modport 即覆盖多路，控制（`rx_rd_en`）统一、数据分散。
- **`IfAXIS128`** 是 AXI-Stream 风格的 128 位数据流接口，核心是 `source`/`sink` 生产者—消费者模型，握手成立条件为 `tvalid && tready`；`tuser` 用 9 位同时编码 first/last/BAR；另有 `source_lite`/`sink_lite` 精简版省去反压。
- **`IfShadow2Fifo`** 是最宽的契约（16 信号），承载 fifo 对配置空间影子的读/写命令与各类功能开关（`bar_en`/`cfgtlp_en` 等），把「主机写寄存器 → PCIe 行为改变」的控制链打通；其 modport 命名为 `fifo`/`shadow`。
- 所有接口的 modport 都遵循「方向互补」铁律：同一信号在两端 modport 中必为一 `input` 一 `output`，这是综合期能查错的根本保证。

## 7. 下一步学习建议

- **承接 u2-l2（FT601 USB3 通信核心）**：进入 `pcileech_com.sv` 与 `pcileech_ft601.sv`，看它们如何用 `IfComToFifo`（`mp_com` 侧）真正收发数据，把本讲的「下行 64 位 / 上行 256 位」契约落到具体状态机上。
- **承接 u2-l3（FIFO 控制中心与 MAGIC 路由）**：这是本讲所有 interface 的「中央枢纽」。重点看 `pcileech_fifo.sv` 如何以 `mp_fifo`/`fifo` 视图同时对接 com、cfg、tlp、pcie、shadow 五个方向——你会发现整张本讲的接线图都汇聚到这里。
- **延伸阅读**：本讲未展开的 `IfPCIeFifoCfg`（[pcileech_header.svh:199-215](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L199-L215)）、`IfPCIeFifoCore`（[第 244-265 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265)）结构与 `IfPCIeFifoTlp` 同构，可自行用本讲的方法独立分析；它们会在 u3 单元（PCIe 核心）被反复用到。
