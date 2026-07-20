# 用 PsiIpPackage 打包 IP 核

## 1. 本讲目标

u1-l3 已经从高处俯瞰过「源码 → 依赖 → 打包 → 综合产物 → zip」这条流水线，并给出过一张 PsiIpPackage 的「五类命令」速查表；u2-l1 又带我们读完了顶层 `fpga_base_v1_0` 的 entity，认识了一组泛型（`C_VERSION`、`C_FREQ_AXI_CLK_HZ` 等）。

本讲要**下沉到命令级**，回答三个更具体的问题：

1. `scripts/package.tcl` 里每一条 PsiIpPackage 命令的**参数**到底怎么填、按什么**顺序**调用？
2. 三个长相相似的 `add_*_relative` 命令，是如何用**相对路径**把本仓库 HDL、`psi_common` 库、C 驱动分别聚合进 IP 的？这些路径在产物 `component.xml` 里又变成了什么样？
3. 收尾的 `package_ip` 那四个参数（目录、`Edit`、`Synth`、器件）各管什么？打包版本号 `1.4` 又是怎样**同时出现在 `component.xml` 的多个位置**的？

学完本讲，你应当能够拿着 `package.tcl`（输入）和 `component.xml`（产物）两份文件逐条对账，说清每一条 DSL 命令在产物里落到了哪个 XML 片段。

> 本讲聚焦「打包 API 与产物对照」。GUI 参数的回调细节、端口使能如何裁剪硬件，留给 u4-l2；Block Design 钩子留给 u4-l3。

## 2. 前置知识

先用三段大白话把背景补齐。

### 2.1 PsiIpPackage 是一层「打包 DSL」

Vivado 原生打包一个 IP 要敲一连串冗长命令：`create_project` → `add_files` → 逐个 `set_property` 配参数/端口 → `synth_design` → `ipx::package_project` …… PSI 把这些重复操作封装成 TCL 库 **PsiIpPackage**，对外暴露一套简短的 DSL（领域专用命令）：`init`、`add_sources_relative`、`gui_create_parameter`、`package_ip` 等。每个 IP 的打包脚本因此都长得几乎一样，而且不到 100 行。

> 该框架不在本仓库内（README `## Dependencies` 把它列为 TCL 类外部依赖），本讲只依据 `package.tcl` 对它的**调用方式**来描述，不臆测其内部 Tcl 过程体。

### 2.2 相对路径的「锚点」会换

这是本讲最容易踩坑、也最值得搞清的一点：**`package.tcl` 里的相对路径，和 `component.xml` 里的相对路径，锚点不同**。

- `package.tcl` 位于 `<仓库>/scripts/` 下，它写的相对路径以 `scripts/` 为锚。
- `component.xml` 位于**仓库根目录**，它写的相对路径以仓库根为锚。

所以同一段「从 scripts 到 hdl」的距离，在 `package.tcl` 里写作 `../hdl/`（往上 1 级），在 `component.xml` 里却写作 `hdl/`（不用往上）。我们会在 4.2 节用真实行号印证这个「锚点切换」。

### 2.3 版本号有两套，别混

`package.tcl` 里 `IP_VERSION 1.4` 是 **IP 打包版本号（两级：主.次）**，它落到 `component.xml` 的 `<spirit:version>`、VHDL 库名、显示名等多处。而顶层 VHDL 的 entity 名是 `fpga_base_v1_0`，这里的 `v1_0` 是一个**历史遗留的实体名后缀**，并不随打包版本走——所以 `component.xml` 里 `modelName` 始终是 `fpga_base_v1_0`，而打包版本是 `1.4`，两者各管各的。`Changelog.md` 顶部的 `1.4.0` 则是三级语义化版本（源码变更记录），粒度更细。

## 3. 本讲源码地图

| 文件 | 语言 | 角色 | 本讲用法 |
|------|------|------|----------|
| `scripts/package.tcl` | TCL | 打包**输入**：用 PsiIpPackage DSL 声明这个 IP 长什么样 | 逐条精读每条命令的参数 |
| `component.xml` | IP-XACT/XML | 打包**产物**：综合后落盘的 IP-XACT 清单 | 与 `package.tcl` 逐条对账 |
| `hdl/fpga_base_v1_0.vhd` | VHDL | 顶层 entity（u2-l1 已读） | 只看 generic 列表，印证「泛型 ↔ 参数」对应 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 PsiIpPackage 打包 API**（命令序列与参数）、**4.2 源码与库聚合**（三个 `add_*_relative` 与路径/文件集映射）、**4.3 综合打包配置**（`package_ip` 四参数与版本号传播）。

