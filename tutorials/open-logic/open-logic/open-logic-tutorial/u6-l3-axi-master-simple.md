# AXI4 简单主机（olo_axi_master_simple）

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `olo_axi_master_simple` 在用户侧暴露的**命令接口**长什么样，以及它为什么比直接驱动 AXI 简单。
- 理解实体名里 _simple_（简单）一词的两层含义：**不做非对齐访问**、**不做位宽转换**，并能把这条限制换算成具体的地址约束。
- 看懂一个任意长度的用户命令是如何被**自动切分**成多个不超长、不跨 4KB 边界的 AXI 突发的。
- 读懂 `olo_axi_pkg_protocol` 这个协议包提供了哪些常量、以及主机在哪里用到它们。
- 学会用「高延迟 / 低延迟」两种模式发起**突发读写**，并明白两者的取舍。

本讲承接 u6-l1（`olo_axi_pl_stage`、AXI4 五通道与 AXI4-Lite 复用）和 u2-l4（同步 FIFO），属于 AXI 区域「主机」线的第一讲。

## 2. 前置知识

阅读本讲前，建议你已经了解以下概念（在 u1/u2/u6 已建立）：

- **AXI4 五通道**：写地址 AW、写数据 W、写响应 B、读地址 AR、读数据 R。每条通道都是独立的 Valid/Ready 握手。请求通道（AW/W/AR）方向是主机→从机，响应通道（B/R）方向是从机→主机。
- **突发（Burst）**：一次 AXI 地址握手之后，数据通道上连续传输若干拍（beat）。AXI4 用 `AxLen` 表示**传输拍数减一**（即 `AxLen=7` 表示 8 拍）。
- **AXI-S 握手与反压**：Valid 与 Ready 同时为高的上升沿完成一次传递（见 u1-l5、u2-l2）。
- **同步 FIFO**：本主机内部大量复用 `olo_base_fifo_sync` 做命令/数据/响应的缓冲与解耦（见 u2-l4）。
- **两进程法 + record**：组合进程 `p_comb` 算下一拍状态 `r_next`，时序进程 `p_reg` 打拍并复位，状态收进 record（见 u2-l2）。

如果你还不熟悉 AXI4 的 `AxSize`、`AxBurst`、`BResp` 等字段含义，不用担心，本讲会在用到时结合 `olo_axi_pkg_protocol` 一并解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/axi/vhdl/olo_axi_master_simple.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd) | 本讲主角。一个把「简单命令」翻译成合规 AXI4 传输的主机实体。 |
| [src/axi/vhdl/olo_axi_pkg_protocol.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pkg_protocol.vhd) | AXI 协议常量包（响应码、突发类型、传输位宽编码）。 |
| [doc/axi/olo_axi_master_simple.md](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_master_simple.md) | 官方文档，含接口表、架构图与多种时序波形。 |
| [test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd) | 配套 VUnit 测试台，是本讲代码实践的主要依据。 |
| [test/tb/olo_test_axi_slave_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_slave_vc.vhd) | AXI 从机验证组件（VC），在测试台中扮演「对端」。 |

> 提示：协议包的文件头里写着 _"This file is not intended to be used directly"_——意思是用户一般不直接 `use` 它，而是通过 `olo_axi_master_simple` 等实体间接享受它定义的常量。

---

## 4. 核心概念与源码讲解

### 4.1 命令接口：把 AXI 复杂度藏到幕后

#### 4.1.1 概念说明

让用户逻辑直接去驱动 AXI4 五通道是很繁琐的：你得自己管 AW/AR 的地址与 `Len`、自己数 W 通道的拍数与 `WLast`、自己等 B 通道的响应、自己缓存 R 通道的数据。`olo_axi_master_simple` 的价值就是把这些都包起来，只给用户留一个极简的**命令接口（Command Interface）**：

- 你告诉它「**从哪个地址开始、传多少拍**」，它就帮你生成全部 AXI 信号。
- 命令和数据是**解耦**的：写数据可以在命令之前、之后或同时给出，没有强制的时序关系。
- 整条用户命令完成后，它用一个单周期脉冲 `Wr_Done`/`Rd_Done`（或失败时的 `Wr_Error`/`Rd_Error`）通知你。

这里有一个关键设计取舍——**两种工作模式**：

- **High Latency（高延迟，默认）**：主机在 FIFO 里凑够一次突发所需的数据（写）或空间（读）之后，才真正向 AXI 总线发出命令。好处是不会阻塞总线；代价是首笔延迟略高。
- **Low Latency（低延迟）**：主机收到命令立刻发出，不等 FIFO。延迟最低，但如果用户数据跟不上，可能会把 AXI 总线卡住。

