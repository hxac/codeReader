# 仓库目录结构与外部依赖

## 1. 本讲目标

学完本讲后，你应当能够：

- 看着 `git ls-files` 的输出，**说清楚仓库里每一个顶层目录各自承担什么职责**。
- 说出本 IP 核依赖的四个外部库（`psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage`）分别是什么、什么时候才会用到。
- 理解 PSI FPGA 库的**文件夹结构约定**，并能解释为什么本仓库必须放在 `<根目录>/VivadoIp/vivadoIP_data_rec/` 这样的相对路径下。
- 知道如何用 `scripts/dependencies.py` 自动拉取依赖。

本讲是「认识项目」这一单元的第二篇，承接上一讲对 IP 功能的认识，**把目光从「它能做什么」转到「代码放在哪、依赖谁、怎么组织起来」**，为后面阅读真实 VHDL 源码打好导航基础。

## 2. 前置知识

阅读本讲前，你需要具备以下基础（不熟悉的术语下面会解释）：

- **IP 核（IP Core）**：可复用的硬件功能模块，类似软件里的「库」。本项目的 `data_rec` 就是一个可以塞进 Xilinx FPGA 工程的数据记录 IP。
- **VHDL**：一种硬件描述语言，本项目的 RTL 源码全部用 VHDL-2008 编写。
- **AXI 总线**：Xilinx/ARM 定义的一种片上总线协议，软件（如 Zynq 的 ARM 核）通过它读写 IP 内部的寄存器与存储。本讲只需知道「AXI 是软件访问 IP 的通道」即可，细节后面讲。
- **IP-XACT / component.xml**：一种 XML 标准，用来描述一个 IP 的端口、参数、总线接口和包含哪些源文件。Vivado 靠它识别 IP。
- **Vivado**：Xilinx 的 FPGA 开发工具；**Modelsim/Questa**：常用的 VHDL 仿真器。
- **EPICS**：实验物理领域常用的控制系统，本项目提供了把 IP 接入 EPICS 的代码生成器（非必需，仅集成时使用）。

如果你已经学完上一讲《项目总览》，知道这是一个「像示波器一样抓波形」的多通道数据记录器，那就足够了。

## 3. 本讲源码地图

本讲涉及的「源码」主要是项目自身的组织文件，而不是某段算法实现：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目入口，包含**依赖清单**与**文件夹结构约定**（被脚本解析） |
| `component.xml` | IP-XACT 描述：IP 名称/版本、端口、参数、总线接口、源文件清单（含对 `psi_common` 的相对引用） |
| `scripts/dependencies.py` | 依赖解析脚本，从 README 抽取依赖并自动 checkout |
| `scripts/package.tcl` | IP 打包脚本，声明源文件与 GUI 参数（同样引用外部库路径） |
| `sim/config.tcl` | 仿真配置，组织源码、`psi_common`、`psi_tb` 与测试平台 |
| `Changelog.md` | 版本变更记录（可佐证依赖脚本、AXI slave 来源等历史） |

> 阅读建议：本讲会反复对比 `component.xml`、`scripts/package.tcl`、`sim/config.tcl` 三者里出现的相对路径，它们共同印证了同一套文件夹约定——这是本讲最有价值的一个观察。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **仓库顶层目录结构与各目录职责**——先把「家」认全。
2. **PSI FPGA 库依赖与文件夹结构约定**——理解项目「住」在哪里、邻居是谁。
3. **依赖解析脚本 `dependencies.py`**——理解依赖如何被自动拉取。

### 4.1 仓库顶层目录结构

#### 4.1.1 概念说明

一个 FPGA IP 仓库通常不止「RTL 源码」一种文件，它还包含：仿真脚本、测试平台、IP 打包脚本、GUI 描述、文档、控制系统集成代码等。PSI 的做法是**按职能把文件分目录存放**，让每种角色都有固定的位置。这样无论是人还是脚本，都能按目录名快速定位。

