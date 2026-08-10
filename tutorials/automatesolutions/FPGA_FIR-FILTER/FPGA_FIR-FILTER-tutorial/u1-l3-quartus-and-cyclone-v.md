# Quartus 工程与目标平台 Cyclone V

## 1. 本讲目标

前两讲（u1-l1、u1-l2）我们已经看清了「项目做什么」和「仓库里有什么」：这是一个在 FPGA 上用 7 抽头 FIR 滤波器对 720p 视频做锐化的工程，源码分 `FPGA-Design`（硬件）、`Verification`（仿真）、`Octave`（算法）三块。但到目前为止，我们还只停留在「读文件」——没有回答一个关键问题：**这一堆 `.vhd` 文件，到底是怎么变成一块 FPGA 上真正跑起来的电路的？**

本讲就补上这一环。我们不进算法，只解决「工程怎么打开、怎么编译、跑到什么硬件上」。学完后你应当能够：

1. 看懂 Quartus 工程文件 `.qpf` / `.qsf` 各自记了什么，知道**器件型号、顶层实体、引脚分配、文件清单**分别在哪一行声明。
2. 说清目标平台——Cyclone V 器件 `5CEBA2F17C6`、EduPow 实验板、74.25 MHz / 720p 视频时钟——之间是什么关系，为什么这套设置是「定死」的。
3. 描述从 VHDL 源码（RTL）到生成 `.sof` 下载文件、再下载到远程实验板的整体流程，并能读懂 Fitter 报告里的资源占用数字。

这一讲是后面所有「改代码 → 重新编译 → 下载验证」动作的基础。

## 2. 前置知识

本讲基本不涉及 VHDL 语法，但会用上几个前面引入的概念，以及几个 FPGA 工具链的新名词：

- **顶层实体（top-level entity）**：设计的最外层模块，它的端口就是 FPGA 芯片物理上对外暴露的引脚。本项目的顶层实体叫 `sharp`（见 u1-l2）。
- **vs / hs / de**：视频时序同步信号。`de=1` 表示当前是有效像素。
- **FIR / 抽头 / 系数**：加权求和的滤波器，本项目系数 `[1, 0, -9, 48, -9, 0, 1] / 32`。
- **Quartus Prime**：Intel（原 Altera）FPGA 的官方综合 / 布局布线 / 下载工具。本讲的主角。
- **RTL（Register Transfer Level）**：寄存器传输级，指我们手写的 `.vhd` 这一层的抽象——描述数据在寄存器之间如何流动和运算，还没变成具体的门电路。
- **综合（Synthesis）**：把 RTL 翻译成门电路 / 查找表（LUT）+ 寄存器的网表。
- **Fitter（布局布线 / Place & Route）**：把综合出的网表「摆」到目标芯片的具体逻辑单元上，并连好线。
- **ALM / RAM Block / DSP Block**：Cyclone V 芯片里的硬件资源类型。ALM 是基本逻辑单元，RAM Block 是片上存储块，DSP Block 是专用乘加硬核。
- **时序约束（SDC）**：告诉工具「时钟有多快、信号何时到达」，工具据此判断电路能否在该时钟下正常工作。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为真实存在文件，仓库实际内容都在 `FPGA-FIR-Filter-master/` 目录下）：

| 文件 | 所属目录 | 在本讲中的作用 |
| --- | --- | --- |
| `FPGA-Design/FIR.qpf` | FPGA-Design | **工程文件**，记录工程名 `FIR` 与 Quartus 版本 |
| `FPGA-Design/FIR.qsf` | FPGA-Design | **设置与约束文件**，器件、顶层、引脚、IO 标准、文件清单全在这 |
| `FPGA-Design/sharp.sdc` | FPGA-Design | **时序约束文件**，定义 74.25 MHz 时钟与 IO 延迟 |
| `FPGA-Design/sharp.vhd` | FPGA-Design | 顶层实体 `sharp`，确认端口与顶层名（仅引用实体声明） |
| `output_files/FIR.fit.summary` | FPGA-Design | **Fitter 资源占用摘要**（仓库已提交的编译产物） |
| `output_files/FIR.sta.summary` | FPGA-Design | **时序分析摘要**（Setup/Hold Slack） |
| `output_files/FIR.done` | FPGA-Design | 编译完成的标志文件 |

