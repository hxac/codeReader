# LTO 链接时优化

## 1. 本讲目标

普通编译时，每个源文件被独立编译成目标文件（`.o`），优化器只能「看见」当前这一个文件里的代码：一个文件里的函数无法被内联进另一个文件里的调用点，未被引用的函数也无法跨文件删除。**链接时优化（Link Time Optimization，LTO）** 就是把优化的时机推迟到「链接」这一步——此时所有目标文件都已摆上桌面，优化器第一次拥有了整个程序的视野。

本讲学完后，你应该能够：

- 说出 **全 LTO（Full LTO）** 与 **ThinLTO** 各自的工作流程、产物形态与适用取舍。
- 解释 **Module Summary Index（模块摘要索引）** 是什么、由哪些数据结构组成、如何用一个轻量摘要替代完整 IR 来支撑跨模块分析。
- 理解 **FunctionImport（函数导入）** 的决策机制：阈值代价模型、热度乘数、DFS 调用图遍历，以及最终如何用 `IRMover` 把选中的函数真正搬进目标模块。
- 能用 `clang -flto` / `clang -flto=thin` 实际构建一个多文件项目，并对照源码解释每一步产物。

本讲是 [u8-l1](u8-l1-executionengine-orc-jit.md) JIT 的「离线镜像」——JIT 在运行期按需编译单个模块，而 LTO 在链接期把许多模块合并优化。它也直接承接 [u4-l1（新 Pass 管理器）](u4-l1-new-pass-manager.md) 与 [u4-l3（经典优化 pass）](u4-l3-classic-optimization-passes.md)：LTO 的本质就是把过程间优化（内联、死代码消除、常量传播）放到能看见全部模块的更大尺度上去做。

## 2. 前置知识

在进入 LTO 之前，请确认以下概念已经清晰（若不熟悉，建议先看对应讲义）：

- **LLVM IR 的三种形态**：内存中的 `Module` 对象、人类可读的 `.ll` 文本、紧凑的 `.bc` 位码（[u1-l4](u1-l4-core-tools.md) / [u3-l5](u3-l5-asm-bitcode.md)）。LTO 的全部魔法，本质上都是在搬运与处理这些 `.bc`。
- **Module / Function / BasicBlock 的包含层次**（[u3-l1](u3-l1-ir-hierarchy.md)）。本讲的「摘要」就是对这些对象做的精简投影。
- **Value / Use 的 def-use 链与 SSA**（[u3-l2](u3-l2-value-use-ssa.md)）。跨模块内联要解决的核心难题，就是「引用了别的模块里某个 `Value`」时如何改写。
- **链接器的基本职责**：符号解析（一个符号被多处定义时选谁）、符号绑定、段合并、产出可执行文件（[u8-l3](u8-l3-lld-linker.md) 会详讲 LLD）。LTO 是嵌进链接过程的一个「优化钩子」。

两个本讲会用到的关键术语：

- **符号可见性 / 链接类型（linkage）**：`external`（外部可见，可被其他模块引用）、`internal`（仅本模块可见，可被随意改名删除）。LTO 在跨模块搬运代码时，常需要把 `internal` 的局部符号**提升（promote）**为带模块后缀的外部符号，否则别的模块引用不到它。
- **GUID（Globally Unique IDentifier）**：跨模块识别同一个全局值用的 64 位整数 ID，由符号名经哈希得到。它是整个跨模块分析的「主键」。

## 3. 本讲源码地图

本讲围绕四个核心源码文件展开，它们恰好对应 LTO 的四步心法「**建摘要 → 写产物 → 算导入 → 搬代码**」：

| 文件 | 作用 | 对应环节 |
| --- | --- | --- |
| `llvm/lib/Analysis/ModuleSummaryAnalysis.cpp` | 遍历一个 `Module`，为每个函数/变量构建精简的摘要 | 建摘要 |
| `llvm/lib/IR/ModuleSummaryIndex.cpp`（及其头 `llvm/include/llvm/IR/ModuleSummaryIndex.h`） | 定义摘要的数据结构：`ModuleSummaryIndex`、`FunctionSummary`、`GlobalValueSummary` | 摘要数据结构 |
| `llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp` | 把完整位码与「仅含摘要的最小位码」分别写到不同文件 | 写产物 |
| `llvm/lib/Transforms/IPO/FunctionImport.cpp` | 跨模块导入的决策（算谁该被搬）与执行（用 `IRMover` 真搬） | 算导入 + 搬代码 |

此外会附带引用：

- `llvm/include/llvm/Transforms/IPO/FunctionImport.h`：导入决策用到的数据结构（`ImportMapTy`、`ExportSetTy`）。
- `llvm/docs/LinkTimeOptimization.md`：官方对 LTO 与链接器协作的设计说明。
- `llvm/lib/LTO/LTOBackend.cpp`：ThinLTO 后端的各阶段（`preopt/promote/internalize/import/opt`），用于实践观察。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲清 LTO/ThinLTO 的全局原理与差异（4.1），再钻进支撑这一切的「摘要索引」数据结构及其构建（4.2），最后讲跨模块内联的决策与执行（4.3）。

### 4.1 LTO 与 ThinLTO 的基本原理与差异

