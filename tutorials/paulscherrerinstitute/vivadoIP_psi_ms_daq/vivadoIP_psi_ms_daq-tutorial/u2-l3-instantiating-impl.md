# 例化 psi_ms_daq_axi 与配置转换

## 1. 本讲目标

本讲是 VHDL 封装层精读的最后一讲。前面两讲我们看清楚了 `psi_ms_daq_vivado` 这个外壳的「接口契约」（u2-l1：泛型与端口）和「信号重排层」（u2-l2：16 路标量端口如何映射成 `Str_*` 数组、时间戳与触发如何二选一）。

现在要回答最后一个问题：**这层重排好的信号，最终是怎么交到真正干活的实现实体 `psi_ms_daq_axi` 手里的？**

学完本讲你应当能够：

- 说清楚 16 路逐流标量泛型（如 `Stream0Width_g`…`Stream15Width_g`）是如何被聚合成实现端需要的数组型泛型（如 `StreamWidth_g`）的。
- 解释 `TimeoutUs_c` / `TimeoutsSec_c` / `FreqHz_c` / `FreqReal_c` 这组常量与函数做了什么单位换算，以及为什么必须由外壳来做这一步。
- 理解 `C_S_Axi_ID_WIDTH` 这个由 Vivado Block Design（BD）注入的常量，是如何透传成实现的 `AxiSlaveIdWidth_g` 的。
- 看懂 `bd/bd.tcl` 里 `init` / `pre_propagate` / `propagate` 三个回调为什么要把 AXI `ID_WIDTH` 在 BD 里来回传播。
- 在 `i_impl` 的 `port map` 中，把 `Str_Clk` / `Str_Data` / `Str_Ts` / `Str_Vld` / `Str_Rdy` / `trigger` / `Irq` 与实现端口一一对应起来。

> 前置承接：u2-l2 已经讲过，所有进入 `i_impl` 的流信号都已先汇入 `Str_*`（`Streams_g-1 downto 0`）数组与 `trigger` 信号。本讲从「这些汇好的信号」开始讲起，不再重复重排逻辑。

## 2. 前置知识

在进入源码前，先用三段话把本讲涉及的几个 VHDL 与 Vivado 概念讲清楚。

**数组型泛型（array generic）**。VHDL 的泛型（generic）通常是标量（一个整数、一个布尔）。但 PSI 的实现实体 `psi_ms_daq_axi` 为了支持「最多 16 路、实际 `Streams_g` 路」流，把每路相关的配置做成了**数组型泛型**——例如 `StreamWidth_g` 是一个整数数组，第 `s` 个元素就是第 `s` 路流的数据位宽。这些数组类型 `t_ainteger`（整数数组）、`t_areal`（实数数组）、`t_aslv64`（64 位 std_logic_vector 数组）都来自上游 `psi_common_array_pkg`，外壳文件第 16 行 `use work.psi_common_array_pkg.all;` 引入了它们。而外壳 entity（u2-l1）因为要给 IP-XACT 暴露标量 GUI 控件，只能用 `Stream0Width_g`…`Stream15Width_g` 这种标量。所以**外壳的一项核心职责，就是把 16 个标量「打包」成实现端要的数组**。

**实数（real）与单位换算**。VHDL 里 `integer` 是 32 位有符号整数，不能带小数；`real` 是浮点（类似 double）。实现端 `psi_ms_daq_axi` 为了精确计算「多少个时钟周期等于一个超时阈值」，需要把超时用**秒（带小数）**表示、把流时钟频率用**实数 Hz** 表示。但 IP 的 GUI 参数为了好填、好综合，用的是整数：超时填微秒（整数）、频率填 Hz（整数）。于是外壳必须做两步换算：微秒→秒（除以 \(10^6\)）、整数 Hz→实数 Hz（`real(...)` 类型转换）。

**BD 参数自动传播（BD propagation）**。Vivado Block Design 里，一个 AXI 接口的 `ID_WIDTH`（地址/数据里 ID 字段的位宽）通常不是手填的，而是由连到这个接口的「对端」自动决定的。例如 ZCU102 的 PS AXI 主端口往往 `ID_WIDTH=0`（不带 ID）。Xilinx 的 IP-XACT 机制允许 IP 自带一段 TCL 回调（即 `bd/bd.tcl`），在 BD 里该 IP 被例化、连线、校验时被调用，用来把 `ID_WIDTH` 在「BD 接口引脚」与「cell 上的参数」之间搬来搬去。本讲会读这段 TCL。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们分属两种完全不同的语言和用途：

| 文件 | 语言 | 角色 | 本讲关注点 |
|------|------|------|-----------|
| `hdl/psi_ms_daq_vivado.vhd` | VHDL | 封装外壳 RTL | 架构体里的常量/函数、`i_impl` 例化的 generic map + port map |
| `bd/bd.tcl` | TCL | BD 集成回调 | `init` / `pre_propagate` / `propagate` 三个 proc 对 `ID_WIDTH` 的传播 |

