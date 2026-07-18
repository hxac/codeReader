# 项目概览：PSI FPGA 集合仓库是什么

## 1. 本讲目标

本讲是整本学习手册的第一讲，不要求你写过一行 VHDL，也不要求你懂 TCL。读完本讲你应该能够：

- 用自己的话说清楚 `psi_fpga_all` 这个仓库**是什么**、**为什么这样设计**。
- 理解「collection-repo（集合仓库）」这个概念，以及它为什么不直接存放库源码。
- 认识仓库的维护者、作者，并知道去哪里查看版本变更记录（Changelog）。
- 建立一个关键直觉：**目录结构本身就是接口**——各库之间用相对路径互相引用，所以目录不能乱动。
- 大致了解仓库「约每 3 个月整体更新一次」的发布节奏。

本讲只读一个文件：`README.md`。它虽然只有 45 行，却把整个仓库的定位、用法和维护方式都讲清楚了，是理解后续所有讲义的基石。

## 2. 前置知识

本讲几乎不需要任何前置知识。但为了看懂后面的内容，先建立下面几个通俗概念即可：

- **FPGA（现场可编程门阵列）**：一种可以用代码「重新接线」的芯片。开发 FPGA 时，工程师通常用硬件描述语言（如 VHDL）写出电路逻辑，再把它「综合」到芯片里。
- **库（library）**：就像软件工程里有「工具库」一样，FPGA 开发也会沉淀出很多可复用的电路模块（比如固定点数运算、常用总线接口、测试平台框架等），它们被组织成一个个独立的仓库。
- **git 仓库**：存放代码及其历史版本的地方。本项目就托管在 GitHub 上。
- **git submodule（子模块）**：可以粗略理解为「仓库里的仓库」。主仓库不把子仓库的代码直接复制进来，而是记录一个指针，指向子仓库在某一个具体版本（commit）。这样主仓库很「轻」，又能把多个独立项目按需组合起来。子模块的细节是下一讲（u1-l2）的内容，本讲只需要这个直觉。

> 一个形象的比喻：把 `psi_fpga_all` 想象成一本「精选合集」。合集本身不写文章，它只是把多篇来自不同作者、各自独立发表的文章，按固定的章节顺序装订到一起，并注明每篇文章用的是哪个版本。读者买一本，就能拿到一套互相配套的文章。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是整个仓库的「说明书」。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md) | 仓库的入口文档，共 45 行。包含维护者、作者、Changelog 指引、**仓库定位（Purpose）**、库分类清单和克隆方式。 |

为了交叉印证，本讲还会少量引用另外两个文件（它们是后续讲义的主线，这里只用来佐证事实，不展开）：

| 文件 | 作用 |
| --- | --- |
| [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) | 列出全部子模块的路径与来源 URL，是「合集装订目录」的机器可读版本。 |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md) | 按发布版本记录每个子模块固定到了哪个 tag。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 仓库定位与 Purpose 章节**：回答「这是什么」。
- **4.2 Maintainer / Authors / Changelog 指引**：回答「谁在维护、去哪看变更」。
- **4.3 更新策略（约每 3 个月）**：回答「多久更新、能不能随时拿到最新」。

### 4.1 仓库定位与 Purpose 章节

#### 4.1.1 概念说明

打开 `README.md`，最重要的就是 `## Purpose of the Repository` 这一节。它用两句话把仓库的定位说死了：

> This repository is a **collection-repo**. It contains all FPGA related libraries as submodules in exactly the directory structure required. The directory structure is important because different libraries reference to each other using relative paths.

翻译并拆解：

