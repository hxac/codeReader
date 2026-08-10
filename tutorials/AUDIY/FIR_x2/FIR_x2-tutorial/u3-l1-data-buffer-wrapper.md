# DATA_BUFFER：输入 PCM 数据缓冲封装

## 1. 本讲目标

本讲聚焦于 FIR_x2 信号通路上的第一个封装模块 **DATA_BUFFER**。它是「输入数据通路」的入口，负责把外部送进来的 PCM 音频样点存进一块双口 RAM，并按地址把历史样点读出来供后续乘法使用。

学完本讲你应该能够：

- 说清 DATA_BUFFER 在整个滤波器里扮演的「封装层（wrapper）」角色，以及它为什么要存在。
- 看懂 DATA_BUFFER 的四个参数（`ADDR_WIDTH` / `DATA_WIDTH` / `OUTPUT_REG` / `RAM_INIT_FILE`）各自的含义。
- 画出控制器 DPRAM_CONT 与存储原语 SDPRAM_SINGLECLK 之间的连线关系，标注每一根内部 wire 连到哪个端口。
- 解释 `OUTPUT_REG` 取 `"TRUE"` / `"FALSE"` 时，读数据相比读地址分别滞后几个 MCLK，以及这对时序的影响。

> 本讲只讲「封装与连线」，刻意不深入控制器内部的环形地址算法（留到 u3-l2）和原语内部的寄存器细节（留到 u3-l3）。本讲建立的是「黑盒拼接」层面的认识。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 为什么 FIR 滤波需要一块数据缓冲

FIR（有限脉冲响应）滤波的本质是**卷积**：每一个输出样点，都是「最近 N 个输入样点」与「N 个固定系数」对应相乘再相加的结果。这里的 N 就是抽头数（taps）。

这意味着硬件必须同时拿到「最近 N 个历史样点」。可是 I2S/PCM 接口每个采样周期只送进来**一个**新样点。所以我们需要一块存储器，把逐个到达的样点存起来，并在每个周期把「最近 N 个样点」按地址读出来。这块存储器就是本讲要讲的 **DATA_BUFFER**——它本质上是一个**滑动窗口 / 环形缓冲（ring buffer）**。

### 2.2 「控制器 + 原语」的分层思想

FIR_x2 把数据缓冲拆成了两层（这是贯穿全项目的关键设计，详见 u1-l2）：

- **控制器（DPRAM_CONT）**：不存数据，只负责产生「往哪个地址写、往哪个地址读、何时写、何时读」这些控制信号。它像大脑。
- **存储原语（SDPRAM_SINGLECLK）**：不懂地址算法，只负责「你给我地址和使能，我给你数据」的纯粹存取。它像肌肉，也像一块可被替换的砖。

DATA_BUFFER 的全部工作，就是把这两层**用几根线接起来**，再加几个参数把它们配置好。这就是「封装」二字的含义。

### 2.3 为什么要分层、为什么原语要被隔离在最底层

不同 FPGA 厂商（Altera、AMD、Gowin、Efinix）的 Block RAM 行为和推断方式各异。把「纯存取」隔离成一个独立的 SDPRAM 原语，移植时只需替换这一块，控制器和封装层完全不动。DATA_BUFFER 正是这个「可替换」策略的承载点。

### 2.4 同步读 RAM 与「读延迟」

SDPRAM 是**同步读**（synchronous read）RAM：地址必须在时钟有效沿之前就准备好，数据在时钟沿之后才出现在输出寄存器上。也就是说「给地址」和「拿到数据」之间天然有若干拍的延迟。本讲要回答的核心问题之一，就是这个延迟到底是几拍——它直接由参数 `OUTPUT_REG` 决定。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。

