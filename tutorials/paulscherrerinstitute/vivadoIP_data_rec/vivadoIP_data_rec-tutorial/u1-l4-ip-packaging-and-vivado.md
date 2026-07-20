# IP 打包与在 Vivado 中使用

## 1. 本讲目标

前三讲我们认识了 `data_rec` 是什么、代码放在哪、怎么跑仿真。但仿真只是开发阶段的事——真正要把这个 IP 核放进 Xilinx 的 Vivado 工具链、让其他工程师像使用官方 IP 一样拖进 Block Design 里配置使用，还差一步：**打包成 Vivado IP（IP-XACT 格式）**。

本讲学完后，你应该能够：

- 说清 `scripts/package.tcl` 这个打包脚本从头到尾做了哪几件事，以及它依赖的 `PsiIpPackage` 框架扮演什么角色。
- 读懂 `component.xml`（IP-XACT / SPIRIT 标准）里的厂商身份、总线接口、端口使能条件、参数默认值和文件集，并理解「可选端口按 generic 条件使能」是如何用一段依赖表达式实现的。
- 读懂 `xgui/data_rec_v2_4.tcl` 这个 GUI 脚本的三类过程（`init_gui` / `update_PARAM_VALUE` / `update_MODELPARAM_VALUE`），明白 Vivado 参数面板上的每个文本框是怎么和 VHDL generic 对应起来的。
- 理解一个核心工程实践：**同一个 generic（如 `NumOfInputs_g`）会在打包脚本、`component.xml`、`xgui` 三个地方被「镜像」，改 IP 时三处必须保持一致**。

## 2. 前置知识

本讲假设你已经读过 u1-l2（仓库目录结构与外部依赖），知道以下事实：

- 本仓库是 PSI FPGA 库家族的一员，依赖 `psi_common`（综合必需）、`psi_tb`（仅仿真）、`PsiSim`（仅仿真流程）、`PsiIpPackage`（仅打包发布）四个外部库。
- 仓库必须放在约定的相对路径下：根目录下 `VivadoIp/vivadoIP_data_rec/`，同级还有 `TCL/PsiIpPackage/` 和 `VHDL/psi_common/`。这一点在本讲的打包脚本里会再次被印证。

在进入源码之前，先解释几个关键术语：

- **IP 核（IP Core）**：一段可复用的硬件设计模块。`data_rec` 就是一个 IP 核。
- **Vivado IP**：Xilinx Vivado 工具能识别的、带配置界面、能拖进 Block Design 使用的 IP 核。它需要一套标准化的元数据来描述自己。
- **IP-XACT / SPIRIT**：一种 XML 标准（IEEE 1685-2009，前身叫 SPIRIT），用于描述 IP 的接口、参数、文件。`component.xml` 就是这个标准的实现文件。你会在本讲看到它的根命名空间是 `spirit:`。
- **generic（泛型）**：VHDL 里类似「编译期参数」的东西。`NumOfInputs_g := 4` 表示「实例化时这个 IP 有 4 个数据通道」。Vivado 的 GUI 参数最终会被传递成 VHDL generic。
- **AXI4 Slave**：Xilinx 主推的总线协议，本 IP 通过它让 CPU（如 Zynq 的 ARM 核）读写寄存器和存储。它的五个通道（读地址/读数据/写地址/写数据/写响应）会在 u2-l1 详讲，本讲只需知道打包时要把这些信号声明成一个总线接口。

一句话建立直觉：**`package.tcl` 是「菜谱」，`component.xml` 是按菜谱炒出来的「成品说明书」，`xgui/*.tcl` 是这份说明书附带的「配置面板脚本」**。三者描述同一个 IP，只是分工不同。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl) | IP 打包脚本（菜谱） | 如何声明名称/版本、加入源码、定义 GUI 参数、设置可选端口使能条件、调用 `package_ip` 产出 IP |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml) | IP-XACT 描述（成品说明书） | 厂商身份、AXI 总线接口、时钟/复位/中断接口、可选端口使能表达式、参数默认值、文件集 |
| [xgui/data_rec_v2_4.tcl](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl) | Vivado 参数面板脚本 | `init_gui` 如何布局参数页、`update_PARAM_VALUE` / `update_MODELPARAM_VALUE` 三类过程的作用 |

辅助理解（非本讲精读对象，但会被引用）：

- [hdl/data_rec_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd) 的 entity，是打包后 IP 的**顶层模块**，它的 generic 和 port 就是 `component.xml` 描述的对象。
- [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/Changelog.md)，记录 v2.4 新增了可选的 `Trig_Out` 端口——这正是本讲「可选端口按 generic 使能」的最佳案例。

## 4. 核心概念与源码讲解

本讲的三个最小模块对应三个文件：打包脚本、IP-XACT 描述、参数 GUI。

### 4.1 IP 打包脚本：scripts/package.tcl

