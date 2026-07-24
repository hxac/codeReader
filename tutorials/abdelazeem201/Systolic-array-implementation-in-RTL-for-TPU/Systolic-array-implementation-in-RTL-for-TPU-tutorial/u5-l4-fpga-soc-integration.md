# FPGA 综合与 SoC 顶层集成

## 1. 本讲目标

学完本讲，你应当能够：

- 看懂 `rtl/RTL_modified/tpu.v` 与 `tpu_system.v` 这类 **FPGA 板级顶层**的端口结构：时钟、外设（七段码 / 按键 / 开关 / LED）与 HPS DDR3 接口分别是什么。
- 理解 **Qsys（Platform Designer）系统例化** 的意义：为什么顶层只例化一个 `tpus_system` / `mysystem`，而 TPU 计算核要藏在里面。
- 说清楚 **HPS（Hard Processor System，硬核处理器）DDR3 接口信号集** 的作用，以及它如何成为 CPU 与 FPGA 之间交换矩阵数据的桥梁。
- 把 FPGA SoC 集成路径与 u5-l1~u5-l3 的 **ASIC 后端流程**（DC 综合 → ICC 布局布线 → GDSII）对照，理解两种实现路线在「时钟、复位、总线、存储」四类信号上的根本差异。
- 解释 `assign HEX0 = ~hex3_hex0[6:0];` 中取反 `~` 的物理原因。

> 承接：本讲是 u5 单元的收口。u5-l1~u5-l3 讲的是「把 RTL 做成一颗可流片的 ASIC 芯片」，本讲切换视角，看「同一套 TPU 思路如何被搬上一块自带 ARM 核的 FPGA 开发板，作为一个 SoC 子系统跑起来」。

---

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

### 2.1 什么是 FPGA SoC

普通 FPGA 是一片「可编程逻辑」：你用 Verilog 描述电路，工具把它映射成查找表（LUT）、触发器（FF）、DSP 块和片上存储（BRAM/M10K），再生成比特流烧进去。

而 **FPGA SoC**（如 Intel Cyclone V SoC、Xilinx Zynq）在硅片上同时固化了一颗 ARM CPU 硬核和一片 FPGA 逻辑。Intel 把这颗 ARM 硬核称为 **HPS（Hard Processor System）**。于是：

- CPU 侧可以跑 Linux、跑 C 程序、做文件 I/O；
- FPGA 侧可以放你的 TPU 加速器；
- 两者通过片上总线和共享的 DDR3 内存通信。

这正是本项目把 TPU「用起来」的形态：TPU 不再是孤立的仿真模块，而是 SoC 里一个可以被 CPU 调用的加速外设。

### 2.2 什么是 Qsys / Platform Designer

当 SoC 里要同时接 HPS、DDR3 控制器、一堆外设（七段码、按键、LED）和你的自定义 IP（TPU）时，手工写顶层互连非常繁琐。Intel 的 **Qsys**（新名叫 Platform Designer）是一个图形化的系统集成工具：你在里面拖拽组件、连好 Avalon 总线，它会自动生成一个 Verilog 顶层模块（本项目的 `mysystem` / `tpus_system`），把所有组件及其连接打包好。

所以本讲的 `tpu.v` / `tpu_system.v` 几乎不含逻辑，**只做一件事：把板子上的物理引脚，对接到 Qsys 生成的系统模块端口上**。

### 2.3 Avalon-MM 总线速览

