# SPI 从机接口与 SB_SPI 硬件块

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 iCE40 片上硬核 `SB_SPI` 是什么，以及它如何用「寄存器读写」方式被软件状态机驱动。
- 画出 `spi_interface.v` 从上电初始化到稳定接收命令的完整状态机流转。
- 解释 SPI 包（opcode + 参数）如何被翻译成核心模块 `image_processing` 的 `comm_cmd` / `comm_data_out` 接口。
- 理解 `buffer_full` 与 `spi_data_in_free` 这一对信号如何构成 `image_processing` 输出方向的反压（backpressure）。
- 看懂 `SEND_DATA32` / `RECEIVE_DATA32` 这类 32 字节批量事务如何把整块图像一次性搬过 SPI。

本讲是「仿真与硬件两条后端」单元的第三篇，承接 u6-l2（顶层 `top.v` 与 SPRAM 接口），把硬件后端的最后一块拼图——通信接口——补齐。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 SPI 全双工与「硬核外设」

SPI（Serial Peripheral Interface）是一条**全双工**串行总线：主机（这里是 PC 端的 FTDI 芯片）和从机（FPGA）各自维护一个移位寄存器，时钟 `SCK` 每跳一次，主机的 1 个 bit 从 `MOSI` 移给从机、从机的 1 个 bit 从 `MISO` 移给主机。**发送和接收同时发生**，一个字节周期里双方各换一个字节。

iCE40 UltraPlus 把一个完整的 SPI 控制器做成了片上硬核 `SB_SPI`，它内部有寄存器文件（控制寄存器、状态寄存器、TX/RX 数据寄存器）。我们写的 Verilog 不必逐 bit 处理 `SCK`/`MOSI`/`MISO`，只需像访问内存一样**读写它的寄存器**：给地址、给数据、拉一次选通（strobe），等它回一个 ack。这正是 `spi_interface.v` 的核心思路。

### 2.2 谁是主机、谁是从机

在本项目里：

- **主机（master）**：PC 上的 `soft_ice40` 程序，通过 FTDI 芯片的 MPSSE 引擎产生 `SCK`、驱动 `MOSI`、拉低片选 `SS`。
- **从机（slave）**：FPGA 里的 `SB_SPI`，被 `spi_interface.v` 配置成从机模式，被动接收主机发来的字节、按需把字节送回主机。

`SB_SPI` 一旦设成从机，片选信号 `SCSNI` 由主机控制，`spi_interface.v` 只需关心「RX 寄存器里有没有新字节」「TX 寄存器空没空」。

### 2.3 为什么要「翻译」

`image_processing` 核心模块只认自己的通信接口（`comm_cmd`/`comm_data_in`/`comm_data_out` 等，见 u3-l1），它**不知道**字节是来自 SPI、FIFO 还是别的什么。`spi_interface.v` 的职责就是当**翻译官**：把主机经 SPI 送来的「SPI 包」翻译成 `image_processing` 能理解的 `comm_*` 信号，反过来也把 `image_processing` 的输出字节打包成 SPI 包送回主机。这与仿真后端用 FIFO 队列当翻译官（见 u6-l1）是完全对称的角色。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [ice40/hdl/spi_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v) | **本讲主角**。例化 `SB_SPI`、初始化、运行接收/发送状态机，完成 SPI↔comm 翻译。 |
| [ice40/hdl/top.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v) | 顶层连线，把 `spi_interface` 的端口接到 `image_processing` 的 `comm_*` 与外部 SPI 引脚。 |
| [ice40/software/image_processing_ice40.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp) | 主机侧后端，定义 SPI 操作码，发出 `SPI_SEND_CMD`/`SPI_SEND_DATA32` 等事务。 |
| [ice40/software/spi_lib/spi_lib.h](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h) 与 [spi_lib.c](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c) | 主机侧底层，定义 `STATUS_FPGA_RECV_MASK` 等状态位掩码，实现重试确认。 |

> 提示：本讲会频繁对照 FPGA 侧（`spi_interface.v`）与主机侧（`image_processing_ice40.cpp` / `spi_lib.*`）的同一件事，因为 SPI 协议是双方共同遵守的契约，单看一侧会似懂非懂。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：① `SB_SPI` 原语例化；② 控制寄存器初始化序列；③ SPI 包到 comm 接口的翻译（命令派发与数据流转）；④ `buffer_full` / `spi_data_in_free` 反压。

### 4.1 SB_SPI 硬件原语与寄存器访问接口

#### 4.1.1 概念说明

