# FPGA 测试环境与 AXI 互连

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `de2_115_top.sv` 这个 FPGA 顶层把哪些模块挂在一起，构成了一颗可以上板运行的完整 SoC（System on Chip）。
- 理解 Nyuzi 核、SDRAM 控制器、Boot ROM、VGA、UART 等外设之间是「谁通过哪条总线跟谁说话」。
- 读懂 `axi_interconnect.sv` 这个自定义 AXI 互连如何用地址区间把事务路由到正确的从设备，并能画出整张地址映射表。
- 把「FPGA 上板环境」与此前学过的「Verilator 仿真环境」「C 模拟器环境」对应起来，明白它们各自替换了 SoC 的哪一部分。

本讲是「FPGA SoC 与外设」单元的第一篇，承接 u6-l4（AXI 总线与 IO 互连）中学过的「Nyuzi 核对外有 AXI 与 IO 两条总线」，把视角从「核的出口」拉到「整块板子的系统连接」。

## 2. 前置知识

在读本讲前，你需要先建立以下几个直觉（均可在此前讲义中找到展开）：

- **SoC（片上系统）**：把处理器核、内存控制器、各种外设控制器都放进同一片 FPGA，用一组「总线」把它们连起来，像主板一样让各部件互相通信。
- **AXI4 总线**：ARM 提出的高性能总线协议，把「写地址 / 写数据 / 写响应 / 读地址 / 读数据」拆成五条独立通道，支持突发（burst）传输。Nyuzi 在 [`defines.svh`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L429-L469) 里把它简化成一组接口信号。所谓 **主设备（master）** 发起读写，**从设备（slave）** 响应。
- **IO 总线（`io_bus`）**：Nyuzi 核的另一条出口，专门接「不可缓存的外设寄存器」（MMIO）。它比 AXI 简单得多，只有 `write_en / read_en / address / write_data / read_data` 五个信号，详见 [`defines.svh` 中的 io_bus_interface](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L416-L425)。
- **两条总线的分工**（u6-l4）：要缓存的主存访问走 AXI（经 L2）；地址高 16 位为 `0xffff` 的 MMIO 访问绕过缓存、走 IO 总线。
- **Boot ROM 与复位向量**：处理器复位后第一条指令的地址由 `RESET_PC` 决定，FPGA 上这个地址指向一片只读的 Boot ROM。

一个关键认知：Nyuzi 核本身「不知道」外面接的是 SDRAM 还是 DDR 还是 SRAM，它只管按 AXI 协议发请求。把请求翻译成具体存储器时序的工作，交给**外设控制器**完成。本讲就是讲「这些控制器怎么被组织成一颗 SoC」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [hardware/fpga/de2-115/de2_115_top.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv) | DE2-115 开发板的 FPGA 顶层，实例化 Nyuzi 核、AXI 互连、SDRAM/ROM 控制器与各外设，是整颗 SoC 的「总装配图」。 |
| [hardware/fpga/common/axi_interconnect.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv) | 自定义 AXI 互连：在两个主设备与两个从设备之间按地址区间路由 AXI 事务。 |
| [hardware/fpga/common/axi_rom.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_rom.sv) | AXI 只读存储器，综合时用 `$readmemh` 装入 Boot ROM 镜像。 |
| [hardware/fpga/de2-115/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md) | DE2-115 上板流程：连线、综合、烧录、串口加载程序。 |
| [software/bootrom/boot.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c) | 烧进 Boot ROM 的串口一级引导程序，从中可读到外设寄存器的软件侧地址约定。 |
| [hardware/core/defines.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh) | 定义 `axi4_interface` 与 `io_bus_interface` 两组接口信号。 |

注意目录划分：`hardware/fpga/common/` 放「与具体板子无关」的通用外设与互连；`hardware/fpga/de2-115/` 放「Terasic DE2-115 这块板子专属」的顶层与引脚约束。换一块板子时，`common/` 可以复用，只需重写板级顶层。

## 4. 核心概念与源码讲解

### 4.1 FPGA 顶层：de2_115_top

#### 4.1.1 概念说明

`de2_115_top` 是整颗 SoC 的最顶层模块。它的职责只有一件事：**把 Nyuzi 核、内存控制器和一堆外设控制器实例化出来，用总线把它们连起来，并把信号引到 FPGA 板子物理引脚上**（LED、按键、SDRAM、VGA、SD 卡、PS/2 等）。

可以把这一层想象成「主板原理图」：Nyuzi 核是 CPU 插槽，SDRAM 控制器是北桥内存控制器，VGA/UART/SD/PS2 是各种外设芯片，AXI 互连是连接它们的总线矩阵。

一个容易忽略的点：这个顶层里**既有 AXI 设备，也有 IO 总线设备**。Nyuzi 核同时伸出两条总线，顶层必须分别接好——可缓存的主存走 AXI，不可缓存的外设寄存器走 IO 总线。

