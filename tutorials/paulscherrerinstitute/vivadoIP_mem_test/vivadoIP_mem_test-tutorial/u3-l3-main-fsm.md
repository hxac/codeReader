# 主状态机：Idle→写命令→写数据→读命令→读数据→错误

## 1. 本讲目标

u3-l2 把核心实体 `mem_test` 的「骨架」立了起来——我们知道了它的端口、它的 `two_process_r` 记录、它的 `p_comb`/`p_reg` 两进程分工，也瞥见了那行 `type Fsm_t is (Idle_s, WrCmd_s, Write_s, RdCmd_s, Read_s, AxiError_s, IntError_s);`。但当时我们刻意没有打开 `case r.Fsm is` 这个大开关——本讲就把它的每个档位掰开揉碎。学完后你应当能够：

- 说出 `Fsm_t` 七个状态各自的职责，并按「写命令→写数据→读命令→读数据」的主链路把它们串成一条完整的状态转移路径。
- 逐个解释每个状态的**进入条件**与**离开条件**，分清 `RegStart`、命令握手（`CmdWr_Rdy/CmdRd_Rdy`）、最后一拍（`PatternCnt = Size-1`）、`Wr_Error/Rd_Error` 这几类转移触发信号。
- 讲清三种模式如何在状态图上「抄近道」：`WRITEONLY` 在写完即回 `Idle_s`、`READONLY` 直接从 `Idle_s` 跳进 `RdCmd_s`、`CONTINUOUS` 在读完后回跳 `WrCmd_s` 形成循环——并解释 `STOP` 为什么是「优雅停止」。
- 说明 `AxiError_s`/`IntError_s` 这两个**不可恢复的陷阱态**是怎么进入、为什么一旦进入就出不来，以及它们与硬件复位的关系。
- 读懂 `FsmToInt` 函数如何把内部 7 个细状态**合并**成对外的 6 个 STATUS 码，并解释状态码在 3 之后跳到 6 的设计意图。

本讲**只讲控制流**（状态怎么走）。pattern 在每一拍具体生成什么值、读回数据怎么比对、首个错误地址怎么算——这些是 u3-l4 的内容，本讲只点到它们触发的时机。

## 2. 前置知识

继续沿用 u3-l1 的「工厂」与 u3-l2 的「两进程」比喻，再补三块新积木。

- **有限状态机（FSM, Finite State Machine）**：一种用「状态 + 转移」描述控制逻辑的方法。系统在任一时刻处于一个**状态**（如「正在写数据」），当满足某个**条件**（如「最后一拍写完」）时，**转移**到下一个状态（如「发读命令」）。VHDL 里通常先用枚举类型 `type Fsm_t is (...)` 列出所有状态，再用 `case r.Fsm is when Idle_s => ...` 把每个状态的行为写一段。本讲的主角就是这台 FSM。
- **Moore 型状态机**：输出只依赖**当前状态**、不直接依赖当前输入。u3-l2 已经点明 `mem_test` 是 Moore 风格——所有输出都从寄存后的 `r` 引出（`outputs = g(r)`），所以输出本质上是「当前状态的函数」。这意味着状态切换会带来输出的变化，但输入只通过「改变下一状态」间接影响输出。
- **握手型状态转移**：状态机的很多转移条件不是「电平满足」那么简单，而是「一次 valid/ready 握手完成」。例如 `Write_s` 不是「一进就走」，而是要等 `WrDat_Vld` 与 `WrDat_Rdy` 同拍为 1（一次成功的数据握手）才推进。理解这一点，才能看懂为什么 FSM 里反复出现 `if (r.Xxx_Vld = '1') and (Xxx_Rdy = '1') then ...` 这种判据。

复习三个老朋友（来自 u3-l2）：① `v := r` 惯法让我们「只写变化量」；② 每个周期开头先把四个握手有效信号清零（`v.CmdWr_Vld := '0'; ...`）、再由具体状态按需拉高，这是产生**单拍脉冲**的手法；③ `p_comb` 检查的是**寄存后**的 `r.Xxx_Vld`，所以「拉高」与「检测握手」天然隔了一拍——这正是本讲要反复用到的「注册式 valid」握手节奏。

再复习 u2-l1 的寄存器地图与 u2-l2 的模式/pattern 语义：`START`(0x00)/`STOP`(0x04) 是触发型寄存器、`MODE`(0x0C) 选四种模式、`STATUS`(0x24) 反映当前状态、`C_MODE_SINGLE/CONTINUOUS/WRITEONLY/READONLY` 四种模式、`C_STATUS_*` 六个状态码。本讲就是把 `MODE` 的取值翻译成 `Fsm_t` 的走法，再把 `Fsm_t` 翻译回 `STATUS`。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，另一个文件只借它的常量。

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心测试逻辑（本讲主角） | `Fsm_t` 类型、`case r.Fsm is` 七个分支、共享错误锁存段、`FsmToInt` 函数、`STATUS` 寄存器回读 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 全局 package | 模式常量 `C_MODE_*`、状态码常量 `C_STATUS_*` |

> 提示：`InitPattern_v`/`UpdatePattern_v` 触发的四种 pattern 算法、`Read_s` 里的读回比对与 `FirstErrAddr` 换算，本讲只说明「它们在哪个状态被触发」，具体算法见 u3-l4。

## 4. 核心概念与源码讲解

### 4.1 Fsm_t 类型定义

#### 4.1.1 概念说明

`mem_test` 的全部控制逻辑，都由一台只有 7 个状态的有限状态机驱动。这 7 个状态用一个 VHDL 枚举类型 `Fsm_t` 声明。在深入每个状态的行为之前，先把它们的名字、顺序、大致职责记牢——这相当于拿到一张地图的图例。

七个状态可分为三组：

