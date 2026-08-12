# elink 总体架构与 IO 协议

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 **elink 是什么**：一条连接 FPGA 与 ASIC（如 Epiphany 多核芯片）的点对点高速串行链路，以及它为什么用「差分 + 源同步时钟」的物理层。
- 画出 elink **物理层 IO 协议**：源同步时钟 `LCLK`、帧信号 `FRAME`、8 位 DDR 数据总线、以及读/写两路 `WAIT` 反压信号，并能解释一个事务如何被拆成一串字节（`B00`–`B13`）在线上传输。
- 讲清 **TX/RX 的划分**与系统侧 **六个 emesh 通道**（`rxwr/rxrd/rxrr/txwr/txrd/txrr`）的来源、方向与各自承载的事务类型。
- 理解 **差分 p/n 信号**的含义，以及为什么反压信号 `WAIT` 必须经过两级同步器采样。

本讲是第 7 单元（elink 高速链路）的总览，**只看顶层 `elink.v` 和 README**，不深入 `etx`/`erx` 内部流水线（那是 u7-l2、u7-l3 的任务），也不展开配置寄存器（u7-l4）。

## 2. 前置知识

本讲假定你已经掌握以下内容（来自前面的讲义）：

- **emesh 104 位包格式**（u5-l1）：一个定长包 `PW = 2×AW + 40`，默认 `AW=32` 时 `PW=104`，字段从低到高为 `write[0]`、`datamode[2:1]`、`ctrlmode[6:3]`、`reserved[7]`、`dstaddr[39:8]`、`data[71:40]`、`srcaddr[103:72]`；伴随 `access`（≈有效）与 `wait`（高有效反压）一对握手信号。elink 在系统侧正是用这个 104 位包和外部对话。
- **access/wait 握手**（u5-l1）：一次事务成立当且仅当同一拍 `access=1` 且 `wait=0`。
- **跨时钟域（CDC）与同步器**（u2-l4）：信号跨异步时钟域时会出现亚稳态，标准对策是串多级触发器（两级为工程底线）。
- **FIFO 与 `oh_fifo_cdc`**（u3-l2）：把 valid/ready（这里是 access/wait）握手包到 FIFO 里跨时钟域传递。

下面几个术语本讲会用到，先做个通俗解释：

- **LVDS（Low-Voltage Differential Signaling，低电压差分信号）**：用两根线（`_p` 正向、`_n` 负向）传一个逻辑位，接收端看两根线的电压差。差分传输抗共模噪声强、翻转电流小，适合高速。
- **源同步（source synchronous）**：发送方不仅发数据，还顺便发一个与时钟对齐的时钟信号给接收方，让接收方用它来采样数据。好处是不依赖接收方本地时钟与数据是否对齐。
- **DDR（Dual Data Rate，双数据率）**：时钟的上升沿和下降沿各采一次数据，一个时钟周期传 2 位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [elink/hdl/elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v) | elink 顶层模块。定义全部对外端口（物理 IO + 系统侧六通道），并实例化 `erx`、`etx`、`elink_cfg`、`ecfg_cdc`。本讲的主要精读对象。 |
| [elink/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md) | elink 的权威说明文档：物理 IO 协议、帧格式、字节排布、104 位包格式、寄存器表、时钟域。波形图与字节表都来自这里。 |
| [elink/dv/dut_elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v) | 仿真用的 dut 包装。把两个 elink 实例「背靠背」回环连接（一个的 TX 接另一个的 RX），是理解物理 IO 方向的最佳范例。 |
| [elink/dv/tests/test_hello.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf) | 一个最小激励文件：16 次写 + 16 次读，用来观察事务如何流入流出 elink。 |

> ⚠️ 一贯原则：README 与脚本可能滞后于代码，遇到不一致以 RTL 源码为准。本讲引用的端口清单已与 `elink.v` 逐行核对。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 LVDS 差分 IO 与源同步时钟**、**4.2 物理层帧格式（FRAME + 8 位 DDR + WAIT 反压）**、**4.3 TX/RX 划分与系统侧六大 emesh 通道**。

### 4.1 LVDS 差分 IO 与源同步时钟

#### 4.1.1 概念说明

elink 要在两块芯片之间（典型场景：FPGA 与 Epiphany ASIC）传数据，距离可能几厘米到几十厘米，时钟频率又高。如果用「一根线传一个位」的单端方式，走线上的噪声、反射、串扰会让信号眼图糊掉。