1. **这是一个 collection-repo（集合仓库）**。它本身几乎不写代码，它的「产品」是一套**按固定目录结构组装好的 FPGA 库合集**。
2. **目录结构很重要**。因为不同的库之间会「用相对路径互相引用」。比如 A 库的代码里可能写着 `../../VHDL/psi_common/...` 去调用 B 库。这意味着目录一旦改动，引用就会断裂。
3. 由此得出本讲最核心的直觉：**目录结构即接口**。在本仓库里，`VHDL/psi_common`、`TCL/PsiSim` 这些路径不只是文件夹，它们是库与库之间约定好的「调用地址」。

为什么 PSAI（Paul Scherrer Institute，瑞士保罗谢尔研究所）要做成集合仓库，而不是把所有源码直接放进一个仓库？

- **独立演进**：每个库（如 `psi_common`、`psi_fix`）都有自己的版本号、发布节奏和 issue 跟踪。把它们拆成独立仓库，能各自独立开发、独立发版。
- **解耦与复用**：有的用户只需要其中一两个库，可以直接去对应仓库克隆，不必拖走整套合集。
- **组合保证**：集合仓库的价值在于「把一套经过验证、互相兼容的版本组合在一起」。通过 submodule 指针固定每个库的具体 commit，就保证了你拿到的是一套配套的组合，而不是各自最新（可能互相不兼容）的状态。

#### 4.1.2 核心流程

从「用户拿到一套配套的 FPGA 库」这个目标反推，集合仓库的工作方式可以画成下面这样：

```text
┌─────────────────────────────────────────────┐
│         psi_fpga_all（集合仓库/主仓库）        │
│                                             │
│  README.md   Changelog.md   .gitmodules     │
│                                             │
│   VHDL/    TCL/    Python/    VivadoIp/     │  ← 固定目录结构（接口）
│     │        │       │           │          │
│     ▼        ▼       ▼           ▼          │
│  [子模块指针：每个指向某库的一个具体 commit]    │  ← 版本固定
└─────────────────────────────────────────────┘
              │ 互相之间用相对路径引用
              ▼
   例如 psi_fix 引用 ../../VHDL/psi_common/...
```

要点：

- 主仓库里**直接可读的「文档型」文件**只有 `README.md`、`Changelog.md`、`.gitmodules`（外加 `scripts/` 下几个驱动脚本，那是后续讲义的内容）。
- `VHDL/`、`TCL/`、`Python/`、`VivadoIp/` 这些目录里放的是 submodule。如果克隆时没有带上子模块，这些目录会是**空的**（这也是为什么下一讲要专门讲 `--recurse-submodules`）。

#### 4.1.3 源码精读

仓库定位在 README 的 Purpose 章节，只有短短几行，但信息密度很高：

[README.md:L13-L18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L13-L18) —— 定义本仓库是 collection-repo，强调目录结构因「相对路径互相引用」而重要，并给出「可以单独更新子模块 / 也可手动按目录结构检出」的两种灵活用法。

逐句解读其中的关键句：

- `This repository is a collection-repo.` —— 一句话定位：集合仓库。
- `... in exactly the directory structure required.` —— 目录结构是「被要求的（required）」，不是随便放的。这正是「目录结构即接口」的原文依据。
- `... different libraries reference to each other using relative paths.` —— 库与库之间用**相对路径**互引，所以结构不能乱动。
- `Alternatively only the repositories used can be checked out manually ...` —— 给了用户第二条路：如果你不需要整套合集，也可以只把用到的几个库，手动克隆到下面描述的目录结构里。这说明「目录结构」是真正重要的约定，至于你是用 submodule 自动装订还是手动摆放，只要结构对就行。

紧随其后的库分类清单（TCL / VHDL / Python）就在同一节里：

[README.md:L20-L32](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L20-L32) —— README 在 Purpose 章节里显式列出了三类库及其包含的子库。

其中 VHDL 类的 `en_cl_fix` 还有一句特别说明：

[README.md:L26-L27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L26-L27) —— 标注 `en_cl_fix` 是 Enclustra GmbH 提供的库的一个 fork（派生版本），并给出原始仓库地址。这说明合集里既有自研库，也收录了第三方库的定制版本。

