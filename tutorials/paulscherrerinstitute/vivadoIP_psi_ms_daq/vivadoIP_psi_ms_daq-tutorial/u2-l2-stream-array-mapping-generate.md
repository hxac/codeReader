# 标量到数组的映射：generate 块与时间戳/触发选择

## 1. 本讲目标

学完本讲你应该能够：

- 说清楚为什么 `psi_ms_daq_vivado` 的架构体里要先声明一套固定 16 路的 `All_*` 数组信号，再声明一套长度由 `Streams_g` 决定的 `Str_*` 数组信号。
- 读懂 16 段「逐行硬连线」代码：如何把 `Str00_Clk`、`Str00_TData` … `Str15_TLast` 这些**固定名字的标量端口**灌进 `All_*` 数组，并理解 `TData` 为什么要用「部分切片赋值」。
- 读懂 `g_stream` 这个 `for … generate` 循环如何把前 `Streams_g` 路从 `All_*` 投影到 `Str_*`，以及循环内嵌套的 `g_tsstr` / `g_ntsstr` 如何根据 `TsPerStream_g` 选择时间戳来源。
- 读懂顶层的 `g_trig` / `g_ntrig` 如何根据 `UseLastAsTrigger_g` 选择触发来源（`Str_Lst` 还是外部 `Trig` 端口）。
- 能在给定三个开关（`Streams_g`、`TsPerStream_g`、`UseLastAsTrigger_g`）的情况下，画出 `Str_Ts` 与 `trigger` 到底连到了哪些源信号。

本讲只读一个文件：仓库唯一的 RTL 文件 `hdl/psi_ms_daq_vivado.vhd` 的**架构体（architecture rtl）**部分。entity 部分已在 [u2-l1](u2-l1-wrapper-entity-generics-ports.md) 讲过，下一讲 [u2-l3](u2-l3-instantiating-impl.md) 才讲 `i_impl` 的例化。本讲聚焦在两者之间的「信号重排层」。

## 2. 前置知识

### 2.1 为什么需要「信号重排」

先回顾一个关键事实（来自 [u2-l1](u2-l1-wrapper-entity-generics-ports.md) 和 [u1-l2](u1-l2-repository-structure.md)）：

- 本仓库只是 **Vivado IP 封装层（wrapper）**，真正实现采集逻辑的是上游 `psi_multi_stream_daq` 里的 `entity psi_ms_daq_axi`。
- Vivado Block Design（BD）要求**对外端口必须是固定的、有名字的标量**，例如 `Str00_TData`、`Str01_TData` … `Str15_TData`，一共 16 组、每组 6 个信号。它**不接受**「长度由 generic 决定的数组端口」。
- 但上游实现 `psi_ms_daq_axi` 的端口恰恰是**数组型**的，例如 `Str_Data : t_aslv64(Streams_g-1 downto 0)`，长度由 `Streams_g` 决定。

于是 wrapper 必须在中间做一层「翻译」：

```
固定 16 路标量端口（BD 可见）   ──逐行赋值──▶  固定 16 路数组 All_*  ──generate 投影──▶  Streams_g 路数组 Str_*  ──port map──▶  i_impl 实现
        Str00..Str15                              All_*(0..15)                          Str_*(0..Streams_g-1)                psi_ms_daq_axi
```

这就是本讲要精读的全部内容。

### 2.2 VHDL 术语速查

| 术语 | 一句话解释 |
|---|---|
| `architecture` | 描述 entity 内部「怎么连、怎么做」的部分。本文件用 `architecture rtl of psi_ms_daq_vivado is`。 |
| `signal` | 架构体内部的连线，类似电路里的导线。 |
| `std_logic_vector(0 to 15)` | 一个 16 位的数组，**升序**编号（`to` 方向）。 |
| `std_logic_vector(N-1 downto 0)` | 一个 N 位的数组，**降序**编号（`downto` 方向）。 |
| `for … generate` | 综合期的循环，把一段电路复制 N 份。**不是运行时循环**，是「画 N 份原理图」。 |
| `if … generate` | 综合期的条件，满足条件才「画」这段电路。 |
| `t_aslv64` | 上游 `psi_common_array_pkg` 提供的类型：「元素为 `std_logic_vector` 的数组」。本仓库不定义它，只通过第 16 行 `use work.psi_common_array_pkg.all;` 引入。从用法看，它的每个元素宽度至少为 64 位（`All_Ts(i)` 整体接收 64 位的 `StrNN_Ts`，`All_Data(i)` 用到 `(StreamNWidth_g-1 downto 0)` 切片且最宽 64 位）。 |

