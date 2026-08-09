# 移植工程到新载板

## 1. 本讲目标

ADI HDL 仓库为每块评估板（FMC 子卡）只挑选了几块「热门且够强」的载板做官方支持，但仓库从设计之初就把「可移植」当一等公民。本讲要回答的问题是：**当你手里有一块 ADI 没有官方支持的载板（比如自研的 ZynqMP 板），怎么把一个现成的 FMC 评估板工程跑起来？**

学完后你应当掌握：

1. 移植前必须做的 **FMC 兼容性检查**（电源、VADJ、时钟专用引脚、收发器线），并理解 LPC/HPC/FMC+ 三种连接器的向下兼容规则。
2. 制作一块新载板 **base design（载板基设计）** 需要哪些文件、要改哪些脚本，以及 `sys_zynq`、`CACHE_COHERENCY` 等全局变量的含义。
3. 在 **系统层（第三层）** 怎么复制现有工程、改 `system_bd.tcl` 的两层 source、生成 FMC 引脚约束、并修复 `system_top.v`。

本讲是高级（advanced）内容，承接 u2-l1（三层工程架构）与 u3-l4（`adi_board.tcl` 连线原语）。它会反复用到「载板层与评估板层分离」这一核心思想——这正是移植得以低成本完成的根本原因。

## 2. 前置知识

在动手之前，请先确认你理解下面这些概念（若不熟，可回到对应讲义复习）：

- **三层架构**：每个参考设计逻辑上分为 *载板基设计*（carrier-dependent，`projects/common/$CARRIER`）、*评估板基设计*（carrier-independent，`projects/$EVAL/common`）、*系统特化设计*（`projects/$EVAL/$CARRIER`）。`system_bd.tcl` 先 source 载板层、再 source 评估板层（见 u2-l1）。
- **`adi_board.tcl` 原语**：`ad_ip_instance`、`ad_connect`、`ad_cpu_interconnect`、`ad_cpu_interrupt` 等是 ADI 的语义化 Tcl DSL，base design 几乎全靠它们搭建（见 u3-l4）。
- **`sys_zynq` 架构标识**：0=7 Series FPGA、1=Zynq-7000、2=Zynq UltraScale+ MPSoC、3=Versal。它决定 PS 端口选择（HP/HPC）与地址平移量。
- **FMC**：FPGA Mezzanine Card，ANSI/VITA 57.1/57.4 标准定义的子卡连接器，是 ADI 评估板与载板之间的物理接口。
- **VADJ**：载板向 FMC 子卡提供的「可调电压」，每块子卡都有自己的 VADJ 要求，是兼容性检查里最常踩坑的一项。
- **Vivado 工程五件套**：`Makefile` / `system_project.tcl` / `system_bd.tcl` / `system_constr.xdc` / `system_top.v`（见 u2-l2）。

> 通俗类比：ADI HDL 的工程像「主板 + 扩展卡」的台式机。载板基设计是主板 BIOS 与南桥（描述主板有什么处理器、内存、时钟）；评估板基设计是扩展卡驱动（描述子卡上的 ADC/DAC 怎么搬数据）。移植 = 给一块新主板写 BIOS，让同一张扩展卡照样能插上跑。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [docs/user_guide/porting_project.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst) | 官方移植指南，本讲的「总纲」 |
| [docs/user_guide/architecture.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst) | 三层架构与各载板的 FMC/VADJ/收发器能力表 |
| [projects/common/zcu102/zcu102_system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl) | ZCU102 载板基设计（base design）样本 |
| [projects/common/zcu102/system_top.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/system_top.v) | 载板层顶层模板（薄壳） |
| [projects/common/zcu102/zcu102_system_constr.xdc](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_constr.xdc) | 载板层约束（板载 GPIO/时钟）样本 |
| [projects/common/zcu102/zcu102_fmc0_hpc.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt) | ZCU102 的 FMC 引脚映射表（兼容性检查的关键证据） |
| [projects/common/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/system_project.tcl) | 载板模板的建工程脚本 |
| [projects/ad9081_fmca_ebz/zcu102/system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad9081_fmca_ebz/zcu102/system_bd.tcl) | 一个真实工程的系统层 `system_bd.tcl`（演示两层 source 顺序） |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | 按工程名后缀自动选器件的脚本（移植要在此注册新载板） |
| [library/scripts/adi_xilinx_device_info_enc.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_xilinx_device_info_enc.tcl) | 器件编码表（移植要在此补充新芯片信息） |
| [projects/scripts/adi_fmc_constr_generator.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_fmc_constr_generator.tcl) | 自动生成 FMC 引脚约束的脚本 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**FMC 兼容性检查** → **carrier base design 制作** → **系统层 `system_bd.tcl` 与约束改写**。三者恰好对应移植工作的先后顺序。