> 说明：README 的 Purpose 章节在正文中**只显式列出了 TCL、VHDL、Python 三类**。但仓库实际还包含第四类 **VivadoIP**（封装好的 Vivado IP 核），这一点可以从 [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) 中的 `VivadoIp/vivadoIP_*` 条目和 [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md) 的 `VivadoIP` 分组得到印证。也就是说，四大类是事实，只是 README 的这段清单没有把 VivadoIP 逐条写出来。本讲的实践任务会把四类都列齐。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用自己的话把仓库定位讲清楚。

1. **实践目标**：能够脱离文档，向一个没接触过本项目的人解释 `psi_fpga_all` 是什么。
2. **操作步骤**：
   - 打开 [README.md 的 Purpose 章节](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L13-L18)。
   - 用自己的话写下 3 句话，分别回答：① 本仓库是什么；② 为什么不直接放源码（而要用 submodule 聚合）；③ 它最重要的设计约束是什么。
3. **需要观察的现象**：你会发现自己写第二句时，必须提到「各库独立演进 / 版本固定 / 相对路径互引」中的至少一个理由。
4. **预期结果**：示例回答（仅供参考，鼓励你用自己的措辞）：
   - ① `psi_fpga_all` 是一个集合仓库，它把 PSI 的全部 FPGA 相关库按固定目录结构聚合在一起。
   - ② 它不直接放源码，是因为每个库都要独立开发、独立发版；用 submodule 既能让各库解耦，又能固定一套互相兼容的版本组合。
   - ③ 最重要的约束是「目录结构即接口」——库与库之间用相对路径互引，目录一旦改动引用就会断裂。
5. 不确定的地方（例如某个库的具体职责）可以标注「待确认」，后续讲义会补全。

#### 4.1.5 小练习与答案

**练习 1**：README 说「目录结构很重要，因为不同的库用相对路径互相引用」。请举一个假设的例子，说明如果有人把 `VHDL/psi_common` 改名成 `VHDL/common`，会出现什么问题。

> **参考答案**：如果有库（例如 `psi_fix`）的代码或脚本里写了形如 `../../VHDL/psi_common/xxx.vhd` 的相对路径，那么改名后这条路径就指向了不存在的位置，编译/仿真时会找不到文件而报错。这就是「目录结构即接口」的含义——路径本身就是被依赖的约定。

**练习 2**：README 的 Purpose 章节给了「也可以手动把用到的库检出到目录结构里」这种用法。结合「目录结构即接口」，说说这种用法为什么可行。

> **参考答案**：因为各库之间依赖的是**相对路径形成的目录结构**，而不是「必须由 submodule 装订」这件事。只要你手动摆放的目录结构与约定一致（例如把 `psi_common` 放到 `VHDL/psi_common`），相对路径就能正常解析，库之间就能互相引用。submodule 只是自动完成这套摆放的工具。

### 4.2 Maintainer / Authors / Changelog 指引

#### 4.2.1 概念说明

一个开源/内部项目，最先要搞清楚的就是「出了问题找谁」「这个库是谁做的」「版本变了去哪看」。README 开头的 `General Information` 就是干这件事的。它包含三块：

- **Maintainer（维护者）**：当前负责这个仓库的人，遇到问题可以先联系他。
- **Authors（作者）**：项目的核心贡献者。
- **Changelog（变更日志）**：记录每次发布改了什么。README 不直接写变更，而是指引你去 `Changelog.md`。

#### 4.2.2 核心流程

当你要弄清「这个仓库谁负责 / 历史版本如何」时，遵循这条简单的查找链：

```text
想找负责人   →  看 README 的 ## Maintainer
想了解作者   →  看 README 的 ## Authors
想看版本变更 →  README 的 ## Changelog 指向 Changelog.md
                →  打开 Changelog.md，按发布版本（如 2021.1）查看
```

#### 4.2.3 源码精读

