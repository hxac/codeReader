# 端到端数据通路与架构取舍

## 1. 本讲目标

前面六单元我们把项目拆成了一块块零件：抽象契约（u2）、核心 HDL 端口与存储（u3）、各类运算状态机（u4）、卷积流水线（u5）、两套后端（u6）。本讲是「综合实践与扩展」单元的第一篇，目标是**把零件装回一台完整的机器**。

读完本讲你应该能够：

- 把「主机一次高层调用 → 像素最终被处理」的完整数据通路，在一张图上从左到右走一遍，并说出每一层做了什么。
- 准确指出通路上 **三个独立的握手/反压点**，并解释为什么输入方向是「开环」、输出方向是「闭环」。
- 对照仿真模式与硬件模式两条通路，说出它们的「共同内核」与「可替换外壳」分别是什么。
- 评估项目里四项关键硬件取舍（16 位打包、双缓冲、定点数、单端口 RAM 两拍流水）各自的「代价 ↔ 收益」。

## 2. 前置知识

本讲是综合篇，默认你已经读过以下讲义（不会重复其细节，只做串联）：

- **u3-l3**：`image_processing.v` 的双 FSM 架构（`state` 命令解析 + `state_processing` 运算执行）、`STATE_WAIT_COMMAND` 派发、`processing_command` 工单寄存器。
- **u5-l1**：运算 FSM 的交接机制（读完参数后一次性写入 `state_processing` 与 `processing_command`，让复用状态选算法分支）。
- **u6-l1**：仿真后端的 `Operation` 队列、`main_loop_clk()` 手动时钟、`counter_free` 反压、`memory[]` 数组模拟 RAM。
- **u6-l2 / u6-l3 / u6-l4**：硬件后端的 `top.v` 三模块连线、`SB_SPI` 从机、FTDI/MPSSE 主机软件、`STATUS_FPGA_RECV_MASK` 重试。

两个贯穿全讲的术语先约定清楚：

- **传输外壳（transport shell）**：把高层调用变成字节流、再搬到模块 `comm` 接口的物理通道。仿真用内存队列，硬件用 SPI 总线。它是「可替换」的部分。
- **共同内核（common core）**：`image_processing.v` 模块本身，以及它消费的字节级命令协议。两套后端跑的是**同一份** HDL、**逐字节相同**的命令流。它是「不可替换」的部分。

本讲的全部洞察都来自这一句话：**外壳可换，内核不变**。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它说明什么 |
|------|------|------------------|
| `hdl/image_processing.v` | 共同内核（双 FSM + 存储/通信接口） | 端到端通路的「目的地」，三个握手点的接收方 |
| `simulation/image_processing_simulation.cpp` | 仿真传输外壳 | FIFO 队列 + `main_loop_clk` + 时间型反压 |
| `ice40/hdl/top.v` | 硬件顶层连线 | 三模块如何用 wire 拼成完整通路、命名翻转 |
| `ice40/software/image_processing_ice40.cpp` | 硬件主机软件 | 高层调用 → SPI 事务的翻译 |
| `ice40/hdl/spi_interface.v` | SPI 从机翻译层 | SPI 字节 → comm 接口的转换（对照） |
| `ice40/software/spi_lib/spi_lib.c` | FTDI 底层 | `STATUS_FPGA_RECV_MASK` 重试闭环 |

## 4. 核心概念与源码讲解

### 4.1 端到端数据流：一次完整运算的全景图

#### 4.1.1 概念说明

我们把「主机调用 `send_add(value, clamp)` 直到运算结束」当作贯穿全讲的样例。选择它是因为它**最短且最典型**：带 3 个参数字节、触发一次真正的像素运算、需要 `wait_end_busy` 等待——完整覆盖「发命令 → 读参数 → 运算 → 回查状态」四段。

关键直觉：一次运算在通路上要穿过 **五个抽象层**，每层只和相邻层打交道：

1. **业务层**：`main.cpp` 调用 `img_proc->send_add(...)`，`img_proc` 是纯虚基类指针。
2. **后端层**：仿真或硬件子类把这次调用拆成「1 命令字节 + N 参数字节」的序列。
3. **传输层**：把字节序列搬到模块的 `comm` 接口（队列 或 SPI）。
4. **内核层**：`image_processing.v` 的双 FSM 消费字节、驱动存储器接口执行运算。
5. **存储层**：RAM（C++ 数组 或 SPRAM）真正保存像素。

注意第 1、4、5 层在两套后端里**完全相同**，只有第 2、3 层不同——这正是「外壳可换」的体现。

#### 4.1.2 核心流程

下图把五层横向铺开，箭头表示一次 `send_add` 之后的数据走向（⬇ 为跨层、→ 为同层推进）：

