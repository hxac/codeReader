# 项目总览：ASIC-Design-Roadmap 是什么

> 本讲是整本学习手册的第一篇。如果你完全没接触过芯片设计，也不用担心——我们从「这个仓库到底是什么」讲起。

## 1. 本讲目标

读完本讲后，你应当能够：

- 用一两句话向别人解释 `ASIC-Design-Roadmap` 这个仓库是做什么的、为什么存在。
- 说清楚 **ASIC** 与 **FPGA**、通用处理器的区别，以及衡量芯片好坏的核心指标 **PPA**（功耗、性能、面积）。
- 指出本仓库面向的三类目标读者：在校学生、应届毕业生、从 FPGA/嵌入式领域转型进入 VLSI 的工程师。
- 看懂 `README.md` 的「目录式」组织方式，并能把仓库里的物理目录与学习内容对应起来。
- 知道本仓库采用 **MIT 许可证**，理解你能用它做什么、不能做什么，以及如何参与贡献。

本讲**不要求**你写过任何 Verilog，也不要求你装过任何 EDA 工具。本讲的全部依据来自两个文件：`README.md` 和 `LICENSE`。

## 2. 前置知识

在进入源码之前，先用最通俗的方式建立几个概念。

### 2.1 什么是芯片（IC）

芯片（Integrated Circuit，集成电路）就是把很多很多晶体管做在一小片硅片上，用导线连成电路。我们日常用的手机、电脑里的 CPU、内存颗粒，本质都是芯片。

### 2.2 通用芯片 vs 专用芯片（ASIC）

- **通用处理器**：像 CPU、GPU，设计成「什么都能算一点」，灵活但为了灵活性付出了代价。
- **FPGA（现场可编程门阵列）**：一种「可反复改写」的芯片。你可以把它想象成一块「乐高底板」，今天搭成电路 A，明天拆掉搭成电路 B。优点是灵活、上市快；缺点是它内部有大量可编程开关，**速度慢、功耗高、单价贵**。
- **ASIC（Application Specific Integrated Circuit，专用集成电路）**：为某一种专门用途「量身定制」的芯片。它的电路一旦做完就**固定**了，不能改。但正因为没有可编程开关的包袱，它在「专为这一件事」上能做到极致省电、极致快速、极致便宜（大批量时）。

一个简单的类比：

| 类型 | 是否可改 | 性能 | 单价（大批量） | 适合场景 |
|------|----------|------|----------------|----------|
| FPGA | 是 | 中 | 高 | 原型验证、小批量、需要频繁升级 |
| ASIC | 否 | 高 | 低 | 大批量量产、对功耗/速度要求极致 |

### 2.3 PPA：芯片设计的「不可能三角」

业界用一个缩写 **PPA** 来概括衡量一颗芯片好坏的三大指标：

- **P（Power，功耗）**：芯片运行时消耗多少电能，影响发热和电池续航。
- **P（Performance，性能）**：芯片能跑多快，通常用主频/时钟频率衡量。
- **A（Area，面积）**：芯片占多大硅片面积，面积越小，单颗成本越低。

这三者往往是「鱼与熊掌不可兼得」：想跑得更快通常更费电、面积更大；想省电又往往要牺牲速度。所以 ASIC 设计的很多工作，本质上就是在 PPA 三者之间做**权衡（trade-off）**。

> 本讲只建立直觉，后续讲义（尤其是 ICC2 物理设计主流程 U4）会反复出现 PPA 这个词。

## 3. 本讲源码地图

本讲只涉及两个文件，它们也是整个仓库的「门面」：

| 文件 | 作用 |
|------|------|
| `README.md` | 仓库的「自我介绍」。它既解释了项目定位、目标读者，又充当一份学习路线索引（目录式地列出课程、资料、工具和开源 IP）。 |
| `LICENSE` | 仓库的开源许可证文本，决定别人能怎样合法使用、修改和分发这些资料。 |

> 小提示：仓库里还有大量 `.tcl`、`.pl`、`.v`、`.upf` 脚本和 PDF 文档，但它们都属于后续讲义的内容。本讲只聚焦在「认识项目」这一层。

## 4. 核心概念与源码讲解

### 4.1 项目定位与 PPA 价值

#### 4.1.1 概念说明

`ASIC-Design-Roadmap` 这个仓库本质上是一份**开源的 ASIC 设计学习路线图（roadmap）**。它不是某一个具体芯片的设计项目，而更像一位经验丰富的工程师整理出来的「学习地图」：把散落在网上的优质课程、书籍、工具和开源 IP 收拢起来，并配上自己写的真实 EDA 脚本，让初学者有一条清晰可走的路。

