# Verilog 库总览与 SystemVerilog 风格

## 1. 本讲目标

u1-l2 在「施工地图」里只给 `ThreePart/projf-explore/lib/` 留了一个定位点：它是「成体系的 SystemVerilog 教学库，分了 clock/display/graphics/maths… 几个区」。本讲把那个点放大成一张完整的内部导览图。

学完本讲你应当能够：

1. **说出 projf 库的分区结构与各分区职责**，并知道每个分区里有哪些代表性模块、放在哪个文件。
2. **读懂 projf 使用的 SystemVerilog 子集**——`logic`、`always_comb`、`always_ff`、`$clog2`、`enum`、`.clk(clk)` 简写——并能说清楚它们比传统 Verilog 2001「好在哪里、安全在哪里」。
3. **理解「厂商中立（vendor-neutral）」设计思想**：库如何用 `xc7/` 与 `ice40/` 两个子目录隔离厂商原语、用 `null/` 空模块骗过 lint、又如何让综合工具自动推断 Block RAM，从而在 Xilinx 7 系列与 Lattice iCE40 之间几乎「零改動」移植。

本讲是 Unit 5（projf 库基础模块）的入口。后续 u5-l2~u5-l5 会分别钻进 clock、memory、uart、essential 等具体分区精读；本讲只负责让你「先把整座图书馆的楼层索引背下来」，再决定去哪一层。

---

## 2. 前置知识

本讲默认你已经读过 u1-l2（仓库目录地图），知道 `ThreePart/projf-explore/` 是什么、放在哪里。在进入正题前，先快速厘清几个本讲反复出现的术语（与 u2-l1 讲过的 Verilog 基础互补，不重复展开语法细节）：

- **Verilog / SystemVerilog**：Verilog 是硬件描述语言（HDL）；SystemVerilog（缩写 SV）是 Verilog 的超集，增补了一批让代码更好写、更不容易出错的特性。本库**只用了 SV 的一个很小子集**，不是在用面向对象那套（那是仿真验证用的，见 u3-l5 的 VE_sv）。
- **模块（module）**：一段可复用的硬件设计，有输入输出端口。projf 库的每个 `.sv` 文件基本就是一个模块。
- **综合（synthesis）**：把 Verilog 文本翻译成 FPGA 上的真实电路（查找表 LUT、触发器 FF、Block RAM、DSP…）。
- **厂商原语（vendor primitive）**：某家 FPGA 厂商独有的、不可用纯 RTL 描述的底层硬件单元。例如 Xilinx 的 `MMCME2_BASE`（时钟管理单元）、`BUFG`（全局时钟缓冲），Lattice iCE40 的 `SB_PLL40_PAD`（PLL）、`SB_IO`（IO 单元）。**厂商原语是「厂商中立」的唯一障碍**——其余逻辑都能跨平台。
- **Block RAM（BRAM）**：FPGA 芯片里自带的专用存储块（几十 Kb 一块）。projf 不直接例化厂商的 BRAM 原语，而是**用普通 RTL 写 RAM，让综合工具自动推断成 BRAM**——这也是厂商中立思想的一部分。

> 关键认知：projf 库不是「一堆能跑的 demo」，而是一座**有设计哲学的可复用基础设施**。它的三大主张——「按领域分区」「用 SV 子集让 Verilog 更安全」「厂商中立可移植」——会贯穿 Unit 5~7 的每一讲。本讲先把这三个主张讲透。

---

## 3. 本讲源码地图

本讲主要阅读以下文件（README 给出「自述」，`.sv` 文件给出「实证」）：

| 文件 | 作用 |
| --- | --- |
| `ThreePart/projf-explore/lib/README.md` | 库总览：列出所有分区、声明厂商中立与 SV 风格 |
| `ThreePart/projf-explore/README.md` | projf-explore 项目总览，含同样的「SystemVerilog?」声明 |
| `ThreePart/projf-explore/lib/maths/div.sv` | 有符号定点除法器——SV 子集的「集大成」范例（`parameter`/`logic`/`$clog2`/`enum`/`always_ff` 全用上了） |
| `ThreePart/projf-explore/lib/essential/debounce.sv` | 按键消抖——展示 `always_comb`/`always_ff` 分工的范例 |
| `ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv` | Xilinx 7 系列时钟生成（`MMCME2_BASE` + `BUFG`）——厂商原语范例 |
| `ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv` | iCE40 时钟生成（`SB_PLL40_PAD`）——同名模块的另一种平台实现 |
| `ThreePart/projf-explore/lib/null/ice40/SB_IO.sv` | 空模块——为 Verilator lint 准备的「占位」厂商原语 |
| `ThreePart/projf-explore/lib/memory/README.md` | 内存模块共性参数（含 `$clog2(DEPTH)` 自动算地址宽度） |

> 说明：本讲是「导览」而非「算法」，所以引用的 `.sv` 文件只截取能体现**风格与结构**的片段；各模块的算法细节留给 u5-l2~u5-l5。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应 projf 库的三大设计主张：

- **4.1 lib 分区**：库的「楼层索引」是怎么分的。
- **4.2 SystemVerilog 子集**：库用了哪几个 SV 特性、为什么。
- **4.3 厂商中立**：库如何在两种 FPGA 架构间保持可移植。