#### 4.1.1 概念说明

手写一份合规的 `component.xml`（一千七百多行 XML）既枯燥又容易出错。PSI 的做法是：**写一个简短的 TCL 脚本来描述「我要打包一个什么样的 IP」，然后由 `PsiIpPackage` 框架自动生成那一大坨 XML**。

`scripts/package.tcl` 就是这样一个脚本。它本身只有一百多行，但它通过调用 `PsiIpPackage` 提供的高层命令（如 `init`、`add_sources_relative`、`gui_create_parameter`、`add_port_enablement_condition`、`package_ip`）来工作。这些命令都来自 `psi::ip_package::latest` 命名空间。

这套框架的价值在于：

- **屏蔽 IP-XACT 的繁琐细节**：你只需声明「我有个 generic 叫 `NumOfInputs_g`，范围 1 到 8」，框架自动生成对应的 XML 片段。
- **与文件夹结构解耦**：Changelog v1.1.2 专门提到「改了打包脚本，让它不依赖库文件夹上层的目录结构」，靠的就是 `add_sources_relative` / `add_lib_relative` 这种相对路径声明。
- **可重复、可版本化**：脚本是纯文本，进 git，任何人 checkout 后都能重新打包出完全一致的 IP。

#### 4.1.2 核心流程

`package.tcl` 的执行顺序可以归纳为八步：

1. **载入框架**：`source` 进 `PsiIpPackage.tcl`，导入命令命名空间。
2. **声明身份**：用 `init` 设置 IP 名（`data_rec`）、版本（`2.4`）、库名（`GPAC3`），再设置描述、logo、数据手册。
3. **加入自有源码**：`add_sources_relative` 列出本仓库 `hdl/` 下的三个 RTL 文件。
4. **加入依赖库源码**：`add_lib_relative` 列出要随 IP 一起打包的 `psi_common` 文件（相对路径上跳三层）。
5. **定义 GUI 参数**：`gui_add_page` 建一个参数页，`gui_create_parameter` 逐个声明用户可调的 generic，设置范围/控件类型。
6. **修正自动检测的接口**：删除并重新添加 `Rst`、`Clk` 接口，把 AXI 总线接口关联到正确的时钟（因为 Vivado 自动检测会把极性和时钟关联搞错）。
7. **声明可选端口使能条件**：用 `add_port_enablement_condition` 告诉框架「`In_Data4` 这个端口只在 `NumOfInputs_g > 4` 时才出现」。
8. **执行打包**：`package_ip` 把以上声明综合成最终 IP，输出到上一级目录。

可以用下面的伪代码概括：

```
source  PsiIpPackage 框架
init(name=data_rec, version=2.4, library=GPAC3)
set_description / set_logo / set_datasheet
add_sources_relative(本仓库 3 个 .vhd)
add_lib_relative(psi_common 的 9 个 .vhd)
gui_add_page("Configuration")
  for each generic: gui_create_parameter + (可选)设范围 + gui_add_parameter
remove/re-add Rst、Clk 接口；关联 AXI 时钟
add_port_enablement_condition(In_Data0..7  依赖 NumOfInputs_g)
add_port_enablement_condition(Trig_Out    依赖 TrigForwarding_g)
package_ip(targetDir="..", edit=false, synth=true)
```

#### 4.1.3 源码精读

**(a) 载入框架**

[scripts/package.tcl:10-11](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L10-L11) 载入 `PsiIpPackage` 并把它的命令导入当前作用域。注意路径 `../../../TCL/PsiIpPackage/PsiIpPackage.tcl` 从 `scripts/` 出发上跳三层到仓库根，再到 `TCL/PsiIpPackage/`——这正是 u1-l2 所说的「本仓库须位于 `<根目录>/VivadoIp/vivadoIP_data_rec/`」的直接证据。

**(b) 声明身份**

[scripts/package.tcl:16-25](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L16-L25) 设置四个关键变量并调用 `init`：

- `IP_NAME = data_rec`、`IP_VERSION = 2.4`、`IP_REVISION = "auto"`（让框架自动算修订号）、`IP_LIBRARY = GPAC3`。
- 描述字符串里有个原文笔误「*Mutli* channel」，但它在 `component.xml` 里也被原样保留，说明描述确实是脚本生成的。
- `set_datasheet_relative "../doc/$IP_NAME.pdf"` 把 `doc/data_rec.pdf` 挂到 IP 的「Data Sheet」视图——这就是为什么 u1-l1 强调「PDF 是权威」。

**(c) 加入源码**

[scripts/package.tcl:32-36](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L32-L36) 列出三个自有 RTL 文件（注意 `data_rec_register_pkg` 排在最前，因为它被另外两个 `use`）。[scripts/package.tcl:39-51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L39-L51) 列出 9 个 `psi_common` 文件，路径同样是 `../../../VHDL/psi_common/hdl`。这 9 个就是 IP 综合时真正需要的全部公共库子集（math/array/logic 包、若干跨时钟域和 RAM、AXI slave 接口、流水寄存器）。

