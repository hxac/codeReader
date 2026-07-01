# SPI 主机与 TL-UL 桥接

## 1. 本讲目标

学完本讲，你应当能够：

- 分清 CoralNPU 里**两个长得像、实则相反**的 SPI 模块：一个是 CPU 用的「SPI 主机」，一个是给外部主机用的「SPI 从机桥」。
- 读懂 `SpiMaster`：它本身是一个 **TL-UL 从机（device）**，CPU 通过一组寄存器（`STATUS`/`CONTROL`/`TXDATA`/`RXDATA`/`CSID`/`CSMODE`）驱动片外 SPI 总线，理解它的状态机、FIFO、波特率与时钟域穿越（CDC）。
- 读懂 `Spi2TLULV2`：它是一个 **TL-UL 主机（host）**，把外部 SPI 串行字节流按「操作码+地址+长度+数据」的帧协议解析成 SoC 内部的 TL-UL 读/写事务，相当于一个「由 SPI 遥控的 DMA」。
- 把 SPI 与本单元前两讲的 DMA 串起来：理解 DMA 用 `Mem→Periph`（`dst_fixed`）模式向 SPI 的 TX FIFO 灌数据的完整路径，以及外设流控如何防止 FIFO 溢出。

> 重要更正：本讲规划时曾把 `Spi2TLULV2` 当作「映射了 TXDATA/RXDATA/STATUS 寄存器的 TL-UL 从机」。**真实源码并非如此**——这些寄存器属于 `SpiMaster`（TL-UL 从机），而 `Spi2TLULV2` 是 TL-UL 主机、没有寄存器映射、只有帧协议。本讲按真实源码讲解，并在第 4.1 节把两者的身份彻底分清。

## 2. 前置知识

- **SPI 是什么**：一种主从式、串行的 4 线总线，4 根线分别是 `SCLK`（时钟）、`CSB/CS`（片选，通常低有效）、`MOSI`（主出从入）、`MISO`（主入从出）。主机产出 `SCLK` 与 `CSB`，每个时钟周期主机和从机各移出 1 位、同时移入 1 位，因此 SPI 是**全双工**的——发一个字节的同时也会收一个字节，哪怕收到的没用。
- **CPOL / CPHA**：CPOL 决定 `SCLK` 空闲电平（0=低、1=高），CPHA 决定在第几个时钟沿采样数据（0=前沿采样、1=后沿采样）。二者组合出 4 种 SPI 模式（Mode 0~3）。
- **FIFO 解耦**：SPI 的位时序很慢（kHz~MHz），而总线事务很快（百 MHz）。用一个小 FIFO 把「总线写入」和「SPI 移位」解耦，CPU 一次性塞几字节进 TX FIFO 就可以去做别的事。
- **TL-UL 复习（见 u3-l3）**：TL-UL 用 A 通道发请求（`Get` 读、`PutFullData`/`PutPartialData` 写）、D 通道回响应（`AccessAckData` 带数据、`AccessAck` 不带数据），靠 `valid/ready` 握手。**谁发请求谁是 host（主），谁响应谁是 device（从）**。
- **CDC（Clock Domain Crossing）**：SPI 常跑在独立的慢时钟上，和系统总线不同步，跨时钟域要用法向 FIFO 或 `AsyncQueue` 做同步。
- **承接**：本讲依赖 u3-l3（TL-UL）、u3-l4（总线互联，理解 host/device 连接）、u8-l1（DMA 引擎与 `dst_fixed`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [SpiMaster.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala) | **SPI 主机 IP**。内含 `SpiMasterCtrl`（核心控制器，TL-UL 从机 + 寄存器 + 状态机 + FIFO）与 `SpiMaster`（带 CDC 的顶层包装）。CPU 经它驱动片外 SPI 器件。 |
| [Spi2TLUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLUL.scala) | **SPI 从机 → TL-UL 主机的薄包装**。把传统 4 线 SPI 从机接口（clk/csb/mosi/miso）接到 `Spi2TLULV2`，对外暴露一个 TL-UL host 端口。 |
| [Spi2TLULV2.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala) | **帧解析 + TL-UL 事务生成器**。把 SPI 字节流解析成「操作码+地址+长度」描述符，再由内部 DMA 状态机发 TL-UL `Get`/`Put` 事务。 |
| [SoCChiselConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala) | 在 SoC 顶层把这两个模块分别以 host / device 身份挂到总线上。 |
| [test_spi_master.py](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_spi_master.py) | cocotb 测试，给出 `SpiMaster` 的寄存器基址与三种工作模式的真实用法。 |
| [dma.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md) | DMA 外设文档，明确把「SRAM→SPI TX FIFO」列为 `Mem→Periph`(`dst_fixed`) 的典型例子。 |