### 4.1 FMC 兼容性检查

#### 4.1.1 概念说明

为什么需要移植？官方文档开篇就讲清了原因：一个 FMC 评估板只会在少数几块载板上做官方测试，因为「全覆盖」会带来巨大的维护与测试开销。官方的取舍是「用一块足够强、足够普及的载板（如 ZC706、A10SoC）来展示子卡的全部特性」。

但好消息是：所有 ADI 的 FMC 子卡都**严格遵循 ANSI/VITA 57.1/57.4 标准**。因此只要你的新载板也兼容该标准，移植就没有物理层面的障碍——剩下的只是写脚本。

FMC 连接器有三种类型，它们有**向下兼容**的包含关系：

- **LPC**（Low Pin Count，引脚最少）
- **HPC**（High Pin Count，引脚多，向下兼容 LPC）
- **FMC+**（VITA 57.4，引脚最多，向下兼容 HPC/LPC）

兼容规则一句话：**载板的连接器等级 ≥ 子卡需要的连接器等级**。即 HPC 载板能插 LPC 或 HPC 子卡；但 LPC 载板只能插 LPC 子卡。

#### 4.1.2 核心流程

移植前，按官方「快速兼容性检查清单」逐项核对（清单不绝对完整，最终以 VITA 标准为准）：

1. **电源与地线**：3P3V / 3P3VAUX / 12P0V / GND 是否齐备。
2. **VADJ**：载板能否提供子卡所要求的可调电压档位。这是最容易出问题的一项——不同子卡对 VADJ 要求不同，而载板默认 VADJ 各异。
3. **时钟专用引脚（clock capable pins）**：FMC 的专用时钟线（`CLK0_M2C` 等）必须连到 FPGA 上「能收发时钟」的专用引脚，不能随便接普通 IO。
4. **收发器线（transceiver lines）**：高速串行线 `DPx_[M2C|C2M]_[P|N]` 必须连到 FPGA 的 GT 收发器通道，否则 JESD204 等高速链路无法工作。

> 提示：先去 [projects/common](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common) 看看你的载板是否已在支持列表里。若在，可直接跳到「系统层」模块，无需做下面的 base design。

#### 4.1.3 源码精读

官方移植指南把上述清单写在「Quick Compatibility Check」一节，并强调 FMC 标准的保证作用：

[docs/user_guide/porting_project.rst:L27-L78](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L27-L78) —— 注意其中明确列出三种连接器类型（LPC/HPC/FMC+）与四项必查项（电源、VADJ、时钟专用引脚、收发器线）。

兼容性的「物证」就在载板的 FMC 映射表里。以 ZCU102 为例，文件 [projects/common/zcu102/zcu102_fmc0_hpc.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt) 用一张四列表把「FMC 引脚 → FPGA 物理引脚 → FPGA 端口名（含 bank/类型）」一一对应。看前几行就能验证时钟与收发器是否落在正确位置：

