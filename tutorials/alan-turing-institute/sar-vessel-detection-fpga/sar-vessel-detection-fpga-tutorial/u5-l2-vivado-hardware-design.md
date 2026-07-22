# Vivado 硬件设计与 XSA 导出

## 1. 本讲目标

上一篇 u5-l1 我们建立了「KV260 是 PS+PL、DPU 跑在 PL 上、BRAM 是瓶颈资源」的硬件认知。但那只是**结果**——这颗 DPU 设计是怎么从一行行代码变成可以下载进 FPGA 的比特流的？本讲回答「**硬件是怎么造出来、怎么交接给软件的**」。

读完本讲，你应当能够：

1. 读懂 `main.tcl` 如何用**一条批处理命令**驱动 Vivado 走完「建工程 → 连 DPU → 综合 → 实现 → 写比特流」全流程，而不依赖 GUI 点鼠标。
2. 理解 `kv260.xdc` 约束文件的三类语句：`BITSTREAM` 压缩、`PACKAGE_PIN` 引脚绑定、`IOSTANDARD/SLEW/DRIVE` 电气属性，以及为什么 KV260 的 xdc 这么短。
3. 理解 `write_hw_platform` 导出的 `.xsa` 文件为何是**硬件团队到软件团队的唯一交接物**，以及它在下一阶段（PetaLinux、固件 dtbo）被谁消费。

本讲是 advanced 层，但它**不要求你拥有 Vivado 许可证**——所有实践都是「源码阅读型」，通过精读 TCL 与 xdc 理解流程即可。

## 2. 前置知识

在进入源码前，先用通俗语言建立四个概念。

### 2.1 什么是 FPGA 的「硬件设计」

普通 CPU 写代码是「告诉一颗固定造好的芯片做什么」。FPGA（Field Programmable Gate Array）相反——它出厂是一片空白的可编程逻辑（PL），你要先用**硬件描述**把 DPU 这个「神经网络加速器电路」画进去，FPGA 才具备运行神经网络的能力。这个「画电路」的过程就是**硬件设计**，工具是 AMD/Xilinx 的 Vivado。

设计画完后，Vivado 把它编译成一片二进制**比特流（bitstream，`.bit`）**，开机时下载进 FPGA，电路才真正「通电生效」。

### 2.2 TCL：Vivado 的「命令行语言」

Vivado 既带图形界面（GUI），也能跑脚本。脚本语言是 **TCL（Tool Command Language）**。GUI 上每一次点击，背后都对应一条 TCL 命令。把所有命令写进一个 `.tcl` 文件，就能用

```bash
vivado -mode batch -source main.tcl
```

让 Vivado 在「批处理模式」（batch）下无界面地执行整段流程。好处是**完全可复现**：换台机器、过两年、换个工程师，只要 Vivado 版本一致，跑出来的设计一致。这正是本项目把 `main.tcl` 提交进仓库的原因——硬件设计即代码。

### 2.3 块设计（Block Design）与 IP

现代 FPGA 设计很少从门电路画起，而是用**块设计**：把现成的功能模块（称为 **IP，Intellectual Property**）像搭积木一样连起来。本设计的两块核心积木是：

- **Zynq UltraScale+ PS IP**：代表芯片里的 ARM 处理系统（见 u5-l1 的 PS）。
- **DPU IP**（`dpuczdx8g:4.1`）：神经网络加速器核（见 u5-l1 的 PL 侧 DPU）。

`dpu_kv260.tcl` 这个上万行的脚本就是「块设计的源码」——它用 TCL 命令逐个实例化 IP、设参数、连线。`main.tcl` 只负责**总调度**，真正的电路图在 `dpu_kv260.tcl` 里。

### 2.4 xdc：从「逻辑」到「物理」的地图

块设计里的端口（如 `fan_en_b`）只是**逻辑名字**。FPGA 的物理芯片有固定的引脚（pin，如 `A12`）。**xdc（Xilinx Design Constraints）** 约束文件负责把逻辑名映射到物理现实：

- 这个端口连到哪根物理引脚？（`PACKAGE_PIN`）
- 这根引脚用什么电平标准？（`IOSTANDARD`，如 3.3V 的 `LVCMOS33`）
- 信号翻转快慢、驱动能力多大？（`SLEW`/`DRIVE`）