---

## 4. 核心概念与源码讲解

### 4.1 先分清身份：两个 SPI 模块，一主一从

这是本讲最容易搞混、也最关键的一点。CoralNPU 里有**两个**名字相近的 SPI 模块，但它们在总线上扮演**相反**的角色：

| 模块 | 类名 | 在 SoC 里的连接身份 | 谁是 SPI 主机 | 干什么 |
| --- | --- | --- | --- | --- |
| `spi_master` | `bus.SpiMaster` | **device（从机）** | **CoralNPU 自己**是 SPI 主机 | CPU 把它当一组寄存器来读写，从而驱动片外的 SPI 从器件（如传感器、Flash） |
| `spi2tlul` | `bus.Spi2TLUL` | **host（主机）** | **外部**是 SPI 主机 | 外部主机通过 SPI 发命令帧，读写 CoralNPU SoC 内部的存储 |

证据在 SoC 装配配置里——注意 `deviceConnections` 与 `hostConnections` 的区别：

`spi_master` 走 `deviceConnections`，即它是被别人访问的从机：
[SoCChiselConfig.scala:L187-L193](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L187-L193) —— `moduleClass = "bus.SpiMaster"`、`deviceConnections = Map("io.tl" -> "spi_master")`，外部端口 `spim_sclk/spim_csb/spim_mosi` 都是 **Output**（CoralNPU 产出 SPI 时序）。

`spi2tlul` 走 `hostConnections`，即它主动发起 TL-UL 事务：
[SoCChiselConfig.scala:L161-L167](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L161-L167) —— `moduleClass = "bus.Spi2TLUL"`、`hostConnections = Map("io.tl" -> "spi2tlul")`，外部端口 `spi_clk` 是 **Input**（由外部提供 SPI 时钟）。

> 一句话记忆：**`SpiMaster` 是「我当 SPI 主机」，`Spi2TLUL` 是「我当 SPI 从机，把 SPI 帧翻译成对内 TL-UL 事务」**。这也解释了为什么「TXDATA/RXDATA/STATUS 寄存器映射成 TL-UL 地址」只发生在 `SpiMaster` 身上——因为只有它才是被 CPU 寻址的 TL-UL 从机。

接下来分别精读。

---

### 4.2 SpiMaster：可被 CPU 寻址的 SPI 主机（TL-UL 从机）

#### 4.2.1 概念说明

`SpiMaster` 是一个标准的「SPI 主机 IP」：CPU 把它当一组 32 位寄存器来访问，通过写 `TXDATA` 把要发送的字节推进 TX FIFO，硬件状态机会自动拉低 `CSB`、产出 `SCLK`、把字节按位移出到 `MOSI`，同时把 `MISO` 上采到的位拼成字节塞进 RX FIFO，CPU 再用 `RXDATA` 读回。

它的对外接口同时跨两个世界：一侧是 TL-UL 总线（对 CPU），一侧是 SPI 引脚（对片外器件）：
[SpiMaster.scala:L35-L40](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L35-L40) —— `io.tl` 是 `Flipped(Host2Device)`（即 TL-UL **从机**端口），`io.spi` 是 4 线 SPI。

4 线 SPI 的方向定义（注意 `csb` 低有效、`miso` 是输入）：
[SpiMaster.scala:L23-L28](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L23-L28)

#### 4.2.2 核心流程

**寄存器映射**（来自 cocotb 测试的基址与源码偏移量）：

