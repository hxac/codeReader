# FPGA 工程构建与 IP 集成

## 1. 本讲目标

上一讲（u1-l3）我们看清了 `hardware/` 里的三类交付物：`BOOT.BIN`、`image.ub`、`system_wrapper.xsa`，以及它们在 ZynqMP 启动链中的角色。但那些交付物是「结果」，本讲要回答的问题是：**这些硬件产物最初是怎么被「造」出来的？** 也就是说，当你拿到一个加密的 TPU IP，怎么把它和 Zynq PS、摄像头接口、时钟、AXI 总线拼成一个能综合、能生成 bitstream 和 xsa 的完整 FPGA 工程。

学完本讲你应当能够：

- 读懂 `script/create_prj.sh` 这一行启动命令，并知道如何在自己的机器上把它跑起来。
- 读懂 `system_rtl_*.tcl` 这个 1900 多行的 TCL 脚本如何「无 GUI」地创建工程、搭出 Block Design、跑综合实现、最终写出 xsa。
- 理解 Xilinx `pragma protect` 加密 IP 的保护机制，以及「黑盒集成」到底黑在哪、能做什么、不能做什么。
- 解释 `constr/top.xdc` 里两条 `set_property` 的含义，以及 TCL 脚本为何要往 xdc 里动态追加一堆引脚约束。

## 2. 前置知识

在进入源码前，先用三段话补齐本讲需要的基础概念。已经熟悉 Vivado 的读者可跳过。

**Vivado 与 TCL 批处理。** Xilinx Vivado 是 Zynq 系列 FPGA 的官方开发工具。它有两种用法：打开图形界面（GUI）用鼠标拖拽，或用 TCL（Tool Command Language）脚本批处理。本项目的脚本走的是第二条路——一条命令进去，工程从无到有全自动生成，不需要点鼠标。`-mode tcl` 就是「只跑 TCL、不弹界面」的意思，适合服务器/命令行环境。

**Block Design（块设计）。** ZynqMP 是「PS + PL」异构芯片：PS 是 ARM 硬核处理器系统（Processing System），PL 是可编程逻辑（FPGA）。在 Vivado 里，把 PS、各种 IP 核、AXI 总线像画电路图一样连起来的那张图，叫 Block Design。本讲里的 TCL 脚本本质上就是在用代码「画」这张图：先 `create_bd_cell` 放一个个器件，再用 `connect_bd_net`/`connect_bd_intf_net` 把线连上，最后 `assign_bd_address` 分配地址。

**AXI 与两条通路。** AXI 是 ARM 的一种总线协议。本工程里 PS 和 PL 之间有两条关键 AXI 通路（u1-l3 已点过）：

- **控制通路（寄存器）**：PS 作为主设备，经 `M_AXI_HPM0_FPD` → AXI 互连 → 各 IP 的 `s00_axi` 从端口，去读写 IP 的控制/状态寄存器。
- **数据通路（DDR 搬运）**：TPU / DVP 作为主设备，经自己的 `M00_AXI`/`M0_AXI` → PS 的 HP（High Performance）口 → 直接访问 DDR 里的张量数据，不经过 CPU。

理解这两条通路，是看懂后面 `assign_bd_address` 那一串地址的关键。

## 3. 本讲源码地图

本讲涉及四个文件，正好对应「启动器 → 工程脚本 → 加密 IP → 约束」四件套：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `script/create_prj.sh` | 1 行 | 启动器：调用 Vivado 以 TCL 模式跑指定脚本 |
| `script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl` | 1929 行 | 工程主体：创建工程、搭 Block Design、综合实现、写出 xsa |
| `ip_repo/EEP_DVP_Top_128B_v6p3.v` | 46726 行 | DVP 摄像头接口顶层 IP（全加密，黑盒） |
| `constr/top.xdc` | 2 行 | 顶层 bitstream 属性约束（引脚约束由 TCL 动态追加） |

一句话关系：`create_prj.sh` 拉起 Vivado 去执行 `system_rtl_*.tcl`；TCL 把 `EEP_DVP_Top_*.v`（加密 IP）和 `top.xdc`（约束）都加进工程，搭好 Block Design，跑完综合实现后写出 `system_wrapper.xsa`——也就是 u1-l3 里那个硬件交付物。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，分别对应四个文件。

### 4.1 create_prj.sh 启动方式

#### 4.1.1 概念说明

整个 FPGA 工程的「入口」不是某个 main 函数，而是一个只有一行的 shell 脚本 `create_prj.sh`。它的职责非常单一：**用批处理方式启动 Vivado，并把一个 TCL 脚本喂给它执行。** 把它单独拎出来讲，是因为初学者常常不知道「这一行到底怎么跑、参数填什么、在哪个目录跑」。

#### 4.1.2 核心流程

```text
用户在 shell 中执行:
  ./create_prj.sh  <某个.tcl>
        │
        ▼
vivado -mode tcl          # 不开 GUI，只接受 TCL 命令
       -source $1         # $1 = 用户传入的 tcl 文件路径
       -nojournal         # 不写 .jou 日志
       -log vivado_create.log  # 把运行日志写到这个文件
        │
        ▼
Vivado 逐行执行 tcl 脚本 → 创建工程 → 综合 → 实现 → 生成 bitstream/xsa
```