| 文件 | 作用 | 本讲定位 |
|------|------|---------|
| [02_DATA_BUFFER/DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v) | 本讲主角：封装模块，把控制器与原语拼起来 | 精读 |
| [01_DPRAM_CONT/DPRAM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v) | 环形缓冲地址控制器（被 DATA_BUFFER 实例化） | 看端口即可，细节在 u3-l2 |
| [02_DATA_BUFFER/SDPRAM_SINGLECLK.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v) | 简单双口 RAM 原语（被 DATA_BUFFER 实例化） | 看端口与读延迟，细节在 u3-l3 |
| [02_DATA_BUFFER/Questa/BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex) | RAM 初始化文件（仿真用） | 看格式与规模 |
| [02_DATA_BUFFER/DATA_BUFFER_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER_TB.v) | DATA_BUFFER 的测试激励 | 仿真实践依据 |
| [07_FIR_x2/FIR_x2.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v) | 顶层，展示 DATA_BUFFER 如何被参数化实例化 | 参考 |

> 一个有力的旁证：DATA_BUFFER 的仿真脚本 [DATA_BUFFER.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/DATA_BUFFER.bat) 的编译列表里同时列出了 `../*.v`（本目录的 DATA_BUFFER / SDPRAM）和 `../../01_DPRAM_CONT/DPRAM_CONT.v`。这条编译命令本身就暴露了「DATA_BUFFER = 本目录原语 + 上级目录控制器」的依赖关系。

## 4. 核心概念与源码讲解

### 4.1 缓冲模块封装

#### 4.1.1 概念说明

DATA_BUFFER 是一个**纯结构化模块**——它内部几乎没有任何 `always` 逻辑，只做两件事：声明几根内部连线，然后实例化两个子模块。它的价值不在于「算什么东西」，而在于：

1. **对外提供一个简洁的音频接口**：左边进 `WDATA_I`（PCM 样点），右边出 `RDATA_O`（历史样点），把复杂的地址/使能时序藏在内部。
2. **对内绑定控制器与原语**：让上层（顶层 FIR_x2）只需要面对一个模块，而不必同时面对控制器和存储两块。
3. **集中暴露可配置参数**：深度、位宽、是否加输出寄存、初始化文件，全部通过 parameter 一目了然。

用一句话概括：**DATA_BUFFER 把「谁来算地址」和「谁来存数据」这两件事，封装成一个「给我样点、我还你样点」的黑盒。**

#### 4.1.2 核心流程

从外部看 DATA_BUFFER 的数据流非常简单：

```
        ┌─────────────────────────────────────────┐
WDATA_I ─┤                                         ├─ RDATA_O
        │  DATA_BUFFER (内部: 控制器 + 双口RAM)    │
LRCK_I ─┤                                         │
MCLK_I ─┤                                         │
NRST_I ─┤                                         │
        └─────────────────────────────────────────┘
```

- **写侧**：每个 LRCK 周期（即每个输入样点），控制器把 `WDATA_I` 写进 RAM 的某个地址。
- **读侧**：在每个 MCLK，控制器给出一个读地址，RAM 把对应的历史样点从 `RDATA_O` 吐出。
- 因为是环形缓冲，读地址会在 RAM 里一圈圈轮转，正好实现「滑动窗口」效果。

> 详细的「读地址如何领先写地址、如何绕圈」属于控制器的环形算法，留到 u3-l2 讲。本讲只要知道「控制器负责地址、原语负责存取」即可。

#### 4.1.3 源码精读

先看模块的参数与端口定义：

[02_DATA_BUFFER/DATA_BUFFER.v:50-67](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L50-L67) —— 这段定义了 DATA_BUFFER 的 4 个 parameter 与对外端口。

```verilog
module DATA_BUFFER #(
    parameter ADDR_WIDTH    = 8,
    parameter DATA_WIDTH    = 32,
    parameter OUTPUT_REG    = "TRUE",
    parameter RAM_INIT_FILE = "BUFFER_INIT.hex"
)
(
    input  wire                           MCLK_I,
    input  wire                           BCK_I,
    input  wire                           LRCK_I,
    input  wire                           NRST_I,
    input  wire signed [(DATA_WIDTH-1):0] WDATA_I,
    output wire signed [(DATA_WIDTH-1):0] RDATA_O
);
```

逐项解释四个参数：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `ADDR_WIDTH` | 8 | RAM 地址位宽，决定存储深度为 \(2^{\text{ADDR\_WIDTH}}\)，默认 256 个样点 |
| `DATA_WIDTH` | 32 | 单个样点的数据位宽，默认 32 位（对应 32 位 PCM） |
| `OUTPUT_REG` | `"TRUE"` | 读路径输出寄存器开关：`"TRUE"` 用两级寄存，`"FALSE"` 用一级（详见 4.3） |
| `RAM_INIT_FILE` | `"BUFFER_INIT.hex"` | 仿真时用 `$readmemh` 灌入 RAM 的初始化文件名 |

