# 主机 SPI 软件：FTDI 与命令封装

## 1. 本讲目标

本讲是「仿真与硬件两条后端」单元的收官篇，聚焦 **iCE40 硬件后端在主机（PC）一侧的软件实现**。

u6-l3 讲了 FPGA 侧的 `spi_interface.v`——它用片上硬核 `SB_SPI` 当从机，把主机发来的 SPI 包翻译成核心模块的 `comm_cmd`/`comm_data_in`。本讲把镜头转回 PC，讲清楚：

1. **分层映射**：`image_processing_ice40.cpp` 如何把每一个高层调用（`send_add`、`send_convolution`……）翻译成一串 SPI 事务。
2. **底层引擎**：`spi_lib.c` 如何通过 FTDI 芯片的 MPSSE 引擎，把 C 函数调用变成 SCK/MOSI/MISO 上的真实时钟边沿。
3. **可靠性机制**：`STATUS_FPGA_RECV_MASK` 重试循环如何在一个「无应答线」的全双工 SPI 上，确认 FPGA 真正收到了数据。
4. **吞吐优化**：`send_image`/`read_image` 为何用 32 字节批量事务摊薄命令开销，剩余字节再回退到逐字节发送。

学完后你应当能：对照 FPGA 与主机两侧源码，画出一次高层调用从 C++ 函数到 SPI 字节、再到 FPGA 状态跳变的完整时序，并解释重试与批量收发的设计动机。

## 2. 前置知识

本讲假设你已掌握：

- **u2-l1** 的抽象基类 `Image_processing` 与 `Commands` 枚举——主机业务逻辑只依赖这个纯虚接口，本讲的 `Image_processing_ice40` 是它的硬件后端实现之一。
- **u6-l3** 的 FPGA 侧 `spi_interface.v`——尤其 `SB_SPI` 寄存器握手、`SPI_TRANSMIT` 发出的状态字节 `0x40 | buffer_full`、以及「主机 SEND↔FPGA RECEIVE」的镜像命名。

下面补充三个本讲特有的背景概念：

**FTDI 芯片与 USB↔SPI 桥**。iCE40 开发板（如 iCE breakout）上有一颗 FTDI（FT2232H/FT232H）USB 转串口芯片。PC 通过 USB 与 FTDI 通信，FTDI 再用它的 GPIO 引脚产生 SPI 时序（SCK/MOSI/MISO/CS）去驱动 FPGA。所以 PC 上「发一个 SPI 字节」实际上是「通过 libftdi 往 USB 写一段命令，由 FTDI 硬件去翻转引脚」。

**MPSSE（Multi-Protocol Synchronous Serial Engine）**。FTDI 芯片内部有一个专用的串行协议引擎。PC 不必逐 bit 控制引脚电平，而是向 FTDI 发送「MPSSE 命令字节」——例如「读入并输出 N 个字节，LSB 优先，负边沿更新」。MPSSE 在硬件里完成移位，把 PC 从精确时序控制中解放出来。本项目的 MPSSE 用法直接取自 IceStorm 项目的 `iceprog`。

**SPI 全双工与「时序错位」**。SPI 是全双工的：主机每发 1 个字节，**同时**收到 1 个字节。但从机（FPGA）处理一个收到的字节、再准备好要回送的字节，需要若干个 `clk` 周期走状态机。于是主机「发第 N 个字节时收到的回字节」，其实是 FPGA 对「更早的字节」的延迟响应。这种错位是本讲重试机制要解决的核心问题——请先记住它，4.4 节会详细展开。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
|------|------|----------|
| [ice40/software/image_processing_ice40.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.hpp) | 类声明 | `Image_processing_ice40` 继承 `Image_processing`，声明全部虚函数的实现 |
| [ice40/software/image_processing_ice40.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp) | 高层→SPI 事务 | 把每个高层调用翻译成 `spi_command_send*` 序列 |
| [ice40/software/spi_lib/spi_lib.h](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h) | 协议常量与原型 | MPSSE 命令枚举、`STATUS_FPGA_RECV_MASK`、引脚映射表 |
| [ice40/software/spi_lib/spi_lib.c](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c) | SPI 底层 + 事务编排 | FTDI 初始化、`xfer_spi`、`spi_command_send_recv` 重试循环 |
| [ice40/hdl/spi_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v) | FPGA 侧（对照） | u6-l3 已详解；本讲引用它来验证主机侧行为 |

编译入口见 [build_ice40.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh)：一条 `g++ -DICE40 ... -lftdi` 把 `spi_lib.c`、`image_processing_ice40.cpp`、`main.cpp` 链成可执行文件 `soft_ice40`，其中 `-lftdi` 就是链接上面提到的 libftid 库。

---

## 4. 核心概念与源码讲解

### 4.1 从高层调用到 SPI 字节：主机软件的三层架构

#### 4.1.1 概念说明

主机软件是一个清晰的三层栈，每层只认下一层的接口：

```
┌─────────────────────────────────────────────────────┐
│ 上层：main.cpp（业务逻辑，调用 send_add 等）          │  ← 只认 Image_processing 抽象接口
├─────────────────────────────────────────────────────┤
│ 中层：Image_processing_ice40（image_processing_ice40.cpp）│  ← 高层调用 → 一串 SPI 事务
├─────────────────────────────────────────────────────┤
│ 底层：spi_lib.c  (spi_command_send_recv / xfer_spi)  │  ← SPI 事务 → FTDI MPSSE 字节
├─────────────────────────────────────────────────────┤
│ 硬件：FTDI 芯片 + USB                                │  ← MPSSE 字节 → SCK/MOSI/MISO 电平
└─────────────────────────────────────────────────────┘
```

关键设计：**高层调用的语义和仿真后端完全一致**（都是 `Image_processing` 的虚函数），差别只在中层——仿真后端把高层调用排进内存 FIFO 队列（见 u6-l1），而 iCE40 后端把它翻译成一串真实的 SPI 事务发到线上。剥去传输外壳，两者送进核心模块的命令/数据字节流逐字节相同（这条结论在 u2-l2 已建立，本讲看它如何落到 SPI）。

