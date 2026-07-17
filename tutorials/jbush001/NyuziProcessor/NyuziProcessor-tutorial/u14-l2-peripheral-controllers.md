# 外设控制器

> 本讲隶属「FPGA SoC 与外设」单元，承接 [u14-l1 FPGA 测试环境与 AXI 互连](u14-l1-fpga-soc-axi.md)。在上一讲里，我们看到了 Nyuzi SoC 把处理器核、SDRAM、Boot ROM、VGA 通过 AXI 总线挂在一起，并把高 16 位为 `0xffff` 的访问绕过缓存引到 IO 总线。本讲就站在 IO 总线的另一端，逐个拆解那些「挂在 IO 总线或 AXI 总线上」的真实外设控制器：UART、SPI、SDRAM、VGA、PS/2。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清每个外设控制器（UART / SPI / SDRAM / VGA / PS2）对外暴露的**寄存器接口**与内部**数据通路**。
- 理解 SDRAM 控制器如何用状态机驱动 SDR SDRAM 的命令时序、如何做自动刷新与页命中/缺失调度。
- 理解 VGA 控制器如何用 AXI 主口 DMA 帧缓冲、并用一个**软件可编程微序列器**产生视频时序。
- 说清外设如何把事件（收到串口字节、敲了一个键、一帧画完）变成一根**中断线**，再经顶层汇聚进核心的 `interrupt_req`。
- 能动手对照软件驱动源码，写一段与 UART / VGA 交互的代码。

## 2. 前置知识：IO 总线框架与中断汇聚

在逐个看控制器之前，先建立两个贯穿全讲的公共认知。

### 2.1 io_bus_interface：一根极简的寄存器总线

所有挂在外设侧的控制器（UART、SPI、VGA 的控制口、PS/2、Timer）共用同一套极简接口 `io_bus_interface`，它只有 5 根信号：

[hardware/core/defines.svh:L416-L425](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L416-L425)

```systemverilog
interface io_bus_interface;
    logic write_en;
    logic read_en;
    defines::scalar_t address;     // 要访问的寄存器地址（IO 窗口内的偏移）
    defines::scalar_t write_data;  // 主设备写出的数据
    defines::scalar_t read_data;   // 从设备返回的数据
endinterface
```

它的语义是「一次一拍」的寄存器读写：主设备（核心）给出 `address` + `write_en`/`read_en` + `write_data`，从设备（外设）在同一拍或下一拍给出 `read_data`。注意它和 AXI 不同——AXI 有独立的读写地址/数据通道与握手，而 IO 总线是为「外设控制寄存器」这种**零星、低带宽、低延迟**访问量身定制的。

### 2.2 广播写、选择读：多个外设如何共用一根总线

在 DE2-115 顶层 `de2_115_top` 中，5 个外设控制器各持有一个 `io_bus_interface.slave`，但它们**并不**各自单独译码，而是采用 u14-l1 提到的「广播写、选择读」结构：

[hardware/fpga/de2-115/de2_115_top.sv:L250-L260](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L250-L260)

```systemverilog
generate
    for (io_idx = 0; io_idx < NUM_PERIPHERALS; io_idx++) begin : io_gen
        assign peripheral_io_bus[io_idx].write_en   = nyuzi_io_bus.write_en;   // 写：广播给所有外设
        assign peripheral_io_bus[io_idx].read_en    = nyuzi_io_bus.read_en;
        assign peripheral_io_bus[io_idx].address    = nyuzi_io_bus.address;
        assign peripheral_io_bus[io_idx].write_data = nyuzi_io_bus.write_data;
        assign peripheral_read_data[io_idx]         = peripheral_io_bus[io_idx].read_data; // 读：各自收回
    end
endgenerate
```

- **写**：`write_en`/`address`/`write_data` 同时广播给全部外设。每个外设内部用自己的 `BASE_ADDRESS` 比较地址，只有匹配的那一个真正执行写动作。这是一种「去中心化译码」——顶层不集中译码，谁该响应由谁自己判断。
- **读**：把所有外设的 `read_data` 收进数组 `peripheral_read_data[]`，再用一个根据地址算出的选择信号 `io_bus_source` 把对应那一路读数据回送给核心：

[hardware/fpga/de2-115/de2_115_top.sv:L239-L248](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L239-L248)

```systemverilog
casez (nyuzi_io_bus.address)
    'h4?: io_bus_source <= IO_UART;      // 0x40~0x4f
    'hc?: io_bus_source <= IO_SDCARD;    // 0xc0~0xcf
    'h8?: io_bus_source <= IO_PS2;       // 0x80~0x8f
    default: io_bus_source <= IO_UART;
endcase
assign nyuzi_io_bus.read_data = peripheral_read_data[io_bus_source];
```

这条 `casez` 只对 UART / SD / PS2 这三类有可读状态的设备做了精细译码；VGA 与 Timer 的控制寄存器都是**只写**的，其 `read_data` 恒为 0，落到 `default` 也无害。

> **术语提示**：软件侧看到的地址是 `0xffff0000` 起的物理地址（见 [registers.h:L21](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L21)）。核心的 MMIO 通路会剥掉高 16 位的 `0xffff`，把窗口内偏移（如 UART 的 `0x40`、VGA 的 `0x180`）送上 IO 总线，于是上面的 `casez` 只需比较低位即可。

### 2.3 中断汇聚：从外设脉冲到核心 interrupt_req

每个能产生事件的外设都拉出一根中断输出线，在顶层被拼成一个位向量送进 Nyuzi 核心的 `interrupt_req` 端口：

[hardware/fpga/de2-115/de2_115_top.sv:L112-L117](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L112-L117)

```systemverilog
nyuzi #(.RESET_PC(BOOT_ROM_BASE)) nyuzi(
    .interrupt_req({11'd0,
        frame_interrupt,      // bit 4
        ps2_rx_interrupt,     // bit 3
        uart_rx_interrupt,    // bit 2
        timer_interrupt,      // bit 1
        1'b0}),               // bit 0
    ...);
```

