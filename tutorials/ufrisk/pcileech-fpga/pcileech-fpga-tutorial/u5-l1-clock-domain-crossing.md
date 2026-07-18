# 跨时钟域设计与双时钟 FIFO

## 1. 本讲目标

本讲聚焦 pcileech-fpga 工程里一条贯穿所有模块、却很少被显式讲清的主线：**时钟域**。

学完后你应当能够：

- 识别 PCIeSquirrel 工程里的三大时钟域（`clk`/`clk_sys`、`clk_com`/`ft601_clk`、`clk_pcie`），并说出每个时钟的物理来源。
- 说清「为什么两个同为 100MHz 的时钟也不能直接互连」——即跨时钟域（CDC, Clock Domain Crossing）与亚稳态（metastability）问题。
- 看懂工程里 `fifo_*_clk2` 系列双时钟 FIFO 的命名规律，并能根据 `wr_clk`/`rd_clk` 判断它跨越了哪两个时钟域。
- 理解 `pcileech_com` 中「`2*clk_com < clk`」这条设计前提的真正含义。

本讲是第 5 单元（时序、约束与 Xilinx IP）的第一篇，前置知识来自 u2-l2（FT601 通信核心）与 u3-l2（PCIe 配置空间管理），后续 u5-l2（约束文件）会用到本讲建立的时钟域概念。

## 2. 前置知识

在进入源码前，先用最通俗的方式把几个概念讲透。

### 2.1 什么是时钟域

FPGA 内部的每个触发器（flip-flop，简称 FF）都由某个时钟信号的上升沿「打拍」采样。**由同一个时钟驱动的所有触发器，构成一个时钟域（clock domain）**。同一个域内，设计师可以放心地用「几拍延迟」来推算时序，因为所有触发器步调一致。

但当数据要从一个时钟域传到另一个时钟域（两个时钟来自不同的振荡器，或频率/相位不同）时，「步调一致」的前提就崩了——这就是**跨时钟域（CDC）问题**。

### 2.2 亚稳态：为什么不能直接连

假设 A 域用一个 100MHz 时钟，B 域也用一个 100MHz 时钟，但两者来自**不同的晶振**。它们标称频率相同，但相位会缓慢漂移。

如果 A 域的某个输出信号直接接到 B 域的触发器输入端，当这个信号恰好在 B 域采样沿附近发生变化时，B 域触发器的输出会**卡在一个非 0 非 1 的中间电压**，过一段不可预测的时间才随机塌缩到 0 或 1。这个现象叫**亚稳态（metastability）**。

亚稳态的危害：后续不同扇出路径可能把这个「半稳定值」分别读成 0 和 1，导致整片电路行为错乱。它是无法彻底消除的，工程上只能靠正确的 CDC 结构把它压到「极小概率」。

### 2.3 解决方案：双时钟 FIFO

对于**多位数据 + 有反压（流量控制）需求**的跨域通路，标准解法是**双时钟 FIFO（dual-clock FIFO / asynchronous FIFO）**。它的关键设计：

- FIFO 内部有独立的两套读写指针，分别由 `wr_clk` 和 `rd_clk` 驱动。
- 指针用**格雷码（Gray code）**编码——相邻两个数只有 1 位不同。
- 每个时钟域读取对方的指针时，先经过一个「两级触发器同步链（2-FF synchronizer）」。
- 因为格雷码一次只变 1 位，即使采样到「正在变化的那一瞬间」，最坏也只错 1 位，不会出现「地址整体跳一大截」的灾难。

> 一句话直觉：双时钟 FIFO 就像一个「旋转门传递窗」——A 域往里塞东西，B 域往外取东西，两边各转各的，互不踩脚，传递窗本身保证东西不丢、不串。

Xilinx 的 FIFO Generator IP 把上述全部细节封装好，用户只要填 `wr_clk`、`rd_clk`、`din`、`wr_en`、`rd_en`、`dout` 即可。pcileech-fpga 里所有的 `fifo_*_clk2` 就是这类 IP 的实例。

### 2.4 单时钟 FIFO vs 双时钟 FIFO

Xilinx FIFO Generator 有两种时钟模式，pcileech-fpga 用命名把它们区分开：

| 命名片段 | 含义 | 端口形式 |
| --- | --- | --- |
| `_clk1` | 单时钟（common clock） | 只有一个 `.clk`，读写同频同相 |
| `_clk2` | 双时钟（independent clock） | 分 `.wr_clk` 和 `.rd_clk`，可异步 |

