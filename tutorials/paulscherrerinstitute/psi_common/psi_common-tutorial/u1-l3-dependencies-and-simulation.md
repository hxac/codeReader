# 依赖管理与仿真运行

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `psi_common` 仿真到底依赖哪些外部仓库、为什么必须按固定目录结构摆放它们。
- 读懂 `sim/config.tcl` 这份「回归测试注册表」，理解它是如何把库源码、被测源码、测试平台分组并逐个注册运行的。
- 用 Modelsim 或 GHDL（以及 Vivado 仿真器）跑通整套回归测试，并理解交互仿真命令的用途。
- 在 `config.tcl` 中定位某个组件（例如 `pl_stage`）的测试平台注册条目，并解释它传递了哪些 generic 参数。

本讲只讲「仿真如何组织、如何跑」，不展开任何具体组件的内部实现——那是后续讲义的任务。

## 2. 前置知识

在进入本讲之前，你需要先具备以下认知（它们在前两讲已建立）：

- **VHDL 与仿真**：VHDL 是硬件描述语言，写好的代码必须经过「编译（elaborate/compile）→ 加载（load）→ 运行（run）」才能在仿真器里看到波形或打印。常见仿真器有 Mentor Modelsim、开源 GHDL、Xilinx Vivado xsim。
- **Testbench（测试平台）**：一段不对应真实硬件、专门用来给被测组件喂激励并检查输出的 VHDL 代码。`psi_common` 要求所有非平凡组件都配「自校验 testbench」——也就是 TB 自己判断对错，出错时打印以 `###ERROR###` 开头的报文。
- **回归测试（regression test）**：把库中所有 TB 一次性批量跑完、最后统一检查有没有 `###ERROR###` 的脚本化流程，用于确认整库没有被某次改动改坏。
- **TCL**：一种脚本语言。Modelsim、Vivado 都内置 TCL 控制台；GHDL 本身是命令行工具，但本库用一个 TCL 框架统一驱动它。
- **仓库结构 vs 工作副本结构**：前讲已说明，单独克隆 `psi_common` 就能读源码；但只要你想「跑仿真或改库」，就必须把它和兄弟仓库按固定相对路径摆好。本讲会把这套结构讲透。

> 名词速查：**generic（类属参数）** 是 VHDL 在「编译期」定制实体的参数（如位宽、FIFO 深度），在命令行里用 `-g名字=值` 传递；**library（VHDL 库）** 是编译产物的逻辑容器，`psi_common` 要求所有文件编译进同一个库。

## 3. 本讲源码地图

本讲涉及的关键文件全部在仓库的文档与仿真脚本目录里，没有任何 `.vhd` 实现需要深读：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md) | 顶层说明，其中「Dependencies」段声明依赖与目录结构，「Simulations and Testbenches」段说明回归测试入口。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) | 回归测试的「注册表」：声明编译哪些源码、哪些 TB，以及每个 TB 用哪些 generic 组合跑。 |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/run.tcl) | Modelsim 回归入口脚本。 |
| [sim/runGhdl.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/runGhdl.tcl) | GHDL 回归入口脚本。 |
| [sim/interactive.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/interactive.tcl) / [sim/interactiveGhdl.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/interactiveGhdl.tcl) | 交互仿真入口：只编译、加载框架，不自动跑，方便手工调试单个 TB。 |
| [doc/old/ch1_introduction/ch1_introduction.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md) | 老版手册第 1 章，权威地描述了工作副本结构、VHDL 库使用方式与运行步骤。 |

> 提示：本仓库的脚本统一引用一个名为 **PsiSim** 的 TCL 仿真框架，它提供了 `init`、`compile_files`、`run_tb`、`run_check_errors` 等命令。这些命令不属于标准 TCL，而是 PsiSim 定义的，是理解所有 `sim/*.tcl` 的钥匙。

## 4. 核心概念与源码讲解

### 4.1 依赖关系与工作副本目录结构

#### 4.1.1 概念说明

