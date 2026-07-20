# 核心 FSM：双进程状态机

## 1. 本讲目标

本讲带你钻进 `vivadoIP_axi_mm_reader` 的「大脑」——核心 RTL `hdl/axi_mm_reader.vhd` 里的有限状态机（FSM）。学完后你应该能够：

- 看懂并默写出 **双进程（two-process）方法** 的代码骨架，理解为什么用 record 把所有寄存器打包在一起。
- 画出 **Idle / ReadAddr / SetCmd / ApplyCmd / WaitDone** 五个状态的转移图，并讲清每个状态做什么、在什么条件之间跳转。
- 解释 FSM 如何在 `Trig`/超时启动后，**逐条遍历 RegTable**、经 AXI 主机发起一串单拍读事务。
- 说清 **DoneCnt 计数** 与 **组合逻辑 Last 检测** 是如何标记「最后一个读回字」并产生 `DoneIrq` 的。
- 回答本讲的核心思考题：为什么在 `ApplyCmd_s` 等待命令握手时，`RamAddr` 已经「领先一项」指向了下一个寄存器。

本讲只讲核心 FSM 本身；「触发/超时如何产生 `Start` 脉冲」的细节留给下一讲（u2-l4），AXI 主机/从机的接口细节在 u2-l5、u2-l6。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自 u2-l1、u2-l2）：

- **IP 的黑盒行为**：软件经 `s00_axi` 配置好「要读哪些寄存器」（RegTable）和「本轮读几个」（`RegCnt`）；`Trig` 或超时启动一次读周期；核心经 `m00_axi` 把这批 32 位寄存器逐个读回，结果进内部 FIFO，再按 AXIS / AXIMM 两种模式输出；`DoneIrq` 在读周期完成时给出一拍脉冲。
- **寄存器地图**：`RegCnt`（本轮读个数）、`Addr[]`（RegTable，从 `0x20` 起的内存区）、`RdData`/`RdLast`（AXIMM 模式下的 FIFO 出口）等。
- **核心 + wrapper 分层**：本讲的 `axi_mm_reader.vhd` 是**纯逻辑核心**，它不懂真正的 AXI4，只懂一套简化的 **IPIC 握手**（命令通道 `CmdRd_*`、数据通道 `RdDat_*`）。真正的 AXI4 协议在 wrapper（`axi_mm_reader_wrp.vhd`）里由 `psi_common_axi_master_simple` 翻译。本讲关注核心一侧的 IPIC 信号。

下面几个术语会反复出现，先约定清楚：

| 术语 | 含义 |
|------|------|
| **FSM** | Finite State Machine，有限状态机。本讲用 5 个状态描述「一次读周期」的推进过程。 |
| **two-process / 双进程** | 一种 VHDL 编码风格：用一个组合进程算「下一拍状态」，用一个时序进程把它「打进寄存器」。 |
| **record（记录）** | VHDL 把多个信号字段打包成一个结构体类型，方便整体传递/寄存。 |
| **IPIC** | 核心与 wrapper 之间的简化握手接口（`CmdRd` 发命令、`RdDat` 收数据）。 |
| **握手（Vld/Rdy）** | valid/ready 同时为 1 的那一拍，一次数据/命令传输才真正发生。 |

## 3. 本讲源码地图

本讲几乎只看一个文件，但会顺带提一句它周边的连线：

| 文件 | 在本讲中的作用 |
|------|----------------|
| [hdl/axi_mm_reader.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd) | **唯一主角**。包含双进程 record、FSM、DoneCnt/Last、RegTable RAM 与读数据 FIFO 的实例化。 |
| hdl/definitions_pkg.vhd | 提供 `MemOffs_c` 等常量；本讲会用到核心端口 `RegCount` 的来源概念（在 u2-l2 已讲）。 |
| tb/top_tb.vhd | 自校验测试台。本讲会用它的断言来「验证」我们对 FSM 行为（尤其 `Last`）的理解。 |

> 提示：本讲引用的行号都基于当前 HEAD `ca5ef76`。每段关键代码都会给出永久链接，点进去即可对照阅读。

## 4. 核心概念与源码讲解

### 4.1 双进程（two-process）方法与状态记录

#### 4.1.1 概念说明

写 FSM 时，新手常犯的错是把「算下一拍逻辑」和「把结果存进寄存器」混在一个 `process(clk)` 里，导致代码又长又容易漏掉某个分支的赋值，综合出意外的锁存器或寄存器。

**双进程方法**把这两件事彻底拆开：

1. **组合进程 `p_comb`**：纯组合逻辑。输入是「当前所有寄存器的值 `r`」加「所有外部输入」，输出是「下一拍的值 `r_next`」。它内部用一个局部变量 `v`，开头先 `v := r;`（整体复制），然后只改写需要变化的字段——没提到的字段自动「保持」。这样从根本上杜绝了锁存器，也让代码读起来像一段顺序程序。
2. **时序进程 `p_seq`**：只做一件事——在时钟上升沿把 `r_next` 打进 `r`，外加复位。它几乎不含业务逻辑。

