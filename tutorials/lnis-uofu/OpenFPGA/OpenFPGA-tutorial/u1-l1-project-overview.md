# OpenFPGA 是什么：FPGA IP 生成与完整 EDA 流程

## 1. 本讲目标

本讲是整本《OpenFPGA 学习手册》的第一篇，面向完全没接触过这个项目的读者。学完本讲，你应当能够：

- 用一句话说清 **OpenFPGA** 这个项目是做什么的、解决了什么问题。
- 写出 OpenFPGA 的**输入**是什么、**输出**是什么。
- 画出一条从 **Verilog 到 Bitstream（比特流）** 的粗粒度流程框图，标注综合、布局布线、fabric 构建、比特流/网表生成等主要阶段。
- 说出 OpenFPGA 与 **VPR**、**Yosys** 等外部工具在整条流程中的分工。
- 知道在仓库里去哪里看版本号、许可证和引用文献。

本讲不会让你写任何代码，重点是建立「全局地图」。有了这张地图，后续每一篇讲义你都能知道自己在流程的哪一环。

## 2. 前置知识

在读本讲前，建议你大致了解以下几个名词。不理解也没关系，本讲会用通俗语言再解释一遍。

- **FPGA（Field-Programmable Gate Array，现场可编程门阵列）**：一种芯片，出厂后还能由用户「重新连线」来实现不同的电路功能。这种「重新连线」不是物理上的线，而是通过给芯片里大量可编程开关下命令来完成的。
- **可编程 fabric（架构 / 网格）**：FPGA 芯片内部那片由逻辑块、连线开关、IO 组成的、可以被编程的硬件资源。
- **比特流（Bitstream）**：一串 0/1，用来给 FPGA 的可编程开关「下达命令」，决定 fabric 实现什么电路。把比特流加载进 FPGA，就叫「烧录 / 配置」。
- **EDA（Electronic Design Automation，电子设计自动化）**：用来设计芯片和电路的软件工具集合。综合器、布局布线器、仿真器都属于 EDA 工具。
- **IP（Intellectual Property）**：在芯片语境下，指一段可复用的设计模块。「FPGA IP 生成」指自动生成一整套 FPGA 的设计文件。
- **综合（Synthesis）/ 布局布线（P&R）**：把高级描述（如 Verilog 代码）翻译成具体硬件门，并决定这些门放在芯片哪个位置、用什么线连起来的两个步骤。

一个关键直觉：**普通的 EDA 流程是「拿别人的 FPGA 来用」，而 OpenFPGA 的流程是「造一个新的 FPGA 并顺便给出能用它的软件」**。这个区别贯穿全本手册。

## 3. 本讲源码地图

本讲引用的源码文件很少，都在仓库顶层，属于「项目门面」级别的文档：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `README.md` | 项目的「门牌」，介绍定位、编译、文档、许可证、引用 | 用来确认 OpenFPGA 是什么、覆盖哪些 EDA 能力 |
| `VERSION.md` | 只有一行的版本号文件 | 用来了解当前版本与版本管理约定 |
| `docs/source/overview/figures/OpenFPGA_logo.png` | README 顶部引用的 Logo 图片 | 仓库根目录 README 的视觉标识 |

此外，本讲在解释 EDA 流程时会引用两篇项目官方文档（已核对存在），供你课后延伸阅读：

- `docs/source/overview/motivation.rst`：解释「为什么要有 OpenFPGA」以及四大工具组成。
- `docs/source/overview/tech_highlights.rst`：列出支持的电路类型与 FPGA 架构特性。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **项目说明 README**：OpenFPGA 的定位、价值与覆盖的 EDA 流程。
2. **版本信息 VERSION.md**：版本号的来源与管理。

### 4.1 项目说明 README

#### 4.1.1 概念说明

`README.md` 是任何开源项目的「第一印象」。OpenFPGA 的 README 第一行就把项目身份亮了出来——它带一张 Logo 图片，标题是 **Getting Started with OpenFPGA**：

[README.md:1](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L1)