端口里有两点值得注意：

- `WDATA_I` 与 `RDATA_O` 都声明为 `signed`，因为 PCM 音频样点是有符号数。这也是后续乘法能正确做有符号运算的前提。
- **`BCK_I` 虽然出现在端口列表里，但模块内部没有任何地方用到它**（既没接到控制器，也没接到原语）。控制器 DPRAM_CONT 的端口里也没有 BCK。这是项目当前的一个真实状态——`BCK_I` 在 DATA_BUFFER 这一层是「预留未用」的接口位。阅读源码时不必怀疑自己看漏，这确实是一根悬空的线（详见 4.2.3 的连线表）。

再看顶层是如何把参数传给 DATA_BUFFER 的，作为对照：

[07_FIR_x2/FIR_x2.v:103-115](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L103-L115) —— 顶层实例化 DATA_BUFFER，参数全部从顶层的 parameter / localparam 透传。

```verilog
DATA_BUFFER #(
    .ADDR_WIDTH(WADDR_WIDTH),   // 顶层 WADDR_WIDTH = 8
    .DATA_WIDTH(DATA_WIDTH),    // 顶层 DATA_WIDTH = 32
    .OUTPUT_REG(OUTPUT_REG),    // 顶层 localparam OUTPUT_REG = "TRUE"
    .RAM_INIT_FILE(BUFF_INIT)   // 顶层 localparam BUFF_INIT = "BUFFER_INIT.hex"
) u_DATA_BUFFER ( ... );
```

可以看到顶层把规模参数（`WADDR_WIDTH`/`DATA_WIDTH`）留给了使用者配置，而把「行为开关」`OUTPUT_REG` 和文件名 `BUFF_INIT` 固化成了 [FIR_x2.v:78-79](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L78-L79) 的 localparam，不暴露到最外层。

#### 4.1.4 代码实践

**实践目标**：亲手确认 DATA_BUFFER 的「封装」性质——它内部只有实例化、没有数据处理逻辑。

**操作步骤**：

1. 打开 [DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v)。
2. 通读第 69 行到第 102 行（内部声明 + 两个实例化）。
3. 数一数：模块体内一共有几个 `always` 块？几个 `assign`？

**需要观察的现象**：

- DATA_BUFFER 模块体内**没有任何 `always` 块，也没有任何 `assign`**（`endmodule` 之前只有两段模块实例化）。
- 它既不做算术，也不做组合逻辑，纯粹是「连线 + 实例化」。

**预期结果**：你会确认 DATA_BUFFER 是 100% 的结构化封装模块。它的全部「智能」都来自被它实例化的 DPRAM_CONT；它的全部「存储」都来自 SDPRAM_SINGLECLK。这印证了 2.2 节的分层思想。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 DATA_BUFFER 支持 64 位 PCM（而非 32 位），需要改哪里？

> **参考答案**：把实例化处的 `.DATA_WIDTH(...)` 传成 64 即可（例如顶层把 `DATA_WIDTH` 参数设为 64）。DATA_BUFFER 内部位宽会随之自适应，因为 `WDATA_I`/`RDATA_O` 都用 `(DATA_WIDTH-1):0` 描述。注意这会同时影响下游乘法位宽（`MULT_WIDTH`），需要整体评估。

**练习 2**：DATA_BUFFER 自己能否独立完成「滑动窗口」功能？为什么？

> **参考答案**：不能。DATA_BUFFER 本身没有任何地址生成逻辑，它只是把控制器和原语连起来。真正决定「读哪个地址、写哪个地址」的是它内部的 DPRAM_CONT；离开控制器，DATA_BUFFER 既不知道何时写也不知道读哪里。

---

### 4.2 控制器与原语连线

#### 4.2.1 概念说明

DATA_BUFFER 内部只有 4 根真正的「内部信号」：`WEN`、`WADDR`、`REN`、`RADDR`。它们构成了一条**控制总线**，把控制器的输出和原语的输入一对一对接。