`psi_common` 自身的源码（`hdl/`）是可以「单仓库独立阅读」的。但是它的**测试平台**用到了另一个仓库 `psi_tb`（提供 `txt_util`、`compare_pkg` 等仿真专用工具包），而**驱动整个回归流程**的 TCL 框架又来自第三个仓库 `PsiSim`。此外还提供了把所有 FPGA 相关仓库按正确结构一次性拉下来的聚合仓库 `psi_fpga_all`。

因此存在两套「结构」概念，不要混淆：

- **仓库结构**：单个 `psi_common` 仓库内部的目录（`hdl/`、`testbench/`、`sim/` 等），前讲已讲。
- **工作副本结构（working copy structure）**：为了跑仿真/改库，必须把 `psi_common`、`psi_tb`、`PsiSim` 三个仓库按固定相对路径摆成一个目录树，因为脚本之间用**相对路径**互相引用。

#### 4.1.2 依赖清单

README 用一段被脚本解析的标记包裹住依赖声明（注释明确写着 `DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies`）：

- [README.md:L54-L65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L54-L65) —— 依赖段：TCL 侧依赖 `PsiSim`（≥ 2.1.0），VHDL 侧依赖 `psi_tb`（≥ 3.0.0），并提示可用 `psi_fpga_all` 聚合仓库一次性获取。

要点：

- **PsiSim**（TCL 框架）：提供回归/交互仿真的全部命令，版本要求 2.1.0 以上。
- **psi_tb**（VHDL 仿真工具包）：提供文本打印、数值比较、AXI/I2C 仿真辅助等，版本要求 3.0.0 以上。
- **psi_common** 自己：被测库本体。

#### 4.1.3 工作副本目录树（从脚本相对路径反推）

`sim/` 里的脚本用相对路径互相引用，反推即可得到必须的目录树。下面三处证据相互印证：