1. **主链路四态**（一次「写→读」测试的核心路径）：
   - `Idle_s`：空闲。等待 CPU 写 `START` 寄存器。
   - `WrCmd_s`：向 AXI 主机下达**写命令**（地址 + 字节数），等待主机接收。
   - `Write_s`：逐拍向主机**送写数据**（pattern）。
   - `RdCmd_s`：向主机下达**读命令**，等待接收。
   - `Read_s`：逐拍从主机**收读数据**并比对。
2. **错误陷阱二态**（一旦进入永不返回）：
   - `AxiError_s`：AXI 总线错误（`Wr_Error` 或 `Rd_Error`）。
   - `IntError_s`：内部错误（配置了非法 pattern）。
3. 枚举本身还隐含一个 `others`（理论上不会发生，作为安全网）。

为什么要拆出 `WrCmd_s` 与 `Write_s` 两个状态、而不是「边发命令边送数据」？因为命令与数据是两条**独立握手**的通路（u3-l2 已讲）：命令握手（`CmdWr_Vld↔CmdWr_Rdy`）要先完成，主机才知道「这次要写哪、写多少」，之后数据握手（`WrDat_Vld↔WrDat_Rdy`）才能开始送 beat。把这两步拆成两个状态，让每一步只关心自己那条握手，逻辑更清晰。读路径同理。

#### 4.1.2 核心流程

七个状态在「正常一次 SINGLE 模式测试」里的串联顺序：

```
   复位
     │
     ▼
  Idle_s ──RegStart──▶ WrCmd_s ──CmdWr握手──▶ Write_s
                                                  │
                              写完最后一拍(非WRITEONLY)
                                                  ▼
   Idle_s ◀──非CONTINUOUS── Read_s ◀──CmdRd握手── RdCmd_s
```

注意四个关键时机：

- **进入主链路**：`Idle_s` 收到 `RegStart` 脉冲。
- **命令态→数据态**：命令的 valid/ready 握手成立。
- **数据态→下一命令态**：`PatternCnt` 数到 `Size-1`（最后一拍）且该拍握手成功。
- **读结束→空闲**：读最后一拍握手成功且不在循环模式。

模式抄近道与循环回跳会在 4.2 详述。

#### 4.1.3 源码精读

**枚举类型声明**：

[hdl/mem_test.vhd:72](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L72) —— `type Fsm_t is (Idle_s, WrCmd_s, Write_s, RdCmd_s, Read_s, AxiError_s, IntError_s);`。

读这行要注意三点：

1. **命名后缀 `_s`** 表示「state」（状态），与信号名里的 `_v`（variable，变量）、`_r`（record/registered，寄存）等区分，是本项目的命名约定。
2. 枚举值的**位置序号**（positional）从 0 开始：`Idle_s=0, WrCmd_s=1, Write_s=2, RdCmd_s=3, Read_s=4, AxiError_s=5, IntError_s=6`。但要注意——**对外 STATUS 码并不直接用这个序号**（否则 AXI 错误会显示成 5 而不是 3）。状态码由 `FsmToInt` 显式映射，见 4.3。
3. 这 7 个状态被装进记录字段 `Fsm : Fsm_t`（[hdl/mem_test.vhd:95](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L95)），复位时初始化为 `Idle_s`（[hdl/mem_test.vhd:385](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L385)）。

> 为什么不用整型而用枚举？枚举让综合器自动为每个状态分配二进制编码（默认顺序编码，也可由综合器选独热/格雷码），而源码里只出现**有意义的名字**（如 `Write_s`），不必记忆「2 代表写数据」——可读性与可维护性都远高于裸整数。

#### 4.1.4 代码实践

**实践目标**：把七个状态按职责分类，建立「主链路 / 错误陷阱」的心智分组。

**操作步骤（源码阅读型）**：

1. 打开 [hdl/mem_test.vhd:72](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L72)。
2. 画出一张两列的表：左列「主链路（正常流转）」，右列「错误陷阱（不可恢复）」。把七个状态填进去。
3. 在主链路里再标出方向：哪几个状态属于「写侧」（命令+数据），哪几个属于「读侧」。

**需要观察的现象**：主链路是严格对称的「写命令+写数据 / 读命令+读数据」两段；错误陷阱与主链路之间**没有出口**（只有入口）。

