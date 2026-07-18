# 外设与 IO：io 命名空间

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `uart_tx` / `uart_rx` 如何用「移位寄存器 + 位时钟选通」实现 1 起始位 + 8 数据位 + 1 停止位的异步串行收发，并能解释为什么波特率分频不在 `uart_tx` 内部。
- 理解 `ddrio_out` 这类 DDR（Double Data Rate）IO 包装实体如何沿用 u3-l2 的「通用包装实体 + 厂商专用子实体」分层，把 Xilinx 的 `ODDR`/`OBUFT` 原语与 Altera 的 `altddio_out` 原语藏到统一接口背后。
- 掌握 `io_Debounce`、`io_7SegmentMux_BCD` 等「慢速 IO」核如何把 `physical` 包里的人话时间（`BOUNCE_TIME`、`REFRESH_RATE`）通过 `TimingToCycles` 换算成计数器位宽。

本讲是第 3 单元「IP 核模式与命名空间」的收尾，把前几讲建立的**命名空间包模式**（u3-l1）、**厂商选择机制**（u3-l2）、**公共包**（u2-l2 / u2-l4 / u2-l5）和**同步器**（u3-l6）汇拢到一类贴近真实管脚的外设上。

## 2. 前置知识

在进入源码前，先用大白话过一遍本讲会反复出现的概念：

- **UART（通用异步收发器）**：两根线（TX/RX）、没有共享时钟的串行协议。发送端把一个字节拆成「起始位（0）+ 8 个数据位（LSB 在先）+ 停止位（1）」逐位 drove 出去；接收端靠事先约定的**波特率**（每秒位数）自己掐时间采样。因为没有时钟线，收发双方必须用同一个波特率。
- **波特率与位时钟**：如果系统时钟是 100 MHz、波特率是 115200 Bd，那么每个数据位持续 \( 100\,000\,000 / 115200 \approx 868 \) 个系统时钟周期。把这个「位长度」变成一个每「位」拉高一个周期的选通脉冲，就是本讲的 `bclk`（bit clock）。
- **过采样（oversampling）**：接收端为了在每位中点采样、避开边沿抖动，常用 8 倍位时钟 `bclk_x8`，在每位的中段取值。
- **DDR（Double Data Rate）IO**：普通 IO 一个时钟周期输出 1 个比特（上升沿采样）；DDR IO 在上升沿和下降沿各输出 1 个比特，时钟不变 throughput 翻倍。FPGA 里这种寄存器只存在于 **IOB（I/O Block，管脚块）** 中，必须用厂商原语例化。
- **消抖（debounce）**：机械按键按下/弹起时触点会在几毫秒内反复通断，产生一串毛刺。消抖器要求信号「稳定持续一段时间」后才更新输出，把毛刺滤掉。
- **七段数码管时分复用**：多位数码管共用 8 根段线（a~g + 小数点），用「公共端」一次点亮一位。快速轮流切换（如 1 kHz）配合人眼视觉暂留，看起来所有位都常亮。

这些概念在源码里都有对应物，后面逐个对照。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/io/io.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io.pkg.vhdl) | `PoC.io` 命名空间根包：三态/LVDS 记录类型、七段编码表 `io_7SegmentDisplayEncoding`、MDIO/LCD 命令枚举、`io_FanControl` 组件声明 |
| [src/io/io_Debounce.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl) | 多比特输入消抖器，可选内嵌两级同步器、可选「公共锁存」 |
| [src/io/io_7SegmentMux_BCD.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_7SegmentMux_BCD.vhdl) | BCD 编码的七段数码管时分复用控制器 |
| [src/io/uart/uart.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart.pkg.vhdl) | `PoC.io.uart` 子命名空间包：声明 `uart_bclk`/`uart_rx`/`uart_tx`/`uart_fifo`/`ft245_uart` 组件与典型波特率表 |
| [src/io/uart/uart_bclk.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl) | 波特率/位时钟发生器，产生 `bclk`（每位 1 个选通）与 `bclk_x8`（每位 8 个选通） |
| [src/io/uart/uart_tx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_tx.vhdl) | UART 发送器（核心实践对象） |
| [src/io/uart/uart_rx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_rx.vhdl) | UART 接收器，靠 `bclk_x8` 过采样在中点取值 |
| [src/io/ddrio/ddrio.pkg.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio.pkg.vhdl) | `PoC.io.ddrio` 子命名空间包：声明 `ddrio_in`/`ddrio_out`/`ddrio_inout` 及各厂商变体 |
| [src/io/ddrio/ddrio_out.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.vhdl) | DDR 输出包装实体：按 `VENDOR` 在 generate 中分发到厂商子实体 |
| [src/io/ddrio/ddrio_out_xilinx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_xilinx.vhdl) | Xilinx 实现：例化 UniSim 库的 `ODDR` + `OBUFT` 原语 |
| [src/io/ddrio/ddrio_out_altera.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_altera.vhdl) | Altera 实现：例化 `altera_mf` 库的 `altddio_out` 原语 |
| [src/io/ddrio/ddrio_out.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.files) | pyIPCMI 编译清单：按 `DeviceVendor` 编译期选择厂商文件与原语库 |

> 提示：`PoC.io` 是「大命名空间」，下挂 `uart`、`ddrio`、`iic`、`lcd`、`mdio`、`ow`、`pmod`、`ps2`、`vga` 等子命名空间（见 [src/io/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/README.md)）。本讲挑三类有代表性的讲：协议类（UART）、管脚类（ddrio）、慢速类（Debounce / 七段）。