#### 4.1.1 概念说明

**全 LTO（Full LTO）** 的做法直观而「暴力」：编译期把每个翻译单元都编译成 LLVM 位码（`.bc`，而不是机器码 `.o`）；链接时把所有 `.bc` **合并成一个巨型 `Module`**，在这个完整视野上跑一遍过程间优化（内联、死代码消除等），再一次性生成机器码。

好处是优化质量极高——优化器真的看见了整个程序；代价是：

1. 链接阶段要重新解析所有位码、重新做前端分析、重新跑优化，**链接时间和内存随程序规模急剧膨胀**。
2. 整个过程是**单点串行**的，难以并行。
3. **增量构建几乎不可能**：改一个文件，整个合并体都得重来。

**ThinLTO（Thin LTO）** 的核心洞见是：跨模块分析其实不需要完整的 IR，只需要一份**摘要（summary）**——记录「每个函数有多大、调用了谁、被谁调用、热度如何」即可。于是它把工作切成两阶段：

- **Thin Link（精简链接）阶段**：读取所有模块的摘要，**合并成一个跨模块的索引**，在此之上做跨模块分析（决定每个模块该从别处「导入」哪些函数），输出一份导入计划。这一步只碰摘要，不碰完整 IR，因此又快又省内存。
- **后端（Backend）阶段**：每个模块**各自独立**地、可并行地，依据导入计划把需要的函数搬进来，再做本地优化与代码生成。

ThinLTO 用「摘要 + 分而治之」换来了：可并行、内存可控、增量友好，同时通过导入机制保留了绝大部分跨模块优化的收益。

> 一个关键澄清：ThinLTO 并不是「不做过程间优化」，而是把「分析在哪里做、IR 什么时候搬」重新编排了一遍。跨模块内联通过 4.3 的 FunctionImport 实现——后端阶段，被调用函数的函数体会从它定义所在的模块「拷贝」到调用者所在的模块，于是后端在本模块内就能完成内联。

#### 4.1.2 核心流程

两种 LTO 的端到端流程对比（伪代码）：

```text
# 编译阶段（两者相同）
clang -flto{=thin} -c a.c   -> a.o（其实是 LLVM 位码，不是机器码）
clang -flto{=thin} -c main.c -> main.o

# 链接阶段
clang a.o main.o -o prog
  |
  |  Full LTO:
  |    把所有 .bc 合并成 1 个大 Module
  |    -> 在大 Module 上跑过程间优化（单一串行）
  |    -> 代码生成 -> 1 个机器码对象 -> 正常链接
  |
  |  ThinLTO:
  |    Thin Link: 读取所有摘要 -> 合并索引 -> 算出每个模块的 ImportList
  |    Backend (可并行, 每模块一份):
  |      promote   : 局部符号提升为带模块ID的外部符号
  |      internalize: 把只在本程序内用的符号改成 internal
  |      import    : 按 ImportList 用 IRMover 搬入别模块函数体
  |      opt       : 本模块优化（含被搬进来函数的内联）
  |      precodegen-> 各模块各自出机器码 -> 正常链接
```

注意 ThinLTO 后端那条流水线里的阶段名 `promote / internalize / import / opt / precodegen`，它们不是抽象概念，而是源码里真实存在的钩子点（见 4.1.3 与综合实践）。

**为什么编译期要把符号标成 ThinLTO 模式？** 因为摘要里需要知道「这个函数将来是否参与跨模块导入」，编译器据此决定要不要为某些不可导入的函数（如含局部内联汇编的）打上标记。

#### 4.1.3 源码精读

官方设计文档把 LTO 与链接器的协作描述为「分阶段通信」：链接器先读所有对象文件收集符号信息（Phase 1），做符号解析（Phase 2），再调用 LTO 优化位码并保留必要符号（Phase 3）：

> 链接器「识别到 `foo2()` 是位码文件里定义的外部可见符号，但全程序无人引用，于是优化器删除它；继而发现 `foo3()` 也无人调用，一并删除」。这种「优化器借助链接器的符号解析结果」的紧耦合，正是 LTO 的价值所在。