没有 xdc，Vivado 不知道端口该往哪儿接，实现阶段会报错。

> **一句话总结前置知识**：用 TCL（`main.tcl`）调度 Vivado，把 IP（`dpu_kv260.tcl`）连成电路，用 xdc（`kv260.xdc`）把端口钉到物理引脚，最终编译出比特流并打包成 `.xsa` 交接给软件。

## 3. 本讲源码地图

本讲涉及的关键文件，全部在 `platform/kv260/` 下：

| 文件 | 行数 | 作用 |
| :--- | ---: | :--- |
| [README.md](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md) | 266 | 平台构建总文档；本讲关注第 1 节「Hardware Setup (Vivado)」 |
| [hw/main.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl) | 57 | **总调度脚本**：建工程、导入约束、跑综合/实现、导出 XSA |
| [hw/xdc/kv260.xdc](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/kv260.xdc) | 7 | KV260 物理约束：比特流压缩 + 风扇使能引脚 |
| [hw/xdc/zcu102.xdc](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/zcu102.xdc) | 1 | 对照样本：仅一行压缩约束，无引脚 |
| [hw/dpu_kv260.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl) | ~1400 | 块设计源码：实例化 Zynq PS + DPU IP + 时钟，连线 |

> 本讲**只精读** `main.tcl`、`kv260.xdc` 与 `dpu_kv260.tcl` 的关键片段（建工程、DPU IP 参数、时钟），不逐行展开上千行的块设计连线——那是另一讲的体量。

## 4. 核心概念与源码讲解

### 4.1 TCL 批处理构建

#### 4.1.1 概念说明

Vivado 的标准 GUI 流程是：点 New Project → 选板 → Add Sources → Run Synthesis → Run Implementation → Generate Bitstream。每一步都靠人手点，不可复现、易出错。

**批处理构建**把这条流水线写成 TCL 脚本，用一条命令从头跑到尾。它的价值有三：

1. **可复现**：脚本入版本库，任何人同版本 Vivado 跑出一致结果。
2. **可审查**：硬件设计变成可以 diff、可以 code review 的文本。
3. **可自动化**：能塞进 CI，配合后续 PetaLinux 做全自动平台构建。

本项目的 `main.tcl` 就是这样一份「总调度脚本」——它本身不画电路，而是按顺序调用各阶段命令，把真正画电路的 `dpu_kv260.tcl` 串进来。

#### 4.1.2 核心流程

`main.tcl` 的执行可以用下面这段伪代码概括（左侧为阶段，右侧为对应的 Vivado 能力）：

```
1. 读命令行参数      →  board = argv[0]          （决定用哪份 xdc / 块设计）
2. 创建工程          →  create_project           （指定 part 与 board_part）
3. 导入约束          →  import_files xdc         （把 .xdc 加进工程）
4. 注册 IP 仓库      →  update_ip_catalog        （让 Vivado 认识 DPU IP）
5. 构建块设计        →  source dpu_kv260.tcl     （画电路：PS + DPU + 时钟）
6. 校验并生成顶层    →  validate_bd_design + make_wrapper
7. 综合              →  launch_runs synth_1       （把电路翻译成逻辑网表）
8. 实现 + 写比特流   →  launch_runs impl_1 ... write_bitstream（布局布线 + .bit）
9. 导出硬件平台      →  write_hw_platform        （打包 .xsa）
```

其中第 5 步是真正的「硬件内容」，其余都是 Vivado 的通用工程套路。

#### 4.1.3 源码精读

**① 工程创建与板型选择**

[platform/kv260/hw/main.tcl:14-19](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L14-L19) 读取命令行首参为 `board`，并**硬编码**工程 board_part 为 KV260：

```tcl
set board [lindex $argv 0]
set proj_name project_1
set proj_board [get_board_parts "xilinx.com:kv260_som:part0:1.4" -latest_file_version]
create_project -name ${proj_name} -force -dir ./hwflow_project -part [get_property PART_NAME [get_board_parts $proj_board]]
set_property board_part $proj_board [current_project]
```

