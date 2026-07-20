# 仓库结构与目录组织

## 1. 本讲目标

学完本讲后，你应该能够：

- 画出 `psi_common` 仓库的顶层目录树，并说出每个目录里放的是什么。
- 理解 `hdl/`（源码）与 `testbench/`（仿真测试）之间严格的命名对应关系，能根据一个组件名立刻找到它的源码和测试。
- 知道文档（`doc/`）、脚本（`scripts/`）、代码生成器（`generators/`）、仿真脚本（`sim/`）和 IDE 配置（`sigasi/`）分别放在哪里、各起什么作用。
- 看懂「工作副本结构（Working Copy Structure）」这个概念——为什么单独克隆本仓库就够用，而要跑仿真时却必须把它和 `psi_tb` 摆成兄弟目录。

本讲只读文档与目录结构，不展开任何 `.vhd` 源码细节（那是后续讲义的任务）。

## 2. 前置知识

在进入目录之前，先用三段话建立直觉（这些概念在 [u1-l1 项目概览与定位](u1-l1-project-overview.md) 已建立，这里只做最小复述）：

- **VHDL 与库（library）**：VHDL 是一种硬件描述语言，一段 VHDL 代码会被「编译」进某个 VHDL *library*。PSI 的所有库（包括 `psi_common`）要求把全部文件编译进**同一个** library 里，引用时写作 `work.psi_common_pl_stage` 或 `psi_lib.psi_common_pl_stage`。
- **Entity / Package / Testbench**：`entity` 描述一个硬件模块对外暴露的端口；`package` 是一组可复用的类型、常量与函数；`testbench`（测试平台，简称 TB）是只用于仿真、用来驱动并检查上面两者的顶层代码，不会真正综合成电路。
- **一个文件一个单元**：PSI 库的硬性约定是「每个 entity 或 package 放在一个独立的 `.vhd` 文件里」，文件名即单元名。理解了这条，目录结构就变成了一张可预测的索引表。

> 命名小提示：源码单元一律以 `psi_common_` 为前缀；测试目录与测试文件在此基础上加 `_tb` 后缀。本讲会反复用到这条规则。

## 3. 本讲源码地图

本讲引用的「源码」其实是文档与目录清单，它们是理解仓库布局的权威依据：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md) | 项目入口：说明库的收录范围、依赖与目录结构要求、仿真与测试方式。 |
| [doc/README.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md) | 组件总索引：把全库 60+ 组件按类别列成表格，并给出源码链接。 |
| [doc/old/ch1_introduction/ch1_introduction.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md) | 旧版手册第 1 章：讲工作副本结构、VHDL library 用法、仿真运行与贡献规范。 |
| `hdl/` | 全部可综合源码（entity + package），每文件一个单元。 |
| `testbench/` | 全部自校验测试平台，每个组件一个子目录。 |
| `sim/` `doc/` `scripts/` `generators/` `sigasi/` | 仿真、文档、重构脚本、代码生成器、IDE 工程等辅助内容。 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层目录与工作副本结构

#### 4.1.1 概念说明

仓库根目录下你能看到这些条目：

```
psi_common/
├── hdl/            # 可综合源码（entity/package）
├── testbench/      # 自校验测试平台（每组件一个子目录）
├── sim/            # 仿真回归脚本（Modelsim / GHDL / Vivado）
├── doc/            # 文档：组件说明、旧版手册章节、演示文稿
├── scripts/        # Python 工具：依赖检出、CI 流程、库重构
├── generators/     # Python 代码生成器：为特定位宽生成 .vhd 实例
├── sigasi/         # Sigasi Studio IDE 的工程文件
├── README.md  Changelog.md  License.txt  LGPL2_1.txt
└── .gitlab-ci.yml  .gitignore
```

一个关键区分：**「仓库结构」**和**「工作副本结构（Working Copy Structure）」**是两件事。

- **仓库结构**：就是上面这棵树，单独克隆 `psi_common` 就完整拥有。
- **工作副本结构**：当你想**运行仿真或修改库**时，`psi_common` 会以相对路径引用兄弟仓库 `psi_tb`（测试工具包）和 `PsiSim`（TCL 仿真框架），因此必须把它们摆成固定的兄弟目录。