读通路有个特别之处：**读操作通常永远用高延迟模式**。因为即便高延迟，用户也能在第一个数据回来后立刻读走，几乎不增加体感延迟，却能避免总线被卡。这一点官方文档专门强调过。

> 文档还提醒：每笔写事务至少需要 **4 个时钟周期**，所以这个实体**不适合**大量「单拍（single-beat）零碎访问」的场景。

#### 4.1.2 核心流程

一次用户写命令的端到端流程可以这样描述（读侧对称）：

```text
用户: 在 CmdWr_* 上给出 Addr/Size/LowLat，拉高 CmdWr_Valid
  │
  ▼  (CmdWr_Ready=1 时握手成功)
主机内部:
  1. WriteTfGen FSM  把 Size 拆成若干不超长、不跨 4KB 的子突发
  2. AW FSM          经 AW 通道逐个发出子突发地址（受延迟模式/ outstanding 数门控）
  3. wr_trans FIFO   记录每个子突发的拍数，供 W FSM 取用
  4. W FSM           把 wr_data FIFO 里的数据按拍数经 W 通道送出，并在末拍置 WLast
  5. wr_resp FIFO    记录「这是否是本用户命令的最后一个子突发」
  6. B 通道应答      收到最后一笔 B 响应后，脉冲 Wr_Done（或 Wr_Error）
```

读侧把 AW/W 换成 AR，把写数据 FIFO 换成**读数据 FIFO**（缓冲从机返回的 R 数据），响应处理改成在 R 通道 `RLast` 上判断。

#### 4.1.3 源码精读

先看实体声明里的**用户侧端口**（注意所有可选输入都带默认值，这是 Open Logic 一贯风格）：

[src/axi/vhdl/olo_axi_master_simple.vhd:56-82](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L56-L82) —— 用户命令接口（写/读）、写数据、读数据、响应脉冲。其中 `CmdWr_Size`/`CmdRd_Size` 的单位是**拍数（beats）**而非字节；`Rd_Last` 标记一条用户命令的最后一个数据拍。

实体名里 "simple" 的官方说明写在文件头的注释里，值得直接读原文：

[src/axi/vhdl/olo_axi_master_simple.vhd:10-14](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L10-L14) —— 明确说不支持非对齐读写、不做位宽转换。

命令与数据的解耦，体现在主机内部用了**三个写侧 FIFO + 两个读侧 FIFO** 来吸收时序差异。这些 FIFO 都基于 `olo_base_fifo_sync`：

[src/axi/vhdl/olo_axi_master_simple.vhd:682-758](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L682-L758) —— 写侧三个 FIFO：`wr_trans`（每个子突发的拍数）、`wr_data`（写数据+字节使能）、`wr_resp`（是否末次子突发的标志）。

[src/axi/vhdl/olo_axi_master_simple.vhd:770-828](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L770-L828) —— 读侧：`rd_data` FIFO（缓冲 R 数据并按用户命令重打 `Last`）与 `rd_resp` FIFO。

注意 `wr_trans`/`wr_resp`/`rd_resp` 三个 FIFO 的深度都等于 `AxiMaxOpenTransactions_g`（最多在途命令数），因为它们是「每条在途命令一项」的结构；`wr_data`/`rd_data` 才是真正承载数据的大 FIFO，深度由 `DataFifoDepth_g` 决定。

#### 4.1.4 代码实践

**实践目标**：通过阅读测试台，直观感受「命令与数据的时序无关」。

**操作步骤**：

1. 打开 [test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd)。
2. 阅读三个相邻用例：`SingleWrite-DataCmdTogether`（数据与命令同时给）、`SingleWrite-DataBeforeCmd`（数据先于命令）、`SingleWrite-CmdBeforeData`（命令先于数据）。
3. 注意 `SingleWrite-CmdBeforeData` 中这一段断言：

