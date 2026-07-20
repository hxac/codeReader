# Vivado IP 封装流程

## 1. 本讲目标

本讲是进阶单元的第二篇，承接 u3-l1（顶层 wrapper 三实例架构），回答一个工程化问题：**一套纯 VHDL 源码，如何变成 Vivado IP Catalog 里可以拖进 Block Design、带图形参数界面、带 C 驱动、能被版本管理的「IP 核」？**

学完后你应当能够：

- 说清楚 `scripts/package.tcl` 这份「封装配方」分哪几步把源码、依赖库、驱动、GUI 参数装配成一个 IP，并最终调用 `package_ip` 产出成品。
- 看懂 `xgui/mem_test_v1_2.tcl` 里三类 TCL 过程（`init_gui` / `update_PARAM_VALUE`·`validate_PARAM_VALUE` / `update_MODELPARAM_VALUE`）各司其职，以及 GUI 参数如何逐级映射到 VHDL generic。
- 读懂 `component.xml` 作为 IP-XACT（IEEE 1685）描述文件的结构：总线接口、地址空间、端口、文件集、参数分别描述了 IP 的哪些侧面。
- 在三个文件基础上，独立设计「新增一个 GUI 参数」所需的全部改动。

## 2. 前置知识

### 2.1 什么是「封装一个 IP」

Vivado 里的 IP（Intellectual Property core）是一段可复用的硬件设计，用户在 Block Design（BD）里像搭积木一样把它拖进来、连线、改参数即可使用。为了让 Vivado 能「读懂」一个第三方 RTL 设计，需要提供两类东西：

1. **机器可读的元数据**：告诉 Vivado 这个 IP 叫什么、有哪些端口、哪些参数、依赖哪些文件、端口属于哪类总线（AXI4？AXI-Lite？时钟？）。这部分用 **IP-XACT** 标准（IEEE 1685，旧称 SPIRIT）描述，本项目里就是那个近 2130 行的 `component.xml`。
2. **图形参数界面（GUI）脚本**：用户双击 IP 弹出的「Re-customize IP」对话框由一段 TCL 脚本动态搭建，本项目里就是 `xgui/mem_test_v1_2.tcl`。

直接手写 `component.xml` 既冗长又易错，因此 PSI 公共库提供了一个 TCL 封装框架 **PsiIpPackage**（见 u1-l2 依赖清单），用少量高层命令帮你生成这两份产物。`scripts/package.tcl` 就是调用这个框架的「配方脚本」。

### 2.2 参数的三级映射

封装里最容易混淆的是「参数」有三个层次，名字相同但命名空间不同：

| 层次 | 命名空间示例 | 作用 | 谁来用 |
|------|--------------|------|--------|
| GUI 可改参数 | `PARAM_VALUE.C_M00_AXI_DATA_WIDTH` | 用户在 Vivado 对话框里能看到的值 | xgui / BD |
| 综合模型参数 | `MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH` | 综合时传给顶层 VHDL 的值 | Vivado 综合 |
| VHDL generic | `C_M00_AXI_DATA_WIDTH` | wrapper 实体里的泛型（见 u3-l1） | RTL |

只要三层**同名**，PsiIpPackage / Vivado 会自动把它们串起来。本讲的反复主线就是这条映射链。

> 名词解释：
> - **IP-XACT**：一种 XML 格式标准，`component.xml` 里 `spirit:` 前缀即其命名空间（SPIRIT 联盟 → IEEE 1685-2009）。
> - **fileSet（文件集）**：component.xml 里按「用途」分组的文件清单，例如综合用、仿真用、驱动用各一组。
> - **generic**：VHDL 实体的泛型，相当于可参数化的编译期常量。

## 3. 本讲源码地图

| 文件 | 角色 | 大小量级 |
|------|------|----------|
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl) | **输入**：封装配方脚本，调用 PsiIpPackage 框架 | 约 100 行 |
| [xgui/mem_test_v1_2.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl) | **产物（可手调）**：图形参数界面 TCL | 约 85 行 |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml) | **产物**：IP-XACT 元数据描述 | 约 2130 行 |
| [hdl/mem_test_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd) | 被封装的顶层实体，generic 名是参数映射的终点 | — |
| [bd/bd.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/bd/bd.tcl) | Block Design 里的 AXI ID_WIDTH 自动传播回调（u5-l3 详讲） | — |