#### 4.1.2 核心流程

判断「我需要哪种结构」的决策流程：

```text
我只是想读源码 / 抄几个组件用？
   └─ 是 ─> 单独克隆 psi_common 即可，无目录要求
   └─ 否（要跑回归仿真 / 改库）─>
        按 <Root>/VHDL/{PsiSim, psi_common, psi_tb} 摆放
        （推荐把 <Root> 命名为 psi_lib）
```

README 里有一个被脚本解析的「Dependencies」段落，明确写出所需的兄弟仓库与目录结构要求。

#### 4.1.3 源码精读

README 的「Dependencies」小节给出工作副本的目录结构要求与依赖仓库（注意开头注释提醒：这段格式被脚本解析，勿改）：

[README.md:52-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L52-L65) —— 声明所需文件夹结构必须精确匹配，并列出依赖：TCL 侧的 PsiSim、VHDL 侧的 psi_common 自身与 psi_tb（≥3.0.0）。

[README.md:67-73](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L67-L73) —— 依赖也可通过 `scripts/dependencies.py` 脚本自动检出。

旧版手册用一节专门讲工作副本结构，并附了一张目录树插图：

[doc/old/ch1_introduction/ch1_introduction.md:8-24](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L8-L24) —— 「Working Copy Structure」：说明仅使用组件时无需特殊结构、但跑仿真/改库时必须按图示相对路径摆放兄弟仓库，并建议把根目录命名为 `psi_lib`。

#### 4.1.4 代码实践

1. **实践目标**：分清「仓库结构」与「工作副本结构」。
2. **操作步骤**：
   - 在仓库根目录执行 `ls`，确认看到 `hdl testbench sim doc scripts generators sigasi` 七个目录。
   - 打开 [README.md:52-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L52-L65)，阅读依赖列表。
3. **需要观察的现象**：仓库自身**不包含** `psi_tb` 或 `PsiSim`，它们是兄弟仓库。
4. **预期结果**：你能在 README 里看到「folder names must be matched exactly」的字样，从而理解为什么摆错目录仿真就会失败。

#### 4.1.5 小练习与答案

- **练习 1**：只想把 `psi_common_sync_fifo` 抄进自己项目，需要克隆 `psi_tb` 吗？
  - **答案**：不一定。源码使用层面单独克隆 `psi_common` 即可；只有要跑它自带的回归测试时才需要 `psi_tb`。
- **练习 2**：为什么 README 把目录结构要求放在一段被注释标记为「DO NOT CHANGE FORMAT」的区域内？
  - **答案**：因为该段落会被 `scripts/dependencies.py` 等脚本自动解析来解析依赖，格式变化会破坏脚本。

---

### 4.2 hdl 源码目录

#### 4.2.1 概念说明

`hdl/` 是整个库的**可综合源码**总集合——也就是最终会被综合成 FPGA/ASIC 电路的代码。它是一个**扁平目录**：没有任何子目录，所有 `.vhd` 文件平铺在一起。

`doc/README.md` 把 `hdl/` 里的组件分成九大类：Memory、FIFO、CDC、Conversions、TDM、Arbiters、Interfaces、miscellaneous（杂项）、Packages。这九类正是后续学习单元（U3–U10）的划分依据。

#### 4.2.2 核心流程

从「一个想法」到「`hdl/` 里多一个文件」的入库流程：

```text
新组件是否符合收录标准？
  (项目无关、完全 generic 化、可复用)
        └─ 是 ─> 新建 hdl/psi_common_<name>.vhd
                  （一个文件只放一个 entity 或 package）
        └─ 否 ─> 不入库（项目相关代码或信号处理代码归 psi_fix）
```

入库的硬性规则来自 README 的「What belongs / What does not belong」两节，以及「一个文件一个单元」的约定。

#### 4.2.3 源码精读