---

## 4. 核心概念与源码讲解

### 4.1 UART 收发

#### 4.1.1 概念说明

UART 是 FPGA 与 PC、传感器之间最便宜的双工通道：只要两根线、不需时钟。代价是收发双方必须事先约定波特率，且发送端要自己把字节「串行化」成位流、接收端要自己「对齐」起始位并采样。

PoC 把 UART 拆成三个职责单一的小核，全部在 `PoC.io.uart` 子命名空间下：

- `uart_bclk`——**波特率/位时钟发生器**。吃系统时钟，吐出两个选通脉冲：`bclk`（每个数据位 1 个脉冲，给发送器用）和 `bclk_x8`（每个数据位 8 个脉冲，给接收器过采样用）。
- `uart_tx`——**发送器**。只要一个 `bclk` 选通就能干活，本身**不含任何波特率分频**。
- `uart_rx`——**接收器**。靠 `bclk_x8` 过采样，自带可选输入同步器。

这种「时钟生成」与「数据移位」分离的设计是本模块最重要的工程直觉，下面会反复回到它。

#### 4.1.2 核心流程

发送一帧（`uart_tx`）的状态机可以浓缩成几行伪代码：

```
常数：每帧 10 位 = 起始位(0) + 8 数据位 + 停止位(1)，LSB 先发
空闲：tx = 1（线路高），Cnt 的符号位 = 0
收到 put：把 di(7..0) & "01" 装进 10 位移位寄存器 Buf，Cnt ← -10
发送中（Cnt 符号位 = 1）：
    每来一个 bclk 选通：Buf 右移 1 位（高位补 1），Cnt ← Cnt + 1
    tx ← Buf(0)        // 顺序输出：起始位 → d0..d7 → 停止位
    当 Cnt 数到 0（符号位翻 0）回到空闲
```

关键变量只有两个：一个 10 位移位寄存器 `Buf`，一个 5 位有符号计数器 `Cnt`。**`Cnt` 的最高位（符号位）身兼两职**：当「忙」标志（`ful`）和当状态判断位——`0` 代表空闲、`1` 代表正在发送。

接收一帧（`uart_rx`）则反过来：在线路空闲（高）时检测到下降沿（起始位），延迟约 1.5 个位长后开始每 8 个 `bclk_x8` 周期采样一次，移位收齐 8 位后给出一个周期的 `stb` 选通和 `do` 字节。

#### 4.1.3 源码精读

**先看发送器的端口**——注意它**没有 generic**：