`SB_SPI` 是 iCE40 UltraPlus 内置的**硬核**（hard IP）——它不是用查找表拼出来的软 SPI，而是芯片里一块固定的硅电路。我们在 Verilog 里把它当成一个「黑盒模块」例化，通过一组「寄存器访问」信号与它对话：

| 信号 | 方向 | 含义 |
|---|---|---|
| `spi_stb` | → SB_SPI | 选通（strobe），读或写时必须拉高 |
| `spi_rw` | → SB_SPI | 1=写，0=读 |
| `spi_adr` | → SB_SPI | 8 位寄存器地址 |
| `spi_dati` | → SB_SPI | 要写入的数据 |
| `spi_dato` | ← SB_SPI | 读出的数据 |
| `spi_ack` | ← SB_SPI | 应答，=1 表示本次读/写完成 |

注意命名规律：`...i` 结尾（`SBCLKI`/`SBDATI`）是 input，`...O` 结尾（`SBDATO`/`SBACKO`）是 output。

#### 4.1.2 核心流程

一次寄存器访问的握手时序：

```text
① 准备：spi_adr <= 目标地址; spi_dati <= 数据(写时); spi_rw <= 1或0;
② 选通：spi_stb <= 1;
③ 等待：if (spi_ack == 1) 进入下一步;
④ 撤销：spi_stb <= 0; （读时 spi_dato 上即为结果）
```

也就是「给地址/数据 → 拉 stb → 等 ack → 收回 stb」的 2 拍以上握手。`SB_SPI` 内部的 `SCK`/`MOSI`/`MISO`/`SS` 串行化细节全部被这层寄存器接口封装掉了。

#### 4.1.3 源码精读

模块端口声明把外部 SPI 引脚与对 `image_processing` 的 comm 信号一并列出：

[ice40/hdl/spi_interface.v:2-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L2-L4) —— 左半边 `spi_sck/spi_ss/spi_mosi/spi_miso` 是物理 SPI 线；右半边 `spi_cmd/spi_data_out/spi_data_out_valid/spi_data_in/spi_data_in_valid/spi_data_in_free` 是面向 `image_processing` 的 comm 翻译接口。注意这里的命名视角：`spi_data_out` 是「SPI 模块向 image_processing 输出的字节」（即主机的命令/数据），`spi_data_in` 是「SPI 模块从 image_processing 收到的字节」（即要送回主机的输出）。

`SB_SPI` 例化把每一位地址/数据线显式连到顶层寄存器（iCE40 原语不接受整总线端口，必须逐 bit 展开）：

[ice40/hdl/spi_interface.v:30-38](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L30-L38) —— 注意 `MI`/`MO`（主机输入/输出）被接到 `_unused`：因为本模块设成**从机**，主机方向的 `MISO`/`MOSI` 由 `SO`/`SI` 承担。`SCKI`/`SCSNI` 直接接外部 `spi_sck`/`spi_ss`，由主机驱动。

寄存器地址表用 parameter 集中定义，后面初始化与运行期都引用它：

[ice40/hdl/spi_interface.v:14-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L14-L15) —— 关键寄存器有：`SPISR`(0x0C) 状态寄存器、`SPITXDR`(0x0D) 发送数据寄存器、`SPIRXDR`(0x0E) 接收数据寄存器，以及一组控制寄存器 `SPICR0/1/2`、波特率 `SPIBR`、片选 `SPICSR`。

#### 4.1.4 代码实践

**实践目标**：把 `SB_SPI` 当黑盒，确认它的寄存器访问握手能与一份典型的「读 SPISR」流程对上号。

**操作步骤**：

1. 打开 [ice40/hdl/spi_interface.v:138-159](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L138-L159)（`SPI_WAIT_RECEPTION` 状态）。
2. 逐行标注它如何执行 4.1.2 的四步：设 `spi_adr <= SPI_ADDR_SPISR`、`spi_rw <= 0`（读）、`spi_stb <= 1`、等 `spi_ack`、再 `spi_stb <= 0`。
3. 回答：`spi_dato` 在哪个时刻才含有有效读出值？（提示：只有 `spi_ack==1` 那一拍。）

**需要观察的现象 / 预期结果**：你会看到「设地址→拉 stb→等 ack→撤销 stb」的固定模式在 `INIT_SPICR0`、`SPI_WAIT_RECEPTION`、`SPI_READ_OPCODE`、`SPI_TRANSMIT` 里**反复出现**，只是 `spi_rw` 和地址不同。这就是「寄存器访问」这一抽象的威力：所有与 `SB_SPI` 的交互都被归一成同一套握手。