看到 `_clk2` 就知道：**这里发生了一次跨时钟域**。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 在本讲的作用 |
| --- | --- |
| `PCIeSquirrel/src/pcileech_squirrel_top.sv` | 顶层，三个时钟信号（`clk`、`ft601_clk`、PCIe 参考时钟）的物理入口，全局复位生成 |
| `PCIeSquirrel/src/pcileech_pcie_a7.sv` | PCIe 封装层，展示 `clk_pcie` 如何由硬核 `user_clk_out` 产生，以及 `IBUFDS_GTE2` 参考时钟缓冲 |
| `PCIeSquirrel/src/pcileech_com.sv` | 通信核心，含「`2*clk_com < clk`」前提与 `fifo_64_64_clk2_comrx` 等跨域 FIFO |
| `PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv` | 配置空间管理，两个跨 `clk_sys ↔ clk_pcie` 的 FIFO 样本 |
| `PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv` | TLP 通路，`fifo_134_134_clk2` 等多例跨域 FIFO（补充引用） |
| `PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv` | 配置空间影子，`fifo_49_49_clk2` 样本（补充引用） |

永久链接统一使用当前 HEAD `c538c41`。

## 4. 核心概念与源码讲解

### 4.1 顶层时钟与复位：三大时钟域的来源

#### 4.1.1 概念说明

整块板卡上有**三个互相独立的时钟源**，它们之间没有任何固定的相位关系（异步）。数据必须在它们之间来回搬运，于是 CDC 问题无处不在。

| 时钟域 | 顶层信号名 | 物理来源 | 典型用途 | 频率量级 |
| --- | --- | --- | --- | --- |
| 系统域 | `clk`（又称 `clk_sys`） | 板载系统晶振 SYSCLK | `fifo`、`com` 的系统侧、DRP | 100 MHz |
| 通信域 | `clk_com`（= `ft601_clk`） | FT601 芯片输出的时钟 | FT601 状态机、32↔64 拼装 | ~100 MHz |
| PCIe 域 | `clk_pcie` | PCIe 硬核 `user_clk_out` | PCIe 硬核、TLP/cfg 子模块 | 62.5 / 125 / 250 MHz |

注意几个易混点：

- **`clk` 与 `clk_sys` 是同一个时钟**，只是不同模块用了不同名字。顶层 `pcileech_squirrel_top` 把 `clk` 传给 `pcie_a7` 时改名 `.clk_sys(clk)`，传给 `com` 时改名 `.clk(clk)`。
- **`clk_com` 与 `ft601_clk` 也是同一个**。顶层把 `ft601_clk` 传给 `com` 时改名 `.clk_com(ft601_clk)`。
- **`clk_pcie` 不是外部引脚**，而是 PCIe 硬核 `pcie_7x_0` 运行起来后，从 100MHz 差分参考时钟派生出的「用户时钟」`user_clk_out`。在链路训练前它可能还没稳，所以 PCIe 域的复位要特别小心（见 u3-l1）。
- PCIe 还有一个 100MHz 差分**参考时钟** `pcie_clk_p/n`，经 `IBUFDS_GTE2` 转成单端 `pcie_clk_c`，专门喂给硬核的 `.sys_clk`——它和 `clk_pcie` 不是一回事，前者是硬核的输入，后者是硬核的输出。

#### 4.1.2 核心流程

顶层时钟与复位的总体流转：

```text
板载晶振 SYSCLK ───────────────► clk (clk_sys, 100MHz)
                                   │
                                   ├─► pcileech_fifo   (系统域主控)
                                   ├─► pcileech_com.clk
                                   └─► pcileech_pcie_a7.clk_sys (DRP 用)

FT601 芯片 ──► ft601_clk (clk_com) ─► pcileech_com.clk_com  (通信域)

PCIe 金手指 100MHz 差分 refclk
        │
        ▼ IBUFDS_GTE2 ─► pcie_clk_c ─► pcie_7x_0.sys_clk
                                              │
                                              ▼  pcie_7x_0.user_clk_out = clk_pcie (PCIe 域)
                                                  ├─► pcileech_pcie_cfg_a7
                                                  └─► pcileech_pcie_tlp_a7
```

**全局复位 `rst`** 由系统域的 64 位自由计数器 `tickcount64` 生成：

```text
上电：tickcount64 从 0 数起，前 64 拍 rst=1（给所有触发器一个确定初值）
长按 SW2：user_sw2_n=0，强制 rst=1，且 tickcount64 清零
正常：tickcount64 >= 64 且 SW2 松开，rst=0
```