把所有寄存器**打包进一个 record**，是这套风格的精髓：新增一个寄存器，只要在 record 里加一个字段、在 `p_comb` 里给它赋值即可，不用单独声明 `signal`，也不用维护一长串敏感量表。

#### 4.1.2 核心流程

```
        ┌──────────────── p_comb（组合进程）────────────────┐
当前 r ─►│  v := r;            // 整体复制，默认「保持」       │
输入   ─►│  修改 v 的若干字段   // 业务逻辑                    │──► r_next
        │  r_next <= v;                                      │
        └─────────────────────────────────────────────────────┘

        ┌──────────────── p_seq（时序进程）──────────────────┐
   Clk ─►│  if rising_edge(Clk) then r <= r_next;             │──► r（回到 p_comb 输入）
   Rst ─►│      if Rst='1' then 复位个别字段; end if;          │
        └─────────────────────────────────────────────────────┘
```

关键性质：

- `r` 是「当前拍」，`r_next`（即 `v`）是「下一拍」。`p_comb` 里读到的是 `r`（已经寄存过的稳定值），写的是 `v`。
- `v := r` 之后只覆盖必要字段 → 任何状态里没被赋值的字段都**保持上拍值**。
- 复位只显式处理「上电后必须立刻确定的字段」（状态机初态、握手信号等），其余字段交给 FSM 逻辑在运行中初始化，节省复位逻辑。

#### 4.1.3 源码精读

先看状态类型定义——整个 FSM 只有 5 个状态：

[hdl/axi_mm_reader.vhd:74](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L74) —— 定义 FSM 的 5 个状态：`Idle_s / ReadAddr_s / SetCmd_s / ApplyCmd_s / WaitDone_s`。

再看把所有寄存器打包在一起的 record：

[hdl/axi_mm_reader.vhd:77-88](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L77-L88) —— `two_process_r` 把超时计数器、`Start` 脉冲、`Fsm` 状态、RAM 读指针 `RamAddr`、命令通道地址/有效信号、本轮要读的个数 `RegCount`、已收到数据计数 `DoneCnt`、完成中断 `DoneIrq` 全部塞进一个 record；并声明 `signal r, r_next : two_process_r;`。

> 注意：这里的 `RegCount` 是 record 里的字段，是「`Idle_s` 时把端口 `RegCount` 拍进来」的快照（见 4.2.3）。它和实体端口 `RegCount` 同名，在 `p_comb` 里用 `r.RegCount` 指字段、用裸 `RegCount` 指端口，读代码时要分清。

`p_comb` 的骨架（开头 `v := r`、结尾 `r_next <= v`）：

[hdl/axi_mm_reader.vhd:100-104](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L100-L104) —— 组合进程敏感于 `r` 与所有外部输入；进程体第一行 `v := r;` 实现「默认保持」。

时序进程 `p_seq` 极其精简，只做寄存与复位：

[hdl/axi_mm_reader.vhd:190-201](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L190-L201) —— 上升沿把 `r_next` 打入 `r`；同步复位只强制 4 个字段（`TimeoutCnt=0`、`Start='0'`、`Fsm=Idle_s`、`AxiM_CmdRd_Vld='0'`），其余字段不在此复位——因为它们会被 `Idle_s` 等状态在运行中初始化。

> 为什么 `AxiM_CmdRd_Vld` 必须复位、而 `RamAddr`/`DoneCnt` 不用？因为复位退出后 FSM 在 `Idle_s`，此时**不能**向 AXI 主机残留一个有效的命令（`Vld` 必须为 0），否则会误发一次读；而 `RamAddr`/`RegCount`/`DoneCnt` 会在 `Idle_s` 被显式赋值（见 4.2.3），所以无需复位。

#### 4.1.4 代码实践

**实践目标**：用「改一处」体会双进程方法的好处——给核心新增一个寄存器化的内部信号。

1. **阅读** [hdl/axi_mm_reader.vhd:77-88](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L77-L88)，数一下 record 里一共有几个字段、分别属于「控制/状态/握手」哪一类。
2. **思考改动**：假设你想加一个「本周期已发出的命令数」寄存器 `CmdCnt`。在双进程风格下，你只需要做两件事：
   - 在 `two_process_r` 里加一行 `CmdCnt : integer range 0 to MaxRegCount_g;`；
   - 在 `p_comb` 里合适的位置（比如 `SetCmd_s`）写 `v.CmdCnt := r.CmdCnt + 1;`，并在 `Idle_s` 重置它。
   - **不需要**新建 `signal`，**不需要**改 `p_seq`，**不需要**改敏感量表（`r` 已整体敏感）。