[README.md:L3-L11](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L3-L11) —— 给出维护者、作者，并用一个链接把读者引导到 Changelog。

要点：

- `## Maintainer` 指明当前维护者是 **Benoit Stef**（附 PSI 邮箱）。
- `## Authors` 列出两位核心作者：**Oliver Bründler** 与 **Benoit Stef**。
- `## Changelog` 只有一句 `See [Changelog](Changelog.md)`，它是一个**指引（pointer）**，真正的变更记录在 [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md)。

打开 Changelog.md 后，你会看到它按发布版本倒序排列，例如最新的 `## 2021.1 (23.08.2021)`，每个版本下再按 TCL / VHDL / Python / VivadoIP 分组，列出该版本固定到的子模块及其 tag：

[Changelog.md:L1-L9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L1-L9) —— 2021.1 发布记录示例：可以看到 `PsiIpPackage 2.4.0`、`en_cl_fix 1.1.8`、`psi_common 2.17.0` 等子模块被固定到了具体版本号。

> 这个「README 只放指引、详情放 Changelog.md」的写法是个好习惯：README 保持简洁稳定，频繁变动的版本信息集中到单独文件，维护起来更清晰。Changelog 的版本固定机制会在 u3-l1 详细讲解。

#### 4.2.4 代码实践

1. **实践目标**：建立「找维护者 / 找作者 / 找变更」的肌肉记忆。
2. **操作步骤**：
   - 在 [README.md:L3-L11](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L3-L11) 找到维护者和作者。
   - 点击 `See [Changelog](Changelog.md)` 跳到 [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md)，找到最新一次发布（最上方的版本号）。
3. **需要观察的现象**：Changelog 里每个子模块名后面都跟着一个数字（如 `2.17.0`），这就是它被固定的 tag。
4. **预期结果**：你能说出当前维护者是 Benoit Stef，最新发布版本是 **2021.1（2021-08-23）**，并能在该版本下找到例如 `psi_common 2.17.0` 这样的「子模块 + 版本」记录。
5. 本实践为阅读型实践，不需要运行任何命令，**待本地验证**的部分仅指：如果你要在本地仓库里点击链接跳转，需先克隆仓库（见 u1-l2）。

#### 4.2.5 小练习与答案

**练习 1**：README 的 `## Changelog` 一节为什么只有一句话、而不直接把变更写进 README？

> **参考答案**：因为变更记录会随每次发布不断增长，写进 README 会让 README 越来越长、越来越不稳定。把详情拆到独立的 `Changelog.md`，README 保持简洁并只放一个指引链接，是更易维护的做法。

**练习 2**：在 Changelog.md 的 2021.1 版本中，`psi_common` 固定到了哪个版本号？

> **参考答案**：`2.17.0`（见 [Changelog.md:L6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L6)）。

### 4.3 更新策略（约每 3 个月）

#### 4.3.1 概念说明

很多初学者会以为「集合仓库 = 永远拿到每个子库的最新代码」。**这是误解**。README 在 Purpose 章节里明确说明了更新策略，理解它能避免你踩坑。

关键点有两个：

1. **整体更新节奏**：仓库「约每 3 个月」整体更新一次。每次更新时，维护者会把各子模块的指针推进到一个经过验证的新版本，并写进 Changelog。
2. **两次更新之间不保证最新**：也就是说，在两次整体更新之间，仓库里的子模块**可能不是各自的最新状态**。这是为了保证「配套兼容」，而不是追新。
3. **可以单独更新子模块**：如果你确实需要某个子库的最新版，README 明确说可以单独更新那一个子模块（`You can update submodules individually if required.`）。

#### 4.3.2 核心流程

用一张时间轴理解「整体快照 + 个别更新」的策略：