```
业务层   img_proc->send_add(value, clamp)          [多态分派]
            │
            ▼
后端层   ┌─ 仿真: Operation(true,COMMAND_APPLY_ADD) + 3×Operation(false,...) 入队 fifo_in
         └─ 硬件: spi_command_send(SPI_SEND_CMD,COMMAND_APPLY_ADD) + 3×SPI_SEND_DATA
            │
            ▼
传输层   ┌─ 仿真: main_loop_clk() 翻转 clk，每拍弹 1 项喂 comm_cmd/comm_data_in
         └─ 硬件: FTDI─MPSSE→SCK/MOSI→SB_SPI→spi_interface 翻译为 comm_cmd/comm_data_in(+valid)
            │
            ▼  comm_cmd=COMMAND_APPLY_ADD, comm_data_in_valid=1
内核层   STATE_WAIT_COMMAND 派发 ──► STATE_APPLY_ADD_READ_PARAM(读3参数)
            │  交接拍: state_processing<=STATE_PROC_UNARY, processing_command<=COMMAND_APPLY_ADD
            ▼
         STATE_PROC_UNARY: 偶拍 rd_en 读 storage ─► 奇拍 add+wr_en 写回 storage (2像素/字)
            │  proc_counter_read 归零 ─► state_processing<=STATE_IDLE
            ▼
存储层   addr/wr_en/rd_en ─► RAM (memory[] 或 4×SPRAM)
            │
            ▼
回查    wait_end_busy: 发 COMMAND_GET_STATUS ─► bit0 = ~(state_processing==IDLE) ─► 循环直到 0
```

一个关键观察：**回查是「拉」而不是「推」**。内核运算完后不会主动通知主机，而是主机反复发 `COMMAND_GET_STATUS` 去读 `busy` 位。这意味着内核的 `state_processing` 状态机在长运算（如卷积）期间仍然空闲地响应状态查询——这正是双 FSM 分工的价值（见 u3-l3）。

#### 4.1.3 源码精读

**第 2 层·两套后端产出相同字节序列。** 仿真后端把命令和数据都压成 `Operation`，靠 `is_command` 标志区分：

[simulation/image_processing_simulation.cpp:L75-L78](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L75-L78) —— 仿真后端：1 个命令 `Operation` + 3 个数据 `Operation`（低字节、高字节、clamp）。

硬件后端把同样的 4 个字节拆成 4 个独立 SPI 事务：

[ice40/software/image_processing_ice40.cpp:L93-L96](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L93-L96) —— 硬件后端：`SPI_SEND_CMD` 发命令字，再 3 次 `SPI_SEND_DATA` 发参数。

> 两段代码用的表达式不同（`Operation(true/false,...)` vs `spi_command_send(SPI_SEND_CMD/DATA,...)`），但产出的 4 个字节 **`[COMMAND_APPLY_ADD, value_low, value_high, clamp]` 完全一致**。这是「共同内核」成立的前提。

**第 4 层·内核派发与交接。** 命令字节到达后，`STATE_WAIT_COMMAND` 把它导向参数读取状态，并预装计数器：

[hdl/image_processing.v:L245-L248](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L245-L248) —— 收到 `COMMAND_APPLY_ADD`，跳到 `STATE_APPLY_ADD_READ_PARAM`，`counter_read<=2`（还要读 2 个字节才到 clamp 字节）。

读完 3 个参数后，在 `counter_read==0` 那一拍**一次性**交接给运算 FSM：

[hdl/image_processing.v:L363-L369](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L363-L369) —— 锁存 `clamp`，同时设置 `state_processing<=STATE_PROC_UNARY`、`processing_command<=COMMAND_APPLY_ADD`、`proc_counter_read<=W*H`、`proc_memory_addr_counter<=buffer_storage_address`，然后主 FSM 回到 `STATE_WAIT_COMMAND` 等下一条命令。

**第 4 层·运算执行（两拍流水）。** `STATE_PROC_UNARY` 用地址计数器最低位当节拍器，偶拍读、奇拍算且写回：

[hdl/image_processing.v:L509-L520](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L509-L520) —— 偶拍 `proc_memory_addr_counter[0]==0` 发起 `rd_en`；奇拍收到 `data_read_valid` 后，对 16 位字的高、低两个像素分别做 `+add_value` 并 `apply_clamp`。

写回时把地址最低位清零，还原成读时的偶字地址，实现 in-place 读改写：

[hdl/image_processing.v:L548-L557](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L548-L557) —— `wr_en<=1`、`addr<={proc_memory_addr_counter[31:1],1'b0}`；`proc_counter_read` 每字减 2，归零时 `state_processing<=STATE_IDLE`（busy 位随之降下）。