---

### 4.1 PsiIpPackage 打包 API

#### 4.1.1 概念说明

`package.tcl` 本身几乎不含控制流（没有循环、没有条件分支），它就是一份**按固定顺序罗列 DSL 命令的清单**。理解它的关键是把握顺序：

1. **加载并导入框架**（让后续命令可用）。
2. **基本信息**：声明 IP 的名字、版本、库、描述、logo。
3. **聚合源码**：把 HDL、依赖库、驱动加进来。
4. **GUI 参数与端口使能**：声明用户可配置什么、哪些端口可裁剪。
5. **打包收尾**：在指定器件上综合，写出 `component.xml`。

之所以顺序不能乱，是因为 PsiIpPackage 内部维护着一个「正在构造的 IP」状态对象：必须先 `init` 给它起名，后续 `add_*` / `gui_*` 才知道往哪个 IP 上加东西，最后 `package_ip` 才能把这一切固化成产物。

#### 4.1.2 核心流程

下面是 `package.tcl` 整条调用链的伪代码（只列命令名，参数见 4.1.3）：

```
source  PsiIpPackage.tcl            ;# 加载外部框架
namespace import psi::ip_package::latest::*   ;# 导入全部命令

init            <name> <version> <revision> <library>
set_description <text>
set_logo_relative <path>

add_sources_relative   { <本仓库 3 个 HDL> }
add_lib_relative       <psi_common 路径> { <5 个 HDL> }
add_drivers_relative   <驱动根> { <.c .h> }

gui_add_page "Configuration"
gui_create_parameter        ...   ;# 逐个声明普通参数
gui_create_user_parameter   ...   ;# 逐个声明用户参数（布尔）
add_port_enablement_condition ... ;# 端口使能条件

package_ip <dir> <Edit> <Synth> <part>
```

注意第 1 步加载的是外部框架（不在本仓库），由依赖解析事先放到 `../../../TCL/PsiIpPackage/` 这个 PSI 约定路径下（详见 u1-l3）。

#### 4.1.3 源码精读

**加载与导入**：

[scripts/package.tcl:1-5](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L1-L5) —— 第 4 行 `source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl` 加载框架；第 5 行 `namespace import -force psi::ip_package::latest::*` 把 `latest`（最新版）命名空间下的全部命令导入当前作用域，`-force` 保证重复 source 时覆盖旧定义。导入之后，`init`、`add_sources_relative` 等命令就可以「裸名」调用，不必写全名前缀。

**基本信息**：

[scripts/package.tcl:10-18](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L10-L18) —— 四个变量 + 三条命令：

- `IP_NAME fpga_base`、`IP_VERSION 1.4`、`IP_REVISION "auto"`、`IP_LIBRARY PSI` 四个变量，分别给出 IP 名、打包版本、修订号、库名。
- `init $IP_NAME $IP_VERSION $IP_REVISION $IP_LIBRARY` 用这四个变量初始化状态对象。其中 `IP_REVISION "auto"` 表示让框架**自动**生成修订号（而不是手填），方便每次打包自动递增。
- `set_description "FPGA Version information (SW and FW build date)"` 写一句话描述。
- `set_logo_relative "../doc/psi_logo_150.gif"` 贴一个 logo（注意 `../doc/` 是以 `scripts/` 为锚的相对路径）。

`IP_LIBRARY PSI` 这一参数对应 IP-XACT 的「库（library）」坐标。IP-XACT 用 `vendor / library / name / version` 四元组唯一标识一个 IP，本 IP 的坐标就是 `psi.ch / PSI / fpga_base / 1.4`，这点会在 4.3.3 节于 `component.xml` 顶部得到印证。

> 注意第 14 行变量名是 `IP_DESCIRPTION`（拼写少了一个 `R`），但它只是个局部变量，传给 `set_description` 用，不影响产物，可视为一处无害的拼写瑕疵。

#### 4.1.4 代码实践

**实践目标**：不运行脚本，仅靠阅读，把 `package.tcl` 的命令还原成 4.1.2 那张「顺序骨架」，并标注每一步用到的 DSL 命令。

**操作步骤**：

1. 打开 `scripts/package.tcl`，从第 1 行通读到第 97 行。
2. 用三种颜色的笔（或三种记号）分别标出：加载/导入命令、`init` 与 `set_*` 等基本信息命令、`package_ip` 收尾命令。
3. 数一下：在 `init` 之后、`package_ip` 之前，一共出现了几条 `add_*` 命令、几条 `gui_*` 命令、几条 `add_port_enablement_condition`。

