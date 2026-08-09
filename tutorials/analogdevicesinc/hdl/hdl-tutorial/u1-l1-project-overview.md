# ADI HDL 项目总览：它是什么、为谁服务

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让你在 **不写一行代码** 的情况下，搞清楚下面几件事：

- 这个仓库到底 **是什么**：是一个可运行的应用程序，还是一堆可复用的硬件设计素材？
- 它 **为谁服务**：什么样的工程师会用到它，解决了他们的什么问题。
- 仓库的 **两大组成部分** `library/`（IP 库）和 `projects/`（参考设计）分别承担什么职责。
- 它如何与 **软件**（no-OS 裸机程序 / Linux）配合，构成一个完整的「硬件 + 软件」系统。

学完本讲，你应当能用自己的话向同事解释「ADI HDL 是干嘛的」，并能看懂仓库顶层目录里每个文件夹的用途。具体的「怎么把它编译出来」留给后续讲义（见第 7 节）。

## 2. 前置知识

本讲面向零基础读者，但在继续之前，建议你先对以下概念有 **一个直观印象**（不必精通）：

- **FPGA（现场可编程门阵列）**：一种芯片，它的内部逻辑可以由用户用硬件描述语言「画」出来。你可以把它理解成「可以反复重新接线、重新定义功能的芯片」。
- **HDL（硬件描述语言，Hardware Description Language）**：用来描述数字电路的语言，本仓库主要使用 **Verilog**，少量使用 **VHDL**。它们写出来的是「电路」，而不是像 C/Python 那样逐行执行的指令。
- **综合 / 实现 / 比特流（bitstream）**：把 HDL 代码「翻译」成 FPGA 能加载的二进制文件（`.bit`）的过程，分别叫做综合（synthesis）、实现（implementation），最终产物是 **比特流**。
- **工具链（tool chain）**：完成上述翻译工作的厂商软件，本仓库主要面向 **AMD Xilinx Vivado**、**Intel Quartus**，同时也跟踪 **Lattice** 版本。
- **Tcl**：一种脚本语言，FPGA 厂商工具普遍用它做自动化批处理。本仓库大量使用 Tcl 脚本来「指挥」工具链。
- **AXI**：ARM 提出的一套片上总线协议，FPGA 里的处理器（PS）和自定义逻辑（PL）之间通过它交换数据。你会在很多模块名里看到 `axi_` 前缀。
- **参考设计（reference design）**：厂商给出的「官方示例工程」，告诉你某块电路板「应该这么连、这么配置」，可以作为你二次开发的起点。

如果你对上面某几个词完全陌生也没关系，本讲用到时都会再用大白话解释一遍。

## 3. 本讲源码地图

本讲只读 **文档与项目入口文件**，不深入任何 Verilog 设计细节。涉及的关键文件如下：

| 文件 | 作用 | 本讲用它来理解什么 |
| --- | --- | --- |
| `README.md` | 仓库的「门面」，说明项目定位、构建方式、分支与软件配套 | 项目定位、目标用户、软硬件关系 |
| `projects/Readme.md` | 针对参考设计子目录的导读 | `projects/` 目录的用途与每个工程的 README 约定 |
| `docs/index.rst` | Sphinx 文档首页，给出文档的三大板块导航 | 文档体系与 `library/projects/user_guide` 的划分 |
| `scripts/adi_env.tcl` | 集中管理工具版本与环境变量 | 仓库对「三家工具链」的支持意图 |
| `Makefile`（顶层） | 构建「总指挥」，自动发现工程并分发到子目录 | 顶层目录如何被 `make` 组织起来 |

> 提示：本讲引用的所有源码行号均基于当前 HEAD（`e57851ff`）。后续讲义会逐步深入 `Makefile`、Tcl 脚本与具体 IP 源码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：
- **4.1 项目定位与目标用户** —— 它是什么、给谁用。
- **4.2 library 与 projects 的分工** —— 仓库的两大组成部分。
- **4.3 软硬件配套（no-OS / Linux）** —— 它如何与软件仓库协作。

### 4.1 项目定位与目标用户

#### 4.1.1 概念说明

Analog Devices（ADI，模拟器件公司）生产大量 **数据转换器（ADC/DAC）、射频收发器、时钟芯片** 等模拟/混合信号器件。这些器件通常以 **评估板（evaluation board）** 的形式出售，方便客户评估芯片性能。