注意三个要点：第一，脚本第一段路径 `[Vivado install path]/Vivado/2021.1/bin/vivado` 是**占位符**，使用前必须替换成你机器上 Vivado 2021.1 的真实安装路径。第二，`$1` 是 shell 的第一个位置参数，即你传给脚本的 TCL 文件。第三，TCL 脚本内部用的是 `../hardware`、`../ip_repo`、`../constr` 这种相对路径（见 4.2.3），它们是相对于**当前工作目录**解析的，而不是相对于脚本所在目录——所以通常需要在 `script/` 目录下执行，`../` 才能正确指到仓库的 `hardware`/`ip_repo`/`constr`。具体的调用目录与是否需要 `cd script` 属于本地环境细节，**待本地验证**。

#### 4.1.3 源码精读

脚本全文只有一行（[script/create_prj.sh:1](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/create_prj.sh#L1)）：

```bash
[Vivado install path]/Vivado/2021.1/bin/vivado -mode tcl -source $1 -nojournal -log vivado_create.log
```

四个参数的含义：

- `-mode tcl`：以纯 TCL 模式运行，不弹图形界面。
- `-source $1`：读入并执行 `$1` 指向的 TCL 脚本（本项目即 `system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl`）。
- `-nojournal`：不产生 `.jou` 命令回放日志，保持目录干净。
- `-log vivado_create.log`：把运行过程中的所有消息写到 `vivado_create.log`，这是事后排错最重要的文件——综合/实现失败时第一件事就是看这个 log。

#### 4.1.4 代码实践

**实践目标**：在不真正安装 Vivado 的前提下，把这一行命令「读透」，准备好真实环境下的调用方式。

**操作步骤**：

1. 打开 `script/create_prj.sh`，确认 `[Vivado install path]` 是占位符。
2. 假设你的 Vivado 装在 `/tools/Xilinx/Vivado/2021.1`，把这一行改写成你会在终端里敲的完整命令（写在纸上即可，不要改源码）：
   ```bash
   cd script
   /tools/Xilinx/Vivado/2021.1/bin/vivado -mode tcl \
       -source system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl \
       -nojournal -log vivado_create.log
   ```
3. 思考：为什么这里要先 `cd script`？如果不 cd，`../ip_repo` 会指到哪里？

**需要观察的现象 / 预期结果**：由于本环境无 Vivado，**待本地验证**。在真实环境里，命令跑完后应在 `script/vivado_create.log` 看到完整的综合实现日志，并在 `../hardware/TPU_DVP_prj_N1_DP_2021/` 下生成工程目录与最终的 `system_wrapper.xsa`（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `-mode tcl` 改成 `-mode gui`，行为会有什么不同？

**答案**：会弹出 Vivado 图形界面并加载脚本，可以在 GUI 里逐步观察工程创建过程，但不再是无人值守的批处理；服务器环境通常用不上。

**练习 2**：脚本里没有 `set -e`，如果 Vivado 启动失败，shell 会怎样？

**答案**：因为只是单条命令、没有后续依赖语句，shell 不会中断别的逻辑；是否失败要看 `vivado_create.log` 和命令退出码，所以排错要主动查日志。

---

### 4.2 system_rtl TCL 工程描述

#### 4.2.1 概念说明

`system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl` 是整个工程的「施工图纸」。它由 Vivado 的 Block Design 导出（脚本第 3–7 行的注释说明了这一点），记录了：用哪个 FPGA 芯片、加哪些源文件、Block Design 里放哪些 IP 实例、它们怎么连线、地址怎么分、最后怎么跑综合与实现。文件名里的信息也值得读：`MZU15A` 对应 `xczu15eg` 这颗 ZynqMP 芯片，`TPU_IP_DVP_DP` 说明工程含 TPU IP、DVP 摄像头接口和 DP 显示输出，`v202101` 表示 2021 年 1 月版本（也对应要求的 Vivado 2021.1）。

#### 4.2.2 核心流程

TCL 脚本的整体执行顺序：

```text
1. 变量与版本检查     设置工程名/路径/芯片型号/线程数；校验 Vivado 必须是 2021.1
2. create_project      在 ../hardware/<工程名> 下创建空工程
3. 挂载 IP 仓库        set ip_repo_paths → update_ip_catalog（注册加密 TPU IP）
4. 加源文件            把 top.xdc（约束）和 EEP_DVP_Top_*.v（加密 IP）加进工程
5. 追加引脚约束        用 puts 往 top.xdc 末尾写一堆 DVP/IIC 引脚约束
6. create_root_design  建 Block Design：放 PS、TPU、DVP、时钟、互连、复位等实例并连线
7. assign_bd_address   分配地址段（控制寄存器 + HP 数据通路）
8. validate_bd_design  校验 Block Design
9. 生成顶层            generate_target → make_wrapper（用 system_wrapper.v 做顶层）
10. 综合 synth_1       launch_runs synth_1
11. 实现 impl_1        launch_runs impl_1 -to_step write_bitstream（一直跑到写出 bitstream）
12. write_hw_platform  导出 system_wrapper.xsa（即 u1-l3 的硬件交付物）
```

其中第 6 步是「画电路图」，第 7 步是「定地址」，第 9–12 步是「跑编译并打包」。这三段是本模块的重点。

#### 4.2.3 源码精读

**(a) 工程基本变量与版本检查**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:10-15](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L10-L15)）：