> 待本地验证：`spi_ack` 相对 `spi_stb` 的确切延迟拍数需结合 `SB_SPI` 数据手册确认；本讲只断言「`spi_ack==1` 那拍 `spi_dato` 有效」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SB_SPI` 例化里 `MI`/`MO` 接到了 `_unused` 信号？
**答案**：因为本模块被配置成 SPI **从机**，`MISO` 由从机输出引脚 `SO` 承担、`MOSI` 由从机输入引脚 `SI` 承担；`MI`/`MO` 是主机方向信号，从机模式下不用，但仍需连到一个 wire 以满足原语端口要求。

**练习 2**：如果要把一次「写寄存器」改成「读寄存器」，需要改变哪几个信号？
**答案**：把 `spi_rw` 从 1 改成 0，并在 `spi_ack==1` 那拍从 `spi_dato`（而非 `spi_dati`）取结果；`spi_adr` 与 `spi_stb` 的用法不变。

---

### 4.2 SPI 从机控制寄存器初始化序列

#### 4.2.1 概念说明

`SB_SPI` 上电后处于默认状态，必须先往它的控制寄存器里写一组配置字，把它「调成从机、使能、LSB 先发、设好时钟分频」，之后才能开始收发。这个配置过程是一次性的，发生在 `is_spi_init` 之前，用一连串 `INIT_*` 状态完成。它对应主机侧 `send_params()` 开头那次 `SPI_INIT` 握手（见 4.3）。

#### 4.2.2 核心流程

初始化状态链（每个状态写一个寄存器，写完等 `spi_ack` 进下一个）：

```text
INIT_SPICR0 (写 0x00)        ─┐
INIT_SPICR1 (写 0x80, 使能)   │  依次写 5 个寄存器
INIT_SPICR2 (写 0x01, LSB)    │
INIT_SPIBR  (写 0x00, 分频=1) │
INIT_SPICSR (写 0x00)        ─┘
        ↓
SPI_WAIT_RECEPTION (进入正常收发循环)
```

#### 4.2.3 源码精读

状态参数用「前一个 +1」的链式定义，避免硬编码连续整数出错：

[ice40/hdl/spi_interface.v:9-12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L9-L12) —— 注意初始化状态（`INIT_SPICR0`…`INIT_SPICSR`）在前，运行期状态（`SPI_WAIT_RECEPTION` 起）在后，`initial` 块把 `state_spi` 设为 `INIT_SPICR0`，于是上电自动走初始化链。

每个 `INIT_*` 状态结构完全一样，以使能 SPI 的 `INIT_SPICR1` 为例：

[ice40/hdl/spi_interface.v:97-106](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L97-L106) —— 写 `8'b10000000` 到 `SPICR1`，注释点明 bit7=使能 SPI。`spi_stb<=1; spi_rw<=1;` 拉起写选通，`spi_ack==1` 后撤销 stb 并切到下一个状态。

几个关键配置字的含义对照：

| 状态 | 寄存器 | 写入值 | 含义 |
|---|---|---|---|
| `INIT_SPICR0` | SPICR0 | 0x00 | 默认（注释说本例无特别项） |
| `INIT_SPICR1` | SPICR1 | 0x80 | bit7=1：**使能 SPI** |
| `INIT_SPICR2` | SPICR2 | 0x01 | bit0=1：**LSB first** |
| `INIT_SPIBR` | SPIBR | 0x00 | 时钟分频=1（从机模式下由主机定 SCK，此处影响有限） |
| `INIT_SPICSR` | SPICSR | 0x00 | 片选寄存器；从机模式注释明确「absolutely no use」 |

最后一个初始化状态写完直接进入运行循环并清零计数器：

[ice40/hdl/spi_interface.v:127-137](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L127-L137) —— `INIT_SPICSR` 在 `spi_ack==1` 后跳转 `SPI_WAIT_RECEPTION` 并 `counter_read<=0`，正式开始监听主机。

#### 4.2.4 代码实践

**实践目标**：理清「上电后到第一次能收命令」之间，FPGA 自主做了哪些事、花了多少拍。

**操作步骤**：

