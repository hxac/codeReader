# pyIPCMI 基础设施与命令行前端

## 1. 本讲目标

PoC 的几百个 IP 核之上，还架着一层「让这一切能在任意厂商工具链下跑起来」的软件基础设施。本讲带你从命令行一直追到这层基础设施的入口，学完后你应当能够：

- 说清 `poc.sh` / `poc.ps1` 这两个极薄的入口脚本如何把控制权**委托**给 `lib/pyIPCMI` 子模块。
- 理解 pyIPCMI 是一个以 **git submodule** 形式嵌入的 Python 项目，为什么必须 `--recursive` 克隆。
- 看懂 `.files` 这种「不是 VHDL、却被 pyIPCMI 当成编译清单消费」的描述语言，并解释它在仿真/综合流程里的角色。
- 理解 pyIPCMI 如何用一组**条件变量**（`ToolChain` / `Tool` / `BoardName` / `VHDLVersion` / `Environment`）把 Xilinx、Altera、Lattice、GHDL、ModelSim 等迥异的工具链抽象成统一模型。

本讲是第 5 单元（专家层）的开篇：前面四单元讲的是「VHDL 里写了什么」，从本讲起转向「PoC 这个项目作为一个整体如何被工具链驱动」。

## 2. 前置知识

在进入源码前，先用大白话理清三个概念。

- **包装脚本（wrapper script）**：用户在终端敲的命令（如 `./poc.sh configure`）并不会直接启动一个庞大的程序。PoC 故意把入口写成几行 shell，这几行 shell 只做「记住我从哪里来、记住我的参数，然后把活儿交给真正干活的程序」。这种「只转发、不干活」的脚本就是 wrapper。
- **git submodule（子模块）**：一个 git 仓库可以把**另一个独立的 git 仓库**挂在自己某个子目录下，挂载点记录的是那个外部仓库的某一次提交，而不是它的完整历史。克隆父仓库时默认不会自动拉取子模块内容，于是子目录看起来是「空的」，必须额外执行初始化。PoC 用这个机制把第三方库和 pyIPCMI 都挂到 `lib/` 下。
- **工具链（tool chain）**：把 VHDL 变成可仿真或可烧录结果所需的一整套厂商软件，例如 Xilinx Vivado、Altera（Intel）Quartus、Lattice Diamond，或开源的 GHDL。每家的命令行、原语库、约束语法都不一样，PoC 需要一层抽象把它们统一起来——这就是 pyIPCMI 存在的根本理由。

如果这三个概念还模糊，没关系，下面的源码精读会反复回到它们。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [poc.sh](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh) | Linux/macOS 下的 Bash 入口包装脚本，委托给 pyIPCMI。 |
| [poc.ps1](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1) | Windows 下的 PowerShell 入口包装脚本，委托给 pyIPCMI。 |
| [.gitmodules](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules) | 声明 `lib/pyIPCMI` 等 6 个子模块的挂载点与上游 URL。 |
| [lib/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md) | 说明第三方库为何以 submodule 形式提供，以及如何手动初始化。 |
| [requirements.txt](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/requirements.txt) | pyIPCMI 的 Python 依赖（仅 colorama、py-flags）。 |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | 公共包的 `.files` 编译清单，是 pyIPCMI 消费的源码依赖描述。 |
| [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) | 板级配置的 `.files`，集中展示了 `BoardName`/`Tool` 条件分支。 |
| [src/sim/sim.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files) | 仿真包的 `.files`，展示了 `ToolChain`/`VHDLVersion` 条件。 |
| [README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md) | 官方对「Python 基础设施提供命令行前端」的总述。 |

> 说明：`lib/pyIPCMI/` 这个子模块在本讲所用仓库快照里**尚未被 checkout**（目录为空），因此 pyIPCMI 自身的 Python 源码（`pyIPCMI.py`、`pyIPCMI.sh`、`pyIPCMI.psm1` 等）不在本仓库内，无法直接引用其行号。本讲只对「PoC 仓库这一侧能看到的委托边界」做精确源码引用，对子模块内部行为只做基于命名与文档的合理推断，并明确标注。

## 4. 核心概念与源码讲解

### 4.1 入口委托链：从 poc.sh / poc.ps1 到 pyIPCMI

#### 4.1.1 概念说明

PoC 想让用户在任意操作系统上用同一条命令（`poc configure`、`poc --simulation ...` 等）驱动仿真与综合。但 Python 解释器在 Linux/macOS（Bash）和 Windows（PowerShell）下的启动方式、路径分隔符、环境变量语法都不一样。PoC 的解法是：**为每个操作系统写一个极薄的本地脚本**，它只负责吸收这些平台差异，然后把同一份参数原封不动地交给跨平台的 pyIPCMI。这条「用户命令 → 本地脚本 → pyIPCMI」的路径，就是委托链（delegation chain）。