这与模拟器侧的中断位定义完全一致（见 [tools/emulator/device.h:L40-L44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.h#L40-L44)）：

| 位 | 宏 | 来源外设 |
|---|---|---|
| 0 | `INT_COSIM` | 协同仿真（仅模拟器） |
| 1 | `INT_TIMER` | Timer |
| 2 | `INT_UART_RX` | UART 收到字节 |
| 3 | `INT_PS2_RX` | PS/2 收到扫描码 |
| 4 | `INT_VGA_FRAME` | VGA 一帧画完 |

这些位再进入核心的 `control_registers` 模块：被逐源屏蔽字 `interrupt_mask` 过滤、汇成 `cr_interrupt_pending`，并在全局使能时，由**解码级把当前指令替换为 `TT_INTERRUPT` 空壳**实现精确中断（机制详见 u7-l2）。注意 **SDRAM 控制器不产生中断**——它是内存后端，只能被总线轮询。

---

## 3. 本讲源码地图

| 文件 | 作用 | 挂在哪条总线 |
|---|---|---|
| `hardware/fpga/common/uart.sv` | UART 串口，TX/RX + 8 项 FIFO + RX 中断 | IO 总线（BASE `0x40`） |
| `hardware/fpga/common/spi_controller.sv` | SPI 主机（mode 0），驱动 SD 卡 | IO 总线（BASE `0xc0`） |
| `hardware/fpga/common/sdram_controller.sv` | SDR SDRAM 控制器，带自动刷新与页调度 | AXI 总线（内存后端） |
| `hardware/fpga/common/vga_controller.sv` | VGA 控制器：AXI 主口 DMA 帧缓冲 + DMA 状态机 | IO 总线（控制）+ AXI 主口（数据） |
| `hardware/fpga/common/vga_sequencer.sv` | VGA 微序列器：软件上传微码产生视频时序 | 由 vga_controller 转发写入 |
| `hardware/fpga/common/ps2_controller.sv` | PS/2 键盘/鼠标接收控制器 + 16 项 FIFO | IO 总线（BASE `0x80`） |
| `hardware/fpga/de2-115/de2_115_top.sv` | 板级顶层：实例化上述外设、地址译码、中断汇聚 | — |
| `software/libs/libos/bare-metal/registers.h` | 软件侧寄存器编址（与硬件 BASE 对齐） | — |
| `software/libs/libos/bare-metal/vga.c` | 软件侧 VGA 驱动：编译微码、设帧缓冲 | — |

## 4. 核心概念与源码讲解

### 4.1 UART 串口控制器

#### 4.1.1 概念说明

UART（通用异步收发器）是 Nyuzi 上**最基础的文本通道**：hello_world 的 `printf` 最终就落在这里（见 u1-l4 / u9-l1）。它用两根线（`uart_tx` 发送、`uart_rx` 接收）按固定波特率逐位传输字符，没有共享时钟，收发双方靠事先约定的「每位占多少个时钟周期」对齐。Nyuzi 的 `uart` 模块还带一个 8 项深的接收 FIFO 和一个 RX 中断输出，让软件不必每收一个字节就被打断一次。

#### 4.1.2 核心流程

- **发送（TX）**：软件向 `TX_REG` 写一个字节 → 触发 `tx_en` → 子模块 `uart_transmit` 按波特率把 8 位 + 起止位逐位推上 `uart_tx`。
- **接收（RX）**：子模块 `uart_receive` 在 `uart_rx` 上检测起始位后逐位采样，拼成字节连同帧错误位送入 `rx_fifo`。
- **状态查询 / 中断**：`rx_interrupt` 在 FIFO 非空时拉高（`INT_UART_RX`）；软件读 `RX_REG` 弹出一个字节，读 `STATUS_REG` 看 `{frame_error, overrun, rx_has_data, tx_ready}`。
- **波特率**：由 `DIVISOR_REG` 设置「每位时钟数」`clocks_per_bit`，复位默认 1（需软件按实际波特率配置）。

#### 4.1.3 源码精读

寄存器布局由 `BASE_ADDRESS` 偏移决定（`BASE_ADDRESS` 在顶层实例化为 `0x40`）：

[hardware/fpga/common/uart.sv:L35-L40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L35-L40)

```systemverilog
localparam STATUS_REG   = BASE_ADDRESS;       // 0x40
localparam RX_REG       = BASE_ADDRESS + 4;   // 0x44
localparam TX_REG       = BASE_ADDRESS + 8;   // 0x48
localparam DIVISOR_REG  = BASE_ADDRESS + 12;  // 0x4c
localparam FIFO_LENGTH  = 8;
```

写 `TX_REG` 即触发一次发送；读 `RX_REG` 弹出 FIFO；中断只要 FIFO 非空就一直有效：

[hardware/fpga/common/uart.sv:L59-L61](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L59-L61)

```systemverilog
assign rx_interrupt = !rx_fifo_empty;          // 有数据待读就拉中断
assign tx_en = io_bus.write_en && io_bus.address == TX_REG;
```

读 `STATUS_REG` 时，低 4 位打包了 4 个状态位：

[hardware/fpga/common/uart.sv:L84-L96](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L84-L96)

```systemverilog
STATUS_REG: begin
    io_bus.read_data[31:4] <= 0;
    io_bus.read_data[3:0]  <= {rx_fifo_frame_error, rx_fifo_overrun, !rx_fifo_empty, tx_ready};
end
```

- bit0 `tx_ready`：发送器空闲，可写下一个字节。
- bit1 `rx_has_data`：FIFO 有数据可读。
- bit2 `overrun`：FIFO 满时又来字节，丢了旧的。
- bit3 `frame_error`：该字节帧格式错（无停止位）。

接收 FIFO 是一个 9 位宽（8 数据 + 1 帧错误位）的 `sync_fifo`，当 FIFO 满（达到 `ALMOST_FULL_THRESHOLD=7`）时再来字符会置 `overrun` 并自动丢弃最旧的一项：

[hardware/fpga/common/uart.sv:L106-L127](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/uart.sv#L106-L127)

```systemverilog
assign rx_fifo_overrun_dq = rx_char_valid && rx_fifo_full;
sync_fifo #(.WIDTH(9), .SIZE(FIFO_LENGTH), .ALMOST_FULL_THRESHOLD(FIFO_LENGTH - 1)) rx_fifo(
    .dequeue_value({rx_fifo_frame_error, rx_fifo_char}),
    .enqueue_en(rx_char_valid),
    .dequeue_en(rx_fifo_read || rx_fifo_overrun_dq), ...);
```

软件侧对应关系（[registers.h:L31-L33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L31-L33)）：`REG_UART_STATUS=0x40/4`、`REG_UART_RX=0x44/4`、`REG_UART_TX=0x48/4`。

#### 4.1.4 代码实践

**目标**：用模拟器验证「写 `REG_UART_TX` → 宿主终端看到字符」这条链路。

1. 构建并运行 `hello_world`：`cmake . && make` 后执行 `bin/run_emulator software/apps/hello_world/hello_world.hex`。
2. 观察宿主终端打印出 `Hello, World`（或类似文本）。模拟器把对 `0xffff0048` 的写转发为 `putc`（见 u8-l2）。
3. 打开 `software/libs/libc/src/stdio.c` 与 `vfprintf.c`，跟踪 `printf → fputc → write_console → _write_uart`，确认终点是对 `REG_UART_TX` 的写。
4. 把 `hello_world.c` 里要打印的字符串改长（例如打印 20 行），观察输出仍然完整——体会 FIFO 与阻塞发送如何配合。

> 如果不在 DE2-115 真板上，UART 行为在模拟器中已可验证；真板上需先用 `DIVISOR_REG` 配波特率。

#### 4.1.5 小练习与答案

**Q1**：软件如何判断「可以写下一个发送字节」而不覆盖正在发送的字节？
答：轮询 `STATUS_REG` 的 bit0 `tx_ready`（=1 表示 `uart_transmit` 空闲），或靠发送相关逻辑；只有 `tx_ready` 为 1 时才写 `TX_REG`。

**Q2**：为什么 `rx_interrupt = !rx_fifo_empty` 而不是「每来一个字节产生一个脉冲」？
答：电平触发让软件在处理中断前可以连续收多个字节（FIFO 缓冲），一次中断把 FIFO 读空即可，减少中断次数；读空后 FIFO 变空，中断自动撤销。

---

### 4.2 SPI 控制器（SD 卡）

#### 4.2.1 概念说明

SPI（串行外设接口）用 4 根线（时钟 `SCLK`、片选 `CS_n`、主出从入 `MOSI`、主入从出 `MISO`）同步串行通信。Nyuzi 的 `spi_controller` **硬编码为主机、mode 0**（时钟空闲低、下降沿移出、上升沿采样），在 DE2-115 上专门用来驱动 SD 卡——SD 卡的上电初始化与块读写都走 SPI 协议（见 u8-l2 模拟器侧的虚拟 SD 设备）。它一次传输 8 位（一个字节），收发同时进行。

#### 4.2.2 核心流程

1. 软件用 `CONTROL_REG` 拉低 `spi_cs_n` 选中从设备。
2. 软件向 `TX_REG` 写一个字节 → 启动一次 8 位传输，`transfer_active` 置 1。
3. 控制器用 `divider_rate` 把系统时钟分频成 `spi_clk`，在下降沿把 MOSI 移出一位、上升沿把 MISO 移入一位（mode 0）。
4. 8 位传完，`transfer_active` 清 0。软件读 `RX_STATUS_REG`（= `!transfer_active`）判断完成，再读 `RX_REG` 取回从设备返回的字节。

#### 4.2.3 源码精读

寄存器布局（顶层 `BASE_ADDRESS = 0xc0`，对应软件 `REG_SD_SPI_*`）：

[hardware/fpga/common/spi_controller.sv:L40-L44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/spi_controller.sv#L40-L44)

```systemverilog
localparam TX_REG        = BASE_ADDRESS;       // 0xc0 写：启动传输
localparam RX_REG        = BASE_ADDRESS + 4;   // 0xc4 读：返回字节
localparam RX_STATUS_REG = BASE_ADDRESS + 8;   // 0xc8 读：!transfer_active
localparam CONTROL_REG   = BASE_ADDRESS + 12;  // 0xcc 写：bit0=spi_cs_n
localparam DIVISOR_REG   = BASE_ADDRESS + 16;  // 0xd0 写：分频值
```

启动一次传输：写 `TX_REG` 把字节载入移位寄存器（最高位 MSB 先放到 `spi_mosi`），并启动 8 位计数：

[hardware/fpga/common/spi_controller.sv:L107-L118](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/spi_controller.sv#L107-L118)

```systemverilog
else if (io_bus.write_en && io_bus.address == TX_REG) begin
    assert(spi_clk == 0);
    transfer_active <= 1;
    transfer_count <= 7;
    divider_countdown <= divider_rate;
    {spi_mosi, mosi_byte} <= {io_bus.write_data[7:0], 1'd0};  // MSB 先出
end
```

传输进行中的 mode 0 时序：下降沿移出 MOSI、上升沿采样 MISO。`divider_countdown` 每拍减 1，到 0 才翻转 `spi_clk`，实现可编程速率：

[hardware/fpga/common/spi_controller.sv:L79-L106](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/spi_controller.sv#L79-L106)

```systemverilog
if (transfer_active) begin
    if (divider_countdown == 0) begin
        divider_countdown <= divider_rate;
        spi_clk <= !spi_clk;
        if (spi_clk) begin                        // 下降沿：移出一位
            if (transfer_count == 0) transfer_active <= 0;
            else begin
                transfer_count <= transfer_count - 3'd1;
                {spi_mosi, mosi_byte} <= {mosi_byte, 1'd0};  // 左移，下一位上 MOSI
            end
        end
        else begin                                // 上升沿：采样 MISO
            miso_byte <= {miso_byte[6:0], spi_miso};
        end
    end
    else divider_countdown <= divider_countdown - 8'd1;
end
```

注意 SPI 是**全双工**：每个时钟周期一边发一位、一边收一位，8 个周期后 `miso_byte` 拼满从设备的应答字节。`CONTROL_REG` 的 bit0 直接驱动片选 `spi_cs_n`（[spi_controller.sv:L73-L74](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/spi_controller.sv#L73-L74)）。

#### 4.2.4 代码实践

**目标**：理解软件如何通过 4 个 SPI 寄存器与 SD 卡「一问一答」。

1. 阅读 [software/libs/libos/bare-metal/registers.h:L36-L40](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L36-L40)，确认 `REG_SD_SPI_WRITE/READ/STATUS/CONTROL` 与硬件 `0xc0/0xc4/0xc8/0xcc` 一一对应。
2. 在模拟器中运行一个使用文件系统的程序（参考 u8-l2：`run_emulator -b disk.img ...`），用 `sdmmc.c` 的逐字节 SPI 驱动模型对照本节源码，说明一次「写字节 → 轮询 RX_STATUS → 读 RX」的往返如何映射到硬件的三拍（载入、移位、完成）。
3. 把 `DIVISOR_REG` 在脑中改为更大值，预测：SPI 时钟变慢、传输单字节耗时变长（用于在慢速 SD 卡初始化阶段降速）。

> 模拟器侧 `tools/emulator/sdmmc.c` 用宿主文件后端仿真 SD，软件侧无需改动即可运行；真板 SPI 时序需对照本节验证。本步以源码阅读为主，**待本地验证**实际块读写。

#### 4.2.5 小练习与答案

**Q1**：为什么控制器要提供独立的 `RX_STATUS_REG`（`!transfer_active`），而不是让软件读 `RX_REG` 来判断完成？
答：读 `RX_REG` 会取走返回字节，是有副作用的操作；而判断「传输是否完成」需要无副作用的查询，所以单独提供 `RX_STATUS_REG`。

**Q2**：mode 0 下，为什么「移出」放在下降沿、「采样」放在上升沿？
答：mode 0 时钟空闲低。下降沿后数据在 MOSI 上稳定半个周期，从设备在紧接着的上升沿采样；主设备同样在上升沿采样 MISO，保证双方都在数据稳定的中点采集，符合 SPI mode 0 约定。

---

### 4.3 SDRAM 控制器

#### 4.3.1 概念说明

SDRAM 控制器是整个 SoC 的**主存后端**：可缓存的内存访问经 AXI 互连同到这里的 AXI 从口，控制器再把它翻译成 SDR SDRAM 芯片要求的命令序列（ACTIVATE / READ / WRITE / PRECHARGE / AUTO REFRESH 等）。SDRAM 的特点是「电容会漏电」，必须周期性刷新；并且访问同一行（page）的连续数据远快于跨行访问。因此控制器做两件关键的事：**自动刷新**与**懒开放的页策略**（把最近用过的行留在各 bank 的打开状态，用到再换）。

#### 4.3.2 核心流程

SDRAM 把存储分成 4 个 bank，每个 bank 有若干行、每行若干列。一次读访问的典型状态机路径：

```
IDLE ──(读请求到来, 该行未开)──> OPEN_ROW(ACTIVATE) ──> CAS_WAIT(发 READ) ──> READ_BURST(8 拍) ──> IDLE
IDLE ──(读请求到来, 该行已开且行号匹配)──> CAS_WAIT ──> READ_BURST ──> IDLE   // page hit
IDLE ──(读请求到来, 该行已开但行号不匹配)──> CLOSE_ROW(PRECHARGE) ──> OPEN_ROW ──> ...  // page miss
```

刷新由一个递减计数器 `refresh_timer_ff` 驱动：数到 0 就在 IDLE 时插入一次 `AUTO_REFRESH`（先把所有打开的 bank precharge 掉）。读写优先级上，**读优先于写**，以免饿死也在读内存的 VGA 控制器；但若已有同地址的写在等，则先写，避免读到旧值。

刷新间隔由 SDRAM 规格决定。对 DE2-115 上的器件，每行需在 64 ms 内刷新一次，共 8192 行，故：

\[ T_{refresh} = \frac{64\,\text{ms}}{8192} \approx 7.8\,\mu\text{s} \]

对应参数 `T_REFRESH = 390` 个 50 MHz 时钟（见顶层实例化 [de2_115_top.sv:L144-L160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L144-L160)，注释 `64 ms / 8192 rows = 7.8125 uS`）。

#### 4.3.3 源码精读

SDRAM 命令编码（4 根控制线 `{cs_n, ras_n, cas_n, we_n}` 的组合）：

[hardware/fpga/common/sdram_controller.sv:L89-L97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L89-L97)

```systemverilog
typedef enum logic[3:0] {
    CMD_MODE_REGISTER_SET = 4'b0000,
    CMD_AUTO_REFRESH      = 4'b0001,
    CMD_PRECHARGE         = 4'b0010,
    CMD_ACTIVATE          = 4'b0011,
    CMD_WRITE             = 4'b0100,
    CMD_READ              = 4'b0101,
    CMD_NOP               = 4'b1000
} sdram_cmd_t;
```

懒页策略的核心：为每个 bank 记住「当前打开的行号」和「是否有行打开」，在 IDLE 态据此分三类处理一次读请求：

[hardware/fpga/common/sdram_controller.sv:L266-L291](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L266-L291)

```systemverilog
if (!bank_active[read_bank]) begin
    perf_dram_page_miss = 1;
    state_nxt = STATE_OPEN_ROW;            // 该 bank 无行打开：先 ACTIVATE
end
else if (read_row != active_row[read_bank]) begin
    perf_dram_page_miss = 1;
    state_nxt = STATE_CLOSE_ROW;           // 打开的是别的行：先 PRECHARGE 再 ACTIVATE
end
else begin
    perf_dram_page_hit = 1;
    state_nxt = STATE_CAS_WAIT;            // 行正好命中：直接发 READ
end
```

`perf_dram_page_hit/miss` 是给性能计数用的脉冲（接到顶层，可用于剖析 SDRAM 效率）。

刷新调度：`refresh_timer_ff` 每拍减 1，到 0 时在 IDLE 触发刷新；若此时有 bank 开着，先 precharge 全部 bank：

[hardware/fpga/common/sdram_controller.sv:L201-L204](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L201-L204) 与 [L256-L264](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L256-L264)

```systemverilog
if (refresh_timer_ff != 0) refresh_timer_nxt = refresh_timer_ff - TIMER_WIDTH'(1);
...
STATE_IDLE: begin
    if (refresh_timer_ff == 0) begin
        if (bank_active[0] | bank_active[1] | bank_active[2] | bank_active[3])
            state_nxt = STATE_AUTO_REFRESH0;   // 有行打开：先全部 precharge
        else
            state_nxt = STATE_AUTO_REFRESH1;   // 无行打开：直接刷新
    end
    ...
```

为避免 SDRAM 突发（8 拍）与 AXI 总线速率脱节，控制器用两个 FIFO 各缓冲一次完整突发：`load_fifo` 缓存读回数据、`store_fifo` 缓存待写数据（[sdram_controller.sv:L142-L166](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L142-L166)）。上电时还有一段硬件初始化序列（precharge all → 两次 auto refresh → 设模式寄存器 CAS=2），见状态 `STATE_INIT0..3`（[L218-L252](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L218-L252)），软件无需参与。

#### 4.3.4 代码实践

**目标**：用源码阅读理解「一次跨行读访问」要走多少状态、为何慢。

1. 对照 [sdram_controller.sv:L73-L87](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/sdram_controller.sv#L73-L87) 的状态枚举，画出一次 **page miss 读** 的状态链：`IDLE → CLOSE_ROW → OPEN_ROW → CAS_WAIT → READ_BURST → IDLE`，统计其中插入了几个等待定时器（`T_ROW_PRECHARGE`、`T_RAS_CAS_DELAY`、`T_CAS_LATENCY`）。
2. 再画一次 **page hit 读**：`IDLE → CAS_WAIT → READ_BURST → IDLE`，体会「连续访问同一行」的加速比。
3. 思考：为什么 L2 缓存按 64 字节缓存行突发取指（u6-l3/u6-l4），能天然利用 SDRAM 的页命中？答：一次突发读 8 个字正好落在同一行内，第二个字起都是 page hit。

> SDRAM 控制器在真板上由综合后的硬件驱动；模拟器不复现其时序（用平坦内存数组代替）。本步以状态机阅读为主，**待本地验证**（需 DE2-115 真板或带 SDRAM 模型的仿真）。

#### 4.3.5 小练习与答案

**Q1**：控制器为什么要在 IDLE 态「读优先于写」？
答：VGA 控制器持续 DMA 读帧缓冲，若写长期优先会把读饿死、导致画面撕裂或停顿。但同地址有写在等时例外（先写避免读到旧脏值），这是注释里点明的防数据竞争处理。

**Q2**：`perf_dram_page_miss` 与 `perf_dram_page_hit` 这两个脉冲是给谁用的？
答：接到顶层（[de2_115_top.sv:L79-L80](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/de2-115/de2_115_top.sv#L79-L80)），可接到逻辑分析仪或性能计数，用来衡量 SDRAM 访问模式的好坏的（命中率高=带宽利用率高）。

---

### 4.4 VGA 控制器与定序器

#### 4.4.1 概念说明

VGA 控制器是 Nyuzi 上**唯一同时扮演两个总线角色**的外设：它既是 **IO 总线从设备**（软件通过 IO 口配置它），又是 **AXI 主设备**（它主动 DMA 读帧缓冲）。它的任务是周期性地把帧缓冲里的像素搬到屏幕，并产生 VGA 显示器需要的行/场同步与消隐时序。设计上它分成两半：

- `vga_controller`：DMA 引擎 + 像素 FIFO + 控制寄存器；
- `vga_sequencer`：一个**软件可编程的微序列器**，软件上传一段「微码」来产生任意分辨率下的同步时序——这是本外设最有意思的设计。

#### 4.4.2 核心流程

**DMA 读帧缓冲**（`vga_controller`）：

```
每帧开始(start_frame)
  → 冲刷像素 FIFO 重新同步
  → 发 AXI 读地址(突发 64 字) → 读数据灌进像素 FIFO
  → 定序器在可见区逐像素从 FIFO 取色 → 送到 DAC
  → 帧缓冲读完后回到等待下一帧
```

**产生时序**（`vga_sequencer`）：软件把一段由 `INITCNT`（载入计数器）和 `LOOP`（计数器减 1、非零则跳转）两种微指令组成的程序写进序列器，它每个像素时钟执行一条微指令，按程序在指定时刻翻转 `vga_hs`/`vga_vs`、置 `in_visible_region`、并在一帧末尾置 `start_frame`。

像素率是输入时钟的一半：DE2-115 主时钟 50 MHz，序列器用 `pixel_en` 二分频得到 25 MHz 像素率（标准 640×480@60 VGA 正好需要 25.175 MHz）。

#### 4.4.3 源码精读

VGA 控制器一身两职的端口（注意同时有 `io_bus.slave` 和 `axi4_interface.master`）：

[hardware/fpga/common/vga_controller.sv:L27-L47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L27-L47)

```systemverilog
module vga_controller #(parameter BASE_ADDRESS = 0)
    (input clk, input reset,
     io_bus_interface.slave io_bus,        // 软件配置口
     output logic frame_interrupt,
     axi4_interface.master axi_bus,         // DMA 读帧缓冲
     output [7:0] vga_r, vga_g, vga_b,      // 到 ADV7123 DAC
     output logic vga_clk, vga_blank_n, vga_hs, vga_vs, vga_sync_n);
```

控制寄存器（写 `BASE_ADDRESS + {0,8,12}`）配置使能与帧缓冲位置：

[hardware/fpga/common/vga_controller.sv:L204-L220](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L204-L220)

```systemverilog
case (io_bus.address)
    BASE_ADDRESS:     sequencer_en    <= io_bus.write_data[0];   // 0x180 序列器总开关
    BASE_ADDRESS + 8: fb_base_address <= io_bus.write_data;      // 0x188 帧缓冲物理基址
    BASE_ADDRESS + 12:fb_length       <= io_bus.write_data;      // 0x18c 帧长（像素数）
endcase
```

（`BASE_ADDRESS + 4` 即 `0x184` 的写入被转发给 `vga_sequencer` 作为微码装载，见模块末尾实例化 [L224-L227](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L224-L227)。）

DMA 用四状态机驱动 AXI 读突发，突发长度 64（刻意是 CPU 缓存行的两倍，保证即便乒乓也有足够带宽）：

[hardware/fpga/common/vga_controller.sv:L49-L59](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L49-L59)

```systemverilog
localparam BURST_LENGTH = 64;
typedef enum { STATE_WAIT_FRAME_START, STATE_WAIT_FIFO_SPACE,
               STATE_ISSUE_ADDR, STATE_BURST_ACTIVE } dma_state_t;
```

像素 FIFO 把 AXI 读回的 32 位字（含一个 RGB 像素，高 8 位 alpha 丢弃）缓冲成 24 位色流，并在每帧开始 `start_frame` 时 flush 以便从欠载（underrun）中恢复同步：

[hardware/fpga/common/vga_controller.sv:L87-L101](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L87-L101)

```systemverilog
sync_fifo #(.WIDTH(24), .SIZE(PIXEL_FIFO_LENGTH),
            .ALMOST_EMPTY_THRESHOLD(PIXEL_FIFO_LENGTH - BURST_LENGTH - 1)) pixel_fifo(
    .flush_en(start_frame),
    .dequeue_value({vga_r, vga_g, vga_b}),
    .enqueue_value(axi_bus.s_rdata[31:8]),
    .enqueue_en(axi_bus.s_rvalid),
    .dequeue_en(pixel_en && in_visible_region && !pixel_fifo_empty));
```

时序由 `vga_sequencer` 产生。它的微指令格式是一条 `uop_t` 结构：操作类型（载入/循环）、选哪个计数器、13 位立即数、以及 4 个同步标志位：

[hardware/fpga/common/vga_sequencer.sv:L47-L62](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_sequencer.sv#L47-L62)

```systemverilog
typedef enum logic { INITCNT, LOOP } instruction_type_t;
typedef struct packed {
    instruction_type_t instruction_type;
    logic counter_select;        // 0=水平计数器, 1=垂直计数器
    counter_t immediate_value;   // 载入值或循环跳转目标
    logic vsync, hsync, frame_done, in_visible_region;  // 本拍的同步信号输出
} uop_t;
```

微指令存在一块 48 项的 `sram_1r1w` 里（[vga_sequencer.sv:L72-L82](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_sequencer.sv#L72-L82)）。序列器每像素时钟取一条、按需翻转同步信号；`start_frame` 在程序计数器回到 0 时拉高（即一帧程序重头开始），同时触发 DMA 与 FIFO 冲刷：

[hardware/fpga/common/vga_sequencer.sv:L84-L92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_sequencer.sv#L84-L92)

```systemverilog
assign vga_vs           = current_uop.vsync && sequencer_en;
assign vga_hs           = current_uop.hsync && sequencer_en;
assign start_frame      = pc == 0 && sequencer_en;
assign in_visible_region= current_uop.in_visible_region && sequencer_en;
assign branch_en        = current_uop.frame_done
                       || (current_uop.instruction_type == LOOP && counter_nxt != 0);
```

软件侧 [vga.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c) 的 `compile_microcode` 把标准 VGA 时序参数（前肩/同步/后肩/可见区）编译成这种微码流，逐条写进 `REG_VGA_MICROCODE`（0x184），最后设 `REG_VGA_BASE`（帧缓冲基址 `0x200000`）、`REG_VGA_LENGTH`、`REG_VGA_ENABLE`：

[software/libs/libos/bare-metal/vga.c:L42-L47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c#L42-L47)

```c
static void emit_op(int opcode, int counter_index, int value) {
    REGISTERS[REG_VGA_MICROCODE] = (opcode << 18) | (counter_index << 17)
                                 | (value << 4) | ucode_sync_flags;
    ucode_emit_pc++;
}
```

帧中断：`frame_interrupt = start_frame`（[vga_controller.sv:L78](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L78)），每帧产生一次 `INT_VGA_FRAME`，软件可借此做垂直同步（避免撕裂）。

#### 4.4.4 代码实践

**目标**：跑通一个有图形输出的程序，看清「软件配置 → DMA → 时序 → 成像」全链路。

1. 构建并运行 `colorbars` 或 `sceneview`：`bin/run_emulator software/apps/colorbars/colorbars.hex`（模拟器会用 SDL 把帧缓冲贴成窗口，见 u8-l2）。
2. 在 `colorbars` 源码里找到对 `init_vga(VGA_MODE_640x480)` 的调用，跟踪进 [vga.c:L90-L130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c#L90-L130)，确认它依次写入了 `REG_VGA_ENABLE=0`（关序列器以装微码）→ 一串 `REG_VGA_MICROCODE` → `REG_VGA_BASE/LENGTH` → `REG_VGA_ENABLE=1`。
3. 解释成像：`init_vga` 返回帧缓冲指针 `0x200000`，程序往这块内存写 RGB 像素 → `vga_controller` 的 AXI 主口把它 DMA 出来 → 经 `vga_sequencer` 的时序扫描送到 DAC/模拟器窗口。
4. 把 `init_vga` 换成 `VGA_MODE_640x400`，预测分辨率变化（垂直参数不同），观察窗口高度变化。

> 模拟器不复现 VGA 微码时序（直接按 `REG_VGA_BASE/LENGTH` 贴图），但真板上微码决定能否正确点亮显示器。模拟器中可验证帧缓冲写法，**待本地验证**真板时序。

#### 4.4.5 小练习与答案

**Q1**：为什么 VGA 控制器要做成 AXI **主**设备，而不是像 UART 那样挂在 IO 总线上？
答：显示需要持续、高带宽地读帧缓冲（每秒上千万像素），IO 总线是「核心主动、低带宽」的寄存器总线；让 VGA 自己当 AXI 主设备 DMA，既不占用核心时间，又能用突发获得 SDRAM 带宽。

**Q2**：`start_frame` 同时被用作「DMA 重新开始」和「帧中断」，为什么还要在此时冲刷像素 FIFO？
答：若上一帧 DMA 速度跟不上、FIFO 欠载，残留数据会错位；在每帧开始的场消隐期冲刷 FIFO，可让新一帧从头对齐，是一种自愈机制（源码注释 [vga_controller.sv:L83-L86](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/vga_controller.sv#L83-L86)）。

---

### 4.5 PS/2 控制器

#### 4.5.1 概念说明

PS/2 是老式键盘/鼠标接口，用两根线 `ps2_clk`（设备驱动的时钟）和 `ps2_data`（串行数据）。设备每发一个字节，会自己产生 11 个时钟周期，按「起始位 + 8 数据位 + 奇偶校验 + 停止位」的帧格式逐位送出。Nyuzi 的 `ps2_controller` **只支持接收**，用一个 4 状态机在 `ps2_clk` 的下降沿逐位拼装字节，存进 16 项 FIFO，并在 FIFO 非空时产生 `INT_PS2_RX` 中断。由于 `ps2_clk/data` 是异步的，入口必须先做两级同步。

#### 4.5.2 核心流程

```
ps2_clk 下降沿采样 ps2_data：
  WAIT_START  ──(data=0 起始位)──> READ_CHARACTER
  READ_CHARACTER ──(收满 8 位)──> READ_PARITY
  READ_PARITY ──(跳过, 不校验)──> READ_STOP_BIT
  READ_STOP_BIT ──(字节入 FIFO)──> WAIT_START
```

数据位低位先收（`{receive_byte[6:0], ps2_data}` 左移装配）。FIFO 近满时会自动丢弃最旧扫描码，保证最新输入不丢。

#### 4.5.3 源码精读

寄存器（顶层 `BASE_ADDRESS = 0x80`，软件 `REG_KB_STATUS/REG_KB_SCANCODE`）：

[hardware/fpga/common/ps2_controller.sv:L37-L38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L37-L38)

```systemverilog
localparam STATUS_REG = BASE_ADDRESS;      // 0x80: !fifo_empty
localparam DATA_REG   = BASE_ADDRESS + 4;  // 0x84: 弹出一个扫描码
```

异步信号先经两级 `synchronizer` 同步到系统时钟域（复位态为全 1，因 PS/2 空闲为高）：

[hardware/fpga/common/ps2_controller.sv:L61-L64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L61-L64)

```systemverilog
synchronizer #(.WIDTH(2), .RESET_STATE(2'b11)) input_synchronizer(
    .data_i({ps2_clk, ps2_data}),
    .data_o({ps2_clk_sync, ps2_data_sync}), .*);
```

下降沿检测（`ps2_clk_sync` 由 1 变 0）触发状态转移，逐位装配字节（LSB first）：

[hardware/fpga/common/ps2_controller.sv:L100-L137](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L100-L137)

```systemverilog
if (ps2_clk_sync == 0 && ps2_clk_prev == 1) begin   // 下降沿：数据有效
    case (state_ff)
        STATE_WAIT_START: if (ps2_data_sync == 0) begin state_ff <= STATE_READ_CHARACTER; ... end
        STATE_READ_CHARACTER: begin
            bit_count <= bit_count + 3'd1;
            if (bit_count == 7) state_ff <= STATE_READ_PARITY;
            receive_byte <= {ps2_data_sync, receive_byte[7:1]};  // 低位先收
        end
        STATE_READ_PARITY: state_ff <= STATE_READ_STOP_BIT;       // XXX 不校验奇偶
        STATE_READ_STOP_BIT: begin state_ff <= STATE_WAIT_START; enqueue_en <= 1; end
    endcase
end
```

注意两处 `XXX` 注释：**奇偶校验位和停止位都不检查**（[L122-L135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L122-L135)），简化实现。FIFO 近满丢弃最旧项，与 UART 同理：

[hardware/fpga/common/ps2_controller.sv:L70-L80](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L70-L80)

```systemverilog
sync_fifo #(.WIDTH(8), .SIZE(FIFO_LENGTH), .ALMOST_FULL_THRESHOLD(FIFO_LENGTH - 1)) input_fifo(
    .enqueue_en(enqueue_en),
    .enqueue_value(receive_byte),
    .dequeue_en((io_bus.read_en && io_bus.address == DATA_REG && !read_fifo_empty) || fifo_almost_full),
    ...);
```

#### 4.5.4 代码实践

**目标**：理解键盘扫描码从「线上的位」到「软件读到的一个字节」的完整路径。

1. 对照 [ps2_controller.sv:L41-L46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/ps2_controller.sv#L41-L46) 的 4 状态枚举，画出一个 PS/2 字节（11 个 `ps2_clk` 脉冲）的接收时序，标出每个下降沿落入哪个状态。
2. 在模拟器中，PS/2 输入经宿主 stdin 喂入（见 u8-l2，`REG_KEYBOARD_*` 对应 `0x80/0x84`）。运行 `consoletest` 之类读取键盘的程序，敲键观察其通过 `REG_KB_STATUS` 轮询/中断、`REG_KB_SCANCODE` 读出扫描码。
3. 思考：如果改成「校验奇偶、错就丢帧」，需要改动哪个状态的处理？（答：`STATE_READ_PARITY`，校验失败则不 `enqueue`、直接回 `WAIT_START`。）

> 模拟器侧把宿主按键映射成扫描码注入（u8-l2）；真板上 PS/2 时序由本控制器硬件接收。模拟器可验证软件读取逻辑，**待本地验证**真板按键。

#### 4.5.5 小练习与答案

**Q1**：为什么必须在入口对 `ps2_clk/ps2_data` 做两级同步？
答：PS/2 设备用自己的时钟（典型 10~17 kHz）驱动这两根线，与 Nyuzi 系统时钟异步。直接采样会因亚稳态（metastability）读到抖动值；两级触发器同步可把亚稳态概率压到可忽略。

**Q2**：FIFO 近满（`fifo_almost_full`）时同时触发一次出队，是为什么？
答：这是「丢旧保新」策略——键盘输入里最新的击键最有意义，FIFO 满了就主动扔掉最旧的一个扫描码腾位置，保证最近的输入不丢。

---

## 5. 综合实践：用 UART 与 VGA 拼一个「带状态输出的彩屏程序」

把本讲的 UART 与 VGA 两个控制器串起来，完成一个小任务：

**任务**：编写一个裸机程序，它

1. 调用 `init_vga(VGA_MODE_640x480)` 打开显示，拿到帧缓冲指针（`0x200000`）。
2. 把帧缓冲填成一个简单的彩色图案（例如上半红、下半蓝，或按坐标算渐变），确认屏幕出图。
3. 用 `printf`（最终走 `REG_UART_TX`）在串口打印一行 `VGA ready, fb=0x200000` 作为就绪标志。
4. （进阶）启用帧中断（`INT_VGA_FRAME`）：参考 u7-l2 的中断机制，在每帧中断里翻转帧缓冲某一行颜色，实现一个简单的动画，并在串口每秒打印一次帧计数。

**操作步骤**：

1. 参考 `software/apps/colorbars` 的 `CMakeLists.txt` 与 `add_nyuzi_executable` 用法（u1-l4），新建一个 app。
2. 用 [vga.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/vga.c) 的 `init_vga` 初始化显示，把返回指针当 `unsigned int*` 帧缓冲写。
3. 用 libc 的 `printf` 打印就绪信息（链路见 u9-l1）。
4. 用 `bin/run_emulator` 运行，观察 SDL 窗口出图 + 终端打印。

**需要观察的现象**：

- 写帧缓冲 → 模拟器窗口立即出现对应像素；说明 VGA 的 AXI 主口 DMA 与软件写内存在同一物理地址空间（`0x200000`）协同。
- 串口能看到就绪信息；说明 UART TX 路径独立于 VGA，两者并行工作。
- 若实现了帧中断动画，颜色逐帧变化、且不被 `printf` 阻塞。

**预期结果**：一个既有图形输出、又有串口文本反馈的最小程序，把「IO 总线配置外设」与「AXI 总线 DMA 数据」两条总线分工在本讲中具象化。运行细节**待本地验证**。

## 6. 本讲小结

- 所有外设控制器共用极简的 `io_bus_interface`（5 根信号）；顶层用「**广播写、选择读**」让 5 个外设共用一根总线，写由各外设自带 `BASE_ADDRESS` 自译码，读由地址高位多路选择。
- **UART**：4 个寄存器（状态/收/发/分频），8 项 RX FIFO + overrun，`rx_interrupt` 电平触发（FIFO 非空）。
- **SPI**：mode 0 主机，8 位全双工，下降沿移出、上升沿采样，软件靠 `RX_STATUS` 轮询完成；专门驱动 SD 卡。
- **SDRAM**：AXI 从口主存后端，状态机驱动 SDR SDRAM 命令，**自动刷新** + **懒开放页策略**（page hit/miss 计数），读优先于写以防饿死 VGA。
- **VGA**：一身两职——IO 从口配置 + AXI 主口 DMA 帧缓冲；时序由**软件可编程微序列器** `vga_sequencer`（INITCNT/LOOP 两种微指令）产生，`start_frame` 兼作帧中断与 DMA 同步触发。
- **PS/2**：仅接收，下降沿采样、LSB first 装配字节，两级同步防亚稳态，奇偶/停止位不校验，16 项 FIFO 近满丢旧。
- 外设事件经顶层拼成 `interrupt_req` 位向量（timer=1, uart=2, ps2=3, vga frame=4），再交核心 `control_registers` 做屏蔽/挂起/精确中断；SDRAM 不产生中断。

## 7. 下一步学习建议

- 串口启动流程：本讲这些外设（尤其 UART 与 SD）是如何被 `serial_boot` 在上电时驱动起来的，见 [u14-l3 串口启动与上板流程](u14-l3-serial-boot-fpga.md)。
- 回看模拟器侧的设备仿真（[u8-l2](u8-l2-emulator-devices.md)）：对照 `device.c` 如何用宿主代码「假装」是这些外设，理解硬件接口与功能仿真的边界。
- 中断的接收侧：本讲只讲到中断「如何产生」，软件如何 `getcr/setcr` 配置中断使能、写中断处理程序，见 [u7-l2 控制寄存器与中断](u7-l2-control-registers-interrupt.md)。
- 性能剖析：SDRAM 的 `page_hit/miss` 脉冲如何被量化，见 [u11-l2 性能计数器与 profiling](u11-l2-performance-counters.md)。
