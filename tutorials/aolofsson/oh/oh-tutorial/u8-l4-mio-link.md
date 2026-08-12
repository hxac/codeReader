# mio 轻量级链路

## 1. 本讲目标

本讲是第 8 单元（AXI、DMA 与多链路系统）的最后一讲，承接第 7 单元的 elink 高速链路（u7-l1～u7-l4），讲解 OH! 仓库中 elink 的「轻量级替代品」——**mio（Mini-IO）**。

学完本讲，你应当能够：

- 说清 **mio 的定位**：一条源同步、协议无关、宽度可参数化的片间/芯间数据链路，用更宽、更简单的并行总线换取比 elink 更低的复杂度。
- 读懂 **mio 顶层 `mio.v`** 的端口划分（IO 侧 / 核侧 / 寄存器侧）与四大子模块（`mtx`、`mrx`、`mio_regs`、`oh_clockdiv`）的职责。
- 区分 **三种传输模式**：emode（emesh 包模式）、dmode（数据流模式）、amode（自动地址模式），并理解它们如何复用同一套 IO 硬件。
- 理解 **TX/RX 的对称分层**：`mtx_fifo`/`mtx_io` 与 `mrx_io`/`mrx_fifo` 如何互为逆过程，以及它们与 elink 的 etx/erx 在结构上的取舍差异。
- 养成「**代码即事实、文档可能滞后**」的阅读习惯——本模块存在多处文档与 RTL 不一致、未完成实现（readback 桩、未定义子模块），是练习这一原则的好样本。

## 2. 前置知识

本讲假设你已掌握以下内容（对应前置讲义）：

- **emesh 104 位事务包**（u5-l1）：`PW = 2·AW + 40`，包内含 write/datamode/dstaddr/srcaddr/data 等字段，配合 `access`/`wait` 握手。mio 的核侧用的就是这种包。
- **寄存器映射 `.vh` 模式**（u6-l1）：用 `` `MIO_CONFIG `` 这类大写宏给寄存器分配地址编号，再用 `dstaddr` 译码产生写选通。
- **elink 链路全貌**（u7-l1～u7-l4）：源同步时钟、LVDS 差分（`_p`/`_n`）、8 位 DDR 数据、FRAME 帧、TX/RX 各 wr/rd/rr 三通道、跨时钟域 FIFO。本讲会反复把 mio 与 elink 对照。
- **同步器与 CDC FIFO**（u2-l4、u3-l2）：`oh_dsync`/`oh_rsync` 打拍同步、`oh_fifo_cdc` 异步 FIFO。mio 的收发都靠它们跨时钟域。
- **`packet2emesh` / `emesh2packet` 的作用**（u5-l3、u6-l2）：在「扁平 104 位包」与「结构化字段」之间互转。注意：这两个模块在仓库中**没有定义**（见后文「工程现实」），mio 因此不能脱离仿真平台库替换直接编译。

补充两个本讲会用到的术语：

- **源同步（source synchronous）**：发送方把时钟和数据一起送上线（mio 的 `tx_clk` + `tx_packet`），接收方用送来的时钟采样数据，而不是各自用独立时钟。
- **DDR / SDR**：DDR（Double Data Rate）在时钟上升沿和下降沿各传一次数据；SDR（Single Data Rate）只在上升沿传。mio 通过一个配置位 `ddr_mode` 切换。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `mio/` 目录下：

| 文件 | 作用 | 是否被 `mio.v` 例化 |
|------|------|:---:|
| [mio/hdl/mio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v) | **顶层**：拼装 TX、RX、寄存器、TX 时钟分频 | （自身） |
| [mio/hdl/mtx.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx.v) | 发送（Transmit） datapath 顶层：例化 `mtx_fifo` + `mtx_io` | ✅ |
| [mio/hdl/mtx_fifo.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v) | TX 侧：把 104 位 emesh 包拆成 ≤64 位 IO 字 + 字节有效掩码，跨域缓冲 | ✅（经 mtx） |
| [mio/hdl/mtx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v) | TX 侧：移位寄存器串化 + `oh_oddr` DDR 输出 + wait 同步 | ✅（经 mtx） |
| [mio/hdl/mrx.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx.v) | 接收（Receive） datapath 顶层：例化 `mrx_io` + `mrx_fifo` | ✅ |
| [mio/hdl/mrx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v) | RX 侧：`oh_iddr` 解串 + 字节有效累积 + 打包成 64 位字 | ✅（经 mrx） |
| [mio/hdl/mrx_fifo.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v) | RX 侧：跨域缓冲 + emode 重组 / amode 包装修复 | ✅（经 mrx） |
| [mio/hdl/mio_regs.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v) | 寄存器堆：译码、配置位、状态、时钟分频/相位、amode 地址 | ✅ |
| [mio/hdl/mio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regmap.vh) | 寄存器地址宏定义（软硬件共同事实源） | ✅（include） |
| [mio/hdl/mio_if.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_if.v) | RX 侧包格式化帮手（MPW=128） | ❌ 备用/历史 |
| [mio/hdl/mio_dp.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_dp.v) | 另一套 datapath（用 NMIO 参数） | ❌ 备用/历史 |
| [mio/hdl/mrx_protocol.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_protocol.v) | 独立的 RX 拆帧状态机 + `oh_ser2par` | ❌ 备用/历史 |
| [mio/dv/dut_mio.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/dv/dut_mio.v) | 测试平台 DUT 包装（**回环**接法） | （仿真用） |
| [mio/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/dv/tests/test_basic.emf) | 基本测试激励（配置寄存器 + amode 数据流） | （仿真用） |
| [mio/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/README.md) | 模块说明（**部分内容与 RTL 不一致，见后文**） | — |

> ⚠️ 大纲里提到的 `mtx_protocol.v` **在本仓库中并不存在**。TX 侧的成帧/拆包逻辑被折叠进了 `mtx_fifo`（拆包）与 `mtx_io`（串化），没有独立的 `mtx_protocol`。这是阅读时要注意的第一个「文档/大纲与代码不符」之处。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

1. **mio 链路总体架构**（顶层 `mio.v` + 寄存器）
2. **三种传输模式** emode / dmode / amode
3. **TX/RX 对称分层**（`mtx` vs `mrx`，以及它们「不完全对称」的现实）

### 4.1 mio 链路总体架构

#### 4.1.1 概念说明

**Mini-IO（mio）是一条「协议无关」的源同步数据链路**，用于在芯片之间或硅芯片的裸芯（die）之间搬数据。它与 elink 的根本区别是定位不同：

- **elink**：追求**高速**——用 8 位 LVDS 差分对 + DDR，跑在很高频率，靠 SERDES 风格的成帧（FRAME）把一个 emesh 事务串行化成字节流。代价是结构复杂、收发各 6 个 emesh 通道、时钟域管理繁琐。
- **mio**：追求**轻量与灵活**——用一条**可参数化宽度**（`IOW`，默认 64 位）的**单端**并行总线一次性传一个宽字，支持 SDR/DDR、支持 LSB/MSB 先发、支持三种传输模式。核侧仍用 104 位 emesh 包，但系统侧只暴露**一对收发通道**（不像 elink 的六通道）。

一句话直觉：**elink 像「细管子、高流速」，mio 像「粗管子、低流速、可换协议」**。

#### 4.1.2 核心流程

`mio.v` 把四块拼起来，数据流如下（核侧 → IO 侧 → 对端 → 核侧）：

```
        核侧 (clk 域)                          IO 侧 (io_clk / rx_clk 域)