一句话关系：**`package.tcl` 是「源」，`component.xml` 和 `xgui/*.tcl` 是它跑出来的「产物」并被一起提交进仓库**；修改 RTL 或参数后重跑 `package.tcl` 即可重新生成产物。

---

## 4. 核心概念与源码讲解

### 4.1 PsiIpPackage 封装脚本

#### 4.1.1 概念说明

`scripts/package.tcl` 是一份纯 TCL 脚本，约定在 Vivado Tcl Console 里 `cd` 到 `scripts/` 目录后 `source package.tcl` 运行。它本身不含任何 Vivado 底层 `ipx::*` 命令，而是先 `source` 进 PSI 的封装框架，再用一组语义化高层命令描述「我要封装的 IP 长什么样」，最后由框架翻译成 Vivado 的打包动作。

这样做的好处是：把「声明 IP 长什么样」和「Vivado 版本相关的打包细节」解耦——当 Vivado 升级、`ipx::*` 命令变动时，只需更新 PsiIpPackage 框架，各 IP 的 `package.tcl` 基本不动。

#### 4.1.2 核心流程

`package.tcl` 的执行可以划成 **6 个有序阶段**：

```text
1. 载入框架      source PsiIpPackage.tcl ; namespace import
2. 基本信息初始化 init / set_description / set_logo / set_datasheet
3. 声明源文件     add_sources_relative  (本项目 RTL)
4. 声明依赖库     add_lib_relative      (psi_common 里的 8 个 .vhd)
5. 声明驱动       add_drivers_relative  (C 驱动 src/)
6. 声明 GUI 参数  gui_add_page / gui_create_parameter / gui_add_parameter
   ───────────  最后一步  ───────────
7. package_ip $TargetDir $Edit $Synth $Part   → 生成成品到 ../
```

注意：所有「声明」命令都只是**往框架的内部数据结构里登记**，真正落盘发生在最后的 `package_ip`。

#### 4.1.3 源码精读

**(a) 载入框架** — [scripts/package.tcl:10-11](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L10-L11)：`source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl` 再把 `psi::ip_package::latest::*` 命令导入当前命名空间。这里的 `../../../` 正是 u1-l2 讲过的「公共根目录」相对路径——从 `scripts/` 上溯三级到公共根，再下钻进 `TCL/PsiIpPackage/`。

**(b) 基本信息初始化** — [scripts/package.tcl:16-25](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L16-L25)：

```tcl
set IP_NAME mem_test
set IP_VERSION 1.2
set IP_REVISION "auto"
set IP_LIBRARY DBPM3
...
init $IP_NAME $IP_VERSION $IP_REVISION $IP_LIBRARY
set_description $IP_DESCIRPTION
set_logo_relative "../doc/psi_logo_150.gif"
set_datasheet_relative "../doc/$IP_NAME.pdf"
```

这 4 个变量与产物有严格对应关系（可对照 `component.xml` 头部验证）：

| package.tcl 变量 | 值 | 对应 component.xml 字段 |
|------------------|----|--------------------------|
| IP_NAME / IP_VERSION | mem_test / 1.2 | `<spirit:name>mem_test</spirit:name>`、`<spirit:version>1.2</spirit:version>` |
| IP_LIBRARY | DBPM3 | `<spirit:library>DBPM3</spirit:library>` |
| IP_REVISION | auto | 框架自动填 `<xilinx:coreRevision>`（产物里是时间戳 `1564732205`） |

IP_NAME+IP_VERSION 还决定了产物文件名：xgui 脚本叫 `mem_test_v1_2.tcl`、综合库名叫 `mem_test_1_2`（下划线替点号）。

**(c) 声明源文件** — [scripts/package.tcl:32-36](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L32-L36)：`add_sources_relative` 列出本仓库自己的三个 RTL 文件（`mem_test_pkg.vhd` / `mem_test.vhd` / `mem_test_wrapper.vhd`），路径相对 `scripts/` 用 `../hdl/`。

**(d) 声明依赖库** — [scripts/package.tcl:39-50](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L39-L50)：`add_lib_relative` 第一个参数是依赖库目录 `"../../../VHDL/psi_common/hdl"`，第二个参数是真正需要打进 IP 的 8 个 `psi_common` 文件清单（含 AXI 主从机、FIFO、RAM、流水线寄存器等，正是 u3-l1 wrapper 三实例里用到的那些）。**只有被列出的文件才会进 IP，不是整个 psi_common 都打包**——这控制了 IP 体积。

