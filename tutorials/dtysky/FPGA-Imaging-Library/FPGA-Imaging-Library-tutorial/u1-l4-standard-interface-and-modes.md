# 标准化IP接口与两种工作模式

## 1. 本讲目标

FPGA-Imaging-Library（下称 F-I-L）里有几十个图像处理 IP，如果每个 IP 各搞一套端口和命名，它们就无法像积木一样串成流水线。本讲要解决的核心问题是：**这一整套 IP 到底遵守了哪些统一的接口约定？**

学完本讲，你应当能够：

1. 说出 F-I-L 所有 IP 共用的三个参数（`work_mode` / `color_channels` / `color_width`）和六个核心端口（`clk` / `rst_n` / `in_enable` / `in_data` / `out_ready` / `out_data`）各自代表什么。
2. 区分 **Pipeline（流水线，`work_mode=0`）** 与 **Req-ack（请求响应，`work_mode=1`）** 两种工作模式在触发时序上的根本差别。
3. 理解 `in_enable` 这一根信号在两种模式下的「双重身份」——流水线模式里它是复位/启动门，请求响应模式里它是逐笔请求的开关。
4. 看懂 `generate if(work_mode==0)/else` 这种「在综合阶段就二选一」的写法，明白它不是运行时的多路选择器。

本讲承接 u1-l2 建立的「单个 IP 的标准目录布局」，把目光从目录结构推进到目录里那一份 RTL 的**对外契约**。

## 2. 前置知识

在阅读本讲前，建议你已经了解以下 Verilog 基础概念（不熟悉也没关系，下面会结合源码再点一遍）：

- **模块（module）、端口（input/output）、参数（parameter）**：模块是一块硬件的封装；端口是它对外的连线；参数是在「造出这块硬件之前」就能改的配置。
- **时序逻辑与寄存器（reg + always @(posedge clk)）**：在时钟上升沿把新值存进寄存器，这是 FPGA 里「记忆」的基本单元。
- **异步复位（negedge rst_n）**：复位信号一旦拉低，寄存器立刻清零，不必等时钟。
- **generate 块**：一种「在硬件生成阶段（elaboration）做选择」的语法，可以依据参数在综合时只留下其中一条硬件分支。
- **握手（handshake）**：发送方与接收方靠一两个「有效/就绪」信号来约定「现在这份数据能不能传」。本讲的 `in_enable` / `out_ready` 就是一对极简握手信号。
- **流水线（pipeline）与请求响应（req-ack）**：流水线像工厂流水线，每个时钟都吞一个、吐一个；请求响应像取号办事，办完一笔再办下一笔。

## 3. 本讲源码地图

本讲围绕两个最简单的点运算 IP 展开，它们最能体现「统一接口」这层共性：

| 文件 | 作用 |
| --- | --- |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L54-L137) | 颜色取反，多通道（带 `color_channels`）的最简点运算，是本讲的「样板 IP」。 |
| [Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L55-L170) | 灰度转二值，单通道（**没有** `color_channels`）的点运算，用来和 ColorReversal 对比接口差异。 |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L132-L185) | ColorReversal 的 testbench，里面分别演示了两种模式该**怎么驱动**，是理解时序的最佳依据。 |
| [Point/ColorReversal/HDL/ColorReversal.srcs/component.xml](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L160-L184) | IP-XACT 描述文件，把参数和端口登记成可参数化的 Vivado IP，能看到 `work_mode` 的枚举选项。 |

> 提示：本讲引用的行号都基于当前 HEAD `c8cd350`。源码文件顶部有大段 LGPL 许可声明，本讲引用时会跳过这些，只关注接口与逻辑部分。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**统一参数**、**统一端口与 `in_enable/out_ready` 握手**、**`generate` 双分支（两种模式的时序差异）**。

### 4.1 统一参数：work_mode / color_channels / color_width

#### 4.1.1 概念说明

F-I-L 的每个 IP 都通过 `parameter` 暴露一组**可配置项**，让同一个 IP 适配不同的使用场景，而不必改源码。最核心的有三个：