#### 4.1.2 核心流程

两条委托链的形状几乎对称：

```text
Linux / macOS                                    Windows
─────────────                                    ───────
用户: ./poc.sh <args>                             用户: .\poc.ps1 <args>
  │                                                │
  ├─ 解析脚本自身所在目录 (SCRIPT_DIR)              ├─ 解析脚本自身所在目录 ($PSScriptRoot)
  ├─ 记录工作目录与参数                            ├─ 记录工作目录与参数
  ├─ 推算 PoC 根目录 (Library_RootDirectory)       ├─ 推算 PoC 根目录 ($Library_RootDirectory)
  │                                                │
  └─ source lib/pyIPCMI/pyIPCMI.sh  ──┐            └─ Import-Module lib\pyIPCMI\pyIPCMI.psm1 ──┐
                                       │                                                            │
                                       └────────────►  pyIPCMI (Python)  ◄───────────────────────┘
                                                           │
                                                           └─ 找到 Python 解释器，运行真正的命令行前端
```

关键点：两个脚本都不「执行 PoC 的业务」，它们只做定位与转发。真正的业务逻辑在子模块里的 Python 代码中。

#### 4.1.3 源码精读

**Bash 侧：poc.sh**

脚本顶部先声明一组「可配置项」，把 PoC 根目录相对路径、库名、以及 pyIPCMI 的目录与模块名写死成变量，方便日后被其它基于 pyIPCMI 的项目复用：

[poc.sh:39-47](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L39-L47) —— 定义 `Library="PoC"`、`pyIPCMI_Dir="lib/pyIPCMI"`、`pyIPCMI_BashModule="pyIPCMI"` 等委托参数。

接着是一段经典的「解析脚本自身真实路径」逻辑（处理符号链接，并对 macOS 的 `readlink` 缺斤少两做兼容）：

[poc.sh:50-60](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L50-L60) —— 在 Darwin 上把 `readlink` 换成 `greadlink`，再用 `while [ -h ... ]` 循环解开符号链接，得到脚本真正所在的 `SCRIPT_DIR`。这一步是为了哪怕用户通过软链调用 `poc.sh`，也能正确定位 PoC 根目录。

然后把「用户给的参数」「当前工作目录」「PoC 根目录」三样东西保存下来，这是委托时要传递的上下文：

[poc.sh:62-66](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L62-L66) —— `Wrapper_Parameters=$@`、`Wrapper_WorkingDirectory=$(pwd)`，并据此算出绝对的 `Library_RootDirectory`。

最后是真正的「委托」动作——一行 `source`：

[poc.sh:69](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L69) —— `source "$Library_RootDirectory/$pyIPCMI_Dir/$pyIPCMI_BashModule.sh"`，展开就是 `source <PoC根>/lib/pyIPCMI/pyIPCMI.sh`。注意是 `source` 而非 `bash`：被引入的脚本和当前脚本共享同一套变量（如上面保存的 `Wrapper_WorkingDirectory`），所以 pyIPCMI 那一侧能直接读到这些上下文。

[poc.sh:72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L72) —— `exit $PoC_ExitCode`：pyIPCMI 侧负责把退出码写到 `PoC_ExitCode` 变量，poc.sh 只是把它透传给调用者。这进一步印证「包装脚本不拥有业务结果，只负责转发」。

> 注意：脚本头部的注释（[poc.sh:12-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L12-L18)）提到委托目标是 `<PoC-Root>/py/wrapper.sh`，这是历史遗留的过期注释；**以第 69 行的实际 `source` 为准**，真实目标是 `lib/pyIPCMI/pyIPCMI.sh`。读源码时，「注释会过期，代码是真相」是一条重要纪律。

**PowerShell 侧：poc.ps1**

同样的思路，PowerShell 语法不同。先定义委托参数：

[poc.ps1:36-43](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L36-L43) —— `$Library = "PoC"`、`$pyIPCMI_Dir = "lib\pyIPCMI"`、`$pyIPCMI_PSModule = "pyIPCMI"`。注意路径分隔符是反斜杠。

记录工作目录、用 `Resolve-Path` 推算根目录：

[poc.ps1:47-48](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L47-L48) —— `$Wrapper_WorkingDirectory = Get-Location` 与 `$Library_RootDirectory = Convert-Path (Resolve-Path (...))`。

委托动作是 `Import-Module`，并把根目录、库名、模块名等作为参数传进去：

[poc.ps1:51-57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L51-L57) —— `Import-Module "$Library_RootDirectory\$pyIPCMI_Dir\$pyIPCMI_PSModule.psm1"`，即加载 `lib\pyIPCMI\pyIPCMI.psm1`。这个模块加载后会负责解析 Python 解释器路径、定位真正的 Python 前端脚本，并把它们写进 `$Python_Interpreter`、`$pyIPCMI_FrontEndPy` 等变量。