HPS（ARM 核）要驱动 FPGA 里的 TPU，靠的是 **Avalon Memory-Mapped（Avalon-MM）总线**——一种「像访问内存一样访问外设」的协议。CPU 向某个地址写数据，就等于给 TPU 下命令或喂矩阵元素；CPU 从某个地址读数据，就等于取回计算结果。本讲只涉及它在 Qsys 里的端口命名约定（如 `.clk_clk`、`.memory_mem_a`），更细的命令解码留到 u6-l4 讲 `busConn.v`。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [rtl/RTL_modified/tpu.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v) | FPGA 板级顶层（模块名 `tpu`），例化 Qsys 系统 `tpus_system` | 引脚声明、七段码取反、系统例化 |
| [rtl/RTL_modified/tpu_system.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu_system.v) | 与 `tpu.v` 几乎相同的另一份顶层（模块名 `lab1`），例化 `mysystem` | 两份文件的差异与命名混乱 |
| [README.md](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md) | 项目说明，含「Vivado FPGA 综合」一节 | 综合工具声明与引脚风格的不一致 |
| rtl/RTL_modified/busConn.v（参考） | Avalon-MM slave 封装，内部例化 `top` TPU 核 | 帮助理解 TPU 如何进入 SoC（详见 u6-l4） |
| rtl/RTL_modified/top.v（参考） | 扩展 TPU 计算核（master_control + sysArr + accumTable…） | TPU 在 SoC 里的「计算实体」（详见 u6 单元） |

> 说明：本讲聚焦「板级顶层」这三块最小模块——**FPGA 引脚声明**、**HPS DDR3 接口**、**Qsys 系统例化**。TPU 计算核本身的细节属于 u6 单元，这里只在需要时点到为止。

---

## 4. 核心概念与源码讲解

### 4.1 FPGA 顶层引脚声明：时钟与外设

#### 4.1.1 概念说明

任何 FPGA 顶层模块的第一职责，是**把 Verilog 里的逻辑信号绑定到芯片的物理引脚（pin）上**。开发板厂商会为每块板子规定一套引脚名称与电平标准，工具（Quartus/Vivado）再通过约束文件（`.qsf` / `.xdc`）把这些名字映射到具体的 bank 和 pin 号。

本项目的引脚命名——`CLOCK_50`、`HEX0`~`HEX5`、`KEY[3:0]`、`LEDR[9:0]`、`SW[9:0]`、`HPS_DDR3_*`——是 **Intel Cyclone V SoC 开发板（Terasic DE1-SoC 系列）**的标准引脚约定。6 个七段码、10 个红色 LED、10 个拨码开关、4 个按键，外加完整 HPS DDR3 接口，与 DE1-SoC 板完全吻合（具体板型待本地确认，但引脚集合足以判定为该系列）。

> ⚠️ 与 README 的矛盾：[README.md:17-20](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L17-L20) 声称「用 Vivado 2021 综合成功」，但 `HEX/KEY/LEDR/SW/HPS` 这套命名是 Intel/Altera 风格，对应工具应是 **Quartus**，而非 Xilinx 的 Vivado。源码与文档冲突时以源码为准：这套 RTL 是面向 Intel Cyclone V SoC 的。综合截图到底来自哪个工具，待本地确认。

#### 4.1.2 核心流程

引脚在源码里分两步落地：

1. **端口列表声明**（模块头 `module tpu(...)` 里列出所有引脚名）。
2. **端口方向与位宽声明**（`input` / `output` / `inout` 及位宽）。

随后在「结构化编码」段把信号接进 Qsys 系统。时钟与外设这一类信号的含义如下：

| 引脚 | 方向 / 位宽 | 含义 |
|------|------------|------|
| `CLOCK_50` | input | 板载 50MHz 晶振，整个 SoC 的主时钟来源 |
| `KEY[3:0]` | input [3:0] | 4 个机械按键，**按下为 0**（低有效） |
| `SW[9:0]` | input [9:0] | 10 个拨码开关，电平直接反映开关位置 |
| `LEDR[9:0]` | output [9:0] | 10 个红色 LED，**高电平点亮** |
| `HEX0..HEX5` | output [6:0] ×6 | 6 个七段数码管，**低电平点亮**（共阳极，见 4.1.4） |

#### 4.1.3 源码精读

模块头先列出 FPGA 引脚（与 HPS 引脚分组，用注释隔开）：

```verilog
// Clock pins
CLOCK_50,
// Seven Segment Displays
HEX0, HEX1, HEX2, HEX3, HEX4, HEX5,
// Pushbuttons
KEY,
// LEDs
LEDR,
// Slider Switches
SW,
```

