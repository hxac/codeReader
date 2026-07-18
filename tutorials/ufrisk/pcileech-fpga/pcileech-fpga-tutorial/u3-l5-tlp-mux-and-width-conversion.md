# TLP 多路复用与 64/128 位流转换

## 1. 本讲目标

学完本讲后，你应当能够：

- 读懂 `pcileech_tlps128_sink_mux1` 的「**固定优先级 + 整包不打断**」仲裁机制，并解释 `id_next` 为何只在「当前空闲」或「当前包发完」这两个时刻才重新挑选最高优先级源。
- 读懂 `pcileech_tlps128_dst64` 如何把一个 128 位 AXIS 拍拆成最多**两个** 64 位 PCIe 核拍，并用 `d1_*` 寄存器暂存「后半段」。
- 读懂 `pcileech_tlps128_src64` 如何把若干 64 位 PCIe 核拍**拼装**成一个 128 位 AXIS 拍，并用 `len` 计数器与 `tkeepdw` 跟踪每个 DWORD 的有效性。
- 说出 PCIe 硬核的 64 位 AXI-Stream 接口（`IfPCIeTlpRxTx`）与工程内部 128 位 TLP 接口（`IfAXIS128`）在位宽、握手信号上的差异，以及为什么要在两者之间做双向位宽转换。

本讲承接 [u3-l3 TLP 处理总览](u3-l3-tlp-handling-overview.md) 与 [u3-l4 TLP 过滤与 FIFO 桥接](u3-l4-tlp-filter-and-fifo-bridge.md)：u3-l3 把 `pcileech_pcie_tlp_a7` 当作「调度枢纽」并点出发送方向有 4 路输入要仲裁，u3-l4 打开了接收方向的过滤与桥接管道。本讲补上最后两块拼图：发送方向上的**多路仲裁器**，以及夹在 128 位内部流与 64 位 PCIe 硬核之间的**双向位宽转换器**。读完本讲，TLP 从硬核进、到硬核出的整条通路就全部展开了。

## 2. 前置知识

### 2.1 为什么有两个位宽：64 位的硬核、128 位的内部流

Xilinx 7 系列 PCIe 硬核 IP（`pcie_7x_0`）的事务层接口是 **64 位** AXI-Stream：每个时钟周期最多承载 2 个 DWORD（64 位）TLP 数据。但工程为了吞吐与处理方便，在 `pcileech_pcie_tlp_a7` 内部统一使用 **128 位** 的 AXIS 流（`IfAXIS128`），每拍承载 4 个 DWORD。

于是硬核与内部 fabric 之间存在一个「宽度不匹配」，必须有两座桥：

- **接收（RX）**：硬核 64 位 → 内部 128 位，由 `pcileech_tlps128_src64` 完成（src = source，从硬核「取源」）。
- **发送（TX）**：内部 128 位 → 硬核 64 位，由 `pcileech_tlps128_dst64` 完成（dst = destination，把数据「送往」硬核）。

位宽转换不是简单的「把两根线并起来」：一个 128 位拍可能只含 1/2/3/4 个有效 DWORD（由 `tkeepdw` 指出），而 PCIe 包还可能在任意位置结束（`tlast`）。转换器必须同时搬运「数据」和「边界信息」。

### 2.2 硬核侧 64 位接口 `IfPCIeTlpRxTx`

回顾 [`IfPCIeTlpRxTx`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L300-L317)，它是硬核 `m_axis_rx` / `s_axis_tx` 一侧的契约：

| 信号 | 位宽 | 含义 |
|---|---|---|
| `data` | 64 | 一个周期承载 2 个 DWORD。 |
| `keep` | 8 | 字节有效。本讲关注 `keep[4]`：为 1 表示这个 64 位拍含 2 个有效 DWORD，为 0 表示只含 1 个 DWORD（窄拍，见于某些 TLP 的起始/收尾）。 |
| `last` | 1 | 本拍是该 TLP 的最后一拍。 |
| `user` | 22 | 接收侧 `user[8:2]` 是 **BAR 命中位**（7 位，对应 BAR0~BAR5 与 EXPROM），告诉设备这次访问命中了哪个 BAR。 |
| `valid / ready` | 1/1 | 标准 AXIS 握手。 |

### 2.3 内部 128 位接口 `IfAXIS128` 回顾

[`IfAXIS128`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194) 的关键字段（[u2-l1](u2-l1-interfaces-and-modports.md)、[u3-l4](u3-l4-tlp-filter-and-fifo-bridge.md) 已详述）：

- `tdata[127:0]`：一拍 4 个 DWORD。
- `tkeepdw[3:0]`：每个 DWORD 的有效位（`[0]`=DW0、`[1]`=DW1、`[2]`=DW2、`[3]`=DW3）。
- `tlast`：包尾拍。
- `tuser[8:0]`：`[0]=first`、`[1]=last`、`[8:2]=BAR 命中`。
- `tvalid / tready / has_data`：握手与「有完整包」指示。

注意 `tkeepdw[2]` 这个位在本讲特别重要：它为 1 表示该 128 位拍含有 **DW2**，也就是「延伸到了上半段 64 位」（DW2+DW3 = `tdata[127:64]`）。`dst64` 正是用它来判断「这个 128 位拍要不要拆成两个 64 位拍」。