#### 4.1.2 核心流程

SoC 的整体数据流可以概括为：

1. **上电复位**：外部 50 MHz 晶振 `clk50` 直接当系统时钟；按板上 KEY[0] 或虚拟 JTAG 触发 `reset`，经同步器后送给所有模块。
2. **取第一条指令**：Nyuzi 核以 `RESET_PC = 0xfffee000`（即 Boot ROM 基地址）开始取指，这条读请求经核内 L2 → AXI 主口 `axi_bus_m[0]` → AXI 互连 → 从口 `axi_bus_s[1]` → Boot ROM。
3. **运行 Boot ROM**：Boot ROM 里跑的是串口一级引导（`boot.c`），它通过 IO 总线点 LED、配 UART、从宿主机串口接收用户程序字节，直接按字写入 SDRAM。
4. **跳转执行用户程序**：收到 `EXECUTE` 命令后，Boot ROM `return`，程序计数器落入刚被写满的 SDRAM，用户程序接管。
5. **运行期**：用户程序经 AXI 访问 SDRAM 中的代码与数据，经 IO 总线访问外设；VGA 控制器则作为另一个 AXI 主设备，不断从 SDRAM 帧缓冲 DMA 取像素送给 DAC。

#### 4.1.3 源码精读

顶层模块的端口几乎全是 FPGA 板上的物理引脚（LED、七段数码管、SDRAM、VGA、SD、PS/2），以及一个 50 MHz 时钟输入：

```systemverilog
module de2_115_top(
    input                       clk50,
    input                       reset_btn,    // KEY[0]
    output logic[17:0]          red_led,
    ...
    inout [31:0]                dram_dq,
    output [7:0]                vga_r, ...
    output                      sd_clk, ...
    input                       ps2_clk, input ps2_data);
```

见 [de2_115_top.sv:21-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L21-L69)。两个关键本地参数锁定了复位向量和外设数量：

```systemverilog
localparam BOOT_ROM_BASE = 32'hfffee000;
localparam NUM_PERIPHERALS = 5;
```

见 [de2_115_top.sv:73-74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L73-L74)。`BOOT_ROM_BASE` 既是 Boot ROM 的基地址，也是 `RESET_PC`，两者必须一致。

接着是总线条数的声明——**两组 AXI 接口数组，分别朝外连「从设备」和「主设备」**：

```systemverilog
axi4_interface axi_bus_s[1:0]();   // 朝外接从设备（SDRAM、ROM）
axi4_interface axi_bus_m[1:0]();   // 被外部主设备驱动（Nyuzi、VGA）
```

见 [de2_115_top.sv:84-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L84-L85)。这里 `_s` 表示「interconnect 的 slave 侧端口，去驱动外部 slave」；`_m` 表示「interconnect 的 master 侧端口，被外部 master 驱动」。

Nyuzi 核作为 0 号主设备接入，复位地址指向 Boot ROM，并把中断请求按位拼成向量：

```systemverilog
nyuzi #(.RESET_PC(BOOT_ROM_BASE)) nyuzi(
    .interrupt_req({11'd0,
        frame_interrupt,      // VGA 垂直同步
        ps2_rx_interrupt,     // PS/2 有键
        uart_rx_interrupt,    // UART 收到字节
        timer_interrupt,      // 定时器到点
        1'b0}),
    .axi_bus(axi_bus_m[0]),   // Nyuzi 是 AXI 主设备 0
    .io_bus(nyuzi_io_bus),    // 外设寄存器走 IO 总线
    .jtag(jtag),
    .*);
```

见 [de2_115_top.sv:111-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L111-L121)。注意 `interrupt_req` 把四个外设的中断线拼成低位向量，对应 u7-l2 讲过的逐源中断机制。

AXI 互连把两组主从接口连起来，`M1_BASE_ADDRESS` 用 Boot ROM 基地址作为两个从设备的分界：

```systemverilog
axi_interconnect #(.M1_BASE_ADDRESS(BOOT_ROM_BASE)) axi_interconnect(
    .axi_bus_s(axi_bus_s),
    .axi_bus_m(axi_bus_m),
    .*);
```

见 [de2_115_top.sv:123-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L123-L126)。

两个 AXI 从设备分别是 Boot ROM（只读）和 SDRAM 控制器：

```systemverilog
axi_rom #(.FILENAME(bootrom)) boot_rom(
    .axi_bus(axi_bus_s[1]),     // ROM 接在从口 1
    .*);
```

见 [de2_115_top.sv:140-142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L140-L142)。`bootrom` 参数默认指向 `software/bootrom/boot.hex`，相对综合工具的调用目录解析。

```systemverilog
sdram_controller #(
    .DATA_WIDTH(32), .ROW_ADDR_WIDTH(13), .COL_ADDR_WIDTH(10),
    .T_REFRESH(390), .T_POWERUP(10000), ...   // 基于 DE2-115 上 SDRAM 颗粒时序
) sdram_controller(
    .axi_bus(axi_bus_s[0]),     // SDRAM 接在从口 0
    .*);
```

