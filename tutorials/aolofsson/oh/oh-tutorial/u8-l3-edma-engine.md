# edma DMA 引擎

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 **DMA（直接内存访问）** 解决什么问题，以及 OH! 的 `edma` 是怎样用「状态机 + 数据通路 + 寄存器堆」三件套实现它的。
- 读懂 `edma_ctrl.v` 里的 **DMA 状态机**：从空闲到搬运、再到完成的整条状态流转，以及「描述符预取」这条支路。
- 手算 `edma_dp.v` 里的 **stride（步长）数据通路**：每完成一次事务，源地址、目的地址、计数器如何更新，1D 与 2D 的差别在哪里。
- 理解 **描述符链（chain）** 的设计意图：把一组搬运参数放进内存，让 DMA 自己去取、取完再取下一条，实现「软件只配一次，硬件自动搬」。
- 识别本模块「文档（README）是愿景、RTL 才是事实」的几处落差，并能解释为何本模块无法原样编译、哪些功能其实只是占位。

本讲是第 8 单元（AXI、DMA 与多链路系统）的第三讲，依赖你已经学过 **emesh 104 位包格式（u5-l1）**、**计数与算术数据通路（u3-l3）**，并会用到 **寄存器映射 `.vh` 模式（u6-l1）**。

## 2. 前置知识

### 2.1 为什么需要 DMA

CPU 读写内存（或外设寄存器）时，每搬一个字都要占用一条指令、一个时钟周期。当要搬运大批量数据（一帧图像、一段缓冲、一整块矩阵）时，CPU 会被「搬砖」占满，没空做真正的计算。

**DMA 引擎**就是一个替 CPU 搬砖的小硬件：CPU 只需要告诉它「从哪儿、到哪儿、搬多少、每次走多远」，它就能自己产生一连串读写事务，搬完再（可选地）用中断通知 CPU。CPU 在这段时间里可以去干别的。

`edma` 是 OH! 里的「轻量级 DMA 引擎」（README 原文 *A lightweight DMA engine*），它跑在 emesh 片上网络之上——也就是说，它产生和消费的都是你在 u5-l1 学过的那 **104 位 emesh 事务包**。

### 2.2 1D 与 2D 搬运，以及 stride

- **1D 搬运**：一条直线，从 `SRCADDR` 开始，每次走一个固定步长（stride），搬 `N` 次，目的端同步从 `DSTADDR` 走另一个步长。典型场景：搬一段连续内存。
- **2D 搬运**：把数据当成「行 × 列」的矩阵。内层循环（inner）沿一行搬运，外层循环（outer）跳到下一行。典型场景：把一块矩阵从紧凑存储搬到带行间距（stride）的目的布局，常见于图像/矩阵运算。

步长 **stride** 是「每完成一次事务后，地址加多少」。它用**有符号数**表示，所以也能往回搬（负步长）。本讲的实践核心就是手算这个地址序列。

### 2.3 三条关键约定（来自前置讲义）

1. **emesh 包**：一个定长事务包，低 8 位是控制字节（含 `write`、`datamode`），随后是 `dstaddr`、`data`、`srcaddr`。包宽 \(PW = 2\cdot AW + 40\)，\(AW=32\) 时 \(PW=104\)（见 u5-l1）。
2. **`.vh` 寄存器映射**：每个 IP 用一个 `xxx_regmap.vh` 头文件，给每个寄存器分配一个大写宏常量作地址编号，再用 `dstaddr[某几位]` 译码产生写选通（见 u6-l1）。
3. **`access` / `wait` 握手**：`access≈valid`，`wait` 高有效表示反压（`~wait≈ready`）。一次事务成立当且仅当同一拍 `access=1` 且 `wait=0`。

> ⚠️ 一句贯穿全讲的提醒：`edma` 是一个**施工区（HH 级）**模块。README 描述的是一个相当完整的设计，但 RTL 里有若干处只画了骨架、留了 `TODO`，甚至有状态机死循环。本讲会**先讲设计意图、再讲 RTL 实际做了什么**，二者不一致处会明确标注。

## 3. 本讲源码地图

`edma` 一共 4 个设计文件 + 1 个 `.vh` + 1 个 DUT 包装 + 1 个测试激励：

| 文件 | 作用 |
|------|------|
| [edma/hdl/edma.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma.v) | 顶层。只做一件事：把下面三个子模块的端口用 wire 拼起来，自身几乎没有逻辑。 |
| [edma/hdl/edma_ctrl.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v) | **状态机**（ sequencer）。决定「现在该取描述符、该搬运、还是该结束」，并产生 `update`、`master_active`、`fetch_access` 等控制信号。 |
| [edma/hdl/edma_dp.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v) | **数据通路**。纯组合地算出「下一个 count / srcaddr / dstaddr」，并把进出的事务包在 master/slave 两种模式间切换、打一拍流水。 |
| [edma/hdl/edma_regs.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v) | **寄存器堆**。承载 8 个配置/状态寄存器，做地址译码、写选通、读回，并在 `update` 时把数据通路算出的「下一拍值」锁存回来。 |
| [edma/hdl/edma_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regmap.vh) | 寄存器地址宏定义（u6-l1 范式）。 |
| [edma/dv/dut_edma.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/dv/dut_edma.v) | 把 `edma` 包装成测试平台认识的 `dut` 模块（u4-l3 范式）。 |
| [edma/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/dv/tests/test_basic.emf) | 一个 `.emf` 测试激励骨架（u4-l2 范式）。 |

