# 顶层 IP 核 psi_ms_daq_axi：生成参数与端口

## 1. 本讲目标

本讲带读者打开 `psi_multi_stream_daq` 的「正门」——顶层实体 `psi_ms_daq_axi`。学完后你应当能够：

- 说清楚顶层 `generic`（生成参数）每一项的含义、取值范围与默认值，知道哪些是「每流一个」的数组型参数。
- 在端口列表里分辨出三组接口：多路**数据流输入**、**AXI Slave**（寄存器访问）、**AXI Master**（向 DDR 写数据），以及一根中断输出 `Irq`。
- 在 `architecture rtl` 的例化代码里，认出 5 个子模块，并在脑子里画出「流输入 → 输入逻辑 → 控制状态机/DMA → AXI Master → 内存」的顶层数据流。

本讲只看顶层这一层，不展开子模块内部实现——那是 u2/u3 单元的事。我们的目标是先建立「整块积木长什么样、有哪些插脚」的全局印象。

## 2. 前置知识

在阅读本讲前，建议你已经读过 [u1-l1](u1-l1-project-overview.md) 与 [u1-l2](u1-l2-repo-and-simulation.md)。本讲会用到以下概念，初学者若不熟可以先记一句话定义：

- **VHDL**：用来描述数字硬件的语言。一个 VHDL 设计由 **entity**（对外接口，相当于「插座」）和 **architecture**（内部实现，相当于「电路」）两部分组成。
- **generic（生成参数）**：在综合（把代码变成电路）之前就要定下来的常量，例如「这个 IP 核一共有几路流」。一旦综合完成就不能再改。
- **port（端口）**：IP 核对外的信号，分 `in`（输入）、`out`（输出）、`inout`（双向）。
- **AXI**：ARM 提出的一种片上总线协议。本 IP 核用到两套 AXI 接口：
  - **AXI Slave**：IP 核作为「从机」，被 CPU 读写——CPU 通过它配置寄存器。
  - **AXI Master**：IP 核作为「主机」，主动发起读写——它通过这组接口把采集到的数据写入 DDR 内存。
- **DMA（Direct Memory Access）**：不经过 CPU、由硬件直接把数据搬进内存的机制。本 IP 核本质就是一个「多流 DMA 引擎」。
- **DDR**：系统主内存（如 Zynq SoC 的 PS 侧 DDR）。
- **每流一个的数组型 generic**：`psi_common` 库提供了 `t_ainteger`（整数数组）、`t_areal`（实数数组）、`t_abool`（布尔数组）、`t_aslv64`（64 位 `std_logic_vector` 数组）等类型。当 `Streams_g = 2` 时，`StreamWidth_g := (16, 16)` 表示「第 0 路 16 位、第 1 路 16 位」。这样就能给每一路单独配参数。

> 小贴士：本讲里出现的 `t_ainteger`、`t_areal`、`t_abool`、`t_aslv64`、`log2ceil` 都来自 `psi_common` 库（`psi_common_array_pkg`、`psi_common_math_pkg`），不是本项目的代码。这一点在 [hdl/psi_ms_daq_axi.vhd:10-17](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L10-L17) 的 `library` / `use` 语句里可以看到。

## 3. 本讲源码地图

本讲只涉及两个 VHDL 文件，主角是第一个：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `hdl/psi_ms_daq_axi.vhd` | 顶层实体 + 架构。把 5 个子模块拼起来、对外暴露 generic 与端口。 | 全篇精读，是本讲的全部依据。 |
| `hdl/psi_ms_daq_pkg.vhd` | 项目公共类型包。定义了顶层用到的 record 类型（如 `Input2Daq_Data_t`、`DaqSm2DaqDma_Cmd_t`）和常量。 | 仅在解释子模块间连线类型时引用，详细讲解留给 [u2-l1](u2-l1-common-package.md)。 |

顶层的 5 个子模块（架构体内例化）对应 [hdl/](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl) 下的另外 5 个 `.vhd` 文件，本讲只在「数据流地图」里点名，不展开：

| 子模块例化 | 对应源文件 | 一句话作用 |
| --- | --- | --- |
| `i_reg` | `psi_ms_daq_reg_axi.vhd` | AXI Slave 寄存器接口 + 上下文存储。 |
| `g_input`（每流一个） | `psi_ms_daq_input.vhd` | 流数据的跨时钟域、缓冲、触发与拼字。 |
| `i_statemachine` | `psi_ms_daq_daq_sm.vhd` | 整个 IP 的「大脑」：仲裁、上下文、窗口切换。 |
| `i_dma` | `psi_ms_daq_daq_dma.vhd` | 把流数据拼成内部宽字、发内存命令。 |
| `i_memif` | `psi_ms_daq_axi_if.vhd` | 把内存命令封装成 AXI Master 写突发。 |

## 4. 核心概念与源码讲解

### 4.1 顶层实体与生成参数（generic）

#### 4.1.1 概念说明

