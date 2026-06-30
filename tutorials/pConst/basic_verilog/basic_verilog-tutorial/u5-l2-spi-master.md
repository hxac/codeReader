# SPI 主机：spi_master

## 1. 本讲目标

UART（u5-l1）教会我们用一根线、靠事先约定的波特率**异步**通信。本讲走向它的对照面——**同步**串行总线 SPI。学完本讲你应当能够：

- 说清 SPI 四线（SCLK/MOSI/MISO/CS）的分工，以及 CPOL/CPHA、MSB/LSB first 这些参数到底在改变什么。
- 读懂 `spi_master.sv` 用**一个计数器 `sequence_cntr` + 一组 `localparam` 窗口**实现的状态机，理解它如何把"慢时钟 `spi_clk`"的边沿变成驱动移位的节拍。
- 解释 MOSI / MISO 移位寄存器为何内部**一律按 LSB first** 移位，再靠 `reverse_vector` 在边界处完成 MSB/LSB 转换。
- 看懂片选（`ncs_pin`）、输出使能（`oe_pin`）、忙标志（`spi_busy`）与 `FREE_RUNNING_SPI_CLK` 选项如何协作。
- 能照着 `spi_master_tb.sv` 里的"从机模型"写一个最小 SPI 从机，做一次回环（loopback）收发并在波形中验证 SCLK 边沿采样。

## 2. 前置知识

在动手前，请确认你已理解下面几个概念（前序讲义已建立）：

- **同步逻辑与时钟节拍**（u1-l2、u2-l1）：`always_ff @(posedge clk)` 里的非阻塞赋值 `<=` 如何一拍一拍推进状态。
- **边沿检测**（u2-l2 / u5-l1）：`edge_detect` 把一个电平信号变成"上升沿单拍脉冲 `rising` / 下降沿单拍脉冲 `falling`"。本讲里它被用来把慢速 `spi_clk` 的两个边沿拆成两条节拍线。
- **UART 异步串行**（u5-l1）：帧格式、波特分频、移位发送。我们会反复拿 SPI 与 UART 对比，凸显"同步 vs 异步"的差异。
- **参数化例化**（u1-l2）：`#(parameter ...)` 与 `.参数名(值)` 覆盖默认值。

几个 SPI 专属术语先打底：

| 术语 | 含义 |
|---|---|
| **SCLK** | 串行时钟，由主机驱动，所有移位都靠它的边沿对齐。 |
| **MOSI** | Master Out, Slave In，主机→从机的数据线。 |
| **MISO** | Master In, Slave Out，从机→主机的数据线。 |
| **CS / nCS** | 片选（本库低有效写作 `ncs_pin`），主机拉低选中某个从机。 |
| **CPOL** | 时钟极性：空闲电平与活动电平的翻转。 |
| **CPHA** | 时钟相位：用哪个边沿采样、哪个边沿改变数据。CPHA=0 表示"前一个边沿采样、后一个边沿改数据"。 |

> 一句话区分：UART 是**两人各戴一块表**对时间（异步），SPI 是**主机敲鼓、大家跟着鼓点动**（同步）。鼓点就是 SCLK。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用法 |
|---|---|---|
| [spi_master.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv) | 通用 SPI 主机，本讲主角 | 逐段精读状态机、移位、片选 |
| [spi_master_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv) | 仿真平台，含 4 种参数组合与一个"从机模型" | 代码实践的蓝本 |
| [edge_detect.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/edge_detect.sv) | 边沿→单拍脉冲（u2-l2 已讲） | `spi_master` 内部三处例化的依赖 |
| [reverse_vector.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/reverse_vector.sv) | 组合反转位序，不占逻辑资源 | MSB/LSB first 的边界转换 |
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 自由计数器分频（u2-l1 已讲） | tb 里用它生成 `spi_clk` |

