# 仓库与目录结构导览

## 1. 本讲目标

上一讲（u1-l1）我们建立了全局印象：ADI HDL 仓库是一套面向 ADI 评估板的 FPGA 参考设计素材库，交付物是 Verilog/VHDL 源码加 Tcl 脚本。本讲要在此基础上把仓库「拆开」，让你学会：

1. 说出仓库顶层每一个目录和文件各自承担什么职责。
2. 区分 `library/`（可复用 IP 积木）和 `projects/`（整板参考设计）两套组织规律，并识别它们各自目录里的文件类型。
3. 分清三处脚本目录——`scripts/`、`projects/scripts/`、`library/scripts/`——各自服务谁、被谁调用。

学完本讲，你应该能在不看文档的情况下，凭目录结构判断「这段内容是设计源码还是构建脚本」「这个文件属于库 IP 还是某个具体工程」。

## 2. 前置知识

在阅读本讲前，建议你已经了解上一讲引入的几个概念（这里只做一句话回顾，不再展开）：

- **综合 / 实现 / 比特流**：把 HDL 源码变成能烧进 FPGA 的二进制文件的过程。
- **工具链**：本仓库支持 AMD Xilinx Vivado、Intel Quartus、Lattice Radiant 三家厂商工具。
- **Tcl**：工具链用来做自动化的脚本语言；本仓库大量用 Tcl 描述「怎么建工程、怎么打包 IP」。
- **AXI 总线**：FPGA 内部模块之间、以及 FPGA 与 CPU 之间通信的标准总线。
- **参考设计**：一块完整评估板对应的、可直接综合的 FPGA 工程。

本讲不涉及任何代码逻辑细节，只讲「东西放在哪里、为什么放在那里」。如果你能熟练使用 `ls`（列目录）和 GitHub 网页浏览，就足以完成本讲所有实践。

## 3. 本讲源码地图

本讲涉及的关键文件（注意都是「说明性」文件和目录组织，而非具体 IP 逻辑）：

| 文件 / 目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md) | 仓库总入口，说明它是什么、怎么构建、用哪个分支 |
| [projects/Readme.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md) | `projects/` 目录的总说明，规定每个工程的 README 规范 |
| [projects/common/README.md](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/README.md) | 载板（carrier）与评估板（eval）两层 README 模板的说明 |
| [docs/index.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst) | Sphinx 文档总入口，揭示 `docs/` 的四大板块 |
| [Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile) | 顶层 Makefile，演示目录自动发现机制 |

此外会用到 `library/`、`projects/`、`scripts/`、`projects/scripts/`、`library/scripts/`、`docs/` 这几个目录的实际内容作为例子。

## 4. 核心概念与源码讲解

### 4.1 顶层目录职责

#### 4.1.1 概念说明

站在仓库根目录往下看，所有顶层条目可以归成四大类：

1. **设计源码目录**：`library/`（IP 积木）和 `projects/`（整板工程）。这是仓库的「主角」，绝大部分代码都在这里。
2. **构建自动化**：`Makefile`、`quiet.mk`（顶层构建编排）以及 `scripts/`、`projects/scripts/`、`library/scripts/`（三处脚本）。它们负责把源码「喂」给工具链。
3. **文档**：`docs/`（Sphinx 文档源码）以及 `README.md`、`CONTRIBUTING.md`。
4. **项目元信息**：一组 `LICENSE*` 文件（不同模块使用不同许可证）。

需要特别强调一个直觉：**本仓库没有传统意义上的「程序入口」**。它不是运行起来给你看的程序，而是一堆「素材」。真正的「入口」是你在某个工程目录下敲下的那条 `make` 命令——上一讲已经用过它。

#### 4.1.2 核心流程

下面这张表把根目录的每一个条目都对应到上述分类。读者可以用 `ls -1F` 在本地复现这张表。