`generic` 是 IP 核的「产品规格」：在综合之前你必须告诉工具，这个核要做成几路流、每路多宽、挂在多大位宽的 AXI 上……这些值在电路运行时是**固定死的常量**，不能像寄存器那样动态改。

`psi_ms_daq_axi` 的 generic 分成四组，恰好对应 IP 核的四个关注点：

1. **Streams（流）组**：有几路流、每路多宽、优先级多少、缓冲多深、超时多长、流时钟多快、要不要时间戳——**全是「每流一个」的数组型 generic**。
2. **Recording（记录）组**：内部数据宽度、每流最多几个窗口、DMA 突发大小。
3. **Axi（AXI Master）组**：AXI 数据宽度、最大突发拍数、最大在途事务数、FIFO 深度。
4. **Axi Slave 组**：AXI Slave 的 ID 宽度（很多 SoC 不需要 ID，默认 0）。

#### 4.1.2 核心流程

顶层 generic 的设计流程可以概括为三步：

1. **用户在工程顶层写 generic 映射**，例如把 `Streams_g => 2`、`StreamWidth_g => (16,16)` 传进来。
2. **工具把数组型 generic 截断到实际流数**：因为 `StreamWidth_g` 的默认值只有 2 个元素，如果 `Streams_g = 4`，用户必须自己提供 4 个元素。架构体内部又用 `StreamWidth_c` 等局部常量把它「规范」成 `0 to Streams_g-1` 的数组（见 [hdl/psi_ms_daq_axi.vhd:137-143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L137-L143)）。
3. **每个 generic 顺流而下**：顶层把对应的 generic 再传给子模块。例如 `IntDataWidth_g` 会被透传给 `i_input`、`i_dma`、`i_memif` 三个子模块（这是近期 feature/se32 的改动，见 [u4-l4](u4-l4-axi-cache-intdatawidth.md)）。

#### 4.1.3 源码精读

generic 全部声明在 [hdl/psi_ms_daq_axi.vhd:23-45](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L23-L45)，下面按四组拆开看。

**Streams 组**（[hdl/psi_ms_daq_axi.vhd:24-32](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L24-L32)）——这几项都是数组型，每路流一个值：

```vhdl
Streams_g               : positive range 1 to 32   := 2;       -- 流的数量，1~32
StreamWidth_g           : t_ainteger               := (16, 16);-- 每路数据位宽，如 8/16/32/64
StreamPrio_g            : t_ainteger               := (1, 1);  -- 每路优先级 1/2/3
StreamBuffer_g          : t_ainteger               := (1024, 1024); -- 每路输入 FIFO 深度
StreamTimeout_g         : t_areal                  := (1.0e-3, 1.0e-3); -- 超时(秒)
StreamClkFreq_g         : t_areal                  := (100.0e6, 100.0e6); -- 流时钟频率(Hz)
StreamTsFifoDepth_g     : t_ainteger               := (16, 16);-- 时间戳 FIFO 深度
StreamUseTs_g           : t_abool                  := (true, true); -- 该路是否使用时间戳
```

> 注意 `StreamClkFreq_g` 与 `StreamTimeout_g`：后者是「秒」（实数），前者是「Hz」（实数）。硬件内部要把超时换算成时钟周期数，所以必须同时知道流时钟频率。换算关系是 \[ N_{\text{timeout}} = f_{\text{clk}} \cdot t_{\text{timeout}} \] 例如 `100.0e6 × 1.0e-3 = 100000` 拍。

**Recording 组**（[hdl/psi_ms_daq_axi.vhd:33-37](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L33-L37)）：

```vhdl
IntDataWidth_g          : positive                 := 64;      -- IP 内部数据宽度(位)
MaxWindows_g            : positive range 1 to 32   := 16;      -- 每流最大窗口数
MinBurstSize_g          : integer range 1 to 512   := 512;     -- 最小突发字节数
MaxBurstSize_g          : integer range 1 to 512   := 512;     -- 最大突发字节数
```

`IntDataWidth_g` 以前是写死的 64，现在被提取成 generic（feature/se32，commit `16f13e6`），让输入逻辑、DMA、AXI 接口三处统一使用可配置的内部宽度。每个流的采样在进入 IP 后会被「打包」成 `IntDataWidth_g` 位的宽字再搬给 DMA，打包比为

\[
\text{pack} = \left\lceil \frac{\text{IntDataWidth\_g}}{\text{StreamWidth\_g}(\text{str})} \right\rceil
\]

**Axi 组**（[hdl/psi_ms_daq_axi.vhd:38-42](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L38-L42)）：

```vhdl
AxiDataWidth_g          : natural range 64 to 1024 := 64;      -- AXI Master 数据位宽
AxiMaxBurstBeats_g      : integer range 1 to 256   := 256;     -- 单次突发最大拍数
AxiMaxOpenTrasactions_g : natural range 1 to 8     := 8;        -- 最大在途事务数
AxiFifoDepth_g          : natural                  := 1024;     -- AXI 侧 FIFO 深度
```

**Axi Slave 组**（[hdl/psi_ms_daq_axi.vhd:43-44](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L43-L44)）：

