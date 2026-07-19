# I2C 主机 i2c_master

## 1. 本讲目标

本讲围绕 PSI 库中的 `psi_common_i2c_master` 组件，讲解一个**支持多主机仲裁（multi-master capable）的 I2C 主机**是如何用纯 VHDL 实现的。学完后你应当能够：

- 说清 I2C 总线上 START、STOP、重复 START、数据位、ACK 的电气与时序含义，并把它们对应到代码里的有限状态机（FSM）状态。
- 理解「系统时钟分频得到 I2C 时钟」的做法：用一个四分之一周期（quarter period）计数器生成节拍，而不是直接分频出方波。
- 看懂开漏（open-drain）总线如何用 VHDL 三态端口建模，并能根据 `internal_tri_state_g` 在「内部三态」与「外部三态（IOBUF 的 O/T）」两套物理接口间切换。
- 描述命令/响应/状态三组握手接口的时序，并能根据测试平台（TB）的激励预测一次单字节写传输的 SDA/SCL 波形。

本讲承接 **u6-l1（选通与节拍生成）**——这里的四分之一周期计数器本质就是一个产生周期性单周期脉冲的节拍生成器；同时承接 **u7-l1（二进程 record 设计法）**——整个 FSM 用 `r` / `r_next` + `p_comb` / `p_seq` 的二进程 record 法写成。

## 2. 前置知识

### 2.1 I2C 总线是什么

I2C（Inter-Integrated Circuit）是一种**两线串行总线**，用两根线连接多个芯片：

- **SCL**（Serial Clock）：时钟线。
- **SDA**（Serial Data）：数据线。

两根线都是**开漏（open-drain）/ 开集（open-collector）**结构：任何器件只能把线拉低（输出 0），不能把线驱动为高；线上的高电平靠**外部上拉电阻**实现。这一点非常关键，因为它带来两个后果：

1. **线与（wired-AND）**：总线上只要有一个器件拉低，线就是低；所有器件都松手，线才被上拉到高。多主机仲裁正是利用线与实现的。
2. **0 是主动驱动、1 是被动释放**：在代码里你会看到「输出 1」其实等于「把引脚置为高阻 `Z`（松手）」，「输出 0」才是真正驱动低电平。

### 2.2 I2C 的基本帧元素

| 元素 | 定义 |
|------|------|
| 空闲 | SCL=1 且 SDA=1（两线都被上拉为高） |
| START（起始） | SCL 为高时，SDA 由高变低 |
| STOP（停止） | SCL 为高时，SDA 由低变高 |
| 数据位 | SCL 为低时允许 SDA 改变；SCL 为高时采样 SDA（数据稳定） |
| ACK / NACK | 每发送 8 个数据位后，接收方回一个应答位：ACK=0（拉低 SDA），NACK=1（松手） |

一帧字节传输固定为 **9 个 SCL 脉冲**：8 个数据位（MSB 先发）+ 1 个应答位。器件地址（含读写位 R/W）并不特殊处理，用户自己把地址填进数据字节的高 7 位、R/W 填进 bit0 即可。

### 2.3 本讲用到的两个前置套路

- **节拍（strobe）生成**（u6-l1）：用一个计数器周期性地产生单周期宽的「点名」脉冲。本讲的四分之一周期节拍 `QPeriodTick` 就是同类思想。
- **二进程 record 设计法**（u7-l1）：所有寄存器收进一个 record `two_process_r`，用 `r` 表示当前态、`r_next` 表示次态；组合进程 `p_comb` 算次态、时序进程 `p_seq` 只负责打拍与复位。

如果你对这两点还不熟，建议先翻 u6-l1 和 u7-l1 再回来。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/psi_common_i2c_master.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd) | 本讲主角。文件内同时定义命令常量包 `psi_common_i2c_master_pkg`、实体 `psi_common_i2c_master` 与 `rtl` 架构（FSM + 三态接口 + 输入同步）。 |
| [hdl/psi_common_bit_cc.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_bit_cc.vhd) | 被 I2C 主机实例化的 2 级同步器，用于把异步的 SCL/SDA 输入同步到系统时钟域（见 u5-l2）。 |
| [testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd) | 自校验测试平台，内含一个软件 I2C 从机模型，覆盖写、读、时钟拉伸、命令超时、多主机仲裁等场景。本讲实践以其中的「Test Write」用例为依据。 |
| [doc/files/psi_common_i2c_master.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_i2c_master.md) | 官方组件说明，给出典型命令序列与接口表。 |

---

## 4. 核心概念与源码讲解

### 4.1 握手接口：命令/响应/状态三接口

#### 4.1.1 概念说明

`psi_common_i2c_master` 是一个**命令驱动**的组件：用户通过「命令接口」告诉它下一步要在总线上做什么（发 START、发字节、收字节、发 STOP……），它执行完后通过「响应接口」回报结果，同时用「状态接口」随时反映总线忙闲。

之所以把 I2C 的一次完整传输拆成一条条命令，是因为 I2C 帧的长度是**运行时动态**的（读几个字节、要不要重复 START，都由软件决定），不可能在综合期写死。于是组件暴露一个通用的「命令—响应」握手，由上层（通常是 AXI 寄存器映射的软件）按协议拼装命令序列。