1. 在 [ice40/hdl/spi_interface.v:55-72](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L55-L72)（`initial` 块）确认初值：`state_spi = INIT_SPICR0`、`is_spi_init = 0`、`buffer_full = 0`。
2. 数一下：从 `INIT_SPICR0` 到 `INIT_SPICSR` 共几个状态？每个状态至少要等一次 `spi_ack`（≥2 拍）。
3. 对照主机侧 [ice40/software/image_processing_ice40.cpp:25-33](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L25-L33)：主机在 `send_params` 一开始先 `spi_init()` 打开 FTDI，再发 `SPI_INIT` 事务。说明主机为何要在发任何命令前先做这一步。

**预期结果**：初始化共 5 个寄存器写状态，FPGA 上电后**自发**完成，不依赖主机；主机侧的 `SPI_INIT` 是为了让 FPGA 跳出「扫描 0x11」的等待、置位 `is_spi_init`（详见 4.3.3），二者协同确保 FPGA 已就绪。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 `SPICR2` 设成 LSB first（0x01）？
**答案**：为了让 SPI 字节内的 bit 顺序与主机 FTDI MPSSE 引擎（默认 LSB first，见 spi_lib.c 中 `MC_DATA_LSB`）一致，否则双方移位方向相反会读出乱码。

**练习 2**：`INIT_SPICSR` 写 0x00 却注释「absolutely no use」，为什么还要写？
**答案**：SPICSR 是**主机模式**下选片用的寄存器；本模块是从机，该寄存器不影响行为，但仍按初始化惯例显式写一个确定值，避免上电随机值在某些工具链下触发告警。

---

### 4.3 SPI 包到 comm 接口的翻译：命令派发与数据流转

这是本讲最重的一节，覆盖第三个最小模块「RECEIVE_CMD/DATA/DATA32 状态翻译」。

#### 4.3.1 概念说明：操作码的「镜像命名」

SPI 协议是双方共守的契约，但本项目里主机与 FPGA 对**同一个操作码数值**给出了互补的命名——从各自视角描述同一件事：

| 数值 | 主机侧名称（image_processing_ice40.cpp） | FPGA 侧名称（spi_interface.v） | 实际发生的事 |
|---|---|---|---|
| 0 | `SPI_NOP` | `NOP` | 空操作 |
| 1 | `SPI_INIT` | `INIT` | 初始化握手 |
| 2 | `SPI_READ_DATA` | `SEND_DATA` | 主机读 1 字节 / FPGA 发 1 字节 |
| 3 | `SPI_SEND_CMD` | `RECEIVE_CMD` | 主机发命令 / FPGA 收命令 |
| 4 | `SPI_SEND_DATA` | `RECEIVE_DATA` | 主机发数据 / FPGA 收数据 |
| 5 | `SPI_READ_DATA32` | `SEND_DATA32` | 主机读 32 字节 / FPGA 发 32 字节 |
| 6 | `SPI_SEND_DATA32` | `RECEIVE_DATA32` | 主机发 32 字节 / FPGA 收 32 字节 |

> 记忆口诀：**主机 SEND ↔ FPGA RECEIVE；主机 READ ↔ FPGA SEND**。数值一一对应，只是名字换了视角。主机侧定义见 [ice40/software/image_processing_ice40.cpp:9-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L9-L15)，FPGA 侧定义见 [ice40/hdl/spi_interface.v:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L6)。

#### 4.3.2 核心流程：一次命令的端到端流转

以主机发一条 `COMMAND_PARAM`（告诉 `image_processing` 图像宽高）为例：

```text
主机：spi_command_send(SPI_SEND_CMD, COMMAND_PARAM)
       ↓ 经 FTDI 发 4 字节 {0x03, COMMAND_PARAM, 0, 0}
FPGA spi_interface:
  SPI_WAIT_RECEPTION: 收到字节 → SPI_READ_OPCODE
  SPI_READ_OPCODE (counter_read=0): opcode=RECEIVE_CMD(3)，存 command_data[0]
  SPI_READ_OPCODE (counter_read=1): spi_dato=COMMAND_PARAM
        → spi_data_out_valid<=1; spi_cmd<=COMMAND_PARAM   ★ 翻译给 image_processing
  SPI_READ_OPCODE (counter_read=3): counter_read<=0，事务结束
       ↓ 同时 SPI_TRANSMIT 把状态字节回送主机
image_processing: 在 comm_cmd 上看到 COMMAND_PARAM，进入命令解析 FSM（见 u3-l3）
```

数据类命令（`RECEIVE_DATA`）的差别仅在于：`counter_read==1` 那拍把字节送到 `spi_data_out`（而非 `spi_cmd`）。32 字节批量命令（`RECEIVE_DATA32`/`SEND_DATA32`）则把帧长从 4 字节扩到 33 字节，见 4.3.4。