```vhdl
AxiSlaveIdWidth_g       : integer                  := 0         -- AXI Slave ID 宽度，默认无 ID
```

`AxiSlaveIdWidth_g` 影响 AXI Slave 的 `S_Axi_ArId`/`S_Axi_AwId` 等端口的位宽——默认为 0 时这些端口退化为空向量（见下文 4.3）。

#### 4.1.4 代码实践

实践目标：**在源码里数清楚 generic 分了几组、哪些是数组型**，为后面自己配参数打基础。

操作步骤：

1. 打开 [hdl/psi_ms_daq_axi.vhd:23-45](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L23-L45)。
2. 画一张表，把每个 generic 按「组别 / 是否数组型 / 默认值 / 取值范围」四列填出来。
3. 在架构体里找到 [hdl/psi_ms_daq_axi.vhd:137-143](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L137-L143)，观察 `StreamWidth_c`、`StreamPrio_c` 等常量如何把 generic 重新声明成 `0 to Streams_g-1` 的定长数组——这是后续按 `str` 索引取「第 str 路的宽度」的前提。

需要观察的现象：数组型 generic 的默认值元素个数与 `Streams_g` 默认值 2 一致；非数组型 generic（如 `MaxWindows_g`、`AxiDataWidth_g`）是「全流共享」的单值。

预期结果：得到一张 15 行的表，其中 8 行（Streams 组除 `Streams_g` 本身外）是数组型。

#### 4.1.5 小练习与答案

**练习 1**：如果要把 IP 配成 4 路、每路宽度分别是 8/16/32/64 位，`StreamWidth_g` 应该写成什么？

> **答案**：`StreamWidth_g => (8, 16, 32, 64)`。注意 `Streams_g` 必须同时设为 4，否则数组长度对不上。

**练习 2**：`MinBurstSize_g` 和 `MaxBurstSize_g` 都默认 512，含义有什么不同？（提示：前者影响「数据是否够发一次 DMA」，后者影响「单次 DMA 上限」。）

> **答案**：`MinBurstSize_g` 是控制状态机判断「某路数据是否值得现在搬」的门槛（数据太少就先攒着）；`MaxBurstSize_g` 是单次 DMA 突发的字节数上限（受 AXI 4KB 边界和窗口剩余共同约束，详见 [u3-l2](u3-l2-sm-context-calcaccess.md)）。

### 4.2 数据流输入端口

#### 4.2.1 概念说明

数据流输入端口是 IP 核的「采集探头」。每个流由一组信号描述（典型的 valid/ready 握手），所有信号都被声明成长度为 `Streams_g` 的**数组**，下标 `0 ~ Streams_g-1` 对应每一路流。

一个容易踩坑的细节：顶层对外把 `Str_Data`/`Str_Ts` 都声明成 `t_aslv64`（64 位的 `std_logic_vector` 数组），但这**不代表每路必须是 64 位**。真正的位宽由 `StreamWidth_g(str)` 决定，顶层在例化输入逻辑时只截取低 `StreamWidth_c(str)` 位使用。

#### 4.2.2 核心流程

单路流的握手时序（一个简化模型）：

```
Str_Clk(str)  __|‾|_|‾|_|‾|_|‾|_|‾|_   ...   （每路可以有自己的时钟！）
Str_Vld(str)  ___|‾‾‾‾|____|‾‾‾‾|___          数据有效
Str_Data(str) ___<D0 ><xxxx><D1 >xxxx         有效时携带采样
Str_Rdy(str)  ‾‾‾‾‾‾‾‾‾|‾‾‾‾‾‾‾‾‾|‾‾          IP 准备好接收
                 ↑ Vld&Rdy 同时高 → 这一拍 D0 被采走
```

- `Str_Trig` 是触发脉冲（在触发/单次模式下用来「锁定一帧」）。
- `Str_Ts` 是该采样对应的 64 位时间戳（仅当 `StreamUseTs_g(str) = true` 时有意义）。
- `Str_Clk` 允许**每路一个独立时钟**，跨时钟域由输入逻辑内部的异步 FIFO 处理（详见 [u2-l2](u2-l2-input-interface-clocks.md)）。

#### 4.2.3 源码精读

数据流输入端口声明在 [hdl/psi_ms_daq_axi.vhd:47-53](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L47-L53)：

```vhdl
-- Data Stream Input
Str_Clk       : in  std_logic_vector(Streams_g - 1 downto 0);  -- 每路一个时钟
Str_Data      : in  t_aslv64(Streams_g - 1 downto 0);           -- 每路采样(最多64位)
Str_Ts        : in  t_aslv64(Streams_g - 1 downto 0);           -- 每路时间戳(64位)
Str_Vld       : in  std_logic_vector(Streams_g - 1 downto 0);   -- 每路有效
Str_Rdy       : out std_logic_vector(Streams_g - 1 downto 0);   -- 每路就绪(输出)
Str_Trig      : in  std_logic_vector(Streams_g - 1 downto 0);   -- 每路触发
```

