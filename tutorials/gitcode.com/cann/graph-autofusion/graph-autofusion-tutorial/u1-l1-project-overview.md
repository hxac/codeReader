# graph-autofusion 项目整体概览与定位

## 1. 本讲目标

本讲是整个学习手册的第一篇，面向**从未接触过本项目**的读者。读完本讲，你应当能够：

- 说清 graph-autofusion 是什么、面向什么硬件、解决什么问题。
- 区分仓库中两个已开源组件 **SuperKernel** 与 **Autofuse**，并各自指出它们要解决的核心性能问题。
- 用仓库中的真实证据（构建脚本、顶层 CMakeLists、文档）证明这两个组件是**解耦**的、可以独立选用。
- 建立「融合加速技术」的整体直觉，为后续逐模块精读源码打下基础。

本讲**不**深入任何一段具体实现代码，重点是建立定位与心智模型。

## 2. 前置知识

本讲从零开始，但有几个名词先解释清楚会更容易跟上：

- **昇腾（Ascend）芯片**：华为的 AI 加速芯片系列。本项目的所有优化都针对这类芯片，最终目的是让模型在昇腾上跑得更快。
- **算子（Operator / Op）**：模型在硬件上执行的最小计算单元，比如一个 `Add`、一个 `MatMul`。一个网络由成百上千个算子组成。
- **调度开销（Scheduling Overhead）**：算子从「被下发」到「真正开始计算」之间的等待与启动代价。算子越多，累计的调度开销越大。
- **Memory Bound（访存瓶颈）**：当计算的快慢主要由「把数据搬进/搬出计算单元」决定、而不是由「计算本身」决定时，就称该计算受限于内存带宽。
- **融合（Fusion）**：把多个算子合并成一个，减少中间环节，从而降低开销。这是贯穿本项目的核心主题。
- **AscendC / runtime**：昇腾算子编程语言（AscendC）和运行时环境（runtime）。README 明确说明本项目底层依赖极少，「仅依赖 AscendC 与 runtime 环境」。

> 不熟悉昇腾也没关系——本讲只讲「为什么需要融合」这一层直觉，硬件细节会在后续讲义中逐步展开。

## 3. 本讲源码地图

本讲主要阅读文档与工程入口文件，理解项目定位与组件关系：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目整体介绍：定位、开源节奏、目录结构、快速入门入口。 |
| `AGENTS.md` | 给仓库内工作的 agent 的指导，浓缩了项目概述、关键目录与开发规范。 |
| `autofuse/README.md` | Autofuse 组件介绍：自动融合原理、目录结构、框架使能与调测环境变量。 |
| `super_kernel/README.md` | SuperKernel 组件介绍：调度优化原理与四项深度优化技术。 |
| `CMakeLists.txt` | 顶层工程文件，决定哪些组件参与编译，是证明「组件解耦」的关键证据。 |
| `build.sh` | 一键式编译脚本，提供 `--no-autofuse` 等选项，是组件可独立选用的运行时证据。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：组件定位与价值、SuperKernel 与 Autofuse 的关系、融合加速技术总览。

### 4.1 组件定位与价值

#### 4.1.1 概念说明

graph-autofusion **不是单一的编译器或框架**，而是一个**面向昇腾芯片的融合加速「组件集合」**。

关键定语有三个，逐个拆开：

- **轻量级（lightweight）**：不引入庞大的依赖，不要求改造你现有的训练/推理栈。
- **解耦式（decoupled）**：组件之间彼此独立，可以「按需选用」——你可以只用其中一个。
- **融合加速技术**：所有组件都围绕同一个主题——通过「融合」相关手段让模型跑得更快。

它的底层依赖极少，README 原文说「仅依赖 AscendC 与 runtime 环境」。换句话说，它跑在昇腾算子编程语言和昇腾运行时之上，而不是依赖某个特定的深度学习框架。

为什么需要这样一个集合？因为模型执行慢，往往**不是某一个单一原因**导致的：有时候是被「调度开销」拖慢，有时候是被「内存搬运」拖慢。这两类问题的解决思路不同，所以项目把对应的能力拆成了独立的组件。

