# IP 打包与 Vivado 集成

## 1. 本讲目标

通过本讲，读者应该能够：

- 说清楚一个 Vivado 自定义 IP 是怎么从一堆 HDL 文件「打包」出来的，理解打包流程在 IP 开发闭环（编码 → 仿真 → **打包** → 驱动）中的位置。
- 读懂本项目的打包脚本 [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl)，并能按顺序列出「初始化 → 加源 → 加库 → 加驱动 → 加 GUI 参数 → 清理接口 → 设置可选端口 → `package_ip`」这一整套 PsiIpPackage 命令序列。
- 理解 GUI 参数页是怎么从 `package.tcl` 声明、再落到 Vivado 原生 [xgui/axi_mm_reader_v1_0.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl) 的，以及 GUI 参数如何映射到 VHDL generic。
- 解释**为什么 `m_axis` 端口只在 `Output_g == "AXIS"` 时才出现**——也就是端口/接口的「条件启用」机制。

本讲不深入 RTL 内部逻辑（那是 u2 的事），只关心「IP 外壳是怎么定义和生成的」。

## 2. 前置知识

在开始之前，先用通俗语言建立几个本讲必须的概念。

### 2.1 什么是 Vivado 自定义 IP

在 Vivado 里，设计通常用 **Block Design（BD）** 搭积木：把一个个 **IP 核**（比如 ZYNQ 的 PS、AXI DMA、GPIO）拖进来连线。Vivado 自带的 IP 叫「官方 IP」。如果你自己写了一段 VHDL/Verilog，想让它也能像官方 IP 那样被拖进 BD、拥有配置界面、可以被别人复用，你就需要把它**打包成一个自定义 IP**。

打包后的 IP 由一组标准化的描述文件组成，这套描述格式叫 **IP-XACT**（一个 IEEE 标准，本仓库里对应 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml)）。`component.xml` 就像这个 IP 的「户口本」，记录了：

- 这个 IP 有哪些源文件；
- 顶层实体有哪些**端口**和**接口**（比如 `s00_axi`、`m00_axi`、`m_axis`）；
- 有哪些可配置**参数**（generic）；
- 用哪个器件族、要不要综合；
- 配套的 GUI 脚本、驱动脚本、BD 钩子脚本在哪里。

> 提示：`component.xml` 的内部细节很多，本讲只在「打包产物」层面使用它，字段级精读留到 u3-l4（专家层）。

### 2.2 什么是 PsiIpPackage

Vivado 原生提供了一套打包 TCL 命令（`create_ip`、`add_files`、`set_property`、`ipx::package_ip` ……），但这些命令又长又碎。PSI 团队写了一个封装库 **PsiIpPackage**，把这些原生命令包成了一组更短、更语义化的命令（`init`、`add_sources_relative`、`gui_add_parameter`、`package_ip` 等）。

所以本项目的打包脚本读起来很像「写菜谱」，而不是「调用一堆底层 API」。使用前要先 `source` 这个库并导入命名空间，这样新命令才可用。PsiIpPackage 属于开发期依赖（见 u1-l1 提到的外部依赖），通过 `psi_fpga_all` 或 `scripts/dependencies.py` 获取。

### 2.3 Generic、GUI 参数、可选端口

- **Generic（类属参数）**：VHDL 实体声明里 `generic (...)` 段的常量，比如本 IP 的 `Output_g`、`MaxRegCount_g`。它们在**综合时**就定死了，决定生成出来的硬件长什么样。
- **GUI 参数**：用户在 Vivado 里双击 IP 看到的那个配置窗口里的可填项。GUI 参数的值最终会传给对应的 generic。
- **可选端口（optional port）**：顶层实体的某些端口可以根据某个参数的取值「出现或不出现」。本 IP 的 `m_axis` 就是一个例子——只在选 AXIS 输出时才存在。

