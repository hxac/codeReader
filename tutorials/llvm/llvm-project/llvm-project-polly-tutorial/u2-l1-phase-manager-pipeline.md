# PhaseManager：Polly 阶段流水线全景

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 Polly 在一个函数上完整执行的阶段顺序，并能解释**为什么是这个顺序**。
- 区分两类截然不同的「修改」：多面体阶段只改内存里的 `Scop` 模型，只有 `prepare` 与 `codegen` 两个阶段真正改写 LLVM-IR。
- 理解为什么 `PhaseManager` 故意**不是**任何一个 Pass Manager 里的 pass，以及为什么 `LoopInfo`/`DominatorTree`/`ScopInfo` 必须在所有阶段间保持有效、绝不能被 Pass Manager 失效。
- 读懂 `PhaseManager::run()` 这一个函数，把它当作后续每一讲的「目录」——U3–U8 讲的每个具体 pass，都对应这里的一行 `runXxxPass`。

> 本讲是整本手册的**枢纽讲义**。它不深入任何一个具体算法，而是把整条流水线一次性铺开，让你建立全局地图。之后每读一讲，都可以回到本讲定位它在地图上的位置。

## 2. 前置知识

本讲承接 u1-l1（多面体模型基础）与 u1-l4（插件入口与 Pass 注册），默认你已经知道：

- **SCoP（Static Control Part）**：函数里一段「控制流与访存都可被仿射（线性）描述」的区域，是 Polly 的优化单元。
- **多面体三件套**：迭代域（Domain）+ 访问关系（Access Relation）+ 调度（Schedule）。U4 会深入，本讲只需知道这三样被装进一个叫 `Scop` 的 C++ 对象里。
- **New Pass Manager**：LLVM 现在的 pass 调度框架，pass 通过 `run()` 方法被调用，分析结果由 `FunctionAnalysisManager`（FAM）缓存与失效。
- **`PollyFunctionPass`**：u1-l4 讲过，Polly 对外暴露的函数级 pass；它的 `run()` 最终调用本讲的 `runPollyPass()` → `PhaseManager::run()`。

本讲新增两个最小模块：**Pass 阶段模型**（Polly 怎么用枚举 + 位集描述「跑哪些阶段」）和 **Region/Scop 概念**（Polly 怎么在区域树上逐个 SCoP 推进流水线）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/polly/Pass/PhaseManager.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h) | 定义 `PassPhase` 枚举（阶段表）、`PollyPassOptions`（位集选项）、`runPollyPass()` 入口。 |
| [lib/Pass/PhaseManager.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp) | 实现 `PhaseManager::run()`（整条流水线）以及阶段名解析、选项一致性检查。 |
| [include/polly/Pass/PollyFunctionPass.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PollyFunctionPass.h) | `PollyFunctionPass`，把 `PhaseManager` 包成 LLVM 能调用的 pass。 |
| [lib/Pass/PollyFunctionPass.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PollyFunctionPass.cpp) | `run()` 调 `runPollyPass()`，并把「是否改了 IR」翻译成 `PreservedAnalyses`。 |
| [lib/Support/RegisterPasses.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp) | `parsePollyOptions()`：把命令行 / `-passes=polly<...>` 解析成 `PollyPassOptions`。 |
| [lib/Support/PollyPasses.def](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/PollyPasses.def) | X-Macro：登记 `polly` / `polly-custom` 两个 pass 名及其参数解析器。 |

---

## 4. 核心概念与源码讲解

### 4.1 Pass 阶段模型

#### 4.1.1 概念说明

Polly 把自己拆成了一串**阶段（phase）**，每个阶段做一件独立的事（检测、建模、求依赖、变换、生成代码……）。这串阶段不是写死在代码顺序里的「无条件执行」，而是用一张**阶段表**（枚举）+ 一个**开关位集**来描述「这次运行要跑哪几个阶段」。

为什么要这样设计？因为 Polly 既是给最终用户用的编译器（要跑全套），也是给开发者调试用的实验台（经常要「只跑到 ast 就停」「只跑 simplify 看效果」）。用枚举 + 位集，同一份 `PhaseManager::run()` 既能跑全流水线，也能跑任意子集，全靠 `Opts` 控制。

