# 源码目录结构与组织方式

## 1. 本讲目标

上一讲我们建立了「LLVM 是编译器基础设施、采用 monorepo、三段式架构以 IR 为桥梁」的全局印象。本讲把镜头拉近到 **`llvm/` 子项目内部**，学完后你应当能够：

- 说出 `llvm/` 下 `lib/`、`include/`、`tools/`、`examples/`、`test/`、`docs/` 这几个标准目录各自的职责与划分规则。
- 理解 **「实现」(`lib/`) 与「公共头」(`include/`) 镜像对应** 这一贯穿 LLVM 全家的组织惯例。
- 看懂 **「工具（`tools/`）只是薄壳，真正逻辑都在 `lib/`」** 的设计，并能从某个工具的 `main` 一路追到它依赖的 `lib/` 子目录。
- 在海量源码中，快速定位「某个功能该去哪个目录读」。

本讲是后续所有「读源码」讲义的基础——如果你不熟悉目录约定，后面读任何模块都会迷路。

## 2. 前置知识

本讲默认你已读过上一讲（u1-l1），了解以下概念：

- **monorepo**：LLVM 把多个子项目（llvm、clang、mlir、lld…）放在同一个 Git 仓库里。
- **三段式架构**：前端 → IR → 后端，IR 是前后端的桥梁。
- **IR / bitcode / target** 等术语。

本讲会用到的几个新术语，先用大白话解释：

- **头文件（header，`.h`）**：声明「有哪些类、函数、接口」，供别人 `#include` 引用。它是「公共契约」。
- **实现文件（`.cpp`）**：写「这些接口具体怎么干活」。
- **库（library）/ 组件（component）**：把一组 `.cpp` 编译打包成一个可被复用的库；LLVM 把每个 `lib/` 子目录打包成一个「组件」，并起一个组件名（如 `LLVMCore`、`LLVMAnalysis`）。
- **驱动（driver）**：在编译器语境里，"driver" 指「接收命令行参数、调度各阶段干活」的入口程序，不是显卡驱动。
- **`#include` 路径**：写 `#include "llvm/IR/Module.h"` 时，编译器会去约定的目录下找 `llvm/IR/Module.h` 这个文件，LLVM 的约定目录就是 `llvm/include/`。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| `llvm/tools/opt/opt.cpp` | `opt` 优化器的入口，现在只剩一个极简的 `main` |
| `llvm/tools/opt/CMakeLists.txt` | 声明 `opt` 工具依赖哪些 `lib/` 组件，是「工具→库」对应关系的权威来源 |
| `llvm/tools/opt/optdriver.cpp` | `opt` 真正的驱动逻辑（`optMain`），与 `opt.cpp` 分离 |
| `llvm/tools/opt/NewPMDriver.cpp` | `opt` 执行 pass 流水线的核心 `runPassPipeline` |
| `llvm/tools/llc/llc.cpp` | `llc` 后端代码生成驱动，演示大量 `#include "llvm/..."` 路径 |
| `llvm/lib/IR/CMakeLists.txt` | 把 `lib/IR` 目录打包成名为 `LLVMCore` 的组件 |
| `llvm/examples/Kaleidoscope/Chapter2/toy.cpp` | 官方教学示例，演示「一个完整小程序」如何分阶段组织 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **`lib/` 与 `include/`：实现层与公共头的镜像布局**
2. **`tools/`：薄壳驱动与「工具—库」对应关系**
3. **`examples/`（与 `test/`）：可运行的学习样本与回归测试**

### 4.1 `lib/` 与 `include/`：实现层与公共头的镜像布局

#### 4.1.1 概念说明

进入 `llvm/` 目录，最显眼的两个顶层目录就是 `lib/` 和 `include/`。它们的关系可以用一句话概括：

> **`include/` 放「对外契约」（头文件 `.h`），`lib/` 放「具体实现」（`.cpp`），两者按功能模块一一镜像。**