**需要观察的现象**：

- 整个脚本里**没有任何** Vivado 原生的 `create_project` / `add_files` / `synth_design`——它们都被 DSL 封装掉了。
- `init` 出现在所有 `add_*` / `gui_*` 之前，`package_ip` 是全文最后一条命令。

**预期结果**：你能用一句话讲清——「`package.tcl` 是一份声明式清单，它声明 IP 长什么样，由 PsiIpPackage 负责把它造出来」。

#### 4.1.5 小练习与答案

**练习 1**：`namespace import` 为什么要带 `latest`？如果直接 `import psi::ip_package::*` 会怎样？
**答案**：框架用命名空间区分版本（`psi::ip_package::latest`）。带 `latest` 表示始终用最新版命令，升级框架时脚本不必改前缀。若直接导入父命名空间 `psi::ip_package::*`，可能把若干「版本子命名空间」本身也导进来，反而污染当前作用域——`latest` 才是正确的「当前版本命令集合」入口。

**练习 2**：`IP_REVISION "auto"` 与 `IP_VERSION 1.4` 各自控制什么？
**答案**：`IP_VERSION 1.4` 是 IP-XACT 的两级版本号（落到 `<spirit:version>`）；`IP_REVISION "auto"` 让框架自动算一个修订号（通常表现为 `component.xml` 里的 `coreRevision` 之类内部字段），无需人工维护。前者是「面向用户的版本」，后者是「面向打包工具的构建序号」。

---

### 4.2 源码与库聚合

#### 4.2.1 概念说明

`fpga_base` 的 RTL 并不自给自足：顶层 `fpga_base_v1_0.vhd` 里写着 `use work.psi_common_array_pkg.all;`（见 [hdl/fpga_base_v1_0.vhd:24](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L24)），它依赖 `psi_common` 库；它还要带一份 C 驱动给软件侧用。所以打包时必须把**三类文件**聚合进同一个 IP：

1. **本仓库 HDL**（3 个 `.vhd`）——由 `add_sources_relative` 加。
2. **`psi_common` 库 HDL**（5 个 `.vhd`）——由 `add_lib_relative` 加，路径指向仓库之外的兄弟仓库。
3. **C 驱动**（`.c` / `.h`）——由 `add_drivers_relative` 加。

三条命令名字相近，但**锚点处理方式**和**落到的文件集（fileSet）**各不相同，这正是本模块要讲清的核心。

还有一个隐藏机制：所有被聚合的 HDL（不管是本仓库的还是 `psi_common` 的）最终都被编译进**同一个 VHDL 库**，名字形如 `fpga_base_1_4`（即「IP 名 + 版本号去点」）。这保证了顶层那句 `use work.psi_common_array_pkg.all` 在消费方工程里能正确解析——因为 `psi_common_array_pkg` 和顶层实体此刻位于同一个 `work` 库里。

#### 4.2.2 核心流程

三条聚合命令的职责对照如下：

| 命令 | 输入锚点 | 典型用途 | 落到的 fileSet |
|------|----------|----------|----------------|
| `add_sources_relative {文件列表}` | `scripts/`（脚本所在目录） | 加入本仓库 RTL | `xilinx_anylanguagesynthesis_view_fileset`（及仿真文件集） |
| `add_lib_relative <库根路径> {文件列表}` | `scripts/` | 加入外部库 RTL（路径指向兄弟仓库） | 同上（路径前缀会被改写） |
| `add_drivers_relative <驱动根> {文件列表}` | `scripts/` | 加入软件驱动 | `xilinx_softwaredriver_view_fileset` |

关键细节有二：

- **锚点切换**：三条命令写的相对路径都以 `scripts/` 为锚；但产物 `component.xml` 在仓库根，所以框架会把所有路径**改写成以仓库根为锚**。这就是为什么 `../hdl/`（1 级）会变成 `hdl/`（0 级），`../../../VHDL/psi_common/hdl`（3 级）会变成 `../../VHDL/psi_common/hdl`（2 级）。
- **库统一**：所有 HDL 条目共享同一个 `<spirit:logicalName>fpga_base_1_4</spirit:logicalName>`，即同一个 VHDL 库。

#### 4.2.3 源码精读

**输入侧（package.tcl）——三条命令**：

[scripts/package.tcl:24-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L24-L28) —— `add_sources_relative` 列出本仓库 3 个 HDL：`fpga_base_date_package.vhd`、`fpga_base_v1_0.vhd`、`fpga_base_scripted_info_pkg.vhd`，路径前缀 `../hdl/`（相对 `scripts/` 往上 1 级到仓库根，再进 `hdl/`）。

