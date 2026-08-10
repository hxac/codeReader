# u2-l2 命令协议与报文格式

## 1. 本讲目标

上一讲（u2-l1）我们看清了 `Image_processing` 这个纯虚基类和 `Commands` 枚举构成了「主机 ↔ 两套后端 ↔ 硬件」四方共守的契约。但那只是**符号级**的映射——「`send_add` 对应 `COMMAND_APPLY_ADD`」。

本讲往下钻一层，回答一个更具体的问题：

> 当主机调用 `send_binary_sub(true, true)` 时，到底有哪几个**字节**被送进了 `image_processing` 模块？仿真后端和硬件后端送出的字节序列是否完全一样？

学完本讲你应该能够：

1. 说清楚一条命令的报文帧结构：「1 字节操作码 + 0 到 N 字节参数」。
2. 手算任意 16 位参数（无符号或带符号）被拆成的小端字节序列。
3. 解释为什么多个布尔参数（`clamp` / `source_input` / `absolute_diff` / `add_to_result`）会被「塞进同一个字节」，并能算出这个打包字节。
4. 说明卷积 3×3 核的 9 个字节如何被**符号扩展**成 16 位有符号数存进硬件。
5. 对照仿真与 iCE40 两套后端，验证它们在 `image_processing` 模块的通信接口（`comm_cmd` / `comm_data_in`）上看到的字节流**逐字节一致**——只有传输通道不同。

## 2. 前置知识

- **字节（byte）与位宽**：1 字节 = 8 位（bit）。本项目的命令操作码、每个参数字节都是 8 位。
- **小端序（little-endian）**：一个多字节数，**低字节排在前面**。例如 16 位数 `0x012C`，小端发送顺序是 `[0x2C, 0x01]`。
- **补码（two's complement）**：带符号整数在计算机里的表示方式。`int16_t` 和 `int8_t` 都用补码；最高位是符号位。关键性质：带符号数与无符号数在**位模式上**完全相同，只是解释不同，所以小端拆字节的方法对两者都一样。
- **握手（handshake）**：每个字节从主机送到模块，都伴随一个 `comm_data_in_valid` 有效信号，模块据此「吃」进这个字节。本讲关注「送了什么字节」，握手时序细节留待 u3 单元。
- **上一讲建立的认知**：`Image_processing` 是纯虚基类；`Commands` 枚举的操作码值与 HDL 的 `parameter` 一一对应；两套后端把同一个高层调用翻译成字节，差别只在发送通道（FIFO 队列 or SPI 总线）。本讲不再重复枚举语义，专注「字节怎么排」。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 操作码枚举 `Commands` 与各高层函数的**签名**（参数个数、类型），决定了每条命令要带几个参数字节。 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端：把高层调用压成 `Operation` 序列，经 `main_loop_clk` 喂给模块。本讲看它**压了哪些字节**。 |
| [ice40/software/image_processing_ice40.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp) | 硬件后端：把同样的字节用 SPI 事务（`SPI_SEND_CMD` / `SPI_SEND_DATA`）发出去。本讲对照它与仿真后端的字节是否一致。 |
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 硬件侧：`STATE_WAIT_COMMAND` 派发命令、各 `*_READ_PARAM` 状态**消费参数字节**、做小端重组与位解包、卷积核符号扩展。是验证「字节含义」的最终裁判。 |
| [README.md](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md) | 项目作者写的命令表，逐条写明每条命令「第 0 字节是什么、第 1 字节是什么」，是报文格式的权威说明。 |

---

## 4. 核心概念与源码讲解

### 4.1 命令操作码与报文帧结构（opcode + 变长参数）

#### 4.1.1 概念说明

`image_processing` 模块对外的通信极其朴素：它不认结构体、不认 JSON，只认**一串字节**。这串字节按固定的「帧」组织：

```
┌──────────┬───────────────────────────────────┐
│ 1 字节    │ 0 ~ N 字节参数                      │
│ 操作码    │ (具体几个，由操作码决定)              │
│ (opcode) │                                   │
└──────────┴───────────────────────────────────┘
```

- **操作码（opcode）**：1 个字节，取值来自 `Commands` 枚举（`COMMAND_PARAM=0` 到 `COMMAND_APPLY_MULT=12`），告诉模块「接下来要干什么」。
- **变长参数**：紧跟操作码之后。不同命令需要的参数字节数不同——`COMMAND_APPLY_INVERT` 需要 0 个，`COMMAND_BINARY_SUB` 需要 1 个，`COMMAND_CONVOLUTION` 需要 10 个。**模块靠操作码预先知道要读几个参数字节。**