- **`work_mode`**：工作模式选择。`0` 表示流水线（Pipeline），`1` 表示请求响应（Req-ack）。它决定数据通路用哪种时序来寄存结果（详见 4.3）。
- **`color_channels`**：颜色通道数。`1` 是灰度，`3` 是 RGB，依此类推。**只有处理多通道数据的 IP 才有这个参数**。
- **`color_width`**：每个颜色通道的位宽，取值范围 `1~12`，最常见是 `8`（即每通道 8 比特）。

这三个参数共同决定了一根数据线的总位宽。设通道数为 \(C\)、每通道位宽为 \(B\)，则一根 `in_data` / `out_data` 的总位宽为：

\[
W = C \times B
\]

例如 RGB 8 位时 \(W = 3 \times 8 = 24\)，即 `in_data` 是 24 比特，高 8 位是 R、中 8 位是 G、低 8 位是 B。

#### 4.1.2 核心流程

参数的传递与生效流程：

1. 用户在例化 IP 时通过 `#(work_mode, color_channels, color_width)` 给出配置（或在 Vivado 的 IP 定制 GUI 里点选）。
2. 综合工具在 **elaboration 阶段**（生成硬件之前）把参数值代入，据此推导出端口位宽和选择哪条硬件分支。
3. 最终烧进 FPGA 的是「按这组参数定制好」的硬件，运行时不再改变。

需要注意：`work_mode` 是**编译期**选择，不是运行期切换。一个 IP 例化成流水线版，它就永远是流水线版。

#### 4.1.3 源码精读

先看 ColorReversal 的三个参数声明：

```verilog
parameter[0 : 0] work_mode = 0;       // 0 流水线, 1 请求响应
parameter color_channels = 3;          // 1 灰度, 3 RGB
parameter[3: 0] color_width = 8;       // 1~12
```

这三行位于 [ColorReversal.v:L62-L80](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L62-L80)（注释里写明了 `::range` 与 `::description`，这些是 Com2DocHDL 文档生成器用的标记）。注意 `work_mode` 被显式声明为 `[0:0]`，即 1 比特，因为它只能取 0 或 1。

数据位宽正是按 \(W = C \times B\) 推导的，见端口声明：

```verilog
input [color_channels * color_width - 1 : 0] in_data;
output[color_channels * color_width - 1 : 0] out_data;
```

这两行在 [ColorReversal.v:L100](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L100) 与 [ColorReversal.v:L110](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L110)。RGB 8 位时它们就是 `[23:0]`。

再对比 Threshold：它是**单通道**灰度 IP，所以**没有** `color_channels` 参数，数据位宽直接等于 `color_width`：

```verilog
parameter work_mode = 0;
parameter color_width = 8;            // 范围 1~12
...
input [color_width - 1 : 0] in_data;  // 单通道，位宽就是 color_width
```

见 [Threshold.v:L67-L80](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L67-L80) 与 [Threshold.v:L118](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L118)。这就是「统一接口」的弹性：多通道 IP 带 `color_channels`，单通道 IP 不带，但两者都共享 `work_mode` 与 `color_width`。

最后看 `work_mode` 是怎么登记成 Vivado 可点选选项的。在 `component.xml` 里，它被定义成一个带枚举的参数，`0` 显示为 "Pipeline"、`1` 显示为 "ReqAck"：

```xml
<spirit:choice>
  <spirit:name>choices_0</spirit:name>
  <spirit:enumeration spirit:text="Pipeline">0</spirit:enumeration>
  <spirit:enumeration spirit:text="ReqAck">1</spirit:enumeration>
</spirit:choice>
```

见 [component.xml:L178-L184](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L178-L184)；同文件 [L160-L176](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L160-L176) 把三个参数登记为 `modelParameter`。而端口位宽随参数自动联动，靠的是 [component.xml:L117](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L117) 那一行 `dependency` 表达式（`color_channels * color_width - 1`），这正是 \(W = C \times B\) 在 IP-XACT 里的官方写法。

#### 4.1.4 代码实践

**实践目标**：亲手验证「参数如何决定端口位宽」。

**操作步骤**：

