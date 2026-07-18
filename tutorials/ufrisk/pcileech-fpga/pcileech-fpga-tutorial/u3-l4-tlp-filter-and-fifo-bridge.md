# TLP 过滤与 FIFO 桥接

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 PCIe TLP 头部里 **Fmt/Type** 字段的位位置，并解释为什么过滤器只比较 `tdata[31:25]` 这 7 位就能识别 `Cpl / CplD / CfgRd / CfgWr`。
- 读懂 `pcileech_tlps128_filter` 的「按包头判定 → 状态锁存 → 逐拍丢弃」过滤流程，并说明 `alltlp_filter`、`cfgtlp_filter` 两个开关各自的丢弃策略。
- 读懂 `pcileech_tlps128_dst_fifo` 如何把 128 位 AXIS 流拆成 4 路 32 位，并安全地从 `clk_pcie` 时钟域跨到 `clk_sys` 时钟域。
- 读懂 `pcileech_tlps128_src_fifo` 如何把主机侧 4 路 32 位流拼回 128 位，并用「包计数器 + 双 FIFO」实现低延迟的跨时钟域包 FIFO。

本讲承接 [u3-l3 TLP 处理总览](u3-l3-tlp-handling-overview.md)：那里把 `pcileech_pcie_tlp_a7` 当作一个「黑盒调度枢纽」，本讲打开它在接收方向上的「过滤 + 下行桥接」、在发送方向上的「上行桥接」这三段管道的内壁。

## 2. 前置知识

### 2.1 TLP 头部的第一个 DWORD

PCIe 的事务层包（TLP，Transaction Layer Packet）头部第一个 32 位字（DW0）的位布局是：

| 位字段 | 含义 |
|---|---|
| `[31:29]` | **Fmt[2:0]**：格式。指出头部长度（3DW/4DW）以及是否带数据。例如 `000`=3DW 无数据，`010`=3DW 带数据。 |
| `[28:24]` | **Type[4:0]**：类型。指出这是 Memory / IO / Configuration / Completion 等哪一类事务。 |
| `[23:]` | TC（流量类别）、属性等，本讲不关心。 |

把 Fmt 和 Type 合起来就能唯一确定 TLP 种类，例如：

- **CplD**（带数据的完成包）：`Fmt=010`、`Type=0101x`（`x` 是 Type 的最低位，区分大小）。
- **Cpl**（无数据完成包）：`Fmt=000`、`Type=0101x`。
- **CfgWr**（配置写）：`Fmt=010`、`Type=0010x`。
- **CfgRd**（配置读）：`Fmt=000`、`Type=0010x`。

本讲的过滤器只用 `Fmt[2:0]` 加上 `Type` 的**高 4 位** `Type[4:1]`，也就是 `tdata[31:25]` 这 7 位来匹配，故意忽略 Type 的最低位 `Type[0]`——这样一个匹配值就能同时覆盖 `CfgRd0/CfgRd1`、`Cpl/CplD` 的两种小变体。

### 2.2 AXIS（AXI-Stream）流与 `IfAXIS128`

TLP 在本工程里以 128 位 AXI-Stream 流的形式流动。回顾 [u2-l1](u2-l1-interfaces-and-modports.md) 讲过的 [`IfAXIS128`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)：一个 128 位包由若干「拍（beat）」组成，每拍承载 4 个 DWORD。关键字段：

- `tdata[127:0]`：数据，一拍 4 个 DWORD。
- `tkeepdw[3:0]`：每个 DWORD 的有效位（类似 AXI 的 `strb`）。
- `tlast`：本拍是该包的最后一拍。
- `tuser[8:0]`：`[0]=first`（包的首拍）、`[1]=last`、`[8:2]=BAR 命中位`。
- `tvalid / tready / has_data`：握手与反压。

它有两套 modport：带反压的 `source`/`sink`，与不带反压、单向推送的 `source_lite`/`sink_lite`。本讲的过滤器两端都用 `*_lite`（上游保证不丢，无需反压），而桥接 FIFO 在面向硬核侧用 `source`/`sink`（可能被反压）。

### 2.3 为什么需要跨时钟域

工程里至少有两个相关时钟域（详见 [u5-l1](u5-l1-clock-domain-crossing.md)）：

- `clk_pcie`：PCIe 硬核给出的用户时钟，TLP 在这侧流动。
- `clk_sys`：系统/主机侧时钟（与 fifo、FT601 通路对齐）。