[README.md:26-37](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L26-L37) —— 收录范围：库只收「不偏应用、可复用」的通用 VHDL，且**每个 package 或 entity 单独占一个 `.vhd` 文件**（见第 31 行）；并列出属于本库的典型例子（CDC、FIFO、厂商无关 RAM、扩展语言的 package）。

[README.md:39-43](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L39-L43) —— 反面清单：项目相关代码、应归 `psi_fix` 的信号处理代码、无法完全参数化的代码都不收。

[doc/README.md:36-148](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L36-L148) —— 组件总索引表：每一行给出「组件名 → 源码文件 `../hdl/psi_common_xxx.vhd` → 说明文档 `files/psi_common_xxx.md`」的三角关系，是浏览 `hdl/` 的最佳地图。

> 经实测（见第 5 节综合实践）：`hdl/` 目录下共有 **61 个 `.vhd` 文件**，全部为扁平存放。

#### 4.2.4 代码实践

1. **实践目标**：验证「一个文件一个单元 + 命名前缀」的约定。
2. **操作步骤**：
   - 用文件浏览器或 `ls hdl/` 查看源码文件名。
   - 随机挑 3 个文件，例如 `psi_common_sync_fifo.vhd`、`psi_common_math_pkg.vhd`、`psi_common_pl_stage.vhd`。
   - 打开每个文件**顶部第一行**，确认 `entity` 或 `package` 的名字与文件名一致。
3. **需要观察的现象**：文件名 == `entity`/`package` 名；`*_pkg.vhd` 是 package，其余大多是 entity。
4. **预期结果**：例如 `psi_common_math_pkg.vhd` 内首行声明的是 `package psi_common_math_pkg is`，名字逐字相同。

#### 4.2.5 小练习与答案

- **练习 1**：`hdl/psi_common_axi_pkg.vhd` 是 entity 还是 package？依据是什么？
  - **答案**：是 package。依据是文件名以 `_pkg` 结尾，且它承载 AXI 接口的 record 类型定义（属「扩展语言」类，符合收录标准）。
- **练习 2**：一个文件里同时放两个 entity 会被接受吗？
  - **答案**：不会。README 第 31 行明确建议「one .vhd file per Package or Entity」。

---

### 4.3 testbench 目录与命名对应

#### 4.3.1 概念说明

`testbench/` 存放**自校验测试平台**。与 `hdl/` 的扁平结构不同，`testbench/` 是**一个组件一个子目录**：每个子目录里至少有一个与组件同名的 `_tb.vhd` 顶层测试文件，复杂的测试还会拆成多个 `*_case_*.vhd` 用例文件和 `*_pkg.vhd` 公共包。

PSI 库强制要求：「凡是非平凡的组件，必须配自校验 TB」。这意味着 `hdl/` 与 `testbench/` 之间存在**几乎一一对应**的命名映射，这是本讲最重要的可操作结论。

#### 4.3.2 核心流程

根据源码文件名定位测试的查表流程：

```text
已知源码：hdl/psi_common_<N>.vhd
        │
        ├─ 测试目录：testbench/psi_common_<N>_tb/
        └─ 测试顶层：testbench/psi_common_<N>_tb/psi_common_<N>_tb.vhd

例：hdl/psi_common_pl_stage.vhd
   ─> testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd
```

少数组件**没有**专属 TB 目录，原因有两类：

- **底层被复用组件**：如各 RAM（`sdp_ram`/`sp_ram_be`/`tdp_ram`）——它们通过上层 FIFO、乒乓缓冲的测试被间接覆盖。
- **辅助/工具型单元**：如 `dont_opt`（防综合优化的占位）、部分 package（`math_pkg`/`array_pkg`/`axi_pkg`，其中 `logic_pkg` 有独立 TB）。

#### 4.3.3 源码精读

[README.md:75-81](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L75-L81) —— 「Simulations and Testbenches」：要求非平凡组件必须配自校验 TB，且**新 TB 必须登记到回归脚本 `sim/config.tcl`**。

[doc/old/ch1_introduction/ch1_introduction.md:79-91](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L79-L91) —— 贡献规范：TB 须覆盖全部功能、运行完自动停止、报错信息以 `###ERROR###` 开头（回归脚本据此检索）；并再次强调新 TB 要加入 `config.tcl` 并验证回归通过。