命令用 3 位编码，组件用一个**同文件内的包** `psi_common_i2c_master_pkg` 把编码定义成具名常量，避免到处写魔法数字：

[hdl/psi_common_i2c_master.vhd:23-29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L23-L29) —— 定义 `CMD_START/CMD_STOP/CMD_REPSTART/CMD_SEND/CMD_REC` 五个命令常量。

#### 4.1.2 核心流程

三组接口的握手关系如下：

```
        命令接口 (用户 -> 主机)         响应接口 (主机 -> 用户)
        AXI-S 风格 VLD/RDY             每条命令恰好回一个响应
 ┌────┐  cmd_vld_i  ┌──────┐          rsp_vld_o   ┌────┐
 │用户│ ──────────> │I2C   │         ───────────> │用户│
 │    │  <────────── │主机  │                      │    │
 │    │  cmd_rdy_o   │  FSM │                      │    │
 │    │  cmd_type_i  │      │  rsp_type/dat/ack/   │    │
 │    │  cmd_dat_i   │      │  arb_lost/seq        │    │
 │    │  cmd_ack_i   └──────┘                      │    │
 └────┘                 状态接口                    └────┘
                     bus_busy_o, timeout_cmd_o
```

要点：

- **命令接口**是 AXI-S 风格握手：`cmd_rdy_o` 与 `cmd_vld_i` 同高那一拍，命令被接收。命令的附加数据随命令一起给：`cmd_type_i`（命令类型）、`cmd_dat_i`（要发送的字节，仅 `CMD_SEND` 用）、`cmd_ack_i`（接收时回的 ACK/NACK，仅 `CMD_REC` 用）。
- **响应接口**没有 RDY，只有 `rsp_vld_o`：每条被接收的命令执行完，主机拉一拍 `rsp_vld_o`，并附上 `rsp_type_o`（哪种命令完成了）、`rsp_dat_o`（收到的字节，仅 `CMD_REC`）、`rsp_ack_o`（1=收到 ACK，0=NACK）、`rsp_arb_lost_o`（仲裁失败）、`rsp_seq_o`（命令序列非法）。
- **状态接口**持续输出：`bus_busy_o`（总线忙，可能被本主机或别的主机占用）、`timeout_cmd_o`（因用户超时未给命令而自动释放总线时，拉一拍脉冲）。

#### 4.1.3 源码精读

实体端口声明把这三组接口清晰分组，命名严格遵守库规范（`_i/_o` 后缀、共同前缀）：

[hdl/psi_common_i2c_master.vhd:52-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L52-L74) —— 命令/响应/状态接口端口。

命令的锁存发生在 `p_comb` 里：当 `cmd_rdy_o='1'` 且 `cmd_vld_i='1'` 时，把命令类型与 ACK 选择锁存进 record：

[hdl/psi_common_i2c_master.vhd:184-188](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L184-L188) —— 锁存命令类型与 ACK。

响应默认每拍清零，只在某个 FSM 状态完成命令时才拉一拍 `rsp_vld_o`：

[hdl/psi_common_i2c_master.vhd:190-197](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L190-L197) —— 响应信号默认值；注意 `rsp_ack_o` 默认取 `not r.ShReg(0)`，即移位寄存器最低位取反映射成「1=ACK」。

组件对命令序列有**合法性校验**：总线空闲时只接受 `CMD_START`，传输进行中（`WaitCmd_s`）只接受 STOP/REPSTART/SEND/REC。非法命令不执行，直接回一个 `rsp_seq_o='1'` 的响应，并用 `assert` 打印 `###ERROR###`（与 u1-l3 的统一报错约定一致）：

[hdl/psi_common_i2c_master.vhd:214-231](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L214-L231) —— `BusIdle_s` 状态：只允许 `CMD_START`，否则报序列错误。

#### 4.1.4 代码实践

**实践目标**：熟悉命令/响应握手的最小用法，并验证「每条命令恰好一个响应」。

**操作步骤**：

1. 打开测试平台，找到「Test Write」用例（`StimCase <= 2`）。
2. 阅读 TB 提供的 `ApplyCmd` 与 `CheckRsp` 两个过程。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:95-114](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L95-L114) —— `ApplyCmd`：拉 `cmd_vld_i`，等 `cmd_rdy_o` 握手那一拍，完成 AXI-S 命令递交。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:116-141](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L116-L141) —— `CheckRsp`：等 `rsp_vld_o` 那一拍，比对响应类型/数据/ACK/仲裁/序列错误。

3. 观察用例中的三行：`ApplyCmd(CMD_START,...)` → `CheckRsp(CMD_START,...)`、`ApplyCmd(CMD_SEND, X"A3",...)` → `CheckRsp(CMD_SEND,...,Ack='1')`、`ApplyCmd(CMD_STOP,...)` → `CheckRsp(CMD_STOP,...)`。

**需要观察的现象 / 预期结果**：每发一条 `ApplyCmd`，必定紧跟一条 `CheckRsp`，且响应类型与命令类型一一对应；`CMD_SEND 0xA3` 后从机回 ACK，故 `rsp_ack_o` 应为 `'1'`。这印证了「命令—响应」严格 1:1 的契约。

#### 4.1.5 小练习与答案