**(d) 定义 GUI 参数**

[scripts/package.tcl:58-80](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L58-L80) 是用户最常打交道的部分：

| 命令 | 参数名 | 面板显示名 | 范围/控件 |
|------|--------|-----------|----------|
| `gui_parameter_set_range` | `NumOfInputs_g` | Data Channels | 1 ~ 8 |
| `gui_parameter_set_range` | `InputWidth_g` | Data Channel Width | 1 ~ 32 |
| （无范围） | `MemoryDepth_g` | Recording Buffer size | 任意正整数 |
| `gui_parameter_set_range` | `TrigInputs_g` | Number of trigger inputs | 0 ~ 8 |
| （无范围） | `C_S00_AXI_ADDR_WIDTH` | Axi address width in bits | 任意 |
| `gui_parameter_set_widget_checkbox` | `TrigForwarding_g` | Enable data recored trigger out port | 复选框（布尔） |

注意 `TrigInputs_g` 的范围是 **0~8**，区别于数据通道数 `NumOfInputs_g` 的 1~8——这呼应了 u1-l1 提到的「外部触发路数与数据通道数是两回事」。`MemoryDepth_g` 没有设范围，所以面板上可以填任意正整数（包括非二次幂，这正是 v2.3.2 修复回绕 bug 的那条路径）。

**(e) 修正接口与时钟关联**

[scripts/package.tcl:83](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L83) 注释写得很直白：「Vivado messes up polarity...（Vivado 会搞错复位极性）」，所以先 `remove_autodetected_interface Rst` 删掉自动检测的复位接口（后续由 AXI 的 `aresetn` 接口接管，低有效）。

[scripts/package.tcl:88-92](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L88-L92) 处理时钟：`Clk`（数据时钟）被自动关联错了，所以删掉重新 `add_clock_in_interface`；再用 `set_interface_clock s00_axi s00_axi_aclk` 把 AXI 总线接口 `s00_axi` 显式关联到 AXI 时钟 `s00_axi_aclk`。Changelog v2.3.1「Removed wrong clock association between Clk and s00_axi」正是修的这件事。

**(f) 可选端口使能条件（本讲重点）**

[scripts/package.tcl:98-102](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L98-L102) 用一个 `for` 循环为 8 个数据端口各写一条使能条件，再单独给 `Trig_Out` 写一条：

```tcl
for {set i 0} {$i < 8} {incr i} {
    add_port_enablement_condition "In_Data$i" "\$NumOfInputs_g > $i"
}
add_port_enablement_condition "Trig_Out" "\$TrigForwarding_g = true"
```

意思是：

- `In_Data0` 在 `NumOfInputs_g > 0`（即至少 1 通道）时存在；
- `In_Data4` 在 `NumOfInputs_g > 4`（即 5~8 通道）时存在；
- `Trig_Out` 仅当勾选 `TrigForwarding_g`（v2.4 新增）时才存在。

这就是为什么「在 Block Design 里把 `NumOfInputs_g` 从 4 改成 6，端口数量会跟着变」——条件是声明在 IP 元数据里的，Vivado 据此重新生成端口。

**(g) 执行打包**

[scripts/package.tcl:107-109](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L107-L109) 调用 `package_ip $TargetDir false true`：目标目录是上一级 `..`，第一个布尔（`Edit`）为 `false`（不在打包后打开可编辑工程），第二个布尔（`Synth`）为 `true`（跑一次综合来「固化」接口推断，确保端口方向、位宽被正确识别）。

#### 4.1.4 代码实践（源码阅读型）

> 本实践不需要 Vivado，只需阅读脚本。

1. **实践目标**：验证「同一个 generic 的范围/默认值会在脚本→XML→GUI 三处保持一致」，建立「改 IP 要三处同步」的肌肉记忆。
2. **操作步骤**：
   - 打开 [scripts/package.tcl:60-73](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L60-L73)，记下 `NumOfInputs_g`、`InputWidth_g`、`TrigInputs_g` 的范围。
   - 打开 [hdl/data_rec_vivado_wrp.vhd:27-30](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L27-L30)，对照 VHDL entity 里的 `range ... to ...`，确认完全一致。
3. **需要观察的现象**：脚本里的 `gui_parameter_set_range 1 8` 应与 VHDL 的 `range 1 to 8` 一一对应；`TrigForwarding_g` 在 VHDL 里是 `boolean`，在脚本里用了 `gui_parameter_set_widget_checkbox`（复选框），类型也对应。
4. **预期结果**：三处定义完全吻合。若将来有人改了 VHDL 的范围却忘了改脚本，IP 面板就会放过非法值，综合时才报错——这就是「三处同步」必要性的来源。
5. 由于本环境无 Vivado，运行结果待本地验证；阅读结论可直接得出。