#### 4.1.2 仓库目录树（依据 `git ls-files`）

下面这棵树是依据仓库实际被 git 跟踪的文件整理出来的（去掉了 `.gitignore` 等纯配置文件的内容，只标注关键文件）：

```
vivadoIP_data_rec/
├── README.md                # 项目入口：依赖清单 + 文件夹约定（被脚本解析）
├── Changelog.md             # 版本变更记录
├── License.txt / LGPL2_1.txt# 许可证
├── component.xml            # IP-XACT：端口/参数/总线接口/源文件清单
│
├── hdl/                     # 【核心】VHDL-2008 源码
│   ├── data_rec.vhd             # 记录器核心：状态机、数据通路、计数器
│   ├── data_rec_register_pkg.vhd# 寄存器地址地图（地址常量、字段位）
│   └── data_rec_vivado_wrp.vhd  # Vivado 封装层：AXI 解码、跨时钟域、存储
│
├── testbench/top_tb/        # 【验证】仿真测试平台
│   ├── top_tb.vhd               # 顶层 TB：双时钟、DUT 实例化
│   ├── top_tb_pkg.vhd           # 公共激励/校验过程
│   └── top_tb_case0..5_pkg.vhd  # 6 个测试用例
│
├── sim/                     # 【仿真流程】PsiSim/Modelsim 脚本
│   ├── run.tcl                  # 回归仿真入口（source ./run.tcl）
│   ├── config.tcl               # 源码/库/TB 组织
│   ├── interactive.tcl          # 交互式仿真
│   └── ci.do                    # CI 用的 do 文件
│
├── scripts/                 # 【构建辅助】打包、依赖、CI、重构
│   ├── package.tcl              # 把工程打包成 Vivado IP
│   ├── dependencies.py          # 自动拉取外部依赖
│   ├── ciFlow.py                # CI 仿真结果判定
│   └── refactor/                # 代码重构辅助脚本（历史遗留工具）
│
├── xgui/                    # 【IP GUI】Vivado 里定制 IP 时的参数页面
│   └── data_rec_v2_4.tcl        # 参数页面布局（v2.4）
│
├── bd/                      # 【Block Design】生成参考 Block Design 的脚本
│   └── bd.tcl
│
├── epics/                   # 【EPICS 集成】控制系统接入（可选）
│   ├── GenerateDataRecTemplates.py  # 生成 EPICS db 模板
│   ├── GenerateDataRecPanel.py      # 生成控制面板 .ui
│   ├── test.bat / README.txt
│   ├── TemplateInput/CONTROL.tpl    # db 模板源
│   └── PanelInput/*.tpl             # 面板模板源
│
└── doc/                     # 【文档】数据手册与图示源文件
    ├── data_rec.pdf             # 权威数据手册（PDF）
    ├── data_rec.docx            # 手册 Word 源
    ├── data_rec.vsd             # 框图 Visio 源
    └── psi_logo_150.gif         # IP logo
```

一条记忆口诀：**`hdl` 是心脏、`testbench` + `sim` 是体检、`scripts` + `xgui` + `component.xml` 是出厂包装、`epics` 是对外接口、`doc` 是说明书**。

#### 4.1.3 源码精读：从 `component.xml` 看 IP 的「身份证」

`component.xml` 是 IP 的元数据，里面的前几行就回答了「这是谁」：

[component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L3-L6) —— 声明 IP 的 vendor（`psi.ch`）、library（`GPAC3`）、name（`data_rec`）、version（`2.4`）。这就是 Vivado 里 IP 的全名。

真正能体现「目录职责」的是它的 **fileset（文件集）** 部分。下面这段是综合（synthesis）视图用到的源文件清单，注意其中两类文件来源不同：

