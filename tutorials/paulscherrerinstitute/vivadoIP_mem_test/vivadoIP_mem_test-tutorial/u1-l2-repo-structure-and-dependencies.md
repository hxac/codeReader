# 仓库结构与外部依赖

## 1. 本讲目标

上一篇（u1-l1）我们认识了 `vivadoIP_mem_test` 是什么——一个通过 AXI 接口测试存储器可靠性的 IP 核。本讲我们要回答一个更落地的问题：**当我把这个仓库 clone 下来之后，看到的这一堆文件夹和文件，各自是干什么的？我还需要准备哪些外部代码才能让它跑起来？**

读完本讲，你应当能够：

- 看懂仓库里 `hdl`、`tb`、`sim`、`scripts`、`drivers`、`bd`、`xgui`、`doc` 这八个目录各自承担的职责。
- 说出项目依赖的四个外部库（`psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage`）分别是什么、用在什么阶段。
- 理解为什么仓库必须被放在一个特定的多级目录结构里（即 `../../..` 这个相对路径的含义）。
- 学会用 `scripts/dependencies.py` 自动拉取这些依赖，而不是手动一个个 clone。

本讲是后续所有讲义的基础：只有在正确的目录结构下、依赖齐全，下一讲的仿真才能跑起来。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

**第一，FPGA 工程通常不是“一个文件搞定”。** 一个 IP 核至少包含三类东西：

- **RTL 源码**：用硬件描述语言（这里是 VHDL）写成的电路本身。
- **仿真测试台（testbench）**：一段不对应真实硬件、只在仿真器里跑的代码，用来给电路喂激励、检查输出是否正确。
- **封装/集成脚本**：把 RTL 打包成 Vivado 可识别的“IP 核”、并在 Block Design 中连线的自动化脚本。

这三类东西通常会分目录存放，本仓库也是如此。

**第二，PSI（Paul Scherrer Institute）有一个庞大的 FPGA 公开代码库。** 本 IP 核并不从零实现所有东西，而是复用了公共库里的现成组件，比如 AXI 主机、AXI 从机、FIFO 等。这些公共组件在 `psi_common` 库里。这意味着：**本项目不能单独运行，必须和它的依赖放在同一个目录树里。**

**第三，依赖可以被“手动摆放”，也可以被“脚本自动拉取”。** 本仓库两种方式都支持：README 里写明了目录结构，同时也提供了一个 Python 脚本 `dependencies.py` 帮你自动搞定。

> 术语提示：
> - **VHDL**：一种硬件描述语言，本仓库 RTL 全部用它写成（`.vhd` 文件）。
> - **TCL**：一种脚本语言，Vivado、Modelsim 等 EDA 工具大量用它做自动化。
> - **IP 核（IP core）**：可复用的硬件功能模块，本仓库的最终产物就是一个 IP 核。
> - **Block Design**：Vivado 中用图形拖拽方式把多个 IP 连成系统的画布。

## 3. 本讲源码地图

本讲涉及的关键文件如下（永久链接基于当前 HEAD `756fa79`）：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md) | 项目总说明；其中 **Dependencies 段**声明了所有外部依赖，且会被脚本自动解析 |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/dependencies.py) | 依赖自动获取脚本：读取 README，拉取依赖到正确位置 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl) | 仿真配置：声明要编译哪些库、哪些源文件——从中能直观看到对外部库的依赖 |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl) | 仿真回归入口：通过相对路径 `../../..` 引用外部 PsiSim 框架 |

本讲还会引用 `git ls-files` 的实际输出，以建立准确的目录树认知。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**目录布局**、**依赖清单**、**依赖获取脚本**。

### 4.1 目录布局

#### 4.1.1 概念说明

一个规范的 FPGA IP 仓库，目录划分通常遵循“**按角色分文件夹**”的原则：RTL 一类、仿真一类、脚本一类、文档一类。这样无论谁来接手，都能凭目录名猜出里面是什么。本仓库就是这套约定俗成结构的典型样本。

`vivadoIP_mem_test` 仓库的顶层结构如下（基于 `git ls-files` 的真实输出）：