[projects/common/zcu102/zcu102_fmc0_hpc.txt:L1-L6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt#L1-L6) —— `FMC0_CLK0_M2C_P/N` 映射到 `IO_L12P_T1U_N10_GC_66`，名字里的 `GC` 表示 **Global Clock** 专用时钟引脚，bank 66。这正是「时钟线接到时钟专用引脚」的硬证据。

[projects/common/zcu102/zcu102_fmc0_hpc.txt:L172-L175](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt#L172-L175) —— `FMC0_GBTCLK0_M2C` 映射到 `MGTREFCLK0P/N_229`，名字里的 `MGTREFCLK` 是收发器参考时钟专用引脚。

[projects/common/zcu102/zcu102_fmc0_hpc.txt:L176-L179](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt#L176-L179) —— `FMC0_DP0_C2M/M2C` 映射到 `MGTHTXP/N` 与 `MGTHRXP/N`，即 GT 收发器通道。

表中大量 `#N/A`（如 `CLK_DIR`、`HA*`、`HB*` 列）表示 ZCU102 这块载板「没把该 FMC 引脚连出来」——移植时如果子卡正好用到某个 `#N/A` 引脚，就说明电气上接不通，必须放弃或换载板。这是兼容性检查的**第一手判据**。

各载板的 FMC 连接器规格与 VADJ 默认值，则在架构文档的「AMD platforms」表里集中给出：

[docs/user_guide/architecture.rst:L402-L407](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L402-L407) —— ZCU102 有两个 HPC 连接器（各 8 GTH @ 16.3 Gbps），VADJ 默认 **1.8V**（可配 1.5V/1.2V，加粗的 `*1.8V` 是默认档）。对照这张表就能判断你的子卡 VADJ 需求是否落在载板可提供的档位内。

#### 4.1.4 代码实践

**实践目标**：用真实文件做一次「纸面兼容性检查」，理解 `#N/A` 与 GC/MGT 引脚的含义。

**操作步骤**：

1. 打开 [projects/common/zcu102/zcu102_fmc0_hpc.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_fmc0_hpc.txt)，统计 `HA*`、`HB*` 两组（FMC 的 HA/HB 是 HPC 才有的额外引脚族）里有多少行是 `#N/A`。
2. 打开 [docs/user_guide/architecture.rst:L332-L419](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L332-L419)，对比 ZCU102（HPC）与 ZC702（LPC）两行的「FMC connector」列。
3. 假设你有一块只需要 LPC 引脚的子卡，判断它能否插在 ZC702 与 ZCU102 上；反过来，一块需要 HPC 全部引脚的子卡能否插在 ZC702 上。

**需要观察的现象 / 预期结果**：

- ZCU102 的 `HA00`~`HA21`、`HB00`~`HB21` 几乎全是 `#N/A`——因为 ZCU102 用的是标准 VITA 57.1 HPC，而 HA/HB 是 FMC+ 扩展引脚，HPC 上本来就没有；这正常。
- 只需 LPC 的子卡：ZC702、ZCU102 都能插（HPC ⊇ LPC）。
- 需要 HPC 全引脚的子卡：只能插 ZCU102，不能插 ZC702（LPC 物理上没有那些引脚）。

> 待本地验证：以上结论基于仓库静态文件得出；真实电气兼容还取决于你板子的原理图，务必交叉核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ADI 不为每块 FMC 子卡在所有载板上都做官方支持？

**参考答案**：组合爆炸。假设有 N 块子卡、M 块载板，全支持需维护 N×M 个工程并在硬件上逐一回归测试，人力与时间成本不可接受。靠「载板/评估板分层 + FMC 标准」，把维护量降为 N+M，因此官方只挑少数「够强够普及」的载板做代表，其余靠移植。

**练习 2**：FMC 时钟线 `CLK0_M2C_P` 为什么必须连到 `GC`（Global Clock）引脚，而不能连到任意普通 IO？

**参考答案**：时钟信号需要进入 FPGA 的全局时钟树（BUFG/MMCM）才能低偏移地驱动整片逻辑。只有 `GC`/`MRCC`/`SRCC` 这类时钟专用引脚能直接接入时钟资源；普通 IO 引脚无法进入时钟树，时钟会无法被综合工具识别或时序崩溃。

---

### 4.2 carrier base design 制作

#### 4.2.1 概念说明

兼容性过了，就进入移植的「重头戏」：为新载板做一份 **base design（载板基设计）**。

回顾三层架构：base design 是**第一层**，与载板**绑死**、**carrier-dependent**，描述「这块板子上有什么处理器、什么内存、哪些外设、哪些时钟」。它描述的是 `system_wrapper` 模块中「与载板相关的那一部分」。它位于 `projects/common/$CARRIER/`，每块载板一份。

base design 的核心职责（来自架构文档）：

- 例化一个嵌入式处理器（软核或硬核 PS7/PS8/Versal/MicroBlaze）；
- 配置运行 Linux 所需的全部外设 IP（SPI、I2C、GPIO、中断控制器等）；
- 做 PS 配置（时钟、地址宽度、电压、DMA 口等）；
- 个别情况下提供 PL 侧 DDR 作为 ADC/DAC offload 内存。

> 关键认知：base design **几乎不碰子卡数据通路**。它只搭好「地基」（处理器 + 时钟 + 中断 + 控制类外设），评估板层（第二层）再在这个地基上盖「数据通路楼层」。这种分离让你换载板时只重写地基，楼层原样复用。

#### 4.2.2 核心流程

为一块 AMD Xilinx 新载板做 base design 的标准步骤（以 ZCU102 为参照）：

1. **建目录**：在 `projects/common/` 下新建以载板名命名的目录。
2. **放四类文件**：`<carrier>_system_bd.tcl`（块设计）、`<carrier>_system_constr.xdc`（IO 约束）、可选的 MIG 配置、其他约束。
3. **在 `adi_project_xilinx.tcl` 注册器件**：用 `regexp` 匹配工程名后缀，设置 `device`/`board`/`sys_zynq`。
4. **在 `adi_xilinx_device_info_enc.tcl` 补充器件编码**：填写 `fpga_technology_list`/`fpga_family_list`/`speed_grade_list`/`dev_package_list`，并在 `adi_device_spec` 里加正则。

对于 **Intel 载板**，base design 文件是 `*_system_assign.tcl` + `*_system_qsys.tcl`；对于 **Lattice 载板**，是 `*_system_constr.pdc` + `*_system_pb.tcl`，注册发生在 `adi_lattice_dev_select.tcl`。三家差异本讲不展开，详见 u7-l3。

#### 4.2.3 源码精读

**(a) 官方步骤说明**

[docs/user_guide/porting_project.rst:L80-L122](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L80-L122) —— 官方列明 ZCU102 目录必须包含的文件，并给出一段要在 `adi_project_xilinx.tcl` 中追加的器件注册代码（见下方代码块）。

[docs/user_guide/porting_project.rst:L144-L172](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L144-L172) —— 定义 `sys_zynq` 四档取值，并列出需在 `adi_xilinx_device_info_enc.tcl` 中填写的四张编码表。

**(b) 真实的 base design 长什么样**

ZCU102 的 base design 是 [zcu102_system_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl)。它结构清晰，可作模板逐段模仿。

第一行设置全局变量，紧接着声明对外的默认端口（SPI、GPIO）：

[projects/common/zcu102/zcu102_system_bd.tcl:L6-L23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L6-L23) —— `set CACHE_COHERENCY true`（HPC 带缓存一致性的 DMA 口，会被评估板层的 `ad_mem_hp*_interconnect` 读到）；用 `create_bd_port` 声明两个 SPI（各 3 个片选）和 95 位 GPIO 的对外方向。这些端口最终会出现在 `system_wrapper` 上，并被 `system_top.v` 接走。

接下来例化 ZynqMP 处理器 `zynq_ultra_ps_e`，用板卡预设并精细配置 PS：

[projects/common/zcu102/zcu102_system_bd.tcl:L27-L60](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L27-L60) —— `apply_bd_automation -config {apply_board_preset 1}` 一键套用 Xilinx 官方板卡预设（DDR/以太网/USB 等都按 ZCU102 原理图配好），再用 `ad_ip_parameter` 逐项微调：启用 `M_AXI_GP2`（32 位 CPU 主口给寄存器）、开三路 PL 时钟（`PL0`=100MHz、`PL1`=250MHz、`PL2`=500MHz）、把 SPI0/SPI1 走 EMIO、使能 `GPIO_EMIO`。

然后定义系统时钟与复位，并把它们导出为全局 Tcl 变量，供评估板层引用：

[projects/common/zcu102/zcu102_system_bd.tcl:L73-L102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L73-L102) —— 用 `ad_connect` 把 PS 的 `pl_clk0/1/2` 命名为 `sys_cpu_clk`/`sys_250m_clk`/`sys_500m_clk`，再经 `proc_sys_reset` 生成同步复位，最后 `set sys_cpu_clk [get_bd_nets sys_cpu_clk]` 等把网络句柄存进变量。**这正是第二层脚本能跨载板复用的关键**：评估板层只认 `sys_cpu_clk`/`sys_dma_clk`/`sys_iodelay_clk` 这些统一名字，不关心它来自哪块载板。

最后铺设中断拼接器，把 16 路 PL 中断汇入 PS：

[projects/common/zcu102/zcu102_system_bd.tcl:L150-L174](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L150-L174) —— 两个 `ilconcat`（各 8 路）拼成 `pl_ps_irq0/1`，先把所有输入默认接 `GND`。评估板层用 `ad_cpu_interrupt` 时会**先断开对应位的 GND 再接入真实中断**（这是 u3-l4 讲过的细节）。注意架构文档提醒：分配中断号前务必查载板的 base design，避免和载板已用的冲突。

**(c) base design 的约束与顶层**

[projects/common/zcu102/zcu102_system_constr.xdc:L9-L34](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_constr.xdc#L9-L34) —— 只约束板载 GPIO（拨码开关、按钮、LED）与 SPI 时钟周期。**注意：FMC 引脚约束不在这里**，它由工程层（第三层）的 `system_constr.xdc` 负责——因为 FMC 引脚归属「评估板 × 载板」组合，不属于纯载板。这是初学者常混淆的点。

[projects/common/zcu102/system_top.v:L38-L67](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/system_top.v#L38-L67) —— 载板层顶层是个「薄壳」：端口只有 `gpio_bd_i`/`gpio_bd_o`，内部例化 `system_wrapper`，并把 95 位 EMIO GPIO 中真正接到板载开关/LED 的那几位重组进去。它会被工程层的 `system_top.v` 进一步扩展（加入 FMC 引脚）。

**(d) 在器件脚本里注册新载板**

移植时必须在 [adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) 里加一段 `regexp` 分支。现有 ZCU102 分支就是模板：

[projects/scripts/adi_project_xilinx.tcl:L107-L110](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L107-L110) —— 当工程名匹配 `_zcu102` 时，设 `device` 为完整 part 号 `xczu9eg-ffvb1156-2-e`，并用 `lsearch` 在已安装板卡库里找到 ZCU102 的 board part。新载板照此复制，把 part 号与匹配串换成你自己的即可。

`sys_zynq` 不是手填的，而是由器件 part 号前缀自动推断：

[projects/scripts/adi_project_xilinx.tcl:L190-L200](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L190-L200) —— `xczu` 前缀 → `sys_zynq=2`（ZynqMP）。所以只要 `device` 填对，`sys_zynq` 会自动正确，下游 `ad_cpu_interconnect` 的地址平移、PS 端口选择都会跟着对。

最后补充器件编码表，让 ADI IP 能在综合期自动得知工艺/封装/速度等级：

[library/scripts/adi_xilinx_device_info_enc.tcl:L30-L84](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_xilinx_device_info_enc.tcl#L30-L84) —— 四张表把字符串名映射成数字编码（如 `ultrascale+ → 3`、`ff → 3`、`-2 → 20`）。新芯片若已存在则**不要重复添加**。

[library/scripts/adi_xilinx_device_info_enc.tcl:L105-L131](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_xilinx_device_info_enc.tcl#L105-L131) —— `adi_device_spec` 用 `switch -regexp` 按 part 号前缀推断 `FPGA_TECHNOLOGY`。新工艺族要在此加一条正则（官方以 ZCU102 的 `^xczu → ultrascale+` 为例）。

#### 4.2.4 代码实践

**实践目标**：为一块假设的新 ZynqMP 载板（命名 `myboard`）起草 base design 的文件骨架与器件注册代码。

**操作步骤**：

1. 复制目录：把 `projects/common/zcu102/` 整体复制为 `projects/common/myboard/`。
2. 改名：`zcu102_system_bd.tcl` → `myboard_system_bd.tcl`，`zcu102_system_constr.xdc` → `myboard_system_constr.xdc`，`zcu102_fmc0_hpc.txt` → `myboard_fmc0_hpc.txt`（FMC 映射按你板子原理图重填）。
3. 在 `adi_project_xilinx.tcl` 现有 `_zcu102` 分支后，仿照格式追加（**示例代码**，part 号按你板子改）：

```tcl
if [regexp "_myboard" $project_name] {
  set device "xczu7ev-ffvc1156-2-e"
  set board "not-applicable"
}
```

> 用 `not-applicable` 表示没有 Vivado 板卡文件（自研板常见），此时 PS 预设需手动配，不能依赖 `apply_board_preset`。

4. 在 `myboard_system_bd.tcl` 里，对照你板子原理图核对三件事：
   - **VADJ**：若子卡需要 1.8V 以外档位，确认载板电源能提供，并在 PS 配置或约束里设置对应 `IOSTANDARD` 的 VCCO bank 电压。
   - **时钟专用引脚**：确认 `CLK0_M2C_P/N` 在你的 `myboard_fmc0_hpc.txt` 里映射到带 `GC`/`MRCC` 的引脚。
   - **PL 时钟频率**：检查 `PL0/PL1/PL2_REF_CTRL__FREQMHZ` 是否仍满足评估板层对 `sys_cpu_clk`(100M)/`sys_dma_clk`(250M)/`sys_iodelay_clk`(500M) 的预期。

**需要观察的现象 / 预期结果**：因为 `device` 仍是 `xczu` 前缀，`sys_zynq` 会自动得到 2，`CACHE_COHERENCY`、`sys_dma_clk` 等变量名不变，评估板层脚本（第二层）**无需任何修改**即可在你新载板上 source。

> 待本地验证：上述文件骨架来自静态复制；实际能否综合取决于你板子原理图与 part 号是否正确。

#### 4.2.5 小练习与答案

**练习 1**：base design 为什么把 `sys_cpu_clk` 等时钟句柄存成全局 Tcl 变量，而不是直接用 PS 端口名？

**参考答案**：为了解耦。评估板层（第二层）需要连接 DMA、寄存器等 IP，但它不应感知「时钟具体来自哪块载板的哪个 PS 口」。只要每块载板的 base design 都导出同名变量 `sys_cpu_clk`/`sys_dma_clk`，评估板层脚本就能原样复用。这是「N+M 维护成本」能成立的关键约定。

**练习 2**：官方移植文档说新自研 ZynqMP 板要「手动 enable all the functions needed」。结合 `apply_bd_automation -config {apply_board_preset 1}`，解释这句话。

**参考答案**：ZCU102 用了 Xilinx 官方板卡预设，一键配好 DDR/以太网/USB 等。自研板没有官方板卡文件（`board="not-applicable"`），无法套预设，所以 DDR 引脚、MIO 配置、PS 电压等所有功能都必须在 `myboard_system_bd.tcl` 里手动逐项 `ad_ip_parameter` 配置。

---

### 4.3 system_bd 与约束改写

#### 4.3.1 概念说明

base design 做好后，第三层（系统层）就轻松了。系统层位于 `projects/$EVAL/$CARRIER/`，入口是 `system_bd.tcl`。它的职责很简单：**先 source 载板 base design，再 source 评估板 base design，最后做该「评估板 × 载板」组合特有的少量微调**。

移植到新载板时，系统层的标准做法是「**从已有工程整套复制，再改载板名**」——因为同一块评估板在不同载板上的系统层脚本结构几乎一致，只是 source 的载板路径与约束不同。

#### 4.3.2 核心流程

系统层移植的典型步骤：

1. **复制现有工程**：从 `projects/$EVAL/$EXISTING_CARRIER/` 复制到 `projects/$EVAL/$NEW_CARRIER/`。
2. **改 `system_bd.tcl`**：把第一处 source 的载板路径换成新载板，第二处评估板路径保持不变。
3. **生成 FMC 引脚约束**：用 `adi_fmc_constr_generator.tcl` 从载板 FMC 映射表 + 评估板 FMC 需求表自动生成 `fmc_constr.xdc`。
4. **改 `system_constr.xdc`**：更新该组合下 FMC 引脚的 `PACKAGE_PIN`/`IOSTANDARD`、板载时钟约束。
5. **修复 `system_top.v`**：最省力的办法是**先让它综合失败**，工具会列出多余/缺失的端口，再据此增删。
6. **改 `Makefile`**：更新载板名（通常自动生成，改 `PROJECT_NAME` 即可）。

#### 4.3.3 源码精读

**(a) 两层 source 的固定模式**

官方在「Project flow」里反复强调：建工程时 `system_bd.tcl` 会被 source，它先 source 载板、再 source 评估板：

[docs/user_guide/porting_project.rst:L174-L214](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L174-L214) —— 注意官方示例用的就是 ad9081_fmca_ebz 这个工程，并展示了带 ADC/DAC FIFO 的三段式 source 写法。

真实工程的 `system_bd.tcl` 长这样：

[projects/ad9081_fmca_ebz/zcu102/system_bd.tcl:L11-L18](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad9081_fmca_ebz/zcu102/system_bd.tcl#L11-L18) —— 第一行 `source .../zcu102/zcu102_system_bd.tcl`（载板层，定义处理器与时钟）；中间两行 source 公共的 ADC/DAC FIFO 脚本；第四行 `source .../ad9081_fmca_ebz/common/ad9081_fmca_ebz_bd.tcl`（评估板层，定义 AD9081 数据通路）。**移植到新载板时，只需把第一行换成 `myboard_system_bd.tcl`，第四行原样不动**——这是分层架构的红利。

顺序绝不能颠倒：载板层必须先跑，因为它定义了 `sys_cpu_clk`、`CACHE_COHERENCY`、`sys_ps8` 实例等，评估板层要消费它们（如 `ad_mem_hp0_interconnect $sys_cpu_clk sys_ps8/S_AXI_HP0`）。

**(b) FMC 约束自动生成**

写 FMC 引脚约束最繁琐，官方提供了生成器。它需要两份输入：

- 载板的 FMC 连接表：`projects/common/$CARRIER/$CARRIER_<fmc_port>.txt`（如 `zcu102_fmc0_hpc.txt`，即 4.1.3 读过的那张表）。
- 评估板的 FMC 需求表：`projects/$EVAL/common/$EVAL_fmc.txt`。

[docs/user_guide/porting_project.rst:L422-L475](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L422-L475) —— 调用方式：在 `projects/$EVAL/$CARRIER` 目录下执行 `tclsh ../../scripts/adi_fmc_constr_generator.tcl fmc0`（单 FMC 口）或带两个端口参数。产物是当前目录的 `fmc_constr.xdc`（AMD）或 `fmc_constr.tcl`（Intel）。若两份输入表不存在，可参照现有例子自制，流程见 [docs/user_guide/porting_project.rst:L475-L509](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L475-L509)。

**(c) system_top 的修复策略**

`system_top.v` 是综合顶层，其端口会接 FPGA 物理引脚。换载板后引脚集变化，最实用的修复法是「让综合报错」：

[docs/user_guide/porting_project.rst:L355-L364](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L355-L364) —— 让综合失败后，工具会精确告诉你哪些端口多余、哪些缺失。第一步永远是核对 `system_wrapper` 的例化——这个文件由工具生成（位于 `<project>.srcs/sources_1/bd/system/hdl/system_wrapper.v`），修正例化通常能消掉大部分错误。

载板层 `system_top.v` 的例化风格可作参照：

[projects/common/zcu102/system_top.v:L55-L67](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/system_top.v#L55-L67) —— 这里把 SPI 端口直接接常量（`spi0_csn = 1'b1`）、把未用的 `gpio_t` 悬空。工程层的 `system_top.v` 会在此基础上把 FMC 数据引脚补进来。

**(d) 工程五件套各自要改什么**

官方「Project files for AMD boards」一节逐文件说明了改动点：

[docs/user_guide/porting_project.rst:L335-L367](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/porting_project.rst#L335-L367) —— `system_project.tcl` 改载板名；`system_bd.tcl` 改 source 的载板路径；`system_constr.xdc` 更新所有 FMC 引脚的物理引脚号；`system_top.v` 按综合报错增删端口；`Makefile` 改载板名。

#### 4.3.4 代码实践

**实践目标**：写出移植到 `myboard` 后，系统层 `system_bd.tcl` 必须保留的两层 source 行，并列出 VADJ、时钟专用引脚等关键检查项。

**操作步骤**：

1. 假设把 `projects/ad9081_fmca_ebz/zcu102/` 复制为 `projects/ad9081_fmca_ebz/myboard/`。
2. 把新工程的 `system_bd.tcl` 改写成（**示例代码**）：

```tcl
## ADC/DAC FIFO depth
set adc_fifo_samples_per_converter [expr 64*1024]
set dac_fifo_samples_per_converter [expr 64*1024]

# 第一层：新载板 base design
source $ad_hdl_dir/projects/common/myboard/myboard_system_bd.tcl
source $ad_hdl_dir/projects/common/xilinx/adcfifo_bd.tcl
source $ad_hdl_dir/projects/common/xilinx/dacfifo_bd.tcl

ad_mem_hp0_interconnect $sys_cpu_clk sys_ps8/S_AXI_HP0

# 第二层：评估板 base design（不变）
source $ad_hdl_dir/projects/ad9081_fmca_ebz/common/ad9081_fmca_ebz_bd.tcl
source $ad_hdl_dir/projects/scripts/adi_pd.tcl
```

3. 对照下面「关键检查项清单」逐项打勾。

**关键检查项清单（综合本讲三个模块）**：

| 检查项 | 在哪里查 | 期望 |
| --- | --- | --- |
| 两层 source 顺序 | 新 `system_bd.tcl` | 载板层在前，评估板层在后 |
| `sys_cpu_clk` 等变量 | `myboard_system_bd.tcl` 末尾 | 必须导出同名变量 |
| VADJ 档位 | 子卡手册 × `myboard_system_bd.tcl` 的 bank VCCO | 子卡需求落在载板可提供档位内 |
| 时钟专用引脚 | `myboard_fmc0_hpc.txt` | `CLK*_M2C` 映射到 `GC`/`MRCC` 引脚 |
| 收发器线 | `myboard_fmc0_hpc.txt` | `DP*` 映射到 `MGTH*`，`GBTCLK*` 映射到 `MGTREFCLK*` |
| `sys_zynq` | `adi_project_xilinx.tcl` 的 device 前缀 | `xczu` 前缀 → 自动为 2 |
| FMC 引脚约束 | `adi_fmc_constr_generator.tcl` 产物 | 生成 `fmc_constr.xdc` 并加入工程 |

**需要观察的现象 / 预期结果**：若 `myboard_system_bd.tcl` 正确导出了 `sys_cpu_clk` 等变量，第二层 source 不会因「变量未定义」报错；FMC 生成器若报告某引脚 `#N/A`，说明该引脚电气上不可用，需在评估板层回避该引脚。

> 待本地验证：能否真正综合取决于你板子原理图与 part 号；本实践为「源码阅读 + 骨架编写」型，未执行实际 `make`。

#### 4.3.5 小练习与答案

**练习 1**：把 `system_bd.tcl` 里载板层与评估板层的 source 顺序对调，会发生什么？

**参考答案**：评估板层会立即报错退出。因为它要调用 `ad_mem_hp0_interconnect $sys_cpu_clk sys_ps8/S_AXI_HP0`、`ad_cpu_interconnect` 等，这些都依赖载板层先定义的 `sys_cpu_clk`/`sys_ps8`/`CACHE_COHERENCY`。先 source 评估板层时这些变量/实例不存在，Tcl 会抛「can't read」错误。

**练习 2**：为什么 FMC 引脚约束不在载板层的 `zcu102_system_constr.xdc` 里，而要放到工程层用生成器产出？

**参考答案**：FMC 引脚的「FMC 信号名 → 该组合实际用到哪些」取决于具体评估板（子卡），而「FMC 信号名 → FPGA 物理引脚」取决于载板。两者是正交维度，只有把「载板映射表」与「评估板需求表」相交才能得到该组合的确切约束，所以必须由工程层动态生成，无法静态写死在任一层。

---

## 5. 综合实践

**任务**：完整规划「把 AD9081 FMC 评估板从 ZCU102 移植到自研 ZynqMP 载板 `myboard`」所需的全部文件改动。

请输出三份产物：

1. **需新建的文件清单**（路径 + 作用）。提示：`projects/common/myboard/` 下四件套 + `projects/ad9081_fmca_ebz/myboard/` 下五件套。
2. **需修改的两个脚本补丁**：
   - `adi_project_xilinx.tcl` 中新增的 `_myboard` regexp 分支（写出完整 Tcl）。
   - `adi_xilinx_device_info_enc.tcl` 中若 `myboard` 用了新工艺族（假设仍是 UltraScale+），说明需不需要改、为什么。
3. **新工程 `system_bd.tcl` 中必须 source 的两层脚本路径**，以及 VADJ、时钟专用引脚、收发器线三项关键检查的核对来源（指明读哪个文件）。

参考思路：

- 新建清单应包含：`myboard_system_bd.tcl`、`myboard_system_constr.xdc`、`myboard_fmc0_hpc.txt`、`system_top.v`、`system_project.tcl`、`Makefile`（载板层）；以及 `ad9081_fmca_ebz/myboard/` 下的 `system_bd.tcl`、`system_project.tcl`、`system_top.v`、`system_constr.xdc`、`timing_constr.xdc`、`Makefile`、`README.md`（工程层）。
- 器件注册分支只需仿照 [adi_project_xilinx.tcl:L107-L110](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L107-L110) 改 part 号与匹配串。
- 若仍是 `xczu` 前缀的 UltraScale+，四张编码表**无需新增**（已存在 `ultrascale+ → 3` 等），`adi_device_spec` 的 `^xczu` 正则也已覆盖；这正是「不要重复添加」原则的体现。
- 三项检查来源：VADJ 看 `architecture.rst` 的平台表 + 子卡手册；时钟/收发器引脚看 `myboard_fmc0_hpc.txt` 的 `GC`/`MGTH`/`MGTREFCLK` 标记。

> 这是设计型实践，不要求实际综合。完成后你应当能独立评估任意「ADI 子卡 + 自研载板」组合的移植可行性。

## 6. 本讲小结

- 移植的可行性根源是 **FMC 标准（VITA 57.1/57.4）** 与 **载板/评估板分层**：前者保证物理兼容，后者把维护量从 N×M 降到 N+M。
- **FMC 兼容性检查**四项必查：电源/地、VADJ、时钟专用引脚（`GC`）、收发器线（`MGTH`/`MGTREFCLK`）；HPC 载板向下兼容 LPC 子卡，反之不行。判据就在载板的 `<carrier>_<fmc>.txt` 映射表里（`#N/A` 表示不可用）。
- **base design** 位于 `projects/common/$CARRIER/`，描述处理器、时钟、中断、控制外设，并导出 `sys_cpu_clk`/`sys_dma_clk`/`CACHE_COHERENCY` 等统一变量名供评估板层复用；移植要新建四件套，并在 `adi_project_xilinx.tcl` 注册器件、按需补充 `adi_xilinx_device_info_enc.tcl`。
- **系统层** `system_bd.tcl` 的铁律是「先 source 载板、再 source 评估板」；移植靠「复制现有工程 + 改载板名」，FMC 引脚约束用 `adi_fmc_constr_generator.tcl` 自动生成，`system_top.v` 用「让综合报错」的反馈法修复。
- `sys_zynq` 由器件 part 号前缀自动推断，无需手填；`board="not-applicable"` 的自研板无法套用官方板卡预设，PS 配置需全手动。
- 三家厂商 base design 文件命名不同（Xilinx `*_system_bd.tcl` / Intel `*_system_qsys.tcl`+`*_system_assign.tcl` / Lattice `*_system_pb.tcl`+`*.pdc`），注册脚本也不同。

## 7. 下一步学习建议

- **u7-l2（创建与定制新工程）**：本讲聚焦「换载板」；若你要从零新建一块全新评估板的工程（含 README 模板、CFG 参数化机制），接 u7-l2。
- **u7-l3（多厂商构建）**：本讲的 Intel/Lattice base design 只点到为止；要把工程真正在 Quartus/Radiant 上跑起来，看 u7-l3 对 `project-intel.mk`/`project-lattice.mk` 与 `system_qsys.tcl`/`system_pb.tcl` 的拆解。
- **延伸阅读源码**：精读 [projects/common/vck190/](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common) 下的 Versal base design，对照 ZCU102 观察 `sys_zynq=3` 时 PS（`versal_cips`）与地址平移（`+0x6000_0000`）的差异，巩固「同一套评估板层脚本跨架构复用」的理解。
