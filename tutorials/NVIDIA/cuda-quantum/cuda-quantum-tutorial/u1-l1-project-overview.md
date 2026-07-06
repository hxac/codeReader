# CUDA-Q 是什么：项目定位与架构总览

> 本讲是整个学习手册的第一篇。读完后你不需要会写量子程序，但你会知道 CUDA-Q 在做什么、仓库里每一块代码大致负责什么，以及后面每一篇讲义要带你去看的地方。

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 **CUDA-Q 是什么**，以及它解决的是哪一类问题。
- 理解 **混合量子-经典（hybrid quantum-classical）编程模型** 的核心理念：CPU、GPU、QPU 协同工作。
- 在仓库里指认 **四大子系统**（MLIR 编译器、C++ 运行时、Python 前端、Realtime 实时子系统）各自所在的目录与职责。
- 听懂后续讲义会反复出现的几个关键名词：**Quake、CC、QIR、nvq++**。
- 画出「一段 C++ 源码如何变成一个可执行程序」的整体数据流，并标注每个阶段发生在哪个子系统。

## 2. 前置知识

本讲是面向零基础读者的「项目总览」，不要求你懂量子力学，也不要求你会写 MLIR。下面几个通俗概念足够支撑你读下去：

- **量子比特（qubit）**：经典比特只能是 0 或 1；量子比特可以处在 0 和 1 的「叠加」上。一台量子计算机（**QPU**）就是在这些比特上施加「量子门」、再做「测量」的设备。
- **CPU / GPU**：你已经熟悉。CPU 擅长复杂控制流，GPU 擅长大规模并行数值计算。
- **混合计算**：很多量子算法不是「一次性扔给 QPU 跑完」，而是「QPU 跑一小段量子线路 → CPU/GPU 处理经典结果 → 决定下一步线路 → 再交给 QPU」。这种「量子-经典交替」的循环就是 hybrid。
- **编译器 / IR**：把人写的源码翻译成机器能跑的代码。中间会经过一种「中间表示（Intermediate Representation, IR）」。CUDA-Q 用的是基于 MLIR 的多层 IR，本讲只需要你记住「源码 → 多层中间表示 → 可执行文件」这条主线。

一句话：**经典计算机负责思考和调度，QPU 负责量子加速，CUDA-Q 把两者粘在一起。**

## 3. 本讲源码地图

本讲只读三份「门户型」文档，它们是理解整个仓库的入口：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/README.md) | 仓库首页，给出 CUDA-Q 的一句话定位、安装与文档入口。 |
| [Overview.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md) | **本讲最重要的文件**，逐目录讲解代码地图，并描述 nvq++ 编译流程。 |
| [llms.txt](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/llms.txt) | 给大模型/读者用的文档索引，浓缩了 CUDA-Q 的定位与文档结构。 |

此外，本讲会顺带「远远地指一下」四个子系统的代表文件，帮你建立目录直觉，但不会深入（那是后续讲义的任务）：

- 编译器：[cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td)、[cudaq/include/cudaq/Optimizer/Dialect/CC/CCOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/CC/CCOps.td)
- 运行时：[runtime/cudaq.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq.h)、[runtime/nvqir/NVQIR.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/nvqir/NVQIR.cpp)
- Python 前端：[python/cudaq/__init__.py](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/python/cudaq/__init__.py)
- Realtime：[realtime/README.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/realtime/README.md)

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目定位与混合量子-经典模型**——CUDA-Q 到底是什么、为什么需要它。
2. **仓库四大子系统鸟瞰**——四大目录各管一摊什么。
3. **关键名词：Quake、CC、QIR、nvq++**——后续讲义的高频词，先建立直觉。

### 4.1 项目定位与混合量子-经典模型

#### 4.1.1 概念说明

CUDA-Q 的官方一句话定位写在 README 里：

> The CUDA-Q Platform for hybrid quantum-classical computers enables integration and programming of quantum processing units (QPUs), GPUs, and CPUs in one system.

翻译过来：**CUDA-Q 是一个面向「混合量子-经典计算机」的平台，让 QPU、GPU、CPU 能在一个系统里被统一编程和调度。**

