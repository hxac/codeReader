# 仓库目录结构与文件角色详解

## 1. 本讲目标

上一篇你已经认识了 `fpga_base` 这个 IP 核「是什么、解决什么问题」。本篇换一个视角：**把整个仓库当作一个「文件柜」，搞清楚每个抽屉里放的是什么。**

读完本讲，你应当能够：

- 在脑子里画出 `vivadoIP_fpga_base` 仓库的**目录地图**，知道 `hdl`、`scripts`、`bd`、`gui`、`xgui`、`drivers`、`epics`、`doc` 这几个目录各自承担什么职责。
- 区分**手写文件**（开发者写的源码）和**生成文件**（工具自动产出的产物），并能解释为什么有些生成文件被提交进版本库、有些却被 `.gitignore` 忽略。
- 看懂根目录下的 `component.xml`——它是 **IP-XACT** 标准的元数据文件，是整个仓库「被 Vivado 识别为 IP」的入口。

本讲仍是 beginner 级别，不要求你懂 VHDL 或 AXI 细节，只要会「读目录、读配置」即可。承接上一篇建立的认知（IP 核、Vivado、AXI、IP-XACT、PSI），本讲不再重复这些名词的定义。

## 2. 前置知识

本讲会用到的几个概念，先用大白话补一下：

- **IP-XACT**：一个用 XML 描述「IP 核长什么样」的国际标准（IEEE 1685-2009）。它告诉 Vivado：这个 IP 叫什么名字、是哪个版本、有哪些端口、参数、依赖哪些源码文件。仓库根目录的 `component.xml` 就是这样一个文件。上一篇已简单提过，本篇会深入它的结构。
- **fileSet（文件集）**：IP-XACT 里把文件按「用途」分组的方式。比如「综合用的源码」是一个文件集，「软件驱动」是另一个文件集。Vivado 根据文件集决定哪些文件在什么时候被使用。
- **打包（Packaging）**：把手写的 VHDL 源码、TCL 脚本、驱动文件「组装」成一个 Vivado 能识别的 IP 的过程。打包会**生成** `component.xml`，以及一些临时工程目录、压缩包等产物。
- **生成文件 vs 手写文件**：手写文件是开发者用编辑器一个字一个字敲出来的，是「源头」；生成文件是工具跑出来的，理论上随时可以重新生成。区分二者是理解仓库结构的关键。

理解这几个概念之后，我们开始逛仓库。

## 3. 本讲源码地图

本讲要读的文件，按「从外到内」的顺序：

| 文件 | 作用 |
| --- | --- |
| [.gitignore](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore) | 告诉 Git 哪些文件不要提交（多为打包/构建产物）。是判断「生成文件」的第一手线索。 |
| [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml) | IP-XACT 元数据，整个 IP 的「身份证 + 零件清单」。它登记了端口、参数、文件集、支持的器件。 |
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/README.md) | 项目门面，上一篇已读；本讲只用它佐证「哪些目录是源码、哪些是依赖」。 |
| [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl) | 打包脚本，它的「源码清单」与 `component.xml` 的文件集一一对应，是理解生成过程的钥匙。 |

此外会**指路**（不精读）的目录文件：`hdl/`、`bd/bd.tcl`、`gui/fpga_base_v1_0.gtcl`、`xgui/fpga_base_v1_4.tcl`、`drivers/`、`epics/`、`doc/`，以及根目录的 `fpga_base.tcl`、`jtag_to_axi_master_cmd.tcl`。

## 4. 核心概念与源码讲解

### 4.1 目录职责划分

#### 4.1.1 概念说明

打开这个仓库，你会看到一堆文件散落在根目录和若干子目录里。乍看杂乱，其实遵循一个很清晰的约定：**按「文件类型/用途」分目录**。Vivado IP 项目的目录划分有一套社区惯例（Xilinx 官方打包向导也大致按这个结构组织），PSI 的 `fpga_base` 基本沿用。

先看一张「目录地图」总览：