[src/io/uart/uart_tx.vhdl:L35-L50](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_tx.vhdl#L35-L50) —— `uart_tx` 的 entity。`bclk` 是输入的「位时钟选通」，`di/put/ful` 构成一字节宽的简单握手：`put` 拉高表示「请发送 `di`」，`ful` 拉高表示「正在发，别喂」。波特率完全由外部 `bclk` 决定。

**再看它如何用移位实现整帧**：

[src/io/uart/uart_tx.vhdl:L64-L98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_tx.vhdl#L64-L98) —— `Buf` 是 10 位移位寄存器，`Cnt` 是 5 位有符号计数器。文件头部的注释把四个阶段画得很清楚：

```
--                Buf           Cnt
--   Idle     "---------1"    "0----"
--   Start    "hgfedcba01"     -10
--   Send     "1111hgfedc"   -10 -> -1
--   Done     "1111111111"       0
```

`Buf <= di & "01"` 这一句是精髓：把 8 位数据 `di` 拼上起始位 `0` 和停止位 `1`，一次性装填成一帧（`Buf(1)` 是起始位、`Buf(0)` 是停止位）。之后每次 `bclk` 选通执行 `Buf <= '1' & Buf(Buf'left downto 1)`——右移、高位补 `1`（补的是停止位/空闲电平）。`tx <= Buf(0)` 持续输出最低位，于是线路上的顺序正是 **起始位 → d0 → … → d7 → 停止位**。`Cnt(Cnt'left)` 即符号位，`1` 为发送中（同时驱动 `ful`），数到 `0` 自动回空闲。

**波特率分频到底在哪？在 `uart_bclk`**：

[src/io/uart/uart_bclk.vhdl:L64-L79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl#L64-L79) —— 关键的三行常量推导。注意它**复用了 u2-l4 讲过的 `physical` 包**：`BAUDRATE` 是带量纲的 `BAUD` 类型（如 `115200 Bd`），`CLOCK_FREQ` 是 `FREQ`（如 `100 MHz`）。

位周期换算的数学关系：

\[
T_{\text{unit}} = \frac{1\,\text{sec}}{\text{BAUDRATE} \times 8}, \qquad
N_{\text{div}} = \text{TimingToCycles}(T_{\text{unit}}, \text{CLOCK\_FREQ})
\]

即「8 倍过采样下的一个时间单元」折算成系统时钟周期数。计数器位宽用 u2-l2 的 `log2ceilnz` 推导。

[src/io/uart/uart_bclk.vhdl:L93-L106](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl#L93-L106) —— 用 u2-l5 `components` 包的 `upcounter_next`/`upcounter_equal` 造一个模 \(N_{\text{div}}\) 计数器 `x8_cnt`，溢出脉冲即 `bclk_x8`；再用 3 位计数器 `x1_cnt` 对 `bclk_x8` 八分频，得到每「位」一次的 `bclk`。`bclk` 只在 `x1_cnt_done and x8_cnt_done` 同时成立时拉高一拍，严格「每位 1 个选通」。

**接收器对起始位与中点采样的处理**：

[src/io/uart/uart_rx.vhdl:L78-L117](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_rx.vhdl#L78-L117) —— 先用 `PoC.sync_Bits`（u3-l6 讲过的两级同步器，`SYNC_DEPTH` 默认 2）把异步的 `rx` 线同步到本时钟域，**这是 UART 接收的第一次 CDC**。检测到起始位后 `Cnt <= 5`，配合每位 8 个 `bclk_x8` 选通，把首个采样点偏移到起始位中点之后约 1.5 个位长，从而后续 8 个数据位都落在各自中点。收齐 8 位后 `Vld` 拉高一拍作为 `stb`，`do = Buf(8 downto 1)` 输出字节。

**子命名空间包把它们打包成一张目录页**（u3-l1 的命名空间包模式）：

[src/io/uart/uart.pkg.vhdl:L57-L106](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart.pkg.vhdl#L57-L106) —— 集中声明 `uart_bclk` / `uart_rx` / `uart_tx` 三个组件，供上层 `use PoC.uart.all` 后直接例化。包里还有一张典型波特率表 `C_IO_UART_TYPICAL_BAUDRATES`（300 Bd 到 921600 Bd，元素类型是 `physical` 包的 `T_BAUDVEC`），`uart_bclk` 在 [L89-L91](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl#L89-L91) 用 `io_UART_IsTypicalBaudRate` 检查用户填的波特率是否「常见」，不常见只给 `WARNING`（不报错）。

> 工程直觉：把「时钟生成」从「数据通路」剥离，意味着同一份 `uart_tx` 既能跑 9600 Bd 也能跑 921600 Bd，只取决于喂给它的 `bclk`；而 `bclk` 的精度由 `uart_bclk` 的 `CLOCK_FREQ`/`BAUDRATE` generic 决定。这种解耦是 PoC 让小核可复用的典型手法。

#### 4.1.4 代码实践

> 这正是本讲指定的实践任务。

**实践目标**：阅读 `uart_tx.vhdl`，列出它的 generic 与端口，并画出一次「起始位 + 数据位 + 停止位」的发送时序；进而发现「波特率分频不在 `uart_tx` 里」。

**操作步骤**：

1. 打开 [src/io/uart/uart_tx.vhdl:L35-L50](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_tx.vhdl#L35-L50)，逐行列出端口：
   - 全局控制：`clk`、`rst`
   - 位时钟与发送线：`bclk`（in，位选通）、`tx`（out）
   - 字节输入：`di`(7..0)、`put`、`ful`
2. 找 generic 区段——**你会发现没有 `generic` 声明**。这是一个反直觉但重要的结论：`uart_tx` 不知道波特率，它只认 `bclk` 选通。
3. 追问「波特率分频在哪」，跳到 [src/io/uart/uart_bclk.vhdl:L64-L79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl#L64-L79)，记下 `CLOCK_FREQ`、`BAUDRATE` 两个 generic 才是真正的波特率来源。
4. 假设要发送字节 `0x41`（ASCII `'A'` = `0100_0001`），手工推演 `Buf` 与 `tx`。装载后 `Buf = "01000001" & "01"`，最低位 `Buf(0)=1`（空闲高）。

**需要观察的现象 / 发送时序**（设 `bclk` 每隔一个位长来一次）：

```
bclk:   _____|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|_|‾‾|___   (10 个选通)
tx:     ‾‾‾‾‾‾‾0    1    0    0    0    0    0    1    0    1‾‾‾‾‾    (每位持续一个位长)
           空闲 起   d0   d1   d2   d3   d4   d5   d6   d7   停止
                位                                      (MSB)
```

对应位流：起始位 `0` → `d0=1` → `d1=0` → … → `d7=0` → 停止位 `1`。（注：上图是示意图，帮助你理解位序；具体高低电平在本地仿真波形里核对。）

**预期结果**：
- `ful` 在 `put` 被接受后立即拉高，并在 10 个 `bclk` 选通内保持高（发送中），随后回低。
- `tx` 上严格出现 1 个低起始位 + 8 个数据位（LSB 先）+ 1 个高停止位。
- 改变 `uart_bclk` 的 `BAUDRATE` generic，`uart_tx` 的代码与端口**完全不用动**，位长自动随之改变。

> 待本地验证：上述时序图基于源码静态推演，建议在仿真器里实际跑一遍（可用 u4-l1 介绍的 `simGenerateClock` 造 100 MHz 时钟）以观察波形。

#### 4.1.5 小练习与答案

**练习 1**：`uart_tx` 用 `Cnt` 的符号位同时当「忙标志」和「状态判断」，这样做的代价是什么？

**参考答案**：好处是省去独立状态寄存器、逻辑极简；代价是 `Cnt` 的位宽要足以容纳 `-10`（5 位有符号刚够：`-10..0`），且计数范围与帧长（固定 10 位）耦合——若想改成 9 位或 11 位帧，必须同步调整 `Cnt` 位宽与装载初值，扩展性不如显式状态机。

**练习 2**：为什么接收器用 `bclk_x8`（8 倍频）而发送器只用 `bclk`（1 倍频）？

**参考答案**：发送端自己产生位流，只需在每位边界翻转，用 1 倍选通即可；接收端的位边界与本地时钟异步，需要 8 倍过采样才能把采样点放到每位**中点**，避开起始位检测误差与线路抖动，保证采到稳定电平。

**练习 3**：`uart_rx` 在 [L78](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_rx.vhdl#L78) 例化了 `sync_Bits`。如果 `rx` 已经和 `clk` 同源（例如来自同一 FPGA 内部另一个同时钟域模块），如何省掉这级同步？

**参考答案**：把 `uart_rx` 的 generic `SYNC_DEPTH` 设为 `0`（见 [uart.pkg.vhdl:L72-L73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart.pkg.vhdl#L72-L73) 的注释 "use zero for already clock-synchronous rx"），`sync_Bits` 退化为直通，省去两级触发器与对应的亚稳态约束。

---

### 4.2 DDR IO 封装

#### 4.2.1 概念说明

DDR IO 要在时钟的上升沿和下降沿各搬一个比特，这种「上下沿都打」的寄存器在 FPGA 里只长在 **IOB（管脚块）** 内，且每家厂商叫法不同：Xilinx 叫 `ODDR`/`IDDR` + 三态缓冲 `OBUFT`/`IBUF`（在 `UniSim` 库），Altera 叫 `altddio_out`/`altddio_in`（在 `altera_mf` 库）。

如果每个核都直接例化原语，代码就锁死在一家厂商。PoC 的做法正是 u3-l2 讲过的双层选择框架：

- 一个**通用包装实体** `ddrio_out`，对外只暴露厂商无关的 generic/port。
- 内部用 `if generate` 按 `VENDOR` 分发到 `ddrio_out_xilinx` / `ddrio_out_altera` / 通用仿真模型。
- 编译期由 `ddrio_out.files` 按 `DeviceVendor` 决定编译哪个子实体、引入哪个原语库。

#### 4.2.2 核心流程

```
上层核                    ddrio_out（包装实体）              厂商子实体
DataOut_high ─┐
DataOut_low  ─┼─►  generic: BITS / INIT_VALUE /      ┌─ VENDOR_XILINX  ─► ddrio_out_xilinx ─► ODDR + OBUFT
OutputEnable ─┤      NO_OUTPUT_ENABLE                 │
Clock ────────┤     ── generate 按 VENDOR 三选一 ──┼─ VENDOR_ALTERA  ─► ddrio_out_altera ─► altddio_out
              │                                     └─ SIMULATION +    ─► 通用 RTL 行为模型
Pad ◄─────────┴─ out                                                  (VENDOR_GENERIC)
```

关键点：`DataOut_high` 与 `OutputEnable` 在上升沿被采样，`DataOut_high` 随上升沿出到管脚，`DataOut_low` 随下降沿出到管脚。`OutputEnable` 高有效，内部按需取反；若不需要三态可设 `NO_OUTPUT_ENABLE = true` 省一组寄存器。

#### 4.2.3 源码精读

**包装实体的接口与厂商分发**：

[src/io/ddrio/ddrio_out.vhdl:L79-L102](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.vhdl#L79-L102) —— entity 与一道 `assert ... severity FAILURE` 兜底：若厂商既不是 Xilinx 也不是 Altera（且非仿真+Generic），直接综合失败，提示「未实现」。这是 u3-l2 提到的「未覆盖厂商用 assert failure 兜底」策略。注意 `INIT_VALUE` 是 `bit_vector`（不是 `std_logic_vector`），上电初值与复位值在此统一。

[src/io/ddrio/ddrio_out.vhdl:L103-L162](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.vhdl#L103-L162) —— 三个 generate 分支：`genXilinx` / `genAltera` / `genGeneric`。前两者只是把同名 generic/port 原样映射给子实体；`genGeneric` 受 `SIMULATION and (VENDOR = VENDOR_GENERIC)` 守卫，用一个上升沿寄存 + 组合多路选择（按 `to_bit(Clock)` 选 high/low）的简单模型模拟 DDR 行为，仅供仿真。

**Xilinx 实现——例化 IOB 原语**：

[src/io/ddrio/ddrio_out_xilinx.vhdl:L60-L113](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_xilinx.vhdl#L60-L113) —— 每个比特例化一个 `ODDR` 原语（来自 `UniSim.vComponents`），`D1=DataOut_high`、`D2=DataOut_low`、`DDR_CLK_EDGE="SAME_EDGE"`；三态用**第二个 `ODDR`** 把 `OutputEnable` 同样打到 DDR 寄存器，再经 `OBUFT` 三态缓冲输出到 `Pad`。注释特意说明「显式例化三态 I/O 缓冲是为了让本实体能作为网表被其他设计例化」——即支持 u4-l4 的 out-of-context 综合。

**Altera 实现——例化 `altddio_out`**：

[src/io/ddrio/ddrio_out_altera.vhdl:L68-L86](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_altera.vhdl#L68-L86) —— 每个比特例化一个 `altddio_out`（来自 `Altera_mf.Altera_MF_Components`），`WIDTH=>1`、`datain_h`/`datain_l` 接高低数据。注释点出 Altera 与 Xilinx 的一个语义差异：`POWER_UP_HIGH` 同时控制输出数据与输出使能寄存器的上电值，而 `INIT_VALUE` 仅在 `NO_OUTPUT_ENABLE = true` 时才相关。这正是「同接口、不同厂商细节」需要包装层吸收的地方。

**编译期按厂商选文件**（u3-l2 的「另一层选择」）：

[src/io/ddrio/ddrio_out.files:L11-L25](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.files#L11-L25) —— `if (DeviceVendor = "Altera")` 分支 `include "lib/Altera.files"` 并编译 `ddrio_out_altera.vhdl`；`Xilinx` 分支 `include "lib/Xilinx.files"` 并编译 `ddrio_out_xilinx.vhdl`；`Simulation + Generic` 走简单实现；最后无论如何都编译厂商无关的 `ddrio_out.vhdl`。**两层选择必须由同一份 `MY_DEVICE` 驱动**：`.files` 决定编译哪个子实体 + 引入哪个原语库，`generate` 决定展开期实例化哪一个，两者靠 `config` 包解析出的 `VENDOR` 保持一致。

> 工程直觉：`ddrio_out` 是 u3-l2 厂商选择机制的「教科书示例」——比 `sync_Bits` 多出来的看点是它处理的是**只能存在于 IOB 的厂商原语**，且需要显式三态缓冲，因此包装层不仅要选实现，还要把三态、上电值、初始化等厂商差异全部吸收成统一 generic。

#### 4.2.4 代码实践

**实践目标**：通过对比 `ddrio_out` 包装实体与两家厂商子实体，体会「同接口、不同原语」的封装价值。

**操作步骤**：

1. 打开 [ddrio_out.vhdl:L79-L93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.vhdl#L79-L93)，记下通用接口：generic `NO_OUTPUT_ENABLE`/`BITS`/`INIT_VALUE`，port `Clock`/`ClockEnable`/`OutputEnable`/`DataOut_high`/`DataOut_low`/`Pad`。
2. 对比 [ddrio_out_xilinx.vhdl:L63-L77](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_xilinx.vhdl#L63-L77) 的 `ODDR` 与 [ddrio_out_altera.vhdl:L70-L85](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_altera.vhdl#L70-L85) 的 `altddio_out`，列出两者各自依赖的原语库名。
3. 打开 [ddrio_out.files:L11-L25](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out.files#L11-L25)，确认 Xilinx 分支引入 `lib/Xilinx.files`（即 UniSim）、Altera 分支引入 `lib/Altera.files`（即 altera_mf）。
4. 写一段「示例代码」例化 `ddrio_out`（**仅为说明用法，非项目原有代码**）：

```vhdl
-- 示例代码：把 8 位 DDR 数据送到管脚（厂商无关）
ddr_out_inst : entity PoC.ddrio_out
  generic map (
    BITS            => 8,
    INIT_VALUE      => x"00",
    NO_OUTPUT_ENABLE => false
  )
  port map (
    Clock         => clk,
    ClockEnable   => '1',
    OutputEnable  => oe,
    DataOut_high  => data_high,   -- 上升沿搬出
    DataOut_low   => data_low,    -- 下降沿搬出
    Pad           => pad_pins     -- 必须连到物理 PAD
  );
```

**需要观察的现象**：
- 包装层 generic/port 完全不出现 `ODDR`/`altddio_out` 等厂商字样，上层核无需知道目标厂商。
- `INIT_VALUE` 在 Xilinx 走 `ODDR` 的 `INIT`，在 Altera 走 `POWER_UP_HIGH`，被包装层翻译。

**预期结果**：你能在不改动上层例化代码的前提下，仅通过改 `MY_DEVICE` 让同一个设计在 Xilinx 和 Altera 之间切换。综合后查看版图，确认 `ddrio_out` 的寄存器被放进 IOB 而非普通逻辑阵列。

> 待本地验证：寄存器落入 IOB 这一现象需在 Vivado/Quartus 综合后的资源报告或版图中确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ddrio_out` 的文档强调「`Pad` 必须连到 PAD」？

**参考答案**：DDR 输出寄存器（`ODDR`/`altddio_out`）只存在于 FPGA 的 IOB（管脚块）中，普通 LAB/CLB 逻辑阵列没有上下沿寄存器。若 `Pad` 指向内部信号，综合器无法把寄存器放进 IOB，DDR 行为就无法实现。

**练习 2**：`NO_OUTPUT_ENABLE = true` 省下了什么？两家厂商分别怎么省？

**参考答案**：省下「输出使能」那一路 DDR 寄存器与三态缓冲。Xilinx 实现里 `genNoOE` 分支直接 `Pad(i) <= o`，不再例化第二个 `ODDR` 和 `OBUFT`（[ddrio_out_xilinx.vhdl:L110-L112](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_xilinx.vhdl#L110-L112)）；Altera 实现里把 `OE_REG` 设为 `"UNREGISTERED"` 并令 `oe='1'` 常通（[ddrio_out_altera.vhdl:L62,L72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/ddrio/ddrio_out_altera.vhdl#L62)）。

**练习 3**：`ddrio_out.vhdl` 的 `genGeneric` 分支为什么用 `to_bit(Clock)` 做多路选择就能模拟 DDR？

**参考答案**：仿真模型先把 `DataOut_high`/`DataOut_low` 用上升沿寄存一拍，再用 `to_bit(Clock)` 在时钟高/低电平期间组合选择输出对应的数据，从而在波形上呈现「上升沿出 high、下降沿出 low」的 DDR 观感——它不依赖任何厂商原语，所以能在 `VENDOR_GENERIC` 仿真环境里跑。

---

### 4.3 慢速 IO 处理

#### 4.3.1 概念说明

并非所有 IO 都跑高频：按键、数码管、慢速传感器都是「人眼/机械时间尺度」的外设。这类核的共同特征是：**用系统时钟驱动计数器，但关键参数用人话时间（毫秒、千赫兹）描述**。PoC 用 u2-l4 的 `physical` 包把人话时间编译期换算成计数器位宽，让代码读起来像规格书。

本模块看两个代表：
- `io_Debounce`：按键消抖。可选内嵌两级同步器（直接复用 u3-l6 的 `sync_Bits`），可选「公共锁存」让所有按键共享一个定时器。
- `io_7SegmentMux_BCD`：多位七段数码管时分复用。用选通发生器轮流点亮各位，段码由 `io.pkg` 的 `io_7SegmentDisplayEncoding` 查表给出。

#### 4.3.2 核心流程

**消抖（`io_Debounce`）**：

```
Input ──(可选 sync_Bits 两级同步)──► sync
                                         │
   检测 prev ≠ sync（输入抖动）──► 把 Lock 计数器装入 -LOCK_COUNT_X
                                         │
   Lock 从负数往上数，符号位=1 表示「锁定中」 ── locked
                                         │
   active = not locked：只有解锁后 Output(i) 才跟随 sync(i)
```

`LOCK_COUNT_X = TimingToCycles(BOUNCE_TIME, CLOCK_FREQ) - 1`，即把 `BOUNCE_TIME`（如 10 ms）换算成系统时钟周期数。输入每抖一次就重装负数、重新锁定，逼着输入「稳定持续整段 `BOUNCE_TIME`」才能改写输出。

**七段复用（`io_7SegmentMux_BCD`）**：

```
misc_StrobeGenerator ──(每 1/REFRESH_RATE 一个选通)──► DigitCounter_en
                                                              │
   DigitCounter_us 在 0..DIGITS-1 间循环 ──► DigitControl(独热) 选中当前位
                                                              │
   用 DigitCounter_us 索引当前 BCD 位 ──► io_7SegmentDisplayEncoding ──► SegmentControl(a..g+小数点)
```

#### 4.3.3 源码精读

**消抖器的关键常量与可选同步**：

[src/io/io_Debounce.vhdl:L70-L96](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L70-L96) —— 第 72 行 `LOCK_COUNT_X` 用 u2-l4 的 `TimingToCycles` 把 `BOUNCE_TIME` 换算成周期数；第 82~96 行根据 `ADD_INPUT_SYNCHRONIZERS`（默认 `true`）决定是否例化 `PoC.sync_Bits`——**这正是 u3-l6 同步器的直接复用**，因为按键信号相对系统时钟是异步的，必须先做 CDC。`INIT` 同时被同步器与输出寄存器用作初值。

**锁存定时器**：

[src/io/io_Debounce.vhdl:L116-L154](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L116-L154) —— `COMMON_LOCK` 决定锁存器数量 `LOCKS`：`true` 时全 BIT 共享 1 个定时器（适合人肉按键，要求所有输入一起稳定），`false` 时每 BIT 一个独立定时器。每个 `Lock` 是 `signed(log2ceil(LOCK_COUNT_X+1) downto 0)`（位宽由 u2-l2 的 `log2ceil` 推导），抖动时装入 `-LOCK_COUNT_X`，之后每周期 `+1`，符号位即 `locked`。`active <= not locked`，只有解锁位才允许 `Output(i) <= sync(i)`。

**七段复用器**：

[src/io/io_7SegmentMux_BCD.vhdl:L45-L60](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_7SegmentMux_BCD.vhdl#L45-L60) —— generic 全用物理类型：`CLOCK_FREQ : FREQ := 100 MHz`、`REFRESH_RATE : FREQ := 1 kHz`、`DIGITS := 4`。读起来就像规格书「在 100 MHz 时钟下、以 1 kHz 刷新 4 位数码管」。

[src/io/io_7SegmentMux_BCD.vhdl:L69-L98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_7SegmentMux_BCD.vhdl#L69-L98) —— `Strobe` 例化 `misc_StrobeGenerator`，周期 = `TimingToCycles(to_time(REFRESH_RATE), CLOCK_FREQ)`；`DigitCounter_us` 在选通驱动下循环计数 0..`DIGITS-1`，`DigitControl` 由 `bin2onehot` 转独热（每次只点亮一位公共端）；组合进程按当前位挑出 BCD 与小数点，调 `io_7SegmentDisplayEncoding(..., WITH_DOT => TRUE)` 得到 8 位段码。

**段码查表函数**在 `io.pkg` 里：

[src/io/io.pkg.vhdl:L193-L218](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io.pkg.vhdl#L193-L218) —— `io_7SegmentDisplayEncoding` 把一个十六进制半字节映射成 7 位段码，注释里画出段位顺序 `GFEDCBA` 与物理排布。比如 `x"0" -> "0111111"`（点亮 a~f 六段显示 `0`），`x"1" -> "0000111"`（只点 b、c 显示 `1`）。`WITH_DOT=TRUE` 时追加第 8 位小数点。

**`io.pkg` 还提供三态/LVDS 记录等基础设施**：

[src/io/io.pkg.vhdl:L45-L63](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io.pkg.vhdl#L45-L63) —— `T_IO_TRISTATE`（I/O/T 三态记录）、`T_IO_LVDS`（P/N 差分对）及其向量、`T_IO_DATARATE`（SDR/DDR/QDR 枚举）。注释特别警告：**不要用 `T_IO_TRISTATE` 的记录类型给可综合 IP 的 `inout` 端口双向驱动**（见源码中 `:ref:ISSUES:General:inout_records` 的提示），`io_tristate_driver` 过程仅限仿真用。

> 工程直觉：`io_Debounce` 与 `io_7SegmentMux_BCD` 都把「人话时间」当一等公民写进 generic，再用 `TimingToCycles` 编译期折算成位宽与周期数——这是 u2-l4 `physical` 包真正落地的样子。换一块板子、改一个时钟频率，只需改 generic，计数器位宽自动重新推导。

#### 4.3.4 代码实践

**实践目标**：理解 `io_Debounce` 如何把 `BOUNCE_TIME` 折算成计数器，并验证「公共锁存」与「独立锁存」的差异。

**操作步骤**：

1. 打开 [io_Debounce.vhdl:L52-L67](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L52-L67)，列出 generic：`CLOCK_FREQ`(FREQ)、`BOUNCE_TIME`(time)、`BITS`、`INIT`、`ADD_INPUT_SYNCHRONIZERS`、`COMMON_LOCK`。
2. 手算 `LOCK_COUNT_X`：设 `CLOCK_FREQ = 100 MHz`、`BOUNCE_TIME = 10 ms`。

\[
N = \text{TimingToCycles}(10\,\text{ms}, 100\,\text{MHz}) - 1 = \frac{10 \times 10^{-3}}{10 \times 10^{-9}} - 1 = 1\,000\,000 - 1 = 999\,999
\]

   `Lock` 位宽 = `log2ceil(999999 + 1) = 20` 位（20 位有符号足以容纳 `-999999`）。
3. 阅读 [L119-L154](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L119-L154)，对比 `COMMON_LOCK=true`（`LOCKS=1`，所有位共享 `toggle(0)` 与 `locked(0)`）与 `COMMON_LOCK=false`（每比特独立）两个分支。
4. 写一段「示例代码」例化（**仅为说明用法，非项目原有代码**）：

```vhdl
-- 示例代码：4 位按键消抖，独立锁存，含输入同步
deb_inst : entity PoC.io_Debounce
  generic map (
    CLOCK_FREQ              => 100 MHz,
    BOUNCE_TIME             => 10 ms,
    BITS                    => 4,
    ADD_INPUT_SYNCHRONIZERS => true,
    COMMON_LOCK             => false
  )
  port map (
    Clock  => clk,
    Input  => raw_buttons,   -- 4 根异步按键
    Output => clean_buttons  -- 4 位已消抖输出
  );
```

**需要观察的现象**：
- 给某个按键输入一串间隔小于 10 ms 的抖动，`Output` 对应位应保持不变；只有当输入稳定持续 ≥10 ms，`Output` 才翻转。
- `COMMON_LOCK=true` 时，4 个按键中任一个抖动都会锁定**全部**输出；`COMMON_LOCK=false` 时各路独立。

**预期结果**：
- `BOUNCE_TIME` 越大，抗抖越强但响应越迟钝；改 `CLOCK_FREQ`（换板子）会自动重新计算 `Lock` 位宽，源码无需手改。
- 在仿真里给 `raw_buttons` 喂一段「先抖 5 ms、再稳定 12 ms」的激励，`clean_buttons` 应在稳定满 10 ms 后才更新。

> 待本地验证：`TimingToCycles` 的具体取整方向（u2-l4 讲过默认 `ROUND_UP`）会影响 `LOCK_COUNT_X` 的精确值，建议在仿真里量出实际锁定时长核对。

#### 4.3.5 小练习与答案

**练习 1**：`io_Debounce` 为什么要默认 `ADD_INPUT_SYNCHRONIZERS => true`？关掉它的前提是什么？

**参考答案**：按键来自外部，相对 FPGA 时钟是异步信号，直接进寄存器会带来亚稳态（见 u3-l6）。默认开两级 `sync_Bits` 同步是为了可靠性。关掉的前提是：输入信号已经与 `Clock` 同源/同步（例如已经在外部做过同步），此时可省两级触发器与亚稳态约束。

**练习 2**：`io_7SegmentMux_BCD` 的 `REFRESH_RATE` 默认 1 kHz，为什么不用更高（如 1 MHz）或更低（如 10 Hz）？

**参考答案**：太低（10 Hz）会看到数码管闪烁，超过人眼视觉暂留阈值；太高（1 MHz）会让每位点亮时间过短、亮度不足，且无谓抬高动态功耗。1 kHz 对 4 位意味着每位每秒被点亮约 250 次，远高于闪烁阈值、又留足亮度，是经验上的折中。

**练习 3**：`io_7SegmentDisplayEncoding` 返回的 7 位是 `GFEDCBA`、`1` 表示「段亮」。如果你的硬件是共阳数码管（段脚低电平才亮），该在哪一层处理反转？

**参考答案**：反转应放在顶层管脚约束或顶层例化处（例如把 `SegmentControl` 取反后再送管脚，或直接在约束里说明驱动极性），而不是去改 `io.pkg` 的查表常量——查表函数本身保持「1=段亮」的厂商无关语义，才能让同一个 `io_7SegmentMux_BCD` 同时服务共阴和共阳硬件。

---

## 5. 综合实践

把本讲三类核串起来，设计一个「按键计数 + 串口上报」的小外设子系统（**仅为说明架构，非项目原有代码**）：

**任务**：4 位按键经消抖后驱动一个计数器，计数器的值通过 UART 周期性上报到 PC，并用一位数码管显示低 4 位。

**建议结构**：

```vhdl
-- 示例架构（仅示意，非项目源码）
-- 1) 消抖 4 位按键
deb : entity PoC.io_Debounce
  generic map (CLOCK_FREQ => 100 MHz, BOUNCE_TIME => 10 ms, BITS => 4)
  port map (Clock => clk, Input => buttons, Output => btn_clean);

-- 2) UART 时钟：100 MHz / 115200 Bd
bclk : entity PoC.uart_bclk
  generic map (CLOCK_FREQ => 100 MHz, BAUDRATE => 115200 Bd)
  port map (clk => clk, rst => rst, bclk => bit_stb, bclk_x8 => bit_stb_x8);

-- 3) UART 发送器：注意它没有 generic，波特率来自 bclk
tx : entity PoC.uart_tx
  port map (clk => clk, rst => rst, bclk => bit_stb,
            di => count_byte, put => send_strobe, ful => tx_busy, tx => uart_txd);

-- 4) 一位七段显示（演示用，可扩展为 io_7SegmentMux_BCD 多位复用）
```

**需要你思考并回答的串联问题**（用本讲三模块的知识）：

1. `io_Debounce` 的 `BOUNCE_TIME` 是怎么变成 `Lock` 计数器位宽的？（→ 4.3，`TimingToCycles` + `log2ceil`）
2. 为什么 `uart_tx` 例化时不用传波特率 generic？（→ 4.1，波特率由 `uart_bclk` 产生的 `bclk` 决定）
3. 如果这个设计要在 Xilinx 和 Altera 之间移植，`uart_tx`/`io_Debounce` 需要改吗？把 UART 换成 DDR 输出呢？（→ 4.2，纯 RTL 核天然可移植，DDR IO 必须靠 `ddrio_out` 包装层）

> 待本地验证：本实践为源码阅读型架构设计，建议在仿真器里先单独跑通 `uart_bclk → uart_tx` 链路，观察 `tx` 上的起始/数据/停止位，再逐步叠加消抖与显示。

## 6. 本讲小结

- `PoC.io` 是「大命名空间 + 多个子命名空间」结构，`uart`/`ddrio`/`iic`/`vga` 等各是一棵子树，根包 `io.pkg` 提供三态/LVDS 记录、七段编码表等共享类型与函数。
- UART 被**职责拆分**成 `uart_bclk`（波特率/位时钟发生器，含真正的分频）+ `uart_tx`（移位发送，**无 generic、无分频**）+ `uart_rx`（8 倍过采样接收，内嵌可选同步器）。
- `ddrio_out` 是 u3-l2 厂商选择机制的完整范例：通用包装实体 + `VENDOR` generate 分发 + `.files` 编译期选原语库，把 Xilinx `ODDR`/`OBUFT` 与 Altera `altddio_out` 藏到统一接口后，且必须落到 IOB。
- 慢速 IO 核（`io_Debounce`、`io_7SegmentMux_BCD`）把 `physical` 包的 `FREQ`/`time`/`BAUD` 与 `TimingToCycles` 当一等公民，用「人话时间 generic → 编译期位宽推导」的方式写参数化代码。
- `io_Debounce` 直接复用 `sync_Bits`（u3-l6）做输入 CDC，再次印证 PoC 公共包与同步器是全库复用的积木。
- 本讲出现的 `upcounter_next`/`upcounter_equal`（u2-l5）、`log2ceil`/`ite`（u2-l2）、`TimingToCycles`（u2-l4）、`sync_Bits`（u3-l6）都是前几讲讲过的公共件——能在这里一眼认出它们，说明你已经把地基打通。

## 7. 下一步学习建议

- **继续纵向深入协议核**：本讲的 `uart_fifo`（[uart.pkg.vhdl:L111-L150](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart.pkg.vhdl#L111-L150)）把 `uart_tx`/`uart_rx` 与 u3-l4 的 FIFO 组合起来，是练习「FIFO + 流式协议」的好材料；之后可读 `iic`、`vga` 等更复杂的子命名空间。
- **横向进入仿真与综合**：u4 单元会讲测试台写法（u4-l2）与 out-of-context 综合流程（u4-l4）。`ddrio_out_xilinx` 注释里特意提到的「网表化供其他设计例化」正是 u4-l4 的伏笔，建议二读。
- **动手扩展**：u5-l6 会教如何为 PoC 贡献新核。你可以尝试在 `src/io/` 下仿照 `io_Debounce` 写一个带 `physical` generic 的简单 IO 核（如 `io_PulseWidthModulation` 已有的可作参考），把本讲的命名空间包模式与编码规范（u1-l4）用一遍。
- **回顾依赖**：如果对 `TimingToCycles`、`log2ceil`、`sync_Bits`、`components` 函数任一处还有疑虑，回到 u2-l2 / u2-l4 / u2-l5 / u3-l6 对照复习，它们是本讲所有核的公共地基。