理解了这三点，后面 `package.tcl` 在做什么就一目了然了。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 角色 | 本讲用它说明什么 |
| --- | --- | --- |
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl) | 打包脚本（PsiIpPackage 命令序列） | 整个打包流程、加源/加库/加驱动、GUI 参数声明、可选端口条件 |
| [xgui/axi_mm_reader_v1_0.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl) | Vivado 原生 GUI 脚本 | GUI 页面如何构建、参数回调、GUI 参数到 generic 的映射 |
| [hdl/axi_mm_reader_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd) | 顶层 wrapper（IP 的顶层实体） | `Output_g` generic、`m_axis` 端口、`g_axis`/`g_naxis` generate 块——解释「可选」在 RTL 侧的根源 |
| [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) | Block Design 钩子脚本 | BD 集成时 AXI4 `ID_WIDTH` 的自动传播（打包产物的下游使用） |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml) | IP-XACT 描述（打包产物） | 作为 `package_ip` 的产物，本讲只在概念层提及 |

承接 u1-l2 的结论：本仓库采用**核心 + wrapper** 分层，`axi_mm_reader_wrp.vhd` 才是真正的 AXI4 接口边界，也是**打包时的顶层实体**。打包脚本加进去的三个 HDL 文件里，wrapper 就是顶层。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **PsiIpPackage 打包流程**——`package.tcl` 的命令序列。
2. **GUI 参数页**——`package.tcl` 声明 + `xgui/*.tcl` 实现。
3. **可选端口的条件启用**——`m_axis` 为何只在 AXIS 模式出现。

### 4.1 模块一：PsiIpPackage 打包流程

#### 4.1.1 概念说明

打包一个 IP，本质上就是回答 Vivado 一连串问题：

- 这个 IP 叫什么名字、版本号多少、归哪个厂商/库？
- 它的源文件有哪些？依赖哪些第三方库？
- 它配套哪些软件驱动文件？
- 用户可配置哪些参数？
- 有哪些端口/接口是要条件出现的？
- 生成到哪个目录、要不要综合、目标器件是什么？

PsiIpPackage 把这些「问答」整理成了一条线性的命令流水线。本项目里这条流水线就写在 `scripts/package.tcl` 中。读完它，你就掌握了「从 HDL 到一个可被 Vivado IP Catalog 识别的自定义 IP」的全部步骤。

#### 4.1.2 核心流程

`package.tcl` 的执行顺序可以归纳为下面这条流水线（每一步都对应脚本里一段连续的命令）：

```
source PsiIpPackage.tcl + import 命名空间      ← 让 psi::ip_package::latest::* 可用
        │
        ▼
init / set_description / set_vendor / ...      ← 声明 IP 元信息
        │
        ▼
add_sources_relative  (3 个本项目 HDL)          ← 加源文件
        │
        ▼
add_lib_relative      (psi_common 9 个文件)     ← 加第三方库
        │
        ▼
add_drivers_relative  (axi_mm_reader.c/.h)      ← 加软件驱动
        │
        ▼
gui_add_page + gui_create_parameter + ...       ← 声明 GUI 参数页
        │
        ▼
remove_autodetected_interface Rst               ← 清理误检接口
        │
        ▼
add_port_enablement_condition / add_interface_enablement_condition   ← 可选端口
        │
        ▼
package_ip  (目标目录, 编辑开关, 综合开关, 器件)  ← 正式打包，产出 component.xml + xgui
```

整个流程**没有循环、没有分支**，是一条直线，这正是 PsiIpPackage 的设计意图——把打包写成线性的「清单」。

#### 4.1.3 源码精读

**(a) 引入 PsiIpPackage 命令**

[scripts/package.tcl:11-12](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L11-L12) 先 `source` 进 PsiIpPackage 库，再用 `namespace import` 把 `psi::ip_package::latest::*` 下所有命令引入当前作用域。注意 `latest` 表示「用最新版的打包命令」，这样以后 PsiIpPackage 升级也能跟着走。

**(b) 声明 IP 元信息**

[scripts/package.tcl:17-29](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L17-L29) 设定 IP 名 `axi_mm_reader`、版本 `1.0`、版本号策略 `auto`（自动递增）、IP 库 `PSI`、描述文字，以及厂商信息和 datasheet/logo 的相对路径。其中 `init` 是 PsiIpPackage 的「开场命令」，它会在内部创建一个空的 IP 工程骨架。

**(c) 加入本项目源文件**