**练习 1**：如果用户在 `WaitCmd_s` 状态下递交一条 `CMD_START`，会发生什么？
**答案**：FSM 检测到 `WaitCmd_s` 下不接受 `CMD_START`（见 [hdl/psi_common_i2c_master.vhd:307-317](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L307-L317)），命令不执行，直接回一个 `rsp_vld_o='1'`、`rsp_seq_o='1'` 的「序列错误」响应，同时 `assert` 打印 `###ERROR###`。

**练习 2**：为什么响应接口没有 `rsp_rdy_i`（即没有反向 RDY）？
**答案**：因为响应是确定的、每命令恰好一个的低速事件（最短间隔也是一个 9 位的 I2C 字节时间，远大于一个系统时钟周期），上游有充裕时间接收；省掉 RDY 简化了接口，用户只需在 `rsp_vld_o` 拉高那一拍采样即可。

---

### 4.2 时钟分频：四分之一周期节拍计数器

#### 4.2.1 概念说明

I2C 的 SCL 频率（如 100 kHz、400 kHz、1 MHz）远低于 FPGA 系统时钟（如 125 MHz）。如何从系统时钟得到 I2C 时序？

初学者可能想直接分频出一个 100 kHz 的方波 SCL。但本组件**没有**这样做。它的做法是：把一个 I2C SCL 周期切成 **4 个等长的「四分之一周期」**，用一个计数器每数到上限就产生一个单周期脉冲 `QPeriodTick`，FSM 每收到一个 `QPeriodTick` 就推进一个状态。这样 SCL/SDA 的电平变化都精确对齐到四分之一周期边界，从而能严格满足 I2C 对 START/STOP/数据建立与保持时间的要求。

这与 u6-l1 讲的 strobe_generator 思想一致——**用计数器生成周期性单周期脉冲作为节拍**——只是这里的节拍粒度是「四分之一 SCL 周期」。

#### 4.2.2 核心流程

设系统时钟频率为 \( f_{\text{clk}} \)、期望 I2C 频率为 \( f_{\text{i2c}} \)。一个完整 SCL 周期含 4 个四分之一周期，故每个四分之一周期应包含的系统时钟数为

\[
N_{\text{quarter}} = \left\lceil \frac{f_{\text{clk}}}{f_{\text{i2c}} \cdot 4} \right\rceil
\]

计数器从 0 数到 \( N_{\text{quarter}}-1 \)，回零同时拉一拍 `QPeriodTick`。所以代码里的常量是「上限值」：

\[
\text{QuarterPeriodLimit\_c} = N_{\text{quarter}} - 1 = \left\lceil \frac{f_{\text{clk}}}{f_{\text{i2c}} \cdot 4} \right\rceil - 1
\]

实际生成的 SCL 周期 \( T_{\text{SCL}} = 4 \cdot N_{\text{quarter}} / f_{\text{clk}} \)。由于 `ceil` 向上取整，**实际频率只会略低于或等于期望频率**，不会偏快，符合 I2C 对最高频率的限制。

举例：\( f_{\text{clk}}=125\,\text{MHz} \)、\( f_{\text{i2c}}=100\,\text{kHz} \)，则

\[
N_{\text{quarter}} = \lceil 125\text{e}6 / 100\text{e}3 / 4 \rceil = \lceil 312.5 \rceil = 313
\]

\( T_{\text{SCL}} = 4 \times 313 / 125\text{e}6 \approx 10.016\,\mu\text{s} \)，即约 99.84 kHz，略低于 100 kHz。

#### 4.2.3 源码精读

三个关键计数上限常量在架构说明区用实数 generic 直接算出，编译期求值，不产生硬件：

[hdl/psi_common_i2c_master.vhd:89-92](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L89-L92) —— `BusyTimoutLimit_c`（总线空闲超时上限）、`QuarterPeriodLimit_c`（四分之一周期上限）、`CmdTimeoutLimit_c`（命令超时上限）。注意 `QuarterPeriodLimit_c` 用了 `ceil`，对应上面公式里的向上取整。

四分之一周期计数器与节拍脉冲的生成在 `p_comb` 中。需要注意注释的提醒：**FSM 在某些状态下会强行改写这个计数器**（例如做时钟拉伸时把它清零），所以这段逻辑会被后面的 FSM 分支覆盖：

[hdl/psi_common_i2c_master.vhd:158-168](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L158-L168) —— 四分之一周期计数：`BusIdle_s`/`BusBusy_s` 时清零（空闲不计节拍），否则数到上限回零并拉一拍 `QPeriodTick`。

节拍脉冲随后被 FSM 各状态用来推进状态机，例如 START 条件的第一个四分之一周期：

[hdl/psi_common_i2c_master.vhd:272-288](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L272-L288) —— `Start1_s`：每来一个 `QPeriodTick` 才推进到 `Start2_s`；同一段还演示了「FSM 改写计数器」——遇到时钟拉伸（`I2cScl_Sync='0'`）时把 `QuartPeriodCnt` 清零，等于暂停节拍、原地等待。

#### 4.2.4 代码实践

**实践目标**：验证时钟分频公式，预测仿真中的 SCL 周期。

**操作步骤**：

