# 外设驱动：ADC/DAC/SPI/I2C

## 1. 本讲目标

FPGA 不可能独立工作，它必须和真实的芯片打交道：ADC 采样、DAC 输出、时钟扇出芯片、温度传感器……这些统称「板级外设（board-level peripherals）」。本讲带你走进 Bedrock 的 [`peripheral_drivers/`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers) 子目录，学完之后你应该能够：

1. 说清 Bedrock 外设驱动的**统一设计哲学**——`passthrough` 模式 + `` `ifndef SIMULATE `` 守卫 + `_sim` 仿真模型——以及它为什么能把「厂家原语（Xilinx IOBUF/ISERDES）」与「可移植控制逻辑」干净分离。
2. 读懂 [`spi_master.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v) 的 SCK 生成、地址+数据移位与**双向 SDIO 读回**机制，并理解在它之上的可编程序列器 [`spi_mon.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v)。
3. 读懂 ADC 驱动 [`ad9653.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v)（源同步 LVDS + ISERDES/IDELAY）与 DAC 驱动 [`ad9781.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9781.v)/[`ad5662.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad5662.v) 如何包装这些原语。
4. 掌握 I2C 桥的**三层架构** [`i2c_chunk.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v) → [`i2c_prog.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v) → [`i2c_bit.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_bit.v)，说清「哪一层把多字节命令序列装配成时序」，以及 Python 装配器 [`assem.py`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py) 在其中的位置。

本讲承接 [u7-l1 picorv32 软核 SoC](u7-l1-picorv32-soc.md)：SoC 里 `gateware/*_pack.v` 那些外设包就是本讲这些驱动的「上层封装」，本讲带你下钻到底层引脚时序。

## 2. 前置知识

- **SPI（串行外设接口）**：主从式、4 线（CS/SCK/MOSI/MISO）同步串行总线，常用于配置 ADC/DAC/时钟芯片的寄存器。有些芯片把 MOSI/MISO 合并成一根双向 **SDIO**，靠方向控制分时复用。
- **I2C / TWI（两线接口）**：两根线 SCL（时钟）+ SDA（数据），开漏（open-drain），每个字节后从机拉低 SDA 回一个 ACK。严格 I2C 还要支持 clock stretching、多主，Bedrock 故意不实现，因此自谦地称为 TWI。
- **LVDS 与源同步（source-synchronous）**：高速 ADC/DAC 用 LVDS 差分对传数据，并随路给一颗数据时钟（DCO）和帧时钟（FCO），FPGA 用 ISERDES 把串行位串行转并行，用 IDELAY 对齐各通道延时。
- **`ifdef / ifndef` 条件编译**：Verilog 的预处理宏。本讲反复出现的 `` `ifndef SIMULATE `` 表示「这段含厂家原语的代码在仿真时跳过」，从而让纯 iverilog 仿真也能跑。
- **localbus**：Bedrock 自研的轻量片上总线（见 [u2-l2](u2-l2-localbus.md)），本讲里的 I2C 桥、SPI 监控都把它当作主机访问接口。

> 一个贯穿全讲的反直觉点：**「驱动」不等于「时序发生器」**。`spi_master`/`i2c_bit` 才是真正的引脚时序引擎；`ad9653`/`ad9781` 这类文件其实只是「把厂家 IO 原语和芯片引脚连起来」的胶水壳，真正控制芯片的 SPI/I2C 时序来自前者。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`peripheral_drivers/spi_master.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v) | 通用 SPI 主机：生成 SCK、移位发送「地址+数据」、支持双向 SDIO 读回。**本讲 SPI 核心。** |
| [`peripheral_drivers/spi_mon.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v) | 建在 `spi_master` 之上的可编程 SPI 命令序列器（轮询、双缓冲回读）。 |
| [`peripheral_drivers/spi_eater.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_eater.v) | 另一种基于 FIFO 的 SPI 主机实现，作对照。 |
| [`peripheral_drivers/ad9653.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v) | AD9653 双通道 16 位 ADC 的「壳」：源同步 LVDS、ISERDES/IDELAY/MMCM + SPI 配置口。**本讲 ADC 核心。** |
| [`peripheral_drivers/ad9653_sim.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653_sim.v) | AD9653 的 SPI 从机仿真模型（配合 `_tb` 用）。 |
| [`peripheral_drivers/ad9781.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9781.v) | AD9781 14 位 LVDS DAC（DDR 数据、ODDR/OBUFDS）。 |
| [`peripheral_drivers/ad5662.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad5662.v) | AD5662 慢速 SPI DAC（纯可综合、无厂家原语）。 |
| [`peripheral_drivers/lmk01801.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/lmk01801.v) | LMK01801 时钟扇出芯片驱动（SPI passthrough + 差分时钟输入）。 |
| [`peripheral_drivers/i2cbridge/i2c_bit.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_bit.v) | I2C **引脚级**时序引擎：单 bit → SCL/SDA 波形。**本讲 I2C 最底层。** |
| [`peripheral_drivers/i2cbridge/i2c_prog.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v) | I2C **协议级**序列器：解释字节指令、驱动 start/ack/stop 状态机。**本讲 I2C 中间层。** |
| [`peripheral_drivers/i2cbridge/i2c_chunk.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v) | I2C **顶层壳**：localbus 接口、4 KB DPRAM、tick 分频、逻辑分析仪、ping-pong 缓冲。 |
| [`peripheral_drivers/i2cbridge/i2c_analyze.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_analyze.v) | 内置小型逻辑分析仪，把 SCL/SDA 跳变抓进内存。 |
| [`peripheral_drivers/i2cbridge/assem.py`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py) | Python「汇编器」：把人话意图翻译成 i2c_prog 的字节指令流。 |
| [`peripheral_drivers/i2cbridge/README.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md) | I2C 桥的权威文档：目标、接口、内存图、指令编码。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲贯穿全家族的「壳」哲学（4.1），再分别精读 SPI（4.2）、ADC/DAC（4.3）、分层 I2C（4.4）。

### 4.1 外设驱动的统一哲学：passthrough、SIMULATE 守卫与 _sim 模型

#### 4.1.1 概念说明

打开 `peripheral_drivers/` 你会发现一个反复出现的「三件套」模式，几乎每个芯片驱动都长这样：

```verilog
parameter SPIMODE="passthrough";
generate
if (SPIMODE=="passthrough") begin: passthrough
    assign CSB  = csb_in;     // 直接把上层来的信号透传到芯片引脚
    assign SCLK = sclk_in;
    // ...
end
endgenerate
`ifndef SIMULATE
IOBUF IOBUF(.O(sdi), .T(sdio_as_i), .I(sdo), .IO(SDIO));  // 厂家原语
`endif
```

这套写法解决一个核心矛盾：**芯片引脚侧必须用厂家专用原语（Xilinx 的 `IOBUF`/`IBUFDS`/`ISERDES`/`ODDR`…），而控制逻辑（`spi_master`/`i2c_bit`）必须可移植、可用 iverilog 仿真。** Bedrock 的做法是把两者在同一个文件里用宏切开：

- `SPIMODE="passthrough"`：把 SPI 三根控制线（CSB/SCLK/SDIO 方向）从「上层控制逻辑」**透传**到「芯片引脚」。真正的 SPI 时序不在这个文件里，而在 `spi_master.v`。
- `` `ifndef SIMULATE … `endif ``：把所有厂家原语包起来，仿真时不编译，所以 iverilog 不会被 `IOBUF` 这种不可综合/不在仿真库里的东西卡住。
- 配套的 `xxx_sim.v`：仿真模型，扮演真实芯片的 SPI/I2C 从机，让 testbench 能闭环。

#### 4.1.2 核心流程

一个芯片驱动（如 `ad9653.v`）的数据流可以画成：

```
  上层控制逻辑                      本驱动文件（壳）                 真实芯片引脚
  spi_master ──csb_in/sclk_in──▶ [passthrough: assign CSB=csb_in] ──▶ CSB
               ──sdio_as_i────▶ [`ifndef SIMULATE: IOBUF 方向控制]──▶ SDIO(双向)
               ◀─sdo────────── [IOBUF .O 读回] ◀───────────────────  SDIO
                                                   ──D0P/D0N…──▶ [ISERDES/IDELAY] ──▶ dout[8*N-1:0]
```

要点：

1. **透传**让本驱动不关心 SPI 时序细节，只负责「这根线连哪个引脚」。
2. **SIMULATE 守卫**让同一份代码既能上板（用真原语）又能仿真（用推断的 `assign` 或干脆省略）。
3. **方向控制信号**（`sdio_as_i`/`sdio_as_sdo`）从控制逻辑传到 IOBUF 的 `.T()`，决定此刻 SDIO 由谁驱动——这是双向总线的关键。

#### 4.1.3 源码精读

AD9653 的 SPI 透传 + IOBUF：[peripheral_drivers/ad9653.v:67-81](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v#L67-L81)

```verilog
generate
if (SPIMODE=="passthrough") begin: passthrough
    assign CSB  = csb_in;
    assign SCLK = sclk_in;
    if (INFER_IOBUF == 0) begin
`ifndef SIMULATE
        IOBUF IOBUF(.O(sdi), .T(sdio_as_i), .I(sdo), .IO(SDIO));
`endif
    end else begin: no_passthrough
        assign SDIO = sdio_as_i ? 1'bz : sdo;   // 仿真用的推断三态
        assign sdi = SDIO;
    end
end
endgenerate
```

这段同时示范了两个分支：默认走真 `IOBUF`（仅上板），`INFER_IOBUF=1` 时走纯 `assign` 的三态推断（可仿真）。`sdio_as_i` 就是来自 `spi_master` 的方向信号。

再看 AD9781（DAC）的透传：[peripheral_drivers/ad9781.v:49-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9781.v#L49-L57)，以及 LMK01801（时钟芯片）：[peripheral_drivers/lmk01801.v:18-25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/lmk01801.v#L18-L25)——三处写法几乎一字不差，这正是「壳」模式的可复用性。

而 `ad9653_sim.v` 则是「扮演芯片」的另一半：[peripheral_drivers/ad9653_sim.v:14-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653_sim.v#L14-L34)

```verilog
always @(posedge clk) begin          // clk 就是 SCLK，按 SPI 节拍移位
    state <= state+1;
    sr <= {sr[22:0], SDIO_OLM};
    if (state==0) write_mode <= ~SDIO_OLM;   // 第 1 位决定读/写
end
```

它把 SDIO 当移位输入，模仿真实 AD9653 收到命令后在数据相位把 SDIO 切成输出（`drive_sdio`）。注意源码里 `sdio_odata <= ~state[0]^state[2]; // XXX totally bogus`——作者坦白这只是个占位回读模型，**不是真实数据**，仅用于验证接线。

#### 4.1.4 代码实践

**实践目标**：直观感受 `SIMULATE` 宏如何切换「上板原语」与「仿真推断」。

**操作步骤**：

1. 阅读 `ad9653.v` 第 67–81 行，确认 `IOBUF` 被 `` `ifndef SIMULATE `` 包住。
2. 看 [`peripheral_drivers/Makefile`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/Makefile) 第 9–11 行的 `ad9653_lint` 目标，它用了 `-DSIMULATE`：

```makefile
ad9653_lint: ad9653.v
	$(VERILOG) -tnull -DSIMULATE $^ -y $(FPGA_FAMILY_DIR)/iserdes -y $(FPGA_FAMILY_DIR)/xilinx
```

3. 在 `peripheral_drivers/` 下运行 `make ad9653_lint`。

**需要观察的现象**：因为带了 `-DSIMULATE`，所有 `IOBUF/IBUFDS/ISERDES` 原语块被跳过，iverilog `-tnull`（只做 elaborate 不生成可执行体）能顺利完成语法/连接检查，而不报「找不到 IOBUF」。

**预期结果**：命令无错退出（exit 0）。若你试着去掉 `-DSIMULATE` 再跑，会因为找不到厂家原语而报错——这就解释了为什么要加守卫。

> 待本地验证：若本机未装 iverilog 或 `FPGA_FAMILY_DIR` 路径异常，命令可能失败，请先按 [u1-l2](u1-l2-build-and-run.md) 装好依赖。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ad9653.v` 里 SPI 部分用 `generate/if` 而数据通道用 `` `ifndef SIMULATE ``？两者目的相同吗？

> **答案**：不同。`generate if (SPIMODE=="passthrough")` 是给**上板综合**用的参数化分支（可在 `passthrough` 之外预留别的模式）；`` `ifndef SIMULATE `` 是给**仿真**用的编译期宏守卫。前者是设计可选项，后者是「这段上板才有、仿真跳过」的隔离。

**练习 2**：`ad9653_sim.v` 里 `sdio_odata <= ~state[0]^state[2]` 被注释为「totally bogus」，为什么仍能用于测试？

> **答案**：因为它只验证「从机能在正确相位把 SDIO 切成输出、且 master 能在数据相位采到值」这一**连接/时序关系**，不验证数据正确性。testbench（`spi_master_tb` 本身也声明 "Not a self-checking testbench"）只需波形对齐即可判 PASS。

---

### 4.2 SPI 主机 spi_master 与可编程序列器 spi_mon

#### 4.2.1 概念说明

[`spi_master.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v) 是一个参数化的通用 SPI 主机，承担所有 SPI 配置型芯片（ADC/DAC/时钟芯片的寄存器口）的真正时序生成。它的设计要点：

- 一次事务 = `ADDR_WIDTH` 位地址 + `DATA_WIDTH` 位数据（默认 16+8=24 拍 SCK），MSB 先发。
- SCK 半周期由 `TSCKHALF` 个 `clk` 周期组成。
- **支持双向 SDIO**：发地址时主机驱动 SDIO；读操作时，进入数据相位后通过 `sdio_as_sdo` 信号释放线路、采样从机回读。
- 读/写由 `spi_read` 输入决定（testbench 里 `spi_rw = spi_addr[15]`，即用地址最高位区分）。

在它之上，[`spi_mon.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v) 是一个**可编程轮询器**：用一段指令内存描述「要发哪些 SPI 命令、哪些要回读」，循环执行，把回读数据双缓冲供主机读取。这正是 I2C 桥（4.4）思想的 SPI 版。

#### 4.2.2 核心流程

`spi_master` 的一次读事务时序（简化）：

```
spi_start ─┐                                  
           ▼
cs ────────┐                                  
           └───────────────────────────────── (持续 24 拍 SCK 后拉高)
sck ──┐ ┌─┐ ┌─┐ ┌─┐ ... ┌─┐ ┌─┐ ┌─┐          
       └─┘ └─┘ └─┘ └─┘     └─┘ └─┘ └─┘          
sdi/SDIO: |<---- 16 位 addr ---->|<-- 8 位 data -->|
                                  ^               
                       数据相位：读时 sdio_as_sdo=1，
                       释放 SDIO，采样 sdo → spi_rdbk
spi_ready ─────────────────────────────┐ ┌──── (cs 下降沿脉冲)
```

内部状态由几个计数器驱动：

- `tckcnt`：从 `TSCKHALF` 向下数到 0，翻转 `sck_r` → 产生 SCK。
- `sck_cnt`：累计 SCK 边沿数，到 `ADDR_WIDTH+DATA_WIDTH` 表示事务结束，拉低 `cs`。
- `sdi_value`：24 位移位寄存器，事务开始时装载 `{spi_addr, spi_data}`，逐拍左移送出 MSB。
- `sdo_rdbk_sr`：数据相位逐拍采样 `sdo`，事务结束锁存到 `spi_rdbk`。

#### 4.2.3 源码精读

**参数与端口**：[peripheral_drivers/spi_master.v:2-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L2-L24)

```verilog
module spi_master #(
    parameter TSCKHALF=10,                                   // SCK 半周期(clk 数)
    parameter ADDR_WIDTH=16,
    parameter DATA_WIDTH=8,
    parameter SCKCNT_WIDTH = clog2(ADDR_WIDTH+DATA_WIDTH+1),
    parameter TSCKW= clog2(TSCKHALF)+1,
    parameter SCK_RISING_SHIFT=1
) ( /* clk, spi_start, spi_busy, spi_read, spi_addr, spi_data,
      cs, sck, sdi, sdo, sdo_addr, spi_rdbk, spi_ready, sdio_as_sdo */ );