实例（一个最简 TB 目录，只含一个文件）：

[testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_pl_stage_tb/psi_common_pl_stage_tb.vhd) —— `pl_stage` 组件对应的自校验测试平台顶层，目录名与文件名都严格遵循 `<源码名>_tb` 规则。

实例（一个复杂 TB 目录，拆成多个用例）：

[testbench/psi_common_axi_master_simple_tb/](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_axi_master_simple_tb/) —— 该目录除顶层 `psi_common_axi_master_simple_tb.vhd` 外，还包含 `..._pkg.vhd`（公共过程包）与多个 `..._case_*.vhd`（按场景拆分的用例，如 `case_simple_tf`、`case_axi_hs`、`case_split` 等）。

> 经实测（见第 5 节综合实践）：`testbench/` 下共 **52 个子目录**、**67 个 `.vhd` 文件**；61 个源码单元中约有 **9 个**没有专属 TB 目录。

#### 4.3.4 代码实践

1. **实践目标**：熟练使用「源码 → TB」命名映射。
2. **操作步骤**：
   - 任选 3 个源码组件：`psi_common_async_fifo`、`psi_common_spi_master`、`psi_common_delay_cfg`。
   - 不看答案，先写出你预测的测试目录与测试文件全路径。
   - 然后在 `testbench/` 下核对。
3. **需要观察的现象**：预测路径与实际路径逐字一致。
4. **预期结果**：
   - `testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd`（单文件）
   - `testbench/psi_common_spi_master_tb/psi_common_spi_master_tb.vhd`
   - `testbench/psi_common_delay_cfg_tb/psi_common_delay_cfg_tb.vhd`
5. **进阶观察**：`psi_common_axi_master_simple_tb` 目录下有几个 `*_case_*.vhd`？这能让你直观感受「简单组件单文件、复杂组件多用例」的组织差异。

#### 4.3.5 小练习与答案

- **练习 1**：`hdl/psi_common_sdp_ram.vhd` 为什么在 `testbench/` 下找不到 `psi_common_sdp_ram_tb`？
  - **答案**：它是底层存储原语，被上层 `sync_fifo`/`async_fifo`/`delay` 等组件当作内部 RAM 复用，其功能通过这些上层组件的 TB 被间接验证。
- **练习 2**：TB 报错时为什么必须以 `###ERROR###` 开头？
  - **答案**：回归脚本（`sim/run.tcl`/`runGhdl.tcl`）靠检索该字符串来判断 TB 是否失败并汇总结果。

---

### 4.4 辅助目录：doc / scripts / generators / sim / sigasi

#### 4.4.1 概念说明

除了源码与测试，仓库还有四个「辅助目录」支撑文档、工具链与开发流程：

| 目录 | 作用 | 关键内容 |
| --- | --- | --- |
| `doc/` | 文档 | `README.md` 组件索引、`files/*.md` 每组件说明、`old/chN_*` 旧版手册章节、`presentation/` 演示稿、`ghdl/GHDL.md` |
| `scripts/` | Python 工具 | `dependencies.py`（依赖检出）、`ciFlow.py`（CI 流程）、`refactoring/`（库级重构与 v2→v3 迁移） |
| `generators/` | 代码生成器 | 为 `simple_cc`/`status_cc`/`par_tdm`/`tdm_par` 按位宽生成实例的 `.py` + `snippets/*.vhd` 模板 + `examples/*.bat` |
| `sim/` | 仿真脚本 | `config.tcl`（TB 注册表）、`run.tcl`/`runGhdl.tcl`（回归）、`interactive.tcl`（交互）、`runVivado.tcl` |
| `sigasi/` | IDE 工程 | Sigasi Studio 的 `.project`、`.library_mapping.xml`、`.settings/`（VHDL 版本偏好等） |