```text
vivadoIP_fpga_base/
├── README.md              项目门面（维护者、许可证、依赖清单）
├── Changelog.md           版本演进记录
├── License.txt / LGPL2_1.txt   许可证全文
├── component.xml          ★ IP-XACT 元数据（IP 的"身份证+零件清单"）
├── .gitignore             Git 忽略规则
├── fpga_base.tcl          综合/实现阶段 TCL 钩子（写编译日期）
├── jtag_to_axi_master_cmd.tcl   硬件调试 TCL（JTAG 读写寄存器）
│
├── hdl/                   ★ VHDL 硬件源码（IP 的电路本体）
├── scripts/               打包 / 依赖 / 版本脚本
├── bd/                    Block Design 回调脚本
├── gui/                   HDL 参数生成脚本（.gtcl）
├── xgui/                  Vivado IP 参数界面布局脚本
├── drivers/               裸机 C 驱动（给 CPU 软件用）
├── epics/                 EPICS 控制系统模板
└── doc/                   文档与 Logo（PDF 数据手册、Logo 图片）
```

打 ★ 的是「理解这个仓库最核心」的几处：`hdl/` 是电路本体、`component.xml` 是元数据入口。其余目录都是围绕它们「配套」的脚本、驱动、文档。

#### 4.1.2 核心流程

把这些目录按「角色」归类，可以分成五组：

| 角色 | 包含的目录/文件 | 一句话职责 |
| --- | --- | --- |
| **电路本体** | `hdl/*.vhd` | 真正描述 FPGA 硬件的 VHDL 代码 |
| **IP 元数据** | `component.xml` | 登记 IP 的端口/参数/文件清单，让 Vivado 认得它 |
| **打包与构建** | `scripts/`、`bd/`、`gui/`、`xgui/`、根目录两个 `.tcl` | 把源码组装、配置、注入信息的各类 TCL/Python 脚本 |
| **软件侧** | `drivers/`、`epics/` | CPU 裸机驱动、实验控制系统的记录模板 |
| **文档与门面** | `doc/`、`README.md`、`Changelog.md`、`License*.txt` | 给人读的说明 |

记忆口诀：**「hdl 是肉体，component.xml 是身份证，scripts/bd/gui/xgui 是组装工具，drivers/epics 是给软件的接口，doc/README 是说明书。」**

#### 4.1.3 源码精读

逐个目录点一下里面装了什么（只点到「这个文件干什么」，不展开内部逻辑——那是后续单元的事）：

- **`hdl/`（电路本体）**：三个 VHDL 文件。
  - `fpga_base_v1_0.vhd`：IP 的**顶层实体**，定义端口、实例化 AXI 从机和寄存器逻辑。
  - `fpga_base_date_package.vhd`：用 FDPE 触发器存放**固件编译日期**的包（第三单元精读）。
  - `fpga_base_scripted_info_pkg.vhd`：用占位符存放 **git hash / 脚本化版本信息**的包。