> 说明：`output_files/` 下的报告是 Quartus 编译生成的产物（u1-l2 已说明它们可重建），但它们记录了「这份代码在这块芯片上实际占用多少资源」的真实数字，本讲会直接引用，作为你重新编译时的对照基准。下文永久链接保留完整 `FPGA-FIR-Filter-master/` 前缀。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先认识工程文件（4.1），再理解目标硬件（4.2），最后走通编译下载流程（4.3）。

### 4.1 Quartus 工程文件：.qpf 与 .qsf

#### 4.1.1 概念说明

一个 Quartus 工程并不是「一个文件」，而是**一组文件**，其中两个最关键：

- **`.qpf`（Quartus Project File）**：工程的「入口名片」，内容极少。它只记录工程名（revision）、Quartus 版本和创建时间。你双击它就能在 Quartus 里打开整个工程。
- **`.qsf`（Quartus Settings File）**：工程的「真正配置」，所有重要设置都在这里——用哪种芯片、顶层是哪个实体、每个信号接到芯片哪个引脚、IO 用什么电气标准、工程包含哪些源文件和约束文件。

可以这么理解：`.qpf` 告诉 Quartus「这是一个叫 FIR 的工程」，`.qsf` 才告诉 Quartus「这个工程具体怎么编译、跑到什么芯片上」。后面你要改器件、改引脚、加文件，本质上都是改 `.qsf`。

#### 4.1.2 核心流程

打开一个 Quartus 工程的典型流程：

1. 在 Quartus Prime 里 `File → Open Project`，选择 `FIR.qpf`。
2. Quartus 读 `.qpf` 拿到工程名与 revision，再自动加载同名的 `FIR.qsf`。
3. `.qsf` 里的 `TOP_LEVEL_ENTITY` 指明顶层是 `sharp`，`VHDL_FILE` 列出 5 个设计文件，`SDC_FILE` 列出时序约束。
4. 你点编译，Quartus 综合 `sharp` 及其例化的全部子模块，按 `.qsf` 的器件和引脚约束布局布线。

#### 4.1.3 源码精读

先看工程「名片」`FIR.qpf`：