### 2.4 为什么发送仲裁必须「整包不打断」

发送方向（设备 → 主机方向之外、这里指 **送入 PCIe 硬核** 的方向）有 4 路来源（详见 [u3-l3](u3-l3-tlp-handling-overview.md)）：

1. `cfg_rsp`：配置空间影子对主机 `CfgRd/CfgWr` 的完成应答（`CplD`）。
2. `bar_rsp`：BAR 控制器对主机 BAR 访问的完成应答。
3. `rx_fifo`：主机主动注入、要发到链路上的原始 TLP（经 [u3-l4](u3-l4-tlp-filter-and-fifo-bridge.md) 的 `src_fifo` 拼装而来）。
4. `static`：`pcileech_pcie_cfg_a7` 预置的静态 TLP。

一个 PCIe TLP 是一个**不可拆分的整体**——若仲裁器在第 1 路某个包发到一半时切到第 2 路，链路上就会出现「半个包」，接收端无法解析。所以发送仲裁的硬约束是：**一旦选中某路，必须让它把当前整个包发完才能切换**。这就是 `sink_mux1` 名字里 `mux1` 的含义——同一时刻只有 1 路在发，且按整包轮转。

## 3. 本讲源码地图

本讲跨两个源文件，三个子模块，外加它们的例化点：

| 子模块 | 所在文件 | 源码行 | 方向 | 职责 |
|---|---|---|---|---|
| `pcileech_tlps128_sink_mux1` | `pcileech_pcie_tlp_a7.sv` | [L294–L348](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L294-L348) | 发送（内部） | 4 路 128 位流按优先级整包仲裁成 1 路 |
| `pcileech_tlps128_dst64` | `pcileech_pcie_a7.sv` | [L269–L296](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L269-L296) | 发送（入硬核） | 128 位 → 64 位，拆拍 |
| `pcileech_tlps128_src64` | `pcileech_pcie_a7.sv` | [L303–L350](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L303-L350) | 接收（出硬核） | 64 位 → 128 位，拼拍 |

在父模块 [`pcileech_pcie_a7`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L87-L111) 中，三者串成一条链（注意 RX/TX 方向相反）：

- **接收（RX）**：硬核 `m_axis_rx`(64b) → `tlp_rx` → [`src64`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L87-L92) → `tlps_rx`(128b) → `pcileech_pcie_tlp_a7` 处理。
- **发送（TX）**：`pcileech_pcie_tlp_a7` 内的 [`sink_mux1`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L86-L94) 产出 `tlps_tx`(128b) → [`dst64`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L106-L111) → `tlp_tx`(64b) → 硬核 `s_axis_tx`。

涉及的接口契约：[`IfAXIS128`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)、[`IfPCIeTlpRxTx`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L300-L317)。

## 4. 核心概念与源码讲解

### 4.1 发送仲裁器 `pcileech_tlps128_sink_mux1`

#### 4.1.1 概念说明

`sink_mux1` 是发送方向（送入 PCIe 硬核）的「合流点」。它有 4 个 128 位 AXIS 输入和 1 个 128 位 AXIS 输出，任务是把 4 路可能同时有数据的 TLP 流，**串行化**成一路送给 `dst64`，再经 `dst64` 拆成 64 位送进硬核。

它的设计哲学与 [u2-l4](u2-l4-output-mux-256bit.md) 讲的 `pcileech_mux`（上行回主机的「合流打包器」）截然不同：

| | `pcileech_mux`（u2-l4，回主机） | `sink_mux1`（本讲，入硬核） |
|---|---|---|
| 粒度 | 按**字**（32 位）轮流打包 | 按**整包**轮流发送 |
| 是否打断 | 多路数据可塞进同一个 256 位包 | 一路必须发完整个 TLP 才能切 |
| 原因 | 主机端能按 tag 还原交错字 | PCIe 链路不允许半截 TLP |

所以 `sink_mux1` 本质是一个「**整包轮转的优先级仲裁器**」：每当下游空闲、或当前包刚发完，它就重新在 4 路里挑「编号最小（优先级最高）且有数据」的那一路，然后把总线独占地交给它，直到它把当前包（以 `tlast` 结尾）发完。

模块头注释一句话点明了规则（「Select the TLP-AXI-STREAM with the highest priority (lowest number) and let it transmit its full packet」，见 [L288–L293](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L288-L293)），并要求「Each incoming stream must have latency of 1CLK」——这正是 [u3-l4](u3-l4-tlp-filter-and-fifo-bridge.md) 里 `src_fifo` 要维护 `pkt_count`/`has_data` 低延迟指示的原因：仲裁器依赖各路「立刻」报上是否有完整包。

#### 4.1.2 核心流程

仲裁由一个 3 位状态寄存器 `id` 驱动：

