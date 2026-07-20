# 公共类型与记录包 psi_ms_daq_pkg

## 1. 本讲目标

本讲是进入 VHDL 源码细节的**第一站**。学完后你应当能够：

- 说出 `psi_ms_daq_pkg` 这个包在整个 IP 核中扮演的「公共字典」角色。
- 读懂包里定义的**上限常量**（`MaxStreams_c`、`MaxWindows_c`、`MaxStreamWidth_c` 等）与**记录模式常量**（`RecMode_*_c`），并理解它们如何约束整个设计的取值范围。
- 看懂模块之间通信用的 **record 类型**：`Input2Daq_Data_t`、`DaqSm2DaqDma_Cmd_t`、`DaqDma2DaqSm_Resp_t`，以及上下文访问用的 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t`。
- 理解为什么这些 record 需要 `ToStdlv` / `FromStdlv` 转换函数，并能手工推出字段在 `std_logic_vector` 里的**位拼接顺序**。
- 解释 `CtxStr_Sel_*` 三个选择值如何用 2 个比特区分一次上下文访问到底读/写的是哪 64 位。

本讲几乎不涉及时序和算法，全部是**类型与布局**的知识。它是后续 u2-l2（输入逻辑）、u3（控制状态机与上下文存储）的必备词汇表。

## 2. 前置知识

在开始前，请确认你已了解以下概念（u1-l1 ~ u1-l4 已建立）：

- **VHDL**：本 IP 核的硬件描述语言。本讲会频繁出现 `record`（记录类型）、`subtype`（子类型）、`constant`（常量）、`array`（数组）、`function`（函数）等 VHDL 关键字。
- **IP 核的五大子模块**（来自 u1-l3）：`i_reg`（寄存器接口）、`g_input`（每路输入逻辑）、`i_statemachine`（控制状态机）、`i_dma`（DMA 引擎）、`i_memif`（AXI 主接口）。本讲解的就是这五个模块互相「说话」时用的**公共语言**。
- **record 与 std_logic_vector 的区别**：record 是带名字字段的复合类型（如 `rec.Address`、`rec.Stream`），可读性好；`std_logic_vector` 是一串没有内部结构的比特。FIFO 和 RAM 只认后者，所以两者之间必须来回转换。
- **数据流总览**（来自 u1-l3）：`输入 → DMA → AXI Master → 内存`，控制流是 `AXI Slave → 寄存器 → 状态机`。

如果你对 VHDL 的 record 语法完全不熟悉，只需记住一点：record 就是「带标签的几个信号打包在一起」，类似 C 语言的结构体 `struct`。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它是整个 IP 核的「共享头文件」：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `hdl/psi_ms_daq_pkg.vhd` | 全局包：常量、子类型、模块间通信 record、上下文访问 record、`ToStdlv`/`FromStdlv` 转换函数 | 全部内容 |

这个包被几乎所有其它 `.vhd` 文件 `use` 引用，类似于 C 项目里的 `common.h`。任何想读懂 `input`、`daq_sm`、`daq_dma`、`reg_axi` 的人，都必须先读懂这个包。

> 小贴士：本讲引用的「上下文访问」用法出现在状态机里，例如 [`hdl/psi_ms_daq_daq_sm.vhd` 的 `ReadCtxStr_s` 状态](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L339-L373)；转换函数被 DMA 引擎的 FIFO 调用，例如 [`hdl/psi_ms_daq_daq_dma.vhd` 的命令/响应 FIFO](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L304-L344)。本讲会在用到时给出链接。

## 4. 核心概念与源码讲解

### 4.1 全局常量、子类型与记录模式常量

#### 4.1.1 概念说明

一个可参数化的 IP 核有很多「上限」：最多支持几路流、最多几个窗口、流最宽多少位。这些上限不能散落在各个文件里，否则改一处忘一处就会出 bug。`psi_ms_daq_pkg` 把它们集中定义为**常量**（`constant`），全设计共用。

除了上限，包里还定义了**记录模式**（Recording Mode）的编码常量。记录模式决定一路流「如何触发、何时停止采集」，是 u2-l3 的核心主题，但它的**编码值**定义在本包里。

最后，包里还用 `subtype` 把一些常用的 `std_logic_vector` 宽度起了别名，让端口声明更可读。

#### 4.1.2 核心流程

常量与子类型的组织逻辑是：

1. 定义**能力上限**（`MaxStreams_c`、`MaxWindows_c`、`MaxStreamWidth_c`）。
2. 用 `log2ceil` 从上限**派生**出所需的地址/编码位宽（`MaxStreamsBits_c`、`MaxWindowsBits_c`）。这样改上限时，位宽自动跟着变。
3. 定义**记录模式编码**（`RecMode_*_c`），用 `to_unsigned` 把整数 0/1/2/3 转成 2 位向量。
4. 定义**窗口号子类型** `WinType_t` 和它的数组形式 `WinType_a`，供需要「一串窗口号」的地方使用。

派生关系可以写成：

\[
\text{MaxStreamsBits\_c} = \lceil \log_2(\text{MaxStreams\_c}) \rceil
\]

当 `MaxStreams_c = 32` 时，`log2ceil(32) = 5`，所以流号编码占 5 位（取值 0–31）。

#### 4.1.3 源码精读

包头的常量与子类型定义在这里：

[hdl/psi_ms_daq_pkg.vhd:20-35 — 包头：上限常量、派生位宽、记录模式编码、窗口号子类型](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L20-L35)

关键几行（只保留要点）：

```vhdl
constant MaxStreams_c     : integer := 32;
constant MaxWindows_c     : integer := 32;
constant MaxStreamsBits_c : integer := log2ceil(MaxStreams_c);   -- = 5
constant MaxWindowsBits_c : integer := log2ceil(MaxWindows_c);   -- = 5
constant MaxStreamWidth_c : integer := 64;