```tcl
set PRJNAME TPU_DVP_prj_N1_DP_2021
set PRJPATH ../hardware
set IP_PATH ../ip_repo
set contrs_PATH ../constr
set PARTNAME "xczu15eg-ffvb1156-2-i"
set JOB_NUM 8
```

`xczu15eg-ffvb1156-2-i` 是 Zynq UltraScale+ 的一颗具体型号（封装 fvb1156、速度等级 -2、工业级）。`JOB_NUM 8` 决定后续综合/实现用 8 个线程并行。注意 `../hardware` 等都是相对路径——这正是 4.1 里强调「要在 `script/` 目录下执行」的原因。

紧接着的版本检查（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:30-38](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L30-L38)）要求 Vivado 必须是 `2021.1`，版本不匹配会直接报错退出。这是加密 IP 的常见要求——加密时用的工具版本和综合时用的版本要对应。

**(b) 创建工程、挂 IP 仓库、加源文件**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:58-70](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L58-L70)）：

```tcl
if { $list_projs eq "" } {
   create_project $PRJNAME $PRJPATH/$PRJNAME -part $PARTNAME
}
set_property  ip_repo_paths  ${IP_PATH}/EEPTPU_M1024_N1_C8_ef16int8_ZU15EG_FOREVAL1h [current_project]
update_ip_catalog
add_files    -fileset constrs_1 -copy_to ... ${contrs_PATH}/top.xdc
add_files -norecurse [ glob  -nocomplain -directory ${IP_PATH} EEP_DVP_Top_128B_v6p3.v]
```

- `ip_repo_paths` 指向 `ip_repo/` 下那个加密 TPU IP 压缩包目录（`EEPTPU_M1024_N1_C8_ef16int8_ZU15EG_FOREVAL1h`，由 `.zip` + `.z01/.z02/.z03` 分卷组成）。文件名里的 `ef16int8` 表示该 IP 支持 FP16 与 INT8，与 README 一致；`M1024/N1/C8` 的精确含义**待确认**，但从命名习惯推测与 MAC 规模、核数、线程数有关。
- `update_ip_catalog` 让 Vivado 解析这个仓库，把里面的 `EEP_TPU` IP 注册进 IP 目录，后面才能实例化它。
- 第 70 行把 DVP 顶层 `EEP_DVP_Top_128B_v6p3.v` 作为普通 Verilog 源文件加进来（注意它是加密的，见 4.3）。

**(c) 动态追加引脚约束**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:76-119](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L76-L119)）。脚本打开工程里那份 `top.xdc` 的副本，用 `puts` 往末尾追加 DVP 摄像头相关的引脚约束，例如：

```tcl
puts $wfid "create_clock -name sdvp_clk -period 10 \[get_ports DVPin_CLOCK\]"
puts $wfid "set_property PACKAGE_PIN N9      \[get_ports DVPin_CLOCK\]"
puts $wfid "set_property IOSTANDARD LVCMOS18 \[get_ports DVPin_CLOCK\]"
...
```

这说明 `top.xdc` 在仓库里只有两条「全局属性」（见 4.4），而**具体的引脚绑定是 TCL 现场写进去的**。`DVPin_CLOCK` 被声明为一个 10ns（100MHz）周期的时钟，DVP 的 8 位数据 `DVPin_DATA[0..7]`、行场同步 `DVPin_HREF/DVPin_VSYNC`、IIC 配置引脚 `IIC_0_scl/sda` 都被绑到 `xczu15eg` 的具体引脚（LVCMOS18 电平）上。最后两行 `set_false_path` 给 `sdvp_clk` 和系统时钟之间打上「假路径」，告诉时序分析器这两个时钟域之间不需要做跨域约束检查。

**(d) Block Design 里的关键 IP 实例**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:304-334](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L304-L334)）：

```tcl
# DVP 摄像头接口顶层（引用 EEP_DVP_Top 模块，来自加密的 .v）
set block_name EEP_DVP_Top
set EEP_DVP_Top_0 [create_bd_cell -type module -reference $block_name $block_cell_name]

# TPU 核（来自加密 IP 仓库）
set EEP_TPU_0 [ create_bd_cell -type ip -vlnv user.org:user:EEP_TPU:1.0 EEP_TPU_0 ]

# 时钟向导：输入 PS 的 pl_clk0(~100M)，输出 250M 与 ~24M
set clk_wiz_0 [ create_bd_cell -type ip -vlnv xilinx.com:ip:clk_wiz:6.0 clk_wiz_0 ]
set_property -dict [ list \
   CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {250} \
   CONFIG.CLKOUT2_REQUESTED_OUT_FREQ {23.99554} \
   ...
] $clk_wiz_0
```

注意两种实例化方式的区别：`EEP_DVP_Top_0` 用 `-type module -reference`，因为它来自一个 `.v` 源文件（虽是加密的）；`EEP_TPU_0` 用 `-vlnv`（Vendor:Library:Name:Version = `user.org:user:EEP_TPU:1.0`），因为它来自 IP 仓库里打包好的 IP。两者都是黑盒，只能通过端口连线，看不到内部。

时钟树（结合 [system_rtl_*.tcl:1867-1875](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L1867-L1875)）是这样的：