见 [de2_115_top.sv:144-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L144-L160)。SDRAM 控制器把 AXI 事务翻译成 SDRAM 颗粒的行/列/_bank 命令时序，参数都按板载 A3V64S40ETP 颗粒的数据手册填好。

VGA 控制器是一个特殊的「既是 IO 总线从设备（配寄存器），又是 AXI 主设备（DMA 读帧缓冲）」的双角色模块：

```systemverilog
vga_controller #(.BASE_ADDRESS('h180)) vga_controller(
    .io_bus(peripheral_io_bus[IO_VGA]),  // 寄存器配置走 IO 总线
    .axi_bus(axi_bus_m[1]),              // DMA 读帧缓冲走 AXI，是主设备 1
    .*);
```

见 [de2_115_top.sv:165-168](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L165-L168)。这一点与 `vga_controller.sv` 开头的注释一致——「This is an AXI master that DMAs color data from a memory framebuffer」，见 [vga_controller.sv:22-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L22-L24)。

其余外设（UART、SPI/SD 卡、PS/2、定时器）只接 IO 总线，不参与 AXI：

```systemverilog
uart          #(.BASE_ADDRESS('h40)) uart (.io_bus(peripheral_io_bus[IO_UART]), ...);
spi_controller #(.BASE_ADDRESS('hc0)) spi_controller (.io_bus(peripheral_io_bus[IO_SDCARD]), ...);
ps2_controller #(.BASE_ADDRESS('h80)) ps2_controller (.io_bus(peripheral_io_bus[IO_PS2]), ...);
timer         #(.BASE_ADDRESS('h240)) timer (.io_bus(peripheral_io_bus[IO_TIMER]), ...);
```

见 [de2_115_top.sv:191-212](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L191-L212)。注意每个外设的 `BASE_ADDRESS` 决定了它在 IO 地址空间里的位置。

最后看 IO 总线如何分发。顶层用一段 `casez` 把 IO 总线地址的低字节译码成「外设选择枚举」，再通过 `generate` 把同一组读写信号广播给所有外设，并用多路选择器收回读数据：

```systemverilog
casez (nyuzi_io_bus.address)
    'h4?: io_bus_source <= IO_UART;     // 0x4x → UART
    'hc?: io_bus_source <= IO_SDCARD;   // 0xcx → SD 卡
    'h8?: io_bus_source <= IO_PS2;      // 0x8x → PS/2
    default: io_bus_source <= IO_UART;
endcase
...
assign nyuzi_io_bus.read_data = peripheral_read_data[io_bus_source];
```

见 [de2_115_top.sv:239-248](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L239-L248) 与 [de2_115_top.sv:250-260](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L250-L260)。

此外，LED 与七段数码管并没有做成独立控制器，而是直接在顶层用 IO 总线写寄存器点亮：

```systemverilog
case (nyuzi_io_bus.address)
    'h00: red_led   <= nyuzi_io_bus.write_data[17:0];
    'h04: green_led <= nyuzi_io_bus.write_data[8:0];
    'h08: hex0 <= nyuzi_io_bus.write_data[6:0];
    ...
endcase
```

见 [de2_115_top.sv:227-237](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L227-L237)。

#### 4.1.4 代码实践

**实践目标**：把 `de2_115_top.sv` 读成一张实例化清单，亲手把「模块名 → 总线角色 → 接哪个接口」对应起来。

**操作步骤**：

1. 打开 [de2_115_top.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv)，从第 111 行起到第 212 行，列出所有被实例化的模块。
2. 对每个模块标注它连接的接口：`axi_bus_s[?]`、`axi_bus_m[?]`、`peripheral_io_bus[?]`，或者「无总线（纯引脚）」。
3. 特别找出**两个 AXI 主设备**和**两个 AXI 从设备**分别是谁。

**需要观察的现象**：你会发现 Nyuzi 与 VGA 是两个主设备，SDRAM 与 Boot ROM 是两个从设备；UART/SPI/PS2/Timer 完全不出现在 AXI 阵列里，只挂在 IO 总线上。

**预期结果**（可对照核对）：

| 模块 | AXI 角色 | IO 总线角色 |
| --- | --- | --- |
| `nyuzi` | 主设备 0 (`axi_bus_m[0]`) | 主（`nyuzi_io_bus`） |
| `vga_controller` | 主设备 1 (`axi_bus_m[1]`) | 从 (`IO_VGA`) |
| `sdram_controller` | 从设备 0 (`axi_bus_s[0]`) | — |
| `boot_rom` (axi_rom) | 从设备 1 (`axi_bus_s[1]`) | — |
| `uart` / `spi` / `ps2` / `timer` | — | 从 (`IO_UART`/`IO_SDCARD`/`IO_PS2`/`IO_TIMER`) |