┌─────────────────────┐                ┌──────────────────────────┐
│  access_in/         │   104 位包      │  mtx: 拆字+跨域FIFO+串化   │
│  packet_in ─────────┼───────────────▶│  ──▶ tx_packet[IOW-1:0]   │──▶ tx_packet
│  wait_out           │                │      tx_access / tx_clk    │    (单端并行)
└─────────────────────┘                └──────────────────────────┘

┌─────────────────────┐                ┌──────────────────────────┐
│  access_out/        │   104 位包      │  mrx: 解串+跨域FIFO+重组   │
│  packet_out ◀───────┼───────────────│  ◀── rx_packet[IOW-1:0]   │◀── rx_packet
│  wait_in            │                │      rx_access / rx_clk    │
└─────────────────────┘                └──────────────────────────┘

   reg_access_in/packet_in ──▶ mio_regs（配置/状态/时钟/amode地址）──▶ reg_*_out
```

- **TX**：核侧 104 位包 → `mtx` 拆成若干 ≤64 位 IO 字（带字节有效掩码）→ `oh_fifo_cdc` 跨到 `io_clk` 域 → `mtx_io` 移位串化 + 可选 DDR → `tx_packet`/`tx_access`。
- **RX**：`rx_packet`/`rx_access` → `mrx_io` 用 `oh_iddr` 解串、按字节有效累积成 64 位字 → `oh_fifo_cdc` 跨到 `clk` 域 → `mrx_fifo` 按 emode/amode 重组为 104 位包 → 核侧。
- **TX 时钟**：`oh_clockdiv` 由核侧 `clk` 分频出 `io_clk`（给 TX 逻辑）和 `tx_clk`（相位偏移后送上线，使时钟落在数据眼中央）。
- **寄存器**：`mio_regs` 单独占一条 104 位包接口（`reg_access_in/...`），软件通过写寄存器选择模式、IO 宽度、DDR、时钟分频等。

#### 4.1.3 源码精读

**(1) 顶层参数与端口**。`mio.v` 的参数锁定了 mio 的「可参数化」本质——IO 宽度 `IOW`、地址宽 `AW`、包宽 `PW`、默认配置 `DEF_CFG`、默认分频 `DEF_CLK`、工艺目标 `TARGET`：

[mio/hdl/mio.v:7-13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L7-L13) —— 参数：`IOW=64`（IO 宽度，elink 固定 8 位 DDR，mio 默认 64 位）、`PW=104`（emesh 包宽）、`DEF_CFG=18'h0010`、`DEF_CLK=7`。

IO 侧端口（注意是**单端** `tx_packet`/`rx_packet`，而非 elink 的 `_p`/`_n` 差分对）：

[mio/hdl/mio.v:17-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L17-L25) —— `tx_clk`（相位偏移的 IO 时钟）、`tx_access`（IO 帧信号）、`tx_packet[IOW-1:0]`（IO 数据）、`tx_wait`（IO 反压）；`rx_clk`/`rx_access`/`rx_packet`/`rx_wait` 对称。

核侧端口（与 elink 系统侧的六通道不同，mio 只有**一对收发** + 寄存器通道）：

[mio/hdl/mio.v:27-39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L27-L39) —— 核侧 `access_in`/`packet_in[104]`/`wait_out`（TX）、`access_out`/`packet_out[104]`/`wait_in`（RX）；寄存器侧 `reg_access_in`/`reg_packet_in`/`reg_wait_out`/`reg_access_out`/`reg_packet_out`/`reg_wait_in`。

**(2) 四大子模块例化**。`mio.v` 用 `AUTOINST`（verilog-mode 标记，详见 u4-l3）把 `mtx`/`mrx`/`mio_regs`/`oh_clockdiv` 连起来：

[mio/hdl/mio.v:76-99](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L76-L99) —— 例化 `mtx`（发送），把核侧 `access_in`/`packet_in` 接入，输出 `tx_access`/`tx_packet`，回吐 `wait_out`；配置位 `tx_en`/`ddr_mode`/`lsbfirst`/`emode`/`iowidth` 来自 `mio_regs`。

