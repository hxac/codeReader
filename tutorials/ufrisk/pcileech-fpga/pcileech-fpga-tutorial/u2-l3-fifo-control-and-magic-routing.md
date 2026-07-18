# FIFO 控制中心与 MAGIC 路由

## 1. 本讲目标

上一篇（u2-l2）我们走完了「主机 → FT601 → `pcileech_com` → 64 位 `dcom.com_dout` 流」这条下行通路，看到 com 模块最后把一拍拍 64 位数据交到了 `IfComToFifo` 接口上。那么问题来了：**这些 64 位数据到了 fifo 这一头之后，究竟由谁来接、又被送往何方？**

本讲就回答这个问题。我们把 `pcileech_fifo` 这一块「夹在中间」的模块彻底打开。读完本讲，你应当能够：

1. 看懂 `CHECK_MAGIC` / `CHECK_TYPE_*` 这一组宏，如何仅凭 64 位数据里的 **MAGIC 字节** 和 **2 位 type 字段**，把数据分流到 **TLP / CFG / Loopback / Command** 四条接收通路。
2. 画出接收方向（FT601 → 四路 FIFO）和发送方向（多路 → 256 位 → FT601）的完整数据通路，并指出每条通路上的 FIFO 缓冲。
3. 理解 `pcileech_fifo` 不仅是「路由器」，还是整个系统的 **控制中枢**：它维护一份 ro / rw 寄存器文件、解析命令包、驱动 PCIe 核复位与 DRP，甚至能通过 STARTUPE2 原语触发整片 FPGA 的全局复位。

本讲的深入版本（多路复用器内部、寄存器文件完整协议）分别放在 u2-l4 与 u2-l5；本讲负责建立 **整体数据通路 + 控制中枢** 的全景图。

## 2. 前置知识

在进入源码前，先用大白话把几个关键术语讲清楚。

- **MAGIC（魔数）**：在数据流里约定一个「固定取值」的字节作为标记。这里规定：每个 64 位数据字的最低字节 `[7:0]` 若等于 `0x77`，就被识别为一个「有效帧」。它的作用类似网络帧里的「帧起始定界符」——只有打上这个标记的数据才会被接收端认领。
- **type 字段**：MAGIC 之后的 2 个比特 `[9:8]`，用来区分这个帧属于哪一类业务（TLP / CFG / Loopback / Command）。4 种取值正好对应 4 条通路。
- **FIFO（先进先出队列）**：一段带「写口」和「读口」的缓冲存储。写入端把数据塞进去，读出端按写入顺序取走，读写可以不在同一时刻、甚至不在同一时钟下进行（双时钟 FIFO）。本模块里大量使用 FIFO 来做「分流后的缓冲」和「跨时钟域搬运」。
- **多路复用器（Mux）**：把多个数据来源按 **优先级** 合并到一条输出上的器件。本讲的发送方向用一个 8 输入的 mux 把多路 32 位数据合并成 256 位大包。
- **寄存器文件（register file）**：一组可被主机读写的配置位。本模块维护两份：`ro`（read-only，设备状态回读）和 `rw`（read-write，主机可改写以控制设备行为）。
- **路由（routing）**：本讲的「路由」特指「根据帧头标记，把同一根管子里来的数据分发到不同去向」，和互联网路由器是同一个思想，只是粒度在「数据帧」一级。

承接 u2-l1：com 与 fifo 之间用 `IfComToFifo` 这条「契约电缆」相连，fifo 侧的 modport 是 `mp_fifo`——也就是说，fifo 在这条电缆上是 `com_dout`（下行 64 位）的 **消费者**、`com_din`（上行 256 位）的 **生产者**。本讲会反复用到这个方向感。

## 3. 本讲源码地图

本讲围绕一个核心文件，并引用另外两个文件作为契约与下游说明：

| 文件 | 作用 | 本讲用到的地方 |
|------|------|----------------|
| [PCIeSquirrel/src/pcileech_fifo.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv) | **本讲主角**：系统的路由 + 控制中枢 | 全文精读 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 5 个接口的契约定义（u2-l1 已讲） | 确认端口方向 |
| [PCIeSquirrel/src/pcileech_mux.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv) | 发送方向的 8 输入多路复用器（u2-l4 深讲） | 理解优先级与 256 位打包 |

## 4. 核心概念与源码讲解

### 4.1 模块全景：fifo 是「路由器 + 控制台」

#### 4.1.1 概念说明

先建立一个直觉：`pcileech_fifo` 是整块板卡的 **中央调度室**。它对外通过 **5 条 interface** 与世界相连：

- `dcom`（`IfComToFifo`）：连 com，唯一一条通向主机（经 FT601）的管道。
- `dcfg`（`IfPCIeFifoCfg`）：连 PCIe 配置空间管理。
- `dtlp`（`IfPCIeFifoTlp`）：连 PCIe TLP 收发，4 路并行。
- `dpcie`（`IfPCIeFifoCore`）：连 PCIe 核的复位与 DRP。
- `dshadow2fifo`（`IfShadow2Fifo`）：连配置空间影子 / BAR 控制器。

可以看到：**所有去向 PCIe 的路，都要经过 fifo；所有去向主机的路，也要经过 fifo。** 这就是「控制中枢」的含义。

