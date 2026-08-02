# MLIR 是什么：项目定位与设计哲学

## 1. 本讲目标

本讲是整本 MLIR 学习手册的第一篇，面向「完全没接触过 MLIR」的读者。读完本讲，你应当能够：

- 用一句话说清楚 MLIR（Multi-Level IR，多层中间表示）是什么、它想解决什么问题；
- 理解「多层 / 可扩展中间表示」的设计动机，以及它和传统单层 LLVM IR 的根本差异；
- 说出至少三到四个 MLIR 在真实世界中的方言（Dialect）应用方向（编译器、硬件加速器、AI 等）；
- 知道本仓库里哪些文档是本讲以及后续学习的权威资料入口。

本讲**不要求你安装、编译或运行任何东西**——这是一篇「建立心智模型 + 阅读源文档」的讲义。具体的「跑起来」会放到第 3 讲（mlir-opt 工具入口）。

## 2. 前置知识

为了读懂本讲，你需要具备以下基础概念（不熟悉也没关系，下面会用通俗语言再解释一遍）：

- **编译器（Compiler）**：把一种程序表示翻译成另一种程序表示的工具，例如把 C++ 翻译成机器码。
- **中间表示（Intermediate Representation，简称 IR）**：编译器在「源代码」和「最终机器码」之间使用的、方便分析和变换的内部数据结构。可以把 IR 想象成「编译器内部用的通用语言」。
- **SSA（Static Single Assignment，静态单赋值）**：一种 IR 约定——每个变量只被赋值一次。这让编译器做优化（比如判断两个值是否相同）变得非常容易。LLVM IR 就是 SSA 形式。
- **LLVM**：一个流行的编译器基础设施，它的 IR（LLVM IR）是业界事实标准之一。MLIR 是 LLVM 项目下的子项目，二者关系密切（后文详述）。

> 术语提示：本讲会出现 **Operation（操作）**、**Value（值）**、**Block（基本块）**、**Region（区域）**、**Dialect（方言）**、**Pass（变换趟）** 等 MLIR 核心名词。本讲只需要你建立一个粗略印象，精确定义会在第 2 单元（核心数据结构）和第 3 单元（Dialect 定义）里深入讲解。

## 3. 本讲源码地图

