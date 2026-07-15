# I2C 主机（olo_intf_i2c_master）

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 `olo_intf_i2c_master` 的「命令式接口」工作方式：如何通过 `Cmd_*` 下达动作、通过 `Resp_*` 收回结果、通过 `Status_*` 观察总线状态。
- 读懂一段完整的 I2C 事务（START → 地址 → 数据 → ACK → STOP / 重复启动）在有限状态机（FSM）里的逐拍演化。
- 理解「多主仲裁」为何能靠开漏总线的线与（wired-AND）特性自动完成，以及代码在哪里检测仲裁失败。
- 理解「时钟拉伸」为何是 SCL 同样线与的结果，以及代码在哪里等待被从机压低的 SCL 释放。

本讲承接 u7-l1（`olo_intf_sync` 同步器、外部信号处理须电路与约束配套），是 intf 区域串行接口的第二站。

## 2. 前置知识

### 2.1 I2C 总线速览

I2C 是一种**两线、串行、半双工、多主**的总线，只有两根信号线：

- **SCL**：时钟线，由当前掌管总线的主机产生。
- **SDA**：数据线，主机与从机分时复用。

一次最简单的「向从机写一个字节」事务长这样：

```
主机发起      7位地址+R/W    从机应答    8位数据     从机应答    主机结束
[START] ────► 1010001 0 ────► ACK ────► 01000110 ──► ACK ────► [STOP]
```

其中：

- **START 条件**：SCL 为高时，SDA 由高变低。
- **STOP 条件**：SCL 为高时，SDA 由低变高。
- **重复启动（Repeated START）**：在不发 STOP 的情况下再发一次 START，用于「写后读」切换方向。
- **ACK/NACK**：每发完 8 位，接收方拉低 SDA 一个周期表示应答（ACK=低电平），不拉则表示不应答（NACK=高电平）。注意总线上的 ACK 是**低有效**。

### 2.2 开漏与线与（本讲最关键的物理事实）

I2C 的两根线都是**开漏（open-drain）**，设备只能把线「拉低」或「松手（高阻）」，**永远不能主动驱动为高**。线上的高电平完全靠外部上拉电阻提供。这带来一个重要性质——**线与**：

\[ \text{SDA}_{\text{bus}} = \text{SDA}_A \;\wedge\; \text{SDA}_B \;\wedge\; \cdots \]

即只要**任意**一个设备把线拉低，整条线就是低电平；只有所有设备都松手，线才是高电平。本讲的两个高级特性都建立在此之上：

- **多主仲裁**：两个主机同时发不同数据时，发「0」的一方会把线拉低，发「1」的一方虽松手却读到 0，于是知道自己输了。
- **时钟拉伸**：SCL 也是线与。从机若处理不过来，可在主机松手 SCL 后继续把它压低，主机读到 SCL 仍为低便原地等待，于是时钟被「拉长」。

### 2.3 「命令式接口」是什么

像 UART/SPI 那样「写一个寄存器就发一个字节」的接口很简单，但 I2C 有 START/STOP/重复启动/读写切换等多种「动作」，且每个动作要等上一个动作完成。因此本实体把每个总线动作抽象成一条**命令**，用户通过 AXI-S 握手一条条下发，实体每完成一条就回一个**响应**。这就像给总线当「指挥」：你下指令，它报结果。

> 术语提醒：本讲沿用 u7-l1 引入的「外部信号须同步」结论——I2C 的 SCL/SDA 是异步外部信号，进入 `Clk` 域前必须过同步器。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [olo_intf_i2c_master.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd) | 本讲主角：I2C 主机实体，含命令常量包、实体声明与 `rtl` 架构（FSM + 两进程法 + 三态缓冲 + 同步器实例）。 |
| [olo_intf_i2c_master.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md) | 官方文档：泛型/接口表与典型命令序列（两字节读、两字节写、写后读、仲裁失败）。 |
| [olo_intf_sync.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd) | 被实例化的 2 级同步器（u7-l1 讲过），把 SCL/SDA 同步进 `Clk` 域。 |
| [olo_intf_i2c_master_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd) | VUnit 测试台：用从机 VC 与「主机 VC」分别验证普通事务、写后读、时钟拉伸、命令超时、多主仲裁等各类场景。 |
| [olo_test_i2c_vc.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_i2c_vc.vhd) | I2C 验证组件（VC）：可扮从机也可扮「另一个主机」，是测试多主仲裁的关键。 |
| [olo_intf.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py) | 测试配置：为本实体按总线频率、三态模式、时钟分频等维度注册多组 generic 组合。 |

## 4. 核心概念与源码讲解

### 4.1 命令式接口：Cmd / Resp / Status 三套 AXI-S