1. 打开 `Point/ColorReversal/HDL/ColorReversal.srcs/component.xml`，找到 `in_data` 端口的 `dependency` 表达式（L117 附近）。
2. 打开 testbench [ColorReversal_TB.sv:L90-L107](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L90-L107)，看它如何用 `#(0, 3, 8)`、`#(0, 1, 8)`、`#(0, 1, 1)` 例化出 RGB / 灰度 / 二值三个流水线版本，以及对应的 `#(1, …)` 请求响应版本。
3. 心算每组参数下的 `in_data` 位宽：`#(0,3,8)`→24 位、`#(0,1,8)`→8 位、`#(0,1,1)`→1 位。

**需要观察的现象**：testbench 里 `TBInterface` 的位宽声明 `bit[channels * color_width - 1 : 0] in_data;`（[L68](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L68)）会随例化参数自动变成 24/8/1 位。

**预期结果**：同一份 ColorReversal 源码，仅靠改参数就能驱动 RGB、灰度、二值三种图像，端口位宽随之改变。

**待本地验证**：若你在 ModelSim 里编译运行，可观察三种例化的波形中 `in_data`/`out_data` 的位宽确实不同。

#### 4.1.5 小练习与答案

**练习 1**：要把 ColorReversal 配成「灰度 10 位」的流水线版本，例化参数应怎么写？`in_data` 是几位？

**答案**：`ColorReversal #(0, 1, 10) ...`（work_mode=0, channels=1, width=10）。`in_data` 位宽为 \(1 \times 10 = 10\) 位。

**练习 2**：Threshold 为什么没有 `color_channels` 参数？它的 `in_data` 位宽由谁决定？

**答案**：因为 Threshold 只处理单通道灰度图，固定 1 个通道，没有可配置的通道数；`in_data` 位宽直接等于 `color_width`（见 [Threshold.v:L118](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L118)）。

---

### 4.2 统一端口与 in_enable / out_ready 握手

#### 4.2.1 概念说明

除了可变的数据端口，所有 IP 都遵守同一组**固定语义**的六个端口：

| 端口 | 方向 | 语义 |
| --- | --- | --- |
| `clk` | input | 时钟，所有寄存器在它的上升沿更新。 |
| `rst_n` | input | 异步复位，低有效；拉低时电路清零。 |
| `in_enable` | input | 输入有效门（双重身份，见下）。 |
| `in_data` | input | 输入像素，必须与 `in_enable` 同步。 |
| `out_ready` | output | 输出就绪，高电平时 `out_data` 才可读。 |
| `out_data` | output | 输出像素，与 `out_ready` 同步。 |

`in_enable` 与 `out_ready` 构成一对极简握手：

- `in_enable`：上游告诉本 IP「我现在给的 `in_data` 是有效的」。
- `out_ready`：本 IP 告诉下游「我现在的 `out_data` 可以读了」。

源码注释把 `in_enable` 的双重身份说得很直白（[ColorReversal.v:L93-L94](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L93-L94)）：在流水线模式下它「相当于另一个 `rst_n`」；在请求响应模式下「只有它为高，`in_data` 才能被接受」。

#### 4.2.2 核心流程

握手的关键在于 `out_ready` 的产生逻辑，它在两种模式下是**完全相同**的（不随 `work_mode` 变）：

```
寄存器 reg_out_ready 的更新规则：
  触发：posedge clk  或  negedge rst_n  或  negedge in_enable
  若 rst_n==0 或 in_enable==0 ：reg_out_ready <= 0
  否则                         ：reg_out_ready <= 1
```

由此可推出两条**确定性的时序关系**（这是本讲最重要的结论之一）：

1. **上升**：`in_enable` 拉高后，要等下一个 `clk` 上升沿，`out_ready` 才变 1——即 `out_ready` 的上升沿比 `in_enable` 晚一个时钟。
2. **下降**：`in_enable` 一旦拉低，由于 `negedge in_enable` 在敏感列表里，`out_ready` **立刻（异步）** 清 0，不必等时钟。

这一「上升慢一拍、下降立刻生效」的不对称，是两种模式表现不同的根源：

- **流水线模式**：`in_enable` 启动后一直保持高，所以「异步下降」只在启动/复位那一刻起作用，相当于一次「帧开始的复位」；之后 `out_ready` 稳定为 1，数据持续流动。
- **请求响应模式**：`in_enable` 每笔交易都翻转一次，所以「异步下降」每笔都触发，把 `out_ready` 在交易之间清零，起到「逐笔门控」的作用。