三者（ctrl / dp / regs）的依赖关系是一个漂亮的**反馈环**：

```
        配置/状态                     控制信号
 edma_regs  ───────►  edma_ctrl  ───────►  edma_dp
   ▲   (count/src/dst 「下一拍值」 + update)      │
   └─────────────── 反馈锁存 ◄────────────────────┘
```

- `edma_regs` 把当前寄存器值（`count_reg`、`srcaddr_reg`、`dstaddr_reg`、`stride_reg`…）喂给 `edma_dp`；
- `edma_dp` 用这些值**组合地**算出「下一次更新后的值」（`count`、`srcaddr`、`dstaddr`）；
- `edma_ctrl` 决定何时发出 `update` 脉冲；
- `edma_regs` 在 `update` 那一拍把算出的新值**锁存回**寄存器。

这个「组合算下一拍 + 选通锁存」的套路，正是 u3-l3 讲过的数据通路思想在 DMA 上的复用。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 DMA 状态机**、**4.2 stride 数据通路**、**4.3 描述符链与寄存器映射**。

### 4.1 DMA 状态机（edma_ctrl）

#### 4.1.1 概念说明

状态机是 DMA 的「指挥」。它回答两个问题：

1. **现在该干什么？** —— 取描述符？搬运？还是已经搬完？
2. **什么时候算「完成一次事务」？** —— 这决定了计数器何时减一、地址何时步进。

OH! 给 DMA 设计了两种工作方式：

- **手动模式（manual mode）**：软件直接把所有寄存器（配置、步长、计数、源地址、目的地址）写好，然后拉 `dma_en`，DMA 直接进入搬运。**这是 RTL 里真正能跑通的主路径。**
- **链式 / 描述符模式（chain mode）**：软件只在内存里放一张「描述符表」，每条描述符记录一次搬运的全部参数；DMA 自己去内存把描述符取回来、装入寄存器，搬完一条再去取下一条。**这是 README 力推、但 RTL 尚未完成的一条支路。**

#### 4.1.2 核心流程

状态机定义在 [edma/hdl/edma_ctrl.v:65-74](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L65-L74)：

```
`define DMA_IDLE    4'b0000 // 空闲
`define DMA_FETCH0  4'b0001 // 取 config / next-ptr / stride_in
`define DMA_FETCH1  4'b0010 // 取 count / stride_out
`define DMA_FETCH2  4'b0011 // 取 srcaddr / dstaddr
`define DMA_FETCH3  4'b0100 // stall
`define DMA_FETCH4  4'b0101 // stall
`define DMA_INNER   4'b0110 // 内层搬运
`define DMA_OUTER   4'b0111 // 外层（2D）切换
`define DMA_DONE    4'b1000 // 完成
`define DMA_ERROR   4'b1001 // 错误
```

**手动模式主路径**（这是你该重点理解、也是实践里要走的路）：

```
IDLE ──(dma_en=1, manualmode=1)──► INNER ──► … ──► DONE
                                       │          ▲
                  (inner 计数到 0)      │          │
                                       ▼          │
                                     OUTER ────────┘
                                  (update 时回 INNER，
                                   或 outer 也到 0 时经 INNER→DONE)
```

简化成一句话：**内层每搬一次，计数减一；内层到 0 就跳外层，外层把外计数减一再回内层；内、外都到 0 就 DONE。**

**链式 / 描述符支路**（看懂意图即可，RTL 未完成）：

```
IDLE ──(dma_en=1, manualmode=0)──► FETCH0 ──► FETCH1 ──► FETCH2 ──► FETCH3 ──► …
                                                                              ↑
                                                                          （见 4.1.4）
```

#### 4.1.3 源码精读

**主状态机**（手动模式部分）在 [edma/hdl/edma_ctrl.v:76-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L76-L116)。关键几段：

IDLE 的出口用 `dma_en` 和 `manualmode` 二选一：

```verilog
`DMA_IDLE:
  casez({dma_en,manualmode})
    2'b0?  : dma_state <= `DMA_IDLE;   // 没使能，留在空闲
    2'b11  : dma_state <= `DMA_INNER;  // 使能 + 手动 → 直接搬运
    2'b10  : dma_state <= `DMA_FETCH0; // 使能 + 链式 → 去取描述符
  endcase
```

INNER → OUTER → DONE 的判决（[edma/hdl/edma_ctrl.v:99-107](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L99-L107)）：

```verilog
`DMA_INNER:
  casez({update,incount_zero,outcount_zero})
    3'b0?? : dma_state <= `DMA_INNER;              // 没更新：继续等
    3'b10? : dma_state <= `DMA_INNER;              // 更新了但内层没到 0：接着搬
    3'b110 : dma_state <= `DMA_OUTER;              // 内层到 0、外层还有：跳外层
    3'b111 : dma_state <= `DMA_DONE;               // 内外都到 0：完成
  endcase
`DMA_OUTER:
  dma_state <= update ? `DMA_INNER : `DMA_OUTER;   // 外层只停一拍，update 后回内层
```