二者异步，必须用**双时钟 FIFO**（dual-clock FIFO，`*_clk2` 系列 IP）才能安全地把数据从一个域搬到另一个域。本讲的 `dst_fifo` 与 `src_fifo` 正是两座这样的「桥」。

## 3. 本讲源码地图

本讲只读一个源文件，但它的三个子模块各司其职：

| 子模块 | 源码行 | 方向 | 职责 |
|---|---|---|---|
| `pcileech_tlps128_filter` | [L156–L198](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L156-L198) | 接收 | 按 Fmt/Type 丢弃不要的 TLP |
| `pcileech_tlps128_dst_fifo` | [L104–L148](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L104-L148) | 接收（出 FPGA） | 128 位 → 4×32 位，`clk_pcie`→`clk_sys` |
| `pcileech_tlps128_src_fifo` | [L206–L284](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L206-L284) | 发送（入 FPGA） | 4×32 位 → 128 位，`clk_sys`→`clk_pcie` |

它们在父模块 [`pcileech_pcie_tlp_a7`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L13-L96) 里的位置：`tlps_rx`（来自硬核的接收流）先被三路并行消费——`bar_controller`、`cfgspace_shadow`、`filter`；其中 `filter` 的输出 `tlps_filtered` 喂给 `dst_fifo`，再出到主机。发送侧则由 `src_fifo` 把主机 32 位流拼成 128 位 `tlps_rx_fifo`，交给 `sink_mux1` 仲裁（仲裁细节见 [u3-l5](u3-l5-tlp-mux-and-width-conversion.md)）。

涉及的接口契约：[`IfAXIS128`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)、[`IfPCIeFifoTlp`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L239)、[`IfShadow2Fifo`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L267-L295)。

## 4. 核心概念与源码讲解

### 4.1 TLP 过滤器 `pcileech_tlps128_filter`

#### 4.1.1 概念说明

PCIe 链路上跑着各种各样的 TLP：主机发给设备的存储读写（MRd/MWr）、设备回主机的完成包（Cpl/CplD）、配置读写（CfgRd/CfgWr）等等。对于一块「DMA 采集卡」而言，并不是每一种都要原样转发给主机软件：

- **CfgRd/CfgWr** 往往由 `cfgspace_shadow` 子模块就地应答（见 [u4-l1](u4-l1-custom-cfgspace-shadow.md)），再转发一份给主机就多余甚至有害。
- 当设备工作在「只关心完成包」的采集模式时，把 Memory/IO 请求类 TLP 全部丢掉，可以省下宝贵的 USB 带宽。

`pcileech_tlps128_filter` 就是这道「门卫」：它根据每个包**首拍头部**的 Fmt/Type，结合两个主机下发的开关位 `cfgtlp_filter`、`alltlp_filter`，决定**整个包**是放行还是丢弃。注意它只做「丢或不丢」，不改包内容。

两个开关位来自 [`IfShadow2Fifo`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L282-L283)（`alltlp_filter`、`cfgtlp_filter`），即主机写 `pcileech_fifo` 的 `rw` 控制寄存器后，经 `dshadow2fifo` 传到这里（控制链见 [u2-l5](u2-l5-command-register-file.md)）。

#### 4.1.2 核心流程

过滤分三步，关键是「首拍判定 + 状态锁存」：

1. **取包头标志**：`first = tlps_in.tuser[0]`，标识本拍是某包的第一拍（含头部 DW0）。
2. **首拍识别种类**：只在 `first` 为真时，用 `tdata[31:25]` 这 7 位判断是不是 `Cpl/CplD`（`is_tlphdr_cpl`）或 `CfgRd/CfgWr`（`is_tlphdr_cfg`）。
3. **决定下一拍是否丢弃** `filter_next`，三条触发条件（见下方源码），其中第一条 `(filter && !first)` 让「已经决定要丢的包」的后续非首拍继续被丢，直到包结束。
4. **寄存输出**：数据通路整体打一拍（1 周期延迟），但 `tvalid` 被 `!filter_next` 闸门关掉——被丢的拍 `tvalid=0`，下游自然忽略。

判定真值表：