「截取低位」的技巧在 generate 块里能看到（[hdl/psi_ms_daq_axi.vhd:303-309](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L303-L309)）：

```vhdl
g_input : for str in 0 to Streams_g - 1 generate
  signal StrInput : std_logic_vector(StreamWidth_c(str) - 1 downto 0);
begin
  StrInput <= Str_Data(str)(StrInput'range);   -- 只取该路需要的位数
  ...
```

这段代码做两件事：① 用 `StrInput'range` 把 `Str_Data(str)` 截成 `StreamWidth_c(str)` 位；② 把截好的信号喂给 `psi_ms_daq_input` 的 `Str_Data` 端口。

#### 4.2.4 代码实践

实践目标：**亲手追一条 `Str_Data` 是怎么从顶层端口流到输入逻辑的**。

操作步骤：

1. 从 [hdl/psi_ms_daq_axi.vhd:49](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L49) 的顶层端口 `Str_Data` 开始。
2. 跳到 [hdl/psi_ms_daq_axi.vhd:309](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L309) 看截位赋值 `StrInput <= Str_Data(str)(StrInput'range);`。
3. 再到 [hdl/psi_ms_daq_axi.vhd:326](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L326) 看 `StrInput` 被接到 `i_input` 的 `Str_Data => StrInput` 端口。

需要观察的现象：尽管顶层端口是 64 位，子模块 `psi_ms_daq_input` 实际拿到的 `Str_Data` 是被裁剪过的 `StreamWidth_c(str)` 位。

预期结果：理解「为什么外部端口统一用 `t_aslv64`」——为了让顶层端口宽度与 `Streams_g` 无关地固定，真正位宽由 generic + 截位决定。这避免了端口数组元素位宽随流变化带来的复杂声明。

> 待本地验证：如果你在仿真器里把 `Streams_g` 改成 3、`StreamWidth_g` 改成 `(8, 16, 32)`，可以用波形窗口确认 `i_input(1)` 的 `Str_Data` 正好是 16 位。

#### 4.2.5 小练习与答案

**练习 1**：`Str_Rdy` 是 `in` 还是 `out`？为什么方向和 `Str_Vld` 相反？

> **答案**：`Str_Rdy` 是 `out`（IP 核向外部数据源声明自己「能接收」）。valid/ready 握手中，发送方给 valid+data，接收方回 ready，二者同时为高时数据才真正传输，所以方向必然相反。

**练习 2**：如果某路 `StreamUseTs_g(str) = false`，外部还需要给 `Str_Ts(str)` 喂有效值吗？

> **答案**：不需要。输入逻辑在该路不会锁存/使用时间戳，`Str_Ts(str)` 可以接 `(others => '0')`。

### 4.3 AXI Slave 与 AXI Master 端口

#### 4.3.1 概念说明

IP 核对外有两套 AXI 接口，**职责完全不同**，必须分清：

- **AXI Slave（`S_Axi_*`）**：CPU 是主机、IP 核是从机。CPU 通过它读写 IP 内部寄存器（使能流、配置触发后采样数、读状态、清中断等）。地址空间只有 16 位（64KB），因为它只是「控制面」。
- **AXI Master（`M_Axi_*`）**：IP 核是主机、DDR 是从机。IP 核通过它把采集数据**写**进系统内存。地址 32 位（4GB），因为它是「数据面」。

> 注意：虽然 `M_Axi_*` 同时声明了写通道（AW/W/B）和读通道（AR/R），但本 IP 核实际**只写不读**——`axi_if` 子模块把读通道相关输出留空、输入给默认值 `'0'`（见 [hdl/psi_ms_daq_axi.vhd:451-452](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L451-L452)、[459](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L459)、[468-469](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L468-L469)）。读通道存在只是因为复用了通用的 `psi_common_axi_master_full`。

AXI 协议把一次传输拆成 5 个独立通道（读：AR/R；写：AW/W/B），每个通道都是独立的 valid/ready 握手。本讲不展开 AXI 协议细节，只要求你能认出这些端口属于哪个通道。

#### 4.3.2 核心流程

**写一次寄存器（AXI Slave）**：

```
CPU ──AwAddr/AwValid──> [IP Slave] ──AwReady──> CPU     (写地址)
CPU ──WData/WValid────> [IP Slave] ──WReady────> CPU     (写数据)
[IP Slave] ──BResp/BValid──> CPU <──BReady──── [CPU]     (写响应)
```

**IP 写一批数据到 DDR（AXI Master）**：

```
[IP Master] ──M_Axi_AwAddr/AwValid──> DDR ──AwReady──> IP   (写地址)
[IP Master] ──M_Axi_WData/WValid────> DDR ──WReady────> IP   (写数据,可能多拍)
DDR ──BResp/BValid──> [IP Master] <──BReady──── [IP]        (写响应)
```

两条 AXI 总线**各自有独立时钟**（`S_Axi_Aclk` 与 `M_Axi_Aclk`），通常在 SoC 里分别接到 PS 的不同时钟域。

#### 4.3.3 源码精读

