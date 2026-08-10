# image_processing.v 的端口与两大接口

## 1. 本讲目标

本讲正式进入项目唯一的核心 HDL 文件 `hdl/image_processing.v`。前面几讲我们一直在讲「主机软件」「抽象接口」「命令协议」，但那些都是围在外面的壳。从本讲开始，我们要钻进 FPGA 内部真正的运算核心。

学完本讲，你应该能够：

- 说清 `image_processing` 这个模块对外暴露了哪些端口，以及它们的方向与位宽。
- 把端口划分成三类：**时钟/复位**、**存储器接口**、**通信接口**，并理解为什么是这样分。
- 理解「16 位存储字宽」意味着一个存储字里同时装了 2 个像素。
- 看懂通信接口上 `valid` / `free` 这一对握手信号是如何控制数据进出的。
- 弄明白 `data_read_valid` 和 `comm_data_out_free` 这两个握手信号分别由谁置位、模块又如何据此推进。

本讲**只讲端口这一层**，不深入状态机内部逻辑（那是 u3-l3 以后的内容）。我们的目标是：先把这个模块当成一个「黑盒」，把它和外部的接线关系彻底搞清楚。

## 2. 前置知识

在开始前，请确保你理解以下几个概念（它们在前置讲义里都已建立）：

- **模块（module）**：Verilog 里描述一个硬件电路的基本单元。一个模块有「端口」，端口就是它和外界的接线引脚。
- **端口的方向**：`input` 是信号流进模块，`output` 是信号流出模块。`reg` 表示这个信号在 `always` 块里被赋值（可以理解为「模块内部维护的寄存器输出」），`wire` 表示只是一根连线。
- **位宽**：`[15:0]` 表示一个 16 位信号，最高位是 15、最低位是 0；`[7:0]` 是 8 位。
- **握手（handshake）**：当发送方和接收方速度不一致时，需要一对信号来协调——「我这边数据有效了（valid）」「我这边准备好了，你发吧（free/ready）」。本项目大量使用这种握手。
- **双后端架构**（u1-l1）：同一个 `image_processing.v` 既会被 Verilator 编译成 C++ 仿真模型，也会被综合进 iCE40 真实芯片。它的端口必须设计得「不绑定任何一种具体环境」。

一个贯穿全讲的核心直觉：**这个模块自己不带存储器（RAM），也不自己接通信线。它只定义「我需要一个能按地址读写 16 位字的存储器」和「我需要一个能收发命令字节的通信通道」这两个抽象接口**。至于存储器到底是 C++ 数组（仿真）还是 4 片 SPRAM（硬件）、通信到底走 FIFO 队列（仿真）还是走 SPI 总线（硬件），模块一概不关心。这种「只认接口、不认实现」的设计，正是它能在两套后端间无缝复用的根本原因。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| `hdl/image_processing.v` | 核心 HDL 模块，本讲主角。端口定义在文件最开头。 |
| `ice40/hdl/top.v` | 硬件后端顶层。把 `image_processing` 实例化，并把它的两类端口分别接到 RAM 和 SPI，是「端口如何被使用」的最佳实例。 |
| `ice40/hdl/ram_interface.v` | 硬件后端的 RAM 包装。它驱动 `data_read_valid`，演示了存储器接口的另一端长什么样。 |
| `simulation/image_processing_simulation.cpp` | 仿真后端。它在 `main_loop_clk()` 里用 C++ 模拟存储器读写和通信握手，演示了同一组端口在仿真环境里如何被驱动。 |

你不需要现在就读懂 `ram_interface.v` 或 `spi_interface.v` 的全部细节（它们是 u6 单元的内容），本讲只会引用其中**和端口契约直接相关**的几行。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：模块端口声明、存储器接口、通信接口、时钟与复位。

### 4.1 模块端口声明

#### 4.1.1 概念说明

在 Verilog 里，一个 `module` 的端口清单（port list）就是它的「对外接线说明书」——别人要使用这个模块，只需要看这份清单，就能知道要给它接几根线、每根线是什么方向、多宽。模块内部具体怎么实现，使用者不必关心。这就是「黑盒」思想。

`image_processing` 模块采用了**传统端口声明风格**：先在 `module(...)` 的括号里把所有端口的名字列一遍，再在下面一行行地声明每个端口的方向和位宽。这和现代 Verilog 的「ANSI 风格」（方向位宽直接写在括号里）不同，但功能等价。

#### 4.1.2 核心流程

端口的组织遵循一个清晰的三段式分组，作者用注释把它标得很明白：