#### 4.1.1 概念说明

`olo_intf_i2c_master` 对外暴露三组接口，分工明确：

- **命令接口 `Cmd_*`**：用户用它下达「下一个总线动作」。带 `Cmd_Ready`/`Cmd_Valid` 的标准 AXI-S 握手，支持反压。
- **响应接口 `Resp_*`**：每完成一条命令回一个响应，附带「收到的 ACK、是否丢仲裁、收到的数据、是否序列错误」。注意它**没有 `Ready`**——响应必须在当拍被取走。
- **状态接口 `Status_*`**：长期有效的总线状态（忙/闲）与偶发的超时脉冲。

一条命令与它对应的响应之间是严格一一对应的；并且文档明确：**上一条命令的响应一定在下一条命令必须被断言之前就给出**，因此用户可以「看响应再下命令」（例如丢仲裁后就不再继续）。

#### 4.1.2 命令编码与接口信号

命令用一个 3 位向量表示，编码在文件开头的包里集中定义，用户应当用具名常量而非魔数：

[olo_intf_i2c_master.vhd:32-40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L32-L40) —— 定义 `I2cCmd_Start_c/Stop_c/RepStart_c/Send_c/Receive_c` 五个命令码。

| 命令常量 | 编码 | 含义 | 合法时机 |
| :--- | :--- | :--- | :--- |
| `I2cCmd_Start_c` | `000` | 发 START | 仅总线空闲时 |
| `I2cCmd_Stop_c` | `001` | 发 STOP | 仅总线忙时 |
| `I2cCmd_RepStart_c` | `010` | 发重复启动 | 仅总线忙时 |
| `I2cCmd_Send_c` | `011` | 发一个数据字节 | 仅总线忙时 |
| `I2cCmd_Receive_c` | `100` | 收一个数据字节 | 仅总线忙时 |

命令接口的端口（含数据、ACK、可选运行时分频）见实体声明：

[olo_intf_i2c_master.vhd:74-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L74-L87) —— `Cmd_Command/Cmd_Data/Cmd_Ack/Cmd_ClkDiv` 与响应 `Resp_Command/Resp_Data/Resp_Ack/Resp_ArbLost/Resp_SeqErr`。

两个**极易踩坑的极性约定**：

- `Cmd_Ack`（接收时要回的应答）：`'1'` = 发 **ACK**，`'0'` = 发 **NACK**——与总线上 ACK 低有效正好**相反**。这样写代码时「想应答就给 1」更直觉。
- `Resp_Ack`（发送后收到的应答）：`'1'` = 收到 ACK，`'0'` = 收到 NACK——同样与总线相反。

> **地址不归实体管**：文档强调，发送含 7 位地址 + R/W 位的字节，靠的就是普通的 `I2cCmd_Send_c`，由用户自己把地址填进 `Cmd_Data` 的 bit7..1、R/W 填进 bit0；10 位地址也一样由用户用两次 `Send` 自行拼出（见 [olo_intf_i2c_master.md:22-27](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L22-L27)）。

#### 4.1.3 核心流程：命令如何在 FSM 中流动

实体用 Open Logic 全库通用的「两进程法 + record」（参见 u2-l2、u7-l2/u7-l3）：组合进程 `p_comb` 算下一拍状态 `r_next`，时序进程 `p_seq` 打拍并复位，所有寄存器收进 `TwoProcess_r` record。

[olo_intf_i2c_master.vhd:122-147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L122-L147) —— record 里既有 FSM 状态 `Fsm`、命令锁存 `CmdTypeLatch/CmdAckLatch`、移位寄存器 `ShReg`、位计数 `BitCnt`，也有分频计数器 `QuartPeriodCnt/ClkDivCnt/CmdClkDivLatch` 与各类超时计数器。

命令进入实体后被锁存，FSM 据此驱动 SCL/SDA，完成后回响应。关键状态机骨架如下（精简伪代码）：

```
BusIdle_s ──Start──► Start1_s ─► Start2_s ─► WaitCmd_s
                                              │
            ┌─────────────────────────────────┤  (用户下达下一条命令)
            ▼                ▼                ▼                ▼
        Stop1_s         RepStart1_s       DataBit1..4       (超时→Stop)
            │                │                │
           ...              ...            9 位后回 WaitCmd_s
            ▼
        BusIdle_s
```

其中 `WaitCmd_s` 是枢纽：每个字节/启动/停止动作做完后都回到它，等用户给下一条命令。命令到达后先去 `WaitLowCenter_s`（等 SCL 低电平中点）再分发到具体动作：

[olo_intf_i2c_master.vhd:366-390](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L366-L390) —— 在 SCL 低电平中点根据 `CmdTypeLatch` 分发到 `Stop1_s`/`RepStart1_s`/`DataBit1_s`。

