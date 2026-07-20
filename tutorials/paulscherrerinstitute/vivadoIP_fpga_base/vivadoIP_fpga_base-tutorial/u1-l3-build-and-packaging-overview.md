# 构建与打包流程总览

## 1. 本讲目标

在前两讲里，我们已经认识了 `fpga_base` 是一个 PSI 维护的 Vivado IP 核（u1-l1），也把仓库看成了一个「文件柜」，知道 `hdl/` 是电路本体、`component.xml` 是 IP-XACT 元数据、`scripts/` 负责打包构建（u1-l2）。本讲要回答一个更上层的问题：

> **从一堆 VHDL 源码到一个可以在 Vivado 里例化、可以分发的 IP 包，中间到底发生了什么？**

学完本讲，你应当能够：

1. 画出「源码 → 依赖 → 打包 → 综合产物 → 可分发 zip」这条高层流水线，并指出每一步由仓库里哪个文件驱动。
2. 说清 `scripts/dependencies.py` 如何借助 README 里一段「受保护区块」解析出项目依赖，以及解析逻辑其实位于一个**外部** Python 包中。
3. 理解 `scripts/package.tcl` 调用 PSI 统一打包框架 **PsiIpPackage** 的 DSL 命令顺序，看清它和最终生成的 `component.xml` 之间的镜像关系。

本讲只做**高层俯瞰**，不深入任何一段电路逻辑（那是第 2 单元的事），也不深读版本号注入脚本 `update_version.py`（那是 u3-l3 的事）。

## 2. 前置知识

在进入源码前，先用大白话建立三个概念。

### 2.1 什么是「IP 打包（IP Packaging）」

Vivado 里的 IP 核（Intellectual Property core）不是「一个 .vhd 文件」，而是一个**带元数据的目录**：里面要有 RTL 源码、参数定义、端口定义、GUI 配置脚本、软件驱动、文档图标，以及一份描述这一切如何拼在一起的清单文件。Xilinx 用一个叫 **IP-XACT** 的 XML 标准（IEEE 1685-2009）来写这份清单，本项目里的清单就是 `component.xml`。

所谓「打包」，就是把上面这些零散的输入，按照 IP-XACT 规范组装、并在 Vivado 里跑一次综合，最终产出一个可被其他工程直接调用的 IP。打包的产物可以直接入库（本项目就把 `component.xml` 入库，参见 u1-l2 的「hand-packaged IP」工作流），也可以再压成 zip 分发。

### 2.2 什么是「依赖解析」

`fpga_base` 自己并不自给自足。它的 RTL 复用了 `psi_common` 库里的 AXI 从机（u1-l1 已埋下伏笔：1.3.0 起改用 `psi_common` 的 AXI 从机），它的打包脚本依赖 PSI 的 TCL 工具库 `PsiIpPackage`。这些库分散在不同的 git 仓库里。**依赖解析**就是用一个脚本，根据一份声明式清单，把这些外部仓库按需要的版本克隆/检出到一个约定好的目录布局里，让后续打包能找到它们。

### 2.3 为什么用 TCL 和 Python 各干一件事

Vivado 原生用 **TCL** 脚本驱动（综合、实现、打包都是 TCL 命令），所以「在 Vivado 里跑的打包逻辑」自然写成 TCL（`package.tcl`）。而「在 Vivado 之外、克隆依赖、算版本号」这类工作更适合用 **Python**（`dependencies.py`、`update_version.py`）。所以本项目里两种脚本分工明确：TCL 管 Vivado 内部，Python 管 Vivado 外围。

## 3. 本讲源码地图

本讲涉及的关键文件如下表：

