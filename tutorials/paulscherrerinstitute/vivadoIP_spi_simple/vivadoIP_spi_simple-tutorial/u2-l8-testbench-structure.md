# 测试平台结构与 AXI/SPI 自检

## 1. 本讲目标

前面几讲我们分别读懂了寄存器地图（u2-l1）、`spi_simple` 核心架构（u2-l2）、AXI 从接口（u2-l3）、SPI 时序（u2-l4）、FIFO 背压（u2-l5）、中断机制（u2-l6）和 C 驱动（u2-l7）。**所有这些机制凭什么被相信是“对的”？** 靠的是 `tb/top_tb.vhd` 这个测试平台（testbench）。读完本讲，你应当能够：

- 说清 `top_tb` 如何实例化被测件（DUT）`spi_vivado_wrp`，并用一组 AXI **总线功能模型（BFM）** 信号驱动完整的 AXI4 五通道。
- 看懂 `p_control` 进程里的几个测试场景（只写、读写、FIFO 填充＋逐笔中断检查、RX 满＋中断清除），以及每段到底在校验什么。
- 理解 `p_spi` 进程如何**扮成一个 SPI 从机**：校验片选是否选中正确的 slave、逐 bit 收发、并在事务前后断言 LE（锁存使能）信号。
- 自己动手为 `top_tb` 设计一个新场景（向 slave 2 发两字节并断言 `Busy` 翻转），并能准确说出插入位置与 AXI 调用顺序。

## 2. 前置知识

本讲假设你已经读过 u2-l1～u2-l4。这里补充几个测试平台专属的概念：

- **测试平台（testbench）**：一段不参与综合、只在仿真里运行的 VHDL 代码。它的职责是给被测件施加激励（stimulus）、观察输出，并在输出与预期不符时报错。本项目的 testbench 入口是 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd)。
- **被测件（DUT, Device Under Test）**：被验证的那个设计。本讲里就是顶层 wrapper `spi_vivado_wrp`（u1-l2 / u2-l3）。
- **总线功能模型（BFM, Bus Functional Model）**：一个“假装成总线主/从”的辅助组件/过程。它不模拟真实 CPU 的微架构，只按协议把 AXI 握手信号驱动成合法的读/写时序，让你能用一行 `axi_single_write(addr, data, …)` 代替手写几十行 VALID/READY 时序。本项目的 AXI BFM 来自外部依赖 `psi_tb`（u1-l3）的 `psi_tb_axi_pkg`。
- **断言式比较（expect/compare）**：把“读到的值”和“期望值”当场比较，不一致就往 transcript 打一条错误信息。本项目用 `psi_tb_compare_pkg` 提供的 `StdlCompare` / `StdlvCompareStdlv` 以及 AXI BFM 自带的 `axi_single_expect`。
- **record（记录类型）**：VHDL 里把多个信号打包成一个“结构体”的类型。本项目用两个 record（`axi_ms_r` / `axi_sm_r`）把 AXI4 三四十根线收成两个对象，避免声明一大堆零散信号。

一句话定位：**`top_tb` 是一份“用 AXI BFM 当 CPU、用一个简易 SPI 从机进程当对端器件”的自检脚本**——它把 u2-l2～u2-l6 讲的所有内部机制端到端地跑了一遍，并在关键节点逐位比对。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲如何使用 |
|------|------|--------------|
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd) | 测试平台主体：例化 DUT、时钟复位、`p_control` 测试场景、`p_spi` 从机仿真 | 本讲的绝对主角，逐段精读 |
| [hdl/spi_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd) | 被测件顶层 wrapper（AXI 解码＋`spi_simple` 核心） | 确认 DUT 的端口与 generic，对应 testbench 的例化 |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd) | 寄存器索引 / 状态位 / 中断位常量（单一数据源） | 解释 testbench 里 `RegIdx_*_c`、`BitIdx_*_c`、`Irq_*_c` 这些名字的含义 |

> 说明：AXI BFM 的具体实现（`psi_tb_axi_pkg`、`psi_tb_compare_pkg`）和类型 `axi_ms_r`/`axi_sm_r` 都在外部依赖 `psi_tb` 里，不在本仓库源码中。本讲只依据 `top_tb.vhd` 里的**调用方式**来推断它们的接口，不臆测其内部实现。

---

## 4. 核心概念与源码讲解

### 4.1 DUT 例化与 AXI BFM

#### 4.1.1 概念说明

要验证 `spi_vivado_wrp`，testbench 必须做三件事：

1. **产生时钟与复位**——给 DUT 的 `s00_axi_aclk` / `s00_axi_aresetn`。
2. **扮演 AXI 主机**——通过 AXI4 五通道（AR/R/AW/W/B）读写 DUT 的寄存器，复现真实 CPU（如 Zynq ARM 核）的行为。
3. **扮演 SPI 从机**——在 `spi_miso` 上回送数据、采样 `spi_mosi`、并检查 `spi_cs_n` / `spi_le` 是否正确。

第 2 件事靠 AXI **BFM** 完成。本项目没有把 AXI 的三四十根信号一根根声明，而是借助 `psi_tb_axi_pkg` 提供的两个 record 类型，把它们收成两个对象：

- `axi_ms`（master → slave 方向）：主机输出、从机输入的那批信号（`araddr`、`awvalid`、`wdata`、`bready` …）。
- `axi_sm`（slave → master 方向）：从机输出、主机输入的那批信号（`arready`、`rdata`、`bresp` …）。

这两个 record 一路贯穿：BFM 过程往 `axi_ms` 写激励、从 `axi_sm` 读响应；DUT 的端口映射则把 `axi_ms.*` 当输入、`axi_sm.*` 当输出。于是 BFM 和 DUT 通过同一对 record 自然对接。

