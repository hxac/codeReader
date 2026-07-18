# 构建与烧录流程：Vivado 工程从生成到 bitstream

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 `vivado_generate_project.tcl` 与 `vivado_build.tcl` 这两个脚本的**调用顺序**和各自职责。
- 理解 Vivado 工程里的 `synth_1`（综合）和 `impl_1`（实现）两个 run 是什么，以及它们如何最终产出可以烧录的 `.bin` 比特流文件。
- 了解不同设备的**烧录途径**差异：PCIeSquirrel 用 OpenOCD、CaptainDMA 家族用 CH347，且烧录与「构建」是两个相互独立的环节。
- 解释为什么 `build.md` 会提示「路径过长会导致构建失败」。

本讲只讲**构建与烧录的工程流程**，不深入 HDL 源码逻辑——那是后续讲义的内容。

## 2. 前置知识

在开始前，先用大白话澄清几个概念：

- **Vivado**：Xilinx 公司的官方 FPGA 开发软件。我们用的是免费版 **Vivado WebPACK**（2023.2 或更高版本），它既能用图形界面操作，也能用 Tcl（一种脚本语言）命令操作。本讲的两个 `.tcl` 文件就是给 Vivado 的「批处理指令清单」。
- **综合（Synthesis）**：把你写的 SystemVerilog 源码翻译成 FPGA 上真实存在的逻辑门、寄存器等底层元件的过程，对应 Vivado 里的 `synth_1` run。
- **实现（Implementation）**：把综合后的逻辑「摆」到具体 FPGA 芯片的物理资源上（布线、放置），并最终生成可烧录文件，对应 `impl_1` run。
- **比特流（bitstream）/ `.bin`**：一串 0/1 数据，烧进 FPGA 后它就「变成」了你设计的电路。Vivado 默认产出 `.bit`，本工程额外要求产出 `.bin`。
- **烧录（Flash）**：把比特流写进板卡的配置芯片（如 SPI Flash）或直接加载进 FPGA。构建（在电脑上用 Vivado 做）和烧录（把文件写进板卡）是两件事。
- **Tcl Shell**：Vivado 自带的命令行窗口（开始菜单里的「Vivado Tcl Shell」），你在里面输入 `source xxx.tcl` 就能执行脚本。

> 回顾前两讲：仓库「一设备一目录」，每个目录（如 `PCIeSquirrel/`）是一个独立的 Vivado 工程，里面有 `src/`（源码与约束）、`ip/`（IP 配置）、以及两个 `.tcl` 脚本。本讲就聚焦这最后一块拼图——怎么把这些文件变成一块能用的板卡。

## 3. 本讲源码地图

本讲涉及的关键文件（都以主参考工程 PCIeSquirrel 为例）：

| 文件 | 作用 |
| --- | --- |
| [PCIeSquirrel/build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) | 构建步骤说明、路径过长警告、定制设备身份（VID/PID/DSN/配置空间）的指引 |
| [PCIeSquirrel/readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md) | 硬件介绍、构建步骤、烧录（OpenOCD）说明、版本历史 |
| [PCIeSquirrel/vivado_generate_project.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl) | **工程生成脚本**：从零创建一个 Vivado 工程，导入源码/约束/IP |
| [PCIeSquirrel/vivado_build.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl) | **构建脚本**：跑综合、实现、生成比特流，并复制 `.bin` 出来 |
| [CaptainDMA/readme.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md) | CaptainDMA 家族烧录说明（CH347 工具、各型号差异） |

## 4. 核心概念与源码讲解

### 4.1 工程生成脚本 vivado_generate_project.tcl

#### 4.1.1 概念说明

仓库里**并不包含**一个现成可用的 Vivado 工程文件（`.xpr`），而是只包含源码（`.sv/.svh`）、约束（`.xdc`）和 IP 定义（`.xci`）。这样做的目的是：Vivado 工程目录里有大量自动生成的中间文件和 Xilinx 专有 IP 产物，不适合放进 Git；用户拿到源码后，由 `vivado_generate_project.tcl` **现场重新生成**一个干净的工程。