- `[lindex $argv 0]`：取命令行第一个参数（`vivado ... -tclargs kv260` 中的 `kv260`）。
- `get_board_parts "xilinx.com:kv260_som:part0:1.4"`：指定板定义为 KV260 SOM 1.4 版（对应 README 第 83 行的 board part 标识）。
- `create_project ... -force`：`-force` 表示若目录已存在则覆盖，保证可重复构建。
- `set_property board_part`：把板定义挂到工程上，使后续能引用板级外设（如 PS 预设）。

> **注意一个易踩的细节**：脚本顶部 [main.tcl:1-12](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L1-L12) 有一段被注释掉的「按 board 名校验只能是 zcu104/zcu102」的逻辑。这说明 `main.tcl` 改编自一个**支持多板**的上游脚本（VAI-3.5-ZUP-DPU-TRD）；本项目把校验注释掉、把 `proj_board` 写死成 KV260，但**保留了用 `${board}` 选择 xdc 和块设计脚本的机制**（见下文）。

**② 约束导入与 IP 仓库**

[platform/kv260/hw/main.tcl:26-33](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L26-L33) 根据板名挑约束文件，并刷新 IP 目录：

```tcl
set output {xsa bit}
set xdc_list xdc/${board}.xdc
set ip_repo_path {dpu_ip}
import_files -fileset constrs_1 $xdc_list
set_property ip_repo_paths $ip_repo_path [current_project]
update_ip_catalog
```

- `xdc/${board}.xdc`：传 `kv260` 就用 `xdc/kv260.xdc`，传 `zcu104` 就用 `xdc/zcu104.xdc`（该文件有大量 DDR4 引脚约束）。
- `ip_repo_paths` + `update_ip_catalog`：把本地 `dpu_ip` 目录注册为 IP 仓库，让 Vivado 能找到 `dpuczdx8g` 这个 IP。

**③ 引入块设计并校验**

[platform/kv260/hw/main.tcl:36-42](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L36-L42) 把「画电路」的工作交给板名对应的块设计脚本，并生成顶层 wrapper：

```tcl
source dpu_${board}.tcl
save_bd_design
validate_bd_design
make_wrapper -files [get_files ... ${design_name}.bd] -top
add_files -norecurse .../${design_name}_wrapper.v
```

- `source dpu_${board}.tcl`：传 `kv260` 即执行 `dpu_kv260.tcl`（仓库中实际只提供了这一份块设计脚本）。
- `make_wrapper ... -top`：块设计本身不是合法的顶层模块，Vivado 自动生成一个 `_wrapper.v` 把它包成顶层，才能综合。

**④ 综合、实现、写比特流**

[platform/kv260/hw/main.tcl:45-50](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L45-L50) 是最耗时的两步——综合（synthesis）与实现（implementation）：

```tcl
generate_target all [get_files ... ${design_name}.bd]
set_property synth_checkpoint_mode Hierarchical [get_files ... ${design_name}.bd]
launch_runs synth_1 -jobs 32
wait_on_run synth_1
launch_runs impl_1 -to_step write_bitstream
wait_on_run impl_1
```

- `launch_runs synth_1 -jobs 32`：综合阶段，`-jobs 32` 表示用 32 线程并行（需主机有足够核数）。
- `launch_runs impl_1 -to_step write_bitstream`：实现阶段**一直跑到 `write_bitstream` 这一步**才停，即布局布线后直接产出 `.bit` 比特流（u5-l1 资源利用率表就是这一步的产物）。
- `wait_on_run`：阻塞等待该 run 完成，因为是 batch 模式，没有 GUI 能看进度。

> **待本地验证**：`-jobs 32` 在核数少于 32 的机器上 Vivado 会自动收敛到可用核数，但具体行为以本地 Vivado 2023.2 实测为准。

#### 4.1.4 代码实践

**实践目标**：理清 `main.tcl` 的阶段顺序与 `${board}` 参数的作用范围，能正确写出 KV260 的构建命令。

**操作步骤**：

1. 打开 [platform/kv260/hw/main.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl)，逐行标注每条命令属于 4.1.2 伪代码的哪个阶段。
2. 找出所有引用 `${board}` 的位置（应为第 14、27、36 行附近），回答：`board` 参数影响哪三处？而工程的 `board_part`（第 17 行）受它影响吗？
3. 对照仓库 `hw/` 目录，确认只有 `dpu_kv260.tcl` 一份块设计脚本、`xdc/` 下有 `kv260/zcu102/zcu104` 三份约束。