`rst` 属于**系统域**。它被分发到 `com`、`fifo`、`pcie_a7`；而 `pcie_a7` 又把它与 PCIe 域的复位源组合，分出软复位 `rst_subsys` 与硬复位 `rst_pcie` 两条线（详见 u3-l1）。这条「复位也跨域」的细节，会在 4.3 节的 FIFO 复位端口里再次体现。

#### 4.1.3 源码精读

顶层时钟与复位入口：

三个时钟/复位相关端口在顶层声明，`clk` 与 `ft601_clk` 是两个独立的输入引脚（[pcileech_squirrel_top.sv:19-21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L19-L21)）：

```systemverilog
input           clk,
input           ft601_clk,
```

全局复位由系统域计数器 `tickcount64` 驱动（[pcileech_squirrel_top.sv:79-87](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L79-L87)）：

```systemverilog
time tickcount64 = 0;
always @ ( posedge clk )
    tickcount64 <= user_sw2_n ? (tickcount64 + 1) : 0;

assign rst = ~user_sw2_n || ((tickcount64 < 64) ? 1'b1 : 1'b0);
```

`clk` 在传给三大子系统时改名、各司其职。给 `com` 同时提供系统时钟与通信时钟（[pcileech_squirrel_top.sv:100-101](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L100-L101)）：

```systemverilog
.clk      ( clk        ),
.clk_com  ( ft601_clk  ),
```

给 `pcie_a7` 时改名为 `clk_sys`，并在硬件上明确标注「100MHz SYSTEM CLK」（[pcileech_squirrel_top.sv:147](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L147)）。这就是「`clk` 与 `clk_sys` 同源」的代码证据。

PCIe 域时钟的产生在 `pcie_a7` 里。差分参考时钟先经 `IBUFDS_GTE2` 原语转单端（[pcileech_pcie_a7.sv:58](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L58)）：

```systemverilog
IBUFDS_GTE2 refclk_ibuf (.O(pcie_clk_c), .ODIV2(), .I(pcie_clk_p), .CEB(1'b0), .IB(pcie_clk_n));
```

随后 `pcie_clk_c` 喂给硬核 `.sys_clk`，硬核再「吐出」用户时钟作为整个 PCIe 域的根时钟（[pcileech_pcie_a7.sv:256](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L256)）：

```systemverilog
.user_clk_out  ( clk_pcie ),   // ->
```

复位分层则在此处定义（[pcileech_pcie_a7.sv:54-55](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L54-L55)），`rst`（系统域）与 `pcie_perst_n`（PCIe 物理引脚）、`user_reset_out`（PCIe 域）三者汇合：

```systemverilog
wire rst_subsys = rst || rst_pcie_user || dfifo_pcie.pcie_rst_subsys;
wire rst_pcie   = rst || ~pcie_perst_n || dfifo_pcie.pcie_rst_core;
```

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「`clk` 与 `clk_sys` 是同一根线、`clk_com` 与 `ft601_clk` 是同一根线」。

**操作步骤**：