subtype RecMode_t is std_logic_vector(1 downto 0);
constant RecMode_Continuous_c  : RecMode_t := ... -- "00"
constant RecMode_TriggerMask_c : RecMode_t := ... -- "01"
constant RecMode_SingleShot_c  : RecMode_t := ... -- "10"
constant RecMode_ManuelMode_c  : RecMode_t := ... -- "11"
```

要点解读：

- `log2ceil` 来自依赖库 `psi_common_math_pkg`（包在第 15 行 `use` 了它），实现「向上取整的对数」。
- `RecMode_t` 是 2 位向量子类型，正好编码 4 种模式。后续 u2-l3 会讲解这 4 种模式的触发行为。
- `WinType_t` 宽度等于 `MaxWindowsBits_c`（5 位），`WinType_a` 是它的数组，用于「一次声明多个窗口号」。
- 注意 `RecMode_ManuelMode_c` 里的 `Manuel` 是源码原样拼写（实际是 Manual 的拼写错误），引用时请用包里的常量名而不是自己手写，避免拼错。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍「上限 → 位宽」的派生关系，确认 `MaxStreamsBits_c` 的来历。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_pkg.vhd:22-25](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L22-L25)。
2. 确认 `MaxStreams_c = 32`、`MaxWindows_c = 32`。
3. 手算 `log2ceil(32)`：因为 \(2^5 = 32\)，所以结果是 5。
4. 假想把 `MaxStreams_c` 改成 33，再算 `log2ceil(33)`：因为 \(2^5 = 32 < 33\)，需要 6 位，结果是 6。

**需要观察的现象**：所有用到「流号位宽」的地方（例如 4.2 节里 `DaqSm2DaqDma_Cmd_Size_c` 的计算）都引用 `MaxStreamsBits_c` 这个派生常量，而不是硬编码 5。

**预期结果**：你能解释「为什么改 `MaxStreams_c` 一处，全设计的流号位宽都会自动跟着变」——这就是把派生关系写进包里的价值。

#### 4.1.5 小练习与答案

**练习 1**：如果要把最多流数从 32 提到 48，`MaxStreamsBits_c` 会变成多少？

**答案**：`log2ceil(48)`。因为 \(2^5 = 32 < 48 \le 2^6 = 64\)，所以结果是 6。

**练习 2**：`RecMode_t` 是 2 位，最多能编码几种模式？目前已用掉几种？

**答案**：2 位可编码 \(2^2 = 4\) 种（0–3），目前已用掉全部 4 种（Continuous、TriggerMask、SingleShot、ManuelMode）。若要新增第 5 种模式，必须先把 `RecMode_t` 加宽。

---

### 4.2 模块间接口记录：Cmd / Resp / Input2Daq

#### 4.2.1 概念说明

IP 核的五个子模块之间需要传递结构化的信息。例如：

- **控制状态机 → DMA 引擎**：「请去流 N 的地址 A 处，最多搬 M 个字节」——这是一条**命令**。
- **DMA 引擎 → 控制状态机**：「这次实际搬了 S 个字节，并且末尾触发了」——这是一条**响应**。
- **输入逻辑 → DMA 引擎**：「这是一拍 64 位数据，附带字节有效、是否触发、是否帧末」——这是**数据流**。

如果用一根根零散的 `std_logic_vector` 连线，端口列表会又长又难读。VHDL 的 `record` 把相关字段打包成一个命名类型，端口只声明一个 record 即可，可读性大大提升。

本包定义了三个核心接口 record：

- `DaqSm2DaqDma_Cmd_t`：状态机发给 DMA 的命令。
- `DaqDma2DaqSm_Resp_t`：DMA 回给状态机的响应。
- `Input2Daq_Data_t`：输入逻辑送给 DMA 的数据（含元信息）。

#### 4.2.2 核心流程

三个 record 的字段对应关系如下（先看字段含义，4.3 节再看比特布局）：

| record | 方向 | 字段 | 含义 |
| --- | --- | --- | --- |
| `DaqSm2DaqDma_Cmd_t` | 状态机 → DMA | `Address`(32) | DMA 写入内存的目标地址 |
| | | `MaxSize`(16) | 本次最多搬多少字节 |
| | | `Stream` | 目标流号（0–31） |
| `DaqDma2DaqSm_Resp_t` | DMA → 状态机 | `Size`(16) | 实际搬了多少字节 |
| | | `Trigger`(1) | 本次传输是否以触发结束 |
| | | `Stream` | 来自哪个流号 |
| `Input2Daq_Data_t` | 输入 → DMA | `Last`(1) | 是否帧末 |
| | | `Data`(动态) | 一拍数据（宽度由 `IntDataWidth_g` 决定） |
| | | `Bytes`(动态) | 这拍有几个有效字节 |
| | | `IsTo`(1) | 这拍是否由超时冲刷产生 |
| | | `IsTrig`(1) | 这拍是否含触发 |

注意 `Input2Daq_Data_t` 的 `Data` 和 `Bytes` 字段在包里**没有写死宽度**（声明为无范围约束的 `std_logic_vector`），宽度在使用处才确定。这是为了配合 `IntDataWidth_g` 可参数化（详见 4.2.3）。

#### 4.2.3 源码精读

命令与响应 record 定义在这里：

[hdl/psi_ms_daq_pkg.vhd:46-62 — DaqSm2DaqDma_Cmd_t 与 DaqDma2DaqSm_Resp_t 两个接口记录](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L46-L62)

```vhdl
type DaqSm2DaqDma_Cmd_t is record
  Address : std_logic_vector(31 downto 0);
  MaxSize : std_logic_vector(15 downto 0);
  Stream  : integer range 0 to MaxStreams_c - 1;