#### 4.1.4 代码实践

> **实践目标**：把 4.1.2 的全景图落到具体调用上，确认「业务调用 → 字节序列 → 状态跳转」三层对齐。
>
> **操作步骤**：
> 1. 在 `software/main.cpp` 中找到一个调用 `send_add` 的 `test_*` 函数（如 `test_add_threshold`）。
> 2. 在它前后各加一行 `printf`（**示例代码，不改源码逻辑**），例如 `printf(">>> before send_add\n");`。
> 3. 打开三个窗口并排对照：该 `test_*`、`simulation/image_processing_simulation.cpp:72` 的 `send_add`、`hdl/image_processing.v:245` 的 `COMMAND_APPLY_ADD` 分支。
> 4. 用笔在每个字节下方标注它被哪个 `counter_read` 值消费。
>
> **需要观察的现象**：`send_add` 一次调用产生 4 次后端动作；内核里 `counter_read` 经历 `2 → 1 → 0` 三次，第 3 次正是交接拍。
>
> **预期结果**：你能用一张「字节 ↔ counter_read ↔ 状态」对照表，把主机发出的每个字节钉到内核的具体某一拍上。
>
> **待本地验证**：若本地装了 verilator，可用 `build_simulation.sh` 跑一遍，对照 `printf` 输出的 `sending img_width8`/`read status` 行序与你的标注是否一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `send_add` 改成 `send_image_invert`（无参数），交接发生在哪一拍？为什么和 `send_add` 不同？

**答案**：发生在 `STATE_WAIT_COMMAND` 收到 `COMMAND_APPLY_INVERT` 的**当拍**，直接设置 `state_processing<=STATE_PROC_UNARY`（见 `hdl/image_processing.v:262-269`），无需经过单独的读参数状态——因为取反是零参数运算，没有参数要读。

**练习 2**：为什么 `wait_end_busy` 必须放在「发运算命令」之后、而不能由内核主动通知？

**答案**：内核的通信接口是单向「主机→模块发命令 / 模块→主机回数据」，没有异步中断线；`state_processing` 的状态变化只能通过主机主动发 `COMMAND_GET_STATUS` 拉取 `busy` 位才能被外界感知。

---

### 4.2 握手与反压汇总：通路上的三个关卡

#### 4.2.1 概念说明

「握手」是两个部件之间协调「我现在有数据 / 我现在能收」的机制。本项目的端到端通路上有 **三个相互独立的握手点**，它们位于不同层、由不同信号承载、方向也不同。理解这三个点是看懂「为什么两套截然不同的后端都能跑通同一份 HDL」的钥匙。

三个握手点：

| # | 名称 | 信号 | 方向 | 反压？ |
|---|------|------|------|--------|
| ① | 输入握手 | `comm_data_in_valid` | 主机→模块 | **无**（开环） |
| ② | 输出握手 | `comm_data_out_valid` + `comm_data_out_free` | 模块→主机 | **有**（闭环） |
| ③ | 存储握手 | `rd_en`/`wr_en` + `data_read_valid` | 模块↔RAM | 有（读延迟对齐） |

**最反直觉的一点**：输入方向（①）没有 `comm_data_in_free` 信号——模块端口列表里根本不存在它（见 u3-l1 的 14 个端口）。也就是说，模块**默认主机自己会掌握好节奏**，不会发太快。这把「控速」的责任完全甩给了传输外壳，是输入开环、输出闭环的根本原因。

#### 4.2.2 核心流程

三套后端用三种不同方式实现同一个握手语义：

```
握手① 输入(主机→模块)  开环，靠主机自律
  仿真外壳: 固定循环 main_loop_clk N 次 (send_image 用 W*H+500 次)
  硬件外壳: STATUS_FPGA_RECV_MASK 重试, 最多 10 次直到 bit6=1
  内核侧 : 只要 comm_data_in_valid=1 且当前状态期待数据, 就消费

握手② 输出(模块→主机)  闭环, comm_data_out_free 是真正的反压闸门
  仿真外壳: counter_free——模块每吐 1 字节, free 拉低 3 拍
  硬件外壳: spi_data_in_free = !(buffer_full || spi_data_in_valid) 组合逻辑
  内核侧 : 只有 comm_data_out_free=1 才置 comm_data_out_valid=1 推字节

握手③ 存储(模块↔RAM)  闭环, 吸收单端口 RAM 的一拍读延迟
  仿真外壳: rd_en 当拍即置 data_read_valid=1 (无延迟, 但语义对齐)
  硬件外壳: rd_en_buffer 三级移位检测下降沿, 延迟一拍对齐 SPRAM
  内核侧 : 发 rd_en 后等 data_read_valid=1 才用 data_read
```