| 偏移 | 寄存器 | 读/写 | 含义 |
| --- | --- | --- | --- |
| 0x00 | `STATUS` | RO | bit0=Busy、bit1=RX Empty、bit2=TX Full |
| 0x04 | `CONTROL` | RW | bit0=enable、bit1=cpol、bit2=cpha、bit3=hdrx、bit4=hdtx、bit[15:8]=div |
| 0x08 | `TXDATA` | WO | 写：低 8 位压入 TX FIFO |
| 0x0C | `RXDATA` | RO | 读：弹出 RX FIFO 一个字节 |
| 0x10 | `CSID` | RW | 片选号（手动模式下用 bit0 决定 CSB） |
| 0x14 | `CSMODE` | RW | 0=Auto（每笔事务自动拉/放 CS）、1=Manual |

> 基址 `0x40020000` 来自 cocotb 测试 [test_spi_master.py:L24-L30](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_spi_master.py#L24-L30)；偏移量定义在源码 [SpiMaster.scala:L43-L50](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L43-L50)。基址由 SoC 地址译码决定，可能因配置而异，**以本地地址映射为准**。

**发送一个字节的状态机**（`SpiState`：`sIdle → sSetup → sShift → sFinish`）：

```
sIdle   : 等到 TX FIFO 有数据（或 HDRX 自动模式）→ 取字节进 tx_reg，拉低 CSB(Auto)，进 sSetup
sSetup  : 等一个 tick，给出第一个 SCLK 沿 → 进 sShift
sShift  : 每 tick 翻转 phase，按 CPOL 驱动 SCLK；前沿采 MISO、后沿移 MOSI；
          bit_count 从 7 减到 0，移完 8 位 → 进 sFinish
sFinish : 收尾，把 rx_reg 压入 RX FIFO（HDTX 模式跳过），放 CSB(Auto) → 回 sIdle
```

**波特率**：内部计数器 `clk_count` 数到 `ctrl_div` 产生一个 `tick`，每个 `tick` 对应 SCLK 的半个周期。设 SPI 控制器时钟频率为 \(f_{\text{spiclk}}\)，则 SCLK 频率约为

\[
f_{\text{SCLK}} \;=\; \frac{f_{\text{spiclk}}}{2\,(\text{div}+1)}
\]

**半双工模式**（用于对常见的「写命令、读数据」型 SPI 器件）：

- `hdtx`（bit4）：发的时候**丢弃**收到的字节，不压 RX FIFO——否则 RX FIFO 很快塞满反压住发送。
- `hdrx`（bit3）：读的时候**自动**产生 `TX=0x00` 的传输来给 MISO 提供时钟，只要 RX FIFO 有空位就连续读，无需 CPU 写 TXDATA。

#### 4.2.3 源码精读

**CONTROL 寄存器的位域切片**——配置都从一个 32 位寄存器里拆出来：
[SpiMaster.scala:L54-L64](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L54-L64) —— 注意 `ctrl_div = reg_control(15,8)`、`ctrl_enable = reg_control(0)`。

**TX/RX FIFO**——各深 4、宽 8 位，用 Chisel 通用 `Queue`：
[SpiMaster.scala:L67-L68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L67-L68)

**波特率 tick 生成**——`clk_count` 数到 `div` 清零并拉高 `tick` 一个周期：
[SpiMaster.scala:L82-L91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L82-L91)

**状态机的入口 `sIdle`**——区分两种启动：正常从 TX FIFO 取字节，或 HDRX 自动产生 `0x00`：
[SpiMaster.scala:L128-L149](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L128-L149)

**`sShift` 里的位移与采样**——`bit_count` 从 7 递减，移完一字节且 RX FIFO 有空位（或 HDTX）才进 `sFinish`，避免覆盖收到的数据：
[SpiMaster.scala:L161-L183](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L161-L183)

**MISO 采样 / MOSI 移位**——用 `phase` 与 `ctrl_cpha` 决定哪个沿采样、哪个沿移位（即实现 4 种 SPI 模式）：
[SpiMaster.scala:L204-L223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L204-L223)

**TL-UL 从机接口的反压**——`tl_a.ready` 要同时满足「没有响应在途」和「FIFO 能收/能给」两个条件，把慢速 SPI 与快速总线解耦：
[SpiMaster.scala:L245-L249](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L245-L249)

**STATUS 寄存器读出**——三个状态位实时拼装：忙、RX 空、TX 满：
[SpiMaster.scala:L276-L284](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L276-L284)

**顶层 CDC 包装 `SpiMaster`**——它是个 `RawModule`，用 `TlulFifoAsync` 把系统总线时钟域桥接到独立的 `spi_clk_i` 域，控制器在 `spi_clk_i` 下运行：
[SpiMaster.scala:L323-L351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L323-L351) —— 注意 `withClockAndReset(io.spi_clk_i, spi_reset)` 把 `SpiMasterCtrl` 整体放进 SPI 时钟域。

#### 4.2.4 代码实践：从源码与测试提炼时序与 FIFO

1. **实践目标**：把 `SpiMaster` 的关键时序参数、FIFO 深度、寄存器语义整理成一张表，并能在 cocotb 测试里对上号。
2. **操作步骤**：
   - 打开 [SpiMaster.scala:L67-L68](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L67-L68)，确认 TX/RX FIFO 深度都是 **4**、宽度 **8 位**。
   - 看 [SpiMaster.scala:L59-L64](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L59-L64)，把 `div/cpol/cpha/hdtx/hdrx/enable` 各自的位填进表里。
   - 打开 cocotb 测试 [test_spi_master.py:L103-L138](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/tests/cocotb/tlul/test_spi_master.py#L103-L138)：它写 `CONTROL = (2<<8)|1`（div=2、enable=1、Mode 0），再写 `TXDATA = 0xA5`，然后在 `spim_sclk` 上升沿采 8 位 `mosi`，断言收到 `0xA5`。
3. **需要观察的现象**：`CSB` 在传输期间为低、传输结束回高；`MOSI` 在 Mode 0 下于 `SCLK` 上升沿有效，8 个沿拼出 `10100101`。
4. **预期结果**：源码的 `div=2` 与测试里「外部 10 MHz SPI 时钟」下，按 \(f_{\text{SCLK}}=f_{\text{spiclk}}/(2(\text{div}+1))\) 可估算 SCLK 频率；测试断言 `received_val == test_byte` 必须成立。**逐周期 SCLK 相位的精确对齐建议在本地跑 `SpiMasterTest` 或该 cocotb 用例后对照波形确认（待本地验证）。**
5. 想直接动手？该项目提供 `tests/cocotb/tlul/test_spi_master.py`，可用本单元 u2-l4 / u11-l3 介绍的 cocotb 回归方式运行它（具体命令以仓库 `tests/cocotb/BUILD` 与 `rules/coco_tb.bzl` 为准）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么读 `TXDATA`、写 `STATUS`/`RXDATA` 会被返回错误？
  - **答案**：见 [SpiMaster.scala:L270-L294](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L270-L294)。`TXDATA` 是只写、`STATUS`/`RXDATA` 是只读，反向访问会在对应分支置 `tl_d_error := true.B`，最终 D 通道带回 `error=1`（TL-UL 层面表现为错误响应）。
- **练习 2**：`hdrx` 模式下，CPU 没写 `TXDATA`，SPI 为什么也能动起来？
  - **答案**：见 [SpiMaster.scala:L141-L148](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L141-L148)。`sIdle` 里 `elsewhen(ctrl_hdrx && rx_fifo.io.enq.ready)` 分支会自动把 `tx_reg := 0.U` 并进入传输，用「假写 0x00」给 MISO 提供采样时钟。

---

### 4.3 Spi2TLULV2：把 SPI 帧翻译成 TL-UL 事务的桥（TL-UL 主机）

#### 4.3.1 概念说明

`Spi2TLULV2` 解决的是另一个方向的问题：**让一个外部 SPI 主机能够读写 CoralNPU SoC 内部的存储**。它是一个 SPI **从机**，接收外部发来的串行字节，按一套自定义「帧协议」拼装成一次内存访问描述符，再由内部的 DMA 状态机对 SoC 总线发出 TL-UL `Get`（读）或 `PutFullData`（写）事务。

它没有 `TXDATA`/`RXDATA` 这种寄存器映射——那是 `SpiMaster` 的事。它对外只暴露：

- SPI 侧：`spi_clk`、`spi_rst_n`、`q_mosi_pin`（输入位流）、`q_miso_pin`（输出位流）。
- TL-UL 侧：`q_tl_a`（发出的请求）、`q_tl_d`（收到的响应）——注意它发 A、收 D，所以是 **host**。

证据见 [Spi2TLULV2.scala:L514-L521](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L514-L521)：`q_tl_a = Decoupled(A_Channel)`、`q_tl_d = Flipped(Decoupled(D_Channel))`。

而 `Spi2TLUL` 是它的一层薄包装，把抽象的 `q_mosi_pin/q_miso_pin` 接到传统 4 线 SPI 信号，并把 `csb` 当作低有效复位（`spi_rst_n := !csb`）：[Spi2TLUL.scala:L34-L46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLUL.scala#L34-L46)。

#### 4.3.2 核心流程

**帧协议**（外部主机经 MOSI 串行发来，按字节解析）：

| 字段 | 字节数 | 说明 |
| --- | --- | --- |
| op | 1 | 操作码：`1`=读、`2`=写 |
| addr | 4 | 目标地址，大端（先发 addr[31:24]，最后 addr[7:0]） |
| len | 2 | 长度，**以 16 字节 beat 计**，大端 |
| data | (len+1)×16 | 仅写操作（op=2）有，紧随其后的写入数据流 |

解析状态机 `SpiFrameParserPhase`：`sOp → sAddr3 → sAddr2 → sAddr1 → sAddr0 → sLen1 → sLen0 → sSendDesc →（写则 sWriteData）→ sWaitEnd`，见 [Spi2TLULV2.scala:L29-L31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L29-L31)。

解析完成后产出 `DmaDesc`（`op/addr/len` 三字段，[L23-L27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L23-L27)），跨时钟域交给 TL-UL 域的 DMA 引擎。

**TL-UL 侧 DMA 引擎** `DmaEnginePhase`：`sIdle →`（读）`sReadAddr → sReadData → …` 或（写）`sWriteData → sWriteAddr → sWriteAck → …`，见 [Spi2TLULV2.scala:L33-L35](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L33-L35)。它发出的每笔 TL-UL 事务都是 **16 字节（size=4，mask=0xffff）** 的 beat，地址按 `dma_addr + beat_cnt×16` 递增。

**模块划分为两个时钟域**：

- `Spi2TLULV2_SpiDomain`（跑 `spi_clk`）：拼字节、解析帧、对读回数据做并→串喂给 MISO。
- `Spi2TLULV2_TlulDomain`（跑系统 `clock`）：拿描述符、发 TL-UL 事务。
- 两者之间用三条 `AsyncQueue` 做 CDC：描述符、写数据、读数据，见 4.3.3。

#### 4.3.3 源码精读

**断言固定 128 位 beat**——整个桥以 16 字节为搬运粒度：
[Spi2TLULV2.scala:L512](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L512) —— `assert(p.lsuDataBits == 128)`。

**SPI 域的字节拼装器 `SpiByteAssembler`**——每收满 8 位产出 1 字节进 `q_spi_byte_q`：
[Spi2TLULV2.scala:L268-L281](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L268-L281)

**SPI 域的帧解析状态转移**——`MuxLookup` 按 phase 调用对应的 `onOp/onAddr/onLen/onSendDesc/onWriteData`，把字节装配进 `DmaDesc`：
[Spi2TLULV2.scala:L310-L351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L310-L351) —— 注意 `onSendDesc` 里 `is_op_write = (op === 2.U)`，写操作会接着进入 `sWriteData` 并算出 `wr_remain = (len+1)<<4`（即总字节数）。

**TL-UL 域构建 A 通道请求**——读发 `Get`、写发 `PutFullData`，固定 `size=4`、`mask=0xffff`、地址按 beat 递增：
[Spi2TLULV2.scala:L473-L485](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L473-L485)

**TL-UL 域 DMA 引擎生命周期**——`onIdle` 根据描述符 `op` 选择走读分支还是写分支：
[Spi2TLULV2.scala:L488-L499](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L488-L499)

**顶层 CDC——三条 `AsyncQueue`**：
[Spi2TLULV2.scala:L526-L542](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L526-L542) —— `q_desc_cdc`(深 4)、`q_wr_data_cdc`(深 16)、`q_rd_data_cdc`(深 4)，分别搬运描述符、写数据、读数据，`safe=true` 表示带双 FIFO 满空保护。

**读回通路（MISO）**——`SpiBulkDeserializer` 把 128 位读数据拆成字节、`SpiMisoShifter` 再串行移出，并在头部插一个 `0xfe` 同步字节：
[Spi2TLULV2.scala:L353-L369](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L353-L369)

#### 4.3.4 代码实践：解码一帧 SPI 命令

1. **实践目标**：给定一帧 SPI 字节，能手工推出它会被解析成什么 TL-UL 事务。
2. **操作步骤**：
   - 假设外部主机经 MOSI 依次发来字节：`02 00 10 00 00 00 01`，后面跟 32 字节写入数据。请按下表逐字段拆解。
   - 对照 [Spi2TLULV2.scala:L310-L351](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L310-L351) 确认你的解析顺序（op→addr3→addr2→addr1→addr0→len1→len0）。
   - 再看 [Spi2TLULV2.scala:L473-L485](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L473-L485)，推出 TL-UL 域会发出几笔、什么类型、各 beat 的地址是多少。
3. **需要观察的现象 / 预期结果**（答案）：
   - op=`0x02` → 写操作；addr=`0x00100000`；len=`0x0001` → `(len+1)=2` 个 beat，共 32 字节。
   - TL-UL 域发出 **2 笔** `PutFullData`，`size=4`、`mask=0xffff`，地址依次为 `0x00100000`、`0x00100010`；写入数据由后续 32 字节填充。
   - 若把首字节换成 `0x01`（读）并去掉数据段，则改为发 2 笔 `Get`，读回的 32 字节会经 `SpiBulkDeserializer` 串行从 MISO 移出（前置一个 `0xfe`）。
4. 想跑真实用例？项目提供了 [Spi2TLULV2Test.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2Test.scala)，可在本地用 Chisel 测试框架运行，对照断言验证上述帧解析（具体测试运行方式以 `hdl/chisel/src/bus/BUILD` 为准）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `Spi2TLUL` 把 `csb` 当成复位（`spi_rst_n := !io.spi.csb`）？
  - **答案**：见 [Spi2TLUL.scala:L37-L38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLUL.scala#L37-L38)。一次 SPI 事务以 `CSB` 拉低开始、拉高结束；用 `CSB=1`（空闲）作为复位，可保证每笔事务开始时帧解析状态机都从 `sOp` 干净起步，避免上一帧残留。
- **练习 2**：读操作时 MISO 上第一个字节为什么是 `0xfe`？
  - **答案**：见 [Spi2TLULV2.scala:L364-L368](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2.scala#L364-L368)。当 `byte_idx===0` 时写入 `0xfe.U`，作为一次读响应的「同步/前导字节」，让外部主机能对齐到有效数据的起点。

---

### 4.4 SPI 与 DMA 的协作：用 `Mem→Periph` 灌 TX FIFO

#### 4.4.1 概念说明

本单元 u8-l1 讲过 DMA 引擎支持 `Mem→Periph`（源地址递增、目的地址固定）模式，文档里给出的典型例子正是 **「SRAM→SPI TX FIFO」**：
[dma.md:L31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L31) —— `| Mem→Periph | Incrementing | Fixed | SRAM→SPI TX FIFO |`。

这里的「SPI TX FIFO」就是 4.2 节 `SpiMaster` 的 TX FIFO——它的入口正是 `TXDATA` 寄存器（偏移 0x08）。把 DMA 与 `SpiMaster` 串起来，CPU 就不必一个字节一个字节地写 `TXDATA`，而是让 DMA 一次性把 SRAM 里的一块缓冲搬到固定的 `TXDATA` 地址。

#### 4.4.2 核心流程：`dst_fixed` 的完整路径

DMA 描述符里 `dst_fixed=1` 时，每搬一个 beat，**目的地址保持不变**，只有源地址递增。源码与文档对此的双重佐证：

- 文档 [dma.md:L75](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L75) 与 [dma.md:L100](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L100)：`flags [27] src_fixed, [28] dst_fixed`。
- RTL [DmaEngine.scala:L323-L324](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L323-L324)：
  ```
  b.src_addr := Mux(desc.src_fixed, xfer.src_addr, xfer.src_addr + beat_bytes)
  b.dst_addr := Mux(desc.dst_fixed, xfer.dst_addr, xfer.dst_addr + beat_bytes)
  ```

完整路径（CPU 视角的配置步骤）：

```
1. 在 SRAM/DTCM 备好待发送缓冲（源，地址递增）
2. 配置 SpiMaster：CONTROL(enable/cpol/cpha/div)、CSID、CSMODE
3. 构造 DMA 描述符：
     src_addr  = 缓冲首地址
     dst_addr  = SpiMaster 基址 + 0x08   (= TXDATA，固定)
     len       = 待发字节数（按 DMA beat 约定）
     dst_fixed = 1,  src_fixed = 0        → Mem→Periph
     (可选) poll_en=1, poll_addr=STATUS, mask=TX_Full 位, value=0
4. 启动 DMA → DMA 反复向固定地址 TXDATA 写字节 → 进 SpiMaster TX FIFO → 硬件自动移位发出
```

**外设流控（防溢出）**：TX FIFO 只有 4 深度，DMA 若一味快写会溢出。DMA 的 `poll` 机制可让它在写之前先轮询外设状态寄存器。文档明说 SPI 主机的状态寄存器（TX Full / RX Empty 标志）正好用于此目的，无需改动外设：
[dma.md:L83-L87](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L83-L87) ——「Peripherals like SPI master expose status registers (TX Full, RX Empty flags)」。

而 `SpiMaster` 的 STATUS 寄存器恰好暴露了这两个标志（[SpiMaster.scala:L276-L284](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L276-L284)），于是 DMA 的 `poll_addr` 指到 `STATUS`、`mask` 选 TX Full 位、`value=0`（即「等到不满再写」），就完成了软硬件握手。

> 说明：以上路径是把 u8-l1 的 DMA 与本讲 `SpiMaster` 按各自真实接口「拼」起来的标准用法，文档 `dma.md` 已把它列为范例。是否在当前 SoC 配置里默认连了这两个端口、以及确切地址，**以本地 SoC 地址映射与 `SoCChiselConfig` 为准（待本地验证）**。

#### 4.4.3 代码实践（源码阅读型）

1. **实践目标**：把 DMA 描述符字段与 `SpiMaster` 寄存器一一对应，写出一张「DMA 字段 → SPI 含义」表。
2. **操作步骤**：
   - 在 [DmaEngine.scala:L77-L91](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L77-L91) 找到描述符的 `src_fixed/dst_fixed` 字段定义。
   - 在 [SpiMaster.scala:L43-L50](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L43-L50) 确认 `TXDATA=0x08`、`STATUS=0x00`。
   - 列表：`dst_addr = BASE+0x08`、`dst_fixed=1`、`poll_addr = BASE+0x00`、`poll mask = 0x4`（TX Full 位）、`poll value = 0`。
3. **需要观察的现象**：若在仿真里把 DMA 的 `poll_en` 关掉、让 DMA 全速写 TX FIFO，会发生什么？
4. **预期结果**：TX FIFO 深度仅 4，关掉流控后 DMA 写入会超过 SPI 移出速度，后到的数据会被 `SpiMaster` 的 TL-UL 反压（`tl_a.ready` 因 `tx_fifo.io.enq.ready=0` 而拉低，见 [L245-L249](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMaster.scala#L245-L249)）从而自动背压，不丢数据；开启 `poll_en` 则让 DMA 主动等「不满」，减少总线上的反压停顿。**具体波形待本地验证。**

---

## 5. 综合实践

**任务：为一次「SPI Flash 读取」设计软件驱动序列，并标注每一步用到的是哪个模块、哪个寄存器。**

背景：假设 CoralNPU 要通过片外 SPI Flash 读一个 32 字节的镜像，外部 SPI Flash 协议是「先发 1 字节读命令 `0x03` + 3 字节地址，再连续读 N 字节」。请用本讲的 `SpiMaster`（TL-UL 从机）完成：

1. 写出初始化序列：配 `CONTROL`（选 Mode 0、合适的 `div`、enable）、`CSID`、`CSMODE=Auto`。
2. 写出「发命令+地址」阶段：依次写 `TXDATA`（命令 1 字节 + 地址 3 字节），说明此时是全双工、收到的字节应丢弃（如何处理？提示：FIFO 会塞满，可考虑 `hdtx`）。
3. 写出「读数据」阶段：切换到 `hdrx` 模式，让硬件自动产生时钟把 32 字节读进 RX FIFO，CPU 再循环读 `RXDATA`。
4. 进阶：把第 2 步的「发命令+地址」改成用 DMA `Mem→Periph`(`dst_fixed`) 从 DTCM 缓冲一次发出，写出 DMA 描述符的关键字段。

要求：每一步都标注引用的源码行号或寄存器偏移，并指出哪些行为**待本地验证**。

> 这个任务把 4.2（SpiMaster 寄存器与半双工模式）、4.3（理解桥的方向，避免误用 Spi2TLULV2）、4.4（DMA 协作）三者串起来，是检验「是否真分清两个 SPI 模块」的试金石。

## 6. 本讲小结

- CoralNPU 有**两个** SPI 模块，身份相反：`SpiMaster` 是 **TL-UL 从机**（CPU 寻址它去当 SPI 主机）；`Spi2TLUL(V2)` 是 **TL-UL 主机**（外部 SPI 主机经它读写 SoC 内存）。
- `SpiMaster` 的寄存器映射 `STATUS/CONTROL/TXDATA/RXDATA/CSID/CSMODE` 才是「映射成 TL-UL 地址」的那一组；它用深 4 的 TX/RX FIFO 解耦总线与慢速 SPI 时序，状态机 `sIdle→sSetup→sShift→sFinish` 发送/接收字节，支持 CPOL/CPHA 与 `hdtx/hdrx` 半双工模式。
- `SpiMaster` 用 `TlulFifoAsync` 做系统时钟域到 `spi_clk` 域的 CDC；控制器整体运行在 SPI 时钟域。
- `Spi2TLULV2` 没有寄存器映射，而是定义了一套 SPI **帧协议**（op+addr+len+data），由 SPI 域解析成 `DmaDesc`，再由 TL-UL 域的 DMA 引擎发 16 字节 beat 的 `Get`/`PutFullData` 事务，两域之间用三条 `AsyncQueue` 跨时钟域。
- DMA 的 `Mem→Periph`(`dst_fixed`) 模式天然适配「SRAM→SPI TX FIFO」：把目的地址固定到 `TXDATA`，配合 `poll` 轮询 `STATUS` 的 TX Full 位即可防溢出——这正是 `SpiMaster` 与 u8-l1 DMA 引擎的协作点。

## 7. 下一步学习建议

- **u8-l4（CLINT 与 PLIC 中断）**：若想让 SPI「发完一段就通知 CPU」，需要把 SPI 的状态变化接到中断控制器，理解 PLIC 的中断源/claim 流程后，可思考 SPI 如何成为一个中断源。
- **u8-l5（外设接口抽象与 GPIO）**：想新增一个像 `SpiMaster` 这样的 TL-UL 从机外设？去读 `PeripheralInterface` 与 GPIO，掌握「寄存器映射 + 接入 socket」的标准模板。
- **继续精读源码**：对照 [SpiMasterTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/SpiMasterTest.scala) 与 [Spi2TLULV2Test.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/Spi2TLULV2Test.scala) 验证你对状态机与帧协议的理解；并阅读 `sw/utils/nexus_loader/spi_master.cc` 看 CPU 侧驱动如何使用这套寄存器。