为什么这样设计？因为硬件最擅长处理「逐字节流入 + 状态机计数」这种模式，比解析复杂结构体简单得多、面积也小。这与本项目「资源受限 FPGA」的整体定位一致（见 u1-l1）。

#### 4.1.2 核心流程

发送一帧的通用过程：

```text
1. 发送 1 字节操作码（走 comm_cmd 端口）
2. 根据该命令的参数个数 N，依次发送 N 个参数字节（走 comm_data_in 端口）
3. 每个字节都由 comm_data_in_valid 握手确认
4. 模块内部用 counter_read 计数器从 N-1 数到 0，读完最后一个字节即推进状态
```

两套后端在这一层完全等价，只是「字节怎么送到模块」的通道不同：

| 后端 | 操作码如何送 | 参数字节如何送 |
| --- | --- | --- |
| 仿真 | `fifo_in.push(Operation(is_command=true, command=OPCODE))` | `fifo_in.push(Operation(is_command=false, data=b))`，由 `main_loop_clk` 取出后写入 `comm_cmd`/`comm_data_in` |
| iCE40 | `spi_command_send(SPI_SEND_CMD, OPCODE)` | `spi_command_send(SPI_SEND_DATA, b)`，SPI 从机把字节转交给同一个 `comm_cmd`/`comm_data_in` |

关键点：**两套后端送给模块的字节完全相同**，区别只在「字节外面套了什么壳」。仿真后端用 `Operation` 结构 + 一个 `is_command` 布尔标志区分「这是命令还是数据」；iCE40 后端用两种 SPI 事务（`SPI_SEND_CMD` vs `SPI_SEND_DATA`）区分。剥掉外壳，里面的字节流一模一样。

HDL 侧如何「预先知道要读几个参数」？答案在 `STATE_WAIT_COMMAND` 的派发逻辑里：每条命令在派发时都会给计数器 `counter_read` 预装一个值。

#### 4.1.3 源码精读

README 用一句话给出了帧结构的定义：

> a message is composed of a 1B operand and the parameters which can be of variable length (from 0 to n).
>
> —— [README.md:75](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L75)

操作码本身定义在枚举里（上一讲已详述，这里只确认**数值**用于字节计算）：

[software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6) —— `Commands` 枚举，操作码从 `COMMAND_PARAM=0` 连续递增，`COMMAND_NONE=255` 作哨兵。

HDL 侧用 `parameter` 定义了**数值完全相同**的命令常量，保证软硬件双方对「第几个操作码代表什么」理解一致：

[hdl/image_processing.v:63-68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L63-L68) —— 硬件侧 `COMMAND_PARAM = 0 ... COMMAND_APPLY_MULT` 的 parameter 列表，顺序与 hpp 枚举逐字对应。

模块如何预知参数字节数？看派发逻辑里给 `counter_read` 装的值：

[hdl/image_processing.v:245-247](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L245-L247) —— `COMMAND_APPLY_ADD` 派发时 `counter_read <= 2`，即接下来要读 3 个参数字节（加法值的低/高字节 + clamp）。

[hdl/image_processing.v:270-273](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L270-L273) —— `COMMAND_CONVOLUTION` 派发时 `counter_read <= 9`，接下来读 10 个参数字节（1 个打包标志 + 9 个核）。注释「will read 10 params」印证了总数。

[hdl/image_processing.v:274-277](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L274-L277) —— `COMMAND_BINARY_SUB` 派发时 `counter_read <= 0`，只读 1 个参数字节（打包标志）。

> **统一规律**：`counter_read` 被预装为 `(参数字节数 - 1)`，然后从该值数到 0；数到 0 并消费掉最后一个字节的那个周期，状态机就推进到下一阶段。所有命令都遵循这一规律，卷积也不例外（`==9` 那个字节就是它的第 1 个参数字节，只不过语义上是「打包标志」而非「核」）。

两套后端如何「区分操作码与参数」：

[simulation/image_processing_simulation.cpp:235-246](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L235-L246) —— 仿真后端从 FIFO 取出 `Operation`，用 `op.is_command` 决定写入 `comm_cmd` 还是 `comm_data_in`。`COMMAND_NONE` 只是个占位符，**不会被当作操作码发送**（因为 `is_command=false` 时数据走的是 `comm_data_in` 端口）。

[ice40/software/image_processing_ice40.cpp:9-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L9-L15) —— iCE40 后端定义的 SPI 事务操作码，`SPI_SEND_CMD`（送一条 image_processing 命令）与 `SPI_SEND_DATA`（送一个参数字节）正是「操作码 vs 数据」的 SPI 版区分。

#### 4.1.4 代码实践

**实践目标**：确认「操作码的数值」在软件枚举、硬件 parameter、README 三处完全一致。

