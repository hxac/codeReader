# 时序约束入门：SDC 与环境约束

## 1. 本讲目标

本讲带你读懂 ASIC 设计里最重要的一类「外部说明文件」——**SDC（Synopsys Design Constraints，Synopsys 设计约束）**。

学完本讲，你应当能够：

- 说清楚 **时钟（clock）** 在静态时序分析里扮演的角色，并能用 `create_clock` 写出指定频率的时钟；
- 区分 **时钟源延迟（source latency）** 与 **时钟网络延迟（network latency）**，理解 **时钟不确定性（uncertainty）** 由 skew、jitter、margin 三部分构成；
- 理解 `set_input_delay` / `set_output_delay` 这一对约束背后的「虚拟外部寄存器」模型，并会套用时序预算公式做计算；
- 理解 `set_driving_cell` / `set_input_transition` / `set_load` 这些**环境约束**为什么会影响内部时序；
- 把约束文件里的每一条命令，对应回 `MY_DESIGN.v` 的真实端口与逻辑结构。

本讲只看约束、不算综合。也就是说，我们关心「设计者如何向工具描述外部时序环境」，而不是「工具最后算出多少 slack」。真正跑出数字需要带工艺库的 Design Compiler / PrimeTime，这部分标为「待本地验证」。

## 2. 前置知识

在进入约束之前，先用大白话建立三个直觉。

**第一，芯片的速度由谁说了算？** 一颗数字芯片里有一根（或多根）时钟线 `clk`，所有寄存器（register / 触发器）都在时钟上升沿采样。两个相邻寄存器之间，信号必须在一个时钟周期内「跑完」全部组合逻辑，并提前一点点到达（这点提前量叫 setup time）。所以「时钟周期」就是给每条数据路径分配的「预算」。

**第二，周期与频率互为倒数。** 若时钟周期为 \( T \)，则频率 \( f = 1/T \)。例如周期 \( T = 3.0\,\text{ns} \)，对应频率约 \( 333\,\text{MHz} \)（\( 1/3.0\,\text{ns} \approx 333\,\text{MHz} \)）。本讲示例库的时间单位是 1ns，电容单位是 1pF，务必记住这两个单位。

**第三，约束文件 = 给工具讲「外部世界长什么样」。** 你的 RTL 只描述了**芯片内部**的逻辑。但内部时序的好坏，取决于外部谁在喂数据、谁在收数据、时钟抖不抖。SDC 就是用来补全这些「外部信息」的语言。本讲的 `My_Design.cons` 就是一份 SDC（`.cons` 是 Synopsys 约束文件的常用后缀，本质是 Tcl 命令）。

> 名词速查：**STA**（Static Timing Analysis，静态时序分析）——不走激励、只穷举路径来检查时序是否满足的方法；**setup**（建立时间）——数据须在时钟沿之前稳定的最小提前量；**slack**（余量）——实际到达时间与要求时间之差，正值代表「达标」。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是一一对应的「设计 + 约束」对：

| 文件 | 作用 |
|------|------|
| [MY-Design/My_Design.cons](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons) | SDC 约束文件，逐条描述时钟、输入/输出延迟、环境属性 |
| [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) | 被约束的 Verilog 设计，含顶层 `MY_DESIGN` 与子模块 `ARITH`、`COMBO` |

约束文件里出现的每个端口名（`clk`、`data*`、`sel`、`out1/2/3`、`Cin*`、`Cout`）都能在 `MY_DESIGN.v` 的端口声明里找到——这是读懂约束的第一条线索：**约束永远挂载在真实存在的端口/引脚上**。

先回顾一下 `MY_DESIGN` 的端口与内部数据流（来自上一讲 u2-l1）：

```verilog
module MY_DESIGN ( Cin1, Cin2, Cout, data1, data2, sel, clk, out1, out2, out3);
  input [4:0] Cin1, Cin2, data1, data2;
  input sel, clk;
  output [4:0] Cout, out1, out2, out3;
```