一块典型的 ADI 评估板长这样：

```
        FPGA 开发板                ADI 评估板
   ┌─────────────────┐        ┌──────────────────┐
   │  处理器(PS)      │        │  ADI ADC/DAC/RF  │
   │  可编程逻辑(PL)  │◄──────►│  数据转换芯片     │
   │  (Xilinx/Intel) │  FMC/  │                  │
   │                 │  PMOD/ │                  │
   └─────────────────┘  排针   └──────────────────┘
```

问题是：**FPGA 里应该放什么逻辑，才能和 ADI 的芯片正确通信、把采样数据搬进内存？** 这正是本仓库要回答的。

仓库 README 的开头一句话就点明了它的定位——它提供的是「HDL 代码（Verilog 或 VHDL）以及创建并构建某个 FPGA 示例设计所需的 Tcl 脚本」：

> HDL libraries and projects for various reference design and prototyping systems. This repository contains HDL code (Verilog or VHDL) and the required Tcl scripts to create and build a specific FPGA example design ... — [README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36)

注意其中的关键词：

- **libraries and projects**：它既是「库」又是「工程」。
- **example design / reference design**：它是「示例 / 参考设计」，不是某个最终产品应用。
- **Verilog or VHDL + Tcl**：交付物是硬件源码 + 自动化脚本。

所以，**本仓库的定位是**：一套面向 ADI 评估板的 **FPGA 参考设计素材库**。它不是可执行程序，而是「半成品硬件设计 + 构建脚本」，用来生成可以烧进 FPGA 的比特流。

**目标用户**主要有三类：

1. **评估 ADI 芯片的工程师**：买了评估板，想快速跑通「FPGA ↔ ADI 芯片」的数据通路。
2. **基于 ADI 芯片做产品的开发者**：把参考设计当起点，裁剪、移植到自己的载板和产品上。
3. **学习 FPGA 数据采集系统的人**：把它当作一套高质量、工业级的开源 HDL 教材来阅读。

#### 4.1.2 核心流程

从「拿到仓库」到「比特流跑起来」的总体流程（本讲只到概念层，细节见后续讲义）：

```text
克隆仓库
   │
   ▼
选择一家 FPGA 工具链（Vivado / Quartus / Radiant）
   │
   ▼
cd 到某个具体工程目录，例如 projects/fmcomms2/zcu102
   │
   ▼
make  ──►  触发 Tcl 脚本  ──►  调用工具链
   │
   ▼
生成比特流（.bit）/ 硬件交付物（.xsa）
   │
   ▼
配合 no-OS 或 Linux 软件在板子上运行
```

其中关键的一步，README 里给出了最简示例——「cd 到工程目录，然后 make」：

```text
cd projects/fmcomms2/zcu102
make
```

这一段来自 [README.md:76-94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L76-L94)，它告诉你构建入口就是 GNU Make，而非某个 GUI 按钮。

**支持的工具链**：README 在「前置条件」里主要列出了 **AMD Xilinx Vivado** 与 **Intel Quartus** 两家。与此同时，仓库的环境脚本还跟踪了 **Lattice** 的版本，说明仓库具备面向三家厂商的设计意图：

- Vivado：`2025.1`
- Quartus Pro：`25.3.0`（Quartus Std：`24.1std.0`）
- Lattice：`2025.2`

> 这些版本号集中写在 `scripts/adi_env.tcl` 里，例如 [scripts/adi_env.tcl:19-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L19-L25)。工具版本的深入讲解见 `u1-l3` 讲义。

#### 4.1.3 源码精读

**① 项目的一句话定位** — [README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36)

这一段是全仓库最重要的「自我介绍」。它说明交付物 = HDL 源码 + Tcl 脚本，目标是「build a specific FPGA example design（构建一个具体的 FPGA 示例设计）」。读完它你就知道：**这里没有「运行项目」这个动作，只有「构建/综合项目」**。

**② 构建入口** — [README.md:76-94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L76-L94)

README 明确写出构建方式是 GNU Make，并给出 `cd projects/fmcomms2/zcu102 && make` 的最小命令。这一点非常关键：**它告诉你仓库的「用户接口」是命令行 make，而不是 IDE 里的菜单**。

