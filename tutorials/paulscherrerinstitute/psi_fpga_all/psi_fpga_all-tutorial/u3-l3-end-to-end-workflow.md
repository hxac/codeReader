# 端到端工作流：克隆→仿真→打包

## 1. 本讲目标

本讲是整本手册的收尾，面向**已经分别理解了「集合仓库怎么克隆」「PsiSim 仿真流水线怎么走」「IP 怎么打包」**的读者。前面九讲是把 psi_fpga_all 拆成一块块来讲，本讲把它们**装回去**，回答一个最实际的问题：

> 「我拿到这个仓库后，从零到把全部库仿真自检跑一遍、再把全部 Vivado IP 打包出来，完整的一条命令链是什么？每一步怎么判断它成功还是失败？」

学完本讲，你应当能够：

- 把**克隆 → 仿真 → 打包**三步串成一条首尾相接、顺序固定的工作流，并说清楚为什么必须按这个顺序。
- 说出 `scripts/` 下每个驱动脚本在这条工作流里扮演的角色，以及它们共同的一个隐藏前提：**运行时 `pwd` 必须是 `scripts/` 目录**。
- 在真实环境（已克隆全部 submodule、且装好对应 EDA 工具的 Vivado / ModelSim / GHDL）里，按顺序 source 正确的脚本驱动整个集合仓库，并用 `run_check_errors "###ERROR###"` 的错误标志判断仿真是否真的通过。

> 本讲是「专家层」的最后一讲，承接 [u2-l2（PsiSim 仿真流程）](u2-l2-simulation-psisim-flow.md)、[u2-l4（IP 批量打包）](u2-l4-ip-packaging.md) 与 [u3-l1（发布管理与版本固定）](u3-l1-release-and-version-pinning.md)，并把 [u1-l2（submodule 与克隆）](u1-l2-submodules-and-cloning.md) 的克隆命令作为整条流程的起点。

## 2. 前置知识

在进入本讲之前，请确认你已理解下列概念（均在依赖讲义中建立）：

- **集合仓库与递归克隆**：psi_fpga_all 用 git submodule 把全部 FPGA 库挂到固定目录，必须用 `git clone --recurse-submodules`，否则 `VHDL/`、`Python/` 等目录存在却为空。详见 [u1-l2](u1-l2-submodules-and-cloning.md)。
- **PsiSim 五步流水线**：三个仿真脚本共享 `init → configure → compile_files → run_tb → run_check_errors` 的骨架，`###ERROR###` 是 self-checking TB 自检失败时主动打印的标志。详见 [u2-l2](u2-l2-simulation-psisim-flow.md)。
- **仿真器兼容性矩阵**：同一个库的 TB 在 GHDL / ModelSim / Vivado 下是否被启用，由脚本里是否注释掉它的 `config.tcl` 决定；`power_sink` 因「没有 self-checking TB」三种仿真器都不跑。详见 [u2-l3](u2-l3-simulator-compatibility.md)。
- **IP 批量打包**：`packageAllIp.tcl` 逐个 `source` 各 `vivadoIP_*/scripts/package.tcl` 完成打包；打包集合与仿真集合相互独立（`power_sink` 不仿真却被打包）。详见 [u2-l4](u2-l4-ip-packaging.md)。
- **版本固定是前提**：`--recurse-submodules` 拉到的是父仓库 gitlink 指向的那组「协调一致」的子模块版本，而不是各库的最新状态。详见 [u3-l1](u3-l1-release-and-version-pinning.md)。

本讲会用到的两个操作概念，先用大白话补一下：

- **Tcl 控制台（Tcl Console）**：Vivado、ModelSim 这类 EDA 工具都内置一个交互式的 Tcl 命令行。`source xxx.tcl` 就是在这个控制台里「把一个脚本文件逐行读进来执行」。psi_fpga_all 的驱动脚本不是用命令行 `tclsh` 跑的，而是**在 EDA 工具的 Tcl 控制台里 source**——因为它们要调用仿真器/打包器本身的能力。
- **工作目录（`pwd`）敏感**：Tcl 里的相对路径（如 `../TCL/PsiSim/PsiSim.tcl`）是相对于「当前工作目录」解析的。source 一个脚本**不会**自动把工作目录切到脚本所在目录，所以你得自己先 `cd` 到正确的位置再 source。

## 3. 本讲源码地图

本讲读五个文件，它们正好覆盖整条工作流的「入口 + 三段执行」：