模块加载后，poc.ps1 还会扫描参数、决定要预热哪些厂商工具环境：

[poc.ps1:60-62](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L60-L62) —— `Get-PyIPCMIEnvironmentArray $args` 解析出要加载哪些工具环境，`Invoke-OpenEnvironment` 执行厂商/工具的 pre-hook（例如把 Vivado 的 bin 目录加进 PATH）。

随后拼出真正的 Python 命令并执行：

[poc.ps1:86-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L86-L93) —— 把 `$Python_Interpreter $Python_Parameters $pyIPCMI_FrontEndPy $args` 拼成命令字符串，再用 `Invoke-Expression` 执行。这就是「最终落到 Python 前端」的一步。

收尾时对称地关闭环境、卸载模块、清理环境变量、还原工作目录、透传退出码：

[poc.ps1:97-112](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L97-L112) —— `Invoke-CloseEnvironment`、`Remove-Module`、清空 `$env:LibraryRootDirectory` 等、`Set-Location` 还原、`exit $PyWrapper_ExitCode`。

> 同样地，poc.ps1 头部注释（[poc.ps1:8-14](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L8-L14)）把终极目标写成 `pyIPCMI.py`，这与第 51/87 行的模块加载 + Python 调用是一致的，但要记住 `.py` 本身位于未 checkout 的子模块内。

#### 4.1.4 代码实践

**实践目标**：亲手把 `poc.sh` 的每一行归类成「定位 / 记忆 / 委托 / 收尾」四类，建立对委托链的肌肉记忆。

**操作步骤**：

1. 打开 [poc.sh](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh)。
2. 在纸上画四列：定位、记忆、委托、收尾。
3. 把第 50–60 行归入「定位」，第 62–66 行归入「记忆」，第 69 行归入「委托」，第 72 行归入「收尾」。
4. 对照 [poc.ps1:51-57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L51-L57) 与 [poc.ps1:86-93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L86-L93)，标注 PowerShell 版本里「委托」分了两步（先 `Import-Module`，再 `Invoke-Expression`）。

**需要观察的现象**：两个脚本都没有出现任何 PoC 业务关键字（没有 `vhdl`、没有 `synthesis`、没有 `fifo`），全文只围绕「目录、参数、模块、退出码」打转。

**预期结果**：你能用一句话概括——「入口脚本是平台相关的薄壳，业务全在 pyIPCMI 里」。

**待本地验证**：若想真实观察委托过程，可在已 `--recursive` 克隆并 `configure` 过的环境里执行 `bash -x ./poc.sh --help`，观察 `source lib/pyIPCMI/pyIPCMI.sh` 这一行确实被执行（本仓库快照未 checkout 子模块，无法在此验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 poc.sh 用 `source` 而不是 `bash` 来调用 pyIPCMI 脚本？
**答案**：`source` 在当前 shell 进程里执行被引入的脚本，两者共享变量；这样 pyIPCMI 一侧能读到 poc.sh 提前保存的 `Wrapper_WorkingDirectory`、`Library_RootDirectory` 等上下文，也能把退出码写回 `PoC_ExitCode`。用 `bash` 会起子进程，这些变量拿不到。

**练习 2**：poc.sh 第 50 行的 Darwin 特判解决什么问题？
**答案**：macOS 自带的 `readlink` 不支持 `-f`（跟随符号链接到真实路径），需改用 Homebrew 提供的 `greadlink`，否则符号链接场景下定位 PoC 根目录会失败。

**练习 3**：poc.ps1 第 87 行和第 93 行分别做什么？为什么分成两步？
**答案**：第 87 行把 Python 解释器、参数、前端脚本、用户参数拼成一条命令字符串；第 93 行用 `Invoke-Expression` 真正执行它。分成两步是为了能在 debug 模式（第 92 行）先打印这条命令、再执行，便于排查。

---

### 4.2 pyIPCMI 子模块：作为 git submodule 的 Python 基础设施

#### 4.2.1 概念说明

pyIPCMI（Python Infrastructure for IPCMI 类项目）是 PoC 的「大脑」：所有命令行解析、工具链探测、源码清单解析、仿真/综合流程编排都由它完成。但 PoC 仓库**并不包含它的源码**，而是把它作为一个独立的 GitHub 仓库，以 git submodule 的形式挂载到 `lib/pyIPCMI/`。这意味着：

- PoC 仓库只记录「pyIPCMI 的某个固定提交」。
- 克隆 PoC 时，`lib/pyIPCMI/` 默认是空的，必须显式初始化。
- pyIPCMI 可以独立版本化、被多个项目（PoC 只是其一）复用。