[component.xml:1499-1539](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1499-L1539) —— 这里同时列出了本仓库自己的 `hdl/data_rec.vhd`、`hdl/data_rec_vivado_wrp.vhd`，以及外部库的 `../../VHDL/psi_common/hdl/psi_common_tdp_ram.vhd` 等。**这些以 `../../VHDL/psi_common/...` 开头的相对路径，正是「外部依赖」存在的直接证据**，下一节会重点讲。

另外，`doc/` 目录的职责也能在文件集里得到印证：

[component.xml:1620-1626](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1620-L1626) —— `xilinx_datasheet_view_fileset` 里把 `doc/data_rec.pdf` 注册为 IP 的数据手册，这就是上一讲强调「PDF 是权威文档」的来源。

#### 4.1.4 代码实践：画一张你自己的目录职责表

**实践目标**：通过亲手整理，把目录树和职责固化成你的「地图」。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`（只看被跟踪的文件，避免被生成物干扰）：

   ```bash
   git ls-files | sort
   ```

2. 用 `git ls-files | cut -d/ -f1 | sort -u` 只看顶层目录名，确认你看到了 `bd doc epics hdl scripts sim testbench xgui` 这几个。
3. 对照本讲 4.1.2 的目录树，为每个顶层目录写一句话职责。

**需要观察的现象**：

- `git ls-files` 的输出里，`hdl/` 下**只有 3 个 `.vhd` 文件**——整个 IP 的核心 RTL 极其精简。
- `testbench/top_tb/` 下有 1 个 `top_tb.vhd`、1 个 `top_tb_pkg.vhd`，外加 `top_tb_case0_pkg.vhd` 到 `top_tb_case5_pkg.vhd` 共 **6 个用例包**——后面验证单元会逐个拆解。
- `scripts/refactor/` 下的几个文件（`hdlrefactor.py` 等）是历史上用来改名的工具，日常学习可先忽略。

**预期结果**：你能不看讲义，对着空目录树补全每个目录的职责说明。

#### 4.1.5 小练习与答案

**练习 1**：如果只允许你看一个目录来理解「这个 IP 到底实现了什么功能」，你会选哪个目录？为什么？

> **参考答案**：选 `hdl/`。因为它只有三个文件：`data_rec.vhd`（核心记录逻辑）、`data_rec_register_pkg.vhd`（寄存器地图）、`data_rec_vivado_wrp.vhd`（封装）。其余目录都是围绕这三个文件的「包装/验证/文档/集成」。

**练习 2**：`xgui/data_rec_v2_4.tcl` 文件名里的 `v2_4` 代表什么？依据是什么？

> **参考答案**：代表 IP 版本 2.4。依据是 `component.xml` 第 6 行 `<spirit:version>2.4</spirit:version>` 与 `Changelog.md` 顶部的 `## 2.4`。打包脚本会按版本号生成对应的 xgui 文件名。

---

### 4.2 PSI FPGA 库依赖与文件夹结构约定

#### 4.2.1 概念说明

本项目**不把所有代码都自己写**，而是大量复用 PSI（保罗·谢勒研究所）对外开源的通用 FPGA 库。这一点非常重要——理解了依赖，你才知道打开源码时那些 `psi_common_xxx` 的实体是从哪儿来的，才不会满世界找不到定义。

README 在一段**会被脚本解析**的特殊区域里列出了依赖。注意这两行 HTML 注释标记：

[README.md:18-20](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L18-L20) —— `<!-- DO NOT CHANGE FORMAT ... -->` 到 `<!-- END OF PARSED SECTION -->` 之间的内容，正是 `dependencies.py` 解析的对象（详见 4.3 节），所以**格式不能乱改**。

#### 4.2.2 四个外部库一览

README 把依赖分成三类（TCL / VHDL / VivadoIp），对应四个外部库：