- **寄存器输入路径**：`data1/data2` → 经 `ARITH` → 顶层 `always @(posedge clk)` 锁存进 `R1/R2/R3/R4`；
- **纯组合路径**：`Cin1/Cin2` → 经 `COMBO` → 直接驱动 `Cout`，中间没有寄存器。

记住这两条路径的区别，后面约束会把 `data*`（寄存器输入）与 `Cin*`（组合输入）区别对待。

## 4. 核心概念与源码讲解

### 4.1 时钟定义（create_clock）

#### 4.1.1 概念说明

`create_clock` 是一切时序分析的**根**。它告诉工具：「这个端口上有一个周期为 \( T \) 的方波时钟，请把它当作整个设计的时间基准。」没有时钟，工具就不知道「一个周期」是多久，所有 setup/hold 检查都无从谈起。

时钟三要素：
- **period（周期）**：相邻两个上升沿的时间间隔，决定设计能跑多快；
- **waveform（波形，本例省略）**：默认是占空比 50% 的方波；
- **source（源对象）**：时钟从哪个端口或引脚进入芯片，这里是 `clk`。

#### 4.1.2 核心流程

1. 工具读到 `create_clock`，在 `clk` 端口建立一个理想时钟，周期由 `-period` 指定；
2. 该时钟沿芯片内部传播，所有以 `posedge clk` 触发的寄存器都自动「挂」到这个时钟域；
3. 工具据此计算每条寄存器到寄存器路径的时序预算（一个周期减去各种损耗，见 4.2、4.3）。

周期与频率的换算：

\[
T = \frac{1}{f}, \qquad f = \frac{1}{T}
\]

#### 4.1.3 源码精读

约束文件开头先声明了**单位**，再用 `reset_design` 清空旧约束，最后才定义时钟：

单位声明——时间 1ns、电容 1pF，是本讲所有数值的度量基准：
[MY-Design/My_Design.cons:7-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L7-L9) —— 注释说明「本库时间单位为 1ns，电容单位为 1pF」。

清空旧约束，避免历史残留属性干扰：
[MY-Design/My_Design.cons:20](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L20) —— `reset_design` 移除已有约束与属性。

定义 333MHz 时钟：
[MY-Design/My_Design.cons:29-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L29-L31) —— 注释「333MHz 对应 3.0ns 周期」，命令 `create_clock -period 3.0 [get_ports clk]`。

注意两点写法：
- `[get_ports clk]` 用 **Tcl 命令替换**取出端口对象，而不是写字符串 `"clk"`——SDC 里凡是要引用对象，都要走 `get_ports` / `get_clocks` / `get_pins` 这类「取对象」命令；
- `3.0` 这个数字的单位是 ns，由开头的单位声明决定。

#### 4.1.4 代码实践

**实践目标**：建立「频率 ↔ 周期」的直觉，能随手换算。

**操作步骤**：
1. 打开 [MY-Design/My_Design.cons:29-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L29-L31)；
2. 用公式 \( T = 1/f \) 自行计算：50MHz、100MHz、200MHz、500MHz 各对应多少 ns 周期。

**需要观察的现象**：频率翻倍，周期减半；频率越高，给每条数据路径的预算越紧。

**预期结果**（单位 ns）：

| 频率 | 周期 \( T \) |
|------|------------|
| 50MHz | 20.0 |
| 100MHz | 10.0 |
| 200MHz | 5.0 |
| 333MHz | 3.0 |
| 500MHz | 2.0 |

> 说明：本步骤是纸笔换算，不依赖 EDA 工具；把周期真正喂给工具验证时序是否收敛，需在 Design Compiler / PrimeTime 中运行，「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `-period` 从 3.0 改成 1.5，设计的目标频率变成多少？时序会变松还是变紧？

> **答案**：\( f = 1/1.5\,\text{ns} \approx 667\,\text{MHz} \)。周期减半意味着每条路径的预算减半，时序明显变紧，更容易出现负 slack（违例）。

