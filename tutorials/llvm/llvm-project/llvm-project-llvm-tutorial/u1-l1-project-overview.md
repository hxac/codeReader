# 项目总览与定位

> 本讲对应学习路线：单元 1（认识 LLVM 项目）· 讲义 1
> 关键源码版本：HEAD `4e924a6276ef015e1482b68371bb8229368fe5f7`
> 本讲无需任何前置讲义，是整套手册的起点。

---

## 1. 本讲目标

读完本讲，你应该能够：

- 用一句话说清楚 **LLVM 是什么**，以及它解决了什么问题。
- 画出 **三段式编译模型**（前端 → 优化器 → 后端），并指出每一段分别由项目的哪个部分承担。
- 说出 LLVM 的 **许可证**（Apache 2.0 + LLVM Exceptions）以及它对实际使用意味着什么。
- 找到项目自带的 **主要文档入口**（`README.txt`、`docs/GettingStarted.md`、`docs/index.md`）。
- 对 `llvm/` 子目录下的 `lib/`、`include/`、`tools/`、`examples/`、`test/` 等 **顶层目录各自承担什么职责** 有一个整体认知，为后续讲义建立“地图”。

本讲是“看地图”的一讲，不要求你读懂任何 C++ 代码，重点是建立心智模型和阅读方向。

---

## 2. 前置知识

在开始之前，最好大致了解以下几个概念。不懂也没关系，本讲会用通俗语言再解释一遍。

- **编译器（Compiler）**：把一种语言（比如 C）翻译成机器能执行的代码的程序。常见例子是 GCC。
- **中间表示（Intermediate Representation，IR）**：源代码和目标机器码之间的“中间语言”。把它设计成一种独立格式，是 LLVM 最核心的思想之一。
- **前端（Front End）**：负责把源代码（C/C++ 等）翻译成 IR 的部分。LLVM 项目里这个角色通常由 **Clang** 扮演。
- **后端（Back End）**：负责把 IR 翻译成某种 CPU（如 x86、ARM）能执行的机器码的部分。
- **位码（Bitcode，`.bc`）**：IR 的一种紧凑二进制存储格式；与之对应的是人类可读的文本格式（`.ll`）。

> 一个关键直觉：**LLVM 本身并不是一个完整的编译器，而是一套“用来搭编译器”的工具箱**。你可以把它的库拼装成一个完整的编译器（像 Clang 那样），也可以只用它的优化器，甚至只用它的 JIT 执行引擎。理解这一点，后面所有内容都会顺理成章。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下，建议你在阅读时打开它们对照：

| 文件 | 作用 |
| --- | --- |
| `README.txt` | `llvm/` 目录的入口说明，一句话点明 LLVM 是什么、许可证在哪、文档在哪。 |
| `LICENSE.TXT` | 完整许可证文本，开头声明“Apache License v2.0 with LLVM Exceptions”，并附带 LLVM 例外条款。 |
| `docs/index.md` | 在线文档的总索引页，按“设计与概览 / 文档 / 社区”三大块组织所有入门资料。 |
| `docs/GettingStarted.md` | 最权威的入门指南，讲解如何获取源码、如何构建、**三段式模型**，以及完整的目录结构说明。 |

这些文件本讲都会逐段引用并给出永久链接。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **4.1** LLVM 定位与三段式编译模型
2. **4.2** 许可证与社区文档入口
3. **4.3** `llvm/` 子目录职责总览

### 4.1 LLVM 定位与三段式编译模型

#### 4.1.1 概念说明

很多初学者第一次听到“LLVM”时会困惑：它到底是一个编译器？一个库？还是一套规范？

答案的核心在 `README.txt` 的第一句：

> LLVM 是 **a toolkit for the construction of highly optimized compilers, optimizers, and runtime environments**（一套用于构建高性能编译器、优化器和运行时环境的工具箱）。

也就是说，LLVM 提供的是“积木”。你可以用这些积木搭出：