1. 读 TB 顶部的 generic 设置：`clock_frequency_g = 125.0e6`、`i2c_frequency_g = 1.0e6`。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:43-48](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L43-L48) —— TB 用 125 MHz 系统时钟、1 MHz I2C 频率。

2. 代入公式手算：\( N_{\text{quarter}} = \lceil 125\text{e}6/1\text{e}6/4 \rceil = \lceil 31.25 \rceil = 32 \)，故 `QuarterPeriodLimit_c = 31`，SCL 周期 \( = 4 \times 32 / 125\text{e}6 = 1.024\,\mu\text{s} \)（约 976.6 kHz）。
3. 在 `sim/config.tcl` 中找到 `psi_common_i2c_master_tb` 的运行条目（见 u1-l3 的注册方式），按 u1-l3 的 Modelsim/GHDL 流程跑一次该 TB。
4. 在波形窗口测量相邻两个 SCL 上升沿之间的时间。

**需要观察的现象 / 预期结果**：测得的 SCL 周期应约为 1.024 µs（待本地验证具体量化值），与手算一致。这验证了「四分之一周期节拍 ×4 = 一个 SCL 周期」的关系。

#### 4.2.5 小练习与答案

**练习 1**：把 `i2c_frequency_g` 从 100 kHz 改成 400 kHz（系统时钟仍 125 MHz），`QuarterPeriodLimit_c` 等于多少？实际 SCL 频率是多少？
**答案**：\( N_{\text{quarter}}=\lceil 125\text{e}6/400\text{e}3/4\rceil=\lceil 78.125\rceil=79 \)，`QuarterPeriodLimit_c=78`；实际 \( T_{\text{SCL}}=4\times79/125\text{e}6=2.528\,\mu\text{s} \)，约 395.6 kHz，略低于 400 kHz。

**练习 2**：为什么用 `ceil`（向上取整）而不是 `round` 或 `floor`？
**答案**：向上取整让每个四分之一周期**不少于**理想时长，保证实际 SCL 频率**不高于**设定值；I2C 协议对最高频率有硬限制（标准模式 100 kHz、快速模式 400 kHz），偏慢安全、偏快违规，所以必须向上取整。

---

### 4.3 I2C 帧时序：START / 数据位 / ACK / STOP / 重复 START

#### 4.3.1 概念说明

这是本讲的核心。组件用一个 16 态的 FSM 把 I2C 帧的每一个电气事件映射到一段状态序列，每段序列由若干个四分之一周期组成（见 4.2）。关键在于：**SDA 只允许在 SCL 为低时变化，在 SCL 为高时必须稳定**；而 START/STOP 是这条规则的**唯一例外**——它们恰恰是「SCL 为高时 SDA 跳变」。

理解下面这张「状态 ↔ SCL/SDA 电平」对应表，就掌握了整个时序生成机制。组件用两个寄存器 `SclOut`、`SdaOut` 描述「本主机想驱动 SCL/SDA 为什么」，再经 4.4 的三态缓冲转成实际总线电平。

#### 4.3.2 核心流程

##### START 条件（占用 2 个四分之一周期）

```
状态:      Start1_s      Start2_s     WaitCmd_s
SCL: ______|█████████████|█████████████|____________   (SclOut=1, =1, =0)
SDA: ______|█████████████|____________|____________   (SdaOut=1, =0, =0)
                          ^
                     SCL高时SDA下降 = START
```

- `Start1_s`：SCL=1，SDA=1（保持空闲高电平一个四分之一周期）。
- `Start2_s`：SCL=1，SDA=0（SCL 仍高时把 SDA 拉低 → 产生 START）。
- 随后进入 `WaitCmd_s`：SCL=0（进入第一个 SCL 低电平）。

##### STOP 条件（占用 3 个四分之一周期）

```
状态:      Stop1_s       Stop2_s      Stop3_s      BusIdle_s
SCL: ______|_____________|█████████████|█████████████|____________  (=0, =1, =1, =1)
SDA: ______|_____________|_____________|█████████████|____________  (=0, =0, =1, =1)
                                        ^
                                  SCL高时SDA上升 = STOP
```

- `Stop1_s`：SCL=0，SDA=0（先把 SDA 拉到低，为上升做准备）。
- `Stop2_s`：SCL=1，SDA=0（SCL 升高，SDA 仍低）。
- `Stop3_s`：SCL=1，SDA=1（SCL 仍高时松开 SDA → 上拉使其上升 → 产生 STOP）。

##### 数据位（每位 4 个四分之一周期 = 1 个 SCL 周期）

每个数据位经历 `DataBit1_s → DataBit2_s → DataBit3_s → DataBit4_s` 四个状态，分别对应「SCL 低的后半 → SCL 高的前半 → SCL 高的后半 → SCL 低的前半」：

```
状态:   DataBit1_s  DataBit2_s  DataBit3_s  DataBit4_s
SCL: ___|____________|████████████|████████████|____________   (=0,=1,=1,=0)
SDA: ___|<-- 本位值稳定建立 -->|<-- 高电平期间被采样 -->|...
```