#### 4.2.3 源码精读

**内核侧的「默认撤销」是所有握手的起点。** 每个 `posedge clk` 开头先把三个脉冲输出清零，状态体内按需重新置位——这是标准 FSM 脉冲模式：

[hdl/image_processing.v:L213-L216](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L213-L216) —— 默认 `comm_data_out_valid<=0; wr_en<=0; rd_en<=0;`，保证握手信号是单拍脉冲。

**握手②的内核端**：`STATE_GET_STATUS` 只有在 `comm_data_out_free==1` 时才推字节，是闭环反压的接收侧：

[hdl/image_processing.v:L310-L315](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L315) —— `if(comm_data_out_free==1)` 才置 `comm_data_out_valid<=1`；首字节 `comm_data_out[0] <= ~(state_processing==STATE_IDLE)` 即 busy 位。

**握手②的仿真外壳**：用 `counter_free` 模拟「通信线被占满」。模块每输出一字节，`free` 拉低 3 拍：

[simulation/image_processing_simulation.cpp:L230-L234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L230-L234) —— 每拍先消耗 `counter_free`，再算 `comm_data_out_free = (counter_free==0)`。

[simulation/image_processing_simulation.cpp:L248-L252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252) —— 模块输出一字节后 `counter_free=3`，立即把 `free` 拉低；之后 3 拍逐步恢复。这逼模块按真实握手等待，覆盖与硬件相同的输出代码路径。

**握手②的硬件外壳**：用组合逻辑实时反映单字节缓冲是否空：

[ice40/hdl/spi_interface.v:L52-L53](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L52-L53) —— `assign spi_data_in_free = !(buffer_full || spi_data_in_valid);`，接到模块的 `comm_data_out_free`。缓冲满或新数据正在进，就不 free。

**握手①的硬件外壳（输入开环的闭环补救）**：因为内核没有 `comm_data_in_free`，硬件主机改用「重试确认」来确保 FPGA 真收到：

[ice40/software/spi_lib/spi_lib.c:L249-L254](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L249-L254) —— 发 4 字节、读回 4 字节，只要回送的第 3 字节 `bit6`（`STATUS_FPGA_RECV_MASK`）未置位就重发，最多 10 次。

`STATUS_FPGA_RECV_MASK` 定义为 `0x1<<6`，对应 FPGA 在 `SPI_TRANSMIT` 状态发的状态字节 `0x40`：

[ice40/software/spi_lib/spi_lib.h:L65-L68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L65-L68) —— 掩码定义。

[ice40/hdl/spi_interface.v:L175-L177](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L175-L177) —— FPGA 侧 `counter_send==0` 时发 `8'b01000000`（bit6 恒 1）作为「已响应」信标，`bit0` 带 `buffer_full`。

#### 4.2.4 代码实践

> **实践目标**：给每个 `valid`/`free` 信号归类到正确的握手点。
>
> **操作步骤**：
> 1. 在 `hdl/image_processing.v` 的端口声明（L13-L30）里圈出所有握手信号。
> 2. 画一张三列表格：列头分别是「握手①输入」「握手②输出」「握手③存储」。
> 3. 把每个信号填入对应列，并标注「谁置位、谁检测」。
>
> **需要观察的现象**：你会发现 `comm_data_in_valid` 那一列「谁检测」是模块、但「谁置位」是外壳；而 `comm_data_out_free` 则反过来。
>
> **预期结果**：表格里握手①只有 `valid` 没有 `free`，握手②既有 `valid` 又有 `free`——直观体现「输入开环、输出闭环」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `STATE_READ_IMG` 里模块要检查 `comm_data_out_free` 才输出，而 `STATE_SEND_IMG` 里模块**不检查**任何 free 就直接接收？

**答案**：发送方向是握手①（输入开环），内核没有 `comm_data_in_free` 可查，默认主机自律；读出方向是握手②（输出闭环），模块必须等 `comm_data_out_free==1` 才能推字节，否则会覆盖尚未被消费的数据。

**练习 2**：仿真后端为什么要在 `read_image` 里循环 `W*H*20` 次 `main_loop_clk`（远大于像素数）？

**答案**：因为握手②被 `counter_free` 限速——模块每输出一字节就被压 3 拍，外加 `STATE_READ_IMG` 的「读存储→缓冲→输出」两级流水，所以实际拍数远大于像素数；乘 20 是给时间型反压留的足够余量（见 `simulation/image_processing_simulation.cpp:154`）。

---

### 4.3 仿真与硬件通路对比：同一内核，两副外壳

#### 4.3.1 概念说明