参见 [llvm/docs/LinkTimeOptimization.md:87-99](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/docs/LinkTimeOptimization.md#L87-L99)——这段描述了「删 `foo2` → 删 `foo3` → 删 `foo4`」的级联死代码消除，是 LTO 跨文件优化的经典例子。

ThinLTO 后端阶段的划分在 `LTOBackend.cpp` 里写得很清楚——这是 `-Wl,-plugin-opt=save-temps` 会落盘的那几个中间文件名的来源：

[llvm/lib/LTO/LTOBackend.cpp:169-192](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/LTO/LTOBackend.cpp#L169-L192) —— 把 ThinLTO 后端拆成 `0.preopt → 1.promote → 2.internalize → 3.import → 4.opt → 5.precodegen` 六个钩子，外加一个 `combinedindex`（合并索引）。每个钩子对应一个「保存中间产物」的时机，这正是我们在综合实践里要逐一观察的阶段。

```cpp
// 简化自 LTOBackend.cpp:169-192
setHook("0.preopt",      PreOptModuleHook);
setHook("1.promote",     PostPromoteModuleHook);
setHook("2.internalize", PostInternalizeModuleHook);
setHook("3.import",      PostImportModuleHook);
setHook("4.opt",         PostOptModuleHook);
setHook("5.precodegen",  PreCodeGenModuleHook);
CombinedIndexHook = SaveCombinedIndex;
```

这些钩子的存在本身就回答了「ThinLTO 到底分几步」——它不是黑盒，而是一条带明确中间产物的流水线。

#### 4.1.4 代码实践

**实践目标**：亲手感受两种 LTO 编译产物形态的根本差异——全 LTO 的 `.o` 是普通位码，ThinLTO 的 `.o` 也是位码，但链接期行为截然不同。

**操作步骤**：

1. 准备两个源文件（复用官方示例的思路）：

```c
// a.h
int foo1(void);

// a.c
#include "a.h"
static int foo3(void) { return 10; }
int foo1(void) { return foo3() + 42; }

// main.c
#include <stdio.h>
#include "a.h"
int main(void) { printf("%d\n", foo1()); return 0; }
```

2. 分别用两种 LTO 编译，注意 `.o` 的本质：

```bash
# 全 LTO
clang -flto -c a.c -o a.full.o
clang -flto -c main.c -o main.full.o

# ThinLTO
clang -flto=thin -c a.c -o a.thin.o
clang -flto=thin -c main.c -o main.thin.o
```

3. 用 `file` 和 `llvm-nm` 查看：两种 `.o` 都不是普通机器码对象，而是 LLVM 位码（`file` 会报 `LLVM IR bitcode`）；`llvm-nm a.thin.o` 能看到符号表（`foo1` 等）。

**需要观察的现象**：

- `file *.o` 报告它们是 bitcode 而非 ELF 机器码——这印证了「LTO 把优化推迟到链接期」。
- 用 `llvm-dis a.thin.o -o -` 可以把位码反汇编成可读的 `.ll`，看到 `foo3` 是 `internal` 链接类型。

**预期结果**：两种产物在编译期看起来相似（都是位码），真正的差别要在链接期才显现（见综合实践）。若本地未装 clang/llvm 工具链，则**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：全 LTO 的链接阶段为什么「增量构建几乎不可能」？

> **参考答案**：因为全 LTO 要把所有位码合并成单一 `Module` 后整体优化，改动任何一个源文件都会让这个合并体（以及其上的所有过程间分析结果）失效，必须整体重做，无法像普通编译那样只重编受影响的那一个文件。

**练习 2**：ThinLTO 用「摘要」代替完整 IR 来做跨模块分析，这种替换在什么前提下才是安全的？

> **参考答案**：前提是摘要必须包含做该分析所需的全部信息。例如做跨模块内联决策，摘要里就得有「函数指令数（大小）」「它调用了谁（调用边）」「调用热度」——这些正是 `FunctionSummary` 的 `instCount()`、`calls()` 与 `CalleeInfo::Hotness`。只要摘要完备，基于摘要的分析就与基于完整 IR 的分析等价，但代价低得多。

### 4.2 Module Summary Index：摘要索引的构建与结构

#### 4.2.1 概念说明

跨模块分析若每次都去读所有模块的完整 IR，代价巨大。Module Summary Index 的设计思想是：**给每个全局值拍一张精简的「证件照」**，只记关键信息（多大、引用谁、被谁引用、可不可导入），扔掉函数体。分析时只看证件照，只在真正需要搬代码时才回去取完整 IR。

整个体系有三层概念：

- **`GlobalValueSummary`**：所有摘要的抽象基类，分三种具体子类——`FunctionSummary`（函数）、`GlobalVarSummary`（全局变量）、`AliasSummary`（别名）。
- **`FunctionSummary`**：函数的「证件照」，核心字段是指令数 `InstCount` 与一组**调用边**（call edges）。
- **`ModuleSummaryIndex`**：把一个模块（thin link 时则是整个程序所有模块）的所有摘要装在一起的容器，外加一张「模块路径字符串表」（记录每个 GUID 来自哪个模块文件）。

跨模块识别靠 **GUID**：它是符号名经哈希得到的 64 位整数，定义在 `GlobalValue` 上：

[llvm/include/llvm/IR/GlobalValue.h:585](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/GlobalValue.h#L585) —— `using GUID = uint64_t;`。这就是跨模块分析的「主键」类型。

#### 4.2.2 核心流程

构建一个模块的摘要索引流程：

```text
buildModuleSummaryIndex(M)
  |
  |  收集 llvm.used 等不可导出的局部符号
  |  for 每个「有定义」的函数 F in M:
  |      computeFunctionSummary(F):
  |          遍历每条指令:
  |              NumInsts++（统计大小）
  |              若是 call 指令 -> 记一条调用边 (被调用者 GUID, 热度)
  |          组装 FunctionSummary(NumInsts, 调用边列表, FFlags...)
  |          Index.addGlobalValueSummary(F, summary)
  |  for 每个全局变量 / 别名 -> 类似建摘要
  |  标记 live root（llvm.used、llvm.global_ctors 等）
  |  return Index
```

关键点：摘要是在**单遍遍历指令**时顺手建出来的，开销很低——它只是对 IR 做一次投影，并不改变 IR。这也正是为什么 thin link 阶段读取所有摘要比读取所有完整 IR 快得多。

#### 4.2.3 源码精读

**（1）摘要的入口与构建**

`buildModuleSummaryIndex` 是建摘要的总入口，它先收集 `llvm.used`/`llvm.compiler.used` 里那些不能被导出（否则要改名提升）的局部符号，然后对每个有定义的函数调用 `computeFunctionSummary`：

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:966-980](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L966-L980) —— 构造 `ModuleSummaryIndex`，并从模块标志位读出 `EnableSplitLTOUnit`、`UnifiedLTO`。注意它新建的索引带 `HaveGVs=true`，意味着此刻还持有指向真实 `GlobalValue` 的指针（因为 IR 就在手边）。

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:1077-1098](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L1077-L1098) —— 对模块里每个非声明的函数，先建支配树（DT）与可能的 BlockFrequencyInfo（BFI，用于算热度），再调用 `computeFunctionSummary`。BFI 只在有 profile 数据时才构造，体现了「按需付代价」。

**（2）`computeFunctionSummary`：指令计数与调用边**

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:341-356](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L341-L356) —— 函数签名与本地状态：`NumInsts` 计指令数，`CallGraphEdges` 用 `MapVector` 累积「被调用者 → 热度信息」的映射。

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:401-404](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L401-L404) —— 遍历基本块里每条指令时，跳过 debug/pseudo 指令后 `++NumInsts`。这个 `NumInsts` 就是 4.3 导入决策里用来衡量「函数有多大」的指标。