```
vivadoIP_mem_test/            <- 仓库根目录（本身位于 VivadoIp/ 下）
├── README.md                 项目总说明（含被解析的依赖段）
├── Changelog.md              版本变更记录
├── License.txt               PSI HDL 许可证正文
├── LGPL2_1.txt               LGPL 许可证正文
├── component.xml             IP-XACT 格式的 IP 元数据（Vivado 识别 IP 用）
├── .gitignore
│
├── hdl/                      【RTL 源码】硬件电路本体（VHDL）
│   ├── mem_test_pkg.vhd          寄存器地图、常量、子类型定义（package）
│   ├── mem_test.vhd              核心测试逻辑（状态机、pattern 生成）
│   └── mem_test_wrapper.vhd      顶层封装，把 AXI 从机/主机/核心连起来
│
├── tb/                       【仿真测试台】只在仿真器里跑
│   └── top_tb.vhd                顶层 testbench
│
├── sim/                      【仿真脚本】Modelsim/PsiSim 用
│   ├── run.tcl                   回归测试入口
│   ├── config.tcl                库与源文件配置
│   ├── interactive.tcl           交互式调试入口
│   └── ci.do                     CI 仿真 do 文件
│
├── scripts/                  【构建/CI 脚本】
│   ├── dependencies.py           依赖自动获取
│   ├── ciFlow.py                 CI 仿真驱动
│   └── package.tcl               Vivado IP 封装脚本
│
├── drivers/                  【C 软件驱动】给 CPU/裸机程序用的
│   └── mem_test/
│       ├── src/
│       │   ├── mem_test.h            寄存器偏移宏 + API 声明
│       │   ├── mem_test.c            API 实现（Xil_Out32/In32）
│       │   └── Makefile
│       └── data/
│           ├── mem_test.tcl          驱动打包元数据
│           └── mem_test.mdd
│
├── bd/                       【Block Design 集成】
│   └── bd.tcl                    BD 参数传播回调（AXI ID_WIDTH 等）
│
├── xgui/                     【Vivado IP 参数界面】
│   └── mem_test_v1_2.tcl         IP 定制 GUI 脚本
│
└── doc/                      【文档】
    ├── mem_test.pdf              数据手册（最终文档）
    ├── mem_test.docx             可编辑源文档
    ├── mem_test.vsd              框图源文件
    └── psi_logo_150.gif          PSI logo
```

#### 4.1.2 核心流程

记住下面这张“**目录 → 阶段**”对照表，就能在脑子里给整个仓库的产物链路定位：

| 目录 | 在哪个阶段被用到 | 典型使用者 |
|------|------------------|-----------|
| `hdl/` | 综合、仿真 | 综合工具、Modelsim |
| `tb/` + `sim/` | 仿真验证 | Modelsim + PsiSim |
| `scripts/package.tcl` + `xgui/` + `component.xml` | IP 封装 | Vivado / PsiIpPackage |
| `bd/` | 系统集成 | Vivado Block Design |
| `drivers/` | 软件开发 | CPU 裸机程序 / Vitis |
| `doc/` | 文档查阅 | 人类读者 |

一条贯穿始终的开发链路是：**改 `hdl/` 里的 RTL → 在 `sim/` 跑仿真验证 → 用 `scripts/package.tcl` 封装成 IP → 在 Block Design（`bd/`）里集成 → 写 `drivers/` 驱动测试**。后面几讲会逐一深入这些环节。

#### 4.1.3 源码精读

目录布局之所以重要，最直接的证据藏在仿真脚本对相对路径的引用里。