**操作步骤**：

1. 打开 [software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6)，按声明顺序数出每条命令的数值（`COMMAND_PARAM=0` 起，每 `+1` 一次）。
2. 打开 [hdl/image_processing.v:63-68](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L63-L68)，同样按顺序数。
3. 打开 [README.md:79-93](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L79-L93) 的命令表，对照描述。

**预期结果**：三处对 `COMMAND_CONVOLUTION` 都给出数值 **9**，对 `COMMAND_BINARY_SUB` 都给出数值 **10**。这正是后续手算字节序列时操作码字节取值的依据。

**待本地验证**：如果你想用编译器验证，可以在任意 `.cpp` 里 `#include "image_processing.hpp"` 后 `printf("%d\n", COMMAND_CONVOLUTION);`，应打印 `9`。

#### 4.1.5 小练习与答案

**练习 1**：为什么仿真后端用 `Operation(false, COMMAND_NONE, b)` 表示一个参数字节，这里的 `COMMAND_NONE` 会不会被模块当成操作码 255？

> **答案**：不会。`is_command=false`，所以 `main_loop_clk` 把它走 `comm_data_in` 端口送出，`COMMAND_NONE`（255）只是 `Operation` 结构里 `command` 字段的占位填充，根本不会出现在 `comm_cmd` 上。真正决定「命令 or 数据」的是 `is_command` 标志。

**练习 2**：`COMMAND_SWITCH_BUFFERS` 需要几个参数字节？依据是什么？

> **答案**：0 个。从 [hdl/image_processing.v:253-257](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L257) 可见它收到操作码后直接交换两个地址寄存器并回到 `STATE_WAIT_COMMAND`，没有进入任何 `*_READ_PARAM` 状态，也没有给 `counter_read` 装值。所以这帧只有 1 个字节（操作码本身）。

---

### 4.2 16bit 小端参数编码

#### 4.2.1 概念说明

很多参数本质上是 16 位数：图像宽高是 16 位无符号（最大 65535），`send_add` 的加法值是 16 位**有符号**（可正可负）。但通信线一次只传 1 个字节，所以必须把一个 16 位数拆成 2 个字节发送。

本项目统一采用**小端序**：先发低字节（LSB），再发高字节（MSB）。设一个 16 位无符号数 \(V\)，其高字节 \(H\) 与低字节 \(L\) 满足：

\[
V = H \cdot 2^{8} + L
\]

发送顺序就是 `[L, H]`。

对**带符号**数（`int16_t`）来说，位模式与对应的无符号数完全相同（补码），所以拆分方法不变：`value & 0xFF` 取低 8 位，`(value >> 8) & 0xFF` 取高 8 位。负数的高字节最高位会是 1（符号位），但这不影响字节排列。

硬件侧收到后做**重组**：先到的字节填 `[7:0]`，后到的字节填 `[15:8]`，正好还原成原来的 16 位数。

#### 4.2.2 核心流程

以 `send_params(width, height)` 为例（2 个 16 位数 = 4 个字节）：

```text
拆 width:  L_w = width & 0xFF;       H_w = (width>>8) & 0xFF
拆 height: L_h = height & 0xFF;      H_h = (height>>8) & 0xFF

帧 = [COMMAND_PARAM, L_w, H_w, L_h, H_h]
                ↑     ↑   ↑   ↑   ↑
             操作码   低  高  低   高
                    (width)(height)
```

硬件重组（伪代码）：

```text
第 1 个数据字节 → width[7:0]
第 2 个数据字节 → width[15:8]      # width 拼好了
第 3 个数据字节 → height[7:0]
第 4 个数据字节 → height[15:8]     # height 拼好了
```

#### 4.2.3 源码精读

仿真后端的拆分（两条命令用了同一套写法）：

[simulation/image_processing_simulation.cpp:23-33](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L23-L33) —— `send_params` 把 width/height 各拆成低/高字节，然后**先 push 操作码，再按 `[0]`、`[1]` 的顺序 push**，即低字节在前。注意第 29 行先 push 操作码 `COMMAND_PARAM`。

[simulation/image_processing_simulation.cpp:72-78](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L72-L78) —— `send_add` 对 `int16_t value` 做同样的拆分（`value&0xFF` 与 `(value>>8)&0xFF`），证明带符号数与无符号数拆法一致。

iCE40 后端**逐字节完全相同**的拆分：

[ice40/software/image_processing_ice40.cpp:38-48](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L38-L48) —— `send_params` 的拆分与 push 顺序和仿真后端一字不差，只是每步换成 `spi_command_send(SPI_SEND_DATA, ...)`。

