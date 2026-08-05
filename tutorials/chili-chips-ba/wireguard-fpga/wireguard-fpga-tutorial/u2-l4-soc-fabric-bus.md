# SoC 互联 fabric 与 soc_if 总线

## 1. 本讲目标

学完本讲，你应该能够：

- 说清控制面里 CPU 是怎样「找到」DMEM 和 CSR 的——也就是**地址译码（address decode）**发生在哪、怎么判定的。
- 读懂 `soc_if` 这条总线的**握手信号**（`vld`/`rdy`/`addr`/`we`/`wdat`/`rdat`）以及 `MST`/`SLV` 两个 `modport` 各自的方向约束。
- 理解 `soc_pkg` 里集中定义的**公共类型**（地址位宽、字地址、按字节写使能）为什么这样设计。
- 在脑海里画出一条完整的访问路径：CPU → fabric → CSR，以及返回数据怎么原路回到 CPU。
- 理解当 **UART 也想当主机**（在线烧写/调试）时，fabric 如何在 CPU 与 UART 之间**仲裁（arbitrate）**，保证两者不打架。

本讲是 Unit 3（CSR 细节）和 u2-l5（UART 在线编程）的公共前置：因为不管是 CSR 寄存器读写，还是 UART 的 `BUSR`/`BUSW` 总线访问，最终都要经过 `soc_fabric` 这座「立交桥」。

---

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 总线 = 一组「公用的多芯电缆」

SoC 里有多个部件要互相通信：CPU 要读写内存（DMEM）、要读写控制寄存器（CSR）、UART 调试通道也要访问同样的内存和寄存器。如果每两个部件之间都拉一组专用线，连线数量会爆炸。**总线（bus）** 的思路是：拉一组「公用的多芯电缆」，谁要说话就先举手、获得许可，然后把地址和数据放上线缆，对方应答。

在本项目里，这组电缆就是 `soc_if`。

### 2.2 Valid/Ready 握手

发送方把 `vld`（valid，有效）拉高表示「我有一个请求」；接收方准备好后把 `rdy`（ready，就绪）拉高表示「我接住了」。**当 `vld` 与 `rdy` 同一拍都为 1，这一次传输就完成了**。这种握手和 DPE 数据面的 AXI-Stream（u4-l1）是同一个思想，只不过这里是单笔寄存器/内存访问，不是流式数据。

> 关键约定：一次访问在 `vld & rdy` 都为 1 的那个时钟沿完成。可以一拍完成（单周期），也可以让 `rdy` 晚几拍再拉高（多周期等待），但本总线**没有超时（timeout）机制**——如果地址打到了没人响应的地方，`rdy` 永远不来，总线会一直挂着。

### 2.3 地址译码 = 看门牌号分流

总线上挂了多个「从机」（slave/peripheral）：DMEM、CSR。fabric 收到一个地址后，要判断「这个地址归谁管」，这叫**地址译码**。判定的依据是一张**内存映射表（memory map）**：

| 地址范围 | 归属 | 译码判定（高位比特） |
|---|---|---|
| `0x1000_0000` – `0x1FFF_FFFF` | DMEM | 最高 4 位 `addr[31:28] == 1` |
| `0x2000_0000` – `0x3FFF_FFFF` | CSR | 最高 3 位 `addr[31:29] == 1` |

为什么 DMEM 看 4 位、CSR 看 3 位？后面用源码和二进制算给你看。先记住：**所有 select 必须互斥**——一个地址同一时刻只能选中一个从机。

---

## 3. 本讲源码地图

本讲只涉及三个文件，都在 `1.hw/ip.infra/` 下：

| 文件 | 作用 | 本讲角色 |
|---|---|---|
| `1.hw/ip.infra/soc_pkg.sv` | 定义全 SoC 公共类型与参数 | 总线的「度量衡」：位宽、地址类型、写使能类型 |
| `1.hw/ip.infra/soc_if.sv` | 定义总线接口与 `MST`/`SLV` modport | 总线本身：握手信号与方向约束 |
| `1.hw/ip.infra/soc_fabric.sv` | 中央互联：译码、仲裁、数据多路返回 | 立交桥：把主机的请求路由到正确的从机 |

辅助证据在 `1.hw/top.sv`（实例化这三者，第 159–203 行），用来把抽象的接口落到真实连线上。

---

## 4. 核心概念与源码讲解

### 4.1 公共包定义：soc_pkg.sv（总线的度量衡）

#### 4.1.1 概念说明