3. **观察现象**：对比「传统写法（每个寄存器一个 `signal` + 一个独立进程）」，体会这套风格在增删寄存器时的省事之处。
4. **预期结果**：你能口头描述出「新增一个寄存器字段」的最小改动清单（record 一处 + `p_comb` 一处）。
5. 本步骤为源码阅读型实践，**待本地验证**（若你手头有仿真环境，可真正加上字段、重新跑 `sim/run.tcl` 看是否仍 PASS）。

#### 4.1.5 小练习与答案

**练习 1**：`p_comb` 里开头为什么一定要写 `v := r;`？如果不写会怎样？
**答案**：`v := r` 让所有字段默认「保持上拍值」。不写的话，凡是在某个分支里没被赋值的字段，综合器会推断成锁存器（latch）来「记住」旧值——这是组合进程里最经典的 bug 来源，会带来时序混乱和面积浪费。

**练习 2**：`p_seq` 里 `if Rst='1'` 的复位分支只改了 4 个字段。请判断 `r.DoneIrq` 复位后是什么值，为什么不会被综合成不确定？
**答案**：`DoneIrq` 没有显式复位，但它在 `p_comb` 每拍开头被 `v.DoneIrq := '0';`（见 4.3.3）强制默认为 0，只有在完成跳转那一拍才被置 1。所以复位后进入 `Idle_s` 的第一拍，`DoneIrq` 立刻被刷成 0，不会是亚稳态/不确定值。

---

### 4.2 FSM 状态机：一次读周期如何遍历 RegTable

#### 4.2.1 概念说明

一次「读周期」要做的事可以概括成一句话：**把 RegTable 里前 `RegCnt` 个地址，逐个交给 AXI 主机去读，等所有数据回来，发一个 `DoneIrq`。**

这看似简单，但要处理几个细节：

- 命令是**单拍事务**（一次发一个地址、读回一个 32 位字），所以核心要**逐条遍历** RegTable。
- 命令握手（`CmdRd_Vld`/`CmdRd_Rdy`）可能被主机**反压**，FSM 要能「原地等」。
- 命令发完 ≠ 数据到齐：AXI 读数据是**异步**经 FIFO 回来的，必须单独数「收齐了几个」才知道周期结束。

FSM 用 5 个状态把这些事拆开：`Idle_s`（待命）、`ReadAddr_s`（推进读指针 + 判完成）、`SetCmd_s`（把 RAM 里读到的地址装上命令通道）、`ApplyCmd_s`（等命令被主机接走）、`WaitDone_s`（等数据收齐发中断）。

#### 4.2.2 核心流程

主循环（每读一个寄存器走一圈）与退出路径如下：

```
   Enable=1 且 (Trig 或 超时) 产生 Start 脉冲
                 │
                 ▼
              ┌───────┐  RamAddr = 0..RegCount-1 (命令未发完)
              │ Idle  │ ─────Start────► ┌─────────────┐  RamAddr≠RegCount  ┌────────┐  无条件  ┌──────────┐
              │  _s   │                 │ ReadAddr_s  │ ────────────────► │ SetCmd │ ──────► │ApplyCmd  │
              └───┬───┘                 └──────┬──────┘                    └────────┘         │  _s      │
   DoneIrq=1 ▲   │ DoneCnt=RegCount           │ RamAddr=RegCount                            │ Rdy=0:  │
   (一拍)    │   │ (数据收齐)                 │ (命令已全部发出)                            │ 自循环等│
              │   ▼                            ▼                                            │ Rdy=1:  │
              │ ┌──────────┐  否则自循环等      │                                            ▼──Vld=0──┘
              └─│ WaitDone │ ◄────────────────┘                                  └──────► 回到 ReadAddr_s
                │  _s      │
                └──────────┘
```

文字版状态转移表（条件均看「当前拍 `r`」，副作用写在「下一拍 `v`」上）：

| 当前态 | 跳转条件 | 下一态 | 关键副作用 |
|--------|----------|--------|------------|
| `Idle_s` | `r.Start='1'` | `ReadAddr_s` | `RamAddr←0`；`RegCount←端口RegCount`；`DoneCnt←0`（由 Done 块） |
| `Idle_s` | `r.Start='0'` | `Idle_s` | 每拍持续 `RamAddr←0`、采样 `RegCount` |
| `ReadAddr_s` | `RamAddr ≠ RegCount` | `SetCmd_s` | `RamAddr←RamAddr+1` |
| `ReadAddr_s` | `RamAddr = RegCount` | `WaitDone_s` | `RamAddr←RamAddr+1`（此值之后不再使用） |
| `SetCmd_s` | （无条件） | `ApplyCmd_s` | `AxiM_CmdRd_Addr←RamRegAddr`；`AxiM_CmdRd_Vld←'1'` |
| `ApplyCmd_s` | `AxiM_CmdRd_Rdy='1'` | `ReadAddr_s` | `AxiM_CmdRd_Vld←'0'` |
| `ApplyCmd_s` | `AxiM_CmdRd_Rdy='0'` | `ApplyCmd_s` | 保持 `Vld='1'`，原地等 |
| `WaitDone_s` | `DoneCnt = RegCount` | `Idle_s` | `DoneIrq←'1'`（一拍脉冲） |
| `WaitDone_s` | `DoneCnt ≠ RegCount` | `WaitDone_s` | 原地等数据 |