也就是说，几乎每一个 `lib/` 下的子目录，都在 `include/llvm/` 下有一个同名的子目录。例如：

- `llvm/lib/IR/` 的实现 ↔ `llvm/include/llvm/IR/` 的头文件
- `llvm/lib/Analysis/` 的实现 ↔ `llvm/include/llvm/Analysis/` 的头文件
- `llvm/lib/CodeGen/` 的实现 ↔ `llvm/include/llvm/CodeGen/` 的头文件

为什么要这样分？这是 C/C++ 大型项目的通行做法：

- **头文件是「接口」**：别的模块只 `#include` 头文件，依赖稳定的接口而不依赖会变动的实现，从而降低耦合。
- **实现可以独立编译**：每个 `lib/` 子目录被编译成一个「组件库」，谁要用就链接谁，不必把整个 LLVM 全编进来。

> 初学者常见困惑：「`lib/IR` 的组件名为什么叫 `LLVMCore`？名字对不上啊！」——这是历史遗留命名，IR 是 LLVM 的「核心（Core）」，所以组件名沿用了 `LLVMCore`。**目录名和组件名不总是 1:1 对应**，这是读 LLVM 源码时要记住的一个小陷阱。

#### 4.1.2 核心流程

当你（或某个工具）想用 LLVM 的某个功能时，发生的事情大致是：

```
源码里写: #include "llvm/IR/Module.h"
          │
          ▼  编译器按 include 路径查找
找到文件: llvm/include/llvm/IR/Module.h        ← 公共契约（类声明）
          │
          ▼  链接阶段
链接到库: LLVMCore（由 llvm/lib/IR/*.cpp 编译打包）  ← 真正实现
```

关键点：

1. **头文件路径就是物理路径**：`#include "llvm/IR/Module.h"` 在磁盘上真的对应 `llvm/include/llvm/IR/Module.h`。所以看到一条 `#include`，你就能直接推算出它引用的是哪个子目录。
2. **镜像目录清单**：`llvm/lib/` 与 `llvm/include/llvm/` 下的子目录名称高度重合（`IR`、`Analysis`、`CodeGen`、`MC`、`Target`、`Transforms`、`Support`…）。建立「同名即镜像」的直觉后，找代码会快很多。

#### 4.1.3 源码精读

**例 1：组件名与目录名的对应**

看 `llvm/lib/IR` 这个目录如何被声明成一个组件。它的 `CMakeLists.txt` 第一行就是组件名：

