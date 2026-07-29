# 项目概览：tiny-gpu 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，不涉及任何具体电路细节。读完后你应当能够：

- 说清楚 **tiny-gpu 的定位**：它是一个用 Verilog 写的「教学型」GPU，目标是帮人从零理解 GPU 硬件，而不是做一个能打游戏的显卡。
- 区分两个容易被混淆的概念：**GPU 编程（软件层）** 与 **GPU 硬件实现（电路层）**。
- 记住 tiny-gpu 聚焦的**三大学习主题**：架构（Architecture）、并行化（Parallelization）、内存（Memory）。
- 理解项目为「便于学习」做了哪些**简化**，以及这些简化分别牺牲了什么。

本讲几乎全部依据 `README.md`，后续每一讲才会真正进入 `.sv` 源码。

## 2. 前置知识

在开始之前，你只需要对以下几个名词有一个**模糊的印象**即可，本讲会用通俗语言再解释一遍：

- **CPU（中央处理器）**：电脑里「什么都干一点」的通用处理器，擅长复杂的串行逻辑。
- **GPU（图形处理器）**：原本为画图而生、现在也广泛用于 AI/科学计算的处理器，特点是「成千上万个简单核心同时干活」。
- **Verilog / SystemVerilog**：一种**硬件描述语言（HDL）**。你用它写的不是「程序」，而是「电路的样子」——寄存器、连线、状态机。写完之后要用仿真器（如 iverilog）跑起来验证。
- **内核（kernel）**：在 GPU 语境下，kernel 指「运行在 GPU 上的一段代码」，不要和操作系统里的 kernel 混淆。

如果你对「写代码」和「画电路」的区别还不清楚也没关系，这正是本讲想帮你建立的第一个直觉。

## 3. 本讲源码地图

本讲只读一个文件，但它信息量极大：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md) | 项目的「总纲」。它同时承担了项目动机、架构图、指令集（ISA）、内核示例、仿真说明、未来计划等多种角色，是本讲唯一也是最重要的依据。 |

> 小提示：tiny-gpu 项目没有单独的 `docs/` 文档站点，几乎所有文档都浓缩在 `README.md` 里，外加 `docs/images/` 下的几张架构图。因此**读懂 README 是学习这个项目的第一关**。

后续讲义才会逐个打开 `src/` 下的 12 个 SystemVerilog 文件。这里先给你一个全景印象（本讲不需要记住细节）：

```
src/
├── gpu.sv          顶层模块，把所有部件连起来
├── dcr.sv          设备控制寄存器（存 thread_count）
├── dispatch.sv     线程派发器
├── controller.sv   内存控制器（带宽仲裁）
├── core.sv         计算核心
├── scheduler.sv    核心内的指令调度状态机
├── fetcher.sv      取指单元
├── decoder.sv      译码单元
├── alu.sv          算术逻辑单元
├── lsu.sv          访存单元（Load/Store）
├── registers.sv    寄存器堆
└── pc.sv           程序计数器
```

这 12 个文件正好对应 README 反复强调的「<15 files of fully documented Verilog」。

## 4. 核心概念与源码讲解

### 4.1 项目背景与动机：为什么 GPU 硬件资料这么稀缺

#### 4.1.1 概念说明

学过计算机组成的人多半有过这种体验：想搞懂一颗 CPU 从架构到控制信号是怎么运转的，网上有成吨的教材、开源核心（比如 RISC-V 生态）、教学动画可以挑。但换成 GPU，画风就完全变了——**几乎找不到一份「从零讲清楚 GPU 硬件」的资料**。

tiny-gpu 的作者正是被这种落差刺痛，才动手写了这个项目。它的全部动机可以浓缩成一句话：**把 GPU 从「黑盒商业产品」变成「15 个文件就能读懂的教具」**。

#### 4.1.2 核心流程

作者在 README 里给出了一个清晰的「问题→现状→对策」推理链：

1. **问题**：想学 GPU 硬件怎么工作。
2. **现状**：GPU 市场竞争激烈，现代架构的低层细节几乎都是各家厂商的商业机密（proprietary）；网上关于「GPU *编程*」的资料很多，关于「GPU *硬件*」的资料几乎没有。
3. **唯一的退路**：去啃开源 GPU（Miaow、VeriGPU），但它们目标是「功能完整、能跑」，所以极其复杂，初学者很难看懂。
4. **对策**：自己做一个**为学习而优化**的最小 GPU，也就是 tiny-gpu。

#### 4.1.3 源码精读

README 开篇就把这套动机摆得很明白：