注意 `incount_zero` / `outcount_zero` 判的是**组合算出的「下一拍 count」**（`count`），而不是当前寄存器值——这样状态切换和计数减一在同一拍对齐（[edma/hdl/edma_ctrl.v:133-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L133-L135)）：

```verilog
assign incount_zero  = ~(|count[15:0]);   // 内层「下一次」计数 == 0
assign outcount_zero = ~(|count[31:16]);  // 外层「下一次」计数 == 0
```

而「何时算完成一次事务」由 `update` 一锤定音（[edma/hdl/edma_ctrl.v:124](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L124)）：

```verilog
assign update = ~wait_in & (master_active | access_in);
```

读法：**没有反压（`~wait_in`）** 且 **（主机在搬 `master_active` 或 从机有访问 `access_in`）** 时，本拍算一次有效事务，触发 `update`。`update` 是整个 DMA 的心跳——它一跳，计数减一、地址步进、寄存器锁存同时发生。

`master_active` 把状态机和「主/从模式」绑在一起（[edma/hdl/edma_ctrl.v:128-130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L128-L130)）：

```verilog
assign master_active = mastermode &
                       ((dma_state==`DMA_INNER) | (dma_state==`DMA_OUTER));
```

即：只有在 INNER/OUTER 搬运阶段、且配置成主机模式时，DMA 才主动发起事务。从机模式下，`update` 靠 `access_in` 触发——来一个包才搬一下。

#### 4.1.4 代码实践：跟着状态机走一遍手动搬运

**实践目标**：用手动模式把状态机走一遍，确认 IDLE→INNER→OUTER→DONE 的跳转条件。

**操作步骤**（源码阅读 + 推演，仿真标注「待本地验证」）：

1. 设想软件按 u6-l1 的 `.vh` 范式写好了：`EDMA_CONFIG`（使能 + 手动模式）、`EDMA_STRIDE`、`EDMA_COUNT`、`EDMA_SRCADDR`、`EDMA_DSTADDR`。
2. 关注 `config_reg` 到 `manualmode` 的极性：[edma/hdl/edma_regs.v:143](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L143) 里 `manualmode = ~config_reg[3]`。README 把 bit[3] 叫 `STARTUP`（=1 表示「按 next_ptr 去取描述符」），所以 **`STARTUP=0`（即 `config_reg[3]=0`）才对应手动模式**。要让 IDLE 直跳 INNER，写 CONFIG 时 bit[3] 必须为 0。
3. 取一组小参数：`COUNT = {outer=2, inner=2}`（即 `0x0002_0002`），让状态机经历「内层到 0 → 外层 → 回内层 → 内外都到 0 → DONE」全过程。
4. 逐拍列出 `{dma_en, manualmode}`、`{update, incount_zero, outcount_zero}` 的取值，对照上面的 `casez` 推下一状态。

**需要观察的现象**：

- `dma_en=1, manualmode=1` 一满足，IDLE 立刻跳 INNER。
- 每次 `update` 为 1，内层计数减一。
- 内层减到 0 那拍，`incount_zero=1`：若 `outcount_zero=0` → OUTER；若 `outcount_zero=1` → DONE。
- OUTER 只待到下一个 `update`，立刻回 INNER。

**预期结果**：对 `{outer=2, inner=2}`，状态序列应为
`IDLE → INNER → INNER → OUTER → INNER → INNER → DONE`
（每个 INNER 代表一次有效搬运，两次 INNER 后内层归零进 OUTER）。**待本地验证**：因 `packet2emesh`/`emesh2packet` 在仓库无定义（见 4.2.4），模块无法直接编译，需在仿真平台补齐这些桩后再跑。

> 🔎 **链式支路的现实**：`DMA_FETCH3` 的下一状态是它自己——[edma/hdl/edma_ctrl.v:95-96](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L95-L96) 写的是 `reg_wait_in ? DMA_FETCH3 : DMA_FETCH3`，两条分支都指向 FETCH3，等于**死循环**；`DMA_FETCH4` 永远到不了。所以**描述符链这条支路在当前 RTL 里走不通**，学习时以手动模式为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `update` 里要 `~wait_in`？去掉会怎样？
> **答**：`wait_in` 是下游的反压（`wait=1` 表示「我还没准备好接收」）。若不判 `wait_in`，DMA 会在下游没接收的拍也减计数、步进地址，于是计数器和真实发出的事务对不上——少搬或多搬。`~wait_in` 保证「只在事务真正被接收的那一拍」才计数。

**练习 2**：`DMA_OUTER` 为什么不像 `DMA_INNER` 那样判 `incount_zero`，而是只等一个 `update` 就回 INNER？
> **答**：OUTER 的职责只是「在外层切换的那一拍，用 2D 方式更新计数」（见 4.2 的 `update2d`），它不发起额外的事务。等 `update` 一拍，是为了让 2D 计数更新与一次有效事务对齐；更新一完成就立刻回 INNER 继续搬下一行。

---

### 4.2 stride 数据通路（edma_dp）

#### 4.2.1 概念说明

如果说状态机是「指挥」，数据通路就是「算盘」。`edma_dp` 要算三件事，而且**全部是组合逻辑**（不给状态，只给「下一拍应该变成什么」）：

1. **count**：下一次更新后，内外层计数各是多少？
2. **srcaddr**：下一次读哪个源地址？
3. **dstaddr**：下一次写哪个目的地址？

它还兼一个「双面角色」：根据是主机还是从机，把进出的事务包在「DMA 自己发的读请求」和「转发外部来的写」之间切换。

#### 4.2.2 核心流程

数据通路的核心是 **stride 步进**。设源步长为 \(s_{src}\)（有符号），目的步长为 \(s_{dst}\)（有符号），则每完成一次事务：

\[
\text{srcaddr}_{\text{next}} = \text{srcaddr}_{\text{now}} + s_{src}
\]

\[
\text{dstaddr}_{\text{next}} = \text{dstaddr}_{\text{now}} + s_{dst}
\]

计数器则分两种情况：

\[
\text{count}_{\text{next}} =
\begin{cases}
\{\,\text{outer}-1,\ \text{inner}\,\} & \text{外层切换拍（update2d=1）} \\
\text{count}_{\text{now}} - 1 & \text{其它（内层）}
\end{cases}
\]

> 直觉：内层搬运时，整体减一（因为内层在低位，减一只动内层）；外层切换拍，把外层减一、内层保持。

**主/从切换**也很关键：

- **从机模式（slave）**：外部顺着 `access_in/packet_in` 送来一串写包，DMA 把每个包的 `data` 改写到「步进中的 `dstaddr`」。等于一个**带地址步进的写转发器**（scatter 写引擎）。`access_out` 跟随 `access_in`。
- **主机模式（master）**：DMA 自己在每个非反压拍拉高 `access_out`，主动发事务；`write_out=0`，即发**读请求**，地址用步进中的 `dstaddr`。`access_out <= access_in | master_active` 里的 `master_active` 项就是用来「在主机模式下持续产生访问」的。

#### 4.2.3 源码精读

**计数更新**（[edma/hdl/edma_dp.v:80-81](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L80-L81)）：

```verilog
assign count[31:0] = update2d ? {(count_reg[31:16] - 1'b1), count_reg[15:0]} :
                                count_reg[31:0] - 1'b1;
```

`update2d` 来自 ctrl（[edma/hdl/edma_ctrl.v:126](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L126)）：`update & (dma_state==DMA_OUTER)`，即「在外层那一拍的有效事务」。

**地址步进**（[edma/hdl/edma_dp.v:87-95](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L87-L95)）：

```verilog
assign srcaddr[AW-1:0] = srcaddr_reg[AW-1:0] +
                         {{(AW-16){stride_reg[15]}},  stride_reg[15:0]};  // 源步长
assign dstaddr[AW-1:0] = dstaddr_reg[AW-1:0] +
                         {{(AW-16){stride_reg[31]}},  stride_reg[31:16]}; // 目的步长
```

读法：`stride_reg` 是 32 位，**低 16 位是源步长、高 16 位是目的步长**，各自做 16→32 位的**符号扩展**（把符号位 `stride_reg[15]` / `stride_reg[31]` 填满高位）。这就是「有符号步长」能正能负的实现。

**主/从事务切换**（[edma/hdl/edma_dp.v:117-122](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L117-L122)）：

```verilog
assign write_out           = master_active ? 1'b0          : 1'b1;  // 主机发读，从机转发写
assign datamode_out[1:0]   = master_active ? datamode[1:0] : datamode_in[1:0];
assign ctrlmode_out[4:0]   = master_active ? ctrlmode[4:0] : ctrlmode_in[4:0];
assign dstaddr_out[AW-1:0] = dstaddr[AW-1:0];                        // 永远用步进地址
assign data_out[AW-1:0]    = master_active ? {(AW){1'b0}}  : data_in[31:0];
assign srcaddr_out[AW-1:0] = master_active ? {(AW){1'b0}}  : srcaddr_in[31:0];
```

注意 `dstaddr_out` **无条件**用数据通路算出的 `dstaddr`——无论主从，DMA 驱动到总线上的地址都是那个「按步长走出来的」地址；主从的差别只在 `write` 位、`data`、`srcaddr` 和 `datamode/ctrlmode` 的来源。

最后是**流水打拍**（[edma/hdl/edma_dp.v:142-152](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L142-L152)），目的纯粹是时序（注释写明 *FOR TIMING PURPOSES*）：

```verilog
always @ (posedge clk)
  if(~wait_in) packet_out <= packet;            // 组合包打一拍
always @ (posedge clk)
  if(~wait_in) access_out <= access_in | master_active;
assign wait_out = wait_in;                       // 反压直通
```

#### 4.2.4 代码实践：手算一次 2D 搬运的地址序列

**实践目标**：给定参数，按 `edma_dp` 的 stride 算式，列出每次事务的源/目的地址，并理解 1D 与 2D 的差别。

**给定参数**：

| 寄存器 | 值 | 说明 |
|--------|-----|------|
| `SRCADDR` | `0x0000_1000` | 源起始地址 |
| `DSTADDR` | `0x0000_2000` | 目的起始地址 |
| `COUNT` | `0x0003_0002` | 外层 3、内层 2（即 3 行 × 2 列） |
| `STRIDE` | `0x0004_0004` | 目的步长 +4（高 16 位）、源步长 +4（低 16 位），每次走一个 32 位字 |

**操作步骤**：

1. 拆 `STRIDE`：源步长 \(s_{src}=+4\)，目的步长 \(s_{dst}=+4\)。
2. 按「每完成一次事务，src += 4，dst += 4」逐次列出地址。下表给出**设计意图**（内层到 0 后，外层减一、内层重装为初始值 2，共 \(3\times2=6\) 次事务）：

| 事务 # | outer | inner | 读 src | 写 dst |
|:---:|:---:|:---:|:---:|:---:|
| 1 | 3 | 2 | `0x1000` | `0x2000` |
| 2 | 3 | 1 | `0x1004` | `0x2004` |
| — 内层到 0，切外层（outer 3→2，inner 重装 2） — | | | | |
| 3 | 2 | 2 | `0x1008` | `0x2008` |
| 4 | 2 | 1 | `0x100C` | `0x200C` |
| — 内层到 0，切外层（outer 2→1，inner 重装 2） — | | | | |
| 5 | 1 | 2 | `0x1010` | `0x2010` |
| 6 | 1 | 1 | `0x1014` | `0x2014` |
| — 内层到 0 且外层到 0 → DONE — | | | | |

**需要观察的现象 / 预期结果**：在「源步长 = 目的步长 = 一个字」时，6 次事务的地址其实是**线性连续**的（src 从 `0x1000` 走到 `0x1014`，dst 从 `0x2000` 走到 `0x2014`）。这说明：**当内外步长相同时，2D 退化成 1D**，只是计数方式不同。

**真正的 2D**（供理解，非本 RTL 完整支持）：要让矩阵按「行」搬运、行与行之间留间距，需要**两个不同的步长**——内层步长（行内逐字）与外层步长（从一行末尾跳到下一行开头），并在外层切换拍把 stride 寄存器换成外层那组。README 的 `EDMA_STRIDE` 说明文字描述的正是这种「内/外 stride 互换」机制。但**当前 `edma_dp` 只有一组 `stride_reg`，且 `update2d` 只改计数、不改 stride**（见 [edma/hdl/edma_dp.v:87-95](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L87-L95)），所以**非均匀步长的 2D 在本版本里并未实现**——这是「README 是愿景、RTL 才是事实」的一处典型落差。

> ⚠️ **计数重装的现实**：把 `COUNT=0x0003_0002` 代入 [edma/hdl/edma_dp.v:80-81](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L80-L81) 的算式逐拍算，外层切换拍得到的是 `{outer-1, inner[15:0]}` = `{0x0002, 0x0000}`——**内层并没有重装为初始值 2，而是留在 0**。回到 INNER 后 `count_reg=0x00020000`，`count = 0x00020000 - 1 = 0x0001FFFF`，内层变成 `0xFFFF` 而非归零重装。所以上表「inner 重装 2」是**设计意图**，**当前 RTL 的内层重装不成立、会导致内层回绕**，2D 终止行为与上表不一致。地址步进本身（src/dst += stride）是忠实实现的；**终止条件「待本地验证」**。

> 🔎 **编译性**：`edma_dp` 实例化了 `packet2emesh` / `emesh2packet`（[edma/hdl/edma_dp.v:102-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_dp.v#L102-L135)），它们的作用等价于 u5-l3 讲过的 `emesh_unpack`/`emesh_pack`，但**全仓库都找不到它们的 `module` 定义**（与 gpio/spi/emailbox 等模块遇到的「改名漂移」是同一类历史遗留）。因此 `edma` **不能原样编译**，本实践以「读源码 + 手算」为主，仿真一律标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：若想让源地址每步**后退** 4 字节（反向搬运），`STRIDE` 的低 16 位应写什么？整个 `STRIDE` 寄存器（假设目的步长仍为 +4）又该写什么？
> **答**：源步长 = −4，16 位补码为 `0xFFFC`。目的步长 +4 = `0x0004`。`STRIDE = {dst_stride, src_stride} = {0x0004, 0xFFFC} = 0x0004_FFFC`。符号扩展后源地址每拍减 4。

**练习 2**：为什么 `dstaddr_out` 不像 `data_out` 那样分主/从两路？
> **答**：无论主从，DMA 要驱动到总线上的「目标地址」都是它自己按步长算出来的 `dstaddr`——这是 DMA 的核心职能（决定写到/读到哪儿）。只有 `data`、`srcaddr`、`write` 这些「事务内容」才需要区分「来自外部包（从机）」还是「DMA 自己生成（主机）」。

**练习 3**：`access_out <= access_in | master_active` 里，`master_active` 这一项的作用是什么？
> **答**：在主机模式下（`master_active=1`），即便外部没有 `access_in`，DMA 也要在每个非反压拍主动拉高 `access_out` 来**自发产生**事务流；`| master_active` 就是把这个「自驱动」叠加进去。从机模式下 `master_active=0`，`access_out` 退化为跟随 `access_in`，外部来一个才转一个。

---

### 4.3 描述符链与寄存器映射（edma_regs + edma_regmap.vh）

#### 4.3.1 概念说明

这一节把两件事合起来讲，因为它们在 RTL 里紧耦合：

1. **寄存器映射**：DMA 有 8 个寄存器（配置、步长、计数、源/目的地址及其 64 位扩展、状态）。软件怎么访问它们？答案就是 u6-l1 的 `.vh` 范式——宏常量 + 地址位译码 + 写选通。
2. **描述符链**：把「一次搬运的全部参数」打包成内存里的一条记录（描述符），让 DMA 自己去取。链式（chain）就是取完一条、按 `next_ptr` 再取下一条，像链表一样串起来。

#### 4.3.2 核心流程

**地址映射**：8 个寄存器用 `dstaddr[6:2]`（5 位）译码，所以寄存器号 0–7 对应字节地址 `0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C`（步长 4 字节，因为低 2 位被忽略）。宏定义在 [edma/hdl/edma_regmap.vh:4-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regmap.vh#L4-L11)：

```
`define EDMA_CONFIG    5'd0  // 配置
`define EDMA_STRIDE    5'd1  // 步长
`define EDMA_COUNT     5'd2  // 计数
`define EDMA_SRCADDR   5'd3  // 源地址
`define EDMA_DSTADDR   5'd4  // 目的地址
`define EDMA_SRCADDR64 5'd5  // 源地址高 32 位（64b）
`define EDMA_DSTADDR64 5'd6  // 目的地址高 32 位（64b）
`define EDMA_STATUS    5'd7  // 状态
```

**CONFIG 位段**（[edma/README.md:36-60](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/README.md#L36-L60) 与 RTL [edma/hdl/edma_regs.v:140-147](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L140-L147) 对照）：

| 位 | 名 | RTL 信号 | 说明 |
|---|---|---|---|
| [0] | DMAEN | `dma_en` | 使能 DMA |
| [1] | MASTER | `mastermode` | 主机 / 从机 |
| [2] | CHAINMODE | `chainmode` | 完成后自动取下一条描述符 |
| [3] | STARTUP | `~manualmode` | =1 按 next_ptr 取描述符；=0 手动（注意取反） |
| [4] | IRQEN | `irqmode` | 完成中断使能 |
| [6:5] | DATASIZE | `datamode` | 00 字节 / 01 半字 / 10 字 / 11 双字 |
| [31:16] | NEXT_PTR | `next_descr` | 下一条描述符地址 |

**写选通**：标准三步——解出 `reg_write`、再与「地址等于某宏」比较、得到 one-hot 选通；选通驱动各 `always` 块更新寄存器。`update` 有效时，寄存器改锁存数据通路回送的「下一拍值」（形成 4.1 里说的反馈环）。

**描述符预取**（意图）：DMA 在 FETCH 状态向描述符所在内存发**读请求**，读回来的数据**作为写事务**回到自己的寄存器口——这是怎么把「读到的那一字」送进正确寄存器的？靠 emesh 读请求里的 `srcaddr`（回信地址）字段做路由（详见 4.3.3）。

#### 4.3.3 源码精读

**地址译码与写选通**（[edma/hdl/edma_regs.v:120-128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L120-L128)），与 u6-l1 范式完全一致：

```verilog
assign reg_write      = write_in & reg_access_in;
assign config_write   = reg_write & (dstaddr_in[6:2]==`EDMA_CONFIG);
assign stride_write   = reg_write & (dstaddr_in[6:2]==`EDMA_STRIDE);
assign count_write    = reg_write & (dstaddr_in[6:2]==`EDMA_COUNT);
assign srcaddr0_write = reg_write & (dstaddr_in[6:2]==`EDMA_SRCADDR);
... // 其余同理
```

**计数寄存器的「双输入」**（[edma/hdl/edma_regs.v:160-164](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L160-L164)）——这是反馈环的落点：

```verilog
always @ (posedge clk)
  if(count_write)            count_reg <= data_in;   // 软件写初值
  else if (update)           count_reg <= count;     // 搬运时锁存「下一拍值」
```

源/目的地址寄存器同理（[edma/hdl/edma_regs.v:170-188](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L170-L188)）：软件可写初值，`update` 时锁存步进后的地址。

**描述符预取的地址构造**（[edma/hdl/edma_ctrl.v:141-183](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L141-L183)）。FETCH0/1/2 分别读描述符的偏移 0/8/16，并用 `oh_mux3` 选出对应的「目标寄存器号」：

```verilog
oh_mux3 #(.DW(5)) mux3s (.out(reg_addr),
    .in0(`EDMA_CONFIG),  .sel0(dma_state==`DMA_FETCH0),
    .in1(`EDMA_COUNT),   .sel1(dma_state==`DMA_FETCH1),
    .in2(`EDMA_SRCADDR), .sel2(dma_state==`DMA_FETCH2));
```

然后把 `reg_addr` 塞进读请求的 `srcaddr`（回信地址）字段（[edma/hdl/edma_ctrl.v:167-183](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_ctrl.v#L167-L183)）：

```verilog
assign srcaddr_out = {{(AW-11){1'b0}}, ID, reg_addr[4:0], 2'b0};
...
emesh2packet e2p (.write_out(1'b0),            // 读请求
                  .dstaddr_out({...,fetch_addr}),   // 去描述符内存读
                  .srcaddr_out(srcaddr_out));        // 回信地址＝寄存器号
```

巧妙之处：emesh 读请求的 `srcaddr` 是「响应回哪儿」，所以被读的内存会把数据**按这个 srcaddr 路由回来**，于是 `dstaddr_in[6:2]` 正好等于目标寄存器号，自动落到 `config_write`/`count_write`/… 上。**一次预取 = 一条读请求 + 一条自动路由的回写**。

（注意 `oh_mux3` 是 one-hot 3 选 1，已在 u2-l1 / u3-l4 见过同类结构，定义见 [stdlib/rtl/oh_mux3.v:8-23](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux3.v#L8-L23)。）

#### 4.3.4 代码实践：把 test_basic.emf 逐行译成寄存器写

**实践目标**：用 u4-l2 / u5-l1 学过的 `.emf` 五段格式，把 `test_basic.emf` 的每一行翻译成「写哪个寄存器、写什么值」，并核对注释。

`.emf` 一行 = `srcaddr/datahi _ datalo(data) _ dstaddr _ ctrlmode _ access(delay)`；`ctrlmode` 的 bit0 是写位、bits[2:1] 是 datamode。

**操作步骤**：

1. 打开 [edma/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/dv/tests/test_basic.emf)。
2. 对每行，取 `dstaddr`，按 `dstaddr[6:2]`（即字节地址除以 4）对照 `edma_regmap.vh` 查寄存器号。
3. `ctrlmode=0x05 = 0b00000101`：bit0=1（写）、bits[2:1]=10（字，32 位）。

**逐行译码**（字节地址 → `[6:2]` → 寄存器）：

| 行 | dstaddr | `[6:2]` | 命中寄存器 | 写入值(datalo) | 文件注释 |
|:--:|:--:|:--:|:--|:--:|:--|
| 1 | `0x000000` | 0 | `EDMA_CONFIG` | `0x00000000` | WRITE CONFIG0 ✓ |
| 2 | `0x000008` | 2 | `EDMA_COUNT` | `0x00000000` | WRITE STRIDE0 ✗ |
| 3 | `0x000010` | 4 | `EDMA_DSTADDR` | `0x00000000` | WRITE COUNT ✗ |
| 4 | `0x000018` | 6 | `EDMA_DSTADDR64` | `0x00000000` | WRITE SRCADDR0 ✗ |
| 5 | `0x000020` | 8 | **（无，超出 0–7）** | — | WRITE DSTADDR0 ✗ |
| 6 | `0xFFF00000` | — | 数据通路（master/slave） | `0x44332211_88776655` | WRITE 64-BIT PACKET |

**需要观察的现象 / 预期结果**：

- 第 1 行注释与译码**一致**（写 CONFIG）。
- 第 2–5 行注释与实际译码**全部对不上**：例如第 2 行地址 `0x08` 译码到 `EDMA_COUNT`，注释却写「STRIDE0」（STRIDE 应在 `0x04`）。
- 第 5 行地址 `0x20` → `[6:2]=8`，**根本不落在任何寄存器**（regmap 最大是 7），这次写不会更新任何寄存器。
- 第 6 行 `dstaddr=0xFFF00000`（高位非零）走的是数据通路（参见 [edma/dv/dut_edma.v:67-71](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/dv/dut_edma.v#L67-L71) 里「高位非零 → edma_access_in，高位全零 → reg_access_in」的拆分），是一次真实的数据事务。

**结论**：`test_basic.emf` 是一个**骨架/占位**激励——地址与注释脱节、甚至越界。这再次印证 `edma` 处于施工区，**不要把它的注释当事实，要以 regmap + 译码逻辑为准**。

> 🔎 **几处「README 有、RTL 无」的功能**（均见 `edma_regs.v`）：
> - **中断**：README 列「Interrupt on completion」，但 [edma/hdl/edma_regs.v:218](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L218) 是 `assign irq = 1'b0;`——中断**恒为 0，未实现**。
> - **读回**：[edma/hdl/edma_regs.v:208](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L208) 注释 `TODO: no readback for now`，`reg_wait_out=0`。`reg_packet_out` 当前只透传 `fetch_packet`（[edma/hdl/edma_regs.v:211-213](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma_regs.v#L211-L213)），软件尚不能读回配置寄存器。

#### 4.3.5 小练习与答案

**练习 1**：要让 DMA 走「手动模式 + 主机模式 + 字传输」，CONFIG 应写多少（只考虑用到 的位）？
> **答**：bit[0] DMAEN=1，bit[1] MASTER=1，bit[3] STARTUP=0（手动），bit[6:5] DATASIZE=10（字）。其余位 0。所以 `CONFIG = 0b 0000_0000_0000_0000_0000_0000_1000_0011 = 0x00000083`（即 `0x83`）。注意 bit[3]=0 才是手动模式。

**练习 2**：描述符预取里，为什么把「目标寄存器号」放进读请求的 `srcaddr` 字段，而不是 `dstaddr`？
> **答**：`dstaddr` 是「去哪儿读」（描述符所在的内存地址），必须留给被访问的内存。emesh 的读请求用 `srcaddr` 表「响应回哪儿」——把它设成「寄存器号编码的地址」，被读内存回写时就会按这个地址路由，使响应数据的 `dstaddr[6:2]` 恰好等于目标寄存器号，自动触发对应的写选通。一举两得。

**练习 3**：`count_reg` 的 `always` 块里，`count_write` 的优先级高于 `update`。为什么？
> **答**：软件写初值是一次性配置，必须能覆盖硬件的自动递减。若 `update` 优先，软件写下去的初值会马上被下一拍的「下一拍值」冲掉。把 `count_write` 放在 `if` 的最高优先级，保证「软件配置那一刻」初值稳稳落进寄存器，随后的 `update` 才在此基础上递减。

## 5. 综合实践

**任务**：在纸上为 `edma` 设计并「运行」一次完整的**手动模式、从机模式**搬运，把本讲三块知识串起来。

**设定**：

- 软件依次写：`EDMA_CONFIG = 0x00000081`（DMAEN=1, MASTER=0 从机, STARTUP=0 手动, DATASIZE=10 字；说明各 bit 含义）。
- `EDMA_STRIDE = 0x00080008`（源步长 +8、目的步长 +8，即每次跳两个字）。
- `EDMA_COUNT = 0x00010004`（外层 1、内层 4，即一维 4 次事务——故意取 outer=1，避开 4.2.4 里 2D 重装的坑）。
- `EDMA_SRCADDR = 0x00001000`、`EDMA_DSTADDR = 0x00002000`。

**要你完成**：

1. **译码**：把上面 5 个写事务写成 5 行 `.emf`（参考 4.3.4 的格式；`ctrlmode` 用 `0x05`=写字）。注意每个寄存器的字节地址要按 `regmap` 算对（`0x00,0x04,0x08,0x0C,0x10`）。
2. **画状态序列**：根据 4.1，列出从 IDLE 到 DONE 的状态跳转序列，标注每次 `update` 发生的拍。
3. **手算地址序列**：根据 4.2，列出 4 次事务各自的「写 dstaddr」，以及每次事务后 `dstaddr_reg` 的新值（应从 `0x2000` 按 +8 递增到 `0x2020`）。
4. **对照检验**：因为 outer=1，内层到 0 时 `outcount_zero` 也为 1，应直接 INNER→DONE，**不进 OUTER**。请验证你的状态序列里确实没有 OUTER。
5. **反思**：写出本次实践中你发现的「README 与 RTL 不一致」之处至少两处（提示：中断、读回、`packet2emesh`、`.emf` 注释）。

**预期结果（自检）**：4 次事务的 `dstaddr` 依次为 `0x2000, 0x2008, 0x2010, 0x2018`；状态序列为 `IDLE → INNER → INNER → INNER → INNER → DONE`；第 5 次回到 IDLE/保持 DONE（取决于是否清 `dma_en`）。**全部「待本地验证」**——在仿真平台补齐 `packet2emesh`/`emesh2packet` 桩后才可实跑。

## 6. 本讲小结

- `edma` 由三块组成：**`edma_ctrl`（状态机）、`edma_dp`（数据通路）、`edma_regs`（寄存器堆）**，顶层 `edma.v` 只把它们拼线；三者通过「组合算下一拍 + `update` 选通锁存」形成反馈环。
- **状态机**的主路径是 `IDLE → INNER → OUTER → DONE`，心跳信号是 `update = ~wait_in & (master_active | access_in)`；`master_active` 把主/从模式与状态绑定。
- **stride 数据通路**用符号扩展的 16 位步长更新源/目的地址，`stride_reg` 低 16 位是源步长、高 16 位是目的步长；`update2d` 只改计数（外层减一）不改步长。
- **寄存器映射**沿用 u6-l1 的 `.vh` 范式：`dstaddr[6:2]` 译 8 个寄存器，`4` 字节步长；CONFIG 的 bit[3] 取反才是 `manualmode`。
- **描述符链**靠 emesh 读请求的 `srcaddr`（回信地址）做路由，把读到的一字自动写进目标寄存器；但当前 RTL 的 FETCH 支路有 `FETCH3→FETCH3` 死循环，**链式不可用**，学习以手动模式为主。
- **现实落差**：中断 `irq` 恒为 0、读回未实现、`packet2emesh`/`emesh2packet` 仓库无定义导致**无法原样编译**、2D 内层重装不成立、`.emf` 注释与地址脱节——一律以 RTL 与 regmap 为准。

## 7. 下一步学习建议

- **向系统互连走**：本讲的 emesh 包会被 `axi_elink`（u8-l2）拿到 AXI 总线上去。建议接着读 [edma/hdl/edma.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/hdl/edma.v) 与 [axi/hdl/emaxi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/axi/hdl/emaxi.v)，理解 DMA 发出的 emesh 事务如何经 AXI 桥访问片外 DDR。
- **向链路走**：DMA 的写请求若要送到对端 Epiphany 芯片，会经 `elink`（u7）串行化。可对比「DMA 产生事务的速率」与「elink TX 通路（u7-l2）的反压机制」，理解 `wait_in` 是怎样一路传回 DMA 的。
- **补仿真**：若想真正跑通 `edma`，需要先在仿真库里补上 `packet2emesh`/`emesh2packet` 的桩（等价于 u5-l3 的 `emesh_unpack`/`emesh_pack`），或直接把它们替换成现有的 `emesh_unpack`/`emesh_pack` 并对齐端口名——这是一个很有价值的二次开发练习。
- **延伸阅读**：[edma/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/edma/README.md) 的 `EDMA_STRIDE` 一节描述了完整的「内/外 stride 互换」2D 机制，可对照本讲 4.2 指出的「当前 RTL 只有单 stride」，思考要怎样改动 `edma_dp` 与 `edma_regs` 才能实现 README 描述的完整 2D。