这个脚本本质上就是「把仓库里的源文件按规则组装成一个 Vivado 工程」。它做的事情可以概括为：建工程 → 导入源码 → 导入 IP → 导入约束 → 配置 run。

#### 4.1.2 核心流程

`vivado_generate_project.tcl` 的执行流程（从上到下）：

1. **确定基准目录与工程名**：默认工程名为 `pcileech_squirrel`，基准目录为脚本所在目录（`.`），可通过命令行参数覆盖。
2. **创建工程**：调用 `create_project`，指定目标 FPGA 型号 `xc7a35tfgg484-2`（Artix-7 35T，FGG484 封装，速度等级 -2）。
3. **设置工程属性**：默认库、IP 缓存目录、仿真器语言等。
4. **导入源码 fileset `sources_1`**：把 `src/` 下的 11 个 `.sv/.svh` 文件加入设计源。
5. **导入 IP 与数据文件**：把 `ip/` 下的 `.xci`（如 `pcie_7x_0.xci`、各类 FIFO）和 `.coe`（配置空间初始化数据）加入工程。
6. **导入约束 fileset `constrs_1`**：加入 `pcileech_squirrel.xdc`。
7. **升级 IP**：`upgrade_ip [get_ips *]`，把旧版 Vivado 生成的 IP 升级到当前安装版本。
8. **创建 synth_1 / impl_1 两个 run**：并设置 `impl_1` 要生成 `.bin` 文件。

#### 4.1.3 源码精读

**工程名与基准目录**可被命令行参数覆盖，体现了脚本的通用性：

[PCIeSquirrel/vivado_generate_project.tcl:7-20](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L7-L20) —— 设置 `origin_dir`（源文件相对基准目录）和工程名 `_xil_proj_name_`，允许用 `--origin_dir`、`--project_name` 覆盖。

**创建工程并指定芯片型号**：

[PCIeSquirrel/vivado_generate_project.tcl:74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74) —— `create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part xc7a35tfgg484-2`，工程建在 `./pcileech_squirrel/` 子目录，锁定了 Artix-7 35T 这颗芯片。

**导入 11 个设计源文件**（`pcileech_header.svh` 是 Verilog 头文件，其余是 SystemVerilog）：

[PCIeSquirrel/vivado_generate_project.tcl:108-121](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L108-L121) —— `com/fifo/ft601/mux/pcie_a7/pcie_cfg/pcie_tlp/bar_controller/cfgspace_shadow/squirrel_top` 这一串文件就是整个 PCIeSquirrel 工程的全部 RTL。同时设定顶层模块名为 `pcileech_squirrel_top`（见 [L174](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L174)）。

**导入 IP 与 .coe 数据**：

[PCIeSquirrel/vivado_generate_project.tcl:181-187](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L181-L187) —— 加入 `pcileech_cfgspace.coe`（配置空间初始化数据）、`pcileech_bar_zero4k.coe`（BAR 全零初始化）等，以及对应的 BRAM IP `bram_pcie_cfgspace.xci`。`.coe` 是「系数/内存初始化文件」，告诉 IP 上电后寄存器里装什么初值。

[PCIeSquirrel/vivado_generate_project.tcl:432-434](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L432-L434) —— 导入 `pcie_7x_0.xci`，这是 Xilinx 7 系列 PCIe 硬核 IP（后续 u3 单元会详讲）。

**导入约束文件**：

[PCIeSquirrel/vivado_generate_project.tcl:568-572](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L568-L572) —— 把 `src/pcileech_squirrel.xdc` 加入 `constrs_1` 约束集，类型标记为 `XDC`（Xilinx Design Constraints，约束文件）。

**升级 IP**：