1. **`has_data` 汇总**：输出「有无数据」是 4 路 `has_data` 的或，让下游立刻知道总有事可做。
2. **数据选择**：`tdata/tkeepdw/tlast/tuser/tvalid` 全部按当前 `id` 从对应输入直通到输出（纯组合多路选择）。
3. **重新选择 `id_next_newsel`**：按编号 1→2→3→4 的顺序，挑第一个 `has_data` 的路；都无数据则回 0（空闲）。
4. **切换时机 `id_next`**：**只有**两种情况才允许切换到新选择——
   - 当前 `id==0`（本来空闲），或
   - 当前正在发的这一拍同时满足 `tvalid && tlast`（即当前包的最后一拍正在送出）。

   否则 `id_next = id`（保持原选择，让当前包继续独占）。
5. **反向路由 `tready`**：把下游的 `tready` 只送给 `id_next` 选中的那一路，其余 3 路 `tready=0`。由于 `id_next` 在包内保持不变，被选中的路在其整个包期间都能拿到 `tready`。

优先级与 [u3-l3](u3-l3-tlp-handling-overview.md) 给出的发送顺序一致：`cfg_rsp(1) > bar_rsp(2) > rx_fifo(3) > static(4)`。即本地应答（cfg/bar）优先于主机注入（rx_fifo），预置包（static）最低。

#### 4.1.3 源码精读

**输出选择**——所有 AXIS 字段都按 `id` 多路选通，`id==0` 时输出全 0（无有效数据）：

```verilog
assign tlps_out.has_data = tlps_in1.has_data || tlps_in2.has_data
                         || tlps_in3.has_data || tlps_in4.has_data;

assign tlps_out.tdata  = (id==1) ? tlps_in1.tdata  :
                         (id==2) ? tlps_in2.tdata  :
                         (id==3) ? tlps_in3.tdata  :
                         (id==4) ? tlps_in4.tdata  : 0;
// tkeepdw / tlast / tuser / tvalid 结构完全相同，按 id 选通 ...
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L305-L330`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L305-L330) — has_data 汇总与按 id 的输出选通。

**重新选择 + 切换时机**——本模块最关键的两行：

```verilog
// 重新挑选：编号越小优先级越高，全无数据则回 0
wire [2:0] id_next_newsel = tlps_in1.has_data ? 1 :
                            tlps_in2.has_data ? 2 :
                            tlps_in3.has_data ? 3 :
                            tlps_in4.has_data ? 4 : 0;

// 切换条件：当前空闲(id==0)，或当前包的最后一拍正在发出(tvalid&&tlast)
wire [2:0] id_next = ((id==0) || (tlps_out.tvalid && tlps_out.tlast))
                       ? id_next_newsel : id;

always @ ( posedge clk_pcie ) begin
    id <= rst ? 0 : id_next;
end
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L332-L346`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L332-L346) — 优先级选择与「整包才切」的切换条件。

要点：

- `id_next_newsel` 是**纯组合**的优先级编码器，每个周期都重新算「此刻该选谁」，但它**并不立即生效**——是否真的切换由 `id_next` 的条件门控。
- 条件 `(id==0) || (tvalid && tlast)` 是「整包不打断」的核心：包发到一半时 `tvalid=1` 而 `tlast=0`，条件不成立，`id_next=id`，当前路继续独占；只有当最后一拍（`tvalid && tlast` 同时为 1）被送出的那个周期，下一拍才允许切到新的最高优先级路。
- `id` 初值为 0（[L303](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L303) `bit [2:0] id = 0;`），所以上电后第一拍就满足 `id==0`，会立刻挑出第一个有数据的路。

**反向路由 tready**——只有下一拍选中的路拿到反压通道：

```verilog
assign tlps_in1.tready = tlps_out.tready && (id_next==1);
assign tlps_in2.tready = tlps_out.tready && (id_next==2);
assign tlps_in3.tready = tlps_out.tready && (id_next==3);
assign tlps_in4.tready = tlps_out.tready && (id_next==4);
```

> 参考：[`pcileech_pcie_tlp_a7.sv#L339-L342`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L339-L342) — tready 按 id_next 路由。

注意 `tready` 用的是 `id_next`（下一拍的选择）而非 `id`，这样选通与数据传输在同一拍对齐：被选中的路在这一拍就能把数据推进来。配合 `id_next` 在包内恒定，被选中路在整个包期间持续拿到 `tready`，包内每一拍都能顺利送出。

#### 4.1.4 代码实践

> **实践目标**：用一张时序表把 `id_next` 的「整包才切」逻辑走一遍，验证仲裁器绝不会把一个 TLP 切成两半。

操作步骤：

1. 阅读 [`pcileech_pcie_tlp_a7.sv#L332-L346`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L332-L346)。
2. 设定场景：`cfg_rsp`(in1) 和 `rx_fifo`(in3) 同时有包要发；`cfg_rsp` 的包占 2 拍（第 2 拍 `tlast=1`）；`rx_fifo` 的包紧随其后。下游始终 `tready=1`。初始 `id=0`。
3. 逐拍推导 `id_next_newsel`、`id_next`、`id`（下一拍）、当前输出的来源。

需要观察的现象 / 预期结果（**待本地仿真验证**）：

