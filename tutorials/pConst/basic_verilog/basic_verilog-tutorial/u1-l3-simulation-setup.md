# 用仿真器跑起来：testbench 模板与编译

## 1. 本讲目标

basic_verilog 是一堆「可综合」的硬件模块，意思是它们最终会被综合成真实芯片里的逻辑门。但在把模块烧进 FPGA 之前，我们必须先在**仿真器**里验证它「想得对不对」。本讲教你怎样把一个模块「跑起来」。

学完本讲你应该能够：

- 看懂一个 SystemVerilog testbench（测试平台，简称 tb）是怎样**凭空产生时钟和复位**的。
- 理解 `` `timescale `` 这条编译指令如何给仿真世界定义「时间单位」和「时间精度」。
- 会用仓库提供的 `sim_clk_gen` 模块，按频率/相位/占空比/抖动生成仿真时钟。
- 能用仓库提供的 iverilog 脚本或 ModelSim 脚本，把 `.sv` 文件编译、运行、并看到波形。

本讲是整个手册的「点火」环节：前两讲你认识了项目和模块写法，本讲让你**第一次在屏幕上看到波形**。后面所有讲义的实践任务都依赖这一讲建立的仿真能力。

## 2. 前置知识

在动手前，先用大白话建立三个直觉。

**直觉一：testbench 不是硬件。** 你在 u1-l2 学到的 `module`（如 `clk_divider`）是「可综合」的，最终会变成门电路。而 testbench 是一段**只在仿真器里运行、不会被综合**的代码。它的角色是「测试仪器」：给被测模块（DUT, Device Under Test）施加激励（时钟、复位、输入信号），然后观察输出。因此 tb 里可以大胆使用真实硬件里不允许的结构，比如 `initial`、`#10` 延时、`$display` 打印、`forever` 死循环。

**直觉二：仿真世界里没有真正的时钟。** 真实板子上有一颗 50 MHz 的晶振在不停振荡；但在仿真器里，「时间」是靠代码里的 `#延时` 一点一点推进的。所谓「200 MHz 时钟」，本质上是「每隔 2.5 ns 把信号翻转一次」的一段循环代码。谁去翻转它？就是 testbench。

**直觉三：仿真时间需要一把「尺子」。** 你写 `#2.5`，仿真器怎么知道这是 2.5 ns 还是 2.5 ps？这把尺子就是 `` `timescale ``。没有它，所有延时数字都没有物理含义。

如果你对 `module`、端口例化、`always_ff`/`always_comb`、同步/异步复位这些概念还生疏，请先回到 u1-l2 复习。本讲会直接使用它们。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [main_tb.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv) | 仓库根目录的「testbench 模板」，演示**手写**时钟/复位/随机激励的原始写法。 |
| [sim_clk_gen.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv) | 参数化的「仿真时钟发生器」模块，按 Hz/度/百分比/ps 生成理想时钟和带抖动时钟。 |
| [scripts/iverilog_compile.bat](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat) | 用免费工具 Icarus Verilog (iverilog) + GTKWave 编译并查看波形的脚本。 |
| [scripts/modelsim_compile.tcl](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl) | 用商业工具 ModelSim/QuestaSim 编译运行的 Tcl 脚本。 |
| [clk_divider.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv) | 本讲综合实践的「被测模块」（u1-l2 已介绍过它的写法）。 |
| [example_projects/testbench_template_tb/](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/testbench_template_tb/main_tb.sv) | 一个**自包含、可直接编译**的 testbench 模板工程，是上手仿真的最佳起点。 |

> 提醒：`scripts/` 下的两个脚本是「模板」。它们里面写死了 `..\main.sv`、`c_rand.v` 等路径，但这些文件并不在仓库根目录——你需要按自己的文件位置改路径。想要「复制即跑」的体验，请用 `example_projects/testbench_template_tb/`，它把所有依赖和编译脚本都打包在一起了。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「先会标尺 → 再会手写时钟 → 再会用模块化时钟 → 最后会编译」的顺序推进。

### 4.1 timescale：仿真世界的「时间标尺」

#### 4.1.1 概念说明

`` `timescale `` 是一条**编译指令**（注意前面那个反引号 `` ` ``，它不是普通单引号）。它告诉仿真器两件事：

1. **时间单位（time unit）**：源码里所有 `#延时` 数字、以及 `$time` 等系统函数，默认以什么为单位。
2. **时间精度（time precision）**：仿真器能分辨的最小时间粒度，比这更细的延时会被四舍五入。