[scripts/package.tcl:31-39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L31-L39) —— `add_lib_relative` 第一个参数是库根 `"../../../VHDL/psi_common/hdl"`（相对 `scripts/` 往上 3 级到 PSI 布局的祖先目录，再进 `VHDL/psi_common/hdl`），第二个参数是 5 个文件名（`psi_common_array_pkg.vhd`、`psi_common_math_pkg.vhd`、`psi_common_logic_pkg.vhd`、`psi_common_pl_stage.vhd`、`psi_common_axi_slave_ipif.vhd`）。这 5 个正是顶层 RTL `use` 到的 `psi_common` 单元（u2-l2 详述了其中的 `psi_common_axi_slave_ipif`）。

[scripts/package.tcl:44-47](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L44-L47) —— `add_drivers_relative ../drivers/fpga_base { src/fpga_base.c src/fpga_base.h }`，根目录 `../drivers/fpga_base`（相对 `scripts/` 往上 1 级到仓库根，再进 `drivers/fpga_base`），只显式列了 2 个文件。

**产物侧（component.xml）——印证锚点切换与库统一**：

综合文件集里能看到路径已被改写：

[component.xml:1192-1235](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1192-L1235) —— `xilinx_anylanguagesynthesis_view_fileset`。逐条对照：

- [component.xml:1195](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1195) —— `hdl/fpga_base_date_package.vhd`：输入侧是 `../hdl/`（1 级），产物侧变成 `hdl/`（0 级），锚点切到仓库根。
- [component.xml:1200](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1200) —— `hdl/fpga_base_scripted_info_pkg.vhd`：同上。
- [component.xml:1205-1228](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1205-L1228) —— 5 个 `psi_common` 文件，路径前缀 `../../VHDL/psi_common/hdl/`（2 级）：输入侧是 `../../../`（3 级），产物侧减一级成 `../../`，同样是锚点从 `scripts/` 切到仓库根。
- [component.xml:1230](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1230) —— `hdl/fpga_base_v1_0.vhd`：顶层放最后（IP-XACT 惯例，方便综合自底向上解析），还多了一个 `<spirit:userFileType>CHECKSUM_89ea37ab</spirit:userFileType>`（框架为顶层加的校验标记）。

库统一最直观的证据——**每一条 HDL 条目都带同一个 logicalName**：

[component.xml:1197](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1197) —— `<spirit:logicalName>fpga_base_1_4</spirit:logicalName>`。从第 1195 行到第 1234 行，无论本仓库 HDL 还是 `psi_common` HDL，`logicalName` 全是 `fpga_base_1_4`。这就是「全部编译进同一个 VHDL 库」的产物表现，也正是顶层 `use work.psi_common_array_pkg.all` 能解析的原因。

仿真文件集是综合文件集的镜像（同一批 HDL）：

[component.xml:1236-1278](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1236-L1278) —— `xilinx_anylanguagebehavioralsimulation_view_fileset`，同样 8 个 HDL、同样的 `logicalName`、同样的路径改写。所以 `add_sources_relative` / `add_lib_relative` 实际上**同时**把文件加进了综合与仿真两个文件集。

**驱动侧的「自动发现」**——这是 `add_drivers_relative` 最值得注意的特性：

[component.xml:1295-1317](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1295-L1317) —— `xilinx_softwaredriver_view_fileset` 里实际有 **5** 个文件：

| 文件（component.xml 中的相对路径） | 是否在 package.tcl 显式列出 |
|------------------------------------|------------------------------|
| `drivers/fpga_base/data/fpga_base.mdd` | 否（自动发现） |
| `drivers/fpga_base/data/fpga_base.tcl` | 否（自动发现） |
| `drivers/fpga_base/src/Makefile` | 否（自动发现） |
| `drivers/fpga_base/src/fpga_base.c` | 是 |
| `drivers/fpga_base/src/fpga_base.h` | 是 |

输入侧 [scripts/package.tcl:44-47](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L44-L47) 只显式列了 `.c` / `.h` 两个源文件，但 `data/fpga_base.mdd`、`data/fpga_base.tcl`、`src/Makefile` 也一并出现在产物里。这说明 PsiIpPackage 在处理驱动目录时，会按 **Xilinx 驱动约定自动收拢** `data/*.mdd`、`data/*.tcl`、`src/Makefile` 等标准驱动元数据文件，而不必逐个手写。这几个文件的作用（`.mdd` 声明驱动支持的周边、`.tcl` 生成 `xparameters.h`、`Makefile` 编译归档）将在 u5-l2 详述。