[ice40/software/image_processing_ice40.cpp:90-97](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L90-L97) —— `send_add` 同理。

硬件侧的小端重组——`COMMAND_APPLY_ADD` 的参数读取状态：

[hdl/image_processing.v:355-367](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L355-L367) —— `counter_read==2` 时把第一字节装入 `add_value[7:0]`（低字节），`counter_read==1` 时把第二字节装入 `add_value[15:8]`（高字节），`counter_read==0` 时读第三个字节作为 clamp。**先到的填低位**，正是小端重组。

README 对 `COMMAND_PARAM` 的权威描述也写明了小端：

[README.md:81](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L81) —— 「sizes are given as unsigned 16bits numbers, **in little endian**」「byte0:width LSB, byte1:width MSB」。

#### 4.2.4 代码实践

**实践目标**：手算 `send_params(8, 8)` 和 `send_add(300, true)` 的完整字节帧，并对照两套后端确认一致。

**操作步骤**：

1. 对 `send_params(8, 8)`：width=8 → `8 & 0xFF = 0x08`，`(8>>8)&0xFF = 0x00`，所以 width 两字节是 `[0x08, 0x00]`；height 同理 `[0x08, 0x00]`。
2. 套上操作码 `COMMAND_PARAM=0`，得到整帧 `[0x00, 0x08, 0x00, 0x08, 0x00]`。
3. 对 `send_add(300, true)`：300 = `0x012C`，低字节 `0x2C`，高字节 `0x01`；clamp=true → `1`。操作码 `COMMAND_APPLY_ADD=4`。整帧 `[0x04, 0x2C, 0x01, 0x01]`。
4. 对照 [simulation/image_processing_simulation.cpp:72-78](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L72-L78) 与 [ice40/software/image_processing_ice40.cpp:90-97](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L90-L97)，确认两者发出的字节序列相同。

**需要观察的现象 / 预期结果**：

| 调用 | 仿真后端 fifo_in 中的字节 | iCE40 后端送出的字节 | 模块 comm 接口看到的帧 |
| --- | --- | --- | --- |
| `send_params(8,8)` | `0x00 0x08 0x00 0x08 0x00` | `0x00 0x08 0x00 0x08 0x00` | `0x00 0x08 0x00 0x08 0x00` |
| `send_add(300,true)` | `0x04 0x2C 0x01 0x01` | `0x04 0x2C 0x01 0x01` | `0x04 0x2C 0x01 0x01` |

两套后端在模块接口处逐字节相同。

**待本地验证**：在仿真模式跑一次 `send_params(8,8)`，观察 `main_loop_clk` 打印或用日志抓 `comm_cmd`/`comm_data_in` 的取值，应依次出现 `0x00, 0x08, 0x00, 0x08, 0x00`。

#### 4.2.5 小练习与答案

**练习 1**：`send_add(-1, false)` 的两个加法值字节各是多少？

> **答案**：`int16_t` 的 -1 是 `0xFFFF`，低字节 `0xFF`，高字节 `0xFF`，所以是 `[0xFF, 0xFF]`。整帧（操作码 4）为 `[0x04, 0xFF, 0xFF, 0x00]`。带符号数的拆法与正数完全一样。

**练习 2**：如果误把高字节先发、低字节后发（大端），`send_add(300, ...)` 会被模块理解成多少？

> **答案**：先到的字节填 `[7:0]`，所以 `[0x01, 0x2C]` 会被重组为 `0x2C01` = 11265，远非 300。这就是为什么必须严格遵守小端——硬件的重组逻辑是固定的「先到填低位」。

---

### 4.3 位打包：clamp / source / abs_diff / add_to_result

#### 4.3.1 概念说明

有好几个命令的参数都是**布尔标志**（取值 0 或 1），而且往往一两个就够用：

- `clamp`：结果是否钳位到 `[0, 255]`，防止溢出回绕。
- `absolute_diff`（仅 `binary_sub`）：减法结果是否取绝对值。
- `input_source`（仅卷积）：卷积的输入取自 input 缓冲还是 storage 缓冲。
- `add_to_output`（仅卷积）：卷积结果是覆盖 storage，还是叠加到 storage。

如果每个布尔都单独占一个字节，太浪费带宽（在 SPI 这种慢速通道上尤其可惜）。所以本项目把它们**塞进同一个字节的不同位**：

| 命令 | bit0 | bit1 | bit2 | 打包公式 |
| --- | --- | --- | --- | --- |
| `binary_add` / `binary_mult` / `apply_add` 等 | clamp | — | — | 直接 `clamp` |
| `binary_sub` | clamp | absolute_diff | — | `(absolute_diff<<1) + clamp` |
| `convolution` | clamp | input_source | add_to_output | `(add_to_output<<2) + (input_source<<1) + clamp` |

