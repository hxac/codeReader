# Intel 与 Lattice 的 IP 打包

## 1. 本讲目标

在前两讲（[u4-l1](u4-l1-library-structure.md) 与 [u4-l2](u4-l2-xilinx-ip-packaging.md)）里，我们已经看清了「库侧如何把厂商无关的 RTL 打包成可复用 IP」的整体框架，并完整拆解了 **Xilinx 侧** 的打包流程（`*_ip.tcl` + `adi_ip_xilinx.tcl`，产物是 `component.xml`）。

本讲把视角扩展到另外两家厂商，目标是让你学完后能够：

1. 说出 **Intel 侧** `*_hw.tcl` 的打包模型：它如何在 Qsys/Platform Designer 内以「回调（callback）」方式描述参数、接口与条件逻辑，以及 `.sdc` 约束的作用。
2. 说出 **Lattice 侧** `*_ltt.tcl` 的打包模型：它如何用纯 `tclsh` 声明式地构建一棵 XML 树，生成 `metadata.xml` 等文件，并与 Radiant / Propel 工具链衔接。
3. 把三家厂商的打包方式放进同一张表里对比，理解「同一份 RTL、三种打包描述」的**共性**与**厂商专属差异**，并能解释 `Makefile` 里 `INTEL_DEPS` / `LATTICE_DEPS` 为何要把 util 模块源码显式列出来。

本讲不重复 u4-l1/u4-l2 已建立的结论（依赖桶、`component.xml` 打包三步走、接口推断等），而是在其基础上补齐另外两条厂商支线。

## 2. 前置知识

阅读本讲前，你应当已经了解以下概念（若不熟悉，请先复习对应讲义）：

- **IP 打包**：把一组 Verilog 源码连同其参数、接口、约束描述成 EDA 工具能识别的「可复用 IP」。Xilinx 用 IP-XACT `component.xml`，Intel 用 `hw.tcl`，Lattice 用 `metadata.xml`（lsccip 格式）。（详见 u4-l2）
- **依赖桶**：库 Makefile 用 `GENERIC_DEPS` / `XILINX_DEPS` / `INTEL_DEPS` / `LATTICE_DEPS` 四个变量把文件按厂商分组，`library.mk` 为每家厂商各定一套 make 目标。（详见 u4-l1）
- **关键不对称**：Xilinx 走「跨库引用已打包 IP」（`XILINX_LIB_DEPS` → 别人的 `component.xml`），Intel/Lattice 走「源码扁平嵌入」。本讲会看到这一不对称在 `axi_dmac/Makefile` 里的具体证据。
- **AXI 总线族**：AXI4 / AXI3（内存映射）、AXI4-Lite（寄存器）、AXI4-Stream（数据流）。三家打包脚本都要把这些端口归类成「接口」。
- **约束文件后缀**：Xilinx `.xdc`、Intel `.sdc`（Synopsys Design Constraints）、Lattice `.ldc` / `.pdc`。

此外有两个 Intel/Lattice 特有的术语需要先建立直觉：

- **回调（callback）**：Intel 的 Qsys 在 GUI 里加载一个 IP 时，会主动调用你在 `hw.tcl` 里登记的 Tcl 过程（如 `ELABORATION_CALLBACK`、`VALIDATION_CALLBACK`）。换句话说，Intel 的打包脚本是「活的」——它在用户配置 IP 时被反复执行，用来动态决定哪些参数可见、哪些接口启用。这与 Xilinx/Lattice 在打包时「一次性生成静态描述文件」截然不同。
- **lsccip / IPXACT**：Lattice 用自家的 `lsccip` XML 命名空间描述 IP 本体（`metadata.xml`），但接口的抽象定义（bus/abstraction）则借用业界标准 IP-XACT 1685-2014 格式。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 |
| --- | --- |
| [library/scripts/adi_ip_intel.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl) | Intel 侧公共打包原语库，封装 Qsys `hw.tcl` API |
| [library/scripts/adi_ip_lattice.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl) | Lattice 侧公共打包库，`ipl` 命名空间，纯 Tcl 构建 XML 树 |
| [library/axi_dmac/axi_dmac_hw.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl) | `axi_dmac` 的 Intel 打包脚本（实例） |
| [library/axi_dmac/axi_dmac_ltt.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl) | `axi_dmac` 的 Lattice 打包脚本（实例） |
| [library/axi_dmac/axi_dmac_constr.sdc](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc) | `axi_dmac` 的 Intel 时序约束（`set_false_path`） |
| [library/axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile) | `axi_dmac` 库的依赖桶声明（三厂商分组） |
| [library/scripts/library.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk) | 库公共 make 脚本，定义 `intel`/`xilinx`/`lattice` 三套目标 |
| [library/scripts/lattice_tool_set.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk) | 声明 `LATTICE_IP_TOOL := tclsh` |

我们仍以 `axi_dmac`（ADI 的 AXI DMA 引擎）作为贯穿全讲的样本 IP，因为它同时具备三套打包脚本，且结构足够丰富（多种 AXI 接口、大量可配置参数、条件接口）。

---

## 4. 核心概念与源码讲解

### 4.1 Intel hw.tcl 打包模型

#### 4.1.1 概念说明

Intel（Altera）的 Qsys / Platform Designer 用一种叫 **hw.tcl** 的脚本来描述一个 IP。与 Xilinx「打包成静态 `component.xml`」不同，Intel 的 `hw.tcl` 是一段**在 Qsys 内被解释执行的 Tcl 程序**：它直接调用 Qsys 提供的命令（`set_module_property`、`add_parameter`、`add_interface`、`add_interface_port`、`add_fileset_file` 等）来「自描述」这个 IP。