#### 4.1.2 核心流程

以 `send_add(value, clamp)` 为例，一次高层调用穿过三层的过程：

```
send_add(value, clamp)                         [中层 image_processing_ice40.cpp]
  │  把 value 拆成小端 2 字节，clamp 当 1 字节
  ├─ spi_command_send(SPI_SEND_CMD, COMMAND_APPLY_ADD)   ─┐
  ├─ spi_command_send(SPI_SEND_DATA, value_low)           │ 4 个 SPI 事务
  ├─ spi_command_send(SPI_SEND_DATA, value_high)          │
  └─ spi_command_send(SPI_SEND_DATA, clamp)              ─┘
        │  每个 spi_command_send 组装 4 字节帧 {cmd, p0, p1, p2}
        └─ spi_command_send_recv(...)                     [底层 spi_lib.c]
              └─ xfer_spi(to_send, 4)   全双工交换 4 字节，带重试
                    └─ send_byte(MC_DATA_IN|...)          [FTDI MPSSE]
```

注意：**高层的一个函数 = 多个 SPI 事务**。事务数 = 1（命令字）+ 参数字节数。`send_add` 有 3 个参数字节，所以是 4 个事务；`send_convolution` 有 1 个标志字节 + 9 个核系数 = 10 个事务。

#### 4.1.3 源码精读

`send_add` 是最典型的高层→SPI 映射，看它如何把参数拆字节并逐个发送：

[ice40/software/image_processing_ice40.cpp:90-97](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L90-L97) —— 把 `int16_t` 拆成小端 2 字节，先发命令字 `COMMAND_APPLY_ADD`，再依次发 3 个数据字节：

```cpp
void Image_processing_ice40::send_add(int16_t value, bool clamp){
   uint8_t add_value8[2] = {value&0xFF, (value>>8)&0xFF};   // 小端拆字节

   spi_command_send(SPI_SEND_CMD, COMMAND_APPLY_ADD);       // 事务1: 发命令字
   spi_command_send(SPI_SEND_DATA, add_value8[0]);          // 事务2: 低字节
   spi_command_send(SPI_SEND_DATA, add_value8[1]);          // 事务3: 高字节
   spi_command_send(SPI_SEND_DATA, clamp);                  // 事务4: clamp 标志
}
```

这与 u2-l2 讲的报文格式完全吻合：**1 字节操作码 + N 字节参数**，参数用小端 16 位。差别只在仿真后端把这 4 步压进 FIFO 队列、iCE40 后端把这 4 步变成 4 次 SPI 事务。

更复杂的例子 `send_convolution`，9 个核系数外加 1 个位打包的标志字节，串成 10 个事务：

[ice40/software/image_processing_ice40.cpp:200-207](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L200-L207) —— 位打包 `(add_to_output<<2)+(input_source<<1)+clamp` 与 u2-l2 完全一致：

```cpp
void Image_processing_ice40::send_convolution(uint8_t *kernel, bool clamp, bool input_source, bool add_to_output){
   spi_command_send(SPI_SEND_CMD, COMMAND_CONVOLUTION);
   spi_command_send(SPI_SEND_DATA, (add_to_output<<2)+(input_source<<1)+clamp);
   for (size_t i = 0; i < 9; i++) {
      spi_command_send(SPI_SEND_DATA, kernel[i]);
   }
}
```

#### 4.1.4 代码实践

**实践目标**：建立「高层调用 → SPI 事务序列」的直觉。

**操作步骤**：
1. 打开 [image_processing_ice40.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp)。
2. 依次阅读 `send_params`、`send_threshold`、`send_image_invert`、`send_binary_sub`、`switch_buffers`。
3. 对每个函数，数一数它调用了几次 `spi_command_send*`。

**需要观察的现象**：参数越多的命令，事务数越多；无参数命令（如 `send_image_invert`、`switch_buffers`）只有 1 个事务。

**预期结果**（应得到下表）：

| 高层调用 | 事务数 | 组成 |
|----------|--------|------|
| `switch_buffers()` | 1 | 1 个 SEND_CMD |
| `send_image_invert()` | 1 | 1 个 SEND_CMD |
| `send_add(value, clamp)` | 4 | 1 SEND_CMD + 3 SEND_DATA |
| `send_binary_sub(clamp, abs)` | 2 | 1 SEND_CMD + 1 SEND_DATA（两位打包） |
| `send_convolution(9 核, 3 标志)` | 11 | 1 SEND_CMD + 1 SEND_DATA(标志) + 9 SEND_DATA(核) |

`send_image_invert` 只发命令字、不带任何参数，验证了 u3-l3 里「取反是零参数运算、派发时直接启动」的说法。

#### 4.1.5 小练习与答案

**练习 1**：`send_threshold` 有 3 个参数，但它和 `send_add`（也 3 个参数）的事务数相同吗？

**参考答案**：相同，都是 4 个事务（1 命令 + 3 数据）。`send_threshold` 的 3 个参数 `threshold_value`、`replacement_value`、`upper_selection` 虽类型不同（两个 `uint8_t`、一个 `bool`），但都被当作单字节发送，所以事务结构与 `send_add` 一致。

**练习 2**：为什么 `switch_buffers` 只需 1 个事务，而 `send_add` 需要 4 个？

**参考答案**：`COMMAND_SWITCH_BUFFERS` 在 FPGA 侧是「零参数命令」——它只需互换两个地址寄存器，不需要主机提供额外数据（见 u3-l2）。而 `COMMAND_APPLY_ADD` 需要 `value`（2 字节）和 `clamp`（1 字节）共 3 个参数字节，故除命令字外还要 3 个数据事务。

---

### 4.2 SPI 命令操作码与镜像命名

#### 4.2.1 概念说明

本项目的命令分**两层**，务必区分：