LVDS 的解法是：**每个逻辑位用两根线传**，一根叫 `_p`（positive），一根叫 `_n`（negative），两根线always 携带大小相等、方向相反的电流。接收端只看两根线的**电压差**判断逻辑值。因为噪声通常同时耦合到两根线上（共模噪声），做差后被抵消，所以差分信号抗干扰能力远强于单端。

源同步（source synchronous）则是另一个关键设计：**发送方把采样自己需要的时钟也一起发过去**。这样接收方用的就是发送方的时钟来采数据，数据和时钟经历了相似的走线延迟，彼此的相对位置（相位关系）更稳定，不必依赖接收方本地时钟去「猜」采样点。elink 把这个随数据同发的时钟叫 **LCLK**。

README 开篇一句话点明了 elink 的定位和物理层选用：

> The "elink" is a low-latency/high-speed interface for communicating between FPGAs and ASICs ... can achieve up to 8 Gbit/s (duplex) ... using 24 LVDS signal pairs.
> —— [elink/README.md:6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L6)

#### 4.1.2 核心流程

一个 elink 实例同时是「发送器」和「接收器」（全双工）。物理层用到的差分对有四类：

1. **LCLK**：源同步时钟（一对）。
2. **FRAME**：帧信号（一对），标记一个事务的起止。
3. **DATA[7:0]**：8 位数据（8 对），DDR 方式传输。
4. **WAIT**：反压信号（读、写各一对），接收方用来告诉发送方「我快满了，只能再收一个」。

由于全双工，上述信号在一个 elink 上同时存在 TX（本端发送）与 RX（本端接收）两套。粗略地说，单向数据吞吐率为：

\[
\text{吞吐率}_{\text{单方向}} = 8\;(\text{数据对数}) \times 2\;(\text{DDR，每周期 2 位}) \times f_{\text{LCLK}} = 16\, f_{\text{LCLK}}
\]

也即数据带宽只跟「数据对数」和 LCLK 频率相关，LCLK、FRAME、WAIT 是「伴生」信号。

#### 4.1.3 源码精读

elink 顶层把物理 IO 直接暴露为端口。参数定义了位宽与身份：

[elink/hdl/elink.v:15-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L15-L20) 定义了 `AW=32`（地址宽）、`DW=32`（数据宽）、`PW=104`（包宽，即 emesh 包）、`ID=12'h810`（本 elink 的片上 ID，对应地址 `addr[31:20]`）、`TARGET="XILINX"`（目标工艺，影响 hard 宏选择）。

接收器（RX）一侧的物理输入是差分对，每个信号都带 `_p/_n`：

```verilog
input     rxi_lclk_p,   rxi_lclk_n;    // rx clock input
input     rxi_frame_p,  rxi_frame_n;   // rx frame signal
input [7:0] rxi_data_p, rxi_data_n;    // rx data
output    rxo_wr_wait_p,rxo_wr_wait_n; // rx write pushback output
output    rxo_rd_wait_p,rxo_rd_wait_n; // rx read pushback output
```
> —— [elink/hdl/elink.v:32-36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L32-L36)：注意 `rxi_data_p/n` 都是 `[7:0]`，即 8 对数据差分线；`rxo_*_wait` 是本接收器向对端发出的反压。

对称地，发送器（TX）一侧把这些信号作为输出：

```verilog
output      txo_lclk_p,   txo_lclk_n;    // tx clock output
output      txo_frame_p,  txo_frame_n;   // tx frame signal
output [7:0] txo_data_p,  txo_data_n;    // tx data
input       txi_wr_wait_p,txi_wr_wait_n; // tx write pushback input
input       txi_rd_wait_p,txi_rd_wait_n; // tx read pushback input
```
> —— [elink/hdl/elink.v:41-45](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L41-L45)：`txo_lclk` 就是本端发出的源同步时钟 LCLK；`txi_*_wait` 是对端回送的反压，作为输入。

把端口按差分对数一数：TX 方向 `txo_lclk`(1) + `txo_frame`(1) + `txo_data[7:0]`(8) = 10 对输出；RX 方向 `rxi_lclk`(1) + `rxi_frame`(1) + `rxi_data[7:0]`(8) = 10 对输入；反压 `rxo_wr/rd_wait`(2) + `txi_wr/rd_wait`(2) = 4 对。**合计 10+10+4 = 24 对**，正好对上 README 宣称的「24 LVDS signal pairs」。

#### 4.1.4 代码实践

**实践目标**：亲手核对「24 对差分线」的说法，并区分哪些是输入、哪些是输出。

**操作步骤**：