---

### 4.1 lib 分区：库的楼层索引

#### 4.1.1 概念说明

projf 库（`ThreePart/projf-explore/lib/`）是一个「按领域（area）分目录」的可复用 Verilog 设计集合。它的设计原则很朴素：**一个目录只管一类事**。时钟归时钟、显示归显示、画图归画图……这样你写一个新工程，只需「按需取用」某几个目录，而不必拖进整个库。

这种「领域驱动的目录划分」和 u2-l1 里 AES 核心的「按角色分目录」（src/utils/gf_s_box/tb…）思路一致，但维度不同：AES 是按「一个工程内部的文件角色」分，projf 库是按「跨工程可复用的功能领域」分。

#### 4.1.2 核心流程

库根 README 把所有分区一次性列了出来（[ThreePart/projf-explore/lib/README.md:L5-L16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L5-L16)）：

```
clock      - clock generation (PLL) and domain crossing
display    - display timings, framebuffer, DVI/HDMI output
essential  - handy modules for many designs
graphics   - drawing lines and shapes
maths      - divide, LFSR, square root...
memory     - roms and ram designs, including BRAM
null       - null modules for linting
res        - palettes, fonts, and resource files for testing
uart       - UART (serial) transmitter/receiver
```

把这份「声明」和实际目录里的代表性模块对应起来（实测，`*.sv` 为设计源、`xc7/` 子目录放 Xilinx 专属实现与 testbench）：

| 分区 | 职责（一句话） | 代表模块（实测文件） | 后续精读讲义 |
| --- | --- | --- | --- |
| `clock/` | 生成像素时钟（PLL）、跨时钟域同步 | `xd.sv`、`xc7/clock_480p.sv`、`ice40/clock_480p.sv` | u5-l2 |
| `display/` | 显示时序、行场同步、DVI/HDMI 编码输出 | `display_480p.sv`、`tmds_encoder_dvi.sv`、`linebuffer_simple.sv` | u6-l1/u6-l2 |
| `essential/` | 各处都要用的公用小模块 | `debounce.sv`、`xc7/async_reset.sv` | u5-l5 |
| `graphics/` | 画线、画形状（硬件绘图原语） | `draw_line.sv`、`draw_circle.sv`、`draw_triangle_fill.sv` | u6-l3 |
| `maths/` | 除法、乘法、开方、LFSR、正弦表 | `div.sv`、`mul.sv`、`sqrt.sv`、`lfsr.sv`、`sine_table.sv` | u7-x |
| `memory/` | ROM、RAM、可推断为 BRAM 的存储 | `bram_sdp.sv`、`rom_sync.sv`、`rom_async.sv` | u5-l3 |
| `uart/` | 串口收发器 | `uart_tx.sv`、`uart_rx.sv`、`uart_baud.sv` | u5-l4 |
| `null/` | **空模块**，仅供 lint，不参与综合 | `ice40/SB_IO.sv` | 本讲 4.3 |
| `res/` | 资源文件：调色板、字库、测试图 | `maths/res/sine_table_64x8.mem`、`res/fonts/…` | 散见各讲 |

> 注意三个容易被忽略的分区：`null/` 是「骗 lint」用的，4.3 节详讲；`res/` 不是 RTL 而是 `.mem`/字体等数据资源；`essential/` 名字最朴素，却装了 `debounce` 这种「几乎所有上板工程都要用」的基础件。spec 里强调的「七个分区」指的是 clock/display/essential/graphics/maths/memory/uart 这七个**功能分区**，`null` 与 `res` 属于辅助分区。

#### 4.1.3 源码精读

库 README 开篇就给项目定了性：「handy Verilog designs for everyone」「freely build on these MIT licensed designs」（[ThreePart/projf-explore/lib/README.md:L1-L3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L1-L3)）——即这是一个**面向所有人、MIT 许可、可自由复用**的基础库。