#### 4.1.2 核心流程

fifo 的职责可以归纳为「一进一出 + 一份寄存器」：

1. **进（接收）**：从 `dcom.com_dout` 取 64 位数据，按 MAGIC/type 路由到 4 条通路（详见 4.2）。
2. **出（发送）**：把 4 条通路 + Loopback/Command 的回送数据，经多路复用器合并成 256 位，交还 `dcom.com_din`（详见 4.3）。
3. **控制台**：维护 ro/rw 寄存器文件，解析命令包，驱动 PCIe 复位、DRP、全局复位（详见 4.4、4.5）。

#### 4.1.3 源码精读

模块声明与 5 条 interface 端口在文件开头一目了然——注意每个端口都带 `.mp_fifo` / `.fifo` 这样的 modport，锁定了 fifo 侧的信号方向：

模块端口（含 5 条 interface）—— [pcileech_fifo.sv:16-35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L16-L35)：声明了 `dcom`、`dcfg`、`dtlp`、`dpcie`、`dshadow2fifo` 五个接口连接，外加 `clk`、`rst`、`rst_cfg_reload`、`pcie_present`、`pcie_perst_n` 等顶层控制信号。

模块内还有一个自由奔跑的 64 位计数器 `tickcount64`，它是后续「节拍采样」「不活动计时」「上电初始化窗口」的公共时钟基准—— [pcileech_fifo.sv:41-43](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L41-L43)。

### 4.2 接收方向：MAGIC 路由与四路分流

#### 4.2.1 概念说明

主机发来的所有数据，无论要读写内存、还是要配置 PCIe、还是只是回环测试，都挤在 **同一条 64 位管道** `dcom.com_dout` 里。fifo 必须在每一拍判断：「这一拍的数据该交给谁？」

办法就是 **给每帧数据贴标签**：最低字节固定为 MAGIC `0x77` 表示「我是一个有效帧」，紧接着的 2 位 type 表示「我属于哪一类」。fifo 用一组组合逻辑（不是状态机！）实时判定，把这一拍数据接到对应的接收通路上。

#### 4.2.2 核心流程

```
              dcom.com_dout (64 bit) + com_dout_valid
                        │
            ┌───────────▼───────────┐
            │  [7:0]==0x77 ? (MAGIC)│ ─── 否 ──→ 丢弃（没人接收）
            └───────────┬───────────┘
                   是   │
            ┌───────────▼───────────┐
            │  [9:8] = type 字段     │
            └─┬───────┬───────┬──────┬─┘
              │00     │01     │10    │11
             TLP     CFG   Loopback Command
            32bit   64bit   34bit  64bit
              │       │       │      │
           dtlp.tx  dcfg.tx  fifo   fifo
                             _loop  _cmd_rx
```

四条通路的「数据宽度」并不相同，作者在源码顶部的注释里画得很清楚：TLP/Loopback 只取高 32 位载荷，CFG/Command 保留完整 64 位。

#### 4.2.3 源码精读

**作者画的接收分流示意图**—— [pcileech_fifo.sv:45-60](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L45-L60)：这是理解整段代码的「总纲」，建议把它截图常备。

**MAGIC 与 type 判定宏**—— [pcileech_fifo.sv:65-69](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L69)：