#### 4.1.5 小练习与答案

**练习 1**：为什么 VGA 控制器要做成 AXI **主**设备，而 UART 只是 IO 总线**从**设备？

**参考答案**：VGA 需要每秒数百万次地把帧缓冲像素搬到 DAC，这种大批量、由硬件主动发起的搬运适合用 AXI 突发 DMA，所以它是主设备（自己去 SDRAM 取数据）。UART 只需要 CPU 偶尔写一个字节、读一个状态位，是典型的「寄存器级」访问，用简单的 IO 总线即可，没必要走 AXI。

**练习 2**：`de2_115_top.sv:239-244` 的 `casez` 只显式覆盖了 UART / SDCARD / PS2 三类，没有 VGA（`'h1??`）和 timer（`'h2??`）的分支。这对读这两个外设寄存器有什么影响？

**参考答案**：读多路选择器 `peripheral_read_data[io_bus_source]` 的 `io_bus_source` 会落入 `default: IO_UART`，于是读 VGA / timer 寄存器时返回的其实是 UART 的读数据。这说明在当前设计里，VGA 与 timer 的寄存器主要被当作「写配置」用，其读路径并未真正接通——读这两个外设的状态是不可靠的。

---

### 4.2 AXI 互连：axi_interconnect

#### 4.2.1 概念说明

`axi_interconnect`（[axi_interconnect.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv)）是连接「多个主设备」与「多个从设备」的**总线矩阵**。它的核心使命是：**根据事务的地址，把它路由到正确的从设备，并在多个主设备争用时做仲裁**。

文件开头的注释说得很直白——「在两个主设备和映射到不同地址区间的两个从设备之间路由 AXI 事务」，见 [axi_interconnect.sv:21-26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L21-L26)。

注意这是一个**手写的、极简的**互连，只支持 2 主 2 从，而不是通用 IP。作者在注释里也标注了「XXX this should be reworked to support an arbitrary number of controlling masters」，所以读它时要关注的是「地址译码 + 仲裁」的设计模式，而非把它当成可扩展产品。

#### 4.2.2 核心流程

互连把地址空间沿 `M1_BASE_ADDRESS` 一刀切成两段：

- **从口 0（SDRAM）**：地址 `0x00000000` ~ `M1_BASE_ADDRESS - 1`
- **从口 1（Boot ROM）**：地址 `M1_BASE_ADDRESS` ~ `0xfffeffff`

见 [axi_interconnect.sv:33-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L33-L37)。

路由判定很简单——**比较地址是否 ≥ `M1_BASE_ADDRESS`**：

```
读/写地址 >= M1_BASE_ADDRESS  →  从口 1（ROM 区）
读/写地址 <  M1_BASE_ADDRESS  →  从口 0（SDRAM 区）
```

读通道还要在两个主设备间仲裁，规则是 **从口 1（即 VGA 的 DMA 读）优先**：若 VGA 主设备和 Nyuzi 主设备同时发起读，先服务 VGA。

互连内部用一个三态状态机（[axi_interconnect.sv:43-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L43-L47)）把一次突发事务切成三步：

1. `STATE_ARBITRATE`：采样主设备的请求，锁存地址、长度、目标从设备，进入下一态。
2. `STATE_ISSUE_ADDRESS`：把地址 + 突发长度发给选中的从设备，等从设备 `arready/awready`。
3. `STATE_ACTIVE_BURST`：逐拍搬运数据，每拍 `length` 减一，减到 0 回到 `ARBITRATE`。

这里有一个关键的地址翻译细节：**送给从口 1 的地址要减去 `M1_BASE_ADDRESS`**，让 ROM 看到的是从 0 开始的本地偏移，而不是全物理地址。SDRAM（从口 0）则原样接收地址。

#### 4.2.3 源码精读

模块参数和两组接口声明：

```systemverilog
module axi_interconnect
    #(parameter M1_BASE_ADDRESS = 32'hffffeee0)
    (input clk, input reset,
     axi4_interface.master axi_bus_s[1:0],   // 去驱动外部从设备
     axi4_interface.slave  axi_bus_m[1:0]);  // 被外部主设备驱动
```

见 [axi_interconnect.sv:28-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L28-L41)。`M1_BASE_ADDRESS` 的默认值是 `0xffffeee0`，但在 `de2_115_top` 里被覆盖成 `BOOT_ROM_BASE = 0xfffee000`。

写通道的目标从设备由地址比较决定，并把「从口 1 要减基地址」体现得明明白白：

```systemverilog
assign axi_bus_s[0].m_awaddr = write_burst_address;
assign axi_bus_s[1].m_awaddr = write_burst_address - M1_BASE_ADDRESS;  // 本地偏移
...
assign axi_bus_s[0].m_awvalid = write_slave_select == 0 && write_state == STATE_ISSUE_ADDRESS;
assign axi_bus_s[1].m_awvalid = write_slave_select == 1 && write_state == STATE_ISSUE_ADDRESS;
```