> 注意：`t_aslv64` 的确切定义在**上游 `psi_common` 仓库**里，本仓库不含该文件。上面关于「元素至少 64 位」的结论是从本文件的实际用法推断的，如果你需要 100% 确认，请到 `psi_common` 仓库的 `psi_common_array_pkg.vhd` 查阅 `t_aslv64` 的声明（待确认上游确切定义）。

## 3. 本讲源码地图

本讲只涉及一个文件，但聚焦在它的中后段：

| 源码位置 | 作用 |
|---|---|
| `hdl/psi_ms_daq_vivado.vhd` 第 369–382 行 | 架构体信号声明：固定 16 路的 `All_*` 与可变长度的 `Str_*`。 |
| `hdl/psi_ms_daq_vivado.vhd` 第 419–529 行 | 16 段「逐行硬连线」：把 `Str00..Str15` 端口灌进 `All_*`。 |
| `hdl/psi_ms_daq_vivado.vhd` 第 531–545 行 | `g_stream` 循环 + 嵌套 `g_tsstr` / `g_ntsstr`：投影 + 时间戳条件选择。 |
| `hdl/psi_ms_daq_vivado.vhd` 第 547–552 行 | `g_trig` / `g_ntrig`：触发源条件选择。 |
| `hdl/psi_ms_daq_vivado.vhd` 第 247–248 行 | entity 里的 `Trig` 与 `StrX_Ts` 端口（本讲会用到的两个外部信号）。 |

> 第 384–415 行的 `TimeoutUs_c` / `TimeoutsSec_c` / `FreqHz_c` / `FreqReal_c` 属于 [u2-l3](u2-l3-instantiating-impl.md) 的内容（逐流泛型聚合），本讲不展开。

## 4. 核心概念与源码讲解

### 4.1 两套信号：固定 16 路的 `All_*` 与可变长度的 `Str_*`

#### 4.1.1 概念说明

wrapper 要同时满足两个互相矛盾的需求：

1. **BD 侧**：端口必须是 16 组固定名字的标量（`Str00`..`Str15`），因为 IP-XACT 不支持「变长数组端口」。
2. **实现侧**：`psi_ms_daq_axi` 想要一个长度恰好为 `Streams_g` 的数组端口，多余的路它不要。

解法是**两套数组信号 + 一次投影**：

- `All_*`（All = 全部 16 路）：固定 16 路的「中转仓库」，把 16 组标量端口先收纳进来。
- `Str_*`（Str = 实际使用的流）：长度由 `Streams_g` 决定的「交付数组」，只把前 `Streams_g` 路交给实现。

这样端口收集（16 路、固定）与交付（Streams_g 路、可变）就解耦了。

#### 4.1.2 核心流程

```
┌─────────────┐   逐行赋值      ┌──────────────┐  generate 投影  ┌────────────┐  port map  ┌──────────┐
│ Str00..Str15│ ──────────────▶ │  All_*(0..15)│ ──────────────▶ │ Str_*(0..  │ ─────────▶ │  i_impl  │
│  (16组标量) │  (16段，4.2节)  │ 固定16路数组 │   (4.3节)       │ Streams_g-1)│            │psi_ms_daq│
└─────────────┘                 └──────────────┘                 └────────────┘            │  _axi    │
                                                                                                └──────────┘
```

#### 4.1.3 源码精读

信号声明位于架构体开头：

[hdl/psi_ms_daq_vivado.vhd:369-382](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L369-L382) —— 声明两套数组信号。这段代码声明了固定 16 路的 `All_*`（带 `(others => '0')` 初值）和长度为 `Streams_g` 的 `Str_*`（无初值）。

关键的几行节选（只保留代表性的）：

```vhdl
signal All_Clk  : std_logic_vector(0 to 15)    := (others => '0');   -- 固定16路，升序 to
signal All_Data : t_aslv64(0 to 15)            := (others => (others => '0'));
signal All_Rdy  : std_logic_vector(0 to 15)    := (others => '0');
...
signal Str_Clk  : std_logic_vector(Streams_g-1 downto 0);            -- 可变长度，降序 downto
signal Str_Data : t_aslv64(Streams_g-1 downto 0);
...
signal trigger  : std_logic_vector(Streams_g-1 downto 0);
```

要点：

1. **`All_*` 全部用 `0 to 15`（升序）并带初值 `'0'`**。带初值是关键：后面 `TData` 会做「部分切片赋值」，未被写入的高位靠这个初值保持为 `0`。
2. **`Str_*` 全部用 `Streams_g-1 downto 0`（降序）且无初值**。因为它会整体交给 `i_impl`，长度由 `Streams_g` 决定，每一路都会被 `g_stream` 写满，不需要初值。
3. **方向不同（`to` vs `downto`）不影响逐元素索引**。后续 `All_Data(s)` 与 `Str_Data(s)` 用同一个下标 `s` 寻址，只要 `s` 落在 `[0, Streams_g-1]` 内，对两种方向都合法。方向差异只影响「整体赋值时的对应关系」和可读性约定。