**练习 2**：为什么 `create_clock` 的对象写成 `[get_ports clk]` 而不是直接 `clk`？

> **答案**：因为 SDC 命令需要接收**对象句柄**而非字符串名字。`get_ports clk` 返回名为 `clk` 的端口对象；直接写 `clk` 只是一个字符串，工具无法识别它指向哪个端口。

---

### 4.2 时钟延迟与不确定性

#### 4.2.1 概念说明

光有一个理想时钟还不够。真实世界里，时钟从晶振走到芯片、再从 `clk` 端口分发到每个寄存器，都需要时间；而且不同寄存器收到的时钟沿不会完全对齐。这些都要在约束里建模，分三类：

- **时钟源延迟（source latency）**：时钟从「理想时钟源」走到芯片 `clk` 端口的延迟，发生在**芯片之外**（板级/锁相环输出到管脚）。
- **时钟网络延迟（network latency）**：时钟从 `clk` 端口分发到**芯片内部各寄存器**的延迟（时钟树）。综合与 CTS 之前，这只是个估计值；CTS 之后会被真实传播延迟取代。
- **时钟不确定性（uncertainty）**：给 setup/hold 检查额外预留的安全余量，通常合并了 **skew**（不同寄存器时钟到达时间差）、**jitter**（周期抖动）和 **margin**（设计裕量）。

此外还有 **时钟翻转时间（clock transition）**：时钟沿本身不是垂直的，上升/下降需要时间，这个斜率会影响寄存器的时序弧。

#### 4.2.2 核心流程

对一条「寄存器 A → 寄存器 B」的 setup 路径，工具实际可用的预算为：

\[
T_{\text{可用}} = T - t_{\text{uncertainty(setup)}}
\]

即**周期减去 setup 不确定性**。源延迟和（CTS 前的）网络延迟对同一时钟域内的寄存器到寄存器路径是「共同抵消」的，因此主要影响的是输入/输出延迟的相对换算（见 4.3），而不确定性则直接吃掉每条路径的预算。

本例不确定性的推导（文件注释给出）：

\[
\underbrace{60\,\text{ps}}_{\text{skew}(\pm30)} + \underbrace{40\,\text{ps}}_{\text{jitter}} + \underbrace{50\,\text{ps}}_{\text{setup margin}} = 150\,\text{ps} = 0.15\,\text{ns}
\]

#### 4.2.3 源码精读

**源延迟**——最大 700ps（0.7ns）：
[MY-Design/My_Design.cons:34-36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L34-L36) —— `set_clock_latency -source -max 0.7 [get_clocks clk]`。`-source` 标明这是片外源延迟。

**网络延迟**——最大 300ps（0.3ns）：
[MY-Design/My_Design.cons:39-41](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L39-L41) —— `set_clock_latency -max 0.3 [get_clocks clk]`。不带 `-source`，即片内时钟网络延迟估计。

**setup 不确定性**——0.15ns，注释里把 60/40/50ps 三项相加讲得很清楚：
[MY-Design/My_Design.cons:44-49](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L44-L49) —— `set_clock_uncertainty -setup 0.15 [get_clocks clk]`。

**时钟翻转时间**——最大 120ps（0.12ns）：
[MY-Design/My_Design.cons:52-54](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L52-L54) —— `set_clock_transition -max 0.12 [get_clocks clk]`。

注意对象的取法变了：定义时钟用 `get_ports clk`（端口），而描述时钟属性用 `get_clocks clk`（已被 `create_clock` 建立出来的时钟对象）——这是初学者最常踩的坑。

#### 4.2.4 代码实践

**实践目标**：体会不确定性如何「吃掉」周期预算。

**操作步骤**：
1. 假设周期 \( T = 3.0\,\text{ns} \)，setup 不确定性 \( 0.15\,\text{ns} \)；
2. 计算一条寄存器到寄存器路径的可用预算 \( T_{\text{可用}} = 3.0 - 0.15 \)；
3. 再把不确定性改成 0.3ns，重新算可用预算，体会变化。