见 [axi_interconnect.sv:70-82](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L70-L82)。`write_slave_select` 在状态机里被赋值：

```systemverilog
else if (axi_bus_m[0].m_awvalid)
begin   // 开始一次新的写事务
    write_slave_select <= axi_bus_m[0].m_awaddr >= M1_BASE_ADDRESS;  // 地址译码
    write_burst_address <= axi_bus_m[0].m_awaddr;
    write_burst_length  <= axi_bus_m[0].m_awlen;
    write_state <= STATE_ISSUE_ADDRESS;
end
```

见 [axi_interconnect.sv:112-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L112-L119)。注意写通道只服务主设备 0（Nyuzi），因为只有 CPU 会写主存；VGA 只读不写。

读通道的仲裁体现「从口 1（VGA）优先」——状态机先看 `axi_bus_m[1].m_arvalid`：

```systemverilog
else if (axi_bus_m[1].m_arvalid)
begin   // VGA 的 DMA 读优先
    read_state <= STATE_ISSUE_ADDRESS;
    read_burst_address <= axi_bus_m[1].m_araddr;
    read_burst_length  <= axi_bus_m[1].m_arlen;
    read_selected_master <= 1'b1;
    read_selected_slave  <= axi_bus_m[1].m_araddr >= M1_BASE_ADDRESS;
end
else if (axi_bus_m[0].m_arvalid)
begin   // Nyuzi 的读
    ...
    read_selected_slave <= axi_bus_m[0].m_araddr[31:28] != 0;  // 最高 nibble 非零 → ROM
end
```

见 [axi_interconnect.sv:181-198](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L181-L198)。这里有个细节：Nyuzi 读时，从设备选择用的是「地址最高 4 位是否非零」（`araddr[31:28] != 0`），而 VGA 读用的是「是否 ≥ `M1_BASE_ADDRESS`」。两者在 DE2-115 的实际地址布局下结论一致（SDRAM 在 `0x0xxxxxxx`，ROM 在 `0xfxxxxxxx`），但写法不同，读源码时要留意。

读地址同样对从口 1 减去基地址：

```systemverilog
assign axi_bus_s[0].m_araddr = read_burst_address;
assign axi_bus_s[1].m_araddr = read_burst_address - M1_BASE_ADDRESS;
```

见 [axi_interconnect.sv:234-235](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L234-L235)。

最后注意 [axi_interconnect.sv:242-246](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L242-L246) 一段注释：作者承认把 `read_burst_length` 在突发后期「复用」来干别的事，靠的是「此刻从设备会忽略 ARLEN」。这是典型的「能跑但脆弱」的简化，读代码时要意识到它对从设备的隐含假设。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「Nyuzi 读 SDRAM」的 AXI 事务在互连内部走了哪条路。

**操作步骤**：

1. 假设 Nyuzi 发起一次读，地址 `0x00001000`，`arlen = 15`（16 拍突发）。在 [axi_interconnect.sv:151-199](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L151-L199) 中找出：此刻 `axi_bus_m[1].m_arvalid` 为 0（VGA 没在读），所以走哪个分支？
2. 算出 `read_selected_master` 和 `read_selected_slave` 的值。注意 `araddr[31:28]` 对 `0x00001000` 等于多少。
3. 跟到 [axi_interconnect.sv:232-235](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L232-L235)：哪个 `axi_bus_s[k].m_arvalid` 会被拉高？`m_araddr` 是 `0x00001000` 还是减过基地址？

**需要观察的现象**：因为地址最高 nibble 是 0，`read_selected_slave = 0`，事务被路由到从口 0（SDRAM），且地址不减基地址。

**预期结果**：`read_selected_master=0`、`read_selected_slave=0`；`axi_bus_s[0].m_arvalid` 拉高，`axi_bus_s[0].m_araddr = 0x00001000`。若把地址换成 `0xfffee000`（ROM），则 `read_selected_slave=1`，`axi_bus_s[1].m_araddr = 0xfffee000 - 0xfffee000 = 0`（本地偏移 0）。两条路径都应能自洽。

#### 4.2.5 小练习与答案

**练习 1**：互连的状态机为什么在 `STATE_ACTIVE_BURST` 里要每拍把 `length` 减 1，并判断 `length == 0`？

**参考答案**：AXI 突发由 `arlen/awlen + 1` 拍组成。互连必须知道一拍数据搬完没有——逐拍减 1 直到 0，才知道整次突发结束、可以回到 `STATE_ARBITRATE` 接受下一个事务。否则它无法判断何时释放总线。

**练习 2**：注释 [axi_interconnect.sv:62-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L62-L67) 说「只有从口 0 真正做写」，但代码明明按地址译码把写路由到了从口 0 或从口 1。这两者矛盾吗？