| 文件 | 语言 | 角色 | 本讲用法 |
|------|------|------|----------|
| `scripts/dependencies.py` | Python | 依赖解析的**入口**（极薄的壳） | 精读全 10 行 |
| `README.md` | Markdown | 依赖清单的**唯一真相源** | 精读被解析区块 |
| `scripts/package.tcl` | TCL | IP 打包的**主编排脚本** | 精读整条调用链 |
| `component.xml` | IP-XACT/XML | 打包的**产物/镜像** | 对照印证 |
| `.gitignore` | 配置 | 声明哪些打包产物不入库 | 解释流水线产物 |
| `Changelog.md` | Markdown | 版本演进记录 | 印证机制引入时间 |

⚠️ 重要提示：`dependencies.py` 里 `import` 的 `PsiFpgaLibDependencies`，以及 `package.tcl` 里 `source` 的 `PsiIpPackage.tcl`，都是**本仓库之外**的外部库，不在当前仓库里。本讲只根据「本项目如何调用它们」来描述其行为，不臆测它们的内部实现。

## 4. 核心概念与源码讲解

### 4.1 依赖解析：把 README 当作依赖清单

#### 4.1.1 概念说明

一个 PSI 的 FPGA 工程通常要同时依赖好几类东西：

- **VHDL 库**：综合时真正需要的电路代码（如 `psi_common`）。
- **TCL 库**：只在开发/打包时需要的工具脚本（如 `PsiIpPackage`、`PsiSim`、`PsiUtil`）。
- **VivadoIp**：已打包好的其他 IP 核。

如果每个工程都手写一份「请先 clone 这些仓库」的说明，版本很容易对不齐。PSI 的做法是：**把依赖清单写进 README 的一个固定区块，再用一个脚本去解析它**。这样「人读的文档」和「机器读的清单」是同一份，不会两处维护。

#### 4.1.2 核心流程

`dependencies.py` 的逻辑可以拆成三步：

1. **定位 README**：用 `__file__` 推算出脚本自身所在目录，再拼出上一级的 `README.md` 路径。
2. **解析依赖**：调用外部包提供的 `Parse.FromReadme(...)`，读取 README 中两个 HTML 注释标记之间的内容，解析出依赖树。
3. **执行动作**：调用外部包提供的 `Actions.ExecMain(repo, dependencies)`，根据解析结果执行克隆/检出。

README 里用一对 HTML 注释作为「受保护区块」的边界：

- 开始标记：`<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->`
- 结束标记：`<!-- END OF PARSED SECTION -->`

两行之间的 `## Dependencies` 区块就是机器解析的对象，格式一旦被人为改动，解析就可能失败。

#### 4.1.3 源码精读

`dependencies.py` 全文只有 10 行，它本身几乎不含逻辑，真正的解析能力来自外部包：

[scripts/dependencies.py:1-10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L1-L10) —— 整个依赖解析入口，先 `import *` 外部包，再定位 README、解析、执行。

几个关键点：