1. 打开 [elink/hdl/elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v)。
2. 在端口列表（第 1–91 行）里找出所有同时带 `_p` 和 `_n` 后缀的信号。
3. 按下表分类填入（已给出表头）：

| 类别 | 信号名（去掉 _p/_n） | 方向（input/output） | 差分对数 |
|------|----------------------|----------------------|----------|
| TX 时钟 | txo_lclk | output | 1 |
| TX 帧 | txo_frame | output | 1 |
| TX 数据 | txo_data[7:0] | output | 8 |
| TX 反压（输入） | txi_wr_wait, txi_rd_wait | input | 2 |
| RX 时钟 | … | … | … |
| RX 帧 | … | … | … |
| RX 数据 | … | … | … |
| RX 反压（输出） | … | … | … |

**需要观察的现象 / 预期结果**：把 8 行的「差分对数」加起来应等于 24。如果对不上，检查是否漏掉了某对（例如把 `txo_data_p/n` 当成 1 对而不是 8 对）。本实践为源码阅读型，无需运行仿真，「待本地验证」仅指请你自行填表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 elink 不直接用接收方本地的 `sys_clk` 去采样 `rxi_data`，而要专门传一个 `rxi_lclk`？

**参考答案**：因为两块芯片的本地时钟独立、频率/相位不锁定，且数据和时钟经过不同的走线延迟后到达接收端的相对相位不可控。源同步地传一个 `rxi_lclk`，让接收端用它（及其 90° 移相版）去采数据，可以保证时钟沿落在数据眼中央。`sys_clk` 只服务于系统侧（AXI/emesh）逻辑，与线上的 DDR 比特流不同步。

**练习 2**：`rxi_data_p` 和 `rxi_data_n` 两根线，如果外部干扰让两根线的电压同时升高 50 mV，接收端判断的逻辑值会变吗？

**参考答案**：不会。差分接收看的是两根线的电压差，同时升高（共模扰动）不影响差值，这正是 LVDS 抗噪的原理。

---

### 4.2 物理层帧格式：FRAME + 8 位 DDR + WAIT 反压

#### 4.2.1 概念说明

物理层只有时钟、帧、8 位数据线，但一个 elink 事务（一个 emesh 包）有 104 位。怎么把 104 位塞进 8 位宽的总线？答案是把事务**拆成一串字节**，按固定顺序逐字节发送，并用 **FRAME** 信号标出事务的边界。

这里有一个关键概念 **DDR（双数据率）**：LCLK 的上升沿和下降沿各传一个字节，所以一个 LCLK 周期传 2 个字节。README 用一张波形图描述这个过程：

```
          ___     ___     ___     ___     ___     ___     ___     ___
 LCLK  \___/   \___/   \___/   \___/   \___/   \___/   \___/   \___/
       _______________________________________________________________
 FRAME _/                                                        \______
DATA  XXXX|B00|B01|B02|B03|B04|B05|B06|B07|B08|B09|B10|B11|B12|B13|B14
```
> —— [elink/README.md:36-44](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L36-L44)：FRAME 上升沿标记新事务开始，第一个上升沿采到的字节是 `B00`。

`B00`–`B13` 这串字节如何对应到 emesh 包的字段？README 给了字节排布表，核心几行是：

| 字节 | 内容 | 说明 |
|------|------|------|
| B00 | `R0000A00` | `R=1` 表示读事务；`A=1` 表示 burst 自增 |
| B01 | `{ctrlmode[3:0], dstaddr[31:28]}` | 控制模式 + 目标地址高 4 位 |
| B02–B05 | dstaddr 其余位 + `{dstaddr[3:0], datamode[1:0], write, access}` | 地址低位与控制位 |
| B06–B09 | `data[31:24] … data[7:0]` | 32 位写数据（或读响应数据） |
| B10–B13 | `data[63:56]…data[39:32]` 或 `srcaddr[31:0]` | 64 位写的高 32 位，或读请求的回信地址 |

> 完整字节表见 [elink/README.md:46-68](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L46-L68)。

可以看到：**一次普通 32 位事务就是 `B00`–`B09` 共 10 个字节**（5 个 LCLK 周期，因为 DDR 每周期 2 字节）。读请求时 `B10–B13` 装的是 `srcaddr`（回信地址），写事务时装的是 `data[63:32]`。

#### 4.2.2 核心流程

一个事务在线上的发送流程：