**③ 分支与稳定性策略** — [README.md:122-132](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L122-L132)

README 给出选分支的两条建议：要稳定用「最新 release 分支」，要最新功能用 `main` 分支（可能不稳定）。每个分支对应的工具链版本可在 `scripts/adi_env.tcl` 中查到。这告诉你：**不同分支对应不同工具版本，不能随意混用**。

#### 4.1.4 代码实践

**实践目标**：通过精读 README 开头，用一句话锁定仓库定位。

**操作步骤**：

1. 打开 [README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36)。
2. 找出包含「HDL libraries and projects」的那一段。
3. 圈出三个关键词：`libraries`、`projects`、`Tcl scripts`。

**需要观察的现象**：README 在「How to build a project」一节用的是 `make`，而不是「点开某个工程文件」。

**预期结果**：你应当能写出类似这样一句话——「**本仓库用 Verilog/VHDL 与 Tcl 脚本，为 ADI 评估板生成可在 AMD Xilinx / Intel / Lattice FPGA 上综合的参考设计；用户通过 `make` 触发构建。**」

> 待本地验证：若你已在本地克隆仓库，可尝试在仓库根目录执行 `make`（不带参数），观察它打印的 `help` 提示，会列出 `make proj.board` 的用法。

#### 4.1.5 小练习与答案

**练习 1**：本仓库的交付物是「可执行程序」还是「硬件设计素材」？依据是什么？

> **参考答案**：是硬件设计素材。依据是 README 明确说它「contains HDL code (Verilog or VHDL) and the required Tcl scripts to create and build a specific FPGA example design」，即交付的是 HDL 源码 + Tcl 脚本，最终产物是 FPGA 比特流，而非 CPU 上跑的二进制程序。

**练习 2**：如果想要最稳定的代码，应该用 `main` 分支还是 release 分支？为什么？

> **参考答案**：应该用最新 release 分支。README 说明 `main` 分支是「最新但不总是稳定」，而 release 分支更稳定；且 `main` 分支的预构建文件「未在硬件上测试过」（见 [README.md:134-147](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L134-L147)）。

---

### 4.2 library 与 projects 的分工

#### 4.2.1 概念说明

仓库最核心的设计，是把它分成两个职责清晰的顶层目录：

- **`library/`（IP 库）**：一堆 **可复用的硬件模块（IP）**。每个子目录是一个独立模块，例如 `axi_dmac`（DMA 引擎）、`axi_ad9361`（AD9361 射频收发器的数据接口）、`util_axis_fifo`（AXI-Stream FIFO）等。这些模块像「乐高积木」，可以被不同工程反复拼装。
- **`projects/`（参考设计）**：针对 **某块具体评估板 + 某块具体 FPGA 载板** 的完整示例工程。它把 `library/` 里的积木按需拼起来，再加上引脚约束、时钟、处理器连接，组成一个能综合出比特流的完整设计。

可以用「积木箱」和「搭好的样品」来类比：

```text
   library/  ──  积木箱（可复用 IP）        projects/  ──  搭好的样品工程
   ┌──────────────────────┐                ┌──────────────────────────┐
   │ axi_dmac (DMA)        │   被拼装进 ──►  │ fmcomms2/zcu102          │
   │ axi_ad9361 (RF 数据)  │   被拼装进 ──►  │   = ADI FMCOMMS2 板      │
   │ util_axis_fifo (FIFO) │   被拼装进 ──►  │     + Xilinx ZCU102 载板 │
   │ ...近百个模块          │                │ adrv9009/zcu102          │
   └──────────────────────┘                │ adv7511/zed ...          │
                                            └──────────────────────────┘
```