#### 4.1.5 小练习与答案

**练习 1**：如果要把数据通道上限从 8 扩到 16，`package.tcl` 里至少要改哪几处？

**参考答案**：至少两处——`gui_parameter_set_range` 把 `NumOfInputs_g` 的上限从 8 改成 16（[scripts/package.tcl:61](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L61)），以及可选端口循环 `{set i 0} {$i < 8}` 改成 `$i < 16`（[scripts/package.tcl:98](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L98)）。当然还要同步改 VHDL entity 的 `range 1 to 8`，否则 RTL 侧根本不支持。

**练习 2**：为什么打包脚本要 `remove_autodetected_interface Rst`？

**参考答案**：因为 Vivado 自动检测会把顶层的 `Rst` 当成一个独立复位接口，并可能猜错极性；而本 IP 实际使用的是 AXI 域的低有效复位 `s00_axi_aresetn`（见 `component.xml` 里的 `ACTIVE_LOW`）。删掉错误的自动接口，才能让复位走正确的 AXI 关联路径（脚本第 83 行注释原话：「Vivado messes up polarity」）。

### 4.2 IP-XACT 描述：component.xml

#### 4.2.1 概念说明

`component.xml` 是 `package.tcl` 跑完之后**自动生成**的产物，也是 Vivado 真正「看」的文件。它遵循 IP-XACT（IEEE 1685-2009，前身 SPIRIT）标准，所以你会看到大量 `spirit:` 前缀的 XML 元素。它虽然很长（本仓库这份有 1700 多行），但结构是清晰的。

需要特别理解的一点：**虽然 `component.xml` 是生成的，但它被 commit 进了仓库**。这意味着即使你本机没有 `PsiIpPackage`，也能直接用这份 XML 把 IP 加进 Vivado 使用；同时也意味着每次改完 `package.tcl` 重新打包后，要记得把更新后的 `component.xml` 一起提交，保持脚本与 XML 一致。

`component.xml` 顶层用四元组唯一定位一个 IP：**vendor（厂商）+ library（库名）+ name（名字）+ version（版本）**，简称 VLNV。对本 IP 而言是 `psi.ch : GPAC3 : data_rec : 2.4`。

#### 4.2.2 核心流程

`component.xml` 由以下几个大块组成：

```
spirit:component
├── busInterfaces        本 IP 暴露的总线/信号接口（AXI、时钟、复位、中断）
│     ├── s00_axi            AXI4 Slave 总线接口（aximm）
│     ├── s00_axi_aresetn    复位接口（ACTIVE_LOW）
│     ├── s00_axi_aclk       AXI 时钟，关联到 s00_axi
│     ├── Done_Irq           中断接口（LEVEL_HIGH）
│     └── Clk                数据时钟
├── memoryMaps           AXI 可访问的地址空间（reg0，范围随地址宽度变）
├── model
│     ├── views              综合/仿真/xgui/数据手册等视图
│     ├── ports              所有物理端口（含可选端口的使能条件）
│     └── modelParameters    generic 的「模型参数」副本（综合时用）
├── fileSets             每个视图包含哪些文件、编译到哪个 VHDL 库
├── parameters           generic 的「用户参数」副本（GUI 上用）
└── vendorExtensions     Xilinx 私有信息（支持的 FPGA 家族、打包版本等）
```

注意 `modelParameters` 和 `parameters` 是**两份副本**：前者是综合时传给 VHDL 的值（`MODELPARAM_VALUE.*`），后者是 GUI 上显示给用户的值（`PARAM_VALUE.*`）。`xgui` 脚本里的 `update_MODELPARAM_VALUE.*` 过程就负责把后者同步到前者（见 4.3）。

#### 4.2.3 源码精读

**(a) VLNV 身份**

[component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L3-L6) 给出 `vendor=psi.ch`、`library=GPAC3`、`name=data_rec`、`version=2.4`，与 `package.tcl` 的 `init` 完全对应。

**(b) AXI4 Slave 总线接口**

[component.xml:8-297](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L8-L297) 定义名为 `s00_axi` 的总线接口：类型是 Xilinx 的 `aximm`（AXI4 memory-mapped），角色是 `slave`（从机），并引用了 `memoryMapRef="s00_axi"`。其 `portMaps` 把逻辑名（如 `AWADDR`/`WDATA`/`BRESP`/`ARVALID`/`RDATA`）一一映射到物理端口（`s00_axi_awaddr` 等）——这就是把几十根散线「捆」成一根总线接口的地方，Vivado 由此能在 Block Design 里把它画成一根粗线连接。

**(c) 时钟关联与中断**

