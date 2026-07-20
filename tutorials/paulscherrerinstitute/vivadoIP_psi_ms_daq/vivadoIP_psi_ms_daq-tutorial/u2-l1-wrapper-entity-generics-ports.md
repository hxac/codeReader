# 封装实体：泛型与端口全景

## 1. 本讲目标

学完本讲，你应当能够：

- 把 `hdl/psi_ms_daq_vivado.vhd` 的 entity 拆成「通用配置 / 录制 / AXI / 逐流」四组泛型，并说出每一组管什么。
- 解释为什么 16 路流要展开成 16 组标量泛型（`Stream0Width_g`…`Stream15Width_g`）而不是一个数组泛型。
- 在 port 区识别出 AXI Slave、AXI Master、AXI-Stream 输入、中断、触发这五大端口分组。
- 对照 `scripts/package.tcl` 说清 GUI 参数控件（下拉框 / 复选框 / 范围输入）与 VHDL 泛型的对应关系，并定位 `IntDataWidth_g` 的宽度约束来自哪句提示文本。

## 2. 前置知识

本讲承接 u1-l2「仓库目录结构」的核心结论：本仓库只是 Vivado IP 封装层，唯一的人写 RTL 文件就是 `hdl/psi_ms_daq_vivado.vhd`，它只做「外壳」，内部例化上游实现 `psi_ms_daq_axi`。本讲**只看这个外壳的 entity（接口契约）**，先不进入 architecture（连线逻辑，那是 u2-l2 的内容）。

需要先建立几个基础概念：

- **泛型（generic）**：entity 的编译期参数，相当于函数的「默认参数」。在 Vivado 里定制 IP 时改的那些参数，本质上就是在改泛型。本项目约定泛型名以 `_g` 结尾。
- **端口（port）**：entity 的物理引脚信号，分输入 `in` 和输出 `out`。
- **AXI4**：ARM 的总线协议，五条子通道：读地址 AR、读数据 R、写地址 AW、写数据 W、写响应 B。本项目 Slave/Master 都是完整 AXI4（带突发）。
- **AXI-Stream**：AXI 的流式协议，握手信号 `TValid`/`TReady`，数据 `TData`，包尾 `TLast`。
- **IP-XACT**：IEEE 1685 的 IP 描述 XML，Vivado IP 的 `component.xml` 就是它。**IP-XACT 要求每个对外参数都是一个独立的标量**——这是后面解释「为何 16 路流要展开成 16 组泛型」的关键。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| `hdl/psi_ms_daq_vivado.vhd` | 唯一的封装 RTL，entity + architecture 同在一个文件 | entity 的 **generic 区** 与 **port 区** |
| `scripts/package.tcl` | IP 打包脚本，定义 GUI 参数与端口使能条件 | GUI 参数定义、端口使能条件、`package_ip` |

这两个文件是「一一对应」关系：package.tcl 里每声明一个 GUI 参数，背后都对应 entity 里的一个同名泛型；package.tcl 里每条端口使能条件，背后都对应 entity 里的一个端口。

## 4. 核心概念与源码讲解

### 4.1 generic 区：四组泛型

#### 4.1.1 概念说明

generic 区是 IP 的「配置面板」。在 Vivado 里「Re-customize IP」时你看到的那些可填项，全部来自这里。本项目把泛型分成四组，对应 GUI 上的四个区域：

1. **General Config（通用配置）**：决定「整体形状」——几路流、要不要每流独立时间戳、要不要用 AXI-Stream 的 `Last` 当触发。
2. **Recording（录制）**：决定「数据搬运」——内部数据通路宽度、每流最多几个窗口、写内存突发的上下界。
3. **Axi（AXI 总线）**：决定「对外写内存的接口」——AXI Master 数据宽度、最大突发拍数、在途事务数、FIFO 深度。
4. **Streams（逐流）**：每路流自己的 7 个参数——数据宽度、优先级、缓冲深度、超时、时钟频率、时间戳 FIFO 深度、是否带时间戳。

#### 4.1.2 核心流程

四组泛型的整体布局如下：

```
entity psi_ms_daq_vivado
└── generic
    ├── General Config : Streams_g, TsPerStream_g, UseLastAsTrigger_g
    ├── Recording      : IntDataWidth_g, MaxWindows_g, MinBurstSize_g, MaxBurstSize_g
    ├── Axi            : AxiDataWidth_g, AxiMaxBurstBeats_g, AxiMaxOpenTrasactions_g, AxiFifoDepth_g
    ├── Streams ×16    : Stream{0..15}{Width, Prio, Buffer, TimeoutUs, ClkFreqHz, TsFifoDepth, UseTs}_g
    └── Vivado BD 常量 : C_S_Axi_ID_WIDTH
```