[scripts/package.tcl:36-40](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L36-L40) 用 `add_sources_relative` 把三个 HDL 文件加入 IP。注意顺序：先 `definitions_pkg.vhd`（共享常量包），再 `axi_mm_reader.vhd`（核心逻辑），最后 `axi_mm_reader_wrp.vhd`（wrapper，即顶层）。wrapper 排最后是因为它例化了前两者。**这三个文件就是整个 IP 的全部自有 RTL**，对应 u1-l2 说的「核心 + wrapper」分层。

**(d) 加入 psi_common 库**

[scripts/package.tcl:43-55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L43-L55) 用 `add_lib_relative` 加入 `psi_common` 库目录下的 **9 个**文件。`add_lib_relative` 和 `add_sources_relative` 的区别在于：库文件会被标记为「属于某个引用库」，在 IP-XACT 里以引用（reference）形式记录，而不是当作本 IP 的源文件。这里加入的 `psi_common_axi_slave_ipif`、`psi_common_axi_master_simple`、`psi_common_sync_fifo`、`psi_common_tdp_ram` 等正是 wrapper 里实际例化的器件（u2 会精读）。

> 小提示：路径里的 `../../../VHDL/psi_common/hdl` 是相对路径，指向 `psi_fpga_all` 仓库中的 psi_common。这也是为什么打包前必须先用 `scripts/dependencies.py` 把依赖拉到正确的相对位置。

**(e) 加入软件驱动**

[scripts/package.tcl:61-64](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L61-L64) 用 `add_drivers_relative` 把 `drivers/axi_mm_reader/src/` 下的 `axi_mm_reader.c` 和 `axi_mm_reader.h` 加进 IP。这样打包出来的 IP 在 Vitis 里生成 BSP 时，就能带上这套 C 驱动（详见 u3-l1）。

**(f) 正式打包**

[scripts/package.tcl:112-114](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L112-L114) 是收尾命令：

- 目标目录 `..`（即仓库根目录，`component.xml` 等会生成在这里）；
- `false`：不打开 GUI 编辑器（脚本化批量打包）；
- `true`：综合开关打开，打包时先跑一遍综合验证；
- `xc7a*`：目标器件族是 Xilinx Artix-7（通配符表示整个 7 系列都支持）。

执行完 `package_ip`，仓库根目录下的 `component.xml` 和 `xgui/` 就更新好了。

#### 4.1.4 代码实践

**实践目标**：把 `package.tcl` 翻译成一份「人能读」的打包步骤清单，确认每一步对应的命令。

**操作步骤**：

1. 打开 [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl)。
2. 按脚本从上到下，把每一段以 `#` 开头的英文注释标题（如 `General Information`、`Add Source Files`、`Driver Files`、`GUI Parameters`、`Optional Ports`、`Package Core`）抄下来作为清单项。
3. 在每一项后面，写下它对应的 PsiIpPackage 命令（如 `init`、`add_sources_relative`、`add_lib_relative`、`add_drivers_relative`、`gui_add_parameter`、`package_ip`）。

**需要观察的现象 / 预期结果**：

- 你应该得到一份 7～8 步的线性清单，顺序与本讲 4.1.2 的流程图一致。
- 你应该能指出：自有源文件有 3 个、psi_common 文件有 9 个、驱动文件有 2 个。
- 器件族应记录为 Artix-7（`xc7a*`）。

> 是否真的能跑通 `package_ip`，**待本地验证**（需要本机装好 Vivado 与 PsiIpPackage，并先用 `dependencies.py` 拉齐 `psi_common` 等依赖）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `package.tcl` 第 39 行的 `axi_mm_reader_wrp.vhd` 从源文件列表里删掉，打包会发生什么？

**参考答案**：wrapper 是 IP 的顶层实体（它例化了核心 `axi_mm_reader`，并对外暴露 `s00_axi`/`m00_axi`/`m_axis` 等接口）。删掉它后，Vivado 找不到正确的顶层，要么报「找不到顶层实体」，要么把核心 `axi_mm_reader`（只有简化的 IPIC 接口）误当顶层，导致对外 AXI4 接口全部消失。所以 wrapper 必须作为顶层加入。

**练习 2**：`add_lib_relative` 与 `add_sources_relative` 的本质区别是什么？为什么 psi_common 要用前者？