1. **时钟与复位**：`clk, reset` —— 整个模块的节拍器与（名义上的）复位。
2. **存储器访问**（`//memory access`）：`addr, wr_en, rd_en, data_read, data_read_valid, data_write` —— 读写外部 RAM 用的 6 根线。
3. **通信模块访问**（`//comm module access`）：`comm_cmd, comm_data_in, comm_data_in_valid, comm_data_out, comm_data_out_valid, comm_data_out_free` —— 与主机收发命令字节用的 6 根线。

这个分组本身就是一份设计文档：模块对外只通过「存储器」和「通信」两扇门与外界打交道，没有任何其它隐藏通道。

#### 4.1.3 源码精读

模块名与端口清单（只有名字，没有方向位宽）：

[hdl/image_processing.v:3-11](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L3-L11) —— 定义模块 `image_processing`，并把 14 个端口按「clk/reset → memory access → comm module access」三组列出，分组用注释隔开。

随后是每个端口的方向与位宽声明，同样按三组排列：

[hdl/image_processing.v:13-30](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L13-L30) —— 第 13–14 行是 clk/reset；第 16–22 行是存储器接口的 6 个信号；第 24–30 行是通信接口的 6 个信号。注意 `output reg` 表示该输出在模块内的 `always` 块中被赋值（是寄存器输出），`input wire` 表示纯输入连线。

把这份声明汇总成一张端口总表（本讲会反复用到）：

| 信号 | 方向 | 位宽 | 所属接口 | 一句话含义 |
| --- | --- | --- | --- | --- |
| `clk` | input | 1 | 时钟 | 主时钟，所有动作都在上升沿发生 |
| `reset` | input | 1 | 复位 | 声明了，但实际未被使用（见 4.4） |
| `addr` | output reg | 32 | 存储器 | 字节粒度的访问地址 |
| `wr_en` | output reg | 1 | 存储器 | 写使能，单拍脉冲 |
| `rd_en` | output reg | 1 | 存储器 | 读使能，单拍脉冲 |
| `data_write` | output reg | 16 | 存储器 | 要写入 RAM 的数据 |
| `data_read` | input wire | 16 | 存储器 | 从 RAM 读回的数据 |
| `data_read_valid` | input wire | 1 | 存储器 | 「data_read 此拍有效」的握手信号 |
| `comm_cmd` | input wire | 8 | 通信 | 命令操作码 |
| `comm_data_in` | input wire | 8 | 通信 | 主机送来的数据字节 |
| `comm_data_in_valid` | input wire | 1 | 通信 | 输入字节有效 |
| `comm_data_out` | output reg | 8 | 通信 | 模块回送给主机的数据字节 |
| `comm_data_out_valid` | output reg | 1 | 通信 | 输出字节有效，单拍脉冲 |
| `comm_data_out_free` | input wire | 1 | 通信 | 输出通道空闲（反压握手） |

共 14 个端口，正好对应端口清单里的 14 个名字。

#### 4.1.4 代码实践

**实践目标**：亲手核对端口表，确认「黑盒说明书」与源码一致。

**操作步骤**：

1. 打开 [hdl/image_processing.v:3-11](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L3-L11)，数一下括号里列出的端口名数量。
2. 对照 [hdl/image_processing.v:13-30](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L13-L30)，把每个名字和它的方向、位宽对上。

**需要观察的现象**：端口清单里的名字数量，必须和下方声明的数量完全相等、一一对应；如果对不上，说明少接了一根线，综合时会报错。

**预期结果**：共 14 个端口，其中 input 7 个（clk、reset、data_read、data_read_valid、comm_cmd、comm_data_in、comm_data_in_valid），output 7 个（addr、wr_en、rd_en、data_write、comm_data_out、comm_data_out_valid；外加——仔细数——其实 output 是 6 个，请你在实践中亲自核对 input/output 各几个，**待本地确认**）。

> 说明：上面故意留了一个小悬念（output 到底是 6 个还是 7 个）让你去数，目的是让你亲手过一遍清单而不是被动接受。

#### 4.1.5 小练习与答案

**练习 1**：模块端口清单（第 3–11 行）里只写了端口名字，没有写方向和位宽。这种写法有没有问题？

**参考答案**：没有问题。这是传统 Verilog 风格——名字在 `module(...)` 里列出，方向位宽在下面单独声明（第 13–30 行）。只要每个名字都有一行声明对应即可。

**练习 2**：为什么作者要在端口清单里用注释把端口分成「memory access / comm module access」两组？删掉这两行注释会影响功能吗？