其中「逐流」这一组是体积的大头：每路 7 个泛型 × 16 路 = **112 个**，占整个 generic 区的绝大多数。

#### 4.1.3 源码精读

entity 从这里开始，前三个就是 General Config：

[hdl/psi_ms_daq_vivado.vhd:L22-L27](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L22-L27) — 声明 `entity psi_ms_daq_vivado`，并给出 General Config 三泛型。

```vhdl
entity psi_ms_daq_vivado is
	generic (
		-- General Config
		Streams_g				: positive range 1 to 32		:= 3;
		TsPerStream_g			: boolean						:= false;
		UseLastAsTrigger_g : boolean := false;
```

注意 `Streams_g` 在 entity 里写的是 `range 1 to 32`，但 GUI 实际只允许 1–16（见 4.3）。真正生效的上限是 16，因为端口和内部数组都按固定 16 路展开。

录制组泛型（Recording）：

[hdl/psi_ms_daq_vivado.vhd:L28-L32](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L28-L32) — 录制相关四泛型，注意 `IntDataWidth_g` 与 `MaxWindows_g` 的区别。

```vhdl
		-- Recording
		IntDataWidth_g			: positive                 		:= 64;
		MaxWindows_g			: positive range 1 to 32		:= 16;
		MinBurstSize_g			: integer range 1 to 512		:= 16;
		MaxBurstSize_g			: integer range 1 to 512		:= 256;
```

- `IntDataWidth_g`：内部数据通路宽度，数据在写进 AXI Master 之前先在这里汇聚。**没有 range 约束**（只是 `positive`），合法取值由 GUI 下拉框 `{64 128 256}` 限定，并受 4.3 的宽度链约束。
- `MaxWindows_g`：每路流最多几个触发窗口（1–32）。
- `MinBurstSize_g` / `MaxBurstSize_g`：写内存突发的上下界，单位是「内部宽度的字」。

AXI 组泛型：

[hdl/psi_ms_daq_vivado.vhd:L33-L37](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L33-L37) — AXI Master 接口的四个调参点。

```vhdl
		-- Axi
		AxiDataWidth_g			: natural range 64 to 1024		:= 64;
		AxiMaxBurstBeats_g		: integer range 1 to 256		:= 256;
		AxiMaxOpenTrasactions_g	: natural range 1 to 8			:= 8;
		AxiFifoDepth_g			: natural						:= 1024;
```

逐流组泛型（以 Stream0 为例，其余 Stream1..Stream15 完全同构）：

[hdl/psi_ms_daq_vivado.vhd:L39-L45](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L39-L45) — 每路流 7 个泛型，Stream0 的样板。

```vhdl
		Stream0Width_g			: integer range 8 to 64			:= 32;
		Stream0Prio_g			: integer range 1 to 3			:= 2;
		Stream0Buffer_g			: integer 						:= 256;
		Stream0TimeoutUs_g		: integer						:= 1e3;		-- in microseconds
		Stream0ClkFreqHz_g		: integer						:= 100e6;
		Stream0TsFifoDepth_g	: integer						:= 16;
		Stream0UseTs_g			: boolean						:= true;
```

每路流 7 个泛型的含义（适用于所有 Stream0..Stream15）：

| 泛型 | 类型/范围 | 含义 |
|------|----------|------|
| `StreamNWidth_g` | int 8–64 | 该流 AXI-Stream 数据位宽 |
| `StreamNPrio_g` | int 1–3 | DMA 仲裁优先级（数越大越优先） |
| `StreamNBuffer_g` | int | 输入缓冲深度（以输入字为单位） |
| `StreamNTimeoutUs_g` | int | 超时时间（微秒），到期强制把缓冲里不够一次突发的数据搬走 |
| `StreamNClkFreqHz_g` | int | 该流时钟频率（Hz），用来把超时换算成周期数 |
| `StreamNTsFifoDepth_g` | int | 时间戳 FIFO 深度 |
| `StreamNUseTs_g` | bool | 是否记录时间戳 |

最后还有一个不属于上面四组的特殊泛型：

[hdl/psi_ms_daq_vivado.vhd:L167-L168](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L167-L168) — BD 注入的 AXI Slave ID 宽度常量。

```vhdl
		-- Vivado BD Constants
		C_S_Axi_ID_WIDTH		: integer						:= 0
```