**需要观察的现象**：

- `board` 参数只决定**约束文件名**和**块设计脚本名**，而工程的物理板型 `proj_board` 被**写死**为 KV260。因此若传 `zcu104`，会出现「工程板型是 KV260、却加载 zcu104 的 xdc 与块设计」的错配——这在仓库当前文件组合下无法成功（因为没有 `dpu_zcu104.tcl`）。

**预期结果**：仓库当前可用的、与文件名自洽的构建命令应为：

```bash
vivado -mode batch -source main.tcl -tclargs kv260
```

（README 第 36 行给出的 `vivado -mode batch -source main.tcl` 无参数形式，与 main.tcl 第 27/36 行依赖 `${board}` 的写法之间存在不一致；**待本地验证**无参数时 Vivado 是否报 `dpu_.tcl` 找不到的错。）

**关于「用 zcu104 生成参考设计」**：README 第 73-75 行记录的

```bash
vivado -mode batch -source main.tcl -tclargs zcu104
```

指的是**上游 VAI-3.5-ZUP-DPU-TRD 原始脚本**（带完整多板校验、含 `dpu_zcu104.tcl`）的用法，用于先生成一份 ZCU104 上的 DPU 参考设计作为基线，再人工迁移到 KV260。仓库里提交的 `main.tcl` 是已经改写、写死 KV260 的版本，二者不是同一份脚本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `launch_runs synth_1` 后必须跟一句 `wait_on_run synth_1`？

> **答案**：`launch_runs` 是**异步**的——它启动 run 后立即返回，不阻塞 TCL 继续往下走。batch 模式下若不 `wait_on_run` 等其完成，TCL 会立刻执行下一句 `launch_runs impl_1`，而实现必须等综合产物就绪，否则会失败或用到旧的网表。`wait_on_run` 显式阻塞，保证阶段顺序。

**练习 2**：`-force`（第 18 行 `create_project ... -force`）去掉会怎样？

> **答案**：去掉 `-force` 后，若 `./hwflow_project` 目录已存在（上一次构建残留），`create_project` 会报错中止，无法覆盖。`-force` 是实现「可重复构建」的关键开关。

---

### 4.2 xdc 约束

#### 4.2.1 概念说明

xdc（Xilinx Design Constraints）是给 Vivado 的「物理实现指令」。块设计描述的是**逻辑电路**——它有端口 `fan_en_b`，但不知道这根线要连到芯片的哪根脚、用什么电压。xdc 把这些物理事实告诉工具。

xdc 里常见的三类指令：

| 类型 | 典型语句 | 作用 |
| :--- | :--- | :--- |
| 比特流属性 | `set_property BITSTREAM.GENERAL.COMPRESS TRUE` | 改变最终 `.bit` 的生成方式（如压缩） |
| 引脚绑定 | `set_property PACKAGE_PIN A12 [...]` | 把逻辑端口钉到物理引脚 |
| 电气属性 | `set_property IOSTANDARD/SLEW/DRIVE [...]` | 设电平标准、翻转速率、驱动强度 |

一个值得思考的问题：为什么本项目的 `kv260.xdc` 只有 7 行，而 `zcu104.xdc` 有上千行 DDR4 引脚约束？答案是——KV260 是 **SOM（System-on-Module）**，PS 侧的 DDR、大部分外设都封在模块内部、由 PS 自己管理，PL 侧真正暴露到载板上的外部引脚极少（本设计只需控制一个风扇使能）。所以 KV260 的 xdc 短，不是因为省事，而是因为「外部 PL 引脚本来就少」。

#### 4.2.2 核心流程

一份 xdc 在 Vivado 流程里的生命周期：

```
main.tcl 第 30 行 import_files  →  xdc 进入工程的 constrs_1 文件集
            ↓
综合 synth_1   →  Vivado 读取 xdc 中的引脚/电平约束，准备布局布线
            ↓
实现 impl_1    →  按 PACKAGE_PIN 把端口连到物理引脚，按 IOSTANDARD 配置 IOB
            ↓
write_bitstream →  按 BITSTREAM 属性（如压缩）生成最终 .bit
```