它的核心价值主张，正是上一节讲的 **PPA**：ASIC 之所以值得学、值得做，是因为它能针对专用场景把功耗、性能、面积优化到极致。

#### 4.1.2 核心流程

仓库作者用一段「引子」建立了整个学习动机，逻辑链条大致是：

1. ASIC 是为专用场景优化的芯片，PPA 表现优于 FPGA 等通用方案 → **值得学**。
2. ASIC 设计流程长且深，从想法到硅片要经过很多专业步骤 → **难，容易迷路**。
3. 网上资料零散、质量参差 → **初学者更迷茫**。
4. 所以作者整理出这份开源 roadmap → **让学习更顺畅、更有条理**。

这条动机链条，就是后面所有讲义的「为什么」。

#### 4.1.3 源码精读

仓库的标题与一句话定位写在 `README.md` 最开头：

```markdown
# 🧠 ASIC Design Roadmap

> Empowering students worldwide with a complete roadmap to learn Application Specific Integrated Circuit (ASIC) Design — from logic to layout, RTL to GDSII.
```

这段话点明了三件事：面向 **students worldwide**（全球学生）、提供一条 **complete roadmap**（完整路线）、覆盖范围 **from logic to layout, RTL to GDSII**（从逻辑到版图，从 RTL 到 GDSII）。其中 **RTL** 是用硬件描述语言写出的寄存器传输级代码，**GDSII** 是最终交给芯片工厂（foundry）制造的版图文件格式——从 RTL 到 GDSII，正是整个 ASIC 设计的起点和终点。

随后作者解释了 ASIC 是什么，并直接点出 PPA：

> ASICs are purpose-built chips tailored for specific applications. Unlike general-purpose processors or FPGAs, they are optimized for **power, performance, and area (PPA)**…

紧接着用一个清单把 ASIC 的优缺点讲得很直白，参见 [README.md:14-18](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L14-L18)，中文大意是：

- ✅ 比 FPGA 更省电
- ✅ 能跑到更高频率
- ✅ 适合大批量生产
- ⚠️ 不适合需要频繁升级的场景
- ⚠️ 流片（tape-out，即把设计送去制造）之后发现 bug 代价极高

> 这里的「流片之后 bug 代价极高」正是 ASIC 区别于 FPGA 的关键：FPGA 出错可以重新烧写，ASIC 做错了就是真金白银的硅片报废。这也是为什么 ASIC 设计需要如此多的验证和签核（sign-off）流程——这些都会在后续 U4、U6 讲义中展开。

作者接着说明为什么要做这份 roadmap，参见 [README.md:23-24](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L23-L24)：很多学生和初级工程师想入门 IC 设计却不知从何开始，网上零散的帖子和视频反而让人更迷茫，于是作者决定做这份开源路线图。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，无需运行任何工具。

1. **实践目标**：从 `README.md` 里「抠」出作者对 ASIC 价值的核心论述，形成你自己的理解。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`。
   - 定位到第 12 行附近，找到包含 `PPA` 字样的那段话。
   - 定位到 [README.md:14-18](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L14-L18) 的优缺点清单。
3. **需要观察的现象**：注意作者把哪些优点打了 ✅、哪些缺点打了 ⚠️，体会「PPA 优势」与「不可修改/流片成本」之间的取舍。
4. **预期结果**：你能用自己的话写出一句话，例如「ASIC 用不可重配置换来更优的 PPA，因此适合大批量量产，但流片后改 bug 极贵」。
5. 待本地验证（如果你想把这段总结贴到自己的笔记里，确认引用的行号与当前 HEAD 一致即可）。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者说 ASIC「Not suitable for frequent upgrades」（不适合频繁升级）？

> **参考答案**：因为 ASIC 的电路在制造后就固定了，无法像 FPGA 那样重新烧写。如果产品需要频繁更新算法或功能，每次改动都要重新走一遍昂贵的 ASIC 设计和流片流程，不划算。

**练习 2**：PPA 中的三个字母分别指什么？为什么说它们是「权衡」关系？

> **参考答案**：Power（功耗）、Performance（性能）、Area（面积）。三者常常互相矛盾，例如提升性能（更高频率）通常会增加功耗、也需要更大面积来容纳更复杂的电路结构，因此设计时需要在三者之间折中。

---

### 4.2 README 的目录式结构

#### 4.2.1 概念说明

`README.md` 并不是一整段平铺的文字，而是用**目录（Table of Contents）**组织起来的「导航式」文档。这种写法的好处是：读者一眼就能看到全貌，想学哪一块就直接跳过去。

需要区分两个层面的「结构」：

1. **README 自身的章节结构**：指这份 Markdown 文档内部用标题分成的几大块（学习内容索引）。
2. **仓库的物理目录结构**：指 Git 仓库里真实的文件夹和文件（脚本、Verilog、PDF 等）。

本节先讲第一层（README 章节结构），并在实践里带你看第二层。

#### 4.2.2 核心流程

README 用一个目录把全文内容分成五大块，并用 Markdown 链接（`#锚点`）实现页内跳转：

