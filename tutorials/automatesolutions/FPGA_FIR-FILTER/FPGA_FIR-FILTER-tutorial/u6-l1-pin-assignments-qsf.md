# 引脚分配与物理约束（QSF）

## 1. 本讲目标

本讲是「专家层」的第一篇。在前面的讲义里，`sharp.vhd` 及其子模块一直被当作「纯逻辑」来读——我们关心的是数据怎么流、系数怎么算。但 VHDL 描述的电路最终要落到一颗真实芯片的物理引脚上，才能和摄像头、显示器、拨码开关连通。这件「把端口接到芯片球上」的事，就写在 `FIR.qsf` 里。

学完本讲，你应当能够：

- 看懂 `set_global_assignment` 如何指定器件族、器件型号和顶层实体；
- 读懂 `set_location_assignment` 把每个端口绑定到哪个物理引脚（BGA 球），并理解输入/输出引脚在芯片上的分组规律；
- 读懂 `set_instance_assignment ... IO_STANDARD` 给每个端口设定的电气标准（3.3-V LVTTL），以及 `VHDL_FILE`/`SDC_FILE` 文件清单的作用；
- 独立地在 `FIR.qsf` 中为一个新信号新增引脚分配与 IO 标准，并重新编译确认无引脚冲突。

> 与 u1-l3 的分工：u1-l3 讲了「`.qpf` 是入口名片、`.qsf` 是真正配置」「器件 `5CEBA2F17C6`」「74.25 MHz/720p」「编译四阶段」。本讲不再重复这些，而是钻进 `.qsf` 内部，逐类拆解它写下的每一条约束。

## 2. 前置知识

阅读本讲前，建议你已经建立以下概念（均在前置讲义中出现）：

- **顶层实体 `sharp` 的端口**：在 u3-l1 中精读过，包括时钟 `clk`、复位 `reset_n`、3 位拨码开关 `enable_in`、视频时序 `vs_in/hs_in/de_in`、RGB 输入 `r_in/g_in/b_in`（各 8 位），以及对应的一组输出与 `clk_o`、`led`。
- **Quartus 工程文件**：`.qpf`（工程入口）和 `.qsf`（设置与约束），见 u1-l3。
- **目标芯片**：Cyclone V `5CEBA2F17C6`，FBGA-256 封装，约 128 个用户 IO，运行在 74.25 MHz。

本讲会用到两个尚未正式介绍、但很直观的概念，先在这里点一下：

- **BGA 引脚命名**：芯片背面是一个字母行 + 数字列的网格，每个焊球（ball）用「字母 + 数字」定位，例如 `PIN_A12` 表示 A 行第 12 列的那个球。`set_location_assignment` 用的就是这个坐标。
- **IO 标准（IO Standard）**：FPGA 的每个 IO 引脚并不是「万能」的，它需要被告知「按什么电气协议收发信号」。`3.3-V LVTTL` 表示单端、电源电压 VCCIO = 3.3 V、输入阈值和输出电平都按 LVTTL 规范。同一组（同一 IO Bank）的引脚通常共享一个 VCCIO，所以电气标准要一致。

## 3. 本讲源码地图

本讲几乎只围绕一个文件展开，但要把它和顶层实体对着读：

| 文件 | 作用 |
| --- | --- |
| `FPGA-Design/FIR.qsf` | Quartus 设置文件，工程级「真正的配置」：器件、顶层、引脚分配、IO 标准、源文件清单。本讲主角。 |
| `FPGA-Design/sharp.vhd` | 顶层实体，定义了被约束的所有端口。读 QSF 的引脚分配时，要随时回这里对端口名和位宽。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**器件与顶层设置**（工程要烧到哪颗芯片、综合哪个实体）、**引脚分配**（每个端口接到哪个物理球）、**IO 标准与文件清单**（每个端口的电气协议 + 要编译哪些源文件）。

### 4.1 器件与顶层设置