打包用一个简单的移位加法表达式完成；硬件侧用**位索引** `comm_data_in[0]`、`[1]`、`[2]` 分别取回各个标志。两套后端用**同一个表达式**打包，所以这一字节天然一致。

#### 4.3.2 核心流程

以 `binary_sub(clamp, absolute_diff)` 为例：

```text
打包字节 = (absolute_diff << 1) + clamp
         = bit1:abs_diff  bit0:clamp

例：clamp=true(1), absolute_diff=true(1)
   = (1 << 1) + 1 = 2 + 1 = 3   即 0b00000011
```

以 `convolution(clamp, input_source, add_to_output)` 为例：

```text
打包字节 = (add_to_output << 2) + (input_source << 1) + clamp
         = bit2:add   bit1:source   bit0:clamp

例：clamp=true(1), input_source=false(0), add_to_output=true(1)
   = (1 << 2) + (0 << 1) + 1 = 4 + 0 + 1 = 5   即 0b00000101
```

硬件解包：

```text
clamp            <= comm_data_in[0]
absolute_diff    <= comm_data_in[1]   # binary_sub
input_source     <= comm_data_in[1]   # convolution
add_to_output    <= comm_data_in[2]   # convolution
```

注意：位的位置是**按命令各自约定的**——`bit1` 在 `binary_sub` 里是 `absolute_diff`，在卷积里是 `input_source`。这没问题，因为模块靠操作码知道当前在解哪条命令。

#### 4.3.3 源码精读

两套后端用**完全相同的表达式**打包——这是「字节一致」的根本保证：

[simulation/image_processing_simulation.cpp:185-192](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L185-L192) —— 仿真后端 `send_binary_sub`，第 187 行 `(absolute_diff<<1)+clamp`。

[ice40/software/image_processing_ice40.cpp:189-192](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L189-L192) —— iCE40 后端 `send_binary_sub`，第 191 行同样的 `(absolute_diff<<1)+clamp`。

[simulation/image_processing_simulation.cpp:203-210](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L203-L210) —— 仿真后端 `send_convolution`，第 206 行 `(add_to_output<<2)+(input_source<<1)+clamp`。

[ice40/software/image_processing_ice40.cpp:200-207](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L200-L207) —— iCE40 后端 `send_convolution`，第 202 行同样的表达式。

硬件侧的解包：

[hdl/image_processing.v:463-473](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L463-L473) —— `STATE_BINARY_SUB_READ_PARAM`，第 468 行 `clamp <= comm_data_in[0]`，第 469 行 `absolute_diff <= comm_data_in[1]`。

[hdl/image_processing.v:431-438](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L431-L438) —— `STATE_CONVOLUTION_READ_PARAM` 的第一字节（`counter_read==9`），第 435-437 行分别取 `comm_data_in[0]`、`[1]`、`[2]` 为 clamp/source/add。

对应的寄存器声明，确认每个标志都是 1 位：

[hdl/image_processing.v:134-136](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L134-L136) —— `reg absolute_diff; reg convolution_source_input; reg convolution_add_to_result;`，各 1 位。

README 的命令表也写明了位含义，可作为交叉验证：

[README.md:91](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L91) —— `COMMAND_BINARY_SUB`：byte0 bit0=clamp，bit1=absolute difference。

[README.md:90](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L90) —— `COMMAND_CONVOLUTION`：byte0 bit0=clamp、bit1=source input、bit2=add to result。

#### 4.3.4 代码实践

**实践目标**：手算 `send_binary_sub(true, true)` 与 `send_convolution(kernel, true, false, true)` 的打包字节，并确认两套后端算出的值相同。

**操作步骤**：

1. `send_binary_sub(true, true)`：`clamp=1`，`absolute_diff=1`，打包 = `(1<<1)+1 = 3`。
2. `send_convolution(_, true, false, true)`：`clamp=1`，`input_source=0`，`add_to_output=1`，打包 = `(1<<2)+(0<<1)+1 = 5`。
3. 对照上面四个源码链接，确认仿真与 iCE40 用的是同一表达式。

**需要观察的现象 / 预期结果**：

| 调用 | 打包字节（十进制 / 二进制） | 仿真后端 | iCE40 后端 | 硬件解出 |
| --- | --- | --- | --- | --- |
| `send_binary_sub(true,true)` | 3 / `0b011` | 3 | 3 | clamp=1, abs_diff=1 |
| `send_convolution(_,T,F,T)` | 5 / `0b101` | 5 | 5 | clamp=1, source=0, add=1 |

两套后端这一字节完全一致；硬件按位索引能正确还原每个布尔。