| 文件 | 在工作流中的角色 | 本讲用来讲什么 |
| --- | --- | --- |
| `README.md` | 流程起点：给出克隆命令 | `--recurse-submodules` 与 SSH/HTTPS 两条命令 |
| `scripts/runModelsim.tcl` | 仿真段（ModelSim 后端） | PsiSim 五步流水线的完整样貌、`###ERROR###` 判据 |
| `scripts/runGhdl.tcl` | 仿真段（GHDL 后端） | `init -ghdl` 选后端、兼容性差异 |
| `scripts/runVivado.tcl` | 仿真段（Vivado 后端） | `init -vivado` 选后端、`source -quiet` |
| `scripts/packageAllIp.tcl` | 打包段 | 逐个 `source package.tcl` 的打包模板 |

> 注意：被这些驱动脚本 `source` 进来的 `PsiSim.tcl`、各库的 `config.tcl`、各 IP 的 `package.tcl` 都住在对应的 submodule 内部，本仓库不直接包含，其具体内容在依赖讲义中标注为「待确认」。本讲只关心**驱动脚本如何编排它们**。

## 4. 核心概念与源码讲解

### 4.1 克隆得到完整目录（工作流的起点）

#### 4.1.1 概念说明

整条端到端工作流有一个不可省略的零步：**先把全部 submodule 克隆下来**。原因在于「目录结构即接口」（详见 [u1-l1](u1-l1-project-overview.md)）——四个驱动脚本里所有的路径都是相对路径，例如 `$myPath/../VHDL/psi_common/sim`、`../TCL/PsiSim/PsiSim.tcl`。这些路径只有当 `VHDL/`、`TCL/`、`VivadoIp/` 等目录里**真的装满了子模块文件**时才指向真实存在的东西。

如果克隆时漏掉 `--recurse-submodules`，目录结构（那一层空壳）还在，但里面是空的——后续 source 任何驱动脚本都会在第一个 `source ../TCL/PsiSim/PsiSim.tcl` 处就报「文件不存在」。所以「克隆得到完整目录」不是可选项，而是整条流程能跑起来的**硬前提**。

#### 4.1.2 核心流程

```
1. 选协议：SSH（有 GitHub 账号 + SSH key）或 HTTPS（无配置也能拉公开仓库）

2. 递归克隆（一条命令拉下父仓库 + 全部 23 个子模块）：
   git clone --recurse-submodules <协议前缀>paulscherrerinstitute/psi_fpga_all.git

3. 克隆完成后，工作流所需的目录都已就位：
   - scripts/        ← 4 个驱动脚本（本仓库自带）
   - TCL/PsiSim/     ← PsiSim 框架（被仿真脚本 source）
   - VHDL/*/sim/     ← 各 VHDL 库的 config.tcl（被仿真脚本 source）
   - VivadoIp/*/sim/ ← 各 IP 的 config.tcl（被仿真脚本 source）
   - VivadoIp/*/scripts/ ← 各 IP 的 package.tcl（被打包脚本 source）

4. （若以前克隆时漏了 --recurse-submodules，补救）：
   git submodule update --init --recursive
```

第 3 步是关键认知：驱动脚本里写的每一个相对路径，都对应一个**子模块内部**的文件。子模块没拉下来，这些路径就是悬空的。

#### 4.1.3 源码精读

克隆命令来自 README 的 `Cloning` 章节：

[README.md:34-45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L34-L45) —— `Cloning` 章节说明「因为含 submodule，必须用 `--recurse-submodules`」，并给出 SSH、HTTPS 两条命令。

```markdown
## Cloning
Because the repository contains submodules, it must be cloned with the *--recurse-submodules* option:

git clone --recurse-submodules git@github.com:paulscherrerinstitute/psi_fpga_all.git

If you do not have a github account with SSH configured use https instead:

git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
```

两条命令逐字对照，**唯一区别是协议前缀**：

| 克隆方式 | 命令前缀 | 适用对象 |
| --- | --- | --- |
| SSH | `git@github.com:paulscherrerinstitute/psi_fpga_all.git` | 已配置 SSH key 的 GitHub 用户 |
| HTTPS | `https://www.github.com/paulscherrerinstitute/psi_fpga_all.git` | 无 SSH key 的用户（公开仓库可直接拉） |

之所以 README 敢同时给出两种命令而无需任何额外配置，是因为子模块 url 用了相对形式 `../../paulscherrerinstitute/...`，自动继承父仓库的克隆协议（详见 [u3-l2 的 SSH/HTTPS 双兼容](u3-l2-maintaining-and-extending.md)）。无论你选哪条，子模块都跟着走同一协议。