```

注意 `clog2` 是模块内自定义函数（[spi_master.v:26-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L26-L34)），因为部分工具不支持 SystemVerilog 的 `$clog2`。

**SCK/CS 状态机**：[peripheral_drivers/spi_master.v:43-67](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L43-L67)

```verilog
always @(posedge clk) begin
    tckcnt <= (tckcnt==0) ? TSCKHALF : tckcnt-1'b1;          // 半周期计数
    if (tckcnt==0 || (spi_start & ~cs_r)) sck_r <= cs_r ? ~sck_r : 1'b0;
    ...
    if (spi_start & ~spi_start_r)      cs_r <= 1'b1;          // 起始沿拉低 cs
    else if (sck_cnt==ADDR_WIDTH+DATA_WIDTH & ~|tckcnt & ~sck_r)
                                      cs_r <= 1'b0;           // 发完 24 拍释放
end
assign cs = ~cs_r_d;
assign sck = SCK_RISING_SHIFT ? sck_in_cs : ~sck_in_cs;
```

`SCK_RISING_SHIFT` 决定 SCK 的有效沿极性，以适配不同芯片的采样约定。

**地址+数据移位发送**：[peripheral_drivers/spi_master.v:72-80](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L72-L80)

```verilog
always @(posedge clk) begin
    if (cs_r & ~cs_r_d) begin
        sdi_value <= {spi_addr, spi_data};                   // 装载 24 位
    end else if (sck_r & ~sck_r_d & |sck_cnt) begin
        sdi_value <= {sdi_value[ADDR_WIDTH+DATA_WIDTH-2:0], 1'b0}; // 左移
    end
end
assign sdi = sdi_value[ADDR_WIDTH+DATA_WIDTH-1];             // MSB 先出
```

**双向 SDIO 方向切换与回读**（本模块最巧妙处）：[peripheral_drivers/spi_master.v:82-101](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L82-L101)

```verilog
reg [3:0] sr_switch=0;
assign sdio_as_sdo = sr_switch[0];                           // 1 = 正在读,释放 SDIO
always @(posedge clk) begin
    if (~sck_r & sck_r_d)                                    // SCK 下降沿
      if (sck_cnt >= ADDR_WIDTH & sck_cnt <= ADDR_WIDTH+DATA_WIDTH)
        sdo_rdbk_sr <= {sdo_rdbk_sr[DATA_WIDTH-2:0], sdo};   // 采从机回读
end
always @(posedge clk) begin
    if (sck_cnt >= ADDR_WIDTH & sck_cnt <= ADDR_WIDTH+DATA_WIDTH) begin
        if (sck_r_d & ~sck_r) sr_switch <= {sr_switch[2:0], spi_read_r}; // 读时移入 1
    end else sr_switch <= {sr_switch[2:0],1'b0};
end
```

进入数据相位后，若 `spi_read_r` 为 1，`sr_switch` 移入 1，其最低位 `sdio_as_sdo` 变高，告诉外部 IOBUF「停止驱动、让从机说话」；同时在 SCK 下降沿把 `sdo` 串行采进 `sdo_rdbk_sr`。事务结束（cs 下降沿）一次性锁存回读与地址：[spi_master.v:103-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L103-L115)。

**上层序列器 spi_mon**：[peripheral_drivers/spi_mon.v:1-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v#L1-L35) 的注释说清了它的定位——「Simple SPI command sequencer for use with spi_master.v … Based on some of the ideas in i2c_chunk.v」。指令编码为 `OPTIONS | 4×SPI_CMD 字节`，其中 OPTIONS 含 `END/SEL/RNW`：[spi_mon.v:125-163](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v#L125-L163)。它逐字节取指、拼成 32 位 SPI 命令、在 `~spi_busy` 时发给 `spi_master`，回读数据写进**双缓冲 DPRAM**（[spi_mon.v:165-184](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v#L165-L184)），主机永远读到一组自洽的值。

#### 4.2.4 代码实践

**实践目标**：跑通 `spi_master` 的仿真，在波形里看清「地址相位驱动 SDIO、数据相位切方向」。

**操作步骤**：

1. `cd peripheral_drivers && make spi_master_check`（对应 [`Makefile`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/Makefile) 的 `spi_master_check`）。
2. `make spi_master.vcd` 后用 gtkwave 打开（`make spi_master_view` 若装了 gtkwave），或在 testbench 里加 `+vcd`。
3. 对照 [`spi_master_tb.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master_tb.v) 第 26–28 行：`spi_addr=16'h8002; spi_data=8'h18;`，注意 `spi_rw=spi_addr[15]=1`（读操作）。

**需要观察的现象**：

- `cs` 在 `start` 后拉低，持续 24 个 SCK 周期。
- 前 16 拍 `sdi` 依次输出 `0x8002` 的位（最高位 1 先出）。
- 进入数据相位后 `sdio_as_sdo` 抬高（因为 `spi_read=1`）。

**预期结果**：终端打印 `WARNING: Not a self-checking testbench` 然后打印 `PASS`（见 [spi_master_tb.v:28-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master_tb.v#L28-L32)）。这不是自校验测试，需要你在波形里人工确认。

> 待本地验证：波形观察依赖 gtkwave 与图形环境；纯命令行可用 `make spi_master.vcd` 后 `vvp spi_master_tb +vcd` 生成 VCD 再离线分析。

#### 4.2.5 小练习与答案

**练习 1**：若要把 SPI 事务改成「24 位纯数据、无地址」，怎么调？

> **答案**：设 `ADDR_WIDTH=0, DATA_WIDTH=24`。注意 `SCKCNT_WIDTH=clog2(0+24+1)` 仍有效，`sdi_value` 退化为 24 位移位，第 [73-74 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_master.v#L73-L74)装载 `{spi_addr,spi_data}` 会变成纯 `{spi_data}`。

**练习 2**：`spi_mon` 为什么要双缓冲 DPRAM？

> **答案**：SPI 轮询在后台持续刷新结果，主机可能随时读。若读写同缓冲，主机会读到「刷了一半」的混合数据。双缓冲 + 周期末整体翻转（[spi_mon.v:168-174](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_mon.v#L168-L174)）保证主机始终读到上一轮的完整快照——这与 I2C 桥的 `buffer_flip`/`freeze` 思想一致。

---

### 4.3 ADC 驱动 ad9653 与 DAC 驱动 ad9781/ad5662

#### 4.3.1 概念说明

`ad9653.v`（AD9653，双通道 16 位 125 MSPS ADC）是本目录里最复杂的驱动，因为它要处理**源同步 LVDS 数据接收**：ADC 随路给出 DCO（数据时钟）和 FCO（帧时钟），FPGA 必须用 ISERDES 把每对差分线上的多位串行数据还原成并行字，并用 IDELAY 精细对齐每条通道的延时，用 bitslip 做字对齐。整个模块仍是一个「壳」——SPI 配置口走 4.1 的 passthrough，数据口包装一组 `lvds_*` 子模块（位于 `fpga_family/iserdes`）。

对照之下：

- `ad9781.v`（14 位 LVDS DAC）：方向反过来，FPGA→DAC，用 `ODDR` 在 DCO 双沿打 `data_i/data_q`，用 `OBUFDS` 转差分输出。
- `ad5662.v`（16 位慢速 SPI DAC）：**纯可综合、无任何厂家原语**，是一个完整的 24 位 SPI 时序发生器，本身既是「壳」又是「主机」。

三者的复杂度阶梯，正好展示了「驱动」一词在不同芯片上的不同含义。

#### 4.3.2 核心流程

**AD9653 数据接收链路**：

```
ADC 引脚                 ad9653.v 内部                      出口
DCO± ──────────────────▶ lvds_dco: MMCM 产生 clk_div ─▶ clk_div_bufr/bufg
FCO± ──────────────────▶ lvds_frame: 还原帧标志 ──────▶ frameout
D0P/D0N … (每通道8对) ─▶ lvds_iophy: ISERDES+IDELAY ─▶ dout[8*N-1:0]
                         (idelay_ce/idelay_ld/bitslip 由外部 idelay_scanner 驱动)
```

每条 lane 的对齐由外部扫描器（[`idelay_scanner/`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/idelay_scanner) 子目录）动态调整 `idelay_value_in/bitslip`，找到稳定的眼图窗口。

**AD5662 发送链路**（自包含）：

```
send(单拍) → count 从 13 计到 63 → 期间按 tick 节拍移位 24 位 sr → sdo 串行输出
                                      sync_ 在发送窗口拉低，结束拉高
```

#### 4.3.3 源码精读

**AD9653 的差分数据线装配与逐 lane 处理**：[peripheral_drivers/ad9653.v:96-146](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v#L96-L146)

```verilog
wire [DWIDTH-1:0] d_p = ({D1PA,D0PA,D1PB,D0PB,D1PC,D0PC,D1PD,D0PD}); // 差分正端按 lane 重排
wire [DWIDTH-1:0] d_n = ({D1NA,D0NA,D1NB,D0NB,D1NC,D0NC,D1ND,D0ND});
...
generate for (ix=0; ix < DWIDTH; ix=ix+1) begin: in_cell
`ifndef VERILATOR
    assign clk_div[ix] = clk_div_in[BANK_SEL[...]];   // 按 bank 选时钟
    always @(negedge clk_div[ix]) begin
        idelay_ld_div_0[ix] <= idelay_ld[ix];         // 控制/对齐信号跨到 clk_div 域
        bitslip_div_0[ix]   <= bitslip[ix];
        idelay_value_in_r[5*ix+4:5*ix] <= idelay_value_in[5*ix+4:5*ix];
    end
`endif
    lvds_iophy #(.flip_d(FLIP_D[ix])) iophy (
        .d_p(d_p[ix]), .d_n(d_n[ix]), .dout(dout[8*ix+7:8*ix]),
        .clk_div(clk_div[ix]), .dco_clk(dco_clk[ix]),
        .idelay_value_out(...), .idelay_value_in(...),
        .iserdes_reset(reset[ix]), .idelay_ld(idelay_ld_div[ix]),
        .idelay_ce(idelay_ce_div[ix]), .bitslip(bitslip_div[ix]), ...);
end endgenerate
```

注意几个设计要点：①每条 lane 独立一个 `lvds_iophy`，由 generate 展开；②`idelay_ld/ce/bitslip` 这些来自慢时钟域的控制信号，先用两级寄存器（`..._div_0/_div_1`）同步到 `clk_div` 域（[ad9653.v:99-127](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v#L99-L127)）再驱动原语，这是典型的 CDC 处理（见 [u4-l1](u4-l1-cdc-basics.md)）；③`FLIP_D[ix]` 允许逐 lane 反相，方便 PCB 互换差分对。

**AD9781 的 DDR 数据发送**：[peripheral_drivers/ad9781.v:70-93](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9781.v#L70-L93)

```verilog
`ifndef SIMULATE
IBUFDS ibuf_dco(.I(...), .O(dco_clk_ds));          // 采 DCO
BUFG   bufg_dco(.I(dco_clk_ds), .O(dco_clk_out));
ODDR   oddr_dci(.C(dco_clk_out), .D1(flip_dci), .D2(~flip_dci), .Q(dci_ddr)); // 产生 DCI
generate for (ix=0;ix<width;ix=ix+1) begin: in_cell
    ODDR  oddr(.C(dco_clk_out), .D1(data_i[ix]), .D2(data_q[ix]), .Q(data_in_buf[ix])); // I/Q 双沿
    OBUFDS obuf_d(.O(d_p[ix]), .OB(d_n[ix]), .I(flip_d[ix] ? ~data_in_buf[ix] : data_in_buf[ix]));
end endgenerate
`endif
```

`ODDR` 在 DCO 的上升沿打 `data_i`、下降沿打 `data_q`，把两路 14 位数据在 DDR 模式下合并到 14 对差分线上——与 [u3-l3](u3-l3-downconvert-upconvert.md) 讲的 DDR DAC 速率翻倍完全对应。

**AD5662 的自包含 SPI 时序**：[peripheral_drivers/ad5662.v:21-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad5662.v#L21-L58)

```verilog
reg [5:0] count=0; always @(posedge clk) begin
    if (send & ~busy) count <= 13;                   // 启动计数
    if (tick & (count != 0)) count <= count+1;       // 由 tick 节拍推进
end
wire running = (14 < count) && (count < 62);
always @(posedge clk) sclk_r <= ~running | ~count[0]; // 仅在 running 区间出 SCK
reg [23:0] sr=0;
always @(posedge clk) begin
    if (send) sr <= {6'b0, ctl, data};                // 24 位：6 空位+2 控制+16 数据
    if (shift) sr <= {sr[22:0], 1'b0};
end
assign sclk = sclk_r; assign sync_ = ~sync_r; assign sdo = sr[23];
```

它没有任何 `` `ifndef SIMULATE ``——因为它只产生 SCK/SYNC/SDO 这种普通逻辑信号，不需要厂家原语，所以「壳」和「主机」合二为一。`ad5662_tb.v` 之所以存在（而很多芯片没有独立 tb），正是因为它自成体系、可独立仿真。

#### 4.3.4 代码实践

**实践目标**：对比「带原语的高性能 DAC」与「纯逻辑慢速 DAC」两种驱动的可仿真性。

**操作步骤**：

1. `cd peripheral_drivers && make ad5662_check`——AD5662 无厂家原语，应能直接仿真。
2. 打开 [`ad5662_tb.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad5662_tb.v) 阅读，确认它给 `data/ctl/sel/send/tick` 喂激励。
3. 尝试 `make ad9781_check`（或 `ad9781_tb`），观察是否报错。

**需要观察的现象**：

- AD5662 仿真应顺利完成，波形里能看到 `sclk` 在 count∈(14,62) 区间翻转、`sync_` 在发送窗口拉低、`sdo` 逐位移出 `{6'b0, ctl, data}`。
- AD9781 因为 `ODDR/IBUFDS/OBUFDS` 被 `` `ifndef SIMULATE `` 守卫，仿真时这些输出悬空（`d_p/d_n` 无驱动），多数情况下 tb 只是「连得上」即可，**不要指望看到真实 LVRS 波形**。

**预期结果**：AD5662 PASS；AD9781 若有独立 tb 也只做连接性检查（待本地验证，取决于本机是否有 `ad9781_check` 目标及 Xilinx 仿真库）。

#### 4.3.5 小练习与答案

**练习 1**：`ad9653.v` 里 `idelay_ld_div_0/_1` 两级寄存器（[第 99-127 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v#L99-L127)）的作用是什么？

> **答案**：把来自慢域（如 localbus/clk）的 `idelay_ld/ce/bitslip` 同步到每条 lane 的 `clk_div` 域，并在第 [105-107 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653.v#L105-L107)做边沿检测（`_0 & ~_1`）形成单拍脉冲，避免跨域亚稳态与多拍误触发。这是 CDC 标准做法。

**练习 2**：为什么 `ad5662` 有独立 testbench，而 `ad9653` 用的是 `ad9653_tb.v` 这种「扮演 ADC 输出」的模型？

> **答案**：`ad5662` 是纯逻辑驱动，可像普通模块一样喂输入看输出；`ad9653` 是接收源同步数据的「壳」，要测它必须有人扮演 ADC 往 DCO/D lanes 上打数据，所以 [`ad9653_tb.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/ad9653_tb.v) 本质是一个「ADC 发送侧模型」，与被测的「接收侧」成对出现。

---

### 4.4 分层 I2C 桥：i2c_bit → i2c_prog → i2c_chunk

#### 4.4.1 概念说明

[`i2cbridge/`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge) 是本讲最值得精读的子系统。它用**三层抽象 + 一个 Python 汇编器**，把「上电后自动给一批 I2C 设备写配置、之后循环轮询状态」这件事做得既小（Spartan-6 仅 197 LUT/FF + 2 块 BRAM，见 [README](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md) 第 115-118 行）又强（支持任意命令序列、双缓冲结果、内置逻辑分析仪）。

三层职责（**这是本讲核心知识点，也是综合实践的考点**）：

| 层 | 文件 | 职责 | 抽象级别 |
|----|------|------|----------|
| 顶层壳 | `i2c_chunk.v` | localbus 接口、4 KB DPRAM、tick 分频、内存分区、ping-pong 缓冲、集成逻辑分析仪 | 「主机看到的内存接口」 |
| 协议序列器 | `i2c_prog.v` | 解释字节指令（opcode+n），跑 I2C 状态机（start/addr/ack/data/stop），生成逐位命令 `bit_cmd` | **字节指令 → 协议时序** |
| 引脚引擎 | `i2c_bit.v` | 把单条 2 位命令（Tx0/Tx1/L/H）变成精确的 SCL/SDA 波形（14 相位/bit） | **位命令 → 电平时序** |
| （软件）汇编器 | `assem.py` | 把「写 0xa5 到设备 0x20」翻译成字节指令流，灌进程序内存 | **人话意图 → 字节指令** |

回到本讲开篇的问题——**「哪一层负责把多字节命令序列装配成时序？」** 答案是分工的：

- **字节序列本身**由 Python 汇编器 `assem.py`（`i2c_assem`/`I2CAssembler` 两个类）在主机侧装配。
- **`i2c_prog`** 是唯一理解这条字节序列编码的硬件层——它逐条取指、解码 opcode、驱动 I2C 协议状态机，把「write n 字节」这类多字节指令映射成 start/地址/ACK/数据/stop 的逐位命令流。**它是「字节→协议时序」的桥梁。**
- **`i2c_bit`** 不懂字节、也不懂协议，它只把每条 2 位命令（发 0/发 1/SCL 停低/SCL 停高）物化成 14 相位的 SCL/SDA 边沿。

所以「装配成时序」= `i2c_prog`（协议层装配）+ `i2c_bit`（电气层物化）协同；而「多字节命令序列」的内容由 `assem.py` 决定。

#### 4.4.2 核心流程

I2C 桥的整体数据流：

```
        主机(localbus)                    i2c_chunk (顶层)
        写程序 ───────────────────────▶ 4KB DPRAM 的 program 区(0x000-0x3ff)
        run_cmd=1 ─────────────────────▶ i2c_prog 开始取指执行
                                          │ 取指 p_addr → p_data(字节指令)
                                          ▼
                                       i2c_prog: 解码 opcode
                                          │ 输出 bit_cmd[1:0] + bit_adv
                                          ▼
                                       i2c_bit: 14 相位合成 SCL/SDA
                                          │ SCL/SDA 引脚
                                          ▼
                                       真实 I2C 设备
        读结果 ◀────────────────────── ping-pong results 区(0x800-0xbff)
                                       (i2c_analyze 同时把 SCL/SDA 跳变抓进 logic-analyzer 区)
```

**i2c_bit 的 14 相位**（见 [README 第 26-28 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md#L26-L28) 与源码注释）：一个 bit 时间被切成 14 份，SCL 高占 5 份、低占 9 份，SDA 在第 3 相位后切换，第 8 相位采样从机 SDA。bit 时间 = `clk周期 × 14 × 2^tick_scale`，`tick_scale=6` 且 125 MHz 时约 7.168 µs，即 ~140 kHz 总线速率。

**i2c_prog 的指令集**（[README 第 149-183 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md#L149-L183)）：每条指令 8 位 = 3 位 opcode + 5 位参数 n：

| opcode | 助记符 | 含义 |
|--------|--------|------|
| 000 | oo | 特殊（sleep/buffer_flip/trigger_analyzer/hw_config） |
| 001 | rd | 读，后跟设备地址，回读 n-1 字节 |
| 010 | wr | 写，后跟 n 字节数据 |
| 011 | wx | 写后重复 start（连续读） |
| 100/101 | p1/p2 | 短/长暂停 |
| 110 | jp | 跳转到 {n,5'b0} |
| 111 | sx | 设置结果地址 |

#### 4.4.3 源码精读

**最底层 i2c_bit：单 bit 波形合成器**。命令编码注释很关键：[peripheral_drivers/i2cbridge/i2c_bit.v:4-6](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_bit.v#L4-L6)

```
// 0: Tx0  1: Tx1  2: L  3: H
// where SCL is stopped (high) for L and H symbols.
```

14 相位计数与波形生成：[i2c_bit.v:29-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_bit.v#L29-L52)

```verilog
reg [3:0] cnt=0;  // count to 14
always @(posedge clk) begin
    if (tick) cnt <= last_tick ? 0 : cnt+1;
end
assign advance = tick & last_tick;            // 一个 bit 结束,请求下一条命令
always @(posedge clk) if (advance) cmd <= command;
always @(posedge clk) begin
    sda_o_r <= (cnt<3) ? old_bit : new_bit;   // 第 3 相位后切换 SDA
    scl_o_r <= cmd[1] ? 1 : cnt>=9;           // cmd[1]=1 则 SCL 停高;否则高占 5/14
    if (cnt == 8) sda_h_r <= sda_v;           // 第 8 相位采样 SDA
end
```

注意 `cmd[1]?1:cnt>=9`：`L`/`H` 命令（码 2/3，`cmd[1]=1`）让 SCL 持续高电平（用于总线空闲/停止），而 `Tx0/Tx1`（码 0/1，`cmd[1]=0`）产生正常时钟脉冲。

**中间层 i2c_prog：协议状态机**。状态定义：[i2c_prog.v:39-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v#L39-L46)

```verilog
localparam s_idle  = 0;
localparam s_start = 1;  // start bit for data transfer instructions
localparam s_data  = 2;
localparam s_ack   = 3;
localparam s_pad   = 4;
localparam s_stop  = 5;
```

opcode 译码（把字节指令展开成协议控制信号）：[i2c_prog.v:50-67](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v#L50-L67)

```verilog
wire o_oo = opcode==0;  wire o_rd = opcode==1;  wire o_wr = opcode==2;
wire o_wx = opcode==3;  wire o_p1 = opcode==4;  wire o_p2 = opcode==5;
wire o_jp = opcode==6;  wire o_sx = opcode==7;
wire op_zz = o_oo & (stream_cnt==0);  // sleep
wire op_bf = o_oo & (stream_cnt==2);  // buffer flip
wire op_ta = o_oo & (stream_cnt==3);  // trigger analyzer
```

主状态机：[i2c_prog.v:71-78](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v#L71-L78)，最妙的是把状态翻译成 `bit_cmd` 喂给 i2c_bit 的解码器：[i2c_prog.v:165-172](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v#L165-L172)

```verilog
always @(posedge clk) case(state)
    s_idle:  bc <= 2'b11;                              // H: 总线空闲
    s_start: bc <= op_xf ? 2'b10 : 2'b11;              // L: 产生 start(下降沿)
    s_data:  bc <= {1'b0, data_bit};                   // Tx0/Tx1: 发数据位
    s_ack:   bc <= {1'b0, ack_bit};                    // 读时释放让从机 ACK
    s_pad:   bc <= o_rd ? pad_rd : o_wx ? pad_wx : pad_wr; // 帧间填充/重复start/停止
    s_stop:  bc <= 2'b11;                              // H: 配合前一个 L 产生 stop
endcase
```

`data_bit = rd_cycle ? 1'b1 : sr[7]`（[第 159 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_prog.v#L159)）：读周期时主机发 1（释放 SDA 让从机驱动），写周期时发移位寄存器最高位。这就是「字节指令 → 逐位协议命令」的装配现场。

**顶层 i2c_chunk：内存接口与集成**。tick 分频：[i2c_chunk.v:50-55](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v#L50-L55)

```verilog
reg [tick_scale-1:0] access=0;
always @(posedge clk) begin access <= access+1; tick <= &access; end   // 2^tick_scale 分频
```

4 KB 内存四等分（这是主机侧看到的地址布局）：[i2c_chunk.v:127-131](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v#L127-L131)

```
//   0x000 - 0x3ff   program              (程序/指令)
//   0x400 - 0x7ff   logic analyzer       (逻辑分析仪 trace)
//   0x800 - 0xbff   results              (主机可读的结果)
//   0xc00 - 0xfff   result buffer in progress (后台正在填,主机别读)
```

写端口多路复用——把 localbus 写、trace 写、result 写、取指读统一到一个 DPRAM 端口：[i2c_chunk.v:138-164](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v#L138-L164)，用 `access[3:0]` 轮转分配时隙（`casez`）。

原子缓冲翻转（保证主机读到自洽数据）：[i2c_chunk.v:111-120](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_chunk.v#L111-L120)

```verilog
if (buffer_flip & ~freeze_d) begin pingpong <= ~pingpong; updated_r <= 1; end
if (~freeze_r & freeze_d) updated_r <= 0;
```

主机配合流程（[README 第 82-96 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md#L82-L96)）：置 freeze → 读结果 → 清 freeze。freeze 期间 buffer_flip 被忽略，主机读到的快照不会中途变化。

**Python 汇编器 assem.py**。它定义两个类：低层 [`class i2c_assem`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py#L45)（[第 45 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py#L45)）和高层 OO 接口 [`class I2CAssembler`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py#L173)（[第 173 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/assem.py#L173)，是前者的超集）。[`ramtest.py`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/ramtest.py) 演示了低层用法：

```python
s = assem.i2c_assem()
a  = s.pause(2)
a += s.hw_config(0)
a += s.write(sadr, 1, [0xa5, 0x5a])   # 向设备 sadr 写 2 字节
a += s.read(sadr, 2, 1)               # 回读
a += s.buffer_flip()                  # 翻转结果缓冲
a += s.jump(1)                        # 循环
print("\n".join(["%02x" % x for x in a]))  # 输出字节流 → init.in
```

这段人话被翻译成 i2c_prog 能吃的字节序列，Makefile 用 `init.in: ramtest.py\n\t$(PYTHON) $< > $@`（[i2cbridge/Makefile:55-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/Makefile#L55-L56)）生成，再由 `$readmemh` 装载进程序内存。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：跑通 I2C 桥自测，并用汇编器亲手装配一段多字节命令序列，验证「字节→时序」的装配链路。

**操作步骤**：

1. `cd peripheral_drivers/i2cbridge && make`（即 [`Makefile`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/Makefile) 的 `all`，第 44 行：`all: i2c_bit_tb i2c_prog_check i2c_analyze_tb i2c_chunk_check`）。
2. 单独看汇编器输出：`python3 ramtest.py`（应打印一串两位十六进制字节，即 `init.in` 内容）。
3. 看 `i2c_chunk_check` 依赖 `init.in`（[Makefile 第 55-62 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/Makefile#L55-L62)），理解字节流如何被 `$readmemh` 装进程序内存。
4. 想看波形：`make i2c_bit.vcd`（底层位时序）、`make i2c_prog.vcd`（协议状态）、`make i2c_chunk.vcd`（顶层），或对应 `_view` 目标。

**需要观察的现象 / 要回答的问题**：

- 对照三层职责表，回答综合实践的核心问题（见下方）。
- 在 `i2c_bit.vcd` 里数 SCL 一个完整 bit 周期内的 clk 数，确认是 `14 × 2^tick_scale`（默认 tick_scale=6 → 每位 896 个 clk）。
- 在 `i2c_prog.vcd` 里跟踪一次 `write(0x20,1,[0xa5,0x5a])`：状态机应经历 `s_start → s_data×8(地址) → s_ack → s_data×8(0xa5) → s_ack → s_data×8(0x5a) → s_ack → s_stop`。

**预期结果**：`make` 全部目标通过（各 `_check` 打印 `PASS`，注意 `i2c_bit_tb` 自称 "Non-checking testbench. Will always PASS"，见 [i2c_bit_tb.v:13-20](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/i2c_bit_tb.v#L13-L20)）。

> 待本地验证：若缺 iverilog 或 gtkwave，`_view` 类目标会失败，但 `_check`/`_tb` 类目标只要 iverilog 可用即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 I2C 桥要分 `i2c_bit`/`i2c_prog` 两层，而不是合并成一个状态机？

> **答案**：分离关注点。`i2c_bit` 只关心「一个 bit 的电气时序」（14 相位、SCL 占空比、SDA 采样点），与协议无关，可独立仿真（`i2c_bit_tb` 喂一张 50 条命令的表）；`i2c_prog` 只关心「协议结构」（start/addr/ack/data/stop），不关心 clk 频率。两者通过 `bit_cmd`/`bit_adv` 这对窄接口解耦，任一层都能单独替换或复用。

**练习 2**：`i2c_chunk` 的 ping-pong 结果缓冲与 `freeze` 配合，解决什么问题？和 `spi_mon` 的双缓冲有何异同？

> **答案**：解决「主机读结果时后台正在刷新」的数据撕裂问题。后台填 `0xc00` 区，填完后 `buffer_flip` 原子切到 `0x800` 区供主机读；主机置 `freeze` 可冻结翻转保证读一整块。与 `spi_mon` 双缓冲思想一致，区别在于 I2C 桥额外提供 `freeze` 显式门控和 `updated` 标志，适配更慢、更需一致性的轮询场景。

**练习 3**：指令集里 `jp`（跳转）的地址是 `{n, 5'b0}`（[README 第 172-173 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/i2cbridge/README.md#L172-L173)），粒度是 32 字节。为什么不是按字节跳转？

> **答案**：5 位 `n` 只能编码 0–31，拼上 5 个 0 后扩展成 10 位地址（覆盖 1 KB 程序区），代价是跳转目标必须 32 字节对齐。这是用「粒度换寻址范围」的经典编码折中——I2C 命令序列通常很短，32 字节对齐足够。

## 5. 综合实践

把四个最小模块串起来：**用 `spi_master` 配置 `ad9653`，再用 I2C 桥轮询一颗 I2C 温度/电压芯片，画出两条配置链路的完整调用层次。**

1. **SPI 配置链**：仿照 `ad9653_sim.v`，说明一次「读 AD9653 寄存器 0x02」的调用栈：
   - 主机（SoC 的 localbus 或 spi_mon）把 `spi_addr=0x8002`（最高位 1=读）、`spi_read=1` 交给 `spi_master`；
   - `spi_master` 发 16 位地址、进入数据相位后抬 `sdio_as_sdo`；
   - `ad9653.v` 的 `IOBUF(.T(sdio_as_i))` 据此释放 SDIO、把从机回读经 `.O(sdi)` 回送；
   - `spi_master` 在 cs 下降沿把 8 位结果锁到 `spi_rdbk`、脉冲 `spi_ready`。
   - 写出这条链上每段代码所在的文件名与行号区间。
2. **I2C 配置链**：用 `ramtest.py` 改写一段你自己的程序（例如先 `hw_config(0)`，向设备 0x48 写两个寄存器，再回读，最后 `buffer_flip` + `jump` 循环），运行 `python3 myprog.py > init.in`，再 `make i2c_chunk.vcd` 看波形。
3. **回答分层问题（本讲核心考点）**：在报告里明确写出——
   - 「多字节命令序列」由谁产生？（答：Python `assem.py`）
   - 谁把字节指令装配成协议时序？（答：`i2c_prog`，经状态机译成 `bit_cmd`）
   - 谁把协议命令物化成电平？（答：`i2c_bit`，14 相位合成）
   - `i2c_chunk` 在其中扮演什么角色？（答：内存接口与集成壳，自身不参与协议时序装配）

完成本实践后，你应能对任意一颗新外设芯片，判断它该走 SPI 还是 I2C、该用哪个驱动当「壳」、配置时序由谁产生。

## 6. 本讲小结

- Bedrock 外设驱动遵循统一的「**passthrough 壳 + `` `ifndef SIMULATE `` 守卫 + _sim 模型**」三件套，把厂家 IO 原语与可移植控制逻辑干净分离；`ad9653`/`ad9781`/`lmk01801` 是壳，`spi_master`/`i2c_bit` 才是真正的时序引擎。
- `spi_master.v` 是参数化 SPI 主机，发「地址+数据」、用 `sdio_as_sdo` 在数据相位切双向 SDIO 方向完成读回；`spi_mon.v` 在其上构建可编程轮询器，双缓冲回读。
- `ad9653.v` 包装源同步 LVDS 接收（`lvds_iophy`/ISERDES/IDELAY/MMCM），逐 lane 处理并做 CDC 同步；`ad9781` 是 DDR LVDS 发送；`ad5662` 是无原语的纯逻辑 SPI DAC——三者体现「驱动」复杂度的三个台阶。
- I2C 桥是分层典范：`i2c_bit`（位→电平）、`i2c_prog`（字节指令→协议时序）、`i2c_chunk`（内存接口/集成壳），外加 Python `assem.py`（人话→字节指令）。
- `i2c_chunk` 用 4 KB DPRAM 四分（程序/逻辑分析仪/结果/后台缓冲）+ ping-pong + freeze 提供自洽的主机读出范式。
- 本目录还含 `ds1822`（单线温度）、`idelay_scanner`（动态 IDELAY 对齐扫描）、`wrappers/`（CDCE62005/SI571/QSFP 等更多芯片壳）等子模块，设计模式与本讲所述一致。

## 7. 下一步学习建议

- **横向对照**：阅读 [`spi_eater.v`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/spi_eater.v)（基于 FIFO 的另一种 SPI 主机）与 `spi_master`，体会「同一协议、不同实现取舍」。
- **钻进 ISERDES**：去 `fpga_family/iserdes/` 读 `lvds_iophy.v`/`lvds_dco.v`/`lvds_frame.v`，理解 `ad9653` 数据通道的真正实现，并配合 [`idelay_scanner/README.md`](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/peripheral_drivers/idelay_scanner/README.md) 看动态对齐。
- **承接 u7-l3**：下一讲 [u7-l3 板级支持与 FPGA 厂家抽象](u7-l3-board-fpga-abstraction.md) 会讲 `board_support` 与 `fpga_family`，本讲里所有 `IOBUF/IBUFDS/ODDR` 原语的归宿就在那里。
- **工程集成**：之后进入 [u7-l4 工程集成实战](u7-l4-projects-integration.md)，看 `marble_top.v` 如何把本讲的 ADC/DAC/SPI/I2C 驱动、Packet Badger、localbus 组装成一块可上板的完整设计。
- **动手扩展**：仿照 `ad5662.v`（最简单）为另一颗 SPI 芯片写一个驱动壳，再用 `spi_master` 驱动它，跑通 iverilog 仿真——这是检验你是否真懂本讲的最佳方式。