#### 4.1.2 核心流程

```
┌──────────────────────── top_tb (architecture sim) ────────────────────────┐
│                                                                            │
│   常量：ClockFrequencyAxi_c=125 MHz,  SlaveCnt_c=3                         │
│   共享信号：SlaveTx / ExpectedSlaveRx / SlaveNr / TbRunning                 │
│                                                                            │
│   ┌──── p_aclk ────┐    aclk    ┌─────────────── DUT ───────────────┐     │
│   │  125 MHz 方波   │──────────▶│ i_dut : spi_vivado_wrp             │     │
│   └────────────────┘           │  s00_axi_* ◄──► axi_ms / axi_sm    │     │
│                                 │  spi_sck/cs_n/mosi/miso/le/irq    │     │
│   ┌──── p_control ────────┐    └───────────┬───────────┬────────────┘     │
│   │ 复位 → 四个测试场景    │  AXI(ms/sm)    │ SPI        │                 │
│   │ 用 axi_single_* 驱动   │◄──────────────┘            │                 │
│   │ 写 SlaveTx/Expected…   │───────────────────────────▶│                 │
│   └────────────────────────┘   ┌─────── p_spi ──────────┐                  │
│                                │ 扮演 SPI 从机：          │                  │
│                                │ 校验 cs_n、逐 bit 收发、  │                  │
│                                │ 断言 LE、比较收发数据     │                  │
│                                └────────────────────────┘                  │
└────────────────────────────────────────────────────────────────────────────┘
```

注意图里那根“写 SlaveTx/Expected…”的虚线：`p_control` 和 `p_spi` 是两个并发进程，它们**通过 architecture 里的信号**通信。`p_control` 在发起一笔事务前先把“从机该回送什么（`SlaveTx`）”、“从机该收到什么（`ExpectedSlaveRx`）”、“这次选中第几号从机（`SlaveNr`）”写到共享信号上，`p_spi` 在事务进行中读这些信号来完成自检。这是理解整个 testbench 的关键。

#### 4.1.3 源码精读

**AXI record 的声明**：用 subtype 把每个字段的位宽钉死，再一次性实例化两个 record。`ID_WIDTH=1`、`ADDR_WIDTH=8`、`DATA_WIDTH=32`，与 DUT 的 AXI 端口完全一致。

[top_tb.vhd:33-53](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L33-L53) —— 声明 AXI 宽度常量、各字段的 range subtype，以及 `axi_ms`（主出从入）与 `axi_sm`（从出主入）两个 record。`wstrb` 的宽度由 `BYTE_WIDTH=DATA_WIDTH/8=4` 决定。

> 这就是“用 record 收纳 AXI”的好处：否则你要为 `arid/araddr/.../bready` 等近 30 根线各写一条 `signal` 声明，端口映射也要写 30 行。这里端口映射仍然写了全量（为了和 DUT 的扁平端口一一对应），但 BFM 过程内部只需传 `axi_ms, axi_sm` 两个参数。

**TB 常量与共享信号**：`SlaveCnt_c=3` 决定 DUT 例化 3 个从机（编号 0/1/2），`SlaveTx`/`ExpectedSlaveRx` 是 8 位（与 `TransWidth_g=8` 匹配）。

[top_tb.vhd:58-78](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L58-L78) —— 时钟频率 125 MHz、`SlaveCnt_c:=3`，以及 `SlaveTx`、`ExpectedSlaveRx`、`SlaveNr`、`TbRunning` 这几个跨进程共享信号，外加 DUT 的物理 SPI/复位/中断信号。

**DUT 例化**：被测件就是 `spi_vivado_wrp`。注意 generic 取值都是“便于测试”的小数字——`ClockDivider_g=20`（SCK 慢，仿真快）、`TransWidth_g=8`（每帧一字节）、`FifoDepth_g=8`（FIFO 很浅，容易灌满/溢出，专测 FIFO/中断逻辑）。

[top_tb.vhd:85-99](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L85-L99) —— `i_dut : entity work.spi_vivado_wrp` 的 generic 映射。`SlaveCnt_g => SlaveCnt_c` 把从机数也参数化。

[top_tb.vhd:100-147](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L100-L147) —— 端口映射：SPI 物理线（`spi_sck`/`spi_cs_n`/`spi_mosi`/`spi_miso`/`spi_le`/`irq`）接到 testbench 信号；AXI 五通道的每一根都从 `axi_ms.*`（输入）或 `axi_sm.*`（输出）取值。这套一一对应正是 u2-l3 讲过的 AXI4 slave 端口。

与 DUT 定义对照，确认两侧端口一致：例如 `s00_axi_araddr` 在 wrapper 里是 `std_logic_vector(7 downto 0)`（[spi_vivado_wrp.vhd:68](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L68)），对应 testbench 里 `ADDR_WIDTH=8`。

**时钟进程**：经典的“半周期置 1、半周期置 0”方波发生器，靠 `TbRunning` 控制何时停。

[top_tb.vhd:152-162](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L152-L162) —— `p_aclk`：周期 `ClockPeriodAxi_c = 1s/125e6 = 8 ns`，每半周期（4 ns）翻转一次，`TbRunning` 变 false 后 `wait` 永久停表。

**AXI BFM 的三个过程**（来自 `psi_tb_axi_pkg`，按 `top_tb` 的调用方式推断接口）：