#### 4.1.4 两个「防卡死」的超时（鲁棒性设计）

实体有两个泛型控制超时，体现「不让坏用户/坏邻居拖垮总线」的设计意图：

- `BusBusyTimeout_g`：若 SCL=1 且 SDA=1（总线实际空闲）持续这么久，就认定总线空闲。应对「某主机发了 START 后崩溃、没发 STOP」的悬空事务（[olo_intf_i2c_master.vhd:273-288](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L273-L288) 的 `BusBusy_s`）。
- `CmdTimeout_g`：在 `WaitCmd_s` 等用户下命令，若超时未给，实体**自动发 STOP 释放总线**并拉一个 `Status_CmdTo` 脉冲提醒用户（[olo_intf_i2c_master.vhd:203-215](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L203-L215) 与 [olo_intf_i2c_master.vhd:358-363](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L358-L363)）。这样即使用户逻辑卡住，总线也不会被一直霸占。

#### 4.1.5 代码实践：阅读一段真实命令序列

打开测试台里的 `WriteThenRead` 用例，它演示了「写后读」完整命令链：

[olo_intf_i2c_master_tb.vhd:286-308](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd#L286-L308) —— 先 `Start`，再 `Send(0x42)`（地址+W），再 `RepStart`，再 `Receive(Ack=0 即 NACK)`，最后 `Stop`。

实践目标：对照这段代码，把每一条 `pushCommand(...)` 翻译成「I2C 总线上发生的事」，并预测对应的 `checkResp(...)` 应期待什么。

操作步骤：

1. 在仓库根目录查看 `sim/test_configs/olo_intf.py` 中本实体的配置（[olo_intf.py:34-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L34-L45)），确认 `WriteThenRead` 用例会被多种 generic 组合覆盖。
2. 阅读上面这段 TB 代码，画出命令/响应对照表（参考文档 [olo_intf_i2c_master.md:160-176](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L160-L176)）。
3. 若本地已装好 VUnit + GHDL，可运行该单个用例（命令见第 5 节综合实践）。

预期结果：第 4 步的 `Receive` 收到 `0x36`、`Resp_Ack` 因发的是 NACK 不检查，`Resp_ArbLost=0`、`Resp_SeqErr=0`；最后 `Status_BusBusy` 回到 0。

> 运行结果待本地验证（取决于是否安装了 GHDL/NVC 与 VUnit）。

#### 4.1.6 小练习与答案

**练习 1**：为什么 `Resp_*` 接口没有 `Ready` 信号？

**答案**：因为响应是「每命令恰好一个、且在下一命令前必到」的确定流，实体不缓冲、不反压；用户必须在 `Resp_Valid='1'` 当拍取走。这简化了实体（无需内部 FIFO），代价是用户必须实时接收。文档对此有明确说明（[olo_intf_i2c_master.md:88-97](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L88-L97)）。

**练习 2**：用户在 `BusIdle_s` 时误发了 `I2cCmd_Send_c`，会发生什么？

**答案**：FSM 不切换状态，直接回一个 `Resp_Valid='1'` 且 `Resp_SeqErr='1'` 的响应（见 [olo_intf_i2c_master.vhd:262-265](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L262-L265)），仿真期还会用 `assert` 打印一条错误（被 `DisableAsserts_g` 控制）。

---

### 4.2 START / STOP / 重复启动 / 字节传输（含 ACK）

#### 4.2.1 概念说明

I2C 的四种「总线动作」靠 SCL/SDA 的电平时序区分。实体把每个动作拆成若干「四分之一 SCL 周期（quarter period）」的状态，逐拍驱动。先看 SCL 周期是怎么分成的。

实体把 `I2cFrequency_g`（默认 100 kHz）的**一个完整 SCL 周期均分为 4 个 quarter**，每个 quarter 占若干个 `Clk` 周期：

\[ N_{\text{quarter}} = \left\lceil \frac{f_{\text{Clk}}}{f_{\text{I2C}} \cdot 4} \right\rceil - 1 \quad\text{（Clk 周期数）} \]

对应代码常量：

[olo_intf_i2c_master.vhd:109-113](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L109-L113) —— 由 `ClkFrequency_g`、`I2cFrequency_g`、超时泛型推导出 `QuarterPeriodLimit_c` 等编译期常量。

文档要求 `ClkFrequency_g` 至少是 `I2cFrequency_g` 的 **16 倍**——这样每个 quarter 至少 4 个 `Clk` 周期，配合 2 级同步器的延迟，才能可靠地边沿检测与中点采样（见 [olo_intf_i2c_master.md:60](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L60)）。

quarter 计数器产生一个 `QPeriodTick` 节拍，FSM 用它推进状态：

[olo_intf_i2c_master.vhd:185-201](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L185-L201) —— quarter 计数；当 `ClkDivBits_g>0` 时还叠加一层运行时分频 `ClkDivCnt`（可选的 `Cmd_ClkDiv` 功能）。

#### 4.2.2 核心流程：四个动作的状态时序

源码在 FSM 各状态上方用 ASCII 波形注释画出了 SCL/SDA 时序（非常值得对照看）。下面用文字归纳。

**START 条件**（SCL 高时 SDA 由高到低）：用 `Start1_s`（SDA 仍高）→ `Start2_s`（SDA 拉低）两个状态实现，每个持续 1 quarter；之后进入 `WaitCmd_s`。

[olo_intf_i2c_master.vhd:306-331](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L306-L331) —— `Start1_s` 释放 SCL/SDA 为高、`Start2_s` 把 SDA 拉低形成下降沿，并在结束拍回 `Resp_Valid`。

**STOP 条件**（SCL 高时 SDA 由低到高）：三段 `Stop1_s`（SDA 低、SCL 低）→ `Stop2_s`（SCL 升高、SDA 仍低）→ `Stop3_s`（SDA 升高）形成上升沿：

[olo_intf_i2c_master.vhd:513-548](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L513-L548) —— 注意 `Stop3_s` 成功后才回 `Resp_Valid`，并且**若是命令超时触发的 STOP 则不回响应**（避免用户收到自己没下的命令的响应）。

**重复启动**：因为总线当前正忙（上一字节后 SCL 处于低），需先把 SDA 升高、再走一遍 START 时序，故多一个 `RepStart1_s`（SDA 升高、SCL 低），随后复用 `Start1_s/Start2_s`：

[olo_intf_i2c_master.vhd:401-412](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L401-L412) —— 注释明确「后续状态与普通 START 共享」。

**字节传输（8 数据位 + 1 ACK 位 = 9 位）**：用 `DataBit1..4` 四个状态循环 9 次。每个数据位占满 4 个 quarter（即一个 SCL 周期），其中：

- `DataBit1_s`：SCL 低的后半，**驱动** SDA（发送方放数据，接收方松手）。
- `DataBit2_s`：SCL 高，**中点采样** SDA 入移位寄存器。
- `DataBit3_s`：SCL 仍高（保持）。
- `DataBit4_s`：SCL 低的前半，位计数 `BitCnt++`，回到 `DataBit1_s`。

[olo_intf_i2c_master.vhd:425-503](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L425-L503) —— 9 位走完后在 `DataBit3_s` 回到 `WaitCmd_s` 并给响应。

#### 4.2.3 ACK 的收发：第 9 位的特殊处理

第 9 位（`BitCnt=8`）是应答位，方向与数据位相反，代码用 `BitCnt=8` 分支专门处理：

[olo_intf_i2c_master.vhd:425-453](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L425-L453) —— `DataBit1_s` 里：

- **发送（`Send`）时**：前 8 位把 `ShReg(8)` 送上 SDA；到第 9 位**松手 SDA**（`SdaOut:='1'`），把线让给从机回 ACK。
- **接收（`Receive`）时**：前 8 位**全程松手** SDA 让从机驱动；到第 9 位按 `CmdAckLatch` 决定回 ACK（拉低）或 NACK（松手）。

数据在中点被打入移位寄存器，9 位结束后 `Resp_Data = ShReg(8 downto 1)`、`Resp_Ack = not ShReg(0)`（移位寄存器最低位是采样到的 ACK，取反映极性约定）：

[olo_intf_i2c_master.vhd:223-230](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L223-L230) —— 响应默认值，`Resp_Ack`/`Resp_Data` 直接取自移位寄存器。

#### 4.2.4 开漏怎么用 VHDL 表达

实体永远不主动驱动 SCL/SDA 为高，只用三态缓冲模拟开漏：`SclOut='1'` 表示「松手（高阻）」，`SclOut='0'` 表示「拉低」。内部三态模式下：

[olo_intf_i2c_master.vhd:589-606](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L589-L606) —— `I2c_Scl <= 'Z' when r.SclOut='1' else '0';`（SDA 同理）。`InternalTriState_g=false` 时改用外部三态 `I2c_*_o/_t` 接口。读回总线真实电平再过同步器：

[olo_intf_i2c_master.vhd:632-645](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L632-L645) —— `to01X` 把 `'Z'/'H'` 规整后送入 `olo_intf_sync`（2 级），得到 `I2cScl_Sync/I2cSda_Sync`。FSM 全程只看这俩同步后的信号——这正是 u7-l1 同步器在此处的落地。

> 文档有一处重要工程提示：I2C 的三态引脚**无法做 scoped 时序约束**，必须手动约束（[olo_intf_i2c_master.md:52-54](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L52-L54)）。

#### 4.2.5 代码实践：跟踪一个字节的逐拍演化

实践目标：用源码注释里的 ASCII 波形，手工「跑」一遍发送字节 `0x42`（即 `0100_0010`，MSB 先发）的全过程。

操作步骤：

1. 阅读 [olo_intf_i2c_master.vhd:415-503](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L415-L503) 上方的波形注释，理解 `DataBit1..4` 与 SCL/SDA 的对应。
2. 在纸上为 9 个 `BitCnt`（0..8）各画一行，标出每个状态实体对 `SdaOut` 的取值（`Send` 模式下取 `ShReg(8)`，移位方向见 [olo_intf_i2c_master.vhd:455-460](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L455-L460)）。
3. 标出第 9 位（ACK）实体松手 SDA、由从机驱动的时刻。

预期结果：bit0..7 依次在 SCL 高电平中点送上 SDA 的是 `0,1,0,0,0,0,1,0`（0x42 的 MSB→LSB）；第 9 拍 SDA 由从机拉低（ACK）。`ShReg` 在每个 `DataBit2_s` 左移并入一位，9 位后 `ShReg(8 downto 1)=0x42`、`ShReg(0)=采到的 ACK`。

> 这是纯源码阅读型实践，无需运行即可完成。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `DataBit2_s` 在「SCL 高的中点」才采样 SDA，而不是 SCL 一升高就采？

**答案**：因为 SCL 升高后数据要经过 2 级同步器（2 个 `Clk` 周期延迟）才稳定可见；放在中点采样能避开边沿与同步延迟，拿到最稳定的值。这也解释了「`Clk` 必须 ≥16× I2C 频率」的约束——要给中点采样留足分辨率。

**练习 2**：重复启动（`RepStart`）与普通 `Start` 在状态机上有何不同？为什么？

**答案**：`RepStart` 多了一个前置状态 `RepStart1_s`，因为此时总线正忙、上一字节结束时 SCL/SDA 都在低，必须先把 SDA 升高、等 SCL 高，才能制造「SCL 高时 SDA 下降」的 START 时序；之后复用 `Start1_s/Start2_s`。普通 `Start` 起点是空闲（两线都高），无需这一步。

---

### 4.3 多主仲裁

#### 4.3.1 概念说明

多个主机可能同时想占用总线。I2C 规定：**仲裁靠 SDA 的线与自然完成，无需中央裁判**。原理是——发送数据位时，谁发「0」谁就把 SDA 拉低；发「1」的主机虽松手，却读到 SDA 为「0」，于是立刻知道自己输了（即「输出 1 却读到 0」）。因为两个主机若发的数据完全相同，就永远读不到冲突，于是总线被它们无害地共用，直到某一位不同才分出胜负。

关键推论：

- 仲裁**只在「发送」时发生**。接收时总线由从机驱动，而从机地址全局唯一，不存在两个主机抢同一条数据流。
- 仲裁失败的表征是 `Resp_ArbLost='1'`，FSM 进入 `ArbitLost_s` 然后回 `BusBusy_s`（让赢的主机先把事务做完）。

#### 4.3.2 核心流程：三个仲裁检测点

实体在**三个**可能丢仲裁的位置做检测。

**① 发送数据位期间**（最常见）：在 SCL 高的两个状态 `DataBit2_s` 与 `DataBit3_s` 里，只要「我发的是 1（`SdaOut='1'`），但总线读到 0」就判负：

[olo_intf_i2c_master.vhd:469-475](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L469-L475) 与 [olo_intf_i2c_master.vhd:490-496](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L490-L496) —— 条件 `(CmdTypeLatch=Send) and (BitCnt/=8)`：发送数据位才比，ACK 位（`BitCnt=8`）不比（因为该位本就由从机驱动，差异是正常的）。

**② START / 重复启动期间**：双方都试图发 START（都松手 SDA 高），若读到 SDA 已被对方拉低，说明对方先动手：

[olo_intf_i2c_master.vhd:317-320](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L317-L320) —— `Start1_s` 里 `I2cSda_Sync='0'` 即判负；`RepStart1_s` 同理（[olo_intf_i2c_master.vhd:406-409](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L406-L409)）。

**③ STOP 期间**：发 STOP 需把 SDA 升高（松手），若读到 SDA 仍为 0，说明对方还在拉低（继续传数据），自己这个 STOP 发不成：

[olo_intf_i2c_master.vhd:535-540](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L535-L540) —— `Stop3_s` 里 `I2cSda_Sync='0'` 判负。

判负后统一去 `ArbitLost_s` 收尾：

[olo_intf_i2c_master.vhd:551-557](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L551-L557) —— 回一个 `Resp_ArbLost='1'`、`Resp_Ack='0'` 的响应，然后退到 `BusBusy_s` 等对方完成。

#### 4.3.3 一个关键后果：丢仲裁后命令变非法

丢仲裁后实体不再拥有总线，因此用户**后续的命令（如 STOP/RepStart）会变成序列错误** `Resp_SeqErr='1'`。文档专门给了一个例子（[olo_intf_i2c_master.md:178-191](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L178-L191)）：第 3 条 `Send` 丢仲裁后，第 4 条 `RepStart` 因总线不归本机所有而返回 `SeqErr='1'`。测试台里 `MultiMaster-ArbLostWrite` 等用例正是这样断言的：

[olo_intf_i2c_master_tb.vhd:422-443](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd#L422-L443) —— 本机发 `0xA3`，对手 VC 主机发 `0x87`（更高位为 0，赢），本机 `Send` 收到 `ArbLost='1'`，随后 `Stop` 收到 `SeqErr='1'`。

#### 4.3.4 代码实践：读懂「另一个主机」是怎么模拟的

实践目标：理解测试台如何用一个 VC 扮演「抢总线的主机」来触发仲裁。

操作步骤：

1. 阅读 [olo_intf_i2c_master_tb.vhd:399-420](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd#L399-L420)（`MultiMaster-SameWrite`，双方发相同字节 `0x42`，不丢仲裁）。
2. 再读上面 4.3.3 引用的 `MultiMaster-ArbLostWrite`（双方发不同字节，高位不同者赢）。
3. 找出 VC 主机用的 API：`i2c_force_master_mode`、`i2c_push_tx_byte(..., delay => 100 ns)`、`i2c_push_stop`（声明见 [olo_test_i2c_vc.vhd:39-60](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/tb/olo_test_i2c_vc.vhd#L39-L60)）。

需要观察的现象：当两主机数据相同时，从机只收到一次该字节（线与无害）；当高位不同时，发高位为 1 的本机在第一个不同位立刻丢仲裁。

预期结果：与 `checkResp` 断言一致——同写不丢仲裁、异写在第一个不同位丢仲裁。

> 完整运行需本地 VUnit + 仿真器；纯阅读可立即完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么仲裁比较要排除 ACK 位（`BitCnt /= 8`）？

**答案**：发送方的第 9 位（ACK）必须**松手** SDA，让从机驱动；此时总线 SDA 必然与本机的「松手=1」不同（从机拉低表 ACK），若也比就会误判丢仲裁。故 ACK 位不参与仲裁。

**练习 2**：两个主机同时发完全相同的一串字节，最后谁赢？

**答案**：谁都不输——数据每一位都相同，线与后 SDA 与各自输出一致，永远检测不到差异，两机「并列」直到其中一方发 STOP/RepStart（SDA 升高动作）时才可能分出胜负；这正是 `Stop3_s` / `RepStart1_s` 仍要做仲裁检测的原因。

---

### 4.4 时钟拉伸

#### 4.4.1 概念说明

时钟拉伸（clock stretching）是慢速从机**拖慢主机**的机制：主机在 SCL 高电平阶段松手 SCL，期望它被上拉拉高；但若从机还没准备好，就**继续把 SCL 压低**。因为 SCL 也是线与，主机读到 SCL 仍为 0，于是原地等待，直到从机松手 SCL 才升起来。对主机而言，表现为「我松手了，但 SCL 迟迟不升高」。

实体支持时钟拉伸，做法很朴素：**在期望 SCL 为高的状态里，若读到 SCL 仍为 0，就把 quarter 计数器清零、原地不前进**，直到 SCL 真的升高。

#### 4.4.2 核心流程：清零计数器即「停表」

检测发生在三个 SCL 应为高的状态：

**① `DataBit2_s`**（数据位 SCL 高的中点，最常见拉伸点）：

[olo_intf_i2c_master.vhd:463-467](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L463-L467) —— `I2cScl_Sync='0'` 时清零 `QuartPeriodCnt` 与 `ClkDivCnt`，于是 `QPeriodTick` 永不来，FSM 卡在 `DataBit2_s` 不前进。

**② `Stop2_s`**（STOP 的 SCL 高阶段）：

[olo_intf_i2c_master.vhd:527-531](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L527-L531) —— 同样的清零逻辑。

**③ `Start1_s`**（仅重复启动时，因为普通 START 起点空闲、SCL 本就高，不会拉伸）：

[olo_intf_i2c_master.vhd:311-315](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L311-L315) —— 注释写明「Handle Clock Stretching in case of a repeated start」，且只在 `CmdTypeLatch=RepStart_c` 时检测。

注意一个**与仲裁的本质区别**：仲裁看的是 **SDA**（「我输出 1 却读到 0」→ 我输了）；时钟拉伸看的是 **SCL**（「我松手 SCL 却读到 0」→ 等对方松手）。两者都靠线与，但盯的线不同、含义不同。

#### 4.4.3 测试台如何注入拉伸

测试台的从机 VC 在每个动作上可指定 `clk_stretch => 2*Scl_Period_c`，模拟从机把 SCL 压低两个 SCL 周期：

[olo_intf_i2c_master_tb.vhd:311-334](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd#L311-L334) —— `ClockStretching` 用例：写、重复启动、读、停止四处全部带 `clk_stretch`，却仍能正常完成并收到正确数据 `0x46`。这证明实体的「停表」逻辑能让主机无限期等待从机释放 SCL。

#### 4.4.4 代码实践：给波形加注释

实践目标：把时钟拉伸在波形上的「SCL 凹槽」与代码里的清零动作对应起来。

操作步骤：

1. 阅读上面的 `ClockStretching` 用例，注意从机 VC 在字节、重复启动、停止上都注入了 `2*Scl_Period_c` 的拉伸。
2. 在源码 [olo_intf_i2c_master.vhd:455-467](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_i2c_master.vhd#L455-L467) 里指出：`DataBit2_s` 期望 SCL 高、却检测到 `I2cScl_Sync='0'` 时，是哪两行把计数器清零导致 FSM 原地等待。
3. 推理：若从机永不释放 SCL，主机会怎样？

预期结果：主机会**无限期停在 `DataBit2_s`**——实体没有针对「拉伸过长」的超时（这与 `CmdTimeout_g` 不同，后者只管用户不下命令）。所以工程上若担心从机死锁 SCL，需在外层加看门狗。这一点源码本身不提供，属「待本地验证/按需补充」的工程考量。

#### 4.4.5 小练习与答案

**练习 1**：时钟拉伸与多主仲裁都表现为「主机松手某根线后读到 0」，为何一个要等待、一个要放弃？

**答案**：因为盯的线不同。SCL 的线与意味着**任意**从机/主机都能压低它，主机读到 SCL=0 是正常的握手反馈（对方还没准备好），故等待；SDA 在发送数据位时，只有当**别的主机**发 0 才会被拉低，而这恰恰证明自己发的 1 输了，故放弃。两套逻辑互不混淆。

**练习 2**：普通 `Start`（非重复启动）为什么不做时钟拉伸检测？

**答案**：普通 `Start` 发起于总线空闲（`BusIdle_s`），此时 SCL 本就被动为高、没有从机在驱动事务，不存在拉伸场景；只有重复启动紧接在上一字节之后，从机可能仍在压低 SCL，才需要检测。

---

## 5. 综合实践

把四个最小模块串起来：**在仿真里完整跑一遍「向从机寄存器写后读」事务，并验证 START / 地址 / 数据 / ACK / 重复启动 / STOP 序列全部正确**。

本实践基于仓库自带的 VUnit 测试台与 I2C 从机 VC，不改动任何源码。

### 5.1 实践目标

- 用一条命令链完成：写一个字节到从机 → 重复启动切换为读 → 读回一个字节（末字节回 NACK）→ 停止。
- 在波形上确认：START、地址字节（含从机 ACK）、重复启动、接收字节、NACK、STOP 各就各位。
- 复盘：把命令、响应、总线事件三者的对应关系列成表。

### 5.2 操作步骤

1. **定位测试用例**：仓库已提供正好对应此场景的 `WriteThenRead` 用例（[olo_intf_i2c_master_tb.vhd:286-308](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_i2c_master/olo_intf_i2c_master_tb.vhd#L286-L308)）。先读懂它：
   - 从机 VC 期望序列：`expect_start` → `expect_rx_byte(0x42)` → `expect_repeated_start` → `push_tx_byte(0x36, NACK)` → `expect_stop`。
   - 主机命令序列：`Start` → `Send(0x42)` → `RepStart` → `Receive(Ack=0→NACK)` → `Stop`。

2. **确认配置**：本实体在 [olo_intf.py:34-45](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L34-L45) 注册了多组 generic（不同总线频率、内/外三态、是否启用运行时分频）。`WriteThenRead` 会在这些组合下被反复覆盖。

3. **运行该单个用例**（需本地已装 VUnit + GHDL）。在 `sim/` 目录下，参考 u1-l4 介绍的方式，用 `--ghdl` 并通过用例名过滤只跑这一个：

   ```bash
   cd sim
   python3 run.py --ghdl -v "*WriteThenRead*"
   ```

   > 上述命令基于 u1-l4 讲过的 `run.py` 用法；具体过滤语法以本地 `python3 run.py --help` 为准。若本地无仿真器，可跳到第 5 步做纯阅读实践。

4. **看波形**：若仿真器支持导出波形（GHDL 配合 GTKWave），观察 `I2c_Scl`/`I2c_Sda` 与 `Cmd_*`/`Resp_*` 的对应。

### 5.3 需要观察的现象与预期结果

| 阶段 | 总线事件 | 主机命令 | 期待响应 |
| :--- | :--- | :--- | :--- |
| 1 | START（SCL 高、SDA 下降） | `Start` | `Resp_Command=Start`, `ArbLost=0`, `SeqErr=0` |
| 2 | 发 `0x42`（地址+W），从机 ACK | `Send(0x42)` | `Resp_Ack=1`（收到 ACK） |
| 3 | 重复启动 | `RepStart` | `Resp_Command=RepStart`, `ArbLost=0` |
| 4 | 收 `0x36`，主机回 NACK | `Receive(Ack=0)` | `Resp_Data=0x36` |
| 5 | STOP（SCL 高、SDA 上升） | `Stop` | `Resp_Command=Stop` |

随后 `Status_BusBusy` 应回到 0，且全程不应出现 `Status_CmdTo` 脉冲（说明没有触发命令超时）。这与 TB 里 `checkResp(...)` 的断言逐条对应。

### 5.4 进阶（可选）

- 把 `WriteThenRead` 用例的思路**复制改造**成一个 TB 子用例（仅在本地实验，不要提交），把接收字节改为 2 个（第 1 个回 ACK、第 2 个回 NACK），对照文档「两字节读」序列（[olo_intf_i2c_master.md:128-142](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_i2c_master.md#L128-L142)）验证你的命令链。
- 给从机 VC 的每个动作都加 `clk_stretch => 2*Scl_Period_c`（仿照 `ClockStretching` 用例），确认即便全程拉伸，事务仍能正确完成。

> 上述运行结果取决于本地工具链；若未安装仿真器，第 5.2 的纯阅读部分仍可独立完成并产出命令/响应对照表。

## 6. 本讲小结

- `olo_intf_i2c_master` 用**命令式接口**（`Cmd_*` 下指令、`Resp_*` 收结果、`Status_*` 看状态）把 I2C 的 START/STOP/重复启动/收发抽象成一条条命令，命令与响应严格一一对应，且响应总在下一条命令前到达。
- 五种命令（`Start/Stop/RepStart/Send/Receive`）由状态机拆成「四分之一 SCL 周期」逐拍驱动；第 9 位是 ACK，发送方松手、接收方按 `Cmd_Ack` 驱动；`Cmd_Ack`/`Resp_Ack` 的极性与总线相反（写代码更直觉）。
- 实体用三态 `'Z'/'0'` 模拟**开漏**，只拉低不拉高；读回线经 `olo_intf_sync`（u7-l1）2 级同步后供 FSM 判断。
- **多主仲裁**靠 SDA 线与在发送数据位时自然完成（输出 1 却读到 0 即输），代码在数据位、START、STOP 三处检测，判负后回 `ArbitLost_s` 并使后续命令变 `SeqErr`。
- **时钟拉伸**靠 SCL 线与实现：在期望 SCL 高的状态里若读到 SCL=0 就清零 quarter 计数器「停表」等待，盯的是 SCL 而非 SDA，与仲裁互不混淆。
- 两个超时（`BusBusyTimeout_g`、`CmdTimeout_g`）保证总线不会被坏邻居或卡住的用户长期霸占，体现「Trustable Code」的鲁棒性设计。

## 7. 下一步学习建议

- **横向对比 intf 串行接口**：回头对比 u7-l2（UART，异步、无时钟线）与 u7-l3（SPI，同步、专有 CS/SCLK、无仲裁），体会 I2C 因「开漏 + 线与」而天生支持多主与拉伸的独特代价与收益。
- **深入验证体系**：本讲的从机/主机 VC（`olo_test_i2c_vc`）是 VUnit 验证组件的典型样本，建议进入 u10-l1 学习 VC 的命名约定与「用消息队列驱动激励」的写法。
- **继续 intf 区域**：若对 FPGA 测量外部时钟感兴趣，可读 `olo_intf_clk_meas`（u7-l1 已涉及），它与本实体一样依赖 `olo_intf_sync` 与跨时钟域脉冲传递。
- **约束实战**：本实体明确「I2C 三态引脚无法 scoped 约束、须手动约束」，可结合 u4-l1（跨时钟域约束）练习为 SCL/SDA 写 `set_max_delay`/`set_false_path` 约束。
