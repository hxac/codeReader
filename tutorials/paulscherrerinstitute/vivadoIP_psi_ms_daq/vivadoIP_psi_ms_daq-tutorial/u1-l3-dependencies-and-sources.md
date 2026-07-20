# 依赖关系与获取全部源码

## 1. 本讲目标

本仓库 `vivadoIP_psi_ms_daq` 本身并不自带全部源码——真正的功能 VHDL 在上游 `psi_multi_stream_daq`，基础库在 `psi_common`。学完本讲后，读者应该能够：

- 看懂 README 中「Dependencies」段为什么用两行 HTML 注释标记包裹，以及它如何被脚本自动解析。
- 区分三类依赖：**运行时依赖**（被综合进 IP 的 VHDL）、**开发期依赖**（仿真/打包工具）、**参考设计专用依赖**。
- 读懂 [scripts/dependencies.py](../scripts/dependencies.py) 这 15 行「薄脚本」如何调用外部包 `PsiFpgaLibDependencies` 完成依赖拉取。
- 读懂 [scripts/package.tcl](../scripts/package.tcl) 中 `add_sources_relative` 与 `add_lib_relative` 的差别，并能判断 `add_lib_relative` 列表里的每个文件属于哪个上游仓库、是否运行时依赖。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-project-overview.md) 和 [u1-l2](u1-l2-repository-structure.md) 已经建立的两条认知：

- 本仓库只是一个 **Vivado IP 封装层（wrapper）**，真正的功能逻辑在上游仓库 `psi_multi_stream_daq`，通用基础库在 `psi_common`。
- 本地唯一的 RTL 是 `hdl/psi_ms_daq_vivado.vhd`，它例化上游 `entity psi_ms_daq_axi`，自身不含任何采集算法。

在此基础上，本讲需要补充三个概念：

- **依赖（dependency）**：一个项目编译或运行时需要的其他项目。本仓库自己不带 `psi_common`、`psi_multi_stream_daq` 的源码，必须从外部获取。
- **PSI FPGA Library（PsiFpgaLib）**：PSI 维护的一整套互相依赖的 VHDL/IP 仓库集合。所有仓库按统一目录布局摆放，顶层有 `VHDL/`、`TCL/`、`VivadoIp/` 等目录。本仓库就摆放在 `VivadoIp/vivadoIP_psi_ms_daq/` 下。
- **统一布局的意义**：因为布局统一，打包脚本里写死的相对路径（如 `../../../VHDL/psi_common/...`）才能在每台机器上都找到文件。`dependencies.py` 的职责就是把依赖克隆到这些「约定位置」。

> 术语提示：下文反复出现「上游」「wrapper」「综合（synthesis）」。上游指被本仓库依赖的外部仓库；wrapper 指只做接口包装不含逻辑的封装层；综合指 Vivado 把 VHDL 翻译成网表、最终烧进 FPGA 的过程。只有被「综合进 IP」的文件才会出现在用户最终拿到的 IP-Core 里。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读重点 |
| --- | --- | --- |
| [README.md](../README.md) | 依赖清单的「唯一事实来源」 | 「Dependencies」段被两行 HTML 注释包裹，供脚本解析 |
| [scripts/dependencies.py](../scripts/dependencies.py) | 拉取依赖的入口脚本（15 行） | 调用外部包 `PsiFpgaLibDependencies` 解析 README 并执行克隆 |
| [scripts/package.tcl](../scripts/package.tcl) | IP 打包脚本 | `add_sources_relative` 与 `add_lib_relative` 两个命令分别声明本地源码与外部库文件 |

## 4. 核心概念与源码讲解

### 4.1 README Dependencies 段：被自动解析的依赖清单

#### 4.1.1 概念说明

依赖清单既要给人看，也要给脚本读。本仓库的做法是：在 README 里用一段人类可读的 Markdown 列出所有依赖，同时用两行 HTML 注释把这段「圈起来」，作为脚本解析的边界标记。这样做的好处是依赖只有**一个事实来源（single source of truth）**——改依赖只改 README，脚本和打包流程都从它读取，不会出现「README 说一套、脚本做另一套」的漂移。

README 第 29 行的注释甚至直接写明意图：「DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies」（不要改格式：这一段会被解析用来解析依赖）。