[src/axi/.../olo_axi_master_simple_tb.vhd:307-328](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd#L307-L328) —— 高延迟模式下命令先到、数据后到时，`AxiMs.aw_valid` 应为 `'0'`（命令被压住）；低延迟模式下则为 `'1'`（命令立刻发出）。

**需要观察的现象**：同样的「写一个字」，因为 `LowLat` 不同，`aw_valid` 拉起的时刻完全不同。

**预期结果**：高延迟模式在数据进 FIFO 前不会发起 AW；低延迟模式会立刻发起 AW。仿真通过即说明命令/数据解耦逻辑正确。具体仿真结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：用户接口的 `CmdWr_Size` 单位是字节还是拍数？为什么这样设计？

> **答案**：单位是**拍数（beats）**，即 AXI 数据宽度的个数。这样设计是因为主机内部需要按拍数去数 `WLast`/`RLast`、去分配 FIFO 空间，用拍数最直接；字节数到拍数的换算（除以 `AxiDataWidth_g/8`）交给用户。

**练习 2**：为什么读操作官方建议「几乎总是用高延迟模式」？

> **答案**：高延迟读模式下，用户仍可在第一个数据到达后立即读走，体感延迟几乎不增加；同时它保证读 FIFO 有足够空间一次性收完一个突发，避免 `RReady` 被迫拉低而卡住 AXI 总线。除非有非常特殊的低延迟需求，否则高延迟更优。

---

### 4.2 字对齐限制与传输自动切分

#### 4.2.1 概念说明

"simple" 的核心约束是**字对齐（word-aligned）**：

- 用户接口的数据宽度与 AXI 总线数据宽度**完全相同**，不做任何位宽转换。
- 所有命令的起始地址必须是「一个 AXI 字（`AxiDataWidth_g/8` 字节）」的整数倍。

为什么？因为一旦允许非对齐访问，主机就必须做复杂的「跨字节拼拆、补零、部分字节使能」逻辑——这正是 `olo_axi_master_full`（下一讲 u6-l4）要做的事。`olo_axi_master_simple` 选择不做这些，换取更小的面积与更简单的时序。

对齐在代码里体现为一个常量与一个掩码函数：

- `UnusedAddrBits_c = log2(AxiDataWidth_g/8)`：地址最低几位是用来在「一个字」内选字节的，对齐传输时这几位必须为 0。
- `addrMasked()`：把地址的低 `UnusedAddrBits_c` 位强制清零，作为对 AXI 的保护。

第二个要点是**自动切分（splitting）**。AXI4 规定单次突发**不能跨越 4KB 边界**，且拍数不能超过协议上限（AXI4 为 256、AXI3 为 16）。但用户的一条命令可以任意长（受 `UserTransactionSizeBits_g` 限制）。于是主机内部用一个状态机把长命令拆成若干合规子突发：

- 每个子突发的拍数取「剩余拍数」与「到 4KB 边界的拍数」与「`AxiMaxBeats_g`」三者中的最小值。
- 拆完后地址相应前移，循环直到剩余拍数为 0。

#### 4.2.2 核心流程

写传输切分状态机 `WriteTfGen`（读侧 `ReadTfGen` 完全对称）有四个状态：

```text
Idle_s     等待用户命令握手
   │  握手后记录 Addr/Size/LowLat
   ▼
MaxCalc_s  计算本段允许的最大拍数 WrMaxBeats
   │  WrMaxBeats = min(AxiMaxBeats_g, 到4KB边界的拍数)
   ▼
GenTf_s    决定本子突发的拍数 WrTfBeats 与 IsLast 标志
   │  WrTfBeats = min(WrMaxBeats, 剩余WrBeats)
   ▼
WriteTf_s  等下游 AW FSM 取走本子突发
   │  取走后：WrBeats -= WrTfBeats；WrAddr += WrTfBeats 个字
   │  若 IsLast → Idle_s；否则 → MaxCalc_s（继续拆下一段）
```

「到 4KB 边界还剩多少拍」的数学表达：4KB 边界是地址位 [11:0] 回绕点。地址的第 11..`UnusedAddrBits_c` 位表示「当前 4KB 页内已用的字数」。剩余字数为：

\[
\text{WrMax4kBeats} = \lnot\,\text{WrAddr}[11{:}\text{UnusedAddrBits}] + 1
\]

即把「页内字地址」取反加一，得到从当前字到页尾（含）的字数。代码里用 `not r.WrAddr(11 downto UnusedAddrBits_c) + 1` 直接实现，再把结果与 `AxiMaxBeats_g` 比较，取较小者。

#### 4.2.3 源码精读

字对齐的常量与掩码函数：

[src/axi/vhdl/olo_axi_master_simple.vhd:128-148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L128-L148) —— `UnusedAddrBits_c` 与 `addrMasked()`，握手时对命令地址做 `addrMasked` 处理。

静态断言在编译期就挡住非法配置（比如 `UserTransactionSizeBits_g` 过大，导致单条命令可能跨满整个地址空间）：

[src/axi/vhdl/olo_axi_master_simple.vhd:242-250](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L242-L250) —— 三条断言：数据宽度是 8 的倍数、字节数是 2 的幂、`UserTransactionSizeBits_g < AxiAddrWidth_g-log2(AxiDataWidth_g/8)`。

切分状态机本体（写侧）：

[src/axi/vhdl/olo_axi_master_simple.vhd:293-340](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L293-L340) —— `WriteTfGen` 四态机。其中 `MaxCalc_s` 计算 `WrMax4kBeats_v` 并与 `AxiMaxBeats_g` 取小；`GenTf_s` 决定 `WrTfBeats` 与 `WrTfIsLast`；`WriteTf_s` 在被取走后扣减剩余拍数、推进地址。

读侧的对应状态机结构完全一致，只是把 `Wr` 前缀换成 `Rd`：

[src/axi/vhdl/olo_axi_master_simple.vhd:477-524](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L477-L524) —— `ReadTfGen`，逻辑与写侧镜像。

#### 4.2.4 代码实践

**实践目标**：用一个现成的测试用例，亲眼看到「一条用户命令被拆成两段 AXI 突发」。

**操作步骤**：

1. 打开测试台用例 `BurstWriteOver4kBoundary`：

[src/axi/.../olo_axi_master_simple_tb.vhd:453-465](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd#L453-L465) —— 用户命令为「跨 4KB 边界的 8 拍写」，从机侧期望被拆成「边界前 2 拍 + 边界后 6 拍」两段。

2. 在 `sim/` 目录运行该用例（GHDL 默认）：

```bash
cd sim
python3 run.py '*olo_axi_master_simple_tb*BurstWriteOver4kBoundary*' -v
```

3. 在波形/日志中观察 `AxiMs.aw_addr` 与 `AxiMs.aw_len`：应该出现两次 AW 握手，地址分别落在边界两侧，`aw_len` 分别为 `2-1=1` 与 `6-1=5`。

**需要观察的现象**：一次 `pushCommand(... 8)`（8 拍）产生两段 AW，第一段地址是 `X"1000"-2*字宽`、长度 2；第二段地址是 `X"1000"`、长度 6。

**预期结果**：两段子突发地址连续、长度之和为 8，且都不跨 4KB。仿真通过即验证切分正确。具体波形**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`AxiDataWidth_g=32` 时，`UnusedAddrBits_c` 等于多少？合法的起始地址有什么特征？

> **答案**：`log2(32/8)=log2(4)=2`，即地址最低 2 位必须为 0。合法起始地址是 4 的整数倍（如 `0x1000`、`0x1088`）。测试台里用到的 `X"1088"` 正是 4 的倍数。

**练习 2**：如果一个 4KB 页还剩 2 个字，而用户命令要写 8 拍，主机会怎么拆？地址如何推进？

> **答案**：拆成两段。第一段取 `min(AxiMaxBeats_g, 剩余到页尾=2, 剩余命令=8)=2` 拍，地址落在页尾前 2 个字；写完后地址推进到下一个 4KB 页起点，第二段写剩余 6 拍。这正是 `BurstWriteOver4kBoundary` 用例的情形。

---

### 4.3 协议包辅助：olo_axi_pkg_protocol

#### 4.3.1 概念说明

AXI4 协议里有一堆「魔法数字」编码，比如写响应 `BResp`：`00` 表示 OKAY（成功）、`10` 表示 SLVERR（从机错误）、`11` 表示 DECERR（译码错误）。如果到处直接写 `"00"`、`"10"`，代码可读性会很差，还容易写错。

`olo_axi_pkg_protocol` 就是把这些编码**集中定义成命名常量**的小工具包。它不包含任何逻辑（包体为空），纯粹是一份「协议字典」。它的价值有两点：

1. **可读性**：`M_Axi_BResp /= AxiResp_Okay_c` 一眼能看出是在判断「响应是否非 OKAY」。
2. **单一真相源**：响应码、突发类型、传输位宽的编码只在这一处定义，全库（包括主机、从机、测试 VC）共用同一套定义，不会出现各处编码不一致。

#### 4.3.2 核心流程

包里定义了三组常量，每组先声明一个 `subtype`（限定宽度的 `std_logic_vector`），再定义若干命名常量：

```text
Resp_t  (2 bit)  : Okay="00"  ExOkay="01"  SlvErr="10"  DecErr="11"
Burst_t (2 bit)  : Fixed="00" Incr="01"    Wrap="10"
Size_t  (3 bit)  : 1,2,4,...,128 字节  (AxiSize_1_c .. AxiSize_128_c)
```

`Size_t` 编码的是 `AxSize` 字段，表示「本事务每拍的字节数以 2 为底的对数」。例如 32 位（4 字节）数据对应 `AxSize=010`（即 `log2(4)=2`）。

#### 4.3.3 源码精读

协议包全文很短，值得整段读：

[src/axi/vhdl/olo_axi_pkg_protocol.vhd:25-48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pkg_protocol.vhd#L25-L48) —— 三组常量定义，包体为空。

主机里通过 `use work.olo_axi_pkg_protocol.all;` 引入它（见 [olo_axi_master_simple.vhd:33](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L33)）。实际用到的地方有两类：

**① 作为常量输出**：主机的突发类型直接固定为 INCR（增量突发），用常量表达：

[src/axi/vhdl/olo_axi_master_simple.vhd:664-675](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L664-L675) —— `M_Axi_AwBurst/ArBurst <= AxiBurst_Incr_c`；其余如 `Cache="0011"`、`Prot="000"`、`Lock='0'` 也在此一次性赋值。

**② 作为响应判断**：写响应/读响应 FSM 用 `AxiResp_Okay_c` 判断成功与否：

[src/axi/vhdl/olo_axi_master_simple.vhd:453-466](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L453-L466) —— B 通道响应处理：`M_Axi_BResp /= AxiResp_Okay_c` 即判错。

> 小细节：`AxSize` 字段主机并没有直接引用包里的 `AxiSize_4_c` 等常量，而是用 `to_unsigned(log2(AxiDataWidth_g/8), 3)` 在线计算（见第 665–666 行）。这是因为数据宽度是泛型，编码必须随泛型变化；包里的 `Size_t` 常量更适用于宽度固定的场合。

#### 4.3.4 代码实践

**实践目标**：确认「单一真相源」确实被遵守——同一套响应码在主机与测试台里含义一致。

**操作步骤**：

1. 在协议包中确认 `AxiResp_SlvErr_c = "10"`（[olo_axi_pkg_protocol.vhd:30](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pkg_protocol.vhd#L30)）。
2. 在测试台 `SingleWrite-RespError` 用例中，看到从机 VC 用同一个常量回送错误响应：

[src/axi/.../olo_axi_master_simple_tb.vhd:366-378](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd#L366-L378) —— `push_b(net, AxiSlave_c, resp => AxiResp_SlvErr_c)`，随后主机应脉冲 `Wr_Error`。

**需要观察的现象**：测试台也 `use olo.olo_axi_pkg_protocol.all`（见测试台第 22 行），与主机共用同一常量。

**预期结果**：从机回 `SlvErr` → 主机 `Wr_Error` 脉冲一次、`Wr_Done` 保持 0。仿真通过即验证两端编码一致。具体结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`AxiBurst_Incr_c` 与 `AxiBurst_Wrap_c` 对应的突发行为有何不同？`olo_axi_master_simple` 用的是哪一种？

> **答案**：INCR（增量）突发中每拍地址依次递增，不回绕；WRAP（回绕）突发在到达边界时会回绕到低位（常用于 Cache 行填充）。主机固定用 `AxiBurst_Incr_c`（见第 667–668 行），因为它做的是连续地址的通用 DMA 式传输。

**练习 2**：为什么协议包要先用 `subtype Resp_t is std_logic_vector(1 downto 0)` 再定义常量，而不是直接定义常量？

> **答案**：`subtype` 限定了宽度与方向，让常量在赋值时能被编译器做宽度匹配检查，减少把 2 位响应误连到其它宽度信号上的错误；同时也让函数/端口签名可以统一用 `Resp_t`。

---

### 4.4 突发读写：高/低延迟与在途命令管理

#### 4.4.1 概念说明

把命令切分成子突发（4.2）之后，还要解决两个问题才能让突发真正跑起来：

1. **什么时候允许发出一条 AW/AR 命令？** 这就是高/低延迟模式的本质——它门控 AW FSM / AR FSM 的启动条件。
2. **怎么知道一条用户命令「整条完成了」？** 因为一条用户命令可能对应多条 AXI 突发，每条突发都会回来一个 B 响应（写）或一个 `RLast`（读）。必须跟踪「这是不是最后一条」，才能在最后一条完成时脉冲一次 `Wr_Done`/`Rd_Done`。

主机用两个计数器/标志分别管理写、读两侧的「数据就绪度」：

- **写侧 `WrBeatsNoCmd`**：当前已经写进 `wr_data` FIFO、但**还没有被某条 AW 命令「认领」**的拍数。高延迟模式下，只有当 `WrBeatsNoCmd >= 本段突发拍数` 时才允许发 AW——也就是「数据齐了才发命令」。
- **读侧 `RdFifoSpaceFree`**：读数据 FIFO 当前剩余可容纳的拍数（复位初值 = `DataFifoDepth_g`）。高延迟模式下，只有当剩余空间 ≥ 本段突发拍数时才允许发 AR——也就是「有地方收才发命令」。

另外还有一个公共约束：**在途命令数**（outstanding transactions）不能超过 `AxiMaxOpenTransactions_g`，否则会撑爆那几个深度为 `AxiMaxOpenTransactions_g` 的小 FIFO。

#### 4.4.2 核心流程

写侧 AW FSM 的启动条件（读侧 AR FSM 对称）：

\[
\text{允许发命令} = \big(\text{LowLat} = '1'\;\lor\;\text{WrBeatsNoCmd} \ge \text{WrTfBeats}\big)
\;\land\; \big(\text{WrOpenTrans} < \text{AxiMaxOpenTransactions\_g}\big)
\;\land\; \big(\text{WrTfVld} = '1'\big)
\]

也就是说，低延迟模式直接跳过数据就绪检查。AW 握手成功后 `WrOpenTrans` 加一；收到一个 B 响应 `WrOpenTrans` 减一。

命令完成判定靠 `wr_resp` FIFO 里存的 `IsLast` 标志：每个子突发在 AW 发出时，把「是否本用户命令的最后一段」(`WrTfIsLast`) 推进 `wr_resp` FIFO；B 响应回来时按 FIFO 顺序取出该标志，若为「最后一段」且响应为 OKAY，就脉冲 `Wr_Done`。读侧用 `rd_resp` FIFO 存同样的标志，在 R 通道的 `RLast` 上判定。

> 巧妙的细节：读数据 FIFO 在拼装输入时，只有当本子突发是「用户命令最后一段」时，才把 AXI 的 `RLast` 透传给用户的 `Rd_Last`（[olo_axi_master_simple.vhd:779](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L779)）。这样用户看到的 `Rd_Last` 永远只在自己整条命令的最后一拍拉起，与内部被拆成几段无关。

#### 4.4.3 源码精读

AW FSM（写命令发出）与高/低延迟门控：

[src/axi/vhdl/olo_axi_master_simple.vhd:346-375](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L346-L375) —— 启动条件里的 `(r.WrLowLat='1') or (r.WrBeatsNoCmd >= signed('0' & r.WrTfBeats))` 正是高/低延迟的分叉；`AwLen` 由 `WrTfBeats-1` 得到（AXI 的 Len = 拍数 − 1）。

`WrBeatsNoCmd` 的维护（写数据进 FIFO 时 +1，发命令时按拍数扣减）：

[src/axi/vhdl/olo_axi_master_simple.vhd:377-391](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L377-L391) —— 注释说明这里为时序优化做了「扣减立即、增量延迟一拍」的取舍，最坏只让高延迟传输多等一拍。

读侧 AR FSM 与 `RdFifoSpaceFree`：

[src/axi/vhdl/olo_axi_master_simple.vhd:530-575](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L530-L575) —— 启动条件把写侧的「数据已到」换成「FIFO 有空间」；`RdFifoSpaceFree` 在用户读走数据时 +1、发 AR 命令时按拍数扣减。

读响应完成判定与 `Rd_Done`/`Rd_Error`：

[src/axi/vhdl/olo_axi_master_simple.vhd:583-596](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_master_simple.vhd#L583-L596) —— 在 `RdRespLast`（某段突发的 RLast）上判断：若是用户命令最后一段且全 OK 则脉冲 `Rd_Done`。

#### 4.4.4 代码实践

**实践目标**：运行现成的突发读/写用例，对照波形理解高/低延迟差异。

**操作步骤**：

1. 阅读测试台 `BurstRead-FifoFull` 用例，它专门构造了「读 FIFO 满」的场景来对比两种模式：

[src/axi/.../olo_axi_master_simple_tb.vhd:533-560](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_master_simple/olo_axi_master_simple_tb.vhd#L533-L560) —— FIFO 满时再发命令：高延迟模式 `ar_valid` 应为 `'0'`（被压住），低延迟模式为 `'1'`（强行发出，但会卡总线）。

2. 运行该用例：

```bash
cd sim
python3 run.py '*olo_axi_master_simple_tb*BurstRead-FifoFull*' -v
```

**需要观察的现象**：同一组激励下，仅 `LowLat` 不同，`ar_valid` 的行为截然相反。

**预期结果**：高延迟在 FIFO 腾出足够空间前不发 AR；低延迟立即发 AR 但 `RReady` 被迫拉低。仿真通过即验证门控逻辑。具体波形**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`AxiMaxOpenTransactions_g=8` 表示什么？把它设成 1 会怎样？

> **答案**：它表示「最多允许 8 条 AW/AR 命令已发出但尚未收到响应（在途）」。设成 1 表示严格串行——必须等上一条的响应回来才能发下一条，吞吐降低但 FIFO 可以更浅（那几个小 FIFO 深度就等于这个值）。

**练习 2**：为什么 `AwLen` 要写成 `WrTfBeats - 1`，而不是直接 `WrTfBeats`？

> **答案**：AXI 协议规定 `AxLen` 字段表示的是「传输拍数减一」。例如 8 拍突发 `AxLen=7`。所以代码里 `M_Axi_AwLen <= (WrTfBeats - 1)`（见第 355–356 行）。

---

## 5. 综合实践：长度 8 的突发写后读回

把本讲四个模块串起来，做一个综合任务：**用 `olo_axi_master_simple` 对一片内存发起一次长度 8 的突发写，再读回，验证数据一致，并说清其对齐约束。**

### 5.1 任务分析

Open Logic 的官方测试台用 VUnit 的**从机验证组件（VC）** `olo_test_axi_slave_vc` 扮演对端。这种 VC 的工作方式是「**预测式**」：测试代码先告诉从机 VC「主机将会写什么、应该返回什么」，VC 据此核对主机的实际行为。因此「写后读回、数据一致」在这个框架里体现为——**写侧推入的数据模式，与读侧期望返回的数据模式相同**。

仓库已经提供了现成的突发用例可以直接运行：

- `BurstWrite`：写 12 拍（`pushCommand(... 12)` + `pushWrData(X"1234", 1, 12)`）。
- `BurstRead`：读 12 拍（`pushCommand(... 12)` + `push_burst_read_aligned(... 12)` + `expectRdData(... 12)`）。

### 5.2 操作步骤（运行现成用例）

```bash
cd sim
# 先跑突发写、突发读两个用例（GHDL 默认）
python3 run.py '*olo_axi_master_simple_tb*BurstWrite*' '*olo_axi_master_simple_tb*BurstRead*' -v
```

观察日志中两个用例均 `pass`。在波形里确认：写侧出现一段 AW（`aw_len = 12-1 = 11`）+ 12 拍 W（末拍 `w_last=1`）+ 一个 B 响应，随后 `Wr_Done` 脉冲；读侧出现一段 AR + 12 拍 R（末拍 `r_last=1`），`Rd_Last` 在第 12 拍拉起，随后 `Rd_Done` 脉冲。

> 仿真结果**待本地验证**。这两个用例属于 CI 常跑集合，预期通过。

### 5.3 写后读回的「示例代码」

如果想亲手构造一个「先写 8 拍、再读回同样 8 拍」的场景，可参考下面的**示例代码**（**非项目原有代码**，仅作示意；若要实际运行需把它加入测试台 `p_control` 进程的 `run(...)` 分支，并相应声明用例，这会修改测试文件，请在你自己的副本中尝试）：

```vhdl
-- 示例代码：写后读回 8 拍，数据模式 0x100..0x107
-- 放在 test_suite 循环内、Reset 之后
if run("BurstWriteReadBack-8") then
    if ImplWrite_g and ImplRead_g then
        -- 1) 从机 VC 期望一次 8 拍写，地址 0x0100，数据 0x100 起递增
        expect_burst_write_aligned(net, AxiSlave_c, X"0100", X"100", 1, 8);
        -- 2) 主机：发写命令(8 拍) + 推 8 拍数据
        pushCommand(net, WrCmdMaster_c, X"0100", 8, CmdLowLat => '0');
        pushWrData(net, X"100", 1, 8);
        expectWrResponse(RespSuccess);

        -- 3) 从机 VC 准备返回同样 8 拍数据 0x100..0x107
        push_burst_read_aligned(net, AxiSlave_c, X"0100", X"100", 1, 8);
        -- 4) 主机：发读命令(8 拍)，期望读回 0x100..0x107
        pushCommand(net, RdCmdMaster_c, X"0100", 8, CmdLowLat => '0');
        expectRdData(net, X"100", 1, 8);   -- 与写入模式一致 => “数据一致”
        expectRdResponse(RespSuccess);
    end if;
end if;
```

要点解读：

- 写命令与读命令的地址都用 `X"0100"`，且 `AxiDataWidth_g` 默认 32（4 字节/字），`0x0100` 是 4 的倍数 → **满足字对齐**。
- `pushWrData(X"100", 1, 8)` 与 `expectRdData(X"100", 1, 8)` 的起值与增量相同，这就编码了「读回等于写入」的一致性预期。
- 读命令用高延迟（`CmdLowLat => '0'`），符合 4.1 的建议。

### 5.4 对齐约束说明（必答）

本任务之所以「简单」，正是建立在以下对齐约束上：

1. **起始地址必须字对齐**：地址必须是 `AxiDataWidth_g/8` 的整数倍。32 位数据时地址必须 4 字节对齐（如 `0x0100` 合法，`0x0102` 非法——低 2 位会被 `addrMasked` 清零，等于悄悄改地址）。
2. **不做位宽转换**：用户数据宽度恒等于 AXI 数据宽度，不能「把 8 个 8 位字节拼成一个 64 位字」或反之——那是 `olo_axi_master_full` 的职责。
3. **长度以拍为单位**：`Cmdxx_Size` 给的是 AXI 拍数，不是字节数。8 拍 × 4 字节 = 32 字节，但命令里只写 `8`。
4. **不能跨 4KB 还由用户操心**：用户可以给任意合法长度（受 `UserTransactionSizeBits_g` 限制），主机会自动在 4KB 边界与 `AxiMaxBeats_g` 处切分，用户无需干预。

---

## 6. 本讲小结

- `olo_axi_master_simple` 用一个**命令接口**（地址 + 拍数 + 延迟模式）把 AXI4 五通道的复杂度封装起来，命令与数据完全解耦，内部用多个 `olo_base_fifo_sync` 缓冲。
- "simple" 的两层含义：**不做非对齐访问、不做位宽转换**；起始地址必须字对齐（`addrMasked` 强制清零低 `log2(AxiDataWidth_g/8)` 位）。
- 任意长度的用户命令会被 `WriteTfGen`/`ReadTfGen` 状态机**自动切分**成不超长（`AxiMaxBeats_g`）、不跨 4KB 边界的多个 AXI 突发。
- `olo_axi_pkg_protocol` 是一份空的「协议字典」，集中定义响应码（`AxiResp_*`）、突发类型（`AxiBurst_*`）、传输位宽（`AxiSize_*`）编码，是主机与测试 VC 共用的单一真相源。
- **高延迟**模式等数据/空间就绪再发命令、不卡总线（默认）；**低延迟**模式立即发命令、延迟最低但可能卡总线；读操作通常建议永远用高延迟。
- 完成判定靠 `wr_resp`/`rd_resp` 两个小 FIFO 跟踪「是否本用户命令的最后一段」，在最后一段的 B 响应/`RLast` 上脉冲一次 `Done`；在途命令数受 `AxiMaxOpenTransactions_g` 限制。

## 7. 下一步学习建议

- **学下一讲 u6-l4**：`olo_axi_master_full`。它在本讲基础上**放开非对齐访问**与**位宽转换**，理解了本讲的「simple 限制」之后，去看 full 版本如何用额外逻辑处理跨字节拼拆，会非常有对照感。
- **回看 u6-l2**：`olo_axi_lite_slave`。本讲是「主机」、那讲是「从机」，两者拼起来正好构成一次完整的 AXI4-Lite 寄存器访问链路，可以作为综合练习的对象。
- **深读测试 VC**：若你想在自己的设计里用 `olo_axi_master_simple`，建议读一遍 [test/tb/olo_test_axi_slave_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_axi_slave_vc.vhd)，理解 `expect_burst_write_aligned` / `push_burst_read_aligned` 等辅助过程，能让你为自己的主机写测试时事半功倍。
- **跑一遍完整 axi 区域测试**：在 `sim/` 执行 `python3 run.py -l` 查看 `olo_axi_master_simple_tb` 的所有配置（不同地址/数据宽度、只读/只写组合），挑两三个跑通，巩固对泛型的理解。