| `cfgtlp_filter` | `alltlp_filter` | 包首拍种类 | 结果 |
|:---:|:---:|---|---|
| 1 | × | CfgRd/CfgWr | 丢弃整包 |
| × | 1 | 非 Cpl/CplD 且非 Cfg（如 MRd/MWr） | 丢弃整包 |
| × | × | Cpl/CplD | 总是放行（完成包是采集的核心数据） |

#### 4.1.3 源码精读

先看种类识别。注意两个常量各覆盖「带数据 / 不带数据」两种 Fmt，且 `==` 右值只有 7 位，对应 `Fmt[2:0] ++ Type[4:1]`：

```verilog
wire first = tlps_in.tuser[0];
wire is_tlphdr_cpl = first && (
                    (tlps_in.tdata[31:25] == 7'b0000101) ||   // Cpl:  Fmt=000, Type=0101x
                    (tlps_in.tdata[31:25] == 7'b0100101)     // CplD: Fmt=010, Type=0101x
                  );
wire is_tlphdr_cfg = first && (
                    (tlps_in.tdata[31:25] == 7'b0000010) ||   // CfgRd: Fmt=000, Type=0010x
                    (tlps_in.tdata[31:25] == 7'b0100010)     // CfgWr: Fmt=010, Type=0010x
                  );
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L178-L186`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L178-L186) — Cpl/CplD 与 CfgRd/CfgWr 的 7 位特征匹配。

再看核心的丢弃决策 `filter_next` 与寄存输出。三条触发条件用 `||` 串联，任一成立则「下一拍」进入丢弃态：

```verilog
wire filter_next = (filter && !first)                              // 包已在丢，且本拍不是新包首拍 → 继续丢
                || (cfgtlp_filter && first && is_tlphdr_cfg)       // 首拍是 Cfg 包且开关开 → 开始丢
                || (alltlp_filter && first && !is_tlphdr_cpl && !is_tlphdr_cfg); // 首拍既非完成包也非 Cfg 且开关开 → 开始丢

always @ ( posedge clk_pcie ) begin
    tdata   <= tlps_in.tdata;
    tkeepdw <= tlps_in.tkeepdw;
    tvalid  <= tlps_in.tvalid && !filter_next && !rst;   // 闸门：要丢的拍 tvalid 拉低
    tuser   <= tlps_in.tuser;
    tlast   <= tlps_in.tlast;
    filter  <= filter_next && !rst;                      // 锁存「正在丢一个包」的状态
end
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L187-L196`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L187-L196) — 丢弃决策与寄存输出。

要点：

- `filter` 是一个**跨拍状态寄存器**：一旦某包首拍被判定要丢，`filter` 置 1，之后该包所有非首拍都命中 `(filter && !first)` 继续被丢；直到下一个包的首拍到来，重新判定。
- 数据/`tkeepdw`/`tuser`/`tlast` 照常寄存穿透，**只有 `tvalid` 被强制拉低**——这是最省资源的「丢包」做法：下游看不到 `tvalid`，自然当这些拍不存在。
- 决策信号 `filter_next` 用的是**输入** `tlps_in.*`（组合逻辑），而 `filter` 寄存器提供历史状态，二者配合实现了「看一眼包头，决定整个包命运」。

#### 4.1.4 代码实践

> **实践目标**：亲手把 `is_tlphdr_cpl` / `is_tlphdr_cfg` 的判定条件翻译成 7 位匹配值，验证 CplD 与 CfgWr。

操作步骤：

1. 打开 [`pcileech_pcie_tlp_a7.sv#L178-L186`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L178-L186)。
2. 记住 7 位常量的位映射：`7'b b6 b5 b4 b3 b2 b1 b0` 分别对应 `tdata[31][30][29][28][27][26][25]`，即 `{Fmt[2:0], Type[4:1]}`。
3. 对 CplD（`Fmt=010, Type=0101x`）：取 `Fmt=010`、`Type` 的高 4 位 `=0101`，拼成 7 位。
4. 对 CfgWr（`Fmt=010, Type=0010x`）：取 `Fmt=010`、`Type` 的高 4 位 `=0010`，拼成 7 位。

需要观察的现象 / 预期结果（已在源码注释中给出，**待本地验证**你是否能独立推出）：

- **CplD** → `7'b0100101`
- **CfgWr** → `7'b0100010`