一句话定位：`psi_ms_daq_vivado.vhd` 的 `i_impl` 是「封装→实现」的**唯一交接点**；`bd.tcl` 是「BD→封装」关于 `C_S_Axi_ID_WIDTH` 的**唯一来源**。两者合起来，就解释了实现实体 `psi_ms_daq_axi` 的全部输入是怎么凑齐的。

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：

1. **4.1 单位换算常量与函数**：先把「超时微秒→秒」「频率整数→实数」两步换算讲透，这是 `StreamTimeout_g` / `StreamClkFreq_g` 两个数组泛型的数据来源。
2. **4.2 `i_impl` 例化**：generic map 如何把标量聚合成数组、`C_S_Axi_ID_WIDTH` 如何透传，port map 如何把 `Str_*` / `trigger` / `Irq` / 两套 AXI 对接。
3. **4.3 AXI `ID_WIDTH` 的 BD 传播**：`bd.tcl` 三个 proc 如何让 `C_S_Axi_ID_WIDTH` 自动取得正确值。

### 4.1 单位换算常量与函数

#### 4.1.1 概念说明

实现端 `psi_ms_daq_axi` 期望两个**实数数组**泛型：

- `StreamTimeout_g`：每路流的「缓冲超时」，单位**秒（real）**。含义是——某路流如果长时间没来触发，但 FIFO 里已经攒了数据，超过这个超时就强制把数据冲到 DDR，避免数据无限期滞留。
- `StreamClkFreq_g`：每路流的输入时钟频率，单位**Hz（real）**。实现用它把「秒」换算回「时钟周期数」来数周期。

但 IP 的 GUI（见 u2-l1、u1-l4）给用户填的是**整数**：

- `StreamNTimeoutUs_g`：超时，单位**微秒**，整数（默认 `1e3` = 1000）。
- `StreamNClkFreqHz_g`：时钟频率，单位**Hz**，整数（默认 `100e6` = 100000000）。

之所以 GUI 用整数而实现用实数，是因为：IP-XACT GUI 控件对实数支持差、综合工具也偏好整数泛型；而实现端要做精确的时间-周期换算，必须用浮点。**这个「整数 GUI ↔ 实数实现」的鸿沟，就由外壳里的常量与函数来填。**

#### 4.1.2 核心流程

单位换算分两步，超时与频率各一对（常量收集 + 函数换算）：

```
超时链路:
  [16 个整数标量 StreamNTimeoutUs_g]
        |  TimeoutUs_c (constant, t_ainteger, 收集成 16 元素数组)
        v
  TimeoutUs_c(i)  (整数, 微秒)
        |  TimeoutsSec_c (function, 返回 t_areal, 只取前 Streams_g 个)
        v
  real(TimeoutUs_c(i)) / 1.0e6   (实数, 秒)  => StreamTimeout_g(i)

频率链路:
  [16 个整数标量 StreamNClkFreqHz_g]
        |  FreqHz_c (constant, t_ainteger, 收集成 16 元素数组)
        v
  FreqHz_c(i)  (整数, Hz)
        |  FreqReal_c (function, 返回 t_areal, 只取前 Streams_g 个)
        v
  real(FreqHz_c(i))   (实数, Hz)  => StreamClkFreq_g(i)
```

换算公式用数学写出来：

- 超时（微秒 → 秒）：\[ t_{\text{sec}}(i) = \frac{\text{TimeoutUs\_c}(i)}{10^{6}} \]
- 频率（整数 → 实数）：\[ f_{\text{real}}(i) = \text{real}\bigl(\text{FreqHz\_c}(i)\bigr) \]

注意两处细节：

- `TimeoutUs_c` 与 `FreqHz_c` 这两个 **constant** 收集的是**固定 16 个**元素（`0 to 15`），与 entity 里 16 路标量泛型一一对应。
- `TimeoutsSec_c` 与 `FreqReal_c` 这两个 **function** 返回的却是**前 `Streams_g` 个**元素（`0 to Streams_g-1`）。这正是 u2-l2 讲过的「从固定 16 路裁剪到实际 `Streams_g` 路」在这里的体现——多余的元素不出现在交给实现的数组里。

#### 4.1.3 源码精读

先看超时链路的常量 `TimeoutUs_c`：它把 16 个标量泛型按顺序列成一个数组字面量（aggregate）。

[hdl/psi_ms_daq_vivado.vhd:384-389](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L384-L389) —— 把 16 个 `StreamNTimeoutUs_g` 收集成 16 元素整数数组 `TimeoutUs_c`。

紧接着是函数 `TimeoutsSec_c`，它逐元素除以 \(10^6\)，并且只取前 `Streams_g` 个：

[hdl/psi_ms_daq_vivado.vhd:392-399](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L392-L399) —— `TimeoutsSec_c` 用循环把微秒换算成秒，返回长度为 `Streams_g` 的实数数组。关键一行是：

```vhdl
v(i) := real(TimeoutUs_c(i))/1.0e6;
```

`real(...)` 是 VHDL 的整数→实数类型转换函数（不是类型转换的副作用，是显式标记），`1.0e6` 是实数字面量。

频率链路结构完全对称：先 `FreqHz_c` 常量收集 16 个整数，再 `FreqReal_c` 函数转成 `Streams_g` 个实数。