- PS 的 `pl_clk0`（约 100MHz）→ 直接喂给 `EEP_TPU_0/clk_100M`（TPU 核时钟）和 `clk_wiz_0` 的输入。
- `clk_wiz_0/clk_out1`（250MHz）→ 250M 复位域，驱动 AXI 互连与各 IP 的 AXI 复位。
- `clk_wiz_0/clk_out2`（约 24MHz）→ `XCLK` 输出引脚，给 DVP 摄像头提供主时钟（OV 系列摄像头典型需要 24MHz XCLK）。

**(e) 两条 AXI 通路的连线与地址分配**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:1852-1890](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L1852-L1890)）：

```tcl
# 数据通路：TPU/DVP 作为主设备，经 HP 口直接访问 DDR
connect_bd_intf_net ... [get_bd_intf_pins EEP_TPU_0/M00_AXI]      [get_bd_intf_pins zynq_ultra_ps_e_0/S_AXI_HP0_FPD]
connect_bd_intf_net ... [get_bd_intf_pins EEP_DVP_Top_0/M0_AXI]   [get_bd_intf_pins zynq_ultra_ps_e_0/S_AXI_HP2_FPD]
# 控制通路：PS 作为主设备，经互连访问 TPU/DVP 的寄存器
connect_bd_intf_net ... [get_bd_intf_pins EEP_TPU_0/s00_axi]      [get_bd_intf_pins ps8_0_axi_periph/M00_AXI]
connect_bd_intf_net ... [get_bd_intf_pins EEP_DVP_Top_0/s00_axi]  [get_bd_intf_pins ps8_0_axi_periph/M01_AXI]
connect_bd_intf_net ... [get_bd_intf_pins ps8_0_axi_periph/S00_AXI] [get_bd_intf_pins zynq_ultra_ps_e_0/M_AXI_HPM0_FPD]

# 地址分配
assign_bd_address -offset 0xA0000000 -range 0x00040000 ... EEP_TPU_0/s00_axi/reg0        # TPU 寄存器 256KB
assign_bd_address -offset 0xA00C0000 -range 0x00040000 ... EEP_DVP_Top_0/s00_axi/reg0    # DVP 寄存器 256KB
assign_bd_address -offset 0x00000000 -range 0x80000000 ... EEP_TPU_0/M00_AXI  HP0_DDR_LOW  # TPU 可 DMA 低 2GB DDR
```

这里和 u1-l3 的结论完全咬合：

- 控制通路：TPU 寄存器落在 `0xA0000000`、DVP 寄存器落在 `0xA00C0000`，各 256KB（`0x40000` = 262144 字节）。两者地址不重叠。
- 数据通路：TPU 经 `HP0`、DVP 经 `HP2`，都能访问 DDR 低 2GB（`0x0`–`0x80000000`）。u1-l3 里提到的 `EEPTPU_MEM_BASE_ADDR=0x31000000` 就落在这 2GB 范围内，所以 TPU 能 DMA 到它。高地址段（`0x800000000`，4GB）被 `exclude_bd_addr_seg` 显式排除，**只有低 2GB 可用**。

