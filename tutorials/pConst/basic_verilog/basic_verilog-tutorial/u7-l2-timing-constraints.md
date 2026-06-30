# 时序约束与收敛：SDC/XDC 与 false_path

## 1. 本讲目标

前面几讲我们一直在写「行为正确」的 RTL——仿真波形对就行。但 RTL 一旦要上真实 FPGA，还要过一道关：**时序收敛（timing closure）**——让综合器布线出来的电路，在你的目标时钟频率下真的能跑通，每一拍数据都稳稳赶上下一拍。这道关靠的不是改代码，而是写**时序约束文件**。

本讲不教新的 RTL 模块，而是讲「怎么用约束文件指挥时序工具」。读完本讲你应该能够：

- 说清 `create_clock` 在做什么——为什么必须由人告诉工具「时钟在哪个端口、多快」，工具自己算不出来。
- 理解 `set_false_path` 在做什么——为什么跨时钟域同步器的输入路径必须被「豁免」出时序分析，否则你永远无法收敛。
- 掌握仓库里的 `_SYNC_ATTR` / `_FP_ATTR` 命名约定：给同步器实例名加一个后缀，就能用**一条**约束管住工程里**所有**同步器。
- 学会用 `scripts/get_fmax_vivado.tcl` 把一份时序报告换算成**一个 Fmax 数字**（最高能跑多少 MHz），并用 `post_flow_*.tcl` 在综合结束后自动汇总编译耗时。

本讲所有约束和脚本都直接取自仓库的 `example_projects/` 工程模板与 `scripts/` 脚本目录，不引入任何外部约束技巧。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（这些都在前面的讲义里讲过）：

- **工程模板的结构**（u1-l4）：顶层 `main.sv` 用「输入寄存化 → 测试逻辑 → 输出寄存化」的三明治结构，配合 PLL 时钟 IP、`.qsf`/`.xdc` 引脚约束与 `.sdc`/`.xdc` 时序约束组成一个可上板工程。
- **跨时钟域同步器与 `delay.sv`**（u2-l3、u3-l1）：把异步信号串两级触发器（两级同步器）能压低亚稳态风险；`cdc_data.sv` 就是 `LENGTH=2` 的 `delay` 封装。同步器第一级触发器是亚稳态发生地，必须用 `set_false_path` 把进入它的那条路径排除出时序分析。
- **SystemVerilog 命名与综合**（u1-l2）：`module` / 例化名 / 寄存器名在综合后会变成网表里的层次化名字，约束正是靠匹配这些名字来「点中」具体单元的。

本讲会新引入的概念：建立时间（setup time）与时序余量（slack）、关键路径（critical path）、Fmax（最高时钟频率）、虚假路径（false path）、时钟域跨越（CDC）约束、`_ATTR` 命名约定、过约束（over-constraint）。

一个贯穿全讲的核心理念：**时序工具默认是「悲观且保守」的——它会把所有它不认识的路径都当成要在目标频率下准时送达的关键路径来检查。** 你的工作不是让所有路径都通过（有些路径本就不该被检查），而是用约束文件**告诉工具真相**：时钟到底多快、哪些路径是异步的可以放过。约束写对了，工具的精力才花在真正关键的路上，时序才能收敛，Fmax 才测得准。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用它做什么 |
|------|------|----------------|
| [example_projects/quartus_test_prj_template_v4/src/main.sdc](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sdc) | Quartus 工程模板的 SDC 时序约束 | 精读 `create_clock` + `derive_pll_clocks` + `derive_clock_uncertainty` |
| [example_projects/vivado_test_prj_template_v3/src/timing.xdc](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc) | Vivado 工程模板的 XDC 时序约束 | 精读 `create_clock` 与 `_SYNC_ATTR` 的 `set_false_path` |
| [scripts/get_fmax_vivado.tcl](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/get_fmax_vivado.tcl) | Vivado 的 Fmax 提取脚本 | 拆解把 slack 反算成 Fmax 的公式 |
| [scripts/post_flow_vivado.tcl](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_vivado.tcl) | Vivado 综合结束后的报告脚本 | 看它如何汇总 synth/impl 耗时 |
| [scripts/post_flow_quartus.tcl](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_quartus.tcl) | Quartus 综合结束后的报告脚本 | 看它如何归档 .sof、扫描告警、汇总编译耗时 |
| [cdc_data.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv) | 两级数据同步器（`delay` 的封装） | 看实例名 `data_SYNC_ATTR` 与头注释里的 false_path 模板 |
| [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) | 跨时钟域单拍脉冲同步器 | 看内部寄存器 `gc_FP_ATTR` 与 `_FP_ATTR` false_path 模板 |