**需要观察的现象**：不确定性越大，可用预算越小，内部组合逻辑允许的最大延迟越小。

**预期结果**：\( 3.0 - 0.15 = 2.85\,\text{ns} \)；不确定性改 0.3ns 后只剩 \( 2.7\,\text{ns} \)。具体每条路径是否达标需在 STA 工具里看 slack，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：源延迟和网络延迟有什么本质区别？

> **答案**：源延迟是时钟在**芯片外部**（从理想源到 `clk` 管脚）的延迟；网络延迟是时钟在**芯片内部**（从管脚分发到各寄存器）的延迟。前者用 `-source` 标注，后者不带。

**练习 2**：为什么 `set_clock_uncertainty` 要加 `-setup`？还会有别的取值吗？

> **答案**：setup 和 hold 检查面对的不确定性来源不同（例如 hold 更怕 skew），所以通常分别用 `-setup` 和 `-hold` 给不同的值。本例只为 setup 设了 0.15ns，hold 不确定性未单独给出（实际项目中通常也会补一条 `-hold`）。

---

### 4.3 输入/输出延迟（input / output delay）

#### 4.3.1 概念说明

芯片不是孤岛。输入端口外面接的是「别处寄存器 + 组合逻辑」，输出端口外面接的也是。工具看不见芯片外面，所以我们要用 `set_input_delay` / `set_output_delay` 把外部世界**假装**成一对虚拟寄存器，并告诉工具它们各自占用多少时间预算。

- **`set_input_delay`**：在输入端口外假设一个**发射寄存器（launching register）**，该值表示「数据从发射时钟沿到达本端口的延迟」。
- **`set_output_delay`**：在输出端口外假设一个**捕获寄存器（capturing register）**，该值表示「数据从本端口到外部寄存器并满足其 setup 所需的时间」。**当外部没有组合逻辑时，output delay 就等于外部寄存器的 setup time**——这是最重要的定义。

#### 4.3.2 核心流程

**输入延迟预算公式**（以 `data*` 为例，文件注释给出）：

\[
t_{\text{input}} = T - t_{\text{uncertainty}} - t_{\text{外部逻辑}S} - t_{\text{setup}}
\]

含义：一个周期里，先刨掉不确定性、再刨掉外部逻辑 S 的延迟、再刨掉内部捕获寄存器的 setup，剩下的才是「允许数据晚到端口」的预算。

**输出延迟的两种典型情形**：
- 外部仅有 setup 要求时：\( t_{\text{output}} = t_{\text{外部setup}} \)（如本例 `out3` 的 0.4ns）；
- 信号在片内先走一段、再在片外被捕获时：\( t_{\text{output}} = (T - t_{\text{uncertainty}}) - t_{\text{内部延迟}} \)（如本例 `out2` 的 2.04ns）。

**纯组合路径**（`Cin* → Cout`）没有时钟寄存器，工具仍需要一个时钟基准。做法是「假装」输入端有发射寄存器、输出端有捕获寄存器，让输入/输出延迟之和等于：

\[
t_{\text{input}} + t_{\text{output}} = T - t_{\text{uncertainty}} - t_{\text{组合最大延迟}}
\]

#### 4.3.3 源码精读

**输入延迟 `data*`（data1/data2）——按预算公式反算**：
[MY-Design/My_Design.cons:63-67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L63-L67) —— 注释给出 \( 3.0 - 0.15 - 2.2 - 0.2 = 0.45\,\text{ns} \)，命令 `set_input_delay -max 0.45 -clock clk [get_ports data*]`。`data*` 是通配符，匹配 `data1`、`data2`；`-clock clk` 指明相对哪个时钟沿算延迟。