| 外部库 | 语言/形态 | 版本要求 | 用途 | 何时需要 |
| --- | --- | --- | --- | --- |
| **psi_common** | VHDL | ≥ 3.0.0 | 通用电路：AXI slave、跨时钟域、双口 RAM、流水线寄存器、数学/数组/逻辑包 | **始终需要**（综合和仿真都要） |
| **psi_tb** | VHDL | ≥ 3.0.0 | 仿真专用：文本工具、比较包、AXI 仿真包 | **仅仿真**需要 |
| **PsiSim** | TCL | ≥ 2.1.0 | 统一的仿真流程框架（`psi::sim::*` 命令） | **仅开发**需要（跑回归仿真） |
| **PsiIpPackage** | TCL | 2.1.0 | 把工程打包成 Vivado IP 的框架 | **仅开发**需要（打包发布 IP） |

一个关键区分：

- **运行/综合**该 IP，只需要 `psi_common`（它被写进了 `component.xml` 的文件集，会随 IP 一起被 Vivado 综合）。
- **仿真验证**还需要 `psi_tb` 和 `PsiSim`。
- **打包发布**还需要 `PsiIpPackage`。
- 把 IP 接入 EPICS 控制系统，则用 `epics/` 下的生成器（不依赖上面任何库）。

#### 4.2.3 哪些 `psi_common` 文件被实际用到了？

`component.xml` 的文件集和 `scripts/package.tcl` 都明确列出了被引用的 `psi_common` 文件。以打包脚本为例：

[scripts/package.tcl:39-51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L39-L51) —— 列出了 9 个 `psi_common` 文件：`math_pkg`、`array_pkg`、`logic_pkg`（三类工具包）、`pulse_cc`、`status_cc`、`simple_cc`（三种跨时钟域）、`tdp_ram`（双口 RAM）、`pl_stage`（流水线寄存器）、`axi_slave_ipif`（AXI Slave 接口）。

这些名字本身就是「剧透」：当你后面读 `data_rec_vivado_wrp.vhd` 时，会看到封装层实例化了 `psi_common_axi_slave_ipif`、`psi_common_status_cc`、`psi_common_pulse_cc`、`psi_common_tdp_ram` 等——它们就是从这里来的。

仿真侧还会用到 `psi_tb`：

[sim/config.tcl:31-35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L31-L35) —— 引用了 `psi_tb_txt_util`、`psi_tb_compare_pkg`、`psi_tb_axi_pkg` 三个仿真辅助包。

#### 4.2.4 文件夹结构约定：三处相对路径指向同一个根

这是本讲**最值得记住的一点**。README 明确要求文件夹名必须精确匹配：

[README.md:22-24](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L22-L24) —— 「The required folder structure looks as given below (folder names must be matched exactly).」，并提到可以用 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all) 仓库一次性获得正确结构（它以 submodule 形式包含了所有 FPGA 相关仓库）。

那么这套「正确的文件夹结构」到底长什么样？**最强有力的证据来自三个不同文件里的相对路径，它们不约而同地指向同一个根目录**：