end record;
constant DaqSm2DaqDma_Cmd_Size_c : integer := 32 + 16 + MaxStreamsBits_c;

type DaqDma2DaqSm_Resp_t is record
  Size    : std_logic_vector(15 downto 0);
  Trigger : std_logic;
  Stream  : integer range 0 to MaxStreams_c - 1;
end record;
constant DaqDma2DaqSm_Resp_Size_c : integer := 16 + 1 + MaxStreamsBits_c;
```

要点：

- `Stream` 字段类型是 `integer range 0 to MaxStreams_c - 1`，这是一个**带范围约束的整数**，综合后正好占 `MaxStreamsBits_c` 位。这样既能在 record 里当整数用，又能精确控制比特宽度。
- 紧跟在每个 record 后面的 `*_Size_c` 常量，把各字段宽度**加起来**，得到展平成 `std_logic_vector` 后的总宽度。这正是 4.3 节转换函数需要的长度。

输入数据 record 定义在这里：

[hdl/psi_ms_daq_pkg.vhd:37-44 — Input2Daq_Data_t 输入数据记录](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L37-L44)

```vhdl
type Input2Daq_Data_t is record
  Last   : std_logic;
  Data   : std_logic_vector;   -- 宽度未定，使用处再约束
  Bytes  : std_logic_vector;   -- 宽度未定，使用处再约束
  IsTo   : std_logic;
  IsTrig : std_logic;
end record;
type Input2Daq_Data_a is array (natural range <>) of Input2Daq_Data_t;
```

`Data` / `Bytes` 的实际宽度在输入逻辑实体里才被「钉死」，与 `IntDataWidth_g` 绑定：

[hdl/psi_ms_daq_input.vhd:66 — 使用处把 Data/Bytes 宽度约束到 IntDataWidth_g](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L66)

```vhdl
Daq_Data : out Input2Daq_Data_t(Data(IntDataWidth_g-1 downto 0),
                               Bytes(log2ceil(IntDataWidth_g/8) downto 0));