[hdl/psi_ms_daq_vivado.vhd:401-406](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L401-L406) —— `FreqHz_c` 收集 16 个 `StreamNClkFreqHz_g`。

[hdl/psi_ms_daq_vivado.vhd:408-415](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L408-L415) —— `FreqReal_c` 用 `real(FreqHz_c(i))` 把整数 Hz 转成实数 Hz，返回长度为 `Streams_g` 的数组。注意它**不做任何乘除**，只做类型转换，因为 Hz 在整数和实数下数值相同，只是类型不同。

> 为什么超时要除 \(10^6\) 而频率不除任何数？因为单位不同：超时 GUI 是微秒、实现要秒，差 \(10^6\) 倍；频率 GUI 和实现都是 Hz，只差类型。这正是「单位换算」与「类型转换」的区别。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍默认配置下 `StreamTimeout_g(0)` 与 `StreamClkFreq_g(0)` 的值，确认你理解了换算。

**操作步骤**（源码阅读型，无需运行工具）：

1. 翻到 entity 里 `Stream0TimeoutUs_g` 的声明（u2-l1 已读，默认 `1e3`）。
2. 代入 `TimeoutUs_c(0)` = 1000（微秒，整数）。
3. 代入 `TimeoutsSec_c(0)` 公式：`real(1000)/1.0e6`。
4. 对频率同样代入 `Stream0ClkFreqHz_g` 默认 `100e6`。

**需要观察的现象 / 预期结果**：

- `StreamTimeout_g(0)` = \( 1000 / 10^6 = 0.001 \) 秒 = 1 毫秒。
- `StreamClkFreq_g(0)` = \( \text{real}(100\,000\,000) = 1.0 \times 10^{8} \) Hz = 100 MHz。

如果你算出 `StreamTimeout_g(0) = 1000`（忘了除 \(10^6\)）或 `= 1e9`（除反了），说明单位换算方向搞错了，回到 4.1.2 的公式核对。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Stream0TimeoutUs_g` 改成 `500`（微秒），`StreamTimeout_g(0)` 是多少秒？换算周期数时（频率 100 MHz）相当于多少个时钟周期？

答案：`StreamTimeout_g(0)` = \( 500/10^6 = 5 \times 10^{-4} \) 秒 = 0.5 ms。在 100 MHz（周期 \(10^{-8}\) s）下，相当于 \( 5 \times 10^{-4} / 10^{-8} = 50\,000 \) 个时钟周期。

**练习 2**：`TimeoutsSec_c` 为什么是 `function` 而不是 `constant`？（提示：返回数组长度依赖 `Streams_g`。）

答案：因为它的返回数组长度是 `Streams_g`，而 `Streams_g` 是 generic。VHDL 里「长度依赖 generic 的数组」用 `constant` 字面量很难直接写（aggregate 的元素个数在编译时虽可变，但要写 16 个元素再裁剪很笨）；用 `function` 可以用 `for` 循环只生成 `0 to Streams_g-1` 共 `Streams_g` 个元素，干净地实现「裁剪」。`TimeoutUs_c` 反过来是固定 16 个元素，所以用 `constant` 即可。

### 4.2 `i_impl` 例化：generic map 与 port map

#### 4.2.1 概念说明

`i_impl` 是整个外壳里**唯一**的 component 例化语句，它把真正的实现实体 `work.psi_ms_daq_axi` 拉进来。这条例化要完成三件事：

1. **generic map（泛型映射）**：把外壳的标量泛型「翻译」成实现端要的数组/标量泛型。其中标量泛型（`Streams_g`、`IntDataWidth_g`、各 AXI 参数）直接同名透传；逐流标量泛型（`StreamN*_g`）聚合成数组泛型（`Stream*_g`）；超时与频率经由 4.1 的函数换算后传入；`C_S_Axi_ID_WIDTH` 改名透传成 `AxiSlaveIdWidth_g`。
2. **port map（端口映射）**：把 u2-l2 重排好的 `Str_*` 数组信号、`trigger`、`Irq`，连同两套 AXI（Slave / Master）端口，一对一接到实现实体上。
3. **隐式承担「接口形状适配」**：外壳 entity 的端口形状（固定 16 路、标量、AXI Master 无 ID）与实现端口的形状（`Streams_g` 路、数组、AXI Slave 带 ID）不同，`i_impl` 就是这个形状差的「转接头」。

#### 4.2.2 核心流程

generic map 可以分成四组来看：

```
A. 同名标量透传
   Streams_g, IntDataWidth_g, MaxWindows_g,
   MinBurstSize_g, MaxBurstSize_g,
   AxiDataWidth_g, AxiMaxBurstBeats_g, AxiMaxOpenTrasactions_g, AxiFifoDepth_g
        => 直接 => 实现同名泛型

B. 标量聚合(16个)成数组字面量
   (Stream0Width_g, ..., Stream15Width_g)  => StreamWidth_g
   (Stream0Prio_g,  ..., Stream15Prio_g)   => StreamPrio_g
   (Stream0Buffer_g,..., Stream15Buffer_g) => StreamBuffer_g
   (Stream0TsFifoDepth_g, ...)             => StreamTsFifoDepth_g
   (Stream0UseTs_g, ...)                   => StreamUseTs_g