`sim/run.tcl` 第 8 行引用了一个仓库里根本不存在的路径 [sim/run.tcl:8](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl#L8)：

```tcl
source ../../../TCL/PsiSim/PsiSim.tcl
```

注意这个 `../../../`。从 `sim/` 出发往上跳三级：

- `..` → 仓库根 `vivadoIP_mem_test/`
- `../..` → `VivadoIp/`
- `../../..` → **公共根目录**，这里才有 `TCL/`、`VHDL/` 等兄弟目录。

这揭示了一个关键事实：**本仓库自身只是“公共根目录”下的一个子目录**，它必须和外部依赖库摆成如下的多级结构（README 明确要求“folder names must be matched exactly”）：

```
<公共根>/                  <- 即 sim/ 里的 ../../..
├── TCL/
│   ├── PsiSim/                仿真框架（含 PsiSim.tcl）
│   └── PsiIpPackage/          IP 封装框架
├── VHDL/
│   ├── psi_common/            公共 VHDL 组件库
│   └── psi_tb/                测试台辅助库
└── VivadoIp/
    └── vivadoIP_mem_test/  <- 就是本仓库
        └── sim/run.tcl     <- 在这里，往上三级回到公共根
```

同样的 `../../../TCL/PsiSim/PsiSim.tcl` 引用也出现在交互式调试脚本里 [sim/interactive.tcl:11](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/interactive.tcl#L11)，说明这不是偶然，而是整个仓库的硬性约定。

> 小贴士：如果你只 clone 了 `vivadoIP_mem_test` 一个仓库就尝试跑仿真，`source ../../../TCL/PsiSim/PsiSim.tcl` 必然失败。这也是为什么下一节要专门讲依赖。

#### 4.1.4 代码实践

**实践目标**：亲手用 `git ls-files` 还原目录树，验证本节给出的结构图与真实文件一一对应。

**操作步骤**：

1. 在仓库根目录执行：

   ```bash
   git ls-files
   ```

2. 观察输出，按顶层目录（`hdl`、`tb`、`sim`、`scripts`、`drivers`、`bd`、`xgui`、`doc`）分组。

3. 把输出整理成一棵树（可借助纸笔或文本编辑器），在每个目录旁标注它的职责（参考 4.1.1 的结构图）。

**需要观察的现象**：

- `hdl/` 下应该恰好有 3 个 `.vhd` 文件：`mem_test_pkg.vhd`、`mem_test.vhd`、`mem_test_wrapper.vhd`。
- `drivers/` 下的文件路径较长（`drivers/mem_test/src/...`），这是因为 Vivado IP 驱动要求固定的子目录约定。
- 仓库里**没有** `TCL/PsiSim/` 或 `VHDL/psi_common/` 这样的目录——它们正是需要从外部引入的依赖。

**预期结果**：你画出的目录树应与 4.1.1 完全一致，共 30 个被 git 跟踪的文件。

#### 4.1.5 小练习与答案

**练习 1**：仓库里哪个目录存放的是“最终会被综合成真实电路”的代码？

> **参考答案**：`hdl/`。其中的三个 `.vhd` 文件是真正的硬件描述，会被综合工具翻译成 FPGA 上的逻辑。`tb/` 里的 testbench 不会被综合。

**练习 2**：为什么 `sim/run.tcl` 里要用 `../../../` 而不是 `./` 或 `../` 来引用 PsiSim？

> **参考答案**：因为 PsiSim 不是本仓库的一部分，它位于“公共根目录”下的 `TCL/PsiSim/`；而本仓库位于公共根下的 `VivadoIp/vivadoIP_mem_test/`，从仓库内的 `sim/` 出发，需要向上跳三级才能回到公共根。

### 4.2 依赖清单

#### 4.2.1 概念说明

本仓库是 PSI FPGA 生态的一个“叶子节点”，它站在几个公共库的肩膀上。理解依赖，关键在于区分两点：**这个库是什么**，以及**它在哪个阶段才需要**（是运行 IP 必需，还是仅仅开发/仿真时才必需）。

README 把依赖信息写在一个特殊标记包裹的段落里，目的就是让脚本可以机器解析。

#### 4.2.2 核心流程

README 中被自动解析的 Dependencies 段如下 [README.md:18-35](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L18-L35)：

```markdown
<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->
## Dependencies
...
* TCL
  * PsiSim (2.1.0 or higher, for development only)
  * PsiIpPackage (2.0.0, for development only)
* VHDL
  * psi_common (2.5.0 or higher)
  * psi_tb (2.1.1 or higher, for development only)
* VivadoIp
  * vivadoIP_mem_test
<!-- END OF PARSED SECTION -->
```

把这段拆成一张清晰的依赖清单表：

| 依赖 | 语言/类型 | 最低版本 | 何时需要 | 它提供什么 |
|------|-----------|----------|----------|-----------|
| **psi_common** | VHDL | 2.5.0 | **运行必需** | AXI 主机/从机、FIFO、同步器等公共硬件组件 |
| **psi_tb** | VHDL | 2.1.1 | 仅开发（仿真） | AXI 仿真辅助过程、文本比较工具 |
| **PsiSim** | TCL | 2.1.0 | 仅开发（仿真） | Modelsim 仿真流程框架（`psi::sim::*`） |
| **PsiIpPackage** | TCL | 2.0.0 | 仅开发（封装） | Vivado IP 自动封装框架 |

这里的分类非常重要：

- **psi_common 是“真依赖”**：它的代码会被综合进最终比特流，IP 离了它根本不能工作。
- **其余三个是“开发依赖”**（README 标注 *for development only*）：只在仿真、封装阶段用到。如果你只是拿到已经封装好的 IP 在 Vivado 里用，并不需要它们；但如果要跑回归仿真或重新封装 IP，就必须备齐。

注意 README 标记的层级（`TCL/`、`VHDL/`、`VivadoIp/`）正好对应 4.1.3 里公共根目录下的三个子目录——这不是巧合，依赖清单同时定义了“需要哪些库”和“它们各自摆在哪个文件夹”。

#### 4.2.3 源码精读

依赖清单的真实性，可以从仿真配置文件 `sim/config.tcl` 得到交叉验证。它用相对路径 `../../..` 引用了 psi_common 和 psi_tb 的具体源文件 [sim/config.tcl:8](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L8) 与 [sim/config.tcl:17-34](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl#L17-L34)：

```tcl
set LibPath "../../.."
...
# PSI Common
psi::sim::add_sources "$LibPath/VHDL/psi_common/hdl" {
    psi_common_axi_master_simple.vhd \
    psi_common_axi_slave_ipif.vhd \
    ...
} -tag lib

# psi_tb
psi::sim::add_sources "$LibPath/VHDL/psi_tb/hdl" {
    psi_tb_axi_pkg.vhd \
    ...
} -tag lib
```

这段代码直接证明了三件事：

1. 仿真确实需要从外部 `VHDL/psi_common/hdl` 编译 AXI 主机/从机等组件——没有 psi_common，本 IP 的 wrapper 根本实例不出来。
2. 测试台需要 `VHDL/psi_tb/hdl` 里的 AXI 仿真辅助包。
3. `LibPath` 的值 `../../..` 与 4.1.3 的目录嵌套推论完全吻合。

README 还明确给出了获取依赖的两种途径 [README.md:37-43](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/README.md#L37-L43)：手动按结构摆放，或用下一节讲的 `dependencies.py` 脚本；也可以直接 clone 汇总仓库 `psi_fpga_all`（它以 git submodule 形式包含了所有相关仓库并摆好了正确结构）。

#### 4.2.4 代码实践

**实践目标**：从 `sim/config.tcl` 反推“跑一次仿真到底要编译多少个外部文件”，从而建立对依赖体积的直观感受。

**操作步骤**：

1. 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/config.tcl)。
2. 分别数一下 `psi_common`、`psi_tb`、项目源（`-tag src`）、testbench（`-tag tb`）四组里各列出了多少个 `.vhd` 文件。

**需要观察的现象**：

- psi_common 组有 8 个文件（含 `psi_common_axi_master_simple.vhd`、`psi_common_axi_slave_ipif.vhd` 等）。
- 项目自身源码只有 3 个文件（`mem_test_pkg.vhd`、`mem_test.vhd`、`mem_test_wrapper.vhd`）。

**预期结果**：你会清楚地看到，**外部依赖（psi_common + psi_tb）的文件数比项目自身源码还多**。这印证了“本 IP 站在公共库肩膀上”的说法。

#### 4.2.5 小练习与答案

**练习 1**：如果某个用户只是想在 Vivado 项目里直接使用已经封装好的本 IP，下列哪个依赖他**不需要**安装？

> **参考答案**：`psi_tb`、`PsiSim`、`PsiIpPackage` 都不需要——它们都是 development only。真正必需的只有 `psi_common`（而且封装后的 IP 通常已把所需 psi_common 组件打进 IP 包，普通使用时连 psi_common 都不必单独管理）。

**练习 2**：README 为什么要用 `<!-- DO NOT CHANGE FORMAT -->` 这样的 HTML 注释把 Dependencies 段包起来？

> **参考答案**：因为这一段会被 `dependencies.py` 脚本自动解析（见 4.3）。注释标记告诉脚本解析的起止位置，也提醒人类不要改动格式，否则脚本解析会失败。

### 4.3 依赖获取脚本

#### 4.3.1 概念说明

手动按目录结构摆放四个依赖仓库，既繁琐又容易出错（版本不对、放错层级都会让仿真失败）。`scripts/dependencies.py` 就是用来解决这个痛点的：它**读取 README 里的 Dependencies 段，自动把依赖 clone 到正确的相对位置**。

这个脚本本身非常短——真正的逻辑都封装在一个叫 `PsiFpgaLibDependencies` 的外部 Python 包里。本仓库的脚本只是“配置 + 调用”。

#### 4.3.2 核心流程

脚本的工作流程只有四步：

1. **导入依赖包**：`from PsiFpgaLibDependencies import *`。
2. **定位自身**：算出 `scripts/` 目录的绝对路径。
3. **解析 README**：调用 `Parse.FromReadme()`，传入 README.md 的路径，提取依赖清单。
4. **执行拉取**：调用 `Actions.ExecMain()`，传入仓库根路径和上一步解析出的依赖，由依赖包完成实际的 clone/检出。

用伪代码表示：

```
导入 PsiFpgaLibDependencies 工具集
THIS_DIR  = dependencies.py 所在目录 (scripts/)
README    = THIS_DIR/../README.md
REPO_ROOT = THIS_DIR/..            (仓库根)
deps      = 解析 README 的 Dependencies 段
执行主动作(REPO_ROOT, deps)         -> 自动 clone 依赖到 ../TCL、../VHDL 等
```

#### 4.3.3 源码精读

整个脚本只有 16 行，逻辑一目了然 [scripts/dependencies.py:7-16](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/dependencies.py#L7-L16)：

```python
from PsiFpgaLibDependencies import *
import sys
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

dependencies = Parse.FromReadme(THIS_DIR + "/../README.md")
repo = os.path.abspath(THIS_DIR + "/..")

Actions.ExecMain(repo, dependencies)
```

逐行说明：

- 第 7 行 `from PsiFpgaLibDependencies import *`：导入外部依赖管理包，它提供了 `Parse`、`Actions` 两个工具对象。注意 README 也提醒：**必须先安装 [PsiFpgaLibDependencies](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 这个包，脚本才能运行**。
- 第 11 行：用 `__file__`（脚本自身路径）求出 `scripts/` 的绝对路径。这种写法的好处是：无论你从哪个目录调用脚本，路径都能正确解析。
- 第 13 行 `Parse.FromReadme(...)`：正是这一行读入 README 并解析 4.2.2 提到的那个被注释包裹的 Dependencies 段——这就是为什么 README 格式不能乱改。
- 第 14 行：求出仓库根目录（`scripts/` 的上一级）。
- 第 16 行 `Actions.ExecMain(repo, dependencies)`：把“在哪摆”（repo 根）和“摆什么”（dependencies 清单）交给工具，由它完成 git clone、版本检出等实际工作。

#### 4.3.4 代码实践

**实践目标**：查看 `dependencies.py` 的命令行帮助，了解它能做哪些动作（拉取、清理等）。

**操作步骤**：

1. 先确保安装了依赖管理包（一次性）：

   ```bash
   pip install PsiFpgaLibDependencies
   ```

   > 待本地验证：该包的安装名与可用版本以 [PsiFpgaLibDependencies 仓库](https://github.com/paulscherrerinstitute/PsiFpgaLibDependencies) 实际说明为准。

2. 在仓库根目录运行帮助命令：

   ```bash
   python scripts/dependencies.py -help
   ```

**需要观察的现象**：帮助文本会列出该脚本支持的动作（例如获取依赖、列出依赖、清理已拉取的依赖等），并列出从 README 解析出的依赖清单。

**预期结果**：能正常打印出帮助信息，且其中出现的依赖项与 README Dependencies 段（psi_common、psi_tb、PsiSim、PsiIpPackage）一致。

**如果无法确定运行结果**：明确标注「待本地验证」。在没有安装 `PsiFpgaLibDependencies` 包的环境下，该命令会抛出 `ModuleNotFoundError: No module named 'PsiFpgaLibDependencies'`，这恰恰印证了脚本第 7 行的导入依赖。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `dependencies.py` 要用 `os.path.dirname(os.path.abspath(__file__))` 来定位目录，而不是直接写死 `"scripts"`？

> **参考答案**：为了让脚本“从哪里调用都能正确工作”。如果写死相对路径，当你从仓库根以外的目录运行脚本时，相对路径会指向错误的位置；用 `__file__` 取脚本自身的真实路径则不受当前工作目录影响。

**练习 2**：脚本第 13 行 `Parse.FromReadme(...)` 解析的输入文件是什么？如果有人把 README 里 Dependencies 段的 HTML 注释标记删掉，会发生什么？

> **参考答案**：输入是 `README.md`（位于脚本上一级目录）。如果删掉 `<!-- DO NOT CHANGE FORMAT -->` 和 `<!-- END OF PARSED SECTION -->` 这两个标记，解析器将无法确定解析范围，很可能解析失败或漏掉依赖，导致脚本拉取不到所需的库。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个“**搭建可仿真环境**”的小任务。

**任务**：假设你刚 clone 了 `vivadoIP_mem_test`，要让下一讲的回归仿真跑起来。请按顺序回答并操作：

1. **画目录树**：运行 `git ls-files`，画出仓库自身的目录树，标注 `hdl`、`tb`、`sim`、`scripts`、`drivers`、`bd`、`xgui`、`doc` 各自的职责（参考 4.1.1）。

2. **盘点依赖**：从 README 的 Dependencies 段，列出四个外部依赖，并标注哪些是“运行必需”、哪些是“仅开发”。指出它们各自应放在公共根目录的哪个子文件夹（`TCL/` 还是 `VHDL/`）。

3. **解释嵌套**：用一句话解释为什么 `sim/config.tcl` 里 `set LibPath "../../.."` 能正确指向公共根目录（提示：从 `sim/` 往上数三级）。

4. **拉取依赖（可选实操）**：在安装好 `PsiFpgaLibDependencies` 包的前提下，运行 `python scripts/dependencies.py -help` 查看帮助；若条件允许，按帮助提示执行实际拉取，然后用 `ls` 确认公共根下出现了 `TCL/PsiSim`、`VHDL/psi_common` 等目录。

5. **验证闭环**：拉取完成后，进入 `sim/` 目录执行 `source ./run.tcl`（需要 Modelsim）。如果 Transcript 出现 PsiSim 加载成功的信息，就说明你的目录结构与依赖全部正确——这正是下一讲 u1-l3 的主题。

> 说明：第 4、5 步依赖具体的本地工具链（Python 包、Modelsim）。若本地不具备，请明确标注「待本地验证」，并改为写出这两步各自的**成功判据**（第 4 步：帮助文本中包含四个依赖名；第 5 步：Transcript 无 `###ERROR###` 且出现仿真完成信息）。

## 6. 本讲小结

- 仓库按角色分八个目录：`hdl`（RTL）、`tb`（测试台）、`sim`（仿真脚本）、`scripts`（构建/CI）、`drivers`（C 驱动）、`bd`（Block Design）、`xgui`（IP 界面）、`doc`（文档）。
- 本仓库不能独立运行：它依赖外部库 `psi_common`（运行必需）、`psi_tb`（仿真）、`PsiSim`（仿真框架）、`PsiIpPackage`（封装框架）。
- 依赖清单写在 README 里被 HTML 注释包裹的 Dependencies 段，既是给人看的，也是给脚本解析的。
- 本仓库必须摆在公共根目录下的 `VivadoIp/vivadoIP_mem_test/` 位置——这解释了 `sim/` 脚本里反复出现的 `../../..` 相对路径。
- `scripts/dependencies.py` 借助外部包 `PsiFpgaLibDependencies`，读取 README 并自动把依赖 clone 到正确位置，省去手动摆放的麻烦。
- `sim/config.tcl` 是依赖真实性的交叉证据：它确实从外部 `VHDL/psi_common/hdl`、`VHDL/psi_tb/hdl` 编译源文件。

## 7. 下一步学习建议

本讲把“仓库长什么样、需要什么依赖、怎么把依赖摆好”讲清楚了。下一讲 **u1-l3 运行仿真：PsiSim / Modelsim 回归测试** 会直接用到本讲建立的目录结构认知——在依赖齐全后，进入 `sim/` 跑 `source ./run.tcl`，并理解 `run.tcl` → `config.tcl` → `ci.do` → `ciFlow.py` 这条从本地仿真到 CI 判定的完整链路。

如果你想提前预热，可以先读：
- [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/run.tcl)（回归入口）
- [sim/ci.do](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/sim/ci.do)（CI do 文件）
- [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/scripts/ciFlow.py)（CI 驱动脚本）

完成仿真链路后，第二单元将进入寄存器地图（`hdl/mem_test_pkg.vhd`）与 C 驱动，从软硬件接口的角度重新认识这个 IP。