#### 4.1.4 代码实践

**实践目标**：亲手确认两套信号的「固定 vs 可变」「升序 vs 降序」差异。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_vivado.vhd:369-382](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L369-L382)。
2. 数一下 `All_*` 系列有几个信号（答案：6 个——`All_Clk`/`All_Data`/`All_Ts`/`All_Vld`/`All_Rdy`/`All_Lst`）。
3. 数一下 `Str_*` 系列（含 `trigger`）有几个（答案：7 个——比 `All_*` 多一个 `trigger`）。

**需要观察的现象**：

- `All_*` 的范围都是字面量 `0 to 15`，与任何 generic 无关 → 固定。
- `Str_*` 的范围都含 `Streams_g` → 可变。

**预期结果**：你会清楚地看到「固定中转层 + 可变交付层」的设计意图。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `All_Data` 元素固定为（至少）64 位宽，而端口 `Str00_TData` 却可以是 8～64 位（由 `Stream0Width_g` 决定）？

> **参考答案**：`All_Data` 要用一个统一类型 `t_aslv64` 来容纳所有 16 路（每路宽度可能不同），所以取最宽的 64 位作为「公共容器」。具体某一路只有 `StreamNWidth_g` 位是有效数据，高位留给初值 `0` 填充。下一节会看到这个填充是怎么做的。

**练习 2**：为什么 `All_Rdy` 要初始化为 `'0'` 而不是 `'1'`？

> **参考答案**：`All_Rdy` 是 AXI-Stream 的 `TReady`（下游告诉上游「我准备好接收了」）。若初始化为 `'1'`，在被实际驱动之前会「假装准备好」，可能让上游在下游未真正就绪时就送出数据，造成数据丢失或伪握手。初始化为 `'0'`（未就绪）是安全默认值。

---

### 4.2 `Str00..Str15` → `All_*` 的逐行硬连线

#### 4.2.1 概念说明

16 组标量端口必须**一行一行**地连到 `All_*` 数组的对应元素。为什么不能用 `for` 循环？因为：

- 每路端口**名字不同**（`Str00_Clk`、`Str01_Clk`、…），VHDL 综合期循环的下标 `s` 不能拼进端口名。
- 每路 `TData` **位宽不同**（`Stream0Width_g`、`Stream1Width_g`、…），无法用统一表达式切片。

所以只能手写 16 段、每段 6 行。这是 wrapper 最「枯燥」但最直白的代码。

#### 4.2.2 核心流程

每一段（以第 `i` 路为例）做 6 件事，对应 AXI-Stream 的 6 类信号：

```
All_Clk(i)  <= StrNN_Clk                          ;  -- 时钟
All_Data(i)(StreamNWidth_g-1 downto 0) <= StrNN_TData ;  -- 数据（部分切片！）
All_Ts(i)   <= StrNN_Ts                           ;  -- 时间戳（整体赋值，64位）
All_Vld(i)  <= StrNN_TValid                       ;  -- 有效
StrNN_TReady <= All_Rdy(i)                        ;  -- 就绪（注意方向反转：从 All_Rdy 读出）
All_Lst(i)  <= StrNN_TLast                        ;  -- 末尾标志
```

#### 4.2.3 源码精读

先看第 0 路（`Str00`）这一段，作为模板：

[hdl/psi_ms_daq_vivado.vhd:419-424](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L419-L424) —— 第 0 路端口 `Str00_*` 到 `All_*(0)` 的 6 行连线。

```vhdl
All_Clk(0)                              <= Str00_Clk;
All_Data(0)(Stream0Width_g-1 downto 0)  <= Str00_TData;   -- 关键：部分切片赋值
All_Ts(0)                               <= Str00_Ts;
All_Vld(0)                              <= Str00_TValid;
Str00_TReady                            <= All_Rdy(0);     -- 方向反转
All_Lst(0)                              <= Str00_TLast;
```

其余 15 段结构完全相同，只是下标 `0→15`、端口名 `Str00→Str15`、宽度泛型 `Stream0Width_g→Stream15Width_g` 递增。例如第 5 路：

[hdl/psi_ms_daq_vivado.vhd:461-466](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L461-L466) —— 第 5 路端口 `Str05_*` 到 `All_*(5)` 的 6 行连线（与第 0 路同构，仅下标/名字/宽度泛型不同）。

完整 16 段从第 419 行一直到第 529 行（`Str15` 段）：

[hdl/psi_ms_daq_vivado.vhd:524-529](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L524-L529) —— 第 15 路（最后一路）`Str15_*` 到 `All_*(15)` 的连线。