1. 打开 `PCIeSquirrel/src/pcileech_squirrel_top.sv`，定位 `i_pcileech_com` 例化，看到 `.clk(clk)`、`.clk_com(ft601_clk)`。
2. 定位 `i_pcileech_pcie_a7` 例化，看到 `.clk_sys(clk)`。
3. 打开 `PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv`，看它的输入端口声明（[L15-16](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_cfg_a7.sv#L15-L16)）同时有 `clk_sys` 和 `clk_pcie`，说明它横跨两个时钟域。

**需要观察的现象**：同一个物理时钟在不同模块里用了三个不同的名字（`clk` / `clk_sys` / `clk_com` 来自两根线）。

**预期结果**：你能画出一张「物理时钟 → 各模块端口名」的对照表，确认本节 4.1.1 的表格成立。本实践为纯源码阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`clk_pcie` 是顶层 `pcileech_squirrel_top` 的输入引脚吗？

> **答案**：不是。顶层输入的是 100MHz 差分参考时钟 `pcie_clk_p/n`；`clk_pcie` 是 `pcie_a7` 内部由硬核 `pcie_7x_0.user_clk_out` 产生的内部信号，不上顶层端口表。

**练习 2**：为什么 `pcie_drp_clk` 用的是 `clk_sys` 而不是 `clk_pcie`？

> **答案**：见 [pcileech_pcie_a7.sv:246-247](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L246-L247)，注释明说「write should only happen when core is in reset state」。DRP（动态重配置）只在 PCIe 核复位、链路未训练时操作，此时 `clk_pcie` 可能尚未稳定，所以借用始终稳定的 100MHz 系统域 `clk_sys` 作为 DRP 时钟。

---

### 4.2 跨时钟域问题与双时钟 FIFO 原理

#### 4.2.1 概念说明

确认了三大时钟域之后，问题就来了：数据要在它们之间流动。例如：

- 主机经 FT601（`clk_com` 域）收到的命令，要送到系统域 `fifo`（`clk` 域）去路由。
- `fifo` 处理后的 TLP（`clk_sys` 域），要送到 PCIe 硬核（`clk_pcie` 域）去发送。
- PCIe 硬核收到的 TLP（`clk_pcie` 域），要回送给主机（最终到 `clk_com` 域）。

每一处「域 → 域」的边界，都必须插一个双时钟 FIFO，否则就会触发 2.2 节讲的亚稳态。

pcileech-fpga 的做法非常一致：**凡是要跨域的多位数据通路，一律用 Xilinx FIFO Generator 生成的 `_clk2` 系列异步 FIFO**。它把「格雷码指针 + 两级同步链 + 异步 RAM」的复杂性全部封装进 IP，源码里只剩干净的端口连接。

#### 4.2.2 核心流程

一次典型的「跨域 + 自适应速率」FIFO 收发流程：

```text
[A 域] 生产者                           [B 域] 消费者
   │ wr_clk                                │ rd_clk
   ▼                                       ▼
 wr_en=1, din=数据 ──► 异步 FIFO ──► rd_en=1 ──► dout=数据
   │                                       │
   └─ 写指针(wr_clk域, 格雷码)              └─ 读指针(rd_clk域, 格雷码)
              │  经2-FF同步到 rd_clk 域                │  经2-FF同步到 wr_clk 域
              ▼                                       ▼
        计算 full 标志                          计算 empty 标志
```

要点：

1. **写侧**只看「自己写到了哪」和「经同步后的对方读指针」，据此判断 `full`，满了就停止 `wr_en`（反压）。
2. **读侧**只看「自己读到了哪」和「经同步后的对方写指针」，据此判断 `empty` 和 `valid`。
3. 因为两边指针都走格雷码 + 两级同步，即便采样瞬间正在变化，最坏也只差 1，不会撕裂成无效值。

**关于 FIFO 深度与速率匹配**：双时钟 FIFO 本身能容忍读写速率不同（这正是它的价值），但深度有限。若长期写快读慢，FIFO 终究会溢出。所以设计师仍需保证**平均读速率 ≥ 平均写速率**，FIFO 只用来吸收突发（burst）。pcileech-fpga 里很多 FIFO 用 `rd_en = 1'b1`（只要非空就读），把读侧开到最大，正是这个用意。

#### 4.2.3 源码精读

`pcileech_com` 的 RX 通路是讲解双时钟 FIFO 的最佳样本，因为它同时展示了「位宽转换 + 跨域 + 速率前提」三件事。

源码注释直接点明了跨域动机与前提（[pcileech_com.sv:92-97](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L92-L97)）：

```systemverilog
// 1: convert 32-bit signal into 64-bit signal using logic.
// 2: change clock domain from clk_com to clk using a very shallow fifo.
//    due to previous 32->64 conversion this will be fine if: 2*clk_com < clk.
```

这段注释揭示了三件事：

1. 第一步在 `clk_com` 域里把 32 位流拼成 64 位（[L107-119](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L107-L119)）。
2. 第二步用一个**很浅**的双时钟 FIFO 把 64 位数据从 `clk_com` 跨到 `clk`。
3. 由于 32→64 拼装，写入 FIFO 的速率被砍半，于是「读时钟足够快」就有了前提：`2*clk_com < clk`。

**如何理解 `2*clk_com < clk`？** 这里 `clk_com` 与 `clk` 指频率。写侧每凑齐两个 32 位（至少消耗 2 个 `clk_com` 周期）才产生一个 64 位 FIFO 写入，所以**最大持续写入率**是：

\[
f_{\text{write}} \;\le\; \frac{f_{\text{clk\_com}}}{2} \quad(\text{字/秒})
\]

读侧 `rd_en = 1'b1`，每个 `clk` 周期读一个字，**读出率**为：

\[
f_{\text{read}} \;=\; f_{\text{clk}} \quad(\text{字/秒})
\]

不溢出的条件是 \(f_{\text{read}} \ge f_{\text{write}}\)，即：

\[
f_{\text{clk}} \;\ge\; \frac{f_{\text{clk\_com}}}{2}
\]

作者写出的 `2*clk_com < clk`（即 \(f_{\text{clk}} > 2 f_{\text{clk\_com}}\)）是**一个更保守的充分条件**，留了很大裕量。注意：在 PCIeSquirrel 上 `clk` 与 `clk_com` 标称都约 100MHz，来自不同振荡器（异步），USB 流量又是突发的、不会持续占满每个 `clk_com` 周期，所以这个「很浅」的 FIFO 实际工作正常。**关键结论是：这个 FIFO 的存在本身不是为了凑速率，而是为了消除两个异步时钟域之间的亚稳态。**

跨域 FIFO 的例化（[pcileech_com.sv:121-132](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L121-L132)）：

```systemverilog
fifo_64_64_clk2_comrx i_fifo_64_64_clk2_comrx(
    .rst        ( rst | (tickcount64_com<2) ),
    .wr_clk     ( clk_com ),   // 写：通信域
    .rd_clk     ( clk          ),   // 读：系统域
    .din        ( com_rx_data64 ),
    .wr_en      ( com_rx_valid64 ),
    .rd_en      ( 1'b1 ),       // 只要非空就读，读侧拉满
    .dout       ( com_rx_dout ),
    .valid      ( com_rx_valid )
);
```

读这段例化要抓三点：

- `.wr_clk(clk_com)` 与 `.rd_clk(clk)` 是两个不同时钟——这是 `_clk2`（双时钟）的标志。
- `.rd_en(1'b1)` 把读侧开到最大，依赖读时钟够快来排空。
- `.rst` 不是简单的系统域 `rst`，而是 `rst | (tickcount64_com<2)`——把系统域复位与通信域计数器（`tickcount64_com` 在 `clk_com` 下计数）合起来，确保 FIFO 在两侧时钟都稳定后才放开。这是**复位也要考虑跨域**的细节。

> 对比看同一文件里的 `fifo_32_32_clk1_comtx`（[pcileech_com.sv:157-170](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L157-L170)）：它的命名是 `_clk1`，端口只有一个 `.clk(clk_com)` 而没有 `wr_clk/rd_clk`——这是**单时钟** FIFO，读写都在 `clk_com` 域，不跨域。命名规律在此一目了然。

#### 4.2.4 代码实践

**实践目标**：体会「单时钟 FIFO 不跨域、双时钟 FIFO 跨域」的命名与端口差异。

**操作步骤**：

1. 打开 `pcileech_com.sv`，并排查看 `fifo_64_64_clk2_comrx`（L121-132）与 `fifo_32_32_clk1_comtx`（L157-170）。
2. 各自记录：名字里的 `_clk1` 还是 `_clk2`？端口用的是 `.clk` 还是 `.wr_clk/.rd_clk`？跨了几个时钟？

**需要观察的现象**：

- `_clk2_comrx`：双时钟，`wr_clk=clk_com`、`rd_clk=clk`，跨越通信域→系统域。
- `_clk1_comtx`：单时钟，只有 `.clk(clk_com)`，不跨域。

**预期结果**：你能用一句话总结「`_clk2` 等于跨域，`_clk1` 等于不跨域」。纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者敢把 `fifo_64_64_clk2_comrx` 的 `rd_en` 恒接 `1'b1`？

> **答案**：因为读侧 `clk`（100MHz）足够快，且写侧因 32→64 拼装写入率被砍半、USB 流量又是突发的，FIFO 不会持续堆积。`rd_en=1` 让读侧「来一个取一个」，把延迟压到最低，同时依赖双时钟 FIFO 内部的 `empty` 标志自动避免空读。

**练习 2**：如果直接把 `clk_com` 域的 `com_rx_data64` 用导线接到 `clk` 域的触发器，会发生什么？

> **答案**：两时钟来自不同振荡器、相位漂移，会在采样沿附近触发亚稳态，导致 `fifo` 偶发读到错乱的 64 位值，表现为 DMA 数据偶发性损坏，且极难复现。双时钟 FIFO 的格雷码指针 + 两级同步链正是为消除这种风险而存在。

---

### 4.3 双时钟 FIFO 例化地图：工程中的关键实例

#### 4.3.1 概念说明

三大时钟域两两之间都有数据往来，于是工程里散布着一批 `_clk2` FIFO。它们的**命名规律**高度规整：

```text
fifo_<写入位宽>_<读出位宽>_<时钟模式>[_用途后缀]
```

- **写入位宽 / 读出位宽**：可以相同（纯跨域），也可以不同（跨域的同时做位宽转换，例如 256→32）。
- **时钟模式**：`clk1` = 单时钟，`clk2` = 双时钟（跨域）。
- **用途后缀**：可读性注释，如 `comrx`（com 接收）、`comtx`（com 发送）、`rxfifo` 等。

掌握这条规律后，看到一个 `fifo_xxx_clk2` 就能立刻判断：它跨了域，且能用 `wr_clk/rd_clk` 两行确认跨越的是哪两个域。

#### 4.3.2 核心流程

把工程里**所有已确认的 `_clk2` 双时钟 FIFO** 集中到一张表，就能看清整个工程的跨域「热力图」：

| FIFO 实例 | 所在文件:行 | wr_clk | rd_clk | 跨越的时钟域 | 携带的数据 |
| --- | --- | --- | --- | --- | --- |
| `fifo_64_64_clk2_comrx` | pcileech_com.sv:121 | `clk_com` | `clk` | 通信域 → 系统域 | 主机→FPGA 命令（64 位） |
| `fifo_256_32_clk2_comtx` | pcileech_com.sv:171 | `clk` | `clk_com` | 系统域 → 通信域 | FPGA→主机回包（256→32 位宽转换） |
| `fifo_64_64`（cfg tx） | pcileech_pcie_cfg_a7.sv:45 | `clk_sys` | `clk_pcie` | 系统域 → PCIe 域 | 主机→配置空间命令 |
| `fifo_32_32_clk2`（cfg rx） | pcileech_pcie_cfg_a7.sv:66 | `clk_pcie` | `clk_sys` | PCIe 域 → 系统域 | 配置空间状态回读 |
| `fifo_134_134_clk2` | pcileech_pcie_tlp_a7.sv:118 | `clk_pcie` | `clk_sys` | PCIe 域 → 系统域 | 128 位 TLP + 边界位 |
| `fifo_134_134_clk2_rxfifo` | pcileech_pcie_tlp_a7.sv:271 | `clk_sys` | `clk_pcie` | 系统域 → PCIe 域 | 主机注入的 TLP |
| `fifo_1_1_clk2` | pcileech_pcie_tlp_a7.sv:252 | `clk_sys` | `clk_pcie` | 系统域 → PCIe 域 | 1 位「整包到达」事件 |
| `fifo_49_49_clk2` | pcileech_tlps128_cfgspace_shadow.sv:47 | `clk_sys` | `clk_pcie` | 系统域 → PCIe 域 | 主机改配置空间影子命令 |
| `fifo_43_43_clk2` | pcileech_tlps128_cfgspace_shadow.sv:129 | `clk_pcie` | `clk_sys` | PCIe 域 → 系统域 | 配置空间影子读回 |

观察规律：

- **通信域 ↔ 系统域**：发生在 `pcileech_com` 内部（`comrx`/`comtx`）。
- **系统域 ↔ PCIe 域**：发生在 PCIe 子系统的边界，每条通路都成对出现——一条过去（sys→pcie）、一条回来（pcie→sys），保证双向都安全。
- **134 位**这个奇怪数字 = 1（first）+ 1（last）+ 4（tkeepdw）+ 128（tdata），是 TLP 桥接的统一打包宽度（详见 u3-l4）。把边界位连同数据一起塞进同一个跨域 FIFO，避免数据与边界位分别跨域后错位。

#### 4.3.3 源码精读

挑三个最有代表性的实例逐行印证。

**实例 1：`fifo_64_64_clk2_comrx`（通信域 → 系统域）**

已在 4.2.3 节精读。关键：`.wr_clk(clk_com)`、`.rd_clk(clk)`（[pcileech_com.sv:123-124](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L123-L124)）。

**实例 2：`fifo_134_134_clk2`（PCIe 域 → 系统域）**

这是 TLP 接收方向的跨域桥，把 128 位 TLP 连同边界位一起打包跨域（[pcileech_pcie_tlp_a7.sv:118-129](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L118-L129)）：

```systemverilog
fifo_134_134_clk2 i_fifo_134_134_clk2 (
    .wr_clk  ( clk_pcie ),                       // 写：PCIe 域
    .rd_clk  ( clk_sys  ),                       // 读：系统域
    .din     ( { tlps_in.tuser[0], tlps_in.tlast,
                 tlps_in.tkeepdw, tlps_in.tdata } ),
    .wr_en   ( tlps_in.tvalid ),
    .rd_en   ( dfifo.rx_rd_en ),
    .dout    ( { first, tlast, tkeepdw, tdata } )
);
```

注意 `.din` 把 4 类信号（首拍、尾拍、字节使能、128 位数据）**拼成一个 134 位字整体跨域**。如果分别跨域，它们之间可能错拍，导致「数据是这一拍的、尾拍标志却是上一拍的」。整体打包是工程上消除此类竞争的惯用法。

**实例 3：`fifo_49_49_clk2`（系统域 → PCIe 域）**

这是配置空间影子接收主机命令的跨域桥（[pcileech_tlps128_cfgspace_shadow.sv:47-58](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_cfgspace_shadow.sv#L47-L58)）：

```systemverilog
fifo_49_49_clk2 i_fifo_49_49_clk2(
    .wr_clk  ( clk_sys  ),    // 写：系统域（来自 IfShadow2Fifo）
    .rd_clk  ( clk_pcie ),    // 读：PCIe 域（喂给 BRAM）
    .wr_en   ( dshadow2fifo.rx_rden || dshadow2fifo.rx_wren ),
    .din     ( { ..., dshadow2fifo.rx_addr, dshadow2fifo.rx_be, dshadow2fifo.rx_data } ),
    .rd_en   ( 1'b1 )
);
```

这就是 u4-l1 提到的「主机 USB 命令经双时钟 FIFO 安全跨域桥接到配置空间 BRAM」的具体落点——49 位 = 各类控制位 + 地址 + 字节使能 + 数据整体打包。

#### 4.3.4 代码实践

**实践目标**：独立找出 3 处双时钟 FIFO，列出 `wr_clk`/`rd_clk` 并判断跨域方向（这正是本讲规格要求的练习）。

**操作步骤**：

1. 用编辑器/Grep 在 `PCIeSquirrel/src/` 下搜索 `_clk2`，列出全部匹配。
2. 对以下 3 个实例，分别打开对应文件、定位例化、抄下 `.wr_clk(...)` 与 `.rd_clk(...)`：
   - `fifo_64_64_clk2_comrx` → `pcileech_com.sv:121`
   - `fifo_134_134_clk2` → `pcileech_pcie_tlp_a7.sv:118`
   - `fifo_49_49_clk2` → `pcileech_tlps128_cfgspace_shadow.sv:47`
3. 把结果填进下表。

**需要观察的现象**：每个 FIFO 的 `wr_clk` 与 `rd_clk` 分别接到哪个时钟信号（`clk_com`/`clk`/`clk_sys`/`clk_pcie`）。

**预期结果**（参考答案）：

| FIFO | wr_clk | rd_clk | 跨越的时钟域 |
| --- | --- | --- | --- |
| `fifo_64_64_clk2_comrx` | `clk_com`（= `ft601_clk`） | `clk`（= `clk_sys`） | 通信域 → 系统域 |
| `fifo_134_134_clk2` | `clk_pcie` | `clk_sys` | PCIe 域 → 系统域 |
| `fifo_49_49_clk2` | `clk_sys` | `clk_pcie` | 系统域 → PCIe 域 |

**进阶观察**：把找到的全部 `_clk2` FIFO 按方向分类，会发现「系统域 ↔ PCIe 域」是出现最频繁的跨域方向——因为 PCIe 硬核跑在自己的 `clk_pcie` 上，而主机控制面全在 `clk_sys` 上，两域之间数据往来最密集。

如果无法本地运行 Vivado，此实践为纯源码阅读，结论可完全离线得到。

#### 4.3.5 小练习与答案

**练习 1**：`fifo_256_32_clk2_comtx`（[pcileech_com.sv:171-183](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_com.sv#L171-L183)）的写入位宽是 256、读出位宽是 32，这说明它除了跨域还做了什么？

> **答案**：它同时做了 **256→32 的位宽转换**。系统域 `fifo` 一次吐出一个 256 位大包（1 状态 + 7 数据，见 u2-l4），而 FT601 物理口是 32 位，所以这个 FIFO 一边把 256 位跨到 `clk_com` 域，一边拆成 8 个 32 位字依次送出。双时钟 FIFO 顺势承担了跨域 + 拆宽两重职责。

**练习 2**：为什么 `fifo_1_1_clk2`（[pcileech_pcie_tlp_a7.sv:252-263](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L252-L263)只有 1 位宽，也要用双时钟 FIFO？

> **答案**：它传递的不是数据本身，而是一个**事件脉冲**（「一整个包已拼好」）。即使只有 1 位，只要它要从 `clk_sys` 域传到 `clk_pcie` 域，仍会面临亚稳态（单比特跨域通常用两级同步器即可，但这里用 1 位 FIFO 既能同步又能天然做「计数」语义——每写入一次就在读侧产生一个 `valid`，配合 `pkt_count` 实现低延迟整包指示，见 u3-l4）。

## 5. 综合实践

**综合任务：画出 PCIeSquirrel 的「时钟域 + 跨域 FIFO」全图。**

把本讲知识串起来，做一张覆盖三大时钟域与全部跨域通路的示意图：

1. **画三个大方框**，分别标注「通信域 clk_com」「系统域 clk(=clk_sys)」「PCIe 域 clk_pcie」。
2. **在域内填上主要模块**：通信域→`pcileech_ft601`；系统域→`pcileech_fifo`、`pcileech_mux`；PCIe 域→`pcie_7x_0`、`pcileech_pcie_cfg_a7`、`pcileech_pcie_tlp_a7`。
3. **在域与域的边界画双时钟 FIFO**，用箭头标方向，并写上实例名。至少包含：
   - 通信域↔系统域：`fifo_64_64_clk2_comrx`（→）、`fifo_256_32_clk2_comtx`（←）。
   - 系统域↔PCIe 域：`fifo_64_64` 与 `fifo_134_134_clk2`（sys→pcie 与 pcie→sys 各一）、`fifo_49_49_clk2` / `fifo_43_43_clk2`（影子通路）。
4. **在图边标注每个时钟的来源**：`clk`←SYSCLK、`clk_com`←FT601、`clk_pcie`←`pcie_7x_0.user_clk_out`（源头是经 `IBUFDS_GTE2` 的 100MHz 差分参考时钟）。

**验收标准**：

- 任何一个 `_clk2` FIFO 都能在这张图上找到位置，且箭头方向与 `wr_clk/rd_clk` 一致。
- 能指出「系统域 ↔ PCIe 域」是跨域最密集的地带，并解释原因（PCIe 硬核独占 `clk_pcie`，主机控制面全在 `clk_sys`）。
- 能在图上指出至少一处「跨域同时做位宽转换」的 FIFO（`fifo_256_32_clk2_comtx`）。

画完后，对照 4.3.2 节的表格自查是否有遗漏。这张图也将成为阅读 u5-l2（约束文件，含 `set_false_path` 等跨域时序约束）时的必备参考。

## 6. 本讲小结

- pcileech-fpga 有三大异步时钟域：系统域 `clk`/`clk_sys`（板载 100MHz）、通信域 `clk_com`/`ft601_clk`（FT601 输出）、PCIe 域 `clk_pcie`（PCIe 硬核 `user_clk_out`，源头是经 `IBUFDS_GTE2` 的 100MHz 差分参考时钟）。
- 不同域之间相位无固定关系，直接互连会触发亚稳态；标准解法是双时钟（异步）FIFO，它用格雷码指针 + 两级同步链把风险压到极小。
- 命名规律：`fifo_<写入位宽>_<读出位宽>_<时钟模式>[_后缀]`，`_clk2`=双时钟（跨域）、`_clk1`=单时钟（不跨域），位宽不同表示跨域的同时做位宽转换。
- `pcileech_com` 的 `fifo_64_64_clk2_comrx` 是教学样本：`wr_clk=clk_com`、`rd_clk=clk`、`rd_en=1'b1`，配合 32→64 拼装满足作者所述 `2*clk_com < clk` 的保守速率前提。
- 跨域最密集的地带是「系统域 ↔ PCIe 域」，每条通路都成对出现（一来一回），且 134 位等「数据+边界位整体打包」跨域以避免错拍。
- 复位也要考虑跨域，如 `comrx` 的 `.rst = rst | (tickcount64_com<2)`，把系统域复位与通信域计数器合起来，确保两侧时钟都稳定后才放开。

## 7. 下一步学习建议

- **u5-l2 约束文件**：跨时钟域在 Vivado 里需要对应的时序约束才能通过——`set_false_path` / `set_clock_groups -asynchronous` 告诉综合器「这两组时钟是异步的，不要硬算它们之间的建立/保持时间」。学完本讲后，去 `.xdc` 里找这些约束会非常自然。
- **u5-l3 Xilinx IP 与工程生成**：本讲反复出现的 `fifo_*_clk2` 都是 FIFO Generator 产物，`.xci` 文件记录了它们的配置（深度、位宽、几乎满/空阈值）。下一讲会讲这些 IP 如何被 `vivado_generate_project.tcl` 组织起来。
- **回看 u3-l1 / u3-l2**：现在再读 `pcileech_pcie_a7` 的复位分层（`rst_subsys` vs `rst_pcie`）与 `cfg_a7` 的两个跨域 FIFO，会有更深的理解——它们正是本讲表格里的实例。