见 [rtl/RTL_modified/tpu.v:7-25](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L7-L25)——这段把板子上「能被 FPGA 逻辑直接使用」的引脚全部罗列出来。

随后是方向与位宽声明，每个引脚的属性一目了然：

```verilog
input CLOCK_50;
input [3:0] KEY;
input [9:0] SW;
output [9:0] LEDR;
output [6:0] HEX0, HEX1, HEX2, HEX3, HEX4, HEX5;
```

见 [rtl/RTL_modified/tpu.v:58-62](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L58-L62)。注意 `HEX0`~`HEX5` 每个都是 7 位——对应七段数码管的 a/b/c/d/e/f/g 七个段。

再看七段码的取反映射，这是本模块最值得品味的几行：

```verilog
wire [31:0] hex3_hex0;
wire [15:0] hex5_hex4;

assign HEX0 = ~hex3_hex0[6:0];
assign HEX1 = ~hex3_hex0[14:8];
assign HEX2 = ~hex3_hex0[22:16];
assign HEX3 = ~hex3_hex0[30:24];
```

见 [rtl/RTL_modified/tpu.v:90-96](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L90-L96)。`hex3_hex0` 是一条 32 位总线，把 4 个七段码（每个 7 位，共 28 位）打包在一起，Qsys 的 PIO（Parallel I/O）外设按「段 a 为最高位、段 g 为最低位」的标准编码输出，**1 表示该段点亮**。而物理数码管是**共阳极（common anode）**结构——某一段要亮，对应的引脚必须被拉**低**。于是用按位取反 `~` 把「1=亮」的逻辑编码翻转成「0=亮」的物理电平。

> 顺带留意：`tpu.v` 第 1 行模块名是 `tpu`，但第 132 行 `endmodule // lab1` 却写着 `lab1`——这是从 `tpu_system.v`（模块名确为 `lab1`）复制改名时遗留的注释，是个真实的源码瑕疵，不影响功能。

#### 4.1.4 代码实践

**实践目标**：亲手验证七段码取反的必要性，并理解按键 / 开关的电平约定。

**操作步骤**（源码阅读型，无需上板）：

1. 打开 [rtl/RTL_modified/tpu.v:93-98](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L93-L98)。
2. 假设想让 `HEX0` 显示数字「0」：标准七段码里「0」是段 a~f 全亮、g 灭，即 7'b011_1111（按 a..g，a 在高位）= 7'h3F。
3. 追踪 `hex3_hex0[6:0] = 7'h3F` 经 `~` 后变成 `7'h40`，这正是物理引脚 `HEX0` 实际收到的电平。
4. 再看 [rtl/RTL_modified/tpu.v:125](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L125)：`.pushbuttons_export(~KEY[3:0])`——按键也取了反，说明 `KEY` 同样是低有效（按下=0），取反后变成「按下=1」的常规逻辑供 Qsys 使用。

**需要观察的现象 / 预期结果**：

- 若**删掉**七段码的 `~`（直接 `assign HEX0 = hex3_hex0[6:0]`），原本该显示「0」的数码管会显示「全段亮中间亮」的乱码——因为段电平反了。这正是必须取反的物证。
- `KEY` 与 `HEX` 都做了取反，但 `LEDR`、`SW` 没有——`LEDR` 是高有效（高=亮），`SW` 是纯电平反映，都不需要翻转。你能从源码里看出 `LEDR` 与 `SW` 直接接进了 Qsys（见 4.3）。

> 结论：是否取反完全取决于**外设的物理有效电平**，而非设计偏好。

#### 4.1.5 小练习与答案

**练习 1**：若把显示「0」的 `7'h3F` 不经取反直接送 `HEX0`，肉眼会看到什么？
**答案**：会看到中间段 g 亮、其余段也亮的「8」字形或乱码（因共阳极下高电平=灭，反转后所有段都接近点亮），不再是「0」。