现在把两条通路并排放在一起。它们的**共同内核**是 `image_processing.v`（连同它消费的命令协议）；**可替换外壳**是时钟来源、传输介质、存储介质、反压实现。理解对比的意义在于：当你以后想换第三种外壳（比如换成以太网、或换成 AXI 总线接入 SoC），你只需要替换「外壳」列，内核一行都不用动。

#### 4.3.2 核心流程

| 维度 | 仿真后端 | 硬件后端 |
|------|----------|----------|
| **时钟** | `main_loop_clk()` 手动翻转 `clk` 并两次 `eval()` | iCE40 板上晶振（综合后接 `clk` 引脚） |
| **传输外壳** | 内存队列 `fifo_in`/`fifo_out` | SPI 总线：FTDI MPSSE → `SB_SPI` → `spi_interface` |
| **命令映射** | `Operation(is_command=true/false,...)` | `SPI_SEND_CMD`/`SPI_SEND_DATA` 事务 |
| **存储外壳** | `uint16_t memory[512*128]` C++ 数组 | 4 片 `SB_SPRAM256KA`（`ram_interface.v`） |
| **输入控速(①)** | 固定循环 N 次（时间估计） | `STATUS_FPGA_RECV_MASK` 重试（闭环确认） |
| **输出反压(②)** | `counter_free` 固定 3 拍占线 | `spi_data_in_free` 组合逻辑（真实缓冲态） |
| **存储延迟(③)** | `rd_en` 当拍即 `data_read_valid` | `rd_en_buffer` 移位对齐 SPRAM 一拍延迟 |

一条规律：**越靠「物理」的外壳，反压越接近真实信号、越靠「软件」的外壳，反压越靠时间估计**。仿真为了不写复杂的等待逻辑，索性用大循环常数「蒙」够时间；硬件面对真实硅片，必须用信号闭环。

#### 4.3.3 源码精读

**顶层连线的「命名翻转」**是硬件外壳最巧妙的一笔。`top.v` 里三个模块的 comm 连线存在「我之 out 即彼之 in」：

[ice40/hdl/top.v:L32-L45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32-L45) —— `image_processing` 的 `.comm_data_in(ip_comm_data_in)` 接的是 `spi_interface` 的 `.spi_data_out(...)`；`image_processing` 的 `.comm_data_out(...)` 接的是 `spi_interface` 的 `.spi_data_in(...)`。命名翻转是因为「SPI 侧说『我要发给内核的数据』= 内核侧说『我从通信口收到的数据』」。

**仿真外壳如何驱动内核**——`main_loop_clk` 每拍弹一个 `Operation`，按 `is_command` 决定喂 `comm_cmd` 还是 `comm_data_in`：

[simulation/image_processing_simulation.cpp:L235-L246](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L235-L246) —— 弹出队首 `Operation`，命令则驱动 `comm_cmd`、数据则驱动 `comm_data_in`，二者都拉高 `comm_data_in_valid`。

**硬件外壳如何驱动内核**——`spi_interface` 的 `SPI_READ_OPCODE` 状态把 SPI 字节翻译成 `comm_cmd`/`comm_data_in`：

[ice40/hdl/spi_interface.v:L246-L254](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L246-L254) —— `counter_read==1` 时，按 `command_data[0]`（SPI 操作码）是 `RECEIVE_CMD` 还是 `RECEIVE_DATA`，分别驱动 `spi_cmd` 或 `spi_data_out` 并拉高 `spi_data_out_valid`。这两个信号经 `top.v` 连线就是内核的 `comm_cmd`/`comm_data_in_valid`。

> 对比这两段：仿真用「队列 + 一个 `is_command` 布尔」，硬件用「SPI 操作码 `RECEIVE_CMD`/`RECEIVE_DATA` + 帧内字节序号 `counter_read`」。机制不同，但喂给内核的字节流逐字节相同。

#### 4.3.4 代码实践

> **实践目标**：亲手验证两条通路产出字节流的一致性。
>
> **操作步骤**：
> 1. 取参数 `value=5, clamp=1`。
> 2. 对照 `simulation/image_processing_simulation.cpp:72-78` 写出仿真压入的字节序列。
> 3. 对照 `ice40/software/image_processing_ice40.cpp:90-96` 与 `spi_command_send` 的发送缓冲（`spi_lib.c:245` 的 `to_send[]={cmd,p0,p1,p2}`）写出硬件发送的字节序列。
> 4. 两序列并排比较。
>
> **需要观察的现象**：剥去 `Operation(...)` 包装和 `SPI_SEND_CMD/DATA` 事务头之后，核心字节都是 `[4, 0x05, 0x00, 0x01]`（`COMMAND_APPLY_ADD=4`）。
>
> **预期结果**：两序列的核心 4 字节完全相同，证明「外壳可换、内核不变」。