| 过程 | 参数（按调用顺序） | 作用 |
|------|------|------|
| `axi_single_write` | `(addr, data, axi_ms, axi_sm, aclk)` | 发起一次 AXI 单字写：走 AW/W/B 三通道，把 `data` 写到 `addr` |
| `axi_single_read` | `(addr, value, axi_ms, axi_sm, aclk, lowbit, highbit)` | 发起一次 AXI 单字读：走 AR/R 两通道，把读回值在 `[highbit:lowbit]` 位段抽出存进 `value` |
| `axi_single_expect` | `(addr, expected, axi_ms, axi_sm, aclk, msg, lowbit, highbit)` | 读一次并和 `expected` 比较，不符则用 `msg` 报错 |

例如 [top_tb.vhd:189](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L189) 读 `Status` 寄存器但只取 `BitIdx_Status_Busy_c`（第 6 位）这一个 bit，而 [top_tb.vhd:232](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L232) 读 `Status` 时取 `[7:0]` 全部 8 位——这就是 `lowbit/highbit` 参数的用途。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把“AXI record ↔ DUT 端口 ↔ BFM 过程”这条链亲手走一遍，确认每根线都接对了。

**步骤**：

1. 打开 [tb/top_tb.vhd:100-147](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L100-L147)，挑出读地址通道（`ar*`）的 5 根线，逐根判断它属于 `axi_ms`（主出）还是 `axi_sm`（从出）。
2. 打开 [hdl/spi_vivado_wrp.vhd:66-83](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L66-L83)，看 wrapper 里 `s00_axi_ar*` 各自是 `in` 还是 `out`。
3. 验证方向一致：BFM 主机输出的信号（`axi_ms`）必须接到 DUT 的 `in` 端口；DUT 输出的信号（接到 `axi_sm`）必须是 `out` 端口。

**预期结果**：例如 `s00_axi_arvalid`（主→从）在 wrapper 是 `in`（[spi_vivado_wrp.vhd:75](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L75)），testbench 里映射到 `axi_ms.arvalid`（[top_tb.vhd:120](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L120)）；`s00_axi_arready`（从→主）在 wrapper 是 `out`（[spi_vivado_wrp.vhd:76](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L76)），映射到 `axi_sm.arready`（[top_tb.vhd:121](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L121)）。每个 AXI 信号都满足“一端 in、一端 out”的握手配对。

**观察现象**：你会看到 `axi_ms` 永远接到 DUT 的 `in` 端口、`axi_sm` 永远接到 `out` 端口，绝无交叉。这就是 record 命名 `_ms`（master→slave）/`_sm`（slave→master）的含义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 testbench 用 `SlaveCnt_c=3` 而 DUT 默认 `SlaveCnt_g=1`？用 3 个从机对测试有什么好处？
**答案**：默认值 1 只能测单从机；用 3 是为了能验证“多从机片选”——确认 IP 选中正确的那一根 `spi_cs_n(s)` 且不误选其他从机（见 4.3 节 `p_spi` 的片选校验）。`SlaveNr` 取 0/1/2 才有区分度。

**练习 2**：`axi_single_read` 的 `lowbit`/`highbit` 参数如果都填 0，读回的是什么？
**答案**：只抽取出读回 32 位值的第 0 位（最低位）。源码里 [top_tb.vhd:189](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L189) 就是 `BitIdx_Status_Busy_c, BitIdx_Status_Busy_c`（都是 6），只取 Busy 那一位，方便在 `while Readback_v /= 0 loop` 里判断。

---

### 4.2 p_control 测试场景

#### 4.2.1 概念说明

`p_control` 是整个 testbench 的“导演进程”：它按顺序发起复位、逐个跑测试场景、最后关停时钟。每个场景都是一段“**用 AXI BFM 操作寄存器 → 读回/断言结果**”的序列，本质上和 u2-l7 讲的 C 驱动做的是同一件事——只不过这里用 VHDL BFM 代替了 `Xil_Out32/In32`。

四个场景各自聚焦一个机制：

| 场景 | 校验的核心机制 | 对应讲义 |
|------|------|------|
| ① 只写事务（Write Only） | TX-only（`StoreRx=0`）、`Busy` 轮询、片选 slave 1 | u2-l2、u2-l4 |
| ② 读写事务（Write/Read） | RX+TX（`StoreRx=1`）、读回 MISO、片选 slave 0 | u2-l2、u2-l7 |
| ③ FIFO 填充＋逐笔中断检查 | FIFO 水位、almost 阈值、`TfDone` 中断、9 条命令灌深度 8 的 FIFO | u2-l5、u2-l6 |
| ④ RX 满＋中断清除 | RX FIFO 溢出（`RxFull`）、W1C 清除、电平型中断“清不掉/自动重置” | u2-l5、u2-l6 |

#### 4.2.2 核心流程

每个场景的共同骨架（以“读写事务”为例）：

```
1. 设置共享信号：SlaveNr <= N;  ExpectedSlaveRx <= 期望收到的字节;  SlaveTx <= 从机要回送的字节
2. AXI 写 SlaveNr   寄存器（选从机）
3. AXI 写 StoreRx   寄存器（0=只写 / 1=读回）
4. AXI 写 Data      寄存器（扣扳机，命令入 TX FIFO，Busy 变 1）
5. 轮询 Status.Busy 直到 0（事务物理完成，Busy 变 0）
6. （若 StoreRx=1）AXI 读 Data，期望读回 == SlaveTx（从机回送的值）
```

步骤 1 把“预期”告诉 `p_spi`，步骤 2～6 通过 AXI 让 DUT 真正发起 SPI 传输。两边的“预期”在传输结束时由 `p_spi` 自行比对（见 4.3 节）。

#### 4.2.3 源码精读

**复位序列**：`aresetn` 先拉低 1 µs 再拉高，复位前后都同步到 `aclk` 上升沿。注意 `aresetn` 是**低有效**复位（u2-l3 讲过 wrapper 内部会取反成高有效）。