- `DataBit1_s`：SCL=0（低的后半），**设置** SDA 为本位的值（SDA 在 SCL 低时变化）。
- `DataBit2_s`：SCL=1（高的前半），**采样** SDA：把 `I2cSda_Sync` 移入移位寄存器；同时做仲裁与时钟拉伸检测。
- `DataBit3_s`：SCL=1（高的后半），数据保持稳定。
- `DataBit4_s`：SCL=0（低的前半），位计数 `BitCnt` 加 1，准备下一位。

一帧固定 9 位：`BitCnt` 0..7 是数据（MSB 先发），`BitCnt=8` 是应答位。**第 9 位（ACK）结束后不经过 `DataBit4_s`，而是直接从 `DataBit3_s` 跳回 `WaitCmd_s` 并回响应**。

移位寄存器 `ShReg` 共 9 位：

- 发送 `CMD_SEND` 时，在 `WaitCmd_s` 把 `ShReg` 装载为 `cmd_dat_i & '0'`，即数据在 `ShReg(8 downto 1)`、最低位补 0；每位从 `ShReg(8)`（MSB）取出移出。
- 接收时，每位在 `DataBit2_s` 执行 `ShReg <= ShReg(7 downto 0) & I2cSda_Sync`，8 次移位后收到的字节落在 `ShReg(8 downto 1)`，恰好作为 `rsp_dat_o`；第 9 位移入的是从机的 ACK/NACK，落在 `ShReg(0)`，经取反得到 `rsp_ack_o`（0→ACK=1，1→NACK=0）。

##### 重复 START（REPSTART）

重复 START 是「不先发 STOP 就再发一次 START」，用于一次传输中切换读写方向。代码先用 `RepStart1_s` 把 SDA 拉高（SCL 低时），再复用普通 START 的 `Start1_s/Start2_s` 完成实际的 START 跳变。

##### 两个健壮性机制

- **时钟拉伸（clock stretching）**：从机可把 SCL 拉低以暂停主机。代码在 SCL 高电平阶段（`Start1_s`/`DataBit2_s`/`Stop2_s`）检测 `I2cScl_Sync='0'`，一旦发现就把 `QuartPeriodCnt` 清零，**冻结节拍、原地等待**，直到从机释放 SCL。
- **多主机仲裁**：I2C 是线与总线，若主机发 1 却在线上读到 0，说明别的主机在发 0，本主机败出。代码在 `CMD_SEND` 的数据位（不含 ACK 位）期间检测 `I2cSda_Sync /= r.SdaOut`，败出即跳 `ArbitLost_s`，回 `rsp_arb_lost_o='1'`；START/REPSTART/STOP 阶段也有类似检测。

#### 4.3.3 源码精读

FSM 状态枚举（共 16 态）：

[hdl/psi_common_i2c_master.vhd:94-96](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L94-L96) —— `Fsm_t` 枚举：总线空闲/忙、START、等待命令、STOP、重复 START、数据位 1~4、仲裁失败等。

START 条件两态：

[hdl/psi_common_i2c_master.vhd:272-296](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L272-L296) —— `Start1_s`（SCL=1,SDA=1）→ `Start2_s`（SCL=1,SDA=0）。`Start1_s` 里同时处理重复 START 的时钟拉伸与 START 仲裁失败。

数据位四态与移位/采样/仲裁：

[hdl/psi_common_i2c_master.vhd:390-418](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L390-L418) —— `DataBit1_s`：SCL=0，按发送/接收与是否 ACK 位决定 `SdaOut`（发送时输出 `ShReg(8)`，接收时松手为 1；ACK 位时发送方松手、接收方按 `CmdAckLatch` 回 ACK/NACK）。

[hdl/psi_common_i2c_master.vhd:420-439](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L420-L439) —— `DataBit2_s`：SCL=1，把 SDA 采样移入 `ShReg`；处理时钟拉伸；发送数据位时做仲裁检测。

[hdl/psi_common_i2c_master.vhd:441-467](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L441-L467) —— `DataBit3_s`（SCL=1，第 9 位即 BitCnt=8 时结束本字节并回响应，否则进 `DataBit4_s`）与 `DataBit4_s`（SCL=0，`BitCnt` 加 1）。

STOP 条件三态与 STOP 仲裁：

[hdl/psi_common_i2c_master.vhd:479-510](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L479-L510) —— `Stop1_s`（SCL=0,SDA=0）→ `Stop2_s`（SCL=1,SDA=0，含时钟拉伸处理）→ `Stop3_s`（SCL=1,SDA=1，产生 STOP；若 SDA 被别的主机拉低则仲裁失败）。

重复 START 入口：

[hdl/psi_common_i2c_master.vhd:363-374](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L363-L374) —— `RepStart1_s`：SCL=0 时把 SDA 拉高，之后复用 `Start1_s` 完成实际 START。

仲裁失败统一处理：

[hdl/psi_common_i2c_master.vhd:516-522](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L516-L522) —— `ArbitLost_s`：松开总线（SCL=SDA=1），回 `rsp_arb_lost_o='1'`，回到 `BusBusy_s`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：根据源码 FSM，描述（画出）一次「单字节写」传输的 SDA/SCL 波形，并与仿真核对。

**操作步骤**：

1. 单字节写的命令序列是 `CMD_START → CMD_SEND(0xA3) → CMD_STOP`，对应 TB「Test Write」用例的前三步。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:266-272](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L266-L272) —— TB 激励：START → SEND 0xA3 → STOP。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:505-507](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L505-L507) —— 从机模型期望收到 0xA3 并回 ACK（第 5 个参数 `'0'` = ACK）。