> **关于 `encoder.v`**：本讲规格表里列出了 `encoder.v`，但打开它会看到它是一个**正交旋转编码器**（输入 `incA/incB` 两路相位差信号，输出 `plus1/minus1` 脉冲），与 SPI 毫无关系，`spi_master` 也并未例化它。这是规格表的一次误列（可能与 u6-l2 的"优先级编码器 priority_enc"混淆）。本讲真正依赖的"积木"是上表五项，**以实际代码为准**——这也是本手册反复强调的"文档/规格会滞后于代码"的又一例证。

## 4. 核心概念与源码讲解

### 4.1 SPI 时序状态机：用一个计数器走完一帧

#### 4.1.1 概念说明

SPI 主机的核心职责，是按 SCLK 的节拍**精确地**完成一件事序列：拉低 CS → 逐位把 MOSI 上的数据挤出去 →（若要读）逐位把 MISO 上的数据收进来 → 拉高 CS 结束。

很多教材会用一段显式的 `case(state)` 状态机来写这件事。`spi_master.sv` 用了另一种很值得学习的写法：**一个 8 位计数器 `sequence_cntr` 当作"时间轴"，用几个 `localparam` 在这条轴上划定窗口**，再用 `if (sequence_cntr 在某窗口内 && 某边沿脉冲)` 精确触发动作。这样做的好处是：写/读的位宽变了，只要改 `localparam` 的算式，状态机本身一行不动。

#### 4.1.2 核心流程

先看窗口是如何在时间轴上排布的：

```
sequence_cntr:  0    1    2 .. 17   18 .. 33   34
               │    │    │          │           │
       空闲 ──►│缓冲│拉CS/oe│  WRITE  │   READ    │结束
               起步 │      │ 8 位移出 │ 8 位移入  │
```

对应的常量定义（默认 8 位字）：

- `WRITE_SEQ_START = 2`
- `WRITE_SEQ_END   = WRITE_SEQ_START + 2*MOSI_DATA_WIDTH = 2 + 16 = 18`
- `READ_SEQ_START  = WRITE_SEQ_END = 18`
- `READ_SEQ_END    = READ_SEQ_START + 2*MISO_DATA_WIDTH = 18 + 16 = 34`

为什么每位移位占 **2** 个 `sequence_cntr` 步？因为一个 SCLK 周期有上升、下降两个边沿，`sequence_cntr` 在**每个**边沿（无论上升还是下降）都 +1。于是 8 位 = 16 个边沿 = 16 步，恰好对应 `2*WIDTH`。这是理解本模块时序的钥匙。

一帧的总流程（伪代码）：

```
等待: sequence_cntr==0 且收到 spi_wr_cmd_rise / spi_rd_cmd_rise
  → 缓存 mosi_data，记录本次是"读"还是"写"(rd_nwr)，cntr ← 1
下个 spi_clk 上升沿: cntr==1
  → ncs_pin ← 0, oe_pin ← 1（选中从机，打开输出），cntr ← 2
WRITE 窗口 [2,18):
  每个 spi_clk 下降沿 → 把 mosi_data_buf[0] 推上 mosi_pin 并右移
  每个边沿 → cntr++
到 cntr==18 的下降沿:
  若是"写"事务(rd_nwr==0) → ncs_pin ← 1, 结束, cntr ← 0
  若是"读"事务(rd_nwr==1) → mosi_pin ← 0, oe_pin ← 0, cntr ← 19
READ 窗口 [18,34):
  每个 spi_clk 上升沿 → 把 miso_pin 采进 miso_data_buf 并右移
  每个边沿 → cntr++
到 cntr==34 的下降沿: ncs_pin ← 1, 结束, cntr ← 0
```

> 关键洞察：**读事务其实先发再收**。`spi_rd_cmd` 并非"只读"，而是"先写 8 位（通常是从机命令字节），再读 8 位（从机响应）"。这正好匹配大量 SPI 从器件（ADC、Flash）的"发命令→读数据"时序。若你只想发不想收，用 `spi_wr_cmd`，它在 WRITE 窗口结束后即终止。

#### 4.1.3 源码精读

