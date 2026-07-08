# ColorReversal——最简单的点运算

## 1. 本讲目标

本讲是「第一个 IP 核」单元的起点。我们将以全库最简单的图像处理 IP——`ColorReversal`（颜色取反）为例，把上一讲建立的「统一接口 + 两种工作模式」这套契约，落成一份真实可综合的 Verilog RTL。

读完本讲，你应当能够：

- 独立读懂一个最简点运算 IP 的完整 RTL（端口、参数、寄存器、赋值）。
- 准确解释 `always @(posedge clk or negedge rst_n or negedge in_enable)` 这个「三沿敏感列表 + 联合复位」的语义，知道它为何能让 `out_ready` 上升晚一拍、下降却立刻清零。
- 说明 `out_ready` 就绪信号是如何由同一个 always 块产生的，以及它和 `out_data` 的门控关系。
- 仿照 `ColorReversal`，自己写出一个保持统一接口、支持两种工作模式的点运算模块。

## 2. 前置知识

本讲默认你已完成 [u1-l4 标准化IP接口与两种工作模式](u1-l4-standard-interface-and-modes.md)，已经掌握以下概念。这里只做最简回顾，不展开重复：

- **统一参数**：`work_mode`（0=Pipeline 流水线、1=Req-ack 请求响应，编译期二选一）、`color_channels`（通道数）、`color_width`（每通道位宽）。数据总线总位宽恒为 `W = color_channels * color_width`。
- **统一六端口**：`clk`、`rst_n`、`in_enable`、`in_data`、`out_ready`、`out_data`。
- **握手对**：`in_enable`（上游告诉我「数据有效」）与 `out_ready`（我告诉下游「输出可读」）。
- **`in_enable` 的双重身份**：流水线模式里相当于「帧开始的二次复位门」，请求响应模式里是「逐笔请求开关」。

另外需要一点 Verilog 基础：

- `always @(posedge clk)` 是「时钟上升沿触发的时序逻辑」，`<=` 是非阻塞赋值。
- `~x` 是按位取反；`assign y = a ? b : c` 是三目条件赋值。
- `generate ... endgenerate` 用于「编译期（elaboration）展开」，配合 `if`/`for` 在综合前决定电路结构，运行时不可改变。

> 一个直觉：点运算（point operation）是「同一个像素位置、输入一个像素、输出一个像素」的运算，不依赖邻域、不依赖坐标。颜色取反 `255 - p`（8 位下）正是最朴素的点运算。它没有状态、没有缓存，理论上一个时钟就能算完，是理解整套 IP 框架最干净的切入点。

## 3. 本讲源码地图

本讲只聚焦一个文件，但会借用另外两个文件做佐证和实践：

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v) | RTL 主模块（被综合的硬件） | 本讲精读对象，全部内容围绕它 |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv) | SystemVerilog testbench | 用于「两种模式如何被例化与驱动」的实践 |
| [Point/ColorReversal/SoftwareSim/sim.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py) | 软件黄金模型 | 用于验证「RTL 取反 = 软件取反」的一致性 |

`ColorReversal.v` 一共只有 137 行，其中前 50 行是 `Com2DocHDL` 注释与 LGPL 协议头，真正的电路描述集中在第 52–137 行，而这其中真正「干活」的逻辑只有大约 20 行。本讲的全部精读就集中在这 20 行上。

## 4. 核心概念与源码讲解

本讲把 RTL 拆成三个最小模块，恰好对应它的三块电路：

1. **generate 块**：用 `generate if/else` 在编译期把 `work_mode` 二选一展开。
2. **联合复位**：用一个三沿敏感列表的 always 块同时处理 `rst_n` 与 `in_enable`，并产生 `out_ready`。
3. **取反逻辑**：`~in_data` 的按位取反，以及它在两种模式下的两种寄存写法。

### 4.1 generate 块与编译期模式选择

#### 4.1.1 概念说明