**练习 2**：`LEDR[9:0]` 为什么不像 `HEX` 那样取反？
**答案**：DE1-SoC 的红色 LED 是共阴极，高电平点亮，与 Qsys 输出的「1=亮」逻辑一致，故无需翻转；`HEX` 共阳极需翻转。是否取反取决于器件有效电平。

---

### 4.2 HPS DDR3 存储接口信号集

#### 4.2.1 概念说明

HPS（ARM Cortex-A9 硬核）要和 FPGA 逻辑共享同一片 **DDR3 SDRAM**——CPU 把待乘矩阵写进 DDR3，TPU 从 DDR3 取数据，算完再写回，CPU 读出结果。这条共享内存通道由 HPS 内部固化的一颗 **DDR3 控制器硬 IP** 驱动，它对外引出固定的一组 PHY 信号，直接连到芯片的 DDR3 引脚上。

FPGA 逻辑**不直接驱动**这些信号——它们是 Qsys 系统里 HPS 组件的专属端口。顶层只负责把这些端口「穿」到芯片的 DDR3 引脚。理解这组信号集，就理解了 SoC 里「存储」这一类信号是如何被处理的。

#### 4.2.2 核心流程

DDR3 是一整套高速差分接口，信号可分为五组：

| 分组 | 信号 | 方向 | 作用 |
|------|------|------|------|
| 命令/地址 | `HPS_DDR3_A[14:0]`、`HPS_DDR3_BA[2:0]` | output | 行/列地址与 bank 选择 |
| 命令控制 | `RAS_n`、`CAS_n`、`WE_n`、`CS_n`、`CKE`、`ODT`、`RESET_n` | output | 行/列选通、片选、时钟使能、片上终端、复位 |
| 时钟（差分） | `CK_p` / `CK_n` | output | 给 DDR3 颗粒的差分时钟对 |
| 数据（双向） | `DQ[31:0]` | inout | 32 位数据总线，CPU↔DDR3 |
| 数据选通（差分） | `DQS_p[3:0]` / `DQS_n[3:0]`、`DM[3:0]` | inout / output | 源同步数据选通与写数据掩码 |
| 校准 | `HPS_DDR3_RZQ` | input | 接外部精密电阻，供 OCT（片上终端）校准 |

其中 `DQ`、`DQS_p/n` 是 `inout`（双向），因为读时由 DDR3 驱向 HPS、写时反向；`RZQ` 是 `input`，接一颗到地的精密电阻，HPS 用它做输出阻抗校准，保证信号完整性。

> 关键认知：**这组信号对用户逻辑是「黑盒」**——你只需把它们从顶层接到 Qsys 的 `memory_mem_*` 端口，时序、训练、刷新全部由 HPS 硬 IP 自动完成。这与 ASIC 里要自己实例化 SRAM macro、自己写控制器截然不同（见 4.4）。

#### 4.2.3 源码精读

DDR3 引脚在模块头按习惯放在「HPS Pins」段：

```verilog
// DDR3 SRAM
HPS_DDR3_A,
HPS_DDR3_BA,
HPS_DDR3_CAS_n,
HPS_DDR3_CKE,
...
HPS_DDR3_RZQ
```

见 [rtl/RTL_modified/tpu.v:31-47](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L31-L47)。

方向与位宽声明揭示出哪些是双向、哪些是单向：

```verilog
output [14:0] HPS_DDR3_A;
output [2:0]  HPS_DDR3_BA;
...
inout  [31:0] HPS_DDR3_DQ;
inout  [3:0]  HPS_DDR3_DQS_n;
inout  [3:0]  HPS_DDR3_DQS_p;
...
input         HPS_DDR3_RZQ;
```

见 [rtl/RTL_modified/tpu.v:68-84](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L68-L84)。注意 `DQ`/`DQS_*` 用 `inout` 声明——这是顶层里仅有的双向端口，恰是 DDR3 源同步协议的特性。`RZQ` 是唯一的 DDR3 相关 `input`。