调用边的记录在处理 `call` 指令时完成：

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:472-495](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L472-L495) —— 对每个直接调用：用 `PSI->getProfileCount` 取该调用点的 profile 计数，经 `getHotness` 映射成 `Hot/Cold/None/Unknown`，然后 `CallGraphEdges[被调用者].updateHotness(...)` 记下这条边；若是尾调用，再 `setHasTailCall(true)`。注意这里累加的是 `GlobalValue`（含别名）的 ValueInfo，而非 `Function`——别名会另建 `AliasSummary` 关联。

热度计算本身很简单：

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:203-212](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L203-L212) —— 依 profile 计数是否超过 `isHotCount`/`isColdCount` 阈值，分别判为 Hot / Cold / None。

最后，把累积的 `NumInsts` 与调用边列表组装成 `FunctionSummary`：

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:754-763](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L754-L763) —— `make_unique<FunctionSummary>(Flags, NumInsts, FunFlags, Refs, CallGraphEdges.takeVector(), ...)`，再 `Index.addGlobalValueSummary(F, ...)`。`FunFlags`（`NoInline`/`AlwaysInline`/`NoRecurse` 等）也会被记录，它们会直接影响 4.3 的导入决策。

**（3）摘要的数据结构**

`FunctionSummary` 的关键字段——指令数与调用边：

[llvm/include/llvm/IR/ModuleSummaryIndex.h:1004-1014](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L1004-L1014) —— `unsigned InstCount;`（忽略 debug 指令的指令数）与 `SmallVector<EdgeTy, 0> CallGraphEdgeList;`（调用边列表）。注释说明用 `SmallVector<ValueInfo, 0>` 而非 `std::vector` 是为了更小的内存占用——摘要对象成千上万，省内存很关键。

调用边的类型与访问器：

[llvm/include/llvm/IR/ModuleSummaryIndex.h:829-830](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L829-L830) —— `using EdgeTy = std::pair<ValueInfo, CalleeInfo>;`，即「被调用者 + 该调用点的热度信息」。

[llvm/include/llvm/IR/ModuleSummaryIndex.h:1085-1089](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L1085-L1089) —— `instCount()` 与 `calls()` 两个访问器，正是 4.3 的代价模型与调用图遍历所调用的接口。

`ModuleSummaryIndex` 容器本身：

[llvm/include/llvm/IR/ModuleSummaryIndex.h:1518-1525](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L1518-L1525) —— 持有两样核心数据：`GlobalValueMap`（GUID → 摘要列表）与 `ModulePathStringTable`（模块路径字符串表，记录每个 GUID 归属哪个模块文件）。后者是跨模块搬运时「去哪个文件取函数体」的依据。

> 为什么是「摘要**列表**」而非单个摘要？因为同一个 GUID 可能有多个定义（COMDAT、weak 符号、不同模块的同名局部），见 `GlobalValueSummaryInfo::SummaryList` 的注释（[ModuleSummaryIndex.h:160-166](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L160-L166)）。

最后看一个版本号常量，它解释了「位码里的摘要格式也会演进」：