| 出处（文件:行） | 相对路径 | 解析（从该文件所在目录出发） |
| --- | --- | --- |
| [component.xml:1529-1533](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1529-L1533) | `../../VHDL/psi_common/hdl/psi_common_tdp_ram.vhd` | 仓库根 `..` → `VivadoIp`，再 `..` → **根目录**，进入 `VHDL/psi_common/...` |
| [scripts/package.tcl:10](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L10) 与 [scripts/package.tcl:39-40](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L39-L40) | `../../../TCL/PsiIpPackage/...`、`../../../VHDL/psi_common/hdl` | `scripts` `..` → 仓库根，`..` → `VivadoIp`，`..` → **根目录** |
| [sim/config.tcl:8](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L8) 配合 [sim/config.tcl:18](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L18)、[sim/config.tcl:31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/sim/config.tcl#L31) | `set LibPath "../../.."` 后拼 `$LibPath/VHDL/psi_common/hdl`、`$LibPath/VHDL/psi_tb/hdl` | `sim` `..` → 仓库根，`..` → `VivadoIp`，`..` → **根目录** |

把这三条相对路径的「上跳层级」对齐（仓库根在 `VivadoIp` 下一级，所以从仓库根的文件要上跳 2 层、从 `scripts`/`sim` 这种子目录的文件要上跳 3 层），它们最终都落在同一个**根目录**上。由此反推出约定的目录结构：

```
<根目录>/
├── TCL/
│   ├── PsiSim/                 # TCL 仿真框架
│   └── PsiIpPackage/           # TCL 打包框架
├── VHDL/
│   ├── psi_common/hdl/         # 通用 VHDL 库
│   └── psi_tb/hdl/             # 仿真 VHDL 库
└── VivadoIp/
    └── vivadoIP_data_rec/      # ★ 本仓库就放在这里
```

也就是说：**使用本仓库时，必须把它 checkout 到 `<根目录>/VivadoIp/vivadoIP_data_rec/`，并在同级摆好 `VHDL/` 和 `TCL/`**，否则上述所有相对路径都会失效、综合与仿真都会找不到 `psi_common`。

> 佐证：`Changelog.md` 中 v1.1.2 提到「Changed packaging script to work independently of the folder structure above the library folder」，说明历史上打包脚本曾依赖上层目录结构，后经调整；当前版本仍要求本仓库位于 `VivadoIp/` 之下。

#### 4.2.5 代码实践：用 `psi_fpga_all` 一键获得正确结构

**实践目标**：用官方聚合仓库省去手动摆放目录的麻烦。

**操作步骤**：

1. 阅读 README 提到的 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all) 仓库说明，确认它「contains all FPGA related repositories as submodules in the correct folder structure」。
2. 克隆并初始化子模块（示例命令，**待本地验证**网络可达性）：

   ```bash
   git clone --recurse-submodules https://github.com/paulscherrerinstitute/psi_fpga_all.git
   ```

3. 进入 `psi_fpga_all/VivadoIp/vivadoIP_data_rec/`，确认它旁边确实有 `../../VHDL/psi_common/hdl/psi_common_tdp_ram.vhd`。

**需要观察的现象**：

- 不用手动创建任何目录，`psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage` 都已经在正确的相对位置。
- 在该位置下，`component.xml` 里 `../../VHDL/...` 这类路径能被 Vivado 正确解析。

**预期结果**：你得到一个开箱即用、目录结构完全合规的工作区。如果网络受限无法克隆，可改为手工按 4.2.4 的目录树摆放四个依赖库。

#### 4.2.6 小练习与答案

**练习 1**：为什么 `component.xml` 里写的是 `../../VHDL/...`（两层），而 `scripts/package.tcl` 里写的是 `../../../VHDL/...`（三层）？

> **参考答案**：因为两个文件的**深度不同**。`component.xml` 在仓库根（`vivadoIP_data_rec/component.xml`），上跳两层到达根目录；`package.tcl` 在仓库根下的 `scripts/` 子目录（`vivadoIP_data_rec/scripts/package.tcl`），比 `component.xml` 深一层，所以要多跳一层、共三层才能到达同一个根目录。

**练习 2**：如果你只想在 Vivado 里综合使用这个 IP、完全不跑仿真也不打包，你**必须**准备哪几个外部库？

> **参考答案**：只需 `psi_common`（≥3.0.0）。因为它是唯一被写进 `component.xml` 综合文件集的外部 VHDL 库，会随 IP 一起被 Vivado 综合；`psi_tb`/`PsiSim` 仅仿真用，`PsiIpPackage` 仅打包用，都可以不要。

---

### 4.3 依赖解析脚本 `scripts/dependencies.py`

#### 4.3.1 概念说明

手动按目录树摆放四个库很繁琐，也容易拉错版本。PSI 提供了一个统一的 Python 工具——只要在每个仓库的 `README.md` 里按规定格式写好依赖清单，`dependencies.py` 就能**自动解析并 checkout** 对应版本的依赖。本项目自 v2.2.0 起引入该脚本（见 `Changelog.md` 第 18-19 行）。

