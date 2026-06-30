# 建一个真实 FPGA 工程：example_projects 模板

## 1. 本讲目标

前三讲我们学会了「认识项目」「读懂单个模块」「用仿真器跑起来」。这些都是**离线**的事——在电脑里就能完成。本讲要迈出关键一步：把可综合代码变成一块能**烧进真实 FPGA 芯片、在板子上跑**的工程。

学完本讲，你应该能够：

- 理解 `example_projects/` 下的工程模板是如何把「顶层模块 + 时钟 IP + 约束 + 脚本」组织成一个可上板工程的。
- 看懂 Quartus 的 `.qsf`、Vivado 的 `.xdc`（物理约束）是如何把代码里的一个端口（如 `clk`、`LED[7]`）**绑定到芯片上某个具体的引脚**的。
- 看懂 `.sdc`（Quartus）/ `timing.xdc`（Vivado）里的**时序约束**是如何告诉工具「时钟有多快、哪些路径不必检查时序」的。
- 掌握「输入寄存化 / 输出寄存化」这条改善时序的工程惯例，并理解模板作者为什么这样做。
- 能动手改一行测试逻辑，并完成一次综合（Synthesis）。

> 本讲关注的不是「某条算法」，而是**工程骨架**。这套骨架你会在 basic_verilog 的几乎所有上板项目里反复看到，是后续单元的「上板脚手架」。

## 2. 前置知识

在进入源码前，先用大白话把几个 FPGA 工程必备概念讲清楚。

- **综合（Synthesis）与布局布线（Fitter / Place & Route）**：仿真器只是按你写的代码「解释执行」一遍，看波形对不对；而要让代码在真实芯片里跑，工具必须把你的 RTL **翻译成芯片里的逻辑单元（LUT/FF/BRAM 等）**，再决定每个单元**摆在芯片哪个位置、用什么金属线连起来**。前者叫综合，后者叫布局布线。仿真过的代码不等于能上板，必须再过一遍综合。

- **引脚约束（Pin Assignment / 物理约束）**：你写 `input clk;`，代码并不知道 `clk` 对应芯片哪根脚。板子上的晶振时钟、按键、LED 都焊死在芯片的特定引脚上。必须用一份约束文件告诉工具「`clk` = `PIN_H16`」「`led[0]` = `PIN_R14`」。Quartus 里这些写在 `.qsf`，Vivado 里写在 `.xdc`（常叫 physical/constraints）。

- **时序约束（Timing Constraints）**：综合后，工具需要知道「时钟频率是多少」，才能判断每条数据路径是否「在一个时钟周期内来得及」。如果不确定时钟频率，工具就无法报出有意义的最高频率（Fmax）。时序约束还用来声明**哪些路径不用检查**（例如跨时钟域的同步器，本来就不该被时序分析），这些写在 Quartus 的 `.sdc` 或 Vivado 的 `timing.xdc`。

- **时钟 IP（PLL / MMCM）**：FPGA 板上的晶振通常只给一两个固定频率（常见 50 MHz）。我们要用芯片内部专门的硬件资源（Altera 叫 PLL，Xilinx 叫 MMCM/PLL，在 Vivado 里包装成 `clk_wiz`）把它倍频/分频成项目需要的多个频率（如 125 MHz、500 MHz）。这是用厂商 GUI 生成的「黑盒 IP」，在代码里当成一个模块例化即可。

- **寄存化（Registering）**：把组合逻辑的结果用一级触发器（`always_ff`）「锁」一拍。输入端先打一拍叫**输入寄存化**，输出端再打一拍叫**输出寄存化**。这样做能让数据从触发器直接出发、直接落到触发器，路径最短、时序最好——这正是本讲模板的核心思想。

> 名词速查：`.qpf`（Quartus 工程文件）、`.qsf`（Quartus 设置文件，含引脚/IP/源文件）、`.sdc`（Quartus 时序约束）、`.xpr`（Vivado 工程文件）、`.xdc`（Vivado 约束，物理与时序都写这里）、`.sof`（Quartus 烧录文件）。

## 3. 本讲源码地图

本讲围绕两个并行的工程模板，它们做的是**同一件事**，只是分别面向 Intel/Altera（Quartus）和 Xilinx（Vivado）两套工具链：