```

这意味着：

- `Data` 宽度 = `IntDataWidth_g`（例如 64 位）。
- `Bytes` 宽度 = `log2ceil(IntDataWidth_g/8) + 1`。当 `IntDataWidth_g = 64` 时，`log2ceil(8) = 3`，所以 `Bytes` 是 4 位（范围 `3 downto 0`），能表示 0–15 个字节。
- 这正是 u4-l4 将要讲的「把内部数据宽度 `IntDataWidth_g` 提取为 generic」的体现——record 的字段宽度跟着 generic 走，而非写死 64。
- `Input2Daq_Data_a` 是该 record 的数组类型，用于「每路流一个数据接口」的场合。

#### 4.2.4 代码实践

**实践目标**：把 record 字段名和它们的物理宽度对上号，为 4.3 节的位拼接做铺垫。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_pkg.vhd:46-62](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L46-L62)。
2. 手算两个总宽常量：
   - `DaqSm2DaqDma_Cmd_Size_c = 32 + 16 + MaxStreamsBits_c = 32 + 16 + 5 = 53`
   - `DaqDma2DaqSm_Resp_Size_c = 16 + 1 + MaxStreamsBits_c = 16 + 1 + 5 = 22`
3. 打开 [hdl/psi_ms_daq_input.vhd:66](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_input.vhd#L66)，代入 `IntDataWidth_g = 64`，算出 `Data` 是 64 位、`Bytes` 是 4 位。

**需要观察的现象**：`Stream` 字段在 record 里是 `integer`，但计入 `*_Size_c` 时用的是 `MaxStreamsBits_c`，说明它综合后就是 `MaxStreamsBits_c` 位。

**预期结果**：你能口算出「命令展平后 53 位、响应展平后 22 位」，并理解这些数字会被 4.3 节的转换函数直接用作向量长度。

**待本地验证**：若你手头有仿真器，可在 `DaqSm2DaqDma_Cmd_ToStdlv` 函数返回的向量上用 `'length` 属性打印长度，确认等于 53。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Stream` 字段用 `integer range 0 to MaxStreams_c - 1` 而不是直接 `std_logic_vector(4 downto 0)`？

**答案**：用带范围约束的 `integer`，可在代码里直接做算术和比较（如 `Stream = 0`），可读性好；同时范围 `0 to MaxStreams_c-1` 让综合器知道它最多占 `MaxStreamsBits_c` 位，宽度仍然受控。展平时再转成无符号向量即可。

**练习 2**：`Input2Daq_Data_t` 的 `Data` 字段为什么在包里不写死宽度？

**答案**：因为内部数据宽度 `IntDataWidth_g` 现在是可参数化的 generic（feature/se32 引入），不同实例可能用 32/64 等不同宽度。把宽度留到使用处（实体端口）再约束，才能让同一个 record 类型适配多种 `IntDataWidth_g`。

---

### 4.3 ToStdlv / FromStdlv 转换函数

#### 4.3.1 概念说明

record 虽然可读性好，但**FIFO 和 RAM 不能直接存 record**——它们只认一串比特（`std_logic_vector`）。控制状态机把命令交给 DMA 引擎时，中间要经过一个**命令 FIFO**；DMA 把响应回传时，也要经过一个**响应 FIFO**。因此需要一对函数：

- `ToStdlv(rec)`：把 record **打平**成一个向量，写进 FIFO。
- `FromStdlv(stdlv)`：把 FIFO 读出的向量**还原**成 record。

这两步必须**互逆**，且字段在向量里的位置必须严格固定，否则还原出来的数据会错位。

#### 4.3.2 核心流程

打平规则是「**低位放第一个字段，依次往高位堆**」。以命令为例，字段在 53 位向量里的布局是：

```
位号:  52    48                                  32                               0
       |Stream| (5b) |      MaxSize (16b)        |        Address (32b)           |
       ^^^^^^^         ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
       最高位                              中间                              最低位
```

写成数学形式，若令 \(S\) 为向量，则：

\[
S[\,31\!:\!0\,] = \text{Address},\quad
S[\,47\!:\!32\,] = \text{MaxSize},\quad
S[\,52\!:\!48\,] = \text{Stream}
\]

响应的 22 位向量布局类似：

\[
S[\,15\!:\!0\,] = \text{Size},\quad
S[\,16\,] = \text{Trigger},\quad
S[\,21\!:\!17\,] = \text{Stream}
\]

还原过程就是反向切片：把向量按上面的边界切回三段，分别赋给 record 的三个字段。

#### 4.3.3 源码精读

转换函数在包体（`package body`）里实现：

[hdl/psi_ms_daq_pkg.vhd:122-138 — DaqSm2DaqDma_Cmd 的打平与还原函数](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L122-L138)

```vhdl
function DaqSm2DaqDma_Cmd_ToStdlv(rec : DaqSm2DaqDma_Cmd_t) return std_logic_vector is
  variable stdlv : std_logic_vector(DaqSm2DaqDma_Cmd_Size_c - 1 downto 0);
begin
  stdlv(31 downto 0)          := rec.Address;
  stdlv(47 downto 32)         := rec.MaxSize;
  stdlv(stdlv'left downto 48) := std_logic_vector(to_unsigned(rec.Stream, MaxStreamsBits_c));
  return stdlv;
end function;
```

要点解读：