```text
Table of Contents
├── Introduction          # 介绍 roadmap 的目的
├── Fundamentals          # 基础：数字电路、Verilog、计算机体系结构、RTL→ASIC
├── ASIC Design Flow      # 流程：逻辑综合与时序收敛、物理设计
├── Awesome Digital IC    # 精选资源清单（FPGA/HDL/EDA/验证）
└── Project Repos & IPs   # 开源 IP 与项目（OpenCores、RISC-V 核等）
```

这种「目录 + 内容块」的模式是开源学习型仓库的常见写法：目录是骨架，每一块下面再挂具体的外部链接（YouTube 播放列表、GitHub 仓库、在线教程）。

#### 4.2.3 源码精读

README 的目录定义在 [README.md:40-47](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L40-L47)：

```markdown
## 📘 Table of Contents

- [Introduction](#introduction)
- [Fundamentals](#fundamentals)
- [ASIC Design Flow](#asic-design-flow)
- [Awesome Digital IC Resources](#awesome-digital-ic-resources)
- [Project Repositories and IPs](#project-repositories-and-ips)
```

注意每个条目括号里的 `#xxx` 是页内锚点，对应下文中某个二级标题的英文小写形式。例如 `[Fundamentals](#fundamentals)` 会跳到 [README.md:56](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L56) 的 `## 🧱 Fundamentals` 这一节。

在 `Fundamentals` 这一大块下，作者又用三级标题细分了四个基础方向，参见 [README.md:58-73](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L58-L73)：

1. Digital Electronics & CMOS Basics（数字电子与 CMOS 基础）
2. Digital Logic Design / Frontend（数字逻辑设计 / 前端）
3. Computer Architecture（计算机体系结构）
4. Digital IC Design (RTL to ASIC)（数字 IC 设计：从 RTL 到 ASIC）

这正好是一条「由浅入深」的前端学习路径：先懂基本电路 → 再学用 Verilog 描述逻辑 → 再懂计算机怎么组织 → 最后把 RTL 变成真正的 ASIC。

除了这五大块，README 还在后面补充了大量「Tutorials and Courses」「Tools」「Online Judge Platforms」等内容，参见 [README.md:145-203](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L145-L203)，这些是作者精选的实战练习和工具入口。

> 小结：README 的「目录式结构」本质上是一份**学习内容的索引**。它告诉你「该学什么、按什么顺序学、去哪里找资料」，但真正可读的脚本和代码，藏在仓库的物理目录里（见下面的实践）。

#### 4.2.4 代码实践

这是一个**仓库结构梳理型实践**。

1. **实践目标**：把「README 的章节结构」与「仓库的物理目录」对应起来，建立空间感。
2. **操作步骤**：
   - 在仓库根目录执行 `git ls-files`（只读命令），观察顶层有哪些目录。
   - 把你看到的目录归类填进下面的表格。
3. **需要观察的现象**：你会发现仓库的物理目录大致对应不同工具/阶段，例如 `IC Compiler II/`（Synopsys ICC2 PnR 脚本）、`PrimeTime/`（静态时序分析）、`MY-Design/`（一个简单 Verilog 设计样例）、`cmsdk/`（ARM Cortex-M0 SoC 样例）、`mentor_scripts/`（Mentor/Siemens Nitro 流程）等。
4. **预期结果**：你能产出一张类似下表的「目录作用速查表」。

   | 目录 | 一句话作用 |
   |------|------------|
   | `IC Compiler II/` | Synopsys ICC2 物理设计（PnR）主流程脚本，是本仓库的核心 |
   | `IC Compiler/` | Synopsys 传统 ICC 流程脚本，用于对比学习 |
   | `PrimeTime/` | PrimeTime 静态时序分析（STA）脚本 |
   | `mentor_scripts/` | Mentor/Siemens Nitro 参考流程脚本 |
   | `MY-Design/` | 一个小型 Verilog 设计及其约束，作为练习素材 |
   | `cmsdk/` | ARM Cortex-M0 DesignStart（CMSDK）SoC 样例 |
   | `LEF2FRAM/` | LEF 到 FRAM/Milkyway 的层映射工具 |
   | `Figures/` | 文档配图 |
   | `Guide to HDL Coding Styles for Synthesis/`、`HDL Compiler ... Manual/` | 综合/编译相关的 PDF 参考资料 |