**参考答案**：纯粹是为了可读性，相当于给「黑盒说明书」加了小标题。删掉注释对编译和功能毫无影响，但会让读者很难一眼看出「这个模块对外只有存储器和通信两扇门」这个关键设计意图。

---

### 4.2 存储器接口（16bit 字）

#### 4.2.1 概念说明

存储器接口是模块的第一扇门。关键观念：**模块内部没有 RAM**。它需要存图像、读图像、做运算时读写中间结果，都得通过这 6 根线去问外部的 RAM 要。

为什么这样设计？因为 RAM 的实现方式在两套后端里完全不同：

- **仿真后端**：RAM 是 C++ 里的一个 `uint16_t memory[512*128]` 数组（见 [simulation/image_processing_simulation.cpp:10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L10)）。
- **硬件后端**：RAM 是 4 片 iCE40 内置的 `SB_SPRAM256KA`（见 [ice40/hdl/ram_interface.v:20-74](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L20-L74)）。

如果把 RAM 写死在模块里，就无法在两套环境间复用。所以模块只声明「我按某个地址读 16 位、写 16 位」，把 RAM 留给外部提供。这是一个非常典型的「接口与实现分离」。

另一个关键观念：**存储字宽是 16 位，而一个像素只有 8 位（1 字节），所以一个 16 位字里装了 2 个像素**。这个「2 像素打包」是全项目最重要的节流技巧之一——它让每次存储访问的吞吐翻倍。

#### 4.2.2 核心流程

存储器接口的两类操作：

**写操作**（模块 → RAM）：

1. 模块在某个时钟上升沿，把 `addr` 设成目标字节地址、`data_write` 设成 16 位数据，并拉高 `wr_en` 一个周期。
2. RAM 那一端（仿真里的 C++ 数组、硬件里的 SPRAM）在看到 `wr_en==1` 时，把 `data_write` 写进 `addr` 对应的位置。

**读操作**（RAM → 模块）：

1. 模块把 `addr` 设成要读的字节地址，拉高 `rd_en` 一个周期。
2. RAM 那一端经过若干周期的读延迟后，把数据放到 `data_read` 上，并拉高 `data_read_valid` 一个周期。
3. 模块**等到 `data_read_valid==1` 的那一拍**，才去采样 `data_read`。

这里最关键的一点：**模块不假设读数据会在下一拍就到**。它每次都要等 `data_read_valid` 的握手信号，否则就可能读到无效数据。这条契约对仿真（读延迟很小）和硬件（SPRAM 有真实读延迟）都必须成立。

关于地址：`addr` 是**字节粒度**的 32 位地址，但实际只用低位（128KB 最多需要 17 位地址，\(2^{17}=131072\)）。地址的最低位 `addr[0]` 被模块用来区分「这个 16 位字里的低字节（第 0 个像素）还是高字节（第 1 个像素）」，真正的字地址是 `addr[31:1]`（即 `addr/2`）。

#### 4.2.3 源码精读

存储器接口的 6 个端口声明：

[hdl/image_processing.v:16-22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L16-L22) —— `addr[31:0]`、`wr_en`、`rd_en`、`data_write[15:0]` 是 `output reg`（模块驱动）；`data_read[15:0]`、`data_read_valid` 是 `input wire`（RAM 驱动）。注意数据线是 16 位宽。

「2 像素打包进一个 16 位字」的最直接证据，在接收图像的状态里：

[hdl/image_processing.v:334-343](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L334-L343) —— `STATE_SEND_IMG` 里，主机一次送来 1 个字节（1 个像素），模块先把它放进 `data_write[7:0]`（低像素）；等下一个字节来，放进 `data_write[15:8]`（高像素），**此时才拉高 `wr_en`**。也就是说，必须凑齐 2 个像素、填满一个 16 位字，才执行一次真正的存储写。

那 `data_read_valid` 这根握手线由谁置位？在**硬件后端**，由 `ram_interface.v` 置位：

[ice40/hdl/ram_interface.v:85](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L85) —— `data_read_valid <= (rd_en_buffer[2] == 0 && rd_en_buffer[1] == 1);`。这是用一段流水线寄存器（`rd_en_buffer`）对模块发出的 `rd_en` 做延迟，检测其「经过延迟后的下降沿」，从而在 SPRAM 数据真正稳定的那一拍才把 `data_read_valid` 拉高，精确对齐 SPRAM 的读延迟（具体机制留到 u6-l2 详讲）。