`C_S_Axi_ID_WIDTH` 是 Vivado Block Design 自动注入的常量（不是 GUI 手填项），它决定 AXI Slave 的 ID 信号位宽。BD 里上游 AXI 主端带不带 ID、ID 多宽，打包时由 `bd.tcl` 传播进来（详见 u2-l3）。默认 0 表示「无 ID」，对应 `std_logic_vector(-1 downto 0)` 即空向量（AXI4-Lite 风格）。

> **为什么是 16 组标量泛型，而不是一个数组泛型？**
> 因为 IP-XACT（`component.xml`）要求每个对外参数都是一个独立的标量控件——下拉框、复选框或数字框，没法把「一个数组」渲染成单个 GUI 字段并允许每元素独立选择。所以封装层只能把每路流的 7 个参数都展开成 16 份标量泛型（共 112 个）。而真正接受数组泛型的实现 `psi_ms_daq_axi`，由 architecture 在例化时把这 112 个标量重新聚合成数组——这部分在 u2-l3 精读。

#### 4.1.4 代码实践

**实践目标**：亲手把 entity 的泛型归类到四组，并理解每路流为什么是 7 个泛型。

**操作步骤**：

1. 打开 `hdl/psi_ms_daq_vivado.vhd`，定位到 L23 的 `generic (`。
2. 顺着注释 `-- General Config`、`-- Recording`、`-- Axi`、`-- Streams` 把泛型分桶。
3. 数一下 Stream0..Stream15 各自的泛型个数（应为 7×16 = 112 个）。
4. 思考：如果你是 IP 作者，能否把这 112 个逐流泛型合并成一个数组泛型 `StreamWidth_g : t_int_array`，让 GUI 直接编辑数组？（提示：结合上面「为什么是 16 组标量」的说明。）

**需要观察的现象**：

- entity 里逐流泛型是**完全平铺**的，没有任何数组语法。
- `IntDataWidth_g`、`MaxWindows_g`、`StreamNBuffer_g` 等用了无 range 的 plain `integer`/`positive`，合法范围留给 GUI。

**预期结果**：你会得到一张「General Config 3 + Recording 4 + Axi 4 + Streams 112 + C_S_Axi_ID_WIDTH 1 = 124 个泛型」的清单。逐流占绝大多数，这正是封装层「最啰嗦」的地方。

> 待本地验证：若本地有 Vivado，可 `Open IP Packager` 后在「Re-customize IP」对话框里数 GUI 控件个数，应与上述泛型数大致对应。

#### 4.1.5 小练习与答案

**练习 1**：`Streams_g` 在 entity 里声明为 `range 1 to 32`，但实际能配置的最大值是多少？为什么？

**答案**：实际最大 16。原因有二：端口 `Str00..Str15` 只展开了 16 路，且 GUI `gui_parameter_set_range 1 16`（package.tcl L92）把范围进一步限制到 1–16。entity 的 `1 to 32` 只是更宽松的类型范围，真正生效的工程上限是 16。

**练习 2**：为什么 `IntDataWidth_g` 和 `AxiFifoDepth_g` 没有写 `range`？

**答案**：它们的合法取值是离散的（64/128/256）或与其他参数耦合，用 VHDL 的 `range` 表达不便，于是把约束下放到 GUI（下拉框）和提示文本，由工具/人工保证取值合法。

---

### 4.2 port 区：五大端口分组

#### 4.2.1 概念说明

port 区是 IP 的「物理接口」。这个 IP 对外有五种角色：

1. **AXI Slave（寄存器访问）**：CPU 通过它读写 IP 内部的配置/状态寄存器。固定 32 位数据、16 位地址。
2. **AXI Master（写内存）**：IP 通过它把采集到的数据**直接写进 DDR**，不经过 CPU。
3. **AXI-Stream 输入（Str00..Str15）**：16 路待采集的数据流输入，每路可独立时钟、可异宽。
4. **中断（Irq）**：采集到数据/窗口完成时通知 CPU。
5. **触发（Trig）**：外部送进来的触发信号（当不使用「Last 当触发」模式时）。

另外还有一个 `StrX_Ts`：当多路流**共用一个时间戳**时（`TsPerStream_g=false`），从这个端口统一输入。

#### 4.2.2 核心流程

一个完整的数据通路可以这么理解：

```
        ┌──────────────────────────────────────────────┐
CPU ──► │ S_Axi (Slave, 32b) ──► 寄存器配置            │
        │                                              │
Str00 ──►│ Stream inputs (×16, 独立时钟) ──► 采集/DMA   │
Str01 ──►│                                              │
 ...    │                                              │ ──► M_Axi (Master) ──► DDR
Str15 ──►│                                              │
        │                                              │
Trig ──► │ 触发输入 (可选)                              │ ──► Irq ──► CPU 中断
        └──────────────────────────────────────────────┘
```