[top_tb.vhd:171-177](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L171-L177) —— 复位：`aresetn<='0'` → 等 1 µs → 同步上升沿 → `aresetn<='1'` → 等 1 µs → 同步上升沿。

**场景① 只写事务**：选 slave 1，`StoreRx=0`（不读回），发 `0xAB`，然后轮询 `Status.Busy` 等完成。`p_spi` 这边会校验收到的是 `0xAB`（即 `ExpectedSlaveRx`）。

[top_tb.vhd:179-190](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L179-L190) —— 设 `SlaveNr<=1`、`ExpectedSlaveRx<=X"AB"` → 写 `SlaveNr*4=1` → 写 `StoreRx*4=0` → 写 `Data*4=0xAB` → `while Readback_v/=0` 轮询 `Status` 的 Busy 位。

> 这里的地址写成 `RegIdx_SlaveNr_c*4` 而不是直接写 `0x10`，是“以索引为单一数据源”的好习惯：`definitions_pkg` 里改了索引，testbench 自动跟上。这与 u2-l1 / u2-l7 讲的软硬件契约一致。

**场景② 读写事务**：选 slave 0，`StoreRx=1`（读回），发 `0x12`，期望从机在 MISO 上回送 `0x34`（`SlaveTx<=X"34"`），最后用 `axi_single_expect` 断言读 `Data` 得到 `0x34`。

[top_tb.vhd:192-205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L192-L205) —— 设 `SlaveTx<=X"34"`、`ExpectedSlaveRx<=X"12"` → 写 `SlaveNr=0` → 写 `StoreRx=1` → 写 `Data=0x12` → 轮询 Busy → `axi_single_expect(Data*4, 0x34, …)` 期望读回等于从机回送的 `0x34`。

这段就是 u2-l7 里 `SpiSimple_RxTxBlocking` 的 VHDL 等价物——寄存器访问顺序完全相同。

**场景③ FIFO 填充＋逐笔中断检查**：最复杂的一段。先配置中断（清全部 → 只使能 `TfDone` → 设 `TxAlmEmpty` 阈值 3、`RxAlmFull` 阈值 2），再连发 9 条命令灌进深度 8 的 TX FIFO（奇数条读、偶数条写），然后**逐笔**等待 `irq='1'`、在每笔事务“完成前”和“完成后”各读一次 `Status`/`TxLevel`/`RxLevel`/`IrqVec` 并断言每一位。

[top_tb.vhd:207-215](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L207-L215) —— 中断与阈值配置：`IrqVec←0xFF`（清全部，W1C）→ `IrqEna←2**Irq_TfDone_c`（只让“事务完成”产生中断）→ `TxAlmEmptyLevel←3`、`RxAlmFullLevel←2`。

[top_tb.vhd:221-225](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L221-L225) —— 连发 9 条命令：每条 `StoreRx = i mod 2`（奇数读、偶数写）、`Data = i`。9 条灌进深度 8 的 FIFO，必然先满后逐渐排空，正好用来检验水位与 almost 标志。

[top_tb.vhd:228-271](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L228-L271) —— 逐笔检查循环：对 `i=1..9`，先在事务完成**前**断言 `RxLevel=i/2`、`TxLevel=9-i` 及各状态/中断位，再 `wait until rising_edge(aclk) and irq='1'` 等中断，最后在完成**后**断言 `RxLevel=(i+1)/2`、`TxLevel=max(9-i-1,0)` 等。例如 `TxEmpty` 仅在 `i=9`（FIFO 真空）前置 1；`TxFull` 仅在 `i=1`（刚灌满）前置 1；`Busy` 在“前”恒为 1、在“后”仅 `i=9` 时为 0。

这段是 u2-l5（FIFO 水位公式 `TxLevel_before(i)=9-i`）和 u2-l6（中断锁存与使能）的活教材——每一行 `StdlCompare` 都对应一条已讲过的机制。

[top_tb.vhd:273-277](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L273-L277) —— 最后连读 5 次 `Data`，期望依次得到 `0x11/0x13/0x15/0x17/0x19`——这正是循环里奇数条（`i=1,3,5,7,9`，`StoreRx=1`）读回的、由 `SlaveTx` 在每笔递增后回送的字节，验证 RX FIFO 的先进先出顺序。

**场景④ RX 满＋中断清除**：连发 8 条读事务（`StoreRx=1`、`Data=0x12`）把响应 FIFO 灌到深度 8，验证 `RxFull` 状态位与 `Irq_RxFull` 中断位置 1；读走一个后该中断**仍置 1**（因为 FIFO 还满着，电平型自动重置，u2-l6）；显式写 `IrqVec` 清除后才归零；最后把剩 7 个读出来。

[top_tb.vhd:279-300](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L279-L300) —— 灌 8 条读命令 → 等 Busy → 断言 `Irq_RxFull` 与 `Status.RxFull` 均为 1 → 读一个 `Data`（得 `0x34`）→ 断言 `Irq_RxFull` **仍**为 1（电平型，FIFO 仍满）→ 写 `IrqVec←2**Irq_RxFull_c` 清除 → 断言 `Irq_RxFull` 与 `RxFull` 均归 0。

紧接着的“Test IRQ clearing”段直接复用前面残留的 `IrqVec` 状态，验证 W1C 的按位清除与电平型中断的自动重置：