每个功能分区都**自带一份 README**，用相同的结构说明「这个区有哪些模块、哪些有 testbench、相关博客在哪」。例如 `clock/README.md` 把本区的模块逐个列出（[ThreePart/projf-explore/lib/clock/README.md:L5-L16](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/README.md#L5-L16)）：

```
xd.sv                 - clock domain crossing (CDC) with pulse
ice40/clock_480p.sv   - 25.125 MHz clock for VGA 640x480 ~60 Hz
xc7/clock_480p.sv     - 25.2 & 126 MHz clocks for VGA 640x480 60Hz
xc7/clock_720p.sv     - 74.25 & 371.25 MHz clocks for 1280x720 60Hz
...
```

这里已经能看到厂商中立思想的雏形：**同一个功能（生成 480p 像素时钟）在 `xc7/` 和 `ice40/` 下各有一份实现**。这一点 4.3 节展开。

`memory/README.md` 则用一张共性参数表统一描述了该区所有模块的参数约定（[ThreePart/projf-explore/lib/memory/README.md:L26-L31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/memory/README.md#L26-L31)）：

| 参数 | 含义 |
| --- | --- |
| `WIDTH` | 数据位宽（未来可能改名 `DATAW`） |
| `DEPTH` | 存储深度（元素个数） |
| `INIT_F` | 初始化时装入的 `.mem` 数据文件 |
| `ADDRW` | 地址位宽，**默认由 `$clog2(DEPTH)` 自动算出** |

注意 `ADDRW` 那一行——它直接印证了 4.2 节要讲的 `$clog2`：你只要告诉它「存储多深」，地址位宽它帮你算，不用手算还可能算错。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：核对「README 声明的分区」与「磁盘上的实际目录」是否一致，并为每个分区挑出一个代表模块，作为后续精读的锚点。

**操作步骤**（在仓库根目录执行，均为只读命令）：

```bash
# 1. 列出 lib 下的所有子目录（即分区）
ls -1 ThreePart/projf-explore/lib

# 2. 统计每个分区里有多少个设计文件（.sv）
for d in clock display essential graphics maths memory uart; do
  n=$(ls -1 ThreePart/projf-explore/lib/$d/*.sv 2>/dev/null | wc -l)
  echo "$d: $n 个 .sv"
done

# 3. 抽查一个分区 README，看它的「模块清单」结构
sed -n '5,16p' ThreePart/projf-explore/lib/uart/README.md
```

**需要观察的现象**：

- 第 1 条应列出 clock/display/essential/graphics/maths/memory/null/res/uart 等目录，与库 README 的声明吻合。
- 第 2 条中，`graphics` 和 `maths` 的 `.sv` 数量通常最多（绘图原语和数学运算各有十来个模块）。
- 第 3 条会看到 uart 的模块清单：`uart_baud.sv` / `uart_rx.sv` / `uart_tx.sv`。

**预期结果**：你能在脑子里给每个分区钉上一个「代表模块」，例如 clock→`xd.sv`、essential→`debounce.sv`、maths→`div.sv`、memory→`bram_sdp.sv`、uart→`uart_tx.sv`。本讲后续会反复用到这几个名字。

#### 4.1.5 小练习与答案

**练习 1**：`null/` 和 `res/` 为什么不算是「功能分区」？

**参考答案**：功能分区（clock/display/…）里放的是**会被综合进电路的 RTL 模块**。`null/` 里是空的占位模块，注释明确写「For Verilator linting - don't include in synthesis」（见 4.3 节），不进电路；`res/` 里是 `.mem` 数据、字体、调色板等**资源文件**，是给 ROM 初始化用的数据，本身也不是 RTL。所以二者是「辅助分区」，与七大功能分区性质不同。

**练习 2**：projf 库每个分区都配了一份 README，这种做法相比「只放源码」有什么好处？

**参考答案**：README 把「本区有哪些模块、各自做什么、哪些有 testbench、相关博客链接」集中说明，让复用者不必逐个打开 `.sv` 读注释就能选型。这降低了库的使用门槛，也便于长期维护——新增模块时只需在分区 README 里加一行。这是「工程级开源库」与「随手丢的脚本」的重要区别（对比 u1-l2 里 hardwarebee 那种只有网盘链接、没有模块说明的碎片化收录）。

---

### 4.2 SystemVerilog 子集：让 Verilog 更安全的五个特性

#### 4.2.1 概念说明

库 README 专门有一节标题就叫「SystemVerilog?」（[ThreePart/projf-explore/lib/README.md:L34-L45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L34-L45)），用问号表达了一种克制的态度：**不是「全面拥抱 SV」，而是「只挑了几个最值的小特性」**。projf-explore 根 README 里有完全相同的一段（[ThreePart/projf-explore/README.md:L81-L91](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/README.md#L81-L91)），说明这是整个项目统一坚持的风格。

这五个特性是：

1. `logic` 类型——替代 `wire`/`reg`，更安全。
2. `always_comb` / `always_ff`——让组合逻辑和时序逻辑「意图分明」。
3. `$clog2`——自动计算地址等向量的位宽。
4. `enum`——让有限状态机（FSM）的可读性大增。
5. 端口连接简写 `.clk`（等价于 `.clk(clk)`）——少打字、少出错。

这些特性都**兼容近期版本的 Verilator、Yosys、Icarus Verilog 和 Xilinx Vivado**（README 同节末尾的保证），所以用了它们不会把工具链门槛抬高。

> 和 u2-l1 的关系：u2-l1 在 AES 核心里讲的是**传统 Verilog 2001**（`wire`/`reg`、`` `define `` 宏、`always @(...)`）。projf 库用的是 **SystemVerilog 子集**，二者是不同年代、不同风格。对比着读，你会更直观地感到 SV 子集「为什么更安全」。注意 u3-l5 讲的 VE_sv 那种 `class`/`interface`/`program` 是** SV 面向对象验证**，远超这里的「可综合子集」，不要混为一谈。

#### 4.2.2 核心流程

下面用一张表把五个特性的「传统 Verilog 写法 → projf 写法 → 收益」讲清，随后用真实源码逐一坐实。

| 特性 | 传统 Verilog 2001 | projf（SV 子集） | 收益 |
| --- | --- | --- | --- |
| 数据类型 | `wire`（连线）/ `reg`（可赋值）二选一，易错 | 统一用 `logic` | 不再纠结该用 wire 还是 reg；编译器替你查多重驱动 |
| 组合/时序 | 都写成 `always @(...)`，靠敏感列表区分 | `always_comb` / `always_ff @(posedge clk)` | 意图自文档化；综合工具能查「组合逻辑意外锁存」等错误 |
| 位宽计算 | 手算 `ADDRW`，改 `DEPTH` 后易忘改 | `$clog2(DEPTH)` 自动算 | 参数化设计不漏改 |
| 状态机 | 状态用数字/`localparam` 编号 | `enum {IDLE, INIT, ...} state;` | 状态有名字、有类型检查 |
| 例化连线 | `.clk(clk)` 每个端口写两遍 | `.clk` 同名简写 | 少打字、名字笔误会被工具抓住 |

#### 4.2.3 源码精读

**(1) `logic` + `always_comb`/`always_ff`：以 `debounce.sv` 为例**

按键消抖模块 `debounce.sv` 是 SV 子集最干净的展示。先看端口声明（[ThreePart/projf-explore/lib/essential/debounce.sv:L8-L14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L8-L14)）：

```verilog
module debounce (
    input  wire logic clk,   // clock
    input  wire logic in,    // signal input
    output      logic out,   // signal output (debounced)
    output      logic ondn,  // on down (one tick)
    output      logic onup   // on up (one tick)
    );
```

这里每个端口都是 `logic` 类型。传统 Verilog 要纠结「输出 `out` 到底声明成 `wire` 还是 `reg`」（取决于它由 `assign` 还是由 `always` 驱动），SV 的 `logic` 把这件事交给编译器：你只声明类型，编译器按驱动方式决定综合成连线还是触发器，并且**会检查「一个 `logic` 是否被多个源驱动」这种经典 bug**。

接着看本模块如何把组合逻辑与时序逻辑分开（[ThreePart/projf-explore/lib/essential/debounce.sv:L17-L37](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/essential/debounce.sv#L17-L37)）：

```verilog
// 同步 + 抗亚稳态：打两拍
always_ff @(posedge clk) sync_0 <= in;
always_ff @(posedge clk) sync_1 <= sync_0;
...
// 组合逻辑：判断 idle/max 与产生单拍脉冲
always_comb begin
    idle = (out == sync_1);
    max  = &cnt;
    ondn = ~idle & max & ~out;
    onup = ~idle & max & out;
end
// 时序逻辑：计数与翻转
always_ff @(posedge clk) begin
    if (idle) cnt <= 0;
    else begin
        cnt <= cnt + 1;
        if (max) out <= ~out;
    end
end
```

三个要点：

- `always_ff @(posedge clk)` 明确告诉工具「这是时钟驱动的时序逻辑，综合成触发器」；`always_comb` 明确告诉工具「这是纯组合逻辑」。
- `always_comb` 有个隐含的好处：工具会**检查敏感列表是否完整**，并在「组合逻辑里某个分支没赋值（会意外生成锁存器）」时报错——传统 `always @(...)` 你得自己手写敏感列表，漏一个信号就静默出错。
- 打两拍（`sync_0`→`sync_1`）是经典的**亚稳态缓解**手法：外部异步信号先经过两级触发器同步到本时钟域再使用。这部分的具体原理留给 u5-l2。

**(2) `$clog2` + `enum` + `always_ff` 状态机：以 `div.sv` 为例**

有符号定点除法器 `div.sv` 是 SV 子集的「集大成者」，五个特性里它占了四个。先看它的模块声明（[ThreePart/projf-explore/lib/maths/div.sv:L8-L23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L8-L23)）：

```verilog
module div #(
    parameter WIDTH=8,  // 数的位宽（整数+小数）
    parameter FBITS=4   // WIDTH 内的小数位数
    ) (
    input wire logic clk, ...
    input wire logic signed [WIDTH-1:0] a,   // 被除数
    input wire logic signed [WIDTH-1:0] b,   // 除数
    output     logic signed [WIDTH-1:0] val  // 商
    );
```

注意 `signed` 关键字让 `a/b/val` 成为有符号数，综合时正确映射成补码运算——这是定点数学（u7 系列主题）的基础。

`$clog2` 用在迭代计数器上（[ThreePart/projf-explore/lib/maths/div.sv:L29-L30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L29-L30)）：

```verilog
localparam ITER = WIDTHU + FBITS;     // 迭代次数
logic [$clog2(ITER):0] i;             // 迭代计数器（位宽自动算）
```

`$clog2(ITER)` 返回「能表示 0..ITER-1 所需的最小位数」。你只要改 `WIDTH`/`FBITS`，`i` 的位宽自动跟着变，**绝不存在「改了参数却忘改计数器位宽」的隐患**。这正是 memory 区用 `$clog2(DEPTH)` 算 `ADDRW` 的同一个道理（见 4.1.3）。

`enum` + `always_ff` 组成的状态机是本模块的骨架（[ThreePart/projf-explore/lib/maths/div.sv:L54-L55](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/div.sv#L54-L55) 及其后的 `case`）：

```verilog
enum {IDLE, INIT, CALC, ROUND, SIGN} state;
always_ff @(posedge clk) begin
    ...
    case (state)
        INIT:  begin state <= CALC; ... end
        CALC:  begin ... if (i == ITER-1) state <= ROUND; ... end
        ROUND: begin state <= SIGN; ... end   // 高斯舍入
        SIGN:  begin state <= IDLE; ... end    // 符号修正
        default: begin ... end                 // IDLE
    endcase
end
```

如果用传统 Verilog，状态要用 `localparam IDLE=0, INIT=1, ...` 手工编号，`case` 里也得写数字或宏，既啰嗦又容易写错编号。`enum` 给状态一组带名字的枚举值，工具会做**类型检查**（例如你拼错 `IDEL` 会被报错，而传统宏拼错往往变成一个未定义的新状态）。除法的算法（恢复余数法迭代、高斯舍入、符号修正）细节留给 u7-l2，本讲只欣赏它的**写法**。

**(3) 端口连接简写 `.clk`**

这种简写无法用一个具体行号单独点出——它遍布全库的例化处。其规则是：例化子模块时，若外部信号名与端口名**相同**，可省略括号，把 `.clk(clk)` 写成 `.clk`。库 README 把它列为第 5 个特性（[ThreePart/projf-explore/lib/README.md:L42](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L42)）。它真正的价值不是少打几个字，而是：**工具能在两端名字不一致（即你写错）时报错或告警**，传统写法 `.clk(clk_pix)` 则静默地把时钟接错。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：在真实 `.sv` 文件里亲手「圈出」SV 的五个特性，确认它们不是 README 的空话。

**操作步骤**（只读）：

```bash
cd ThreePart/projf-explore/lib

# 1. 统计全库出现各 SV 特性的次数
echo "logic 出现次数:        $(grep -rh '\blogic\b' --include='*.sv' . | wc -l)"
echo "always_comb 出现次数:  $(grep -rh 'always_comb' --include='*.sv' . | wc -l)"
echo "always_ff 出现次数:    $(grep -rh 'always_ff' --include='*.sv' . | wc -l)"
echo "\$clog2 出现次数:       $(grep -rh 'clog2' --include='*.sv' . | wc -l)"
echo "enum 出现次数:         $(grep -rh '\benum\b' --include='*.sv' . | wc -l)"

# 2. 找出全库使用了 enum 的模块（即含状态机的模块）
grep -rl '\benum\b' --include='*.sv' .
```

**需要观察的现象**：

- `logic` 的次数远多于其它特性——几乎所有端口和内部信号都用 `logic`，它是全库的基础。
- `enum` 只出现在少数几个含状态机的模块里（如 `div.sv`、`uart_tx.sv`、`uart_rx.sv`），符合「状态机才用 enum」。
- 第 2 条命令的结果应是 `div.sv` 等少数文件。

**预期结果**：你能给出每个特性在库里的「典型出处」，例如 `logic`→几乎所有端口、`$clog2`→`div.sv`/`bram_sdp.sv`、`enum`→`div.sv`/`uart_tx.sv`。这证明 README 的「SystemVerilog?」声明是有真实代码支撑的，而非宣传。

#### 4.2.5 小练习与答案

**练习 1**：为什么 projf 坚持用 `logic` 而不是 `wire`/`reg`？给一个传统写法容易出 bug、而 `logic` 能帮上忙的具体场景。

**参考答案**：传统 Verilog 里，一个信号该用 `wire` 还是 `reg` 取决于驱动方式：`assign`/例化连线驱动的用 `wire`，`always` 块驱动的用 `reg`。初学者经常在 `always` 里驱动却声明成 `wire`，或在 `assign` 里驱动却声明成 `reg`，导致编译报错甚至综合出非预期电路。`logic` 让你不必预判驱动方式，由编译器按上下文决定；同时它会检查「一个 `logic` 信号被多个源驱动」（多重驱动，经典 bug）并报错。所以 `logic` 既省心又更安全。

**练习 2**：`always_comb` 相比 `always @(*)` 有什么实质好处？

**参考答案**：二者都表示组合逻辑，但 `always_comb` 有三点更强：(1) 工具**自动**生成完整敏感列表，`always @(*)` 的敏感列表由工具推断，某些老工具/边角情形推断不全；(2) `always_comb` 会在「某条分支未对所有输出赋值」时**警告可能生成锁存器**，而组合逻辑里混入锁存器通常是 bug；(3) `always_comb`「意图自文档化」，读代码的人一眼知道这是组合逻辑，而 `always @(*)` 还需结合块内语句判断。

---

### 4.3 厂商中立：在两种 FPGA 架构间可移植

#### 4.3.1 概念说明

「厂商中立（vendor-neutral）」是 projf 库的第二大主张。它的含义是：**尽量用可在任何 FPGA 上综合的纯 RTL，把不可避免的厂商专属部分隔离到最小、最清晰的位置**。

为什么「不能完全中立」？因为有些硬件功能只能调用厂商原语才能用，最典型的是**时钟生成（PLL/MMCM）**——FPGA 里的锁相环是硬核，每家厂商的原语名、参数都不同，纯 RTL 写不出来。

projf 的对策是「双实现 + 占位」：

- 同一个功能，在 `xc7/`（Xilinx 7 系列）和 `ice40/`（Lattice iCE40）下**各写一份**，对外暴露相同的端口名，让上层调用者无感切换。
- 对那些「只有仿真/lint 才需要的厂商原语」，用 `null/` 下的空模块占位，骗过 Verilator 的 lint，但**绝不参与综合**。
- 存储器不调厂商 BRAM 原语，而是写普通 RTL，**让综合工具推断**出 BRAM。

库 README 把支持的两种架构和它们各自的原语列得很清楚（[ThreePart/projf-explore/lib/README.md:L19-L32](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L19-L32)）：

```
XC7   - Xilinx 7 Series (Spartan-7, Artix-7)
  BUFG, MMCME2_BASE ; HDMI: OBUFDS, OSERDES2
iCE40 - Lattice iCE40 (UltraPlus)
  SB_IO, SB_PLL40_PAD, SB_SPRAM256KA
We also infer block ram (BRAM); see memory.
```

#### 4.3.2 核心流程

厂商中立在目录结构上的体现可以用三句话概括：

1. **纯逻辑模块**（除法、消抖、画线……）直接放在分区根目录，无前缀，XC7 与 iCE40 都能用。
2. **需要厂商原语的模块**放到 `xc7/` 或 `ice40/` 子目录，两端各一份，**端口尽量一致**。
3. **testbench 与波形配置**通常只给 `xc7/` 一份（Vivado 仿真），因为仿真不挑板子。

```
lib/clock/
├── xd.sv                 纯 RTL（跨时钟域），双平台通用
├── xc7/clock_480p.sv     Xilinx 版：MMCME2_BASE + BUFG
├── xc7/clock_720p.sv     Xilinx 版：720p
├── ice40/clock_480p.sv   Lattice 版：SB_PLL40_PAD
└── xc7/clock_tb.sv       testbench（仅 xc7）
```

移植到新架构（比如 Intel/Altera）时，只需在新建的 `cyclone/`（或类似）目录里照着端口写一份 `clock_480p`，上层调用代码完全不动——这就是 README 说的「Porting to other architectures should be straightforward」。

#### 4.3.3 源码精读

**(1) 同名模块的双实现：`clock_480p` 的 XC7 版 vs iCE40 版**

Xilinx 7 系列版用 `MMCME2_BASE`（混合模式时钟管理器）生成 25.2 MHz 像素时钟与 5× 的 126 MHz（给 DVI 的 10:1 串化用），再用 `BUFG` 走全局时钟网络（[ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv:L30-L61](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L30-L61)）：

```verilog
MMCME2_BASE #(
    .CLKFBOUT_MULT_F(MULT_MASTER), .CLKIN1_PERIOD(IN_PERIOD),
    .CLKOUT0_DIVIDE_F(DIV_5X), .CLKOUT1_DIVIDE(DIV_1X), .DIVCLK_DIVIDE(DIV_MASTER)
) MMCME2_BASE_inst (
    .CLKIN1(clk_100m), .RST(rst),
    .CLKOUT0(clk_pix_5x_unbuf), .CLKOUT1(clk_pix_unbuf), ...
);
BUFG bufg_clk(.I(clk_pix_unbuf), .O(clk_pix));            // 全局缓冲
BUFG bufg_clk_5x(.I(clk_pix_5x_unbuf), .O(clk_pix_5x));
```

Lattice iCE40 版用 `SB_PLL40_PAD` 实现同样目标（生成 25.125 MHz），并把输出直接走全局网络（`PLLOUTGLOBAL`），见 [ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv:L25-L37](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/ice40/clock_480p.sv#L25-L37)：

```verilog
SB_PLL40_PAD #(
    .FEEDBACK_PATH(FEEDBACK_PATH), .DIVR(DIVR), .DIVF(DIVF), .DIVQ(DIVQ), ...
) SB_PLL40_PAD_inst (
    .PACKAGEPIN(clk_12m), .PLLOUTGLOBAL(clk_pix), .RESETB(rst), .BYPASS(1'b0), .LOCK(locked)
);
```

两份实现的**输入时钟不同**（XC7 假设 100 MHz 板钟，iCE40 假设 12 MHz 板钟）、**原语不同**（`MMCME2_BASE` vs `SB_PLL40_PAD`）、**参数模型完全不同**（XC7 用乘除法 MULT/DIV，iCE40 用 DIVR/DIVF/DIVQ 寄存器位），但它们都对外输出同名的 `clk_pix` 与 `clk_pix_locked`。上层（如显示控制器）只认这两个名字，根本不知道底下是 Xilinx 还是 Lattice。

> 一个贯穿全库的纪律：**「锁相环稳定」前别用生成的时钟**。两份实现都把 `locked` 信号打一拍同步后输出 `clk_pix_locked`，clock README 还给出范例：用 `always_ff @(posedge clk_pix) rst_pix <= !clk_pix_locked;` 把显示控制器按住，直到时钟稳定（[ThreePart/projf-explore/lib/clock/README.md:L22-L33](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/README.md#L22-L33)）。这个「等 lock」的纪律在 u5-l2/u6-l1 还会遇到。

**(2) `null/` 空模块：为 lint 准备的占位**

有些厂商原语（如 iCE40 的 `SB_IO`）只在真实芯片上有意义，仿真/lint 时并不存在。Verilator 做 lint（静态检查）时若遇到例化了 `SB_IO` 却找不到定义，会报错。projf 的办法是在 `null/ice40/SB_IO.sv` 里放一个**空壳**（[ThreePart/projf-explore/lib/null/ice40/SB_IO.sv:L6-L23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/null/ice40/SB_IO.sv#L6-L23)）：

```verilog
// NB. For Verilator linting - don't include in synthesis
module SB_IO #( parameter PIN_TYPE ) (
    output      logic PACKAGE_PIN,
    input  wire logic OUTPUT_CLK,
    input  wire logic D_OUT_0,
    input  wire logic D_OUT_1
    );
    // NULL MODULE