- **`Commands` 枚举（核心模块命令）**：如 `COMMAND_APPLY_ADD`、`COMMAND_SEND_IMG`，这是主机与 `image_processing` 核心模块之间的业务语义，u2-l1 已讲。
- **SPI 命令操作码（传输层命令）**：如 `SPI_SEND_CMD`、`SPI_SEND_DATA`，这是主机与 `spi_interface`（通信接口）之间的「怎么搬数据」指令，本讲的主题。

两者关系：SPI 命令是「外壳」，核心命令是「内容」。主机先用一个 `SPI_SEND_CMD` 事务把某个 `COMMAND_*` 当作数据字节送进去，FPGA 的 `spi_interface` 收到后，把它原样转发给核心模块的 `comm_cmd` 端口。README 也明确强调：「The SPI commands are different from the image processing commands.」

#### 4.2.2 核心流程

主机与 FPGA 对同一批操作码数值采用**镜像命名**：同一个数值，从主机视角叫 `SEND_*`（我发出去了），从 FPGA 视角叫 `RECEIVE_*`（我收到了）；反之主机 `READ_*` 对应 FPGA `SEND_*`。数值本身两侧完全一致，所以两边能对上号。

#### 4.2.3 源码精读

主机侧 7 个 SPI 操作码定义在 [image_processing_ice40.cpp:9-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L9-L15)：

```cpp
#define SPI_NOP 0x00
#define SPI_INIT 0x01
#define SPI_READ_DATA 0x02
#define SPI_SEND_CMD 0x03
#define SPI_SEND_DATA 0x04
#define SPI_READ_DATA32 0x05
#define SPI_SEND_DATA32 0x06
```

FPGA 侧对应定义在 [ice40/hdl/spi_interface.v:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L6)：

```verilog
parameter NOP=0, INIT=1, SEND_DATA=2, RECEIVE_CMD=3, RECEIVE_DATA=4, SEND_DATA32=5, RECEIVE_DATA32=6;
```

按**数值**对齐，镜像关系一目了然：

| 数值 | 主机视角（image_processing_ice40.cpp） | FPGA 视角（spi_interface.v） | 语义 |
|------|----------------------------------------|------------------------------|------|
| 0 | `SPI_NOP` | `NOP` | 空操作 |
| 1 | `SPI_INIT` | `INIT` | 初始化握手（body 必须是 `{0,0,0x11}`） |
| 2 | `SPI_READ_DATA` | `SEND_DATA` | 主机读 1 字节 ↔ FPGA 送 1 字节 |
| 3 | `SPI_SEND_CMD` | `RECEIVE_CMD` | 主机送 1 个 `COMMAND_*` ↔ FPGA 收命令字 |
| 4 | `SPI_SEND_DATA` | `RECEIVE_DATA` | 主机送 1 数据字节 ↔ FPGA 收数据字节 |
| 5 | `SPI_READ_DATA32` | `SEND_DATA32` | 主机读 32 字节 ↔ FPGA 送 32 字节 |
| 6 | `SPI_SEND_DATA32` | `RECEIVE_DATA32` | 主机送 32 字节 ↔ FPGA 收 32 字节 |

记忆口诀：**主机 READ ↔ FPGA SEND；主机 SEND ↔ FPGA RECEIVE**。

`SPI_INIT` 是个特例：它是上电后主机发的第一个事务，body 固定为 `{0,0,0x11}`。FPGA 侧 `SPI_READ_INIT` 状态一旦读到字节 `0x11`，就把 `is_spi_init` 置 1（[spi_interface.v:215-219](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L215-L219)），此后的收发才按正常命令流程走。主机在 `send_params` 开头发起这个初始化：

[ice40/software/image_processing_ice40.cpp:29-33](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L29-L33) —— 注意 body 的第三个字节正是 `0x11`：

```cpp
uint8_t init_param[3] = {0x0, 0x0, 0x11};
if (spi_command_send(SPI_INIT, init_param) != 0){
   printf("trouble to get answer\n");
}
```

> 说明：README 的 SPI 命令清单里把 `READ_DATA32`/`SEND_DATA32` 的中英文描述写反了（称 READ 是「send 32bytes」）。本讲以两侧源码的实际行为为准，请以上表为准。

#### 4.2.4 代码实践

**实践目标**：亲手验证主机与 FPGA 操作码的镜像对应关系。

**操作步骤**：
1. 同时打开主机侧 [image_processing_ice40.cpp:9-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L9-L15) 与 FPGA 侧 [spi_interface.v:6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L6)。
2. 逐个数值核对：主机的 `SPI_SEND_CMD=0x03` 是否等于 FPGA 的 `RECEIVE_CMD=3`。
3. 在 FPGA 侧找到 `SPI_READ_OPCODE` 状态里对 `RECEIVE_CMD`/`RECEIVE_DATA` 的处理（[spi_interface.v:246-254](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L246-L254)）。

**需要观察的现象**：FPGA 收到 `RECEIVE_CMD` 后，在 `counter_read==1` 时把第二个字节送到 `spi_cmd`（即核心模块的 `comm_cmd`）；收到 `RECEIVE_DATA` 时则送到 `spi_data_out`（即 `comm_data_in`）。

**预期结果**：主机用 `SPI_SEND_CMD` 送出去的 `COMMAND_APPLY_ADD`，最终出现在核心模块的 `comm_cmd` 端口；用 `SPI_SEND_DATA` 送出去的参数字节，出现在 `comm_data_in` 端口。两层命令各司其职。

#### 4.2.5 小练习与答案

**练习 1**：主机想从 FPGA 读回一张图像的像素，应该用哪个 SPI 命令？对应的 FPGA 操作码叫什么？

**参考答案**：批量读用 `SPI_READ_DATA32`（数值 5）。FPGA 侧同一个数值叫 `SEND_DATA32`——因为对 FPGA 而言是「送出 32 字节」。这正是镜像命名的体现。

**练习 2**：为什么 `SPI_INIT` 的 body 第三字节必须是 `0x11`？换个值会怎样？