SystemVerilog 的 `package`（包）是一个**集中存放共享声明**的地方：类型定义（`typedef`）、参数（`localparam`）、常量。任何模块只要 `import soc_pkg::*;` 就能用里面的类型，避免每个文件各写一遍、改一处要改十处。

`soc_pkg` 把整条总线用到的「度量衡」定死在这里：地址多宽、数据多宽、地址怎么按字对齐、写使能怎么按字节拆。后面所有模块（`soc_if`、`soc_fabric`、CPU、DMEM、CSR）都吃这套统一规格。

#### 4.1.2 核心流程：从「字节地址」到「字地址 + 字节使能」

总线数据宽度是 32 位（4 字节）。一个朴素的设计是：地址按**字节**编址（每个字节一个地址），地址低位 `addr[1:0]` 用来在 4 字节里选某一个字节。但本项目用了一个更精巧、也更省线的做法：

1. 地址总线**不传最低 2 位**（字节在字内的偏移），只传「字地址」（每个 32 位字一个地址）。
2. 这最低 2 位的语义，改由**按字节的写使能 `we[3:0]`** 来表达：哪几个字节要写，对应的 `we` 比特就为 1。

用数学表达：设字节地址为 \(A\)，字地址为 \(W\)，则

\[
W = \left\lfloor A / 4 \right\rfloor \quad\Longleftrightarrow\quad W = A \gg 2
\]

而 `we` 这 4 个比特，正好编码了 \(A \bmod 4\) 这个字节偏移里的「哪些字节被选中」——读时 `we` 全 0，写时非 0。

> 为什么这样省？因为 `we` 无论如何都要存在（要支持子字写入），让它**兼任**地址最低位，就免去了地址总线最末两位的传输。这是总线位宽与控制信号之间的一次小权衡。

#### 4.1.3 源码精读

先看总线宽度的两个总参数：