数据从 AXI-Stream 输入进来，经内部采集与 DMA，从 AXI Master 写出去；CPU 走 AXI Slave 配置，靠 Irq 收通知。

#### 4.2.3 源码精读

port 区从这里开始：

[hdl/psi_ms_daq_vivado.vhd:L170-L175](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L170-L175) — AXI Slave 接口起点，可见 32 位数据与 16 位地址。

```vhdl
	port (
		-- AXI Slave Interface for Register Access
		S_Axi_Aclk		: in	std_logic;
		S_Axi_ArId		: in	std_logic_vector(C_S_Axi_ID_WIDTH-1 downto 0);
		S_Axi_Aresetn	: in	std_logic;
		S_Axi_ArAddr	: in	std_logic_vector(15 downto 0);
		S_Axi_Arlen		: in	std_logic_vector(7 downto 0);
```

**AXI Slave 组**（L171–L208）覆盖 AXI4 的五条子通道：读地址 AR、读数据 R、写地址 AW、写数据 W、写响应 B。两个关键宽度：

- 地址 `S_Axi_ArAddr/AwAddr : std_logic_vector(15 downto 0)` = 16 位，寻址空间 \(2^{16}=65536\) 字节 = 64 KB。
- 数据 `S_Axi_RData/WData : std_logic_vector(31 downto 0)` = 32 位（寄存器按字访问）。
- ID 信号 `S_Axi_ArId/RId/AwId/BId` 的宽度由 `C_S_Axi_ID_WIDTH` 决定，默认 0（空向量）。

**AXI Master 组**（L210–L243）同样五条子通道，但方向相反（IP 是主端）：

[hdl/psi_ms_daq_vivado.vhd:L210-L223](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L210-L223) — AXI Master 接口，数据宽度绑定 `AxiDataWidth_g`。

```vhdl
		-- AXI Master Interface for Memory Access
		M_Axi_Aclk		: in	std_logic;
		M_Axi_Aresetn	: in	std_logic;
		M_Axi_AwAddr	: out	std_logic_vector(31 downto 0);
		M_Axi_AwLen		: out	std_logic_vector(7 downto 0);
		...
		M_Axi_WData		: out	std_logic_vector(AxiDataWidth_g-1 downto 0);
		M_Axi_WStrb		: out	std_logic_vector(AxiDataWidth_g/8-1 downto 0);
```

三个关键点：

- 地址 `M_Axi_AwAddr/ArAddr : std_logic_vector(31 downto 0)` = 32 位，可寻址 4 GB（足够覆盖 ZCU102 的 DDR）。
- 数据 `M_Axi_WData/RData` 宽度直接绑定泛型 `AxiDataWidth_g`；`WStrb` 是每字节一个 strobe，所以宽度是 `AxiDataWidth_g/8`。
- Master 的输入信号（`M_Axi_AwReady`、`M_Axi_WReady`、`M_Axi_BValid` 等）都带默认值 `:= '0'`，顶层不连接时也能综合通过。
- Master **没有 ID 信号**（只有 `BResp`/`RResp`，没有 `BId`/`RId`），因为写内存不需要区分多主端的响应路由。

**Misc 组**（L245–L248）：

[hdl/psi_ms_daq_vivado.vhd:L245-L248](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L245-L248) — 中断、触发、共用时间戳三个杂项端口。

```vhdl
		-- Miscellaneous
		Irq				: out	std_logic;
		Trig			: in	std_logic_vector(Streams_g-1 downto 0);
		StrX_Ts			: in	std_logic_vector(63 downto 0);
```

- `Irq`：单比特中断输出（电平敏感）。
- `Trig`：宽度随 `Streams_g` 变化——每路流一个触发位。仅当 `UseLastAsTrigger_g=false` 时才出现（见 4.3）。
- `StrX_Ts`：64 位共用时间戳输入，仅当 `TsPerStream_g=false` 时出现。

**AXI-Stream 输入组**（L250–L361）：`Str00..Str15` 共 16 路，每路 6 个信号。以 Str00 为例：

[hdl/psi_ms_daq_vivado.vhd:L251-L256](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L251-L256) — 单路 AXI-Stream 输入的 6 个信号，`TData` 宽度绑定逐流泛型。

```vhdl
		Str00_Clk		: in	std_logic;
		Str00_TData		: in	std_logic_vector(Stream0Width_g-1 downto 0);
		Str00_Ts		: in	std_logic_vector(63 downto 0);
		Str00_TValid	: in	std_logic;
		Str00_TReady	: out	std_logic;
		Str00_TLast 	: in	std_logic;
```