| 顶层条目 | 类别 | 一句话职责 |
| --- | --- | --- |
| `library/` | 设计源码 | 约 90 余个可复用 IP 模块（积木） |
| `projects/` | 设计源码 | 约 90 余个评估板整板参考设计 |
| `Makefile` | 构建自动化 | 顶层编排，自动发现 `projects/` 下的目标 |
| `quiet.mk` | 构建自动化 | 被顶层 Makefile 包含，提供日志/构建宏 |
| `scripts/` | 构建自动化 | 全局脚本，目前主要是 `adi_env.tcl`（工具版本与环境） |
| `docs/` | 文档 | Sphinx 文档源码（架构/构建/库/工程/寄存器表） |
| `README.md` | 文档 | 仓库总入口说明 |
| `CONTRIBUTING.md` | 文档 | 贡献流程 |
| `LICENSE`、`LICENSE_ADIBSD`、`LICENSE_GPL2`、`LICENSE_LGPL` 等 | 项目元信息 | 各模块适用的许可证 |
| `hdl-tutorial/` | （本手册） | 你正在阅读的学习手册目录 |

> 说明：`hdl-tutorial/` 是本学习手册生成的目录，不属于 ADI 官方仓库内容，这里列出只是为了让你在本地 `ls` 时不会困惑。

#### 4.1.3 源码精读

README 开头一句话就点明了仓库的本质——它装的是 **HDL 源码 + Tcl 脚本**，用来配合 Xilinx/Intel 工具链生成 FPGA 设计：

[README.md:L30-L36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36) 说明仓库「contains HDL code (Verilog or VHDL) and the required Tcl scripts」，并用 Xilinx 和/或 Intel 工具链构建。

README 还指明了 `projects/`（评估板）和 `projects/common`（载板）这两个关键位置：

[README.md:L60-L63](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L60-L63) 说明仓库为不同的 ADI 评估板（指向 `projects`）提供参考设计，这些设计基于 Xilinx/Intel 的开发板（指向 `projects/common`）或独立运行。

构建入口也在 README 里给出，强调「进入具体工程目录再 `make`」：

[README.md:L88-L91](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L88-L91) 给出典型命令 `cd projects/fmcomms2/zcu102` 后 `make`。

顶层 Makefile 则展示了「目录自动发现」机制——它并不手写一份工程清单，而是用 `wildcard` 直接扫描 `projects/` 下所有子目录：

[Makefile:L24-L28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L28) 用 `$(wildcard projects/*)` 枚举所有工程目录，并据此生成形如 `fmcomms2.zcu102` 的 `proj.board` 目标名。

[Makefile:L32-L33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L32-L33) 把 `make fmcomms2.zcu102` 中的点号 `.` 替换成 `/`，从而进入 `projects/fmcomms2/zcu102` 子目录执行 make。这正是上一讲提到的「`make projname.carrier` 即定位到对应子目录」的实现原理。

#### 4.1.4 代码实践

**实践目标**：亲手把根目录的每一个条目归到四大类，建立「看到文件名就知道它属于什么」的直觉。

**操作步骤**：

1. 在仓库根目录执行 `ls -1F`（`-F` 会给目录加 `/`、给可执行文件加 `*`，便于区分）。
2. 也可以用只读 git 命令查看被纳管的顶层条目：`git ls-files | cut -d/ -f1 | sort -u`。
3. 对照上面的「顶层条目」分类表，给本地列出的每一项标注类别（设计源码 / 构建自动化 / 文档 / 项目元信息）。

**需要观察的现象**：`library/`、`projects/`、`docs/`、`scripts/` 都带 `/`（是目录）；`Makefile`、`quiet.mk`、`README.md`、`CONTRIBUTING.md`、各 `LICENSE*` 都不带后缀（是文件）。