**预期结果**：主链路 = `Idle_s, WrCmd_s, Write_s, RdCmd_s, Read_s`；错误陷阱 = `AxiError_s, IntError_s`。写侧 = `WrCmd_s, Write_s`，读侧 = `RdCmd_s, Read_s`。本实践为源码阅读，结论确定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Idle_s` 不归入「写侧」或「读侧」？

**答案**：`Idle_s` 是「等待命令」的中立状态，既不属于写也不属于读——它是主链路的**起点与终点**，也是 `STATUS` 寄存器对外表示「IP 空闲、可接受新配置」的状态。读写侧是运行态，`Idle_s` 是就绪态。

**练习 2**：如果不小心把 `AxiError_s` 的名字写漏、枚举里少了它，综合会怎样？

**答案**：源码里 `FsmToInt`（4.3）的 `when AxiError_s =>` 与 `case r.Fsm is` 的 `when AxiError_s =>` 都会因找不到该枚举值而**编译报错**。这正是枚举相对于整数的好处——拼写错误在编译期就被捕获，不会潜伏成运行期 bug。

---

### 4.2 主 FSM case 分支

#### 4.2.1 概念说明

`Fsm_t` 只是「状态的名字清单」；真正决定「在哪个状态做什么、何时跳到哪」的，是 `p_comb` 里的 `case r.Fsm is ... end case;` 这一大段。它是整台状态机的「大脑」，也是本讲篇幅最大的部分。

理解这段代码，要抓住**一个统一节奏**与**三类转移条件**。

**一个统一节奏——「注册式 valid 握手」**。回顾 u3-l2：每个周期开头先把四个握手信号清零（`v.CmdWr_Vld := '0'; v.WrDat_Vld := '0'; v.CmdRd_Vld := '0'; v.RdDat_Rdy := '0';`），具体状态再按需拉高。于是这些有效信号是**先注册一拍**才被检测的——也就是说，「拉高 valid」与「检测 ready」天然错开一拍。这就解释了为什么每个命令/数据状态里，握手判据都写成 `if (r.Xxx_Vld = '1') and (Xxx_Rdy = '1') then`：它看的是**上一拍已经注册进 `r` 的 valid** 与**当前拍的 ready**，两者同拍为 1 才算一次握手成功。这种写法保证 valid 至少稳定一拍，避免毛刺。

**三类转移条件**：

1. **寄存器触发**：`RegStart`（CPU 写 START）、`RegStop`（CPU 写 STOP）。它们是一拍脉冲。
2. **握手完成**：命令握手 `CmdWr_Rdy/CmdRd_Rdy`、数据握手 `WrDat_Rdy/RdDat_Vld`。
3. **计数到位**：`PatternCnt = Size-1`（写到/读到最后一拍）。

外加一类**异常注入**（在 case 之外的共享段处理）：`Wr_Error/Rd_Error`、非法 pattern——它们随时可以把 FSM 拽进错误陷阱，与当前状态无关。

#### 4.2.2 核心流程

下面是一张覆盖**所有转移**的状态图。实线是主链路，虚线是模式抄近道与异常。

```
                     ┌──────────── RegStart (READONLY) ───────────┐
                     │                                            ▼
   复位 ─▶ Idle_s ───┼─ RegStart (其他模式) ─▶ WrCmd_s ──[CmdWr握手]─▶ Write_s
            ▲ ▲      │                              ▲               │
            │ │      │                              │ 最后一拍       │ 最后一拍
            │ │      │                              │ +WRITEONLY     │ (非WRITEONLY)
            │ │      │                              │ 自 Idle 直来   ▼
            │ │      │                              │          RdCmd_s
            │ │      │                              ▲   │            ▲
            │ │      │                   [CmdRd握手] │   │            │
            │ │      │                              │   ▼            │
            │ │      │                            Read_s            │
            │ │      │                              │   │            │
            │ │非Cont│ 最后一拍+ContRunning=0       │   │            │
            │ └──────┤ ◀────────────────────────────┘   │            │
            │        │                                  │            │
            │        │ 最后一拍+ContRunning=1            │            │
            │        └──────── 循环回 WrCmd_s ◀──────────┘            │
            │                                                           │
            │   STOP 仅清 ContRunning：当前迭代 Read 跑完才回 Idle（优雅停止）
            │                                                           │
            │   任意状态 ──Wr_Error=1 或 Rd_Error=1──▶ AxiError_s (陷阱)│
            │   InitPattern/UpdatePattern 非法 ─────────▶ IntError_s(陷阱)│
            └────────────────────── 仅硬件复位能离开陷阱 ────────────────┘