#### 4.3.3 源码精读：派发总台 SPI_WAIT_RECEPTION

这是整个状态机的心脏，决定「收到一个字节后下一步干什么」：

[ice40/hdl/spi_interface.v:138-159](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L138-L159) —— 它先读状态寄存器 `SPISR`，默认停留在本状态自循环；只有当 `spi_dato[3]==1`（bit3=RX 有新字节）才前进。前进方向由 `is_spi_init` 二选一：

- **`is_spi_init==0`（尚未初始化）** → 跳 `SPI_READ_INIT`：去读 `SPIRXDR`，并把读到的字节与魔法值 `0x11` 比较；命中则置 `is_spi_init<=1`、清零计数器，正式进入命令态。
- **`is_spi_init==1`（已初始化）** → 再二选一：
  - 若**还欠主机回送字节**（普通命令 `counter_send<2`，或 `SEND_DATA32` `counter_send<31`）→ 跳 `SPI_WAIT_TRANSMIT_READY` 去发送；
  - 否则跳 `SPI_READ_OPCODE` 去读取下一个命令/数据字节。

`is_spi_init` 的置位发生在 `SPI_READ_INIT`：

[ice40/hdl/spi_interface.v:206-221](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L206-L221) —— 主机侧 [send_params 的 INIT 事务](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L29-L33) 发出 `{0x01, 0x00, 0x00, 0x11}`，FPGA 在扫描到的第 4 个字节 `0x11` 处命中，完成握手。注意 FPGA 此刻**只认 0x11**，opcode 0x01 几乎是装饰性的——这是双方约定的「初始化完成」哨兵。

#### 4.3.4 源码精读：命令/数据翻译 SPI_READ_OPCODE

这是把 SPI 字节真正翻译成 `comm_*` 信号的地方：

[ice40/hdl/spi_interface.v:222-281](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L222-L281) —— 关键翻译规则按 `counter_read`（帧内字节序号）与 `command_data[0]`（opcode）分派：

- `counter_read==0`：存 opcode 到 `command_data[0]`。若 opcode 是读类（`SEND_DATA`/`SEND_DATA32`）且 `buffer_full`，则把待回送字节装入 `data_to_send`、置 `is_data_to_send`（为发送做准备）。
- `counter_read==1`：
  - `RECEIVE_CMD` → `spi_cmd <= spi_dato; spi_data_out_valid <= 1;`（命令字节交给 `image_processing` 的 `comm_cmd`）
  - `RECEIVE_DATA` → `spi_data_out <= spi_dato; spi_data_out_valid <= 1;`（数据字节交给 `comm_data_in`）
- `counter_read>=1`：
  - `RECEIVE_DATA32` → 逐字节 `spi_data_out <= spi_dato`（32 字节数据流式灌入）
  - `SEND_DATA32 && counter_read<30` → 若 `buffer_full` 则装载回送字节
- 帧长：32 字节类命令在 `counter_read==32` 复位，普通命令在 `counter_read==3` 复位。

注意 `top.v` 里的命名翻转：`spi_interface` 的 `spi_data_out` 实际接到 `image_processing` 的 `comm_data_in`：

[ice40/hdl/top.v:42-45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L42-L45) —— `spi_data_out(ip_comm_data_in)`、`spi_data_in(ip_comm_data_out)`。即「SPI 之 out 即 image_processing 之 in」，这是 u6-l2 提过的命名翻转，在本讲翻译逻辑里要时刻记在脑后。

#### 4.3.5 源码精读：发送状态与状态字节

发送路径由 `SPI_WAIT_TRANSMIT_READY` 等 TX 就绪、`SPI_TRANSMIT` 写 `SPITXDR` 两状态组成：

[ice40/hdl/spi_interface.v:160-172](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L160-L172) —— 读 `SPISR`，等 `spi_dato[4]`（bit4=TRDY，TX 寄存器空）为 1 才跳 `SPI_TRANSMIT`。

[ice40/hdl/spi_interface.v:173-205](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L173-L205) —— `SPI_TRANSMIT` 决定写什么进 `SPITXDR`：

- `counter_send==0`：写**状态字节** `spi_dati <= 8'b01000000; spi_dati[0] <= buffer_full;`
  - 即 \(\text{status} = 0\mathrm{x}40 \;|\; b_{\text{full}}\)，bit6 恒 1、bit0 = `buffer_full`。
- `counter_send>0`：若 `is_data_to_send` 写 `data_to_send`，否则写占位 `0x42`。

