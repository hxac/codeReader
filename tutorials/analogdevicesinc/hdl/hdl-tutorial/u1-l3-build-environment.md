# 构建环境与工具链版本

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚构建 ADI HDL 工程需要安装哪些工具链（AMD Xilinx Vivado / Intel Quartus / Lattice Radiant），以及为什么必须用「指定版本」。
- 读懂 `scripts/adi_env.tcl`，从中查出当前分支要求的具体工具版本号。
- 理解 `ADI_HDL_DIR`、`ADI_GHDL_DIR`、`ADI_IGNORE_VERSION_CHECK`、`REQUIRED_VIVADO_VERSION` 等关键环境变量的作用与优先级。
- 掌握 `QUARTUS_PRO_ISUSED` 这类「自动探测」逻辑是如何根据工程路径推断工具行为的。
- 知道在「被迫使用非推荐版本」的调试场景下，如何安全地绕过版本检查。

本讲是入门层的第三篇，承接 [u1-l1（项目总览）](u1-l1-project-overview.md) 提到的「release 分支与 main 分支对应不同工具版本」，以及 [u1-l2（目录结构）](u1-l2-repo-structure.md) 提到的「`scripts/adi_env.tcl` 在全局脚本目录中集中声明工具版本」。本讲只讲「环境与版本」，不涉及具体工程的构建流程——那是 [u1-l4（构建第一个工程）](u1-l4-first-build.md) 的内容。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**第一，FPGA 工具链是「带版本的编译器」。** 就像 C 代码依赖特定版本的 gcc 一样，Verilog/VHDL 依赖特定版本的综合工具（Vivado / Quartus / Radiant）。不同版本之间，IP 接口、约束语法、底层原语都可能变化。ADI 每个发布分支（release branch）只在一组**经过硬件验证**的工具版本上测试，所以仓库会用脚本强制检查版本。

**第二，ADI HDL 同时支持三家厂商。** 同一份 Verilog 源码要能分别在 AMD Xilinx、Intel、Lattice 的工具里打包和综合。因此版本管理不是「一个版本号」，而是「每个厂商一个版本号」，外加一些厂商专属的探测逻辑。

**第三，环境变量是「配置覆盖」机制。** 仓库在 Tcl 脚本里写死了默认版本号，但允许你用环境变量临时覆盖。理解「默认值 → 环境变量覆盖」这一优先级，是读懂 `adi_env.tcl` 的关键。下面用一个简单的优先级表来概括：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 高 | shell 环境变量 `::env(NAME)` | 用户在终端 `export` 的值，优先级最高 |
| 中 | Tcl 变量 `NAME`（脚本内已存在） | 由上游脚本提前设置 |
| 低 | 脚本中的字面量默认值 | 写在 `adi_env.tcl` 里的版本号 |

> 名词解释：
> - **Vivado**：AMD（原 Xilinx）的 FPGA 综合实现工具。
> - **Quartus**：Intel（原 Altera）的 FPGA 工具，分 **Pro** 版与 **Standard（Std）** 版，两者命令行与 IP 机制不同。
> - **Radiant / Propel**：Lattice 的 FPGA 工具，本仓库用 `tclsh` 配合其 Tcl 接口打包 IP。
> - **tclsh**：Tcl 语言的命令行解释器，本仓库大量用 Tcl 脚本驱动工具链。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| [scripts/adi_env.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl) | **核心**：集中声明三家工具的「要求版本号」、解析关键环境变量、做自动探测。 |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | 消费 `required_vivado_version`，在创建 Vivado 工程时做实际的版本比对与拦截。 |
| [projects/scripts/adi_project_intel.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl) | 消费 `required_quartus_version` 与 `quartus_pro_isused`，做 Quartus 版本检查并据此选择 Pro/Std 流程。 |
| [projects/scripts/adi_project_lattice.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl) | 消费 `required_lattice_version`，做 Radiant 版本检查。 |
| [library/scripts/lattice_tool_set.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/lattice_tool_set.mk) | Lattice 侧 Make 片段，设定 `LATTICE_IP_TOOL := tclsh` 与 Propel IP 的默认搜索路径。 |
| [projects/fmcomms2/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl) | 一个真实工程入口，第一行就 `source` 了 `adi_env.tcl`，展示它如何被加载。 |
| [README.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md) | 顶层说明，指向工具版本与下载地址。 |