**输入延迟 `sel`——由绝对到达时间换算**：
[MY-Design/My_Design.cons:70-73](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L70-L73) —— 注释说 `sel` 外部绝对到达 1.4ns，而时钟总延迟（源 0.7 + 网络 0.3 = 1.0ns），所以相对输入延迟 \( 1.4 - 1.0 = 0.4\,\text{ns} \)。这条恰好展示了源延迟 + 网络延迟在输入延迟换算里的真实用途。

**输出延迟 `out1`——外部组合 + setup**：
[MY-Design/My_Design.cons:82-84](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L82-L84) —— 注释 \( 420 + 80 = 500\,\text{ps} = 0.5\,\text{ns} \)，命令 `set_output_delay -max 0.5 -clock clk [get_ports out1]`。

**输出延迟 `out2`——片内延迟 + 下一拍捕获**：
[MY-Design/My_Design.cons:87-91](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L87-L91) —— 注释解释：外部捕获沿在发射后 \( 3.0\,\text{ns} \)（下一拍），扣掉不确定性 \( 0.15\,\text{ns} \) 得 2.85ns，再减片内到端口延迟 \( 0.81\,\text{ns} \)，故 \( t_{\text{output}} = 2.85 - 0.81 = 2.04\,\text{ns} \)。

**输出延迟 `out3`——直接等于外部 setup**：
[MY-Design/My_Design.cons:94-97](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L94-L97) —— 注释「out3 相对外部捕获寄存器的 setup 要求是 400ps」，按定义这正是 output delay，故 `set_output_delay -max 0.4`。

**纯组合路径 `Cin*/Cout`**：
[MY-Design/My_Design.cons:106-113](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L106-L113) —— 注释推导组合最大延迟 2.45ns，要求输入 + 输出延迟之和 \( = 3.0 - 0.15 - 2.45 = 0.4\,\text{ns} \)，这里选择输入 0.3 + 输出 0.1 的分配。`get_ports Cin*` 匹配 `Cin1`、`Cin2`。

把约束对应回 RTL：`data*` 走的是 `MY_DESIGN.v` 第 9 行 `ARITH` 例化 → 顶层 `always @(posedge clk)` 的**寄存器路径**；而 `Cin*/Cout` 走的是第 10 行 `COMBO` 例化的**纯组合路径**，所以二者约束写法不同。

#### 4.3.4 代码实践

**实践目标**：亲手跑一遍输入延迟预算公式。

**操作步骤**：
1. 假设某设计中 \( T = 3.0\,\text{ns} \)、不确定性 0.15ns、外部逻辑 S = 1.0ns、内部 setup 0.2ns；
2. 套用公式 \( t_{\text{input}} = T - t_{\text{uncertainty}} - S - t_{\text{setup}} \) 求输入延迟；
3. 对照 [MY-Design/My_Design.cons:63-67](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L63-L67) 的注释验证自己的算法一致。

**需要观察的现象**：外部逻辑 S 越大，留给数据晚到的输入延迟预算越小。

**预期结果**：\( 3.0 - 0.15 - 1.0 - 0.2 = 1.65\,\text{ns} \)。该数值最终是否满足，需在 STA 工具中看路径 slack，「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `out3` 的 output delay 直接写 0.4ns 就行，而 `out2` 却要算 2.04ns？

> **答案**：`out3` 外部直接接捕获寄存器、无组合逻辑，按定义 output delay = 外部 setup = 0.4ns；`out2` 片内到端口已有 0.81ns 延迟，外部捕获沿又落在下一拍（2.85ns），所以要把外部可用的余量算出来，\( 2.85 - 0.81 = 2.04\,\text{ns} \)。

**练习 2**：`Cin*` 和 `data*` 都是输入端口，为什么前者的输入延迟（0.3ns）和后者（0.45ns）不同？

> **答案**：二者外部环境不同。`data*` 接外部发射寄存器并经过逻辑 S，按周期预算反算得 0.45ns；`Cin*` 是纯组合通路的输入端，其延迟值是和输出端 `Cout` 的 output delay「凑」出来的（二者之和须等于 0.4ns），所以可取 0.3ns。

---

### 4.4 驱动单元与负载（环境约束）