**参数与端口**——SPI 的所有"可调旋钮"都集中在这里：CPOL、是否自由运行时钟、收发位宽、收发 MSB/LSB 顺序：[spi_master.sv:51-89](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L51-L89)。注意 INFO 注释明确说本模块只支持 **mode 0（CPOL=0, CPHA=0）** 与 **mode 2（CPOL=1, CPHA=0）**——即 CPHA 固定为 0。

**窗口常量**——这就是上文"时间轴窗口"的来源，位宽改了算式自适应：[spi_master.sv:92-98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L92-L98)。

**把 `spi_clk` 拆成两条节拍线**——这是承接 u2-l2 的关键一步。慢速 `spi_clk` 本身不能直接拿来 `if` 判断边沿，于是用 `edge_detect` 把它的上升/下降沿变成单系统时钟周期的脉冲 `spi_clk_rise` / `spi_clk_fall`：[spi_master.sv:101-110](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L101-L110)。后面所有"在某个边沿做某事"都靠这两个脉冲把关。

> 这也是本模块的硬性前提（端口注释写明）：`spi_clk` 必须是系统 `clk` 的**整数倍同步分频**，且**至少 2 个 `clk` 周期**。因为 `edge_detect` 需要用 `clk` 去采样 `spi_clk`，违反这一点就会漏掉边沿。

**命令也是用上升沿触发**——`spi_wr_cmd` / `spi_rd_cmd` 同样各过一级 `edge_detect`，得到 `spi_wr_cmd_rise` / `spi_rd_cmd_rise`，保证无论命令脉冲多宽都只触发一次：[spi_master.sv:112-121](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L112-L121)。

**事务起步与数据缓冲**——`sequence_cntr==0` 且收到命令沿时，锁存方向 `rd_nwr` 并把 `mosi_data` 冻结进 `mosi_data_buf`（防止用户在移位途中改输入）：[spi_master.sv:179-194](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L179-L194)。

**选中从机**——下一拍 `spi_clk_rise` 上拉低 `ncs_pin`、置高 `oe_pin`，正式开工：[spi_master.sv:197-201](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L197-L201)。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把"计数器当时间轴"的设计内化。

1. 打开 [spi_master.sv:92-98](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L92-L98)。
2. 假设把例化参数改为 `.MOSI_DATA_WIDTH(16)`、`.MISO_DATA_WIDTH(8)`，手算 `WRITE_SEQ_START / WRITE_SEQ_END / READ_SEQ_START / READ_SEQ_END` 四个值。
3. 预期：`WRITE_SEQ_START=2`、`WRITE_SEQ_END=2+2*16=34`、`READ_SEQ_START=34`、`READ_SEQ_END=34+2*8=50`。
4. 再回答：一帧"读事务"总共要走多少个 `sequence_cntr` 步？（答：到 50 才归零，即占用 1~50 这些步。）

> 待本地验证：可在仿真里 `$display(sequence_cntr)` 观察它是否按你推算的值递增。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WRITE_SEQ_END` 用的是 `2*MOSI_DATA_WIDTH` 而不是 `MOSI_DATA_WIDTH`？
> **答**：因为 `sequence_cntr` 在 SCLK 的**每个**边沿（上升+下降）都加 1，一个数据位横跨一个完整 SCLK 周期 = 两个边沿 = 两步。

**练习 2**：若用户在 `sequence_cntr==5` 时再次拉高 `spi_wr_cmd`，会发生什么？
> **答**：不会重触发。重触发的条件是 `sequence_cntr==0`（见第 181 行），中途的命令脉冲被忽略——这正是 `spi_busy` 存在的意义，提示调用方"我现在忙，别发新命令"。

---

### 4.2 移位收发：内部恒为 LSB first，边界用 reverse_vector 翻转

#### 4.2.1 概念说明

SPI 的数据线是**一位一位**传的，于是核心数据通路就是两个**移位寄存器**：

- 发送：把并行的 `mosi_data` 装进移位寄存器，每个节拍把最低位挤到 `mosi_pin`，整体右移。
- 接收：每个节拍把 `miso_pin` 上的电平塞进移位寄存器高位，逐位拼成并行 `miso_data`。

但 SPI 器件有的要求 **MSB first**（先发最高位），有的要求 **LSB first**（先发最低位）。`spi_master` 的处理思路非常优雅：**移位硬件永远按 LSB first 工作，至于"先发的是不是物理最高位"，用 `reverse_vector` 在入口/出口做一次组合翻转来选择。** 这样移位逻辑只有一种写法，简单且不易错。

> `reverse_vector`（u6-l1 会细讲）是一个**纯组合**模块：`out[i] = in[WIDTH-1-i]`，作用是把总线"物理倒序"。它不占任何 LUT/FF，只是改变连线，所以用它做翻转几乎零成本。

#### 4.2.2 核心流程

设 `mosi_data = 8'b1010_0011`（即 0xA3），`WRITE_MSB_FIRST = 1`（先发 MSB）：