[top_tb.vhd:305-312](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L305-L312) —— 先断言 `IrqVec=0x17`（=`0b10111`，TxEmpty/TxAlmEmpty/TfDone/RxAlmFull）→ 写 `0x0C`（=`0b01100`，清 bit2/bit3）→ 断言变 `0x13`（=`0b10011`）→ 再写 `0x02`（清 bit1 TxAlmEmpty）→ 断言**仍是** `0x13`（因为 TxAlmEmpty 的触发条件持续存在，被清后立即自动重置）。这正是 u2-l6 讲的“电平型中断清不掉”。

**收尾**：把 `TbRunning<=false`，让 `p_aclk` 和 `p_spi` 的循环退出、各自 `wait` 永久停住，仿真结束。

[top_tb.vhd:314-316](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L314-L316) —— `TbRunning <= false; wait;` 关停全局运行标志并让 `p_control` 自身挂起。

#### 4.2.4 代码实践（源码阅读型）

**目标**：把场景③里那一长串 `StdlCompare` 与 u2-l5/u2-l6 的机制逐条对上，理解每一句“为什么期望这个值”。

**步骤**：

1. 打开 [top_tb.vhd:234-240](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L234-L240)，挑出 `TxFull` 那一行：`StdlCompare(choose(i=1, 1, 0), …)`。
2. 回忆 u2-l5：9 条命令灌进深度 8 的 FIFO，`i=1` 时 FIFO 刚被写满 → `TxFull=1`；`i≥2` 时引擎已弹走至少一条 → 不满 → `TxFull=0`。
3. 同理核对 `TxAlmEmpty`：阈值 3，`i>=9-3=6` 时 `TxLevel<=3` → 置 1。
4. 最后看 [top_tb.vhd:262](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L262) 的 `Busy`：`choose(i=9, 0, 1)`——只在第 9 笔（最后一条）完成后 `Busy` 才为 0，因为此前 FIFO 里总有未发完的命令。

**预期结果**：每一句 `StdlCompare` 都能用“水位公式 `TxLevel=9-i`”或“almost 阈值”推出，不需要死记。

**观察现象**：你会注意到“完成前”和“完成后”两组断言几乎对称，差别只在 `i` 的边界（`i=1` 满、`i=9` 空）和 `Busy` 的翻转——这正是 FIFO 边界条件的完整覆盖。

#### 4.2.5 小练习与答案

**练习 1**：场景③为什么连发 9 条命令，而 FIFO 深度只有 8？
**答案**：8 条恰好灌满（验证 `TxFull`），第 9 条会在 FIFO 满时被软件背压挡住或排队，但更重要的是“9 条”能完整覆盖 FIFO 从满到空的全部水位（`TxLevel` 从 8 一路降到 0），让每一级 `TxEmpty/TxAlmEmpty` 都能被检验。

**练习 2**：场景④里读完一个数据后，为什么 `Irq_RxFull` 还是 1？
**答案**：响应 FIFO 深度 8、灌了 8 条读命令，读走 1 个后还剩 7 个——但此时引擎仍在后台把剩余命令的 MISO 继续写进响应 FIFO，FIFO 很快又满，`RxFull` 条件持续成立。`Irq_RxFull` 是电平型中断（u2-l6），条件持续就会在 W1C 清除后立刻被重新锁存，所以读一个不够，得显式写 `IrqVec` 清除。

---

### 4.3 p_spi 从机仿真与数据校验

#### 4.3.1 概念说明

光有 AXI 侧的“发命令/读状态”还不够——SPI 是**对端协议**，DUT 发出的 `spi_sck`/`spi_mosi` 必须有东西应答，`spi_miso` 上必须有东西驱动，否则读回的全是 `U`。`p_spi` 进程就扮成这个“对端 SPI 从机”，它的职责有四：

1. **片选校验**：每次传输开始，确认 `spi_cs_n` 只选中了 `SlaveNr` 指定的那一根，其余为高（未选）。
2. **逐 bit 收发**：按 `CPOL/CPHA` 决定的采样/驱动边沿，在 `spi_miso` 上逐位移出 `SlaveTx`、在 `spi_mosi` 上逐位移入并拼成接收字节。
3. **LE 断言**：传输期间 `spi_le` 应全低，CS 释放后只有被选从机的那一位 LE 变高（u3-l3 详讲）。
4. **数据比对**：把移位收到的字节和 `ExpectedSlaveRx` 比较，不符即报错。

#### 4.3.2 核心流程

```
p_spi:
  wait until aresetn='1';  wait until rising_edge(aclk);
  while TbRunning loop
    if spi_cs_n /= 全高 then            ── 检测到一次传输开始（某个 CS 被拉低）
      装载 ShiftRegTx←SlaveTx, ExpLatch←ExpectedSlaveRx
      for s in 0..SlaveCnt-1 loop        ── 片选校验
        被选的那根必须=0，其余必须=1
      for i in 0..TransWidth-1 loop      ── 逐 bit
        等待 apply 边沿  ── 驱动 MISO（按 CPHA/CPOL 决定上升/下降沿）
        移出 ShiftRegTx 的一位到 spi_miso
        等待 transfer 边沿 ── 采样 MOSI
        把 spi_mosi 移入 ShiftRegRx
      断言 spi_le = 全 0                  ── 传输中 LE 全低
      wait until spi_cs_n = 全高          ── 等 CS 释放
      断言 spi_le = 2**SlaveNr            ── 释放后只有被选从机的 LE 变高
      断言 ShiftRegRx == ExpLatch         ── 收到的字节正确
    else
      wait until rising_edge(aclk)        ── 空闲，按 AXI 时钟节拍轮询
```

这里的 **apply 边沿 / transfer 边沿**正是 u2-l4 讲过的 SPI 四种模式在 testbench 里的编码：apply 边沿是从机**驱动 MISO** 的时刻，transfer 边沿是从机**采样 MOSI** 的时刻。两者由 `CPOL`/`CPHA` 共同决定（默认 Mode 0：空闲低、上升沿采样、下降沿切换）。