`work_mode` 是一个 `parameter`，在模块例化时（如 `ColorReversal #(0, 3, 8)`）就固定下来，**运行时不能切换**。这意味着：一个具体的硬件实例，要么是流水线模式、要么是请求响应模式，不可能两者兼具。

既然「二选一」在综合时就已确定，最自然的写法就是用 `generate` 把两种实现都写出来，让综合器根据 `work_mode` 只保留其中一支、丢弃另一支。这正是 F-I-L 全库统一采用的「双模式」实现范式。

> 注意：这里 `generate` 的作用是**条件展开**（配合 `if`），不是循环展开（配合 `for`）。尽管源码里声明了 `genvar i`，但在本模块中 `i` 并未真正用于 `for` 循环——它是 F-I-L 模板里为「按通道循环」预留的声明（在多通道移位、缩放等运算里会用到）。在 `ColorReversal` 里它被声明但未使用，属于模板遗留，不影响综合。

#### 4.1.2 核心流程

```text
编译期 elaboration 阶段：
  读到 work_mode 的具体取值（0 或 1）
        │
        ├── work_mode == 0 ──> 保留流水线支：always @(posedge clk) reg_out_data <= ~in_data;
        │                     丢弃请求响应支
        │
        └── work_mode == 1 ──> 保留请求响应支：always @(posedge in_enable) reg_out_data <= ~in_data;
                              丢弃流水线支
```

最终落到硅片上的电路，只有一支 always 块在为 `reg_out_data` 赋值，不会出现「两个 always 驱动同一个 reg」的多驱动冲突。

#### 4.1.3 源码精读

模块声明与六个端口（与上一讲的契约完全一致）：

模块与端口列表——[ColorReversal.v:L54-L60](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L54-L60)：定义 `clk/rst_n/in_enable/in_data/out_ready/out_data` 六个端口，注意它**没有**列出任何参数名，参数靠 `parameter` 在体内声明。

三个统一参数——[ColorReversal.v:L68](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L68)（`work_mode`，默认 0 流水线）、[ColorReversal.v:L73](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L73)（`color_channels`，默认 3）、[ColorReversal.v:L80](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L80)（`color_width`，默认 8）：每个参数上方都有一段 `::description / ::range` 注释，这是 `Com2DocHDL` 工具用来自动生成文档的标记。

数据总线宽度随参数动态变化——[ColorReversal.v:L100](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L100) 与 [ColorReversal.v:L110](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L110)：`in_data` / `out_data` 位宽都是 `[color_channels * color_width - 1 : 0]`，默认配置下即 24 位（RGB 各 8 位）。

`generate` 的开始与模式分支——[ColorReversal.v:L115-L135](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L115-L135)（关键，建议点开逐行看）：

```verilog
genvar i;
generate
    always @(posedge clk or negedge rst_n or negedge in_enable) begin
        if(~rst_n | ~in_enable)
            reg_out_ready <= 0;
        else
            reg_out_ready <= 1;
    end
    assign out_ready = reg_out_ready;

    if(work_mode == 0) begin
        always @(posedge clk)
            reg_out_data <= ~in_data;          // 流水线支：每拍锁存
    end else begin
        always @(posedge in_enable)
            reg_out_data <= ~in_data;          // 请求响应支：请求沿锁存
    end
    assign out_data = out_ready == 0 ? 0 : reg_out_data;
endgenerate
```

可以看到：`out_ready` 的产生（前半段）与模式无关，两种模式共用；`reg_out_data` 的锁存（后半段 `if/else`）才是模式差异所在。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `generate if/else` 是「编译期二选一」，而不是运行时分支。

**操作步骤**：

1. 打开 [ColorReversal_TB.sv:L90-L107](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L90-L107)，看 testbench 如何用不同参数例化出 6 个实例。
2. 重点观察这两组例化：
   - `ColorReversal #(0, 3, 8) CRRGBPipeline(...)` —— 第一个参数 `0` 即 `work_mode=0`。
   - `ColorReversal #(1, 3, 8) CRRGBReqAck(...)` —— 第一个参数 `1` 即 `work_mode=1`。