```
入口翻转: reverse_vector 把 0xA3 → 0b11000101 (0xC5) 装进 mosi_data_buf
         (即原 bit7 被搬到 buf 的 bit0 位置)

每个 spi_clk_fall:
   mosi_pin ← mosi_data_buf[0]        // 取当前最低位上线
   mosi_data_buf ← {1'b0, buf[7:1]}   // 整体右移，0 补高位

实际线上顺序 (bit0 先出):
   buf=0xC5=11000101 → 依次输出 1,0,1,0,0,0,1,1
   但这些"原 bit0"经过入口翻转后对应原数据的 bit7..bit0
   → 线上看到: 1,0,1,0,0,0,1,1 = 原数据从 MSB 到 LSB ✓
```

接收侧对称：硬件把 `miso_pin` 依次塞进 `miso_data_buf` 的高位（LSB first 地填），若 `READ_MSB_FIRST=1`，再用一个 `reverse_vector` 在出口翻转回正常位序。

CPOL 决定"哪条节拍线负责改数据、哪条负责采样"。对默认 mode 0（CPOL=0），代码的实际行为是：

- **MOSI 在 `spi_clk_fall`（SCLK 下降沿）改变**，于是从机可在随后的上升沿稳定采样；
- **MISO 在 `spi_clk_rise`（SCLK 上升沿）被主机采样**。

这恰好是标准 **CPHA=0** 的"前沿采样、后沿改数"约定。

> 小提醒：源码第 53-59 行的 CPOL 注释把"updates / reads"对应的边沿写反了（与上面代码实际行为相反）。请以**代码实际行为**为准——这也是"读注释更要读代码"的一例。

#### 4.2.3 源码精读

**入口翻转（发送）**——若 `WRITE_MSB_FIRST=1`，先把用户数据倒序装入缓冲，使硬件恒按 LSB first 移位：[spi_master.sv:129-135](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L129-L135)。具体选正序还是倒序装缓冲，见 [spi_master.sv:188-192](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L188-L192)。

**发送移位**——这是"挤出一位 + 右移"的核心，注意它被 `spi_clk_fall` 把关（mode 0 下对应 SCLK 下降沿改数）：[spi_master.sv:204-217](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L204-L217)。

**接收移位**——对称地，被 `spi_clk_rise` 把关（上升沿采样），把 `miso_pin` 拼进缓冲高位：[spi_master.sv:240-251](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L240-L251)。

**出口翻转（接收）+ 组合输出**——把按 LSB first 收到的缓冲按需倒序，得到最终 `miso_data`；同时这里也定义了 `spi_busy`：[spi_master.sv:266-295](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L266-L295)。

发送侧每位的时序关系可用一段简洁公式描述。设 SCLK 周期为 \(T_{\text{spi}}\)，则每位占据：

\[
T_{\text{bit}} = T_{\text{spi}} = 1/f_{\text{spi\_clk}}
\]

整帧 N 位的发送窗口时长为：

\[
T_{\text{frame}} \approx N \cdot T_{\text{spi}}
\]