#### 4.3.3 源码精读

**片选校验**：传输开始的判据是 `spi_cs_n /= OnesVector(SlaveCnt_c)`——只要有一根 CS 不是高，就说明某次传输开始了。然后遍历所有从机，被 `SlaveNr` 选中的那根必须为 0、其余必须为 1。

[top_tb.vhd:336-348](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L336-L348) —— 检测到 CS 拉低后，装载移位寄存器与期望值；`for s in 0 to SlaveCnt_c-1` 逐根校验：`s=SlaveNr` 期望 `spi_cs_n(s)=0`，否则期望 `=1`。这就是 4.2 节“选 slave 1/0”能被验证的落点。

> 注意 `SlaveNr` 是 `p_control` 在发起事务**前**就写好的共享信号（例如 [top_tb.vhd:180](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L180) 的 `SlaveNr <= 1`）。`p_spi` 读到它，才知道这次该期望哪根 CS 被拉低。这是两个进程通过信号协作的关键一处。

**逐 bit 收发**：`TransWidth_c=8`，循环 8 次。先等 apply 边沿（驱动 MISO），再等 transfer 边沿（采样 MOSI）。MSB first（`LsbFirst_c=false`）。

[top_tb.vhd:351-374](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L351-L374) —— 位循环：apply 边沿由 `SpiCPHA_c`/`SpiCPOL_c` 选上升或下降沿；按 MSB first 把 `ShiftRegTx_v(7)` 摆到 `spi_miso`，再把移位寄存器左移、低位补 `'U'`。

[top_tb.vhd:374-386](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L374-L386) —— transfer 边沿采样 `spi_mosi`，按 MSB first 左移进 `ShiftRegRx_v`。8 拍之后 `ShiftRegRx_v` 就是 DUT 这一帧发出的完整字节。

**LE 断言（两处）**：传输期间 LE 全低；CS 释放后只有被选从机的 LE 位变高，期望值是 `2**SlaveNr`（即只有第 `SlaveNr` 位为 1）。

[top_tb.vhd:388](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L388) —— 传输收尾、CS 仍低时：`StdlvCompareStdlv(ZerosVector(SlaveCnt_c), spi_le, "LE is not low")`，断言所有 LE 都为 0。

[top_tb.vhd:390-391](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L390-L391) —— `wait until spi_cs_n = OnesVector(SlaveCnt_c)` 等 CS 释放后，`StdlvCompareStdlv(to_uslv(2**SlaveNr,SlaveCnt_c), spi_le, "LE not high after transmission")`，断言只有第 `SlaveNr` 位 LE 为 1。例如 `SlaveNr=1` → 期望 `0b010`，`SlaveNr=2` → 期望 `0b100`。

**数据比对**：把移位收到的字节与 `p_control` 提前写好的 `ExpectedSlaveRx` 比较。

[top_tb.vhd:392](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L392) —— `StdlvCompareStdlv(ExpLatch_v, ShiftRegRx_v, "SPI slave received wrong data")`：从机实际收到的字节必须等于期望。例如场景②里 DUT 发 `0x12`，这里就校验 `ShiftRegRx_v = 0x12`。

至此，一次 SPI 传输的“片选 → 逐 bit 收发 → LE 时序 → 数据正确性”被 `p_spi` 全方位自检；配合 `p_control` 在 AXI 侧读回的 `0x34`（场景②），收发两个方向都闭环验证了。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把 `p_spi` 的 LE 断言与 u2-l4 的 Mode 0 时序对上，亲手画出一次 8-bit 传输的理想波形。

**步骤**：

1. 打开 [top_tb.vhd:322-330](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L322-L330)，确认本地常量 `SpiCPHA_c=0`、`SpiCPOL_c=0`、`LsbFirst_c=false`（即 Mode 0、MSB first），与 DUT 例化的 `SpiCPOL_g=0`/`SpiCPHA_g=0`（[top_tb.vhd:92-93](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L92-L93)）一致。
2. 对 Mode 0（`CPHA=0`），第一位 `i=0` **不**等 apply 边沿（[top_tb.vhd:359](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L359) 的 `elsif (SpiCPHA_c=0) and (i/=0)`），即首位在首个 SCK 沿到来前就已就位——这是 u2-l4 讲的“前导沿采样”的体现。
3. 画一组波形：`spi_cs_n`（选中期间低）、`spi_sck`（空闲低，8 个脉冲）、`spi_mosi`（逐位变化）、`spi_le`（传输中全低、CS 释放后第 `SlaveNr` 位高）。

**预期结果**：Mode 0 下，MOSI 的每一位在 SCK **上升沿**被 DUT 采样（对应 `p_spi` 的 transfer 边沿是 `rising_edge(spi_sck)`，[top_tb.vhd:375-377](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L375-L377)），MISO 的每一位在 SCK **下降沿**之后被从机驱动（apply）。

**观察现象**：你会看到 `spi_le` 在 `spi_cs_n` 拉低期间恒为 0，只在 `spi_cs_n` 回到全高的那一刻、在第 `SlaveNr` 位上跳到 1——这是 LE 作为“传输完成锁存脉冲”的直观波形。

> 待本地验证：本实践是“源码阅读＋画图”型，不实际运行。若要眼见为实，可在 sim 目录 `source ./run.tcl`（u1-l4）跑回归，在波形窗里观察 `spi_cs_n`/`spi_sck`/`spi_mosi`/`spi_le` 的实际波形与上述推断一致。

#### 4.3.5 小练习与答案