#### 4.1.2 核心流程

阶段的生命周期是：

```
命令行 / -passes=polly<...>
        │  parsePollyOptions()
        ▼
PollyPassOptions（一个位集：每个阶段 on/off）
        │  checkConsistency()  ← 拒绝非法组合（如开了 codegen 却没开 ast）
        ▼
runPollyPass() → PhaseManager::run()
        │  对每个阶段：if (Opts.isPhaseEnabled(phase)) runXxxPass(...)
        ▼
按枚举顺序，逐阶段执行
```

阶段在枚举里的**声明顺序就是执行顺序**。这是后文 `parsePollyOptions` 强制「参数必须按序、不可重复」的根本原因——顺序不是约定，是语义。

#### 4.1.3 源码精读：阶段表与位集

阶段枚举定义在头文件里，按执行顺序排列：

[include/polly/Pass/PhaseManager.h:32-67](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h#L32-L67) —— `enum class PassPhase`，从 `Prepare` 到 `CodeGen`，并定义 `PassPhaseFirst = Prepare`、`PassPhaseLast = CodeGen` 作为遍历边界。

完整数一遍，去掉 `None` 后枚举共有 **24 个**阶段。但其中 **7 个是「诊断/打印/可视化」辅助阶段**（`PrintDetect`、`DotScops`、`DotScopsOnly`、`ViewScops`、`ViewScopsOnly`、`PrintScopInfo`、`PrintDependences`），它们只输出信息、不改变任何状态。剩下的 **17 个是「实质阶段」**——真正做检测、建模、变换、生成的阶段。这正是实践任务里「17 个 PassPhase」所指的范围（见 [4.2 节](#42-源码精读run-的逐阶段拆解) 的流程图）。

`PollyPassOptions` 用一个 `llvm::Bitset` 存每个阶段是否启用：

[include/polly/Pass/PhaseManager.h:74-101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h#L74-L101) —— 注意位下标是「阶段值减去 `PassPhaseFirst`」做平移，因为 `None` 占了 0 位但不参与。读写分别走 `isPhaseEnabled` / `setPhaseEnabled`。

> **小贴士**：用位集而非 `bool` 数组，是因为阶段数量固定且小，位集省内存、利于整体拷贝。`PollyPassOptions` 是值类型，会被 `std::move` 进 `PhaseManager` 构造函数。

#### 4.1.4 源码精读：阶段名 ↔ 枚举的双向映射

用户在命令行写的是字符串（如 `simplify-0`、`opt-isl`），代码里是枚举。两者靠两个函数互转：

[lib/Pass/PhaseManager.cpp:268-321](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L268-L321) —— `getPhaseName()`，枚举 → 字符串。注意 `Optimization` 阶段对应的名字是 `"opt-isl"` 而非 `"opt"`，源码注释明确写「`opt` 会和 LLVM 的可执行文件名冲突」。

[lib/Pass/PhaseManager.cpp:323-350](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L323-L350) —— `parsePhase()`，字符串 → 枚举，用 `StringSwitch` 实现，未匹配返回 `None`。

这两个函数是「命令行 ↔ 内部」的唯一翻译层。你在 `-passes=polly-custom<simplify-0;stopafter=ast>` 里写的每个名字，都先过 `parsePhase`。

#### 4.1.5 源码精读：默认选项与一致性检查

`PollyPassOptions` 提供三个「批量开关」方法，对应三种典型用法：

[lib/Pass/PhaseManager.cpp:373-379](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L373-L379) —— `enableEnd2End()`：只开「能让 IR 进、IR 出」的**最小骨架**——`Detection`、`ScopInfo`、`Dependences`、`AstGen`、`CodeGen`。注意它**不**开任何优化变换，所以单独 `end2end` 会原样回写 IR（验证端到端通路用）。

[lib/Pass/PhaseManager.cpp:381-389](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L381-L389) —— `enableDefaultOpts()`：开**默认优化**——`Prepare`、`Simplify0`、`Optree`、`DeLICM`、`Simplify1`、`PruneUnprofitable`、`Optimization`。这是 Polly 默认会跑的那些变换。注意它**不**开 `Detection`/`ScopInfo`/`AstGen`/`CodeGen`——那些由 `enableEnd2End` 负责。所以默认 `-polly` 实际是「end2end + default-opts」的并集。

[lib/Pass/PhaseManager.cpp:391-398](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L391-L398) —— `disableAfter(Phase)`：从给定阶段的**下一个**阶段起全部关闭。配合 `stopafter=ast` 这种调试需求：只跑到某阶段、其后的都不执行。

最后是「守门员」`checkConsistency()`：

[lib/Pass/PhaseManager.cpp:400-436](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L400-L436) —— 它拒绝三类非法组合：
1. 开了任何非 `Prepare`/`Detection` 的阶段，却没开 `Detection`；
2. 开了 `ScopInfo` 之后的阶段，却没开 `ScopInfo`；
3. 开了「依赖依赖分析」的阶段（`dependsOnDependenceInfo` 返回真），却没开 `Dependences`；
4. 开了 `CodeGen` 却没开 `AstGen`（codegen 必须先有 AST）。

其中第 3 条用到的 `dependsOnDependenceInfo` 单独定义：

[lib/Pass/PhaseManager.cpp:352-371](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L352-L371) —— 返回 `true`（即「逻辑上需要 deps 选项」）的阶段只有：`PrintDependences`、`DeadCodeElimination`、`MaximumStaticExtension`、`Optimization`。

> **易混淆点（重要）**：`dependsOnDependenceInfo` 描述的是「**选项层面**是否要求 `deps` 选项被打开」，**不等于**「实现里是否真的用到 `DependenceAnalysis::Result`」。事实上 `ImportJScop`/`AstGen`/`CodeGen` 在 `run()` 里都接收了 `DA` 对象（见 [4.2.3](#423-关键细节deps-阶段其实只控制打印)），但被判定为「不依赖 deps」。原因见下一节——`DA` 永远按需计算，`deps` 选项只控制是否打印。所以这是一个**一致性约定**，而非实现事实。

#### 4.1.6 代码实践：用 polly-custom 验证选项语义

**实践目标**：亲手验证 `polly` 与 `polly-custom` 的默认行为差异，并触发一次 `checkConsistency` 报错。

**操作步骤**（基于 u1-l3 的 clang/opt 用法）：

1. 准备一个含嵌套循环的 C 文件 `mm.c`，先用 `clang -O1 -Xclang -disable-O0-optnone -emit-llvm -S mm.c -o mm.ll` 产出可优化的 IR（`-O1` 避免给函数加 `optnone`，否则 Polly 会跳过它）。
2. 跑全套默认：`opt -passes='polly' -disable-output mm.ll`（应正常通过）。
3. 跑空白自定义：`opt -passes='polly-custom<>' -disable-output mm.ll`（`polly-custom` 不开任何默认，应当几乎什么都不做、也不报错）。
4. 故意构造非法组合：`opt -passes='polly-custom<codegen>' -disable-output mm.ll`。

**需要观察的现象**：
- 步骤 4 应当**报错并退出**，错误信息形如 `inconsistent: 'codegen' requires 'ast' to be enabled`（来自 `checkConsistency` 末尾的检查）。
- 步骤 2、3 不报错。

**预期结果**：你亲眼看到「`polly` 默认开全套、`polly-custom` 默认全关、一致性检查会拦截非法组合」。若本地未配置带 Polly 的 opt，则上述命令结果**待本地验证**；此时可改为阅读源码：[RegisterPasses.cpp:238-239](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L238-L239) 里 `IsCustom` 决定了 `EnableDefaultOpts`/`EnableEnd2End` 的初值。

#### 4.1.7 小练习与答案

**练习 1**：`-passes=polly-custom<simplify-0;optree;delicm>` 会被 `checkConsistency` 接受吗？为什么实际跑起来却看不到任何 IR 变化？

**答案**：会被接受——`simplify-0`/`optree`/`delicm` 都不 `dependsOnDependenceInfo`，且 `parsePollyOptions` 会**隐式**把它们依赖的 `Detection`/`ScopInfo` 打开（见 [RegisterPasses.cpp:372-389](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L372-L389)）。看不到 IR 变化是因为没开 `AstGen`/`CodeGen`：所有变换只改了内存里的 `Scop` 模型，没有写回 IR。要看效果需加 `;ast;codegen` 或改用 `polly`。

**练习 2**：`getPhaseName(Optimization)` 为什么返回 `"opt-isl"` 而不是 `"opt"`？

**答案**：源码注释说 `"opt"` 会与 LLVM 的 `opt` 可执行文件名冲突，可能造成解析歧义。

---

### 4.2 阶段流水线与 `run()` 执行

#### 4.2.1 概念说明

`PhaseManager::run()` 是整本手册最关键的**一个函数**——它把上文的阶段表「活过来」，按顺序驱动 Polly 的所有 pass。

理解它要抓住三个层次：

1. **函数级一次**：`prepare`、`detection`、`scops` 只在整个函数上各跑一次（检测出若干 SCoP、构建出 `ScopInfo`）。
2. **SCoP 级多次**：从 `flatten` 到 `codegen` 的变换，是对**每一个检测到的 SCoP** 分别跑一遍（一个 `while` 循环）。
3. **分析保持**：`LoopInfo`、`DominatorTree`、`ScopInfo` 等 Polly 持有的状态，必须在整个 `run()` 期间始终有效——这正是 `PhaseManager` **不**作为普通 pass 嵌入 PM 的根本原因。

还要建立一个至关重要的区分：**多面体阶段改的是 `Scop` 对象（内存模型），不是 IR；只有 `prepare` 和 `codegen` 真正改 LLVM-IR。** 这是 Polly 架构的精髓——先把 IR 翻译成数学模型、在数学空间里做变换、最后再翻译回 IR。

#### 4.2.2 核心流程：整条流水线一览

`run()` 的大骨架（省略打印/可视化）：

```
PhaseManager::run(F)
├─ 取 LoopInfo、DominatorTree（必须全程保持有效）
├─ [prepare]      runCodePreparation        ← 规范化 IR（改 IR）
├─ [detect]       ScopDetection::detect      ← 找出 ValidRegions（函数级一次）
├─ [scops]        构造 ScopInfo Info(...)     ← 多面体模型容器（函数级一次）
│
└─ Worklist ← SD（每个检测到的区域）
   while (Region *R = Worklist.pop()) {
      Scop *S = Info.getScop(R);            // 按需构建该区域的 Scop
      if (!S || !SD.isMaxRegionInScop(R)) continue;
      ├─ [flatten]      runFlattenSchedulePass(S)        ── 改 Scop
      ├─ [deps]         DA = runDependenceAnalysis(S)    ── 求依赖（永远算）
      ├─ [import-jscop] runImportJSON(S, DA)             ── 改 Scop
      ├─ [simplify-0]   runSimplify(S, 0)                ── 改 Scop
      ├─ [optree]       runForwardOpTree(S)              ── 改 Scop
      ├─ [delicm]       runDeLICM(S)                     ── 改 Scop
      ├─ [simplify-1]   runSimplify(S, 1)（仅当有改动）  ── 改 Scop
      ├─ [dce]          runDeadCodeElim(S, DA)           ── 改 Scop
      ├─ [mse]          runMaximalStaticExpansion(S, DA) ── 改 Scop
      ├─ [prune]        runPruneUnprofitable(S)          ── 改 Scop
      ├─ [opt-isl]      runIslScheduleOptimizer(S, &TTI, DA) ── 改 Scop
      ├─ [export-jscop] runExportJSON(S)                 ── 改 Scop
      ├─ [ast]          IslAst = runIslAstGen(S, DA)     ── 生成 AST
      └─ [codegen]      runCodeGeneration(S, RI, *IslAst) ← 改 IR
                        若改了 IR → Info.invalidate()     ← 丢弃所有 Scop
   }
   return ModifiedIR
```

**17 个实质阶段的顺序流程图**（标注依赖关系与是否改 IR）：

| # | 阶段 | 关键调用 | 依赖 DependenceInfo？ | 改 IR？ |
| --- | --- | --- | --- | --- |
| 1 | prepare | `runCodePreparation` | 否 | **是** |
| 2 | detect | `ScopDetection::detect` | 否 | 否（改 RegionInfo） |
| 3 | scops | 构造 `ScopInfo` | 否 | 否 |
| 4 | flatten | `runFlattenSchedulePass` | 否 | 否 |
| 5 | deps | `runDependenceAnalysis` | （本身即依赖） | 否 |
| 6 | import-jscop | `runImportJSON` | 否（选项层）/ 用 DA | 否 |
| 7 | simplify-0 | `runSimplify(…,0)` | 否 | 否 |
| 8 | optree | `runForwardOpTree` | 否 | 否 |
| 9 | delicm | `runDeLICM` | 否 | 否 |
| 10 | simplify-1 | `runSimplify(…,1)` | 否 | 否 |
| 11 | dce | `runDeadCodeElim` | **是** | 否 |
| 12 | mse | `runMaximalStaticExpansion` | **是** | 否 |
| 13 | prune | `runPruneUnprofitable` | 否 | 否 |
| 14 | opt-isl | `runIslScheduleOptimizer` | **是** | 否 |
| 15 | export-jscop | `runExportJSON` | 否 | 否 |
| 16 | ast | `runIslAstGen` | 否（选项层）/ 用 DA | 否 |
| 17 | codegen | `runCodeGeneration` | 否（选项层）/ 用 IslAst | **是** |

「依赖 DependenceInfo？」一列对应 [4.1.5](#415-源码精读默认选项与一致性检查) 的 `dependsOnDependenceInfo`（选项一致性视角）。「改 IR？」一列只有 `prepare` 与 `codegen` 为「是」——这是本讲最重要的结论。

#### 4.2.3 源码精读：`run()` 的逐阶段拆解

**函数签名与位置**：[lib/Pass/PhaseManager.cpp:66-264](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L66-L264) —— 整个 `PhaseManager::run()`。

**(a) 取分析 & 全程保持有效**

[lib/Pass/PhaseManager.cpp:74-76](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L74-L76) —— 从 FAM 取出 `LoopInfo`、`DominatorTree`。上方注释（[L67-L73](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L67-L73)）解释了为什么这些分析必须全程保持：`ScopDetection` 内部**存有指向旧分析结果的引用**，一旦被 PM 失效重算，这些引用就悬空了——所以**不能**靠 PM 的失效/重算机制，必须手动保持。

这正是头文件顶部注释的核心论点：

[include/polly/Pass/PhaseManager.h:9-13](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h#L9-L13) —— `PhaseManager` 本身**不是**任何一个 PM 里的 pass，而是被 `PollyFunctionPass`/`PollyModulePass` 调用。这样它就能在一个 pass 内部手动控制所有阶段与分析的生命周期，绕开 PM 的自动失效。

**(b) prepare 阶段（改 IR）**

[lib/Pass/PhaseManager.cpp:81-89](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L81-L89) —— `runCodePreparation` 做规范化（u2-l3 详述）。它**改 IR**，所以设 `ModifiedIR = true`，并显式 `preserve<DominatorTreeAnalysis>()` / `preserve<LoopAnalysis>()` 后再 `FAM.invalidate`——即「我改了 IR，但 DT/LI 仍有效，请别重算」。注意它用 `FAM.invalidate(F, PA)` 时传的是一个**只保留** DT/LI 的 `PreservedAnalyses`，效果是「失效除 DT/LI 外的所有分析」。

**(c) detect 阶段（函数级一次）**

[lib/Pass/PhaseManager.cpp:100-106](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L100-L106) —— 注意 `RegionInfo` 是**就地新建**的（`RegionInfoAnalysis().run(F, FAM)`）而非从 FAM 取缓存，注释说因为 `ScopDetection` 会**修改** `RegionInfo`，不能用缓存版本。然后构造 `ScopDetection SD(...)` 并 `SD.detect(F)`。检测结果存在 `SD.ValidRegions`。

若 `Detection` 阶段没开（[L92-L93](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L92-L93)），直接 `return false`——没检测就什么都做不了。

**(d) scops 阶段（函数级一次）**

[lib/Pass/PhaseManager.cpp:136-138](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L136-L138) —— 构造唯一的 `ScopInfo Info(DL, SD, SE, LI, AA, DT, AC, ORE)`。它是后续所有变换共享的「多面体模型容器」。`Info.getScop(R)` 是**懒构建**：第一次访问某区域时才真正建 `Scop`。若没开 `ScopInfo`（[L132-L133](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L132-L133)），同样直接返回。

**(e) Worklist 与逐-SCoP 循环**

[lib/Pass/PhaseManager.cpp:159-176](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L159-L176) —— 把 `SD`（可迭代的检测区域集合）塞进 `SmallPriorityWorklist`，然后 `while (!Worklist.empty())` 逐个 `pop`。每次取出一个区域 `R`，`Info.getScop(R)` 拿到它的 `Scop *S`：

- 若 `S == nullptr`：跳过。注释（[L167-L173](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L167-L173)）列了三种原因——区域非极大、`ScopBuilder` 判定无效、**或前一个 SCoP 的 codegen 让这个区域不再是 SCoP 了**（这点见 (g)）。
- 若 `!SD.isMaxRegionInScop(*R)`：跳过（避免对子区域重复处理）。

通过后，进入变换链。

**(f) 变换链：flatten → … → codegen**

[lib/Pass/PhaseManager.cpp:178-261](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L178-L261) —— 每个阶段都形如 `if (Opts.isPhaseEnabled(PassPhase::Xxx)) runXxxPass(...)`。注意几个细节：

- `simplify-1` 受 `ModifiedSinceSimplify` 保护（[L218-L221](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L218-L221)）：只有自 `simplify-0` 以来模型**确实被改过**（optree/delicm 有改动）才重跑 simplify，否则跳过——避免无谓重复。
- `ast` 没开会 `continue`（[L245-L246](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L245-L246)）；`codegen` 没开也 `continue`（[L250-L251](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L250-L251)）。

**(g) codegen 改 IR 后丢弃 Scop**

[lib/Pass/PhaseManager.cpp:249-260](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L249-L260) —— 这是全函数第二个改 IR 的点。`runCodeGeneration(*S, RI, *IslAst)` 若返回真（改了 IR），则：

1. `ModifiedIR = true`；
2. `Info.invalidate()` —— **丢弃所有已构建的 `Scop` 对象**。注释（[L255-L259](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L255-L259)）说原因：`Scop` 对象内部引用了 LLVM-IR 指令和 SCEV 表达式，codegen 改写 IR 后这些引用就失效了。`ScopInfo` 会按需重建。

这条 `invalidate()` 直接解释了 (e) 里「前一个 SCoP 的 codegen 可能让后续区域不再是 SCoP」——因为 codegen 破坏了 IR，下一个区域的 `Info.getScop(R)` 重建时可能失败，于是 `S == nullptr` 而被跳过。**因此 SCoP 的处理顺序会影响结果**。

#### 4.2.4 关键细节：deps 阶段其实只控制打印

这是一个极易被源码「选项一致性检查」误导的点。看这段：

[lib/Pass/PhaseManager.cpp:182-190](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L182-L190) —— 注释明说：「**Actual analysis runs on-demand, so it does not matter whether the phase is actually enabled, but use this location to print dependencies.**」（实际分析按需运行，阶段是否启用无关紧要，这个位置只是用来打印依赖。）

也就是说：

- `DependenceAnalysis::Result DA = runDependenceAnalysis(*S)` **无条件执行**——不管 `deps` 选项开没开，`DA` 总会被算出来，供后续 `dce`/`mse`/`opt-isl`/`ast` 等使用。
- `Dependences` 阶段选项**只**控制下面那个 `if (PrintDependences)` 是否打印。

于是 `checkConsistency` 里「`dce` 要求 `deps` 开启」这条规则，更多是一个**用户意图的合理性约束**（你既然要跑依赖敏感的变换，就应该意识到依赖的存在），而非实现层面的硬依赖。

#### 4.2.5 代码实践：跟踪一条完整调用链

**实践目标**：把 `PhaseManager::run()` 这一个函数读透，亲手画出 17 个实质阶段的流程图，并标出哪些阶段接收 `DA`、哪些改 IR。

**操作步骤**：

1. 打开 [lib/Pass/PhaseManager.cpp:66](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L66)，定位 `PhaseManager::run()`。
2. 用笔（或文本）把 `run()` 分成三段誊抄：①函数级预备（取分析 + prepare + detect + scops）；②Worklist 循环骨架；③循环体里的变换链。
3. 对变换链里的每一个 `runXxxPass` 调用，标注三件事：
   - 调用是否被 `if (Opts.isPhaseEnabled(...))` 包住；
   - 是否接收 `DA`（即用了 `DependenceAnalysis::Result`）；
   - 调用结果是否影响 `ModifiedIR`（只有 `runCodeGeneration` 会）。
4. 单独解释 Worklist 循环：为什么用 `SmallPriorityWorklist` 而非普通 `vector`？`Info.getScop(R)` 为什么可能返回 `nullptr`？`Info.invalidate()` 之后，循环里后续的 `Info.getScop(R)` 会发生什么？

**需要观察的现象**（源码阅读型，不依赖运行）：
- 你会发现**只有两处** `ModifiedIR = true`：`prepare`（L87）和 `codegen`（L254）。其余十几个阶段**完全不碰 IR**，只在 `Scop` 模型上操作——这就是「数学空间变换」与「IR 回写」的清晰边界。
- `Info.getScop(R)` 返回 `nullptr` 的注释（L167-L173）列举了三种情形，其中「前一个 SCoP 的 codegen 改坏了后续区域」是 `Info.invalidate()` 的直接后果。

**预期结果**：得到一张与 [4.2.2](#422-核心流程整条流水线一览) 表格一致的流程图，并能口头解释「为什么 `Info` 必须在 codegen 后失效、为什么这会影响下一个 SCoP」。这是后续 U3–U8 每一讲的定位坐标。

#### 4.2.6 小练习与答案

**练习 1**：如果用户写 `-passes=polly-custom<opt-isl>`，`parsePollyOptions` 最终会隐式打开哪些阶段？

**答案**：`opt-isl` 隐式要求 `Detection`、`ScopInfo`（因为它在两者之后），又因 `dependsOnDependenceInfo(opt-isl)` 为真，还隐式打开 `Dependences`。所以最终打开：`Detection`、`ScopInfo`、`Dependences`、`Optimization`。但**不**会打开 `AstGen`/`CodeGen`，所以跑完看不到 IR 变化。依据：[RegisterPasses.cpp:372-389](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Support/RegisterPasses.cpp#L372-L389)。

**练习 2**：为什么 `PhaseManager` 不直接注册成一个普通的 LLVM pass，而要藏在 `PollyFunctionPass` 内部？

**答案**：因为 `PhaseManager` 需要在多个阶段间**手动保持** `LoopInfo`/`DominatorTree`/`ScopInfo` 等状态有效（`ScopDetection` 存有指向它们的引用，不能被 PM 失效重算）。普通 pass 边界会触发 PM 的分析失效机制，破坏这些引用。把它藏在单个 `PollyFunctionPass` 内部，就能在一个 pass 的作用域里独占控制这些分析的生命周期。依据：[PhaseManager.h:9-13](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/include/polly/Pass/PhaseManager.h#L9-L13) 与 [PhaseManager.cpp:67-73](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L67-L73)。

**练习 3**：`simplify-1` 在什么条件下会被跳过？设计意图是什么？

**答案**：当 `simplify-0` 之后没有任何变换改过模型（`ModifiedSinceSimplify == false`）时跳过。意图是：如果 `optree`/`delicm` 这一轮没产生新变化，模型已经是 simplify 过的干净状态，没必要再 simplify 一遍——省编译时间。依据：[PhaseManager.cpp:218-221](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L218-L221)。

---

## 5. 综合实践

**任务**：充当一次「Polly 流水线讲解员」，用一张图 + 一段解说，把整条流水线讲清楚。

1. **画图**：参照 [4.2.2](#422-核心流程整条流水线一览) 的伪代码与表格，自己画一张流程图，要求：
   - 用**两条泳道**区分「函数级一次」（prepare/detect/scops）与「逐-SCoP 循环内」（flatten…codegen）；
   - 在每个阶段框上标注两个符号：🔍（接收/计算 `DA`）与 ✏️（修改 LLVM-IR）。预期只有 codegen 与 prepare 带 ✏️。
2. **解说**：用 5–8 句话向一个没读过 Polly 的同事解释：
   - Polly 为什么先把 IR 翻译成 `Scop` 模型、在模型上变换、最后才写回 IR；
   - 为什么 `PhaseManager` 不能让 PM 随便失效它的分析；
   - codegen 成功后为什么要 `Info.invalidate()`，以及这为何可能让后续 SCoP「消失」。
3. **交叉验证**：把你的图与 [lib/Pass/PhaseManager.cpp:178-261](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/polly/lib/Pass/PhaseManager.cpp#L178-L261) 逐行对照，确认每一条 `runXxxPass` 都在你图里、且符号标注正确。

完成这个练习后，你就拥有了一张「后续 U3–U8 的导航图」——每读一讲，把它对应的那一行 `runXxxPass` 在图上点亮即可。

## 6. 本讲小结

- `PhaseManager::run()` 是 Polly 的**唯一总调度**：它按 `PassPhase` 枚举顺序，用 `PollyPassOptions` 位集决定每个阶段跑不跑。
- 枚举共 24 个阶段（去 `None`），其中 **17 个是实质阶段**、7 个是打印/可视化辅助。
- 三种选项预设：`enableEnd2End`（最小 IR→IR 骨架）、`enableDefaultOpts`（默认变换）、`disableAfter`（调试截断）；`checkConsistency` 守住选项合法性。
- **只有 `prepare` 与 `codegen` 两个阶段改 LLVM-IR**；其余所有变换只改内存里的 `Scop` 多面体模型——这是「数学空间变换」与「IR 回写」的清晰边界。
- `PhaseManager` **不是** PM 里的 pass，而是藏在 `PollyFunctionPass` 内部，目的是手动保持 `LoopInfo`/`DominatorTree`/`ScopInfo` 在所有阶段间有效。
- `Dependences` 阶段**只控制打印**：`DA` 永远按需计算；codegen 成功后 `Info.invalidate()` 丢弃所有 `Scop`，可能导致后续 SCoP 在重建时「消失」。

## 7. 下一步学习建议

本讲给出了「目录」，接下来按数据流自顶向下深入每一行 `runXxxPass`：

- **先看入口的两条岔路**：u2-l2 讲 `PollyFunctionPass`/`PollyModulePass` 如何封装、`parsePollyOptions` 如何把命令行变成 `PollyPassOptions`；u2-l3 讲 `prepare` 阶段背后的规范化 pass 链。
- **再深入检测与建模**：U3（SCoP 检测，对应 `detect` 阶段）、U4（`ScopInfo`/`ScopBuilder`，对应 `scops` 阶段）。
- **然后是依赖与变换**：U5（对应 `deps` 阶段）、U6（`simplify`/`optree`/`delicm`/`dce`/`mse`/`prune` 这一串预优化）。
- **最后是调度与回写**：U7（`opt-isl` 调度优化）、U8（`ast` + `codegen`，以及为什么 codegen 后要 `invalidate`）。

建议阅读时始终带着一个问题回看本讲：**「这个 pass 在 `PhaseManager::run()` 里是哪一行？它改的是 `Scop` 还是 IR？」** 这能把每一讲牢牢锚定在全局地图上。
