# 获取、运行与配置 PoC

> 本讲属于「入门层 · 第 1 单元」，承接 [u1-l1 项目总览](u1-l1-project-overview.md) 与 [u1-l2 目录结构解析](u1-l2-directory-structure.md)。
> 前两讲建立了「PoC 是什么」与「文件放在哪里」的认知；本讲回答第三个问题：**怎么把它拉到本地、怎么启动它的命令行、怎么完成最小配置**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 `git clone --recursive` 正确获取 PoC，并解释为什么必须是 `--recursive`。
2. 说清楚 `poc.sh`（Linux/macOS）和 `poc.ps1`（Windows）这两个脚本是什么、各自把命令委托给了谁。
3. 从 `.template` 模板复制出 `my_config.vhdl` 和 `my_project.vhdl`，并填好其中的全局常量。
4. 回答一个关键问题：**你在模板里填写的那些常量，最终被谁读取、用来做什么**。

本讲只覆盖「下载 + 入口脚本 + 配置模板」这三件事，不展开配置机制的内部原理——那是 [u2-l3 配置机制：my_config 与 config 包](u2-l3-config-mechanism.md) 的主题。

## 2. 前置知识

本讲默认你已经从 [u1-l1](u1-l1-project-overview.md) 和 [u1-l2](u1-l2-directory-structure.md) 了解了下面几个事实，这里只做一句话复习，并补两个新概念：

- PoC 是一个以 VHDL/Verilog 源码形式交付的硬件 IP 核库。
- 仓库顶层有若干目录：`src`（源码）、`tb`（测试台）、`lib`（第三方库）、`ucf`（约束）、`xst`/`netlist`（综合）等。
- `lib/` 里的第三方库是**以 git submodule（子模块）形式嵌入**的，所以克隆时必须带上子模块。
- PoC 自带两个模板文件 `my_config` 与 `my_project`，用于声明目标板和项目环境。

本讲新引入两个概念：

- **git submodule（子模块）**：在一个 git 仓库内部嵌套引用另一个 git 仓库的技术。普通 `git clone` 不会把子模块的内容拉下来，目录会「存在但为空」。
- **wrapper script（包装脚本）**：一层很薄的脚本，它自己不做核心工作，只负责保存环境、解析路径，然后把真正的执行委托给另一个程序。PoC 的 `poc.sh` / `poc.ps1` 就是这种包装脚本，背后真正的引擎是 Python 基础设施 **pyIPCMI**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md) | 官方「Quick Start Guide」，给出下载、配置、集成的权威步骤。 |
| [poc.sh](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh) | Linux/macOS 下的 Bash 入口包装脚本。 |
| [poc.ps1](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1) | Windows 下的 PowerShell 入口包装脚本。 |
| [requirements.txt](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/requirements.txt) | Python 基础设施依赖的第三方包清单。 |
| [.gitmodules](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules) | 子模块清单，解释「为什么要 `--recursive`」。 |
| [src/common/my_config.vhdl.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template) | 目标板/器件配置模板（`MY_BOARD`、`MY_DEVICE`）。 |
| [src/common/my_project.vhdl.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_project.vhdl.template) | 项目目录/操作系统配置模板（`MY_PROJECT_DIR`、`MY_OPERATING_SYSTEM`）。 |
| [src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl) | 真正**读取**上述常量的 VHDL 包，本讲用于验证「谁在消费这些常量」。 |

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**下载与子模块**、**入口脚本**、**配置模板**。

### 4.1 下载与子模块

#### 4.1.1 概念说明

PoC 不只是一个 git 仓库，它还「引用」了一组外部仓库——主要是验证方法学库（OSVVM、UVVM、VUnit、cocotb）和驱动整个工具链的 Python 基础设施（pyIPCMI）。这些外部仓库都放在 `lib/` 下，并以 **git submodule** 的方式登记在根目录的 `.gitmodules` 文件里。

子模块的特点是：当你用普通的 `git clone` 拉取 PoC 时，子模块目录会被创建，但**里面是空的**——因为子模块的真正内容需要额外一步（`git submodule update --init`）才能下载。这就是为什么官方下载命令必须带 `--recursive`：它会递归地把所有子模块也一并拉下来。

如果没有 `--recursive`，最直接的后果就是 `lib/pyIPCMI/` 是空的，于是 `poc.sh` / `poc.ps1` 根本找不到要委托的 Python 引擎，整个命令行基础设施无法启动。