| 拍 | in1(cfg) has/tlast | in3(rx) has | id（本拍） | id_next_newsel | tvalid&&tlast? | id_next（下拍 id） | 实际输出 |
|---|---|---|---|---|---|---|---|
| 1 | 有 / 0 | 有 | 0（空闲） | 1（in1 优先） | — | 1 | in1 第 1 拍 |
| 2 | 有 / 1（尾） | 有 | 1 | 1（in1 仍优先） | 是（in1 尾拍） | 1（newsel 仍为 1，但 in1 此后将无数据） | in1 第 2 拍（尾） |
| 3 | 无 | 有 | 1 | 3（in1 无数据 → 选 in3） | 否 | 3 | in3 第 1 拍 |
| 4 | … | 有 / 1（尾） | 3 | 3 | 是 | 3 | in3 第 2 拍（尾） |

关键现象：第 2 拍虽然 in1 即将发完，但因为此刻 `tvalid&&tlast` 成立，`id_next` 取 `newsel`——只是此时 in1 还在报 `has_data`，所以 `newsel` 仍是 1；真正切到 in3 发生在 in1 不再报数据之后。整个过程 in1 的两拍连续送出、in3 的两拍也连续送出，**没有任何包被打断**。

> 说明：上表为帮助理解的逻辑模型，未计入上游 FIFO `valid` 的具体时序细节；以本地仿真波形为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把切换条件从 `(id==0) || (tvalid && tlast)` 改成 `id==0 || id_next_newsel < id`（即「只要更高优先级的路一有数据就抢占」），对 PCIe 链路会有什么影响？

**答案**：会出现「一个 TLP 发到一半被更高优先级路抢占」的情况，链路上送出半截包，PCIe 接收端无法解析，导致协议错误。这正是本模块坚持「整包才切」的原因——PCIe TLP 不可拆分，优先级只能在包与包之间生效，不能在包内抢占。

**练习 2**：模块注释要求「Each incoming stream must have latency of 1CLK」（[L292](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L292)）。如果某一路的 `has_data` 有 3 拍延迟才报上来，会发生什么？

**答案**：`id_next_newsel` 依赖 `has_data` 当拍组合给出。若 `has_data` 迟迟不报，仲裁器在该路实际已有包的最初几拍里会认为它「无数据」而错失选择，可能先选了低优先级路，造成不必要的延迟，甚至在该路是「应答类」包时影响协议时序。这就是为什么 [u3-l4](u3-l4-tlp-filter-and-fifo-bridge.md) 的 `src_fifo` 要专门用「1 位事件 FIFO + `pkt_count`」把 `has_data` 做成低延迟信号——它直接服务于本仲裁器。

---

### 4.2 发送位宽转换 `pcileech_tlps128_dst64`（128→64）

#### 4.2.1 概念说明

`dst64` 紧跟在 `sink_mux1` 之后，把仲裁器输出的 128 位 AXIS 流（`tlps_tx`）拆成硬核能吃的 64 位 AXIS 流（`tlp_tx`，接 `pcie_7x_0` 的 `s_axis_tx`）。

核心难点：**一个 128 位拍最多含 4 个 DWORD，而硬核每拍只收 2 个 DWORD**。所以一个「满」的 128 位拍（DW0~DW3 全有效）必须分成**两个** 64 位拍依次送出：先 `{DW1,DW0}`，再 `{DW3,DW2}`。而一个「短」的 128 位拍（只有 DW0~DW1，即 `tkeepdw[2]==0`）只需一个 64 位拍。

模块用一组 `d1_*`「延迟 1 拍」寄存器来暂存这个「后半段」：当看到 DW2 存在时，把上半段 64 位（`tdata[127:64]`）及其有效位、`tlast` 锁存进 `d1_*`，下一拍再发出去。`d1` = delayed by 1 cycle。

#### 4.2.2 核心流程

每个进入的 128 位拍按 `tkeepdw[2]`（「是否含 DW2 / 是否有上半段」）分两路处理：

1. **下半段（DW0/DW1，即 `tdata[63:0]`）**：组合直送硬核，`keep` 由 `tkeepdw[1]` 决定（DW1 在则 8 字节全有效 `0xff`，否则只低 4 字节 `0x0f`）。
2. **是否锁存上半段**：若 `tkeepdw[2]==1`（有 DW2，即存在上半段），把 `tdata[127:64]` 锁进 `d1_tdata`、`tkeepdw[3]` 锁进 `d1_tkeepdw2`、`tlast` 锁进 `d1_tlast`，并置 `d1_tvalid=1`。
3. **发上半段**：下一拍 `d1_tvalid==1` 时，输出来源切到 `d1_*`，送出 `{DW3,DW2}`，`keep` 由 `d1_tkeepdw2` 决定，`last` 由 `d1_tlast` 决定。
4. **反压**：当输入拍含有上半段（`tkeepdw[2]==1`）时，向输入回 `tready=0`，确保当前 128 位拍完整拆成两拍后再接纳下一拍。

`tlast` 的归属也很精巧：如果 128 位拍有上半段，`tlast` 必须跟着上半段（第二个 64 位拍）走，因为包尾的真正终点在 DW3/DW2 那一拍；只有当拍没有上半段时，`tlast` 才在唯一的 64 位拍上生效。

#### 4.2.3 源码精读

**反压与组合输出**——`d1_tvalid` 是「输出选哪个半段」的总开关：