3. 在脑中（或在 Vivado 综合后的 Schematic 里）确认：`CRRGBPipeline` 内部只存在 `always @(posedge clk)` 那一支，`CRRGBReqAck` 内部只存在 `always @(posedge in_enable)` 那一支。

**需要观察的现象**：同一份源码，仅仅因为例化时第一个参数不同，就长成了两份不同的电路。

**预期结果**：综合后两个实例的 `reg_out_data` 寄存器，其时钟/触发沿引脚分别接到 `clk` 与 `in_enable`。若手头没有 Vivado，这一步可标注为「待本地验证」，仅通过阅读 RTL 即可确认结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `work_mode` 从 `parameter` 改成一个 `input` 端口，让它在运行时切换，这套 `generate if/else` 写法还成立吗？为什么？

> **答案**：不成立。`generate` 是编译期展开，分支条件必须是常量表达式。`work_mode` 一旦变成运行时可变的输入端口，`if(work_mode==0)` 就无法在综合时定下来，综合器会报错或忽略。这也是为什么 F-I-L 把 `work_mode` 设计成编译期参数——两种模式是两份不同的硬件。

**练习 2**：源码里声明了 `genvar i;` 却没用到它，会不会导致综合警告或资源浪费？

> **答案**：不会。`genvar` 只是给编译期 `for` 循环用的「下标变量」，声明而不使用不会生成任何电路，综合器通常忽略。它存在的原因是 F-I-L 的模块模板统一预留了这个声明，方便需要按通道展开的运算直接用。

---

### 4.2 联合复位与 out_ready 的产生

#### 4.2.1 概念说明

这是本讲最关键、也最容易被初学者误读的一块。先看这一段：

```verilog
always @(posedge clk or negedge rst_n or negedge in_enable) begin
    if(~rst_n | ~in_enable)
        reg_out_ready <= 0;
    else
        reg_out_ready <= 1;
end
assign out_ready = reg_out_ready;
```

它做了两件事：

1. **联合复位**：把 `rst_n`（全局复位）和 `in_enable`（输入使能）**一起**当作异步清零源。只要任意一个为低，`reg_out_ready` 就立刻被清 0；两个都为高时，每个时钟上升沿把它置 1。
2. **产生 `out_ready`**：`out_ready` 就是 `reg_out_ready`，由这个带三沿敏感列表的寄存器输出。

为什么要把 `in_enable` 也接进复位？回顾上一讲：`in_enable` 在流水线模式下相当于「帧开始的二次复位门」。一帧图像开始前 `in_enable=0`，此时即便有时钟，`out_ready` 也必须保持 0，告诉下游「现在没有有效输出」；当 `in_enable` 拉高表示「数据来了」，`out_ready` 才在一个时钟后跟上来。把 `in_enable` 接进异步清零，正好实现「数据无效时输出立刻失效」。

#### 4.2.2 核心流程

「三沿敏感列表」`@(posedge clk or negedge rst_n or negedge in_enable)` 的含义是：以下三种边沿中**任意一个**发生，always 块就被触发：

```text
触发沿 1: posedge clk        （每个时钟上升沿）
触发沿 2: negedge rst_n      （rst_n 由 1→0，异步复位下沿）
触发沿 3: negedge in_enable  （in_enable 由 1→0，帧结束/请求结束下沿）

触发后执行：
  if (~rst_n | ~in_enable)  reg_out_ready <= 0;   // 任一无效 → 立刻清 0
  else                      reg_out_ready <= 1;   // 都有效   → 置 1
```

由此推导出 `out_ready` 相对 `in_enable` 的时序（设 `rst_n` 全程为 1）：