**参考答案**：FPGA 的 `SPI_READ_INIT` 状态用 `if(spi_dato == 8'h11)` 作为「初始化完成」的判据（[spi_interface.v:215](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L215)）。只有读到 `0x11` 才会把 `is_spi_init` 置 1，FPGA 才会进入正常的命令收发流程。换成其他值，`is_spi_init` 永远为 0，后续命令都不会被处理——这是主机与 FPGA 之间的一个简单「密码握手」。

---

### 4.3 FTDI MPSSE 底层引擎：xfer_spi / send_spi / read_spi / set_gpio

#### 4.3.1 概念说明

最底层的问题是：PC 上的 C 代码如何让导线上出现 SPI 时序？答案是不直接控制——而是通过 libftdi 向 FTDI 芯片发送 **MPSSE 命令字节**，由 FTDI 内部的硬件引擎去移位。

MPSSE 的核心思想：一条 MPSSE 命令 = 「在 SCK 上产生 N 个时钟沿，同时从 MOSI 输出数据、从 MISO 采样数据，按指定边沿和位序」。这样 PC 只需准备「命令字节 + 长度 + 数据」，剩下的精确时序由 FTDI 硬件保证。

`spi_lib.c` 提供了三个语义层级的原语：

- `xfer_spi(data, n)`：**全双工**，同时发 `n` 字节、收 `n` 字节，收到的覆盖回 `data[]`。
- `send_spi(data, n)`：**只发**，不回收（仍占用时钟）。
- `read_spi(data, n)`：**只收**，发出的是任意电平。

本项目绝大多数事务用 `xfer_spi`（全双工），因为重试机制需要同时读回 FPGA 的状态字节。

#### 4.3.2 核心流程

一次 `xfer_spi(buf, 4)` 的内部流程：

```
xfer_spi(buf, 4)
  ├─ send_byte(MC_DATA_IN | MC_DATA_OUT | MC_DATA_LSB | MC_DATA_OCN)   // MPSSE 命令: 全双工、LSB、负边沿更新
  ├─ send_byte(4 - 1)                                                   // 长度低字节 (=3 表示 4 字节)
  ├─ send_byte((4 - 1) >> 8)                                            // 长度高字节 (=0)
  ├─ ftdi_write_data(buf, 4)                                            // 把 4 字节通过 USB 送给 FTDI 输出
  └─ for i in 0..3: buf[i] = recv_byte()                                // 收回 4 字节（FPGA 的回送）
```

其中 `send_byte`/`recv_byte` 是对 `ftdi_write_data`/`ftdi_read_data` 的单字节封装（[spi_lib.c:11-34](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L11-L34)）。MPSSE 命令字节的各位含义在头文件里定义。

#### 4.3.3 源码精读

MPSSE 命令的「数据相位」控制位定义在 [spi_lib.h:57-63](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L57-L63)：

```c
#define MC_DATA_IN   (0x20) /* When set read data (Data IN) */
#define MC_DATA_OUT  (0x10) /* When set write data (Data OUT) */
#define MC_DATA_LSB  (0x08) /* When set input/output data LSB first. */
#define MC_DATA_ICN  (0x04) /* When set receive data on negative clock edge */
#define MC_DATA_BITS (0x02) /* When set count bits not bytes */
#define MC_DATA_OCN  (0x01) /* When set update data on negative clock edge */
```

于是 `xfer_spi` 拼出的命令字节 `MC_DATA_IN | MC_DATA_OUT | MC_DATA_LSB | MC_DATA_OCN` = `0x20|0x10|0x08|0x01` = `0x39`，含义是「读入 + 输出 + LSB 优先 + 负边沿更新数据」。对照 [spi_lib.c:49-67](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L49-L67)：

```c
static void xfer_spi(uint8_t *data, int n)
{
  if (n < 1) return;
  send_byte(MC_DATA_IN | MC_DATA_OUT | MC_DATA_LSB | MC_DATA_OCN);  // 命令字节 0x39
  send_byte(n - 1);                                                  // 长度低字节
  send_byte((n - 1) >> 8);                                           // 长度高字节
  int rc = ftdi_write_data(&ftdic, data, n);                         // USB 送出 n 字节
  ...
  for (int i = 0; i < n; i++) data[i] = recv_byte();                 // 收回 n 字节，覆盖原缓冲
}
```

引脚映射（哪些 FTDI 引脚当 SCK/MOSI/MISO/CS）记录在 [spi_lib.h:10-21](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L10-L21)：

| FTDI 引脚 | 信号 | 控制方式 |
|-----------|------|----------|
| xDBUS0 | SCK | MPSSE |
| xDBUS1 | MOSI | MPSSE |
| xDBUS2 | MISO | MPSSE |
| xDBUS4 | CS（片选） | GPIO |
| xDBUS7 | CRESET | GPIO |

SCK/MOSI/MISO 由 MPSSE 引擎直接驱动，而 CS 和 CRESET 是普通 GPIO，靠 `set_gpio` 用另一条 MPSSE 命令 `MC_SETB_LOW` 单独控制（[spi_lib.c:107-124](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L107-L124)）。访问 FPGA 的 SRAM 模式前要拉低 CS（`sram_chip_select` → `set_gpio(0,1)`），这等价于 iceprog 里选中 FPGA 的操作。

整个 FTDI 的初始化在 `spi_init()`（[spi_lib.c:148-219](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L148-L219)），关键步骤：打开 USB 设备（vendor `0x0403` 即 FTDI）→ 设 latency timer → 切到 MPSSE 模式（`ftdi_set_bitmode(..., BITMODE_MPSSE)`）→ 设时钟分频 → 拉低 CS 选中 FPGA。

MPSSE 时钟由两段分频得到：先由 `MC_TCK_D5` 把 60 MHz 主时钟 5 分频，再用 `MC_SET_CLK_DIV` 进一步分频。MPSSE 的 SCK 频率满足：

\[
f_{SCK} = \dfrac{f_{clk}}{2\,(1+D)}
\]

其中 \(D\) 是写入 `MC_SET_CLK_DIV` 的分频寄存器值（两字节小端），\(f_{clk}\) 在使能 `MC_TCK_D5` 后为 \(60/5=12\) MHz。代码写入的分频字节为 `0x00, 0x01`（即 \(D=256\)），目的是把 SCK 压到足够低，让 iCE40 的 `SB_SPI` 从机能稳定跟上（代码注释自述为 MHz 量级，精确值待本地用逻辑分析仪确认）。