[PCIeSquirrel/vivado_generate_project.tcl:593](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L593) —— `upgrade_ip [get_ips *]`，把仓库里用旧版 Vivado 生成的 IP 全部升级到当前版本（这就是为什么 readme 说用户要自行「重生成 Xilinx 专有 IP」）。

**创建 synth_1 / impl_1 两个 run**：

[PCIeSquirrel/vivado_generate_project.tcl:602-608](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L602-L608) —— 若不存在则 `create_run` 创建综合 run `synth_1`，绑定 `constrs_1` 约束集。

[PCIeSquirrel/vivado_generate_project.tcl:628-634](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L628-L634) —— 创建实现 run `impl_1`，其 `parent_run` 是 `synth_1`（实现依赖综合的产出）。

[PCIeSquirrel/vivado_generate_project.tcl:838-841](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L838-L841) —— 关键设置：`steps.write_bitstream.args.bin_file = 1`，告诉 `impl_1` 的 `write_bitstream` 步骤额外产出 `.bin`（否则只产出 `.bit`，不便烧录）。

#### 4.1.4 代码实践

**实践目标**：弄清「工程生成」到底往哪个目录里塞了哪些东西，并验证顶层模块与芯片型号。

**操作步骤**（仅源码阅读，无需安装 Vivado）：

1. 打开 [PCIeSquirrel/vivado_generate_project.tcl:74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74)，确认工程会建在 `./pcileech_squirrel/` 子目录、芯片是 `xc7a35tfgg484-2`。
2. 在 [L108-L121](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L108-L121) 中数一下导入的 `.sv/.svh` 源文件个数（应为 11 个），并对照 `PCIeSquirrel/src/` 目录看是否一致。
3. 在 [L838-L841](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L838-L841) 确认 `bin_file` 为 `1`。

**需要观察的现象 / 预期结果**：

- 工程目录名 = 工程名 = `pcileech_squirrel`；芯片锁定 Artix-7 35T。
- 顶层模块 `pcileech_squirrel_top` 同时被设为 `sources_1` 和 `sim_1` 的 top。
- 约束 `pcileech_squirrel.xdc` 属于 `constrs_1`，并被 `synth_1` 与 `impl_1` 共用。

> 待本地验证：若你已装 Vivado，可在 Tcl Shell 里执行 `source vivado_generate_project.tcl -notrace`，结束后在 `PCIeSquirrel/` 下应出现 `pcileech_squirrel/` 子目录和 `pcileech_squirrel.xpr` 工程文件。

#### 4.1.5 小练习与答案

**练习 1**：为什么仓库不直接提交 `.xpr` 工程文件，而要用脚本现场生成？
**答案**：因为 Vivado 工程目录里包含大量自动生成的中间产物和 Xilinx 专有 IP（受授权限制不能随便分发）。只提交源码 + `.tcl` 脚本，既保持仓库干净，也符合 Xilinx 专有 IP 的授权要求——拥有 Vivado WebPACK 的用户可以自行重生成（见 [PCIeSquirrel/readme.md:49-51](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L49-L51)）。

**练习 2**：`upgrade_ip [get_ips *]`（[L593](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L593)）这一行如果删掉，最可能出现什么问题？
**答案**：仓库里的 `.xci` 可能是用更早版本的 Vivado 生成的，直接用新版 Vivado 打开时 IP 处于「锁定/需升级」状态，综合时会报版本不匹配的错误。`upgrade_ip` 把它们升级到当前版本，保证后续综合能通过。

### 4.2 构建脚本 vivado_build.tcl

#### 4.2.1 概念说明

工程生成后，还只是一个「空壳工程」——里面的电路还没被翻译成芯片能认识的 0/1。`vivado_build.tcl` 才是真正「把设计变成比特流」的脚本。它非常短（只有 20 多行），核心是按顺序启动 Vivado 的两个 run：先综合 `synth_1`，再实现 `impl_1`（含写比特流），最后把产物 `.bin` 复制到工程根目录方便取用。