C. 函数换算结果传入(已裁剪到 Streams_g 个)
   TimeoutsSec_c  => StreamTimeout_g   (微秒->秒, real)
   FreqReal_c      => StreamClkFreq_g   (int Hz -> real Hz)

D. 改名透传
   C_S_Axi_ID_WIDTH => AxiSlaveIdWidth_g
```

port map 则把信号按实现端口的顺序接上，重点是流信号这一组：

```
Str_Clk  => Str_Clk       (各路流时钟)
Str_Data => Str_Data      (各路流数据, 已装入 64 位容器)
Str_Ts   => Str_Ts        (各路流时间戳, 4.1/ u2-l2 决定来源)
Str_Vld  => Str_Vld       (各路流有效)
Str_Rdy  => Str_Rdy       (各路流就绪, 方向回传)
Str_Trig => trigger       (触发, u2-l2 决定取自 TLast 还是外部 Trig)
Irq      => Irq           (合并中断输出)
S_Axi_*  => S_Axi_*       (寄存器访问, AXI Slave)
M_Axi_*  => M_Axi_*       (写内存, AXI Master)
```

注意 `Str_Trig => trigger`：实现端的端口名叫 `Str_Trig`，外壳里的信号名叫 `trigger`（小写），这里就是 u2-l2 那两个 `if generate`（`g_trig` / `g_ntrig`）算出来的结果。

#### 4.2.3 源码精读

例化语句开头：直接引用 `work` 库里的实现实体 `psi_ms_daq_axi`，实例名叫 `i_impl`。

[hdl/psi_ms_daq_vivado.vhd:554-555](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L554-L555) —— `i_impl : entity work.psi_ms_daq_axi`，开始 generic map。

**B 组：标量聚合成数组**。以 `StreamWidth_g` 为例，16 个标量被写成一个 aggregate 字面量：

[hdl/psi_ms_daq_vivado.vhd:557-560](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L557-L560) —— `StreamWidth_g => (Stream0Width_g, ..., Stream15Width_g)`，把 16 路位宽打包成数组。`StreamPrio_g`、`StreamBuffer_g`、`StreamTsFifoDepth_g`、`StreamUseTs_g` 的写法完全一致（只是元素不同），见 [L561-L578](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L561-L578)。

> 这五个聚合字面量都写满了 16 个元素（与 entity 的 16 路标量泛型一一对应），实现端按索引 `0..Streams_g-1` 读取，多余元素在综合时被优化掉。

**C 组：函数换算结果传入**。这是 4.1 的成果，直接把函数调用作为 actual 传给泛型——VHDL 允许函数调用出现在 association list 里：

[hdl/psi_ms_daq_vivado.vhd:569-570](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L569-L570) —— `StreamTimeout_g => TimeoutsSec_c` 与 `StreamClkFreq_g => FreqReal_c`，把换算+裁剪后的实数数组交给实现。注意这两个 actual 是**函数名**（无参函数调用），返回值类型 `t_areal` 正好匹配实现端期望。

**D 组：改名透传 ID 宽度**。这是本讲第二个关键点：

[hdl/psi_ms_daq_vivado.vhd:587](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L587) —— `AxiSlaveIdWidth_g => C_S_Axi_ID_WIDTH`。`C_S_Axi_ID_WIDTH` 是外壳 entity 里那个由 BD 注入的特殊泛型（[L168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L168)），这里改名为实现端的 `AxiSlaveIdWidth_g`。它决定了 `S_Axi_ArId/AwId/RId/BId` 这些端口的位宽（见 entity 里 `std_logic_vector(C_S_Axi_ID_WIDTH-1 downto 0)`），它的值从哪来，就是 4.3 要讲的 `bd.tcl`。

完整的 generic map 范围：

[hdl/psi_ms_daq_vivado.vhd:555-588](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L555-L588) —— `i_impl` 的完整 generic map，涵盖 A/B/C/D 四组映射。

**port map**：先看流信号与中断这一小段（最体现「封装重排成果交接」的部分）：

[hdl/psi_ms_daq_vivado.vhd:590-596](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L590-L596) —— `Str_Clk/Str_Data/Str_Ts/Str_Vld/Str_Rdy/Str_Trig/Irq` 七个端口对接。`Str_Trig => trigger` 是 u2-l2 触发选择的结果出口。

随后的 AXI Slave 与 AXI Master 端口则是大量「同名对接」——把外壳 entity 的 `S_Axi_*` / `M_Axi_*` 端口逐一连到实现实体的同名端口（端口名、方向、宽度都一致，所以是一一对应的直连）：

[hdl/psi_ms_daq_vivado.vhd:597-666](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L597-L666) —— `S_Axi_*` 与 `M_Axi_*` 全部端口的逐一映射。注意 `M_Axi_*` 里**没有**任何 `Id` 端口（外壳 entity 里 AXI Master 也不带 ID），这与 Slave 端「带 ID 且宽度可变」形成对比。

> 一句话：`i_impl` 的 port map 里，**只有 `Str_*`/`trigger`/`Irq` 这一侧是「重排过的」信号，`S_Axi_*`/`M_Axi_*` 两侧都是「直穿」的**。封装的价值恰恰集中在流信号这一侧。

#### 4.2.4 代码实践

**实践目标**：对照 `i_impl` 的 generic map，把 `StreamTimeout_g` 数组第一个元素的计算链手动走一遍，并指出哪些泛型是「同名透传」、哪些是「聚合」、哪些是「换算」。

**操作步骤**（源码阅读型）：

1. 确认默认值 `Stream0TimeoutUs_g = 1e3 = 1000`。
2. 写出链路：`Stream0TimeoutUs_g` → `TimeoutUs_c(0)` → `TimeoutsSec_c(0)` → `StreamTimeout_g(0)`。
3. 代入数值：`1000` → `1000`（微秒，整数）→ `real(1000)/1.0e6` → `0.001`（秒，实数）。
4. 翻一遍 [L555-L588](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L555-L588)，给每个 generic 映射标注「同名透传 / 聚合 / 换算 / 改名」四类之一。

**需要观察的现象 / 预期结果**：

- `StreamTimeout_g(0)` 最终值 = **0.001**（秒）。
- 分类结果应大致为：
  - 同名透传：`Streams_g`、`IntDataWidth_g`、`MaxWindows_g`、`MinBurstSize_g`、`MaxBurstSize_g`、`AxiDataWidth_g`、`AxiMaxBurstBeats_g`、`AxiMaxOpenTrasactions_g`、`AxiFifoDepth_g`。
  - 聚合（16 标量→数组）：`StreamWidth_g`、`StreamPrio_g`、`StreamBuffer_g`、`StreamTsFifoDepth_g`、`StreamUseTs_g`。
  - 换算（函数结果）：`StreamTimeout_g`（来自 `TimeoutsSec_c`）、`StreamClkFreq_g`（来自 `FreqReal_c`）。
  - 改名：`AxiSlaveIdWidth_g <= C_S_Axi_ID_WIDTH`。

如果无法在本地运行 Vivado 综合来验证，标注「待本地验证」（综合后可在综合日志里看到 `i_impl` 例化的 generic 实际取值）。

#### 4.2.5 小练习与答案

**练习 1**：`StreamUseTs_g => (Stream0UseTs_g, ..., Stream15UseTs_g)` 这个聚合里，元素类型是 `boolean`。VHDL 允许把布尔数组作为 generic actual 吗？实现端拿到后通常怎么用？

答案：允许。`t_ainteger` 等是 PSI 自定义数组类型，但布尔也可以有数组类型（实现端会声明对应类型）。实现端拿到布尔数组后，通常在 generate 里按 `if StreamUseTs_g(s) generate` 决定第 `s` 路是否启用时间戳通路——和外壳里 `TsPerStream_g` 的 `if generate` 是同一类用法。

**练习 2**：为什么 `AxiSlaveIdWidth_g` 要改名（从 `C_S_Axi_ID_WIDTH` 改过来），而 `Streams_g` 等不改名？

答案：`C_S_Axi_ID_WIDTH` 这个名字是 **Vivado BD / IP-XACT 的命名约定**——BD 自动传播参数时会按 `C_<BUSIF>_<PARAM>` 规则去找（见 4.3），所以外壳 entity 里**必须**叫这个名字才能被 BD 注入。但实现实体 `psi_ms_daq_axi` 是上游通用代码、不绑定 Vivado 约定，它自己起名 `AxiSlaveIdWidth_g`。于是在 `i_impl` 这个「转接头」处做一次改名，让两边各自保持自己的命名风格。`Streams_g` 等参数不涉及 BD 自动传播，外壳与实现恰好同名，就直接透传。

**练习 3**：port map 里 `Str_Trig => trigger`，如果把这里误写成 `Str_Trig => Trig`（直接接 entity 的外部 `Trig` 端口），在 `UseLastAsTrigger_g = true` 时会发生什么？

答案：当 `UseLastAsTrigger_g = true` 时，u2-l2 的 `g_trig` 分支会让 `trigger <= Str_Lst`（用 TLast 当触发），而 `g_ntrig` 分支不生成、外部 `Trig` 端口也被 package.tcl 隐藏。若强行接 `Trig`，在 `UseLastAsTrigger_g = true` 下 `Trig` 端口不存在/悬空，会综合出错或语义错误。所以必须接中间信号 `trigger`——它已经是两种触发源的「二选一」结果。

### 4.3 AXI `ID_WIDTH` 的 BD 传播：bd.tcl

#### 4.3.1 概念说明

4.2 留了一个问题：`C_S_Axi_ID_WIDTH` 的值到底从哪来？它不是用户在 GUI 里填的（GUI 里没有它），而是 **Vivado Block Design 在连线时自动算出来并注入的**。驱动这段自动逻辑的就是 `bd/bd.tcl`。

背景：在 BD 里，IP 的 AXI Slave 端口 `S00_AXI` 会被连到某个 AXI 主设备（最常见是 Zynq/ZynqMP 的 PS 主端口，或一个 AXI Interconnect）。对端的 `ID_WIDTH` 是已知的（PS 往往是 0）。本 IP 的 Slave 端口位宽必须**匹配**对端，否则 BD 校验报「宽度不一致」。Xilinx 的做法是：让 IP 提供一段 TCL 回调，在 BD 的不同时机把 `ID_WIDTH` 在「接口引脚」与「cell 参数 `C_S00_AXI_ID_WIDTH`」之间搬运，最终让 `C_S00_AXI_ID_WIDTH` 取得正确值，再经 IP-XACT 映射到 VHDL generic `C_S_Axi_ID_WIDTH`，最后到 4.2 的 `AxiSlaveIdWidth_g`。

`bd.tcl` 里正好三个 proc，分别对应 BD 的三个时机：

- `init`：IP 刚被拖进 BD 时调用。**声明** `C_S00_AXI_ID_WIDTH` 是「仅由传播决定」的参数（用户不能手填）。
- `pre_propagate`：BD 传播前调用。把 cell 上已有的 `C_<MASTER>_ID_WIDTH` **下推**到本 IP 的 master 接口引脚上（本 IP 的 master 接口比较特殊，见下）。
- `propagate`：BD 传播时调用。把连到本 IP **slave** 接口的对端 `ID_WIDTH` **上拉**到 cell 参数 `C_S00_AXI_ID_WIDTH`。

> 这是一段接近「Xilinx 官方样板」的 AXI ID_WIDTH 传播代码，几乎每个带 AXI4 Slave 的自定义 IP 都长这样。本讲只解释它在本项目里的作用，不展开 Xilinx 通用机制。

#### 4.3.2 核心流程

三个 proc 的触发顺序与职责：

```
[IP 被拖入 BD]
   |
   v
 init()
   - 找到名为 S00_AXI 的 slave 接口
   - mark_propagate_only  C_S00_AXI_ID_WIDTH
     => 声明这个参数只能由传播产生, 用户不能在 GUI 改
   |