另外有一条**全局覆盖规则**（详见 u2-l4）：只要 `Enable='0'`，无论当前在哪个状态，都立刻被拉回 `Idle_s` 并清零超时计数器。

#### 4.2.3 源码精读

触发逻辑产生 `Start` 单拍脉冲（细节在 u2-l4 展开，这里只需知道 `Start` 是启动信号）：

[hdl/axi_mm_reader.vhd:106-115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L106-L115) —— 每拍先把 `v.Start` 默认置 0；当 `Trig='1'` 或超时计数到顶，且 `Enable='1'` 时，才置 `Start='1'` 并清零超时计数器。

FSM 的状态机本体——这是本讲最该逐行读的段落：

[hdl/axi_mm_reader.vhd:117-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L117-L159) —— `case r.Fsm is` 五个状态分支 + 末尾的 `Enable='0'` 全局覆盖。

逐态拆开看：

- [hdl/axi_mm_reader.vhd:120-125](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L120-L125) `Idle_s`：每拍把读指针 `RamAddr` 归零、把端口 `RegCount` 拍进 record；只有 `r.Start='1'` 才跳到 `ReadAddr_s`。**注意判进用的是 `r.Start`（当前拍已寄存的值），所以从 `Start` 产生到真正跳走有一拍对齐**。
- [hdl/axi_mm_reader.vhd:128-135](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L128-L135) `ReadAddr_s`：先判断 `RamAddr` 是否已等于 `RegCount`（命令发完了？），决定走 `WaitDone_s` 还是 `SetCmd_s`；**无论走哪条，都把 `RamAddr` 自增 1**。这就是「指针领先一项」的来源（见 4.2.4）。
- [hdl/axi_mm_reader.vhd:137-140](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L137-L140) `SetCmd_s`：把 RAM 输出 `RamRegAddr` 装到命令地址、拉高 `Vld`，无条件进 `ApplyCmd_s`。
- [hdl/axi_mm_reader.vhd:142-146](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L142-L146) `ApplyCmd_s`：等 `AxiM_CmdRd_Rdy='1'`；握手成功的那拍把 `Vld` 拉低、回到 `ReadAddr_s` 处理下一个。
- [hdl/axi_mm_reader.vhd:148-152](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L148-L152) `WaitDone_s`：命令都发完了，但数据可能还在回来。这里**只看 `DoneCnt`**：等到 `DoneCnt = RegCount`（数据收齐）才置 `DoneIrq='1'` 并回 `Idle_s`。

末尾的全局覆盖（`Enable='0'` 强制回 `Idle_s`）在 [hdl/axi_mm_reader.vhd:156-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L156-L159)，它写在 `case` 之后，所以优先级最高。

> **为什么 `WaitDone_s` 要单独存在？** 因为「命令发完」和「数据到齐」在 AXI 上是两件事。命令是核心主动发的（`SetCmd`/`ApplyCmd` 一拍一个），但读回数据要经主机、经 FIFO，可能滞后若干拍才到。`DoneCnt` 专门数数据（见 4.3），所以 FSM 必须在 `WaitDone_s` 等到「命令数 = 收到的数据数」才算周期真正结束。这也是为什么 `DoneIrq` 永远不会在数据没到齐时提前拉高。

#### 4.2.4 代码实践（本讲核心思考题之一）

**实践目标**：亲手跟踪一次读周期，把「`RamAddr` 为什么领先一项」想透。

设 `RegCnt=2`（本轮读 2 个寄存器），`RegTable[0]=A`、`RegTable[1]=B`，并假设 AXI 主机立刻就绪（`AxiM_CmdRd_Rdy='1'`）。RegTable 是一片 **`psi_common_tdp_ram`**，B 端口地址由 `r.RamAddr` 驱动、输出 `RamRegAddr` 在**地址给出后的下一拍**稳定（寄存输出）：

[hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223) —— RegTable 用双端口 RAM 实现：A 口供软件写配置（`RegCfg_*`），B 口供核心读（`AddrB <= r.RamAddr`，`DoutB => RamRegAddr`，只读 `WrB=>'0'`）。

按下表逐拍填写（已给出关键列），核对「正在被读出命令的项」与「`RamAddr` 当前值」的关系：