此外，输出数据有一层保护：当 `out_ready` 为 0 时，`out_data` 被强制输出 0：

\[
\texttt{out\_data} = \begin{cases} \texttt{reg\_out\_data} & \texttt{out\_ready}=1 \\ 0 & \texttt{out\_ready}=0 \end{cases}
\]

这样下游只需盯住 `out_ready`：它为 0 时拿到的必是 0（无效占位），为 1 时拿到的才是真实结果。

#### 4.2.3 源码精读

`out_ready` 的产生逻辑是整个接口里最值得逐字读的一段，见 [ColorReversal.v:L118-L124](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L118-L124)：

```verilog
always @(posedge clk or negedge rst_n or negedge in_enable) begin
    if(~rst_n | ~in_enable)
        reg_out_ready <= 0;
    else
        reg_out_ready <= 1;
end
assign out_ready = reg_out_ready;
```

注意敏感列表里有**三个**边沿：`posedge clk`（正常更新）、`negedge rst_n`（复位）、`negedge in_enable`（输入撤销）。后者正是「异步下降」的来源。Threshold 里的对应段落 [Threshold.v:L135-L141](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L135-L141) 写法几乎逐字相同，印证了这是全库统一的握手模板。

`out_data` 的门控在 [ColorReversal.v:L133](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L133)：

```verilog
assign out_data = out_ready == 0 ? 0 : reg_out_data;
```

testbench 验证了这套握手：流水线驱动里，每拍都查 `if(out_ready)` 才把 `out_data` 写进文件，见 [ColorReversal_TB.sv:L144](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L144)；请求响应驱动里，用 `while(~out_ready)` 死等 `out_ready` 变高才读，见 [ColorReversal_TB.sv:L170-L171](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L170-L171)。两处都说明：**`out_data` 只有在 `out_ready=1` 时才有意义**。

#### 4.2.4 代码实践

**实践目标**：在不跑仿真的前提下，根据上面的规则预测 `out_ready` 的行为。

**操作步骤**：

1. 假设 `rst_n` 已稳定为 1。设 `in_enable` 在第 0 拍为 0，第 1 拍拉高并保持。
2. 用 4.2.2 的两条规则，逐拍推导 `out_ready`：第 0 拍=? 第 1 拍（`in_enable` 刚拉高，还没到下一个 clk）=? 第 2 拍=?
3. 再设想请求响应场景：第 1 拍拉高、第 3 拍拉低，问 `out_ready` 在第 3 拍拉低的「瞬间」会发生什么？

**需要观察的现象**：`out_ready` 上升比 `in_enable` 晚一个 clk；下降与 `in_enable` 同步（异步）。

**预期结果**（按规则推导）：

- 流水线场景：第 0、1 拍 `out_ready=0`，从第 2 拍起 `out_ready=1`。
- 请求响应场景：`in_enable` 在第 3 拍拉低时，`out_ready` 在同一个下降沿立即变 0。

**待本地验证**：精确到「具体哪一拍」的波形以你在 ModelSim 中实际观测为准（testbench 用阻塞赋值驱动，采样边沿的精确时刻取决于仿真器调度）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `out_ready` 的下降是「异步立刻」的，而上升却要「等一个 clk」？

**答案**：因为敏感列表里同时有 `negedge in_enable`（异步）和 `posedge clk`（同步）。`in_enable` 拉低触发异步分支立即清 0；而 `in_enable` 拉高只能等下一次 `posedge clk` 才把 1 写进寄存器，所以上升晚一拍。

**练习 2**：`out_data = out_ready == 0 ? 0 : reg_out_data;` 这行如果删掉，直接 `assign out_data = reg_out_data;`，下游会出什么问题？

**答案**：在 `out_ready=0` 的「无效期」，`reg_out_data` 里残留的是上一次的旧值（流水线模式）或未定义值，下游若不严格看 `out_ready` 就会把脏数据当成结果。门控成 0 提供了一个确定的「无效占位」值，降低下游误读风险。

---

### 4.3 generate 双分支：Pipeline 与 Req-ack 的时序差异