**练习 1**：`p_spi` 靠什么判断“一次传输开始了”？
**答案**：靠 `spi_cs_n /= OnesVector(SlaveCnt_c)`——一旦任意一根 CS 被拉低（不全为高），就认为某次传输开始（[top_tb.vhd:336](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L336)）。这与 SPI“片选低有效”的约定一致。

**练习 2**：CS 释放后 LE 的期望值是 `2**SlaveNr`，如果当前 `SlaveNr=2`、`SlaveCnt_c=3`，这个值是多少？哪一位为 1？
**答案**：`2**2 = 4 = 0b100`，即 3 位 LE 向量里只有最高位（bit 2，对应 slave 2）为 1，其余为 0。这正是 [top_tb.vhd:391](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L391) 断言的内容。

**练习 3**：为什么 `p_spi` 在 `ShiftRegRx_v` 的低位初始化为 `'U'`、移位时也补 `'U'`？
**答案**：`'U'` 表示“未初始化/未驱动”。因为从机是逐位移入数据的，尚未采到的位还没有合法值，用 `'U'` 占位可以避免把残留的旧值误当成有效数据；只有 8 拍全部移完之后，整个 `ShiftRegRx_v` 才是有效字节，随后才与 `ExpectedSlaveRx` 比较。

---

## 5. 综合实践

把 4.1～4.3 串起来，完成本讲指定的实践任务：**为 `top_tb` 增加一个注释良好的新场景——向 slave 2 发送两字节，并断言 `Busy` 在两次事务之间正确翻转**。要求给出插入位置、AXI 调用顺序，并写一版可直接粘贴的代码草稿。

### 任务分析

- **“向 slave 2 发送两字节”**：`TransWidth_g=8`，所以是两笔独立的 8-bit 事务（不是一笔 16-bit）。`SlaveCnt_c=3`，slave 编号 0/1/2 都合法，选 2 是为了覆盖最高位从机。
- **“断言 Busy 翻转”**：回顾 u2-l5，`Busy = (not TxEmpty) or SpiBusy`。写一次 `Data` 命令入队后 `TxEmpty=0` → `Busy=1`；事务做完、FIFO 排空后 `Busy=0`。所以两次事务之间 `Busy` 会经历 `1 → 0（第一笔做完）→ 1（第二笔入队）→ 0（第二笔做完）` 的翻转。
- **复用现有模式**：本场景在结构上最接近场景①（只写、`StoreRx=0`、轮询 Busy），把它扩展成“连发两次”并加上 `Busy` 断言即可。

### 插入位置

**推荐插入点**：[top_tb.vhd:205](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L205) 之后、[top_tb.vhd:207](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L207) 的 `*** Fill FIFO ***` 注释之前。理由：此时场景②刚结束，IP 处于干净空闲态（`Busy` 已轮询到 0、RX FIFO 已被 `axi_single_expect` 读空），插入新场景不会与前后的 FIFO/中断测试状态互相干扰。插入新代码后，原 207 行起的“Fill FIFO”段及其后所有行号会整体下移——记得同步更新本讲里引用的那些行号（或改为以“Fill FIFO 注释”等锚点描述位置）。

### AXI 调用顺序

```
0. (前提) IP 空闲：Busy=0, TX FIFO 空
1. SlaveNr <= 2;                                  ── 告诉 p_spi 这次校验 slave 2 的 CS
2. ExpectedSlaveRx <= X"A1";                      ── 第一笔期望从机收到 0xA1
   axi_single_write(SlaveNr*4,   2, ...)          ── 选 slave 2
   axi_single_write(StoreRx*4,   0, ...)          ── 只写，不读回
   axi_single_write(Data*4,   0xA1, ...)          ── 扣扳机，命令入队 → Busy 应变 1
3. 读 Status.Busy，断言 == 1（命令已入队/引擎忙）   ── 第一次“1”
4. 轮询 Status.Busy 直到 0（第一笔做完）            ── 翻转到“0”
5. ExpectedSlaveRx <= X"A2";                      ── 第二笔期望从机收到 0xA2
   axi_single_write(Data*4,   0xA2, ...)          ── 再扣扳机 → Busy 应回 1
6. 读 Status.Busy，断言 == 1                        ── 第二次“1”（证明两次之间 Busy 确实翻转过）
7. 轮询 Status.Busy 直到 0（第二笔做完）            ── 最终回到“0”
```

### 代码草稿（示例代码，非仓库原有文件）

下面是一段可直接粘贴到上述插入点的 VHDL（**示例代码**）。注意：因为命令入队到引擎真正开始移位之间有若干 AXI 时钟的延迟，步骤 3/6 读 `Busy` 前先等一个上升沿，给状态位一点更新时间；若本地仿真发现 `Busy` 尚未置起，可适当加一两个 `wait until rising_edge(aclk)`。