#### 4.2.4 代码实践

**实践目标**：亲手完成「输入侧相对路径 → 产物侧相对路径」的锚点换算，验证 4.2 节的核心结论。

**操作步骤**：

1. 在 `scripts/package.tcl` 第 24-39 行，抄下每条 `add_*` 命令里出现的相对路径前缀（`../hdl/`、`../../../VHDL/psi_common/hdl`、`../drivers/fpga_base`）。
2. 到 `component.xml` 第 1192-1317 行，找到这些文件在产物里的实际路径（`hdl/`、`../../VHDL/psi_common/hdl/`、`drivers/fpga_base/...`）。
3. 做一张换算表：左列「以 scripts/ 为锚的路径」，中列「级数」，右列「以仓库根为锚的路径」，验证「级数恰好减 1」。

**需要观察的现象**：

- `../hdl/` → `hdl/`：1 级 → 0 级。
- `../../../VHDL/psi_common/hdl` → `../../VHDL/psi_common/hdl`：3 级 → 2 级。
- `../drivers/fpga_base` → `drivers/fpga_base`：1 级 → 0 级。
- 三类文件的 `logicalName` 是否一致（驱动文件集不带 `logicalName`，因为它不是 VHDL）。

**预期结果**：你得出结论——**框架统一把所有相对路径重写为「以 `component.xml` 所在仓库根为锚」**，因此同一段物理距离，在 `package.tcl`（scripts/ 锚）里比在 `component.xml`（仓库根锚）里多写一级 `../`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `add_lib_relative` 用 `../../../`（3 级）而 `add_sources_relative` 只用 `../`（1 级）？
**答案**：因为它们指向的物理位置不同。`add_sources_relative` 指向本仓库内的 `hdl/`，从 `scripts/` 只需往上 1 级到仓库根。`add_lib_relative` 指向**仓库之外**的兄弟仓库 `psi_common`，按 PSI 约定布局，它位于从 `scripts/` 往上 3 级的祖先目录下的 `VHDL/psi_common/hdl`。这正是 u1-l3 讲过的「依赖解析先用 Python 把兄弟仓库放到 PSI 布局，打包脚本才能用相对路径找到它们」。

**练习 2**：如果有人误把 `add_drivers_relative` 显式列表改成空 `{}`，产物 `component.xml` 的驱动文件集会变空吗？
**答案**：不会完全变空。框架会按约定**自动发现** `data/*.mdd`、`data/*.tcl`、`src/Makefile` 等标准驱动元数据文件，所以这些仍会出现；但 `fpga_base.c` / `fpga_base.h` 这两个**显式列出**的源文件会丢失，导致驱动只有元数据没有实现，编译时出错。这说明「显式列表」与「约定自动发现」是互补的两条路径。

---

### 4.3 综合打包配置

#### 4.3.1 概念说明

前两模块把「声明」讲完了，本模块讲最后一步 `package_ip`——它真正驱动 Vivado 跑一次综合，把所有声明固化成 `component.xml`。理解它要抓住两点：

1. **四个参数**各管一件事：输出目录、是否打开 GUI 手工编辑、是否跑综合、目标器件。
2. **打包版本号 `1.4` 会传播到 `component.xml` 的多个位置**，而顶层实体名 `fpga_base_v1_0` 不变——这是「打包版本」与「RTL 实体名」两套独立命名的关键区别。

#### 4.3.2 核心流程

`package_ip` 的调用形式是：

```
package_ip  <TargetDir>  <Edit>  <Synth>  <part>
```

四个参数的含义：

| 参数 | 本讲取值 | 含义 |
|------|----------|------|
| `TargetDir` | `..` | 产物输出目录；相对 `scripts/` 往上 1 级即仓库根，所以 `component.xml` 落在仓库根 |
| `Edit` | `false` | 不打开 Vivado 的打包 GUI 让人工编辑，全自动完成 |
| `Synth` | `true` | 跑一次综合，从 RTL 反推端口方向/位宽、参数取值，再写进 `component.xml` |
| `part` | `xc7a200t` | 目标器件，一颗 Xilinx Kintex-7 芯片 |

`Synth=true` 是关键：只有真跑综合，Vivado 才能从 entity 反推出 `s00_axi_araddr` 是 8 位输入、`s00_axi_rdata` 是 32 位输出、`C_S00_AXI_ID_WIDTH` 默认 1 等精确信息（这些在 u2-l1 已对照过 entity）。若 `Synth=false`，产物元数据会不完整，下游例化可能出错。