接着它用一句话给出最关键的定位。你需要记住的核心定义是：

> The award-winning OpenFPGA framework is the **first open-source FPGA IP generator with silicon proofs** supporting highly-customizable FPGA architectures.

这句话信息量很大，拆开看有三个要点：

- **开源（open-source）**：代码全部公开，MIT 许可证（子模块除外）。
- **FPGA IP generator（FPGA IP 生成器）**：它的产物不是「在某个 FPGA 上跑的设计」，而是「一整套 FPGA 芯片的设计文件 + 配套 CAD 工具」。换句话说，它帮你**造 FPGA**，而不仅仅是**用 FPGA**。
- **with silicon proofs（有流片验证 / 硅验证）**：它不是纸上谈兵，已经真实造出过芯片验证过——这是它区别于很多「只能仿真」的学术工具的地方。

完整的 Introduction 段落见这里：

[README.md:12](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L12)

#### 4.1.2 核心流程

OpenFPGA 解决的核心问题，可以用一个对比来理解（来自官方 motivation 文档）：

- **传统方式**：要造一个定制 FPGA，需要一整组资深工程师花一年以上手工画版图、再额外开发配套软件。
- **OpenFPGA 方式**：用户写一份 **XML 架构描述文件**，OpenFPGA 自动生成描述完整 FPGA fabric 的 **Verilog 网表**，配合现代半定制后端工具，一天内就能拿到可流片的版图；同时它还能用**同一份 XML** 自动生成比特流工具，省去为每个新 FPGA 重新写 CAD 工具的重复劳动。

参见 motivation 文档对这一点的原文描述：