#### 4.1.2 核心流程

项目的「价值产生」流程，可以用一句话概括：

> 在昇腾芯片上，针对模型执行的不同瓶颈，提供对应的融合加速组件，让用户按需选用，最终降低模型总执行时间。

落到本讲，需要记住一条主线：

1. 识别模型执行的两类典型瓶颈：**调度开销** 与 **Memory Bound**。
2. 为每一类瓶颈提供一个独立组件（SuperKernel / Autofuse）。
3. 通过 codegen（代码生成）机制把这些优化落到具体的算子代码里。
4. 组件解耦、可独立构建，用户按需启用。

这条主线也是整个学习手册的「骨架」——后续每一讲都是顺着它深入某个环节。

#### 4.1.3 源码精读

先看 README 顶部的开源节奏，它说明了项目是**分阶段、逐组件开放**的：

[README.md:5-8](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/README.md#L5-L8) —— 「Latest News」记录了两件大事：2025/10 开源 SuperKernel（减少调度开销），2026/04 开源 Autofuse（自动融合）。这两条新闻本身就对应了两个组件要解决的两种性能问题。

再看项目的自我定位，这是理解整个项目最重要的一句话：

[README.md:12-13](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/README.md#L12-L13) —— 明确写出「面向昇腾芯片的轻量级、解耦式组件集合，旨在通过各种融合相关技术，加速模型执行」。

AGENTS.md 用一句话浓缩了同样的定位，并点明了组件构成：

[AGENTS.md:7](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/AGENTS.md#L7) —— 「面向昇腾芯片的融合加速组件集合，当前包含 SuperKernel 和 Autofuse 两个主要组件」。

#### 4.1.4 代码实践

> **实践目标**：亲手从源码中提取项目定位，而不是只看本讲的转述。

操作步骤：

1. 打开仓库根目录的 `README.md`，找到「🚀概述」小节。
2. 打开 `AGENTS.md` 的「项目概述」小节。
3. 用自己的话（一两句中文）写出 graph-autofusion 的定位，要求包含三个要素：**面向什么硬件**、**用什么手段**、**达到什么目的**。

需要观察的现象：
- README 与 AGENTS.md 对项目定位的描述是否一致。
- 两个文件是否都强调了「解耦」「融合」这两个关键词。

预期结果：你应该能写出类似「graph-autofusion 是面向昇腾芯片的融合加速组件集合，通过融合相关技术（codegen JIT 等）加速模型执行，组件之间解耦、可按需选用」的句子。

#### 4.1.5 小练习与答案

**练习 1**：README 为什么要强调「解耦式组件集合」，而不是做成一个大一统的库？

**参考答案**：因为模型执行的瓶颈有多种（调度开销、Memory Bound 等），不同用户只关心其中一种，且两种优化手段的实现路径差别很大。解耦后用户可以只引入需要的组件，降低依赖与风险，也便于单独维护和演进。

**练习 2**：本项目「底层依赖极少」具体指依赖什么？

**参考答案**：依赖 AscendC（昇腾算子编程语言）与 runtime（运行时环境），不绑定某个特定的深度学习框架。Autofuse 在框架侧通过 `torch.compile` 等 API 接入，但底层仍建立在 AscendC + runtime 之上。

---

### 4.2 SuperKernel 与 Autofuse 的关系

#### 4.2.1 概念说明

项目当前有**两个已开源组件**，它们各自瞄准一种性能瓶颈，互不重叠：

| 组件 | 解决的瓶颈 | 核心手段 | 一句话价值 |
|------|-----------|---------|-----------|
| **SuperKernel** | 算子调度开销 | 把多个子算子融合成一个「超核」+ JIT 编译 | 省下 N-1 次调度开销，优化算子执行头开销 |
| **Autofuse** | Memory Bound（访存瓶颈） | 自动识别相邻 Vector 算子并融合为一个 | 消除中间搬运，减少算子数量和内存搬运 |

理解它们的关系，关键是抓住「**它们解决的是两类完全不同的问题，所以彼此独立**」：

- SuperKernel 关注**算子和算子之间的调度代价**——即使每个算子本身已经很快，算子数量太多时，「下发→启动」的累计开销仍会拖慢整体。
- Autofuse 关注**算子和算子之间的数据搬运代价**——大量相邻的 Vector 计算会把中间结果反复写回/读出内存，导致计算单元在等数据。

一个从「调度」切入，一个从「访存」切入，因此它们是**正交的、可以单独使用**的两个组件。这就是 README 里「各组件之间独立，可按需选用」的含义。

#### 4.2.2 核心流程

两个组件的「正交关系」可以用下面的对照来理解（伪代码，非项目真实代码）：

```
一个有 N 个算子的子图：

  [算子1] → [算子2] → ... → [算子N]

两类代价同时存在：
  ① 调度代价：N 个算子，每个都要「下发 + 启动」  → SuperKernel 关心
  ② 搬运代价：相邻算子之间有 N-1 次中间内存读写 → Autofuse 关心

SuperKernel 的做法：把整图重编译成 1 个超核，省掉 (N-1) 次调度。
Autofuse   的做法：把相邻可融合的 Vector 算子合成 1 个，减少搬运。

二者各自成立，互不需要对方在场。
```

正因为两者目标不同，它们的**实现技术栈也不同**：SuperKernel 偏「运行时调度 + JIT」，Autofuse 偏「编译器式的图优化 + codegen + 自动 tiling」。这也是后续学习手册把 SuperKernel 和 Autofuse 分成两条独立线来讲的原因。

#### 4.2.3 源码精读

**证据 1：两个组件各自要解决的问题**——直接看它们的 README 第一段。

SuperKernel 的定位（调度优化）：

[super_kernel/README.md:5](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L5) —— 「面向网络图模型的调度优化技术……将整个网络模型重新编译为单一算子，从而显著降低算子调度开销」。

[super_kernel/README.md:12](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L12) —— 「将多个子算子融合成一个 SuperKernel，以节省 N-1 次算子调度开销」。

Autofuse 的定位（Memory Bound）：

[autofuse/README.md:3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3) —— 「存在大量的 Vector 计算，各个 Vector 计算之间会产生大量的内存搬运，导致 Memory Bound 问题……将多个算子融合为一个算子，减少网络中的算子数量和内存搬运」。

**证据 2：两个组件在工程上是解耦的**——看顶层 CMakeLists 如何分别纳入两个组件。

[CMakeLists.txt:54](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L54) —— 无条件地 `add_subdirectory(super_kernel)`。

[CMakeLists.txt:56-61](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/CMakeLists.txt#L56-L61) —— Autofuse 则被一个 `BUILD_AUTOFUSE` 选项包裹：默认 `ON`，可以通过选项关闭，且只有当 `autofuse/CMakeLists.txt` 存在时才纳入。这与 super_kernel 的无条件纳入形成对照，正是「Autofuse 可选、可裁剪」的工程体现。

**证据 3：构建脚本允许完全跳过 Autofuse**——这是组件可独立选用的最直接证据。

[build.sh:344-345](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L344-L345) —— 命令行支持 `--no-autofuse` 选项，帮助文本里写明它会「Skip autofuse backend build/package artifacts」。

[build.sh:477](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/build.sh#L477) —— 当传了 `--no-autofuse` 时，脚本会向 CMake 传入 `-DBUILD_AUTOFUSE=OFF`，从而让上面 CMakeLists 里的 `if(BUILD_AUTOFUSE)` 分支不执行。

**证据 4：README 显式声明解耦原则**。

[README.md:17-18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/README.md#L17-L18) —— 「专注融合加速技术」与「模块化与解耦：各组件之间独立，可按需选用；底层依赖极少，仅依赖 AscendC 与 runtime 环境」。

以上四条证据（两个组件各自的定位说明 + CMake 的条件纳入 + build.sh 的 `--no-autofuse` + README 的解耦声明）合在一起，足以证明 SuperKernel 与 Autofuse 互不依赖。

#### 4.2.4 代码实践

> 这就是本讲规格中要求的主实践任务。

操作步骤：

1. 阅读根目录 `README.md` 的「概述」与 `AGENTS.md` 的「项目概述」。
2. 阅读 `super_kernel/README.md` 的「原理介绍」第一段和 `autofuse/README.md` 的「简介」。
3. 打开顶层 `CMakeLists.txt` 第 54–61 行和 `build.sh` 中处理 `--no-autofuse` 的部分（约 344、477 行）。
4. 完成两项产出：
   - 用自己的话写出 **SuperKernel 解决的一个性能问题** 和 **Autofuse 解决的一个性能问题**（各一句）。
   - 列出 **两条「二者互不依赖」的工程证据**（例如：CMake 的条件纳入、`--no-autofuse` 选项）。

需要观察的现象：
- 两个组件的 README 是否分别只强调「调度开销」与「Memory Bound」这两个不同的词。
- 顶层工程文件是否把 Autofuse 当成「可选模块」对待。

预期结果（供对照）：
- SuperKernel：解决算子过多导致的**调度/启动开销**。
- Autofuse：解决相邻 Vector 算子间的**内存搬运（Memory Bound）**。
- 解耦证据示例：① 顶层 CMakeLists 用 `BUILD_AUTOFUSE` 选项包裹 Autofuse，super_kernel 却无条件纳入；② `build.sh --no-autofuse` 可完全跳过 Autofuse 的构建与打包。

#### 4.2.5 小练习与答案

**练习 1**：如果一句话里同时出现「减少算子数量」，SuperKernel 和 Autofuse 各自的「减少」含义有什么不同？

**参考答案**：SuperKernel 的「减少」指把 N 个算子**重编译成 1 个超核**，目的是省调度开销，子算子之间的逻辑边界仍在；Autofuse 的「减少」指把相邻可融合的算子**真正合并成同一个算子**的计算，目的是省掉中间内存搬运。

**练习 2**：为什么 Autofuse 在 CMakeLists 里要用 `option(BUILD_AUTOFUSE ...)` 包起来，而 super_kernel 不需要？

**参考答案**：因为 Autofuse 的编译较重（后续讲义会提到它编译时容易 OOM，需要限制并行度 `-j 8`），作为「可选」组件，提供开关让用户可以跳过；这也客观体现了「组件解耦、可按需选用」的设计原则。

---

### 4.3 融合加速技术总览

#### 4.3.1 概念说明

第三个最小模块是把两个组件的**具体优化技术**串成一个总览，让你对「融合加速」这个词有具象认识。

**SuperKernel 的「超核融合」** 是一种**调度层面的融合**：它在编译期拿到整网算子的先验信息（算子类型、前后序依赖等），用 JIT 把整网重编译成单个算子，从而：

- 省下 N-1 次调度开销；
- 并在此基础上叠加四项**深度优化**：ICache Preload（指令缓存预取）、Early-Start（前后算子部分指令并发）、同步优化（按算子类型细化同步范围）、子 Kernel 拆分（缓解多核对同一指令地址的争用）。

**Autofuse 的「自动融合」** 是一种**计算层面的融合**：它是一个基于 AscendC 的**编译器框架**，自动完成三件事：

1. **融合范围识别**：自动找出哪些相邻 Vector 算子可以合并；
2. **算子代码生成（codegen）**：把合并后的逻辑生成成一个新的算子实现；
3. **Auto Tiling 优化**：自动决定数据切分（tiling）策略，以适配硬件。

它还支持**动态 shape** 与**混合精度**，这意味着面对真实网络里形状多变、精度多样的算子，Autofuse 仍能工作。

可以用一个简单的对比记住两者分工：

| 维度 | SuperKernel | Autofuse |
|------|-------------|----------|
| 融合层面 | 调度层（整网→单算子） | 计算层（相邻算子→合并算子） |
| 主要敌人 | 调度/启动开销 | Memory Bound（内存搬运） |
| 技术形态 | 调度优化 + JIT | 编译器（图优化 + codegen + 自动 tiling） |
| 优化手段 | ICache 预取、Early-Start、同步优化、子核拆分 | 融合范围识别、代码生成、Auto Tiling |

#### 4.3.2 核心流程

把两个组件的技术放在一起，得到本项目的「融合加速技术全景」（伪结构，仅用于建立直觉）：

```
模型在昇腾上执行
   │
   ├──► 瓶颈 A：调度开销大（算子多、启动等待长）
   │       └─► SuperKernel
   │             · 整网 JIT 重编译成单个超核 → 省 N-1 调度
   │             · + ICache Preload / Early-Start / 同步优化 / 子核拆分
   │
   └──► 瓶颈 B：Memory Bound（相邻 Vector 算子搬运多）
           └─► Autofuse（一个编译器框架）
                 · 融合范围识别 → 找出可合并的相邻算子
                 · codegen      → 生成合并后的新算子
                 · Auto Tiling  → 自动决定数据切分
                 · 支持 动态 shape / 混合精度
```

后续学习手册（u2–u3）会带你分别跑通这两个组件；进阶层（u4–u9）沿 Autofuse 的数据流逐层拆源码；专家层（u10）再回到 SuperKernel 的运行时与融合决策。本讲只要建立这张全景图即可。

#### 4.3.3 源码精读

**SuperKernel 的四项深度优化**，原文在它的原理介绍里：

[super_kernel/README.md:5](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/super_kernel/README.md#L5) —— 一句话点明了「ICache 预取、Early-Start、同步优化、子 Kernel 拆分」这四项手段，以及它们都建立在「编译阶段即可获取全部子算子的先验信息」之上。这是理解 SuperKernel 全部优化的前提：**因为是编译期融合，所以能做更深的优化**。

**Autofuse 的能力清单**，原文在它的简介里：

[autofuse/README.md:3](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L3) —— 点明了 Autofuse 支持的四大特性：「自动融合范围识别、自动算子代码生成、Auto Tiling 优化、动态 shape 及混合精度」，并解释了 Memory Bound 的来源（「各个 Vector 计算之间会产生大量的内存搬运」）。

**Autofuse 的模块构成**（这也是后续源码精读的地图）：

[autofuse/README.md:9-27](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/README.md#L9-L27) —— 目录结构注释揭示了 Autofuse 内部的分工：`ascendc`（算子 API）、`ascir`（算子注册）、`att`（自动 tiling）、`codegen`（代码生成）、`optimize`（调度切分）、`graph_metadef`（基本图接口）、`compiler`（对外接口）、`inc`（供 GE 调用）、`v35`（昇腾 950 专属优化）。这正好对应后续 u4–u9 的学习路线。

#### 4.3.4 代码实践

> **实践目标**：从源码中把两个组件的「优化技术清单」亲手整理出来，建立全景图。

操作步骤：

1. 打开 `super_kernel/README.md`，在「原理介绍」中找出 SuperKernel 列举的深度优化技术（应能找到 4 项）。
2. 打开 `autofuse/README.md` 的「简介」，找出 Autofuse 自述支持的特性（应能找到 4 项），以及它声称缓解的问题（Memory Bound）。
3. 在笔记里画一张两列对照表：左列 SuperKernel 的技术，右列 Autofuse 的特性。

需要观察的现象：
- SuperKernel 的优化是否都依赖「编译期拿到先验信息」这一前提。
- Autofuse 的特性里是否包含「codegen」和「Auto Tiling」这两个词——它们是进阶层学习的主线。

预期结果：你能复述 SuperKernel 的四项优化名称，以及 Autofuse 的「范围识别 / 代码生成 / Auto Tiling / 动态 shape + 混合精度」四大特性。

#### 4.3.5 小练习与答案

**练习 1**：SuperKernel 的所有深度优化都建立在一个共同前提上，这个前提是什么？为什么？

**参考答案**：前提是「在编译阶段就能获取全部子算子的先验信息（类型、依赖等）」。因为只有事先知道全部信息，才能预取正确的指令（ICache Preload）、判断哪些指令可并发（Early-Start）、按类型定制同步范围（同步优化）、决定如何复制代码避免地址争用（子核拆分）。

**练习 2**：Autofuse 目录里的 `att` 模块对应它的哪一项特性？

**参考答案**：对应「Auto Tiling 优化」。`att` 即 Auto Tiling 的缩写，负责自动决定数据切分（tiling）策略。这一模块将在 u7（ATT 自动 Tiling）单元详细展开。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「项目定位速写」任务：

1. **定位陈述**：用一段话（3–4 句）向一个完全没听过 graph-autofusion 的同事介绍它，必须涵盖：面向昇腾、融合加速组件集合、两个组件分别解决调度开销与 Memory Bound、组件解耦可按需选用。

2. **证据收集**：在仓库里找到并记录以下三类证据的文件与大致行号：
   - 项目自我定位（提示：`README.md` 概述段、`AGENTS.md` 项目概述）。
   - 两个组件各自的性能目标（提示：两个组件各自的 `README.md` 第一段）。
   - 组件解耦的工程证据（提示：`CMakeLists.txt` 的 `BUILD_AUTOFUSE`、`build.sh` 的 `--no-autofuse`）。

3. **判断题自测**：基于你收集的证据，判断以下说法是否正确，并说明依据。
   - 「SuperKernel 和 Autofuse 必须一起使用。」
   - 「Autofuse 主要解决的是内存搬运导致的性能问题。」
   - 「SuperKernel 的优化只能在编译期做，因为它依赖先验信息。」

参考结论：① 错（有 `--no-autofuse`、CMake 条件纳入等解耦证据）；② 对（autofuse/README.md 第 3 行明确 Memory Bound）；③ 对（super_kernel/README.md 第 5、12 行说明其依赖编译期先验信息）。

## 6. 本讲小结

- graph-autofusion 是**面向昇腾芯片的轻量级、解耦式融合加速组件集合**，底层只依赖 AscendC 与 runtime。
- 它包含两个已开源组件：**SuperKernel**（2025/10 开源）和 **Autofuse**（2026/04 开源），分别针对不同瓶颈。
- **SuperKernel** 解决**调度开销**：把整网 JIT 重编译成单个「超核」，省下 N-1 次调度，并叠加 ICache 预取、Early-Start、同步优化、子核拆分四项深度优化。
- **Autofuse** 解决 **Memory Bound**：作为基于 AscendC 的自动融合编译器框架，提供融合范围识别、代码生成、Auto Tiling、动态 shape 与混合精度支持。
- 两者**正交、可独立选用**：工程上由顶层 CMakeLists 的 `BUILD_AUTOFUSE` 选项与 `build.sh` 的 `--no-autofuse` 选项保证。
- 后续学习手册的主线是：Autofuse 数据流（graph_metadef → ascir → optimize → att → codegen → compiler）逐层精读，SuperKernel 作为相对独立的并行线穿插讲解。

## 7. 下一步学习建议

本讲建立了整体定位，接下来建议按以下顺序继续：

1. **下一讲 u1-l2（仓库目录结构与组件关系）**：动手把目录结构画出来，建立「按目录找代码」的导航能力，这是后续所有源码精读的基础。
2. **u1-l3、u1-l4**：掌握一键构建脚本 `build.sh` 与上板运行环境，让两个组件真正跑起来。
3. 跑通后，再根据兴趣选择主线：
   - 想深入 **Autofuse 编译器**：从 u3（Autofuse 入门）开始，沿数据流走到 u9。
   - 想深入 **SuperKernel**：先看 u2（SuperKernel 组件入门），专家层的 u10 会展开其 AOT 运行时与融合决策。

建议在进入任何一讲之前，先确保自己能用一句话说清本讲小结里的「两者各解决什么问题、为何解耦」——这是整本手册的地基。