[component.xml:320-345](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L320-L345) 是 `s00_axi_aclk` 接口，关键在参数 `ASSOCIATED_BUSIF = s00_axi`——它告诉 Vivado「这根时钟驱动的是 `s00_axi` 总线」，正是 `package.tcl` 第 92 行 `set_interface_clock` 的产物。

[component.xml:346-367](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L346-L367) 把 `Done_Irq` 声明为中断接口，灵敏度 `LEVEL_HIGH`（高电平有效）。这就是录制完成后 CPU 收中断的来源。

**(d) 地址空间范围（一个数学细节）**

[component.xml:392-405](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L392-L405) 定义 AXI 地址块 `reg0`：基址 `0x0`，宽度 32 位，**范围随地址宽度变化**。依赖表达式是：

\[
\text{range} = 2^{\,(\text{C\_S00\_AXI\_ADDR\_WIDTH} - 1) - 0 + 1} = 2^{\text{C\_S00\_AXI\_ADDR\_WIDTH}}
\]

默认 `C_S00_AXI_ADDR_WIDTH = 14`，所以：

\[
\text{range} = 2^{14} = 16384 = 0\mathrm{x}4000
\]

这正是 XML 里 `<spirit:range>0x4000</spirit:range>` 的由来——这个 IP 默认占用 16 KiB 的 AXI 地址空间（寄存器 + 各通道录制存储都在里面，u2-l2 会展开寄存器地图）。

**(e) 可选端口的使能条件（承接 4.1）**

`component.xml` 的 `spirit:ports` 把 `package.tcl` 里的使能条件落到了 XML 上。以 `In_Data0` 为例，[component.xml:510-536](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L510-L536) 里能看到：

- 端口位宽的 `left` 依赖 `(MODELPARAM_VALUE.InputWidth_g) - 1`，`right` 为 0，所以宽度就是 `InputWidth_g` 位；
- `vendorExtensions` 里有 `<xilinx:isEnabled ... dependency="$NumOfInputs_g > 0">true</xilinx:isEnabled>`。

而 `In_Data4`（[component.xml:636-640](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L636-L640)）的 `isEnabled` 默认是 `false`、依赖 `$NumOfInputs_g > 4`——因为默认 `NumOfInputs_g=4`，所以第 5~8 个数据端口默认不出现。

`Trig_Out`（[component.xml:756-778](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L756-L778)）同理，`isEnabled` 默认 `false`、依赖 `$TrigForwarding_g = true`，对应 v2.4 新增的可选触发转发端口。

**(f) 参数默认值**

[component.xml:1424-1460](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1424-L1460) 是 `modelParameters`，给出每个 generic 的默认值和范围，例如 `NumOfInputs_g` 默认 4（范围 1~8）、`MemoryDepth_g` 默认 128、`TrigForwarding_g` 默认 `false`、`C_S00_AXI_ADDR_WIDTH` 默认 14。这与 `package.tcl` 及 VHDL entity 的默认值再次吻合。

**(g) 文件集与库名**

[component.xml:1476-1627](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1476-L1627) 是 `fileSets`。注意两点：一是综合视图和仿真视图各列了 12 个文件（3 个自有 RTL + 9 个 `psi_common`），与 `package.tcl` 的 `add_sources_relative` / `add_lib_relative` 完全对应；二是每个文件都标了 `<spirit:logicalName>data_rec_2_4</spirit:logicalName>`——**所有文件被编译进名为 `data_rec_2_4` 的 VHDL 库**。这个带版本号的库名是为了避免同一工程里不同版本的 IP 互相冲突。

xgui 文件单独成一个文件集（[component.xml:1604-1612](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1604-L1612)），指向 `xgui/data_rec_v2_4.tcl`——注意文件名里的 `v2_4` 也是随版本走的。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：亲手验证「默认 4 通道」这件事在 `component.xml` 里是如何编码的。
2. **操作步骤**：
   - 在 `component.xml` 中找到 `In_Data0`~`In_Data7` 这 8 个端口的 `<xilinx:isEnabled>` 默认值与 `dependency`（参考 4.2.3 (e) 给出的行号）。
   - 数一下默认值为 `true` 的有几个、为 `false` 的有几个。
3. **需要观察的现象**：`In_Data0`~`In_Data3` 默认 `true`（依赖 `>0/>1/>2/>3`），`In_Data4`~`In_Data7` 默认 `false`（依赖 `>4/>5/>6/>7`）。
4. **预期结果**：恰好 4 个 `true`、4 个 `false`，与 `NumOfInputs_g` 默认值 4 一致。这解释了为什么你刚把 IP 拖进 Block Design 时看到的是 4 个 `In_Data` 端口。
5. 运行环境无关，阅读结论可直接得出。

#### 4.2.5 小练习与答案

**练习 1**：把 `NumOfInputs_g` 在 GUI 里从 4 改成 6 后，`In_Data6`/`In_Data7` 会出现吗？为什么？