**参考答案**：`add_sources_relative` 把文件登记为「本 IP 自己的源」，会随 IP 一起被复制/综合；`add_lib_relative` 把文件登记为「引用的外部库」，在 IP-XACT 里以库引用形式记录，便于多个 IP 共享同一份 psi_common、避免重复与版本混乱。psi_common 是被很多 PSI IP 共用的公共库，所以用引用方式更合理。

---

### 4.2 模块二：GUI 参数页

#### 4.2.1 概念说明

用户在 Vivado 里双击这个 IP，会弹出一个**配置窗口**，里面有若干可填/可选项，比如 s00_axi 地址宽度、时钟频率、超时时间、每周期读寄存器数、缓冲周期数、输出类型。这些项就是 **GUI 参数**。

GUI 参数有两个「住所」：

1. **`package.tcl` 里的声明**：用 PsiIpPackage 命令描述「有哪些参数、叫什么、范围/下拉选项是什么」。这是**作者视角**。
2. **`xgui/axi_mm_reader_v1_0.tcl`**：Vivado 原生的 GUI 实现脚本，定义页面布局、参数回调、以及 GUI 参数到 VHDL generic 的映射。这是 **Vivado 视角**，通常是 `package_ip` 生成的产物（也随仓库一起提交）。

两者其实是「同一件事的两种表达」：你改 `package.tcl` 的声明，重新 `package_ip` 后，`xgui/*.tcl` 会跟着更新。

#### 4.2.2 核心流程

GUI 参数的声明与生效流程：

```
gui_add_page "Configuration"                      ← 建一个名为 Configuration 的页
   │
   ├─ gui_create_parameter <name> <description>   ← 声明一个参数（含说明文字）
   ├─ [gui_parameter_set_range <min> <max>]       ← 可选：设数值范围
   ├─ [gui_parameter_set_widget_dropdown {...}]   ← 可选：设成下拉选项
   └─ gui_add_parameter                           ← 把参数真正加到当前页
        │
        ▼
package_ip  ──生成──►  xgui/axi_mm_reader_v1_0.tcl
                        ├─ init_gui               （构建页面 + 控件）
                        ├─ update_PARAM_VALUE.X   （参数变化回调）
                        ├─ validate_PARAM_VALUE.X （参数校验回调）
                        └─ update_MODELPARAM_VALUE.X （GUI 参数 → VHDL generic）
```

关键点：`gui_create_parameter` 只是「造一个参数对象」，**必须再调一次 `gui_add_parameter` 才会把它放进页面**。这个两步式 API 容易让初学者漏掉第二步。

#### 4.2.3 源码精读

**(a) 在 package.tcl 中声明参数页与各参数**

[scripts/package.tcl:72](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L72) 先建页 `Configuration`，随后逐个声明参数：

- [scripts/package.tcl:74-76](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L74-L76)：`AxiSlaveAddrWidth_g`，用 `gui_parameter_set_range 8 24` 限定范围 **8～24**。
- [scripts/package.tcl:78-79](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L78-L79)：`ClkFrequencyHz`，无范围约束（自由填）。
- [scripts/package.tcl:81-83](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L81-L83)：`TimeoutUs_g`，范围 **1～10000**。
- [scripts/package.tcl:85-86](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L85-L86)：`MaxRegCount_g`，无范围约束。
- [scripts/package.tcl:88-90](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L88-L90)：`MinBuffers_g`，无范围约束。
- [scripts/package.tcl:92-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L92-L94)：`Output_g`，用 `gui_parameter_set_widget_dropdown {"AXIMM" "AXIS"}` 设成**下拉框**，只有两个选项。

这些参数名（如 `AxiSlaveAddrWidth_g`、`Output_g`）与 wrapper 实体里的 generic **完全同名**，这是 GUI 能正确传值给 RTL 的前提。

**(b) 在 xgui 脚本中构建页面**

真正的 GUI 构建逻辑在 [xgui/axi_mm_reader_v1_0.tcl:2-14](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L2-L14) 的 `init_gui` 过程里：先 `ipgui::add_param -name "Component_Name"` 放上 IP 名，再用 `ipgui::add_page` 创建 `Configuration` 页，然后用一连串 `ipgui::add_param -parent ${Configuration}` 把 6 个参数依次加到该页下。注意 [xgui/axi_mm_reader_v1_0.tcl:11](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L11) 给 `Output_g` 指定了 `-widget comboBox`，正好对应 `package.tcl` 里声明的下拉框。（注意区分：`xgui` 文件里的过程叫 `init_gui`，而 [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) 里的过程叫 `init`/`pre_propagate`/`propagate`，是 BD 钩子，名字相近但作用不同。）