[BD 开始连线 / validate]
   |
   v
 pre_propagate()
   - 遍历所有 AXI4 *master* 接口
   - 若 cell 上存了 C_<busif>_ID_WIDTH 且与接口引脚上的值不同
     => 把 cell 的值写到 master 接口引脚的 ID_WIDTH
   |
   v
 propagate()
   - 遍历所有 AXI4 *slave* 接口 (本项目即 S00_AXI)
   - 读 S00_AXI 接口引脚上的 ID_WIDTH (这个值是 BD 从对端主设备传来的)
   - 写入 cell 参数 C_S00_AXI_ID_WIDTH
   |
   v
 [IP-XACT 把 C_S00_AXI_ID_WIDTH 映射到 VHDL generic C_S_Axi_ID_WIDTH]
   |
   v
 [i_impl: AxiSlaveIdWidth_g => C_S_Axi_ID_WIDTH]
```

关键是 `propagate`：对端主设备的 `ID_WIDTH` 顺着连线到达 `S00_AXI` 接口引脚，`propagate` 把它「抄」进 `C_S00_AXI_ID_WIDTH`。这一步之后，VHDL 里 `S_Axi_ArId` 等端口的位宽 `std_logic_vector(C_S_Axi_ID_WIDTH-1 downto 0)` 就有了正确值——若对端 `ID_WIDTH=0`，这些向量变成空（null range），端口在综合时被自动消除，IP 不再处理 ID 字段，与 PS 的「无 ID」行为匹配。

#### 4.3.3 源码精读

**`init`：声明 `C_S00_AXI_ID_WIDTH` 为传播专属参数**。

[bd/bd.tcl:7-27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L7-L27) —— `init` proc。它遍历所有接口引脚，只挑出 **slave** 模式且名字在 `full_sbusif_list`（即 `S00_AXI`）里的接口，把 `C_S00_AXI_ID_WIDTH` 加入 `busif_param_list`，然后调用 `bd::mark_propagate_only`。这一句的语义是：告诉 BD「这个参数请用传播机制维护，不要让用户在 GUI 编辑」。

**`pre_propagate`：把 cell 参数下推到 master 接口引脚**。

[bd/bd.tcl:30-58](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L30-L58) —— `pre_propagate` proc。它遍历 **master** 模式的 AXI4 接口（条件 `PROTOCOL == AXI4` 且 `MODE == master`），对比接口引脚上的 `ID_WIDTH` 与 cell 上的 `C_<busif>_ID_WIDTH`，若不一致且 cell 值非空，则把 cell 值写到接口引脚。本项目 AXI Master 不带 ID，这段主要沿用样板，保证 master 接口的 `ID_WIDTH` 与 cell 一致。

**`propagate`：把 slave 接口收到的 `ID_WIDTH` 上拉到 cell 参数**（本项目最关键的一段）。

[bd/bd.tcl:61-89](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L61-L89) —— `propagate` proc。它遍历 **slave** 模式的 AXI4 接口，读取接口引脚上的 `ID_WIDTH`（这个值来自连到 `S00_AXI` 的对端主设备），若与 cell 上 `C_S00_AXI_ID_WIDTH` 不一致且接口值非空，就把接口值写到 cell 参数。核心两行：

```tcl
set val_on_cell_intf_pin [get_property CONFIG.${tparam} $busif]   ;# 接口引脚上的 ID_WIDTH
...
set_property CONFIG.${busif_param_name} $val_on_cell_intf_pin $cell_handle  ;# 写进 C_S00_AXI_ID_WIDTH
```

> 注意三个 proc 共用同一个 `axi_standard_param_list = [list ID_WIDTH]`（[L11](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L11)、[L34](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L34)、[L65](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/bd/bd.tcl#L65)），说明本 IP 只传播 `ID_WIDTH` 一个标准 AXI 参数（不含 `DATA_WIDTH`、`PROTOCOL` 等其他可传播量）。

#### 4.3.4 代码实践

**实践目标**：把 `bd.tcl` 三个 proc 与 4.2 的 `AxiSlaveIdWidth_g => C_S_Axi_ID_WIDTH` 串成一条完整的「值从哪来」链路。

**操作步骤**（源码阅读型）：

1. 假设在 ZCU102 BD 里，本 IP 的 `S00_AXI` 连到 ZynqMP PS 的 `M_AXI_HPM0_FPD`（一类 PS 主端口，`ID_WIDTH = 0`，即不带 ID）。
2. 走一遍链路：PS 主端口 `ID_WIDTH=0` → BD 连线 → `S00_AXI` 接口引脚 `ID_WIDTH=0` → `propagate` 写入 `C_S00_AXI_ID_WIDTH=0` → IP-XACT 映射 `C_S_Axi_ID_WIDTH=0` → `i_impl` 里 `AxiSlaveIdWidth_g=0`。
3. 想一想：`AxiSlaveIdWidth_g=0` 后，实现实体里所有 `std_logic_vector(AxiSlaveIdWidth_g-1 downto 0)` 的 AXI ID 字段会变成什么？

**需要观察的现象 / 预期结果**：

- 完整链路：`PS ID_WIDTH(0)` → `S00_AXI 接口` → `propagate` → `C_S00_AXI_ID_WIDTH` → `C_S_Axi_ID_WIDTH` → `AxiSlaveIdWidth_g`。
- 当 `AxiSlaveIdWidth_g=0` 时，`std_logic_vector(-1 downto 0)` 是 **null range（空向量）**，对应的 `S_Axi_ArId/AwId/RId/BId` 端口宽度为 0，综合时这些端口被消除，IP 不再处理 ID。

若你没有 ZCU102 板子或 Vivado 环境无法实操 BD，明确标注「待本地验证」——可在 Vivado 里打开 refdesign 工程的 BD，选中本 IP，查看 `C_S00_AXI_ID_WIDTH` 的实际取值来验证。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `bd.tcl` 里 `init` proc 中的 `bd::mark_propagate_only` 那一句，会有什么后果？

答案：`C_S00_AXI_ID_WIDTH` 不再被标记为「传播专属」，BD 可能允许用户在 GUI 手填它，或不再自动维护它。这会破坏「`ID_WIDTH` 由对端主设备决定」的约定——用户可能填一个与对端不一致的值，导致 BD 校验报宽度错误，或综合后 ID 信号位宽错配。`mark_propagate_only` 是把参数「锁死为自动」的关键。

**练习 2**：`propagate` proc 里有一句 `if { $val_on_cell_intf_pin != "" }` 的判空保护。为什么要判空？

答案：在 IP 刚放入 BD、尚未连线时，`S00_AXI` 接口引脚上的 `ID_WIDTH` 可能还没有值（空串）。若不判空就 `set_property`，会把 `C_S00_AXI_ID_WIDTH` 写成空，破坏已有值或报错。判空确保只有当对端确实传来一个有效的 `ID_WIDTH` 时才更新 cell 参数。

**练习 3**：本 IP 的 AXI **Master** 端口（`M_Axi_*`）在 entity 里**没有** `Id` 信号。那 `pre_propagate` 里处理 master 接口的 `ID_WIDTH` 还有意义吗？

答案：对**本 IP 的 master 输出**而言意义有限（因为 master 不带 ID，`ID_WIDTH` 实际为 0），但这段代码是 Xilinx AXI4 IP 的通用样板，对「master 接口的 `ID_WIDTH` 传播」做了对称处理，保证如果将来 IP 的 master 带 ID 也能正确工作。它不会造成错误，只是对本项目是「无害的样板」。这也提醒读者：读样板代码时要区分「为本项目服务的逻辑」与「沿袭样板的通用逻辑」。

## 5. 综合实践

把本讲三个模块串起来，完成一个「**给实现实体 `psi_ms_daq_axi` 画一张完整的输入来源图**」的任务。

**任务**：在一张纸上（或文本里）画一张表，左列是实现实体 `psi_ms_daq_axi` 的每个 generic 与关键端口，右列填它的值/信号「来自外壳的哪里、经过了什么处理」。要求覆盖以下条目：

| 实现端 generic/port | 来源（外壳侧） | 经过处理 |
|---|---|---|
| `Streams_g` | `Streams_g` | 同名透传 |
| `StreamWidth_g` | `Stream0Width_g..Stream15Width_g` | 16 标量聚合 |
| `StreamTimeout_g` | `Stream0TimeoutUs_g..Stream15TimeoutUs_g` | TimeoutUs_c → TimeoutsSec_c（微秒→秒，裁剪到 Streams_g） |
| `StreamClkFreq_g` | `Stream0ClkFreqHz_g..Stream15ClkFreqHz_g` | FreqHz_c → FreqReal_c（int→real，裁剪到 Streams_g） |
| `AxiSlaveIdWidth_g` | BD 对端主设备的 `ID_WIDTH` | bd.tcl propagate → `C_S00_AXI_ID_WIDTH` → IP-XACT → `C_S_Axi_ID_WIDTH` → 改名 |
| `Str_Trig`（port） | `Str_Lst` 或外部 `Trig` | u2-l2 的 `g_trig`/`g_ntrig` 二选一 → `trigger` |
| `Str_Ts`（port） | `All_Ts` 或 `StrX_Ts` | u2-l2 的 `g_tsstr`/`g_ntsstr` 二选一 |

**完成后自检**：

1. 默认配置下（`Stream0TimeoutUs_g=1e3`、`Stream0ClkFreqHz_g=100e6`），你表里 `StreamTimeout_g(0)` 应填 `0.001`、`StreamClkFreq_g(0)` 应填 `1.0e8`。
2. 你应当能用一句话说清：**外壳 `psi_ms_daq_vivado` 的全部存在意义，就是把「Vivado BD 友好的 16 路标量接口」翻译成「实现实体 `psi_ms_daq_axi` 要的数组+实数+正确 ID 宽度接口」**——本讲的 `i_impl` 例化与 `bd.tcl` 传播，就是这个翻译的两条主干。

如果你能不看源码把这张表默写出来，说明本讲三个最小模块都已掌握。

## 6. 本讲小结

- `i_impl : entity work.psi_ms_daq_axi` 是封装外壳与真正实现之间的**唯一交接点**，外壳的全部工作都是为了喂饱这一条例化的 generic map 与 port map。
- 单位换算由两对「常量收集 + 函数换算」完成：`TimeoutUs_c`/`TimeoutsSec_c` 把微秒整数（GUI）转成秒实数（实现），`FreqHz_c`/`FreqReal_c` 把整数 Hz 转成实数 Hz；函数版同时把固定 16 路裁剪到实际 `Streams_g` 路。
- 逐流标量泛型通过 aggregate 字面量聚合成数组泛型（`StreamWidth_g`/`StreamPrio_g`/`StreamBuffer_g`/`StreamTsFifoDepth_g`/`StreamUseTs_g`）；超时与频率则把换算函数的结果直接作为 actual 传入。
- `C_S_Axi_ID_WIDTH` 改名透传为 `AxiSlaveIdWidth_g`，决定了 AXI Slave ID 端口的位宽，是外壳里唯一「不由用户填、由 BD 注入」的泛型。
- `bd/bd.tcl` 的 `init`/`pre_propagate`/`propagate` 三个回调负责把对端主设备的 `ID_WIDTH` 经 `S00_AXI` 接口传播到 cell 参数 `C_S00_AXI_ID_WIDTH`，再经 IP-XACT 映射到 VHDL 泛型。
- port map 中，`Str_*`/`trigger`/`Irq` 是 u2-l2 重排成果的交接，`S_Axi_*`/`M_Axi_*` 则是同名直穿——封装的价值集中在流信号一侧。

## 7. 下一步学习建议

到这里，**单元 2（VHDL 封装层）全部讲完**：u2-l1 看了 entity 接口契约，u2-l2 看了信号重排层，u2-l3（本讲）看了 `i_impl` 例化与 `bd.tcl` 传播。你现在应当能完整解释「从 BD 拖进 IP、配置 GUI、到实现实体 `psi_ms_daq_axi` 拿到全部正确输入」的整条硬件链路。

接下来建议进入**单元 3（C 驱动架构与寄存器映射）**，从软件侧重新切入：

- **u3-l1 寄存器映射全景**：从 `drivers/psi_ms_daq_axi/src/psi_ms_daq.h` 读四类地址空间（通用/逐流录制/逐流上下文/窗口），你会看到本讲 AXI Slave（`S_Axi_*`，32 位数据/16 位地址）背后的寄存器布局究竟是什么样。
- **u3-l3 初始化与寄存器访问抽象**：看 C 驱动如何通过 AXI Slave 访问这些寄存器，与本讲的 `C_S_Axi_ID_WIDTH`/`S_Axi_*` 端口形成「硬件端口 ↔ 软件寄存器读写」的闭环。

如果暂时留在硬件侧，可以改去 **u5-l1 参考设计：Vivado 工程与时钟域**，看本讲的 IP 在 ZCU102 BD 里实际怎么连、`S00_AXI` 连到哪个 PS 主端口（直接印证 4.3 的 `ID_WIDTH` 传播场景），以及各路流时钟 `StrNN_Clk` 的跨时钟域约束。