为什么这件事需要一个专门的平台？因为真实量子算法的执行形态是这样的：

- 量子线路每次能跑的时间很短（相干时间有限），不能一口气跑完整个算法。
- 大量「经典工作」必须穿插在量子执行之间：比如根据上一次测量结果决定下一次要施加什么门、计算梯度、更新参数等。
- 这意味着一个量子程序天然是「量子 + 经典 + 调度」三种逻辑交织的。

如果每一种硬件（CPU 上的某个模拟器、某厂商的 GPU、某厂商的 QPU）都各自一套编程方式，开发者会被「硬件细节」淹没。CUDA-Q 的目标就是把这些差异藏到一层统一的编程模型和编译器背后：**你用同一份 C++ 或 Python 代码描述算法，由 CUDA-Q 决定怎么把它分发到 CPU/GPU/QPU 上去。**

llms.txt 把这点说得更凝练：

> CUDA-Q … offers a hybrid programming model designed for a setting where CPUs, GPUs, and QPUs work together. CUDA-Q contains support for programming in Python and in C++.

#### 4.1.2 核心流程

一个典型的混合量子算法，其执行循环可以用下面这段伪代码描述：

```text
初始化经典参数 θ
重复若干轮（或直到收敛）：
    ① 量子：在 QPU（或模拟器）上跑一段参数化线路 U(θ)，做采样或测量
    ② 经典：把测量结果送回 CPU/GPU，计算目标函数 f(θ) 与梯度 ∇f(θ)
    ③ 经典：用优化器更新 θ ← θ − η·∇f(θ)
返回最优 θ 与目标函数值
```

其中步骤 ① 是「量子」的，步骤 ②③ 是「经典」的，三者必须低开销地来回切换。CUDA-Q 的整个设计——从语言、到编译器、到运行时——都是为了让这个切换尽可能顺滑。

这里出现的两个基本量，正是后续讲义会反复用到的：

- **采样概率**：对一个量子态 \(|\psi\rangle\)，测量得到经典结果 \(x\) 的概率为

  \[
  p(x) = |\langle x \mid \psi \rangle|^2
  \]

- **期望值**：对可观测量 \(H = \sum_i c_i P_i\)（\(P_i\) 是某些 Pauli 串），其期望值为

  \[
  \langle H \rangle = \sum_i c_i \langle \psi \mid P_i \mid \psi \rangle
  \]

  你不需要现在就懂它的物理含义，只要记住：**这一步既要在「量子侧」做大量线路测量，又要在「经典侧」做加权求和与优化**——这正是「混合」二字的由来。

#### 4.1.3 源码精读

CUDA-Q 的一句话定位出现在 README 的开篇：