**(c) GUI 参数 → VHDL generic 的映射**

这是最关键的一类回调。以 `Output_g` 为例，[xgui/axi_mm_reader_v1_0.tcl:100-103](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L100-L103) 的 `update_MODELPARAM_VALUE.Output_g` 把 GUI 上 `PARAM_VALUE.Output_g` 的值写进 `MODELPARAM_VALUE.Output_g`——后者就是最终传给 RTL generic 的「模式参数」。其余 5 个 generic（`ClkFrequencyHz`、`TimeoutUs_g`、`MaxRegCount_g`、`MinBuffers_g`、`AxiSlaveAddrWidth_g`）在 [xgui/axi_mm_reader_v1_0.tcl:80-108](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L80-L108) 里各有一段完全对称的映射代码。

此外还有两类回调（本 IP 里基本是空壳）：`update_PARAM_VALUE.X`（当依赖参数变化时更新本参数，[xgui/axi_mm_reader_v1_0.tcl:16-77](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L16-L77)）和 `validate_PARAM_VALUE.X`（校验，全部 `return true`）。本 IP 没有做参数间联动校验，所以这些过程体为空。

> 注意一个细节：xgui 文件里还出现了 `C_S00_AXI_ID_WIDTH` 相关的回调（[xgui/axi_mm_reader_v1_0.tcl:25-32](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L25-L32) 与 [110-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L110-L113)）。这个参数不在 `package.tcl` 的 GUI 列表里——它对应 wrapper 的 `C_S00_AXI_ID_WIDTH` generic，由 BD 钩子 [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) 在 Block Design 里自动传播（见 4.3 与 u3-l4），用户不需要在 GUI 里手填。

#### 4.2.4 代码实践

**实践目标**：建立一个「参数 → 默认值 → 范围/选项 → 作用」的对照表，并理解 GUI→generic 的映射链。

**操作步骤**：

1. 在 `package.tcl` 的 72～94 行中，提取每个参数的「范围」或「下拉选项」。
2. 到 [hdl/axi_mm_reader_wrp.vhd:27-35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L27-L35) 查每个 generic 的**默认值**。
3. 拼成一张表（示例第一行已给出）：

| 参数（generic） | 默认值（wrapper） | GUI 范围/选项 | 作用 |
| --- | --- | --- | --- |
| `ClkFrequencyHz` | `100_000_000` | 自由填 | 用于计算超时计数值 |
| `TimeoutUs_g` | `100` | 1～10000 | 多久没触发就自动开始读 |
| `MaxRegCount_g` | `1024` | 自由填 | 每周期最多读多少个寄存器 |
| `MinBuffers_g` | `4` | 自由填 | FIFO 预留多少个完整读周期 |
| `Output_g` | `"AXIMM"` | {AXIMM, AXIS} | 输出方式（决定有无 m_axis） |
| `AxiSlaveAddrWidth_g` | `14` | 8～24 | s00_axi 地址宽度 |

4. 在 `xgui` 文件里定位 `update_MODELPARAM_VALUE.Output_g`，确认它把 GUI 值写给同名 MODELPARAM。

**需要观察的现象 / 预期结果**：

- 6 个 GUI 参数与 6 个 wrapper generic **一一同名**。
- `Output_g` 是唯一一个下拉框参数，也是唯一一个会影响「端口是否存在」的参数。
- 注意默认值 `Output_g = "AXIMM"`，意味着**默认打包出来的 IP 没有 `m_axis` 端口**（见 4.3）。

> 这部分是纯源码阅读型实践，不需要运行 Vivado 即可完成；表格内容**可直接对照源码核对**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `AxiSlaveAddrWidth_g` 有 `8～24` 的范围限制，而 `MaxRegCount_g` 没有？