#### 4.2.2 核心流程

`vivado_build.tcl` 的执行顺序（严格按行）：

1. `launch_runs -jobs 4 synth_1` —— **启动**综合（4 线程并发）。
2. `wait_on_run synth_1` —— **阻塞等待**综合结束。
3. `launch_runs -jobs 4 impl_1 -to_step write_bitstream` —— **启动**实现，且只跑到 `write_bitstream` 这一步（也就是产出比特流为止）。
4. `wait_on_run impl_1` —— **阻塞等待**实现结束。
5. `file copy -force .../pcileech_squirrel_top.bin pcileech_squirrel.bin` —— 把 `.bin` 从深层 runs 目录复制到工程根。

关键点：`launch_runs` 是「开火」，立即返回；`wait_on_run` 是「等火灭」，会一直阻塞到该 run 完成。两者必须配对——没有 `wait_on_run`，脚本会立刻往下走而综合还没做完。

#### 4.2.3 源码精读

脚本头注释说明它只能在 Vivado Tcl Shell 里 `source` 运行：

[PCIeSquirrel/vivado_build.tcl:1-3](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L1-L3) —— 注释提示运行方式：`source vivado_build.tcl -notrace`（`-notrace` 表示不回显每条命令）。

**启动并等待综合**：

[PCIeSquirrel/vivado_build.tcl:8](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L8) —— `launch_runs -jobs 4 synth_1`，开 4 个并行 job 跑综合（`-jobs 4` 控制并发度，数字越大越快但也越吃 CPU/内存）。

[PCIeSquirrel/vivado_build.tcl:13](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L13) —— `wait_on_run synth_1`，阻塞直到综合完成。中间的 `puts` 打印「THIS IS LIKELY TO TAKE A VERY LONG TIME」就是提醒用户耐心等。

**启动并等待实现（含写比特流）**：

[PCIeSquirrel/vivado_build.tcl:17](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L17) —— `launch_runs -jobs 4 impl_1 -to_step write_bitstream`，`-to_step write_bitstream` 限定实现流程在「写比特流」这一步停下（实现里后续还有生成报告等步骤，但本工程只需要比特流）。

[PCIeSquirrel/vivado_build.tcl:22](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L22) —— `wait_on_run impl_1`，阻塞直到实现 + 写比特流完成。

**复制产物**：

[PCIeSquirrel/vivado_build.tcl:23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L23) —— `file copy -force ./pcileech_squirrel/pcileech_squirrel.runs/impl_1/pcileech_squirrel_top.bin pcileech_squirrel.bin`，把藏在 `pcileech_squirrel.runs/impl_1/` 深处的 `pcileech_squirrel_top.bin` 复制成工程根目录下的 `pcileech_squirrel.bin`，方便后续烧录工具找到它。

#### 4.2.4 代码实践

**实践目标**：逐条走读 `vivado_build.tcl`，弄清每条命令的作用，并解释「路径过长导致失败」的原因。

**操作步骤**（源码阅读型实践）：

1. 打开 [PCIeSquirrel/vivado_build.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl)，按行号把以下 5 条核心命令逐一填进下表（已给出第一条作示例）：

   | 行号 | 命令 | 作用（用一句话写） |
   | --- | --- | --- |
   | L8 | `launch_runs -jobs 4 synth_1` | 用 4 线程启动综合 run，立即返回不等待 |
   | L13 | | |
   | L17 | | |
   | L22 | | |
   | L23 | | |

2. 阅读路径警告：[PCIeSquirrel/build.md:14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L14)。

3. 思考：为什么「路径过长」会失败？提示——Vivado 实现阶段会在工程目录下生成非常深的临时文件路径（参见产物路径 `pcileech_squirrel.runs/impl_1/...`），而 Windows 默认有路径长度上限。

**需要观察的现象 / 预期结果**：