#### 4.1.1 概念说明

`.qsf` 里大量以 `set_global_assignment` 开头的行，写的是「工程级」参数——它们不属于某个具体端口，而是描述整个工程。其中最关键的三条决定了「为哪颗芯片、综合哪个实体」：

- `FAMILY`：器件族（Cyclone V），圈定综合器可用的底层资源类型（ALM、M10K、DSP 等）。
- `DEVICE`：具体型号 `5CEBA2F17C6`，圈定封装、容量、IO 数量。型号尾缀里，`F17` 指封装（FBGA-17 系列，即 256 球），`C6` 指速度等级。
- `TOP_LEVEL_ENTITY`：顶层实体名 `sharp`。Quartus 从它开始，按例化关系递归向下找出全部需要编译的模块。

这三条一旦写错，后果是「综合出来的网表与目标芯片对不上」或「找错顶层」，是最先要核对的项目。

#### 4.1.2 核心流程

Quartus 在综合前的读约束阶段大致按如下顺序消化这些全局设置：

1. 读取 `FAMILY` → 加载 Cyclone V 的器件原语库；
2. 读取 `DEVICE` → 锁定 `5CEBA2F17C6` 的资源上限与封装引脚地图；
3. 读取 `TOP_LEVEL_ENTITY` → 以 `sharp` 为根，按 `entity work.xxx` 例化关系（见 u3-l1）展开整棵设计树；
4. 把 `PROJECT_OUTPUT_DIRECTORY output_files` 指向的目录作为产物输出地（u1-l3 提到的 `.sof` 等都落在这里）；
5. 记录 `EDA_SIMULATION_TOOL` 等 EDA 选项，供后续生成仿真网表使用。

#### 4.1.3 源码精读

器件族、型号与顶层实体三条紧挨在一起：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L40-L42](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L40-L42) —— 指定 Cyclone V 器件族、型号 `5CEBA2F17C6`、顶层实体 `sharp`。

```tcl
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CEBA2F17C6
set_global_assignment -name TOP_LEVEL_ENTITY sharp
```

产物目录与仿真工具选项：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L46-L48](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L46-L48) —— 产物输出目录 `output_files`、仿真工具 Questa（Verilog 网表）。

```tcl
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files
...
set_global_assignment -name EDA_SIMULATION_TOOL "Questa Intel FPGA (Verilog)"
```

还有一个常被忽略但很实用的全局项——**器件迁移列表**：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L206](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L206) —— 允许在 `5CEBA2F17C6` 与更大容量的 `5CEBA4F17C6` 之间迁移（同封装、不同逻辑容量）。

```tcl
set_global_assignment -name DEVICE_MIGRATION_LIST "5CEBA2F17C6,5CEBA4F17C6"
```

它的含义是：当前设计也允许被迁移到同封装（`F17`）但容量更大的 `5CEBA4F17C6`。当你发现资源占用接近上限时，可以无缝换到更大的型号而不必改引脚分配——因为两颗芯片的球数和封装一致。这是 Quartus 在做「设计可移植性」提示。

#### 4.1.4 代码实践

**目标**：亲手改一次全局器件设置，体会 `DEVICE` 改变后资源上限的变化。

**操作步骤**：

1. 备份 `FIR.qsf`（本讲所有实践都建议先备份）。
2. 把 `DEVICE` 改成迁移列表里更大的 `5CEBA4F17C6`：

   ```tcl
   set_global_assignment -name DEVICE 5CEBA4F17C6
   ```

3. 在 Quartus 中重新编译（Processing → Start Compilation）。
4. 编译完成后打开 Compilation Report → Fitter → Resource Usage，对比 ALM / Block Memory 的「占用百分比」。

**需要观察的现象**：分母（总资源）变大，故同样的设计占用百分比下降；引脚分配不会报冲突，因为封装相同、引脚地图兼容。

**预期结果**：设计成功编译，资源占用率比原先更低。