[llvm/include/llvm/IR/ModuleSummaryIndex.h:1646](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/IR/ModuleSummaryIndex.h#L1646) —— `static constexpr uint64_t BitcodeSummaryVersion = 14;`，注释说明「每当摘要记录的解释方式（如某些标志位）发生变化就递增，且需同步改 BitcodeReader/Writer」。

**（4）把摘要写进位码：`ThinLTOBitcodeWriter`**

ThinLTO 编译期会写**两份**位码：一份完整的（含函数体，给后端用），一份极简的（**只含摘要**，给 thin link 用）。后者体积小得多，使得 thin link 不必读完整 IR。

[llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp:569-614](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp#L569-L614) —— `writeThinLTOBitcode`：先用 `WriteBitcodeToFile(M, OS, ..., Index, /*GenerateHash=*/true, &ModHash)` 写完整位码并算出模块哈希 `ModHash`；若请求了 thin link 产物（`ThinLinkOS`），再调 `writeThinLinkBitcodeToFile(M, *ThinLinkOS, *Index, ModHash)` 只写摘要与哈希。

当模块含类型元数据（CFI/去虚化）需要拆分时，走更复杂的 `splitAndWriteThinLTOBitcode`：

[llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp:300-322](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp#L300-L322) —— 它把模块拆成 regular LTO 部分（含类型元数据、需在合并模块里处理）与 thin LTO 部分，分别写到一个多模块位码文件里。

这个 Pass 作为新 PM 的 `ThinLTOBitcodeWriterPass` 被接入流水线：

[llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp:620-624](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/ThinLTOBitcodeWriter.cpp#L620-L624) —— `ThinLTOBitcodeWriterPass::run` 把 AAResults 等通过 lambda 喂给 `writeThinLTOBitcode`。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「一个模块的摘要」长什么样，并理解它为什么远比完整 IR 紧凑。

**操作步骤**：

1. 把上一节的 `a.c` 先单独编译成文本 IR：`clang -S -emit-llvm a.c -o a.ll`。
2. 用 `opt` 的隐藏选项把摘要导出成 **DOT 调用图**（这是个真实存在的选项）：

```bash
opt -passes='module-summary' -module-summary-dot-file=a.dot a.ll -o /dev/null
# 若 -passes 文本流水线名不适用, 可改用旧式:
opt -module-summary -module-summary-dot-file=a.dot a.ll -o /dev/null
```

[llvm/lib/Analysis/ModuleSummaryAnalysis.cpp:81-83](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L81-L83) 定义了 `-module-summary-dot-file` 选项；[ModuleSummaryAnalysis.cpp:1168-1175](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L1168-L1175) 在索引建完后调用 `Index.exportToDot(OSDot, {})` 落盘。

3. 用 `dot -Tsvg a.dot -o a.svg`（Graphviz）渲染，或直接读 `a.dot` 文本。

**需要观察的现象**：

- DOT 图里每个节点是一个有定义的全局值，每条边是一条 call（如 `main → foo1 → foo3`）。
- 节点上会带 `inst`（指令数）、`flags`（如 noinline/readonly）等摘要字段。

**预期结果**：你会看到一张调用图，节点数 = 该模块有定义的函数数，远小于完整 IR 的指令数——这就是「摘要比 IR 紧凑」的直观证明。若 `opt` 版本不支持该子命令名，则**待本地验证**确切命令形式。

#### 4.2.5 小练习与答案

**练习 1**：`FunctionSummary` 的 `InstCount` 是怎么算出来的？它包含了 debug 指令吗？

> **参考答案**：在 `computeFunctionSummary` 里遍历函数的每条指令，遇到 debug/pseudo 指令 `continue` 跳过，否则 `++NumInsts`（[ModuleSummaryAnalysis.cpp:402-404](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Analysis/ModuleSummaryAnalysis.cpp#L402-L404)）。因此它**不包含** debug 指令，反映的是「真实可执行代码的规模」。

**练习 2**：为什么 `ModuleSummaryIndex` 里同一个 GUID 可能对应「一组」摘要而非单个？

> **参考答案**：因为存在 COMDAT、weak 符号、以及不同模块里同名局部（编译时未带区分路径）等情况，同一个名字（同一 GUID）可能在多个模块都有定义，每份定义各对应一个 summary，所以用 `GlobalValueSummaryList`（一个 vector）保存。thin link 时再由「prevailing（胜出）」机制选定其中一份作为主定义。

### 4.3 FunctionImport：跨模块内联的决策与执行

#### 4.3.1 概念说明

有了摘要索引，ThinLTO 就能在 thin link 阶段回答一个关键问题：**为了让后端能做内联，每个模块该从别的模块搬进来哪些函数？** 这就是 FunctionImport。

直觉是：如果 `main.c` 里的 `main` 调用了 `a.c` 里的 `foo1`，而 `foo1` 又小又热，那么把 `foo1` 的函数体拷贝进 `main` 所在的模块，后端就能把它内联进 `main`，省掉一次跨模块调用。注意——**搬进来的是一份拷贝，原定义仍在原模块**，这纯粹是为了让本模块的优化器「够得着」它去内联。

但无脑全搬会撑爆目标模块、丢失并行性，所以需要一个**代价模型**：函数太大不搬、调用点是冷的（cold）不搬、带 `noinline` 的不值得搬。模型用一个「指令数预算（阈值）」来度量，并按调用点的**热度**调节预算——热调用给的预算高，冷调用给的预算为 0。

涉及两个对称的产物：

- **ImportList（导入清单）**：目标模块 → 它要从哪些源模块导入哪些 GUID。
- **ExportList（导出清单）**：源模块 → 它有哪些 GUID 被别处导入了（搬走后，本地对这些符号的处理要相应调整）。

#### 4.3.2 核心流程

导入决策在调用图上做一次**带阈值衰减的 DFS**：

```text
对每个目标模块 DM, 对 DM 里每个有定义的函数 F (作为 DFS 起点):
    computeImportForFunction(F, Threshold=ImportInstrLimit):
        for F 的每条调用边 (callee, hotness):
            NewThreshold = Threshold * 热度乘数(hotness)
            # 热边: NewThreshold = Threshold*10; 冷边: NewThreshold = 0
            若 callee 已在 DM 内有定义 -> 跳过
            选一个合格被调用者 summary:
                selectCallee: 若 callee.instCount() <= NewThreshold
                              且 非 noinline -> 选定, 加入 ImportList
            若选定: 把 callee 也压入 worklist, 用 NewThreshold 继续向下 DFS
```

热度乘数的代价模型：每条调用边把当前阈值乘以一个由热度决定的乘数 m(h)，得到传给被调用者的新阈值。

\[ T_{\text{new}} = T \cdot m(h) \]

其中乘数 m(h) 按调用点热度取值如下：

| 热度 h | 乘数 m(h) | 默认值 |
| --- | --- | --- |
| Hot | ImportHotMultiplier | 10.0 |
| Critical | ImportCriticalMultiplier | （见源码默认） |
| Cold | ImportColdMultiplier | 0 |
| 其他（Unknown / None） | 1.0 | — |

基础阈值 \( T = \text{ImportInstrLimit} \)，默认 100。于是热边的预算被放大到约 1000 条指令，冷边预算为 0（永不导入）。最终导入判据为：

\[ \text{instCount}(\text{callee}) \le T_{\text{new}} \]

由于 DFS 向下传递的是 NewThreshold，越深的调用链预算可能越小（除非又遇到热边），从而天然抑制「搬太多」。

执行阶段则简单直接：拿着 ImportList，用 `IRMover` 把选中函数的定义从源模块链接进目标模块（只搬指定的那些符号，不是整个模块）。

#### 4.3.3 源码精读

**（1）代价模型的参数**

[llvm/lib/Transforms/IPO/FunctionImport.cpp:86-88](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L86-L88) —— `ImportInstrLimit`，导入函数的指令数上限，默认 100。

[llvm/lib/Transforms/IPO/FunctionImport.cpp:108-110](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L108-L110) 与 [FunctionImport.cpp:119-121](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L119-L121) —— `ImportHotMultiplier` 默认 `10.0`、`ImportColdMultiplier` 默认 `0`。这正是上面公式里的两个关键乘数。

**（2）`selectCallee`：选定一个合格被调用者**

[llvm/lib/Transforms/IPO/FunctionImport.cpp:307-326](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L307-L326) —— 核心判据：

```cpp
// 简化自 FunctionImport.cpp:324-331
if ((Summary->instCount() > Threshold) && !Summary->fflags().AlwaysInline
    && !ForceImportAll) {
  Reason = ImportFailureReason::TooLarge;   // 太大, 不导入
  continue;
}
if (Summary->fflags().NoInline && !ForceImportAll) {
  Reason = ImportFailureReason::NoInline;   // 带 noinline, 导入了也无法内联
  continue;
}
return Summary;                             // 选定
```

注意 `AlwaysInline` 是个例外：即便函数大于阈值，只要标了 `always_inline`，依然导入（因为它一定会被内联掉，值回票价）。

**（3）`computeImportForFunction`：DFS 与阈值衰减**

[llvm/lib/Transforms/IPO/FunctionImport.cpp:887-934](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L887-L934) —— 对每条调用边：`NewThreshold = Threshold * GetBonusMultiplier(Edge.second.getHotness())`；若 callee 已在目标模块有定义则跳过；用 `selectCallee` 在 `NewThreshold` 下选 callee。关键细节（[FunctionImport.cpp:951-963](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L951-L963)）：因为 DFS，同一个函数可能被第二次访问且这次带着更高的阈值——此时更新 `ProcessedThreshold` 并把它重新压回 worklist，让它的下游调用链也能享受这个更高预算。

[llvm/lib/Transforms/IPO/FunctionImport.cpp:923-934](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L923-L934) —— `GetBonusMultiplier` 的 lambda：Hot→`ImportHotMultiplier`、Cold→`ImportColdMultiplier`、Critical→`ImportCriticalMultiplier`、其他→1.0。

**（4）`ComputeCrossModuleImport`：thin link 的顶层编排**

[llvm/lib/Transforms/IPO/FunctionImport.cpp:1232-1247](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L1232-L1247) —— 对每个有函数定义的模块，调用 `computeImportForModule` 算出它的 `ImportList`。

[llvm/lib/Transforms/IPO/FunctionImport.cpp:1249-1293](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L1249-L1293) —— 随后补全 **ExportList**：凡是被导入的定义，它所引用/调用的值也要标记为导出（否则搬过去的函数体引用了「看不见」的符号会出错）。

**（5）决策用的数据结构**

[llvm/include/llvm/Transforms/IPO/FunctionImport.h:185-238](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Transforms/IPO/FunctionImport.h#L185-L238) —— `ImportMapTy`：记录「从哪个源模块导入哪个 GUID、按定义还是按声明」。注意它区分两种导入：导入**定义**（`addDefinition`，可被内联）与导入**声明**（`maybeAddDeclaration`，只为把摘要上的属性标注到本模块的声明上）。定义优先级高于声明——若同一 GUID 既有定义又有声明导入，定义胜出（[FunctionImport.cpp:351-363](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L351-L363)）。

[llvm/include/llvm/Transforms/IPO/FunctionImport.h:273-300](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Transforms/IPO/FunctionImport.h#L273-L300) —— `ImportListsTy`：「目标模块 → ImportMapTy」的映射，是整个 thin link 的最终产物之一。

[llvm/include/llvm/Transforms/IPO/FunctionImport.h:305](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/include/llvm/Transforms/IPO/FunctionImport.h#L305) —— `using ExportSetTy = DenseSet<ValueInfo>;`。

**（6）真正搬代码：`importFunctions`**

决策完成后，后端用 `IRMover` 执行搬运。`IRMover` 负责把一个模块里的指定全局值链接进另一个模块（只搬指定符号，并正确处理类型、符号重命名等）：

[llvm/lib/Transforms/IPO/FunctionImport.cpp:1946-1964](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L1946-L1964) —— `importFunctions`：对 ImportList 里每个源模块，用 `ModuleLoader` 加载它，逐个找出要导入的函数，组成 `GlobalsToImport`，最后交给 `IRMover Mover(DestModule)` 完成链接。`IRMover` 在 [u8-l3 LLD](u8-l3-lld-linker.md) 的「链接即合并 IR」视角下会更易理解——它就是「把别模块的 IR 增量并进本模块」的引擎。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「跨模块调用是否被导入」的决策，理解代价模型如何起作用。

**操作步骤**：

1. 准备一个能让内联有收益的小项目，让 `main` 调用一个**小**函数 `add` 和一个**大**函数 `big`：

```c
// util.c
int add(int a, int b) { return a + b; }            // 很小, 应被导入
int big(int x) {                                    // 故意写大
  int s = x;
  for (int i = 0; i < 100; ++i) s = s * 3 + i;      // 远超 100 条指令
  return s;
}
// main.c
#include <stdio.h>
int add(int, int); int big(int);
int main(void) { printf("%d %d\n", add(1,2), big(10)); return 0; }
```

2. 用 ThinLTO 构建，并打开导入统计的 debug 输出：

```bash
clang -O2 -flto=thin -c util.c main.c
clang -O2 -flto=thin util.o main.o -o prog -Wl,-plugin-opt=-debug=function-import 2> import.log
# 较新版本可用: -Wl,-mllvm,-debug-only=function-import
```

3. 在 `import.log` 里搜 `main` 模块对 `add` 和 `big` 的导入决策。

**需要观察的现象**：

- 日志会显示 `edge -> ... Threshold:100` 之类行（[FunctionImport.cpp:897](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L897)），以及 `ignored! ...` 或导入成功。
- 预期：`add`（指令数远小于 100）被导入；`big`（指令数远超 100）因 `TooLarge` 被拒。

**预期结果**：你将看到 `add` 被搬进 `main.o` 所在后端、`big` 被留在 `util.o`——这正是代价模型 `instCount() > Threshold`（[FunctionImport.cpp:326](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L326)）的直观体现。具体日志格式与插件选项随版本变化，**待本地验证**确切开关。

#### 4.3.5 小练习与答案

**练习 1**：为什么「带 `noinline` 的函数不值得导入」？这和「函数太大不导入」是同一回事吗？

> **参考答案**：不是同一回事。「太大不导入」是因为搬一份大函数体进目标模块、却只在少量调用点用得到，性价比低（`instCount > Threshold`，[FunctionImport.cpp:326](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L326)）；「`noinline` 不导入」是因为导入的主要目的就是为了让后端能内联它，既然 `noinline` 注定不会被内联，那搬进来除了省一次跨模块调用外没有额外优化收益，性价比同样低（[FunctionImport.cpp:334](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L334)）。注意 `always_inline` 恰好相反，永远会被导入。

**练习 2**：`computeImportForFunction` 为什么是 DFS，并且为什么同一个被调用者可能被「第二次访问」？

> **参考答案**：DFS 是为了沿着调用链向下探索可导入的函数（A 调 B、B 调 C，若导入 A 就可能还想导入 C）。同一个被调用者可能从不同路径、以不同阈值被访问到；由于是 DFS，第二次访问时可能带着**更高**的阈值（比如这次来自一条热边）。代码在 [FunctionImport.cpp:951-963](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/Transforms/IPO/FunctionImport.cpp#L951-L963) 处理这种情况：若新阈值更高，就更新 `ProcessedThreshold` 并把该函数重新压回 worklist，让它的下游也能享受更高预算；若新阈值不高于已处理阈值，则直接跳过，避免重复工作。

## 5. 综合实践

**综合任务**：把本讲三个模块串起来——用同一个多文件项目，对比全 LTO 与 ThinLTO 的完整链接产物链，并验证跨模块内联确实发生了。

**步骤**：

1. 用 4.1.4 里的源文件（`a.c` / `main.c`）。

2. **全 LTO 链接**，观察「单一大 Module」：

```bash
clang -O2 -flto -c a.c main.c
clang -O2 -flto -fuse-ld=lld -Wl,-save-temps a.o main.o -o prog.full
```

观察 `0.0.*.bc`（合并后的大模块位码）等中间文件——全 LTO 把所有位码合并成单一 Module。

3. **ThinLTO 链接**，观察「分阶段 + 索引 + 导入」：

```bash
clang -O2 -flto=thin -c a.c main.c
clang -O2 -flto=thin -fuse-ld=lld \
  -Wl,-plugin-opt=save-temps \
  a.o main.o -o prog.thin
```

预期会看到形如 `*.index.bc`（合并摘要索引，对应 [LTOBackend.cpp:190-191](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/LTO/LTOBackend.cpp#L190-L191) 的 `combinedindex`）、`*.imports`（导入清单，对应 `EmitImportsFiles`）、以及各模块的 `*.0.preopt.bc` / `*.1.promote.bc` / `*.2.internalize.bc` / `*.3.import.bc` / `*.4.opt.bc`（对应 [LTOBackend.cpp:169-192](https://github.com/llvm/llvm-project/blob/2a4acc46ea711175ef5cfe6ea5a795f62221084a/llvm/lib/LTO/LTOBackend.cpp#L169-L192) 的六个钩子）。

4. **验证导入确实发生**：用 `llvm-dis` 把 `main.o.3.import.bc` 反汇编成 `.ll`，对比 `main.o.0.preopt.bc`，应能看到 `foo3`（原本只在 `a.c`）的函数体被搬进了 `main` 所在模块——这就是 4.3 `importFunctions` 经 `IRMover` 完成的工作。

5. **验证优化收益**：在 `main.o.4.opt.bc` 里检查 `main` 是否已把 `foo1`/`foo3` 内联（调用点消失、常量折叠出 `52 = 10 + 42`）。

**报告要点**（写一段总结）：

- 全 LTO 产生几个中间文件、是否串行；ThinLTO 产生几个、各模块是否独立。
- ThinLTO 的 `*.imports` 里列出了哪些被导入的符号。
- 内联是否真的在 `*.opt` 阶段发生，对应到本讲哪段源码逻辑。

> 提示：`-Wl,-plugin-opt=save-temps` 的具体可用值（如 `=preopt,promote,...`）与中间文件命名因 lld 版本而异，**待本地验证**。若本地无 lld，可改用 `llvm-lto2` 工具手动驱动 thin link 各阶段。

## 6. 本讲小结

- **LTO 把过程间优化推迟到链接期**，让优化器第一次拥有整个程序的视野；代价是链接变慢、且传统全 LTO 难以并行与增量。
- **ThinLTO 用「摘要索引」替代完整 IR 做跨模块分析**，把工作拆成 *thin link（分析）* 与 *后端（搬运+优化）* 两阶段，从而可并行、省内存、增量友好。
- **`ModuleSummaryIndex` 是核心数据结构**：`FunctionSummary` 用 `instCount()`（指令数）与 `calls()`（调用边带热度）记录函数的「证件照」，由 `buildModuleSummaryIndex`/`computeFunctionSummary` 在单遍遍历 IR 时构建，由 `ThinLTOBitcodeWriter` 同时写出完整位码与仅含摘要的最小位码。
- **FunctionImport 的决策是一个带阈值衰减的 DFS**：`NewThreshold = Threshold × 热度乘数`，热边放大约 10 倍、冷边归零，`selectCallee` 用 `instCount ≤ Threshold` 选定被调用者，最终用 `IRMover` 把选中函数的定义搬进目标模块。
- **跨模块识别靠 GUID**（符号名哈希），导入清单 `ImportMapTy` 与导出清单 `ExportSetTy` 是一对对称产物，由 `ComputeCrossModuleImport` 在 thin link 阶段一次性算出。
- ThinLTO 后端的 `promote → internalize → import → opt → precodegen` 是真实存在的钩子阶段，可用 `-Wl,-plugin-opt=save-temps` 落盘观察。

## 7. 下一步学习建议

- **继续 u8 单元**：[u8-l3 LLD 链接器架构](u8-l3-lld-linker.md) 会讲链接器本体，你会更清楚 LTO 是如何作为「插件」嵌入 ELF/COFF 链接的，以及 `IRMover` 与 LLD 符号解析的关系。
- **阅读 `llvm/lib/LTO/LTO.cpp` 与 `LTOBackend.cpp`**：这是 thin link 编排与后端阶段调度的「主控台」，把本讲的 `ComputeCrossModuleImport` 与后端流水线真正串起来的地方；尤其关注它如何调用 `FunctionImporter` 与 `IRMover`。
- **配合 [u9-l3 调试信息与 LLDB](u9-l3-debuginfo-lldb.md)**：LTO 会重排与内联代码，调试信息如何在合并/导入后保持与源码的对应，是工程实践中的高频痛点。
- **深入读 `llvm/tools/llvm-lto2/llvm-lto2.cpp`**：这个命令行工具是手工驱动 LTO/ThinLTO 各阶段的最直接入口，适合作为下一个「读懂调用链」的目标。