- 结果向量长度 = `DaqSm2DaqDma_Cmd_Size_c`（53），用 `'left`（即 52）来定位 Stream 段的最高位，避免硬编码 52——这样即使将来 `MaxStreamsBits_c` 变了，这段代码也不用改。
- `rec.Stream` 是整数，用 `to_unsigned(..., MaxStreamsBits_c)` 转成 5 位无符号向量。
- 还原函数 `DaqSm2DaqDma_Cmd_FromStdlv` 是严格逆操作：把 `stdlv(31 downto 0)` 切回 `Address`，依此类推。

响应的转换函数在这里：

[hdl/psi_ms_daq_pkg.vhd:141-157 — DaqDma2DaqSm_Resp 的打平与还原函数](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L141-L157)

注意一个**源码原样事实**：响应的还原函数名是 `DaqDme2DaqSm_Resp_FromStdlv`（中间是 `Dme` 而非 `Dma`，是源码里的拼写不一致）。它在 DMA 引擎里被实际调用：

[hdl/psi_ms_daq_daq_dma.vhd:304 — 命令进 FIFO 前打平](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L304)

```vhdl
CmdFifo_InData <= DaqSm2DaqDma_Cmd_ToStdlv(DaqSm_Cmd);
```

[hdl/psi_ms_daq_daq_dma.vhd:344 — 响应出 FIFO 后还原](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_dma.vhd#L344)

```vhdl
DaqSm_Resp     <= DaqDme2DaqSm_Resp_FromStdlv(RspFifo_OutData);
```

这就是「打平 → 存 FIFO → 读 FIFO → 还原」的完整闭环。如果用 `grep` 找这个函数，要用 `DaqDme` 这个拼写才能命中。

#### 4.3.4 代码实践

**实践目标**：画出两个 record 的位拼接关系图（本讲指定的核心实践任务之一）。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_pkg.vhd:122-157](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L122-L157)。
2. 在纸上画一条 53 格的横条代表命令向量，按下表标注每段：

   | 比特区间 | 字段 | 宽度 |
   | --- | --- | --- |
   | `[31:0]` | Address | 32 |
   | `[47:32]` | MaxSize | 16 |
   | `[52:48]` | Stream | 5 |

3. 再画一条 22 格的横条代表响应向量：

   | 比特区间 | 字段 | 宽度 |
   | --- | --- | --- |
   | `[15:0]` | Size | 16 |
   | `[16]` | Trigger | 1 |
   | `[21:17]` | Stream | 5 |

4. 对照函数体确认每段的起止比特与你画的图一致。

**需要观察的现象**：两个 record 的 `Stream` 字段都放在**最高位**段，且最高位都用 `'left` 而非硬编码，方便将来扩展位宽。

**预期结果**：你能不看源码，口述「命令的 Address 在最低 32 位、MaxSize 在中段 16 位、Stream 在最高 5 位」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ToStdlv` 里 Stream 段的最高位用 `stdlv'left` 而不直接写 52？

**答案**：`stdlv'left` 会自动等于 `DaqSm2DaqDma_Cmd_Size_c - 1`。当 `MaxStreams_c` 改变导致 `MaxStreamsBits_c` 改变时，向量总长会变，用 `'left` 能自动跟随，而硬编码 52 就会出错。

**练习 2**：如果有人误把 `ToStdlv` 里 Address 和 MaxSize 的位置写反了（即 `stdlv(47 downto 32) := rec.Address`），但 `FromStdlv` 没改，会出现什么现象？

**答案**：打平后写进 FIFO 的是错位的向量，还原时 Address 取到 `[31:0]`、MaxSize 取到 `[47:32]`，于是命令的地址和最大长度被互换——DMA 会去错误的地址搬错误数量的字节。这正是 `ToStdlv`/`FromStdlv` 必须严格互逆的原因。

---

### 4.4 上下文访问记录与 CtxStr_Sel_* / CtxStr_Sft_* 常量

#### 4.4.1 概念说明

除了命令/响应，控制状态机还需要读写另一块存储：**上下文存储（Context Memory）**。上下文里保存着每路流的配置和指针（缓冲起始地址、窗口大小、当前写指针、窗口末地址等）。u3-l4 会详细讲这块存储的组织，本讲只关注**访问它的接口类型**。

这块存储按「流」和「流×窗口」两级组织，所以包里定义了两套访问 record：

- `ToCtxStr_t`：访问**流上下文**的命令（读/写哪个流的哪一段 64 位）。
- `ToCtxWin_t`：访问**窗口上下文**的命令（读/写哪个流、哪个窗口的哪一段 64 位）。
- `FromCtx_t`：两类访问共用的**读返回数据**（64 位，拆成高低两个 32 位半字）。