**(e) 声明驱动** — [scripts/package.tcl:56-59](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L56-L59)：`add_drivers_relative ../drivers/mem_test {src/mem_test.c src/mem_test.h}` 登记了 u2-l3 讲过的 C 驱动两个源文件，框架会把它俩连同 `data/mem_test.mdd`、`data/mem_test.tcl`、`src/Makefile` 一起归入 IP 的「Software Driver」文件集（见 4.3.3）。

**(f) 声明 GUI 参数** — [scripts/package.tcl:67-83](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L67-L83)：先建一个参数页 `gui_add_page "AXI-M"`，再用「三连」依次创建 4 个参数：

```tcl
gui_create_parameter "C_M00_AXI_DATA_WIDTH" "AXI-M data width"
gui_parameter_set_range 16 256
gui_add_parameter
```

「三连」含义：`gui_create_parameter <名> <显示名>` 声明一个参数（名字必须与 wrapper generic 完全一致，见 u3-l1），`gui_parameter_set_range <min> <max>` 限定取值范围，`gui_add_parameter` 把它加进当前页。4 个参数与 wrapper 4 个 generic 一一对应：

| GUI 参数名 | 取值范围 | wrapper 默认值 |
|------------|----------|----------------|
| C_M00_AXI_DATA_WIDTH | 16–256 | 64 |
| C_M00_AXI_ADDR_WIDTH | 16–64 | 32 |
| C_M00_AXI_MAX_BURST_SIZE | 1–256 | 16 |
| C_M00_AXI_MAX_OPEN_TRANS | 0–8 | 2 |

注意 wrapper 还有第 5 个 generic `C_S00_AXI_ID_WIDTH`，但 **package.tcl 里并没有为它调用 `gui_create_parameter`**——它是由 AXI4 接口的标准参数自动提取、并由 `bd/bd.tcl` 在 Block Design 中自动传播的（详见 u5-l3），所以不暴露成用户可改参数。

**(g) 打包** — [scripts/package.tcl:93-95](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L93-L95)：

```tcl
set TargetDir ".."
#						Edit  	Synth	Part
package_ip 	$TargetDir 	false 	true	xczu9eg-ffvb1156-2-e
```

`package_ip` 是最后的「落盘」动作，4 个参数含义：目标目录 `..`（即仓库根，所以 `component.xml` 落在根目录）、`Edit=false`（不打开可编辑项目）、`Synth=true`（打包时跑一次综合以校验 IP 可综合）、目标器件 `xczu9eg-ffvb1156-2-e`（一颗 Zynq UltraScale+）。跑完之后，仓库根的 `component.xml`、`xgui/mem_test_v1_2.tcl` 就被刷新。

#### 4.1.4 代码实践：核对相对路径与产物对应关系

这是一个**源码阅读型实践**，无需安装 Vivado。

1. **实践目标**：验证 package.tcl 里的「声明」与产物 component.xml 的「结果」一一对应，确认你对封装流程的理解。
2. **操作步骤**：
   - 打开 [scripts/package.tcl:39-50](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L39-L50)，记下列出的 8 个 psi_common 文件名。
   - 打开 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml) 的综合文件集（约第 1883 行起的 `xilinx_anylanguagesynthesis_view_fileset`），核对这 8 个文件是否都在、且 `../../VHDL/psi_common/hdl/` 前缀是否吻合。
   - 对照 package.tcl 第 16–20 行的 `IP_NAME/IP_VERSION/IP_LIBRARY` 与 component.xml 第 3–6 行的 `vendor/library/name/version`。
