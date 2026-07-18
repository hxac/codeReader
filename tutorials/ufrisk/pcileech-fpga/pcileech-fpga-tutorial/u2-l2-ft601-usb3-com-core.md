# FT601 USB3 通信核心

> 本讲对应大纲：`u2-l2` · 阶段 intermediate · 依赖 `u2-l1`（接口与 modport）
> 主参考工程：`PCIeSquirrel`（Screamer PCIe Squirrel，Artix-7 XC7A35T，PCIe x1 + FT601 USB3）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 FT601 这颗 USB3 桥接芯片在「主机 ↔ FPGA」链路上的角色，以及它那组 `RXF_N / TXE_N / OE_N / RD_N / WR_N` 握手信号的含义。
- 读懂 `pcileech_ft601.sv` 里那个 `IDLE → RX → COOLDOWN` / `IDLE → TX → COOLDOWN` 的状态机，并解释「RX 优先于 TX」「冷却（cooldown）」「空闲填充 `0x66665555`」三件事各解决什么问题。
- 读懂 `pcileech_com.sv` 如何把 FT601 送来的 32 位流拼成 64 位流、如何用 `0x66665555` 重同步、又如何用一个很浅的双时钟 FIFO 把数据从 `clk_com` 时钟域安全搬到 `clk` 系统时钟域。
- 解释 `pcileech_com.sv` 里的 `initial_rx[5]` 数组：为什么要在上电时「伪造」几条主机命令，以及末尾那条 `64'h00000003_80182377` 是怎样把 PCIe 核从复位状态拉上线的。

## 2. 前置知识

本讲默认你已经读过 `u2-l1`（接口与 modport），知道 `IfComToFifo` 这个「契约」定义了 com 与 fifo 之间一条 64 位下行（主机→FPGA）、一条 256 位上行（FPGA→主机）的数据通道。除此之外，再补充三个概念：

- **FT601**：Future Technology Devices（FTDI）出的一颗 USB3 ↔ 并行 FIFO 桥接芯片。对 FPGA 而言，它就是一组 32 位双向数据线 + 几根类似 FIFO 的控制线（「有数据可读吗」「能写吗」）。pcileech-fpga 不实现 USB3 协议本身，协议都由 FT601 硬件处理，FPGA 只需按它的并行接口时序读写。
- **时钟域（clock domain）**：由同一个时钟驱动的一组寄存器称为一个时钟域。把数据从一个时钟域传到另一个，不能直接连线（会采到亚稳态），通常要用「双时钟 FIFO」做隔离。本讲里有两个时钟域：`clk`（100 MHz 系统主时钟）与 `clk_com`（FT601 通信时钟）。
- **字宽转换（width conversion）**：FT601 一次给 32 位，而系统内部按 64 位处理。所以要在 com 模块里把「两个 32 位」拼成「一个 64 位」。反之，上行要把 256 位的包拆成 32 位送出去。

一句话定位：`pcileech_com` 是夹在「物理 USB3 芯片」和「系统路由中枢 fifo」之间的一层**适配层**——它对下用 `pcileech_ft601` 驱动 FT601 的硬件时序，对上提供 64 位/256 位的干净数据流和上电初始化动作。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [PCIeSquirrel/src/pcileech_ft601.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv) | FT601/FT245 并行接口控制器 | RX/TX 状态机、RX 优先、冷却、`0x66665555` 空闲填充 |
| [PCIeSquirrel/src/pcileech_com.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv) | 通信核心适配层 | 32→64 拼装、重同步、跨时钟域、`initial_rx` 上电动作 |
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | 路由与控制中枢 | 仅在解析 `initial_rx` 命令字时引用（MAGIC/命令格式/`rw` 寄存器布局） |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 接口定义 | `IfComToFifo`（com↔fifo 契约） |

> 说明：`pcileech_com.sv` 顶部用条件编译宏在 **FT601 USB3** 与 **RMII 以太网** 两种物理层之间二选一。`PCIeSquirrel` 默认 `ENABLE_FT601`（[pcileech_com.sv:L13](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L13)），本讲只讲 FT601 这条路径；以太网路径留到 `u6-l1` 设备变种再对比。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1 `pcileech_ft601`** —— FT601 物理时序状态机（最底层，直接驱动芯片引脚）。
2. **4.2 `pcileech_com` 的 32→64 拼装与跨时钟域** —— 把 FT601 的 32 位流「升级」成系统要的 64 位流。
3. **4.3 `pcileech_com` 的 `initial_rx` 上电动作** —— 在主机还没说话之前，FPGA 自己给自己发几条命令（本讲主实践所在）。