#### 4.4.1 概念说明

到这里时钟和 I/O 延迟都已就位，但时序分析还差最后一环：**外部信号是怎么进入芯片的、芯片输出又驱动了多重的外部负载**。这些会显著改变内部单元的延迟，因为：

- **输入翻转时间（input transition）**：输入信号斜率越缓，第一个内部单元的延迟越大。`set_driving_cell` 用一个库里的标准单元来等效「外部驱动器」，由工具推算出输入斜率；`set_input_transition` 则直接给定斜率值。
- **输出负载（load）**：输出端口驱动的电容越大，最后一个内部单元的延迟越大。`set_load` 给定输出端口看到的外部电容。

三者合称**环境约束**：它们不改变逻辑，却直接决定内部时序路径的延迟计算结果。

#### 4.4.2 核心流程

1. 工具为每个输入端口确定一个 transition（来自 driving_cell 或 input_transition）；
2. 工具为每个输出端口确定一个 load（来自 set_load）；
3. 在计算内部路径延迟时，把首单元的输入斜率、末单元的输出负载代入工艺库的延迟表（NLDM/CCS 等），得到更真实的延迟值。

这些约束只影响「延迟数值」，不影响「时序是否需要检查」——后者由时钟和 I/O 延迟决定。

#### 4.4.3 源码精读

**`set_driving_cell`——除 `clk`、`Cin*` 外的所有输入由 `bufbd1` 驱动**：
[MY-Design/My_Design.cons:123-126](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L123-L126) —— 命令：

```tcl
set_driving_cell -max -lib_cell bufbd1 \
 [remove_from_collection [all_inputs] [get_ports "clk Cin*"]]
```