| 拍 | `r.Fsm` | `r.RamAddr` | `RamRegAddr`(本拍) | 对外命令 `AxiM_CmdRd_Addr/Vld` | 说明 |
|----|---------|-------------|--------------------|--------------------------------|------|
| C1 | `Idle_s` | 0 | A | — | `r.Start=1`，准备出发 |
| C2 | `ReadAddr_s` | 0 | A | — | 0≠2，下一拍去 `SetCmd_s`；`RamAddr`→1 |
| C3 | `SetCmd_s` | 1 | **A** | 装载 `A`，`Vld` 下拍起 | RAM 输出仍是 A（地址 0 的滞后结果） |
| C4 | `ApplyCmd_s` | **1** | B | `Vld=1`，地址=`A`，被接走 | **注意：此刻 `RamAddr` 已是 1，但命令发的是第 0 项 A** |
| C5 | `ReadAddr_s` | 1 | B | — | 1≠2；`RamAddr`→2 |
| C6 | `SetCmd_s` | 2 | **B** | 装载 `B` | RAM 输出已是 B（地址 1 的滞后结果） |
| C7 | `ApplyCmd_s` | **2** | (下一项) | `Vld=1`，地址=`B`，被接走 | `RamAddr` 已是 2，命令发的是第 1 项 B |
| C8 | `ReadAddr_s` | 2 | — | — | 2=2，转 `WaitDone_s` |

**要观察的现象 / 设计意图**：

1. 在 `ApplyCmd_s` 等待握手的那一拍（C4、C7），`RamAddr` **已经指向「下一个」要读的项**（C4 时 `RamAddr=1`，C7 时 `RamAddr=2`），而当前正在发送的命令却是「当前」项（A、B）。
2. 之所以能这么做，是因为 RegTable RAM 的读出有**一拍延迟**：`ReadAddr_s` 里先自增 `RamAddr`，正好让 RAM 在下一个 `SetCmd_s` 拍把「下一项地址」送到 `RamRegAddr`。
3. **设计意图**：这是一种小型流水线「预读」。若不自增在前，每读完一项都要额外花一拍去递增指针、再等 RAM 出数，每个寄存器会多一个气泡（bubble）周期。提前自增 + 利用 RAM 的寄存输出，让「发命令」这一环节做到「几乎一项一命令」，把遍历 RegTable 的开销压到最低。即便 `ApplyCmd_s` 因主机反压多等几拍，`RamAddr` 静止、RAM 输出稳定，下一项地址也始终就绪，握手一通就能立刻续上。
4. **预期结果**：你能对着上表讲清「为什么 `SetCmd_s` 里用的是 `RamRegAddr` 而不是直接用 `r.RamAddr` 当地址」——因为 `RamRegAddr` 才是真正查表得到的、要读的目标寄存器地址；`r.RamAddr` 只是 RegTable 的索引，而且已经领先了一项。

> 本实践为源码阅读 + 推理型，**待本地验证**：若有仿真环境，可在 `tb/top_tb.vhd` 的「Single Reg Read Four Times」（用例 6，`RegCnt=1`）或默认的 14 寄存器用例里加波形，观察 `RamAddr` 与 `AxiM_CmdRd_*` 的相对节拍。

#### 4.2.5 小练习与答案

**练习 1**：如果端口 `RegCount`（`RegCnt`）配成 0，FSM 会怎样？会卡死吗？
**答案**：不会卡死。`Idle_s` 拍进 `RegCount=0`；到 `ReadAddr_s` 时 `RamAddr(0) = RegCount(0)` 成立，直接转 `WaitDone_s`，一条命令也不发；而 `DoneCnt` 初值已是 0 且等于 `RegCount`，于是当拍就满足 `DoneCnt=RegCount`，置 `DoneIrq` 回 `Idle_s`。即「读 0 个寄存器」=一次空周期、一个完成脉冲。

**练习 2**：读周期进行中又来了一个 `Trig` 脉冲，会重新开始吗？由哪段代码保证？
**答案**：不会。`Start` 只在 `Idle_s` 被检查（[hdl/axi_mm_reader.vhd:120-125](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L120-L125) 的 `if r.Start='1'`），其它四个状态都不看 `Start`，所以中途的 `Trig` 无法把 FSM 拽回起点。这与文档「Trigger input pulses while a read cycle is still going on are ignored」一致。

**练习 3**：`SetCmd_s` 到 `ApplyCmd_s` 是无条件跳转，为什么还要单独分一个 `ApplyCmd_s` 状态？
**答案**：因为命令握手可能要等好几拍。`SetCmd_s` 负责「装命令、拉 `Vld`」（一拍搞定），`ApplyCmd_s` 负责「等 `Rdy`」。如果合并成一个状态，要么得在装命令的同一拍就要求 `Rdy=1`（无法容忍反压），要么逻辑混在一起难以阅读。拆开后语义清晰：「装载」与「等待握手」各司其职。

---