[FIR.qpf:20-31](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qpf#L20-L31) —— 记录 Quartus Prime `23.1std.1` 版本、创建日期，以及最关键的一行 `PROJECT_REVISION = "FIR"`。Quartus 用这个 revision 名去寻找同名的 `FIR.qsf`。

再看工程的「真正配置」`FIR.qsf`，核心的三行全局设置：

[FIR.qsf:40-42](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L40-L42) —— 三条 `set_global_assignment` 一次性定下三件事：`FAMILY "Cyclone V"`（芯片家族）、`DEVICE 5CEBA2F17C6`（具体型号）、`TOP_LEVEL_ENTITY sharp`（顶层实体）。这三行决定了「跑到什么芯片、从哪个实体开始综合」。

工程包含哪些源文件，也由 `.qsf` 声明：

[FIR.qsf:125-130](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L125-L130) —— `VHDL_FILE` 列出 5 个设计文件（`sharp_control` / `sharp_slice` / `sharp_linemem` / `sharp_arith` / `sharp`），`SDC_FILE` 列出 `sharp.sdc`。注意 `sharp_slice` 在这里只写一次——它内部例化的 `linemem`/`arith` 通过 VHDL 的 `entity work.xxx` 在综合时自动找到，但仍需在此登记为工程文件。

输出目录与仿真工具的设置：

[FIR.qsf:46-50](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L46-L50) —— `PROJECT_OUTPUT_DIRECTORY output_files` 把所有编译产物（报告、`.sof`）集中到 `output_files/` 目录；`EDA_SIMULATION_TOOL` 选了 Questa，对应仿真流程（见第 5 单元）。

> 一句话总结 `.qsf` 的「分区」：`set_global_assignment`（全局，如器件/顶层/文件清单）+ `set_location_assignment`（引脚分配，见 4.2）+ `set_instance_assignment`（单个信号的属性，如 IO 标准）。4.2、4.3 会用到后两类。

#### 4.1.4 代码实践

**实践目标**：动手确认工程设置，建立「改设置 = 改 `.qsf`」的直觉。

**操作步骤**：

1. 用文本编辑器（不必开 Quartus）打开 `FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf`。
2. 定位到第 40–42 行，确认看到 `FAMILY "Cyclone V"`、`DEVICE 5CEBA2F17C6`、`TOP_LEVEL_ENTITY sharp`。
3. 定位到第 125–130 行，数一下 `VHDL_FILE` 的数量，并对照 u1-l2 的文件地图，确认这 5 个文件覆盖了顶层的全部例化。
4. （可选）如果你想看看「同一份设计跑到别的芯片上」是什么样，仓库还附带了 `sharp_default_Cyclone_10.qsf`、`sharp_default_Cyclone_IV.qsf`、`sharp_default_Cyclone_V.qsf` 三份模板约束，对比它们的 `DEVICE` 行，体会器件迁移时改的就是这一行。

**需要观察的现象**：`FIR.qsf` 与 `sharp_default_Cyclone_V.qsf` 的器件、顶层、引脚分配基本一致，区别在于 `FIR.qsf` 是 Quartus 实际编译时维护的「工作版」（含更多自动生成的全局设置），而 `sharp_default_*.qsf` 是作者提供的、面向不同板卡的模板。

**预期结果**：`FIR.qsf` 第 40–42 行的三项设置与下面 4.2 节的器件、顶层完全吻合；`VHDL_FILE` 恰好 5 行。

> 注：这一步只读文件，不需要安装 Quartus，任何编辑器都能做。如要真正打开 `.qpf`，需安装 Quartus Prime 23.1std.1（Lite 版免费），见 4.3。

#### 4.1.5 小练习与答案

**练习 1**：如果要把顶层实体从 `sharp` 改名为 `top`，至少要改 `.qsf` 的哪一行？VHDL 文件本身要不要改？

> **答案**：改 `.qsf` 第 42 行 `TOP_LEVEL_ENTITY` 为 `top`；同时 VHDL 里 `sharp.vhd` 的 `entity sharp is` 也要相应改名（否则顶层实体名对不上）。这正说明 `.qsf` 的顶层设置必须与源码实体名一致。

**练习 2**：`.qpf` 文件那么短，删掉它、只留 `.qsf`，工程还能打开吗？

> **答案**：不能直接双击打开。`.qpf` 是工程入口，Quartus 靠它定位 revision 名再去读 `.qsf`。不过工程内容（设置）确实全在 `.qsf` 里，所以新建一个同名 `.qpf` 或用 `File → New Project` 指向同一份 `.qsf` 即可恢复。

---

### 4.2 Cyclone V 器件与 EduPow 板

#### 4.2.1 概念说明

FPGA 不是一片通用芯片，而是「一片可编程芯片 + 一块承载它的电路板」两层：

- **器件（Device）`5CEBA2F17C6`**：这是 Intel **Cyclone V** 家族里的一颗具体 FPGA 芯片型号。编号里每个字段都有含义：`5C`=Cyclone V、`EBA`=普通 E 等级（含 ARM 硬核的 SoC 变体一般写 `SE`/`SX`）、`2`=规模挡位、`F17`=FBGA-256 封装、`C6`=最快速度等级。编译时工具必须知道**精确到这一串**的型号，才能把设计映射到这颗芯片真实的资源上。
- **EduPow 板**：承载这颗芯片的实验开发板，本项目的引脚分配就是按这块板的物理连线做的。`sharp.sdc` 开头的注释直接点明它是「EduPow-Board」。这块板属于 **FPGA Vision Remote Lab**（H-BRS，作者 Marco Winzker）的远程实验平台。

为什么器件型号和引脚分配「定死」？因为视频锐化要实时处理外部视频流的输入输出引脚（RGB、vs/hs/de、时钟），这些引脚在板上是固定连到视频接口芯片的——你必须把 `r_in[0..7]` 这类信号绑到板子上对应的物理引脚，否则综合出来的电路「够得着」芯片却「接不到」外部视频信号。

#### 4.2.2 核心流程

「引脚分配」在 Quartus 里的运作方式：

1. 顶层实体 `sharp` 声明了一组端口（`clk`、`r_in`、`vs_out` 等），每个端口对应芯片一个引脚。
2. `.qsf` 用 `set_location_assignment PIN_xxx -to <信号>` 把每个端口（含位向量的每一位）绑到具体引脚号。
3. `.qsf` 再用 `set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to <信号>` 声明每个引脚的电气标准（这里统一用 3.3V LVTTL）。
4. Fitter 阶段，Quartus 据此把信号落到对应物理引脚，并按 LVTTL 配置 IO 缓冲。

视频时钟部分：顶层注释写明 `clk` 是「input clock 74.25 MHz, video 720p」，这个频率正是 720p（1280×720@60）逐行视频的标准像素时钟。13.46 ns 周期与 74.25 MHz 的换算：

\[ f = \frac{1}{T} = \frac{1}{13.46\,\text{ns}} \approx 74.29\,\text{MHz} \approx 74.25\,\text{MHz} \]

（`sharp.sdc` 里取 13.46 ns 是 74.25 MHz 的工程化近似；该时钟约束本身的细节留到 u6-l2 详讲。）

#### 4.2.3 源码精读

顶层实体声明的端口，就是「芯片要对外提供哪些引脚」：

[sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33) —— `entity sharp` 的全部端口：`clk`（第 13 行注释「input clock 74.25 MHz, video 720p」）、`reset_n`、`enable_in`、视频入 `vs_in/hs_in/de_in/r_in/g_in/b_in`、视频出 `vs_out/...`、`clk_o`、`led`。这些名字会逐个出现在 `.qsf` 的引脚分配里。

引脚分配的关键片段（`clk` 与部分输入）：

[FIR.qsf:101-105](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L101-L105) —— `clk` 绑到 `PIN_P9`、`de_in` 绑到 `PIN_N11`、`enable_in[2..0]` 分别绑到 `PIN_J16/H15/G16`。整张引脚分配表从第 62 行一直到第 124 行，覆盖了顶层全部端口（含 RGB 各 8 位、时序、时钟、使能、LED）。

IO 电气标准的统一设置（节选）：

[FIR.qsf:188-192](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L188-L192) —— `clk`、`clk_o`、`reset_n`、`led[0..2]` 等均设为 `3.3-V LVTTL`。实际上第 131–194 行对几乎每个端口都重复了这条 `IO_STANDARD` 设置。

时序约束文件 `sharp.sdc` 的开头注释直接点明目标板与时钟：

[sharp.sdc:1-11](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L1-L11) —— 第 3 行注释「Timing constraints for EduPow-Board with 74.25 MHz 720p-signal」；第 10 行 `create_clock -name input_clk -period 13.46ns [get_ports {clk}]` 就是那个 74.25 MHz 视频时钟。SDC 细节在 u6-l2 精讲，这里只需知道它「锁定了这块板、这个时钟」。

> 引脚分配的完整解读（每个 PIN 号对应板上哪个外设）属于 u6-l1 的内容，本讲只需建立「这些设置是为了把设计落到 EduPow 板的具体引脚上」的整体认识。

#### 4.2.4 代码实践

**实践目标**：验证「顶层端口 ↔ 物理引脚」的对应关系，理解为什么器件和引脚是绑定的。

**操作步骤**：

1. 打开 `FIR.qsf`，找到 `set_location_assignment PIN_P9 -to clk`（第 101 行）。
2. 在 `sharp.vhd` 第 13 行确认 `clk` 确实是 74.25 MHz 输入时钟。
3. 数一下 RGB 输入相关引脚：`r_in`、`g_in`、`b_in` 各 8 位 = 24 个引脚（见 `FIR.qsf` 第 93–122 行）。
4. 对照 `FIR.fit.summary` 里的 `Total pins : 63 / 128 ( 49 % )`（见 4.3.3），体会这颗 `5CEBA2F17C6` 一共 128 个可用 IO，本设计用了 63 个，其中光是 RGB+时序就占了绝大多数。

**需要观察的现象**：顶层声明的每一个端口，在 `.qsf` 里都有一条对应的 `set_location_assignment`；如果某个端口漏了引脚分配，Fitter 会报「未分配引脚」警告。

**预期结果**：`clk=PIN_P9`、`reset_n=PIN_T2`、`vs_in=PIN_L10`、`de_in=PIN_N11`，且全部端口（除 `clk_n_o` 这一约束行外）都有引脚和 IO 标准设置。

> 注：本步只读文件即可完成，无需安装工具。

#### 4.2.5 小练习与答案

**练习 1**：器件型号 `5CEBA2F17C6` 里的 `F17` 代表什么？它和「芯片有多少引脚」有什么关系？

> **答案**：`F17` 指 **FBGA-256 封装**，是该器件的物理封装代号。封装决定了芯片对外可用的 IO 引脚上限（`fit.summary` 显示本器件 128 个可用 IO），所以「封装」直接影响「能接多少视频信号」。

**练习 2**：如果把 `.qsf` 第 41 行的 `DEVICE` 改成另一颗 Cyclone V，但引脚分配（`PIN_P9 -to clk` 等）不动，会怎样？

> **答案**：很可能报错。不同器件（哪怕同家族）的封装 / 引脚布局不同，`PIN_P9` 在新器件上未必存在或未必对应同样功能。器件迁移时引脚分配通常要整套重做——这正是仓库提供 `sharp_default_Cyclone_10.qsf` 等多套模板的原因。

---

### 4.3 编译与下载流程

#### 4.3.1 概念说明

从 VHDL 到芯片跑起来，要经过 Quartus 的**标准编译流水线**，每一阶段产出一个报告文件：

| 阶段 | Quartus 名称 | 产出报告 | 作用 |
| --- | --- | --- | --- |
| 1 | Analysis & Synthesis（综合） | `FIR.map.rpt` / `.map.summary` | 把 RTL 翻译成门 / LUT + 寄存器网表 |
| 2 | Fitter（布局布线） | `FIR.fit.rpt` / `.fit.summary` | 把网表摆到 `5CEBA2F17C6` 的具体资源上、连线 |
| 3 | Assembler（汇编） | `FIR.asm.rpt` / `FIR.sof` | 生成可下载的编程文件 `.sof` |
| 4 | TimeQuest / Timing Analyzer（STA） | `FIR.sta.rpt` / `.sta.summary` | 按 `sharp.sdc` 检查时序是否满足 |

编译全部通过后，会在 `output_files/` 得到 `FIR.sof`（SRAM 编程文件）。把 `.sof` 下载（Program）到芯片，电路就开始工作；掉电后 `.sof` 内容丢失，所以也叫「配置 SRAM」。

下载到 EduPow 板的方式有两种：本地用 USB-Blaster 下载器直连，或通过 **FPGA Vision Remote Lab** 的远程接口操作这块板（README 给出了项目页 `h-brs.de/fpga-vision-lab`）。本项目作为开放教育资源，主推远程实验。

#### 4.3.2 核心流程

完整流程伪代码：

```
打开 FIR.qpf
  → 确认设置（器件=5CEBA2F17C6、顶层=sharp、引脚/SDC 已配）
Processing → Start Compilation
  ├─ Synthesis  → FIR.map.summary   （检查综合是否成功、寄存器数）
  ├─ Fitter     → FIR.fit.summary   （检查资源占用、引脚分配是否落地）
  ├─ Assembler  → FIR.sof           （生成下载文件）
  └─ Timing     → FIR.sta.summary   （检查 Slack 是否为正，即满足时序）
全部 Successful
  → 用 Programmer 把 FIR.sof 下载到 EduPow 板（本地 USB-Blaster 或远程 Lab）
  → 板上视频接口送入 720p 信号，观察输出是否锐化
```

关键判断点：**Fitter 必须显示 Successful，且 STA 的 Setup Slack 必须为正**，否则即使生成 `.sof`，电路也可能在该时钟下工作不稳定。

#### 4.3.3 源码精读

仓库里 `output_files/` 已经提交了一份完整编译结果，直接拿来当对照基准。

Fitter 摘要——确认器件、顶层与资源占用：

[FIR.fit.summary:1-14](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/output_files/FIR.fit.summary#L1-L14) —— 第 1 行 `Fitter Status : Successful`；第 4–6 行确认 `Top-level Entity Name : sharp`、`Family : Cyclone V`、`Device : 5CEBA2F17C6`，与 `.qsf` 设置完全一致；第 8–14 行是资源占用：

| 资源 | 占用 / 总量 | 比例 |
| --- | --- | --- |
| 逻辑利用率（ALM） | 345 / 9,430 | 4 % |
| 寄存器总数 | 334 | — |
| 引脚 | 63 / 128 | 49 % |
| 片上存储位（block memory bits） | 294,912 / 1,802,240 | 16 % |
| RAM Block | 29 / 176 | 16 % |
| DSP Block | 0 / 25 | 0 % |

> 注意 `block memory bits` 占了 16%——这正是因为 `sharp_slice` 例化了 6 个 `sharp_linemem`（每个 1280 项行存储）来支撑垂直滤波抽头（详见 u4-l1）。而 `DSP Block = 0`，说明定点乘加是用普通逻辑（LUT）实现的，没有动用专用乘法硬核。

时序分析摘要——确认电路在 74.25 MHz 下能满足建立时间：

[FIR.sta.summary:5-7](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/output_files/FIR.sta.summary#L5-L7) —— 在最严苛的 `Slow 1100mV 85C` 模型下，`input_clk` 的 Setup Slack = `0.658` ns（正值），TNS=0。Slack 为正意味着数据能在时钟沿之前稳定到达，时序收敛。

编译完成的标志文件：

[FIR.done](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/output_files/FIR.done) —— Quartus 编译跑完会写这个只有一行时间戳的空标志文件，表示「编译结束」。配合 `FIR.sof`（2.4 MB 的编程文件，也在 `output_files/`）即可下载。

#### 4.3.4 代码实践

**实践目标**：在 Quartus Prime 中打开工程、确认设置、执行一次完整编译，并核对 Fitter 报告的资源占用是否与仓库提交的数字一致。

**操作步骤**：

1. 安装 Quartus Prime **23.1std.1 Lite**（免费版，与 `.qpf` 记录的版本一致；版本差别太大可能需要转换工程）。
2. `File → Open Project`，选择 `FPGA-FIR-Filter-master/FPGA-Design/FIR.qpf`。
3. 在左侧 Project Navigator / 顶部确认：Family = Cyclone V、Device = `5CEBA2F17C6`、顶层实体 = `sharp`（对应 `.qsf` 第 40–42 行）。
4. `Processing → Start Compilation`（或工具栏的 ▶ 按钮），等待四阶段全绿。
5. 编译完成后，`output_files/FIR.fit.summary` 会重新生成；打开它核对资源占用。

**需要观察的现象**：四阶段（Synthesis / Fitter / Assembler / Timing）状态都应为 `Successful`；`Fitter` 阶段会打印「器件 5CEBA2F17C6、顶层 sharp、占用 ALM 约 4%、block memory 约 16%」等。

**预期结果**（对照仓库已提交的 `FIR.fit.summary`）：逻辑利用率约 345/9430 ALM（4%）、片上存储约 294,912 位（16%）、引脚 63/128（49%）、DSP 0/25。若你用的是同一版本 Quartus，数字应几乎一致；不同版本间可能因综合策略略有差异。

**如果无法本地验证**：明确标注「待本地验证」。即便不安装 Quartus，你也可以直接阅读仓库里的 `output_files/FIR.fit.summary`、`FIR.map.summary`、`FIR.sta.summary`，把它们当成「一次成功编译的样张」来学习上述指标的含义。

> 下载到板子的部分：本地需 USB-Blaster 下载器与 EduPow 板；本项目更推荐通过 FPGA Vision Remote Lab 远程操作，具体接入方式见项目页，本讲不展开。

#### 4.3.5 小练习与答案

**练习 1**：`FIR.fit.summary` 里 `DSP Block : 0 / 25 (0 %)`，但 `block memory bits` 占了 16%。这说明了本设计资源使用的什么特点？

> **答案**：本设计**重存储、轻乘法**。垂直滤波要缓存多行像素（行存储 RAM 吃掉 16% 存储位），而 7 抽头定点乘加的系数简单（`[1,0,-9,48,-9,0,1]/32`，可用移位+加减近似），所以没用专用 DSP 硬核，全靠 LUT 逻辑实现。

**练习 2**：编译成功后，`output_files/` 里哪个文件是真正用来下载到芯片的？它和「综合报告」是同一类东西吗？

> **答案**：`FIR.sof`（SRAM Object File）是下载用的二进制编程文件。它与 `.map.rpt` / `.fit.rpt` 这类文本「报告」不同：报告是给人看的资源 / 时序统计，`.sof` 是给芯片的配置比特流。掉电后 `.sof` 内容会丢，需重新下载。

**练习 3**：如果某次编译 `FIR.sta.summary` 里 Setup Slack 变成负数（比如 -0.3），电路还能下载吗？能用吗？

> **答案**：仍能下载（Assembler 照样生成 `.sof`），但**时序未收敛**，电路在 74.25 MHz 下可能工作不可靠（像素错乱、随机失效）。Slack 为负是危险信号，必须优化（改流水线、降频等）后再交付——这正是 u6-l2 调时钟周期实践要演示的现象。

---

## 5. 综合实践

把本讲三块内容串起来，完成一个「读懂工程设置 → 解释编译结果」的小任务。

**任务**：假设你要向同事介绍这份工程「跑到什么硬件上、占多少资源」，请基于本讲引用的真实文件，填写下面这张表（先自己填，再对照给出的参考答案）：

| 项目 | 你的答案 | 来源（文件:行） |
| --- | --- | --- |
| 工程名 / revision | ？ | `FIR.qpf` |
| 顶层实体 | ？ | `FIR.qsf` / `sharp.vhd` |
| 芯片家族 / 型号 | ？ | `FIR.qsf` |
| 视频时钟频率 / 周期 | ？ | `sharp.vhd` 注释 / `sharp.sdc` |
| 目标开发板 | ？ | `sharp.sdc` 注释 |
| 逻辑利用率 | ？ | `FIR.fit.summary` |
| 片上存储占用 | ？ | `FIR.fit.summary` |
| 时序是否收敛（Slack） | ？ | `FIR.sta.summary` |

**参考答案**：

| 项目 | 答案 |
| --- | --- |
| 工程名 / revision | `FIR`（`FIR.qpf` 第 31 行 `PROJECT_REVISION = "FIR"`） |
| 顶层实体 | `sharp`（`FIR.qsf` 第 42 行；`sharp.vhd` 第 12 行 `entity sharp`） |
| 芯片家族 / 型号 | Cyclone V / `5CEBA2F17C6`（`FIR.qsf` 第 40–41 行） |
| 视频时钟频率 / 周期 | 74.25 MHz / 13.46 ns（`sharp.vhd` 第 13 行注释；`sharp.sdc` 第 10 行） |
| 目标开发板 | EduPow-Board（`sharp.sdc` 第 3 行注释），属 FPGA Vision Remote Lab |
| 逻辑利用率 | 345 / 9,430 ALM ≈ 4 %（`FIR.fit.summary` 第 8 行） |
| 片上存储占用 | 294,912 / 1,802,240 位 ≈ 16 %（`FIR.fit.summary` 第 12 行） |
| 时序收敛 | 收敛，最差 Setup Slack = +0.658 ns（`FIR.sta.summary` 第 6–7 行） |

完成这张表后，你就具备了「看懂一个 Quartus 工程基本盘」的能力，这也是后续修改设计、重新编译前必须先确认的检查清单。

## 6. 本讲小结

- Quartus 工程由 `.qpf`（入口名片，含 revision `FIR`）和 `.qsf`（真正配置）组成，器件、顶层、引脚、IO 标准、文件清单都写在 `.qsf` 里——**改设置就是改 `.qsf`**。
- 目标平台是 Cyclone V 器件 `5CEBA2F17C6`（FBGA-256、128 个 IO）+ EduPow 实验板，运行在 **74.25 MHz / 720p** 视频时钟下（13.46 ns 周期）；引脚分配按这块板的物理连线「定死」。
- 顶层实体 `sharp` 的每个端口（RGB × 3、vs/hs/de、clk、enable、led）都在 `.qsf` 里有对应的 `set_location_assignment` 引脚号和 `3.3-V LVTTL` IO 标准。
- 编译走 Synthesis → Fitter → Assembler → Timing 四阶段，产物集中在 `output_files/`；本设计约用 4% 逻辑、16% 片上存储、0 个 DSP，时序收敛（Setup Slack +0.658 ns）。
- 下载用 `FIR.sof`，可经 USB-Blaster 本地下载，或通过 FPGA Vision Remote Lab 远程操作 EduPow 板。
- 即便不装 Quartus，也能直接读仓库已提交的 `output_files/*.summary` 报告，把「一次成功编译」当样张来学。

## 7. 下一步学习建议

到这里，你已经能打开工程、看懂设置和编译结果。后续建议：

1. **进入算法层**：下一单元（u2）先用 Octave 讲清图像锐化与可分离 FIR 的数学原理和系数设计——这是读懂硬件运算逻辑的前提。
2. **进阶到顶层架构**：U3 单元会逐段精读 `sharp.vhd`、`sharp_control.vhd`、`sharp_slice.vhd`，把数据通路串起来。
3. **想深入约束**：本讲只点到 `sharp.sdc` 的 74.25 MHz 时钟。若你对引脚分配细节、IO 标准和时序约束（`create_clock` / `set_input_delay` / Slack）感兴趣，可直接跳到 **u6-l1（引脚约束 QSF）** 和 **u6-l2（时序约束 SDC）**。
4. **想动手仿真**：若暂时没有 FPGA 硬件，可先跳到 U5 单元，用 Questa/ModelSim 跑 testbench 验证设计，不需要真实板子。