**参考答案**：不矛盾。互连代码本身会把写路由到从口 0（SDRAM）或从口 1（ROM 区）；但 Boot ROM 是只读存储器（`axi_rom` 接受了写地址却从不存数据），所以「真正把写数据留下来」的只有从口 0 上的 SDRAM。注释描述的是功能后果，代码描述的是路由行为。

---

### 4.3 地址映射

#### 4.3.1 概念说明

SoC 设计的核心交付物之一是一张**地址映射表（address map）**：它告诉软件「写哪个地址能点亮 LED」「读哪个地址能拿到串口字节」「代码应该加载到哪个地址」。这张表是硬件（`de2_115_top` 的译码逻辑）与软件（`boot.c` 的寄存器宏）之间的一份契约，两边必须对齐。

Nyuzi 的 SoC 地址空间被**两条独立总线**切成了两大区：

- **AXI 区（可缓存主存）**：由 `axi_interconnect` 按 `M1_BASE_ADDRESS` 切成 SDRAM 段与 ROM 段。
- **IO 总线区（不可缓存外设）**：地址高 16 位为 `0xffff`，由 `de2_115_top` 的 `casez` 按低字节进一步分给各外设。

记住这条总纲：**地址决定了请求走哪条总线、落到哪个设备**。

#### 4.3.2 核心流程

一张完整的 DE2-115 地址映射表如下（结合 `de2_115_top.sv` 的参数与译码逻辑整理）：

**AXI 总线（经 Nyuzi 核内 L2 缓存）**

| 地址区间 | 设备 | 互连从口 | 说明 |
| --- | --- | --- | --- |
| `0x00000000` ~ `0xfffedfff` | SDRAM | 从口 0 | 主存，用户程序代码与数据加载于此 |
| `0xfffee000` ~ `0xffffefff` | Boot ROM | 从口 1 | 复位向量所在，串口一级引导；`axi_rom` 内部限 `MAX_SIZE = 0x2000` 字 |

**IO 总线（绕过缓存，地址高位为 `0xffff`）**

| IO 总线地址（低字节） | 全地址 | 设备 | 说明 |
| --- | --- | --- | --- |
| `0x00` / `0x04` / `0x08~0x14` | `0xffff00xx` | LED / 七段数码管 | 顶层直接译码，无独立控制器 |
| `0x40~0x4f` (`'h4?`) | `0xffff004x` | UART | 状态/RX/TX/分频寄存器，`BASE_ADDRESS='h40` |
| `0x80~0x8f` (`'h8?`) | `0xffff008x` | PS/2 | `BASE_ADDRESS='h80` |
| `0xc0~0xcf` (`'hc?`) | `0xffff00cx` | SD 卡（SPI） | `BASE_ADDRESS='hc0` |
| `0x180~` | `0xffff018x` | VGA 寄存器 | `BASE_ADDRESS='h180`（DMA 帧缓冲另走 AXI） |
| `0x240~` | `0xffff024x` | 定时器 | `BASE_ADDRESS='h240` |

需要强调的是：**软件看到的全地址是 `0xffffxxxx`，而 IO 总线上实际承载的就是这个全地址**，`de2_115_top` 的 `casez` 用 `'h4?` 这样的通配只看低字节的高 nibble，等价于屏蔽了 `0xffff` 前缀。

#### 4.3.3 源码精读

先看软件侧的约定。`boot.c` 把外设寄存器统一映射到 `0xffff0000` 起的区间：

```c
static volatile unsigned int * const REGISTERS = (volatile unsigned int*) 0xffff0000;

enum register_index {
    REG_GREEN_LED    = 0x04 / 4,
    REG_UART_STATUS  = 0x40 / 4,
    REG_UART_RX      = 0x44 / 4,
    REG_UART_TX      = 0x48 / 4,
    REG_UART_DIVISOR = 0x4c / 4
};
```

见 [boot.c:29-40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L29-L40)。注意 `REG_UART_TX = 0x48/4`：因为是指向 `unsigned int`（4 字节）的指针，下标按字偏移，所以字节地址 `0x48` 对应 `0x48/4`。于是 `REGISTERS[REG_UART_TX]` 即 `*(0xffff0048)`——这与 u9-l1 中模拟器侧 `REG_SERIAL_OUTPUT = 0xffff0048` 完全一致，证明 FPGA 与模拟器共用同一份外设地址约定。

再看硬件侧的译码。IO 总线接口本身就只有 5 个信号，地址是全 32 位 `scalar_t`：

```systemverilog
interface io_bus_interface;
    logic write_en;
    logic read_en;
    defines::scalar_t address;
    defines::scalar_t write_data;
    defines::scalar_t read_data;
    ...
endinterface
```

见 [defines.svh:416-425](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L416-L425)。

顶层收到这个全地址后，用 `casez` 做外设选择（已在 4.1.3 引用，[de2_115_top.sv:239-244](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L239-L244)），同时把同一组读/写信号广播给所有外设，由各外设用自己的 `BASE_ADDRESS` 自行决定是否响应：