1. 发送方在某个 LCLK 上升沿把 **FRAME 拉高**，同时开始传 `B00`。
2. 之后每个 LCLK 周期（DDR）传 2 个字节：`B01,B02` → `B03,B04` → …。
3. 到 `B09`（32 位事务）或 `B13`（带 srcaddr/64 位）时，事务主体传完。
4. 若 FRAME 在 `B13` 之后仍保持高电平，进入 **burst（突发）模式**：紧接 `B13` 之后直接发下一个事务的 `B06`（不再发 `B00` 头），实现连续数据流。
5. 接收方若来不及处理，拉高 **WAIT** 反压。

关于反压，README 明确了两条规则（[elink/README.md:72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L72)）：

- 接收方在活动传输中拉高 WAIT，含义是「**我只能再收一个事务**」。
- 发送方看到的 WAIT 相位未指定（仍是 LCLK 周期量级），**必须用两级同步器采样**；若 WAIT 在某个事务传到一半时变高，**当前这个事务必须不打断地传完**。

「必须用两级同步器」正是 u2-l4 讲过的 CDC 同步器——WAIT 是接收方时钟域的信号，跨到发送方时钟域必须打两拍抗亚稳态。

#### 4.2.3 源码精读

帧格式与字节排布是**协议约定**，定义在 README 而非 `elink.v` 里。`elink.v` 只暴露承载这些比特的物理管脚（见 4.1.3），真正的拆包/组包在 `erx_protocol` / `etx_protocol`（u7-l2、u7-l3 精读）。本讲只确认两件事：

第一，**WAIT 分读/写两路**。在 `elink.v` 端口里，RX 输出的反压分写与读：

```verilog
output rxo_wr_wait_p,rxo_wr_wait_n; // rx write pushback output
output rxo_rd_wait_p,rxo_rd_wait_n; // rx read pushback output
```
> —— [elink/hdl/elink.v:35-36](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L35-L36)：因为写事务和读事务走不同的内部 FIFO（见 4.3），它们各自可能拥塞，所以反压也分两路，粒度更细。

第二，系统侧的 **104 位包格式**与 u5-l1 的 emesh 包完全一致。README 把它重新列了一遍（[elink/README.md:92-100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L92-L100)）：

| 字段 | 比特位 | 说明 |
|------|--------|------|
| write | [0] | 1=写事务 |
| datamode[1:0] | [2:1] | 数据宽度 8/16/32/64 位 |
| ctrlmode[3:0] | [6:3] | Epiphany 专用控制模式 |
| reserved | [7] | 保留 |
| dstaddr[31:0] | [39:8] | 目标地址 |
| data[31:0] | [71:40] | 写数据 / 读响应数据 |
| srcaddr[31:0] | [103:72] | 读请求回信地址 / 写高 32 位数据 |

也就是说：**系统侧是 104 位并行包，物理侧是按字节串行化的 `B00–B13`，两者是同一事务的两种表示**。`etx_protocol` 负责把 104 位包拆成字节流，`erx_protocol` 负责把字节流还原成 104 位包。

#### 4.2.4 代码实践

**实践目标**：把一个真实激励事务「摊」到字节流 `B00…B09` 上，建立 104 位包与字节流的直觉。

**操作步骤**：

1. 打开 [elink/dv/tests/test_hello.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/tests/test_hello.emf)，看第 2 行：
   ```
   00000000_00000000_80800000_05_0010 //WRITE
   ```
   这是 u4-l2 / u5-l1 讲过的 `.emf` 格式：`srcaddr_datahi_datalo_dstaddr_ctrlmode_access`。这里 `dstaddr=0x80800000`，控制字节 `0x05` = 二进制 `00000101`，即 `write=1`、`datamode=10`（32 位）。
2. 对照字节表（[README:46-68](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L46-L68)），手算这个写事务在线上的字节（`X` 表示不关心/保留位）：
   - `B00` = `R0000A00`，写事务 → `R=0` → `B00 = 0x00`。
   - `B01` = `{ctrlmode[3:0], dstaddr[31:28]}` = `{0000, 1000}` = `0x08`。
   - `B02` = `dstaddr[27:20]` = `0x80`。
   - `B03` = `dstaddr[19:12]` = `0x00`。
   - `B04` = `dstaddr[11:4]` = `0x00`。
   - `B05` = `{dstaddr[3:0], datamode[1:0], write, access}` = `{0000, 10, 1, 1}` = `0x0B`。
   - `B06–B09` = `data = 0x00000000` → `B06=00, B07=00, B08=00, B09=00`。