写法是 `` `timescale <单位> / <精度> ``。比如本仓库 ubiquitous 使用的：

```systemverilog
`timescale 1ns / 1ps
```

意思是「单位 = 1 纳秒，精度 = 1 皮秒」。于是 `#2.5` 就是 2.5 ns，而 `#0.001` 会被舍入到最近的 1 ps 网格上。

#### 4.1.2 核心流程

- 仿真器读到 `` `timescale 1ns / 1ps `` 后，建立一个「时间网格」，网格步长 = 1 ps。
- 之后每遇到 `#n`，就把 `n` 个「单位」（ns）换算成皮秒：`n ns = n × 1000 ps`，再对齐到 1 ps 网格。
- `#2.5` → 2500 ps → 2.5 ns；`#10.2` → 10200 ps → 10.2 ns。
- 如果精度是 1 ns（如 `1ns/1ns`），则 `#2.5` 会被舍入成 `#3` 或 `#2`——时钟周期直接变了！所以**精度必须比单位细**，否则会失真。

一句话记忆：**单位决定数字的「含义」，精度决定数字的「分辨率」**。

#### 4.1.3 源码精读

仓库里凡是 testbench 相关的文件，第一行几乎都是这条指令。

[main_tb.sv:13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L13) 设置了本讲默认的时间标尺：

```systemverilog
`timescale 1ns / 1ps
```

[sim_clk_gen.sv:11](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L11) 同样使用它——这一点很关键，因为 `sim_clk_gen` 内部要用 `real`（实数）类型算出 `2.5 ns` 这种带小数的周期，必须依赖 1 ps 的精度才能精确表达。

正因为有了这把尺子，下面 main_tb.sv 里手写的时钟 `#2.5` 才能被正确理解为 2.5 ns，从而构成一个周期 5 ns、即 200 MHz 的时钟。

#### 4.1.4 代码实践

**目标**：亲手体会「单位」与「精度」的差别。

**步骤**：

1. 想象一段最小时钟代码：`initial begin clk=0; forever #2.5 clk=~clk; end`。
2. 在 `` `timescale 1ns / 1ps `` 下，写出 `clk` 的周期与频率。
3. 假设把指令改成 `` `timescale 1ns / 1ns ``（精度变粗），再写出 `clk` 的周期。

**需要观察的现象**：精度变粗后，`#2.5` 被舍入，周期会发生跳变。

**预期结果**：

- `1ns/1ps` 下：半周期 2.5 ns，周期 5 ns，频率 200 MHz。
- `1ns/1ns` 下：`#2.5` 舍入为 `#2` 或 `#3`（取决于仿真器），周期变成 4 ns 或 6 ns，频率随之变成 250 MHz 或 ~167 MHz。

> 待本地验证：不同仿真器对 `.5` 的舍入方向可能不同（向上/向下/最近），实际数值请以你本机工具为准。

#### 4.1.5 小练习与答案

**练习 1**：`` `timescale 1ns/1ps `` 下，`#10.2` 表示多少时间？
**答**：10.2 ns（= 10200 ps）。

**练习 2**：如果把指令改成 `` `timescale 1ps/1ps ``，同样写 `#2.5`，时钟频率会变成多少？
**答**：此时单位是 1 ps，`#2.5` 舍入为 2 ps 或 3 ps，半周期约 2~3 ps，周期约 4~6 ps，频率约 167~250 GHz——完全脱离预期。可见**改 timescale 等于改了所有延时的物理含义**。

---

### 4.2 用 initial 块手写时钟与复位

#### 4.2.1 概念说明

testbench 最核心的技能，是用 `initial` 块「凭空捏造」出时钟和复位。

- `initial begin ... end`：在仿真时刻 0 开始**执行一次**，按顺序执行里面的语句，遇到 `#延时` 就把仿真时间向前推。
- `forever`：在 `initial` 里写一个永不退出的循环，常用来产生周期性翻转的时钟。
- 由于 tb 不可综合，这里可以尽情用 `initial` + `forever` 这种「真实硬件里严禁」的结构。