打包版本 `1.4` 在 `component.xml` 中的传播路径（编号对应 4.3.3 的行号）：

```
IP_VERSION 1.4 (package.tcl:11)
   │
   ├──> <spirit:version>1.4</spirit:version>            (component.xml:6)
   ├──> <spirit:library>PSI</spirit:library>            (component.xml:4)   ← IP_LIBRARY
   ├──> logicalName  fpga_base_1_4                       (component.xml:1197)
   ├──> displayName  fpga_base_1_4                       (component.xml:1403)
   ├──> xgui 脚本名  xgui/fpga_base_v1_4.tcl             (component.xml:1282)
   └──> 归档标签  psi.ch:PSI:fpga_base:1.4_ARCHIVE_LOCATION (component.xml:1412)

   ⚝ 不受 1.4 影响 ❝
        modelName  fpga_base_v1_0                        (component.xml:366, 382)  ← 来自 entity 名
```

#### 4.3.3 源码精读

**收尾命令**：

[scripts/package.tcl:95-97](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L95-L97) —— `set TargetDir ".."`，第 96 行注释 `# Edit Synth` 是为下面四个实参做标注，第 97 行 `package_ip $TargetDir false true xc7a200t`。`$TargetDir` 展开为 `..`（仓库根），所以 `component.xml` 与 `scripts/` 同级落在仓库根；`false true` 分别是 `Edit=false`、`Synth=true`；`xc7a200t` 是目标器件。

**版本号传播——逐处印证**：

[component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L3-L6) —— `<spirit:vendor>psi.ch</spirit:vendor>`、`<spirit:library>PSI</spirit:library>`、`<spirit:name>fpga_base</spirit:name>`、`<spirit:version>1.4</spirit:version>`。这四行正是 IP-XACT 的「vendor / library / name / version」四元组坐标，与 `package.tcl` 的 `init fpga_base 1.4 auto PSI` 一一对应（`revision` 不进四元组）。

[component.xml:1197](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1197) —— `logicalName fpga_base_1_4`：VHDL 库名由「IP 名 + 版本号去点」拼成（4.2 节已述）。

[component.xml:1282](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1282) —— xgui 文件集里的脚本名 `xgui/fpga_base_v1_4.tcl`，同样带版本号 `v1_4`。这个脚本是 GUI 布局定义，u4-l2 会精读。

[component.xml:1403](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1403) —— `<xilinx:displayName>fpga_base_1_4</xilinx:displayName>`，Vivado IP 目录里展示给用户的名字。

[component.xml:1410-1413](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1410-L1413) —— `<xilinx:tags>` 下有 `psi.ch:user:fpga_base:1.0_ARCHIVE_LOCATION`、`psi.ch:PSI:fpga_base:1.0_ARCHIVE_LOCATION`、`psi.ch:PSI:fpga_base:1.4_ARCHIVE_LOCATION` 三条归档位置标签。注意其中既有 `1.0` 也有 `1.4`——这些是 Vivado 历次打包留下的「这个 IP 曾以哪个版本归档过、源在哪」的痕迹，`1.4` 是当前打包版本，`1.0` 是历史遗留。

**版本号「不」传播的地方——modelName**：

[component.xml:366](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L366) 与 [component.xml:382](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L382) —— 综合视图与仿真视图的 `<spirit:modelName>fpga_base_v1_0</spirit:modelName>`。这个 `v1_0` 来自顶层 VHDL 的 **entity 名**（见 [hdl/fpga_base_v1_0.vhd:27](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L27)），是历史遗留后缀，**不随打包版本变化**。这是初学者最易混淆处：`modelName=fpga_base_v1_0` 与 `version=1.4` 是两套独立命名。

**目标器件的产物表现**：

[component.xml:1382](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1382) —— `<xilinx:family ...>kintex7</xilinx:family>`，正是 `xc7a200t` 所属的 Kintex-7 系列。该 IP 支持的器件族列表在第 1378-1399 行（artix7、zynq、kintex7、virtexuplus 等），`xc7a200t` 只是打包时**用来跑综合的那一颗**，并非该 IP 只能用于这一颗。

**泛型 ↔ 参数的对应（承接 u2-l1）**：