#### 4.2.4 代码实践

**实践目标**：把 DDR3 信号按功能分组，理解「哪些必须双向、哪个是校准输入」。

**操作步骤**（源码阅读型）：

1. 在 [rtl/RTL_modified/tpu.v:68-84](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L68-L84) 中数一数 DDR3 相关端口共多少个。
2. 把它们按「命令/地址」「时钟」「数据」「校准」四类填进 4.2.2 的表格。
3. 标出所有 `inout` 与唯一的 DDR3 `input`（`RZQ`）。

**需要观察的现象 / 预期结果**：

- `DQ[31:0]`、`DQS_p/n[3:0]` 共 3 个 `inout`，其余 DDR3 信号均为 `output`，外加 `RZQ` 一个 `input`。
- 没有任何用户 `always` 块驱动这些信号——它们只是「穿墙线」，下一节会看到它们被原封不动接进 `tpus_system` 的 `memory_mem_*` 端口。

> 上板验证 DDR3 时序训练需在 Quartus 里全编译并配置 HPS 引脚，本环境无法运行，标记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `HPS_DDR3_DQ` 必须是 `inout`，而 `HPS_DDR3_A` 是 `output`？
**答案**：`DQ` 是数据总线，读时由 DDR3 颗粒驱动向 HPS、写时由 HPS 驱向颗粒，故双向；`A` 是地址线，永远由控制器（HPS）发给颗粒，故单向输出。

**练习 2**：`HPS_DDR3_RZQ` 外接的是什么？起什么作用？
**答案**：外接一颗精密电阻（通常到地）。HPS 用它做 OCT（On-Chip Termination）输出阻抗校准，保证 DDR3 高速信号的阻抗匹配与信号完整性。

---

### 4.3 Qsys 系统例化与 Avalon 端口对接

#### 4.3.1 概念说明

`tpu.v` / `tpu_system.v` 的「结构化编码」段只有一处模块例化——把 Qsys 生成的系统模块接进来。这个系统模块（`tpus_system` / `mysystem`）内部整合了：HPS（含 DDR3 控制器）、若干 PIO 外设（驱动 HEX/KEY/SW/LEDR），以及（在完整设计里）作为 Avalon-MM slave 的 TPU 加速器。

理解这一段，就理解了「**FPGA SoC 集成的整体形态**」：板级顶层是「接线员」，Qsys 系统是「主板」，TPU 是插在主板 Avalon 总线上的一块「加速卡」，CPU（HPS）通过内存映射地址访问它。

> 命名约定：Qsys 自动生成的端口名是 `<组件>.<接口>` 的扁平化形式。如 `.clk_clk`（clk 组件的 clk 接口）、`.reset_reset_n`、`.memory_mem_a`（memory 组件即 HPS DDR3 控制器的 mem_a 接口）、`.pushbuttons_export`（pushbuttons PIO 的 export conduit）、`.hex3_hex0_export` 等。这种「重复前缀」的奇怪写法正是 Qsys 风格的标志。

#### 4.3.2 核心流程

顶层到 Qsys 系统的对接流程：

1. 把 `CLOCK_50` 接到 `.clk_clk`——整个 Qsys 系统的单一时钟源。
2. 把 4.2 的 DDR3 信号一一接到 `.memory_mem_*`（注意差分对 `CK_p→mem_ck`、`DQS_p→mem_dqs` 的映射）。
3. 把外设经 PIO 对接：`hex3_hex0`/`hex5_hex4`（已分组的七段码内部总线）接 `.hex3_hex0_export` / `.hex5_hex4_export`；`LEDR` 接 `.rled_export`；`SW` 接 `.switches_export`；取反后的按键 `~KEY[3:0]` 接 `.pushbuttons_export`。
4. 复位 `.reset_reset_n` 直接接常量 `1'b1`——本顶层不复位 Qsys（复位由 HPS 上电序列管理）。