#### 4.1.2 核心流程

依赖清单的解析流程：

1. README 写入起始标记 `<!-- DO NOT CHANGE FORMAT ... -->`。
2. 中间是依赖清单，按 **TCL / VHDL / VivadoIp** 三组组织。
3. 写入结束标记 `<!-- END OF PARSED SECTION -->`。
4. 脚本只截取两个标记之间的内容进行解析。

每条依赖包含四个要素：

- **名字**（如 `psi_common`）
- **GitHub 链接**
- **版本要求**（如 `2.5.0 or higher`）
- **用途标签**（如 `for development only`，可省略，省略即表示运行时必需）

#### 4.1.3 源码精读

先看边界标记与三组依赖的完整原文：

[README.md:29-44](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L29-L44) —— 第 29 行是起始标记，第 44 行是结束标记，中间是被解析的依赖清单。注意第 29 行那句英文明确警告「这一段会被脚本解析」，所以格式不能随意改动（增减空格、改列表符号都可能让解析失败）。

把这三组依赖按「是否运行时必需」归类，得到下表：

| 组 | 依赖 | 版本要求 | 用途标签 | 是否运行时必需 |
| --- | --- | --- | --- | --- |
| TCL | PsiSim | ≥ 2.1.0 | for development only | 否（仿真框架，仅开发期） |
| TCL | PsiIpPackage | 2.0.0 | for development only | 否（IP 打包工具，仅开发期） |
| VHDL | psi_common | ≥ 2.5.0 | （无标签） | **是（基础库，综合进 IP）** |
| VHDL | psi_tb | ≥ 2.2.2 | for development only | 否（testbench 库，仅开发期） |
| VHDL | psi_multi_stream_daq | ≥ 1.2.0 | （无标签） | **是（功能实现，综合进 IP）** |
| VivadoIp | vivadoIP_axis_data_gen | 1.2.0 | for reference design only | 否（仅参考设计用） |
| VivadoIp | vivadoIP_psi_ms_daq | — | （本仓库自己） | — |

**关键结论**：真正「运行时必需」的只有两个 VHDL 仓库——`psi_common` 和 `psi_multi_stream_daq`。它们没有 `for ... only` 标签，意味着用户最终拿到的 IP-Core 里必须包含它们的源码。其余四个（PsiSim、PsiIpPackage、psi_tb、vivadoIP_axis_data_gen）都是开发期或参考设计专用，不会进入最终 IP。

> 版本与功能的关系：根据 [Changelog.md](../Changelog.md)，1.1.0 版本「Added dependency resolution script」（新增依赖解析脚本），也就是说 `dependencies.py` 这套机制是从 1.1.0 才引入的；而当前 1.2.1 只是「Built IP with 1.2.1 version of the *psi_multi_stream_daq* VHDL repository」——跟随上游重建。这正说明 `psi_multi_stream_daq` 是被持续跟踪的运行时依赖。

再看 README 对 `dependencies.py` 用法的说明：

[README.md:46-52](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L46-L52) —— 说明依赖可以用 `python dependencies.py -help` 查看，并强调必须先安装外部包 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies)。这一点是 4.2 节的关键前提。

#### 4.1.4 代码实践

**实践目标**：亲手确认「被解析区域」的边界，并分清运行时与开发期依赖。

**操作步骤**：

1. 打开 [README.md](../README.md)，定位到第 29 行和第 44 行的两行 HTML 注释。
2. 数一下两个标记之间一共列出了几条依赖（答案：含本仓库自身共 7 条；去掉本仓库自身是 6 条外部依赖）。
3. 给每条依赖标注「运行时 / 开发期 / 参考设计」三类。

**需要观察的现象**：起始标记行里包含英文「this section is parsed」，结束标记是 `<!-- END OF PARSED SECTION -->`；只有 `psi_common` 和 `psi_multi_stream_daq` 两条没有任何 `for ... only` 后缀。

**预期结果**：你会得到一张与上面「是否运行时必需」列一致的表——只有两条运行时依赖。

#### 4.1.5 小练习与答案

**练习 1**：如果把 README 第 29 行的起始标记误删了，`dependencies.py` 会怎样？