2. 在纸上按 4.3.2 的状态表，逐个四分之一周期画出 SCL/SDA：
   - START：2 个四分之一周期（SCL 高、SDA 高→低）。
   - 字节 0xA3 = `1010_0011`（MSB 先发）：8 个数据位 ×4 + 1 个 ACK 位 ×3 ≈ 共 9 位。
   - ACK 位：从机回 ACK，即主机在 ACK 位松手（SDA=1），从机把 SDA 拉低。
   - STOP：3 个四分之一周期（SCL 升高后 SDA 由低升高）。
3. 跑该 TB 用例（按 u1-l3 的 Modelsim/GHDL 流程），在波形里展开 `i2c_scl_io`、`i2c_sda_io`。

**需要观察的现象 / 预期结果**：

- 起始处 SCL 高电平期间出现 SDA 下降沿（START）。
- 0xA3 的 8 个数据位在 SCL 高电平被采样时依次为 1,0,1,0,0,0,1,1。
- 第 9 个 SCL 脉冲（ACK）期间 SDA 被从机拉低（ACK）。
- 结束处 SCL 高电平期间出现 SDA 上升沿（STOP）。
- `rsp_ack_o` 在 `CMD_SEND` 的响应中为 `'1'`（收到 ACK）。

波形的具体电平持续时间待本地验证，但上述事件顺序应与源码 FSM 严格一致。

#### 4.3.5 小练习与答案

**练习 1**：一次 `CMD_SEND` 字节传输在总线上产生多少个 SCL 脉冲？为什么？
**答案**：9 个。8 个数据位 + 1 个应答位，每位的 `DataBit2_s/DataBit3_s`（SCL 高）构成一个 SCL 正脉冲，共 9 个；见 [hdl/psi_common_i2c_master.vhd:441-450](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L441-L450)（`BitCnt=8` 即第 9 位结束）。

**练习 2**：`CMD_REC`（接收）时，主机在第 9 位应输出什么？由哪个信号决定？
**答案**：主机按用户给的 `cmd_ack_i`（锁存为 `CmdAckLatch`）决定：`CmdAckLatch='1'` 时拉低 SDA 发 ACK，`'0'` 时松手发 NACK；见 [hdl/psi_common_i2c_master.vhd:407-417](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L407-L417)。通常读最后一字节时发 NACK，通知从机停止。

**练习 3**：为什么数据位的采样（移位）放在 `DataBit2_s`（SCL 高的前半），而不是 SCL 一变高就立刻采？
**答案**：放在高电平的中段采样，能保证 SDA 已满足建立时间、且离下降沿还有半个高电平的保持时间裕量，是最稳的采样点；同时也给「读取总线实际电平做仲裁判断」留出时间。

---

### 4.4 三态驱动与物理接口

#### 4.4.1 概念说明

I2C 的 SCL/SDA 是开漏线，必须用**三态**端口建模：输出 0 时驱动低、输出 1 时高阻（松手让上拉电阻工作）。不同 FPGA 平台对三态的处理方式不同，因此组件用 generic `internal_tri_state_g` 提供两套接口：

- **内部三态（`internal_tri_state_g = true`，默认）**：直接用 VHDL 的 `inout` 端口 `i2c_scl_io`/`i2c_sda_io`。综合器自动把它映射到 FPGA 引脚上的三态缓冲。适合大多数 FPGA 顶层直连。
- **外部三态（`internal_tri_state_g = false`）**：不使用 `inout`，而是拆成三个单向端口——输出 `i2c_scl_o`、输入 `i2c_scl_i`、三态控制 `i2c_scl_t`（`1`=高阻、`0`=驱动）。这正是 Xilinx IOBUF 原语的 `(O, I, T)` 接口形态，便于在顶层例化一个显式 IOBUF，或用于 ASIC 流程。

此外，由于 SCL/SDA 相对系统时钟是**异步信号**（别的主机、从机随时可能驱动总线），组件实例化了一个 2 级同步器 `psi_common_bit_cc`（u5-l2 讲过的 `bit_cc`）把读回的 SCL/SDA 同步到系统时钟域，避免亚稳态。

#### 4.4.2 核心流程

```
          SclOut/SdaOut (FSM 想要的电平, 1=松手/高, 0=驱动低)
               │
   ┌───────────┴───────────┐
   │ internal_tri_state_g? │
   └───────────┬───────────┘
       true ↓             ↓ false
  i2c_scl_io:           i2c_scl_o = SclOut
    'Z' if SclOut=1     i2c_scl_t = SclOut   (1=三态/松手, 0=驱动)
    '0' if SclOut=0     i2c_scl_i ← 外部 IOBUF 回读
         │
         ↓ (总线实际电平)
  to_01X → psi_common_bit_cc (2级同步) → I2cScl_Sync (回 FSM 判断)
```

注意「输出 1」与「高阻」等价：当 `SclOut=1` 时，内部三态把引脚置 `Z`（外部上拉拉高），外部三态把 `t=1`（IOBUF 高阻）——两者物理效果都是「总线为高（被上拉）」。当 `SclOut=0` 时，两者都驱动低电平。