> 关键点：`--recurse-submodules` 这一个标志，把「拉父仓库」和「拉全部 23 个子模块到正确目录」合并成了一步。少了它，第 4.2、4.3 节的所有脚本都跑不起来。

#### 4.1.4 代码实践

**实践目标**：确认「完整克隆」与「非递归克隆」的差别，验证工作流所需的目录都已就位。

**操作步骤**：

1. 用 HTTPS 完整克隆到一个临时目录（**示例命令**，需本地有网络）：

   ```bash
   git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
   cd psi_fpga_all
   ```

2. 检查关键目录是否非空：

   ```bash
   ls scripts/                 # 应看到 4 个 .tcl + .gitignore
   ls TCL/PsiSim/              # 应看到 PsiSim 框架文件（非空）
   ls VHDL/psi_common/sim/     # 应看到 config.tcl（非空）
   ls VivadoIp/vivadoIP_data_rec/scripts/  # 应看到 package.tcl（非空）
   ```

3. 对照实验（**待本地验证**）：在另一个目录用**不带** `--recurse-submodules` 的方式克隆，重复第 2 步，观察 `TCL/`、`VHDL/`、`VivadoIp/` 是否表现为空目录。

**需要观察的现象**：

- 完整克隆：第 2 步所有目录都有真实文件。
- 非递归克隆：`VHDL/`、`TCL/`、`VivadoIp/`、`Python/` 等目录**存在但为空**——这正是 [u1-l2](u1-l2-submodules-and-cloning.md) 讲的「空目录现象」。

**预期结果**：完整克隆下，驱动脚本里出现的每一个相对路径（如 `../TCL/PsiSim/PsiSim.tcl`）都能解析到真实文件，工作流的后两步才具备运行条件。

#### 4.1.5 小练习与答案

**练习 1**：同事用普通 `git clone`（不带 `--recurse-submodules`）拉了仓库，然后直接在 ModelSim 里 `source scripts/runModelsim.tcl`，会在哪一行先报错？为什么？

> **答案**：会在脚本第 8 行 `source ../TCL/PsiSim/PsiSim.tcl` 处报「找不到文件」。因为 `TCL/PsiSim/` 目录是空的（子模块未检出），这个相对路径指向一个不存在的文件。补救办法是执行 `git submodule update --init --recursive`，把全部子模块补拉下来。

**练习 2**：为什么克隆命令必须带 `--recurse-submodules`，而不能等以后需要时再说？

> **答案**：因为四个驱动脚本里的全部路径都是相对路径，依赖子模块真实存在于固定目录。子模块是整条工作流的「素材」，没有素材，编排脚本（驱动脚本）再正确也无从执行。`--recurse-submodules` 一次性把素材备齐，是工作流能跑的硬前提。

---

### 4.2 用 run*.tcl 跑仿真并检查错误

#### 4.2.1 概念说明

目录备齐后，工作流进入第二段：**仿真自检**。`scripts/` 下有三个仿真驱动脚本，对应三种仿真器后端：

| 脚本 | 仿真器后端 | 启用方式 |
| --- | --- | --- |
| `runModelsim.tcl` | ModelSim（默认） | `init`（无参数） |
| `runGhdl.tcl` | GHDL | `init -ghdl` |
| `runVivado.tcl` | Vivado 仿真器 | `init -vivado` |

三者共享同一套 PsiSim 流水线（详见 [u2-l2](u2-l2-simulation-psisim-flow.md)），差异只在 `init` 的参数和 configure 阶段启用了哪些库（兼容性矩阵详见 [u2-l3](u2-l3-simulator-compatibility.md)）。

这一段最容易被忽视的两个操作要点：

1. **运行前提是 `pwd == scripts/`**：三个脚本都用 `set myPath [pwd]` 记录起点、再用 `$myPath/../...` 拼路径。如果你不在 `scripts/` 里 source 它们，`$myPath` 就不是 `scripts/`，所有相对路径都会错位。
2. **「跑完」不等于「通过」**：仿真流水线的最后一步是 `run_check_errors "###ERROR###"`，它扫描日志里是否出现 self-checking TB 在断言失败时打印的 `###ERROR###` 标志。**日志里没有这个标志才算通过**，光看到仿真「跑完了」不能下结论。

#### 4.2.2 核心流程