每路流的 6 类信号：

| 信号 | 方向 | 含义 |
|------|------|------|
| `StrNN_Clk` | in | 该流独立时钟（不同流可异频异相） |
| `StrNN_TData` | in | 数据，宽度 = 对应 `StreamNWidth_g` |
| `StrNN_Ts` | in | 64 位时间戳（仅 `TsPerStream_g=true` 时用） |
| `StrNN_TValid` | in | AXI-Stream 握手：数据有效 |
| `StrNN_TReady` | out | AXI-Stream 握手：IP 准备好接收 |
| `StrNN_TLast` | in | AXI-Stream 包尾标记（也兼触发源） |

注意 `StrNN_TData` 的宽度直接绑到逐流泛型 `StreamNWidth_g`，所以**不同流可以有不同位宽**——这是这个 IP「多流异构」的体现。

#### 4.2.4 代码实践

**实践目标**：在 port 区数清五大分组，并验证「数据宽度跟泛型走」。

**操作步骤**：

1. 在 `hdl/psi_ms_daq_vivado.vhd` 里分别搜索 `S_Axi_`、`M_Axi_`、`Str0`、`Irq`、`Trig`，给每组端口计数。
2. 找到 `M_Axi_WStrb`（L223），验证它的宽度表达式 `AxiDataWidth_g/8-1 downto 0`：当 `AxiDataWidth_g=64` 时，WStrb 应为 8 位（\(64/8=8\)，每字节一个 strobe）。
3. 找到 `Trig`（L247），验证它的宽度 `Streams_g-1 downto 0`：当 `Streams_g=3` 时只有 3 位。

**需要观察的现象**：

- Slave 有 ID 信号（`ArId/RId/AwId/BId`），Master 没有。
- Master 的 ready/valid 输入带 `:= '0'` 默认值，Slave 的不带。
- 16 路 Stream 输入完全平铺，没有 for-generate——因为这是 entity 接口层，端口必须显式声明。

**预期结果**：你会确认 Slave 5 通道齐全、Master 5 通道齐全但无 ID、16 路 Stream 每路 6 信号、外加 Irq/Trig/StrX_Ts 三个杂项端口。

> 待本地验证：综合后查看 Vivado 的端口列表（Port Table），分组与方向应与本节描述一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么 AXI Master 的输入信号（`M_Axi_AwReady` 等）都有 `:= '0'` 默认值，而 AXI Slave 的输入信号没有？

**答案**：Master 的输入来自外部从端（如 DDR 控制器），在 IP 例化但尚未连接到真实从端时，给默认值 `'0'` 能让综合/仿真不至于出现未驱动信号（`'U'`）导致错误。Slave 的输入必然由 CPU/互连驱动，不存在「悬空」场景，故无需默认值。

**练习 2**：`Str00_TData` 的宽度由什么决定？如果用户在 GUI 把 Stream0 的 Data Width 设为 16，这个端口会变成几位？

**答案**：由 `Stream0Width_g` 决定（L252：`Stream0Width_g-1 downto 0`）。设为 16 后，`Str00_TData` 就是 `std_logic_vector(15 downto 0)`，即 16 位。

**练习 3**：`Trig` 端口的宽度是 `Streams_g-1 downto 0`。如果 `Streams_g=3` 但用户想给所有 16 路都送外部触发，行不行？

**答案**：不行。`Trig` 宽度随 `Streams_g` 动态裁剪，只有实际启用的路数有触发位。未启用的路（`Streams_g..15`）连端口都不存在（被端口使能条件裁掉，见 4.3）。

---

### 4.3 package.tcl：GUI 参数与泛型的对应

#### 4.3.1 概念说明

`scripts/package.tcl` 是把 entity 变成「Vivado 可视化可配置 IP」的桥梁。它做三件事：

1. 为每个泛型定义一个 GUI 控件（下拉框 / 复选框 / 范围输入框）。
2. 为端口定义「使能条件」——什么参数组合下，哪个端口该出现。
3. 调用 `package_ip` 生成 `component.xml`（IP-XACT）与 `xgui/*.tcl`（GUI 布局）。

理解这一节的关键：**GUI 参数 ≠ 泛型本身，而是泛型的「可视化包装」**。改泛型要去 `.vhd`，改 GUI 控件类型/范围要去 package.tcl。

#### 4.3.2 核心流程

每个 GUI 参数的「三步走」：