3. **需要观察的现象**：声明清单与产物文件集应当完全吻合；名称字段应一一对应（`mem_test` / `1.2` / `DBPM3`）。
4. **预期结果**：8 个 psi_common 文件全部出现在综合文件集中；`psi.ch : DBPM3 : mem_test : 1.2` 四元组在两份文件里一致。
5. 若你装了 Vivado 与依赖库，可进 `scripts/` 执行 `source package.tcl`，观察 Vivado 控制台是否按「init → add → gui → package_ip」顺序打印日志——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add_lib_relative` 的目录写成 `../../../VHDL/psi_common/hdl` 而不是直接 `psi_common/hdl`？
**答**：因为 `package.tcl` 从 `scripts/` 目录被 source，必须先上溯三级到 u1-l2 讲的「公共根目录」，再下钻到并列的 `VHDL/psi_common/hdl`。这是整个 PSI 库「公共根 + 相对路径」约定的体现。

**练习 2**：如果把 `C_M00_AXI_MAX_OPEN_TRANS` 的取值上限从 8 改成 16，需要改 package.tcl 的哪一行？产物里哪个文件会随之变化？
**答**：改 [scripts/package.tcl:82](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L82) 的 `gui_parameter_set_range 0 8`；重跑 `package_ip` 后，`component.xml` 里 `PARAM_VALUE.C_M00_AXI_MAX_OPEN_TRANS` 的 `spirit:maximum` 会从 8 变为 16。

---

### 4.2 xgui 参数界面

#### 4.2.1 概念说明

`xgui/mem_test_v1_2.tcl` 是 Vivado 在用户**打开 IP 的「Re-customize IP」对话框时**动态执行的脚本。它定义了对话框长什么样、参数变化时如何联动、参数合法性如何校验。它由 `package.tcl` 的 `gui_*` 命令生成，但因为是 TCL 源码，也可以（且经常需要）手工微调。

这份脚本里只有 `proc`（过程），没有顶层执行语句。Vivado 在特定时机按命名约定去调用它们。

#### 4.2.2 核心流程

xgui 脚本里的过程分 **三类**，按 Vivado 调用时机排列：

```text
打开对话框时：
  init_gui { IPINST }            ── 搭页面、摆参数控件

任意参数变化时（每个参数各一对）：
  update_PARAM_VALUE.X { ... }   ── 据依赖关系刷新别的参数
  validate_PARAM_VALUE.X { ... } ── 校验 X 合法性，return true/false

参数值最终落到综合模型时（每个参数各一个）：
  update_MODELPARAM_VALUE.X { MODEL PARAM } ── 把 PARAM_VALUE 拷给 MODELPARAM_VALUE
```

关键是命名约定：过程名里的 `X` 必须是参数全名（如 `C_M00_AXI_DATA_WIDTH`），Vivado 据此自动找到并调用对应过程。

#### 4.2.3 源码精读

**(a) `init_gui` 搭界面** — [xgui/mem_test_v1_2.tcl:2-12](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L2-L12)：

```tcl
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  set AXI-M [ipgui::add_page $IPINST -name "AXI-M"]
  ipgui::add_param $IPINST -name "C_M00_AXI_DATA_WIDTH" -parent ${AXI-M}
  ...
}
```

先加 `Component_Name`（每个 IP 都有的实例名输入框），再建一个名为 `AXI-M` 的页面（对应 package.tcl 里的 `gui_add_page "AXI-M"`），最后把 4 个参数控件挂到该页下。页名与控件名由 `package.tcl` 的 GUI 段决定，这里只是把它们「画」出来。

**(b) `update_PARAM_VALUE` / `validate_PARAM_VALUE`** — 以数据宽度为例，[xgui/mem_test_v1_2.tcl:23-30](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L23-L30)：

```tcl
proc update_PARAM_VALUE.C_M00_AXI_DATA_WIDTH { PARAM_VALUE.C_M00_AXI_DATA_WIDTH } {
	# Procedure called to update ... when any of the dependent parameters ... change
}