**参考答案**：`In_Data6`（依赖 `>6`）不会出现，`In_Data7`（依赖 `>7`）也不会出现，因为 6 不大于 6、6 不大于 7。只有 `In_Data0`~`In_Data5` 这 6 个端口会出现。

**练习 2**：为什么所有源文件的 `logicalName` 都是 `data_rec_2_4` 而不是简单的 `work`？

**参考答案**：Vivado 把每个 IP 的源码编译进一个**以「名字_主版本_次版本」命名的独立 VHDL 库**（`data_rec_2_4` 对应 v2.4）。这样同一个工程里如果同时存在 v2.3 和 v2.4 的 `data_rec`，它们的 `data_rec_register_pkg` 等同名文件不会在 `work` 库里撞名。

### 4.3 参数 GUI：xgui/data_rec_v2_4.tcl

#### 4.3.1 概念说明

在 Vivado 里双击一个 IP，弹出来的那个带文本框、下拉框、复选框的「Re-customize IP」窗口，就是由 `xgui/*.tcl` 脚本驱动的。Xilinx 把这套机制叫 XGUI。

`xgui/data_rec_v2_4.tcl` 这个文件名里的 `v2_4` 同样随版本走——升级到 v2.5 时，`package.tcl` 会生成 `data_rec_v2_5.tcl`，并更新 `component.xml` 里的引用。所以**版本号变更会牵连打包脚本、`component.xml`、xgui 文件名、VHDL 库名四处**，这是升级 IP 时最容易遗漏的地方。

这个脚本里有三类过程（Vivado 在不同时机回调它们）：

- `init_gui`：IP 被打开/刷新时调用，负责把参数摆放到面板页面上。
- `update_PARAM_VALUE.<参数名>` / `validate_PARAM_VALUE.<参数名>`：当某个参数变化时调用。`update` 用来联动修改别的参数，`validate` 用来校验输入合法性（返回布尔）。
- `update_MODELPARAM_VALUE.<参数名>`：把 GUI 参数（`PARAM_VALUE`）的值同步成综合用的模型参数（`MODELPARAM_VALUE`），即真正传给 VHDL generic 的值。

#### 4.3.2 核心流程

```
Vivado 打开 IP
   └─> init_gui(IPINST)
          ├─ 加 Component_Name 参数
          └─ 建页 "Configuration"，把 6 个参数加到该页

用户改了某个参数（如 NumOfInputs_g）
   ├─> validate_PARAM_VALUE.NumOfInputs_g  → 校验合法性
   ├─> update_PARAM_VALUE.NumOfInputs_g    → 联动（本 IP 此处为空）
   └─> update_MODELPARAM_VALUE.NumOfInputs_g → 把 GUI 值写给 VHDL generic

综合时
   └─ Vivado 用 MODELPARAM_VALUE.* 实例化 data_rec_vivado_wrp
```

本 IP 的 `update_PARAM_VALUE.*` 和 `validate_PARAM_VALUE.*` 过程目前都是空壳/恒返回 `true`——也就是说它没有做参数联动校验，校验主要靠 `package.tcl` 里 `gui_parameter_set_range` 设的范围（由 Vivado 框架强制）。真正有实质逻辑的是 `update_MODELPARAM_VALUE.*`。

#### 4.3.3 源码精读

**(a) init_gui 布局**

