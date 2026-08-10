# iCE40 硬件顶层与 SPRAM 接口

## 1. 本讲目标

本讲是「仿真与硬件两条后端」单元的第二篇。上一篇 u6-l1 讲了仿真后端如何用 C++ 数组模拟 RAM、用 FIFO 队列模拟通信；本讲转到硬件后端，聚焦芯片内部真正发生的事情：核心模块 `image_processing` 在 iCE40 芯片里到底被谁「喂」命令、被谁「供」内存。

学完本讲你应该能够：

1. 画出 `top.v` 里三个模块（`image_processing` / `ram_interface` / `spi_interface`）的连线图，说清存储器（mem）、通信（comm）、SPI 物理三类接口各由谁连到谁。
2. 解释为什么 128KB 片上存储必须用 **4 片 `SB_SPRAM256KA`** 拼，并且用 `addr[16:15]` 做片选。
3. 看懂 `ram_interface` 里 `output_mux` 两级寄存存器与 `rd_en_buffer` 三级移位寄存器如何共同「对齐」SPRAM 的一拍读延迟，让 `data_read_valid` 在数据真正可用的那一拍才拉高。

---

## 2. 前置知识

本讲假设你已经学过：

- **u3-l1**：`image_processing` 模块对外的两大接口——存储器接口（`addr/wr_en/rd_en/data_write/data_read/data_read_valid`）与通信接口（`comm_cmd/comm_data_in/_valid/comm_data_out/_valid/_free`）。它「只认接口、不认实现」，自己不含任何 RAM。
- **u3-l2 / u3-l4**：存储字宽 16 位、每字装 2 个像素；`STATE_READ_IMG` 里 `rd_en` 是**单周期脉冲**，配合 `data_read_valid` 握手完成一次读取。
- **u1-l4**：iCE40 的开源工具链（yosys→arachne-pnr→icepack→iceprog），综合入口是 `top.v`。

几个本讲要用到的新术语：

- **顶层模块（top）**：一个 FPGA 设计最外面的模块，它的端口就是芯片的物理引脚。`top.v` 把内部所有模块「包」起来对外。
- **例化（instantiation）**：在一个模块里放上另一个模块的「一份」，并把端口信号连起来。类似面向对象里创建对象。
- **原语（primitive）**：FPGA 厂商预先固化在芯片里的硬件块，Verilog 里直接调用即可，无需自己用逻辑门搭。`SB_SPRAM256KA`（单端口 RAM）和下一篇要讲的 `SB_SPI` 都是 iCE40 UltraPlus 的原语。
- **读延迟（read latency）**：从给出地址到数据出现在端口上所需的时钟周期数。`SB_SPRAM256KA` 是**同步**RAM，读延迟为 1 拍——地址在第 N 拍给出，数据在第 N+1 拍才出现在 `DATAOUT`。

---

## 3. 本讲源码地图

本讲只涉及两个文件，它们都在硬件后端目录 `ice40/hdl/` 下：

| 文件 | 作用 |
| --- | --- |
| [`ice40/hdl/top.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v) | 顶层模块。例化 `image_processing`、`ram_interface`、`spi_interface` 三个实例并用 wire 把它们的端口连起来。它的端口就是芯片物理引脚。 |
| [`ice40/hdl/ram_interface.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v) | 存储器接口模块。例化 4 片 `SB_SPRAM256KA` 原语拼出 128KB，并用一条小流水线对齐 SPRAM 的读延迟。 |

辅助参考（本讲只作背景）：