#### 4.1.2 核心流程

最小下载流程：

```text
git clone --recursive <URL> PoC
        │
        ├── 1) 拉取 PoC 主仓库
        └── 2) 递归拉取 .gitmodules 登记的全部子模块
                    (lib/pyIPCMI, lib/osvvm, lib/uvvm, lib/vunit, lib/cocotb, ...)
```

下载完成后，PoC 把这个安装目录称为 **`PoCRoot`**。如果之前已经用普通 `clone` 拉过、忘了带 `--recursive`，也可以事后补救：

```bash
cd PoC
git submodule update --init --recursive
```

> 提示：本仓库 `VLSI-EDA/PoC` 已是历史快照（详见 [u1-l1](u1-l1-project-overview.md)），克隆时仍按官方命令即可，本讲以其当前 HEAD 为准。

#### 4.1.3 源码精读

官方在 README 的 Download 一节给出了两条等价的克隆命令（HTTPS 与 SSH），都带了 `--recursive`：

README 下载命令表（[README.md:121-124](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L121-L124)）——说明克隆协议可选，但 `--recursive` 是必须的。

那「递归」到底会拉哪些子模块？答案在 `.gitmodules`。其中和命令行最相关的是 pyIPCMI：

pyIPCMI 子模块登记（[.gitmodules:16-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L16-L18)）——把 `lib/pyIPCMI` 指向上游 pyIPCMI 仓库。整份 `.gitmodules` 还登记了 `lib/vunit`、`lib/osvvm`、`lib/cocotb`、`lib/uvvm` 以及一个文档主题，共 6 个子模块。

#### 4.1.4 代码实践

1. **实践目标**：直观看到「带不带 `--recursive` 的区别」。
2. **操作步骤**：
   - 执行 `git clone --recursive https://github.com/VLSI-EDA/PoC.git PoC`。
   - 进入目录，用 `ls lib/pyIPCMI` 查看 pyIPCMI 子模块是否有内容。
3. **需要观察的现象**：`lib/pyIPCMI/` 下应能看到 `pyIPCMI.sh`、`pyIPCMI.psm1` 等文件；`lib/osvvm`、`lib/uvvm` 等也非空。
4. **预期结果**：若忘记 `--recursive`，`lib/pyIPCMI/` 为空目录；补跑 `git submodule update --init --recursive` 后恢复正常。
5. 待本地验证（取决于你的网络能否访问各子模块上游仓库）。

#### 4.1.5 小练习与答案

- **练习 1**：如果不带 `--recursive` 克隆，`git status` 会不会报错？
  - **答案**：不会报错。子模块目录会被创建但为空，需要你主动意识到并补跑 `git submodule update --init --recursive`——这正是 `--recursive` 容易被新手忽略的陷阱。
- **练习 2**：`lib/` 下一共有几个第三方库子模块？分别是什么？
  - **答案**：5 个库相关子模块：`lib/pyIPCMI`（基础设施）、`lib/osvvm`、`lib/uvvm`、`lib/vunit`、`lib/cocotb`（验证方法学/协仿真）；另外 `.gitmodules` 还登记了一个文档主题 `docs/_themes/sphinx_rtd_theme`，不属于 `lib/`。

### 4.2 入口脚本

#### 4.2.1 概念说明

PoC 想同时支持多家厂商的仿真器/综合器、Windows/Linux/macOS 三种系统。为了不把平台差异暴露给使用者，它选了一条统一路线：**用 Python 3 写一套平台无关的命令行基础设施（pyIPCMI），再用两个极薄的 shell 脚本把它包装起来**。

- 在 Linux/macOS 上，入口是 Bash 脚本 `poc.sh`。
- 在 Windows 上，入口是 PowerShell 脚本 `poc.ps1`。

这两个脚本都叫「wrapper（包装脚本）」：它们几乎不包含业务逻辑，职责只有三件——**保存当前工作目录、解析脚本自身所在路径、把控制权交给 pyIPCMI**。README 把这套设计总结为：PoC 用 Python 3 作为平台无关的脚本环境，再用 Bash/PowerShell 脚本隐藏 Darwin/Linux/Windows 的平台差异。

运行 PoC 的命令行还需要一点 Python 依赖，记录在 `requirements.txt` 里。

#### 4.2.2 核心流程

两条入口脚本各自的委托链：