### 4.3 DoneCnt 计数与组合逻辑 Last 检测

#### 4.3.1 概念说明

读周期的「命令侧」由 FSM 推进，「数据侧」则几乎旁路了 FSM：AXI 主机把读回的 32 位字直接写进核心内部的 **读数据 FIFO**。FSM 唯一关心数据侧的，就是**「这一周期该回来的字都回来了吗？」**

为此核心维护一个计数器 `DoneCnt`：每收到一个有效字（`AxiM_RdDat_Vld='1'` 且 FIFO 可写 `Fifo_Rdy='1'`，即一次成功的 valid/ready 握手）就加 1。当 `DoneCnt` 追上 `RegCount` 时，`WaitDone_s` 就判定周期完成、发 `DoneIrq`。

除此之外，还要在数据流里**标记「最后一个字」**（AXI-Stream 的 `TLAST`，或 AXIMM 模式下的 `RdLast`）。核心用一个**纯组合逻辑**信号 `Last`：当 `DoneCnt` 已收到 `RegCount-1` 个字（即「下一个将到位的就是第 N 个、也就是最后一个」）时拉高。这个 `Last` 位与数据一起写进 FIFO 的第 33 位，随数据一起输出。

#### 4.3.2 核心流程

数据通路（与 FSM 并行）：

```
AXI 主机读回字 ──► AxiM_RdDat_Data(32) ──┐
                                         ├──► FIFO InData(32:0) ──► (AXIS 或 AXIMM) 输出
            组合 Last ──────────────────►  InData(32)
            AxiM_RdDat_Vld ──► InVld
            Fifo_Rdy ◄──── InRdy ──► 同时接到 AxiM_RdDat_Rdy
```

计数与标记逻辑（在 `p_comb` 里，与 FSM case 并列）：

```
# DoneCnt：收齐计数
if (当前 Idle_s 且 Start=1):   DoneCnt_next = 0          # 新周期清零
elif (RdDat_Vld=1 且 Fifo_Rdy=1): DoneCnt_next = DoneCnt + 1   # 收到一个字
else:                          DoneCnt_next = DoneCnt      # 保持

# Last：组合逻辑，标记「当前正在写入的就是最后一个字」
Last = (DoneCnt == RegCount - 1) ? '1' : '0'
```

为什么 `Last` 用 `DoneCnt == RegCount-1` 而不是 `== RegCount`？因为 `DoneCnt` 是**「已经收到的字数」**。当已收到 `N-1` 个、第 N 个字正在被握手写入的那一拍，`DoneCnt` 的当前值还是 `N-1`，此时 `Last` 应为 1，让这第 N 个字带上末尾标记。换句话说：

\[ \text{Last 在「写入第 } k \text{ 个字」的那拍为 1，当且仅当 } k = \text{RegCount} \quad (k\in\{1,\dots,N\}) \]

而那一拍 `DoneCnt` 的值是 \(k-1\)，所以判据就是 `DoneCnt = RegCount - 1`。

#### 4.3.3 源码精读

`DoneCnt` 的清零与自增（写在 FSM case 之后、与之并列）：

[hdl/axi_mm_reader.vhd:161-166](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L161-L166) —— 新周期开始（`r.Fsm=Idle_s` 且 `r.Start='1'`）时清零；否则只要读数据握手成功（`AxiM_RdDat_Vld='1'` 且 `Fifo_Rdy='1'`）就 `DoneCnt + 1`。注意判据用的是 `r.Fsm`/`r.Start`（当前拍），与 `Idle_s` 里启动跳转的那拍对齐。

组合逻辑 `Last`：

[hdl/axi_mm_reader.vhd:168-172](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L168-L172) —— 默认 `Last<='0'`；当 `r.DoneCnt = r.RegCount-1` 时 `Last<='1'`。这是一个**并发赋值写在进程里**的信号（不是 record 字段），直接连到 FIFO 的 `InData(32)`。

`Last` 与数据如何汇入 FIFO，看实例化：

[hdl/axi_mm_reader.vhd:226-247](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L226-L247) —— FIFO 宽度 `Width_g=32+1`：低 32 位是数据 `AxiM_RdDat_Data`，第 32 位是 `Last`；输出侧第 32 位变成 `AxiS_Last`。这样「末尾标记」与数据一起在 FIFO 里排队，不会因为 FIFO 的先入先出而错位。

核心对外输出的连线：

[hdl/axi_mm_reader.vhd:181-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L181-L184) —— `DoneIrq <= r.DoneIrq`（已寄存，单拍脉冲）；`AxiM_RdDat_Rdy <= Fifo_Rdy`（把 FIFO 的背压直接透传给 AXI 主机读数据通道）。后者很关键：**FIFO 满时，核心会反压 AXI 主机，让它别再送数据**，`DoneCnt` 也因此暂停计数——这保证了「计数」与「真正写入 FIFO」永远同步。