1. `gui_create_parameter <泛型名> <显示文本>` —— 声明参数。
2. `gui_parameter_set_widget_*` 或 `gui_parameter_set_range` —— 设置控件类型与可选值。
3. `gui_add_parameter` —— 真正添加进当前页面。

整体流向：

```
package.tcl                              component.xml (产物)
─────────────                            ──────────────────────────
gui_create_parameter "Streams_g"         <spirit:parameter spirit:name="Streams_g">
gui_parameter_set_range 1 16      ──►          (format=long, 约束 1..16)
gui_add_parameter
                  ↓
add_port_enablement_condition            <port spirit:name="Str00_TData" ...>
  "Str00_TData"  "$Streams_g > 0"  ──►         (条件生效才出现在 IP 接口)
```

#### 4.3.3 源码精读

**General Configuration 页**：

[scripts/package.tcl:L88-L109](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L88-L109) — 通用配置页：Streams_g 范围、两个布尔开关、IntDataWidth 下拉。

```tcl
gui_add_page "General Configuration"

gui_create_parameter "Streams_g" "Number of Streams"
gui_parameter_set_range 1 16
gui_add_parameter
...
gui_create_parameter "IntDataWidth_g" "Internal Data Width \[max(Stream Data Width) <= Internal Data Width <= AXI Master Data Width\]"
gui_parameter_set_widget_dropdown {64 128 256}
gui_add_parameter
```

关键对应关系：

- `Streams_g` → 范围输入 1–16（把 entity 的 1–32 收紧到 1–16）。
- `TsPerStream_g` / `UseLastAsTrigger_g` → 复选框（`gui_parameter_set_widget_checkbox`）。
- `IntDataWidth_g` → 下拉框 `{64 128 256}`，**提示文本里就写明了宽度约束**：

\[ \max_{n}(\text{StreamNWidth\_g}) \;\le\; \text{IntDataWidth\_g} \;\le\; \text{AxiDataWidth\_g} \]

方括号里的 `[max(Stream Data Width) <= Internal Data Width <= AXI Master Data Width]` 就是本讲实践任务要找的出处。

**AXI Master 页**：

[scripts/package.tcl:L119-L128](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L119-L128) — AXI Master 页：数据宽度、突发拍数（含 AXI3/AXI4 提示）。

```tcl
gui_create_parameter "AxiDataWidth_g" "AXI Master Data Width"
gui_parameter_set_widget_dropdown {64 128 256 512 1024}
gui_add_parameter

gui_create_parameter "AxiMaxBurstBeats_g" "Maximum AXI burst size (16 for AXI-3, 256 for AXI-4)"
gui_parameter_set_range 1 256
```

注意 `AxiDataWidth_g` 下拉范围 `{64 128 256 512 1024}` ⊇ `IntDataWidth_g` 的 `{64 128 256}`，所以上面 `IntDataWidth_g <= AxiDataWidth_g` 约束在数值上总是可达。`AxiMaxBurstBeats_g` 的提示文本还顺带科普了 AXI3（最大 16 拍）与 AXI4（最大 256 拍）的差异。

**Streams 页**：用 Tcl 循环为 16 路流各生成一个页面：

[scripts/package.tcl:L138-L147](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L138-L147) — 用 for 循环批量生成 16 个 Stream 页与逐流参数。

```tcl
for {set i 0} {$i < 16} {incr i} {
	gui_add_page "Stream $i"

	gui_create_parameter "Stream$i\Width_g" "Data Width"
	gui_parameter_set_widget_dropdown {8 16 32 64}
	gui_add_parameter

	gui_create_parameter "Stream$i\Prio_g" "Priority"
	gui_parameter_set_widget_dropdown {1 2 3}
	gui_add_parameter
	...
```

`Stream$i\Width_g` 这种字符串拼接，正是 entity 里 `Stream0Width_g..Stream15Width_g` 的来源——GUI 工具用循环展开，entity 只能手写 16 份（VHDL 的 generic 区不支持这种循环）。

**端口使能条件**：决定哪些端口在什么参数组合下出现：

[scripts/package.tcl:L171-L182](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L171-L182) — 端口使能条件：按 Streams_g 裁剪流端口、StrX_Ts 与 Trig 的互斥条件。

```tcl
for {set i 0} {$i < 16} {incr i} {
	set i02 [format "%02d" $i]
	add_port_enablement_condition "Str$i02\_TData" "\$Streams_g > $i"
	add_port_enablement_condition "Str$i02\_Ts" "(\$Streams_g > $i) && \$Stream$i\UseTs_g && \$TsPerStream_g"
	...
	add_interface_enablement_condition "Str$i02" "\$Streams_g > $i"
}
add_port_enablement_condition "StrX_Ts" "!\$TsPerStream_g"
add_port_enablement_condition "Trig"    "!\$UseLastAsTrigger_g"
```