```systemverilog
generate
    for (io_idx = 0; io_idx < NUM_PERIPHERALS; io_idx++)
    begin : io_gen
        assign peripheral_io_bus[io_idx].write_en   = nyuzi_io_bus.write_en;
        assign peripheral_io_bus[io_idx].read_en    = nyuzi_io_bus.read_en;
        assign peripheral_io_bus[io_idx].address    = nyuzi_io_bus.address;
        assign peripheral_io_bus[io_idx].write_data = nyuzi_io_bus.write_data;
        assign peripheral_read_data[io_idx]         = peripheral_io_bus[io_idx].read_data;
    end
endgenerate
```

见 [de2_115_top.sv:250-260](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L250-L260)。这是一种常见的「广播写、选择读」的轻量总线扇出方式：写请求人手一份，由外设内部用地址比较决定是否真的写；读数据则按 `io_bus_source` 选一份回来。

以 UART 为例，它就是靠 `BASE_ADDRESS` 偏移区分自己的四个寄存器：

```systemverilog
localparam STATUS_REG   = BASE_ADDRESS;          // 0x40
localparam RX_REG       = BASE_ADDRESS + 4;      // 0x44
localparam TX_REG       = BASE_ADDRESS + 8;      // 0x48
localparam DIVISOR_REG  = BASE_ADDRESS + 12;     // 0x4c
...
assign tx_en = io_bus.write_en && io_bus.address == TX_REG;
```

见 [uart.sv:35-61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L35-L61)。`0x48` 与上面 `boot.c` 的 `REG_UART_TX` 严格对齐。

最后，AXI 区的「ROM 基地址」同时承担三个角色：它是 `RESET_PC`、是 `BOOT_ROM_BASE`、也是互连的 `M1_BASE_ADDRESS`。三者用同一个常量串起来，见 [de2_115_top.sv:73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L73) 与 [de2_115_top.sv:111-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L111-L126)。改这块地址必须三处一起改。

#### 4.3.4 代码实践

**实践目标**：验证「软件地址约定 ↔ 硬件译码」两边是否真的对得上。

**操作步骤**：