#### 4.4.3 源码精读

两套三态接口的 `generate` 分支：

[hdl/psi_common_i2c_master.vhd:552-567](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L552-L567) —— `g_intTristate`：`SclOut=1` 时 `i2c_scl_io<='Z'`，否则 `'0'`（开漏建模）；`g_extTristatte`：`i2c_scl_o<=SclOut`、`i2c_scl_t<=SclOut`，即 IOBUF 的 O/T 接口。

读回输入的选择与归一化：

[hdl/psi_common_i2c_master.vhd:593-594](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L593-L594) —— 内部三态时从 `i2c_scl_io` 读、外部三态时从 `i2c_scl_i` 读；统一过 `to_01X`（u2-l2）把九值逻辑归一成 `{0,1,X}`，避免 `'Z'`/`'H'` 等进入比较逻辑。

2 级同步器例化（同步 SCL 与 SDA 两根线）：

[hdl/psi_common_i2c_master.vhd:596-606](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L596-L606) —— 实例化 `psi_common_bit_cc`（`width_g=>2`），把 `I2cScl_Input/I2cSda_Input` 同步成 `I2cScl_Sync/I2cSda_Sync`，FSM 全程只用同步后的版本。

TB 里两种三态配置各跑一遍（与 u1-l3 的 generic 组合回归方法一致），并且 TB 自己也实现了外部三态的 IOBUF 模型与上拉模型：

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:34-36](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L34-L36) —— TB 顶层 generic `internal_tri_state_g`，用于回归两套接口。

[testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:185-191](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L185-L191) —— TB 用 `I2cPullup` 模拟上拉电阻，并在外部三态模式下用 `t/o` 重建 `i2c_scl_io`。

#### 4.4.4 代码实践

**实践目标**：理解两种三态配置的端口差异，学会在顶层正确连线。

**操作步骤**：

1. 对照实体声明，分别列出两种配置下「哪些端口有效、哪些应悬空」。

[hdl/psi_common_i2c_master.vhd:75-84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L75-L84) —— `inout` 与 `i/o/t` 两套物理端口并存于实体。

2. 在 `sim/config.tcl` 中找到 `psi_common_i2c_master_tb` 的注册条目，确认它对 `internal_tri_state_g` 取了 true/false 两个运行组合（与 u1-l3 的 `create_tb_run`/`tb_run_add_arguments` 用法一致）。
3. 想象你要把这个组件放进一个 Xilinx 顶层：选择 `internal_tri_state_g => false`，并把 `i2c_scl_i/o/t` 连到一个 `IOBUF` 端口的 `I/O/T`。

**需要观察的现象 / 预期结果**：

- `internal_tri_state_g=true`：只接 `i2c_scl_io`/`i2c_sda_io` 两个 `inout`，其余 `i2c_scl_*` 端口可不连。
- `internal_tri_state_g=false`：接 `i2c_scl_i/o/t` 与 `i2c_sda_i/o/t`，`i2c_scl_io`/`i2c_sda_io` 被 `generate` 分支固定为 `'Z'`（见源码 565-566 行），不参与驱动。
- 两种配置下功能完全等价（TB 同一套断言都通过），差别只在物理引脚映射方式——待本地验证：跑两个 generic 组合，均应无 `###ERROR###`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `internal_tri_state_g=true` 分支里，`i2c_scl_io` 只有 `'Z'` 和 `'0'` 两种取值，从不取 `'1'`？
**答案**：因为 I2C 是开漏总线，「1」必须靠外部上拉电阻实现，器件只能「松手」（高阻 `'Z'`）或「拉低」（`'0'`）；主动驱动 `'1'` 违反开漏约定，可能损坏器件或破坏线与仲裁。见 [hdl/psi_common_i2c_master.vhd:553-554](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L553-L554)。

**练习 2**：为什么读回 SCL/SDA 要过 `psi_common_bit_cc` 同步器？
**答案**：SCL/SDA 由本主机之外的器件（从机、别的主机、上拉 RC）驱动，相对系统时钟是异步的，直接进 FSM 比较会产生亚稳态。2 级同步器（带 `ASYNC_REG` 等综合属性）把异步输入整形成干净的同步信号，这正是 u5-l2 讲的 `bit_cc` 的用途。见 [hdl/psi_common_i2c_master.vhd:596-606](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L596-L606)。

**练习 3**：`disable_asserts_g` 在仿真里起什么作用？上板时呢？
**答案**：仿真时若为 `false`，命令序列非法等情况下会用 `assert ... report "###ERROR###"` 打印报错信息，配合 u1-l3 的回归判据；为 `true` 则静默。上板（综合）时 `assert/report` 本就不产生硬件，该 generic 无实际作用。TB 里设为 `true`（[testbench/...tb.vhd:154](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L154)），把检测交给 `CheckRsp` 的 `Err` 比对而非断言文本。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**带仲裁失败处理的双字节读**」的源码阅读与时序预测。这是 I2C 实际工程中最常见的场景（先写寄存器地址，再重复 START 转读）。

**任务**：