读这三条使能条件：

- `StrNN_TData` 在 `Streams_g > N` 时出现——只有前 `Streams_g` 路流的端口被保留。
- `StrNN_Ts`（每流独立时间戳）需三条件同时满足：该流被启用、该流 `UseTs_g=true`、全局 `TsPerStream_g=true`。
- `StrX_Ts`（共用时间戳）在 `TsPerStream_g=false` 时出现——与 `StrNN_Ts` 互斥。
- `Trig`（外部触发）在 `UseLastAsTrigger_g=false` 时出现——与「用 Last 当触发」互斥。

**打包目标**：

[scripts/package.tcl:L187-L189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L187-L189) — 调用 package_ip，目标器件为 ZCU102 的 xczu9eg。

```tcl
set TargetDir ".."
#											Edit  Synth	Part
package_ip $TargetDir 						false  true	xczu9eg-ffvb1156-2-e
```

目标器件 `xczu9eg-ffvb1156-2-e` 就是 ZCU102 板上的 Zynq UltraScale+ MPSoC；`Synth=true` 表示打包时已预先综合。

#### 4.3.4 代码实践

**实践目标**：定位 `IntDataWidth_g` 宽度约束的出处，并验证三条数据宽度（流宽 / 内部宽 / AXI 宽）的取值集合满足约束链。

**操作步骤**：

1. 在 `scripts/package.tcl` 中搜索 `IntDataWidth_g`（L107），读出提示文本方括号里的约束。
2. 分别查 Stream 宽度下拉（L142，`{8 16 32 64}`）、IntDataWidth 下拉（L108，`{64 128 256}`）、AxiDataWidth 下拉（L123，`{64 128 256 512 1024}`）。
3. 验证：最大的流宽是 64，最小的内部宽是 64，故 `max(流宽)=64 <= min(IntDataWidth)=64` 恒成立；当 `IntDataWidth=256` 时，必须选 `AxiDataWidth ∈ {256, 512, 1024}` 才满足 `IntDataWidth <= AxiDataWidth`。

**需要观察的现象**：

- 约束 `max(Stream Width) <= IntDataWidth <= AxiDataWidth` 不在 VHDL 的 `range` 里，也不在 Tcl 的 `set_range` 里，而是**纯靠提示文本**告诉用户。工具不会强制校验，填错会在综合或行为上出问题。

**预期结果**：你会得出结论——这句约束**不是硬约束**，而是「人读的契约」。它的存在恰恰说明三个宽度参数有耦合，配置时必须整体考虑。

> 待本地验证：在 Vivado 里故意把 `IntDataWidth_g=256`、`AxiDataWidth_g=64`，观察综合阶段的报错信息（应提示数据通路宽度不匹配）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 entity 里 `Streams_g` 写 `range 1 to 32`，而 package.tcl 里 `gui_parameter_set_range 1 16`？两者矛盾吗？

**答案**：不矛盾。VHDL 的 `range 1 to 32` 是类型层面的合法区间（放宽写更安全，方便上游实现复用）；package.tcl 的 `1 16` 是 GUI 层面真正暴露给用户的可选区间，更严格。实际生效的是 GUI 的 1–16（端口和内部数组都按 16 展开）。

**练习 2**：端口使能条件 `StrNN_Ts` 的表达式是 `(\$Streams_g > $i) && \$Stream$i\UseTs_g && \$TsPerStream_g`，三个条件分别防什么？

**答案**：① `Streams_g > i` 防止给未启用的路暴露端口；② `StreamNUseTs_g` 防止该流根本不要时间戳却留个空端口；③ `TsPerStream_g` 防止全局采用「共用时间戳」模式时还出现每流独立时间戳端口。

**练习 3**：如果用户在 GUI 同时勾选 `UseLastAsTrigger_g=true`，但顶层又想连 `Trig` 端口，会发生什么？

**答案**：`Trig` 端口在 `UseLastAsTrigger_g=true` 时被使能条件裁掉（L182 `!\$UseLastAsTrigger_g`），根本不会出现在 IP 接口上，用户想连也连不上。触发改由各流的 `TLast` 提供（这部分连线在 architecture 里，见 u2-l2）。

---

## 5. 综合实践

**任务**：你是这个 IP 的使用者，要在 ZCU102 上用 3 路 AXI-Stream 采集传感器数据。请基于本讲的 entity 与 package.tcl，回答下面一组配置问题，并画出端口连接草图。