这与 UART 里"位周期 = 1/波特率"形似，但 SPI 的 \(f_{\text{spi\_clk}}\) 由主机直接给出，无需双方对表。

#### 4.2.4 代码实践

**目标**：亲眼看到"内部 LSB first + 边界翻转 = 可配置 MSB/LSB first"。

1. 阅读 [spi_master_tb.sv:109-132](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L109-L132)（SM1：`WRITE_MSB_FIRST=1`）与 [spi_master_tb.sv:159-182](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L159-L182)（SM3：`WRITE_MSB_FIRST=0`），两者发同一个 `mosi_data=8'b1010_0011`。
2. 在波形里把 SM1 的 `mosi_pin` 与 SM3 的 `mosi_pin` 上下排列，跳过起始两拍后逐位比对。
3. **预期现象**：SM1 线上顺序为 `1,0,1,0,0,0,1,1`（MSB 先发，即原样从高位到低位）；SM3 线上顺序为 `1,1,0,0,0,1,0,1`（LSB 先发，原样从低位到高位）。两者互为倒序。
4. 若不一致，先检查 `WRITE_MSB_FIRST` 是否真的传入了不同值。

> 待本地验证：仿真器（iverilog/ModelSim，见 u1-l3）跑通后用 GTKWave/波形窗观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么发送和接收要**各用**一个 `reverse_vector`，而不是共用一个？
> **答**：因为收发位宽（`MOSI_DATA_WIDTH` / `MISO_DATA_WIDTH`）和方向（`WRITE_MSB_FIRST` / `READ_MSB_FIRST`）都可独立配置；且发送是入口翻转、接收是出口翻转，物理位置不同，故分别例化。

**练习 2**：若 `WRITE_MSB_FIRST=0`（LSB first），`reverse_vector` 还会改变数据吗？
> **答**：不会改变最终线上顺序。此时缓冲直接装 `mosi_data` 原序（见 191 行 `else` 分支），硬件按 LSB first 移位，结果就是"原数据的低位先发"，与 LSB first 语义一致。

---

### 4.3 片选与忙状态：CS、OE 与自由时钟

#### 4.3.1 概念说明

一条 SPI 总线上常挂多个从机，靠 **CS（片选）** 区分：主机把某个从机的 nCS 拉低，它才"听"得到 SCLK 和 MOSI。因此 CS 的边界就是一次事务的边界。

`spi_master` 用三个信号把"事务边界"管起来：

- `ncs_pin`：低有效片选，事务期间为 0，事务外为 1。
- `oe_pin`：输出使能，用于**双向缓冲**场景——当 MOSI 与 MISO 共用一根物理引脚（即所谓 3-wire / half-duplex）时，`oe_pin=1` 表示主机正在驱动该共享引脚，外接的三态缓冲应打开输出。
- `spi_busy`：`sequence_cntr != 0` 即忙，告诉调用方"本帧还没结束，别发新命令"。

还有一个选项 `FREE_RUNNING_SPI_CLK`：

- `=0`（默认）：SCLK **仅在事务期间**翻转，空闲时停在自己静止电平。多数低功耗、怕干扰的场景用这个。
- `=1`：SCLK **始终**翻转（有些从机要求时钟一直跑）。此时 `clk_pin` 不再被 `ncs_pin` 门控。

#### 4.3.2 核心流程

`clk_pin` 的产生分两步，这是个很巧的"先建非反相版，再按 CPOL 决定是否反相"的结构：

```
clk_pin_before_inversion:        // 一个中间寄存器
   if (FREE_RUNNING || ncs==0):  // 自由模式 或 事务进行中
       spi_clk_rise → 置 1
       spi_clk_fall → 置 0
   else (空闲且非自由):          // 停钟
       保持 = CPOL

clk_pin = CPOL ? ~clk_pin_before_inversion     // mode 2: 反相
                :  clk_pin_before_inversion;   // mode 0: 不反相
```

`spi_busy` 是纯组合输出：

\[
\text{spi\_busy} = (\text{sequence\_cntr} \neq 0)
\]