发完一拍 `counter_send++`；若刚发的是真实数据（`is_data_to_send`），同时清 `is_data_to_send` 与 `buffer_full`（**抽干缓冲**）。

#### 4.3.6 代码实践（本讲主实践）

**实践目标**：把 `SPI_WAIT_RECEPTION` 的分支逻辑、状态字节里 `buffer_full` 的作用、以及 `SEND_DATA32` 批量回传三件事串起来。

**操作步骤**：

1. **分支含义**。阅读 [SPI_WAIT_RECEPTION](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L138-L159)，回答：
   - `is_spi_init==0 && spi_dato[3]==1` 跳 `SPI_READ_INIT` 的含义是？（提示：上电后还没收到主机的初始化哨兵，先把来的字节当候选 `0x11` 读出来检查。）
   - `is_spi_init==1 && spi_dato[3]==1` 时，依据 `counter_send` 在 `SPI_WAIT_TRANSMIT_READY` 与 `SPI_READ_OPCODE` 之间二选一的含义是？（提示：「我还欠主机回送字节」就去发送，否则就把这个字节当新命令/数据读进来。）
2. **状态字节的 buffer_full 位**。阅读 [SPI_TRANSMIT 的 counter_send==0 分支](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L175-L178)，再对照主机 [read_status](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L51-L67) 里的 `if(recv_data[0]&1 == 1 ...)`：说明状态字节 bit0（`buffer_full`）如何告诉主机「紧跟其后的字节是不是 `image_processing` 的有效输出」。
   - 进一步对照 [spi_lib.h:65-69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L65-L69)：bit6（`0x40`，`STATUS_FPGA_RECV_MASK`）是状态字节的**固定标记**，主机据此确认「FPGA 确实回送了一个真正的状态字节」；bit0（`buffer_full`）则是「附带的有效数据指示」。
3. **SEND_DATA32 批量回传**。以主机 [read_image](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L142-L162) 为入口，它用 `SPI_READ_DATA32`（FPGA 侧 `SEND_DATA32`=5）一次收 31 字节。回到 FPGA [SPI_READ_OPCODE 的 SEND_DATA32 分支](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L260-L265)：在 `counter_read<30` 的每一拍，只要 `buffer_full`，就把 `image_processing` 刚吐出的字节（`buffer_data_in`）装进 `data_to_send`，再由 `SPI_TRANSMIT` 经 MISO 送出。于是**一次 33 字节的 SPI 事务**就能把 `image_processing` 的连续输出（最多约 30 个像素）批量回传，而不必每个像素单独开一次事务。

**需要观察的现象 / 预期结果**：

- 状态字节 bit6 恒为 1（`0x40`），bit0 随 `buffer_full` 翻转。
- 主机在 `read_image` 里每收一个 32 字节包，先看 `recv_data[0]&1`（即 `buffer_full`）决定是否把后续 30 字节当真实像素；这正是「整块图像一次性搬过 SPI」的关键。
- 反向的 `RECEIVE_DATA32`（主机 `SPI_SEND_DATA32`=6）用于 [send_image](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L69-L88) 批量发图，原理对称：FPGA 在 `RECEIVE_DATA32` 分支逐字节 `spi_data_out <= spi_dato` 把像素灌进 `image_processing`。

> 待本地验证：状态字节经全双工 SPI 到达主机的确切字节偏移（主机注释「前 2 字节是 garbage、第 3 字节是 status」）取决于双方移位寄存器的 pipeline 深度，建议在真实硬件上用逻辑分析仪抓一次 `MISO` 波形确认。

#### 4.3.7 小练习与答案

**练习 1**：主机发 `SPI_SEND_CMD`（0x03）与 `SPI_SEND_DATA`（0x04）时，FPGA 侧 `SPI_READ_OPCODE` 的处理有何不同？
**答案**：opcode 存进 `command_data[0]` 后，`RECEIVE_CMD`(3) 在 `counter_read==1` 把字节送到 `spi_cmd`（命令），`RECEIVE_DATA`(4) 在 `counter_read==1` 把字节送到 `spi_data_out`（数据）。两者帧长都是 4 字节（`counter_read==3` 复位）。

**练习 2**：为什么状态字节要固定把 bit6 设成 1？
**答案**：bit6（`0x40`）是「这是一个真状态字节」的固定标记，主机用 `STATUS_FPGA_RECV_MASK` 检测它；当 FPGA 还没来得及装载状态、只发了占位 `0x42` 或残留字节时，主机据此重试（见 spi_lib.c 重试循环），避免把无效响应当成命令确认。