[llvm/lib/IR/CMakeLists.txt:1](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/IR/CMakeLists.txt#L1) —— `add_llvm_component_library(LLVMCore ...)` 把整个 `lib/IR` 目录里的 `.cpp` 打包成名为 **`LLVMCore`** 的组件库。注意：**目录叫 `IR`，组件叫 `Core`**，这就是前文提醒过的「名字不 1:1」。

对照看分析模块，它就老实多了：

[llvm/lib/Analysis/CMakeLists.txt:41](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/lib/Analysis/CMakeLists.txt#L41) —— `add_llvm_component_library(LLVMAnalysis ...)`，目录 `Analysis` ↔ 组件 `LLVMAnalysis`，这次名字一致。

**例 2：从一条 `#include` 反推目录**

`llc` 工具里大量使用「路径即目录」的头文件引用方式：

[llvm/tools/llc/llc.cpp:29-42](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp#L29-L42) —— 这段集中引用了 IR 与 CodeGen 相关头文件，例如第 36 行 `#include "llvm/IR/Module.h"`、第 41 行 `#include "llvm/MC/TargetRegistry.h"`。看到 `llvm/IR/Module.h`，你就知道它的声明在 `llvm/include/llvm/IR/Module.h`，实现打包在 `lib/IR`（即 `LLVMCore`）里。

整理成一张常用的「功能 → 目录 → 组件」对照表，读源码时可随时查阅：

| 功能 | 实现目录 `lib/` | 公共头 `include/llvm/` | 组件名 |
|---|---|---|---|
| IR 核心数据结构 | `lib/IR` | `include/llvm/IR` | `LLVMCore` |
| 分析 pass | `lib/Analysis` | `include/llvm/Analysis` | `LLVMAnalysis` |
| 后端代码生成 | `lib/CodeGen` | `include/llvm/CodeGen` | `LLVMCodeGen` |
| 机器代码 / 目标文件 | `lib/MC` | `include/llvm/MC` | `LLVMMC` |
| 优化变换 | `lib/Transforms` | `include/llvm/Transforms` | 多个（如 `LLVMInstCombine`） |

#### 4.1.4 代码实践

**实践目标**：亲手验证「`include/` 与 `lib/` 镜像对应」这一惯例。

**操作步骤**：

1. 在仓库里列目录，对照同名子目录。在终端执行（仅观察，不改动源码）：
   ```bash
   ls llvm/include/llvm/IR
   ls llvm/lib/IR
   ```
2. 在 `llvm/include/llvm/IR/` 里随便挑一个头文件，例如 `Module.h`。
3. 去同名的实现目录 `llvm/lib/IR/` 里找 `Module.cpp`。
4. 打开这两个文件，观察：头文件里声明了 `class Module` 的方法，实现文件里给出这些方法的定义。

**需要观察的现象**：`include/llvm/IR/` 下的 `.h` 文件名，大多能在 `lib/IR/` 下找到同名 `.cpp`（不是全部，但比例很高）。

**预期结果**：你会直观看到「头文件声明 + 同名实现文件」的镜像结构，对这一约定产生肌肉记忆。

> 说明：本实践是「源码阅读型」，不涉及编译运行；如无本地构建环境也可在 GitHub 网页上浏览对应路径完成。

#### 4.1.5 小练习与答案

**练习 1**：看到源码里写 `#include "llvm/CodeGen/TargetPassConfig.h"`，它对应的头文件物理路径和实现所在组件分别是什么？

> **答案**：物理路径是 `llvm/include/llvm/CodeGen/TargetPassConfig.h`；实现打包在 `lib/CodeGen` 目录，对应组件 `LLVMCodeGen`。

**练习 2**：为什么 `lib/IR` 的组件名不叫 `LLVMIR` 而叫 `LLVMCore`？

> **答案**：历史命名。IR 是 LLVM 的「核心」，早期就把这个组件命名为 `LLVMCore` 并沿用至今。这说明**目录名与组件名不总是 1:1**，读 `CMakeLists.txt` 里的 `add_llvm_component_library(名字 ...)` 才是确认组件名的可靠办法。

---

### 4.2 `tools/`：薄壳驱动与「工具—库」对应关系

#### 4.2.1 概念说明

`llvm/tools/` 下是一大堆命令行工具：`opt`、`llc`、`llvm-as`、`llvm-dis`、`lli`、`llvm-mc`、`llvm-objdump`…（仓库里有上百个）。它们是我们与 LLVM 交互的「门面」。

但关键认知是：

> **工具本身通常很「薄」。`main` 函数往往只负责解析命令行参数、读取输入，然后把真正的活儿交给 `lib/` 里的组件去干。**

这样做的好处是：**逻辑全部沉淀在可复用的库中**，工具只是「胶水」。于是你可以用同一套库，写出自定义工具，而不必复制粘贴实现。

为什么强调这点？因为初学者常以为「要学 `opt` 就读 `tools/opt/`」，结果发现 `tools/opt/opt.cpp` 短得离谱，真正逻辑藏在别处。本节就带你把这条「工具 → 库」的路径走通。

#### 4.2.2 核心流程

以 `opt` 为例，它从命令行到执行优化，经历的链路是：

```
opt 命令行 (opt -passes=...)
   │
   ▼  入口
opt.cpp: main()              ← 极简，只转发
   │  调用 optMain(...)
   ▼
optdriver.cpp: optMain()     ← 解析参数、加载 IR
   │  调用 runPassPipeline(...)
   ▼
NewPMDriver.cpp: runPassPipeline()  ← 真正跑优化流水线
   │  调用 lib/ 里的各组件（Analysis / Passes / Transforms…）
   ▼
lib/ 里的实现              ← 真正干活的库
```

而「`opt` 到底依赖哪些 `lib/` 组件」这件事，权威答案写在它的 `CMakeLists.txt` 的 `LLVM_LINK_COMPONENTS` 里。读这张清单，就等于看到了工具到库的全部依赖边。

#### 4.2.3 源码精读

**例 1：`opt.cpp` 现在有多薄**

[llvm/tools/opt/opt.cpp:23-27](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/opt.cpp#L23-L27) —— 整个 `main` 只有一行：`return optMain(argc, argv, {});`。真正的 `optMain` 是 `extern "C"` 声明、在别处定义的。这就是「薄壳」的极致体现：工具入口几乎不干事，只把控制权交出去。

那 `optMain` 定义在哪？在 `optdriver.cpp`：

[llvm/tools/opt/optdriver.cpp:402](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/optdriver.cpp#L402) —— 这里定义了 `optMain`，负责解析命令行、读取输入 IR 模块，再调用流水线执行函数。而真正执行 pass 流水线的 `runPassPipeline` 在：

[llvm/tools/opt/NewPMDriver.cpp:355](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/NewPMDriver.cpp#L355) —— 这是 `opt` 把优化交给「新 Pass 管理器」的衔接点，背后再调用 `lib/Passes`、`lib/Transforms` 等组件。

**例 2：`CMakeLists.txt` 是「工具→库」的权威清单**

[llvm/tools/opt/CMakeLists.txt:1-31](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/CMakeLists.txt#L1-L31) —— `set(LLVM_LINK_COMPONENTS ...)` 列出了 `opt` 依赖的全部组件：`Analysis`、`AsmParser`、`BitWriter`、`CodeGen`、`Core`、`IPO`、`IRReader`、`InstCombine`、`MC`、`ScalarOpts`、`Support`、`Target`、`TransformUtils`、`Vectorize`、`Passes`… **这张清单就是 `opt` 到 `lib/` 的完整依赖图**。例如清单里的 `Core` 对应 `lib/IR`，`Analysis` 对应 `lib/Analysis`，`ScalarOpts` 对应 `lib/Transforms/Scalar`。

**例 3：工具如何被组装出来**

[llvm/tools/opt/CMakeLists.txt:34-41](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/CMakeLists.txt#L34-L41) —— 先把 `NewPMDriver.cpp` 和 `optdriver.cpp` 编成一个静态库 `LLVMOptDriver`。

[llvm/tools/opt/CMakeLists.txt:43-52](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/CMakeLists.txt#L43-L52) —— 再用 `add_llvm_tool(opt ...)` 把 `opt.cpp` 编成可执行文件，并用 `target_link_libraries(opt PRIVATE LLVMOptDriver)` 链接上前面的驱动库。可见「工具 = 薄入口 + 驱动库 + 一堆组件库」的组装方式。

小结一下「薄壳」带来的好处：因为逻辑都在库里，所以 `opt`、`clang`、乃至你自己写的工具，都能复用同一份 `lib/` 实现，互不重复。

#### 4.2.4 代码实践

**实践目标**：把 `opt` 的「工具→库」路径完整走一遍，确认工具只是薄壳。

**操作步骤**（纯阅读，不改源码）：

1. 打开 [llvm/tools/opt/opt.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/opt.cpp)，确认 `main` 只有一行，调用 `optMain`。
2. 打开 [llvm/tools/opt/optdriver.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/optdriver.cpp)，找到 `optMain` 的定义（第 402 行附近）。
3. 打开 [llvm/tools/opt/NewPMDriver.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/NewPMDriver.cpp)，找到 `runPassPipeline`（第 355 行）。
4. 打开 [llvm/tools/opt/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/opt/CMakeLists.txt)，读 `LLVM_LINK_COMPONENTS` 清单。

**需要观察的现象**：`opt.cpp` 越往下走，代码越多、越接近「真正干活」；而 `LLVM_LINK_COMPONENTS` 里列的每一个组件名，都对应 `lib/` 下一个子目录。

**预期结果**：你能画出一张 `opt → optMain → runPassPipeline → lib/ 组件` 的调用/依赖路径图，并理解「工具很薄、库很厚」。

> 说明：本实践为「源码阅读型」，无需编译；如本地已构建，也可用 `grep` 验证调用关系。

#### 4.2.5 小练习与答案

**练习 1**：在 `llvm/tools/opt/CMakeLists.txt` 的 `LLVM_LINK_COMPONENTS` 里，`Core` 这一项对应 `lib/` 下哪个目录？

> **答案**：对应 `lib/IR`。组件名 `Core`（即 `LLVMCore`）就是 `lib/IR` 打包出来的，见 4.1 节的对照表。

**练习 2**：既然工具只是薄壳，那「`opt` 如何执行优化」的核心逻辑最终落在哪个目录？

> **答案**：落在 `lib/` 下相关组件，尤其是 `lib/Passes`（pass 管理器框架）、`lib/Transforms`（各类优化变换）和 `lib/Analysis`（供优化复用的分析结果）。`tools/opt/` 只负责驱动与衔接。

**练习 3**：为什么 LLVM 要把工具的 `main` 写得这么薄、把驱动逻辑拆成单独的库（`LLVMOptDriver`）？

> **答案**：为了复用与解耦。把驱动逻辑放进库后，它既能被 `opt` 工具用，也能被其他工具或测试复用；同时让「入口」与「实现」分离，便于维护和插件化（后续 u9 会讲 pass 插件机制）。

---

### 4.3 `examples/`（与 `test/`）：可运行的学习样本与回归测试

#### 4.3.1 概念说明

除了 `lib/`、`include/`、`tools/`，还有几个对学习者极其友好的目录：

- **`examples/`**：官方提供的「可运行教学样本」，体量小、自包含，是最适合初学者读的代码。里面有经典的 **Kaleidoscope**（实现一门小语言）、**Bye**（最小 pass 示例）、**Fibonacci**、**ModuleMaker**、**HowToUseLLJIT** 等。
- **`test/`**：回归测试。几乎每个功能都有对应的 `.ll` / `.c` 测试用例，用 `lit` + `FileCheck` 跑。这是理解「某个功能预期行为」的宝库。
- **`docs/`**：文档（Markdown / RST），237 个文件，覆盖从入门到深入的各类主题。
- **`unittests/`**：基于 gtest 的 C++ 单元测试。

本节聚焦 `examples/`，因为它最适合初学者上手；`test/` 与 `unittests/` 会在专家层（u9）专门讲。

#### 4.3.2 核心流程

`examples/` 的每个子目录通常是「一个独立小项目」，能单独构建运行。以 Kaleidoscope 为例，它的章节是渐进式的：

```
examples/Kaleidoscope/
  Chapter2/   ← 第 2 章：词法分析 + 解析 + AST（还没有 IR）
  Chapter3/   ← 第 3 章：生成 LLVM IR
  Chapter4/   ← 加入 JIT 与优化
  ...
  Chapter7~9/ ← 更完整的语言特性
```

每个 `ChapterN/toy.cpp` 都是一个「自包含的单文件小编译器」，从词法分析一路到目标阶段，章节之间逐步加料。这种结构让你可以**从最简单的一章开始读，循序渐进**。

#### 4.3.3 源码精读

以 Chapter2 为例，看它如何用注释把单文件分成清晰的几个阶段：

[llvm/examples/Kaleidoscope/Chapter2/toy.cpp:11](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L11) —— 注释 `// Lexer` 标出词法分析段，把源码字符切成 Token。

[llvm/examples/Kaleidoscope/Chapter2/toy.cpp:83](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L83) —— 注释 `// Abstract Syntax Tree` 标出 AST 节点定义段。

[llvm/examples/Kaleidoscope/Chapter2/toy.cpp:160](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L160) —— 注释 `// Parser` 标出语法分析段，把 Token 流组装成 AST。

[llvm/examples/Kaleidoscope/Chapter2/toy.cpp:427-430](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp#L427-L430) —— `// Main driver code.` 段下的 `int main()` 是整个示例的入口。

这种「单文件、用注释分阶段」的组织方式，是初学者理解编译器各阶段最友好的读法。后续 u2 会专门带读 Kaleidoscope。

#### 4.3.4 代码实践

**实践目标**：在 `examples/` 中找到一个能读懂的最小样本，建立「读示例学 LLVM」的习惯。

**操作步骤**：

1. 列出所有示例：`ls llvm/examples/`，浏览有哪些可选（Kaleidoscope、Bye、Fibonacci、ModuleMaker…）。
2. 打开 [llvm/examples/Kaleidoscope/Chapter2/toy.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/examples/Kaleidoscope/Chapter2/toy.cpp)。
3. 用本节给出的行号定位四个阶段注释（Lexer / AST / Parser / Main），逐段粗读。
4. 找到 `int main()`（第 430 行），看它如何驱动整个流程。

**需要观察的现象**：一个「编译器前端」被拆成 词法 → AST → 解析 → 主驱动 四段，每段职责清晰。

**预期结果**：你能用一句话说出每个阶段在干什么，并对后续 Kaleidoscope 专题（u2）有心理预期。

> 说明：Chapter2 尚未涉及 IR 生成，是纯前端，读起来不需要 LLVM 背景，非常适合作为第一份完整阅读的源码。如本地已构建，可按 `examples/Kaleidoscope/` 的 `CMakeLists.txt` 构建运行；否则仅阅读即可。

#### 4.3.5 小练习与答案

**练习 1**：`llvm/examples/` 里的代码和 `llvm/lib/` 里的代码，定位上有何不同？

> **答案**：`lib/` 是 LLVM 工具链正式依赖的实现库，必须存在、必须正确；`examples/` 是教学/演示用的可选样本，自包含、体量小，主要给人读和学，不是工具链运行的必需部分。

**练习 2**：如果你想理解「某个 IR 指令的预期行为」，去哪个目录找答案最快？

> **答案**：去 `llvm/test/` 找对应的回归测试（通常是 `.ll` 文件配 `FileCheck` 检查行）。测试用例往往是最小、最精确的「行为说明书」。这部分会在 u9 详讲。

---

## 5. 综合实践

把本讲三个模块串起来，完成规格里给定的核心任务：

> **选择 `opt` 或 `llc` 工具，找到它的 `main` 入口文件，以及它主要依赖的 `lib/` 子目录，记录这条「从工具到库」的路径。**

以 `opt` 为例，建议产出一张这样的记录表（请你自己读完源码后填全）：

| 层级 | 文件 / 目录 | 关键行 | 作用 |
|---|---|---|---|
| 工具入口 | `llvm/tools/opt/opt.cpp` | L27 `main` | 极简入口，转发到 `optMain` |
| 驱动逻辑 | `llvm/tools/opt/optdriver.cpp` | L402 `optMain` | 解析参数、加载 IR |
| 流水线衔接 | `llvm/tools/opt/NewPMDriver.cpp` | L355 `runPassPipeline` | 交给新 PM 跑优化 |
| 依赖清单 | `llvm/tools/opt/CMakeLists.txt` | L1-31 `LLVM_LINK_COMPONENTS` | 声明依赖哪些 `lib/` 组件 |
| 真正实现 | `lib/IR`、`lib/Analysis`、`lib/Passes`、`lib/Transforms`… | — | 真正干活的库 |

**进阶（可选）**：换 `llc` 再做一遍。从 [llvm/tools/llc/llc.cpp](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/tools/llc/llc.cpp) 找到它的 `main`，观察它 `#include` 了大量 `llvm/CodeGen/...`、`llvm/MC/...`、`llvm/IR/...` 头文件，据此推断它依赖 `lib/CodeGen`、`lib/MC`、`lib/IR` 等组件，再画出 `llc` 的「工具→库」路径图。

**预期结果**：你能对至少一个工具说出「入口在哪、逻辑在哪、依赖哪些 `lib/` 子目录」，并且形成「看到 `#include "llvm/X/Y.h"` 就知道去 `include/llvm/X/` 读声明、去 `lib/X/` 读实现」的条件反射。

> 说明：本实践以源码阅读为主，无需构建；若想验证依赖关系，可在本地构建目录用 `llvm-build`/CMake 相关命令查看组件依赖，但非必需。

## 6. 本讲小结

- `llvm/` 的标准目录有明确分工：`include/`（公共头）、`lib/`（实现）、`tools/`（命令行工具）、`examples/`（教学样本）、`test/`（回归测试）、`docs/`（文档）、`unittests/`（单元测试）。
- **`lib/` 与 `include/llvm/` 镜像对应**：几乎每个功能在两边都有同名子目录；头文件给契约，`.cpp` 给实现。注意目录名与组件名不总是 1:1（如 `lib/IR` ↔ 组件 `LLVMCore`）。
- **头文件路径即物理路径**：`#include "llvm/IR/Module.h"` 直接对应 `llvm/include/llvm/IR/Module.h`。
- **工具是薄壳、库是主体**：`tools/opt/opt.cpp` 的 `main` 只有一行，真正逻辑在 `optdriver.cpp` / `NewPMDriver.cpp`，再下沉到 `lib/` 各组件；`CMakeLists.txt` 的 `LLVM_LINK_COMPONENTS` 是「工具→库」依赖的权威清单。
- **`examples/` 是最好的入门读物**：Kaleidoscope 章节渐进、单文件分阶段，适合从零读起；`test/` 则是「行为说明书」。
- 这套「lib/include 镜像 + 薄壳工具」的布局，在 `clang/`、`mlir/` 等子项目里同样适用（如 `clang` 也有 `lib/`、`include/`、`tools/`、`test/`、`examples/`），学会一处即可举一反三。

## 7. 下一步学习建议

本讲让你能在仓库里「找得到路」。接下来：

- **横向**：用同样的方法扫一眼 `clang/`、`mlir/`、`lld/` 的顶层目录，确认它们是否都遵循 `lib/include/tools/test` 布局（`lld/` 略有不同，按目标格式 `ELF/COFF/MachO/wasm` 组织，值得留意）。
- **纵向（下一讲 u1-l3）**：学「构建系统：CMake 与编译流程」，理解这些 `lib/` 组件是如何被 `CMakeLists.txt` 串起来编译的，以及如何配置一次最小构建。
- **后续**：u1-l4 会带你看 `opt`/`llc`/`llvm-as`/`llvm-dis`/`lli` 这些工具的实际用法；u2 起进入 LLVM IR 与三段式编译的细节。
- **延伸阅读**：建议浏览 [llvm/docs/](https://github.com/llvm/llvm-project/blob/610a3105af18f5efd127d2eaa1e4633de830b593/llvm/docs) 下的入门文档，以及官方 [LLVM 默认文档索引](https://llvm.org/docs/)，配合本讲的目录地图理解会更深。