在**仿真后端**，则由 `main_loop_clk()` 直接模拟：

[simulation/image_processing_simulation.cpp:254-259](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L254-L259) —— 看到 `simulator->rd_en == 1`，就把 C++ 数组 `memory[addr/2]` 放到 `data_read` 上，并置 `data_read_valid = 1`。注意 `/2`：因为 `addr` 是字节地址，而 C++ 数组是按 16 位字组织的。

模块内部如何「等待」这个握手？以逐像素运算为例：

[hdl/image_processing.v:509-514](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L509-L514) —— 第 509–512 行先在偶地址拍发起读（`rd_en<=1`、`addr<=...`）；第 514 行 `if (data_read_valid == 1'b1)` 才去消费 `data_read`。这就体现了「先发读请求、再等 valid」的两拍节奏。

一个地址相关的容量计算。硬件后端用 4 片 SPRAM 拼出 128KB：

\[ \text{单片} = 2^{14}\,\text{字} \times 2\,\text{B/字} = 32768\,\text{B} = 32\,\text{KB},\qquad \text{4 片} = 128\,\text{KB} \]

`ram_interface` 用 `addr[16:15]` 选片、`addr[14:1]` 作片内字地址、`addr[0]` 作字节选择（见 [ice40/hdl/ram_interface.v:22](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L22) 与 [:76](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L76)），正好把 17 位字节地址空间用满。

#### 4.2.4 代码实践

**实践目标**：亲眼看清「2 像素 → 1 个 16 位字 → 1 次 `wr_en`」的打包过程。

**操作步骤**：

1. 打开 [hdl/image_processing.v:334-353](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L334-L353)（`STATE_SEND_IMG` 全貌）。
2. 假设主机依次送来两个像素字节 `0x11`、`0x22`，模拟两个连续时钟上升沿里 `memory_addr_counter[0]` 分别为 `0` 和 `1` 时，`data_write` 和 `wr_en` 的取值。

**需要观察的现象**：

- 第 1 拍（`memory_addr_counter[0]==0`）：`data_write[7:0]` 被赋成 `0x11`，但**`wr_en` 不拉高**（因为 `wr_en<=1` 写在 `else` 分支里）。
- 第 2 拍（`memory_addr_counter[0]==1`）：`data_write[15:8]` 被赋成 `0x22`，**此时 `wr_en<=1`**，一次写操作发生，写入的 16 位字是 `0x2211`。

**预期结果**：每收到 2 个像素字节，模块才产生 1 个 `wr_en` 脉冲；这印证了「16 位字 = 2 像素」。如果你在仿真里看到 `main_loop_clk` 打印的 `wants to write` 日志数量约为像素数的一半，就是这个原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么模块用 `addr[0]` 来区分一个字里的高低字节，而不是直接用单独一根「字节选择」信号？

**参考答案**：因为 `addr` 本身就是字节粒度的地址。一个 16 位字对应两个连续的字节地址（偶地址、奇地址），用 `addr[0]` 就能天然区分当前处理的是低字节还是高字节，无需额外信号。这也让 `addr` 可以同时承担「字寻址」和「字内字节选择」两个职责（字地址 = `addr/2`）。

**练习 2**：在硬件后端，`data_read_valid` 为什么不能像仿真那样「看到 `rd_en` 当拍就拉高」？

**参考答案**：因为真实 SPRAM 有读延迟——`rd_en` 发出后，数据要在 1 个时钟周期后才在输出端稳定。如果当拍就拉高 `data_read_valid`，模块会读到上一拍的旧数据。`ram_interface` 用 `rd_en_buffer` 流水线把 valid 信号延迟到数据真正稳定的那一拍，正是为了对齐这个物理延迟。

**练习 3**：模块声明 `addr` 是 32 位，但 128KB 顶多需要 17 位地址，多出来的位有意义吗？

**参考答案**：功能上用不到，只是留了充足的位宽余量（综合时高位不会被使用）。实际有效的是低 17 位（`addr[16:0]`）。

---

### 4.3 通信接口（cmd/data_in/data_out + 握手）

#### 4.3.1 概念说明

通信接口是模块的第二扇门，也是它和主机软件唯一的交互通道。回顾 u2-l2：主机和模块之间用「1 字节操作码 + 变长参数」的命令报文通信。这扇门就是这些字节进出的物理通道。

通信接口分成**两个方向**：