| 事件 | `out_ready` 反应 | 原因 |
| --- | --- | --- |
| `in_enable` 由 0→1（上升） | **下一个 `posedge clk` 后**才变 1 | 上升沿不在敏感列表里，需等下一个时钟沿走 `else` 分支 |
| `in_enable` 由 1→0（下降） | **立刻**变 0 | `negedge in_enable` 在敏感列表里，异步触发 `if` 分支清 0 |

这正是上一讲总结的那句话：**「上升晚一拍，下降立刻清零」**。这个不对称是刻意设计的——上升晚一拍，是因为输出数据本身就要寄存一拍才稳定（见 4.3）；下降立刻清零，是为了在数据流戛然而止时，不让下游误读到一个过期的无效值。

#### 4.2.3 源码精读

`out_ready` 的产生与门控——[ColorReversal.v:L118-L124](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L118-L124)：三沿敏感列表 + 联合复位条件 `if(~rst_n | ~in_enable)`，把复位和使能合并成同一个清零条件。

中间寄存器声明——[ColorReversal.v:L112-L113](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L112-L113)：`reg_out_ready`（1 位）与 `reg_out_data`（数据位宽）都是内部寄存器，对外端口 `out_ready`/`out_data` 用 `assign` 从它们驱动，这样端口方向保持 `output` 而非 `output reg`，便于 IP-XACT 打包。

#### 4.2.4 代码实践

**实践目标**：通过跟踪 testbench，在脑子里画出 `out_ready` 与 `in_enable` 的相对时序。

**操作步骤**：

1. 打开 [ColorReversal_TB.sv:L132-L156](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L132-L156)（流水线模式的驱动任务 `work_pipeline`）。
2. 注意第 135 行 `RGBPipeline.in_enable = 1;` 之后，第 144 行才检查 `out_ready` 是否为高——中间隔了一个 `@(posedge clk)`（第 133 行）。这正好对应「上升晚一拍」。
3. 再看 [ColorReversal_TB.sv:L158-L185](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L158-L185)（请求响应模式 `work_regack`）：第 170 行 `while(!out_ready) @(posedge clk);` 是在「等 `out_ready` 升起来」；第 182–184 行读完后立刻把 `in_enable=0`，下一个时刻 `out_ready` 就被异步清掉。

**需要观察的现象**：流水线任务里，`in_enable` 拉高后要等一个时钟才有 `out_ready`；请求响应任务里，`in_enable` 一拉低，`out_ready` 几乎同时消失。

**预期结果**：如果你能在 ModelSim 里跑出波形，会看到 `out_ready` 是 `in_enable`「右移一拍、但下降沿对齐」的形态。若无 ModelSim 环境，此为「待本地验证」，仅靠读 RTL 与 testbench 也能推得该结论。

#### 4.2.5 小练习与答案

**练习 1**：如果把敏感列表里的 `or negedge in_enable` 去掉，只保留 `posedge clk or negedge rst_n`，`out_ready` 的行为会怎么变？

> **答案**：`out_ready` 将不再能「立刻清零」。因为 `in_enable` 的下降沿不再触发 always 块，必须等到下一个 `posedge clk` 才会执行 `if(~in_enable) reg_out_ready<=0`。虽然逻辑值最终也会变 0，但会多出最多一个时钟的延迟，且失去了「数据无效即输出无效」的即时性。这正是 F-I-L 把 `in_enable` 接进异步敏感列表的原因。

**练习 2**：为什么 `out_ready` 用 `assign out_ready = reg_out_ready;` 而不直接把端口声明成 `output reg out_ready`？

> **答案**：两种写法功能等价，但 F-I-L 统一把端口保持为纯 `output`、内部用 `reg` 中转再 `assign` 出去。这样做的好处是端口声明干净、与 IP-XACT（`component.xml`）的端口描述一致，便于把模块打包成可参数化 IP，也便于在打包工具里统一处理端口方向。

---

### 4.3 取反逻辑与两种模式的寄存写法

#### 4.3.1 概念说明