1. 打开 [boot.c:42-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/bootrom/boot.c#L42-L67) 的 `read_serial_byte` / `write_serial_byte`，确认它们读写的是 `0xffff0040`（状态）和 `0xffff0048`（TX）。
2. 打开 [uart.sv:35-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L35-L38)，确认 UART 的 `TX_REG = BASE_ADDRESS + 8 = 0x40 + 8 = 0x48`。
3. 打开 [de2_115_top.sv:191-194](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L191-L194) 与 [de2_115_top.sv:239-244](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L239-L244)，确认 UART 的 `BASE_ADDRESS='h40`，且 `'h4?` 译码会选中 `IO_UART`。

**需要观察的现象**：三个文件里出现的地址 `0x48`、寄存器名 `TX`、外设编号 `IO_UART` 三方一致，形成闭环。

**预期结果**：当 `boot.c` 执行 `REGISTERS[REG_UART_TX] = ch`（即写 `0xffff0048`）时，请求经 Nyuzi IO 总线广播到 UART，UART 用 `address == TX_REG (0x48)` 命中并启动发送。整条链路自洽。若哪一步对不上（例如把 UART 的 `BASE_ADDRESS` 改成 `'h50`），串口输出就会失效——这是很好的故障定位练习。**待本地验证**：若有 DE2-115 实板，可改 `BASE_ADDRESS` 重新综合，观察串口是否还能输出 Boot ROM 的提示。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SDRAM 放在 `0x00000000`，而 Boot ROM 放在 `0xfffee000` 这么高的地址？能不能反过来？

**参考答案**：复位向量 `RESET_PC` 指向 ROM，而 ROM 必须是上电就可读的非易失存储；用户程序要加载进易失的 SDRAM。把 SDRAM 放在低地址、ROM 放在高地址是常见做法，因为：① 用户程序的链接地址常从 0 开始，低地址方便；② 高地址区（`0xffff_xxxx` 附近）天然与 IO 外设的 `0xffff` 区接近，便于把「系统区」集中。反过来并非不可，但要同时改 `RESET_PC`、链接脚本和互连的 `M1_BASE_ADDRESS`，且要避开 IO 区。

**练习 2**：一个软件写 `0x00001000` 与写 `0xffff0048`，在 Nyuzi SoC 内部走的是同一条物理路径吗？

**参考答案**：不是。`0x00001000` 在可缓存区，走的是「核内 L1/L2 → `axi_bus_m[0]` → `axi_interconnect` → 从口 0 → SDRAM 控制器」这条 AXI 路径；`0xffff0048` 因高 16 位为 `0xffff`，被识别为 MMIO，绕过缓存，走「核内 `io_request_queue` → `io_interconnect` → `nyuzi_io_bus` → 顶层 `casez` → UART」这条 IO 总线路径。两条总线物理分离，互不干扰。

**练习 3**：`axi_rom` 用 `$readmemh(FILENAME, rom_data)` 在 `initial` 块里装载数据（[axi_rom.sv:42-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_rom.sv#L42-L46)）。为什么这一步发生在「综合」而不是「运行时」？

**参考答案**：FPGA 的 ROM 要在生成比特流时就把内容「烧死」进查找表/块存储器里。`$readmemh` 在综合时被 Quartus 识别，把 `boot.hex` 的内容固化成 ROM 初值，这样上电瞬间 ROM 里就已经有 Boot ROM 代码，处理器复位即可取指。若在运行时装载，上电时 ROM 是空的，连第一条指令都取不到。

## 5. 综合实践

**任务**：为 `de2_115_top` 这颗 SoC 绘制一张完整的系统框图，并在图上标出地址空间划分。

**要求**：

1. 画出以下模块：`nyuzi`（核）、`axi_interconnect`、`sdram_controller`、`boot_rom`(axi_rom)、`vga_controller`、`uart`、`spi_controller`、`ps2_controller`、`timer`，以及 LED/七段数码管这一组由顶层直接驱动的输出。
2. 用**实线**表示 AXI 连接，并在每条实线上标出 `axi_bus_m[0/1]`、`axi_bus_s[0/1]`；用**虚线**表示 IO 总线连接，标出 `nyuzi_io_bus` 与各 `peripheral_io_bus[*]`。
3. 在每个 AXI 从设备旁标出它的地址区间（SDRAM：`0x0 ~ 0xfffee000`；ROM：`0xfffee000 ~ 0xffffefff`）；在每个 IO 外设旁标出它的 `BASE_ADDRESS` 与 `'h?` 译码模式。
4. 用箭头标出四条中断线（`uart_rx_interrupt`、`ps2_rx_interrupt`、`timer_interrupt`、`frame_interrupt`）汇入 `nyuzi.interrupt_req` 的方向。
5. 特别用标注说明：`vga_controller` 同时出现在 AXI（主，DMA 读帧缓冲）与 IO 总线（从，寄存器配置）两侧。

**如何自检**：画完后，对照 [de2_115_top.sv:111-212](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L111-L212) 的实例化段，逐一确认每个模块的总线角色与图上是否一致；再对照 [axi_interconnect.sv:33-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv#L33-L41) 确认主从编号。这张图就是你后续阅读外设控制器（u14-l2）和串口启动（u14-l3）时的「导航地图」。

## 6. 本讲小结

- `de2_115_top` 是 DE2-115 板级 SoC 顶层，把 Nyuzi 核、AXI 互连、SDRAM/Boot ROM 控制器和五个 IO 外设装配在一起，并把信号引到物理引脚。
- Nyuzi 核伸出两条总线：可缓存主存走 AXI（主设备 0），不可缓存外设寄存器走 IO 总线。
- VGA 控制器身兼二职——既是 AXI 主设备（DMA 读帧缓冲），又是 IO 总线从设备（寄存器配置）；其余外设只挂 IO 总线。
- `axi_interconnect` 是手写的 2 主 2 从总线矩阵，用「地址是否 ≥ `M1_BASE_ADDRESS`」做从设备译码，读通道让 VGA（主设备 1）优先，并用三态状态机推进突发。
- 送给 ROM 从设备的地址会被减去 `M1_BASE_ADDRESS` 转成本地偏移；SDRAM 从设备原样接收地址。
- 整张地址映射是硬件译码与软件宏（`boot.c` 的 `0xffff0048` 等）之间的一份契约，FPGA 与模拟器共用同一约定；`0xffff_xxxx` 走 IO 总线，其余走 AXI。

## 7. 下一步学习建议

- **u14-l2 外设控制器**：本讲只把外设当成「挂在总线上的黑盒」，下一步应打开 `uart.sv`、`vga_controller.sv`、`sdram_controller.sv`、`spi_controller.sv`、`ps2_controller.sv`，看清每个控制器内部如何用寄存器接口与外部时序跟软件对话。
- **u14-l3 串口启动与上板流程**：本讲提到的 Boot ROM 与 `serial_boot` 如何配合把用户程序经串口灌进 SDRAM，将在下一篇展开，并对比 `run_fpga` 与 `run_emulator`/`run_verilator` 的环境差异。
- **回顾 u6-l4**：若对「为什么 `0xffff` 地址会绕过缓存走 IO 总线」还不够清晰，可重读 u6-l4 中 `io_request_queue` 与 `l2_axi_bus_interface` 的分工。
- **动手实验（可选）**：若有 Quartus 与 DE2-115 实板，按 [de2-115/README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/README.md) 走一遍 `make synthesize && make program`，再用 `run_fpga` 跑 `tests/fpga/blinky`，亲眼看到本讲这张框图在硅片上跑起来。
