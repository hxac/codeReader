# AXI/总线接口与 logger

> 本讲是「通信协议 IP」单元（u5）的第 4 篇，依赖 u4-l2（单时钟 FIFO）。
> 在 u5-l1（UART）和 u5-l2（SPI）里，我们一直在为模块**手写一长串端口**：`txd`、`rxd`、`miso`、`sclk`、`ncs`…… 当总线宽到 AXI 这种 40 多根信号时，这种写法既啰嗦又容易把方向接反。本讲就来解决「如何把一捆信号打包、并约束主从方向」，并用一个真实的 `axi4l_logger` 看看这种打包接口在实际调试里怎么用。

## 1. 本讲目标

学完本讲你应该能够：

- 说清 SystemVerilog `interface` 解决了什么问题，以及 `modport` 如何为同一组信号定义「主/从两种视角」。
- 读懂 `axis_if`（AXI4-Stream）的 `tvalid/tready` 握手规则，并能在波形里判断「哪一拍发生了传输」。
- 说出 AXI4（全 AXI / AXI4-Lite）的五通道结构，以及它和 Wishbone 这类「单周期经典总线」的本质差别。
- 看懂 `axi4l_logger` 如何**只读地旁路监听**一条 AXI4-Lite 总线，把每个事务的「地址 + 数据 + 读/写」压进双时钟 FIFO。
- 自己用 `axis_if` + `modport` 把一个数据源和一个（包了 FIFO 的）从机连起来，演示带反压的握手。

## 2. 前置知识

本讲默认你已经掌握：

- **模块、例化、参数化端口、`always_ff` 时序逻辑**（见 u1-l2）。
- **仿真与 timescale、iverilog `-g2012` / ModelSim 编译流程**（见 u1-l3）。
- **FIFO 的满/空判断、`w_req`/`r_req` 握手、overflow/underflow 保护**（见 u4-l2）——本讲的 `axi4l_logger` 内部就用到了双时钟 FIFO 与 FWFT（首字直通）概念。

两个本讲要新引入的术语，先建立直觉：

- **总线（bus）/ 接口（interface）**：把「地址、数据、控制」按某种约定组合起来，让一个主设备（master）能读写一个或多个从设备（slave）的寄存器或存储。AXI、Wishbone、Avalon 都是不同的「总线协议」。
- **通道（channel）与握手（handshake）**：AXI 用「valid/ready」成对信号做握手——发送方拉高 `xvalid` 表示「我手上有有效数据」，接收方拉高 `xready` 表示「我能收」；**同一时钟沿上两者都为 1，才发生一次传输**。这是本讲反复出现的核心动作。

## 3. 本讲源码地图

本讲涉及的关键文件都集中在仓库 `interfaces/` 子目录和根目录，它们是一组**「接口模板」**——本身不是完整设计，而是供你在自己的工程里 `include` / 例化的「线束」：

| 文件 | 作用 |
|------|------|
| [interfaces/axis_if.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv) | AXI4-Stream 接口：单向数据流，`tvalid/tready` 握手，含 `master_mp`/`slave_mp` 两个 modport。 |
| [interfaces/axi4_if.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv) | 全 AXI4（memory-mapped）接口：五通道完整信号组，同样带主/从 modport。 |
| [interfaces/wb_if.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv) | Wishbone 接口：经典单周期总线，信号少、最简单，适合与 AXI 做对比。 |
| [axi4l_logger.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv) | AXI4-Lite 事务嗅探器：并联在总线上只读采样，把事务地址/数据/方向写入双时钟 FIFO。 |

> 说明：README 的 DIRECTORY 表里并没有列出 `interfaces/` 目录，README 的 FILE 表（第 49 行）也只单独提到 `axi4l_logger.sv`。这组接口文件是「半隐藏」的模板，阅读时要以**实际代码**为准（这和 u1-l1 提到的「文档会滞后于代码」是一致的）。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：**interface/modport**（语言机制）→ **AXI4-Stream**（`axis_if`）→ **AXI4 / AXI4-Lite**（`axi4_if`）→ **Wishbone**（`wb_if`）→ **事务嗅探**（`axi4l_logger`）。前 4 个是「如何定义总线」，第 5 个是「如何把总线用起来做调试」。

### 4.1 interface 与 modport：把一捆信号打包成有方向的端口组

#### 4.1.1 概念说明

随着总线变宽，传统写法的两个痛点越来越明显：

1. **端口爆炸**：一个 AXI 主机要把 40 多根信号逐一写进端口列表，每个使用它的工程都要抄一遍，又长又容易抄错。
2. **方向含糊**：哪怕我把这 40 根信号捆成一个「总线」传进模块，模块又怎么知道**哪几根该由它驱动、哪几根该由它采样**？

SystemVerilog 的 `interface` + `modport` 就是为这两点设计的：

- `interface` 把一整组相关信号（用 `logic` 声明）和它们共用的 `parameter` 封装在一起，作为一个**整体类型**被传来传去——解决痛点 1。
- `modport`（module port 的缩写）给这组信号定义「若干种视角」，每种视角显式列出**在这个角色下每根信号是 input 还是 output**——解决痛点 2。

一句话：`interface` 负责「打包」，`modport` 负责「定方向」。

#### 4.1.2 核心流程

使用接口的典型四步：

1. 在 `interface ... endinterface` 中用 `logic` 声明全部信号，并用 `#(parameter ...)` 参数化位宽。
2. 定义一个或多个 `modport`，按角色列出每根信号的 `input`/`output` 方向。
3. 在顶层（或 testbench）例化接口实例：`axis_if #(.DATA_W(8)) bus();`（注意结尾的空括号——这是「实例」不是「模块」）。
4. 把这个实例接到模块端口上；模块的端口声明里写明用哪个 modport：`module src (axis_if.master_mp bus)`。