### 4.1 pcileech_ft601：FT601 物理时序状态机

#### 4.1.1 概念说明

FT601 把 USB3 上的字节流映射成一组「FT245 风格」的并行 FIFO 接口。FPGA 侧看到的引脚（[pcileech_ft601.sv:L12-L30](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L12-L30)）：

| 方向 | 信号 | 含义（低有效） |
| --- | --- | --- |
| 输入 | `FT601_RXF_N` | **R**x **F**IFO 非空：为 0 表示 FT601 里有来自主机的数据可读 |
| 输入 | `FT601_TXE_N` | **T**x **E**mpty：为 0 表示 FT601 的发送 FIFO 可以接收 FPGA 要发的数据 |
| 输出 | `FT601_OE_N` | 输出使能（读方向）：为 0 时 FPGA 驱动读时序 |
| 输出 | `FT601_RD_N` | 读脉冲：拉低一拍，从 FT601 取一个 32 位字 |
| 输出 | `FT601_WR_N` | 写脉冲：拉低一拍，向 FT601 写一个 32 位字 |
| 输出 | `FT601_SIWU_N` | Send Immediate / Wake-Up：本工程常驻 1，不使用 |
| 双向 | `FT601_DATA[31:0]` | 32 位数据总线（读时为输入，写时为输出） |
| 输出 | `FT601_BE[3:0]` | 字节使能，写时全 1 |

`pcileech_ft601` 这个模块的唯一职责，就是用一个小状态机把这些握手信号排成正确的时序：什么时候去读、什么时候去写、读和写冲突时谁先。它**完全运行在 `clk_com` 上**（见 com 里的例化 [.clk(clk_com)](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L192)），向 com 暴露的是干净的 32 位 `dout/dout_valid`（收）和 `din/din_wr_en/din_req_data`（发）。

#### 4.1.2 核心流程

状态机一共 13 个状态（[pcileech_ft601.sv:L41-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L41-L52)），分两组：

```
        ┌─────────── IDLE ───────────┐
        │  RXF_N==0 ?  → RX_WAIT1     │   ← RX 优先
        │  else TXE_N==0 && 有排队 ?   │
        │       → TX_WAIT1            │
        └────────────────────────────┘
   RX 分支                                TX 分支
   IDLE → RX_WAIT1 → RX_WAIT2            IDLE → TX_WAIT1 → TX_WAIT2
        → RX_WAIT3 → RX_ACTIVE                → TX_ACTIVE
        → RX_COOLDOWN1 → RX_COOLDOWN2         → TX_COOLDOWN1 → TX_COOLDOWN2
        → IDLE                                 → IDLE
```

三条关键设计：

1. **RX 优先于 TX**（[L144-L148](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L144-L148)）：在 `IDLE` 里先判 `RXF_N`，只有「没有可读数据」时才去看 `TXE_N`。这样主机的命令永远比 FPGA 的上行数据先被取走，避免控制延迟。
2. **冷却态（COOLDOWN）**：每次 RX 或 TX 突发结束后，强制空转两拍（`COOLDOWN1/2`）再回 `IDLE`，给 FT601 内部 FIFO 一个缓冲、也避免读/写时序紧挨导致建立时间不足。
3. **空闲填充 `0x66665555`**（[L88-L96](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L88-L96)）：当发送队列空了 16 拍（`data_cooldown_count==4'hf`），把 5 个输出槽全部填成同步字 `0x66665555`。这个字是主机与 FPGA 约定的「无意义填充」，4.2 节会看到 com 端专门识别它来做重同步。

#### 4.1.3 源码精读

**RX 数据通路（字节序交换）。** FT601 物理总线上的字节序和 FPGA 内部相反，所以读进来要做一次字节倒序（[L67-L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L67-L74)）：