> 名词提示：SDC（Synopsys Design Constraints）和 XDC（Xilinx Design Constraints）是两套语法几乎一样的时序约束文件格式，前者被 Quartus/TimeQuest 采用，后者被 Vivado 采用。同一个意图（声明时钟、豁免路径）在两边的写法只是命令细节不同，本讲会成对给出。

## 4. 核心概念与源码讲解

本讲按四个最小模块推进：先用 `create_clock` 把「时钟真相」告诉工具（4.1），再用 `set_false_path` 把「不该查的路径」摘出去（4.2），接着用 `_ATTR` 命名约定把成百上千个同步器收编进一条约束（4.3），最后用 Fmax 脚本把整轮综合的结果换算成一个频率数字（4.4）。

### 4.1 create_clock：告诉工具时钟在哪、多快

#### 4.1.1 概念说明

时序分析的本质是一道「时间预算」题：在每个时钟沿到来之前，数据必须**提前一点点**（建立时间 setup time）稳定在下一级触发器的输入端。工具要算「数据从源触发器出发、经过组合逻辑、到达目的触发器」用了多久，再和「时钟周期 − 建立时间」比，多出来的时间就叫**余量（slack）**。slack ≥ 0 表示这拍能赶上，slack < 0 表示来不及——时序违例。

要算这道题，工具必须先知道两件事：

1. **时钟周期是多少**——决定时间预算的上限；
2. **时钟从哪个端口进来、又经 PLL 分出哪些子时钟**——决定哪些触发器共享同一个时钟沿。

问题是：**工具自己无法从 RTL 里推断出时钟频率**。一个 `input clk` 在 RTL 里只是一个普通端口，它到底是 50 MHz 还是 500 MHz，代码里没写（`timescale` 只影响仿真，不影响综合后的时序分析）。所以必须由设计者用 `create_clock` 显式声明，否则工具要么报「无约束时钟」警告，要么按一个默认的、不准的频率分析，Fmax 也就无从谈起。

#### 4.1.2 核心流程

`create_clock` 的一条命令由三部分组成：

```text
create_clock -period <周期ns> -waveform { <上升沿ns> <下降沿ns> } [get_ports { <端口名> }]
                 │                    │                                   │
                 │                    └─ 描述占空比（默认 50%）              └─ 时钟进入芯片的物理端口
                 └─ 时钟周期（纳秒），频率 = 1/周期
```

时钟频率 \(f\) 与周期 \(T\) 的关系：

\[
f(\text{MHz}) = \frac{1000}{T(\text{ns})}
\]

例如 `Quartus` 模板写 `-period 2.000`，对应 \(1000/2.000 = 500\) MHz。

声明完板载晶振时钟后，还要处理 PLL。PLL（如 Quartus 的 `sys_pll`、Vivado 的 `clk_wiz`）把输入时钟倍频/分频出多路新时钟，这些**派生时钟**也必须被工具认识。Quartus 用一条 `derive_pll_clocks` 让工具自动从 PLL 配置里推出所有输出时钟；Vivado 则通常在例化 `clk_wiz` 时由 IP 自带的 `OOC` 约束自动生成，不必手写。

最后，真实时钟有抖动（jitter）和 PLL 引入的偏差，分析时要留一点余量。Quartus 用 `derive_clock_uncertainty` 自动把这些不确定量折算进去，让分析更保守、更接近真实硅片表现。

一个值得注意的设计技巧叫**过约束（over-constraint）**：故意把时钟周期写得比真实需要更短（即频率更高），逼迫布局布线器把电路做得尽可能快。仓库的 Quartus 模板就把 50 MHz 的板载时钟约束成 500 MHz，目的就是「榨干」器件速度，从而读到一个尽量高的 Fmax（见 4.4）。

#### 4.1.3 源码精读

先看 Quartus 模板 `main.sdc` 的全部内容——它极简，只有时钟声明：