关键在于：一次访问能读/写 **64 位**，而一个流有好几个 64 位的字段组。到底访问哪一组？由 `Sel`（选择）字段决定，它的取值就是 `CtxStr_Sel_*` / `CtxWin_Sel_*` 常量。

#### 4.4.2 核心流程

流上下文有 5 个 32 位字段：`SCFG`、`BUFSTART`、`WINSIZE`、`PTR`、`WINEND`。它们两两拼成 64 位，用 2 位的 `Sel` 选择：

| `Sel` 取值（常量名） | `Sel` 编码 | 低 32 位（`RdatLo`） | 高 32 位（`RdatHi`） |
| --- | --- | --- | --- |
| `CtxStr_Sel_ScfgBufstart_c` | `"00"` | SCFG（流配置） | BUFSTART（缓冲起始地址） |
| `CtxStr_Sel_WinsizePtr_c` | `"01"` | WINSIZE（窗口大小） | PTR（当前写指针） |
| `CtxStr_Sel_Winend_c` | `"10"` | WINEND（窗口末地址） | （未用） |

> 这三个常量名其实是「低半字段 + 高半字段」的拼接，例如 `Scfg` + `Bufstart`，直接告诉你这次访问同时拿到的是哪两个字段。

其中 SCFG 这 32 位内部又被切成多个子字段，位置由 `CtxStr_Sft_*`（shift，位移）常量定义：

| 子字段常量 | 起始比特 | 含义 |
| --- | --- | --- |
| `CtxStr_Sft_SCFG_RINGBUF_c` | 0 | 是否环形缓冲 |
| `CtxStr_Sft_SCFG_OVERWRITE_c` | 8 | 是否允许覆盖旧窗口 |
| `CtxStr_Sft_SCFG_WINCNT_c` | 16 | 窗口总数 |
| `CtxStr_Sft_SCFG_WINCUR_c` | 24 | 当前窗口号 |

窗口上下文只有两种 64 位组，用 1 位 `Sel` 选择：

| `Sel` 取值（常量名） | `Sel` 编码 | 内容 |
| --- | --- | --- |
| `CtxWin_Sel_WincntWinlast_c` | `"0"` | 窗口字节数 + 末样地址 / 末样标志 |
| `CtxWin_Sel_WinTs_c` | `"1"` | 时间戳 |

#### 4.4.3 源码精读

三个上下文 record 定义在这里：

[hdl/psi_ms_daq_pkg.vhd:64-97 — ToCtxStr_t / ToCtxWin_t / FromCtx_t 三个上下文访问记录](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L64-L97)

关键常量（流上下文选择与 SCFG 内部位移）：

```vhdl
constant CtxStr_Sel_ScfgBufstart_c   : std_logic_vector(1 downto 0) := "00";
constant CtxStr_Sel_WinsizePtr_c     : std_logic_vector(1 downto 0) := "01";
constant CtxStr_Sel_Winend_c         : std_logic_vector(1 downto 0) := "10";

constant CtxStr_Sft_SCFG_RINGBUF_c   : integer := 0;
constant CtxStr_Sft_SCFG_OVERWRITE_c : integer := 8;
constant CtxStr_Sft_SCFG_WINCNT_c    : integer := 16;
constant CtxStr_Sft_SCFG_WINCUR_c    : integer := 24;
```

要点：

- `ToCtxStr_t` 的 `Sel` 是 2 位（最多 4 组，实际用了 3 组）；`ToCtxWin_t` 的 `Sel` 是 1 位（2 组，全用）。
- `WenLo` / `WenHi` 分别控制写低 32 位和高 32 位——可以只写一半。`WdatLo` / `WdatHi` 是对应的写数据。
- `FromCtx_t` 只有 `RdatLo` / `RdatHi` 两个读数据半字，因为读返回不需要地址/选择信息。

这套机制在状态机里被这样使用（注意 `Sel` 如何在多拍读取中切换）：

[hdl/psi_ms_daq_daq_sm.vhd:339-373 — ReadCtxStr_s 状态：用 CtxStr_Sel_* 分三拍读出三组字段](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L339-L373)