#### 4.3.1 概念说明

`work_mode` 真正发挥作用的地方，是数据结果寄存器 `reg_out_data` 的时钟选择。F-I-L 用 `generate` 块里的 `if/else` 在**综合阶段**二选一：

```verilog
generate
    if(work_mode == 0) begin
        always @(posedge clk)          // 流水线：每个时钟锁存
            reg_out_data <= ~in_data;
    end else begin
        always @(posedge in_enable)    // 请求响应：输入有效沿才锁存
            reg_out_data <= ~in_data;
    end
endgenerate
```

注意：这是 **generate-conditional**（条件生成），不是运行时的 `if`。综合后**只有一个** `always` 块真实存在——`work_mode=0` 的例化里只有 `posedge clk` 那个，`work_mode=1` 的例化里只有 `posedge in_enable` 那个。所以「切换模式」=「重新综合一个不同版本的 IP」，而不是「运行时拨开关」。

两种模式的本质差别：

- **Pipeline（`work_mode=0`）**：结果在**每个时钟上升沿**都更新。`in_enable` 拉高后持续喂入数据，IP 像流水线一样每拍吞吐一个像素。延迟 1 个时钟（源码头注释「Give the first output after 1 cycle while the input enable」，见 [ColorReversal.v:L11](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L11)）。吞吐量最高，适合连续的视频流。
- **Req-ack（`work_mode=1`）**：结果只在 **`in_enable` 的上升沿**锁存一次。上游每发起一次请求（拉高 `in_enable`），IP 才处理一笔，处理完下游拉低 `in_enable` 结束这笔交易。吞吐量低（每笔至少约 2 个时钟），但适合「上游/下游节奏不确定、需要逐笔握手」的场景。

#### 4.3.2 核心流程

两种模式的工作流程对比（设 `rst_n` 已稳定为 1）：

**Pipeline 模式（连续流）：**

```
每拍：in_enable 保持 1；每拍喂一个新 in_data；
     out_ready 自第 2 拍起恒为 1；
     out_data 每拍给出「上一拍输入的取反」。
吞吐：1 像素/时钟。
```

**Req-ack 模式（逐笔交易）：**

```
每笔交易：
  1. 上游拉高 in_enable，同时给出 in_data；
  2. 在 in_enable 上升沿，reg_out_data 锁存 ~in_data；
  3. 下一个 clk 上升沿，out_ready 变 1；
  4. 下游发现 out_ready=1，读走 out_data；
  5. 下游拉低 in_enable → out_ready 异步清 0，交易结束。
吞吐：≤ 1 像素 / (2 时钟)。
```

相对时序示意（`↑` 表示上升沿，`T` 为一个时钟周期）：

```
Pipeline:
  in_enable: ___|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾   (拉高后保持)
  out_ready: _______|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾   (滞后 in_enable 一个 T)
  in_data  :    A0   A1   A2   A3  …   (每拍一个)
  out_data :         ~A0  ~A1  ~A2  …  (滞后输入一个 T)

Req-ack:
  in_enable: ‾|___|‾|___|‾|___|‾|___   (每笔请求一个脉冲)
  out_ready:    ‾|___|‾|___|‾|___|‾|_   (请求后一个 T 出现，请求撤销即消失)
```

> 说明：上图表达的是「相对关系」（上升晚一拍、下降随请求），精确到具体时钟拍的电平请以仿真波形为准。

#### 4.3.3 源码精读

ColorReversal 的 generate 双分支在 [ColorReversal.v:L116-L135](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L116-L135)，核心是这两段：

```verilog
if(work_mode == 0) begin
    always @(posedge clk)
        reg_out_data <= ~in_data;       // L127-L128 流水线分支
end else begin
    always @(posedge in_enable)
        reg_out_data <= ~in_data;       // L130-L131 请求响应分支
end
```