```systemverilog
dout_valid  <= !rst && !FT601_RXF_N && (state == `S_FT601_RX_ACTIVE);
dout[7:0]   <= FT601_DATA[31:24];   // 最高字节送到最低字节
dout[15:8]  <= FT601_DATA[23:16];
dout[23:16] <= FT601_DATA[15:8];
dout[31:24] <= FT601_DATA[7:0];
```

只有在 `RX_ACTIVE` 且 `RXF_N==0` 时 `dout_valid` 才拉高，保证 com 收到的每个有效字都真实来自主机。

**TX 端的 5 深队列与驱动。** FT601 写侧用一个 5 项的小数组 `FT601_DATA_OUT[5]` 当队列，`data_queue_count` 记录已排队数（[L54-L59](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L54-L59)）。三个关键派生信号（[L82-L85](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L82-L85)）：

```systemverilog
assign din_req_data = !rst && ((data_queue_count == 2) || (data_queue_count == 3));
assign FWD          = !rst && !FT601_TXE_N && (data_queue_count != 0)
                          && (state == `S_FT601_TX_ACTIVE);
```

- `din_req_data`：队列只剩 2~3 项时，向上游（com）要更多数据——既不让队列空（否则 FT601 会等到冷却填充），也不让它溢出。
- `FWD`：在 `TX_ACTIVE` 且 FT601 可写且队列非空时，本拍把队首 `FT601_DATA_OUT[0]` 推到总线，整队前移（[L100-L113](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L100-L113)）。

写出去时同样做字节倒序（[L83](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L83)），与 RX 对称。

**主控信号生成。** `OE_N/OE/RD_N/WR_N` 都是组合出来的，且都把 `rst` 和 `RXF_N` 作为前提（[L126-L132](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L126-L132)）。注意 `FT601_WR_N` 只有在「非复位 + FT601 可写 +（在 `TX_WAIT2` 建立期 或 在 `TX_ACTIVE` 且确有数据可写）」时才拉低，避免写空。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「RX 优先」与「冷却」在状态机里的具体体现。

**步骤**：

1. 打开 [pcileech_ft601.sv:L134-L179](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L134-L179) 的状态机 `case` 块。
2. 在 `S_FT601_IDLE`（[L144-L148](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L144-L148)）里数清楚 `if/else if` 的顺序。
3. 在 `S_FT601_RX_ACTIVE`（[L160-L161](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_ft601.sv#L160-L161)）看 `RXF_N` 变 1 时跳到哪。

**需要观察的现象**：

- `IDLE` 里第一个 `if` 判的是 `!FT601_RXF_N`（有数据可读），所以**即便同时有数据要发，也会先服务 RX**。
- RX 突发结束不是直接回 `IDLE`，而是经 `RX_COOLDOWN1 → RX_COOLDOWN2 → IDLE` 两拍冷却。

**预期结果**：你能用一句话说出「为什么主机发来的命令不会被 FPGA 的上行流量阻塞」——因为 IDLE 判优里 RX 在前。

#### 4.1.5 小练习与答案

**练习 1**：`din_req_data` 为何在 `data_queue_count==2 || ==3` 时才拉高，而不是 `==0` 时？

**参考答案**：等到队列快空（==0）再要数据，FT601 写时序会断流、触发 4.1.2 的冷却填充；提前到还剩 2~3 项时就预取，能让队列在「被 FWD 消费」和「被 com 补充」之间保持一个稳定的水位，避免空窗。剩太多（>3）又可能溢出这 5 深队列，所以卡在 2~3。

**练习 2**：`0x66665555` 这个填充字为什么不用 `0x00000000` 或 `0xFFFFFFFF`？

**参考答案**：`0x66665555` 是一个在数据流中极不可能自然出现的「特征字」（0/1 均衡、无长连 0/连 1，利于线路时钟恢复和模式匹配）。com 端专门拿连续两个这样的字来重置 32→64 拼装状态（见 4.2）。若用全 0/全 1，既容易和真实数据混淆，也不利于直流平衡。

---

### 4.2 pcileech_com：32→64 位拼装、重同步与时钟域跨越

#### 4.2.1 概念说明

FT601 给的是 32 位流，但 `IfComToFifo` 契约要求 com 向 fifo 提供 64 位下行数据（`com_dout[63:0]`，见 [pcileech_header.svh:L19-L20](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L19-L20)）。原因有二：一是 fifo 里的命令、TLP 等结构本就按 64 位组织；二是拼成 64 位后数据率减半，跨时钟 FIFO 更从容。

`pcileech_com` 的下行（RX，主机→FPGA）通路要做三件事：

1. **拼装**：把两个连续的 32 位字拼成一个 64 位字。
2. **重同步**：万一拼装边界错位（上电、掉电瞬间很常见），要能靠 `0x66665555` 重新对齐。
3. **跨时钟域**：把数据从 `clk_com` 域搬到 `clk` 域。

#### 4.2.2 核心流程

下行 RX 数据通路（[pcileech_com.sv:L99-L135](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L99-L135)）：

```
FT601 dout[31:0]            （clk_com 域，32 位）
      │
      ▼
 [32→64 拼装 + 重同步]        com_rx_data64 / com_rx_valid64_dw  （clk_com 域）
      │  条件：收到第 1 个 32 位字 → dw=2'b01
      │        收到第 2 个 32 位字 → dw=2'b11（com_rx_valid64=1）
      │        连续两个 0x66665555  → dw 复位为 2'b00（重同步）
      ▼
 fifo_64_64_clk2_comrx        wr_clk=clk_com, rd_clk=clk  （跨时钟域）
      │
      ▼
 com_rx_dout[63:0]            （clk 域，交给 fifo）
```

**跨时钟安全的前提**（com 文件头注释 [L92-L97](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L92-L97)）：

\[ \text{需要满足} \quad 2 \cdot f_{clk\_com} < f_{clk} \]

因为 32→64 拼装把有效数据率减半，所以 64 位流的最大到达频率是 `f_clk_com / 2`。只要它低于读侧 `f_clk`，那个「非常浅（very shallow）」的 FIFO 就不会溢出。这是一个静态的、靠器件选型保证的条件，不需要运行时流控。

#### 4.2.3 源码精读

**32→64 拼装与重同步**（[L107-L119](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L107-L119)）：

```systemverilog
always @ ( posedge clk_com )
    if ( rst | (~com_rx_valid32 & com_rx_valid64_dw[0] & com_rx_valid64_dw[1]) )
        com_rx_valid64_dw <= 2'b00;                       // 复位 / 消费掉一个完整字
    else if ( com_rx_valid32
              && (com_rx_data32 == 32'h66665555)
              && (com_rx_data64[31:0] == 32'h66665555) )
        com_rx_valid64_dw <= 2'b00;                       // 重同步：连续两个同步字
    else if ( com_rx_valid32 ) begin
        com_rx_data64 <= (com_rx_data64 << 32) | com_rx_data32;  // 左移并入
        com_rx_valid64_dw <= (com_rx_valid64_dw == 2'b01) ? 2'b11 : 2'b01;
    end
```

`com_rx_valid64_dw` 是一个 2 比特的小状态机：

| `dw` 值 | 含义 | `com_rx_valid64 = dw[0] & dw[1]` |
| --- | --- | --- |
| `2'b00` | 空闲，等待第 1 个 32 位字 | 0 |
| `2'b01` | 已收第 1 个（低 32 位），等第 2 个 | 0 |
| `2'b11` | 两个都到齐，64 位字有效 | 1 |

重同步分支的意义：如果边界错位了半个字（比如把某个真实数据的后半截当成了新字的开始），主机只需发送一串 `0x66665555`，当 com 看到「刚收到的 32 位」和「上一个 32 位」都是同步字时，就把 `dw` 打回 `00`，下一个真实数据就从干净的低 32 位开始拼。这和 4.1 里 ft601 在空闲时主动填充 `0x66665555` 是**配对的**——一个负责发、一个负责识别。

**跨时钟 FIFO**（[L121-L132](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L121-L132)）：

```systemverilog
fifo_64_64_clk2_comrx i_fifo_64_64_clk2_comrx(
    .rst    ( rst | (tickcount64_com<2) ),
    .wr_clk ( clk_com ),  .rd_clk ( clk ),
    .din    ( com_rx_data64 ),  .wr_en ( com_rx_valid64 ),
    .rd_en  ( 1'b1 ),            .dout  ( com_rx_dout ),
    ...
);
```

`rd_en` 恒为 1：只要 FIFO 里有数据就读。复位条件 `rst | (tickcount64_com<2)` 多了一个「上电头两拍」保护，让 `clk_com` 域的计数器先稳定。

**输出选择**（[L134-L135](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L134-L135)）：

```systemverilog
assign dfifo.com_dout       = initial_rx_valid ? initial_rx_data : com_rx_dout;
assign dfifo.com_dout_valid = initial_rx_valid | com_rx_valid;
```

这里把「上电虚拟命令」和「真实下行数据」合并到同一条 `com_dout` 上——上电初期优先走 `initial_rx`，之后交给真实通路。这是下一节 4.3 的入口。

> 上行（TX，FPGA→主机）通路结构对称：fifo 给的 256 位包先经 `fifo_256_32_clk2_comtx`（`clk`→`clk_com`，256→32）再经 `fifo_32_32_clk1_comtx` 缓冲，最后交给 ft601 发送（[L157-L183](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L157-L183)）。`com_din_ready` 用前一级的 `almost_full` 反压 fifo，`led_state_txdata = com_tx_prog_full ^ led_state_invert` 把发送水位变成 LED 闪烁（[L154-L155](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L154-L155)）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：验证「32→64 拼装」与「重同步」这两个分支互不干扰。

**步骤**：

1. 读 [pcileech_com.sv:L107-L119](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L107-L119)。
2. 假设主机依次发来 4 个 32 位字：`A`、`B`、`0x66665555`、`0x66665555`、然后 `C`、`D`。在纸上逐拍追踪 `com_rx_data64` 和 `com_rx_valid64_dw`。

**需要观察的现象**：

- `A`、`B` 被拼成 `{B,A}`，在 B 到来那一拍 `com_rx_valid64` 拉高一次。
- 连续两个 `0x66665555` 出现时，`dw` 被打回 `00`，**且这两个同步字本身不会作为一个有效 64 位字上送**（它们只用于对齐）。
- `C`、`D` 从干净状态重新拼成 `{D,C}`。

**预期结果**：你得到的 64 位有效序列是 `{B,A}`、`{D,C}`，中间的同步字被「吃掉」用于复位边界。如果删掉那两个同步字而边界又恰好错位，`{B,A}` 可能错成 `{?,B}`——这正是重同步要消除的故障。

#### 4.2.5 小练习与答案

**练习 1**：跨时钟 FIFO 的 `rd_en` 接了常 `1`，为什么不会读空导致错误？

**参考答案**：双时钟 FIFO 的 `valid`/`empty` 信号会反映是否真的有数据。`rd_en=1` 只是「只要非空就读」的意愿；当 FIFO 空时读操作不会产生有效 `dout`（由 `com_rx_valid` 体现）。由于 `2*f_clk_com < f_clk`，读侧比写侧快，FIFO 不会堆积溢出，所以无需复杂流控，恒定读即可。

**练习 2**：为什么重同步条件要求「连续两个」`0x66665555`，而不是一个？

**参考答案**：单个 `0x66665555` 有可能恰好是真实数据流里的某个 32 位字（虽然概率低）。要求连续两个、且「当前收到的」和「上一个锁存的」都是同步字，大幅降低误触发。两个连续特征字在真实业务数据里几乎不可能出现，可以作为可靠的对齐锚点。

---

### 4.3 pcileech_com 的 initial_rx：上电虚拟命令与 PCIe 核上线

#### 4.3.1 概念说明

PCIe 核上线（脱离复位、开始链路训练）这件事，在 pcileech-fpga 里**不是自动发生**的，而是由 fifo 的 `rw` 寄存器里一个控制位 `PCIE CORE RESET` 决定（见 [pcileech_fifo.sv:L287](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287) 与 [L311](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L311)）。而那个寄存器位的默认值是 **1（复位保持）**。

这就带来一个先有鸡还是先有蛋的问题：上电瞬间主机软件（PCILeech/LeechCore）还没来得及通过 USB 发命令，FPGA 的 PCIe 核就一直被按在复位里。`initial_rx` 机制就是来解决它的——**让 com 模块在上电头几个时钟里，假装主机发来了几条预设命令**，喂给 fifo 去执行。

代码注释（[pcileech_com.sv:L56-L62](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L56-L62)）原话：有时需要在 PCIe 核上线之前先做一些动作（比如设置 DRP 值），就可以在下面填「虚拟的 COM-core 初始发送值」。

#### 4.3.2 核心流程

`initial_rx` 的注入节奏由系统时钟 `clk` 下的自由计数器 `tickcount64` 控制（[L81-L90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L81-L90)）：

```
tickcount64:   0..15        → 还未开始（等系统稳定）
               16           → 注入 initial_rx[0]
               17           → 注入 initial_rx[1]
               ...
               16+N-1       → 注入 initial_rx[N-1]
               >= 16+N      → initial_rx_valid=0，交棒给真实 USB 数据
```

关键式（[L89-L90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L89-L90)）：

\[ \text{initial\_rx\_valid} = \neg\text{rst} \ \land \ 16 \le \text{tickcount64} < 16 + N \]

其中 \(N = \$\text{size}(\text{initial\_rx}) = 5\)。也就是上电后第 16~20 拍，每拍「虚构」一条 64 位命令，经 [L134-L135](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L134-L135) 的复用器送上 `com_dout`，fifo 完全把它当成真实主机命令来解析。

数组定义（[L63-L79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L63-L79)）：前 4 项是 `0`（占位，留给用户填自定义动作），第 5 项（最后一条）是 `64'h00000003_80182377`，注释明确写着「在 DRP 与 Config 动作完成之后、发送 PCIe TLP 之前，把 PCIe 核从热复位状态拉上线」。

#### 4.3.3 源码精读（命令字段的完整解码）

要理解末尾那条 `64'h00000003_80182377`，得先看 fifo 如何识别一条命令。fifo 在下行数据里找**魔术字节**和**类型字段**（[pcileech_fifo.sv:L65-L71](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L71)）：

```systemverilog
`define CHECK_MAGIC     (dcom.com_dout[7:0] == 8'h77)   // 低字节必须是 0x77
`define CHECK_TYPE_CMD  (dcom.com_dout[9:8] == 2'b11)   // 类型字段=11 → 命令
wire _cmd_rx_wren = dcom.com_dout_valid & `CHECK_MAGIC & `CHECK_TYPE_CMD;
```

把 `64'h00000003_80182377` 的低 32 位 `0x80182377` 按字段拆开：

| 位段 | 值 | 字段 | 含义 |
| --- | --- | --- | --- |
| `[7:0]` | `0x77` | MAGIC | 命令包头魔术字，通过 `CHECK_MAGIC` |
| `[9:8]` | `2'b11` | TYPE | 命令类型（00=TLP, 01=CFG, 10=Loop, **11=CMD**） |
| `[12]` | `0` | 读标志 | `in_cmd_read`（[L353](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L353)）=0，本条不是读 |
| `[13]` | `1` | 写标志 | `in_cmd_write`（[L354](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L354)）=1，**本条是写** |
| `[31:16]` | `0x8018` | 地址+标志 | 见下 |

地址字段 `in_cmd_address_byte = [31:16] = 0x8018`（[L346](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L346)）继续拆（[L350-L351](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L350-L351)）：

| 位（在 16 位地址字内） | 值 | 含义 |
| --- | --- | --- |
| `[15]`（=`f_rw`） | `1` | 目标是读写寄存器文件 `rw`（0 则是只读 `ro`） |
| `[14]`（=`f_shadowcfgspace`） | `0` | 不是访问影子配置空间 |
| `[14:0]` | `0x0018` | 字节地址 = 24（0x18） |

字节地址 0x18 经 `{addr[14:0], 3'b000}` 换算成比特偏移（[L347](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L347)）：\(24 \times 8 = 192\)，即指向 `rw[192 +\!:\! 16]`（覆盖字节 0x18、0x19）。

最后看高 32 位 `0x00000003`，它编码「写什么」和「写哪几位」（[L348-L349](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L348-L349)）：

```systemverilog
wire [15:0] in_cmd_value = {cmd_rx_dout[48+:8], cmd_rx_dout[56+:8]};  // = 0x0000
wire [15:0] in_cmd_mask  = {cmd_rx_dout[32+:8], cmd_rx_dout[40+:8]};  // = 0x0300
```

- `in_cmd_mask = 0x0300`：只写一个 16 位字里的第 8、9 位（即 `rw[200]`、`rw[201]`）。
- `in_cmd_value = 0x0000`：把这两位写成 0。

写执行在 [L405-L410](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L405-L410)，按掩码逐位写入。

那么 `rw[200]`、`rw[201]` 是什么？查 `rw` 初始化（[L286-L294](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L286-L294)）：

```systemverilog
rw[200] <= 1'b1;   // +019: PCIE CORE RESET        ← 上电默认保持复位！
rw[201] <= 1'b0;   //       PCIE SUBSYSTEM RESET
```

而它们如何影响硬件（[L310-L312](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L310-L312)）：

```systemverilog
_pcie_core_config <= rw[207:128];
assign dpcie.pcie_rst_core   = _pcie_core_config[72];  // = rw[200]
assign dpcie.pcie_rst_subsys = _pcie_core_config[73];  // = rw[201]
```

**串起来**：上电时 `rw[200]=1` → `pcie_rst_core=1` → PCIe 核被按在复位；`initial_rx` 末条 `64'h00000003_80182377` 在上电第 20 拍被当作「写命令」执行，把 `rw[200]`、`rw[201]` 都写成 0 → `pcie_rst_core=0` → **PCIe 核脱离复位、开始上线**。这就是注释里「Bring the PCIe core online」的全部含义。

> 字段语法的权威来源是 LeechCore 项目的 `device_fpga.c`（com 文件头 [L69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L69) 明确指向它）。上面的逐位结论可以直接在本仓库的 fifo.sv 里验证，无需翻看 LeechCore。

#### 4.3.4 代码实践（本讲主实践 · 源码阅读 + 改参数观察）

**目标**：亲手验证 `64'h00000003_80182377` 是一条「写 `rw`、解除 PCIe 核复位」的命令，并预测若修改它会发生什么。

**步骤**：

1. 打开 [pcileech_com.sv:L63-L79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L63-L79)，确认 `initial_rx[4]`（第 5 项）= `64'h00000003_80182377`。
2. 对照 [pcileech_fifo.sv:L65-L71](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L71) 验证 `[7:0]=0x77`、`[9:8]=11`，所以它进入 CMD 通路。
3. 对照 [L346-L354](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L346-L354) 复算：地址 `0x8018` → `f_rw=1`、字节地址 0x18 → `rw[192+:16]`；掩码 `0x0300`、值 `0x0000`。
4. 对照 [L287](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287) 与 [L311](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L311)，确认被写 0 的两位正是 `PCIE CORE RESET` 与 `PCIE SUBSYSTEM RESET`。
5. **思想实验（不实际改源码）**：如果把这条改成 `64'h00000000_00000000`（即不复位 PCIe 核），按上面的链路推断 FPGA 上电后的 PCIe 行为。

**需要观察的现象 / 预期结果**：

- 正常固件：上电后 LED 上 `led_pcie`（见 `u1-l4`）会在 PCIe 核上线后点亮，主机 `lspci` 能枚举到设备。
- 思想实验改空后：`rw[200]` 保持上电默认值 `1`，`pcie_rst_core` 始终为 1，PCIe 核永远不脱离复位——`lspci` 看不到任何设备，`led_pcie` 不亮。这反证了这条命令的必要性。

> ⚠️ 本实践为**源码阅读型**，上述「改空」仅为推断，不要实际修改并烧录去验证（会让板卡 PCIe 不再上线，且本讲义禁止改源码）。若确需硬件验证，应在本地另有备份的前提下进行，结果记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`initial_rx` 一共 5 项，但前 4 项都是 `0`。一个全 0 的 64 位字会被 fifo 当成命令执行吗？

**参考答案**：不会。fifo 的入口先过 `CHECK_MAGIC`（要求低字节 `==0x77`）和 `CHECK_TYPE_*`，全 0 字的低字节是 `0x00`，过不了魔术字检查，四种类型使能（`_cmd_rx_wren` 等）都不会拉高。所以前 4 个全 0 项是安全的「空操作占位」，留给用户填自定义动作（如先做 DRP 写再上线）。

**练习 2**：为什么「解除 PCIe 核复位」要放在 `initial_rx` 的**最后一条**，而不是第一条？

**参考答案**：注释（[L75-L78](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L75-L78)）说得很清楚：理想顺序是「先完成 DRP/Config 动作，再发 PCIe TLP 前」上线。因为 PCIe 核一旦脱离复位就开始链路训练并向主机暴露配置空间；如果需要在上线前用 DRP 改核内部参数（比如改链路速率、改 ID），就必须先发那些 DRP 写命令（填在前 4 项里），最后才解除复位。把上线放最后，给前面的配置动作留出了窗口。

**练习 3**：`initial_rx_valid` 的窗口是 `tickcount64` 在 `[16, 16+N)`。为什么要跳过前 16 拍？

**参考答案**：上电瞬间系统刚脱离硬复位，时钟、双时钟 FIFO、`_pcie_core_config` 等寄存器都还在收敛（注意 comrx FIFO 的复位还额外要求 `tickcount64_com>=2`）。等 16 拍（100 MHz 下 160 ns）让系统稳定后再注入虚拟命令，避免在未稳定期把命令喂进 fifo 造成误执行。

---

## 5. 综合实践

**任务**：画一张完整的「主机 → FPGA 上电自举」时序—数据通路图，把本讲三个模块串起来。

要求在你的图里至少体现以下要素，并附上一段文字说明：

1. **物理层**（ft601）：`RXF_N/TXE_N` 握手、RX 优先、冷却、空闲填 `0x66665555`。
2. **适配层**（com RX）：32→64 拼装、`0x66665555` 重同步、`clk_com→clk` 双时钟 FIFO、`2*f_clk_com < f_clk` 前提。
3. **上电注入**（com initial_rx）：`tickcount64` 在 16~20 拍注入 5 条虚拟命令，末条 `64'h00000003_80182377`。
4. **命令落地**（fifo）：魔术字 `0x77` + 类型 11 识别为 CMD → 解析地址 `0x8018` / 掩码 `0x0300` / 值 `0x0000` → 写 `rw[200:201]` → `pcie_rst_core` 拉低 → PCIe 核上线。

**交付**：

- 一张框图（手绘或工具画均可），标出每个数据通段的位宽（32 / 64）、所在时钟域（`clk_com` / `clk`）。
- 一段 150 字以内的说明，解释「为什么上电后即使主机软件还没启动，板卡的 PCIe 也能自己上线」。

**自检**：如果你的说明里能自然提到「`initial_rx` 伪造命令」「解除 `rw[200]` 复位」这两点，就算答到核心了。

## 6. 本讲小结

- `pcileech_ft601` 是纯粹的 FT601 物理时序驱动器：一个 13 状态的小 FSM，**RX 优先于 TX**，突发后走两拍冷却，发送空闲时主动填充 `0x66665555` 同步字。
- `pcileech_com` 的下行通路把 FT601 的 32 位流**拼成 64 位**，用「连续两个 `0x66665555`」做**边界重同步**，再用一个很浅的双时钟 FIFO 从 `clk_com` 搬到 `clk`（前提 `2*f_clk_com < f_clk`）。
- `0x66665555` 是贯穿 ft601（发送）与 com（识别）的配对机制，解决上电/掉电时的字节边界对齐。
- `initial_rx[5]` 让 FPGA 在上电头 5 拍「自己给自己发命令」，解决「主机还没来、PCIe 核得先上线」的先有鸡先有蛋问题。
- 末条 `64'h00000003_80182377` 是一条 CMD 写：地址 `0x8018`（`rw` 寄存器、字节 0x18）、掩码 `0x0300`、值 `0x0000`，作用是把 `rw[200]`（PCIE CORE RESET）清 0，让 PCIe 核脱离复位。
- 命令字的字段语法由 fifo 的 `CHECK_MAGIC` / `in_cmd_*` 解码逻辑定义，权威对照表在 LeechCore 的 `device_fpga.c`，但每一步都能在本仓库 fifo.sv 内验证。

## 7. 下一步学习建议

- 下一讲 **`u2-l3` FIFO 控制中心与 MAGIC 路由**：本讲只用到「CMD 类型=11」，下一讲会完整讲 `pcileech_fifo.sv` 如何用 `CHECK_MAGIC` + `CHECK_TYPE_TLP/CFG/LOOP/CMD` 把 64 位流分拣到四条接收通路——那是 com 交付给 fifo 之后的故事。
- 之后 **`u2-l5` 命令/控制寄存器文件与读写协议**：本讲 4.3 解码了 `0x8018` 这一个命令字，`u2-l5` 会系统讲 `ro`/`rw` 整张寄存器表（DRP、PCIE 状态、过滤开关等），建议读完 `u2-l3`/`u2-l4` 再看。
- 若你对 32↔64、256↔32 这类**位宽转换 + 跨时钟 FIFO** 想系统了解，可跳读 `u5-l1`（跨时钟域设计与双时钟 FIFO）。
- 想对比 FT601 与「以太网」这条可替换通信路径，留到 `u6-l1`（设备变种对比）。