**参考答案**：脚本找不到起始边界，无法截取被解析段，依赖解析会失败或得到空列表。这正是 README 警告「DO NOT CHANGE FORMAT」的原因。

**练习 2**：为什么 `psi_tb` 虽然是 VHDL 依赖，却不算运行时依赖？

**参考答案**：因为它带有 `for development only` 标签，只用于仿真 testbench；它不会被综合进最终 IP，用户拿到 IP 时不需要它。

---

### 4.2 dependencies.py：解析 README 并拉取依赖

#### 4.2.1 概念说明

[scripts/dependencies.py](../scripts/dependencies.py) 故意写得很短——去掉注释和空行只有 5 行有效代码。真正的解析与拉取逻辑全在外部 Python 包 `PsiFpgaLibDependencies` 里。本仓库的脚本只做两件事：**指明「读哪个 README」**和**「在哪个目录执行」**。

这种「薄包装（thin wrapper）+ 外部共享包」的设计让所有 PSI 仓库共用同一套依赖管理代码，每个仓库只需提供自己的 README 和这几行模板。这也是为什么 1.1.0 引入这个脚本后，后续仓库都能复用同一套机制。

#### 4.2.2 核心流程

脚本的三步逻辑：

1. `from PsiFpgaLibDependencies import *`：导入外部包的全部公开接口（其中包含 `Parse`、`Actions` 等模块）。
2. `Parse.FromReadme(README 路径)`：读取 README，截取两个 HTML 注释标记之间的内容，解析成结构化的依赖列表 `dependencies`。
3. `Actions.ExecMain(repo, dependencies)`：在本仓库根目录 `repo` 上执行「主动作」（默认是把依赖仓库克隆到统一布局的约定位置，使 `package.tcl` 的相对路径能找到文件）。

运行前提：系统已通过 pip 等方式安装了 `PsiFpgaLibDependencies` 包。命令行用法见 README：`python dependencies.py -help`。

> 说明：`PsiFpgaLibDependencies` 是外部包，不在本仓库内。它内部「如何解析 Markdown、如何调用 git 克隆、支持哪些命令行子命令」等细节不在本讲范围内。本仓库只承诺「README 的 Dependencies 段 + 这三步调用」。如果需要了解 `ExecMain` 的具体行为，应去该外部包的仓库阅读。

#### 4.2.3 源码精读

导入外部包：