至于 TPU 计算核 `top.v`，它**不直接出现在 `tpu.v` 里**，而是作为 Avalon-MM slave 组件，在 Qsys 里通过 [rtl/RTL_modified/busConn.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v)（模块名 `matrixMultiplier`）挂到总线上，最终被打包进 `mysystem`/`tpus_system`。详见 u6-l4。

#### 4.3.3 源码精读

唯一的例化块——把板级引脚全部交给 Qsys 系统：

```verilog
tpus_system  system(
    .clk_clk            (CLOCK_50),            // clk.clk
    .reset_reset_n      (1'b1),                // reset.reset_n（常置 1，不复位）

    .memory_mem_a       (HPS_DDR3_A),          // memory.mem_a
    .memory_mem_ba      (HPS_DDR3_BA),
    ...
    .memory_mem_ck      (HPS_DDR3_CK_p),       // 差分正端
    ...
    .memory_oct_rzqin   (HPS_DDR3_RZQ),        // oct.rzqin

    .pushbuttons_export (~KEY[3:0]),            // pushbuttons.export（按键取反）
    .hex3_hex0_export   (hex3_hex0),           // hex3_hex0.export
    .hex5_hex4_export   (hex5_hex4),           // hex5_hex4.export
    .rled_export        (LEDR),                // rled.export
    .switches_export    (SW)                   // switches.export
);
```

见 [rtl/RTL_modified/tpu.v:104-130](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L104-L130)。注意几个细节：

- `.reset_reset_n (1'b1)`（[第 106 行](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L106)）：Qsys 复位被永久拉高，意味着本顶层不主动复位；真正的复位来自 HPS 上电流程。这与 ASIC 里 `srstn` 由外部统一控制（见 u1-l3）形成对比。
- `.memory_mem_ck (HPS_DDR3_CK_p)`（[第 113 行](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L113)）：差分时钟的正端 `CK_p` 对应 Qsys 的 `mem_ck`，负端 `CK_n` 对应 `mem_ck_n`，命名映射需逐一对齐。
- `hex3_hex0` / `hex5_hex4` 是 4.1 里那条 32 位 / 16 位打包总线，Qsys PIO 按它驱动七段码（再经 `~` 取反送物理引脚）。

#### 4.3.4 代码实践

**实践目标**：理清「板级引脚 → Qsys 端口」的完整对接表，定位 TPU 在 SoC 里的接入位置。

**操作步骤**（源码阅读 + 对照）：