#### 4.3.5 小练习与答案

**练习 1**：仿真后端的 `send_image` 用 `+500` 作为余量（L67），硬件后端用 32 字节批量事务（`SPI_SEND_DATA32`）。两者解决的是同一个什么问题？

**答案**：都是「输入开环」下的**主机控速/效率**问题。仿真用固定循环蒙够时间；硬件因为 SPI 每个事务有固定命令开销（每帧 4 字节里只有部分是有效数据），改用 32 字节批量事务把有效数据占比从约 25% 提到约 97%，逼近 SPI 吞吐上限（见 u6-l4）。

**练习 2**：如果要把内核接入一颗带 AXI 总线的 SoC，外壳要改哪几层、内核要改吗？

**答案**：只改传输外壳（新增一个把 AXI 写事务翻译成 `comm_cmd`/`comm_data_in` 的桥）和存储外壳（用 SoC 的 SRAM 替换 SPRAM）；`hdl/image_processing.v` 一行都不用改，因为它只认接口、不认实现。

---

### 4.4 架构取舍清单：面积、吞吐与精度的博弈

#### 4.4.1 概念说明

项目的每一处「不寻常」写法都不是随手为之，而是资源约束下的**主动取舍**。iCE40 UltraPlus 的约束是：片上存储仅 1Mbit（128KB）、无浮点单元、单端口块 RAM、逻辑单元有限。本节评估四项关键取舍，每项都按「**约束 → 取舍 → 代价**」三段式说明，让你判断「如果预算翻倍，哪项该最先改」。

四项取舍一览：

| 取舍 | 换来了什么 | 付出了什么 |
|------|------------|------------|
| 16 位字打包 2 像素 | 存储带宽×2、地址空间减半 | 要求像素数为偶、奇偶地址处理复杂 |
| 双缓冲 input/storage | 零拷贝链式运算 | 单图上限 64KB（256×256）、128KB 对半切 |
| 1.3.4 定点数 | 整数乘法器即可做实数运算 | 精度仅 1/16、范围约 ±8 |
| 单端口 RAM 两拍流水 | 省一个读端口（省面积） | 每像素需 2 拍、读改写冒险要编排 |

#### 4.4.2 核心流程

下面用伪代码浓缩四项取舍的「触发条件」，详细行号见 4.4.3：

```
// 取舍 A: 16 位打包
每收到 2 个字节 → 凑成 1 个 16 位字 → 1 次 wr_en
代价: addr 末位清零得字地址; 像素数必须为偶

// 取舍 B: 双缓冲
COMMAND_SWITCH_BUFFERS: buffer_input_address <=> buffer_storage_address  (仅换寄存器, 不搬数据)
代价: 单缓冲仅 64KB

// 取舍 C: 定点数
主机: float → 乘 16 量化成 uint8 (send_mult)
内核: (kernel × pixel) 累加 → apply_clamp_fixed16 取 [11:4] 除以 16 还原
代价: 精度 1/16

// 取舍 D: 单端口两拍
偶拍: rd_en (发起读)
奇拍: data_read_valid=1 → 运算 → wr_en (写回)
代价: 2 拍/字; binary/convolution 需更多状态编排读改写
```

#### 4.4.3 源码精读

**取舍 B 的源码根基**——存储参数划分：

[hdl/image_processing.v:L78-L83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L83) —— `MEMORY_SIZE=128KB`，`BUFFER_SIZE=BUFFER2_LOCATION=64KB`，把存储对半切成两块连续区间。

[hdl/image_processing.v:L253-L257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L257) —— `COMMAND_SWITCH_BUFFERS` 用两条非阻塞赋值在一个时钟沿**互换两个地址寄存器**，不搬任何数据，是零拷贝链式运算的关键。

**取舍 A 的源码根基**——发送时 2 字节凑 1 字：

[hdl/image_processing.v:L337-L343](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L337-L343) —— 偶字节进 `data_write[7:0]`，奇字节进 `[15:8]` 并置 `wr_en`，`addr<={memory_addr_counter[31:1],1'b0}` 清最低位得字地址。

**取舍 D 的源码根基**——单端口两拍流水（4.1.3 已引用，此处看节拍器本质）：

[hdl/image_processing.v:L509-L513](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L509-L513) —— `proc_memory_addr_counter[0]` 既是地址位、又是节拍器：`0` 发起读、`1` 运算写回。这一位的双重身份是「单端口 RAM 不能同拍读写」的直接产物。同样的约束在 `STATE_PROC_BINARY` 里演化成 `binary_read_buffer` + `operation_step` 多拍编排（u4-l3），在卷积里演化成 4 状态循环（u5）。