解读：`all_inputs` 取所有输入端口，再用 `remove_from_collection` 减去 `clk` 和 `Cin*`，剩下的 `data1/data2/sel` 才由 `bufbd1` 驱动。`clk` 不需要驱动单元（它是时钟源），`Cin*` 单独用下一条命令处理。注意反斜杠 `\` 是 Tcl 的续行符。

**`set_input_transition`——`Cin*` 是芯片级输入，直接给 120ps 斜率**：
[MY-Design/My_Design.cons:129-131](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L129-L131) —— `set_input_transition -max 0.12 [get_ports Cin*]`。

**`set_load`——除 `Cout` 外的输出驱动 2 个 `bufbd7` 负载**：
[MY-Design/My_Design.cons:134-136](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L134-L136) —— 命令：

```tcl
set_load -max [expr {2 * [load_of cb13fs120_tsmc_max/bufbd7/I]}] [get_ports out*]
```

解读：`load_of 库/单元/引脚` 取该引脚的电容值，`[expr {...}]` 计算 Tcl 表达式（这里是 2 倍），即 `out1/out2/out3` 各自驱动两个 `bufbd7` 输入的负载。`get_ports out*` 匹配 `out1/out2/out3`（注意 `Cout` 以 C 开头，**不会被匹配**，故需单独设）。

**`set_load`——`Cout` 驱动 0.025pF**：
[MY-Design/My_Design.cons:139-141](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/My_Design.cons#L139-L141) —— `set_load -max 0.025 [get_ports Cout*]`，注释「25fF = 0.025pF」。

把环境约束与端口对应回 RTL：被 `bufbd1` 驱动的是 `data1/data2/sel`（寄存器路径输入）；`Cin1/Cin2` 用固定斜率（芯片级输入）；`out1/out2/out3` 驱动 2× `bufbd7`，而 `Cout`（组合输出）驱动 0.025pF。可见**同一份约束对寄存器路径与组合路径的环境也分别建模**。

#### 4.4.4 代码实践

**实践目标**：理解 `load_of` + `expr` 如何组合出负载值。

**操作步骤**：
1. 阅读第 134-136 行，假设 `cb13fs120_tsmc_max/bufbd7/I` 引脚的电容为 \( C_7 \)；
2. 写出 `out1` 端口负载的表达式；
3. 想象把倍数从 2 改成 4，说明对末级单元延迟的影响方向。

**需要观察的现象**：负载翻倍，末级输出单元延迟增大，输出路径更难满足时序。

**预期结果**：负载 \( = 2 C_7 \)（pF）；改成 4 倍后末级单元延迟上升。具体延迟数值需在带工艺库的工具中查看，「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `clk` 和 `Cin*` 要从 `set_driving_cell` 的对象里剔除？

> **答案**：`clk` 是时钟源端口，其斜率由时钟相关约束（如 `set_clock_transition`）处理，不能用普通驱动单元建模；`Cin*` 作为芯片级输入，其斜率已由第 131 行 `set_input_transition` 直接给定为 0.12ns，故不能再套 driving_cell，否则会重复/冲突。

**练习 2**：`get_ports out*` 会匹配到 `Cout` 吗？为什么 `Cout` 要单独写一条 `set_load`？

> **答案**：不会。通配符 `out*` 只匹配以 `out` 开头的端口，即 `out1/out2/out3`；`Cout` 以 `C` 开头，故需单独用 `get_ports Cout*` 设 0.025pF 负载。这是初学者写通配约束时最容易漏掉的端口。

---

## 5. 综合实践

**综合实践目标**：把四个最小模块串起来——为 `MY_DESIGN` 把时钟从 333MHz 改成 **200MHz**，重新计算并改写 `input/output delay` 约束，生成一份新的 `.cons` 片段。

**背景**：200MHz 对应周期 \( T = 5.0\,\text{ns} \)。假设外部物理环境不变（外部逻辑 S = 2.2ns、内部 setup = 0.2ns、源延迟 0.7ns、网络延迟 0.3ns、setup 不确定性 0.15ns、片内到 out2 延迟 0.81ns、组合最大延迟 2.45ns）。

**操作步骤**：

1. **时钟**：把周期改为 5.0ns。

   ```tcl
   create_clock -period 5.0 [get_ports clk]
   ```

2. **时钟延迟/不确定性/翻转**：这些是物理估计值，与频率无关，**保持不变**（源延迟 0.7、网络延迟 0.3、setup 不确定性 0.15、transition 0.12）。

3. **`data*` 输入延迟**：套公式 \( T - 0.15 - 2.2 - 0.2 \)。

   ```tcl
   set_input_delay -max 2.45 -clock clk [get_ports data*]
   ```

   计算：\( 5.0 - 0.15 - 2.2 - 0.2 = 2.45\,\text{ns} \)。

4. **`sel` 输入延迟**：基于外部绝对到达时间，外部不变则值不变。

   ```tcl
   set_input_delay -max 0.4 -clock clk [get_ports sel]
   ```

   计算：\( 1.4 - 1.0 = 0.4\,\text{ns} \)（外部到达与延迟均未变）。

5. **`out1` 输出延迟**：纯外部量（420+80ps），不变。

   ```tcl
   set_output_delay -max 0.5 -clock clk [get_ports out1]
   ```

6. **`out2` 输出延迟**：下一拍捕获沿 \( = T - 0.15 = 4.85\,\text{ns} \)，减片内 0.81ns。

   ```tcl
   set_output_delay -max 4.04 -clock clk [get_ports out2]
   ```

   计算：\( 4.85 - 0.81 = 4.04\,\text{ns} \)。

7. **`out3` 输出延迟**：等于外部 setup，不变。

   ```tcl
   set_output_delay -max 0.4 -clock clk [get_ports out3]
   ```

8. **组合 `Cin*/Cout`**：输入 + 输出之和 \( = 5.0 - 0.15 - 2.45 = 2.4\,\text{ns} \)，自由分配，这里取输入 1.2 + 输出 1.2。

   ```tcl
   set_input_delay -max 1.2 -clock clk [get_ports Cin*]
   set_output_delay -max 1.2 -clock clk [get_ports Cout]
   ```

9. **环境约束**：保持不变（driving_cell、input_transition、set_load 与频率无关）。

**需要观察的现象**：周期从 3.0ns 放宽到 5.0ns 后，所有「按周期反算」的延迟（`data*`、`out2`、组合路径之和）都明显变大，说明 200MHz 比 333MHz 时序预算更宽松，设计更容易满足。

**预期结果汇总表**（200MHz / 5.0ns 版本）：

| 约束对象 | 333MHz 原值 | 200MHz 新值 | 是否随周期变 |
|----------|------------|------------|-------------|
| `create_clock` period | 3.0 | 5.0 | 是 |
| 时钟延迟/不确定性/transition | 0.7/0.3/0.15/0.12 | 不变 | 否 |
| `data*` input delay | 0.45 | 2.45 | 是 |
| `sel` input delay | 0.4 | 0.4 | 否（外部决定） |
| `out1` output delay | 0.5 | 0.5 | 否（外部决定） |
| `out2` output delay | 2.04 | 4.04 | 是 |
| `out3` output delay | 0.4 | 0.4 | 否（外部 setup） |
| `Cin*`/`Cout` 之和 | 0.4 | 2.4 | 是 |
| 环境约束 | 见原文件 | 不变 | 否 |

> 把改写后的约束真正喂给 Design Compiler 或 PrimeTime、查看各路径 slack 是否为正，需要工艺库与工具许可，本环境无法运行，「待本地验证」。

## 6. 本讲小结

- **时钟是一切时序分析的根**：`create_clock -period 3.0 [get_ports clk]` 定义了 333MHz、3.0ns 周期的时间基准；周期与频率互为倒数 \( T = 1/f \)。
- **时钟延迟分两类**：`-source` 标注的片外源延迟（0.7ns）与不带 `-source` 的片内网络延迟（0.3ns），它们主要用于输入/输出延迟的相对换算。
- **不确定性吃掉周期预算**：setup 不确定性 0.15ns = skew 60ps + jitter 40ps + margin 50ps，使可用预算变为 \( T - 0.15 \)。
- **I/O 延迟 = 虚拟外部寄存器**：`set_input_delay` 按预算公式 \( T - t_{\text{unc}} - S - t_{\text{setup}} \) 反算；`set_output_delay` 在无外部逻辑时直接等于外部 setup，否则按捕获沿减片内延迟计算。
- **环境约束决定延迟数值**：`set_driving_cell` / `set_input_transition` 给输入斜率，`set_load` 给输出电容，它们改变内部单元延迟但不改变是否需要检查。
- **约束与 RTL 端口一一对应**：`data*` 走寄存器路径、`Cin*/Cout` 走组合路径，二者约束写法不同；通配符 `out*` 不会误匹配 `Cout`。

## 7. 下一步学习建议

本讲你掌握了**如何用 SDC 描述一个设计的时序环境**，但这些约束最终要在工具里「跑出结果」。建议下一步：

- **进入 U3「库数据与物理数据准备」**：理解 `set_driving_cell` 里的 `bufbd1`、`set_load` 里的 `bufbd7` 这些标准单元来自哪里——即 Liberty（`.lib`/`.db`）时序库与 LEF 物理库，以及它们如何被组织成 ICC2 的 NDM 参考库。
- **阅读 `IC Compiler II/Scripts/01_common_setup.tcl`**：看看真实 PnR 流程里，时钟、约束、库变量是如何被集中设置的，本讲的 `.cons` 正是其中约束部分的雏形。
- **为 U6 PrimeTime 静态时序分析打基础**：本讲的 slack、setup、uncertainty 概念将在 PrimeTime 里以具体数字呈现；到时可对照 `PrimeTime/` 脚本看这些 SDC 如何被读入并报告。
- **延伸阅读**：如果想深入 SDC 细节，可结合仓库里的 `Guide to HDL Coding Styles for Synthesis` 与 HDL Compiler 文档，理解可综合 RTL 与约束的配合关系。