endmodule
```

它的端口签名照抄真原语，但函数体是空的。这样 Verilator lint 能通过；而综合时你**绝不**把它加进文件列表——真实综合时，Yosys/Vivado 会自动用芯片自带的 `SB_IO`。注释里那句「don't include in synthesis」就是给使用者的明确警告。

**(3) BRAM 推断：用 RTL「写得像 RAM」，让综合工具认出来**

厂商中立的第三招是不直接例化厂商的 Block RAM 原语，而是写**风格规范的 RTL**，让综合工具识别出「这段代码就是一个双口 RAM」并自动映射到芯片的 BRAM 资源。库 README 一句「We also infer block ram (BRAM); see memory」（[ThreePart/projf-explore/lib/README.md:L30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/README.md#L30)）指的就是这招。具体写法（`bram_sdp.sv`）留给 u5-l3 精读，本讲只需记住结论：**可推断 BRAM 的 RTL 天然跨平台**，XC7 和 iCE40 各自的综合器都会把它映成自家的 Block RAM/SPRAM，无需你写两份。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：在库里找出「纯 RTL（双平台通用）」与「厂商专属（分目录）」的分界线，验证厂商中立思想确实落地。

**操作步骤**（只读）：

```bash
cd ThreePart/projf-explore/lib

# 1. 找出所有「厂商专属子目录」（xc7/ ice40/），看哪些分区有、哪些没有
echo "=== 含 xc7/ 子目录的分区 ==="
find . -type d -name xc7
echo "=== 含 ice40/ 子目录的分区 ==="
find . -type d -name ice40