```text
【Linux / macOS】
./poc.sh configure
   │  1) 保存 $@（参数）与 $(pwd)（当前工作目录）
   │  2) 解析脚本自身目录 → 得到 PoCRoot
   └─► source  lib/pyIPCMI/pyIPCMI.sh      ← 真正的引擎
                │
                └─► 驱动 Python 前端执行子命令（configure / simulate / ...）

【Windows】
.\poc.ps1 configure
   │  1) 记录 Get-Location（当前工作目录）
   │  2) Import-Module lib\pyIPCMI\pyIPCMI.psm1
   └─► 用模块提供的 $Python_Interpreter 运行 Python 前端
                │
                └─► 执行子命令
```

无论走哪条路，最终都是「shell 脚本 → pyIPCMI（Python）→ 具体工具链」。这就是为什么 `lib/pyIPCMI` 必须存在——它是两条链的共同终点。

#### 4.2.3 源码精读

先看 `poc.sh` 顶部的配置区，这里声明了「库叫 PoC」「pyIPCMI 在 `lib/pyIPCMI`」等基本信息：

poc.sh 配置变量（[poc.sh:40-48](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L40-L48)）——`Library="PoC"`、`pyIPCMI_Dir="lib/pyIPCMI"`，决定了后续委托的目标目录。

脚本主体在末尾，做了「保存参数与工作目录 → source pyIPCMI 的 Bash 模块」两步：

poc.sh 保存环境并委托（[poc.sh:62-72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L62-L72)）——其中 `source "$Library_RootDirectory/$pyIPCMI_Dir/$pyIPCMI_BashModule.sh"`（即 `lib/pyIPCMI/pyIPCMI.sh`）就是把控制权交给 Python 基础设施的那一行。

Windows 的 `poc.ps1` 思路相同，只是用 PowerShell 语法。它先导入 pyIPCMI 的 PowerShell 模块：

poc.ps1 导入 pyIPCMI 模块（[poc.ps1:51-57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L51-L57)）——`Import-Module "...pyIPCMI.psm1"`，并把库根目录、库名等参数传进去。

随后用模块提供的解释器拼出 Python 命令并执行：

poc.ps1 构造并执行 Python 命令（[poc.ps1:86-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L86-L93)）——`$Command = "$Python_Interpreter ... $pyIPCMI_FrontEndPy $args"`，再用 `Invoke-Expression $Command` 真正运行。

> 源码阅读小提示：`poc.ps1` 头部的注释里写着「This is a bash wrapper script」（[poc.ps1:12](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L12)），这其实是历史复制粘贴的残留——它本身是 PowerShell 脚本。看源码时要以**实际代码**为准，注释偶尔会滞后。

最后是 Python 依赖清单，非常简短：

