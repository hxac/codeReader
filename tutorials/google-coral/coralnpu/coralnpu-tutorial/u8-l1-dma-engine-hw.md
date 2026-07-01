# DMA 引擎硬件

## 1. 本讲目标

CoralNPU 是一台 ML 加速器，推理过程中要把成 MB 的模型权重从 SRAM/DDR 搬进 TCM，还要把外设（SPI、I2C）的数据流式搬进内存。如果这些搬运全靠标量核一条条 `lw`/`sw` 指令完成，CPU 会被「搬运工」的活儿占满而无法做计算。DMA（Direct Memory Access）引擎就是专门解放 CPU 的硬件搬运工。

学完本讲，你应该能够：

- 说清 DMA 引擎为什么需要**两个 TL-UL 端口**（128 位 host + 32 位 device），它们各自承担什么角色。
- 对照寄存器表，在源码里定位 `CTRL/STATUS/DESC_ADDR/CUR_DESC/XFER_REMAIN` 五个 CSR，并解释每个位域的含义。
- 读懂**链表描述符**（`src_addr/dst_addr/len_flags/next_desc` 加 `poll` 三元组）的内存格式，理解 Mem→Mem、Mem→Periph、Periph→Mem 三类传输模式。
- 跟着 ChiselEnum 状态机，画出一次 Mem→Mem 链表传输在硬件中的完整状态流转。
- 理解 `poll_addr/poll_mask/poll_value` 如何实现「无侵入」的外设流控，把 DMA 的速度自动适配到 SPI/I2C 时钟。

## 2. 前置知识

本讲依赖你在 u3 单元建立的 TileLink-UL 与 Crossbar 认知。这里只做最简回顾，不重复展开。

- **TL-UL（TileLink-UL）**：CoralNPU SoC 内部总线协议，只有 A（请求）和 D（响应）两个通道，经 `Decoupled`（valid/ready/bits）握手传输。请求类型主要是 `Get`（读，opcode=4）和 `PutFullData`（写，opcode=0）；响应对应是 `AccessAckData`（带数据）和 `AccessAck`（无数据）。详见 [TileLinkUL.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L22-L26)。

- **host 与 device（master 与 subordinate）**：发起事务的一方叫 host/master，响应的一方叫 device/subordinate。一个模块可以同时是 host（去访问别人）和 device（被别人访问）。

- **Crossbar / Socket**：u3-l4 讲过，SoC 用 `TlulSocket1N`/`TlulSocketM1` 把多个 host 路由到多个 device。DMA 既要作为 host 去读写 SRAM/外设，又要作为 device 被 CPU 编程，所以它在 Crossbar 两侧都占有一席之地。

- **CSR（Control & Status Register）**：CPU 通过读写一段内存映射的寄存器来控制和观测硬件模块。GPIO、SPI 这些简单外设的 TL-UL 从机本质上就是一堆 CSR，DMA 的 device 端口沿用了同一套模式。

- **描述符（descriptor）**：一段放在内存里、描述「从哪搬到哪、搬多少」的数据结构。CPU 把描述符准备好，再让 DMA 自己去读并执行。把多个描述符用指针串起来就是「链表」。

> 关键直觉：CPU 是「指挥官」，DMA 是「执行搬运的下级军官」。CPU 下达一次命令（写 `DESC_ADDR` + `CTRL.start`），DMA 就能独立完成一长串搬运，CPU 这期间可以去做别的。这就是「offload」（卸载）的含义。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [doc/peripherals/dma.md](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md) | DMA 引擎的设计文档：架构、寄存器表、描述符格式、状态机图、流控示例。本讲的「规格说明书」。 |
| [hdl/chisel/src/bus/DmaEngine.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala) | DMA 引擎的全部 RTL 实现：CSR 寄存器、描述符锁存、FSM、host/device 双端口驱动。本讲精读的核心。 |
| [hdl/chisel/src/bus/DmaEngineTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala) | Chisel 仿真测试：含一个响应式内存模型，覆盖 CSR 访问、Mem→Mem、链表、固定地址、abort 等场景，是理解硬件行为的最佳「活教材」。 |
| [hdl/chisel/src/soc/CrossbarConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala) | DMA 在 SoC 地址映射与互连中的注册：基址 `0x40050000`、host 连接清单。 |
| [hdl/chisel/src/soc/SoCChiselConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala) | `DmaParameters(hostDataBits=128, deviceDataBits=32)` 的定义与模块装配。 |

## 4. 核心概念与源码讲解

本讲按 4 个最小模块组织：先看双端口架构，再看描述符与三类传输模式，接着是 CPU 可见的 CSR，最后把整条执行主线——状态机（含外设流控）——走一遍。