以 ModelSim 为例，在 EDA 工具的 Tcl 控制台里执行：

```
1. 先切到 scripts/ 目录（关键前提！）：
   cd <仓库根>/scripts

2. source 仿真驱动脚本：
   source runModelsim.tcl

3. 脚本内部自动执行 PsiSim 五步：
   a. source ../TCL/PsiSim/PsiSim.tcl    ← 加载框架
   b. namespace import psi::sim::*        ← 让命令可裸名调用
   c. init                                 ← 选 ModelSim 后端
   d. 逐库 cd <lib>/sim + source config.tcl ← 登记 10 个库的源文件与 TB
   e. compile_files -all -clean           ← 编译全部登记的文件
   f. run_tb -all                          ← 跑全部登记的 TB
   g. run_check_errors "###ERROR###"       ← 扫描错误标志

4. 判断结果：看最后一步的输出 / 日志
   - 日志中【不】出现 ###ERROR###  → 全部库自检通过 ✅
   - 日志中出现 ###ERROR###        → 某个 self-checking TB 断言失败 ❌
```

第 4 步是整段的「验收标准」，也是实践任务要求写明的成功/失败判据。换仿真器只需把第 2 步的脚本换成 `runGhdl.tcl`（GHDL）或 `runVivado.tcl`（Vivado），其余判据完全相同——三个脚本的结尾都是同一句 `run_check_errors "###ERROR###"`。

#### 4.2.3 源码精读

**（1）加载框架与选后端**

三个脚本的开头几乎相同，只有 `init` 参数和是否 `-quiet` 不同。先看 ModelSim：

[scripts/runModelsim.tcl:7-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L7-L12) —— 加载 PsiSim 框架、导入命名空间、用无参 `init` 选 ModelSim 后端。

```tcl
#Load dependencies
source ../TCL/PsiSim/PsiSim.tcl
namespace import psi::sim::*

#Initialize Simulation
init
```

GHDL 与 Vivado 只把 `init` 换成带参数的版本：

[scripts/runGhdl.tcl:11-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L11-L12) —— `init -ghdl` 选 GHDL 后端。

```tcl
#Initialize Simulation
init -ghdl
```

[scripts/runVivado.tcl:13-14](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L13-L14) —— `init -vivado` 选 Vivado 仿真器后端。

```tcl
#Initialize Simulation
init -vivado
```