proc validate_PARAM_VALUE.C_M00_AXI_DATA_WIDTH { PARAM_VALUE.C_M00_AXI_DATA_WIDTH } {
	# Procedure called to validate C_M00_AXI_DATA_WIDTH
	return true
}
```

`update_*` 过程体留空，说明本参数**不依赖其他参数**、无需联动；`validate_*` 恒返回 `true`，说明合法性完全交给 package.tcl 里声明的取值范围（16–256）去兜底。如果将来加一个「超时阈值」并要求它必须是某值的整数倍，就应在这里写校验逻辑并 `return false` 拦截非法值。

**(c) `update_MODELPARAM_VALUE`：三级映射的桥** — [xgui/mem_test_v1_2.tcl:65-68](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L65-L68)：

```tcl
proc update_MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH { MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH PARAM_VALUE.C_M00_AXI_DATA_WIDTH } {
	set_property value [get_property value ${PARAM_VALUE.C_M00_AXI_DATA_WIDTH}] ${MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH}
}
```

这一行就是把用户在 GUI 改的 `PARAM_VALUE.C_M00_AXI_DATA_WIDTH` 的值，拷贝到综合用的 `MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH`。由于本 IP 的 GUI 值就是最终 generic 值，所以是直接搬运；但在有「GUI 显示值 ≠ 综合值」换算需求的 IP 里（例如 GUI 用 MHz、综合用 ns），这里就是做单位换算的地方。综合时 Vivado 会把 `MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH` 作为名为 `C_M00_AXI_DATA_WIDTH` 的 generic 传给 `mem_test_wrapper`——这就回到了 u3-l1 的 wrapper 实体泛型。

> 小贴士：`C_S00_AXI_ID_WIDTH` 在 xgui 里也有 `update/validate_PARAM_VALUE` 与 `update_MODELPARAM_VALUE`（[第 50–63 行](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L50-L63)），但它并非来自 package.tcl 的 `gui_create_parameter`，而是 AXI4 从机接口的标准参数被框架自动补全的，配合 `bd/bd.tcl` 在 BD 里自动传播（u5-l3）。

#### 4.2.4 代码实践：跟踪一个参数的三级跳

1. **实践目标**：以 `C_M00_AXI_ADDR_WIDTH` 为例，把 2.2 节的「三级映射」在真实源码里走一遍。
2. **操作步骤**：
   - 在 [package.tcl:73-75](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L73-L75) 找到它的声明与范围（16–64）。
   - 在 [xgui/mem_test_v1_2.tcl:14-21](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L14-L21) 看 `update/validate_PARAM_VALUE.C_M00_AXI_ADDR_WIDTH`。
   - 在 [xgui/mem_test_v1_2.tcl:70-73](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl#L70-L73) 看 `update_MODELPARAM_VALUE.C_M00_AXI_ADDR_WIDTH`。
   - 在 [hdl/mem_test_wrapper.vhd:21](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L21) 确认 generic 同名。
3. **需要观察的现象**：四级（GUI 声明 → PARAM_VALUE → MODELPARAM_VALUE → VHDL generic）名字完全一致，全是 `C_M00_AXI_ADDR_WIDTH`。
4. **预期结果**：名字一致即映射自动成立，无需任何手动「接线」。
5. 本实践为静态阅读，**无需运行**。

#### 4.2.5 小练习与答案

**练习 1**：`validate_PARAM_VALUE.C_M00_AXI_DATA_WIDTH` 恒返回 `true`，那 GUI 怎么阻止用户填入 17、1000 这种非法值？
**答**：取值合法性主要由 [package.tcl:70](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L70) `gui_parameter_set_range 16 256` 声明的范围（落入 component.xml 的 `spirit:minimum/maximum`）在 GUI 控件层兜底；`validate_*` 用于范围之外的复杂约束（例如「必须是 2 的幂」），本 IP 不需要，故留空返回 true。

**练习 2**：如果想让 GUI 里数据宽度的单位显示成「字节」而综合时仍按「位」传给 generic，该改哪类过程？
**答**：改 `update_MODELPARAM_VALUE.C_M00_AXI_DATA_WIDTH`，在里面把字节值乘 8 后再 `set_property` 给 `MODELPARAM_VALUE`，并在 `init_gui` 里把控件显示名改为字节语义。这正是三级拆分的价值所在。

---

### 4.3 IP-XACT component.xml

#### 4.3.1 概念说明

`component.xml` 是 Vivado 真正消费的 IP 描述文件，遵循 IP-XACT（IEEE 1685-2009，故根元素命名空间是 `spirit:`）。它把「这个 IP 是什么」拆成若干侧面分别描述。虽然文件很长（约 2130 行），但结构是规整的几大块，读它时按「块」定位即可。

#### 4.3.2 核心流程（component.xml 的六大块）

```text
<spirit:component>
 ├─ 头部:        vendor / library / name / version        （是谁的哪个 IP）
 ├─ busInterfaces: m00_axi(master) s00_axi(slave) axi_aclk axi_aresetn
 │                └ 每个接口把逻辑端口名(AWADDR...)映射到物理端口名(m00_axi_awaddr...)
 ├─ addressSpaces: m00_axi 主机看到的存储空间大小（依赖 ADDR_WIDTH）
 ├─ memoryMaps:    s00_axi 从机的寄存器块 reg0（range=256, width=32）
 ├─ model:
 │   ├─ views:     synthesis / simulation / xpgui / utility / datasheet / driver
 │   ├─ ports:     所有物理端口及其位宽（位宽可依赖 MODELPARAM_VALUE）
 │   └─ modelParameters: 5 个 MODELPARAM_VALUE 默认值
 ├─ fileSets:      每个 view 对应一组文件清单
 └─ parameters:    面向用户的 PARAM_VALUE（含 min/max/default）