[xgui/data_rec_v2_4.tcl:2-14](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl#L2-L14) 先加 `Component_Name`（每个 IP 都有的实例名输入框），再建一个名为 `Configuration` 的页，把 6 个参数（`NumOfInputs_g`、`InputWidth_g`、`MemoryDepth_g`、`TrigInputs_g`、`C_S00_AXI_ADDR_WIDTH`、`TrigForwarding_g`）依次加到该页。这与 `package.tcl` 的 `gui_add_page "Configuration"` 和六个 `gui_add_parameter` 完全对应——又一次「三处镜像」。

**(b) update / validate 过程（空壳）**

[xgui/data_rec_v2_4.tcl:16-77](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl#L16-L77) 为每个参数都生成了 `update_PARAM_VALUE.*`（空体）和 `validate_PARAM_VALUE.*`（恒返回 `true`）。这些是 Xilinx 模板生成的占位过程，留作将来加联动逻辑的钩子。例如若想「`InputWidth_g` 不能超过 16 当通道数大于 4」，就会在 `validate_PARAM_VALUE.InputWidth_g` 里加判断。

**(c) MODEL PARAM 同步（实质逻辑）**

[xgui/data_rec_v2_4.tcl:80-113](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl#L80-L113) 是真正干活的代码。以 `NumOfInputs_g` 为例（[xgui/data_rec_v2_4.tcl:80-83](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl#L80-L83)）：

```tcl
proc update_MODELPARAM_VALUE.NumOfInputs_g { MODELPARAM_VALUE.NumOfInputs_g PARAM_VALUE.NumOfInputs_g } {
    set_property value [get_property value ${PARAM_VALUE.NumOfInputs_g}] \
                     ${MODELPARAM_VALUE.NumOfInputs_g}
}
```

含义：把 GUI 参数 `PARAM_VALUE.NumOfInputs_g` 的值读出来，写到模型参数 `MODELPARAM_VALUE.NumOfInputs_g` 上。综合时 Vivado 再用这个 `MODELPARAM_VALUE` 去实例化 VHDL，generic `NumOfInputs_g` 就拿到了用户在面板上填的值。这一条「GUI → MODEL → VHDL generic」的链路就是参数能生效的完整原因。

注意：并不是所有参数都有 `update_MODELPARAM_VALUE` 过程。本文件里有 `NumOfInputs_g`、`InputWidth_g`、`MemoryDepth_g`、`TrigInputs_g`、`TrigForwarding_g`、`C_S00_AXI_ID_WIDTH`、`C_S00_AXI_ADDR_WIDTH` 七个——与 `component.xml` 里 `modelParameters` 的个数一致。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：追踪一个 GUI 参数是如何最终变成 VHDL generic 的值。
2. **操作步骤**：
   - 在 [xgui/data_rec_v2_4.tcl:90-93](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/xgui/data_rec_v2_4.tcl#L90-L93) 找到 `MemoryDepth_g` 的 `update_MODELPARAM_VALUE` 过程。
   - 再到 [hdl/data_rec_vivado_wrp.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_vivado_wrp.vhd#L29) 看 VHDL 里 `MemoryDepth_g : positive := 128`。
   - 想象用户在面板把 `MemoryDepth_g` 填成 256，跟踪数据流。
3. **需要观察的现象**：面板值 256 → `PARAM_VALUE.MemoryDepth_g=256` → `set_property` 写入 `MODELPARAM_VALUE.MemoryDepth_g` → 综合时 VHDL 的 `MemoryDepth_g` 取 256。
4. **预期结果**：整条链路全程靠 `update_MODELPARAM_VALUE.MemoryDepth_g` 这一个过程把 GUI 值「搬」过去。如果有人删掉了这个过程，面板上改 `MemoryDepth_g` 将不会生效（综合仍用默认 128）。
5. 运行环境无关，阅读结论可直接得出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `validate_PARAM_VALUE.NumOfInputs_g` 直接 `return true`，却仍能阻止用户填 0 或 9？

**参考答案**：因为范围的强制约束来自 `package.tcl` 里的 `gui_parameter_set_range 1 8`（被写进 `component.xml` 的 `minimum`/`maximum`），由 Vivado 框架在更早的阶段拦截非法值。`validate` 过程是给「范围之外的复杂联动规则」预留的钩子，本 IP 暂未用到，故恒返回 `true`。

**练习 2**：如果把 IP 从 v2.4 升级到 v2.5，xgui 相关需要改动什么？

**参考答案**：需要新增/重命名 `xgui/data_rec_v2_5.tcl`（或让 `package.tcl` 重新生成），并更新 `component.xml` 里 `xilinx_xpgui_view_fileset` 对 xgui 文件名的引用（[component.xml:1607](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1607)）。同时 VHDL 库名 `data_rec_2_4` 也要相应改成 `data_rec_2_5`。通常做法是只改 `package.tcl` 里的 `IP_VERSION`，再重跑打包让框架自动生成新的 xgui 文件名和库名。

## 5. 综合实践

> 这是一个贯穿三个文件的完整任务，需要在装有 Vivado 与 `PsiIpPackage` 的本机进行。若无环境，可降级为「源码推演」版本（见末尾）。

**任务**：在 Vivado Tcl 控制台中运行 `scripts/package.tcl` 打包 IP，然后在 Block Design 中实例化它，确认 `NumOfInputs_g` 改变时 `In_Data` 端口数量随之变化。

**完整操作步骤（待本地验证）**：

1. **准备依赖目录**：按 u1-l2 的约定，把本仓库放在 `<根目录>/VivadoIp/vivadoIP_data_rec/`，确保 `<根目录>/TCL/PsiIpPackage/`、`<根目录>/VHDL/psi_common/`（≥3.0.0）就位。
2. **打包**：打开 Vivado，把工作目录切到 `scripts/`，在 Tcl Console 执行：
   ```tcl
   cd <根目录>/VivadoIp/vivadoIP_data_rec/scripts
   source package.tcl
   ```
   预期在上一级目录（`vivadoIP_data_rec/`）生成 IP 输出（含 `component.xml` 等）。
3. **加入 IP 仓库**：新建一个工程，在 Settings → IP → Repository 里把 `vivadoIP_data_rec/` 加为 IP 仓库路径，确认能在 IP Catalog 搜到 `data_rec`。
4. **实例化**：新建 Block Design，右键 Add IP → 选 `data_rec`，把它加进画布。
5. **观察默认端口**：双击 IP 打开定制面板，此时 `NumOfInputs_g` 默认为 4，确认画布上 `In_Data0`~`In_Data3` 共 4 个数据端口（`In_Data4`~`In_Data7` 不出现）。
6. **改参数验证**：把 `NumOfInputs_g` 改成 6，点 OK。预期画布上出现 `In_Data0`~`In_Data5` 共 6 个端口；再改成 2，预期只剩 `In_Data0`~`In_Data1`。
7. **验证可选触发转发端口**：勾选 `TrigForwarding_g`，预期新增一个输出端口 `Trig_Out`；取消勾选则消失。

**需要观察的现象与预期结果**：

- `NumOfInputs_g` 每次变化，`In_Data*` 端口数量精确等于该值，端口的使能边界由 `component.xml` 里的 `$NumOfInputs_g > i` 条件决定（4.2.3 (e)）。
- `Trig_Out` 的出现/消失完全由 `TrigForwarding_g` 控制（4.1.3 (f)）。
- 数据端口位宽随 `InputWidth_g` 变化（如设成 12，则 `In_Data*` 是 12 位）。

> 以上运行结果**待本地验证**（本环境无 Vivado/PsiIpPackage）。

**降级版（纯源码推演，立即可做）**：不改任何代码，回答下面三个问题并给出代码行佐证——

1. 默认情况下，IP 会有几个 `In_Data` 端口？→ 4 个（`NumOfInputs_g` 默认 4，见 [component.xml:1428](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1428)）。
2. `NumOfInputs_g=7` 时，哪些端口出现？→ `In_Data0`~`In_Data6`（依赖条件 `>0`~`>6` 均成立，`>7` 不成立，见 [scripts/package.tcl:98-100](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L98-L100)）。
3. `Trig_Out` 默认出现吗？→ 不出现（`TrigForwarding_g` 默认 `false`，见 [component.xml:774](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L774)）。

## 6. 本讲小结

- `scripts/package.tcl` 是 IP 的「菜谱」：它通过 `PsiIpPackage` 框架的高层命令（`init`/`add_sources_relative`/`gui_create_parameter`/`add_port_enablement_condition`/`package_ip`）声明 IP 身份、源码、参数和可选端口，框架据此自动生成上千行 XML。
- `component.xml`（IP-XACT/SPIRIT）是 Vivado 真正读取的「成品说明书」，用 VLNV（`psi.ch:GPAC3:data_rec:2.4`）唯一标识 IP，并把 AXI/时钟/复位/中断接口、端口使能条件、参数默认值、文件集全部编码其中。
- 「可选端口按 generic 使能」是本 IP 灵活配置的核心机制：`In_Data0..7` 依赖 `NumOfInputs_g > i`，`Trig_Out` 依赖 `TrigForwarding_g = true`（v2.4 新增），声明在 `package.tcl`、落地于 `component.xml` 的 `<xilinx:isEnabled dependency="...">`。
- `xgui/data_rec_v2_4.tcl` 驱动 Vivado 定制面板：`init_gui` 摆放参数，`update_MODELPARAM_VALUE.*` 把 GUI 值（`PARAM_VALUE`）同步成综合用的 `MODELPARAM_VALUE`，最终传给 VHDL generic。
- **核心工程教训**：同一个 generic 在 `package.tcl`、`component.xml`、`xgui/*.tcl`（以及 VHDL entity）四处被镜像，改 IP 时必须同步；版本号升级还会牵连 xgui 文件名（`v2_4`）和 VHDL 库名（`data_rec_2_4`）。
- AXI 默认地址范围由 `C_S00_AXI_ADDR_WIDTH` 决定，默认 14 位对应 \(2^{14}=16384=0\mathrm{x}4000\) 字节，即 16 KiB。

## 7. 下一步学习建议

本讲解决的是「IP 怎么被打包、参数怎么配」，但还没有真正看**端口和寄存器具体是什么含义**。建议接着学：

- **u2-l1 顶层封装端口与 AXI4 Slave 接口**：把本讲提到的 `s00_axi_*` 五个通道、`Done_Irq`、`Trig_Out` 端口语义逐个讲透，理解数据时钟域与 AXI 时钟域的划分。
- **u2-l2 寄存器与存储地址地图**：展开本讲算出的那 16 KiB 地址空间，看 `data_rec_register_pkg` 如何定义每个寄存器地址和存储寻址函数。

如果你已经迫不及待想看「录制本身」的机制，可以跳到第 3 单元（u3-l1 起）阅读 `data_rec.vhd` 核心；但建议先过完 u2，建立端口与地址的全局索引，再读核心 RTL 会顺畅很多。