点运算的核心算法在 `ColorReversal` 里只有一行：`~in_data`——按位取反。

为什么按位取反等价于颜色取反？对一个 \(B\) 位无符号像素值 \(p\)，按位取反的结果是：

\[
\text{out} \;=\; \sim p \;=\; 2^{B} - 1 - p
\]

当 \(B=8\) 时，\(\sim p = 255 - p\)，这正是 [sim.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py) 里软件黄金模型 `im.point(lambda p : 255 - p)`（见第 72 行）所做的事。所以 RTL 与软件在此完全一致，这就是「软硬一致性」。

> 一个关键性质：**按位取反是「通道无关」的**。`in_data` 是把 R、G、B 三通道拼接成的一根 24 位总线，对整根总线做 `~`，等价于对每个通道各自做 `~`，因为取反不涉及相邻位之间的进位或移位。这一点很重要——下一节的实践里你会看到，并非所有运算都享有这个便利（比如左移就不行）。

#### 4.3.2 核心流程

同一个 `~in_data` 表达式，在两种模式下被「锁存」的时机不同：

```text
流水线模式 (work_mode==0):
  always @(posedge clk)  reg_out_data <= ~in_data;
  ── 每个时钟上升沿都锁存一次 ── 吞吐 = 1 像素/时钟，延迟 = 1 拍
  ── 适合连续不断的像素流（如扫描线）

请求响应模式 (work_mode==1):
  always @(posedge in_enable)  reg_out_data <= ~in_data;
  ── 只有 in_enable 的上升沿才锁存一次 ── 来一笔算一笔
  ── 适合上游节奏不定、需要逐笔握手的场景

最后统一门控：
  assign out_data = out_ready == 0 ? 0 : reg_out_data;
  ── out_ready=0 时强制输出 0，给下游一个确定的「无效占位」
```

两种模式**功能等价**（同一像素都得到 `~p`），只是时序形态不同。注释 [ColorReversal.v:L10-L11](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L10-L11) 也写明了：「Give the first output after 1 cycle while the input enable.」——首拍输出延迟 1 个时钟。

#### 4.3.3 源码精读

两支模式分支与最终门控——[ColorReversal.v:L126-L133](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L126-L133)：

```verilog
if(work_mode == 0) begin
    always @(posedge clk)
        reg_out_data <= ~in_data;          // 流水线：时钟沿锁存
end else begin
    always @(posedge in_enable)
        reg_out_data <= ~in_data;          // 请求响应：使能沿锁存
end
assign out_data = out_ready == 0 ? 0 : reg_out_data;
```

- 第 128 / 131 行：两支都算 `~in_data`，差别只在敏感沿。
- 第 133 行：`out_ready==0 ? 0 : reg_out_data` 是输出门控。它和 4.2 的 `out_ready` 配合，保证「无效时不输出乱七八糟的残留值，而是干净的 0」。