```

#### 4.3.3 源码精读

**(a) 头部四元组** — [component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L3-L6)：`vendor=psi.ch`、`library=DBPM3`、`name=mem_test`、`version=1.2`，正是 4.1.3(b) 表里所列的对应关系。

**(b) 总线接口** — 以主机口为例，[component.xml:8-14](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L8-L14)：声明 `m00_axi` 是 Xilinx 标准 `aximm` 接口的 **master**，引用地址空间 `m00_axi`。从机口 [component.xml:266-272](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L266-L272) 则是 **slave**，引用 memoryMap `s00_axi`。每个接口下的 `portMaps` 把 AXI 标准逻辑名（`AWADDR`/`WDATA`/`BRESP`…）逐一映射到 wrapper 的物理端口名（`m00_axi_awaddr`…）——这就是 Vivado 知道「BD 里连一根 AXI 线，实际该接到哪些端口」的依据。时钟与复位接口 [component.xml:578-603](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L578-L603) 还用参数声明了 `ASSOCIATED_BUSIF=m00_axi:s00_axi`（这俩 AXI 口共用此时钟）和 `ASSOCIATED_RESET=axi_aresetn`。

**(c) 地址空间（动态位宽公式）** — [component.xml:605-611](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L605-L611)：主机地址空间的 range 和 width 不是写死的，而是用依赖表达式根据 `MODELPARAM_VALUE` 实时算出：

\[
\text{range} = 2^{\,\text{C\_M00\_AXI\_ADDR\_WIDTH}} \quad(\text{默认 } 2^{32}=4294967296\text{ 字节})
\]

\[
\text{width} = \text{C\_M00\_AXI\_DATA\_WIDTH} \quad(\text{默认 } 64)
\]

component.xml 里的写法 `pow(2,(decode(...ADDR_WIDTH) - 1) + 1)` 化简即 \(2^{N}\)。这样用户在 GUI 改了地址宽度，BD 里的地址空间大小会自动跟着变，无需重新封装。

**(d) 寄存器 memoryMap** — [component.xml:612-623](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L612-L623)：从机 `s00_axi` 有一个地址块 `reg0`，`baseAddress=0`、`range=256`、`width=32`、`usage=register`。这里的 `range=256` 字节正是 u4-l1 讲过的 AXI-Lite 从机 8 位地址空间（\(2^{8}=256\) 字节），`width=32` 对应每寄存器 32 位——与 u2-l1 寄存器地图的物理接口一致。

**(e) 模型端口（位宽依赖参数）** — 以写数据端口为例，[component.xml:1491-1507](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L1491-L1507)：`m00_axi_wdata` 的 `left` 用表达式 `decode(C_M00_AXI_DATA_WIDTH) - 1`（默认 63），`m00_axi_wstrb` 的 `left` 用 `(C_M00_AXI_DATA_WIDTH/8) - 1`（默认 7）。这与 wrapper 里 `std_logic_vector(C_M00_AXI_DATA_WIDTH-1 downto 0)` 完全同构，保证 GUI 改宽度后端口位宽同步。

**(f) 模型参数与用户参数** — 模型参数默认值在 [component.xml:1846-1872](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L1846-L1872)（5 个 `MODELPARAM_VALUE`，如 `C_M00_AXI_DATA_WIDTH=64`），面向用户的可改参数在 [component.xml:2047-2077](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L2047-L2077)（含 `minimum/maximum`，例如 `C_M00_AXI_DATA_WIDTH` min=16 max=256）。后者与 package.tcl 的 `gui_parameter_set_range` 完全吻合，证明 component.xml 是 package.tcl 的产物。

**(g) 文件集（尤其驱动）** — [component.xml:2022-2044](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L2022-L2044)：`xilinx_softwaredriver_view_fileset` 收纳了 `data/mem_test.mdd`、`data/mem_test.tcl`、`src/Makefile`、`src/mem_test.c`、`src/mem_test.h`——比 package.tcl 显式声明的两个 `.c/.h` 多出来的 `.mdd/.tcl/Makefile` 是框架按驱动目录约定自动补全的（`mem_test.mdd` 声明驱动绑定 `mem_test` 外设，`mem_test.tcl` 在生成 BSP 时写 `xparameters.h`，详见 u2-l3）。

#### 4.3.4 代码实践：定位一个参数的三处出现

1. **实践目标**：在 component.xml 里找到 `C_M00_AXI_MAX_BURST_SIZE` 的三处出现，体会同一参数在 IP-XACT 不同节点的角色。
2. **操作步骤**：
   - 用编辑器在 component.xml 内搜索 `C_M00_AXI_MAX_BURST_SIZE`。
   - 在 `modelParameters` 段（约 [第 1862-1866 行](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L1862-L1866)）找到 `MODELPARAM_VALUE.C_M00_AXI_MAX_BURST_SIZE=16`。
   - 在 `parameters` 段（约 [第 2063-2067 行](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/component.xml#L2063-L2067)）找到 `PARAM_VALUE.C_M00_AXI_MAX_BURST_SIZE`，min=1 max=256 default=16。
3. **需要观察的现象**：同一参数名出现在 modelParameters（综合默认值）与 parameters（用户可改、带范围）两段；MODEL 默认值 = PARAM 默认值 = wrapper generic 默认值（都是 16）。
4. **预期结果**：三处默认值一致，范围与 package.tcl 第 78 行 `gui_parameter_set_range 1 256` 一致。
5. 本实践为静态阅读，**无需运行**。

#### 4.3.5 小练习与答案

**练习 1**：component.xml 里 `m00_axi` 地址空间的 range 用依赖表达式算，而 `s00_axi` 的 reg0 range 写死成 256，为什么？
**答**：主机地址空间随用户可配的 `C_M00_AXI_ADDR_WIDTH` 变化（公式 \(2^{N}\)），故必须用依赖表达式；从机是 AXI-Lite 寄存器接口，地址宽度在 IP 内固定为 8 位（u4-l1 的 `AxiAddrWidth_g=8`），不随用户参数变，所以 range 恒为 \(2^{8}=256\) 字节。

**练习 2**：为什么 component.xml 里有 `C_S00_AXI_ID_WIDTH`，而 package.tcl 没有为它写 `gui_create_parameter`？
**答**：`ID_WIDTH` 是 AXI4 的标准接口参数，框架在识别到 AXI4 从机接口时会自动补全该参数的 modelParameter / parameter / xgui 过程，并用 `bd/bd.tcl` 把它标记为「仅传播」——在 Block Design 里由相连对端自动决定，不暴露给用户手填（详见 u5-l3）。

---

## 5. 综合实践：新增一个「测试超时阈值」GUI 参数

**任务**：假设要给 IP 增加一个 GUI 参数 `C_TIMEOUT`（测试超时阈值，单位为时钟周期，0 表示关闭），让它一路映射到 wrapper 的同名 generic。请写出三个文件需要的改动，并说明映射链路。本任务为「设计 + 源码阅读型」，**运行验证待本地进行**。

### 步骤 1：在 wrapper 增加 generic（承接 u3-l1）

在 [hdl/mem_test_wrapper.vhd:15-24](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L15-L24) 的 generic 列表里加一行（**示例代码，非项目原有**）：

```vhdl
C_TIMEOUT                   : integer := 0;   -- 0 = 关闭超时
```

### 步骤 2：在 package.tcl 声明 GUI 参数

仿照现有「三连」，在 [scripts/package.tcl:83](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/package.tcl#L83) 之后追加（**示例代码**）：

```tcl
gui_create_parameter "C_TIMEOUT" "Test timeout (cycles, 0=off)"
gui_parameter_set_range 0 1000000000
gui_add_parameter
```

参数名 `C_TIMEOUT` 必须与 generic 同名，框架据此自动建立 PARAM_VALUE ↔ MODELPARAM_VALUE ↔ generic 的三级映射。

### 步骤 3：补全 xgui 的三类过程

在 [xgui/mem_test_v1_2.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/xgui/mem_test_v1_2.tcl) 中，仿照现有参数新增（**示例代码**）：

```tcl
# 1) init_gui 里挂控件（参考第 6-9 行的 ipgui::add_param 写法）
ipgui::add_param $IPINST -name "C_TIMEOUT" -parent ${AXI-M}