本讲引用的关键文件都是**文档类源文件**（MLIR 仓库把规范、理由、教程都当作「源」来维护），它们是后续所有讲义的基础参考资料：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/README.md#L1-L3) | mlir 子项目的入口说明（内容极简，主要指向官网）。 |
| [docs/LangRef.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L1-L24) | **MLIR 语言参考**，定义 IR 文本语法的权威文档。 |
| [docs/Rationale/Rationale.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L1-L27) | **设计理由文档**，记录「为什么这样设计」的取舍与备选方案。 |
| [docs/Tutorials/Toy/Ch-2.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/Toy/Ch-2.md#L1-L23) | Toy 教程第 2 章，最清晰地阐述了 MLIR 的可扩展性动机。 |
| [docs/Tutorials/_index.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/_index.md#L1-L5) | 教程总索引，列出官方提供的两条学习路径。 |

> 一个诚实的观察：本仓库的 `README.md` 全文只有 3 行，核心内容是「See https://mlir.llvm.org/」。这说明 MLIR 把真正的说明性内容放在了 `docs/` 下的规范文档里，而不是 README 里。本讲后续的「源码精读」也主要引用 `docs/` 下的文档。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：①多层中间表示的核心思想；②MLIR 与 LLVM IR 的关系与差异；③MLIR 的典型应用场景。

---

### 4.1 多层中间表示的核心思想

#### 4.1.1 概念说明

传统编译器（比如经典 LLVM）通常只用**一种**中间表示：源代码经过前端解析后，尽快降到一层低级的、接近机器的 IR（LLVM IR），然后在这层做几乎所有优化，最后生成机器码。这叫**单层 IR** 设计。

问题在于：现实中的「源」往往抽象层次很高（比如一个 TensorFlow 计算图、一个 PyTorch 模型、一个高级循环嵌套），而机器码又抽象层次很低。把这两端硬塞进**同一层** IR，会导致两个尴尬：

1. **高层信息丢失得太早**：循环结构、张量形状、并行维度等「高层语义」一旦降到 LLVM IR 就难以重建，而这些信息对 GPU/加速器优化至关重要。
2. **前端各自造轮子**：每个高级语言前端（Clang、Swift、各种 ML 框架）都得自己实现一套「从 AST/计算图到 LLVM IR」的分析与变换基础设施，无法复用。

MLIR 的核心思想就是「**多层（Multi-Level）**」：不要强求一层 IR 表达一切，而是提供**一个可扩展的基础设施**，让同一套框架能够表示**从最高层（领域计算图）到最低层（接近机器码）的多个抽象层级**，并通过「逐步降低（progressive lowering）」在层级之间迁移。

用一句话概括：

> MLIR 不是一个「具体的 IR」，而是一台「**用来制造各种 IR 的机器**」。

#### 4.1.2 核心流程

MLIR 的典型使用流程是一个**从高到低逐层降低**的管线（pipeline）：

```
源语言 / 计算图
     │  （前端生成高层方言 IR，例如 TOSA / Linalg / Toy）
     ▼
┌──────────────── MLIR 同一套基础设施 ────────────────┐
│  高层方言  ──lower──▶  中层方言  ──lower──▶  LLVM 方言  │
│ (TOSA/Linalg)        (Affine/Vector/MemRef)      (llvm.*)   │
└──────────────────────────────────────────────────┘
     │  （翻译 / translate）
     ▼
  LLVM IR  ──▶  机器码
```

每一层都是 MLIR 的一种「方言（Dialect）」。层与层之间通过一个叫 **lowering（降级）** 的过程衔接：把高层操作改写成等价的、更接近底层的操作。关键点在于：

- **同一套基础设施**贯穿所有层级——同一套 IR 数据结构、同一套 Pass 管线、同一套文本/字节码格式。
- **抽象层级是连续的**，不是「要么高层要么底层」的二选一。这一点 LangRef 用一句话点明：MLIR 的设计「提供了一个从数据流图一路降到高性能目标相关代码的框架」。

「多层」也体现在数学层面：MLIR 借鉴了**多面体模型（polyhedral model）** 的思想，把循环嵌套、多维数据访问表示成仿射（affine）函数，例如一个二维访问 \((i, j) \mapsto (128i + d, 128j + e)\) 可以被精确分析、变换（分块、交换、融合等）。这种仿射表示是「第一等公民」（first-class concept），而不是事后才贴上去的标注。

#### 4.1.3 源码精读

**① 官方对 MLIR 的一句话定义**——LangRef 开头：

[docs/LangRef.md:L3-L12](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L3-L12) 这段说：MLIR 是一种编译器中间表示，**类似于**传统三地址 SSA 表示（如 LLVM IR、SIL），**但它把多面体循环优化的概念作为第一等公民引入**。这种混合设计既能表示/分析/变换高层的数据流图，也能处理面向高性能数据并行系统的目标相关代码。

注意两个关键词：「**类似**」（说明它继承了 SSA 的成熟经验）和「**多面体第一等公民**」（说明它的高层抽象能力，这正是单层 IR 做不到的）。

**② MLIR 的三种存在形态**——同一段稍后：

[docs/LangRef.md:L20-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L20-L24) 这段说：MLIR 设计为可在**三种形态**下使用——人类可读的文本形态（便于调试）、内存中的形态（便于程序化变换与分析）、紧凑的序列化形态（便于存储与传输）。三者描述**相同的语义内容**。

理解这一点很重要：你后续会大量接触的 `.mlir` 文本文件、内存里的 Operation 对象、以及 `.mlirbc` 字节码文件，本质上是「同一份 IR 的三种皮肤」。

**③ 「Multi-Level」这个名字的官方解释**——Rationale 文档：

[docs/Rationale/Rationale.md:L24-L27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L24-L27) 这段说：MLIR 这个名字可以理解为 "Multi-Level IR"（多层 IR）或 "Multi-dimensional Loop IR"（多维循环 IR）或 "Machine Learning IR"（机器学习 IR）或 "Mid Level IR"（中级 IR），**官方首选第一个**（Multi-Level IR）。

> 小提醒：很多初学者误以为 MLIR = Machine Learning IR（因为它在 AI 领域很火）。官方明确更倾向「多层 IR」这个解释——它强调的是**抽象层级**，而不是某个应用领域。

**④ 「多层」到底多在哪——Rationale 的关键区分**：

[docs/Rationale/Rationale.md:L45-L57](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L45-L57) 这段说：MLIR 是一个**多层 IR**，即它能表示从领域特定表示（如 HLO 或 TensorFlow 计算图）一直到机器层面的代码。它与既有「多面体实现」（如 LLVM 的 Polly）的关键区别在于：Polly 把多面体抽象**隔离地**用于一段固定的仿射循环里，而 MLIR 是**整体的、贯穿多层的**。

**⑤ 逐步降低（progressive lowering）的官方描述**：

[docs/Rationale/Rationale.md:L79-L96](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L79-L96) 这段说：MLIR 的设计允许**渐进式地降低到目标相关形态**。它既能做高层（循环嵌套、数据布局）变换，也能做一些传统后端 IR 才做的事（向专用向量指令映射、自动向量化、软件流水线）。

这正是前面流程图里「高层方言 → 中层方言 → LLVM 方言」逐层降低的依据。

#### 4.1.4 代码实践

**实践目标**：亲手从官方文档里提炼 MLIR 的定义，而不是听我转述。

**操作步骤**：

1. 打开 [docs/LangRef.md:L1-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L1-L24)，通读前 24 行。
2. 打开 [docs/Rationale/Rationale.md:L11-L57](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L11-L57)，重点看「Abstract」「Introduction and Motivation」两节。
3. 用**自己的话**写一段约 150 字的定义，必须包含三个要素：(a) MLIR 是什么；(b) 它「混合」了哪两类思想；(c) 为什么叫「多层」。

**需要观察的现象**：你会注意到 LangRef 和 Rationale 的开头几段几乎逐字相同（都强调「类似 SSA + 多面体第一等公民 + 提供降级框架」）。这不是偶然——它们是同一份设计意图的两次陈述。

**预期结果**：你写出的定义应当能回答「为什么不能直接用 LLVM IR 做这件事」。

> 待本地验证：本实践无需运行命令，是纯阅读任务。如果你无法用自己的话写出 (a)(b)(c) 三点，说明需要重读 Rationale 的 L29-L57。

#### 4.1.5 小练习与答案

**练习 1**：MLIR 的名字有四种可能解释，官方首选哪一个？为什么不是 "Machine Learning IR"？

**参考答案**：首选 **"Multi-Level IR"（多层 IR）**。因为 MLIR 强调的核心是「能在多个抽象层级上表示和变换代码」，这比「机器学习」这个具体应用领域更本质——机器学习只是它最显眼的应用之一，但它的设计目标远不止于此。见 [Rationale.md:L24-L27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L24-L27)。

**练习 2**：判断对错：「MLIR 是一种用来替代 LLVM IR 的新 IR。」并说明理由。

**参考答案**：**错**。MLIR 不是要「替代」LLVM IR，而是提供一个**可扩展的多层基础设施**。LLVM IR 反而是 MLIR 最常见的**最终目标**之一（经过 LLVM 方言翻译而成）。两者的定位不同：LLVM IR 是「一种具体的低级 IR」，MLIR 是「制造各种层级 IR 的框架」。

**练习 3**：MLIR 设计为可在「三种形态」下使用，请列出并各举一个用途。

**参考答案**：(1) 人类可读文本形态——编写 `.mlir` 测试用例、调试变换过程；(2) 内存形态——Pass 在程序内对 Operation 做分析与重写；(3) 紧凑序列化形态（字节码）——存储与跨进程/跨工具传输。见 [LangRef.md:L20-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L20-L24)。

---

### 4.2 MLIR 与 LLVM IR 的关系与差异

#### 4.2.1 概念说明

理解 MLIR，绕不开和 LLVM IR 的对比。两者**同宗同源**：MLIR 是 LLVM 项目的一部分，主动借鉴了 LLVM IR 在低层构造上的经验。但它们在「**可扩展性**」这个根本维度上分道扬镳。

一句话总结两者的关系：

> **LLVM IR 是一种「写死的」低级 IR；MLIR 是一个「可扩展的」IR 基础设施，LLVM IR 可以作为它的一个目标（target）。**

差异可以从四个维度看：

| 维度 | LLVM IR | MLIR |
| --- | --- | --- |
| 抽象层级 | 单一、偏底层 | 多层，从高层计算图到接近机器 |
| 操作集合 | 固定的指令集（add、load、call…） | **没有固定集合**，操作可被任意扩展（Dialect） |
| 结构 | 指令 + 基本块 + PHI 节点 | Operation + Region + Block，**层次化**，操作可嵌套区域 |
| 前端复用 | 各前端自建 AST→IR 设施 | 共享同一套可扩展基础设施 |

#### 4.2.2 核心流程

对比「可扩展性」时，可以这样想象两条流水线：

```
传统 LLVM 流水线（单层）：
  源码 ──▶ [前端自建分析与变换] ──▶ LLVM IR ──▶ 优化 ──▶ 机器码
                 ▲
                 └ 各前端重复造轮子，高层语义在到达 LLVM IR 前就丢了

MLIR 流水线（多层、共享基础设施）：
  源码 ──▶ 高层方言 IR ──▶ ... ──▶ LLVM 方言 ──▶ LLVM IR ──▶ 机器码
            └──── 共享同一套 Operation / Pass / 验证 / 打印基础设施 ────┘
```

在结构层面，MLIR 用 **block arguments（块参数）** 代替了 LLVM 的 **PHI 节点**。这是一种「表示能力等价、但工程上更省事」的选择——后面的小练习会让你说清它解决了 PHI 的哪些痛点。

在类型系统上，MLIR 也和 LLVM IR 做了相同的关键设计：**整数是无符号语义（signless）的**，符号由具体操作（如 `arith.divsi` 有符号除 vs `arith.divui` 无符号除）来解释。这种「类型不带符号、操作带符号」的设计正是 LLVM 2.0 引入并沿用至今的。

#### 4.2.3 源码精读

**① MLIR 与 LLVM IR 的「同源」关系**——LangRef 开头明确点名：

[docs/LangRef.md:L3-L7](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L3-L7) 这段说：MLIR **与传统的三地址 SSA 表示相似**（明确举例 LLVM IR 和 Swift 的 SIL），但**引入多面体优化的概念作为第一等公民**。这一定位决定了「同源但分层」的关系。

**② LLVM core IR 只是 MLIR 的「一种应用」**：

[docs/LangRef.md:L58-L66](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L58-L66) 这段说：MLIR 的一个显然应用就是**表示一个基于 SSA 的 IR（像 LLVM core IR）**，只要选合适的操作类型来定义 Module、Function、Branch 等。MLIR 自带一组方言恰好定义了这些结构。**但** MLIR 的目标是足够通用，还能表示语言前端的 AST、目标后端的指令、甚至高层次综合（HLS）里的电路。

这段极其关键：它说明 LLVM IR 风格的 SSA 只是 MLIR 能表达的**其中一种**东西，而不是 MLIR 的全部。

**③ 最清晰的「为什么需要可扩展性」论述**——Toy 教程第 2 章：

[docs/Tutorials/Toy/Ch-2.md:L8-L23](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/Toy/Ch-2.md#L8-L23) 这段说：其他编译器（如 LLVM）提供**固定的**预定义类型和（通常是低级 / 类 RISC 的）指令，前端必须自己做语言特定的类型检查、分析、变换后才 emit LLVM IR，**结果多个前端各自重新实现了一大堆基础设施**。MLIR 通过「**为可扩展性而设计**」来解决这个问题——它几乎没有预定义的指令（MLIR 术语叫 *operation*）或类型。

这是全仓库里把「MLIR 解决了什么」说得最直白的一段，建议反复读。

**④ block arguments 代替 PHI 节点——与 LLVM 的结构性差异**：

[docs/Rationale/Rationale.md:L170-L205](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L170-L205) 这段列出 MLIR 用块参数（block arguments）代替 LLVM PHI 节点的**五条理由**，例如：PHI 必须堆在块首、变换时要手动跳过；LLVM 还需要单独的函数参数节点；PHI 块的「原子执行」语义令人困惑且容易引入 bug；某些 unwind 块有成千上前驱导致线性扫描开销。块参数表示「把这些问题直接从设计上消除掉了」。

**⑤ 与 LLVM 共享的类型设计：signless 整数**：

[docs/Rationale/Rationale.md:L241-L273](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L241-L273) 这段说：builtin 整数类型**可选地带符号语义**，目的是满足不同方言（靠近源语言的想区分符号、靠近机器的想要无符号）。标准方言选了**无符号（signless）整数**，并明确指出「LLVM 用了同样的设计」——一个 `add` 不该因为操作数是 `sbyte` 还是 `ubyte` 就变成两条不同的指令。

#### 4.2.4 代码实践

**实践目标**：用一个具体的结构差异，体会「设计取舍」如何影响编译器工程。

**操作步骤**：

1. 打开 [docs/Rationale/Rationale.md:L170-L205](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L170-L205)。
2. 把「block arguments vs PHI 节点」的 5 条理由，整理成一张「痛点 → MLIR 如何消除」的对照表（草稿即可）。
3. 再打开 [docs/Tutorials/Toy/Ch-2.md:L8-L23](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/Toy/Ch-2.md#L8-L23)，找出一句最能体现「MLIR 与 LLVM IR 根本差异」的话并抄下来。

**需要观察的现象**：你会看到 MLIR 的设计理由几乎都是「**把复杂性从设计上消除掉**」的风格，而不是「打补丁」——这是它和很多实用主义 IR 的气质差异。

**预期结果**：你能用一句话向同事解释「为什么 MLIR 不用 PHI 节点」。

> 待本地验证：纯阅读任务，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：填空：「LLVM IR 提供的是 ____ 的指令集，而 MLIR 几乎没有 ____ 的指令（操作）或类型。」

**参考答案**：「**固定的**（predefined / fixed）」；「**预定义**」。依据 [docs/Tutorials/Toy/Ch-2.md:L8-L23](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/Toy/Ch-2.md#L8-L23)。

**练习 2**：MLIR 用 block arguments 取代了 LLVM 的 PHI 节点。请举出**两条**理由说明这种替代的好处。

**参考答案**（任选两条）：(1) PHI 节点必须堆在块首、变换时要手动跳过，块参数消除了这个麻烦；(2) LLVM 需要单独的「函数参数」节点，块参数让入口块参数直接充当函数参数；(3) LLVM 的 PHI 块「原子执行」语义令人困惑、容易引入 lost-copy 类 bug，块参数消除了这种困惑；(4) LLVM 某些块有成千上前驱，PHI 列表无序导致线性扫描开销，块参数消除该问题。依据 [Rationale.md:L170-L205](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L170-L205)。

**练习 3**：为什么 MLIR 和 LLVM 都选择「整数类型不带符号（signless）」？

**参考答案**：因为「有符号 add」和「无符号 add」在机器层面是同一条指令，让类型带符号会把同一种计算人为地变成两条不同的指令，还引入毫无意义的类型转换。把符号交给具体操作（如 `divsi`/`divui`）来解释，能让 IR 更简洁、编译器更简单。依据 [Rationale.md:L241-L273](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L241-L273)。

---

### 4.3 MLIR 的典型应用场景

#### 4.3.1 概念说明

MLIR 的「可扩展性」不是空谈，它通过**方言（Dialect）**机制落地：每个方言是一组操作 / 类型 / 属性的命名空间，专门服务某一抽象层级或某一应用领域。正因为方言可以任意扩展，MLIR 形成了一个庞大的「方言生态」，覆盖了从 AI 到硬件的多个真实场景。

LangRef 用一段话点明了 MLIR 操作能表达的跨度：从「函数定义、函数调用、缓冲区分配、缓冲区视图/切片、进程创建」这样的**高层概念**，到「目标无关算术、目标相关指令、配置寄存器、逻辑门」这样的**底层概念**——这些不同概念用不同的操作表示，而操作集合「可以被任意扩展」。

#### 4.3.2 核心流程

可以按「应用领域 → 代表方言」建立心智地图：

| 应用领域 | 代表方言（仓库内文档） | 这一层在做什么 |
| --- | --- | --- |
| **AI / 机器学习** | TOSA、Linalg、Vector | 把神经网络算子表示成可分析的张量计算，再降级 |
| **高性能循环 / 科学计算** | Affine、MemRef | 用多面体（仿射）模型做循环分块、交换、融合 |
| **GPU / 异构计算** | GPU、SPIR-V、NVVM | 面向 GPU/SPIR-V 的内核生成与映射 |
| **CPU / 最终代码生成** | LLVM（`llvm.*`）、Arith | 降到接近机器的层次，再翻译成 LLVM IR |
| **并行编程模型** | OpenACC、OpenMP | 把 pragma 风格的并行性落到 IR |
| **特定硬件** | ArmSME、AMDGPU 等 | 面向专用加速器 / 矩阵引擎的指令 |
| **语言前端 / 代码生成** | Func（函数）、Builtin（module 等）、emitc（生成 C 代码） | 提供模块、函数等通用骨架，或反向生成高级语言 |

整个生态的运转方式是：**前端把源语言翻译成某个高层方言 → 通过 lowering 在方言之间逐层下降 → 最终到达 LLVM 方言 → 翻译成 LLVM IR → 生成机器码**。每一层都可以插入通用的分析与变换（Pass），而这些变换能跨方言工作，靠的是 **Traits（特性）** 和 **Interfaces（接口）** 这套「让操作语义被抽象描述」的机制。

#### 4.3.3 源码精读

**① 操作能表达的跨度——从高层到逻辑门**：

[docs/LangRef.md:L40-L45](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L40-L45) 这段说：操作能表示**很多不同的概念**——从函数定义、调用、缓冲分配、切片，到目标无关算术、目标相关指令、配置寄存器，乃至**逻辑门**。这些概念用不同操作表示，而操作集合可以被**任意扩展**。

「逻辑门」这个词很有冲击力：它说明 MLIR 不止是「软件编译器」的 IR，连硬件高层次综合（HLS）都能装下。

**② 可扩展变换如何可能——Traits 与 Interfaces**：

[docs/LangRef.md:L47-L56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L47-L56) 这段说：MLIR 还提供一个**可扩展的变换框架**（用熟悉的 Pass 概念）。但「在任意操作上跑任意 pass」会带来巨大的组合爆炸（每个变换都得理解每个操作的语义）。MLIR 的解法是用 **Traits 和 Interfaces** 把操作语义**抽象地描述**出来，让变换能更**通用**地作用于操作。

这段是理解后续第 5–8 单元（Pass、模式重写、接口）的伏笔：方言再多，只要实现了通用接口，就能被通用变换处理。

**③ 官方提供两条上手路径——教程总索引**：

[docs/Tutorials/_index.md:L1-L5](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/_index.md#L1-L5) 这段说：本节包含多个教程——**Toy 教程**带你入门「如何使用 MLIR 基础设施」；**Transform 方言教程**带你入门「如何使用和扩展 MLIR 的 Transform 方言」。

这是给初学者的两条官方路线图。本手册后续也会大量参考 Toy 教程（它是最经典的新手示例）。

**④ 方言生态的真实证据——仓库内的方言文档与示例目录**：

MLIR 仓库 `docs/Dialects/` 下维护着大量方言的规范文档，例如 [Affine.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/Affine.md)、[Builtin.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/Builtin.md)、[GPU.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/GPU.md)、[LLVM.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/LLVM.md)、[TOSA.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/TOSA.md)、[SPIR-V.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/SPIR-V.md) 等。同时 `examples/` 下有 `toy`、`standalone`、`transform`、`minimal-opt` 等可编译的示例工程——这些就是方言生态「真实存在、可上手」的物证。第 9 单元会专门讲这些工具与示例。

#### 4.3.4 代码实践

**实践目标**：用仓库内的真实文件，验证「MLIR 方言生态覆盖多个领域」不是一句空话。

**操作步骤**：

1. 浏览 `docs/Dialects/` 目录（在仓库里 `ls docs/Dialects/`），数一数有多少个方言文档。
2. 任选**两个**不同领域的方言文档打开，例如：
   - AI 方向：[docs/Dialects/TOSA.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/TOSA.md)
   - 循环优化方向：[docs/Dialects/Affine.md](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Dialects/Affine.md)
3. 浏览 `examples/` 目录（`ls examples/`），确认存在 `toy`、`standalone`、`transform` 三个示例工程。

**需要观察的现象**：你会看到方言文档数量远超一般编译器的「内置指令文档」——这正是「可扩展」带来的直接结果：每多一个应用方向，就多一组方言文档，而核心基础设施不变。

**预期结果**：你能列出至少 4 个方言及其所属领域，并指出它们都共享同一套 MLIR 基础设施。

> 待本地验证：本实践用 `ls` 即可，不需要编译。如果你在仓库根目录 `mlir/` 下执行 `ls docs/Dialects/`，应能看到约 20 个方言相关的 `.md` / 目录条目。

#### 4.3.5 小练习与答案

**练习 1**：MLIR 操作能表达的「最低层」概念是什么？请举一个 LangRef 里提到的极端例子。

**参考答案**：可以低到**逻辑门（logic gates）**，也可以是目标相关指令、配置寄存器。这体现 MLIR 不局限于「软件 IR」。依据 [LangRef.md:L40-L45](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L40-L45)。

**练习 2**：「在任意操作上跑任意 pass」会带来什么问题？MLIR 用什么机制解决？

**参考答案**：会带来**组合爆炸**——每个变换都得理解每个操作的语义。MLIR 用 **Traits（特性）** 和 **Interfaces（接口）** 把操作语义**抽象描述**出来，让变换能更通用、跨方言地作用于操作。依据 [LangRef.md:L47-L56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L47-L56)。

**练习 3**：官方为初学者提供了哪两条教程路径？分别侧重什么？

**参考答案**：(1) **Toy 教程**——侧重「如何使用 MLIR 基础设施」从零搭一个玩具语言前端；(2) **Transform 方言教程**——侧重「如何用 IR 来描述对 IR 的变换」。依据 [docs/Tutorials/_index.md:L1-L5](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/_index.md#L1-L5)。

---

## 5. 综合实践

本讲是入门第一篇，综合实践是一个**贯穿三个模块的阅读 + 写作任务**（对应规格里的 practice_task），目的是把「多层 IR 的动机、与 LLVM IR 的差异、方言应用」串成你自己的理解。

**任务**：阅读以下两段官方文档后，用自己的话写一段 **约 200 字** 的中文总结：

- [docs/LangRef.md:L1-L24](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/LangRef.md#L1-L24)
- [docs/Rationale/Rationale.md:L11-L101](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Rationale/Rationale.md#L11-L101)

你的总结**必须**包含以下三点：

1. **MLIR 相比「单一 IR」解决了什么问题**（例如：高层语义过早丢失、前端各自造轮子、组合爆炸……至少一点）；
2. **举出至少两个现实中真实的方言应用方向**（可参考 4.3 的表格，例如 TOSA/Linalg 用于 AI、Affine 用于循环优化、SPIR-V/GPU 用于异构计算、LLVM 方言用于最终代码生成……）；
3. **一句话点出 MLIR 与 LLVM IR 的关系**（不是「替代」，而是「可扩展的多层基础设施，LLVM IR 是其目标之一」）。

**自检方法**：写完后，把总结拿给一个完全不懂编译器的朋友看。如果对方读完仍不知道「MLIR 到底干嘛的」，说明你的总结缺少第 1 点；如果对方以为 MLIR 只服务于 AI，说明第 2 点举例太窄；如果对方以为 MLIR 要淘汰 LLVM，说明第 3 点没写清楚。

> 待本地验证：这是写作型任务，不需要运行任何命令。建议把写好的总结存到自己的笔记里，学完第 2、3 单元后回看，你会发现自己当初的理解有哪些可以修正。

## 6. 本讲小结

- **MLIR = Multi-Level IR（多层中间表示）**，是 LLVM 项目下的可扩展编译器基础设施，官方首选「多层 IR」而非「机器学习 IR」这个解释。
- 它的**核心思想**是「不要一层 IR 表达一切」，而是用一套共享基础设施表示从高层计算图到接近机器的**多个抽象层级**，通过**逐步降低（progressive lowering）** 在层级间迁移。
- 它**混合了两类思想**：传统三地址 SSA（继承自 LLVM IR / SIL）+ 多面体循环优化（仿射映射作为第一等公民）。
- 它与 **LLVM IR** 同源但定位不同：LLVM IR 是一种「写死的」低级 IR，MLIR 是「可扩展的」框架，**LLVM IR 是 MLIR 的目标之一**而非被替代者。
- 它通过 **Dialect（方言）** 实现可扩展性，形成了覆盖 **AI（TOSA/Linalg）、循环优化（Affine）、异构计算（GPU/SPIR-V）、最终代码生成（LLVM 方言）** 等多领域的生态。
- 让「任意变换作用于任意操作」不失控的关键，是 **Traits 与 Interfaces** 把操作语义抽象化——这是后续 Pass / 模式重写 / 接口单元的伏笔。

## 7. 下一步学习建议

本讲建立了「MLIR 是什么、为什么需要它」的全局心智模型，但还没碰任何代码。建议按以下顺序继续：

1. **下一讲（u1-l2）源码目录结构与构建系统**：先认识 `include/mlir` 与 `lib` 的镜像结构、CMake 构建入口，知道「代码长在哪、怎么组织」。
2. **再下一讲（u1-l3）mlir-opt 工具入口**：动手跑起第一个 MLIR 程序，把本讲讲到的「文本 IR」变成可以执行的东西。
3. **并行阅读**：强烈建议同步开始官方 **Toy 教程**（[docs/Tutorials/Toy/](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/mlir/docs/Tutorials/Toy/Ch-2.md#L8-L23)），它是本手册第 1–3 单元最好的配套实战。
4. **暂不深入**：第 2 单元（Operation/Value/Block/Region 数据结构）需要先建立目录与运行认知，建议等 u1-l2、u1-l3 学完再进入，避免一上来就被 C++ 内存布局细节劝退。