`projects/Readme.md` 进一步说明了每个工程目录里应当有什么：它要求 **每个工程都有自己的 `Readme.md`**，里面要给出板上芯片的产品页、datasheet 链接、wiki 文档和驱动文档等在线指引——见 [projects/Readme.md:1-17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md#L1-L17)。这说明 `projects/` 不仅放代码，还承担「文档入口」的角色。

> 命名小贴士：`projects/<eval>/<carrier>/` 的两层结构很常见——外层 `<eval>` 是 ADI 评估板（如 `fmcomms2`），内层 `<carrier>` 是 FPGA 载板（如 `zcu102`、`zc706`、`kcu105`）。同一个评估板可以适配多种载板，所以一个 `eval` 下常有多个 `carrier` 子目录。

#### 4.2.2 核心流程

顶层 `Makefile` 用一段自动发现逻辑，把 `projects/` 下的子目录「扫描」成可构建的目标。其核心是 `SUBPROJECTS` 的生成（伪代码描述）：

```text
对 projects/ 下每一个子目录 projname：
    如果 projname/ 下直接有 system_project.tcl   → 这是一个「独立工程」，目标名 = projname
    否则，对 projname/ 下每一个含 Makefile 的子目录 archname
                                                 → 目标名 = projname.archname

最终，执行 make projname.archname 等价于：
    进入 projects/projname/archname/ 目录，再次执行 make
```

这条规则就写在顶层 [Makefile:24-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L33)。它解释了为什么 README 里说 `make adv7511.zed` 这种「工程名.载板名」的写法能直接工作——名字里的点号会被替换成目录分隔符，从而定位到 `projects/adv7511/zed/`。

至于 `library/`，它由顶层 `Makefile` 的 `lib` 目标统一构建（见 [Makefile:35-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L35-L36)），单独把所有 IP 打包好，供 `projects/` 引用。

#### 4.2.3 源码精读

**① 两大组成部分的证据** — [README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36)

README 反复出现 `libraries and projects`，正是仓库两大目录的来源。

**② projects 目录的文档约定** — [projects/Readme.md:1-17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md#L1-L17)

这段说明每个工程都应自带 `Readme.md`，并提供五类在线链接（板卡、芯片产品页、板卡 wiki、IP 驱动 wiki、芯片驱动 wiki）。它告诉你：**`projects/` 是面向「人」查阅的，每个工程都是自解释的**。

**③ docs 文档的三大板块** — [docs/index.rst:31-47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst#L31-L47)

文档首页的 `toctree` 把内容分成三块：`user_guide`（用户指南：架构、构建、移植、规范）、`library`（每个 IP 的文档）、`projects`（参考设计清单）。这与仓库的 `library/` + `projects/` 二分法完全对应。

**④ 工程目标的自动发现** — [Makefile:24-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L33)

这段 `PROJECTS` / `SUBPROJECTS` 的生成逻辑，是理解「为什么 `make fmcomms2.zcu102` 能定位到 `projects/fmcomms2/zcu102/`」的关键。

#### 4.2.4 代码实践

**实践目标**：亲手对比一个 `library` 模块和一个 `project` 的目录构成，体会「积木」与「样品」的差异。

**操作步骤**：

1. 列出某个 library 模块的文件，例如查看 `library/axi_ad9361/`（包含 `axi_ad9361.v`、`axi_ad9361_ip.tcl`、`axi_ad9361_hw.tcl` 等）。
2. 列出某个 project 的文件，例如 `projects/fmcomms2/zcu102/`（包含 `Makefile`、`system_top.v`、`system_bd.tcl`、`system_constr.xdc`、`system_project.tcl`、`README.md`）。
3. 在一张表里分别记录两边的文件类型（`.v`、`.tcl`、`.xdc`、`Makefile`、`.md`）。

**需要观察的现象**：

- `library/axi_ad9361/` 里主要是 **`.v` 设计源码** 和 **打包脚本（`_ip.tcl` / `_hw.tcl`）**——它是「积木」。
- `projects/fmcomms2/zcu102/` 里多了 **`system_top.v`（顶层）、`system_bd.tcl`（块设计连线）、`system_constr.xdc`（引脚/时序约束）、`Makefile`**——它是「把积木拼到具体板子上的样品」。

**预期结果**：你会得到一张类似下表的对比：

| 维度 | library 模块（如 `axi_ad9361`） | project（如 `fmcomms2/zcu102`） |
| --- | --- | --- |
| 核心产物 | 可复用 IP（Verilog + 打包脚本） | 针对具体板卡的完整工程 |
| 是否含顶层 `.v` | 通常无系统顶层，是模块 | 有 `system_top.v` 作为 FPGA 顶层 |
| 是否含约束 | 含本模块的 `_constr.xdc/_constr.sdc` | 含整板的 `system_constr.xdc` |
| 是否含连线脚本 | 有 IP 的 `bd.tcl` | 有整板块设计 `system_bd.tcl` |
| 文档 | 多在 `docs/library/` | 自带 `README.md` |

> 待本地验证：若本地已克隆仓库，可用 `ls library/axi_ad9361/` 和 `ls projects/fmcomms2/zcu102/` 实际比对。

#### 4.2.5 小练习与答案

**练习 1**：`projects/fmcomms2/zcu102` 这个路径里，`fmcomms2` 和 `zcu102` 分别代表什么？

> **参考答案**：`fmcomms2` 是 ADI 的 **评估板**（FMCOMMS2，基于 AD9361 的射频捷变收发器子板）；`zcu102` 是 **FPGA 载板**（Xilinx ZCU102 开发板）。两者通过 FMC 等连接器对接，共同构成一个参考设计。

**练习 2**：如果我只想构建 `fmcomms2` 适配 `zcu102` 载板的工程，应该在仓库根目录敲什么命令？它最终会进入哪个目录？

> **参考答案**：`make fmcomms2.zcu102`。根据顶层 [Makefile:24-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L33)，`SUBPROJECTS` 会把 `fmcomms2.zcu102` 展开为进入 `projects/fmcomms2/zcu102/` 并在其中执行 `make`。

**练习 3**：`library/` 和 `projects/` 的关系，更接近下面哪一个？
- A. `library` 是源码，`projects` 是它的测试用例。
- B. `library` 是可复用 IP 积木，`projects` 是用这些积木拼出的整板参考设计。
- C. 两者互不相关。

> **参考答案**：B。`library` 提供可复用模块，`projects` 把它们拼装成针对具体评估板 + 载板的完整设计。

---

### 4.3 软硬件配套（no-OS / Linux）

#### 4.3.1 概念说明

仅有 FPGA 比特流是不够的。一块评估板要真正「跑起来」，还需要 **软件** 来初始化芯片、配置寄存器、搬运数据。ADI 把这套生态分成三个配套仓库：

| 仓库 | 角色 | 类比 |
| --- | --- | --- |
| **hdl**（本仓库） | FPGA 硬件设计（比特流） | 给 FPGA「画好的电路」 |
| **no-OS** | 裸机（baremetal）C 驱动，无操作系统 | 最简「直接操作寄存器」的程序 |
| **linux** | Linux 内核驱动与设备树 | 完整 OS 下的驱动栈 |

README 的「Software」一节直接点明了这一点：

> In general, all the projects have no-OS (baremetal) and a Linux support. — [README.md:148-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L148-L152)

也就是说，**本仓库（hdl）只负责「硬件」那一半；软件那一半由另外两个仓库提供**。三者配合的完整图景如下：

```text
   ┌──────────────────────────── 板上系统 ────────────────────────────┐
   │                                                                  │
   │   软件（来自 no-OS 或 linux 仓库）                                 │
   │   ┌──────────────────────────────────────────┐                   │
   │   │  应用代码 → 驱动 → 通过 AXI 读写寄存器     │                   │
   │   └──────────────────────┬───────────────────┘                   │
   │                          │ AXI 总线                               │
   │   硬件（来自 hdl 仓库，即本仓库）              │                   │
   │   ┌──────────────────────▼───────────────────┐                   │
   │   │  FPGA 逻辑：axi_* IP、DMA、数据转换接口    │                   │
   │   └──────────────────────┬───────────────────┘                   │
   │                          │ 数字数据                               │
   │   ┌──────────────────────▼───────────────────┐                   │
   │   │  ADI 芯片：ADC / DAC / 射频收发器          │                   │
   │   └──────────────────────────────────────────┘                   │
   └──────────────────────────────────────────────────────────────────┘
```

这里有两个关键概念需要解释：

- **no-OS（裸机）**：不跑操作系统，程序直接在处理器上运行，直接读写寄存器。适合 **快速验证、低延迟、资源受限** 的场景。ADI 的 [no-OS 仓库](https://github.com/analogdevicesinc/no-OS) 提供各芯片的 C 驱动。
- **Linux**：跑完整 Linux，通过内核驱动（如 IIO 子系统）访问硬件。适合 **功能丰富、联网、复用 Linux 生态** 的场景。ADI 的 [linux 仓库](https://github.com/analogdevicesinc/linux) 提供内核与设备树。

`projects/Readme.md` 里要求每个工程的 README 提供「驱动（Linux 或 No-OS）」的 wiki 链接（见 [projects/Readme.md:13-15](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md#L13-L15)），正是因为 **硬件设计必须配合对应软件驱动才能工作**。

#### 4.3.2 核心流程

一个评估板「从零到跑通」的软硬件协作流程：

```text
1. 在 hdl 仓库：make → 得到比特流（.bit）/ 硬件交付物（.xsa）
        │
        ▼
2. 把比特流加载进 FPGA（或打包进 BOOT.BIN，见 u8-l4）
        │
        ▼
3. 在 no-OS 或 linux 仓库：编译对应驱动 / 设备树
        │
        ▼
4. 软件通过 AXI 总线初始化 hdl 里的 IP、配置 ADI 芯片寄存器
        │
        ▼
5. 数据在 ADI 芯片 ↔ FPGA IP ↔ 内存 之间正确流动
```

其中第 1 步是本仓库的职责，第 3、4 步是软件仓库的职责。理解这一点，你就明白为什么 `hdl` 仓库里看不到任何 C 代码或设备树——它们都在另外两个仓库。

> 重要：hdl 仓库的「寄存器映射」（regmap）是软硬件之间的 **契约**。FPGA IP 暴露哪些寄存器、每个寄存器的位域含义，会被软件驱动直接消费。这部分会在 `u4-l5`（寄存器映射与 up_axi）深入讲解。

#### 4.3.3 源码精读

**① 软件配套声明** — [README.md:148-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L148-L152)

这一节是理解「hdl 不是孤岛」的关键。它明确「所有工程都有 no-OS 和 Linux 支持」，并指向两个软件仓库。

**② 工程文档要求提供驱动链接** — [projects/Readme.md:13-15](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/Readme.md#L13-L15)

工程 README 模板里列出「驱动（Linux 或 No-OS）」的 wiki 链接要求，说明每个硬件工程都预设了「配套软件驱动」的存在。

**③ 文档体系的「用户指南」入口** — [docs/index.rst:31-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst#L31-L36)

文档首页把 `user_guide`（用户指南）放在最前面，里面包含架构、构建、移植、编码规范、发布等，是后续讲义的重要资料来源。

#### 4.3.4 代码实践

**实践目标**：理清「hdl / no-OS / linux」三者职责边界。

**操作步骤**：

1. 阅读 [README.md:148-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L148-L152)。
2. 在本仓库内搜索是否存在 C 语言源码或设备树文件（例如 `.c`、`.dts`）。

**需要观察的现象**：你会发现在 `hdl` 仓库里 **基本找不到** 芯片级的 C 驱动或 Linux 设备树——它们属于另外两个仓库。`hdl` 里只有 HDL 与 Tcl。

**预期结果**：你能口头复述——「hdl 产出比特流，no-OS/linux 提供驱动，两者通过 AXI 寄存器映射对接」。

> 待本地验证：可尝试在仓库根目录执行 `find . -name "*.c" | head`（只读检索），观察结果是否印证「hdl 仓库不含芯片驱动 C 代码」。注意：可能有极少量脚本辅助文件，但不会是芯片驱动主体。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `hdl` 仓库里看不到 ADI 芯片的 C 语言驱动？

> **参考答案**：因为 C 驱动属于「软件」范畴，分别在 [no-OS](https://github.com/analogdevicesinc/no-OS)（裸机）和 [linux](https://github.com/analogdevicesinc/linux)（内核）两个独立仓库。`hdl` 只负责 FPGA 硬件设计（比特流）。

**练习 2**：no-OS 和 Linux 两种软件方案，各适合什么场景？

> **参考答案**：no-OS（裸机）适合快速验证、低延迟、资源受限的场景，程序直接操作寄存器；Linux 适合功能丰富、需要联网或复用 Linux 生态（如 IIO）的场景，通过内核驱动访问硬件。README 明确「所有工程都同时支持这两种」，见 [README.md:148-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L148-L152)。

**练习 3**：hdl 仓库的硬件设计与软件驱动之间，靠什么「契约」对接？

> **参考答案**：靠 **寄存器映射（regmap）**。FPGA IP 通过 AXI 暴露一组寄存器，软件驱动按既定地址和位域去读写它们，从而控制硬件。这部分会在 `u4-l5` 讲义深入。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个总任务（即本讲的 `practice_task`）：

> **任务**：阅读 `README.md` 与 `docs/index.rst`，用一段话写出本仓库解决的 **三个具体问题**；并整理出仓库 **顶层目录** 中哪些属于构建脚本、哪些属于源码或文档，标注每个目录的作用。

**建议操作步骤**：

1. **精读定位**：读 [README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36) 与 [docs/index.rst:1-8](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/index.rst#L1-L8)，归纳仓库要解决的三个问题。

2. **目录归类**：浏览仓库根目录，把顶层条目按下表分类（参考答案已给出，建议你先自己填再对照）：

   | 顶层条目 | 类别 | 作用 |
   | --- | --- | --- |
   | `library/` | 源码（可复用 IP 库） | 近百个可复用硬件模块，是「积木箱」 |
   | `projects/` | 源码（参考设计） | 针对各评估板 + 载板的完整示例工程 |
   | `docs/` | 文档 | Sphinx 文档源（user_guide / library / projects / regmap） |
   | `scripts/` | 构建脚本 | 环境与版本管理（如 `adi_env.tcl`） |
   | `Makefile` / `quiet.mk` | 构建脚本 | 顶层 GNU Make 编排与构建宏 |
   | `library/scripts/`、`projects/scripts/` | 构建脚本 | 库与工程各自的 Make/Tcl 构建流水线 |
   | `README.md`、`CONTRIBUTING.md`、`LICENSE*` | 文档 | 项目说明、贡献流程、各模块许可证 |

3. **写一段话**：用 100~150 字回答「本仓库解决了哪三个具体问题」。

**预期结果（参考）**：本仓库解决的三个问题可表述为——
1. **「FPGA 该放什么逻辑才能对接 ADI 芯片」**：提供 `library/` 里大量可复用 IP（DMA、数据转换接口、FIFO 等）。
2. **「如何快速跑通某块评估板」**：提供 `projects/` 里针对具体板卡的完整参考设计，`make` 即可构建。
3. **「如何在多家厂商 FPGA 上综合」**：用 Tcl + Make 抽象出统一构建流水线，适配 AMD Xilinx / Intel / Lattice 三家工具链。

## 6. 本讲小结

- **ADI HDL 是一套 FPGA 参考设计素材库**，交付物是 Verilog/VHDL 源码 + Tcl 脚本，最终产物是比特流，不是可执行程序（[README.md:30-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L30-L36)）。
- 仓库 **二分为 `library/`（可复用 IP 积木）与 `projects/`（拼好的整板参考设计）**，后者把前者按评估板 + 载板组合起来。
- **构建入口是命令行 `make`**，例如 `cd projects/fmcomms2/zcu102 && make`，而非 IDE 菜单（[README.md:76-94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L76-L94)）。
- 仓库 **面向多家 FPGA 工具链**，README 主推 AMD Xilinx 与 Intel，`scripts/adi_env.tcl` 还跟踪了 Lattice 版本（[scripts/adi_env.tcl:19-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L19-L25)）。
- **hdl 不是孤岛**：它只产出硬件，软件驱动分别由 `no-OS`（裸机）和 `linux` 仓库提供，三者通过 AXI 寄存器映射对接（[README.md:148-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L148-L152)）。
- **稳定性建议**：生产用最新 release 分支，尝鲜可用 `main`，两者对应不同工具版本（[README.md:122-132](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L122-L132)）。

## 7. 下一步学习建议

本讲只建立了「全局印象」，还没碰任何具体构建与设计细节。建议按以下顺序继续：

1. **`u1-l2` 仓库与目录结构导览**：系统走读 `library/` 与 `projects/` 的内部命名规律，看清每个子目录里都有什么文件。
2. **`u1-l3` 构建环境与工具链版本**：深入 `scripts/adi_env.tcl`，搞清楚各工具链的精确版本与 `ADI_IGNORE_VERSION_CHECK` 等环境变量。
3. **`u1-l4` 构建第一个工程**：以 `fmcomms2/zcu102` 为例，完整跑一遍从 `make` 到比特流的入口流程。

如果你想提前感受「一个工程长什么样」，可以先打开 [projects/fmcomms2/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl) 瞄一眼——它就是 `make` 最终会喂给 Vivado 的入口脚本，但具体每行含义留到 `u1-l4` 再讲。