**(f) 生成顶层、综合、实现、导出 xsa**（[script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl:1908-1929](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/script/system_rtl_MZU15A_TPU_IP_DVP_DP_v202101.tcl#L1908-L1929)）：

```tcl
generate_target all [get_files .../system.bd]
make_wrapper -files [get_files .../system.bd] -top
add_files -norecurse .../hdl/system_wrapper.v
set_property top system_wrapper [current_fileset]
set_property strategy Performance_Explore [get_runs impl_1]
set_param general.maxThreads $JOB_NUM
...
launch_runs -jobs $JOB_NUM synth_1        ; wait_on_run synth_1
launch_runs impl_1 -to_step write_bitstream -jobs $JOB_NUM ; wait_on_run impl_1
write_hw_platform -fixed -include_bit -force -file $PRJPATH/$PRJNAME/system_wrapper.xsa
```

`make_wrapper` 会基于 Block Design 自动生成一个 `system_wrapper.v` 作为工程顶层（顶层把 BD 的对外端口引出来）。`-to_step write_bitstream` 表示实现流程一直跑到生成 bitstream 为止。最后一行 `write_hw_platform -include_bit` 把 bitstream 和硬件设计一起打包成 `system_wrapper.xsa`——这正是 u1-l3 讲过的那个硬件交付物。实现策略选了 `Performance_Explore`（性能优先），与 README 强调「低延迟」一致。

#### 4.2.4 代码实践

**实践目标**：不跑 Vivado，仅靠阅读 TCL，列出工程里所有 IP 实例名及其作用，并画出两条 AXI 通路的连接关系。

**操作步骤**：

1. 在 `system_rtl_*.tcl` 中搜索所有 `create_bd_cell`（共约 10 处）。
2. 把每个实例名、它的 VLNV 或模块引用、作用填进一张表。
3. 找到 1852–1857 行的四条 `connect_bd_intf_net`，画出「PS HPM0 → 互连 → TPU/DVP 控制端口」和「TPU/DVP → HP0/HP2 → DDR」两组连线。

**预期结果**（参考答案见 4.2.5 第 1 题）：实例应包括 `EEP_DVP_Top_0`、`EEP_TPU_0`、`clk_wiz_0`、`ps8_0_axi_periph`、`rst_clk_wiz_0_250M`、`rst_ps8_0_99M`、`xlconcat_0`、`xlconstant_0/1/2`、`zynq_ultra_ps_e_0`。

#### 4.2.5 小练习与答案

**练习 1**：列出工程中所有 `create_bd_cell` 创建的 IP 实例名，并各用一句话说明作用。

**答案**：

| 实例名 | 作用 |
| --- | --- |
| `zynq_ultra_ps_e_0` | ZynqMP 的 PS（ARM 处理器系统），提供 CPU、DDR 控制器、HP/HPM AXI 口 |
| `EEP_TPU_0` | 加密的 TPU 推理核（数据主设备经 HP0，控制从设备经 s00_axi） |
| `EEP_DVP_Top_0` | DVP 摄像头接口顶层（采集图像、经 HP2 写 DDR，控制经 s00_axi） |
| `clk_wiz_0` | 时钟向导，由 pl_clk0 生成 250M 与 ~24M（XCLK） |
| `ps8_0_axi_periph` | AXI 互连，把 PS 的一个 HPM 主口扇出到 TPU/DVP 两个控制从口（NUM_MI=2） |
| `rst_clk_wiz_0_250M` / `rst_ps8_0_99M` | 复位同步器，分别给 250M 域和 99M 域产生同步复位 |
| `xlconcat_0` | 中断拼接器，把多个 PL 中断拼成一路送 PS 的 `pl_ps_irq0` |
| `xlconstant_0/1/2` | 常量驱动，用来把未用的输入口（如 `DVPout_CLK`）固定到确定电平 |
| `xlconstant_2` | 驱动 `fpga_io_en` 输出引脚（板卡使能信号） |

**练习 2**：为什么 `assign_bd_address` 要给 TPU 的 `M00_AXI` 分配 DDR_LOW（2GB）地址段？这 2GB 是 TPU 的「寄存器」还是「数据」？

**答案**：因为 `M00_AXI` 是 TPU 作为**主设备**去访问 DDR 的数据通路（经 HP0），分配的是 TPU 可 DMA 的 DDR 物理地址范围，不是寄存器。TPU 推理时要不断把输入张量、权重、输出张量在 DDR 和片上之间搬运，所以需要一大段 DDR 地址空间。被 `exclude` 的高 4GB 段说明本工程限制 TPU 只能用低 2GB DDR。

---

### 4.3 加密 IP 与 DVP 顶层

#### 4.3.1 概念说明

`ip_repo/` 里有两类加密交付：一类是打包成 IP 仓库的 `EEPTPU_M1024_...FOREVAL1h`（TPU 核，由 `.zip` + 分卷组成，经 `update_ip_catalog` 注册后用 VLNV 实例化）；另一类是直接以 Verilog 文件形式给出的 `EEP_DVP_Top_128B_v6p3.v`（DVP 摄像头接口顶层）。

后者特别值得单独看：它虽然后缀是 `.v`、虽然是「源文件」，但**整个文件全是加密的**——从第 1 行 `pragma protect begin_protected` 到第 46726 行 `pragma protect end_protected`，中间没有任何可读的 `module EEP_DVP_Top(...)` 端口声明。你唯一能读到的，是开头的保护策略声明。这就是 Xilinx 的 IP 加密机制（`pragma protect`）：用 RSA + AES 把真正的 RTL 加密成一坨 Base64，只有 Vivado 在「生成 bitstream」时才会用 Xilinx 私钥解密；人眼和第三方工具都看不到内部逻辑。

这与 u1-l2 里「核心 IP 以 pragma protect 加密形式黑盒交付，看不到内部 RTL」的说法直接对应。本讲进一步把「黑盒」具体化：黑盒到连端口都要靠 TCL 里的连线去反推。

#### 4.3.2 核心流程

加密 IP 的保护模型可以分成两层：

```text
┌─────────────────────────────────────────────┐
│  commonblock（通用保护策略）                  │
│   - 仿真时不解密（decryption: activity==simulation ? false : true）│
│   - 错误处理/可见性/子模块可见性 都委托给工具  │
├─────────────────────────────────────────────┤
│  toolblock（Xilinx 专用控制项）              │
│   - xilinx_configuration_visible  = false   │ ← 看不到配置
│   - xilinx_enable_modification    = false   │ ← 不能改 RTL
│   - xilinx_enable_probing         = false   │ ← 不能在调试时探测内部网线
│   - xilinx_enable_netlist_export  = false   │ ← 不能导出网表
│   - xilinx_enable_bitstream       = true    │ ← 唯一允许：生成 bitstream
│   - 仿真时不解密                              │
├─────────────────────────────────────────────┤
│  data_block（真正加密的 RTL）                │
│   - data_method = AES128-CBC                 │
│   - encoding = BASE64, bytes = 2661648       │ ← 加密前的 RTL 约 2.6 MB
│   - 由 RSA 分发的 AES 密钥保护                │
└─────────────────────────────────────────────┘
```

关键结论：**这个 IP 只能用来生成 bitstream，不能仿真、不能改、不能探、不能导出网表。** 这就是「黑盒集成」的全部含义——你只能信任它对外暴露的端口名（从 TCL 的 `connect_bd_*` 反推：`s00_axi`/`M0_AXI`/`DVPin_*`/`clk`/`DVPout_CLK` 等），按端口把它连进系统，其余一律不可见。

#### 4.3.3 源码精读

文件头部的保护策略（[ip_repo/EEP_DVP_Top_128B_v6p3.v:1-29](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/ip_repo/EEP_DVP_Top_128B_v6p3.v#L1-L29)）：

```verilog
`pragma protect begin_protected
`pragma protect version = 2
`pragma protect encrypt_agent = "XILINX"
`pragma protect encrypt_agent_info = "Xilinx Encryption Tool 2019.1"
`pragma protect begin_commonblock
`pragma protect control error_handling = "delegated"
`pragma protect control runtime_visibility = "delegated"
`pragma protect control decryption=(activity==simulation) ? "false" : "true"
`pragma protect end_commonblock
`pragma protect begin_toolblock
`pragma protect key_keyowner = "Xilinx", key_keyname= "xilinxt_2017_05", key_method = "rsa", key_block
... (RSA 加密的 AES 密钥，Base64) ...
`pragma protect control xilinx_configuration_visible = "false"
`pragma protect control xilinx_enable_modification = "false"
`pragma protect control xilinx_enable_probing = "false"
`pragma protect control xilinx_enable_netlist_export = "false"
`pragma protect control xilinx_enable_bitstream = "true"
`pragma protect control decryption=(xilinx_activity==simulation) ? "false" : "true"
`pragma protect end_toolblock="..."
`pragma protect data_method = "AES128-CBC"
`pragma protect encoding = (enctype = "BASE64", line_length = 76, bytes = 2661648)
`pragma protect data_block
```

要点逐条解释：

- `encrypt_agent = "XILINX"`：加密代理是 Xilinx 工具，意味着只有 Xilinx 工具链能解密。
- `key_method = "rsa"` + `data_method = "AES128-CBC"`：典型的混合加密——用 RSA 保护 AES 密钥，再用 AES 加密真正的 RTL 数据（`bytes = 2661648` 约 2.6MB）。
- `decryption=(activity==simulation) ? "false" : "true"`：**仿真时不解密**。这意味着你无法对这个 IP 跑行为仿真，只能直接上板验证。
- 五个 `xilinx_enable_*` 控制项把「配置可见/修改/探测/导出网表」全关，只留 `xilinx_enable_bitstream = "true"`。这是 IP 厂商保护核心算法的常规手段。

文件结尾在 [ip_repo/EEP_DVP_Top_128B_v6p3.v:46726](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/ip_repo/EEP_DVP_Top_128B_v6p3.v#L46726) 以 `pragma protect end_protected` 收尾，中间几万行都是 Base64 密文。注意文件名 `EEP_DVP_Top_128B_v6p3.v` 里的模块名是 `EEP_DVP_Top`（去掉后缀），这正是 TCL 第 305 行 `set block_name EEP_DVP_Top` 引用的名字——`create_bd_cell -type module -reference EEP_DVP_Top` 时，Vivado 靠这个名字在加进来的 `.v` 文件里找到模块（即便内容加密，模块名这一层元数据对工具仍可见）。

#### 4.3.4 代码实践

**实践目标**：通过阅读 `pragma protect` 控制项，量化「黑盒」到底禁止了哪些操作。

**操作步骤**：

1. 打开 `ip_repo/EEP_DVP_Top_128B_v6p3.v` 的前 30 行。
2. 把所有 `xilinx_enable_*` / `xilinx_configuration_visible` 控制项列成一张「允许/禁止」表。
3. 找到 `decryption=(...==simulation) ? ...` 这一行，回答：能否对这个 IP 跑仿真？

**预期结果**：

| 控制项 | 取值 | 含义 |
| --- | --- | --- |
| `xilinx_configuration_visible` | false | 禁止查看 IP 配置参数 |
| `xilinx_enable_modification` | false | 禁止修改 RTL |
| `xilinx_enable_probing` | false | 禁止调试时探测内部信号 |
| `xilinx_enable_netlist_export` | false | 禁止导出网表 |
| `xilinx_enable_bitstream` | true | 允许生成 bitstream |
| `decryption` (simulation) | false | 仿真时不解密 → **不能仿真** |

#### 4.3.5 小练习与答案

**练习 1**：既然 `EEP_DVP_Top_*.v` 是加密的、看不到端口声明，TCL 脚本是怎么知道要连哪些端口的？

**答案**：端口信息来自 IP 厂商随 IP 一起提供的接口定义（端口名是工具可见的元数据，即使 RTL 加密）。TCL 里 `connect_bd_net [get_bd_pins EEP_DVP_Top_0/DVPin_CLOCK]` 等命令正是按这些已知端口名去连线。换言之，「黑盒」黑的是实现逻辑，不是接口契约。

**练习 2**：为什么 IP 厂商要单独把 `xilinx_enable_probing = "false"` 关掉？这对用户调试有什么影响？

**答案**：探测（probing）允许在 FPGA 调试时把内部网线接到 ILA 逻辑分析仪上观察波形。关掉它意味着用户无法用 Vivado 在线调试工具窥探 DVP/TPU 内部信号，只能从对外端口（如 AXI 寄存器、DDR 数据）间接验证行为。这是防止内部算法被逆向的必要保护，代价是调试门槛变高。

---

### 4.4 xdc 约束属性

#### 4.4.1 概念说明

`constr/top.xdc` 在仓库里只有两行，但它的角色很特别：它既是工程约束的「种子」，又会**被 TCL 脚本在运行时追加内容**（见 4.2.3 的 `puts $wfid ...`）。追加完之后，它才包含完整的引脚绑定、电平标准、时钟定义和时序例外。本模块聚焦于仓库里这两行「全局 bitstream 属性」，因为它们直接影响最终生成的 bitstream 的安全性与体积。

#### 4.4.2 核心流程

两条属性作用于 `[current_design]`（当前整个设计），属于 bitstream 级别的属性，而不是普通的引脚/时序约束：

```text
top.xdc（仓库原始内容，2 行）
  ├─ BITSTREAM.READBACK.SECURITY = level2   → 读回保护，防 bitstream 被从芯片读出
  └─ BITSTREAM.GENERAL.COMPRESS  = true     → 压缩 bitstream，减小体积、加快加载

      ↓ create_prj.sh 跑起来后，TCL 把 top.xdc 复制进工程，
        并用 puts 在末尾追加 DVP 引脚约束（PACKAGE_PIN / IOSTANDARD / create_clock / set_false_path）

top.xdc（工程里最终内容 = 2 行全局属性 + 一堆 DVP 引脚约束）
```

#### 4.4.3 源码精读

仓库里的 `top.xdc` 全文（[constr/top.xdc:1-2](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/constr/top.xdc#L1-L2)）：

```tcl
set_property BITSTREAM.READBACK.SECURITY level2 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS true [current_design]
```

**第一条：读回安全等级 2**（[constr/top.xdc:1](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/constr/top.xdc#L1)）。`BITSTREAM.READBACK.SECURITY` 控制 FPGA 上电配置完成后，是否允许通过 JTAG 把片内配置数据「读回」出来。`level2` 是较高级别的保护——禁止读回，防止有人拿到已烧录的板卡后用 JTAG 把 bitstream 抽出来反抄。这与 4.3 的加密 IP 是**纵深防御**的关系：IP 在源码层已加密，bitstream 在芯片层再用读回保护兜底，即使物理拿到板卡也难以提取设计。

**第二条：bitstream 压缩**（[constr/top.xdc:2](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/constr/top.xdc#L2)）。`BITSTREAM.GENERAL.COMPRESS = true` 让 Vivado 对 bitstream 做行程编码压缩（重复的配置帧合并）。好处有二：bitstream 文件更小（`BOOT.BIN` 也跟着变小），FPGA 上电加载更快（要回写的帧更少）。代价是解压由 FPGA 内部完成，略增一点点配置逻辑开销，通常可忽略。

把这两条和 4.3 的加密放一起看，能看出整个工程的 IP 保护思路：**源码加密（不可见/不可改/不可仿）+ bitstream 读回保护（不可从芯片读出）+ bitstream 压缩（体积小加载快）**，三层叠加。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把本讲规格要求的任务做完——列出工程用到的 IP 实例名，并解释 `top.xdc` 里两条 `set_property` 的含义。

**操作步骤**：

1. 打开 `constr/top.xdc`，确认它只有两条 `set_property`。
2. 在 `system_rtl_*.tcl` 里搜索 `create_bd_cell`，把所有实例名抄下来（参考 4.2.5 第 1 题的表）。
3. 对照 4.4.3 的解释，用自己的话写出两条属性的作用。
4. 进阶：打开 TCL 第 76–119 行，确认这两条属性之后被追加了哪些引脚约束（`DVPin_CLOCK`、`DVPin_DATA[*]`、`IIC_0_scl/sda`、`fpga_io_en`、`XCLK` 等），从而理解「top.xdc 仓库版」与「top.xdc 工程版」的区别。

**需要观察的现象 / 预期结果**：

- 工程用到的 IP 实例名（共约 10 个）：`zynq_ultra_ps_e_0`、`EEP_TPU_0`、`EEP_DVP_Top_0`、`clk_wiz_0`、`ps8_0_axi_periph`、`rst_clk_wiz_0_250M`、`rst_ps8_0_99M`、`xlconcat_0`、`xlconstant_0`、`xlconstant_1`、`xlconstant_2`。
- `BITSTREAM.READBACK.SECURITY level2`：禁止 JTAG 读回已配置的 bitstream，防止设计被从芯片抽取，配合加密 IP 形成纵深防御。
- `BITSTREAM.GENERAL.COMPRESS true`：压缩 bitstream，减小 `BOOT.BIN` 体积、加快上电加载。

#### 4.4.5 小练习与答案

**练习 1**：如果有人担心 `level2` 读回保护会妨碍调试，能不能把它改成 `level0`？会有什么后果？

**答案**：技术上可以改（把 `level2` 改成 `level0` 即允许读回），但会显著削弱 IP 保护——任何人用 JTAG 就能把 bitstream 从板卡上读出来。由于本工程核心是加密 TPU IP，厂商通常不允许放开读回；调试需求一般通过对外端口和软件日志满足，而不是读回 bitstream。

**练习 2**：`top.xdc` 在仓库里只有 2 行，但综合时实际生效的约束远不止 2 行，为什么？

**答案**：因为 TCL 脚本（第 76–119 行）在工程创建阶段用 `puts` 把 DVP 摄像头的引脚绑定、电平标准、时钟定义和 `set_false_path` 时序例外**追加**到了工程里那份 `top.xdc` 副本的末尾。所以仓库里的 2 行是「种子」，工程里实际生效的是「2 行全局属性 + TCL 追加的引脚约束」。

---

## 5. 综合实践

**任务**：从「一行 shell」追到「一个 xsa」，把本讲四个模块串成一条完整的构建链路。

请按顺序回答/绘制：

1. **启动**：写出在真实环境下用 `create_prj.sh` 启动构建的完整命令（替换 Vivado 路径、传入 TCL 文件、注意工作目录）。说明 `-mode tcl`、`-source`、`-log` 三个参数的作用。
2. **工程创建**：TCL 创建的工程名叫什么？放在哪个目录？目标芯片是什么？要求哪个版本的 Vivado？
3. **IP 集成**：工程里挂了哪两类加密 IP？分别用什么方式实例化（`-type module -reference` vs `-vlnv`）？为什么 DVP 用前者、TPU 用后者？
4. **连线与地址**：画出两条 AXI 通路——控制通路（PS → 互连 → TPU/DVP 的 `s00_axi`）和数据通路（TPU/DVP 的 `M0_AXI`/`M00_AXI` → HP2/HP0 → DDR）。标出 TPU 寄存器、DVP 寄存器、TPU 可 DMA 的 DDR 范围三个地址。
5. **加密与约束**：`EEP_DVP_Top_*.v` 的加密策略禁止了哪 5 类操作、只允许哪 1 类？`top.xdc` 两条属性分别管什么？TCL 往 xdc 里追加了哪类约束？
6. **产物**：TCL 最后用哪条命令写出 `system_wrapper.xsa`？这个 xsa 和 u1-l3 讲的 `BOOT.BIN`/`image.ub` 是什么关系？

**参考思路**：第 6 步的 xsa 是「硬件设计 + bitstream」的打包，是给软件/Vitis 用的硬件说明书；`BOOT.BIN` 则是把 FSBL + bitstream + U-Boot 用 bootgen 打包的启动镜像。xsa 里的 bitstream 最终会被 bootgen 抽出来塞进 BOOT.BIN。三者关系是：TCL 生成 xsa → xsa 里的 bitstream 进 BOOT.BIN → BOOT.BIN 上板启动。

## 6. 本讲小结

- `create_prj.sh` 是一行启动器：以 `vivado -mode tcl -source $1` 批处理方式跑 TCL 脚本，`[Vivado install path]` 需替换、日志写 `vivado_create.log`、需在 `script/` 目录下执行以让 `../` 相对路径生效。
- `system_rtl_*.tcl` 是 1929 行的「施工图」：创建工程（`xczu15eg`、Vivado 2021.1）→ 挂加密 IP 仓库 → 加 `top.xdc` 与 `EEP_DVP_Top_*.v` → 搭 Block Design → 分配地址 → 跑综合实现 → `write_hw_platform` 导出 xsa。
- Block Design 的核心实例是 `zynq_ultra_ps_e_0`（PS）、`EEP_TPU_0`（TPU 核，VLNV 实例化）、`EEP_DVP_Top_0`（DVP 顶层，module 引用）、`clk_wiz_0`（100M→250M/24M）、`ps8_0_axi_periph`（AXI 互连）及若干复位/常量辅助单元。
- 两条 AXI 通路：控制通路 PS `M_AXI_HPM0_FPD` → 互连 → TPU/DVP 的 `s00_axi`（TPU 寄存器 `0xA0000000`、DVP 寄存器 `0xA00C0000`，各 256KB）；数据通路 TPU/DVP → `HP0`/`HP2` → DDR 低 2GB（高 4GB 被排除）。
- `EEP_DVP_Top_*.v` 全文 46726 行加密（RSA + AES128-CBC，Base64），`pragma protect` 关闭了配置可见/修改/探测/网表导出/仿真，只允许生成 bitstream——这就是「黑盒集成」的精确含义。
- `top.xdc` 两条全局属性：`READBACK.SECURITY level2`（禁止 JTAG 读回，与加密 IP 纵深防御）+ `GENERAL.COMPRESS true`（压缩 bitstream，减小体积、加快加载）；引脚约束由 TCL 在运行时追加。

## 7. 下一步学习建议

本讲讲清了「硬件工程怎么造、加密 IP 怎么集成、地址怎么分」。接下来有两条自然的下钻方向：

- **走向软件/Linux 路线**：直接进入 u2 单元。建议先读 [u2-l1 SDK 全景](u2-l1-sdk-overview.md)，理解 `libeeptpu_pub` 怎么用本讲定死的地址（`0xA0000000` 控制寄存器、`0x31000000` 数据区）去驱动 TPU；再看 u2-l3 的 classify demo，把「软件写寄存器 → TPU 跑 → 读 DDR 结果」和本讲的 AXI 通路对上。
- **走向裸机路线**：进入 u4 单元。建议先读 [u4-l1 standalone 工程结构](u4-l1-standalone-platform-init.md)，看裸机工程的 `config.h` 里那些寄存器宏（`BASEADDR0`、`STARTUP`、`STATUS`）如何对应本讲的 `0xA0000000` 地址段，再读 u4-l2 的 `tpu_forward`，理解软件如何按「写地址→启动→轮询」的协议驱动 TPU。

无论走哪条路，记住本讲的一个核心结论：**软件里那些「魔法地址」全部由这份 TCL 的 `assign_bd_address` 定死，改硬件地址就要同步改软件宏。** 这个软硬件契约会贯穿后续所有讲义。