**预期结果**：你能得到一张与「4.1.2 核心流程」中表格一致的分类。如果某项无法归类，把它记下来——可能是本手册生成物（如 `hdl-tutorial/`）或后续新增内容。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LICENSE` 文件有不止一个（如 `LICENSE_ADIBSD`、`LICENSE_GPL2`、`LICENSE_LGPL`）？

> **参考答案**：因为仓库里不同模块由不同团队、在不同历史阶段开发，各自带有独立的许可证条款。README 的 License 章节明确说明「单个模块可能附带独立的许可条款」，使用某段源码前需要看它对应的 LICENSE。

**练习 2**：顶层 Makefile 是「手写工程清单」还是「自动发现」？依据是什么？

> **参考答案**：自动发现。依据 [Makefile:L24-L28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L28) 使用 `$(wildcard projects/*)` 动态扫描目录。这意味着新增一个工程目录后会自动被识别为构建目标，无需手动登记。

### 4.2 library 与 projects 的子目录约定

#### 4.2.1 概念说明

`library/` 和 `projects/` 都是设计源码，但组织规律完全不同，这是本讲最重要的区分点：

- **`library/` = 可复用 IP 积木（扁平结构）**。每个子目录就是一个独立的 IP 模块，目录名即模块名。例如 `library/axi_dmac/` 是 DMA 引擎、`library/axi_ad9361/` 是 AD9361 射频收发器的数据通路 IP。它们不针对某一块板子，而是被各种工程「拼装」复用。
- **`projects/` = 整板参考设计（两层结构）**。每个子目录对应一块 ADI 评估板（如 `projects/fmcomms2/`）。而每块评估板下面又按「载板」再分一层（如 `projects/fmcomms2/zcu102/`），因为同一块评估板可以插在不同的 FPGA 载板上。

一句话记忆：**library 是「零件」，projects 是「整车图纸」；整车图纸会引用零件。**

`library/` 内部除 IP 模块外，还有几个「框架/工具」子目录：`common/`（全仓共享的基础 Verilog）、`interfaces/`（接口定义）、`jesd204/`、`spi_engine/`（两大协议框架，各自又含多个子模块）、以及 `util_*` 系列（小工具 IP，如 FIFO、跨时钟域）。粗略统计，`library/` 下约有 90 余个模块目录，其中包含约 53 个 `axi_*` 核心 IP、约 28 个 `util_*` 辅助 IP，以及前述框架子库。

#### 4.2.2 核心流程

**library 单个 IP 模块的内部约定**（以 `library/axi_dmac/` 为真实样本）：

| 文件类型 | 含义 |
| --- | --- |
| `Makefile` | 声明本 IP 的源文件、约束、以及多厂商依赖 |
| `*.v` | Verilog 设计源码（如顶层 `axi_dmac.v`、`axi_dmac_regmap.v`） |
| `*_ip.tcl` | Xilinx 端 IP 打包脚本 |
| `*_hw.tcl` | Intel 端 IP 打包脚本 |
| `*_ltt.tcl` | Lattice 端 IP 打包脚本 |
| `*_constr.sdc` / `*_constr.ttcl` | 时序/工具约束 |
| `bd/` | 块设计（Block Design）相关的 Tcl 片段 |
| `interfaces/` | 接口定义文件 |
| `tb/` | 仿真测试平台（testbench） |

**projects 的两层约定**（以 `projects/fmcomms2/` 为真实样本）：

```
projects/fmcomms2/              ← 评估板层（eval）：与具体载板无关的公共设计
├── Makefile
├── README.md
├── common/                     ← 评估板的公共块设计（如 fmcomms2_bd.tcl）
├── zcu102/                     ← 载板层（carrier）：针对 ZCU102 的特化
│   ├── Makefile
│   ├── README.md
│   ├── system_top.v            ← 顶层 Verilog（例化 wrapper、IO 缓冲）
│   ├── system_bd.tcl           ← 块设计脚本（连线各个 IP）
│   ├── system_project.tcl      ← 建工程、跑综合实现的 Tcl 入口
│   └── system_constr.xdc       ← 引脚与时序约束
├── zc702/                      ← 同一评估板，换一块载板
├── zc706/
└── zed/
```

此外还有一个**特别容易混淆**的位置：`projects/common/`。它和 `projects/fmcomms2/common/` 是两回事：

- `projects/fmcomms2/common/`：**评估板**的公共设计（与载板无关的 FMCOMMS2 块设计）。
- `projects/common/`：**载板**的基设计（base design），按载板名再分一层，如 `projects/common/zcu102/`，里面是该载板的系统顶层、约束、FMC 引脚映射等。

也就是说，命名里的 `common` 出现在不同层级、含义不同：在评估板目录下是「该板的公共部分」，在 `projects/` 下是「所有载板的基设计仓库」。下一讲（u2-l1）会专门讲这两层如何叠加成一个完整工程。

#### 4.2.3 源码精读

`projects/Readme.md` 明确规定了每个工程都要自带一份说明文件，并列出它应包含的链接：

[projects/Readme.md:L10-L15](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md#L10-L15) 说明每个工程应有自己的 `Readme.md`，包含板卡链接、器件产品页、wiki 文档、Linux/no-OS 驱动文档等在线指引。

`projects/common/README.md` 则给出了评估板层与载板层各自的 README 模板规范：

[projects/common/README.md:L1-L3](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/README.md#L1-L3) 指出评估板层 README 用 `template_readme_evalboard.md`，位于 `hdl/projects/$evalboard/README.md`。

[projects/common/README.md:L13-L19](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/README.md#L13-L19) 指出载板层 README 有三套模板（`template1/2/3_readme_carrier.md`），分别对应「无参数」「有 make 参数」「固定配置」三种工程。

至于真实目录样本，可在本地直接查看：
- 库 IP 样本：`ls -1 library/axi_dmac/` 会看到上面「library 单个 IP 模块」表中的全部文件类型（27 个 `.v`、3 个打包 `.tcl`、`.sdc`/`.ttcl` 约束，以及 `bd/`、`interfaces/`、`tb/` 子目录）。
- 工程样本：`ls -1 projects/fmcomms2/zcu102/` 恰好是「标准五件套」加 README——`Makefile`、`system_top.v`、`system_bd.tcl`、`system_project.tcl`、`system_constr.xdc`、`README.md`。
- 载板基设计样本：`ls -1 projects/common/zcu102/` 会看到 `system_top.v`、`system_bd.tcl`、`system_project.tcl`、`zcu102_system_bd.tcl`、`zcu102_system_constr.xdc` 以及 `zcu102_fmc0_hpc.txt`、`zcu102_fmc1_hpc.txt`（FMC 连接器引脚映射表）。

#### 4.2.4 代码实践

**实践目标**：亲手对比「一个 library 模块」和「一个 project」的目录构成，体会「零件」与「整车图纸」的差异。这是本讲的主实践任务。

**操作步骤**：

1. 选一个库 IP 模块，例如 `axi_dmac`：
   ```bash
   ls -1 library/axi_dmac/
   ```
2. 选一个工程，例如 `fmcomms2` 的 `zcu102` 载板：
   ```bash
   ls -1 projects/fmcomms2/zcu102/
   ls -1 projects/fmcomms2/common/
   ```
3. 用文件后缀统计两边各有几类文件，例如：
   ```bash
   ls -1 library/axi_dmac/ | sed 's/.*\.//' | sort | uniq -c
   ```

**需要观察的现象**：
- library 模块里**全是设计源码**（`.v` 占多数）和**打包脚本**（`*_ip.tcl` / `*_hw.tcl` / `*_ltt.tcl`），没有「建工程」脚本。
- 工程目录里**没有任何 RTL 设计**，只有「怎么拼」的脚本：`system_bd.tcl`（连线）、`system_project.tcl`（建工程）、`system_top.v`（顶层例化）、`system_constr.xdc`（约束）、`Makefile`（编排）。
- 工程目录的 `.v` 文件（`system_top.v`）通常只是「把别人的 IP 例化并接好 IO」，并不是新功能逻辑。

**预期结果**：你能填出类似下表的对比（以 `axi_dmac` 与 `fmcomms2/zcu102` 为例）：

| 维度 | library 模块（`axi_dmac`） | project（`fmcomms2/zcu102`） |
| --- | --- | --- |
| 主要内容 | Verilog 设计源码 + 寄存器映射 | Tcl 连线脚本 + 顶层例化 |
| 典型 `.v` 数量 | 多个（27 个） | 1 个（`system_top.v`） |
| 是否含打包脚本 | 是（`*_ip.tcl` 等） | 否 |
| 是否含块设计脚本 | 部分（`bd/` 片段） | 是（`system_bd.tcl`） |
| 是否针对具体板卡 | 否，可复用 | 是，绑定评估板+载板 |

> 如果你在本地 `ls` 的结果与本讲描述有出入，以本地实际结果为准——仓库会随版本演进增删文件。

#### 4.2.5 小练习与答案

**练习 1**：`projects/common/zcu102/` 和 `projects/fmcomms2/common/` 里的 `common` 各自指什么？

> **参考答案**：前者是**载板**层概念——`projects/common/<carrier>/` 存放某块载板（如 ZCU102）的基设计，被所有插在该载板上的评估板共用；后者是**评估板**层概念——`projects/<eval>/common/` 存放某块评估板（如 FMCOMMS2）与载板无关的公共块设计。

**练习 2**：为什么 `projects/fmcomms2/` 下面会有 `zcu102`、`zc702`、`zc706`、`zed` 等多个并列子目录？

> **参考答案**：因为同一块 FMCOMMS2 评估板可以插在多块不同的 FPGA 载板上。每个载板的 FPGA 型号、引脚、DDR 都不同，所以需要为每种「评估板+载板」组合各准备一份特化设计（顶层、约束、连线）。这与顶层 Makefile 里 `fmcomms2.zcu102`、`fmcomms2.zed` 等多目标一一对应。

### 4.3 构建脚本目录的定位

#### 4.3.1 概念说明

仓库里有**三处**名字里带 `scripts` 的位置，初学者很容易混。区分的关键是看它们「服务谁」：

1. **`scripts/`（全局）**：整个仓库级别的脚本。目前最核心的是 `adi_env.tcl`——它集中声明当前分支要求的三家工具链版本，以及 `ADI_HDL_DIR`、`ADI_IGNORE_VERSION_CHECK` 等环境变量。无论你构建哪个工程、打包哪个 IP，最终都会用到这里定义的环境。
2. **`projects/scripts/`（工程级）**：服务「整板工程」的构建。这里既有 Tcl 助手（如 `adi_project_xilinx.tcl` 封装「建工程」，`adi_board.tcl` 封装「块设计连线」），也有 Make 片段（如 `project-xilinx.mk`、`project-intel.mk`、`project-lattice.mk`）。
3. **`library/scripts/`（库/IP 级）**：服务「IP 打包」。这里的核心是 `library.mk`（库构建的 Make 主体）和 `adi_ip_xilinx.tcl` / `adi_ip_intel.tcl` / `adi_ip_lattice.tcl`（把 Verilog 打包成三家厂商 IP 的 Tcl 助手）。

记忆口诀：**全局环境在 `scripts/`，建工程在 `projects/scripts/`，打包 IP 在 `library/scripts/`。**

#### 4.3.2 核心流程

| 脚本目录 | 文件数（实测） | 主要内容 | 被谁调用 |
| --- | --- | --- | --- |
| `scripts/` | 1 | `adi_env.tcl`：工具版本与环境变量 | 所有工程与库构建都会 source |
| `projects/scripts/` | 21 | `adi_project_xilinx.tcl`、`adi_board.tcl`、`adi_make_boot_bin.tcl`、`project-xilinx.mk`/`project-intel.mk`/`project-lattice.mk` 等 | 各工程的 `Makefile` 与 `system_*.tcl` |
| `library/scripts/` | 7 | `library.mk`、`adi_ip_xilinx.tcl`/`adi_ip_intel.tcl`/`adi_ip_lattice.tcl`、`lattice_tool_set.mk` 等 | 各库模块的 `Makefile` 与 `*_ip.tcl`/`*_hw.tcl`/`*_ltt.tcl` |

这些脚本之间形成清晰的「分层调用链」：

```text
顶层 Makefile / quiet.mk
        │  （编排：发现目录、递归进入子目录）
        ▼
projects/<eval>/<carrier>/Makefile ── include ──▶ projects/scripts/project-xilinx.mk
        │                                              │
        │  （建工程、综合、实现）                        │ （打包 IP 依赖）
        ▼                                              ▼
projects/scripts/adi_project_xilinx.tcl        library/<ip>/Makefile ── include ──▶ library/scripts/library.mk
projects/scripts/adi_board.tcl                                                      │
                                                                                     ▼
所有脚本共享 ──▶ scripts/adi_env.tcl（工具版本与环境）           library/scripts/adi_ip_xilinx.tcl
```

注意：这是「调用关系示意」，不是某一份真实源码；具体 include 与 source 语句会在第 3、4 单元逐条精读。

#### 4.3.3 源码精读

由于三处脚本目录的内容是文件清单（而非单行代码），这里用本地命令列出真实内容（你可在本地复现）：

- **`scripts/`（全局）**：执行 `ls -1 scripts/` 可见仅有 `adi_env.tcl`。它是整个仓库「工具版本中枢」，README 也指向它：

  [README.md:L129-L132](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L129-L132) 指出各分支对应的 Vivado/Quartus 版本可在 `scripts/adi_env.tcl` 中查到（需在你所切换的分支上查看）。

- **`projects/scripts/`（工程级）**：执行 `ls -1 projects/scripts/` 可见约 21 个文件，典型如 `adi_project_xilinx.tcl`（建工程助手）、`adi_board.tcl`（连线助手）、`adi_make_boot_bin.tcl`（打 BOOT.BIN）、`gtwizard_generator.tcl`（收发器生成），以及 `project-xilinx.mk`/`project-intel.mk`/`project-lattice.mk`/`project-toplevel.mk` 四个 Make 片段。

- **`library/scripts/`（库/IP 级）**：执行 `ls -1 library/scripts/` 可见约 7 个文件，核心是 `library.mk`（库构建主体）与 `adi_ip_xilinx.tcl`/`adi_ip_intel.tcl`/`adi_ip_lattice.tcl`（三厂商 IP 打包助手），外加 `lattice_tool_set.mk` 等。

#### 4.3.4 代码实践

**实践目标**：凭直觉判断任意一个脚本「属于哪一层、被谁用」。

**操作步骤**：

1. 列出三处脚本目录的内容：
   ```bash
   ls -1 scripts/
   ls -1 projects/scripts/
   ls -1 library/scripts/
   ```
2. 对 `projects/scripts/` 中的每个文件，根据**文件名前缀**猜测它的职责层级：
   - 以 `adi_project_*` 开头 → 与「建工程」相关；
   - 以 `adi_ip_*` 开头 → 一般在 `library/scripts/`（与「打包 IP」相关）；
   - 以 `project-*.mk` 命名 → 工程级 Make 片段；
   - `library.mk` / `lattice_tool_set.mk` → 库级 Make。
3. 验证你的判断：在某个工程 `Makefile`（如 `projects/fmcomms2/zcu102/Makefile`）里搜索 `include`，看它引入的是不是 `projects/scripts/` 下的片段（这一步只需看 `include` 行，不必读懂全部逻辑）。

**需要观察的现象**：工程 `Makefile` 会 `include` 来自 `projects/scripts/` 的 `.mk`；而库 `Makefile`（如 `library/axi_dmac/Makefile`）会 `include` 来自 `library/scripts/` 的 `.mk`。两边互不串台。

**预期结果**：你能说出每个脚本目录的「消费者」——`scripts/` 被所有人共用、`projects/scripts/` 被 `projects/` 下的 Makefile/Tcl 消费、`library/scripts/` 被 `library/` 下的 Makefile/Tcl 消费。具体的 include 语句留待第 3、4 单元精读，本讲只需建立「归属感」。

> 如果某个文件名你判断不准，标注「待确认」，不必硬猜——本讲目标是建立目录直觉，而非记熟每个脚本。

#### 4.3.5 小练习与答案

**练习 1**：`adi_project_xilinx.tcl` 和 `adi_ip_xilinx.tcl` 一个在 `projects/scripts/`、一个在 `library/scripts/`，为什么分开存放？

> **参考答案**：因为它们的职责层级不同。`adi_project_xilinx.tcl` 服务「整板工程」（创建工程、加文件、跑综合实现），属于工程级；`adi_ip_xilinx.tcl` 服务「IP 打包」（把一段 Verilog 变成可被块设计拖拽的 Vivado IP），属于库级。把它们放在各自消费者附近的脚本目录，便于维护也体现了「工程层 vs 库层」的边界。

**练习 2**：如果有人想修改「当前分支要求的 Vivado 版本号」，应该改哪个文件？为什么？

> **参考答案**：应该改 `scripts/adi_env.tcl`。因为它是仓库**全局**的环境中枢，README 明确指出各分支的工具版本在此声明（[README.md:L129-L132](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L129-L132)）。它不属于工程级或库级，而是被所有构建流程共享。

## 5. 综合实践

把本讲三个模块串起来，完成一次「目录侦探」任务：

1. **选定研究对象**：任选一个库 IP（如 `library/axi_dmac`）和它被使用的一个工程（如 `projects/fmcomms2/zcu102`）。
2. **产出三份清单**：
   - 库 IP 的文件清单（按后缀分类，标注哪些是设计源码、哪些是打包脚本、哪些是约束/测试）。
   - 工程的文件清单（标注每个文件在「评估板层 / 载板层」中属于哪一层、是源码还是构建脚本）。
   - 该工程与该 IP 各自依赖的脚本目录（`projects/scripts/` 还是 `library/scripts/`，以及全局的 `scripts/`）。
3. **画一张关系图**：用文字或箭头表示「工程引用库 IP、工程用 `projects/scripts/`、库 IP 用 `library/scripts/`、二者共享 `scripts/`」的分层关系。
4. **回答一个判断题**：给你一个陌生文件路径（例如 `library/util_cdc/sync_bits.v` 或 `projects/scripts/adi_board.tcl`），仅凭路径判断它是「设计源码」还是「构建脚本」，并说出依据。

完成本任务后，你应当能在脑中形成一张稳定的「仓库地图」，后续阅读任何文件都能快速定位它属于哪一层。

## 6. 本讲小结

- 仓库顶层分为四类：设计源码（`library/`、`projects/`）、构建自动化（`Makefile`、`quiet.mk`、三处 `scripts/`）、文档（`docs/` 等）、项目元信息（`LICENSE*` 等），且没有传统程序入口，入口是工程目录下的 `make`。
- `library/` 是扁平的可复用 IP 积木（约 90 余个模块，含 `axi_*` 核心、`util_*` 工具、`jesd204`/`spi_engine` 框架子库）；`projects/` 是两层结构（评估板层 + 载板层）的整板参考设计。
- `projects/common/`（载板基设计）与 `projects/<eval>/common/`（评估板公共设计）里的 `common` 含义不同，是最易混淆的点。
- 工程目录是「标准五件套」（`Makefile`、`system_top.v`、`system_bd.tcl`、`system_project.tcl`、`system_constr.xdc`），几乎不含新 RTL，只是「拼装与连线」。
- 三处脚本目录按服务对象分层：全局环境在 `scripts/`、建工程在 `projects/scripts/`、打包 IP 在 `library/scripts/`。
- 顶层 Makefile 用 `wildcard` 自动发现 `projects/` 子目录，把 `make proj.board` 映射为进入 `projects/proj/board` 子目录。

## 7. 下一步学习建议

你已经看清了「东西放在哪里」，接下来建议：

1. **u1-l3 构建环境与工具链版本**：进入 `scripts/adi_env.tcl` 的内部，搞清当前分支要求哪家工具的哪个版本，以及 `ADI_IGNORE_VERSION_CHECK` 等环境变量的作用——这是把仓库「跑起来」的前提。
2. **u1-l4 构建第一个工程**：跟着 `projects/fmcomms2/zcu102` 的 `Makefile` → `system_project.tcl` 链路，亲手走一遍从 `make` 到 Vivado 启动的完整流程。
3. 之后第 2 单元（u2）会带你剖析单个工程「五件套」每一件的具体职责，以及三层架构如何叠加——那是对本讲「projects 两层结构」的深入。
