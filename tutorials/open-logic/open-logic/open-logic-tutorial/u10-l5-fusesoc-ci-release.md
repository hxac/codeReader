# 厂商集成、FuseSoC 与 CI/发布流程

## 1. 本讲目标

本讲是第 10 单元（验证、工程化与 CI）的最后一讲，也是整套手册的收尾。前面几讲我们分别看过了 VUnit 测试台（u10-l1）、仿真运行器（u10-l2）、覆盖率与质量徽章（u10-l3）以及代码检查与综合测试（u10-l4）。本讲把这些散落的工程化能力**收束成一条完整的「写代码 → 进工具 → 过 CI → 发版本」流水线**。

学完后你应当能够：

- 说清 Open Logic 如何用一组厂商导入脚本把全库源码塞进各厂商工具，以及为什么只有 AMD（Vivado）能自动套上时序约束；
- 读懂一份 FuseSoC `.core` 文件，理解 `UpdateCoreFiles.py` 如何用 Jinja2 模板批量维护它们；
- 画出 Open Logic 的 CI 工作流矩阵——哪些检查跑在免费的 GitHub runner 上、哪些跑在付费的 AWS runner 上、各自的触发时机是什么；
- 描述一次贡献从 fork 到合入、再到发布 `CompleteSources.zip` 的完整路径。

## 2. 前置知识

本讲假设你已经读过：

- **u1-l3**：知道各厂商有 `tools/<厂商>/import_sources.*` 脚本、单库编译进库名 `olo`、`--recursive` 克隆子模块、跨语言实例化注意事项。
- **u4-l1**：知道跨时钟域（CDC）路径必须「电路 + 约束」配套，AMD 靠 scoped constraints 自动覆盖。
- **u10-l2 / u10-l4**：知道 `sim/run.py`、`codegen` 前置生成、VSG lint、`inference_test` 综合测试的存在。

本讲新引入的术语：

| 术语 | 含义 |
| --- | --- |
| **FuseSoC** | HDL 界的包管理器 + 构建系统，用 `.core` 文件（CAPI=2 格式）描述一个 IP 的源文件、依赖与目标工具。 |
| **core 文件** | FuseSoC 的清单文件，声明名字、文件集（fileset）、依赖、target 与 provider。 |
| **scoped constraints** | AMD Vivado 的一种约束机制：约束用 `read_xdc -ref <实体名>` 绑定到具体实体实例，工具自动按实例作用域应用，无需用户手工连约束文件。 |
| **GitHub Runner / AWS Runner** | CI 的两种执行环境。前者免费（`ubuntu-latest`）但只装了开源工具；后者是项目自建的 EC2（`[self-hosted, aws]`），装了 Questa/Vivado/Quartus 等商业工具，跑一次要花钱。 |
| **develop / main** | 两条长期分支。`develop` 是开发主线、贡献 PR 的目标；`main` 是稳定发布线，只接受经过完整 CI 的合并。 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [tools/vivado/import_sources.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl) | Vivado 导入脚本：加源、设库 `olo`、设 VHDL-2008、触发约束加载。 |
| [tools/vivado/all_constraints_amd.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/all_constraints_amd.tcl) | 聚合脚本：遍历 base/intf 区域，分别 source 各自的约束聚合脚本。 |
| [src/base/tcl/olo_base_constraints_amd.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_constraints_amd.tcl) | base 区域的 AMD 约束聚合脚本，用 `read_xdc -ref` 为每个 CDC/RAM 实体挂 scoped 约束。 |
| [tools/quartus/import_sources.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/quartus/import_sources.tcl) | Quartus 导入脚本，作为「无 scoped constraints」厂商的对照样本。 |
| [tools/fusesoc/UpdateCoreFiles.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/UpdateCoreFiles.py) | 用 Jinja2 批量生成 dev/stable 两套 `.core` 文件的维护脚本。 |
| [tools/fusesoc/core.template](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/core.template) | 区域 core 文件的 Jinja2 模板。 |
| [tools/fusesoc/stable/olo_base.core](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_base.core) | 已生成的 stable core 样本（base 区域）。 |
| [tools/fusesoc/stable/olo_fix.core](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_fix.core) | fix 区域 core，演示对 base + en_cl_fix 的依赖声明。 |
| [doc/CI-Workflows.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md) | CI 工作流的官方总览表（触发事件 × 运行环境）。 |
| [.github/workflows/hdl_check.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml) | 免费的 HDL-Check 工作流（GHDL/NVC 仿真 + lint + 综合 YAML 覆盖检查）。 |
| [.github/workflows/fusesoc.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/fusesoc.yml) | FuseSoC 测试工作流（AWS runner）。 |
| [.github/workflows/coverage_sim.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml) | 覆盖率仿真工作流（AWS runner，95% 门禁）。 |
| [.github/workflows/release.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/release.yml) | 发布工作流：打 release 时打包含子模块的 `CompleteSources.zip`。 |
| [Contributing.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Contributing.md) | 贡献流程、分支约定与 CLA 要求。 |

## 4. 核心概念与源码讲解

### 4.1 厂商导入脚本与 AMD scoped 约束自动应用

#### 4.1.1 概念说明