#### 4.3.2 核心流程

脚本本身只有十几行，逻辑非常清晰：

1. 定位自己所在目录（`scripts/`）。
2. 通过相对路径找到上一层的 `README.md`。
3. 调用 `PsiFpgaLibDependencies` 库的 `Parse.FromReadme()`，从 README 里那段被 `<!-- ... -->` 包裹的区域解析出依赖清单。
4. 调用 `Actions.ExecMain()`，根据解析结果在本仓库旁边的正确位置 checkout 依赖。

#### 4.3.3 源码精读

整个脚本的「业务部分」只有三句：

[scripts/dependencies.py:7](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/dependencies.py#L7) —— 导入 `PsiFpgaLibDependencies` 工具库的全部符号（`Parse`、`Actions` 都来自这里）。注意 README 第 43 行强调：必须先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 这个 Python 包，脚本才能运行。

[scripts/dependencies.py:11-14](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/dependencies.py#L11-L14) —— 关键三步：

- `THIS_DIR` = 脚本自身所在目录（即 `scripts/`）；
- `Parse.FromReadme(THIS_DIR + "/../README.md")`：解析**上一层的 README**，得到 `dependencies`；
- `repo = os.path.abspath(THIS_DIR + "/..")`：把「本仓库根目录」作为工作目录传给执行器。

[scripts/dependencies.py:16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/dependencies.py#L16) —— `Actions.ExecMain(repo, dependencies)` 真正执行拉取，依赖会被放到与 4.2.4 完全一致的目录结构里。

它解析的「数据源」就是 README 里这段被注释包裹的清单：

[README.md:26-35](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L26-L35) —— 列出 TCL（PsiSim、PsiIpPackage）、VHDL（psi_common、psi_tb）、VivadoIp（本仓库）三类依赖与版本号。脚本据此知道要拉哪些库、什么版本。

README 还给出了用法说明：

[README.md:37-43](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/README.md#L37-L43) —— 用 `python dependencies.py -help` 查看详细参数；并提示必须先安装 `PsiFpgaLibDependencies` 包。

#### 4.3.4 代码实践：查看脚本帮助并理解解析链路

**实践目标**：在不实际拉取依赖的前提下，验证「README → 脚本 → 依赖」这条链路。

**操作步骤**：

1. 确认已安装 Python 与 `PsiFpgaLibDependencies` 包（若未装，跳到步骤 3 的源码阅读型实践）：

   ```bash
   python scripts/dependencies.py -help
   ```

2. 阅读输出，找到它支持的子命令（通常包含 checkout/list 之类）。
3. **源码阅读型验证**：打开 `README.md`，定位第 18 行 `<!-- DO NOT CHANGE FORMAT ... -->` 与第 35 行 `<!-- END OF PARSED SECTION -->`。然后对照 `dependencies.py` 第 13 行的 `Parse.FromReadme(...)`，确认脚本读取的正是这段区间。

**需要观察的现象**：

- README 依赖清单的两端有明确的 HTML 注释标记，且顶部写有「DO NOT CHANGE FORMAT」——因为机器要按格式解析。
- 脚本里没有任何硬编码的库名或版本号，**唯一的真相源是 README**；所以升级依赖版本只需改 README、不必改脚本。

**预期结果**：你能向别人讲清楚「改 README 的依赖区 → 重新跑 `dependencies.py` → 依赖被自动更新到正确位置与版本」这条链路。若未安装依赖包无法运行命令，请明确记录「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `psi_common` 的版本要求从 `3.0.0` 改成 `3.2.0`，需要修改 `dependencies.py` 吗？

> **参考答案**：不需要。脚本里没有硬编码版本号，版本信息全部来自 README 第 26-35 行的依赖清单。只需改 README，再重跑脚本即可。

**练习 2**：`dependencies.py` 第 13 行用 `THIS_DIR + "/../README.md"` 拼路径，为什么不直接写 `"README.md"`？

> **参考答案**：因为脚本被设计成**从任意当前工作目录**都能正确运行。`THIS_DIR` 是脚本文件自身的绝对路径（`scripts/`），拼上 `/../README.md` 永远指向本仓库根的 README；而裸 `"README.md"` 依赖调用者当前所在目录，容易出错。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**给新同事画一张入职地图**」的小任务：

1. **目录认知**：在仓库根执行 `git ls-files | sort`，把输出整理成一棵树，并为 `hdl`、`testbench`、`sim`、`scripts`、`xgui`、`bd`、`epics`、`doc` 每个目录写一句中文职责（参考 4.1.2）。
2. **依赖盘点**：打开 `component.xml` 的综合文件集（[component.xml:1479-1539](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/component.xml#L1479-L1539)），数出其中来自 `psi_common` 的文件有几个、分别叫什么；再与 [scripts/package.tcl:39-51](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/scripts/package.tcl#L39-L51) 对照，确认两边一致。
3. **目录摆放**：根据 4.2.4，写出本仓库在文件系统中的**完整相对路径**（`<根目录>/VivadoIp/vivadoIP_data_rec/`），并用 `component.xml` 里 `../../VHDL/psi_common/...` 这条路径验证：从仓库根上跳两层后，`VHDL/psi_common/` 确实位于同一个根目录下。
4. **自动化**：说明若要让 `scripts/dependencies.py` 正常工作，需要先安装哪个 Python 包、它读取的「真相源」是 README 的哪一段。

**交付物**：一张「目录树 + 职责 + 依赖 + 摆放位置」的说明图（手绘或文本均可）。完成后，你就拥有了一份能直接交给新同事的「项目导览图」。

## 6. 本讲小结

- 仓库按职能分目录：`hdl`（核心 RTL，仅 3 个文件）、`testbench`（仿真）、`sim`（仿真流程）、`scripts`（打包/依赖/CI）、`xgui`（IP 参数页面）、`bd`（Block Design）、`epics`（控制系统集成）、`doc`（数据手册）。
- `component.xml` 是 IP 的「身份证」与文件清单，**最权威地暴露了外部依赖**：里面直接列出了多个 `../../VHDL/psi_common/...` 文件。
- 四个外部库中，`psi_common` 是综合/运行必需，`psi_tb` 与 `PsiSim` 是仿真必需，`PsiIpPackage` 是打包必需。
- 关键约定：本仓库必须放在 `<根目录>/VivadoIp/vivadoIP_data_rec/`，`component.xml`、`package.tcl`、`config.tcl` 三处的相对路径（分别上跳 2/3/3 层）共同印证了这一点；或直接用 `psi_fpga_all` 获得合规结构。
- `scripts/dependencies.py` 通过解析 README 中被 HTML 注释包裹的依赖清单来自动拉取依赖；**README 是依赖的唯一真相源**，脚本本身不含硬编码版本号。
- 口诀：**README 是入口（也是依赖真相源），PDF 是权威文档，源码是事实，目录结构是约定**。

## 7. 下一步学习建议

- 下一篇 **u1-l3《在仿真中跑起来：PsiSim/Modelsim 回归测试》** 会带你实际执行 `sim/run.tcl`，届时你会真正用到本讲提到的 `PsiSim` 框架与 `psi_tb` 库，建议紧接着学习。
- 如果你想先了解「IP 怎么被打包发布」，可以跳到 **u1-l4《IP 打包与在 Vivado 中使用》**，那里会详解 `scripts/package.tcl` 与 `xgui/`、`component.xml` 的配合。
- 在进入第二单元阅读 `hdl/` 真实 VHDL 之前，建议先把本讲的目录地图记牢——后面所有讲义引用的文件路径，都建立在这张地图之上。