[docs/source/overview/motivation.rst:20-21](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/docs/source/overview/motivation.rst#L20-L21)

在功能划分上，OpenFPGA 由四个部分组成。这是本讲最重要的「全局地图」，后续几乎每篇讲义都对应其中一块：

[docs/source/overview/motivation.rst:25](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/docs/source/overview/motivation.rst#L25)

| 组成 | 作用 | 对应输出 |
| --- | --- | --- |
| **FPGA-Verilog** | 生成描述完整 FPGA fabric 的 Verilog 网表 | fabric Verilog 网表 + testbench |
| **FPGA-SDC** | 生成时序约束文件 | PnR 约束、sign-off 时序分析约束 |
| **FPGA-Bitstream** | 生成配置比特流（原生 CAD 工具） | fabric 比特流 |
| **FPGA-SPICE** | 生成晶体管级 SPICE 网表 | SPICE 子电路 / testbench |

把上面这些拼起来，就得到 OpenFPGA 端到端的粗粒度流程。下面用伪流程图描述（方括号是输入，圆括号是阶段，箭头表示数据流向）：

```text
输入:
  [用户 Verilog 设计]      ← 你想让 FPGA 实现的电路，例如 and2.v
  [VPR 架构 XML]          ← 描述 FPGA 器件结构（逻辑块、布线）
  [openfpga_arch.xml]     ← OpenFPGA 补充的电路级物理实现描述

        │  1) 综合 Synthesis（由 Yosys 完成）
        ▼     把 Verilog 翻译成基本门 / LUT 的网表
   (mapped netlist)
        │  2) 打包 + 布局 + 布线 Packing/Placement/Routing（由 VPR 完成）
        ▼     决定每个逻辑块放在芯片哪里、用什么线连起来
   (placed & routed design)
        │  3) fabric 构建 build_fabric（OpenFPGA 核心）
        ▼     根据架构描述生成完整 FPGA fabric 的模块图
   (FPGA fabric ModuleManager + Verilog 网表)  ← FPGA-Verilog
        │  4) 比特流 / 约束生成
        ▼
输出:
   (configuration bitstream)   ← FPGA-Bitstream
   (SDC constraints)           ← FPGA-SDC
   (SPICE netlists)            ← FPGA-SPICE（可选）
   (Verilog testbenches)       ← 用来验证 fabric 正确性
```

一句话总结输入输出：

- **输入**：用户电路 Verilog + 两套 XML 架构描述（VPR 架构 + `openfpga_arch.xml`）。
- **输出**：一整套 FPGA fabric 的 Verilog 网表、配置比特流、时序约束（SDC）、可选的 SPICE 网表和自检 testbench。

注意流程中 OpenFPGA 与外部工具的分工：**综合用 Yosys，布局布线用 VPR，OpenFPGA 自己负责 fabric 构建、网表/比特流/约束/SPICE 生成**。VPR 和 Yosys 都是作为子模块（submodule）集成进来的，README 的许可证段也专门提到它们：

[README.md:46](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L46)

> All the codes are under MIT license, with the exception of submodules, e.g., VTR, Yosys and Yosys-plugin, which are distributed under its own (permissive) terms.

#### 4.1.3 源码精读

README 顶部除了 Logo，还挂着一排徽章（CI 状态、文档状态、Binder 等），其中第 8 行明确指出「版本号见 VERSION.md」，这就把本讲的第二个最小模块串起来了：

[README.md:8](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L8)

README 末尾给出了引用 OpenFPGA 时应使用的论文。这非常重要——OpenFPGA 是一个学术 + 工程项目，论文是它的「权威说明书」，里面详细讲了它解决了什么问题、怎么解决的：

[README.md:50-52](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L50-L52)

论文标题直译就是「OpenFPGA：一个用于敏捷原型化可定制 FPGA 的开源框架」，这正是本项目最准确的定位。

#### 4.1.4 代码实践

> **本实践是源码阅读型 + 文档阅读型，不需要编译运行任何东西。**

1. **实践目标**：读完 README 与论文摘要后，用自己的话把 OpenFPGA 的「问题—输入—输出」讲清楚，并手画一张流程图。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，重点读 `Introduction`（第 10–22 行）和 `How to Cite`（第 48–60 行）两段。
   - 打开官方文档 `docs/source/overview/motivation.rst`，读第 10–26 行（为什么需要 OpenFPGA、四个工具组成）。
   - 找到 README 第 52 行给出的论文题目和 DOI，在网上检索这篇 IEEE Micro 论文的摘要（题目：*OpenFPGA: An Open-Source Framework for Agile Prototyping Customizable FPGAs*，DOI: `10.1109/MM.2020.2995854`），读它的 Abstract。
3. **需要观察的现象**：你会发现 README 里说的「first open-source FPGA IP generator with silicon proofs」、motivation 文档里说的「XML-to-Prototype」「避免为每个新 FPGA 重写 CAD 工具」，和论文摘要里的表述是高度一致的——三处互相印证，说明你理解到位了。
4. **预期结果**：你能写出类似下面这样的两段话——
   - **解决问题**：传统造一个定制 FPGA 要一年以上、要手画版图和手写配套软件；OpenFPGA 用一份 XML 架构描述自动生成完整 FPGA fabric 的 Verilog 网表，并复用同一份描述自动给出比特流工具，把周期压到约一天。
   - **输入 / 输出**：输入是「用户 Verilog 设计 + VPR 架构 XML + openfpga_arch.xml」；输出是「fabric Verilog 网表、配置比特流、SDC 时序约束、可选 SPICE 网表与自检 testbench」。同时画出本讲 4.1.2 中的那条流程框图。
5. 如果你在检索论文摘要时遇到访问限制，明确标注「待本地验证：论文摘要需自行检索确认」，但流程图本身不依赖论文，可以直接画。

#### 4.1.5 小练习与答案

**练习 1**：README 说 OpenFPGA 是「FPGA IP generator」，它和「在 FPGA 上跑一个设计」有什么本质区别？

**参考答案**：在 FPGA 上跑设计，是拿别人已经造好的 FPGA、往里烧一串比特流，让它实现你的电路；而「FPGA IP generator」是反过来——它**生成一整套 FPGA 芯片的设计文件（Verilog 网表等）以及配套的 CAD 工具（比特流生成器）**。前者的产物是「一个应用」，后者的产物是「一款芯片 + 它的软件」。

**练习 2**：OpenFPGA 由哪四部分组成？各自输出什么？

**参考答案**：四部分是 **FPGA-Verilog**（输出 fabric Verilog 网表与 testbench）、**FPGA-SDC**（输出 PnR 与 sign-off 的时序约束）、**FPGA-Bitstream**（输出配置比特流）、**FPGA-SPICE**（输出晶体管级 SPICE 网表）。

**练习 3**：在「综合 → 布局布线 → fabric 构建 → 比特流生成」这条链里，哪几步是 OpenFPGA 自己做的？哪几步交给外部工具？

**参考答案**：综合交给 **Yosys**，布局布线（打包/放置/布线）交给 **VPR**；这两者作为子模块集成进来。**fabric 构建、Verilog/SDC/SPICE 网表生成、比特流生成** 是 OpenFPGA 自己做的。

### 4.2 版本信息 VERSION.md

#### 4.2.1 概念说明

`VERSION.md` 是仓库里最短的文件之一，但它很重要：它是整个项目的「版本号唯一来源」。README 第 8 行写的就是 `Version: see [VERSION.md](VERSION.md)`，意味着版本号不写在 README 里，而是集中维护在这个文件里。这种「单一数据源（single source of truth）」的做法，能避免多个地方写版本号、改一处忘另一处的混乱。

#### 4.2.2 核心流程

版本号在 OpenFPGA 里遵循一种 **major.minor[.patch] + build** 的风格。当前文件内容只有一行：

```text
1.2.4307
```

把它拆开看：

- `1` 是主版本号（major）。
- `2` 是次版本号（minor）。
- `4307` 这里通常被项目当作持续累计的构建号 / 补丁号（patch / build counter），随提交不断递增。

仓库的提交历史里能看到大量 `Updated Patch Count` 这类提交，它们的作用就是自动把这个构建号往上加，用来追踪「当前代码处在第几次构建」。对学习者来说，你只要记住：**告诉别人「我用的是哪个版本的 OpenFPGA」时，报这一行的数字即可**。

> 注：OpenFPGA 的发布版本号风格会随项目演进调整，上面 `1.2.4307` 的具体位数解读以仓库实际版本策略为准。如果你要确认它当前的语义，可以对照 `git log` 里 `Updated Patch Count` 的提交来理解。若无法确认具体语义，请标注「待确认」。

#### 4.2.3 源码精读

整个 `VERSION.md` 文件只有一行，内容就是版本号字符串：

[VERSION.md:1](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/VERSION.md#L1)

它被 README 通过相对链接引用，构成「README 指向 VERSION.md」的单向依赖：

[README.md:8](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/README.md#L8)

#### 4.2.4 代码实践

> **本实践是命令行观察型，安全且不修改任何源码。**

1. **实践目标**：确认你本地仓库的版本号，并理解它是如何随「Patch Count」提交变化的。
2. **操作步骤**：
   - 在仓库根目录查看版本文件：`cat VERSION.md`（应当输出 `1.2.4307`）。
   - 查看最近的提交：`git log --oneline -8`，留意是否有 `Updated Patch Count` 这类提交。
   - （可选）找出最近一次更新版本号的提交：`git log -- VERSION.md | head`。
3. **需要观察的现象**：你会看到若干条 `Updated Patch Count` 提交，每次提交通常只让 `VERSION.md` 里的最后一段数字 +1；而功能性提交（如 `GSB V2`、`Fix a memory leak ...`）本身不改版本号，由后续的 Patch Count 提交统一更新。
4. **预期结果**：你能说出当前版本是 `1.2.4307`，并理解它表示「主版本 1、次版本 2、构建号 4307」。
5. 如果你当前不在一个干净 checkout 的仓库上，`cat` 出来的数字可能不同——以你本地实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OpenFPGA 要把版本号单独放进 `VERSION.md`，而不是直接写死在 README 里？

**参考答案**：为了「单一数据源」。把版本号集中在一个文件里，README、构建脚本、CI 都来读它，就能避免「版本号在多个地方各写一份、更新时漏掉某处」的不一致问题。

**练习 2**：`1.2.4307` 中的 `4307` 大概率表示什么？怎样验证你的猜测？

**参考答案**：它大概率是一个持续累加的构建 / 补丁计数（patch count），随每次例行更新递增。可以通过 `git log -- VERSION.md` 查看 `Updated Patch Count` 类提交，看这个数字是否单调递增来验证；本项目最近提交里确实能看到 `Updated Patch Count` 这类记录。

## 5. 综合实践

把本讲两个最小模块串起来的小任务：**为 OpenFPGA 写一张「一页纸项目名片」**。

要求你产出一份不超过一页的 Markdown（写在你的笔记里即可，不要写进仓库），必须包含以下内容：

1. **一句话定位**：用你自己的话（不照抄 README）说清 OpenFPGA 是什么。
2. **输入 / 输出表**：两列表格，列出 OpenFPGA 的主要输入和主要输出。
3. **流程框图**：手画或用文本画出「Verilog → Bitstream」的四个主要阶段（综合、布局布线、fabric 构建、网表/比特流生成），并在每个阶段旁标注是 OpenFPGA 自己做的还是交给 Yosys/VPR 的。
4. **四大工具组成**：列出 FPGA-Verilog / FPGA-SDC / FPGA-Bitstream / FPGA-SPICE 各自的输出。
5. **版本与引用**：写明当前版本号（`VERSION.md` 第 1 行）以及官方推荐引用论文的题目。

完成后再回到本讲，对照 4.1 与 4.2 的内容自查：你的名片是否准确、是否把「IP 生成」和「在 FPGA 上跑设计」区分清楚了。这一页名片会是你后续读源码时随时回看的「地图」。

## 6. 本讲小结

- OpenFPGA 是**第一个有流片验证（silicon proofs）的开源 FPGA IP 生成器**，目标是敏捷地原型化可定制 FPGA（README.md:12）。
- 它解决的核心痛点：传统造定制 FPGA 要一年以上、还要手写配套 CAD 工具；OpenFPGA 用一份 XML 架构描述同时自动生成 fabric Verilog 网表和比特流工具。
- 它的**输入**是「用户 Verilog + VPR 架构 XML + openfpga_arch.xml」；**输出**是 fabric Verilog 网表、配置比特流、SDC 约束、SPICE 网表和自检 testbench。
- 端到端流程分四阶段：综合（Yosys）→ 布局布线（VPR）→ fabric 构建（OpenFPGA）→ 网表/比特流/约束生成（OpenFPGA）。
- OpenFPGA 由 **FPGA-Verilog、FPGA-SDC、FPGA-Bitstream、FPGA-SPICE** 四部分组成（motivation.rst:25）。
- 版本号集中维护在 `VERSION.md`（当前 `1.2.4307`），README 通过相对链接引用它，体现「单一数据源」。

## 7. 下一步学习建议

本讲建立了全局地图，但还没有带你进过仓库的目录结构。建议按手册顺序继续：

- **下一篇（u1-l2）**：讲仓库目录结构解析——搞清 `openfpga/`（核心引擎）、`libs/`（支撑库）、`openfpga_flow/`（流程脚本与数据）、子模块（`vtr-verilog-to-routing`、`yosys`）各自放什么。这是动手前的「认路」。
- **u1-l3**：从源码编译并运行环境搭建（`make checkout` + `make compile`，`openfpga.sh`）。
- **u1-l4**：跑通第一个 FPGA 设计流（`run-task`、`task.conf`）。

延伸阅读（项目官方文档，本讲已核对存在）：

- `docs/source/overview/motivation.rst`：四大工具的动机详解。
- `docs/source/overview/tech_highlights.rst`：支持的电路类型与 FPGA 架构特性清单。

等你读完 u1 整个单元，再进入 u2（Shell 入口与命令）和 u3（架构描述文件）就会顺理成章了。