| 文件 | 工具链 | 作用 |
| --- | --- | --- |
| `example_projects/quartus_test_prj_template_v4/src/main.sv` | Quartus | DE10-Nano 板的顶层模块，演示「寄存化 + 测试逻辑」骨架 |
| `example_projects/quartus_test_prj_template_v4/src/main.sdc` | Quartus | 时序约束：声明 500 MHz 参考时钟、自动推导 PLL 时钟 |
| `example_projects/quartus_test_prj_template_v4/test.qsf` | Quartus | 工程设置：引脚位置、电平标准、源文件、IP、虚拟引脚 |
| `example_projects/vivado_test_prj_template_v3/src/main.sv` | Vivado | Arty-7020 板的顶层模块，结构与 Quartus 版一一对应 |
| `example_projects/vivado_test_prj_template_v3/src/physical.xdc` | Vivado | 物理约束：`PACKAGE_PIN` 把端口绑到引脚 |
| `example_projects/vivado_test_prj_template_v3/src/timing.xdc` | Vivado | 时序约束：`create_clock` 与 `set_false_path` |

辅助文件：`ip/sys_pll/sys_pll.v`（Quartus 生成的 PLL 黑盒）、`src/define.svh`（跨工程复用的宏定义）、`Makefile`（命令行编译入口）。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**顶层模块**、**PLL/时钟 IP 例化**、**引脚约束**、**时序约束**。每个模块都以「同一套骨架，两种工具链」的方式对照讲解。

### 4.1 顶层模块：main.sv 的「三明治」结构

#### 4.1.1 概念说明

`main.sv` 是整个 FPGA 工程的**顶层模块（top module）**。综合工具从它开始，自顶向下找到所有被它例化的子模块（`clk_divider`、`delay`、`edge_detect`、PLL 等），把它们一起烧进芯片。顶层模块的特殊之处在于：它的端口（`input`/`output`）就是芯片的**物理引脚**，必须和约束文件里的引脚一一对应。

basic_verilog 的顶层模板遵循一个统一的「三明治」结构，作者在文件头注释里写得非常明白——这是读懂整份 `main.sv` 的总纲：

- 最外层是**输入寄存化**：外部进来的数据先打一拍，得到 `in_data_reg`。
- 中间是**你的测试逻辑**（注释写着 `place your test logic here`）：把寄存化后的输入做运算（模板里是异或），得到 `out_data_comb`。
- 最外层是**输出寄存化**：再把组合结果打一拍送到输出 `out_data`。

这样任何用户逻辑（哪怕是纯组合逻辑）都被两片触发器「夹」住，对外呈现的永远是寄存器输出，时序最干净。

#### 4.1.2 核心流程

模板的数据流可以画成下面这条链：

```text
外部引脚 in_data ──►【输入寄存化 always_ff】──► in_data_reg
                                                        │
                                          【测试逻辑 always_comb】
                                                        ▼
                                              out_data_comb (= 输入 ^ 分频计数)
                                                        │
外部引脚 out_data ◄──【输出寄存化 always_ff】◄──────────┘
```

关键点：

1. 输入寄存化和输出寄存化都在**同一个时钟**（本模板用 `clk500`，即 PLL 输出的 500 MHz）的 `posedge` 采样。
2. 测试逻辑用的是 `always_comb`（组合逻辑），它夹在两级寄存器之间。
3. 复位 `~nrst` 同时控制两级寄存器，复位时输出清零。

#### 4.1.3 源码精读

先看 Quartus 版顶层模块的端口声明，这是整个 `main.sv` 的「门面」：

[example_projects/quartus_test_prj_template_v4/src/main.sv:25-66](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L25-L66) —— 声明顶层模块端口。注意 `FPGA_CLK1_50`（板载 50 MHz 晶振）、`KEY`/`SW`（按键/拨码）、`LED`、`GPIO_*` 这些都对应 DE10-Nano 板上真实的引脚；而最后的 `in_data`/`in_datb`/`out_data` 是**虚拟引脚（virtual pins）**，留作逻辑调试用（4.3 节会讲为什么）。

文件头注释把作者的设计意图写得非常清楚，建议先读这段再读代码：

[example_projects/quartus_test_prj_template_v4/src/main.sv:7-16](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L7-L16) —— INFO 说明：明确写了「输入输出寄存化，以便即便你的组合逻辑/IP 是组合输出也能得到有效的时序报告」，这是整个模板的设计哲学。

接着是输入寄存化（第一片「面包」）：