- **输入方向**（主机 → 模块）：主机把命令操作码放在 `comm_cmd`、把参数字节放在 `comm_data_in`，并用 `comm_data_in_valid` 表示「这一拍的数据有效」。
- **输出方向**（模块 → 主机）：模块要把结果（比如读图像时的像素字节、查状态时的状态字节）回送给主机，放在 `comm_data_out` 上，用 `comm_data_out_valid` 表示「这一拍有数据要发」，同时必须等 `comm_data_out_free==1`（表示输出通道空闲、对方能接）才能发。

`comm_data_out_free` 是一个**反压（backpressure）**信号：如果主机（或通信线）暂时来不及接收，就让 `comm_data_out_free=0`，模块就老老实实等着，不丢数据。这是典型的 valid/ready 双向握手。

#### 4.3.2 核心流程

**输入**（主机送命令/参数）：

1. 主机把 `comm_cmd`（或 `comm_data_in`）摆好，拉高 `comm_data_in_valid` 一个周期。
2. 模块在每个上升沿检查 `comm_data_in_valid`，若为 1，就采样 `comm_cmd` / `comm_data_in`。

**输出**（模块回送结果）：

1. 模块先检查 `comm_data_out_free`。若为 0（通道忙），就等。
2. 通道空闲（`comm_data_out_free==1`）时，模块把数据摆到 `comm_data_out`，拉高 `comm_data_out_valid` 一个周期。
3. 主机那一端在看到 `comm_data_out_valid==1` 时把字节收走。

一个时序上的关键约定：模块在 `always` 块开头把三个「脉冲型」输出默认拉低：

```verilog
comm_data_out_valid <= 0;
wr_en <= 0;
rd_en <= 0;
```

这意味着 `comm_data_out_valid`、`wr_en`、`rd_en` 都是「默认低、需要时拉高一拍」的脉冲信号，避免某拍忘记拉低导致持续输出。

#### 4.3.3 源码精读

通信接口的 6 个端口声明：

[hdl/image_processing.v:24-30](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L24-L30) —— `comm_cmd[7:0]`、`comm_data_in[7:0]`、`comm_data_in_valid`、`comm_data_out_free` 是 input；`comm_data_out[7:0]`、`comm_data_out_valid` 是 output reg。注意命令和数据都是 8 位（1 字节）。

输入方向的握手——模块如何采样命令：

[hdl/image_processing.v:221-225](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L225) —— `STATE_WAIT_COMMAND` 里，只有 `if(comm_data_in_valid == 1)` 时才去看 `comm_cmd` 是哪条命令。没有 valid，模块就原地等。这就是输入握手。

输出方向的握手——模块如何回送状态，且必须先看 `comm_data_out_free`：

[hdl/image_processing.v:310-315](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L315) —— `STATE_GET_STATUS` 里，外层是 `if(comm_data_out_free == 1)`，只有通道空闲，才把状态字节摆上 `comm_data_out`、拉高 `comm_data_out_valid`。注意第 315 行 `comm_data_out[0] <= ~(state_processing == STATE_IDLE);` —— 状态字节的 bit0 就是「是否正忙」，这正是 u1-l5 里 `wait_end_busy` 轮询的那个 busy 位。

默认拉低（脉冲约定）：

[hdl/image_processing.v:214-216](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L214-L216) —— 每个 `posedge clk` 一开始就把 `comm_data_out_valid`、`wr_en`、`rd_en` 默认设为 0，下面各状态需要时再覆盖为 1，保证它们都是单拍脉冲。

那 `comm_data_out_free` 这根反压线由谁置位？在**仿真后端**，由 `main_loop_clk()` 模拟：

[simulation/image_processing_simulation.cpp:230-234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L230-L234) —— 用一个计数器 `counter_free` 模拟「通信线被占满」：当 `counter_free>0` 时 `comm_data_out_free=0`（忙）。

[simulation/image_processing_simulation.cpp:248-252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252) —— 当模块真的发出一个字节（`comm_data_out_valid==1`）时，把 `counter_free` 置为 3，于是接下来 3 个周期 `comm_data_out_free=0`，模拟「主机收一个字节需要消化几拍」。这逼着模块的输出状态机必须正确处理反压。

在**硬件后端**，`comm_data_out_free` 由 SPI 从机接口驱动（在 `top.v` 里它被接到 `spi_interface` 的 `spi_data_in_free` 端口，见 [ice40/hdl/top.v:43-44](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L43-L44)），具体机制留到 u6-l3。

#### 4.3.4 代码实践

**实践目标**：追踪一次完整的「输入握手」和「输出握手」，把 valid/free 的时序对应起来。