[mio/hdl/mio.v:105-130](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L105-L130) —— 例化 `mrx`（接收），把 `rx_access`/`rx_packet` 接入，输出核侧 `access_out`/`packet_out`；注意它额外接了 `amode`/`ctrlmode`/`datamode`/`dstaddr`/`emode`——这些是 amode/emode 重组包时需要的配置（见 4.2）。

[mio/hdl/mio.v:141-177](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L141-L177) —— 例化 `mio_regs`，用 `AUTO_TEMPLATE` 把 `*_out`/`*_in` 重命名映射到 `reg_*` 前缀；它消费寄存器侧事务，吐出全部配置位与状态读取。

[mio/hdl/mio.v:183-198](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L183-L198) —— 例化 `oh_clockdiv`（u2-l3 讲过）产生 `io_clk`（`clkout0`）与 `tx_clk`（`clkout1`，相位偏移版），由 `tx_en` 使能、`clkdiv`/`clkphase0/1` 配置。

**(3) 寄存器映射**。地址宏定义在 `.vh`（u6-l1 的标准模式）：

[mio/hdl/mio_regmap.vh:4-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regmap.vh#L4-L11) —— `MIO_CONFIG=0`、`MIO_STATUS=1`、`MIO_CLKDIV=2`、`MIO_CLKPHASE=3`、`MIO_ODELAY=4`、`MIO_IDELAY=5`、`MIO_ADDR0=6`、`MIO_ADDR1=7`（地址取 `dstaddr[5:2]`）。

> ⚠️ **文档与 RTL 不一致 #1**：README 的寄存器表把 `MIO_ODELAY` 标为 `0x5`、`MIO_IDELAY` 标为 `0x4`，与 `.vh`（`ODELAY=4`、`IDELAY=5`）**正好相反**。以 `.vh` 与 RTL 译码为准。

地址译码与写选通（标准 `reg_write & (addr==宏)` 写法）：

[mio/hdl/mio_regs.v:107-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L107-L116) —— 用 `dstaddr_in[5:2]` 与各宏比较产生 `config_write`/`status_write`/`clkdiv_write`/`clkphase_write`/`idelay_write`/`odelay_write`/`addr0_write`/`addr1_write`；`clkchange = clkdiv_write | clkphase_write` 通知 `oh_clockdiv` 重新稳定。

**配置寄存器位域**（这是 mio 的「控制面板」，务必记牢，4.2 会反复用到）：

[mio/hdl/mio_regs.v:130-140](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L130-L140) —— `config_reg[20:0]` 的位映射：
- `[0]` TX 禁止（`tx_en = ~[0]`，低有效使能）
- `[1]` RX 禁止（`rx_en = ~[1]`）
- `[3:2]` 传输模式（00=emode, 01=dmode, 10=amode）
- `[5:4]` IO 宽度 `iowidth`（对应 8/16/32/64 位 IO）
- `[7:6]` `datamode`（amode 时接收侧的数据宽度）
- `[12]` `ddr_mode`（DDR 模式）
- `[13]` `lsbfirst`（LSB 先发）
- `[14]` `framepol`（帧极性）
- `[20:16]` `ctrlmode`（emesh ctrlmode）

> ⚠️ **文档与 RTL 不一致 #2**：README 的 `MIO_CONFIG` 字段表声称 `[11:4]` 是「Number of flits/packet」，与上述 RTL 位域对不上。README 的字段表是早期/愿望式描述，**以 `mio_regs.v` 的 RTL 为准**。

**(4) 寄存器读回是「桩」**。`mio_regs` 的读回输出被直接写死为 0：

[mio/hdl/mio_regs.v:205-207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L205-L207) —— `assign access_out='b0; assign wait_out='b0; assign packet_out='b0;`。也就是说**寄存器读回当前未实现**，`reg_access_out`/`reg_packet_out` 恒为 0。这是 mio 处于「施工区」的明确信号。

#### 4.1.4 代码实践

**实践目标**：用源码核对的方式，把 mio 顶层端口分类，并与 elink 对比，建立「轻量 vs 高速」的直觉。

**操作步骤**：

1. 打开 [mio/hdl/mio.v:14-40](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L14-L40)，把每个端口归入四类：TX-IO、RX-IO、核侧、寄存器侧。
2. 打开 [elink/hdl/elink.v:1-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L1-L14)，统计 elink 顶层的端口数与差分对数。
3. 列出 `mio.v` 里所有 `output`/`input` 的位宽，回答：mio 的 IO 数据线有几根？elink 的呢？

**需要观察的现象**：

- mio 的 IO 数据是 `tx_packet[IOW-1:0]`（默认 64 根单端线）+ 1 根 `tx_access` + 1 根 `tx_clk` + 1 根 `tx_wait`。
- elink 的 IO 数据是 `txo_data_p/n[7:0]`（8 对差分 = 16 根）+ `txo_frame_p/n` + `txo_lclk_p/n` + wait 差分对——**差分、更窄、更多协议信号**。

**预期结果**：mio 用「宽并行 + 单端 + 少控制信号」换简单；elink 用「窄差分 + DDR + FRAME 成帧」换高速。核侧 mio 只有 1 收 1 发（+ 寄存器），elink 有 6 个 emesh 通道。

**待本地验证**：若你手头有 `iverilog`，可尝试 `grep -n "output\|input" mio/hdl/mio.v` 自行核对端口计数。

#### 4.1.5 小练习与答案

**练习 1**：`mio.v` 里 `tx_packet` 是几位？由哪个参数决定？
**答案**：`IOW` 位（默认 64），由顶层 `parameter IOW` 决定；`mio.v` 把它传给 `mtx`/`mrx` 的 `IOW` 参数。

**练习 2**：`dut_mio.v`（仿真包装）为什么把 `rx_packet` 接到 `tx_packet`、`tx_wait` 接到 `rx_wait`？
**答案**：这是**回环（loopback）**接法——让 mio 自己发自己收，这样单个 DUT 就能验证 TX→IO→RX 的完整通路，无需第二个芯片。

**练习 3**：`mio_regs` 的 `reg_access_out` 在读事务时会有响应吗？
**答案**：不会。源码第 205-207 行把读回三个输出都写死为 0，读回功能当前未实现。

---

### 4.2 三种传输模式：emode / dmode / amode

#### 4.2.1 概念说明

mio 最有特色的设计是「**同一套 IO 硬件，三种用法**」，由配置寄存器 `[3:2]` 选择：

| 模式 | `[3:2]` | 含义 | TX 侧发什么 | RX 侧如何还原 |
|------|:---:|------|------------|--------------|
| **emode** | 00 | emesh 包模式 | 完整 104 位 emesh 包（拆成多个 IO 字） | 把多个 IO 字**重组**回 104 位包 |
| **dmode** | 01 | 数据流模式 | 纯数据字（无包头），原样透传 | 原样上交（无包重组） |
| **amode** | 10 | 自动地址模式 | 纯数据字 | 把每个数据字**自动包装**成一次 emesh **写事务**，写到固定目的地址 |

直觉理解：

- **emode** = 「我就是一个 elink 的简化版，老老实实传 emesh 包」。适合需要语义（读/写/地址）的场景。
- **dmode** = 「我只搬字节，别管含义」。适合纯粹的数据流（如视频帧、采样流）。
- **amode** = 「我搬字节，但请帮我把每个字写进固定的内存地址」。这是 dmode 的「带自动地址」升级版，接收方无需软件干预就能把流写进指定地址区间。

> 注意：`dmode` 在 `mio_regs` 里被译出（`config_reg[3:2]==2'b01`），但在 `mio.v` 顶层**并没有把 `dmode` 信号接到 `mtx`/`mrx`**——收发两侧实际是用 `emode` 与 `~emode`（即「非包模式」）来区分的。换句话说，dmode 与 amode 在 TX 侧共享同一条「纯数据流」通路，差别只体现在 RX 侧 `mrx_fifo` 是否做 amode 包装。这是阅读时要留意的不对称点（见 4.4）。

#### 4.2.2 核心流程

**TX 侧（`mtx_fifo`）如何按模式拆字**：

```
emode = 1（包模式）:
   一个 104 位包 > 64 位 IO 字 → 用 emesh_cycle 状态机把包「折叠」
   成多个 ≤64 位 IO 字，每个字附带 valid[7:0] 字节有效掩码。

emode = 0（dmode/amode 流模式）:
   直接把 {srcaddr, data} 拼成 64 位 data_wide 一次性发出，
   valid 按 datamode 给出 0x01/0x03/0x0F/0xFF。
```

**RX 侧（`mrx_fifo`）如何按模式重组**：

```
amode: 把每个 64 位 fifo_data 直接包装成 emesh 写包：
       write=1, datamode=2'b11(64位), dstaddr=addr_reg(MIO_ADDR0/1),
       data=fifo_data[31:0], srcaddr=fifo_data[63:32]
emode: 用 emode_valid 轮转 one-hot 把最多 3 个 64 位字拼回 192 位缓冲，
       取低 104 位作为 emesh 包，emode_done 时上交。
```

一个关键数学关系：因为 IO 字宽（受 `iowidth` 控制，8/16/32/64）与包宽（104）不一定匹配，所以「一个包要拆成几个 IO 字」是个除法问题。备用模块 `mrx_protocol.v` 用一个 `\$clog2` 衍生位宽来计数：

\[ CW = \lceil \log_2(2 \cdot PW / NMIO) \rceil \]

即传输计数器的位宽由「包宽 / IO 宽」取对数得到（`mrx_protocol.v` 第 15 行：`parameter CW = $clog2(2*PW/NMIO);`）。当前在线的 `mrx_fifo` 没用这个计数器，而是用字节有效掩码 `valid` 来判断何时拼满，但思想一致：**IO 字越窄，传一个包需要的拍数越多**。

#### 4.2.3 源码精读

**(1) 配置位如何派生三种模式**：

[mio/hdl/mio_regs.v:130-134](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L130-L134) —— `emode = config_reg[3:2]==2'b00`；`dmode = ...==2'b01`；`amode = ...==2'b10`。这三位是互斥的派生线。

**(2) TX 侧 emode 折叠状态机**。`mtx_fifo` 用 `emesh_cycle[1:0]` 记录当前在发包的第几个字：

[mio/hdl/mtx_fifo.v:77-85](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L77-L85) —— emode 且收到 access 时进入 `emesh_cycle=01`；对 64 位地址宽还会再进一级 `10`；非 emode 立即清零。

字节有效掩码 `valid`（告诉 `mtx_io` 这个 IO 字里哪几个字节有效）：

[mio/hdl/mtx_fifo.v:88-93](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L88-L93) —— emode + AW=32 的首字有效掩码为 `0x3F`（48 位/6 字节）；流模式下按 `datamode` 给 `0x01/0x03/0x0F`。

按 `emesh_cycle` 从 `packet_buffer` 里挑出本拍要发的 64 位字：

[mio/hdl/mtx_fifo.v:96-99](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L96-L99) —— 流模式发 `data_wide`；emode 按 `emesh_cycle` 从 192 位 `packet_buffer` 取 `[127:64]` 或 `[191:128]`。`data_wide` 本身见 [mtx_fifo.v:69](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L69)（`{srcaddr_in, data_in}`）。

**(3) RX 侧 amode 自动包装**。`mrx_fifo` 把流数据直接包成 emesh 写事务：

[mio/hdl/mrx_fifo.v:85-90](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L85-L90) —— `amode_write=1`（恒写）、`amode_datamode=2'b11`（64 位）、`amode_ctrlmode=ctrlmode`（来自寄存器）、`amode_dstaddr=dstaddr`（即 `MIO_ADDR0`）、`amode_data=fifo_data[31:0]`、`amode_srcaddr=fifo_data[63:32]`。再用 `emesh2packet` 打包（[mrx_fifo.v:142-153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L142-L153)）。

**(4) RX 侧 emode 重组**。用 one-hot 轮转的 `emode_valid` 把多个 64 位字写进 192 位 `emode_shiftreg`：

[mio/hdl/mrx_fifo.v:100-118](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L100-L118) —— `emode_valid` 每收到一字轮转一位（`emode_next`），`emode_select` 选出当前字要落到 192 位缓冲的哪一段；`emode_packet = emode_shiftreg[103:0]`。`emode_done = fifo_access & (~&fifo_valid)`（出现「不满的字节掩码」说明收到包尾）见 [mrx_fifo.v:96](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L96)。

最后用 `amode` 选择上交哪个包：

[mio/hdl/mrx_fifo.v:130-134](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L130-L134) —— `access_out`/`packet_out` 在 amode 时取 `fifo_access`/`amode_packet`，否则取 `emode_access`/`emode_packet`。

#### 4.2.4 代码实践

**实践目标**：读懂 `test_basic.emf` 如何用配置寄存器切换模式与 IO 宽度。

**操作步骤**：

1. 打开 [mio/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/dv/tests/test_basic.emf)。每行格式为 `srcaddr_data_dstaddr_ctrlmode_access`（u4-l2、u5-l1 讲过）。
2. 看第 2 行：`DEADBEEF_00000018_00000000_05_0000`，注释为 `CONFIG(AMODE,MSB,SDR,16B-IO)`：
   - `dstaddr=0x00000000` → `dstaddr[5:2]=0` → `MIO_CONFIG`（[mio_regmap.vh:4](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regmap.vh#L4)）。
   - `ctrlmode=0x05` → bit0=1（写）、bits[2:1]=10（datamode=2，32 位写）。
   - `data=0x00000018` → 写入 `config_reg[20:0]`。`0x18 = 0b11000`：`[3:2]=10`（amode）、`[5:4]=01`（16 位 IO）。与注释吻合。
3. 依次看后续 `CONFIG(...)` 行（第 13、19、25、31、33 行），分别计算它们写入的 `config_reg` 值对应的「模式 / IO 宽度 / DDR / MSB」。

**需要观察的现象**：测试先做一次 emode 32 位写（第 1 行），然后切换到 amode、依次试 16B/8B/32B/64B IO 宽度的 SDR，再试 8B DDR 的 amode 与 emode。它本质上是一张「模式 × IO 宽度 × SDR/DDR」的组合覆盖表。

**预期结果**：你能用 [mio_regs.v:130-140](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L130-L140) 的位域定义，把每条 `CONFIG` 行的 `data` 字段解码成「模式 + IO 宽度 + DDR」，并与行尾注释一致。

**待本地验证**：完整跑通该测试需要 `packet2emesh` 等未定义模块的仿真库替换，本仓库状态下不一定能直接编译通过。

#### 4.2.5 小练习与答案

**练习 1**：amode 下，RX 侧给数据流用的「目的地址」从哪里来？
**答案**：来自寄存器 `MIO_ADDR0`/`MIO_ADDR1`（即 `mio_regs` 里的 `addr_reg`，输出为 `dstaddr`），不是从数据流里解析出来的。

**练习 2**：为什么 emode 模式下 `mtx_fifo` 需要状态机 `emesh_cycle`，而 dmode/amode 不需要？
**答案**：emode 的 emesh 包有 104 位，比一个 64 位 IO 字宽，必须折叠成多个 IO 字分拍发送，因此需要状态机记住「发到第几个字」；dmode/amode 每次只发一个 64 位数据字，一发即走，无需折叠。

**练习 3**：`mio.v` 把 `dmode` 信号接到 `mtx`/`mrx` 了吗？
**答案**：没有。`mio.v` 只把 `emode`（与 amode 相关配置）接给收发两侧。TX 侧 dmode/amode 共享同一条流式通路（用 `~emode` 区分），二者差别只在 RX 侧 `mrx_fifo` 是否做 amode 包装。

---

### 4.3 TX/RX 对称分层：mtx 与 mrx

#### 4.3.1 概念说明

mio 的收发通路在**概念上对称**，都遵循「`*_fifo`（跨域 + 拆/组包）+ `*_io`（串化/解串 + DDR）」两层结构，与 elink 的 etx/erx 同构：

| | TX（mtx） | RX（mrx） |
|---|-----------|-----------|
| 跨域+拆/组包 | `mtx_fifo`（包→IO 字） | `mrx_fifo`（IO 字→包） |
| 串化/解串+DDR | `mtx_io`（移位 + `oh_oddr`） | `mrx_io`（`oh_iddr` + 累积） |
| 时钟 | `io_clk`（本地 `oh_clockdiv` 产生） | `rx_clk`（对端送来的源同步时钟） |

但**实际上并不完全对称**，有几处重要差异：

1. **TX 没有独立的 `mtx_protocol`**（文件不存在），成帧/拆包被折叠进 `mtx_fifo`；而 RX 侧有一个独立的 `mrx_protocol.v`，但**当前 `mrx.v` 并不例化它**——RX 的拆帧/重组被分散到 `mrx_io`（捕获+打包）和 `mrx_fifo`（emode 重组）。所以「对称」是设计意图，现实是两边各有各的折叠方式。
2. **TX 时钟本地产生，RX 时钟来自对端**：TX 用 `oh_clockdiv` 从 `clk` 分频出 `io_clk` 并相位偏移得到 `tx_clk`；RX 直接用对端送来的 `rx_clk`。
3. **wait 反压方向相反**：TX 的 `io_wait` 是给串化器的本地反压，`tx_wait` 是对端给的远端反压（需 `oh_dsync` 跨域同步）；RX 的 `rx_wait` 是本地给对端的反压。

#### 4.3.2 核心流程

**发送（mtx）流水线**：

```
packet_in[104] ──packet2emesh──▶ 字段(write/datamode/dstaddr/data/srcaddr)
        │
        ▼ 按 emode/dmode/amode 拆字 + valid[7:0] 字节掩码
   {fifo_data[64], valid[8]} = fifo_packet[72]
        │
        ▼ oh_fifo_cdc  (clk 域 → io_clk 域)
   fifo_packet_out[72]
        │
        ▼ mtx_io: shiftreg 重载/移位, oh_oddr 双沿
   tx_packet[IOW-1:0], tx_access, (tx_wait 经 oh_dsync 同步)
```

**接收（mrx）流水线**（互为逆过程）：

```
rx_packet[IOW-1:0], rx_access, rx_clk
        │
        ▼ mrx_io: oh_iddr 解串 → mux_data 按字节复制 → shiftreg 累积
   io_packet[64], io_valid[8], io_access
        │
        ▼ oh_fifo_cdc  (rx_clk 域 → clk 域)
   fifo_data[64], fifo_valid[8]
        │
        ▼ mrx_fifo: amode 包装 / emode 重组
   packet_out[104], access_out
```

#### 4.3.3 源码精读

**(1) mtx 顶层**：例化 `mtx_fifo` + `mtx_io`，注意**没有 `mtx_protocol`**。

[mtx/hdl/mtx.v:53-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx.v#L53-L70) —— 例化 `mtx_fifo`，产出 `io_packet[63:0]` + `io_valid[7:0]`，回吐 `wait_out`。

[mtx/hdl/mtx.v:76-89](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx.v#L76-L89) —— 例化 `mtx_io`，把 `io_packet`/`io_valid` 串化为 `tx_packet`/`tx_access`。

**(2) mtx_io 的移位与 DDR**。先用 `iowidth` + `ddr_mode` 译出本次传输宽度（DDR 会使有效宽度翻倍）：

[mio/hdl/mtx_io.v:48-54](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L48-L54) —— `dmode8/16/32/64` 译码（DDR 时宽度翻倍）。

移位寄存器「向下移」（高位补 0，从最高有效字节开始发）：

[mio/hdl/mtx_io.v:86-94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L86-L94) —— `reload` 时重载 `io_packet`；否则按 8/16/32 位左移（补 0）。

DDR 输出用 `oh_oddr`（上升沿发 `even`、下降沿发 `odd`）：

[mio/hdl/mtx_io.v:105-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L105-L116) —— `ddr_data_even` 取 `shiftreg` 低半，`ddr_data_odd` 按 `iowidth` 取相邻半字；`oh_oddr` 双沿输出 `tx_packet_ddr`。

最后在 DDR 与 SDR 间选择，并把 `tx_wait` 用 `oh_dsync` 同步进来、把复位用 `oh_rsync` 同步到 `io_clk`：

[mio/hdl/mtx_io.v:118-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L118-L135) —— `tx_packet = ddr_mode ? {零扩, ddr} : sdr`；`oh_rsync sync_reset`、`oh_dsync sync_wait`。

**(3) mrx 顶层**：例化 `mrx_io` + `mrx_fifo`，**同样不例化 `mrx_protocol`**。

[mio/hdl/mrx.v:51-72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx.v#L51-L72) —— 例化 `mrx_fifo`，消费 `io_access`/`io_valid`/`io_packet`，输出核侧 `access_out`/`packet_out`，回吐 `rx_wait`。

[mio/hdl/mrx.v:79-92](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx.v#L79-L92) —— 例化 `mrx_io`，把 `rx_packet`/`rx_access` 解串为 `io_packet`/`io_valid`/`io_access`。

**(4) mrx_io 的解串与累积**（与 `mtx_io` 的移位方向相反——这里是「向上累积」）：

[mio/hdl/mrx_io.v:62-87](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v#L62-L87) —— `valid_input` 给出初始字节有效（`dmode8→0x01`、`dmode16→0x03`、…），`valid_next` 把有效位向高位累积；`transfer_done = &io_valid`（全满）、`transfer_active = |io_valid`（非空）。

`io_access` 在「凑满一字」或「帧结束的部分字」时拉高：

[mio/hdl/mrx_io.v:90-91](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v#L90-L91) —— `io_access = transfer_done | (~io_frame & transfer_active)`。

DDR 解串用 `oh_iddr`（与 `mtx_io` 的 `oh_oddr` 互逆）：

[mio/hdl/mrx_io.v:98-108](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v#L98-L108) —— `oh_iddr` 把 `rx_packet` 的半字拆成上升沿 `ddr_even`/下降沿 `ddr_odd`，再按 `iowidth` 拼回全宽。

打包器按字节有效写 64 位 `shiftreg`：

[mio/hdl/mrx_io.v:141-149](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v#L141-L149) —— `data_select` 选出本拍要写入哪些字节车道，`for` 循环逐字节把 `mux_data` 写进 `shiftreg`，输出 `io_packet`。

#### 4.3.4 代码实践

**实践目标**：画出 TX 数据通路框图，标注一个写包从核侧到 `tx_packet` 经过的每一级；再对照 RX 说明「串化/解串互逆」。

**操作步骤**：

1. 沿 [mtx_fifo.v:56-66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L56-L66)（`packet2emesh` 解包）→ [mtx_fifo.v:96-101](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L96-L101)（拼 `fifo_packet_in[72]`）→ [mtx_fifo.v:113-127](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L113-L127)（`oh_fifo_cdc`）→ [mtx_io.v:86-94](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L86-L94)（移位）→ [mtx_io.v:112-120](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_io.v#L112-L120)（`oh_oddr` + DDR/SDR 选择）画出框图。
2. 对照 [mrx_io.v:98-108](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_io.v#L98-L108)（`oh_iddr` 解串）与 `mtx_io` 的 `oh_oddr`，说明二者互逆。

**需要观察的现象**：TX 是「宽字 → 移位 → 双沿串化」，RX 是「双沿解串 → 累积 → 宽字」，数据宽度沿通路「先宽、后窄、再宽」。

**预期结果**：你画出的 TX 框图应包含 5 级：解包 → 拆字+掩码 → CDC FIFO → 移位寄存器 → ODDR；RX 框图是它的镜像（IDDR → 累积 → CDC FIFO → 重组/包装）。

**待本地验证**：`oh_oddr`/`oh_iddr` 的具体行为依赖 `TARGET`（XILINX 下是 IDDR/ODDR 原语，GENERIC 下是行为模型），可在 `xilibs/` 里核对（u9-l3）。

#### 4.3.5 小练习与答案

**练习 1**：`mtx_io` 的移位寄存器是「向高位补 0、从高位发」，`mrx_io` 的累积方向是怎样的？
**答案**：`mrx_io` 是「向高位累积」——`valid_next` 把有效位向左（高位）移，新数据填进低位车道，直到 `&io_valid`（全满）凑成一个完整 64 位字。

**练习 2**：为什么 `mtx_io` 用 `oh_dsync` 同步 `tx_wait`，而 `mrx_io` 不需要同步 wait？
**答案**：`tx_wait` 是对端（远端 RX）给的、跨时钟域的反压，必须用同步器采样；`mrx_io` 的 `rx_wait` 是本域产生、发给对端的反压，不涉及跨域采样（对端会自己同步它）。

**练习 3**：仓库里有 `mrx_protocol.v`，但 `mrx.v` 不例化它，这说明什么？
**答案**：说明 mio 处于演进中——`mrx_protocol.v`（IDLE/BUSY 状态机 + `oh_ser2par`）是一份独立的拆帧实现，而当前在线的 datapath 把等价逻辑内联进了 `mrx_io` + `mrx_fifo`。两者并存是重构未收敛的迹象，阅读以实际例化关系为准。

---

### 4.4 工程现实：不能直接编译的模块与备用文件

> 这一节没有新概念，但**对你避免踩坑至关重要**，也延续了本手册「代码即事实、文档可能滞后」的一贯原则。

本模块（与 elink、gpio 等一样）存在若干**未定义引用**与**备用/历史文件**：

1. **`packet2emesh` / `emesh2packet` 未定义**：`mtx_fifo`、`mrx_fifo`、`mio_regs`、`mio_if` 都例化了它们（如 [mtx_fifo.v:56-66](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mtx_fifo.v#L56-L66)、[mrx_fifo.v:142-153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_fifo.v#L142-L153)），但仓库里找不到这两个模块的定义。因此 **mio 不能原样编译**，需要在仿真平台库中提供替换实现（这与其他外设模块的情况一致，见 u6-l2、u7-l2）。

2. **`mtx_protocol.v` 不存在**：大纲与本讲最初假设的 TX 协议子模块其实并不存在。TX 成帧折叠在 `mtx_fifo`，串化在 `mtx_io`。

3. **`mio_if.v`、`mio_dp.v`、`mrx_protocol.v` 是备用/历史模块**，**未被 `mio.v` 例化**：
   - [mio_if.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_if.v)（第 15-17 行 `MPW=128`）是另一套 RX 包格式化逻辑，amode 字段覆盖逻辑与 `mrx_fifo` 重叠（[mio_if.v:93-114](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_if.v#L93-L114)）。
   - [mio_dp.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_dp.v)（第 17 行 `NMIO=8`）是用 `NMIO` 参数的另一套 datapath，例化 `mtx`/`mrx` 的端口集也与现行 `mio.v` 不同。
   - [mrx_protocol.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mrx_protocol.v) 是独立的 RX 拆帧状态机（第 49-60 行 `MRX_IDLE`/`MRX_BUSY` + `oh_ser2par`），未被 `mrx.v` 使用。

4. **寄存器读回未实现**：[mio_regs.v:205-207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L205-L207) 把读回三个输出写死为 0。

5. **README 与 RTL 的若干不一致**：接口表用 `data_in`/`data_out`（实际核侧是 `packet_in`/`packet_out`）、参数用 `N`（实际是 `IOW`）、`ODELAY`/`IDELAY` 地址相反、`MIO_CONFIG` 字段表对不上位域。一律以 RTL 为准。

> 阅读策略：把 `mio.v` 的例化关系当作「事实地图」，凡是被 `mio.v` 例化的才是「在线 datapath」；其余文件当作参考实现或历史残留，理解其意图即可，不要假设它们参与工作。

## 5. 综合实践：mio 与 elink 的取舍对比

本讲的综合实践正是大纲指定的任务：**对比 mio 与 elink 的 IO 信号宽度与寄存器表，列出两者各自适合的场景**。

### 任务

请填写下表（先自己填，再核对下方参考答案）：

| 对比维度 | elink | mio |
|----------|-------|-----|
| IO 数据线宽度 | ? | ? |
| 单端 / 差分 | ? | ? |
| 默认数据率模式 | ? | ? |
| 系统侧通道数 | ? | ? |
| 支持的传输模式 | ? | ? |
| 寄存器数（约） | ? | ? |
| 典型适用场景 | ? | ? |

### 操作步骤

1. **IO 信号宽度**：读 [elink.v:32-45](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L32-L45)（`rxi_data_p/n[7:0]`、`txo_data_p/n[7:0]`）与 [mio.v:17-25](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L17-L25)（`tx_packet[IOW-1:0]`，默认 64）。
2. **系统侧通道**：读 [elink.v:63-70](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink.v#L63-L70)（rxwr/rxrd/…）与 [mio.v:27-39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio.v#L27-L39)（只有 1 收 1 发 + 寄存器）。
3. **寄存器表**：对照 [elink 的 regmap](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_regmap.vh)（u7-l4 讲过，含收发通道、MMU、转发等大量寄存器）与 [mio_regmap.vh:4-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regmap.vh#L4-L11)（只有 8 个）。

### 参考答案

| 对比维度 | elink | mio |
|----------|-------|-----|
| IO 数据线宽度 | 8 位（DDR） | 可参数化，默认 64 位（`IOW`） |
| 单端 / 差分 | **差分** `_p`/`_n`（含 LCLK、FRAME、WAIT） | **单端**（`tx_packet`/`rx_packet`/`tx_access`/`tx_clk`） |
| 默认数据率模式 | DDR（双沿） | 可配 SDR/DDR（`config[12]`） |
| 系统侧通道数 | 6 个 emesh 通道（rxwr/rxrd/rxrr + txwr/txrd/txrr） | 1 收 + 1 发 + 1 寄存器 |
| 支持的传输模式 | 仅 emesh 包模式 | emode / dmode / amode 三种 |
| 寄存器数（约） | 数十个（含收发配置、MMU、邮箱、转发） | 8 个（CONFIG/STATUS/CLKDIV/CLKPHASE/ODELAY/IDELAY/ADDR0/ADDR1） |
| 典型适用场景 | 芯片间长距离、高速（如 FPGA↔Epiphany，跑百兆字节/秒级） | 裸芯间/板内短距离、宽度灵活、协议可换、引脚预算宽裕的低复杂度互连 |

**结论**：elink 与 mio 不是「谁取代谁」，而是 **速度/距离 ↔ 灵活/简单** 的取舍。需要把 emesh 包高速打到远端芯片，选 elink；需要在两个裸芯或同板 FPGA 之间用一条宽并行总线搬数据（且想随时在「传包/传流/自动写」间切换），选 mio。

**待本地验证**：若要进一步量化，可在 `parallella/` 板级顶层（u9-l4）里看 elink 的真实接线，对照 `mio/driver/linux-uio/` 的设备树（`zynq-parallella-oh-mio.dts`）看 mio 在 Parallella 上的实际部署。

## 6. 本讲小结

- **mio 是 elink 的轻量替代**：源同步、协议无关、IO 宽度可参数化（`IOW` 默认 64）、SDR/DDR 可配、单端并行，核侧仍用 104 位 emesh 包但系统侧只暴露一对收发通道。
- **顶层 `mio.v`** 拼装四块：`mtx`（发）、`mrx`（收）、`mio_regs`（寄存器）、`oh_clockdiv`（TX 时钟分频/移相）；收发各分「`*_fifo`（跨域+拆/组包）+ `*_io`（串化/解串+DDR）」两层。
- **三种传输模式**复用同一硬件：emode 传完整 emesh 包（需拆/重组）、dmode 传纯数据流、amode 把数据流自动包装成写固定地址的 emesh 事务。
- **TX/RX 概念对称但现实不对称**：TX 没有 `mtx_protocol`（折叠进 `mtx_fifo`）；RX 有 `mrx_protocol.v` 却不例化（折叠进 `mrx_io`/`mrx_fifo`）；TX 时钟本地产生，RX 时钟来自对端。
- **配置寄存器 `[3:2]` 选模式、`[5:4]` 选 IO 宽度、`[12]` 选 DDR**，位域以 `mio_regs.v` RTL 为准（README 字段表与 `.vh` 的 ODELAY/IDELAY 地址均与 RTL 有出入）。
- **工程现实**：`packet2emesh`/`emesh2packet` 未定义、`mio_if`/`mio_dp`/`mrx_protocol` 未被例化、寄存器读回是桩——mio 处于施工区，阅读以 `mio.v` 的实际例化关系为「事实地图」。

## 7. 下一步学习建议

- **向上（板级集成）**：读 [parallella/hdl/parallella_base.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/parallella/hdl/parallella_base.v) 与 `mio/driver/linux-uio/zynq-parallella-oh-mio.dts`，看 mio 如何在 Parallella 板上与 Zynq PL 对接（承接 u9-l4 板级集成）。
- **向深（物理实现）**：mio 的 `oh_oddr`/`oh_iddr` 在 `TARGET=XILINX` 时映射到 IDDR/ODDR 原语，可结合 `xilibs/`（u9-l3）理解厂商仿真模型如何替换黑盒。
- **横向（对比 elink）**：重读 u7-l2/u7-l3 的 etx/erx 流水线，把本讲的 mtx/mrx 与之逐级对照，体会「宽并行轻量」与「窄差分高速」两种风格的工程取舍。
- **动手（二次开发）**：参照 u9-l5 的「新建最小 IP」流程，尝试为 mio 补一个最小的寄存器读回实现（替换 [mio_regs.v:205-207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/mio/hdl/mio_regs.v#L205-L207) 的桩），并用 `dut_mio.v` 的回环接法在仿真里验证写 CONFIG 后能读回。