**练习 3**：`SEND_DATA32`（FPGA 发 32 字节）与 `RECEIVE_DATA32`（FPGA 收 32 字节）分别被主机侧哪个函数使用？
**答案**：`SEND_DATA32`(5) 对应主机 `SPI_READ_DATA32`，被 `read_image` 用于批量读回像素；`RECEIVE_DATA32`(6) 对应主机 `SPI_SEND_DATA32`，被 `send_image` 用于批量发送像素。

---

### 4.4 反压机制：buffer_full 与 spi_data_in_free

#### 4.4.1 概念说明

`image_processing` 在回读图像或状态时，会经 `comm_data_out` 逐字节吐出结果。但 SPI 这边一次只能搬一个字节、且要等主机来「读」才会搬。如果 `image_processing` 吐得比 SPI 搬得快，字节就会丢。因此需要一个**单字节缓冲 + 反压**机制：

- `buffer_data_in` + `buffer_full`：缓存 `image_processing` 吐出的**一个**字节。
- `spi_data_in_free`：告诉 `image_processing`「我现在能不能再收一个字节」。

这与 u3-l1/u3-l4 讲过的 `comm_data_out_free` 握手是同一件事的两端。

#### 4.4.2 核心流程

```text
image_processing 吐字节 (comm_data_out_valid=1)
        ↓ top.v 连线
spi_interface: if (spi_data_in_valid) buffer_data_in<=字节; buffer_full<=1;
        ↓
spi_data_in_free = !(buffer_full || spi_data_in_valid)   ← 反压回去
        ↓ 主机发起读事务 (SEND_DATA / SEND_DATA32)
SPI_TRANSMIT 把 buffer_data_in 送出 → buffer_full<=0 → spi_data_in_free 重新变 1
        ↓
image_processing 看到 comm_data_out_free=1，吐下一个字节
```

#### 4.4.3 源码精读

反压信号是组合逻辑，一行定义：

[ice40/hdl/spi_interface.v:53](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L53) —— `spi_data_in_free = !(buffer_full || spi_data_in_valid)`。即：缓冲空**且**当前没有字节正在到来，才宣告「空闲」。经 `top.v` 接到 `image_processing` 的 `comm_data_out_free`，于是 `image_processing` 在 `STATE_READ_IMG` 等状态里会据此停顿（见 u3-l4）。

缓冲的装载发生在 `always` 块开头、所有状态之前：

[ice40/hdl/spi_interface.v:81-84](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L81-L84) —— 只要 `spi_data_in_valid`（即 `image_processing` 的 `comm_data_out_valid`）为 1，就锁存字节并置 `buffer_full`。这段在 `case(state_spi)` 之外，意味着**任何状态**都能接收 `image_processing` 的输出，不被当前 SPI 事务所阻塞。

缓冲的抽干发生在 `SPI_TRANSMIT` 发出真实数据之后：

[ice40/hdl/spi_interface.v:190-197](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L190-L197) —— `is_data_to_send` 清零的同时 `buffer_full<=0`，于是 `spi_data_in_free` 立刻（组合逻辑）回升为 1，放行下一个字节。

#### 4.4.4 代码实践

**实践目标**：验证「单字节缓冲」是 `image_processing` 输出与 SPI 发送之间唯一的耦合点，并理解它为何必须配反压。

**操作步骤**：

1. 在 `spi_interface.v` 里搜索 `buffer_full` 的所有写处（`initial`、L83、L196），确认它只有「置 1（收到字节）」和「清 0（发出字节）」两种变化。
2. 回答：如果删掉 `spi_data_in_free` 的 `buffer_full` 条件（即让 `image_processing` 永远以为空闲），在 `read_image` 期间会发生什么？
3. 对照 [image_processing.v 的 STATE_READ_IMG](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v)（见 u3-l4）：`comm_data_out_free` 如何让 `image_processing` 在 SPI 没来读时停住、不来读时不丢字节。

**预期结果**：`buffer_full` 是一个容量为 1 的 FIFO 等价物；删掉反压后，`image_processing` 会在 SPI 还没搬走上一字节时覆盖 `buffer_data_in`，导致像素丢失。这正是 u6-l1 仿真后端用 `counter_free` 模拟的同一现象（输出方向反压），硬件侧用 `buffer_full` + `spi_data_in_free` 实现。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `spi_data_in_free` 用组合逻辑（`assign`）而不是寄存器？
**答案**：因为它必须**即时**反映 `buffer_full` 与 `spi_data_in_valid` 的当前值，让 `image_processing` 在同一拍就能决定是否推进；用寄存器会多一拍延迟，可能在边界条件下丢字节。