**待本地验证**：在 `main.cpp` 临时加一行打印 `(absolute_diff<<1)+clamp` 的值（不改源码逻辑，仅观察），应得 3。

#### 4.3.5 小练习与答案

**练习 1**：`send_binary_sub(false, true)`（不钳位、取绝对差）的打包字节是多少？

> **答案**：`(1<<1)+0 = 2`，即 `0b10`。bit0=clamp=0，bit1=abs_diff=1。

**练习 2**：如果想在卷积里再加一个布尔参数（比如「是否对结果取反」），把它放在 bit3，打包表达式该怎么改？硬件侧又该怎么取？

> **答案**：打包表达式加一项 `(invert << 3)`，即 `(invert<<3)+(add_to_output<<2)+(input_source<<1)+clamp`；硬件侧新增 `invert <= comm_data_in[3];`。注意：只要新增位，就必须同步改 hpp 签名、两套后端打包、HDL 解包三处，这正是下一讲 u7-l2「扩展新操作」要面对的联动修改。

---

### 4.4 卷积 3x3 矩阵的符号扩展

#### 4.4.1 概念说明

卷积需要一个 3×3 的**核（kernel）**，共 9 个系数。这些系数可以是负数（比如边缘检测核里中心为正、四邻为负），所以它们是 **8 位有符号定点数**，格式为 1 位符号 + 3 位整数 + 4 位小数（见 u1-l1）。

主机把 9 个系数各作为 1 个 `uint8_t` 字节发出（共 9 字节，紧跟在卷积的打包标志字节之后）。但硬件里做「乘加」时用的是 16 位运算，所以收到这 9 个字节后，要把每个 8 位有符号数**符号扩展**成 16 位有符号数，存进 `convolution_matrix[0..8]`。

符号扩展（sign extension）的含义：把最高位（符号位）复制填充到高位，使**数值不变**但位宽变宽。对一个 8 位补码数 \(b\)，其符号位是 \(b\) 的 bit7：

- 若 bit7 = 0（非负），高位全填 0；
- 若 bit7 = 1（负数），高位全填 1。

扩展后的 16 位数与原 8 位数代表**同一个数值**。例如：

| 原字节（uint8） | 8 位有符号值 | 定点值（÷16） | 符号扩展为 16 位 | 16 位有符号值 |
| --- | --- | --- | --- | --- |
| `0x10` | +16 | +1.0 | `0x0010` | +16 |
| `0xF0` | -16 | -1.0 | `0xFFF0` | -16 |
| `0xFF` | -1 | -0.0625 | `0xFFFF` | -1 |

定点还原（÷16）发生在后续的 clamp 阶段，属于 u5 卷积单元的内容；本讲只关注**字节如何变成 16 位有符号数**。

#### 4.4.2 核心流程

```text
主机发出：[COMMAND_CONVOLUTION(9)] [打包标志 1B] [k0] [k1] ... [k8]
                                                       ↑ 9 个 uint8 字节

硬件 STATE_CONVOLUTION_READ_PARAM 逐字节接收：
  counter_read==9  → 这是「打包标志」字节（clamp/source/add），不是核
  counter_read==8  → convolution_matrix[0] <= 符号扩展(k0)
  counter_read==7  → convolution_matrix[1] <= 符号扩展(k1)
  ...
  counter_read==0  → convolution_matrix[8] <= 符号扩展(k8)  → 推进到卷积运算

符号扩展(k) = { {8{k[7]}}, k }      # 把 k 的符号位 k[7] 复制 8 份拼在前面
```

注意核的存储顺序：软件按 `kernel[0]..kernel[8]` 顺序发送，硬件按 `convolution_matrix[0]..convolution_matrix[8]` 顺序接收（行优先：`kernel[0..2]` 是核的第 1 行），两者一一对齐。

#### 4.4.3 源码精读

主机侧只管按顺序发 9 个字节，不做符号处理：

[simulation/image_processing_simulation.cpp:208-210](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L208-L210) —— 仿真后端循环 `i=0..8` 依次 push `kernel[i]`，原样送出。

[ice40/software/image_processing_ice40.cpp:204-206](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L204-L206) —— iCE40 后端同样的循环发送。

16 位有符号存储容器声明：

[hdl/image_processing.v:145](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L145) —— `reg [15:0] convolution_matrix [0:8];`，9 个 16 位寄存器（注意 `reg [15:0]` 默认按无符号声明，但配合 `$signed` 在运算时按有符号解释）。

符号扩展的核心那一行：