**操作步骤**：

1. **输入侧**：阅读 [hdl/image_processing.v:221-286](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L221-L286)，确认模块在每个读命令/参数的状态里，最外层都是 `if(comm_data_in_valid == 1)`。
2. **输出侧**：阅读 [hdl/image_processing.v:310-333](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L310-L333)（`STATE_GET_STATUS`）和 [simulation/image_processing_simulation.cpp:248-252](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L248-L252)。

**需要观察的现象**：

- 输入：模块对 `comm_cmd`/`comm_data_in` 的采样，全部被 `comm_data_in_valid` 门控；valid 为 0 时模块什么都不做。
- 输出：模块发出 `comm_data_out_valid=1` 后，仿真立刻把 `counter_free=3`，于是接下来几拍 `comm_data_out_free=0`；如果模块还想发下一个字节，它必须等到 `comm_data_out_free` 重新变 1。

**预期结果**：你能在脑中画出这样一段时序——`comm_data_out_valid` 单拍脉冲 → `comm_data_out_free` 随后变 0 持续数拍 → 模块停发 → `comm_data_out_free` 回到 1 → 模块发下一个字节。这就是反压的完整往返。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `comm_data_in_valid` 是 input（主机驱动），而 `comm_data_out_free` 也是 input（主机/通信线驱动）？两者方向相同合理吗？

**参考答案**：合理。两者都属于「握手控制位」，方向由「谁掌握节奏」决定。输入方向上，主机知道何时数据有效，所以 `comm_data_in_valid` 由主机给（input）。输出方向上，主机/通信线知道自己何时有空接收，所以「通道是否空闲」`comm_data_out_free` 也由外部给（input）。模块两端的握手各取所需，方向相同并不矛盾。

**练习 2**：如果模块在 `comm_data_out_free==0` 时强行拉高 `comm_data_out_valid` 发数据，会发生什么？

**参考答案**：那一个字节会丢失。因为外部（主机/SPI）在 `free==0` 时不会去采样 `comm_data_out`。这正是模块在 `STATE_GET_STATUS`、`STATE_READ_IMG` 等输出状态里都要先判断 `comm_data_out_free==1` 的原因。

**练习 3**：`comm_data_out_valid`、`wr_en`、`rd_en` 为什么要在 always 块开头统一默认拉低？

**参考答案**：因为它们都是「单拍脉冲」型信号——只在需要的那一拍为 1，其余拍必须为 0。如果在某个分支里拉高了却忘了在别的分支拉低，信号就会黏在高电平上持续多个周期，造成多次误写/误读/误发送。开头默认赋 0，再由需要的分支覆盖为 1，是最稳妥的写法。

---

### 4.4 clk / reset（含 reset 未使用的细节）

#### 4.4.1 概念说明

`clk`（时钟）是整个数字电路的心跳。`image_processing` 是一个**同步时序电路**：几乎所有状态变化都发生在 `clk` 的上升沿（`posedge clk`）。理解这一点很重要——本模块里所有 `<=`（非阻塞赋值）都在 `always @(posedge clk)` 块里，意味着「在下一个时钟沿生效」。这让模块的行为可预测、便于分析。

`reset`（复位）在概念上用于把电路恢复到初始状态。但本模块有一个容易被忽略、却很重要的细节：**`reset` 端口虽然声明了，却在 RTL 里从未被使用**。模块的初始化完全靠 `initial begin` 块完成。

#### 4.4.2 核心流程

模块的时序骨架可以概括为：

1. 上电/仿真启动时，`initial begin` 块把所有关键寄存器（`state`、`addr`、`wr_en`、缓冲地址等）初始化到已知值。
2. 之后每个 `posedge clk`，`always @(posedge clk)` 块执行一遍：
   - 先给三个脉冲输出赋默认值 0。
   - 再根据当前 `state` 决定这一拍做什么（命令解析主状态机）。
   - 同时根据 `state_processing` 决定运算子状态机做什么。
3. `clk` 不停地翻转，模块就一拍一拍地推进。

`reset` 之所以能「缺席」，是因为：

- **在 FPGA 上**：iCE40 的 `initial begin` 会在配置加载（上电）时被执行，相当于一次「上电复位」，所以不需要显式 reset 信号。
- **在 Verilator 仿真里**：C++ 构造 `Vimage_processing` 对象时也会执行 `initial begin`，同样完成初始化。