ADI 为了不让大家每写一个 IP 都重复这套冗长的 Qsys 原生命令，在 [library/scripts/adi_ip_intel.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl) 里提供了一组薄封装（`ad_ip_create`、`ad_ip_files`、`ad_ip_parameter`、`ad_interface`、`ad_ip_intf_s_axi` 等），它们内部转调 Qsys 原生 API。于是每个 IP 的 `*_hw.tcl` 只需 `source adi_ip_intel.tcl`，再用这组封装「报菜名」即可。

Intel 模型最核心的特征是**回调驱动**：

- `ELABORATION_CALLBACK`：Qsys 在展开 IP 时调用，用来根据当前参数值**动态增删接口**（例如只在 `DMA_TYPE_DEST==0` 时启用内存映射主接口）。
- `VALIDATION_CALLBACK`：用户每次改动参数时调用，用来**推导派生参数**（例如根据所选时钟域自动计算 `ASYNC_CLK_*`）、校验合法性、控制参数的可见/可编辑性。

这种「活的脚本」是 Intel 与另外两家最大的区别。

#### 4.1.2 核心流程

一个典型 Intel `hw.tcl`（以 `axi_dmac_hw.tcl` 为例）的执行流程：

1. **加载环境**：`package require qsys 14.0`，`source adi_env.tcl` 与 `adi_ip_intel.tcl`。
2. **声明模块属性与回调**：`set_module_property NAME/VERSION/GROUP/...`，并登记 `ELABORATION_CALLBACK` 与 `VALIDATION_CALLBACK`。
3. **登记源码与约束**：`ad_ip_files axi_dmac [list ...]`，内部自动建两个 fileset（综合用 `QUARTUS_SYNTH`、仿真用 `SIM_VERILOG`），并按后缀识别文件类型；约束 `.sdc` 以 `SDC` 类型登记，`set_qip_strings` 追加 Quartus 赋值。
4. **声明参数**：对每个参数 `add_parameter` + 一串 `set_parameter_property`（显示名、`HDL_PARAMETER`、`ALLOWED_RANGES`、`GROUP`、`DERIVED` 等）。派生参数标记为 `DERIVED true`，其值由校验回调计算。
5. **声明常驻接口**：如 AXI4-Lite 从接口 `ad_ip_intf_s_axi`、中断 `interrupt_sender`、各 AXI-Stream/FIFO 接口。
6. **回调内做条件化**：`axi_dmac_elaborate` 依据参数 `set_interface_property $intf ENABLED false` 关闭不需要的接口、`set_port_property ... TERMINATION true` 终止个别端口；`axi_dmac_validate` 推导派生参数并校验。

注意第 1～5 步描述的是 IP 的「静态骨架」，第 6 步的回调才在 GUI 里动态生效——这正是 Intel 模型的精髓。

#### 4.1.3 源码精读

**（a）公共原语库 `adi_ip_intel.tcl` 的几个关键封装**

`ad_ip_create` 设置模块属性并登记回调，注意它把 `ELABORATION_CALLBACK` / `COMPOSITION_CALLBACK` 作为可选参数挂上：