#### 4.3.4 代码实践

**实践目标**：理解「C 函数 → MPSSE 命令字节 → FTDI 硬件」的链条。

**操作步骤**：
1. 读 [spi_lib.c:49-67](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L49-L67) 的 `xfer_spi`。
2. 对照 [spi_lib.h:57-63](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L57-L63) 逐位解释命令字节 `0x39`。
3. 比较 `send_spi`（[spi_lib.c:69-84](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L69-L84)）的命令字节少了 `MC_DATA_IN`，因此不回收数据。

**需要观察的现象**：`xfer_spi` 拼命令时同时置 `MC_DATA_IN|MC_DATA_OUT`（既读又写）；`send_spi` 只置 `MC_DATA_OUT`（只写）；`read_spi` 只置 `MC_DATA_IN`（只读）。

**预期结果**：三个原语的命令字节分别是 `0x39`、`0x19`、`0x28`（`read_spi` 注释掉了 `MC_DATA_OCN`）。本项目只在 `spi_command_send_recv*` 里用 `xfer_spi`，因为重试机制必须读回 FPGA 的状态字节。

> 待本地验证：在没有 FTDI 硬件的环境下，以上命令字节可对照 FTDI MPSSE 协议手册核实；运行行为需要真实 iCE40 开发板。

#### 4.3.5 小练习与答案

**练习 1**：`xfer_spi(buf, 4)` 里 `send_byte(n - 1)` 为什么是 `n-1` 而不是 `n`？

**参考答案**：MPSSE 的长度字段是「字节数 − 1」编码（即写入 0 表示传 1 字节）。这是 FTDI MPSSE 协议的约定，`send_byte(n-1)` 让硬件产生正好 n 个时钟字节。若误写成 n，会多传一个字节，导致整条事务错位。

**练习 2**：为什么 CS（片选）不走 MPSSE 的数据相位，而要用 `set_gpio` 单独控制？

**参考答案**：CS 需要在一次 SPI 事务的**整个持续时间**保持有效（低电平），而 MPSSE 数据相位只在传输瞬间驱动 SCK/MOSI/MISO。CS 是电平信号、不是时钟流数据，所以归到 GPIO，用 `MC_SETB_LOW` 在事务前拉低、事务后拉高。这与 iceprog 选中 FLASH/FPGA SRAM 的做法一致。

---

### 4.4 重试确认：STATUS_FPGA_RECV_MASK 如何保证 FPGA 真正收到数据

#### 4.4.1 概念说明

这是本讲最精妙（也最容易被忽略）的设计。问题背景：

SPI 全双工意味着主机发字节的**同时**就在收字节，但 FPGA 处理一个收到的字节要走状态机、花好几个 `clk` 周期。于是主机在「发完命令字、刚开始发参数」时收到的回字节，大概率是**陈旧的、无意义的**——FPGA 还没来得及响应。主机如何确认「我发出去的东西 FPGA 真的收到了」？

本项目的解法：**让 FPGA 在它真正「说话」时，永远在一个固定位置（bit6）置 1，当作「我已就绪、这是有效响应」的信号灯**。主机收到这一位为 1，才相信本次事务被 FPGA 处理了；否则原样重发整个事务。

这个固定信号灯就是 `STATUS_FPGA_RECV_MASK`（bit6 = `0x40`）。它不是 FPGA 显式回传的「ACK」，而是利用 FPGA 状态字节里的一个**常量位**充当存在性证明。

#### 4.4.2 核心流程

`spi_command_send_recv` 的重试循环（[spi_lib.c:243-261](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L243-L261)）：

```
组装 4 字节帧 to_send = {cmd, p0, p1, p2}
retries = 0
do:
    xfer_spi(to_send, 4)              // 全双工交换 4 字节，回字节覆盖回 to_send
    retries++
while retries < 10 AND (to_send[2] & 0x40) == 0
                                     // 第 3 个回字节的 bit6 必须为 1，否则重发
拷贝 to_send[2..3] 到 recv_data       // to_send[2]=状态字节, to_send[3]=数据字节
```

为什么是第 3 个字节（`to_send[2]`）？因为前两个回字节是 FPGA 尚未就绪时的陈旧填充，到第 3 个字节时 FPGA 通常已走到 `SPI_TRANSMIT` 状态、发出了真正的状态字节（u6-l3 讲过这个时序错位）。

#### 4.4.3 源码精读

主机侧重试循环：

[ice40/software/spi_lib/spi_lib.c:243-261](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L243-L261) —— 注意循环条件检查的是 `to_send[2] & STATUS_FPGA_RECV_MASK`：

```c
int spi_command_send_recv(uint8_t cmd, uint8_t send_param[3], uint8_t recv_data[2])
{
   uint8_t to_send[] = {cmd, send_param[0], send_param[1], send_param[2]};
   uint retries = 0;
   uint max_retries = 10;

   do{
      to_send[0] = cmd;
      memcpy(to_send+1, send_param, 7);
      xfer_spi(to_send, 4);                                      // 全双工交换 4 字节
      retries++;
   } while(retries < max_retries && (to_send[2] & STATUS_FPGA_RECV_MASK) == 0);

   memcpy(recv_data, to_send+2, 2);                              // to_send[2]=状态, to_send[3]=数据
   return retries >= max_retries;
}
```

掩码定义在 [spi_lib.h:65-69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L65-L69) —— bit6 是「FPGA 已收到」，bit7 预留给「FPGA 已发送」：

```c
#define STATUS_FPGA_RECV_OFFSET 6 //fpga has received data
#define STATUS_FPGA_SEND_OFFSET 7 //fpga has sent data
#define STATUS_FPGA_RECV_MASK (0x1<<STATUS_FPGA_RECV_OFFSET) // = 0x40
```