requirements.txt 内容（[requirements.txt:1-2](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/requirements.txt#L1-L2)）——只要求 `colorama`（跨平台彩色终端输出）和 `py-flags`（命令行参数解析辅助）。说明 pyIPCMI 自身的依赖很轻量。

#### 4.2.4 代码实践

1. **实践目标**：沿 `poc.sh` 的真实执行路径走一遍，确认「它最终落到 `lib/pyIPCMI`」。
2. **操作步骤**：
   - 打开 `poc.sh`，定位第 40–48 行的配置变量与第 69 行的 `source`。
   - 不实际运行，而是推断：如果 `lib/pyIPCMI/pyIPCMI.sh` 不存在，第 69 行会怎样？
3. **需要观察的现象**：第 69 行 `source` 的目标路径展开后是 `<PoCRoot>/lib/pyIPCMI/pyIPCMI.sh`。
4. **预期结果**：若该文件不存在（即子模块没拉下来），`source` 会报「No such file」，这正是 4.1 节强调 `--recursive` 的原因——两条入口链都依赖 pyIPCMI 子模块存在。
5. 待本地验证（你也可以装好 Python 3 与 `requirements.txt` 依赖后，运行 `./poc.sh --help` 观察是否打印出 pyIPCMI 的帮助）。

#### 4.2.5 小练习与答案

- **练习 1**：`poc.sh` 为什么要保存 `$(pwd)`（当前工作目录）？
  - **答案**：因为 pyIPCMI 的很多子命令（如综合、仿真）需要知道「你在哪个项目目录里调用它」，而后续 Python 程序的工作目录可能被改变；先把调用者所在目录记下来，才能正确解析相对路径与项目配置。
- **练习 2**：`poc.sh` 与 `poc.ps1` 谁是「真正干活的人」？
  - **答案**：都不是。两者都只是 wrapper，真正干活的是 `lib/pyIPCMI` 提供的 Python 基础设施；脚本只负责把命令安全地交到 Python 手里。

### 4.3 配置模板

#### 4.3.1 概念说明

光把仓库拉下来还不够。PoC 的很多 IP 核有「多家厂商、多种实现」的版本（例如同一功能在 Xilinx、Altera、Lattice 上用不同底层原语实现）。为了在编译期自动选对实现，PoC 需要你告诉它两件事：

1. **目标硬件**：你在为哪块板、哪颗器件综合？（由 `my_config.vhdl` 回答）
2. **宿主环境**：你的项目目录在哪、跑在什么操作系统上？（由 `my_project.vhdl` 回答）

这两个文件在仓库里只提供 **模板**：`my_config.vhdl.template` 与 `my_project.vhdl.template`。你需要把它们**复制成不带 `.template` 后缀的同名文件**，再编辑其中的几个全局 VHDL 常量。模板里的默认值都是 `"CHANGE THIS"`，就是提醒你「这里必须改」。

这些常量是「全局」的：它们被 PoC 公共包 `config` 读取，进而派生出 `VENDOR`（厂商）、`DEVICE_INFO`（器件信息）等，供后续所有核使用。完整的派生机制留到 [u2-l3](u2-l3-config-mechanism.md)，本讲只确认「谁在读它们」。

#### 4.3.2 核心流程

最小配置流程（以 Linux 为例，Windows 把 `cp` 换成 `copy`、路径分隔符换 `\`）：

```text
1) cp src/common/my_config.vhdl.template  src/common/my_config.vhdl
2) cp src/common/my_project.vhdl.template src/common/my_project.vhdl
3) 编辑这两个 .vhdl 文件，把 "CHANGE THIS" 改成真实值
4) 把它们加入综合/仿真工具的 PoC 库一起编译
        │
        └─► config.vhdl 通过 use PoC.my_config.all / use PoC.my_project.all
            读取这些常量 → 派生厂商与器件信息