> **一致性要点**：`DoneCnt` 自增条件 `RdDat_Vld='1' and Fifo_Rdy='1'` 与「数据真正写入 FIFO」的条件**完全相同**（FIFO 的 `InVld/InRdy` 握手）。而 `AxiM_RdDat_Rdy` 又等于 `Fifo_Rdy`。所以「`DoneCnt+1`」当且仅当「一个字真正进了 FIFO」，二者绝不脱节——即使发生背压，计数也不会多算或少算。

#### 4.3.4 代码实践

**实践目标**：用测试台的断言反向验证我们对 `Last` 的理解。

1. **阅读** [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd) 中的 `CheckResultsAxiS` 过程（约第 96–111 行）：它循环接收 14 个字（`for i in 0 to 13 loop`），并在第 `i` 个字上断言 `StdlCompare(choose(i=13,1,0), last, ...)`。
2. **解读断言**：`choose(i=13,1,0)` 表示「只有当 `i=13`（第 14 个、也就是最后一个字）时，期望 `last=1`，其余期望 `last=0`」。这正好对应核心里 `Last` 在 `DoneCnt = RegCount-1 = 13` 时拉高——而 14 个字对应 `RegCnt=14`（见 `top_tb` 里 `axi_single_write(RegIdx_RegCnt_c*4, 14, ...)`）。
3. **核对数字**：`RegCount=14`，`Last` 判据 `DoneCnt = 14-1 = 13`。第 14 个字（`i=13`）写入时 `DoneCnt` 当前值为 13 → `Last=1`。✓
4. **观察现象**：若你把测试台里 `RegCnt` 改成别的值（比如 1，对应「Single Reg Read Four Times」用例 6），`Last` 应在每个单字包上都为 1（因为 `RegCount-1=0`，每包第一个也是唯一一个字即末尾）。用例 6 的 AXIS 分支断言 `StdlCompare(1, m_axis_tlast, "Wrong Tlast")` 正好验证了这一点。
5. **预期结果**：你能说清「为什么 14 个字的包里只有最后一个 `TLAST=1`」由 [hdl/axi_mm_reader.vhd:168-172](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L168-L172) 这两行决定。
6. 本步骤为源码阅读型实践，**待本地验证**（跑通 `sim/run.tcl` 后查看 transcript 无 `###ERROR###`）。

#### 4.3.5 小练习与答案

**练习 1**：`DoneCnt` 的类型是 `integer range 0 to MaxRegCount_g`，而每周期最多读 `RegCount ≤ MaxRegCount_g` 个字。有没有可能 `DoneCnt` 溢出？
**答案**：不会。`RegCount` 上限就是 `MaxRegCount_g`，`DoneCnt` 从 0 数到 `RegCount` 即停（`WaitDone_s` 一旦 `DoneCnt=RegCount` 立刻发中断回 `Idle_s`，下一周期又清零）。范围 `0..MaxRegCount_g` 恰好覆盖，综合时也不会插多余的越界保护逻辑。

**练习 2**：`Last` 是组合逻辑信号，不是 record 字段。为什么它不需要进 record、不需要寄存？
**答案**：因为 `Last` 必须**与它标记的那个数据字在同一拍**到达 FIFO 的 `InData(32)`。数据 `AxiM_RdDat_Data` 本身是组合连进 FIFO 的（不经过核心寄存），所以 `Last` 也必须是组合的，才能与数据对齐。若把 `Last` 寄存一拍，它就会错位到「下一个字」上，末尾标记就错了。

**练习 3**：为什么 `WaitDone_s` 判完成用 `DoneCnt = RegCount`，而 `Last` 判据用 `DoneCnt = RegCount-1`？两者差一，会矛盾吗？
**答案**：不矛盾，它们标记的是不同事件。`Last` 标记「最后一个字**正在写入**」的那拍（此时 `DoneCnt` 还停留在 `N-1`）；`WaitDone_s` 判完成是「最后一个字**已经写完**之后」的那拍（此时 `DoneCnt` 已变成 `N`）。一个是写入瞬间，一个是写入完成之后，自然差一个计数。

## 5. 综合实践

把本讲三块内容串起来，完成下面这个端到端的小任务（本讲正式的代码实践任务）：

**任务**：为 `axi_mm_reader` 的核心 FSM 绘制一张完整的**状态转移图**，并撰写一段「设计意图说明」解释 `RamAddr` 的预自增。