一个值得注意的对称美：`doc/files/` 下每个组件也有一份 `<组件名>.md`，于是形成了「`hdl/<N>.vhd` ↔ `testbench/<N>_tb/` ↔ `doc/files/<N>.md`」三位一体的索引，而 `doc/README.md` 的表格正是这三者的总目录。

#### 4.4.2 核心流程

各类辅助文件在开发循环中的角色：

```text
写代码 ─> hdl/<N>.vhd
写测试 ─> testbench/<N>_tb/  ──(登记)──> sim/config.tcl ─> sim/run.tcl 跑回归
写文档 ─> doc/files/<N>.md   （并被 doc/README.md 索引）
位宽特化─> generators/<N>_X.py + snippets/<N>_X.vhd ─> 生成实例 .vhd
库升级 ─> scripts/refactoring/hdlrefactor.py + migration_from_v2_to_v3_db.json
```

#### 4.4.3 源码精读

[doc/README.md:43-46](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/README.md#L43-L46) —— Memory 类表格示例：每行同时给出源码链接 `../hdl/psi_common_sdp_ram.vhd` 与说明文档链接 `files/psi_common_sdp_ram.md`，体现「源码 ↔ 文档」的并排索引。

[doc/old/ch1_introduction/ch1_introduction.md:26-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/old/ch1_introduction/ch1_introduction.md#L26-L33) —— 「VHDL Libraries」：说明 PSI 库要求所有文件编译进同一 VHDL library，引用时用 `work.psi_common_xxx` 或 `psi_lib.psi_common_xxx`，这是理解 `sim/` 脚本编译顺序的前提。

[README.md:75-93](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/README.md#L75-L93) —— 回归测试运行方式：在 `sim/` 目录下 `source ./run.tcl`（Modelsim）或 `source ./runGhdl.tcl`（GHDL），自动跑完所有登记的 TB 并汇总结果（`sim/` 的详细用法见 [u1-l3 依赖管理与仿真运行](u1-l3-dependencies-and-simulation.md)）。

`scripts/refactoring/` 目录下的工具（库级重构与 v2→v3 迁移，详见 [u11-l3](u11-l3-refactoring-scripts.md)）：

[scripts/refactoring/](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/scripts/refactoring/) —— 含 `parse_library.py`（解析库结构）、`hdlrefactor.py`（应用重构规则）、`migration_from_v2_to_v3_db.json`（v2→v3 命名迁移映射库）。

`generators/` 的模板与示例（详见 [u11-l2](u11-l2-code-generators.md)）：

[generators/psi_common_simple_cc_X.py](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/psi_common_simple_cc_X.py) —— 代码生成器脚本，配合 [generators/snippets/psi_common_simple_cc_X.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/snippets/psi_common_simple_cc_X.vhd) 模板与 [generators/examples/psi_common_simple_cc.bat](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/generators/examples/psi_common_simple_cc.bat) 调用示例使用。

#### 4.4.4 代码实践

1. **实践目标**：走通「源码 ↔ 文档 ↔ 测试」三位一体索引。
2. **操作步骤**：
   - 选定组件 `psi_common_async_fifo`。
   - 分别打开 `hdl/psi_common_async_fifo.vhd`、`testbench/psi_common_async_fifo_tb/psi_common_async_fifo_tb.vhd`、`doc/files/psi_common_async_fifo.md`。
   - 在 `doc/README.md` 中找到登记该组件的那一行（FIFO 类表格）。
3. **需要观察的现象**：四个位置用的是同一个组件名，彼此交叉链接。
4. **预期结果**：你能在 `doc/files/psi_common_async_fifo.md` 里读到该 FIFO 的端口表与设计说明，与源码端口一一吻合。
5. **进阶**：浏览 `sigasi/.project` 与 `sigasi/.library_mapping.xml`，了解 Sigasi IDE 如何把 `hdl/` 映射为 VHDL library（不必深究 XML 细节）。

#### 4.4.5 小练习与答案

- **练习 1**：`sim/config.tcl` 在仓库里扮演什么角色？
  - **答案**：它是回归测试的 TB 注册表——所有新 TB 必须在此登记，否则 `run.tcl` 不会跑它（详见 u1-l3）。
- **练习 2**：`generators/` 与 `scripts/refactoring/` 都是 Python，它们的用途有何不同？
  - **答案**：`generators/` 面向**使用**——为某组件按特定位宽生成可综合实例；`scripts/refactoring/` 面向**库维护**——批量重命名、迁移旧代码到新版本。

---

## 5. 综合实践

把本讲全部知识点串起来，完成下面这个「仓库测绘」小任务。

**任务**：为 `psi_common` 仓库绘制一张「组件索引速查表」，并验证源码与测试的对应关系。

**操作步骤**：

1. **统计源码规模**：数 `hdl/` 下的 `.vhd` 文件数量，并按 `doc/README.md` 的九大类各挑 1 个代表组件。
2. **验证命名映射**：从 `hdl/` 任选 3 个组件（建议 `psi_common_pl_stage`、`psi_common_sync_fifo`、`psi_common_par_tdm`），写出并核对它们的 `testbench/` 子目录与顶层 TB 文件路径。
3. **找例外**：找出 3 个**没有**专属 TB 目录的源码单元，并解释原因（提示：从 RAM、package、`dont_opt` 中找）。
4. **走文档索引**：对这 3 个组件，分别在 `doc/files/` 下找到说明文档，在 `doc/README.md` 找到登记行。
5. **定位工具链**：指出「新 TB 要登记到哪里」「位宽特化实例由谁生成」「v2→v3 迁移用哪个脚本」三个答案所在的目录。

**预期结果（供你核对）**：

- `hdl/` 共 **61** 个 `.vhd` 文件。
- `testbench/` 共 **52** 个子目录；所选 3 个组件的 TB 路径形如 `testbench/psi_common_<N>_tb/psi_common_<N>_tb.vhd`，逐字吻合。
- 没有专属 TB 的典型单元例如 `psi_common_sdp_ram`、`psi_common_math_pkg`、`psi_common_dont_opt`（原因见 4.3.2）。
- 登记新 TB → `sim/config.tcl`；位宽特化 → `generators/`；v2→v3 迁移 → `scripts/refactoring/`。

> 若你本地 clone 的版本与本讲 HEAD（`98c2fcc`）不同，文件数量可能略有出入，请以你本地实测为准。

## 6. 本讲小结

- 仓库顶层分为 `hdl/`（源码）、`testbench/`（测试）、`sim/`（仿真脚本）、`doc/`（文档）、`scripts/`（工具）、`generators/`（代码生成器）、`sigasi/`（IDE 工程）七大目录。
- **仓库结构**与**工作副本结构**不同：前者单独可用，后者要求把 `psi_common` 与 `psi_tb`/`PsiSim` 按固定相对路径摆成兄弟目录才能跑仿真。
- `hdl/` 是扁平目录，遵循「一个文件一个 entity/package」、统一 `psi_common_` 前缀；当前共 61 个 `.vhd`。
- `testbench/` 一个组件一个子目录，与 `hdl/` 几乎一一对应，命名规则为 `<源码名>_tb`；当前 52 个子目录，约 9 个源码单元无专属 TB。
- `doc/README.md` 的组件表是全库总索引，把「源码 ↔ 测试 ↔ 说明文档」三者串成一张表。
- 新 TB 须登记到 `sim/config.tcl`；位宽特化靠 `generators/`；库重构与版本迁移靠 `scripts/refactoring/`。

## 7. 下一步学习建议

- 接下来读 [u1-l3 依赖管理与仿真运行](u1-l3-dependencies-and-simulation.md)，深入 `sim/config.tcl`、`run.tcl`、`runGhdl.tcl`，亲手把仓库跑起来。
- 想了解命名之外的编码规范（`_i/_o/_io` 后缀、AXI-S 握手、TDM 约定），读 [u1-l4 编码规范、AXI-S 握手与 TDM 约定](u1-l4-coding-conventions-handshaking.md)。
- 进入源码前，建议先通读 `doc/README.md` 的九大类组件表，按「Memory → FIFO → CDC → …」的顺序建立全局地图，再按本手册 U3 起的单元逐层深入。