[soc_pkg.sv:48-49 处定义了总线地址宽度 32 位、数据宽度 32 位](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_pkg.sv#L48-L49)

```systemverilog
localparam SOC_ADDRW = 32;
localparam SOC_DATAW = 32;
```

再看由它们推导出的「字地址」「字节写使能」「数据」三个类型——这是总线真正使用的信号类型：

[soc_pkg.sv:82-87 由数据宽度推导出字地址类型与按字节写使能类型](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_pkg.sv#L82-L87)

```systemverilog
localparam SOC_BYTES = SOC_DATAW / 8;     // 4 for 32-bit data bus
localparam SOC_ADDRL = $clog2(SOC_BYTES); // 2 for 32-bit data bus

typedef logic [SOC_ADDRW-1:SOC_ADDRL] soc_addr_t; // 地址以完整数据字为单位
typedef logic [SOC_BYTES-1:0]         soc_we_t;   // 按字节写使能，兼任地址低位译码
typedef logic [SOC_DATAW-1:0]         soc_data_t; // 写数据
```

注意 `soc_addr_t` 是 `logic [31:2]`：它是一个 30 位的向量，但**比特编号保留为 31 到 2**。这一点非常关键，后面 4.3 节译码时要靠它。也就是说：

- 向量位数 = 30（从 bit 31 到 bit 2）。
- `soc_addr_t` 的值 = 字节地址右移 2 位。
- 但 `addr[31:29]`、`addr[31:28]` 这些**高位切片的比特编号与原始字节地址一一对应**——因为低 2 位只是被「裁掉」了，高位的编号没动。

另外，包里还放了一个 80 MHz 时钟的时间基准（`PERIOD_PS = 12_500`）和一些以太网用的差分类型 `diff_t`，它们和本讲的总线无关，是给定时器与 GMII 用的，这里不展开。

#### 4.1.4 代码实践

**实践目标**：亲手验证「字地址 + 字节使能」这套机制。

1. 打开 `soc_pkg.sv`，确认 `SOC_DATAW=32`、`SOC_BYTES=4`、`SOC_ADDRL=$clog2(4)=2`。
2. 在纸上推演一次 **CPU 写 DMEM 的 1 个字节**：假设要写字节地址 `0x1000_0005`（即 DMEM 区里的第 5 个字节）。
   - 字地址 \(W = \lfloor 0x1000\_0005 / 4 \rfloor = 0x0400\_0001\)，对应 `soc_addr_t` 的取值。
   - 字节偏移 \(0x1000\_0005 \bmod 4 = 1\)，所以 `we = 4'b0010`（只写第 1 个字节）。
3. **需要观察的现象**：写一个字节时 `we` 只有 1 位为 1；写整字时 `we = 4'b1111`；读时 `we = 4'b0000`。

> 结果待本地验证：如果你在仿真里抓到一次总线写，可以核对 `we` 的模式与软件意图是否一致。

#### 4.1.5 小练习与答案

**练习 1**：`soc_addr_t` 这个类型一共有多少位？为什么不是 32 位？

**参考答案**：30 位。因为它是 `logic [31:2]`，去掉了最低 2 位（`bit 1` 和 `bit 0`）。这 2 位是「字内字节偏移」，其语义被转移到了 4 位的 `we` 上，所以地址总线不必再传它们。

**练习 2**：一次「读整字」操作，`we` 应该是什么值？为什么？

**参考答案**：`we = 4'b0000`（全 0）。因为读操作不写任何字节；`soc_if` 里约定「写操作当 `vld & |we`，读操作当 `vld & ~|we`」，所以 `we` 全 0 即代表读。

---

### 4.2 总线握手接口：soc_if.sv（这条「多芯电缆」长什么样）

#### 4.2.1 概念说明

`soc_if` 是一条**接口（interface）**。在 u2-l2 我们说过，interface 把一组相关信号打包成一根「多芯电缆」，`modport` 再规定每个连接方看到的信号方向（input 还是 output）。这样做的好处是：模块端口不用再罗列一堆零散信号，只挂一个接口名即可，连线和方向都不容易出错。

`soc_if` 就是本项目控制面的那根总线电缆：它定义了**有效/就绪握手**、地址、写使能、写数据、读数据，并用 `MST`（主机）和 `SLV`（从机）两个 modport 固定方向。

#### 4.2.2 核心流程：一次访问的生命周期

一次总线访问由主机发起，状态机可以是下面几种（摘自源码注释）：

```
IDLE -> WRITE -> IDLE                          （单拍写）
IDLE -> WRITE -> WAIT -> WAIT -> IDLE          （多拍写，从机插入等待）
IDLE -> READ  -> IDLE                          （单拍读）
IDLE -> READ  -> WAIT -> IDLE                  （多拍读）
IDLE -> WRITE -> WRITE -> READ -> ... -> IDLE  （连续突发）
```

握手规则只有一条：

\[
\text{传输完成} \iff \text{vld} = 1 \;\wedge\; \text{rdy} = 1
\]

主机把 `vld`、`addr`、`we`、`wdat`（写时）放上线；从机准备好就回 `rdy=1`，并在读操作时同时回 `rdat`。`vld` 与 `rdy` 同时为 1 的那个时钟沿，地址/数据被采走，一次传输结束。

> 注意 `we` 的双重身份：它既是「写 vs 读」的标志（`|we` 非零即写），又是「写哪些字节」的子字掩码。

#### 4.2.3 源码精读

接口声明带两个输入：异步复位与时钟（这条总线是同步的，所有信号都在 `clk` 域里跳变）：

[soc_if.sv:54-57 接口声明，输入异步复位 arst_n 与总线时钟 clk](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_if.sv#L54-L57)

```systemverilog
interface soc_if (
   input  logic arst_n,
   input  logic clk
);
   import soc_pkg::*;
```

接着是六个核心信号：

[soc_if.sv:61-67 六个核心信号：vld/rdy 握手，addr/we/wdat/rdat](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_if.sv#L61-L67)

```systemverilog
logic        vld;    // 1 = 发起请求
logic        rdy;    // 1 = 应答；vld&rdy 同时为 1 则传输完成

soc_addr_t   addr;
soc_we_t     we;     // 写当 (vld & |we)，读当 (vld & ~|we)
soc_data_t   wdat;
soc_data_t   rdat;
```

然后是两个 modport，把方向定死。**主机（MST）**：驱动请求侧信号，观察应答侧信号：

[soc_if.sv:72-80 MST modport：主机输出 vld/addr/we/wdat，输入 rdy/rdat](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_if.sv#L72-L80)

```systemverilog
modport MST (
  output vld,
         addr,
         we, wdat,
  input  arst_n, clk,
         rdy,
         rdat
);
```

**从机（SLV）**：方向正好相反——观察请求侧，驱动应答侧：

[soc_if.sv:85-93 SLV modport：从机输入 vld/addr/we/wdat，输出 rdy/rdat](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_if.sv#L85-L93)

```systemverilog
modport SLV (
  input  arst_n, clk,
         vld,
         addr,
         we, wdat,
  output rdy,
         rdat
);
```

> 一个关键直觉：MST 与 SLV 的方向是**互补**的。同一个接口实例，连到主机那端按 MST 看待，连到从机那端按 SLV 看待，二者一对接，一个 output 对一个 input，正好成对。这正是下一节 fabric 端口方向的依据。

#### 4.2.4 代码实践

**实践目标**：弄清「谁驱动谁」。

1. 打开 `top.sv` 第 159–162 行，看四个 `soc_if` 实例（`bus_cpu`/`bus_uart`/`bus_dmem`/`bus_csr`）是怎么声明的：
   [top.sv:159-162 声明四条 soc_if 总线实例，共用 sys_clk 与 sys_rst_n](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L159-L162)
2. 注意 CPU 模块 `u_cpu` 把 `bus_cpu` 当 **MST** 用；而 fabric 把 `bus_cpu` 当 **SLV** 用（见 4.3.3）。同一个 `bus_cpu`，两端 modport 互补。
3. **需要观察的现象**：在 `soc_if` 里，`vld`、`addr`、`we`、`wdat` 一定由 MST 端驱动、SLV 端采样；`rdy`、`rdat` 反过来。

> 结果待本地验证：可在仿真波形里确认一次 CPU 读 CSR 时，`vld` 由 CPU 侧拉高、`rdy`/`rdat` 由 CSR 侧返回。

#### 4.2.5 小练习与答案

**练习 1**：如果某次访问 `vld=1` 但 `rdy` 一直为 0，会发生什么？总线有超时保护吗？

**参考答案**：访问会**一直挂起**，主机停在等待状态。`soc_if.sv` 注释明确写「Multi-cycle (with wait) transactions don't have a TimeOut (TO) mechanism」——本总线**没有超时**。因此软件必须保证只访问已映射的地址（DMEM/CSR 区），否则总线死锁。

**练习 2**：为什么 `rdat` 在 MST modport 里是 `input`，在 SLV modport 里是 `output`？

**参考答案**：读数据由**从机产生**（DMEM/CSR 把读出的内容放在 `rdat` 上），所以对从机是 output、对主机是 input。`wdat`（写数据）方向正好相反，由主机产生。

---

### 4.3 互联 fabric：soc_fabric.sv（译码 + 仲裁 + 多路返回）

#### 4.3.1 概念说明

`soc_fabric` 是控制面的**中央立交桥**。它的端口很能说明问题：

```systemverilog
module soc_fabric (
   soc_if.SLV  cpu,   // CPU 主机接入（fabric 把它当从机方向看）
   soc_if.SLV  uart,  // UART 主机接入
   soc_if.MST  dmem,  // 接出到 DMEM（fabric 当主机方向）
   soc_if.MST  csr    // 接出到 CSR
);
```

注意方向：`cpu`/`uart` 是 **SLV**（fabric 在这两个方向上「接收」主机的请求），`dmem`/`csr` 是 **MST**（fabric 在这两个方向上「驱动」从机）。这正好呼应 4.2 的「MST 与 SLV 互补」。

fabric 干三件事：

1. **地址译码**：根据 `addr` 高位判断这次访问归 DMEM 还是 CSR（或谁都不归）。
2. **数据多路返回（read mux）**：把被选中的从机的 `rdy`/`rdat` 回送给发起请求的主机。
3. **双主机仲裁**：CPU 和 UART 都是主机，当两者都想用总线时，决定谁先用，且不发生碰撞。

为什么会有两个主机？UART 不只是字符 CLI（u2-l5 会详讲）：它还有一种「特殊模式」，通过 `BUSR`/`BUSW` 命令直接读写 DMEM/CSR，从而实现**在线烧写指令内存、原子地观察/修改整片内存**。这时 UART 也要当总线主机，于是 fabric 必须仲裁。

#### 4.3.2 核心流程：一次 CPU → CSR 读的全链路

```
1) CPU 拉高 bus_cpu.vld，给出 addr / we=0（读）
2) fabric 仲裁：cpu_ack = cpu.vld & ~uart_busy
     —— UART 没在占用总线，CPU 立即获得总线
3) fabric 把 addr/we/wdat 透传到 dmem 和 csr 两条出线
     —— 用 cpu_ack 在 cpu 与 uart 之间二选一
4) fabric 译码：csr_sel = addr[31:29]==1（且 dmem_sel 互斥为 0）
5) fabric 把 vld 只送给被选中的从机：csr.vld = (cpu.vld) & csr_sel
6) CSR 从机处理读请求，回 csr.rdy=1、csr.rdat=<寄存器值>
7) fabric 把 csr.rdy/rdat 经 mux 回送给 CPU（cpu.rdat = csr.rdat）
8) bus_cpu 上 vld & rdy 同拍为 1 —— 一次读完成
```

如果此刻 UART 也在请求总线（在线编程），第 2 步的仲裁结果会不同，CPU 会被 `& ~uart_busy` 挡住——这就是 4.3.5 要讲的仲裁。

#### 4.3.3 源码精读：端口与「CPU vs UART」二选一

fabric 把两个主机的地址/控制信号，按 `cpu_ack` 二选一地透传给两条出线：

[soc_fabric.sv:71-77 按 cpu_ack 在 CPU 与 UART 之间选择 addr/we/wdat 透传给 dmem 与 csr](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_fabric.sv#L71-L77)

```systemverilog
assign dmem.addr = cpu_ack ? cpu.addr : uart.addr;
assign dmem.we   = cpu_ack ? cpu.we   : uart.we;
assign dmem.wdat = cpu_ack ? cpu.wdat : uart.wdat;

assign csr.addr  = cpu_ack ? cpu.addr : uart.addr;
assign csr.we    = cpu_ack ? cpu.we   : uart.we;
assign csr.wdat  = cpu_ack ? cpu.wdat : uart.wdat;
```

注意 `addr/we/wdat` 同时送到 `dmem` 和 `csr` 两条线——**地址译码只决定 `vld` 给谁**，地址本身不挑路。

#### 4.3.4 源码精读：地址译码（本讲的核心）

这一段是「看门牌号」的全部逻辑：

[soc_fabric.sv:83-91 地址译码：dmem_sel 看 addr[31:28]，csr_sel 看 addr[31:29]；vld 只送给被选中的从机](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_fabric.sv#L83-L91)

```systemverilog
logic dmem_sel, csr_sel;

// 所有 select 必须互斥！
assign dmem_sel = cpu_ack ? (cpu.addr[31:28] == 4'd1) : (uart.addr[31:28] == 4'd1); // 0x1000_0000 - 0x1FFF_FFFF
assign csr_sel  = cpu_ack ? (cpu.addr[31:29] == 3'd1) : (uart.addr[31:29] == 3'd1); // 0x2000_0000 - 0x3FFF_FFFF

assign dmem.vld = (uart.vld | cpu.vld) & dmem_sel;
assign csr.vld  = (uart.vld | cpu.vld) & csr_sel;
```

把最高几位展开成二进制看，互斥关系一目了然：

| 最高位（字节地址视角） | `addr[31:28]` | `addr[31:29]` | 选中 |
|---|---|---|---|
| `0x0_xxxx_xxxx` | `0000` | `000` | 都不选（保留/IMEM 区） |
| `0x1_xxxx_xxxx` | `0001` | `000` | **DMEM**（`dmem_sel=1`） |
| `0x2_xxxx_xxxx` | `0010` | `001` | **CSR** |
| `0x3_xxxx_xxxx` | `0011` | `001` | **CSR** |
| `0x4_xxxx_xxxx` 及以上 | `0100`+ | `010`+ | 都不选 |

所以：

- DMEM 占 `0x1` 一个最高位组（256 MB 窗口），用 4 位 `addr[31:28]==1` 判定。
- CSR 占 `0x2`、`0x3` 两个最高位组（共 512 MB 窗口），用 3 位 `addr[31:29]==1` 判定（因为 `0x2`/`0x3` 的最高 3 位都是 `001`）。

> 回想 4.1.3 的要点：`soc_addr_t` 是 `logic [31:2]`，虽然去掉了低 2 位，但**高位的比特编号与字节地址一致**，所以 `addr[31:28]`、`addr[31:29]` 直接等价于字节地址的高位判定。源码注释里的 `0x1000_0000 - 0x1FFF_FFFF` 就是字节地址范围。

源码注释还提醒一句（第 50–51 行）：这是**总分配窗口**，实际资源小得多并会**别名（alias）**。比如 DMEM 实际只有 64 KB（`top.sv` 里 `NUM_WORDS_DMEM=16384` 即 64 KB），但译码窗口是 256 MB，于是这 64 KB 在窗口里会重复出现很多次。软件靠链接脚本（`link_map.lds`）把一切放在窗口基地址，别名就不会被踩到。

#### 4.3.5 源码精读：数据多路返回与双主机仲裁

被选中的从机返回 `rdy`/`rdat`，fabric 用同一个 `dmem_sel/csr_sel` 把它们回送给**当前持有总线的主机**：

[soc_fabric.sv:94-109 把选中从机的 rdy/rdat 经 mux 回送给 CPU 和 UART](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_fabric.sv#L94-L109)

```systemverilog
// 回送给 UART
assign uart.rdy  = dmem_sel ? dmem.rdy & uart_busy
                 : csr_sel  ? csr.rdy  & uart_busy
                            : 1'b0;
assign uart.rdat = dmem_sel ? dmem.rdat
                 : csr_sel  ? csr.rdat
                            : '0;

// 回送给 CPU
assign cpu.rdy   = dmem_sel ? dmem.rdy & ~uart_busy
                 : csr_sel  ? csr.rdy  & ~uart_busy
                            : 1'b0;
assign cpu.rdat  = dmem_sel ? dmem.rdat
                 : csr_sel  ? csr.rdat
                            : '0;
```

注意两个细节：

- **未映射地址 → `rdy=0`**：`dmem_sel`/`csr_sel` 都为 0 时，`rdy` 给 0、`rdat` 给 0。结合 4.2.5「无超时」的结论，访问未映射地址会让主机永远等待——这正是为什么软件必须只用合法地址。
- **`& uart_busy` / `& ~uart_busy`**：即便从机已就绪，CPU 的 `rdy` 在 UART 占线时也被屏蔽为 0（CPU 等）；UART 的 `rdy` 在它没拿到总线时也被屏蔽为 0（UART 等）。这就是仲裁的「执行侧」。

仲裁的「决策侧」是一个寄存器 `uart_busy`，CPU 优先但 UART 不可被抢占：

[soc_fabric.sv:124-138 仲裁状态机 uart_busy：CPU 同时刻请求则 CPU 赢；UART 一旦拿到总线就持有到事务结束](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_fabric.sv#L124-L138)

```systemverilog
always_ff @(negedge cpu.arst_n or posedge cpu.clk) begin
   if (cpu.arst_n == 1'b0) begin
      uart_busy <= 1'b0;
   end
   else begin
      uart_busy <= uart_busy
                   ? ~uart_done | uart.vld          // 持有：直到完成且不再请求
                   : ({cpu.vld, uart.vld} == 2'b01); // 获取：仅当 CPU 不请求、UART 请求
   end
end
```

配两条组合逻辑：

[soc_fabric.sv:141-144 uart_done=选中从机已就绪；cpu_ack=CPU 请求且 UART 不占线](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_fabric.sv#L141-L144)

```systemverilog
assign uart_done = (dmem_sel & dmem.rdy) | (csr_sel & csr.rdy);
assign cpu_ack   = cpu.vld & ~uart_busy;
```

仲裁规则归纳成三条（与源码注释 119–122 行一致）：

1. **CPU 与 UART 同时请求 → CPU 赢**：获取条件 `{cpu.vld, uart.vld} == 2'b01` 要求 `cpu.vld=0`，所以同时请求时 UART 拿不到、`uart_busy` 保持 0，于是 `cpu_ack=1`。
2. **UART 已在用总线 → CPU 必须等**：`cpu_ack = cpu.vld & ~uart_busy`，UART 占线期间 CPU 被挡。这正是 UART 在线编程 `BUSR`/`BUSW` 能**原子地**读写整片 DMEM/CSR 的硬件保障——它把 CPU 暂停了。
3. **CPU 正在用 → UART 等**：UART 只在 CPU 不请求时获取总线；CPU 单笔访问通常一两拍就结束，UART 很快就能插进来。

把这套仲裁落实在 `top.sv` 的实例化里：

[top.sv:183-203 fabric 把两条主机总线(bus_cpu/bus_uart)接到 SLV 端，两条从机总线(bus_dmem/bus_csr)接到 MST 端](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L183-L203)

```systemverilog
soc_fabric u_fabric (
   .cpu  (bus_cpu),   // SLV
   .uart (bus_uart),  // SLV
   .dmem (bus_dmem),  // MST
   .csr  (bus_csr)    // MST
);

soc_ram #(.NUM_WORDS(NUM_WORDS_DMEM)) u_dmem (.bus(bus_dmem)); // SLV
soc_csr u_soc_csr (.bus(bus_csr), .hwif_in(to_csr), .hwif_out(from_csr)); // SLV
```

至此整条路径闭环：CPU（MST）→ `bus_cpu` → fabric（SLV 入 / MST 出）→ `bus_csr` → `soc_csr`（SLV）。

#### 4.3.6 代码实践（本讲指定实践）

**实践目标**：在 `soc_fabric.sv` 里找到 DMEM 与 CSR 的译码条件，并手算一次「CPU 访问某个 CSR 寄存器」的译码判定。

**操作步骤**：

1. 打开 `soc_fabric.sv`，定位到第 86–87 行的 `dmem_sel`/`csr_sel` 两条赋值（见 4.3.4 的永久链接）。
2. 选一个具体的 CSR 寄存器字节地址，例如 `A = 0x2000_0010`（CSR 区起点 `0x2000_0000` 偏移 `0x10`）。
3. 计算 `soc_addr_t` 上的判定值。因为 `soc_addr_t` 高位比特编号与字节地址一致，直接看字节地址高位：
   - `addr[31:28]`：`0x2` 的二进制是 `0010` → `addr[31:28] == 4'd1`？`0010 == 0001`？**否** → `dmem_sel = 0`。
   - `addr[31:29]`：最高 3 位是 `001` → `addr[31:29] == 3'd1`？**是** → `csr_sel = 1`。
4. 追踪 `vld`：`csr.vld = (cpu.vld | uart.vld) & csr_sel = cpu.vld & 1 = cpu.vld`（假设 UART 闲）。CSR 被选中、收到请求。
5. 追踪返回：`cpu.rdat = csr_sel ? csr.rdat : ... = csr.rdat`；`cpu.rdy = csr.rdy & ~uart_busy = csr.rdy`。CSR 就绪后，读数据回到 CPU。

**需要观察的现象**：

- 对 `0x2000_0010`：`dmem_sel=0`、`csr_sel=1`，请求只送到 CSR，不送到 DMEM。
- 改试一个 DMEM 地址 `A = 0x1000_0020`：`addr[31:28]==0001==1` → `dmem_sel=1`；`addr[31:29]==000 != 1` → `csr_sel=0`。请求只送到 DMEM。
- 改试一个未映射地址 `A = 0x4000_0000`：两个 select 都为 0 → `rdy=0`，主机挂起（印证 4.2.5 的「无超时」）。

**预期结果**：你能用一张表把任意字节地址映射到「选中谁 / 不选 / 挂起」三种结果之一。

> 结果待本地验证：可在仿真中对 `bus_cpu` 分别驱动上述三类地址，观察 `dmem.vld`/`csr.vld` 与 `cpu.rdy` 的变化。

#### 4.3.7 小练习与答案

**练习 1**：为什么 DMEM 用 `addr[31:28]`（4 位）译码，而 CSR 用 `addr[31:29]`（3 位）？

**参考答案**：因为两块区域占的最高位组数量不同。DMEM 只占 `0x1` 一个最高位组，所以只需也必须用 4 位精确锁定 `0001`；CSR 占 `0x2` 和 `0x3` 两个最高位组，它们的最高 3 位都是 `001`，所以用 3 位 `addr[31:29]==1` 就能同时覆盖两者。两者判定结果天然互斥。

**练习 2**：当 UART 正在执行 `BUSW`（在线写一片 CSR）时，CPU 恰好也要读 DMEM，会发生什么？CPU 会读到脏数据吗？

**参考答案**：UART 占线期间 `uart_busy=1`，于是 `cpu_ack = cpu.vld & ~uart_busy = 0`，CPU 被挡在总线外（`cpu.rdy` 也被 `& ~uart_busy` 屏蔽为 0）。CPU 必须等 UART 整个事务结束。所以**不会读到脏数据**——这正是 UART 在线访问能保证「原子性」的原理（u2-l5 会详细讲 UART 如何利用这一点）。

**练习 3**：如果把一个未映射地址（如 `0x4000_0000`）送上总线，`dmem_sel` 和 `csr_sel` 各是多少？主机会怎样？

**参考答案**：两者都为 0。于是 `dmem.vld=0`、`csr.vld=0`，没有任何从机应答，`cpu.rdy=0` 且 `cpu.rdat=0`。由于总线无超时，主机将**永远等待**——这是软件必须避免的非法访问。

---

## 5. 综合实践

把本讲三块知识（公共类型、握手接口、fabric 译码/仲裁）串起来，完成下面这个端到端追踪任务。

**任务**：软件要往 CSR 区的某个寄存器写一个 32 位字。请画出并叙述这次写访问从 CPU 发起到 CSR 收数据的**完整信号旅程**，并回答两个加问题。

1. 选定字节地址 `A = 0x2000_000C`（CSR 区起点偏移 12）。
2. 写出 CPU 此刻应驱动的 `bus_cpu` 信号：`vld`、`addr`（字地址）、`we`（写整字）、`wdat`。
3. 在 fabric 内部：给出 `cpu_ack`、`dmem_sel`、`csr_sel`、`csr.vld` 的取值与依据。
4. 跟踪返回路径：`cpu.rdy` 何时变 1？由谁驱动？
5. **加问 A（仲裁）**：如果这次写发生时，UART 恰好已先一步拿到总线（`uart_busy=1`），CPU 的写会被立即执行吗？为什么？
6. **加问 B（别名）**：DMEM 实际只有 64 KB，但译码窗口是 256 MB。地址 `0x1000_0000` 和 `0x1100_0000` 访问的是同一个物理字吗？为什么软件不会因此出错？

**参考要点**：

- `addr`（字地址）= `0x2000_000C >> 2 = 0x0800_0003`；`we = 4'b1111`（整字写）；`wdat` = 待写数据。
- `cpu_ack = cpu.vld & ~uart_busy`；`dmem_sel = 0`（`0x2` 的 `addr[31:28]=0010 != 1`）；`csr_sel = 1`（`addr[31:29]=001 == 1`）；`csr.vld = cpu.vld & csr_sel = 1`。
- `cpu.rdy` 在 CSR 处理完成、`csr.rdy=1` 时变 1，由 fabric 经 `csr_sel ? csr.rdy & ~uart_busy` 回送。
- 加问 A：不会立即执行。`uart_busy=1` 使 `cpu_ack=0`、`cpu.rdy` 被屏蔽，CPU 等到 UART 事务结束。
- 加问 B：是同一个物理字（64 KB RAM 在 256 MB 窗口里别名重复）。软件靠 `link_map.lds` 把所有数据放在窗口基地址附近，不会踩到别名副本。

> 结果待本地验证：可在仿真里对 `bus_cpu` 驱动上述写，在 `bus_csr` 上核对收到的 `addr/we/wdat`，并人为拉高一个模拟的 `uart_busy` 观察 CPU 的等待行为。

---

## 6. 本讲小结

- `soc_pkg.sv` 是总线的「度量衡」：定死 32 位地址/32 位数据，用**字地址 `soc_addr_t = logic[31:2]` + 按字节写使能 `soc_we_t`** 取代字节地址的最低 2 位，`we` 兼任「写/读」标志与子字掩码。
- `soc_if.sv` 是控制面的总线电缆：`vld`/`rdy` 握手（同拍为 1 即完成），`MST`/`SLV` 两个 modport 把方向定死且互补；本总线**没有超时**，非法地址会永久挂起。
- `soc_fabric.sv` 是中央立交桥，干三件事：**地址译码**（DMEM 看 `addr[31:28]==1`、CSR 看 `addr[31:29]==1`，互斥）、**数据多路返回**（按 select 把 `rdy/rdat` 回送给主机）、**双主机仲裁**（CPU 与 UART 同时请求时 CPU 赢，但 UART 一旦持总线则不可被抢占）。
- 译码窗口远大于实际资源并会**别名**，软件靠链接脚本把数据放在基地址来回避。
- UART 的「特殊模式」能当主机在线读写 DMEM/CSR，靠的正是仲裁把 CPU 暂停——这是原子在线编程的硬件基础（承接 u2-l5）。
- 同一个 `soc_if` 实例（如 `bus_cpu`）在 CPU 端按 MST、在 fabric 端按 SLV，两端方向互补、一一对接，这正是 modport 的意义。

---

## 7. 下一步学习建议

- **紧接着读 u2-l5（UART 子系统与 IMEM 在线编程）**：那里会用到本讲的 `uart` 主机端口与 `BUSR`/`BUSW` 命令，理解 fabric 仲裁如何让 UART 暂停 CPU、实现原子内存访问。
- **进入 Unit 3（CSR）**：u3-l1 会从 `csr.rdl` 出发讲 CSR 寄存器规格，u3-l2 讲 PeakRDL 如何生成 `soc_csr` 这个从机模块——本讲的 `bus_csr`（SLV）正是它的对外接口，二者对照阅读效果最好。
- **想看仲裁与握手在数据面的对应**：可提前翻阅 u4-l1，对比 `dpe_if` 的 AXI-Stream 握手（`TVALID`/`TREADY`）与本讲 `vld`/`rdy` 的异同——思想一致，但数据面是流式 128 位、控制面是单笔 32 位。
- **建议同步打开的源码**：`1.hw/top.sv`（第 159–203 行实例化）、`1.hw/ip.infra/soc_ram.sv`（DMEM 从机侧）与生成的 `csr.sv`（CSR 从机侧），把「主机—fabric—从机」的三角在真实连线上坐实。