- 命令顺序必须是「launch → wait → launch → wait → file copy」，不能乱。
- 关于路径过长的解释（参考答案见下方「小练习」第 2 题）：Windows 的路径长度上限约为 260 个字符（MAX_PATH）。工程根目录若本身就很长（如 `C:\Users\xxx\Documents\repos\...\PCIeSquirrel\pcileech_squirrel\pcileech_squirrel.runs\impl_1\...`），再叠上中间文件名后很容易超过 260，Vivado 在 Windows 上就会因找不到/写不了文件而失败。把工程放到 `C:\Temp` 这种短路径下可以缓解。

> 待本地验证：在 Windows 上若真机可跑，把工程放在深目录构建一次、再放到 `C:\Temp` 构建一次，对比是否复现该问题。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 [vivado_build.tcl:13](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L13) 的 `wait_on_run synth_1`，会发生什么？
**答案**：`launch_runs` 立即返回，脚本会马上执行 L17 的 `launch_runs impl_1`。由于 `impl_1` 依赖 `synth_1` 的产出（综合网表），而此时综合还没完成，实现会拿到不完整或旧的网表，导致报错或产出错误结果。`wait_on_run` 的作用就是保证「先综合完，再开始实现」。

**练习 2**：请用一句话解释「路径过长导致构建失败」。
**答案**：Vivado 实现阶段会在工程目录下生成很深的中间文件路径（如 `pcileech_squirrel/pcileech_squirrel.runs/impl_1/...`），在 Windows 上若总路径超过约 260 个字符（MAX_PATH 限制），文件读写会失败，从而导致构建中断；把工程放到 `C:\Temp` 等短路径下可规避（见 [build.md:14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L14)）。

**练习 3**：为什么 [vivado_build.tcl:23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L23) 要用 `-force`？
**答案**：`file copy` 默认在目标已存在时会报错；`-force` 表示覆盖已存在的 `pcileech_squirrel.bin`。这样每次重新构建都能直接覆盖旧文件，避免「上次留下的 .bin」导致脚本中断。

### 4.3 从 synth_1 / impl_1 到 .bin 产物

#### 4.3.1 概念说明

Vivado 把「源码 → 比特流」拆成两个阶段性的 run（运行任务）：

- **`synth_1`（综合）**：把 SystemVerilog 翻译成与工艺无关的逻辑网表（门、触发器、查找表 LUT 等）。
- **`impl_1`（实现）**：把网表映射到具体芯片 `xc7a35tfgg484-2` 的物理资源上（布局 place、布线 route），最后一步 `write_bitstream` 把结果转成可烧录的比特流文件。

二者是**上下游关系**：`impl_1` 的 `parent_run` 就是 `synth_1`（见 [vivado_generate_project.tcl:630](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L630)）。没有综合的产出，实现无从谈起。

#### 4.3.2 核心流程

```
SystemVerilog 源码 (src/*.sv)
        │  synth_1 (综合)
        ▼
   逻辑网表 (.xml/.edif)
        │  impl_1 (布局+布线)
        ▼
   布线后设计 + write_bitstream
        │
        ▼
   pcileech_squirrel_top.bit / .bin
        │  file copy (vivado_build.tcl:23)
        ▼
   pcileech_squirrel.bin  ← 烧录用
```

#### 4.3.3 源码精读

**`.bin` 产物从何而来**：是由 `impl_1` 的 `write_bitstream` 步骤生成的。是否产出 `.bin`（而非默认只有 `.bit`）由生成脚本里的属性控制：

[PCIeSquirrel/vivado_generate_project.tcl:838-841](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L838-L841) —— `steps.write_bitstream.args.bin_file = 1` 表示同时输出原始二进制 `.bin`；`readback_file = 0` 表示不输出回读文件。这一行决定了最终能拿到 `.bin`。

**产物文件名**：`.bin` 以顶层模块命名（`pcileech_squirrel_top.bin`），因为顶层模块就是 `sources_1` 的 top：