Open Logic 是「纯 VHDL、厂商无关」的，但用户的工程最终要落到某个厂商工具里（Vivado、Quartus、Libero、Efinity、Gowin、Yosys……）。如果每换一个工具都要手工把上百个 `.vhd` 文件加进去、再设库、再设 VHDL-2008、再挂约束，体验会很差——这违背了 u1-l1 讲过的 **Ease of Use** 哲学。

所以每个厂商在 `tools/<厂商>/` 下都有一个导入脚本，统一完成四件事：

1. 由脚本自身路径反推出仓库根目录（脚本可被复制到任意工程里运行）；
2. 把四个区域（base/axi/intf/fix）加 `3rdParty/en_cl_fix` 的全部源文件加进工程；
3. 把它们登记到同一个名为 `olo` 的 VHDL 库，并设为 VHDL-2008；
4. **如果该厂商支持，自动套上时序约束**。

第 4 步是关键差异点：跨时钟域（u4-l1）、RAM 读时序等都需要约束才能正确工作。**只有 AMD（Vivado）实现了约束的自动应用**，因为它独有的 **scoped constraints** 机制能让约束随实体实例自动生效；其他厂商必须由用户手工添加约束文件。

#### 4.1.2 核心流程

Vivado 导入的执行流程：

```text
import_sources.tcl
  ├─ 定位仓库根 oloRoot = 脚本路径/../..
  ├─ foreach area in {base axi intf fix}:
  │     add_files(src/<area>/vhdl)
  │     设库 = olo, 设类型 = VHDL 2008
  ├─ add_files(3rdParty/en_cl_fix/hdl) → 同样进库 olo
  └─ source all_constraints_amd.tcl          ← 只在 Vivado 走这条分支
        └─ foreach area in {base intf}:       ← axi/fix 无约束
              source src/<area>/tcl/olo_<area>_constraints_amd.tcl
                  └─ read_xdc -ref <实体> <实体>.tcl -unmanaged   ← scoped 约束
```

对照之下，Quartus 的 `import_sources.tcl` 只做前三步（加文件、设库 `olo`、设 VHDL-2008），**没有约束分支**——用户需要自己去 `src/<area>/tcl/` 找 `.sdc` 手工挂。这就是「AMD 独享自动约束」在脚本层面的体现。

#### 4.1.3 源码精读

Vivado 导入脚本的源码加入与设库逻辑：

[tools/vivado/import_sources.tcl:39-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L39-L53) — 由脚本路径定位仓库根，遍历四区域把 `olo_<area>_*` 文件加进工程、统一设库 `olo` 与 `VHDL 2008`，再单独把 `en_cl_fix_*` 加进来。注意它用通配 `*olo_$area\_*` 精确命中本库文件，不会误伤用户已有文件。

约束聚合的入口在同一脚本末尾：