两个**必须看懂**的细节：

1. **部分切片赋值（partial assignment）**：`All_Data(0)(Stream0Width_g-1 downto 0) <= Str00_TData`。`All_Data(0)` 是 64 位容器，但 `Str00_TData` 只有 `Stream0Width_g` 位（例如 16 位）。这行只把低 `Stream0Width_g` 位写进去，高位于保持声明时的初值 `'0'`。这就是 [4.1.5 练习 1](#415-小练习与答案) 里说的「公共容器 + 高位填零」。也正因如此，`All_Data` **必须**带初值，否则这些高位是未定义的（综合期会报错或产生锁存）。
2. **方向反转**：`Str00_TReady <= All_Rdy(0)`。其余 5 行都是从端口**读入**到 `All_*`，唯独 `TReady` 是从 `All_Rdy(i)` **读出**到端口（因为 `TReady` 是 `out` 方向——下游告诉上游就绪）。这是 AXI-Stream 握手的天然方向：`TValid/TData/TLast` 由源端驱动，`TReady` 由宿端驱动。

#### 4.2.4 代码实践

**实践目标**：验证「部分切片赋值」如何把异宽端口装进 64 位容器。

**操作步骤**：

1. 假设 `Stream5Width_g = 16`，盯住 [hdl/psi_ms_daq_vivado.vhd:462](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L462) 这一行：`All_Data(5)(Stream5Width_g-1 downto 0) <= Str05_TData`。
2. 代入 `Stream5Width_g = 16`，它等价于 `All_Data(5)(15 downto 0) <= Str05_TData`。

**需要观察的现象 / 预期结果**：

- `All_Data(5)` 是 64 位，其中第 `[15 downto 0]` 位是 `Str05_TData` 的有效数据。
- 第 `[63 downto 16]` 位没有被这行赋值，靠 [hdl/psi_ms_daq_vivado.vhd:371](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L371) 声明时的 `(others => (others => '0'))` 保持为 `0`。
- 由于上游实现 `psi_ms_daq_axi` 知道这一路的真实宽度（通过 `StreamWidth_g` 数组传入，见 [u2-l3](u2-l3-instantiating-impl.md)），它只会读低 16 位，高位填零不影响功能。

> 本地无 Vivado 仿真环境时，这是一个「源码阅读型实践」，结论可通过阅读直接得出，无需运行。若你在 Vivado 中打开综合后的原理图（待本地验证），应能看到 `Str05_TData[15:0]` 驱动到内部总线的低 16 位、高位接常数 0。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TData` 用部分切片赋值，而 `Ts` / `TValid` / `TLast` 不用？

> **参考答案**：`StrNN_Ts` 本身就是 64 位（端口声明见 [u2-l1](u2-l1-wrapper-entity-generics-ports.md)），与 `All_Ts(i)` 的元素宽度完全一致，所以整体赋值即可；`TValid`/`TLast` 是单 bit，与 `All_Vld(i)`/`All_Lst(i)` 的单 bit 一致。只有 `TData` 是「每路宽度不同」的，才需要切片对齐到固定 64 位容器。

**练习 2**：如果把 `Str00_TReady <= All_Rdy(0)` 这行删掉，会发生什么？

> **参考答案**：`Str00_TReady` 是 `out` 端口，不连就等于悬空（或综合默认值）。AXI-Stream 源端（外部数据发生器）会一直看到 `TReady` 无效或未知，从而永不发送数据，第 0 路流完全采集不到——典型的「握手死锁」。

---

### 4.3 `g_stream` 循环：投影到 `Streams_g` 路 + 时间戳条件选择

#### 4.3.1 概念说明

`All_*` 收满了 16 路，但实现 `i_impl` 只要前 `Streams_g` 路。这一步用一个 `for … generate` 循环把 `All_*` 的前 `Streams_g` 个元素**投影**到 `Str_*`。

同时，时间戳 `Str_Ts` 的来源有一个**二选一**的开关 `TsPerStream_g`：

- `TsPerStream_g = true`：每路流自带独立时间戳 → `Str_Ts(s) <= All_Ts(s)`（`All_Ts(s)` 又来自该路的 `StrNN_Ts`）。
- `TsPerStream_g = false`：所有流共用一个外部时间戳端口 `StrX_Ts` → `Str_Ts(s) <= StrX_Ts`。

这两个分支用**互斥的 `if generate`** 实现：`g_tsstr`（条件为真）和 `g_ntsstr`（条件为假，`n` 表示 not）。由于 `TsPerStream_g` 是 boolean，两段必有一段、且只有一段被综合出来。

#### 4.3.2 核心流程

`g_stream` 循环每一轮（下标 `s` 从 `0` 到 `Streams_g-1`）做这些事：

```
Str_Data(s) <= All_Data(s)        # 数据投影
Str_Vld(s)  <= All_Vld(s)         # 有效投影
All_Rdy(s)  <= Str_Rdy(s)         # 就绪反投影（注意方向：实现→All_*→端口）
Str_Lst(s)  <= All_Lst(s)         # 末尾投影

# 时间戳二选一（互斥 if generate）：
if TsPerStream_g   :  Str_Ts(s) <= All_Ts(s)   # g_tsstr
if not TsPerStream_g: Str_Ts(s) <= StrX_Ts      # g_ntsstr

Str_Clk(s)  <= All_Clk(s)         # 时钟投影
```

循环结束后，`All_*(Streams_g .. 15)` 这些多余元素**虽然存在但不被任何下游引用**，综合工具会优化掉它们（它们有初值、无驱动冲突，不会报错）。

#### 4.3.3 源码精读

[hdl/psi_ms_daq_vivado.vhd:531-545](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L531-L545) —— `g_stream` 循环及其内部嵌套的时间戳条件选择。

```vhdl
g_stream : for s in 0 to Streams_g-1 generate
    Str_Data(s) <= All_Data(s);
    Str_Vld(s)  <= All_Vld(s);
    All_Rdy(s)  <= Str_Rdy(s);          -- 方向反转
    Str_Lst(s)  <= All_Lst(s);

    g_tsstr : if TsPerStream_g generate
        Str_Ts(s) <= All_Ts(s);         -- 各流自带时间戳
    end generate;
    g_ntsstr : if not TsPerStream_g generate
        Str_Ts(s) <= StrX_Ts;           -- 共用外部时间戳端口
    end generate;

    Str_Clk(s) <= All_Clk(s);
end generate;
```

要点：

1. **循环方向 `0 to Streams_g-1`**（`to`，升序）与 `All_*` 的 `0 to 15` 方向一致，下标 `s` 直接索引 `All_*(s)` 不会错位。`Str_*` 虽然是 `downto` 方向，但用同一个 `s` 索引单个元素同样合法（见 [4.1.3](#413-源码精读) 要点 3）。
2. **`All_Rdy(s) <= Str_Rdy(s)` 方向反转**。和 [4.2.3](#423-源码精读) 的 `TReady` 一样，`Str_Rdy` 由下游实现驱动，要回灌给 `All_Rdy`，再由 `All_Rdy(i)` 回传到端口 `StrNN_TReady`。所以 `TReady` 的完整回链是：`i_impl.Str_Rdy(s) → All_Rdy(s) → StrNN_TReady`。
3. **`StrX_Ts` 是 entity 的单一外部端口**，64 位，所有流共用。声明在 [hdl/psi_ms_daq_vivado.vhd:248](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L248)：`StrX_Ts : in std_logic_vector(63 downto 0)`。当 `TsPerStream_g = false` 时，循环里每一轮都把同一个 `StrX_Ts` 赋给 `Str_Ts(s)`，即所有流拿同一时间戳。
4. **互斥 `if generate`**。`g_tsstr` 条件是 `TsPerStream_g`，`g_ntsstr` 条件是 `not TsPerStream_g`。两者必然一真一假，所以综合后每轮循环里 `Str_Ts(s)` 恰好被驱动一次，不会有「多驱动」冲突。

#### 4.3.4 代码实践

**实践目标**：确认 `g_stream` 只投影前 `Streams_g` 路，多余路被丢弃。

**操作步骤**：

1. 假设 `Streams_g = 3`，阅读 [hdl/psi_ms_daq_vivado.vhd:531](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L531) 的循环范围 `for s in 0 to Streams_g-1`。
2. 代入 `Streams_g = 3` 得 `for s in 0 to 2`，即循环体被复制 3 份：`s = 0, 1, 2`。

**需要观察的现象 / 预期结果**：

- 综合后只存在 `Str_Data(0/1/2)`、`All_Rdy(0/1/2)` 等连线，`s = 3..15` 的连线不存在。
- `All_Data(3) .. All_Data(15)` 没有任何下游读取 → 综合工具把它们连同对应的端口 `Str03_TData .. Str15_TData` 一起优化掉（前提是 `package.tcl` 也用端口使能条件隐藏了这些端口，见 [u2-l1](u2-l1-wrapper-entity-generics-ports.md)）。
- 因此 `Streams_g = 3` 时，BD 里只暴露 `Str00/Str01/Str02` 三组流端口。

> 待本地验证：在 Vivado 中打开 `Streams_g = 3` 的 IP，确认 GUI 只显示 3 路流端口；多余端口不在 BD 接口列表中。

#### 4.3.5 小练习与答案

**练习 1**：`g_stream` 循环范围为什么是 `0 to Streams_g-1`，而不是和 `Str_*` 一样的 `Streams_g-1 downto 0`？

> **参考答案**：循环方向（`to` vs `downto`）只决定 `s` 取值的「书写顺序」，对每轮内部 `All_*(s)` / `Str_*(s)` 的单元素索引没有影响。作者选 `0 to Streams_g-1` 是为了与 `All_*` 的 `0 to 15` 升序风格保持一致，便于阅读。两种写法综合结果完全等价。

**练习 2**：当 `TsPerStream_g = false` 时，循环里每一轮都执行 `Str_Ts(s) <= StrX_Ts`，会不会造成 `StrX_Ts` 一个信号被「多次驱动」？

> **参考答案**：不会。被多次驱动的是 `Str_Ts(s)` 的不同元素（`s` 不同），而 `StrX_Ts` 是**被读取**的源（一个源可以被多个下游读，这是合法扇出）。每一轮驱动的是不同的 `Str_Ts(s)`，所以 `Str_Ts` 数组的每个元素只被驱动一次。

---

### 4.4 触发源条件选择：`g_trig` / `g_ntrig`

#### 4.4.1 概念说明

`trigger` 信号（长度 `Streams_g`）告诉实现「这一路流的这一拍是不是触发点」。它的来源由 `UseLastAsTrigger_g` 决定：

- `UseLastAsTrigger_g = true`：把 AXI-Stream 的 `TLast`（一帧的最后一拍）当作触发 → `trigger <= Str_Lst`。
- `UseLastAsTrigger_g = false`：用外部专用触发端口 `Trig` → `trigger <= Trig`。

和上一节的时间戳一样，用两个**互斥**的顶层 `if generate` 实现：`g_trig`（条件为真）和 `g_ntrig`（条件为假）。

#### 4.4.2 核心流程

```
if UseLastAsTrigger_g      :  trigger <= Str_Lst   # g_trig ：用 TLast 当触发
if not UseLastAsTrigger_g  :  trigger <= Trig       # g_ntrig：用外部 Trig 端口
```

`Str_Lst` 的来源链（把前几节串起来）：

```
StrNN_TLast（端口，第i路） ─4.2节逐行赋值─▶ All_Lst(i) ─4.3节 g_stream─▶ Str_Lst(s) ─本节 g_trig─▶ trigger(s)
```

即当 `UseLastAsTrigger_g = true` 时，每路流的 `TLast` 直接成为该路的触发信号。

#### 4.4.3 源码精读

[hdl/psi_ms_daq_vivado.vhd:547-552](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L547-L552) —— 顶层两个互斥 `if generate`，选择 `trigger` 来源。

```vhdl
g_trig  : if UseLastAsTrigger_g generate
    trigger <= Str_Lst;       -- 用 AXI-Stream 的 TLast 当触发
end generate;
g_ntrig : if not UseLastAsTrigger_g generate
    trigger <= Trig;          -- 用外部 Trig 端口
end generate;
```

两个相关端口的声明（来自 entity，[u2-l1](u2-l1-wrapper-entity-generics-ports.md) 已讲过，这里只引行号佐证）：

- [hdl/psi_ms_daq_vivado.vhd:247](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L247)：`Trig : in std_logic_vector(Streams_g-1 downto 0)` —— 外部触发端口，宽度与 `trigger` 完全一致，可直接整体赋值。
- `Str_Lst` 由 [hdl/psi_ms_daq_vivado.vhd:535](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L535) 在 `g_stream` 里从 `All_Lst` 投影而来，宽度同为 `Streams_g-1 downto 0`。

要点：

1. **互斥保证无冲突**。`UseLastAsTrigger_g` 是 boolean，`g_trig` 与 `g_ntrig` 必有一段生效、仅一段生效，`trigger` 恰好被驱动一次。
2. **`trigger <= Str_Lst` 而非 `trigger <= All_Lst`**。作者刻意走「`Str_*`」这一层，让 `Str_*` 成为通往 `i_impl` 的**统一入口契约**：所有进入实现的信号都先汇入 `Str_*`/`trigger`，再统一 `port map`。这样 `i_impl` 的 port map（下一讲）只需要看 `Str_*`，不必关心它们来自 `All_*` 还是外部端口。
3. **`Trig` 端口的可见性**。当 `UseLastAsTrigger_g = true` 时，`g_ntrig` 不生效，`Trig` 端口没有任何下游。此时 `package.tcl` 会用端口使能条件把 `Trig` 端口**隐藏**（不暴露到 BD），这是 [u2-l1](u2-l1-wrapper-entity-generics-ports.md) 讲过的 GUI/端口使能机制。反过来，`UseLastAsTrigger_g = false` 时 `Trig` 才出现在 BD 里。

#### 4.4.4 代码实践（本讲指定实践任务）

**实践目标**：在三种开关组合下，画出 `Str_Ts` 与 `trigger` 的来源。本讲规格指定的组合是：

> `TsPerStream_g = false`、`UseLastAsTrigger_g = true`、`Streams_g = 3`。

**操作步骤与推理**：

1. **`Str_Ts` 的来源**：因 `TsPerStream_g = false`，`g_ntsstr` 生效（[hdl/psi_ms_daq_vivado.vhd:540-542](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L540-L542)），循环每一轮执行 `Str_Ts(s) <= StrX_Ts`。
   - 结论：`Str_Ts(0)`、`Str_Ts(1)`、`Str_Ts(2)` **全部连到同一个外部端口 `StrX_Ts`**（三路共用一个时间戳）。
   - `All_Ts(*)` 在此模式下**不被 `g_stream` 读取**（虽然它仍被 [4.2 节](#42-str00str15--all_-的逐行硬连线) 的 `All_Ts(i) <= StrNN_Ts` 填满，但没有下游，会被优化）。

2. **`trigger` 的来源**：因 `UseLastAsTrigger_g = true`，`g_trig` 生效（[hdl/psi_ms_daq_vivado.vivado.vhd:547-549](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L547-L549)），执行 `trigger <= Str_Lst`。`Str_Lst` 又来自 `g_stream` 的 `Str_Lst(s) <= All_Lst(s)`，`All_Lst(s)` 来自 `StrNN_TLast`。
   - 结论：
     - `trigger(0)` ← `Str_Lst(0)` ← `All_Lst(0)` ← `Str00_TLast`
     - `trigger(1)` ← `Str_Lst(1)` ← `All_Lst(1)` ← `Str01_TLast`
     - `trigger(2)` ← `Str_Lst(2)` ← `All_Lst(2)` ← `Str02_TLast`
   - 外部 `Trig` 端口在此模式下**不被读取**，且被 `package.tcl` 隐藏，BD 里看不到它。

**预期结果（一张表）**：

| 信号 | 在指定开关下连到的源 |
|---|---|
| `Str_Ts(0)` | `StrX_Ts`（外部共用时间戳端口） |
| `Str_Ts(1)` | `StrX_Ts` |
| `Str_Ts(2)` | `StrX_Ts` |
| `trigger(0)` | `Str00_TLast`（经 `All_Lst(0)` → `Str_Lst(0)`） |
| `trigger(1)` | `Str01_TLast`（经 `All_Lst(1)` → `Str_Lst(1)`） |
| `trigger(2)` | `Str02_TLast`（经 `All_Lst(2)` → `Str_Lst(2)`） |

> 这是一个「源码阅读型实践」，结论可由本节和 [4.3 节](#43-g_stream-循环投影到-streams_g-路--时间戳条件选择) 的代码直接推出，无需运行。若要在 Vivado 中确认（待本地验证），可在 IP 参数里设 `UseLastAsTrigger_g = true`，观察 BD 接口里 `Trig` 端口消失、各路流端口里仍保留 `TLast`。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `g_trig` 改成 `trigger <= All_Lst`（跳过 `Str_Lst` 这一层），功能上对吗？

> **参考答案**：功能上**可能**等价（`Str_Lst` 就是从 `All_Lst` 投影来的），但破坏了「所有进入 `i_impl` 的信号都先汇入 `Str_*`/`trigger`」的统一契约，让 `trigger` 与 `Str_Lst` 的来源不一致，可读性变差。此外，`All_Lst` 是 16 路而 `trigger` 是 `Streams_g` 路，整体赋值时长度不匹配（除非正好 `Streams_g = 16`），会直接报错。所以原写法 `trigger <= Str_Lst`（长度一致）既正确又规整。

**练习 2**：`UseLastAsTrigger_g = false` 且 `TsPerStream_g = true` 时，`StrX_Ts` 和 `StrNN_TLast` 各自还有下游吗？

> **参考答案**：`UseLastAsTrigger_g = false` → `g_ntrig` 生效 → `trigger <= Trig`，此时 `Str_Lst` 不再驱动 `trigger`，但 `Str_Lst` 仍会被 `g_stream` 投影出来（只是没人用），最终 `StrNN_TLast` 没有有效下游，会被优化/隐藏。`TsPerStream_g = true` → `g_tsstr` 生效 → `Str_Ts(s) <= All_Ts(s)`，此时用不到 `StrX_Ts`，所以 `StrX_Ts` 没有下游，同样会被 `package.tcl` 的端口使能条件隐藏。这两个端口的「出现与否」完全由这两个 generic 决定。

---

## 5. 综合实践

**任务**：填写下面这张「开关组合 → 信号来源」总表，把本讲 4 个模块的知识串起来。

对每一组开关，写出 `Str_Ts(s)` 和 `trigger(s)`（以 `s = 0` 为例）最终连到哪个 entity 端口，并画出完整信号通路。

| 组合 | `Streams_g` | `TsPerStream_g` | `UseLastAsTrigger_g` | `Str_Ts(0)` 来源 | `trigger(0)` 来源 |
|---|---|---|---|---|---|
| A | 3 | false | true | ? | ? |
| B | 3 | true | false | ? | ? |
| C | 1 | false | false | ? | ? |

**参考答案**：

- **组合 A**（即 4.4.4 的指定任务）：
  - `Str_Ts(0)` ← `StrX_Ts`（经 `g_ntsstr`）。
  - `trigger(0)` ← `Str00_TLast`（经 `All_Lst(0)` → `Str_Lst(0)` → `g_trig`）。
- **组合 B**：
  - `Str_Ts(0)` ← `Str00_Ts`（经 `All_Ts(0)` → `g_tsstr`，即各流自带时间戳）。
  - `trigger(0)` ← `Trig(0)`（经 `g_ntrig`，外部触发端口）。
- **组合 C**（单路）：
  - `Str_Ts(0)` ← `StrX_Ts`（经 `g_ntsstr`）。
  - `trigger(0)` ← `Trig(0)`（经 `g_ntrig`）。

**完整通路图（以组合 A 的 `trigger(0)` 为例）**：

```
Str00_TLast ─▶ All_Lst(0) ─▶ Str_Lst(0) ─▶ trigger(0) ─▶ i_impl.Str_Trig(0)
 [4.2 逐行]     [4.1 声明]    [4.3 g_stream]  [4.4 g_trig]   [下一讲 u2-l3]
```

做完后，你应该能体会到 wrapper 的「三层重排」骨架：**端口收集（4.2）→ 投影裁剪（4.3）→ 条件选择（4.4）**，最后统一交给 `i_impl`。

## 6. 本讲小结

- wrapper 架构体用**两套数组信号**解耦了「BD 固定 16 路标量端口」与「实现需要 `Streams_g` 路数组端口」的矛盾：固定 16 路的 `All_*` 负责收，可变长度的 `Str_*` 负责交付。
- 16 段**逐行硬连线**（[419–529 行](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L419-L529)）把 `Str00..Str15` 灌进 `All_*`；其中 `TData` 用**部分切片赋值**把异宽端口装进 64 位容器，`TReady` 方向反转回传。
- `g_stream` 这个 `for … generate`（[531–545 行](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L531-L545)）把前 `Streams_g` 路从 `All_*` 投影到 `Str_*`，多余路被综合优化掉。
- 时间戳来源由**互斥 `if generate`** `g_tsstr`/`g_ntsstr` 按 `TsPerStream_g` 选择：各流自带（`All_Ts`）或共用外部 `StrX_Ts`。
- 触发来源由**互斥 `if generate`** `g_trig`/`g_ntrig` 按 `UseLastAsTrigger_g` 选择：`Str_Lst`（即 `TLast`）或外部 `Trig` 端口；后者在 `UseLastAsTrigger_g = true` 时被 `package.tcl` 隐藏。
- 所有进入 `i_impl` 的信号都先汇入 `Str_*`/`trigger`，形成统一的 `port map` 入口契约——这是下一讲 [u2-l3](u2-l3-instantiating-impl.md) 的起点。

## 7. 下一步学习建议

- 本讲只讲了「信号怎么重排」，但还没讲这些 `Str_*`/`trigger` 信号如何 `port map` 进 `i_impl`，也没讲那几个 `constant`/`function`（`TimeoutUs_c`、`TimeoutsSec_c`、`FreqHz_c`、`FreqReal_c`）如何把逐流标量泛型聚合成数组型 generic。这正是下一讲 [u2-l3：例化 psi_ms_daq_axi 与配置转换](u2-l3-instantiating-impl.md) 的内容。
- 建议同时打开 [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl) 对照阅读 `add_port_enablement_condition` 段落，确认本讲反复提到的「`Trig`/`StrX_Ts`/`StrNN_Ts` 端口可见性由 generic 控制」在打包脚本里是如何实现的（这部分在 [u2-l1](u2-l1-wrapper-entity-generics-ports.md) 已建立心智模型，本讲是其 RTL 侧的对应）。
- 如果你想真正在波形里看到 `All_*` 与 `Str_*` 的关系，需要到上游 `psi_multi_stream_daq` 仓库找 `psi_ms_daq_vivado` 的 testbench（本仓库不含仿真），这属于 [u1-l3](u1-l3-dependencies-and-sources.md) 讲过的「上游依赖」范畴。