[README.md:L22-L27](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/README.md#L22-L27) —— 说明仓库内容包含 `nvq++` 编译器、CUDA-Q 运行时，以及一组内置的 CPU/GPU 后端，用于快速开发和测试。

> 这一句同时点出了仓库的三大交付物：**编译器（nvq++）、运行时（runtime）、后端（backends）**。后面你会看到它们分别落在不同的目录里。

更凝练的「混合模型」表述在 llms.txt：

[llms.txt:L1-L3](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/llms.txt#L1-L3) —— 把 CUDA-Q 概括为「让 CPU、GPU、QPU 协同工作的混合编程模型」，并强调同时支持 Python 和 C++。

Overview.md 的「鸟瞰（Bird's Eye View）」段落则给出了 CUDA-Q 最本质的技术定位：

[Overview.md:L9-L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L9-L18) —— CUDA-Q 首要是一个「用现代 C++ 表达的异质量子-经典编程模型」，并提供一个基于 MLIR 的编译器，把 Clang 生成的 C++ AST 映射到量子/经典 MLIR 方言，再降低为符合 QIR 规范的 LLVM IR。

> 这一段非常关键：它告诉你 CUDA-Q **本质上是一个「编程模型 + 编译器」项目**，而不是「某一个模拟器」或「某一个 QPU 的驱动」。模拟器和 QPU 都是它「能瞄准的目标」之一。

#### 4.1.4 代码实践

这是一个「源码阅读型」实践，目标是让你亲手确认 CUDA-Q 的定位描述。

1. **实践目标**：从三份门户文档中提炼 CUDA-Q 的「一句话定位」，并找到它强调的「协同三类硬件」表述。
2. **操作步骤**：
   - 打开 [README.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/README.md)，找到包含 `QPUs`, `GPUs`, and `CPUs` 的那一句。
   - 打开 [llms.txt](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/llms.txt)，确认它同样提到了三种硬件协同。
   - 打开 [Overview.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md)，阅读「Bird's Eye View」前两段。
3. **需要观察的现象**：三份文档对「混合」「协同」「编程模型」这几个词的用法是否一致；它们各自侧重哪一面（README 偏交付物，llms.txt 偏能力，Overview 偏技术原理）。
4. **预期结果**：你会得到一句自己的话，例如「CUDA-Q 是一个用 C++/Python 描述混合量子-经典算法、并通过 MLIR 编译器把它们分发到 CPU/GPU/QPU 的平台」。
5. 如果你想进一步验证：在仓库根目录执行 `git log --oneline -5`，看看最近的提交主题（如 `Enable available optimizations for FTQC/NISQ targets`），你会发现维护者也正是在围绕「不同目标硬件」做工作。

> 待本地验证：`git log` 的具体提交信息会随时间变化，本讲引用的是当前 HEAD（`61face2b9a`）下的快照。

#### 4.1.5 小练习与答案

**练习 1**：CUDA-Q 主要解决「让量子计算机变快」的问题，还是「让 CPU/GPU/QPU 被统一编程」的问题？

> **答案**：后者。CUDA-Q 的核心是混合编程模型与编译器，目标是「统一编程与调度」，而不是提升某一台 QPU 的物理速度。

**练习 2**：在 4.1.2 的伪代码循环里，哪几步是「经典」的？为什么这些经典步骤常常被忽视却又决定整体性能？

> **答案**：步骤 ②（计算目标函数与梯度）和 ③（更新参数）是经典的。它们常常决定整体性能，是因为量子测量每轮的数据要被搬运到经典侧、再做求和与优化；如果经典侧调度和数据搬运开销大，量子的「加速」就会被抵消。这正是 CUDA-Q 强调「低开销混合」的原因。

---

### 4.2 仓库四大子系统鸟瞰

#### 4.2.1 概念说明

CUDA-Q 仓库体量很大，直接扎进去很容易迷路。最有效的办法是先把它分成四个职责清晰的大块。结合目录结构和 Overview.md 的描述，CUDA-Q 由四大子系统组成：

| 子系统 | 主目录 | 一句话职责 |
| --- | --- | --- |
| **① MLIR 编译器**（nvq++ / Quake / CC） | `cudaq/` | 把 C++ 内核源码翻译成量子中间表示（Quake/CC），优化后再降低到 QIR/LLVM。 |
| **② C++ 运行时**（含 nvqir 模拟器） | `runtime/` | 提供量子类型、算法原语（sample/observe/evolve）、平台抽象，以及真正执行量子门的模拟器后端。 |
| **③ Python 前端** | `python/` | 让用户用 Python 写内核，复用同一套运行时与编译器。 |
| **④ Realtime 实时子系统** | `realtime/` | 把 GPU 算力与量子控制硬件（FPGA）紧耦合，提供低延迟（微秒级）的实时协处理与网络栈。 |

> 注意：C++ 运行时是「公共底盘」，Python 前端最终也调用它；编译器负责「造」可执行代码；Realtime 是面向物理控制的独立子系统。这就是为什么后面很多讲义都依赖 `runtime/`。

#### 4.2.2 核心流程

四个子系统之间的协作可以概括为下面这张「分层图」：

```text
        ┌─────────────────────────────────────────────────────┐
用户层  │  C++ 内核 (__qpu__)        │   Python 内核 (@cudaq.kernel) │
        └───────────────┬────────────┴───────────────┬────────┘
                        │                            │ (Python 前端
        ┌───────────────▼────────────┐   生成 Quake)  │
编译器  │      ① cudaq/ (MLIR 编译器) │◄───────────────┘
        │  AST Bridge → Quake/CC → QIR│
        └───────────────┬────────────┘
                        │ 链接生成可执行程序，调用运行时
        ┌───────────────▼────────────┐
运行时  │   ② runtime/ (C++ 运行时)    │
        │  quantum_platform → 后端    │
        │  └─ runtime/nvqir (模拟器)  │
        └───────────────┬────────────┘
                        │ 物理控制路径（可选）
        ┌───────────────▼────────────┐
实时    │   ④ realtime/ (FPGA↔GPU)    │
        └────────────────────────────┘
```

要点：

- 用户写一份内核（C++ 或 Python）。
- 编译器子系统 ① 把它加工成可执行代码，并把内核「注册」给运行时。
- 程序运行时由运行时子系统 ② 负责调度到具体后端（CPU 模拟器、GPU 模拟器，或远程 QPU）。
- Realtime 子系统 ④ 是「贴近硬件」的可选层，用于真实量子控制的低延迟协处理。

#### 4.2.3 源码精读

Overview.md 的「Code Map（代码地图）」段落把仓库逐目录讲了一遍。下面挑出与四大子系统最相关的几条：

编译器子系统的两个核心方言定义在这里被点出：

[Overview.md:L33-L37](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L33-L37) —— Quake 与 CC 两个方言分别用 `QuakeOps.td` 与 `CCOps.td`（TableGen）定义，分别建模量子计算与经典计算抽象。

> `.td` 是 MLIR 的 TableGen 定义文件，相当于「方言里有哪些操作」的清单。这两个文件是后续 u4-l2、u4-l3 讲义的主角。

运行时子系统里，最关键的 `runtime/nvqir` 目录的作用：

[Overview.md:L95-L102](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L95-L102) —— `libnvqir` 实现 QIR 规范定义的量子指令集（QIS）与运行时函数，并委托给一个可扩展的 `CircuitSimulator` API；该 API 既有 CPU（OpenMP 多线程）实现，也有基于 cuQuantum 的 GPU 实现。**切换后端是「链接期」任务**，由 `nvq++` 隐式完成。

> 「链接期切换后端」是 CUDA-Q 一个非常关键的设计：你不在源码里写「用哪个模拟器」，而是在编译/链接时决定。这一点会在 u6（后端与模拟器）单元详细展开。

运行时还提供了量子类型与算法原语：

[Overview.md:L104-L123](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L104-L123) —— `runtime/cudaq` 提供 `qudit`/`qubit`/`qreg`/`qspan` 类型、内建量子操作，以及 `cudaq::sample`、`cudaq::observe`、`cudaq::evolve` 等算法原语（含异步版本）；并定义了 `quantum_platform` 架构，使 CUDA-Q 既能瞄准模拟器也能瞄准真实量子硬件。

> 这里还藏着一个有趣的设计点：Overview 提到「量子比特不可拷贝」这一规范，被运行时通过「删除拷贝构造函数」在**编译期**强制执行。也就是说，违反量子语义的代码会在编译 C++ 时直接报错。这是「用类型系统守护量子规范」的范例，u2（量子类型）单元会深入讲。

第四个子系统 Realtime 的定位在它自己的 README 里说得很清楚：

[realtime/README.md:L1-L13](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/realtime/README.md#L1-L13) —— CUDA-Q Realtime 是一个把 GPU 加速计算与量子处理器控制系统**紧耦合**的库，承担两项职责：FPGA 与 CPU-GPU 之间的实时协处理基础；以及 NVQLink 架构的低延迟网络栈（实现 FPGA 与 GPU 之间几微秒级的数据往返）。

> Realtime 是相对独立的一块，初学者可以先把它当成「给真实量子硬件准备的低延迟控制层」，等读完前面几个单元再回来攻 u7-l2。

#### 4.2.4 代码实践

这是一个「目录地图」实践。

1. **实践目标**：把四大子系统与仓库目录一一对应起来，建立空间直觉。
2. **操作步骤**：
   - 在仓库根目录列出顶层目录（用 `ls` 或 Glob）。
   - 对照上面的「四大子系统」表格，确认 `cudaq/`、`runtime/`、`python/`、`realtime/` 都存在。
   - 进一步进入 `cudaq/`，确认它下面有 `include/`、`lib/`、`tools/`、`test/` 这类典型编译器子目录。
3. **需要观察的现象**：`runtime/` 下是否同时有 `cudaq/`（运行时库）和 `nvqir/`（模拟器）；`python/` 下是否有一个 `cudaq` 包目录。
4. **预期结果**：你能在脑子里画出一张「顶层目录 → 子系统」的速查表。例如：要找编译器 Pass → `cudaq/lib/Optimizer/Transforms/`；要找模拟器后端 → `runtime/nvqir/`；要找 Python 装饰器 → `python/cudaq/kernel/`。
5. 待本地验证：不同版本下子目录列表可能有细微差异，以你本地的 `ls` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果有人问你「CUDA-Q 的模拟器代码在哪个目录」，你应该指向哪里？它属于四大子系统中的哪一个？

> **答案**：`runtime/nvqir/`。它属于「② C++ 运行时」子系统，因为模拟器本质上是被运行时通过 `CircuitSimulator` API 调用的后端。

**练习 2**：为什么 Python 前端（`python/`）没有自己独立实现一套量子执行引擎，而是要「共享」C++ 运行时？

> **答案**：因为量子执行的真正逻辑（调度、采样、模拟、平台抽象）非常重，重复实现两套既浪费又会行为不一致。Overview.md 也指出 Python 内核最终也会生成 Quake 并复用同一套运行时与编译器，从而保证 C++ 与 Python 两端的行为一致。

---

### 4.3 关键名词：Quake、CC、QIR、nvq++

#### 4.3.1 概念说明

后续讲义会反复出现下面四个名词。本讲只需要你建立「直觉级」的理解，不必深究细节。

- **Quake（Quantum Kernel Execution）**：CUDA-Q 自己定义的「量子方言」。它离源码最近，用来表达量子比特和量子门操作。Quake 有两种建模方式：**内存语义（memory-semantics）**——比特像「内存里的对象」，被门就地修改；**值语义（value-semantics）**——每个门「消费」旧比特值、「产生」新比特值，更像函数式风格，便于优化。
- **CC（Classical Compute）**：CUDA-Q 的「经典计算方言」，专门用来表达 Quake 函数里那些 C++ 经典逻辑（循环、数组、调用等）。Quake 管「量子」，CC 管「经典里的胶水代码」。
- **QIR（Quantum Intermediate Representation）**：一个跨项目的**开放规范**（基于 LLVM），定义了「量子程序在 LLVM 层应该长什么样」。CUDA-Q 的最终 lowering 目标之一就是符合 QIR 规范的 LLVM IR，再继续降低到机器码。
- **nvq++**：CUDA-Q 的「编译器驱动」，本质上是一个 **bash 脚本**（位于 `cudaq/tools/nvqpp/`），负责把上面这些工具和步骤串起来，最终产出可执行文件。你以后写完 C++ 内核，第一个敲的命令就是 `nvq++`。

#### 4.3.2 核心流程

Overview.md 用一段「nvq++ 的四步」精炼地描述了从源码到可执行程序的流程。我们把它转成伪代码：

```text
nvq++ my_kernel.cpp 流程：
  步骤 1：用 Clang 解析 C++，通过 AST Bridge 把「量子内核」翻译成 Quake MLIR
          （子系统：① 编译器，工具 cudaq-quake）
  步骤 2：把所有 Quake 内核「注册」到运行时，便于量子 IR 内省
          （子系统：① 编译器 ↔ ② 运行时）
  步骤 3：改写原始 C++ 内核入口函数，让它转而调用一个「内核启动」运行时函数，
          该函数会瞄准指定的 quantum_platform
          （子系统：① 编译器 ↔ ② 运行时）
  步骤 4：把 Quake/CC 降低到 QIR，链接，产出可执行文件或目标代码
          （子系统：① 编译器；后续运行时由 ② 承载）
```

注意第 3 步：你的内核源码本身会被「改写」成一个调用运行时的入口，真正执行量子门的是运行时和后端——这就是为什么「换后端不用改源码」。

#### 4.3.3 源码精读

Quake 方言的「内存语义 vs 值语义」在 Overview 里是这样描述的：

[Overview.md:L20-L27](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L20-L27) —— Quake 提出了一种「内存语义」的比特模型（比特像内存对象，被就地操作），并能编码运行时才已知的信息，因而是「具体量子线路的生成器」；同时 Quake 也支持「值语义」模型——量子操作消费比特值并产生新值——更适合优化，能编码完全已知的、具体的量子线路。

CC 方言的定位紧随其后：

[Overview.md:L29-L31](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L29-L31) —— 仓库还提供了一个用于经典计算抽象的方言（CC），专门建模「构造 Quake 函数所需的 C++ 类型与运算」。

「降低到符合 QIR 的 LLVM IR」这条主线：

[Overview.md:L14-L18](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L14-L18) —— 编译器把 Clang 生成的 C++ AST 映射到内部量子/经典 MLIR 方言，再进一步降低为**符合 QIR 规范**的 LLVM IR，从而可以很方便地继续降低为目标代码。

而 nvq++ 这个「总指挥」脚本的四步流程在 Overview 的最末尾：

[Overview.md:L152-L160](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L152-L160) —— `nvq++` 是一个 bash 脚本，编排以下工作流：(1) 经由 Clang `ASTConsumer` 把 CUDA-Q C++ 内核映射为 Quake MLIR；(2) 把所有 Quake 内核注册到运行时以供量子 IR 内省；(3) 改写原始 C++ 内核入口函数，使其调用一个瞄准指定 `quantum_platform` 的内部运行时内核启动函数；(4) 降低到 QIR 并链接，产出可执行代码或目标代码。

> 这一段是整个第一单元的「总纲」。后续 u1-l3（构建与运行）、u4-l5（nvq++ 驱动脚本）都会回到这里展开。

#### 4.3.4 代码实践

这是一个「读 + 画」的实践，对应本讲指定的实践任务。

1. **实践目标**：把「C++ 源码到可执行程序」的整体数据流画成一张图，并标注每个阶段属于哪个子系统、用到哪个工具。
2. **操作步骤**：
   - 重新阅读 [Overview.md:L152-L160](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md#L152-L160) 的四步流程。
   - 在纸上或任意画图工具里画出一张从左到右的流水线，节点示例：
     `my_kernel.cpp` → `[Clang AST]` → `[Quake/CC MLIR]` → `[优化 Pass]` → `[QIR / LLVM IR]` → `[可执行文件]` → `[运行时 + 后端执行]`。
   - 在每个节点上方标注「子系统」（① 编译器 / ② 运行时），在下方标注「主要工具/目录」（如 `cudaq-quake`、`cudaq-opt`、`cudaq-translate`、`nvq++`、`runtime/nvqir`）。
3. **需要观察的现象**：哪几个阶段纯粹发生在编译期，哪个阶段发生在程序运行期；运行期阶段又被哪个子系统的代码承担。
4. **预期结果**：你得到一张「编译期 vs 运行期」分明的数据流图。理想情况下，你会清晰地看到「编译器负责把源码变成可执行文件并嵌入运行时调用，运行时负责在程序跑起来后选择后端执行量子门」。
5. 待本地验证：本实践不要求运行程序，重在理解。等 u1-l3 你真正跑通 `nvq++` 后，可以回来对照你的图修正细节。

> 提示：如果你愿意现在就先看一眼「真实内核长什么样」，可以瞄一眼示例 [docs/sphinx/examples/cpp/basics/expectation_values.cpp](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/examples/cpp/basics/expectation_values.cpp)，但**不要**纠结语法——那是 u1-l4 的内容。

#### 4.3.5 小练习与答案

**练习 1**：Quake 和 CC 各自负责表达什么？为什么要把它们分成两个方言，而不是合在一起？

> **答案**：Quake 表达量子比特与量子门（量子逻辑）；CC 表达构造 Quake 函数时需要的 C++ 经典逻辑（循环、数组、调用等）。分开是因为量子逻辑和经典逻辑的优化方式、语义约束都不同（比如量子比特不可拷贝），分开建模后各自的优化 Pass 可以更聚焦，也更容易把量子部分最终降低到 QIR。

**练习 2**：用户写的 `my_kernel.cpp` 在编译过程中，函数本身被「改写」成了什么？

> **答案**：根据 Overview.md 的第 3 步，原始内核入口函数被改写为一个调用「内部运行时内核启动函数」的入口，而那个启动函数会瞄准指定的 `quantum_platform`。换句话说，源码里的内核「调用」最终被替换成对运行时的调用，真正执行量子门的是运行时和后端。

**练习 3**：`nvq++` 是一个用 C++ 写的重型编译器可执行文件，还是一个 bash 脚本？这一点对它的「可观察性」有什么好处？

> **答案**：根据 Overview.md，`nvq++` 是一个 bash 脚本，它编排 `cudaq-quake`、`cudaq-opt`、`cudaq-translate` 等工具。因为是脚本，它内部的每一步子命令都可以被打印、替换、单独执行（例如用 `-v` 或 echo 模式），非常便于调试和理解编译流程——这是 u8-l2（调试与日志）会利用的特性。

## 5. 综合实践

把本讲三个模块串起来，完成一份**「CUDA-Q 一页速查卡」**：

1. **定位**：用你自己的一句话写出 CUDA-Q 是什么（参考 4.1）。
2. **地图**：列出四大子系统及其主目录，并写出每个子系统「最该被记住的一句话职责」（参考 4.2）。
3. **数据流**：画出 4.3.4 那张「源码到可执行程序」的数据流图，并在图上用颜色或标记区分**编译期**和**运行期**。
4. **名词表**：用自己的话解释 Quake、CC、QIR、nvq++ 各是什么（每项不超过两句）。
5. **延伸阅读清单**：从 [Overview.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md) 的 Code Map 里，挑出你最想先深入的三个目录，记下它们的名字和你猜测的作用。

完成这份速查卡后，你就具备了读后续所有讲义的「全局坐标系」。建议把这张图保存下来——在 u4（编译器）、u6（后端）单元你会不断回到这张图去对照。

## 6. 本讲小结

- **CUDA-Q 是一个混合量子-经典编程平台**：让 CPU、GPU、QPU 在一个系统里被统一编程与调度，支持 C++ 与 Python 两套前端。
- **仓库由四大子系统组成**：`cudaq/`（MLIR 编译器）、`runtime/`（C++ 运行时与 nvqir 模拟器）、`python/`（Python 前端）、`realtime/`（实时控制子系统）。
- **C++ 运行时是公共底盘**：Python 前端最终也复用它，从而两端行为一致；模拟器与远程 QPU 都通过 `quantum_platform` 抽象接入。
- **核心中间表示是 Quake 与 CC**：Quake 建模量子逻辑（有内存语义和值语义两种），CC 建模经典逻辑；二者最终降低到符合 QIR 规范的 LLVM IR。
- **`nvq++` 是编译流程的总指挥**：一个 bash 脚本，四步走（映射到 Quake → 注册到运行时 → 改写内核入口 → 降低到 QIR 并链接）。
- **后端在链接期切换**：源码里不指定用哪个模拟器，而由 `nvq++` 在链接时决定——这是 CUDA-Q 一个贯穿全篇的重要设计。

## 7. 下一步学习建议

本讲只建立了「鸟瞰图」，下一步建议按以下顺序推进：

- **先建立空间感**：去读 [u1-l2 仓库目录结构地图](./u1-l2-repo-structure.md)，把四大子系统细化到二级目录。
- **动手跑起来**：再读 [u1-l3 从源码构建与运行 CUDA-Q](./u1-l3-build-and-run.md)，亲手跑通一次 `nvq++`，让 4.3 的数据流图在你机器上「活」一次。
- **写第一个内核**：接着读 [u1-l4 第一个 C++ 量子内核](./u1-l4-first-cpp-kernel.md)，进入真正的编程模型。
- **想直接深入编译器的读者**：可以跳到 u4 单元（MLIR、Quake 与代码生成），但建议至少先读完 u1-l3，保证你能在本地观察编译产物。

> 建议继续精读的源码：先反复读 [Overview.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/Overview.md)，它是整个仓库最好的「自带导览」；之后带着本讲画的数据流图，进入 [runtime/cudaq.h](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/runtime/cudaq.h) 和 [QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td)。