对照源码注释，二者完全一致。进一步思考：为什么 `Cpl`（无数据完成包）用的是 `7'b0000101`？因为它的 `Fmt=000`（3DW 头、无数据），只有 Type 与 CplD 相同。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `alltlp_filter` 设为 1、`cfgtlp_filter` 设为 0，一个从主机发往设备的 `MRd`（存储读，`Fmt=000, Type=00000`）经过过滤器会怎样？一个设备回给主机的 `CplD` 又会怎样？

**答案**：`MRd` 首拍 `is_tlphdr_cpl=0`、`is_tlphdr_cfg=0`，命中 `alltlp_filter && first && !cpl && !cfg`，整包被丢弃。`CplD` 命中 `is_tlphdr_cpl=1`，被「豁免」，整包放行——这正是「只采集完成包」模式。

**练习 2**：`filter` 寄存器的初值是 0（见 [`L177`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L177) `bit filter = 0;`）。若上电后第一个进来的就是一个「应该被丢」的包的非首拍（理论上不会发生，但假设），会发生什么？

**答案**：`filter=0`，`filter_next` 的第一项 `(filter && !first)` 为假，又因 `!first` 使另两项（都要求 `first`）也为假，故 `filter_next=0`，这一拍被放行。这说明过滤器**依赖「首拍先到」这一前提**——正常的 AXIS 流保证每个包都有 `tuser[0]=1` 的首拍，所以不会出问题；该设计把信任交给了上游 `tlps128_src64` 的成帧。

---

### 4.2 下行 FIFO 桥接 `pcileech_tlps128_dst_fifo`

#### 4.2.1 概念说明

`dst` = destination。这个模块把**已经被过滤、要送出 FPGA 给主机**的 128 位 TLP 流，搬到 `clk_sys` 域，并拆成 4 路并行的 32 位字，交给系统侧的 [`IfPCIeFifoTlp`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L239) 接口（对应 [u2-l4](u2-l4-output-mux-256bit.md) 里 `dtlp.rx_data[0..3]` 那四路并行接收通道）。

它解决两个问题：

1. **跨时钟域**：`clk_pcie → clk_sys`，用双时钟 FIFO `fifo_134_134_clk2`。
2. **位宽适配**：128 位 AXIS 一拍 = 4 个 DWORD，正好对应 `rx_data[0..3]` 四条 32 位车道。

#### 4.2.2 核心流程

1. **打包写入**：把每拍的有效信息 `{first(tuser[0]), tlast, tkeepdw[3:0], tdata[127:0]}` 拼成 134 位写入双时钟 FIFO，写时钟 `clk_pcie`，写使能 `tlps_in.tvalid`。
2. **跨域读出**：读时钟 `clk_sys`，读使能 `dfifo.rx_rd_en`（由主机侧 fifo 拉动）。FIFO 的 `valid` 指示本拍 `dout` 有效。
3. **拆分到 4 车道**：把读回的 128 位 `tdata` 拆成 `rx_data[0..3]`；用 `tkeepdw` 决定每条车道是否有效（`rx_valid[i]`）、用 `tlast` 决定哪条车道标记「包尾」（`rx_last[i]`）。

134 位的由来：\(1\text{(first)} + 1\text{(tlast)} + 4\text{(tkeepdw)} + 128\text{(tdata)} = 134\)，这正是 IP 名 `fifo_134_134` 的含义（位宽 134 进 134 出）。

#### 4.2.3 源码精读

双时钟 FIFO 的例化，注意 `wr_clk` 与 `rd_clk` 分属两个域：

```verilog
fifo_134_134_clk2 i_fifo_134_134_clk2 (
    .rst    ( rst               ),
    .wr_clk ( clk_pcie          ),          // 写侧：PCIe 域
    .rd_clk ( clk_sys           ),          // 读侧：系统域
    .din    ( { tlps_in.tuser[0], tlps_in.tlast, tlps_in.tkeepdw, tlps_in.tdata } ),  // 134 位
    .wr_en  ( tlps_in.tvalid    ),
    .rd_en  ( dfifo.rx_rd_en    ),
    .dout   ( { first, tlast, tkeepdw, tdata } ),
    .valid  ( tvalid            )
);
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L118-L129`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L118-L129) — `clk_pcie→clk_sys` 的 134 位双时钟 FIFO。

读出后拆分到 4 条车道。关键是 `rx_last[i]` 的译码：只有「包尾拍」中**最后一个有效 DWORD** 所在的车道才标记 last，这样主机端能精确知道一个 32 位流在哪里结束：