一个关键直觉：**同一个 `bus` 实例，对主机是 `master_mp` 视角，对从机是 `slave_mp` 视角——同一根物理线，两个方向相反的视图**。这正是主从握手能对上的原因。

#### 4.1.3 源码精读

以最短的 `axis_if` 为例，它把「打包」和「定方向」两件事一次做齐。

先看打包——参数化位宽，再集中声明信号：

[interfaces/axis_if.sv:13-17](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L13-L17) 定义接口参数 `DATA_W/ID_W/USER_W`；

[interfaces/axis_if.sv:20](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L20) 用 `localparam KEEP_W = DATA_W/8` 从数据宽度派生字节有效位宽度——这是「参数化派生」的典型手法，改一处 `DATA_W`，`tkeep` 自动跟着变；

[interfaces/axis_if.sv:22-29](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L22-L29) 集中声明全部信号（`tdata/tdest/tid/tkeep/tlast/tready/tuser/tvalid`）。

再看定方向——同一个 `tready`，在主从两边方向相反：

[interfaces/axis_if.sv:32-43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L32-L43) `master_mp`：`input tready`（主机**采样**从机是否就绪），其余 `t*` 全是 `output`（主机**驱动**数据和有效标志）；

[interfaces/axis_if.sv:46-57](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L46-L57) `slave_mp`：正好反过来，`output tready`（从机**驱动**就绪标志），其余 `t*` 全是 `input`。

> 小贴士：如果一个模块的端口写成 `axis_if bus`（**不带 modport**），编译器不会约束方向，模块里所有信号都可读可写——这在 testbench 当「线束」时很方便，但在可综合设计里不推荐，因为失去了「防接反」的保护。

#### 4.1.4 代码实践

**目标**：亲手把「同一根 `tready` 在两个 modport 里方向相反」这件事在仿真里验证一次。

**操作步骤**（源码阅读型实践）：

1. 打开 `interfaces/axis_if.sv`，对照 [L32-43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L32-L43) 与 [L46-57](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L46-L57)，在一张纸上列两列：左列写 `master_mp` 下每根信号的方向，右列写 `slave_mp` 下的方向。
2. 检查 `tready`：它是否一列为 `input`、另一列为 `output`？其余 `tdata/tvalid/tlast/...` 是否都成对相反？

**需要观察的现象**：每根信号在两个 modport 里的方向都恰好相反；没有任何一根在两边同为 `input` 或同为 `output`。

**预期结果**：你会得到一张完美的「镜像表」，这就是 modport 的全部魔法——同一组线，两个互逆视角。后续 4.2 的握手之所以成立，正是建立在 `tready`（从机出 / 主机入）与 `tvalid`（主机出 / 从机入）这一对反向信号之上。

#### 4.1.5 小练习与答案

- **Q1**：如果把 `axis_if` 实例接到一个端口声明为 `axis_if bus`（无 modport）的模块上，会发生什么？
  - **答**：能连上、能仿真，但模块内所有信号都没有方向约束，相当于一根「裸线束」；工具不会帮你拦截「从机误驱动了 `tvalid`」这类接反错误。可综合模块应当始终带 modport。
- **Q2**：`axis_if` 里 `USER_W` 默认为 `0`，此时 `tuser` 的位宽是多少？为什么这样做是安全的？
  - **答**：`logic [USER_W-1:0]` 即 `logic [-1:0]`，是 SystemVerilog 合法的**零位空向量**，等价于「这根线不存在」。这样默认例化时不必为用不到的边带信号留资源，需要时再设 `USER_W>0`。

### 4.2 AXI4-Stream：axis_if 的单向流握手

#### 4.2.1 概念说明

AXI4-Stream（简称 AXIS）是 AXI 家族里最简单的成员：它**没有地址**，只负责「把一串数据字从 A 单向搬到 B」。典型场景是视频像素流、ADC 采样流、数据包流水线。

它的全部交互由一对握手信号统治：

- 主机驱动 `tvalid`（「我这拍有有效数据」）+ `tdata`（数据本身）。
- 从机驱动 `tready`（「我这一拍能收」）。
- **当某个时钟上升沿上 `tvalid == 1 && tready == 1`，这一拍就完成了一次传输（俗称「握手成功 / fire」）**，数据从主机过到从机。

其余信号都是「装饰」：`tkeep` 标记哪些字节有效、`tlast` 标记一包的最后一个字、`tid/tdest` 用于路由、`tuser` 传边带信息。

> 承接 u4-l3：AXIS 的 `tvalid/tready` 握手本质上就是一个 **FWFT（首字直通）风格的流接口**——`tvalid` 等价于「队头有数据」，`tready` 等价于「读使能」，握手当拍数据就过线。这也是为什么把 FIFO 包成 AXIS 从机如此自然（见本讲综合实践）。

#### 4.2.2 核心流程

每一拍的握手只有三种合法组合：

| `tvalid` | `tready` | 这拍发生了什么 |
|:---:|:---:|---|
| 0 | × | 主机没数据，无事发生 |
| 1 | 0 | 主机想发、从机不收 → **反压（stall）**，主机必须原地保持 `tdata` 不变、继续拉高 `tvalid` |
| 1 | 1 | **传输成功**，下一拍主机可发下一个字 |

两条铁律（AXI 协议规定）：

1. **`tvalid` 一旦拉高，在握手成功前不许撤下**，且 `tdata` 必须保持不变——即「先有效再等就绪」。
2. **`tready` 允许依赖 `tvalid`**（从机可以等看到有效数据后再决定收不收），反过来 `tvalid` 不允许依赖 `tready`。这条规则是为了避免「组合环」。

伪代码描述一个最小 AXIS 主机：

```
每拍:
  tvalid <= 1                 // 一直想发
  tdata  <= 待发数据
  if (tvalid && tready)       // 握手成功
      切换到下一个待发数据
```

#### 4.2.3 源码精读