**待本地验证**：本讲无法替你运行 Quartus，具体百分比数字以你的 Fitter 报告为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `TOP_LEVEL_ENTITY` 从 `sharp` 改成一个不存在的名字（如 `foo`），综合时会怎样？

**参考答案**：Quartus 找不到名为 `foo` 的顶层实体，会在 Analysis & Synthesis 阶段报错（无法确定设计顶层），编译无法继续。这说明顶层实体名必须与某个 `entity ... is` 声明一致。

**练习 2**：`DEVICE` 末尾的 `C6` 代表什么？为什么换到 `5CEBA4F17C6` 时引脚分配仍然有效？

**参考答案**：`C6` 是速度等级（speed grade），数字越小越慢也越便宜。`5CEBA4F17C6` 与 `5CEBA2F17C6` 同属 `F17` 封装（256 球 BGA），物理引脚地图一致，所以原有的 `set_location_assignment` 仍然成立——这正是 `DEVICE_MIGRATION_LIST` 的意义。

---

### 4.2 引脚分配

#### 4.2.1 概念说明

如果说 4.1 解决的是「为哪颗芯片编译」，那么 4.2 解决的是「每个端口接到芯片的哪个球」。这件事由一行行 `set_location_assignment` 完成，格式固定：

```tcl
set_location_assignment PIN_<坐标> -to <顶层端口名>
```

- `PIN_A12` 这类「字母 + 数字」就是 4.2 节开头说的 BGA 网格坐标；
- `-to` 后面是顶层实体 `sharp` 的端口名（含位宽下标，如 `r_in[0]`）。

这一步把「逻辑端口」与「物理球」绑定，Fitter（布局布线器）才会把综合出的端口网络接到对应 IO 单元（IOE）上。读这组约束时，最有效的方法是**和 `sharp.vhd` 的 entity 对着看**：每条 `-to` 都能在端口表里找到对应。

#### 4.2.2 核心流程

引脚分配的运作流程：

1. Quartus 综合 `sharp` 得到顶层端口列表；
2. 逐条读 `set_location_assignment`，把端口绑到指定球；
3. 检查冲突：同一个球不能绑两个端口，绑定的球必须在 `DEVICE` 的引脚地图里存在且可用；
4. Fitter 据此把每个端口布线到对应的 IOE，并按 IO 标准配置电气属性（见 4.3）。

把 `FIR.qsf` 里 63 条引脚分配按信号分组，可以清楚看到输入与输出被有意地分到了芯片的不同物理区域：

| 信号组 | 方向 | 位宽 | 代表性引脚（节选） | 物理区域 |
| --- | --- | --- | --- | --- |
| `r_in/g_in/b_in` | 输入 | 8×3 | `PIN_P4..T7`、`PIN_T8..T13`、`PIN_P13..P16` | 右半区 P/R/T 列 |
| `vs_in/hs_in/de_in` | 输入 | 1×3 | `PIN_L10`、`PIN_M10`、`PIN_N11` | 中右部 |
| `clk / reset_n` | 输入 | 1×1 | `PIN_P9` / `PIN_T2` | 右半区 |
| `enable_in[2:0]` | 输入 | 3 | `PIN_J16`、`PIN_H15`、`PIN_G16` | 中部（拨码开关） |
| `r_out/g_out/b_out` | 输出 | 8×3 | `PIN_A3..C4`、`PIN_A9..C11`、`PIN_A12..D14` | 左半区 A/B/C 列 |
| `vs_out/hs_out/de_out` | 输出 | 1×3 | `PIN_E16`、`PIN_E15`、`PIN_D16` | 左半区 |
| `clk_o` | 输出 | 1 | `PIN_F15` | 左半区 |
| `led[2:0]` | 输出 | 3 | `PIN_H13`、`PIN_G15`、`PIN_J14` | 中部（LED） |