- [scripts/dependencies.py:1](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L1) —— `from PsiFpgaLibDependencies import *` 把外部包里的 `Parse`、`Actions` 等对象引进当前作用域（**该包不在此仓库中**，必须另行安装）。
- [scripts/dependencies.py:5](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L5) —— `THIS_DIR` 用 `os.path.dirname(os.path.abspath(__file__))` 算出脚本目录，保证无论从哪里调用都能找到仓库根。
- [scripts/dependencies.py:7](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L7) —— `Parse.FromReadme(THIS_DIR + "/../README.md")` 把 README 作为依赖的唯一来源。
- [scripts/dependencies.py:10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/dependencies.py#L10) —— `Actions.ExecMain(repo, dependencies)` 真正执行拉取动作。

被解析的 README 区块如下：

[README.md:19-32](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L19-L32) —— 注意第 19 行和第 32 行那一对 HTML 注释，它们圈出的就是机器解析区间；第 21 行的 `## Dependencies` 标题，以及下面按 `TCL` / `VHDL` / `VivadoIp` 三类列出的条目，每一项都带名称、链接和版本要求。

README 也在区块外面说明了如何使用这个脚本：

[README.md:34-40](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md#L34-L40) —— 提示用 `python dependencies.py -help` 查看用法，并强调必须先安装外部依赖包。

> 说明：README 第 40 行把外部包指向名为 `PsiLibDependencies` 的仓库，而代码第 1 行 import 的名字是 `PsiFpgaLibDependencies`。这两个名字之间的确切对应关系属于外部包的事，本仓库不负责定义，标注「待确认」。对本讲而言，只需知道「存在一个必须另行安装的外部 Python 包」即可。

#### 4.1.4 代码实践

**实践目标**：不运行脚本，仅靠阅读源码，说清 `dependencies.py` 如何定位 README 的被解析区块，并总结依赖解析失败的最可能原因。

**操作步骤**：

1. 打开 `README.md`，定位第 19 行与第 32 行那一对 HTML 注释，用笔把两个标记之间（含 `## Dependencies` 区块）的内容框出来——这就是「被解析区块」。
2. 打开 `scripts/dependencies.py`，确认第 7 行把 README 作为 `Parse.FromReadme` 的输入。
3. （可选）在本机有 Vivado/PSI 环境时，于仓库根目录执行 `python scripts/dependencies.py -help`，对照帮助信息与源码。

**需要观察的现象**：

- 被解析区块里每一行依赖都形如 `* [名称](链接) (版本要求, 用途)`，分属 `TCL`、`VHDL`、`VivadoIp` 三类。
- 第 19 行的英文明确写着 "DO NOT CHANGE FORMAT"（不要改动格式），第 32 行写着 "END OF PARSED SECTION"（解析区结束）。

**预期结果**：

- 你应该能指出：`dependencies.py` 并不自己写解析规则，而是把 README 路径交给外部包的 `Parse.FromReadme`，由后者按这两个注释标记界定区块、按三类缩进结构解析条目。

**依赖解析失败时最可能的原因之一**：

- **外部 Python 包未安装**：`PsiFpgaLibDependencies`（代码 import 名）不在 Python 搜索路径里，第 1 行 `import` 直接报 `ModuleNotFoundError`，脚本根本进不到解析步骤。（这是最常见的原因，因为该包不在本仓库内。）

其余可能原因还包括：README 里两个 HTML 注释标记被误删或改写、区块内的列表格式被破坏（例如缩进或链接写法变了）、某条依赖声明的版本号格式不符合外部解析器的预期。这些都属于「人动了那份机器要读的清单」。运行结果「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 README 第 19 行的 HTML 注释整行删掉，依赖解析会怎样？
**答案**：解析器会找不到「区块开始」的边界，很可能解析不到任何依赖或直接报错。这正是第 19 行强调 "DO NOT CHANGE FORMAT" 的原因——这两个注释既是文档，也是机器契约。

**练习 2**：`dependencies.py` 为什么不直接把依赖写死在 Python 里，而要从 README 解析？
**答案**：为了避免「文档」和「机器清单」两处维护、互相不一致。README 既是给人读的，也是给脚本读的，单一真相源（single source of truth）。

---

### 4.2 IP 打包流水线：从源码到可分发 zip

#### 4.2.1 概念说明

打包流水线把本仓库里手写的输入，变换成两类产物：

- **入库存物**：`component.xml`（IP-XACT 清单）。它被打包脚本生成、又被提交进 git，便于分发（u1-l2 已讲）。
- **不入库的中间产物**：综合工程目录、zip 分发包、自动生成的 xgui 脚本等，由 `.gitignore` 排除。

理解这条流水线，关键是分清「谁是输入、谁是产物」，以及「`package.tcl`（输入的编排）与 `component.xml`（产物的快照）互为镜像」这一对应关系——这一点在 u1-l2 已经点出，本讲把它落实到具体文件。

#### 4.2.2 核心流程

`package.tcl` 在 Vivado 的 TCL 控制台里被执行，整体顺序如下（伪代码）：

```
1. source 外部 PsiIpPackage 框架，导入它的全部命令
2. init      : 声明 IP 名称/版本/库
   set_description / set_logo_relative : 写描述、贴 logo
3. add_sources_relative : 加入本仓库的 3 个 HDL 文件
   add_lib_relative    : 加入 psi_common 的 5 个 HDL 文件
   add_drivers_relative: 加入 C 驱动文件
4. gui_*             : 定义 IP 参数与 GUI 布局
5. add_port_enablement_condition : 声明哪些端口可被参数裁掉
6. package_ip        : 在目标器件 xc7a200t 上综合，写出 component.xml
```

第 6 步里会真正跑一次综合（Synth=true），所以 `component.xml` 里那些精确的端口方向、位宽、参数，是综合阶段从 RTL「反推」出来再落盘的，而不是手写的。

打包完成后，Vivado 可以把整个 IP 目录压成一个 zip 用于分发——这正是 `.gitignore` 第 2 行 `/*.zip` 要忽略的东西。

#### 4.2.3 源码精读

先看流水线两端的「输入声明」与「打包收尾」：

[scripts/package.tcl:10-18](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L10-L18) —— 声明 `IP_NAME=fpga_base`、`IP_VERSION=1.4`、`IP_LIBRARY=PSI`，再 `init`、`set_description`、`set_logo_relative`。这里的 `1.4` 会原样落到 `component.xml` 第 6 行的 `<spirit:version>1.4</spirit:version>`。

聚合三类源码：

[scripts/package.tcl:24-47](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L24-L47) —— `add_sources_relative` 列出本仓库 3 个 HDL 文件（日期包、顶层、脚本化信息包）；`add_lib_relative` 用相对路径 `../../../VHDL/psi_common/hdl` 引入 5 个 `psi_common` 文件；`add_drivers_relative` 引入 C 驱动。

注意 `../../../` 这个写法——它假定了一套 **PSI 约定的目录布局**：本仓库被克隆到某个根目录下，而 `psi_common` 等兄弟仓库作为同级目录存在。这也正是 4.1 节依赖解析要负责建立的布局。换句话说，**依赖解析（Python）先搭好目录结构，打包脚本（TCL）才能用相对路径找到依赖库**。

最后是收尾的综合打包：

[scripts/package.tcl:95-97](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L95-L97) —— `package_ip $TargetDir false true xc7a200t`。第 96 行的注释 `# Edit Synth` 说明三个标志位：`Edit=false`（不打开 GUI 手动编辑）、`Synth=true`（跑综合）、目标器件 `xc7a200t`（一颗 Kintex-7 芯片）。综合的结果就是 `component.xml`。

产物侧印证：`component.xml` 的综合文件集里确实包含了这 8 个 HDL 文件（3 个本仓库 + 5 个 psi_common），且顺序上把顶层 `fpga_base_v1_0.vhd` 放在最后：

[component.xml:1191-1235](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1191-L1235) —— `<spirit:fileSets>` 下的 `xilinx_anylanguagesynthesis_view_fileset`，先列 `fpga_base_date_package.vhd`、`fpga_base_scripted_info_pkg.vhd`，中间夹着 5 个 `psi_common` 文件，最后是顶层 `fpga_base_v1_0.vhd`（顶层放最后是 IP-XACT 的惯例，方便综合工具自底向上解析）。

`xc7a200t` 对应的器件族也能在产物里找到：

[component.xml:1382](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1382) —— `<xilinx:family ...>kintex7</xilinx:family>`，正是 `xc7a200t` 所属的 Kintex-7 系列（该 IP 支持的器件族列表见其前后几行）。

最后，哪些产物不入库由 `.gitignore` 声明：

[.gitignore:1-11](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L1-L11) —— 第 2 行忽略根目录 zip 分发包；第 5 行忽略 `scripts/package_prj`（打包临时工程目录）；第 6 行忽略自动生成的旧版 xgui 脚本；第 11 行忽略 Python 的 `*.pyc`。

#### 4.2.4 代码实践

**实践目标**：对照「输入」`package.tcl` 与「产物」`component.xml`，验证二者互为镜像。

**操作步骤**：

1. 在 `scripts/package.tcl` 第 24-28 行的 `add_sources_relative` 列表里数出 3 个 HDL 文件名。
2. 在 `component.xml` 第 1192-1235 行的 `xilinx_anylanguagesynthesis_view_fileset` 里找到同名条目。
3. 做一张映射表：左列是 `package.tcl` 的来源命令，右列是 `component.xml` 里落到的文件集（fileSet）。

**需要观察的现象**：

- `add_sources_relative` 的 3 个文件 + `add_lib_relative` 的 5 个 psi_common 文件，都落在同一个 `xilinx_anylanguagesynthesis_view_fileset` 文件集里。
- `add_drivers_relative` 的 C 驱动文件落在另一个文件集 `xilinx_softwaredriver_view_fileset`（`component.xml` 第 1295-1317 行附近）。

**预期结果**：

| `package.tcl` 中的命令 | 涉及文件 | `component.xml` 中对应的 fileSet |
|------------------------|----------|-----------------------------------|
| `add_sources_relative` | `fpga_base_date_package.vhd`、`fpga_base_scripted_info_pkg.vhd`、`fpga_base_v1_0.vhd` | `xilinx_anylanguagesynthesis_view_fileset`（含仿真文件集） |
| `add_lib_relative` | `psi_common` 的 5 个 `.vhd` | 同上（路径前缀 `../../VHDL/psi_common/hdl/`） |
| `add_drivers_relative` | `fpga_base.c`、`fpga_base.h` 等 | `xilinx_softwaredriver_view_fileset` |

如果你本机装有 Vivado 和 `PsiIpPackage`，可尝试按 README 指引在 TCL 控制台 `source scripts/package.tcl` 观察产物生成；否则本实践为「源码阅读型」，运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `package.tcl` 里写 `IP_VERSION 1.4`，而 `Changelog.md` 顶部是 `1.4.0`？
**答案**：IP-XACT 的版本字段只到两级（`<spirit:version>1.4</spirit:version>`，即主.次），而 `Changelog.md` 用三级语义化版本（主.次.补丁）。两者粒度不同：`1.4` 是 IP 打包版本号，`1.4.0` 是源码变更记录号。

**练习 2**：`package_ip` 的 `Synth=true`（第 97 行）为什么重要？
**答案**：只有真正跑综合，Vivado 才能从 RTL 反推出每个端口的精确方向/位宽、参数的有效取值，并把它们写进 `component.xml`。若 `Synth=false`，产出的元数据会不完整，下游例化时可能出错。

---

### 4.3 PsiIpPackage 框架：PSI 的统一打包 DSL

#### 4.3.1 概念说明

直接用 Vivado 原生命令打包一个 IP 非常繁琐：要手动建工程、加文件、设参数、配端口使能、跑综合、再 export。PSI 把这些重复步骤封装成了一个 TCL 库 **PsiIpPackage**，提供一套小而稳定的 DSL（领域专用命令），让每个 IP 的打包脚本都能写得简短且一致。

`fpga_base` 的 `package.tcl` 几乎全是 PsiIpPackage 提供的命令（`init`、`add_sources_relative`、`gui_create_parameter`、`package_ip` 等），自己只负责按顺序调用它们并填参数。这就是为什么一个能产出完整 IP 的脚本只有不到 100 行。

#### 4.3.2 核心流程

使用 PsiIpPackage 的固定套路是「加载 + 导入 + 调用」：

1. **加载**：用 `source` 把外部的 `PsiIpPackage.tcl` 读进来。
2. **导入**：用 `namespace import` 把框架最新版本（`latest`）的全部命令导到当前命名空间，省得每条命令都写全名前缀。
3. **调用**：按「信息 → 源码 → 参数 → 端口 → 打包」的分类顺序调用 DSL 命令。

PsiIpPackage 的命令大致可分五类：

| 类别 | 代表命令（见 `package.tcl`） | 作用 |
|------|------------------------------|------|
| 基本信息 | `init`、`set_description`、`set_logo_relative` | 设 IP 名称/版本/库、描述、logo |
| 源码聚合 | `add_sources_relative`、`add_lib_relative`、`add_drivers_relative` | 把 RTL、依赖库、驱动加入 IP |
| GUI 参数 | `gui_add_page`、`gui_create_parameter`、`gui_create_user_parameter`、`gui_add_parameter` | 定义 IP 配置页面与参数 |
| 端口使能 | `add_port_enablement_condition` | 用参数控制某端口是否生成 |
| 打包收尾 | `package_ip` | 在指定器件上综合并写出 IP |

#### 4.3.3 源码精读

框架的加载与导入在最开头：

[scripts/package.tcl:1-5](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L1-L5) —— 第 4 行 `source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl` 加载外部框架（**不在本仓库**，由依赖解析事先放好）；第 5 行 `namespace import -force psi::ip_package::latest::*` 导入 `psi::ip_package::latest` 命名空间下的全部命令，`-force` 保证重复 source 时覆盖旧定义。

GUI 参数定义示例（DSL 风格的典型写法）：

[scripts/package.tcl:54-82](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L54-L82) —— 先 `gui_add_page "Configuration"` 建一页，再用 `gui_create_parameter` 逐个声明普通参数（如 `C_VERSION`），用 `gui_create_user_parameter` 声明用户参数（如 `IMPL_LED` 这类布尔开关），每个参数都紧跟一个 `gui_add_parameter` 把它真正加进 GUI。

端口使能与参数联动：

[scripts/package.tcl:84-90](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L84-L90) —— `add_port_enablement_condition "o_led" "\$IMPL_LED"` 表示：只有当用户参数 `IMPL_LED` 为真时，端口 `o_led` 才会被生成；`o_blink`、`i_sw` 同理。这就是「用布尔参数裁剪硬件端口」的实现入口（细节留到 u4-l2）。

> 说明：PsiIpPackage 的命令实现位于外部 TCL 仓库，本讲只依据 `package.tcl` 对它的**调用方式**来归类，不描述其内部 Tcl 过程体。

#### 4.3.4 代码实践

**实践目标**：把 `package.tcl` 里出现的所有「非标准 TCL」命令按 4.3.2 的五类归类，理解 DSL 的职责划分。

**操作步骤**：

1. 通读 `scripts/package.tcl`，挑出所有 `init`、`set_*`、`add_*`、`gui_*`、`package_ip` 命令。
2. 对照 4.3.2 的分类表，把每条命令归入相应类别。
3. 用一句话写出每类命令「改变的是 IP 的哪个侧面」（例如：GUI 参数类改变的是「用户在 Vivado 里能配什么」）。

**需要观察的现象**：

- 整个脚本里看不到任何 Vivado 原生的 `create_project` / `add_files` / `synth_design` 之类长命令——它们都被 PsiIpPackage 的 DSL 封装掉了。
- 「信息」类命令集中在脚本上半部，「打包收尾」只有最后一条 `package_ip`。

**预期结果**：

- 你应该能得出结论：`package.tcl` 是一份**声明式的打包配置**，它「声明这个 IP 长什么样」，而把「如何让 Vivado 真的把它造出来」交给 PsiIpPackage 框架。这正是 DSL 的价值——把繁琐的过程式 Vivado 操作，压成一份短小、可读、可维护的清单。

#### 4.3.5 小练习与答案

**练习 1**：`namespace import` 时为什么要带 `latest`？
**答案**：PsiIpPackage 用命名空间区分版本（如 `psi::ip_package::latest`）。导入 `latest` 表示始终用框架的最新版命令，这样升级框架时打包脚本不必改命名空间前缀。

**练习 2**：普通参数（`gui_create_parameter`）和用户参数（`gui_create_user_parameter`）在本项目里的区别是什么？
**答案**：从 `package.tcl` 第 56-82 行可见，`C_VERSION`、`C_FREQ_AXI_CLK_HZ` 等是普通参数（通常由设计传入、对应 RTL 的 generic）；而 `IMPL_BLINK`、`IMPL_SWITCH`、`IMPL_LED` 是布尔用户参数，它们不出现在 RTL 的 generic 里，而是被 `add_port_enablement_condition` 用来控制端口是否生成（即裁剪硬件结构）。这一区别会在 u4-l2 详细展开。

## 5. 综合实践

把本讲三个模块串起来，完成一个「流水线追踪」小任务：

1. **依赖侧**：打开 `README.md`，把第 19-32 行被解析区块里的 4 条依赖（PsiSim、PsiIpPackage、PsiUtil、psi_common）抄下来，并标注每条属于 TCL / VHDL / VivadoIp 哪一类、是「仅开发期」还是「综合必需」。再打开 `Changelog.md` 第 8-12 行，确认「依赖解析脚本」和「改用 psi_common 的 AXI 从机」都是在 1.3.0 引入的——这两件事其实是配套的。
2. **打包侧**：在 `scripts/package.tcl` 里，从第 4 行的 `source` 一路读到第 97 行的 `package_ip`，在纸上画出这条 DSL 调用链；然后到 `component.xml` 第 1191-1317 行，核对你画出的每个「源码聚合」命令产出了哪个 fileSet。
3. **产物侧**：打开 `.gitignore`，指出第 2、5、6、11 行分别忽略的是流水线的哪一类产物（分发包 / 综合工程目录 / 自动生成脚本 / Python 字节码）。

**验收标准**：你能用一段话讲清楚——「Python 的 `dependencies.py` 先把外部库按 PSI 布局克隆好 → TCL 的 `package.tcl` 用相对路径找到它们并通过 PsiIpPackage 的 DSL 打包 → 综合后生成入库的 `component.xml`，其余中间产物被 `.gitignore` 排除」。这段话就是本讲的核心结论。

## 6. 本讲小结

- 本讲只做高层俯瞰，不碰电路逻辑：流水线是「源码 → 依赖 → 打包 → 综合产物 → zip」。
- `dependencies.py` 是个 10 行的薄壳，真正的解析能力来自外部包 `PsiFpgaLibDependencies`；依赖清单的真相源是 README 中两个 HTML 注释之间的受保护区块。
- `package.tcl` 是打包主编排脚本，靠相对路径 `../../../` 找到 PSI 布局下的兄弟仓库——所以依赖解析必须先于打包完成。
- `package.tcl`（输入声明）与 `component.xml`（产物快照）互为镜像：`IP_VERSION 1.4` ↔ `<spirit:version>1.4</spirit:version>`，`add_sources_relative` ↔ `xilinx_anylanguagesynthesis_view_fileset`，`xc7a200t` ↔ `kintex7`。
- PsiIpPackage 是 PSI 的统一打包 DSL，把繁琐的 Vivado 原生操作压成一套「信息/源码/参数/端口/打包」五类命令，使打包脚本不到 100 行。
- 不入库的产物（zip、`package_prj`、自动生成 xgui、`*.pyc`）由 `.gitignore` 声明，符合 u1-l2 讲过的 hand-packaged IP 工作流。

## 7. 下一步学习建议

本讲建立了「打包流水线」的全景。接下来有两条推荐路线：

- **想深入硬件接口**：进入第 2 单元「AXI4 从机寄存器接口」。建议从 u2-l1「顶层实体：端口、泛型与外部接口」开始，你会看到本讲提到的 `component.xml` 里那一大堆 `s00_axi_*` 端口，在 RTL 顶层到底是怎么定义的。
- **想继续工程化主题**：可以先读第 4 单元 u4-l1「用 PsiIpPackage 打包 IP 核」，它会从本讲的 DSL 概览下沉到每条命令的参数细节，并与 `component.xml` 做更细致的对照。

无论走哪条，建议保留本讲画的「输入 ↔ 产物」映射表，后续读 `component.xml` 时随时回看。