[README.md:25-37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L25-L37) —— 「Overview」整段。注意它把「CPU 学习资料多」和「GPU 学习资料几乎没有」做了直接对比，并点出商业机密是根本原因。

[README.md:35](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L35) —— 提到了两个真实的开源 GPU 项目 Miaow 和 VeriGPU，并解释它们为什么「不适合初学者」：因为它们追求功能完整，复杂度太高。这正是 tiny-gpu 想要避开的反面教材。

[README.md:37](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L37) —— 一句 "This is why I built `tiny-gpu`!" 收束整个动机。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目的是让你亲手确认「动机」不是空话：

1. **实践目标**：验证 tiny-gpu 真的比 Miaow/VeriGPU 小得多。
2. **操作步骤**：
   - 在本仓库根目录用 `git ls-files src/` 列出全部硬件源码文件，数一数有几个。
   - 打开 [Miaow 仓库](https://github.com/VerticalResearchGroup/miaow) 与 [VeriGPU 仓库](https://github.com/hughperkins/VeriGPU/tree/main) 的页面，粗略感受它们的目录规模。
3. **需要观察的现象**：tiny-gpu 的 `src/` 只有 12 个 `.sv` 文件；而 Miaow/VeriGPU 的源码树通常有几十到上百个文件、多层目录。
4. **预期结果**：你会直观地体会到 README 说的「these projects … are quite complex」是什么量级的复杂。

#### 4.1.5 小练习与答案

- **练习 1**：README 说现代 GPU 的低层细节为什么大多是商业机密？
  - **答案**：因为 GPU 市场竞争极其激烈，厂商把架构细节视为核心竞争力，不对外公开。
- **练习 2**：为什么作者认为直接读 Miaow/VeriGPU 对初学者「challenging」？
  - **答案**：这两个项目目标是「功能完整、能真正运行」，因此复杂度很高，初学者还没理解基本原理就被细节淹没。

---

### 4.2 GPU 编程 vs GPU 硬件：先分清两个世界

#### 4.2.1 概念说明

很多人说「我学过 GPU」，其实指的是**用 CUDA / OpenCL 写程序跑在 GPU 上**——这属于**GPU 编程（软件层）**。而 tiny-gpu 关心的是另一个世界：**GPU 本身这块芯片里，电路是怎么搭出来的**——这属于**GPU 硬件实现（电路层）**。

打个比方：

- **GPU 编程** 像是「会开汽车」——你知道踩油门、打方向盘能做什么。
- **GPU 硬件** 像是「会造发动机」——你知道气缸、火花塞、变速箱怎么协作。

这两件事互相有关，但绝不是一回事。tiny-gpu 选择的是后者，并且只用最少的部件把「发动机」讲清楚。

#### 4.2.2 核心流程

README 在 Overview 里用一句话点破了这种资源失衡：

- 「lots of resources to learn about **GPU programming**」（编程资料很多）
- 「almost nothing available to learn about **how GPU's work at a hardware level**」（硬件资料几乎没有）

因此本项目的学习姿态是：**先放下 CUDA，先看电路**。读这套手册时，请把脑子切换到「连线、寄存器、时钟周期」的频道，而不是「线程块、流式多处理器 API」的频道——那些概念会在读到具体电路时，再被「翻译」成硬件语言。

#### 4.2.3 源码精读

[README.md:33](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L33) —— 原文 "While there are lots of resources to learn about GPU programming, there's almost nothing available to learn about how GPU's work at a hardware level." 这一句是整本手册的「立项目标」，值得画线。

[README.md:3](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L3) —— 开篇定位 "A minimal GPU implementation in Verilog optimized for learning about how GPUs work from the ground up." 注意三个关键词：**Verilog**（不是 CUDA）、**learning**（不是 production）、**from the ground up**（从底层往上）。

#### 4.2.4 代码实践

1. **实践目标**：在阅读后续讲义前，先给自己做一次「频道校准」。
2. **操作步骤**：找一张纸，中间画一条竖线。左边写下你已知的「GPU 编程」概念（如线程、block、共享内存），右边留空。当你读到本手册 Unit 2~5 讲到 `dispatch.sv`、`core.sv` 时，把对应的**硬件实现**填到右边（例如「block」→「dispatcher 切分出来的线程组」）。
3. **需要观察的现象**：你会发现自己左边能写很多，右边一开始几乎空白——这正是 tiny-gpu 要补上的那块。
4. **预期结果**：读完整本手册后，左右两边能大致一一对应。

#### 4.2.5 小练习与答案

- **练习 1**：用一句话区分「GPU 编程」和「GPU 硬件」。
  - **答案**：GPU 编程是写**运行在 GPU 上的代码**；GPU 硬件是设计**GPU 这块芯片本身的电路**。
- **练习 2**：tiny-gpu 用的是哪种语言？为什么不用 C/C++？
  - **答案**：用 Verilog/SystemVerilog，因为它要描述的是**电路**而不是程序；C/C++ 描述的是顺序执行的指令，无法表达并行的硬件连线。

---

### 4.3 三大学习主题：架构 / 并行化 / 内存

#### 4.3.1 概念说明

README 明确把项目的探索方向收敛成三个主题。这三个主题其实就是 GPU 区别于 CPU 的三块「命门」，也是整本手册后续单元的隐含主线：

1. **Architecture（架构）**：一颗 GPU 由哪些大块组成？谁是大脑、谁是手脚、谁管对外通信？
2. **Parallelization（并行化）**：GPU 最招牌的 **SIMD**（单指令多数据）模型，在硬件里到底怎么实现？怎么让成百上千个线程「同时」执行同一条指令？
3. **Memory（内存）**：那么多核心同时要数据，而外部内存带宽是有限的，GPU 怎么在这个瓶颈下活下来？

> 术语解释：**SIMD**（Single Instruction, Multiple Data）= 所有的线程在同一时刻执行**同一条指令**，只是各自处理**不同的数据**。比如「把寄存器相加」这条指令，8 个线程同时执行，但各自加的是自己寄存器里的数。这是 GPU 并行能力的根基。

#### 4.3.2 核心流程

三个主题并非平铺，而是有递进关系：

- 先有**架构**（把 GPU 拆成 DCR、dispatcher、core、memory controller 等部件）→
- 架构里的 core 负责**并行化**（一个 core 带多个线程，用 SIMD 执行）→
- 并行化一旦铺开，**内存**带宽立刻成为瓶颈，于是需要 memory controller 去节流。

本手册的单元划分几乎是按这条链走的：Unit 2 讲顶层架构，Unit 4 讲 core 内的并行执行流水线，Unit 3 和 Unit 7 讲内存子系统与优化。

#### 4.3.3 源码精读

[README.md:49-53](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L49-L53) —— 三个主题的原话：Architecture / Parallelization / Memory。这是后续所有讲义的「目录骨架」。

[README.md:153](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L153) —— 在 Register Files 一节提到 "the same-instruction multiple-data (SIMD) pattern"，直接把并行化主题和具体硬件（寄存器堆）挂上钩。

[README.md:115-117](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L115-L117) —— Memory Controllers 一节解释了为什么需要控制器：外部内存带宽固定，但 core 发出的请求可能远超带宽，所以必须 throttle（节流）。这是「内存」主题的核心矛盾。

#### 4.3.4 代码实践

1. **实践目标**：把三个抽象主题映射到具体的源码文件，提前建立全局坐标。
2. **操作步骤**：对照第 3 节的 `src/` 文件树，给每个主题挑出「最相关的 2~3 个文件」：
   - Architecture → `gpu.sv`、`dcr.sv`、`dispatch.sv`
   - Parallelization → `core.sv`、`scheduler.sv`、`registers.sv`
   - Memory → `controller.sv`、`lsu.sv`
3. **需要观察的现象**：你会发现有些文件（如 `core.sv`）同时服务多个主题——这很正常，因为核心本身就是「并行 + 访存」交汇的地方。
4. **预期结果**：你能在不打开文件的前提下，说出每个主题大致由哪些模块负责。后续讲义会逐个兑现这些猜测。

#### 4.3.5 小练习与答案

- **练习 1**：SIMD 的四个字母分别代表什么？它和「多线程」的最大区别是什么？
  - **答案**：Single Instruction, Multiple Data（单指令多数据）。区别在于：SIMD 要求所有线程**同一时刻执行同一条指令**，而普通多线程的各线程可以跑各自不同的代码。
- **练习 2**：为什么「内存」会成为 GPU 的独立主题，而 CPU 教材里通常不那么强调？
  - **答案**：因为 GPU 的并行核心数量极多，对内存带宽的需求远超 CPU，外部内存很容易成为瓶颈，所以「如何在有限带宽下喂饱所有核心」成了 GPU 设计的一等难题。

---

### 4.4 项目范围与简化原则：哪些被砍掉了

#### 4.4.1 概念说明

tiny-gpu 之所以「tiny」，不是因为它功能弱，而是因为作者**主动砍掉了一切对「理解原理」非必要的复杂度**。理解这些「被砍掉的东西」，比理解它「有什么」同样重要——因为后者（缓存、流水线、warp 调度、分支分歧……）恰恰是真实 GPU 花大力气优化的地方，也是本手册 Unit 7 的内容。

关键认识：**tiny-gpu 故意做出了一些「天真（naive）」的假设，这些假设在某些内核下会算错**。这不是 bug，而是教学取舍。

#### 4.4.2 核心流程

README 列出的简化点可以分成两类：

**A. 结构上的简化（少做点事）**
- 一次只执行**一个 kernel**。
- 数据内存和程序内存**物理分离**（真实 GPU 通常统一编址）。
- 数据内存只有 8 位地址（256 行）、8 位数据（每行 < 256）。
- **Cache 标注为 WIP（Work In Progress）**，目前基本是占位。

**B. 行为上的「天真假设」（做了，但假设很简单）**
- **PC 收敛假设**：所有线程在每条指令后都收敛到同一个 PC，即不考虑**分支分歧（branch divergence）**。
- **无流水线（no pipelining）**：一条指令执行完才开始下一条。
- **无 warp 调度**：一个 core 一次只处理一个 block，且 block 内线程同步串行推进。

> 术语解释：
> - **分支分歧（branch divergence）**：当同在一个 block 里的不同线程，因为数据不同而走上不同的 `if/else` 分支、跳到不同的 PC，它们就「分歧」了。真实 GPU 要花额外硬件管理这种分歧与重新汇合；tiny-gpu 假装它不会发生。
> - **流水线（pipelining）**：像工厂流水线一样，让多条指令重叠执行以提高吞吐；tiny-gpu 不做。
> - **Warp 调度**：把一个 block 再切成多个「warp」小批次，在一个 warp 等内存时去跑另一个 warp，掩盖延迟；tiny-gpu 也不做。

#### 4.4.3 源码精读

[README.md:5](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L5) —— "Built with <15 files of fully documented Verilog …" 这是对「简化原则」最直接的承诺：用文件数量自我设限。（实际 `src/` 有 12 个 `.sv` 文件，确实 <15。）

[README.md:121-125](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L121-L125) —— Cache 一节标题就写着 "Cache (WIP)"，正文解释了缓存的意义，但坦承目前还没真正实现。这是「结构简化」的典型例子。

[README.md:177-179](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L177-L179) —— 「PC 收敛假设」的原话："tiny-gpu assumes that all threads 'converge' to the same program counter … which is a naive assumption for the sake of simplicity." 并紧接着引入 branch divergence 概念。这是「行为简化」最重要的例子。

[README.md:334-378](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L334-L378) —— 「Advanced Functionality」整节，逐项列出 tiny-gpu **没做**的优化：多层缓存与共享内存、内存合并（coalescing）、流水线、warp 调度、分支分歧、同步与 barrier。这一节是 Unit 7 的总纲。

[README.md:380-392](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L380-L392) —— 「Next Steps」用一个 todo 列表把未来的改进点公开列出来（cache、branch divergence、memory coalescing、pipelining……），等于把「简化清单」直接写成了「待办清单」。

#### 4.4.4 代码实践

1. **实践目标**：亲手确认哪些「真实 GPU 特性」在 tiny-gpu 里缺席。
2. **操作步骤**：打开 README 的 [Advanced Functionality 小节](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L334-L378)，把它列出的 6 项高级功能抄成一张表，第二列写「tiny-gpu 是否实现」，第三列写「一句话代价」。
3. **需要观察的现象**：你会看到 6 项里大多数是「未实现」。
4. **预期结果**：得到一张类似下表的「简化清单」：

   | 真实 GPU 特性 | tiny-gpu 是否实现 | 代价 |
   |---|---|---|
   | 多层缓存 + 共享内存 | 否（仅一层 cache，且 WIP） | 频繁访问全局内存，慢 |
   | 内存合并（coalescing） | 否 | 相邻地址被逐个请求，浪费带宽 |
   | 流水线（pipelining） | 否 | 一条指令一拍，资源闲置 |
   | Warp 调度 | 否 | 等内存时整个 core 空转 |
   | 分支分歧 | 否（PC 收敛假设） | 某些内核会算错 |
   | 同步 / barrier | 否 | 线程间无法安全交换数据 |

#### 4.4.5 小练习与答案

- **练习 1**：tiny-gpu 假设「所有线程每条指令后都收敛到同一个 PC」。请举一个会让这个假设失效的内核场景。
  - **答案**：一个含 `if (threadIdx % 2 == 0) 走 A 分支 else 走 B 分支` 的内核——偶数线程和奇数线程会走向不同 PC，发生分支分歧，tiny-gpu 无法正确处理。
- **练习 2**：为什么作者宁可让某些内核算错，也要保留「PC 收敛假设」？
  - **答案**：因为去掉这个假设就要引入分支栈、掩码、汇合点检测等大量硬件，会破坏「<15 文件、能读懂」的教学目标。教学取舍优先于功能完备。

---

## 5. 综合实践

**任务：写一份「tiny-gpu 简化清单」短文（这是本讲的核心实践，对应规格中的实践任务）。**

1. **实践目标**：用自己的语言，把 tiny-gpu 相对真实开源 GPU（Miaow / VeriGPU）做的简化整理出来，并解释**每一条简化为什么反而有助于学习**。
2. **操作步骤**：
   - 重读 README 的 [Overview](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L25-L55)、[Advanced Functionality](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L334-L378)、[Next Steps](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md#L380-L392) 三段。
   - 至少列出 **3 点**简化（提示：可从「一次性单 kernel」「PC 收敛 / 无分支分歧」「无流水线」「无 warp 调度」「cache 仅有占位」「数据/程序内存分离且容量很小」中选）。
   - 对每一点写两句话：① 它砍掉了什么；② 为什么砍掉它之后，初学者反而更容易看懂 GPU。
3. **需要观察的现象**：你会发现这些简化几乎都对应 README「Advanced Functionality」里的一条——也就是说，**「现在没有的」恰好是「将来要学的」**。
4. **预期结果**：一篇 200~400 字的短文。把它保存下来，等读完 Unit 7（架构取舍与扩展方向）后再回来对照，你会发现自己当初的理解被源码印证了多少。

> 示例答案要点（供对照，不要照抄）：
> - **简化点 1：一次只跑一个 kernel**。真实 GPU 要支持多任务上下文切换、虚拟内存、调度策略，这些会淹没「GPU 长什么样」的主线；砍掉后，顶层 `gpu.sv` 只需关心一次启动的时序。
> - **简化点 2：PC 收敛假设（无分支分歧）**。真实 GPU 要维护分支栈与汇合；砍掉后，scheduler 可以假设所有线程同步前进，状态机大幅简化，便于读者抓住「取指→译码→执行」主干。
> - **简化点 3：cache 仅占位、内存只有 256 行**。真实 GPU 的多级缓存与一致性协议极其复杂；砍掉后，读者可以直接看清「核心→控制器→外部内存」的最短数据通路，不被命中率/替换策略干扰。

## 6. 本讲小结

- tiny-gpu 是一个**用 Verilog 写、为学习而优化**的最小 GPU，目标是填补「GPU 硬件学习资料稀缺」的空白。
- 要分清两个世界：**GPU 编程（软件）资料很多，GPU 硬件（电路）资料几乎没有**——本项目专攻后者。
- 项目的三大学习主题是 **架构、并行化（SIMD）、内存**，它们也是本手册后续单元的主线。
- 项目靠**主动简化**保持可读：一次单 kernel、PC 收敛、无流水线、无 warp 调度、cache 占位。
- 这些「被砍掉的东西」（分支分歧、coalescing、pipelining……）恰恰是真实 GPU 的优化重点，会在 Unit 7 专门讨论。
- 整个项目核心只有 12 个 `.sv` 文件，且几乎所有文档都浓缩在 `README.md` 里。

## 7. 下一步学习建议

下一讲 **u1-l2《仓库结构与源码地图》** 会带你打开 `src/` 和 `test/`，把 README 里的架构图和真实的 `.sv` / `.py` 文件一一对应起来，建立「源码地图」。

在那之前，建议你：

- 再通读一遍 [README.md](https://github.com/adam-maj/tiny-gpu/blob/02b6c2ce223f606051a6d3a35ca942fbb1dffde2/README.md)，重点看 Architecture、ISA、Execution 三个小节，先有个模糊的整体印象即可，细节后续会逐讲拆解。
- 留意 README 引用的几张图：`docs/images/gpu.png`（GPU 顶层）、`docs/images/core.png`（core 内部）、`docs/images/isa.png`（指令集），它们会在后续讲义反复出现。
- 如果你对 Verilog 完全陌生，可以先花 20 分钟了解「模块、端口、always 块、寄存器 vs 连线」这几个概念，这样下一讲打开 `gpu.sv` 时不会一头雾水。