- 一个完整的 C/C++ 编译器（Clang + LLVM）；
- 一个只做优化的工具（`opt`）；
- 一个把 IR 转成汇编的后端（`llc`）；
- 一个在运行时动态编译并执行代码的 JIT（`lli` / ORC）。

而支撑这些能力的，是一个被反复强调的设计思想：**三段式编译模型（Three-Phase Compiler Design）**。

三段式把编译器拆成三段，中间用 **统一的中间表示（IR）** 衔接：

1. **前端（Front End）**：把源语言（C、C++、Rust、Swift……）解析、语义分析后，翻译成 LLVM IR。前端可以有很多个，互不相关。
2. **优化器（Optimizer）**：对 IR 做 IR 到 IR 的变换（死代码消除、内联、循环展开……），与源语言和目标机器都无关。这部分就是 LLVM 的核心优势所在。
3. **后端（Back End）**：把优化后的 IR 翻译成某一种目标机器的指令（x86、ARM、RISC-V、WebAssembly……）。后端也可以有很多个。

这种拆分带来两个巨大好处：

- **复用**：新增一门前端语言，可以白嫖所有后端；新增一个后端，可以白嫖所有前端语言。`M 种语言 × N 种机器` 不再需要 `M×N` 个编译器，只需 `M` 个前端 + `N` 个后端。
- **解耦**：优化器只认 IR，前后端换了它都不用改。这也是为什么 LLVM 的优化器能成为业界标杆。

#### 4.1.2 核心流程

一个源程序从 C 代码到可执行文件，在 LLVM 体系下的典型流程如下：

```text
   hello.c  ──[前端 Clang]──▶  LLVM IR (.ll/.bc)  ──[优化器 opt]──▶  优化后的 IR
                                                                                  │
                                                                                  ▼
                                                              可执行文件  ◀──[汇编器/链接器]──  目标汇编/机器码 (.s/.o)
                                                                                          ▲
                                                                                          │
                                                                                     [后端 llc]
```

要点：

- 前端只负责“把 C 变成 IR”，与目标机器无关。
- 优化器（`opt`）吃进 IR、吐出 IR，纯粹是 IR 到 IR 的变换。
- 后端（`llc`）吃进 IR、吐出汇编或目标文件，与源语言无关。
- 现实中，`clang` 把上述多步打包成一条命令（`clang hello.c -o hello`），让你感觉不到中间的 IR；但底层走的依然是这条链路。

> 小贴士：你完全可以跳过前端，自己手写一段 LLVM IR 文本（`.ll`），然后直接喂给 `opt` 或 `llc`。这也是后面讲义里大量练习的做法——绕开语言前端，直接在 IR 层面观察 LLVM 的行为。

#### 4.1.3 源码精读

**① LLVM 的自我定义**