[hdl/image_processing.v:439-442](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L439-L442) —— 第 441 行 `convolution_matrix[8-counter_read] <= { {8{comm_data_in[7]}}, comm_data_in };`。`{8{comm_data_in[7]}}` 把符号位 `comm_data_in[7]` 复制 8 份，拼在 8 位 `comm_data_in` 前面，得到 16 位结果。这就是从 int8 到 int16 的符号扩展。

> 验证索引对齐：`counter_read==8` 时 `8-counter_read=0` → `matrix[0] <= k0`；`counter_read==0` 时 `8-0=8` → `matrix[8] <= k8`。所以 `matrix[i]` 正好存 `kernel[i]`，行优先顺序与软件一致。

它如何被消费（仅作印证，乘加细节在 u5）：

[hdl/image_processing.v:685-693](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L685-L693) —— 卷积计算把 `convolution_matrix[0..2]` 与像素邻域相乘累加，可见 `convolution_matrix` 确实作为 16 位有符号数参与运算（与无符号的像素拼接 `{8'b0, pixel}` 相对）。

#### 4.4.4 代码实践

**实践目标**：手算一个含负数的 3×3 核被符号扩展后的 9 个 16 位值。

**操作步骤**：

1. 取一个常见的边缘检测核（中心正、四邻负），定点表示：
   - 中心 +1.0 → 字节 `0x10`
   - 四邻 -1.0 → 字节 `0xF0`
   - 四角 0 → 字节 `0x00`
   - 即 `kernel = {0xF0,0xF0,0xF0, 0xF0,0x10,0xF0, 0xF0,0xF0,0xF0}`（行优先）。
2. 对每个字节套用 `{ {8{bit7}}, byte }` 求扩展结果。
3. 对照 [hdl/image_processing.v:441](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L441) 验证你的手算。

**需要观察的现象 / 预期结果**：

| kernel 字节 | bit7 | `{8{bit7}}` | 扩展后 16 位（hex） | 16 位有符号值 |
| --- | --- | --- | --- | --- |
| `0xF0` | 1 | `0xFF` | `0xFFF0` | -16（即 -1.0） |
| `0x10` | 0 | `0x00` | `0x0010` | +16（即 +1.0） |
| `0x00` | 0 | `0x00` | `0x0000` | 0 |

预期：负系数（`0xF0`）扩展后高位全 1（`0xFFF0`），数值仍是 -16；正系数高位全 0。**数值在扩展前后不变**，这正是符号扩展的目的。

**待本地验证**：可在 Verilator 仿真里给模块发上述核，用 `$display` 或在 `main_loop_clk` 里打印 `top->convolution_matrix[4]`（若该信号对仿真可见），应见 `0x0010`。

#### 4.4.5 小练习与答案

**练习 1**：`kernel[0]=0x80`（即 -128，定点 -8.0）符号扩展后是多少？

> **答案**：bit7=1，`{8{1}}=0xFF`，扩展为 `0xFF80`，16 位有符号值 = -128，定点值 -8.0。注意 `0x80` 是 8 位有符号能表示的最小值。

**练习 2**：如果硬件忘了做符号扩展、直接把 `0xF0` 零扩展成 `0x00F0`（=+240），卷积结果会怎样出错？

> **答案**：本该是 -16 的系数被当成 +240，乘加结果会严重偏正，边缘检测完全失效。这就是为什么对带符号核必须**符号扩展**而非零扩展——零扩展会把负数误读成一个大的正数。

---

## 5. 综合实践

**任务**：完整写出 `send_binary_sub(true, true)` 与 `send_convolution(kernel, true, false, true)` 在**仿真后端**和 **iCE40 后端**下分别产生的字节流，证明剥掉传输外壳后，两者送到 `image_processing` 模块的 `comm` 接口的字节序列**逐字节相同**。

设 `kernel = {0x10, 0x00, 0x00, 0x00, 0x10, 0x00, 0x00, 0x00, 0x10}`（仅示意字节流，核的实际含义见 u5-l3）。

### 步骤 1：算每个字节

- `send_binary_sub(true, true)`：操作码 `COMMAND_BINARY_SUB=10=0x0A`；打包字节 `(1<<1)+1 = 3 = 0x03`。
- `send_convolution(_, true, false, true)`：操作码 `COMMAND_CONVOLUTION=9=0x09`；打包字节 `(1<<2)+(0<<1)+1 = 5 = 0x05`；后接 9 个核字节。

### 步骤 2：填表对照