1. **画状态转移图**：把 4.2.2 的流程细化为一张含 5 个状态的图，**每条转移线上标注精确条件**（用 `r.xxx` 表达当前拍信号）。至少应包含：
   - `Idle_s → ReadAddr_s`：`r.Start='1'`
   - `ReadAddr_s → SetCmd_s`：`unsigned(r.RamAddr) /= r.RegCount`
   - `ReadAddr_s → WaitDone_s`：`unsigned(r.RamAddr) = r.RegCount`
   - `SetCmd_s → ApplyCmd_s`：无条件
   - `ApplyCmd_s → ReadAddr_s`：`AxiM_CmdRd_Rdy='1'`
   - `ApplyCmd_s → ApplyCmd_s`（自循环）：`AxiM_CmdRd_Rdy='0'`
   - `WaitDone_s → Idle_s`：`r.DoneCnt = r.RegCount`（同时 `DoneIrq<='1'`）
   - `任意态 → Idle_s`：`Enable='0'`（全局覆盖）
2. **在图上额外标注**每个状态的「副作用」（如 `Idle_s`：`RamAddr←0`、`RegCount←port`；`ReadAddr_s`：`RamAddr+1`；`SetCmd_s`：装载 `RamRegAddr`、`Vld←1`；`ApplyCmd_s`：`Vld←0`）。
3. **写设计意图**：用自己的话解释——为什么 `ReadAddr_s` 在判断完成与否的**同时**就无条件 `RamAddr+1`，导致 `ApplyCmd_s` 等待握手时 `RamAddr` 已经指向下一项？要点应包括：
   - RegTable RAM 读出有一拍延迟（`RamRegAddr` 滞后 `r.RamAddr` 一拍）；
   - 提前自增让下一项地址在回到 `SetCmd_s` 时刚好就绪，省掉一个气泡周期；
   - 反压期间 `RamAddr` 静止、RAM 输出稳定，握手一通即可无缝续读。
4. **自检**：把你的图与 [hdl/axi_mm_reader.vhd:117-159](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L117-L159) 逐条对照，确认没有遗漏 `Enable='0'` 这条全局覆盖边。
5. **预期产出**：一张状态图 + 一段 3~5 句的意图说明。本任务为纸面/源码阅读型，无需改源码；**待本地验证**——若有仿真环境，可在波形上确认「`ApplyCmd_s` 期间 `RamAddr` 确实领先当前命令一项」。

## 6. 本讲小结

- 核心用**双进程方法**：`p_comb` 里 `v := r` 后只改必要字段（默认保持、杜绝锁存器），`p_seq` 只负责打拍 + 复位；所有寄存器打包进 `two_process_r` record，增删字段极轻量。
- FSM 共 5 态：`Idle_s`（待命、采样 `RegCount`）→ `ReadAddr_s`（推进指针、判完成）→ `SetCmd_s`（装命令）→ `ApplyCmd_s`（等握手）→ `WaitDone_s`（等数据收齐发 `DoneIrq`）。
- 主循环是 `ReadAddr_s → SetCmd_s → ApplyCmd_s → ReadAddr_s`，每个寄存器走一圈；`RamAddr` 在 `ReadAddr_s` 里**提前自增**，配合 RAM 一拍读延迟，做到「几乎一项一命令」并耐受反压。
- 命令侧与数据侧解耦：FSM 只管发命令，读回数据**旁路**直接进 FIFO；`DoneCnt` 专门数收回的字数。
- `Last` 是**组合逻辑**信号，判据 `DoneCnt = RegCount-1`，与数据同拍写入 FIFO 第 33 位，保证末尾标记不错位；`DoneIrq` 在 `WaitDone_s` 命中 `DoneCnt = RegCount` 时给出一拍脉冲。
- `DoneCnt` 自增条件与「字真正写入 FIFO」的条件完全一致（`RdDat_Vld & Fifo_Rdy`），且 `AxiM_RdDat_Rdy = Fifo_Rdy`，所以计数与 FIFO 内容永不脱节。

## 7. 下一步学习建议

- **u2-l4 触发与超时机制**：本讲把 `Start` 当成现成的启动脉冲，下一讲会回到 [hdl/axi_mm_reader.vhd:106-115](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L106-L115) 与 `Enable` 覆盖逻辑，讲清 `Trig`、`TimeoutCkCycles_g`、`Enable` 三者如何共同决定「何时出发、能否出发」。
- **u2-l5 AXI 从机配置接口**：去看 wrapper 里 `RegCount`/`Enable`/RegTable 是怎么从 `s00_axi` 解码到核心这些端口的，把「软件视角」与「核心端口」接起来。
- **u2-l6 AXI 主机读取通路**：本讲反复出现的 `CmdRd_*`/`RdDat_*` 握手，在 wrapper 里由 `psi_common_axi_master_simple` 翻译成真正的 AXI4 `AR`/`R` 通道，建议结合阅读，理解反压与单拍事务的来由。
- **u3-l2 测试台架构**：想看 FSM 在 6 组用例（单次读、缓冲双读、超时、禁用、背压、单寄存器四次读）下的真实波形，就回到 `tb/top_tb.vhd`；本讲的 `Last` 判据正是被 `CheckResultsAxiS` 校验的。