```

模板里需要改的常量一共四个：

| 文件 | 常量 | 含义 | 示例值 |
| --- | --- | --- | --- |
| `my_config.vhdl` | `MY_BOARD` | 目标开发板名 | `Custom`、`ML505`、`KC705`、`Atlys` |
| `my_config.vhdl` | `MY_DEVICE` | 具体器件型号（可填 `None`） | `None`、`XC5VLX50T-1FF1136`、`EP2SGX90FF1508C3` |
| `my_project.vhdl` | `MY_PROJECT_DIR` | 项目根目录（绝对路径，含末尾斜杠） | `/home/me/projects/myproject/` |
| `my_project.vhdl` | `MY_OPERATING_SYSTEM` | 宿主操作系统 | `WINDOWS`、`LINUX` |

#### 4.3.3 源码精读

先看 `my_config.vhdl.template` 的实体——一个 VHDL `package`，里面就是几个字符串常量：

my_config.vhdl.template 的常量（[src/common/my_config.vhdl.template:46-53](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template#L46-L53)）——定义 `MY_BOARD`、`MY_DEVICE` 两个字符串常量（默认 `"CHANGE THIS"`），以及一个 `MY_VERBOSE`（控制详细报告输出，内部用）。模板头部的注释明确写了用法：复制、改名、加入 PoC 库、修改设置。

再看 `my_project.vhdl.template`，结构与上面完全对称：

my_project.vhdl.template 的常量（[src/common/my_project.vhdl.template:43-47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_project.vhdl.template#L43-L47)）——定义 `MY_PROJECT_DIR` 和 `MY_OPERATING_SYSTEM` 两个字符串常量。

官方 README 的「Creating my_config/my_project」一节给出了完整的复制命令和需要修改的常量，可作为权威参照：

README 创建配置文件的步骤（[README.md:186-217](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L186-L217)）——包含 `cp` 复制命令、`MY_BOARD`/`MY_DEVICE`/`MY_PROJECT_DIR`/`MY_OPERATING_SYSTEM` 四个常量的示例值。

**那么，这些常量到底被谁读取？** 答案是公共包 `config`。它在文件开头就 `use` 了这两个包：

config.vhdl 导入并消费模板常量（[src/common/config.vhdl:376-386](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L376-L386)）——`use PoC.my_config.all;`、`use PoC.my_project.all;`，然后立刻把 `MY_PROJECT_DIR` / `MY_OPERATING_SYSTEM` 赋给 `PROJECT_DIR` / `OPERATING_SYSTEM` 两个对内对外的常量。

对于 `MY_BOARD` 与 `MY_DEVICE`，`config.vhdl` 用一个解析函数处理二者的优先级——器件串可以来自调用点临时指定、或 `MY_DEVICE`、或由 `MY_BOARD` 派生：

config.vhdl 的器件串解析 getLocalDeviceString（[src/common/config.vhdl:657-678](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L657-L678)）——逻辑是：优先用调用点传入的 `DeviceString`；否则若 `MY_DEVICE` 非空且不为 `"None"`，就用 `MY_DEVICE`；**否则回退到由 `MY_BOARD` 派生的器件串**（`MY_DEVICE_STR := BOARD_DEVICE`，见第 659 行）。

这段逻辑直接决定了本讲实践任务里 `MY_DEVICE=None` 的行为：因为 `MY_DEVICE` 等于 `"None"`，第 671 行的条件 `not str_imatch(MY_DEVICE, "None")` 为假，于是跳过 `MY_DEVICE` 分支，回退到由 `MY_BOARD`（即 `Custom`）派生器件信息。换句话说，**`MY_DEVICE=None` 的语义是「不显式指定器件，请根据板子推断」**。

> 补充：`MY_OPERATING_SYSTEM` 除了被 `config.vhdl` 读取外，还会被文件 IO 包用来决定换行符（Linux 用 `\n`、Windows 用 `\r\n`），见 [src/common/fileio.v08.vhdl:83](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/fileio.v08.vhdl#L83)（`.v93` 版本同样）。这些深入细节留给后续单元。

#### 4.3.4 代码实践（本讲主任务）

> 这正是本讲规格里要求的实践。

1. **实践目标**：从模板创建可用的 `my_config.vhdl`，并准确说明 `MY_BOARD` / `MY_DEVICE` 被谁读取。
2. **操作步骤**：
   ```bash
   cd PoCRoot
   cp src/common/my_config.vhdl.template  src/common/my_config.vhdl
   cp src/common/my_project.vhdl.template src/common/my_project.vhdl
   ```
   然后编辑 `src/common/my_config.vhdl`，把两个常量改为：
   ```vhdl
   constant MY_BOARD  : string := "Custom";
   constant MY_DEVICE : string := "None";
   ```
   （再把 `my_project.vhdl` 里的 `MY_PROJECT_DIR` 改成你自己的项目绝对路径、`MY_OPERATING_SYSTEM` 改成 `"LINUX"` 或 `"WINDOWS"`。）
3. **需要观察的现象**：保存后，文件名不再带 `.template`；四个常量都已脱离 `"CHANGE THIS"`。
4. **预期结果 / 回答「被谁读取」**：
   - `MY_BOARD`、`MY_DEVICE`、`MY_VERBOSE` 由公共包 `config`（[src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl)）通过 `use PoC.my_config.all;` 读取（[config.vhdl:377](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L377)），用来派生厂商（`VENDOR_XILINX` / `VENDOR_ALTERA` / ...）与器件信息，供后续 IP 核在编译期选择实现。
   - `MY_PROJECT_DIR`、`MY_OPERATING_SYSTEM` 同样被 `config` 通过 `use PoC.my_project.all;` 读取（[config.vhdl:378](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L378)），在 [config.vhdl:384-385](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L384-L385) 赋给 `PROJECT_DIR` / `OPERATING_SYSTEM`。
   - 因为 `MY_DEVICE = "None"`，`config` 会忽略 `MY_DEVICE`，转而从 `MY_BOARD = "Custom"` 派生器件信息（见上文 `getLocalDeviceString` 的回退分支）。`Custom` 表示「不针对具体厂商板卡的通用目标」。
5. 待本地验证（真正编译时是否生效，取决于你是否把这两个 `.vhdl` 加入了 `PoC` 库）。

#### 4.3.5 小练习与答案

- **练习 1**：为什么不直接编辑 `.template` 文件，而要先复制改名？
  - **答案**：`.template` 是仓库自带的模板，会被 git 跟踪；复制成不带后缀的 `my_config.vhdl` / `my_project.vhdl` 后，这些文件通常被 `.gitignore` 忽略，属于你本机/本项目的私有配置，不会被提交回去，也不会在升级 PoC 时被覆盖。
- **练习 2**：把 `MY_DEVICE` 设成 `"None"`、`MY_BOARD` 设成 `"Custom"`，综合时会发生什么？
  - **答案**：`config` 解析器件串时会跳过 `MY_DEVICE`（因为是 `"None"`），回退到由 `MY_BOARD` 派生；`Custom` 代表通用/非厂商特定目标，通常用于只想做功能仿真、不绑定具体 FPGA 器件的场景。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从零到可调用」的最小上手：

1. **下载**：`git clone --recursive https://github.com/VLSI-EDA/PoC.git PoC`，进入 `PoC` 目录，确认 `lib/pyIPCMI/` 非空。
2. **准备 Python 环境**：安装 Python 3，并按 [requirements.txt](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/requirements.txt) 安装依赖（`pip install -r requirements.txt`）。
3. **启动配置流程**：运行 `./poc.sh configure`（Linux/macOS）或 `.\poc.ps1 configure`（Windows），按屏幕提示操作（`Y` 接受、`N` 拒绝、`P` 跳过、回车采纳方括号里的默认值）。这一步对应 README 的 [2.3 Configuring PoC on a Local System](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L129-L145)。
4. **创建配置文件**：按 4.3.4 节复制并填写 `my_config.vhdl`（`MY_BOARD="Custom"`、`MY_DEVICE="None"`）与 `my_project.vhdl`。
5. **画一张「调用与读取」关系图**，要求包含：
   - `poc.sh` / `poc.ps1` → `lib/pyIPCMI`（Python 引擎）的委托箭头；
   - `my_config.vhdl` / `my_project.vhdl` → `config.vhdl`（`use ... .all`）的读取箭头；
   - 标注「`MY_DEVICE=None` 时回退到 `MY_BOARD`」这一分支。