它从命令沿那一拍（`cntr` 由 0 跳到 1）起为 1，直到事务结束 `cntr` 回 0 那一拍才降为 0——与 `ncs_pin` 的活跃区间高度对齐，调用方只需等 `spi_busy==0` 即可发下一帧。

#### 4.3.3 源码精读

**SCLK 的产生与门控**——`FREE_RUNNING_SPI_CLK` 与 `ncs_pin` 共同决定 `clk_pin_before_inversion` 是否跟随 `spi_clk`：[spi_master.sv:157-175](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L157-L175)。

**按 CPOL 决定是否反相 + 定义 spi_busy**——全部在最后的 `always_comb` 里：[spi_master.sv:275-295](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L275-L295)。

**事务结束拉高 CS**——写事务在 WRITE 窗口末尾结束，读事务在 READ 窗口末尾结束，两处都把 `ncs_pin ← 1`、`cntr ← 0`：[spi_master.sv:220-235](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L220-L235) 与 [spi_master.sv:254-259](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L254-L259)。

**复位初值**——注意端口声明里 `ncs_pin = 1`、`mosi_pin = 0`、`oe_pin = 0` 已经给了上电默认值，复位块里再次明确置位，确保空闲态干净：[spi_master.sv:144-154](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master.sv#L144-L154)。

#### 4.3.4 代码实践

**目标**：观察 `FREE_RUNNING_SPI_CLK` 对 `clk_pin` 的影响。

1. tb 里 SM2 为 `FREE_RUNNING_SPI_CLK=0`、SM4 为 `FREE_RUNNING_SPI_CLK=1`（见 [spi_master_tb.sv:134-157](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L134-L157) 与 [spi_master_tb.sv:184-207](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L184-L207)）。
2. 在波形里对齐看 `clk2_pin`（门控）与 `clk4_pin`（自由）。
3. **预期现象**：`clk4_pin` 在整段仿真里持续翻转；`clk2_pin` 仅在 `ncs2_pin==0` 期间翻转，事务结束后停回静止电平（CPOL=1 对应的空闲电平）。
4. 思考：为什么"怕干扰"的板子更倾向 `FREE_RUNNING_SPI_CLK=0`？（提示：SCLK 持续翻转会带来动态功耗与串扰。）

> 待本地验证：用 u1-l3 的 iverilog/ModelSim 流程编译 `spi_master_tb.sv` 后观察。

#### 4.3.5 小练习与答案

**练习 1**：`spi_busy` 与 `ncs_pin` 的活跃区间是否完全相同？
> **答**：高度对齐但**不完全相同**。`spi_busy` 在 `sequence_cntr` 离开 0 的当拍即置 1（命令被接受），而 `ncs_pin` 要等到下一拍 `cntr==1` 的 `spi_clk_rise` 才拉低。所以 `spi_busy` 略早于 `ncs_pin` 生效——这正是给"选中从机"留出准备时间。

**练习 2**：`oe_pin` 在什么时候为 1？
> **答**：仅事务进行中（与 `ncs_pin==0` 同步生效，见 199 行）。它面向"单根数据线分时收发"的外接三态缓冲，告诉缓冲"主机正在驱动共享引脚"。

---

### 4.4 仿真与从机模型：spi_master_tb 如何"自收自发"

#### 4.4.1 概念说明

SPI 是**主机↔从机**的对话，光有主机没法验证。`spi_master_tb.sv` 的精彩之处在于：它在 testbench 里**用一段 `always_ff` 模拟了一个最小从机**——一个在 SCLK 某个边沿把固定位串行推上 MISO 的移位器。于是主机发什么、从机就回什么，形成**回环（loopback）**，可以自校验收发通路。

这是 testbench 的常见手法（承接 u1-l3、u7-l1）：用不可综合的仿真结构搭建一个"对手方"，让 DUT 不依赖真实芯片就能跑起来。

#### 4.4.2 核心流程

tb 同时例化 **4 个** `spi_master`，覆盖四种参数组合：

| 实例 | CPOL | FREE_RUNNING | WRITE_MSB_FIRST | 考察点 |
|---|---|---|---|---|
| SM1 | 0 | 0 | 1 | 标准 mode 0 |
| SM2 | 1 | 0 | 1 | mode 2（时钟反相） |
| SM3 | 0 | 1 | 0 | 自由时钟 + LSB first |
| SM4 | 0 | 1 | 0 | 自由时钟 + LSB first（复现） |

时钟体系（值得学习的多时钟 testbench 写法）：

- `clk200`（200 MHz，周期 5 ns）作系统 `clk`；
- `clk800`（800 MHz，周期 1.25 ns）作"理想高速从机"的采样时钟；
- `clk_divider` 把 `clk200` 分频得到 `DerivedClocks`，取 `DerivedClocks[0]`（= `clk200/2` = 100 MHz，恰好 2 个系统周期）作为 `spi_clk`，满足"≥2 个 clk 周期"的前提。

从机模型（以 SM1 为例）：在 `clk800` 域里，当 `ncs1_pin==0 && oe1_pin==0` 且 `clk1_pin_fall` 时，把 `test1_data[7]` 推上 `din1_pin` 并左移——即每个 SCLK 边沿送出一位。`test1_data` 初值与 `mosi_data` 同为 `8'b1010_0011`，故主机读回的 `miso_data` 应等于发送值，形成回环自检。

#### 4.4.3 源码精读

**多时钟与分频得 spi_clk**——`clk_divider` 产 `DerivedClocks`，取 bit0 作 SPI 时钟：[spi_master_tb.sv:60-68](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L60-L68)。

**启动脉冲**——单拍 `start` 作 `spi_rd_cmd`，触发"先写后读"的一帧：[spi_master_tb.sv:89-94](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L89-L94)。

**从机模型**——`clk800` 域的 8 位移位器，按 SCLK 边沿逐位回送：[spi_master_tb.sv:223-233](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L223-L233)（SM2/SM3/SM4 的从机逻辑对称，分别见 [spi_master_tb.sv:235-269](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/spi_master_tb.sv#L235-L269)，注意 SM2 用 `clk2_pin_rise` 而非 `_fall`，呼应其 CPOL=1 的反相时钟）。

## 5. 综合实践：写一个 SPI 从机，做 0xA5 回环

把本讲三个最小模块（时序状态机 / 移位收发 / 片选与忙）串起来，完成规格表要求的核心任务。

**目标**：写一个最小 SPI 从机（8 位移位），用 `spi_master` 发送 `0xA5` 并收回从机回送的数据，在波形中验证"SCLK 一个边沿改数、另一个边沿采样"。

**操作步骤**：

1. 复制 `spi_master_tb.sv` 为 `my_spi_tb.sv`（放进你自己的仿真目录，**不要改原始源码**）。
2. 只保留一个 `spi_master` 例化（参照 SM1：`CPOL=0`、`FREE_RUNNING_SPI_CLK=0`、8 位、MSB first），把 `mosi_data` 改为 `8'hA5`，`spi_clk` 仍接 `DerivedClocks[0]`。
3. 写一个**最小从机模型**（系统时钟 `clk` 域即可，不必用 `clk800`），伪代码如下（**示例代码，非仓库原有**）：

   ```systemverilog
   // 示例代码：最小 SPI 从机回环模型
   logic [7:0] shifter;
   logic       miso_line;
   always_ff @(posedge clk) begin
     if (ncs_pin) begin               // 空闲：装载回送数据
       shifter <= 8'hA5;
     end else if (clk_pin_fall) begin // 主机在 fall 改 MOSI，从机也可在 fall 准备下一位
       miso_line <= shifter[7];        // MSB first 送出
       shifter  <= {shifter[6:0], 1'b0};
     end
   end
   // 把 miso_line 接到 spi_master 的 miso_pin
   ```
   > 注意：从机送数的边沿要选在与主机**采样边沿**错开的位置（主机在 `spi_clk_rise` 采 MISO，所以从机在 `fall` 准备好下一位是安全的）。具体边沿以你仿真的波形为准——这正是"待本地验证"的部分。
4. 用 `spi_rd_cmd`（单拍脉冲）触发一帧，让主机"先发 0xA5，再读 8 位"。
5. 在波形中观察：`ncs_pin`、`clk_pin`、`mosi_pin`、`miso_pin`、`miso_data[7:0]`、`spi_busy`。

**需要观察的现象与预期结果**：

- `ncs_pin` 在事务期间为 0，结束后回 1；`spi_busy` 与之基本重叠。
- `clk_pin`（mode 0）仅在事务期间翻转，每位一个完整周期。
- `mosi_pin` 在每个 `clk_pin` 下降沿更新，线上依次出现 `0xA5` 的 MSB→LSB（即 `1,0,1,0,0,1,0,1`）。
- `miso_data` 在事务结束后应等于从机回送值（本例为 `0xA5`）。
- **验证边沿采样**：在波形上用游标对齐 `clk_pin` 上升沿，确认主机正是在该沿把 `miso_pin` 的稳定值采进 `miso_data_buf`，而 `mosi_pin` 的变化都发生在下降沿之后——一改一采，错半拍。

> 待本地验证：若回读值不对，最常见原因是**从机送数边沿选错**（与主机采样沿重合导致采到旧值），或 `READ_MSB_FIRST` 与从机的送出顺序不匹配。先调整从机边沿，再核对位序。

## 6. 本讲小结

- SPI 是**同步**串行总线：主机敲 SCLK，四线（SCLK/MOSI/MISO/nCS）分工明确；与 UART 的"异步对表"形成对照。
- `spi_master` 用**一个 `sequence_cntr` 计数器 + `localparam` 窗口**（`WRITE_SEQ_START/END`、`READ_SEQ_START/END`）替代显式状态机，每位占 2 步（两个边沿），位宽变化只需改算式。
- 移位硬件**内部恒按 LSB first**，靠入口/出口各一个 `reverse_vector` 实现 MSB/LSB first 的可配置翻转；翻转是纯组合、不占资源。
- mode 0（CPOL=0）下：MOSI 在 SCLK 下降沿改变、MISO 在上升沿被采样，符合 CPHA=0 约定（注意源码 CPOL 注释与实际行为相反，以代码为准）。
- `ncs_pin` 划定事务边界，`oe_pin` 服务于双向缓冲，`spi_busy = (sequence_cntr != 0)` 给调用方握手；`FREE_RUNNING_SPI_CLK` 决定 SCLK 是否在空闲时持续翻转。
- 读事务是"**先写后读**"：先发 8 位（常作命令），再读 8 位（响应），契合大多数 SPI 从器件时序。
- tb 用一段 `always_ff` 充当**最小从机移位器**形成回环，是"在仿真里造对手方"的典型手法。

## 7. 下一步学习建议

- **横向对比 UART**：回到 u5-l1，把 `uart_tx/uart_rx` 的"波特分频+移位帧"与本讲的"SCLK 节拍+窗口计数器"做一张对比表，巩固"异步 vs 同步"的直觉。
- **继续通信协议簇**：u5-l3 的 **8b10b 编解码** 讲直流平衡与游程，是更高速串行链路（PCIe、SATA 底层）的编码基础；u5-l4 的 **AXI 接口** 则把"主从握手"推广到总线级 `interface/modport`。
- **深入相关积木**：本讲用到但未细讲的 `reverse_vector`、`edge_detect` 在 u6-l1（编码转换工具箱）与 u2-l2 有完整讲解；`clk_divider` 在 u2-l1。
- **工程化收尾**：u7-l2（时序约束）会让你理解为何像 `spi_clk` 这类生成时钟需要在 `.sdc/.xdc` 里用 `create_clock` 显式声明、用 `set_false_path` 处理跨域；u7-l4 综合实战会把 SPI 主机接进一个端到端小系统。