**参考答案**：地址宽度受 AXI 协议与寄存器空间大小的物理约束（太小放不下寄存器表，太大浪费连线），所以给了合理上下限；`MaxRegCount_g` 决定的是内部 RAM/FIFO 深度，理论上可大可小，约束较少，故未在 GUI 限定（实际仍受器件资源限制）。文档还给出地址宽度的下限要求：至少为 \(\lceil \log_2(\text{MaxRegisters} \times 4 + 32) \rceil\)（见 [doc/Documentation.md](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md)）。

**练习 2**：`update_MODELPARAM_VALUE.X` 和 `update_PARAM_VALUE.X` 这两类回调分别什么时候被 Vivado 调用？

**参考答案**：`update_PARAM_VALUE.X` 在「**任何依赖参数发生变化时**」被调用，用于联动更新本参数的取值（本 IP 未使用，故为空）；`update_MODELPARAM_VALUE.X` 在「**需要把 GUI 参数值下发给 RTL**」时被调用，它把 `PARAM_VALUE.X` 的当前值写到 `MODELPARAM_VALUE.X`，从而真正改变综合时的 generic 取值。前者管「GUI 内部一致性」，后者管「GUI → 硬件」。

---

### 4.3 模块三：可选端口的条件启用

#### 4.3.1 概念说明

这是本讲最精妙的一处，也直接对应实践任务里的问题：**为什么 `m_axis` 端口只在 `Output_g == "AXIS"` 时出现？**

先回顾 u1-l1：本 IP 有两种输出方式——

- **AXIS**：读回的寄存器值经 `m_axis` 端口以 AXI-Stream 直出；
- **AXIMM**：读回的值映射到寄存器空间的 FIFO，软件通过读 `RdData`/`RdLast` 寄存器获取，**没有 `m_axis` 端口**。

问题在于：wrapper 的 VHDL 实体里，`m_axis_tdata/tvalid/tready/tlast` 这 4 个端口**永远存在**（见下方源码）。如果不做任何处理，那么用户即使选了 AXIMM 模式，打包出来的 IP 也会带一排没人用的 `m_axis` 引脚，既丑陋又容易接错。

解决办法就是 PsiIpPackage 的**条件启用（enablement condition）**：告诉 Vivado「这几个端口、这个接口，只有当 `Output_g` 取某个值时才存在」。Vivado 在用户改 GUI 选项时，会实时求值这个条件表达式，决定端口是否出现在 Block Design 里。

#### 4.3.2 核心流程

```
用户在 GUI 把 Output_g 选成 AXIS 或 AXIMM
        │
        ▼
Vivado 求值条件表达式:  $Output_g == "AXIS"
        │
   ┌────┴─────┐
   ▼          ▼
 为真         为假(AXIMM)
   │          │
   ▼          ▼
m_axis 端口    m_axis 端口
和接口出现     和接口隐藏
```

这个「条件」在打包阶段由 `add_port_enablement_condition` / `add_interface_enablement_condition` 写进 `component.xml`，在 BD 使用阶段被 Vivado 实时求值。注意：RTL 侧 wrapper 里另有 `g_axis` / `g_naxis` 两个 generate 块来保证「端口隐藏时内部逻辑也对」，三层（component.xml 条件、GUI 参数、RTL generate）必须一致，缺一不可。

#### 4.3.3 源码精读

**(a) RTL 侧：端口与 generic 的根源**

wrapper 实体在 [hdl/axi_mm_reader_wrp.vhd:118-121](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L118-L121) 声明了 4 个 `m_axis` 端口，`Output_g` generic 在 [hdl/axi_mm_reader_wrp.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L31) 声明为 `string := "AXIMM"`（注意默认值是 AXIMM）。这两个就是条件表达式里 `Output_g` 与 `m_axis` 的出处。

**(b) RTL 侧：两种模式的 generate 块**

[hdl/axi_mm_reader_wrp.vhd:170-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L184) 用两个互斥 generate 实现：

- `g_axis : if Output_g = "AXIS" generate` —— AXIS 模式，把内部 `AxiS_*` 信号接到 `m_axis_*` 端口；
- `g_naxis : if Output_g /= "AXIS" generate` —— AXIMM 模式，把 FIFO 数据映射到 `reg_rdata`（软件读寄存器），并把 `m_axis_tvalid <= '0'`。

也就是说，**即便端口存在**，RTL 内部也会按 `Output_g` 选一套接法。这是硬件层面的「二选一」。

**(c) 打包侧：端口/接口的条件启用**