```vhdl
case r.HndlCtxCnt is
  when 0 => v.CtxStr_Cmd.Sel := CtxStr_Sel_Winend_c;       -- 先读 WinEnd
  when 1 => v.CtxStr_Cmd.Sel := CtxStr_Sel_WinsizePtr_c;   -- 再读 WinSize+Ptr
  when 2 => v.CtxStr_Cmd.Sel := CtxStr_Sel_ScfgBufstart_c; -- 再读 SCFG+Bufstart
  ...
-- 一拍后在响应里取回对应字段：
when 2 => v.HndlWinEnd := CtxStr_Resp.RdatLo;                       -- 取 WinEnd
when 3 => v.HndlWinSize := CtxStr_Resp.RdatLo;
          v.HndlPtr0    := CtxStr_Resp.RdatHi;                      -- 取 WinSize / Ptr
when 4 => v.HndlRingbuf   := CtxStr_Resp.RdatLo(CtxStr_Sft_SCFG_RINGBUF_c);
          v.HndlOverwrite := CtxStr_Resp.RdatLo(CtxStr_Sft_SCFG_OVERWRITE_c);
          v.HndlWincnt    := CtxStr_Resp.RdatLo(... WINCNT_c ...);
          v.HndlWincur    := CtxStr_Resp.RdatLo(... WINCUR_c ...);  -- 从 SCFG 切出各子字段
          v.HndlBufstart  := CtxStr_Resp.RdatHi;                    -- 取 Bufstart
```

这段真实用法完美印证了上表的布局：选 `ScfgBufstart` 时，`RdatLo` 是 SCFG（再按 `CtxStr_Sft_*` 切出 4 个子字段），`RdatHi` 是 Bufstart。

#### 4.4.4 代码实践