FPGA 侧的「信号灯」就在 `SPI_TRANSMIT` 状态里——当 `counter_send==0`（即发本次事务的第一个回字节）时，状态字节被硬编码为 `0x40`，bit0 再叠加 `buffer_full`：

[ice40/hdl/spi_interface.v:173-178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L173-L178)：

```verilog
SPI_TRANSMIT: begin
   spi_adr <= SPI_ADDR_SPITXDR;
   if(counter_send == 0) begin //status
      spi_dati <= 8'b01000000;          // bit6 恒为 1（0x40）——这就是 RECV 掩码的来源
      spi_dati[0] <= buffer_full;       // bit0: 是否有有效数据要回送
   end
   ...
```

**两侧的耦合点**：FPGA 只要走到了 `SPI_TRANSMIT`、发出了第一个回字节，这个字节的 bit6 必然是 1。主机在第 3 个回字节位置看到 bit6=1，就反向推断「FPGA 已经处理完我发的内容、进入了发送态」——即数据被真正接收了。若 bit6=0，说明收到的还是陈旧填充，FPGA 尚未跟上，主机重发整帧。

这解释了 `STATUS_FPGA_RECV_MASK`（bit6）对应 FPGA 侧的什么：**就是 `SPI_TRANSMIT` 状态在 `counter_send==0` 时发出的状态字节里那个恒定的 bit6（`8'b01000000`）**。它不是单独的 ACK 信号线，而是一个「FPGA 已就绪并正在有效发送」的隐式信标。

顺带看清状态字节的两层结构，后续 `read_status` 会用到：
- **bit6**：恒为 1 的「FPGA 已响应」信标（主机据此重试，本节主题）。
- **bit0**：`buffer_full`，表示「本次回送是否带有效数据」（`read_status`/`read_image` 据此判断 `recv_data[1]` 是否可取）。

#### 4.4.4 代码实践

**实践目标**：彻底搞清主机重试检查的 bit6 对应 FPGA 侧的哪一行、什么状态。