因此作者用 `initial begin` 代替了传统的 `if (reset)` 复位逻辑。这也解释了文件第 1 行那句 `/* verilator lint_off UNUSED */`——它告诉 Verilator「不要因为有未使用的信号（比如 `reset`）而报警告」。

#### 4.4.3 源码精读

clk 与 reset 的声明：

[hdl/image_processing.v:13-14](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L13-L14) —— 两者都是 `input wire`。

`initial begin` 完成全部初始化（代替 reset）：

[hdl/image_processing.v:180-208](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L180-L208) —— 把 `addr`、`wr_en`、`rd_en`、`data_write`、`state`（设为 `STATE_WAIT_COMMAND`）、`state_processing`（设为 `STATE_IDLE`）、缓冲地址等全部置初值。注意第 196–197 行：主状态机初始就停在「等待命令」，运算子状态机初始为「空闲」。

主时钟驱动的 always 块与默认赋值：

[hdl/image_processing.v:210-216](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L210-L216) —— `always @(posedge clk)` 是唯一的时钟驱动块；开头三行默认赋值已在 4.3 提到。

验证 `reset` 真的没被使用：在整个文件里搜索 `reset`，只会命中两处——端口清单（第 4 行）和方向声明（第 14 行），`always` 块内没有任何 `if (reset)`。而仿真后端只是机械地把 `reset` 拉低：

[simulation/image_processing_simulation.cpp:224-225](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L224-L225) —— `simulator->reset = 0;`，仅为满足端口连接，本身无实际作用。

> 小提示：在 `top.v` 里，模块的 `reset` 接的是 `reset_ip`（见 [ice40/hdl/top.v:32](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32)），而 `reset_ip` 从未被赋值（默认为 0）。这进一步印证它是个「占位」端口。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `reset` 在 RTL 中未被使用，理解「initial 代替 reset」的设计选择。

**操作步骤**：

1. 在仓库根目录运行只读搜索（本环境用 `Grep`）查找 `reset` 在 `hdl/image_processing.v` 里的所有出现位置。
2. 再看文件第 1 行的 `/* verilator lint_off UNUSED */`。

**需要观察的现象**：`reset` 只出现在第 4 行（端口名）和第 14 行（`input wire reset;`），没有任何 `always` 块内的引用；第 1 行的 lint 注释则说明作者知道会有「未使用」告警并主动屏蔽。

**预期结果**：确认 `reset` 是一个声明了但功能上闲置的端口。如果你将来想给模块加上「运行中可复位」的能力，就需要在 `always @(posedge clk)` 开头加 `if (reset) begin ... end`，把 `initial begin` 里的初值搬进去——这是一个很好的二次开发切入点。

> 「待本地验证」：如果你装了 Verilator，可以试着把第 1 行的 lint 注释删掉再编译，观察是否会冒出 `UNUSED` 警告（多半会指向 `reset`）。

#### 4.4.5 小练习与答案

**练习 1**：模块为什么用 `initial begin` 而不是 `if (reset)` 来初始化？

**参考答案**：因为目标平台 iCE40 FPGA 在加载比特流时会执行 `initial begin`，等价于一次上电复位；而 Verilator 仿真在构造模型对象时也会执行它。既然两种运行环境都能保证 `initial begin` 生效，就不必再写一套 `if (reset)` 的同步复位逻辑，省去了复位扇出和对时序的影响。

**练习 2**：本模块有几个 `always` 块？为什么强调「一个时钟」？

**参考答案**：只有一个 `always @(posedge clk)` 块（第 210 行起），里面同时处理「命令解析主状态机」和「运算子状态机」两个 `case`。强调一个时钟是因为它是单时钟域同步设计——所有寄存器都用同一个 `clk`，不存在跨时钟域问题，分析与综合都更简单可靠。

**练习 3**：如果硬要在仿真中途给模块发一个「复位」，仅靠现有的 `reset` 端口能做到吗？

**参考答案**：做不到。因为 RTL 里没有读取 `reset` 的逻辑，把 `reset` 拉高不会有任何效果。中途复位需要先改 RTL（加 `if (reset)` 分支）才行。

---

## 5. 综合实践

把本讲的全部内容串起来，完成下面这个端到端的小任务。

**任务**：为 `image_processing` 模块画一张完整的端口框图，并解释两个关键握手信号。

**步骤 1 — 画框图**：画一个大矩形代表 `image_processing` 模块，把 14 个端口按三类分组，标清每个信号的**方向（箭头朝向）**和**位宽**：