5. 待本地验证（不同克隆时刻文件列表可能略有差异，以你本地 `git ls-files` 输出为准）。

#### 4.2.5 小练习与答案

**练习 1**：README 的目录里，「ASIC Design Flow」一节对应仓库里的哪些物理目录？

> **参考答案**：主要对应 `IC Compiler II/`（ICC2 物理设计）、`IC Compiler/`（传统 ICC 流程）、`mentor_scripts/`（Nitro 流程）、`PrimeTime/`（时序签核），以及 `low_power.upf`、`LEF2FRAM/`、库生成脚本等。README 的「流程」章节是概念描述，仓库目录则是这些流程的真实脚本实现。

**练习 2**：README 目录里的锚点（如 `#fundamentals`）是怎么和正文标题对应的？

> **参考答案**：Markdown 会把标题文本转成小写、空格替换成连字符作为锚点 id。所以 `## 🧱 Fundamentals` 对应锚点 `#fundamentals`，目录里写 `[Fundamentals](#fundamentals)` 即可页内跳转。

---

### 4.3 LICENSE 与开源贡献

#### 4.3.1 概念说明

一个开源仓库「能被别人怎么用」由它的**许可证（License）**决定。没有许可证的代码默认是「保留所有权利」，别人其实不能合法使用；而明确的许可证则给出了清晰的授权规则。

本仓库使用的是 **MIT License**——业界最宽松、最受欢迎的开源许可证之一。它的核心精神是：**「随便用，但别告我」**。

#### 4.3.2 核心流程

MIT 许可证的逻辑可以浓缩成两段：

1. **授权（你可以）**：免费使用、复制、修改、合并、发布、分发、再授权甚至**卖**这份资料，商业用途也允许。
2. **义务（你必须）**：在所有副本或重要部分里**保留版权声明和这份许可证文本**。
3. **免责（你不能）**：软件按「原样」提供，**不提供任何担保**；无论怎么用出了问题，作者都不负责。

#### 4.3.3 源码精读

`LICENSE` 文件开头表明许可证类型和版权人，参见 [LICENSE:1-3](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L1-L3)：

```text
MIT License

Copyright (c) 2021 Ahmed Abdelazeem
```

紧接着是授权条款，列出你可以做的几乎所有事，参见 [LICENSE:5-10](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L5-L10)：

```text
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software ... to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software ...
```

唯一附带的条件是必须保留版权与许可声明，参见 [LICENSE:12-13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L12-L13)。

最后是「免责声明」，声明软件按原样提供、不承担任何责任，参见 [LICENSE:15-21](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L15-L21)。

README 的结尾也呼应了「开源贡献」的理念，鼓励读者分享和回馈，参见 [README.md:213](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L213)：

```markdown
If this roadmap helped you, consider sharing it with others or contributing back to the repo!
```

并给出了作者的联系方式，参见 [README.md:214](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L214)。也就是说，这个仓库欢迎你：发现错误就提 Issue、有补充资料就提 Pull Request、觉得有用就分享给同学。

> 小提示：仓库里引用了大量**外部链接**（YouTube、GitHub、在线教程）。注意——MIT 许可证只覆盖**本仓库自己的内容**，那些外部资源各自有自己的版权和使用条款，不能当成 MIT 授权来用。

#### 4.3.4 代码实践

这是一个**许可证理解型实践**。

1. **实践目标**：搞清楚在 MIT 许可证下，你「能」和「不能」做什么。
2. **操作步骤**：
   - 打开 `LICENSE` 文件，通读 21 行全文。
   - 用笔/记事本把授权清单和免责清单分别列出来。
3. **需要观察的现象**：注意 MIT 几乎不限制用途（连商用、转卖都允许），但强制要求保留版权声明，并且明确拒绝任何担保。
4. **预期结果**：你能回答这样一个场景题——「我想把这份 roadmap 整理进我自己公司的内部培训材料，合法吗？」
5. 预期答案：合法。你可以自由复制、修改、分发，但必须在这份材料里**保留 `Copyright (c) 2021 Ahmed Abdelazeem` 和 MIT 许可证声明**；同时作者不为该材料的准确性承担任何责任。