理解这一节的关键是分清「谁产生、谁消费」：

- **控制器 DPRAM_CONT 是生产者**：它产出写使能、写地址、读使能、读地址。
- **原语 SDPRAM 是消费者**：它消费这些使能与地址，完成实际的读写。
- **样点数据不走控制器**：`WDATA_I`（要写的数据）和 `RDATA_O`（读出的数据）直接连到原语，控制器完全不碰数据本身——它只管「地址和使能」。

这是一种很干净的职责切分：**控制器管「在哪、何时」，原语管「存什么、取什么」。**

#### 4.2.2 核心流程

连线的数据流可以画成：

```
              ┌──────────────┐
              │  DPRAM_CONT  │  (大脑：地址+使能)
              │   控制器      │
              └──┬───┬──┬──┬─┘
       WEN_O  ───┘   │  │  │
      WADDR_O ───────┘  │  │        4 根控制线
       REN_O  ──────────┘  │
      RADDR_O ─────────────┘
                 │ │ │ │
                 ▼ ▼ ▼ ▼
              ┌──────────────┐
 WDATA_I ────▶│   SDPRAM     │─────▶ RDATA_O
              │  双口RAM原语  │  (肌肉：纯存取)
 MCLK_I  ────▶│              │
              └──────────────┘
```

- 控制器的 4 个输出，正好对应原语的 4 个控制输入（`WENABLE_I`/`WADDR_I`/`RENABLE_I`/`RADDR_I`）。
- 时钟 `MCLK_I` 同时供给两者：控制器用它打拍产生地址，原语用它同步读写。
- `LRCK_I` 与 `NRST_I` 只给控制器（控制器需要 LRCK 边沿来推进样点节奏；复位时它有特殊的预填充时序，见 u3-l2）。

#### 4.2.3 源码精读

先看 4 根内部连线的声明：

[02_DATA_BUFFER/DATA_BUFFER.v:69-73](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L69-L73) —— 声明控制器与原语之间的 4 根控制线。

```verilog
wire WEN;
wire REN;
wire [(ADDR_WIDTH-1):0] WADDR;
wire [(ADDR_WIDTH-1):0] RADDR;
```

再看控制器实例化——注意它只接了 `MCLK_I / LRCK_I / NRST_I` 三个输入：

[02_DATA_BUFFER/DATA_BUFFER.v:75-86](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L75-L86) —— 实例化 DPRAM_CONT，4 个输出连到内部 wire。

```verilog
DPRAM_CONT #(
    .ADDR_WIDTH(ADDR_WIDTH)
) u_DPRAM_CONT (
    .MCLK_I(MCLK_I),
    .LRCK_I(LRCK_I),
    .NRST_I(NRST_I),
    .WEN_O(WEN),
    .WADDR_O(WADDR),
    .REN_O(REN),
    .RADDR_O(RADDR)
);
```

最后是原语实例化——它消费那 4 根控制线，并直接接数据口：

[02_DATA_BUFFER/DATA_BUFFER.v:88-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L88-L102) —— 实例化 SDPRAM_SINGLECLK，控制线、数据线、时钟全部接好。

```verilog
SDPRAM_SINGLECLK #(
    .DATA_WIDTH(DATA_WIDTH),
    .ADDR_WIDTH(ADDR_WIDTH),
    .OUTPUT_REG(OUTPUT_REG),
    .RAM_INIT_FILE(RAM_INIT_FILE)
) u_SDPRAM_SINGLECLK (
    .CLK_I(MCLK_I),
    .WENABLE_I(WEN),
    .WADDR_I(WADDR),
    .WDATA_I(WDATA_I),
    .RENABLE_I(REN),
    .RADDR_I(RADDR),
    .RDATA_O(RDATA_O)
);
```

把上面三段综合起来，就得到了完整的「逐线连接表」：

| 内部 wire | 位宽 | 产生自（控制器端口） | 消费于（原语端口） |
|-----------|------|---------------------|--------------------|
| `WEN` | 1 | `DPRAM_CONT.WEN_O` | `SDPRAM.WENABLE_I` |
| `WADDR` | `ADDR_WIDTH` | `DPRAM_CONT.WADDR_O` | `SDPRAM.WADDR_I` |
| `REN` | 1 | `DPRAM_CONT.REN_O` | `SDPRAM.RENABLE_I` |
| `RADDR` | `ADDR_WIDTH` | `DPRAM_CONT.RADDR_O` | `SDPRAM.RADDR_I` |