```text
时间 ─────────────────────────────────────────────────►
     │           │           │           │
   2021.1     (约3月)      (约3月)     下一次release
     │
     ▼
 集合仓库整体快照：
   把每个子模块固定到「一套互相兼容的版本」
   写进 Changelog

两次 release 之间：
   子模块可能落后于各自最新版（不追新，求稳定）
   但用户可以单独把某个子模块更新到最新
```

这套策略的本质是：**集合仓库优先保证「整套可用、互相兼容」，而不是「每个库都最新」**。这与 4.1 讲的「版本固定」一脉相承。

#### 4.3.3 源码精读

更新策略写在 Purpose 章节的第二段：

[README.md:L16-L18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L16-L18) —— 说明「约每 3 个月更新一次、中间可能不是各子模块最新、可单独更新子模块、也可手动按目录结构检出」。

逐句拆解：

- `The repository will be updated regularly (roughly every 3 months)` —— 整体节奏：约每 3 个月一次。
- `but it may not always contain the vey-newest state of all submodules in between the updates.` —— 中间不保证最新（注意原文 `vey-newest` 是 `very-newest` 的笔误，语义不变）。这是最容易让初学者误解的一点。
- `You can update submodules individually if required.` —— 给了「逃生口」：需要最新版时，单独更新某个子模块即可。

把这条策略和 Changelog 对照看会更清楚：Changelog 里每个 release 只列出**有变化**的子模块（及其新版本），没有列出的子模块说明这次发布没有改动它。例如 2021.1 只列了 `PsiIpPackage`、`en_cl_fix`、`psi_common`、`psi_fix`、`vivadoIP_mem_test` 几个，意味着这次发布只推进了这几个库：

[Changelog.md:L1-L10](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L1-L10) —— 2021.1 发布只列出了发生变化的少数子模块，其余子模块沿用上一次发布的版本。

#### 4.3.4 代码实践

1. **实践目标**：体会「整体快照 + 个别更新」的版本管理思路。
2. **操作步骤**：
   - 打开 [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md)。
   - 对比相邻两次发布 `2021.1` 与 `2020.2`，看哪些子模块在 2021.1 里被列出（即被更新），哪些没有出现（即沿用旧版）。
3. **需要观察的现象**：2020.2 列了非常多子模块（TCL/VHDL/Python/VivadoIP 各一大片），而 2021.1 只列了 5 个。这说明 2021.1 是一次「小更新」——只推进了少数几个库，其余保持不变。
4. **预期结果**：你能用一句话总结——「集合仓库每次发布只更新部分子模块，未列出的子模块沿用上次版本，这就是『约每 3 个月整体更新、中间不保证最新』的实际体现」。
5. 关于「单独更新某个子模块」的具体命令（如 `git submodule update`、`git -C <path> checkout <tag>`），属于下一讲 u1-l2 的内容，这里先建立概念，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：某同事抱怨「这个仓库里的 `psi_common` 不是 GitHub 上的最新版，是不是仓库坏了？」请结合 4.3 的内容反驳他。

> **参考答案**：没有坏。README 明确说仓库「约每 3 个月整体更新一次，两次更新之间可能不包含各子模块的最新状态」，优先保证整套兼容而非追新。如果确实需要最新版，可以单独更新该子模块。这正是集合仓库「整体快照 + 个别更新」的设计。

**练习 2**：对比 Changelog 的 2021.1 和 2020.2，`psi_common` 分别固定在哪个版本？

> **参考答案**：2021.1 是 `2.17.0`（[Changelog.md:L6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L6)），2020.2 是 `2.13.0`（[Changelog.md:L17](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L17)）。说明在 2020.2 到 2021.1 之间，`psi_common` 从 2.13.0 升级到了 2.17.0。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读懂说明书」的小任务。

**任务背景**：假设你要向团队介绍 `psi_fpga_all`，需要一份一页纸的「项目速览」。

**操作步骤**：