main_tb.sv 演示了**最原始、最直观**的手写法：每个时钟、每个复位都用一个独立的 `initial` 块描述。

#### 4.2.2 核心流程

一个「理想时钟」的产生模板：

```
在 t=0 把 clk 拉低
永远重复：延时半个周期，翻转 clk
```

一个「单次复位」的产生模板（同步设计中常见的上电复位）：

```
在 t=0 rst=0
延时一段时间后 rst=1（释放复位，假设高有效）
再延时一小段后 rst=0（可选）
```

如果还想模拟「复位偶尔抖动」，就在 `initial` 里继续用 `forever` 反复拉高拉低。另外，仓库统一约定**低有效复位**命名为 `nrst`（not-reset），所以代码里常看到 `assign nrst = ~rst;` 把高有效 `rst` 翻成低有效 `nrst` 喂给模块。

#### 4.2.3 源码精读

**理想 200 MHz 时钟** —— [main_tb.sv:17-22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L17-L22)：

```systemverilog
logic clk200;
initial begin
  #0 clk200 = 1'b0;
  forever
    #2.5 clk200 = ~clk200;
end
```

这段做了两件事：`#0` 在 0 时刻把 `clk200` 初始化为 0（避免仿真开始时是 `x` 未定态）；然后 `forever #2.5` 每 2.5 ns 翻转一次 → 周期 5 ns → 200 MHz。

**「异步」慢时钟** —— [main_tb.sv:24-30](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L24-L30) 用同样的手法产生第二个频率完全不同的时钟 `clk33a`（`#7` → 周期 14 ns → ~71.4 MHz），用来模拟一个外部异步器件的时钟域。

**带抖动的时钟** —— [main_tb.sv:32-36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L32-L36)：

```systemverilog
always @(*) begin
  clk33 = #($urandom_range(0, 2000)*10ps) clk33a;
end
```

这里把理想的 `clk33a` 整体随机延迟 0~20000 ps（0~20 ns），模拟真实时钟的**相位漂移/抖动**，用于压力测试跨时钟域逻辑。`$urandom_range(a,b)` 返回 `[a,b]` 区间的随机整数，是 tb 里制造随机激励的常用手段。

**复位发生器** —— [main_tb.sv:38-48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L38-L48)：

```systemverilog
initial begin
  #0 rst = 1'b0;
  #10.2 rst = 1'b1;
  #5 rst = 1'b0;
  forever begin
    #9985 rst = ~rst;
    #5 rst = ~rst;
  end
end
```

先在 10.2 ns 给一个上电复位脉冲（高有效 5 ns），随后用 `forever` 周期性地再次拉高拉低 `rst`——模拟运行中「复位抖动」，专门用来暴露那些对复位毛刺敏感的电路。注意 `#10.2`、`#9985` 这种**非整数倍时钟周期**的偏移是故意的，目的是让复位边沿相对时钟沿「滚动」，覆盖不同的时序关系。

**高低有效转换** —— [main_tb.sv:50-51](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L50-L51) 用一句 `assign nrst = ~rst;` 把高有效翻转成模块要的低有效。