真正让 `m_axis` 在 AXIMM 模式「消失」的是这两行：

[scripts/package.tcl:106](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L106) 用 `add_port_enablement_condition m_axis_* "$Output_g == \"AXIS\""` 让**所有名字以 `m_axis_` 开头的端口**（即 `m_axis_tdata/tvalid/tready/tlast`）仅在 AXIS 时启用；[scripts/package.tcl:107](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L107) 用 `add_interface_enablement_condition m_axis "$Output_g == \"AXIS\""` 让**名为 `m_axis` 的 AXI-Stream 接口**整体仅在 AXIS 时启用。

> 注意区别：端口（port）是物理引脚，接口（interface）是 Vivado 把一组相关端口捆成的「总线」（这里是 AXI-Stream）。要让 BD 里整个 `m_axis` 总线干净地消失，端口和接口两条条件都要设。

**(d) 顺带：清理误检接口**

[scripts/package.tcl:100](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L100) 的 `remove_autodetected_interface Rst` 也是一种「接口整理」：Vivado 看到顶层有个叫 `Rst` 的端口，会自作主张给它生成一个「复位接口」。但本 IP 的复位是公共时钟域的一部分，不需要单独成一个接口，所以用这条命令把它删掉，保持对外接口干净。

#### 4.3.4 代码实践

**实践目标**：解释清楚「`m_axis` 为何只在 AXIS 出现」，并追踪条件在三个层面的一致性。

**操作步骤**：

1. 读 [hdl/axi_mm_reader_wrp.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L31)：确认 `Output_g` 是 string，默认 `"AXIMM"`。
2. 读 [hdl/axi_mm_reader_wrp.vhd:170-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L184)：确认 RTL 用两个 generate 块分别处理 AXIS / 非 AXIS。
3. 读 [scripts/package.tcl:106-107](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L106-L107)：确认打包侧用端口条件 + 接口条件两行。
4. 用一句话回答：为什么 `m_axis` 只在 AXIS 出现？

**需要观察的现象 / 预期结果**：

- 三处条件表达式都指向同一个判断 `Output_g == "AXIS"`（RTL 用 `=`，TCL 用 `==`）。
- 因为默认 `Output_g = "AXIMM"`，所以**默认打包**（不做任何 GUI 选择）时 `m_axis` 是**不出现**的；只有用户在 GUI 下拉框里选 AXIS 才会出现。
- `g_naxis` 块里 `m_axis_tvalid <= '0'`，是为了在端口万一存在的退化情况下，让输出保持安静（防御性写法）。

> 实际在 Vivado GUI 里切换 `Output_g` 观察 `m_axis` 的出现/消失，**待本地验证**（需 Vivado 环境）。

#### 4.3.5 小练习与答案

**练习 1**：如果不设 `add_interface_enablement_condition`，只设 `add_port_enablement_condition`，会有什么后果？

**参考答案**：端口本身会被正确隐藏，但 Vivado 仍然可能把残留的接口定义挂在外面，导致 Block Design 里出现一个「空壳」`m_axis` 接口或连线告警。端口和接口是两个层面的概念，通常需要同时声明条件，BD 体验才干净。

**练习 2**：`Output_g` 的表达式里，`$Output_g` 前的 `$` 是什么意思？为什么字符串比较要用转义的引号 `\"AXIS\"`？

**参考答案**：`$` 是 TCL 的变量替换符，`$Output_g` 表示「取当前 GUI 参数 `Output_g` 的值」。因为 `AXIS` 是字符串字面量，需要在表达式里用双引号包起来；而整个表达式本身又处在 TCL 双引号字符串里，所以内层双引号必须用 `\"` 转义，避免提前结束外层字符串。最终 Vivado 求值时看到的是 `Output_g 的值 == "AXIS"`。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**「读懂一次打包，并尝试给 IP 加一个新参数」**。这个任务把三个最小模块串起来。

**任务背景**：假设你想给这个 IP 加一个 GUI 参数 `LedPulseOnDone_g`（布尔，决定 `DoneIrq` 完成时是否额外输出一个脉冲展宽），你需要弄清楚要在哪些地方动刀。

**步骤**：