[dependencies.py:7-11](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/dependencies.py#L7-L11) —— 第 7 行 `from PsiFpgaLibDependencies import *` 是全部能力的来源；第 11 行用 `os.path` 求出脚本自身所在目录 `THIS_DIR`，用于后续拼出 README 和仓库根的绝对路径。注意它不依赖「当前工作目录」，所以无论从哪里调用脚本都能正确定位。

核心三步：

[dependencies.py:13-15](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/dependencies.py#L13-L15) —— 第 13 行 `Parse.FromReadme(...)` 读 README；第 14 行 `os.path.abspath(THIS_DIR + "/..")` 求出仓库根目录（脚本在 `scripts/` 下，`/..` 即仓库根）；第 15 行 `Actions.ExecMain(repo, dependencies)` 在仓库根上执行依赖拉取。

把第 13、14 行翻译成路径算术：

- `THIS_DIR` = `<仓库>/scripts`
- `THIS_DIR + "/../README.md"` = `<仓库>/README.md` ✓
- `THIS_DIR + "/.."` 取绝对路径 = `<仓库>` ✓

#### 4.2.4 代码实践

**实践目标**：在不实际克隆的前提下，验证脚本能否被正确加载、并查看它的命令行帮助。

**操作步骤**：

1. 先确认外部包是否已安装：`python -c "import PsiFpgaLibDependencies"`。
2. 若已安装，运行 `python scripts/dependencies.py -help` 查看可用子命令。
3. 若未安装，先 `pip install` 对应包（参见 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 仓库说明）。

**需要观察的现象**：`-help` 输出里应能看到类似「clone 依赖到约定位置」「校验版本」等子命令（具体名称以实际输出为准）。

**预期结果**：**待本地验证**——本讲无法在没有 `PsiFpgaLibDependencies` 包的环境里替你跑这条命令。如果暂时无法安装，可改为「源码阅读型实践」：通读这 15 行，确认它没有做任何 git 操作，所有 git 动作都封装在 `Actions.ExecMain` 里。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dependencies.py` 里没有任何 `git clone` 字样，却能克隆依赖？

**参考答案**：因为克隆逻辑封装在外部包 `PsiFpgaLibDependencies` 的 `Actions.ExecMain` 里。本脚本只负责「读哪个 README、在哪个目录执行」，git 操作由外部包代劳。

**练习 2**：脚本用 `THIS_DIR`（脚本自身所在目录）而不是 `os.getcwd()`（当前工作目录）来定位 README，有什么好处？

**参考答案**：这样无论用户从哪个目录调用 `python scripts/dependencies.py`，README 路径都正确；若用 `os.getcwd()`，用户必须切到仓库根才能运行，容易出错。

---

### 4.3 package.tcl 的 add_lib_relative 列表：综合进 IP 的运行时依赖

#### 4.3.1 概念说明

`dependencies.py` 解决的是「把依赖源码拉到本地」。但拉下来之后，**哪些文件真正会被综合（synthesis）进最终的 IP-Core**？这件事由打包脚本 [scripts/package.tcl](../scripts/package.tcl) 决定。

`package.tcl` 用两个命令区分「本地源码」与「外部库文件」：

- `add_sources_relative`：**本仓库自己写的**源码。本仓库只有 `hdl/psi_ms_daq_vivado.vhd` 一个。
- `add_lib_relative`：**来自上游依赖库的**源码。包含 `psi_common` 和 `psi_multi_stream_daq` 的若干 `.vhd`。

**只有出现在 `add_lib_relative` 列表里的文件才会被打包进 IP**；开发期依赖（`psi_tb`、`PsiSim`、`PsiIpPackage`）完全不出现在这里——它们是给人/工具用的，不是给 FPGA 综合用的。

#### 4.3.2 核心流程

`add_lib_relative` 接受两个参数：第一个是「基准目录」，第二个是「文件列表」。基准目录 `"../../.."` 是相对 `package.tcl` 所在的 `scripts/` 目录向上三级：

```
scripts/                         （package.tcl 所处目录）
  ../      →  vivadoIP_psi_ms_daq/   （本仓库根）
  ../../   →  VivadoIp/              （PSI 的 IP 仓库集合）
  ../../../→  PsiFpgaLib 根          （含 VHDL/、TCL/、VivadoIp/ 等顶层目录）
```

所以基准目录是 **PsiFpgaLib 根**，文件路径 `VHDL/psi_common/...` 就解析为：

\[ \text{完整路径} = \langle\text{PsiFpgaLib 根}\rangle / \text{VHDL}/\text{psi\_common}/\ldots \]

这要求所有依赖仓库按统一布局摆在 `<根>/VHDL/` 下——正是 `dependencies.py` 负责保证的布局。两者是配套的：`dependencies.py` 把仓库克隆到约定位置，`package.tcl` 用写死的相对路径去取文件。

> 同样的目录算术也出现在 `package.tcl` 第 10 行 `source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl`——它从 PsiFpgaLib 根下的 `TCL/PsiIpPackage/` 取打包工具，印证了「基准是 PsiFpgaLib 根」这一布局。

#### 4.3.3 源码精读

先看本地源码声明：

[package.tcl:32-34](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L32-L34) —— `add_sources_relative` 只列了 `../hdl/psi_ms_daq_vivado.vhd` 这一个文件，即本仓库唯一的 wrapper RTL（见 [u1-l2](u1-l2-repository-structure.md)）。注意它的基准目录是默认的（脚本所在目录的上一级，即仓库根），所以路径写成 `../hdl/...`。

再看外部库文件声明（本讲的核心）：

[package.tcl:37-64](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L37-L64) —— 第 38 行第一个参数 `"../../.."` 是基准目录（PsiFpgaLib 根），第 40–64 行的花括号列表是要综合进 IP 的全部外部 VHDL 文件。把列表里的 24 个文件按上游仓库归类：

| 上游仓库 | 文件（均位于 `VHDL/<仓库>/hdl/` 下） | 数量 | 角色 |
| --- | --- | --- | --- |
| `psi_common` | `psi_common_array_pkg`、`psi_common_math_pkg`、`psi_common_logic_pkg`、`psi_common_sdp_ram`、`psi_common_pulse_cc`、`psi_common_bit_cc`、`psi_common_simple_cc`、`psi_common_status_cc`、`psi_common_async_fifo`、`psi_common_arb_priority`、`psi_common_sync_fifo`、`psi_common_tdp_ram`、`psi_common_axi_master_simple`、`psi_common_wconv_n2xn`、`psi_common_axi_master_full`、`psi_common_pl_stage`、`psi_common_axi_slave_ipif` | 17 | 通用积木：FIFO、时钟域穿越、RAM、仲裁器、AXI 主/从接口 |
| `psi_multi_stream_daq` | `psi_ms_daq_pkg`、`psi_ms_daq_input`、`psi_ms_daq_daq_sm`、`psi_ms_daq_daq_dma`、`psi_ms_daq_axi_if`、`psi_ms_daq_reg_axi`、`psi_ms_daq_axi` | 7 | 采集引擎本体：输入、状态机、DMA、AXI 接口、寄存器、顶层 |

**这 24 个文件全部是运行时依赖**——它们都会被综合进最终 IP。其中 `psi_ms_daq_axi.vhd` 正是 wrapper 在 [hdl/psi_ms_daq_vivado.vhd](../hdl/psi_ms_daq_vivado.vhd) 里例化的那个 `entity psi_ms_daq_axi`（详见 [u2-l3](u2-l3-instantiating-impl.md)）。

**对比：哪些依赖没有出现在这里？** `psi_tb`（testbench）、`PsiSim`（仿真框架）、`PsiIpPackage`（打包工具）都没有进入 `add_lib_relative`——因为它们是开发期/工具链依赖，不参与综合。`vivadoIP_axis_data_gen` 也不在这里——它只在参考设计里被例化，由参考设计自己的工程管理，不在本 IP 的打包范围内。

顺带一提驱动文件也是从上游拷过来的：

[package.tcl:75-76](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L75-L76) —— 这两行 `file copy -force` 在打包时把 `psi_multi_stream_daq` 仓库里的 `psi_ms_daq.c/.h` 强制拷到本仓库 `drivers/psi_ms_daq_axi/src/`。第 70–72 行的注释明确警告：「Driver files are stored with the VHDL code... The local files are overwritten automatically during packaging」（驱动文件随 VHDL 代码一起存放，本地文件在打包时会被自动覆盖）。这与 [u1-l2](u1-l2-repository-structure.md) 的结论一致：本地 `drivers/*.c/*.h` 是产物，真身在上游。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把 `add_lib_relative` 列表里的 24 个文件逐一归类到上游仓库，并回答「哪些是运行时、哪些是开发期」。

**操作步骤**：

1. 打开 [package.tcl:37-64](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L37-L64)。
2. 对列表里每个文件，看它的路径前缀是 `VHDL/psi_common/` 还是 `VHDL/psi_multi_stream_daq/`，分别归入两类。
3. 数一下两类各几个文件（预期：psi_common 17 个、psi_multi_stream_daq 7 个）。
4. 回答：列表里有没有出现 `psi_tb`、`PsiSim`、`PsiIpPackage`？为什么？

**需要观察的现象**：所有文件路径都以 `VHDL/` 开头；没有任何文件以 `TCL/` 开头；没有任何 testbench 文件（如 `*_tb.vhd`）。

**预期结果**：得到上面那张归类表。24 个文件**全部**是运行时依赖（综合进 IP）；`psi_tb`/`PsiSim`/`PsiIpPackage` 因属开发期依赖而缺席。这条结论与 4.1 节 README 的依赖标签完全吻合——README 标 `for development only` 的依赖，确实没有出现在综合清单里。

#### 4.3.5 小练习与答案

**练习 1**：`add_lib_relative` 的基准目录为什么是 `"../../.."` 而不是 `".."`？

**参考答案**：`".."` 只到仓库根，找不到 `VHDL/psi_common/...`。必须向上三级到 PsiFpgaLib 根，才能命中统一布局下的 `VHDL/` 目录。这是 PSI 全家桶统一布局的约定。

**练习 2**：假如上游 `psi_common` 新增了一个本 IP 需要的文件 `psi_common_xxx.vhd`，本仓库要改哪些地方才能让它综合进 IP？

**参考答案**：第一，更新 [README.md](../README.md) 的依赖版本要求（如果版本约束需要变化）；第二，在 [package.tcl](../scripts/package.tcl) 的 `add_lib_relative` 列表里新增该文件路径；第三，确认本地已通过 `dependencies.py` 拉到含该文件的新版 `psi_common`。只改 README 不够——README 只决定「拉哪个版本」，`add_lib_relative` 才决定「综合哪些文件」。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「依赖全景梳理」：

1. **读 README，列出全部外部依赖**：打开 [README.md:29-44](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L29-L44)，把 6 条外部依赖按「运行时 / 开发期 / 参考设计」三类填进一张表。
2. **读 dependencies.py，画一张数据流图**：画出 `README.md → Parse.FromReadme → dependencies（结构化列表）→ Actions.ExecMain → git clone 到 <PsiFpgaLib 根>/VHDL/` 这条链路。标注每一步发生在哪个函数里（其中 `Parse`/`Actions` 来自外部包）。
3. **读 package.tcl，验证布局**：确认 `add_lib_relative` 的基准 `"../../.."` 从 `scripts/` 出发正好落到 PsiFpgaLib 根；再把列表 24 个文件按上游仓库归类，确认全部是运行时依赖。
4. **交叉验证**：对比第 1 步和第 3 步——README 里标 `for development only` 的依赖（`psi_tb`/`PsiSim`/`PsiIpPackage`），是否确实**没有**出现在 `add_lib_relative` 列表里？如果一致，说明「README 声明的运行时依赖」与「实际综合进 IP 的文件」是对得上的。

**预期结果**：你会得到一个清晰的认识——本仓库的运行时依赖只有 `psi_common`（17 个文件）和 `psi_multi_stream_daq`（7 个文件 + 驱动），它们由 `dependencies.py` 拉取、由 `package.tcl` 的 `add_lib_relative` 综合进 IP；其余依赖只服务于开发或参考设计。

## 6. 本讲小结

- README 的「Dependencies」段被两行 HTML 注释标记包裹，是依赖清单的**唯一事实来源**，既给人看也给脚本解析。
- 6 条外部依赖中，只有 `psi_common` 和 `psi_multi_stream_daq` 是**运行时必需**（无 `for ... only` 标签）；`PsiSim`/`PsiIpPackage`/`psi_tb` 是开发期依赖，`vivadoIP_axis_data_gen` 是参考设计专用。
- [scripts/dependencies.py](../scripts/dependencies.py) 是 15 行薄脚本，通过 `Parse.FromReadme` + `Actions.ExecMain` 调用外部包 `PsiFpgaLibDependencies` 完成依赖拉取，自身不含任何 git 逻辑。
- 依赖被克隆到 PSI 统一布局的约定位置（`<PsiFpgaLib 根>/VHDL/<仓库>/`），所以 [package.tcl](../scripts/package.tcl) 能用写死的相对路径 `../../../VHDL/...` 取到文件。
- `package.tcl` 用 `add_sources_relative` 声明本地唯一 RTL，用 `add_lib_relative` 声明 24 个被综合进 IP 的上游文件（psi_common 17 个 + psi_multi_stream_daq 7 个），全部是运行时依赖。
- README 声明的运行时依赖与 `add_lib_relative` 实际综合的文件**一一对应、互相印证**。

## 7. 下一步学习建议

- 下一篇 [u1-l4](u1-l4-ip-packaging-overview.md) 会以 `package.tcl` 为主线讲清「从 RTL 到可例化 IP」的完整打包流程，本讲涉及的 `add_sources_relative`/`add_lib_relative`/`add_drivers_relative` 将在那里被放进全局步骤里理解。
- 想提前了解那 24 个上游文件里 wrapper 真正直接例化的对象，可跳读 [hdl/psi_ms_daq_vivado.vhd](../hdl/psi_ms_daq_vivado.vhd) 中 `i_impl : entity work.psi_ms_daq_axi` 的例化（对应 [u2-l3](u2-l3-instantiating-impl.md)）。
- 若想了解依赖拉取的底层细节（`Actions.ExecMain` 具体支持哪些子命令、如何校验版本），应去外部包 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 仓库阅读，本仓库不包含这部分实现。