注意 `runVivado.tcl` 还在 `source` 上加了 `-quiet`（[第 8 行](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L8)、[第 20 行](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runVivado.tcl#L20)），抑制加载与配置时的多余输出，这是它在三脚本里的另一个细微差异。

**（2）configure 阶段——逐库登记**

`init` 之后是 configure：先记下起点，再逐库 `cd` 到 `<lib>/sim` 并 `source config.tcl`。第一库如下：

[scripts/runModelsim.tcl:15-18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L15-L18) —— `set myPath [pwd]` 锚定起点；`cd` 到 `psi_common/sim` 后 `source config.tcl` 登记该库的源文件与 testbench。

```tcl
#Configure
set myPath [pwd]

cd $myPath/../VHDL/psi_common/sim
source config.tcl
```

这里能看到 `$myPath/../...` 的写法——`myPath` 必须是 `scripts/`，`../VHDL/...` 才指向仓库根下的 VHDL 库。这正是 4.2.1 强调的「`pwd == scripts/`」前提的来源。跳过一个库就把它的 `cd` 与 `source` 两行整块注释（兼容性判定详见 [u2-l3](u2-l3-simulator-compatibility.md)），例如 GHDL 跳过 `psi_multi_stream_daq`：

[scripts/runGhdl.tcl:23-25](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runGhdl.tcl#L23-L25) —— 注释 `#TB not GHDL compatible!` 后把该库的两行整块注释，等价于「本次不仿真它」。

```tcl
#TB not GHDL compatible!
#cd $myPath/../VHDL/psi_multi_stream_daq/sim
#source config.tcl
```

configure 结束后回到起点：

[scripts/runModelsim.tcl:50](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L50) —— `cd $myPath` 把工作目录切回 `scripts/`，为后续编译/运行做准备。

```tcl
cd $myPath
```

**（3）三段执行与错误检查**

configure 之后是三段执行：编译、运行、检查。三个脚本在这部分几乎逐字相同：

[scripts/runModelsim.tcl:52-65](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/runModelsim.tcl#L52-L65) —— 编译全部文件、跑全部 TB、最后扫描 `###ERROR###` 标志。

```tcl
#Run Simulation
puts "------------------------------"
puts "-- Compile"
puts "------------------------------"
compile_files -all -clean
puts "------------------------------"
puts "-- Run"
puts "------------------------------"
run_tb -all
puts "------------------------------"
puts "-- Check"
puts "------------------------------"

run_check_errors "###ERROR###"
```

三段对应三个 PsiSim 命令：

| 阶段 | 命令 | 作用 |
| --- | --- | --- |
| Compile | `compile_files -all -clean` | 编译 configure 阶段登记的全部源文件（`-clean` 先清旧产物） |
| Run | `run_tb -all` | 跑 configure 阶段登记的全部 testbench |
| Check | `run_check_errors "###ERROR###"` | 扫描日志，出现 `###ERROR###` 即报错 |

末行 `run_check_errors "###ERROR###"` 就是整段的**成功/失败判据**：self-checking TB 在断言失败时会主动打印 `###ERROR###`，这条命令把它作为关键字搜日志——找到了就说明有 TB 没通过。所以**仿真通过 = 日志里不出现 `###ERROR###`**，而不是「仿真器没崩溃」。

#### 4.2.4 代码实践

**实践目标**：在 ModelSim 的 Tcl 控制台里跑完全部库的仿真自检，并用 `###ERROR###` 判据判断结果。

**操作步骤**：

1. 确认已完成 4.1 的完整克隆，且本机装有 ModelSim。
2. 启动 ModelSim，在其 Tcl 控制台里先把工作目录切到 `scripts/`：

   ```tcl
   cd <仓库根>/scripts
   ```

3. source 仿真脚本：

   ```tcl
   source runModelsim.tcl
   ```

4. 观察控制台依次打印 `-- Compile` / `-- Run` / `-- Check` 三段分隔标题，最后由 `run_check_errors` 给出检查结论。

**需要观察的现象**：

- 编译段：各库源文件被编译，无报错。
- 运行段：各库 testbench 依次跑完。
- 检查段：`run_check_errors "###ERROR###"` 扫描日志。

**预期结果**：若全部 self-checking TB 通过，日志中**不会**出现 `###ERROR###`，整体判为通过；若任一 TB 断言失败，日志中**会**出现 `###ERROR###`，整体判为失败。换 GHDL 则 source `runGhdl.tcl`、换 Vivado 则 source `runVivado.tcl`，判据完全相同。

**待本地验证**：完整运行需要在已克隆全部 submodule、且装好对应仿真器的真实环境中进行；本实践以「正确切换 `pwd` 到 `scripts/`、source 正确脚本、用 `###ERROR###` 判据解读结果」为验收标准。

#### 4.2.5 小练习与答案

**练习 1**：如果你在仓库根目录（而不是 `scripts/`）下执行 `source scripts/runModelsim.tcl`，会出什么问题？

> **答案**：`set myPath [pwd]` 会把 `myPath` 记成仓库根，于是 `$myPath/../TCL/PsiSim/PsiSim.tcl` 解析到「仓库根的上一级」去找 `TCL/PsiSim/`，路径错位、文件不存在，脚本在最开始的 `source` 就失败。正确做法是先 `cd scripts/` 再 source。这印证了「驱动脚本假设运行时 `pwd` 为 `scripts/`」。

**练习 2**：仿真器跑完所有 TB、控制台没有崩溃，能直接宣布「仿真通过」吗？

> **答案**：不能。必须看 `run_check_errors "###ERROR###"` 的结论。self-checking TB 的设计是「断言失败时打印 `###ERROR###`」，所以**日志里不出现 `###ERROR###` 才算通过**。「跑完」只代表流程走完，不代表断言全绿。

**练习 3**：三种仿真器下，`power_sink` 都不参与仿真，原因是什么？

> **答案**：与兼容性无关。`power_sink` 衡量的是功耗、翻转率、综合优化，这些无法用功能仿真器模拟，所以它**没有 self-checking TB**，三种仿真器都跳过它（脚本注释原话：*Does not have a self-checking TB because power consumption/toggling/optimization cannot be simulated!*，详见 [u2-l3](u2-l3-simulator-compatibility.md)）。注意它只是「不仿真」，下一节会看到它仍会被打包。

---

### 4.3 用 packageAllIp.tcl 打包 IP

#### 4.3.1 概念说明

工作流的第三段是 **Vivado IP 批量打包**：把一批 HDL 库封装成可被 Vivado 直接调用的 IP 核。这一段由 `scripts/packageAllIp.tcl` 单独驱动，与仿真段相互独立。

「独立」是这一段最重要的认知，有两层含义：

1. **流程独立**：打包脚本顶部**不加载 PsiSim 框架**，不复用仿真的 init/compile/run 流水线。它只用 [u2-l1](u2-l1-scripts-overview.md) 讲的那个更简单的「`set myPath` → 逐个 `cd` + `source` → `cd` 回」模板。
2. **集合独立**：打包的 IP 集合 ≠ 仿真的库集合。最典型的反例就是 `power_sink`——它三种仿真器都不跑（4.2.5 练习 3），**却出现在打包列表里**；反过来，能仿真的 `axi_mm_reader` 却不在打包列表里。一个 IP 在仿真里的状态，不能推断它在打包里的状态（详见 [u2-l4](u2-l4-ip-packaging.md)）。

#### 4.3.2 核心流程

在 Vivado 的 Tcl 控制台里：

```
1. 先切到 scripts/ 目录（与仿真段同样的前提）：
   cd <仓库根>/scripts

2. source 打包脚本：
   source packageAllIp.tcl

3. 脚本内部自动：
   a. set myPath [pwd]                     ← 记录起点（scripts/）
   b. 逐个 cd <IP>/scripts + source package.tcl  ← 打包 8 个 IP
   c. cd $myPath                            ← 回到起点收尾

4. 判断结果：每个 package.tcl 跑完会在对应 IP 的目录下
   生成 Vivado 可识别的 IP 产物；任一 package.tcl 报错
   则该 IP 打包失败（无统一错误标志，需逐个看日志）。
```

注意第 4 步与仿真段的差别：仿真段有一个统一的 `###ERROR###` 判据；打包段没有等价的统一标志，成败要看每个 `package.tcl` 的执行日志。

#### 4.3.3 源码精读

`packageAllIp.tcl` 是四个驱动脚本里最干净的一个，全文只有「记起点 → 逐个打包 → 回起点」三段：

[scripts/packageAllIp.tcl:7-12](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L7-L12) —— `set myPath [pwd]` 记录起点；第一个 IP：`cd` 到 `vivadoIP_axis_data_gen/scripts` 后 `source package.tcl` 打包。

```tcl
#Setup
set myPath [pwd]

#Package
cd $myPath/../VivadoIp/vivadoIP_axis_data_gen/scripts
source package.tcl
```

与仿真段对照，能看到两个结构差异：

1. **顶部没有 `source PsiSim.tcl` / `namespace import`**。打包脚本不依赖 PsiSim 框架（打包用的 `PsiIpPackage` 很可能在每个 `package.tcl` 内部各自加载，详见 [u2-l4](u2-l4-ip-packaging.md)）。
2. **`cd` 的目标从 `<lib>/sim` 换成 `<IP>/scripts`**，`source` 的对象从 `config.tcl` 换成 `package.tcl`。`package.tcl` 声明该 IP 包含哪些 HDL、暴露哪些参数/端口与版本号，是 IP 打包的「登记文件」，与仿真侧的 `config.tcl` 角色对称。

脚本逐个打包 8 个 IP，最后一个值得专门看：

[scripts/packageAllIp.tcl:32-36](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L32-L36) —— 打包 `vivadoIP_power_sink`（它不仿真、但被打包），随后 `cd $myPath` 回起点收尾。

```tcl
cd $myPath/../VivadoIp/vivadoIP_power_sink/scripts
source package.tcl

#Go back to initial directory
cd $myPath
```

`power_sink` 出现在这里，正好和 4.2 形成**镜像反例**：

| 模块 | 仿真（三种后端） | 打包 |
| --- | --- | --- |
| `power_sink` | 全部跳过（无 self-checking TB） | ✅ 打包 |
| `axi_mm_reader` | ✅ 仿真（ModelSim/GHDL） | 不打包 |

这正是「仿真集合与打包集合相互独立」的最直观证据：功耗不可仿真，但可以做成 IP 喂给功耗分析；反之能仿真的 IP 不一定需要打包。两件事目的不同，列表自然不同。

> 关于打包数量：脚本当前打包 8 个 IP，而 `.gitmodules` 共声明 11 个 `vivadoIP_*`，差出的 `fpga_base`、`sync_edge_det`、`axi_mm_reader` 未被打包，原因本仓库未说明，标注「待确认」（详见 [u2-l4](u2-l4-ip-packaging.md)）。

#### 4.3.4 代码实践

**实践目标**：在 Vivado 的 Tcl 控制台里批量打包全部 IP，并核对打包集合与仿真集合的差异。

**操作步骤**：

1. 确认已完成 4.1 的完整克隆，且本机装有 Vivado。
2. 启动 Vivado，在其 Tcl 控制台里切到 `scripts/`：

   ```tcl
   cd <仓库根>/scripts
   ```

3. source 打包脚本：

   ```tcl
   source packageAllIp.tcl
   ```

4. 逐个观察每个 `package.tcl` 的执行日志，确认对应 IP 产物生成。

**需要观察的现象**：脚本依次 `cd` 进 8 个 `vivadoIP_*/scripts` 目录、各 `source` 一次 `package.tcl`；最后回到 `scripts/`。

**预期结果**：8 个 IP 各自完成打包。注意 `power_sink` 在这里**会**被打包——与它在仿真段被全部跳过形成对照，印证「打包集合 ≠ 仿真集合」。

**待本地验证**：完整运行需要已克隆全部 submodule 且装有 Vivado 的真实环境；各 `package.tcl` 的具体打包逻辑住在 submodule 内部，本仓库不含，标注「待确认」。本实践以「正确 source 脚本、理解集合独立性」为验收标准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `packageAllIp.tcl` 顶部不需要像仿真脚本那样 `source ../TCL/PsiSim/PsiSim.tcl`？

> **答案**：因为打包不使用 PsiSim 仿真框架。PsiSim 提供的是 `init/compile_files/run_tb/run_check_errors` 这一套仿真专用命令，打包用不到。打包所需的 IP 封装能力（`PsiIpPackage`）由每个 `package.tcl` 在各自内部加载，所以驱动脚本顶部无需统一加载框架。

**练习 2**：某同事看到 `power_sink` 不参与仿真，就推断「它一定也不在打包列表里」。这个推断对吗？

> **答案**：不对。仿真集合与打包集合是两份独立的列表，由各自脚本逐条 `source` 决定。`power_sink` 因「功耗不可仿真」没有 self-checking TB 而不参与仿真，但它**可以**作为 IP 被打包出来、喂给功耗分析工具。事实正相反——`power_sink` 在 `packageAllIp.tcl` 里是被打包的（[第 32-33 行](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/scripts/packageAllIp.tcl#L32-L33)）。所以一个 IP 在一处的状态不能推断另一处。

**练习 3**：打包段的成败判据，和仿真段的 `###ERROR###` 判据有何不同？

> **答案**：仿真段有一个统一的 `run_check_errors "###ERROR###"`，扫到这个标志即判失败，口径明确。打包段没有等价的统一错误标志——每个 `package.tcl` 是独立执行的，成败要看各自日志里 Vivado 打包是否报错。所以在「操作手册」里，仿真段的判据可以一句话写清（无 `###ERROR###` 即通过），打包段则需逐个 IP 看日志。

---

## 5. 综合实践

把三段串起来，产出本讲要求的**「操作手册」**：一份按顺序驱动整个集合仓库的清单，每一步都写明命令与成功/失败判据。

**场景**：新同事入职，需要从零把 psi_fpga_all 拉下来，跑一遍全部库的仿真自检，再批量打包全部 Vivado IP。请你给他写一份操作手册。

**任务清单**：

1. **第一步——克隆**：写出完整的递归克隆命令（SSH、HTTPS 两条），并说明成功标志（`scripts/`、`TCL/PsiSim/`、各 `VHDL/*/sim/`、各 `VivadoIp/*/scripts/` 均非空）。
2. **第二步——仿真自检**：说明 source 之前必须先 `cd scripts/`；分别写出在 ModelSim / GHDL / Vivado 三种 Tcl 控制台里各应 source 哪个脚本；写出统一的成功/失败判据（日志中**不**出现 `###ERROR###` 即通过）。
3. **第三步——IP 打包**：写出在 Vivado Tcl 控制台里 source 哪个脚本来批量打包 IP；说明打包段无统一错误标志、需逐个看日志，并指出 `power_sink` 在此处会被打包（与仿真段不同）。
4. **顺序与前提**：用一句话说明为什么三步的顺序不能乱（克隆是素材前提；仿真与打包都依赖 `pwd == scripts/` 与完整目录）。

**参考答案（操作手册要点）**：

```text
【前提】完整运行需在已克隆全部 submodule、且装有对应 EDA 工具的环境中进行。

【第 1 步：克隆得到完整目录】
  SSH   : git clone --recurse-submodules git@github.com:paulscherrerinstitute/psi_fpga_all.git
  HTTPS : git clone --recurse-submodules https://www.github.com/paulscherrerinstitute/psi_fpga_all.git
  成功标志：scripts/ 有 4 个 .tcl；TCL/PsiSim/、VHDL/*/sim/、VivadoIp/*/scripts/ 均非空。
  （漏了 --recurse-submodules 则补：git submodule update --init --recursive）

【第 2 步：仿真自检】
  先在 Tcl 控制台执行：cd <仓库根>/scripts
  ModelSim 后端：source runModelsim.tcl
  GHDL 后端    ：source runGhdl.tcl
  Vivado 后端  ：source runVivado.tcl
  成功判据：run_check_errors "###ERROR###" 不在日志中命中 ###ERROR### → 通过；
           一旦出现 ###ERROR### → 有 self-checking TB 失败。
  注意：三脚本均假设 pwd==scripts/；跑完≠通过，以 ###ERROR### 为准。

【第 3 步：批量打包 Vivado IP】
  在 Vivado Tcl 控制台执行：cd <仓库根>/scripts ; source packageAllIp.tcl
  成功判据：无统一错误标志，逐个 package.tcl 看日志；
           power_sink 此处会被打包（与仿真段相反）。

【顺序】克隆（备素材）→ 仿真（自检）→ 打包（出 IP）；
       后两步都依赖完整目录与 pwd==scripts/，故克隆必须先行。
```

**验收要点**：手册中三步顺序固定、命令准确、每步都有可观察的判据；特别要写明仿真段的 `###ERROR###` 判据与「跑完≠通过」、以及打包段集合与仿真段集合的独立性（`power_sink` 反例）。

## 6. 本讲小结

- 端到端工作流是三段的固定链条：**克隆（`--recurse-submodules`）→ 仿真自检（source `run*.tcl`）→ IP 打包（source `packageAllIp.tcl`）**；克隆是后两段的素材前提，顺序不能乱。
- 克隆命令在 `README.md` 的 `Cloning` 章节给出，SSH 与 HTTPS 两条仅协议前缀不同；子模块用相对 URL 自动继承协议，故两条命令都零配置可用。
- 仿真段三个脚本对应三种后端：`runModelsim.tcl`（`init` 默认）、`runGhdl.tcl`（`init -ghdl`）、`runVivado.tcl`（`init -vivado`）；它们共享 PsiSim 的 init→configure→compile→run→check 流水线。
- 仿真段的**成功/失败判据是统一的**：`run_check_errors "###ERROR###"` 扫日志，**不出现** `###ERROR###` 才算通过——「跑完」不等于「通过」。
- 四个驱动脚本有一个共同的隐藏前提：**运行时 `pwd` 必须是 `scripts/`**（由 `set myPath [pwd]` + `$myPath/../...` 决定），source 前必须先 `cd scripts/`。
- 打包段不加载 PsiSim、不复用仿真流水线，且**打包集合 ≠ 仿真集合**：`power_sink` 不仿真却打包、`axi_mm_reader` 能仿真却不打包，证明一个 IP 在一处的状态不能推断另一处。

## 7. 下一步学习建议

- 至此整本手册的「克隆 → 目录 → 脚本 → 仿真 → 打包 → 版本管理 → 维护 → 端到端」主线已闭环。建议回头重读 [u3-l1（发布管理与版本固定）](u3-l1-release-and-version-pinning.md)，把「`--recurse-submodules` 拉到的为何是一组协调一致的快照、而非各库最新」这条认知补进工作流的第一步。
- 若你要在真实项目里落地，下一步是进入各 submodule 内部：读 `TCL/PsiSim/` 里的 `PsiSim.tcl` 搞清 `init/compile_files/run_tb/run_check_errors` 的真实实现，读某个 `VivadoIp/vivadoIP_*/scripts/package.tcl` 搞清 IP 打包细节。这些内容本仓库不含，需到对应子模块仓库中阅读（**待确认**）。
- 想把仿真纳入 CI，可研究 PsiSim 是否支持命令行批跑（而非仅在 EDA Tcl 控制台 source），并参照 [u3-l2](u3-l2-maintaining-and-extending.md) 的 `.gitignore` 白名单策略处理仿真产物的版本控制问题。