[example_projects/quartus_test_prj_template_v4/src/main.sv:147-154](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L147-L154) —— 在 `posedge clk500` 把 `in_data` 打一拍成 `in_data_reg`；`~nrst` 时清零。

中间是测试逻辑（「夹心」）：

[example_projects/quartus_test_prj_template_v4/src/main.sv:158-161](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L158-L161) —— 组合运算 `out_data_comb = in_data_reg ^ div_clk500`。

> 小提醒：作者在这里的 `always_comb` 里用了非阻塞赋值 `<=`。组合逻辑更地道的写法其实是阻塞 `=`，但综合后两者在这里都生成纯组合电路，不影响功能。读源码时知道这一点即可，不必纠结——本讲的实践任务就改的是这一行。

最后是输出寄存化（第二片「面包」）：

[example_projects/quartus_test_prj_template_v4/src/main.sv:164-170](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L164-L170) —— 在 `posedge clk500` 把 `out_data_comb` 打一拍送到顶层输出 `out_data`。

Vivado 版的顶层模块结构**完全一致**，只是端口名换成 Arty 板的命名，并且测试逻辑同样是一句异或：

[example_projects/vivado_test_prj_template_v3/src/main.sv:177-203](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/main.sv#L177-L203) —— Vivado 版的输入寄存化、组合测试逻辑（`in_data_reg ^ div_clk500`）、输出寄存化，与 Quartus 版逐行对应。两套模板这样设计是为了让同一份用户逻辑能「几乎不改」地在两种芯片间移植。

#### 4.1.4 代码实践

**实践目标**：亲手修改测试逻辑，跑通一次综合。

**操作步骤**：

1. 打开 `example_projects/quartus_test_prj_template_v4/src/main.sv`，定位到第 160 行。
2. 把异或 `^` 改成加法 `+`：

   ```verilog
   out_data_comb[`WIDTH-1:0] <= in_data_reg[`WIDTH-1:0] + div_clk500[31:0];
   ```

3. 在该目录用 Quartus 重新综合（命令行可用 `make`，见本目录的 `Makefile`；GUI 里则点 Processing → Start Compilation）。

**需要观察的现象**：

- 综合能顺利通过，**没有语法错误**（这是本实践的核心验收点）。
- 由于 `out_data_comb` 是 32 位、两个加数也都是 32 位，加法会自然丢弃最高位进位（回绕），无需额外处理位宽。
- 综合报告里 `out_data` 路径的资源/时序和原先的「异或版」略有不同：加法器比异或门更深一层，Fmax 可能略降——这正是 4.4 节时序约束要关注的事。

**预期结果**：综合成功，生成 `.sof`。若你手边没有 Quartus，至少可以用 `iverilog -g2012` 做一次语法/语义检查（连同 `clk_divider.sv`、`delay.sv`、`edge_detect.sv` 一起编译），确认改动不引入语法错误。

> 待本地验证：Fmax 与资源占用数字需要你自己的工具版本/器件才能给出，本讲不臆造具体数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者要把输入和输出都寄存化，而不是直接 `assign out_data = in_data ^ div_clk500;`？

**参考答案**：直接 `assign` 会让 `out_data` 成为组合输出，路径起点是外部引脚（无时钟）、终点也是外部引脚，工具很难约束、时序很差。寄存化后，数据路径变成「触发器 → 组合 → 触发器」，起点终点都有时钟，工具能精确分析，Fmax 更高、更稳定。

**练习 2**：模板里输入寄存化和输出寄存化用的是 `clk500`，而按键去抖用的是 `clk125`，为什么不统一？

**参考答案**：不同功能跑在不同时钟域。高速数据通路用 500 MHz 追求性能；按键这类慢速、异步输入用 125 MHz 采样足够，且更省功耗。跨域处用同步器（`delay` 模块，见 u2-l3）衔接。这正是后续 CDC 单元（u3）要深入的主题。

---

### 4.2 PLL / 时钟 IP 例化

#### 4.2.1 概念说明

板载晶振通常只给固定频率（DE10-Nano 是 50 MHz，Arty 是 125 MHz 单端时钟）。但项目需要多种频率：高速数据想用 500 MHz，慢速采样想用 125 MHz。FPGA 内部有专门的模拟硬核（PLL / MMCM）可以倍频分频，但它们不能靠 RTL 描述——必须用厂商 GUI 生成一个「IP 核」，得到一个**黑盒模块**，再像普通模块一样例化。

- Quartus 版的 IP 叫 `sys_pll`（Altera PLL，通过 megafunction 生成）。
- Vivado 版的 IP 叫 `clk_wiz_0`（Xilinx Clocking Wizard）。

两者都是工具生成的文件（`ip/sys_pll/sys_pll.v`、`.../ip/clk_wiz_0.xci`），**不该手改**，只需读懂它的端口，按端口接线即可。

#### 4.2.2 核心流程

时钟 IP 的使用流程：

1. 在厂商 GUI 里配置 IP：输入参考频率、要输出的频率数、占空比、复位极性等。
2. 工具生成黑盒模块（带 `.qip`/`.xci` 描述文件，告诉工程怎么编译它）。
3. 在 `main.sv` 里例化它：把板载晶振接到 `refclk`/`clk_in1`，从 `outclk_0`/`clk_out1`、`outclk_1`/`clk_out2` 取出多路输出时钟，`locked` 信号告诉你 PLL 是否锁定稳定。

PLL 输出与输入的频率关系由 IP 配置决定，本质上：

\[ f_{\text{out}} = f_{\text{ref}} \cdot \frac{M}{N \cdot O} \]

其中 \(M\)（倍频）、\(N\)（分频预分）、\(O\)（输出分频）都在 GUI 里配好，对使用者而言只要知道「`outclk_0`=125 MHz、`outclk_1`=500 MHz」即可。

#### 4.2.3 源码精读

先看 Quartus 版例化 PLL 的写法：

[example_projects/quartus_test_prj_template_v4/src/main.sv:71-83](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L71-L83) —— 声明 `clk125`/`clk500`/`sys_pll_locked` 三根线，例化 `sys_pll`：参考时钟 `FPGA_CLK1_50`（板载 50 MHz）接 `refclk`，`rst` 恒为 `1'b0`（不复位），`outclk_0`→`clk125`、`outclk_1`→`clk500`，`locked`→`sys_pll_locked`。

黑盒模块的端口长什么样？看工具生成的 `sys_pll.v`：

[example_projects/quartus_test_prj_template_v4/ip/sys_pll/sys_pll.v:6-14](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/ip/sys_pll/sys_pll.v#L6-L14) —— `sys_pll` 模块声明：端口就是 `refclk`、`rst`、`outclk_0`、`outclk_1`、`locked`，与上面例化完全对得上。文件头明确写着「THIS IS A WIZARD-GENERATED FILE. DO NOT EDIT」——这种文件只读、不改。

Vivado 版用的是 Xilinx 的 Clocking Wizard，名字和端口都不一样：

[example_projects/vivado_test_prj_template_v3/src/main.sv:108-114](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/main.sv#L108-L114) —— 例化 `clk_wiz_0`：`clk_in1` 接板载 `clk`（125 MHz），`resetn` 接 `1'b1`（高有效复位，此处不复位），`clk_out1`→`clk125`、`clk_out2`→`clk500`，`locked`→`sys_pll_locked`。

> 对照记忆：Quartus 的 `refclk/rst/outclk_0/outclk_1` ↔ Vivado 的 `clk_in1/resetn/clk_out1/clk_out2`。同一件事，两套命名。例化 PLL 时**必须严格按各自工具生成的端口名接线**，否则综合报「port not found」。

拿到 500 MHz 后，模板还接了一个 `clk_divider`，把高速时钟再分频出一组慢速位（`div_clk500[31:0]`），其中某一位拿来当测试数据/闪 LED：

[example_projects/quartus_test_prj_template_v4/src/main.sv:95-105](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L95-L105) —— 在 `clk500` 域例化 `clk_divider` 得到 `div_clk500`，并把 `div_clk125[25]` 接到 `LED[7]` 让 LED 闪烁（`clk_divider` 的 `out[N] = clk/2^(N+1)`，详见 u2-l1）。

#### 4.2.4 代码实践

**实践目标**：通过阅读 + 对照，建立「IP 黑盒 = 按端口接线」的直觉。

**操作步骤**：

1. 打开 `example_projects/quartus_test_prj_template_v4/ip/sys_pll/sys_pll.v`，找到 `module sys_pll(...)`，把它的端口列表抄在纸上。
2. 回到 `main.sv` 第 77–83 行，逐端口核对你例化时连的线是否与黑盒端口一一对应（名字、方向、位宽）。
3. 同样对照 Vivado 版：`clk_wiz_0` 的端口在 `test.srcs/sources_1/ip/clk_wiz_0/` 下由 `.xci` 描述，你可以打开 Vivado 工程用 IP Sources 视图查看其例化模板。

**需要观察的现象**：你能列出两套 IP 各自的「参考时钟端口名、复位端口名（含极性）、输出时钟端口名、locked 端口名」。

**预期结果**：得到一张对照表（如上文 4.2.3 给出的对照）。这是后续跨工程移植代码时最常查的东西。

#### 4.2.5 小练习与答案

**练习 1**：`sys_pll_locked`（`locked`）信号被声明为 `// asyn` 的异步信号，模板里却几乎没用它，这样安全吗？

**参考答案**：严谨做法是在 PLL `locked` 拉高之后再释放系统复位，避免锁定前的毛刺时钟污染电路。模板为了简洁没有做这层保护，直接用按键产生 `nrst`。在产品级工程里应把 `locked` 纳入复位逻辑。

**练习 2**：为什么不能直接写一段 RTL 来「倍频」50 MHz 到 500 MHz？

**参考答案**：纯数字 RTL 无法创造比输入更高频的时钟——倍频依赖模拟电路（PLL 的压控振荡器 VCO 和相位检测器），这是 FPGA 里专门的硬核资源，只能通过 IP 调用，不能用 RTL 描述。

---

### 4.3 引脚约束（物理约束）

#### 4.3.1 概念说明

顶层模块声明了端口，但代码不知道每个端口对应芯片哪根物理引脚，也不知道这根引脚该用什么**电平标准**（LVCMOS33、3.3-V LVTTL、TMDS_33……）。这两件事由**物理约束文件**完成：

- Quartus 写在 `.qsf`：用 `set_location_assignment PIN_xxx -to 端口名` 定位引脚，用 `set_instance_assignment -name IO_STANDARD "..." -to 端口名` 设电平。
- Vivado 写在 `physical.xdc`：用 `set_property PACKAGE_PIN xxx ...` 定位，`IOSTANDARD` 设电平，二者合并进一个 `set_property -dict {...}`。

> 4.1.1 里提到的「虚拟引脚」是个特例：像 `in_data`/`out_data` 这种只在调试/综合时用、不真正接外部硬件的宽位端口，如果不约束，工具会报错「没地方放」。用 `VIRTUAL_PIN` 约束告诉工具「这根线不要分配物理脚，留着综合用即可」。

#### 4.3.2 核心流程

引脚约束的工作流：

1. 查板子原理图，找到晶振/按键/LED 对应的芯片引脚号与电平。
2. 把对应关系写进约束文件（`.qsf` 或 `.xdc`）。
3. 综合布局布线时，工具读取约束，把每个端口绑到指定引脚。

```text
代码: input clk;   ──┐
                       ├──► 约束: clk = PIN_H16, LVCMOS33 ──► 工具把 clk 布到芯片 H16 脚
板子: 晶振焊在 H16 ──┘
```

#### 4.3.3 源码精读

先看 Vivado 版的物理约束，语法最紧凑：

[example_projects/vivado_test_prj_template_v3/src/physical.xdc:9-13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/physical.xdc#L9-L13) —— 把 `clk` 绑到 `H16`（Arty 板载 125 MHz 晶振脚）、`sw[0]`/`sw[1]` 绑到拨码开关脚，全部用 `LVCMOS33` 电平。`set_property -dict { PACKAGE_PIN <脚> IOSTANDARD <电平> } [get_ports { <端口> }]` 是 Vivado 的固定句式。

LED 引脚同理：

[example_projects/vivado_test_prj_template_v3/src/physical.xdc:24-27](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/physical.xdc#L24-L27) —— `led[0..3]` 绑到 `R14/P14/N16/M14`。

再看 Quartus 版。`.qsf` 把「位置」和「电平」拆成两条语句，先设电平：

[example_projects/quartus_test_prj_template_v4/test.qsf:46-48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/test.qsf#L46-L48) —— 给 `FPGA_CLK1_50`/`FPGA_CLK2_50`/`FPGA_CLK3_50` 三个板载时钟设电平 `3.3-V LVTTL`。

再设位置（举例几个）：

[example_projects/quartus_test_prj_template_v4/test.qsf:304-307](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/test.qsf#L304-L307) —— `set_location_assignment PIN_xxx -to 端口` 把 ADC 相关端口绑到 DE10-Nano 的对应脚。

最后看「虚拟引脚」这条很关键的约束，专门给那几个 32 位调试端口：

[example_projects/quartus_test_prj_template_v4/test.qsf:451-453](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/test.qsf#L451-L453) —— `set_instance_assignment -name VIRTUAL_PIN ON -to in_data[*]`（以及 `in_datb`、`out_data`）。声明它们不占物理引脚，从而让这个「带调试端口」的模板能顺利综合（否则 32 位宽的 `in_data` 无处安放）。

#### 4.3.4 代码实践

**实践目标**：建立「端口名 ↔ 物理引脚号」的查表能力。

**操作步骤**：

1. 打开 `example_projects/vivado_test_prj_template_v3/src/physical.xdc`，找到 `clk`、`led[0]`、`btn[0]` 各自的 `PACKAGE_PIN`。
2. 在 `main.sv` 端口表里确认这几个名字与约束里的 `get_ports` 完全一致（大小写、位宽下标都要对得上）。
3. 想象你要把 `out_data` 从「虚拟引脚」改成「接到一排真实 LED」：在 `physical.xdc` 里你会怎么写？（先不真的改，只写出 4 条 `set_property` 语句。）

**需要观察的现象**：约束里的端口名拼写必须与 `main.sv` 端口表一字不差；如果 `main.sv` 里写 `output [3:0] led`，约束里 `led[0..3]` 四条缺一不可。

**预期结果**：你能口头描述「`clk`→`H16`、`led[0]`→`R14`、`btn[0]`→`D19`」，并能解释为什么改了端口名就必须同步改约束，否则综合会报「unconstrained / not found」。

> 待本地验证：自己手写的那 4 条 `set_property` 是否语法正确，可粘贴进 Vivado 工程的 Constraints 里验证。

#### 4.3.5 小练习与答案

**练习 1**：如果你删掉 `physical.xdc` 里关于 `clk` 的那行，综合还会成功吗？

**参考答案**：综合本身可能通过（因为 `clk` 只是个端口），但布局布线会报 `clk` 没有 IO 位置约束，无法生成可烧录的 bitstream。这就是为什么物理约束是「上板必需」。

**练习 2**：`in_data[*]` 为什么用 `VIRTUAL_PIN` 而不是给它也分配一个真实引脚？

**参考答案**：`in_data` 是 32 位调试端口，目的是配合 SignalTap/VIO 在片内观察数据，并不真的连到板子外部。真实板子也没有 32 根空闲脚。`VIRTUAL_PIN` 让它综合时存在、但不必落到物理 IO，是 Quartus 处理这类「逻辑端口」的标准手法。

---

### 4.4 时序约束（.sdc / timing.xdc）

#### 4.4.1 概念说明

物理约束解决「端口放哪儿」，**时序约束**解决「电路能不能跑得多快、哪些路径要不要检查」。时序约束的核心是两条：

1. **声明时钟**（`create_clock`）：告诉工具参考时钟的周期。工具据此计算每条寄存器到寄存器路径的 Slack（余量），进而给出最高频率 Fmax。
2. **豁免路径**（`set_false_path` 等）：跨时钟域的同步器路径本来就不该按同步时序检查（数据可能一拍两拍后才稳定，是设计上允许的），必须显式排除，否则工具会误报时序违例。

这两个模板用了一个共同的小技巧：**故意把参考时钟约束成 500 MHz（周期 2 ns）**，逼布局布线器尽最大努力跑得快，从而测出这个器件上能实现的「最快可能电路」。这条意图作者写在 `main.sv` 头注释里。

#### 4.4.2 核心流程

时序约束与综合的配合：

```text
create_clock 500 MHz ──► 工具按 2 ns 周期分析所有同步路径
                         │
derive_pll_clocks ──────►│ 自动把 PLL 输出时钟（125M/500M）也纳入分析
                         │
set_false_path _SYNC_ATTR ► 把名为 *_SYNC_ATTR 的同步器第 2 级寄存器排除分析
                         ▼
                   得到 Fmax / Slack 报告
```

时钟周期与频率的换算：

\[ T = \frac{1}{f}, \quad f = 500\,\text{MHz} \Rightarrow T = 2\,\text{ns} \]

所以 `.sdc` 里 `-period 2.000` 就代表 500 MHz。

#### 4.4.3 源码精读

先看 Quartus 的 `.sdc`，非常短，但每一行都有用：

[example_projects/quartus_test_prj_template_v4/src/main.sdc:7-13](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sdc#L7-L13) —— 对 `FPGA_CLK1_50`/`FPGA_CLK2_50`/`FPGA_CLK3_50` 三个板载时钟各 `create_clock -period 2.000`（即 500 MHz，注意这是**故意放大**的真实约束，见上文），再用 `derive_pll_clocks` 自动推导 PLL 输出时钟、`derive_clock_uncertainty` 计入时钟不确定性。

> 注意：端口名虽叫 `FPGA_CLK1_50`（暗示 50 MHz），但 SDC 里把它约束成 500 MHz。这是「施压测试」写法，不是笔误。读源码时要把「端口名」和「约束频率」分开理解。

再看 Vivado 的 `timing.xdc`：

[example_projects/vivado_test_prj_template_v3/src/timing.xdc:8-10](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L8-L10) —— `create_clock -name clk -period 8.000`（125 MHz，对应 Arty 的真实晶振）作用在端口 `clk` 上。

最有教学价值的是 Vivado 的 false_path，它和 `main.sv` 里那个奇怪的例化名 `sw_SYNC_ATTR` 直接相关：

[example_projects/vivado_test_prj_template_v3/src/timing.xdc:35-39](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/vivado_test_prj_template_v3/src/timing.xdc#L35-L39) —— 注释解释：所有例化名以 `_SYNC_ATTR` 结尾的 `delay.sv` 实例，不应被当作普通延迟，而是**同步器**；用 `set_false_path -to [get_cells -hier -filter {NAME =~ *_SYNC_ATTR/data_reg[1]*}]` 把它们的第 2 级寄存器从时序分析中排除。

回到 `main.sv` 你会发现按键同步用的正是这个命名：

[example_projects/quartus_test_prj_template_v4/src/main.sv:119-128](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/example_projects/quartus_test_prj_template_v4/src/main.sv#L119-L128) —— 例化 `delay` 做按键/拨码的 2 级同步，实例名故意叫 `sw_SYNC_ATTR`。这个后缀就是给 4.4.3 上面的 false_path 通配符匹配用的——**命名约定 + 约束通配**是 basic_verilog 处理「多个同步器统一豁免」的优雅手法（详见 u3-l1）。

#### 4.4.4 代码实践

**实践目标**：理解「时序约束如何影响 Fmax」。

**操作步骤**：

1. 打开 `main.sdc`，把第 8 行的 `-period 2.000` 临时改成 `-period 20.000`（即 50 MHz，回到真实频率）。
2. 重新跑一次时序分析（Quartus 里 `make` 或 Start Compilation；命令行也可用仓库 `scripts/` 下的 Quartus 后处理脚本读取 Fmax）。
3. 把改之前和改之后两份时序报告的 Fmax 记录下来对比。

**需要观察的现象**：

- 约束成 500 MHz 时，工具为满足 2 ns 周期会尽力优化，Fmax 报告逼近器件极限；约束成 50 MHz 时，工具只需满足 20 ns，会「摆烂」，布局更松。
- 这正解释了模板为什么故意约束成高频：**用「不可能完成的目标」榨出最大性能**，从而知道这块板子最快能跑多少。

**预期结果**：你能解释「同一个 RTL、同一块板子，仅改 SDC 周期，Fmax 报告会不同」——因为 Fmax 本质是「在满足当前约束的前提下，电路能稳定运行的最高频率」。

> 待本地验证：具体 Fmax 数值取决于你的 Quartus 版本和器件型号，本讲不给具体数字。仓库的 `benchmark_projects/`（见 u7-l3）专门做了这类跨工具对比。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `timing.xdc` 里那两条 `set_false_path`，会发生什么？

**参考答案**：`sw_SYNC_ATTR` 同步器的第 2 级寄存器会被纳入同步时序分析。由于输入来自异步的外部按键，第一级寄存器可能进入亚稳态，第 2 级的建立时间在某些情况下满足不了，工具会报时序违例（negative slack）。这条 false_path 正是为了告诉工具「这里允许违例，是设计意图」。

**练习 2**：`derive_pll_clocks`（Quartus）为什么需要？我们在 SDC 里不是已经 `create_clock` 了吗？

**参考答案**：`create_clock` 只声明了**输入参考时钟**。PLL 内部生成的 `clk125`/`clk500` 是派生时钟，工具默认不知道它们的频率/相位关系。`derive_pll_clocks` 让工具读取 PLL IP 的配置，自动给所有输出时钟建立约束，这样从 125 MHz 域到 500 MHz 域的跨域路径才能被正确分析。

---

## 5. 综合实践

把本讲四个模块串成一个完整动作：**改一行逻辑 → 理解它的时钟域 → 确认它的引脚/时序 → 跑综合**。

任务：在 `quartus_test_prj_template_v4` 里完成下面这条链。

1. **改逻辑**（对应 4.1）：把 `main.sv` 第 160 行的异或改为加法 `in_data_reg + div_clk500`。
2. **认时钟**（对应 4.2）：指出这行加法所在的时钟域是 `clk500`（由 `sys_pll` 的 `outclk_1` 提供），而参与运算的 `div_clk500` 来自 `clk_divider`，也在 `clk500` 域——所以这是**同源同域**运算，无需同步器。
3. **查引脚**（对应 4.3）：由于 `out_data` 是 `VIRTUAL_PIN`，加法结果不会真的输出到外部引脚，而是留在片内供 SignalTap 观察。确认 `.qsf` 里 `out_data[*]` 仍是虚拟引脚，无需改动。
4. **看时序**（对应 4.4）：重新综合后，打开 TimeQuest（Quartus）/ Timing Summary（Vivado），定位到 `out_data` 寄存器的路径，观察 Slack。加法比异或的组合深度更大，Slack 应比改之前更紧张——这正好印证「输出寄存化」给时序留出了评估空间。
5. 记录三件事：① 是否综合成功（核心验收）；② `out_data` 路径的 Slack 变化方向；③ 你能读出的 Fmax（若工具给出）。

> 这条链覆盖了「顶层模块 + PLL + 引脚约束 + 时序约束」全部四个最小模块，是后续每个上板实验的标准流程。如果没有 Quartus/Vivado 环境，第 1、2、3 步纯靠读源码即可完成；第 4、5 步标注「待本地验证」。

## 6. 本讲小结

- `example_projects/` 提供了 Quartus（DE10-Nano）和 Vivado（Arty-7020）两套**结构完全对应**的工程模板，是上板的「脚手架」。
- 顶层 `main.sv` 用「**输入寄存化 → 测试逻辑 → 输出寄存化**」的三明治结构，保证任何组合用户逻辑对外都是干净的寄存器输出，时序最稳。
- 板载晶振频率固定，靠 **PLL/Clocking Wizard IP** 倍频出多路时钟（`clk125`/`clk500`）；IP 是工具生成的黑盒，按端口例化即可，不可手改。
- **物理约束**（`.qsf` 的 `set_location_assignment` / `.xdc` 的 `PACKAGE_PIN`）把端口绑到芯片引脚并设电平；调试用的宽位端口用 `VIRTUAL_PIN` 不占物理脚。
- **时序约束**（`.sdc`/`timing.xdc`）用 `create_clock` 声明时钟（模板故意约束成 500 MHz 施压求 Fmax），用 `set_false_path` 配合 `_SYNC_ATTR` 命名约定豁免同步器路径。
- 改一行测试逻辑（异或→加法）并综合成功，是验证你掌握这套骨架的最小闭环。

## 7. 下一步学习建议

本讲建立的是「上板工程骨架」。接下来建议：

- 进入 **u2 基础组合与时序原语**：从 `clk_divider`（u2-l1）开始，真正理解本讲反复出现的 `out[N] = clk/2^(N+1)` 是怎么来的；再看 `edge_detect`（u2-l2）和 `delay`（u2-l3），搞懂本讲 `sw_SYNC_ATTR` 这个同步器实例的内部原理。
- 暂时没有上板环境的读者，可以用 **u1-l3** 的 iverilog/ModelSim 流程，把本讲的 `main.sv`（连同 `clk_divider`/`delay`/`edge_detect`）当成仿真对象，先在波形里验证三明治结构里各级寄存器的时序关系。
- 对时序约束意犹未尽的读者，可直接跳到 **u7-l2（时序约束与收敛）** 和 **u7-l3（多 IDE 基准）**，那里系统讲解 `get_fmax` 脚本与跨工具 Fmax 对比方法论。