[README.txt:L1-L6](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/README.txt#L1-L6) 用一句话点明身份：这个目录存放的是 LLVM 的源码，它是一套用于构建高性能编译器、优化器和运行时环境的工具箱。

```text
This directory and its subdirectories contain source code for LLVM,
a toolkit for the construction of highly optimized compilers,
optimizers, and runtime environments.
```

**② 三段式模型在官方文档里的描述**

三段式并非我们杜撰，`docs/GettingStarted.md` 的 Overview 段落就直接体现了它。先看“核心”这一段：

[docs/GettingStarted.md:L7-L13](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L7-L13) 说明：项目的核心本身就叫 “LLVM”，包含处理 IR 并把它转换成目标文件所需的全部工具、库和头文件——这就是三段式中的 **优化器 + 后端**。

紧接着的两段分别交代了 **前端** 和 **其它组件**：

[docs/GettingStarted.md:L14-L17](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L14-L17) 指出：类 C 语言使用 Clang 作为前端，把 C/C++/Objective-C 编译成 LLVM 位码，再由 LLVM 转成目标文件——这正是“前端 → IR → 后端”的链路。

[docs/GettingStarted.md:L18-L21](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L18-L21) 补充：其它组件还包括 libc++（C++ 标准库）、LLD（链接器）等，但它们属于整个 `llvm-project` 仓库的其它子项目，不在 `llvm/` 这一讲义范围内。

把这三段连起来读，三段式模型的三个角色（前端 Clang / 核心 LLVM = 优化器+后端 / 外围组件）就一清二楚了。

#### 4.1.4 代码实践

> **实践类型**：源码阅读型（本讲是入门“看地图”讲义，暂不要求构建运行）。

1. **实践目标**：用一句话写出 LLVM 的定位，并把三段式模型和具体工具对应起来。
2. **操作步骤**：
   - 打开 [README.txt](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/README.txt#L1-L6)，抄下 LLVM 对自己的定义。
   - 打开 [docs/GettingStarted.md 的 Overview 段](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L7-L21)。
   - 在笔记里画一张表，把“前端 / 优化器 / 后端”三行，分别填上对应的项目组件（提示：前端=Clang，优化器+后端=`llvm/` 核心）。
3. **需要观察的现象**：你会注意到文档把“处理 IR、转成目标文件”统称为核心 LLVM，而把“把 C 变成 IR”单列给 Clang——这正是三段式分界线的体现。
4. **预期结果**：你能写出类似 “LLVM 是一套构建编译器的工具箱，核心提供与语言无关的优化器和与机器相关的后端，前端由 Clang 等单独承担” 这样的一句话总结。
5. 如需构建验证，可待第二讲（构建系统）再做；本讲写「待本地验证」的运行类步骤即可。

#### 4.1.5 小练习与答案

**练习 1**：三段式模型里，优化器为什么和“源语言”以及“目标机器”都无关？

> **参考答案**：因为优化器只读写 LLVM IR 这一种中间表示。IR 是前端和后端之间的“通用语”，只要前端能把代码翻成 IR、后端能从 IR 生成机器码，优化器就不关心源是 C 还是 Rust、目标是 x86 还是 ARM。

**练习 2**：有人说“LLVM 就是一个 C/C++ 编译器”，这句话对吗？

> **参考答案**：不准确。LLVM 是一套工具箱（toolkit），核心是优化器与后端，本身不含 C/C++ 前端。我们日常说的“Clang 编译器”是 Clang（前端）+ LLVM（优化器+后端）的组合。

---

### 4.2 许可证与社区文档入口

#### 4.2.1 概念说明

阅读一个开源项目，先看许可证和文档入口，是低成本高收益的事——它能告诉你“我能怎么用”和“我该去哪学”。

LLVM 当前的许可证是 **Apache License 2.0 with LLVM Exceptions**（Apache 2.0 加 LLVM 例外条款）。这个组合在编译器领域相当友好：

- **Apache 2.0** 本身就是一个宽松的开源许可证，允许商业使用、修改、再分发，并带有明确的专利授权条款。
- **LLVM Exceptions** 是 LLVM 在 Apache 2.0 基础上加的两条额外豁免，专门解决编译器场景的特殊问题。

其中最重要的一条例外（见 4.2.3）解决了“编译产物里嵌入了 LLVM 代码片段怎么办”的问题——这对把 LLVM 用于产品编译器的人来说极其关键，意味着你编译出的用户程序不会因为链入了 LLVM 而被许可证义务波及。

至于文档入口，LLVM 的文档非常丰富但也很庞杂，初学者容易迷路。好在有两个明确的“路标”：

- `README.txt` 直接告诉你“去看 `docs/GettingStarted.md` 和 `docs/README.txt`”；
- `docs/index.md` 是在线文档总索引，按主题分类组织。

#### 4.2.2 核心流程

当你要找信息时，推荐的查阅路径是：

```text
想知道 LLVM 是什么 / 怎么开始 ──▶ README.txt ──▶ docs/GettingStarted.md
                                                        │
需要系统化、按主题找文档 ──────────────────────────────▶ docs/index.md
                                                        │
需要 API 级参考 ─────────────────────────────────────▶ https://llvm.org/doxygen/
```

也就是说：先从 `README.txt` 出发，跳到 `GettingStarted.md` 建立全局观；需要分类目录时再去 `docs/index.md`；需要 C++ API 细节时再去 Doxygen 站点。

#### 4.2.3 源码精读

**① 许可证声明**

[LICENSE.TXT:L1-L3](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/LICENSE.TXT#L1-L3) 开头一句话即声明整个项目采用“Apache License v2.0 with LLVM Exceptions”。

接着才是 Apache 2.0 的标准条款全文。这说明：要读懂“能怎么用”，重点是开头声明 + 例外条款，而不是逐条读 Apache 正文。

**② LLVM 例外条款**

[LICENSE.TXT:L208-L213](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/LICENSE.TXT#L208-L213) 是最关键的例外：如果你在编译自己的源代码时，有部分 LLVM 代码被嵌入了产物的目标码（Object form）中，你可以分发这些嵌入部分，而无需遵守 Apache 2.0 第 4(a)/4(b)/4(d) 条（即署名、修改声明等义务）。

```text
As an exception, if, as a result of your compiling your source code, portions
of this Software are embedded into an Object form of such source code, you
may redistribute such embedded portions in such Object form without complying
with the conditions of Sections 4(a), 4(b) and 4(d) of the License.
```

直觉解释：用 LLVM 编译你的程序，产物里偶尔会夹带一点 LLVM 的常量数据；这条豁免让你不必为这一点点嵌入物去履行繁琐的署名义务。这就是为什么企业可以放心把 LLVM 用作产品编译器。

**③ README 直接指路文档**

[README.txt:L11-L14](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/README.txt#L11-L14) 明确说：请看 `docs/`，特别是 `docs/GettingStarted.md`（入门）和 `docs/README.txt`（文档总览）。这是项目作者亲自给你画的两条路标。

**④ 在线文档总索引**

[docs/index.md:L10-L16](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/index.md#L10-L16) 开篇给出 LLVM 的覆盖范围：从工业级编译器、专用 JIT 应用，到小型研究项目；并说明文档按受众分成了若干大类（设计与概览、文档、社区等）。从这里你能顺着目录树找到 FAQ、Lexicon（术语表）、User Guides 等。

#### 4.2.4 代码实践

> **实践类型**：源码阅读 + 整理型。

1. **实践目标**：整理出一份“文档入口清单”，并理解 LLVM 例外条款的实际含义。
2. **操作步骤**：
   - 打开 [LICENSE.TXT:L208-L213](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/LICENSE.TXT#L208-L213)，用自己的话复述这条例外解决的是什么问题。
   - 打开 [docs/index.md](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/index.md#L10-L16)，把“Design & Overview / Documentation / Community”三大块分别能链到哪些子文档列出来（例如 FAQ、Lexicon、GettingStartedTutorials、UserGuides、Reference）。
3. **需要观察的现象**：你会发现 `index.md` 用了 Sphinx 的 `{toctree}` 和 `{doc}` 指令来组织目录树——这正是 LLVM 在线文档网站的生成方式。
4. **预期结果**：得到一张“主题 → 文件路径”的对照表，例如：
   - 入门上手 → `docs/GettingStarted.md`
   - 常见问题 → `docs/FAQ.rst`
   - 术语表 → `docs/Lexicon.rst`
   - 文档总索引 → `docs/index.md`
5. 完整文件名以你本地 `docs/` 目录实际为准；如有不确定，标注「待确认」。

#### 4.2.5 小练习与答案

**练习 1**：Apache 2.0 + LLVM Exceptions 相比纯 Apache 2.0，多给了什么？

> **参考答案**：多了“编译产物嵌入豁免”：当 LLVM 代码片段被嵌入你编译产出的目标码时，分发该产物可免除 Apache 2.0 第 4(a)/4(b)/4(d) 条的署名与修改声明义务，使 LLVM 更适合作为产品级编译器。

**练习 2**：如果你只想快速了解某个 LLVM 术语（比如 “pass”“basic block”），该先去哪个文档？

> **参考答案**：先去 `docs/Lexicon.rst`（术语表，见 `index.md` 的 Design & Overview 分区）。它是为这种“查词”需求准备的。

---

### 4.3 llvm/ 子目录职责总览

#### 4.3.1 概念说明

本套手册只聚焦 `llvm-project` 仓库里的 **`llvm/` 子目录**（即“核心 LLVM”）。在深入任何具体模块之前，先看清这个目录是怎么分层的，能帮你后面读源码时迅速定位。

`docs/GettingStarted.md` 的 “Directory Layout” 一节给出了官方的目录职责说明。本模块就是带你看懂这份地图，并把它和后续讲义对应起来。

需要先建立的认知是：`llvm/` 把几乎所有逻辑都写成 **库（libraries）** 放在 `lib/`，把对应的 **公开头文件** 放在 `include/`，而各种 **可执行工具**（`opt`、`llc`、`lli` 等）只是这些库的“薄壳”驱动，放在 `tools/`。这种“库 + 薄壳工具”的分层，是后续理解“为什么能写自定义 pass / 自定义工具”的基础。

#### 4.3.2 核心流程

源码在目录之间的组织关系可以这样理解：

```text
include/llvm/   ◀── 公共 API 头文件（你写代码时要 #include 的）
       │
       ▼
   lib/           ◀── 实现这些 API 的源码（按子系统分目录）
       │
       ▼
   tools/         ◀── 用 lib/ 拼出的可执行程序（opt/llc/lli…）
       │
       ▼
   examples/      ◀── 教学示例（ModuleMaker、Fibonacci、Bye、HowToUseLLJIT…）
       │
       ▼
   test/          ◀── 回归测试（lit + FileCheck）
```

- `lib/` 的子目录（`IR/`、`Analysis/`、`Transforms/`、`CodeGen/`、`Target/`、`MC/` 等）几乎与后续每个学习单元一一对应。
- `tools/` 里的每个目录通常对应一个命令行工具，是观察 `lib/` 行为最方便的窗口。
- `examples/` 是官方提供的“从零上手”范例，本手册大量练习会基于它们。

#### 4.3.3 源码精读

**① Directory Layout 总入口**

[docs/GettingStarted.md:L691-L696](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L691-L696) 是目录布局说明的开头，它建议你把 LLVM 的 Doxygen 文档作为代码结构的参考，随后逐个目录介绍。

**② lib/ 各子目录职责**

[docs/GettingStarted.md:L753-L805](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L753-L805) 把 `lib/` 拆成多个子目录并逐一说明。下面把官方描述与本手册后续讲义的对应关系整理成表：

| `lib/` 子目录 | 官方职责（GettingStarted.md） | 对应后续讲义 |
| --- | --- | --- |
| `lib/IR/` | 核心类（Instruction、BasicBlock 等）的实现 | u2（LLVM IR 基础） |
| `lib/AsmParser/` | LLVM 汇编（IR 文本）解析器 | u2-l4（IR 格式） |
| `lib/Bitcode/` | 位码读写 | u2-l4（IR 格式） |
| `lib/Analysis/` | 各种程序分析（调用图、归纳变量、自然循环等） | u4（分析与变换） |
| `lib/Transforms/` | IR 到 IR 的变换（死代码消除、SCCP、内联、循环不变量外提等） | u3、u4（Pass 与优化） |
| `lib/Target/` | 各目标架构描述（如 `lib/Target/X86`） | u7（目标后端与 TableGen） |
| `lib/CodeGen/` | 代码生成主干：指令选择、调度、寄存器分配 | u5、u6（代码生成、寄存器分配、调度） |
| `lib/MC/` | 机器码层表示与处理：汇编、目标文件发射 | u6-l3（MC 层） |
| `lib/ExecutionEngine/` | 运行时直接执行位码（解释器 / JIT） | u8-l1（ORC JIT） |

**③ tools/ 主要工具**

[docs/GettingStarted.md:L834-L888](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L834-L888) 介绍了最重要的几个可执行工具。它们正是三段式模型各阶段的“操作把手”：

| 工具 | 职责 | 在三段式中的位置 |
| --- | --- | --- |
| `llvm-as` | 人类可读 IR 文本（`.ll`）→ 位码（`.bc`） | IR 工具 |
| `llvm-dis` | 位码（`.bc`）→ 人类可读 IR 文本（`.ll`） | IR 工具 |
| `opt` | 读入 IR，施加一系列 IR 到 IR 变换后输出 | **优化器** |
| `llc` | 把 IR 翻译成原生汇编 | **后端** |
| `lli` | 直接解释 / JIT 执行位码 | 执行引擎 |
| `llvm-link` | 把多个 LLVM 模块链接成一个程序 | IR 工具（链接） |

> 这张表是本手册后续练习的“工具速查表”，建议收藏。第三讲（u1-l3）会专门带你看这些工具的命令行用法与源码入口。

**④ examples/ 的教学示例**

[docs/GettingStarted.md:L713-L729](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L713-L729) 介绍了 `examples/`：它包含如何用 LLVM 为自定义语言做编译的简单范例，其中最著名的是 **Kaleidoscope 教程**（手写词法分析器、解析器、AST，并做代码生成，含静态编译与 JIT 多种方案），以及 **BuildingAJIT** 教程。本手册会从更小的示例（`ModuleMaker`、`Fibonacci`、`Bye`、`HowToUseLLJIT`）入手。

#### 4.3.4 代码实践

> **实践类型**：源码地图绘制型（仅用只读命令，不修改任何文件）。

1. **实践目标**：把 `llvm/` 顶层目录与 `lib/` 子目录的职责整理成一张属于你自己的速查表。
2. **操作步骤**：
   - 列出顶层目录（命令示例：`ls -1d */`，仅只读查看）。
   - 对照 [docs/GettingStarted.md 的 Directory Layout 段](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/docs/GettingStarted.md#L691-L805)，给每个 `lib/` 子目录写一句中文职责。
   - 在 `tools/` 下找到 `opt`、`llc`、`lli`、`llvm-as`、`llvm-dis` 五个目录，确认它们都存在。
   - 在 `examples/` 下找到 `ModuleMaker`、`Fibonacci`、`Bye`、`HowToUseLLJIT` 四个示例，确认它们都存在。
3. **需要观察的现象**：你会看到 `tools/` 里绝大多数目录名就是命令行工具名（如 `llvm-as/`），且 `examples/` 里既有教学项目（`Kaleidoscope`）也有最小范例（`ModuleMaker`）。
4. **预期结果**：得到一张“目录 → 职责 → 对应讲义”的三列表，作为后续阅读源码的索引页。
5. 是否能在你本机直接构建，留到第二讲（u1-l2）验证；本讲仅做目录确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LLVM 要把绝大部分代码写成 `lib/` 里的库，而不是直接写在 `tools/` 里？

> **参考答案**：为了复用。库可以被任意工具或外部项目链接；`tools/` 里的程序只是库的薄壳驱动。这样 `opt`、`llc`、`lli` 以及你自己写的工具都能共享同一套核心实现，避免重复代码。

**练习 2**：`lib/Transforms/` 和 `lib/Analysis/` 有什么区别？

> **参考答案**：`lib/Analysis/` 负责“分析但不改”——计算调用图、归纳变量、循环结构等信息供别人使用；`lib/Transforms/` 负责“改”——基于分析结果对 IR 做变换（优化）。二者在新 Pass 管理器里分别对应 analysis pass 和 transformation pass，后续 u3/u4 会深入。

---

## 5. 综合实践

> **贯穿任务**：写一份《LLVM 一页速览》备忘单，把本讲三个模块串起来。

要求你的备忘单里至少包含以下四块内容：

1. **一句话定义**：参考 [README.txt:L1-L6](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/README.txt#L1-L6)，用自己的话写一句“LLVM 是什么”。
2. **三段式流程图**：画出 `源代码 →(前端 Clang)→ IR →(优化器 opt)→ IR →(后端 llc)→ 机器码`，并在每个箭头旁标注对应工具与 `lib/` 子目录（参考 4.1.2 与 4.3.3 的表）。
3. **许可证要点**：摘录 [LICENSE.TXT:L208-L213](https://github.com/llvm/llvm-project/blob/4e924a6276ef015e1482b68371bb8229368fe5f7/llvm/LICENSE.TXT#L208-L213) 的例外条款，写一句它对“把 LLVM 用作产品编译器”的意义。
4. **文档入口表**：列出 `README.txt`、`docs/GettingStarted.md`、`docs/index.md` 各自的用途，并给出至少两个子文档的路径（参考 4.2.4）。

**验收标准**：

- 备忘单全部用中文，术语首次出现时给出英文原文（如“中间表示（IR）”“三段式（Three-Phase）”）。
- 每条结论都能在给出的永久链接里找到出处，不编造。
- 留下“待本地验证”标记的位置：凡是涉及实际构建/运行命令的，本讲先不执行，留到 u1-l2、u1-l3 再做。

完成后，你就拥有了一张可以在后续阅读中随时回查的“地图”，本讲目标即告达成。

---

## 6. 本讲小结

- **LLVM 是一套构建编译器/优化器/运行时的工具箱**，而不是单一编译器；核心是优化器与后端，前端由 Clang 等单独承担。
- **三段式模型**（前端 → IR → 优化器 → 后端）通过统一的 IR 解耦语言与机器，是 LLVM 设计的灵魂，详见 `docs/GettingStarted.md`。
- **许可证**为 Apache 2.0 + LLVM Exceptions，其例外条款让编译产物中嵌入的 LLVM 片段免于繁琐署名义务，商业友好。
- **文档入口**首推 `README.txt` → `docs/GettingStarted.md`，分类查阅看 `docs/index.md`，API 细节看 Doxygen。
- **`llvm/` 采用“库 + 薄壳工具”分层**：`lib/` 放实现、`include/` 放头文件、`tools/` 放驱动、`examples/` 放示例、`test/` 放测试。
- **关键工具速查**：`opt`=优化器、`llc`=后端、`lli`=JIT/解释执行、`llvm-as`/`llvm-dis`=IR 文本与位码互转。

---

## 7. 下一步学习建议

本讲建立了全局认知，接下来建议按以下顺序推进：

1. **u1-l2 构建系统与目录结构**：学习如何用 CMake 实际构建出 `opt`、`llc` 等工具，把本讲的“地图”变成能跑起来的程序。
2. **u1-l3 命令行工具入口**：亲手使用 `opt`/`llc`/`lli`/`llvm-as`/`llvm-dis`，观察三段式模型在命令行的真实表现。
3. **u1-l4 第一个 IR 程序（ModuleMaker）**：通过官方示例，第一次用 C++ 构造 LLVM IR 并写出位码，为单元 2（LLVM IR 基础）打基础。

**建议同步阅读的源码**（在进入下一篇之前先扫一眼即可）：

- `CMakeLists.txt`（顶层构建入口）
- `docs/CommandGuide/` 下的 `opt.rst`、`llc.rst`（命令手册）
- `examples/ModuleMaker/README.txt`（最小示例说明）