**实践目标**：解释三个 `CtxStr_Sel_*` 选择值如何区分一次上下文访问读的是哪 64 位（本讲指定的核心实践任务之二）。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_pkg.vhd:73-79](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_pkg.vhd#L73-L79)，确认三个 `Sel` 常量的编码值。
2. 打开 [hdl/psi_ms_daq_daq_sm.vhd:339-373](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L339-L373)，对照 `when` 分支与上一拍响应取回的字段，填出下表：

   | `Sel` 常量 | 编码 | `RdatLo`(低32) 取到 | `RdatHi`(高32) 取到 |
   | --- | --- | --- | --- |
   | `CtxStr_Sel_???` | `"00"` | ?（SCFG） | ?（BUFSTART） |
   | `CtxStr_Sel_???` | `"01"` | ?（WINSIZE） | ?（PTR） |
   | `CtxStr_Sel_???` | `"10"` | ?（WINEND） | ?（未用） |

3. 解释为什么常量名形如 `ScfgBufstart`（两个字段名拼起来）——因为它同时告诉你这次 64 位访问拿到的是哪两个 32 位字段。

**需要观察的现象**：状态机分三拍（`HndlCtxCnt = 0,1,2`）分别置 `Sel` 为 `Winend`、`WinsizePtr`、`ScfgBufstart`；又因为读延迟一拍，所以在 `HndlCtxCnt = 2,3,4` 时分别取回这三组数据。

**预期结果**：你能用自己的话讲清「同一个 `CtxStr_Cmd` 端口，靠 `Sel` 在 2 比特里取 3 个值，就能分三次读出一个流的全部 5 个上下文字段」。

**待本地验证**：可在 testbench 里观察 `CtxStr_Cmd.Sel` 与一拍后 `CtxStr_Resp.RdatLo/RdatHi` 的波形，确认对应关系。

#### 4.4.5 小练习与答案

**练习 1**：`ToCtxStr_t.Sel` 是 2 位，但只定义了 3 个选择值（`"00"`/`"01"`/`"10"`）。`"11"` 去哪了？

**答案**：`"11"` 当前未使用（预留）。因为流上下文只有 3 组 64 位字段（SCFG+BUFSTART、WINSIZE+PTR、WINEND），第 4 组暂无对应内容。将来若新增字段可启用 `"11"`。

**练习 2**：为什么 `WenLo` 和 `WenHi` 是两个独立的写使能，而不是一个总的 `Wen`？

**答案**：状态机常常只需更新 64 位中的某一个 32 位半字（例如只改 PTR 而不动 WINSIZE）。两个独立使能允许「只写高半字、保留低半字」，避免读改写整字带来的额外开销和竞争。

**练习 3**：SCFG 的 4 个子字段（RINGBUF/OVERWRITE/WINCNT/WINCUR）为什么分别放在比特 0/8/16/24，而不是紧挨着放？

**答案**：它们各占约 8 位（一字节边界对齐），这样每个子字段落在自己独立的字节里，便于驱动软件按字节读写，也便于用 `CtxStr_Sft_*` 常量做位切片。这是硬件/软件协同的布局约定。

---

## 5. 综合实践

**综合任务**：把本讲全部四个最小模块串起来，做一次「端到端字段追踪」。

场景：控制状态机决定对**流 7**发起一次 DMA，目标地址 `0x4000_0000`，本次最多搬 `0x0800`（2048）字节。

请按顺序完成：

1. **用 record 描述这条命令**：写出一个 `DaqSm2DaqDma_Cmd_t` 的伪实例，填好 `Address`、`MaxSize`、`Stream` 三个字段的值。
2. **打平它**：参考 4.3 的位布局，画出这条命令展平后 53 位向量的分段图，标注每段的十六进制值（`Stream=7` 占高 5 位，`MaxSize=0x0800` 占中 16 位，`Address=0x4000_0000` 占低 32 位）。
3. **定位上下文来源**：这条命令的地址和最大长度，是状态机从上下文存储的哪个字段组读出来的？根据 4.4，指出它对应 `CtxStr_Sel_WinsizePtr_c`（提供 PTR 作为地址基准）和 `CtxStr_Sel_ScfgBufstart_c`（提供 BUFSTART）。再用 `CtxStr_Sft_*` 说明：如果该流配置为「环形缓冲、不覆盖、8 个窗口、当前窗口 2」，SCFG 这个 32 位字的比特 0/8/16/24 分别应是什么值。
4. **验证互逆**：把第 2 步得到的 53 位向量送进 `DaqSm2DaqDma_Cmd_FromStdlv`，确认能还原出第 1 步的三个字段值。

参考答案要点：

1. `Address = 0x40000000`、`MaxSize = 0x0800`、`Stream = 7`。
2. `[52:48]=00111`(=7)、`[47:32]=0x0800`、`[31:0]=0x40000000`。
3. 地址基准来自 PTR（属 `WinsizePtr` 组，`RdatHi`），BUFSTART 来自 `ScfgBufstart` 组（`RdatHi`）。SCFG 各比特：RINGBUF(bit0)=1、OVERWRITE(bit8)=0、WINCNT(bit16)=8-1=7（驱动写 winCnt-1，详见 u1-l4/u3）、WINCUR(bit24)=2。
4. `FromStdlv` 按 `[31:0]`/`[47:32]`/`[52:48]` 三段切回，正好还原。

> 说明：第 3 步中「WINCNT 存 winCnt-1」是驱动层的约定（u1-l4 已提及），本讲只关注它在 SCFG 里的比特位置。如果你尚不确定 winCnt 的具体编码，可标注「待本地验证」并在 u3 单元回来核对。

## 6. 本讲小结

- `psi_ms_daq_pkg` 是全 IP 核的**公共字典**：上限常量、派生位宽、记录模式编码、模块间通信 record、上下文访问 record 全部集中在此。
- 上限常量 `MaxStreams_c=32` / `MaxWindows_c=32` / `MaxStreamWidth_c=64` 经 `log2ceil` 派生出 `MaxStreamsBits_c=5` 等位宽，改上限即自动改位宽。
- 模块间接口用 record 表达：`DaqSm2DaqDma_Cmd_t`（命令，53 位）、`DaqDma2DaqSm_Resp_t`（响应，22 位）、`Input2Daq_Data_t`（数据，`Data`/`Bytes` 宽度由 `IntDataWidth_g` 动态决定）。
- record 与 `std_logic_vector` 之间靠 `ToStdlv` / `FromStdlv` 互逆转换，字段在向量里**从低位向高位堆叠**，最高位段用 `'left` 定位以适应可变位宽。
- 上下文访问靠 `Sel` 字段选择 64 位组：`CtxStr_Sel_ScfgBufstart_c`/`WinsizePtr_c`/`Winend_c` 分别读出 SCFG+BUFSTART / WINSIZE+PTR / WINEND；SCFG 内部再用 `CtxStr_Sft_*` 切出 RINGBUF/OVERWRITE/WINCNT/WINCUR 子字段。
- 源码里有一处拼写不一致：响应还原函数名为 `DaqDme2DaqSm_Resp_FromStdlv`（`Dme`），`grep` 时需用此拼写。

## 7. 下一步学习建议

本讲只是建立了「类型词汇表」。接下来建议：

- **u2-l2（输入逻辑接口与时钟域）**：看 `Input2Daq_Data_t` 这个 record 是怎么被输入逻辑填满并跨时钟域送到 DMA 的，你会第一次看到本讲的类型在真实时序里流动。
- **u2-l5 / u2-l6（DMA 引擎）**：看 `DaqSm2DaqDma_Cmd_ToStdlv` / `DaqDme2DaqSm_Resp_FromStdlv` 是如何配合命令/响应 FIFO 工作的，理解「为什么必须打平」。
- **u3-l4（上下文存储模型）**：看 `ToCtxStr_t` / `ToCtxWin_t` / `FromCtx_t` 背后那块双口 RAM 的真实组织，把本讲的 `Sel` 选择值落实到具体的 RAM 地址译码。
- **u4-l4（IntDataWidth_g 与 AXI 缓存控制）**：看 `Input2Daq_Data_t` 的 `Data`/`Bytes` 动态宽度是如何随 `IntDataWidth_g` generic 化贯穿 input/dma/axi_if 三个模块的。

阅读时，建议随时回到本讲查阅字段名、位宽和位布局——后续讲义会频繁引用这些约定。