直接相连（不经过控制器）的端口：

| 顶层信号 | 去向 | 说明 |
|----------|------|------|
| `MCLK_I` | `DPRAM_CONT.MCLK_I` **且** `SDPRAM.CLK_I` | 同一个时钟同时驱动控制器与原语 |
| `WDATA_I` | `SDPRAM.WDATA_I` | 待写入样点，直送原语 |
| `RDATA_O` | 来自 `SDPRAM.RDATA_O` | 读出样点，直出模块 |
| `LRCK_I` | `DPRAM_CONT.LRCK_I` | 只给控制器做样点节拍 |
| `NRST_I` | `DPRAM_CONT.NRST_I` | 只给控制器做复位 |
| `BCK_I` | **未连接** | 预留端口，DATA_BUFFER 内部未使用 ⚠️ |

> 这张表就是本讲的「核心交付物」。它回答了规格里要求的「标注每一根内部 wire 连接到哪个端口」。注意 `BCK_I` 这一行——这是从源码如实读出的结论，不是看漏。

#### 4.2.4 代码实践

**实践目标**：把 4.2.3 的连线表在源码里逐行核对一遍，并理解控制器对读写地址的约束（为 u3-l2 预热）。

**操作步骤**：

1. 打开 [DPRAM_CONT.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v)，找到它的 4 个输出端口声明（[DPRAM_CONT.v:59-62](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L59-L62)）。
2. 对照 DATA_BUFFER 实例化（[DATA_BUFFER.v:82-85](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L82-L85)），确认端口名一一对应（注意控制器侧带 `_O` 后缀，原语侧带 `_I` 后缀）。
3. 看控制器里 `REN_O` 的产生式（[DPRAM_CONT.v:104](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/01_DPRAM_CONT/DPRAM_CONT.v#L104)）：
   ```verilog
   assign REN_O = ~(WEN_REG & (WADDR_REG == RADDR_REG));
   ```

**需要观察的现象**：

- 端口名之间的对应关系：`WEN_O→WENABLE_I`、`WADDR_O→WADDR_I`、`REN_O→RENABLE_I`、`RADDR_O→RADDR_I`。
- `REN_O` 的表达式含义：**当「正在写」且「写地址等于读地址」时，读使能拉低**——也就是读写撞到同一个地址时，优先保护写，暂停一次读，避免读到尚未写好的数据。

**预期结果**：你会得到一张「控制器端口 → wire 名 → 原语端口」的三列对照表，并初步理解控制器在用 `REN` 防止读写冲突。环形地址如何递推本身是 u3-l2 的主题，这里只需建立「控制器在保护 RAM」的直觉。

> 是否真能在仿真里看到 `REN` 被拉低，取决于复位与样点时序；如需确认请按 4.3.4 的仿真步骤本地验证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WDATA_I` 和 `RDATA_O` 不连到控制器 DPRAM_CONT？

> **参考答案**：因为控制器的职责只是「算地址、产生使能」，它不参与数据的搬运与变换。把数据通路与控制通路分离，可以让控制器保持简单、可复用，也让原语专注于纯粹的存取。这是「地址产生器 + 存储宏」的经典分工。

**练习 2**：在 4.2.3 的表中，`BCK_I` 标注为「未连接」。这是否会影响仿真？

> **参考答案**：不会报错。Verilog 里模块输入端口未在内部使用是合法的（只是综合时会被优化掉、可能产生 warning）。由于 DATA_BUFFER 的控制器只依赖 MCLK 与 LRCK 产生地址，BCK 在本模块确实没有作用。它出现在端口列表里更像是接口对称性的预留。

---

### 4.3 输出寄存器配置

#### 4.3.1 概念说明

`OUTPUT_REG` 是 DATA_BUFFER 唯一一个「行为开关」型参数（其余三个描述规模或文件）。它控制原语 SDPRAM 读路径上的**输出寄存器级数**，从而在「读延迟」与「时序余量（fmax）」之间做权衡：

- 多一级寄存器 → 读数据多滞后一拍，但寄存器到寄存器的路径更短，电路能跑更高的时钟频率。
- 少一级寄存器 → 读数据更快出来，但组合路径更长，可能限制最高频率。

在 FIR_x2 这种「时钟随数据逐级打拍、严格对齐」的流水线里（见 u2-l1），读延迟到底是几拍非常关键——下游乘法器必须知道样点在哪一拍出现，才能正确地对齐系数。

#### 4.3.2 核心流程

SDPRAM 的读路径用两个寄存器串联：

```
读地址 RADDR ──▶ [RAM[RADDR]] ──拍1──▶ RDATA_REG_1P ──拍2──▶ RDATA_REG_2P
                                                       │                  │
                                  OUTPUT_REG="FALSE" ──┘                  │
                                  OUTPUT_REG="TRUE"  ─────────────────────┘
```

- 每个时钟上升沿，`RDATA_REG_1P` 捕获 `RAM[RADDR]` 的内容（这是第 1 拍延迟）。
- 同一个沿，`RDATA_REG_2P` 再把 `RDATA_REG_1P` 的内容搬过来（这是第 2 拍延迟）。
- `OUTPUT_REG` 决定 `RDATA_O` 从哪一级取出。

因此：

\[ \text{读延迟} = \begin{cases} 1 \text{ 个 MCLK}, & \text{OUTPUT\_REG} = \text{"FALSE"} \\ 2 \text{ 个 MCLK}, & \text{OUTPUT\_REG} = \text{"TRUE"} \end{cases} \]

注意这里的「延迟」是相对于**读地址在时钟沿被采样**而言：地址在第 N 个沿被采样后，数据分别在第 N+1、N+2 个沿之后出现在 1P、2P 寄存器上。

#### 4.3.3 源码精读

先看原语如何定义深度与两级读寄存器：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:65](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L65) 与 [SDPRAM_SINGLECLK.v:69-71](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L69-L71) —— 定义存储深度与两级输出寄存器。

```verilog
localparam MEMORY_DEPTH = 2**ADDR_WIDTH;     // ADDR_WIDTH=8 → 深度 256
...
reg [DATA_WIDTH-1:0] RAM[MEMORY_DEPTH-1:0];  // RAM 存储阵列
reg [DATA_WIDTH-1:0] RDATA_REG_1P = ...;     // 第 1 级读寄存
reg [DATA_WIDTH-1:0] RDATA_REG_2P = ...;     // 第 2 级读寄存
```

再看读路径的 always 块——每一拍同时推进两级寄存器：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:87-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L87-L93) —— 同步读：地址先进 1P，再进 2P。

```verilog
always @ (posedge CLK_I) begin
    if (RENABLE_I == 1'b1) begin
        RDATA_REG_1P <= RAM[RADDR_I];   // 地址 → 第 1 级（1 拍延迟）
        RDATA_REG_2P <= RDATA_REG_1P;   // 第 1 级 → 第 2 级（2 拍延迟）
    end
end
```

最后是 `OUTPUT_REG` 控制的 `generate` 选择：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:95-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L95-L102) —— 根据参数选择从 1P 或 2P 输出。

```verilog
generate
    if (OUTPUT_REG == "TRUE") begin : gen_reg2p
        assign RDATA_O = RDATA_REG_2P;   // 两级寄存 → 2 拍延迟
    end else begin : gen_reg1p
        assign RDATA_O = RDATA_REG_1P;   // 一级寄存 → 1 拍延迟
    end
endgenerate
```

所以对本讲主角 DATA_BUFFER（顶层把 `OUTPUT_REG` 固化为 `"TRUE"`）：

> **`OUTPUT_REG="TRUE"` 时，读数据相比读地址滞后 2 个 MCLK。**

最后看一眼初始化文件，验证规模对得上：

[02_DATA_BUFFER/Questa/BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex) —— 共 256 行，每行 8 个十六进制字符（= 32 位），且**全部为 `00000000`**。

- 行数 256 = \(2^{8}\) = `2**ADDR_WIDTH`，与原语的 `MEMORY_DEPTH` 完全一致。
- 每行 8 个 hex 位 = 32 位 = `DATA_WIDTH`。
- 全零：仿真起始时 RAM 被清零，随后由控制器的写口逐步填入真实 PCM 样点。这个文件存在的意义是给 `$readmemh` 提供一个合法的初始镜像，全零是安全无害的初值。

> `$readmemh` 的加载发生在 [SDPRAM_SINGLECLK.v:74-78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L74-L78) 的 `initial` 块里；它和 TB 喂数据用的 `.txt` 文件是两回事（前者初始化 RAM 内容，后者通过 `$fscanf` 提供输入样点，见 u1-l3，二者不可混淆）。

#### 4.3.4 代码实践

**实践目标**：通过仿真，亲眼确认 `OUTPUT_REG="TRUE"` 时 `RDATA_O` 相对读地址滞后 2 个 MCLK。

**操作步骤**：

1. 进入仿真目录，按 [DATA_BUFFER.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/DATA_BUFFER.bat) 的命令编译并启动仿真：
   ```
   cd 02_DATA_BUFFER/Questa
   vsim -do "do DATA_BUFFER.bat"
   ```
   （`DATA_BUFFER.bat` 会执行 `vlib` / `vlog -cover bcs` / `vsim -do "do run.do"`。）
2. 默认的 [run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/run.do) 只添加了模块对外的端口波形。为了观察延迟，手动把内部信号也加进波形：
   ```
   add wave -position insertpoint \
     sim:/DATA_BUFFER_TB/u_DATA_BUFFER/RADDR \
     sim:/DATA_BUFFER_TB/u_DATA_BUFFER/REN \
     sim:/DATA_BUFFER_TB/u_DATA_BUFFER/u_SDPRAM_SINGLECLK/RDATA_REG_1P \
     sim:/DATA_BUFFER_TB/u_DATA_BUFFER/u_SDPRAM_SINGLECLK/RDATA_REG_2P \
     sim:/DATA_BUFFER_TB/u_DATA_BUFFER/RDATA_O
   ```
3. 重启并运行：`restart -f; run -all`，然后在波形里把 `RADDR` 与三个数据信号对齐查看。

**需要观察的现象**：

- `RADDR` 在某个 MCLK 上升沿采到一个新地址 `A`。
- 1 拍之后，`RDATA_REG_1P` 出现 `RAM[A]` 的值。
- 再 1 拍（共 2 拍）之后，`RDATA_REG_2P` 出现同样的值，并经由 `RDATA_O` 输出。
- 也就是说 `RDATA_O` 比采样的 `RADDR` 晚 **2 个 MCLK**。

**预期结果**：波形上 `RDATA_O` 与 `RDATA_REG_2P` 完全同相，且整体比 `RADDR` 滞后 2 拍。若把实例化参数临时改成 `.OUTPUT_REG("FALSE")` 重新仿真，则 `RDATA_O` 会跟踪 `RDATA_REG_1P`，延迟变为 1 拍。

> 本地是否方便跑 Questa、波形的具体样值取决于环境，上述现象标注为**待本地验证**。即使不跑仿真，仅凭 4.3.3 的源码也能从逻辑上确定「2 拍延迟」这一结论。

#### 4.3.5 小练习与答案

**练习 1**：`BUFFER_INIT.hex` 为什么正好是 256 行？把它改成 255 行会发生什么？

> **参考答案**：因为 `ADDR_WIDTH=8`，`MEMORY_DEPTH = 2**8 = 256`，所以需要 256 个初值。改成 255 行后，`$readmemh` 只会填充前 255 个单元，最后一个单元保持未定义（`X`）。由于真实运行中控制器会逐步写入所有地址，这个 `X` 通常很快被覆盖；但严谨起见应保持文件行数与 `MEMORY_DEPTH` 一致。

**练习 2**：如果把 `OUTPUT_REG` 从 `"TRUE"` 改成 `"FALSE"`，除了读延迟变化，还会影响什么？

> **参考答案**：读延迟从 2 拍降到 1 拍，但同时从 RAM 读出到输出寄存器之间的组合路径变长（少了一级流水寄存），会降低该模块能达到的最高时钟频率。更关键的是：FIR_x2 的下游（FIR_COEF 派生的过采样时钟、乘法流水线）是按「2 拍读延迟」来对齐数据的（见 u2-l1 与 u4-l1），单独改这里会破坏整条流水线的时序对齐，需要同步调整下游寄存级数。所以顶层的 `OUTPUT_REG` 才被固化为 localparam、不轻易暴露给使用者。

## 5. 综合实践

把本讲三部分串起来，完成一个「DATA_BUFFER 全景标注」任务：

1. **画封装框图**：在 [DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v) 上画出 4.1.2 的数据流框图，标出对外端口（`WDATA_I`/`RDATA_O`/`MCLK_I`/`LRCK_I`/`NRST_I`）和内部两个子模块。
2. **补全连线表**：把 4.2.3 的两张表抄录成一张完整的「信号 → 来源 → 去向」对照表，特别标注出 `BCK_I` 未连接这一项，并写下你对「为什么预留」的猜测。
3. **标注读延迟**：在 `SDPRAM_SINGLECLK` 的读路径旁，用箭头标出 `RADDR → RDATA_REG_1P → RDATA_REG_2P → RDATA_O` 的两拍延迟路径，并写明「`OUTPUT_REG="TRUE"` ⇒ 2 个 MCLK」。
4. **验证规模**：打开 [BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex)，确认行数（256）与位宽（每行 8 hex = 32 bit）分别对应 `ADDR_WIDTH=8` 与 `DATA_WIDTH=32`。
5. **（可选）仿真确认**：按 4.3.4 在 Questa 中观察 `RDATA_O` 相对 `RADDR` 的 2 拍延迟（待本地验证）。

完成后再回头看 [FIR_x2.v:103-115](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/07_FIR_x2/FIR_x2.v#L103-L115) 顶层的实例化，你应该能立刻说清每一个参数取值的来源、每一根线的去向——这就达到了本讲的目标。

## 6. 本讲小结

- DATA_BUFFER 是一个**纯结构化封装模块**：内部无 `always`、无 `assign`，只声明 4 根内部线并实例化控制器 + 原语。
- 它把**控制器 DPRAM_CONT**（管地址与使能）和**原语 SDPRAM_SINGLECLK**（管纯粹存取）拼接成一个「给我样点、还你样点」的黑盒，体现了项目「控制器 / 原语」分层、原语可被厂商替换的设计。
- 4 根内部控制线 `WEN/WADDR/REN/RADDR` 一一对接控制器的 `_O` 端口与原语的 `_I` 端口；数据 `WDATA_I/RDATA_O` 和时钟 `MCLK_I` 直连原语；`BCK_I` 在本模块内未连接。
- `OUTPUT_REG` 是读路径的行为开关：`"TRUE"`（顶层固定取值）→ 两级寄存 → **读数据相比读地址滞后 2 个 MCLK**；`"FALSE"` → 一级寄存 → 滞后 1 个 MCLK。
- `BUFFER_INIT.hex` 是 256×32bit 的全零初始化镜像，行数与位宽分别匹配 `ADDR_WIDTH=8`、`DATA_WIDTH=32`，经 `$readmemh` 在仿真起始灌入 RAM。

## 7. 下一步学习建议

本讲只完成了「封装与连线」这层视角。要真正理解 DATA_BUFFER 的行为，需要分别向下钻到它的两个子模块：

- **u3-l2（DPRAM_CONT）**：进入控制器内部，搞懂它如何用 LRCK 上升沿检测驱动写地址、如何让读地址领先写地址一圈、以及复位期间的预填充时序。本讲 4.2 里看到的 `REN_O = ~(WEN_REG & ...)` 与环形窗口都在那里展开。
- **u3-l3（SDPRAM_SINGLECLK）**：进入原语内部，系统对比双口 RAM 与单口 ROM（系数通路用）的端口差异，并完整理解 `$readmemh` 与两级读寄存器的 generate 分支。

读完 u3-l2 / u3-l3 后，建议再回到本讲，把「控制器算出的地址」与「原语按地址返回的数据」在脑海里连成一条完整的时间线，为 u4（系数通路）和 u5（乘法累加）的流水线对齐分析打好基础。