```systemverilog
`define CHECK_MAGIC     (dcom.com_dout[7:0] == 8'h77)
`define CHECK_TYPE_TLP  (dcom.com_dout[9:8] == 2'b00)
`define CHECK_TYPE_CFG  (dcom.com_dout[9:8] == 2'b01)
`define CHECK_TYPE_LOOP (dcom.com_dout[9:8] == 2'b10)
`define CHECK_TYPE_CMD  (dcom.com_dout[9:8] == 2'b11)
```

注意这 5 个全是 **文本宏**（`define），不是变量。它们会被展开进下面的 `assign` 里，最终综合成纯组合比较器——零时钟开销。

**用宏把数据接到 4 条通路**—— [pcileech_fifo.sv:71-74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L71-L74)：

```systemverilog
assign dtlp.tx_valid = dcom.com_dout_valid & `CHECK_MAGIC & `CHECK_TYPE_TLP;
assign dcfg.tx_valid = dcom.com_dout_valid & `CHECK_MAGIC & `CHECK_TYPE_CFG;
wire   _loop_rx_wren = dcom.com_dout_valid & `CHECK_MAGIC & `CHECK_TYPE_LOOP;
wire   _cmd_rx_wren  = dcom.com_dout_valid & `CHECK_MAGIC & `CHECK_TYPE_CMD;
```

每一行都是同一个套路：`有效 & 是MAGIC & 是某type`。于是同一时刻最多只有一条通路被点亮——天然互斥，不需要仲裁。

**各通路取走的载荷**—— [pcileech_fifo.sv:77-80](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L77-L80)：

```systemverilog
assign dtlp.tx_data = dcom.com_dout[63:32];   // TLP：只要高 32 位
assign dtlp.tx_last = dcom.com_dout[10];      // TLP：bit10 标记包尾
assign dcfg.tx_data = dcom.com_dout;          // CFG：完整 64 位
```

TLP 用 `bit[10]` 当「包末拍」标志（一个 TLP 由多个 32 位 DWORD 组成，最后一拍拉高 `tx_last`）。Loopback 与 Command 则把数据写入各自的 FIFO，写入信号就是上面的 `_loop_rx_wren` / `_cmd_rx_wren`。

**Loopback FIFO 的写入数据**—— [pcileech_fifo.sv:108-119](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L108-L119)：注意它的 `din` 是 `{dcom.com_dout[11:10], dcom.com_dout[63:32]}`，即 **2 位上下文 + 32 位载荷 = 34 位**。这 2 位上下文（来自 `bit[11:10]`）会随数据一起存进 FIFO，发回去时原样带回，用来在 256 位包里标记 first/last。

**Command 接收 FIFO**—— [pcileech_fifo.sv:333-343](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L333-L343)：`i_fifo_cmd_rx` 把 **完整 64 位** `dcom.com_dout` 缓存起来（`fifo_64_64_clk1_fifocmd`），后面命令解析状态机再慢慢消费它。

#### 4.2.4 代码实践

> **实践目标**：把 MAGIC=0x77 时的 type 分流关系整理成一张表 + 一张示意图，亲手验证 4.2.2 的流程图。

**操作步骤**：

1. 打开 [pcileech_fifo.sv:65-74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L74)，确认 5 个宏的取值。
2. 填写下面的分流表（答案见 4.2.5）：

   | `dcom.com_dout[9:8]` | type 名称 | 点亮的接收信号 | 取走的载荷 | 去向 |
   |---|---|---|---|---|
   | `00` | ? | `dtlp.tx_valid` | 高 32 位 | PCIe TLP 发送 |
   | `01` | ? | ? | ? | ? |
   | `10` | ? | ? | ? | ? |
   | `11` | ? | ? | ? | ? |

3. 画一张示意图：一个 `dcom.com_dout` 节点，向下分出 4 条支路，每条支路标上 type 值与去向 FIFO。

**需要观察的现象**：四条通路的 `wr_en`/`valid` 信号在同一拍 **互斥**（因为 type 字段只能取一个值）。

**预期结果**：分流表如下——`00`→TLP、`01`→CFG、`10`→Loopback、`11`→Command；其中 TLP 与 Loopback 只取高 32 位，CFG 与 Command 保留 64 位。这与作者注释图（L53-59）完全一致。

> 说明：本实践为「源码阅读型」，无需硬件；若要在仿真中观察，需自建 testbench 驱动 `dcom.com_dout`，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果某一拍 `dcom.com_dout[7:0] != 0x77`（即不是 MAGIC），会发生什么？

<details><summary>参考答案</summary>

`CHECK_MAGIC` 为假，4 条 `assign`/`wire` 全部为 0，没有任何通路接收这一拍数据——它被静默丢弃。这正是 MAGIC 的「门禁」作用：只有打上 `0x77` 标记的帧才被认领。
</details>

**练习 2**：为什么 TLP 通路需要一个 `tx_last = dcom.com_dout[10]`，而 CFG 通路却没有？

<details><summary>参考答案</summary>

一个 TLP（事务层包）由 **多个 32 位 DWORD** 拼成，接收端必须知道哪一拍是「最后一拍」才能把包边界对齐，所以借用 `bit[10]` 当末拍标志。CFG 帧是 **定长 64 位** 的单拍配置事务，一拍即一个完整事务，不需要末拍标志。
</details>

### 4.3 发送方向：多路复用与 256 位打包

#### 4.3.1 概念说明

接收方向是「一分四」，发送方向反过来是「多合一」。要回送给主机的数据来源有：TLP（4 路）、CFG、Loopback、Command 回送。如果让它们各自抢着往 FT601 发，既会冲突也浪费——FT601 的有效载荷较宽。作者的办法是：用一个 **多路复用器 `pcileech_mux`**，把若干个 32 位字按优先级合并成 **1 个状态字 + 7 个数据字 = 256 位** 的大包，再一次性交给 com 上行。

#### 4.3.2 核心流程

```
   Loopback FIFO ──┐
   Command  FIFO ──┤
   CFG  rx_data  ──┼──→ pcileech_mux (按 p0..p7 优先级) ──→ 256 位包 ──→ com_din ──→ FT601
   TLP #1..#4     ──┘                  (1 状态 + 7 数据)
```

mux 内部用一组递增索引 `p0_idx..p8_idx` 来「贪心」地把这一拍有数据的端口依次塞进 256 位包的 7 个数据槽里，详见 u2-l4。本讲只关注 **谁接到了哪个端口、优先级如何**。

#### 4.3.3 源码精读

**作者画的发送合并示意图**—— [pcileech_fifo.sv:82-100](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L82-L100)：可见优先级从高到低是 **TLP > CFG > Loopback > Command**（注释里的 1st/2nd/3rd/4th）。

**两路发送缓冲 FIFO**：

- Loopback 发送 FIFO—— [pcileech_fifo.sv:108-119](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L108-L119)：`fifo_34_34`，把接收时存下的 Loopback 数据原样送回 mux。
- Command 发送 FIFO—— [pcileech_fifo.sv:130-141](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L130-L141)：`fifo_34_34`（`i_fifo_cmd_tx`），缓存命令的「读回值 / 计时器事件 / 计数事件」，再交给 mux。

**多路复用器例化与端口优先级**—— [pcileech_fifo.sv:146-201](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L146-L201)：

```systemverilog
pcileech_mux i_pcileech_mux(
    .dout ( dcom.com_din ), .valid ( dcom.com_din_wr_en ), .rd_en ( dcom.com_din_ready ),
    // LOOPBACK:
    .p0_din ( _loop_dout[31:0] ), .p0_tag ( 2'b10 ), ...
    // COMMAND:
    .p1_din ( _cmd_tx_dout[31:0] ), .p1_tag ( 2'b11 ), ...
    // PCIe CFG
    .p2_din ( dcfg.rx_data ), .p2_tag ( 2'b01 ), ...
    // PCIe TLP #1..#4
    .p3_din ( dtlp.rx_data[0] ), .p3_tag ( 2'b00 ), ...
    .p4_din ( dtlp.rx_data[1] ), .p4_tag ( 2'b00 ), ...
    .p5_din ( dtlp.rx_data[2] ), .p5_tag ( 2'b00 ), ...
    .p6_din ( dtlp.rx_data[3] ), .p6_tag ( 2'b00 ), ...
    // P7 空闲占位
    .p7_wr_en ( 1'b0 ), ...
);
```

要点：

- **端口号即优先级**：mux 内部 `p0` 最高、`p7` 最低（见 [pcileech_mux.sv:21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L21) 的注释 `port0: input highest priority`）。这里把 Loopback/Command 接到 p0/p1，看似优先级很高，但它们的数据量极小（只是控制回送）；真正的大流量 TLP 接到 p3-p6。**注意**：mux 的「优先级」是「同一拍谁先塞进 256 位包的顺序」，而注释图 L88-99 说的「TLP 1st」是指 **整包不被切断的策略语义**，两者不矛盾——u2-l4 会专门讲清。
- **tag**：2 位标签，与 4.2 的 type 取值 **完全对应**（TLP=00、CFG=01、Loopback=10、Command=11），主机端凭 tag 就能反向识别 256 位包里每个 32 位字来自哪条通路。这是一个非常漂亮的「收发对称」设计。
- **ctx**：2 位上下文，TLP 通路塞入 `{rx_first, rx_last}`，Loopback/Command 塞入各自的 `[33:32]`，用来在 256 位包里标记首拍/末拍。
- 输出 `dout/valid/rd_en` 直接连到 `dcom.com_din / com_din_wr_en / com_din_ready`，把合好的 256 位包交还 com 上行。

#### 4.3.4 代码实践

> **实践目标**：把 mux 的 8 个输入端口与「数据来源 + tag」整理成对照表，验证「收发 tag 对称」。

**操作步骤**：

1. 阅读 [pcileech_fifo.sv:146-201](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L146-L201) 的例化。
2. 填表（答案见 4.3.5）：

   | 端口 | 数据来源 | tag | 业务 |
   |---|---|---|---|
   | p0 | `_loop_dout` | 10 | Loopback |
   | p1 | ? | ? | ? |
   | p2 | ? | ? | ? |
   | p3-p6 | ? | ? | ? |

3. 对照 4.2 的接收 type 表，确认「接收 type 值」与「发送 tag 值」一一相等。

**需要观察的现象**：tag 取值在收发两端一致；TLP 占了 4 个端口（p3-p6）对应 4 路 `dtlp.rx_data[*]`。

**预期结果**：p1=Command(tag 11)、p2=CFG(tag 01)、p3-p6=TLP #1-#4(tag 00)，p7 恒为 `wr_en=0` 的空闲占位。tag 与接收 type 完全对称。

> 本实践为源码阅读型，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TLP 要独占 p3-p6 四个端口，而 CFG 只占一个 p2？

<details><summary>参考答案</summary>

接口 `IfPCIeFifoTlp` 用数组 `rx_data[4] / rx_valid[4]` 表达 **4 路并行的 TLP 接收**（见 u2-l1 与 [pcileech_header.svh:220-239](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L220-L239)），每一路都要能独立送进 mux，所以占 4 个端口。CFG 是单路 32 位 `rx_data`，只需 1 个端口。
</details>

**练习 2**：256 位包里「1 个状态字 + 7 个数据字」中的「状态字」用来装什么？

<details><summary>参考答案</summary>

装每个数据槽的 **tag + ctx**（即「这个字来自哪条通路 + 首末拍标记」）。这样主机收到 256 位包后，无需额外带外信息，就能把 7 个 32 位字正确归位到 TLP/CFG/Loopback/Command 各路。详见 [pcileech_mux.sv:114](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_mux.sv#L114) 的 `dout_data` 拼接。
</details>

### 4.4 命令/控制寄存器文件与命令解析

#### 4.4.1 概念说明

Command 通路（type=11）承载的不是普通数据，而是 **主机对设备的控制命令**：读某个寄存器、写某个寄存器、触发 DRP、改配置空间影子……fifo 必须把这些命令解析出来，作用于它内部维护的 **寄存器文件**，或转发给配置空间影子。

寄存器文件分两份：

- `ro`（320 位，read-only）：设备的 **状态回读**，如 MAGIC、版本、DEVICE_ID、运行时长（UPTIME）、PCIe 在位/复位状态、DRP 读回值等。主机只能读。
- `rw`（240 位，read-write）：设备的 **控制位**，如 PCIE CORE RESET、CFGTLP 处理开关、BAR PIO 开关、DRP 触发位、全局复位位等。主机可读可写。

#### 4.4.2 核心流程

```
   dcom.com_dout (type=11) ──→ i_fifo_cmd_rx (64-bit FIFO)
                                      │ (每 4 拍读一次)
                                      ▼
                                 cmd_rx_dout (64 bit)
                                      │
                      ┌───────────────┼────────────────┐
                      │ 解析：         │                │
                      │  address_byte (位[31:16])        │
                      │  value (位[55:48],[63:56])       │
                      │  mask  (位[39:32],[47:40])       │
                      │  f_rw (位31) f_shadow(位30)      │
                      │  read(位12) write(位13)          │
                      └───────────────┬────────────────┘
                                      │
            ┌─────────────────────────┼─────────────────────────┐
            │                         │                          │
       读 ro / rw               写 rw (按 mask 逐位)        转发影子配置空间
       → 回送 cmd_tx FIFO         → 改控制位                  → dshadow2fifo
```

#### 4.4.3 源码精读

**ro（只读）寄存器布局**—— [pcileech_fifo.sv:226-251](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L226-L251)：逐字段标注了字节偏移（`+000` MAGIC、`+008` VERSION、`+00A` DEVICE_ID、`+010` UPTIME、`+020` DRP 读回、`+022` PCIe PRSNT#/PERST# …）。其中 `+010 UPTIME` 直接绑到 `tickcount64`，主机读它就知道设备已上电多久。

**rw（读写）寄存器初值**—— [pcileech_fifo.sv:259-302](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L259-L302)：用 task `pcileech_fifo_ctl_initialvalues` 集中初始化。重点位：

```systemverilog
rw[200] <= 1'b1;   // +019: PCIE CORE RESET     （上电默认把 PCIe 核按在复位态）
rw[201] <= 1'b0;   //       PCIE SUBSYSTEM RESET
rw[202] <= 1'b1;   //       CFGTLP PROCESSING ENABLE
rw[203] <= 1'b1;   //       CFGTLP ZERO DATA
rw[204] <= 1'b1;   //       CFGTLP FILTER TLP FROM USER
rw[205] <= 1'b1;   //       PCIE BAR PIO ON-BOARD PROCESSING ENABLE
```

这些就是「主机改一位、设备行为就变」的控制开关。还记得 u2-l2 末尾那条上电注入命令 `64'h00000003_80182377` 吗？它的最终目的就是改写这里的 `rw[200]`——下面就用它做实战。

**命令字段解析**—— [pcileech_fifo.sv:345-360](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L345-L360)：

```systemverilog
wire [15:0] in_cmd_address_byte = cmd_rx_dout[31:16];
wire [17:0] in_cmd_address_bit  = {in_cmd_address_byte[14:0], 3'b000}; // 字节地址×8=位地址
wire [15:0] in_cmd_value        = {cmd_rx_dout[48+:8], cmd_rx_dout[56+:8]};
wire [15:0] in_cmd_mask         = {cmd_rx_dout[32+:8], cmd_rx_dout[40+:8]};
wire        f_rw                = in_cmd_address_byte[15];   // 1=操作 rw，0=操作 ro
wire        f_shadowcfgspace    = in_cmd_address_byte[14];   // 1=转发影子配置空间
wire        in_cmd_read  = cmd_rx_valid & cmd_rx_dout[12] & ~f_shadowcfgspace;
wire        in_cmd_write = cmd_rx_valid & cmd_rx_dout[13] & ~f_shadowcfgspace & f_rw;
```

要点：

- **字节地址 → 位地址**：寄存器文件以 **位** 为粒度寻址，所以把字节地址左移 3 位（`×8`）。
- **mask 逐位写**：写操作时不是整 16 位覆盖，而是「 mask 的第 i 位为 1，才把 value 的第 i 位写进 `rw[base+i]`」——见下方写循环。
- **读 / 写 / 影子三选一**：`bit[12]` 是读、`bit[13]` 是写；若 `f_shadowcfgspace=1` 则不碰本地寄存器，而是把命令转发给 `dshadow2fifo`（读写配置空间影子 BRAM）。

**主控 always 块（读回 / 计时 / 计数 / 写入 / DRP）**—— [pcileech_fifo.sv:364-431](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L364-L431)，其中 **按 mask 逐位写 rw** 的循环在 [pcileech_fifo.sv:405-410](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L405-L410)：

```systemverilog
else if ( in_cmd_write )
    for ( i_write = 0; i_write < 16; i_write = i_write + 1 )
        if ( in_cmd_mask[i_write] )
            rw[in_cmd_address_bit+i_write] <= in_cmd_value[i_write];
```

**命令读回**走 [pcileech_fifo.sv:377-382](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L377-L382)：读 `ro`/`rw` 的 16 位值，封进 `_cmd_tx_din`，经 `i_fifo_cmd_tx` → mux p1 回送主机。

#### 4.4.4 代码实践

> **实践目标**：把 u2-l2 那条上电命令 `64'h00000003_80182377` 逐字段拆开，亲手走一遍「命令 → 解析 → 写 rw → 释放 PCIe 复位」的全链路。

**操作步骤**：

1. 把命令按位段拆开（对照 [pcileech_fifo.sv:346-354](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L346-L354)）：

   ```
   64'h00000003_80182377
        高32 │   低32
   [63:32]=0x00000003   [31:16]=0x8018   [15:0]=0x2377
   ```
   - `[7:0] = 0x77` → MAGIC ✓
   - `[9:8] = 11`  → type=Command ✓
   - `in_cmd_address_byte = 0x8018`：`f_rw=bit15=1`（写 rw），`f_shadow=bit14=0`（本地寄存器）
   - `in_cmd_address_bit = 0x0018 << 3 = 192`（字节地址 0x18 → 位地址 192）
   - `in_cmd_mask = {0x03, 0x00} = 0x0300`（位 [9:8] 为 1）
   - `in_cmd_value = 0x0000`
   - `bit[13]=write`：确认 0x8018 的 bit13——`0x8018` 的 bit13=0？需核对（见下）。

2. 写循环执行：mask 的 bit8、bit9 为 1，于是写 `rw[192+8]=rw[200] ← 0`、`rw[192+9]=rw[201] ← 0`。
3. 对照 [pcileech_fifo.sv:287-288](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287-L288)：`rw[200]` 是 **PCIE CORE RESET**，初值 1；被写成 0 即「释放 PCIe 核复位」。`rw[201]` 初值本就是 0，写 0 是空操作。

**需要观察的现象**：一条 64 位命令，最终落在 `rw[200]` 这一个比特上，由它去控制 PCIe 核的复位线（见 4.5）。

**预期结果**：该命令 = 「向 rw 寄存器位 200（PCIE CORE RESET）写 0」，把 PCIe 核从上电默认的复位态释放出来——这正是 u2-l2 所说的「先有鸡先有蛋」问题的解法。

> 关于 `bit[13]`（write 使能）：严格说本命令的 write 使能位由命令打包格式决定，完整协议在 u2-l5 详讲；本讲只需确认它能触发 `in_cmd_write=1`，**待本地验证** 位级细节。

#### 4.4.5 小练习与答案

**练习 1**：为什么写寄存器要用「mask 逐位写」，而不是直接整体覆盖 16 位？

<details><summary>参考答案</summary>

因为同一个 16 位字里可能混装了多个独立开关（如 `rw[200..207]` 是 8 个不同的 PCIe 控制位）。mask 让主机 **只改动自己关心的那几位**，其余位保持原值不被破坏。这是「读-改-写」冲突避免的经典做法。
</details>

**练习 2**：`f_shadowcfgspace=1` 时，命令不再读写本地 `ro/rw`，那它去哪了？

<details><summary>参考答案</summary>

去配置空间影子 BRAM。fifo 把地址、数据、字节使能、读/写使能通过 `dshadow2fifo` 接口（`rx_rden/rx_wren/rx_addr/rx_data/rx_be`）转发给 `pcileech_tlps128_cfgspace_shadow` 模块，由后者读写 BRAM 并把读回值经 `tx_valid/tx_data` 送回——见 [pcileech_fifo.sv:355-360](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L355-L360) 与 [pcileech_fifo.sv:369-375](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L369-L375)。
</details>

### 4.5 系统控制中枢：从寄存器到 PCIe 复位、DRP 与全局复位

#### 4.5.1 概念说明

`pcileech_fifo` 最容易被忽略、却最关键的职责是：**把 rw 寄存器里的「控制位」翻译成对 PCIe 核的实际硬件动作**。这一节把这条「控制链」打通：rw 的某些位 → `_pcie_core_config` → `dpcie` / `dshadow2fifo` 的输出信号 → PCIe 核与影子模块。此外 fifo 还能通过 STARTUPE2 原语触发 **整片 FPGA 的全局复位**（GSR），这是「软件重启硬件」的终极手段。

#### 4.5.2 核心流程

```
   rw[207:128]  ──(每拍同步)──→  _pcie_core_config[79:0]
                                       │
                ┌──────────┬───────────┼──────────┬────────────┐
                ▼          ▼           ▼          ▼            ▼
         pcie_rst_core  pcie_rst_subsys cfgtlp_en bar_en   alltlp_filter ...
          (dpcie)        (dpcie)       (dshadow)  (dshadow)  (dshadow)

   rw[208+:16] (DRP di) ──→ dpcie.drp_di
   rw[224+:9]  (DRP addr)──→ dpcie.drp_addr
   rw[DRP_WR_EN/RD_EN] ──→ dpcie.drp_en / drp_we ──→ PCIe 核 DRP
   dpcie.drp_do / drp_rdy ──→ 回写 ro / 清请求位

   rw[31] (GLOBAL SYSTEM RESET) ──→ STARTUPE2.GSR ──→ 整片 FPGA 复位
```

#### 4.5.3 源码精读

**rw → `_pcie_core_config` 的同步与分发**—— [pcileech_fifo.sv:304-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L304-L322)：

```systemverilog
always @ ( posedge clk )
     _pcie_core_config <= rw[207:128];          // 把 rw 高段锁存进 80 位影子寄存器
assign dpcie.pcie_rst_core   = _pcie_core_config[72];   // = rw[200]
assign dpcie.pcie_rst_subsys = _pcie_core_config[73];   // = rw[201]
assign dshadow2fifo.cfgtlp_en     = _pcie_core_config[74]; // = rw[202]
assign dshadow2fifo.cfgtlp_zero   = _pcie_core_config[75]; // = rw[203]
assign dshadow2fifo.cfgtlp_filter = _pcie_core_config[76]; // = rw[204]
assign dshadow2fifo.bar_en        = _pcie_core_config[77]; // = rw[205]
assign dshadow2fifo.cfgtlp_wren   = _pcie_core_config[78]; // = rw[206]
assign dshadow2fifo.alltlp_filter = _pcie_core_config[79]; // = rw[207]
```

这就是 4.4 里 `rw[200]` 的最终去向：`rw[200] → _pcie_core_config[72] → dpcie.pcie_rst_core`，直接拉/放 PCIe 核的复位线。一条命令改一个比特，就能让 PCIe 核上线或下线。

**DRP（动态重配置端口）握手**—— [pcileech_fifo.sv:319-322](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L319-L322) 给出 DRP 的地址/数据/使能连线，而 [pcileech_fifo.sv:416-429](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L416-L429) 是完成握手：当 `dpcie.drp_rdy` 拉高，把 `drp_do` 锁进 `rwi_drp_data`（供 ro 回读）、并清掉请求位；否则把 `rw[DRP_RD_EN/WR_EN]` 的请求「或」到内部 `rwi_drp_*` 保持住，直到完成。配合 `RWPOS_WAIT_COMPLETE`（[pcileech_fifo.sv:207, 331](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L328-L331)）可在 DRP 未完成时暂停命令 FIFO 的读出，避免覆盖。DRP 深入讲解见 u5-l4。

**全局复位 STARTUPE2**—— [pcileech_fifo.sv:436-456](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L436-L456)：

```systemverilog
STARTUPE2 #(...) i_STARTUPE2 (
    .CLK ( clk ),
    .GSR ( rw[RWPOS_GLOBAL_SYSTEM_RESET] | rst_cfg_reload ), // <- GLOBAL SYSTEM RESET
    ...
);
```

`STARTUPE2` 是 7 系列 FPGA 的专用原语，其 `GSR`（Global Set/Reset）信号一旦拉高，会 **同时复位片内所有触发器**——等价于「软件按一下板卡的复位键」。它由 `rw[31]`（`RWPOS_GLOBAL_SYSTEM_RESET`）或顶层的 `rst_cfg_reload`（长按按键触发的 PCIe 配置重载）触发。这是 fifo 作为「控制中枢」最高权限的体现。

#### 4.5.4 代码实践

> **实践目标**：追踪「主机写 rw[200] → PCIe 核复位线变化」这条完整的控制链，确认 4.4 的命令最终落到了硬件。

**操作步骤**：

1. 从 [pcileech_fifo.sv:287](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L287) 出发：`rw[200]` 初值=1。
2. 跟到 [pcileech_fifo.sv:309-311](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L309-L312)：`rw[207:128]` 每拍锁进 `_pcie_core_config`，`rw[200]` 对应 `_pcie_core_config[72]`。
3. 跟到 [pcileech_fifo.sv:311](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L311)：`dpcie.pcie_rst_core = _pcie_core_config[72]`。
4. 翻到接口定义 [pcileech_header.svh:244-265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265)：`pcie_rst_core` 经 `IfPCIeFifoCore` 送到 PCIe 核封装 `pcileech_pcie_a7`，作用于 `pcie_7x_0` IP 的复位输入。

**需要观察的现象**：`rw[200]` 从 1→0 后，`dpcie.pcie_rst_core` 随之变 0，PCIe 核退出复位、开始链路训练（LTSSM）。

**预期结果**：控制链 `命令 → rw[200] → _pcie_core_config[72] → dpcie.pcie_rst_core → PCIe 核` 全程贯通，无任何「中间人」改写。这正是 fifo「控制中枢」角色的最直接证据。

> 实际波形需在 Vivado 仿真中观察 `dpcie.pcie_rst_core` 与 `user_lnk_up`，**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`_pcie_core_config` 是一个 80 位的 **寄存器**（`reg`），而不是直接用组合逻辑把 `rw` 接到 `dpcie`。为什么要多此一举？

<details><summary>参考答案</summary>

为了 **打断组合路径**、改善时序。`rw` 的写来自命令 FIFO（跨时钟、多源），直接组合外连到 PCIe 核会让这条路径很长、很难收敛。先用一级 `always @(posedge clk)` 寄存器把 `rw[207:128]` 同步一拍，再扇出到各 `dpcie/dshadow2fifo` 信号，把长组合链切成两段，时序更稳。
</details>

**练习 2**：主机想「软重启整片 FPGA」，应该写哪个寄存器位？

<details><summary>参考答案</summary>

写 `rw[31]`（`RWPOS_GLOBAL_SYSTEM_RESET`）=1。它会拉高 `STARTUPE2.GSR`，触发全局复位，等价于按板卡复位键。注意这是一次性动作，复位后 `rw` 会回到 `pcileech_fifo_ctl_initialvalues` 设定的初值（见 [pcileech_fifo.sv:364-366](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L364-L366)）。
</details>

## 5. 综合实践

让我们把本讲四条主线串起来，做一次「**一个命令的生命周期**」端到端追踪。对象仍是 u2-l2 那条上电注入命令 `64'h00000003_80182377`。

**任务**：写一份追踪文档，按下面 6 个阶段，每阶段给出「涉及的源码行号 + 信号取值变化」。

1. **注入**：com 模块在上电第 16~20 拍把该命令作为 `dcom.com_dout` 交给 fifo（见 u2-l2）。
2. **路由**：fifo 在 [pcileech_fifo.sv:65-74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L65-L74) 识别 `[7:0]=0x77`、`[9:8]=11` → `_cmd_rx_wren=1`，命令进入 `i_fifo_cmd_rx`（[L333-343](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L333-L343)）。
3. **解析**：命令读出为 `cmd_rx_dout`，在 [L346-354](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L346-L354) 拆出 `address_bit=192`、`mask=0x0300`、`value=0x0000`、`f_rw=1`、`in_cmd_write=1`。
4. **写寄存器**：写循环 [L405-410](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L405-L410) 把 `rw[200]`、`rw[201]` 清 0。
5. **控硬件**：`rw[200]` 经 [L309-311](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L309-L312) 传到 `dpcie.pcie_rst_core=0`，PCIe 核退出复位。
6. **无回送**：因为这是写命令（非读），不会产生回送包；若你把命令改成「读 `ro` 的 UPTIME（字节 0x10）」，则会在 [L377-382](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_fifo.sv#L377-L382) 生成回送字 → `i_fifo_cmd_tx` → mux p1 → 256 位包 → 主机。

**交付物**：一张时序表，列出 `tickcount64`、`dcom.com_dout_valid`、`_cmd_rx_wren`、`cmd_rx_valid`、`in_cmd_write`、`rw[200]`、`_pcie_core_config[72]`、`dpcie.pcie_rst_core` 在各阶段的取值。

**进阶**：把第 6 阶段的「读 UPTIME」命令也按本讲规则 **手工组装** 出一个 64 位命令字（提示：type=11、address_byte 指向字节 0x10、`bit[12]`=read、`f_rw=0` 因为读 ro），并预测回送的 256 位包里 tag 应为多少。

> 时序表的具体取值需结合仿真或上板波形核对，**待本地验证**。

## 6. 本讲小结

- `pcileech_fifo` 是系统的 **中央调度室**：5 条 interface 汇聚于此，所有「主机 ↔ PCIe」的数据与控制都必经它。
- **接收方向**靠一组 `CHECK_MAGIC` / `CHECK_TYPE_*` 文本宏做纯组合分流：`[7:0]=0x77` 验明正身，`[9:8]` 取 `00/01/10/11` 分别送往 TLP / CFG / Loopback / Command，四路天然互斥。
- 四路宽度不同：TLP、Loopback 取高 32 位（Loopback 额外带 2 位 ctx），CFG、Command 保留完整 64 位。
- **发送方向**用 `pcileech_mux` 把 Loopback/Command/CFG/TLP×4 共 7 路按端口优先级合并成「1 状态字 + 7 数据字 = 256 位」大包；端口 **tag 与接收 type 完全对称**，主机端可无损还原。
- fifo 维护 **ro（只读状态）/ rw（读写控制）** 两份寄存器文件；命令经「字节地址→位地址」、按 **mask 逐位写**，并可被 `f_shadowcfgspace` 转发到配置空间影子。
- rw 的控制位经 `_pcie_core_config` 翻译成 `dpcie.pcie_rst_core / cfgtlp_en / bar_en / DRP` 等硬件信号；`rw[31]` 更能经 **STARTUPE2.GSR** 触发整片 FPGA 全局复位——fifo 因此是名副其实的「控制中枢」。

## 7. 下一步学习建议

- **u2-l4 输出多路复用器与 256 位打包**：本讲只用了 mux 的「端口与 tag」，下一讲会钻进 `pcileech_mux.sv` 内部，讲清 `p0_idx..p8_idx` 的贪心索引、空闲端口 p8、输出缓冲 `dout_buf` 的防丢包逻辑。
- **u2-l5 命令/控制寄存器文件与读写协议**：本讲给了寄存器文件全景与一条命令的实战，下一讲会补全 ro/rw 的 **完整字段表**、读回时序、DRP 完成握手（`RWPOS_WAIT_COMPLETE`）等细节。
- **u5-l1 跨时钟域设计与双时钟 FIFO** / **u5-l4 DRP 动态重配置端口**：本讲出现的 `fifo_64_64_clk1_fifocmd`、`dpcie.drp_*` 握手，在这两讲里有完整的跨时钟与 DRP 协议讲解。
- 想立刻看到「控制位 → PCIe 行为」的端到端效果，可先跳读 [pcileech_pcie_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv) 里 `pcie_rst_core` 如何作用于 `pcie_7x_0` IP，这会和本讲 4.5 无缝衔接。