1. 打开 [rtl/RTL_modified/tpu.v:104-130](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L104-L130)，画一张两列对照表：左列「物理引脚」，右列「Qsys 端口」。
2. 再打开 [rtl/RTL_modified/busConn.v:189-210](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L189-L210)，确认 `matrixMultiplier` 内部例化了 `top TPU`——这就是 TPU 计算核真正进入 SoC 的地方（Avalon-MM slave）。
3. 对照 [rtl/RTL_modified/top.v:18-34](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/top.v#L18-L34) 的 `top` 模块端口。

**需要观察的现象 / 预期结果**：

- 板级顶层 `tpu.v` 里**看不到任何 TPU 计算逻辑**——它只有一处 Qsys 例化。TPU 藏在 Qsys 系统内部，经 `busConn` 的 Avalon-MM slave 接口被 CPU 访问。
- **发现一个真实的不一致**：`busConn.v` 第 189 行用 `.active / .inputMem_wr_en / .fill_fifo / .drain_fifo …` 等端口名例化 `top`，但 `top.v` 实际端口是 `.start / .done / .opcode / .dim_1/2/3 / .inputMem_wr_data …`，两者对不上。这说明 `busConn.v` 与 `top.v` 处于不同的演进分支，直接综合会报端口不匹配错误。该结论与 u6-l4 的「待确认」一致——**在本环境无法综合验证，标记为「待本地确认」**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tpu.v` 里要写 `.reset_reset_n (1'b1)`，而不是接一个按键？
**答案**：Qsys 系统的复位由 HPS 上电序列统一管理，板级顶层不需要（也不应）用机械按键去异步复位整个 SoC，故直接常拉高（不复位）。

**练习 2**：Qsys 端口名为什么长成 `.clk_clk`、`.memory_mem_a` 这种重复前缀的样子？
**答案**：这是 Qsys 的扁平化命名规则 `<组件名>_<接口名>`——`clk` 组件的 `clk` 接口、`memory` 组件的 `mem_a` 接口。重复是组件名与接口名撞词的结果，是 Qsys 自动生成的标志。

---

### 4.4（对比）FPGA SoC 集成 vs ASIC 后端流程

> 本节是为综合实践（第 5 节）做铺垫的对比模块，帮助把 u5 单元串起来。

把 TPU 做成一颗 ASIC（u5-l1~u5-l3）和把它跑在一块 FPGA SoC 上（本讲），是两条根本不同的工程路线：

| 维度 | ASIC 后端（DC 综合 → ICC PnR） | FPGA SoC 集成（Quartus + Qsys） |
|------|-------------------------------|--------------------------------|
| 映射目标 | Nangate 45nm **标准单元** | FPGA 的 **LUT/FF/DSP/BRAM** |
| 处理器 | 无内置 CPU，需自行集成或假外部主机 | **HPS（ARM Cortex-A9）硬核**，板上即有 |
| 存储 | 自己实例化 SRAM macro 或用触发器拼 | **HPS DDR3 控制器硬 IP** + 片上 BRAM |
| 顶层引脚 | 由 PnR + IO 单元处理 pad | 板级固定物理引脚（`CLOCK_50` 等）+ `.qsf` 约束 |
| 集成方式 | 手写顶层 `tpu_top` 例化五子模块 | **Qsys 自动生成系统模块**，总线互连自动化 |
| 交付物 | 门级网表 + GDSII 版图 | 比特流 `.sof`，烧录即运行 |
| 设计迭代 | 慢（数小时级 PnR） | 快（分钟~十几分钟级编译） |

**接入 SoC 至少要处理四类信号**（这也是综合实践的题眼）：

1. **时钟**：`CLOCK_50` → `.clk_clk`，全系统唯一主时钟（ASIC 里则是 `create_clock` 约束的 `clk`，见 u5-l2）。
2. **复位**：`.reset_reset_n (1'b1)`，本层不复位，交 HPS 管理（ASIC 里 `srstn` 是关键控制信号）。
3. **总线**：Avalon-MM slave（`busConn` 里的 `slave_address/read/write/…`），CPU 经此下命令 / 喂数据 / 取结果（ASIC 无内置总线，靠外部 testbench 或主机接口）。
4. **存储**：HPS DDR3 接口（4.2 那一大组），共享内存通道；外加片上 BRAM 作 inputMem/weightMem/outputMem（见 `top.v`）。

外设类（HEX/KEY/SW/LEDR）属于「人机交互」，是 FPGA 板级特有的便利，ASIC 设计通常不涉及。

---

## 5. 综合实践

**任务**：对照 `tpu.v`（FPGA 板级顶层）与 ASIC 后端流程，整理「FPGA 上把 TPU 接入 SoC」的信号清单，并解释七段码取反。

请完成以下三件事：

1. **画一张信号分类表**。从 [rtl/RTL_modified/tpu.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v) 中提取所有端口，按 **时钟 / 复位 / 总线 / 存储 / 外设** 五类归类。其中：
   - 时钟类：`CLOCK_50`；
   - 复位类：`reset_reset_n`（接 `1'b1`，注意它不是物理引脚而是 Qsys 端口）；
   - 总线类：本顶层不直接暴露，但要指出 TPU 经 `busConn` 的 Avalon-MM slave 进入 SoC（参考 [busConn.v:14-23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/busConn.v#L14-L23)）；
   - 存储类：整组 `HPS_DDR3_*`（[tpu.v:68-84](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L68-L84)）；
   - 外设类：`HEX0-5` / `KEY` / `LEDR` / `SW`。

2. **对比 ASIC 差异**。结合 u5-l1~u5-l3 写 3~5 句话：在 ASIC 路线里这四类信号分别由什么承担？（提示：时钟靠 `create_clock` 约束、复位是 `srstn`、无内置总线与 CPU、存储靠 SRAM macro）。指出 FPGA 路线相比 ASIC 在「集成便利度」上的核心优势。

3. **解释七段码取反**。用一句话说清 [tpu.v:93-98](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/RTL_modified/tpu.v#L93-L98) 里 `~` 的原因，并回答：若板子换成**共阴极**数码管（高电平点亮），这行代码该怎么改？

**预期结果**（要点）：

- 信号表能正确区分 `inout`（`DQ`/`DQS`）与单向端口。
- 能指出「FPGA SoC 自带 CPU + DDR3 控制器 + 自动总线互连（Qsys）」是 ASIC 没有的三大红利，故集成迭代远快于 ASIC。
- 七段码取反答：因共阳极数码管**低电平点亮**，而 Qsys PIO 输出**高有效**段码，故需 `~` 翻转；若换成共阴极，则去掉 `~`，直接 `assign HEX0 = hex3_hex0[6:0];`。

> 本实践为纯源码阅读与分析型，无需运行工具链；若要在 Quartus 中真正编译验证，需完整的 Qsys `.qsys` 文件与 `.qsf` 引脚约束，本仓库未提供，相关结论标记为「待本地验证」。

---

## 6. 本讲小结

- `tpu.v` / `tpu_system.v` 是 **FPGA 板级顶层**，几乎不含逻辑，只把物理引脚对接到 Qsys 生成的系统模块；两份文件除模块名（`tpu` vs `lab1`）与例化的系统名（`tpus_system` vs `mysystem`）外几乎逐行相同。
- **FPGA 引脚**含时钟 `CLOCK_50` 与外设 `HEX0-5/KEY/LEDR/SW`；七段码因共阳极低电平点亮，需 `~` 把高有效段码翻成物理电平。
- **HPS DDR3 接口**是一组固定 PHY 信号（命令/地址、差分时钟、双向 `DQ`/`DQS`、`DM`、校准 `RZQ`），由 HPS 硬核控制器驱动，对用户逻辑是黑盒。
- **Qsys 系统例化**把上述引脚一次性接进 `tpus_system`；TPU 计算核不在此层，而是经 `busConn`（Avalon-MM slave）挂进 SoC，被 HPS（ARM）以内存映射方式访问。
- 与 ASIC 后端对比，FPGA SoC 集成的核心差异在于「自带 CPU + 硬核 DDR3 控制器 + 自动总线互连」，迭代快但性能/面积不如定制芯片。
- 源码存在两处真实瑕疵需记取：README 称用 Vivado 但 RTL 实为 Intel 风格；`busConn.v` 与 `top.v` 的例化端口对不上（待本地确认）。

---

## 7. 下一步学习建议

- 想搞清 **TPU 如何经 Avalon 总线被 CPU 调用**（RESET/FILL_FIFO/MULTIPLY 等命令、地址空间划分、读回 `done` 状态），进入 **u6-l4 Avalon 总线接口与控制指令**，精读 `busConn.v`。
- 想了解 Qsys 系统里那个真正的 TPU 计算核（`master_control` 调度 + `sysArr` 阵列 + `accumTable` 分块累加 + `reluArr` 激活 + `weightFifo` 歪斜喂入），从 **u6-l1 扩展 TPU 架构总览** 开始，逐篇读 `top.v` 及其子模块。
- 若对 FPGA 板级工程感兴趣，建议在拿到 DE1-SoC/DE10-Nano 实板后，用 Quartus 的 Platform Designer 重建 `.qsys`，把本项目 TPU 作为一个 Avalon-MM slave 组件挂上去，亲历从综合到下载的完整流程（本仓库未提供 Qsys 工程文件，需自行搭建）。