打包版本之外的另一组「输入 ↔ 产物」对照，是顶层 entity 的 generic 与 `component.xml` 的参数。顶层 7 个 generic（[hdl/fpga_base_v1_0.vhd:31-39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L31-L39)：`C_VERSION`、`C_VERSION_MAJOR`、`C_VERSION_MINOR`、`C_FREQ_AXI_CLK_HZ`、`C_FREQ_BLINKING_LED_HZ`、`C_USE_INFO_FROM_SCRIPT`、`C_S00_AXI_ID_WIDTH`）与 `package.tcl` 第 56-70 行用 `gui_create_parameter` 声明的 7 个普通参数一一对应，并在 `component.xml` 的 `modelParameters`（[component.xml:1147-1182](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1147-L1182)）与 `parameters`（[component.xml:1320-1375](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1320-L1375)）里各出现一次。`gui_create_parameter` 声明的「普通参数」都对应一个 RTL generic；而 `gui_create_user_parameter` 声明的 `IMPL_BLINK` / `IMPL_SWITCH` / `IMPL_LED` 不在 generic 里，它们只用于端口使能（u4-l2 详述）。

#### 4.3.4 代码实践

**实践目标**：完成规格里要求的两件事——(a) 指出 `add_sources_relative` 的 3 个 HDL 文件在 `component.xml` 中对应哪些 fileSet 条目；(b) 说明版本号 `1.4` 如何体现在 `component.xml` 中。

**操作步骤**：

1. 在 [scripts/package.tcl:24-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L24-L28) 抄下 3 个 HDL 文件名。
2. 在 `component.xml` 中搜索这 3 个文件名，记录它们各自出现在哪几个 fileSet。
3. 全文搜索字符串 `1.4` 与 `fpga_base_1_4`，记录所有出现位置并分类（版本字段 / 库名 / 显示名 / xgui 脚本名 / 归档标签）。

**需要观察的现象**：

- `fpga_base_date_package.vhd`、`fpga_base_scripted_info_pkg.vhd`、`fpga_base_v1_0.vhd` 这 3 个文件**同时**出现在综合文件集（约 1195、1200、1230 行）和仿真文件集（约 1239、1244、1274 行）两个 fileSet 里。
- `1.4` 出现在 `<spirit:version>`（第 6 行）；`fpga_base_1_4` 出现在 `logicalName`、`displayName`、xgui 脚本名；`1.4_ARCHIVE_LOCATION` 出现在 tags；而 `modelName` 是 `fpga_base_v1_0`（不含 1.4）。

**预期结果**：

| `add_sources_relative` 文件 | 在 component.xml 中出现的 fileSet |
|------------------------------|------------------------------------|
| `fpga_base_date_package.vhd` | `xilinx_anylanguagesynthesis_view_fileset`（L1195）+ `xilinx_anylanguagebehavioralsimulation_view_fileset`（L1239） |
| `fpga_base_scripted_info_pkg.vhd` | 同上两个文件集（L1200、L1244） |
| `fpga_base_v1_0.vhd` | 同上两个文件集（L1230、L1274） |

版本号 `1.4` 的体现（至少五处）：① `<spirit:version>1.4</spirit:version>`（L6，IP-XACT 版本字段）；② `logicalName fpga_base_1_4`（L1197 等，VHDL 库名）；③ `displayName fpga_base_1_4`（L1403，Vivado 显示名）；④ xgui 脚本 `xgui/fpga_base_v1_4.tcl`（L1282）；⑤ 归档标签 `psi.ch:PSI:fpga_base:1.4_ARCHIVE_LOCATION`（L1412）。同时注意「不体现 1.4」的 `modelName fpga_base_v1_0`（L366、L382）。

#### 4.3.5 小练习与答案

**练习 1**：`package_ip` 的 `Edit=false` 改成 `true` 会发生什么？
**答案**：`Edit=true` 时，框架在打包过程中会**打开 Vivado 的 IP 打包 GUI**，暂停等待人工在图形界面里增删端口、改参数、调文件集，再手动点完成。本项目选 `false` 是为了「全自动、可复现」——一切由 `package.tcl` 声明驱动，不依赖人工点击，便于在 CI 或命令行里反复打包。