给定配置：

- `Streams_g = 3`
- Stream0：16 位 ADC，带时间戳，独立时钟 100 MHz
- Stream1：32 位 DAC 回采，不带时间戳，独立时钟 125 MHz
- Stream2：8 位低速 GPIO，带时间戳，独立时钟 50 MHz
- `TsPerStream_g = true`，`UseLastAsTrigger_g = true`
- `IntDataWidth_g = 64`，`AxiDataWidth_g = 64`

请回答：

1. 实际会有几路 `StrNN_*` 端口出现？（Str00/01/02 还是更多？）
2. `StrX_Ts` 和 `Trig` 这两个端口会出现吗？为什么？
3. Stream1 不带时间戳（`UseTs_g=false`），那 `Str01_Ts` 端口会出现吗？
4. 验证 `max(流宽) <= IntDataWidth <= AxiDataWidth` 是否满足。最大流宽是多少？
5. 画一张简图，标出 CPU→S_Axi、Str00/01/02→IP、IP→M_Axi→DDR、IP→Irq→CPU 的连线。

**参考答案要点**：

1. 只有 Str00、Str01、Str02 三路端口（Str03..Str15 被 `$Streams_g > $i` 裁掉）。
2. 都不会出现。`StrX_Ts` 需要 `!TsPerStream_g`（现在是 true）；`Trig` 需要 `!UseLastAsTrigger_g`（现在是 true）。
3. 不会。`Str01_Ts` 的使能条件是 `(Streams_g>1) && Stream1UseTs_g && TsPerStream_g`，中间项为 false。
4. 最大流宽 = max(16, 32, 8) = 32；`32 <= 64 <= 64` 成立。
5. 略（连线方向见 4.2.2 的数据通路图）。

> 待本地验证：在 Vivado 里按上述参数 Re-customize IP，观察 IP 符号上实际出现的端口，与你的预测逐项核对。

## 6. 本讲小结

- `hdl/psi_ms_daq_vivado.vhd` 的 generic 分四组：General Config（形状）、Recording（搬运）、Axi（内存接口）、Streams×16（逐流），外加 BD 注入的 `C_S_Axi_ID_WIDTH`。
- 16 路流展开成 16 组标量泛型（共 112 个），而不是数组泛型——因为 IP-XACT 要求每个泛型在 GUI 上是独立标量控件；数组聚合发生在 architecture 的 `i_impl` 例化里（u2-l3 详讲）。
- port 区分五大组：AXI Slave（32 位数据 / 16 位地址，配寄存器）、AXI Master（`AxiDataWidth_g` 数据 / 32 位地址，写 DDR）、16 路 AXI-Stream 输入（每路独立时钟、可异宽）、Irq（电平中断）、Trig（外部触发）。
- AXI-Stream 每路 6 个信号：`_Clk/_TData/_Ts/_TValid/_TReady/_TLast`，其中 `_TData` 宽度绑定逐流 `StreamNWidth_g`，`_Ts` 仅在 `TsPerStream_g` 且该流 `UseTs_g` 时出现。
- `scripts/package.tcl` 用三步（`gui_create_parameter` → 设控件 → `gui_add_parameter`）把每个泛型包成 GUI 控件，用 `add_port_enablement_condition` 控制端口去留。
- `IntDataWidth_g` 的宽度约束 `max(流宽) <= 内部宽 <= AXI 宽` 只写在 package.tcl 的提示文本里（L107），是「人读契约」而非硬校验，配置时需整体权衡。

## 7. 下一步学习建议

本讲只读了 entity（接口契约），完全没碰 architecture（连线逻辑）。下一讲 **u2-l2「标量到数组的映射：generate 块与时间戳/触发选择」** 正好接上：你会看到那 112 个标量泛型如何被 architecture 里的 `All_*` 固定 16 路数组信号、`g_stream` generate 块投影成 `Streams_g` 路实际数组，以及 `TsPerStream_g` / `UseLastAsTrigger_g` 两个开关如何决定 `Str_Ts` 和 `trigger` 的连线来源。

建议在进入 u2-l2 前，先回头确认本讲这几个点你已经清楚：

- 四组泛型各管什么；
- 为什么是 16 组标量而不是数组；
- `StrNN_TData` 宽度跟哪个泛型走；
- `Trig` 和 `StrX_Ts` 各自的「不出现」条件。

如果想顺便了解 `C_S_Axi_ID_WIDTH` 是怎么从 BD 传进来的，可以提前扫一眼 `bd/bd.tcl`（u2-l3 会精读）。