数一数：输入侧约 32 个、输出侧约 31 个，合计 **63 个**，恰好等于 `sharp` 实体的全部端口位数（见 u3-l1 的端口清单）。输入集中在右半区（P/R/T 列，靠近视频输入连接器），输出集中在左半区（A/B/C 列，靠近视频输出连接器）——这种「输入一边、输出一边」的布局是为了让 PCB 走线更短、更不互相干扰，是硬件工程师的有意安排。

#### 4.2.3 源码精读

视频输出 `b_out` 整组（8 位）逐位绑定到左半区连续的球：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L62-L69](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L62-L69) —— 把 `b_out[7..0]` 绑到 `PIN_A12..D14` 一组左半区引脚。

```tcl
set_location_assignment PIN_A12 -to b_out[7]
set_location_assignment PIN_B12 -to b_out[6]
...
set_location_assignment PIN_D14 -to b_out[0]
```

注意位序：`b_out[7]` 是最高位（MSB），分配在 `PIN_A12`。在对照 `sharp.vhd` 端口声明时，`r_out : out std_logic_vector(7 downto 0)` 的 `7` 正是 MSB。

时钟与复位这两个全局控制信号各占一球：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L101-L102](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L101-L102) —— 输入时钟 `clk`（74.25 MHz）绑到 `PIN_P9`，数据使能 `de_in` 绑到 `PIN_N11`。

```tcl
set_location_assignment PIN_P9  -to clk
set_location_assignment PIN_N11 -to de_in
```

3 位拨码开关 `enable_in`（本设计中虽被采样但未参与运算，见 u3-l1）：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L103-L105](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L103-L105) —— `enable_in[2..0]` 绑到 `PIN_J16/H15/G16`，对应板上 3 个拨码开关。

```tcl
set_location_assignment PIN_J16 -to enable_in[2]
set_location_assignment PIN_H15 -to enable_in[1]
set_location_assignment PIN_G16 -to enable_in[0]
```

复位与场同步这类「单 bit 控制」也各占一球：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L114-L124](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L114-L124) —— 行同步 `hs_in`（`PIN_M10`）、`r_in[7..0]`（`PIN_P4..P7`）、复位 `reset_n`（`PIN_T2`）、场同步 `vs_in`（`PIN_L10`）。

```tcl
set_location_assignment PIN_M10 -to hs_in
...
set_location_assignment PIN_T2  -to reset_n
set_location_assignment PIN_L10 -to vs_in
```

> 提醒：`reset_n` 末尾的 `_n` 表示「低有效」，与 `sharp.vhd` 内部 `reset <= not reset_n` 的取反对应（见 u3-l1）。命名约定的「物理含义」在引脚分配阶段也要一并留意。

#### 4.2.4 代码实践

**目标**：用 Quartus 的图形界面「反向核验」QSF 里的引脚分配，建立「文本 ↔ 图形」的双向对应。

**操作步骤**：

1. 打开工程 `FIR.qpf`。
2. 菜单 Assignments → Pin Planner（或快捷键 Ctrl+Shift+N）。
3. 在 Pin Planner 的表格里找到 `Location` 列，确认 `r_in[7]` 的位置是 `PIN_P4`、`b_out[7]` 是 `PIN_A12`、`clk` 是 `PIN_P9`。
4. 在 Pin Planner 底部的封装视图里，观察输入端口（P/R/T 列）与输出端口（A/B/C 列）的分布。

**需要观察的现象**：Pin Planner 显示的引脚与 QSF 文本完全一致；图形上输入、输出明显分居两侧。

**预期结果**：文本里的每条 `set_location_assignment` 都能在 Pin Planner 找到对应行，且无任何端口显示为 `Unassigned`。

**待本地验证**：具体封装视图外观以你本机的 Quartus 版本为准（本工程用 23.1std.1 Lite）。

#### 4.2.5 小练习与答案

**练习 1**：`set_location_assignment PIN_A12 -to b_out[7]` 里，`b_out[7]` 是 MSB 还是 LSB？依据是什么？