**操作步骤**：
1. 在主机侧找到掩码定义 [spi_lib.h:65-69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.h#L65-L69)，确认 `STATUS_FPGA_RECV_MASK = 0x40`（bit6）。
2. 找到重试循环 [spi_lib.c:254](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L254)，确认它检查的是第 3 个回字节 `to_send[2]` 的 bit6。
3. 在 FPGA 侧找到 `SPI_TRANSMIT` 里 `counter_send==0` 的赋值 [spi_interface.v:175-178](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L175-L178)，确认状态字节 = `8'b01000000 | buffer_full`。

**需要观察的现象**：主机侧掩码位（bit6）与 FPGA 状态字节里的常量位（`01000000` 的 bit6）数值完全吻合——这不是巧合，而是两侧约定的契约。

**预期结果**：你能用自己的话讲清这条链路——「主机发 4 字节 → FPGA 在第 3 字节时机走到 SPI_TRANSMIT → 回送 `0x40 | buffer_full` → 主机在第 3 回字节看到 bit6=1 → 确认 FPGA 收到 → 停止重试」。若 FPGA 因任何原因没走到发送态，bit6 始终为 0，主机最多重试 10 次。

> 待本地验证：要在真实硬件上观察重试次数，可在 `spi_command_send_recv` 的 `retries++` 后加一行 `printf("retry %u, status=0x%x\n", retries, to_send[2]);`（这属于教学性修改，验证后请还原，不要提交）。预期大多数事务 retries=1 就成功，仅在 FPGA 忙时出现重试。

#### 4.4.5 小练习与答案

**练习 1**：如果 FPGA 的 `SPI_TRANSMIT` 把 `8'b01000000` 改成 `8'b00000000`（bit6 不再恒为 1），主机会有什么表现？

**参考答案**：主机的 `to_send[2] & 0x40` 将永远为 0，重试循环会一直跑到 `max_retries=10` 才退出，每个事务都要重发 10 次，吞吐暴跌、且 `spi_command_send_recv` 返回非 0（表示超时）。这说明 bit6 是两侧必须同步维护的契约，改一侧不改另一侧会直接坏掉。

**练习 2**：为什么主机检查的是 `to_send[2]`（第 3 个字节）而不是 `to_send[0]`（第 1 个）？

**参考答案**：因为全双工时序错位——主机发第 1 字节时，FPGA 还没来得及处理并准备回送，回送的只能是陈旧内容；FPGA 通常要等到主机发到第 3 字节左右，才走完状态机进入 `SPI_TRANSMIT`、把带 bit6 的状态字节送上 MISO。所以有效状态出现在第 3 个回字节位置，代码注释也写明「first 2 bytes are garbage, the third one is the status」。

---

### 4.5 32 字节批量收发：send_image 与 read_image

#### 4.5.1 概念说明

图像数据量大（一幅 256×256 灰度图 = 65536 字节）。若像 `send_add` 那样每字节都用一个独立 4 字节事务，开销巨大——每个事务都要走一遍「组装帧→全双工交换→重试确认」，而真正有效的数据只有 1 字节，协议开销占绝大多数。

为此本项目提供了 32 字节的批量事务 `SPI_SEND_DATA32`/`SPI_READ_DATA32`：一次 33 字节的 SPI 交换（1 字节操作码 + 32 字节数据），就能搬 32 字节有效载荷。`send_image` 和 `read_image` 正是用它来逼近 SPI 的吞吐上限。

#### 4.5.2 核心流程

`send_image` 的策略是「能整除的部分用批量，零头用逐字节」：

```
send_image(image):
    发命令字 COMMAND_SEND_IMG
    image_size = width * height
    # 批量段：每 32 字节一个 SPI_SEND_DATA32 事务
    for i in 0, 32, 64, ... while i < image_size:
        spi_command_send_32B(SPI_SEND_DATA32, image[i..i+31])
    # 零头段：剩余 image_size % 32 字节，逐字节发
    for j in 0 .. (image_size % 32 - 1):
        spi_command_send(SPI_SEND_DATA, image[i+j])
```

`read_image` 方向相反，用 `SPI_READ_DATA32` 批量读回（每次 33 字节交换里，第 1 字节是状态、后 31 字节是数据）。

#### 4.5.3 源码精读

[ice40/software/image_processing_ice40.cpp:69-88](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L69-L88) —— 批量段 + 零头段：

```cpp
void Image_processing_ice40::send_image(uint8_t *image){
   spi_command_send(SPI_SEND_CMD, COMMAND_SEND_IMG);

   uint image_size = image_width*image_height;

   uint8_t data_to_send[32];
   size_t i = 0;
   for (i = 0; i < image_size; i+=32) {                 // 批量段：32 字节/包
      memcpy(data_to_send, image+i, 32);
      spi_command_send_32B(SPI_SEND_DATA32, data_to_send);
   }

   for (size_t j = 0; j < (image_size%32); j++) {        // 零头段：逐字节
      spi_command_send(SPI_SEND_DATA, image[i+j]);
   }
}
```

为什么用 32 字节批量、零头回退逐字节？原因有二：

1. **摊薄协议开销**。批量事务每 33 字节交换搬运 32 字节有效数据，有效率 \(32/33 \approx 97\%\)；而逐字节事务每 4 字节交换只搬 1 字节，有效率 \(1/4 = 25\%\)。整图搬运用批量可快近 4 倍。README 也说明：「sending and reading images, where the spi packets are bigger to accelerate throughput」。
2. **零头只能逐字节**。批量事务固定 32 字节粒度（FPGA 侧 `RECEIVE_DATA32` 用 `counter_read==32` 判终止，见 [spi_interface.v:268-273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L268-L273)）。若图像字节数不是 32 的整倍数，最后不足 32 字节的零头无法凑满一个批量包，只能回退到逐字节的 `SPI_SEND_DATA`。

本项目自带的测试图 `image_fruits_8.h` 是 8×8 = 64 像素（见 [image_fruits_8.h:3-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/images/image_fruits_8.h#L3-L4)），正好是 32 的整倍数（2 包），所以走的是纯批量路径，零头段不触发。

批量收发的底层和重试机制与单字节版完全同构，只是帧长度从 4 变 33，回缓冲从 2 变 31，见 [spi_lib.c:264-282](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L264-L282) 的 `spi_command_send_recv_32B`：

```c
int spi_command_send_recv_32B(uint8_t cmd, uint8_t send_param[32], uint8_t recv_data[31])
{
   uint8_t to_send[33];
   ...
   do{
      to_send[0] = cmd;
      memcpy(to_send+1, send_param, 32);
      xfer_spi(to_send, 33);                                       // 33 字节全双工交换
      retries++;
   } while(retries < max_retries && (to_send[2] & STATUS_FPGA_RECV_MASK) == 0);
   memcpy(recv_data, to_send+2, 31);                               // 状态在 to_send[2]，数据在 [3..32]
   return retries >= max_retries;
}
```

`read_image` 用 `SPI_READ_DATA32` 批量读，每次 31 字节有效数据（第 1 字节是状态、bit0=`buffer_full` 表示有效），见 [image_processing_ice40.cpp:142-162](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L142-L162)：

```cpp
while(counter_read < image_width*image_height) {
   spi_command_send_recv_32B(SPI_READ_DATA32, send_data, recv_data);
   if(recv_data[0]&1 == 1){                       // bit0 = buffer_full = 有有效数据
      for (size_t i = 1; i < 31; i++) {           // recv_data[1..31] 是 31 字节像素
         if(counter_read < image_width*image_height){
            image_out[counter_read] = recv_data[i];
            counter_read++;
         }
      }
   }
}
```

这里 `recv_data[0]&1` 检查的是 **SPI 层状态字节的 bit0（`buffer_full`）**，与 4.4 节的 bit6（重试信标）是同一个状态字节的两位：bit6 表示「FPGA 已响应」，bit0 表示「本次回送带有效数据」。两者职责不同，共同保证批量读的正确性。

#### 4.5.4 代码实践

**实践目标**：对照 FPGA 与主机两侧，解释批量发送与零头回退的设计。

**操作步骤**：
1. 读主机侧 [image_processing_ice40.cpp:69-88](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L69-L88) 的 `send_image`。
2. 读 FPGA 侧批量接收的终止条件 [spi_interface.v:268-273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L268-L273)，确认 `RECEIVE_DATA32` 恰好在收满 32 字节后复位计数器。
3. 计算两种方式搬运 64 字节图像的事务开销：纯逐字节 vs 批量。

**需要观察的现象**：批量方式把 64 字节图分成 2 个 33 字节事务；若用逐字节则需要 64 个 4 字节事务。

**预期结果**：

| 方式 | 事务数 | SPI 总交换字节数 | 有效数据字节 |
|------|--------|------------------|--------------|
| 纯逐字节（`SPI_SEND_DATA` × 64） | 64 | 64×4 = 256 | 64 |
| 批量（`SPI_SEND_DATA32` × 2） | 2 | 2×33 = 66 | 64 |

批量方式把 SPI 总流量从 256 字节压到 66 字节。这就是「32 字节批量发送、零头回退逐字节」的根本动机：零头回退是因为批量包固定 32 字节粒度、凑不满时只能逐字节补。

> 待本地验证：上述开销计算基于源码静态分析；在真实硬件上的实际加速比受 FTDI USB 轮询延迟（latency timer 设为 1）影响，需实测确认。

#### 4.5.5 小练习与答案

**练习 1**：假设图像是 70 字节，`send_image` 会发几个批量包、几个零头字节？

**参考答案**：批量段循环 `i = 0, 32, 64`，`i=0` 和 `i=32` 时各发一个批量包（共 2 个，覆盖字节 0–63）；`i=64` 时 64<70 成立，会再发一个批量包（覆盖字节 64–69，但末尾会读到数组末尾之后的内存，属边界情况）。循环结束后 `i=96`，零头段按 `70 % 32 = 6` 再发 6 个逐字节事务。可以看出，当图像不是 32 的整倍数时，批量段最后一片与零头段会存在重叠/越界问题——这是当前实现的已知粗糙处，正因为自带测试图是 64 字节（2×32）才恰好避开了它。

**练习 2**：`read_image` 里为什么用 `i` 从 1 开始、到 31 结束（31 字节），而 `send_image` 的批量包是 32 字节？

**参考答案**：读方向每次 33 字节交换里，第 1 个回字节是 SPI 层状态（`recv_data[0]`），剩下 31 个才是有效像素（`recv_data[1..31]`）。而写方向 `SPI_SEND_DATA32` 的 33 字节里第 1 个是操作码、后 32 个全是数据，所以是 32 字节有效载荷。读比写少 1 字节有效数据，是因为读方向的「状态」与「数据」复用同一条回送通路，状态占了 1 字节位置。

---

## 5. 综合实践

把本讲五条线索串起来，完成一次「端到端字节级追踪」。

**任务**：以 `send_add(100, true)` 为例，完整写出从 C++ 函数调用到 FPGA 状态跳变的全部字节与时序，并验证重试机制。

**步骤**：

1. **高层拆解**。`send_add(100, true)` 会发出 4 个 SPI 事务。先在纸上写出每个事务的 4 字节帧：
   - 事务1 `{SPI_SEND_CMD(0x03), COMMAND_APPLY_ADD(0x04), 0, 0}`
   - 事务2 `{SPI_SEND_DATA(0x04), 100, 0, 0}`（100 = 0x64，低字节）
   - 事务3 `{SPI_SEND_DATA(0x04), 0, 0, 0}`（100 的高字节 = 0）
   - 事务4 `{SPI_SEND_DATA(0x04), 1, 0, 0}`（clamp=true）
   - 提示：`COMMAND_APPLY_ADD` 的数值在 [image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6) 枚举里数一下它的位置确认。

2. **底层重试**。对每个事务，画出 `spi_command_send_recv` 的 `xfer_spi(to_send, 4)` 全双工交换：主机发 4 字节、收回 4 字节；标出第 3 个回字节（`to_send[2]`）必须满足 `& 0x40 != 0` 才停。

3. **FPGA 侧响应**。对照 [spi_interface.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v) 说明：事务1 让 FPGA 走到 `SPI_READ_OPCODE`，在 `counter_read==1` 时把 `0x04` 送上 `spi_cmd`（核心模块 `comm_cmd`）；事务2–4 把字节送上 `spi_data_out`（`comm_data_in`）。

4. **(可选，教学性修改，验证后还原)** 在 [spi_lib.c:253](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/spi_lib/spi_lib.c#L253) 的 `retries++` 后临时加 `printf("[txn] cmd=0x%02x retries=%u status=0x%02x\n", cmd, retries, to_send[2]);`，在真实硬件上运行 `soft_ice40`，观察日志。

**预期结果**：你得到一张完整的「字节级时序表」，把第 1 章（高层调用）、第 4.2 节（SPI 操作码）、第 4.4 节（重试信标）、第 4.5 节（批量思想虽未直接出现，但对比可得逐字节开销）全部串通。若做了步骤 4，日志里应看到每个事务 retries 多为 1，状态字节多为 `0x40` 或 `0x41`（`0x40 | buffer_full`）。

> 待本地验证：步骤 4 需要真实 iCE40 开发板与 FTDI。无硬件时，步骤 1–3 的静态分析已可完成。

---

## 6. 本讲小结

- 主机软件是三层栈：`Image_processing_ice40`（高层→SPI 事务）→ `spi_command_send_recv`（事务编排+重试）→ `xfer_spi`/MPSSE（FTDI 底层）；高层调用语义与仿真后端完全一致，差别只在传输外壳。
- 存在两层命令：**核心命令**（`COMMAND_*`，业务语义）和 **SPI 命令**（`SPI_*`，传输外壳）；两者数值无关，SPI 命令把核心命令当数据字节送进 FPGA。
- 主机与 FPGA 对 SPI 操作码采用**镜像命名**：主机 `SEND_*`/`READ_*` ↔ FPGA `RECEIVE_*`/`SEND_*`，数值一致；`SPI_INIT` 的 body 第三字节 `0x11` 是初始化握手密码。
- FTDI 的 **MPSSE 引擎**把 PC 从精确时序中解放：C 代码只发「命令字节+长度+数据」，由 FTDI 硬件产生 SCK/MOSI/MISO；`xfer_spi` 全双工交换、`send_spi`/`read_spi` 单向。
- **`STATUS_FPGA_RECV_MASK`（bit6）重试机制**解决了全双工时序错位：FPGA 在 `SPI_TRANSMIT` 的状态字节里把 bit6 恒置 1（`0x40`）当「已响应」信标，主机在第 3 个回字节位置看到它才停止重发，最多重试 10 次。
- **32 字节批量事务**（`SPI_SEND_DATA32`/`SPI_READ_DATA32`）把整图搬运的有效率从 25% 提到 ~97%；剩余不足 32 字节的零头因批量包固定粒度而回退到逐字节 `SPI_SEND_DATA`。

---

## 7. 下一步学习建议

- **横向对比两条后端**：回头读 u6-l1 的仿真后端，把 `main_loop_clk` 的 FIFO 队列驱动与本讲的 SPI 事务驱动并排比较，体会「同一份 HDL、两套传输外壳」的分层之美（这也是 u7-l1 端到端通路讲义的主题）。
- **通读 `read_status` 与 `wait_end_busy`**：本讲聚焦发送方向，读方向的 `recv_data[0]` 的 bit0（`buffer_full`）与 `wait_end_busy` 轮询的 image_processing 层 busy 位（核心模块状态字节 bit0）容易混淆，建议画出两层状态字节的位定义对照表。
- **进入综合单元**：本讲是硬件后端的最后一块拼图。接下来 u7-l1 会把仿真与硬件两条端到端通路画在一张图上，u7-l2 则引导你新增一条完整命令（接口层+两套后端+HDL 状态机），届时你会用到本讲学到的「新增高层调用 = 新增一串 SPI 事务」的知识。