记忆要点：`adi_env.tcl` 是「**版本与环境的唯一事实来源**」（README 也明确把它称作 "the script that sets these versions"）；三个 `adi_project_*.tcl` 是「**版本检查的执行者**」。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 工具链前置条件**：要装什么、版本为什么重要。
- **4.2 adi_env.tcl 版本与环境变量**：版本号写在哪、`ADI_HDL_DIR` 等变量怎么解析。
- **4.3 QUARTUS_PRO_ISUSED 等自动探测逻辑**：脚本如何根据工程路径自动判断行为。

### 4.1 工具链前置条件

#### 4.1.1 概念说明

要构建 ADI HDL 工程，你需要两样东西：

1. **GNU Make**：构建的总调度器。用户在工程目录敲 `make`，由 Make 调用对应的工具链。
2. **一家（或多家）FPGA 厂商工具**：AMD Xilinx Vivado **或** Intel Quartus **或** Lattice Radiant。三者是「或」的关系——你只为手头的载板安装对应厂商的工具即可。

README 的 Prerequisites 一节把这一点说得很直白：Vivado 与 Quartus 是「二选一」，并且要确保安装 [required](https://github.com/analogdevicesinc/hdl/releases) 的工具版本。

为什么版本如此重要？因为 ADI 的发布分支**只在特定工具版本上做过硬件验证**。官方文档 [releases.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst) 明确写道：release 分支只在某些版本的工具上测试过，**可能无法**在其他版本上工作；而且「跨版本移植虽然可能，但不推荐」。

#### 4.1.2 核心流程

从「想知道该装什么版本」到「真正开始构建」，流程如下：

```text
选定分支 (main 或某个 release 分支)
        │
        ▼
查 scripts/adi_env.tcl  ──→  得到该分支要求的工具版本号
        │
        ▼
安装对应版本的工具链 (Vivado / Quartus / Radiant)
        │
        ▼
cd 到工程目录，执行 make
        │
        ▼
make 调用 vivado/quartus/radiant，Tcl 脚本再次校验版本
        │
   版本匹配？──否──→ 报 ERROR 并退出 (除非设置了 ADI_IGNORE_VERSION_CHECK)
        │是
        ▼
     正常构建
```

关键点：版本号会**被检查两次**——一次是你装工具时自己核对，一次是构建脚本运行时自动比对。后者是硬性拦截。

#### 4.1.3 源码精读

README 指明工具下载入口与「必读版本脚本」：

- [README.md:65-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L65-L74)：Prerequisites，声明需要 Vivado **或** Quartus，并要求核对版本。

- [README.md:129-132](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L129-L132)：告诉你「每个分支对应的 Vivado/Quartus 版本」可在线查表，也可以直接读 `scripts/adi_env.tcl`。这一行正是本讲存在的理由。

构建指南则把「版本检查」列为构建前的强制步骤：

- [docs/user_guide/build_hdl.rst:496-500](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/build_hdl.rst#L496-L500)：构建前**必须**查 `adi_env.tcl` 里的 Vivado 版本；不想查的话，可以 `export ADI_IGNORE_VERSION_CHECK=1`，但官方强烈不建议，否则工程会失败。

#### 4.1.4 代码实践

**实践目标**：确认本仓库支持的厂商工具范围与「必查版本」的官方要求。

**操作步骤**：

1. 打开 `README.md`，定位到 `## Prerequisites` 一节。
2. 打开 `docs/user_guide/build_hdl.rst`，定位到 "4. Building the projects" 的 `.. caution::` 块。

**需要观察的现象**：

- README 用 "or"（**或**）连接 Vivado 与 Quartus，说明不需要同时安装。
- build_hdl.rst 把「查 `adi_env.tcl` 的版本」列为构建前的第一步 caution。

**预期结果**：你能在两份文档中各找到一处明确指向 `scripts/adi_env.tcl` 的引用，确认它就是「版本事实来源」。

#### 4.1.5 小练习与答案

**练习 1**：假如你只有一块 Lattice 载板，是否需要安装 Vivado 和 Quartus？

> **答案**：不需要。三家厂商工具是「或」的关系，只需安装 Lattice Radiant/Propel 即可构建 Lattice 工程。

**练习 2**：为什么 README 反复强调要用 release 分支的「指定版本」工具，而不是随便装一个最新版？

> **答案**：因为 release 分支只在该指定版本上做过硬件验证；其他版本可能因 IP 接口、约束语法或底层原语变化而无法综合或无法在硬件上正常工作。

---

### 4.2 adi_env.tcl 版本与环境变量

#### 4.2.1 概念说明

`scripts/adi_env.tcl` 是全仓唯一的「版本与环境配置中心」。它做三件事：

1. **定位仓库根目录**：算出 `ad_hdl_dir`，供后续脚本拼路径。
2. **声明三家工具的要求版本号**：Vivado、Quartus（Pro）、Quartus Std、Lattice 各一个。
3. **解析一组关键环境变量**：允许用户覆盖默认版本、跳过检查、引入第二个仓库目录等。

它被每个工程入口的第一行 `source` 进来。例如 fmcomms2/zcu102 工程：

- [projects/fmcomms2/zcu102/system_project.tcl:6](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L6)：`source ../../../scripts/adi_env.tcl`——这一行执行后，`ad_hdl_dir`、`required_vivado_version` 等变量就在当前 Tcl 解释器里可用了。

#### 4.2.2 核心流程

`adi_env.tcl` 内部对「每一个配置项」都遵循同一个解析模板：

```text
设一个字面量默认值
        │
   shell 里有 ::env(NAME) 吗？──是──→ 用环境变量的值覆盖
        │否
   Tcl 里有变量 NAME 吗？──是──→ 用 Tcl 变量的值覆盖
        │否
        ▼
     保留默认值
```

这就是第 2 节那张优先级表的具体落地。理解了这个模板，整段脚本就是它的重复展开。

需要重点认识的环境变量：

| 变量 | 含义 | 默认/取值 |
|------|------|-----------|
| `ADI_HDL_DIR` | 本仓库（hdl）的根目录 | 脚本自动推断为 `scripts/` 的上一级 |
| `ADI_GHDL_DIR` | 第二个仓库目录（用于额外库，较少见） | 不设置则不存在 |
| `REQUIRED_VIVADO_VERSION` | 覆盖要求的 Vivado 版本 | 字面量 `2025.1` |
| `REQUIRED_QUARTUS_VERSION` | 覆盖要求的 Quartus 版本 | 字面量 `25.3.0`（或 Std 值） |
| `REQUIRED_LATTICE_VERSION` | 覆盖要求的 Lattice 版本 | 字面量 `2025.2` |
| `ADI_IGNORE_VERSION_CHECK` | 跳过版本硬拦截 | 不设置则为 `0`（不跳过） |

#### 4.2.3 源码精读

**（a）定位仓库根目录与 `ADI_HDL_DIR`**

- [scripts/adi_env.tcl:7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L7)：用 `info script` 拿到当前脚本自身路径，`file dirname` 取目录，再 `../` 上一层，得到仓库根目录作为默认 `ad_hdl_dir`。

- [scripts/adi_env.tcl:9-13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L9-L13)：如果 shell 里设了 `ADI_HDL_DIR`，就用它覆盖；否则把推断出来的目录**回写**到 `::env(ADI_HDL_DIR)`，让子进程也能继承。这就是「优先环境变量，否则自动推断并回写」的模式。

- [scripts/adi_env.tcl:15-17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L15-L17)：`ADI_GHDL_DIR` 仅在环境变量存在时才设置 `ad_ghdl_dir`，用于引入第二个存放 library 的仓库（例如把通用库与项目库分开放）。

**（b）四家/三个工具版本号的声明**

- [scripts/adi_env.tcl:20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L20)：`set required_vivado_version "2025.1"`——**当前分支要求的 Vivado 版本是 `2025.1`**。
- [scripts/adi_env.tcl:21-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L21-L25)：允许用 `::env(REQUIRED_VIVADO_VERSION)` 或 Tcl 变量 `REQUIRED_VIVADO_VERSION` 覆盖。注意这一处同时支持「shell 环境变量」和「Tcl 变量」两种覆盖来源——这就是优先级表里「高」与「中」两层。

- [scripts/adi_env.tcl:54-62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L54-L62)：Quartus 有两个版本号：
  - 第 54 行 `required_quartus_version "25.3.0"`：**Quartus Pro 要求 `25.3.0`**。
  - 第 55 行 `required_quartus_std_version "24.1std.0"`：**Quartus Standard 要求 `24.1std.0`**。
  - 第 60-62 行：如果检测到要用 Std 版（`quartus_pro_isused == 0`），就把 `required_quartus_version` 改成 Std 的值。这是一种「版本号联动探测」。

- [scripts/adi_env.tcl:65](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L65)：`set required_lattice_version "2025.2"`——**当前分支要求的 Lattice Radiant 版本是 `2025.2`**。
- [scripts/adi_env.tcl:66-70](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L66-L70)：同样支持 `REQUIRED_LATTICE_VERSION` 覆盖。

> 一句话汇总本分支要求（这也是本讲实践任务的核心答案）：
> **Vivado = `2025.1`，Quartus Pro = `25.3.0`，Quartus Std = `24.1std.0`，Lattice Radiant = `2025.2`。**

**（c）`ADI_IGNORE_VERSION_CHECK` 解析**

- [scripts/adi_env.tcl:28-32](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L28-L32)：如果设了 `::env(ADI_IGNORE_VERSION_CHECK)`，就把 `IGNORE_VERSION_CHECK` 置 1；否则（且 Tcl 变量未预先存在）置 0。这个标志位随后被三个 `adi_project_*.tcl` 读取，决定版本不匹配时是「报错退出」还是「只给一条警告」。

**（d）一个通用取值小工具**

- [scripts/adi_env.tcl:76-83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L76-L83)：`get_env_param` 过程——「有环境变量就读、没有就返回默认值」，并在读取时打印一行日志。它把上面那个「优先级模板」封装成了可复用函数，别的脚本会调用它取参数。

#### 4.2.4 代码实践

**实践目标**：用 `tclsh` 实际加载 `adi_env.tcl`，把四个版本号和关键标志位打印出来，验证你读到的是「运行时真实生效」的值，而不只是文本。

**操作步骤**：

1. 在仓库根目录新建一个临时探针脚本 `probe_env.tcl`（**示例代码**，不要提交到仓库）：

   ```tcl
   # 示例代码：探针，仅供本地学习使用
   source scripts/adi_env.tcl
   puts "VIVADO  = $required_vivado_version"
   puts "QUARTUS = $required_quartus_version"
   puts "QUARTUS_STD = $required_quartus_std_version"
   puts "LATTICE = $required_lattice_version"
   puts "IGNORE_VERSION_CHECK = $IGNORE_VERSION_CHECK"
   puts "QUARTUS_PRO_ISUSED = $quartus_pro_isused"
   ```

2. 运行 `tclsh probe_env.tcl`（`tclsh` 是本仓库 Lattice 侧也用到的解释器，见 4.3 节）。
3. 再试一次带覆盖：`env REQUIRED_VIVADO_VERSION=2099.1 tclsh probe_env.tcl`。

**需要观察的现象**：

- 第一次运行应打印出 `VIVADO = 2025.1` 等默认值。
- 第二次运行，`VIVADO` 应变成 `2099.1`，证明环境变量确实覆盖了字面量默认值。

**预期结果**：你亲眼看到「默认值 → 环境变量覆盖」的优先级在运行时生效。精确输出取决于本地 Tcl 环境，若 `tclsh` 未安装则**待本地验证**（可在 Linux 上用包管理器安装 `tcl`）。

#### 4.2.5 小练习与答案

**练习 1**：`required_vivado_version` 同时检查了 `::env(REQUIRED_VIVADO_VERSION)` 和 Tcl 变量 `REQUIRED_VIVADO_VERSION` 两处，为什么？

> **答案**：因为版本号可能由两种途径传入——用户在 shell 里 `export`（成为 `::env`），或上游 Make/Tcl 脚本预先 `set` 了同名 Tcl 变量（例如 `project-xilinx.mk` 在调用库打包时会透传 `REQUIRED_VIVADO_VERSION`）。两处都查，才能兼顾这两种调用方。

**练习 2**：如果不设置 `ADI_GHDL_DIR`，会发生什么？

> **答案**：`ad_ghdl_dir` 不会被创建，后续脚本中所有「如果存在 `ADI_GHDL_DIR`」的判断分支都会被跳过，仓库只用单一的 `library/` 目录。这是引入「第二库目录」的可选机制。

---

### 4.3 QUARTUS_PRO_ISUSED 等自动探测逻辑

#### 4.3.1 概念说明

Intel 的 Quartus 分 **Pro** 版和 **Standard（Std）** 版，二者不仅版本号不同（本分支 Pro 是 `25.3.0`，Std 是 `24.1std.0`），连 Qsys 的命令行参数、IP 机制、时序分析开关都有差异。问题是：脚本怎么知道你当前工程该按 Pro 还是 Std 来跑？

ADI 的做法是「**默认 Pro，按工程路径自动降级到 Std**」：只要工程路径里出现特定载板名（这些载板只能用 Std 版），就自动切换。这由 `QUARTUS_PRO_ISUSED` 控制。

这种「根据上下文自动推断」的思想在构建系统里很常见——用约定（路径名）代替手动配置，减少出错。

#### 4.3.2 核心流程

`quartus_pro_isused` 的决策逻辑：

```text
默认 quartus_pro_isused = 1   （假设用 Pro 版）
        │
  设了 ::env(QUARTUS_PRO_ISUSED) ？──是──→ 用环境变量的值
        │否
  Tcl 变量 QUARTUS_PRO_ISUSED 已存在？──是──→ 用该值
        │否
  当前路径 [pwd] 包含 "de10nano" 或 "c5soc"？
        │是                              │否
        ▼                                 ▼
  quartus_pro_isused = 0            保持 = 1
  （切到 Std）                      （继续用 Pro）
```

随后这个值产生两个连锁影响：

1. **版本号联动**（见 4.2.3b）：若 `quartus_pro_isused == 0`，`required_quartus_version` 被改成 Std 的 `24.1std.0`。
2. **流程分支**（见 4.3.3b）：在 `adi_project_intel.tcl` 里，Pro 与 Std 走不同的 Qsys 调用参数与不同的时序分析开关。

#### 4.3.3 源码精读

**（a）自动探测本体**

- [scripts/adi_env.tcl:36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L36)：默认 `quartus_pro_isused` 为 1（Pro）。

- [scripts/adi_env.tcl:37-41](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L37-L41)：同样支持「环境变量优先、Tcl 变量次之」的覆盖。

- [scripts/adi_env.tcl:42-49](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L42-L49)：核心探测——定义 `quartus_std_carriers {de10nano c5soc}`（这两种载板只能用 Quartus Std），然后遍历它们，只要当前工作目录 `[pwd]` 的字符串里能匹配到其中任一名字，就把 `quartus_pro_isused` 置 0 并 `break`。换句话说，**当你在 `projects/.../de10nano` 或 `.../c5soc` 目录下构建时，脚本自动认定你在用 Std 版**。

**（b）探测结果如何驱动 Quartus 流程**

`quartus_pro_isused` 被传递到 Intel 工程脚本，决定具体的命令行差异：

- [projects/scripts/adi_project_intel.tcl:146-180](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L146-L180)：Pro 版（`== 1`）用带 `--quartus-project` 参数的 `qsys-script` / `qsys-generate`；Std 版则不带该参数，并额外打开 `ENABLE_ADVANCED_IO_TIMING`（注释说明「I/O Timing Analysis 仅在 Quartus Standard 可用」）。同一份脚本，靠这一个标志位分叉出两条工具链路径。

**（c）版本检查的真正执行点（三家对照）**

`adi_env.tcl` 只**声明**版本号与标志位；真正「比对实际工具版本并决定是否拦截」的是三个工程脚本。它们的模式完全一致——读实际版本、字符串比较、按 `IGNORE_VERSION_CHECK` 决定报错还是报警告：

- **Vivado**：[projects/scripts/adi_project_xilinx.tcl:202-217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L202-L217)。第 202 行用 Vivado 内建命令 `[version -short]` 取实际版本；不匹配时，若 `IGNORE_VERSION_CHECK` 为真则只打印 `CRITICAL WARNING`，否则打印 `ERROR` 并 `exit 2` 终止构建。

- **Quartus**：[projects/scripts/adi_project_intel.tcl:79-94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L79-L94)。第 79 行从 Quartus 注入的 `quartus(version)` 数组里取实际版本 `[lindex $quartus(version) 1]`，比对逻辑与 Vivado 完全对称。

- **Lattice**：[projects/scripts/adi_project_lattice.tcl:127-148](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl#L127-L148)。第 127-128 行用 Radiant/Propel 的 `sys_install_version` 取版本并截取前两段，再与 `required_lattice_version` 比对。

> 一个易被忽略的细节：三处比对都用 Tcl 的 `string compare` 做**字符串**比较，而不是语义化版本比较。这意味着版本号格式必须严格一致（例如 `2025.1` 不能写成 `2025.10` 期望被当成更晚的版本）。这也解释了为什么 `adi_env.tcl` 里的版本字符串要写得和工具实际报告的格式分毫不差。

#### 4.3.4 代码实践

**实践目标**：验证 `QUARTUS_PRO_ISUSED` 的自动探测在不同目录下会给出不同结果。

**操作步骤**：

1. 把 4.2.4 的探针脚本扩展，再打印 `[pwd]`：

   ```tcl
   # 示例代码
   source scripts/adi_env.tcl
   puts "PWD = [pwd]"
   puts "QUARTUS_PRO_ISUSED = $quartus_pro_isused"
   puts "QUARTUS_VERSION_REQUIRED = $required_quartus_version"
   ```

2. 在仓库根目录运行一次 `tclsh probe_env.tcl`（`[pwd]` 不含 de10nano/c5soc）。
3. 进入一个名字含 `c5soc` 的目录（例如 `cd projects/common/c5soc` 若存在，或临时 `mkdir -p /tmp/c5soc && cd /tmp/c5soc` 后把 `adi_env.tcl` 的相对路径改成绝对路径再运行）。

**需要观察的现象**：

- 第 2 步：`QUARTUS_PRO_ISUSED = 1`，`QUARTUS_VERSION_REQUIRED = 25.3.0`（Pro）。
- 第 3 步：因路径含 `c5soc`，`QUARTUS_PRO_ISUSED = 0`，`QUARTUS_VERSION_REQUIRED` 联动变成 `24.1std.0`（Std）。

**预期结果**：你用同一个脚本、仅改变工作目录，就看到 Pro/Std 自动切换及版本号联动。精确行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `quartus_std_carriers` 里是 `de10nano` 和 `c5soc` 这两个名字？

> **答案**：这两种载板（DE10-Nano、Cyclone V SoC）对应的 FPGA（Cyclone V 系列）只被 Quartus Standard 支持，不在 Quartus Pro 的支持列表里。因此脚本遇到它们时必须切到 Std 版，否则工具链会拒绝器件。

**练习 2**：如果我想强制某个新载板也走 Std 流程，但不想改 `adi_env.tcl` 的代码，该怎么办？

> **答案**：在构建前 `export QUARTUS_PRO_ISUSED=0`。因为环境变量优先级最高（见 [scripts/adi_env.tcl:37-38](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L37-L38)），它会跳过基于路径的自动探测，直接采用你给的值。

---

## 5. 综合实践

把三个模块串起来，完成一个「**版本核对 + 调试绕过**」的小任务，这是本讲实践任务（practice_task）的完整版。

**任务背景**：假设你拿到一台只装了 Vivado `2024.2` 的机器，却被要求构建一个本分支（要求 `2025.1`）的工程。你需要：先查清要求版本，再决定如何处理版本不匹配。

**操作步骤**：

1. **查要求版本**：阅读 [scripts/adi_env.tcl:20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L20) 与第 54、55、65 行，填写下表（这是本讲的核心产出）：

   | 工具 | 要求版本（本分支） | 出处行号 |
   |------|------------------|----------|
   | Vivado | `2025.1` | adi_env.tcl:20 |
   | Quartus Pro | `25.3.0` | adi_env.tcl:54 |
   | Quartus Std | `24.1std.0` | adi_env.tcl:55 |
   | Lattice Radiant | `2025.2` | adi_env.tcl:65 |

2. **预测构建结果**：你的 Vivado 是 `2024.2`，与要求的 `2025.1` 不匹配。对照 [adi_project_xilinx.tcl:202-217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L202-L217) 预测：构建会在版本检查处打印 `ERROR: vivado version mismatch` 并 `exit 2` 终止。

3. **解释 `ADI_IGNORE_VERSION_CHECK` 的调试场景**：阅读 [docs/user_guide/releases.rst:34-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L34-L40) 与 [docs/user_guide/build_hdl.rst:496-500](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/build_hdl.rst#L496-L500)。用一段话说明：在**「被迫跨版本移植」**（例如评估某个新工具版本是否能跑通、或在升级窗口期临时构建）这种调试/验证场景下，可 `export ADI_IGNORE_VERSION_CHECK=1` 把硬 `ERROR` 降级为 `CRITICAL WARNING`，让构建继续。但要明白——官方明确声明**不保证、也不支持**在非推荐版本上的结果，这只是为了让你能「试着跑」。

4. **（可选，待本地验证）亲手验证**：

   ```bash
   export ADI_IGNORE_VERSION_CHECK=1
   env ADI_IGNORE_VERSION_CHECK=1 tclsh probe_env.tcl   # 应见 IGNORE_VERSION_CHECK = 1
   ```

**预期结果**：你产出一张本分支工具版本表，能准确预测版本不匹配时的拦截行为，并能说清 `ADI_IGNORE_VERSION_CHECK` 适用的「跨版本移植/临时验证」场景及其「官方不支持」的代价。

## 6. 本讲小结

- 构建 ADI HDL 工程需要 **GNU Make + 一家厂商工具**（Vivado / Quartus / Radiant，三选一），且必须用**指定版本**。
- `scripts/adi_env.tcl` 是全仓「版本与环境的唯一事实来源」：声明四组版本号、解析 `ADI_HDL_DIR` 等环境变量、做 `QUARTUS_PRO_ISUSED` 自动探测。
- 本分支（HEAD `e57851ff`）的要求版本是：**Vivado `2025.1`、Quartus Pro `25.3.0`、Quartus Std `24.1std.0`、Lattice Radiant `2025.2`**。
- 每个配置项都遵循「shell 环境变量 > Tcl 变量 > 字面量默认值」的优先级，环境变量可覆盖几乎所有默认值。
- `QUARTUS_PRO_ISUSED` 默认为 Pro，当工程路径含 `de10nano` / `c5soc` 时自动降级为 Std，并联动切换要求版本号与 Qsys 命令行参数。
- 真正的版本拦截发生在三个 `adi_project_*.tcl` 里：版本不匹配默认 `ERROR + exit 2`，设 `ADI_IGNORE_VERSION_CHECK=1` 可降级为警告，用于「跨版本移植/临时验证」调试，但官方不予支持。

## 7. 下一步学习建议

- 接下来读 **[u1-l4 构建第一个工程：从 make 到比特流](u1-l4-first-build.md)**，把本讲学到的版本检查放进完整的 `make → vivado` 调用链中观察。
- 想了解版本与发布分支的长期策略，可先浏览 [docs/user_guide/releases.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst)，这会在 **u8-l4（Boot 镜像生成与发布管理）** 深入讲解。
- 进阶阶段（第 3 单元）会拆解 `project-xilinx.mk` 如何把 `REQUIRED_VIVADO_VERSION` 经由 Make 透传到 Tcl（参见 [projects/scripts/project-xilinx.mk:142-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L142-L146)），届时你会看到「环境变量覆盖」在 Make 与 Tcl 两层之间的完整闭环。