# 2) 更新与校验（占位，参考第 23-30 行）
proc update_PARAM_VALUE.C_TIMEOUT { PARAM_VALUE.C_TIMEOUT } { }
proc validate_PARAM_VALUE.C_TIMEOUT { PARAM_VALUE.C_TIMEOUT } {
    return true
}

# 3) 桥接 PARAM_VALUE -> MODELPARAM_VALUE（参考第 65-68 行）
proc update_MODELPARAM_VALUE.C_TIMEOUT { MODELPARAM_VALUE.C_TIMEOUT PARAM_VALUE.C_TIMEOUT } {
    set_property value [get_property value ${PARAM_VALUE.C_TIMEOUT}] ${MODELPARAM_VALUE.C_TIMEOUT}
}
```

### 步骤 4：重新封装并核对

1. 进 `scripts/` 执行 `source package.tcl`（需 Vivado + 依赖库，**待本地验证**）。
2. 重封装后，`component.xml` 应自动新增：`modelParameters` 段多一个 `MODELPARAM_VALUE.C_TIMEOUT`；`parameters` 段多一个 `PARAM_VALUE.C_TIMEOUT`（min=0 max=1000000000）。

### 需要观察的现象与预期结果

- 打开 IP 的 Re-customize IP 对话框，`AXI-M` 页应出现 `C_TIMEOUT` 输入框。
- 把它改成 1000、综合，wrapper 实例的 `C_TIMEOUT` generic 应等于 1000（可在综合后的 RTL 层级里查看）。
- 若 wrapper 内部已实现超时逻辑（本仓库当前并无，需自行添加），超时后应触发 u3-l3 讲的 `IntError_s` 状态。

### 反思要点

- 三个文件改动里，**只有 wrapper 的 generic 与 package.tcl 的「三连」是必须手工写的**；xgui 的过程在重封装时会被框架重新生成，手工写只是为了定制校验/联动。
- 参数名在三处（package.tcl / xgui / wrapper generic）必须完全一致，这是整个封装机制的「隐式契约」。

## 6. 本讲小结

- `scripts/package.tcl` 是封装「配方」，按「载入框架 → init → 加源码 → 加依赖库 → 加驱动 → 声明 GUI 参数 → `package_ip`」七步把 RTL 描述成一个 IP，产物落盘到仓库根。
- `component.xml` 是 IP-XACT（IEEE 1685）元数据，分头部、总线接口、地址空间、寄存器 memoryMap、模型（视图/端口/参数）、文件集、用户参数七大块，是 Vivado 真正消费的「IP 契约」。
- `xgui/mem_test_v1_2.tcl` 定义图形参数界面，含三类过程：`init_gui`（搭界面）、`update/validate_PARAM_VALUE`（联动与校验）、`update_MODELPARAM_VALUE`（GUI 值 → 综合参数的桥）。
- 参数三级映射 `PARAM_VALUE` → `MODELPARAM_VALUE` → VHDL generic 靠「同名」自动建立，这是新增参数时必须遵守的隐式契约。
- `package.tcl` 显式声明 4 个用户可改参数；第 5 个 `C_S00_AXI_ID_WIDTH` 由 AXI4 接口自动提取并由 `bd/bd.tcl` 自动传播（下讲详讲）。
- component.xml 里端口位宽与地址空间用依赖表达式动态计算，故改 GUI 参数后无需重新封装即可在 BD 中看到正确的端口宽度与地址范围。

## 7. 下一步学习建议

- 下一讲 **u5-l3 Block Design 集成与 CI 流水线** 将讲解 `bd/bd.tcl` 的 `init` / `pre_propagate` / `propagate` 三个回调如何自动传播 `C_S00_AXI_ID_WIDTH`，以及 `scripts/ciFlow.py` 如何在 CI 里跑仿真并判定通过/失败，把「RTL → 仿真 → 封装 → BD 集成」的完整交付链闭环。
- 想深入了解 PsiIpPackage 框架本身的高层命令全集，可阅读外部库 [PsiIpPackage](https://github.com/paulscherrerinstitute/PsiIpPackage)（u1-l2 依赖清单里列为开发期依赖）。
- 想验证本讲综合实践的产物，可在装有 Vivado 与 psi_common 依赖的环境里跑一次 `source scripts/package.tcl`，对照本讲给出的「观察现象」逐项核对 component.xml 与 xgui 的变化。