**一次性复位** —— [main_tb.sv:53-61](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/main_tb.sv#L53-L61) 定义了 `rst_once`/`nrst_once`：它**只在上电时复位一次**，之后不再抖动。这是大多数实际模块期望的「干净」复位；前面那个会反复抖动的 `rst` 则用于极限测试。读者在例化模块时，通常接 `nrst_once`。

> 对比阅读：`example_projects/testbench_template_tb/main_tb.sv` 给出了**同步复位**的另一种写法——用 `repeat(N) @(posedge clk200);` 把复位边沿对齐到时钟沿，详见其 [main_tb.sv:75-92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/testbench_template_tb/main_tb.sv#L75-L92)。两种风格都合法：本节是「绝对时间 `#` 驱动」，那种是「事件 `@` 驱动」。

#### 4.2.4 代码实践

**目标**：手写一个 100 MHz 时钟 + 一个一次性高有效复位，并算对延时。

**步骤**：

1. 100 MHz 的周期 = 1/100 MHz = 10 ns，半周期 = 5 ns。
2. 写出：

   ```systemverilog
   logic clk100;
   initial begin
     clk100 = 1'b0;
     forever #5 clk100 = ~clk100;
   end

   logic rst_once;
   initial begin
     rst_once = 1'b0;
     #23 rst_once = 1'b1;   // 在 23 ns 释放复位
     #7  rst_once = 1'b0;
   end
   ```

3. 把它放进一个空 testbench，在波形里数 `clk100` 的周期、确认 `rst_once` 的脉冲位置。

**需要观察的现象**：`clk100` 每 5 ns 翻转一次；`rst_once` 在 23 ns 变高、30 ns 变低。

**预期结果**：`clk100` 周期稳定为 10 ns；`rst_once` 高电平持续 7 ns。把 `#5` 误写成 `#2.5` 会得到 200 MHz——这是最常见的笔误。

#### 4.2.5 小练习与答案

**练习 1**：为什么 main_tb.sv 里写 `#10.2` 而不是整齐的 `#10`？
**答**：为了让复位相对时钟沿产生「滚动」的相位关系，覆盖更多时序 corner，提升测试覆盖率的随机化（randomization）手段。

**练习 2**：`assign nrst = ~rst;` 中，如果 `rst` 在某个时刻是 `x`（未定态），`nrst` 会是什么？
**答**：按 Verilog 四值逻辑，`~x = x`，`nrst` 也是 `x`。这就是为什么时钟和复位都要在 `#0` 或 `initial` 开头显式初始化，避免上游模块在 `x` 复位下行为异常。

---

### 4.3 sim_clk_gen：参数化的仿真时钟发生器

#### 4.3.1 概念说明

4.2 节的手写时钟够直观，但有个痛点：换一个频率就要重算半周期、改 `#` 数字。`sim_clk_gen` 把这件事**封装成一个模块**：你只要用人话告诉它「我要 200 MHz、相位 0°、占空比 50%、抖动 200 ps」，它自己算出该怎么翻转，并额外输出一路带抖动的 `clkd`。

这是一个「**tb 专用、不可综合**」的模块——它内部用了 `real`（实数）、`while`、`$display` 这些只有仿真器才认识的结构。它的价值在于**复用**：整个手册里但凡需要高质量仿真时钟，都可以直接例化它。

#### 4.3.2 核心流程

`sim_clk_gen` 的工作流程：

1. **参数换算**：把「Hz/度/百分比」换算成「ns」级别的实数周期。
   - 周期 `clk_pd = 1/FREQ × 10⁹` ns。
   - 高电平时长 `clk_on = 占空比% × clk_pd`，低电平 `clk_off = (100 − 占空比%) × clk_pd`。
   - 起始相位延迟 `start_dly = clk_pd/4 × 相位/90`。
2. **打印配置**：用 `$display` 把换算结果打到日志，方便核对。
3. **门控**：`ena` 信号拉低时停止输出时钟。
4. **翻转**：在 `always` 块里循环 `#clk_on 置 0`、`#clk_off 置 1`，得到理想 `clk`。
5. **加抖动**：把理想 `clk` 整体随机延迟 0~`DISTORT` ps，得到 `clkd`。

周期换算的核心公式（FREQ 单位为 Hz，结果单位为 ns）：

\[
\text{clk\_pd} = \frac{1}{\text{FREQ}} \times 10^{9}
\]

例如 FREQ = 200 000 000 Hz 时：

\[
\text{clk\_pd} = \frac{1}{2 \times 10^{8}} \times 10^{9} = 5 \text{ ns}
\]

正好对应 4.2 节手写的 `#2.5` 半周期。

#### 4.3.3 源码精读

**参数与端口** —— [sim_clk_gen.sv:13-22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L13-L22)：

```systemverilog
module sim_clk_gen #( parameter
  FREQ  = 200_000_000,  // in Hz
  PHASE = 0,            // in degrees
  DUTY  = 50,           // in percentage
  DISTORT = 200         // in picoseconds
)(
  input  ena,
  output logic clk,     // ideal clock
  output logic clkd     // distorted clock
);
```

四个参数全是「人话单位」：频率 Hz、相位度、占空比百分比、抖动 ps。两个输出：`clk` 是理想方波，`clkd` 是加了随机抖动的版本。

**实数换算** —— [sim_clk_gen.sv:24-27](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L24-L27) 用 `real` 类型（仿真专用）算出周期与高/低电平时长。注意 `1.0 / FREQ * 1e9` 这一句正是上面公式的直接翻译。

**配置回显** —— [sim_clk_gen.sv:31-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L31-L41) 在仿真开始时 `$display` 出全部参数和换算结果。仿真日志开头你会看到 `PERIOD = 5.000 ns` 之类的行，**这是核对「我设的频率对不对」的最快途径**。

**门控 + 翻转循环** —— [sim_clk_gen.sv:48-65](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L48-L65)：先在 `ena` 边沿用 `start_dly` 延迟来引入相位，再用 `while(do_clk)` 循环按 `clk_on`/`clk_off` 翻转 `clk`。一旦 `ena` 拉低，`do_clk` 清零，循环退出，时钟停摆。

**带抖动输出** —— [sim_clk_gen.sv:67-69](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/sim_clk_gen.sv#L67-L69)：

```systemverilog
always @(*) begin
  clkd = #($urandom_range(0, DISTORT)*1ps) clk;
end
```

与 main_tb.sv 里手写抖动的手法完全一致，只是把「最大抖动」做成了参数 `DISTORT`。

**真实用法示例** —— [example_projects/testbench_template_tb/main_tb.sv:26-36](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/testbench_template_tb/main_tb.sv#L26-L36) 例化了 `sim_clk_gen` 来产生 `clk200`，参数 `FREQ=200_000_000, DISTORT=10`；同文件 [63-72 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/testbench_template_tb/main_tb.sv#L63-L72) 又例化了一个 `DISTORT=1000` 的实例当外部异步时钟 `clk33`。这就是它的典型用法：**一个实例 = 一路仿真时钟**。

#### 4.3.4 代码实践

**目标**：用 `sim_clk_gen` 生成一个非 50% 占空比、带抖动的时钟，并在日志和波形里核对。

**步骤**：

1. 例化：

   ```systemverilog
   logic clk100, clk100d;
   sim_clk_gen #(
     .FREQ( 100_000_000 ),
     .PHASE( 0 ),
     .DUTY( 25 ),       // 25% 占空比
     .DISTORT( 500 )    // 0~500 ps 抖动
   ) gen (
     .ena( 1'b1 ),
     .clk( clk100 ),
     .clkd( clk100d )
   );
   ```

2. 把它放进 testbench，运行几个微秒。
3. 查仿真日志里 `$display` 打出的 `CLK_ON` / `CLK_OFF` 值。

**需要观察的现象**：日志里 `PERIOD=10 ns, CLK_ON=2.5 ns, CLK_OFF=7.5 ns`；波形里 `clk100` 高电平明显比低电平短；`clk100d` 的边沿相对 `clk100` 有随机偏移。

**预期结果**：DUTY=25 时高电平占周期的 1/4（2.5 ns），低电平 7.5 ns，周期仍为 10 ns。

#### 4.3.5 小练习与答案

**练习 1**：要把一个 50 MHz 时钟接到 `sim_clk_gen`，`FREQ` 该填多少？
**答**：`50_000_000`（50 MHz = 50 000 000 Hz）。换算后周期 = 1/50e6 × 1e9 = 20 ns。

**练习 2**：`sim_clk_gen` 能不能被综合进 FPGA？为什么？
**答**：不能。它用了 `real`、`while`、`$display`、`$urandom_range` 等仿真专用结构，综合工具无法映射成门电路。它是「纯仿真 IP」，这也是它放在 testbench 体系里的原因。

---

### 4.4 编译脚本：从 .sv 到波形（iverilog 与 ModelSim）

#### 4.4.1 概念说明

写完 testbench 还不能直接「看波形」，要经过一套工具链：

1. **编译（compile）**：把 `.sv`/`.v` 源码翻译成仿真器能用的中间形式（iverilog 生成 `.vvp`，ModelSim 生成 `work` 库）。
2. **elaborate/加载**：选定一个顶层模块（top-level），把所有例化关系展开成完整的仿真电路。
3. **运行（run）**：推进仿真时间，在此期间把信号变化「录制」下来。
4. **看波（view）**：把录制文件（iverilog 是 `.vcd`，ModelSim 是内部波形数据库）用波形查看器打开。

仓库为这条工具链提供了两套脚本：免费的 **iverilog + GTKWave**，商业的 **ModelSim/QuestaSim**。它们本质做的是同一件事，只是命令不同。

#### 4.4.2 核心流程

**iverilog 路线**（三步）：

```
iverilog -g2012 -s <顶层> -o sim.vvp  <tb.sv> <模块.sv> ...
vvp      sim.vvp                 # 运行，生成 .vcd 波形
gtkwave  sim.vcd                 # 打开波形
```

要点：`-g2012` 启用 SystemVerilog-2012 语法（仓库大量使用 `logic`、`always_ff`、`'0` 等 SV 特性，必须开）；`-s` 指定顶层模块名；后面依次列出所有参与编译的源文件。要让 iverilog 输出波形，testbench 里必须加上 `$dumpfile`/`$dumpvars`（见下文源码精读里的注释模板）。

**ModelSim 路线**（`.tcl` 脚本驱动）：

```
vlog <文件>...        # 编译进 work 库
vsim work.<顶层>      # 加载顶层
add wave ...          # 添加要看的信号
run 100us             # 运行
```

仓库的 `modelsim_compile.tcl` 把这些命令打包，并加上了「增量编译」（只重编改过的文件）和 `r/rr/q` 快捷命令。

#### 4.4.3 源码精读

**iverilog 编译命令** —— [iverilog_compile.bat:8-14](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat#L8-L14)：

```bat
C:\iverilog\bin\iverilog.exe -Wall -g2012 -o iverilog_sim.vvp -s main_tb ^
      main_tb.sv ^
      ..\main.sv ^
      clk_divider.sv ^
      c_rand.v ^
      delay.sv ^
      edge_detect.sv
```

`-Wall` 打开全部警告，`-g2012` 选 SV-2012 标准，`-s main_tb` 指定 `main_tb` 为顶层，`-o` 指定输出的 `.vvp` 文件，最后是参与编译的源文件清单（行尾的 `^` 是 Windows 批处理的续行符）。注意：这份清单里的 `..\main.sv`、`c_rand.v` 等并不在仓库根目录，所以**这是模板，需要你按实际文件位置改写**——这也是本讲强调「用 `example_projects/testbench_template_tb/` 上手」的原因。

**运行 + 生成 VCD** —— [iverilog_compile.bat:17](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat#L17) 用 `vvp.exe iverilog_sim.vvp` 执行仿真。VCD（Value Change Dump）文件由 testbench 里的 `$dumpfile`/`$dumpvars` 触发生成，脚本在 [iverilog_compile.bat:35-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat#L35-L41) 以注释形式给出了**必须插入 testbench 的代码片段**：

```systemverilog
initial begin
  $dumpfile("iverilog_sim.vcd");
  $dumpvars( 0, M );     // 从实例 M 开始递归 dump 全部信号
  #10000 $finish;
end
```

没有这段，iverilog 不会产出 `.vcd`，GTKWave 也就没东西可看。`$dumpvars(0, M)` 的 `0` 表示「递归到所有层级」，`M` 是要录制的顶层实例名。

**ModelSim 脚本的配置区** —— [modelsim_compile.tcl:17-25](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L17-L25) 用 `set library_file_list` 声明 `work` 库里要编译哪些文件；[第 33 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L33) 的 `vsim_params` 指定仿真需要的预编译库（如 Altera 的 `altera_mf`）；[第 37 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L37) 的 `set top_level work.fifo_tb` 指定顶层。**换工程时，改这三处即可**，这正是脚本把配置和流程分离的设计意图。

**快捷命令** —— [modelsim_compile.tcl:46-50](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L46-L50) 定义了三个 Tcl 过程：`r`（只重编改动文件并重跑）、`rr`（全量重编）、`q`（直接退出）。在 ModelSim 控制台敲 `r` 回车，就能增量重编——比每次点菜单快得多。

**增量编译主循环** —— [modelsim_compile.tcl:60-78](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L60-L78) 遍历文件列表，比较每个文件的修改时间（`file mtime`）和上次编译时间，只 `vlog` 有更新的文件；`.vhd` 走 `vcom`，其余走 `vlog`。

**加载并运行** —— [modelsim_compile.tcl:81-87](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.tcl#L81-L87)：`eval vsim $top_level` 加载顶层，`do wave.do` 载入波形布局，`run 100us` 推进仿真 100 µs。把 `run 100us` 改成你需要的时长即可。

> 补充：`scripts/modelsim_compile.bat`（[第 10 行](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/modelsim_compile.bat#L10)）只是个一句话包装：`modelsim.exe -do modelsim_compile.tcl`，用来在 Windows 上双击启动上面这个 Tcl 脚本。

#### 4.4.4 代码实践

**目标**：理解脚本「改三处即可换工程」的结构，动手改 `modelsim_compile.tcl`。

**步骤**：

1. 打开 `scripts/modelsim_compile.tcl`。
2. 把 `library_file_list` 里的文件清单改成只剩 `main_tb.sv` 和 `clk_divider.sv`。
3. 把 `set top_level work.fifo_tb` 改成 `set top_level work.main_tb`。
4. 阅读第 60-78 行的增量编译循环，预测：如果你只改了 `main_tb.sv`，下次 `r` 会重编哪几个文件。

**需要观察的现象**：ModelSim 控制台里，只有 `main_tb.sv` 被 `vlog`，`clk_divider.sv` 因未改动被跳过。

**预期结果**：`clk_divider.sv` 不会出现在重编日志里，因为它在本轮没有改动。这正是增量编译的意义。

> 待本地验证：本实践需要安装 ModelSim/QuestaSim；若本机没有，可改为阅读 [iverilog_compile.bat](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat)，把源文件清单替换成你自己 tb 的文件。

#### 4.4.5 小练习与答案

**练习 1**：用 iverilog 编译仓库的 `.sv` 文件时，为什么必须加 `-g2012`？
**答**：仓库用 SystemVerilog-2012 语法（`logic`、`always_ff`、`always_comb`、`'0`/`'1`、`$urandom_range` 等）。iverilog 默认标准较旧，不加 `-g2012` 会报大量语法错误。

**练习 2**：iverilog 跑完没有任何 `.vcd` 文件生成，最可能的原因是什么？
**答**：testbench 里缺少 `$dumpfile("....vcd")` 和 `$dumpvars(...)`。这两条是「告诉 iverilog 把哪些信号变化录下来」的指令，缺了就不会产出波形文件。

---

## 5. 综合实践

把本讲四个模块串起来：写一个**最小的、能直接编译的** testbench，例化 `clk_divider`，在波形里观察 `out[]` 的逐位翻转，并跑通一次 iverilog 仿真。这个任务正是本讲规格里指定的实践。

**实践目标**：亲手走完「写 tb → 编译 → 运行 → 看波形」全流程，并验证 `clk_divider` 的 `out[N]` 频率等于 `clk / 2^(N+1)`。

**第一步：理解被测模块**。回顾 [clk_divider.sv:35-41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clk_divider.sv#L35-L41) 的核心：一个自由运行的二进制计数器，每个时钟上升沿 `out <= out + 1`。因此 `out[0]` 每拍翻转（频率 = clk/2），`out[1]` 每 2 拍翻转（clk/4），`out[N]` = clk/2^(N+1)。

**第二步：写最小 testbench**（示例代码，非仓库原有文件）。基于 4.2 节的手写时钟法和 4.4 节的 `$dumpvars` 模板：

```systemverilog
`timescale 1ns / 1ps

module clk_divider_tb();

  // 4.1 + 4.2：手写 200 MHz 时钟
  logic clk;
  initial begin
    clk = 1'b0;
    forever #2.5 clk = ~clk;     // 半周期 2.5 ns → 200 MHz
  end

  // 4.2：一次性低有效复位（同步复位，故 nrst 初值为 0）
  logic nrst;
  initial begin
    nrst = 1'b0;
    #10 nrst = 1'b1;              // 第 10 ns 释放复位
  end

  // 被测模块：8 位宽计数器
  logic [7:0] out;
  clk_divider #( .WIDTH( 8 ) ) dut (
    .clk( clk ),
    .nrst( nrst ),
    .ena( 1'b1 ),
    .out( out )
  );

  // 4.4：dump 波形 + 限时结束
  initial begin
    $dumpfile("clk_divider_tb.vcd");
    $dumpvars( 0, clk_divider_tb );
    #2000 $finish;               // 跑 2 µs 就够看出 out[0..7] 的翻转
  end

endmodule
```

**第三步：编译运行**（iverilog，参考 [iverilog_compile.bat](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat)）。把上面两个文件（`clk_divider_tb.sv` 与仓库的 `clk_divider.sv`）放在同一目录，执行：

```bash
iverilog -Wall -g2012 -s clk_divider_tb \
         -o clk_divider_tb.vvp \
         clk_divider_tb.sv clk_divider.sv
vvp clk_divider_tb.vvp            # 生成 clk_divider_tb.vcd
gtkwave clk_divider_tb.vcd        # 打开波形
```

**ModelSim 等价命令**：

```tcl
vlog clk_divider_tb.sv clk_divider.sv
vsim -t 1ps work.clk_divider_tb
add wave -position insertpoint sim:/clk_divider_tb/clk
add wave -position insertpoint -radix binary sim:/clk_divider_tb/out
run 2000 ns
```

**需要观察的现象**：

- `out` 在 `nrst=0` 期间保持 0；第 10 ns `nrst=1` 后开始每拍递增。
- `out[0]` 每 5 ns（一个时钟周期）翻转一次，看起来是 clk/2 的方波。
- `out[1]` 翻转频率是 `out[0]` 的一半，`out[2]` 又是一半……逐位减半。
- 整个 `out[7:0]` 呈现「二进制计数器」的典型纹波图案。

**预期结果**（以 200 MHz 时钟为基准）：

| 位 | 频率 | 周期 |
| --- | --- | --- |
| out[0] | 100 MHz | 10 ns |
| out[1] | 50 MHz | 20 ns |
| out[2] | 25 MHz | 40 ns |
| out[N] | 200 MHz / 2^(N+1) | 5 ns × 2^N |

> 待本地验证：波形截图需在你本机的 GTKWave / ModelSim 中实际截取；上表数值可用 `$display` 在 tb 里打印 `out` 变化时刻来自动核对。

**延伸（可选）**：把上面 tb 里的手写时钟换成 4.3 节的 `sim_clk_gen` 例化（`FREQ=200_000_000`），验证两种方式产生的波形一致——这正是 `example_projects/testbench_template_tb/main_tb.sv` 采用的现代化写法。

## 6. 本讲小结

- **testbench 不可综合**，是「测试仪器」，可以放心用 `initial`、`#延时`、`forever`、`$display` 等仿真专用结构。
- `` `timescale <单位>/<精度> `` 是仿真世界的时间标尺：单位决定 `#n` 的物理含义，精度决定时间分辨率；改它等于改所有延时。
- 手写时钟的套路是 `initial begin clk=0; forever #半周期 clk=~clk; end`；复位用 `initial` 加 `#延时` 序列描述，并通过 `assign nrst=~rst;` 转成低有效。
- `sim_clk_gen` 把时钟生成封装成参数化模块，用 Hz/度/百分比/ps 描述，并额外提供带抖动的 `clkd`，是仓库推荐的复用方式。
- iverilog 路线 = `iverilog -g2012 -s ... -o ... 文件...` → `vvp` → GTKWave，需要 tb 里有 `$dumpfile`/`$dumpvars`；ModelSim 路线 = `vlog` → `vsim` → `run`，可用 `.tcl` 脚本做增量编译。
- `scripts/` 下的脚本是模板（路径需按实际改写），`example_projects/testbench_template_tb/` 是自包含、可直接编译的起点。

## 7. 下一步学习建议

- **横向巩固**：本讲你用 `clk_divider` 当了「被测模块」，下一讲 u2-l1《时钟分频：clk_divider》会深入讲它每一位的派生关系，建议紧接着读。
- **纵向走向真实硬件**：本讲只在仿真器里跑。u1-l4《建一个真实 FPGA 工程：example_projects 模板》会带你进入 Quartus/Vivado 工程，看引脚约束（`.qsf`/`.xdc`）和时序约束（`.sdc`/`.xdc`）如何让同一份 RTL 真正上板。
- **源码阅读建议**：把 `example_projects/testbench_template_tb/main_tb.sv` 完整读一遍，对照本讲，你会发现它同时演示了 `sim_clk_gen`、同步复位（`repeat(N) @(posedge clk)`）、`$timeformat`、`$urandom` 种子等进阶手法，是本讲最好的「习题答案」。