[PCIeSquirrel/vivado_generate_project.tcl:174](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L174) —— `set_property -name "top" -value "pcileech_squirrel_top"`，所以比特流文件叫 `pcileech_squirrel_top.bin`。

**默认设备身份**：构建出来的固件，在目标系统上默认显示为「Xilinx Ethernet Adapter，Device ID 0x0666」（详见 u4 单元的身份定制），这是 Vivado PCIe 核的默认 ID：

[PCIeSquirrel/build.md:16](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L16) —— 说明默认设备会显示为 Device ID `0x0666`，并指向后续身份定制章节。

#### 4.3.4 代码实践

**实践目标**：在脚本里追踪 `.bin` 是怎么被「点名产出」并「搬出来」的。

**操作步骤**：

1. 在 [vivado_generate_project.tcl:839](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L839) 找到 `bin_file = 1`，确认实现阶段会产出 `.bin`。
2. 在 [vivado_build.tcl:17](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L17) 确认 `impl_1` 被限制跑到 `write_bitstream` 这一步。
3. 在 [vivado_build.tcl:23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L23) 确认产物被复制为根目录下的 `pcileech_squirrel.bin`。

**预期结果**：能画出一条「`bin_file=1`（生成脚本设定）→ `write_bitstream`（构建脚本触发）→ `file copy`（搬出 .bin）」的因果链。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `bin_file` 从 `1` 改成 `0`（[L839](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L839)），[vivado_build.tcl:23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_build.tcl#L23) 的 `file copy` 会怎样？
**答案**：`write_bitstream` 不再产出 `.bin`，只有 `.bit`。那么 `file copy` 在 `impl_1` 目录下找不到 `pcileech_squirrel_top.bin`，会因源文件不存在而报错。所以这两个文件是配套的——生成脚本决定产出 `.bin`，构建脚本负责搬运它。

### 4.4 不同设备的烧录途径

#### 4.4.1 概念说明

构建产出 `.bin` 之后，还要把它**烧录**进板卡才能使用。烧录与构建是两件独立的事：

- **构建**：在电脑上用 Vivado 把源码变成 `.bin`（前面 4.1–4.3 讲的）。
- **烧录**：用专门的工具把 `.bin` 写进板卡的配置存储，让板卡上电后自动加载这个固件。

不同板卡的烧录硬件接口不同，因此烧录工具也不同。pcileech-fpga 支持两大类：

| 设备 | 烧录工具 | 说明 |
| --- | --- | --- |
| PCIeSquirrel（Screamer PCIe Squirrel） | **OpenOCD**（经板卡内置更新口） | 内置更新口不被 Vivado 直接支持，推荐 Linux 下用 OpenOCD |
| CaptainDMA M2 / 75T / 100T / M2 100T | **CH347 FPGA Tool**（WCH） | 需以管理员权限运行，可能要装 WCH347 驱动 |
| CaptainDMA 4.1th | 参考 PCIeSquirrel 的 OpenOCD 流程 | 但用对应型号的固件 |

此外，大多数设备也提供**预编译固件**（pre-built binary），可以直接下载烧录，免去自己构建的麻烦（见各 readme 的版本历史 / Firmware 表）。

#### 4.4.2 核心流程

**PCIeSquirrel 烧录流程**（OpenOCD）：

1. 自己构建得到 `pcileech_squirrel.bin`，或下载预编译固件。
2. 按板卡厂商（LambdaConcept）Wiki 的 OpenOCD 指南，把 `.bin` 烧进板卡。

**CaptainDMA 烧录流程**（CH347）：

1. 下载对应型号的固件 `.bin`（注意 M2 要区分 x1/x4，型号要对应 FPGA 工程，如 35t325_x1、75t484_x1、100t484_x1）。
2. 下载 [CH347 FPGA Tool](https://github.com/WCHSoftGroup/ch347/releases/tag/CH347_OpenOCD_Release)，必要时安装驱动。
3. 以管理员身份运行 CH347 FPGA Tool 烧录。

#### 4.4.3 源码精读

**PCIeSquirrel 用 OpenOCD，并强调内置更新口不被 Vivado 直接支持**：

[PCIeSquirrel/readme.md:28-32](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L28-L32) —— 说明内置更新口推荐用 OpenOCD（Linux 优先），且板载 JTAG 引脚默认禁用；指向 LambdaConcept Wiki 获取具体步骤。

**CaptainDMA 通用烧录说明（CH347）**：

[CaptainDMA/readme.md:20-21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L20-L21)（M2）和 [CaptainDMA/readme.md:31](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L31)（75T）—— 统一动作：下载 CH347 FPGA Tool 与固件，以管理员身份运行烧录，可能需要安装 WCH347 驱动。

**CaptainDMA 4.1th 走 PCIeSquirrel 流程**：

[CaptainDMA/readme.md:41](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L41) —— 4.1th 的烧录沿用 PCIeSquirrel（OpenOCD）说明，但要用本型号的固件。

**固件与 FPGA 工程的对应关系**：

[CaptainDMA/readme.md:77-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L77-L84) —— Firmware 表里每行都标了对应的 FPGA 工程（如 `35t325_x1`、`75t484_x1`、`100t484_x1`、`35t325_x4`），下载固件必须对应正确设备，否则无法工作。

#### 4.4.4 代码实践

**实践目标**：把「设备—烧录工具—固件工程」三者对应关系梳理清楚。

**操作步骤**：

1. 阅读 [PCIeSquirrel/readme.md:28-32](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L28-L32)，记录 PCIeSquirrel 的烧录工具。
2. 阅读 [CaptainDMA/readme.md:77-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L77-L84)，把 Firmware 表整理成「设备 → 烧录工具 → FPGA 工程」三列。
3. 注意 [CaptainDMA/readme.md:73](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L73) 的提示：M2 x4 是 v4.15（含 x4 专属 bugfix），其余是 v4.14。

**预期结果**：得到一张类似下表的对应关系（自行补全烧录工具列）：

| 设备 | FPGA 工程 | 烧录工具 |
| --- | --- | --- |
| PCIeSquirrel | pcileech_squirrel | OpenOCD |
| CaptainDMA M2 x1 | 35t325_x1 | CH347 |
| CaptainDMA M2 x4 | 35t325_x4 | CH347 |
| CaptainDMA 75T | 75t484_x1 | CH347 |
| CaptainDMA 4.1th | 35t484_x1 | OpenOCD（同 PCIeSquirrel） |
| CaptainDMA 100T / M2 100T | 100t484_x1 | CH347 |

> 待本地验证：上表「烧录工具」列请以 readme 原文为准逐项核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 PCIeSquirrel 的内置更新口「不被 Vivado 直接支持」？
**答案**：Vivado 自带的硬件管理器（Hardware Manager）主要通过其专有的 Xilinx 下载线（如 Platform Cable USB）和标准 JTAG 协议烧录。Screamer 板卡的内置更新口用的是另一套廉价方案（非标准 Xilinx 下载线），Vivado 无法识别它，所以需要用第三方开源工具 OpenOCD 来驱动这个口（见 [readme.md:30](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L30)）。

**练习 2**：把 CaptainDMA 75T 的固件烧到 4.1th 上会发生什么？
**答案**：不会工作。两块板卡虽然都是 Artix-7，但 FPGA 型号/封装/引脚约束不同（75T 对应 `75t484_x1` 工程，4.1th 对应 `35t484_x1` 工程，见 [readme.md:77-84](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/readme.md#L77-L84)）。固件里的引脚约束必须匹配实际板卡硬件，否则 PCIe/USB 等物理接口无法正常工作。

## 5. 综合实践

**任务**：以 PCIeSquirrel 为对象，写出从「拿到源码」到「烧录可用」的完整操作清单，并在关键节点标注对应脚本/文件。

请按以下顺序整理一份操作文档：

1. **环境准备**：安装 Vivado WebPACK 2023.2+，打开 Vivado Tcl Shell。
2. **进入目录**：`cd` 到 `PCIeSquirrel/`（注意用正斜杠）。
3. **生成工程**：执行 `source vivado_generate_project.tcl -notrace`，并说明这一步会创建 `pcileech_squirrel/` 子目录和 `pcileech_squirrel.xpr`。
4. **（可选）定制身份**：若要改 VID/PID/DSN，此时按 [build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) 用 GUI 改 PCIe 核、或改源码里的 `cfg_dsn`。
5. **构建**：执行 `source vivado_build.tcl -notrace`，等待约 1 小时，得到 `pcileech_squirrel.bin`。
6. **排错准备**：若构建失败，按 [build.md:14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L14) 把工程移到短路径（如 `C:\Temp`）重试。
7. **烧录**：用 OpenOCD 把 `.bin` 烧进板卡（参考 [readme.md:28-32](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/readme.md#L28-L32)）。

完成后，你应该能用一句话回答：**「为什么必须先 `generate_project` 再 `build`？两步分别产出什么？」**

> 参考答案：`generate_project` 产出的是一个可被 Vivado 打开的工程（`.xpr` 及源码/IP/约束组织），还没有任何电路实现；`build` 才在这个工程里跑综合 `synth_1` 与实现 `impl_1`，最终产出可烧录的 `pcileech_squirrel.bin`。前者是「搭好厨房」，后者是「做出菜」。

## 6. 本讲小结

- pcileech-fpga 的每个设备目录都通过**两个 Tcl 脚本**完成构建：先 `vivado_generate_project.tcl` 生成工程，再 `vivado_build.tcl` 综合实现出比特流。
- `vivado_generate_project.tcl` 负责：建工程（芯片 `xc7a35tfgg484-2`）→ 导入 11 个源文件 + IP/`.coe` + `.xdc` 约束 → `upgrade_ip` → 创建 `synth_1`/`impl_1` 两个 run，并设定 `bin_file=1`。
- `vivado_build.tcl` 只有 5 条核心命令：`launch_runs synth_1` → `wait_on_run synth_1` → `launch_runs impl_1 -to_step write_bitstream` → `wait_on_run impl_1` → `file copy` 出 `.bin`。`launch`（启动）与 `wait`（等待）必须配对。
- `synth_1`（综合，产出网表）是 `impl_1`（实现，含 `write_bitstream`）的父 run；`.bin` 由 `impl_1` 产出，文件名取自顶层模块 `pcileech_squirrel_top`。
- 构建与烧录是两件事：PCIeSquirrel 用 OpenOCD，CaptainDMA 家族多用 CH347；下载预编译固件可免去自建。固件必须与设备型号/FPGA 工程对应。
- 「路径过长导致失败」是 Windows MAX_PATH（约 260 字符）限制叠加 Vivado 深层 runs 目录造成的，把工程放到 `C:\Temp` 等短路径可缓解。

## 7. 下一步学习建议

到这里，你已经掌握了「怎么把仓库变成一块能用的板卡」。接下来建议：

- **u1-l4 顶层模块架构**：进入 `pcileech_squirrel_top.sv`，看顶层如何把 com/fifo/pcie 三大子系统连起来——这是理解后续所有源码的入口。
- **u2 系统级通信与控制中枢**：理解主机 → FT601 → FIFO → 各子模块的数据流。
- 如果你对**设备身份定制**（VID/PID/DSN/配置空间）更感兴趣，可先跳到 **u4** 单元，但建议先学完 u3 的 PCIe 配置空间基础。

在进入源码之前，如果你手头有 Vivado，强烈建议真机跑一遍 `generate_project`，亲眼看到 `pcileech_squirrel.xpr` 被生成、用 GUI 打开它浏览一下模块层次——这会让后续读源码时脑中有「图」。