[library/scripts/adi_ip_intel.tcl:103-118](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl#L103-L118) —— 设置 NAME/VERSION/GROUP 等模块属性，若传了回调名就用 `set_module_property ELABORATION_CALLBACK` 挂上。

`ad_ip_files` 为同一个文件列表创建**两个 fileset**（综合 + 仿真），这是 Intel 模型特有的「按用途分文件集」：

[library/scripts/adi_ip_intel.tcl:395-408](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl#L395-L408) —— 先 `add_fileset quartus_synth QUARTUS_SYNTH`，再 `add_fileset quartus_sim SIM_VERILOG`，分别塞入同一批源码。

`ad_ip_addfile` 按扩展名自动判定文件类型（`.v`→VERILOG、`.sv`→SYSTEM_VERILOG、`.sdc`→SDC、`.tcl`→OTHER），免去手工区分：

[library/scripts/adi_ip_intel.tcl:353-388](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl#L353-L388) —— 文件类型分发逻辑。

`ad_ip_intf_s_axi` 是 ADI 为「每个 IP 都有的 AXI4-Lite 寄存器从接口」准备的一键宏，把 18 个 AXI-Lite 信号一次性声明成 `s_axi` 接口：

[library/scripts/adi_ip_intel.tcl:416-447](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_intel.tcl#L416-L447) —— 声明 `s_axi_clock`、`s_axi_reset`、`s_axi`（axi4lite）三个接口及其端口，地址宽度由参数 `addr_width` 决定。

**（b）实例 `axi_dmac_hw.tcl`：加载、骨架、回调三段式**

开头加载环境并声明模块属性与两个回调：

[library/axi_dmac/axi_dmac_hw.tcl:6-16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L6-L16) —— `package require qsys 14.0`，登记 `ELABORATION_CALLBACK axi_dmac_elaborate` 与 `VALIDATION_CALLBACK axi_dmac_validate`。

源码登记（注意它把 `axi_dmac_constr.sdc` 也塞进了文件列表，由 `ad_ip_addfile` 按 `.sdc` 后缀识别为 SDC 约束）：

[library/axi_dmac/axi_dmac_hw.tcl:20-61](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L20-L61) —— `ad_ip_files` 列出全部 RTL 与 `.sdc`，随后 `set_qip_strings` 追加一条 `MESSAGE_DISABLE` 的 Quartus 赋值（禁用双口 RAM 读改写警告）。

派生参数的典型写法（以「时钟域异步」系列为例）：同时声明一个可见的手动参数 `XXX_MANUAL`（`HDL_PARAMETER false`、`VISIBLE false`）和一个真实 HDL 参数 `XXX`（`DERIVED true`），后者在校验回调里被计算赋值：

[library/axi_dmac/axi_dmac_hw.tcl:275-297](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L275-L297) —— `ASYNC_CLK_*` 的手动/派生参数对，配合下方的 `CLK_DOMAIN_*` 系统信息参数。

校验回调 `axi_dmac_validate` 是 Intel 模型最「活」的部分——它读取各时钟域的系统信息（`CLOCK_DOMAIN`），两两比较后 `set_parameter_value ASYNC_CLK_*` 自动判定是否异步，同时根据 `DMA_TYPE_*` 控制接口与参数的可见性：

[library/axi_dmac/axi_dmac_hw.tcl:348-461](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L348-L461) —— 校验回调：自动推导时钟域异步标志、计算 `MAX_BYTES_PER_BURST` 上限、控制 `AXIS_TUSER_SYNC` 的启用、推导 `AXI_AXCACHE/AXPROT` 等。

展开回调 `axi_dmac_elaborate` 负责按参数「点亮 / 熄灭」接口：把不启用的接口名收集进 `disabled_intfs`，再统一 `set_interface_property $intf ENABLED false`；同时用 `set_port_property ... TERMINATION true` 终止 AXI4 模式下多余的 AXI3 信号：

[library/axi_dmac/axi_dmac_hw.tcl:602-729](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L602-L729) —— 展开回调：按 `DMA_TYPE_SRC/DEST`、`DMA_SG_TRANSFER` 等启用对应接口，关闭其余接口。

辅助过程 `add_axi_master_interface` 展示了 Intel 如何「逐端口」描述一个 AXI 主接口，并用 `TERMINATION` 在 AXI4 模式下隐藏 AXI3 专属信号（`awid`/`wid`/`bid` 等）：

[library/axi_dmac/axi_dmac_hw.tcl:545-601](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L545-L601) —— 逐端口 `add_interface_port` 声明 AXI 主接口，并在 AXI4 时把 AXI3 专属端口 `TERMINATION true`。

**（c）`.sdc` 约束**

Intel 侧时序约束用 `.sdc`，核心是大量 `set_false_path`，把跨时钟域同步链路、复位管理器、调试通路排除在时序分析之外：

[library/axi_dmac/axi_dmac_constr.sdc:6-37](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_constr.sdc#L6-L37) —— 对 `cdc_sync_stage1`、`eot_mem`、`burst_len_mem`、复位管理器的 `reset_gen` 等设置 false path。

#### 4.1.4 代码实践

**实践目标**：理解 Intel `hw.tcl`「静态骨架 + 动态回调」的二分结构。

**操作步骤**：

1. 打开 [axi_dmac_hw.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl)。
2. 用「两支彩笔」法通读：把**脚本顶层（非 `proc` 内）**的 `add_parameter` / `add_interface` / `ad_ip_files` 标成一种颜色（静态骨架），把 `proc axi_dmac_validate` 与 `proc axi_dmac_elaborate` 内的语句标成另一种颜色（动态回调）。
3. 在 `axi_dmac_validate` 中定位 `ASYNC_CLK_REQ_SRC` 的赋值语句，回答：它的值由哪几个变量比较得出？为什么不能在脚本顶层直接写死？

**需要观察的现象**：

- 静态骨架部分「声明」了所有可能出现的参数与接口；动态回调部分根据用户选择「裁剪」它们。
- `ASYNC_CLK_REQ_SRC` 依赖 `CLK_DOMAIN_REQ` 与 `CLK_DOMAIN_SRC_AXI`，而这两个 `CLK_DOMAIN_*` 是 `SYSTEM_INFO {CLOCK_DOMAIN ...}`（见 [L299-330](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L299-L330)）——即它们取自 Qsys 连线时的实际时钟，只有运行时才知道，所以推导必须放进回调。

**预期结果**：你能用一句话说清「Intel 的条件化逻辑为何必须写成回调，而不能像 Xilinx 那样写成静态的依赖表达式」。

> 本实践为源码阅读型，无需运行工具；若要本地验证回调行为，需在安装 Quartus 的环境中用 Qsys 实例化该 IP 并切换 `DMA_TYPE_*` 观察接口变化（**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：`ad_ip_files` 为什么要把同一批文件塞进两个 fileset？少塞一个会怎样？
**参考答案**：因为综合与仿真可能用不同的源码视图（仿真有时需要 behavioral 模型）。`ad_ip_files` 同时建 `QUARTUS_SYNTH`（综合）与 `SIM_VERILOG`（仿真）两个 fileset。若只建综合 fileset，仿真时该 IP 将无源可用。

**练习 2**：`AXI_AXCACHE` 这个参数被标记为 `DERIVED true`（[L133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L133)），它的最终值从哪里来？
**参考答案**：它不是由用户直接填写，而是由 `axi_dmac_validate` 回调依据 `CACHE_COHERENT`、`AXI_AXCACHE_AUTO`、`AXI_AXCACHE_MANUAL` 三者推导（见 [L447](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L447)）。`DERIVED true` 表示该参数对用户只读、由系统计算。

---

### 4.2 Lattice ltt.tcl 与工具链

#### 4.2.1 概念说明

Lattice 侧的打包脚本叫 `*_ltt.tcl`（ltt ≈ Lattice Tool / IP 描述）。它的公共原语库 [library/scripts/adi_ip_lattice.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl) 把所有过程放在 `ipl` 命名空间里。

Lattice 的打包模型与 Intel 几乎相反——它是**纯声明式、数据驱动**的：

- 你不需要调用任何 Lattice 工具的命令，而是用 `ipl::general`、`ipl::add_axi_interfaces`、`ipl::set_parameter` 等过程，把 IP 的描述一点点「填」进一棵内存中的 XML 树（用嵌套 Tcl 列表表示：`{名字 属性 内容 子节点}`）。
- 描述填完后，`ipl::generate_ip $ip` 把这棵树序列化成一组静态 XML 文件（`metadata.xml`、`bus_interface.xml`、`address_space.xml`、`memory_map.xml`）写到 `./ltt/<ip_name>/` 目录。

最关键的工程细节是：**这一切由普通 `tclsh` 执行**，不需要 Radiant 或 Propel 在场。`library.mk` 里 `LATTICE_IP_TOOL := tclsh`（[lattice_tool_set.mk:6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk#L6)）。也就是说，Lattice 是三家中唯一在「库打包阶段」就用脚本实际生成产物文件的厂商（Intel 在库阶段只 `touch` 一个时间戳，Xilinx 需要 `vivado`）。

生成的 `metadata.xml` 使用 Lattice 自家的 `lsccip` 命名空间，可被 **Radiant** 与 **Propel**（脚本里以平台代号 `esi` 表示）两套工具消费。脚本通过 `supported_platforms {esi radiant}` 与 `min_radiant_version` / `min_esi_version` 声明兼容性。

#### 4.2.2 核心流程

一个 Lattice `*_ltt.tcl`（以 `axi_dmac_ltt.tcl` 为例）的执行流程：

1. **加载**：`source adi_env.tcl` 与 `adi_ip_lattice.tcl`，得到 `ipl` 命名空间。
2. **解析顶层模块**：`ipl::parse_module ./axi_dmac.v` 用正则把 Verilog 顶层模块的端口与参数抽出来，返回一个 `mod_data` 结构，供后续过程自动复用。
3. **设置 IP 元信息**：`ipl::general` 设置 VLNV（`analog.com:ip:axi_dmac:1.0`）、显示名、类别、`supported_products`/`supported_platforms`、最小工具版本、文档链接。
4. **添加地址空间 / 内存映射**：`ipl::add_memory_map`（从接口的寄存器空间）、`ipl::add_address_space`（每个 AXI 主接口的可寻址范围）。
5. **添加端口与接口**：`ipl::add_ports_from_module`（把顶层端口灌进 IP）、`ipl::add_axi_interfaces`（按 `<前缀>_aclk` 等命名规律自动推断 AXI4/AXI4-Lite/AXI4-Stream 接口）、`ipl::add_interface`（对自定义接口如 `fifo_wr`/`fifo_rd`/`IRQ` 显式给出端口映射）。
6. **登记源码**：`ipl::add_ip_files -dpath rtl -flist {…}`，把这些文件归到产物目录下的 `rtl/` 子文件夹（注意：源码会被复制进产物目录，与 Intel 一样是「源码嵌入」）。
7. **声明参数与条件**：`ipl::set_parameter` 逐个声明，用 `-options`/`-value_range` 限定取值，用 `-editable {(<Python 表达式>)}` 控制可编辑性；`ipl::ignore_ports` / `ipl::ignore_ports_by_prefix` 用 Python 表达式隐藏不该出现的端口。
8. **生成**：`ipl::generate_ip $ip` 序列化成 XML 文件。

Lattice 用 **Python 表达式**（而不是 Tcl 回调）来表达条件逻辑——这是 Propel IP Packager 的约定：可编辑性、端口隐藏都写成基于参数的 Python 布尔表达式，由 GUI 求值，无需 Tcl 回调。

#### 4.2.3 源码精读

**（a）公共库 `adi_ip_lattice.tcl`：一棵 XML 树的生长**

文件开头的注释明确点出设计思想：「主要是若干 XML 树操作过程 + Lattice IP 的基础描述符 + 配置这些描述符的过程 + 一个 XML 生成器与目录生成器」，并指出 `$::ipl::ip` 是承载所有 IP 数据的结构，每个 `set ip [ipl::xxx -ip $ip ...]` 调用都在更新它：

[library/scripts/adi_ip_lattice.tcl:6-83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L6-L83) —— 设计总览：四种 IP 相关 XML 描述符 + 两种接口描述符，统一包进 `$::ipl::ip`。

`ipl::general` 是元信息入口，解析 VLNV、写 `supported_products`/`supported_platforms`、设最小版本等：

[library/scripts/adi_ip_lattice.tcl:865-950](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L865-L950) —— 接收 `-vlnv`/`-display_name`/`-supported_products`/`-supported_platforms`/`-min_radiant_version` 等，把它们写进 `ip_desc/lsccip:general` 子树。

`ipl::set_parameter` 把每个参数变成 `lsccip:settings` 下的一个 `lsccip:setting` 节点，支持 `-options`（枚举）、`-value_range`（范围）、`-editable`/`-hidden`（Python 表达式）：

[library/scripts/adi_ip_lattice.tcl:1013-1101](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L1013-L1101) —— 参数声明过程，把选项拼成 XML 属性挂到 `lsccip:settings` 节点。

`ipl::ignore_ports` 用 Python 表达式给端口打上 `-dangling`（隐藏）标记：

[library/scripts/adi_ip_lattice.tcl:1188-1202](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L1188-L1202) —— 把端口列表逐个 `set_port -dangling $expression`，实现按参数隐藏端口。

`ipl::parse_module` 用正则从 Verilog 文本里抽端口与参数，是 Lattice「自动推断」的基础：

[library/scripts/adi_ip_lattice.tcl:1545-1624](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L1545-L1624) —— 正则匹配 `module`、`input/output/inout`、`parameter`，组装成 `mod_data`。

`ipl::add_axi_interfaces` 实现自动推断：它扫描所有以 `_aclk` 结尾的端口当作时钟，按前缀分组，再据 `arid/awid/araddr/awaddr/tvalid/valid` 等端口的有无与方向，判定是 AXI4 / AXI4-Lite / AXI4-Stream，以及主从方向：

[library/scripts/adi_ip_lattice.tcl:2261-2501](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L2261-L2501) —— 按时钟前缀分组、按标志性端口判定总线类型与主从，调用 `add_interface_by_prefix` 批量映射。

`ipl::generate_ip_on_path` 是序列化出口：建目录、复制 `fdeps`（文件依赖，按 `rtl`/`ldc`/`doc` 等分类）、写出 `bus_interface.xml`/`address_space.xml`/`memory_map.xml`/`metadata.xml`，并生成 `doc/introduction.html`：

[library/scripts/adi_ip_lattice.tcl:1465-1536](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L1465-L1536) —— 复制文件依赖、为各类描述符写 XML、最终落盘 `metadata.xml`。

**（b）实例 `axi_dmac_ltt.tcl`：声明式链式调用**

开头加载环境并解析顶层模块，拿到 `mod_data` 与初始 `$ip`：

[library/axi_dmac/axi_dmac_ltt.tcl:6-12](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L6-L12) —— `source adi_env.tcl` + `adi_ip_lattice.tcl`，`ipl::parse_module ./axi_dmac.v`，`ipl::add_ports_from_module`。

元信息与平台兼容性声明（注意 `supported_platforms {esi radiant}`，对应 Propel 与 Radiant）：

[library/axi_dmac/axi_dmac_ltt.tcl:14-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L14-L23) —— 设置显示名、`supported_products {*}`（全部器件）、`supported_platforms {esi radiant}`、VLNV 与最小版本。

地址空间（三个 AXI 主接口各一个 4 GB 空间）与寄存器内存映射：

[library/axi_dmac/axi_dmac_ltt.tcl:25-42](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L25-L42) —— `add_memory_map`（64 KB 寄存器空间）+ 三个 `add_address_space`（`m_dest_axi_aspace`/`m_src_axi_aspace`/`m_sg_axi_aspace`，各 `0x100000000`）。

自动推断 AXI 接口 + 显式声明自定义接口（`fifo_wr`/`fifo_rd`/`m_framelock`/`s_framelock`/`IRQ`），每个自定义接口给出端口→逻辑名映射与 VLNV：

[library/axi_dmac/axi_dmac_ltt.tcl:44-105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L44-L105) —— `add_axi_interfaces` 自动推断 + 五个 `add_interface` 显式声明。

源码登记，全部归入 `rtl` 子目录（注意它显式列出了 `../util_axis_fifo/util_axis_fifo.v`、`../util_cdc/sync_bits.v`、`../common/ad_mem.v` 等跨库源码——源码嵌入的证据）：

[library/axi_dmac/axi_dmac_ltt.tcl:107-145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L107-L145) —— `add_ip_files -dpath rtl -flist {…}`，把本地与跨库 RTL 一次性列入。

参数声明示例，用 `-editable {(<Python 表达式>)}` 表达条件可编辑（这正是 Lattice 替代 Intel 回调的机制）：

[library/axi_dmac/axi_dmac_ltt.tcl:148-176](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L148-L176) —— `DMA_TYPE_SRC`/`DMA_AXI_PROTOCOL_SRC`（`-editable {(DMA_TYPE_SRC == 0)}`）/`DMA_DATA_WIDTH_SRC` 等。

端口隐藏示例，用 Python 表达式按 `DMA_TYPE_*` 隐藏未使用的通道端口：

[library/axi_dmac/axi_dmac_ltt.tcl:205-216](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L205-L216) —— `ignore_ports_by_prefix` 依据 `not(DMA_TYPE_SRC == 0)` 等表达式隐藏 `m_src_axi`/`s_axis`/`fifo_rd` 前缀端口。

收尾一行触发序列化：

[library/axi_dmac/axi_dmac_ltt.tcl:791](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L791) —— `ipl::generate_ip $ip`，把累积的 `$ip` 结构写成 XML 产物。

#### 4.2.4 代码实践

**实践目标**：用「填表」的视角理解 Lattice 声明式打包，并验证它由普通 `tclsh` 驱动。

**操作步骤**：

1. 打开 [axi_dmac_ltt.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl)，注意几乎每行都是 `set ip [ipl::xxx -ip $ip ...]` 形式——这是「把描述累加进 `$ip`」的链式写法。
2. 找到 `DMA_AXI_PROTOCOL_SRC` 的声明（[L159](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L159)），对比 Intel 侧对同一参数的条件化处理（Intel 是在 `axi_dmac_validate` 里 `set_parameter_property DMA_AXI_PROTOCOL_SRC VISIBLE $show_axi_protocol`）。体会：Lattice 用一行 `-editable`，Intel 用一段回调。
3. 打开 [lattice_tool_set.mk:6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk#L6) 与 [library.mk:156-165](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L156-L165)，确认 `lattice` 目标用 `$(LATTICE_IP_TOOL)`（即 `tclsh`）执行 `axi_dmac_ltt.tcl`。

**需要观察的现象**：

- 整个 `axi_dmac_ltt.tcl` 里没有任何 `proc`，也没有回调登记——所有条件逻辑都退化成了「带 Python 表达式的属性」。
- 驱动工具是 `tclsh`，意味着只要系统装了 Tcl，`make lattice` 就能在没有 Radiant 的机器上生成 `./ltt/axi_dmac/metadata.xml`。

**预期结果**：你能口头复述「Lattice 打包 = 用纯 Tcl 描述一棵 XML 树 + `tclsh` 序列化」，并指出它与 Intel「回调驱动」的本质差异。

> 若本地装了 Tcl，可尝试在 `library/axi_dmac/` 下运行 `tclsh axi_dmac_ltt.tcl`，观察是否在 `./ltt/axi_dmac/` 下生成 `metadata.xml` 等文件（**待本地验证**：实际生成还需 `ad_hdl_dir` 等环境变量正确设置）。

#### 4.2.5 小练习与答案

**练习 1**：为什么说 Lattice 是三家中唯一在「库打包阶段」就真正生成产物文件的厂商？另外两家在 `library.mk` 的对应目标里分别做了什么？
**参考答案**：因为 `lattice` 目标用 `tclsh` 跑 `*_ltt.tcl`，直接写出 `./ltt/<ip>/metadata.xml` 等文件（[library.mk:156-165](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L156-L165)）。对比之下，Intel 的 `intel` 目标只 `touch .timestamp_intel`（[library.mk:84-87](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L84-L87)），不跑 Quartus；Xilinx 的 `xilinx` 目标要调 `vivado` 跑 `*_ip.tcl` 才产出 `component.xml`（[library.mk:116-125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125)）。

**练习 2**：`ipl::add_axi_interfaces` 是怎么判断一个接口是 AXI4 还是 AXI4-Lite 的？
**参考答案**：它先按 `_aclk` 后缀找时钟、按前缀分组；然后看该前缀下是否有 `arid`/`awid`（有则为 AXI4），否则看是否有 `araddr`/`awaddr`（有则为 AXI4-Lite），再否则看 `tvalid`/`valid` 等判定 AXI4-Stream。主从方向由这些端口的 input/output 方向决定（见 [L2261-2501](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/adi_ip_lattice.tcl#L2261-L2501)）。

---

### 4.3 三厂商打包差异对比

#### 4.3.1 概念说明

把三家放在一起，最容易抓住共性与差异。**共性**是：三者都在描述「这个 IP 由哪些源码组成、有哪些参数、对外呈现哪些接口、需要哪些约束」这同一组信息，只是载体与执行时机不同。**差异**主要体现在四个维度：

1. **描述载体**：Xilinx = IP-XACT `component.xml`；Intel = Qsys 内解释执行的 `hw.tcl`；Lattice = `lsccip` 格式的 `metadata.xml` 等。
2. **执行模型**：Xilinx = Vivado 打包时一次性生成静态描述；Intel = 回调在 GUI 里动态求值；Lattice = `tclsh` 一次性序列化静态 XML，条件逻辑转写成 Python 表达式。
3. **跨库复用**：Xilinx 引用别人已打包的 `component.xml`（`XILINX_LIB_DEPS`）；Intel/Lattice 把跨库源码扁平嵌入（`INTEL_DEPS`/`LATTICE_DEPS` 显式列源文件）。
4. **约束**：Xilinx `.xdc`（常由 `.ttcl` 动态生成）；Intel `.sdc`；Lattice `.ldc`。

#### 4.3.2 核心流程（三家对照）

下面这张总表把三家的关键差异压缩到一屏，建议作为本讲的「速查卡」：

| 维度 | Xilinx | Intel | Lattice |
| --- | --- | --- | --- |
| 打包脚本 | `*_ip.tcl` | `*_hw.tcl` | `*_ltt.tcl` |
| 公共原语库 | `adi_ip_xilinx.tcl` | `adi_ip_intel.tcl` | `adi_ip_lattice.tcl`（`ipl` 命名空间） |
| 产物 | `component.xml`（IP-XACT） | （库阶段无静态产物，仅 `.timestamp_intel`；`hw.tcl` 由 Qsys 运行时解释） | `./ltt/<ip>/metadata.xml` + `bus_interface.xml` + `address_space.xml` + `memory_map.xml`（lsccip 格式） |
| 库阶段驱动工具 | `vivado` 批处理 | 不跑工具，仅 `touch` | `tclsh`（`LATTICE_IP_TOOL`） |
| 执行模型 | 一次性静态打包 | 回调驱动（`ELABORATION`/`VALIDATION_CALLBACK`） | 声明式 XML 树构建 + 序列化 |
| 条件逻辑 | `adi_set_bus_dependency`（XPath `spirit:decode` 表达式） | Tcl 回调里改 `VISIBLE`/`ENABLED`/`TERMINATION` | 参数 `-editable`/端口 `-dangling` 的 Python 表达式 |
| 源码登记 | `adi_ip_files`（单列表） | `ad_ip_files`（双 fileset：综合+仿真） | `ipl::add_ip_files -dpath rtl`（复制进产物 `rtl/`） |
| 接口推断 | `adi_ip_infer_mm_interfaces` + `adi_add_bus` | 逐端口 `add_interface_port` + 回调裁剪 | `ipl::add_axi_interfaces` 自动推断 + `add_interface` 显式映射 |
| 约束文件 | `*_constr.ttcl` → `.xdc` | `*_constr.sdc` | `ldc`（经 `fdeps` 的 `ldc` 目录；本 IP 无） |
| 跨库复用 | `XILINX_LIB_DEPS` → 引用 `component.xml` | `INTEL_DEPS` 显式列源文件 | `LATTICE_DEPS` 显式列源文件 |

#### 4.3.3 源码精读（用 `axi_dmac/Makefile` 印证三家差异）

`axi_dmac` 的 Makefile 把同一个 IP 的资产分进了四个桶，是观察「源码嵌入 vs 跨库引用」不对称的最佳证据：

[library/axi_dmac/Makefile:9-39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L9-L39) —— `GENERIC_DEPS`：三家共用的纯 RTL（`axi_dmac.v`、`up_axi.v`、`ad_mem_asym.v` 等），与厂商无关。

[library/axi_dmac/Makefile:41-55](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L41-L55) —— `XILINX_DEPS` 只列打包资产（`_ip.tcl`/`.ttcl`/`bd.tcl`/接口 XML），跨库复用走 `XILINX_LIB_DEPS += util_axis_fifo util_cdc`（**只写库名，引用其 `component.xml`**）。

[library/axi_dmac/Makefile:58-64](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L58-L64) —— `INTEL_DEPS` **显式列出** `../util_axis_fifo/util_axis_fifo.v`、`../util_cdc/sync_bits.v` 等源文件 + `axi_dmac_constr.sdc` + `axi_dmac_hw.tcl`。源码嵌入。

[library/axi_dmac/Makefile:66-72](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L72) —— `LATTICE_DEPS` 同样**显式列出** `../common/ad_mem.v`、`../util_axis_fifo/*.v`、`../util_cdc/*.v` + `axi_dmac_ltt.tcl`。源码嵌入。

一个值得回味的设计副产品：Xilinx 的 `axi_dmac_ip.tcl` 里有一大段 `dummy_axi_ports`，注释直接说明这是「为了让 Intel 工具满意」才加进 RTL 的——Intel 不支持单向 AXI 接口、且 AXI3 模式要求某些标准里可选的信号。这说明三家打包并不彼此独立，RTL 本身会被迫向「最严格的那家」妥协：

[library/axi_dmac/axi_dmac_ip.tcl:113-192](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L113-L192) —— `dummy_axi_ports` 注释「keep the Intel tools happy」，然后在 Xilinx 侧用 `adi_set_ports_dependency $p "false"` 把这些为 Intel 而存在的端口隐藏掉。

#### 4.3.4 代码实践

**实践目标**：把三家的打包脚本横向对齐，亲自整理出「共用 vs 厂商专属」的清单。

**操作步骤**：

1. 同时打开三个文件：[axi_dmac_ip.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl)（Xilinx）、[axi_dmac_hw.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl)（Intel）、[axi_dmac_ltt.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl)（Lattice）。
2. 在三个文件里分别定位下列三类信息，填进一张表：
   - **源文件列表**：Xilinx 在 `adi_ip_files axi_dmac [list …]`（[ip.tcl L13-47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L13-L47)）；Intel 在 `ad_ip_files axi_dmac [list …]`（[hw.tcl L20-58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L20-L58)）；Lattice 在 `ipl::add_ip_files … -flist [list …]`（[ltt.tcl L107-145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L107-L145)）。
   - **约束**：Xilinx `axi_dmac_constr.ttcl`；Intel `axi_dmac_constr.sdc`；Lattice（本 IP 无独立约束文件）。
   - **参数**：三家都声明了 `ID`、`DMA_TYPE_SRC/DEST`、`DMA_DATA_WIDTH_*`、`CYCLIC`、`DMA_SG_TRANSFER` 等——这就是「共用参数集」。
3. 标出**厂商专属**部分：Xilinx 的 `bd/bd.tcl` 与 `adi_ip_bd`、Intel 的 `VALIDATION_CALLBACK` 与 `ELABORATION_CALLBACK`、Lattice 的 `supported_platforms`/`min_radiant_version` 与 Python 表达式。

**需要观察的现象**：

- 三家列出的 RTL 集合「几乎一致」（都以 `axi_dmac.v` 为顶层、都包含 `data_mover.v`/`dmac_sg.v` 等），差异主要在「是否显式列出 util 模块源码」与「约束后缀」。
- 参数集合高度重合，说明「IP 的可配置面」是厂商无关的；厂商差异集中在「如何把可配置面表达给工具」。

**预期结果**：你产出一张三列对照表，能圈出「共用 RTL/参数」与「厂商专属打包机制」，并用一句话解释为何 Intel/Lattice 的 Makefile 要把 util 源码显式列出（因为它们走源码嵌入，不像 Xilinx 能引用别人的 `component.xml`）。

> 本实践为源码阅读型，无需运行工具。

#### 4.3.5 小练习与答案

**练习 1**：同一个参数 `DMA_TYPE_SRC`，三家各用什么机制表达「只有选了 Memory-Mapped 时，AXI 协议参数才可编辑」？
**参考答案**：Xilinx 用 `enablement_tcl_expr "\$DMA_TYPE_SRC == 0"` 挂在 user parameter 上（见 [ip.tcl L341](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L341)）；Intel 在 `axi_dmac_validate` 回调里 `set_parameter_property DMA_AXI_PROTOCOL_SRC VISIBLE $show_axi_protocol`（[hw.tcl L422](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_hw.tcl#L422)）；Lattice 用 `-editable {(DMA_TYPE_SRC == 0)}`（[ltt.tcl L166](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ltt.tcl#L166)）。三者语义相同，载体分别是 XPath/Tcl 回调/Python 表达式。

**练习 2**：为什么 `axi_dmac_ip.tcl`（Xilinx）里会出现一段注释为「keep the Intel tools happy」的 `dummy_axi_ports`？
**参考答案**：因为 RTL 要被三家共用，而 Intel 工具不支持单向 AXI 接口、且 AXI3 模式要求某些标准里可选的信号（`awid`/`wid`/`bid` 等）。为了让 Intel 也能打包，RTL 里保留了这些端口；Xilinx 侧则用 `adi_set_ports_dependency $p "false"` 把它们隐藏，以免影响 Xilinx 的接口质量（见 [ip.tcl L113-192](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_ip.tcl#L113-L192)）。这是「多厂商共用 RTL 迫使 RTL 向最严格厂商妥协」的典型例子。

**练习 3**：如果一个新 IP 只打算支持 Xilinx，它的 Makefile 里 `INTEL_DEPS` / `LATTICE_DEPS` 会怎样？`library.mk` 的 `intel` / `lattice` 目标又会怎样？
**参考答案**：`INTEL_DEPS` / `LATTICE_DEPS` 为空。`library.mk` 用 `ifneq ($(INTEL_DEPS),)` / `ifneq ($(LATTICE_DEPS),)` 守卫（[library.mk:77](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L77) 与 [L137](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L137)）——变量为空时对应的 `intel` / `lattice` 目标根本不会被定义，`make all`（依赖 `intel xilinx lattice`，[L65](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L65)）里那两家就自动跳过。

---

## 5. 综合实践

**任务**：为本讲的样本 IP `axi_dmac` 制作一份「三厂商打包对照速查表」，并在表下用 3～5 句话总结「同一份 RTL、三种打包」的根本逻辑。

**建议步骤**：

1. 复用 4.3.4 的对照表，但这次要补全三列的下列行项：
   - 顶层打包脚本入口（Xilinx `adi_ip_create axi_dmac`、Intel `set_module_property NAME axi_dmac`、Lattice `ipl::general -vlnv …`）。
   - 接口推断方式（Xilinx `adi_ip_infer_mm_interfaces`、Intel 逐端口+回调、Lattice `ipl::add_axi_interfaces`）。
   - `AXI4-Lite` 从接口的声明位置（Xilinx 由 `adi_ip_properties` 固化、Intel `ad_ip_intf_s_axi`、Lattice 由 `add_axi_interfaces` 自动判定）。
   - 中断接口（Xilinx `ipx::infer_bus_interface irq …`、Intel `add_interface interrupt_sender interrupt`、Lattice `add_interface … -vlnv {spiritconsortium.org:busdef.interrupt:…}`）。
2. 在 [axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile) 中用三种颜色分别标出 `GENERIC_DEPS`、`XILINX_DEPS`+`XILINX_LIB_DEPS`、`INTEL_DEPS`+`LATTICE_DEPS`，目测哪一家的「依赖行数最多」，并解释原因（提示：源码嵌入 vs 引用已打包 IP）。
3. 写一段总结，回答这个问题：**如果让你为 ADI 新增第四家厂商（假设叫 `VendorX`）的支持，你需要新增/修改哪几类文件？**（参考答案骨架：① 新增 `library/scripts/adi_ip_vendorx.tcl` 公共原语库；② 为每个要支持的 IP 新增 `*_vx.tcl` 打包脚本；③ 在各 IP 的 `Makefile` 增加 `VENDORX_DEPS` 桶；④ 在 `library.mk` 增加 `vendorx` 目标与对应产物规则；⑤ 在 `lattice_tool_set.mk` 之类文件声明驱动工具；⑥ 必要时在 `adi_env.tcl` 增加版本号。）

**预期产出**：一张完整的三厂商对照表 + 一段关于「扩展到第四家厂商」的简短设计草案。这能检验你是否真正把「打包 = 描述源码/参数/接口/约束 + 一套厂商专属载体」这一抽象吃透了。

---

## 6. 本讲小结

- **Intel `hw.tcl`** 是「在 Qsys 内被解释执行的活脚本」：用 `adi_ip_intel.tcl` 的封装描述静态骨架，用 `ELABORATION_CALLBACK`/`VALIDATION_CALLBACK` 做动态条件化；约束用 `.sdc`；库阶段只 `touch .timestamp_intel`，真正解析发生在 Qsys 运行时。
- **Lattice `ltt.tcl`** 是「纯 `tclsh` 声明式构建 XML 树」：`ipl` 命名空间把描述累加进 `$ip`，`ipl::generate_ip` 序列化成 `metadata.xml` 等 lsccip 文件；条件逻辑用 Python 表达式（`-editable`/`-dangling`）替代回调；产物给 Radiant/Propel 消费。
- **三家共性**：描述的都是同一组信息（源码/参数/接口/约束），参数集合高度重合，说明「IP 的可配置面」厂商无关。
- **三家差异**集中在载体（`component.xml` / `hw.tcl` / `metadata.xml`）、执行模型（静态 / 回调 / 声明式）、条件逻辑语法（XPath / Tcl 回调 / Python 表达式）、约束后缀（`.xdc` / `.sdc` / `.ldc`）。
- **跨库复用不对称**：Xilinx 引用别人已打包的 `component.xml`（`XILINX_LIB_DEPS` 只写库名），Intel/Lattice 把 util 模块源码扁平嵌入（`INTEL_DEPS`/`LATTICE_DEPS` 显式列源文件），这直接体现在 `axi_dmac/Makefile` 里。
- **RTL 向最严格厂商妥协**：Xilinx 脚本里的 `dummy_axi_ports`（「keep the Intel tools happy」）说明三家共用同一份 RTL 时，RTL 会被迫容纳 Intel 的限制，再由 Xilinx 侧隐藏多余端口。

## 7. 下一步学习建议

本讲把「IP 库系统」单元的三家厂商打包机制讲完了。建议接下来：

1. **横向读完一个 IP 的三套脚本**：挑一个比 `axi_dmac` 更简单的 IP（如 `library/util_axis_fifo/`），把它的 `_ip.tcl` / `_hw.tcl` / `_ltt.tcl` 三件套对照通读，巩固本讲的对照表。
2. **进入数据通路**：下一单元（[u5-l1 axi_dmac 深入](u5-l1-axi-dmac.md)）会钻进 `axi_dmac` 的内部架构（`data_mover`、请求/响应管理器、src/dest 通道），届时你会看到本讲描述的那些接口（`m_src_axi`、`s_axis`、`fifo_wr` 等）在 RTL 里究竟连到了什么逻辑。
3. **关注公共工具库**：[u4-l4 library/common](u4-l4-common-utilities.md) 会讲解 `ad_mem.v`、`up_axi.v` 等被三家打包脚本反复引用的基础 RTL，补齐「源码侧」的拼图。
4. **若对 Lattice 感兴趣**：可继续阅读 `adi_ip_lattice.tcl` 中本讲未展开的 `ipl::create_interface` / `ipl::generate_interface`（自定义总线/抽象定义的生成），理解 `bus_interface.xml` 与 IP-XACT 抽象定义的关系。