**练习 2**：`buffer_full` 容量只有 1 字节，是否意味着 `image_processing` 的输出吞吐被 SPI 速率卡死？
**答案**：是的。`image_processing` 每吐一个字节都必须等主机发起一次读事务把它搬走，吞吐上限 = SPI 字节速率。`SEND_DATA32` 批量事务正是为了摊薄每个字节的命令开销、尽量逼近这个上限。

---

## 5. 综合实践

**任务**：画一张「主机 `read_image` 一次 32 字节读取」的端到端时序图，把本讲四个最小模块全部串起来。

要求在图上标出：

1. 主机 `soft_ice40` 调用 `spi_command_send_recv_32B(SPI_READ_DATA32, ...)`（[read_image](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L142-L162)），FTDI 经 MPSSE 驱动 `SCK`/`MOSI`/`SS`。
2. FPGA `spi_interface` 在 `SPI_WAIT_RECEPTION` 检测到 `SPISR` bit3（RX 有字节），进 `SPI_READ_OPCODE` 识别 opcode=`SEND_DATA32`(5)。
3. `image_processing` 在 `STATE_READ_IMG` 把像素放上 `comm_data_out`（`spi_data_in`），`spi_interface` 锁存进 `buffer_data_in`/`buffer_full`（4.4）。
4. `SPI_TRANSMIT` 先发状态字节 `0x40|buffer_full`，再逐拍把 `data_to_send` 经 MISO 送出，每发一字节清一次 `buffer_full`、放行下一字节（4.3.5 + 4.4）。
5. 主机收到 33 字节，取 `recv_data[0]`（状态）的 bit0 判断有效，把 `recv_data[1..30]` 当像素存盘。

**验收**：能在图上指出三个反压/握手点——`comm_data_out_free`（4.4）、`SPISR` bit3/bit4（4.1/4.3）、主机 `STATUS_FPGA_RECV_MASK` 重试（4.3.6）——并说清它们各自防止什么（丢字节 / 读空 / 假确认）。

> 待本地验证：完整时序建议在真实 iCE40 板上用逻辑分析仪同时抓 `SCK`/`MOSI`/`MISO`/`SS` 与 `led_debug`，对照本图确认。

## 6. 本讲小结

- `SB_SPI` 是 iCE40 片上硬核，`spi_interface.v` 用「寄存器读写」握手（`stb/rw/adr/dati/dato/ack`）驱动它，不必逐 bit 处理串行线。
- 上电后先走 `INIT_SPICR0…INIT_SPICSR` 五个状态写控制寄存器（使能、LSB first、分频等），再进 `SPI_WAIT_RECEPTION` 收发循环。
- 主机与 FPGA 对同一操作码数值用**镜像命名**（SEND↔RECEIVE、READ↔SEND），`SPI_READ_OPCODE` 按 opcode + `counter_read` 把 SPI 字节翻译成 `spi_cmd`/`spi_data_out`（即 `image_processing` 的 `comm_cmd`/`comm_data_in`）。
- `SPI_TRANSMIT` 的状态字节 = `0x40 | buffer_full`：bit6 是固定标记（主机 `STATUS_FPGA_RECV_MASK`），bit0 告诉主机后续字节是否为有效输出。
- `buffer_full` + `spi_data_in_free` 构成 `image_processing` 输出方向的单字节缓冲与反压，`SEND_DATA32`/`RECEIVE_DATA32` 用 33 字节批量事务摊薄每字节开销、逼近 SPI 吞吐上限。

## 7. 下一步学习建议

- 下一篇 **u6-l4 主机 SPI 软件：FTDI 与命令封装** 会从主机视角讲 `spi_lib.c` 如何用 FTDI MPSSE 引擎产生 SPI 时序、以及 `STATUS_FPGA_RECV_MASK` 重试循环的完整实现，与本讲 FPGA 侧逐字节对应。
- 想从全局看一次完整运算的数据通路，可读 **u7-l1 端到端数据通路与架构取舍**，把仿真（FIFO）与硬件（SPI）两条通路并排比较。
- 若要动手扩展，**u7-l2 添加一个新的图像处理操作** 会要求你同时改 `image_processing.hpp`、两套后端 cpp、以及 `image_processing.v`，届时可回头检验本讲对「SPI 包 ↔ comm 接口」翻译的理解是否到位。