| | `send_binary_sub(true,true)` | `send_convolution(kernel, T, F, T)` |
| --- | --- | --- |
| **仿真后端**（fifo_in 中的 Operation 流） | `Op(cmd=0x0A)`、`Op(data=0x03)` | `Op(cmd=0x09)`、`Op(data=0x05)`、`Op(data=0x10)`、`Op(data=0x00)` ×2、`Op(data=0x10)`、`Op(data=0x00)` ×2、`Op(data=0x10)` |
| **iCE40 后端**（SPI 事务流） | `(SPI_SEND_CMD,0x0A)`、`(SPI_SEND_DATA,0x03)` | `(SPI_SEND_CMD,0x09)`、`(SPI_SEND_DATA,0x05)`、`(SPI_SEND_DATA,0x10)`、… 9 个 `SPI_SEND_DATA` |
| **模块 comm 接口看到的帧**（剥壳后） | `0x0A 0x03` | `0x09 0x05 0x10 0x00 0x00 0x00 0x10 0x00 0x00 0x00 0x10` |

### 步骤 3：得出结论

- 仿真后端的 `Operation` 壳（`is_command` 标志）与 iCE40 后端的 SPI 壳（`SPI_SEND_CMD` / `SPI_SEND_DATA`）只是**传输层**的差异。
- 剥掉外壳后，两套后端送到 `image_processing` 模块的**字节序列完全相同**。这正是一份 `main.cpp` 能同时驱动仿真和硬件的根本原因（见 u2-l1 的多态契约）。
- 硬件侧：`STATE_BINARY_SUB_READ_PARAM` 会从 `0x03` 解出 `clamp=1`、`absolute_diff=1`；`STATE_CONVOLUTION_READ_PARAM` 会从 `0x05` 解出 `clamp=1`、`source=0`、`add=1`，并把后 9 个字节符号扩展存入 `convolution_matrix`。

### 进阶观察（可选）

iCE40 后端在 `send_image` 里用 `SPI_SEND_DATA32` 一次发 32 字节，而仿真后端每个像素都 push 一个 `Operation`（[simulation/image_processing_simulation.cpp:61-69](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp#L61-L69) vs [ice40/software/image_processing_ice40.cpp:69-88](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/software/image_processing_ice40.cpp#L69-L88)）。这是传输层的**吞吐优化**，不改变逻辑字节流——图像的每个像素字节最终都按原顺序到达模块。可见「逻辑协议」与「物理传输」被干净地分层了。

---

## 6. 本讲小结

- 一条命令就是一帧：**1 字节操作码 + 0 到 N 字节参数**，参数个数由操作码预先决定（HDL 在派发时给 `counter_read` 装入 `N-1`）。
- 16 位参数统一用**小端序**：先发低字节、后发高字节；带符号（`int16_t`）与无符号的拆分方法相同，硬件靠「先到填 `[7:0]`」重组。
- 多个布尔参数被**位打包**进同一字节（`(absolute_diff<<1)+clamp`、`(add_to_output<<2)+(input_source<<1)+clamp`），硬件用位索引 `comm_data_in[0/1/2]` 取回。
- 卷积 9 个核字节是 **8 位有符号定点数**，硬件用 `{ {8{comm_data_in[7]}}, comm_data_in }` 做**符号扩展**成 16 位存入 `convolution_matrix`，数值不变。
- 仿真后端与 iCE40 后端在打包、拆分上用**完全相同的表达式**，所以送到模块 `comm` 接口的字节流**逐字节一致**；两者差别只在传输外壳（FIFO + `is_command` 标志 vs SPI 的 `SPI_SEND_CMD`/`SPI_SEND_DATA` 事务）。
- 这套「逻辑协议与物理传输分层」的设计，正是 `main.cpp` 一份源码能驱动两套后端的底层原因。

## 7. 下一步学习建议

本讲把「字节长什么样」讲透了，但还有两个方向的空白：

1. **字节是怎么被硬件逐个吃掉的**：`STATE_WAIT_COMMAND` 的派发、`counter_read` 倒计时、各 `*_READ_PARAM` 状态、以及 `comm_data_in_valid` 的握手时序——这是 u3 单元「核心 HDL 模块」的主题，建议接着读 [u3-l3 主命令处理状态机](u3-l3-command-fsm.md) 与 [u3-l4 图像发送/接收与参数读取状态](u3-l4-send-receive-states.md)。
2. **打包好的字节在运算里怎么用**：`clamp` 位如何驱动钳位函数、卷积核符号扩展后如何参与 9 拍乘加、定点如何还原——见 u4 单元（运算状态机）与 u5 单元（卷积引擎）。
3. **传输外壳的内部**：iCE40 的 SPI 事务如何经 `spi_interface.v` 翻译成 `comm` 接口、FTDI 如何驱动 MPSSE——见 u6 单元（仿真与硬件两条后端）。

建议按 u3 → u4 → u5 → u6 的顺序继续，每一步都把本讲的「字节视角」作为对照基准。