### 4.1 双端口架构与总线集成

#### 4.1.1 概念说明

DMA 引擎是一个**单通道、链表描述符**控制器（single-channel, linked-list descriptor DMA）。它只搬运一路数据流，但这一路可以由任意多个描述符串成。设计文档的第一段就点明了它的两个总线角色：

> The DMA engine is a single-channel, linked-list descriptor DMA controller that offloads bulk data movement from the CPU. It connects to the existing TileLink-UL crossbar as both a **host** (master, for read/write transactions) and a **device** (slave, for CPU programming via CSRs).
> —— [dma.md:3-6](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L3-L6)

为什么要**两个**端口，而且宽度不同？

- **Host 端口（128 位）**：DMA 要搬「大块」数据，所以走与 Crossbar 同宽的 128 位（16 字节）通道，一拍能搬 16 字节，吞吐高。所有「读源、写目的、读描述符、轮询外设」都从这一个端口出去——它是 DMA 唯一的「手脚」。

- **Device 端口（32 位）**：CPU 编程 DMA 只需要写几个 32 位 CSR，没必要拉一条 128 位通道。所以 device 端口做成 32 位，和 GPIO/SPI 这类简单外设完全同构。它是 DMA 的「耳朵」——听 CPU 的命令。

两个端口宽度不同，所以 `DmaEngine` 接收两份独立的 `Parameters`：[DmaEngine.scala:22-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L22-L29) 分别构造 `hostTlulP`（w=16 字节）和 `deviceTlulP`（w=4 字节）。`TLULParameters` 里的 `w = axi2DataBits/8`（总线字节宽）、`z = log2Ceil(w)`、`o = axi2IdBits` 这三个派生量（[TileLinkUL.scala:22-26](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/TileLinkUL.scala#L22-L26)）会在后面字节通道对齐时反复用到。

#### 4.1.2 核心流程

DMA 在 SoC 里的接入关系（来自 CrossbarConfig，已逐行核对）：

```
CPU(coralnpu_core) ──device端口──► DMA CSR(0x40050000)        [编程/查询]
DMA host端口 ──► sram, ddr_mem, rom, spi_master, gpio,
                   i2c_master, uart0/1, coralnpu_device ...   [取指/搬运/轮询]
```

- DMA device 端口占据 `0x40050000–0x40050FFF`（4 KB），紧跟 I2C（`0x40040000`）之后——见 [CrossbarConfig.scala:117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L117)。
- CPU 能访问到 DMA：`"coralnpu_core" -> Seq(..., "dma", ...)`（[CrossbarConfig.scala:127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L127)）。
- DMA host 能访问到的设备清单：`"dma" -> Seq("sram", "coralnpu_device", "rom", ...)`（[CrossbarConfig.scala:129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L129)）。

关键参数（[dma.md:34-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L34-L41)）：单描述符最多搬 16 MB（24 位长度域）；同时只允许 1 笔未完成事务（source id 恒为 0）；v1 无中断，CPU 靠轮询 `STATUS`。

#### 4.1.3 源码精读

模块的对外接口就两个 TL-UL 端口，方向相反：

```scala
val io = IO(new Bundle {
  val tl_host   = new OpenTitanTileLink.Host2Device(hostTlulP)        // DMA 当主机：发起 Get/Put
  val tl_device = Flipped(new OpenTitanTileLink.Host2Device(deviceTlulP)) // DMA 当从机：被 CPU 编程
})
```

—— [DmaEngine.scala:26-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L26-L29)。注意 `tl_device` 用了 `Flipped`：从 CPU 视角它是 Host2Device，但从 DMA 视角它被驱动，方向翻转。

装配侧（SoCChiselConfig）用一句话把这两个端口挂到 Crossbar 的同名节点上：

```scala
name = "dma",
moduleClass = "bus.DmaEngine",
params = DmaParameters(hostDataBits = 128, deviceDataBits = 32),
hostConnections   = Map("io.tl_host"   -> "dma"),
deviceConnections = Map("io.tl_device" -> "dma"),
```

—— [SoCChiselConfig.scala:211-217](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L211-L217)。`DmaParameters` 定义见 [SoCChiselConfig.scala:67-70](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L67-L70)。

为了把 FSM 与 Crossbar 解耦，四个通道各自套了一个深度 1 的 `Queue`：[DmaEngine.scala:431-434](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L431-L434)（host A/D）、[DmaEngine.scala:446](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L446)（device A）、[DmaEngine.scala:463](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L463)（device D）。这样 FSM 看到的是 `host_a_internal` 等内部信号，节奏由自己掌控。

#### 4.1.4 代码实践

1. **实践目标**：确认双端口的「身份」与宽度。
2. **操作步骤**：
   - 打开 [DmaEngine.scala:22-29](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L22-L29)，找到 `tl_host` 与 `tl_device`，注意谁带 `Flipped`。
   - 打开测试 [DmaEngineTest.scala:23-28](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L23-L28)，看 `hostP.lsuDataBits = 128`、`deviceP.lsuDataBits = 32`。
   - 在 [CrossbarConfig.scala:117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L117) 确认 DMA 4 KB 地址窗口；在 [CrossbarConfig.scala:129](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CrossbarConfig.scala#L129) 看 host 能去哪些设备。
3. **需要观察的现象**：host 端口宽（128 位、能访问一堆设备），device 端口窄（32 位、只接 CPU）。
4. **预期结果**：你能用一句话回答「为什么 DMA 需要两个宽度不同的端口」——一个追求搬运吞吐，一个贴合 CPU 编程。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 DMA 一次搬更多数据以提升吞吐，最该调宽哪个端口？为什么 device 端口保持 32 位即可？
**答案**：调宽 host 端口（已是最宽的 128 位，对齐 Crossbar）；device 端口只是给 CPU 写几个 CSR，32 位正好放下一个寄存器，加宽纯属浪费面积。

**练习 2**：host A/D 通道为什么各加一个 `Queue(io, 1)`？
**答案**：把 FSM 与 Crossbar 的时序解耦——FSM 发出 A 后可立即进入「等 D」状态，无需关心 Crossbar 何时 ready；D 通道也缓冲一拍，避免响应丢失。

### 4.2 描述符格式、链表与三类传输模式

#### 4.2.1 概念说明

DMA 不会自己「想」要搬什么，一切由内存里的**描述符**决定。一个描述符长 32 字节，描述一次完整的搬运：从 `src_addr` 搬到 `dst_addr`，搬 `xfer_len` 字节，每个 beat 多宽，是否固定源/目的地址，要不要轮询外设。多个描述符用 `next_desc` 指针串成链，`next_desc == 0` 表示链尾。

设计文档把描述符布局写得非常清楚（[dma.md:66-81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L66-L81)）：

```
偏移    字段          位域       含义
0x00    src_addr      [31:0]     源地址
0x04    dst_addr      [31:0]     目的地址
0x08    xfer_len      [23:0]     传输字节数
        xfer_width    [26:24]    beat 大小：log2(字节)。0=1B,1=2B,2=4B,3=8B,4=16B
        flags         [31:27]    [27]src_fixed [28]dst_fixed [29]poll_en
0x0C    next_desc     [31:0]     下一个描述符地址(0=链尾)
0x10    poll_addr     [31:0]     要轮询的状态寄存器地址(0=不轮询)
0x14    poll_mask     [31:0]     屏蔽掩码
0x18    poll_value    [31:0]     (读值 & mask) 期望等于它
0x1C    reserved      [31:0]     保留
```

`src_fixed`/`dst_fixed` 两个标志位决定了**三类传输模式**（[dma.md:28-33](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L28-L33)）：

| 模式 | src_addr | dst_addr | 典型用途 |
|------|----------|----------|----------|
| Mem→Mem | 递增 | 递增 | SRAM↔DDR、SRAM→ITCM/DTCM |
| Mem→Periph | 递增 | 固定 | SRAM→SPI TX FIFO |
| Periph→Mem | 固定 | 递增 | I2C RX→SRAM |

> 直觉：源/目的「固定」时，每搬一个 beat 地址不增加，于是反复读写同一个寄存器——这正是往 FIFO 灌数据或从 FIFO 取数据的语义。

#### 4.2.2 核心流程

描述符在内存里是普通字节，但 DMA 通过 host 端口把它当 128 位数据读进来后，要按位域「拆开」。因为 32 字节 = 2 个 128 位 beat，所以取描述符分两拍：

```
FETCH_DESC_0：Get 16 字节 @ desc_addr      → 拿到 src/dst/len_flags/next_desc
FETCH_DESC_1：Get 16 字节 @ desc_addr+16   → 拿到 poll_addr/poll_mask/poll_value
```

Chisel 用 `asTypeOf` 把 128 位原始数据重新解释成命名字段。`DmaDescriptorPart0` 把 beat0 的字段从 MSB 到 LSB 排列（[DmaEngine.scala:86-96](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L86-L96)），`DmaDescriptorPart1` 对应 beat1（[DmaEngine.scala:98-103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L98-L103)）。

链表推进逻辑很简单：当一个描述符的 `remaining` 减到 0 时，看 `next_desc`——非 0 就把 `xfer.desc_addr` 换成 `next_desc`，回到 `FETCH_DESC_0` 取下一段；为 0 就 `DONE`。

#### 4.2.3 源码精读

把 32 字节描述符的所有字段汇总到一个寄存器 bundle `DmaDescriptor`（[DmaEngine.scala:72-84](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L72-L84)），它是 FSM 全程引用的「当前任务卡」。取指时把两个 beat 的原始数据分别按 `Part0`/`Part1` 拆开，再拷进 `desc`：

```scala
(state === sFetchDesc0Resp && host_d_fire && !host_d_err) -> {
  val b  = WireInit(desc)
  val d0 = host_d_internal.bits.data.asTypeOf(new DmaDescriptorPart0)
  b.src_addr := d0.src_addr; b.dst_addr := d0.dst_addr
  b.xfer_len := d0.xfer_len; b.xfer_width := d0.xfer_width
  b.src_fixed := d0.src_fixed; b.dst_fixed := d0.dst_fixed
  b.poll_en := d0.poll_en; b.next_desc := d0.next_desc
  b
}
```

—— [DmaEngine.scala:276-288](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L276-L288)（beat1 同理在 [DmaEngine.scala:289-297](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L289-L297)）。

链表推进在写回完成那一拍处理，把 `desc_addr` 切到 `next_desc`：

```scala
b.desc_addr := Mux(
  new_remaining === 0.U && desc.next_desc =/= 0.U,
  desc.next_desc,
  xfer.desc_addr
)
```

—— [DmaEngine.scala:326-330](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L326-L330)。

源/目的地址是否递增也由 fixed 标志位控制（[DmaEngine.scala:323-324](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L323-L324)）：`src_fixed` 为真则 `src_addr` 不动，否则加 `beat_bytes`，`dst_addr` 同理。这就是三类传输模式落到硬件的全部实现——**没有专门的「模式」枚举，只有两个固定地址标志位**。

测试里的 `buildDescriptor` 是把上述位域「打包」成 32 字节内存的参考实现，对照阅读最能固化理解：[DmaEngineTest.scala:79-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L79-L107)。注意 `flags` 字段的位拼装顺序与文档表完全一致。

#### 4.2.4 代码实践

1. **实践目标**：把文档的描述符位域表与源码的 bundle 字段一一对应。
2. **操作步骤**：
   - 对照 [dma.md:70-81](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L70-L81) 与 [DmaDescriptorPart0](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L86-L96)，在纸上把 `xfer_width[26:24]`、`src_fixed[27]`、`dst_fixed[28]`、`poll_en[29]` 这些位标注到 32 位整数上。
   - 看 [DmaEngineTest.scala:96-101](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L96-L101) 的 `flags` 拼装，确认位移量与位域表吻合。
3. **需要观察的现象**：bundle 字段从 MSB 到 LSB 的声明顺序，恰好就是内存里从高位到低位的排列。
4. **预期结果**：你能手算出一个「12 字节、4 字节 beat、目的地址固定」的描述符的 `0x08` 字节内容（参考测试 [DmaEngineTest.scala:336](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L336)：`xferLen=12, xferWidth=2, dstFixed=true` → `0x10_0000_0c`，即 bit28 置 1、width=2、len=0xc）。

#### 4.2.5 小练习与答案

**练习 1**：一个描述符要搬 16 MB，`xfer_len` 域够用吗？为什么上限是 16 MB？
**答案**：刚好够。`xfer_len` 是 24 位，\(2^{24} = 16{,}777{,}216\) 字节 = 16 MB，正好是上限（[dma.md:38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L38)）。要搬更多就用链表接下一个描述符。

**练习 2**：Mem→SPI TX FIFO 属于哪类模式？靠哪几个字段实现？
**答案**：Mem→Periph。靠 `dst_fixed=1`（目的固定为 TXDATA 寄存器地址）+ `src_fixed=0`（源逐 beat 递增）实现，无需任何「模式寄存器」。

### 4.3 CSR 寄存器映射与编程模型

#### 4.3.1 概念说明

CPU 通过 device 端口的 5 个 CSR 来驾驶 DMA。寄存器表见 [dma.md:44-53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L44-L53)：

| 偏移 | 名字 | 访问 | 含义 |
|------|------|------|------|
| `0x00` | CTRL | RW | [0]enable [1]start(写 1 自清) [2]abort |
| `0x04` | STATUS | RO | [0]busy [1]done [2]error [7:4]error_code |
| `0x08` | DESC_ADDR | RW | 第一个描述符地址 |
| `0x0C` | CUR_DESC | RO | 当前正在执行的描述符地址 |
| `0x10` | XFER_REMAIN | RO | 当前传输剩余字节数 |

CPU 编程 DMA 的标准序列只有 5 步（[dma.md:56-62](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L56-L62)）：在内存里建好描述符链 → 写 `DESC_ADDR` → 写 `CTRL`(enable=1,start=1) → 轮询 `STATUS.done` → 检查 `STATUS.error`。

两个细节值得注意：
- `start` 是 **W1S（write-1-to-set）自清位**——写 1 触发一次启动，硬件下一拍自动清零，避免重复触发。
- `STATUS` 的 `error_code` 把错误来源编码成 4 位：1=描述符取指错、2=轮询错、3=读数据错、4=写数据错、5=abort。这让 CPU 能精确定位故障。

#### 4.3.2 核心流程

device 端口的 CSR 处理遵循 GPIO 那套成熟模式（[dma.md:197-202](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L197-L202)）：

```
CPU 写 CSR：tl_a.fire 时按 address[11:0] 译码 → 命中可写寄存器则更新
CPU 读 CSR：根据地址 mux 出对应寄存器值 → 走 AccessAckData 返回
非法地址或写只读寄存器 → 返回 error(AccessAck/AccessAckData 带 error=1)
```

`error_code` 的生成与「出错即停」是绑定的：FSM 在任何「等 D」状态发现 `host_d_err`，都立刻置 `status.error` 并按当前状态写 `error_code`，然后跳 `DONE`。

#### 4.3.3 源码精读

CSR 偏移用 `ChiselEnum` 集中声明，既是地址也是枚举名（[DmaEngine.scala:38-48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L38-L48)）。注意末尾那个 `RSVD = 0xfff` 不是真寄存器——注释解释它是为了防止 ChiselEnum 把宽度塌缩（[DmaEngine.scala:44-47](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L44-L47)），好让所有枚举值保持 12 位宽，对齐 `address[11:0]`。

`CTRL` 的写处理体现「start 自清」语义（[DmaEngine.scala:202-223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L202-L223)）：一旦 `start_condition`（start && enable）成立或 `start && !enable`，下一拍就把 `start` 拉回 false。

`STATUS` 的拼装把位域摆好（[DmaEngine.scala:340-342](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L340-L342)）：

```scala
val status_reg_val =
  Cat(0.U(24.W), status.error_code, 0.U(1.W), status.error, status.done, status.busy)
// 位：[7:4]error_code [3]reserved [2]error [1]done [0]busy
```

`error_code` 在出错那拍按当前 FSM 状态查表生成（[DmaEngine.scala:250-263](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L250-L263)）：

```scala
sFetchDesc0Resp -> 1.U, sFetchDesc1Resp -> 1.U,  // 描述符取指错
sPollResp       -> 2.U,                          // 轮询错
sXferReadResp   -> 3.U,                          // 读数据错
sXferWriteResp  -> 4.U                           // 写数据错
```

device 端口的读返回逻辑（[DmaEngine.scala:344-371](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L344-L371)）用 `MuxLookup(dev_addr_reg, 0.U)` 把地址映射到对应寄存器值，并把「非法地址」与「写只读寄存器」都判为 `error := true`（[DmaEngine.scala:354](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L354)），其中只读判定 `is_ro_reg` 覆盖 `STATUS/CUR_DESC/XFER_REMAIN`（[DmaEngine.scala:337](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L337)）。

测试 "CSR Register Access" 与 "CSR Error on Invalid Address"、"CSR Error on Write to Read-Only Register" 三例正好验证了上述读写与报错语义，建议对照阅读：[DmaEngineTest.scala:193-239](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L193-L239)。

#### 4.3.4 代码实践

1. **实践目标**：在源码里逐一锁定 5 个 CSR 的偏移与读写权限。
2. **操作步骤**：
   - 在 [DmaEngine.scala:38-48](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L38-L48) 找到 5 个偏移值，对照 [dma.md:46-53](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L46-L53) 的表。
   - 在 [DmaEngine.scala:337](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L337) 找出哪些寄存器是只读，解释为什么 `DESC_ADDR` 可写而 `CUR_DESC` 只读。
3. **需要观察的现象**：`DESC_ADDR` 是 CPU 设的「起点」，`CUR_DESC` 是硬件运行中「走到哪了」的实时镜像——前者必须可写，后者只能观测。
4. **预期结果**：你能写出驱动 DMA 的最小 C 序列伪代码（写 `DESC_ADDR` → 写 `CTRL=0x3` → 轮询 `STATUS&0x2`），并解释 `0x3` = enable+start。

#### 4.3.5 小练习与答案

**练习 1**：CPU 读 `STATUS` 得到 `0x36`，解读每一位。
**答案**：`0x36 = 0b0011_0110`。bit[1]done=1、bit[2]error=1、bit[5:4]error_code=`0b11`=3，即「传输完成，但发生读数据错误（error_code=3）」。busy(bit0)=0。

**练习 2**：为什么向 `STATUS`（只读）写会在 D 通道返回 error，而不会改掉 busy/done？
**答案**：[DmaEngine.scala:354](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L354) 把 `dev_is_write && is_ro_reg` 判为 `error := true`；而 `status` 寄存器的更新逻辑（[DmaEngine.scala:225-265](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L225-L265)）只响应 FSM 状态迁移，从不响应 device 端口的写，所以写只读寄存器只回报错误、不污染状态。

### 4.4 取指-轮询-读写状态机（含外设流控）

#### 4.4.1 概念说明

DMA 的全部行为浓缩在一个 ChiselEnum 状态机里。设计文档的 ASCII 状态图（[dma.md:124-152](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L124-L152)）把概念状态画得很清楚；源码为了精确控制「发请求」与「收响应」两个不同时刻，把每个概念态拆成 `XxxReq`（发 A）/`XxxResp`（收 D）两态。

外设流控（peripheral flow control）是状态机里最巧妙的一环。SPI/I2C 这类外设有「TX FIFO 满」「RX 有数据」之类的状态位，DMA 不能不管不顾地猛灌——得等外设就绪。CoralNPU 的做法是**描述符级轮询**：描述符里带 `poll_addr/poll_mask/poll_value` 三元组，每搬一个数据 beat 之前，DMA 先读 `poll_addr`，直到 `(读值 & poll_mask) == poll_value` 才继续搬。这样**完全不需要修改外设**就能把 DMA 速度自动适配到外设时钟（[dma.md:83-92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L83-L92)）。

#### 4.4.2 核心流程

源码里的 13 个状态（[DmaEngine.scala:52-56](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L52-L56)）按一次完整搬运串起来：

```
sIdle ──start──► sFetchDesc0 ──a.fire──► sFetchDesc0Resp ──d.fire──► sFetchDesc1
   ──a.fire──► sFetchDesc1Resp ──d.fire──► sPollCheck
        │  (poll_en && poll_addr≠0)
        └──────────────► sPollReq ◄──────┐
                             │a.fire      │ 不匹配
                             ▼            │
                          sPollResp ──────┘
                             │匹配
        ┌────────────────────┴─────────────── (无 poll 则直接到此)
        ▼
   sXferReadReq ──► sXferReadResp ──► sXferWriteReq ──► sXferWriteResp
        │
        ├── remaining>0 ──► 回 sPollCheck（搬下一个 beat）
        ├── remaining==0 且 next_desc≠0 ──► sFetchDesc0（取下一段描述符）
        └── remaining==0 且 next_desc==0 ──► sDone ──► sIdle
   任意状态 host_d_err ──► sDone（带 error_code）
   任意状态 abort ──► sIdle（error_code=5）
```

每个数据 beat 的搬运是一个「读-缓冲-写」的小流水：`sXferReadReq` 发 Get，`sXferReadResp` 把数据存进 `data_buf`，`sXferWriteReq` 发 PutFullData，`sXferWriteResp` 更新地址与剩余量。`beat_bytes = 1 << xfer_width`（[DmaEngine.scala:153](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L153)），每完成一个 beat，`remaining` 减 `beat_bytes`（[DmaEngine.scala:154](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L154)）。

**字节通道对齐**是读写的难点。host 总线 128 位（16 字节），但 `Get` 的地址未必 16 字节对齐——TL-UL 的 D 通道永远返回整条 128 位总线字，有效字节落在对应 lane 里。所以读响应要「右移」把目标字节拉到低位、写请求要「左移」把数据推到目标 lane：

\[ \text{laneShift} = (\text{addr} \bmod 16) \times 8 \text{ bit} \]

#### 4.4.3 源码精读

状态机下一态用一张 `MuxCase` 表完整描述（[DmaEngine.scala:165-198](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L165-L198)），逐行就是上面的流程图。其中轮询匹配判定（[DmaEngine.scala:176-187](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L176-L187)）是外设流控的核心：

```scala
(state === sPollResp && host_d_fire) -> Mux(
  host_d_err, sDone,
  Mux(
    ((host_d_internal.bits.data >> (desc.poll_addr(log2Ceil(hostTlulP.w) - 1, 0) << 3))(31, 0)
      & desc.poll_mask) === desc.poll_value,
    sXferReadReq,   // 匹配 → 可以搬数据
    sPollReq        // 不匹配 → 再轮询一次
  )
)
```

`poll_addr(3,0)` 取地址在 16 字节总线字内的字节偏移，`<<3` 转成比特，右移后取低 32 位，正是把 polled 寄存器值抽到低位再与 `poll_mask`/`poll_value` 比较。

数据读的字节对齐（[DmaEngine.scala:315-320](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L315-L320)）：

```scala
b.data_buf := host_d_internal.bits.data >> (xfer.src_addr(log2Ceil(hostTlulP.w) - 1, 0) << 3)
```

数据写则反向左移（[DmaEngine.scala:424](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L424)）。host A 通道的 opcode/size/address/mask 全部按当前状态 mux 出来（[DmaEngine.scala:384-423](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L384-L423)）——一张总线被描述符取指、轮询、读、写四种用途时分复用。

要直观感受状态机，看测试里的响应式内存模型 `runDmaWithMemory`（[DmaEngineTest.scala:117-189](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L117-L189)）：它捕获 host 端口的每个 A 请求，按 opcode 回放 Get（读内存、按 lane 摆放）或 PutFullData（写内存、按 lane 提取），再喂回 D 响应。这正是 DMA 视角下「Crossbar + 各从机」的简化替身。"Simple Mem-to-Mem Transfer"（[DmaEngineTest.scala:241-276](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L241-L276)）就是用 16 字节、4 字节 beat 跑通一次搬运；"Descriptor Chaining"（[DmaEngineTest.scala:278-321](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L278-L321)）验证链表；"Abort Transfer"（[DmaEngineTest.scala:352-385](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L352-L385)）验证任意状态可被 `abort` 拉回 `sIdle` 并置 error。

#### 4.4.4 代码实践

1. **实践目标**：画出一次 Mem→Mem 链表传输在硬件中的状态流转，并标注每个状态发出的 TL-UL 事务。
2. **操作步骤**：
   - 取测试 [DmaEngineTest.scala:241-276](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L241-L276) 的场景：16 字节、4 字节 beat、源 `0x1000`、目的 `0x3000`。
   - 在 [DmaEngine.scala:165-198](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L165-L198) 逐拍推演：`sIdle→sFetchDesc0→…→sPollCheck→sXferReadReq→…`。
   - 注意 `sPollCheck` 因 `poll_en=0` 直接跳到 `sXferReadReq`（[DmaEngine.scala:174](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L174)）。
3. **需要观察的现象**：16 字节、4 字节 beat → 4 个数据 beat；每个 beat 走 `POLL_CHECK→READ_REQ→READ_RESP→WRITE_REQ→WRITE_RESP` 共 5 个状态（无 poll 时 POLL_CHECK 一拍即过）；第 4 个 beat 后 `remaining==0` 且 `next_desc==0` → `sDone`。
4. **预期结果**：你在纸上能数出这次传输共产生 2（取描述符）+ 4×2（读+写）= 10 个 host 事务，并标注每个事务的 opcode/size/address。**待本地验证**：用 `bazel test` 跑 `DmaEngineSpec`，对照波形确认状态翻转与事务计数。

#### 4.4.5 小练习与答案

**练习 1**：源码的状态比文档 ASCII 图多了一倍（多了 `…Resp` 态），为什么？
**答案**：因为发 TL-UL 请求（A 通道 fire）和收响应（D 通道 fire）发生在不同时钟沿，必须用两个状态分别等待 `host_a_fire` 与 `host_d_fire`，才能精确驱动 `host_a_internal.valid` 与 `host_d_internal.ready`（[DmaEngine.scala:376-382](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L376-L382) 与 [DmaEngine.scala:437-443](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L437-L443)）。

**练习 2**：DMA→SPI TX 的描述符里 `poll_mask=0x4`、`poll_value=0x0` 表示什么？匹配时 DMA 做什么？
**答案**：`0x4` 是 SPI STATUS 的「TX Full」位（[dma.md:101-103](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/peripherals/dma.md#L101-L103)）。`(status & 0x4) == 0` 表示 TX 未满；匹配后 DMA 才读一个源字节、写进 TXDATA，于是搬运节奏自动跟上 SPI 时钟。

**练习 3**：为什么 `abort` 能从任意状态回到 `sIdle`？这与 `error_code=5` 有什么关系？
**答案**：FSM 的 `MuxCase` 第一条就是 `abort_condition -> sIdle`（[DmaEngine.scala:168](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L168)），优先级最高，覆盖所有当前态；同时 `status` 更新把 `error:=true`、`error_code:=5`（[DmaEngine.scala:228-235](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngine.scala#L228-L235)），让 CPU 能从 STATUS 区分「abort」与「总线错」。

## 5. 综合实践

把本讲四个模块串成一个完整任务：**手工推演一次 Mem→Mem 链表传输，并设计它的描述符内存**。

设定：把 SRAM 中 `0x1000` 起的 8 字节搬到 `0x3000`，再把 `0x1100` 起的 8 字节搬到 `0x3100`，4 字节 beat，无轮询。这就是测试 "Descriptor Chaining" 的场景（[DmaEngineTest.scala:278-321](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L278-L321)）。

请完成：

1. **写描述符内存**：参考 `buildDescriptor`（[DmaEngineTest.scala:79-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L79-L107)），手算两个描述符（分别在 `0x2000`、`0x2020`）每个字节的值。关键点：第一个描述符的 `next_desc=0x2020`，第二个的 `next_desc=0`（链尾）；两者的 `flags` 字段 = `(2<<24) | 8`（width=2，len=8，无 fixed/poll）。
2. **写驱动序列**：用 C 伪代码写出 CPU 侧的 CSR 操作——写 `DESC_ADDR=0x2000`、写 `CTRL=0x3`、循环读 `STATUS` 直到 `done`。
3. **画状态流转图**：在纸上画出 DMA 从 `sIdle` 到第二次 `sDone` 的完整状态序列，标注每个 `sXferReadReq`/`sXferWriteReq` 发出的地址（注意 4 字节 beat 下地址每次 +4）。
4. **核对**：你的状态序列里应该出现两次 `sFetchDesc0`（两个描述符各取一次），每个描述符各 2 个数据 beat（8 字节 / 4 字节）。若手算结果与测试断言（[DmaEngineTest.scala:305-319](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala#L305-L319)）一致，即通过。

> 进阶（选做）：把这个链表的第二个描述符改成 Mem→Periph 模式（`dst_fixed=1`，目的指向某个外设寄存器），并配上 `poll_addr/mask/value` 模拟等外设就绪，在状态图里加入 `sPollReq↔sPollResp` 的循环分支。

## 6. 本讲小结

- DMA 引擎是**单通道、链表描述符**搬运工，用**两个 TL-UL 端口**接入 SoC：128 位 host（追求吞吐）发 Get/Put，32 位 device（贴合 CPU）接受 CSR 编程。
- CPU 通过 5 个 CSR 驾驶它：`CTRL`(enable/start/abort) 启停、`STATUS`(busy/done/error/error_code) 观测、`DESC_ADDR` 给起点、`CUR_DESC`/`XFER_REMAIN` 实时进度；编程序列只有 5 步。
- 一次任务由内存中的**链表描述符**定义：`src/dst/len_flags/next_desc` 加 `poll` 三元组；`src_fixed`/`dst_fixed` 两个标志位就实现了 Mem→Mem、Mem→Periph、Periph→Mem 全部三类模式。
- 核心是一个 13 态 FSM：取描述符（2 beat）→（可选）轮询 → 读-缓冲-写 → 地址/剩余量更新 → 取下一段或 DONE；任意 `host_d_err` 带 error_code 跳 DONE，任意 `abort` 回 IDLE。
- 外设流控靠**描述符级轮询**：每搬一个 beat 前读 `poll_addr`，直到 `(读值 & mask) == value` 才继续，无需改动外设即可适配 SPI/I2C 时钟。
- host A 通道被取指/轮询/读/写**时分复用**，字节通道靠「读右移、写左移」在 128 位总线字内对齐。

## 7. 下一步学习建议

- **软件编程侧**：本讲只讲了硬件。下一篇 **u8-l2 DMA 软件编程指南** 会用 C 代码教你如何构造描述符、调用 `make_len_flags` 拼位域、轮询 `STATUS.done` 与处理 `error_code`，是从「读懂硬件」走向「会用硬件」的下一步。
- **外设协作侧**：DMA 的 Mem→Periph 模式要与 SPI 配合，建议接着读 **u8-l3 SPI 主机与 TL-UL 桥接**，看 SPI 的 TX/RX FIFO 与 STATUS 寄存器如何成为 DMA 轮询的目标。
- **深入源码**：若想验证你对 FSM 与字节对齐的理解，尝试用 `bazel test` 跑 `DmaEngineSpec`（[DmaEngineTest.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/bus/DmaEngineTest.scala)），并在波形里跟踪一次 Mem→Mem 传输的每个状态翻转。