# 2. 在所有 .sv 里搜索厂商原语，统计它们只出现在哪里
echo "=== MMCME2_BASE 出现位置 ==="
grep -rl 'MMCME2_BASE' --include='*.sv' .
echo "=== SB_PLL40_PAD 出现位置 ==="
grep -rl 'SB_PLL40_PAD' --include='*.sv' .
echo "=== BUFG 出现位置 ==="
grep -rl 'BUFG' --include='*.sv' .
```

**需要观察的现象**：

- 第 1 条：只有 `clock/`、`display/`、`essential/`、`memory/` 等少数分区有 `xc7/` 或 `ice40/` 子目录；`maths/`、`graphics/`、`uart/` 的设计源**直接在分区根**，没有厂商子目录——因为除法、画线、串口这些是纯逻辑，不碰原语。
- 第 2 条：`MMCME2_BASE`/`BUFG` 只出现在 `xc7/` 下，`SB_PLL40_PAD` 只出现在 `ice40/` 下；纯逻辑分区（maths/graphics/uart）里**搜不到任何厂商原语**。

**预期结果**：你能画出一条清晰的「厂商分界线」——**只有触及时钟生成、差分 IO、片上存储这几类硬核资源时，才需要分 `xc7/`/`ice40/` 双实现；其余逻辑天然跨平台**。这正是 projf 能宣称「vendor-neutral」的实证基础。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `div.sv`（除法器）不需要 `xc7/` 和 `ice40/` 两份实现，而 `clock_480p` 却必须分两份？

**参考答案**：除法器是**纯逻辑运算**（加法器、移位、比较、寄存器），这些基本元件在 XC7 和 iCE40 上都由通用的 LUT/FF/进位链实现，RTL 完全一样，所以一份就够。而像素时钟必须靠芯片里的**锁相环硬核**生成，Xilinx 的 `MMCME2_BASE` 与 Lattice 的 `SB_PLL40_PAD` 是两套完全不同的原语（名字、参数、端口都不同），无法用同一份 RTL 表达，所以必须各写一份。一句话：**纯逻辑可中立，硬核原语必须分平台**。

**练习 2**：`null/ice40/SB_IO.sv` 为什么端口签名要和真原语一模一样、函数体却是空的？

**参考答案**：端口签名一致，是为了让**例化了 `SB_IO` 的设计能在 Verilator lint/仿真时通过编译**——Verilator 需要找到模块定义，只要端口对得上、哪怕体是空的也能 lint。函数体为空，是因为这个空壳**只服务于 lint，绝不进综合**；真正综合时由 Yosys 用芯片自带的 `SB_IO` 硬原语替换。如果体里写了真实逻辑，反而会和真原语冲突。注释「don't include in synthesis」就是提醒使用者别把它加进综合文件列表。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一份 **projf 库「分区 + 风格 + 平台依赖」速查表**。这是本讲的核心交付物，也是你阅读 u5-l2~u5-l5 时的速查索引。

**任务**：浏览 `ThreePart/projf-explore/lib/` 下的七大功能分区，制作一张表，每个分区占一行，包含 4 列：

| 目录名 | 职责（一句话） | 代表模块（带文件名） | 依赖的厂商原语（如 BUFG、SB_IO）|

**示例行（请仿照补全）**：

| 目录名 | 职责 | 代表模块 | 依赖的厂商原语 |
| --- | --- | --- | --- |
| `clock` | 时钟生成与跨时钟域 | `xd.sv`、`xc7/clock_480p.sv` | `MMCME2_BASE`、`BUFG`（XC7）；`SB_PLL40_PAD`（iCE40） |
| `display` | 显示时序与 DVI/HDMI 输出 | `display_480p.sv`、`tmds_encoder_dvi.sv` | `OBUFDS`、`OSERDES2`（XC7，HDMI）；iCE40 差分用 `SB_IO` |
| `essential` | 公用小模块 | `debounce.sv` | （基本无；`xc7/async_reset.sv` 触及复位原语） |
| `graphics` | 画线与形状 | … | …（你来填） |
| `maths` | 除法/乘法/开方/LFSR/正弦 | … | …（你来填） |
| `memory` | ROM/RAM/BRAM | … | 推断 BRAM（XC7）/`SB_SPRAM256KA`（iCE40） |
| `uart` | 串口收发 | … | …（你来填） |

**建议步骤**：

1. 用 `ls -1 ThreePart/projf-explore/lib/<分区>` 列出每个分区的设计文件，挑一个最有代表性的填入「代表模块」列。
2. 对每个分区，用 `grep -rl 'MMCME2_BASE\|BUFG\|SB_PLL40_PAD\|SB_IO\|OBUFDS\|OSERDES2' ThreePart/projf-explore/lib/<分区>` 检查它是否、在哪里依赖厂商原语；不依赖的就填「无（纯 RTL，跨平台）」。
3. 在表上用记号标出：**哪些分区是纯逻辑（跨平台）、哪些必须分 `xc7/`/`ice40/` 双实现**。这条线就是厂商中立的「分界线」。

**预期结果**：你得到一张可打印的速查表，既回答了「这个库有哪些零件」（4.1），又标注了「这些零件用什么风格写的」（4.2）、还点明了「哪些零件挑平台」（4.3）。后续读任何一篇 projf 讲义，先回看这张表定位。

---

## 6. 本讲小结

- projf 库（`ThreePart/projf-explore/lib/`）是一个 **MIT 许可、按领域分区**的可复用 SystemVerilog 库，七大功能分区为 `clock`/`display`/`essential`/`graphics`/`maths`/`memory`/`uart`，另有 `null`（lint 占位）与 `res`（资源文件）两个辅助分区；每个分区自带一份结构统一的 README。
- 库坚持用一个**很小的 SystemVerilog 子集**：`logic`（统一类型、查多重驱动）、`always_comb`/`always_ff`（意图分明、查锁存器）、`$clog2`（自动算位宽）、`enum`（命名状态机）、`.clk` 简写（例化同名端口）；这些特性兼容 Verilator/Yosys/Icarus/Vivado，不抬高工具门槛。
- `logic`+`always_comb`/`always_ff` 的范例如 `debounce.sv`；`$clog2`+`enum`+`always_ff` 状态机的集大成范例如 `div.sv`（有符号定点除法，`enum {IDLE,INIT,CALC,ROUND,SIGN}`）。
- 库主张**厂商中立**：纯逻辑模块放在分区根目录跨平台通用；必须用硬核原语的（时钟 PLL、差分 IO）放进 `xc7/` 与 `ice40/` 双实现、端口保持一致；`null/` 下的空壳模块只为 Verilator lint；存储器用可推断 BRAM 的 RTL 写法，避免直接例化厂商 RAM 原语。
- 三条贯穿 Unit 5~7 的纪律：**「领域分目录」「SV 子集更安全」「等时钟 lock 再用」「可推断 BRAM」**——这些会在 u5-l2（clock）、u5-l3（memory）等后续讲义反复出现。

---

## 7. 下一步学习建议

本讲把整座 projf 库的「楼层索引」背了下来，接下来可以按分区逐层精读：

- **想学时钟与跨时钟域**：进入 u5-l2，精读 `lib/clock/xd.sv`（同步器）与 `xc7/clock_480p.sv`（PLL），理解亚稳态与「等 lock」纪律。
- **想学片上存储**：进入 u5-l3，精读 `lib/memory/bram_sdp.sv`，看可推断 BRAM 的 RTL 到底怎么写。
- **想学串口通信**：进入 u5-l4，精读 `lib/uart/uart_tx.sv`/`uart_rx.sv`，看 `enum` 状态机如何驱动按位移位。
- **想直接看图形/显示**：可跳到 u6-l1（显示时序）与 u6-l3（绘图原语），但建议先过 u5-l2/u5-l3 打好时钟与存储基础。
- **想第一次点亮开发板**：跳到 u6-l6（Hello 示例），用 `hello-arty`/`hello-nexys` 三部曲把本讲的理论变成一块会闪灯、会读开关的板子。

无论走哪条线，把本讲的「分区速查表」常备手边——它会告诉你「现在在 projf 库的哪一层」。