1. **梳理现有打包清单**：按 4.1 的流程，写出 `package.tcl` 当前的 8 步命令序列（初始化 → 加源 → 加库 → 加驱动 → GUI 页 → 清理接口 → 可选端口 → package_ip）。
2. **定位要改的文件**：
   - RTL 侧：在 wrapper 实体 [hdl/axi_mm_reader_wrp.vhd:27-35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L27-L35) 的 generic 段新增一个 `LedPulseOnDone_g : std_logic := '1'`。
   - 打包侧：在 [scripts/package.tcl:72-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L72-L94) 的 GUI 参数段，照葫芦画瓢加一段 `gui_create_parameter` + `gui_add_parameter`。
   - 重新跑 `package_ip` 后，确认 [xgui/axi_mm_reader_v1_0.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl) 自动多出对应的 `init_gui`/`update_MODELPARAM_VALUE` 条目。
3. **回答两个关键问题**（写进你的笔记）：
   - 这个新参数需要不需要 `add_port_enablement_condition`？为什么？（答：不需要，它不增删端口，只影响内部行为。）
   - 为什么说 GUI 参数名必须和 generic 名**完全相同**？（答：见 4.2.3(c)，`update_MODELPARAM_VALUE` 是按名字一对一映射的。）

**预期产出**：一份「新增参数改动清单」（文件 + 每个文件的关键改动点），以及一段对 `package.tcl` 8 步流程的中文说明。

> 本任务是「设计 + 源码阅读」型实践，不要求真的综合出比特流；若要在 Vivado 中实测打包效果，**待本地验证**。

## 6. 本讲小结

- 打包一个 Vivado IP = 把 HDL + 库 + 驱动 + GUI 参数 + 接口规则写成一份 IP-XACT 描述（`component.xml`），本项目用 PsiIpPackage 把它简化成了一条线性命令流水线。
- [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl) 的顺序是：`init` 元信息 → `add_sources_relative` 自有 RTL（3 个，wrapper 为顶层）→ `add_lib_relative` psi_common（9 个）→ `add_drivers_relative` C 驱动 → GUI 参数页 → `remove_autodetected_interface Rst` → 可选端口条件 → `package_ip`（输出到 `..`，综合开，器件 `xc7a*`）。
- GUI 参数有两个住所：`package.tcl` 里用 PsiIpPackage 声明（`gui_create_parameter`/`gui_add_parameter`），`xgui/*.tcl` 里是 Vivado 原生实现（`init_gui` 建页、`update_MODELPARAM_VALUE.X` 把 GUI 值映射到同名 VHDL generic）。6 个 GUI 参数与 6 个 wrapper generic 一一同名。
- `m_axis` 端口只在 `Output_g == "AXIS"` 时出现，是靠 `add_port_enablement_condition` + `add_interface_enablement_condition` 两行条件实现的；RTL 侧另有 `g_axis`/`g_naxis` generate 块配套。三层（component.xml 条件、GUI 参数、RTL generate）必须一致。
- 默认 `Output_g = "AXIMM"`，所以默认打包出来的 IP **没有** `m_axis` 端口；`C_S00_AXI_ID_WIDTH` 不在 GUI 里，由 BD 钩子 [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) 自动传播。
- 改 GUI 参数的「正规」入口是 `package.tcl`，改完重新 `package_ip`，`xgui/*.tcl` 和 `component.xml` 会自动更新。

## 7. 下一步学习建议

- 本讲只看了「IP 外壳」。下一单元 u2 会**进入 RTL 内部**：建议先读 [u2-l1 整体架构与数据流](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd)，建立从 `s00_axi` 到 `m00_axi` 再到 `m_axis`/FIFO 的完整数据通路心智模型。
- 想深挖 GUI 与参数化的约束（地址宽度下限、各 generic 的物理含义），留到 u3-l3「参数化与 GUI 配置」展开。
- 想了解 [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl) 里 `init`/`pre_propagate`/`propagate` 如何在 BD 不同时机自动传递 AXI4 `ID_WIDTH`，以及 `component.xml` 字段级细节，留到 u3-l4「IP-XACT 打包产物与 Block Design 集成」。
- 建议随手翻一遍仓库根目录的 `component.xml`，对照本讲提到的「源文件、接口、参数」去找对应字段，建立「TCL 声明 ↔ XML 记录」的直觉。