**取舍 C 的源码根基**——定点还原：

[hdl/image_processing.v:L166-L178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L166-L178) —— `apply_clamp_fixed16` 取 `in[11:4]`，即把被放大 16 倍的乘积右移 4 位（除以 16）还原成像素尺度，钳位判断用更宽的 `in[15:4]` 才能正确检测越界（u4-l2）。

数学上，定点乘法的还原可写成：

\[
\text{pixel}_{\text{out}} = \frac{(k_{\text{fixed}} \cdot p)}{16} = \frac{(k_{\text{real}}\cdot 16)\cdot p}{16} = k_{\text{real}}\cdot p
\]

主机侧 `send_mult` 用「从高位到低位减权重」的试凑把 float 量化成定点字节：

[simulation/image_processing_simulation.cpp:L112-L117](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L112-L117) —— 遍历 7 个位权 \(2^{i-4}\)，够减就减并置位，最终得到 `val_fixed_4_4`。注意主机「放大 16 倍」与内核「除以 16」必须配对，端到端才等价于浮点乘法。

#### 4.4.4 代码实践

> **实践目标**：评估「预算翻倍」时各项取舍的改进优先级。
>
> **操作步骤**：
> 1. 假设你换到一颗**双端口 RAM** + **128KB 翻倍到 256KB** + **带硬浮点单元**的 FPGA。
> 2. 对四项取舍各写一句：「若放宽约束 X，就可以去掉代价 Y」。
> 3. 排出改进优先级（哪个收益最大）。
>
> **需要观察的现象**：取舍 D（单端口两拍）影响面最大——它同时决定了 unary/binary/convolution 三类运算的状态机复杂度；放宽它能让卷积从 4 状态循环大幅简化。
>
> **预期结果**：优先级大致为 **D（双端口 RAM）> C（浮点）> A（带宽）> B（容量）**，因为 D 牵动最多代码。
>
> **待本地验证**：若你在本地改 `image_processing.v` 把单端口读改写成双端口同拍读写，需对照仿真输出确认像素结果不变——这是一个很有价值的扩展实验。

#### 4.4.5 小练习与答案

**练习 1**：双缓冲（取舍 B）的「零拷贝」具体省了什么？如果不做双缓冲、改用「搬数据」，会多花什么？

**答案**：省了一次「把结果从 storage 整块拷回 input 再读出」的存储搬运（W×H 次读写）。改用搬数据会多花 W×H 拍的存储带宽，且链式运算（如图像做加法后再做卷积）每步都要搬一次，吞吐骤降。

**练习 2**：为什么卷积的写回被刻意拆成 `WRITEBACK_1`/`WRITEBACK_2` 两个状态？这和哪项取舍直接相关？

**答案**：为了让 `convolution_buffer` 被综合器推断成块 RAM（SPRAM）——块 RAM 一拍只能写一次（u5-l2）。这与取舍 D（单端口/块 RAM 一拍一写）同根同源，是同一约束在卷积场景下的体现。

---

## 5. 综合实践

> **任务**：写一份《`COMMAND_APPLY_ADD` 端到端时序说明》，把仿真与硬件两条通路上每个关键信号的跳变顺序画出来，并指出反压机制的**本质差异**。这是本讲的核心交付物。

### 5.1 实践目标

把 4.1～4.3 的内容融合成一份具体到「信号电平」的时序文档。完成后，你应该能向一个没读过本项目的人讲清楚「主机按下回车后，FPGA 内部到底发生了什么」。

### 5.2 操作步骤

取参数 `add_value=5, clamp=1`，假设图像 8×8（`W=H=8`，64 像素）。

**Part A·仿真模式时序**（参考 `simulation/image_processing_simulation.cpp`）

按拍列出 `main_loop_clk` 的前若干拍，每拍记录：`fifo_in` 弹出的 Operation、`comm_cmd`、`comm_data_in`、`comm_data_in_valid`、模块 `state`、`counter_read`。关键节点：

1. 第 1 拍：弹 `Operation(true,COMMAND_APPLY_ADD)` → `comm_cmd=4, valid=1`，`state: WAIT_COMMAND→APPLY_ADD_READ_PARAM`，`counter_read<=2`。
2. 第 2-4 拍：依次弹 `0x05 / 0x00 / 0x01`，`counter_read: 2→1→0`，第 4 拍交接（`state_processing<=PROC_UNARY`）。
3. 第 5 拍起进入 `STATE_PROC_UNARY`：偶拍 `rd_en=1`，奇拍 `wr_en=1`，`proc_counter_read` 每字减 2，共 32 字（64 像素 / 2）。
4. `send_add` 内部跑满 100 拍后返回；`wait_end_busy` 反复发 `COMMAND_GET_STATUS` 读 `busy`，直到 `state_processing==IDLE` 使 `bit0=0`。