注意 `BITSTREAM.GENERAL.COMPRESS` 这类属性要在 `write_bitstream` 阶段才生效，而 `PACKAGE_PIN/IOSTANDARD` 在实现阶段就需要。

#### 4.2.3 源码精读

整个 KV260 约束只有 7 行，[platform/kv260/hw/xdc/kv260.xdc:1-7](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/kv260.xdc#L1-L7)：

```xdc
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]

#Fan Speed Enable
set_property PACKAGE_PIN A12 [get_ports {fan_en_b}]
set_property IOSTANDARD LVCMOS33 [get_ports {fan_en_b}]
set_property SLEW SLOW [get_ports {fan_en_b}]
set_property DRIVE 4 [get_ports {fan_en_b}]
```

逐条拆解：

**① 比特流压缩（第 1 行）**

```xdc
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
```

- `[current_design]` 指当前整个设计。
- `BITSTREAM.GENERAL.COMPRESS TRUE`：开启**比特流压缩**。FPGA 配置比特流里有大量重复的填充数据，压缩后显著减小 `.bit` 体积，从而**缩短上电配置时间**、节省存储。对星载/嵌入式场景（u1-l1 的 <10W、存储受限）尤其有用。这正对应本讲主题里点名的「BITSTREAM 压缩」。

**② 风扇使能引脚（第 4-7 行）**

四条语句共同约束同一个端口 `fan_en_b`（风扇使能，低有效，`_b` 后缀表示 active-low）：

| 语句 | 取值 | 含义 |
| :--- | :--- | :--- |
| `PACKAGE_PIN` | `A12` | 绑定到芯片的 A12 引脚（载板上连到风扇控制电路） |
| `IOSTANDARD` | `LVCMOS33` | 低电压 CMOS，3.3V 电平标准（与载板风扇电路匹配） |
| `SLEW` | `SLOW` | 信号翻转速率设为慢，降低边沿速率以**减少 EMI/串扰** |
| `DRIVE` | `4` | 驱动能力 4mA（风扇使能这种慢速控制信号不需要大电流） |

这就是本讲主题点名的「风扇使能引脚」。它揭示一个模式：**即使只驱动一个低速开关，FPGA 的物理引脚也必须显式声明电平、速率、驱动**——这是硬件设计与软件 GPIO 配置的本质不同。

**③ 对照：极简的 zcu102.xdc**

[platform/kv260/hw/xdc/zcu102.xdc:1](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/zcu102.xdc#L1) 只有一行：

```xdc
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
```

zcu102 板的 DPU 参考设计把所有外设都留在 PS DDR 侧，PL 没有需要绑定的外部引脚，因此 xdc 只剩比特流压缩。对比之下，`zcu104.xdc` 有上千行——因为 ZCU104 的参考设计在 PL 侧挂了 DDR4 MIG，每个 DDR 数据/地址线都要逐一绑定引脚与电平（`DIFF_SSTL12` 等）。这组对照能让你直观理解：**xdc 的长短 = PL 侧外部引脚的多少**。

> **关于压缩效果（待本地验证）**：开启 `BITSTREAM.GENERAL.COMPRESS` 后 `.bit` 体积减小的具体比例取决于设计内部重复度，需用本地 Vivado 对比开启/关闭两种 `.bit` 大小才能给出确切数字；一般可减 30%–50%，但以实测为准。

#### 4.2.4 代码实践

**实践目标**：从 xdc 读出「端口→物理引脚→电气属性」三件套，并理解为何 KV260 的约束这么少。

**操作步骤**：

1. 打开 [platform/kv260/hw/xdc/kv260.xdc](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/kv260.xdc)，把 4 条 `fan_en_b` 约束填进 4.2.3 的表格。
2. 打开 [platform/kv260/hw/xdc/zcu104.xdc](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/xdc/zcu104.xdc) 前 10 行，找出 `c0_ddr4_dq[*]` 这类端口绑的是哪类物理资源（DDR4 数据线）。
3. 在 `main.tcl` 里定位 xdc 是被哪条命令、在哪个阶段导入工程的（应为第 30 行 `import_files -fileset constrs_1`）。

**需要观察的现象**：

- KV260 的 PL 设计只暴露一个风扇使能端口需要物理约束；其余（DDR、网络、USB）都在 PS/SOM 内部，不进 xdc。
- ZCU104 的 PL 设计因为挂了 DDR4，几乎每根数据线都要一行 `PACKAGE_PIN` + `IOSTANDARD`。

**预期结果**：写出一句话结论——「xdc 行数与 PL 侧外部引脚数量正相关；KV260 作为 SOM 把绝大多数外设收进 PS，故 PL 约束极简。」

#### 4.2.5 小练习与答案

**练习 1**：把 `SLEW SLOW` 改成 `SLEW FAST` 会带来什么好处和坏处？

> **答案**：`FAST` 让信号边沿更陡，能支持更高的翻转频率（好处：时序余量更大）。但陡边沿的高频分量更多，会**加剧 EMI 与相邻走线串扰**（坏处）。风扇使能是慢速、一次性翻转的控制信号，对速率无要求，所以选 `SLOW` 牺牲无关紧要的速率换取更干净的信号。

**练习 2**：为什么 `BITSTREAM.GENERAL.COMPRESS` 不针对某个 `get_ports`，而是针对 `[current_design]`？

> **答案**：比特流压缩是**整个配置比特流整体**的属性（对内部重复模式做压缩编码），不属于任何单个引脚或端口；而 `PACKAGE_PIN` 之类是逐端口的。所以压缩属性挂在 `[current_design]`（整个设计）这一级。

---

### 4.3 XSA 导出

#### 4.3.1 概念说明

走到这一步，硬件设计已经编译出比特流 `.bit`。但软件团队（做 PetaLinux 的人）不能只用 `.bit`——他们还需要知道：PS 怎么配置、PL 里挂了哪些地址空间、外设接在哪里、时钟频率多少。这些信息都在**硬件平台文件 `.xsa`（Xilinx Support Archive）**里。

`.xsa` 是一个打包文件，把以下内容**封装成一个交接物**：

- 比特流 `.bit`（PL 电路）
- PS 配置（Zynq 的 DDR 地址、外设、时钟预设）
- 地址映射（PS 经哪些地址访问 PL 的 DPU）
- 块设计的元数据

> **核心结论**：`.xsa` 是**硬件团队到软件团队的唯一正式交接物**。下一讲 u5-l3 的 PetaLinux 用它生成设备树与驱动配置；本平台的固件制作（README 第 3.2 节）也用 `createdts -hw xxx.xsa` 从它生成设备树 overlay。没有 `.xsa`，软件侧无法知道硬件长什么样。

#### 4.3.2 核心流程

XSA 的产出与下游消费链路：

```
main.tcl 第 53 行 write_hw_platform ... -include_bit
            ↓
        产出 design_name.xsa   （硬件 → 软件 交接物）
            ↓
    ┌───────────────┴───────────────┐
    ↓                               ↓
PetaLinux（u5-l3）            固件 dtbo（README 3.2）
读 .xsa 生成设备树/驱动        createdts -hw xxx.xsa
    ↓                               ↓
  Linux 镜像                    kv260.dtbo
```

`write_hw_platform` 是 XSA 导出的唯一命令，其关键开关 `-include_bit` 决定是否把比特流打进 XSA。

#### 4.3.3 源码精读

XSA 导出在 `main.tcl` 的最后一条实质命令，[platform/kv260/hw/main.tcl:53](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl#L53)：

```tcl
write_hw_platform -fixed -force -include_bit -file ./$output_dir/${design_name}.xsa
```

逐个开关拆解：

| 开关 | 含义 |
| :--- | :--- |
| `-fixed` | 导出**固定（不可再编辑）**的硬件平台，符合交接/发布的规范形态 |
| `-force` | 若 `.xsa` 已存在则覆盖，保证可重复构建 |
| `-include_bit` | **把比特流 `.bit` 一起打进 XSA**——这是关键，否则下游拿到的是「无电路的平台空壳」 |
| `-file .../${design_name}.xsa` | 输出文件名，`design_name` 来自 `dpu_kv260.tcl`（第 59 行 `set design_name project_1`），故产物为 `project_1.xsa` |

仓库里实际提交了这份产物 [hw/project_1.xsa](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/project_1.xsa)（约 5.5 MB）和对应的比特流 [hw/project_1.bit](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/project_1.bit)，可直接用于下游验证，不必本地重跑 Vivado。

**XSA 在 README 中的交接角色**：README 第 101 行明确写道，生成比特流后要「export the **Hardware Platform (`.xsa` file)**. This file is the crucial handoff from the hardware team to the software team.」而 README 第 176-179 行的固件制作命令 `createdts -hw /path/to/your/project.xsa ...` 正是 `.xsa` 的直接下游消费者——它从 XSA 解析出 PL 的设备节点，生成设备树 overlay（`.dtbo`，见 u5-l4）。这闭合了「硬件导出 → 软件消费」的交接环。

> **设计名一致性**：`design_name` 在 `main.tcl` 第 41-53 行反复出现，它由 `dpu_kv260.tcl` 第 59 行 `set design_name project_1` 定义。`make_wrapper`、`write_hw_platform` 都用这个名字定位文件，故块设计脚本里的 `design_name` 必须与 `main.tcl` 的预期一致，否则导出找不到 `.bd`。

#### 4.3.4 代码实践

**实践目标**：追踪 XSA 从「哪条命令产出」到「哪条下游命令消费」的完整链路。

**操作步骤**：

1. 在 [main.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/main.tcl) 中找到导出 XSA 的行（第 53 行），记下四个开关。
2. 在 [dpu_kv260.tcl:52-59](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L52-L59) 找到 `design_name` 的定义（`project_1`）与 part（`xck26-sfvc784-2LV-c`），确认产物文件名就是 `project_1.xsa`。
3. 在 [README.md:176-179](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/README.md#L176-L179) 找到 `createdts -hw ... .xsa`，确认 XSA 被固件制作步骤消费。

**需要观察的现象**：

- XSA 导出语句必须带 `-include_bit`；若去掉，下游 PetaLinux 拿到的平台不含 PL 电路，DPU 无法被加载。
- 仓库已提交 `project_1.xsa`（约 5.5 MB）与 `project_1.bit`，可作为下游实践的现成交接物。

**预期结果**：画出一条链路图：`dpu_kv260.tcl(design_name=project_1)` → `main.tcl:53 write_hw_platform -include_bit` → `project_1.xsa` → `README createdts -hw project_1.xsa` → `kv260.dtbo`（u5-l4）。

> **待本地验证**：仓库提交的 `project_1.xsa` 是否能直接被本地 PetaLinux 2023.2 / XSCT `createdts` 成功解析，取决于本地工具版本是否与构建时一致（2023.2）；跨版本可能需要重新导出。

#### 4.3.5 小练习与答案

**练习 1**：去掉 `-include_bit` 会怎样？什么场景下可以去掉？

> **答案**：去掉后导出的 XSA 不含比特流，只有 PS 配置与地址映射。下游若只需做**纯 PS 侧的软件开发**（如先不加载 DPU、只跑 ARM Linux），可用不含比特流的 XSA 加快迭代（无需等实现完成）。但本项目的目标是在 PL 上跑 DPU，所以**必须**带 `-include_bit`。

**练习 2**：为什么 XSA 用 `-fixed` 而不是可编辑形态导出作为交接物？

> **答案**：`-fixed` 产出不可再编辑的固定平台，保证软件团队拿到的是**与硬件实现完全一致、不会被意外改动**的快照。可编辑形态适合硬件团队内部迭代，但不适合跨团队交接——固定形态是「发布」的规范。

---

## 5. 综合实践

把三个模块串起来，完成一次「**只读源码、画出完整硬件构建与交接链路**」的综合作业。

**任务**：假设你是新加入的硬件工程师，需要向软件团队解释「`.xsa` 是怎么来的、里面有什么、下游怎么用」。请基于本仓库真实文件完成下面四件事。

1. **流程图**：用文本框图画出从 `vivado -mode batch -source main.tcl -tclargs kv260` 到产出 `project_1.xsa` 的完整阶段，标注每个阶段对应的 `main.tcl` 行号（建工程 18、导入约束 30、IP 仓库 32-33、块设计 36、综合 47、实现+比特流 49、导出 53）。

2. **约束清单**：列出 `kv260.xdc` 全部 7 行约束，按「比特流属性 / 引脚绑定 / 电气属性」三类归类，并解释为什么 ZCU104 的 xdc 比它长两个数量级。

3. **参数核对**：打开 [dpu_kv260.tcl:1265-1294](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl#L1265-L1294)，确认 DPU IP 关键参数与 u5-l1 的一致性：
   - DPU IP 版本：`dpuczdx8g:4.1`（第 1266 行）
   - softmax 使能：`CONFIG.SFM_ENA {1}`（第 1269 行）
   - URAM 数：`CONFIG.URAM_N_USER {40}`（第 1275 行）
   - DPU 时钟：`CLKOUT2_REQUESTED_OUT_FREQ {325}`（第 1294 行，对应 u5-l1 的 325 MHz 与 `xdputil query` 输出）
   
   验证这些参数与 u5-l1 讲义、README `xdputil query` 输出三处自洽。

4. **交接说明**：写一段 150 字以内的交接说明，告诉软件团队：`.xsa` 由 `main.tcl` 第 53 行 `write_hw_platform -include_bit` 产出，文件名 `project_1.xsa`，他们应在本平台 README 第 176 行 `createdts -hw` 与 u5-l3 的 PetaLinux 流程中消费它。

**预期结果**：一份能把「TCL 调度 → xdc 约束 → XSA 交接」三件事讲清楚、且每一处都带行号与永久链接的文档。如果某一步无法在本地用 Vivado 实跑（多数读者无许可证），请明确标注「待本地验证」，不要假装已运行。

## 6. 本讲小结

- **TCL 批处理构建**：`vivado -mode batch -source main.tcl` 把「建工程 → 导入约束 → IP 仓库 → 块设计 → 综合 → 实现+比特流 → 导出 XSA」全流程脚本化，硬件设计即代码、可复现可审查。
- **`${board}` 参数的作用范围**：它只决定 `xdc/${board}.xdc` 与 `dpu_${board}.tcl` 的文件名，而工程 `board_part` 已被写死为 `xilinx.com:kv260_som:part0:1.4`；仓库当前自洽的调用是 `-tclargs kv260`。
- **xdc 三类约束**：`BITSTREAM.GENERAL.COMPRESS`（比特流压缩）、`PACKAGE_PIN`（引脚绑定）、`IOSTANDARD/SLEW/DRIVE`（电气属性）；KV260 的 xdc 极短是因为 SOM 把绝大多数外设收进了 PS 侧。
- **风扇使能引脚**：`fan_en_b` → A12 / LVCMOS33 / SLEW SLOW / DRIVE 4，是 KV260 PL 侧少数需要物理约束的外部端口。
- **XSA 是交接物**：`write_hw_platform -fixed -force -include_bit` 把比特流 + PS 配置 + 地址映射打包成 `project_1.xsa`，是硬件→软件（PetaLinux、`createdts` 固件）的唯一正式交接物。
- **承接关系**：本讲产出的 `.xsa` 会被 u5-l3（PetaLinux 镜像）与 u5-l4（固件 dtbo 制作）直接消费，是平台侧「硬件 → 软件」流水线的衔接点。

## 7. 下一步学习建议

本讲完成了「硬件设计 → XSA」这一段，接下来顺着交接物往下走：

1. **u5-l3 PetaLinux 软件镜像构建**：看软件团队如何消费 `.xsa`，用 `helper_build_bsp.sh` 构建 BSP、`petalinux-config -c kernel` 启用 DPU 驱动、`petalinux-build` 产出 `BOOT.BIN/image.ub`，以及 rootfs 过大的排错。
2. **u5-l4 固件制作与板载部署**：看 `.xsa` 如何经 `createdts -hw` 变成 `kv260.dtbo`，连同 `.bit.bin`、`shell.json` 组成「固件三件套」，用 `xmutil loadapp` 加载、`xdputil query` 验证 DPU。
3. **源码延伸阅读**：若想理解 DPU IP 的完整连线，可通读 [dpu_kv260.tcl](https://github.com/alan-turing-institute/sar-vessel-detection-fpga/blob/a318ec963da6007330fc2d80cd27b3060dd1cee3/platform/kv260/hw/dpu_kv260.tcl)，重点关注 Zynq PS 预设、DPU 的 `M_AXI` 接口与 PS `S_AXI_HP` 端口如何对接（这是 u5-l1 所讲「PS 与 PL 经 AXI 共享 DDR」的电路级实现）。