```verilog
bit [63:0]  d1_tdata;   bit d1_tkeepdw2;   bit d1_tlast;   bit d1_tvalid = 0;

// 含上半段时(tkeepdw[2])不接纳新拍，先把后半段发完
assign tlps_in.tready = tlp_tx.ready && !(tlps_in.tvalid && tlps_in.tkeepdw[2]);

wire tkeepdw2       = d1_tvalid ? d1_tkeepdw2 : tlps_in.tkeepdw[1];
assign tlp_tx.data  = d1_tvalid ? d1_tdata    : tlps_in.tdata[63:0];
assign tlp_tx.last  = d1_tvalid ? d1_tlast    : (tlps_in.tlast && !tlps_in.tkeepdw[2]);
assign tlp_tx.keep  = tkeepdw2 ? 8'hff : 8'h0f;
assign tlp_tx.valid = d1_tvalid || tlps_in.tvalid;
```

> 参考：[`pcileech_pcie_a7.sv#L276-L287`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L276-L287) — d1 暂存与「上下半段」输出选通。

**锁存上半段**——`d1_tvalid` 仅当本拍含 DW2 时置位：

```verilog
always @ ( posedge clk_pcie ) begin
    d1_tvalid    <= !rst && tlps_in.tvalid && tlps_in.tkeepdw[2];  // 有上半段才锁存
    d1_tdata     <= tlps_in.tdata[127:64];   // 上半段 64 位
    d1_tlast     <= tlps_in.tlast;           // 包尾随上半段走
    d1_tkeepdw2  <= tlps_in.tkeepdw[3];      // DW3 是否有效
end
```

> 参考：[`pcileech_pcie_a7.sv#L289-L294`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L289-L294) — 上半段（DW2/DW3）的锁存。

要点对照：`d1_tvalid` 由 `tkeepdw[2]` 触发，`d1_tkeepdw2` 记的是 `tkeepdw[3]`（DW3），二者配合还原上半段的 `keep`（`0xff` 表示 DW2/DW3 都在，`0x0f` 表示只有 DW2）。`d1_tlast` 直接搬运 `tlast`，保证包尾落在最后一个 64 位拍上。

#### 4.2.4 代码实践

> **实践目标**：用两个典型场景验证 `d1_tvalid` 如何处理 128 位拍的「后半段」。

操作步骤：

1. 阅读 [`pcileech_pcie_a7.sv#L276-L294`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L276-L294)。
2. 场景 A：一个满拍进入，`tdata={DW3,DW2,DW1,DW0}`、`tkeepdw=4'b1111`、`tlast=1`。
3. 场景 B：一个短拍进入，`tdata={x,x,DW1,DW0}`、`tkeepdw=4'b0011`（只 DW0/DW1 有效）、`tlast=1`。
4. 分别写出每种场景下，硬核 `tlp_tx` 上会看到几个 64 位拍，每拍的 `data/keep/last` 各是多少。

需要观察的现象 / 预期结果（**待本地仿真验证**）：

**场景 A（满拍，4 DWORD）→ 拆成 2 个 64 位拍**：

| 64 位拍 | data | keep | last | 由谁输出 |
|---|---|---|---|---|
| 1 | `{DW1,DW0}`（`tdata[63:0]`） | `0xff`（`tkeepdw[1]=1`） | 0（`tlast && !tkeepdw[2]` = 1&&0） | 组合直送 |
| 2 | `{DW3,DW2}`（`d1_tdata`） | `0xff`（`d1_tkeepdw2=tkeepdw[3]=1`） | 1（`d1_tlast`） | `d1_*` 暂存后送出 |

**场景 B（短拍，2 DWORD）→ 只 1 个 64 位拍**：

| 64 位拍 | data | keep | last | 由谁输出 |
|---|---|---|---|---|
| 1 | `{DW1,DW0}`（`tdata[63:0]`） | `0xff`（`tkeepdw[1]=1`） | 1（`tlast && !tkeepdw[2]` = 1&&1） | 组合直送 |