- [main.sdc:L7-L10](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sdc#L7-L10) —— 对三个板载时钟端口 `FPGA_CLK1_50` / `FPGA_CLK2_50` / `FPGA_CLK3_50` 各声明一个 500 MHz（周期 2.000 ns、上升沿 0、下降沿 1.000 ns、50% 占空比）的时钟。注释明说这是「main reference clock, 500 MHz」。**注意这是过约束**：DE10-Nano 板上这几个端口实际是 50 MHz 晶振，故意写成 500 MHz 是为了把 fitter 逼到极限。顶层 `main.sv` 的 INFO 也写明这条意图：
- [main.sv:L14-L15](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L14-L15) —— 「SDC 约束文件把 clk 设成 500MHz，以迫使 fitter 综合出尽可能快的电路」。
- [main.sdc:L12](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sdc#L12) —— `derive_pll_clocks` 让 TimeQuest 自动推出 `sys_pll`（[main.sv:L77-L83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L77-L83) 例化的 PLL）输出的 `clk125` / `clk500` 两路时钟，无需手写。
- [main.sdc:L13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sdc#L13) —— `derive_clock_uncertainty` 自动注入时钟不确定度（抖动 + PLL 偏差），使分析带安全裕量。

再看 Vivado 模板 `timing.xdc` 对应的时钟声明，写法略有不同：

- [timing.xdc:L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L9) —— `create_clock -name clk -period 8.000 -waveform {0.000 4.000} [get_ports { clk }]`。`-name clk` 给这条时钟起名 `clk`（方便后续约束按名字引用）；周期 8.000 ns 即 **125 MHz**，对应 Arty 板真实的 125 MHz 晶振；`clk_wiz`（[main.sv:L108-L114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/main.sv#L108-L114)）的输出时钟由 IP 自带约束生成，所以这里不必再 derive。

> 文档滞后提示：Vivado 顶层 [main.sv:L14-L15](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/main.sv#L14-L15) 的 INFO 也写着「500MHz 过约束」，但实际的 `timing.xdc` 用的是 125 MHz（8 ns）。两套模板在这一点上并不一致——**以实际 xdc 代码为准**。这正是 u1-l1 提醒过的「文档可能滞后于代码」。

#### 4.1.4 代码实践

1. **实践目标**：体会「不声明时钟 → 工具瞎猜」的后果。
2. **操作步骤**：
   - 复制 Vivado 模板 `vivado_test_prj_template_v3`，在 `timing.xdc` 里把第 9 行的 `create_clock` 注释掉。
   - 跑一次综合（synthesis）+ 实现（implementation），打开时序报告（Timing Report）。
3. **需要观察的现象**：报告里会出现大量「Unconstrained paths / 没有定义时钟域」的告警，关键路径的 slack 无法给出有意义的值。
4. **预期结果**：恢复 `create_clock` 后告警消失，slack 重新被正常计算。**待本地验证**（具体告警文案随 Vivado 版本变化，但「无约束时钟」类警告一定会出现）。

#### 4.1.5 小练习与答案

**练习 1**：把 `create_clock -period 8.000` 改成 `-period 4.000`，频率变成多少？时序会更难还是更易通过？

**答案**：周期 4.000 ns → \(1000/4 = 250\) MHz，频率翻倍。时间预算减半，时序**更难**通过（slack 变小甚至变负）。这正是过约束的原理。

**练习 2**：为什么 `timescale 1ns/1ps` 不能替代 `create_clock`？

**答案**：`` `timescale `` 只对**仿真**生效（决定 `#8` 是 8 ns），综合后的时序分析器根本不看它。真实的时钟频率只能由 `create_clock` 告诉工具。

### 4.2 set_false_path：告诉工具哪些路径不用查

#### 4.2.1 概念说明

不是所有寄存器之间的路径都该被时序分析。最典型的一类就是**跨时钟域（CDC）路径**：数据从一个时钟域（`clkA`）的触发器出来，进入另一个时钟域（`clkB`）的同步器第一级触发器。因为 `clkA` 和 `clkB` 互不同步，数据到达同步器第一级的时刻相对 `clkB` 的沿是完全随机的——这正是亚稳态的根源，也是**两级同步器存在的意义**：它故意「允许」第一级偶尔进入亚稳态，再靠第二级把电平稳定下来。

如果把这条异步到达的路径也交给时序分析器按 `clkB` 频率去查，它几乎一定会判 slack < 0（因为数据到达时刻随机，经常「来不及」），于是你永远无法收敛——而这其实是个**伪违例**：这条路径本来就不该按时钟节拍准时送达，同步器的设计已经消化了这种不确定性。

`set_false_path` 的作用就是**把某条路径（或某个单元）排除出时序分析**，相当于告诉工具：「这条路是异步的，你别管它准不准时，同步器自己会处理。」

#### 4.2.2 核心流程

`set_false_path` 用 `-from` / `-to` 指定路径的起点和终点，二者都可以省略一个表示「从任意处来」或「到任意处去」。对同步器，最精确的做法是 `-to` 同步器的**第一级**触发器单元：

```text
set_false_path -to [get_cells -hier -filter { NAME =~ <匹配模式> }]
                      └─ 在整个网表层次里，按名字过滤出目标触发器单元(cell)
```

为什么是「第一级」而不是「第二级」？回顾 [delay.sv:L192-L204](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/delay.sv#L192-L204) 的寄存器链：`data[1]` 接收输入（第一级，亚稳态发生地），`data[2]` 是输出（第二级）。要豁免的是「异步数据进入 `data[1]`」的那一段；而 `data[1] → data[2]` 是同步器内部、同在 `clkB` 域的两拍，**应当**被正常分析（确保亚稳态有足够时间塌缩）。所以约束 `-to data[1]`，精确摘掉前一段异步路径。

> 工具命名规则：Vivado 把寄存器 `data[1]` 综合后命名为 `..._reg[1]`（自动加 `_reg` 后缀）；Quartus 用 `get_registers` 且路径形如 `...|data[1]`。这就是为什么同一意图在两边的匹配字符串长得不一样。

Quartus 里还能用另一类「时钟对时钟」的写法，直接豁免两个时钟域之间的所有路径：

```text
set_false_path -from [get_clocks <源时钟>] -to [get_clocks <目的时钟>]
```

它更省事，但粒度粗（整两个域之间一刀切）。仓库的 Vivado 模板把这种写法作为注释示例保留了下来（见 4.2.3）。

#### 4.2.3 源码精读

- [timing.xdc:L27-L30](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L27-L30) —— 给出的「时钟对时钟」CDC false_path **示例（被注释掉）**：在 `clk_wiz` 的两路输出 `clk_out1_clk_wiz_0`（即 `clk125`）与 `clk_out2_clk_wiz_0`（即 `clk500`）之间双向 `set_false_path`。如果你的逻辑真的在这两域之间传数据，取消注释即可。
- [timing.xdc:L35-L37](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L35-L37) —— 注释解释了核心约定：「所有名字带 `_SYNC_ATTR` 后缀的 `delay.sv` 实例都不再被当作延迟，而被当作同步器」，并指向 Xilinx 官方答案记录 AR#62136 解释 `get_cells -hier -filter` 的语法。
- [timing.xdc:L38](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L38) —— 第一条真正生效的 false_path：`set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]`。`-hier` 表示在整个设计层次里递归搜，`-filter {NAME =~ ...}` 用通配符匹配任何层次下、实例名以 `_SYNC_ATTR` 结尾、且是其中第一级寄存器 `data_reg[1]` 的单元。这一条就把工程里**所有**同步器的第一级全豁免了。
- [timing.xdc:L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L39) —— 第二条用 `*_SYNC_ATTR[*]/data_reg[1]*` 兼容**数组例化**的情形：当同步器被例化成数组（如 `cdc_data CD [31:0]`，见 [cdc_data.sv:L26](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L26)）时，实例名会带下标 `[_]`，这条模式专门匹配它。

对应的 RTL 侧——约束能「点中」单元，前提是实例名真的带了后缀：

- [cdc_data.sv:L43-L55](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L43-L55) —— `cdc_data` 内部把 `delay` 例化名为 `data_SYNC_ATTR`。正因为这个名字带 `_SYNC_ATTR`，上面那条 `*_SYNC_ATTR/data_reg[1]*` 才能匹配到它。
- [quartus main.sv:L119-L128](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L119-L128) 与 [vivado main.sv:L150-L159](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/main.sv#L150-L159) —— 两个模板里把 6 位按钮/开关异步输入做两级同步的实例名都叫 `sw_SYNC_ATTR`，同样会被这条约束一网打尽。

#### 4.2.4 代码实践

1. **实践目标**：直观看到「不豁免 → 时序红」与「豁免 → 时序绿」的差别。
2. **操作步骤**：
   - 在 Vivado 模板工程里，临时把 [timing.xdc:L38-L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L38-L39) 这两条 `set_false_path` 注释掉。
   - 重新综合 + 实现，打开 Timing Report 里的「违例路径（Setup Violations）」。
3. **需要观察的现象**：违例路径里会冒出终点指向 `sw_SYNC_ATTR/data_reg[1]` 的条目，slack 为负。
4. **预期结果**：恢复这两条 false_path 后，这些违例条目消失，设计才可能整体收敛。**待本地验证**（违例大小取决于器件与布局，但路径终点名字会出现）。

#### 4.2.5 小练习与答案

**练习 1**：为什么约束写成 `-to data_reg[1]`（第一级），而不是 `-to data_reg[2]`（第二级）？

**答案**：要豁免的是「异步数据进入同步器」那一段，终点正是第一级 `data[1]`。`data[1] → data[2]` 是同一时钟域内的两拍，必须保留分析，才能保证亚稳态有足够恢复时间。

**练习 2**：`set_false_path -from clkA -to clkB` 与 `set_false_path -to <同步器第一级单元>`，哪种更精确？

**答案**：后者更精确。前者把 `clkA→clkB` 整个域之间所有路径都豁免，万一你有真正需要分析的跨域路径也会被误伤；后者只豁免明确标注为同步器的单元。

### 4.3 _ATTR 命名约定：一条约束管住所有同步器

#### 4.3.1 概念说明

一个真实工程里可能有几十上百个同步器：每个异步按键、每个跨域握手、每个外部输入都要串两级。如果每个同步器都要单独写一条 `set_false_path`，约束文件会膨胀、且每加一个同步器就要记得补一条——极易漏。

仓库的解法是一个极其轻量的**命名约定**：凡是当同步器用的 `delay.sv` 实例，名字一律加 `_SYNC_ATTR` 后缀；凡是 `cdc_strobe` 那种靠 false_path 豁免的脉冲跨域寄存器，名字一律加 `_FP_ATTR` 后缀（FP = false path）。然后**一条**通配符约束就能匹配工程里所有带后缀的单元。新增同步器时，只要遵守命名约定，约束自动覆盖，零维护。

这本质上是把「这是同步器」这个**设计意图**编码进了**名字**里，让 RTL 和约束通过同一个字符串约定联动。

#### 4.3.2 核心流程

约定的运作链路如下：

```text
RTL 侧：把同步器实例命名为  xxx_SYNC_ATTR
                  │
                  ▼ 综合后，单元名形如  .../xxx_SYNC_ATTR/data_reg[1]
                  │
约束侧：set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]
                  │
                  ▼ 一次性豁免工程内全部同步器的第一级
```

仓库里有两套后缀，对应两种跨域电路：

| 后缀 | 用于 | 豁免对象 | 豁免哪一级 |
|------|------|----------|------------|
| `_SYNC_ATTR` | `delay`/`cdc_data` 做的**数据**同步器 | 第一级寄存器 `data_reg[1]` | 进入第一级（亚稳态发生地）的那段路径 |
| `_FP_ATTR` | `cdc_strobe` 做的**脉冲**跨域 | 整个带后缀的寄存器（格雷计数器 `gc_FP_ATTR`） | 源域到目的域的采样路径 |

两种后缀的 false_path 模板，仓库都直接写在了对应模块的头注释里，照抄即可。

#### 4.3.3 源码精读

`_SYNC_ATTR` 约定的「契约」写在 `cdc_data.sv` 头注释里：

- [cdc_data.sv:L12-L20](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L12-L20) —— 明说「别忘了给所有同步器写 false_path；最好的办法是给所有当同步器用的 `delay` 实例加 `_SYNC_ATTR` 后缀，然后只需一条约束」，并分别给出 Quartus（`get_registers {*delay:*_SYNC_ATTR*|data[1]*}`）和 Vivado（`get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}`）的模板。Vivado 版正是 [timing.xdc:L38](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L38) 落地的同一条。

`_FP_ATTR` 约定则写在 `cdc_strobe.sv` 头注释里，对应另一种跨域电路：

- [cdc_strobe.sv:L26-L33](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L26-L33) —— 「所有带 `_FP_ATTR` 后缀的节点都需要 false_path」，给出 Quartus（`get_registers {*_FP_ATTR*}`）与 Vivado（`get_cells -hier -filter {NAME =~ *_FP_ATTR*}`）模板。注意这里是 `-from`（从该节点出发），因为 `cdc_strobe` 是目的域直接采样源域的格雷计数器值，要把「从格雷计数器出发」的路径豁免。
- [cdc_strobe.sv:L83-L92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv#L83-L92) —— 真正带上后缀的寄存器：`logic [1:0] gc_FP_ATTR = '0;`，即那个「绝不能被复位」、跨域传递脉冲事件的 2 位格雷计数器。它的名字带 `_FP_ATTR`，所以会被上面的通配约束捕获。

> 小结：两个后缀、两套模板、一条约束各管一类。**新增同步器/脉冲跨域时，唯一要做的事就是给实例/寄存器起对名字。**

#### 4.3.4 代码实践

1. **实践目标**：亲手验证「命名约定 → 自动覆盖」。
2. **操作步骤**：
   - 在 Vivado 模板 `main.sv` 里再例化一个 `cdc_data`，实例名**故意**取 `my_async_input_SYNC_ATTR`（保留后缀）。
   - 综合后，在 Vivado Tcl 控制台跑 `report_property [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]`，列出所有被匹配到的单元。
3. **需要观察的现象**：列表里应同时包含原有的 `sw_SYNC_ATTR/data_reg[1]` 和你新加的 `my_async_input_SYNC_ATTR/data_reg[1]`。
4. **预期结果**：一条约束、两（及以上）个单元，全部命中——证明约定生效。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果你给新同步器起名 `my_async_input`（忘了加 `_SYNC_ATTR`），会发生什么？

**答案**：通配符 `*_SYNC_ATTR/...` 匹配不到它，它的第一级不会被豁免，时序分析仍会查这条异步路径，可能报伪违例。这正是命名约定「零维护」的前提——**必须守约**。

**练习 2**：`_SYNC_ATTR` 用 `-to`，`_FP_ATTR` 用 `-from`，为什么方向相反？

**答案**：`cdc_data` 要豁免的是「异步数据**进入**第一级」的路径，终点是第一级单元，故 `-to`；`cdc_strobe` 要豁免的是「格雷计数器**被**目的域采样」的路径，起点是计数器单元，故 `-from`。方向取决于哪一段路径是异步的。

### 4.4 Fmax 提取脚本：把时序报告变成一个数字

#### 4.4.1 概念说明

跑完一轮综合+实现，你最想知道的一个数字往往是：**这个设计最高能跑多快**——即 Fmax（最高时钟频率）。但时序报告通常给你的是「在当前约束频率下的 slack」，不是直接的 Fmax。

二者的换算关系很简单。设你约束的时钟周期是 \(T_{\text{target}}\)（ns），最差路径的 setup slack 是 \(S\)（ns，可正可负）。那么这条关键路径**实际**需要的周期是：

\[
T_{\text{path}} = T_{\text{target}} - S
\]

- slack \(S > 0\)（按时完成还有富余）→ \(T_{\text{path}} < T_{\text{target}}\)，路径其实能跑得比约束更快；
- slack \(S < 0\)（违例）→ \(T_{\text{path}} > T_{\text{target}}\)，路径需要更多时间。

于是真正的最高频率为：

\[
F_{\max}(\text{MHz}) = \frac{1000}{T_{\text{path}}} = \frac{1000}{T_{\text{target}} - S}
\]

这正是仓库 `get_fmax_vivado.tcl` 里那个看起来吓人的公式的全部含义。它的妙处在于：**你只需约束一个频率、读一个 slack，就能反算出关键路径真正的极限频率**，不必反复改约束试跑。

为了让 \(T_{\text{target}} - S\) 反映真实极限，常见做法是**过约束**——把目标频率设得比实际高（如 4.1 里 Quartus 的 500 MHz），让工具把路径压到极限，slack 自然为负，反算出的 Fmax 才是「榨干」后的真实能力。

#### 4.4.2 核心流程

`get_fmax_vivado.tcl` 的核心是一个 Tcl 过程（proc）：

```text
proc fmax {target_clock}        # target_clock = 你约束的时钟频率(MHz)
   open_run impl_1              # 打开已完成的实现结果（含时序信息）
   S = [get_property SLACK [get_timing_paths]]   # 取最差路径的 slack(ns)
   T_target = 1000 / target_clock                # 约束周期(ns)
   Fmax = round( 1000 / (T_target - S) )         # 反算最高频率(MHz)
   puts "$Fmax MHz"
```

其中 `get_timing_paths` 不带参数时返回**最差（最负 slack）的那一条** setup 路径，`get_property SLACK` 取它的 slack。

关键使用要点：**`target_clock` 必须等于你在 `create_clock` 里实际约束的频率**。例如你在 xdc 里写 `-period 8.000`（125 MHz），就要调用 `fmax 125`；若你过约束到 `-period 2.000`（500 MHz），就调用 `fmax 500`。脚本顶部那行 `fmax 1000`（[get_fmax_vivado.tcl:L8](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/get_fmax_vivado.tcl#L8)）是个**示例调用**，假设你把时钟过约束到了 1000 MHz。

> 注意：仓库**没有**提供对应的 `get_fmax_quartus.tcl`。Quartus 的 Fmax 通常在 `.sta.rpt`（时序分析报告）里直接以文字形式给出，配合下面的 `post_flow_quartus.tcl` 解析报告即可读出。

#### 4.4.3 源码精读

- [get_fmax_vivado.tcl:L8](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/get_fmax_vivado.tcl#L8) —— 顶层的示例调用 `fmax 1000`，表示「假设时钟约束在 1000 MHz，请反算 Fmax」。
- [get_fmax_vivado.tcl:L11-L17](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/get_fmax_vivado.tcl#L11-L17) —— 整个 `fmax` 过程定义。`open_run impl_1` 打开实现结果；`puts` 用 Tcl 的 `list` + `join` 拼字符串。
- [get_fmax_vivado.tcl:L14](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/get_fmax_vivado.tcl#L14) —— 核心公式 `round(1e3/((1e3/$target_clock)-[get_property SLACK [get_timing_paths]]))`。逐项对应：`1e3/$target_clock` = \(T_{\text{target}}\)（ns），减去 `SLACK` 得 \(T_{\text{path}}\)，`1e3/` 再换算回 MHz，`round` 取整。就是 4.4.1 的那条公式。

除了 Fmax，仓库还有两个「综合后自动报告」脚本，用来把流程收尾信息汇总打印：

- [post_flow_vivado.tcl:L25-L35](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_vivado.tcl#L25-L35) —— 从 `synth_1`、`impl_1` 两个 run 读取 `STATS.ELAPSED`（耗时），扫描成时分秒。
- [post_flow_vivado.tcl:L37-L46](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_vivado.tcl#L37-L46) —— 把秒/分进位归一化，最后 `puts` 出 `TOTAL: HH:MM:SS`，告诉你这轮综合一共花了多久。脚本头注释（[post_flow_vivado.tcl:L6-L11](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_vivado.tcl#L6-L11)）说明它要被设为「Generate Bitstream」步骤的 post.tcl 钩子。

Quartus 版功能更丰富，把版本归档、告警扫描、耗时汇总都塞进一个脚本：

- [post_flow_quartus.tcl:L25-L42](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_quartus.tcl#L25-L42) —— 读 `DEBUG/version.bin`，拆出高低字节，打印当前工程版本号，用于给 `.sof` 比特流文件打版本标记。
- [post_flow_quartus.tcl:L86-L92](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_quartus.tcl#L86-L92) —— 逐行扫 `map.rpt`，把含 `implicit`（隐式线网声明，通常是笔误信号）的行当错误 `post_message` 出来——一个自动的代码风格检查。
- [post_flow_quartus.tcl:L105-L169](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_quartus.tcl#L105-L169) —— 分别从 `map.rpt` / `fit.rpt` / `asm.rpt` / `sta.rpt`（新版 Quartus 时序分析）/ `tan.rpt`（老版）里捞 `Info: Elapsed time:` 行，注意 `sta.rpt` 正是 Quartus 存放 Fmax/时序结论的报告。
- [post_flow_quartus.tcl:L188-L197](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/post_flow_quartus.tcl#L188-L197) —— 同样把各阶段耗时进位归一化，`post_message` 出 `TOTAL: HH:MM:SS`。

#### 4.4.4 代码实践

1. **实践目标**：用 `fmax` 过程反算一个设计的真实 Fmax，并与「不约束 false_path 时」对比。
2. **操作步骤**：
   - 用 Vivado 模板跑完一轮完整实现（implementation），生成 `impl_1` 结果。
   - 在 Tcl 控制台 `cd` 到 `scripts/` 目录，`source get_fmax_vivado.tcl`，再按你 xdc 里的约束频率调用，例如 `fmax 125`（若你保持了 8 ns 约束）。
3. **需要观察的现象**：控制台打印一行 `<数字> MHz`，即反算的关键路径最高频率。
4. **预期结果**：数字因器件和设计而异，**待本地验证**。可做对照实验：先注释掉 `set_false_path` 跑一次、读 Fmax（会被同步器伪违例拖低），再恢复约束跑一次、读 Fmax（应回升），体会 false_path 对 Fmax 读数的影响。

> 安全提示：用 `source` 加载任意 Tcl 脚本前应先阅读其内容。本脚本只读 `impl_1` 的时序属性并 `puts` 打印，不修改工程，可安全运行。

#### 4.4.5 小练习与答案

**练习 1**：你把时钟约束在 200 MHz（周期 5 ns），工具报告最差 slack = −3 ns。Fmax 是多少？

**答案**：\(T_{\text{path}} = 5 - (-3) = 8\) ns，\(F_{\max} = 1000/8 = 125\) MHz。即这条关键路径实际只能撑到 125 MHz。

**练习 2**：为什么脚本里 `get_timing_paths` 不指定 `-n_paths` 也能工作？

**答案**：不指定数量时，Vivado 默认返回 1 条最差（slack 最小）的 setup 路径，正好就是我们要的关键路径，取它的 SLACK 代入公式即可。

## 5. 综合实践

把本讲四个模块串成一个完整闭环：**约束 → 综合 → 读 Fmax**。

**任务背景**：你要给 Vivado 模板工程加一个外部异步输入，用 `cdc_data` 同步进 `clk500` 域，然后正确约束它，并量化约束前后的 Fmax 差别。

**操作步骤**：

1. **加 RTL**：在 `vivado_test_prj_template_v3/src/main.sv` 里，新增一个 8 位输入端口 `in_async[7:0]`（在 `ck_io_low` 里挑几根线，并到对应 `.xdc` 引脚约束），用数组例化的 `cdc_data` 同步它：

   ```systemverilog
   // 示例代码：在 main.sv 内部新增
   logic [7:0] in_async_sync;
   cdc_data in_async_s [7:0] (
       .clk  ( {8{clk500}}      ),
       .nrst ( {8{1'b1}}         ),
       .d    ( in_async[7:0]     ),
       .q    ( in_async_sync[7:0])
   );
   ```

   > 注意：上面是**示例代码**，需按你实际的端口/约束补全；`cdc_data` 的例化模板见 [cdc_data.sv:L26-L32](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L26-L32)。因为是数组例化，内部 `delay` 实例名天然带 `_SYNC_ATTR` 后缀（[cdc_data.sv:L48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_data.sv#L48)），无需你额外改 RTL。

2. **验证约束覆盖**：[timing.xdc:L38-L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L38-L39) 的两条通配 false_path 已经能覆盖数组例化（第二条 `*_SYNC_ATTR[*]/...` 就是为此而设）。综合后在 Tcl 控制台执行：

   ```tcl
   get_cells -hier -filter {NAME =~ *_SYNC_ATTR[*]/data_reg[1]*}
   ```

   确认列表里出现 `in_async_s[*]/data_SYNC_ATTR/data_reg[1]*` 之类的新单元。

3. **对照测 Fmax**：
   - **情况 A（不约束）**：临时注释掉 [timing.xdc:L38-L39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L38-L39)，跑完整实现，`source scripts/get_fmax_vivado.tcl` 后调用 `fmax 125`，记下 Fmax_A。
   - **情况 B（正确约束）**：恢复约束，重跑，`fmax 125`，记下 Fmax_B。
4. **需要观察的现象**：情况 A 的时序报告里会出现以 `in_async_s.../data_reg[1]` 为终点的 setup 违例，Fmax_A 被这条伪违例拖低；情况 B 这些违例消失，Fmax_B ≥ Fmax_A。
5. **预期结果**：Fmax_B 不低于 Fmax_A，证明 `_SYNC_ATTR` 约定 + 一条 false_path 正确豁免了新增的 8 个同步器。具体数值**待本地验证**（取决于器件型号与布局种子）。

## 6. 本讲小结

- **`create_clock` 是时序分析的起点**：工具无法从 RTL 推断时钟频率，必须由你声明端口、周期与波形；Quartus 模板过约束到 500 MHz 以榨取极限，Vivado 模板用 125 MHz 的真实板载频率。
- **`derive_pll_clocks` / `derive_clock_uncertainty`** 让 Quartus 自动推出 PLL 派生时钟并注入抖动裕量，免去手写。
- **`set_false_path` 豁免不该被分析的路径**：跨时钟域同步器的输入路径是异步的，按时钟节拍查必报伪违例；精确豁免 `-to` 同步器第一级寄存器 `data_reg[1]`。
- **`_SYNC_ATTR` / `_FP_ATTR` 命名约定**把「这是同步器/脉冲跨域」的意图编码进名字，一条通配约束（如 `*_SYNC_ATTR/data_reg[1]*`）即可覆盖工程内全部同类实例，新增零维护。
- **Fmax = 1000 / (T_target − slack)**：`get_fmax_vivado.tcl` 用这条公式把一次 slack 读数反算成关键路径的最高频率；调用时传入的 `target_clock` 必须与 `create_clock` 的约束频率一致。
- **`post_flow_*.tcl` 是综合后报告钩子**：汇总编译耗时（Quartus 版还做版本归档、`implicit` 告警扫描、解析 `sta.rpt`），把流程收尾信息自动化。

## 7. 下一步学习建议

- **下一讲 u7-l3（多 IDE 基准）** 会把本讲的 Fmax 提取放进真实场景：同一份 RTL 在 Quartus / Vivado / Gowin / ISE 下分别综合，用 `benchmark_projects/` 对比各工具读到的 Fmax 差异，本讲的 `get_fmax_vivado.tcl` 正是其中的度量工具。
- **延伸阅读源码**：
  - 重读 [cdc_strobe.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/cdc_strobe.sv) 全文，结合本讲理解它的格雷计数器为何「绝不能复位」、以及为何用 `-from _FP_ATTR` 而非 `-to`。
  - 浏览 `scripts/` 下的 `clean_vivado.bat` / `compile_quartus.bat` 等，看一键编译流程如何把约束文件、源码、post_flow 脚本串成一条命令。
- **动手方向**：在 u7-l4 综合实战里，你将组装一个含按键去抖（异步输入同步）、FIFO、UART 的完整系统，届时本讲的 `_SYNC_ATTR` 约束与时钟声明将真正派上用场——这是把「会写约束」变成「会收敛一个系统」的最后一步。