```vhdl
-- *** Two-byte transfer to slave 2, assert Busy toggles ***
-- 目标：向 slave 2 连发两字节(0xA1, 0xA2)，校验 Busy 在两次事务间正确翻转。
-- 依赖前提：此时 IP 空闲（Busy=0，TX FIFO 空），由前一个“Write/Read”场景保证。
SlaveNr <= 2;
ExpectedSlaveRx <= X"A1";                       -- p_spi 将校验从机收到 0xA1
axi_single_write(RegIdx_SlaveNr_c*4, 2, axi_ms, axi_sm, aclk);  -- 选 slave 2
axi_single_write(RegIdx_StoreRx_c*4, 0, axi_ms, axi_sm, aclk);  -- StoreRx=0：只写
axi_single_write(RegIdx_Data_c*4, 16#A1#, axi_ms, axi_sm, aclk);-- 第一笔入队
wait for 20 ns;
wait until rising_edge(aclk);
-- 第一笔入队后 Busy 应为 1
axi_single_read (RegIdx_Status_c*4, Readback_v, axi_ms, axi_sm, aclk,
                 BitIdx_Status_Busy_c, BitIdx_Status_Busy_c);
StdlCompare(1, Readback_v, "Busy should be 1 after first enqueue");
-- 等第一笔完成，Busy 翻转到 0
Readback_v := 1;
while Readback_v /= 0 loop
    axi_single_read(RegIdx_Status_c*4, Readback_v, axi_ms, axi_sm, aclk,
                    BitIdx_Status_Busy_c, BitIdx_Status_Busy_c);
end loop;
-- 立刻发第二笔，Busy 应回到 1（证明两次之间确实翻转过）
ExpectedSlaveRx <= X"A2";
axi_single_write(RegIdx_Data_c*4, 16#A2#, axi_ms, axi_sm, aclk);
wait for 20 ns;
wait until rising_edge(aclk);
axi_single_read (RegIdx_Status_c*4, Readback_v, axi_ms, axi_sm, aclk,
                 BitIdx_Status_Busy_c, BitIdx_Status_Busy_c);
StdlCompare(1, Readback_v, "Busy should be 1 after second enqueue");
-- 等第二笔完成
Readback_v := 1;
while Readback_v /= 0 loop
    axi_single_read(RegIdx_Status_c*4, Readback_v, axi_ms, axi_sm, aclk,
                    BitIdx_Status_Busy_c, BitIdx_Status_Busy_c);
end loop;
```

**需要观察的现象**：

- 四个断言点应全部通过：入队后 `Busy=1`、做完后 `Busy=0`、再入队又 `Busy=1`、再做完 `Busy=0`。任何一处不符都会在 transcript 打出对应的 `StdlCompare` 报错信息。
- 波形里 `spi_cs_n(2)` 应在两次传输期间分别被拉低，`p_spi` 的片选校验（[top_tb.vhd:342-348](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L342-L348)）会确认只有 slave 2 被选、其余两根为高。
- 因为 `StoreRx=0`，响应 FIFO 不应增长——若在场景后读 `RxLevel`，期望仍为 0。

**预期结果**：新场景跑完后 transcript 无新增 `###ERROR###`，整份回归仍以 `SIMULATIONS COMPLETED SUCCESSFULLY` 收尾（u1-l4 的 CI 判定契约）。

> 待本地验证：`Busy` 在“入队后、引擎启动前”是否已经置 1，取决于命令 FIFO 弹出命令的相对时序。若第一处 `StdlCompare(1, …)` 偶发失败，可把前置的 `wait for 20 ns; wait until rising_edge(aclk);` 加长到两三个周期，确保 `Busy`（含 `not TxEmpty` 分量）已经更新。

---

## 6. 本讲小结

- `top_tb` 用两个 record（`axi_ms` 主出从入 / `axi_sm` 从出主入）收纳 AXI4 五通道，再由 `psi_tb_axi_pkg` 的 `axi_single_write/read/expect` 三个 BFM 过程驱动，免去了手写 VALID/READY 时序。
- DUT 例化自 `spi_vivado_wrp`，generic 全取“便于测试”的小值（`TransWidth=8`、`FifoDepth=8`、`SlaveCnt=3`），`p_aclk` 产生 125 MHz 时钟，复位低有效先低后高。
- `p_control` 是导演进程，跑四个场景：只写、读写、FIFO 填充＋逐笔中断检查、RX 满＋中断清除——分别覆盖 u2-l2～u2-l6 讲的 TX-only/读回、FIFO 水位与 almost 阈值、`TfDone`/`RxFull` 中断与 W1C 清除、电平型中断自动重置。
- `p_control` 与 `p_spi` 通过 architecture 共享信号（`SlaveTx`/`ExpectedSlaveRx`/`SlaveNr`）协作：前者在发起事务前写“预期”，后者在事务中据此自检。
- `p_spi` 扮演 SPI 从机：靠 `spi_cs_n` 不全高判定传输开始、逐根校验片选、按 CPOL/CPHA 的 apply/transfer 边沿逐 bit 收发、断言 LE（传输中全低、CS 释放后 `2**SlaveNr`）、并比较收到的字节。
- 两个进程的校验闭环：AXI 侧读回的 MISO（如 `0x34`）与 SPI 侧收到的 MOSI（如 `0x12`）双向互证，让收发两个方向都被验证。

## 7. 下一步学习建议

- **进入专家层**：本讲对 LE 的断言只讲了“怎么测”，LE 输出在 RTL 里**怎么生成**、为什么是“传输中低、完成后高”留到 u3-l3（LE 锁存使能输出时序）详讲。
- **动手扩展 testbench**：尝试把 DUT 例化的 `SpiCPHA_g`/`SpiCPOL_g`（[top_tb.vhd:92-93](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L92-L93)）改成 Mode 1/2/3，同步改 `p_spi` 里的 `SpiCPHA_c`/`SpiCPOL_c`（[top_tb.vhd:324-325](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L324-L325)），跑回归验证 apply/transfer 边沿切换后收发仍正确——这是把 u2-l4 的四种模式吃透的最好练习。
- **理解打包与 CI**：本讲的 testbench 由 sim 目录的 `run.tcl`/`config.tcl` 编译运行、由 `ciFlow.py` 在 CI 里判定成败（u1-l4）；而 DUT 本身怎么被打包成 Vivado IP、`top_tb` 如何被纳入回归，是 u3-l4（IP 打包与发布）和 u3-l5（CI 与开发工作流）的内容。