场景 B 因 `tkeepdw[2]=0`，`d1_tvalid` 不会被置位，没有第二拍。这正说明 `d1_*` 是「按需启用」的：只有含上半段的拍才会产生第二拍输出。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tlp_tx.last` 在「直送下半段」分支里写成 `tlps_in.tlast && !tlps_in.tkeepdw[2]`，而不是直接 `tlps_in.tlast`？

**答案**：如果一个 128 位拍既有上半段（`tkeepdw[2]=1`）又是包尾（`tlast=1`），那么包的真正终点在第二拍（上半段 `{DW3,DW2}`）上。若第一拍就标 `last=1`，硬核会误以为包在 `{DW1,DW0}` 处结束，丢掉 DW2/DW3。所以第一拍的 `last` 必须被 `!tkeepdw[2]` 屏蔽掉，真正的 `last` 由 `d1_tlast` 在第二拍给出。

**练习 2**：`tlps_in.tready = tlp_tx.ready && !(tlps_in.tvalid && tlps_in.tkeepdw[2])`。对一个不断送来的「满拍」流（每拍都 `tkeepdw=1111`），这意味着 `dst64` 的吞吐与硬核 `tlp_tx.ready` 是什么关系？

**答案**：每个满拍要占用 2 个 64 位拍周期，且期间 `tready=0` 不接纳新拍。所以「接纳新 128 位拍」的速率最高只有硬核 `tlp_tx` 速率的一半（满拍情况下）。换言之，`dst64` 把 128 位/拍折算成 64 位/拍时，吞吐守恒：128 位流的有效带宽不能超过硬核 64 位接口带宽。这是位宽转换的固有特性，而非缺陷。

---

### 4.3 接收位宽转换 `pcileech_tlps128_src64`（64→128）

#### 4.3.1 概念说明

`src64` 是 `dst64` 的镜像：它从硬核 `m_axis_rx`(64 位) 接收 TLP，拼装成内部 128 位 AXIS 流（`tlps_rx`），交给 `pcileech_pcie_tlp_a7` 的过滤/应答管道。

它的任务有两半：

1. **拼拍**：把若干 64 位拍（每拍 1 或 2 个 DWORD）攒成 128 位拍（最多 4 个 DWORD）再输出。用一个 4 位计数器 `len` 记录「当前 128 位拍里已经攒了几个 DWORD」。
2. **搬运边界信息**：把硬核侧的 `keep[4]`（本拍 1 个还是 2 个 DWORD）、`last`（包尾）、`user[8:2]`（BAR 命中）翻译成 `IfAXIS128` 的 `tkeepdw/tlast/tuser`。

注意 `src64` 把硬核侧 `tlp_rx.ready` 恒置 1（[L317](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L317)），即**不反压硬核**——硬核给什么就收什么，自己负责攒成 128 位。这是 `source_lite` 风格（单向推送）的体现。

#### 4.3.2 核心流程

拼拍用一个累加器 `len`（已攒 DWORD 数）驱动：

1. **何时输出一拍**：`tvalid = tlast || (len>2)`。即「收到包尾」或「攒满（接近 4 个 DWORD）」时，本拍就绪输出。攒满后 `len` 归零，开始攒下一个 128 位拍。
2. **写入位置**：`next_base = (tlast || tvalid) ? 0 : len`——若上一拍刚输出过（或包尾），从 DW0 重新开始；否则接着 `len` 的位置继续填。`tdata[(32*next_base)+:64] <= tlp_rx.data` 把 64 位写到对应位置。
3. **DWORD 计数**：`next_len = next_base + 1 + tlp_rx.keep[4]`。恒 +1（DW0 总在），再 +`keep[4]`（DW1 是否在）。
4. **tkeepdw 译码**：`tkeepdw = {(len>3),(len>2),(len>1),1'b1}`——按 `len` 还原 4 个有效位（DW0 恒有效）。
5. **边界信号**：`tuser[0]=first`（包首）、`tuser[1]=tlast`（包尾）、`tuser[8:2]=bar_hit`（搬运硬核 `user[8:2]` 的 BAR 命中位）。

`first` 的维护（`first <= tvalid ? tlast : first`）确保每个包的第一拍带 `first=1`：发完一个 `tlast` 拍后，下一个攒起来的拍自然标 `first=1`（新包开始）。

#### 4.3.3 源码精读

**输出与边界译码**——`tvalid`、`tkeepdw`、`tuser` 全部由 `len/first/tlast/bar_hit` 组合给出：

```verilog
bit [127:0] tdata;   bit first = 1;   bit tlast = 0;
bit [3:0]   len = 0; bit [6:0] bar_hit = 0;
wire        tvalid = tlast || (len>2);                       // 包尾或攒满 → 输出

assign tlp_rx.ready     = 1'b1;                              // 不反压硬核
assign tlps_out.tdata   = tdata;
assign tlps_out.tkeepdw = {(len>3), (len>2), (len>1), 1'b1}; // 还原 4 个 DWORD 有效位
assign tlps_out.tlast   = tlast;
assign tlps_out.tvalid  = tvalid;
assign tlps_out.tuser[0]    = first;                         // 包首
assign tlps_out.tuser[1]    = tlast;                         // 包尾
assign tlps_out.tuser[8:2]  = bar_hit;                       // BAR 命中（来自硬核 user[8:2]）
```

> 参考：[`pcileech_pcie_a7.sv#L310-L324`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L310-L324) — 输出组合逻辑与 `tkeepdw/tuser` 译码。

**拼拍状态机**——`next_base/next_len` 决定写入位置与计数推进：

```verilog
wire [3:0] next_base = (tlast || tvalid) ? 0 : len;          // 刚输出过则从 DW0 重开
wire [3:0] next_len  = next_base + 1 + tlp_rx.keep[4];       // +1(DW0) +keep[4](DW1?)

always @ ( posedge clk_pcie )
    if ( rst ) begin first<=1; tlast<=0; len<=0; bar_hit<=0; end
    else if ( tlp_rx.valid ) begin
        tdata[(32*next_base)+:64] <= tlp_rx.data;            // 64 位写到 next_base 位置
        first   <= tvalid ? tlast : first;                   // 发完 tlast 拍后，下拍是新包首
        tlast   <= tlp_rx.last;                              // 搬运硬核包尾
        len     <= next_len;
        bar_hit <= tlp_rx.user[8:2];                         // 搬运 BAR 命中
    end
    else if ( tvalid ) begin                                 // 上游停了但本拍就绪：收尾
        first <= tlast; tlast <= 0; len <= 0; bar_hit <= 0;
    end
```

> 参考：[`pcileech_pcie_a7.sv#L326-L348`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L326-L348) — 拼拍写入与 `len/first` 推进。

要点：

- `next_len = next_base + 1 + keep[4]`：硬核每个 64 位拍至少带来 1 个 DWORD（DW0），`keep[4]` 为 1 时再带 DW1，所以一个满 64 位拍贡献 2 个 DWORD，窄拍贡献 1 个。
- `tdata[(32*next_base)+:64]` 是 SystemVerilog 的可变起始位切片：`next_base` 为 0 时写 `tdata[63:0]`（下半段），为 2 时写 `tdata[127:64]`（上半段），正好把两个 64 位拍拼成一个 128 位拍。
- `bar_hit <= tlp_rx.user[8:2]` 把硬核的 BAR 命中位原样搬到 `tuser[8:2]`，供下游 `bar_controller`/`cfgspace_shadow` 判断这次访问命中哪个 BAR（详见 [u4-2](u4-l2-bar-controller-engines.md)、[u4-1](u4-l1-custom-cfgspace-shadow.md)）。

#### 4.3.4 代码实践

> **实践目标**：手算一次 64→128 拼拍，验证一个 4-DWORD 的小 TLP 如何被拼成一个 128 位拍。

操作步骤：

1. 阅读 [`pcileech_pcie_a7.sv#L310-L348`](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L310-L348)。
2. 设初值 `len=0`、`first=1`、`tlast=0`。硬核依次送两个满 64 位拍：W0=`{DW1,DW0}`（`keep[4]=1`、`last=0`、`user[8:2]` 命中 BAR0），W1=`{DW3,DW2}`（`keep[4]=1`、`last=1`）。
3. 逐拍写下每拍结束时 `len`、`tvalid`、`first/tlast` 与 `tdata` 的填充情况。

需要观察的现象 / 预期结果（**待本地仿真验证**）：

| 进入的 64 位拍 | next_base | 写入位置 | next_len（len 更新后） | 本拍 tvalid（按更新前 len） | 累计效果 |
|---|---|---|---|---|---|
| W0（`keep[4]=1`，首） | `(0||0)?0:0`=0 | `tdata[63:0]`=W0 | 0+1+1=2 | `0||(0>2)`=0 | 攒了 DW0/DW1 |
| W1（`keep[4]=1`，`last=1`） | `(0||0)?0:2`=2 | `tdata[127:64]`=W1 | 2+1+1=4 | `0||(2>2)`=0（本拍仍 0） | 攒齐 4 个 DWORD，`tlast` 置 1 |
| （W1 之后的下一拍） | — | — | — | `1||(4>2)`=1 → **输出** | 输出 128 位 `{DW3,DW2,DW1,DW0}`，`tkeepdw=1111`、`tlast=1`、`first=1`、`tuser[8:2]`=BAR0 命中 |

关键现象：两个 64 位拍正好拼成一个 128 位拍；输出拍带 `tlast=1`（因为 W1 带 `last=1`）、`first=1`（包的首拍）、`tkeepdw=1111`（4 个 DWORD 全有效），BAR 命中位被搬运到 `tuser[8:2]`。

> 说明：上表是逻辑模型，寄存器赋值有 1 拍延迟；以源码时序 / 本地仿真为准。

#### 4.3.5 小练习与答案

**练习 1**：`tvalid = tlast || (len>2)` 里为什么用 `len>2`（即 len 为 3 或 4 时触发），而不是直接 `len==4`？

**答案**：在最常见的「满 64 位拍」流里，`len` 按每次 +2 增长（0→2→4），`len>2` 在 `len=4` 时成立，等价于「攒满 4 个 DWORD 就输出」，效果与 `len==4` 相同。但用 `len>2` 还能兼容窄拍（`keep[4]=0`，每次 +1）的奇数累积情况，并在 `tlast` 到来时立刻输出尾部不满 4 个 DWORD 的拍。这是一个更宽松、对各种 TLP 长度都成立的触发条件。

**练习 2**：硬核送来的 `user[8:2]`（BAR 命中）被原样搬到 `tuser[8:2]`。下游的 `bar_controller` 为什么要关心这个字段？

**答案**：BAR 命中位告诉设备「这次 Memory 读/写访问命中了哪个 BAR」。`bar_controller` 需要据此把请求分发到对应的用户实现核心（如 `zerowrite4k`、`loopaddr`，详见 [u4-2](u4-l2-bar-controller-engines.md) / [u4-3](u4-l3-bar-impls-and-coe.md)），并对读请求生成正确的 `CplD` 应答。`src64` 只负责搬运这个字段，真正使用它的是后续的 BAR 处理逻辑。

---

## 5. 综合实践

**任务：把本讲三个模块串成一条「设备回送 CplD 给主机」的完整发送通路，并标注每一段的位宽与时钟域。**

请完成：

1. **画一张发送方向（设备 → 主机以外的「送出」方向其实分两段，这里聚焦「送入 PCIe 硬核」段）的框图**：

   `cfgspace_shadow` 产出的 `tlps_cfg_rsp`(128b) ┐
   `bar_controller` 产出的 `tlps_bar_rsp`(128b) ─┤→ `sink_mux1`（按优先级整包仲裁）→ `tlps_tx`(128b) → `dst64`（128→64，`d1_*` 拆拍）→ `tlp_tx`(64b) → 硬核 `s_axis_tx`。
   `src_fifo` 产出的 `tlps_rx_fifo`(128b) ──────┤
   `pcileech_pcie_cfg_a7` 的 `tlps_static`(128b) ┘

   在图上标出：每个箭头的位宽（128 或 64）、`sink_mux1` 的优先级编号（1/2/3/4）、`dst64` 里 `d1_tvalid` 起作用的那个「拆成两拍」的位置。

2. **场景推演**。假设 `cfgspace_shadow` 刚生成一个对主机 `CfgRd` 的 `CplD` 应答（3 DW 头 + 1 DW 数据 = 4 DWORD，`tlast=1`、`tkeepdw=1111`），同时主机也注入了一个 TLP（`rx_fifo` 路有数据）。
   - `sink_mux1` 会优先选哪一路？为什么？（提示：`id_next_newsel` 的优先级编码）
   - 被选中的这个 4-DWORD 拍经过 `dst64` 时，会变成几个 64 位拍？第一个 64 位拍的 `last` 是 0 还是 1？为什么？
3. **一句话小结**：用本讲的概念解释「为什么内部用 128 位、硬核用 64 位，二者之间不会丢包也不会冒出半截包」——`sink_mux1`、`dst64`、`src64` 各自在其中起了什么作用？

预期结论：`sink_mux1` 因 in1(cfg_rsp) 编号最小而优先选中它；其 4-DWORD 拍经 `dst64` 拆成两个 64 位拍（先 `{DW1,DW0}`，`last=0`；再 `{DW3,DW2}`，`last=1`），`d1_*` 暂存上半段保证包尾落在第二拍；`src64` 在接收方向对称地把硬核 64 位拍拼回 128 位。三者合力保证位宽转换中 TLP 始终完整。

## 6. 本讲小结

- `pcileech_tlps128_sink_mux1` 是发送方向的「整包轮转优先级仲裁器」：`id_next_newsel` 按 in1→in4 编码最高优先级路，但 `id_next` 只在 `id==0` 或 `tvalid&&tlast` 时才切换，保证一个 TLP 绝不被打断；`tready` 按 `id_next` 路由给当前选中路。
- 4 路优先级为 `cfg_rsp(1) > bar_rsp(2) > rx_fifo(3) > static(4)`，本地应答优先于主机注入、预置包最低。
- `pcileech_tlps128_dst64`（128→64）用一个 128 位拍含 DW2 与否（`tkeepdw[2]`）判断要不要拆成两拍；`d1_tdata/d1_tlast/d1_tkeepdw2/d1_tvalid` 暂存上半段，下一拍送出，包尾始终落在最后一个 64 位拍。
- `pcileech_tlps128_src64`（64→128）用 `len` 计数器把 64 位拍攒成 128 位拍，`next_len = next_base + 1 + keep[4]`，`tvalid = tlast || len>2`；并把硬核 `user[8:2]`（BAR 命中）搬到 `tuser[8:2]`。
- 位宽转换的关键是「同时搬运数据与边界」：`tkeepdw` 标 DWORD 有效、`tlast` 标包尾、`tuser` 标首/尾/BAR，三者必须在 64↔128 转换中一一对应、不丢失。
- 三个模块都运行在 `clk_pcie` 域（`dst64`/`src64` 与硬核同域，`sink_mux1` 也在 `clk_pcie`），跨到 `clk_sys` 的工作已由 [u3-l4](u3-l4-tlp-filter-and-fifo-bridge.md) 的 `dst_fifo`/`src_fifo` 完成。

## 7. 下一步学习建议

- 进入 [u4 单元 设备仿真](u4-l1-custom-cfgspace-shadow.md)：本讲反复提到的 `cfg_rsp`、`bar_rsp` 两路应答，正是 `cfgspace_shadow` 与 `bar_controller` 产生的；下一讲先打开影子配置空间，看 `CfgRd/CfgWr` 如何被就地应答成 `CplD`。
- 继续读 [u4-l2 BAR PIO 控制器](u4-l2-bar-controller-engines.md)：本讲 `src64` 搬运的 BAR 命中位 `tuser[8:2]` 在那里被用来分发 BAR 访问请求，看读/写引擎如何拆分请求并拼装 `CplD`。
- 想把本讲的 `clk_pcie` 放进全板时钟图谱，可读 [u5-l1 跨时钟域设计与双时钟 FIFO](u5-l1-clock-domain-crossing.md)，弄清 `src64`/`dst64` 为何不必跨域、而 `src_fifo`/`dst_fifo` 必须。
- 若想验证本讲的位宽转换时序，可参考 [u1-l3 构建](u1-l3-build-and-flash.md) 生成工程后，在 Vivado 仿真里对 `pcileech_pcie_a7` 注入激励，观察 `tlp_rx`→`tlps_rx`、`tlps_tx`→`tlp_tx` 的波形（精确周期级行为以仿真为准）。