**需要观察的现象 / 预期结果**：你应得到一串 `00 08 80 00 00 0B 00 00 00 00` 共 10 个字节（`B00–B09`）。把它们与 104 位包字段对照：地址 `0x80800000` 落在 `B01–B05`，写位落在 `B05`，数据 `0` 落在 `B06–B09`，逻辑自洽。本实践为手工演算，**待本地验证**指可在仿真中用 `elink_monitor`（见综合实践）抓到线上字节比对。

#### 4.2.5 小练习与答案

**练习 1**：一次 32 位写事务占多少个 LCLK 周期？为什么？

**参考答案**：10 个字节 ÷ 2 字节/周期（DDR）= 5 个 LCLK 周期（`B00–B09`）。

**练习 2**：如果接收方在某个事务传到 `B05` 时拉高了 WAIT，发送方应如何反应？

**参考答案**：当前事务必须**完整传到 `B09`（或 `B13`）不打断**；WAIT 影响的是**后续**事务——发送方在同步采样到 WAIT 高后，停止发起下一个新事务（不发下一个 `B00`），直到 WAIT 撤销。

**练习 3**：读请求的 `srcaddr` 在物理层走哪些字节？

**参考答案**：`B10–B13`（见 [README:60-61](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L60-L61)）。写事务时这 4 个字节装 `data[63:32]`，读请求时装回信地址 `srcaddr`。

---

### 4.3 TX/RX 划分与系统侧六大 emesh 通道

#### 4.3.1 概念说明

物理线之外，elink 在**系统侧**（即本地 FPGA/ASIC 内部，通常接 AXI 总线）用 104 位 emesh 包与外部对话。一个 elink 实例同时扮演发送器和接收器，且事务有三种类型：**写（write）、读请求（read request）、读响应（read response）**。两两组合，就得到 **6 个通道**：

- 从 **RX（接收器，本端从链路收到的）** 来的：`rxwr`（收到的写）、`rxrd`（收到的读请求）、`rxrr`（收到的读响应）。
- 往 **TX（发送器，本端要发到链路上的）** 去的：`txwr`（待发的写）、`txrd`（待发的读请求）、`txrr`（待发的读响应）。

命名规律：前缀 `rx`/`tx` 表示方向，后缀 `wr`/`rd`/`rr` 表示事务类型。每个通道都是一组 `access + packet[104] + wait` 三线（与 u5-l1 的 emesh 握手一致）。