1. 读 [README.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md) 全文（只有 45 行）。
2. 产出一份速览，必须包含以下五块：
   - **一句话定位**：本仓库是什么（用「collection-repo / 集合仓库」来表述）。
   - **为什么不直接放源码**：结合「各库独立演进 + 版本固定 + 相对路径互引」说明。
   - **维护者与作者**：从 README 的 General Information 摘出。
   - **四大类库清单**：列出 TCL / VHDL / Python / VivadoIP 四类各自包含哪些库。
     - TCL、VHDL、Python 三类直接照 README 的 Purpose 清单（[README.md:L20-L32](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L20-L32)）即可。
     - VivadoIP 类请到 [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) 中所有 `path = VivadoIp/...` 的条目里收集（共 11 个）。
   - **更新策略**：用一句话说明「约每 3 个月整体更新、中间不保证最新、可单独更新子模块」。
3. **参考答案（VivadoIP 清单部分）**：从 `.gitmodules` 可整理出 VivadoIP 类包含：`vivadoIP_data_rec`、`vivadoIP_clock_measure`、`vivadoIP_spi_simple`、`vivadoIP_axis_data_gen`、`vivadoIP_mem_test`、`vivadoIP_psi_ms_daq`、`vivadoIP_i2c_devreg`、`vivadoIP_power_sink`、`vivadoIP_fpga_base`、`vivadoIP_sync_edge_det`、`vivadoIP_axi_mm_reader`，共 11 个。其余三类按 README：
   - **TCL**：PsiSim
   - **VHDL**：psi_common、psi_tb、psi_fix、en_cl_fix（en_clustra 的 fork）
   - **Python**：PsiPyUtils、IseScripting、VivadoScripting、TbGenerator
4. **预期结果**：你得到一份结构清晰、全部基于真实文件、不编造的一页纸速览；其中任何一条都能在 README / .gitmodules / Changelog 里找到出处。

> 提示：如果你发现 README 没有逐条列出 VivadoIP，不要凭空编造库的数量或名字——以 `.gitmodules` 为准。这也是本讲反复强调的原则：**只基于真实源码**。

## 6. 本讲小结

- `psi_fpga_all` 是一个 **collection-repo（集合仓库）**，本身几乎不含代码，而是用 git submodule 把 PSI 的全部 FPGA 库按固定目录结构聚合起来。
- **目录结构即接口**：各库之间用相对路径互相引用，所以目录不能随意改动（README Purpose 章节的原文依据）。
- 仓库由 **Benoit Stef** 维护，作者是 **Oliver Bründler** 与 **Benoit Stef**；版本变更记录在独立的 `Changelog.md` 里，README 只放指引。
- 更新策略是**约每 3 个月整体更新一次**，两次更新之间不保证各子模块最新，但**可以单独更新某个子模块**。
- Changelog 按 release 分组、每个子模块后跟具体 tag，体现了「整体快照 + 个别更新」的版本管理思路。
- 仓库共有 **四大类库**：TCL、VHDL、Python、VivadoIP；前三类在 README 中列出，VivadoIP 类需从 `.gitmodules` / Changelog 印证。

## 7. 下一步学习建议

本讲只建立了「它是什么」的认知，还没有真正把仓库「拿在手里」。建议下一步：

- **学习 u1-l2《git submodule 机制与克隆方式》**：搞清楚 `.gitmodules` 里 `path` 和相对 `url` 的含义、为什么必须用 `git clone --recurse-submodules`、以及 SSH 与 HTTPS 两种克隆方式的区别。学完你就能在本地得到一个**目录非空**的完整仓库。
- **随后学习 u1-l3《目录结构与四大类库》**：把四大类库的职责和代表模块梳理成一张全景表，为进入第二单元（脚本驱动的仿真与 IP 打包）做准备。
- 在阅读后续讲义前，建议先把本讲的「综合实践」做完——它会逼着你把 README、`.gitmodules`、Changelog 三个文件对照着读一遍，这是理解整个项目最快的方式。