```verilog
assign dfifo.rx_data[0]  = tdata[31:0];
assign dfifo.rx_data[1]  = tdata[63:32];
assign dfifo.rx_data[2]  = tdata[95:64];
assign dfifo.rx_data[3]  = tdata[127:96];
assign dfifo.rx_first[0] = first;                       // 只有车道0标记首拍
assign dfifo.rx_last[0]  = tlast && (tkeepdw == 4'b0001); // 包尾且只有DW0有效
assign dfifo.rx_last[1]  = tlast && (tkeepdw == 4'b0011); // 包尾且DW0~1有效
assign dfifo.rx_last[2]  = tlast && (tkeepdw == 4'b0111); // 包尾且DW0~2有效
assign dfifo.rx_last[3]  = tlast && (tkeepdw == 4'b1111); // 包尾且DW0~3全有效
assign dfifo.rx_valid[i] = tvalid && tkeepdw[i];          // 每车道按自己的 keep 位有效
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L131-L146`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L131-L146) — 128 位拆 4 车道，及 first/last/valid 译码。

要点：`rx_last[i]` 用 `tkeepdw` 的具体值（而不是 `>=`）来匹配，是因为一个「尾部只含 2 个 DWORD」的包，其 `tkeepdw=0011`，应当只有车道 1 标 last、车道 0 不标——这样主机端把车道 0、车道 1 拼起来正好在车道 1 收尾。

#### 4.2.4 代码实践

> **实践目标**：用一张表把 `rx_last` / `rx_valid` 的译码规律吃透。

操作步骤：

1. 阅读 [`pcileech_pcie_tlp_a7.sv#L139-L146`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L139-L146)。
2. 假设一个 TLP 包尾拍 `tlast=1`、`tkeepdw=4'b0111`（即该拍只有 DW0/DW1/DW2 三个字有效），填出 `rx_valid[0..3]` 与 `rx_last[0..3]` 各是多少。

需要观察的现象 / 预期结果（**待本地验证**）：

| 车道 i | `rx_valid[i]` | `rx_last[i]` |
|:---:|:---:|:---:|
| 0 | 1（`tkeepdw[0]=1`） | 0 |
| 1 | 1（`tkeepdw[1]=1`） | 0 |
| 2 | 1（`tkeepdw[2]=1`） | 1（匹配 `0111`） |
| 3 | 0（`tkeepdw[3]=0`） | 0 |

结论：只有车道 2 收尾，主机把车道 0/1/2 三个 DWORD 串起来正好在车道 2 结束，车道 3 被忽略。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rx_first` 只在车道 0 标记（`rx_first[1..3]=0`），而 `rx_last` 却要每条车道单独译码？

**答案**：包的「首拍」总是含满 4 个 DWORD 的头部（`tkeepdw=1111`），首拍 first 只需标在一个车道（车道 0）即可让主机知道「新包开始」。而「尾拍」可能不满 4 个 DWORD，必须按 `tkeepdw` 精确指出在哪个车道结束，否则主机无法判断 32 位流的终止位置。

**练习 2**：`dst_fifo` 的输入 `tlps_in` 用的是 `sink_lite` modport（无 `tready`/`has_data`，见 [`L108`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L108)）。如果 PCIe 侧 TLP 突发速率高于主机侧读出速率，会发生什么？

**答案**：因为上游无反压，`wr_en` 持续有效时数据会不断压入 `fifo_134_134_clk2`；一旦 FIFO 写满，`full` 信号（本例未接出使用）之后的写入会被丢弃。换言之 `dst_fifo` 依赖「FIFO 足够深 + 主机侧及时读」来吸收突发；这也是 [u3-l3](u3-l3-tlp-handling-overview.md) 强调的「上游保证不丢」前提在这里的体现。

---

### 4.3 上行 FIFO 桥接 `pcileech_tlps128_src_fifo`

#### 4.3.1 概念说明

`src` = source。这是发送方向（主机 → FPGA → PCIe 硬核）的桥：主机侧以 32 位为单位、逐 DWORD 注入 TLP（`dfifo.tx_data/tx_last/tx_valid`，即 [`IfPCIeFifoTlp`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L223) 的 `mp_pcie` 视角），本模块要把它们**拼装回 128 位 AXIS 包**，并跨到 `clk_pcie` 域，交给 `sink_mux1` 仲裁后送入硬核。

它比 `dst_fifo` 多一个精巧设计：**包计数器**。直接用双时钟 FIFO 的 `empty` 标志判断「有包可发」会有多周期同步延迟，于是模块自己维护一个「已完整入队的包数」计数，使下游 `has_data` 能在包刚拼好时就置位——这被称为**低延迟包 FIFO（low-latency packet fifo）**（见源码注释 [`L269-L270`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L269-L270)）。

#### 4.3.2 核心流程

分三段：

1. **32→128 拼装状态机**（`clk_sys` 域）：用 `tkeepdw` 作「已填充 DWORD 位图」，每来一个有效 32 位字就填进最低空槽；当 4 个槽填满（`tkeepdw[3]`）或收到 `tx_last`（包尾）时，本拍 `tvalid` 置位，输出一个完整 128 位字。
2. **包计数**（跨 `clk_sys→clk_pcie`）：每拼完一个完整包（`tvalid && tlast`）计数 +1，每发走一个包（`tlps_out.tvalid && tlps_out.tlast`）计数 -1；`has_data = (计数 > 0)`。
3. **输出双时钟 FIFO**（`clk_sys→clk_pcie`）：把 134 位 `{first, tlast, tkeepdw, tdata}` 跨域，读使能受 `tready` 与「有完整包」共同控制。

#### 4.3.3 源码精读

**拼装状态机**。`tvalid = tlast || tkeepdw[3]`——只要「看到包尾」或「四槽满」就视为一个 128 位字就绪：

```verilog
bit [3:0]   tkeepdw = 0;
bit         tlast;
bit         first   = 1;
wire        tvalid  = tlast || tkeepdw[3];