#### 4.3.5 小练习与答案

**练习 1**：MIT 许可证要求你在使用时必须保留什么？

> **参考答案**：必须保留版权声明（`Copyright (c) 2021 Ahmed Abdelazeem`）和本许可证全文（或至少是许可声明段落）。

**练习 2**：MIT 和「公有领域（public domain）」是一回事吗？

> **参考答案**：不是。公有领域意味着完全没有版权约束；而 MIT 仍然保留版权，只是大方地授权给别人使用，并附加「保留声明」和「免责」两个条件。所以 MIT 不是「没有版权」，而是「有版权但宽松授权」。

**练习 3**：仓库里链接的 YouTube 课程也受 MIT 许可证保护吗？

> **参考答案**：不受。MIT 许可证只覆盖本仓库作者自己创作的内容。YouTube 上的课程、别人的 GitHub 仓库等外部资源，版权归各自的作者或平台，需要遵守它们各自的条款。

---

## 5. 综合实践

把本讲三个模块串起来，完成一份「项目速写卡」：

> 假设你要向一位完全不懂芯片的室友介绍 `ASIC-Design-Roadmap`，请用 200 字以内写一段话，必须包含：
>
> 1. 这个仓库**是什么**（用「roadmap/学习路线图」来概括）。
> 2. 它**为什么有价值**（结合 PPA 与 ASIC vs FPGA）。
> 3. 它**面向谁**（三类目标读者）。
> 4. 它的**内容怎么组织**（README 的目录式结构 + 仓库里的真实脚本目录）。
> 5. 它**能被怎么用**（MIT 许可证：可自由使用/修改/分发，但需保留声明）。

**操作建议**：

- 第 1、2 点：参考 [README.md:1-3](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L1-L3) 和 [README.md:12-18](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L12-L18)。
- 第 3 点：参考 [README.md:26-27](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L26-L27)。
- 第 4 点：参考 [README.md:40-47](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L40-L47) 的目录 + 你用 `git ls-files` 看到的物理目录。
- 第 5 点：参考 [LICENSE:1-3](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L1-L3) 与 [LICENSE:5-10](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/LICENSE#L5-L10)。

写完后，你可以把它和同学互相点评——这是检验你是否真的「认识了这个项目」的最好方式。

## 6. 本讲小结

- `ASIC-Design-Roadmap` 是一份**开源的 ASIC 设计学习路线图**，目标是把零散的网络资料整理成一条清晰的学习路径，覆盖「从逻辑到版图，从 RTL 到 GDSII」。
- ASIC 是为专用场景量身定制的芯片，相对 FPGA 它用「不可重配置」换来更优的 **PPA（功耗/性能/面积）**，适合大批量量产，但流片后改 bug 代价极高。
- 本仓库面向三类读者：**在校学生、应届毕业生、从 FPGA/嵌入式转型进入 VLSI 的工程师**。
- `README.md` 采用**目录式结构**，用五大章节（Introduction / Fundamentals / ASIC Design Flow / Awesome Digital IC / Project Repos & IPs）组织学习内容；仓库物理目录则存放真实的 EDA 脚本（ICC2、ICC、PrimeTime、Mentor Nitro 等）与 RTL 样例。
- 仓库采用 **MIT 许可证**，授权极为宽松（可自由使用、修改、分发、商用），但要求保留版权声明且不提供任何担保。
- 后续讲义会深入到具体脚本：先建立 RTL 与 SDC 基础（U2），再进入 ICC2 物理设计主流程（U4）。

## 7. 下一步学习建议

本讲只是「站在门口看了一眼」。建议按以下顺序继续：

1. **先读下一篇** [u1-l2：ASIC 设计流程全景——从 RTL 到 GDSII](u1-l2-asic-flow-panorama.md)，它会把本讲提到的「RTL→GDSII」展开成一条完整的设计主线，并指出仓库脚本分别对应哪些阶段。
2. **再读** [u1-l3：仓库目录结构与学习资源地图](u1-l3-repo-structure-map.md)，更系统地梳理仓库里每个目录的作用。
3. 如果你想立刻动手写一点 Verilog，可以直接跳到 **U2** 的 [u2-l1：读懂一个简单 Verilog 设计](u2-l1-verilog-basic-design.md)，但要记得回头补流程全景。

> 学习心态（借 README 末尾作者的话）：**Start slow, stay consistent. Simulate everything before synthesizing.**（慢慢来，坚持住；综合之前先仿真。）