**练习 2**：为什么 `component.xml` 里 `modelName` 是 `fpga_base_v1_0`，而打包版本却是 `1.4`？这两个数字为什么不一样？
**答案**：`modelName` 是 RTL 顶层 **entity 名**，取自 [hdl/fpga_base_v1_0.vhd:27](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/hdl/fpga_base_v1_0.vhd#L27) 的 `entity fpga_base_v1_0`，其中的 `v1_0` 是最早创建该 IP 时留下的实体名后缀，之后不再改动。而 `1.4` 是 **IP 打包版本号**（`package.tcl` 的 `IP_VERSION`），每次发版会递增。两者是独立的命名维度：实体名稳定不变，打包版本号持续演进。

## 5. 综合实践

把三个模块串起来，做一次完整的「`package.tcl` ↔ `component.xml`」双向对账：

1. **正向追踪（声明 → 产物）**：从 [scripts/package.tcl:16](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L16) 的 `init` 开始，逐条向下，把每条 DSL 命令在 `component.xml` 中落到的片段画成一张映射图（`init` → 第 3-6 行四元组；`set_logo_relative` → `xilinx_utilityxitfiles_view_fileset` 第 1291 行 logo；`add_sources_relative` / `add_lib_relative` → 综合与仿真文件集；`add_drivers_relative` → 驱动文件集；`gui_create_parameter` → `modelParameters` 与 `parameters`；`package_ip` 的 `xc7a200t` → 第 1382 行 kintex7）。
2. **反向验证（产物 → 声明）**：在 `component.xml` 第 1282 行看到 xgui 脚本名是 `fpga_base_v1_4.tcl`，但 `package.tcl` 里并没有显式写这个名字——请解释它是怎么来的（提示：由 `IP_NAME` + `IP_VERSION` 自动派生）。
3. **路径换算**：任选一个 `psi_common` 文件，写出它在 `package.tcl`（scripts/ 锚）与 `component.xml`（仓库根锚）里的相对路径，验证级数差 1。

**验收标准**：你能用一段话讲清——「`package.tcl` 用 PsiIpPackage 的 DSL 声明 IP，框架把所有相对路径从 `scripts/` 锚重写为仓库根锚、把全部 HDL 编进同一个 `fpga_base_1_4` 库、并按约定自动收拢驱动元数据；最后 `package_ip` 在 `xc7a200t` 上跑综合，把版本号 `1.4` 传播到 `component.xml` 的版本字段/库名/显示名/xgui 脚本名/归档标签，但不动 `modelName=fpga_base_v1_0`」。这段话就是本讲的核心结论。

## 6. 本讲小结

- `package.tcl` 是一份**声明式**打包清单，按「加载 → 基本信息 → 聚合源码 → GUI 参数/端口使能 → `package_ip`」的固定顺序调用 PsiIpPackage 的 DSL，自身几乎不含控制流。
- 三个 `add_*_relative` 命令分别聚合本仓库 HDL、`psi_common` 库 HDL、C 驱动；它们的相对路径都以 `scripts/` 为锚，但框架在产物里统一**重写为以仓库根为锚**（级数减 1）。
- 所有 HDL（含 `psi_common`）被编进**同一个 VHDL 库 `fpga_base_1_4`**（`logicalName`），这正是顶层 `use work.psi_common_array_pkg.all` 能解析的原因。
- `add_drivers_relative` 只显式列 `.c/.h`，但 `data/*.mdd`、`data/*.tcl`、`src/Makefile` 由框架按 Xilinx 驱动约定**自动发现**并入产物。
- `package_ip <dir> false true xc7a200t` 四参数分别控制输出目录、不开 GUI、跑综合、目标器件（Kintex-7）；`Synth=true` 才能从 RTL 反推精确端口/参数写进 `component.xml`。
- 打包版本 `1.4` 传播到 `component.xml` 的版本字段、库名、显示名、xgui 脚本名、归档标签等多处；但 `modelName=fpga_base_v1_0` 来自 entity 名，是独立维度、不随打包版本变化。

## 7. 下一步学习建议

本讲把「打包 API 与产物对照」讲到了命令级。接下来推荐两条路线：

- **继续工程化主题（推荐）**：进入 u4-l2「GUI 参数与可选端口实现」。它会精读本讲只点到为止的两件事——`gui_create_user_parameter` 定义的 `IMPL_LED` / `IMPL_SWITCH` / `IMPL_BLINK` 这三个布尔用户参数，是如何通过 `add_port_enablement_condition` 与 `xgui/fpga_base_v1_4.tcl` 的回调，真正把 `o_led` 等物理端口从生成的硬件里裁掉的。
- **转向 BD 集成**：u4-l3「Block Design 钩子与 AXI ID 宽度传播」讲本 IP 被放进 Block Design 后，`bd/bd.tcl` 的三个回调如何自动协商 `C_S00_AXI_ID_WIDTH`。

无论走哪条，建议保留本讲画的「输入 ↔ 产物」映射图与「路径锚点换算表」，后续读 `component.xml` 任何片段时随时回看。