Threshold 的对应结构在 [Threshold.v:L143-L163](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sources_1/new/Threshold.v#L143-L163)，差别只是把 `~in_data` 换成了带 `case(th_mode)` 的阈值比较——**模式切换的骨架完全一致**，这正是「统一接口」的力量：换算法不换时序框架。

testbench 用两个 task 把两种模式的「驱动方式」写得泾渭分明。流水线驱动 [ColorReversal_TB.sv:L132-L156](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L132-L156)：

```verilog
task work_pipeline();
    @(posedge clk);
    RGBPipeline.in_enable = 1;                       // 持续保持高
    fst = $fscanf(fi, "%b", RGBPipeline.in_data);    // 每拍喂新数据
    if(RGBPipeline.out_ready)                        // ready 即采
        $fwrite(fo, ..., RGBPipeline.out_data);
endtask
```

请求响应驱动 [ColorReversal_TB.sv:L158-L185](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L158-L185)：

```verilog
task work_regack();
    @(posedge clk);
    RGBReqAck.in_enable = 1;                         // 发起一次请求
    fst = $fscanf(fi, "%b", RGBReqAck.in_data);
    while (~(RGBReqAck.out_ready))                   // 死等 ready
        @(posedge clk);
    $fwrite(fo, ..., RGBReqAck.out_data);            // 读结果
    RGBReqAck.in_enable = 0;                         // 结束这笔交易
endtask
```

对比两者：`work_pipeline` 里 `in_enable` 设了 1 之后就不再管（保持高，连续流）；`work_regack` 里每笔交易末尾都要把 `in_enable` 拉回 0。Threshold 的同名 task 在 [Threshold_TB.sv:L119-L142](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/Threshold/HDL/Threshold.srcs/sim_1/new/Threshold_TB.sv#L119-L142)，逻辑一致。

最后注意例化：testbench 用 `#(0,3,8)` 造流水线版、`#(1,3,8)` 造请求响应版（[ColorReversal_TB.sv:L90-L95](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L90-L95)），两组结果分别写到 `-pipeline.res` 与 `-reqack.res`（[L199 与 L208](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L199-L208)），从而在**同一次仿真**里同时验证两种模式。

#### 4.3.4 代码实践

**实践目标**：把 `work_mode=0` 与 `work_mode=1` 的 `out_ready` / `out_data` 相对 `in_enable` 的时序波形画出来并对比。

**操作步骤**：

1. 打开 `Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv`，确认它已经同时例化了流水线版（`CRRGBPipeline`）与请求响应版（`CRRGBReqAck`），所以你**不需要改源码**就能对比两种模式。
2. 按 u1-l3 的工具链步骤，在 `FunSimForHDL` 目录里用 ModelSim 跑 `Run.do`（或 `RunOver.do` 跳过波形只看结果）。
3. 在波形窗口里把 `RGBPipeline` 和 `RGBReqAck` 两组接口的 `clk` / `in_enable` / `in_data` / `out_ready` / `out_data` 都拉出来。
4. 手绘（或截图标注）两张时序图，重点标出：`out_ready` 相对 `in_enable` 上升沿的延迟、请求响应模式下 `in_enable` 拉低时 `out_ready` 的即时回落。

**需要观察的现象**：

- 流水线版：`in_enable` 拉高后保持不变；`out_ready` 滞后一个 `clk` 变高并保持；`out_data` 每拍更新，是上一拍 `in_data` 的按位取反。
- 请求响应版：`in_enable` 呈脉冲；`out_ready` 在每个脉冲后一个 `clk` 出现、脉冲消失即回落；`out_data` 在 `in_enable` 上升沿被锁存。

**预期结果**：两种模式的 `out_ready` 上升都满足「滞后 `in_enable` 一个时钟」；差别在于 `in_enable` 是「持续高」还是「逐笔脉冲」，以及由此带来的吞吐不同。两份 `.res` 最终经 `convert.py`/`compare.py` 与软件结果比对，PSNR 都应为完全一致（记为 `10^6`），证明两种模式**功能等价、仅时序形态不同**。

**待本地验证**：上述波形需在你的 ModelSim 环境中实际运行得到；若暂无环境，可先按 4.3.2 的示意图手绘「理论波形」，待具备环境后再核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 `generate if(work_mode==0)/else` 不是运行时的多路选择器？证据是什么？

**答案**：因为它是 generate-conditional，在 elaboration 阶段依参数值只实例化其中一个 `always` 块。证据是两个分支写的是两个**独立的** `always` 块（不同敏感列表），综合后网表里只会留下与 `work_mode` 取值匹配的那一个，而非一个二选一 mux。

**练习 2**：对于连续的 1080p 视频流（每像素需 1 拍处理），应选哪种模式？为什么？

**答案**：选 Pipeline（`work_mode=0`）。它每拍吞吐一个像素，能跟上连续视频流的速率；Req-ack 每笔至少 2 拍，会拖慢吞吐，适合节奏不定的逐笔处理而非连续流。

**练习 3**：把 ColorReversal 的请求响应分支 `always @(posedge in_enable)` 改成 `always @(posedge clk)`，会破坏请求响应模式吗？为什么？

**答案**：会。请求响应模式的精髓是「结果在请求沿（`in_enable` 上升）锁存」，配合「`in_enable` 拉低即结束交易」。若改成 `posedge clk`，则每拍都会用当前 `in_data` 覆盖结果，`in_enable` 的脉冲语义就失效了——交易没结束结果也可能被改写，握手被破坏。

## 5. 综合实践

把本讲三个模块串起来，完成一次「接口契约速读」：

1. **挑两个 IP**：ColorReversal（多通道）与 Threshold（单通道）。
2. **填一张接口对照表**，包含：是否有 `color_channels`、`color_width` 默认值与范围、`in_data`/`out_data` 位宽表达式、`work_mode` 取值含义。
3. **定位握手三件套**：在两个 IP 里分别找到 `reg_out_ready` 的 `always` 块、`out_ready` 的 `assign`、`out_data` 的门控 `assign`，确认它们逐字相同（这是「全库统一握手模板」的证据）。
4. **解释 `in_enable` 双重身份**：用一句话分别说明它在 Pipeline 与 Req-ack 下的作用，并指出是哪一行代码（敏感列表里的 `negedge in_enable`）同时支撑了这两种作用。
5. **预测而非盲跑**：在不跑仿真的前提下，写出 Pipeline 模式下「`in_enable` 第 1 拍拉高」后，第 1、2、3 拍 `out_ready` 与 `out_data` 的取值；再写出 Req-ack 模式下「`in_enable` 第 1 拍拉高、第 3 拍拉低」的对应取值。

完成后再去 ModelSim 里跑 `Run.do` 核对，修正你预测与实际不符之处——这一步最能加深对时序的理解。

## 6. 本讲小结

- F-I-L 所有 IP 共享三个参数 `work_mode` / `color_channels` / `color_width` 和六个端口 `clk` / `rst_n` / `in_enable` / `in_data` / `out_ready` / `out_data`；数据位宽 \(W = C \times B\)。
- `work_mode` 是**编译期**选择：`0`=Pipeline、`1`=Req-ack，由 `component.xml` 登记成可点选枚举。
- `in_enable` / `out_ready` 是全库统一的握手对；`out_ready` 上升比 `in_enable` 晚一个时钟，下降则因 `negedge in_enable` 异步立刻生效。
- `in_enable` 有双重身份：流水线模式下相当于「帧开始的第二个复位」；请求响应模式下是「逐笔请求的开关」。
- `generate if(work_mode==0)/else` 在综合阶段二选一，只留下匹配模式的数据通路 `always` 块，因此模式切换=重新综合，而非运行时切换。
- `out_data` 在 `out_ready=0` 时被门控为 0，给下游一个确定的「无效占位」值。

## 7. 下一步学习建议

- **进入 Unit 2**：本讲把「接口契约」讲透了，下一讲 [u2-l1 ColorReversal——最简单的点运算](u2-l1-color-reversal.md) 会带你逐行读懂这个样板 IP 的 `generate` 块、联合复位与取反逻辑，并让你仿写一个自己的点运算 IP。
- **横向对比更多 IP**：随手翻 `Point/` 下其他单通道点运算（如 Graying），确认它们的握手三件套与 `generate` 骨架是否与 ColorReversal 完全一致——这是验证「统一接口」最直接的方式。
- **向下游延伸**：当你想看 `in_enable`/`out_ready` 在真实流水线里如何被上下游驱动，可提前浏览 `Generator/` 分类下产生坐标与节拍的 IP，那是流水线数据的「源头」。