完成后，你就掌握了 PoC 的「下载 → 启动 → 配置」最小闭环；剩下的「`config` 内部如何把器件串拆解成 `VENDOR` 与 `DEVICE_INFO`」留给 [u2-l3](u2-l3-config-mechanism.md)。

## 6. 本讲小结

- PoC 用 **git submodule** 嵌入第三方库与 pyIPCMI，所以必须用 `git clone --recursive` 下载，否则 `lib/pyIPCMI/` 为空、命令行无法启动。
- `poc.sh`（Bash）和 `poc.ps1`（PowerShell）都是**包装脚本**，只负责保存环境与解析路径，把真正的执行委托给 Python 基础设施 **pyIPCMI**。
- pyIPCMI 自身依赖很轻，仅需 `colorama` 和 `py-flags`（见 `requirements.txt`）。
- 配置靠两个模板：`my_config.vhdl`（`MY_BOARD`、`MY_DEVICE`）描述目标硬件，`my_project.vhdl`（`MY_PROJECT_DIR`、`MY_OPERATING_SYSTEM`）描述宿主环境。
- 使用方式是「复制改名、去掉 `.template` 后缀、改掉 `"CHANGE THIS"`」；这些文件属本地私有配置，不应回填仓库。
- 这四个常量最终都由公共包 `config`（`src/common/config.vhdl`）通过 `use PoC.my_config.all` / `use PoC.my_project.all` 读取，用来派生厂商与器件信息；当 `MY_DEVICE="None"` 时会回退到由 `MY_BOARD` 派生。

## 7. 下一步学习建议

- 想看清「`MY_BOARD` / `MY_DEVICE` 如何变成 `VENDOR` 与 `DEVICE_INFO`」：直接进入 [u2-l3 配置机制：my_config 与 config 包](u2-l3-config-mechanism.md)，那里会逐行拆解 `config.vhdl` 的器件串解析。
- 想了解这些公共包整体如何被引用：先读 [u2-l1 公共包总览与 Common 上下文](u2-l1-common-packages-overview.md)，理解 `context Common` 与 `.files` 编译清单。
- 对 `poc.sh` / `poc.ps1` 背后的 Python 引擎感兴趣：留到 [u5-l1 pyIPCMI 基础设施与命令行前端](u5-l1-pyipcmi-infrastructure.md) 深入。
- 顺手阅读建议：通读一遍 [README.md 的 Quick Start Guide](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md)（2.1–2.5 节），与本讲对照，能加深对「官方推荐流程」的整体印象。