- **根目录 `fpga_base.tcl`**：综合阶段的 TCL 钩子。文件头注释写得很清楚——它「把编译日期和时间写到包含 fpga_base 的设计里」，见 [fpga_base.tcl:11-12](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/fpga_base.tcl#L11-L12)。

- **根目录 `jtag_to_axi_master_cmd.tcl`**：硬件调试用的 TCL，通过 JTAG-to-AXI 主机在真实芯片上读写寄存器。文件头说明它面向 Arty 板（XC7A35T），见 [jtag_to_axi_master_cmd.tcl:10](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L10)，第一个函数是写 LED，见 [jtag_to_axi_master_cmd.tcl:16](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/jtag_to_axi_master_cmd.tcl#L16)。

- **`scripts/`**：三个脚本。
  - `package.tcl`：调用 PSI 的 PsiIpPackage 框架打包 IP（第四单元精读）。它用 `add_sources_relative` 登记三个 HDL 文件，见 [scripts/package.tcl:24-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L24-L28)。
  - `dependencies.py`：从 README 解析依赖清单（上一篇已介绍）。
  - `update_version.py`：用 git hash 注入版本信息（第三单元精读）。

- **`bd/bd.tcl`**：Block Design 回调，处理 AXI ID 宽度在主从接口间的自动传播（第四单元精读）。它定义了 `init`、`pre_propagate` 等回调，见 [bd/bd.tcl:2](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L2) 与 [bd/bd.tcl:25](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/bd/bd.tcl#L25)。

- **`gui/fpga_base_v1_0.gtcl`**：扩展名为 `.gtcl`（gui tcl），定义若干 `gen_HDLPARAMETER_*` 过程，用于由用户参数**推导**出 HDL generic 的值，见 [gui/fpga_base_v1_0.gtcl:2-7](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/gui/fpga_base_v1_0.gtcl#L2-L7)。

- **`xgui/fpga_base_v1_4.tcl`**：Vivado IP 参数界面的布局脚本，定义 `init_gui` 把参数摆到「Configuration」页上，见 [xgui/fpga_base_v1_4.tcl:2-15](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L2-L15)。

- **`drivers/fpga_base/`**：裸机 C 驱动，分 `src/`（`fpga_base.c`、`fpga_base.h`、`Makefile`）和 `data/`（`fpga_base.mdd`、`fpga_base.tcl`，Vitis 集成用的元数据）。第五单元精读。

- **`epics/FPGA_BASE.template`**：EPICS 控制系统的记录模板，把寄存器映射成 EPICS 记录（第五单元精读）。

- **`doc/`**：`fpga_base.pdf`（数据手册）、`fpga_base.docx`（手册源文档）、`psi_logo_150.gif`（IP 在 Vivado 里显示的 Logo）。

> 小贴士：你可能在 `doc/` 下还看到 `~WRL0003.tmp`、`~$ga_base.docx` 之类的文件——那是 Microsoft Word 打开文档时留下的**临时锁文件**，不是项目正式内容（严格说也应该被忽略，属历史遗留，不必纠结）。

#### 4.1.4 代码实践

**实践目标**：用「角色五分组」的框架，亲手把仓库里的每个文件归位，建立牢固的目录心智模型。

**操作步骤**：

1. 在本地用文件管理器或 `ls -R`（或 IDE 的文件树）浏览仓库，对照上面的目录地图。
2. 仿照本节的「角色」表格，自己画一张表，把根目录和每个子目录里的**每一个文件**填进对应的角色格里（电路本体 / IP 元数据 / 打包与构建 / 软件侧 / 文档与门面）。
3. 对拿不准的文件，打开它的头部注释看「Unit / Comment」说明再归类（例如 `fpga_base.tcl` 头部就写明了用途）。

**需要观察的现象**：你会发现 `hdl/` 里的文件全部归入「电路本体」，而根目录与 `scripts/`、`bd/`、`gui/`、`xgui/` 下的 `.tcl`/`.py` 文件全部归入「打包与构建」。

**预期结果**：得到一张完整的「文件 → 角色」对照表。没有一个文件无法归类（如果有，说明你遇到了一个新角色，值得记下来）。

**待本地验证**：如果你本地克隆了仓库，可以直接 `git ls-files` 列出全部被跟踪的文件，与你的表格交叉核对。

#### 4.1.5 小练习与答案

**练习 1**：如果有人问你「`fpga_base` 这个 IP 的硬件电路到底写在哪」，你应该指向哪个目录？

> **答案**：`hdl/` 目录。里面三个 `.vhd` 文件是真正的 VHDL 硬件描述，其中 `fpga_base_v1_0.vhd` 是顶层。`component.xml` 只是描述这些源码的「清单」，本身不是电路。

**练习 2**：根目录有两个 `.tcl` 文件（`fpga_base.tcl` 和 `jtag_to_axi_master_cmd.tcl`），它们都放在根目录而不是 `scripts/` 下，结合文件头注释猜猜为什么。

> **答案**：它们都不是「打包流水线」的一部分，而是**挂到 Vivado 工程/硬件会话上的钩子或命令集**。`fpga_base.tcl` 是综合阶段的 `tcl.pre` 钩子（Vivado 通过 IP-XACT 的 utility TTCL 机制找到它），`jtag_to_axi_master_cmd.tcl` 是给硬件调试会话手动 source 的命令库。放在根目录便于工具和开发者一眼定位，与 `scripts/` 里「主动运行的构建脚本」性质不同。

---

### 4.2 手写文件与生成文件

#### 4.2.1 概念说明

仓库里的文件并非「生而平等」。分清两类非常重要：

- **手写文件（source of truth，真相之源）**：开发者亲手写的，是项目的「第一手内容」。比如 `hdl/*.vhd`、`drivers/fpga_base/src/fpga_base.c`、`README.md`、`scripts/package.tcl`。这些文件丢了，项目就真的丢了。
- **生成文件（artifacts，产物）**：工具跑出来的，能从手写文件**重新生成**。比如打包产出的 `component.xml`、Vivado 综合时生成的临时工程目录、Python 跑出来的 `.pyc` 字节码。

这里有个容易混淆的点：**`component.xml` 是「生成文件」，但它被提交进了版本库**。为什么？因为 PSI 采用「手工打包 + 把产物入库」的工作流（`.gitignore` 第 1 行的注释 `#GITignore for hand-packaged IP` 就点明了这一点，见 [.gitignore:1](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L1)）。这样使用者克隆仓库后，不需要重新打包就能直接把 IP 加进 Vivado。而真正「用完即扔」的中间产物（zip 包、临时工程目录）才被 `.gitignore` 排除。

理解了这个权衡，你就能解释 `.gitignore` 里每一条规则存在的理由。

#### 4.2.2 核心流程

判断一个文件「手写还是生成」的简单决策流程：

```text
            这个文件是开发者用编辑器写出来的吗？
                       /            \
                 是（手写）         否（可能是生成）
                    |                    |
            归类为 source         能否用某个命令重新生成？
                                       /          \
                                 能（生成产物）   否（可能是
                                    |             二进制/锁文件）
                              看 .gitignore：
                              - 被忽略  → 用完即扔的中间产物
                              - 入库    → 便于分发的产物（如 component.xml）
```

`xgui/fpga_base_v1_4.tcl` 是一个有意思的「中间态」例子：它的第一行写着 `# This file is automatically written. Do not modify.`，见 [xgui/fpga_base_v1_4.tcl:1](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/xgui/fpga_base_v1_4.tcl#L1)——说明它最初是 Vivado **生成**的框架；但开发者随后在里面手写了 `init_gui` 等回调逻辑，所以它被提交入库，属于「生成框架 + 手写内容」的混合体。

#### 4.2.3 源码精读

`.gitignore` 是判断「生成产物」的第一手证据，完整内容如下：

- 忽略最终 IP 压缩包，见 [.gitignore:2](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L2)：`/*.zip`——打包完成后导出的可分发 zip，体积大且可随时重新打包，不入库。

- 忽略打包临时工程目录，见 [.gitignore:4-6](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L4-L6)：
  - `scripts/package_prj`：`package.tcl` 运行时在 `scripts/` 下创建的 Vivado 工程目录，里面是综合过程的中间文件。
  - `xgui/fpga_base_v1_0_v1_0.tcl`：Vivado 重新打包时**自动再生成**的一份 xgui 脚本（命名规律是 `<ip>_v<core版本>_v<实例版本>.tcl`），与手写维护的 `xgui/fpga_base_v1_4.tcl` 重复，所以忽略掉自动生成的那份。

- 忽略 IDE 配置，见 [.gitignore:8-9](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L8-L9)：`**/.idea`（PyCharm/IntelliJ 的工程配置，因人而异，不入库）。

- 忽略 Python 字节码，见 [.gitignore:10-11](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L10-L11)：`*.pyc`——运行 `update_version.py` / `dependencies.py` 时 Python 解释器自动生成的字节码缓存。

反过来，**被提交入库的生成文件**最典型的就是 `component.xml`。它是 `scripts/package.tcl` 跑出来的产物，但被纳入版本控制以便直接分发。打包脚本设置 IP 名、版本、描述的地方见 [scripts/package.tcl:10-18](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L10-L18)，这些值最终会出现在 `component.xml` 的对应字段里（下一节详述）。

#### 4.2.4 代码实践

**实践目标**：本讲的指定实践任务——基于 `.gitignore` 与目录结构，列出 3 个被 Git 忽略的「打包/构建产物」，并解释为什么不该入库。

**操作步骤**：

1. 打开 [.gitignore](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore) 全文。
2. 找出所有「与打包/构建相关」的忽略规则（排除 `**/.idea` 这种纯 IDE 配置）。
3. 对每一条，写一句话：「它是什么 → 为什么不该入库」。

**需要观察的现象**：与打包/构建直接相关的忽略条目至少有三个：`/*.zip`、`scripts/package_prj`、`xgui/fpga_base_v1_0_v1_0.tcl`（外加 `*.pyc` 是 Python 运行产物）。

**预期结果**（参考答案）：

| 被忽略的产物 | 它是什么 | 为什么不该入库 |
| --- | --- | --- |
| `/*.zip` | 打包后导出的 IP 分发压缩包 | 体积大，且可由 `package.tcl` 随时重新生成；入库会造成仓库膨胀和频繁无意义 diff |
| `scripts/package_prj` | `package.tcl` 运行时生成的 Vivado 临时工程目录 | 综合过程的中间文件，机器/路径相关，可重新生成，入库只会制造噪声 |
| `xgui/fpga_base_v1_0_v1_0.tcl` | Vivado 重新打包时自动生成的重复 xgui 脚本 | 与手写维护的 `xgui/fpga_base_v1_4.tcl` 内容重复，是自动产物；保留手写那份即可 |

**核心结论**：它们都是**可由源码重新生成的产物**，提交进版本库既浪费空间，又容易把「与本机环境有关的临时状态」混进历史，破坏可追溯性。

**待本地验证**：如果你本地跑过 `scripts/package.tcl`，可以用 `git status` 观察到这些被忽略的文件/目录确实出现过了，但不会出现在「待提交」列表里。

#### 4.2.5 小练习与答案

**练习 1**：`component.xml` 是生成文件，为什么它不像 `scripts/package_prj` 那样被 `.gitignore` 忽略？

> **答案**：因为 PSI 采用「hand-packaged IP」工作流——把可分发的产物（`component.xml`）入库，让使用者克隆后无需重新打包就能直接用 IP。而 `package_prj` 只是打包**过程中**的临时工程，不可分发、可随时重建，所以忽略。一句话：**「分发用的产物入库，过程用的中间产物忽略」**。

**练习 2**：`xgui/fpga_base_v1_4.tcl` 第一行说「Do not modify」，但仓库里它的 `init_gui` 明显是手写布局。这两者矛盾吗？怎么理解？

> **答案**：不矛盾。这句提示是 Vivado 打包向导**首次生成**该文件时写下的模板注释，提醒你「别手工从零改结构」。实际开发中，PSI 在这个生成框架里**填入了**参数布局与回调（`init_gui`、`update_PARAM_VALUE.*` 等）。这是 Vivado IP 开发的常见模式：工具生成骨架，开发者填业务回调。

**练习 3**：如果你不小心把 `scripts/package_prj` 目录提交进了版本库，最先该检查什么？

> **答案**：检查 `.gitignore` 是否包含 `scripts/package_prj` 这条规则（本仓库已包含，见 [.gitignore:5](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/.gitignore#L5)）。如果规则在但文件仍被跟踪，说明它是在加入 `.gitignore` **之前**就已提交，需要用 `git rm --cached` 把它从索引移除，再提交一次。

---

### 4.3 IP-XACT 元数据（component.xml）

#### 4.3.1 概念说明

如果说 `hdl/` 是 IP 的「肉体」，那么 `component.xml` 就是它的**身份证 + 零件清单 + 接口规格书**，三者合一。它用 IP-XACT 标准（IEEE 1685-2009，文件第 2 行的 `xmlns:spirit="http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009"` 就是这个标准的命名空间）回答了 Vivado 关心的全部问题：

- 这个 IP 叫什么、版本号多少、谁出品？
- 它对外暴露哪些**端口**（port）？哪些是 AXI 信号、哪些是 LED/开关？
- 使用者可以配置哪些**参数**（parameter）？默认值是多少？
- 它由哪些**源码文件**组成？每个文件属于哪个文件集、什么用途？
- 它支持哪些 FPGA 器件系列？

Vivado 在「把 IP 加入工程」时，读的就是这个文件。没有它，仓库就只是一堆散落的 VHDL，Vivado 不认。

> 名词解释：IP-XACT 里每一层都用 `spirit:` 前缀（Spirit 是制定该标准的组织旧名）。你会反复看到 `spirit:component`、`spirit:busInterface`、`spirit:port`、`spirit:fileSet`——把它们想成「组件 / 总线接口 / 端口 / 文件集」即可。

#### 4.3.2 核心流程

`component.xml` 的顶层结构可以概括成这棵树：

```text
spirit:component
├── vendor / library / name / version     ← IP 的"坐标"（psi.ch / PSI / fpga_base / 1.4）
├── busInterfaces                          ← 总线接口（AXI 从机、时钟、复位）
├── memoryMaps                             ← 地址映射（寄存器空间 base=0, range=256, width=32）
├── model
│   ├── views                             ← "视图"（综合视图、仿真视图、xgui 视图、驱动视图…）
│   ├── ports                             ← 所有物理端口（o_led / s00_axi_* …）
│   └── modelParameters                   ← 传给 HDL 的 generic（C_VERSION 等）
├── fileSets                              ← 文件清单（按用途分组）
├── description                           ← 一句话描述
├── parameters                            ← 用户可配置参数（IMPL_LED 等）
└── vendorExtensions                      ← Xilinx 私有扩展（支持的器件系列、Logo、校验和…）
```

这个文件是 `scripts/package.tcl` 调用 PsiIpPackage 框架**生成**出来的：你在 `package.tcl` 里写 `set IP_NAME fpga_base` / `set IP_VERSION 1.4`、用 `add_sources_relative {...}` 列出源码，PsiIpPackage 就把这些信息翻译成 `component.xml` 里的对应字段。

#### 4.3.3 源码精读

挑几个最值得认识的字段来看（行号均对应当前 HEAD 的 `component.xml`）：

- **IP 的「坐标」与版本**：见 [component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L3-L6)——`vendor=psi.ch`、`library=PSI`、`name=fpga_base`、`version=1.4`。这正是 `package.tcl` 里 `set IP_NAME` / `set IP_VERSION` 的镜像。

- **地址映射（寄存器空间大小）**：见 [component.xml:347-358](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L347-L358)——`baseAddress=0`、`range=256`、`width=32`、`usage=register`。意思是：这个 IP 暴露一段 256 字节、32 位宽的寄存器空间，起始偏移为 0。256 字节 = 64 个 32 位寄存器，这正对应后续单元会讲到的「64 个寄存器」。（呼应上一篇 1.1.0 changelog 提到的「AXI 地址范围固定 8 位」——8 位地址 = 256 字节。）

- **端口与「可选实现」**：见 `o_led` 端口及其使能条件 [component.xml:437-463](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L437-L463)，其中使能依赖写在 [component.xml:459](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L459)：`xilinx:dependency="$IMPL_LED"`——意思是「只有当用户参数 `IMPL_LED` 为真时，`o_led` 端口才会被实现」。同理 `i_sw` 依赖 `IMPL_SWITCH`、`o_blink` 依赖 `IMPL_BLINK`。这是 IP-XACT 表达「可选端口」的标准手法。

- **文件集（fileSets）——零件清单**：见 [component.xml:1191-1318](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1191-L1318)。共有 5 个文件集，每个对应一个「视图」：

  | 文件集名 | 用途 | 里面的代表文件 |
  | --- | --- | --- |
  | `xilinx_anylanguagesynthesis_view_fileset` | 综合（生成电路） | `hdl/*.vhd` + psi_common 的 5 个 `.vhd`，见 [component.xml:1192-1235](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1192-L1235) |
  | `xilinx_anylanguagebehavioralsimulation_view_fileset` | 仿真 | 同上源码，见 [component.xml:1236-1278](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1236-L1278) |
  | `xilinx_xpgui_view_fileset` | IP 参数界面 | `xgui/fpga_base_v1_4.tcl`，见 [component.xml:1279-1287](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1279-L1287) |
  | `xilinx_utilityxitfiles_view_fileset` | Logo 等 | `doc/psi_logo_150.gif`（标记为 `LOGO`），见 [component.xml:1288-1294](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1288-L1294) |
  | `xilinx_softwaredriver_view_fileset` | 软件驱动 | `drivers/fpga_base/{data,src}/` 下的 5 个文件，见 [component.xml:1295-1317](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1295-L1317) |

  注意综合文件集里除了 `hdl/` 下自己的 3 个文件，还混入了 5 个 `../../VHDL/psi_common/hdl/*.vhd`（相对路径指向仓库外的 psi_common）——这就是上一篇强调的「`psi_common` 综合必需」在元数据层面的铁证：没有这些文件，综合视图就不完整。

- **用户参数**：见 [component.xml:1320-1375](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1320-L1375)，其中 `IMPL_LED`/`IMPL_SWITCH`/`IMPL_BLINK` 默认都为 `true`，见 [component.xml:1360-1374](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1360-L1374)。

- **支持的器件系列**：见 [component.xml:1378-1399](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1378-L1399)，列出了 artix7、kintex7、zynq、virtexuplus 等一长串 7 系/UltraScale+ 系列。

- **打包工具版本**：见 [component.xml:1416](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1416)——`<xilinx:xilinxVersion>2018.2</xilinx:xilinxVersion>`，说明这份 `component.xml` 最初是用 Vivado 2018.2 打包生成的。

#### 4.3.4 代码实践

**实践目标**：验证「`scripts/package.tcl` 的源码清单」与「`component.xml` 的文件集」是一一对应的，亲手确认元数据是「生成」出来的。

**操作步骤**：

1. 打开打包脚本的源码登记段 [scripts/package.tcl:24-28](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L24-L28)，记下 `add_sources_relative` 列出的三个 HDL 文件名。
2. 打开 `component.xml` 的综合文件集 [component.xml:1192-1235](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1192-L1235)。
3. 在文件集里找这三个名字，确认它们都作为 `<spirit:file>` 出现了。

**需要观察的现象**：`package.tcl` 里的 `hdl/fpga_base_date_package.vhd`、`hdl/fpga_base_v1_0.vhd`、`hdl/fpga_base_scripted_info_pkg.vhd` 三个名字，在 `component.xml` 的综合文件集里能一一找到对应条目（分别见 [component.xml:1194-1198](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1194-L1198)、[component.xml:1229-1234](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1229-L1234)、[component.xml:1199-1203](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L1199-L1203)）。

**预期结果**：你会清楚地看到——**手写的 `package.tcl` 是「输入」，生成的 `component.xml` 是「输出」**，二者通过文件名严格对应。这印证了 4.2 节的结论：`component.xml` 是产物，但被入库以便分发。

**待本地验证**：如果本地装了 Vivado + PsiIpPackage，运行 `scripts/package.tcl` 后用 `git diff component.xml` 观察哪些字段变化（通常只有校验和 `viewChecksum`、`coreRevision` 等自动字段变）。

#### 4.3.5 小练习与答案

**练习 1**：从 `component.xml` 看，这个 IP 的寄存器空间有多大？能放几个 32 位寄存器？

> **答案**：`range=256` 字节、`width=32` 位（见 [component.xml:353-354](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L353-L354)）。256 ÷ 4 = **64 个 32 位寄存器**。这正好对应后续单元会讲的 `NumReg_g=64`。

**练习 2**：如果使用者把 `IMPL_LED` 参数设成 `false`，会发生什么？依据是 `component.xml` 的哪一处？

> **答案**：`o_led` 端口会被「裁掉」、不实现。依据是 [component.xml:459](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml#L459) 的使能条件 `xilinx:dependency="$IMPL_LED"`——当该参数为假时，端口使能条件不满足，Vivado 在生成顶层 wrapper 时不会引出 `o_led`。

**练习 3**：为什么综合文件集里会出现 `../../VHDL/psi_common/hdl/*.vhd` 这种指向仓库**外**的相对路径？

> **答案**：因为本 IP 综合时依赖 `psi_common` 库（上一篇讲过的「综合必需」依赖）。打包时 PsiIpPackage 通过 `add_lib_relative`（见 [scripts/package.tcl:31-39](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl#L31-L39)）把 psi_common 的若干 `.vhd` 也登记进文件集，用相对路径 `../../VHDL/psi_common/hdl/` 指向同级检出位置。这是 PSI 多仓库工作区的约定布局。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「仓库导游」小任务：

1. **画一张完整的仓库结构图**：用一棵目录树（参考 4.1.1 的示例），把仓库里**每一个被 Git 跟踪的文件**都标出来，并在每个文件后面用括号注明它的角色（电路本体 / IP 元数据 / 打包与构建 / 软件侧 / 文档与门面）。可以用 `git ls-files` 获取完整清单。

2. **标注「手写 / 生成 / 忽略」三态**：用三种颜色或记号区分：
   - 手写源文件（如 `hdl/*.vhd`、`drivers/fpga_base/src/fpga_base.c`）。
   - 入库的生成文件（如 `component.xml`、`xgui/fpga_base_v1_4.tcl`）。
   - 被 `.gitignore` 忽略的产物（如 `/*.zip`、`scripts/package_prj`、`xgui/fpga_base_v1_0_v1_0.tcl`）。

3. **回答一个连接性问题**：如果有人删掉了 `component.xml`，仓库还能不能被 Vivado 当作 IP 直接使用？如果删掉的是 `scripts/package.tcl` 呢？分别影响什么？

**参考结论**：

- 删掉 `component.xml`：Vivado 无法直接识别这个目录为 IP（没有元数据入口），**需要重新运行 `scripts/package.tcl` 打包**才能再生成它。但因为 `hdl/` 源码还在，电路本身没丢，可恢复。
- 删掉 `scripts/package.tcl`：已经生成的 `component.xml` 仍在，IP **仍可直接使用**；但你**失去了重新打包/升级 IP 的能力**，今后改了 `hdl/` 也无法方便地刷新 `component.xml`。所以二者一个是「分发态」、一个是「构建态」，缺一不可，但缺失造成的影响不同。

这个练习把「目录职责 → 手写/生成区分 → IP-XACT 元数据的作用」连成了一条完整的理解链。

## 6. 本讲小结

- 仓库按**用途分目录**：`hdl/` 是电路本体，`scripts/`/`bd/`/`gui/`/`xgui/` 和根目录两个 `.tcl` 是打包与构建脚本，`drivers/`/`epics/` 是软件侧接口，`doc/`/`README`/`Changelog`/`License` 是文档与门面。
- 文件分**手写与生成**两类：手写文件是真相之源（`hdl/`、驱动源码、`package.tcl`）；生成文件是产物。`.gitignore` 帮我们区分「用完即扔的中间产物」（zip、`package_prj`、重复的 xgui、`.pyc`）和「入库便于分发的产物」（`component.xml`）。
- **`component.xml` 是 IP-XACT 元数据**，登记 IP 的坐标（`psi.ch/PSI/fpga_base/1.4`）、端口（含 `IMPL_*` 可选使能）、地址映射（256 字节 = 64 个 32 位寄存器）、文件集（综合/仿真/xgui/logo/驱动五个）、支持的器件系列。它是 `scripts/package.tcl` 跑出来的产物。
- PSI 采用 **「hand-packaged IP」工作流**：把生成产物 `component.xml` 入库以便直接分发，同时用 `.gitignore` 排除真正的临时构建产物。
- 理解「`package.tcl`（输入） ↔ `component.xml`（输出）」的镜像关系，是把握整个仓库工程化的关键。
- 本讲只读了结构与配置，没有深入任何 VHDL/电路逻辑；下一篇起进入打包流水线，之后才读 HDL。

## 7. 下一步学习建议

- **接下来读**：[u1-l3 构建与打包流程总览](u1-l3-build-and-packaging-overview.md)——从高层俯瞰「VHDL 源码 → 可分发 IP zip 包」的完整流水线，深入 `scripts/package.tcl` 和 `scripts/dependencies.py` 的协作。
- **再之后（第二单元）**：进入 [u2-l1 顶层实体：端口、泛型与外部接口](u2-l1-top-entity-ports-generics.md)，正式开始读 `hdl/fpga_base_v1_0.vhd` 的 VHDL 电路逻辑。
- **想现在就摸 IP-XACT**：通读 [component.xml](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/component.xml)，重点对照本讲 4.3 列出的字段，把「端口 → 使能条件」「文件集 → 视图」两套对应关系在源文件里走一遍。
- **想理解工具链**：精读 [scripts/package.tcl](https://github.com/paulscherrerinstitute/vivadoIP_fpga_base/blob/9b249a7a48c9f50f411c936da435eb5438aeb097/scripts/package.tcl) 全文，它是 PsiIpPackage 这套 PSI 打包框架的具体用例。