为什么要分六路而不是一路？因为写、读请求、读响应在 elink 内部走**不同的 FIFO**（`rxwr_fifo/rxrd_fifo/rxrr_fifo` 与 `txwr_fifo/txrd_fifo/txrr_fifo`，见 README 的设计结构树 [elink/README.md:144-171](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/README.md#L144-L171)），互不阻塞。例如一次读操作：本端发出 `txrd`（读请求）→ 远端收到后回 `txrr`（读响应）→ 本端在 `rxrr` 上收到响应。如果把它们混在一路 FIFO 里，一个未完成的读可能卡住后面的写。

#### 4.3.2 核心流程

以「本端 CPU 通过 elink 写远端内存」和「读远端内存」为例，追踪六通道：

```
【写远端】  本端系统 --txwr--> [本elink TX] ==LVDS==> [远elink RX] --rxwr--> 远端内存
【读远端】  本端系统 --txrd--> [本elink TX] ==LVDS==> [远elink RX] --rxrd--> 远端内存
            远端内存 --txrr--> [远elink TX] ==LVDS==> [本elink RX] --rxrr--> 本端系统
```

即：**本端的 `tx*` 是远端 `rx*` 的源头**；读响应要走「远端 txrr → 本端 rxrr」这条回程路。

每个通道的方向（对 elink 而言是输入还是输出）很关键：

| 通道 | 对 elink 的方向 | elink 在系统侧扮演的角色 | 含义 |
|------|-----------------|--------------------------|------|
| `rxwr` | 输出 access/packet，输入 wait | master（主动写本地系统） | 从链路收到的写，注入本地系统 |
| `rxrd` | 输出 access/packet，输入 wait | master（主动向本地系统发读请求） | 从链路收到的读请求 |
| `rxrr` | 输出 access/packet，输入 wait | （来自链路的）读响应 | 从链路收到的读响应 |
| `txwr` | 输入 access/packet，输出 wait | slave（被本地系统写） | 本地系统要发往链路的写 |
| `txrd` | 输入 access/packet，输出 wait | slave（被本地系统请求读） | 本地系统要发往链路的读请求 |
| `txrr` | 输入 access/packet，输出 wait | （回程）读响应 | 发往链路的读响应 |

> 注：表中 master/slave 是相对「本地 AXI/emesh 系统」而言的角色；README 的设计结构里 `emaxi`（AXI 主）/`esaxi`（AXI 从）即对应这些通道（见 u8-l1、u8-l2）。

#### 4.3.3 源码精读

`elink.v` 把这六个通道整齐地列在端口注释 `SYSTEM SIDE INTERFACE` 段下。先看 RX 来的三路（elink 输出 access/packet，输入 wait）：

```verilog
//Master Write (from RX)
output      rxwr_access;  output [PW-1:0] rxwr_packet;  input rxwr_wait;
//Master Read Request (from RX)
output      rxrd_access;  output [PW-1:0] rxrd_packet;  input rxrd_wait;
//Slave Read Response (from RX)
output      rxrr_access;  output [PW-1:0] rxrr_packet;  input rxrr_wait;
```
> —— [elink/hdl/elink.v:63-76](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L63-L76)：三路的 `packet` 位宽都是 `PW-1:0` = 104 位，正是 emesh 包。

再看 TX 去的三路（elink 输入 access/packet，输出 wait）：

```verilog
//Slave Write (to TX)
input  txwr_access;  input [PW-1:0] txwr_packet;  output txwr_wait;
//Slave Read Request (to TX)
input  txrd_access;  input [PW-1:0] txrd_packet;  output txrd_wait;
//Master Read Response (to TX)
input  txrr_access;  input [PW-1:0] txrr_packet;  output txrr_wait;
```
> —— [elink/hdl/elink.v:78-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L78-L91)：注意 `wait` 的方向与 access/packet 相反——握手是双向的。

在实现上，这六路分别由 `erx` 与 `etx` 两个子模块承接。`erx`（接收器）实例化连接了 `rxwr/rxrd/rxrr` 三路输出（[elink/hdl/elink.v:158-163](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L158-L163)），`etx`（发送器）实例化连接了 `txwr/txrd/txrr` 三路的 `wait` 输出（[elink/hdl/elink.v:211-213](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L211-L213)）：

```verilog
erx #(.ID(ID), .ETYPE(ETYPE), .TARGET(TARGET))
erx(.rx_active(elink_active),
    ... // rxi_* 物理输入 → rxwr/rxrd/rxrr 系统侧输出
    .rxwr_access(rxwr_access), .rxwr_packet(rxwr_packet[PW-1:0]), ...);

etx #(.ID(ID), .ETYPE(ETYPE), .TARGET(TARGET))
etx(.tx_active(tx_active),
    ... // txwr/txrd/txrr 系统侧输入 → txo_* 物理输出
    .txrd_wait(txrd_wait), .txwr_wait(txwr_wait), .txrr_wait(txrr_wait), ...);
```
> —— erx 见 [elink/hdl/elink.v:150-183](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L150-L183)；etx 见 [elink/hdl/elink.v:197-232](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L197-L232)。

最后，`elink.v` 里还有一个值得注意的小细节：TX 与 RX 两个子模块虽然各自独立，但需要一个**配置寄存器通路**让 TX 侧的配置写入能被 RX 侧读到。由于 TX 与 RX 跑在不同的分频时钟（`tx_lclk_div4` 与 `rx_lclk_div4`）下，这条通路必须跨时钟域，于是用一个 `oh_fifo_cdc`（u3-l2 讲过的 CDC FIFO）实现：

```verilog
oh_fifo_cdc #(.DW(104), .DEPTH(32), .TARGET(TARGET))
ecfg_cdc (.nreset(erx_nreset),
          .wait_out(etx_cfg_wait), .access_out(erx_cfg_access),
          .packet_out(erx_cfg_packet[PW-1:0]),
          .clk_in(tx_lclk_div4),  .access_in(etx_cfg_access),
          .packet_in(etx_cfg_packet[PW-1:0]),
          .clk_out(rx_lclk_div4), .wait_in(erx_cfg_wait));
```
> —— [elink/hdl/elink.v:237-249](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L237-L249)：`clk_in` 是 TX 域分频时钟，`clk_out` 是 RX 域分频时钟，包宽 104 位、深度 32。这是 u3-l2 的 `oh_fifo_cdc` 在系统中的真实用例。

#### 4.3.4 代码实践

**实践目标**：本讲指定的核心实践——把 `elink.v` 的全部顶层端口分成三类（RX 物理侧、TX 物理侧、系统侧），并把系统侧六个通道标出方向。

**操作步骤**：

1. 打开 [elink/hdl/elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v)，通读端口声明（第 1–91 行）。
2. 建立三栏分类表，把每个端口填入：

| 分类 | 信号 | 方向 | 一句话作用 |
|------|------|------|------------|
| **系统侧（时钟/复位/状态）** | sys_nreset | input | 系统侧低有效复位 |
| | sys_clk | input | 系统侧单一时钟（FIFO 用） |
| | elink_active | output | TX 与 RX 均活跃 |
| | mailbox_irq | output | 邮箱中断（见 u6-l4） |
| | chipid[11:0]/cclk_p/n/chip_nreset | output | Epiphany 芯片附带 IO |
| **RX 物理侧（接收）** | rxi_lclk_p/n | input | 源同步时钟输入 |
| | rxi_frame_p/n | input | 帧输入 |
| | rxi_data_p/n[7:0] | input | DDR 数据输入 |
| | rxo_wr_wait_p/n, rxo_rd_wait_p/n | output | 读/写反压输出 |
| **TX 物理侧（发送）** | txo_lclk_p/n | output | 源同步时钟输出 |
| | txo_frame_p/n | output | 帧输出 |
| | txo_data_p/n[7:0] | output | DDR 数据输出 |
| | txi_wr_wait_p/n, txi_rd_wait_p/n | input | 读/写反压输入 |
| **系统侧六通道** | rxwr/rxrd/rxrr 各 access/packet/wait | 出/出/入 | RX 三类事务 |
| | txwr/txrd/txrr 各 access/packet/wait | 入/入/出 | TX 三类事务 |

3. 核对「系统侧六通道」共 18 个信号（6×3），数一下端口列表里是否恰好 18 个。

**需要观察的现象 / 预期结果**：你会清楚地看到 `elink.v` 的端口自然分成三块——注释里用 `ELINK RECEIVER` / `ELINK TRANSMITTER` / `SYSTEM SIDE INTERFACE` 三个分隔块（[L29-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L29-L91)），与上表一一对应。本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`rxwr_wait` 和 `txwr_wait` 的方向分别是什么？为什么相反？

**参考答案**：`rxwr_wait` 是 elink 的**输入**（本地系统告诉 elink「我暂时不能接收这个写」）；`txwr_wait` 是 elink 的**输出**（elink 告诉本地系统「我暂时不能接收这个待发的写」）。两者方向相反是因为前者站在「elink 向本地系统输出事务」的 RX 通道上，后者站在「本地系统向 elink 输入事务」的 TX 通道上——握手信号的 `wait` 总是由**接收方**驱动。

**练习 2**：为什么读响应用专门的 `rxrr`/`txrr` 通道，而不是复用 `rxwr`/`txwr`？

**参考答案**：因为读响应在时序上与原始读请求异步——它要等远端真正读出数据后才返回，期间链路可能还在传别的写。若与写混用同一 FIFO，一个未完成的读响应会阻塞写或被写阻塞。独立通道 + 独立 FIFO 让三类事务互不阻塞，这正是 elink 用六通道而非两通道的根本原因。

**练习 3**：`ecfg_cdc` 为什么用 `oh_fifo_cdc` 而不是普通寄存器？

**参考答案**：因为它要跨越 `tx_lclk_div4`（TX 域）与 `rx_lclk_div4`（RX 域）两个异步时钟域（[elink.v:244-247](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L244-L247)）。普通寄存器跨域会有亚稳态风险（u2-l4），`oh_fifo_cdc` 内部用格雷码指针 + 同步器安全地把 104 位包从 TX 域搬到 RX 域（u3-l2）。

---

## 5. 综合实践

**任务**：在仿真平台里追踪一个写事务的**完整旅程**——从系统侧 `txwr` 进入，经 elink 的 TX 物理管脚串行化上线，再被另一个 elink 的 RX 物理管脚接收、还原成 104 位包，最终出现在系统侧 `rxwr` 上。这个综合实践把本讲三个最小模块（LVDS IO、帧格式、六通道）串起来。

**操作步骤**：

1. 阅读 [elink/dv/dut_elink.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v)。这是仿真用的 dut 包装，里面实例化了**两个 elink**（`elink0` 与 `elink1`），并把它们**回环连接**：`elink0` 的 TX 输出接到 `elink1` 的 RX 输入。
2. 找到回环接线的关键行（[elink/dv/dut_elink.v:225-234](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L225-L234)）：
   ```verilog
   .rxi_lclk_p (elink1_txo_lclk_p),  // elink0 的 RX 时钟 ← elink1 的 TX 时钟
   .rxi_data_p (elink1_txo_data_p[7:0]),
   .txi_wr_wait_p (elink1_rxo_wr_wait_p),  // elink0 收到的反压 ← elink1 发出的反压
   ```
   即 `elink1` 的 `txo_*`（发送）正是 `elink0` 的 `rxi_*`（接收）。另一方向同理（[elink/dv/dut_elink.v:308-317](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L308-L317)）。
3. 看 `elink0` 的系统侧：它通过一个 `emesh_if`（[dut_elink.v:143-169](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L143-L169)）把外部 `access_in/packet_in` 路由成 `txwr`/`txrd` 通道进入 `elink0`（[dut_elink.v:235-238](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L235-L238)）。
4. 看 `elink1` 的系统侧：它的 `rxwr`/`rxrd` 输出被一个 `ememory`（仿真存储器）接收（[dut_elink.v:325-353](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L325-L353)）。
5. 想象（或运行）一次写事务：激励 `packet_in` → `elink0.txwr` → `elink0.txo_data_p/n`（串行成 `B00..B09`）→ `elink1.rxi_data_p/n` → `elink1.rxwr_packet` → 写入 `ememory`。
6. 注意 dut 里还挂了一个 `elink_monitor`（[dut_elink.v:242-245](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/dv/dut_elink.v#L242-L245)），它盯住 `elink0` 的 `txo_frame_p/txo_lclk_p/txo_data_p`，可以把线上字节抓出来——这正是 4.2.4 手算结果的验证手段。

**需要观察的现象 / 预期结果**：画出一张端到端框图，标出一个写包经过的每一级：`emesh_if → txwr → etx（拆字节）→ txo_data_p/n[7:0] + txo_frame_p + txo_lclk_p →（回环）→ rxi_data_p/n + rxi_frame_p + rxi_lclk_p → erx（组包）→ rxwr → ememory`。如果你能在波形里看到 `elink0_txo_frame_p` 的上升沿对应 `elink1` 收到一个事务，就说明三个模块都通了。完整跑通仿真需要先按 u1-l3 搭好 iverilog 环境；若环境缺依赖（仓库脚本存在历史路径问题），则至少完成源码阅读与框图绘制，标注「待本地验证」。

## 6. 本讲小结

- elink 是 FPGA↔ASIC 之间点对点、全双工的高速串行链路，物理层用 **24 对 LVDS 差分信号**（LCLK + FRAME + DATA[7:0] + 读/写 WAIT，各分 TX/RX 两套）。
- 物理层是**源同步 + DDR**：发送方发 LCLK，接收方用它采 8 位数据；FRAME 上升沿标记事务起点，一个 32 位事务占 10 个字节（`B00–B09`，5 个 LCLK 周期）。
- 系统侧用 **104 位 emesh 包**（与 u5-l1 完全一致）+ `access/wait` 握手；物理侧的字节流 `B00–B13` 是同一个包的串行化表示，二者由 `etx_protocol`/`erx_protocol` 互转。
- 反压 **WAIT 分读/写两路**，且因相位未指定**必须用两级同步器采样**（u2-l4 的 CDC 同步器）；当前事务传到一半遇到 WAIT 不打断。
- 系统侧分 **TX/RX × {wr,rd,rr} = 六个通道**，每个通道 `access+packet+wait` 三线；三类事务走各自 FIFO 互不阻塞。
- TX 与 RX 跑在不同分频时钟下，二者间的配置通路用 `oh_fifo_cdc`（u3-l2）跨时钟域。

## 7. 下一步学习建议

本讲只看了 elink 的「外壳」。后续建议：

- **u7-l2 发送通路 etx 流水线**：拆开 `etx.v` → `etx_arbiter` → `etx_protocol` → `etx_fifo` → `etx_io` → `etx_clocks`，看清 104 位包如何一步步变成 `txo_data_p/n` 上的 DDR 比特，以及 LCLK 如何由 `etx_clocks` 对齐到数据眼中央。
- **u7-l3 接收通路 erx 流水线**：对称地看 `erx_io`（IDDR 解串）→ `erx_clocks`（恢复时钟）→ `erx_protocol`（字节流还原成包），理解 CDR 与 90° 移相采样的实现。
- **u7-l4 elink 配置子系统**：精读 `elink_regmap.vh`、`elink_constants.vh`、`elink_cfg`/`ecfg_if`，学会通过写寄存器改变链路模式、时钟、复位。
- 若想看 elink 如何接到 AXI 总线，可预习 **u8-l1（emaxi 主桥）** 与 **u8-l2（esaxi 从桥 + axi_elink 集成）**，那里会用到本讲的六通道概念。