AXI Slave 端口在 [hdl/psi_ms_daq_axi.vhd:56-93](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L56-L93)，开头几行：

```vhdl
-- AXI Slave Interface for Register Access
S_Axi_Aclk    : in  std_logic;
S_Axi_Aresetn : in  std_logic;
S_Axi_ArId    : in  std_logic_vector(AxiSlaveIdWidth_g - 1 downto 0);  -- ID(可空)
S_Axi_ArAddr  : in  std_logic_vector(15 downto 0);   -- 16位地址=64KB寄存器空间
...
S_Axi_RData   : out std_logic_vector(31 downto 0);   -- 寄存器数据32位
```

几个关键点：

- `S_Axi_ArAddr/AwAddr` 都是 16 位 → 寄存器空间 64KB。
- `S_Axi_RData/WData` 都是 32 位 → 寄存器按 32 位组织。
- `S_Axi_*Id` 的位宽由 `AxiSlaveIdWidth_g` 决定；默认 0 时该向量是空范围 `(0 downto 0)` 之外的空（实际综合为空向量），相当于「不带 ID」。

AXI Master 端口在 [hdl/psi_ms_daq_axi.vhd:94-127](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L94-L127)，开头几行：

```vhdl
-- AXI Master Interface for Memory Access
M_Axi_Aclk    : in  std_logic;
M_Axi_Aresetn : in  std_logic;
M_Axi_AwAddr  : out std_logic_vector(31 downto 0);                  -- 32位地址=4GB
...
M_Axi_WData   : out std_logic_vector(AxiDataWidth_g - 1 downto 0);  -- 数据位宽可配
M_Axi_WStrb   : out std_logic_vector(AxiDataWidth_g / 8 - 1 downto 0);-- 字节使能
...
```

注意 `M_Axi_WData`/`M_Axi_RData` 的位宽随 `AxiDataWidth_g` 变化；`M_Axi_WStrb` 是 `AxiDataWidth_g/8` 位（每字节一个使能）。

读通道输入端口带了默认值（[hdl/psi_ms_daq_axi.vhd:105](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L105)、[110-112](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L110-L112)、[122-126](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L122-L126)）：

```vhdl
M_Axi_AwReady : in  std_logic := '0';   -- 不接时默认'0'，避免悬空
...
M_Axi_RData   : in  ... := (others => '0');
M_Axi_RValid  : in  std_logic := '0';
```

这些默认值是 VHDL 的好习惯：当上层例化时若不连某些只读通道的输入，端口不会悬空成 `'U'`。