软件侧的等价运算——[sim.py:L68-L73](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py#L68-L73)：`im.point(lambda p : 255 - p)` 即 `255 - p`，与 8 位下的 `~p` 在数学上完全相同，这是软硬比对能通过的根基。

> 旁注：源码注释头里 `:Design` 字段写的是 `ContrastTransform`（[ColorReversal.v:L7](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L7)），与模块名 `ColorReversal` 不一致，疑似从别的 IP 复制模板时的笔误。读开源项目源码时遇到这种小瑕疵不必困惑，认准 `module ColorReversal` 即可。

#### 4.3.4 代码实践

**实践目标**：手工验证「RTL 取反 = 软件取反」，建立对软硬一致性的直觉。

**操作步骤**：

1. 取一个 8 位像素值，例如 R=100。
2. 用软件公式算：`255 - 100 = 155`。
3. 用 RTL 公式算：`~100`。100 的二进制是 `01100100`，按位取反得 `10011011` = 155。
4. 两者相等。再自选 3 个值（如 0、255、128）重复验证。

**需要观察的现象**：对任意 8 位值，`~p` 与 `255 - p` 永远相等。

**预期结果**：`p=0 → 255`；`p=255 → 0`；`p=128 → 127`。全部吻合。这从数学上也成立，因为 \(\sim p = 2^8 - 1 - p = 255 - p\)。

> 若 `color_width` 不是 8（比如 1 位二值图），公式同样成立：\(\sim p = 2^B - 1 - p\)，只是上界随位宽变。testbench 里就专门例化了 `#(0,1,1)` 的二值实例（[ColorReversal_TB.sv:L102-L103](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L102-L103)）来覆盖这种情况。

#### 4.3.5 小练习与答案

**练习 1**：流水线模式下，`reg_out_data` 每个 `posedge clk` 都更新一次。如果 `in_enable` 此时为 0（帧间隙），输出会不会是错的旧数据？

> **答案**：不会暴露给下游。虽然 `reg_out_data` 在帧间隙仍会被更新成 `~in_data`（此时 `in_data` 可能是无意义值），但第 133 行的门控 `out_ready==0 ? 0 : reg_out_data` 会把输出强制成 0，因为 4.2 里 `in_enable=0` 已让 `out_ready=0`。所以「内部寄存器在乱算，但对外输出被门控屏蔽」——这就是门控存在的意义。

**练习 2**：为什么说按位取反「通道无关」，而左移 `<<` 「通道相关」？

> **答案**：取反每一位独立，R 通道的位不会影响 G 通道的位。但左移会把每一位向高位方向挪，若直接对整根 24 位总线 `in_data << 1`，R 通道的最低位会和 G 通道的最高位发生串扰（数据跨通道边界）。所以「左移」必须按通道分别做，而取反可以整根总线一次做。这正是本讲综合实践里要小心的陷阱。

---

## 5. 综合实践

**任务**：仿照 `ColorReversal`，写一个点运算模块 `ColorShift`——把每个通道的像素值**左移一位并截断**（即每通道 `<< 1`，溢出高位丢弃），保持统一的六个端口、三个参数，并用 `generate if/else` 支持两种工作模式。

**为什么做这个任务**：它能把你刚学的三块知识一次串起来——`generate` 双模式骨架（4.1）、`out_ready` 与联合复位整段照搬（4.2）、而取反换成左移后，必须直面「通道相关运算」带来的新问题（4.3），从而真正理解 `genvar i` 的用途。

**操作步骤**：

1. **复制骨架**：把 [ColorReversal.v:L54-L137](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L54-L137) 整段复制为 `ColorShift.v`，把 `module ColorReversal` 改成 `module ColorShift`，端口、参数、`out_ready` 那段 always（4.2）**原样保留不动**。

2. **替换运算核心**：把 `~in_data` 改成「每通道左移一位」。注意 4.3 练习 2 的陷阱——**不要**写成 `reg_out_data <= in_data << 1`（这会让数据跨通道串扰）。正确做法是用 `genvar i` 按通道循环（这也正好解释了原模板为何预留 `genvar i`）。下面是**示例代码**（非项目原有代码）：

   ```verilog
   // 示例代码：ColorShift 的核心运算，替换原 ColorReversal 的 if/else 分支
   if(work_mode == 0) begin : gen_pipeline
       for (i = 0; i < color_channels; i = i + 1) begin : ch_pipe
           always @(posedge clk)
               reg_out_data[i*color_width +: color_width] <= in_data[i*color_width +: color_width] << 1;
       end
   end else begin : gen_reqack
       for (i = 0; i < color_channels; i = i + 1) begin : ch_req
           always @(posedge in_enable)
               reg_out_data[i*color_width +: color_width] <= in_data[i*color_width +: color_width] << 1;
       end
   end
   assign out_data = out_ready == 0 ? 0 : reg_out_data;
   ```

   其中 `[i*color_width +: color_width]` 是 Verilog 的「位片段」语法，表示「从 `i*color_width` 位起、向上取 `color_width` 位」，正好选中第 `i` 个通道。

3. **手算预期输出**：对 8 位通道，左移一位等价于乘 2 后丢弃溢出位。例如 R=100 → 200；R=200 → 400 截断为 400-256=144。验证公式：\(\text{out} = (2p) \bmod 2^B\)。

4. **软件侧对齐**：在你的 `SoftwareSim/sim.py` 里把 `lambda p : 255 - p` 改成 `lambda p : (p << 1) & 255`（即 `(2*p) % 256`），这样软件黄金模型才与 RTL 一致。

**需要观察的现象**：两种模式下输出都应是「每通道左移一位并截断」的结果；`out_ready` 时序与 `ColorReversal` 完全一致（因为那一段没改）。

**预期结果**（以 8 位 RGB 为例）：

| 输入像素 (R,G,B) | ColorShift 输出 (R,G,B) |
| --- | --- |
| (100, 50, 0) | (200, 100, 0) |
| (200, 128, 255) | (144, 0, 254) |

若你已完成 [u1-l3 工具链与仿真运行方式](u1-l3-toolchain-and-simulation.md)的学习并在本地搭好 Python 2.7 + PIL + ModelSim 环境，可参照 `ColorReversal` 的五步仿真流程跑一遍，并用 `compare.py` 看 PSNR 是否为满分（`10^6`）。**若环境未就绪，此项标注为「待本地验证」**，但手算上表应当成立。

## 6. 本讲小结

- `ColorReversal` 是全库最简点运算 IP，核心运算只有一行 `~in_data`，但完整示范了 F-I-L 的统一接口与双模式骨架。
- `generate if/else` 是**编译期**二选一：`work_mode=0` 留流水线支（`posedge clk` 锁存），`work_mode=1` 留请求响应支（`posedge in_enable` 锁存），二者功能等价、仅时序不同。
- `out_ready` 由一个「三沿敏感列表 + 联合复位」的 always 块产生：`in_enable` 上升沿不在敏感列表→`out_ready` 上升晚一拍；`in_enable` 下降沿在敏感列表→`out_ready` 立刻异步清零。
- 把 `in_enable` 接进异步复位，实现了「数据无效时输出立刻失效」；输出门控 `out_ready==0 ? 0 : reg_out_data` 则保证无效时输出干净的 0。
- 对 \(B\) 位像素，`~p = 2^B - 1 - p`，8 位下即 `255 - p`，与软件黄金模型 `im.point(lambda p : 255 - p)` 完全一致——这就是「软硬一致性」。
- 按位取反是「通道无关」的，可对整根总线一次完成；而左移等「通道相关」运算必须借助 `genvar i` 按通道分别做（见综合实践）。

## 7. 下一步学习建议

本讲你已能读懂并仿写一个最简点运算 IP。建议下一步：

1. **横向对比**：阅读 [Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v)，进入 [u2-l2 Threshold——灰度转二值](u2-l2-threshold.md)，看一个「带模式选择（`th_mode`）与参数（`th1/th2`）」的点运算如何在同一套骨架上扩展——它的双模式骨架与本讲几乎一致，只是把 `~in_data` 换成了 `case` 驱动的阈值比较。
2. **纵向深入接口**：如果你想更透彻地理解 `out_ready` 在上下游级联时的握手传播，可回到 [u1-l4](u1-l4-standard-interface-and-modes.md) 复习「握手对」一节，并留意后续 `Generator`（节拍源）讲义里 `in_enable` 是如何被「源头」产生出来的。
3. **动手跑通**：若本地的综合实践标注了「待本地验证」，建议先按 [u1-l3 工具链与仿真运行方式](u1-l3-toolchain-and-simulation.md)把 `ColorReversal` 的五步仿真跑通、看到 `compare.py` 给出满分，再回过头验证你写的 `ColorShift`。