1. **写命令序列**。参考官方文档的「One Byte Write followed by One Byte Read (with Repeated Start)」示例，列出一次「写 1 字节寄存器地址 → 重复 START → 读 1 字节数据」所需的命令清单，标出每条命令的 `cmd_type_i / cmd_dat_i / cmd_ack_i` 取值。

   提示：序列应为 `CMD_START → CMD_SEND(地址+W) → CMD_SEND(寄存器号) → CMD_REPSTART → CMD_SEND(地址+R) → CMD_REC(ack=0) → CMD_STOP`。

2. **跟踪 FSM**。在源码中逐条把这 7 条命令对应到 FSM 状态跳转，标出每条命令结束时是哪个状态拉了 `rsp_vld_o`（例如 `CMD_SEND` 在 `DataBit3_s` 的 `BitCnt=8` 分支回响应，[hdl/psi_common_i2c_master.vhd:441-450](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_i2c_master.vhd#L441-L450)）。

3. **预测波形**。在纸上画出 SCL/SDA 波形，标注：两个 START（第二个是重复 START，SCL 低时先把 SDA 拉高再产生下降沿）、各字节的 9 个 SCL 脉冲、最后一次 `CMD_REC` 的 NACK 位（主机松手、SDA 为高）。

4. **核对 TB**。TB「Test Clock Stretching」用例的「Write / Read」子段（`StimCase=4` 的最后一段）正是「写→重复 START→读」的带时钟拉伸版本，把它当参考答案核对：

   [testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd:337-346](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_i2c_master_tb/psi_common_i2c_master_tb.vhd#L337-L346) —— 激励：SEND 0x12 → REPSTART → REC(回 ACK) 收 0x67 → STOP。

5. **延伸（可选）**：在该子段基础上，思考如果第 4 步 `CMD_REC` 时丢了仲裁（`rsp_arb_lost_o='1'`），上层软件依据「响应先于下一条命令到达」的契约（见 [doc/files/psi_common_i2c_master.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_i2c_master.md) 第 104-108 行说明），应如何决定是否重发整条序列。

**预期结果**：能画出完整 SCL/SDA 波形并解释每个跳变对应的 FSM 状态；能说明 `CMD_REC` 最后一字节发 NACK 是为通知从机停止输出；能说出仲裁失败后 `rsp_arb_lost_o` 会让上层有机会重试。波形具体时长待本地验证。

---

## 6. 本讲小结

- `psi_common_i2c_master` 是一个**命令驱动、多主机 capable** 的 I2C 主机：用户通过命令接口递交 START/STOP/REPSTART/SEND/REC，组件逐条执行并通过响应接口回报，每条命令恰好一个响应。
- 它把一个 SCL 周期切成 **4 个四分之一周期**，用一个计数器生成节拍脉冲 `QPeriodTick`（与 u6-l1 的 strobe 思想一致），FSM 每拍推进一个状态；SCL 频率由 `QuarterPeriodLimit_c = ⌈f_clk/f_i2c/4⌉ − 1` 决定，`ceil` 保证实际频率不超标。
- I2C 帧时序由 16 态 FSM 生成：START 占 2 个四分之一周期（SCL 高时 SDA 下降）、STOP 占 3 个（SCL 高时 SDA 上升）、每位数据占 4 个（SCL 低建立、SCL 高采样），第 9 位为 ACK；移位寄存器 `ShReg` 同时承担发送装载与接收移位。
- 组件具备两个健壮性机制：**时钟拉伸**（从机拉低 SCL 时冻结节拍等待）与**多主机仲裁**（发送数据位时检测「发 1 读 0」判败出，回 `rsp_arb_lost_o`）。
- 物理接口由 `internal_tri_state_g` 二选一：内部三态用 `inout` 开漏建模（`'Z'`/`'0'`），外部三态用 IOBUF 的 `i/o/t` 接口；读回的 SCL/SDA 经 `psi_common_bit_cc` 2 级同步后供 FSM 使用。
- 全模块沿用 u7-l1 的**二进程 record 设计法**（`two_process_r` + `p_comb`/`p_seq`），`rst_pol_g` 支持高/低有效复位。

## 7. 下一步学习建议

- **走向 AXI 寄存器映射**：本讲的命令/响应接口很自然会被包一层 AXI4-Lite 从机暴露给软件。建议接着学 **u9-l5（AXI 从机 axi_slave_ipif / axilite_slave_ipif）**，把命令/响应寄存器挂到 AXI-Lite 上，做成一个软件可驱动的 I2C 控制器。
- **补齐 CDC 视角**：本组件实例化的 `bit_cc` 来自 u5-l2；如果想深入理解「为什么 2 级同步器够用、为什么不需要格雷码」，可回看 **u5-l1/u5-l2**——单 bit 跨越不需要格雷码，只有异步 FIFO 的多 bit 指针才需要（u4-l2）。
- **对比 SPI 主机**：同单元的 **u9-l1（SPI 主机）** 用的是 `start_i/busy_o/done_o` 脉冲握手而非 AXI-S 风格的命令接口，且 SPI 是推挽电气而非开漏。对照阅读两种主机的接口设计与时序生成，能加深对「串行总线主机」通用结构的理解。
- **阅读官方文档与波形图**：[doc/files/psi_common_i2c_master.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_i2c_master.md) 给出了多种典型命令序列表与一张最简事务波形图，是检验你理解的好材料。