**参考答案**：是 MSB。依据是 `sharp.vhd` 里 `b_out : out std_logic_vector(7 downto 0)`，`downto` 表示下标 7 是最高位；同时 QSF 中 `b_out[7]` 对应第一个分配 `PIN_A12`，与实体位序一致。

**练习 2**：假如你不小心把 `clk` 和 `reset_n` 都分配到了 `PIN_T2`，会发生什么？

**参考答案**：两个端口争用同一个物理球，Quartus 在 Fitter 阶段报「引脚冲突 / location assignment conflict」错误，编译失败。这正是「同一个球不能绑两个端口」规则的体现，也是 4.3 节实践要排查的情形。

---

### 4.3 IO 标准与文件清单

#### 4.3.1 概念说明

引脚分配只回答了「接到哪个球」，还要回答「按什么电气协议收发」——这就是 IO 标准。`.qsf` 用 `set_instance_assignment` 逐端口指定：

```tcl
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to <端口名>
```

`3.3-V LVTTL` 是单端、VCCIO = 3.3 V 的传统 TTL 电平，输入/输出阈值固定（VIH ≈ 2.0 V，VOL ≈ 0.4 V 等）。EduPow 板上的视频接口、拨码开关、LED 都跑在 3.3 V 单端信号上，所以本设计**所有**端口统一采用这一标准。电气标准必须与 PCB 上对该 Bank 的供电（VCCIO）一致，否则电平不匹配，轻则误码、重则损坏。

本模块还包含 `.qsf` 的另一项核心职责——**源文件清单**。Quartus 不会自动扫描目录里的所有 `.vhd`，它只编译 `VHDL_FILE`/`SDC_FILE` 显式列出的文件。漏列一个文件，对应模块就会「找不到」。

#### 4.3.2 核心流程

- **IO 标准**：Fitter 在配置每个 IOE 时，按 `IO_STANDARD` 设定该引脚的输入缓冲、输出驱动、VCCIO 归属。同一 IO Bank 的引脚若被指定了矛盾的 VCCIO，会报 Bank 冲突。
- **文件清单**：Quartus 读到 `VHDL_FILE` 列表后，把这些文件加入编译；`TOP_LEVEL_ENTITY` 决定从哪个 entity 开始递归例化（顶层例化关系见 u3-l1）。文件在 QSF 中的**先后顺序不影响编译**——Quartus 会自行做依赖分析——但保持「子模块在前、顶层在后」的习惯有助阅读。

#### 4.3.3 源码精读

本设计要编译的全部源文件清单（5 个 VHDL + 1 个 SDC）：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L125-L130](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L125-L130) —— 列出 5 个 VHDL 文件（control/slice/linemem/arith/sharp）与 1 个时序约束文件 `sharp.sdc`（时序约束留待 u6-l2）。

```tcl
set_global_assignment -name VHDL_FILE sharp_control.vhd
set_global_assignment -name SDC_FILE  sharp.sdc
set_global_assignment -name VHDL_FILE sharp_slice.vhd
set_global_assignment -name VHDL_FILE sharp_linemem.vhd
set_global_assignment -name VHDL_FILE sharp_arith.vhd
set_global_assignment -name VHDL_FILE sharp.vhd
```

这正是 u1-l2 给出的「5 个 `sharp*.vhd`」结构在 QSF 里的兑现。注意 `sharp.vhd`（顶层）排在最后，但编译结果与顺序无关。

IO 标准逐端口声明，几乎全是 `3.3-V LVTTL`。以红色输入 `r_in` 为例：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L131-L138](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L131-L138) —— `r_in[0..7]` 逐位声明为 3.3-V LVTTL。

```tcl
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to r_in[0]
...
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to r_in[7]
```

LED、时钟、复位等控制信号也是同一标准：

[FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf:L188-L194](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L188-L194) —— `clk / clk_o / reset_n / led[0..2]` 同样设为 3.3-V LVTTL。