1. 入口脚本引用 PsiSim 框架（`sim/run.tcl`、`sim/runGhdl.tcl` 都从 `sim/` 往上走三级再进 `TCL/PsiSim/`）：[sim/run.tcl:L7-L8](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/run.tcl#L7-L8)
   ```tcl
   #Load dependencies
   source ../../../TCL/PsiSim/PsiSim.tcl
   ```
2. `config.tcl` 定义库根并从这里加载 `psi_common` 与 `psi_tb` 的源码：[sim/config.tcl:L8-L31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L8-L31)
   ```tcl
   set LibPath "../.."
   ...
   add_sources $LibPath {
       psi_common/hdl/psi_common_array_pkg.vhd \
       ...
       psi_tb/hdl/psi_tb_txt_util.vhd \
       ...
   } -tag lib
   ```
3. 同一份 `config.tcl` 又用 `../hdl` 和 `../testbench` 引用本项目源码与 TB：[sim/config.tcl:L33-L34](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L33-L34)

把这三条相对路径合并，得到如下目录树（`<Root>` 推荐命名为 `psi_lib`）：

```
<Root>/                          ← 推荐命名 psi_lib
├── TCL/
│   └── PsiSim/                  ← sim/*.tcl 里 ../../../TCL/PsiSim/PsiSim.tcl
│       └── PsiSim.tcl
└── VHDL/
    ├── psi_common/              ← 当前仓库
    │   ├── hdl/                 ← config.tcl 里 ../hdl（相对 sim/）
    │   ├── testbench/           ← config.tcl 里 ../testbench
    │   └── sim/                 ← 回归脚本所在目录，本讲所有 source 在此执行
    └── psi_tb/                  ← config.tcl 里 $LibPath/psi_tb/hdl
        └── hdl/
```

**路径算术**：站在 `sim/` 里看——

- `../hdl` → `psi_common/hdl/`（往上 1 级到 `psi_common/`，再进 `hdl/`）。
- `../..` → `VHDL/`（往上 2 级），所以 `$LibPath/psi_common/...` 与 `$LibPath/psi_tb/...` 都解析到 `VHDL/` 下的两个兄弟仓库。
- `../../../TCL/PsiSim/PsiSim.tcl` → 往上 3 级到 `<Root>/`，再进 `TCL/PsiSim/`。

只要目录名拼错或层级不对，`source` 与 `add_sources` 会立刻报找不到文件。文档原文要求「folder names must be matched exactly」（目录名必须完全匹配）。

#### 4.1.4 VHDL 库的两类用法

引入章节还规定了 PSI 库在用户工程里的两种编译方式（与目录结构同样重要，决定了你 `use` 子句怎么写）：

- [doc/old/ch1_introduction/ch1_introduction.md:L26-L33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L26-L33) —— 全部编译进同一库时用 `work.psi_common_xxx`；编译进独立库（推荐名 `psi_lib`）时用 `psi_lib.psi_common_xxx`。

#### 4.1.5 代码实践

**实践目标**：用脚本辅助把三个依赖仓库拉下来，验证工作副本结构。

**操作步骤**：

1. 阅读依赖获取脚本的入口（它从 README 的依赖段解析依赖，再执行 checkout）：[scripts/dependencies.py:L1-L10](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/dependencies.py#L1-L10)。README 说明需先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 包，然后运行 `python dependencies.py -help`（见 [README.md:L67-L73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L67-L73)）。
2. 在仓库根目录执行 `python dependencies.py -help` 查看用法（**待本地验证**：具体子命令以你机器上安装的 `PsiFpgaLibDependencies` 版本输出为准）。
3. 若不想用脚本，也可直接按 4.1.3 的目录树手工 `git clone` 三个仓库到对应位置，或直接克隆聚合仓库 `psi_fpga_all`。

**需要观察的现象**：执行后 `<Root>/VHDL/` 下应同时出现 `psi_common/` 与 `psi_tb/`，`<Root>/TCL/` 下应出现 `PsiSim/`。

**预期结果**：从 `psi_common/sim/` 出发，相对路径 `../../../TCL/PsiSim/PsiSim.tcl` 与 `../../psi_tb/hdl/` 都能解析到真实文件。若提示找不到 `PsiSim.tcl` 或 `psi_tb` 源码，说明目录层级或命名不对。

#### 4.1.6 小练习与答案

**练习 1**：站在 `psi_common/sim/` 目录里，相对路径 `../..` 指向目录树中的哪一级？为什么 `config.tcl` 能用它同时访问 `psi_common` 和 `psi_tb`？

> **答案**：指向 `VHDL/`。因为 `psi_common` 与 `psi_tb` 是 `VHDL/` 下的兄弟目录，所以 `$LibPath/psi_common/...` 和 `$LibPath/psi_tb/...`（`$LibPath="../.."`）都能正确解析。

**练习 2**：为什么 README 说目录名必须「完全匹配」？

> **答案**：因为各仓库脚本之间用相对路径硬编码引用（如 `../../../TCL/PsiSim/PsiSim.tcl`），目录名或层级一旦改动，`source` / `add_sources` 就找不到文件，整个回归无法启动。

---

### 4.2 回归测试脚本 config.tcl

#### 4.2.1 概念说明

`sim/config.tcl` 是整库回归测试的**唯一注册表**。它本身不直接运行仿真，而是用 PsiSim 提供的命令「声明」要编译哪些文件、跑哪些 TB、每个 TB 用哪些 generic 组合。真正「编译 + 运行 + 检查」的动作由入口脚本（`run.tcl` 等）在 `source ./config.tcl` 之后触发。

理解 `config.tcl` 的关键，是把它看成三张清单加一组运行配置：

| 概念 | 命令 | 含义 |
| --- | --- | --- |
| 库源码清单 | `add_sources ... -tag lib` | psi_common 与 psi_tb 的基础包（被所有人依赖） |
| 被测源码清单 | `add_sources "../hdl" {...} -tag src` | 本库所有可综合组件 |
| 测试平台清单 | `add_sources "../testbench" {...} -tag tb` | 所有自校验 TB |
| 单次运行配置 | `create_tb_run` / `tb_run_add_arguments` / `add_tb_run` | 指定一个 TB 用哪几组 generic 跑 |

`-tag` 只是个分组标签，方便框架按组编译/过滤。

#### 4.2.2 核心流程

`config.tcl` 自上而下的执行顺序如下（伪代码）：

```
set LibPath "../.."                 # 库根 = VHDL/
namespace import psi::sim::*        # 引入 PsiSim 命令
add_library psi_common              # 创建名为 psi_common 的 VHDL 库
compile_suppress / run_suppress ...  # 抑制已知无害的告警

add_sources $LibPath {...} -tag lib   # 1. 基础包（array/math/logic + psi_tb 工具包）
add_sources "../hdl" {...} -tag src   # 2. 本库全部组件源码
add_sources "../testbench" {...} -tag tb  # 3. 全部测试平台

# 4. 为每个 TB 声明「跑几遍、每遍用什么 generic」
create_tb_run "psi_common_xxx_tb"
tb_run_add_arguments "-g...=... " "-g...=..."
add_tb_run
... (重复 N 次)
```

注意 `add_sources` 用 `\` 续行把几十个文件列在同一调用里；新增组件或 TB 时，必须把文件名加进对应清单，否则框架不会编译它。

#### 4.2.3 源码精读

**库源码清单**——注意它同时包含 `psi_common` 的三个基础包和 `psi_tb` 的多个工具包，这正是「依赖 psi_tb」在脚本层面的体现：[sim/config.tcl:L22-L31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L22-L31)

```tcl
add_sources $LibPath {
    psi_common/hdl/psi_common_array_pkg.vhd \
    psi_common/hdl/psi_common_math_pkg.vhd \
    psi_common/hdl/psi_common_logic_pkg.vhd \
    psi_tb/hdl/psi_tb_txt_util.vhd \
    psi_tb/hdl/psi_tb_compare_pkg.vhd \
    psi_tb/hdl/psi_tb_activity_pkg.vhd \
    psi_tb/hdl/psi_tb_axi_pkg.vhd \
    psi_tb/hdl/psi_tb_i2c_pkg.vhd \
} -tag lib
```

**被测源码清单**——本库全部可综合组件，`pl_stage` 也在其中：[sim/config.tcl:L34-L51](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L34-L51)（`psi_common_pl_stage.vhd` 出现在第 51 行）。

**测试平台清单**——与源码一一对应的 TB 文件，`pl_stage` 的 TB 在第 108 行：[sim/config.tcl:L108-L108](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L108-L108)

```tcl
psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd \
```

**单个 TB 的运行配置**——这是 `config.tcl` 最需要读懂的部分。以 `pl_stage_tb` 为例：[sim/config.tcl:L319-L323](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L319-L323)

```tcl
create_tb_run "psi_common_pl_stage_tb"
tb_run_add_arguments \
    "-ghandle_rdy_g=true" \
    "-ghandle_rdy_g=false"
add_tb_run
```

含义：

1. `create_tb_run "psi_common_pl_stage_tb"` —— 接下来要为名为 `psi_common_pl_stage_tb` 的顶层 TB 配置运行。
2. `tb_run_add_arguments "-g..." "-g..."` —— 声明**两套** generic 参数组合，框架会把它们当作两次独立运行：
   - 第 1 次：`-ghandle_rdy_g=true`（启用 RDY 反压处理）
   - 第 2 次：`-ghandle_rdy_g=false`（不处理 RDY）
3. `add_tb_run` —— 把上面的配置提交，开始登记下一个 TB。

需要特别说明的是 generic 命名的「中转」关系：TB 顶层声明的 generic 是 `handle_rdy_g`（见 [testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd:L28-L29](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L28-L29)），它在例化被测组件时映射到 `pl_stage` 的 `use_rdy_g`（[testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd:L69-L70](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd#L69-L70) `use_rdy_g => handle_rdy_g`）。而 `pl_stage` 的 `use_rdy_g` 决定是否启用 RDY 反压通路（[hdl/psi_common_pl_stage.vhd:L23-L25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_pl_stage.vhd#L23-L25)）。所以命令行里的 `-ghandle_rdy_g` 实际上最终控制的是被测组件是否处理反压——回归测试用两种取值各跑一遍，覆盖两条代码分支。

**例外与跳过**：某些 TB 因仿真器限制需要跳过特定工具，例如 AXI 相关 TB 用到 Vivado 不支持的无约束 record，故显式 `tb_run_skip Vivado`：[sim/config.tcl:L379-L386](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L379-L386)。

#### 4.2.4 代码实践

**实践目标**：在 `config.tcl` 中找到 `pl_stage` 组件的 testbench 注册条目，并说明它的 generic 参数。这就是本讲规格里指定的练习。

**操作步骤**：

1. 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl)。
2. 用搜索定位 `pl_stage`，你会看到三处出现：
   - 第 51 行：被测源码 `psi_common_pl_stage.vhd`（源码清单）。
   - 第 108 行：测试平台 `psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd`（TB 清单）。
   - 第 319–323 行：运行配置（`create_tb_run` / `tb_run_add_arguments` / `add_tb_run`）。
3. 阅读第 319–323 行，列出 generic 名与取值。

**需要观察的现象**：`pl_stage_tb` 被登记了**两套** generic，分别是 `-ghandle_rdy_g=true` 与 `-ghandle_rdy_g=false`。

**预期结果**：

- generic 名：`handle_rdy_g`，类型为 boolean，取值 `true` / `false`。
- 它是 TB 顶层的 generic，经 `generic map(use_rdy_g => handle_rdy_g)` 传递给 `pl_stage` 的 `use_rdy_g`。
- 语义：`true` 时流水线级启用 RDY（ready）反压处理；`false` 时关闭。回归测试两种都跑，确保两条分支都被覆盖。

#### 4.2.5 小练习与答案

**练习 1**：`config.tcl` 里的三处 `add_sources` 分别用 `-tag lib / src / tb`，它们各自编译什么？如果新增一个组件却忘了登记，会发生什么？

> **答案**：`-tag lib` 编译 psi_common 基础包与 psi_tb 工具包；`-tag src` 编译本库全部可综合组件；`-tag tb` 编译全部测试平台。漏登的文件不会被框架编译，对应的 TB 也就跑不起来（或源码缺失导致编译失败）。

**练习 2**：为什么像 `pl_stage_tb` 这样只把一个 generic 翻转一下就要登记两次运行，而不是一次？

> **答案**：因为这个 generic 切换了被测组件的功能分支（是否处理 RDY 反压），两次运行分别覆盖两条代码路径；只跑一次只能验证其中一条分支，另一条分支的错误会被漏掉。

---

### 4.3 用 Modelsim 运行回归与交互仿真

#### 4.3.1 概念说明

Modelsim（含其衍生版 Questa）是商业仿真器，自带 TCL 控制台。PSI 库为它准备了两个入口：

- **回归入口** `run.tcl`：编译全部源码 → 跑全部 TB → 检查 `###ERROR###`，一键完成。
- **交互入口** `interactive.tcl`：只编译并加载 PsiSim 框架，不自动跑，留给你在控制台手工选择单个 TB 反复编译/调试。

两者都先 `source` 同一个 `config.tcl`，所以「跑什么」完全由 `config.tcl` 决定，入口脚本只决定「怎么跑」。

#### 4.3.2 核心流程

`run.tcl` 的执行流程：

```
source ../../../TCL/PsiSim/PsiSim.tcl   # 1. 载入 PsiSim 框架
namespace import psi::sim::*            # 2. 把框架命令引入当前命名空间
init                                    # 3. 初始化仿真（Modelsim 模式）
source ./config.tcl                     # 4. 载入回归配置（声明源码/TB/运行）
compile_files -all -clean               # 5. 全量干净编译
run_tb -all                             # 6. 跑所有 TB 的所有 generic 组合
run_check_errors "###ERROR###"          # 7. 扫描输出，遇 ###ERROR### 即判失败
```

#### 4.3.3 源码精读

**回归入口**（Modelsim）：[sim/run.tcl:L7-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/run.tcl#L7-L32)。关键三句：

```tcl
init                  # 不带参数 = Modelsim/Questa 模式
...
compile_files -all -clean   # -clean 表示先清掉旧编译产物
...
run_tb -all                 # 跑 config.tcl 里登记的全部 TB 与全部 generic 组合
...
run_check_errors "###ERROR###"  # 把含该串的 report 当作错误
```

`run_check_errors "###ERROR###"` 是整库自校验机制的收口：每个 TB 出错时必须打印以 `###ERROR###` 开头的报文（贡献规范强制要求，见 [doc/old/ch1_introduction/ch1_introduction.md:L79-L84](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L79-L84)），回归脚本据此统一判定成败。

**交互入口**（Modelsim）：[sim/interactive.tcl:L10-L19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/interactive.tcl#L10-L19)

```tcl
init
source ./config.tcl
compile_files -all -clean
```

它到「编译完」就停下，没有 `run_tb`。之后你在控制台用 PsiSim 的选择性命令调试，文档推荐的常用命令见 [doc/old/ch1_introduction/ch1_introduction.md:L55-L67](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L55-L67)：

- `compile_files -contains <字符串>`：只重编文件名含该串的源码。
- `run_tb -contains <字符串>`：只跑名字含该串的 TB。
- `launch_tb -contains <字符串>`：加载 TB 到波形/仿真环境便于交互观察。

#### 4.3.4 代码实践

**实践目标**：在 Modelsim 里跑通回归，并理解输出末尾的判定。

**操作步骤**（参照文档 [doc/old/ch1_introduction/ch1_introduction.md:L39-L44](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L39-L44) 与 [README.md:L83-L87](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L83-L87)）：

1. 打开 Modelsim。
2. 在 TCL 控制台 `cd` 到 `<Root>/VHDL/psi_common/sim`。
3. 执行 `source ./run.tcl`。

**需要观察的现象**：控制台依次打印 `-- Compile`、`-- Run`、`-- Check` 三段；所有 TB 自动执行。

**预期结果**：末尾 `run_check_errors` 不再报告 `###ERROR###`，回归通过。若任一 TB 打印了 `###ERROR###`，回归失败并定位到对应 TB。**待本地验证**：实际能否跑通取决于你是否安装了 Modelsim 与正确的工作副本结构。

#### 4.3.5 小练习与答案

**练习 1**：`run.tcl` 与 `interactive.tcl` 都 `source ./config.tcl`，它们的本质区别是什么？

> **答案**：`run.tcl` 在载入配置后继续 `run_tb -all` 并 `run_check_errors`，是无人值守的批量回归；`interactive.tcl` 只编译到 `compile_files -all -clean` 就停下，把控制权交给用户，便于用 `run_tb -contains` 等命令手工调试单个 TB。

**练习 2**：为什么 PSI 库要求 TB 的错误报文必须以 `###ERROR###` 开头？

> **答案**：因为回归脚本用 `run_check_errors "###ERROR###"` 扫描这个字符串来判定成败；不遵守该约定的报错会被回归「漏检」，看起来通过实则失败。

---

### 4.4 用 GHDL（及 Vivado）运行回归与交互仿真

#### 4.4.1 概念说明

**GHDL** 是开源 VHDL 仿真器，本身是命令行工具、没有 TCL 控制台。PSI 库通过同一个 PsiSim 框架把它包装成与 Modelsim 几乎一致的体验：只要把 `init` 换成 `init -ghdl`，其余命令不变。这意味着同一份 `config.tcl` 可以无缝地在不同仿真器上跑。

此外仓库还提供 **Vivado xsim** 入口 `runVivado.tcl`，用法同构（`init -vivado`），用于在 Xilinx 工具链内跑回归。

GHDL 的前提条件（见 [README.md:L89-L93](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L89-L93) 与 [doc/old/ch1_introduction/ch1_introduction.md:L47-L53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L47-L53)）：

- GHDL 已安装并加入 PATH。
- 已安装一个 TCL 解释器（通常通过 `tclsh` 启动），因为驱动脚本是 TCL 写的。

#### 4.4.2 核心流程

GHDL 回归流程与 Modelsim 完全同构，唯一的差别是初始化参数：

```
source ../../../TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*
init -ghdl                 # ← 关键区别：声明使用 GHDL 后端
source ./config.tcl
compile_files -all -clean
run_tb -all
run_check_errors "###ERROR###"
```

#### 4.4.3 源码精读

**GHDL 回归入口**：[sim/runGhdl.tcl:L7-L32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/runGhdl.tcl#L7-L32)。与 `run.tcl` 逐行对比，只有第 14 行不同：

```tcl
init -ghdl    # run.tcl 里是无参的 init（Modelsim）
```

**GHDL 交互入口**：[sim/interactiveGhdl.tcl:L14-L19](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/interactiveGhdl.tcl#L14-L19)，同样只把 `init` 换成 `init -ghdl`。

**Vivado 回归入口**（补充）：[sim/runVivado.tcl:L13-L14](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/runVivado.tcl#L13-L14)

```tcl
#Initialize Simulation (by exact name because vivado has a command called init)
init -vivado
```

注释点出一个易错点：Vivado 自带一个名为 `init` 的命令，会与 PsiSim 的 `init` 冲突，所以这里必须用 `-vivado` 显式指明后端。

> 仿真器差异的体现：`config.tcl` 里部分 TB 用 `tb_run_skip` 跳过特定仿真器。例如 AXI/I2C 相关 TB 因用到 Vivado 不支持的无约束 record 而跳过 Vivado（[sim/config.tcl:L379-L386](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L379-L386)），I2C TB 还因旧版 GHDL 的 bug 跳过 GHDL（[sim/config.tcl:L424-L432](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl#L424-L432)）。这也说明「同一份 `config.tcl`、不同后端」的取舍。

#### 4.4.4 代码实践

**实践目标**：用 GHDL 跑回归，并与 Modelsim 流程对照，体会 PsiSim 对后端的抽象。

**操作步骤**（参照 [doc/old/ch1_introduction/ch1_introduction.md:L47-L53](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L47-L53)）：

1. 确认 `ghdl --version` 能输出版本号（PATH 已配置）。
2. 启动 TCL 解释器：`tclsh`。
3. 在解释器里 `cd` 到 `<Root>/VHDL/psi_common/sim`。
4. 执行 `source ./runGhdl.tcl`。

**需要观察的现象**：与 Modelsim 一样出现 `-- Compile / -- Run / -- Check` 三段；除了被 `tb_run_skip "GHDL"` 跳过的 TB 外，其余 TB 都被执行。

**预期结果**：`run_check_errors "###ERROR###"` 通过。**待本地验证**：GHDL 版本不同，个别 TB（如 I2C）的跳过行为可能随 GHDL 修复而变化。

#### 4.4.5 小练习与答案

**练习 1**：从 Modelsim 切换到 GHDL 跑回归，需要改 `config.tcl` 吗？为什么？

> **答案**：不需要。`config.tcl` 只声明「编译什么、跑什么」，与后端无关；后端选择由入口脚本的 `init -ghdl`（或 `init` / `init -vivado`）决定。这正是 PsiSim 抽象掉后端差异的好处。

**练习 2**：为什么 `runVivado.tcl` 要专门注释说明「因为 Vivado 有个叫 init 的命令」？

> **答案**：Vivado 自带的 `init` 会与 PsiSim 的 `init` 同名冲突，所以必须用 `init -vivado` 让 PsiSim 精确切到自己的初始化命令，避免误调 Vivado 的 `init`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「新增并跑通一个组件回归」的纸上推演（不要求真的有仿真器，重在理解流程）：

1. **摆目录**：按 4.1.3 的目录树，确认 `<Root>/VHDL/{psi_common,psi_tb}` 与 `<Root>/TCL/PsiSim` 就位，并在 `sim/` 下验证 `../../../TCL/PsiSim/PsiSim.tcl` 可解析。
2. **读注册表**：打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl)，找到 `pl_stage` 在源码清单（L51）、TB 清单（L108）、运行配置（L319–L323）的三处登记，解释它的 generic `handle_rdy_g` 如何经 TB 映射到 `pl_stage` 的 `use_rdy_g`。
3. **选后端跑回归**：
   - 有 Modelsim → `source ./run.tcl`；
   - 只有开源工具 → 在 `tclsh` 里 `source ./runGhdl.tcl`。
4. **判定结果**：看末尾 `run_check_errors "###ERROR###"` 是否报错；若报错，根据报错定位到具体 TB 与 generic 组合。
5. **交互调试**（可选）：若某个 TB 失败，改用 `source ./interactive.tcl`（或 `interactiveGhdl.tcl`），再用 `run_tb -contains pl_stage` 单独跑它反复调试。

完成上述推演后，你应当能在不查文档的情况下，解释「一个 PSI 组件从源码到回归通过」的完整工具链路径。

## 6. 本讲小结

- `psi_common` 仿真依赖两个外部仓库：TCL 框架 **PsiSim**（≥ 2.1.0）与 VHDL 仿真工具包 **psi_tb**（≥ 3.0.0）；二者必须与 `psi_common` 按固定相对路径摆成工作副本结构（推荐根目录名 `psi_lib`）。
- 所有相对路径都从 `sim/` 出发：`../hdl` 指向本库源码，`../..`（`$LibPath`）指向 `VHDL/` 下的兄弟仓库，`../../../TCL/PsiSim/PsiSim.tcl` 指向框架。
- [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/sim/config.tcl) 是整库回归的注册表，用 `add_sources -tag lib/src/tb` 分组声明编译目标，用 `create_tb_run`/`tb_run_add_arguments`/`add_tb_run` 声明每个 TB 的 generic 运行组合。
- Modelsim 回归用 `source ./run.tcl`，交互用 `source ./interactive.tcl`；两者都 `source ./config.tcl`，差别只在「跑不跑、检查不检查」。
- GHDL/Vivado 与 Modelsim 流程同构，仅入口脚本的 `init` 后端参数不同（`init -ghdl` / `init -vivado`），`config.tcl` 完全复用；个别 TB 用 `tb_run_skip` 按仿真器能力跳过。
- `pl_stage_tb` 在 `config.tcl` 第 319–323 行登记，用 `-ghandle_rdy_g=true/false` 各跑一遍，该 generic 经 TB 映射到 `pl_stage` 的 `use_rdy_g`，覆盖「是否处理 RDY 反压」两条分支。

## 7. 下一步学习建议

- **接着学编码规范与握手**：下一讲 [u1-l4 编码规范、AXI-S 握手与 TDM 约定](u1-l4-coding-conventions-handshaking.md) 会解释本讲反复提到的 VLD/RDY 反压、`###ERROR###` 自检约定背后的握手语义，是理解 `pl_stage` 为何要测两种 `use_rdy_g` 的前置。
- **读懂一个真实 TB**：进入 [testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd)，观察它如何用 `###ERROR###` 报告、如何用多进程协调停止——这是 U11「编写自校验测试平台」的预热。
- **想跑真仿真**：先按 4.1 把工作副本结构搭好，安装 GHDL（最轻量），在 `sim/` 下执行 `source ./runGhdl.tcl` 亲自跑一次回归。