| 文件 | 作用 |
| --- | --- |
| [`ice40/hdl/io.pcf`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/io.pcf) | 引脚约束，把 `top` 的端口（clk、LED、SPI 四线）锁到 iCE40UP5K 的物理引脚。 |
| [`hdl/image_processing.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心模块（被 `top.v` 用 `include` 纳入）。本讲只看它的端口与 `STATE_READ_IMG` 如何驱动 `rd_en`。 |

注意：`top.v` 第一行就是 `` `include "../../hdl/image_processing.v" ``，所以平台无关的核心模块是在顶层被「物理」包含进硬件后端的——这与仿真后端用 Verilator 单独编译它（见 u6-l1）形成对照。

---

## 4. 核心概念与源码讲解

### 4.1 top.v：三个模块的例化与三类接口的互连

#### 4.1.1 概念说明

上一篇 u6-l1 的仿真后端里，「存储」和「通信」都是用 C++ 在模块**外部**模拟的。到了真实芯片，这两个角色必须由真实的硬件来扮演：

- **谁来当存储？** → `ram_interface`（用片上 SPRAM 实现）。
- **谁来当通信对端？** → `spi_interface`（下一篇 u6-l3 详讲，用片上 `SB_SPI` 原语把主机的 SPI 包翻译成 comm 接口信号）。

`top.v` 的全部职责，就是把这三个模块摆好、用 wire 把端口连起来。它自己几乎不含逻辑。这正体现了 u3-l1 的核心结论——`image_processing` **只认接口、不认实现**：在仿真里存储是数组、通信是队列；在硬件里存储是 SPRAM、通信是 SPI，而核心模块一行代码都不用改。

#### 4.1.2 核心流程

`top.v` 的连线可以归纳为三组接口：

```
                 ┌─────────────────────── top.v ───────────────────────┐
   物理引脚       │                                                       │
   clk ──────────┼──┬──────────────────┬──────────────────┐              │
   SPI_* ────────┼──┼──────────────────┼──────────────────┤              │
   LED_* ────────┼──┼──────────────────┼──────────────────┤              │
                 │  │                  │                  │              │
                 │  ▼                  ▼                  ▼              │
                 │ spi_interface ◄──comm 接口──► image_processing ◄──mem 接口──► ram_interface
                 │  (SPI↔comm 翻译)                        (核心运算)        (SPRAM 128KB)
                 └───────────────────────────────────────────────────────┘
```

- **存储器接口（mem）**：`image_processing` ⟷ `ram_interface`，共 6 根线。
- **通信接口（comm）**：`image_processing` ⟷ `spi_interface`，共 6 根线。
- **SPI 物理接口**：`spi_interface` ⟷ 芯片引脚（`SPI_SCK/SS/MOSI/MISO`）。

关键观察：`image_processing` **完全不接触** SPI 引脚，也**完全不接触** SPRAM——它只通过 comm 接口收发命令、通过 mem 接口读写存储。两扇「门」之外的世界它一无所知。

#### 4.1.3 源码精读

**顶层端口就是物理引脚。** `top` 模块的端口声明里，`clk`、三色 LED、SPI 四线都是真实的芯片引脚（具体锁到哪个脚由 `io.pcf` 决定，见 u1-l4）：

[top.v:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L5) 声明 `top` 的端口：`SW`、`clk`、`LED_R/G/B`、`SPI_SCK/SS/MOSI/MISO`。

LED 是低有效（active low），所以输出取反：

[top.v:9-L12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L9-L12) `assign LED_R = ~led[0];` 等——板子上 LED 共阳极，给 0 才点亮。

**存储器接口的 6 根 wire**（命名前缀 `ip_mem_`，意为 image_processing 的 mem 接口）：

[top.v:16-L21](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L16-L21) 声明 `addr/wr_en/rd_en/data_write/data_read/data_read_valid`。注意 `addr` 是 32 位——`image_processing` 内部按字节寻址用满低位，而 `ram_interface` 只用其中 17 位（见 4.3）。

**通信接口的 6 根 wire**（命名前缀 `ip_comm_`）：

[top.v:23-L28](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L23-L28) 声明 `comm_cmd/comm_data_in/_valid/comm_data_out/_valid/_free`。

**三个例化**。先看核心模块：把 `top` 的 `clk` 和 `reset_ip`、两组 wire 各自接到 `image_processing` 的端口上：

[top.v:32-L36](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32-L36) 例化 `image_processing`，端口一一对应。

接着 `ram_interface` 只接 mem 那组 wire（它不关心 comm，也不碰 SPI）：

[top.v:38-L40](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L38-L40) 例化 `ram_interface`，只连存储器接口。

`spi_interface` 一侧接 SPI 物理引脚，另一侧接 comm 那组 wire。这里有一个**最容易绕晕的命名翻转**，务必看懂：

[top.v:42-L45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L42-L45) 例化 `spi_interface`。

| spi_interface 端口 | 连到 image_processing 的 | 含义 |
| --- | --- | --- |
| `spi_cmd` | `comm_cmd` | SPI 翻译出的命令操作码 |
| `spi_data_out` | `comm_data_in` | SPI **输出**的数据 → 进核心模块的 **in** |
| `spi_data_out_valid` | `comm_data_in_valid` | 上面数据的 valid |
| `spi_data_in` | `comm_data_out` | 核心模块要 **out** 的数据 → 进 SPI 的 **in**（待回传主机）|
| `spi_data_in_valid` | `comm_data_out_valid` | 上面数据的 valid |
| `spi_data_in_free` | `comm_data_out_free` | SPI 是否有空位接收核心模块的输出 |

口诀：**「我之 out，即彼之 in」**。两个模块面对面站着，一方的输出正好是另一方的输入，于是 `spi_data_out` 接 `comm_data_in`、`spi_data_in` 接 `comm_data_out`。

> 小知识：`reset_ip`（[top.v:14](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L14)）是个声明了却从未赋值的 reg，默认为 0；而 `image_processing` 内部本来就不用 `reset`（初始化全靠 `initial begin`，见 u3-l1）。所以这个复位端口在本设计里是「空转」的，保留它只是为了让端口表完整。

#### 4.1.4 代码实践

**实践目标**：亲手把 `top.v` 的连线图画清楚，建立「核心模块两扇门、两个后端模块各守一扇」的空间感。

**操作步骤**：

1. 打开 [`top.v`](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v)，对照 [top.v:32-L45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32-L45) 三个例化。
2. 用纸笔或画图工具画三个方框：`image_processing`（中）、`ram_interface`（右）、`spi_interface`（左）。
3. 在中—右之间标出 6 根 mem 线（`addr`、`wr_en`、`rd_en`、`data_write`、`data_read`、`data_read_valid`），逐根标方向。
4. 在中—左之间标出 6 根 comm 线，并刻意把 `spi_data_out↔comm_data_in` 这对「翻转」用不同颜色标出来。
5. 在 `spi_interface` 左侧标出 SPI 物理引脚，在 `top` 四周标出 clk/LED。

**需要观察的现象**：画完会发现 `image_processing` 方框的左边只通向 SPI、右边只通向 SPRAM，自己不直接面向任何物理引脚（clk 除外）。

**预期结果**：得到一张与 4.1.2 流程图一致的连线图；能复述「`spi_data_out` 是 SPI 模块的输出，却接在核心模块的 `comm_data_in` 上」这句绕口令。

#### 4.1.5 小练习与答案

**练习 1**：如果要把这块板子换成「以太网通信」而不是 SPI，需要改 `image_processing.v` 吗？
**答案**：不需要。核心模块只认 comm 接口那 6 根线。只要新写一个 `ethernet_interface` 模块，把它例化进 `top.v` 接到同一组 `ip_comm_*` wire 上即可。这正是「只认接口、不认实现」带来的可替换性。

**练习 2**：`top.v` 里的 `reset_ip` 为什么是「空转」的？
**答案**：它是声明了却从未赋值的 reg（恒为默认值），而 `image_processing` 内部并不使用 `reset`（初始化靠 `initial begin`）。所以该信号实际不起作用。

---

### 4.2 SB_SPRAM256KA 原语与 4 片拼装

#### 4.2.1 概念说明

iCE40 UltraPlus（iCE40UP5K）芯片内部有 **1Mbit = 128KB** 的单端口 RAM 资源，但它们并不是一整块 128KB 的存储器，而是切成若干小片的 **`SB_SPRAM256KA` 原语**。每一片 `SB_SPRAM256KA` 的容量是：

\[
256\text{ Kbit} = 256 \times 1024\text{ bit} = 32768\text{ byte} = 32\text{ KB}
\]

按 16 位字宽组织就是 **16K 字 × 16 bit**（16K 个字，每字 2 字节）。要把 128KB 全用上，就得把 **4 片** `SB_SPRAM256KA` 并起来。`ram_interface` 就是干这件事的「拼装工」。

代码首行的注释也直接点明了总量：

[ram_interface.v:1](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L1) 注释 `//ice40 has 1024 kbit of spram ==> 128KB`。

#### 4.2.2 核心流程

`SB_SPRAM256KA` 的关键端口（同步单端口 RAM）：

| 端口 | 含义 |
| --- | --- |
| `ADDRESS[13:0]` | 14 位字地址，寻址 \(2^{14}=16384=16\text{K}\) 个字 |
| `DATAIN[15:0]` | 写入的 16 位数据 |
| `DATAOUT[15:0]` | 读出的 16 位数据（**滞后地址 1 拍**） |
| `WREN` | 写使能（高=写，低=读） |
| `CLOCK` | 同步时钟，上升沿锁存地址/数据 |
| `MASKWREN[3:0]` | 字节写掩码，`4'b1111` 表示整字写 |
| `CHIPSELECT / STANDBY / SLEEP / POWEROFF` | 片选与功耗控制（本设计固定常量） |

4 片的接法是：**地址线和数据线全部并联**，只有 `WREN` 各自独立——这样哪片被写、哪片被读，完全由高位地址译码决定（见 4.3）。

读时序（本讲难点，4.4 展开）：`SB_SPRAM256KA` 是同步 RAM，给地址后的**下一个时钟沿**数据才出现在 `DATAOUT`，这就是「1 拍读延迟」。

#### 4.2.3 源码精读

模块端口（注意它与 `image_processing` 的存储器接口**完全同名**，因为 `top.v` 把它们一一对接）：

[ram_interface.v:10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L10) `module ram_interface(input clk, input [31:0] addr, input wr_en, input rd_en, input [15:0] data_write, output reg [15:0] data_read, output reg data_read_valid);`

内部 wire/reg：

[ram_interface.v:12-L17](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L12-L17) 声明每片独立的写使能 `ram_wren[3:0]`、每片独立的输出 `ram_data_out[3:0]`，以及读延迟流水线 `output_mux[1:0]`、`rd_en_buffer[2:0]`。

第 0 片的例化（其余 3 片结构完全相同）：

[ram_interface.v:20-L32](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L20-L32) 例化 `spram0`。

注意几个要点：
- `.ADDRESS(addr[14:1])` —— 注释 `//14bits (16K*2B)` 说明用 `addr[14:1]` 这 14 位作为片内字地址，正好寻址 16K 字。
- `.DATAIN(data_write)` —— 4 片共用同一条写数据线。
- `.WREN(ram_wren[0])` —— 每片写使能独立，由 4.3 的片选逻辑驱动。
- `.MASKWREN(4'b1111)` —— 整字写（不按字节屏蔽）。
- `.DATAOUT(ram_data_out[0])` —— 每片输出独立，由 4.4 的 `output_mux` 选出正确的那片。

第 1、2、3 片见 [ram_interface.v:34-L46](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L34-L46) 与 [ram_interface.v:48-L74](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L48-L74)，结构完全一致，只是 `WREN` 和 `DATAOUT` 的下标换成 1/2/3。

#### 4.2.4 代码实践

**实践目标**：确认每片 `SB_SPRAM256KA` 的容量与地址位数对应得上。

**操作步骤**：
1. 数一数 `ADDRESS` 端口的位数：代码里是 `addr[14:1]`，共 14 位。
2. 计算 \(2^{14} = 16384\) 字，每字 2 字节 → \(16384 \times 2 = 32768\) 字节 = 32 KB，正好等于 256 Kbit。

**预期结果**：验证「14 位地址 ↔ 16K 字 ↔ 32 KB ↔ 256 Kbit」这条等价链，理解注释里 `16K*2B` 的含义。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MASKWREN` 设成 `4'b1111`？
**答案**：`SB_SPRAM256KA` 是 16 位字宽，`MASKWREN` 4 位分别对应 4 个字节写掩码（每个掩码位管 4 位）。设全 1 表示「整字都写」，配合本项目「每个 16 位字一次性写两个像素」的做法（见 u3-l2），不需要字节级屏蔽。

**练习 2**：4 片 `SB_SPRAM256KA` 的 `ADDRESS` 为什么可以全部接同一个 `addr[14:1]`？
**答案**：因为它们寻址的是各自片内的 16K 字，片内地址相同没冲突；至于到底读写哪一片，由更高位 `addr[16:15]` 选片（见 4.3）。4 片同时给出相同的片内地址、同时把对应字送到各自的 `DATAOUT`，再由 `output_mux` 选出我们要的那一片。

---

### 4.3 addr[16:15] 片选：把 128KB 拆成 4 片 32KB

#### 4.3.1 概念说明

4 片并联后，必须有一个机制告诉硬件「这次读写到底落在哪一片」。`ram_interface` 用 `addr` 的高 2 位 `addr[16:15]` 当**片选**（chip select），把 128KB 地址空间平均切成 4 段 32KB。

把 32 位 `addr` 拆开看，每一位都有归属：

| 位 | 位数 | 用途 | 使用者 |
| --- | --- | --- | --- |
| `addr[0]` | 1 位 | 字内的字节选择（偶=低字节，奇=高字节） | `image_processing`（拼包用，见 u3-l2） |
| `addr[14:1]` | 14 位 | 片内字地址（16K 字） | `ram_interface` → SPRAM 的 `ADDRESS` |
| `addr[16:15]` | 2 位 | 选 4 片中的一片 | `ram_interface` 的片选/写使能译码 |
| `addr[31:17]` | 高位 | 未用（地址空间足够，留空） | 不接 |

注意 `addr[0]` 对 `ram_interface` 是**透明**的——SPRAM 按整 16 位字读写，字节拼拆是 `image_processing` 在 `data_write[7:0]/[15:8]` 层面做的事（见 u3-l2）。所以 `ram_interface` 用 `addr[14:1]`（跳过最低位）作为字地址，连续两个字地址之间在 `addr` 上相差 2。

#### 4.3.2 核心流程

地址空间总览（字节地址）：

\[
\text{总容量} = 2^{17}\text{ 字节} = 131072\text{ B} = 128\text{ KB}
\]

之所以是 \(2^{17}\)，是因为有效地址用到 `addr[16:0]` 共 17 位。4 片各管一段：

| `addr[16:15]` | 选中 | 字节范围 | 容量 |
| --- | --- | --- | --- |
| `00` | spram0 | 0x00000–0x07FFF | 32 KB |
| `01` | spram1 | 0x08000–0x0FFF | 32 KB |
| `10` | spram2 | 0x10000–0x17FFF | 32 KB |
| `11` | spram3 | 0x18000–0x1FFF | 32 KB |

这 4 段拼起来正好对应 u3-l2 里「128KB 切成两个 64KB 缓冲」的模型：`buffer_input_address=0`（input 缓冲）与 `buffer_storage_address=BUFFER2_LOCATION=64KB`（storage 缓冲）各占 2 片 SPRAM。

#### 4.3.3 源码精读

**写使能的片选译码**——一行就把 `wr_en` 路由到正确的片：

[ram_interface.v:76](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L76) `assign ram_wren[addr[16:15]] = wr_en;`

这一句是个**可变下标赋值**：把 `wr_en` 只送给下标为 `addr[16:15]` 的那根 `ram_wren`，其余 3 根保持未使能（综合后等价于一个 4 选 1 译码器：只有被选中的那片 `WREN` 跟随 `wr_en`，其余片的 `WREN` 为 0）。于是写入时只有目标片被写，另外 3 片不受影响。

**读出的片选**则不在这一行，而在 4.4 的 `output_mux`：因为 4 片的 `DATAOUT` 总是同时各自给出片内对应字，需要用一个 mux 根据「发起读那时的 `addr[16:15]`」选出正确的一片。

**为什么读和写的片选方式不一样？** 写是「只要一片动作」，所以直接用当拍的 `addr[16:15]` 译码 `WREN` 即可；读是「4 片都输出、再选一片」，而且选片信号要**延迟**到数据真正出来那一拍才能用（见 4.4），所以读走的是 `output_mux` 流水线，而不是当拍译码。

#### 4.3.4 代码实践

**实践目标**：把 32 位 `addr` 的每一位都对应到一个角色。

**操作步骤**：
1. 打开 [ram_interface.v:76](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L76) 和 [ram_interface.v:80](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L80)（`output_mux[0] <= addr[16:15]`）。
2. 做一张表，把 `addr[31:0]` 32 位分成 4 组（如 4.3.1 的表），写出每组的位数与用途。
3. 计算当 `addr[16:15]=2'b01`、`addr[14:1]=14'd5` 时，落点是哪一片、片内第几个字。

**预期结果**：落点是 spram1、片内第 5 个字（字节地址 = 0x0800A 附近）。能解释 `addr[0]` 为何不进 `ADDRESS`。

#### 4.3.5 小练习与答案

**练习 1**：`buffer_storage_address = BUFFER2_LOCATION = 64KB` 落在哪几片 SPRAM 上？
**答案**：64KB = 0x10000，即 `addr[16]=1, addr[15]=0`，`addr[16:15]=2'b10`，从 spram2 开始。整个 storage 缓冲（64KB）横跨 spram2 和 spram3 两片；input 缓冲（地址 0 起）横跨 spram0 和 spram1。

**练习 2**：为什么 `ram_wren` 的片选用「当拍译码」，而读出的片选却要 `output_mux` 延迟？
**答案**：写操作在 `CLOCK` 上升沿把 `DATAIN` 写入 `ADDRESS` 指向的字，当拍给出 `WREN`+地址即可，不需要等。读操作的数据要到下一拍才出现在 `DATAOUT`，所以「选哪一片的输出」这个决定必须推迟到数据有效的拍，用 `output_mux` 把当拍的 `addr[16:15]` 延迟相应拍数后再用。

---

### 4.4 读延迟流水线：output_mux 与 rd_en_buffer

这是本讲最难、也最精彩的部分。它要解决一个问题：**SPRAM 读数据要等 1 拍，怎么让 `image_processing` 在数据真正可用的那一拍才看到 `data_read_valid=1`？**

#### 4.4.1 概念说明

回顾 `image_processing` 的读握手（u3-l4）：在 `STATE_READ_IMG` 里，它发一次读请求（拉高 `rd_en` 一拍、同时给出 `addr`），然后**轮询** `data_read_valid`，等到 `data_read_valid=1` 才把 `data_read` 锁进 `mem_data_buffer`。

[image_processing.v:373-L382](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L373-L382) `STATE_READ_IMG`：偶拍发读请求（`rd_en<=1`、给 `addr`），之后等 `data_read_valid`。

问题在于 `ram_interface` 这一侧：它必须保证「`data_read_valid` 拉高那一拍，`data_read` 上正好是这次请求读到的字」。这里有两重延迟要处理：

1. **SPRAM 自身的 1 拍读延迟**：地址在拍 N 给出，`DATAOUT` 在拍 N+1 才稳定。
2. **片选信息的延迟**：4 片的 `DATAOUT` 同时都有数据，要选对那一片，就得知道「发起读那时的 `addr[16:15]`」。可这个信息在拍 N 之后就没了（`image_processing` 下一拍可能改 `addr`），所以必须把它**存起来**延迟到数据有效的拍再用。

`output_mux`（两级寄存存器）解决第 2 点；`rd_en_buffer`（三级移位寄存器）解决「`data_read_valid` 何时拉高」。

#### 4.4.2 核心流程

整个读流水线在一次读请求下的时序（设 `rd_en` 在拍 1 为单周期脉冲）：

```
拍:        1        2        3        4
addr:    [请求]    (保持)   (保持)   (保持)
rd_en:    1────────0────────0────────0──►
output_mux[0]:  ?  │  cs(请求) │   ...   │  ...
output_mux[1]:  ?  │     ?     │  cs(请求)│ ...   ← 片选延迟到位
rd_en_buffer:   ...  rb0=1   rb1=1   rb2=1
                          (rb1=1,rb2=0 的窗口在拍3)
SPRAM DATAOUT:        数据稳定(拍2起,只要addr不变)
data_read_valid:                       1 ◄── 在拍4拉高
data_read:                             = 请求的那片的那字
```

关键：`output_mux` 把片选 `addr[16:15]` 延迟到数据有效的那一拍；`rd_en_buffer` 把 `rd_en` 脉冲延迟并对齐到同一拍，用 `(rb[2]==0 && rb[1]==1)` 这一拍生成 `data_read_valid`。两者在**同一拍**汇合，于是 `data_read` 与 `data_read_valid` 同时有效。

> 关于「下降沿」的说法：条件 `rd_en_buffer[2]==0 && rd_en_buffer[1]==1` 描述的是「`rd_en` 的延迟信号还停在 buffer 第 1 级、尚未到达第 2 级」的那一拍。若 `rd_en` 是**持续电平**，这个窗口出现在电平**下降**的传播过程中（故称对齐下降沿）；本项目 `image_processing` 实际发的是**单周期脉冲**，该条件则在每个脉冲后恰好生成一个对齐到读延迟的 `data_read_valid` 脉冲。两种理解殊途同归：目的是让 `data_read_valid` 在 SPRAM 数据与片选 mux 都稳定的那一拍才拉高。

#### 4.4.3 源码精读

整条流水线集中在 `ram_interface` 唯一的 `always` 块里：

[ram_interface.v:78-L87](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L78-L87) 读延迟流水线的全部代码。

逐行拆解：

```verilog
output_mux[0] <= addr[16:15];        // 第 80 行：片选进第 1 级
output_mux[1] <= output_mux[0];      // 第 81 行：片选进第 2 级（延迟 2 拍）
rd_en_buffer[0] <= rd_en;            // 第 82 行：rd_en 进第 1 级
rd_en_buffer[1] <= rd_en_buffer[0];  // 第 83 行：rd_en 进第 2 级
rd_en_buffer[2] <= rd_en_buffer[1];  // 第 84 行：rd_en 进第 3 级
data_read_valid <= (rd_en_buffer[2] == 0 && rd_en_buffer[1] == 1);  // 第 85 行
data_read <= ram_data_out[output_mux[1]];                            // 第 86 行
```

**第 85 行**（`data_read_valid` 的产生）：用 `rd_en_buffer[2]==0 && rd_en_buffer[1]==1` 这个条件，等价于「检测 `rd_en` 经延迟后进入第 1 级、但还没到第 2 级的那一拍」，从而在固定的延迟拍数后输出一个 `data_read_valid` 脉冲。这个延迟拍数正好等于（SPRAM 1 拍读延迟）+（mux 对齐所需的拍数），使 `data_read_valid` 与有效数据同拍出现。

**第 86 行**（`data_read` 的选片）：`data_read` 取自 `ram_data_out[output_mux[1]]`——即「两级延迟后的片选」所指的那片 SPRAM 的输出。`output_mux[1]` 之所以延迟两级，是为了让「选哪一片」这个决定落在 SPRAM 数据已经稳定的那一拍：发起读那拍的 `addr[16:15]` 被存进 `output_mux[0]`，再过一拍进 `output_mux[1]`，正好和数据到达 `DATAOUT` 的时刻对齐。

**两层寄存存为什么恰好是「2 级 mux + 3 级 rd_en」？** 它们是**一起标定**的：要让 `data_read_valid` 拉高那一拍，`output_mux[1]` 必须指向正确的片。`output_mux[1]` 延迟 2 拍、`rd_en_buffer` 延迟到第 3 级触发——两者经过同一套时钟节拍，被作者调到同一拍汇合。这是「用移位寄存器做固定延迟对齐」的经典手法。

#### 4.4.4 代码实践

**实践目标**：把「4 片 16K×2B = 128KB」算清楚，并解释读延迟流水线两层寄存存各自的对齐作用。

**操作步骤**（源码阅读 + 手算）：

1. **容量手算**：一片 = 16K 字 × 2 字节 = 32 KB；4 片 = \(4 \times 32 = 128\) KB。也可写成 \(4 \times 16\text{K} \times 2\text{B} = 4 \times 32768 = 131072\) 字节 = 128 KB。验证它与 [ram_interface.v:1](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L1) 的注释一致。
2. **解释 `rd_en_buffer` 的作用**：阅读 [ram_interface.v:82-L85](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L82-L85)。回答：为什么用 `rd_en_buffer[2]==0 && rd_en_buffer[1]==1` 来置 `data_read_valid`？因为它把 `rd_en` 脉冲延迟到 SPRAM 数据已经稳定的那一拍，在那一拍生成一个 `data_read_valid` 脉冲；这样 `image_processing` 看到握手时数据一定是好的。
3. **解释 `output_mux` 的作用**：阅读 [ram_interface.v:80-L81](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L80-L81) 与 [ram_interface.v:86](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L86)。回答：两级 `output_mux` 把发起读那拍的 `addr[16:15]` 延迟到数据有效的拍，让 `data_read` 从 4 片 `DATAOUT` 中选出正确的那一片。
4. **（可选，待本地验证）波形验证**：若有 ice40 仿真环境，把 `rd_en`、`addr[16:15]`、`rd_en_buffer[2:0]`、`output_mux[1]`、`data_read_valid`、`data_read` 一起加入波形窗口，发起一次跨片读（如从 spram0 切到 spram2），观察 `data_read_valid` 拉高那拍 `data_read` 是否等于目标字、`output_mux[1]` 是否等于当时的片选。

**需要观察的现象**：`data_read_valid` 每次只在高出 `rd_en` 脉冲约 3 拍后出现一个单拍脉冲；该拍 `data_read` 与请求的字一致、`output_mux[1]` 与请求的片选一致。

**预期结果**：
- 4 片容量 = 128 KB；
- `data_read_valid` 的作用是「延迟对齐 SPRAM 读延迟，在数据稳定那拍通知 `image_processing`」；
- `output_mux` 两级是「把片选延迟到数据有效的拍，从 4 片输出里选对那一片」。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `output_mux`，直接写 `data_read <= ram_data_out[addr[16:15]]`，会发生什么？
**答案**：读数据那拍的 `addr[16:15]` 已经不是发起读那拍的值了（`image_processing` 可能已经把 `addr` 改成下一个读地址或别的），于是会从错误的片选出数据，读回张冠李戴的字。`output_mux` 的存在就是为了「记住」发起读时的片选，延迟到数据有效那拍再用。

**练习 2**：把 `data_read_valid <= (rd_en_buffer[2]==0 && rd_en_buffer[1]==1)` 改成 `data_read_valid <= rd_en_buffer[1]`（去掉「窗口」限定）会有什么问题？
**答案**：那样 `data_read_valid` 的高电平会持续过多拍或落点偏移，可能早于或晚于数据稳定的拍，导致 `image_processing` 在错误的拍采样 `data_read`。窗口条件 `rb[2]==0 && rb[1]==1` 把 `data_read_valid` 精确锁定在「数据与片选都就绪」的那一拍。

**练习 3**：为什么 `wr_en` 的片选不需要类似的延迟流水线，一行 `assign ram_wren[addr[16:15]] = wr_en;` 就够？
**答案**：写操作是「即时的」——`CLOCK` 上升沿把 `DATAIN` 写入当拍 `ADDRESS` 与 `WREN` 选中的字，当拍译码即可，没有「数据滞后一拍」的问题。只有读操作需要等数据出来，所以才需要延迟片选和延迟 `valid`。

---

## 5. 综合实践

**任务：为 `ram_interface` 补一张完整的「地址 → 片 → 字」映射表，并标注读流水线每一拍的信号值。**

1. **地址映射表**：基于 [ram_interface.v:20-L74](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L20-L74) 与 [ram_interface.v:76](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/ram_interface.v#L76)，做一张表：列出 `addr[16:15]` 的 4 种取值，各对应哪一片 SPRAM、字节地址区间、容量，并标出 input/storage 两个 64KB 缓冲分别落在哪两片上（结合 u3-l2 的 `BUFFER2_LOCATION`）。

2. **读时序拍拍表**：假设 `image_processing` 在拍 1 发起一次对 spram2（`addr[16:15]=2'b10`）的读请求（`rd_en=1`、`addr[14:1]=5`），之后 `addr` 保持不变。按下表填出拍 1～4 每个信号的值（? 表示该拍尚未确定/无关）：

   | 信号 | 拍1 | 拍2 | 拍3 | 拍4 |
   | --- | --- | --- | --- | --- |
   | `rd_en` | 1 | 0 | 0 | 0 |
   | `rd_en_buffer[0]` | 0 | ? | ? | ? |
   | `rd_en_buffer[1]` | 0 | ? | ? | ? |
   | `rd_en_buffer[2]` | 0 | ? | ? | ? |
   | `output_mux[0]` | ? | ? | ? | ? |
   | `output_mux[1]` | ? | ? | ? | ? |
   | `data_read_valid` | 0 | ? | ? | ? |

   完成后，验证：`data_read_valid` 在哪一拍变 1？那一拍 `output_mux[1]` 是否等于 `2'b10`（选 spram2）？`data_read` 是否等于 spram2 片内第 5 个字？

3. **反思**：用一段话说明「为什么 `image_processing` 可以完全不关心这些延迟细节、只管发 `rd_en` 然后等 `data_read_valid`」——即 `ram_interface` 是如何把 SPRAM 的物理时序**封装**成一个干净握手的。

> 这一任务把本讲的四个最小模块（顶层连线、SPRAM 原语、片选、读延迟流水线）串成一条从「主机发读命令」到「核心模块拿到正确数据」的完整证据链。无法在硬件上跑也没关系，手填时序表本身就是一次扎实的时序理解训练；若有仿真环境则可在波形上逐拍核对（待本地验证）。

---

## 6. 本讲小结

- `top.v` 是个几乎不含逻辑的「接线员」：例化 `image_processing`、`ram_interface`、`spi_interface` 三个模块，用 wire 把 mem 接口、comm 接口、SPI 物理接口分别接好。核心模块**只通过两扇门**（mem/comm）与外界打交道，不碰 SPRAM 也不碰 SPI 引脚。
- comm 接口的连线存在「我之 out 即彼之 in」的命名翻转：`spi_data_out↔comm_data_in`、`spi_data_in↔comm_data_out`，看图时务必小心。
- 128KB 片上存储由 **4 片 `SB_SPRAM256KA`**（每片 16K 字 × 16 bit = 32 KB）拼成，4 片的地址线和数据线并联，只靠写使能/输出 mux 区分。
- 地址线各段分工明确：`addr[0]` 字节选择（核心模块用，对 RAM 透明）、`addr[14:1]` 片内字地址、`addr[16:15]` 片选、高位未用。有效地址 17 位 = 128KB。
- 写片选用当拍译码 `assign ram_wren[addr[16:15]] = wr_en;`；读片选用 `output_mux` 两级寄存存器延迟到数据有效的那一拍。
- `rd_en_buffer` 三级移位寄存器 + `(rb[2]==0 && rb[1]==1)` 窗口检测，把 `rd_en` 脉冲对齐到 SPRAM 数据稳定的拍，生成 `data_read_valid`。它与 `output_mux` 在同一拍汇合，保证 `data_read` 与 `data_read_valid` 同时有效——这就是把「SPRAM 一拍读延迟」封装成干净握手的关键。

---

## 7. 下一步学习建议

本讲把**存储这一侧**的硬件后端讲完了：核心模块的 mem 接口连到了真实的 4 片 SPRAM。下一篇 **u6-l3（SPI 从机接口与 SB_SPI 硬件块）** 将转向**通信这一侧**：讲 `spi_interface.v` 如何用 iCE40 内置的 `SB_SPI` 原语做从机，把主机经 FTDI 发来的 SPI 包（`RECEIVE_CMD`/`RECEIVE_DATA`/`SEND_DATA32`）翻译成 `image_processing` 的 comm 接口信号。

建议先做两件事再进入下一篇：

1. 重看 [top.v:42-L45](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L42-L45) 的 `spi_interface` 例化，记住 6 根 comm wire 各自接到了 `spi_interface` 的哪个端口——这是下一篇的「接口底图」。
2. 浏览 [spi_interface.v:1-L40](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/spi_interface.v#L1-L40) 的端口与 `SB_SPI` 原语例化，对 `SB_SPI` 的 `stb/rw/adr/dati/ack` 寄存器访问方式有个第一印象。

本讲覆盖的最小模块：**top 顶层例化与三类接口互连**、**SB_SPRAM256KA 原语与 4 片拼装**、**addr[16:15] 片选译码**、**output_mux / rd_en_buffer 读延迟流水线**。