`axis_if` 把 AXIS 的全部信号声明在一起，主从两个 modport 各给一份互逆视角（4.1.3 已逐行看过）。这里聚焦「握手所需的信号对」：

[interfaces/axis_if.sv:22-29](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L22-L29) 中，握手的核心就是 `tvalid`（第 29 行）和 `tready`（第 27 行）这一对；`tdata`（第 22 行）是载荷，`tkeep`（第 25 行，宽度 `KEEP_W=DATA_W/8`，见 [L20](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axis_if.sv#L20)）标记有效字节，`tlast`（第 26 行）标记一包末尾。

注意：`axis_if` **只声明信号、不规定协议**。「`tvalid && tready` 才算传输」是 AXI 协议文本规定的，接口文件里并没有写这句判断——它由使用接口的模块自己负责实现（见 4.2.4 的示例代码）。

#### 4.2.4 代码实践

**目标**：写一个最小 AXIS 主机（计数器数据源）+ 一个最小 AXIS 从机（接收端），用 `axis_if` 连起来，用 `modport` 约束方向，在波形里看到 `tvalid/tready` 握手并观察反压。这就是本讲规格里要求的核心实践（4.5 综合实践会再把它扩展成带 FIFO 缓冲的版本）。

**操作步骤**：

1. 新建三个文件（下列均为**示例代码**，不在仓库里，请自行创建到自己的工作目录）。

示例代码 —— AXIS 主机（每握手一次就推进一个计数器）：

```systemverilog
// axis_src.sv —— 示例代码
module axis_src (
  input  logic clk,
  input  logic rst_n,
  axis_if.master_mp m_axis      // 用 master_mp：tready 入，其余出
);
  logic [7:0] cnt;
  always_ff @(posedge clk or negedge rst_n)
    if (~rst_n)              cnt <= 8'd0;
    else if (m_axis.tvalid && m_axis.tready)  // 握手成功才推进
                   cnt <= cnt + 8'd1;

  assign m_axis.tvalid = 1'b1;   // 永远想发（满足“先有效再等就绪”）
  assign m_axis.tdata  = cnt;
  assign m_axis.tlast  = 1'b0;
  assign m_axis.tkeep  = '1;
endmodule
```

示例代码 —— AXIS 从机（接收端，演示反压）：

```systemverilog
// axis_sink.sv —— 示例代码
module axis_sink (
  input  logic clk,
  input  logic rst_n,
  axis_if.slave_mp s_axis       // 用 slave_mp：tready 出，其余入
);
  logic [3:0] gate;             // 用周期性拉低 tready 来制造反压
  always_ff @(posedge clk or negedge rst_n)
    if (~rst_n) gate <= 4'd0;
    else        gate <= gate + 4'd1;

  assign s_axis.tready = gate[3];  // 每 16 拍里有 8 拍收、8 拍不收

  logic [7:0] captured;
  always_ff @(posedge clk or negedge rst_n)
    if (~rst_n)                              captured <= 8'd0;
    else if (s_axis.tvalid && s_axis.tready) captured <= s_axis.tdata;
endmodule
```

示例代码 —— testbench，把两者用接口实例连起来：

```systemverilog
// axis_demo_tb.sv —— 示例代码
`timescale 1ns/1ps
module axis_demo_tb;
  logic clk = 1'b0; always #5 clk = ~clk;     // 100MHz
  logic rst_n = 1'b0;
  initial begin #23 rst_n = 1'b1; #500; $finish; end

  axis_if #(.DATA_W(8), .ID_W(0), .USER_W(0)) bus();  // 接口实例

  axis_src  SRC  (.clk(clk), .rst_n(rst_n), .m_axis(bus));
  axis_sink SINK (.clk(clk), .rst_n(rst_n), .s_axis(bus));
endmodule
```

2. 用 u1-l3 学过的 iverilog 流程编译（注意必须 `-g2012` 才支持 interface/modport）：

```bash
iverilog -g2012 -o sim.vvp \
  interfaces/axis_if.sv axis_src.sv axis_sink.sv axis_demo_tb.sv
vvp sim.vvp            # 配合 $dumpfile/$dumpvars 出 .vcd，再用 GTKWave 看
```

**需要观察的现象**：

- `tvalid` 复位后一直为 1；`tready`（=`gate[3]`）呈周期性高低。
- 仅当 `tvalid && tready` 同时为 1 的那些上升沿，`cnt` 才 +1，`captured` 才跟随 `tdata` 更新——这就是「握手当拍传输」。
- 当 `tready` 为 0 的那些拍，`cnt` 原地不动，体现「反压」。

**预期结果**：`captured` 的更新沿严格落在 `tvalid && tready` 的沿上，且每次更新值正好等于上一拍 `cnt`（因为 `cnt` 在握手沿才自增）。

**待本地验证**：iverilog 对 interface/modport 的支持有限，个别版本在零位向量（`ID_W=0`）上可能告警；若遇到问题，把 `ID_W` 改成 `1` 再试。整体握手行为以 ModelSim / Vivado 仿真为准。

#### 4.2.5 小练习与答案

- **Q1**：把 `axis_src` 里 `assign m_axis.tvalid = 1'b1;` 改成「先等 `tready` 再拉高 `tvalid`」（即 `tvalid` 依赖 `tready`），为什么违反 AXI 规则？
  - **答**：AXI 要求 `tvalid` 不能依赖 `tready`，否则主机等从机、从机又可能等主机，容易形成**组合环 / 死锁**。正确做法是主机无条件先拉 `tvalid`，由从机决定何时 `tready`。
- **Q2**：如果从机永远 `tready=0`，主机的 `cnt` 会怎样？
  - **答**：`cnt` 永远停在 0——`tvalid && tready` 永不成立，握手机会为 0，数据一个字也发不出去，这正是「持续反压」的极端情况。

### 4.3 AXI4 与 AXI4-Lite：axi4_if 的五通道模型

#### 4.3.1 概念说明

AXI4-Stream 是「无地址的流」，而 **AXI4（memory-mapped，存储映射）**是「有地址、能读写寄存器/内存」的总线。它的核心思想是**把一次事务拆成多条独立的「通道」**，每条通道各自 valid/ready 握手，彼此可以「错峰」并行，从而支持突发（burst）和未完成（outstanding）事务。

AXI4 共有 **5 条通道**：

| 通道 | 方向 | 携带 | 关键信号前缀 |
|---|---|---|---|
| **AW** 写地址 | 主→从 | 「我要写到这个地址」 | `awaddr/awvalid/awready/awlen/...` |
| **W** 写数据 | 主→从 | 写的数据（可多拍突发） | `wdata/wstrb/wlast/wvalid/wready` |
| **B** 写响应 | 从→主 | 「写完了，结果 OKAY/SLVERR/...」 | `bresp/bvalid/bready` |
| **AR** 读地址 | 主→从 | 「我要读这个地址」 | `araddr/arvalid/arready/arlen/...` |
| **R** 读数据 | 从→主 | 读回的数据（可多拍）+ 响应 | `rdata/rresp/rlast/rvalid/rready` |

写事务走 AW→W→B（先告诉地址、再给数据、最后收响应）；读事务走 AR→R（给地址、收数据）。

**AXI4-Lite** 是 AXI4 的「精简子集」，专用于寄存器配置：**禁止突发**（`awlen/arlen` 恒为 0），并砍掉 `awsize/awburst/awcache/awqos/awregion` 等只在突发里才有意义的字段。AXI4-Lite 只保留每通道的最少信号（`awaddr/awvalid/awready`、`wdata/wstrb/wvalid/wready`、`bresp/bvalid/bready`、`araddr/arvalid/arready`、`rdata/rresp/rvalid/rready`）。

仓库的 `axi4_if.sv` 头注释写的是「**AXI4-M instantiation**」，即**全 AXI4（主机）**的完整信号集；若要做 AXI4-Lite，只需使用其中那个精简子集。

#### 4.3.2 核心流程

一次 AXI4-Lite **写**事务的时序骨架（每条线都遵循 valid/ready 握手）：

```
主: awvalid=1,awaddr=A ──┐  (AW 通道握手)
从: ───────────── awready=1 ┘
主: wvalid=1,wdata=D,wstrb=.. ──┐  (W 通道握手)
从: ────────────────── wready=1 ┘
从: bvalid=1,bresp=OKAY ──┐      (B 通道握手)
主: ───────────── bready=1 ┘
```

关键点：**AW 与 W 通道允许解耦（decouple）**——AXI 规范并不强制两者同拍握手，地址可以先到、数据后到（这正是 4.5 里 `axi4l_logger` 要专门处理 `aw_en` 锁存的原因）。

#### 4.3.3 源码精读

`axi4_if.sv` 把五通道的完整信号一次性声明，然后给主从各一份 modport。

参数与派生位宽：

[interfaces/axi4_if.sv:13-24](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L13-L24) 定义 `ADDR_W/DATA_W/ID_W` 及各通道 USER 宽度（默认 0，同 4.1.5 的空向量技巧）；

[interfaces/axi4_if.sv:27](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L27) `localparam STRB_W = DATA_W/8` 派生写选通（byte-enable）宽度，思路与 `axis_if` 的 `KEEP_W` 一致。

五通道信号分组（注意每组的 valid/ready 成对出现）：

- **AR 读地址通道**：[interfaces/axi4_if.sv:29-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L29-L41)（`araddr/arlen/arsize/arburst/...arvalid/arready`）。
- **AW 写地址通道**：[interfaces/axi4_if.sv:43-55](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L43-L55)。
- **B 写响应通道**：[interfaces/axi4_if.sv:57-61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L57-L61)（`bid/bresp/bvalid/bready`）。
- **R 读数据通道**：[interfaces/axi4_if.sv:63-69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L63-L69)（`rdata/rresp/rlast/rvalid/rready`）。
- **W 写数据通道**：[interfaces/axi4_if.sv:71-77](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L71-L77)（`wdata/wstrb/wlast/wvalid/wready`）。

主从 modport 各列一遍全部信号方向：

[interfaces/axi4_if.sv:80-128](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L80-L128) `master_mp`；[interfaces/axi4_if.sv:131-179](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L131-L179) `slave_mp`——和 `axis_if` 一样，两个 modport 里每根信号方向互逆。

> ⚠️ **阅读源码时务必留意（如实指出，非杜撰）**：当前 HEAD 的 `axi4_if.sv` 第 67 行存在笔误——读响应信号写成 `logic [1:0] rres`，**既漏了末尾的 `p`（应为 `rresp`），也漏了分号**；而两个 modport（[L90](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L90) 和 [L176](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L176)）引用的却是 `rresp`。因此该文件按当前 HEAD **直接编译会报语法/未声明错误**，使用前需把第 67 行改为 `logic [1:0] rresp;`。这再次印证 u1-l1 的提醒：仓库文档/模板会滞后于代码，以实际源码为准。

#### 4.3.4 代码实践

**目标**：在 `axi4_if` 里数清楚「五通道」与「valid/ready 对」，并理解 AXI4-Lite 只是其子集。

**操作步骤**（源码阅读型实践）：

1. 打开 [axi4_if.sv:29-77](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/axi4_if.sv#L29-L77)，把信号按通道分成五组，每组圈出它的 `xxxvalid` 和 `xxxready`。
2. 在每组里挑出「AXI4-Lite 会保留」的最小信号（`araddr/arvalid/arready`、`awaddr/awvalid/awready`、`wdata/wstrb/wvalid/wready`、`bresp/bvalid/bready`、`rdata/rresp/rvalid/rready`），其余（`arlen/arsize/arburst/arcache/arqos/arregion/arlock/...`）标记为「仅全 AXI4 需要」。

**需要观察的现象**：每个通道都恰好有一对 `valid/ready`；五对握手相互独立。

**预期结果**：你会得到一张「5 通道 × (valid, ready)」的清单，其中 AXI4-Lite 子集明显只是全 AXI4 的一个切片。

**待本地验证**：如要真正仿真，记得先按 4.3.3 修复第 67 行的 `rresp` 笔误。

#### 4.3.5 小练习与答案

- **Q1**：AXI4-Lite 为什么禁止突发（`arlen/awlen` 恒为 0）？
  - **答**：Lite 面向「寄存器级配置」，一次只读写一个 32 位寄存器，不需要一次搬一整块数据；去掉突发能大幅简化主从双方的控制逻辑。
- **Q2**：写事务里，AW 通道和 W 通道必须同拍握手吗？
  - **答**：不必。AXI 允许两者解耦：地址可以先握手、数据后到，或反之。这也是真实 AXI 从机（及 4.5 的 logger）要专门处理「地址先到、数据还没到」情形的原因。

### 4.4 Wishbone：wb_if 的经典单周期总线

#### 4.4.1 概念说明

Wishbone 是比 AXI 早得多的「经典 SoC 总线」，思路截然不同：**不分通道，所有信号共用一次「主从对话」**。它用一个 `STB`（strobe，选通）+ `CYC`（cycle，周期）的组合做单次握手，从机用 `ACK`（应答）表示「这次完成了」。

典型信号含义：

- `CYC`：主设备声明「我正在发起一个总线周期」（整个事务期间保持）。
- `STB`：选通，配合 `CYC` 表示「这拍有效」——从机在 `STB` 为高时才看地址/数据。
- `ADR`：地址；`DAT`：数据（写时主→从，读时从→主）；`WE`：写使能；`SEL`：字节选择。
- `ACK`：从机应答，主设备看到 `ACK` 就结束本次传输。
- `ERR`/`RTY`：错误 / 重试。

与 AXI 的本质差别：**Wishbone 把「地址 + 数据 + 握手」绑成一个整体周期，一次 `STB/ACK` 搞定一个字；AXI 把它们拆成独立通道，可以流水线 / 突发 / 多个未完成事务。** Wishbone 简单直观，AXI 性能高但复杂。

#### 4.4.2 核心流程

一次 Wishbone 单字读的时序骨架：

```
主: cyc=1, stb=1, adr=A, we=0  ──────────────
从:                          ack=1, dat=读回值  （从机就绪时拉 ACK）
主: 看到 ack → cyc=0, stb=0，事务结束
```

核心规则：**从机只在 `cyc && stb` 同时为高时才驱动 `ack/dat`**；主设备在 `ack` 为高的那拍采数据并结束周期。

#### 4.4.3 源码精读

`wb_if.sv` 的信号比 AXI 少得多，一眼能看完：

[interfaces/wb_if.sv:13-18](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L13-L18) 定义 `ADDR_W/DATA_W/SEL_W`（还预留了一个被注释掉的 `TAG_W`）；

[interfaces/wb_if.sv:21-31](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L21-L31) 声明信号（`ack/adr/cyc/dat/err/rty/sel/stb/we`）；

[interfaces/wb_if.sv:34-48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L34-L48) `master_mp` 与 [L51-65](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L51-L65) `slave_mp`——注意主机的 `ack/err/rty/dat` 为 `input`（采样从机应答与读回数据），`adr/cyc/sel/stb/we/dat` 为 `output`（驱动命令与写数据），从机方向相反。

> ⚠️ **阅读源码时务必留意（如实指出，非杜撰）**：当前 HEAD 的 [wb_if.sv:24-25](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L24-L25) 把 `dat` **连续声明了两次**（`logic [DATA_W-1:0] dat;` 出现两遍）。真实 Wishbone 里数据线是双向的（`DAT_MOSI` 主→从、`DAT_MISO` 从→主），作者的本意应是把它拆成两根（如 `dat_mosi`/`dat_miso`）来区分方向，但当前实现两端都叫 `dat`，构成**重复声明**，直接编译会报错。使用前需把它们改成两个不同名字。modport 里 `dat` 同时出现在 `input` 和 `output` 列（[L37](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L37)/[L43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L43) 与 [L54](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L54)/[L62](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/interfaces/wb_if.sv#L62)），印证了「同一根数据线既写又读」的双向语义。

#### 4.4.4 代码实践

**目标**：把 Wishbone 和 AXI 做一次「信号规模 + 握手复杂度」的对比，加深对两类总线风格的理解。

**操作步骤**（源码阅读型实践）：

1. 数一下 `wb_if` 的信号总数（约 8 根有效信号 + 1 对隐式握手 `STB/ACK`）。
2. 对比 `axi4_if`：五通道、每通道一对 valid/ready、外加 `len/size/burst/...`。
3. 在一张表里对照：「定义一次单字读，AXI4-Lite 要走几条通道几次握手？Wishbone 要几次 `STB/ACK`？」

**需要观察的现象**：Wishbone 用最少的信号完成一次读（一个 `STB/ACK`），而 AXI4-Lite 即便最简也要 AR、R 两条通道两次握手。

**预期结果**：你会直观体会到「Wishbone 简单、AXI 表达力强但开销大」的取舍。

**待本地验证**：如要仿真 `wb_if`，先按 4.4.3 修复 `dat` 的重复声明。

#### 4.4.5 小练习与答案

- **Q1**：在 Wishbone 里，从机什么时候才应该驱动 `ack` 和读回的 `dat`？
  - **答**：只在 `cyc && stb` 同时为高时；否则总线是空闲的，从机不该应答。
- **Q2**：相对 Wishbone，AXI 用「拆通道」换来了什么？
  - **答**：地址和数据可以错峰、可以一次发地址后续连发多个数据（突发）、可以同时挂多个未完成事务——吞吐显著更高，代价是信号更多、控制更复杂。

### 4.5 事务嗅探：axi4l_logger 如何把 AXI-Lite 事务落盘

#### 4.5.1 概念说明

前面三个接口都是「如何定义总线」。`axi4l_logger` 回答的是另一个工程实际问题：**总线跑起来之后，怎么知道软件/CPU 到底访问了哪些寄存器？**

`axi4l_logger`（README 第 49 行：「sniffs all AXI transactions and stores address and data to fifo」）是一个**旁路嗅探器（sniffer/probe）**：

- 它**不是** AXI 主，也**不是** AXI 从，而是**并联**在 AXI4-Lite 总线上的「监听探头」，把所有信号当**只读输入**接进来（看它的端口 [axi4l_logger.sv:41-79](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L41-L79)，全是 `input`，对总线零驱动、零干扰）。
- 它把每个**完成的事务**浓缩成三元组（地址、数据、读/写方向），压进一个**双时钟 FIFO**；另一侧（通常是另一时钟域的 CPU/调试口）通过 `r_req` 慢慢读出来。

这正是 u3 单元「跨时钟域」+ u4 单元「FIFO」的一次综合应用：嗅探在 AXI 时钟域 `clk_axi` 发生，读出在系统域 `clk` 完成，中间靠双时钟 FIFO 跨域。

> 设计取舍：它用 Verilog 宏（`` `AXI_ADDR_WIDTH ``、`` `AXI_DATA_WIDTH `` 等）而不是 `parameter` 来定宽，INFO 明说「optimized for `AXI_ADDR_WIDTH=32` and `AXI_DATA_WIDTH=32`」。这是较老的 Verilog-2001 风格，复用性不如 `axis_if` 的参数化接口——使用前需在工程里全局 `` `define `` 这些宏。

#### 4.5.2 核心流程

整个 logger 的数据流可以拆成 5 步：

1. **地址窗过滤**：只记录地址落在 `[REG_ADDRESS_FROM, REG_ADDRESS_TO]` 区间内的事务（见模块参数 [axi4l_logger.sv:35-36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L35-L36)）。
2. **检测写/读发生**：用组合逻辑分别判出 `aw_w_req`（写握手发生）和 `ar_w_req`（读地址握手发生）。
3. **延迟一拍快照**：把请求各打一拍（`_d1`），在那一拍把当时的 `addr/data` 组装成 FIFO 写入字 `w_addr_f/w_data_f/w_rnwr_f`。
4. **过滤重复读**（可选）：连续相同的读事务只记一次，避免轮询把 FIFO 灌满。
5. **三口双时钟 FIFO**：用三个 Xilinx `FIFO18E1` 分别存 addr/data/rnwr，共享同一个写使能和读使能，从而**读一次同时弹出一个完整事务**。

读侧端口：`empty`（空标志）、`r_req`（读使能）、`r_rnw`（read-not-write，1=读、0=写）、`r_addr`、`r_data`（见 [axi4l_logger.sv:82-88](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L82-L88) 与 [L341-343](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L341-L343)）。

#### 4.5.3 源码精读

**(a) 写事务检测（处理 AW/W 解耦）**

AXI 写允许 AW（地址）和 W（数据）分开握手。logger 用 `aw_en` 锁存器 + `s_axi_awaddr_buf` 缓冲来兜住「地址先到、数据后到」的情形：

[axi4l_logger.sv:110-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L110-L121) `aw_en`：当 AW 已有效（`awvalid`）但还没握手上（`~awready`）时拉低，直到写响应 B 完成（`bready && bvalid`）再拉高——保证一个写事务只锁一次地址；

[axi4l_logger.sv:123-132](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L123-L132) 在同样条件下把 `awaddr` 存进 `s_axi_awaddr_buf`，避免后续地址变化；

[axi4l_logger.sv:134-138](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L134-L138) `aw_w_req` 组合判据：**AW 与 W 两路同时握手**（`awready&&awvalid&&wready&&wvalid`）且地址在窗内——这就是「写发生」脉冲。

**(b) 读事务检测**

[axi4l_logger.sv:143-152](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L143-L152) 把 `araddr` 缓冲进 `s_axi_araddr_buf`（当 AR 还没握手时）；

[axi4l_logger.sv:154-158](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L154-L158) `ar_w_req` 组合判据：`arready && arvalid && ~rvalid` 且地址在窗内——注意 **`~rvalid`**：它在 R 通道还没回数据的「地址阶段」就触发记录请求。

**(c) 延迟一拍 + 组装 FIFO 写入字**

[axi4l_logger.sv:97-105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L97-L105) 把 `aw_w_req/ar_w_req` 各打一拍得 `_d1`；

[axi4l_logger.sv:169](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L169) `fifo_wren = aw_w_req_d1 || ar_w_req_d1`——任意一种事务发生都写 FIFO；

[axi4l_logger.sv:171-185](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L171-L185) 组装写入字：写则取 `awaddr_buf + wdata + rnw=0`，读则取 `araddr_buf + rdata + rnw=1`。

> ⚠️ **阅读源码时务必留意（如实指出，非杜撰）**：这个「打一拍再采数据」的写法有一个**时序假设**——它假定 AXI-Lite 对端在握手后**下一拍** `wdata`/`rdata` 仍有效（即写数据在 W 握手后还保持、读数据在 AR 握手后约 1 拍返回）。这与「AXI4-Lite 对端恰好 1 拍读延迟」的特定从机（典型 Xilinx 生成 IP）是配对设计的；若你的从机读延迟更长，`rdata` 可能尚未到，logger 会记到无效数据。工程含义：**旁路探头必须与被探测总线的时序配对**，不能假定它对任意 AXI4-Lite 从机都正确——这是真实「嗅探器」设计的常见坑。

**(d) 过滤重复读**

[axi4l_logger.sv:196](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L196) 用 `` `define FILTER_REPETITIVE_READS yes `` 打开过滤；

[axi4l_logger.sv:198-233](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L198-L233) 只缓存**读**事务的上次 (addr,data,rnw)，若新事务三者全等则不写 FIFO（[L226-229](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L226-L229)）；遇到**写**则清缓存（[L214-219](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L214-L219)）——这样软件反复轮询同一个状态寄存器时，只有「值真正变化」的那次才会被记录。

**(e) 三口双时钟 FIFO**

[axi4l_logger.sv:236-269](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L236-L269)、[L271-304](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L271-L304)、[L306-339](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L306-L339) 是三个结构完全相同的 Xilinx `FIFO18E1` 原语（分别存 addr / data / rnwr）。要点：

- **双时钟**：`WRCLK = clk_axi`（嗅探域写），`RDCLK = clk`（系统域读）——这就是 u4-l2/u4-l3 讲过的「双时钟 FIFO 跨域」。
- **FWFT**：`FIRST_WORD_FALL_THROUGH("TRUE")`，读侧 `empty` 撤销时队头数据已直通可用（呼应 u4-l3 的 FWFT 主题）。
- **三口共享同一 `fifo_wren_filt` 写使能、同一 `r_req` 读使能**：所以读一次必然同时弹出 addr+data+rnw 三元组，保持事务完整性；`empty` 只取自 addr FIFO（[L265](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L265)）。

[axi4l_logger.sv:341-343](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L341-L343) 把 FIFO 输出对外暴露为 `r_rnw`（取最低位）、`r_addr`、`r_data`。

#### 4.5.4 代码实践

**目标**：跟踪一次「AXI4-Lite 写」在 logger 内部从握手到入 FIFO 的完整链路，画出关键信号时序图。

**操作步骤**（源码跟踪型实践）：

1. 假定一次写事务：某拍 `awvalid && awready && wvalid && wready` 同时成立（地址 `0x4000_0000` 在窗内，写数据 `0xDEAD_BEEF`）。
2. 顺着 logger 内部依次标出每一拍的信号值：
   - 当拍：`aw_w_req = 1`（[L134-138](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L134-L138)）。
   - 下一拍：`aw_w_req_d1 = 1`（[L97-105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L97-L105)）→ `fifo_wren = 1`（[L169](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L169)）→ `w_addr_f=0x4000_0000, w_data_f=0xDEAD_BEEF, w_rnwr_f=0`（[L171-185](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L171-L185)）→ 三个 FIFO18E1 在 `clk_axi` 上升沿写入（[L236-339](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/axi4l_logger.sv#L236-L339)）。
3. 改参数把 `REG_ADDRESS_FROM` 设为 `0x4001_0000`（高于 `0x4000_0000`），重走第 2 步。

**需要观察的现象**：第 2 步中三元组被完整写入；第 3 步因地址不在窗内，`aw_w_req` 为 0，整条链路不触发。

**预期结果**：你画出一张「握手拍 → +1 拍入 FIFO」的时序图，并能解释地址窗过滤如何切断记录。

**待本地验证**：`FIFO18E1` 是 Xilinx 7 系列专用原语，iverilog/ModelSim 默认无该模型，需挂 Xilinx UNISIM/GLBL 库才能仿真；纯阅读跟踪无需上机。

#### 4.5.5 小练习与答案

- **Q1**：为什么 logger 要用**三个** FIFO，而不是把 addr/data/rnw 拼成一个 65 位字存进一个 FIFO？
  - **答**：三个 FIFO 共享同一写/读使能，等价于「一个事务占一个深度位」，读一次同步弹出完整三元组；用单 FIFO 拼位宽在功能上也行，但这里用三个 `FIFO18E1`（每口 36 位）正好各自存一个 32 位字，资源映射干净，且便于分别观察 addr/data。
- **Q2**：`FILTER_REPETITIVE_READS` 为什么只缓存读、遇到写就清缓存？
  - **答**：写操作改变了寄存器内容，其后的读即使「地址相同」也很可能读到新值，属于应当记录的新事件；只有「连续相同的轮询读」才是无信息量的噪声，需要滤除。写清缓存确保写之后第一次读一定会被记录。

## 5. 综合实践

把 4.1~4.4 的知识串起来：用 `axis_if` + `modport` 搭一条**带反压缓冲**的数据通路，并把 4.2 的「裸 sink」升级成「包了 u4-l2 `fifo_single_clock_ram` 的弹性 sink」。这一步同时用到本讲的接口知识和 u4-l2 的 FIFO，正好印证本讲的依赖关系。

**任务**：写一个 AXIS 从机 `axis_fifo_sink`，内部用 `fifo_single_clock_ram` 缓存收到的数据；上游主机 `axis_src` 持续发计数，下游用一个读进程慢慢读 FIFO。验证两点——(1) 当 FIFO 满时 `tready` 自动拉低（反压）；(2) 读出的数据与写入顺序一致（先进先出）。

**操作步骤**：

示例代码 —— 把 `fifo_single_clock_ram` 包成 AXIS 从机（端口名严格对应 [fifo_single_clock_ram.sv:53-87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L53-L87) 的真实声明）：

```systemverilog
// axis_fifo_sink.sv —— 示例代码
module axis_fifo_sink (
  input  logic clk,
  input  logic rst_n,
  axis_if.slave_mp s_axis,
  // 下游读取侧
  input  logic        r_req,
  output logic [7:0]  r_data,
  output logic        empty
);
  logic full;
  // 关键：tready = "FIFO 没满"，上游握手直接驱动 FIFO 写口
  assign s_axis.tready = ~full;

  fifo_single_clock_ram #(
    .DEPTH ( 8 ),     // 必须为 2 的幂（见 u4-l2）
    .DATA_W( 8 )
  ) buf (
    .clk   ( clk ),
    .nrst  ( rst_n ),
    .w_req ( s_axis.tvalid && s_axis.tready ),  // 握手当拍写入
    .w_data( s_axis.tdata ),
    .r_req ( r_req ),
    .r_data( r_data ),
    .cnt   ( /* 悬空，演示用不到 */ ),
    .empty ( empty ),
    .full  ( full ),
    .fail  ( /* 溢出/下溢指示，演示用不到 */ )
  );
endmodule
```

示例代码 —— testbench：上游持续发、下游慢慢读，制造「FIFO 先填满→反压→再排空」的过程。

```systemverilog
// axis_fifo_demo_tb.sv —— 示例代码
`timescale 1ns/1ps
module axis_fifo_demo_tb;
  logic clk = 1'b0; always #5 clk = ~clk;
  logic rst_n = 1'b0;
  initial begin #23 rst_n = 1'b1; #2000; $finish; end

  axis_if #(.DATA_W(8), .ID_W(0), .USER_W(0)) bus();

  logic        r_req  = 1'b0;
  logic [7:0]  r_data;
  logic        empty;

  axis_src        SRC (.clk(clk), .rst_n(rst_n), .m_axis(bus));
  axis_fifo_sink  SNK (.clk(clk), .rst_n(rst_n), .s_axis(bus),
                       .r_req(r_req), .r_data(r_data), .empty(empty));

  // 下游每隔几拍读一次，让 FIFO 经历“填满→反压→排空”
  initial begin
    #100;
    forever begin
      @(posedge clk);
      r_req <= ~empty;   // 只要非空就读
      #37;               // 人为拖慢读速
    end
  end
endmodule
```

**需要观察的现象**：

- 起初 `r_req` 为 0、上游持续写，`cnt`（FIFO 内部计数，可在波形里展开 `SNK.buf.cnt`）一路涨到 `DEPTH=8`，`full` 拉高。
- `full` 一拉高，`s_axis.tready` 随即变 0，上游 `axis_src` 的 `cnt` 停在原地——**反压生效**，数据不丢。
- 下游开始读后 `full` 撤销，`tready` 回 1，上游继续推进。
- 读侧 `r_data` 出现的序列，恰好是上游写入序列的**先进先出**顺序（注意 normal 模式下 `r_data` 比请求晚一拍，见 u4-l2）。

**预期结果**：波形里能清晰看到「FIFO 满 → tready=0 反压 → 读出后排空」的闭环，且读出顺序与写入一致；任何时刻都没有数据丢失或乱序。

**待本地验证**：综合实践依赖 iverilog 对 interface/modport 与 `fifo_single_clock_ram` 的支持；若 iverilog 报错，可改用 Vivado/ModelSim 仿真。`fifo_single_clock_ram` 为 normal（非 FWFT）模式，`r_data` 在 `r_req` 之后一拍有效，下游采样要相应对齐。

## 6. 本讲小结

- `interface` 把一捆相关信号和参数**打包**成一个类型，`modport` 给同一组信号定义**主/从两种互逆视角**，一举解决「端口爆炸」和「方向接反」两个痛点（`axis_if`/`axi4_if`/`wb_if` 都是这个套路）。
- **AXI4-Stream**（`axis_if`）是无地址单向流，靠 `tvalid/tready` 同高握手传输；`tvalid` 必须先有效且不可依赖 `tready`，否则会形成组合环。
- **AXI4 / AXI4-Lite**（`axi4_if`）是有地址的存储映射总线，拆成 AW/W/B/AR/R **五条独立 valid/ready 通道**，允许解耦与突发；Lite 是其「无突发」精简子集。
- **Wishbone**（`wb_if`）是经典单周期总线，用 `STB/ACK` 一次握手完成一个字，信号少、直观，但表达力和吞吐远不如 AXI。
- **`axi4l_logger`** 是只读并联在 AXI4-Lite 总线上的嗅探器：检测写/读握手 → 延迟一拍快照地址/数据/方向 →（可选）滤重复读 → 经三个双时钟 `FIFO18E1`（FWFT）跨域输出完整事务三元组。
- 阅读这批接口/logger 源码时要以**实际代码**为准：当前 HEAD 的 `axi4_if.sv:67`（`rres` 笔误）、`wb_if.sv:24-25`（`dat` 重复声明）以及 logger 的「1 拍延迟假设」都需要你在使用前自行核对/修复——这正是真实工程阅读的常态。

## 7. 下一步学习建议

- **向「系统互联」迈进**：本讲每个接口都是点对点的「一根线」。多个主/多个从如何通过互联矩阵（AXI Interconnect / Wishbone Crossbar）仲裁？这正是 u6-l2（优先级与轮询仲裁）的主题——`fifo_combiner` 的多路汇聚其实就是一种微型互联，可对照阅读。
- **把 logger 用起来**：找一个含 AXI4-Lite 从机的真实工程（仓库 `axi_master_slave_templates/` 目录里有 Vivado 生成的 AXI 主从模板），把 `axi4l_logger` 并联上去，配合 u7-l1 的自检 testbench 验证日志三元组的正确性。
- **时序约束视角**：`axi4l_logger` 内含跨时钟域 FIFO，其 `clk_axi → clk` 的路径需要在约束里正确处理；读完 u7-l2（时序约束与 false_path）后再回头看 logger，你会更清楚哪些路径要设 `set_false_path`、哪些要设 `set_clock_groups`。
- **继续协议层**：本单元 u5 到此结束。下一单元 u6 从「协议」回到「数据处理与算术」（编码转换、仲裁、加法树/滤波、脉冲整形），其中 `reverse_vector`/`gray2bin` 等组合工具常出现在总线数据通路的预处理环节，可与本讲搭配阅读。