**Part B·硬件模式时序**（参考 `ice40/software/image_processing_ice40.cpp` 与 `spi_interface.v`）

按 SPI 事务列出，每事务记录：主机发送的 `to_send[4]`、主机读回的 `to_send[4]`、FPGA `spi_interface` 状态、是否触发 `comm_cmd/comm_data_in_valid`。关键节点：

1. 事务 1：主机发 `[SPI_SEND_CMD=3, COMMAND_APPLY_ADD=4, 0, 0]`，重试直到读回第 3 字节 `bit6=1`；FPGA 在 `SPI_READ_OPCODE` 把 `spi_cmd<=4`、`spi_data_out_valid<=1`。
2. 事务 2-4：各发 `[SPI_SEND_DATA=4, param, 0, 0]`，FPGA 把 param 送到 `comm_data_in`。
3. 内核 FSM 演化与 Part A **完全相同**（同一份 HDL）。
4. `wait_end_busy` → `read_status`：发 `SPI_READ_DATA` 事务，检查读回字节 `bit0`（busy）。

**Part C·反压本质差异**（本任务的核心结论）

写一段对比，要点：

- **仿真反压是「时间型」**：`counter_free` 把 `comm_data_out_free` 按固定 3 拍压低；输入方向干脆用 `W*H+500` 之类的大循环常数「蒙」够时间。它是开环估计。
- **硬件反压是「信号型/闭环」**：输出用 `spi_data_in_free`（组合逻辑，真实反映单字节缓冲态）；输入用 `STATUS_FPGA_RECV_MASK` 重试（主机主动探测直到 FPGA 回 `0x40` 确认）。
- **本质差异一句话**：仿真「假设时间够用就用够多的拍」，硬件「不假设、用信号确认」。这也解释了为什么仿真代码里有那么多魔数循环（`+500`、`*20`、`100`），而硬件代码里全是重试和掩码检查。

### 5.3 需要观察的现象

写完后自检：Part A 和 Part B 中「内核 `state` 的跳转序列」是否**逐拍一致**？如果一致，就证明了「外壳可换、内核不变」；它们的差异是否全部集中在「字节怎么到达 `comm` 接口」这一段？

### 5.4 预期结果

得到一份两栏时序表（仿真 | 硬件），上面是各自的外壳信号，下面是共同的内核状态序列；表底用一句话点出反压的时间型 vs 信号型差异。

### 5.5 待本地验证

若本地有 verilator，可实际跑 `build_simulation.sh`，用 `printf`（内核里已有大量调试打印，如 `simulation/image_processing_simulation.cpp:256` 的 `read req addr`）核对 Part A 的拍序；硬件部分若无实物板，则按源码静态推导即可，标注「待硬件验证」。

## 6. 本讲小结

- 端到端通路分五层（业务 → 后端 → 传输 → 内核 → 存储），其中业务、内核、存储三层在两套后端里完全相同，只有后端与传输两层是「可替换外壳」。
- 通路上有三个独立握手点：输入（开环，无 free）、输出（闭环，`comm_data_out_free`）、存储（`data_read_valid` 对齐读延迟）。输入开环、输出闭环是最关键的不对称。
- 仿真与硬件两条通路跑同一份 HDL、同一条字节流；仿真用内存队列 + 手动时钟，硬件用 SPI + 真实晶振，差异全在传输外壳与反压实现。
- 四项架构取舍（16 位打包、双缓冲、1.3.4 定点、单端口两拍）都源于 iCE40 资源约束，其中单端口约束牵动面最大，决定了 unary/binary/convolution 三类状态机的复杂度。
- 主机「放大 16 倍」与内核「除以 16」必须配对，端到端才等价于浮点运算——这是定点取舍的数学本质。

## 7. 下一步学习建议

- **动手扩展**：下一篇 u7-l2《扩展实践：添加一个新的图像处理操作》会让你完整新增一条命令（如 `COMMAND_APPLY_GAMMA`），同时改接口、两套后端、内核状态机——那是验证你是否真懂端到端通路的最好试金石。
- **深读源码**：重读 `hdl/image_processing.v:503-842` 的整个 `case(state_processing)`，把本讲的「单端口两拍」取舍在 unary/binary/convolution 三处的不同演化（2 拍 / 多拍 / 4 状态）对照看一遍。
- **对比两条后端**：并排打开 `simulation/image_processing_simulation.cpp` 与 `ice40/software/image_processing_ice40.cpp`，逐函数比对它们如何把同一个高层调用翻译成字节——这是巩固「外壳可换」直觉的最佳练习。