```tcl
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk_o
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk_n_o
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to reset_n
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to led[0]
```

> ⚠️ **一个值得注意的「残留约束」**：上面第 3 行出现了 `-to clk_n_o`，但 `sharp.vhd` 的 entity 里**并没有** `clk_n_o` 这个端口，QSF 里也没有为它做 `set_location_assignment`。这是项目历史遗留的一条「孤儿约束」——它会触发 Quartus 的一条警告（指派的目标在设计中不存在），但不会让编译失败。读懂这一点很重要：它告诉你 **`.qsf` 不会自动校验 `-to` 是否真实存在**，残留约束会悄悄累积。做二次开发时，删掉这类无用行是良好习惯。

（注：本设计逐位书写 IO 标准，是为了和引脚分配一一对应、便于阅读。Quartus 也支持通配写法，例如 `set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to r_in[*]`，一行覆盖整组。）

#### 4.3.4 代码实践

本模块的实践并入第 5 节「综合实践」——在那里你会完整地新增一个端口的「引脚分配 + IO 标准」两条约束，正是本模块的直接应用。在动手前，请先记住这两条「模板」：

```tcl
# 引脚分配
set_location_assignment PIN_<坐标> -to <新端口名>
# IO 标准
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to <新端口名>
```

#### 4.3.5 小练习与答案

**练习 1**：为什么本设计所有端口都用 `3.3-V LVTTL`，而不是更高速的 LVDS 或 SSTL？

**参考答案**：EduPow 板的视频输入/输出、拨码开关、LED 都是 3.3 V 单端信号，速率不高（74.25 MHz 像素时钟），3.3-V LVTTL 完全够用且电平与外设一致。LVDS/SSTL 适用于差分或高速内存接口，反而需要不同的 PCB 供电与终端电阻，不匹配本板。

**练习 2**：如果你新增了一个 VHDL 文件 `sharp_bypass.vhd` 并在顶层例化了它，但忘了在 QSF 里加 `VHDL_FILE`，会怎样？

**参考答案**：Quartus 找不到该模块的实体定义，会在 Analysis & Synthesis 报「cannot find entity `sharp_bypass`」之类的错误。这说明源文件清单是 Quartus 决定「编译什么」的唯一依据，新文件必须显式登记。

---

## 5. 综合实践

**任务**：为顶层新增一个独立的使能开关信号，在 `FIR.qsf` 中为它分配一个空闲引脚并设置 3.3-V LVTTL IO 标准，重新编译确认无引脚冲突。

> 说明：本设计已存在 3 位拨码开关 `enable_in[2:0]`（u3-l1 指出它被采样但未使用）。本实践为了演示「新增端口 → 分配引脚 → 设 IO 标准 → 编译验证」的完整 QSF 工作流，再新增一个**独立**的单比特开关信号 `sharp_bypass`（读者可后续用它控制「锐化/直通」切换）。这是在你自己的工作副本上进行的练习，正常地涉及一处实体改动 + 两行 QSF 改动。

**操作步骤**：

1. **在顶层实体声明新端口**。打开 `sharp.vhd`，在 entity 的 port 列表里加一行（位置随意，建议放在 `enable_in` 附近）：

   ```vhdl
   sharp_bypass : in std_logic;   -- 新增：锐化/直通切换开关
   ```

   先不必在架构体里使用它——只要端口存在，QSF 的引脚约束就能生效，Fitter 才会真正把它布线到一个物理球。

2. **找一个空闲引脚**。在 Quartus 里打开 Pin Planner，挑选一个当前未被任何端口占用、且在板上是用户可访问输入（如一个空闲拨码开关或按键）的球，记下它的坐标，例如 `PIN_<你选的坐标>`。具体哪个球空闲取决于你的板，**待本地确认**——切勿复用本讲表格里已占用的 63 个球。