> 进阶提示：`M_Axi_AwCache/AwProt/ArCache/ArProt` 这四个输出**没有**由 `axi_if` 子模块驱动（它在端口映射里写了 `=> open`，见 [4.4.3](#443-源码精读)），而是由顶层一个专门的 `sync_apc_reg` 进程驱动。这是 ACPCFG 寄存器的功能，详见 [u4-l4](u4-l4-axi-cache-intdatawidth.md)。

#### 4.3.4 代码实践

实践目标：**把两套 AXI 接口的端口「分通道」归类**，理解哪些端口真正被使用。

操作步骤：

1. 打开 [hdl/psi_ms_daq_axi.vhd:56-127](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L56-L127)。
2. 建一张表，列出 AXI Slave 的 AR/R/AW/W/B 五个通道各有哪些端口。
3. 对 AXI Master 做同样的事，并标出哪些端口在 [445-477 行](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L435-L477) 的例化里被接到 `open`（说明 IP 核不驱动它）。
4. 找到所有带 `:= '0'` 或 `:= (others => '0')` 默认值的 `M_Axi_*` 输入端口，统计它们都属于哪个通道。

需要观察的现象：AXI Master 的**读通道（AR/R）几乎全带默认值**，且 `M_Axi_ArCache/ArProt/AwCache/AwProt` 在 `i_memif` 例化时是 `open`。

预期结果：得出结论——本 IP 的 AXI Master 实质上是一个「只写」master，读通道只是协议复用留下的「空壳」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `S_Axi_RData` 是 32 位，而 `M_Axi_WData` 是 `AxiDataWidth_g` 位？

> **答案**：AXI Slave 用于访问 32 位寄存器，所以数据固定 32 位；AXI Master 用于写 DDR，DDR 位宽可配（64/128/256/…），所以数据位宽跟随 `AxiDataWidth_g`。

**练习 2**：`S_Axi_Aclk` 和 `M_Axi_Aclk` 可以是同一个时钟吗？可以是不同时钟吗？

> **答案**：两种都可以。本 IP 内部本来就支持多时钟域（流时钟、寄存器时钟、内存时钟三套，见 [u2-l2](u2-l2-input-interface-clocks.md)）；这两个 AXI 时钟只是其中两个。SoC 里常见做法是把它们接到不同时钟，以解耦控制面和数据面。

### 4.4 架构体内的 5 个子模块例化（顶层数据流）

#### 4.4.1 概念说明

`architecture rtl of psi_ms_daq_axi`（[hdl/psi_ms_daq_axi.vhd:134](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L134)）是顶层架构。它本身几乎不含逻辑，主要工作是「布线」：声明一组中间信号，再把 5 个子模块像搭积木一样拼起来，让数据和控制信号在它们之间流动。

5 个例化分别是：

1. `i_reg`：寄存器接口（AXI Slave ↔ 配置/状态/上下文）。
2. `g_input`：**generate 循环**，每个流例化一个 `psi_ms_daq_input`。
3. `i_statemachine`：控制状态机（大脑）。
4. `i_dma`：DMA 引擎。
5. `i_memif`：AXI Master 接口。

另外还有一个不算子模块但很重要的进程 `sync_apc_reg`（[hdl/psi_ms_daq_axi.vhd:212-222](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L212-L222)），它把 AXI Cache/Prot 这些准静态向量打两拍寄存器后再输出，用来满足 AXI 对 master 输出的时序要求。

#### 4.4.2 核心流程

顶层的数据流可以画成两条主线（控制面 + 数据面）：

**数据面（流 → 内存）**：

```
Str_Data/Vld/Trig/Ts ──► g_input (每流)
                          │ InpDma_Data/Vld/Rdy
                          ▼
                        i_dma ──(拼宽字)──► DmaMem_CmdAddr/Size + DmaMem_DatData
                                              │
                                              ▼
                                            i_memif ──► M_Axi_Aw*/W*/B* ──► DDR
                                              │
                                              └─ Done(MemSm_Done) ──► i_statemachine
```

**控制面（CPU → 寄存器 → 状态机）**：

```
S_Axi_* ──► i_reg ──► Cfg_* (StrEna, GlbEna, PostTrig, Arm, RecMode, ...)
              │   ▲        │ CtxStr/CtxWin_* (上下文RAM, 双口共享)
              │   │        ▼
              │   │     i_statemachine ◄──► SmDma_Cmd/DmaSm_Resp ──► i_dma
              │   │        │
              │   └──────  │ StrIrq/StrLastWin (状态回读)
              │            ▼
              └─ Irq ◄── (i_reg 聚合中断) ◄── i_statemachine
```

要点：

- 输入逻辑（`g_input`）和控制状态机（`i_statemachine`）通过 `InpSm_Level`（数据电平）、`InpSm_HasTlast`（是否有末帧）、`InpSm_Ts*`（时间戳）等信号通信——状态机据此判断「哪路有数据可搬」。
- 状态机和 DMA 之间用 `SmDma_Cmd`（命令：地址+最大长度+流号）和 `DmaSm_Resp`（响应：实际传输字节数+触发标志+流号）握手。这两个 record 类型定义在 [hdl/psi_ms_daq_pkg.vhd:46-62](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L46-L62)。
- 上下文 RAM（`CtxStr_*`/`CtxWin_*`）是寄存器接口和状态机**共享**的双口存储——CPU 通过 A 口写配置（bufstart、winSize 等），状态机通过 B 口读写运行时指针（ptr、winEnd 等）。这正是 [hdl/psi_ms_daq_axi.vhd:196-199](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L196-L199) 里同一组信号既接到 `i_reg` 又接到 `i_statemachine` 的原因。

#### 4.4.3 源码精读

**例化 1：`i_reg`**（[hdl/psi_ms_daq_axi.vhd:231-298](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L231-L298)）。它一面接 AXI Slave 端口，一面把配置/状态/上下文信号扇出给其它模块，并把中断聚合成 `Irq => Irq`（[hdl/psi_ms_daq_axi.vhd:275](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L275)）。注意它还把 AXI Cache/Prot 吐给 `AWCache(0)` 等（[hdl/psi_ms_daq_axi.vhd:276-279](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L276-L279)），再由 `sync_apc_reg` 打拍后驱动 `M_Axi_*`。

**例化 2：`g_input`**（[hdl/psi_ms_daq_axi.vhd:303-349](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L303-L349)）。用 `for str in 0 to Streams_g - 1 generate` 为每路流例化一个 `psi_ms_daq_input`，并把该路的 generic（`StreamWidth_c(str)` 等）和端口（`Str_Data(str)` 等）一对一映射进去。关键点：

- 每路有独立复位 `InRst <= M_Axi_Areset or not Cfg_StrEna(str) or not Cfg_GlbEna;`（[hdl/psi_ms_daq_axi.vhd:308](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L308)）——禁用某路或全局未使能时，该路输入逻辑进入复位，停止采集。
- `IntDataWidth_g => IntDataWidth_g`（[hdl/psi_ms_daq_axi.vhd:320](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L320)）——内部数据宽度透传。

**例化 3：`i_statemachine`**（[hdl/psi_ms_daq_axi.vhd:358-390](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L358-L390)）。运行在内存时钟 `M_Axi_Aclk` 上，接收输入电平/末帧/时间戳，与 DMA 握手命令/响应，并访问上下文 RAM。注意 [hdl/psi_ms_daq_axi.vhd:355](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L355) 把输入侧和 DMA 侧的 HasLast 合并：`Sm_HasLast <= InpSm_HasTlast or DmaSm_HasLast;`——「末帧」可能来自输入缓冲或 DMA 缓冲任一侧。

**例化 4：`i_dma`**（[hdl/psi_ms_daq_axi.vhd:395-419](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L395-L419)）。承上（状态机命令）启下（内存接口命令），把流数据按 `IntDataWidth_g` 拼字、统计字节数，向 `i_memif` 发出 `Mem_CmdAddr/Size + Mem_DatData`。

**例化 5：`i_memif`**（[hdl/psi_ms_daq_axi.vhd:424-477](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L424-L477)）。把内存命令封装成 AXI Master 写突发。三个细节：

- `MaxOpenCommands_g => max(2, Streams_g)`（[hdl/psi_ms_daq_axi.vhd:430](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L430)）——源码注释解释：ISE 工具在只有 1 流时会把内存综合成触发器（资源浪费），所以强制至少按 2 流实现。
- `M_Axi_AwCache/AwProt/ArCache/ArProt => open`（[hdl/psi_ms_daq_axi.vhd:451-452](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L451-L452)、[468-469](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L468-L469)）——这四个 AXI 信号**不**由 `i_memif` 驱动，而是由顶层 `sync_apc_reg` 进程统一驱动（见 [hdl/psi_ms_daq_axi.vhd:223-226](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L223-L226)）。
- `Done => MemSm_Done`（[hdl/psi_ms_daq_axi.vhd:445](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L445)）——传输完成信号回灌给状态机，是中断生成链路的起点（详见 [u4-l1](u4-l1-irq-generation-fifo.md)）。

#### 4.4.4 代码实践

实践目标：**在源码里把「数据面」和「控制面」两条主线用颜色标出来**，建立顶层数据流的整体印象。

操作步骤：

1. 打开 [hdl/psi_ms_daq_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd)。
2. 在架构体信号声明区（[hdl/psi_ms_daq_axi.vhd:146-204](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L146-L204)）找出三组中间信号：`InpSm_*`/`InpDma_*`（输入与下游）、`SmDma_*`/`DmaSm_*`（状态机与 DMA）、`DmaMem_*`（DMA 与内存接口）、`CtxStr/CtxWin_*`（上下文共享）、`Cfg_*`/`Stat_*`（寄存器与各方）。
3. 跟踪一次完整的数据搬运：`Str_Data` →（`g_input`）→ `InpDma_Data` →（`i_dma`）→ `DmaMem_DatData` →（`i_memif`）→ `M_Axi_WData`。
4. 再跟踪一次完成的回灌：`i_memif` 的 `Done` → `MemSm_Done` → `i_statemachine` 的 `TfDone` → 最终影响 `Stat_StrIrq` → `i_reg` 聚合成 `Irq`。

需要观察的现象：顶层架构体里没有任何 `process` 在「计算数据」，只有 `sync_apc_reg` 这一个寄存化进程和大量连续赋值/例化——所有真正的逻辑都在 5 个子模块里。

预期结果：能在不看源码的情况下，凭直觉画出本节 [4.4.2](#442-核心流程) 的两张数据流框图。

> 待本地验证：如果用 [u1-l2](u1-l2-repo-and-simulation.md) 介绍的 PsiSim 跑顶层 testbench `psi_ms_daq_axi_tb`（详见 [u5-l3](u5-l3-toplevel-multistream-tb.md)），可以在波形里同时看到 `Str_Vld`、`InpDma_Vld`、`DmaMem_DatVld`、`M_Axi_WValid` 依次拉高，直观验证这条数据链。

#### 4.4.5 小练习与答案

**练习 1**：`g_input` 为什么用 `generate` 循环而不是单个例化？

> **答案**：因为每路流的 generic（宽度、缓冲、超时等）可能不同，必须按 `str` 索引从数组常量（如 `StreamWidth_c(str)`）里取对应的值传进每个 `psi_ms_daq_input`。`generate` 循环让「每路一个例化」可以用同一段代码描述。

**练习 2**：`CtxStr_Cmd`/`CtxStr_Resp` 这组信号为什么同时接到 `i_reg` 和 `i_statemachine` 两个模块？

> **答案**：因为上下文存储是一块**双口 RAM**，被寄存器接口和状态机共享：CPU 通过 `i_reg` 的 A 口写入静态配置（如 bufstart、winSize），状态机通过 B 口在运行时读写动态指针（如 ptr、winEnd）。同一组信号连到两个模块，正是「共享存储」的布线体现。

**练习 3**：源码注释说 `MaxOpenCommands_g => max(2, Streams_g)` 是为了规避 ISE 工具的资源问题。这对 Vivado 用户有影响吗？

> **答案**：功能上没影响（只是允许在途命令数 ≥ 2），资源上对 Vivado 用户可能略有浪费（多开了命令槽），但保持 `max(2, Streams_g)` 是为了在 ISE/Vivado 两种工具下都安全。这是「兼容旧工具」的保守写法。

## 5. 综合实践

把本讲学的「generic 配置 + 端口识别 + 数据流地图」串起来，完成下面这个**场景设计任务**（不要求真的综合，只要在纸上做对）。

**场景**：你要用 `psi_ms_daq_axi` 采集 **2 路 16 位数据**，流时钟是 **125 MHz**，挂在 **64 位 DDR** 上；两路优先级相同，每路需要 4 个窗口，使用时间戳，超时 0.5 ms。

任务：

1. **列 generic**：参照 [hdl/psi_ms_daq_axi.vhd:23-45](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L23-L45)，写出你会设置的所有非默认 generic 的取值。
2. **算打包比**：根据 [4.1.3](#413-源码精读) 的公式，计算每路几个采样会被打包成一个内部宽字。
3. **列端口连线**：参照 [4.2](#42-数据流输入端口)、[4.3](#43-axi-slave-与-axi-master-端口)，指出你需要从外部连接哪些 `Str_*`、`S_Axi_*`、`M_Axi_*` 信号，以及 `Irq`。

**参考答案**：

1. generic（其余用默认值即可）：
   - `Streams_g => 2`
   - `StreamWidth_g => (16, 16)`
   - `StreamClkFreq_g => (125.0e6, 125.0e6)`
   - `StreamTimeout_g => (0.5e-3, 0.5e-3)`
   - `MaxWindows_g => 4`（4 个窗口够用）
   - `IntDataWidth_g => 64`、`AxiDataWidth_g => 64`（DDR 64 位）
   - `StreamUseTs_g => (true, true)`、`StreamPrio_g => (1, 1)`（默认值，可省略）
2. 打包比：\(\lceil 64/16 \rceil = 4\)，即每路每 4 个 16 位采样被打包成一个 64 位内部字。
3. 端口连线：
   - 流输入（每路一组，下标 0/1）：`Str_Clk`、`Str_Data`(只用低 16 位)、`Str_Vld`、`Str_Rdy`、`Str_Trig`、`Str_Ts`。
   - AXI Slave（接 CPU）：`S_Axi_Aclk`、`S_Axi_Aresetn` + AR/R/AW/W/B 五通道（`AxiSlaveIdWidth_g=0` 时 `*Id` 为空向量可不接）。
   - AXI Master（接 DDR）：`M_Axi_Aclk`、`M_Axi_Aresetn` + 写通道 AW/W/B（读通道 AR/R 因只写可不接或留给默认值）+ `M_Axi_AwCache/AwProt`（由 ACPCFG 寄存器驱动，硬件内部已接好，外部仅看输出）。
   - 中断：`Irq`（输出到 CPU 中断控制器）。

## 6. 本讲小结

- 顶层实体 `psi_ms_daq_axi` 的 generic 分 **Streams / Recording / Axi / Axi Slave** 四组；Streams 组（除 `Streams_g` 外）都是「每路一个」的数组型 generic。
- 数据流输入端口（`Str_Clk/Data/Vld/Rdy/Trig/Ts`）按 `Streams_g` 数组化，外部端口统一用 `t_aslv64`，真正位宽由 `StreamWidth_g` + generate 块里的截位决定。
- IP 核对外有 **AXI Slave**（32 位寄存器、16 位地址，CPU 配置用）与 **AXI Master**（可配位宽、32 位地址，写 DDR 用）两套独立 AXI 接口；Master 实质只写不读。
- 架构体 `rtl` 用例化把 5 个子模块（`i_reg`、`g_input`、`i_statemachine`、`i_dma`、`i_memif`）拼起来；数据走「输入→DMA→AXI Master」主线，控制走「AXI Slave→状态机→各方」主线，上下文 RAM 被寄存器接口与状态机共享。
- 中断 `Irq` 由 `i_reg` 聚合各流的 `StrIrq` 产生；传输完成信号 `MemSm_Done` 是中断链路的起点。
- AXI Cache/Prot 输出不由 `i_memif` 驱动，而由顶层 `sync_apc_reg` 进程打两拍后驱动（ACPCFG 特性）。

## 7. 下一步学习建议

本讲建立了「顶层积木长什么样」的全局观。接下来建议：

- **想看清模块间通信用的 record 类型**（如 `DaqSm2DaqDma_Cmd_t`、`Input2Daq_Data_t`、上下文访问记录）→ 直接进入 [u2-l1 公共类型与记录包 psi_ms_daq_pkg](u2-l1-common-package.md)。
- **想理解输入逻辑如何跨时钟域、如何缓冲和拼字**（顶层的 `g_input` 内部）→ 进入 [u2-l2 输入逻辑：接口、时钟域与缓冲](u2-l2-input-interface-clocks.md)。
- **想先看软件视角、用 C 驱动操作这个 IP**（会反向加深对 AXI Slave 寄存器和中断的理解）→ 进入 [u1-l4 软件驱动快速上手](u1-l4-driver-quickstart.md)。

无论先走哪条线，回到本讲的 [4.4.2 数据流框图](#442-核心流程) 反复对照，都是把后续模块「装回整体」的最快方式。