这一点和 `lib/` 下的 cocotb、OSVVM、UVVM、VUnit 完全同构——它们都是 submodule。

#### 4.2.2 核心流程

子模块的生命周期分两段：

```text
克隆阶段（一次性）
  git clone --recursive ...   ──►  自动 init + update 所有 submodule
                                         │
                                  lib/pyIPCMI/ 被填满
                                         │
运行阶段（每次）
  poc.sh / poc.ps1
        │
        └─ source/Import lib/pyIPCMI/{pyIPCMI.sh | pyIPCMI.psm1}
                  │
                  └─ 启动 Python 前端 (pyIPCMI.py)
```

若克隆时忘了 `--recursive`，就必须手动补救，否则委托链断在第一步。

#### 4.2.3 源码精读

**子模块声明**

[.gitmodules:16-18](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L16-L18) —— 声明 `lib/pyIPCMI` 这个子模块，`path = lib/pyIPCMI`，`url = ../pyIPCMI.git`（相对 URL，配合 GitHub 自动解析成 `VLSI-EDA/pyIPCMI`）。同文件还声明了 vunit、osvvm、cocotb、uvvm 等（[.gitmodules:1-15](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/.gitmodules#L1-L15)），印证「`lib/` 下全是 submodule」的组织约定。

**手动初始化的官方步骤**

[lib/README.md:7-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L7-L22) —— 给出未带 `--recursive` 克隆后的补救方法：`cd <PoCRoot>\lib\` 后依次 `git submodule init`、`git submodule update`，并把每个子模块的 `origin` 远端重命名为 `github`（这是 PoC 的命名约定，便于区分上游与镜像）。

> 有趣的是，`lib/README.md` 把 cocotb/OSVVM/UVVM/VUnit/Xillybus 逐一介绍（[lib/README.md:24-120](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L24-L120)），却**没有**专门介绍 pyIPCMI——因为该文档的主题是「第三方验证库」，而 pyIPCMI 属于 PoC 自身的基础设施（虽然物理上也躺在 `lib/` 下）。这种「物理位置相同、逻辑角色不同」的区分，是读 PoC 文档时要注意的。

**Python 依赖极简**

[requirements.txt:1-2](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/requirements.txt#L1-L2) —— pyIPCMI 只依赖 `colorama`（跨平台彩色输出）和 `py-flags`（把命令行标志解析成 Python 对象）。依赖如此之轻，说明 pyIPCMI 的复杂度在于「对工具链与流程的建模」，而不在于引入重量级框架。

**官方对基础设施的总述**

[README.md:53-55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L53-L55)（节选自 Overview）：*"To generalize all supported free and commercial vendor tool chains, PoC is shipped with a Python based infrastructure to offer a command line based frontend."* —— 这一句是理解 pyIPCMI 存在意义的总纲：它存在的目的就是「泛化（generalize）所有厂商工具链」。

[README.md:64-80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L64-L80) —— 明确「Python 3 + Bash/PowerShell」是使用 PoC 基础设施的前提，并要求一台「受支持的仿真或综合工具链」。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「本快照里 pyIPCMI 子模块未 checkout」，并掌握补救命令。

**操作步骤**：

1. 在仓库根目录执行 `ls -1 lib/pyIPCMI | wc -l`，观察输出。
2. 执行 `git submodule status`（只读命令），观察 `lib/pyIPCMI` 前缀的标记符号。
3. 查阅 [lib/README.md:7-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L7-L22) 给出的补救命令。

**需要观察的现象**：

- 第 1 步输出 `0`（目录为空）。
- 第 2 步中 `lib/pyIPCMI` 行首有一个 `-` 号，git 用它表示「该子模块尚未初始化」。

**预期结果**：你确认了「仓库可见的只是挂载点，真正代码不在」，并知道用 `git submodule update --init lib/pyIPCMI` 把它拉下来。

**待本地验证**：本讲所用快照确为空目录（已验证文件数为 0）；拉取后 `lib/pyIPCMI/` 内应出现 `pyIPCMI.py`、`pyIPCMI.sh`、`pyIPCMI.psm1` 等文件，届时可对照本讲 4.1 的委托描述核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么 PoC 把 pyIPCMI 做成 submodule，而不是直接把它的 Python 代码拷进仓库？
**答案**：pyIPCMI 是跨项目复用的基础设施（服务于多个「IPCMI 风格」的 HDL 库），独立版本化更清晰；PoC 只锁定它的某次提交，既能跟踪上游更新，又不会把基础设施的历史混进 PoC 自身的提交树。

**练习 2**：`.gitmodules` 里 `url = ../pyIPCMI.git` 是相对路径，它相对于什么？
**答案**：相对于父仓库（`VLSI-EDA/PoC`）在 GitHub 上的位置，解析后即 `https://github.com/VLSI-EDA/pyIPCMI.git`。相对 URL 的好处是 fork 后也能正确指向同组织的同名仓库。

**练习 3**：requirements.txt 只有两条依赖，这反映了 pyIPCMI 的什么设计取向？
**答案**：它偏好「自己建模」而非「堆框架」，复杂度集中在工具链/流程抽象上，运行期尽量零外部依赖、便于在各种受限的 EDA 服务器上部署。

---

### 4.3 .files 编译模型：pyIPCMI 消费的源码清单

#### 4.3.1 概念说明

VHDL 有严格的**编译顺序**：被 `use` 的包必须先编译，使用它的实体后编译；同一份逻辑在不同 VHDL 版本或不同厂商工具下可能要用**不同的源文件**。各家 EDA 工具描述这种「文件清单 + 顺序 + 条件」的方式五花八门。PoC 的解法是发明一种**与工具无关的清单语言**——`.files` 文件，由 pyIPCMI 在编译前读取、求值，再翻译成具体工具的工程文件。所以 `.files` 不是 VHDL，pyIPCMI 也不会把它喂给综合器；它是给 pyIPCMI 看的「菜谱」。

这一机制在前面单元已多次出现（u2-l1 的 `common.files`、u4-l3 的版本条件、u4-l2 的板级变体），本讲从「pyIPCMI 如何消费它」的视角统一收束。

#### 4.3.2 核心流程

```text
.files 清单（工具无关）
   │  含 vhdl / include / if...then...end if / report / path 等语句
   │
   ▼
pyIPCMI 在编译前读取并求值
   │  代入本次的 ToolChain / Tool / BoardName / VHDLVersion / Environment
   │
   ▼
展开成「本次实际要编译的、有序的 .vhdl 文件列表」
   │
   ▼
翻译成具体工具的工程/脚本（Vivado / Quartus / GHDL / ModelSim ...）
```

关键点：条件求值发生在**编译前**，结果是「一组具体的物理文件」，不兼容的源文件根本不会被对应的工具看到——这是 PoC 可移植性三层的「编译期」层（另两层是 u3-l2 的展开期 `generate`、u4-l4 的综合期网表模板）。

#### 4.3.3 源码精读

**`.files` 的基本语句：`vhdl 库 "路径"`**

[src/common/common.files:11-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L11-L17) —— 逐行声明公共包的编译顺序：先 `utils.vhdl`，再 `config.vhdl`，然后 `math/strings/vectors/physical/components`。顺序严格对应包之间的依赖（如 `config` 依赖 `utils`）。每一行格式是 `vhdl  <库名=poc>  "<相对PoC根的路径>"`，pyIPCMI 据此把文件编进名为 `PoC` 的库。

**条件分支：`if (VHDLVersion ...) then`**

[src/common/common.files:19-28](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19-L28) —— 用两层 `if` 表达「先按工具链、再按 VHDL 版本」选文件：当工具链不是 Altera/Lattice 时，若 `VHDLVersion < 2002` 编 `fileio.v93.vhdl`；若 `<= 2008` 则改用 `protected.v08.vhdl` + `fileio.v08.vhdl`（因为受保护类型在 VHDL-2002 才标准化）。不支持的版本用 `report` 报错。这正是 u4-l3 讲过的「版本差异走 `.files`」。

**环境分支：`if (Environment = "Simulation") then`**

[src/common/common.files:30-32](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L30-L32) —— 仅仿真环境才 `include "src/sim/sim.files"`，综合时不编译仿真包。`include` 语句把另一个 `.files` 文件内联进来，形成清单的依赖网。

**`include` 与板级分支的更复杂例子**

[tb/common/my_config.files:8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L8) —— 先无条件编译 `my_project.vhdl`。

[tb/common/my_config.files:14-103](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L14-L103) —— 用一长串 `if (BoardName = "...")` 在 GENERIC/Custom/DE0/KC705/ML505/... 之间挑出**唯一**一份 `my_config_<board>.vhdl` 编进 PoC 库。这是 u4-l2 讲过的「编译期按板选配置变体」。

[tb/common/my_config.files:17-27](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L17-L27) —— 在 `Custom` 分支里还嵌套了 `Tool` 条件与 `path` 变量：`${CONFIG.DirectoryNames:TemporaryFiles}`、`${CONFIG.DirectoryNames:GHDLFiles}` 等。这些 `${CONFIG....}` 是 pyIPCMI 配置字典里的命名目录，由 pyIPCMI 在求值时替换成实际路径。`Tool = "GHDL"` 与 `Tool in ["Mentor_vSim", "Cocotb_QuestaSim"]` 说明 `.files` 能按**具体仿真器**走不同分支。

**工具链分支的另一例**

[src/sim/sim.files:8-24](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files#L8-L24) —— `if (ToolChain != "Cocotb")`：cocotb 用 Python 写测试台、不需要 PoC 的 VHDL 仿真辅助包，故整个 sim 包对 cocotb 跳过。内层再用 `VHDLVersion` 在 `.v93` 与 `.v08` 两套实现里二选一。

#### 4.3.4 代码实践

**实践目标**：把 `common.files` 里的条件求值在脑中跑一遍，体会「同一份清单、不同环境下编译出不同文件集」。

**操作步骤**：

1. 打开 [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files)。
2. 假设本次运行环境是 `ToolChain=Xilinx_Vivado`、`VHDLVersion=2008`、`Environment=Simulation`。
3. 逐行判断每个 `if` 分支是否命中，列出最终会被编译的 `.vhdl` 文件清单。

**需要观察的现象**：

- 第 11–17 行的 7 个包无条件全部编译。
- 第 19 行外层 `if` 命中（Vivado 不是 Altera/Lattice），第 22–24 行 `elseif` 命中（2008），于是编译 `protected.v08.vhdl` + `fileio.v08.vhdl`，**不**编译 `fileio.v93.vhdl`。
- 第 30 行命中，`include` 进 `sim.files`。

**预期结果**：你得到一份约 12 个文件的有序清单，并理解「v93 与 v08 永远不会同时进库」。

**待本地验证**：真实求值由 pyIPCMI 完成；可尝试在已配置环境运行 `./poc.sh listfiles`（或等价子命令，**待确认该子命令名**）观察 pyIPCMI 展开后的实际文件列表。

#### 4.3.5 小练习与答案

**练习 1**：`.files` 里的 `vhdl poc "..."` 第二个字段 `poc` 是什么含义？
**答案**：目标库名。PoC 约定所有源码统一编译进名为 `PoC` 的库，pyIPCMI 据此调用工具的「建库/编译进库」命令。

**练习 2**：为什么 `Environment = "Simulation"` 的判断要写在 `.files` 里，而不是写在 VHDL 源码里？
**答案**：综合工具看到仿真专用包（含 `wait`、文件 IO、受保护类型等）会报错，所以必须在「编译前」就排除这些文件；`.files` 的编译期筛选正是保证「不兼容文件根本不被该工具看到」。

**练习 3**：`include "src/sim/sim.files"` 与直接把 sim 包的文件列在 common.files 里相比，有什么好处？
**答案**：`include` 让清单按子领域分文件维护、形成依赖网，sim 包的版本分支逻辑集中在 `sim.files` 一处，避免在多处复制；也方便单独引用（如某些工程只要 sim 包）。

---

### 4.4 工具链抽象：用条件变量统一多厂商工具

#### 4.4.1 概念说明

pyIPCMI 的核心价值是「泛化工具链」。它把现实里千差万别的厂商工具抽象成一组**正交的条件变量**，让 `.files` 清单与流程脚本只跟这些变量打交道，而不直接写死任何厂商命令。本讲归纳出这套变量体系，并指出它们各自在哪一层发挥作用：

| 变量 | 取值举例（来自源码） | 控制哪一层 | 证据出处 |
| --- | --- | --- | --- |
| `VHDLVersion` | `< 2002` / `<= 2008` | 编译期：选 `.v93` / `.v08` 文件 | common.files:20-22 |
| `ToolChain` | `Altera_QuartusII` / `Lattice_Diamond` / `Cocotb` / `Xilinx_Vivado` | 编译期：按厂商排除不兼容包 | common.files:19、sim.files:8 |
| `Tool` | `GHDL` / `Mentor_vSim` / `Cocotb_QuestaSim` | 编译期：按具体仿真器选目录 | my_config.files:18-20 |
| `BoardName` | `Custom` / `KC705` / `ML505` / `DE0` / `ECP5Versa` ... | 编译期：选 `my_config_<board>.vhdl` | my_config.files:14-98 |
| `Environment` | `Simulation` / 综合 | 编译期：是否引入仿真包 | common.files:30 |
| `CONFIG.*` | `${CONFIG.DirectoryNames:TemporaryFiles}` 等 | 编译期：pyIPCMI 配置字典里的命名路径 | my_config.files:17-19 |

#### 4.4.2 核心流程

pyIPCMI 在每次运行时建立一棵「上下文树」：

```text
读取本地配置 (poc configure 生成)
        │
        ▼
┌──────────────── 上下文变量  ────────────────┐
│ BoardName / Device / ToolChain / Tool /     │
│ VHDLVersion / Environment / CONFIG.*        │
└─────────────────────────────────────────────┘
        │
        ├──► 代入各 .files 清单  ──► 选出物理 .vhdl 文件（编译期层）
        │
        ├──► 决定调用哪家工具的命令（流程编排层）
        │
        └──► 选网表综合模板 xst/<系列>.xst（综合期层，见 u4-l4）
```

同一组变量同时驱动三个可移植层，这正是 PoC 能做到「一份源码、多厂商可移植」的软件侧根基（硬件侧根基是 u2-l3 的 `MY_DEVICE`→`DEVICE_INFO`→`generate`）。

#### 4.4.3 源码精读

**工具链大类的直接证据**

[poc.ps1:79-81](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L79-L81) —— debug 输出里直接列出三大类受支持的工具链：`Lattice Diamond`、`Xilinx ISE`、`Xilinx Vivado`。这说明 pyIPCMI 把工具按「厂商/产品」分层管理，`$PyWrapper_LoadEnv` 这个哈希表就是工具链加载开关。

[poc.ps1:60-62](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.ps1#L60-L62) —— `Get-PyIPCMIEnvironmentArray` 扫描命令行参数，决定本次要预热哪些工具环境；`Invoke-OpenEnvironment`/`Invoke-CloseEnvironment`（第 62、97 行）成对出现，对应「运行前设置环境、运行后还原」，这是跨工具链调用的典型封装。

**编译期工具链分支**

[src/common/common.files:19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L19) —— `if (ToolChain not in ["Altera_QuartusII", "Lattice_Diamond"])`：因为这两家的工具对 fileio/受保护类型支持有缺陷，整个 fileio 包对它们跳过。这是「按厂商工具链选文件」最直接的例子。

[src/sim/sim.files:8](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sim/sim.files#L8) —— `if (ToolChain != "Cocotb")`：cocotb 不走传统 VHDL 仿真包路径，整包跳过。

**板与器件的桥接**

[tb/common/my_config.files:14-98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L14-L98) —— `BoardName` 在编译期挑出板级配置文件。这与 u4-l5 讲的「`MY_BOARD` 单一常量同时驱动 RTL 层 `generate` 与约束层 `ucf/<BoardName>/`」形成闭环：板名这一个开关，在软件侧（pyIPCMI 编译期）、RTL 侧（generate 展开期）、约束侧（ucf 目录）三处保持一致。

**配置字典的命名路径**

[tb/common/my_config.files:17-19](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files#L17-L19) —— `${CONFIG.DirectoryNames:TemporaryFiles}`、`${CONFIG.DirectoryNames:GHDLFiles}`、`${CONFIG.DirectoryNames:ModelSimFiles}`。`CONFIG` 是 pyIPCMI 维护的配置字典，`DirectoryNames` 是其中的命名目录表；用名字而不是硬编码路径，既跨平台又跨工具。这类变量由 `poc configure`（[README.md:137-140](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/README.md#L137-L140)）写入本地。

#### 4.4.4 代码实践

**实践目标**：在全仓库范围内盘点 `.files` 用到的条件变量，建立完整的「工具链抽象矩阵」。

**操作步骤**：

1. 用搜索工具查找所有 `.files` 文件（如 `**/*.files`）。
2. 在每个文件里提取形如 `if (... = ...)` / `if (... in [...])` / `if (... < ...)` 的条件，归纳出用到的变量名。
3. 把结果填进本讲 4.4.1 的表格，标注每个变量第一次出现的文件与行号。

**需要观察的现象**：你会反复看到 `ToolChain`、`Tool`、`VHDLVersion`、`BoardName`、`Environment` 这几个名字，几乎不会看到任何厂商专用的命令行（如 `vivado`、`quartus_sh`）直接出现在 `.files` 里。

**预期结果**：你得出结论——`.files` 是「工具中立」的，所有厂商差异都被收敛进那一小组条件变量，真正的厂商命令由 pyIPCMI 在更深层翻译。

**待本地验证**：若已 checkout pyIPCMI 子模块，可在其源码里搜索这些变量名的求值逻辑，确认它们由 pyIPCMI 的「上下文/工具链」模块统一提供（本快照无法验证）。

#### 4.4.5 小练习与答案

**练习 1**：`ToolChain` 与 `Tool` 有什么区别？
**答案**：`ToolChain` 粒度更粗，指厂商产品线（如 `Xilinx_Vivado`、`Altera_QuartusII`、`Cocotb`）；`Tool` 粒度更细，指具体仿真器/可执行件（如 `GHDL`、`Mentor_vSim`、`Cocotb_QuestaSim`）。前者多用于按厂商排除整包，后者多用于在同一厂商下选不同后端。

**练习 2**：为什么说「板名 `BoardName` 是贯穿三层可移植性的开关」？
**答案**：编译期它让 pyIPCMI 选对 `my_config_<board>.vhdl`；展开期它经 `config.vhdl` 解析成器件/厂商驱动 `generate` 选厂商子实体（u2-l3、u4-l5）；约束期它对应 `ucf/<BoardName>/` 选约束文件。一处定义、三处一致。

**练习 3**：若新增一家厂商工具链，PoC 的 `.files` 与 pyIPCMI 各需要做什么？
**答案**：`.files` 这一层几乎不用改——只要新工具链的 `ToolChain`/`Tool` 名字被 pyIPCMI 识别，现有条件分支会自然命中（如落到通用兜底分支）；真正的工作在 pyIPCMI 内部——要为新工具链实现「编译/仿真/综合」的命令翻译与工程文件生成。这正是把厂商差异封装在 pyIPCMI 一处的好处。

---

## 5. 综合实践

**综合任务**：把本讲四条线索串成一条完整的「命令行→加载→编译模型」追踪报告。

请完成以下步骤，产出一份图文报告：

1. **委托链追踪**：从 `./poc.sh --simulation fifo_cc_got_tb`（假设命令）出发，按 [poc.sh:39-72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/poc.sh#L39-L72) 标注每一步：哪些变量被设置、哪一行把控制权交出去、交给谁。画出从命令行参数到 `lib/pyIPCMI/pyIPCMI.sh` 被加载的完整时序。
2. **子模块确认**：说明为什么这一步在未 `--recursive` 克隆的仓库里会失败，并写出补救命令（参考 [lib/README.md:7-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/lib/README.md#L7-L22)）。
3. **`.files` 角色**：解释 pyIPCMI 加载后，如何用 [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) 与 [tb/common/my_config.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/tb/common/my_config.files) 决定「这次到底编译哪些 `.vhdl`」；给出 `VHDLVersion=2008`、`Environment=Simulation`、`BoardName=Custom`、`Tool=GHDL` 时的展开结果。
4. **工具链抽象**：列出本次运行涉及的所有条件变量取值，并说明它们如何最终决定 pyIPCMI 调用 GHDL 而非其它仿真器。

**验收标准**：报告中每一处结论都必须能指回具体源码行号或 `.files` 语句；凡涉及 pyIPCMI 子模块内部、无法从本仓库验证的，一律标注「待本地验证」，不得编造。

## 6. 本讲小结

- PoC 的命令行入口 `poc.sh` / `poc.ps1` 是**平台相关的薄壳**：只做定位目录、记忆参数、委托加载、透传退出码，不含任何业务逻辑。
- 委托动作分别是 `source lib/pyIPCMI/pyIPCMI.sh`（Bash）与 `Import-Module lib\pyIPCMI\pyIPCMI.psm1` 后 `Invoke-Expression` 运行 Python 前端（PowerShell）；脚本头部关于委托目标的注释已过期，以代码为准。
- pyIPCMI 是一个以 **git submodule** 挂在 `lib/pyIPCMI/` 的独立 Python 项目，必须 `--recursive` 克隆或手动 `git submodule update --init`，否则委托链断在第一步。
- `.files` 是 pyIPCMI 自定义的**工具中立编译清单语言**，含 `vhdl`/`include`/`if`/`report`/`path` 语句，在编译前被求值，负责源码顺序、版本与条件选择。
- 工具链差异被抽象成一组正交条件变量 `ToolChain`/`Tool`/`VHDLVersion`/`BoardName`/`Environment`/`CONFIG.*`，同一组变量同时驱动编译期（`.files`）、展开期（`generate`）、综合期（`xst` 模板）三层可移植性。
- pyIPCMI 运行期依赖极轻（仅 colorama、py-flags），复杂度全部集中在「对工具链与流程的建模」上。

## 7. 下一步学习建议

- **继续向 pyIPCMI 内部深入**：本讲止步于「委托边界」。若你已 checkout 子模块，下一步应阅读 `lib/pyIPCMI/` 下的 `pyIPCMI.py`（命令行前端、子命令分发）与工具链适配模块，验证本讲对条件变量求值的推断。
- **横向对照第三方验证库**：[u5-l2 第三方验证库集成](u5-l2-third-party-verification-libs.md) 讲 cocotb/OSVVM/UVVM/VUnit 同样以 submodule 嵌入 `lib/`，可对照体会「基础设施 submodule」与「验证方法学 submodule」的异同。
- **回顾三层可移植性**：把本讲（软件/编译期）与 [u3-l2 厂商选择](u3-l2-vendor-selection-portability.md)（RTL/展开期）、[u4-l4 综合与 netlist](u4-l4-synthesis-netlist-flow.md)（综合期）连起来读，建立「同一份 `MY_DEVICE` 贯穿三层」的完整图景。
- **动手扩展**：参考 [u5-l6 扩展 PoC](u5-l6-extending-poc.md)，尝试为新核编写配套的 `.files` 条目，亲手体会 pyIPCMI 的编译模型。