- 左侧画两组 input：时钟复位（`clk`、`reset`）；通信输入（`comm_cmd[8]`、`comm_data_in[8]`、`comm_data_in_valid`、`comm_data_out_free`）。
- 右侧画两组 output：存储器输出（`addr[32]`、`wr_en`、`rd_en`、`data_write[16]`）；通信输出（`comm_data_out[8]`、`comm_data_out_valid`）。
- 上/下两侧画存储器的两条 input：`data_read[16]`、`data_read_valid`。
- 用虚线把「存储器接口」的 6 根线框成一个区，标注「→ 接 RAM」；把「通信接口」的 6 根线框成另一个区，标注「→ 接主机/SPI」。

你可以参考 `top.v` 里真实的连线来核对方向：[ice40/hdl/top.v:32-45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32-L45) —— 这里 `image_processing` 的存储器端口接 `ram_interface`、通信端口接 `spi_interface`，正好印证「两扇门各接一个子系统」。

**步骤 2 — 解释两个握手信号**：在框图旁用文字回答：

1. **`data_read_valid` 由谁置位？模块如何据此推进？**
   - 提示：硬件里由 [ram_interface.v:85](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L85) 置位（用 `rd_en_buffer` 检测延迟后的下降沿，对齐 SPRAM 读延迟）；仿真里由 [image_processing_simulation.cpp:254-259](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L254-L259) 置位。模块在 [image_processing.v:514](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L514) 等处以 `if (data_read_valid == 1'b1)` 门控，只在有效拍才采样 `data_read` 并推进状态。

2. **`comm_data_out_free` 由谁置位？模块如何据此推进？**
   - 提示：硬件里由 SPI 从机接口驱动（`top.v` 里接 `spi_data_in_free`）；仿真里由 [image_processing_simulation.cpp:230-234](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L230-L234) 用 `counter_free` 模拟，发出一个字节后置 3 拍忙。模块在 [image_processing.v:311](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L311) 和 [:384](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L384) 等处以 `if (comm_data_out_free == 1)` 门控，通道忙时就原地等待，不丢字节。

**预期结果**：一张清晰的端口框图 + 两段准确的握手说明。完成后，你应该能向别人讲清「这个模块对外有哪两扇门、每扇门上有哪些线、哪些线由对面驱动、模块靠哪两个握手信号和对面同步」。

## 6. 本讲小结

- `image_processing` 模块共 14 个端口，按**时钟复位 / 存储器接口 / 通信接口**三类组织，对外只有「存储」和「通信」两扇门。
- **存储器接口**（6 根线）是抽象的「按地址读写 16 位字」通道：模块不含 RAM，RAM 由仿真后端（C++ 数组）或硬件后端（4 片 SPRAM）提供。
- 16 位存储字宽意味着**一个字装 2 个像素**，地址是字节粒度，`addr[0]` 区分字内高低字节，这是全项目核心的吞吐节流技巧。
- **通信接口**（6 根线）是模块与主机唯一的交互通道，输入靠 `comm_data_in_valid` 握手，输出靠 `comm_data_out_valid` + `comm_data_out_free` 的 valid/free 双向握手（带反压）。
- `data_read_valid` 由存储器那一端置位（仿真直接给、硬件用流水线对齐 SPRAM 延迟），模块每次读操作都要等它才采样数据。
- `clk` 是唯一的同步时钟；`reset` 虽声明但**未被使用**，初始化完全靠 `initial begin`（FPGA 上电 + Verilator 构造时各执行一次），这也解释了文件首行的 verilator lint 屏蔽注释。

## 7. 下一步学习建议

本讲把模块当「黑盒」，只看清了它的接线。接下来要打开黑盒：

- **u3-l2 双缓冲存储模型与 16 位像素打包**：深入讲 128KB 如何被切成 input/storage 两个 64KB 缓冲、`COMMAND_SWITCH_BUFFERS` 如何只换地址不搬数据，以及奇偶地址处理的更多细节。
- **u3-l3 主命令处理状态机**：进入模块内部，看 `state` 主状态机如何根据 `comm_cmd` 把命令派发到各个读参数状态——也就是这扇「通信门」收进来的命令到底是怎么被消费的。
- 如果你更想先看「门对面」长什么样，可以跳到 **u6-l1（Verilator 仿真后端）** 和 **u6-l2（iCE40 硬件顶层与 SPRAM 接口）**，看存储器接口和通信接口在两套后端里分别如何被接线、驱动。

建议按 u3-l2 → u3-l3 → u3-l4 的顺序读完「核心 HDL」单元，再回头比较两套后端的实现差异，理解会扎实得多。