```

把这张图浓缩成一张**转移表**（这是本讲实践任务的核心交付物之一）：

| # | 当前状态 | 转移条件 | 下一状态 | 副作用/备注 |
| --- | --- | --- | --- | --- |
| 1 | `Idle_s` | `RegStart=1` 且 `mode≠READONLY` | `WrCmd_s` | 清零 `Errors/FirstErrAddr/FirstErrFound/ContIter` |
| 2 | `Idle_s` | `RegStart=1` 且 `mode=READONLY` | `RdCmd_s` | 同上；跳过整个写阶段 |
| 3 | `WrCmd_s` | `r.CmdWr_Vld=1` 且 `CmdWr_Rdy=1` | `Write_s` | 命令被主机接收；`CmdWr_Vld` 拉低 |
| 4 | `Write_s` | 最后一拍握手 且 `mode=WRITEONLY` | `Idle_s` | 只写不读，写完即停 |
| 5 | `Write_s` | 最后一拍握手 且 `mode≠WRITEONLY` | `RdCmd_s` | 转入读阶段 |
| 6 | `RdCmd_s` | `r.CmdRd_Vld=1` 且 `CmdRd_Rdy=1` | `Read_s` | 读命令被接收；`CmdRd_Vld` 拉低 |
| 7 | `Read_s` | 最后一拍握手 且 `ContRunning=0` | `Idle_s` | 单轮/优雅停止后结束 |
| 8 | `Read_s` | 最后一拍握手 且 `ContRunning=1` | `WrCmd_s` | **CONTINUOUS 循环回跳**；`ContIter+1` |
| E1 | 任意 | `Wr_Error=1` 或 `Rd_Error=1` | `AxiError_s` | AXI 总线错误陷阱 |
| E2 | 任意 | `InitPattern/UpdatePattern` 落到 `when others` | `IntError_s` | 非法 pattern 陷阱 |
| E3 | `AxiError_s` | 无条件（每拍自锁） | `AxiError_s` | 不可恢复 |
| E4 | `IntError_s` | 无条件（每拍自锁） | `IntError_s` | 不可恢复 |

「最后一拍」的判定是个简单算式。设本轮命令大小（已折算成 beat 数）为 \(S\)，进度计数 `PatternCnt` 从 0 数起，则共写/读 \(S\) 拍，最后一拍满足：

\[ \text{PatternCnt} = S - 1 \]

源码里写侧用 `r.PatternCnt = r.CmdWr_Size-1`、读侧用 `r.PatternCnt = r.CmdRd_Size-1`。两者是同一套计数逻辑，只是分别用写/读命令记录下来的 Size。

#### 4.2.3 源码精读

**进入 FSM 前的两段预处理**。在 `case r.Fsm is` 之前，`p_comb` 先做两件事，它们对理解状态行为至关重要：

第一，**每周期清零握手有效信号**（u3-l2 已点）：

[hdl/mem_test.vhd:200-205](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L200-L205) —— `v.CmdWr_Vld := '0'; v.WrDat_Vld := '0'; v.CmdRd_Vld := '0'; v.RdDat_Rdy := '0';` 加 `InitPattern_v := false; UpdatePattern_v := false;`。这意味着：除非某个状态在本周期显式把它们置 1，否则下一拍这些握手信号都是 0——正是产生单拍脉冲、避免 valid 卡死的关键。

第二，**START/STOP 与 Continuous 标志的联动**：

[hdl/mem_test.vhd:188-197](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L188-L197) —— 当 `RegStart_v=1` 时，按模式决定 `ContRunning`：若 `RegMode_v = C_MODE_CONTINUOUS` 则 `v.ContRunning := '1'`，否则 `:= '0'`；当 `RegStop_v=1` 时无条件 `v.ContRunning := '0'`。这段代码在 `case` 之外，**先于状态机执行**，因此 `ContRunning` 的值在 FSM 各分支读到时已经是「考虑了本拍 START/STOP 之后」的最新值。这也解释了 `STOP` 的「优雅停止」语义：它只清标志、不打断当前状态，FSM 依然把当前迭代的 `Read_s` 跑完，到最后一拍时因 `ContRunning=0` 走转移 #7 回 `Idle_s`。

`RegStart_v` 与 `RegStop_v` 本身来自寄存器写脉冲：

[hdl/mem_test.vhd:147](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L147) 与 [hdl/mem_test.vhd:150](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L150) —— `RegStart_v := Reg_WData(REG_START)(C_START_START) and Reg_Wr(REG_START);`、`RegStop_v := Reg_WData(REG_STOP)(C_STOP_STOP) and Reg_Wr(REG_STOP);`。即「写数据里的 START/STOP 位为 1」且「本拍确实发生了对该寄存器的写」——两者同时成立才产生一拍脉冲。这正是 u2-l1 所说触发型寄存器（strobe）的实现方式。

**主开关 `case r.Fsm is`**：

[hdl/mem_test.vhd:206](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L206) —— 按 `r.Fsm`（当前已寄存的状态）分派到七个分支。

**① `Idle_s` —— 起点：监听 START，按模式分流**：

[hdl/mem_test.vhd:209-221](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L209-L221) —— 若 `RegStart_v=1`：`READONLY` 模式直接跳 `RdCmd_s`（转移 #2，跳过写阶段），其余模式跳 `WrCmd_s`（转移 #1）。**同时**执行一次「软清零」：`v.FirstErrAddr := (others => '0'); v.Errors := (others => '0'); v.FirstErrFound := '0'; v.ContIter := (others => '0');`。这印证了 u3-l2 的结论——这些统计字段在硬件复位时不清，而在每次软件 START 时清零，所以省下了复位触发器。

**② `WrCmd_s` —— 写命令态：装配命令、等握手**：

[hdl/mem_test.vhd:224-234](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L224-L234) —— 这一段做了四件事：

1. **装配地址**：`v.CmdWr_Addr := RegAddr_v(v.CmdWr_Addr'range);` 取基地址，再用 `v.CmdWr_Addr(log2(AxiDataWidth_g/8)-1 downto 0) := (others => '0');` 把低 `log2(数据字节数)` 位清零——即按数据宽度对齐（例如 32 位数据清低 2 位，保证地址 4 字节对齐）。
2. **折算大小**：`v.CmdWr_Size := shift_right(RegSize_v(v.CmdRd_Size'left downto 0), log2(AxiDataWidth_g/8));` 把「字节数」右移 `log2(数据字节数)` 位，换算成「beat 数」。例如 32 位数据（4 字节）下，`Size=0x100` 字节 → `0x40` beat。
3. **拉高 valid、复位进度、触发 pattern 初始化**：`v.CmdWr_Vld := '1'; v.PatternCnt := (others => '0'); InitPattern_v := true;`。
4. **等握手**：`if (r.CmdWr_Vld = '1') and (CmdWr_Rdy = '1') then v.Fsm := Write_s; v.CmdWr_Vld := '0'; end if;`（转移 #3）。注意判据用的是**寄存后**的 `r.CmdWr_Vld`——第一拍进 `WrCmd_s` 时 `r.CmdWr_Vld` 还是 0（周期开头清零过），所以本拍只把 `v.CmdWr_Vld` 拉高、不跳转；下一拍 `r.CmdWr_Vld` 变 1，若 `CmdWr_Rdy` 也为 1 才握手成功、跳 `Write_s`。这就是「注册式 valid」节奏。

**③ `Write_s` —— 写数据态：逐拍送 pattern**：

[hdl/mem_test.vhd:237-253](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L237-L253) —— 先 `v.WrDat_Vld := '1';`，再判 `if (r.WrDat_Vld = '1') and (WrDat_Rdy = '1') then`：一次数据握手成功后，看是不是最后一拍 `r.PatternCnt = r.CmdWr_Size-1`：

- **最后一拍**：按模式分流——`WRITEONLY` 跳 `Idle_s`（转移 #4，写完即停），否则跳 `RdCmd_s`（转移 #5）。同时 `v.WrDat_Vld := '0';` 收尾。
- **非最后一拍**：`v.PatternCnt := r.PatternCnt+1; UpdatePattern_v := true;`——推进进度、触发 pattern 更新（具体怎么更新见 u3-l4）。

注意 `WrDat_Data` 并不在记录里——它由并发赋值 `WrDat_Data <= std_logic_vector(r.Pattern);`（[hdl/mem_test.vhd:367](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L367)）直接取当前 pattern 值，所以「送写数据」就是「把 pattern 推进一拍再输出」。

**④ `RdCmd_s` —— 读命令态**：

[hdl/mem_test.vhd:256-266](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L256-L266) —— 与 `WrCmd_s` **完全对称**：装配 `CmdRd_Addr`（同样对齐）、折算 `CmdRd_Size`（同样的 `shift_right`）、拉高 `CmdRd_Vld`、复位 `PatternCnt`、`InitPattern_v := true`（读阶段也要重新初始化 pattern，以便与写阶段用同一套序列逐拍比对），最后 `if (r.CmdRd_Vld = '1') and (CmdRd_Rdy = '1') then v.Fsm := Read_s; ...`（转移 #6）。

> 关键细节：读阶段**再次** `InitPattern_v := true`。这是因为读比对要求「读回的第 N 拍数据」与「写时发出的第 N 拍 pattern」一一对应。重新初始化 pattern、再用 `UpdatePattern` 逐拍推进，就能在 `Read_s` 里复现写时的同一序列，从而比对。pattern 算法见 u3-l4。

**⑤ `Read_s` —— 读数据态：收数据、比对、决定回跳**：

[hdl/mem_test.vhd:269-296](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L269-L296) —— 先 `v.RdDat_Rdy := '1';`，再判 `if (r.RdDat_Rdy = '1') and (RdDat_Vld = '1') then`（注意这里方向翻转：核心给 ready、主机给 valid）。一次读握手成功后：

- **最后一拍**（`r.PatternCnt = r.CmdRd_Size-1`）：`v.ContIter := r.ContIter+1;`（迭代数加 1），然后按 `ContRunning` 分流——为 1 跳 `WrCmd_s`（转移 #8，**CONTINUOUS 循环回跳**），为 0 跳 `Idle_s`（转移 #7）。同时 `v.RdDat_Rdy := '0';`。
- **非最后一拍**：`v.PatternCnt := r.PatternCnt+1; UpdatePattern_v := true;`。
- **pattern 比对**（与上面同级、每次握手成功都执行）：`if RdDat_Data /= r.Pattern then v.Errors := r.Errors + 1; ... end if;`——读回数据与期望 pattern 不符就累加错误、并（若首次出错）记录 `FirstErrAddr`。具体换算见 u3-l4。

**CONTINUOUS 循环的关键一句**就在转移 #8：[hdl/mem_test.vhd:275-276](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L275-L276) —— `if r.ContRunning = '1' then v.Fsm := WrCmd_s;`。回跳到 `WrCmd_s` 后，那里会再次 `InitPattern_v := true`、`PatternCnt := 0`，于是新一轮「写→读」开始；每轮结束 `ContIter` 加 1，对外可通过 `ITER` 寄存器（[hdl/mem_test.vhd:185](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L185)）读到已完成轮数。

**⑥ `AxiError_s` 与 ⑦ `IntError_s` —— 空 trap**：

[hdl/mem_test.vhd:299-301](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L299-L301) 与 [hdl/mem_test.vhd:304-306](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L304-L306) —— 这两个分支体只有一句 `null;`，注释 `-- Non recoverable!`。单看分支体它们「什么都不做」，真正的「锁死」逻辑在 case 之后的共享段（见下）。

**case 之后的共享错误锁存段**——这是理解「不可恢复」的关键：

[hdl/mem_test.vhd:349-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L349-L358) —— 三条规则，**在 case 之后执行、可覆盖 case 内已设置的 `v.Fsm`**：

1. `if r.Fsm = IntError_s then v.Fsm := IntError_s; end if;`——当前已在 `IntError_s`，则下一拍强制留在 `IntError_s`。
2. `if r.Fsm = AxiError_s then v.Fsm := AxiError_s; end if;`——同理锁死 `AxiError_s`。
3. `if (Wr_Error = '1') or (Rd_Error = '1') then v.Fsm := AxiError_s; end if;`——**任意状态**下，只要主机报 AXI 错误，立刻跳 `AxiError_s`（转移 E1）。

合起来：进入错误态的途径有两个（AXI 错误 / 非法 pattern），离开的途径**零个**——一旦 `r.Fsm` 是错误态，规则 1/2 每拍都把它钉死，只有硬件复位（`p_reg` 里 `r.Fsm <= Idle_s`）能解锁。这就是「Non recoverable」的全部含义：**fail-latch（故障锁定）**，让 CPU 通过 `STATUS` 寄存器稳定地看到「出错了」，而不会因为状态机继续跑导致错误信息被覆盖。

> 为什么不让 FSM 自动恢复？因为 AXI 错误（如地址越界、slave 返回 SLVERR）通常意味着外部存储器或地址映射出了问题，盲目重试只会反复出错；内部错误（非法 pattern）则意味着配置软件本身有 bug。两种情形下，「停下并暴露问题」都比「继续跑」更安全——这正是测试仪器应有的保守行为。

#### 4.2.4 代码实践

**实践目标**：亲手把 FSM 画成状态转移图、标注所有转移条件，并解释 CONTINUOUS 的回跳——这是本讲指定的实践任务。

**操作步骤（源码阅读 + 纸面作图型）**：

1. 对照 [hdl/mem_test.vhd:206-308](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L206-L308)，为每个状态的每个 `if ... v.Fsm := ... end if;` 画一条带标注的箭头。
2. 给每条箭头标注触发条件，归入四类之一：`RegStart` / 命令握手（`CmdWr_Rdy`、`CmdRd_Rdy`）/ 数据握手 + 最后一拍（`PatternCnt = Size-1`）/ 错误注入（`Wr_Error`、`Rd_Error`）。
3. 单独画出 CONTINUOUS 的回跳：从 `Read_s` 末尾经 `ContRunning=1` 回到 `WrCmd_s`，并在旁边标注 `ContIter+1`。
4. 在图上用红笔标出两个错误陷阱，注明「无出口」。
5. 把你画的图与 4.2.2 的转移表逐行核对。

**需要观察的现象**：

- `Idle_s` 有两条出边（按 `READONLY` 分流）；`Write_s` 有两条出边（按 `WRITEONLY` 分流）；`Read_s` 有两条出边（按 `ContRunning` 分流）。**所有「模式/循环」分支都集中在数据态的末尾**，命令态（`WrCmd_s/RdCmd_s`）只有单一出边（握手成功）。
- CONTINUOUS 回跳的目标是 `WrCmd_s` 而不是 `Idle_s`——所以循环里**不经过空闲态**，节省时间，也意味着 `START` 的「软清零」只发生一次（首轮），后续轮的错误数是**累计**的（呼应 u2-l2 的结论）。

**预期结果**：得到一张与 4.2.2 状态图、转移表一致的手绘 FSM 图。本实践为纸面作图 + 源码阅读，结论确定；若要用仿真核对每个转移，可在 testbench 里逐拍观察 `r.Fsm`（需把记录字段引到顶层或用 Modelsim 的 `view -internal`），结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`READONLY` 模式跳过了 `WrCmd_s`/`Write_s`，那么它依赖什么前提才能正确比对？

**答案**：依赖「被测存储器里**事先已经写好**了与所选 pattern 相同的数据」（例如之前用 `WRITEONLY` 或 `SINGLE` 写过一遍）。`READONLY` 只读不写，直接进 `RdCmd_s` 用同一 pattern 序列去比对存储器现有内容。如果存储器是空的或内容不符，会读出大量「错误」。

**练习 2**：为什么命令态（`WrCmd_s`/`RdCmd_s`）里要 `InitPattern_v := true`，而数据态（`Write_s`/`Read_s`）里用 `UpdatePattern_v`？

**答案**：命令态是「开始一轮数据传输」的时刻，需要把 pattern **初始化**到序列的第一个值（如 Counter 的 0、PRBN 的种子 `0x6D3F`）；数据态每拍握手成功只是**推进**到序列的下一个值。所以「初始化」发生在进入数据传输之前（命令态），「更新」发生在每个 beat 之上（数据态）。两个标志分别触发 `InitPattern`/`UpdatePattern` 两段共享代码（u3-l4 详述）。

**练习 3**：若在 `Read_s` 跑到一半时主机突然拉高 `Rd_Error`，会发生什么？

**答案**：[hdl/mem_test.vhd:356-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L356-L358) 的共享规则 3 会把 `v.Fsm` 强制改成 `AxiError_s`，覆盖 `Read_s` 分支本拍算出的下一态。此后每拍规则 2 把它钉死在 `AxiError_s`。即「立即终止并锁定」，CPU 读 `STATUS` 会看到 `AXIERR`。

---

### 4.3 FsmToInt 状态映射

#### 4.3.1 概念说明

FSM 内部有 7 个状态，但对外暴露给 CPU 的 `STATUS` 寄存器（u2-l1）只有 6 个状态码。这中间靠一个纯函数 `FsmToInt` 做「多对一」映射——把若干**内部细状态**合并成一个**对外粗状态**。

为什么要合并？因为 CPU 关心的是「IP 现在在大干什么阶段」，而不是「FSM 当前在哪个具体子状态」。例如 `WrCmd_s`（正在发写命令）和 `Write_s`（正在送写数据）对 CPU 而言都是「正在写存储器」，区分二者没有意义——软件轮询 `STATUS` 只想知道「还在忙吗 / 在写还是在读 / 出错了吗」。于是 `FsmToInt` 把它们都映射成 `C_STATUS_WRITING`。

这体现了「**内部实现复杂度对外不可见**」的良好接口设计：FSM 可以为了握手清晰而拆出 `WrCmd_s`/`Write_s` 两个状态，但这个实现细节不会泄漏到寄存器地图里——CPU 看到的始终是稳定的 6 个状态码。

#### 4.3.2 核心流程

`FsmToInt` 的映射关系：

| 内部状态（`Fsm_t`） | → | STATUS 码 | 数值 | 含义 |
| --- | --- | --- | --- | --- |
| `Idle_s` | → | `C_STATUS_IDLE` | 0 | 空闲 |
| `WrCmd_s`, `Write_s` | → | `C_STATUS_WRITING` | 1 | 正在写（命令+数据合并）|
| `RdCmd_s`, `Read_s` | → | `C_STATUS_READING` | 2 | 正在读（命令+数据合并）|
| `AxiError_s` | → | `C_STATUS_AXIERR` | 3 | AXI 总线错误 |
| `IntError_s` | → | `C_STATUS_INTERR` | 6 | 内部错误 |
| `others`（理论不发生）| → | `C_STATUS_UNKNOWN` | 7 | 未知 |

注意数值上的**跳变**：0、1、2 之后是 3，然后直接跳到 6、7。这不是笔误，而是有意为之的分组：

- `0~2`：正常态（空闲 / 写 / 读）。
- `3`：AXI 错误（外部总线问题）。
- `6`：内部错误（配置问题）。
- 4、5 留空，为将来可能新增的错误子类（如「超时错误」「地址未对齐错误」）预留编码空间，不必改动现有枚举值，向后兼容。

`STATUS` 寄存器的回读就是「读 `r.Fsm` → 过 `FsmToInt` → 放进寄存器位域」，是一个纯组合查询，没有额外状态。

#### 4.3.3 源码精读

**`FsmToInt` 函数**：

[hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89) —— 纯函数（`function ... return integer`），输入一个 `Fsm_t`、输出一个整数。`case fsm is` 里用 `|` 把多个状态合并到同一返回值：`when Write_s | WrCmd_s => return C_STATUS_WRITING;`、`when Read_s | RdCmd_s => return C_STATUS_READING;`。末尾 `when others => return C_STATUS_UNKNOWN;` 是安全网——理论上 `Fsm_t` 只有 7 个值、上面已穷举，`others` 不会被命中；但 VHDL 要求 `case` 完备，且万一综合器采用了带冗余的编码（如独热可能出现非法组合），`UNKNOWN` 提供了一个明确的「不应出现」信号而非随机值。

**STATUS 码常量**（在 package 里）：

[hdl/mem_test_pkg.vhd:58-63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L58-L63) —— `C_STATUS_IDLE=0, C_STATUS_WRITING=1, C_STATUS_READING=2, C_STATUS_AXIERR=3, C_STATUS_INTERR=6, C_STATUS_UNKNOWN=7`。注意 3 之后跳到 6 的留白。

**STATUS 寄存器回读**——`FsmToInt` 的唯一调用点：

[hdl/mem_test.vhd:173-174](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L173-L174) —— `Reg_RData(REG_STATUS) <= (others => '0'); Reg_RData(REG_STATUS)(RNG_STATUS) <= std_logic_vector(to_unsigned(FsmToInt(r.Fsm), RNG_STATUS'high+1));`。即先把整个 32 位 STATUS 寄存器清零，再把 `FsmToInt(r.Fsm)` 的结果放进 `RNG_STATUS`（bit 2 downto 0）位域。`RNG_STATUS` 是 3 位（[hdl/mem_test_pkg.vhd:57](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L57)），可表示 0~7，正好覆盖到 `UNKNOWN=7`。

> Moore 风格的体现：`STATUS` 直接读 `r.Fsm`（已寄存的当前状态），不读任何输入。所以 CPU 看到的状态码是「上一个时钟沿定格的状态」，时序干净、无毛刺。这也是为什么 `FsmToInt` 是纯函数——它只做静态映射，不带状态、不带副作用。

#### 4.3.4 代码实践

**实践目标**：追踪「一次测试过程中 `STATUS` 寄存器的取值序列」，把内部状态流转与对外状态码对应起来。

**操作步骤（源码阅读 + 推演型）**：

1. 假设一次 `SINGLE` 模式、`COUNTER` pattern 的测试，按时间顺序列出 `r.Fsm` 经历的状态：`Idle_s → WrCmd_s → Write_s → RdCmd_s → Read_s → Idle_s`。
2. 对每个内部状态，查 [hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89) 得到 `FsmToInt` 输出。
3. 写出 CPU 轮询 `STATUS` 会看到的码值序列。
4. 思考：`WrCmd_s` 持续多久？CPU 有没有可能在轮询时恰好「看不到」它？

**需要观察的现象**：`WrCmd_s`/`RdCmd_s` 与各自的数据态合并成同一个码（`WRITING`/`READING`），所以 CPU 看到的序列比内部状态序列「粗」。

**预期结果**：CPU 看到的 `STATUS` 序列为 `IDLE(0) → WRITING(1) → READING(2) → IDLE(0)`——只有四个阶段，尽管内部走了六个状态。`WrCmd_s` 通常只持续 1~2 拍（命令握手很快），CPU 如果采样不够频繁可能恰好错过它，但因为 `Write_s` 也是 `WRITING`，错过 `WrCmd_s` 不影响判断「正在写」。这正是合并映射的实际收益。本实践为推演型，结论确定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `FsmToInt` 用 `case` + `|` 合并，而不是写一个查找数组？

**答案**：`case` 在 `Fsm_t` 枚举上做匹配，可读性最强（直接看到「这两个状态都返回 WRITING」），且综合器会把它优化成高效的组合逻辑。数组查找需要先把枚举转成整数下标，反而绕弯、也更容易在新增状态时漏掉某一项。`case` 还强制完备性检查（配 `others`），新增状态时编译器会提醒你补分支。

**练习 2**：如果未来要新增一个「校准中」状态 `Calib_s`，STATUS 码用 4 表示，需要改哪些地方？

**答案**：① 在 `Fsm_t` 枚举（[hdl/mem_test.vhd:72](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L72)）加 `Calib_s`；② 在 `FsmToInt`（[hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89)）加 `when Calib_s => return C_STATUS_CALIB;`；③ 在 package 加 `constant C_STATUS_CALIB : integer := 4;`；④ 在 C 驱动（u2-l3）的 `MemTest_Status` 枚举里加一项。注意 4、5 正是当初留白的编码位——这印证了 4.3.2 说的「向后兼容」设计。

**练习 3**：`STATUS` 寄存器为什么先整体清零再写位域，而不是直接整字赋值？

**答案**：因为 `STATUS` 寄存器只用了低 3 位（`RNG_STATUS`），高位是保留位。先 `(others => '0')` 把高位清零、再写低 3 位，能保证保留位恒为 0，避免读回时出现未定义的 `U`/`X`，也让未来的字段扩展不依赖当前未用的位。这是寄存器接口的常见稳健写法。

## 5. 综合实践

**任务**：用本讲三块积木（`Fsm_t` 类型、`case` 转移、`FsmToInt` 映射）做一次「给定配置，预测整条 FSM 轨迹与 STATUS 序列」的纸面推演，把控制流知识串起来。

情景：CPU 配置 `MODE = C_MODE_CONTINUOUS`、`PATTERN_SEL = C_PATTERN_SEL_COUNT`、`ADDR = 0x0000_0000`、`SIZE = 0x0000_0010`（16 字节），数据宽 32 位（`AxiDataWidth_g = 32`）。随后写 `START=1`；等观察到若干轮后写 `STOP=1`。

请完成：

1. **算 beat 数**：根据 [hdl/mem_test.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227) 的 `shift_right(RegSize_v, log2(AxiDataWidth_g/8))`，`log2(32/8)=2`，`16 >> 2 = 4`，即每轮写/读 4 个 beat。确认最后一拍判据为 `PatternCnt = 4-1 = 3`。
2. **预测首轮 FSM 轨迹**：写 `START` 后，`ContRunning` 被置 1（[hdl/mem_test.vhd:188-194](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L188-L194)）。轨迹：`Idle_s →(START) WrCmd_s →(CmdWr握手) Write_s`（写 4 拍，`PatternCnt` 0→3）`→ RdCmd_s →(CmdRd握手) Read_s`（读 4 拍，末拍 `ContRunning=1`）`→ WrCmd_s`（第 2 轮开始）。
3. **预测 STATUS 序列**（CPU 视角）：`IDLE(0) → WRITING(1) → READING(2) → WRITING(1) → READING(2) → ...` 反复交替，直到 STOP。
4. **推演 STOP 的优雅停止**：写 `STOP` 后 `ContRunning` 清 0（[hdl/mem_test.vhd:195-197](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L195-L197)），但当前轮的 `Read_s` 不会被打断；读到末拍时 `ContRunning=0`，走转移 #7 回 `Idle_s`。所以 STOP 之后还能看到最多一个完整的 `WRITING→READING`（如果当时正在写）或半个 `READING`（如果当时正在读），然后才 `IDLE`。
5. **预测 ITER 寄存器**：每轮末拍 `ContIter` 加 1（[hdl/mem_test.vhd:274](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L274)），回读 `ITER`（[hdl/mem_test.vhd:185](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L185)）得到已完成轮数。
6. **错误注入推演**：若第 2 轮读时主机报 `Rd_Error=1`，则 [hdl/mem_test.vhd:356-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L356-L358) 立刻把 `v.Fsm` 改成 `AxiError_s`，STATUS 变 `AXIERR(3)` 并锁死，CPU 再写 `START` 也无效（FSM 出不了陷阱），唯有复位。

**交付物**：一张「时间 → `r.Fsm` → STATUS 码 → `ContIter`」的四列表，覆盖「首轮→第二轮→STOP→回 Idle」全过程，并在末尾用一句话点出 `FsmToInt` 的合并映射如何让这条序列对 CPU 变得「粗而稳」。

> 提示：本实践是纯纸面推演，不需任何工具；若要用仿真核对每个状态的停留拍数与 STATUS 序列，可在 `tb/top_tb.vhd` 里复现上述配置后用 `axi_single_expect` 连续读 `STATUS` 寄存器，结果「待本地验证」。

## 6. 本讲小结

- `mem_test` 的控制逻辑是一台 7 态有限状态机 `Fsm_t`：主链路四态 `Idle_s/WrCmd_s/Write_s/RdCmd_s/Read_s`，加上 `Idle_s` 共五个正常态，另有两个错误陷阱 `AxiError_s/IntError_s`。
- 主链路是「写命令→写数据→读命令→读数据」的严格对称结构；每个命令态/数据态的推进都靠 valid/ready 握手（`r.Xxx_Vld='1' and Xxx_Rdy='1'`），命令态先注册一拍 valid 再检测 ready，是「注册式 valid」节奏。
- 模式分流集中在数据态末尾：`READONLY` 在 `Idle_s` 直跳 `RdCmd_s`（跳写）、`WRITEONLY` 在 `Write_s` 末拍直回 `Idle_s`（跳读）、`CONTINUOUS` 在 `Read_s` 末拍按 `ContRunning` 回跳 `WrCmd_s`（循环）。
- `STOP` 是「优雅停止」：只清 `ContRunning` 标志（[hdl/mem_test.vhd:195-197](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L195-L197)），不打断当前迭代，等本轮 `Read_s` 跑完才回 `Idle_s`。
- 错误处理是「故障锁定」：`Wr_Error/Rd_Error` 任意时刻把 FSM 拽进 `AxiError_s`、非法 pattern 拽进 `IntError_s`，case 之后的共享段（[hdl/mem_test.vhd:349-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L349-L358)）每拍把它们钉死，只有硬件复位能解锁。
- `FsmToInt`（[hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89)）把 7 个内部细状态合并成 6 个对外 STATUS 码（`WrCmd_s+Write_s→WRITING`、`RdCmd_s+Read_s→READING`），实现「内部复杂度对外不可见」；状态码 0/1/2/3/6/7 的跳变为将来预留了编码空间。

## 7. 下一步学习建议

本讲把状态机「怎么走」讲透了，接下来该看「每一拍的数据是什么」：

- **u3-l4 pattern 生成与校验**：深入 `InitPattern_v`/`UpdatePattern_v` 两个标志触发的四种 pattern 算法（Counter 递增、Walking-1 循环左移、OwnAddr 自身地址、PRBN 的 16 位 LFSR 反馈），以及 `Read_s` 里 `RdDat_Data /= r.Pattern` 的比对、`Errors` 累加、`FirstErrAddr` 由 `PatternCnt` 与基地址换算的实现。本讲里所有 `InitPattern_v := true` / `UpdatePattern_v := true` 的触发点，都在那一讲兑现成具体数值。
- **u4-l2 AXI4 主机命令/burst**：本讲里 `CmdWr_Size := shift_right(RegSize_v, log2(AxiDataWidth_g/8))`（[hdl/mem_test.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227)）把字节数折算成 beat 数，那一讲会补全它如何映射到主机 `psi_common_axi_master_simple` 的 AXI4 burst、`AxiMaxBeats_g` 如何决定一次命令拆成几个 burst。
- **u5-l1 仿真平台**：想知道本讲的 FSM 转移怎么在 testbench 里被验证、错误怎么被注入，就看 `tb/top_tb.vhd` 的控制进程与 AXI 仿真进程如何配合，把 `STATUS`、`ERRORS`、`FIRSTERR` 用 `axi_single_expect` 逐个校验。