3. **在 `FIR.qsf` 末尾（任何 `set_location_assignment` 区域均可）追加两行**：

   ```tcl
   set_location_assignment PIN_<你选的坐标> -to sharp_bypass
   set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to sharp_bypass
   ```

   第一行做引脚分配，第二行设电气标准——这正是 4.3.4 节给出的两条模板。

4. **重新编译**：Processing → Start Compilation。

5. **核对无冲突**：编译完成后查看 Compilation Report。
   - 若 Fitter 报「location assignment conflict」或「pin ... already assigned」，说明第 2 步选到了已占用球，回到 Pin Planner 重选；
   - 若报告显示 `sharp_bypass` 成功落到你选的球、且总引脚数变为 64，则实践成功。

**需要观察的现象**：编译通过；Fitter 报告中新端口 `sharp_bypass` 出现在引脚列表里，位置与你分配的坐标一致；原来 63 个端口的分配保持不变（无回归）。

**预期结果**：新增一条有效的引脚分配，不与既有 63 条冲突。

**待本地验证**：具体可用的空闲引脚、Quartus 报告的措辞，以你的板与本机 Quartus 为准。若你暂时没有硬件，可只做第 1、3 步并在 Quartus 跑到 Fitter 阶段，观察是否报「未分配/冲突」即可体会流程。

**延伸思考**：如果你在第 3 步漏写了 `IO_STANDARD` 那一行，Quartus 会用什么默认标准？通常会用 `.qdf` 里的默认 IO 标准（往往也是 LVTTL），但显式写出才能保证可移植与文档化——这也是本设计「逐端口显式声明」的原因。

## 6. 本讲小结

- `FIR.qsf` 用 `set_global_assignment` 设定工程级参数，其中 `FAMILY`/`DEVICE`/`TOP_LEVEL_ENTITY` 三条决定「为哪颗 Cyclone V 芯片、综合哪个 `sharp` 实体」。
- `set_location_assignment PIN_<坐标> -to <端口>` 把每个顶层端口绑定到 BGA 的一个物理球；本设计共 63 个端口，输入集中在右半区（P/R/T 列）、输出集中在左半区（A/B/C 列），是有意的 PCB 布局。
- 引脚分配要与 `sharp.vhd` 的 entity 端口表逐位对照，注意 `downto` 决定的 MSB/LSB 位序与 `_n` 表示的低有效含义。
- `set_instance_assignment -name IO_STANDARD "3.3-V LVTTL"` 给每个端口定电气标准，全设计统一 3.3 V 单端，与 EduPow 板的外设电平一致。
- `VHDL_FILE`/`SDC_FILE` 清单是 Quartus 决定「编译哪些文件」的唯一依据；漏列即报「找不到实体」。文件顺序不影响编译。
- `.qsf` 不会自动校验 `-to` 是否真实存在，因此会留下 `clk_n_o` 这类「孤儿约束」（有 IO 标准却无端口、无引脚），二次开发时应主动清理。

## 7. 下一步学习建议

本讲把「物理约束」中的**引脚与 IO 标准**讲完了，但 `.qsf` 还没有覆盖**时序**——那部分写在 `sharp.sdc` 里。下一讲 **u6-l2 时序约束与 74.25MHz 时钟（SDC）** 会精读 `create_clock`（13.46 ns 周期）、`create_generated_clock`、`set_input_delay`/`set_output_delay` 与 `derive_clock_uncertainty`，回答「这 63 个引脚的建立/保持时间能否在 74.25 MHz 下收敛」。

建议你在进入 u6-l2 前，先把本讲的综合实践做完——亲手分配一个引脚、看一次 Fitter 报告，会让你在读 SDC 时对「IO 延迟是相对哪些引脚说的」有更扎实的直觉。学完两篇后，可继续 **u6-l3 架构取舍与二次开发实践**，把「改系数 → 改引脚/约束 → 仿真验证」串成一个完整的二次开发闭环。