always @ ( posedge clk_sys )
    ...
    begin
        tlast   <= dfifo_tx_valid && dfifo_tx_last;
        tkeepdw <= tvalid ? (dfifo_tx_valid ? 4'b0001 : 4'b0000)      // 本拍已满/收尾：下一拍从 DW0 重新开始
                          : (dfifo_tx_valid ? ((tkeepdw << 1) | 1'b1) : tkeepdw); // 否则左移并占下一个槽
        first   <= tvalid ? tlast : first;                            // 包尾后，下一个字是新城的首拍
        if ( dfifo_tx_valid ) begin                                  // 广播写入所有空槽，实际填充最低空槽
            if ( tvalid || !tkeepdw[0] )  tdata[31:0]   <= dfifo_tx_data;
            if ( !tkeepdw[1] )             tdata[63:32]  <= dfifo_tx_data;
            if ( !tkeepdw[2] )             tdata[95:64]  <= dfifo_tx_data;
            if ( !tkeepdw[3] )             tdata[127:96] <= dfifo_tx_data;
        end
    end
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L216-L243`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L216-L243) — 32 位到 128 位的拼装。

理解技巧：`tkeepdw` 是「已占用槽位」位图。`tvalid` 为真（本拍出字）时，下一个 32 位字重新占用 DW0（`tkeepdw=0001`）；否则每来一个字，位图左移一位并置最低位（`<< 1 | 1`）。写入时把同一个 `dfifo_tx_data` 广播到所有「未占用」槽，但因为位图顺序占用，实际效果就是「填到最低空槽」，高槽会被后续字覆盖。

**包计数 + 跨域指示**。`fifo_1_1_clk2` 是一个 1 位宽的双时钟 FIFO，专门把「拼好一个包」这一事件从 `clk_sys` 安全传到 `clk_pcie`，作为 `pkt_count_inc` 脉冲：

```verilog
bit [10:0]  pkt_count      = 0;
wire        pkt_count_dec  = tlps_out.tvalid && tlps_out.tlast;     // 下游取走一个完整包
wire [10:0] pkt_count_next = pkt_count + pkt_count_inc - pkt_count_dec;
assign tlps_out.has_data   = (pkt_count_next > 0);                  // 有完整包可发

fifo_1_1_clk2 i_fifo_1_1_clk2(
    .wr_clk ( clk_sys ), .rd_clk ( clk_pcie ),
    .din    ( 1'b1 ), .wr_en ( tvalid && tlast ), .rd_en ( 1'b1 ),
    .valid  ( pkt_count_inc )
);
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L245-L267`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L245-L267) — 包计数与跨域「有包」指示。

**输出双时钟 FIFO**。读使能要求「下游 ready」且「确有完整包」同时成立，避免发出半截包：

```verilog
fifo_134_134_clk2_rxfifo i_fifo_134_134_clk2_rxfifo(
    .wr_clk ( clk_sys ), .rd_clk ( clk_pcie ),
    .din    ( { first, tlast, tkeepdw, tdata } ),
    .wr_en  ( tvalid ),
    .rd_en  ( tlps_out.tready && (pkt_count_next > 0) ),
    .dout   ( { tlps_out.tuser[0], tlps_out.tlast, tlps_out.tkeepdw, tlps_out.tdata } ),
    .valid  ( tlps_out.tvalid )
);
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L271-L282`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L271-L282) — 134 位输出双时钟 FIFO。

要点：数据通路有两个 FIFO——`fifo_1_1_clk2` 只传「事件」（包就绪），`fifo_134_134_clk2_rxfifo` 传「数据」。后者名字带 `_rxfifo` 后缀，是与 `dst_fifo` 用的同宽 FIFO 的**另一份独立例化**（同一个 `.xci` IP 可在不同地方各自例化），二者宽度都是 134，但分别处于发送/接收通路。

#### 4.3.4 代码实践

> **实践目标**：手算一次 32→128 拼装，验证 `tkeepdw` 与 `tvalid` 的演变。

操作步骤：

1. 阅读 [`pcileech_pcie_tlp_a7.sv#L216-L243`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L216-L243)。
2. 设初值 `tkeepdw=0000`、`first=1`。主机依次送 5 个有效 DWORD：A、B、C、D、E，其中 D 伴随 `tx_last=1`（即第 4 个字是一个包的结尾），E 是下一个包的开头。
3. 逐拍写下每拍结束时 `tkeepdw`、`tvalid`、输出 `tdata` 的占用情况。

需要观察的现象 / 预期结果（**待本地验证**）：

| 进入的字 | 进入后 `tkeepdw` | 本拍 `tvalid` | 输出 128 位字 |
|---|---|---|---|
| A（首） | `0001` | 0（未满 4、非 last） | — |
| B | `0011` | 0 | — |
| C | `0111` | 0 | — |
| D（`tx_last`） | `1111` | 1（`tlast` 即将置位 → `tvalid`） | `{D, C, B, A}` 按 [127:0] 排布，`tlast=1` |
| E（新包首） | `0001` | 0 | —（新包开始累积） |

说明：D 到来后 `tlast` 在本拍被赋值（下一拍生效），`tvalid = tlast || tkeepdw[3]` 在 `tkeepdw` 满到第 4 位时即置 1，于是 4 个字作为一包输出。该 128 位字进入输出 FIFO，包计数 +1，下游 `has_data` 随之置位。

> 注意：上表为帮助理解的简化模型，实际寄存器赋值有 1 拍延迟；以源码时序为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么需要单独的 `fifo_1_1_clk2` 来传「包就绪」事件，而不是直接用 `fifo_134_134_clk2_rxfifo` 的 `empty` 信号？

**答案**：双时钟 FIFO 的 `empty`/`valid` 标志在跨域时有多周期同步延迟，且它是「逐字」粒度。而仲裁器 `sink_mux1` 需要「以包为单位」知道某路是否有完整包可发（见 [u3-l5](u3-l5-tlp-mux-and-width-conversion.md) 的整包仲裁）。用一个独立的 1 位事件 FIFO 把「拼好一个包」的脉冲跨域，并维护 `pkt_count`，能让 `has_data` 在包一拼好就精确置位，降低仲裁延迟。

**练习 2**：`rd_en` 为什么写成 `tlps_out.tready && (pkt_count_next > 0)`，去掉 `(pkt_count_next > 0)` 会怎样？

**答案**：`(pkt_count_next > 0)` 保证「只在确有完整包时」才读。若去掉，可能在只剩半截包（尚未收到 `tx_last`）时就被 `tready` 拉动读出，导致下游收到不完整 TLP。该条件是实现「整包进、整包出」的必要闸门。

---

## 5. 综合实践

**任务：画出本讲三模块在「接收」与「发送」两条通路上的完整数据流，并用一次「主机注入 TLP」的场景串起全部知识点。**

请完成：

1. **画两张框图**。
   - 接收方向：`tlps_rx`(128b, `clk_pcie`) → `filter` → `tlps_filtered` → `dst_fifo`(`fifo_134_134_clk2`) → `dfifo.rx_data[0..3]`(32b×4, `clk_sys`) → 主机。在图上标出每个箭头的数据宽度与时钟域。
   - 发送方向：主机 → `dfifo.tx_data`(32b, `clk_sys`) → `src_fifo`（拼装状态机 → `fifo_1_1_clk2` + `fifo_134_134_clk2_rxfifo`）→ `tlps_rx_fifo`(128b, `clk_pcie`) → `sink_mux1` → `tlps_tx`。
2. **场景推演**。假设主机通过 PCILeech 下发命令，把 `cfgtlp_filter` 置 1，然后向目标机发起一次配置读。
   - 目标机回送的 `CplD` 走接收方向：它会被 `filter` 放行吗？为什么？（提示：`is_tlphdr_cpl`）
   - 它经过 `dst_fifo` 时，若该 CplD 头部 + 数据共 3 个 DWORD，尾拍 `tkeepdw=0111`，写出 `rx_valid[0..3]` 与 `rx_last[0..3]`。
3. **小结一句话**：用本讲的概念解释「为什么 PCILeech 能选择只把完成包（CplD）回送给主机软件」——这条链路上是哪个模块、哪个开关位、哪个 7 位匹配值在起作用？

预期结论：CplD 因命中 `is_tlphdr_cpl`（`7'b0100101`）被豁免放行；尾拍 `tkeepdw=0111` 时 `rx_valid=1110`、`rx_last[2]=1` 其余 0；起作用的是 `filter` 模块的 `is_tlphdr_cpl` 判定，与 `alltlp_filter` 开关配合（置 1 即只留完成包）。

## 6. 本讲小结

- `pcileech_tlps128_filter` 用 `tdata[31:25]` 这 7 位（`{Fmt[2:0], Type[4:1]}`）识别 `Cpl/CplD/CfgRd/CfgWr`，靠 `filter` 状态寄存器实现「首拍判定、整包丢弃」，被丢的拍只是 `tvalid` 被拉低。
- 两个开关位：`cfgtlp_filter` 丢 Cfg 包，`alltlp_filter` 丢「非完成包且非 Cfg」的包（即只留 Cpl/CplD），二者来自主机经 `dshadow2fifo` 下发。
- `pcileech_tlps128_dst_fifo` 用 `fifo_134_134_clk2` 把 134 位打包流从 `clk_pcie` 跨到 `clk_sys`，再拆成 4 条 32 位车道，`rx_last[i]` 按 `tkeepdw` 精确译码以标注包尾。
- `pcileech_tlps128_src_fifo` 反向把 32 位流拼回 128 位，用 `tkeepdw` 位图跟踪占用槽，`tvalid = tlast || tkeepdw[3]`。
- `src_fifo` 用「1 位事件 FIFO `fifo_1_1_clk2` + `pkt_count`」实现低延迟的整包指示，`has_data = (pkt_count > 0)`，读使能同时要求下游 ready 与「有完整包」。
- 134 位 = 1(first) + 1(last) + 4(tkeepdw) + 128(tdata)，是本工程 TLP 桥接 FIFO 的统一打包宽度。

## 7. 下一步学习建议

- 继续读 [u3-l5 TLP 多路复用与 64/128 位流转换](u3-l5-tlp-mux-and-width-conversion.md)：本讲 `src_fifo` 产出的 `tlps_rx_fifo` 如何被 `sink_mux1` 与其他三路（`cfg_rsp`/`bar_rsp`/`static`）按优先级仲裁；以及 64 位硬核流与 128 位 AXIS 流之间的 `tlps128_src64`/`dst64` 位宽转换。
- 进入 [u4 单元](u4-l1-custom-cfgspace-shadow.md)：本讲提到 `cfgtlp_filter` 把 Cfg 包从主机通路过滤掉，是因为它们被 `cfgspace_shadow` 就地应答——下一讲就打开这个影子配置空间模块。
- 想深入跨时钟域 FIFO 的设计前提，可读 [u5-l1 跨时钟域设计与双时钟 FIFO](u5-l1-clock-domain-crossing.md)，把本讲 `fifo_134_134_clk2`、`fifo_1_1_clk2` 的角色放进全板时钟域图谱中理解。