[tools/vivado/import_sources.tcl:55-69](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl#L55-L69) — 先算出工程目录、在工程下建一个 `open_logic` 子目录（Vivado 执行 TCL 时会把它拷到 `impl_1` 目录，故约束用相对路径引用），最后 `source all_constraints_amd.tcl`。

[tools/vivado/all_constraints_amd.tcl:9-11](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/all_constraints_amd.tcl#L9-L11) — 总聚合脚本只遍历 `base` 和 `intf` 两个有约束的区域（`axi`/`fix` 无约束），分别 source 它们各自的 `olo_<area>_constraints_amd.tcl`。

真正「scoped」的魔法在区域聚合脚本里，以 base 为例：

[src/base/tcl/olo_base_constraints_amd.tcl:8-24](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_constraints_amd.tcl#L8-L24) — 对每个需要约束的底层实体（`cc_reset`/`cc_bits`/`cc_simple`/`reset_gen`/`ram_sdp`）执行 `read_xdc -ref <实体名> <实体>.tcl -unmanaged`。`-ref` 把约束绑定到该实体名，Vivado 会对工程里每个该实体的实例自动套用，用户无需手工连线；随后两段 `set_property` 把这些约束标为「仅用于实现、不用于综合」并设 `PROCESSING_ORDER LATE`，确保它们在用户自己的约束之后才生效、不冲突。

作为对照，Quartus 脚本没有这层约束自动化：

[tools/quartus/import_sources.tcl:50-63](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/quartus/import_sources.tcl#L50-L63) — 它用 `glob` 枚举每个区域的 `.vhd`，逐个 `set_global_assignment -name VHDL_FILE ... -library olo` 加进 Quartus 工程。到这里就结束了，没有任何 `read_sdc` 之类的约束分支。

> 小贴士：脚本要可移植，就不能写死绝对路径。所有厂商脚本都用「脚本所在目录往上推两级 = 仓库根」这个约定（Vivado 用 `[file dirname [info script]]/../..`，Quartus 用 `$fileLoc/../..`），所以你可以把整个仓库丢进工程目录的任意子层都能正常导入。

#### 4.1.4 代码实践

**实践目标**：亲手对比「有自动约束」与「无自动约束」两种厂商导入脚本的差异。

**操作步骤**：

1. 打开 [tools/vivado/import_sources.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/vivado/import_sources.tcl) 与 [tools/quartus/import_sources.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/quartus/import_sources.tcl)。
2. 列出两者各自做了哪几件事（加源 / 设库 / 设语言版本 / 加约束）。
3. 打开 [src/base/tcl/olo_base_constraints_amd.tcl](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/tcl/olo_base_constraints_amd.tcl)，数一数 base 区域一共有几个底层实体被挂上了 scoped 约束。

**需要观察的现象**：Vivado 脚本结尾有 `source all_constraints_amd.tcl` 这一行；Quartus 脚本没有对应分支。

**预期结果**：

| 步骤 | Vivado | Quartus |
| --- | --- | --- |
| 加四区域源 | ✅ `add_files` | ✅ `glob` + `VHDL_FILE` |
| 加 en_cl_fix | ✅ | ✅ |
| 设库 `olo` + VHDL-2008 | ✅ | ✅ |
| 自动加时序约束 | ✅ `source all_constraints_amd.tcl` | ❌ 需用户手工挂 |

base 区域共有 5 个实体被挂 scoped 约束（`cc_reset`、`cc_bits`、`cc_simple`、`reset_gen`、`ram_sdp`）。

> 若你本地没有 Vivado/Quartus，以上为「源码阅读型实践」，结论可直接从脚本读出，无需运行（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `all_constraints_amd.tcl` 只遍历 `{base intf}`，而不含 `axi` 和 `fix`？

**参考答案**：因为 `axi` 和 `fix` 区域没有任何 AMD 约束文件（`src/axi/tcl`、`src/fix/tcl` 目录不存在或为空）。AXI 是同步总线、fix 是纯组合/流水运算，二者都不涉及需要跨时钟域约束或 RAM 时序约束的底层实体，故无需 scoped 约束。

**练习 2**：scoped constraints 用了 `-unmanaged`，并设 `PROCESSING_ORDER LATE`，这两者各自解决什么问题？

**参考答案**：`-unmanaged` 让约束以「不托管」方式读入，避免 Vivado 把它当成普通约束文件去反复解析/排序；`PROCESSING_ORDER LATE` 让库内置约束在用户自己写的约束**之后**才应用，这样当用户约束与库约束冲突时，用户约束优先，库约束不会反过来覆盖用户意图。

---

### 4.2 FuseSoC core 文件结构与 UpdateCoreFiles.py 维护

#### 4.2.1 概念说明

[FuseSoC](https://github.com/olofk/fusesoc) 是 HDL 界的包管理器 + 构建系统，类似 Python 的 pip + setuptools。它的核心是一个个 `.core` 文件（CAPI=2 格式），每个 core 声明：

- **名字**：`<出版方>:<库>:<IP>:<版本>`，例如 `open-logic:open-logic:base:4.6.0`；
- **文件集（fileset）**：一组源文件及其类型（如 `vhdlSource-2008`）和逻辑库名（`olo`）；
- **依赖**：对其他 core 的引用，FuseSoC 会自动拉取并按序编译；
- **target**：不同工具/目标下启用哪些文件集（例如只在 Vivado 下启用 `scoped_constraints`）；
- **provider**：告诉 FuseSoC 从哪里下载这个 core（如 GitHub）。

Open Logic 一共需要维护的 core 文件不少：4 个区域（base/axi/intf/fix）+ en_cl_fix + 3 个教程（Vivado/Quartus/OloFix），而且每个还要分 **dev**（本地工作进展，指向本地 `vhdl/`）和 **stable**（已发布版本，指向 `src/<area>/vhdl/`）两套——总共十几个文件，且版本号要随发布同步更新。手写极易出错，于是项目用 `UpdateCoreFiles.py` 这个 Jinja2 生成器把它们**从模板批量渲染**出来。

#### 4.2.2 核心流程

`UpdateCoreFiles.py` 的生成逻辑：

```text
读取命令行：--version（主版本）--cl-fix-version（en_cl_fix 版本）
for state in [dev, stable]:                      # 两套各渲染一遍
    for area in src/* 的区域目录:
        扫描 src/<area>/vhdl/*.vhd 与 src/<area>/tcl/*.tcl
        用 core.template 渲染 → olo_<area>[_dev].core
    渲染 en_cl_fix.template  → en_cl_fix[_dev].core
    渲染 3 个教程 template    → olo_*_tutorial[_dev].core
```

dev 与 stable 的关键差异（由脚本内 `state` 分支决定）：

| 维度 | dev | stable |
| --- | --- | --- |
| 库名（library） | `open-logic-dev` | `open-logic` |
| 源文件路径 | `vhdl/<file>`（本地相对） | `src/<area>/vhdl/<file>`（仓库相对） |
| codebase 描述 | "local files (release plus WIP)" | "stable release (downloaded from GitHub)" |
| 是否含 provider | 否 | 是（指向 GitHub 仓库 + 版本） |
| 产物去向 | 生成为本地构建产物 | 提交到 `tools/fusesoc/stable/` |

#### 4.2.3 源码精读

生成器入口与配置常量：

[tools/fusesoc/UpdateCoreFiles.py:16-37](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/UpdateCoreFiles.py#L16-L37) — 接收 `--version` 与 `--cl-fix-version` 两个参数，并定义两个查找表：`DESCRIPTIONS`（每个区域的一句话描述）与 `DEPENDENCIES`（区域间依赖：base 无依赖，axi/intf/fix 都依赖 base）。注意 fix 区域对 `en_cl_fix` 的依赖是「外部依赖」，在下文单独处理。

dev/stable 分支与库名后缀：

[tools/fusesoc/UpdateCoreFiles.py:49-64](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/UpdateCoreFiles.py#L49-L64) — 外层 `for state in ["dev", "stable"]` 把同一套渲染跑两遍；dev 时库名加 `-dev` 后缀、文件名加 `_dev` 后缀，codebase 标为本地 WIP。

路径前缀切换与 fix 的外部依赖：

[tools/fusesoc/UpdateCoreFiles.py:94-124](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/UpdateCoreFiles.py#L94-L124) — `fileDir` 在 dev 下是 `vhdl/`、在 stable 下是 `src/<area>/vhdl/`，这就是 dev/stable 指向不同源的根本；此外 `area == "fix"` 时往 data 里追加 `ext_dependencies`，指向带版本号的 `en_cl_fix` core。

模板如何表达「按需启用约束」与「仅 stable 有 provider」：

[tools/fusesoc/core.template:24-44](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/core.template#L24-L44) — 当区域有 TCL 约束文件（`scoped_constraints` 为真）时，才渲染 `scoped_constraints` 文件集，并在 default target 里用 `tool_vivado? (scoped_constraints)` 声明「仅 Vivado 启用它」。`tool_vivado?` 是 FuseSoC 的条件语法：问号前是工具名，仅当用 Vivado 构建时才把该文件集纳入。

[tools/fusesoc/core.template:47-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/core.template#L47-L53) — provider 块只在 `library == "open-logic"`（即 stable）时渲染。dev core 不写 provider，因为它指向本地文件、不需要从远程下载。

看一份真实生成的 stable core：

[tools/fusesoc/stable/olo_base.core:1-4](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_base.core#L1-L4) — 头部 `CAPI=2:` 声明格式版本，`name` 是全限定名 `open-logic:open-logic:base:4.6.0`，description 由 codebase + 区域描述拼接，并附上 EntityList 链接。

[tools/fusesoc/stable/olo_base.core:6-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_base.core#L6-L53) — `rtl` 文件集列出 base 全部 `.vhd`，统一 `file_type: vhdlSource-2008` 与 `logical_name: olo`（与厂商导入脚本一样进库 `olo`）。base 无依赖，故无 `depend` 字段。

[tools/fusesoc/stable/olo_base.core:55-75](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_base.core#L55-L75) — `scoped_constraints` 文件集把 5 个 `.tcl` 用 `copyto` 拷到 `<area>/` 子目录、主聚合脚本标 `file_type: tclSource`；default target 同时启用 `rtl` 与 `tool_vivado? (scoped_constraints)`；末尾 provider 指向 GitHub `open-logic/open-logic` 仓库的 `4.6.0`。

依赖声明看 fix：

[tools/fusesoc/stable/olo_fix.core:44-46](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_fix.core#L44-L46) — fix 区域声明依赖 `^open-logic:open-logic:base:4.6.0` 与 `^open-logic:open-logic:en_cl_fix:2.3.2`。开头的 `^` 是 FuseSoC 的「兼容版本」前缀（允许补丁号向上）。这正是 u1-3 讲过的依赖链（en_cl_fix → fix）在包管理层面的精确表达——用户只要 `fusesoc run` 一个 fix 设计，FuseSoC 会自动把 base 和 en_cl_fix 一并拉来编译，无需 `--recursive`。

#### 4.2.4 代码实践

**实践目标**：理解 core 文件如何被模板渲染出来，并能读懂一份 core 的依赖与 target。

**操作步骤**：

1. 阅读 [doc/HowTo.md 的 FuseSoC 小节](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/HowTo.md#L230-L255)，记下两条 `fusesoc library add` 命令。
2. 打开 [tools/fusesoc/stable/olo_fix.core](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/stable/olo_fix.core)，找出它依赖哪两个 core、为什么 fix 不像 base 那样有 `scoped_constraints` 文件集。
3. 对照 [tools/fusesoc/core.template](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/tools/fusesoc/core.template)，定位模板里哪一行决定了「只有 stable 才写 provider」。

**需要观察的现象**：fix core 有 `depend` 字段而 base 没有；fix 没有 `scoped_constraints` 文件集，因为模板里该段受 `{% if scoped_constraints %}` 控制，而 fix 区域没有 `.tcl` 约束文件。

**预期结果**：fix 依赖 `base:4.6.0` 与 `en_cl_fix:2.3.2`；provider 仅在 stable 渲染，对应模板第 47 行的 `{%- if library == "open-logic" %}`。

> 若本地装了 FuseSoC，可执行 `fusesoc library add open-logic https://github.com/open-logic/open-logic` 后 `fusesoc run --tool vivado --target zybo_z7 open-logic:tutorials:vivado_tutorial` 验证依赖自动解析（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：dev core 与 stable core 的库名分别是什么？为什么 dev core 不写 provider？

**参考答案**：dev 库名是 `open-logic-dev`、stable 是 `open-logic`。dev core 指向用户本地工作副本里的 `vhdl/` 文件（含未发布的 WIP），文件就在磁盘上，FuseSoC 直接读即可，不需要远程下载，故不写 provider；stable core 才需要 provider 告诉 FuseSoC 去 GitHub 拉对应版本的源码。

**练习 2**：模板里 `tool_vivado? (scoped_constraints)` 这一句去掉 `tool_vivado?` 前缀会怎样？

**参考答案**：去掉后 `scoped_constraints` 会在**所有工具**的构建里都启用，包括 Quartus/Libero 等。但 scoped constraints 是 AMD Vivado 专有语法（`read_xdc -ref`），其他工具无法解析这些 `.tcl`，会导致构建失败。`tool_vivado?` 条件正是为了把约束文件集限制在 Vivado。

---

### 4.3 CI 工作流矩阵：免费 GitHub 与付费 AWS 双轨

#### 4.3.1 概念说明

Open Logic 的 CI 是一个典型的**「成本分层」**设计。它要验证的事情很多——仿真、lint、覆盖率、综合、多厂商参考设计构建——但其中只有一部分能用开源工具在免费 runner 上做（GHDL/NVC 仿真、VSG lint、YAML 配置检查）；另一部分必须用商业工具（Questa 带覆盖率、Vivado/Quartus 等综合），这些工具装在项目自建的 AWS EC2 上，跑一次要真金白银。

于是 CI 被分成两条轨：

| 轨道 | 运行环境 | 触发频率 | 包含的工作流 |
| --- | --- | --- | --- |
| **免费轨** | GitHub `ubuntu-latest` | 每个贡献 PR 都跑 | HDL-Check、Doc-Check、analyze-issues |
| **付费轨** | AWS `[self-hosted, aws]` | 仅 PR 到 main / 定期 | Coverage Simulation、FuseSoC Test、Synthesis Test、Reference Design Build |

核心思想：**便宜且能挡住大多数问题的检查，高频跑在免费环境；昂贵但必要的检查，只在「即将发布」的关口（PR 到 main）跑。** 此外，从 fork 来的 PR 需要维护者手动批准才跑付费轨，既省钱也防恶意代码。

#### 4.3.2 核心流程

一个工作流（workflow）由「触发器 `on:` + 若干 job」组成。AWS 类工作流都遵循同一个骨架：

```text
on: pull_request(main) / push(main) / schedule / workflow_dispatch
jobs:
  start-instance:        # 在 ubuntu 上调 AWS action 启动 EC2
    uses: ./.github/actions/start-aws-instance
  <真实工作>:            # runs-on: [self-hosted, aws]，needs: start-instance
    source $LOCAL_TOOLS  # 加载商业工具环境
    cd <对应目录>
    python3 ...          # 跑覆盖率/综合/构建
# EC2 空闲后由 AWS alarm 自动关机，无需显式 stop
```

免费类工作流则简单得多：直接 `runs-on: ubuntu-latest`，用 `setup-environment` 复合 action 装好开源工具即可。

#### 4.3.3 源码精读

总览表是理解整个 CI 的入口：

[doc/CI-Workflows.md:11-19](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md#L11-L19) — 这张矩阵表把 7 个工作流按 5 种触发事件（PR 到 develop、PR 到 main、push 到 main、每月、每日）和 2 种基础设施（GitHub/AWS runner）交叉列出。可一眼看出：HDL-Check/Doc-Check 在最左两列（贡献 + 预发布）都有 ✅ 且只占 GitHub 列；而 Coverage/FuseSoC/Synthesis/Reference Design 只在「PR 到 main」之后才出现且只占 AWS 列。

免费的 HDL-Check 工作流触发器与仿真 job：

[.github/workflows/hdl_check.yml:10-17](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml#L10-L17) — 触发器含 `push: main`、`workflow_dispatch`、`pull_request`（**注意没有 branches 过滤，所以任何 PR 包括到 develop 的贡献 PR 都会触发**）、每周一凌晨 3 点的 `schedule`。

[.github/workflows/hdl_check.yml:21-42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml#L21-L42) — `simulation-ghdl` job 在 `ubuntu-latest` 上 checkout（带 `submodules: true`）、setup-environment，再调 simulation 复合 action 跑 GHDL。同文件还有并行的 `simulation-nvc`、`linting`、`check-synthesis-config` 四个 job。

HDL-Check 的「综合配置检查」用 dry-run 省钱：

[.github/workflows/hdl_check.yml:108-123](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml#L108-L123) — 对四个区域调用 `InferenceTest.py --check-coverage --dry-run`。`--dry-run` 意为「只检查 YAML 是否覆盖了所有实体、不真正跑综合」（u10-l4 讲过），所以它能在免费 runner 上执行；真正的综合在付费轨的 `synthesis.yml` 里。

AWS 轨的启动骨架（以 FuseSoC 为例）：

[.github/workflows/fusesoc.yml:23-40](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/fusesoc.yml#L23-L40) — `start-instance` job 在 ubuntu 上调 `start-aws-instance` 复合 action 启动 EC2，sleep 20 秒等它就绪；后续 `fusesoc-local` job 标 `runs-on: [self-hosted, aws]` 且 `needs: start-instance`。

[.github/workflows/fusesoc.yml:43-77](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/fusesoc.yml#L43-L77) — `fusesoc-local` 把仓库加为 FuseSoC library，用 dev core 构建三个参考设计（Vivado/Quartus/OloFix 教程），每次 PR/push 到 main 都跑。

stable core 只在定期跑——这是 CI 与发布时序的关键耦合点：

[.github/workflows/fusesoc.yml:82-85](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/fusesoc.yml#L82-L85) — `fusesoc-stable` job 带 `if: github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'`。原因写在注释里：stable core 的 provider 指向已发布的 GitHub tag，而 tag 只有在合并到 main **之后**打 release 才存在，所以 PR/push 阶段还构建不了 stable，只能等定期任务。

覆盖率门禁：PR 到 main 必须 ≥95%：

[.github/workflows/coverage_sim.yml:63-76](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml#L63-L76) — 在 PR/dispatch 时跑 `AnalyzeCoverage.py --min_coverage=95`，任一实体低于 95% 则 `sys.exit(1)` 让 CI 失败（u10-l3）；而在 push/schedule（即已到 main）时改为 `--badges` 只更新徽章、**不阻断**，确保差覆盖率被亮出来而非被掩盖。

复合 action 如何复用仿真逻辑：

[.github/actions/simulation/action.yml:16-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/actions/simulation/action.yml#L16-L20) — simulation action 接收 `simulator` 参数，拼成 `python3 run.py --${simulator} ... -p 16`。HDL-Check 的 ghdl/nvc 两个 job 共用这一个 action，仅传参不同——这正是 GitHub 复合 action 的价值。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手画出「工作流 × 触发事件 × 运行环境」对照表，并明确 PR 到 main 前必须通过哪些**免费**检查。

**操作步骤**：

1. 阅读 [doc/CI-Workflows.md 的总览表](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/CI-Workflows.md#L11-L19)。
2. 打开 [.github/workflows/hdl_check.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/hdl_check.yml)，确认它的 4 个 job（simulation-ghdl、simulation-nvc、linting、check-synthesis-config）都跑在 `ubuntu-latest`。
3. 打开 [.github/workflows/coverage_sim.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/coverage_sim.yml) / [fusesoc.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/fusesoc.yml) / [synthesis.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/synthesis.yml)，确认它们 `runs-on: [self-hosted, aws]` 且 `pull_request: branches: [main]`。
4. 据此填写下表。

**需要观察的现象**：免费工作流的 `pull_request` 多不带 `branches` 过滤（贡献 PR 也触发）；付费工作流的 `pull_request` 都带 `branches: [main]`。

**预期结果**（对照表）：

| 工作流 | 文件 | 触发事件 | 运行环境 |
| --- | --- | --- | --- |
| HDL-Check | hdl_check.yml | push main / 任意 PR / 每周一 / 手动 | GitHub |
| Doc-Check | md_check.yml | push main / 任意 PR / 每周一 / 手动 | GitHub |
| analyze-issues | analyze_issues.yml | 每日 / 手动 | GitHub |
| Coverage Simulation | coverage_sim.yml | PR 到 main / push main / 每月 / 手动 | AWS |
| FuseSoC Test | fusesoc.yml | PR 到 main / push main / 每月 / 手动 | AWS |
| Synthesis Test | synthesis.yml | PR 到 main / push main / 每月 / 手动 | AWS |
| Reference Design Build | rd_build.yml | PR 到 main / push main / 每月 / 手动 | AWS |

PR 到 main 前**必须通过的免费检查**：HDL-Check（GHDL + NVC 双仿真器回归、VSG lint、综合 YAML 覆盖 dry-run 检查）与 Doc-Check（Markdown lint）。覆盖率 95% 与真实综合虽然也在 PR 到 main 时跑，但它们在 **AWS** 上，属付费轨。

> 以上结论全部可从 yaml 与文档直接读出，无需触发真实 CI（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `fusesoc-stable` job 只在 `schedule`/`workflow_dispatch` 时跑，而不在 PR/push 时跑？

**参考答案**：stable core 通过 provider 指向 GitHub 上**已发布**的版本 tag。PR/push 到 main 时，这个新版本号对应的 release 还没被打出（release 在合并之后才创建），FuseSoC 拉不到对应 tag，stable 构建必然失败。所以只能等定期任务（届时最新 release 已存在）或人工 dispatch 时跑。

**练习 2**：HDL-Check 里已经有一个 `check-synthesis-config` job 用了 `InferenceTest.py`，那 `synthesis.yml`（付费轨）里的综合和它有什么区别？

**参考答案**：HDL-Check 里的调用带 `--dry-run`，只检查「YAML 是否覆盖了所有实体」（静态配置检查），不启动任何综合工具，能在免费 runner 上跑；`synthesis.yml` 不带 `--dry-run`，会真正对每个实体在 6 种厂商工具里跑综合并解析资源利用率（u10-l4），需要 AWS runner 上的商业工具。

---

### 4.4 贡献流程与发布流程

#### 4.4.1 概念说明

CI 不是孤立的机器检查，它嵌在一条「贡献 → 审查 → 合并 → 发布」的人机协作链里。Open Logic 用 `develop` 和 `main` 两条分支把「日常开发」与「稳定发布」隔开：

- **贡献**：所有 PR 都指向 `develop`（不是 `main`）。小修（文档/不影响接口的 bug）可直接提；大功能（新实体等）须先开 issue 讨论、再实现，且必须带自校验 VUnit 测试台与文档。所有贡献者要签 CLA。
- **预发布**：从 `develop` 合并到 `main` 的 PR 会触发付费轨全套 CI（覆盖率 95% 门禁、综合、参考设计构建），这是发布前的最后关口。
- **发布**：在 `main` 上打 tag、发布 GitHub Release。发布动作会触发 `release.yml`，自动打包一份含子模块的 `CompleteSources.zip` 上传到 release 页（因为 GitHub 自动生成的源码包不含子模块，用户下载会缺 `en_cl_fix`）。

#### 4.4.2 核心流程

```text
贡献者 fork ── feature 分支(基于 develop) ── PR ──┐
                                                  ▼
                              develop ← 合并（免费 CI 随 PR 跑）
                                                  │
                              release/x.y.z 分支 ← 准备发布
                                                  ▼
                              main  ← PR（触发付费轨全套 CI + 95% 门禁）
                                                  │
                              打 tag、发布 GitHub Release
                                                  ▼
                  release.yml 触发 → CompleteSources.zip 上传
                  FuseSoC stable core 对应 tag 此刻才可被解析
```

#### 4.4.3 源码精读

贡献的分支约定与两类贡献的区分：

[Contributing.md:54-66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Contributing.md#L54-L66) — 「Simple Fixes」要求从 `develop` 切 `feature/<名字>` 分支、改完确保测试通过、**PR 到 develop**。注意目标是 develop，不是 main。

[Contributing.md:68-87](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Contributing.md#L68-L87) — 「Larger Features」额外要求先开 issue 讨论、实现时必须包含自校验 VUnit testbench 与文档。

CLA 要求：

[Contributing.md:96-98](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Contributing.md#L96-L98) — 任何贡献要被接受，必须通过 cla-assistant.io 签署 CLA。

发布的机器化部分——打包含子模块的源码：

[.github/workflows/release.yml:3-25](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/release.yml#L3-L25) — 触发器是 `release: types: [published]`（即发布 Release 时）。它 checkout 时带 `submodules: recursive`，删掉所有 `.git` 目录后 `zip -r CompleteSources.zip .`，再用 `AButler/upload-release-assets` 上传到该 release。

仓库的发布节律可从 git 历史直接读出。`git log` 显示一条条 `RELEASE: x.y.z` 提交（如 `RELEASE: 4.6.0`、`RELEASE: 4.5.0`……），每个版本对应一次 main 上的发布提交；历史上还存在过 `release/4.5.0`、`revert-329-release/4.6.0` 这类分支，印证了「release 分支准备 → main 合并 → 必要时回退」的真实流程。

这条发布链还解释了 u1-3 里那个告诫：从 release 页下载源码必须选 `CompleteSources.zip`，因为它是这个 workflow 用 `submodules: recursive` 打的；GitHub 自动生成的 zip/tar **不含子模块**，下载后 fix 区域会缺文件。

#### 4.4.4 代码实践

**实践目标**：把发布流程的触发条件与产物对应起来。

**操作步骤**：

1. 阅读 [.github/workflows/release.yml](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/.github/workflows/release.yml)，确认它的触发器是「release 发布」而非「打 tag」本身。
2. 阅读 [Readme.md 的 Download Archive 小节](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md#L56-L66)，理解为什么强调要下 `CompleteSources.zip`。
3. 用 `git log --oneline --grep="RELEASE"` 观察版本号序列。

**需要观察的现象**：release.yml 在 release published 时跑，产物是 `CompleteSources.zip`；git 历史里 RELEASE 提交的版本号与 stable core 文件里的版本号（如 `olo_base.core` 的 `4.6.0`）一致。

**预期结果**：一次发布的完整闭环为——`main` 上提交 `RELEASE: x.y.z` → 创建 GitHub Release → `release.yml` 自动打包 `CompleteSources.zip`（含 en_cl_fix 子模块）上传 → 该 tag 此后可被 FuseSoC stable core 解析、被定期 FuseSoC CI 验证。

> 该实践为源码阅读型，无需真实打 release（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `release.yml` 用 `release: types: [published]` 而不是 `push: tags`？

**参考答案**：因为产物 `CompleteSources.zip` 要**上传到 Release 页面**，而 Release 资产必须等 Release 对象存在才能挂。`release: published` 事件恰好发生在 Release 创建之时，此时上传目标已就绪；若用 `push: tags`，事件发生时还没有 Release 对象可挂资产。

**练习 2**：一个外部贡献者按流程提 PR，他的改动会触发哪些 CI？付费轨会自动跑吗？

**参考答案**：会触发免费轨的 HDL-Check 与 Doc-Check（因为它们 `pull_request` 不过滤分支，贡献 PR 到 develop 也会触发）。付费轨（覆盖率/综合/FuseSoC）**不会自动跑**——它们要么只在 PR 到 main 时触发，要么对来自 fork 的 PR 需要维护者手动批准（出于成本与安全考虑，见 CI-Workflows.md 的 Note）。

---

## 5. 综合实践

**任务**：追踪一个假设的「新增一个 base 实体 `olo_base_foo`」从写代码到发布的完整工程链路，回答每一步该动哪个文件、会被哪条 CI 检查。

**操作步骤**：

1. **写实体**：在 `src/base/vhdl/olo_base_foo.vhd` 实现，遵循 u1-l5 规范（后缀 `_g/_c`、AXI-S 握手、同步复位覆盖）。
2. **加测试与文档**：在 `test/base/olo_base_foo/` 写 VUnit testbench（Contributing.md 要求自校验），在 `doc/base/` 加文档。
3. **注册综合测试**：在 `tools/inference_test/yaml/base.yml` 加一条（u10-l4），使 `--check-coverage` 不报「实体未覆盖」。
4. **更新导入/core**：
   - 厂商导入脚本**无需改**——它们用 `glob`/通配自动收录新 `.vhd`（见 4.1.3）。
   - FuseSoC core **无需手改**——重跑 `tools/fusesoc/UpdateCoreFiles.py --version <新版本>` 即可把新文件渲染进 `.core`（见 4.2.3）。
5. **提 PR 到 develop**：触发免费轨 HDL-Check（GHDL/NVC 回归 + VSG lint + 综合 YAML 覆盖 dry-run）。
6. **合并后准备 release**：经 `release/x.y.z` 分支提 PR 到 main，触发付费轨（覆盖率 95% 门禁、真实综合、FuseSoC/参考设计构建）。
7. **发布**：main 上 `RELEASE: x.y.z` → GitHub Release → `release.yml` 打包 `CompleteSources.zip`。

**需要观察的现象（设计要点）**：正是因为导入脚本用通配、core 文件用生成器，新增一个实体时**机器可推导的部分全部自动化**，人只需改「实体本身 + 测试 + 文档 + 综合 YAML」这四处单一真相源。

**预期结果**：你能画出一张表，左边是步骤 1–7，右边标注「改动文件」与「触发/依赖的 CI 工作流」，并指出步骤 4 是本讲两个自动化机制（厂商脚本通配、FuseSoC 生成器）让人省力的关键。

## 6. 本讲小结

- 厂商导入脚本（`tools/<厂商>/import_sources.*`）统一完成「加源 → 进库 `olo` → 设 VHDL-2008 → 加约束」，其中**仅 Vivado 凭 scoped constraints（`read_xdc -ref`）自动套约束**，其他厂商需用户手工挂约束。
- FuseSoC 用 `.core` 文件（CAPI=2）描述 IP 的源、依赖、target 与 provider；`UpdateCoreFiles.py` 用 Jinja2 模板批量生成 dev（本地 WIP）与 stable（已发布）两套，区域间依赖（fix→base+en_cl_fix）在 core 里精确声明。
- CI 采用**免费 GitHub 轨 + 付费 AWS 轨**双轨分层：HDL-Check/Doc-Check/analyze-issues 高频跑在免费 runner；Coverage(95%)/FuseSoC/Synthesis/Reference Design 仅在 PR 到 main 或定期跑在 AWS，fork PR 需人工批准。
- stable core 的 FuseSoC 验证只能在定期任务跑，因为它依赖尚未创建的新 release tag——这体现了 CI 与发布时序的耦合。
- 贡献一律 PR 到 `develop`，合并到 `main` 才触发付费轨门禁；发布时 `release.yml` 自动打包含子模块的 `CompleteSources.zip`，弥补 GitHub 自动源码包缺子模块的问题。
- 整条链路的设计哲学是「机器可推导的自动化、人只维护单一真相源」：导入脚本通配收录新文件、core 文件由生成器渲染，故新增实体时人只改实体+测试+文档+综合 YAML。

## 7. 下一步学习建议

至此 Open Logic 学习手册全部 10 个单元讲完。建议你：

1. **动手做一次真实贡献**：按本讲 4.4 与综合实践的流程，挑 [Feature Ideas](https://github.com/open-logic/open-logic/wiki/Feature-Ideas) 里一个小项，走一遍 fork → feature 分支 → PR 到 develop，亲身感受免费轨 CI 的反馈。
2. **本地复现一条 CI**：装 GHDL 或 NVC，按 `sim/run.py`（u10-l2）与 `lint/script/script.py`（u10-l4）在本地复刻 HDL-Check 的仿真与 lint，把 CI 从「黑盒」变成「可本地跑的脚本」。
3. **横向对照其他 HDL 库**：对比 Open Logic 的 FuseSoC+CI 方案与其他开源 IP 库（如 [PoC-Library](https://github.com/VLSI-EDA/PoC)、[UVVM](https://github.com/UVVM/UVVM)）的工程化思路，加深对「Trustable Code + Ease of Use + Pure VHDL」三大哲学如何落到工程实践的理解。
4. **重读 Readme 与 Conventions**：带着现在对全库结构的认识回头读 [Readme.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/Readme.md) 与 [doc/Conventions.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md)，你会发现许多当初抽象的表述现在都有了具体的源码落点。
